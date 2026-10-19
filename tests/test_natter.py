"""Natter tests for everything that doesn't need a window: settings, chats, links, the dock menu."""

import json
import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path

from natter.launcher import exec_quote, with_chat_actions, without_chat_actions
from natter.links import chat_link, is_whatsapp, unread_in
from natter.settings import ChatIndex, Settings, phone_number, quiet_at, rank, score, Chat

ENTRY = """[Desktop Entry]
Type=Application
Name=Natter
Exec=/home/me/.local/bin/natter %U
Icon=io.natter.Natter
"""


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class SettingsTest(Temp):
    def test_defaults_and_saving(self):
        settings = Settings(self.dir / "s.json")
        self.assertTrue(settings["run_in_background"])
        seen = []
        settings.connect(lambda k, v: seen.append((k, v)))
        settings["zoom"] = 1.2
        settings["zoom"] = 1.2  # no change, no event
        self.assertEqual(seen, [("zoom", 1.2)])
        self.assertEqual(Settings(self.dir / "s.json")["zoom"], 1.2)

    def test_list_defaults_are_copies(self):
        settings = Settings(self.dir / "s.json")
        settings["accounts"].append({"id": "x"})
        self.assertEqual(len(settings["accounts"]), 1)

    def test_broken_file_falls_back_to_defaults(self):
        path = self.dir / "s.json"
        path.write_text("{not json")
        self.assertEqual(Settings(path)["zoom"], 1.0)

    def test_quiet_hours(self):
        self.assertTrue(quiet_at("22:00", "07:00", time(23, 30)))
        self.assertTrue(quiet_at("22:00", "07:00", time(6, 59)))
        self.assertFalse(quiet_at("22:00", "07:00", time(7, 0)))
        self.assertFalse(quiet_at("22:00", "07:00", time(12, 0)))
        self.assertTrue(quiet_at("13:00", "14:00", time(13, 30)))
        self.assertFalse(quiet_at("13:00", "13:00", time(13, 0)))
        self.assertFalse(quiet_at("nonsense", "07:00", time(3, 0)))
        settings = Settings(self.dir / "s.json")
        settings["quiet_hours"] = True
        self.assertTrue(settings.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))
        settings["quiet_hours"] = False
        self.assertFalse(settings.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))


class ChatsTest(Temp):
    def test_list_order_becomes_recency(self):
        chats = ChatIndex(self.dir / "c.json")
        self.assertTrue(chats.add([("Mum", 0), ("Work", 1), ("Football", 2)], when=1000))
        self.assertEqual([c.name for c in chats.recent()], ["Mum", "Work", "Football"])
        # The same rows again change nothing, so nothing is written.
        self.assertFalse(chats.add([("Mum", 0), ("Work", 1), ("Football", 2)], when=1000))

    def test_scrolling_down_does_not_promote_old_chats(self):
        chats = ChatIndex(self.dir / "c.json")
        chats.add([("Mum", 0), ("Work", 1)], when=1000)
        chats.add([("Old friend", 40)], when=1100)  # seen far down the list, a bit later
        self.assertEqual(chats.recent()[0].name, "Mum")
        self.assertEqual(chats.recent()[-1].name, "Old friend")

    def test_touch_and_persistence(self):
        chats = ChatIndex(self.dir / "c.json")
        chats.add([("Mum", 0), ("Work", 1)], when=1000)
        chats.touch("Work")
        self.assertEqual(ChatIndex(self.dir / "c.json").recent()[0].name, "Work")

    def test_only_names_are_stored(self):
        chats = ChatIndex(self.dir / "c.json")
        chats.add([("Mum", 0)], when=1000)
        self.assertEqual(json.loads((self.dir / "c.json").read_text()), {"Mum": 1000.0})

    def test_ranking(self):
        chats = [Chat("Ana Souza", 10), Chat("Banana Club", 30), Chat("Joana", 20), Chat("Bob", 5)]
        self.assertEqual([c.name for c in rank("ana", chats)][:3], ["Ana Souza", "Banana Club", "Joana"])
        self.assertEqual(rank("", chats)[0].name, "Banana Club")  # newest first
        self.assertEqual([c.name for c in rank("bc", chats)], ["Banana Club"])  # letters in order
        self.assertGreater(score("jo", "Joana"), score("jo", "Ana Souza Jo"))
        self.assertEqual(score("zz", "Ana"), 0)
        self.assertGreater(score("jose", "José"), 0)  # accents fold

    def test_phone_numbers(self):
        self.assertEqual(phone_number("+55 (11) 98765-4321"), "5511987654321")
        self.assertEqual(phone_number("0044 20 7946 0958"), "442079460958")
        self.assertIsNone(phone_number("Mum"))
        self.assertIsNone(phone_number("123"))
        self.assertIsNone(phone_number("+1 555 0100 extra"))


class LinksTest(unittest.TestCase):
    def test_wa_me(self):
        self.assertEqual(chat_link("https://wa.me/5511987654321"), "https://web.whatsapp.com/send?phone=5511987654321")
        self.assertEqual(chat_link("https://wa.me/15550100?text=Hi%20there"),
                         "https://web.whatsapp.com/send?phone=15550100&text=Hi+there")
        self.assertIsNone(chat_link("https://wa.me/message/ABCDEF"))

    def test_whatsapp_scheme_and_api(self):
        self.assertEqual(chat_link("whatsapp://send?phone=+44 7700 900123&text=ok"),
                         "https://web.whatsapp.com/send?phone=447700900123&text=ok")
        self.assertEqual(chat_link("https://api.whatsapp.com/send?phone=15550100"),
                         "https://web.whatsapp.com/send?phone=15550100")
        self.assertIsNone(chat_link("whatsapp://"))
        self.assertIsNone(chat_link("https://example.com/send?phone=1"))
        self.assertIsNone(chat_link("/home/me/photo.jpg"))

    def test_hosts_and_titles(self):
        self.assertTrue(is_whatsapp("https://web.whatsapp.com/"))
        self.assertFalse(is_whatsapp("http://web.whatsapp.com/"))
        self.assertFalse(is_whatsapp("https://web.whatsapp.com.evil.example/"))
        self.assertEqual(unread_in("(12) WhatsApp"), 12)
        self.assertEqual(unread_in("WhatsApp"), 0)
        self.assertEqual(unread_in(""), 0)


class DockMenuTest(unittest.TestCase):
    def test_quoting(self):
        self.assertEqual(exec_quote("Mum"), "Mum")
        self.assertEqual(exec_quote("Ana Souza"), '"Ana Souza"')
        self.assertEqual(exec_quote('Say "hi"'), '"Say \\\\"hi\\\\""')
        self.assertEqual(exec_quote("100% $cash"), '"100%% \\\\$cash"')

    def test_actions_are_added_and_replaced(self):
        once = with_chat_actions(ENTRY, [("main", "Mum"), ("work", "Team Chat")])
        self.assertIn("Actions=chat-0;chat-1;", once)
        self.assertIn("[Desktop Action chat-1]\nName=Team Chat\n"
                      'Exec=/home/me/.local/bin/natter --account work --chat "Team Chat"', once)
        twice = with_chat_actions(once, [("main", "Dad")])
        self.assertIn("Actions=chat-0;", twice)
        self.assertNotIn("Team Chat", twice)
        self.assertEqual(without_chat_actions(twice), ENTRY)

    def test_other_actions_survive(self):
        entry = ENTRY + "Actions=new;\n\n[Desktop Action new]\nName=New\nExec=natter --new\n"
        result = with_chat_actions(entry, [("main", "Mum")])
        self.assertIn("Actions=new;chat-0;", result)
        self.assertIn("[Desktop Action new]", result)
        self.assertEqual(without_chat_actions(result).count("[Desktop Action"), 1)


class SystemTest(unittest.TestCase):
    def test_scope_detection(self):
        from natter.system import in_own_scope, scope_name

        base = Path("/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice")
        self.assertTrue(in_own_scope(base / "app-ada-io.natter.Natter-4242.scope"))  # Ada's launcher
        self.assertTrue(in_own_scope(base / scope_name(4242)))  # our own
        self.assertFalse(in_own_scope(base / "app-ada-com.anthropic.Claude-9571.scope"))  # started from Claude
        self.assertFalse(in_own_scope(base / "vte-spawn-1234.scope"))  # a terminal
        self.assertEqual(scope_name(7), "app-io.natter.Natter-7.scope")


class ClipboardTest(unittest.TestCase):
    def test_what_natter_pastes(self):
        import gi

        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        from natter.clipboard import has_pastable

        def formats(*types):
            return Gdk.ContentFormats.new(list(types))

        self.assertEqual(has_pastable(formats("image/png")), "image")
        self.assertEqual(has_pastable(formats("text/html", "image/png")), "image")  # an image copied from a browser
        self.assertEqual(has_pastable(formats("x-special/gnome-copied-files", "text/uri-list", "text/plain")), "files")
        self.assertIsNone(has_pastable(formats("text/plain", "text/plain;charset=utf-8")))
        self.assertIsNone(has_pastable(formats("text/html", "text/plain")))


class SearchTest(unittest.TestCase):
    def test_ids_round_trip(self):
        from natter.search import result_id, split_id

        self.assertEqual(split_id(result_id("main", "Mum / Dad")), ("main", "Mum / Dad"))
        self.assertEqual(split_id("account-2/Work"), ("account-2", "Work"))


if __name__ == "__main__":
    unittest.main()
