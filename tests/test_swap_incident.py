"""The swap exhaustion incident of 20 Oct 2026, and the video attach that took a page to 12 GB the
same day: what Natter Den changed (system.py, app.py, window.py)."""

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MEMINFO = "MemTotal: 15000000 kB\nMemAvailable: 9000000 kB\nSwapTotal: {total} kB\nSwapFree: {free} kB\n"
GiB = 1024 ** 3


class SwapTest(unittest.TestCase):
    def test_share(self):
        from natter.system import swap_share

        self.assertAlmostEqual(swap_share(MEMINFO.format(total=1000, free=250)), 0.75)
        self.assertEqual(swap_share(MEMINFO.format(total=0, free=0)), 0.0)

    def test_full_by_share_or_own_budget(self):
        from natter import system

        with mock.patch.object(system, "own_swap", return_value=0):
            self.assertTrue(system.swap_full(meminfo=MEMINFO.format(total=1000, free=300)))  # 70% in use
            self.assertFalse(system.swap_full(meminfo=MEMINFO.format(total=1000, free=900)))
        with mock.patch.object(system, "own_swap", return_value=(system.SWAP_BUDGET_MB + 1) << 20):
            self.assertTrue(system.swap_full(meminfo=MEMINFO.format(total=1000, free=900)))

    def test_reclaimer_stops_when_swap_is_full(self):
        from natter import system

        reclaimer = system.Reclaimer(floor_mb=0)
        reclaimer.cgroup = object()
        steps = []
        reclaimer.usage = lambda: 10 << 30
        reclaimer._step = lambda: steps.append(1) or True
        with mock.patch.object(system, "swap_full", return_value=True):
            reclaimer._run()
        self.assertEqual(steps, [])


class FakeSystemd:
    """The session bus, as far as Natter asks systemd's user manager about its unit."""

    def __init__(self, policy="stop", knows_oom_policy=True):
        self.policy, self.knows_oom_policy = policy, knows_oom_policy
        self.calls = []

    def call_sync(self, _name, _path, _interface, method, params, _reply, _flags, _timeout, _cancellable):
        from gi.repository import GLib

        args = params.unpack() if params is not None else ()
        self.calls.append((method, args))
        if method == "StartTransientUnit":
            if not self.knows_oom_policy and "OOMPolicy" in dict(args[2]):
                raise GLib.Error("Cannot set property OOMPolicy, or unknown property.")
            return GLib.Variant("(o)", ("/org/freedesktop/systemd1/job/7",))
        if method == "GetUnit":
            return GLib.Variant("(o)", ("/org/freedesktop/systemd1/unit/natter",))
        if method == "Get":
            return GLib.Variant("(v)", (GLib.Variant("s", self.policy),))
        return None

    def sent(self, method):
        return [args for name, args in self.calls if name == method]


class ScopeLimitsTest(unittest.TestCase):
    """The unit's backstop: MemoryMax and MemorySwapMax from the computer's memory, never
    MemoryHigh (it only swapped the runaway and stalled the window), and OOMPolicy=continue where
    Natter makes the scope, so the kernel ending a page doesn't stop Natter."""

    def setUp(self):
        from natter import APP_ID, system

        self.system = system
        self.bus = FakeSystemd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cgroup = Path(self.tmp.name) / f"app-ada-{APP_ID}-4242.scope"  # Ada's launcher made it
        self.cgroup.mkdir()
        self.limits("max", "max")
        self.out = io.StringIO()
        for patch in (mock.patch.object(system, "_made_scope", None),
                      mock.patch.object(system, "_launcher_limits", None),
                      mock.patch.object(system, "_told_oom_policy", False),
                      mock.patch.object(system, "in_sandbox", return_value=False),
                      mock.patch.object(system, "mem_total", return_value=16 * GiB),
                      mock.patch.object(system, "own_cgroup", return_value=self.cgroup),
                      mock.patch.object(system, "_has_owner", return_value=True),
                      mock.patch.object(system.Gio, "bus_get_sync", return_value=self.bus),
                      contextlib.redirect_stdout(self.out)):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)

    def limits(self, memory_max, swap_max):
        (self.cgroup / "memory.max").write_text(f"{memory_max}\n")
        (self.cgroup / "memory.swap.max").write_text(f"{swap_max}\n")

    def test_own_scope_is_made_with_the_limits_and_continue(self):
        with mock.patch.object(self.system, "in_own_scope", side_effect=[False, True]):
            name = self.system.move_to_own_scope()
        self.assertEqual(name, self.system.scope_name(os.getpid()))
        (unit, mode, properties, _aux), = self.bus.sent("StartTransientUnit")
        properties = dict(properties)
        self.assertEqual((unit, mode), (name, "fail"))
        self.assertEqual(properties["MemoryMax"], 6 * GiB)
        self.assertEqual(properties["MemorySwapMax"], int(1.5 * GiB))
        self.assertEqual(properties["OOMPolicy"], "continue")
        self.assertNotIn("MemoryHigh", properties)
        self.assertEqual(properties["Slice"], "app.slice")

    def test_a_systemd_without_oom_policy_still_makes_the_scope(self):
        self.bus.knows_oom_policy = False
        with mock.patch.object(self.system, "in_own_scope", side_effect=[False, True]):
            self.assertIsNotNone(self.system.move_to_own_scope())
        first, second = self.bus.sent("StartTransientUnit")
        self.assertIn("OOMPolicy", dict(first[2]))
        self.assertNotIn("OOMPolicy", dict(second[2]))
        self.assertEqual(dict(second[2])["MemoryMax"], 6 * GiB)
        self.assertIn("stops all of", self.out.getvalue())

    def test_a_launchers_scope_gets_what_can_be_set(self):
        self.assertTrue(self.system.limit_own_scope())
        (unit, runtime, properties), = self.bus.sent("SetUnitProperties")
        self.assertEqual((unit, runtime), (self.cgroup.name, True))
        self.assertEqual(properties, [("MemoryMax", 6 * GiB), ("MemorySwapMax", int(1.5 * GiB))])
        # Its OOMPolicy is the launcher's: said once.
        self.assertIn(f"{self.cgroup.name} has OOMPolicy=stop", self.out.getvalue())
        self.assertTrue(self.system.limit_own_scope(accounts=2))
        self.assertEqual(self.bus.sent("SetUnitProperties")[-1][2],
                         [("MemoryMax", int(7.5 * GiB)), ("MemorySwapMax", int(1.5 * GiB))])
        self.assertEqual(self.out.getvalue().count("OOMPolicy"), 1)
        self.assertEqual(len(self.bus.sent("GetUnit")), 1)

    def test_a_launcher_with_continue_says_nothing(self):
        self.bus.policy = "continue"
        self.assertTrue(self.system.limit_own_scope())
        self.assertEqual(self.out.getvalue(), "")

    def test_a_launchers_lower_limits_stay(self):
        # Such as `systemd-run --scope -p MemoryMax=5G -p MemorySwapMax=1G natter`.
        self.limits(5 * GiB, GiB)
        self.system.limit_own_scope()
        self.limits(5 * GiB, GiB)  # what Natter set, read back: still the launcher's ceiling
        self.system.limit_own_scope(accounts=3)
        for _unit, _runtime, properties in self.bus.sent("SetUnitProperties"):
            self.assertEqual(properties, [("MemoryMax", 5 * GiB), ("MemorySwapMax", GiB)])

    def test_accounts_change_natters_own_scope(self):
        with mock.patch.object(self.system, "_made_scope", self.cgroup.name):
            self.limits(6 * GiB, int(1.5 * GiB))  # as Natter made it
            self.assertTrue(self.system.limit_own_scope(accounts=2))
        self.assertEqual(self.bus.sent("SetUnitProperties")[0][2],
                         [("MemoryMax", int(7.5 * GiB)), ("MemorySwapMax", int(1.5 * GiB))])
        self.assertEqual(self.bus.sent("GetUnit"), [])  # OOMPolicy=continue was set at the start
        self.assertEqual(self.out.getvalue(), "")

    def test_nothing_in_a_sandbox_or_a_unit_not_its_own(self):
        with mock.patch.object(self.system, "in_sandbox", return_value=True):
            self.assertFalse(self.system.limit_own_scope())
            self.assertIsNone(self.system.move_to_own_scope())
        with mock.patch.object(self.system, "own_cgroup", return_value=self.cgroup.parent / "vte-spawn-1.scope"):
            self.assertFalse(self.system.limit_own_scope())
        self.assertEqual(self.bus.calls, [])


class FreshPageReasonTest(unittest.TestCase):
    def app(self, past_limit=False):
        from natter.app import Natter

        fake = type("App", (), {})()
        fake._memory_past_limit = lambda: past_limit
        return fake, Natter

    def test_reasons(self):
        from natter import app as app_module

        fake, Natter = self.app(past_limit=True)
        self.assertEqual(Natter._memory_reason(fake), "past its limit")
        fake, Natter = self.app()
        with mock.patch.object(app_module, "own_swap", return_value=(app_module.SWAP_BUDGET_MB + 1) << 20):
            self.assertEqual(Natter._memory_reason(fake), "much of it in swap")
        with mock.patch.object(app_module, "own_swap", return_value=0), \
                mock.patch.object(app_module, "footprint", return_value=(app_module.MEMORY_LIMIT + 1) << 20), \
                mock.patch.object(app_module, "memory_tight", return_value=False), \
                mock.patch.object(app_module, "swap_full", return_value=True):
            self.assertEqual(Natter._memory_reason(fake), "the computer is short of memory")
        with mock.patch.object(app_module, "own_swap", return_value=0), \
                mock.patch.object(app_module, "footprint", return_value=100 << 20), \
                mock.patch.object(app_module, "memory_tight", return_value=True), \
                mock.patch.object(app_module, "swap_full", return_value=True):
            self.assertEqual(Natter._memory_reason(fake), "")  # a small Natter isn't the problem


class HideAndThawTest(unittest.TestCase):
    def test_memory_checked_the_moment_the_window_hides(self):
        from natter.app import Natter

        calls = []
        fake = type("App", (), {})()
        fake._reclaim_timer = 0
        fake._schedule_fresh_pages = lambda visible: None
        fake._refresh_tray = lambda: None
        fake._write_menu_chats = lambda: None
        fake._fresh_pages_for_memory = lambda: calls.append("fresh") or False
        fake.settings = {"light_background": False}
        Natter._on_window_visible(fake, False)
        self.assertEqual(calls, ["fresh"])

    def test_a_long_gap_means_a_thaw(self):
        from natter.app import THAW_GAP, Natter

        thawed = []
        fake = type("App", (), {})()
        fake._network_processes, fake._network_looks, fake._quitting = set(), 0, False
        fake._last_look = time.monotonic() - THAW_GAP - 5
        fake._thawed = lambda paused: thawed.append(paused)
        fake.accounts = []
        with mock.patch("natter.app.network_processes", return_value=set()):
            Natter._check_network_process(fake)
            Natter._check_network_process(fake)  # right after: no gap
        self.assertEqual(len(thawed), 1)
        self.assertGreater(thawed[0], THAW_GAP)


class CloseWhileHiddenTest(unittest.TestCase):
    def test_a_hidden_window_asked_to_close_quits(self):
        from natter.window import Window

        quit_calls = []
        fake = type("Window", (), {})()
        fake.get_visible = lambda: False
        fake.app = type("App", (), {"quit_natter": lambda _self: quit_calls.append(1)})()
        self.assertTrue(Window._on_close(fake, None))
        self.assertEqual(quit_calls, [1])


if __name__ == "__main__":
    unittest.main()
