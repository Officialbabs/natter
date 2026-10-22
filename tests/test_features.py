"""The app lock, do not disturb, group invite and channel links, tray submenus, and the
call pieces that don't need a running WhatsApp (rtc.py's description handling)."""

import os
import tempfile
import unittest
from pathlib import Path

from natter import NAME, lock
from natter.links import chat_link, page_link
from natter.settings import Settings


class LockTest(unittest.TestCase):
    def test_passcode_round_trip(self):
        stored = lock.make_hash("2468", iterations=1000)
        self.assertTrue(lock.check("2468", stored))
        self.assertFalse(lock.check("2469", stored))
        self.assertNotIn("2468", stored)

    def test_salted(self):
        self.assertNotEqual(lock.make_hash("2468", iterations=1000), lock.make_hash("2468", iterations=1000))

    def test_damaged_hash_never_unlocks(self):
        for stored in ("", "garbage", "md5$1$00$00", "pbkdf2-sha256$x$zz$zz"):
            self.assertFalse(lock.check("2468", stored))

    def test_waits_after_too_many_tries(self):
        now = [1000.0]
        store = {}
        attempts = lock.Attempts(store, clock=lambda: now[0])
        for _ in range(lock.TRIES - 1):
            attempts.failed()
        self.assertEqual(attempts.wait(), 0)
        attempts.failed()
        self.assertEqual(attempts.wait(), 30)
        # A restart (a new Attempts over the same settings) still waits.
        self.assertEqual(lock.Attempts(store, clock=lambda: now[0]).wait(), 30)
        now[0] += 30
        self.assertEqual(attempts.wait(), 0)
        for _ in range(lock.TRIES):
            attempts.failed()
        self.assertEqual(attempts.wait(), 60)  # longer each time
        attempts.succeeded()
        self.assertEqual((attempts.wrong, attempts.wait()), (0, 0))

    def test_damaged_types_never_unlock(self):
        for stored in (None, 42, ["x"], "pbkdf2-sha256$1000$00$zz", "pbkdf2-sha256$99999999999999999999$00$00"):
            self.assertFalse(lock.check("2468", stored))

    def test_short_passcodes(self):
        self.assertTrue(lock.too_short("123"))
        self.assertFalse(lock.too_short("1234"))
        self.assertEqual(lock.idle_label(0), "Never")
        self.assertEqual(lock.idle_label(1), "After 1 Minute")
        self.assertEqual(lock.idle_label(60), "After 1 Hour")


class DoNotDisturbTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.folder.name) / "settings.json")

    def tearDown(self):
        self.folder.cleanup()

    def test_off_by_default(self):
        self.assertFalse(self.settings.do_not_disturb())

    def test_for_a_while(self):
        self.settings["dnd_until"] = 1000
        self.assertTrue(self.settings.do_not_disturb(now=999))
        self.assertFalse(self.settings.do_not_disturb(now=1001))

    def test_until_turned_off(self):
        self.settings["dnd_until"] = -1
        self.assertTrue(self.settings.do_not_disturb(now=10 ** 12))
        self.assertTrue(self.settings.notifications_paused())


class PageLinkTest(unittest.TestCase):
    def test_invites(self):
        for link in ("https://chat.whatsapp.com/AbC123", "https://chat.whatsapp.com/invite/AbC123/",
                     "http://chat.whatsapp.com/AbC123", "whatsapp://chat?code=AbC123"):
            self.assertEqual(page_link(link), "https://chat.whatsapp.com/AbC123", link)

    def test_channels(self):
        for link in ("https://whatsapp.com/channel/0029VaX", "https://www.whatsapp.com/channel/0029VaX/",
                     "whatsapp://channel/0029VaX"):
            self.assertEqual(page_link(link), "https://whatsapp.com/channel/0029VaX", link)

    def test_not_page_links(self):
        for link in ("https://chat.whatsapp.com/", "https://chat.whatsapp.com/a b", "https://example.com/chat.whatsapp.com/x",
                     "https://whatsapp.com/channel/", "https://wa.me/123", "javascript:alert(1)", "whatsapp://chat?code=a'b"):
            self.assertIsNone(page_link(link), link)

    def test_chat_links_stay_chat_links(self):
        self.assertIsNotNone(chat_link("https://wa.me/123"))
        self.assertIsNone(chat_link("https://chat.whatsapp.com/AbC123"))


class TraySubmenuTest(unittest.TestCase):
    def test_layout_nests_and_keys_change(self):
        from natter.tray import MenuItem, Tray

        tray = Tray.__new__(Tray)  # just the menu side, no bus
        tray.items, tray.next_id, tray.parents = None, 1, {}
        menu = [MenuItem("Show"), MenuItem("Pause", children=[MenuItem("1 h", lambda: None), MenuItem("2 h", lambda: None)])]
        tray.build_menu = lambda: menu
        layout = tray._layout(0).unpack()
        top = layout[2]
        self.assertEqual(len(top), 2)
        self.assertEqual(top[1][1]["children-display"], "submenu")
        self.assertEqual([child[1]["label"] for child in top[1][2]], ["1 h", "2 h"])
        tray.refresh = lambda: None
        tray.items = tray._number(menu)
        self.assertFalse(tray._about_to_show())  # unchanged, submenu and all: no renumbering
        self.assertEqual(tray._layout(10 ** 6).unpack(), (10 ** 6, {}, []))  # a stale submenu id
        before = MenuItem("Pause", children=[MenuItem("1 h", lambda: None)]).key()
        after = MenuItem("Pause", children=[MenuItem("2 h", lambda: None)]).key()
        self.assertNotEqual(before, after)


class RemoteDescriptionTest(unittest.TestCase):
    """WhatsApp's relays log in with ICE credentials webrtcbin's description check refuses."""

    def test_odd_credentials_are_held_back(self):
        from natter import rtc

        sdp = ("v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=ice-ufrag:abc=def-ghi_jkl\r\n"
               "a=ice-pwd:0123456789012345678901==\r\na=candidate:2 1 udp 2122262783 192.0.2.1 3478 typ host\r\n"
               "a=end-of-candidates\r\n")
        cleaned, real, held = rtc._for_webrtcbin(sdp)
        self.assertEqual(real, ("abc=def-ghi_jkl", "0123456789012345678901=="))
        self.assertEqual(held, [(0, "a=candidate:2 1 udp 2122262783 192.0.2.1 3478 typ host")])
        self.assertNotIn("a=candidate", cleaned)
        self.assertNotIn("abc=def", cleaned)

    def test_usual_credentials_pass_through(self):
        from natter import rtc

        sdp = "v=0\r\nm=application 9 x\r\na=ice-ufrag:abcd\r\na=ice-pwd:0123456789012345678901ab\r\n"
        self.assertEqual(rtc._for_webrtcbin(sdp), (sdp, None, []))

    def test_fingerprint_is_the_certificates(self):
        from natter import rtc

        pem, fingerprint = rtc.make_certificate()
        sdp = rtc.with_fingerprint("a=fingerprint:sha-256 AA:BB\r\n", fingerprint)
        self.assertEqual(sdp, f"a=fingerprint:sha-256 {fingerprint}\r\n")
        self.assertEqual(len(fingerprint.split(":")), 32)
        self.assertIn("PRIVATE KEY", pem)



class AudioFollowerTest(unittest.TestCase):
    def test_only_the_pages_streams(self):
        from natter.system import page_streams

        streams = [
            {"index": 1, "sink": 58, "properties": {"application.name": "Natter", "application.process.binary": "WebKitWebProcess"}},
            {"index": 2, "sink": 58, "properties": {"application.name": "Natter", "application.process.binary": "python3"}},
            {"index": 3, "sink": 58, "properties": {"application.name": "ada-browser", "application.process.binary": "QtWebEngineProcess"}},
            {"index": 4, "sink": 58, "properties": {"application.name": "WebKitWebProcess", "application.process.binary": "WebKitWebProcess"}},
            {"index": 5, "sink": 58},
        ]
        self.assertEqual([s["index"] for s in page_streams(streams, "Natter")], [1])


class ReloadingTest(unittest.TestCase):
    """The header says "Reloading…" from a reload until WhatsApp is back (account.py, window.py)."""

    class Page:
        """Just enough of an Account for its load and ready steps."""

        def __init__(self):
            self.home = "https://web.whatsapp.com/"
            self.reloading = self.ready = False
            self._reload_timer = 0
            self.loaded, self.emitted = [], 0
            self.view = type("View", (), {"load_uri": lambda _view, uri: self.loaded.append(uri)})()

        def _end_link(self):
            pass

        def apply_privacy(self):
            pass

        def _run_pending(self):
            pass

        def emit(self, signal):
            self.emitted += 1

    def setUp(self):
        from natter.account import Account

        self.Account = Account
        self.page = self.Page()
        self.page._set_reloading = lambda value: Account._set_reloading(self.page, value)

    def test_reload_until_ready(self):
        self.Account._load(self.page, self.page.home)
        self.assertTrue(self.page.reloading)
        self.assertEqual(self.page.loaded, [self.page.home])
        self.Account._set_ready(self.page, True)
        self.assertFalse(self.page.reloading)
        self.assertGreaterEqual(self.page.emitted, 2)

    def test_a_link_is_not_a_reload(self):
        self.Account._load(self.page, "https://web.whatsapp.com/accept?code=abc")
        self.assertFalse(self.page.reloading)

    def test_status_line(self):
        from natter.window import status_line

        self.assertEqual(status_line(False, True, 4), "Reloading…")
        self.assertEqual(status_line(True, True, 4), "Needs linking")
        self.assertEqual(status_line(False, False, 4), "4 unread")
        self.assertEqual(status_line(False, False, 0), "")


class BlankPageTest(unittest.TestCase):
    """A WhatsApp page that arrives empty or stops loading is loaded again, a few times (account.py)."""

    def page(self, progress=()):
        from natter.account import Account

        page = type("Page", (), {})()
        page.name, page.home = "Personal", "https://web.whatsapp.com/"
        page.removed = page.ready = page.needs_link = False
        page.failed, page._blank_reloads, page.loads = None, [], []
        page._load = lambda uri: page.loads.append(uri)
        page.reloading = True
        page.emit = lambda signal: None
        page._set_reloading = lambda value: Account._set_reloading(page, value)
        page._reload_blank = lambda what: Account._reload_blank(page, what)
        values = iter(progress)
        page.view = type("View", (), {"get_estimated_load_progress": lambda _view: next(values)})()
        page._progress, page._page_timer = (-1.0, 0.0), 1
        return page

    def test_reloads_are_limited(self):
        from natter.account import BLANK_TRIES, Account

        page = self.page()
        for _ in range(BLANK_TRIES + 2):
            Account._reload_blank(page, "came back empty")
        self.assertEqual(len(page.loads), BLANK_TRIES)

    def test_no_reload_once_whatsapp_is_up(self):
        from natter.account import Account

        page = self.page()
        page.ready = True
        Account._reload_blank(page, "came back empty")
        self.assertEqual(page.loads, [])

    def test_slow_progress_is_not_a_stall(self):
        from unittest import mock

        from natter.account import STALL_LIMIT, Account

        page = self.page(progress=[0.1, 0.2, 0.3, 0.4, 0.5])
        with mock.patch("natter.account.time.monotonic", side_effect=[0, 50, 100, 150, 200]):
            for _ in range(5):
                self.assertTrue(Account._page_stalled(page))  # keeps looking
        self.assertEqual(page.loads, [])
        self.assertGreater(200, STALL_LIMIT)

    def test_no_progress_is_a_stall(self):
        from unittest import mock

        from natter.account import Account

        page = self.page(progress=[0.4, 0.4, 0.4])
        # monotonic: first look (progress noted), second look (+15 s), third look (+50 s, then the reload's own)
        with mock.patch("natter.account.time.monotonic", side_effect=[0, 15, 50, 50]):
            self.assertTrue(Account._page_stalled(page))
            self.assertTrue(Account._page_stalled(page))
            self.assertFalse(Account._page_stalled(page))
        self.assertEqual(page.loads, [page.home])


class ReviewFixesTest(unittest.TestCase):
    """What the review of these changes found (account.py, app.py, prefs.py)."""

    def test_reloading_ends_on_the_link_screen_after_a_reload_there(self):
        import json

        from natter.account import Account

        page = type("Page", (), {})()
        page._token, page.needs_link, page.reloading, page.emitted = "t", False, True, 0
        page._trusted = lambda: True
        page.emit = lambda signal: setattr(page, "emitted", page.emitted + 1)
        page._set_reloading = lambda value: Account._set_reloading(page, value)
        page._drop_pending = lambda: None
        value = type("Value", (), {"to_json": lambda _v, _i: json.dumps({"token": "t", "type": "state",
                                                                            "value": "needs-link"})})()
        Account._on_message(page, None, value)
        self.assertFalse(page.reloading)
        self.assertTrue(page.needs_link)

    def test_giving_up_on_a_blank_page_ends_reloading(self):
        from natter.account import BLANK_TRIES, Account

        page = type("Page", (), {})()
        page.name, page.home = "Personal", "https://web.whatsapp.com/"
        page.removed = page.ready = page.needs_link = False
        page.reloading, page._blank_reloads = True, [0.0] * BLANK_TRIES
        page.emit = lambda signal: None
        page._set_reloading = lambda value: Account._set_reloading(page, value)
        page._load = lambda uri: self.fail("no more reloads")
        from unittest import mock

        with mock.patch("natter.account.time.monotonic", return_value=1.0):
            Account._reload_blank(page, "came back empty")
        self.assertFalse(page.reloading)

    def test_blur_when_away_applies_at_once(self):
        from natter.app import Natter

        calls = []
        fake = type("App", (), {})()
        fake.window = type("Window", (), {"is_active": lambda _w: False})()
        fake.replies = {}  # no reply window open (reply.py follows the setting too)
        fake.on_focus_changed = lambda active: calls.append(active)
        fake._refresh_tray = lambda: calls.append("tray")
        Natter._on_setting(fake, "blur_when_away", True)
        self.assertIn(False, calls)  # applied now, not at the next focus change

    def test_preferences_follow_the_current_value(self):
        from natter.prefs import Preferences

        row = type("Row", (), {})()
        row.active = True
        row.get_active = lambda: row.active
        row.set_active = lambda value: setattr(row, "active", value)
        fake = type("Prefs", (), {})()
        fake._rows = {"start_at_login": row}
        fake.settings = {"start_at_login": False}  # an earlier listener turned it back off
        Preferences._on_setting(fake, "start_at_login", True)  # the stale announcement
        self.assertFalse(row.active)

    def test_a_lost_network_process_is_noticed(self):
        from unittest import mock

        from natter.app import Natter

        lost = []
        fake = type("App", (), {})()
        import time

        fake._network_processes, fake._network_looks, fake._quitting = {101}, 0, False
        fake._last_look = time.monotonic()
        fake._note_oom_kills = lambda events, now: None
        fake.accounts = [type("Account", (), {"network_lost": lambda _a: lost.append(1)})()]
        with mock.patch("natter.app.network_processes", return_value={202}):
            Natter._check_network_process(fake)
        self.assertEqual(lost, [1])
        fake._quitting = True
        with mock.patch("natter.app.network_processes", return_value=set()):
            Natter._check_network_process(fake)
        self.assertEqual(lost, [1])  # quitting: WebKit ends it, nothing lost


class TrayTest(unittest.TestCase):
    """The tray: chats gone, switches in, and property answers cheap (tray.py, app.py)."""

    def menu(self, locked: bool, blur: bool = False, unread: int = 0):
        from natter.app import Natter

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        fake = type("App", (), {})()
        fake.settings = Settings(Path(folder.name) / "settings.json")
        fake.settings["privacy_blur"] = blur
        account = type("Account", (), {"needs_link": False, "unread": unread, "name": "Personal"})()
        fake.window, fake.accounts, fake.locked, fake.lock_enabled = None, [account], locked, True
        for name in ("_from_tray", "_dnd_items"):
            setattr(fake, name, getattr(Natter, name).__get__(fake))
        for name in ("present_window", "pause_notifications", "resume_notifications", "lock", "quit_natter"):
            setattr(fake, name, lambda *_: None)
        fake.chosen = []
        fake.choose_do_not_disturb = lambda: fake.chosen.append("dnd")
        fake.tray = None
        return fake, Natter._tray_menu(fake)

    def test_menu_has_only_the_key_items(self):
        fake, items = self.menu(locked=False, unread=200)
        labels = [item.label for item in items if not item.separator]
        self.assertEqual(labels, [f"Show {NAME}", "Turn On Privacy Blur", "Do Not Disturb…", f"Lock {NAME}",
                                  f"Quit {NAME}"])
        self.assertFalse(any(item.children for item in items))  # Ada's panel unfolds submenus
        next(item for item in items if item.label == "Do Not Disturb…").run()
        self.assertEqual(fake.chosen, ["dnd"])
        self.assertFalse(any(a.separator and b.separator for a, b in zip(items, items[1:])))
        next(item for item in items if item.label == "Turn On Privacy Blur").run()
        self.assertTrue(fake.settings["privacy_blur"])
        _fake, items = self.menu(locked=False, blur=True)
        self.assertIn("Turn Off Privacy Blur", [item.label for item in items])

    def test_locked_menu(self):
        _fake, items = self.menu(locked=True)
        self.assertEqual([item.label for item in items if not item.separator],
                         [f"Show {NAME}", "Do Not Disturb…", f"Quit {NAME}"])

    def test_paused_notifications_offer_resume(self):
        fake, _items = self.menu(locked=False)
        fake.settings["dnd_until"] = -1
        from natter.app import Natter

        labels = [item.label for item in Natter._tray_menu(fake)]
        self.assertIn("Resume Notifications", labels)
        self.assertNotIn("Do Not Disturb…", labels)

    def test_pixmaps_built_in_one_block(self):
        from gi.repository import GLib

        from natter.tray import pixmap_variant

        drawn = [(2, 1, bytes(range(8))), (1, 1, b"\xff\x00\x10\x20")]
        self.assertTrue(pixmap_variant(drawn).equal(GLib.Variant("a(iiay)", drawn)))
        self.assertEqual(pixmap_variant([]).get_type_string(), "a(iiay)")

    def test_notification_picture_in_one_block(self):
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        from natter.notify import image_data

        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 5, 3)
        pixbuf.fill(0x336699FF)
        reference = GLib.Variant("(iiibiiay)", (pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride(),
                                                pixbuf.get_has_alpha(), pixbuf.get_bits_per_sample(),
                                                pixbuf.get_n_channels(), pixbuf.get_pixels()))
        self.assertTrue(image_data(pixbuf).equal(reference))


class ChatOrderTest(unittest.TestCase):
    """bridge.js orders chat rows from their offsets alone, never asking for a layout."""

    def run_js(self, rows_js: str):
        import json
        import re

        import gi

        gi.require_version("JavaScriptCore", "6.0")
        from gi.repository import JavaScriptCore

        source = (Path(__file__).parent.parent / "natter" / "bridge.js").read_text()
        functions = "\n".join(re.search(rf"  function {name}\(.*?\n  }}\n", source, re.S).group(0)
                              for name in ("offsetOf", "rowHeight", "positionOf"))
        script = functions + f"""
        const row = (y, index) => ({{ style: {{ transform: y === null ? "" : `translateY(${{y}}px)` }},
                                     getAttribute: () => index === undefined ? null : String(index),
                                     get offsetHeight() {{ throw new Error("layout read"); }} }});
        const rows = {rows_js};
        const height = rowHeight(rows.map(offsetOf));
        JSON.stringify(rows.map((r, i) => positionOf(r, offsetOf(r), height, i)));
        """
        context = JavaScriptCore.Context()
        value = context.evaluate(script, -1)
        error = context.get_exception()
        self.assertIsNone(error, error and error.get_message())
        return json.loads(value.to_string())

    def test_places_from_offsets(self):
        self.assertEqual(self.run_js("[row(144), row(0), row(72), row(288)]"), [2, 0, 1, 4])

    def test_aria_rowindex_wins(self):
        self.assertEqual(self.run_js("[row(144, 7), row(0, 5)]"), [6, 4])

    def test_one_row_or_no_offsets_fall_back_to_document_order(self):
        self.assertEqual(self.run_js("[row(72)]"), [0])
        self.assertEqual(self.run_js("[row(null), row(null)]"), [0, 1])


class RealtimeThreadsTest(unittest.TestCase):
    """WebKit's real-time threads got its network process killed (system.py hide_system_bus)."""

    class Launcher:
        def __init__(self):
            self.env = {}

        def setenv(self, name, value, overwrite=True):
            self.env[name] = value

        def unsetenv(self, name):
            self.env[name] = None

    def setUp(self):
        from natter import system

        self.system = system
        self.before = system._system_bus_before
        system._system_bus_before = None

    def tearDown(self):
        self.system._system_bus_before = self.before

    def test_hidden_and_given_back(self):
        environ = {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket"}
        self.assertTrue(self.system.hide_system_bus(environ, sandboxed=False))
        self.assertEqual(environ["DBUS_SYSTEM_BUS_ADDRESS"], self.system.NO_SYSTEM_BUS)
        self.assertFalse(self.system.hide_system_bus(environ, sandboxed=False))  # already hidden
        launcher = self.Launcher()
        self.system.give_system_bus(launcher)
        self.assertEqual(launcher.env, {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket"})

    def test_unset_before_is_unset_for_others(self):
        environ = {}
        self.assertTrue(self.system.hide_system_bus(environ, sandboxed=False))
        launcher = self.Launcher()
        self.system.give_system_bus(launcher)
        self.assertEqual(launcher.env, {"DBUS_SYSTEM_BUS_ADDRESS": None})

    def test_given_back_to_apps_natter_opens(self):
        from gi.repository import Gio

        environ = {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket"}
        self.assertTrue(self.system.hide_system_bus(environ, sandboxed=False))
        context = Gio.AppLaunchContext()
        self.system.give_system_bus(context)
        self.assertIn("DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket", context.get_environment())

    def test_sandbox_left_alone(self):
        environ = {}
        self.assertFalse(self.system.hide_system_bus(environ, sandboxed=True))
        self.assertEqual(environ, {})
        launcher = self.Launcher()
        self.system.give_system_bus(launcher)
        self.assertEqual(launcher.env, {})

    def test_camera_fix_taken_off_apps_natter_starts(self):
        """gstfix.apply()'s GStreamer settings are for WebKit only: not a browser, a media player
        or the call window."""
        from unittest import mock

        from natter import gstfix

        with tempfile.TemporaryDirectory() as home:
            plugins = Path(home, ".local", "lib", "natter", "gstreamer-1.0")
            plugins.mkdir(parents=True)
            (plugins / gstfix.PLUGIN).write_bytes(b"")
            environ = {"HOME": home, "XDG_CACHE_HOME": str(Path(home, "cache")), "GST_TRACERS": "leaks",
                       "NATTER_CAMFIX": "1"}
            self.addCleanup(setattr, gstfix, "STATUS", gstfix.STATUS)
            fix = gstfix.apply(environ, flatpak_info=str(Path(home, "none")), dirs=[plugins], version=(1, 28, 2))
            self.assertTrue(fix.on, fix.reason)
            with mock.patch.dict(os.environ, environ, clear=True):
                launcher = self.Launcher()
                self.system.give_system_bus(launcher)  # nothing hidden: still cleaned
                self.assertEqual(launcher.env, {"GST_PLUGIN_PATH": None, "GST_TRACERS": "leaks", "GST_REGISTRY": None})
                self.assertTrue(gstfix.cleaned())  # so open_uri() starts apps itself, cleaned

    def test_network_processes_are_children_by_name(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for task, children in (("100", "200 201\n"), ("105", "202")):
                (proc / "100" / "task" / task).mkdir(parents=True)
                (proc / "100" / "task" / task / "children").write_text(children)
            for pid, name in (("200", "WebKitNetworkPr\n"), ("201", "bwrap\n"), ("202", "WebKitNetworkPr\n")):
                (proc / pid).mkdir()
                (proc / pid / "comm").write_text(name)
            self.assertEqual(self.system.network_processes(100, proc), {200, 202})
            self.assertEqual(self.system.network_processes(999, proc), set())


if __name__ == "__main__":
    unittest.main()
