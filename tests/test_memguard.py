"""The memory guard (memguard.py): its limits, its looks at a fake /proc, its kills of real
processes (children of the test, never anything else), and what it logs while a page grows."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from natter import memguard
from natter.memguard import CALL_GRACE, CALL_ROOM, GiB, KILL_MEMORY, MiB

UI = 1000
TICKS = os.sysconf("SC_CLK_TCK")


class FakeProc:
    """/proc with Natter (UI), WebKit's network process, and sandboxed web processes:
    UI → bwrap → bwrap → WebKitWebProcess, as WebKit's bubblewrap launcher makes them."""

    def __init__(self, root: Path):
        self.root = root
        self.add(UI, "python3", [], parent=1)

    def add(self, pid, comm, children, anon_mb=0, swap_mb=0, parent=UI):
        folder = self.root / str(pid)
        (folder / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        (folder / "task" / str(pid) / "children").write_text(" ".join(map(str, children)) + (" " if children else ""))
        (folder / "comm").write_text(comm + "\n")
        (folder / "oom_score_adj").write_text("0\n")
        self.set(pid, anon_mb, swap_mb, comm=comm, parent=parent)

    def page(self, bwrap, inner, pid, anon_mb, swap_mb=0):
        self.add(bwrap, "bwrap", [inner])
        self.add(inner, "bwrap", [pid], parent=bwrap)
        self.add(pid, "WebKitWebProces", [], anon_mb, swap_mb, parent=inner)

    def children(self, pids):
        (self.root / str(UI) / "task" / str(UI) / "children").write_text(" ".join(map(str, pids)) + " ")

    def set(self, pid, anon_mb, swap_mb=0, comm="WebKitWebProces", parent=None):
        status = self.root / str(pid) / "status"
        if parent is None:
            parent = next(line.split()[1] for line in status.read_text().splitlines() if line.startswith("PPid:"))
        status.write_text(f"Name:\t{comm}\nPPid:\t{parent}\nRssAnon:\t{anon_mb * 1024} kB\n"
                          f"RssFile:\t1000 kB\nVmSwap:\t{swap_mb * 1024} kB\n")

    def threads(self, pid, ticks):
        """Threads of `pid`: {tid: (name, CPU ticks)}."""
        for tid, (name, used) in ticks.items():
            folder = self.root / str(pid) / "task" / str(tid)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "stat").write_text(f"{tid} ({name}) S 1 1 1 0 -1 4194560 0 0 0 0 {used} 0 0 0 20 0 1 0\n")


def region(start, size, perms="rw-p", path="", anon=0, swap=0):
    """One region of a /proc/<pid>/smaps (sizes in bytes)."""
    head = f"{start:x}-{start + size:x} {perms} 00000000 00:00 0"
    return (f"{head}                          {path}\n" if path else head + "\n") + (
        f"Size:           {size >> 10} kB\nRss:            {anon >> 10} kB\nAnonymous:      {anon >> 10} kB\n"
        f"AnonHugePages:         0 kB\nSwap:           {swap >> 10} kB\nSwapPss:        {swap >> 10} kB\n"
        "VmFlags: rd wr mr mw me ac\n")


FRAMES = 469 * memguard.FRAME  # 703.5 MiB of decoded frames, as GStreamer's H.264 decoder keeps them
SMAPS = "".join([
    region(0x55d000000000, 20 * MiB, path="[heap]", anon=20 * MiB),
    region(0x7a0000000000, 8 * MiB, perms="r-xp", path="/usr/lib/libjavascriptcoregtk-6.0.so.1", anon=4 * MiB),
    # JavaScriptCore's reservation: 64 GiB of address space, opened up in the middle.
    region(0x7b0000000000, 4 * GiB, perms="---p"),
    region(0x7b0100000000, 512 * MiB, anon=400 * MiB, swap=50 * MiB),
    region(0x7b0120000000, 64 * GiB - 4 * GiB - 512 * MiB, perms="---p"),
    region(0x7c0000000000, FRAMES, anon=FRAMES),
    region(0x7d0000000000, 100 * MiB, anon=100 * MiB),
    region(0x7ffc00000000, 8 * MiB, path="[stack]", anon=8 * MiB),
])


class LimitsTest(unittest.TestCase):
    def test_page_limit(self):
        self.assertEqual(memguard.page_limit(4 * GiB), 2 * GiB)  # never more than half
        self.assertEqual(memguard.page_limit(8 * GiB), int(2.5 * GiB))
        self.assertEqual(memguard.page_limit(16 * GiB), 4 * GiB)
        self.assertEqual(memguard.page_limit(64 * GiB), 6 * GiB)
        self.assertEqual(memguard.page_limit(int(14.8 * GiB)), int(14.8 * GiB * 0.25))  # the Den's laptop: 3.7 GiB
        self.assertEqual(memguard.page_limit(0), int(2.5 * GiB))

    def test_scope_limits(self):
        swap = int(1.5 * GiB)
        self.assertEqual(memguard.scope_limits(4 * GiB, 1), (int(4 * GiB * 0.6), swap))
        self.assertEqual(memguard.scope_limits(8 * GiB, 1), (int(4.5 * GiB), swap))
        self.assertEqual(memguard.scope_limits(8 * GiB, 2), (int(8 * GiB * 0.6), swap))
        self.assertEqual(memguard.scope_limits(16 * GiB, 1), (6 * GiB, swap))
        self.assertEqual(memguard.scope_limits(16 * GiB, 2), (int(7.5 * GiB), swap))
        self.assertEqual(memguard.scope_limits(64 * GiB, 1), (8 * GiB, swap))
        self.assertEqual(memguard.scope_limits(64 * GiB, 3), (11 * GiB, swap))
        self.assertEqual(memguard.scope_limits(16 * GiB, 0), memguard.scope_limits(16 * GiB, 1))

    def test_the_guard_comes_first(self):
        # Even in a call, a page at the guard's limit fits under the unit's, with Natter beside it.
        for total in (4 * GiB, 8 * GiB, 16 * GiB, 64 * GiB):
            memory_max, swap_max = memguard.scope_limits(total, 1)
            self.assertLess(memguard.page_limit(total) + CALL_ROOM, memory_max + swap_max)

    def test_mem_total(self):
        self.assertEqual(memguard.mem_total("MemTotal:       15551240 kB\nMemFree: 1 kB\n"), 15551240 * 1024)
        self.assertEqual(memguard.mem_total(""), 0)


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = FakeProc(Path(self.tmp.name))
        self.proc.add(2000, "WebKitNetworkPr", [], 4 * 1024)  # big, but not a page
        self.proc.page(3000, 3001, 3002, anon_mb=1200)  # WhatsApp
        self.proc.page(4000, 4001, 4002, anon_mb=20)  # the sandbox anchor's blank page
        self.proc.children([2000, 3000, 4000])
        self.killed, self.reported, self.call = [], [], False
        self.guard = self.make()
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def make(self, killer=None, **options):
        return memguard.MemoryGuard(
            3 * GiB, lambda pid, size: self.reported.append((pid, size)), calling=lambda: self.call,
            proc=self.proc.root, pid=UI, flatpak=False, killer=killer or (lambda pid: self.killed.append(pid) or True),
            idle_add=lambda fn, *args: fn(*args), **options)

    def tearDown(self):
        self.tmp.cleanup()


class GuardTest(GuardBase):
    def test_finds_sandboxed_pages_only(self):
        self.assertEqual(memguard.web_processes(UI, self.proc.root), {3002, 4002})
        self.assertTrue(memguard.descends(3002, UI, self.proc.root))
        self.assertFalse(memguard.descends(3002, 4000, self.proc.root))

    def test_in_flatpak_every_page_in_its_namespace(self):
        # WebKit starts them through the portal there: not Natter's children.
        self.proc.children([])
        self.assertEqual(memguard.web_processes(UI, self.proc.root, flatpak=True), {3002, 4002})

    def test_held_counts_swap(self):
        self.proc.set(3002, anon_mb=1000, swap_mb=2500)
        self.assertEqual(memguard.held(3002, self.proc.root), 3500 * MiB)
        self.assertIsNone(memguard.held(9999, self.proc.root))

    def test_below_the_limit_nothing_happens(self):
        self.assertEqual(self.guard.check(now=0), [])
        self.assertEqual(self.killed, [])
        self.assertEqual((self.proc.root / "3002" / "oom_score_adj").read_text(), "500")
        self.assertEqual(self.out.getvalue(), "")

    def test_past_the_limit_in_swap_the_page_ends(self):
        # WebKit's own limit counts resident memory only: this page would pass it unseen.
        self.guard.check(now=0)
        self.proc.set(3002, anon_mb=1500, swap_mb=1800)
        self.assertEqual(self.guard.check(now=2), [3002])
        self.assertEqual(self.killed, [3002])
        self.assertEqual(self.reported, [(3002, 3300 * MiB)])
        self.assertIn("page 3002 held 3300 MB, past its limit of 3072 MB, growing 1050 MB/s; ended it",
                      self.out.getvalue())
        self.assertFalse(self.guard.recent_kill(4002, now=5))
        self.assertTrue(self.guard.recent_kill(3002, now=5))
        self.assertFalse(self.guard.recent_kill(3002, now=6))  # claimed once: one view per page

    def test_the_kill_is_noted_before_the_signal(self):
        # WebKit may report the crash before the guard's thread runs on: recent_kill knows already.
        claimed = []
        guard = self.make(killer=lambda pid: claimed.append(guard.recent_kill(pid)) or True)
        self.proc.set(3002, anon_mb=3500)
        guard.check(now=time.monotonic())
        self.assertEqual(claimed, [True])

    def test_recent_kill_of_any_page(self):
        self.proc.set(3002, anon_mb=3500)
        self.guard.check(now=0)
        self.assertTrue(self.guard.recent_kill(now=10))
        self.assertFalse(self.guard.recent_kill(now=11))

    def test_an_old_kill_is_not_claimed(self):
        self.proc.set(3002, anon_mb=3500)
        self.guard.check(now=0)
        self.assertFalse(self.guard.recent_kill(3002, now=KILL_MEMORY + 1))

    def test_in_a_call_below_the_raised_limit_the_page_stays(self):
        self.call = True
        for now, size in enumerate((3200, 3700, 4200)):  # past the limit, 500 MB/s
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [])

    def test_in_a_call_past_the_raised_limit_slow_growth_stays(self):
        # A long call's engine keeps growing, slowly: never ended for that.
        self.call = True
        for now, size in enumerate((4700, 4800, 4900, 5000)):  # 50 MB/s
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [])

    def test_in_a_call_a_runaway_past_the_raised_limit_ends(self):
        self.call = True
        self.proc.set(3002, anon_mb=4700)
        self.guard.check(now=0)
        self.proc.set(3002, anon_mb=5200)
        self.guard.check(now=2)
        self.assertEqual(self.killed, [])  # one fast look isn't two
        self.proc.set(3002, anon_mb=5700)
        self.guard.check(now=4)
        self.assertEqual(self.killed, [3002])
        self.assertIn("past its limit of 4608 MB in a call, growing 250 MB/s; ended it", self.out.getvalue())

    def test_the_call_room_lasts_a_while_after_the_call(self):
        self.call = True
        self.guard.check(now=0)
        self.call = False
        self.proc.set(3002, anon_mb=3500)
        self.guard.check(now=CALL_GRACE - 1)
        self.assertEqual(self.killed, [])
        self.guard.check(now=CALL_GRACE + 1)
        self.assertEqual(self.killed, [3002])

    def test_a_calling_check_that_fails_keeps_what_was_known(self):
        self.call = True
        self.guard.check(now=0)
        self.guard.calling = lambda: 1 / 0
        self.proc.set(3002, anon_mb=3500)
        self.guard.check(now=2)
        self.assertEqual(self.killed, [])

    def test_a_page_that_vanished(self):
        self.guard.check(now=0)
        (self.proc.root / "3002" / "status").unlink()  # gone between the list and the look
        self.assertEqual(self.guard.check(now=2), [])
        self.proc.add(3002, "WebKitWebProces", [], 3500, parent=3001)
        guard = self.make(killer=lambda pid: False)  # gone between the look and the kill
        self.assertEqual(guard.check(now=4), [])
        self.assertEqual(self.reported, [])
        self.assertFalse(guard.recent_kill(now=5))
        self.assertNotIn("ended it", self.out.getvalue())

    def test_gone_pages_are_forgotten(self):
        self.guard.check(now=0)
        self.proc.children([2000, 4000])
        self.guard.check(now=2)
        self.assertEqual(set(self.guard._pages), {4002})

    def test_a_page_is_ended_once(self):
        # SIGKILL sent, a page holding gigabytes takes a while to go: not ended (or claimed) twice.
        self.proc.set(3002, anon_mb=3500)
        self.guard.check(now=0)
        self.guard.check(now=2)
        self.assertEqual(self.killed, [3002])
        self.assertTrue(self.guard.recent_kill(now=3))
        self.assertFalse(self.guard.recent_kill(now=3))


class GrowthTest(GuardBase):
    """What the guard logs while a page grows: its busiest threads, its largest regions."""

    def test_threads_after_two_fast_looks_then_regions_at_each_step(self):
        (self.proc.root / "3002" / "smaps").write_text(SMAPS)
        threads = {3002: ("WebKitWebProces", 500), 3010: ("openh264dec0:sr", 100), 3011: ("HeapHelper", 40),
                   3012: ("WebCore: Worker", 900), 3013: ("pool-spawner", 7)}
        self.proc.threads(3002, threads)
        self.proc.set(3002, anon_mb=1600)
        self.guard.check(now=0)
        self.proc.set(3002, anon_mb=1800)
        self.guard.check(now=2)  # 100 MB/s: the first fast look
        threads.update({3002: ("WebKitWebProces", 500 + TICKS), 3010: ("openh264dec0:sr", 100 + 2 * TICKS),
                        3011: ("HeapHelper", 40 + TICKS // 2), 3014: ("GstVideoDec", TICKS // 5)})
        self.proc.threads(3002, threads)
        self.proc.set(3002, anon_mb=2100)
        self.guard.check(now=4)
        out = self.out.getvalue()
        self.assertIn("natter: memory: page 3002 growing 150 MB/s; busiest threads: openh264dec0:sr 100%, "
                      "WebKitWebProces 50%, HeapHelper 25%\n", out)
        self.assertIn("natter: memory: page 3002 holds 2100 MB, growing 150 MB/s; largest regions: 703 MB in "
                      "703 MB (1.5 MiB multiple, no reservation); 450 MB in 512 MB (not a 1.5 MiB multiple, in a "
                      "64 GiB reservation); 100 MB in 100 MB (not a 1.5 MiB multiple, no reservation)\n", out)
        self.proc.set(3002, anon_mb=2400)
        self.guard.check(now=6)  # the same spell, the same step: nothing new
        self.assertEqual(self.out.getvalue(), out)
        self.proc.set(3002, anon_mb=2600)
        self.guard.check(now=8)
        self.assertEqual(self.out.getvalue().count("busiest threads"), 1)
        self.assertEqual(self.out.getvalue().count("largest regions"), 2)

    def test_an_idle_page_loading_is_not_news(self):
        self.proc.set(3002, anon_mb=30)
        for now, size in enumerate((30, 400, 800, 1200, 1400)):
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.out.getvalue(), "")

    def test_regions(self):
        regions = memguard.anon_regions(SMAPS)
        self.assertEqual([r["held"] for r in regions], [FRAMES, 450 * MiB, 100 * MiB])
        self.assertEqual([r["frames"] for r in regions], [True, False, False])
        self.assertEqual([r["reservation"] for r in regions], [0, 64 * GiB, 0])
        self.assertEqual(memguard.anon_regions(""), [])

    def test_thread_names_with_spaces_and_brackets(self):
        self.proc.threads(3002, {77: ("a (b) c", 12)})
        self.assertEqual(memguard.thread_times(3002, self.proc.root)[77], ("a (b) c", 12))
        before = {1: ("idle", 5), 2: ("busy", 10)}
        after = {1: ("idle", 5), 2: ("busy", 10 + TICKS), 3: ("new\x1b", TICKS)}
        self.assertEqual(memguard.busiest(before, after, 2.0), [("busy", 50), ("new?", 50)])


class KillTest(unittest.TestCase):
    """The real kill, on the test's own children only."""

    def child(self, name=None):
        code = "import time\n"
        if name:
            code += f"open('/proc/self/comm', 'w').write({name!r})\n"
        code += "time.sleep(30)\n"
        process = subprocess.Popen([sys.executable, "-I", "-c", code])
        self.addCleanup(lambda: (process.kill(), process.wait()))
        deadline = time.monotonic() + 5
        while name and memguard._read(f"/proc/{process.pid}/comm").strip() != name and time.monotonic() < deadline:
            time.sleep(0.01)
        return process

    def test_ends_a_web_process_below_natter(self):
        page = self.child("WebKitWebProces")
        self.assertTrue(memguard.kill(page.pid, ancestor=os.getpid()))
        self.assertEqual(page.wait(timeout=5), -9)

    def test_refuses_another_name(self):
        # What a pid reused by another process would look like.
        other = self.child()
        self.assertFalse(memguard.kill(other.pid, ancestor=os.getpid()))
        self.assertIsNone(other.poll())

    def test_refuses_a_web_process_not_below_natter(self):
        # Another WebKit app's page has the same name.
        page, sibling = self.child("WebKitWebProces"), self.child()
        self.assertFalse(memguard.kill(page.pid, ancestor=sibling.pid))
        self.assertIsNone(page.poll())

    def test_refuses_a_pid_that_is_gone(self):
        page = self.child("WebKitWebProces")
        page.kill()
        page.wait()
        self.assertFalse(memguard.kill(page.pid, ancestor=os.getpid()))


class ThreadTest(unittest.TestCase):
    def test_a_stalled_main_loop_does_not_stall_the_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = FakeProc(Path(folder))
            proc.page(3000, 3001, 3002, anon_mb=1200)
            proc.children([3000])
            killed, queued = [], []
            guard = memguard.MemoryGuard(GiB, lambda pid, size: None, poll=0.02, proc=proc.root, pid=UI,
                                         flatpak=False, killer=lambda pid: killed.append(pid) or True,
                                         idle_add=lambda fn, *args: queued.append(args))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                guard.start()
                time.sleep(0.3)  # the main loop, stalled: nothing it would run runs
                guard.stop()
            self.assertEqual(killed, [3002])
            self.assertEqual(queued, [(3002, 1200 * MiB)])  # on_kill waits for the main loop
            self.assertIn("ended it", out.getvalue())


if __name__ == "__main__":
    unittest.main()
