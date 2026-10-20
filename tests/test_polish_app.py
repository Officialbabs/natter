"""The app's side of the polish: the Do Not Disturb chooser, the window banner, Preferences
opening at a page, Go to Chat's avatars, the login-portal alert and the About dialog (app.py,
palette.py). Widgets are stood in for, so no display is needed."""

import ast
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree

from gi.repository import GLib

from natter import APP_ID, DEFAULT_APP_ID, NAME, VERSION
from natter import app as natter_app
from natter.app import (DO_NOT_DISTURB, PREFERENCES_PAGES, RELEASE_NOTES, Natter, banner_for, debug_info)
from natter.palette import initials
from natter.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


class Recorder:
    """Stands in for a widget: keeps its constructor's properties and every call made on it."""

    def __init__(self, *args, **props):
        self.args, self.props, self.calls = args, props, []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, *args))

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Check(Recorder):
    def __init__(self, **props):
        super().__init__(**props)
        self.active = props.get("active", False)

    def get_active(self):
        return self.active


class Alert(Recorder):
    made = []

    def __init__(self, **props):
        super().__init__(**props)
        Alert.made.append(self)

    def answer(self, response):
        for callback in self.called("connect"):
            if callback[0] == "response":
                callback[1](self, response)


def widgets():
    """Fake Gtk and Adw modules for app.py's dialogs."""
    Alert.made = []
    adw = SimpleNamespace(AlertDialog=Alert, ActionRow=Recorder, AboutDialog=Alert,
                          ResponseAppearance=SimpleNamespace(SUGGESTED="suggested"),
                          get_major_version=lambda: 1, get_minor_version=lambda: 5, get_micro_version=lambda: 0)
    gtk = SimpleNamespace(ListBox=Recorder, CheckButton=Check, Label=Recorder,
                          SelectionMode=SimpleNamespace(NONE="none"), Align=SimpleNamespace(CENTER="center"),
                          License=SimpleNamespace(GPL_3_0="gpl3", CUSTOM="custom"),
                          get_major_version=lambda: 4, get_minor_version=lambda: 14, get_micro_version=lambda: 0)
    return mock.patch.multiple(natter_app, Adw=adw, Gtk=gtk)


def fake_app(test):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    fake = type("App", (), {})()
    fake.settings = Settings(Path(folder.name) / "settings.json")
    fake.window = Recorder()
    fake.present_window = lambda: True
    fake.paused = []
    fake.pause_notifications = fake.paused.append
    fake.locked = False
    fake._media_banner = False
    fake.style_hint, fake.preferences = "", None  # the theme's hint under Style (page_look_changed)
    fake._apply_theme_window = lambda: None  # Preferences' swatches: test_theme_wiring
    # The microphone and camera, as About's debug info reads them (media_state): nothing known.
    fake.desktop_media, fake.media_lost, fake.microphone_present, fake.camera_fallback = None, False, None, False
    fake.media_state = lambda kind: Natter.media_state(fake, kind)
    return fake


class DoNotDisturbTest(unittest.TestCase):
    def choose(self):
        fake = fake_app(self)
        with widgets():
            Natter.choose_do_not_disturb(fake)
        alert, = Alert.made
        rows = alert.called("set_extra_child")[0][0].called("append")
        return fake, alert, [row[0] for row in rows]

    def test_one_list_of_lengths_and_two_answers(self):
        _fake, alert, rows = self.choose()
        self.assertEqual(alert.props["heading"], "Do Not Disturb")
        self.assertIn(NAME.replace(" ", "\u00a0"), alert.props["body"])  # kept on one line
        self.assertEqual(alert.called("add_response"), [("cancel", "_Cancel"), ("pause", "_Pause")])
        self.assertEqual(alert.called("set_response_appearance"), [("pause", "suggested")])
        self.assertEqual(alert.called("set_default_response"), [("pause",)])
        self.assertEqual(alert.called("set_close_response"), [("cancel",)])
        self.assertEqual([row.props["title"] for row in rows], ["For 1 Hour", "For 2 Hours", "Until Turned Back On"])
        checks = [row.props["activatable_widget"] for row in rows]
        self.assertEqual([row.called("add_prefix") for row in rows], [[(check,)] for check in checks])
        self.assertEqual([check.active for check in checks], [True, False, False])  # one hour, unless changed
        self.assertEqual([check.called("set_group") for check in checks[1:]], [[(checks[0],)], [(checks[0],)]])

    def test_pause_takes_the_chosen_length(self):
        for index, (_label, seconds) in enumerate(DO_NOT_DISTURB):
            fake, alert, rows = self.choose()
            for i, row in enumerate(rows):
                row.props["activatable_widget"].active = i == index
            alert.answer("pause")
            self.assertEqual(fake.paused, [seconds])

    def test_cancel_pauses_nothing(self):
        fake, alert, _rows = self.choose()
        alert.answer("cancel")
        self.assertEqual(fake.paused, [])

    def test_tray_labels_stay(self):
        fake = fake_app(self)
        fake._from_tray = lambda run: run
        fake.resume_notifications = lambda: None
        fake.choose_do_not_disturb = lambda: None
        self.assertEqual([item.label for item in Natter._dnd_items(fake)], ["Do Not Disturb…"])
        fake.settings["dnd_until"] = -1
        self.assertEqual([item.label for item in Natter._dnd_items(fake)], ["Resume Notifications"])


class BannerTest(unittest.TestCase):
    def test_paused_notifications_come_first(self):
        resume = ("Resume", "app.resume-notifications", "")
        self.assertEqual(banner_for(-1, True, 1000.0), ("Notifications paused", *resume))
        until = time.mktime((2026, 10, 20, 14, 30, 0, 0, 0, -1))
        self.assertEqual(banner_for(until, True, until - 3600), ("Notifications paused until 14:30", *resume))

    def test_media_when_not_paused(self):
        media = ("Some videos and voice messages can't play", "Details", "app.preferences-page", "general")
        for dnd_until in (0, 999.0, 1000, "junk", None):  # off, run out, or a damaged setting
            self.assertEqual(banner_for(dnd_until, True, 1000.0), media, dnd_until)
            self.assertIsNone(banner_for(dnd_until, False, 1000.0), dnd_until)

    def test_window_follows_the_setting(self):
        fake = fake_app(self)
        fake._apply_page_sound = fake._refresh_tray = lambda: None
        fake._show_banner = lambda: Natter._show_banner(fake)
        fake.settings.values["dnd_until"] = -1
        Natter._on_setting(fake, "dnd_until", -1)
        self.assertEqual(fake.window.calls, [("set_banner", "Notifications paused", "Resume",
                                              "app.resume-notifications", None)])
        fake.window.calls.clear()
        fake.settings.values["dnd_until"] = 0
        Natter._on_setting(fake, "dnd_until", 0)
        self.assertEqual(fake.window.calls, [("hide_banner",)])

    def test_no_window_no_banner(self):
        fake = fake_app(self)
        fake.window = None
        Natter._show_banner(fake)  # started hidden, or for a search: nothing to show it in

    def test_missing_media_shows_a_banner_not_a_toast(self):
        fake = fake_app(self)
        fake.window = Recorder()
        fake.window.get_visible = lambda: True
        fake.accounts = [SimpleNamespace(ready=True)]
        fake._media_checked = False
        fake._show_banner = lambda: Natter._show_banner(fake)
        fake.media = lambda done: done(["H.264 video"], "sudo apt install x")
        with mock.patch.object(GLib, "timeout_add_seconds", side_effect=lambda _delay, run: run()):
            Natter._check_media(fake)
        (title, button, action, target), = fake.window.called("set_banner")
        self.assertEqual((button, action), ("Details", "app.preferences-page"))
        self.assertTrue(target.equal(GLib.Variant("s", "general")))
        self.assertEqual(fake.window.called("toast"), [])


class PreferencesPageTest(unittest.TestCase):
    def open(self, fake, page=""):
        made = []

        class Preferences(Recorder):
            def __init__(self, app):
                super().__init__(app)
                made.append(self)

        with mock.patch("natter.prefs.Preferences", Preferences):
            Natter.show_preferences(fake, page)
        return made

    def test_opens_at_the_page(self):
        fake = fake_app(self)
        fake._held_while_locked = lambda run: False
        prefs, = self.open(fake, "privacy")
        self.assertEqual(prefs.called("set_visible_page_name"), [("privacy",)])
        prefs, = self.open(fake)
        self.assertEqual(prefs.called("set_visible_page_name"), [])

    def test_waits_for_the_unlock_with_its_page(self):
        fake = fake_app(self)
        fake.show_preferences = lambda page="": Natter.show_preferences(fake, page)
        held = []
        fake._held_while_locked = lambda run: held.append(run) or True
        self.assertEqual(self.open(fake, "privacy"), [])
        fake._held_while_locked = lambda run: False
        made = []
        with mock.patch("natter.prefs.Preferences", lambda app: made.append(Recorder()) or made[-1]):
            held[0]()
        self.assertEqual(made[0].called("set_visible_page_name"), [("privacy",)])

    def test_the_media_banner_goes_once_general_is_seen(self):
        fake = fake_app(self)
        fake._held_while_locked = lambda run: False
        fake._show_banner = lambda: fake.window.calls.append(("banner", fake._media_banner))
        fake._media_banner = True
        self.open(fake, "privacy")
        self.assertTrue(fake._media_banner)  # not where the details are
        self.open(fake, "general")
        self.assertFalse(fake._media_banner)
        self.assertEqual(fake.window.calls[-1], ("banner", False))

    def test_lock_toast_opens_privacy(self):
        fake = fake_app(self)
        fake.lock_enabled = False
        Natter.lock_now(fake)
        (_title, button, action, target), = fake.window.called("toast")
        self.assertEqual((button, action), ("Preferences", "app.preferences-page"))
        self.assertTrue(target.equal(GLib.Variant("s", "privacy")))
        self.assertIn("privacy", PREFERENCES_PAGES)

    def test_prefs_names_its_pages_so(self):
        tree = ast.parse((ROOT / "natter" / "prefs.py").read_text())
        names = [keyword.value.value for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "PreferencesPage"
                 for keyword in node.keywords if keyword.arg == "name"]
        self.assertEqual(sorted(names), sorted(PREFERENCES_PAGES))


class ShortcutsTest(unittest.TestCase):
    def test_lock_is_listed_only_with_a_passcode(self):
        from natter import window

        fake = fake_app(self)
        for enabled in (False, True):
            fake.lock_enabled = enabled
            made = []
            with mock.patch.object(window, "shortcuts_dialog", lambda app=None: made.append(app) or Recorder()):
                Natter.show_shortcuts(fake)
            self.assertIs(made[0], fake)  # the app goes along, or Lock is always listed
            rows = [what for _section, keys in window.shortcut_sections(lock=made[0].lock_enabled)
                    for what, _keys in keys]
            self.assertEqual("Lock" in rows, enabled)


class WordingTest(unittest.TestCase):
    def test_needs_linking_in_the_tray(self):
        fake = fake_app(self)
        fake.window, fake.locked, fake.lock_enabled = None, True, False
        fake._from_tray = lambda run: run
        fake._dnd_items = lambda: []
        fake.quit_natter = lambda: None
        account = SimpleNamespace(needs_link=True, name="Work")
        fake.accounts = [account]
        self.assertIn("Needs Linking", [item.label for item in Natter._tray_menu(fake)])
        fake.accounts = [SimpleNamespace(needs_link=False, name="Personal"), account]
        self.assertIn("Work: Needs Linking", [item.label for item in Natter._tray_menu(fake)])

    def test_still_running(self):
        fake = fake_app(self)
        fake._told, fake.tray, told = set(), None, []
        fake.tell = lambda *args: told.append(args)
        Natter.window_closed(fake)
        self.assertEqual(told, [("still-running", f"{NAME} is still running", "Open it again from your apps.")])

    def test_login_portal_alert(self):
        fake = fake_app(self)
        fake.window.get_visible = lambda: True
        pango = SimpleNamespace(WrapMode=SimpleNamespace(WORD="word", WORD_CHAR="word-char"))
        with widgets(), mock.patch.object(natter_app, "Pango", pango):
            Natter._no_login_portal(fake)
        alert, = Alert.made
        self.assertEqual(alert.props["heading"], "Can't Start at Login")
        self.assertEqual(alert.called("add_response"), [("close", "_Close"), ("copy", "Co_py Command")])
        self.assertEqual(alert.called("set_response_appearance"), [("copy", "suggested")])
        label = alert.called("set_extra_child")[0][0]
        self.assertEqual(label.props["label"], f"flatpak run {APP_ID} --background")  # no word joiners in it
        self.assertTrue(label.props["selectable"])
        self.assertEqual(label.props["wrap_mode"], "word")  # never inside the app id


class AboutTest(unittest.TestCase):
    def test_about(self):
        fake = fake_app(self)
        fake.calls_in_page, fake.calls = False, None
        fake._media = (["H.264 video"], "")
        with widgets():
            Natter.show_about(fake)
        about, = Alert.made
        self.assertEqual(about.props["application_icon"], DEFAULT_APP_ID)  # a lab instance's too
        self.assertEqual(about.props["application_name"], NAME)
        self.assertEqual(about.props["issue_url"], about.props["website"] + "/issues")
        self.assertNotIn("Meta", about.props["comments"])  # in the legal section instead
        (title, _copyright, _license, text), = about.called("add_legal_section")
        self.assertEqual(title, "WhatsApp")
        self.assertIn("affiliated with WhatsApp or Meta", text)
        (info,), = about.called("set_debug_info")
        self.assertIn("H.264 video", info)
        self.assertIn("the browser", info)

    def test_developer_matches_the_metainfo(self):
        metainfo = ElementTree.parse(ROOT / "data" / f"{DEFAULT_APP_ID}.metainfo.xml").getroot()
        fake = fake_app(self)
        fake.calls_in_page, fake.calls, fake._media = True, None, ([], "")
        with widgets():
            Natter.show_about(fake)
        self.assertEqual(Alert.made[0].props["developer_name"], metainfo.findtext("developer/name"))

    def test_release_notes_match_the_metainfo(self):
        metainfo = ElementTree.parse(ROOT / "data" / f"{DEFAULT_APP_ID}.metainfo.xml").getroot()
        release = metainfo.find("releases/release")
        self.assertEqual(release.get("version"), VERSION)
        written = "".join(f"<p>{' '.join(''.join(p.itertext()).split())}</p>" for p in release.find("description"))
        self.assertEqual(" ".join(RELEASE_NOTES.split()), written)

    def test_debug_info(self):
        info = debug_info("the call window", None, False)
        for wanted in (f"{NAME} {VERSION}", "GTK 4.", "libadwaita 1.", "WebKitGTK 2.", "PyGObject 3.",
                       "Calls: the call window", "not checked yet"):
            self.assertIn(wanted, info)
        self.assertIn("nothing missing", debug_info("the browser", [], True))
        self.assertIn("H.264 video, Opus audio", debug_info("the browser", ["H.264 video", "Opus audio"], True))


class GoToChatTest(unittest.TestCase):
    def test_numbers_get_no_initials(self):
        for title in ("+44 7700 900123", "‪+44 7700 900123‬", "5511987654321", " +1 555", "١٢٣"):
            self.assertFalse(initials(title), title)
        for title in ("Ana Souza", "Mum", "", "Team 2024", "#general"):
            self.assertTrue(initials(title), title)

    def test_search_has_a_name_and_files_stay_in_sight(self):
        source = (ROOT / "natter" / "palette.py").read_text()
        self.assertIn('update_property([Gtk.AccessibleProperty.LABEL], ["Search chats"])', source)
        self.assertRegex(source, re.compile(r'Adw\.WindowTitle\(title="Send To", subtitle=names\)'))


if __name__ == "__main__":
    unittest.main()
