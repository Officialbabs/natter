"""Privacy blur and Cover When in the Background, made cheap. The privacy blur (style.css) uses no
CSS filter: a filter on every row painted each row again on its own on every repaint, and froze a
PC while WhatsApp's chat scrolled. Words go see-through with a soft shadow, pictures aren't drawn,
and each row, the header and the message box still show when pointed at (or, the message box,
when it has the keys). While another window is in front, an opaque cover drawn by GTK lies over
every page (window.py), in place of a blur of the whole page on every frame. No display needed."""

import ast
import inspect
import re
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from natter import themes, window
from natter.app import Natter
from natter.window import AccountPage, Window

STYLE = (Path(window.__file__).parent / "style.css").read_text()

# What privacy blur hides, as style.css names it, and whether it holds pictures too.
AREAS = {
    '#pane-side [role="listitem"]': True,
    '#pane-side [role="row"]': True,
    "#main header": True,
    '#main [role="row"]': True,
    "#main .message-in": True,
    "#main .message-out": True,
    'footer [contenteditable="true"]': True,  # pasted emoji are pictures
}


def rules(css):
    """(selectors, {property: value}) for each rule in `css`, comments left out."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        # A selector list splits at commas outside brackets: ":not(svg, svg *)" is one selector.
        selectors, depth, part = [], 0, ""
        for ch in selector:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and not depth:
                selectors.append(part.strip())
                part = ""
            else:
                part += ch
        selectors.append(part.strip())
        declarations = {}
        for declaration in body.split(";"):
            if ":" in declaration:
                name, value = declaration.split(":", 1)
                declarations[name.strip()] = value.strip()
        out.append((selectors, declarations))
    return out


class Fake:
    """Records the methods called on it, by name, and answers each with another Fake."""

    def __init__(self, **values):
        self.calls = []
        self.__dict__.update(values)

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, *args))
            return Fake()
        return method

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class PrivacyBlurTest(unittest.TestCase):
    def test_no_filter_and_no_transition(self):
        # The costly part: a filter is an extra pass for each element it's on, on every repaint.
        for selectors, declarations in rules(STYLE):
            for name in declarations:
                with self.subTest(selector=selectors[0], name=name):
                    self.assertNotIn("filter", name)
                    self.assertNotIn("transition", name)
                    self.assertNotIn("animation", name)
                    self.assertNotIn("will-change", name)
                    self.assertNotIn("backdrop", name)

    def test_away_is_not_in_the_page(self):
        self.assertNotIn("natter-away", STYLE)
        self.assertNotIn("#app", STYLE)

    def test_every_rule_hangs_off_the_class_and_shows_when_pointed_at(self):
        for selectors, _declarations in rules(STYLE):
            for selector in selectors:
                with self.subTest(selector=selector):
                    self.assertTrue(selector.startswith("html.natter-blur "))
                    self.assertIn(":not(:hover)", selector)
                    if "contenteditable" in selector:
                        self.assertIn(":not(:focus)", selector)  # typed in, it can be read

    def words_rule(self):
        (selectors, declarations), = [rule for rule in rules(STYLE) if "text-shadow" in rule[1]]
        return selectors, declarations

    def test_words_are_see_through_with_a_soft_shadow(self):
        _selectors, declarations = self.words_rule()
        # !important: a user style sheet's rules otherwise give way to WhatsApp's own colours.
        self.assertEqual(declarations["color"], "transparent !important")
        self.assertEqual(declarations["-webkit-text-fill-color"], "transparent !important")
        x, y, blur, ink = declarations["text-shadow"].removesuffix(" !important").split(" ", 3)
        self.assertEqual((x, y), ("0", "0"))
        self.assertGreaterEqual(int(blur.removesuffix("px")), 12)  # as hard to read as the old blur(7px)
        # WhatsApp's own text colour, which follows its light and dark; Natter's themes leave it be.
        self.assertTrue(ink.startswith("var(--WDS-content-default"))
        self.assertNotIn("content-default", themes.PAGE_NAMES)

    def test_every_area_and_what_is_in_it(self):
        selectors, _declarations = self.words_rule()
        for area in AREAS:
            with self.subTest(area=area):
                matching = [s for s in selectors if s.startswith(f"html.natter-blur {area}:not(:hover)")]
                self.assertEqual(len(matching), 2)  # the area itself and everything in it

    def test_pictures_are_not_drawn(self):
        (selectors, declarations), = [rule for rule in rules(STYLE) if "visibility" in rule[1]]
        self.assertEqual(declarations, {"visibility": "hidden !important"})
        for area, pictures in AREAS.items():
            if pictures:
                with self.subTest(area=area):
                    self.assertTrue(any(s.startswith(f"html.natter-blur {area}:not(:hover)") and "img" in s
                                        for s in selectors))

    def test_icons_keep_their_shape(self):
        selectors, _declarations = self.words_rule()
        for selector in selectors:
            if "contenteditable" not in selector and not selector.endswith(":not(:hover)"):
                with self.subTest(selector=selector):
                    self.assertTrue(selector.endswith(":not(svg, svg *)"))


class AwayCoverTest(unittest.TestCase):
    def app(self, active, setting, has_window=True):
        fake = Fake(settings={"blur_when_away": setting}, locked=False, accounts=[Fake()],
                    notifier=Fake())
        fake._apply_page_sound = lambda: None
        fake.window = Fake(account=None) if has_window else None
        Natter.on_focus_changed(fake, active)
        return fake

    def test_the_cover_follows_the_window_and_the_setting(self):
        for active, setting, away in ((False, True, True), (True, True, False), (False, False, False),
                                      (True, False, False)):
            fake = self.app(active, setting)
            with self.subTest(active=active, setting=setting):
                self.assertEqual(fake.window.called("set_away"), [(away,)])
                self.assertEqual(fake.accounts[0].called("set_class"), [])  # nothing in the page

    def test_no_window_yet(self):
        self.app(False, True, has_window=False)

    def test_every_page_and_pages_added_later(self):
        shown = {}
        pages = {name: Fake(get_child=lambda name=name: Fake(set_away=lambda away, name=name: shown.__setitem__(name, away)))
                 for name in ("main", "account-2")}
        fake = Fake(pages=pages, away=False)
        Window.set_away(fake, True)
        self.assertEqual((fake.away, shown), (True, {"main": True, "account-2": True}))
        Window.set_away(fake, False)
        self.assertEqual(shown, {"main": False, "account-2": False})

        # An account added while in the background comes under the cover too.
        made = []

        class Page(Fake):
            def __init__(self, *_args):
                super().__init__()
                made.append(self)

        fake = Fake(pages={}, away=True, app=Fake(settings={}), stack=Fake())
        with mock.patch.object(window, "AccountPage", Page):
            Window.add_account(fake, Fake(id="account-3", name="Work"))
        page, = made
        self.assertEqual(page.called("set_away"), [(True,)])

    def test_the_cover_shows_and_goes_at_once(self):
        page = Fake(away_cover=Fake())
        AccountPage.set_away(page, True)
        AccountPage.set_away(page, False)
        self.assertEqual(page.away_cover.called("set_visible"), [(True,), (False,)])

    def test_the_cover_is_opaque_and_under_the_hint_and_notice(self):
        source = textwrap.dedent(inspect.getsource(AccountPage.__init__))
        overlays = [ast.unparse(node.args[0]) for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_overlay"]
        self.assertEqual(overlays, ["self.away_cover", "self.drop_zone", "self.notice"])
        made = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign)
                    and ast.unparse(node.targets[0]) == "self.away_cover")
        keywords = {k.arg: ast.unparse(k.value) for k in made.value.keywords}
        self.assertEqual(keywords["css_classes"], "['natter-cover']")  # the lock's opaque colour
        self.assertEqual(keywords["visible"], "False")
        self.assertNotIn("can_target", keywords)  # it takes the click that brings the window forward
        rule = window.CSS[window.CSS.index(".natter-cover"):]
        self.assertIn("background-color: black; background-image: image(@window_bg_color)", rule[:rule.index("}")])


if __name__ == "__main__":
    unittest.main()
