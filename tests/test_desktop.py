"""Natter's desktop pieces without a desktop: dock menu, notification text and pictures, search, tray icons."""

import hashlib
import re
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

from natter.launcher import DockMenu, chat_key, exec_quote, menu_name, with_chat_actions, without_chat_actions
from natter.notify import BODY_CHARS, SUMMARY_CHARS, clip, decode_image, escape, picture_allowed, texts
from natter.search import SearchProvider
from natter.settings import Chat
from natter.tray import ITEM_PATH, MenuItem, Tray, argb

# Every bidi control and invisible mark Natter drops from names.
INVISIBLE = "\u061c\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff"
FAMILY = "\U0001F468\u200d\U0001F469\u200d\U0001F467"  # one emoji, joined with U+200D
HEART = "\u2764\ufe0f"  # with a variation selector

ENTRY = """[Desktop Entry]
Type=Application
Name=Natter
Exec=/home/me/.local/bin/natter %U
Icon=io.github.Officialbabs.Natter
Actions=new;

[Desktop Action new]
Name=New Window
Exec=/home/me/.local/bin/natter --new
"""


def exec_lines(entry: str) -> list[str]:
    return [line for line in entry.splitlines() if line.startswith("Exec=")]


class ChatKeyTest(unittest.TestCase):
    def test_stable_and_nameless(self):
        key = chat_key("main", "Mum")
        self.assertEqual(key, hashlib.sha1(b"main/Mum").hexdigest()[:16])
        self.assertEqual(key, chat_key("main", "Mum"))
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertNotEqual(key, chat_key("work", "Mum"))
        self.assertNotEqual(key, chat_key("main", "Mum "))


class DesktopEntryTest(unittest.TestCase):
    def test_names_never_reach_exec(self):
        chats = [("main", "Mum"), ("work", "Team; rm -rf ~ $(id) `x` %u")]
        entry = with_chat_actions(ENTRY, chats)
        execs = exec_lines(entry)
        self.assertIn(f"Exec=/home/me/.local/bin/natter --account main --chat-id {chat_key('main', 'Mum')}", execs)
        for line in execs:
            self.assertNotIn("Mum", line)
            self.assertNotIn("Team", line)
        self.assertIn("Name=Team; rm -rf ~ $(id) `x` %u", entry)

    def test_no_control_characters(self):
        name = "Ana\nExec=evil\r\x1b[31m\tSouza\x00\x85"
        entry = with_chat_actions(ENTRY, [("main", name)])
        self.assertIsNone(re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", entry))
        self.assertIn("Name=Ana Exec=evil [31m Souza", entry)
        self.assertEqual(len(exec_lines(entry)), 3)  # the main one, "new", and the chat's

    def test_long_names_are_cut(self):
        self.assertEqual(len(menu_name("x" * 200)), 60)
        self.assertTrue(menu_name("x" * 200).endswith("…"))
        self.assertEqual(menu_name("  Ana \u2028 Souza  "), "Ana Souza")

    def test_invisible_characters_are_dropped_but_emoji_stay_whole(self):
        for mark in INVISIBLE:
            self.assertEqual(menu_name(f"Ana{mark}Souza"), "AnaSouza", hex(ord(mark)))
        # An override left open would turn "Mum" and anything after it around.
        self.assertEqual(menu_name("\u202eMum\u2066 \u200f(work)"), "Mum (work)")
        self.assertEqual(menu_name(f"{FAMILY} Family {HEART}"), f"{FAMILY} Family {HEART}")
        entry = with_chat_actions(ENTRY, [("main", f"\u202e{FAMILY}\u200b Family")])
        self.assertIn(f"Name={FAMILY} Family\n", entry)

    def test_other_actions_survive(self):
        result = with_chat_actions(ENTRY, [("main", "Mum"), ("main", "Dad")])
        self.assertIn("Actions=new;chat-0;chat-1;", result)
        self.assertIn("[Desktop Action new]\nName=New Window\nExec=/home/me/.local/bin/natter --new", result)
        self.assertEqual(without_chat_actions(result), ENTRY)

    def test_unsafe_accounts_and_empty_names_are_left_out(self):
        result = with_chat_actions(ENTRY, [("ma\nin", "Mum"), ("main", " \t ")])
        self.assertEqual(result, ENTRY)

    def test_launcher_path_is_quoted(self):
        result = with_chat_actions(ENTRY, [("main", "Mum")], launcher="/opt/my apps/natter")
        self.assertIn('Exec="/opt/my apps/natter" --account main --chat-id', result)


class ExecQuoteTest(unittest.TestCase):
    def test_control_characters_are_refused(self):
        for bad in ("a\nb", "a\rb", "tab\there", "\x00", "esc\x1b", "del\x7f"):
            with self.assertRaises(ValueError):
                exec_quote(bad)

    def test_quoting(self):
        self.assertEqual(exec_quote("main"), "main")
        self.assertEqual(exec_quote(""), '""')
        self.assertEqual(exec_quote("/opt/my apps/natter"), '"/opt/my apps/natter"')
        self.assertEqual(exec_quote("100%"), '"100%%"')


class DockMenuWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.entry = self.root / "applications" / "natter.desktop"
        self.entry.parent.mkdir()
        self.entry.write_text(ENTRY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_rarely_and_only_on_change(self):
        self.entry.with_suffix(".tmp").write_text("left from before")
        self.entry.with_suffix(".tmp").chmod(0o644)
        menu = DockMenu(self.entry, self.root)
        self.assertTrue(menu.supported())
        self.assertTrue(menu.set([("main", "Mum"), ("main", "Dad")]))
        self.assertIn("Name=Mum", self.entry.read_text())
        self.assertEqual(self.entry.stat().st_mode & 0o777, 0o600)  # it names chats
        # The same chats in another order aren't worth rewriting the entry for.
        self.assertFalse(menu.set([("main", "Dad"), ("main", "Mum")]))
        # A new chat within five minutes waits...
        self.assertFalse(menu.set([("main", "Ana")]))
        self.assertNotIn("Ana", self.entry.read_text())
        self.assertEqual(menu.pending, [("main", "Ana")])
        # ...unless forced.
        self.assertTrue(menu.set([], force=True))
        self.assertEqual(self.entry.read_text(), ENTRY)
        if menu.timer:
            GLib.source_remove(menu.timer)

    def test_only_the_users_own_entry(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        menu = DockMenu(self.entry, Path(outside.name))
        self.assertFalse(menu.supported())
        self.assertFalse(menu.set([("main", "Mum")], force=True))
        self.assertEqual(self.entry.read_text(), ENTRY)
        self.assertFalse(DockMenu(self.root / "missing.desktop", self.root).supported())


class NotificationTextTest(unittest.TestCase):
    def test_capped(self):
        summary, body = texts("A" * 500, "b" * 2000, 1, None, True, True, False)
        self.assertEqual(len(summary), SUMMARY_CHARS)
        self.assertEqual(len(body), BODY_CHARS)
        self.assertTrue(body.endswith("…"))
        self.assertEqual(clip("short", 10), "short")

    def test_escaped_only_for_markup(self):
        self.assertEqual(escape("<b>Tom & Jerry</b>"), "&lt;b&gt;Tom &amp; Jerry&lt;/b&gt;")
        self.assertEqual(texts("Mum", "1 < 2 & 3", 1, None, True, True, True)[1], "1 &lt; 2 &amp; 3")
        self.assertEqual(texts("Mum", "1 < 2 & 3", 1, None, True, True, False)[1], "1 < 2 & 3")

    def test_summary_is_one_clean_line(self):
        summary, body = texts("Mum\n\x1bDad", "line one\nline\x07 two", 3, "Work", True, True, False)
        self.assertEqual(summary, "Mum Dad (3) · Work")
        self.assertEqual(body, "line one\nline two")

    def test_summary_drops_invisible_characters(self):
        summary, body = texts(f"\u202e{FAMILY} Mum\u2067", "\u200fשלום \u2066x\u2069", 2, "Work\ufeff",
                              True, True, False)
        self.assertEqual(summary, f"{FAMILY} Mum (2) · Work")
        self.assertEqual(body, "\u200fשלום \u2066x\u2069")  # a message keeps its direction marks
        self.assertEqual(clip("a\x85b\x9bc", 10), "abc")

    def test_hidden_sender_and_message(self):
        self.assertEqual(texts("Mum", "Hi", 1, None, False, False, True), ("New message", ""))
        self.assertEqual(texts("Mum", "Hi", 4, "Work", False, True, True), ("4 new messages · Work", "Hi"))


class PictureTest(unittest.TestCase):
    def test_allowlist(self):
        for url in ("https://pps.whatsapp.net/v/t61.24694-24/1.jpg?ccb=11-4&oh=x",
                    "https://media-lhr8-1.cdn.whatsapp.net/a.jpg",
                    "data:image/jpeg;base64,/9j/4AAQ", "data:image/png;base64,iVBORw0KGgo=",
                    "data:image/webp;base64,UklGRg=="):
            self.assertTrue(picture_allowed(url), url)
        for url in ("http://pps.whatsapp.net/a.jpg", "https://whatsapp.net/a.jpg",
                    "https://pps.whatsapp.net.evil.example/a.jpg", "https://evil.example/?h=.whatsapp.net",
                    "https://pps.whatsapp.net@evil.example/a.jpg", "file:///home/me/a.png",
                    "data:image/svg+xml;base64,PHN2Zz4=", "data:text/html;base64,PGI+",
                    "data:image/png,rawbytes", "", "pps.whatsapp.net/a.jpg"):
            self.assertFalse(picture_allowed(url), url)

    def test_only_jpeg_png_webp_decode(self):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 4, 3)
        pixbuf.fill(0x336699FF)
        ok, png = pixbuf.save_to_bufferv("png", [], [])
        self.assertTrue(ok)
        decoded = decode_image(png)
        self.assertEqual((decoded.get_width(), decoded.get_height()), (4, 3))
        ok, bmp = pixbuf.save_to_bufferv("bmp", [], [])
        self.assertIsNone(decode_image(bmp))
        self.assertIsNone(decode_image(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'))
        self.assertIsNone(decode_image(b"\x89PNG\r\n\x1a\nbroken"))


class FakeBus:
    def register_object(self, *_args):
        return 1

    def unregister_object(self, _id):
        pass


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.on = True
        self.opened = []
        chats = [("main", "Personal", Chat("Ana Souza", 30)), ("main", "Personal", Chat("Banana Club", 20)),
                 ("work", "Work", Chat("Team Ana", 10)), ("work", "Work", Chat("Bob", 5))]
        self.provider = SearchProvider(FakeBus(), lambda: chats, lambda a, n: self.opened.append((a, n)),
                                       lambda q: None, lambda: self.on)

    def test_sure_hits_only(self):
        self.assertEqual(self.provider.search(["ana"]), ["main/Ana Souza", "work/Team Ana", "main/Banana Club"])
        self.assertEqual(self.provider.search(["bc"]), [])  # letters in order aren't enough here
        self.assertEqual(self.provider.search(["  "]), [])

    def test_off_means_nothing(self):
        self.on = False
        self.assertEqual(self.provider.search(["ana"]), [])

    def test_metas_name_the_account_when_there_are_several(self):
        metas = self.provider.metas(["work/Team Ana", "gone/Someone"])
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["name"].get_string(), "Team Ana")
        self.assertEqual(metas[0]["description"].get_string(), "WhatsApp chat · Work")
        self.assertEqual(self.provider.metas(["work/Team\u202e\nAna"])[0]["name"].get_string(), "Team Ana")

    def test_activates_only_known_chats(self):
        self.provider._activate("main", "Ana Souza")
        self.provider._activate("main", "Somebody Else")
        self.assertEqual(self.opened, [("main", "Ana Souza")])


class TrayPixmapTest(unittest.TestCase):
    def test_argb_network_order(self):
        # Two pixels, RGBA: opaque red, then half-transparent blue.
        raw = bytes([255, 0, 0, 255, 0, 0, 255, 128])
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(raw), GdkPixbuf.Colorspace.RGB, True, 8, 2, 1, 8)
        self.assertEqual(argb(pixbuf), bytes([255, 255, 0, 0, 128, 0, 0, 255]))

    def test_padded_rows_and_no_alpha(self):
        # 1x2 RGB with a 4-byte rowstride: one padding byte per row.
        raw = bytes([1, 2, 3, 0, 4, 5, 6, 0])
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(raw), GdkPixbuf.Colorspace.RGB, False, 8, 1, 2, 4)
        self.assertEqual(argb(pixbuf), bytes([255, 1, 2, 3, 255, 4, 5, 6]))


class Invocation:
    def __init__(self):
        self.returned = []

    def return_value(self, value):
        self.returned.append(value)


class TrayClickTest(unittest.TestCase):
    def setUp(self):
        self.tray = Tray.__new__(Tray)  # only its method handler, so no bus
        self.tray.last_token = ""
        self.activated = []
        self.tray.on_activate = self.activated.append

    def call(self, method, signature, *args):
        invocation = Invocation()
        self.tray._item_call(None, ":1.1", ITEM_PATH, "org.kde.StatusNotifierItem", method,
                             GLib.Variant(signature, args), invocation)
        self.assertEqual(invocation.returned, [None])

    def test_context_menu_activates_with_the_token(self):
        # What swaybar and other panels without a dbusmenu send on a right click.
        self.call("ProvideXdgActivationToken", "(s)", "token-1")
        self.call("ContextMenu", "(ii)", 10, 20)
        self.assertEqual(self.activated, ["token-1"])
        self.assertEqual(self.tray.last_token, "")
        self.call("ProvideXdgActivationToken", "(s)", "token-2")
        self.call("SecondaryActivate", "(ii)", 10, 20)
        self.call("Activate", "(ii)", 10, 20)
        self.assertEqual(self.activated, ["token-1", "token-2", ""])

    def test_scroll_does_nothing(self):
        self.call("ProvideXdgActivationToken", "(s)", "token-1")
        self.call("Scroll", "(is)", 120, "vertical")
        self.assertEqual(self.activated, [])
        self.assertEqual(self.tray.last_token, "token-1")

    def test_labels_are_one_clean_line(self):
        item = MenuItem(f"\u202eMum_{FAMILY}\n\u200bwork", lambda: None)
        self.assertEqual(item.label, f"Mum_{FAMILY} work")
        self.assertEqual(item.props()["label"].get_string(), f"Mum__{FAMILY} work")


if __name__ == "__main__":
    unittest.main()
