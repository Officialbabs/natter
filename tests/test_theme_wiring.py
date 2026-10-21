"""How the theme is wired in (account.py, app.py): the page's style sheet, the window's style
provider, following Ada's own light or dark, and what WhatsApp's page says of its own look. Fakes
stand in for WebKit, GTK and libadwaita, so no display is needed. The theme's ban-safety rules are
checked here, in test_themes.py (the page text) and in test_page.py (the bridge)."""

import ast
import contextlib
import inspect
import io
import json
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from natter import account as natter_account, app as natter_app, themes
from natter.account import WHATSAPP_PAGES, Account
from natter.ada import AdaStyle
from natter.app import Natter, color_scheme, page_look, style_hint

STYLE_HINT = style_hint(True)  # the window dark, WhatsApp kept to its own Light
from natter.prefs import Preferences, SwatchGrid, ThemeRow
from natter.settings import DEFAULTS, Settings
from natter.window import AccountPage, Window

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {path.name: path.read_text() for path in (ROOT / "natter").iterdir() if path.suffix in (".py", ".js")}


class Recorder:
    """Stands in for anything: keeps every call made on it, by name."""

    def __init__(self, *args, **props):
        self.args, self.props, self.calls = args, props, []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, *args))

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Untouchable:
    """What the theme must leave alone (the page's view, its WebKit settings, its session)."""

    def __getattr__(self, name):
        raise AssertionError(f"the theme reached for {name}")


def source(function) -> str:
    return textwrap.dedent(inspect.getsource(function))


def called_names(function) -> set[str]:
    """The names of everything `function` calls, as `x.name(...)` or `name(...)`."""
    names = set()
    for node in ast.walk(ast.parse(source(function))):
        if isinstance(node, ast.Call):
            names.add(node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
    return names


# ---- The page's style sheet (account.py) -----------------------------------------------------


class Sheet:
    """WebKit.UserStyleSheet.new's arguments."""

    def __init__(self, css, frames, level, allow, block):
        self.css, self.frames, self.level, self.allow, self.block = css, frames, level, allow, block


WEBKIT = SimpleNamespace(UserStyleSheet=SimpleNamespace(new=Sheet),
                         UserContentInjectedFrames=SimpleNamespace(TOP_FRAME="top frame", ALL_FRAMES="all frames"),
                         UserStyleLevel=SimpleNamespace(USER="user", AUTHOR="author"))


def page(theme="den", theme_off=False, custom=DEFAULTS["theme_custom"]):
    """An account as apply_theme sees it: the content manager is all it may touch."""
    return SimpleNamespace(app=SimpleNamespace(settings={"theme": theme, "theme_custom": custom}), content=Recorder(),
                           _allow=WHATSAPP_PAGES, _theme_sheet=None, _theme_css="", theme_off=theme_off,
                           view=Untouchable(), session=Untouchable())


def apply(account, theme=None, custom=None):
    if theme is not None:
        account.app.settings["theme"] = theme
    if custom is not None:
        account.app.settings["theme_custom"] = custom
    with mock.patch.object(natter_account, "WebKit", WEBKIT):
        Account.apply_theme(account)


class PageSheetTest(unittest.TestCase):
    def test_a_user_sheet_for_whatsapps_pages_only(self):
        account = page()
        apply(account)
        (sheet,), = account.content.called("add_style_sheet")
        self.assertEqual(sheet.css, themes.page_css("den"))
        self.assertEqual((sheet.frames, sheet.level, sheet.allow, sheet.block),
                         ("top frame", "user", WHATSAPP_PAGES, None))
        self.assertEqual(account.content.called("remove_style_sheet"), [])

    def test_another_theme_swaps_only_its_own_sheet(self):
        account = page()
        apply(account)
        den = account._theme_sheet
        apply(account, "wa-merino-teal")
        self.assertEqual(account.content.called("remove_style_sheet"), [(den,)])
        self.assertEqual([sheet.css for sheet, in account.content.called("add_style_sheet")],
                         [themes.page_css("den"), themes.page_css("wa-merino-teal")])
        # style.css (privacy blur) stays where it is: no flicker.
        self.assertNotIn("remove_all_style_sheets", [call[0] for call in account.content.calls])

    def test_customs_colour_swaps_its_sheet(self):
        account = page("custom", custom="#3584e4")
        apply(account)
        blue = account._theme_sheet
        self.assertEqual(blue.css, themes.page_css("custom", "#3584e4"))
        apply(account, custom="#e01b24")
        self.assertEqual(account.content.called("remove_style_sheet"), [(blue,)])
        self.assertEqual(account._theme_sheet.css, themes.page_css("custom", "#e01b24"))
        self.assertNotEqual(blue.css, account._theme_sheet.css)
        # Another theme's sheet doesn't depend on Custom's colour: nothing to swap.
        account = page("wa-pink")
        apply(account)
        apply(account, custom="#e01b24")
        self.assertEqual(len(account.content.called("add_style_sheet")), 1)
        self.assertEqual(account.content.called("remove_style_sheet"), [])

    def test_an_older_theme_is_its_nearest_whatsapp_theme(self):
        account = page("lagoon")
        apply(account)
        (sheet,), = account.content.called("add_style_sheet")
        self.assertEqual(sheet.css, themes.page_css("wa-merino-teal"))

    def test_plain_adds_none(self):
        account = page("plain")
        apply(account)
        self.assertEqual(account.content.calls, [])
        account = page()
        apply(account)
        den = account._theme_sheet
        apply(account, "plain")
        self.assertEqual(account.content.called("remove_style_sheet"), [(den,)])
        self.assertEqual(len(account.content.called("add_style_sheet")), 1)
        self.assertIsNone(account._theme_sheet)

    def test_theme_off_adds_none(self):
        account = page(theme_off=True)
        apply(account)
        self.assertEqual(account.content.calls, [])
        account.theme_off = False  # the page draws with the names again
        apply(account)
        (sheet,), = account.content.called("add_style_sheet")
        self.assertEqual(sheet.css, themes.page_css("den"))

    def test_unknown_values_are_den(self):
        account = page("solarized")
        apply(account)
        (sheet,), = account.content.called("add_style_sheet")
        self.assertEqual(sheet.css, themes.page_css("den"))

    def test_the_same_sheet_isnt_swapped(self):
        account = page()
        apply(account)
        apply(account)
        apply(account, "Den")  # not a theme id: Den, which is on already
        self.assertEqual(len(account.content.called("add_style_sheet")), 1)
        self.assertEqual(account.content.called("remove_style_sheet"), [])

    def test_on_from_the_first_page(self):
        # Added with the page's other user content, after style.css and before the view loads.
        init = source(Account.__init__)
        style, theme, view = (init.index("WebKit.UserStyleSheet.new(STYLE"), init.index("self.apply_theme()"),
                              init.index("self.view = WebKit.WebView("))
        self.assertLess(style, theme)
        self.assertLess(theme, view)
        self.assertIn("self.content = content = WebKit.UserContentManager()", init)
        # The allow list the theme sheet gets is the real one, not the test's: WhatsApp's pages,
        # and only a --url page (a developer's) goes without.
        self.assertIn("self._allow = allow = None if url else WHATSAPP_PAGES", init)


class LookMessageTest(unittest.TestCase):
    TOKEN = "f" * 32

    def account(self):
        reported = []
        account = SimpleNamespace(_token=self.TOKEN, _trusted=lambda: True, look=None,
                                  app=SimpleNamespace(page_look_changed=lambda a, fresh=False: reported.append(a)))
        return account, reported

    def test_kept_and_passed_to_the_app(self):
        account, reported = self.account()
        message = {"type": "look", "tokens": True, "dark": True, "darkClass": True, "media": False,
                   "token": self.TOKEN}
        Account._on_message(account, None, SimpleNamespace(to_json=lambda _indent: json.dumps(message)))
        self.assertEqual(account.look, {"tokens": True, "dark": True, "dark_class": True, "media": False})
        self.assertEqual(reported, [account])

    def test_without_the_token_its_ignored(self):
        account, reported = self.account()
        message = {"type": "look", "tokens": True, "dark": True, "darkClass": True, "media": False, "token": "x"}
        Account._on_message(account, None, SimpleNamespace(to_json=lambda _indent: json.dumps(message)))
        self.assertIsNone(account.look)
        self.assertEqual(reported, [])

    def test_a_new_page_says_it_again(self):
        loaded = []
        account = SimpleNamespace(_reload_timer=0, _end_link=lambda: None, _set_reloading=lambda _r: None,
                                  home="https://web.whatsapp.com/", ready=True,
                                  view=SimpleNamespace(load_uri=loaded.append),
                                  look={"tokens": True, "dark": False, "dark_class": False, "media": False})
        Account._load(account, account.home)
        self.assertIsNone(account.look)
        self.assertEqual(loaded, [account.home])
        self.assertIn("self.look = self.look_dark = None", source(Account._on_load_changed))  # WhatsApp loading itself again


# ---- Natter's window: the style provider (app.py) ---------------------------------------------


class Manager:
    """libadwaita's style manager: the style now, what's connected to it and the schemes asked of it."""

    def __init__(self, dark=False, high_contrast=False):
        self.dark, self.high_contrast, self.handlers, self.schemes = dark, high_contrast, {}, []

    def get_dark(self):
        return self.dark

    def get_high_contrast(self):
        return self.high_contrast

    def connect(self, signal, handler):
        self.handlers.setdefault(signal, []).append(handler)

    def set_color_scheme(self, scheme):
        self.schemes.append(scheme)

    def emit(self, signal):
        for handler in self.handlers.get(signal, []):
            handler(self, None)


def libraries(manager, minor=22):
    """Fake Gtk, Adw and Gdk for app.py: the provider made, and where it was added."""
    provider, added = Recorder(), []
    adw = SimpleNamespace(StyleManager=SimpleNamespace(get_default=lambda: manager), ColorScheme=Adw.ColorScheme)
    gtk = SimpleNamespace(CssProvider=lambda: provider, STYLE_PROVIDER_PRIORITY_THEME=Gtk.STYLE_PROVIDER_PRIORITY_THEME,
                          StyleContext=SimpleNamespace(
                              add_provider_for_display=lambda display, made, priority: added.append((made, priority))),
                          get_major_version=lambda: 4, get_minor_version=lambda: minor)
    gdk = SimpleNamespace(Display=SimpleNamespace(get_default=lambda: "display"))
    return mock.patch.multiple(natter_app, Adw=adw, Gtk=gtk, Gdk=gdk), provider, added


def fake_app(test, theme="den", accounts=(), window=True, custom=None):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    fake = SimpleNamespace(settings=Settings(Path(folder.name) / "settings.json"), accounts=list(accounts),
                           window=Recorder() if window else None, theme_css=Recorder(), _told=set(),
                           style_hint="", preferences=None, ada_style=None)
    if theme != themes.DEFAULT:
        fake.settings["theme"] = theme
    if custom is not None:
        fake.settings["theme_custom"] = custom
    for name in ("_apply_theme", "_apply_theme_window", "_apply_appearance", "_show_style_hint", "page_look_changed"):
        setattr(fake, name, (lambda name: lambda *args: getattr(Natter, name)(fake, *args))(name))
    return fake


def loaded(provider):
    return [call[0] for call in provider.called("load_from_string")]


def window_css(theme, dark, high_contrast=False, variables=True, custom=None, picker=False):
    """What app.py should load for the window: the theme's colours, then while Preferences is
    open (`picker`), the swatches."""
    return (themes.gtk_css(theme, dark, high_contrast, variables, custom)
            + (themes.swatch_css(dark, high_contrast, custom) if picker else ""))


class WindowThemeTest(unittest.TestCase):
    def test_above_the_theme_below_the_app(self):
        fake = fake_app(self)
        patches, provider, added = libraries(Manager())
        with patches:
            Natter._start_theme(fake)
        self.assertEqual(added, [(provider, Gtk.STYLE_PROVIDER_PRIORITY_THEME + 1)])
        self.assertLess(Gtk.STYLE_PROVIDER_PRIORITY_THEME + 1, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.assertEqual(loaded(provider), [window_css("den", False)])

    def test_follows_the_style_and_high_contrast(self):
        fake = fake_app(self, "wa-tyrian-purple")
        manager = Manager()
        patches, provider, _added = libraries(manager)
        with patches:
            Natter._start_theme(fake)
            manager.dark = True
            manager.emit("notify::dark")
            manager.high_contrast = True
            manager.emit("notify::high-contrast")
        self.assertEqual(loaded(provider)[1:], [window_css("wa-tyrian-purple", True),
                                                window_css("wa-tyrian-purple", True, True)])

    def test_the_swatches_only_while_preferences_is_open(self):
        # Every theme's colours go into them: not at start-up, nor at the first switch to the
        # other style, unless there's a picker to show them.
        fake = fake_app(self, "wa-tyrian-purple")
        manager = Manager()
        patches, provider, _added = libraries(manager)
        with patches, mock.patch.object(themes, "swatch_css", side_effect=AssertionError("swatches made")):
            Natter._start_theme(fake)
            manager.dark = True
            manager.emit("notify::dark")
        fake.preferences = Recorder()  # open
        with patches:
            manager.dark = False
            manager.emit("notify::dark")
            manager.high_contrast = True
            manager.emit("notify::high-contrast")
        self.assertEqual(loaded(provider)[2:], [window_css("wa-tyrian-purple", False, picker=True),
                                                window_css("wa-tyrian-purple", False, True, picker=True)])

    def test_preferences_loads_its_swatches_before_its_shown(self):
        order = []

        class Preferences(Recorder):
            def present(self, parent):
                order.append("present")

        fake = fake_app(self, "custom", custom="#3584e4")
        fake._held_while_locked = lambda run: False
        fake.present_window = lambda: True
        fake._media_banner = False
        fake.theme_css.load_from_string = lambda css: order.append(css)
        patches, _provider, _added = libraries(Manager(dark=True))
        with patches, mock.patch("natter.prefs.Preferences", Preferences):
            Natter.show_preferences(fake)
        self.assertEqual(order, [window_css("custom", True, custom="#3584e4", picker=True), "present"])

    def test_gtk_414_gets_named_colours_only(self):
        for minor, variables in ((14, False), (15, False), (16, True), (22, True)):
            fake = fake_app(self, "wa-lime")
            patches, provider, _added = libraries(Manager(dark=True), minor)
            with patches:
                Natter._start_theme(fake)
            text, = loaded(provider)
            with self.subTest(minor=minor):
                self.assertEqual(":root" in text, variables)
                self.assertEqual(text, window_css("wa-lime", True, variables=variables))
                if not variables:
                    self.assertNotIn("var(", text)

    def test_plain_keeps_only_the_swatches(self):
        fake = fake_app(self, "plain")
        patches, provider, _added = libraries(Manager())
        with patches:
            Natter._start_theme(fake)
            fake.preferences = Recorder()  # open
            Natter._apply_theme_window(fake)
        self.assertEqual(loaded(provider), ["", themes.swatch_css(False, False)])

    def test_the_setting_updates_the_window_every_page_and_their_colours(self):
        accounts = [Recorder(), Recorder()]
        fake = fake_app(self, accounts=accounts)
        patches, _provider, _added = libraries(Manager(dark=True))
        fake.settings.values["theme"] = "wa-pink"
        with patches:
            Natter._on_setting(fake, "theme", "wa-pink")
        self.assertEqual(loaded(fake.theme_css), [window_css("wa-pink", True)])
        self.assertEqual([account.called("apply_theme") for account in accounts], [[()], [()]])
        self.assertEqual(fake.window.called("repaint_pages"), [()])

    def test_customs_colour_updates_the_window_every_page_and_the_swatch(self):
        accounts = [Recorder(), Recorder()]
        fake = fake_app(self, "custom", accounts=accounts)
        fake.preferences = Recorder()  # open, its Custom Colour button used
        patches, provider, _added = libraries(Manager())
        with patches:
            Natter._start_theme(fake)
            fake.settings.values["theme_custom"] = "#3584e4"
            Natter._on_setting(fake, "theme_custom", "#3584e4")
        self.assertEqual(loaded(provider), [window_css("custom", False, custom="#ff7800", picker=True),
                                            window_css("custom", False, custom="#3584e4", picker=True)])
        self.assertNotEqual(*loaded(provider))
        self.assertEqual([account.called("apply_theme") for account in accounts], [[()], [()]])
        self.assertEqual(fake.window.called("repaint_pages"), [()])

    def test_customs_colour_shows_in_its_swatch_whatever_the_theme(self):
        fake = fake_app(self, "den", custom="#e01b24")
        fake.preferences = Recorder()  # open
        patches, provider, _added = libraries(Manager(dark=True))
        with patches:
            Natter._start_theme(fake)
        text, = loaded(provider)
        self.assertEqual(text, window_css("den", True, custom="#e01b24", picker=True))
        self.assertTrue(text.startswith(themes.gtk_css("den", True, False, True)))  # Den's own colours
        self.assertIn(themes.swatch("custom", True, "#e01b24")[0], text)

    def test_the_window_and_the_page_have_one_fill_and_label(self):
        # What app.py loads for the window and what account.py hands WebKit for the page carry the
        # same accent and label: Lemon's own yellow with the dark label in light (white has 1.6:1
        # on it), and Custom's colour, exactly as picked, with white.
        for theme, custom, dark, fill, window_label, page_label in (
                ("wa-lemon", None, False, "#e5cb00", "rgba(0, 0, 6, 0.8)", "#0a0a0a"),
                ("custom", "#c01c28", False, "#c01c28", "#ffffff", "#ffffff"),
                ("custom", "#613583", False, "#613583", "#ffffff", "#ffffff"),
                ("custom", "#ff7800", True, "#ef7000", "#ffffff", "#ffffff")):
            with self.subTest(theme=theme, custom=custom, dark=dark):
                fake = fake_app(self, theme, custom=custom)
                patches, provider, _added = libraries(Manager(dark=dark))
                with patches:
                    Natter._start_theme(fake)
                text, = loaded(provider)
                self.assertIn(f"@define-color accent_bg_color {fill};", text)
                self.assertIn(f"@define-color accent_fg_color {window_label};", text)
                account = page(theme, custom=custom or DEFAULTS["theme_custom"])
                apply(account)
                (sheet,), = account.content.called("add_style_sheet")
                block = sheet.css.split("body.dark, body.dark #main {" if dark else "body, #main {")[1].split("}")[0]
                self.assertIn(f"--WDS-accent: {fill} !important;", block)
                self.assertIn(f"--WDS-content-on-accent: {page_label} !important;", block)
        # Custom's swatch is the theme made from it, as the page has it: in light the colour as
        # picked, in dark its accent there, a step lighter.
        for dark, fill in ((False, "#c01c28"), (True, themes.look("custom", True, "#c01c28").page["accent"])):
            self.assertIn("checkbutton.natter-swatch.theme-custom { background-image: "
                          f"linear-gradient(135deg, {fill} 49%", window_css("den", dark, custom="#c01c28", picker=True))
        self.assertNotEqual(fill, "#c01c28")

    def test_the_setting_without_a_window(self):
        fake = fake_app(self, window=False)
        patches, _provider, _added = libraries(Manager())
        fake.settings.values["theme"] = "plain"
        with patches:
            Natter._on_setting(fake, "theme", "plain")
        self.assertEqual(loaded(fake.theme_css), [""])

    def test_every_variant_parses(self):
        # What the provider can be given: each choice, light and dark, high contrast or not, with
        # and without variables.
        errors = []
        choices = [(theme_id, None) for theme_id, _name, _describe in themes.CHOICES]
        for theme_id, custom in choices + [("custom", "#0b4b79"), ("custom", "#808080"), ("custom", "#ffffff")]:
            for dark in (False, True):
                for high_contrast in (False, True):
                    for variables in (False, True):
                        provider = Gtk.CssProvider()
                        provider.connect("parsing-error", lambda _p, _section, error, key=(theme_id, dark):
                                         errors.append((key, error.message)))
                        provider.load_from_string(themes.gtk_css(theme_id, dark, high_contrast, variables, custom)
                                                  + themes.swatch_css(dark, high_contrast, custom))
        self.assertEqual(errors, [])

    def test_startup_order(self):
        # Ada's style is known before the first style is set, and the theme follows that style.
        startup = source(Natter.do_startup)
        ada, appearance, theme = (startup.index("AdaStyle(on_change=self._apply_appearance)"),
                                  startup.index("self._apply_appearance()"), startup.index("self._start_theme()"))
        self.assertLess(ada, appearance)
        self.assertLess(appearance, theme)
        self.assertIn("if on_ada():", startup[:ada])


# ---- Following Ada's own Light, Dark or Dune (app.py, ada.py) ---------------------------------


class AppearanceTest(unittest.TestCase):
    def test_color_scheme(self):
        DEFAULT, LIGHT, DARK = Adw.ColorScheme.DEFAULT, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK
        for appearance, ada, expected in (("system", None, DEFAULT), ("system", True, DARK), ("system", False, LIGHT),
                                          ("light", True, LIGHT), ("light", None, LIGHT), ("dark", False, DARK),
                                          ("dark", None, DARK),
                                          # What Preferences shows as Follow System, so it is.
                                          ("auto", True, DARK), (None, False, LIGHT), (["dark"], None, DEFAULT)):
            with self.subTest(appearance=appearance, ada=ada):
                self.assertEqual(color_scheme(appearance, ada), expected)

    def apply(self, appearance, ada_dark, on_ada=True):
        fake = fake_app(self)
        fake.settings.values["appearance"] = appearance
        fake.ada_style = None if ada_dark == "none" else SimpleNamespace(dark=ada_dark)
        manager = Manager()
        patches, _provider, _added = libraries(manager)
        with patches, mock.patch.object(natter_app, "on_ada", return_value=on_ada):
            Natter._apply_appearance(fake)
        return manager.schemes

    def test_follows_ada_when_following_the_system_on_ada(self):
        self.assertEqual(self.apply("system", False), [Adw.ColorScheme.FORCE_LIGHT])
        self.assertEqual(self.apply("system", True), [Adw.ColorScheme.FORCE_DARK])

    def test_ada_saying_nothing_leaves_it_to_libadwaita(self):
        self.assertEqual(self.apply("system", None), [Adw.ColorScheme.DEFAULT])

    def test_light_and_dark_ignore_ada(self):
        self.assertEqual(self.apply("light", True), [Adw.ColorScheme.FORCE_LIGHT])
        self.assertEqual(self.apply("dark", False), [Adw.ColorScheme.FORCE_DARK])

    def test_off_ada_its_ignored(self):
        self.assertEqual(self.apply("system", True, on_ada=False), [Adw.ColorScheme.DEFAULT])
        self.assertEqual(self.apply("system", "none", on_ada=False), [Adw.ColorScheme.DEFAULT])
        self.assertEqual(self.apply("system", "none"), [Adw.ColorScheme.DEFAULT])

    def test_changes_along_with_ada(self):
        fake = fake_app(self)
        manager = Manager()
        patches, _provider, _added = libraries(manager)
        with tempfile.TemporaryDirectory() as folder, patches, \
                mock.patch.object(natter_app, "on_ada", return_value=True):
            ini = Path(folder) / "desktop.ini"
            ini.write_text("[Desktop]\nTheme=midnight\n")
            fake.ada_style = AdaStyle(on_change=fake._apply_appearance, path=ini)
            Natter._apply_appearance(fake)
            partial = Path(folder) / "desktop.ini.new"
            partial.write_text("[Desktop]\nTheme=paper\n")  # Light picked in Ada's Settings
            partial.replace(ini)
            context, end = GLib.MainContext.default(), time.monotonic() + 3
            while len(manager.schemes) < 2 and time.monotonic() < end:
                context.iteration(False)
                time.sleep(0.01)
            fake.ada_style.close()
        self.assertEqual(manager.schemes, [Adw.ColorScheme.FORCE_DARK, Adw.ColorScheme.FORCE_LIGHT])

    def test_quitting_lets_go_of_it(self):
        fake = fake_app(self)
        closed = []
        fake.ada_style = SimpleNamespace(close=lambda: closed.append(True))
        for name in ("notifier", "reclaimer", "call_sound", "audio_follower"):
            setattr(fake, name, Recorder())
        fake.tray = fake.calls = fake.window = fake.desktop_media = fake.memory_guard = None
        fake.quit = lambda: None
        Natter.quit_natter(fake)
        self.assertEqual(closed, [True])
        self.assertIsNone(fake.ada_style)


# ---- What WhatsApp's page says of its own look (app.py) ---------------------------------------


def look(tokens=True, dark=False, dark_class=None, media=None):
    return {"tokens": tokens, "dark": dark, "dark_class": dark if dark_class is None else dark_class,
            "media": dark if media is None else media}


class Page:
    """An account as page_look_changed sees it."""

    def __init__(self, page_look=None, ready=True, account_id="main"):
        self.id, self.name, self.ready, self.look, self.theme_off = account_id, "Personal", ready, page_look, False
        self.look_dark = None
        self.applied = 0

    def apply_theme(self):
        self.applied += 1


class PageLookTest(unittest.TestCase):
    def report(self, fake, account, page_look, dark=False, fresh=True):
        account.look = page_look
        patches, _provider, _added = libraries(Manager(dark=dark))
        out = io.StringIO()
        with patches, contextlib.redirect_stdout(out):
            Natter.page_look_changed(fake, account, fresh=fresh)
        return out.getvalue().splitlines()

    def test_a_report_from_before_a_style_change_says_nothing(self):
        account = Page()
        fake = fake_app(self, accounts=[account])
        self.report(fake, account, look(dark=False), dark=False)  # taken in the light style
        # The window turned dark; until the page's next report, the old one isn't news.
        lines = self.report(fake, account, look(dark=False), dark=True, fresh=False)
        self.assertEqual((lines, fake.style_hint, fake.window.called("toast")), ([], "", []))

    def test_the_hint_names_whatsapps_look(self):
        self.assertIn("Light", style_hint(True))
        self.assertIn("Dark", style_hint(False))
        self.assertIn("System default", style_hint(True))

    def test_page_look(self):
        self.assertEqual(page_look(look(), False), "")
        self.assertEqual(page_look(look(dark=True), True), "")
        self.assertEqual(page_look(look(tokens=False), False), "broken")  # WhatsApp renamed its colour names
        self.assertEqual(page_look(look(dark=True, dark_class=False), True), "broken")  # its class doesn't follow
        self.assertEqual(page_look(look(dark=True, media=True), False), "media")  # GTK_THEME=…:dark
        self.assertEqual(page_look(look(dark=False, media=True), True), "own")  # WhatsApp set to Light
        self.assertEqual(page_look(look(dark=True, media=False), False), "own")  # WhatsApp set to Dark

    def test_the_theme_comes_off_a_page_it_cant_follow_and_back_on(self):
        account = Page()
        fake = fake_app(self, accounts=[account])
        lines = self.report(fake, account, look(tokens=False))
        self.assertTrue(account.theme_off)
        self.assertEqual((account.applied, len(lines)), (1, 1))
        self.assertEqual(fake.window.called("toast"), [])  # nothing for the user to do about it
        lines = self.report(fake, account, look(dark=True, dark_class=False), dark=True)
        self.assertEqual((account.theme_off, account.applied, lines), (True, 1, []))  # still off: said once
        lines = self.report(fake, account, look(dark=True), dark=True)
        self.assertFalse(account.theme_off)
        self.assertEqual((account.applied, len(lines)), (2, 1))
        self.assertEqual(fake.style_hint, "")

    def test_only_once_its_ready(self):
        account = Page(ready=False)
        fake = fake_app(self, accounts=[account])
        self.assertEqual(self.report(fake, account, look(tokens=False)), [])
        self.assertEqual((account.theme_off, account.applied), (False, 0))

    def test_another_style_from_webkit_is_only_logged(self):
        account = Page()
        fake = fake_app(self, accounts=[account])
        lines = self.report(fake, account, look(dark=True), dark=False)
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.report(fake, account, look(dark=True), dark=False), [])  # once
        self.assertEqual((account.theme_off, fake.style_hint, fake.window.called("toast")), (False, "", []))

    def test_whatsapps_own_look_gets_the_hint(self):
        first, second = Page(), Page(account_id="account-2")
        fake = fake_app(self, accounts=[first, second])
        fake.preferences = Recorder()  # open
        self.report(fake, first, look(dark=False, media=True), dark=True)
        self.assertEqual(fake.style_hint, STYLE_HINT)
        self.assertEqual(fake.preferences.called("show_style_hint"), [(STYLE_HINT,)])
        (title, button, action, target), = fake.window.called("toast")
        self.assertEqual((button, action), ("Details", "app.preferences-page"))
        self.assertTrue(target.equal(GLib.Variant("s", "general")))  # where the hint is
        self.assertFalse(first.theme_off)
        # Once a run, however many accounts say so.
        self.report(fake, second, look(dark=False, media=True), dark=True)
        self.assertEqual(len(fake.window.called("toast")), 1)
        # Matched in one: the other still has a look of its own.
        self.report(fake, first, look(dark=True), dark=True)
        self.assertEqual(fake.style_hint, STYLE_HINT)
        self.report(fake, second, look(dark=True), dark=True)
        self.assertEqual(fake.style_hint, "")
        self.assertEqual(fake.preferences.called("show_style_hint"), [(STYLE_HINT,), ("",)])
        self.report(fake, second, look(dark=False, media=True), dark=True)
        self.assertEqual(len(fake.window.called("toast")), 1)

    def test_ready_again_goes_over_what_it_said(self):
        account = Page(look(tokens=False))
        fake = fake_app(self, accounts=[account])
        fake._show_unread = fake._check_media = lambda: None
        fake._linking, fake._memory_told, fake.calls = set(), set(), None
        patches, _provider, _added = libraries(Manager())
        with patches, contextlib.redirect_stdout(io.StringIO()):
            Natter._on_account_state(fake, account)
        self.assertTrue(account.theme_off)

    def test_preferences_shows_the_hint(self):
        made = []

        class Preferences(Recorder):
            def __init__(self, app):
                super().__init__(app)
                made.append(self)

        fake = fake_app(self)
        fake._held_while_locked = lambda run: False
        fake.present_window = lambda: True
        fake._media_banner = False
        with mock.patch("natter.prefs.Preferences", Preferences):
            Natter.show_preferences(fake)
            self.assertEqual(made[0].called("show_style_hint"), [])  # nothing to say
            fake.style_hint = STYLE_HINT
            Natter.show_preferences(fake)
        dialog = made[1]
        self.assertEqual(dialog.called("show_style_hint"), [(STYLE_HINT,)])
        self.assertIs(fake.preferences, dialog)
        closed, = [handler for signal, handler in dialog.called("connect") if signal == "closed"]
        closed(made[0])  # an older one closing leaves the open one
        self.assertIs(fake.preferences, dialog)
        closed(dialog)
        self.assertIsNone(fake.preferences)


# ---- The ban-safety rules (README: How it works, Themes) --------------------------------------


THEME_CODE = [Account.apply_theme, Natter._start_theme, Natter._apply_theme_window, Natter._apply_theme,
              Natter._apply_appearance, Natter.page_look_changed, Natter._show_style_hint,
              Natter.show_preferences,  # which loads the picker's swatches
              # The page's colour before WhatsApp paints, and the picker with Custom's colour.
              AccountPage.paint, Window.repaint_pages, Preferences._theme_row, Preferences._show_theme,
              Preferences._theme_toggled, Preferences._custom_row, Preferences._show_custom,
              Preferences._custom_chosen, ThemeRow._key_pressed, ThemeRow._leave, ThemeRow.do_grab_focus,
              SwatchGrid.do_size_allocate]


class BanSafetyTest(unittest.TestCase):
    def test_the_page_text_goes_in_only_as_a_user_sheet(self):
        # page_css is handed to WebKit in one place, apply_theme, and nowhere put in the page.
        users = [name for name, text in SOURCES.items() if "page_css(" in text and name != "themes.py"]
        self.assertEqual(users, ["account.py"])
        self.assertEqual(SOURCES["account.py"].count("page_css("), 1)
        self.assertIn("themes.page_css(", source(Account.apply_theme))
        for name, text in SOURCES.items():
            for banned in ("<style", "insertRule", "adoptedStyleSheets", "remove_all_style_sheets"):
                self.assertNotIn(banned, text, name)

    def test_light_and_dark_only_through_libadwaita(self):
        # Never WhatsApp's own storage, where its theme and synced settings live: not from the
        # page's scripts, nor from the Python that runs code in the page or themes it.
        for name in ("bridge.js", "rtc.js", "calls.js", "account.py", "app.py", "themes.py", "window.py", "prefs.py",
                     "ada.py", "callwindow.py"):
            text = SOURCES[name]
            for banned in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB", "system-theme-mode",
                           "webpackChunk"):
                self.assertNotIn(banned, text, name)
        for name in ("bridge.js", "rtc.js", "calls.js"):
            for key in ('"theme"', "'theme'", "setTheme", "getTheme"):
                self.assertNotIn(key, SOURCES[name], name)
        self.assertIn("set_color_scheme", source(Natter._apply_appearance))

    def test_no_page_script_for_the_theme(self):
        for function in THEME_CODE:
            for banned in ("run", "evaluate_javascript", "call_async_javascript_function", "set_class", "load_uri",
                           "reload", "fresh_page"):
                self.assertNotIn(banned, called_names(function), function.__qualname__)

    def test_never_the_user_agent_settings_session_or_requests(self):
        for function in THEME_CODE:
            text = source(function)
            for banned in ("user_agent", "get_settings", "set_property", "session", "decide-policy", "Settings(",
                           "set_feature_enabled", "WebsitePolicies", "UserScript"):
                self.assertNotIn(banned, text, function.__qualname__)
        self.assertEqual(called_names(Account.apply_theme), {"page_css", "remove_style_sheet", "new",
                                                             "add_style_sheet"})

    def test_themes_is_text_only(self):
        # Colour maths and text: no GTK, WebKit, files or network, so nothing it makes can do more
        # than the CSS it returns.
        tree = ast.parse(SOURCES["themes.py"])
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or "." for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "math", "dataclasses", "functools", "typing", "."})
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(calls & {"open", "exec", "eval", "__import__"})

    def test_theme_state_is_only_natters_setting(self):
        account = Page()
        fake = fake_app(self, accounts=[account])
        written = []
        fake.settings.connect(lambda key, value: written.append(key))
        patches, _provider, _added = libraries(Manager(dark=True))
        fake.settings["theme"] = "custom"
        fake.settings["theme_custom"] = "#3584e4"
        with patches, contextlib.redirect_stdout(io.StringIO()):
            Natter._on_setting(fake, "theme", "custom")
            Natter._on_setting(fake, "theme_custom", "#3584e4")
            for page_look_now in (look(tokens=False), look(dark=False, media=True), look(dark=True)):
                account.look = page_look_now
                Natter.page_look_changed(fake, account)
        self.assertEqual(written, ["theme", "theme_custom"])  # the user's own choices, and nothing else

    def test_an_older_theme_isnt_written_over(self):
        # "lagoon" reads as its nearest WhatsApp theme; Natter doesn't rewrite the setting for it.
        account = Page()
        fake = fake_app(self, "lagoon", accounts=[account])
        written = []
        fake.settings.connect(lambda key, value: written.append(key))
        patches, provider, _added = libraries(Manager())
        with patches:
            Natter._start_theme(fake)
            Natter._apply_theme(fake)
        self.assertEqual(written, [])
        self.assertEqual(fake.settings["theme"], "lagoon")
        self.assertEqual(loaded(provider)[0], window_css("wa-merino-teal", False))

    def test_whatsapps_own_theme_settings_are_never_named(self):
        # Natter paints what WhatsApp's chat themes look like; it never sets, reads or asks for
        # WhatsApp's own chat theme, wallpaper or app theme, nor turns on the flags behind them.
        for name in ("account.py", "app.py", "themes.py", "window.py", "prefs.py", "settings.py", "ada.py",
                     "bridge.js", "rtc.js", "calls.js"):
            for banned in ("chatThemeId", "colorSchemeId", "appTheme", "APP_THEME", "wa_web_chat_themes",
                           "aura_", "CUSTOM_APP_THEME", "ChatThemeDefinitions.load", "setWallpaper",
                           "WhatsApp Plus"):
                self.assertNotIn(banned, SOURCES[name], (name, banned))
        # Nor reaches into WhatsApp's own modules (its chat themes live there), from any page script.
        for name in ("bridge.js", "rtc.js", "calls.js"):
            for banned in ("require(", "__d(", "webpack", "importNamespace"):
                self.assertNotIn(banned, SOURCES[name], (name, banned))

    def test_customs_colour_cant_carry_css(self):
        # The one value a user types or picks that reaches the page's sheet: anything but
        # "#rrggbb" is Den's orange before it gets near the CSS.
        den_orange = themes.page_css("custom", themes.CUSTOM_DEFAULT)
        for value in ("#fff;}body{background:url(https://example.com/x)}", "#ff0000 !important; --x: 1",
                      "red", "#ff0000/*", "#ff0000\n}", "url(#ff0000)", "#ff00001"):
            self.assertEqual(themes.custom_colour(value), themes.CUSTOM_DEFAULT, value)
            self.assertEqual(themes.page_css("custom", value), den_orange, value)
            self.assertEqual(themes.gtk_css("custom", True, False, True, value),
                             themes.gtk_css("custom", True, False, True, themes.CUSTOM_DEFAULT), value)
            self.assertNotIn("url(", themes.swatch_css(False, False, value), value)

    def test_plain_stays_available(self):
        self.assertIn(themes.PLAIN, themes.GROUPS[0][1])  # with Den and Custom, Natter Den's own
        self.assertIn(themes.PLAIN, [theme_id for theme_id, _name, _describe in themes.CHOICES])
        self.assertEqual(themes.page_css(themes.PLAIN), "")
        self.assertEqual(themes.page_css(themes.PLAIN, "#ff0000"), "")
        self.assertEqual(themes.chosen("plain"), "plain")


class SettingsTest(unittest.TestCase):
    def test_den_by_default(self):
        self.assertEqual(DEFAULTS["theme"], "den")
        self.assertEqual(DEFAULTS["appearance"], "system")
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder) / "settings.json")  # an upgrade: no "theme" in the file
            self.assertEqual(themes.chosen(settings["theme"]), "den")

    def test_customs_colour_by_default(self):
        # Den's orange, until a colour is picked; an upgrade has no "theme_custom" in its file.
        self.assertEqual(DEFAULTS["theme_custom"], themes.CUSTOM_DEFAULT)
        self.assertEqual(themes.custom_colour(DEFAULTS["theme_custom"]), DEFAULTS["theme_custom"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"theme": "custom"}))
            settings = Settings(path)
            self.assertEqual(themes.custom_colour(settings["theme_custom"]), "#ff7800")
            self.assertEqual(themes.page_css(settings["theme"], settings["theme_custom"]),
                             themes.page_css("custom", "#ff7800"))


if __name__ == "__main__":
    unittest.main()
