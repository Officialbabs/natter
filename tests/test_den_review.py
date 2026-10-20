"""What the review of the Natter Den rename and polish found (app.py, window.py)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from natter import NAME


class AutostartNameTest(unittest.TestCase):
    """An autostart entry from before the rename gets the new name, and keeps the rest."""

    def test_only_the_name_line_changes(self):
        from natter.app import Natter

        with tempfile.TemporaryDirectory() as folder:
            entry = Path(folder) / "io.github.Officialbabs.Natter.desktop"
            entry.write_text("[Desktop Entry]\nType=Application\nName=Natter\nExec=natter --background\n"
                             "X-GNOME-Autostart-enabled=false\n")
            with mock.patch("natter.app.AUTOSTART", entry), mock.patch("natter.app.in_sandbox", return_value=False):
                Natter._rename_autostart(object())
            text = entry.read_text()
        self.assertIn(f"Name={NAME}\n", text)
        self.assertIn("X-GNOME-Autostart-enabled=false", text)  # what the desktop's own settings wrote
        self.assertNotIn("Name=Natter\n", text)

    def test_no_entry_is_left_alone(self):
        from natter.app import Natter

        with tempfile.TemporaryDirectory() as folder:
            entry = Path(folder) / "missing.desktop"
            with mock.patch("natter.app.AUTOSTART", entry), mock.patch("natter.app.in_sandbox", return_value=False):
                Natter._rename_autostart(object())
            self.assertFalse(entry.exists())


class PasscodeTriesTest(unittest.TestCase):
    """Wrong tries at an old passcode don't count against a new one."""

    def test_a_new_passcode_starts_clean(self):
        from natter.app import Natter

        fake = type("App", (), {})()
        fake.locked, fake.window = False, None
        fake.settings = {}
        fake._attempts = mock.Mock()
        fake._refresh_tray = lambda: None
        fake._write_menu_chats = lambda force=False: None
        Natter._on_setting(fake, "lock_hash", "pbkdf2-sha256$1$00$00")
        fake._attempts.succeeded.assert_called_once()


class SubtitleTest(unittest.TestCase):
    """With several accounts, the subtitle is about the account on screen."""

    def window(self, accounts, shown):
        from natter.window import Window

        fake = type("Window", (), {})()
        fake.app = type("App", (), {"accounts": accounts})()
        fake.title = mock.Mock()
        type(fake).account = property(lambda _self: shown)
        return fake, Window

    @staticmethod
    def account(needs_link=False, reloading=False, unread=0):
        return type("Account", (), {"needs_link": needs_link, "reloading": reloading, "unread": unread})()

    def test_several_accounts_describe_the_shown_one(self):
        linked, unlinked = self.account(unread=2), self.account(needs_link=True)
        fake, Window = self.window([linked, unlinked], shown=linked)
        Window._show_subtitle(fake)
        fake.title.set_subtitle.assert_called_with("2 unread")

    def test_one_account_as_before(self):
        only = self.account(needs_link=True)
        fake, Window = self.window([only], shown=only)
        Window._show_subtitle(fake)
        fake.title.set_subtitle.assert_called_with("Needs linking")


if __name__ == "__main__":
    unittest.main()
