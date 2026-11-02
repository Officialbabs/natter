"""The updater (natter/updater.py), run whole as Natter starts it, in a scratch home: it waits for a
stand-in Natter to end, takes the instance lock, swaps the installed copy for the staged one, runs
the new install.sh (a stand-in that writes the launcher as install.sh does), and starts the new
copy through a stand-in launcher, which says it's ready or ends without saying so. Every failure
after the swap puts the old copy back. Never systemd-run (only the command is checked, in
test_update.py), never the real home, the network or a session bus."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from natter import updater

ROOT = Path(__file__).resolve().parent.parent
UPDATER = ROOT / "natter" / "updater.py"
LOCK = "natter-io.github.Officialbabs.Natter.lock"

# install.sh, as far as the updater is concerned: says it ran (with its arguments and version),
# fails or hangs when asked to, else writes ~/.local/bin/natter from its own bin/natter with
# NATTER_DIR set to its folder, as the real one does.
INSTALL_SH = r"""#!/bin/sh
here="$(cd "$(dirname "$0")" && pwd)"
version="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$here/natter/__init__.py")"
echo "install.sh $version $*" >> "$FAKE_LOG"
echo "==> Installing the natter command"
if [ "$version" = "$FAKE_FAIL" ]; then echo "Couldn't build the camera fix."; exit 3; fi
if [ "$version" = "$FAKE_HANG" ]; then echo $$ > "$FAKE_LOG.hung"; exec sleep 30; fi
mkdir -p "$HOME/.local/bin"
sed -e "s|^NATTER_DIR = .*|NATTER_DIR = \"$here\"|" "$here/bin/natter" > "$HOME/.local/bin/natter.new"
chmod 755 "$HOME/.local/bin/natter.new"
mv -f "$HOME/.local/bin/natter.new" "$HOME/.local/bin/natter"
echo "Done."
"""

# The natter command: says it started (its version and arguments), and when it's the new one and
# FAKE_START is "ready", marks result.json "started" as Updates.started does, then ends ("ready-stay":
# runs on a while after, as Natter does; "die": ends with status 3).
LAUNCHER = """#!{python}
import json, os, re, sys, time
NATTER_DIR = ""
with open(os.path.join(NATTER_DIR, "natter", "__init__.py")) as source:
    version = re.search(r'VERSION = "(.*)"', source.read())[1]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(["started", version, *sys.argv[1:]]) + "\\n")
if os.environ.get("FAKE_START", "").startswith("ready") and version == os.environ.get("FAKE_NEW"):
    path = os.environ["FAKE_RESULT"]
    with open(path) as result:
        found = json.load(result)
    if found["state"] in ("installed", "started"):
        found["state"] = "started"
        with open(path + ".tmp", "w") as out:
            json.dump(found, out)
        os.replace(path + ".tmp", path)
    if os.environ["FAKE_START"] == "ready-stay":
        with open(os.environ["FAKE_LOG"] + ".stay", "w") as out:
            out.write(str(os.getpid()))
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)  # not the updater's output, which the test reads to its end
        os.dup2(null, 2)
        time.sleep(20)
elif os.environ.get("FAKE_START") == "die" and version == os.environ.get("FAKE_NEW"):
    time.sleep(0.5)  # what a start that fails takes, at least
    sys.exit(3)
"""


def ticks(pid):
    return updater.start_ticks(pid)


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.runtime = base / "run"
        self.runtime.mkdir(mode=0o700)
        self.lib = self.home / ".local/lib/natter"
        self.source = self.lib / "source"
        self.stage = self.lib / ".update/v0.2.0"
        self.launcher = self.home / ".local/bin/natter"
        self.cache = self.home / ".cache/natter/update"
        self.cache.mkdir(parents=True, mode=0o700)
        self.log = base / "log"
        self.log.write_text("")
        self.copy(self.source, "0.1.9")
        self.copy(self.stage / "source", "0.2.0")
        self.environ = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                        "XDG_DATA_HOME": str(self.home / ".local/share"), "XDG_CONFIG_HOME": str(self.home / ".config"),
                        "XDG_CACHE_HOME": str(self.home / ".cache"), "XDG_STATE_HOME": str(self.home / ".local/state"),
                        "XDG_RUNTIME_DIR": str(self.runtime), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus",
                        "FAKE_LOG": str(self.log), "FAKE_RESULT": str(self.cache / "result.json"), "FAKE_NEW": "0.2.0",
                        "FAKE_START": "ready"}
        self.install_launcher(self.source)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        self.tmp.cleanup()

    def copy(self, folder, version):
        (folder / "natter").mkdir(parents=True)
        (folder / "bin").mkdir()
        (folder / "natter/__init__.py").write_text(f'VERSION = "{version}"\n')
        (folder / "install.sh").write_text(INSTALL_SH)
        (folder / "install.sh").chmod(0o755)
        (folder / "bin/natter").write_text(LAUNCHER.format(python=sys.executable))
        (folder / "bin/natter").chmod(0o755)

    def install_launcher(self, folder):
        """The natter command as the old install.sh left it."""
        self.launcher.parent.mkdir(parents=True, exist_ok=True)
        text = (folder / "bin/natter").read_text().replace('NATTER_DIR = ""', f"NATTER_DIR = {json.dumps(str(folder))}")
        self.launcher.write_text(text)
        self.launcher.chmod(0o755)

    def spawn(self, *command):
        process = subprocess.Popen(list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def natter(self, seconds=0.3):
        """The Natter that's quitting, by its pid and start time."""
        return self.spawn("sleep", str(seconds))

    def plan(self, old=None, args=(), **more):
        old = old or self.natter()
        plan = {"tag": "v0.2.0", "from": "0.1.9", "to": "0.2.0", "pid": old.pid, "ticks": ticks(old.pid),
                "source": str(self.source), "stage": str(self.stage), "launcher": str(self.launcher),
                "lock": str(self.runtime / LOCK), "result": str(self.cache / "result.json"),
                "log": str(self.cache / "update.log"), "args": list(args), "env": self.environ,
                "limits": {"quit": 10, "lock": 1, "install": 20, "others": 3, "start": 15}}
        for key, value in more.items():
            if key == "limits":
                plan["limits"].update(value)
            else:
                plan[key] = value
        return plan

    def run_updater(self, plan, write=True):
        path = self.cache / "plan.json"
        if write:
            path.write_text(json.dumps(plan))
        done = subprocess.run([sys.executable, "-I", str(UPDATER), str(path)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, env=self.environ, cwd=str(self.home),
                              start_new_session=True, timeout=60)
        result = updater.read_result(self.cache / "result.json")
        try:
            log = (self.cache / "update.log").read_text()
        except OSError:
            log = ""
        return SimpleNamespace(code=done.returncode, out=done.stdout + done.stderr, result=result, log=log,
                               logged=self.log.read_text().splitlines(), plan_left=path.exists())

    def version(self):
        return updater.version_in(self.source)

    def launcher_dir(self):
        return updater.launcher_dir(self.launcher.read_text())

    # ---- Cases ---------------------------------------------------------------------------------

    def test_success(self):
        run = self.run_updater(self.plan(args=["--background"]))
        self.assertEqual(run.code, 0, run.out + run.log)
        self.assertEqual(self.version(), "0.2.0")
        self.assertEqual(updater.version_in(self.stage / "old"), "0.1.9")  # kept until the new one is ready
        self.assertEqual(self.launcher_dir(), str(self.source))
        self.assertEqual(run.logged, ["install.sh 0.2.0 --force --no-deps", "started 0.2.0 --background"])
        self.assertEqual((run.result["state"], run.result["why"]), ("started", ""))
        self.assertEqual((run.result["from"], run.result["to"]), ("0.1.9", "0.2.0"))
        self.assertFalse(run.plan_left)  # taken
        self.assertIn("Done.", run.log)  # install.sh's output is in the log
        self.assertIn("natter: updates: done", run.log)
        self.assertEqual((self.stage / "natter.launcher").read_text(),
                         LAUNCHER.format(python=sys.executable).replace('NATTER_DIR = ""',
                                                                        f"NATTER_DIR = {json.dumps(str(self.source))}"))
        self.assertTrue(updater.lock_free(self.runtime / LOCK))

    def test_waits_for_natter_and_its_own_processes(self):
        # Natter's call window (python3 -m natter.callwindow) outlives it a moment: the swap waits
        # for it. Natter itself lives long enough for a slow start of the updater to see its child.
        old = self.spawn("sh", "-c", f'"{sys.executable}" -c "import time; time.sleep(3)" natter.callwindow & exec sleep 1.5')
        time.sleep(0.1)
        child = next(iter(updater.children(old.pid)))
        run = self.run_updater(self.plan(old))
        self.assertEqual(run.code, 0, run.out + run.log)
        self.assertFalse(updater.alive(child))
        self.assertIn("waiting for 1 process it started to end", run.log)
        self.assertLess(run.log.index("waiting for 1 process"), run.log.index("running"))  # before install.sh

    def test_an_app_it_opened_isnt_waited_for(self):
        old = self.spawn("sh", "-c", "sleep 8 & exec sleep 0.3")  # a browser it opened a link in
        time.sleep(0.1)
        began = time.monotonic()
        run = self.run_updater(self.plan(old))
        self.assertEqual(run.code, 0, run.out + run.log)
        self.assertLess(time.monotonic() - began, 5)

    def test_install_fails(self):
        self.environ["FAKE_FAIL"] = "0.2.0"
        run = self.run_updater(self.plan())
        self.assertEqual(run.code, 1)
        self.assertEqual(self.version(), "0.1.9")
        self.assertEqual(self.launcher_dir(), str(self.source))
        self.assertEqual(run.logged, ["install.sh 0.2.0 --force --no-deps", "install.sh 0.1.9 --force --no-deps",
                                      "started 0.1.9"])
        self.assertEqual((run.result["state"], run.result["why"], run.result["detail"]),
                         ("failed", "install", "Couldn't build the camera fix."))
        self.assertEqual(updater.version_in(self.stage / "failed"), "0.2.0")
        self.assertFalse((self.stage / "old").exists())

    def test_install_fails_and_the_launcher_is_put_back(self):
        # The new install.sh rewrote the launcher before failing: the old one's copy goes back,
        # even when the old install.sh can't run.
        self.environ["FAKE_FAIL"] = "0.2.0"
        script = (self.stage / "source/install.sh").read_text().replace(
            'if [ "$version" = "$FAKE_FAIL" ]', 'echo "#!/bin/sh" > "$HOME/.local/bin/natter"\nif [ "$version" = "$FAKE_FAIL" ]')
        (self.stage / "source/install.sh").write_text(script)
        (self.source / "install.sh").write_text("exit 9\n")
        run = self.run_updater(self.plan())
        self.assertEqual(run.result["why"], "install")
        self.assertEqual(self.launcher_dir(), str(self.source))
        self.assertEqual(run.logged[-1], "started 0.1.9")

    def test_install_hangs(self):
        self.environ["FAKE_HANG"] = "0.2.0"
        run = self.run_updater(self.plan(limits={"install": 1}))
        self.assertEqual((run.result["state"], run.result["why"]), ("failed", "timeout"))
        self.assertEqual(self.version(), "0.1.9")
        self.assertEqual(run.logged[-2:], ["install.sh 0.1.9 --force --no-deps", "started 0.1.9"])
        self.assertFalse(updater.alive(int(Path(f"{self.log}.hung").read_text())))  # it was ended

    def test_new_one_ends_before_ready(self):
        self.environ["FAKE_START"] = "die"
        run = self.run_updater(self.plan())
        self.assertEqual(run.code, 1)
        self.assertEqual((run.result["state"], run.result["why"], run.result["detail"]),
                         ("failed", "start", "exit status 3"))
        self.assertIn("the new one ended before it was ready (exit status 3)", run.log)
        self.assertEqual(self.version(), "0.1.9")
        self.assertEqual(self.launcher_dir(), str(self.source))
        self.assertEqual(run.logged, ["install.sh 0.2.0 --force --no-deps", "started 0.2.0",
                                      "install.sh 0.1.9 --force --no-deps", "started 0.1.9"])

    def test_old_natter_slow_to_end(self):
        # It took the plan, so it's quitting: past the limit, nothing changes, and once it has
        # ended it starts again as it was. Never two at once.
        old = self.natter(2.5)
        run = self.run_updater(self.plan(old, limits={"quit": 1}))
        self.assertEqual(run.code, 1)
        self.assertEqual((run.result["state"], run.result["why"]), ("failed", "busy"))
        self.assertEqual(self.version(), "0.1.9")
        self.assertEqual(updater.version_in(self.stage / "source"), "0.2.0")
        self.assertEqual(run.logged, ["started 0.1.9"])
        self.assertIn("it starts again once it has ended", run.log)
        self.assertLess(run.log.index("still running after 1 s"), run.log.index("starting"))

    def test_natter_stays_after_all(self):
        # A call came in as the updater started (update.Updates.stay): nothing changes, nothing
        # starts, and the next start has nothing to tell; before the quit limit or after it.
        for limit in (30, 0.5):
            with self.subTest(limit=limit):
                old = self.natter(30)
                cancel = self.cache / updater.CANCEL
                writer = self.spawn("sh", "-c", f"sleep 1.5 && : > '{cancel}'")
                began = time.monotonic()
                run = self.run_updater(self.plan(old, limits={"quit": limit}))
                writer.wait()
                self.assertLess(time.monotonic() - began, 10)
                self.assertEqual((run.result["state"], run.result["why"]), ("told", "busy"))
                self.assertEqual((run.logged, self.version()), ([], "0.1.9"))
                self.assertFalse(cancel.exists())
                self.assertIn("stays after all (a call came in); nothing changed", run.log)
                old.kill()

    def test_a_rollback_is_made_under_the_lock(self):
        # The new one ends before it's ready; seeing no window, the user opens Natter Den from the
        # menu meanwhile. That start waits on the lock, which the updater took back first: the old
        # install.sh --force never runs beside a Natter Den, nor is its folder swapped under one.
        # Made from the new copy, it's ended before the old copy starts. install.sh says who holds
        # the lock, from its file.
        check = (f'"{sys.executable}" -c \'import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\ntry:\n'
                 '    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); print("free")\nexcept OSError:\n'
                 '    print("held by " + " ".join(os.pread(fd, 64, 0).decode().split()[::2]))\' "$FAKE_LOCK"')
        for folder in (self.source, self.stage / "source"):
            text = (folder / "install.sh").read_text().replace(
                'echo "install.sh $version $*" >> "$FAKE_LOG"',
                f'sleep 0.5\necho "install.sh $version $* ($({check}))" >> "$FAKE_LOG"')
            (folder / "install.sh").write_text(text)
        self.environ.update(FAKE_START="die", FAKE_LOCK=str(self.runtime / LOCK))
        clicked = Path(self.tmp.name) / "clicked"
        (self.cache / "update.log").write_text("")
        menu = self.spawn(sys.executable, "-c", "import fcntl, os, sys, time\n"
                          "while 'ended before it was ready' not in open(sys.argv[1]).read(): time.sleep(0.002)\n"
                          "fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600)\n"
                          "fcntl.flock(fd, fcntl.LOCK_EX)\nopen(sys.argv[3], 'w').write('took it')\ntime.sleep(5)",
                          str(self.cache / "update.log"), str(self.runtime / LOCK), str(clicked))
        run = self.run_updater(self.plan())
        self.assertEqual((run.result["state"], run.result["why"]), ("failed", "start"))
        self.assertRegex(run.logged[0], r"^install.sh 0.2.0 --force --no-deps \(held by \d+ updating\)$")
        self.assertEqual(run.logged[1], "started 0.2.0")
        self.assertRegex(run.logged[2], r"^install.sh 0.1.9 --force --no-deps \(held by \d+ updating\)$")
        self.assertNotIn(f"held by {menu.pid}", run.logged[2])
        self.assertEqual(run.logged[3], "started 0.1.9")
        self.assertEqual(menu.wait(timeout=10), -signal.SIGTERM)  # ended while it waited
        self.assertFalse(clicked.exists())

    def test_no_rollback_once_another_has_started(self):
        # The new one handed its start over, or ended, and a Natter Den from the menu has the lock:
        # it runs the new copy, which stays; it says it's ready once it is.
        self.environ["FAKE_START"] = "die"
        taker = self.spawn(sys.executable, "-c", "import fcntl, os, sys, time\n"
                           "while 'starting' not in open(sys.argv[1]).read(): time.sleep(0.002)\n"
                           "fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600)\n"
                           "fcntl.flock(fd, fcntl.LOCK_EX)\ntime.sleep(30)",
                           str(self.cache / "update.log"), str(self.runtime / LOCK))
        (self.cache / "update.log").write_text("")
        run = self.run_updater(self.plan(limits={"start": 2}))
        taker.kill()
        self.assertEqual(run.code, 0, run.out + run.log)
        self.assertEqual((run.result["state"], self.version()), ("installed", "0.2.0"))
        self.assertEqual(run.logged, ["install.sh 0.2.0 --force --no-deps", "started 0.2.0"])

    def test_a_start_waiting_through_a_rollback_is_stopped(self):
        # Made from the new copy while install.sh ran: it mustn't take over on the old copy's files.
        self.environ["FAKE_FAIL"] = "0.2.0"
        script = (self.stage / "source/install.sh").read_text().replace(
            'if [ "$version" = "$FAKE_FAIL" ]',
            f'"{sys.executable}" -c "import os, sys, time\nfd = os.open(sys.argv[1], os.O_RDWR)\ntime.sleep(30)" '
            f'"{self.runtime / LOCK}" </dev/null >/dev/null 2>&1 &\necho $! > "$FAKE_LOG.waiter"\nsleep 1\n'
            'if [ "$version" = "$FAKE_FAIL" ]')
        (self.stage / "source/install.sh").write_text(script)
        run = self.run_updater(self.plan())
        waiter = int(Path(f"{self.log}.waiter").read_text())
        self.assertEqual(run.result["why"], "install")
        self.assertFalse(updater.alive(waiter))
        self.assertIn("asking 1 other start waiting on the lock to stop", run.log)
        self.assertLess(run.log.index("asking 1 other start"), run.log.index("starting"))

    def test_a_natter_staying_in_its_cgroup_isnt_waited_for_without_a_unit(self):
        # Started by Natter Den itself (no systemd-run), the updater shares the old one's scope,
        # which outlives it: the new Natter Den may stay there, and isn't waited for.
        self.environ["FAKE_START"] = "ready-stay"
        began = time.monotonic()
        run = self.run_updater(self.plan())
        staying = int(Path(f"{self.log}.stay").read_text())
        self.assertTrue(updater.alive(staying))
        os.kill(staying, signal.SIGKILL)
        self.assertEqual(run.code, 0, run.out + run.log)
        self.assertLess(time.monotonic() - began, 15)
        self.assertNotIn("still in the updater's cgroup", run.log)
        self.assertIn("natter: updates: done", run.log)

    def test_lock_held(self):
        fd = os.open(self.runtime / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run = self.run_updater(self.plan())
        finally:
            os.close(fd)
        self.assertEqual((run.result["state"], run.result["why"]), ("failed", "other"))
        self.assertEqual(self.version(), "0.1.9")
        self.assertEqual(run.logged, [])

    def test_launcher_changed(self):
        checkout = self.home / "src/natter"
        self.copy(checkout, "0.1.5")
        self.install_launcher(checkout)
        run = self.run_updater(self.plan())
        self.assertEqual((run.result["state"], run.result["why"]), ("failed", "launcher"))
        self.assertEqual(self.version(), "0.1.9")
        self.assertEqual(self.launcher_dir(), str(checkout))
        self.assertEqual(run.logged, ["started 0.1.5"])  # the command as it is

    def test_another_start_waiting_on_the_lock_is_stopped(self):
        # One D-Bus started meanwhile: it has the lock's file open, waiting for it.
        waiting = self.spawn(sys.executable, "-c", "import os, sys, time\n"
                             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\ntime.sleep(30)",
                             str(self.runtime / LOCK))
        time.sleep(0.3)
        run = self.run_updater(self.plan())
        self.assertEqual(run.code, 0, run.out + run.log)
        self.assertEqual(waiting.wait(timeout=5), -15)  # SIGTERM
        self.assertIn("asking 1 other start waiting on the lock to stop", run.log)

    def test_a_log_it_cant_write_leaves_the_plan(self):
        # Nothing taken, so Natter Den keeps running and says the updater didn't start.
        (self.cache / "update.log").mkdir()
        run = self.run_updater(self.plan())
        self.assertEqual(run.code, 1)
        self.assertTrue(run.plan_left)
        self.assertIn("no log", run.out)
        self.assertIsNone(run.result)
        self.assertEqual((run.logged, self.version()), ([], "0.1.9"))

    def test_bad_plans_change_nothing(self):
        outside = Path(self.tmp.name).resolve() / "elsewhere"
        self.copy(outside, "0.1.9")
        for name, changes in [
            ("source outside the home", {"source": str(outside)}),
            ("stage elsewhere", {"stage": str(outside)}),
            ("another lock", {"lock": str(self.runtime / "other.lock")}),
            ("another launcher", {"launcher": str(self.home / "bin/natter")}),
            ("a tag that isn't one", {"tag": "v0.2.0-rc1"}),
            ("to isn't the tag", {"to": "0.3.0"}),
            ("odd arguments", {"args": ["--url", "https://example.com"]}),
            ("a result elsewhere", {"result": str(self.home / "result.json")}),
        ]:
            with self.subTest(name):
                run = self.run_updater(self.plan(**changes))
                self.assertEqual(run.code, 1)
                self.assertIn("not updating", run.out)
                self.assertTrue(run.plan_left)  # Natter Den sees it untaken, and keeps running
                self.assertIsNone(run.result)
                self.assertEqual(self.version(), "0.1.9")
                self.assertEqual(updater.version_in(self.stage / "source"), "0.2.0")
                self.assertEqual(run.logged, [])
                self.assertEqual(run.log, "")

    def test_no_plan(self):
        run = self.run_updater({}, write=False)
        self.assertEqual(run.code, 1)
        self.assertIn("no plan to follow", run.out)

    def test_plan_problem(self):
        plan = self.plan()
        path = str(self.cache / "plan.json")
        self.assertEqual(updater.plan_problem(plan, path, str(self.home)), "")
        for key, value in (("pid", "1"), ("pid", 1), ("ticks", None), ("env", {"A": 1}), ("limits", {"quit": "x"}),
                           ("limits", {"forever": 1}), ("tag", None), ("log", str(self.home / "update.log"))):
            with self.subTest(key=key, value=value):
                self.assertNotEqual(updater.plan_problem(dict(plan, **{key: value}), path, str(self.home)), "")
        self.assertNotEqual(updater.plan_problem(plan, path, str(self.home / "other")), "")
        self.assertNotEqual(updater.plan_problem([], path, str(self.home)), "")


class StepsTest(unittest.TestCase):
    """The updater's steps on their own: what counts as Natter Den's, the swap, its service."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            process.kill()
            process.wait()
            process.stdout.close()
        self.tmp.cleanup()

    def named(self, name, *args):
        """A process named `name` (as its comm), with `args` after its own command."""
        process = subprocess.Popen([sys.executable, "-c", "import ctypes, sys, time\n"
                                    "ctypes.CDLL(None).prctl(15, sys.argv[1].encode(), 0, 0, 0)\n"
                                    "print(flush=True)\ntime.sleep(30)", name, *args],
                                   stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self.processes.append(process)
        process.stdout.readline()
        return process.pid

    def test_whats_natter_dens_own(self):
        # WebKit's sandbox runs its process or the bus proxy in bwrap; a Flatpak app Natter Den
        # opened a link in runs in bwrap too, and may run for hours.
        cases = [(("bwrap", "--args", "29", "--", "/usr/lib/x86_64-linux-gnu/webkitgtk-6.0/WebKitWebProcess", "4"), True),
                 (("bwrap", "--args", "73", "--", "xdg-dbus-proxy", "--args=75"), True),
                 (("bwrap", "--args", "41", "--", "firefox", "https://example.com"), False),
                 (("bwrap", "--args", "41", "launch-script.sh"), False),
                 (("WebKitNetworkPr", "7"), True), (("xdg-dbus-proxy",), True), (("firefox",), False)]
        for words, own in cases:
            with self.subTest(words=words):
                self.assertEqual(updater.own_process(self.named(*words)), own)

    def test_exchange(self):
        first, second = self.dir / "a", self.dir / "b"
        for folder in (first, second):
            folder.mkdir()
            (folder / "x").write_text(folder.name)
        updater.exchange(first, second)
        self.assertEqual(((first / "x").read_text(), (second / "x").read_text()), ("b", "a"))
        self.assertEqual(sorted(path.name for path in self.dir.iterdir()), ["a", "b"])

    def test_exchange_by_renames_where_it_cant_in_one(self):
        # No renameat2 (an old C library), or a filesystem without RENAME_EXCHANGE: three renames,
        # with the signals that would end the updater held meanwhile, and nothing lost on a failure.
        first, second = self.dir / "a", self.dir / "b"
        for folder in (first, second):
            folder.mkdir()
            (folder / "x").write_text(folder.name)
        held, real = [], os.rename

        def rename(source, target):
            held.append(signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []))
            real(source, target)

        with mock.patch.object(updater.ctypes, "CDLL", side_effect=OSError("no C library")), \
                mock.patch.object(updater.os, "rename", rename):
            updater.exchange(first, second)
        self.assertEqual(((first / "x").read_text(), (second / "x").read_text()), ("b", "a"))
        self.assertEqual(held, [True, True, True])
        self.assertNotIn(signal.SIGTERM, signal.pthread_sigmask(signal.SIG_BLOCK, []))
        failing = iter([None, OSError(13, "Permission denied")])

        def once(source, target):
            error = next(failing, None)
            if error:
                raise error
            real(source, target)

        with mock.patch.object(updater.ctypes, "CDLL", side_effect=OSError("no C library")), \
                mock.patch.object(updater.os, "rename", once), self.assertRaises(OSError):
            updater.exchange(first, second)
        self.assertEqual(((first / "x").read_text(), (second / "x").read_text()), ("b", "a"))  # as it was

    def test_its_own_unit(self):
        for found, unit in (("/user.slice/user-1000.slice/user@1000.service/app.slice/natter-update-42-1800000000.service",
                             "natter-update-42-1800000000.service"),
                            ("/user.slice/user-1000.slice/user@1000.service/app.slice/"
                             "app-gnome-io.github.Officialbabs.Natter-4242.scope", ""), (None, "")):
            with self.subTest(found=found), mock.patch.object(updater, "cgroup", return_value=found):
                self.assertEqual(updater.own_unit(), unit)

    def test_the_lock_says_its_the_updater(self):
        # instance.taking waits for it as long as an install may take (test_instance).
        from natter import instance

        self.assertEqual(updater.UPDATING, instance.UPDATING)
        self.assertGreater(instance.UPDATE_WAIT, updater.LIMITS["install"] + updater.LIMITS["others"])


if __name__ == "__main__":
    unittest.main()
