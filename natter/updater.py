"""The updater: puts a release Natter Den has downloaded and checked (update.py) in place of the
installed copy, once Natter Den has quit, and starts it again. It runs on its own:

    python3 -I updater.py plan.json

Standard library only, and it never imports natter: the folder natter comes from is the one it
swaps. Natter Den copies this file out of that folder before starting it (CACHE/update), as a
service of the user's systemd where it can (update.helper_command), so it's no child of Natter Den,
else as a process of a session of its own.

Its steps, each with a deadline of its own: wait for Natter Den and the processes it started to
end; take the instance lock, so no Natter Den starts while it works; check the `natter` command
still starts the copy it replaces; swap the folders; run the new copy's install.sh; end a Natter
Den that D-Bus started meanwhile (it waits on the lock, with nothing loaded); start the new one and
wait for it to say it's ready. Whatever fails after the swap puts the old copy back, with its
launcher and its own install.sh (rollback), and starts it, always under the lock. What happened
goes to result.json, which the next Natter Den reads (update.Updates.started), and to update.log:
paths and install.sh's own output only. A Natter Den that stays after all (a call came in as the
updater started) says so with the file `cancel` beside the plan (update.Updates.stay), and nothing
changes.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

APP_ID = "io.github.Officialbabs.Natter"  # only the default instance updates (update.channel)
LOCK_NAME = f"natter-{APP_ID}.lock"  # instance.lock_path's file
# A release's tag: v and two to four numbers, without leading zeros. get.sh's sane_tag accepts
# them all; anything else ("v1.0-rc1") is never offered.
TAG = re.compile(r"v(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){1,3}")
LAUNCHER_LINE = re.compile(r'^NATTER_DIR = (".*")$', re.M)  # as install.sh writes it (py_quote)
STATES = ("planned", "swapped", "installed", "started", "failed", "told")
CANCEL = "cancel"  # beside the plan: Natter Den stays after all (update.Updates.stay)
UPDATING = "updating"  # after the pid and start time in the lock's file (instance.UPDATING): it's the updater
# Seconds each step may take at most: Natter Den quitting (QUIT_WAIT and the players' answers take
# about 2 s), the lock, install.sh, a Natter Den D-Bus started meanwhile ending, and the new one
# reaching ready. A plan may lower them (the tests do), never raise them. There's no limit on the
# whole unit: a Natter Den the updater started and that is still in its cgroup would end with it,
# and one that took the plan but outlived "quit" is waited for, to be started again once it ends.
LIMITS = {"quit": 60.0, "lock": 10.0, "install": 300.0, "others": 10.0, "start": 120.0}
POLL = 0.1
LOCK_POLL = 0.25
DETAIL = 200  # characters of a line kept for the failure's words
PLAN_BYTES = 1 << 20


# ---- Shared with update.py (which imports this file; this file imports nothing of Natter's) ----

def launcher_dir(text: str) -> Optional[str]:
    """The folder the `natter` launcher runs Natter Den from: its NATTER_DIR line, read as the
    string it is, never run. None when there's none, or it's "" (the launcher's own checkout)."""
    match = LAUNCHER_LINE.search(text or "")
    if not match:
        return None
    try:
        value = ast.literal_eval(match[1])
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) and value else None


def version_in(folder) -> Optional[str]:
    """The VERSION of the copy of Natter Den in `folder`, read from its natter/__init__.py, parsed
    and never run: its one `VERSION = "…"`. None when there's none, or when anything else there
    gives VERSION a value too (a second assignment, an import, a def), as the last would win."""
    try:
        with open(os.path.join(folder, "natter", "__init__.py"), encoding="utf-8") as source:
            tree = ast.parse(source.read(64 * 1024))
    except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
        return None
    named = [node for node in ast.walk(tree) if _names(node, "VERSION")]
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(named) == 1 and len(statement.targets) == 1 \
                and statement.targets[0] is named[0] and isinstance(statement.value, ast.Constant) \
                and isinstance(statement.value.value, str):
            return statement.value.value
    return None


def _names(node, name: str) -> bool:
    """Whether `node` gives `name` a value, as plainly written: assigned (or deleted), imported (a
    star import might), defined."""
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.alias):
        return node.name == "*" or (node.asname or node.name.split(".")[0]) == name
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    return False


def write_result(path, result: dict) -> None:
    """Replace result.json at once, readable by this user only: settings.write_private's way (a
    .tmp, fsync, rename), so a crash leaves the old one, never half of the new."""
    path = str(path)
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600),
                   "w") as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(result, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def read_result(path) -> Optional[dict]:
    """result.json as the updater (or Natter Den) last wrote it, its fields checked; None when
    there's none or it isn't one."""
    try:
        with open(os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as source:
            data = json.loads(source.read(64 * 1024))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("state") not in STATES or not isinstance(data.get("tag"), str) \
            or not TAG.fullmatch(data["tag"]):
        return None
    result = {"tag": data["tag"], "state": data["state"]}
    for key in ("from", "to", "why", "detail", "log", "kept"):
        result[key] = data.get(key) if isinstance(data.get(key), str) else ""
    at = data.get("at")
    result["at"] = int(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0
    helper = data.get("helper")
    if isinstance(helper, list) and len(helper) == 2 and all(isinstance(n, int) and n > 0 for n in helper):
        result["helper"] = helper
    return result


def start_ticks(pid: int) -> Optional[int]:
    """When process `pid` started, in clock ticks since boot: with the pid, it names one process."""
    fields = _stat(pid)
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def alive(pid: int, ticks: Optional[int] = None) -> bool:
    """Whether process `pid` (the one that started at `ticks`, when given) still runs: a zombie
    its parent hasn't reaped counts as ended."""
    fields = _stat(pid)
    if not fields or fields[0] in ("Z", "X"):
        return False
    return not ticks or start_ticks(pid) == ticks


def _stat(pid: int) -> list[str]:
    try:
        with open(f"/proc/{int(pid)}/stat") as stat:
            return stat.read().rpartition(")")[2].split()
    except (OSError, ValueError):
        return []


def children(pid: int) -> set[int]:
    """The processes `pid` started that are still its children: WebKit's, mostly."""
    found: set[int] = set()
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return found
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/children") as listed:
                found |= {int(child) for child in listed.read().split()}
        except (OSError, ValueError):
            pass
    return found


def own_process(pid: int) -> bool:
    """Whether a child of Natter Den's is one of its own: WebKit's processes, the sandbox and bus
    proxy they run in, or the call window (python3 -m natter.callwindow). Not an app it opened a
    link or a file in, which may run for hours: a Flatpak one runs in bwrap too, so bwrap counts
    only with WebKit's process or the bus proxy in its command."""
    try:
        with open(f"/proc/{pid}/comm") as comm:
            name = comm.read().strip()
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
            words = cmdline.read(4096).split(b"\0")
    except OSError:
        return False
    if name == "bwrap":
        return any(os.path.basename(word).startswith(b"WebKit") or os.path.basename(word) == b"xdg-dbus-proxy"
                   for word in words[1:])
    return name.startswith("WebKit") or name == "xdg-dbus-proxy" or any(word.startswith(b"natter.") for word in words)


def home_path(path, home: str) -> str:
    """`path` as people read it: ~/… when it's in the home folder."""
    path = str(path)
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        return "~" + path[len(home.rstrip("/")):]
    return path


def plan_problem(plan, plan_path, home: str) -> str:
    """What's wrong with a plan, in words for the log; "" when nothing is. Only what Natter Den
    writes passes: the copy get.sh installed in this home, a stage beside it, the default
    instance's lock, and its own files next to the plan."""
    if not isinstance(plan, dict):
        return "it isn't a plan"
    for key in ("tag", "from", "to", "source", "stage", "launcher", "lock", "result", "log"):
        if not isinstance(plan.get(key), str) or not plan[key]:
            return f"it has no {key}"
    if not TAG.fullmatch(plan["tag"]) or plan["to"] != plan["tag"][1:]:
        return "its tag isn't a version"
    for key in ("pid", "ticks"):
        if not isinstance(plan.get(key), int) or isinstance(plan[key], bool) or plan[key] < (2 if key == "pid" else 0):
            return f"it has no {key}"
    if plan.get("args") not in ([], ["--background"]):
        return "its arguments are neither none nor --background"
    env = plan.get("env")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return "it has no environment"
    limits = plan.get("limits", {})
    if not isinstance(limits, dict) or not all(name in LIMITS and isinstance(value, (int, float))
                                               and not isinstance(value, bool) and value > 0
                                               for name, value in limits.items()):
        return "its limits aren't numbers"
    source = os.path.realpath(os.path.join(home, ".local", "lib", "natter", "source"))
    if plan["source"] != source:
        return f"its source isn't {home_path(source, home)}"
    stage = Path(plan["stage"])
    if stage.parent != Path(source).parent / ".update" or stage.name != plan["tag"]:
        return "its stage isn't beside the source"
    if not (stage / "source").is_dir() or (stage / "source").is_symlink():
        return "nothing is staged"
    if not os.path.isabs(plan["lock"]) or os.path.basename(plan["lock"]) != LOCK_NAME:
        return "its lock isn't the instance lock"
    if plan["launcher"] != os.path.join(home, ".local", "bin", "natter"):
        return "its launcher isn't ~/.local/bin/natter"
    folder = os.path.dirname(os.path.abspath(str(plan_path)))
    if plan["result"] != os.path.join(folder, "result.json") or plan["log"] != os.path.join(folder, "update.log"):
        return "its result and log aren't beside it"
    return ""


# ---- The update -------------------------------------------------------------------------------

class Failed(Exception):
    """A step that failed: `why` is update.why_words's key, `detail` what it adds."""

    def __init__(self, why: str, detail: str = "") -> None:
        super().__init__(why)
        self.why = why
        self.detail = detail


class Updater:
    """One update, by a plan plan_problem passed: run() takes the steps in the module's docstring
    and returns the exit status (0 when the new copy runs). Made before the plan is taken: its log
    is opened here, and a log that can't be leaves the plan where it is, for Natter Den to see."""

    def __init__(self, plan: dict, home: str) -> None:
        self.plan = plan
        self.home = home
        self.source = Path(plan["source"])
        self.stage = Path(plan["stage"])
        self.staged = self.stage / "source"
        self.old = self.stage / "old"
        self.backup = self.stage / "natter.launcher"
        self.launcher = Path(plan["launcher"])
        self.cancel = Path(plan["result"]).with_name(CANCEL)
        self.unit = own_unit()
        given = plan.get("limits", {})
        self.limits = {name: min(limit, float(given.get(name, limit))) for name, limit in LIMITS.items()}
        self.lock_fd: Optional[int] = None
        self.swapped = False  # the new copy is in the source's place
        self.rolled_back = False
        self.launched = False  # a Natter Den was started
        self.start_detail = ""  # how the new one ended before it was ready
        self.log_fd = os.open(plan["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        self.result = {"tag": plan["tag"], "from": plan["from"], "to": plan["to"], "state": "planned", "why": "",
                       "detail": "", "log": home_path(plan["log"], home),
                       "helper": [os.getpid(), start_ticks(os.getpid()) or 1]}

    def say(self, text: str) -> None:
        """A step, in the log with the time, and in the journal."""
        line = f"natter: updates: {text}"
        try:
            os.write(self.log_fd, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n".encode())
        except OSError:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    def mark(self, state: str, why: str = "", detail: str = "") -> None:
        self.result.update(state=state, why=why, detail=detail, at=int(time.time()))
        try:
            write_result(self.plan["result"], self.result)
        except OSError as error:
            self.say(f"couldn't write the result ({error.strerror or error})")

    def shown(self, path) -> str:
        return home_path(path, self.home)

    def run(self) -> int:
        try:
            return self._run()
        except Failed as failed:
            return self.fail(failed.why, failed.detail)
        except Exception as error:  # a bug: put back what can be, and start whatever the source holds
            self.say(f"stopped by {type(error).__name__}")
            try:
                # Put back only under the lock: once it was let go, a Natter Den may run from the folder.
                if self.swapped and not self.rolled_back and (self.lock_fd is not None or self.take_lock(0)):
                    return self.rollback("unfinished", "")
                if not self.rolled_back:
                    self.mark("failed", "unfinished")
                self.release()
                if not self.launched and self.source.is_dir():
                    self.start(expect_ready=False)
            except Exception as again:
                self.say(f"and again by {type(again).__name__}")
            return 1

    def _run(self) -> int:
        self.say(f"updating to {self.plan['tag']}; waiting for pid {self.plan['pid']} to end")
        self.mark("planned")  # with the updater's pid, now it's at work
        ended = self.wait_for_quit(self.limits["quit"])
        if ended == "stayed":
            return self.stayed()
        if ended == "running":
            # Natter Den is still there: nothing is swapped under it, and no second one starts. It
            # took the plan to quit, though (a frozen one, thawed, does): once it has, it starts
            # again as it was, as app.restart would start it.
            self.say(f"pid {self.plan['pid']} is still running after {self.limits['quit']:g} s; nothing changed, "
                     "and it starts again once it has ended")
            self.mark("failed", "busy")
            if self.wait_for_quit(None) == "stayed":
                return self.stayed()
            self.start(expect_ready=False)
            return 1
        if ended == "children":
            # It has gone, but WebKit or the call window hasn't: nothing is swapped under them, and
            # Natter Den starts as it is, as app.restart would start it.
            self.say("processes it started are still running; nothing changed")
            return self.fail("busy")
        if not self.take_lock(self.limits["lock"]):
            self.say("another one holds the instance lock; nothing changed")
            return self.fail("other", start=False)
        named = launcher_dir(self.read_launcher())
        if named is None or os.path.realpath(named) != str(self.source):
            self.say(f"{self.shown(self.launcher)} starts {self.shown(named or 'something else')}, "
                     f"not {self.shown(self.source)}; nothing changed")
            return self.fail("launcher")
        self.swap()
        status, last = self.install(self.source)
        if status is None:
            raise Failed("timeout")
        if status != 0:
            raise Failed("install", last or f"exit status {status}")
        found = version_in(self.source)
        if found != self.plan["to"]:
            raise Failed("install", f"the new copy says it's {found or 'no version'}")
        named = launcher_dir(self.read_launcher())
        if named is None or os.path.realpath(named) != str(self.source):
            raise Failed("install", f"{self.shown(self.launcher)} doesn't start the new copy")
        self.mark("installed")
        self.end_others()
        self.release()
        outcome = self.start(expect_ready=True)
        if outcome == "ended":
            # The lock is the updater's again (start took it), so a Natter Den opened meanwhile,
            # as no window came, waits: nothing is put back under one.
            self.say(f"the new one ended before it was ready ({self.start_detail})"
                     + (f"; what it said is in the journal: journalctl --user -u {self.unit}" if self.unit else ""))
            return self.rollback("start", self.start_detail)
        self.say("done" if outcome == "ready" else "the new one is still starting; it says when it's ready")
        return 0

    def stayed(self) -> int:
        """Natter Den stays after all (a call came in as the updater started, update.Updates.stay):
        nothing changed, and it knows, so the next start has nothing to say."""
        self.say(f"pid {self.plan['pid']} stays after all (a call came in); nothing changed")
        try:
            os.unlink(self.cancel)
        except OSError:
            pass
        self.mark("told", "busy")
        return 1

    # ---- The steps ------------------------------------------------------------------------------

    def wait_for_quit(self, limit: Optional[float]) -> str:
        """Wait for Natter Den and the processes of its own it started (WebKit's, their sandbox,
        the call window) to end: two WebKits on one WhatsApp session can damage it. Its children
        are gathered while it runs (after, they're no longer its), by pid and start time, as pids
        are reused; an app it opened a link or a file in isn't waited for. "ended" when they all
        have, "running" when Natter Den itself outlived `limit` (None: for as long as it runs),
        "children" when only its processes outlived the "quit" limit, "stayed" when it said it
        stays (the cancel file)."""
        deadline = None if limit is None else time.monotonic() + limit
        pid, ticks = self.plan["pid"], self.plan["ticks"]
        mine = {os.getpid(), os.getppid()}
        started: dict[int, Optional[int]] = {}
        while alive(pid, ticks):
            if os.path.lexists(self.cancel):
                return "stayed"
            # Looked at again each time until it's one: a child just forked isn't what it runs yet.
            for child in children(pid) - mine - set(started):
                if own_process(child):
                    started[child] = start_ticks(child)
            if deadline is not None and time.monotonic() > deadline:
                return "running"
            time.sleep(POLL)
        if os.path.lexists(self.cancel):  # it stayed, then quit itself
            return "stayed"
        if deadline is None:
            deadline = time.monotonic() + self.limits["quit"]
        waiting = {child: when for child, when in started.items() if when is not None and alive(child, when)}
        if waiting:
            self.say(f"waiting for {len(waiting)} process{'es' if len(waiting) > 1 else ''} it started to end")
        for child, when in waiting.items():
            while alive(child, when):
                if time.monotonic() > deadline:
                    return "children"
                time.sleep(POLL)
        return "ended"

    def take_lock(self, wait: float) -> bool:
        """The instance lock, as Natter Den takes it (instance.InstanceLock), waiting up to `wait`
        seconds for it: a Natter Den that starts meanwhile waits for it, with nothing loaded. The
        pid, the start time and "updating" go in the file: with no bus noted, instance.end_stale
        never ends the updater, and instance.taking waits for it as long as install.sh may take.
        No lock to be had at all (no runtime folder) doesn't stop it, as it doesn't stop Natter Den."""
        if self.lock_fd is not None:
            return True
        try:
            fd = os.open(self.plan["lock"], os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            self.say(f"no instance lock ({error.strerror or error}); going on without it")
            return True
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(fd)
                    return False
                time.sleep(LOCK_POLL)
        self.lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()} {start_ticks(os.getpid()) or 0} {UPDATING}\n".encode(), 0)
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)  # the lock goes with it
            self.lock_fd = None

    def read_launcher(self) -> str:
        try:
            with open(self.launcher, encoding="utf-8", errors="replace") as text:
                return text.read(256 * 1024)
        except OSError:
            return ""

    def swap(self) -> None:
        """The new copy in the source's place, in one step (exchange), the old one kept in the
        stage, with the launcher."""
        try:
            with open(self.launcher, "rb") as launcher:
                saved = launcher.read()
            mode = os.stat(self.launcher).st_mode & 0o777
            with open(os.open(self.backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as out:
                out.write(saved)
            os.chmod(self.backup, mode)
        except OSError as error:
            raise Failed("install", f"couldn't keep a copy of {self.shown(self.launcher)}: {error.strerror or error}")
        self.say(f"putting {self.shown(self.staged)} in place of {self.shown(self.source)}, which goes to "
                 f"{self.shown(self.old)}")
        try:
            exchange(self.staged, self.source)
        except OSError as error:
            raise Failed("install", f"couldn't swap {self.shown(self.source)}: {error.strerror or error}")
        self.swapped = True  # the old copy is out of its place: from here on, a failure puts it back
        self.mark("swapped")
        try:
            os.rename(self.staged, self.old)
        except OSError as error:
            self.old = self.staged  # where it is now: the rollback takes it from there
            self.say(f"couldn't move it to {self.shown(self.stage / 'old')} ({error.strerror or error}); "
                     f"it stays in {self.shown(self.old)}")

    def install(self, folder: Path) -> tuple[Optional[int], str]:
        """Run `folder`'s install.sh with --force (the updater holds the lock: no Natter Den runs)
        and --no-deps (it never asks, never runs sudo), its output in the log. (exit status, its
        last line), or (None, …) when it was ended at the limit."""
        self.say(f"running {self.shown(folder / 'install.sh')} --force --no-deps")
        start = os.fstat(self.log_fd).st_size
        try:
            process = subprocess.Popen(["/bin/sh", str(folder / "install.sh"), "--force", "--no-deps"],
                                       stdin=subprocess.DEVNULL, stdout=self.log_fd, stderr=self.log_fd,
                                       cwd=self.home, env=self.plan["env"], start_new_session=True)
        except OSError as error:
            return 127, f"couldn't run it: {error.strerror or error}"
        try:
            status: Optional[int] = process.wait(timeout=self.limits["install"])
        except subprocess.TimeoutExpired:
            self.say(f"install.sh took over {self.limits['install']:g} s; ending it")
            self.end_group(process)
            status = None
        try:
            with open(self.plan["log"], "rb") as log:
                log.seek(start)
                output = log.read().decode(errors="replace")
        except OSError:
            output = ""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        last = lines[-1][:DETAIL] if lines else ""
        self.say(f"install.sh ended with exit status {status}" if status is not None else "install.sh was ended")
        return status, last

    def end_group(self, process: subprocess.Popen) -> None:
        for signal_number, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(process.pid, signal_number)
            except OSError:
                pass
            try:
                process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                pass

    def end_others(self) -> None:
        """A Natter Den started meanwhile (D-Bus activation: a notification's click, autostart)
        waits on the lock, with nothing loaded. Every process that has the lock's file open is
        one: each is asked to stop, and quits before its start-up does anything; one still there
        after the limit is ended. Each is held by a pidfd first, then looked at again: a signal
        reaches that process, never one that took its pid since."""
        others = lock_users(self.plan["lock"]) - {os.getpid(), os.getppid()}
        if not others:
            return
        self.say(f"asking {len(others)} other start{'s' if len(others) > 1 else ''} waiting on the lock to stop")
        held: dict[int, tuple[Optional[int], Optional[int]]] = {}
        for pid in others:
            try:
                fd: Optional[int] = os.pidfd_open(pid)
            except OSError:
                continue  # gone
            except AttributeError:
                fd = None  # no pidfd here: by its pid and start time alone
            if fd is not None and not lock_users(self.plan["lock"], [pid]):
                os.close(fd)
                continue
            held[pid] = (fd, start_ticks(pid))
        try:
            for pid, (fd, _ticks) in held.items():
                send(pid, fd, signal.SIGTERM)
            deadline = time.monotonic() + self.limits["others"]
            while any(alive(pid, ticks) for pid, (_fd, ticks) in held.items()) and time.monotonic() < deadline:
                time.sleep(POLL)
            for pid, (fd, ticks) in held.items():
                if alive(pid, ticks):
                    self.say(f"pid {pid} didn't stop; ending it")
                    send(pid, fd, signal.SIGKILL)
        finally:
            for fd, _ticks in held.values():
                if fd is not None:
                    os.close(fd)

    def start(self, expect_ready: bool) -> str:
        """Start Natter Den with the plan's arguments. With `expect_ready`: "ready" once it says
        so in result.json (Updates.started), "ended" when it ended before that and the instance
        lock could be had again, which the updater then holds (no other Natter Den came up, and
        none starts while the old copy goes back), "left" when it's left still starting. Run as a
        service of its own (own_unit), the updater waits as long as it runs for a Natter Den
        still in its cgroup (its move to a scope of its own failed, or hasn't happened by the
        limit): the service's end would end it too."""
        self.launched = True
        args = list(self.plan["args"])
        self.say(f"starting {self.shown(self.launcher)}{' ' + ' '.join(args) if args else ''}")
        try:
            child = subprocess.Popen([str(self.launcher), *args], env=self.plan["env"], stdin=subprocess.DEVNULL,
                                     cwd=self.home, start_new_session=True)
        except OSError as error:
            self.say(f"couldn't start it ({error.strerror or error})")
            self.start_detail = f"couldn't start it: {error.strerror or error}"
            return "ended" if not expect_ready or self.take_lock(0) else "left"
        deadline = time.monotonic() + self.limits["start"]
        ready = False
        while True:
            status = child.poll()
            if expect_ready and not ready:  # read after the look: what it wrote before it ended is there
                found = read_result(self.plan["result"])
                ready = found is not None and found["state"] in ("started", "told")
            done = ready or not expect_ready
            late = time.monotonic() > deadline
            if status is not None:
                if done:
                    return "ready" if expect_ready else "left"
                if self.take_lock(0):
                    self.start_detail = ending(status)
                    return "ended"
                # Another Natter Den holds the lock (this one handed its start over to it): the
                # new copy too, which says so once it's ready.
                if late:
                    return "left"
            elif not self.unit or not same_cgroup(child.pid):
                if done:
                    return "ready" if expect_ready else "left"
                if late:
                    return "left"
            elif ready or (done and late):
                self.say("it's still in the updater's cgroup; waiting for it to end")
                child.wait()
                return "ready" if expect_ready else "left"
            time.sleep(POLL * 2)

    def fail(self, why: str, detail: str = "", start: bool = True) -> int:
        """A step failed: after the swap, the rollback; before it, nothing changed, and the
        launcher is started as it is (`start`)."""
        if self.swapped:
            return self.rollback(why, detail)
        self.mark("failed", why, detail)
        self.release()
        if start:
            self.start(expect_ready=False)
        return 1

    def rollback(self, why: str, detail: str) -> int:
        """Put the old copy back, under the lock: its folder (exchanged with the new one, which
        goes to failed), its launcher, then its own install.sh, for the menu entry, the service
        file, the icons and the camera fix. Then it's started."""
        self.rolled_back = True
        self.say(f"putting {self.shown(self.old)} back")
        try:
            if os.path.lexists(self.source):
                exchange(self.old, self.source)
                failed = self.stage / "failed"
                if os.path.lexists(failed):
                    failed = self.stage / f"failed-{int(time.time())}"
                try:
                    os.rename(self.old, failed)
                except OSError as error:
                    self.say(f"the new copy stays in {self.shown(self.old)} ({error.strerror or error})")
            else:
                os.rename(self.old, self.source)
            back = True
        except OSError as error:
            back = False
            self.result["kept"] = self.shown(self.old)
            self.say(f"couldn't put it back ({error.strerror or error}); the old copy is in {self.shown(self.old)}")
        try:
            with open(self.backup, "rb") as saved:
                launcher = saved.read()
            new = Path(str(self.launcher) + ".new")
            with open(os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as out:
                out.write(launcher)
            os.chmod(new, os.stat(self.backup).st_mode & 0o777)
            os.replace(new, self.launcher)
        except OSError as error:
            self.say(f"couldn't put {self.shown(self.launcher)} back ({error.strerror or error})")
        if back:
            self.install(self.source)  # whatever it says: the old copy runs either way
        self.mark("failed", why, detail)
        # A start that waited on the lock meanwhile came from the new copy: it mustn't go on with
        # the old one's files, so it's ended, as before a start, and the old one starts after.
        self.end_others()
        self.release()
        if os.path.isdir(self.source):
            self.start(expect_ready=False)
        return 1


def ending(status: int) -> str:
    """How a process ended, in words for the log and the failure's detail."""
    return f"exit status {status}" if status >= 0 else f"ended by signal {-status}"


def send(pid: int, fd: Optional[int], number: int) -> None:
    """Signal `number` to the process a pidfd holds, or by its pid where there's none."""
    try:
        if fd is not None:
            signal.pidfd_send_signal(fd, number)
        else:
            os.kill(pid, number)
    except OSError:
        pass


# Signals that would end the updater between two renames, held while it makes them.
ENDING = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT}
RENAME_EXCHANGE = 2  # linux/fs.h
AT_FDCWD = -100


def exchange(first: Path, second: Path) -> None:
    """Swap two folders of one filesystem in one step (renameat2's RENAME_EXCHANGE): whenever the
    updater stops, a power cut included, each place holds one or the other, never neither. Where
    the filesystem or the C library can't, by three renames, with the signals that would end the
    updater held meanwhile. Raises OSError."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    except (OSError, AttributeError):
        renameat2 = None
    if renameat2 is not None:
        if renameat2(AT_FDCWD, os.fsencode(str(first)), AT_FDCWD, os.fsencode(str(second)), RENAME_EXCHANGE) == 0:
            sync_folder(second.parent)
            return
        number = ctypes.get_errno()
        if number not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(number, os.strerror(number), str(first))
    spare = second.with_name(second.name + ".swapping")
    held = signal.pthread_sigmask(signal.SIG_BLOCK, ENDING)
    try:
        os.rename(second, spare)
        try:
            os.rename(first, second)
        except OSError:
            os.rename(spare, second)
            raise
        os.rename(spare, first)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)
    sync_folder(second.parent)


def sync_folder(folder: Path) -> None:
    """The folder's entries on the disk: a rename in it is there after a power cut."""
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def lock_users(path, pids=None) -> set[int]:
    """The processes (of `pids`, else all) that have the instance lock's file open (only this
    user's can be seen). By the name each open file has, never by looking at the file: that could
    wait on a mount that doesn't answer."""
    wanted = os.path.realpath(str(path))
    found = set()
    for name in (os.listdir("/proc") if pids is None else [str(pid) for pid in pids]):
        if not name.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{name}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                opened = os.readlink(f"/proc/{name}/fd/{fd}")
            except OSError:
                continue
            if opened == wanted:
                found.add(int(name))
                break
    return found


def lock_free(path) -> bool:
    """Whether nobody holds the instance lock now (taken for a moment, to see)."""
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def cgroup(pid) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cgroup") as listed:
            for line in listed:
                if line.startswith("0::"):
                    return line[3:].strip()
    except OSError:
        pass
    return None


def same_cgroup(pid: int) -> bool:
    """Whether `pid` is still in the updater's own cgroup (when that can't be told, it is)."""
    mine, theirs = cgroup("self"), cgroup(pid)
    return mine is None or theirs is None or mine == theirs


def own_unit() -> str:
    """The service of the user's systemd the updater runs as (update.helper_command's
    natter-update-….service), "" when it doesn't: started by Natter Den itself, it shares the
    old one's scope, which outlives the updater, and the new one may stay there."""
    name = os.path.basename(cgroup("self") or "")
    return name if name.startswith("natter-update-") and name.endswith(".service") else ""


def main(argv: list[str]) -> int:
    try:
        os.setsid()  # started by Natter Den itself (no systemd-run): what ends its session doesn't end this
    except OSError:
        pass  # a session of its own already: systemd's service
    if len(argv) != 2:
        print("usage: updater.py plan.json", file=sys.stderr)
        return 2
    plan_path = os.path.abspath(argv[1])
    home = os.path.expanduser("~")
    try:
        with open(os.open(plan_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as source:
            plan = json.loads(source.read(PLAN_BYTES))
    except (OSError, ValueError) as error:
        print(f"natter: updates: no plan to follow ({type(error).__name__})", file=sys.stderr, flush=True)
        return 1
    problem = plan_problem(plan, plan_path, home)
    if problem:
        print(f"natter: updates: not updating: {problem}", file=sys.stderr, flush=True)
        return 1
    try:
        update = Updater(plan, home)
    except OSError as error:  # its log: nothing taken, nothing changed, and Natter Den keeps running
        print(f"natter: updates: not updating: no log ({error.strerror or error})", file=sys.stderr, flush=True)
        return 1
    # Taken by moving it: one updater follows a plan, even if a slow systemd-run started a second
    # (update.start_helper). Natter Den quits once it's gone.
    taken = f"{plan_path}.{os.getpid()}"
    try:
        os.rename(plan_path, taken)
        os.unlink(taken)
    except OSError:
        print("natter: updates: another updater has the plan", file=sys.stderr, flush=True)
        return 1
    return update.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
