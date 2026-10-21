"""The memory guard wired into Natter Den (app.py, account.py): a page it or the kernel ended is
reloaded once, outside the crash count, and stays down after a second; the unit's limits follow
the accounts; and the log says what was attached and what a stall cost, never a name."""

import contextlib
import inspect
import io
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import WebKit

from natter import NAME
from natter import account as natter_account
from natter import app as natter_app
from natter.account import MEMORY_AGAIN, Account, attach_line
from natter.app import Natter, stall_line

Reason = WebKit.WebProcessTerminationReason


class Timers:
    """GLib's timeouts, as far as the crash path uses them: recorded, never run."""

    SOURCE_REMOVE = False

    def __init__(self):
        self.added, self.removed = [], []

    def timeout_add_seconds(self, seconds, run):
        self.added.append((seconds, run))
        return len(self.added)

    def source_remove(self, timer):
        self.removed.append(timer)


class Page:
    """An Account as its crash path sees it: Account's own code, on stand-in parts."""

    def __init__(self, ended_by=None):
        self.ended_by = list(ended_by or [])
        self.app = SimpleNamespace(ended_for_memory=lambda: self.ended_by.pop(0) if self.ended_by else None)
        self.name, self.home, self.active_chat = "Personal", "https://web.whatsapp.com/", ""
        self.ready, self.removed, self.rtc = True, False, None
        self.crashing = self.memory_stopped = self.reloading = False
        self.fresh_at, self._reload_timer = 0.0, 0
        self._crashes, self._memory_ends, self._pending = [], [], {}
        self.emitted = []

    def __getattr__(self, name):
        return types.MethodType(getattr(Account, name), self)

    def emit(self, *signal):
        self.emitted.append(signal)

    def _watch_page(self, *_args):
        pass

    def _end_link(self):
        pass


class MemoryEndTest(unittest.TestCase):
    def setUp(self):
        self.timers = Timers()
        patch = mock.patch.object(natter_account, "GLib", self.timers)
        patch.start()
        self.addCleanup(patch.stop)
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_a_guard_kill_reloads_after_a_second_outside_the_crash_count(self):
        page = Page(ended_by=["the guard"])
        page._on_crash(None, Reason.CRASHED)
        self.assertEqual([seconds for seconds, _run in self.timers.added], [1])
        self.assertEqual(page._crashes, [])
        self.assertTrue(page.reloading)
        self.assertFalse(page.crashing)
        self.assertGreater(page.fresh_at, 0)
        self.assertEqual(page.emitted, [("state-changed",), ("memory-ended", False)])
        self.assertIn("ended for its memory (the guard); a fresh page", self.out.getvalue())

    def test_a_second_within_ten_minutes_stays_down(self):
        page = Page(ended_by=["the guard", "the kernel"])
        page._on_crash(None, Reason.CRASHED)
        page._on_crash(None, Reason.CRASHED)
        self.assertEqual(len(self.timers.added), 1)  # the first one's reload only
        self.assertTrue(page.crashing and page.memory_stopped)
        self.assertFalse(page.reloading)
        self.assertEqual(page.emitted[-2:], [("state-changed",), ("memory-ended", True)])
        self.assertIn("again within 10 minutes; not reloading", self.out.getvalue())

    def test_one_long_after_the_last_is_reloaded_again(self):
        page = Page(ended_by=["the guard"])
        page._memory_ends = [natter_account.time.monotonic() - MEMORY_AGAIN - 1]
        page._on_crash(None, Reason.CRASHED)
        self.assertFalse(page.crashing)
        self.assertEqual(len(self.timers.added), 1)

    def test_webkits_own_kill_counts_as_one_of_the_two(self):
        page = Page(ended_by=["the guard"])
        page._on_crash(None, Reason.EXCEEDED_MEMORY_LIMIT)  # WebKit doesn't ask the guard
        self.assertEqual(page.ended_by, ["the guard"])
        self.assertFalse(page.crashing)
        page._on_crash(None, Reason.CRASHED)
        self.assertTrue(page.memory_stopped)

    def test_a_crash_nobody_claims_is_a_crash(self):
        page = Page()
        page._on_crash(None, Reason.CRASHED)
        self.assertEqual(len(page._crashes), 1)
        self.assertEqual(page._memory_ends, [])
        self.assertNotIn(("memory-ended", False), page.emitted)

    def test_ended_by_natter_itself_is_not_asked_about(self):
        page = Page(ended_by=["the guard"])
        page._on_crash(None, Reason.TERMINATED_BY_API)  # a fresh page, a removed account
        self.assertEqual(page.ended_by, ["the guard"])
        self.assertEqual(page.emitted, [])

    def test_try_again_starts_over(self):
        page = Page(ended_by=["the guard", "the guard"])
        page._on_crash(None, Reason.CRASHED)
        page._on_crash(None, Reason.CRASHED)
        page._load = lambda uri: None
        page.reload()
        self.assertFalse(page.crashing or page.memory_stopped)
        self.assertEqual(page._memory_ends, [])


class StaysDownTest(unittest.TestCase):
    """A page that stopped stays down until Try Again: what else would load it again doesn't."""

    def stopped(self):
        page = Page()
        page.crashing = page.memory_stopped = True
        page.calling, page.view = False, SimpleNamespace(terminate_web_process=lambda: self.fail("loaded again"))
        page._load = lambda uri: self.fail("loaded again")
        return page

    def test_no_fresh_page(self):
        page = self.stopped()
        page.fresh_page()  # after a call while hidden, a stale grant, memory while hidden
        self.assertTrue(page.crashing)

    def test_no_reload_for_a_switch_turned_on(self):
        page = self.stopped()
        page.refused_since_load = {"camera": True, "microphone": False}
        page.reload = lambda: self.fail("reloaded")
        page.media_allowed_again("camera")


class EndedForMemoryTest(unittest.TestCase):
    def setUp(self):
        self.now, self.events, self.own = 1000.0, {}, True
        for name, value in (("memory_events", lambda: dict(self.events)), ("in_own_scope", lambda: self.own)):
            patch = mock.patch.object(natter_app, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(natter_app.time, "monotonic", lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)

    def app(self, guard_claims=False, oom_kills=0):
        fake = type("App", (), {})()
        fake.memory_guard = SimpleNamespace(recent_kill=lambda: guard_claims)
        fake._oom_kills, fake._oom_unclaimed = oom_kills, []
        for name in ("_unit_events", "_note_oom_kills"):
            setattr(fake, name, types.MethodType(getattr(Natter, name), fake))
        return fake

    def look(self, fake):
        """What _check_network_process does every few seconds with the counts."""
        fake._note_oom_kills(natter_app.memory_events() if natter_app.in_own_scope() else {}, self.now)

    def test_the_guards_kill(self):
        self.assertEqual(Natter.ended_for_memory(self.app(guard_claims=True)), "the guard")
        self.assertIsNone(Natter.ended_for_memory(self.app()))

    def test_each_kernel_kill_is_claimed_once(self):
        fake = self.app(oom_kills=1)
        self.events = {"oom_kill": 3}
        self.assertEqual(Natter.ended_for_memory(fake), "the kernel")
        self.assertEqual(Natter.ended_for_memory(fake), "the kernel")
        self.assertIsNone(Natter.ended_for_memory(fake))

    def test_a_kill_no_page_claims_is_forgotten(self):
        # The kernel ended the page and the sandbox anchor (run k1): the page claims one, and the
        # other mustn't be pinned on an ordinary crash hours later.
        fake = self.app()
        self.events = {"oom_kill": 2}
        self.assertEqual(Natter.ended_for_memory(fake), "the kernel")
        self.now += natter_app.KILL_MEMORY + 5
        self.look(fake)
        self.assertEqual(fake._oom_unclaimed, [])
        self.now += 6 * 3600
        self.assertIsNone(Natter.ended_for_memory(fake))

    def test_a_kill_first_seen_by_a_look_is_still_claimed_soon_after(self):
        fake = self.app()
        self.events = {"oom_kill": 1}
        self.look(fake)  # the network process's look comes first
        self.now += 30  # the page's crash reported late: a stalled main loop
        self.assertEqual(Natter.ended_for_memory(fake), "the kernel")

    def test_kills_seen_by_looks_alone_go_too(self):
        fake = self.app()
        self.events = {"oom_kill": 1}  # WebKit's network process: its page ends by Natter's hand, not a crash
        self.look(fake)
        self.now += natter_app.KILL_MEMORY + 1
        self.look(fake)
        self.assertIsNone(Natter.ended_for_memory(fake))

    def test_only_natters_own_unit(self):
        # In a terminal's or a session's cgroup the counts are other programs' too.
        fake = self.app()
        self.own, self.events = False, {"oom_kill": 4}
        self.look(fake)
        self.assertIsNone(Natter.ended_for_memory(fake))

    def test_counted_first_in_another_cgroup(self):
        # Natter's own scope took over after the first count: its count starts at 0.
        fake = self.app(oom_kills=7)
        self.events = {"oom_kill": 0}
        self.look(fake)
        self.events = {"oom_kill": 1}
        self.assertEqual(Natter.ended_for_memory(fake), "the kernel")

    def test_no_guard(self):
        fake = self.app()
        fake.memory_guard = None
        self.assertIsNone(Natter.ended_for_memory(fake))


class SayingSoTest(unittest.TestCase):
    BODY = f"It won't reload on its own. Open {NAME} and choose Try Again."

    def app(self, active=True, accounts=1, locked=False, shown=None):
        fake = type("App", (), {})()
        fake.toasts, fake.told, fake.withdrawn = [], [], []
        fake.window = SimpleNamespace(is_active=lambda: active, account=shown,
                                      toast=lambda title, *args, **kwargs: fake.toasts.append((title, *args)))
        fake.tell = lambda key, title, body="", button=None, account=None: fake.told.append((key, title, body, account))
        fake.withdraw_notification = fake.withdrawn.append
        fake.accounts = [SimpleNamespace(name=f"Account {n}") for n in range(accounts)]
        fake.locked, fake._memory_told = locked, set()
        return fake

    def test_a_toast_while_its_used(self):
        account = SimpleNamespace(id="main", name="Personal")
        fake = self.app(shown=account)
        Natter._on_memory_ended(fake, account, False)
        Natter._on_memory_ended(fake, account, True)
        self.assertEqual(fake.toasts, [("WhatsApp used too much memory and was reloaded",),
                                       ("WhatsApp used too much memory and was stopped",)])
        self.assertEqual(fake.told, [])

    def test_several_accounts_name_it_and_lead_there(self):
        work = SimpleNamespace(id="work", name="Work")
        fake = self.app(accounts=2, shown=SimpleNamespace(id="home"))
        Natter._on_memory_ended(fake, work, False)
        (title, button, action, target), = fake.toasts
        self.assertEqual((title, button, action, target.get_string()),
                         ("WhatsApp used too much memory and was reloaded (Work)", "Show", "app.show-account", "work"))
        fake = self.app(accounts=2, shown=work)  # already on screen: nowhere to go
        Natter._on_memory_ended(fake, work, False)
        self.assertEqual(fake.toasts, [("WhatsApp used too much memory and was reloaded (Work)",)])

    def test_unseen_only_a_stop_is_told(self):
        # Hidden, minimized, behind other windows (not active) or locked: a toast would go unseen.
        account = SimpleNamespace(id="main", name="Personal")
        for fake in (self.app(active=False), self.app(locked=True)):
            Natter._on_memory_ended(fake, account, False)
            self.assertEqual((fake.toasts, fake.told), ([], []))
            Natter._on_memory_ended(fake, account, True)
            self.assertEqual(fake.toasts, [])
            self.assertEqual(fake.told, [("memory-main", "WhatsApp used too much memory", self.BODY, account)])

    def test_the_notification_goes_once_its_loaded_again(self):
        account = SimpleNamespace(id="main", name="Personal", memory_stopped=True)
        fake = self.app(active=False)
        fake._show_unread = lambda: None
        fake._linking, fake.window.get_visible = set(), lambda: False
        Natter._on_memory_ended(fake, account, True)
        account.ready = account.needs_link = False
        account.look = None
        fake._show_style_hint, fake.calls = lambda: None, None
        Natter._on_account_state(fake, account)  # still stopped
        self.assertEqual(fake.withdrawn, [])
        account.memory_stopped = False  # Try Again
        Natter._on_account_state(fake, account)
        self.assertEqual(fake.withdrawn, ["memory-main"])
        Natter._on_account_state(fake, account)
        self.assertEqual(fake.withdrawn, ["memory-main"])

    def test_the_notification_opens_that_account(self):
        sent = []
        fake = type("App", (), {})()
        fake.send_notification = lambda key, notification: sent.append((key, notification))
        with mock.patch.object(natter_app, "app_icon", return_value=natter_app.Gio.ThemedIcon.new("x")):
            Natter.tell(fake, "memory-work", "WhatsApp used too much memory", account=SimpleNamespace(id="work"))
        self.assertEqual(sent[0][0], "memory-work")
        self.assertIn('set_default_action_and_target("app.show-account", GLib.Variant("s", account.id))',
                      inspect.getsource(Natter.tell))
        self.assertIn('Gio.SimpleAction.new("show-account", GLib.VariantType("s"))', inspect.getsource(Natter.do_startup))
        # A button too (a download's Open): PyGObject's names, not C's.
        Natter.tell(fake, "download-x", "Saved", "", ("Open", "app.open-file", natter_app.GLib.Variant("s", "/x")))
        self.assertEqual(sent[1][0], "download-x")

    def test_show_account_waits_for_the_unlock(self):
        shown = []
        fake = type("App", (), {})()
        fake.accounts = [SimpleNamespace(id="work")]
        fake.account_by_id = types.MethodType(Natter.account_by_id, fake)
        fake.present_window = lambda: True
        fake.window = SimpleNamespace(show_account=shown.append)
        fake._held_while_locked = lambda run: False
        Natter.open_account(fake, "work")
        Natter.open_account(fake, "gone")
        self.assertEqual(shown, fake.accounts)
        fake._held_while_locked = lambda run: True
        Natter.open_account(fake, "work")
        self.assertEqual(shown, fake.accounts)

    def test_removing_the_account_withdraws_it(self):
        self.assertIn('self.withdraw_notification(f"memory-{account.id}")', inspect.getsource(Natter.remove_account))


class WiringTest(unittest.TestCase):
    """Where app.py starts, stops and sizes things: read from its source, as the order matters."""

    def source(self, function):
        return inspect.getsource(function)

    def test_the_guard_starts_once_the_web_context_is_made(self):
        startup = self.source(Natter.do_startup)
        made, guard = startup.index("self._make_web_context()"), startup.index("MemoryGuard(page_limit(total)")
        self.assertLess(made, guard)
        self.assertIn("calling=lambda: self.calling", startup)
        self.assertIn("ceiling=page_ceiling(total)", startup)
        self.assertLess(guard, startup.index("self.memory_guard.start()"))

    def test_the_guard_stops_on_quitting(self):
        self.assertIn("self.memory_guard.stop()", self.source(Natter.quit_natter))

    def test_the_units_limits_follow_the_accounts(self):
        startup = self.source(Natter.do_startup)
        self.assertIn("move_to_own_scope(accounts=accounts)", startup)
        self.assertIn("limit_own_scope(accounts)", startup)
        for change in (Natter.add_account, Natter.remove_account):
            self.assertIn("limit_own_scope(len(self.accounts))", self.source(change))

    def test_the_guard_asks_a_call_through_the_pages(self):
        fake = type("App", (), {})()
        fake.accounts = [SimpleNamespace(calling=False), SimpleNamespace(calling=True)]
        self.assertTrue(Natter.calling.fget(fake))


class AttachLogTest(unittest.TestCase):
    def test_counts_sizes_and_types_never_names(self):
        with tempfile.TemporaryDirectory() as folder:
            video, note = Path(folder) / "secret-name.mp4", Path(folder) / "private plans.pdf"
            video.write_bytes(b"\0" * (3 * 2 ** 20))
            note.write_bytes(b"%PDF-1.4\n" + b"\0" * (2 ** 19))
            line = attach_line([video, note, video], "drop")
        self.assertTrue(line.startswith("natter: attach: 3 files, 6.5 MB, "), line)
        self.assertIn("video/mp4", line)
        self.assertIn("application/pdf", line)
        self.assertEqual(line.count("video/mp4"), 1)
        self.assertTrue(line.endswith(", via drop"))
        for private in ("secret", "name", "private", "plans", folder):
            self.assertNotIn(private, line)

    def test_one_file_that_went_away(self):
        self.assertEqual(attach_line([Path("/nonexistent/holiday.png")], "share"),
                         "natter: attach: 1 file, 0.0 MB, image/png, via share")

    def test_attach_logs_before_anything_else(self):
        page = SimpleNamespace(run=lambda *args: None, _picked=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            Account.attach(page, [Path("/nonexistent/clip.webm")], None, "paste")
        self.assertEqual(out.getvalue().strip(), "natter: attach: 1 file, 0.0 MB, video/webm, via paste")

    def test_every_way_in_says_how(self):
        self.assertIn('self.send_files(paths, "paste")', inspect.getsource(Natter.paste))
        self.assertIn('self.go_to_chat(files, how="share")', inspect.getsource(Natter.do_command_line))
        self.assertIn("account.attach(paths, done, how)", inspect.getsource(Natter.send_files))
        self.assertIn("self.go_to_chat(paths, how=how)", inspect.getsource(Natter.send_files))
        from natter.window import Window

        self.assertIn('self.app.send_files(paths, "drop")', inspect.getsource(Window._on_drop))

    def test_send_to_passes_it_on(self):
        attached = []
        account = SimpleNamespace(open_chat=lambda name, opened: opened(True),
                                  attach=lambda files, done, how: attached.append((files, how)))
        fake = type("App", (), {})()
        fake._held_while_locked = lambda run: False
        fake.present_window = lambda: True
        fake.window = SimpleNamespace(show_account=lambda a: None, toast=lambda *a: None)
        Natter.go_to(fake, account, "Ana", [Path("/tmp/x.png")], "share")
        self.assertEqual(attached, [([Path("/tmp/x.png")], "share")])


class StallLineTest(unittest.TestCase):
    def test_says_what_the_kernel_did_meanwhile(self):
        before = {"low": 0, "high": 10, "max": 2, "oom": 0, "oom_kill": 0}
        after = {"low": 0, "high": 130, "max": 2, "oom": 1, "oom_kill": 1}
        self.assertEqual(stall_line(34.4, 2110 << 20, 1150 << 20, before, after),
                         "natter: main loop stalled 34 s; 2110 MB held, 1150 MB of it in swap; "
                         "memory.events high +120, max +0, oom_kill +1")

    def test_without_its_counts(self):
        self.assertEqual(stall_line(21, 900 << 20, 0, {}, {}),
                         "natter: main loop stalled 21 s; 900 MB held, 0 MB of it in swap")
        # Counts first read after the gap: nothing to compare with, so nothing is made up.
        self.assertTrue(stall_line(21, 0, 0, {}, {"high": 5}).endswith("memory.events high +0"))

    def test_the_gap_hands_over_both_counts(self):
        thawed = []
        fake = type("App", (), {})()
        fake._network_processes, fake._network_looks, fake._quitting = set(), 0, False
        fake._last_look = natter_app.time.monotonic() - natter_app.THAW_GAP - 5
        fake._memory_events = {"high": 1}
        fake._thawed = lambda *args: thawed.append(args)
        fake._note_oom_kills = lambda events, now: None
        fake.accounts = []
        with mock.patch.object(natter_app, "network_processes", return_value=set()), \
                mock.patch.object(natter_app, "memory_events", return_value={"high": 9}):
            Natter._check_network_process(fake)
        self.assertEqual(thawed[0][1:], ({"high": 1}, {"high": 9}))
        self.assertEqual(fake._memory_events, {"high": 9})


class MemoryEventsTest(unittest.TestCase):
    def test_reads_the_units_counts(self):
        from natter.system import memory_events

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "memory.events").write_text("low 0\nhigh 12\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n")
            self.assertEqual(memory_events(Path(folder)),
                             {"low": 0, "high": 12, "max": 3, "oom": 1, "oom_kill": 1, "oom_group_kill": 0})
            self.assertEqual(memory_events(Path(folder) / "gone"), {})


if __name__ == "__main__":
    unittest.main()
