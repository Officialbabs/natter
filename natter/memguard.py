"""Natter's memory guard: it ends a WhatsApp page that grows without end, whether the window is
shown, hidden or stalled, and says what was growing.

On 20 Oct 2026 a video attach took a page from 1 GB to 12 GB at 130-160 MB/s and nothing stopped
it. WebKit's own limit (app.py, MemoryPressureSettings) counts resident memory only, swap left
out, on a timer in the page's own main thread, which the busy page never let run. Natter's own
checks (app.py, _check_memory) wait for the window to hide. The unit's MemoryHigh never kills: it
pushed the page into swap and stalled Natter's window along with it.

This guard looks from Natter's side, in a thread of its own, so a stalled main loop doesn't stall
it: every GUARD_POLL seconds, each of Natter's WebKit web processes, by the private memory it
holds, resident or swapped (RssAnon + VmSwap). Past its limit and growing like a runaway, or far
past it whatever it does, the guard ends that process with SIGKILL, through a pidfd and after
checking the name again, so a pid the kernel gave to another process meanwhile is never hit.
WebKit reports it as a crash to the view that process served, and the view asks recent_kill()
whether it was this guard.

It logs from its own thread too, so the journal has it while the main loop is stalled. Sizes
only: which of the page's threads are busy and what shape its biggest regions have tell what
was growing (a video's decoded frames, or JavaScript's own heap), never what was in it.
"""

from __future__ import annotations

import collections
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

GiB = 1024 ** 3
MiB = 1024 ** 2
PROC = Path("/proc")
GUARD_POLL = 2.0  # seconds between looks
# A page's limit is this share of the computer's memory, within these bounds, and never more than
# half of it. An idle WhatsApp page holds 1.0-1.4 GB; a 16 GB laptop (14.8 GiB to the kernel)
# gives 3.7 GiB.
PAGE_SHARE, PAGE_FLOOR, PAGE_CEILING = 0.25, int(2.5 * GiB), 6 * GiB
# Past its limit a page is ended once it grows like a runaway: faster than RUNAWAY_RATE at two
# looks in a row (a video's frames decoded and kept grew 130-1000 MB/s; an idle page creeps about
# 140 MB an hour). One that holds still there is left alone for up to CALL_ROOM more: WhatsApp's
# call engine only grows, well over a gigabyte in a long call, and the page keeps that after the
# call until Natter gives it a fresh page once the window has been hidden a while (app.py). On an
# 8 GB computer that's past the limit, a minute after the call, with the window still in use.
# Past that room it's ended whatever it does.
RUNAWAY_RATE = 64 * MiB  # bytes a second
# During a call both move up by CALL_ROOM, as ending the page ends the call; past that, a page
# growing like a runaway is ended all the same, and at CALL_MARGIN more whatever it does, before
# the unit's backstop (below) does it. The call's room lasts CALL_GRACE after the call, so a call
# whose connection drops and comes back isn't ended in the gap. A call in one account's page
# counts for every page: WebKit doesn't say which web process is whose.
CALL_ROOM = int(1.5 * GiB)
CALL_MARGIN = GiB // 2
CALL_GRACE = 60.0
# Natter's unit (system.py) is the kernel's backstop, for when the guard couldn't act: room for
# one page at its limit, the call engine of every account's page, and Natter itself; never more
# than this share of the computer's memory. Its swap is capped too, at one page's limit
# (scope_limits): what's swapped is still held, and a runaway parked in swap filled the
# computer's once. A fixed 1.5 GiB sat full all morning on 24 Oct 2026 with the page idle (half
# a gigabyte of its heap and the GPU's buffers in swap), and the guard, counting RssAnon and
# VmSwap, still ends a page that parks its growth there. Never more than UNIT_SWAP_SHARE of the
# computer's swap, so a small swap file isn't Natter's alone.
ACCOUNT_ROOM, NATTER_ROOM, UNIT_SHARE = int(1.5 * GiB), GiB // 2, 0.6
UNIT_SWAP_SHARE = 0.5
# Growth diagnostics. A page growing faster than GROWTH_RATE at two looks in a row gets its
# busiest threads logged (once a spell); at every STEP it passes above QUIET, its largest
# regions. An idle page passes 512 and 1024 MB at every start: that isn't news.
GROWTH_RATE = 50 * MiB  # bytes a second
STEP = 512 * MiB
QUIET = 1536 * MiB
# What the regions tell. JavaScriptCore reserves its heap's address space up front, 64 GiB in
# WebKitGTK 2.52, and uses it from the inside: memory in a run of 16 GiB or more is JavaScript's
# own (ArrayBuffers, WebAssembly). A video's frames, decoded by GStreamer, lie outside it. Each
# 1366x766 frame from its H.264 decoder is one buffer of exactly 1.5 MiB, so regions of kept
# frames are often a whole number of them; not always (in a lab, 202 MB of frames were, while
# 652 MB more lay in a 4.7 GB region that wasn't).
FRAME = 1536 * 1024
RESERVATION = 16 * GiB
# A crash reported this long after a kill is still that kill: the main loop that reports it may
# have been stalled by the very shortage the guard ended.
KILL_MEMORY = 120.0
# The kernel, short of memory, takes a page before Natter's window or other apps. Only where that
# ends the page alone (system.pages_end_alone: Natter's own unit, OOMPolicy=continue): under a
# launcher's unit (OOMPolicy=stop) the kernel's ending a page stops all of Natter.
OOM_SCORE_ADJ = 500
WEB_PROCESS = "WebKitWebProces"  # /proc/<pid>/comm is cut to 15 characters


def _read(path) -> str:
    try:
        return Path(path).read_text()
    except (OSError, ValueError):
        return ""


def mem_total(meminfo: Optional[str] = None) -> int:
    """The computer's memory in bytes, MemTotal (0 when it can't be read)."""
    for line in (_read("/proc/meminfo") if meminfo is None else meminfo).splitlines():
        if line.startswith("MemTotal:") and line.split()[1:2] and line.split()[1].isdigit():
            return int(line.split()[1]) * 1024
    return 0


def page_limit(total: int) -> int:
    """Bytes one page may hold (resident or swapped) before the guard ends it, for a computer
    with `total` bytes of memory (0: unknown)."""
    if total <= 0:
        return PAGE_FLOOR
    limit = min(max(int(total * PAGE_SHARE), PAGE_FLOOR), PAGE_CEILING)
    return min(limit, total // 2)  # 4 GB of memory: 2 GB


def swap_total(meminfo: Optional[str] = None) -> int:
    """The computer's swap in bytes, SwapTotal (0 when there's none or it can't be read)."""
    for line in (_read("/proc/meminfo") if meminfo is None else meminfo).splitlines():
        if line.startswith("SwapTotal:") and line.split()[1:2] and line.split()[1].isdigit():
            return int(line.split()[1]) * 1024
    return 0


def scope_limits(total: int, accounts: int, swap: int = 0) -> tuple[int, int]:
    """(MemoryMax, MemorySwapMax) for Natter's unit with `accounts` pages, on a computer with
    `total` bytes of memory and `swap` bytes of swap (0: unknown). The swap cap is one page's
    limit (page_limit), with its bounds, and at most UNIT_SWAP_SHARE of the swap when that's
    known."""
    memory_max = page_limit(total) + ACCOUNT_ROOM * max(1, accounts) + NATTER_ROOM
    if total > 0:
        memory_max = min(memory_max, int(total * UNIT_SHARE))
    swap_max = page_limit(total)
    if swap > 0:
        swap_max = min(swap_max, int(swap * UNIT_SWAP_SHARE))
    return memory_max, swap_max


def page_ceiling(total: int) -> int:
    """The most a page may hold (resident or swapped), whatever it does and call or not, on a
    computer with `total` bytes of memory: what Natter's unit with one account leaves a page
    beside Natter, swap and all, less CALL_MARGIN for the growth between two looks. The guard
    acts first: past the unit's limit, which only a scope of Natter's own has (system.py), the
    kernel would end the page itself. Under a launcher's unit, which gets no limit, it's simply
    the most the guard lets a page hold. It only comes below the call's room on small
    computers, where the unit is held to UNIT_SHARE. The computer's own swap is left out: where
    it's small the unit's swap cap is smaller (scope_limits), and the kernel may end a page
    there before the guard would; that ends the page only, as the unit keeps Natter running."""
    memory_max, swap_max = scope_limits(total, 1)
    return memory_max + swap_max - NATTER_ROOM - CALL_MARGIN


def page_limits(limit: int, ceiling: Optional[int], in_call: bool) -> tuple[int, int]:
    """(soft, hard) for a page whose limit is `limit` (page_limit): past soft the guard ends it
    when it grows like a runaway, past hard whatever it does. A call moves both up (CALL_ROOM),
    and `ceiling` (page_ceiling), when given, caps them."""
    soft = limit + (CALL_ROOM if in_call else 0)
    hard = soft + (CALL_MARGIN if in_call else CALL_ROOM)
    if ceiling is not None:
        soft, hard = min(soft, ceiling), min(hard, ceiling)
    return soft, hard


def in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def web_processes(pid: Optional[int] = None, proc: Path = PROC, depth: int = 4,
                  flatpak: bool = False) -> set[int]:
    """The WebKit web processes Natter (`pid`) started: its children, or their children down to
    `depth`, named WebKitWebProcess. With WebKit's sandbox a page is three down: Natter, bwrap,
    bwrap, the page.

    In Flatpak, WebKit starts them through the portal (flatpak-spawn), so they aren't Natter's
    children; Flatpak gives the app a process namespace of its own, so every web process in its
    /proc is its own. (Not tried in a Flatpak build yet.)"""
    if flatpak:
        try:
            entries = os.listdir(proc)
        except OSError:
            return set()
        return {int(entry) for entry in entries
                if entry.isdigit() and _read(proc / entry / "comm").startswith(WEB_PROCESS)}
    pid = os.getpid() if pid is None else pid
    found, level = set(), {pid}
    for _ in range(depth):
        below = set()
        for parent in level:
            try:
                tasks = os.listdir(proc / str(parent) / "task")
            except OSError:
                continue
            for task in tasks:
                below.update(int(child) for child in _read(proc / str(parent) / "task" / task / "children").split()
                             if child.isdigit())
        found.update(child for child in below if _read(proc / str(child) / "comm").startswith(WEB_PROCESS))
        level = below - found
    return found


def page_sizes(pid: Optional[int] = None, proc: Path = PROC, flatpak: Optional[bool] = None) -> dict[int, int]:
    """What each of Natter's pages holds (held), by pid: the guard's measure, the page alone."""
    found = web_processes(pid, proc, flatpak=in_flatpak() if flatpak is None else flatpak)
    sizes = {page: held(page, proc) for page in found}
    return {page: size for page, size in sizes.items() if size is not None}


def held(pid: int, proc: Path = PROC) -> Optional[int]:
    """Bytes of private memory the process holds, resident or swapped (RssAnon + VmSwap); None
    when it's gone."""
    values = {}
    for line in _read(proc / str(pid) / "status").splitlines():
        key, _, value = line.partition(":")
        if key in ("RssAnon", "VmSwap") and value.split() and value.split()[0].isdigit():
            values[key] = int(value.split()[0]) * 1024
    return sum(values.values()) if "RssAnon" in values else None


_GEM_UNITS = {"": 1, "kib": 1024, "mib": MiB, "gib": GiB}


def gem_held(pid: int, proc: Path = PROC) -> int:
    """Bytes of GPU buffers the process owns in system memory, by its DRM files' fdinfo
    (drm-total-system…: the i915 driver backs them with shmem). None of it is in the process's
    RssAnon or VmSwap, or in any limit of Natter's or WebKit's: the page's held 542 MB of them on
    24 Oct 2026, seen only as its unit's shmem. Each DRM client once, however many descriptors
    it has (drm-client-id); 0 when there's nothing to read."""
    try:
        entries = os.listdir(proc / str(pid) / "fdinfo")
    except OSError:
        return 0
    clients: dict[str, int] = {}
    for entry in entries:
        client, total = None, None
        for line in _read(proc / str(pid) / "fdinfo" / entry).splitlines():
            key, _, value = line.partition(":")
            parts = value.split()
            if key == "drm-client-id" and parts:
                client = parts[0]
            elif key.startswith("drm-total-system") and parts and parts[0].isdigit():
                unit = parts[1].lower() if len(parts) > 1 else ""
                total = (total or 0) + int(parts[0]) * _GEM_UNITS.get(unit, 1)
        if total is not None:
            clients[client or entry] = total
    return sum(clients.values())


def descends(pid: int, ancestor: int, proc: Path = PROC, depth: int = 4) -> bool:
    """Whether `ancestor` is the parent of `pid`, or its parent's, up to `depth` steps."""
    for _ in range(depth):
        parent = next((line.split()[1] for line in _read(proc / str(pid) / "status").splitlines()
                       if line.startswith("PPid:") and len(line.split()) > 1), "")
        if not parent.isdigit() or int(parent) <= 1:
            return False
        if int(parent) == ancestor:
            return True
        pid = int(parent)
    return False


def kill(pid: int, proc: Path = PROC, ancestor: Optional[int] = None) -> bool:
    """SIGKILL `pid` if it's still a WebKit web process (and below `ancestor`, when given).

    The pidfd is opened first and the process looked at after: if the one it holds is still
    alive when the signal goes, /proc showed that one; if it died and the pid went to another
    process meanwhile, the signal goes nowhere. True when it was sent."""
    try:
        fd = os.pidfd_open(pid)
    except (OSError, AttributeError):
        return False
    try:
        if not _read(proc / str(pid) / "comm").startswith(WEB_PROCESS):
            return False
        if ancestor is not None and not descends(pid, ancestor, proc):
            return False
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


# ---- Diagnostics ---------------------------------------------------------------------------

def thread_times(pid: int, proc: Path = PROC) -> dict[int, tuple[str, int]]:
    """CPU each of the process's threads has used (user and system, in clock ticks), with its
    name, by thread id."""
    times = {}
    try:
        tasks = os.listdir(proc / str(pid) / "task")
    except OSError:
        return times
    for task in tasks:
        stat = _read(proc / str(pid) / "task" / task / "stat")
        # "tid (name) state ppid …": the name may hold spaces and brackets of its own.
        start, end = stat.find("("), stat.rfind(")")
        fields = stat[end + 1:].split()
        if task.isdigit() and 0 <= start < end and len(fields) > 12 \
                and fields[11].isdigit() and fields[12].isdigit():
            times[int(task)] = (stat[start + 1:end], int(fields[11]) + int(fields[12]))
    return times


def busiest(before: dict, after: dict, seconds: float, top: int = 3) -> list[tuple[str, int]]:
    """The threads that used the most CPU between two thread_times(): (name, % of one core)."""
    ticks = os.sysconf("SC_CLK_TCK")
    used = sorted(((total - before[tid][1] if tid in before else total, name)
                   for tid, (name, total) in after.items()), key=lambda pair: -pair[0])
    return [("".join(c if c.isprintable() else "?" for c in name), round(100 * delta / ticks / max(seconds, 0.001)))
            for delta, name in used[:top] if delta > 0]


_REGION = re.compile(r"([0-9a-f]+)-([0-9a-f]+) \S+ \S+ \S+ \S+ *(.*)")


def anon_regions(smaps: str, top: int = 3) -> list[dict]:
    """The largest private anonymous regions in a /proc/<pid>/smaps, by what they hold, resident
    or swapped: {"held": bytes, "size": bytes of address space, "frames": whether the size is a
    whole number of FRAMEs, "reservation": the bytes of the run of address space it lies in when
    that's RESERVATION or more, else 0}. A run is anonymous regions that follow on without a gap:
    JavaScriptCore's reservation is one region, or several once parts of it change protection."""
    regions, current = [], None
    for line in smaps.splitlines():
        if line[:1] and line[0] in "0123456789abcdef":  # a region's first line; its fields start in capitals
            found = _REGION.match(line)
            if found:
                name = found[3].strip()
                current = {"start": int(found[1], 16), "end": int(found[2], 16), "held": 0,
                           "anon": not name or name == "[heap]" or name.startswith("[anon:")}
                regions.append(current)
                continue
        if current is not None and line.startswith(("Anonymous:", "Swap:")) and line.split()[1:2]:
            current["held"] += int(line.split()[1]) * 1024
    # Runs of anonymous regions with no gap between them.
    run, runs = [], []
    for region in regions:
        if region["anon"] and run and region["start"] == run[-1]["end"]:
            run.append(region)
        else:
            if run:
                runs.append(run)
            run = [region] if region["anon"] else []
    if run:
        runs.append(run)
    for run in runs:
        span = run[-1]["end"] - run[0]["start"]
        for region in run:
            region["reservation"] = span if span >= RESERVATION else 0
    anon = sorted((region for region in regions if region["anon"] and region["held"] >= MiB), key=lambda r: -r["held"])
    return [{"held": region["held"], "size": region["end"] - region["start"],
             "frames": (region["end"] - region["start"]) % FRAME == 0, "reservation": region["reservation"]}
            for region in anon[:top]]


def describe_region(region: dict) -> str:
    shape = "1.5 MiB multiple" if region["frames"] else "not a 1.5 MiB multiple"
    where = f"in a {region['reservation'] / GiB:.0f} GiB reservation" if region["reservation"] else "no reservation"
    return f"{region['held'] >> 20} MB in {region['size'] >> 20} MB ({shape}, {where})"


# ---- The guard -----------------------------------------------------------------------------

class _Page:
    """What the guard keeps of one web process between looks."""

    def __init__(self, size: int) -> None:
        self.looks: collections.deque = collections.deque(maxlen=3)  # (time, bytes): two rates
        self.step = size // STEP  # the highest step logged (or passed before it was first seen)
        self.fast = 0  # looks in a row growing faster than GROWTH_RATE
        self.threads: Optional[tuple[float, dict]] = None  # thread_times at the first fast look
        self.told = False  # its busiest threads were logged for this spell of growth

    def rates(self) -> list[float]:
        """Bytes a second between each look and the one before, oldest first."""
        looks = list(self.looks)
        return [(size - before) / (now - then) if now > then else 0.0
                for (then, before), (now, size) in zip(looks, looks[1:])]


class MemoryGuard:
    """Looks at each of Natter's pages every `poll` seconds from a thread of its own, and ends
    one past `limit_bytes` (page_limit) that grows like a runaway, or one past the room above it
    whatever it does (limits). `ceiling` (page_ceiling), when given, is the most any page may
    hold. `calling()`, when given, says whether a call is on in any page (it's asked from the
    guard's thread). `on_kill(pid, held)` runs on the main loop after a kill: the kill itself
    and its log line don't wait for it. Each page's oom_score_adj is set to `oom_score_adj`,
    unless that's None (see OOM_SCORE_ADJ). `charge()`, when given, says what the kernel charges
    Natter's unit beside its pages' private memory ({"shmem": bytes, "zswap": bytes},
    system.unit_charge), for the step line only: none of it counts towards ending a page."""

    def __init__(self, limit_bytes: int, on_kill: Optional[Callable[[int, int], None]] = None,
                 poll: float = GUARD_POLL, calling: Optional[Callable[[], bool]] = None, *,
                 ceiling: Optional[int] = None, pid: Optional[int] = None, proc: Path = PROC,
                 flatpak: Optional[bool] = None, killer: Optional[Callable[[int], bool]] = None,
                 idle_add: Optional[Callable] = None, oom_score_adj: Optional[int] = OOM_SCORE_ADJ,
                 charge: Optional[Callable[[], dict[str, int]]] = None) -> None:
        self.limit = limit_bytes
        self.oom_score_adj = oom_score_adj
        self.ceiling = ceiling
        self.on_kill = on_kill
        self.poll = poll
        self.calling = calling
        self.charge = charge
        self.pid = os.getpid() if pid is None else pid
        self.proc = proc
        self.flatpak = in_flatpak() if flatpak is None else flatpak
        # Outside Flatpak only Natter's own descendants: other WebKit apps' pages have the same name.
        self.killer = killer or (lambda target: kill(target, proc, None if self.flatpak else self.pid))
        if idle_add is None:
            from gi.repository import GLib

            idle_add = GLib.idle_add
        self._idle_add = idle_add
        self._pages: dict[int, _Page] = {}
        self._ended: set[int] = set()  # pids sent SIGKILL that haven't gone yet (a big one takes a while)
        self._call_seen: Optional[float] = None  # when calling() last said yes
        self._kills: list[tuple[float, int]] = []  # (time, pid), not yet claimed
        self._sizes: Optional[tuple[float, dict[int, int]]] = None  # (when, what each page held) at the last look
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failed = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="natter-memguard", daemon=True)
        self._thread.start()
        if os.environ.get("NATTER_DEBUG"):
            (soft, hard), (call_soft, call_hard) = self.limits(False), self.limits(True)
            print(f"natter: memory: a page may hold {soft >> 20} MB, {hard >> 20} MB holding still; in a call "
                  f"{call_soft >> 20} MB, {call_hard >> 20} MB holding still", flush=True)

    def stop(self, wait: float = 0.25) -> None:
        """Stop looking: back once the thread has stopped (it may be partway through a look, and
        ending a page), or after `wait` seconds."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and wait > 0:
            thread.join(wait)

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll):
            try:
                self.check()
            except Exception as error:  # the guard must outlive one bad look
                if not self._failed:
                    self._failed = True
                    print(f"natter: memory: a look at the pages failed ({type(error).__name__})", flush=True)

    def limits(self, in_call: bool) -> tuple[int, int]:
        """(soft, hard) for a page now: past soft it's ended when it grows like a runaway, past
        hard whatever it does."""
        return page_limits(self.limit, self.ceiling, in_call)

    def _in_call(self, now: float) -> bool:
        if self.calling is not None:
            try:
                if self.calling():
                    self._call_seen = now
            except Exception:
                pass  # as it was at the last look
        return self._call_seen is not None and now - self._call_seen <= CALL_GRACE

    def check(self, now: Optional[float] = None) -> list[int]:
        """One look at every page. Returns the pids it ended."""
        now = time.monotonic() if now is None else now
        in_call = self._in_call(now)
        pids = web_processes(self.pid, self.proc, flatpak=self.flatpak)
        for gone in set(self._pages) - pids:
            del self._pages[gone]
        self._ended &= pids
        ended, sizes = [], {}
        for pid in sorted(pids - self._ended):
            size = held(pid, self.proc)
            if size is None:
                continue  # gone since the list was made
            sizes[pid] = size
            page = self._pages.get(pid)
            if page is None:
                page = self._pages[pid] = _Page(size)
                self._raise_oom_score(pid)
            page.looks.append((now, size))
            rates = page.rates()
            rate = round(rates[-1] / MiB) if rates else 0
            soft, hard = self.limits(in_call)
            runaway = len(rates) >= 2 and min(rates[-2:]) > RUNAWAY_RATE
            if (size > hard or (size > soft and runaway)) and self._end(pid, now):
                del self._pages[pid]
                self._ended.add(pid)
                ended.append(pid)
                print(f"natter: memory: page {pid} held {size >> 20} MB, past its limit of "
                      f"{(hard if size > hard else soft) >> 20} MB{' in a call' if in_call else ''}, "
                      f"growing {rate} MB/s; ended it", flush=True)
                if self.on_kill is not None:
                    self._idle_add(self._report, pid, size)
                continue
            try:
                self._diagnose(pid, page, size, rates, rate, now, in_call)
            except Exception:  # what it says about one page mustn't keep it from the others
                pass
        with self._lock:
            self._sizes = (now, {pid: size for pid, size in sizes.items() if pid not in ended})
        return ended

    def recent_sizes(self, now: Optional[float] = None) -> Optional[dict[int, int]]:
        """What each page held at the guard's last look (held, by pid), if that was within three
        polls; else None. Natter's main loop asks this rather than reading every page's status
        itself: during a call it has the call's packets to pass on (rtc.py)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self._sizes
        if last is None or now - last[0] > 3 * self.poll:
            return None
        return dict(last[1])

    def _end(self, pid: int, now: float) -> bool:
        # Noted before the signal goes, so WebKit's report of the crash can't come before it.
        entry = (now, pid)
        with self._lock:
            self._kills = self._kills[-15:] + [entry]
        if self.killer(pid):
            return True
        with self._lock:
            if entry in self._kills:
                self._kills.remove(entry)
        return False

    def _report(self, pid: int, size: int) -> bool:
        self.on_kill(pid, size)
        return False

    def _diagnose(self, pid: int, page: _Page, size: int, rates: list[float], rate: int, now: float,
                  in_call: bool = False) -> None:
        # Which threads are busy, once a spell of growth: their CPU between two fast looks.
        if rates and rates[-1] > GROWTH_RATE:
            page.fast += 1
        else:
            page.fast, page.threads, page.told = 0, None, False
        if page.fast and not page.told and size >= QUIET:
            times = thread_times(pid, self.proc)
            if page.threads is not None:
                then, before = page.threads
                top = busiest(before, times, now - then)
                page.told, page.threads = True, None
                print(f"natter: memory: page {pid} growing {rate} MB/s; busiest threads: "
                      + (", ".join(f"{name} {share}%" for name, share in top) or "none"), flush=True)
            else:
                page.threads = (now, times)
        # The largest regions at every step: decoded frames or JavaScript's heap. Not in a call:
        # reading smaps walks the whole page under its memory map's lock, which the call's sound
        # thread may want. The step is told at the first look after the call.
        if size >= QUIET and size // STEP > page.step and not in_call:
            page.step = size // STEP
            regions = anon_regions(_read(self.proc / str(pid) / "smaps"))
            print(f"natter: memory: page {pid} holds {size >> 20} MB, growing {rate} MB/s; largest regions: "
                  + ("; ".join(describe_region(region) for region in regions) or "unreadable")
                  + self._beside(pid), flush=True)

    def _beside(self, pid: int) -> str:
        """What the page holds that its size (RssAnon + VmSwap) doesn't count, for the step line:
        its GPU buffers (gem_held), and what the kernel charges the unit as shmem and zswap
        (`charge`). Said only where there's something to say."""
        parts = []
        gem = gem_held(pid, self.proc)
        if gem:
            parts.append(f"GPU buffers {gem >> 20} MB")
        if self.charge is not None:
            try:
                charged = self.charge()
            except Exception:
                charged = {}
            if any(charged.values()):
                parts.append("the unit's " + ", ".join(f"{name} {bytes_ >> 20} MB" for name, bytes_ in charged.items()))
        return f"; besides, {', '.join(parts)}" if parts else ""

    def _raise_oom_score(self, pid: int) -> None:
        if self.oom_score_adj is None:
            return
        try:
            with open(self.proc / str(pid) / "oom_score_adj", "w") as handle:
                handle.write(str(self.oom_score_adj))
        except OSError:
            pass

    def recent_kill(self, pid: Optional[int] = None, now: Optional[float] = None) -> bool:
        """Whether the guard ended `pid` (any page, when None) in the last KILL_MEMORY seconds.
        Each kill answers yes once: WebKit reports it to the one view that process served."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._kills = [(when, killed) for when, killed in self._kills if now - when <= KILL_MEMORY]
            for index, (_when, killed) in enumerate(self._kills):
                if pid is None or killed == pid:
                    del self._kills[index]
                    return True
        return False
