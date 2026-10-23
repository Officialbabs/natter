"""The app's side of the lock (app.py): what waits for the unlock, and what the lock changes about
notifications, the page's tones and downloads. The window's side is in test_lock_warmup.py. No
display is needed: the window, the notifier and the accounts are stood in for."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from natter import app as natter_app
from natter.app import UNLOCK_QUEUE, Natter
from natter.settings import Settings


class Window:
    def __init__(self, active=True, account=None):
        self.active, self.account, self.toasts, self.locks = active, account, [], []

    def is_active(self):
        return self.active

    def get_visible(self):
        return True

    def toast(self, *args, **_kwargs):
        self.toasts.append(args)

    def show_lock(self, locked):
        self.locks.append(locked)


class Notifier:
    def __init__(self):
        self.shown, self.withdrawn = [], []

    def show(self, *args):
        self.shown.append(args)

    def withdraw(self, account_id):
        self.withdrawn.append(account_id)


def fake_app(test, locked=True):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    fake = type("App", (), {})()
    fake.settings = Settings(Path(folder.name) / "settings.json")
    fake.locked = locked
    fake._after_unlock = []
    fake.presented = []
    fake.present_window = lambda token="": fake.presented.append(token or 1) or True
    fake.told = []
    fake.tell = lambda key, title, body="", button=None, account=None: fake.told.append((key, title, body, button))
    fake.notifier = Notifier()
    fake.accounts = []
    fake.calling = fake.in_call = False
    fake.downloaded = set()
    fake.replies = {}  # no reply windows open (notify-ui's, where it has them)
    fake._held_while_locked = lambda run, key="show", token="": Natter._held_while_locked(fake, run, key, token)
    fake._wait_for_unlock = lambda key, run: Natter._wait_for_unlock(fake, key, run)
    return fake


class HeldWhileLockedTest(unittest.TestCase):
    def test_nothing_waits_while_unlocked(self):
        fake = fake_app(self, locked=False)
        self.assertFalse(fake._held_while_locked(lambda: None))
        self.assertEqual((fake._after_unlock, fake.presented), ([], []))

    def test_the_lock_comes_forward_and_the_latest_of_a_kind_waits(self):
        fake = fake_app(self)
        ran = []
        self.assertTrue(fake._held_while_locked(lambda: ran.append("chat A")))
        self.assertTrue(fake._held_while_locked(lambda: ran.append("chat B")))
        self.assertTrue(fake._held_while_locked(lambda: ran.append("file"), "file:/x"))
        self.assertEqual(len(fake.presented), 3)
        self.assertEqual([key for key, _run in fake._after_unlock], ["show", "file:/x"])
        for _key, run in fake._after_unlock:
            run()
        self.assertEqual(ran, ["chat B", "file"])

    def test_a_few_at_most_oldest_dropped(self):
        fake = fake_app(self)
        for n in range(UNLOCK_QUEUE + 2):
            fake._held_while_locked(lambda: None, f"file:{n}")
        self.assertEqual([key for key, _run in fake._after_unlock],
                         [f"file:{n}" for n in range(2, UNLOCK_QUEUE + 2)])

    def test_the_unlock_runs_them_in_turn(self):
        fake = fake_app(self)
        fake.window = Window()
        fake.note_activity = fake._refresh_tray = lambda: None
        fake.check_passcode = lambda passcode, checked: checked(True, "")
        ran = []
        fake._held_while_locked(lambda: ran.append(("chat", fake.locked)))
        fake._held_while_locked(lambda: ran.append(("file", fake.locked)), "file:/x")
        answers = []
        Natter.try_unlock(fake, "1234", lambda ok, message: answers.append(ok))
        self.assertEqual(answers, [True])
        self.assertEqual(ran, [("chat", False), ("file", False)])  # after the unlock, in order
        self.assertEqual(fake._after_unlock, [])
        self.assertEqual(fake.window.locks, [False])

    def test_the_clicks_token_brings_the_passcode_forward(self):
        # On Wayland a window comes forward only with the token of the click that asked for it.
        fake = fake_app(self)
        opened = []
        fake.account_by_id = lambda account_id: SimpleNamespace(click_notification=lambda *a: opened.append(a))
        fake._open_from_notification = lambda *args: Natter._open_from_notification(fake, *args)
        Natter._open_from_notification(fake, "main", "Mum", 7, "token-1")
        self.assertEqual(fake.presented, ["token-1"])
        # After the unlock the click is spent: Natter's own window asks, with no token.
        fake.locked = False
        fake.window = SimpleNamespace(show_account=lambda account: None)
        fake._after_unlock[0][1]()
        self.assertEqual(fake.presented, ["token-1", 1])
        self.assertEqual(opened, [(7, "Mum")])

    def test_present_window_gives_the_token_first(self):
        done = []
        fake = SimpleNamespace(window=SimpleNamespace(set_startup_id=lambda token: done.append(token),
                                                      give_back_shown=lambda: done.append("back"),
                                                      present=lambda: done.append("present")))
        self.assertTrue(Natter.present_window(fake, "token-1"))
        self.assertTrue(Natter.present_window(fake))
        # The page it shows back in it first, if lent to a reply card (loan.py): never an empty page on show.
        self.assertEqual(done, ["token-1", "back", "present", "back", "present"])

    def test_a_wrong_passcode_runs_nothing(self):
        fake = fake_app(self)
        fake.check_passcode = lambda passcode, checked: checked(False, "Wrong passcode")
        ran = []
        fake._held_while_locked(lambda: ran.append(1))
        Natter.try_unlock(fake, "0000", lambda ok, message: None)
        self.assertEqual(ran, [])
        self.assertEqual(len(fake._after_unlock), 1)

    def test_removing_the_lock_forgets_them(self):
        fake = fake_app(self)
        fake.window = None
        fake._attempts = SimpleNamespace(succeeded=lambda: None)
        fake._refresh_tray = lambda: None
        fake._write_menu_chats = lambda force=False: None
        fake._held_while_locked(lambda: None)
        Natter._on_setting(fake, "lock_hash", "")
        self.assertEqual(fake._after_unlock, [])
        self.assertFalse(fake.locked)


class LockedWindowTest(unittest.TestCase):
    def account(self, chat="Mum"):
        return SimpleNamespace(id="main", name="Personal", active_chat=chat, muted=False,
                               view=SimpleNamespace(get_is_muted=lambda: False,
                                                    set_is_muted=lambda muted: None),
                               set_class=lambda *_: None)

    def test_the_chat_on_screen_still_notifies_behind_the_lock(self):
        account = self.account()
        for locked, shown in ((False, 0), (True, 1)):
            fake = fake_app(self, locked=locked)
            fake.accounts = [account]
            fake.window = Window(active=True, account=account)
            Natter._on_notify_message(fake, account, 7, "Mum", "Hello", "1234567@c.us", "")
            self.assertEqual(len(fake.notifier.shown), shown, locked)

    def test_notifications_stay_until_the_unlock(self):
        account = self.account()
        fake = fake_app(self)
        fake.accounts = [account]
        fake.window = Window(active=True, account=account)
        fake._apply_page_sound = lambda: None
        Natter.on_focus_changed(fake, True)
        Natter.on_page_changed(fake)
        self.assertEqual(fake.notifier.withdrawn, [])
        fake.locked = False
        Natter.on_focus_changed(fake, True)
        Natter.on_page_changed(fake)
        self.assertEqual(fake.notifier.withdrawn, ["main", "main"])

    def test_a_locked_window_isnt_in_use_for_do_not_disturb(self):
        muted = []
        account = self.account()
        account.view = SimpleNamespace(get_is_muted=lambda: None, set_is_muted=muted.append)
        for locked in (False, True):
            fake = fake_app(self, locked=locked)
            fake.accounts = [account]
            fake.window = Window(active=True)
            fake.settings["dnd_until"] = -1  # paused until turned back on
            Natter._apply_page_sound(fake)
        self.assertEqual(muted, [False, True])


class DownloadTest(unittest.TestCase):
    PATH = "/home/someone/Downloads/Holiday with Mum.jpg"

    def test_finished_while_locked_names_nothing(self):
        fake = fake_app(self)
        fake.window = Window()
        Natter._on_download(fake, None, self.PATH, True, "")
        (key, title, body, button), = fake.told
        self.assertEqual((title, body), ("Download finished", ""))
        self.assertEqual(button[:2], ("Open", "app.open-file"))
        self.assertEqual(fake.window.toasts, [])
        self.assertIn(self.PATH, fake.downloaded)

    def test_failed_while_locked_names_nothing(self):
        fake = fake_app(self)
        fake.window = Window()
        Natter._on_download(fake, None, self.PATH, False, f"Can't write {self.PATH}")
        (key, title, body, button), = fake.told
        self.assertNotIn("Holiday", title + body)
        self.assertNotIn("Downloads", title + body)

    def test_unlocked_as_before(self):
        fake = fake_app(self, locked=False)
        fake.window = Window()
        Natter._on_download(fake, None, self.PATH, True, "")
        self.assertEqual(fake.window.toasts[0][0], "Saved “Holiday with Mum.jpg”")

    def test_open_waits_for_the_unlock(self):
        fake = fake_app(self)
        fake.window = None
        fake.downloaded.add(self.PATH)
        fake._open_file = lambda path: Natter._open_file(fake, path)
        opened = []
        with mock.patch.object(natter_app, "open_uri", lambda uri, window: opened.append(uri)):
            Natter._open_file(fake, self.PATH)
            self.assertEqual(opened, [])
            self.assertEqual([key for key, _run in fake._after_unlock], [f"file:{self.PATH}"])
            fake.locked = False
            fake._after_unlock[0][1]()
        self.assertEqual(len(opened), 1)

    def test_only_natters_own_downloads(self):
        fake = fake_app(self)
        with mock.patch.object(natter_app, "open_uri", lambda uri, window: self.fail("opened")):
            Natter._open_file(fake, "/etc/passwd")
        self.assertEqual(fake._after_unlock, [])


if __name__ == "__main__":
    unittest.main()
