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
from natter.memguard import CALL_GRACE, CALL_MARGIN, CALL_ROOM, NATTER_ROOM, RUNAWAY_RATE, GiB, KILL_MEMORY, MiB

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

    def fdinfo(self, pid, files):
        """The process's open files' fdinfo: {fd: text}."""
        folder = self.root / str(pid) / "fdinfo"
        folder.mkdir(parents=True, exist_ok=True)
        for fd, text in files.items():
            (folder / fd).write_text(text)

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
        # The swap cap is one page's limit, bounds and all: a fixed 1.5 GiB sat full with the
        # page idle (24 Oct 2026). The guard counts what's swapped, so the kernel's cap needn't.
        self.assertEqual(memguard.scope_limits(4 * GiB, 1), (int(4 * GiB * 0.6), 2 * GiB))
        self.assertEqual(memguard.scope_limits(8 * GiB, 1), (int(4.5 * GiB), int(2.5 * GiB)))
        self.assertEqual(memguard.scope_limits(8 * GiB, 2), (int(8 * GiB * 0.6), int(2.5 * GiB)))
        self.assertEqual(memguard.scope_limits(16 * GiB, 1), (6 * GiB, 4 * GiB))
        self.assertEqual(memguard.scope_limits(16 * GiB, 2), (int(7.5 * GiB), 4 * GiB))
        self.assertEqual(memguard.scope_limits(64 * GiB, 1), (8 * GiB, 6 * GiB))
        self.assertEqual(memguard.scope_limits(64 * GiB, 3), (11 * GiB, 6 * GiB))
        self.assertEqual(memguard.scope_limits(0, 1), (int(2.5 * GiB) + int(1.5 * GiB) + GiB // 2, int(2.5 * GiB)))
        self.assertEqual(memguard.scope_limits(16 * GiB, 0), memguard.scope_limits(16 * GiB, 1))
        for total in (4 * GiB, 8 * GiB, int(14.8 * GiB), 64 * GiB, 0):
            self.assertEqual(memguard.scope_limits(total, 1)[1], memguard.page_limit(total))

    def test_scope_swap_is_at_most_half_the_computers(self):
        # A 2 GB swap file on 8 GB: the unit may take 1 GB of it, not all of it (page_limit, 2.5 GiB).
        self.assertEqual(memguard.scope_limits(8 * GiB, 1, 2 * GiB), (int(4.5 * GiB), GiB))
        self.assertEqual(memguard.scope_limits(4 * GiB, 1, 2 * GiB)[1], GiB)
        # Plenty of swap, or none known: one page's limit.
        self.assertEqual(memguard.scope_limits(16 * GiB, 1, 16 * GiB), (6 * GiB, 4 * GiB))
        self.assertEqual(memguard.scope_limits(16 * GiB, 1, 0), (6 * GiB, 4 * GiB))
        self.assertEqual(memguard.swap_total("MemTotal: 100 kB\nSwapTotal: 2097152 kB\n"), 2 * GiB)
        self.assertEqual(memguard.swap_total("MemTotal: 100 kB\nSwapTotal: 0 kB\n"), 0)
        self.assertEqual(memguard.swap_total(""), 0)

    def test_the_guard_comes_first(self):
        # Even in a call, the most the guard lets a page hold fits under the unit's limit, swap and
        # all, with Natter beside it: there the kernel would end it, and under a launcher's unit
        # (OOMPolicy=stop) all of Natter.
        for total in (4 * GiB, int(7.6 * GiB), 8 * GiB, int(14.8 * GiB), 16 * GiB, 64 * GiB, 0):
            memory_max, swap_max = memguard.scope_limits(total, 1)
            guard = memguard.MemoryGuard(memguard.page_limit(total), ceiling=memguard.page_ceiling(total),
                                         idle_add=lambda *a: None)
            for in_call in (False, True):
                soft, hard = guard.limits(in_call)
                self.assertLessEqual(soft, hard, (total, in_call))
                self.assertLessEqual(hard + CALL_MARGIN, memory_max + swap_max - NATTER_ROOM, (total, in_call))

    def test_the_ceiling_only_binds_on_small_computers(self):
        for total in (int(7.6 * GiB), 8 * GiB, int(14.8 * GiB), 16 * GiB, 64 * GiB):
            limit = memguard.page_limit(total)
            self.assertGreater(memguard.page_ceiling(total), limit + CALL_ROOM + CALL_MARGIN, total)
        self.assertEqual(memguard.page_ceiling(4 * GiB), int(4 * GiB * 0.6) + 2 * GiB - GiB)
        guard = memguard.MemoryGuard(2 * GiB, ceiling=memguard.page_ceiling(4 * GiB), idle_add=lambda *a: None)
        self.assertEqual(guard.limits(False), (2 * GiB, memguard.page_ceiling(4 * GiB)))
        self.assertEqual(guard.limits(True), (memguard.page_ceiling(4 * GiB),) * 2)

    def test_limits(self):
        guard = memguard.MemoryGuard(3 * GiB, idle_add=lambda *a: None)
        self.assertEqual(guard.limits(False), (3 * GiB, 3 * GiB + CALL_ROOM))
        self.assertEqual(guard.limits(True), (3 * GiB + CALL_ROOM, 3 * GiB + CALL_ROOM + CALL_MARGIN))
        for in_call in (False, True):  # the same sums for anyone else who needs them (app.py's kill_threshold)
            self.assertEqual(memguard.page_limits(3 * GiB, None, in_call), guard.limits(in_call))
            self.assertEqual(memguard.page_limits(3 * GiB, 4 * GiB, in_call),
                             tuple(min(limit, 4 * GiB) for limit in guard.limits(in_call)))
        self.assertLess(RUNAWAY_RATE, 130 * MiB)  # the slowest runaway seen grew 130-160 MB/s

    def test_mem_total(self):
        self.assertEqual(memguard.mem_total("MemTotal:       15551240 kB\nMemFree: 1 kB\n"), 15551240 * 1024)
        self.assertEqual(memguard.mem_total(""), 0)


class GemTest(unittest.TestCase):
    """What a page owns on the GPU, by its DRM files' fdinfo (gem_held): i915 writes the totals
    in bytes, KiB or MiB, once per client however many descriptors it has, and stolen memory
    under a key of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = FakeProc(Path(self.tmp.name))
        self.proc.page(3000, 3001, 3002, anon_mb=1200)

    def test_each_client_once_in_any_unit(self):
        self.proc.fdinfo(3002, {
            "5": "drm-driver:\ti915\ndrm-client-id:\t30\ndrm-total-system0:\t1048576\ndrm-total-stolen-system0:\t4096\n",
            "6": "drm-driver:\ti915\ndrm-client-id:\t30\ndrm-total-system0:\t1048576\n",
            "8": "drm-driver:\ti915\ndrm-client-id:\t31\ndrm-total-system0:\t512 MiB\n",
            "9": "drm-driver:\tamdgpu\ndrm-client-id:\t40\ndrm-total-system:\t2048 KiB\n",
            "10": "pos:\t0\nflags:\t02\nmnt_id:\t41\n",
            "11": "drm-driver:\ti915\ndrm-client-id:\t32\ndrm-total-system0:\tlots\n",
        })
        self.assertEqual(memguard.gem_held(3002, self.proc.root), MiB + 512 * MiB + 2 * MiB)

    def test_nothing_to_read(self):
        self.assertEqual(memguard.gem_held(3002, self.proc.root), 0)  # no fdinfo at all
        self.proc.fdinfo(3002, {"5": "pos:\t0\n"})
        self.assertEqual(memguard.gem_held(3002, self.proc.root), 0)
        self.assertEqual(memguard.gem_held(99999, self.proc.root), 0)


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

    def test_page_sizes(self):
        # What app.py weighs a hidden page by: each page alone, swap and all, not Natter's whole unit.
        self.proc.set(3002, anon_mb=1000, swap_mb=500)
        self.assertEqual(memguard.page_sizes(UI, self.proc.root, flatpak=False), {3002: 1500 * MiB, 4002: 20 * MiB})

    def test_pages_keep_their_oom_score_where_the_kernel_would_stop_all_of_natter(self):
        # Under a launcher's unit (OOMPolicy=stop) the kernel's choosing a page stops all of Natter.
        guard = self.make(oom_score_adj=None)
        guard.check(now=0)
        self.assertEqual((self.proc.root / "3002" / "oom_score_adj").read_text(), "0\n")

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
        self.proc.set(3002, anon_mb=1500, swap_mb=1000)
        self.assertEqual(self.guard.check(now=2), [])
        self.proc.set(3002, anon_mb=1500, swap_mb=1800)
        self.assertEqual(self.guard.check(now=4), [3002])  # past the limit, growing at both looks
        self.assertEqual(self.killed, [3002])
        self.assertEqual(self.reported, [(3002, 3300 * MiB)])
        self.assertIn("page 3002 held 3300 MB, past its limit of 3072 MB, growing 400 MB/s; ended it",
                      self.out.getvalue())
        self.assertFalse(self.guard.recent_kill(4002, now=5))
        self.assertTrue(self.guard.recent_kill(3002, now=5))
        self.assertFalse(self.guard.recent_kill(3002, now=6))  # claimed once: one view per page

    def test_past_the_limit_holding_still_the_page_stays(self):
        # What a call left behind, or slow creep: left for the fresh page (app.py), not ended in
        # front of whoever is using the window.
        for now, size in enumerate((3300, 3300, 3350, 3350, 3400, 4500)):
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [])

    def test_past_the_limit_growing_like_a_runaway_ends(self):
        for now, size in enumerate((2800, 3000, 3200)):  # 100 MB/s, past the limit at the third look
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [3002])

    def test_slower_than_a_runaway_is_ended_past_the_room(self):
        for now, size in enumerate((4000, 4100, 4200, 4300, 4400, 4500, 4600)):  # 50 MB/s
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [])
        self.proc.set(3002, anon_mb=4700)
        self.guard.check(now=14)
        self.assertEqual(self.killed, [3002])
        self.assertIn("held 4700 MB, past its limit of 4608 MB, growing 50 MB/s; ended it", self.out.getvalue())

    def test_the_kill_is_noted_before_the_signal(self):
        # WebKit may report the crash before the guard's thread runs on: recent_kill knows already.
        claimed = []
        guard = self.make(killer=lambda pid: claimed.append(guard.recent_kill(pid)) or True)
        self.proc.set(3002, anon_mb=5000)
        guard.check(now=time.monotonic())
        self.assertEqual(claimed, [True])

    def test_recent_kill_of_any_page(self):
        self.proc.set(3002, anon_mb=5000)
        self.guard.check(now=0)
        self.assertTrue(self.guard.recent_kill(now=10))
        self.assertFalse(self.guard.recent_kill(now=11))

    def test_an_old_kill_is_not_claimed(self):
        self.proc.set(3002, anon_mb=5000)
        self.guard.check(now=0)
        self.assertFalse(self.guard.recent_kill(3002, now=KILL_MEMORY + 1))

    def test_in_a_call_below_the_raised_limit_the_page_stays(self):
        self.call = True
        for now, size in enumerate((3200, 3700, 4200)):  # past the limit, 500 MB/s
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [])

    def test_in_a_call_past_the_raised_limit_slow_growth_stays(self):
        # A long call's engine keeps growing, slowly: not ended for that until the call's margin.
        self.call = True
        for now, size in enumerate((4700, 4800, 4900, 5000, 5100)):  # 50 MB/s
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
        self.assertEqual(self.killed, [])
        self.proc.set(3002, anon_mb=5200)  # past the call's margin: the unit's backstop isn't far now
        self.guard.check(now=10)
        self.assertEqual(self.killed, [3002])
        self.assertIn("past its limit of 5120 MB in a call, growing 50 MB/s; ended it", self.out.getvalue())

    def test_in_a_call_a_runaway_past_the_raised_limit_ends(self):
        self.call = True
        self.proc.set(3002, anon_mb=4650)
        self.guard.check(now=0)
        self.proc.set(3002, anon_mb=4800)
        self.guard.check(now=2)
        self.assertEqual(self.killed, [])  # one fast look isn't two
        self.proc.set(3002, anon_mb=4950)
        self.guard.check(now=4)
        self.assertEqual(self.killed, [3002])
        self.assertIn("past its limit of 4608 MB in a call, growing 75 MB/s; ended it", self.out.getvalue())

    def test_in_a_call_the_runaway_of_20_october_is_ended(self):
        # 130-160 MB/s, in a call in any account: ended within two looks of passing the call's room.
        self.call = True
        sizes = list(range(2600, 6000, 300))  # 150 MB/s
        for now, size in enumerate(sizes):
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=now * 2)
            if self.killed:
                break
        self.assertEqual(self.killed, [3002])
        self.assertLessEqual(size, 3072 + 1536 + 2 * 300)

    def test_the_call_room_lasts_a_while_after_the_call(self):
        self.call = True
        self.guard.check(now=0)
        self.call = False
        self.proc.set(3002, anon_mb=4700)  # in the call's room, holding still
        self.guard.check(now=CALL_GRACE - 1)
        self.assertEqual(self.killed, [])
        self.guard.check(now=CALL_GRACE + 1)
        self.assertEqual(self.killed, [3002])

    def test_after_the_call_a_runaway_is_ended_at_once(self):
        # Not a minute later: the grace keeps only the call's room.
        self.call = True
        self.guard.check(now=0)
        self.call = False
        for now, size in enumerate((4400, 4700, 5000)):  # 150 MB/s
            self.proc.set(3002, anon_mb=size)
            self.guard.check(now=10 + now * 2)
        self.assertEqual(self.killed, [3002])

    def test_after_a_long_call_on_an_8_gb_computer_the_page_stays(self):
        # 7.6 GiB to the kernel: a limit of 2.5 GiB, and an idle page of 1.3 GB that a long call
        # left 1.4 GB more. It holds still once the call is over, window shown: no reload.
        total = int(7.6 * GiB)
        guard = self.make(ceiling=memguard.page_ceiling(total))
        guard.limit = memguard.page_limit(total)
        self.call = True
        self.proc.set(3002, anon_mb=2700)
        guard.check(now=0)
        self.call = False
        for now in range(2, int(CALL_GRACE) + 600, 2):
            guard.check(now=now)
        self.assertEqual(self.killed, [])
        for now, size in ((1000, 2900), (1002, 3100), (1004, 3300)):  # then grows like a runaway: ended
            self.proc.set(3002, anon_mb=size)
            guard.check(now=now)
        self.assertEqual(self.killed, [3002])

    def test_a_calling_check_that_fails_keeps_what_was_known(self):
        self.call = True
        self.guard.check(now=0)
        self.guard.calling = lambda: 1 / 0
        self.proc.set(3002, anon_mb=4700)
        self.guard.check(now=2)
        self.assertEqual(self.killed, [])

    def test_a_page_that_vanished(self):
        self.guard.check(now=0)
        (self.proc.root / "3002" / "status").unlink()  # gone between the list and the look
        self.assertEqual(self.guard.check(now=2), [])
        self.proc.add(3002, "WebKitWebProces", [], 5000, parent=3001)
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
        self.proc.set(3002, anon_mb=5000)
        self.guard.check(now=0)
        self.guard.check(now=2)
        self.assertEqual(self.killed, [3002])
        self.assertTrue(self.guard.recent_kill(now=3))
        self.assertFalse(self.guard.recent_kill(now=3))


class RecentSizesTest(GuardBase):
    """What the pages held at the guard's last look, for Natter's main loop (app.py held_by_page),
    which then needn't read every page's status itself."""

    def test_from_the_last_look_while_its_recent(self):
        self.assertIsNone(self.guard.recent_sizes(now=0))  # no look yet
        self.guard.check(now=10)
        self.assertEqual(self.guard.recent_sizes(now=11), {3002: 1200 * MiB, 4002: 20 * MiB})
        self.assertIsNone(self.guard.recent_sizes(now=10 + 3 * self.guard.poll + 1))  # the guard stopped looking

    def test_a_page_it_ended_isnt_in_them(self):
        self.proc.set(3002, anon_mb=20 * 1024)
        self.assertEqual(self.guard.check(now=0), [3002])
        self.assertEqual(self.guard.recent_sizes(now=0), {4002: 20 * MiB})


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

    def test_no_regions_read_during_a_call(self):
        # smaps walks the page under its memory map's lock: not while the call's sound runs there.
        smaps = self.proc.root / "3002" / "smaps"
        smaps.write_text(SMAPS)
        smaps.chmod(0)  # read, it would say "unreadable"
        self.guard.check(now=0)
        self.call = True
        self.proc.set(3002, anon_mb=2100)
        self.guard.check(now=2)
        self.assertNotIn("largest regions", self.out.getvalue())
        smaps.chmod(0o600)
        self.call = False
        self.guard.check(now=CALL_GRACE + 3)  # the call and the guard's own grace over: told now
        self.assertIn("natter: memory: page 3002 holds 2100 MB", self.out.getvalue())
        self.assertIn("largest regions: 703 MB", self.out.getvalue())

    def test_the_step_line_says_what_the_size_leaves_out(self):
        # The page's GPU buffers (fdinfo) and the unit's shmem and zswap pool (charge): seen on
        # 24 Oct 2026 as 542 MB of GEM objects, in no RssAnon and under no limit.
        (self.proc.root / "3002" / "smaps").write_text(SMAPS)
        self.proc.fdinfo(3002, {"5": "drm-client-id:\t30\ndrm-total-system0:\t542 MiB\ndrm-total-stolen-system0:\t9 MiB\n",
                                "6": "drm-client-id:\t30\ndrm-total-system0:\t542 MiB\n",  # the same client, dup'd
                                "7": "pos:\t0\nflags:\t02\n"})
        guard = self.make(charge=lambda: {"shmem": 400 * MiB, "zswap": 100 * MiB})
        self.proc.set(3002, anon_mb=1600)
        guard.check(now=0)
        self.proc.set(3002, anon_mb=2100)
        guard.check(now=2)
        self.assertIn("natter: memory: page 3002 holds 2100 MB, growing 250 MB/s; largest regions: 703 MB in 703 MB "
                      "(1.5 MiB multiple, no reservation); 450 MB in 512 MB (not a 1.5 MiB multiple, in a 64 GiB "
                      "reservation); 100 MB in 100 MB (not a 1.5 MiB multiple, no reservation); besides, GPU buffers "
                      "542 MB, the unit's shmem 400 MB, zswap 100 MB\n", self.out.getvalue())
        self.assertEqual(self.killed, [])

    def test_the_step_line_without_anything_beside(self):
        (self.proc.root / "3002" / "smaps").write_text(SMAPS)
        guard = self.make(charge=lambda: {"shmem": 0, "zswap": 0})
        self.proc.set(3002, anon_mb=1600)
        guard.check(now=0)
        self.proc.set(3002, anon_mb=2100)
        guard.check(now=2)
        self.assertNotIn("besides", self.out.getvalue())
        self.assertIn("no reservation)\n", self.out.getvalue())

    def test_gpu_buffers_and_the_charge_never_count_towards_ending_a_page(self):
        self.proc.fdinfo(3002, {"5": "drm-client-id:\t30\ndrm-total-system0:\t2147483648\n"})  # 2 GiB, in bytes
        guard = self.make(charge=lambda: {"shmem": 3 * GiB, "zswap": GiB})
        self.assertEqual(guard.limits(False), (3 * GiB, 3 * GiB + CALL_ROOM))
        self.proc.set(3002, anon_mb=3 * 1024 - 1)  # just under its limit, with 2 GiB of GEM beside
        for now in (0, 2, 4):
            self.assertEqual(guard.check(now=now), [])
        self.assertEqual(self.killed, [])
        self.assertEqual(memguard.gem_held(3002, self.proc.root), 2 * GiB)

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
            guard = memguard.MemoryGuard(GiB // 2, lambda pid, size: None, poll=0.02, ceiling=GiB, proc=proc.root,
                                         pid=UI, flatpak=False, killer=lambda pid: killed.append(pid) or True,
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
