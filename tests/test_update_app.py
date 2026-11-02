"""Updates as the app and Preferences wire them (app.py, prefs.py): the banner's order, the tray's
item, the debug info's line, the update actions and when start-up and the minute reach Updates,
Update's guards before and after the download, and Preferences' Updates group. Updates itself
(update.py, tests/test_update.py) is stood in for, so no transport is ever made here, let alone
one that would reach GitHub."""

import io
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import GLib

from natter import NAME, VERSION, prefs, update
from natter.app import Natter, banner_for, debug_info
from natter.calls import CallWindow
from natter.instance import Taken
from natter.settings import Settings

import test_polish_prefs as prefs_stand_ins
from test_instance import CommandLine, Starting, spin, startup_app
from test_polish_app import Alert, fake_app, widgets

LINE = "Updates: get.sh copy, updates in place, checks on, newest none found, checked never"
AVAILABLE = (f"{NAME} 9.9 is available", "Update", "app.update", False)
DOWNLOADING = (f"Downloading {NAME} 9.9…", "", "", True)
FAILED = (f"Couldn't update to {NAME} 9.9", "Details", "app.update", True)


class Updates:
    """Stands in for update.Updates: what it shows is set by the test, and every other call made
    on it is kept (prepare, hold, launch, stop, started, tick, changed, check_now, open_notes)."""

    def __init__(self, **values):
        self.calls = []
        self.working, self.can_update, self.channel = False, True, "get"
        self.shown, self.item, self.line = None, None, ("Not checked yet", "check")
        self.__dict__.update(values)

    def banner(self):
        return self.shown

    def menu_item(self):
        return self.item

    def status(self):
        return self.line

    def debug_line(self):
        return LINE

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args: self.calls.append((name, *args))

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Fake:
    """Records the methods called on it, by name (in `made`: an app's `calls` is its call window)."""

    def __init__(self, **values):
        self.made = []
        self.__dict__.update(values)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.made.append((name, *args))

    def called(self, name):
        return [call[1:] for call in self.made if call[0] == name]


def account(**values):
    """An account whose page is up, quiet, as update_now's guards see it."""
    return SimpleNamespace(**{"ready": True, "needs_link": False, "failed": None, "crashing": False,
                              "recording": False, "replying": 0, "loaded_at": time.monotonic(), **values})


# ---- The banner, the tray, the debug info --------------------------------------------------------


class BannerTest(unittest.TestCase):
    def test_order(self):
        # Paused notifications, a restart, an update under way or failed, media, a release that's out.
        self.assertEqual(banner_for(0, False, 1000.0, update=AVAILABLE), (*AVAILABLE[:3], ""))
        self.assertEqual(banner_for(0, True, 1000.0, update=AVAILABLE)[2], "app.preferences-page")
        self.assertEqual(banner_for(0, True, 1000.0, update=FAILED), (*FAILED[:3], ""))
        self.assertEqual(banner_for(0, True, 1000.0, restart=True, update=FAILED)[2], "app.restart")
        self.assertEqual(banner_for(-1, True, 1000.0, restart=True, update=FAILED)[0], "Notifications paused")
        self.assertEqual(banner_for(0, False, 1000.0, update=DOWNLOADING), (DOWNLOADING[0], "", "", ""))  # no button
        self.assertIsNone(banner_for(0, False, 1000.0, update=None))

    def test_positional_calls_as_before(self):
        self.assertEqual(banner_for(0, True, 1000.0)[2], "app.preferences-page")
        self.assertEqual(banner_for(0, False, 1000.0, True)[2], "app.restart")
        self.assertIsNone(banner_for(0, False, 1000.0))

    def test_the_window_shows_updates_banner(self):
        fake = fake_app(self)
        fake.media_lost = False
        fake.updates = Updates(shown=AVAILABLE)
        Natter._show_banner(fake)
        self.assertEqual(fake.window.called("set_banner"), [(*AVAILABLE[:3], None)])
        self.assertEqual(fake.banner_shown, AVAILABLE[0])  # Updates tells a release once its banner is up
        fake.settings["dnd_until"] = -1
        Natter._show_banner(fake)
        self.assertEqual(fake.banner_shown, "Notifications paused")
        fake.settings["dnd_until"] = 0
        fake.updates.shown = None
        Natter._show_banner(fake)
        self.assertEqual(fake.window.called("hide_banner"), [()])
        self.assertEqual(fake.banner_shown, "")

    def test_what_changes_reaches_the_banner_tray_and_preferences(self):
        events = []
        app = SimpleNamespace(_show_banner=lambda: events.append("banner"), _refresh_tray=lambda: events.append("tray"),
                              preferences=None)
        Natter._updates_changed(app)
        app.preferences = SimpleNamespace(update_status=lambda: events.append("preferences"))
        Natter._updates_changed(app)
        self.assertEqual(events, ["banner", "tray", "banner", "tray", "preferences"])

    def test_update_settings_reach_updates(self):
        # Whatever wrote one (Updates itself, or Preferences' switch): the surfaces follow.
        app = SimpleNamespace(updates=Updates(), window=None)
        Natter._on_setting(app, "update_latest", "v9.9")
        Natter._on_setting(app, "update_check", False)
        self.assertEqual(app.updates.called("changed"), [(), ()])
        app.updates = None  # a search's start: no window, nothing to show it
        Natter._on_setting(app, "update_latest", "")


class TrayTest(unittest.TestCase):
    def menu(self, item):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        fake = type("App", (), {})()
        fake.settings = Settings(Path(folder.name) / "settings.json")
        fake.window, fake.accounts, fake.locked, fake.lock_enabled, fake.tray = None, [], False, False, None
        fake._from_tray = types.MethodType(Natter._from_tray, fake)
        fake._dnd_items = lambda: []
        fake.present_window = fake.quit_natter = lambda: None
        fake.shown = []
        fake.show_update = lambda: fake.shown.append("dialog")
        fake.updates = Updates(item=item)
        return fake, Natter._tray_menu(fake)

    def test_the_item_comes_and_goes(self):
        fake, items = self.menu(f"Update to {NAME} 9.9…")
        self.assertEqual([(item.label, item.separator) for item in items],
                         [(f"Show {NAME}", False), ("Turn On Privacy Blur", False), (f"Update to {NAME} 9.9…", False),
                          ("", True), (f"Quit {NAME}", False)])
        items[2].run()
        self.assertEqual(fake.shown, ["dialog"])
        _fake, items = self.menu(None)
        self.assertNotIn(f"Update to {NAME} 9.9…", [item.label for item in items])


class DebugInfoTest(unittest.TestCase):
    def test_the_line_only_when_given(self):
        self.assertNotIn("Updates:", debug_info("the browser", [], False))
        self.assertIn(f"\n{LINE}\n", debug_info("the browser", [], False, updates=LINE))

    def test_about_gives_it(self):
        fake = fake_app(self)
        fake.calls_in_page, fake.calls, fake._media = False, None, ([], "")
        fake.updates = Updates()
        with widgets():
            Natter.show_about(fake)
        about, = Alert.made
        (info,), = about.called("set_debug_info")
        self.assertIn(LINE, info)


# ---- The actions, start-up and the minute ---------------------------------------------------------


class ActionsTest(Starting):
    def test_made_off_and_waiting_for_start_up(self):
        app = startup_app(waits=("scope",), updates=Updates())
        actions = {}
        app.add_action = lambda action: actions.update({action.get_name(): action})
        shown = []
        app.show_update = lambda: shown.append("dialog")
        self.startup(app, Taken(True, "taken"))
        spin()
        # Anything on the bus can call an action: off until Updates has something to say.
        self.assertEqual([actions[name].get_enabled() for name in ("update", "update-notes")], [False, False])
        actions["update"].set_enabled(True)
        actions["update-notes"].set_enabled(True)
        actions["update"].activate(None)
        actions["update-notes"].activate(None)
        self.assertEqual((shown, app.updates.called("open_notes")), ([], []))  # start-up isn't done
        app.go["scope"]()
        spin()
        self.assertEqual((shown, app.updates.called("open_notes")), (["dialog"], [()]))

    def test_started_once_what_waited_has_run(self):
        # The window a launch asked for is up first: a toast there says Natter Den was updated.
        events = []
        app = startup_app(waits=("scope",), updates=Updates(started=lambda: events.append("updates")))
        app._command_line = lambda line: events.append(line.name) or line.status
        self.startup(app, Taken(True, "taken"))
        spin()
        Natter.do_command_line(app, CommandLine("first"))
        app.go["scope"]()
        spin()
        self.assertEqual(events, ["first", "updates"])


class MinuteTest(unittest.TestCase):
    def make(self, updates):
        return SimpleNamespace(settings={"dnd_until": 0}, updates=updates, _apply_page_sound=lambda: None)

    def test_ticks(self):
        app = self.make(Updates())
        self.assertEqual(Natter._minute(app), GLib.SOURCE_CONTINUE)
        self.assertEqual(app.updates.called("tick"), [()])
        self.assertEqual(Natter._minute(self.make(None)), GLib.SOURCE_CONTINUE)  # no window: no Updates

    def test_a_tick_that_fails_keeps_the_minute(self):
        def broken():
            raise RuntimeError("no network monitor")

        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(Natter._minute(self.make(Updates(tick=broken))), GLib.SOURCE_CONTINUE)
        self.assertIn("no network monitor", err.getvalue())


# ---- Update (app.update's dialog, update_now) -----------------------------------------------------


class ShowUpdateTest(unittest.TestCase):
    def make(self, **values):
        app = Fake(**{"updates": Updates(), "locked": False, "held": [], "window": Fake(), **values})
        app.show_update = types.MethodType(Natter.show_update, app)
        app._held_while_locked = lambda run, key="show": app.locked and (app.held.append((key, run)) or True)
        app.present_window = lambda: True
        return app

    def test_the_dialog_on_the_window(self):
        app = self.make()
        alert = Fake()
        with mock.patch("natter.app.dialog_for", lambda on: alert if on is app else None):
            Natter.show_update(app)
        self.assertEqual(alert.called("present"), [(app.window,)])

    def test_past_the_passcode(self):
        app = self.make(locked=True)
        with mock.patch("natter.app.dialog_for", lambda on: self.fail("shown over the lock")):
            Natter.show_update(app)
        self.assertEqual([key for key, _run in app.held], ["update"])

    def test_nothing_while_one_is_under_way(self):
        app = self.make(updates=Updates(working=True))
        with mock.patch("natter.app.dialog_for", lambda on: self.fail("asked again")):
            Natter.show_update(app)

    def test_nothing_to_show(self):
        app = self.make()
        with mock.patch("natter.app.dialog_for", lambda on: None):
            Natter.show_update(app)
        app.present_window = lambda: False  # no window to show it in
        with mock.patch("natter.app.dialog_for", lambda on: self.fail("no window")):
            Natter.show_update(app)


class UpdateNowTest(unittest.TestCase):
    """update_now's guards, at the click and again once the release is ready, as RestartTest
    (test_reply.py) has restart's."""

    def make(self, **values):
        toasts, told = [], []
        app = Fake(**{"updates": Updates(), "calling": False, "calls": None, "locked": False, "replies": {},
                      "reply_drafts": {}, "accounts": [account()], "toasts": toasts, "told": told, "_update_wait": 0,
                      "_quitting": False, "presented": [],
                      "held": [], "window": SimpleNamespace(get_visible=lambda: True,
                                                            toast=lambda *args, **kw: toasts.append(args)),
                      **values})
        for name in ("update_now", "_update_prepared", "_update_held", "_say_update_held", "_update_after_reply",
                     "_ask_update_replies"):
            setattr(app, name, types.MethodType(getattr(Natter, name), app))
        app._held_while_locked = lambda run, key="show": app.locked and (app.held.append((key, run)) or True)
        app.present_window = lambda: True
        app.tell = lambda *args, **kw: told.append((args, kw))
        return app

    def prepared(self, app):
        """update_now as far as Updates.prepare: what it hands to say it's ready."""
        Natter.update_now(app)
        (then,), = app.updates.called("prepare")
        return then

    def test_only_when_updates_can(self):
        # No window (a search's start), a copy that isn't get.sh's, one already under way.
        for index, updates in enumerate((None, Updates(can_update=False), Updates(working=True))):
            with self.subTest(index=index):
                app = self.make(updates=updates)
                Natter.update_now(app)
                self.assertEqual(updates.called("prepare") if updates else [], [])
        app = self.make(_quitting=True)
        Natter.update_now(app)
        self.assertEqual(app.updates.called("prepare"), [])

    def test_not_during_a_call(self):
        title = f"Update {NAME} once the call ends"
        for values in ({"calling": True}, {"calls": SimpleNamespace(in_call=True, ringing=False)},
                       {"calls": SimpleNamespace(in_call=False, ringing=True)}):  # a page's, the call window's, ringing there
            with self.subTest(values=values):
                app = self.make(**values)
                Natter.update_now(app)
                self.assertEqual((app.updates.called("prepare"), app.toasts), ([], [(title,)]))

    def test_out_of_sight_a_notification_says_so(self):
        app = self.make(calling=True, window=SimpleNamespace(get_visible=lambda: False))
        Natter.update_now(app)
        self.assertEqual(app.told, [(("update", f"Update {NAME} once the call ends"),
                                     {"button": ("Update", "app.update", None)})])

    def test_not_while_a_page_loads(self):
        app = self.make(accounts=[account(), account(ready=False)])
        Natter.update_now(app)
        self.assertEqual((app.updates.called("prepare"), app.toasts),
                         ([], [(f"Update {NAME} once WhatsApp has loaded",)]))
        # A link screen or a page that's offline isn't loading: neither is coming.
        app = self.make(accounts=[account(ready=False, needs_link=True), account(ready=False, failed="offline")])
        Natter.update_now(app)
        self.assertEqual(len(app.updates.called("prepare")), 1)

    def test_not_while_recording(self):
        app = self.make(accounts=[account(recording=True)])
        Natter.update_now(app)
        self.assertEqual((app.updates.called("prepare"), app.toasts),
                         ([], [(f"Update {NAME} once you've stopped recording",)]))

    def test_after_a_reply_being_sent(self):
        for sending in ({"accounts": [account(replying=1)]},
                        {"replies": {("main", "Ana"): SimpleNamespace(sending=True)}}):
            with self.subTest(sending=list(sending)):
                app = self.make(**sending)
                with mock.patch.object(GLib, "timeout_add", return_value=3) as later:
                    Natter.update_now(app)
                    Natter.update_now(app)  # pressed again meanwhile: still the one wait
                self.assertEqual((app.updates.called("prepare"), app.toasts), ([], []))
                self.assertEqual(later.call_count, 1)
                self.assertEqual(later.call_args[0][0], 500)
                app.accounts, app.replies = [account()], {}  # answered
                self.assertFalse(later.call_args[0][1](*later.call_args[0][2:]))
                self.assertEqual(len(app.updates.called("prepare")), 1)

    def test_past_the_passcode(self):
        app = self.make(locked=True)
        Natter.update_now(app)
        self.assertEqual([key for key, _run in app.held], ["update"])
        self.assertEqual(app.updates.called("prepare"), [])

    def test_prepared_then_the_updater_then_quit(self):
        app = self.make()
        self.prepared(app)(True)
        (hidden, started), = app.updates.called("launch")
        self.assertFalse(hidden)
        self.assertEqual(app.called("quit_natter"), [])  # not before the updater has taken its plan
        started(True)
        self.assertEqual(app.called("quit_natter"), [()])

    def test_hidden_starts_again_hidden(self):
        app = self.make()
        then = self.prepared(app)
        app.window = SimpleNamespace(get_visible=lambda: False)  # closed during the download
        then(True)
        (hidden, _started), = app.updates.called("launch")
        self.assertTrue(hidden)

    def test_a_call_during_the_download(self):
        app = self.make()
        then = self.prepared(app)
        app.calling = True
        then(True)
        # What was downloaded is kept for the next Update; nothing quits.
        self.assertEqual((app.updates.called("hold"), app.updates.called("launch")), ([()], []))
        self.assertEqual(app.toasts, [(f"Update {NAME} once the call ends",)])
        self.assertEqual(app.called("quit_natter"), [])

    def test_a_reply_during_the_download_is_waited_for(self):
        app = self.make()
        then = self.prepared(app)
        app.accounts = [account(replying=1)]
        with mock.patch.object(GLib, "timeout_add", return_value=3) as later:
            then(True)
        self.assertEqual((app.updates.called("hold"), app.updates.called("launch")), ([], []))  # ready as it is
        app.accounts = [account()]
        self.assertFalse(later.call_args[0][1](*later.call_args[0][2:]))
        self.assertEqual(len(app.updates.called("launch")), 1)

    def test_the_updater_doesnt_start(self):
        app = self.make()
        self.prepared(app)(True)
        (_hidden, started), = app.updates.called("launch")
        started(False)  # told by Updates: Natter Den keeps running, untouched
        self.assertEqual(app.called("quit_natter"), [])

    def test_not_ready(self):
        app = self.make()
        self.prepared(app)(False)  # told by Updates
        self.assertEqual((app.updates.called("launch"), app.called("quit_natter")), ([], []))

    def test_quit_during_the_download(self):
        app = self.make()
        then = self.prepared(app)
        app._quitting = True
        then(True)
        self.assertEqual(app.updates.called("launch"), [])

    def test_replies_written_meanwhile_are_asked_about(self):
        # The dialog said nothing of replies, and one was written while the release downloaded (a
        # notification's Reply): asked about before Natter quits, what was downloaded kept.
        app = self.make()
        then = self.prepared(app)
        app.reply_drafts = {("main", "Ana"): "see you at six"}
        alert = Fake()
        with mock.patch("natter.app.replies_dialog", lambda on: alert if on is app else None):
            then(True)
        self.assertEqual((app.updates.called("hold"), app.updates.called("launch")), ([()], []))
        self.assertEqual(alert.called("present"), [(app.window,)])
        self.assertEqual(app.called("quit_natter"), [])

    def test_replies_the_dialog_warned_of_arent_asked_about_again(self):
        app = self.make(reply_drafts={("main", "Ana"): "see you at six"})
        Natter.update_now(app, True)
        (then,), = app.updates.called("prepare")
        with mock.patch("natter.app.replies_dialog", lambda on: self.fail("asked again")):
            then(True)
        self.assertEqual(len(app.updates.called("launch")), 1)

    def test_warned_holds_past_the_passcode(self):
        app = self.make(locked=True)
        Natter.update_now(app, True)
        (key, run), = app.held
        app.locked = False
        run()
        self.assertEqual((key, app._update_warned, len(app.updates.called("prepare"))), ("update", True, 1))

    def test_a_call_as_the_updater_starts(self):
        # Answered, or ringing, while the updater takes its plan (up to update.HELPER_WAIT): Natter
        # stays, and tells the updater to change nothing.
        for values in ({"calling": True}, {"calls": SimpleNamespace(in_call=False, ringing=True)}):
            with self.subTest(values=values):
                app = self.make()
                self.prepared(app)(True)
                (_hidden, started), = app.updates.called("launch")
                app.__dict__.update(values)
                started(True)
                self.assertEqual(app.called("quit_natter"), [])
                self.assertEqual(app.updates.called("stay"), [()])
                self.assertEqual(app.toasts, [(f"Update {NAME} once the call ends",)])

    def test_a_page_that_never_gets_ready_doesnt_hold_it_off(self):
        # WhatsApp changed its page, or Natter stopped reloading it: a new release is what helps.
        app = self.make(accounts=[account(ready=False, loaded_at=time.monotonic() - update.LOAD_HOLD)])
        Natter.update_now(app)
        self.assertEqual((len(app.updates.called("prepare")), app.toasts), (1, []))


class CallWindowTest(unittest.TestCase):
    def test_ringing_until_answered_or_over(self):
        # What holds an update off there (update.in_a_call): in_call alone comes once it's answered.
        calls = CallWindow(Path(tempfile.gettempdir()))
        self.assertFalse(calls.ringing)
        calls._event({"event": "incoming", "name": "Ana"})
        self.assertEqual((calls.ringing, calls.in_call), (True, False))
        calls._event({"event": "incoming-ended"})
        self.assertFalse(calls.ringing)
        calls._event({"event": "incoming", "name": "Ana"})
        calls._event({"event": "in-call", "value": True})
        self.assertEqual((calls.ringing, calls.in_call), (False, True))


# ---- Preferences → General → Updates -------------------------------------------------------------


class PreferencesTest(unittest.TestCase):
    def setUp(self):
        patches = mock.patch.multiple(prefs, **prefs_stand_ins.STAND_INS)
        patches.start()
        self.addCleanup(patches.stop)

    def open(self, channel="get", line=("Not checked yet", "check")):
        host = prefs_stand_ins.Host(prefs_stand_ins.Settings())
        host.app.updates = Updates(channel=channel, line=line)
        host.app.activate_action = lambda name, target: host.app.updates.calls.append(("action", name, target))
        page = host._general()
        groups = {group.props.get("title"): group for group in page.added()}
        titles = [group.props.get("title") for group in page.added()]
        return host, groups, titles

    def test_after_running(self):
        _host, _groups, titles = self.open()
        self.assertEqual(titles[titles.index("Running") + 1], "Updates")

    def test_the_switch(self):
        host, groups, _titles = self.open()
        switch, _status = groups["Updates"].added()
        self.assertEqual((switch.props["title"], switch.props["subtitle"], switch.props["active"]),
                         ("Check for Updates", "Asks GitHub once a day whether a new version is out", True))
        self.assertIs(host._rows["update_check"], switch)  # followed like every other switch
        (signal, flipped), = switch.called("connect")
        self.assertEqual(signal, "notify::active")
        flipped(SimpleNamespace(get_active=lambda: False), None)
        self.assertEqual(host.settings.written, [("update_check", False)])

    def test_the_status_and_its_button(self):
        host, groups, _titles = self.open()
        _switch, status = groups["Updates"].added()
        updates = host.app.updates
        self.assertEqual(status.called("set_title"), [(f"{NAME} {VERSION}",)])
        self.assertEqual(status.called("set_subtitle")[-1], ("Not checked yet",))
        (check,), = status.called("add_suffix")
        self.assertEqual(check.props["tooltip_text"], "Check Now")
        (_clicked, run), = check.called("connect")
        run(check)
        self.assertEqual(updates.called("check_now"), [()])

        updates.line = ("9.9 is available", "update")
        host.update_status()
        self.assertEqual(status.called("remove"), [(check,)])
        update = status.called("add_suffix")[-1][0]
        self.assertEqual(update.props["label"], "Update…")
        (_clicked, run), = update.called("connect")
        run(update)
        self.assertEqual(updates.called("action"), [("update", None)])

        written = len(status.calls)
        host.update_status()  # nothing new: nothing set again, nothing read out again
        self.assertEqual(len(status.calls), written)

        updates.line = ("Checking…", "")
        prefs.Preferences._on_setting(host, "update_checked_at", 1)  # a setting of its own changed
        self.assertEqual(status.called("set_subtitle")[-1], ("Checking…",))
        self.assertEqual(status.called("remove")[-1], (update,))
        self.assertEqual(len(status.called("add_suffix")), 2)

    def test_other_copies_say_details(self):
        _host, groups, _titles = self.open("checkout", ("9.9 is available", "details"))
        _switch, status = groups["Updates"].added()
        (details,), = status.called("add_suffix")
        self.assertEqual(details.props["label"], "Details")

    def test_a_build_with_checks_off(self):
        host, groups, _titles = self.open("off")
        row, = groups["Updates"].added()
        self.assertEqual((row.called("set_title"), row.called("set_subtitle")),
                         ([(f"{NAME} {VERSION}",)], [("Updates come with your system's other updates",)]))
        self.assertNotIn("update_check", host._rows)
        host.update_status()  # nothing to follow

    def test_none_in_a_separate_instance(self):
        host, _groups, titles = self.open("lab")
        self.assertNotIn("Updates", titles)
        prefs.Preferences._on_setting(host, "update_latest", "v9.9")  # nothing to follow


if __name__ == "__main__":
    unittest.main()
