"""The app icon and the tray's symbolic icons: drawn away from WhatsApp's green, legible at panel
sizes, with the unread dot cut out of the bubbles, and shipped and named alike everywhere."""

import colorsys
import os
import re
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

from natter import DEFAULT_APP_ID
from natter.tray import ICON, ICON_UNREAD, icon_names, tray_host_available

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HICOLOR = DATA / "icons" / "hicolor"
APP_ICON = HICOLOR / "scalable" / "apps" / f"{DEFAULT_APP_ID}.svg"
UNREAD_ICON = HICOLOR / "scalable" / "apps" / f"{DEFAULT_APP_ID}-unread.svg"
SYMBOLIC = HICOLOR / "symbolic" / "apps" / f"{DEFAULT_APP_ID}-symbolic.svg"
UNREAD_SYMBOLIC = HICOLOR / "symbolic" / "apps" / f"{DEFAULT_APP_ID}-unread-symbolic.svg"
ALL = (APP_ICON, UNREAD_ICON, SYMBOLIC, UNREAD_SYMBOLIC)
SVG = "{http://www.w3.org/2000/svg}"
SIZES = (16, 22, 24, 32, 48, 64, 96, 128)
WHATSAPP_GREEN = "#25d366"
DOTS = (28, 54, 80)  # the front bubble's three dots, across the 128-unit icon
DOT_ROW = 66


def draw(svg, size: int) -> GdkPixbuf.Pixbuf:
    """The icon as a desktop draws it (librsvg, through GdkPixbuf), from a file or SVG text."""
    if isinstance(svg, Path):
        return GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), size, size)
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.set_size(size, size)
    loader.write(svg.encode())
    loader.close()
    return loader.get_pixbuf()


def pixel(pixbuf: GdkPixbuf.Pixbuf, x: int, y: int) -> tuple[int, int, int, int]:
    offset = y * pixbuf.get_rowstride() + x * pixbuf.get_n_channels()
    pixels = pixbuf.get_pixels()
    if pixbuf.get_has_alpha():
        return tuple(pixels[offset:offset + 4])
    return (*pixels[offset:offset + 3], 255)


def hue(colour: str) -> tuple[float, float]:
    """(hue in degrees, saturation) of "#rrggbb" or "#rgb"."""
    if len(colour) == 4:
        colour = "#" + "".join(c * 2 for c in colour[1:])
    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, _l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s


def fills(svg: Path) -> list[str]:
    return [element.get("fill").lower() for element in ElementTree.parse(svg).iter()
            if (element.get("fill") or "").startswith("#")]


def dots_apart(svg: Path, size: int) -> bool:
    """Whether, along the dots' row at `size` px, something darker than the dots lies between
    each two of them."""
    pixbuf = draw(svg, size)
    y = int(DOT_ROW * size / 128)

    def white(x: int) -> int:
        r, g, b, a = pixel(pixbuf, x, y)
        return min(r, g, b) * a // 255

    centres = [int(dot * size / 128) for dot in DOTS]
    peaks = [max(range(max(0, c - 1), min(size, c + 2)), key=white) for c in centres]
    return all(b > a + 1 and min(white(x) for x in range(a + 1, b)) < min(white(a), white(b)) / 2
               for a, b in zip(peaks, peaks[1:]))


class ColourIconTest(unittest.TestCase):
    def test_every_icon_draws_at_every_size(self):
        for svg in ALL:
            for size in SIZES:
                pixbuf = draw(svg, size)
                self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (size, size), svg.name)
                self.assertTrue(any(pixel(pixbuf, x, y)[3] == 255 for x in range(size) for y in range(size)),
                                f"{svg.name} draws nothing at {size} px")

    def test_away_from_whatsapp_green(self):
        # Flat colours (gradients blur at 16 px), none of them with the hue of WhatsApp's own icon.
        whatsapp = hue(WHATSAPP_GREEN)[0]
        for svg in (APP_ICON, UNREAD_ICON):
            text = svg.read_text()
            self.assertNotIn("Gradient", text, svg.name)
            for colour in fills(svg):
                h, s = hue(colour)
                if s > 0.3:
                    self.assertGreater(min(abs(h - whatsapp), 360 - abs(h - whatsapp)), 40, f"{svg.name}: {colour}")

    def test_dots_stay_apart_at_panel_sizes(self):
        # At least one pixel between dots at 16 px, so the three never run together into a bar.
        circles = list(ElementTree.parse(APP_ICON).iter(f"{SVG}circle"))
        self.assertEqual(sorted(float(c.get("cx")) for c in circles), list(DOTS))
        radius = max(float(c.get("r")) for c in circles)
        self.assertGreaterEqual(min(b - a for a, b in zip(DOTS, DOTS[1:])) - 2 * radius, 128 / 16)
        for svg in (APP_ICON, UNREAD_ICON):
            for size in (16, 22, 24, 32):
                self.assertTrue(dots_apart(svg, size), f"{svg.name}: the dots merge at {size} px")

    def test_unread_dot_is_cut_out_of_the_bubbles(self):
        # A ring of nothing around the dot, where the plain icon has the back bubble: no outline
        # that would vanish against a light panel.
        plain, unread = draw(APP_ICON, 128), draw(UNREAD_ICON, 128)
        self.assertEqual(pixel(plain, 104, 46)[3], 255)
        self.assertEqual(pixel(unread, 104, 46)[3], 0)
        self.assertEqual(pixel(unread, 104, 24), (0xe0, 0x1b, 0x24, 255))
        self.assertNotIn("stroke", UNREAD_ICON.read_text())
        # Everything but the dot draws the same.
        self.assertEqual(pixel(plain, 30, 90), pixel(unread, 30, 90))

    def test_branding_matches_the_icon(self):
        metainfo = ElementTree.parse(DATA / f"{DEFAULT_APP_ID}.metainfo.xml").getroot()
        colours = {c.get("scheme_preference"): c.text.lower() for c in metainfo.iterfind("branding/color")}
        self.assertEqual(set(colours), {"light", "dark"})
        self.assertIn(colours["dark"], fills(APP_ICON))  # the front bubble's edge
        front = hue("#ff7800")[0]
        for colour in colours.values():
            self.assertLess(abs(hue(colour)[0] - front), 10, colour)


class SymbolicIconTest(unittest.TestCase):
    def test_one_colour_the_panel_chooses(self):
        for svg in (SYMBOLIC, UNREAD_SYMBOLIC):
            root = ElementTree.parse(svg).getroot()
            self.assertEqual(root.get("viewBox"), "0 0 16 16")
            styles = [s for s in root.iter(f"{SVG}style") if s.get("id") == "current-color-scheme"]
            self.assertEqual(len(styles), 1, svg.name)  # what KDE Plasma replaces with its colours
            shapes = [e for e in root.iter() if e.tag in (f"{SVG}path", f"{SVG}circle", f"{SVG}rect")]
            self.assertTrue(shapes)
            for shape in shapes:
                classes = set(shape.get("class", "").split())
                self.assertTrue("ColorScheme-Text" in classes or {"error", "ColorScheme-NegativeText"} <= classes,
                                f"{svg.name}: a shape the panel can't colour")
                self.assertEqual(shape.get("style"), "fill:currentColor")
                self.assertIsNone(shape.get("fill"))
        dots = [e for e in ElementTree.parse(UNREAD_SYMBOLIC).iter() if "error" in e.get("class", "").split()]
        self.assertEqual(len(dots), 1)

    def test_gtk_recolours_them(self):
        # GTK draws a symbolic icon inside an SVG of its own whose style overrides every fill:
        # the text colour, and its error colour for the unread dot (gdkpixbufutils.c).
        import base64

        for svg in (SYMBOLIC, UNREAD_SYMBOLIC):
            data = base64.b64encode(svg.read_bytes()).decode()
            wrapped = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xi="http://www.w3.org/2001/XInclude" '
                       f'width="16" height="16"><style type="text/css">'
                       f'rect,circle,path {{ fill: #00ff00 !important; }} .error {{ fill: #0000ff !important; }}'
                       f'</style><xi:include href="data:text/xml;base64,{data}"/></svg>')
            pixbuf = draw(wrapped, 64)
            self.assertEqual(pixel(pixbuf, 24, 42), (0, 255, 0, 255), svg.name)  # the front bubble
        self.assertEqual(pixel(pixbuf, 54, 10), (0, 0, 255, 255))  # the unread dot

    def test_plasma_recolours_them(self):
        scheme = ".ColorScheme-Text { color: #fcfcfc; } .ColorScheme-NegativeText { color: #da4453; }"
        for svg in (SYMBOLIC, UNREAD_SYMBOLIC):
            text = re.sub(r'(<style id="current-color-scheme"[^>]*>).*?(</style>)', rf"\g<1>{scheme}\g<2>",
                          svg.read_text(), flags=re.S)
            pixbuf = draw(text, 64)
            self.assertEqual(pixel(pixbuf, 24, 42), (0xfc, 0xfc, 0xfc, 255), svg.name)
        self.assertEqual(pixel(pixbuf, 54, 10), (0xda, 0x44, 0x53, 255))

    def test_dots_show_through(self):
        # The three dots are holes in the bubble, so they take the panel's own background.
        pixbuf = draw(SYMBOLIC, 64)
        for x in (12, 24, 36):
            self.assertEqual(pixel(pixbuf, x, 32)[3], 0)


class TrayIconTest(unittest.TestCase):
    def test_symbolic_except_on_ada(self):
        self.assertEqual(icon_names("Ada"), (ICON, ICON_UNREAD))
        self.assertEqual(icon_names("ada"), (ICON, ICON_UNREAD))
        for desktop in ("GNOME", "ubuntu:GNOME", "KDE", "XFCE", "sway", ""):
            self.assertEqual(icon_names(desktop), (f"{ICON}-symbolic", f"{ICON_UNREAD}-symbolic"), desktop)
        self.assertEqual(icon_names("Adamant"), (f"{ICON}-symbolic", f"{ICON_UNREAD}-symbolic"))

    def test_every_name_has_its_file(self):
        # Named by the default id even in a separate instance, which has no icons of its own.
        self.assertEqual(ICON, DEFAULT_APP_ID)
        for desktop in ("Ada", "GNOME"):
            for name in icon_names(desktop):
                folder = "symbolic" if name.endswith("-symbolic") else "scalable"
                self.assertTrue((HICOLOR / folder / "apps" / f"{name}.svg").is_file(), name)


class TrayHostTest(unittest.TestCase):
    def test_no_session_bus(self):
        # Preferences asks too, to say whether Run in Background can show a tray icon.
        self.assertFalse(tray_host_available(None))


class ShippedTest(unittest.TestCase):
    def test_installers_ship_every_icon(self):
        install = (ROOT / "install.sh").read_text()
        icons = sorted(HICOLOR.rglob("*.svg"))
        self.assertEqual(len(icons), 4)
        for svg in icons:
            suffix = svg.name[len(DEFAULT_APP_ID):]
            self.assertTrue(svg.name.startswith(DEFAULT_APP_ID), svg.name)  # what a Flatpak exports
            folder = svg.parent.relative_to(DATA).as_posix()
            self.assertIn(f'"$HERE/data/{folder}/$APP_ID{suffix}"', install)  # copied in
            self.assertIn(f"/$1{suffix}\"", install)  # and removed again
        self.assertIn("install_subdir('data/icons', install_dir: datadir)", (ROOT / "meson.build").read_text())

    def test_one_copy_of_the_icon(self):
        self.assertEqual([p for p in DATA.rglob("*.svg") if HICOLOR not in p.parents], [])

    def test_notifications_carry_the_icon_in_a_separate_instance(self):
        env = dict(os.environ, NATTER_APP_ID=f"{DEFAULT_APP_ID}.Lab")
        script = "from natter import APP_ID, notify; print(APP_ID); print(notify.APP_ICON)"
        out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True,
                             check=True).stdout.split()
        self.assertEqual(out, [f"{DEFAULT_APP_ID}.Lab", str(APP_ICON)])

    def test_readme_starts_with_the_icon(self):
        first = (ROOT / "README.md").read_text().splitlines()[0]
        source = re.fullmatch(r'<img src="([^"]+)" width="96" height="96" alt="">', first)
        self.assertTrue(source, first)
        self.assertEqual(ROOT / source.group(1), APP_ICON)

    def test_store_listing_tells_how_calls_work(self):
        metainfo = ElementTree.parse(DATA / f"{DEFAULT_APP_ID}.metainfo.xml").getroot()
        text = " ".join(" ".join(metainfo.find("description").itertext()).split())
        self.assertIn("GStreamer’s WebRTC plugins", text)
        self.assertNotIn("calls open in your web browser: they need WebRTC", text)


if __name__ == "__main__":
    unittest.main()
