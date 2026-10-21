"""Preferences without a window: the passcode dialog's checks, the Accounts page's status line,
folders written with ~, what the dialog's words and pages promise (alert mnemonics, page names),
and the Theme picker (its groups, its one radio group, Custom's colour, its layout and its keys),
built from stand-in widgets so no display is needed."""

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk

from natter import NAME, lock, prefs, themes
from natter.prefs import Preferences, ThemeRow, account_status, mismatched, passcode_ready, shown_folder
from natter.settings import DEFAULTS

PREFS = Path(__file__).resolve().parent.parent / "natter" / "prefs.py"


def calls(name: str) -> list[ast.Call]:
    """Every call in prefs.py to a function or method called `name`."""
    tree = ast.parse(PREFS.read_text())
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and
            (getattr(node.func, "attr", None) == name or getattr(node.func, "id", None) == name)]


class Account:
    def __init__(self, ready=False, needs_link=False, failed=None, crashing=False, memory_stopped=False):
        self.ready, self.needs_link, self.failed, self.crashing = ready, needs_link, failed, crashing
        self.memory_stopped = memory_stopped


class FolderTest(unittest.TestCase):
    def test_home_is_a_tilde(self):
        self.assertEqual(shown_folder("/home/ana/Downloads", "/home/ana"), "~/Downloads")
        self.assertEqual(shown_folder("/home/ana/Media/R&D", "/home/ana"), "~/Media/R&D")
        self.assertEqual(shown_folder("/home/ana", "/home/ana"), "~")

    def test_elsewhere_stays_as_it_is(self):
        self.assertEqual(shown_folder("/mnt/data/WhatsApp", "/home/ana"), "/mnt/data/WhatsApp")
        # A folder whose name only starts like the home folder's isn't in it.
        self.assertEqual(shown_folder("/home/anabel/Downloads", "/home/ana"), "/home/anabel/Downloads")

    def test_takes_paths(self):
        self.assertEqual(shown_folder(Path.home() / "Downloads"), "~/Downloads")


class AccountStatusTest(unittest.TestCase):
    def test_each_state(self):
        self.assertEqual(account_status(Account(ready=True)), "Linked")
        self.assertEqual(account_status(Account(needs_link=True)), "Needs linking")
        self.assertEqual(account_status(Account()), "Loading…")
        self.assertEqual(account_status(Account(failed="Error resolving the name")), "Can't reach WhatsApp")
        self.assertEqual(account_status(Account(crashing=True)), "Keeps stopping")
        self.assertEqual(account_status(Account(crashing=True, memory_stopped=True)), "Used too much memory")

    def test_needing_a_link_comes_first(self):
        # A page that crashed on the link screen still needs the phone first.
        self.assertEqual(account_status(Account(needs_link=True, crashing=True)), "Needs linking")


class PasscodeDialogTest(unittest.TestCase):
    def test_a_new_passcode_must_be_long_enough_and_typed_twice(self):
        short = "1" * (lock.SHORTEST - 1)
        good = "1" * lock.SHORTEST
        self.assertFalse(passcode_ready("", "", "", needs_current=False, remove=False))
        self.assertFalse(passcode_ready("", short, short, needs_current=False, remove=False))
        self.assertFalse(passcode_ready("", good, good + "2", needs_current=False, remove=False))
        self.assertTrue(passcode_ready("", good, good, needs_current=False, remove=False))

    def test_changing_needs_the_current_passcode(self):
        self.assertFalse(passcode_ready("", "abcd", "abcd", needs_current=True, remove=False))
        self.assertTrue(passcode_ready("1234", "abcd", "abcd", needs_current=True, remove=False))

    def test_removing_needs_only_the_current_passcode(self):
        self.assertFalse(passcode_ready("", "", "", needs_current=True, remove=True))
        self.assertTrue(passcode_ready("1", "", "", needs_current=True, remove=True))
        self.assertTrue(passcode_ready("", "", "", needs_current=False, remove=True))

    def test_mismatch_shows_only_once_it_cant_match(self):
        self.assertFalse(mismatched("2468", ""))
        self.assertFalse(mismatched("2468", "24"))  # still typing
        self.assertFalse(mismatched("2468", "2468"))
        self.assertTrue(mismatched("2468", "25"))  # a typo
        self.assertTrue(mismatched("2468", "24680"))  # one too many
        self.assertTrue(mismatched("", "2"))

    def test_the_dialogs_name_no_files(self):
        # How to get in without the passcode belongs on the lock page, not in the dialog that sets one.
        bodies = [keyword.value for call in calls("AlertDialog") for keyword in call.keywords
                  if keyword.arg == "body"]
        self.assertTrue(bodies)
        self.assertEqual([node.attr for body in bodies for node in ast.walk(body)
                          if isinstance(node, ast.Attribute) and node.attr == "path"], [])


class WordsTest(unittest.TestCase):
    def test_alert_responses_have_mnemonics(self):
        labels = []
        for call in calls("add_response"):
            for value in ast.walk(call.args[1]):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    labels.append(value.value)
        self.assertTrue(labels)
        self.assertEqual([label for label in labels if "_" not in label], [])

    def test_pages_have_names(self):
        # app.py opens a page by these (Adw.PreferencesDialog.set_visible_page_name).
        names = [keyword.value.value for call in calls("PreferencesPage") for keyword in call.keywords
                 if keyword.arg == "name"]
        self.assertEqual(names, ["general", "notifications", "privacy", "accounts"])


class FollowTest(unittest.TestCase):
    """Rows that follow settings changed elsewhere while the dialog is open."""

    def fake(self, zoom: float):
        spin = type("Spin", (), {})()
        spin.value = 120.0
        spin.sets = []
        spin.get_value = lambda: spin.value
        spin.set_value = lambda value: (spin.sets.append(value), setattr(spin, "value", value))
        prefs = type("Prefs", (), {})()
        prefs._rows, prefs.settings, prefs.zoom = {}, {"zoom": zoom}, spin
        return prefs, spin

    def test_zoom_follows_the_keyboard(self):
        prefs, spin = self.fake(0.9)
        Preferences._on_setting(prefs, "zoom", 0.9)
        self.assertEqual(spin.sets, [90])

    def test_zoom_already_shown_is_left_alone(self):
        prefs, spin = self.fake(1.2)
        Preferences._on_setting(prefs, "zoom", 1.2)
        self.assertEqual(spin.sets, [])

    def test_closing_lets_go_of_the_accounts(self):
        dropped = []
        account = type("Account", (), {"disconnect": lambda _a, handler: dropped.append(handler)})()
        listeners = []
        prefs = type("Prefs", (), {})()
        prefs._account_handlers = [(account, 7), (account, 9)]
        prefs.settings = type("Settings", (), {"disconnect": lambda _s, listener: listeners.append(listener)})()
        prefs._on_setting = "listener"
        prefs._forget_accounts = lambda: Preferences._forget_accounts(prefs)
        # The microphone and camera lines stop following the desktop (test_media_ui.py).
        unwatched = []
        prefs.app, prefs._open, prefs._media_watch = SimpleNamespace(unwatch_media=unwatched.append), True, 3
        Preferences._closed(prefs)
        self.assertEqual((dropped, prefs._account_handlers, listeners), ([7, 9], [], ["listener"]))
        self.assertEqual(unwatched, [3])


# ---- The Theme picker ------------------------------------------------------------------------


class Widget:
    """Stands in for any GTK or libadwaita widget: keeps what it was made with and every call
    made on it. `kind` is the name it was made by, such as "Adw.ComboRow"."""

    def __init__(self, *args, **props):
        self.args, self.props, self.calls, self.kind = args, props, [], ""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, *args))

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]

    def added(self):
        """The children put in it, in order, however they were put in."""
        return [call[1] for call in self.calls if call[0] in ("add", "append", "set_child")]

    def visible(self):
        """Whether it's shown, as the last set_visible left it (shown unless made hidden)."""
        shown = self.called("set_visible")
        return shown[-1][0] if shown else self.props.get("visible", True)


class Swatch(Widget):
    """A check button in a group, as GTK runs one: turning one on first turns off the one that
    was on, and each that changes says "toggled"."""

    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.active, self.group, self.handlers = False, [self], []

    def set_group(self, other):
        self.group = other.group
        self.group.append(self)

    def connect(self, signal, handler, *data):
        self.handlers.append((signal, handler, data))

    def get_active(self):
        return self.active

    def set_active(self, active):
        if active == self.active:
            return
        changed = [other for other in self.group if active and other is not self and other.active]
        for other in changed:
            other.active = False
        self.active = active
        for swatch in changed + [self]:
            for signal, handler, data in swatch.handlers:
                if signal == "toggled":
                    handler(swatch, *data)


class ColourButton(Widget):
    """A Gtk.ColorDialogButton: holds a colour, and says notify::rgba when it changes (picked in
    its dialog, or set)."""

    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.rgba, self.handlers = None, []

    def connect(self, signal, handler, *data):
        self.handlers.append((signal, handler, data))

    def get_rgba(self):
        return self.rgba

    def set_rgba(self, rgba):
        self.calls.append(("set_rgba", prefs.hex_colour(rgba)))
        self.rgba = rgba
        for signal, handler, data in self.handlers:
            if signal == "notify::rgba":
                handler(self, None, *data)

    def pick(self, colour):
        """The colour dialog answered with `colour`."""
        rgba = Gdk.RGBA()
        self.assert_parsed = rgba.parse(colour)
        self.set_rgba(rgba)


class Name(str):
    """A name in a stand-in module, such as "Gtk.AccessibleRole.RADIO": its own value as an enum,
    a Widget maker when called."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Name(f"{self}.{name}")

    def __call__(self, *args, **props):
        widget = {"Gtk.CheckButton": Swatch, "Gtk.ColorDialogButton": ColourButton}.get(self, Widget)(*args, **props)
        widget.kind = str(self)
        return widget


class Settings(dict):
    """Natter's settings as the dialog sees them, keeping a list of what it writes."""

    def __init__(self, **values):
        super().__init__({**DEFAULTS, "theme": themes.DEFAULT, **values})
        self.written = []
        self.download_dir = Path.home() / "Downloads"

    def __setitem__(self, key, value):
        self.written.append((key, value))
        super().__setitem__(key, value)

    def change_elsewhere(self, key, value):
        """A change made outside the dialog (another window, settings.json by hand)."""
        super().__setitem__(key, value)


class Host:
    """Preferences' own methods on a plain object, so neither a dialog nor a display is needed."""

    def __init__(self, settings):
        self.settings, self._rows = settings, {}
        self.app = SimpleNamespace(media=lambda _show: None, has_tray_host=lambda: True,
                                   reclaimer=SimpleNamespace(available=False))

    def __getattr__(self, name):
        member = getattr(Preferences, name)
        return member.__get__(self) if inspect.isfunction(member) else member


IDS = [theme_id for theme_id, _, _ in themes.CHOICES]
NAMES = {theme_id: name for theme_id, name, _ in themes.CHOICES}
STAND_INS = dict(Gtk=Name("Gtk"), Adw=Name("Adw"), ThemeRow=Name("ThemeRow"), SwatchGrid=Name("SwatchGrid"),
                 menu_supported=lambda: False)


class ThemePickerTest(unittest.TestCase):
    def setUp(self):
        patches = mock.patch.multiple(prefs, **STAND_INS)
        patches.start()
        self.addCleanup(patches.stop)

    def open(self, theme=themes.DEFAULT, custom=None):
        """The General page as the dialog builds it, with `theme` (and Custom's colour) in the settings."""
        values = {"theme": theme} if custom is None else {"theme": theme, "theme_custom": custom}
        host = Host(Settings(**values))
        page = host._general()
        return host, page

    def appearance(self, page):
        group, = [group for group in page.added() if group.props.get("title") == "Appearance"]
        return group.added()

    def picker(self, page):
        """The Theme row's line at the top (name and chosen theme), and its groups."""
        row = self.appearance(page)[1]
        box, = row.added()
        top, *groups = box.added()
        return top, groups

    def swatches(self, host):
        return [host.theme_buttons[theme_id] for theme_id in IDS]

    def chosen(self, host):
        return [theme_id for theme_id in IDS if host.theme_buttons[theme_id].active]

    def test_between_style_and_zoom(self):
        host, page = self.open()
        style, theme, custom, zoom = self.appearance(page)
        self.assertEqual((style.kind, style.props["title"]), ("Adw.ComboRow", "Style"))
        self.assertEqual((theme.kind, theme.props["title"]), ("ThemeRow", "Theme"))
        self.assertEqual((custom.kind, custom.props["title"]), ("Adw.ActionRow", "Custom Colour"))
        self.assertIs(custom, host.custom_row)
        self.assertIs(zoom, host.zoom)

    def test_natters_own_then_whatsapps(self):
        _host, page = self.open()
        _top, groups = self.picker(page)
        self.assertEqual(len(groups), len(themes.GROUPS))
        self.assertEqual([title for title, _ids in themes.GROUPS], [NAME, "WhatsApp"])
        self.assertEqual(themes.GROUPS[0][1], ("den", "custom", "plain"))
        self.assertEqual(len(themes.GROUPS[1][1]), 22)
        self.assertTrue(all(theme_id.startswith("wa-") for theme_id in themes.GROUPS[1][1]))
        for (title, ids), group in zip(themes.GROUPS, groups):
            with self.subTest(group=title):
                heading, grid = group.added()
                self.assertEqual((heading.kind, heading.props["label"]), ("Gtk.Label", title))
                self.assertEqual(grid.kind, "SwatchGrid")  # wrapping lines, not one long row
                self.assertEqual([button.props["css_classes"][1] for button in grid.added()],
                                 [f"theme-{theme_id}" for theme_id in ids])

    def test_one_radio_group_across_both(self):
        host, _page = self.open()
        self.assertEqual(IDS, [theme_id for _title, ids in themes.GROUPS for theme_id in ids])
        buttons = self.swatches(host)
        self.assertEqual(len(buttons), 25)
        self.assertEqual({b.kind for b in buttons}, {"Gtk.CheckButton"})
        self.assertEqual([b.group for b in buttons], [buttons] * 25)  # one group, in the groups' order
        # Choosing across groups turns the other group's choice off: one theme at a time.
        host.theme_buttons["wa-lime"].set_active(True)
        host.theme_buttons["plain"].set_active(True)
        self.assertEqual(self.chosen(host), ["plain"])

    def test_swatches_are_named_radios(self):
        host, _page = self.open()
        for (theme_id, name, describe), button in zip(themes.CHOICES, self.swatches(host)):
            with self.subTest(theme=theme_id):
                self.assertEqual(button.props["accessible_role"], "Gtk.AccessibleRole.RADIO")
                self.assertEqual(button.props["tooltip_text"], name)
                self.assertEqual(button.called("update_property"),
                                 [(["Gtk.AccessibleProperty.LABEL", "Gtk.AccessibleProperty.DESCRIPTION"],
                                   [name, describe])])
                self.assertTrue(name and describe)
                tick, = button.added()
                self.assertEqual((tick.kind, tick.props["icon_name"]), ("Gtk.Image", "object-select-symbolic"))

    def test_swatches_wear_the_classes_swatch_css_colours(self):
        host, _page = self.open()
        for dark in (False, True):
            for high_contrast in (False, True):
                css = themes.swatch_css(dark, high_contrast, "#3a7bd5")
                for button in self.swatches(host):
                    classes = button.props["css_classes"]
                    self.assertEqual(classes[0], "natter-swatch")
                    self.assertIn(f"checkbutton.{'.'.join(classes)} ", css)

    def test_the_row_and_its_groups_are_named(self):
        host, page = self.open()
        row = self.appearance(page)[1]
        self.assertEqual(row.props["accessible_role"], "Gtk.AccessibleRole.GROUP")
        self.assertIs(row.props["activatable"], False)
        # Unfocusable, so Tab goes straight to the chosen swatch.
        self.assertIs(row.props["focusable"], False)
        self.assertEqual(row.called("update_property"), [(["Gtk.AccessibleProperty.LABEL"], ["Theme"])])
        self.assertEqual(row.swatches, self.swatches(host))  # what it hands the focus and arrow keys on to
        top, groups = self.picker(page)
        self.assertEqual(top.added()[0].props["label"], "Theme")
        self.assertIs(top.added()[1], host.theme_name)
        for (title, _ids), group in zip(themes.GROUPS, groups):
            self.assertEqual(group.props["accessible_role"], "Gtk.AccessibleRole.GROUP")
            self.assertEqual(group.called("update_property"), [(["Gtk.AccessibleProperty.LABEL"], [title])])

    def test_shows_the_theme_in_use(self):
        for theme_id in IDS:
            host, _page = self.open(theme_id)
            with self.subTest(theme=theme_id):
                self.assertEqual(self.chosen(host), [theme_id])
                self.assertEqual(host.theme_name.called("set_label")[-1], (NAMES[theme_id],))
                self.assertEqual(host.custom_row.visible(), theme_id == "custom")
                self.assertEqual(host.settings.written, [])  # showing it isn't choosing it

    def test_older_themes_show_as_their_whatsapp_theme(self):
        for old, new in (("lagoon", "wa-merino-teal"), ("meadow", "wa-lime"), ("dusk", "wa-tyrian-purple"),
                         ("rose", "wa-pink"), ("graphite", "wa-dune-mono")):
            host, _page = self.open(old)
            with self.subTest(theme=old):
                self.assertEqual(self.chosen(host), [new])
                self.assertEqual(host.theme_name.called("set_label")[-1], (NAMES[new],))
                self.assertEqual(host.settings.written, [])  # what Natter uses; settings.json is left as it is

    def test_anything_else_shows_den(self):
        # What themes.chosen() makes of it, and so what Natter actually uses.
        for value in ("solarized", "Den", "wa-", "wa-nope", "", None, 3, ["lagoon"]):
            host, _page = self.open(value)
            with self.subTest(value=value):
                self.assertEqual(self.chosen(host), ["den"])
                self.assertEqual(host.theme_name.called("set_label")[-1], ("Den",))
                self.assertEqual(host.settings.written, [])

    def test_choosing_writes_the_setting(self):
        host, _page = self.open()
        host.theme_buttons["wa-pink"].set_active(True)  # a click, or an arrow key moving onto it
        self.assertEqual(host.settings.written, [("theme", "wa-pink")])
        self.assertEqual(self.chosen(host), ["wa-pink"])
        self.assertEqual(host.theme_name.called("set_label")[-1], (NAMES["wa-pink"],))
        host.theme_buttons["plain"].set_active(True)  # Plain is always there to go back to
        self.assertEqual(host.settings.written, [("theme", "wa-pink"), ("theme", "plain")])
        self.assertEqual(host.theme_name.called("set_label")[-1], ("Plain",))

    def test_follows_a_change_made_elsewhere(self):
        host, _page = self.open()
        host.settings.change_elsewhere("theme", "wa-royal-blue")
        host._on_setting("theme", "wa-royal-blue")
        self.assertEqual(self.chosen(host), ["wa-royal-blue"])
        self.assertEqual(host.theme_name.called("set_label")[-1], (NAMES["wa-royal-blue"],))
        host.settings.change_elsewhere("theme", "custom")
        host._on_setting("theme", "custom")
        self.assertTrue(host.custom_row.visible())
        host.settings.change_elsewhere("theme", "neon")
        host._on_setting("theme", "neon")
        self.assertEqual(self.chosen(host), ["den"])
        self.assertFalse(host.custom_row.visible())
        self.assertEqual(host.settings.written, [])  # nothing written back

    def test_style_hint(self):
        host, page = self.open()
        style = self.appearance(page)[0]
        self.assertIs(style.props["use_markup"], False)  # the hint is shown as it is
        hint = "WhatsApp has a light or dark look of its own. To match, set Theme to “System default” " \
               "in WhatsApp’s settings."
        host.show_style_hint(hint)
        host.show_style_hint("")
        self.assertEqual(style.called("set_subtitle"), [(hint,), ("",)])


class CustomColourTest(unittest.TestCase):
    """Custom's row: shown while Custom is the theme, with a colour button for the colour it's
    made from (settings "theme_custom", "#rrggbb")."""

    def setUp(self):
        patches = mock.patch.multiple(prefs, **STAND_INS)
        patches.start()
        self.addCleanup(patches.stop)

    def open(self, theme="custom", custom="#3a7bd5"):
        host = Host(Settings(theme=theme, theme_custom=custom))
        host._general()
        return host, host.custom_row, host.custom_button

    def test_shown_only_while_custom_is_the_theme(self):
        host, row, _button = self.open(theme="den")
        self.assertFalse(row.visible())
        host.theme_buttons["custom"].set_active(True)
        self.assertTrue(row.visible())
        self.assertEqual(host.settings.written, [("theme", "custom")])
        host.theme_buttons["wa-lime"].set_active(True)
        self.assertFalse(row.visible())

    def test_a_row_for_the_colour_button(self):
        _host, row, button = self.open()
        self.assertEqual(button.kind, "Gtk.ColorDialogButton")  # GTK 4.10
        self.assertEqual(row.called("add_suffix"), [(button,)])
        self.assertEqual(row.called("set_activatable_widget"), [(button,)])  # the whole row opens it
        dialog = button.props["dialog"]
        self.assertEqual((dialog.kind, dialog.props["title"], dialog.props["with_alpha"]),
                         ("Gtk.ColorDialog", "Custom Colour", False))  # a colour, not a see-through one
        self.assertTrue(row.props["subtitle"])

    def test_named_for_screen_readers(self):
        _host, _row, button = self.open()
        self.assertEqual(button.called("update_property"), [(["Gtk.AccessibleProperty.LABEL"], ["Custom Colour"])])
        self.assertEqual(button.props["tooltip_text"], "Choose Colour")

    def test_shows_the_colour_in_use(self):
        host, _row, button = self.open(custom="#3A7BD5")
        self.assertEqual(prefs.hex_colour(button.get_rgba()), "#3a7bd5")
        self.assertEqual(host.settings.written, [])  # showing it isn't choosing it

    def test_a_colour_that_isnt_one_shows_dens_orange(self):
        # What themes.custom_colour() makes of it, and so what Natter actually uses.
        for value in ("teal", "#12345", "#ggg000", "rgb(1, 2, 3)", "", None, 7, ["#3a7bd5"]):
            host, _row, button = self.open(custom=value)
            with self.subTest(value=value):
                self.assertEqual(prefs.hex_colour(button.get_rgba()), themes.CUSTOM_DEFAULT)
                self.assertEqual(host.settings.written, [])

    def test_picking_a_colour_writes_it(self):
        host, _row, button = self.open()
        button.pick("#bf4040")
        self.assertEqual(host.settings.written, [("theme_custom", "#bf4040")])
        button.pick("rgb(191, 64, 64)")  # the same colour again: nothing new to write
        button.pick("#BF4040")
        self.assertEqual(host.settings.written, [("theme_custom", "#bf4040")])
        button.pick("#00ff7f")
        self.assertEqual(host.settings.written, [("theme_custom", "#bf4040"), ("theme_custom", "#00ff7f")])

    def test_follows_a_change_made_elsewhere(self):
        host, _row, button = self.open()
        host.settings.change_elsewhere("theme_custom", "#123456")
        host._on_setting("theme_custom", "#123456")
        self.assertEqual(prefs.hex_colour(button.get_rgba()), "#123456")
        shown = len(button.called("set_rgba"))
        host._on_setting("theme_custom", "#123456")  # already shown: left alone
        self.assertEqual(len(button.called("set_rgba")), shown)
        self.assertEqual(host.settings.written, [])  # nothing written back

    def test_hex_colour(self):
        for colour in ("#000000", "#ffffff", "#ff7800", "#0a0a0a", "#3a7bd5"):
            rgba = Gdk.RGBA()
            self.assertTrue(rgba.parse(colour))
            self.assertEqual(prefs.hex_colour(rgba), colour)
        rgba = Gdk.RGBA()
        rgba.parse("rgba(255, 120, 0, 0.5)")
        self.assertEqual(prefs.hex_colour(rgba), "#ff7800")  # the colour; the dialog offers no alpha


# ---- Laying the swatches out, and the keys among them -----------------------------------------


# The picker's inside width at the dialog's narrowest, 360 px, and at its widest (measured in a lab).
NARROW, WIDE = 308, 504
SIZE = 36  # each swatch, as themes.SWATCH_CSS makes it


def layout(width):
    """Where each swatch's centre is in the picker `width` wide, in IDS order: each group's
    heading, then its swatches in lines, as the dialog lays them out."""
    centres, top = [], 0.0
    for _title, ids in themes.GROUPS:
        top += 24  # the heading
        columns = prefs.wrap_columns(len(ids), width, SIZE)
        for index in range(len(ids)):
            line, column = divmod(index, columns)
            centres.append((column * (SIZE + prefs.SWATCH_GAP) + SIZE / 2,
                            top + line * (SIZE + prefs.SWATCH_GAP) + SIZE / 2))
        top += -(-len(ids) // columns) * (SIZE + prefs.SWATCH_GAP)
    return centres


class SwatchGridTest(unittest.TestCase):
    """The swatches' lines: as many as fit across, evened out, in a dialog as narrow as 360 px."""

    def grid(self, count, rtl=False):
        placed = []

        def swatch(index):
            return SimpleNamespace(measure=lambda _orientation, _for: (SIZE, SIZE, -1, -1),
                                   size_allocate=lambda box, _base: placed.append((index, box.x, box.y,
                                                                                   box.width, box.height)))
        swatches = [swatch(index) for index in range(count)]
        direction = prefs.Gtk.TextDirection.RTL if rtl else prefs.Gtk.TextDirection.LTR
        grid = SimpleNamespace(_swatches=lambda: swatches, _cell=prefs.SwatchGrid._cell,
                               get_direction=lambda: direction)
        return grid, placed

    def test_lines_come_out_even(self):
        self.assertEqual(prefs.wrap_columns(22, NARROW, SIZE), 6)  # 6, 6, 6 and 4, not 7, 7, 7 and 1
        self.assertEqual(prefs.wrap_columns(22, WIDE, SIZE), 11)  # two lines of 11
        self.assertEqual(prefs.wrap_columns(3, NARROW, SIZE), 3)
        self.assertEqual(prefs.wrap_columns(22, 0, SIZE), 1)  # never none a line
        self.assertEqual(prefs.wrap_columns(0, NARROW, SIZE), 1)

    def test_every_line_fits(self):
        for width in range(SIZE, 700, 4):
            for count in (3, 22):
                columns = prefs.wrap_columns(count, width, SIZE)
                with self.subTest(width=width, count=count):
                    self.assertTrue(columns == 1 or columns * SIZE + (columns - 1) * prefs.SWATCH_GAP <= width)
                    # Even: no line more than one shorter than a full one.
                    last = count - (-(-count // columns) - 1) * columns
                    self.assertGreaterEqual(last, columns - -(-count // columns))

    def test_places_each_swatch(self):
        grid, placed = self.grid(22)
        prefs.SwatchGrid.do_size_allocate(grid, NARROW, 168, -1)
        self.assertEqual(placed[0], (0, 0, 0, SIZE, SIZE))
        self.assertEqual(placed[5], (5, 5 * 44, 0, SIZE, SIZE))
        self.assertEqual(placed[6], (6, 0, 44, SIZE, SIZE))  # the second line
        self.assertEqual(placed[21], (21, 3 * 44, 3 * 44, SIZE, SIZE))
        self.assertTrue(all(x + w <= NARROW for _i, x, _y, w, _h in placed))

    def test_right_to_left(self):
        grid, placed = self.grid(3, rtl=True)
        prefs.SwatchGrid.do_size_allocate(grid, NARROW, SIZE, -1)
        self.assertEqual([x for _i, x, _y, _w, _h in placed], [NARROW - SIZE, NARROW - SIZE - 44, NARROW - SIZE - 88])

    def test_asks_for_the_height_its_lines_take(self):
        grid, _placed = self.grid(22)
        vertical, horizontal = prefs.Gtk.Orientation.VERTICAL, prefs.Gtk.Orientation.HORIZONTAL
        self.assertEqual(prefs.SwatchGrid.do_measure(grid, vertical, NARROW), (4 * 36 + 3 * 8,) * 2 + (-1, -1))
        self.assertEqual(prefs.SwatchGrid.do_measure(grid, vertical, WIDE), (2 * 36 + 8,) * 2 + (-1, -1))
        # As narrow as one swatch, at most 11 across when there's room.
        self.assertEqual(prefs.SwatchGrid.do_measure(grid, horizontal, -1), (SIZE, 11 * 36 + 10 * 8, -1, -1))
        self.assertEqual(prefs.SwatchGrid.do_measure(self.grid(0)[0], vertical, NARROW), (0, 0, -1, -1))

    def test_a_layout_only(self):
        # No keys, focus or selection of its own (Gtk.FlowBox has all three): the swatches' radio
        # group and ThemeRow's keys are all there is.
        self.assertTrue(issubclass(prefs.SwatchGrid, prefs.Gtk.Widget))
        self.assertFalse(issubclass(prefs.SwatchGrid, prefs.Gtk.FlowBox))
        names = {node.attr if isinstance(node, ast.Attribute) else node.id
                 for node in ast.walk(ast.parse(inspect.getsource(prefs.SwatchGrid).lstrip()))
                 if isinstance(node, (ast.Attribute, ast.Name))}
        self.assertEqual({name for name in names if any(word in name.lower() for word in
                                                         ("controller", "focus", "select", "key", "flowbox"))}, set())
        self.assertIn("unparent", inspect.getsource(prefs.SwatchGrid.do_dispose))  # GTK leaves it to the parent


class ThemeRowFocusTest(unittest.TestCase):
    """The row hands the focus on to the chosen swatch, as libadwaita's spin and entry rows do to
    their fields: that's where the list's Up and Down keys land."""

    def row(self, chosen, focus_inside=False):
        grabbed = []
        swatches = []
        for theme_id in IDS:
            swatch = Swatch()
            swatch.active = theme_id == chosen
            swatch.grab_focus = lambda theme_id=theme_id: grabbed.append(theme_id) or True
            swatches.append(swatch)
        row = SimpleNamespace(swatches=swatches, get_focus_child=lambda: swatches[0] if focus_inside else None)
        return row, grabbed

    def test_a_preferences_row(self):
        self.assertTrue(issubclass(ThemeRow, prefs.Adw.PreferencesRow))

    def test_focus_goes_to_the_chosen_swatch(self):
        row, grabbed = self.row("wa-lime")
        self.assertTrue(ThemeRow.do_grab_focus(row))
        self.assertEqual(grabbed, ["wa-lime"])

    def test_focus_on_its_way_out_isnt_taken_back(self):
        # Shift+Tab from a swatch: GTK offers the row the focus, and it lets it go on to Style.
        row, grabbed = self.row("wa-lime", focus_inside=True)
        self.assertFalse(ThemeRow.do_grab_focus(row))
        self.assertEqual(grabbed, [])


class Neighbour(SimpleNamespace):
    """A row next to the Theme row in the list."""

    def grab_focus(self):
        self.focused = self.takes_focus
        return self.takes_focus


class ThemeRowKeysTest(unittest.TestCase):
    """The arrow keys while a swatch has the focus: Left and Right along the themes in order, Up
    and Down from line to line, choosing as they go; out of the row from the top or bottom line."""

    NONE = Gdk.ModifierType(0)

    def row(self, focus, width=NARROW, rtl=False, above=None, below=None):
        swatches = []
        for theme_id in IDS:
            swatch = Swatch()
            swatch.theme_id = theme_id
            swatch.active = theme_id == focus
            if swatches:
                swatch.set_group(swatches[0])
            swatches.append(swatch)
        self.focus = focus

        def grab(swatch):
            self.focus = swatch.theme_id
            return True
        for swatch in swatches:
            swatch.has_focus = lambda swatch=swatch: self.focus == swatch.theme_id
            swatch.grab_focus = lambda swatch=swatch: grab(swatch)
        row = SimpleNamespace(swatches=swatches, _centres=lambda: layout(width),
                              get_direction=lambda: prefs.Gtk.TextDirection.RTL if rtl else prefs.Gtk.TextDirection.LTR,
                              get_prev_sibling=lambda: above, get_next_sibling=lambda: below)
        row._leave = lambda down: ThemeRow._leave(row, down)
        return row

    def press(self, row, *keys, state=None):
        handled = [ThemeRow._key_pressed(row, None, key, 0, self.NONE if state is None else state) for key in keys]
        chosen = [swatch.theme_id for swatch in row.swatches if swatch.active]
        return handled, self.focus, chosen

    def test_left_and_right_go_along_the_themes_in_order(self):
        row = self.row("den")
        self.assertEqual(self.press(row, Gdk.KEY_Right), ([True], "custom", ["custom"]))
        self.assertEqual(self.press(row, Gdk.KEY_Right, Gdk.KEY_KP_Right), ([True, True], IDS[3], [IDS[3]]))
        # On from Natter Den's last to WhatsApp's first, and from the end of a line to the next one's start.
        self.assertEqual(IDS[3], "wa-whatsapp-green")
        row = self.row(IDS[8])  # the end of WhatsApp's first line, 6 across
        self.assertEqual(self.press(row, Gdk.KEY_Right)[1:], (IDS[9], [IDS[9]]))
        self.assertEqual(self.press(row, Gdk.KEY_Left, Gdk.KEY_Left)[1:], (IDS[7], [IDS[7]]))

    def test_left_and_right_stop_at_either_end(self):
        row = self.row("den")
        self.assertEqual(self.press(row, Gdk.KEY_Left), ([True], "den", ["den"]))  # handled: nothing else moves
        row = self.row(IDS[-1])
        self.assertEqual(self.press(row, Gdk.KEY_Right), ([True], IDS[-1], [IDS[-1]]))

    def test_right_to_left(self):
        row = self.row("custom", rtl=True)
        self.assertEqual(self.press(row, Gdk.KEY_Left)[1:], ("plain", ["plain"]))  # the next one is on the left
        self.assertEqual(self.press(row, Gdk.KEY_Right, Gdk.KEY_Right)[1:], ("den", ["den"]))

    def test_up_and_down_go_from_line_to_line(self):
        # Narrow: Den, Custom and Plain, then WhatsApp's 22 in lines of 6.
        row = self.row("custom")
        whatsapp = IDS[3:]
        self.assertEqual(self.press(row, Gdk.KEY_Down)[1:], (whatsapp[1], [whatsapp[1]]))  # straight below
        self.assertEqual(self.press(row, Gdk.KEY_Down, Gdk.KEY_KP_Down)[1:], (whatsapp[13], [whatsapp[13]]))
        self.assertEqual(self.press(row, Gdk.KEY_Up)[1:], (whatsapp[7], [whatsapp[7]]))
        row = self.row(whatsapp[5])  # far right: up to the nearest of Natter Den's three
        self.assertEqual(self.press(row, Gdk.KEY_Up)[1:], ("plain", ["plain"]))
        row = self.row(whatsapp[17])  # the last line is shorter: down from above it goes to its nearest
        self.assertEqual(self.press(row, Gdk.KEY_Down)[1:], (whatsapp[21], [whatsapp[21]]))
        row = self.row(whatsapp[5], width=WIDE)  # wide: lines of 11
        self.assertEqual(self.press(row, Gdk.KEY_Down)[1:], (whatsapp[16], [whatsapp[16]]))

    def test_up_and_down_leave_from_the_top_and_bottom_lines(self):
        style, zoom = Neighbour(takes_focus=True), Neighbour(takes_focus=True)
        hidden = Neighbour(takes_focus=True)  # Custom Colour, while it isn't Custom
        for row_ in (style, zoom, hidden):
            row_.get_mapped = lambda r=row_: r is not hidden
            row_.is_sensitive = lambda: True
        hidden.get_next_sibling = lambda: zoom
        row = self.row("plain", above=style, below=hidden)
        self.assertEqual(self.press(row, Gdk.KEY_Up), ([True], "plain", ["plain"]))  # the choice stays
        self.assertTrue(style.focused)
        row = self.row(IDS[-1], below=hidden)
        self.assertEqual(self.press(row, Gdk.KEY_Down), ([True], IDS[-1], [IDS[-1]]))
        self.assertTrue(zoom.focused)  # past the hidden row
        self.assertFalse(hasattr(hidden, "focused"))
        # The list's first row: what's above is the list's to say.
        row = self.row("den", above=None)
        self.assertEqual(self.press(row, Gdk.KEY_Up)[0], [False])

    def test_a_row_that_wont_take_the_focus_is_passed(self):
        dimmed = Neighbour(takes_focus=True, get_mapped=lambda: True, is_sensitive=lambda: False)
        stubborn = Neighbour(takes_focus=False, get_mapped=lambda: True, is_sensitive=lambda: True)
        style = Neighbour(takes_focus=True, get_mapped=lambda: True, is_sensitive=lambda: True)
        dimmed.get_prev_sibling, stubborn.get_prev_sibling = (lambda: stubborn), (lambda: style)
        row = self.row("custom", above=dimmed)
        self.assertEqual(self.press(row, Gdk.KEY_Up)[0], [True])
        self.assertTrue(style.focused)
        self.assertFalse(hasattr(dimmed, "focused"))

    def test_other_keys_and_modifiers_go_on(self):
        row = self.row("custom")
        for key in (Gdk.KEY_Tab, Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_Home, Gdk.KEY_Page_Down, Gdk.KEY_a):
            self.assertEqual(self.press(row, key), ([False], "custom", ["custom"]))
        for state in (Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.ALT_MASK, Gdk.ModifierType.SHIFT_MASK):
            self.assertEqual(self.press(row, Gdk.KEY_Right, state=state), ([False], "custom", ["custom"]))

    def test_only_while_a_swatch_has_the_focus(self):
        row = self.row("custom")
        self.focus = None  # the focus is in another row
        self.assertEqual(self.press(row, Gdk.KEY_Right, Gdk.KEY_Down), ([False, False], None, ["custom"]))

    def test_the_keys_come_before_the_lists(self):
        # Captured on the way down, so the list's Up and Down (and anything else's) never see them.
        source = inspect.getsource(ThemeRow.__init__)
        self.assertIn("EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)", source)
        self.assertIn('"key-pressed", self._key_pressed', source)


class ThemeBanSafetyTest(unittest.TestCase):
    """The picker's part in the theme's ban-safety rules: it only ever writes Natter's own
    settings.json, and it never reaches into WhatsApp's page or how it's loaded."""

    def test_only_the_theme_settings_are_written(self):
        with mock.patch.multiple(prefs, **STAND_INS):
            host = Host(Settings())
            host._general()
            for theme_id in IDS[1:] + IDS[:1]:
                host.theme_buttons[theme_id].set_active(True)
            host.custom_button.pick("#bf4040")
        self.assertEqual({key for key, _ in host.settings.written}, {"theme", "theme_custom"})
        self.assertEqual([value for key, value in host.settings.written if key == "theme"], IDS[1:] + IDS[:1])
        self.assertEqual([value for key, value in host.settings.written if key == "theme_custom"], ["#bf4040"])

    def test_no_page_storage_scripts_or_web_settings(self):
        source = PREFS.read_text()
        for banned in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB", "WebKit", "javascript",
                       "UserStyleSheet", "UserScript", "user_agent", "system-theme-mode", "webpackChunk",
                       "chatThemeId", "appTheme", "wa_web_chat_themes", "aura", "dispatchEvent"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
