"""The microphone and camera switches as people see them: Preferences → Privacy (the rows, the
lines under them, when Ask Again… or How to Allow… shows, both dialogs, what a screen reader is
told), the main menu's check items, Go to Chat's commands and the toast when WhatsApp is refused.
Widgets are stood in for, so no display is needed."""

import ast
import inspect
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from natter import APP_ID, NAME, media, palette, prefs, window
from natter.palette import Palette, device_command
from natter.prefs import Preferences, camera_blocked, camera_help
from natter.settings import DEFAULTS
from natter.window import REFUSED_GAP, Window, refused_due, refused_toast

ROOT = Path(__file__).resolve().parent.parent


# ---- Stand-ins -----------------------------------------------------------------------------------


class Widget:
    """Stands in for any GTK or libadwaita widget: keeps what it was made with and every call
    made on it. `kind` is the name it was made by, such as "Adw.SwitchRow"."""

    def __init__(self, *args, **props):
        self.args, self.props, self.calls, self.kind = args, props, [], ""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, *args))

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]

    def added(self):
        return [call[1] for call in self.calls if call[0] in ("add", "append", "add_prefix", "set_child")]

    def visible(self):
        shown = self.called("set_visible")
        return shown[-1][0] if shown else self.props.get("visible", True)

    def sensitive(self):
        set_to = self.called("set_sensitive")
        return set_to[-1][0] if set_to else True

    def handler(self, signal):
        """The callback last connected to `signal`."""
        return [call[1] for call in self.called("connect") if call[0] == signal][-1]


class Switch(Widget):
    """An Adw.SwitchRow: holds whether it's on, and says notify::active when that changes."""

    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.active = props.get("active", False)

    def get_active(self):
        return self.active

    def set_active(self, active):
        if active != self.active:
            self.active = active
            for (signal, run, *_data) in self.called("connect"):
                if signal == "notify::active":
                    run(self, None)

    def subtitle(self):
        lines = self.called("set_subtitle")
        return lines[-1][0] if lines else self.props.get("subtitle", "")


class Alert(Widget):
    """An Adw.AlertDialog, answered with the id of the response chosen."""

    made = []

    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        Alert.made.append(self)

    def choose_finish(self, result):
        return result

    def responses(self):
        return [(response, label) for response, label in self.called("add_response")]

    def answer(self, response):
        """The dialog closed with `response`, as choose() or the "response" signal hears it."""
        for _parent, _cancellable, done in self.called("choose"):
            done(self, response)
        for signal, run, *_data in self.called("connect"):
            if signal == "response":
                run(self, response)


class Name(str):
    """A name in a stand-in module, such as "Gtk.AccessibleRole.PRESENTATION": its own value as an
    enum, a Widget maker when called."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Name(f"{self}.{name}")

    def __call__(self, *args, **props):
        made = {"Adw.SwitchRow": Switch, "Adw.AlertDialog": Alert}.get(self, Widget)(*args, **props)
        made.kind = str(self)
        return made


STAND_INS = dict(Gtk=Name("Gtk"), Adw=Name("Adw"), Pango=Name("Pango"))
MEDIUM = "Gtk.AccessibleAnnouncementPriority.MEDIUM"  # as the stand-in Gtk names it


class Settings(dict):
    """Natter's settings as Preferences sees them: a change to the value already there is none,
    as in settings.py, so a row set to match doesn't write back."""

    def __init__(self, **values):
        super().__init__({"microphone": True, "camera": True, **DEFAULTS, **values})
        self.written = []

    def __setitem__(self, key, value):
        if self.get(key) == value:
            return
        self.written.append((key, value))
        super().__setitem__(key, value)

    def change_elsewhere(self, key, value):
        super().__setitem__(key, value)


class App:
    """The app's side of the contract: media_state from the settings and what the desktop says,
    watch_media and ask_camera_again kept to be answered by hand."""

    def __init__(self, settings, desktop=None, present=True, lockdown=False, sandboxed=False):
        self.settings = settings
        self.desktop, self.present, self.lockdown, self.sandboxed = desktop, present, lockdown, sandboxed
        self.watching, self.unwatched, self.asks = [], [], []

    def media_state(self, kind):
        camera = kind == "camera"
        return dict(on=bool(self.settings[kind]), desktop=self.desktop if camera else None,
                    present=self.present, lockdown=self.lockdown if camera else False, sandboxed=self.sandboxed)

    def watch_media(self, callback):
        self.watching.append(callback)
        return f"watch-{len(self.watching)}"

    def unwatch_media(self, handle):
        self.unwatched.append(handle)

    def ask_camera_again(self, done):
        self.asks.append(done)


class Host:
    """Preferences' own methods on a plain object, so neither a dialog nor a display is needed."""

    def __init__(self, app):
        self.app, self.settings, self._rows = app, app.settings, {}
        self._open, self._media_watch = True, "watch-1"
        self.clipboard, self.toasts = [], []
        self.add_toast = self.toasts.append
        self.get_clipboard = lambda: SimpleNamespace(set=self.clipboard.append)

    def __getattr__(self, name):
        member = getattr(Preferences, name)
        return member.__get__(self) if inspect.isfunction(member) else member


def warned(row):
    """The warning icon put in and taken out of `row`, in order."""
    return [call for call in row.calls if call[0] in ("add_prefix", "remove")]


def line(kind, on=True, desktop=None, present=True, lockdown=False, sandboxed=False, restart=False):
    return media.device_subtitle(kind, on, desktop, present, lockdown, sandboxed, restart=restart)


class Privacy(unittest.TestCase):
    """Builds Privacy from stand-ins."""

    def setUp(self):
        patches = mock.patch.multiple(prefs, **STAND_INS)
        patches.start()
        self.addCleanup(patches.stop)
        Alert.made = []

    def open(self, **state):
        values = {key: state.pop(key) for key in ("microphone", "camera") if key in state}
        host = Host(App(Settings(**values), **state))
        page = host._privacy()
        return host, page


# ---- Preferences → Privacy -------------------------------------------------------------------------


class PageTest(Privacy):
    def test_after_blur_before_lock(self):
        _host, page = self.open()
        groups = page.added()
        blur, devices, help_, lock, passcode = groups
        self.assertEqual(blur.props["description"], "For working in public or sharing your screen.")
        self.assertEqual(devices.props["title"], "Microphone and Camera")
        self.assertEqual(devices.props["description"], "What WhatsApp can use for calls and voice messages.")
        self.assertEqual(help_.props.get("title"), None)  # a group of its own for something to do
        self.assertEqual(lock.props["title"], "Lock")
        self.assertEqual([row.props["title"] for row in passcode.added()], ["Change Passcode…"])

    def test_two_switches(self):
        host, page = self.open()
        microphone, _pause, camera = page.added()[1].added()  # Pause Other Audio under Microphone (test_mediapause)
        self.assertEqual([(row.kind, row.props["title"]) for row in (microphone, camera)],
                         [("Adw.SwitchRow", "Microphone"), ("Adw.SwitchRow", "Camera")])
        self.assertEqual(host.device_rows, {"microphone": microphone, "camera": camera})
        self.assertIs(host._rows["microphone"], microphone)  # followed like every other switch
        self.assertTrue(microphone.get_active() and camera.get_active())

    def test_switches_show_and_write_the_settings(self):
        host, _page = self.open(microphone=False)
        self.assertFalse(host.device_rows["microphone"].get_active())
        self.assertTrue(host.device_rows["camera"].get_active())
        self.assertEqual(host.settings.written, [])  # showing them isn't choosing
        host.device_rows["camera"].set_active(False)
        host.device_rows["microphone"].set_active(True)
        self.assertEqual(host.settings.written, [("camera", False), ("microphone", True)])

    def test_follow_changes_made_elsewhere(self):
        host, _page = self.open(desktop="yes")
        camera = host.device_rows["camera"]
        host.settings.change_elsewhere("camera", False)  # the main menu, Go to Chat, a toast's Turn On
        host._on_setting("camera", False)
        self.assertFalse(camera.get_active())
        self.assertEqual(camera.subtitle(), line("camera", on=False))
        host.settings.change_elsewhere("camera", True)
        host._on_setting("camera", True)
        self.assertTrue(camera.get_active())
        self.assertEqual(camera.subtitle(), line("camera", desktop="yes"))
        self.assertEqual(host.settings.written, [])  # nothing written back

    def test_lines_come_from_device_subtitle(self):
        cases = [dict(desktop="yes"), dict(desktop="no"), dict(desktop="ask"), dict(desktop=None),
                 dict(lockdown=True), dict(present=False), dict(camera=False, desktop="no"),
                 dict(microphone=False), dict(sandboxed=True)]
        for case in cases:
            with self.subTest(**case):
                host, _page = self.open(**dict(case))
                for kind, row in host.device_rows.items():
                    state = host.app.media_state(kind)
                    self.assertEqual(row.subtitle(), line(kind, state["on"], state["desktop"], state["present"],
                                                          state["lockdown"], state["sandboxed"]))

    def test_restart_is_passed_on(self):
        # The camera can't work again until NAME restarts (app.py's media_state says so).
        host, _page = self.open(desktop="yes")
        state = host.app.media_state
        host.app.media_state = lambda kind: dict(state(kind), restart=kind == "camera")
        host._show_devices()
        self.assertEqual(host.device_rows["camera"].subtitle(), line("camera", desktop="yes", restart=True))
        self.assertEqual(host.device_rows["microphone"].subtitle(), line("microphone"))

    def test_lines_keep_up_with_the_desktop(self):
        host, _page = self.open(desktop="yes")
        camera = host.device_rows["camera"]
        host.app.desktop = "no"  # changed in the desktop's own settings
        host._show_devices()  # what app.watch_media calls
        self.assertEqual(camera.subtitle(), line("camera", desktop="no"))
        self.assertTrue(host.ask_again.visible())
        host.app.present = False  # unplugged
        host._show_devices()
        self.assertEqual(camera.subtitle(), line("camera", desktop="no", present=False))

    def test_a_line_is_set_only_when_it_changes(self):
        host, _page = self.open(desktop="yes")
        for _ in range(3):
            host._show_devices()
        self.assertEqual(len(host.device_rows["camera"].called("set_subtitle")), 1)

    def test_on_by_default(self):
        self.assertIs(DEFAULTS.get("microphone"), True)
        self.assertIs(DEFAULTS.get("camera"), True)


class WordingTest(unittest.TestCase):
    """The lines under the switches, from the camera report's table: the switch is NAME's choice,
    the line what the desktop or the hardware makes of it."""

    def test_the_table(self):
        self.assertEqual(line("microphone"), "WhatsApp can use it for calls and voice messages")
        self.assertEqual(line("microphone", present=False), "No microphone found")
        self.assertEqual(line("microphone", on=False), "WhatsApp can't use it")
        self.assertEqual(line("camera", desktop="yes"), "WhatsApp can use it for video calls")
        self.assertEqual(line("camera", desktop="ask"), "Your desktop will ask the first time WhatsApp uses it")
        self.assertEqual(line("camera", desktop=None, sandboxed=True),
                         "Your desktop will ask the first time WhatsApp uses it")  # the Flatpak can't tell
        self.assertEqual(line("camera", desktop="no"), "Blocked by your desktop's privacy settings")
        self.assertEqual(line("camera", desktop="yes", lockdown=True),
                         "Your desktop has the camera turned off for all apps")
        self.assertEqual(line("camera", desktop="yes", present=False), "No camera found")

    def test_off_wins(self):
        for desktop in ("yes", "no", "ask", None):
            for lockdown in (False, True):
                for present in (True, False):
                    with self.subTest(desktop=desktop, lockdown=lockdown, present=present):
                        self.assertEqual(line("camera", False, desktop, present, lockdown), "WhatsApp can't use it")

    def test_lines_are_plain(self):
        # Straight apostrophes and no full stop, as the other rows' lines.
        for kind in ("microphone", "camera"):
            for on in (True, False):
                for desktop in ("yes", "no", "ask", None):
                    for present in (True, False):
                        for lockdown in (True, False):
                            for sandboxed in (True, False):
                                text = line(kind, on, desktop, present, lockdown, sandboxed)
                                self.assertTrue(text and not text.endswith("."), text)
                                self.assertNotIn("’", text)


# ---- When Ask Again… and How to Allow… show ----------------------------------------------------------


class VisibilityTest(Privacy):
    def test_rules(self):
        # (on, desktop, lockdown, sandboxed) → which row
        self.assertEqual(camera_help(True, "no", False, False), "ask")
        for desktop in ("yes", "ask", None):
            self.assertEqual(camera_help(True, desktop, False, False), "", desktop)
        self.assertEqual(camera_help(False, "no", False, False), "")  # NAME's own no comes first
        self.assertEqual(camera_help(True, "no", True, False), "")  # no answer for NAME gets past lockdown
        for desktop in ("no", "ask", None):  # the Flatpak can't read the answer, so it can't rule it out
            self.assertEqual(camera_help(True, desktop, False, True), "how", desktop)
        self.assertEqual(camera_help(True, "yes", False, True), "")
        self.assertEqual(camera_help(False, None, False, True), "")
        self.assertEqual(camera_help(True, None, True, True), "")

    def test_native_and_blocked_shows_ask_again(self):
        host, page = self.open(desktop="no")
        help_ = page.added()[2]
        self.assertEqual(help_.added(), [host.ask_again, host.how_to_allow])
        self.assertEqual(host.ask_again.props["title"], "Ask Again…")
        self.assertEqual(host.how_to_allow.props["title"], "How to Allow…")
        self.assertTrue(help_.visible() and host.ask_again.visible())
        self.assertFalse(host.how_to_allow.visible())

    def test_allowed_shows_neither(self):
        for state in (dict(desktop="yes"), dict(desktop=None), dict(desktop="no", camera=False),
                      dict(desktop="no", lockdown=True), dict(desktop="yes", sandboxed=True)):
            with self.subTest(**state):
                host, page = self.open(**dict(state))
                self.assertFalse(page.added()[2].visible())
                self.assertFalse(host.ask_again.visible())
                self.assertFalse(host.how_to_allow.visible())

    def test_flatpak_shows_how_to_allow(self):
        host, page = self.open(desktop=None, sandboxed=True)
        self.assertTrue(page.added()[2].visible() and host.how_to_allow.visible())
        self.assertFalse(host.ask_again.visible())  # Ask Again… is native only

    def test_turning_the_camera_off_takes_it_away(self):
        host, page = self.open(desktop="no")
        host.device_rows["camera"].set_active(False)
        host._on_setting("camera", False)
        self.assertFalse(page.added()[2].visible())
        host.device_rows["camera"].set_active(True)
        host._on_setting("camera", True)
        self.assertTrue(page.added()[2].visible())

    def test_warning_only_while_blocked(self):
        self.assertTrue(camera_blocked(True, "no", False))
        self.assertTrue(camera_blocked(True, "yes", True))
        self.assertFalse(camera_blocked(True, "yes", False))
        self.assertFalse(camera_blocked(True, None, False))
        self.assertFalse(camera_blocked(False, "no", True))  # off says it all
        host, _page = self.open(desktop="no")
        camera = host.device_rows["camera"]
        self.assertEqual(warned(camera), [("add_prefix", host.camera_warning)])
        host._show_devices()  # still blocked: put in once
        self.assertEqual(warned(camera), [("add_prefix", host.camera_warning)])
        host.app.desktop = "yes"
        host._show_devices()
        # Taken out, not hidden: a hidden prefix still moves the title along.
        self.assertEqual(warned(camera)[-1], ("remove", host.camera_warning))
        self.assertEqual(host.camera_warning.called("set_visible"), [])
        host, _page = self.open(desktop="yes")
        self.assertEqual(warned(host.device_rows["camera"]), [])


# ---- Ask Again… ---------------------------------------------------------------------------------------


class AskAgainTest(Privacy):
    def ask(self, response="ask"):
        host, _page = self.open(desktop="no")
        host.ask_again.handler("activated")(host.ask_again)
        alert, = Alert.made
        alert.answer(response)
        return host, alert

    def test_the_dialog(self):
        _host, alert = self.ask("cancel")
        self.assertEqual(alert.props, {"heading": "Ask Again for the Camera?",
                                       "body": "Your desktop will ask you now."})
        self.assertEqual(alert.responses(), [("cancel", "_Cancel"), ("ask", "_Ask Again")])
        self.assertEqual(alert.called("set_response_appearance"), [("ask", "Adw.ResponseAppearance.SUGGESTED")])
        self.assertEqual(alert.called("set_close_response"), [("cancel",)])

    def test_cancel_asks_nothing(self):
        host, _alert = self.ask("cancel")
        self.assertEqual(host.app.asks, [])
        self.assertTrue(host.ask_again.sensitive())

    def test_ask_runs_the_portal_flow_once(self):
        host, _alert = self.ask()
        self.assertEqual(len(host.app.asks), 1)
        self.assertFalse(host.ask_again.sensitive())  # one question at a time

    def test_the_answer_is_shown_and_said_once(self):
        host, _alert = self.ask()
        camera = host.device_rows["camera"]
        self.assertEqual(camera.called("announce"), [])
        host.app.desktop = "yes"  # the desktop's dialog answered Allow, and the store was read again
        host.app.asks[0]("yes")
        self.assertTrue(host.ask_again.sensitive())
        self.assertEqual(camera.subtitle(), line("camera", desktop="yes"))
        self.assertFalse(host.ask_again.visible())
        self.assertEqual(camera.called("announce"), [(line("camera", desktop="yes"), MEDIUM)])
        self.assertEqual(host.toasts, [])

    def test_said_before_the_store_is_read_again(self):
        # The answer can come back before the desktop's store is read again (the line follows
        # through watch_media): what's said is what the answer makes of it.
        host, _alert = self.ask()
        camera = host.device_rows["camera"]
        host.app.asks[0]("yes")
        self.assertEqual(camera.called("announce"), [(line("camera", desktop="yes"), MEDIUM)])
        host.app.desktop = "yes"
        host._show_devices()
        self.assertEqual(camera.subtitle(), line("camera", desktop="yes"))
        self.assertEqual(len(camera.called("announce")), 1)  # the line catching up isn't said again

    def test_a_refusal_is_said_too(self):
        host, _alert = self.ask()
        host.app.asks[0]("no")
        self.assertEqual(host.device_rows["camera"].called("announce"), [(line("camera", desktop="no"), MEDIUM)])
        self.assertTrue(host.ask_again.visible())

    def test_when_the_desktop_couldnt_ask(self):
        host, _alert = self.ask()
        host.app.asks[0](None)
        toast, = host.toasts
        self.assertEqual(toast.props["title"], "Your desktop couldn't ask about the camera")
        self.assertEqual(host.device_rows["camera"].called("announce"), [])  # the toast says it
        self.assertTrue(host.ask_again.sensitive() and host.ask_again.visible())  # to try again

    def test_closed_meanwhile(self):
        host, _alert = self.ask()
        host._open = False  # as _closed leaves it
        host.app.asks[0]("yes")
        self.assertEqual(host.device_rows["camera"].called("announce"), [])
        self.assertTrue(host.ask_again.sensitive())

    def test_background_changes_arent_announced(self):
        host, _page = self.open(desktop="yes")
        host.app.desktop = "no"
        host._show_devices()
        host._on_setting("camera", True)
        self.assertEqual(host.device_rows["camera"].called("announce"), [])


# ---- How to Allow… --------------------------------------------------------------------------------


class HowToAllowTest(Privacy):
    def show(self):
        host, _page = self.open(sandboxed=True)
        host.how_to_allow.handler("activated")(host.how_to_allow)
        alert, = Alert.made
        return host, alert

    def test_the_dialog(self):
        _host, alert = self.show()
        self.assertEqual(alert.props["heading"], "How to Allow the Camera")
        self.assertIn(NAME, alert.props["body"])
        self.assertIn("privacy settings", alert.props["body"])
        self.assertEqual(alert.responses(), [("close", "_Close"), ("copy", "Co_py Command")])
        self.assertEqual(alert.called("set_default_response"), [("copy",)])
        self.assertEqual(alert.called("set_close_response"), [("close",)])
        (command,), = alert.called("set_extra_child")
        self.assertEqual(command.kind, "Gtk.Label")
        self.assertEqual(command.props["label"], f"flatpak permission-remove devices camera {APP_ID}")
        self.assertTrue(command.props["selectable"])
        self.assertIn("monospace", command.props["css_classes"])
        # Not the dialog's first focus, which would open with the whole command selected.
        self.assertEqual(command.called("set_focusable"), [(False,)])

    def test_copy_command(self):
        host, alert = self.show()
        alert.answer("copy")
        self.assertEqual(host.clipboard, [f"flatpak permission-remove devices camera {APP_ID}"])

    def test_close_copies_nothing(self):
        host, alert = self.show()
        alert.answer("close")
        self.assertEqual(host.clipboard, [])

    def test_only_natters_own_answer(self):
        # Without the app id, permission-remove forgets every app's camera answer, and
        # permission-reset all of NAME's permissions.
        source = inspect.getsource(Preferences._how_to_allow)
        self.assertIn('f"flatpak permission-remove devices camera {APP_ID}"', source)
        self.assertNotIn("permission-reset", source)


# ---- Screen readers -------------------------------------------------------------------------------


class AccessibilityTest(Privacy):
    def test_names_and_descriptions(self):
        # A switch row's title is its name for screen readers and the line under it its
        # description (libadwaita), so the state is said in words, never by colour alone.
        host, _page = self.open(desktop="no")
        for kind, title in (("microphone", "Microphone"), ("camera", "Camera")):
            row = host.device_rows[kind]
            self.assertEqual(row.props["title"], title)
            self.assertTrue(row.subtitle())
        self.assertEqual(host.device_rows["camera"].subtitle(), line("camera", desktop="no"))

    def test_the_warning_is_decoration(self):
        host, _page = self.open(desktop="no")
        self.assertEqual(host.camera_warning.props["accessible_role"], "Gtk.AccessibleRole.PRESENTATION")
        self.assertEqual(host.camera_warning.props["icon_name"], "dialog-warning-symbolic")

    def test_buttons_are_rows_with_words(self):
        host, _page = self.open(desktop="no")
        for row in (host.ask_again, host.how_to_allow):
            self.assertIn(row.kind, ("Adw.ButtonRow", "Adw.ActionRow"))
            self.assertTrue(row.props["title"].endswith("…"))  # it opens a dialog first

    def test_announced_once_with_medium_priority(self):
        source = inspect.getsource(Preferences._ask_camera_again)
        self.assertEqual(source.count(".announce("), 1)
        self.assertIn("Gtk.AccessibleAnnouncementPriority.MEDIUM", source)
        self.assertNotIn(".announce(", inspect.getsource(Preferences._show_devices))


# ---- Watching while open --------------------------------------------------------------------------


class WatchTest(unittest.TestCase):
    def test_watched_from_opening(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(Preferences.__init__)))
        watched = [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "watch_media"]
        self.assertEqual(watched, ["app.watch_media(self._show_devices)"])

    def closing(self):
        app = App(Settings())
        dialog = SimpleNamespace(settings=SimpleNamespace(disconnect=lambda _listener: None), app=app,
                                 _on_setting=None, _forget_accounts=lambda: None, _open=True, _media_watch="watch-7")
        return dialog, app

    def test_unwatched_on_closing(self):
        dialog, app = self.closing()
        Preferences._closed(dialog)
        self.assertEqual(app.unwatched, ["watch-7"])
        self.assertFalse(dialog._open)

    def test_unwatched_once(self):
        dialog, app = self.closing()
        Preferences._closed(dialog)
        Preferences._closed(dialog)
        self.assertEqual(app.unwatched, ["watch-7"])


# ---- The main menu --------------------------------------------------------------------------------


class MenuTest(unittest.TestCase):
    def sections(self, calls=None):
        menu = window.main_menu(SimpleNamespace(calls=calls))
        found = []
        for i in range(menu.get_n_items()):
            section = menu.get_item_link(i, "section")
            items = []
            for j in range(section.get_n_items()):
                attributes = {}
                for name in ("label", "action", "target"):
                    value = section.get_item_attribute_value(j, name, None)
                    attributes[name] = value.unpack() if value is not None else None
                items.append(attributes)
            found.append(items)
        return found

    def test_a_section_of_their_own_after_the_first(self):
        for calls in (None, object()):
            sections = self.sections(calls)
            self.assertEqual([(item["label"], item["action"]) for item in sections[1]],
                             [("_Microphone", "app.microphone"), ("Cam_era", "app.camera")])
            self.assertEqual(sections[0][0]["action"], "win.go-to-chat")
            self.assertEqual(sections[2][0]["action"], "app.preferences")

    def test_check_items(self):
        # No target: a stateful boolean action without one is shown as a check item (a target
        # would make it a radio item).
        for item in self.sections()[1]:
            self.assertIsNone(item["target"])

    def test_their_letters_are_free(self):
        for calls in (None, object()):
            labels = [item["label"] for section in self.sections(calls) for item in section]
            letters = [label[label.index("_") + 1].lower() for label in labels]
            self.assertEqual(len(letters), len(set(letters)), labels)
            self.assertIn("m", letters)
            self.assertIn("e", letters)


# ---- Go to Chat's commands ------------------------------------------------------------------------


class PaletteTest(unittest.TestCase):
    def test_labels_follow_the_setting(self):
        self.assertEqual(device_command("microphone", True), ("Turn Off Microphone", "microphone-disabled-symbolic"))
        self.assertEqual(device_command("microphone", False), ("Turn On Microphone", "audio-input-microphone-symbolic"))
        self.assertEqual(device_command("camera", True), ("Turn Off Camera", "camera-disabled-symbolic"))
        self.assertEqual(device_command("camera", False), ("Turn On Camera", "camera-video-symbolic"))

    def palette(self, **values):
        settings = Settings(**values)
        settings.do_not_disturb = lambda: False
        actions = []
        app = SimpleNamespace(settings=settings, accounts=[], window=SimpleNamespace(account=None), can_call=False,
                              lock_enabled=False,
                              activate_action=lambda name, target: actions.append((name, target)),
                              change_action_state=lambda name, value: actions.append((name, value)))
        fake = SimpleNamespace(app=app, files=[])
        fake._turn_device = lambda kind, on: Palette._turn_device(fake, kind, on)
        return fake, actions

    def listed(self, fake, query):
        return {entry.title: entry for entry in Palette.entries(fake, query)}

    def test_listed_as_the_switches_are(self):
        fake, _actions = self.palette(microphone=True, camera=False)
        found = self.listed(fake, "microphone")
        self.assertIn("Turn Off Microphone", found)
        self.assertNotIn("Turn On Microphone", found)
        found = self.listed(fake, "camera")
        self.assertIn("Turn On Camera", found)
        self.assertNotIn("Turn Off Camera", found)
        self.assertEqual(found["Turn On Camera"].icon, "camera-video-symbolic")
        self.assertFalse(found["Turn On Camera"].avatar)

    def test_turn_off(self):
        fake, actions = self.palette()
        self.listed(fake, "turn off cam")["Turn Off Camera"].run()
        (name, value), = actions
        self.assertEqual(name, "camera")
        self.assertTrue(value.equal(GLib.Variant("b", False)))

    def test_turn_on(self):
        fake, actions = self.palette(microphone=False)
        entry = self.listed(fake, "turn on mic")["Turn On Microphone"]
        fake.app.settings.change_elsewhere("microphone", True)  # turned on meanwhile: still on after
        entry.run()
        (name, target), = actions
        self.assertEqual(name, "allow-media")
        self.assertTrue(target.equal(GLib.Variant("s", "microphone")))

    def test_not_while_sending_files(self):
        fake, _actions = self.palette()
        fake.files = [Path("/tmp/a.png")]
        self.assertNotIn("Turn Off Camera", self.listed(fake, "camera"))


# ---- The toast when WhatsApp is refused -----------------------------------------------------------


class ToastWordsTest(unittest.TestCase):
    def test_off(self):
        for kind in ("camera", "microphone"):
            title, button, action, target = refused_toast(kind, "off")
            self.assertEqual(title, f"The {kind} is off in {NAME}")
            self.assertEqual((button, action), ("Turn On", "app.allow-media"))
            self.assertTrue(target.equal(GLib.Variant("s", kind)))  # turns it on, never over

    def test_desktop(self):
        title, button, action, target = refused_toast("camera", "desktop")
        self.assertEqual(title, f"Your desktop blocks the camera for {NAME}")
        self.assertEqual((button, action), ("Details", "app.preferences-page"))
        self.assertTrue(target.equal(GLib.Variant("s", "privacy")))

    def test_waiting(self):
        # Refused before the desktop's answer could be read: Privacy says it's waiting for it.
        title, button, action, target = refused_toast("camera", "waiting")
        self.assertEqual(title, "Waiting for your desktop's privacy settings")
        self.assertEqual((button, action), ("Details", "app.preferences-page"))
        self.assertTrue(target.equal(GLib.Variant("s", "privacy")))
        self.assertEqual(refused_toast("camera", "waiting", "Work")[0],
                         "Waiting for your desktop's privacy settings (Work)")

    def test_names_the_account(self):
        # In brackets, as app.py names them in its own toasts about the microphone and camera.
        self.assertEqual(refused_toast("camera", "off", "Work")[0], f"The camera is off in {NAME} (Work)")
        self.assertEqual(refused_toast("camera", "desktop", "R&D")[0],
                         f"Your desktop blocks the camera for {NAME} (R&D)")

    def test_privacy_is_a_page(self):
        from natter.app import PREFERENCES_PAGES

        self.assertIn("privacy", PREFERENCES_PAGES)


class RateLimitTest(unittest.TestCase):
    def test_pure(self):
        self.assertTrue(refused_due(None, 5.0))
        self.assertFalse(refused_due(100.0, 100.0))
        self.assertFalse(refused_due(100.0, 100.0 + REFUSED_GAP - 0.01))
        self.assertTrue(refused_due(100.0, 100.0 + REFUSED_GAP))
        self.assertEqual(REFUSED_GAP, 10.0)


class Account(SimpleNamespace):
    pass


class RefusedToastTest(unittest.TestCase):
    def window(self, accounts=1, locked=False):
        toasts = []
        fake = SimpleNamespace(_refused_at={}, toasts=toasts,
                               app=SimpleNamespace(accounts=[Account(name=f"A{i}") for i in range(accounts)],
                                                   locked=locked),
                               toast=lambda *args, **kwargs: toasts.append((args, kwargs)))
        return fake

    def refuse(self, fake, at, kind="camera", reason="off", account=None):
        with mock.patch.object(window.time, "monotonic", lambda: at):
            Window._media_refused(fake, account or fake.app.accounts[0], kind, reason)

    def test_one_per_kind_every_ten_seconds(self):
        fake = self.window()
        self.refuse(fake, 100.0)
        self.refuse(fake, 101.0)
        self.refuse(fake, 100.0 + REFUSED_GAP - 0.5, reason="desktop")  # the same kind, another reason
        self.assertEqual(len(fake.toasts), 1)
        self.refuse(fake, 103.0, kind="microphone")  # another kind has its own
        self.assertEqual(len(fake.toasts), 2)
        self.refuse(fake, 100.0 + REFUSED_GAP)
        self.assertEqual(len(fake.toasts), 3)

    def test_the_toast(self):
        fake = self.window()
        self.refuse(fake, 50.0, "camera", "desktop")
        (args, kwargs), = fake.toasts
        title, button, action, target = args
        self.assertEqual((title, button, action), (f"Your desktop blocks the camera for {NAME}", "Details",
                                                   "app.preferences-page"))
        self.assertTrue(target.equal(GLib.Variant("s", "privacy")))
        self.assertEqual(kwargs, {"timeout": 8})

    def test_names_the_account_only_with_several(self):
        fake = self.window(accounts=1)
        self.refuse(fake, 1.0)
        self.assertEqual(fake.toasts[0][0][0], f"The camera is off in {NAME}")
        fake = self.window(accounts=2)
        self.refuse(fake, 1.0, account=fake.app.accounts[1])
        self.assertEqual(fake.toasts[0][0][0], f"The camera is off in {NAME} (A1)")

    def test_waiting_is_said(self):
        fake = self.window()
        self.refuse(fake, 1.0, reason="waiting")
        self.assertEqual(fake.toasts[0][0][0], "Waiting for your desktop's privacy settings")

    def test_nothing_unknown(self):
        fake = self.window()
        self.refuse(fake, 1.0, kind="screen")
        self.refuse(fake, 2.0, reason="busy")
        self.assertEqual(fake.toasts, [])
        self.assertEqual(fake._refused_at, {})

    def test_not_behind_the_lock(self):
        fake = self.window(locked=True)
        self.refuse(fake, 1.0)
        self.assertEqual(fake.toasts, [])
        fake.app.locked = False
        self.refuse(fake, 2.0)  # the one behind the lock didn't count
        self.assertEqual(len(fake.toasts), 1)

    def test_every_account_is_heard(self):
        source = inspect.getsource(Window.add_account)
        self.assertIn('account.connect("media-refused", self._media_refused)', source)


# ---- Ban safety -----------------------------------------------------------------------------------


class BanSafetyTest(unittest.TestCase):
    """The switches' UI acts only through NAME's own settings and actions: it never runs anything
    in WhatsApp's page, reads its storage or sends it events."""

    NEW = [Preferences._device_groups, Preferences._show_devices, Preferences._ask_camera_again,
           Preferences._how_to_allow, Window._media_refused, Palette._turn_device, window.refused_toast,
           window.main_menu, palette.device_command]

    def test_nothing_in_the_page(self):
        for function in self.NEW:
            source = inspect.getsource(function)
            for banned in (".run(", "evaluate_javascript", "call_async_javascript_function", "localStorage",
                           "indexedDB", "dispatchEvent", "getUserMedia", "set_microphone_capture_state",
                           "set_camera_capture_state"):
                with self.subTest(function=function.__qualname__, banned=banned):
                    self.assertNotIn(banned, source)

    def test_no_desktop_permissions_written_here(self):
        # Ask Again's portal flow is the app's (app.ask_camera_again, after the dialog's yes).
        for path in ("prefs.py", "window.py", "palette.py"):
            source = (ROOT / "natter" / path).read_text()
            for banned in ("PermissionStore", "DeletePermission", "SetPermission", "AccessCamera"):
                self.assertNotIn(banned, source, path)


if __name__ == "__main__":
    unittest.main()
