"""The swap exhaustion incident of 20 Oct 2026, and the video attach that took a page to 12 GB the
same day: what Natter Den changed (system.py, app.py, window.py). And what that change did wrong,
seen on CachyOS and Ada in October 2026: WhatsApp reloaded the moment the window was closed, and
every half hour after while it stayed closed (Natter's own Reclaimer filled the very swap budget
that set off the reload), and the kernel's backstop on a launcher's unit stopped all of Natter."""

import contextlib
import io
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from natter.app import (FRESH_AGAIN, HIDDEN_BEFORE_FRESH, HIDDEN_BEFORE_RECLAIM, MEMORY_LIMIT, QUIET_BEFORE_FRESH,
                        SETTLE_BEFORE_RECLAIM)

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

    def test_reclaimer_stops_when_the_computer_waits_on_memory(self):
        # There the kernel reclaims anyway, and systemd-oomd ends whole units, the one
        # reclaiming the most first.
        from natter import system

        reclaimer = system.Reclaimer(floor_mb=0)
        reclaimer.cgroup = object()
        steps, pressure = [], iter([1.0, 2.0, system.RECLAIM_PRESSURE + 1, 0.0])
        reclaimer.usage = lambda: 10 << 30
        reclaimer._step = lambda: steps.append(1) or True
        reclaimer._stop.wait = lambda _seconds: False
        with mock.patch.object(system, "swap_full", return_value=False), \
                mock.patch.object(system, "memory_stalls", side_effect=lambda: next(pressure)):
            reclaimer._run()
        self.assertEqual(len(steps), 2)

    def test_memory_stalls(self):
        from natter.system import memory_stalls

        text = ("some avg10=12.50 avg60=4.19 avg300=5.35 total=77402451\n"
                "full avg10=99.00 avg60=3.57 avg300=4.73 total=1\n")
        self.assertEqual(memory_stalls(text), 12.5)
        self.assertEqual(memory_stalls(""), 0.0)  # no PSI in this kernel
        self.assertEqual(memory_stalls("some avg10=lots\n"), 0.0)


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
                      mock.patch.object(system, "_made_without_policy", False),
                      mock.patch.object(system, "_unit_policy", None),
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

    def test_a_systemd_without_oom_policy_makes_the_scope_without_limits(self):
        # There systemd's default, "stop", would stop all of Natter for one page.
        self.bus.knows_oom_policy = False
        with mock.patch.object(self.system, "in_own_scope", side_effect=[False, True]):
            name = self.system.move_to_own_scope()
        self.assertIsNotNone(name)
        first, second = self.bus.sent("StartTransientUnit")
        self.assertIn("OOMPolicy", dict(first[2]))
        for limit in ("OOMPolicy", "MemoryMax", "MemorySwapMax"):
            self.assertNotIn(limit, dict(second[2]))
        self.assertEqual(dict(second[2])["Slice"], "app.slice")  # still grouped
        self.assertIn("sets no memory limit", self.out.getvalue())
        self.cgroup = self.cgroup.parent / name
        self.cgroup.mkdir()
        with mock.patch.object(self.system, "own_cgroup", return_value=self.cgroup):
            self.assertFalse(self.system.limit_own_scope(accounts=2))  # an account added: still none
            self.assertFalse(self.system.pages_end_alone())
        self.assertEqual(self.bus.sent("SetUnitProperties"), [])
        self.assertEqual(self.out.getvalue().count("sets no memory limit"), 1)

    def test_a_launchers_unit_that_stops_all_of_natter_gets_no_limit(self):
        # KDE's app-…@….service, GNOME's and Ada's scopes: OOMPolicy=stop, theirs to change. A
        # MemoryMax there turned a page past it into all of Natter stopping (Ada, 22 Oct 2026).
        self.assertFalse(self.system.limit_own_scope())
        self.assertEqual(self.bus.sent("SetUnitProperties"), [])
        said = self.out.getvalue()
        self.assertIn(f"{self.cgroup.name} has OOMPolicy=stop", said)
        self.assertIn("sets no memory limit on it; the guard ends a page that grows without end, alone", said)
        self.assertNotIn("past ", said)  # the old line said a limit was there
        self.assertFalse(self.system.limit_own_scope(accounts=2))
        self.assertEqual(self.bus.sent("SetUnitProperties"), [])
        self.assertEqual(self.out.getvalue().count("OOMPolicy"), 1)  # said once
        self.assertEqual(len(self.bus.sent("GetUnit")), 1)  # asked once
        self.assertFalse(self.system.pages_end_alone())

    def test_an_unknown_policy_counts_as_stop(self):
        self.bus.policy = ""
        with mock.patch.object(self.system, "_unit_property", return_value=None):
            self.assertFalse(self.system.limit_own_scope())
        self.assertEqual(self.bus.sent("SetUnitProperties"), [])
        self.assertIn("OOMPolicy=unknown", self.out.getvalue())

    def test_a_launcher_with_continue_gets_the_limits(self):
        self.bus.policy = "continue"
        self.assertTrue(self.system.limit_own_scope())
        (unit, runtime, properties), = self.bus.sent("SetUnitProperties")
        self.assertEqual((unit, runtime), (self.cgroup.name, True))
        self.assertEqual(properties, [("MemoryMax", 6 * GiB), ("MemorySwapMax", int(1.5 * GiB))])
        self.assertTrue(self.system.limit_own_scope(accounts=2))
        self.assertEqual(self.bus.sent("SetUnitProperties")[-1][2],
                         [("MemoryMax", int(7.5 * GiB)), ("MemorySwapMax", int(1.5 * GiB))])
        self.assertEqual(self.out.getvalue(), "")
        self.assertTrue(self.system.pages_end_alone())

    def test_a_launchers_lower_limits_stay(self):
        # Such as a launcher's unit with OOMPolicy=continue and MemoryMax=5G, MemorySwapMax=1G.
        self.bus.policy = "continue"
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
        with mock.patch.object(self.system, "_made_scope", self.cgroup.name):
            self.assertTrue(self.system.pages_end_alone())

    def test_nothing_in_a_sandbox_or_a_unit_not_its_own(self):
        with mock.patch.object(self.system, "in_sandbox", return_value=True):
            self.assertFalse(self.system.limit_own_scope())
            self.assertIsNone(self.system.move_to_own_scope())
        with mock.patch.object(self.system, "own_cgroup", return_value=self.cgroup.parent / "vte-spawn-1.scope"):
            self.assertFalse(self.system.limit_own_scope())
            self.assertFalse(self.system.pages_end_alone())
        self.assertEqual(self.bus.calls, [])


MiB = 1024 ** 2


class Clock:
    """time.monotonic, as far as app.py's memory decisions read it."""

    def __init__(self, now=100_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def minutes(self, count):
        self.now += count * 60


class Timers:
    """GLib's timeouts as _on_window_visible and _reclaim use them: recorded, never run."""

    SOURCE_REMOVE, SOURCE_CONTINUE = False, True

    def __init__(self):
        self.added = []

    def timeout_add_seconds(self, seconds, run):
        self.added.append((seconds, run))
        return len(self.added)

    def source_remove(self, _timer):
        pass

    def getenv(self, _name):
        return None


class Page:
    """An account, as the memory decisions see it. A fresh page reloads it: small again, just loaded."""

    def __init__(self, world, name, loaded_at):
        self.world, self.id, self.name = world, name, name
        self.ready, self.reloading, self.crashing, self.calling = True, False, False, False
        self.replying = 0
        self.loaded_at, self.notified_at = loaded_at, None
        self.fresh = []

    def fresh_page(self):
        self.fresh.append(self.world.clock.now)
        self.loaded_at = self.world.clock.now
        self.world.pages[self.name] = 900 * MiB


class Reclaimer:
    available = True

    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1

    def stop(self):
        pass


class Window:
    def __init__(self):
        self.visible = True

    def get_visible(self):
        return self.visible


class World:
    """Natter's memory decisions (app.py) on a computer of `total` bytes, with pages whose sizes
    the test sets (by account name), on a clock the test moves. Natter's own code, stand-in parts."""

    def __init__(self, accounts=("Main",), total=int(15.3 * GiB), loaded_hours_ago=6):
        from natter import app as app_module
        from natter.app import Natter
        from natter.memguard import page_limit

        self.module, self.clock = app_module, Clock()
        self.pages = {name: 1200 * MiB for name in accounts}
        self.tight, self.swapped, self.held = False, 0, None
        self.out = io.StringIO()

        class App:
            calling = Natter.calling
            _on_window_visible = Natter._on_window_visible
            _on_page_call = Natter._on_page_call
            _fresh_pages_for_memory = Natter._fresh_pages_for_memory
            _memory_reason = Natter._memory_reason
            _memory_wait = Natter._memory_wait
            _reclaim = Natter._reclaim
            _reclaim_wait = Natter._reclaim_wait
            _check_memory = Natter._check_memory

        app = self.app = App()
        app.accounts = [Page(self, name, self.clock.now - loaded_hours_ago * 3600) for name in accounts]
        app.window, app.reclaimer = Window(), Reclaimer()
        app.memory_guard = types.SimpleNamespace(limit=page_limit(total))
        app.settings = {"light_background": True}
        app._reclaim_timer, app._hidden_at, app._call_ended_at = 0, None, None
        app._call_held, app._after_call, app._memory_waiting = {}, {}, None
        app.call_sound = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
        for name in ("_refresh_tray", "_write_menu_chats", "_check_media", "_apply_page_sound"):
            setattr(app, name, lambda: None)
        app._measure_call = lambda on: None
        self.timers = Timers()
        self.patches = [mock.patch.object(app_module.time, "monotonic", self.clock),
                        mock.patch.object(app_module, "GLib", self.timers),
                        mock.patch.object(app_module, "page_sizes",
                                          lambda: {4000 + i: size for i, size in enumerate(self.pages.values())}),
                        mock.patch.object(app_module, "footprint", lambda: sum(self.pages.values()) + 300 * MiB),
                        mock.patch.object(app_module, "own_swap", lambda: self.swapped),
                        mock.patch.object(app_module, "memory_tight", lambda: self.tight),
                        contextlib.redirect_stdout(self.out)]

    def __enter__(self):
        for patch in self.patches:
            patch.__enter__()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.__exit__(*exc)

    def account(self, name="Main"):
        return next(account for account in self.app.accounts if account.name == name)

    def hide(self):
        self.app.window.visible = False
        self.app._on_window_visible(False)

    def show(self):
        self.app.window.visible = True
        self.app._on_window_visible(True)

    def run(self, minutes):
        """Natter's minute look (_check_memory), `minutes` times."""
        for _ in range(minutes):
            self.clock.minutes(1)
            self.app._check_memory()

    def fresh(self):
        return sum(len(account.fresh) for account in self.app.accounts)


class HiddenFreshPageTest(unittest.TestCase):
    """When a hidden page gets a fresh one for its memory (app.py _fresh_pages_for_memory)."""

    def test_closing_the_window_reloads_nothing_however_big_natter_is(self):
        # The friend's and the Den's case: the whole unit past the old 1836 MB, 391 MB in swap,
        # at the moment the window hid. The page itself is an ordinary one.
        with World() as world:
            world.pages["Main"] = 2400 * MiB
            world.swapped = 391 * MiB
            world.hide()
            self.assertEqual(world.fresh(), 0)
            world.run(8 * 60)  # a working day, hidden
            self.assertEqual(world.fresh(), 0)
            self.assertNotIn("a fresh page", world.out.getvalue())

    def test_what_the_reclaimer_put_in_swap_is_never_a_reason(self):
        # It stopped at its own budget, 1024 MB in swap, which then reloaded WhatsApp at the next
        # minute's look, and again every half hour (CachyOS, Ada; October 2026).
        from natter.system import SWAP_BUDGET_MB

        with World() as world:
            world.pages["Main"] = 2100 * MiB
            world.swapped = (SWAP_BUDGET_MB + 100) * MiB
            world.hide()
            world.run(4 * 60)
            self.assertEqual(world.fresh(), 0)

    def test_hiding_and_showing_all_day_reloads_nothing(self):
        with World() as world:
            world.pages["Main"] = 2500 * MiB
            for _ in range(48):
                world.hide()
                world.run(15)
                world.show()
                world.run(5)
            self.assertEqual(world.fresh(), 0)

    def test_a_page_past_the_guards_limit_gets_one_after_a_while_hidden(self):
        with World() as world:
            limit = world.app.memory_guard.limit
            world.pages["Main"] = limit + 300 * MiB
            world.hide()
            world.run(HIDDEN_BEFORE_FRESH // 60 - 1)
            self.assertEqual(world.fresh(), 0)  # not in the first minutes
            said = world.out.getvalue()
            self.assertIn("Main could do with a fresh page (a page holds 4216 MB, past the guard's limit of 3916 MB)",
                          said)
            self.assertIn("not yet: the window was hidden", said)
            self.assertEqual(said.count("could do with"), 1)  # said once, not every minute
            world.run(2)
            self.assertEqual(world.fresh(), 1)
            self.assertIn("natter: a fresh page for Main: a page holds 4216 MB, past the guard's limit of 3916 MB "
                          "(Natter Den holds 4516 MB, 0 MB of it in swap)", world.out.getvalue())

    def test_never_more_often_than_fresh_again(self):
        with World() as world:
            limit = world.app.memory_guard.limit
            world.pages["Main"] = limit + 300 * MiB
            world.hide()
            world.run(HIDDEN_BEFORE_FRESH // 60 + 1)
            self.assertEqual(world.fresh(), 1)
            first = world.account().fresh[0]
            world.run(30)
            world.pages["Main"] = limit + 500 * MiB  # past it again, half an hour later
            world.run(FRESH_AGAIN // 60 - 60)
            self.assertEqual(world.fresh(), 1)
            self.assertIn("not yet: its page loaded", world.out.getvalue())
            world.run(40)
            self.assertEqual(world.account().fresh, [first, first + FRESH_AGAIN])

    def test_a_page_that_just_loaded_waits(self):
        with World(loaded_hours_ago=1) as world:
            world.pages["Main"] = world.app.memory_guard.limit + 100 * MiB
            world.hide()
            world.run(2 * 60)
            self.assertEqual(world.fresh(), 0)
            world.run(2 * 60 + 1)
            self.assertEqual(world.fresh(), 1)

    def test_never_while_shown(self):
        with World() as world:
            world.pages["Main"] = world.app.memory_guard.limit * 2
            world.run(24 * 60)
            self.assertEqual(world.fresh(), 0)

    def test_never_during_or_just_after_a_call(self):
        with World() as world:
            world.pages["Main"] = world.app.memory_guard.limit + 100 * MiB
            world.hide()
            world.run(HIDDEN_BEFORE_FRESH // 60 - 1)
            main = world.account()
            main.calling = True
            world.app._on_page_call(main, True)
            world.run(60)
            self.assertEqual(world.fresh(), 0)
            main.calling = False
            world.app._on_page_call(main, False)
            world.run(QUIET_BEFORE_FRESH // 60 - 1)
            self.assertEqual(world.fresh(), 0)
            self.assertIn("not yet: a call ended a moment ago", world.out.getvalue())
            world.run(2)
            self.assertEqual(world.fresh(), 1)

    def test_never_during_a_reply_a_load_or_just_after_a_notification(self):
        for state in ("replying", "reloading", "not ready", "notified", "crashing"):
            with self.subTest(state), World() as world:
                world.pages["Main"] = world.app.memory_guard.limit + 100 * MiB
                main = world.account()
                world.hide()
                world.run(HIDDEN_BEFORE_FRESH // 60 - 1)
                if state == "replying":
                    main.replying = 1
                elif state == "reloading":
                    main.reloading = True
                elif state == "not ready":
                    main.ready = False
                elif state == "crashing":
                    main.crashing = True
                world.clock.minutes(1)
                if state == "notified":
                    main.notified_at = world.clock.now  # a message, or a call ringing
                world.run(QUIET_BEFORE_FRESH // 60 - 1)
                self.assertEqual(world.fresh(), 0)
                main.replying, main.reloading, main.ready, main.crashing = 0, False, True, False
                world.run(2)
                self.assertEqual(world.fresh(), 1)

    def test_the_computer_short_of_memory(self):
        with World() as world:
            world.hide()
            world.tight = True
            world.pages["Main"] = 1200 * MiB  # an idle page: a fresh one wouldn't help
            world.run(60)
            self.assertEqual(world.fresh(), 0)
            world.pages["Main"] = (MEMORY_LIMIT + 200) * MiB
            world.run(1)
            self.assertEqual(world.fresh(), 1)
            self.assertIn("a fresh page for Main: the computer is short of memory, and a page holds 1736 MB",
                          world.out.getvalue())

    def test_swap_alone_is_no_shortage(self):
        # Natter's swap past its budget, or the computer's past 60% (swap_full): not a reason.
        with World() as world:
            world.hide()
            world.pages["Main"] = (MEMORY_LIMIT + 400) * MiB
            world.swapped = 3 * GiB
            with mock.patch("natter.system.swap_share", return_value=0.95):
                world.run(6 * 60)
            self.assertEqual(world.fresh(), 0)

    def test_after_a_call_that_left_a_lot_behind(self):
        with World() as world:
            main = world.account()
            main.calling = True
            world.app._on_page_call(main, True)  # the call begins at 1200 MB
            world.pages["Main"] = 2300 * MiB
            main.calling = False
            world.app._on_page_call(main, False)
            world.hide()
            world.run(HIDDEN_BEFORE_FRESH // 60 - 1)
            self.assertEqual(world.fresh(), 0)
            world.run(2)
            self.assertEqual(world.fresh(), 1)
            self.assertIn("a fresh page for Main: its call left 1100 MB behind", world.out.getvalue())
            world.run(6 * 60)
            self.assertEqual(world.fresh(), 1)  # once

    def test_a_calls_leftovers_dont_wait_four_hours(self):
        # Asked for once a call and gone with the fresh page, so no loop to guard against; and the
        # guard's room above a page's limit after a call counts on it (memguard.py), on an 8 GB
        # computer most. A page past its limit for another reason still waits FRESH_AGAIN.
        with World(total=8 * GiB, loaded_hours_ago=1) as world:
            main, limit = world.account(), world.app.memory_guard.limit
            main.calling = True
            world.app._on_page_call(main, True)  # the call begins at 1200 MB
            world.pages["Main"] = limit + 300 * MiB  # past the page's limit too
            main.calling = False
            world.app._on_page_call(main, False)
            world.hide()
            world.run(HIDDEN_BEFORE_FRESH // 60 + 1)
            self.assertEqual(world.fresh(), 1)
            self.assertIn("a fresh page for Main: its call left 1660 MB behind", world.out.getvalue())
            world.pages["Main"] = limit + 300 * MiB  # past it again, no call since
            world.run(60)
            self.assertEqual(world.fresh(), 1)
            self.assertIn("not yet: its page loaded", world.out.getvalue())

    def test_after_a_call_that_left_little_behind(self):
        with World() as world:
            main = world.account()
            main.calling = True
            world.app._on_page_call(main, True)
            world.pages["Main"] = 1400 * MiB
            main.calling = False
            world.app._on_page_call(main, False)
            world.hide()
            world.run(6 * 60)
            self.assertEqual(world.fresh(), 0)
            self.assertEqual(world.out.getvalue().count("call left 200 MB behind; no fresh page"), 1)

    def test_a_page_loaded_since_the_call_needs_none(self):
        with World() as world:
            main = world.account()
            main.calling = True
            world.app._on_page_call(main, True)
            world.pages["Main"] = 2300 * MiB
            main.calling = False
            world.app._on_page_call(main, False)
            world.clock.minutes(1)
            main.loaded_at = world.clock.now  # it crashed and came back, say
            world.hide()
            world.run(6 * 60)
            self.assertEqual(world.fresh(), 0)

    def test_several_accounts_one_at_a_time_longest_loaded_first(self):
        # The guard can't tell whose page is whose: the next look tries the next account.
        with World(accounts=("Work", "Main")) as world:
            world.account("Main").loaded_at -= 3600
            limit = world.app.memory_guard.limit
            world.pages["Work"] = limit + 100 * MiB  # Work's page is the big one
            world.hide()
            world.run(HIDDEN_BEFORE_FRESH // 60)
            self.assertEqual((len(world.account("Main").fresh), len(world.account("Work").fresh)), (1, 0))
            world.run(1)
            self.assertEqual((len(world.account("Main").fresh), len(world.account("Work").fresh)), (1, 1))
            world.run(6 * 60)
            self.assertEqual(world.fresh(), 2)

    def test_a_reclaim_never_follows_a_fresh_load(self):
        with World(loaded_hours_ago=0) as world:
            world.hide()
            seconds, reclaim = world.timers.added[-1]
            self.assertEqual(seconds, HIDDEN_BEFORE_RECLAIM)
            world.clock.now += seconds
            reclaim()
            self.assertEqual(world.app.reclaimer.started, 0)  # the page loaded a moment ago
            world.run(SETTLE_BEFORE_RECLAIM // 60)
            world.account().replying = 1
            world.app._reclaim()
            self.assertEqual(world.app.reclaimer.started, 0)
            world.account().replying = 0
            world.app._reclaim()
            self.assertEqual(world.app.reclaimer.started, 1)
            self.assertEqual(world.fresh(), 0)


class ReclaimerToSwapTest(unittest.TestCase):
    """The Reclaimer, run on a stand-in unit until it stops at its own budget, then Natter's next
    looks: no fresh page (it made one each time before October 2026)."""

    def test_the_reclaimer_running_to_its_budget_reloads_nothing(self):
        from natter import system
        from natter.system import SWAP_BUDGET_MB, Reclaimer, own_swap

        with tempfile.TemporaryDirectory() as folder:
            unit = Path(folder)
            state = {"anon": 2100 * MiB, "file": 300 * MiB, "swap": 0}

            def write():
                (unit / "memory.current").write_text(str(state["anon"] + state["file"]))
                (unit / "memory.swap.current").write_text(str(state["swap"]))
                (unit / "memory.stat").write_text(f"anon {state['anon']}\nfile {state['file']}\n")

            def step():  # file cache first, then WhatsApp's memory to swap, 64 MB a step
                cache = min(max(state["file"] - 100 * MiB, 0), 64 * MiB)
                state["file"] -= cache
                state["anon"] -= 64 * MiB - cache
                state["swap"] += 64 * MiB - cache
                write()
                return True

            write()
            reclaimer = Reclaimer()
            reclaimer.cgroup, reclaimer._step = unit, step
            reclaimer._stop.wait = lambda _seconds: False
            with mock.patch.object(system, "swap_share", return_value=0.1), \
                    mock.patch.object(system, "memory_stalls", return_value=0.0):
                reclaimer._run()
            self.assertGreater(own_swap(unit), SWAP_BUDGET_MB * MiB)  # it stopped at its own budget
            with World() as world:
                world.pages["Main"] = 2000 * MiB  # resident and swapped: what the page still holds
                world.swapped = own_swap(unit)
                world.hide()
                world.run(3 * 60)
                self.assertEqual(world.fresh(), 0)


class HideAndThawTest(unittest.TestCase):
    def test_nothing_reloads_the_moment_the_window_hides(self):
        from natter.app import Natter

        calls = []
        fake = type("App", (), {})()
        fake._reclaim_timer, fake._hidden_at, fake.calling = 0, None, False
        fake._refresh_tray = lambda: None
        fake._write_menu_chats = lambda: None
        fake._fresh_pages_for_memory = lambda: calls.append("fresh") or False
        fake.settings = {"light_background": False}
        with mock.patch("natter.app.time.monotonic", return_value=500.0):
            Natter._on_window_visible(fake, False)
        self.assertEqual(calls, [])
        self.assertEqual(fake._hidden_at, 500.0)
        with mock.patch("natter.app.time.monotonic", return_value=900.0):
            Natter._on_window_visible(fake, False)  # again, as when a call ends with it hidden
        self.assertEqual(fake._hidden_at, 500.0)  # hidden since it hid

    def test_a_thaw_looks_again(self):
        from natter.app import Natter

        calls = []
        fake = type("App", (), {})()
        fake._fresh_pages_for_memory = lambda: calls.append("fresh") or False
        with mock.patch("natter.app.footprint", return_value=0), mock.patch("natter.app.own_swap", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            Natter._thawed(fake, 300.0, {}, {})
        self.assertEqual(calls, ["fresh"])

    def test_a_long_gap_means_a_thaw(self):
        from natter.app import THAW_GAP, Natter

        thawed = []
        fake = type("App", (), {})()
        fake._network_processes, fake._network_looks, fake._quitting = set(), 0, False
        fake._last_look = time.monotonic() - THAW_GAP - 5
        fake._memory_events = {}
        fake._note_oom_kills = lambda events, now: None
        fake._thawed = lambda stalled, before, after: thawed.append(stalled)
        fake.accounts = []
        with mock.patch("natter.app.network_processes", return_value=set()), \
                mock.patch("natter.app.memory_events", return_value={}):
            Natter._check_network_process(fake)
            Natter._check_network_process(fake)  # right after: no gap
        self.assertEqual(len(thawed), 1)
        self.assertGreater(thawed[0], THAW_GAP)


class KillThresholdTest(unittest.TestCase):
    """WebKit's own kill doesn't know about calls: it comes only after the guard's, call and all."""

    def test_after_the_guard_in_a_call(self):
        from natter.app import KILL_ROOM, MEMORY_KILL, kill_threshold
        from natter.memguard import page_ceiling, page_limit, page_limits

        for total in (4 * GiB, int(7.6 * GiB), 8 * GiB, int(15.3 * GiB), 16 * GiB, 64 * GiB, 0):
            for limit in (MEMORY_LIMIT, 1024, 4096):
                fraction = kill_threshold(limit, total)
                _soft, hard = page_limits(page_limit(total), page_ceiling(total), True)
                self.assertGreaterEqual(fraction, MEMORY_KILL)
                self.assertGreaterEqual(fraction * limit * MiB, hard + KILL_ROOM * MiB, (total, limit))
        self.assertEqual(kill_threshold(4096, 8 * GiB), MEMORY_KILL)  # already past it: as it was

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
