"""The window's polish (window.py): the stock header with Natter's buttons on the left, the page's
colour before WhatsApp paints (the theme's), the drop overlay, the offline notice, the lock page, the accounts'
tabs at any width, the banner, the main menu and the shortcuts it lists. None of it needs a display:
widgets are stood in for by fakes, as in test_features.py."""

import ast
import contextlib
import inspect
import io
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from natter import NAME, themes, window
from natter.window import AccountPage, Window


class Fake:
    """Records the methods called on it, by name, and answers each with another Fake."""

    def __init__(self, **values):
        self.calls = []
        self.__dict__.update(values)

    def __getattr__(self, name):
        def method(*args):
            self.calls.append((name, *args))
            return Fake()
        return method

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Entry(Fake):
    """A password entry: its text and its style classes."""

    def __init__(self, text=""):
        super().__init__(text=text, classes=set())

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text  # like GTK, no "changed" here: the tests call what it would

    def add_css_class(self, name):
        self.classes.add(name)

    def remove_css_class(self, name):
        self.classes.discard(name)


class Button(Fake):
    def __init__(self):
        super().__init__(sensitive=True)

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive


def parsed(accel):
    ok, key, mods = Gtk.accelerator_parse(accel)
    assert ok, accel
    return key, int(mods)


def keys_of(written):
    """Every key combination in Gtk.ShortcutLabel's notation: alternatives and first...last ranges."""
    found = []
    for part in written.split():
        if "..." in part:
            first, last = part.split("...")
            (low, mods), (high, _) = parsed(first), parsed(last)
            found += [(key, mods) for key in range(low, high + 1)]
        else:
            found.append(parsed(part))
    return found


class HeaderTest(unittest.TestCase):
    def header_calls(self):
        """(method, what) for each widget Window.__init__ packs into the header, in order."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(Window.__init__)))
        packed = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("pack_start", "pack_end") and ast.unparse(node.func.value) == "self.header":
                arg = node.args[0]
                what = arg.func.attr if isinstance(arg, ast.Call) else ast.unparse(arg)
                packed.append((node.lineno, node.func.attr, what))
        return [(method, what) for _line, method, what in sorted(packed)]

    def test_buttons_on_the_left_in_order(self):
        # From the left edge: the main menu, Reload, Privacy Blur. The right keeps the window
        # controls and the call button, which belongs to the chat open on that side.
        self.assertEqual(self.header_calls(), [("pack_start", "MenuButton"), ("pack_start", "self.reload_button"),
                                               ("pack_start", "ToggleButton"), ("pack_end", "self.call")])

    def test_stock_header(self):
        source = Path(window.__file__).read_text()
        self.assertNotIn("natter-header", source)
        self.assertNotIn("headerbar", window.CSS)
        self.assertIn("RAISED_BORDER", inspect.getsource(Window.__init__))

    def test_css_parses(self):
        errors = []
        provider = Gtk.CssProvider()
        provider.connect("parsing-error", lambda _p, _section, error: errors.append(error.message))
        provider.load_from_string(window.CSS)
        self.assertEqual(errors, [])

    def test_drop_overlay_is_a_neutral_scrim(self):
        rule = window.CSS[window.CSS.index(".natter-drop"):]
        rule = rule[:rule.index("}")]
        self.assertIn("alpha(@window_bg_color", rule)
        self.assertNotIn("accent_bg_color", rule)
        self.assertNotIn("color: @accent_color", rule)


class MenuTest(unittest.TestCase):
    def items(self, calls):
        menu = window.main_menu(Fake(calls=calls))
        sections = []
        for i in range(menu.get_n_items()):
            section = menu.get_item_link(i, "section")
            sections.append([(section.get_item_attribute_value(j, "label", GLib.VariantType("s")).get_string(),
                              section.get_item_attribute_value(j, "action", GLib.VariantType("s")).get_string())
                             for j in range(section.get_n_items())])
        return sections

    def test_do_not_disturb_is_in_the_first_section(self):
        first = self.items(None)[0]
        self.assertIn(("_Do Not Disturb…", "app.do-not-disturb"), first)
        self.assertEqual(first[0][1], "win.go-to-chat")

    def test_each_item_has_its_own_letter(self):
        for calls in (None, object()):
            labels = [label for section in self.items(calls) for label, _action in section]
            letters = [label[label.index("_") + 1].lower() for label in labels]
            self.assertEqual(len(letters), len(set(letters)), labels)

    def test_microphone_and_camera_have_a_section(self):
        # Check items for the switches in Preferences → Privacy (test_media_ui.py has more).
        for calls in (None, object()):
            self.assertEqual(self.items(calls)[1], [("_Microphone", "app.microphone"), ("Cam_era", "app.camera")])

    def test_set_up_calls_only_with_the_call_window(self):
        actions = lambda calls: [a for section in self.items(calls) for _l, a in section]  # noqa: E731
        self.assertNotIn("app.call-window", actions(None))
        self.assertIn("app.call-window", actions(object()))

    def test_names_the_app(self):
        labels = [label for section in self.items(None) for label, _action in section]
        self.assertIn(f"_About {NAME}", labels)


class ShortcutsTest(unittest.TestCase):
    def test_lock_only_with_a_passcode(self):
        listed = lambda lock: [what for _t, rows in window.shortcut_sections(lock) for what, _k in rows]  # noqa: E731
        self.assertIn("Lock", listed(True))
        self.assertNotIn("Lock", listed(False))

    def test_first_section_is_the_app(self):
        self.assertEqual(window.shortcut_sections()[0][0], NAME)

    def test_lists_the_real_keys(self):
        rows = {what: keys for _title, section in window.shortcut_sections() for what, keys in section}
        self.assertEqual(len(keys_of(rows["Switch account"])), 9)
        self.assertEqual(keys_of(rows["Reload WhatsApp"]), [parsed("<Control>r"), parsed("F5")])
        self.assertEqual(keys_of(rows["Zoom in"]), [parsed("<Control>plus"), parsed("<Control>equal")])
        self.assertIn("Keyboard shortcuts", rows)

    def test_every_listed_key_is_bound(self):
        bound = {parsed(accel) for accel, _action, _target in window.window_keys()}
        bound.add(parsed("<Control>l"))  # Lock is the app's (app.lock), not the window's
        for title, rows in window.shortcut_sections()[:2]:  # the third is WhatsApp's own
            for what, keys in rows:
                for key in keys_of(keys):
                    self.assertIn(key, bound, f"{title}: {what}")

    def test_every_window_key_is_listed(self):
        listed = {key for _title, rows in window.shortcut_sections() for _what, keys in rows for key in keys_of(keys)}
        for accel, _action, _target in window.window_keys():
            self.assertIn(parsed(accel), listed, accel)


class PureHelpersTest(unittest.TestCase):
    def test_drop_description(self):
        self.assertEqual(window.drop_description("Sam", blurred=False), "To Sam")
        self.assertEqual(window.drop_description("Sam", blurred=True), "")  # no name under privacy blur
        self.assertEqual(window.drop_description("", blurred=False), "You'll choose the chat next")
        self.assertEqual(window.drop_description("", blurred=True), "You'll choose the chat next")

    def test_tab_look(self):
        self.assertEqual(window.tab_look(False, False, 3), ("avatar-default-symbolic", 3))
        self.assertEqual(window.tab_look(True, False, 0), ("dialog-warning-symbolic", 0))
        self.assertEqual(window.tab_look(True, False, 2), ("dialog-warning-symbolic", 0))
        self.assertEqual(window.tab_look(False, True, 3), ("view-refresh-symbolic", 3))  # as before
        # Stopped (Try Again): a warning, and no count, as WhatsApp isn't there to keep it.
        self.assertEqual(window.tab_look(False, False, 4, stopped=True), ("dialog-warning-symbolic", 0))

    def test_a_stopped_account_wants_attention(self):
        accounts = [SimpleNamespace(id="a", needs_link=False, reloading=False, unread=0, crashing=False),
                    SimpleNamespace(id="b", needs_link=False, reloading=False, unread=3, crashing=True)]
        pages = {account.id: Fake() for account in accounts}
        fake = Fake(app=SimpleNamespace(accounts=accounts), pages=pages)
        fake._show_subtitle = fake._show_reloading = lambda: None
        Window.show_status(fake, 3)
        self.assertEqual(pages["a"].called("set_needs_attention"), [(False,)])
        self.assertEqual(pages["b"].called("set_needs_attention"), [(True,)])
        self.assertEqual(pages["b"].called("set_icon_name"), [("dialog-warning-symbolic",)])
        self.assertEqual(pages["b"].called("set_badge_number"), [(0,)])

    def test_switcher_places(self):
        self.assertEqual(window.switcher_places(1, False), (False, False))
        self.assertEqual(window.switcher_places(1, True), (False, False))
        self.assertEqual(window.switcher_places(2, False), (True, False))
        self.assertEqual(window.switcher_places(2, True), (False, True))

    def test_plain_keeps_adwaitas_backgrounds(self):
        # With no theme, the page and the snapshot keep libadwaita's own window colour, as before.
        for colour in (themes.page_background, themes.window_background):
            self.assertEqual(window.rgba(colour("plain", False)).to_string(), "rgb(250,250,251)")
            self.assertEqual(window.rgba(colour("plain", True)).to_string(), "rgb(34,34,38)")


def style(dark):
    """libadwaita as window.py asks it, in the dark style or not."""
    return SimpleNamespace(StyleManager=SimpleNamespace(get_default=lambda: SimpleNamespace(get_dark=lambda: dark)))


class ThemeBackgroundTest(unittest.TestCase):
    """Before WhatsApp paints, the page shows its start-up screen's colour in the theme, and the
    snapshot puts the theme's window colour behind the header bar."""

    def test_page_takes_the_themes_colour(self):
        for theme, dark in (("den", False), ("den", True), ("lagoon", True), ("plain", False), ("neon", True)):
            page = Fake(account=Fake(view=Fake()), settings={"theme": theme})
            with mock.patch.object(window, "Adw", style(dark)):
                AccountPage.paint(page)
            (colour,), = page.account.view.called("set_background_color")
            with self.subTest(theme=theme, dark=dark):
                self.assertEqual(colour.to_string(), window.rgba(themes.page_background(theme, dark)).to_string())
        self.assertNotEqual(window.rgba(themes.page_background("den", True)).to_string(), "rgb(34,34,38)")

    def test_repaint_pages(self):
        painted = []
        pages = {name: Fake(get_child=lambda name=name: Fake(paint=lambda name=name: painted.append(name)))
                 for name in ("main", "account-2")}
        Window.repaint_pages(Fake(pages=pages))
        self.assertEqual(painted, ["main", "account-2"])

    def snapshot_background(self, theme, custom="#ff7800", dark=False):
        """The colour save_snapshot puts behind the header bar, and the box it fills."""
        snapshot = Fake(to_node=lambda: "node")
        saved = []
        texture = Fake(save_to_png=saved.append)
        content = Fake(get_width=lambda: 800, get_height=lambda: 600)
        gtk = SimpleNamespace(Window=SimpleNamespace(get_child=lambda _w: content),
                              WidgetPaintable=SimpleNamespace(new=lambda _c: Fake()), Snapshot=lambda: snapshot)
        fake = Fake(app=Fake(settings={"theme": theme, "theme_custom": custom}),
                    get_renderer=lambda: Fake(render_texture=lambda _node, _rect: texture))
        with mock.patch.multiple(window, Adw=style(dark), Gtk=gtk,
                                 Graphene=SimpleNamespace(Rect=lambda: Fake(init=lambda *box: box)),
                                 GLib=SimpleNamespace(timeout_add=lambda _ms, run: run(), SOURCE_REMOVE=False)), \
                contextlib.redirect_stdout(io.StringIO()):
            Window.save_snapshot(fake, "/tmp/never-written.png")
        self.assertEqual(saved, ["/tmp/never-written.png"])
        (colour, box), = snapshot.called("append_color")
        return colour.to_string(), box

    def test_snapshot_background(self):
        colour, box = self.snapshot_background("lagoon")
        self.assertEqual(colour, window.rgba(themes.window_background("lagoon", False)).to_string())
        self.assertEqual(box, (0, 0, 800, 600))

    def test_snapshot_background_takes_customs_colour(self):
        # Custom's window greys turn towards the colour picked, so the snapshot needs it too.
        for custom in ("#2ec27e", "#e01b24"):
            colour, _box = self.snapshot_background("custom", custom, dark=True)
            self.assertEqual(colour, window.rgba(themes.window_background("custom", True, custom)).to_string())
        self.assertNotEqual(self.snapshot_background("custom", "#2ec27e", True)[0],
                            self.snapshot_background("custom", "#e01b24", True)[0])


class SwitcherTest(unittest.TestCase):
    def fake(self, accounts):
        fake = Fake(pages=dict.fromkeys(range(accounts)), narrow=False, header=Fake(), switcher=Fake(),
                    title=Fake(), switcher_bar=Fake())
        fake._show_switcher = lambda: Window._show_switcher(fake)
        return fake

    def test_narrow_moves_the_tabs_to_the_bottom(self):
        fake = self.fake(2)
        Window._show_switcher(fake)
        self.assertIs(fake.header.called("set_title_widget")[-1][0], fake.switcher)
        self.assertEqual(fake.switcher_bar.called("set_reveal")[-1], (False,))
        Window._set_narrow(fake, True)
        self.assertIs(fake.header.called("set_title_widget")[-1][0], fake.title)
        self.assertEqual(fake.switcher_bar.called("set_reveal")[-1], (True,))
        Window._set_narrow(fake, False)
        self.assertIs(fake.header.called("set_title_widget")[-1][0], fake.switcher)

    def test_one_account_has_no_tabs(self):
        fake = self.fake(1)
        Window._set_narrow(fake, True)
        self.assertIs(fake.header.called("set_title_widget")[-1][0], fake.title)
        self.assertEqual(fake.switcher_bar.called("set_reveal")[-1], (False,))


class BannerTest(unittest.TestCase):
    def test_with_a_button(self):
        fake = Fake(banner=Fake())
        target = GLib.Variant("s", "general")
        Window.set_banner(fake, "Notifications paused until 14:30", "Resume", "app.resume-notifications", target)
        banner = fake.banner
        self.assertEqual(banner.called("set_title"), [("Notifications paused until 14:30",)])
        self.assertEqual(banner.called("set_button_label"), [("Resume",)])
        self.assertEqual(banner.called("set_action_name")[-1], ("app.resume-notifications",))
        self.assertEqual(banner.called("set_action_target_value"), [(target,)])
        self.assertEqual(banner.called("set_revealed"), [(True,)])
        # The target first: GTK checks an action against the target already there, and the
        # Details banner's "s" against none makes it warn.
        order = [call[0] for call in banner.calls if call[0].startswith("set_action")]
        self.assertEqual(order[-2:], ["set_action_target_value", "set_action_name"])
        Window.hide_banner(fake)
        self.assertEqual(banner.called("set_revealed")[-1], (False,))

    def test_without_a_button(self):
        fake = Fake(banner=Fake())
        Window.set_banner(fake, "Notifications paused")
        self.assertEqual(fake.banner.called("set_button_label"), [("",)])
        self.assertEqual(fake.banner.called("set_action_name")[-1], (None,))
        self.assertEqual(fake.banner.called("set_action_target_value"), [(None,)])
        Window.set_banner(fake, "Notifications paused", "Resume")  # a button with nothing to do isn't shown
        self.assertEqual(fake.banner.called("set_button_label")[-1], ("",))


class ToastTest(unittest.TestCase):
    """Toasts go where they can be seen: over Preferences while it's open."""

    def adw(self):
        class Toast(Fake):
            def __init__(self, **values):
                super().__init__(**values)

        class PreferencesDialog(Fake):
            pass

        return SimpleNamespace(Toast=Toast, PreferencesDialog=PreferencesDialog)

    def test_over_preferences(self):
        adw = self.adw()
        dialog = adw.PreferencesDialog()
        fake = Fake(toasts=Fake(), get_visible_dialog=lambda: dialog)
        with mock.patch.object(window, "Adw", adw):
            Window.toast(fake, "Microphone turned off. WhatsApp can't hear you")
        self.assertEqual(len(dialog.called("add_toast")), 1)
        self.assertEqual(dialog.called("add_toast")[0][0].title, "Microphone turned off. WhatsApp can't hear you")
        self.assertEqual(fake.toasts.called("add_toast"), [])

    def test_in_the_window_otherwise(self):
        adw = self.adw()
        for dialog in (None, Fake()):  # no dialog, or one with no toasts of its own (an alert)
            fake = Fake(toasts=Fake(), get_visible_dialog=lambda dialog=dialog: dialog)
            with mock.patch.object(window, "Adw", adw):
                Window.toast(fake, "Saved", "Open", "app.open-file", GLib.Variant("s", "/x"))
            toast = fake.toasts.called("add_toast")[0][0]
            self.assertEqual(toast.called("set_button_label"), [("Open",)])
            if dialog is not None:
                self.assertEqual(dialog.called("add_toast"), [])


class NoticeTest(unittest.TestCase):
    ERROR = "Error resolving “web.whatsapp.com”: Temporary failure in name resolution"

    def page(self, failed=None, crashing=False, memory_stopped=False):
        account = Fake(failed=failed, crashing=crashing, memory_stopped=memory_stopped)
        page = Fake(account=account, notice=Fake(), detail=Fake(), software=Fake(),
                    settings={"hardware_acceleration": True})
        AccountPage._show_state(page)
        return page

    def test_offline_says_it_plainly_with_the_error_as_small_print(self):
        page = self.page(failed=self.ERROR)
        description = page.notice.called("set_description")[-1][0]
        self.assertTrue(description.startswith("Check your internet connection."))
        self.assertNotIn("resolving", description)
        self.assertEqual(page.detail.called("set_label")[-1], (self.ERROR,))
        self.assertEqual(page.detail.called("set_visible")[-1], (True,))
        self.assertEqual(page.notice.called("set_visible")[-1], (True,))

    def test_crashing_has_no_small_print(self):
        page = self.page(crashing=True)
        self.assertEqual(page.detail.called("set_visible")[-1], (False,))
        self.assertEqual(page.software.called("set_visible")[-1], (True,))

    def test_stopped_for_memory_says_so_without_the_graphics_way_out(self):
        page = self.page(crashing=True, memory_stopped=True)
        self.assertEqual(page.notice.called("set_title")[-1], ("WhatsApp Used Too Much Memory",))
        self.assertIn("twice in ten minutes", page.notice.called("set_description")[-1][0])
        self.assertEqual(page.software.called("set_visible")[-1], (False,))  # drivers don't make a page grow
        self.assertEqual(page.notice.called("set_visible")[-1], (True,))

    def test_back_online(self):
        page = self.page()
        self.assertEqual(page.detail.called("set_visible")[-1], (False,))
        self.assertEqual(page.notice.called("set_visible")[-1], (False,))


class DropHintTest(unittest.TestCase):
    def test_description_is_escaped_and_hidden_under_blur(self):
        page = AccountPage.__new__(AccountPage)  # never initialised: no widget is made
        page.account, page.drop_hint, page.drop_zone = Fake(active_chat="Tom & <Jerry>"), Fake(), Fake()
        settings = {"privacy_blur": False}
        fake = Fake(stack=Fake(get_visible_child=lambda: page), app=Fake(settings=settings))
        Window._drag_hint(fake, True)
        # A status page's description is markup: the name is shown as it is, not read as tags.
        self.assertEqual(page.drop_hint.called("set_description")[-1], ("To Tom &amp; &lt;Jerry&gt;",))
        self.assertEqual(page.drop_zone.called("set_visible")[-1], (True,))
        settings["privacy_blur"] = True
        Window._drag_hint(fake, True)
        self.assertEqual(page.drop_hint.called("set_description")[-1], ("",))
        Window._drag_hint(fake, False)
        self.assertEqual(page.drop_zone.called("set_visible")[-1], (False,))


class LockPageTest(unittest.TestCase):
    def fake(self, text="", answer=(False, "Wrong passcode")):
        tries = []

        def try_unlock(passcode, done):
            tries.append(passcode)
            done(*answer)

        fake = Fake(passcode=Entry(text), unlock_button=Button(), lock_message=Fake(), _checking=False,
                    app=Fake(try_unlock=try_unlock), locks=Fake(), account=None)
        fake._can_unlock = lambda: Window._can_unlock(fake)
        return fake, tries

    def test_an_empty_passcode_isnt_tried(self):
        fake, tries = self.fake("")
        Window._unlock(fake)
        self.assertEqual(tries, [])  # it would count towards the waits
        Window._can_unlock(fake)
        self.assertFalse(fake.unlock_button.sensitive)

    def test_a_wrong_passcode_is_shown_and_announced(self):
        fake, tries = self.fake("1234")
        Window._unlock(fake)
        self.assertEqual(tries, ["1234"])
        self.assertIn("error", fake.passcode.classes)
        self.assertEqual(fake.lock_message.called("set_label"), [("Wrong passcode",)])
        self.assertEqual(fake.passcode.called("announce"),
                         [("Wrong passcode", Gtk.AccessibleAnnouncementPriority.MEDIUM)])
        self.assertFalse(fake._checking)
        self.assertTrue(fake.unlock_button.sensitive)  # the text is still there, to try again

    def test_no_second_try_while_checking(self):
        fake, tries = self.fake("1234")
        fake._checking = True
        Window._unlock(fake)
        self.assertEqual(tries, [])

    def test_locking_again_clears_the_red_outline(self):
        fake, _tries = self.fake("")
        fake.passcode.add_css_class("error")  # a wrong try, then the field emptied without "changed"
        Window.show_lock(fake, False)
        self.assertNotIn("error", fake.passcode.classes)
        self.assertFalse(fake.unlock_button.sensitive)

    def test_typing_clears_the_red_outline(self):
        fake, _tries = self.fake("12")
        fake.passcode.add_css_class("error")
        Window._passcode_changed(fake)
        self.assertNotIn("error", fake.passcode.classes)
        self.assertTrue(fake.unlock_button.sensitive)


if __name__ == "__main__":
    unittest.main()
