"""Natter Den's themes: its own Den, WhatsApp's own 22 chat themes, one made from a colour you pick,
or Plain.

WhatsApp Web paints through its own colour names (--WDS-accent, --WDS-systems-bubble-surface-outgoing
and so on), and each name points at one step of one colour ramp in each style. WhatsApp's own chat
themes (Pink, Tyrian purple, Lime ...) are its green's steps moved onto another ramp. They change the
accent, links, read ticks, the sent bubble and the wallpaper (three of them the bubbles' small grey
text too), and never a surface or the label on the accent. Natter's themes are made the same way:

* WhatsApp's chat themes are WhatsApp's own: its names, its picker's order and its colours exactly,
  from their tonal form (a soft sent bubble), light and dark. Not their vibrant form: that one relies
  on WhatsApp turning the sent bubble's text white itself, which colour names alone can't do safely.
* Den (Natter's own, from its icon's orange) is made the way WhatsApp makes its own: a ramp at the
  colour's hue with WhatsApp green's lightness and chroma at every step, and every colour name on
  green's step: accent 500 light / 450 dark, soft 100 / 800, strong 700 / 100, links 600 / 450, sent
  bubble 150 / 750 (green100's chroma exists only in green). Its doodles (150 in light) are no more
  colourful than WhatsApp's own most colourful ones, Custom's too.
* Custom (from a colour you pick) looks like the colour picked: in light its accent is that colour
  exactly, wherever white reads on it (3:1) and it's neither near-black nor too pale, or else the
  nearest one of the same hue and chroma that is. In dark it's a step lighter, as WhatsApp's own Red,
  Pink and Royal blue are, but no lighter than white still reads on. Its other names take green's
  steps on a ramp at its hue that's as colourful as the colour picked, up to WhatsApp's own Red. A
  greyish colour takes WhatsApp's low-chroma pattern (Dune mono, Charcoal green) and a near-black one
  its deep pattern (Persian plum, Dark cerulean): the accent dark in light, so it still reads as the
  accent, and well lifted in dark.
* The label on the accent: white on Den and Custom, light and dark (no dark text on a coloured
  button). The one exception is a pale yellow, cream or lime picked for Custom: darkened until white
  reads on it, it would turn mustard, tan or olive, so it keeps its colour and takes WhatsApp's own
  dark label, #0A0A0A. WhatsApp's themes keep WhatsApp's rule, white in light and #0A0A0A in dark,
  except on its three pale fills (Lemon, Warm yellow, Cream), where white has under 3:1. WhatsApp
  keeps a chat theme to the open chat; Natter paints it across the page, so those fills also label
  the chat list's unread badges, and there the label is #0A0A0A in light too.
* Surfaces stay WhatsApp's: the page's sheet doesn't set them at all. Natter's window keeps
  libadwaita's greys, at most turned towards the accent's hue at chroma 0.003, WhatsApp's own
  warm-grey level. Its accent is the page's fill with the page's label, and where that label is
  the dark one, so is the knob of a switch that's on (libadwaita paints it white whatever the label).
* Then Natter's floors, on everything Natter makes (Den, Custom, its own window): body text and links
  4.5:1 on every surface they sit on (links move along the same ramp until they get there), the label
  on the accent and a switch's knob 3:1 (WhatsApp's own white on its green has 3.01:1), read ticks
  and the accent on the chat list 3:1 (a pale Custom fill: no less than WhatsApp's own palest,
  Lemon's). WhatsApp's own themes keep WhatsApp's colours on the page, where some go lower (Lemon's
  accent is 1.6:1 on the chat list); in Natter's window their accent text moves until it reaches
  the floors.

Each theme recolours Natter's own window (a GTK style provider: @define-color lines, plus the
same values as :root variables on GTK 4.16+, and on a fill with the dark label the switches' knob)
and WhatsApp's page (a user style sheet that only redefines WhatsApp's own --WDS-* colour names).
The CSS that comes out is literals only, so it means the same on libadwaita 1.5 (GTK 4.14) and on
1.6+.

No GTK, WebKit or file access here: app.py loads the GTK text, account.py hands the page text
to WebKit. "plain" is no theme at all: no page style sheet and plain libadwaita. Custom's colour is
the "theme_custom" setting, passed in as `custom` wherever it matters.

Ban safety (see account.py): the page text is custom properties only, every one !important, no
url(), no @import, no fonts, no selectors but body, #main, body.dark and html. It's never put in
the page as an element; WebKit applies it at the user level. WhatsApp's chat themes are copied here
as values only, painted with CSS the way they look. Nothing turns on, reads or writes WhatsApp's own
chat themes, wallpapers, app themes or flags.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from . import NAME

# ---- Colour maths (sRGB <-> OKLab/OKLCH, WCAG contrast) ------------------------------------------


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def rgb_hex(rgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def rgb_triplet(value: str) -> str:
    """'#ff7800' -> '255, 120, 0', and 'rgba(255, 120, 0, 0.1)' -> the same: WhatsApp's -RGB twins."""
    if value.startswith("rgba("):
        return ", ".join(value[5:-1].split(", ")[:3])
    return ", ".join(str(round(c * 255)) for c in hex_rgb(value))


def rgba(value: str, alpha: float) -> str:
    return f"rgba({rgb_triplet(value)}, {alpha:g})"


def _oklab(rgb):
    r, g, b = (_lin(c) for c in rgb)
    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(x) ** (1 / 3), x) for x in (l_, m_, s_))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _srgb(lab):
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)
    return tuple(_gam(c) if c >= 0 else -_gam(-c) for c in rgb)


def oklch(value: str) -> tuple[float, float, float]:
    L, a, b = _oklab(hex_rgb(value))
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def _fits(L: float, C: float, H: float):
    rgb = _srgb((L, C * math.cos(math.radians(H)), C * math.sin(math.radians(H))))
    return all(-1e-4 <= c <= 1 + 1e-4 for c in rgb), rgb


@lru_cache(maxsize=4096)  # bounded: every custom colour brings its own hue
def _cmax(L: float, H: float) -> float:
    """The most chroma sRGB holds at this lightness and hue."""
    lo, hi = 0.0, 0.5
    for _ in range(30):
        mid = (lo + hi) / 2
        if _fits(L, mid, H)[0]:
            lo = mid
        else:
            hi = mid
    return lo


def from_oklch(L: float, C: float, H: float) -> str:
    """OKLCH to hex, with chroma lowered until it fits sRGB (lightness and hue kept)."""
    ok, rgb = _fits(L, C, H)
    return rgb_hex(rgb if ok else _fits(L, _cmax(L, H), H)[1])


def _mix(a: str, b: str, t: float) -> str:
    """The colour `t` of the way from `a` to `b`, in OKLab."""
    la, lb = _oklab(hex_rgb(a)), _oklab(hex_rgb(b))
    return rgb_hex(_srgb(tuple(x + (y - x) * t for x, y in zip(la, lb))))


def luminance(value: str) -> float:
    r, g, b = (_lin(c) for c in hex_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def over(fg: str, alpha: float, bg: str) -> str:
    """`fg` at `alpha` laid on `bg`, as one opaque colour."""
    return rgb_hex(tuple(f * alpha + b * (1 - alpha) for f, b in zip(hex_rgb(fg), hex_rgb(bg))))


# ---- WhatsApp's own colours (WhatsApp Web client revision 1050968285, 20 Oct 2026) -----------------
# Its green ramp (WDSThemes): 21 steps, the same in light and dark. Den's and Custom's ramps are made
# from its lightness and chroma.

STEPS = (50, 75, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 900, 950, 1000)
GREEN = ("#F2FDF0", "#E7FCE3", "#D9FDD3", "#C3FDC0", "#ACFCAC", "#8FF499", "#71EB85", "#4BDF76", "#25D366",
         "#21C063", "#1DAA61", "#1C995B", "#1B8755", "#18744A", "#15603E", "#144D37", "#103529", "#0E2C23",
         "#0C241C", "#0A1B16", "#08120F")

# The step each of Natter's colour roles takes, from WhatsApp's green (its WDSThemes base theme):
# accent 500/450, accent-deemphasized 100/800, accent-emphasized 700/100, links and text actions
# 600/450, sent bubble 100/750. Wallpaper 50 and doodles 150 in light are WhatsApp's chat-theme
# pattern (its base look uses cream there); in dark the wallpaper stays WhatsApp's own.
GREEN_STEPS = {
    False: {"accent": 500, "soft": 100, "strong": 700, "link": 600, "outgoing": 100, "wallpaper": 50, "doodle": 150},
    True: {"accent": 450, "soft": 800, "strong": 100, "link": 450, "outgoing": 750},
}
MADE_STEPS = {False: {"outgoing": 150}, True: {}}  # green100's chroma exists only in green
# WhatsApp's low-chroma chat themes (Dune mono, Charcoal green) and its deep ones (Persian plum,
# Dark cerulean) put the accent dark in light, so it still reads as the accent, and their links and
# strong colour with it; dark keeps green's steps. (Custom's accent itself: _custom.)
DARK_ACCENT_STEPS = {False: {"link": 700, "strong": 800}, True: {}}
# A made ramp's tints (soft, sent bubble, wallpaper, doodles: the steps paler than 350 and darker
# than 700) are never more colourful than green's own, as in WhatsApp's tonal themes; the steps
# between (accent, links, strong) take a Custom colour's chroma, up to C_TOP.
VIVID_STEPS = range(350, 701)

# WhatsApp's 22 chat themes, in its picker's order (WAWebChatThemeDefinitions), with the names its
# picker shows (getColorSchemeName), in their tonal form: per style the values of the names Natter
# sets, in WA_ROLES order. Copied from the shipped StyleX classes (WAWebChatTheme*.stylex), with
# rgb() written as hex; their action text is always their link colour.
WA_ROLES = ("accent", "soft", "strong", "link", "read", "outgoing", "wallpaper", "doodle")
WA_CHAT_THEMES = (
    # id, name: light (accent, soft, strong, link, read ticks, sent bubble, wallpaper, doodles), then dark
    ("whatsapp-green", "WhatsApp green",
     ("#1daa61", "#d9fdd3", "#15603e", "#1b8755", "#007bfc", "#d9fdd3", "#f5f1eb", "#eae0d3"),
     ("#21c063", "#103529", "#d9fdd3", "#21c063", "#53bdeb", "#144d37", "#0a0a0a", "rgba(255, 255, 255, 0.1)")),
    ("pearl-indigo", "Pearl indigo",
     ("#5e47de", "#e8e0ff", "#3a327b", "#5e47de", "#007bfc", "#e8e0ff", "#ffebf1", "#e4dbff"),
     ("#a791ff", "#242447", "#e8e0ff", "#a791ff", "#53a6fd", "#2c2a5b", "#0a0a0a", "#1d1d3a")),
    ("tyrian-purple", "Tyrian purple",
     ("#c15add", "#f1d9f7", "#713082", "#c15add", "#007bfc", "#f1d9f7", "#ffebe6", "#f1d9f7"),
     ("#d184e6", "#2b1830", "#f1d9f7", "#d184e6", "#53a6fd", "#4b215a", "#0a0a0a", "#27162c")),
    ("sunset-orange", "Sunset orange",
     ("#e56d53", "#fee2d8", "#6b3424", "#e56d53", "#007bfc", "#fee2d8", "#fcf4e5", "#fae4de"),
     ("#e56d53", "#35221e", "#fee2d8", "#e56d53", "#53a6fd", "#4c2c24", "#0a0a0a", "#2a1b18")),
    ("merino-teal", "Merino teal",
     ("#028377", "#cbf2ee", "#074d4a", "#028377", "#007bfc", "#cbf2ee", "#f5f1eb", "#d4e7e6"),
     ("#02a698", "#092d2f", "#cbf2ee", "#02a698", "#53a6fd", "#0a3e3e", "#0a0a0a", "#072527")),
    ("royal-blue", "Royal blue",
     ("#2d68eb", "#d3d9ee", "#073d76", "#2d68eb", "#2d68eb", "#d2e8fe", "#e6effa", "#c4d9fd"),
     ("#007bfc", "#092642", "#d2e8fe", "#007bfc", "#53a6fd", "#072f5a", "#0a0a0a", "#071c31")),
    ("dark-cerulean", "Dark cerulean",
     ("#0b4b79", "#d3e3ee", "#0b4b79", "#0b4b79", "#007bfc", "#d3e3ee", "#edf3f7", "#d3e3ee"),
     ("#1591ea", "#072b46", "#d3e3ee", "#1591ea", "#53a6fd", "#08344e", "#0a0a0a", "#05202e")),
    ("charcoal-green", "Charcoal green",
     ("#3c503a", "#dfe8df", "#313f30", "#3c503a", "#007bfc", "#dfe8df", "#f4f5f0", "#dfe8df"),
     ("#84a082", "#242e23", "#dfe8df", "#84a082", "#53a6fd", "#293328", "#0b0a0a", "#1d251c")),
    ("persian-plum", "Persian plum",
     ("#731a2f", "#fdced9", "#731a2f", "#731a2f", "#007bfc", "#fdced9", "#f1ede7", "#e7dfd4"),
     ("#cf4a69", "#531322", "#fdced9", "#cf4a69", "#53a6fd", "#531323", "#0a0a0a", "#2a131c")),
    ("dune-mono", "Dune mono",
     ("#333333", "#eaebeb", "#333333", "#333333", "#007bfc", "#dadcdc", "#eaebeb", "#dadcdc"),
     ("#bdbdbd", "#333333", "#eaebeb", "#bdbdbd", "#53a6fd", "#333333", "#0a0a0a", "rgba(255, 255, 255, 0.1)")),
    ("default-blue", "Default blue",
     ("#2a91fd", "#d3d9ee", "#073d76", "#2a91fd", "#2a91fd", "#d2e8fe", "#f2f8ff", "#d2e8fe"),
     ("#007bfc", "#092642", "#d2e8fe", "#2a91fd", "#53a6fd", "#072f5a", "#051321", "#092642")),
    ("default-mono", "Default mono",
     ("#333536", "#dfe8df", "#333536", "#333536", "#007bfc", "#dadcdc", "#fafafa", "#e3e3e3"),
     ("#ffffff", "#323334", "#eaebeb", "#bdbdbd", "#53a6fd", "#333333", "#101111", "#242626")),
    ("sky-blue", "Sky blue",
     ("#009de2", "#d3e3ee", "#009de2", "#009de2", "#007bfc", "#d3e3ee", "#f2fafe", "#caecfa"),
     ("#009de2", "#072b46", "#d3e3ee", "#009de2", "#53a6fd", "#08344e", "#05151d", "#092c3d")),
    ("brown", "Brown",
     ("#9e6947", "#f4ded1", "#5b3c29", "#855538", "#007bfc", "#f4ded1", "#fcede3", "#edd4c5"),
     ("#c0835d", "#5b3c29", "#f4ded1", "#c0835d", "#53a6fd", "#35271e", "#0a0a0a", "#191310")),
    ("cream", "Cream",
     ("#c1a886", "#efe9e0", "#504334", "#7b654c", "#007bfc", "#efe9e0", "#faf8f5", "#efe9e0"),
     ("#c1a886", "#504334", "#efe9e0", "#c1a886", "#53a6fd", "#3b342b", "#0a0a0a", "#1e1a15")),
    ("emerald", "Emerald",
     ("#00a884", "#d5fded", "#125c4e", "#008069", "#007bfc", "#d5fded", "#f0fff9", "rgba(0, 0, 0, 0.05)"),
     ("#06cf9c", "#0a332c", "#d5fded", "#06cf9c", "#53a6fd", "#0e493d", "#0a0a0a", "#07221d")),
    ("lemon", "Lemon",
     ("#e5cb00", "#fefcdc", "#5c5100", "#bfa40d", "#007bfc", "#fdf8af", "#fefde6", "rgba(0, 0, 0, 0.05)"),
     ("#fbeb1e", "#383100", "#fefcdc", "#fbeb1e", "#53a6fd", "#5c5100", "#0a0a0a", "#242100")),
    ("lime", "Lime",
     ("#64902c", "#e6fbcb", "#3c561a", "#496920", "#007bfc", "#e3ffbd", "#f4fee6", "rgba(0, 0, 0, 0.05)"),
     ("#89cc33", "#3c561a", "#e6fbcb", "#89cc33", "#53a6fd", "#2e4214", "#0a0a0a", "#1e2a0e")),
    ("pink", "Pink",
     ("#e63371", "#ffebf1", "#6d1e3e", "#e63371", "#007bfc", "#ffdae7", "#ffebf1", "#ffdae7"),
     ("#ff518b", "#6d1e3e", "#ffebf1", "#ff518b", "#53a6fd", "#571e35", "#0a0a0a", "#2b1221")),
    ("red", "Red",
     ("#ea0038", "#fbd8dc", "#a71136", "#d10335", "#007bfc", "#fbd8dc", "#fde8eb", "#fbd8dc"),
     ("#ef3345", "#a71136", "#fbd8dc", "#ef3345", "#53a6fd", "#61182e", "#0a0a0a", "#221118")),
    ("sunrise-orange", "Sunrise orange",
     ("#cb2910", "#ffdbcc", "#801100", "#cb2910", "#007bfc", "#ffdbcc", "#ffede6", "#ffdbcc"),
     ("#ff553b", "#731a0d", "#ffdbcc", "#ff553b", "#53a6fd", "#57140a", "#0a0a0a", "#290e0a")),
    ("warm-yellow", "Warm yellow",
     ("#e5b61a", "#fff0d4", "#9d832c", "#b79115", "#007bfc", "#fff0d4", "#fff7e5", "#ffeac2"),
     ("#fdc85d", "#362e1f", "#fff0d4", "#fdc85d", "#53a6fd", "#514224", "#0a0a0a", "#292419")),
)
# Three of them also set the bubbles' small grey text (times, the voice message's waveform) their own
# way, light and dark; the others leave WhatsApp's own, black or white at 0.6. They set the colour
# only: its -RGB twin keeps WhatsApp's own value, as theirs does (_declarations).
WA_BUBBLE_META = dict.fromkeys(("default-blue", "default-mono", "sky-blue"),
                               ("rgba(17, 27, 33, 0.7)", "rgba(255, 255, 255, 0.5)"))
TWINLESS = ("systems-bubble-content-deemphasized",)

# WhatsApp's own surfaces, which Natter never sets: here for the contrast checks and the splash.
WA_SURFACES = {
    False: {"surface-default": "#ffffff", "surface-elevated-default": "#ffffff", "surface-emphasized": "#f7f5f3",
            "surface-elevated-emphasized": "#f7f5f3", "background-wash-inset": "#f7f5f3",
            "systems-chat-surface-tray": "#f7f5f3", "systems-chat-surface-composer": "#ffffff",
            "systems-bubble-surface-incoming": "#ffffff"},
    True: {"surface-default": "#161717", "surface-elevated-default": "#1d1f1f", "surface-emphasized": "#1d1f1f",
           "surface-elevated-emphasized": "#242626", "background-wash-inset": "#161717",
           "systems-chat-surface-tray": "#161717", "systems-chat-surface-composer": "#242626",
           "systems-bubble-surface-incoming": "#242626"},
}
WA_WALLPAPER = {True: "#161717"}  # Den's and Custom's in dark: neutralGray900, as in WhatsApp's own look
WA_DOODLE_ALPHA = 0.1  # dark doodles: WhatsApp's whiteAlpha10, with white swapped for the theme's palest step
WA_SPLASH = {False: ("#f7f5f3", "#ffffff"), True: ("#161717", "#161717")}  # startup background, -plain
WA_READ = ("#007bfc", "#53bdeb")  # cobalt400 / skyBlue300
WA_DARK_LABEL = "#0a0a0a"  # neutralGray1000: WhatsApp's label on its accent in dark
PAGE_TEXT = {False: "#0a0a0a", True: "#fafafa"}  # WhatsApp's own text, which Natter leaves alone
WHITE = "#ffffff"
GTK_DARK_LABEL = ("#000006", 0.8)  # libadwaita's dark text, laid on the fill

# The least contrast for each check: 3:1 for the label on the accent (WCAG's for large text and
# controls; WhatsApp's own white on its green has 3.01:1), for a switch's knob on it, for read ticks
# and for the accent against the chat list; 4.5:1 for every text.
LABEL_FLOOR = 3.0

# What WhatsApp's own chat themes span, which Custom keeps within: the most chroma any of their
# accents has (Red's #ea0038, 0.238), their darkest accent (Dune mono's, L 0.32), their darkest in
# dark (Persian plum's, L 0.60) and their palest on the chat list (Lemon's, 1.63:1).
C_TOP = 0.24
NEAR_BLACK = min(oklch(light[0])[0] for *_, light, _dark in WA_CHAT_THEMES)
DARK_FILL_MIN = min(oklch(dark[0])[0] for *_, dark in WA_CHAT_THEMES)
PALE_FLOOR = min(contrast(light[0], WHITE) for *_, light, _dark in WA_CHAT_THEMES)
# Their most colourful doodles in light (Warm yellow's, 0.057), which Den's and Custom's keep within:
# green's step 150 at a yellow's or a green's hue has nearly twice that, where WhatsApp's own Lemon,
# Lime and Emerald draw theirs in see-through black.
DOODLE_TOP = max(oklch(light[7])[1] for *_, light, _dark in WA_CHAT_THEMES if light[7].startswith("#"))


def _natter_floor(check: str) -> float:
    if "label on accent" in check or "knob on accent" in check:
        return LABEL_FLOOR
    if "read ticks" in check or "accent on " in check:
        return 3.0
    return 4.5


def floor(check: str, theme: Optional["Theme"] = None) -> float:
    """The least contrast a check may have. Natter's floors for everything Natter makes; on the
    page, a WhatsApp chat theme keeps WhatsApp's own colours, so there the floor is the lowest any
    of WhatsApp's own chat themes has for that check, if that's below Natter's. A pale Custom fill
    (with the dark label) may be as pale on the chat list as WhatsApp's own palest, Lemon's."""
    natter = _natter_floor(check)
    if theme is not None and theme.wa and check.startswith("page "):
        return min(natter, _wa_lowest().get(check, natter))
    if theme is not None and theme.pale and check == "page accent on surface-default":
        return min(natter, PALE_FLOOR)
    return natter


# ---- Ramps ---------------------------------------------------------------------------------------


def at(ramp: tuple, step: float) -> str:
    """A ramp's colour at a step, between two steps mixed in OKLab."""
    if step in STEPS:
        return ramp[STEPS.index(step)].lower()
    above = next(i for i, s in enumerate(STEPS) if s > step)
    low, high = STEPS[above - 1], STEPS[above]
    return _mix(ramp[above - 1], ramp[above], (step - low) / (high - low))


@lru_cache(maxsize=64)
def made_ramp(hue: float, chroma: float = 1.0) -> tuple:
    """A ramp at `hue` with WhatsApp green's lightness at every step and `chroma` times its chroma
    (above 1 on VIVID_STEPS only, and never past C_TOP), never using more of sRGB's room than
    green does at that step (green: 66-95%)."""
    out = []
    for step, green in zip(STEPS, GREEN):
        L, C, H = oklch(green)
        share = C / _cmax(L, H)
        times = chroma if step in VIVID_STEPS else min(chroma, 1.0)
        out.append(from_oklch(L, min(C * times, share * _cmax(L, hue), C_TOP), hue))
    return tuple(out)


@lru_cache(maxsize=None)
def _green_chromas() -> tuple:
    """(lightness, chroma) for each of green's steps, darkest first."""
    return tuple(sorted(oklch(green)[:2] for green in GREEN))


def _green_chroma(L: float) -> float:
    """WhatsApp green's chroma at lightness `L`, between its two nearest steps."""
    points = _green_chromas()
    if L <= points[0][0]:
        return points[0][1]
    for (l0, c0), (l1, c1) in zip(points, points[1:]):
        if L <= l1:
            return c0 + (c1 - c0) * (L - l0) / (l1 - l0)
    return points[-1][1]


FINE_STEPS = tuple(range(50, 1001, 25))


def _along(ramp: tuple, start: str, dark: bool, against: list, ratio: float) -> tuple[str, Optional[int]]:
    """`start`, or failing `ratio` on any of `against`, the nearest point further along its ramp
    (darker in light, lighter in dark, in quarter steps) that has it; with that step, or None."""
    if all(contrast(start, s) >= ratio for s in against):
        return start, None
    L0 = oklch(start)[0]
    for step in (FINE_STEPS[::-1] if dark else FINE_STEPS):
        colour = at(ramp, step)
        if (oklch(colour)[0] > L0) if dark else (oklch(colour)[0] < L0):
            if all(contrast(colour, s) >= ratio for s in against):
                return colour, step
    return _push(start, dark, against, ratio), None


def _push(colour: str, dark: bool, against: list, ratio: float) -> str:
    """`colour`, lighter (dark style) or darker (light style) until it has `ratio` on each of `against`."""
    L, C, H = oklch(colour)
    candidate = colour
    for _ in range(100):
        if all(contrast(candidate, s) >= ratio for s in against):
            break
        L += 0.01 if dark else -0.01
        candidate = from_oklch(L, C, H)
    return candidate


# ---- The themes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    id: str
    name: str  # the picker's tooltip, accessible name and caption
    describe: str  # the picker's accessible description
    brand: str  # the theme's own colour; a made ramp takes its hue
    second: str = ""  # the swatch's other half (Den: the icon's blue); "" for the sent bubble
    group: str = NAME  # the picker's group: NAME's own, or "WhatsApp"
    wa: tuple = ()  # WhatsApp's own chat theme: (light, dark) values in WA_ROLES order
    meta: tuple = ()  # and where it has them, its bubbles' grey text, light and dark (WA_BUBBLE_META)
    chroma: float = 1.0  # made ramps: times green's chroma (Custom's can be more, see made_ramp)
    fills: tuple = ()  # Custom's accent, light and dark (_custom); () takes the ramp's
    muted: bool = False  # WhatsApp's low-chroma pattern: links and strong a dark step in light
    deep: bool = False  # WhatsApp's deep pattern: the same, for an accent that's dark in light
    pale: bool = False  # a pale Custom fill: WhatsApp's dark label on it, light and dark
    read: tuple = WA_READ  # made ramps: read ticks, light and dark
    dark_label: str = "whatsapp"  # a made ramp's label in dark: "white" puts it on the light
    # style's fill, as Den's (no dark text on orange)
    source: str = ""  # where the colours come from

    @property
    def ramp(self) -> tuple:
        """A made theme's ramp (WhatsApp's own themes have their colours instead)."""
        return () if self.wa else made_ramp(round(oklch(self.brand)[2], 1), round(self.chroma, 3))


DEFAULT = "den"
PLAIN = "plain"  # no theme: WhatsApp's own colours, libadwaita's own (system accent included)
CUSTOM = "custom"  # made from the "theme_custom" setting
CUSTOM_DEFAULT = "#ff7800"  # Den's own orange, the icon's: a custom colour that's missing or isn't one
WHATSAPP = "WhatsApp"  # the picker's group of WhatsApp's own chat themes
DEN = Theme("den", "Den", "Orange accents and bubbles, blue read ticks", "#ff7800", "#99c1f1",
            read=("#007bfc", "#99cafe"), dark_label="white",
            source="made at #ff7800's hue (WhatsApp has no ramp there), white labels in both styles")
WA_THEMES = tuple(
    Theme(f"wa-{slug}", name, "Accents, bubbles and wallpaper from WhatsApp’s own chat theme", light[0],
          group=WHATSAPP, wa=(light, dark), meta=WA_BUBBLE_META.get(slug, ()),
          source=f"WhatsApp's {name} chat theme, tonal")
    for slug, name, light, dark in WA_CHAT_THEMES)
THEMES = (DEN,) + WA_THEMES  # every theme with fixed colours (Custom's depend on the setting)
BY_ID = {t.id: t for t in THEMES}
# Natter Den's own, then WhatsApp's in its own picker's order.
GROUPS = ((NAME, (DEFAULT, CUSTOM, PLAIN)), (WHATSAPP, tuple(t.id for t in WA_THEMES)))
_WORDS = {DEFAULT: (DEN.name, DEN.describe), CUSTOM: ("Custom", "Accents and bubbles in a colour you pick"),
          PLAIN: ("Plain", "WhatsApp’s and your system’s own colours"),
          **{t.id: (t.name, t.describe) for t in WA_THEMES}}
# The picker's order and words: (id, name, description), group by group.
CHOICES = [(theme_id, *_WORDS[theme_id]) for _group, ids in GROUPS for theme_id in ids]

# Themes Natter had before it took WhatsApp's own: each is now the WhatsApp theme nearest to it.
OLD_IDS = {"lagoon": "wa-merino-teal", "meadow": "wa-lime", "dusk": "wa-tyrian-purple", "rose": "wa-pink",
           "graphite": "wa-dune-mono"}


def chosen(value) -> str:
    """A setting's value as a theme id: a known one, "custom", "plain", an older theme's nearest,
    or the default for anything else."""
    if not isinstance(value, str):
        return DEFAULT
    if value in BY_ID or value in (CUSTOM, PLAIN):
        return value
    return OLD_IDS.get(value, DEFAULT)


def custom_colour(value) -> str:
    """The "theme_custom" setting as a colour: "#rrggbb" (in lower case), or Den's orange for
    anything else (missing, hand-edited, another notation)."""
    if (isinstance(value, str) and len(value) == 7 and value[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in value[1:])):
        return value.lower()
    return CUSTOM_DEFAULT


# A colour with less chroma than this is greyish and takes WhatsApp's low-chroma pattern: it splits
# WhatsApp's own chat themes, Charcoal green's accent (0.044) and Dune mono's below, Cream's (0.055)
# and every other's above.
MUTED_C = 0.05
# Hues where a pale colour darkened until white reads on it turns tan, mustard or olive (creams,
# yellows, pale limes), so it keeps its colour and takes the dark label: from past the oranges (CSS's
# #ffa500 is at 71°, WhatsApp's Brown at 53°, both darken well) to WhatsApp's Lime (130°), the
# yellowest green WhatsApp darkens itself. WhatsApp's own pale ones sit between: Cream at 75°, Warm
# yellow 89°, Lemon 100°.
PALE_HUES = (72.0, 130.0)
DEEP_BELOW = 0.5  # an accent this dark in light takes the deep pattern (Persian plum's is at 0.37)
GREEN_LIGHT_TO_DARK = oklch(GREEN[STEPS.index(450)])[0] - oklch(GREEN[STEPS.index(500)])[0]  # a step: 0.059
DARK_ACCENT_L = oklch(GREEN[STEPS.index(700)])[0]  # the low-chroma pattern's accent in light: step 700


def _until(L: float, C: float, H: float, stop: float, good) -> float:
    """The lightness nearest `L`, going towards `stop`, at which the colour of chroma `C` and hue
    `H` passes `good`: `L` itself if it does, else no further than `stop`. `good` changes only once
    along the way (white reads better on a colour the darker it is), so halving the gap finds where."""
    if good(from_oklch(L, C, H)):
        return L
    bad, fine = L, stop
    for _ in range(30):
        middle = (bad + fine) / 2
        if good(from_oklch(middle, C, H)):
            fine = middle
        else:
            bad = middle
    return fine


def _white_reads(colour: str) -> bool:
    return contrast(WHITE, colour) >= LABEL_FLOOR


@lru_cache(maxsize=32)
def _custom(colour: str) -> Theme:
    """Custom, made from `colour` so that it looks like it, the way WhatsApp makes its chat themes.

    The accent in light is the colour itself where white reads on it and it's neither near-black
    (darker than any accent WhatsApp has) nor more colourful than WhatsApp's own Red; else the
    nearest colour of the same hue and chroma that's all of those. A greyish colour takes the
    low-chroma pattern (dark in light, as Dune mono and Charcoal green). A pale cream, yellow or
    lime keeps its colour with the dark label instead, at most as pale on the chat list as Lemon.
    In dark the accent is a step lighter, as WhatsApp's own are, and no darker than its darkest
    (Persian plum's: the deep pattern); but where white would stop reading there, only as light
    as it still reads (the default orange stays at its light style's accent, as Den's does). The
    ramp for the other names is as colourful as the accent, so links and the strong colour match it."""
    L, C, H = oklch(colour)
    exact = C <= C_TOP
    C = min(C, C_TOP)
    base = colour if exact else from_oklch(L, C, H)
    muted = C < MUTED_C
    pale = not muted and PALE_HUES[0] <= H < PALE_HUES[1] and not _white_reads(base)
    if pale:
        light_L = _until(L, C, H, NEAR_BLACK, lambda fill: contrast(fill, WHITE) >= PALE_FLOOR)
    else:
        start = max(NEAR_BLACK, min(L, DARK_ACCENT_L) if muted else L)
        light_L = _until(start, C, H, NEAR_BLACK, _white_reads)
    light = base if light_L == L else from_oklch(light_L, C, H)
    if pale:
        dark = from_oklch(min(1.0, light_L + GREEN_LIGHT_TO_DARK), C, H)
    else:
        dark_L = _until(max(light_L + GREEN_LIGHT_TO_DARK, DARK_FILL_MIN), C, H, light_L, _white_reads)
        dark = light if dark_L == light_L else from_oklch(dark_L, C, H)
    light_C = oklch(light)[1]
    chroma = light_C / _green_chroma(light_L)
    return Theme(CUSTOM, "Custom", _WORDS[CUSTOM][1], colour, chroma=chroma, fills=(light, dark), muted=muted,
                 deep=not muted and not pale and light_L < DEEP_BELOW, pale=pale,
                 source=f"made from {colour}: accent {light} / {dark}, {chroma:.2f} times green's chroma")


def theme(theme_id: str, custom=None) -> Theme:
    """A theme id's Theme; "custom" is made from `custom`, the "theme_custom" setting."""
    return _custom(custom_colour(custom)) if theme_id == CUSTOM else BY_ID[theme_id]


# ---- GTK: Natter's own window --------------------------------------------------------------------

# libadwaita's own surfaces (1.6+ values; 1.5's are within a step). Natter keeps their lightness and
# only turns the greys towards the accent's hue, at WhatsApp's own warm-grey chroma; white stays white.
GTK_SURFACES = {
    False: {"window_bg_color": "#fafafb", "view_bg_color": "#ffffff", "headerbar_bg_color": "#ffffff",
            "headerbar_backdrop_color": "#fafafb", "sidebar_bg_color": "#ebebed", "sidebar_backdrop_color": "#f2f2f4",
            "secondary_sidebar_bg_color": "#f3f3f5", "dialog_bg_color": "#fafafb", "popover_bg_color": "#ffffff",
            "card_bg_color": "#ffffff", "thumbnail_bg_color": "#ffffff"},
    True: {"window_bg_color": "#222226", "view_bg_color": "#1d1d20", "headerbar_bg_color": "#2e2e32",
           "headerbar_backdrop_color": "#222226", "sidebar_bg_color": "#2e2e32", "sidebar_backdrop_color": "#28282c",
           "secondary_sidebar_bg_color": "#28282c", "dialog_bg_color": "#36363a", "popover_bg_color": "#36363a",
           "thumbnail_bg_color": "#39393d"},
}
GTK_TINT = 0.003  # OKLCH chroma: WhatsApp's warmGray75 has 0.003
GTK_TEXT_SURFACES = ("window_bg_color", "view_bg_color", "headerbar_bg_color", "dialog_bg_color",
                     "popover_bg_color", "sidebar_bg_color")
ACCENT_NAMES = ("accent_bg_color", "accent_fg_color", "accent_color")
# libadwaita's own window colours: the page's colour before WhatsApp paints, with no theme.
PLAIN_WINDOW = {False: "#fafafb", True: "#222226"}

# ---- WhatsApp: what a theme sets, and the surfaces its text and links must read on ---------------

PAGE_NAMES = ("accent", "persistent-always-branded", "content-on-accent", "accent-deemphasized",
              "components-filter-surface-selected", "accent-emphasized", "content-action-emphasized",
              "content-external-link", "content-read", "systems-bubble-surface-outgoing",
              "systems-chat-background-wallpaper", "systems-chat-foreground-wallpaper",
              "systems-bubble-content-deemphasized")  # the last: three of WhatsApp's themes only
TEXT_SURFACES = ("surface-default", "surface-emphasized", "surface-elevated-default", "surface-elevated-emphasized",
                 "background-wash-inset", "systems-chat-surface-composer", "systems-bubble-surface-incoming",
                 "systems-bubble-surface-outgoing")


@dataclass(frozen=True)
class Look:
    """One theme in one style: Natter's window colours, the page's, and the contrast checks."""
    theme: Theme
    dark: bool
    gtk: dict  # libadwaita named colour -> literal
    page: dict  # WhatsApp name without "--WDS-" -> literal; "splash:*" for the start-up screen
    checks: dict  # what -> contrast ratio
    steps: dict  # role -> where its colour came from ("step 600", "WhatsApp's own", "step 650 for 4.5:1")
    knob: str = WHITE  # a switch's knob while it's on: libadwaita's white, or the window's dark label


def _tint(base: str, hue: float, chroma: float) -> str:
    """libadwaita's grey at its own lightness, turned towards `hue` at `chroma` (0: a neutral grey),
    and never past GTK_TINT once it's a hex colour: in the dark greys one 8-bit step is a lot of
    chroma (asking 0.003 of #1d1d20 at a green hue gives #1c1e1c, 0.005), so less is asked until
    what comes out is within it."""
    if base == WHITE:
        return base
    L = oklch(base)[0]
    while chroma > 0:
        colour = from_oklch(L, chroma, hue)
        if oklch(colour)[1] <= GTK_TINT:
            return colour
        chroma -= 0.0005
    return from_oklch(L, 0.0, hue)


def _roles(theme: Theme, dark: bool) -> tuple[dict, dict]:
    """Each role's colour in one style, and where it came from: WhatsApp's own values, or the
    theme's ramp at green's steps."""
    if theme.wa:
        roles = dict(zip(WA_ROLES, theme.wa[dark]))
        return roles, {role: "WhatsApp's own" for role in roles}
    steps = dict(GREEN_STEPS[dark])
    steps.update(MADE_STEPS[dark])
    if theme.muted or theme.deep:
        steps.update(DARK_ACCENT_STEPS[dark])
    roles = {role: at(theme.ramp, step) for role, step in steps.items()}
    where = {role: f"step {step}" for role, step in steps.items()}
    if theme.fills:
        roles["accent"] = theme.fills[dark]
        where["accent"] = "the colour picked" if roles["accent"] == theme.brand else \
            f"{theme.brand}'s hue and chroma, moved for its label" + (" and a step lighter" if dark else "")
    elif not dark:
        # White on the fill at least 3:1, further along the ramp if not.
        roles["accent"], moved = _along(theme.ramp, roles["accent"], False, [WHITE], LABEL_FLOOR)
        if moved:
            where["accent"] = f"step {moved} for a white label (from {where['accent']})"
    return roles, where


def build(theme: Theme, dark: bool) -> Look:
    """One theme's colours in one style (uncached: look() is the cached way in)."""
    roles, where = _roles(theme, dark)
    if theme.wa:
        # WhatsApp's own rule, except where white falls under the floor on its pale fills: across
        # the whole page those label the chat list's unread badges, which WhatsApp's never reach.
        white_label = not dark and _white_reads(roles["accent"])
        if not dark and not white_label:
            where["label"] = "WhatsApp's dark label: white has under 3:1 here"
    elif theme.fills:
        white_label = not theme.pale
    elif dark and theme.dark_label == "white":
        # The light style's fill, which carries white at 3:1 or better.
        light, light_where = _roles(theme, False)
        roles["accent"], where["accent"] = light["accent"], light_where["accent"] + " (light's, white label)"
        white_label = True
    else:
        white_label = not dark
    accent, soft, strong, outgoing = roles["accent"], roles["soft"], roles["strong"], roles["outgoing"]
    label = WHITE if white_label else WA_DARK_LABEL
    wa = WA_SURFACES[dark]
    surfaces = {**wa, "systems-bubble-surface-outgoing": outgoing}
    if theme.wa:
        wallpaper, doodle, link, read = roles["wallpaper"], roles["doodle"], roles["link"], roles["read"]
    else:
        if dark:
            wallpaper = WA_WALLPAPER[True]
            doodle = rgba(strong, WA_DOODLE_ALPHA)
            where["wallpaper"], where["doodle"] = "WhatsApp's own", f"strong at {WA_DOODLE_ALPHA:g}, as WhatsApp's white"
        else:
            wallpaper, doodle = roles["wallpaper"], roles["doodle"]
            doodle_L, doodle_C, doodle_H = oklch(doodle)
            if doodle_C > DOODLE_TOP:
                doodle = from_oklch(doodle_L, DOODLE_TOP, doodle_H)
                where["doodle"] += f", at WhatsApp's most colourful doodles' chroma ({DOODLE_TOP:.3f})"
        link, moved = _along(theme.ramp, roles["link"], dark, [surfaces[k] for k in TEXT_SURFACES], 4.5)
        if moved:
            where["link"] = f"step {moved} for 4.5:1 (from {where['link']})"
        read = _push(theme.read[dark].lower(), dark, [outgoing, wa["surface-default"]], 3.0)

    L, C, H = oklch(accent)
    gtk = {name: _tint(base, H, min(GTK_TINT, C)) for name, base in GTK_SURFACES[dark].items()}
    if dark:
        gtk["card_bg_color"] = "rgba(255, 255, 255, 0.08)"  # see-through: takes the window under it
    on_window = [gtk[k] for k in GTK_TEXT_SURFACES]
    if theme.wa:
        gtk_link = _push(link, dark, on_window, 4.5)
        where["gtk accent text"] = "the link" if gtk_link == link else "the link moved for 4.5:1 on the window"
    else:
        gtk_link, gtk_moved = _along(theme.ramp, roles["link"], dark, on_window, 4.5)
        where["gtk accent text"] = f"step {gtk_moved} for 4.5:1 on the window" if gtk_moved else "the link role's colour"
    # The page's fill and label, so the window and the page match.
    gtk_fill = accent
    gtk["accent_bg_color"] = gtk_fill
    gtk["accent_fg_color"] = WHITE if white_label else "rgba(0, 0, 6, 0.8)"  # legacy syntax: GTK 4.14
    gtk["accent_color"] = gtk_link
    gtk_label = WHITE if white_label else over(GTK_DARK_LABEL[0], GTK_DARK_LABEL[1], gtk_fill)
    # libadwaita paints a switch's knob white while it's on, whatever accent_fg_color is (that only
    # colours its high-contrast I/O mark): on a fill with the dark label, the knob is that label.
    knob = gtk_label

    page = {
        "accent": accent,
        "persistent-always-branded": accent,  # muted chats' badges, progress, mention pills, the brand
        "content-on-accent": label,
        "accent-deemphasized": soft,
        "components-filter-surface-selected": soft,
        "accent-emphasized": strong,
        "content-action-emphasized": link,
        "content-external-link": link,
        "content-read": read,
        "systems-bubble-surface-outgoing": outgoing,
        "systems-chat-background-wallpaper": wallpaper,
        "systems-chat-foreground-wallpaper": doodle,
        "splash:startup-background": WA_SPLASH[dark][0],
        "splash:startup-background-plain": WA_SPLASH[dark][1],
        "splash:progress-primary": accent,
    }
    if theme.meta:
        page["systems-bubble-content-deemphasized"] = theme.meta[dark]
        where["meta"] = "WhatsApp's own"

    ink = PAGE_TEXT[dark]
    checks = {}
    for k in TEXT_SURFACES:
        checks[f"page text on {k}"] = contrast(ink, surfaces[k])
        checks[f"page link on {k}"] = contrast(link, surfaces[k])
    checks["page label on accent"] = contrast(label, accent)
    checks["page accent on surface-default"] = contrast(accent, wa["surface-default"])
    checks["page strong on soft (selected chip)"] = contrast(strong, soft)
    checks["page read ticks on outgoing"] = contrast(read, outgoing)
    checks["page read ticks on surface-default"] = contrast(read, wa["surface-default"])
    for k in GTK_TEXT_SURFACES:
        gtk_ink = WHITE if dark else over(GTK_DARK_LABEL[0], GTK_DARK_LABEL[1], gtk[k])
        checks[f"gtk text on {k}"] = contrast(gtk_ink, gtk[k])
        checks[f"gtk accent text on {k}"] = contrast(gtk_link, gtk[k])
    checks["gtk label on accent"] = contrast(gtk_label, gtk_fill)
    checks["gtk switch knob on accent"] = contrast(knob, gtk_fill)
    return Look(theme, dark, gtk, page, checks, where, knob)


def look(theme_id: str, dark: bool, custom=None) -> Look:
    """A theme id's colours in one style; `custom` is the "theme_custom" setting, used by "custom"
    only (so the other themes' cache doesn't hold one copy per colour)."""
    return _look(theme_id, dark, custom_colour(custom) if theme_id == CUSTOM else None)


@lru_cache(maxsize=128)
def _look(theme_id: str, dark: bool, custom: Optional[str]) -> Look:
    return build(theme(theme_id, custom), dark)


@lru_cache(maxsize=None)
def _wa_lowest() -> dict:
    """The least each page check has across WhatsApp's own chat themes, light and dark."""
    lowest: dict = {}
    for wa_theme in WA_THEMES:
        for dark in (False, True):
            for what, ratio in look(wa_theme.id, dark).checks.items():
                if what.startswith("page "):
                    lowest[what] = min(ratio, lowest.get(what, ratio))
    return lowest


# ---- Outputs -------------------------------------------------------------------------------------


def gtk_css(theme_id: str, dark: bool, high_contrast: bool, variables: bool, custom=None) -> str:
    """The window's colours for Natter's style provider ("" for plain). `variables`: GTK 4.16+,
    which also needs them as :root variables (Ubuntu's Yaru sets some of those as literals, and
    libadwaita works the standalone accent out by itself unless it's given). In high contrast
    only the accent is set: libadwaita's own high-contrast surfaces stay. On a fill with the dark
    label, one rule more: the knob of a switch that's on (Look.knob)."""
    if chosen(theme_id) == PLAIN:
        return ""
    shown = look(chosen(theme_id), dark, custom)
    colours = shown.gtk
    names = list(ACCENT_NAMES) if high_contrast else list(colours)
    out = [f"@define-color {n} {colours[n]};" for n in names]
    if variables:
        out += [":root {"] + [f"  --{n.replace('_', '-')}: {colours[n]};" for n in names] + ["}"]
    if shown.knob != WHITE:
        # The fill is set in high contrast too. Above libadwaita's style sheet, this wins over its
        # white knob (switch:hover's too) whatever their selectors.
        out.append(f"switch:checked > slider {{ background-color: {shown.knob}; }}")
    return "\n".join(out) + "\n"


def _declarations(values: dict, splash: bool) -> list[str]:
    out = []
    for name, value in values.items():
        if name.startswith("splash:") != splash:
            continue
        if splash:
            var = "--splashscreen-" + name[len("splash:"):]
            out.append(f"  {var}: {value} !important;")
            if var == "--splashscreen-startup-background":
                out.append(f"  --splashscreen-startup-background-rgb: {rgb_triplet(value)} !important;")
            continue
        out.append(f"  --WDS-{name}: {value} !important;")
        if name not in TWINLESS:
            out.append(f"  --WDS-{name}-RGB: {rgb_triplet(value)} !important;")
    return out


def page_css(value, custom=None) -> str:
    """The style sheet for WhatsApp's page ("" for plain), for settings values of any kind: they're
    made a theme id and a colour before the cache sees them (a hand-edited list or dict can't be
    cached). `custom` is the "theme_custom" setting."""
    theme_id = chosen(value)
    return _page_css(theme_id, custom_colour(custom) if theme_id == CUSTOM else None)


@lru_cache(maxsize=64)
def _page_css(theme_id: str, custom: Optional[str]) -> str:
    """The style sheet for WhatsApp's page ("" for plain): WhatsApp's own colour names, redefined.

    The page's colours follow WhatsApp's own light or dark (its body.dark class), so they always
    match what WhatsApp draws, even when it's been set to a look of its own. The start-up screen
    comes before that class, so it follows prefers-color-scheme (Natter's style) instead."""
    if theme_id == PLAIN:
        return ""
    light, dark = look(theme_id, False, custom).page, look(theme_id, True, custom).page
    name = theme(theme_id, custom).name
    return "\n".join([
        f"/* {NAME} theme “{name}”, made by natter/themes.py: WhatsApp's own colour names only. */",
        "html {", *_declarations(light, True), "}",
        "@media (prefers-color-scheme: dark) {", "html {", *_declarations(dark, True), "}", "}",
        "body, #main {", *_declarations(light, False), "}",
        "body.dark, body.dark #main {", *_declarations(dark, False), "}",
    ]) + "\n"


def page_background(theme_id: str, dark: bool) -> str:
    """The page's colour until WhatsApp paints: its start-up screen's, so there's no step between.
    WhatsApp's own for every theme (build() sets it so), so no theme needs making for it."""
    if chosen(theme_id) == PLAIN:
        return PLAIN_WINDOW[dark]
    return WA_SPLASH[dark][0]


def window_background(theme_id: str, dark: bool, custom=None) -> str:
    """The window's colour (behind the header bar in a snapshot)."""
    if chosen(theme_id) == PLAIN:
        return PLAIN_WINDOW[dark]
    return look(chosen(theme_id), dark, custom).gtk["window_bg_color"]


# Plain's swatch: libadwaita's own light and dark greys.
PLAIN_SWATCH = ("#deddda", "#77767b")


# Two colours nearer than this in OKLab look the same (Den's ramp keeps its orange within it of the icon's).
SAME_TO_THE_EYE = 0.02


def _distance(a: str, b: str) -> float:
    return math.dist(_oklab(hex_rgb(a)), _oklab(hex_rgb(b)))


@lru_cache(maxsize=None)
def _wa_swatches(dark: bool) -> dict:
    """WhatsApp's themes' swatches in one style, from its own values (no theme needs making): each
    one's accent and sent bubble. Where those look the same as an earlier theme's (Default blue's
    as Royal blue's in dark, Default mono's as Dune mono's in light), its chat background instead,
    the colour that tells the two apart there."""
    out = {}
    for wa_theme in WA_THEMES:
        roles = dict(zip(WA_ROLES, wa_theme.wa[dark]))
        pair = (roles["accent"], roles["outgoing"])
        if any(all(_distance(a, b) < SAME_TO_THE_EYE for a, b in zip(pair, other)) for other in out.values()):
            pair = (roles["accent"], roles["wallpaper"])
        out[wa_theme.id] = pair
    return out


def swatch(theme_id: str, dark: bool, custom=None) -> tuple[str, str]:
    """A choice's two swatch colours in one style, as the page has them in that style: its accent
    and its second colour, or else its sent bubble (WhatsApp's own: _wa_swatches). Custom's shows
    the theme made from its colour, as every swatch shows its theme: in light, the colour picked
    itself wherever the theme can use it as it is (_custom); the Custom Colour button beside it
    shows the colour as picked."""
    if theme_id == PLAIN:
        return PLAIN_SWATCH
    if theme_id in BY_ID and BY_ID[theme_id].wa:
        return _wa_swatches(dark)[theme_id]
    shown = look(theme_id, dark, custom)
    return shown.page["accent"], shown.theme.second or shown.page["systems-bubble-surface-outgoing"]


# 36 px: seven of them 8 px apart fit across a row of Preferences at its narrowest, 360 px (prefs.py).
SWATCH_CSS = """
checkbutton.natter-swatch { padding: 0; min-width: 36px; min-height: 36px; border-radius: 9999px; outline: none; }
checkbutton.natter-swatch > radio { -gtk-icon-source: none; min-width: 0; min-height: 0; margin: 0; padding: 0;
  border: none; background: none; box-shadow: none; }
checkbutton.natter-swatch > image { margin: 0; opacity: 0; transition: opacity 150ms; border-radius: 9999px;
  padding: 2px; -gtk-icon-size: 12px; }
checkbutton.natter-swatch:checked > image { opacity: 1; }
"""


def swatch_css(dark: bool, high_contrast: bool, custom=None) -> str:
    """The picker's swatches, one per choice (CHOICES): its two colours (swatch()) split diagonally;
    the chosen one ringed in its accent text colour (4.5:1 on the window), with a see-through gap.
    `custom` is the "theme_custom" setting: Custom's swatch shows the theme made from it. Every
    theme's colours go into this (the rings are their window's), so app.py loads it only while
    there's a picker to show it."""
    border = ("rgba(255, 255, 255, 0.6)" if dark else "rgba(0, 0, 6, 0.6)") if high_contrast else \
             ("rgba(255, 255, 255, 0.2)" if dark else "rgba(0, 0, 6, 0.15)")
    # The picker sits on a Preferences row (a card): its tick badge's cut-out takes the card's
    # colour, which in dark is see-through white over the dialog, so it's mixed here.
    cut = "mix(@dialog_bg_color, white, 0.08)" if dark else "@card_bg_color"
    focus, halo = (0.85, 0.8) if high_contrast else (0.5, 0.35)
    rules = [SWATCH_CSS]
    for theme_id, _name, _describe in CHOICES:
        fill, second = swatch(theme_id, dark, custom)
        ring = "@accent_color" if theme_id == PLAIN else look(theme_id, dark, custom).gtk["accent_color"]
        rules.append(
            f"checkbutton.natter-swatch.theme-{theme_id} {{ background-image: linear-gradient(135deg, {fill} 49%,"
            f" {second} 51%); box-shadow: inset 0 0 0 1px {border}; outline: 0 solid transparent; outline-offset: 2px; }}\n"
            f"checkbutton.natter-swatch.theme-{theme_id}:checked {{ outline: 2px solid {ring}; }}\n"
            f"checkbutton.natter-swatch.theme-{theme_id}:focus-visible {{ outline: 2px solid alpha({ring}, {focus}); }}\n"
            f"checkbutton.natter-swatch.theme-{theme_id}:checked:focus-visible {{ outline: 2px solid {ring};"
            f" box-shadow: inset 0 0 0 1px {border}, 0 0 0 7px alpha({ring}, {halo}); }}\n"
            f"checkbutton.natter-swatch.theme-{theme_id} > image {{ background-color: {ring}; color: {cut};"
            f" box-shadow: 0 0 0 2px {cut}; }}\n")
    return "\n".join(rules)
