"""The chat that was open when Natter loads WhatsApp's page again itself opens again in the new
page (account.py _reopen_after_load, _reopen_when_up): after a fresh page (for memory, after a
call), a page ended for its memory (by Natter's guard, the kernel or WebKit), a crash, the
network process lost, a blank or stalled page, and Reload or Try Again. Remembered as the old page
goes, opened once the new page's chat list is up, however long that takes, the window shown or
not; never while locked (it waits for the unlock), never over a chat opened since, and never after
WhatsApp's own reload or once the chat was closed. The log says whether it opened, never which
chat."""

import contextlib
import io
import json
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import WebKit

from natter import account as natter_account
from natter import media
from natter.account import BACKOFF, LOOP_CRASHES, STALL_CHECK, STALL_LIMIT, Account
from natter.app import Natter

Reason = WebKit.WebProcessTerminationReason
TOKEN = "f" * 32
OPEN = "return window.__natter ? await window.__natter.openChat(name) : false"
BLANK = "return document.readyState === 'complete' && !document.getElementById('app')"


class Clock:
    """GLib's timeouts and idles as account.py uses them: kept, run when the test says."""

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self):
        self.timers, self.idles, self.next = {}, [], 0

    def timeout_add_seconds(self, seconds, run, *args):
        self.next += 1
        self.timers[self.next] = (seconds, run, args)
        return self.next

    def timeout_add(self, ms, run, *args):
        return self.timeout_add_seconds(ms / 1000, run, *args)

    def source_remove(self, timer):
        self.timers.pop(timer, None)

    def idle_add(self, run, *args):
        self.idles.append((run, args))
        return 0

    def getenv(self, name):
        return None

    def flush(self):
        while self.idles:
            run, args = self.idles.pop(0)
            run(*args)

    def fire(self, seconds):
        """Run the timers that were due after `seconds`."""
        for timer, (due, run, args) in sorted(self.timers.items()):
            if due == seconds:
                del self.timers[timer]
                run(*args)


class App:
    """Natter as an account's reload paths see it. What waits for the unlock waits as in app.py
    (its own _wait_for_unlock), and runs at the unlock as try_unlock runs it, oldest first."""

    def __init__(self):
        self.locked = False
        self.settings = {"privacy_blur": False, "microphone": True, "camera": True}
        self.ended_by = []  # who ended each page that stops next for its memory (ended_for_memory)
        self._after_unlock = []
        self._wait_for_unlock = types.MethodType(Natter._wait_for_unlock, self)

    def ended_for_memory(self):
        return self.ended_by.pop(0) if self.ended_by else None

    def camera_gate(self):
        return None

    def unlock(self):
        self.locked = False
        waiting, self._after_unlock = self._after_unlock, []
        for _key, run in waiting:
            run()


class Features:
    """WebKit.Settings as _apply_throttle sets it."""

    def __init__(self):
        self.features = {}

    def set_feature_enabled(self, feature, on):
        self.features[feature.get_identifier()] = on


class View:
    def __init__(self):
        self.loaded, self.terminated, self.progress = [], 0, 0.4
        self.settings = Features()

    def load_uri(self, uri):
        self.loaded.append(uri)

    def terminate_web_process(self):
        self.terminated += 1

    def get_settings(self):
        return self.settings

    def get_estimated_load_progress(self):
        return self.progress


class Page:
    """An Account as its reload paths see it: Account's own code, on stand-in parts. The page
    calls it makes are kept (runs), and the bridge's messages come in through its own
    _on_message, as the bridge sends them (bridge.js scan)."""

    calling = False

    def __init__(self, chat="Mum"):
        self.app = App()
        self.id, self.name, self.home = "a1", "Personal", "https://web.whatsapp.com/"
        self.view, self.rtc, self._token = View(), None, TOKEN
        self.chats = SimpleNamespace(touch=lambda name: False, add=lambda rows: False)
        self.audio_sizes = SimpleNamespace(ready=lambda: None, stop=lambda: None)
        self.active_chat, self._reopen, self._reopen_up = "", "", None
        self.ready = self.needs_link = self.removed = self.crashing = self.memory_stopped = False
        self.reloading = False
        self.failed = self.look = self.look_dark = None
        self.fresh_at = self.loaded_at = 0.0
        self._reload_timer, self._page_timer, self._link, self._link_timer = 0, 0, None, 0
        self._crashes, self._memory_ends, self._blank_reloads = [], [], []
        self._progress, self._stall_look = (-1.0, 0.0), 0.0
        self._pending, self._in_page, self._held_actions, self._fresh_held = {}, 0, [], False
        self._fallback, self._picked = {}, None
        self._allowed_at = dict.fromkeys(media.KINDS)
        self._ended_at = dict.fromkeys(media.KINDS)
        self.refused_since_load = dict.fromkeys(media.KINDS, False)
        self._fresh_after_call, self._held_media = set(), []
        self._chats_later = []
        self.recording = False
        self.runs, self.emitted = [], []
        # Up, with `chat` open in it.
        self.up(chat)

    def __getattr__(self, name):
        return types.MethodType(getattr(Account, name), self)

    def _trusted(self, uri=None):
        return True

    def run(self, body, args=None, done=None):
        self.runs.append((body, args, done))

    def emit(self, *signal):
        self.emitted.append(signal)

    def _watch_page(self, *_args):
        pass

    # ---- The bridge in the page ---------------------------------------------------------------

    def says(self, kind, **values):
        message = {"type": kind, "token": TOKEN, **values}
        self._on_message(None, SimpleNamespace(to_json=lambda _indent: json.dumps(message)))

    def up(self, chat=""):
        """The page comes up: its first look says which chat it has open (none, in a new page),
        and that its chat list is up."""
        self.says("active", name=chat)
        self.says("ready", ready=True)

    def commits(self):
        """A new page replaces the one there (WebKit's load-changed, COMMITTED): one Natter loaded,
        or WhatsApp loading itself again."""
        self._on_load_changed(None, WebKit.LoadEvent.COMMITTED)

    def arrives(self, chat=""):
        """A new page replaces the one there and comes up."""
        self.commits()
        self.up(chat)

    def opens(self):
        """The chats the page was asked to open (the bridge's openChat), oldest first."""
        return [args["name"] for body, args, _done in self.runs if body == OPEN]

    def answer(self, body, value):
        """The page answers its latest call of `body`."""
        for run_body, _args, done in reversed(self.runs):
            if run_body == body:
                done(value)
                return
        raise AssertionError("no such call")


class ReopenTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patch = mock.patch.object(natter_account, "GLib", self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.log = io.StringIO()
        redirect = contextlib.redirect_stdout(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def assertReopens(self, page, chats=("Mum",)):
        """Nothing opens until the new page is up; then the chat that was open, once."""
        self.assertEqual(page.opens(), [])
        page.commits()
        page.says("active", name="")  # the new page's first look: no chat open
        self.assertEqual(page.opens(), [], "not before the chat list is up")
        page.says("ready", ready=True)
        self.assertEqual(page.opens(), list(chats))

    # ---- Every reload Natter makes itself ------------------------------------------------------

    def test_a_fresh_page(self):
        # For memory while hidden (app.py _fresh_pages_for_memory), after a call that left a lot
        # behind, or for a stale grant (stop_media): the window shown or not.
        page = Page()
        page.fresh_page()
        self.assertEqual((page.view.terminated, page.view.loaded), (1, [page.home]))
        self.assertReopens(page)

    def test_a_fresh_page_after_a_call(self):
        page = Page()
        page.app.settings["camera"] = False
        page._fresh_after_call = {"camera"}  # switched off during the call
        page._on_page_call(False)
        self.assertEqual(page.view.loaded, [page.home])
        self.assertReopens(page)

    def test_a_fresh_page_held_for_a_reply(self):
        # Asked for while a reply is in the page, it waits for the reply's answer: the chat open
        # when it goes ahead is the one that opens again, the reply's.
        page = Page()
        page._in_page = 1
        page.fresh_page()
        self.assertEqual(page.view.loaded, [])
        page.says("active", name="Dad")  # the reply's chat
        page._in_page = 0
        page._release_held()
        self.assertEqual(page.view.loaded, [page.home])
        self.assertReopens(page, ["Dad"])

    def test_a_page_ended_for_its_memory(self):
        for by in ("the guard", "the kernel"):
            with self.subTest(by=by):
                page = Page()
                page.app.ended_by = [by]
                page._on_crash(None, Reason.CRASHED)
                self.assertIn(f"ended for its memory ({by}); a fresh page", self.log.getvalue())
                self.clock.fire(1)
                self.assertEqual(page.view.loaded, [page.home])
                self.assertReopens(page)

    def test_webkits_own_memory_kill(self):
        page = Page()
        page._on_crash(None, Reason.EXCEEDED_MEMORY_LIMIT)
        self.clock.fire(1)
        self.assertEqual(page.view.loaded, [page.home])
        self.assertReopens(page)

    def test_a_crash(self):
        page = Page()
        page._on_crash(None, Reason.CRASHED)  # nobody claims it: a crash, reloaded after its backoff
        self.clock.fire(BACKOFF[0])
        self.assertEqual(page.view.loaded, [page.home])
        self.assertReopens(page)

    def test_the_network_process_lost_after_a_long_backoff(self):
        # Before, the chat was asked for a second after the loss and given up 20 s later: a
        # reload 30 s later came back on no chat.
        page = Page()
        now = natter_account.time.monotonic()
        page._crashes = [now - 400, now - 200]  # the third in a row within ten minutes: 30 s
        page.network_lost()
        self.assertEqual(page.view.terminated, 1)
        for seconds in (1, 5, 20):
            self.clock.fire(seconds)
        self.assertEqual(page.view.loaded, [])
        self.clock.fire(BACKOFF[2])
        self.assertEqual(page.view.loaded, [page.home])
        self.assertReopens(page)

    def test_reload(self):
        # The Reload button, the palette's Reload WhatsApp, back online, a switch turned on again.
        page = Page()
        page.reload()
        self.assertEqual(page.view.loaded, [page.home])
        self.assertReopens(page)

    def test_try_again_after_a_crash_loop(self):
        page = Page()
        for _ in range(LOOP_CRASHES):
            page._on_crash(None, Reason.CRASHED)
        self.assertTrue(page.crashing)
        self.assertEqual(page.view.loaded, [])
        page.reload()
        self.assertReopens(page)

    def test_try_again_after_memory_twice(self):
        page = Page()
        page.app.ended_by = ["the guard", "the guard"]
        page._on_crash(None, Reason.CRASHED)
        page._on_crash(None, Reason.CRASHED)  # before its fresh page: it stays down
        self.assertTrue(page.memory_stopped)
        page.reload()
        self.assertReopens(page)

    def test_a_page_that_came_back_empty(self):
        page = Page()
        page.fresh_page()
        page.commits()
        page._check_blank()
        page.answer(BLANK, True)
        self.assertEqual(page.view.loaded, [page.home, page.home])
        self.assertReopens(page)

    def test_a_page_that_stopped_loading(self):
        page = Page()
        page.reload()
        page.commits()
        page.says("active", name="")  # it got as far as the bridge's first look, no further
        now = [0.0]
        with mock.patch.object(natter_account.time, "monotonic", lambda: now[0]):
            for now[0] in (0.0, STALL_CHECK, STALL_LIMIT + 5):
                page._page_stalled()
        self.assertIn("stopped loading", self.log.getvalue())
        self.assertEqual(page.view.loaded, [page.home, page.home])
        self.assertReopens(page)

    def test_reloads_one_after_another(self):
        # A fresh page that crashed before it came up, then came back empty: still that chat.
        page = Page()
        page.fresh_page()
        page.commits()
        page.says("active", name="")
        page._on_crash(None, Reason.CRASHED)
        self.clock.fire(BACKOFF[0])
        page.commits()
        page._reload_blank("came back empty")
        self.assertEqual(len(page.view.loaded), 3)
        self.assertReopens(page)

    def test_whatsapp_loading_itself_again_before_its_up(self):
        # Natter's reload, and WhatsApp's own on top of it as it starts: still the chat open
        # before Natter's.
        page = Page()
        page.fresh_page()
        page.commits()
        page.says("active", name="")
        page.commits()  # WhatsApp loaded itself again
        self.assertReopens(page)

    # ---- Once, when it's up -------------------------------------------------------------------

    def test_once(self):
        page = Page()
        page.fresh_page()
        page.arrives()
        page.says("ready", ready=False)  # WhatsApp's chat list goes and comes back
        page.says("ready", ready=True)
        page.reload()  # and later a reload with no chat open
        page.arrives()
        self.assertEqual(page.opens(), ["Mum"])

    def test_the_log_says_whether_never_which(self):
        page = Page()
        page.fresh_page()
        page.arrives()
        page.answer(OPEN, True)
        page.says("active", name="Mum")  # open again
        page.reload()
        page.arrives()
        page.answer(OPEN, False)  # gone from the chat list since, say
        self.assertEqual(page.opens(), ["Mum", "Mum"])
        log = self.log.getvalue()
        self.assertIn("natter: Personal: back on the chat that was open\n", log)
        self.assertIn("natter: Personal: the chat that was open didn't open again\n", log)
        self.assertNotIn("Mum", log)

    # ---- When it doesn't ----------------------------------------------------------------------

    def test_whatsapps_own_reload(self):
        page = Page()
        page.arrives()  # WhatsApp loaded itself again
        self.assertEqual(page.opens(), [])

    def test_whatsapps_own_reload_that_came_back_empty(self):
        page = Page()
        page.commits()
        page._reload_blank("came back empty")  # the blank page never said anything
        page.arrives()
        self.assertEqual(page.view.loaded, [page.home])
        self.assertEqual(page.opens(), [])

    def test_a_chat_closed_before_the_reload(self):
        page = Page()
        page.says("active", name="")
        page.fresh_page()
        page.arrives()
        self.assertEqual(page.opens(), [])

    def test_no_chat_ever_open(self):
        page = Page(chat="")
        page.reload()
        page.arrives()
        self.assertEqual(page.opens(), [])

    def test_a_chat_asked_for_meanwhile(self):
        # Desktop search, the open-chat action, files sent to a chat: open_chat waits for the
        # page, and that chat is the one that opens.
        page = Page()
        page.fresh_page()
        page.open_chat("Dad")
        page.arrives()
        self.assertEqual(page.opens(), ["Dad"])

    def test_a_notification_clicked_meanwhile(self):
        page = Page()
        page.fresh_page()
        page.click_notification(4, "Dad")
        page.arrives()
        self.assertEqual(page.opens(), [])
        self.assertTrue(any("clickNotification" in body for body, _a, _d in page.runs))

    def test_a_link_opened_meanwhile(self):
        page = Page()
        page.fresh_page()
        self.assertTrue(page.open_link("https://wa.me/15550100"))
        self.assertEqual(page.view.loaded[-1], natter_account.WHATSAPP_URL + "send?phone=15550100")
        page.arrives()
        self.assertEqual(page.opens(), [])

    def test_a_chat_opened_in_the_old_page_meanwhile(self):
        # Reload loads over the page that's there: until the new one replaces it, it's still used.
        page = Page()
        page.reload()
        page.says("active", name="Dad")
        page.arrives()
        self.assertEqual(page.opens(), [])

    def test_a_chat_already_open_when_its_up(self):
        page = Page()
        page.reload()
        page.arrives("Dad")
        self.assertEqual(page.opens(), [])
        page.says("active", name="")
        page.says("ready", ready=False)
        page.says("ready", ready=True)
        self.assertEqual(page.opens(), [], "given up for good")

    def test_open_again_by_itself(self):
        page = Page()
        page.reload()
        page.arrives("Mum")
        self.assertEqual(page.opens(), [])
        page.says("active", name="")  # closed in the page
        page.says("ready", ready=False)
        page.says("ready", ready=True)
        self.assertEqual(page.opens(), [])

    def test_logged_out(self):
        page = Page()
        page.fresh_page()
        page.commits()
        page.says("state", value="needs-link")
        page.says("state", value="chats")  # linked again
        page.up()
        self.assertEqual(page.opens(), [])

    def test_an_account_removed(self):
        page = Page()
        page.fresh_page()
        page.removed = True
        page.arrives()
        self.assertEqual(page.opens(), [])

    # ---- Locked: after the unlock -------------------------------------------------------------

    def test_not_while_locked(self):
        page = Page()
        page.app.locked = True  # locked when hidden, say, with the guard's fresh page
        page.fresh_page()
        page.arrives()
        self.clock.flush()
        self.assertEqual(page.opens(), [])
        page.app.unlock()
        self.assertEqual(page.opens(), [], "after what else waited for the unlock")
        self.clock.flush()
        self.assertEqual(page.opens(), ["Mum"])

    def test_locked_again_before_it_ran(self):
        page = Page()
        page.app.locked = True
        page.fresh_page()
        page.arrives()
        page.app.unlock()
        page.app.locked = True  # locked again before it ran
        self.clock.flush()
        self.assertEqual(page.opens(), [])
        page.app.unlock()
        self.clock.flush()
        self.assertEqual(page.opens(), ["Mum"])

    def test_once_after_the_unlock_whatever_loaded_meanwhile(self):
        page = Page()
        page.app.locked = True
        page.fresh_page()
        page.arrives()
        page._on_crash(None, Reason.CRASHED)  # under the lock, the new page crashed
        self.clock.fire(BACKOFF[0])
        page.arrives()
        page.reload()  # and again, Reload
        page.arrives()
        self.assertEqual(len(page.app._after_unlock), 1)
        page.app.unlock()
        self.clock.flush()
        self.assertEqual(page.opens(), ["Mum"])

    def test_whatsapps_own_reload_while_it_waits_for_the_unlock(self):
        # The page came up locked, and WhatsApp loaded itself again under the lock: as after any
        # reload of WhatsApp's own, no chat opens.
        page = Page()
        page.app.locked = True
        page.fresh_page()
        page.arrives()
        page.arrives()  # WhatsApp loaded itself again
        page.app.unlock()
        self.clock.flush()
        self.assertEqual(page.opens(), [])
        page.says("ready", ready=False)
        page.says("ready", ready=True)
        self.assertEqual(page.opens(), [], "dropped for good")

    def test_whatsapps_own_reload_after_its_turn_at_the_unlock_was_lost(self):
        # More waited for the unlock than app.py keeps (UNLOCK_QUEUE), and its turn was dropped:
        # it doesn't come back with a reload of WhatsApp's own later.
        page = Page()
        page.app.locked = True
        page.fresh_page()
        page.arrives()
        page.app._after_unlock = []  # dropped, oldest first
        page.app.unlock()
        self.clock.flush()
        page.arrives()  # WhatsApp loaded itself again
        self.assertEqual(page.opens(), [])

    def test_a_notification_clicked_while_locked_wins(self):
        page = Page()
        page.app.locked = True
        page.fresh_page()
        page.arrives()
        # Clicked while locked: it waits for the unlock (app._held_while_locked), after this.
        page.app._wait_for_unlock("show", lambda: page.click_notification(4, "Dad"))
        page.app.unlock()
        self.clock.flush()
        self.assertEqual(page.opens(), [])
        self.assertTrue(any("clickNotification" in body for body, _a, _d in page.runs))

    def test_not_up_at_the_unlock(self):
        page = Page()
        page.app.locked = True
        page.fresh_page()
        page.arrives()
        page.reload()  # loading again when the unlock comes
        page.app.unlock()
        self.clock.flush()
        self.assertEqual(page.opens(), [])
        self.assertReopens(page)

    # ---- A reply in the page goes first -------------------------------------------------------

    def test_after_a_reply_in_the_page(self):
        page = Page()
        page.fresh_page()
        page._in_page = 1  # a reply the page has and hasn't answered (send_reply)
        page.arrives()
        self.assertEqual(page.opens(), [])
        page._in_page = 0
        page._release_held()
        self.assertEqual(page.opens(), ["Mum"])

    def test_not_over_the_chat_a_reply_opened(self):
        page = Page()
        page.fresh_page()
        page._in_page = 1
        page.arrives()
        page.says("active", name="Dad")  # the reply's chat
        page._in_page = 0
        page._release_held()
        self.assertEqual(page.opens(), [])

    def test_whatsapps_own_reload_while_a_reply_holds_it(self):
        page = Page()
        page.fresh_page()
        page._in_page = 1
        page.arrives()
        page.arrives()  # WhatsApp loaded itself again under the reply
        page._in_page = 0
        page._release_held()
        self.assertEqual(page.opens(), [])


if __name__ == "__main__":
    unittest.main()
