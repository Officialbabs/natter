"""Natter tests for what it takes from WhatsApp's page: messages, crashes, downloads, links."""

import base64
import re
import unittest

from natter.account import (BACKOFF, BRIDGE, ICON_LIMIT, INVISIBLE, LOOP_SECONDS, STEADY, clean, clean_icon,
                            file_name, parse_message, reload_delay)
from natter.links import same_site, short_link

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(32)).decode()


class MessageTest(unittest.TestCase):
    def test_notification(self):
        message = parse_message({"type": "notify", "id": 7, "title": "Mum\x00\x1b", "body": "Line 1\nLine 2\x07",
                                 "tag": "mum@c.us", "icon": PNG, "token": "ignored here"})
        self.assertEqual(message, {"type": "notify", "id": 7, "title": "Mum", "body": "Line 1\nLine 2",
                                   "tag": "mum@c.us", "icon": PNG})

    def test_wrong_kinds_are_dropped(self):
        good = {"type": "notify", "id": 7, "title": "Mum", "body": "Hi", "tag": "", "icon": ""}
        self.assertIsNotNone(parse_message(good))
        for change in ({"id": "7"}, {"id": True}, {"id": 0}, {"id": -7}, {"id": 2**31}, {"title": None},
                       {"body": ["Hi"]}, {"tag": 3}):
            self.assertIsNone(parse_message(dict(good, **change)), change)
        self.assertIsNone(parse_message({"type": "unknown"}))
        self.assertIsNone(parse_message(["notify"]))
        self.assertIsNone(parse_message({"type": "ready", "ready": 1}))
        self.assertIsNone(parse_message({"type": "state", "value": "something"}))
        self.assertIsNone(parse_message({"type": "call", "video": "yes"}))
        self.assertIsNone(parse_message({"type": "active", "name": 5}))
        self.assertEqual(parse_message({"type": "call", "video": True}), {"type": "call", "video": True})
        self.assertEqual(parse_message({"type": "state", "value": "needs-link"})["value"], "needs-link")

    def test_notification_ids(self):
        for page_id in (1, 2**31 - 1):
            message = parse_message({"type": "notify", "id": page_id, "title": "Mum", "body": "", "tag": ""})
            self.assertEqual(message["id"], page_id)
            self.assertEqual(parse_message({"type": "unnotify", "id": page_id}), {"type": "unnotify", "id": page_id})

    def test_lengths(self):
        message = parse_message({"type": "notify", "id": 1, "title": "x" * 500, "body": "y" * 5000, "tag": ""})
        self.assertEqual((len(message["title"]), len(message["body"])), (200, 2000))
        self.assertEqual(len(parse_message({"type": "active", "name": "z" * 300})["name"]), 200)

    def test_chat_rows(self):
        message = parse_message({"type": "chats", "rows": [["Mum\t", 0], [" ", 1], ["Work\nTeam", 2]]})
        self.assertEqual(message["rows"], [("Mum", 0), ("Work Team", 2)])
        many = parse_message({"type": "chats", "rows": [[f"Chat {i}", i] for i in range(500)]})
        self.assertEqual(len(many["rows"]), 200)
        for rows in ([["Mum", -1]], [["Mum", "0"]], [["Mum", True]], [["Mum"]], [[1, 0]], "Mum"):
            self.assertIsNone(parse_message({"type": "chats", "rows": rows}), rows)

    def test_text(self):
        self.assertEqual(clean("bad \ud800 half", 20), "bad  half")  # GLib can't carry half a surrogate pair
        self.assertIsNone(clean(None, 10))

    def test_invisible_characters(self):
        self.assertEqual(clean("\u202a+44 7700 900123\u202c", 50), "+44 7700 900123")
        self.assertEqual(clean("\u202eevil\u202d\u2066x\u2067y\u2068z\u2069\u202a\u202b\u202c", 50), "evilxyz")
        self.assertEqual(clean("\ufeffa\u200bb\u2060c\u200ed\u200f", 50), "abcd")
        self.assertEqual(clean("Mum\u200f\n", 50, lines=True), "Mum")
        # Emoji keep what they're made of: joiners, variation selectors, tag characters.
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        heart = "\u2764\ufe0f"
        england = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
        for emoji in (family, heart, england, "a\u200db"):
            self.assertEqual(clean(emoji, 20), emoji)
        self.assertEqual(clean(f"\u202b{family} Family\u202c", 50), f"{family} Family")

    def test_icons(self):
        self.assertEqual(clean_icon("https://pps.whatsapp.net/v/t61/photo.jpg?ccb=1"),
                         "https://pps.whatsapp.net/v/t61/photo.jpg?ccb=1")
        for icon in ("https://example.com/a.jpg", "http://pps.whatsapp.net/a.jpg", "https://whatsapp.net.evil.com/a",
                     "https://evil.com\\@pps.whatsapp.net/a", "https://me@pps.whatsapp.net/a",
                     "https://pps.whatsapp.net:8443/a", "data:image/svg+xml;base64,PHN2Zz4=",
                     "data:image/png,rawbytes", "data:image/png;base64,not base64!", "file:///etc/passwd", 5):
            self.assertEqual(clean_icon(icon), "", icon)
        big = "data:image/jpeg;base64," + "A" * (ICON_LIMIT * 4 // 3 + 8)
        self.assertEqual(clean_icon(big), "")
        self.assertEqual(clean_icon(PNG), PNG)


class BridgeTest(unittest.TestCase):
    """The bridge agrees with Natter on what they share."""

    def test_same_invisible_characters(self):
        marks = re.search(r"const INVISIBLE = /\[([^\]]*)\]/g", BRIDGE)[1]
        found = set()
        for first, last in re.findall(r"\\u([0-9A-F]{4})(?:-\\u([0-9A-F]{4}))?", marks):
            found.update(map(chr, range(int(first, 16), int(last or first, 16) + 1)))
        self.assertEqual(found, INVISIBLE)

    def test_notification_ids_fit(self):
        # Each page load starts at a random id up to 2**n, then counts up.
        highest_start = 2 ** int(re.search(r"Math\.random\(\) \* 2 \*\* (\d+)", BRIDGE)[1])
        for page_id in (1, highest_start, highest_start + 10**6):
            self.assertIsNotNone(parse_message({"type": "unnotify", "id": page_id}), page_id)


class CrashTest(unittest.TestCase):
    def test_backoff(self):
        self.assertEqual(reload_delay([0]), BACKOFF[0])
        self.assertEqual(reload_delay([0, 200]), BACKOFF[1])
        self.assertEqual(reload_delay([0, 200, 400]), BACKOFF[2])
        self.assertEqual(reload_delay([0, 200, 400, 600, 800]), BACKOFF[3])

    def test_quiet_spell_starts_again(self):
        self.assertEqual(reload_delay([0, 200, 400 + STEADY + 1]), BACKOFF[0])
        self.assertEqual(reload_delay([0, 200 + STEADY + 1, 400 + STEADY + 1]), BACKOFF[1])

    def test_crash_loop(self):
        self.assertIsNone(reload_delay([0, 2, 8]))
        self.assertIsNone(reload_delay([0, 1, LOOP_SECONDS]))
        self.assertIsNotNone(reload_delay([0, 1, LOOP_SECONDS + 1]))


class DownloadTest(unittest.TestCase):
    def test_file_names(self):
        self.assertEqual(file_name("../../.bashrc"), "bashrc")
        self.assertEqual(file_name(".bash_aliases"), "bash_aliases")
        self.assertEqual(file_name(". .. .profile"), "profile")
        self.assertEqual(file_name("report\n.pdf"), "report .pdf")
        self.assertEqual(file_name("invoice\u202efdp.exe"), "invoicefdp.exe")
        self.assertEqual(file_name("\u2066photo\u2069\u200b.jpg"), "photo.jpg")
        self.assertEqual(file_name("\U0001f468\u200d\U0001f469\u200d\U0001f467.png"),
                         "\U0001f468\u200d\U0001f469\u200d\U0001f467.png")
        for name in ("", "..", ".", "/", None, "...", "\u202e", ".\u200b.", "\ufeff.\u202c"):
            self.assertEqual(file_name(name), "download", repr(name))
        long = file_name("é" * 300 + ".pdf")
        self.assertTrue(long.endswith(".pdf"))
        self.assertLessEqual(len(long.encode()), 200)


class LinkTest(unittest.TestCase):
    def test_same_site(self):
        mock = "file:///home/me/natter/tests/mock-whatsapp.html"
        self.assertTrue(same_site(mock + "?link", mock))
        self.assertFalse(same_site("file:///etc/passwd", mock))
        self.assertTrue(same_site("http://localhost:8000/other", "http://localhost:8000/"))
        self.assertFalse(same_site("http://localhost:8001/", "http://localhost:8000/"))
        self.assertFalse(same_site("https://localhost:8000/", "http://localhost:8000/"))
        self.assertFalse(same_site("", mock))

    def test_short_links(self):
        self.assertEqual(short_link("whatsapp://send?phone=+44 7700 900123&text=ok"),
                         "https://wa.me/447700900123?text=ok")
        self.assertEqual(short_link("https://api.whatsapp.com/send?phone=15550100"), "https://wa.me/15550100")
        self.assertIsNone(short_link("https://example.com/"))


if __name__ == "__main__":
    unittest.main()
