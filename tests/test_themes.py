"""Natter Den's themes (themes.py): Den and Custom made the way WhatsApp makes its own chat themes,
WhatsApp's 22 chat themes exactly as WhatsApp ships them, WhatsApp's own surfaces left alone,
everything Natter makes readable, and nothing in the page but colour names."""

import ast
import itertools
import re
import unittest
from pathlib import Path
from unittest import mock

from natter import NAME, themes

DECLARATION = re.compile(r"^  (--(?:WDS|splashscreen)-[A-Za-z0-9-]+): ([^;{}]+) !important;$")
SELECTORS = {"html", "body, #main", "body.dark, body.dark #main"}
HEX = re.compile(r"^#[0-9a-f]{6}$")
LEGACY_RGBA = re.compile(r"^rgba\(\d{1,3}, \d{1,3}, \d{1,3}, (?:0(?:\.\d+)?|1)\)$")
TRIPLET = re.compile(r"^\d{1,3}, \d{1,3}, \d{1,3}$")  # WhatsApp's -RGB twins
# A one-line comment and nothing else on it: a "*/" inside could close it early and slip a rule in.
COMMENT = re.compile(r"^/\* [^*{};\\]* \*/$")

# WhatsApp's own values (client revision 1050968285), written out here so the module can't drift
# away from them unnoticed.
WA_GREEN_500 = "#1daa61"
WA_LABEL = {False: "#ffffff", True: "#0a0a0a"}  # content-on-accent in its light and dark looks
# Its chat-theme picker, in order, with the names it shows (WAWebChatThemeDefinitions).
WA_PICKER = ("WhatsApp green", "Pearl indigo", "Tyrian purple", "Sunset orange", "Merino teal", "Royal blue",
             "Dark cerulean", "Charcoal green", "Persian plum", "Dune mono", "Default blue", "Default mono",
             "Sky blue", "Brown", "Cream", "Emerald", "Lemon", "Lime", "Pink", "Red", "Sunrise orange",
             "Warm yellow")
# Three of its tonal chat themes as shipped (WAWebChatTheme*.stylex), name by name.
WA_TONAL = {
    ("wa-sunset-orange", False): {
        "accent": "#e56d53", "accent-deemphasized": "#fee2d8", "accent-emphasized": "#6b3424",
        "content-action-emphasized": "#e56d53", "content-external-link": "#e56d53", "content-read": "#007bfc",
        "systems-bubble-surface-outgoing": "#fee2d8", "systems-chat-background-wallpaper": "#fcf4e5",
        "systems-chat-foreground-wallpaper": "#fae4de"},
    ("wa-sunset-orange", True): {
        "accent": "#e56d53", "accent-deemphasized": "#35221e", "accent-emphasized": "#fee2d8",
        "content-action-emphasized": "#e56d53", "content-external-link": "#e56d53", "content-read": "#53a6fd",
        "systems-bubble-surface-outgoing": "#4c2c24", "systems-chat-background-wallpaper": "#0a0a0a",
        "systems-chat-foreground-wallpaper": "#2a1b18"},
    ("wa-lemon", False): {
        "accent": "#e5cb00", "accent-deemphasized": "#fefcdc", "accent-emphasized": "#5c5100",
        "content-action-emphasized": "#bfa40d", "content-external-link": "#bfa40d", "content-read": "#007bfc",
        "systems-bubble-surface-outgoing": "#fdf8af", "systems-chat-background-wallpaper": "#fefde6",
        "systems-chat-foreground-wallpaper": "rgba(0, 0, 0, 0.05)"},
    ("wa-whatsapp-green", True): {
        "accent": "#21c063", "accent-deemphasized": "#103529", "accent-emphasized": "#d9fdd3",
        "content-action-emphasized": "#21c063", "content-external-link": "#21c063", "content-read": "#53bdeb",
        "systems-bubble-surface-outgoing": "#144d37", "systems-chat-background-wallpaper": "#0a0a0a",
        "systems-chat-foreground-wallpaper": "rgba(255, 255, 255, 0.1)"},
}
# Its three pale fills, where its white label has under 3:1 (Lemon 1.63, Warm yellow 1.90, Cream 2.28).
WA_PALE = {"wa-lemon": 1.63, "wa-warm-yellow": 1.90, "wa-cream": 2.28}
# The three that also set their bubbles' grey text, light and dark, and leave its -RGB twin alone.
WA_META = {theme_id: ("rgba(17, 27, 33, 0.7)", "rgba(255, 255, 255, 0.5)")
           for theme_id in ("wa-default-blue", "wa-default-mono", "wa-sky-blue")}
META = "systems-bubble-content-deemphasized"
# The page name each of WhatsApp's chat-theme roles (themes.WA_ROLES) goes to.
ROLE_NAMES = {"accent": ("accent",), "soft": ("accent-deemphasized",), "strong": ("accent-emphasized",),
              "link": ("content-action-emphasized", "content-external-link"), "read": ("content-read",),
              "outgoing": ("systems-bubble-surface-outgoing",), "wallpaper": ("systems-chat-background-wallpaper",),
              "doodle": ("systems-chat-foreground-wallpaper",)}

# Colours a user might pick for Custom: the default, WhatsApp-like ones, pale, dark, grey, the extremes.
CUSTOMS = ("#ff7800", "#0b4b79", "#731a2f", "#e5cb00", "#ffc0cb", "#808080", "#3c503a", "#5e47de", "#00ff00",
           "#0000ff", "#000000", "#ffffff", "#c01c28", "#613583", "#3584e4", "#26a269", "#f6d32d")
# GNOME's own palette colours (and CSS's orange), each a colour white reads on (3:1) or not: Custom's
# accent in light is the colour itself where it does.
WHITE_READS = ("#c01c28", "#613583", "#3584e4", "#26a269", "#1c71d8", "#e01b24", "#9141ac", "#0b4b79",
               "#731a2f", "#c64600")
# Pale yellows, creams and limes: darkened until white reads on them they'd turn mustard, tan or olive.
PALE = ("#f6d32d", "#f8e45c", "#e5a50a", "#e5cb00", "#c1a886", "#d2b48c", "#ffff00", "#cddc39", "#b5e61d")
# Oranges and other light colours: darkened until white reads, they stay what they were.
DARKENED = ("#ff7800", "#ffa348", "#ffa500", "#ffbe6f", "#33d17a", "#99c1f1", "#ffc0cb", "#00ff00", "#8ae234")


def blocks(css):
    """{selector: {name: value}} for the page's style sheet, and every line that's neither."""
    found, current, other = {}, None, []
    for line in css.splitlines():
        if line.endswith("{") and not line.startswith("@"):
            current = line[:-1].strip()
            found.setdefault(current, {})
        elif DECLARATION.match(line):
            name, value = DECLARATION.match(line).groups()
            found[current][name] = value
        elif line not in ("}", "@media (prefers-color-scheme: dark) {") and not COMMENT.match(line):
            other.append(line)
    return found, other


# Exactly the names the page's sheet may set, each with its -RGB twin: WhatsApp's accent names and
# the three chat names its own chat themes change (and on three of them, the bubbles' grey text,
# without a twin: WA_META). Its surfaces are never set.
ACCENT = ("accent", "persistent-always-branded", "content-on-accent", "accent-deemphasized",
          "components-filter-surface-selected", "accent-emphasized", "content-action-emphasized",
          "content-external-link", "content-read")
CHAT = ("systems-chat-background-wallpaper", "systems-chat-foreground-wallpaper", "systems-bubble-surface-outgoing")
NEVER = ("surface-default", "surface-elevated-default", "surface-emphasized", "surface-elevated-emphasized",
         "components-surface-nav-bar", "background-wash-plain", "background-wash-inset",
         "background-elevated-wash-plain", "background-elevated-wash-inset", "systems-chat-surface-tray",
         "systems-chat-surface-composer", "systems-bubble-surface-incoming", "systems-bubble-surface-system",
         "systems-bubble-surface-overlay", "content-default", "content-deemphasized")


def wds(names):
    return {f"--WDS-{n}" for n in names} | {f"--WDS-{n}-RGB" for n in names}


PAGE_NAMES = wds(ACCENT + CHAT)
SPLASH_NAMES = {"--splashscreen-startup-background", "--splashscreen-startup-background-rgb",
                "--splashscreen-startup-background-plain", "--splashscreen-progress-primary"}


def L(value):
    return themes.oklch(value)[0]


def C(value):
    return themes.oklch(value)[1]


def H(value):
    return themes.oklch(value)[2]


def hue_gap(a, b):
    return abs((H(a) - H(b) + 180) % 360 - 180)


def sheets():
    """(what, page_css) for every theme with a sheet: each fixed one, and Custom in several colours."""
    return [(t.id, themes.page_css(t.id)) for t in themes.THEMES] + \
           [(f"custom {c}", themes.page_css(themes.CUSTOM, c)) for c in CUSTOMS]


def looks():
    """(what, Theme, dark, Look) for every fixed theme and Custom in several colours, both styles."""
    out = []
    for dark in (False, True):
        out += [(t.id, t, dark, themes.look(t.id, dark)) for t in themes.THEMES]
        out += [(f"custom {c}", themes.theme(themes.CUSTOM, c), dark, themes.look(themes.CUSTOM, dark, c))
                for c in CUSTOMS]
    return out


def made():
    """The themes made the WhatsApp way (not copied from WhatsApp): Den and Custom in several colours."""
    return [themes.BY_ID["den"]] + [themes.theme(themes.CUSTOM, c) for c in CUSTOMS]


class CatalogueTest(unittest.TestCase):
    def test_the_groups(self):
        # Natter Den's own (Den, Custom, Plain), then WhatsApp's in its own picker's order.
        (own, own_ids), (wa, wa_ids) = themes.GROUPS
        self.assertEqual((own, own_ids), (NAME, ("den", "custom", "plain")))
        self.assertEqual(wa, "WhatsApp")
        self.assertEqual([themes.BY_ID[i].name for i in wa_ids], list(WA_PICKER))
        self.assertEqual([i for i, _, _ in themes.CHOICES], list(own_ids + wa_ids))
        self.assertEqual(len({name for _, name, _ in themes.CHOICES}), len(themes.CHOICES))
        for theme_id in wa_ids:
            self.assertTrue(re.fullmatch(r"wa-[a-z]+(-[a-z]+)*", theme_id), theme_id)
            self.assertEqual(themes.BY_ID[theme_id].group, "WhatsApp")
        self.assertEqual(themes.BY_ID["den"].group, NAME)
        self.assertEqual(themes.DEFAULT, "den")

    def test_words(self):
        names = {theme_id: (name, describe) for theme_id, name, describe in themes.CHOICES}
        self.assertEqual(names["den"][0], "Den")
        self.assertEqual(names["custom"][0], "Custom")
        self.assertEqual(names["plain"][0], "Plain")
        self.assertEqual(names["wa-merino-teal"][0], "Merino teal")
        for name, describe in names.values():
            self.assertTrue(describe and not describe.endswith("."), describe)
            self.assertNotIn("'", describe)  # typographic apostrophes, as the rest of the interface

    def test_old_themes_are_their_nearest_whatsapp_theme(self):
        for old, new in (("lagoon", "wa-merino-teal"), ("meadow", "wa-lime"), ("dusk", "wa-tyrian-purple"),
                         ("rose", "wa-pink"), ("graphite", "wa-dune-mono")):
            self.assertEqual(themes.chosen(old), new)
            self.assertEqual(themes.page_css(old), themes.page_css(new))
            self.assertEqual(themes.gtk_css(old, True, False, True), themes.gtk_css(new, True, False, True))
            self.assertNotIn(old, themes.BY_ID)

    def test_unknown_settings_fall_back_to_den(self):
        for value in (None, "", "Den", "solarized", 3, ["den"], {"theme": "wa-pink"}, "wa-", "wa-neon", "WA-PINK",
                      "Lagoon"):
            self.assertEqual(themes.chosen(value), "den", value)
        for value in ("plain", "custom", "den", "wa-pink"):
            self.assertEqual(themes.chosen(value), value)

    def test_tonal_only(self):
        # WhatsApp's vibrant bubbles rely on it turning the bubble's text white itself, which colour
        # names alone can't do: Natter carries the tonal form only, a soft sent bubble.
        for theme_id in [i for i, _, _ in themes.CHOICES]:
            self.assertNotRegex(theme_id, "vibrant|minimal")
        for theme in themes.WA_THEMES:
            page = themes.look(theme.id, False).page
            self.assertNotEqual(page["systems-bubble-surface-outgoing"], page["accent"], theme.id)
            self.assertGreater(L(page["systems-bubble-surface-outgoing"]), 0.85, theme.id)


class WhatsAppThemesTest(unittest.TestCase):
    """WhatsApp's own chat themes: its colours exactly, its label rule, Natter's floors in its window."""

    def test_whatsapps_own_values(self):
        for (theme_id, dark), values in WA_TONAL.items():
            page = themes.look(theme_id, dark).page
            for name, value in values.items():
                self.assertEqual(page[name], value, (theme_id, dark, name))

    def test_every_one_is_its_table_row(self):
        for theme in themes.WA_THEMES:
            for dark in (False, True):
                page = themes.look(theme.id, dark).page
                for role, value in zip(themes.WA_ROLES, theme.wa[dark]):
                    for name in ROLE_NAMES[role]:
                        self.assertEqual(page[name], value, (theme.id, dark, name))
                # Names WhatsApp's chat themes leave to the app's own look, set the way it sets them.
                self.assertEqual(page["persistent-always-branded"], page["accent"])
                self.assertEqual(page["components-filter-surface-selected"], page["accent-deemphasized"])
                label = "#0a0a0a" if theme.id in WA_PALE else WA_LABEL[dark]
                self.assertEqual(page["content-on-accent"], label, (theme.id, dark))
                self.assertEqual(page.get(META), WA_META[theme.id][dark] if theme.id in WA_META else None)

    def test_three_set_their_bubbles_grey_text(self):
        # Default blue, Default mono and Sky blue set the bubbles' times and voice-message grey
        # their own way (WAWebChatTheme*.stylex); every other theme leaves WhatsApp's own.
        for what, css in sheets():
            found, _ = blocks(css)
            theme_id = what if what in WA_META else None
            for dark, selector in ((False, "body, #main"), (True, "body.dark, body.dark #main")):
                with self.subTest(theme=what, dark=dark):
                    if theme_id:
                        self.assertEqual(found[selector][f"--WDS-{META}"], WA_META[theme_id][dark])
                    else:
                        self.assertNotIn(f"--WDS-{META}", found[selector])
                    # Its -RGB twin stays WhatsApp's own (black or white), as WhatsApp's theme leaves it.
                    self.assertNotIn(f"--WDS-{META}-RGB", found[selector])
        self.assertEqual({t.id for t in themes.WA_THEMES if t.meta}, set(WA_META))

    def test_its_pale_fills_take_its_dark_label(self):
        # White on Lemon, Warm yellow and Cream has under 3:1 (WhatsApp's own), and across the whole
        # page those fills label the chat list's unread badges: there the label is WhatsApp's dark one.
        # All 22 checked: exactly these three.
        pale = {theme.id: themes.contrast("#ffffff", theme.wa[False][0]) for theme in themes.WA_THEMES
                if themes.contrast("#ffffff", theme.wa[False][0]) < 3.0}
        self.assertEqual(set(pale), set(WA_PALE))
        for theme_id, ratio in pale.items():
            self.assertAlmostEqual(ratio, WA_PALE[theme_id], places=2)
            light = themes.look(theme_id, False)
            self.assertEqual(light.page["content-on-accent"], "#0a0a0a")
            self.assertGreaterEqual(light.checks["page label on accent"], 7)
            self.assertEqual(light.page["accent"], themes.BY_ID[theme_id].wa[False][0])  # its own fill still
        for theme in themes.WA_THEMES:
            if theme.id not in WA_PALE:
                self.assertGreaterEqual(themes.contrast("#ffffff", theme.wa[False][0]), 3.0, theme.id)
                self.assertEqual(themes.look(theme.id, False).page["content-on-accent"], "#ffffff")

    def test_window_matches_the_page(self):
        # The window has the page's fill and label: on Lemon, WhatsApp's own fill with the dark label
        # (libadwaita's, laid on it), not a darker yellow under white.
        light = themes.look("wa-lemon", False)
        self.assertEqual(light.gtk["accent_bg_color"], "#e5cb00")
        self.assertEqual(light.gtk["accent_fg_color"], "rgba(0, 0, 6, 0.8)")
        self.assertGreaterEqual(light.checks["gtk label on accent"], 7)
        for theme in themes.WA_THEMES:
            for dark in (False, True):
                shown = themes.look(theme.id, dark)
                self.assertEqual(shown.gtk["accent_bg_color"], shown.page["accent"], (theme.id, dark))
                white = shown.page["content-on-accent"] == "#ffffff"
                self.assertEqual(shown.gtk["accent_fg_color"], "#ffffff" if white else "rgba(0, 0, 6, 0.8)")

    def test_floors_on_the_page_are_whatsapps_lowest(self):
        lemon, den = themes.BY_ID["wa-lemon"], themes.BY_ID["den"]
        on_list = themes.floor("page accent on surface-default", lemon)
        self.assertLess(on_list, 3.0)
        self.assertAlmostEqual(on_list, themes.contrast("#ffffff", "#e5cb00"))  # Lemon's own, the lowest
        # Every label WhatsApp's themes have now reaches Natter's floor: WhatsApp green's 3.01:1 is the least.
        self.assertEqual(themes.floor("page label on accent", lemon), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("gtk label on accent", lemon), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("gtk accent text on view_bg_color", lemon), 4.5)
        self.assertEqual(themes.floor("page label on accent", den), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("page link on surface-default", den), 4.5)
        self.assertEqual(themes.floor("page accent on surface-default", den), 3.0)
        for theme in themes.WA_THEMES:
            for what in themes.look(theme.id, False).checks:
                self.assertLessEqual(themes.floor(what, theme), themes.floor(what))


class ReadableTest(unittest.TestCase):
    def test_every_theme_in_every_style(self):
        for what, theme, dark, shown in looks():
            for check, ratio in shown.checks.items():
                with self.subTest(theme=what, dark=dark, check=check):
                    self.assertGreaterEqual(ratio, themes.floor(check, theme))

    def test_any_custom_colour(self):
        # Every check at its floor, every label (page and window, light and dark) at least 3:1, and
        # white unless the colour is a pale one; the accent the colour itself wherever it can be.
        values = (0x00, 0x33, 0x66, 0x99, 0xcc, 0xff)
        for rgb in itertools.product(values, repeat=3):
            colour = "#" + "".join(f"{v:02x}" for v in rgb)
            theme = themes.theme(themes.CUSTOM, colour)
            for dark in (False, True):
                shown = themes.build(theme, dark)
                for check, ratio in shown.checks.items():
                    if ratio < themes.floor(check, theme):
                        self.fail(f"Custom {colour}, dark {dark}: {check} {ratio:.2f}")
                self.assertGreaterEqual(shown.checks["page label on accent"], 3.0, colour)
                self.assertGreaterEqual(shown.checks["gtk label on accent"], 3.0, colour)
                self.assertGreaterEqual(shown.checks["gtk switch knob on accent"], 3.0, colour)
                self.assertEqual(shown.page["content-on-accent"], "#0a0a0a" if theme.pale else "#ffffff", colour)
                if not dark:
                    self.assertLessEqual(C(shown.page["systems-chat-foreground-wallpaper"]),
                                         themes.DOODLE_TOP + 0.004, colour)
            Lp, Cp, _ = themes.oklch(colour)
            if (themes.contrast("#ffffff", colour) >= 3.0 and Lp >= themes.NEAR_BLACK
                    and themes.MUTED_C <= Cp <= themes.C_TOP):
                self.assertEqual(theme.fills[False], colour)

    def test_the_floors(self):
        # Body text and links 4.5:1; the label on the accent, read ticks and the accent against the
        # chat list 3:1 (WhatsApp's own white on its green just makes it: 3.01:1).
        self.assertEqual(themes.LABEL_FLOOR, 3.0)
        self.assertGreaterEqual(themes.contrast("#ffffff", WA_GREEN_500), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("page link on systems-bubble-surface-outgoing"), 4.5)
        self.assertEqual(themes.floor("gtk accent text on sidebar_bg_color"), 4.5)
        self.assertEqual(themes.floor("page label on accent"), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("gtk label on accent"), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("gtk switch knob on accent"), themes.LABEL_FLOOR)
        self.assertEqual(themes.floor("page read ticks on outgoing"), 3.0)
        self.assertEqual(themes.floor("page accent on surface-default"), 3.0)
        for theme in themes.THEMES:
            names = set(themes.look(theme.id, False).checks)
            for surface in ("surface-default", "surface-emphasized", "systems-bubble-surface-incoming",
                            "systems-bubble-surface-outgoing"):
                self.assertIn(f"page link on {surface}", names)
            self.assertIn("gtk accent text on sidebar_bg_color", names)
            self.assertIn("gtk label on accent", names)
            self.assertIn("gtk switch knob on accent", names)

    def test_every_check_is_measured_on_whatsapps_own_surfaces(self):
        for what, _theme, dark, shown in looks():
            self.assertAlmostEqual(shown.checks["page text on surface-default"],
                                   themes.contrast(themes.PAGE_TEXT[dark], "#161717" if dark else "#ffffff"), msg=what)


class MadeTest(unittest.TestCase):
    """Den and Custom: one ramp at the colour's hue, WhatsApp green's steps on it."""

    def test_den_is_white_on_orange_in_both_styles(self):
        # Never dark text on Den's orange: in dark too it's white, on the light style's fill.
        light, dark = themes.look("den", False), themes.look("den", True)
        for shown in (light, dark):
            self.assertEqual(shown.page["content-on-accent"], "#ffffff")
            self.assertEqual(shown.gtk["accent_fg_color"], "#ffffff")
            self.assertEqual(shown.page["accent"], "#d86d25")
            self.assertEqual(shown.gtk["accent_bg_color"], "#d86d25")
            self.assertGreaterEqual(shown.checks["page label on accent"], 3.4)

    def test_den_keeps_its_identity(self):
        den = themes.BY_ID["den"]
        # The brand orange is its ramp's 450 step: within a just-visible difference.
        brand = themes._oklab(themes.hex_rgb("#ff7800"))
        step = themes._oklab(themes.hex_rgb(themes.at(den.ramp, 450)))
        self.assertLess(sum((a - b) ** 2 for a, b in zip(brand, step)) ** 0.5, 0.02)
        self.assertEqual(themes.look("den", True).page["content-read"], "#99cafe")  # cobalt200, the icon's blue
        self.assertEqual(themes.look("den", False).page["content-read"], "#007bfc")  # WhatsApp's own
        self.assertEqual(den.second, "#99c1f1")
        self.assertEqual(themes.swatch("den", False), ("#d86d25", "#99c1f1"))

    def test_made_ramps_have_greens_lightness(self):
        # A step means the same lightness as on WhatsApp's green, so every role sits as far from
        # WhatsApp's surfaces as its own accent does; never more of sRGB than green uses, and no
        # more chroma than WhatsApp's own Red's accent. Custom's chroma may be more than green's
        # between steps 350 and 700 (accent, links, strong); its tints never are.
        for theme in made():
            for step, ours, green in zip(themes.STEPS, theme.ramp, themes.GREEN):
                with self.subTest(theme=theme.source, step=step):
                    times = theme.chroma if 350 <= step <= 700 else min(theme.chroma, 1.0)
                    self.assertAlmostEqual(L(ours), L(green), delta=0.006)
                    self.assertLessEqual(C(ours), C(green) * times + 0.004)
                    self.assertLessEqual(C(ours), themes.C_TOP + 0.004)

    def test_roles_take_greens_steps(self):
        steps = {False: {"accent": 500, "accent-deemphasized": 100, "accent-emphasized": 700,
                         "systems-bubble-surface-outgoing": 150, "systems-chat-background-wallpaper": 50,
                         "systems-chat-foreground-wallpaper": 150},
                 True: {"accent": 450, "accent-deemphasized": 800, "accent-emphasized": 100,
                        "systems-bubble-surface-outgoing": 750}}
        for dark in (False, True):
            for theme_id, custom in (("den", None), ("custom", "#ff7800"), ("custom", "#00a0ff")):
                theme = themes.theme(theme_id, custom)
                page = themes.look(theme_id, dark, custom).page
                for name, step in steps[dark].items():
                    if name == "accent" and theme_id == "custom":
                        continue  # the colour picked: CustomTest
                    if theme_id == "den" and dark and name == "accent":
                        step = 500  # white on the light style's fill
                    self.assertEqual(page[name], themes.at(theme.ramp, step), (theme_id, custom, dark, name))

    def test_links_stay_on_their_ramp(self):
        # A link that misses 4.5:1 moves along its own ramp, not off it.
        for theme in made():
            fine = {themes.at(theme.ramp, s) for s in themes.FINE_STEPS}
            for dark in (False, True):
                shown = themes.build(theme, dark)
                link = shown.page["content-external-link"]
                self.assertIn(link, fine, (theme.source, dark))
                self.assertEqual(shown.page["content-action-emphasized"], link)
                self.assertIn(shown.gtk["accent_color"], fine, (theme.source, dark))

    def test_within_whatsapps_own_ranges(self):
        # Where WhatsApp's own tonal chat themes sit: fills (no darker than Dune mono's in light,
        # than Persian plum's in dark, unless white would stop reading there; no more colourful than
        # Red's), sent bubbles, wallpapers.
        for theme in made():
            light, dark = themes.build(theme, False).page, themes.build(theme, True).page
            with self.subTest(theme=theme.source):
                self.assertGreaterEqual(L(light["accent"]), themes.NEAR_BLACK - 0.003)
                self.assertLessEqual(C(light["accent"]), themes.C_TOP + 0.004)
                if theme.muted or theme.deep:
                    self.assertLessEqual(L(light["accent"]), 0.50)
                if theme.pale:
                    self.assertGreater(L(dark["accent"]), L(light["accent"]))
                else:
                    self.assertGreaterEqual(L(dark["accent"]), L(light["accent"]))
                    self.assertTrue(L(dark["accent"]) >= themes.DARK_FILL_MIN - 0.003
                                    or themes.contrast("#ffffff", themes.from_oklch(L(dark["accent"]) + 0.01,
                                                                                    C(dark["accent"]),
                                                                                    H(dark["accent"]))) < 3.0)
                self.assertTrue(0.89 <= L(light["systems-bubble-surface-outgoing"]) <= 0.97)
                self.assertTrue(0.28 <= L(dark["systems-bubble-surface-outgoing"]) <= 0.44)
                wallpaper = light["systems-chat-background-wallpaper"]
                self.assertLessEqual(C(wallpaper), 0.034)  # WhatsApp's most: its Lime theme's lime50, 0.033
                self.assertGreaterEqual(L(wallpaper), 0.94)
                doodle = light["systems-chat-foreground-wallpaper"]
                self.assertLessEqual(C(doodle), themes.DOODLE_TOP + 0.004)  # WhatsApp's most: Warm yellow's
                self.assertAlmostEqual(L(doodle), L(themes.GREEN[themes.STEPS.index(150)]), delta=0.006)

    def test_doodles_no_louder_than_whatsapps(self):
        # Green's step 150 at a yellow's or a green's hue is nearly twice as colourful as any
        # doodle WhatsApp's own themes have; there it keeps its lightness at Warm yellow's chroma.
        self.assertAlmostEqual(themes.DOODLE_TOP, C("#ffeac2"), places=4)
        for colour in ("#33d17a", "#26a269", "#f6d32d", "#ffff00", "#00ff00"):
            theme = themes.theme("custom", colour)
            doodle = themes.look("custom", False, colour).page["systems-chat-foreground-wallpaper"]
            with self.subTest(colour=colour):
                self.assertGreater(C(themes.at(theme.ramp, 150)), themes.DOODLE_TOP + 0.02)
                self.assertAlmostEqual(C(doodle), themes.DOODLE_TOP, delta=0.004)
                self.assertLess(hue_gap(doodle, themes.at(theme.ramp, 150)), 3)
        # Den's orange is well within it: step 150 as it is.
        den = themes.BY_ID["den"]
        self.assertEqual(themes.look("den", False).page["systems-chat-foreground-wallpaper"], themes.at(den.ramp, 150))

    def test_dark_wallpaper(self):
        # Den's and Custom's: WhatsApp's own look's; WhatsApp's chat themes: their own.
        for theme in made():
            page = themes.build(theme, True).page
            self.assertEqual(page["systems-chat-background-wallpaper"], "#161717")
            self.assertTrue(page["systems-chat-foreground-wallpaper"].endswith(", 0.1)"))  # as whiteAlpha10
        self.assertEqual(themes.look("wa-pink", True).page["systems-chat-background-wallpaper"], "#0a0a0a")
        self.assertEqual(themes.look("wa-sky-blue", True).page["systems-chat-background-wallpaper"], "#05151d")


class CustomTest(unittest.TestCase):
    def test_the_colour_setting(self):
        self.assertEqual(themes.custom_colour("#3A7BD5"), "#3a7bd5")
        self.assertEqual(themes.CUSTOM_DEFAULT, "#ff7800")  # Den's orange
        for value in (None, "", "red", "#ff00", "#gg0000", "rgb(1, 2, 3)", "#ff0000 ", " #ff0000", ["#ff0000"],
                      0xff0000, "ff0000", "#ff000000"):
            self.assertEqual(themes.custom_colour(value), "#ff7800", value)

    def test_the_colour_picked_is_the_accent(self):
        # Where white reads on it (3:1), it's the accent in light exactly, the window's too, and
        # its swatch's there; in dark it's the same hue and chroma, a step lighter, swatch and all.
        for colour in WHITE_READS:
            light, dark = themes.look("custom", False, colour), themes.look("custom", True, colour)
            with self.subTest(colour=colour):
                self.assertEqual(light.page["accent"], colour)
                self.assertEqual(light.gtk["accent_bg_color"], colour)
                self.assertEqual(themes.swatch("custom", False, colour)[0], colour)
                self.assertEqual(themes.swatch("custom", True, colour)[0], dark.page["accent"])
                self.assertGreater(L(dark.page["accent"]), L(colour))
                self.assertLess(hue_gap(dark.page["accent"], colour), 3)
                self.assertGreater(C(dark.page["accent"]), C(colour) * 0.8)
                self.assertEqual(light.page["content-on-accent"], "#ffffff")
                self.assertEqual(dark.page["content-on-accent"], "#ffffff")
        # Not a muted or dusty version of it: red stays red, purple purple (once #df635e, #5a486b).
        self.assertEqual(themes.look("custom", False, "#c01c28").page["accent"], "#c01c28")
        self.assertEqual(themes.look("custom", False, "#613583").page["accent"], "#613583")
        self.assertEqual(themes.look("custom", False).page, themes.look("custom", False, "#ff7800").page)
        self.assertEqual(themes.look("custom", False, "nonsense").page, themes.look("custom", False).page)

    def test_else_the_nearest_colour_white_reads_on(self):
        # The same hue and chroma, only as much darker as white needs: an orange stays an orange.
        for colour in DARKENED:
            theme = themes.theme("custom", colour)
            light, dark = theme.fills
            with self.subTest(colour=colour):
                self.assertFalse(theme.pale or theme.muted)
                self.assertLess(L(light), L(colour))
                self.assertLess(hue_gap(light, colour), 3)
                self.assertAlmostEqual(C(light), min(C(colour), themes.C_TOP, themes._cmax(L(light), H(light))),
                                       delta=0.012)
                self.assertGreaterEqual(themes.contrast("#ffffff", light), 3.0)
                self.assertLess(themes.contrast("#ffffff", light), 3.15)  # the nearest: not darker than needed
                self.assertGreaterEqual(L(dark), L(light))
                self.assertGreaterEqual(themes.contrast("#ffffff", dark), 3.0)

    def test_the_default_orange_is_white_in_dark_too(self):
        # Den's orange as a Custom colour: white on it in both styles, like Den, never dark text.
        for dark in (False, True):
            shown = themes.look("custom", dark)
            self.assertEqual(shown.page["content-on-accent"], "#ffffff")
            self.assertEqual(shown.gtk["accent_fg_color"], "#ffffff")
            self.assertGreaterEqual(shown.checks["page label on accent"], 3.0)
            self.assertLess(hue_gap(shown.page["accent"], "#ff7800"), 2)
        self.assertEqual(themes.theme("custom").fills, ("#ef7000", "#ef7000"))

    def test_pale_colours_keep_their_colour_with_the_dark_label(self):
        # Creams, yellows, pale limes: white can't reach 3:1 on them without them turning mustard,
        # tan or olive, so they stay and take WhatsApp's dark label, light and dark. No paler on the
        # chat list than WhatsApp's own Lemon; where they're that already, the colour itself.
        for colour in PALE:
            theme = themes.theme("custom", colour)
            light, dark = themes.look("custom", False, colour), themes.look("custom", True, colour)
            with self.subTest(colour=colour):
                self.assertTrue(theme.pale)
                for shown in (light, dark):
                    self.assertEqual(shown.page["content-on-accent"], "#0a0a0a")
                    self.assertEqual(shown.gtk["accent_fg_color"], "rgba(0, 0, 6, 0.8)")
                    self.assertGreaterEqual(shown.checks["page label on accent"], 7)
                    self.assertLess(hue_gap(shown.page["accent"], colour), 3)
                fill = light.page["accent"]
                self.assertGreaterEqual(themes.contrast(fill, "#ffffff"), themes.PALE_FLOOR)
                if themes.contrast(colour, "#ffffff") >= themes.PALE_FLOOR:
                    self.assertEqual(fill, colour)
                self.assertGreater(L(dark.page["accent"]), L(fill))
        self.assertEqual(themes.theme("custom", "#e5cb00").fills[0], "#e5cb00")  # Lemon's own
        self.assertEqual(themes.theme("custom", "#c1a886").fills[0], "#c1a886")  # Cream's own
        self.assertAlmostEqual(themes.PALE_FLOOR, themes.contrast("#e5cb00", "#ffffff"))
        # GNOME's yellow: the dark label, a shade deeper to show on the chat list as Lemon does.
        yellow = themes.look("custom", False, "#f6d32d")
        self.assertEqual(yellow.page["content-on-accent"], "#0a0a0a")
        self.assertLess(L(yellow.page["accent"]), L("#f6d32d"))
        self.assertLess(L("#f6d32d") - L(yellow.page["accent"]), 0.05)
        # Oranges aren't pale ones: white on them, even on a light orange.
        for colour in ("#ff7800", "#ffa348", "#ffa500", "#ffbe6f"):
            self.assertFalse(themes.theme("custom", colour).pale, colour)

    def test_more_colourful_than_whatsapps_red_is_brought_in(self):
        for colour in ("#0000ff", "#ff00ff", "#00ff00", "#ff0000"):
            light = themes.theme("custom", colour).fills[0]
            self.assertLessEqual(C(light), themes.C_TOP + 0.004, colour)
            self.assertLess(hue_gap(light, colour), 3, colour)

    def test_greys_take_whatsapps_low_chroma_pattern(self):
        # Dark in light, as Dune mono and Charcoal green; white on it in dark too. Greys stay grey.
        theme = themes.theme("custom", "#808080")
        self.assertTrue(theme.muted)
        light, dark = themes.look("custom", False, "#808080"), themes.look("custom", True, "#808080")
        self.assertAlmostEqual(L(light.page["accent"]), L(themes.GREEN[themes.STEPS.index(700)]), delta=0.006)
        for shown in (light, dark):
            for name, value in shown.page.items():
                if value.startswith("#") and name != "content-read":
                    self.assertLess(C(value), 0.01, (shown.dark, name, value))
            self.assertEqual(shown.page["content-on-accent"], "#ffffff")
        self.assertGreaterEqual(L(dark.page["accent"]), themes.DARK_FILL_MIN - 0.003)
        for name in ("window_bg_color", "sidebar_bg_color"):
            self.assertLess(C(dark.gtk[name]), 0.001)  # no hue to turn towards: neutral greys
        # A grey already that dark is the accent itself; black, WhatsApp's darkest accent (Dune mono's).
        self.assertEqual(themes.theme("custom", "#333333").fills[0], "#333333")
        self.assertEqual(themes.theme("custom", "#000000").fills[0], "#333333")
        self.assertEqual(themes.theme("custom", "#3c503a").fills[0], "#3c503a")  # Charcoal green's own
        self.assertTrue(themes.theme("custom", "#3c503a").muted)
        self.assertFalse(themes.theme("custom", "#c1a886").muted)  # Cream: a colour, not a grey

    def test_dark_colours_take_whatsapps_deep_pattern(self):
        # A navy like Dark cerulean's: itself in light; in dark lifted to where WhatsApp's own deep
        # themes sit (Persian plum's 0.60 at least), with white on it.
        theme = themes.theme("custom", "#0b4b79")
        self.assertTrue(theme.deep and not theme.muted)
        light, dark = themes.look("custom", False, "#0b4b79"), themes.look("custom", True, "#0b4b79")
        self.assertEqual(light.page["accent"], "#0b4b79")
        self.assertGreaterEqual(L(dark.page["accent"]), themes.DARK_FILL_MIN - 0.003)
        self.assertGreaterEqual(dark.checks["page accent on surface-default"], 3.0)
        self.assertEqual(dark.page["content-on-accent"], "#ffffff")
        self.assertEqual(dark.gtk["accent_fg_color"], "#ffffff")
        self.assertEqual(themes.theme("custom", "#1a0033").fills[0], themes.from_oklch(
            themes.NEAR_BLACK, C("#1a0033"), H("#1a0033")))  # near-black: lifted to the darkest WhatsApp has

    def test_cached_per_colour(self):
        self.assertIs(themes.look("custom", False, "#FF0000"), themes.look("custom", False, "#ff0000"))
        self.assertIsNot(themes.look("custom", False, "#ff0000"), themes.look("custom", False, "#00ff00"))
        self.assertIs(themes.look("den", False, "#ff0000"), themes.look("den", False))  # only Custom takes it
        self.assertNotEqual(themes.page_css("custom", "#ff0000"), themes.page_css("custom", "#00ff00"))
        self.assertEqual(themes.page_css("den", "#ff0000"), themes.page_css("den"))
        self.assertEqual(themes.page_css("custom", "#FF0000"), themes.page_css("custom", "#ff0000"))
        self.assertNotEqual(themes.gtk_css("custom", False, False, True, "#ff0000"),
                            themes.gtk_css("custom", False, False, True, "#00ff00"))

    def test_its_swatch_shows_the_theme_made_from_it(self):
        # As every swatch shows its theme: the accent and the bubble the page has in that style. In
        # light that's the colour as picked, where it's used as it is.
        for dark in (False, True):
            red = themes.swatch("custom", dark, "#e01b24")
            page = themes.look("custom", dark, "#e01b24").page
            self.assertEqual(red, (page["accent"], page["systems-bubble-surface-outgoing"]))
            self.assertIn(f"linear-gradient(135deg, {red[0]} 49%, {red[1]} 51%)",
                          themes.swatch_css(dark, False, "#e01b24"))
        self.assertEqual(themes.swatch("custom", False, "#e01b24")[0], "#e01b24")
        self.assertNotEqual(themes.swatch_css(False, False, "#e01b24"), themes.swatch_css(False, False))
        # A colour that's moved shows as it's used: white makes a grey theme, black Dune mono's.
        for colour, light in (("#ff7800", "#ef7000"), ("#ffffff", "#515151"), ("#000000", "#333333")):
            self.assertEqual(themes.swatch("custom", False, colour)[0], light)
            self.assertEqual(themes.swatch("custom", True, colour)[0],
                             themes.look("custom", True, colour).page["accent"])


class SwatchTest(unittest.TestCase):
    def test_every_choice_has_one(self):
        for dark in (False, True):
            for hc in (False, True):
                css = themes.swatch_css(dark, hc)
                for theme_id, _name, _describe in themes.CHOICES:
                    self.assertIn(f"checkbutton.natter-swatch.theme-{theme_id} ", css)
            for theme_id, _name, _describe in themes.CHOICES:
                fill, second = themes.swatch(theme_id, dark)
                self.assertTrue(HEX.match(fill) and HEX.match(second), (theme_id, fill, second))
        self.assertEqual(themes.swatch("plain", True), ("#deddda", "#77767b"))
        # WhatsApp's: its own accent (not the window's, darkened for its label) and its sent bubble.
        self.assertEqual(themes.swatch("wa-lemon", False), ("#e5cb00", "#fdf8af"))
        self.assertEqual(themes.swatch("wa-pink", True), ("#ff518b", "#571e35"))

    def test_whatsapps_are_its_own_colours(self):
        # The accent and the sent bubble as the page has them, or the chat background (_wa_swatches).
        for theme in themes.WA_THEMES:
            for dark in (False, True):
                fill, second = themes.swatch(theme.id, dark)
                page = themes.look(theme.id, dark).page
                self.assertEqual(fill, page["accent"], (theme.id, dark))
                self.assertIn(second, (page["systems-bubble-surface-outgoing"],
                                       page["systems-chat-background-wallpaper"]), (theme.id, dark))

    def test_no_two_look_the_same(self):
        # Default blue has Royal blue's accent and bubble in dark, Default mono Dune mono's in light:
        # there the later one shows its chat background, which tells them apart.
        for dark in (False, True):
            shown = {theme_id: themes.swatch(theme_id, dark) for theme_id, _name, _describe in themes.CHOICES}
            for (a, one), (b, other) in itertools.combinations(shown.items(), 2):
                apart = max(themes._distance(x, y) for x, y in zip(one, other))
                self.assertGreaterEqual(apart, themes.SAME_TO_THE_EYE, (dark, a, b, one, other))
        self.assertEqual(themes.swatch("wa-default-blue", True), ("#007bfc", "#051321"))
        self.assertEqual(themes.swatch("wa-royal-blue", True), ("#007bfc", "#072f5a"))
        self.assertEqual(themes.swatch("wa-default-mono", False), ("#333536", "#fafafa"))
        self.assertEqual(themes.swatch("wa-dune-mono", False), ("#333333", "#dadcdc"))
        # Where the pair doesn't collide, each keeps its bubble.
        self.assertEqual(themes.swatch("wa-default-blue", False), ("#2a91fd", "#d2e8fe"))
        self.assertEqual(themes.swatch("wa-default-mono", True), ("#ffffff", "#333333"))

    def test_high_contrast_rings_are_stronger(self):
        plain, strong = themes.swatch_css(False, False), themes.swatch_css(False, True)
        self.assertIn("0.35)", plain)
        self.assertIn("0.8)", strong)
        self.assertNotIn("0.35)", strong)

    def test_the_tick_badge_takes_the_rows_colour(self):
        self.assertIn("@card_bg_color", themes.swatch_css(False, False))
        self.assertIn("mix(@dialog_bg_color, white, 0.08)", themes.swatch_css(True, False))
        self.assertNotIn("@window_bg_color", themes.swatch_css(True, False))

    def test_rings_are_the_window_accent_text(self):
        css = themes.swatch_css(False, False)
        ring = themes.look("wa-lemon", False).gtk["accent_color"]
        self.assertIn(f"checkbutton.natter-swatch.theme-wa-lemon:checked {{ outline: 2px solid {ring}; }}", css)


class NamesTest(unittest.TestCase):
    def test_exactly_the_allowed_names(self):
        for what, css in sheets():
            found, _ = blocks(css)
            names = PAGE_NAMES | ({f"--WDS-{META}"} if what in WA_META else set())
            with self.subTest(theme=what):
                self.assertEqual(set(found["body, #main"]), names)
                self.assertEqual(set(found["body.dark, body.dark #main"]), names)
                self.assertEqual(set(found["html"]), SPLASH_NAMES)

    def test_surfaces_stay_whatsapps(self):
        # WhatsApp's own themes never recolour a surface; neither does Natter.
        for what, css in sheets():
            for name in NEVER:
                self.assertNotIn(f"--WDS-{name}:", css, (what, name))

    def test_a_corrupt_setting_is_den(self):
        # settings.json edited by hand: a list or a mapping can't be cached, and isn't a theme.
        self.assertEqual(themes.page_css(["den"]), themes.page_css("den"))
        self.assertEqual(themes.page_css({"theme": "wa-pink"}), themes.page_css("den"))
        self.assertEqual(themes.page_css("custom", ["#ff0000"]), themes.page_css("custom", "#ff7800"))
        self.assertEqual(themes.page_css("custom", {"r": 1}), themes.page_css("custom"))


class PageSheetTest(unittest.TestCase):
    def test_only_whatsapps_colour_names(self):
        for what, css in sheets():
            found, other = blocks(css)
            with self.subTest(theme=what):
                self.assertEqual(other, [])
                self.assertEqual(set(found), SELECTORS)
                self.assertEqual(css.count("@media"), 1)
                for banned in ("url(", "@import", "@font-face", "image-set", "\\", "--x", " .x", "{}", "var(",
                               "chatTheme", "appTheme", "localStorage"):
                    self.assertNotIn(banned, css)

    def test_values_are_colour_literals_only(self):
        for what, css in sheets():
            found, _ = blocks(css)
            for selector, values in found.items():
                for name, value in values.items():
                    with self.subTest(theme=what, selector=selector, name=name):
                        self.assertTrue(HEX.match(value) or LEGACY_RGBA.match(value) or TRIPLET.match(value), value)

    def test_dark_covers_everything_light_sets(self):
        # body.dark matches "body" too: a name only the light block sets would stay light in the dark.
        for what, css in sheets():
            found, _ = blocks(css)
            self.assertLessEqual(set(found["body, #main"]), set(found["body.dark, body.dark #main"]), what)

    def test_every_colour_has_its_rgb_twin(self):
        for what, css in sheets():
            found, _ = blocks(css)
            for selector in ("body, #main", "body.dark, body.dark #main"):
                values = found[selector]
                for name, value in values.items():
                    if name.endswith("-RGB"):
                        continue
                    with self.subTest(theme=what, name=name):
                        self.assertTrue(HEX.match(value) or LEGACY_RGBA.match(value), value)
                        if name == f"--WDS-{META}":
                            self.assertNotIn(name + "-RGB", values)  # WhatsApp's own twin stays
                        else:
                            self.assertEqual(values[name + "-RGB"], themes.rgb_triplet(value))

    def test_plain_is_no_sheet(self):
        self.assertEqual(themes.page_css("plain"), "")
        self.assertEqual(themes.page_css("plain", "#ff0000"), "")
        self.assertEqual(themes.gtk_css("plain", True, False, True), "")

    def test_the_comment_names_the_theme(self):
        self.assertTrue(themes.page_css("wa-sunset-orange").startswith(f"/* {NAME} theme “Sunset orange”"))
        self.assertTrue(themes.page_css("custom").startswith(f"/* {NAME} theme “Custom”"))

    def test_splash_is_whatsapps_own_with_the_accent(self):
        for theme_id, custom in [(t.id, None) for t in themes.THEMES] + [("custom", c) for c in CUSTOMS]:
            css = themes.page_css(theme_id, custom)
            light, dark = css.split("@media (prefers-color-scheme: dark)")
            accent = {d: themes.look(theme_id, d, custom).page["accent"] for d in (False, True)}
            self.assertIn("--splashscreen-startup-background: #f7f5f3 !important;", light)
            self.assertIn("--splashscreen-startup-background-plain: #ffffff !important;", light)
            self.assertIn(f"--splashscreen-progress-primary: {accent[False]} !important;", light)
            self.assertIn("--splashscreen-startup-background: #161717 !important;", dark)
            self.assertIn(f"--splashscreen-progress-primary: {accent[True]} !important;", dark)


KNOB = re.compile(r"^switch:checked > slider \{ background-color: (#[0-9a-f]{6}); \}$")


class WindowTest(unittest.TestCase):
    def test_gtk_414_gets_named_colours_only(self):
        # Named colours, and on a fill with the dark label one rule more, the switch's knob: literals only.
        for theme_id, custom in [(t.id, None) for t in themes.THEMES] + [("custom", c) for c in CUSTOMS]:
            for dark in (False, True):
                css = themes.gtk_css(theme_id, dark, False, False, custom)
                for newer in (":root", "var(", "oklab", "oklch", "color-mix", "/"):
                    self.assertNotIn(newer, css)
                for line in css.splitlines():
                    if KNOB.match(line):
                        continue
                    name, value = re.match(r"^@define-color ([a-z_]+) (.+);$", line).groups()
                    self.assertTrue(HEX.match(value) or LEGACY_RGBA.match(value), (theme_id, name, value))

    def test_switch_knobs_read_on_the_fill(self):
        # libadwaita paints a switch's knob white while it's on, whatever accent_fg_color says. On a
        # fill with the dark label (WhatsApp's in dark, its three pale ones, a pale Custom colour)
        # the knob is that label, in high contrast too; on the others it stays libadwaita's white.
        dark_knobs = set()
        for what, theme, dark, shown in looks():
            with self.subTest(theme=what, dark=dark):
                white = shown.gtk["accent_fg_color"] == "#ffffff"
                fill = shown.gtk["accent_bg_color"]
                self.assertEqual(shown.knob, "#ffffff" if white else themes.over("#000006", 0.8, fill))
                self.assertGreaterEqual(shown.checks["gtk switch knob on accent"], 3.0)
                self.assertAlmostEqual(shown.checks["gtk switch knob on accent"], themes.contrast(shown.knob, fill))
                for high_contrast in (False, True):
                    for variables in (False, True):
                        css = themes.gtk_css(theme.id, dark, high_contrast, variables,
                                             what.split()[1] if theme.id == "custom" else None)
                        knobs = [line for line in css.splitlines() if "slider" in line]
                        self.assertEqual(knobs, [] if white else
                                         [f"switch:checked > slider {{ background-color: {shown.knob}; }}"])
                if not white:
                    dark_knobs.add((what, dark))
        # White on these fills is under 3:1 (Default mono's white in dark, 1:1; Lemon's 1.6:1 in light).
        for theme_id, dark in (("wa-default-mono", True), ("wa-lemon", False), ("wa-lemon", True),
                               ("wa-whatsapp-green", True), ("custom #f6d32d", False), ("custom #f6d32d", True)):
            self.assertIn((theme_id, dark), dark_knobs)
        self.assertNotIn(("den", True), dark_knobs)
        self.assertEqual(themes.look("wa-default-mono", True).knob, "#333338")

    def test_variables_repeat_the_named_colours(self):
        css = themes.gtk_css("wa-merino-teal", True, False, variables=True)
        named = dict(re.findall(r"@define-color ([a-z_]+) ([^;]+);", css))
        variables = dict(re.findall(r"  --([a-z-]+): ([^;]+);", css))
        self.assertEqual({k.replace("_", "-"): v for k, v in named.items()}, variables)
        self.assertIn("accent-color", variables)  # never left to libadwaita to work out

    def test_high_contrast_sets_only_the_accent(self):
        css = themes.gtk_css("den", False, True, variables=True)
        self.assertEqual(set(re.findall(r"@define-color ([a-z_]+)", css)), set(themes.ACCENT_NAMES))

    def test_the_accent_comes_from_the_theme(self):
        # The window's fill and label are the page's, so the two match; its accent text is the
        # theme's link, moved along until it reads on the window.
        for what, theme, dark, shown in looks():
            self.assertEqual(shown.gtk["accent_bg_color"], shown.page["accent"], (what, dark))
            label = shown.page["content-on-accent"]
            self.assertEqual(shown.gtk["accent_fg_color"], "#ffffff" if label == "#ffffff" else "rgba(0, 0, 6, 0.8)")
            self.assertGreaterEqual(shown.checks["gtk label on accent"], 3.0, (what, dark))

    def test_window_surfaces_barely_tinted(self):
        # libadwaita's own lightness; chroma at most WhatsApp's warm grey's (0.003); white stays white.
        for what, _theme, dark, shown in looks():
            for name, base in themes.GTK_SURFACES[dark].items():
                if name == "card_bg_color" and dark:
                    continue
                with self.subTest(theme=what, dark=dark, name=name):
                    self.assertAlmostEqual(L(shown.gtk[name]), L(base), delta=0.004)
                    self.assertLessEqual(C(shown.gtk[name]), themes.GTK_TINT + 1e-9)
                    if base == "#ffffff":
                        self.assertEqual(shown.gtk[name], "#ffffff")

    def test_window_surfaces_stay_neutral_for_any_custom_colour(self):
        # As a hex colour, not only as asked: in the dark greys one 8-bit step is a lot of chroma
        # (a green pick once made the view #1c1e1c, 0.005).
        values = (0x00, 0x33, 0x66, 0x99, 0xcc, 0xff)
        for rgb in itertools.product(values, repeat=3):
            colour = "#" + "".join(f"{v:02x}" for v in rgb)
            for dark in (False, True):
                gtk = themes.look("custom", dark, colour).gtk
                for name, base in themes.GTK_SURFACES[dark].items():
                    if base.startswith("#"):
                        self.assertLessEqual(C(gtk[name]), themes.GTK_TINT + 1e-9, (colour, dark, name, gtk[name]))

    def test_backgrounds(self):
        self.assertEqual(themes.page_background("plain", False), "#fafafb")
        self.assertEqual(themes.page_background("plain", True), "#222226")
        for theme_id in ("den", "custom", "wa-lemon"):
            self.assertEqual(themes.page_background(theme_id, False), "#f7f5f3")  # WhatsApp's own start-up screen
            self.assertEqual(themes.page_background(theme_id, True), "#161717")
        # The start-up screen's colour of every theme, Custom in any colour, without making one.
        for what, theme, dark, shown in looks():
            self.assertEqual(shown.page["splash:startup-background"], themes.page_background(theme.id, dark), what)
        with mock.patch.object(themes, "look", side_effect=AssertionError("a theme was made")):
            for theme_id, _name, _describe in themes.CHOICES:
                themes.page_background(theme_id, True)
        self.assertEqual(themes.window_background("den", False), themes.look("den", False).gtk["window_bg_color"])
        self.assertEqual(themes.window_background("custom", True, "#3584e4"),
                         themes.look("custom", True, "#3584e4").gtk["window_bg_color"])
        self.assertEqual(themes.window_background("lagoon", False), themes.window_background("wa-merino-teal", False))

    def test_module_is_text_only(self):
        tree = ast.parse(Path(themes.__file__).read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or "." for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "math", "dataclasses", "functools", "typing", "."})


if __name__ == "__main__":
    unittest.main()
