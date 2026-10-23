"""Replies from notifications, the page's side: Account.send_reply (account.py) and the bridge's
reply (bridge.js). One reply is one send through WhatsApp's own page, or none: never begun while
locked or in a call, one at a time, never retried, the words only ever as arguments, the log
without names, ids or words, and nothing else acting on the page while it runs. The bridge's
whole run was tried against real Lexical in WebKitGTK (a lab); here, what can be pinned without
a page: its rules, and its text comparison run in JavaScriptCore, and what it takes for open in
WhatsApp's drawers run there on the mock page's own markup."""

import contextlib
import io
import json
import re
import types
import unittest
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("JavaScriptCore", "6.0")
from gi.repository import JavaScriptCore

from natter import account as natter_account
from natter.account import (BRIDGE, CHAT_ID, PAGE_REPLY_RESULTS, REPLY_BUDGET_MS, REPLY_GUARD, REPLY_LET_GO,
                            REPLY_LIMIT, REPLY_QUEUE_WAIT, REPLY_RESULTS, REPLY_SCRIPT, Account, reply_answer,
                            reply_length, reply_text)

GOOD = "4917612345678@c.us"
OTHER = "120363041234567890@g.us"
SECTION = BRIDGE[BRIDGE.index("// ---- Replying from a notification"):BRIDGE.index("// ---- Classes Natter's style")]
MOCK = Path(__file__).with_name("mock-whatsapp.html").read_text()


class Clock:
    """GLib's timeouts and idles as account.py uses them: kept, run when the test says."""

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self):
        self.timers, self.idles, self.next = {}, [], 0

    def _add(self, seconds, run, *args):
        self.next += 1
        self.timers[self.next] = (seconds, run, args)
        return self.next

    def timeout_add_seconds(self, seconds, run, *args):
        return self._add(seconds, run, *args)

    def timeout_add(self, ms, run, *args):
        return self._add(ms / 1000, run, *args)

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


class Settings:
    """WebKit.Settings as _features sets it: which features are on."""

    def __init__(self):
        self.features = {}

    def set_feature_enabled(self, feature, on):
        self.features[feature.get_identifier()] = on

    def throttled(self):
        return self.features.get("HiddenPageDOMTimerThrottling")


class View:
    def __init__(self):
        self.settings, self.mapped, self.terminated = Settings(), False, 0

    def get_settings(self):
        return self.settings

    def get_mapped(self):
        return self.mapped

    def terminate_web_process(self):
        self.terminated += 1


class Page:
    """An Account as the reply path sees it: Account's own code, on stand-in parts. The page
    calls it makes are kept (runs) and answered by the test."""

    replying = Account.replying

    def __init__(self):
        self.app = SimpleNamespace(locked=False, reclaimer=SimpleNamespace(stops=0))
        self.app.reclaimer.stop = lambda: setattr(self.app.reclaimer, "stops", self.app.reclaimer.stops + 1)
        self.view, self.rtc, self.in_call, self.trusted = View(), None, False, True
        self.removed = self.needs_link = self.crashing = False
        self.ready = True
        self.active_chat, self.home, self.fresh_at = "", "https://web.whatsapp.com/", 0.0
        self._pending, self._replies, self._reply, self._in_page = {}, deque(), None, 0
        self._held_actions, self._fresh_held = [], False
        self._link, self._fallback, self._picked, self._fresh_after_call = None, {}, None, set()
        self.runs, self.loaded, self.emitted = [], [], []

    calling = property(lambda self: self.in_call)

    def __getattr__(self, name):
        return types.MethodType(getattr(Account, name), self)

    def _trusted(self, uri=None):
        return self.trusted

    def run(self, body, args=None, done=None):
        self.runs.append((body, args, done))

    def _load(self, uri):
        self.loaded.append(uri)

    def emit(self, *signal):
        self.emitted.append(signal)

    def answer(self, value, index=-1):
        """The page answers one of its calls."""
        body, args, done = self.runs[index]
        done(value)


def sent(why=""):
    return {"result": "sent", "why": why}


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for patch in (mock.patch.object(natter_account, "GLib", self.clock),):
            patch.start()
            self.addCleanup(patch.stop)
        self.log = io.StringIO()
        redirect = contextlib.redirect_stdout(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def reply(self, page, text="On my way", chat=GOOD, page_id=7):
        results = []
        page.send_reply(page_id, chat, text, results.append)
        return results

    def replies(self, page):
        return [run for run in page.runs if run[0] == REPLY_SCRIPT]

    # ---- What never reaches the page --------------------------------------------------------

    def test_what_isnt_a_message_from_a_chat_never_reaches_the_page(self):
        for page_id, chat in ((0, GOOD), (-3, GOOD), (True, GOOD), (2**31, GOOD), ("7", GOOD), (7, ""),
                              (7, "status@broadcast"), (7, "1203@newsletter"), (7, "call-accepted-elsewhere:123@c.us"),
                              (7, "false_4917612345678@c.us_3EB0"), (7, "Mum"), (7, None), (7, GOOD + " ")):
            page = Page()
            results = self.reply(page, page_id=page_id, chat=chat)
            self.clock.flush()
            self.assertEqual(results, ["gone"], (page_id, chat))
            self.assertEqual(page.runs, [])

    def test_words_that_cant_go(self):
        for text in ("", "  \n\t ", None, 42, "x" * (REPLY_LIMIT + 1), "\U0001F44D" * (REPLY_LIMIT // 2 + 1)):
            page = Page()
            results = self.reply(page, text=text)
            self.clock.flush()
            self.assertEqual(results, ["too-long"], repr(text)[:20])
            self.assertEqual(page.runs, [])

    def test_length_counts_as_the_page_does(self):
        # String.length in the page: an emoji outside the BMP is two units.
        self.assertEqual(reply_length("\U0001F44D"), 2)
        self.assertEqual(reply_length("a\u00e9\u4e2d"), 3)
        page = Page()
        self.assertEqual(self.reply(page, text="\U0001F44D" * (REPLY_LIMIT // 2)), [])
        self.assertEqual(len(self.replies(page)), 1)

    def test_line_breaks_go_as_newlines(self):
        self.assertEqual(reply_text("a\r\nb\rc\nd"), "a\nb\nc\nd")
        page = Page()
        self.reply(page, text="one\r\ntwo")
        self.assertEqual(self.replies(page)[0][1]["request"]["text"], "one\ntwo")

    def test_locked_or_calling_or_removed(self):
        for attribute, result in (("locked", "locked"), ("calling", "calling"), ("removed", "removed")):
            page = Page()
            if attribute == "locked":
                page.app.locked = True
            elif attribute == "calling":
                page.in_call = True
            else:
                page.removed = True
            results = self.reply(page)
            self.assertEqual(results, [], "answered later, never inside send_reply")
            self.clock.flush()
            self.assertEqual(results, [result])
            self.assertEqual(page.runs, [])

    def test_the_lock_checked_again_when_the_page_would_be_touched(self):
        # Waiting for WhatsApp's chat list (up to 20 s), the lock came on.
        page = Page()
        page.ready = False
        results = self.reply(page)
        page.app.locked = True
        page.ready = True
        page._run_pending()
        self.clock.flush()
        self.assertEqual(results, ["locked"])
        self.assertEqual(page.runs, [])

    def test_a_call_checked_again_for_a_reply_that_waited_its_turn(self):
        page = Page()
        first, second = self.reply(page, "first"), self.reply(page, "second")
        self.assertEqual(len(self.replies(page)), 1)
        page.in_call = True
        page.answer(sent())
        self.clock.flush()
        self.assertEqual((first, second), (["sent"], ["calling"]))
        self.assertEqual(len(self.replies(page)), 1)

    def test_the_page_not_up_in_time(self):
        page = Page()
        page.ready = False
        results = self.reply(page)
        self.clock.fire(natter_account.READY_WAIT)
        self.clock.flush()
        self.assertEqual(results, ["not-ready"])
        page = Page()
        page.ready, page.needs_link = False, True
        results = self.reply(page)
        self.clock.flush()
        self.assertEqual(results, ["needs-link"])
        self.assertEqual(page.runs, [])

    def test_a_page_somewhere_else_isnt_touched(self):
        page = Page()
        page.trusted = False
        results = self.reply(page)
        self.clock.flush()
        self.assertEqual(results, ["not-ready"])
        self.assertEqual(page.runs, [])

    # ---- In the page -------------------------------------------------------------------------

    def test_the_words_go_as_arguments(self):
        page = Page()
        page.view.mapped = True
        self.reply(page, text='"); alert(1); ("')
        body, args, _done = self.replies(page)[0]
        self.assertEqual(body, REPLY_SCRIPT)
        self.assertNotIn("alert", body)
        self.assertEqual(args, {"request": {"pageId": 7, "chatId": GOOD, "text": '"); alert(1); ("',
                                            "budgetMs": REPLY_BUDGET_MS, "visible": True}})
        json.dumps(args)  # as run() sends them

    def test_sent(self):
        page = Page()
        results = self.reply(page)
        self.assertEqual(page.replying, 1)
        page.answer(sent())
        self.assertEqual(results, [], "answered on an idle")
        self.clock.flush()
        self.assertEqual(results, ["sent"])
        self.assertEqual(page.replying, 0)

    def test_every_answer_the_bridge_gives_goes_through(self):
        for result in sorted(PAGE_REPLY_RESULTS):
            page = Page()
            results = self.reply(page)
            page.answer({"result": result, "why": "no-ids"})
            self.clock.flush()
            self.assertEqual(results, [result])

    def test_no_answer_or_an_odd_one_may_have_sent(self):
        for value in (None, "sent", {"result": "SENT"}, {"result": "unknown"}, {"result": "locked"}, {}, [1]):
            page = Page()
            results = self.reply(page)
            page.answer(value)
            self.clock.flush()
            self.assertEqual(results, ["unknown"], value)
        self.assertEqual(reply_answer({"result": "busy", "why": "x" * 40}), ("busy", ""))
        self.assertEqual(reply_answer({"result": "busy", "why": "Mum's chat"}), ("busy", ""))

    def test_throttling_off_while_the_page_has_it(self):
        page = Page()
        self.reply(page)
        self.assertIs(page.view.settings.throttled(), False)
        self.assertEqual(page.app.reclaimer.stops, 1)
        page.answer(sent())
        self.assertIs(page.view.settings.throttled(), True)

    def test_a_call_and_a_reply_dont_turn_it_on_under_each_other(self):
        page = Page()
        self.reply(page)
        page.in_call = True
        page._apply_throttle()
        page.answer(sent())
        self.assertIs(page.view.settings.throttled(), False)  # the call goes on
        page.in_call = False
        self.reply(page, "again")
        page.in_call = True
        page._on_page_call(True)
        page.in_call = False
        page._on_page_call(False)  # the call ended mid-reply
        self.assertIs(page.view.settings.throttled(), False)
        page.answer(sent())
        self.assertIs(page.view.settings.throttled(), True)

    def test_one_at_a_time_in_the_order_asked(self):
        page = Page()
        first, second = self.reply(page, "first"), self.reply(page, "second", chat=OTHER)
        self.assertEqual([run[1]["request"]["text"] for run in self.replies(page)], ["first"])
        page.answer(sent())
        self.assertEqual([run[1]["request"]["text"] for run in self.replies(page)], ["first", "second"])
        page.answer(sent())
        self.clock.flush()
        self.assertEqual((first, second), (["sent"], ["sent"]))

    def test_the_same_reply_asked_twice_is_sent_once(self):
        page = Page()
        first, again = self.reply(page, "ok"), self.reply(page, "ok")
        self.clock.flush()
        self.assertEqual(again, ["busy"])
        page.answer(sent())
        self.clock.flush()
        self.assertEqual(first, ["sent"])
        self.assertEqual(len(self.replies(page)), 1)
        self.assertEqual(self.reply(page, "ok"), [], "a new act once the first is over")
        self.assertEqual(len(self.replies(page)), 2)

    def test_waiting_too_long_behind_another(self):
        page = Page()
        first, second = self.reply(page, "first"), self.reply(page, "second")
        self.clock.fire(REPLY_QUEUE_WAIT)
        self.clock.flush()
        self.assertEqual(second, ["busy"])
        page.answer(sent())
        self.clock.flush()
        self.assertEqual(first, ["sent"])
        self.assertEqual(len(self.replies(page)), 1)

    def test_no_answer_in_time(self):
        page = Page()
        results = self.reply(page)
        held = []
        page.open_chat("Mum", held.append)
        self.clock.fire(REPLY_GUARD)
        self.clock.flush()
        self.assertEqual(results, ["unknown"])
        # Still the page's: nothing else switches chats under it until it answers.
        self.assertEqual(page.replying, 1)
        self.assertEqual(len(page.runs), 1)
        page.answer(sent())
        self.clock.flush()
        self.assertEqual(results, ["unknown"], "said once")
        self.assertIn("natter: reply: sent, answered after the wait ran out", self.log.getvalue())
        self.assertEqual(page.replying, 0)
        self.assertEqual(len(page.runs), 2)  # the chat it held opens now

    def test_a_page_that_never_answers_is_let_go(self):
        page = Page()
        results = self.reply(page)
        held = []
        page.open_chat("Mum", held.append)
        self.clock.fire(REPLY_GUARD)
        self.clock.fire(REPLY_LET_GO - REPLY_GUARD)
        self.clock.flush()
        self.assertEqual(results, ["unknown"])
        self.assertEqual(page.replying, 0)
        self.assertIn("openChat(name)", page.runs[-1][0])  # what it held goes now
        page.answer(sent(), index=0)  # far too late: changes nothing
        self.clock.flush()
        self.assertEqual((results, page.replying), (["unknown"], 0))
        self.assertIn("answered after the wait ran out", self.log.getvalue())

    def test_the_next_reply_goes_once_the_page_is_let_go(self):
        page = Page()
        self.reply(page, "first")
        second = self.reply(page, "second")
        self.clock.fire(REPLY_GUARD)
        self.assertEqual(len(self.replies(page)), 1)  # not while the page may still be at the first
        self.clock.fire(REPLY_LET_GO - REPLY_GUARD)
        self.assertEqual(len(self.replies(page)), 2)
        page.answer(sent(), index=0)  # the first, late: the second is still the page's
        self.assertEqual(page.replying, 1)
        page.answer(sent())
        self.clock.flush()
        self.assertEqual((second, page.replying), (["sent"], 0))

    def test_the_log_never_says_who_or_what(self):
        page = Page()
        self.reply(page, text="Secret words")
        page.answer({"result": "mismatch", "why": "no-ids"})
        self.clock.flush()
        self.assertEqual(self.log.getvalue(), "natter: reply: mismatch (no-ids)\n")
        self.assertNotIn(GOOD, self.log.getvalue())

    # ---- Nothing else acts on the page meanwhile ---------------------------------------------

    def test_page_actions_wait_for_the_reply(self):
        page = Page()
        self.reply(page)
        page.open_chat("Mum")
        page.click_notification(9, "Mum")
        self.assertTrue(page.open_link("https://wa.me/15550100"))
        page.attach([Path("/nonexistent/a.png")], None, "paste")
        self.assertEqual(len(page.runs), 1)
        page.answer(sent())
        bodies = [body for body, _args, _done in page.runs[1:]]
        self.assertEqual(len(bodies), 4)
        self.assertIn("openChat(name)", bodies[0])
        self.assertIn("clickNotification(id)", bodies[1])
        self.assertIn("openLink(href)", bodies[2])
        self.assertIn("pick()", bodies[3])

    def test_a_held_action_still_waits_for_the_next_reply(self):
        page = Page()
        self.reply(page, "first")
        self.reply(page, "second")
        page.open_chat("Mum")
        page.answer(sent())  # the second reply starts at once; the chat waits for it too
        self.assertEqual([body == REPLY_SCRIPT for body, _a, _d in page.runs], [True, True])
        page.answer(sent())
        self.assertIn("openChat(name)", page.runs[-1][0])

    def test_a_fresh_page_waits_for_the_reply(self):
        page = Page()
        self.reply(page)
        page.fresh_page()
        self.assertEqual((page.view.terminated, page.loaded), (0, []))
        page.answer(sent())
        self.assertEqual((page.view.terminated, page.loaded), (1, [page.home]))

    def test_the_account_going_answers_what_waits(self):
        page = Page()
        page.ready = False
        first, second = self.reply(page, "first"), self.reply(page, "second")
        page.removed = True
        page._drop_replies()
        page._drop_pending()
        self.clock.flush()
        self.assertEqual((first, second), (["removed"], ["removed"]))
        self.assertEqual(page.runs, [])

    def test_nothing_held_without_a_reply(self):
        page = Page()
        page.open_chat("Mum")
        page.fresh_page()
        self.assertEqual(len(page.runs), 1)
        self.assertEqual(page.view.terminated, 1)

    def test_the_held_list_is_bounded(self):
        page = Page()
        self.reply(page)
        for n in range(20):
            page.open_chat(f"Chat {n}")
        self.assertEqual(len(page._held_actions), natter_account.REPLY_HELD)


class BridgeRulesTest(unittest.TestCase):
    """What the bridge's reply may and may not do, read from its source."""

    def test_one_press_and_no_keys(self):
        self.assertEqual(SECTION.count(".click("), 1)
        self.assertEqual(SECTION.count("clickNotification(pageId)"), 1)  # WhatsApp's own handler, once
        for banned in ("KeyboardEvent", "keydown", '"Enter"', "'Enter'", "setInterval", "quickReply",
                       "sendTextMsgToChat", "Store", "require(", "while (", "press(", "openChat(", "setSearch(",
                       "dispatchEvent", "localStorage", "sessionStorage", "indexedDB", "__react"):
            self.assertNotIn(banned, SECTION)

    def test_typed_with_the_pages_own_editing_commands(self):
        self.assertIn('document.execCommand("insertText", false, lines[i])', SECTION)
        self.assertIn('document.execCommand("insertLineBreak")', SECTION)
        self.assertEqual(len(re.findall(r"execCommand\(", SECTION)), 2)

    def test_never_presses_after_its_budget(self):
        before_press = SECTION[:SECTION.index("button.click()")]
        self.assertIn('if (!left()) return ["left-as-draft", "late"];', before_press)

    def test_what_a_click_would_close_is_looked_at_seen_or_not(self):
        # WhatsApp's click closes the left drawer and leaves the open chat whether or not Natter's
        # window is showing (the page keeps both while it's hidden): only these guards stand
        # between that and what the person was in the middle of.
        start = SECTION.index("async function replyOnce")
        before_click = SECTION[start:SECTION.index("clickNotification(pageId);", start)]
        self.assertIn('if (drawerEdit()) return ["busy", "drawer"];', before_click)
        self.assertIn('if (liveStreams()) return ["busy", "recording"];', before_click)
        self.assertIn("footerInUse(open, visible)", before_click)  # any controls only in sight
        self.assertNotIn("if (visible)", before_click)

    def test_a_message_being_replied_to_counts_as_a_draft(self):
        start = SECTION.index("async function replyOnce")
        before_typing = SECTION[start:SECTION.index("execCommand(", start)]
        self.assertIn('if (besideTheBox(main, box)) return ["draft", "quote"];', before_typing)
        # Once, before typing: a link typed brings its preview up there, and that's not theirs.
        self.assertEqual(len(re.findall(r"(?<!function )besideTheBox\(", SECTION)), 1)

    def test_a_chat_that_takes_no_messages_isnt_busy(self):
        self.assertIn('footerInUse(main, false) ? ["busy", "no-composer"] : ["cant-send", ""]', SECTION)

    def test_the_drawers_are_looked_in_not_at(self):
        # WhatsApp keeps drawer-middle and -left in its page all the time, never empty: that one is
        # there, or has something in it, says nothing (it said "busy" to every reply, DrawerTest).
        self.assertNotRegex(SECTION, r'if \(!?find\("drawer-(middle|left)"\)\)')
        self.assertNotIn("drawerOpen", BRIDGE)
        self.assertNotIn("childElementCount", SECTION)
        start = SECTION.index("async function replyOnce")
        before_click = SECTION[start:SECTION.index("clickNotification(pageId);", start)]
        self.assertIn('if (previewOpen()) return ["busy", "preview"];', before_click)
        before_press = SECTION[SECTION.index("clickNotification(pageId);"):SECTION.index("button.click()")]
        self.assertIn("|| previewOpen()) {", before_press)  # looked at again just before Send

    def test_every_answer_is_one_natter_knows(self):
        answers = set(re.findall(r'\["([a-z-]+)", ', SECTION))
        for pair in re.findall(r'\[[\w.()]+ \? "([a-z-]+)" : "([a-z-]+)"', SECTION):
            answers.update(pair)
        answers.update(re.findall(r'(?:"": |typed: |pressed: )"([a-z-]+)"', SECTION))  # a mistake's
        self.assertEqual(answers, PAGE_REPLY_RESULTS)
        self.assertLessEqual(PAGE_REPLY_RESULTS, REPLY_RESULTS)

    def test_agrees_with_natter(self):
        self.assertEqual(re.search(r"const CHAT_ID = /\^(.*)\$/;", SECTION)[1], CHAT_ID.pattern)
        self.assertEqual(int(re.search(r"const REPLY_LIMIT = (\d+);", SECTION)[1]), REPLY_LIMIT)
        try:
            from natter.notify import CHAT_ID as NOTIFY_CHAT_ID
        except ImportError:
            NOTIFY_CHAT_ID = CHAT_ID
        self.assertEqual(NOTIFY_CHAT_ID.pattern, CHAT_ID.pattern)
        self.assertIn("reply,", BRIDGE[BRIDGE.index("window.__natter = {"):])

    def test_natter_waits_longer_than_the_page_can_take(self):
        sent_wait = int(re.search(r"const SENT_WAIT_MS = (\d+);", SECTION)[1])
        self.assertGreater(REPLY_GUARD * 1000, REPLY_BUDGET_MS + sent_wait)
        self.assertGreater(REPLY_LET_GO, REPLY_GUARD)
        most = int(re.search(r"Math\.min\(Math\.max\(budgetMs, 0\), (\d+)\)", SECTION)[1])
        self.assertLessEqual(REPLY_BUDGET_MS, most)

    def test_chat_ids(self):
        for good in ("4917612345678@c.us", "120363041234567890@g.us", "4917612345678-1600000000@g.us",
                     "123456789012345@lid"):
            self.assertTrue(CHAT_ID.fullmatch(good), good)
        for bad in ("", "Mum", "status@broadcast", "1203@newsletter", "call-accepted-elsewhere:123@c.us",
                    "call-link-joined:x", "waiting-room:x", "wa-web-call-active", "false_123@c.us_ABC", "1234@c.us"):
            self.assertIsNone(CHAT_ID.fullmatch(bad), bad)


class OpenChatTest(unittest.TestCase):
    """The bridge's check that the open chat is the one replied to (chatIds, isChat), run as it is,
    on stand-in rows with WhatsApp's keys."""

    PERSON_LID = "123456789012345@lid"

    @classmethod
    def setUpClass(cls):
        start = SECTION.index("  // The chats the open chat's messages belong to")
        end = SECTION.index("  // The keys of the person's own messages")
        cls.js = JavaScriptCore.Context()
        cls.js.evaluate('const WHERE = {"msg-key": ["[data-id]"]};\n' + SECTION[start:end], -1)
        exception = cls.js.get_exception()
        assert exception is None, exception and exception.get_message()

    def is_chat(self, keys, chat):
        main = "{querySelectorAll: () => %s.map((key) => ({getAttribute: () => key}))}" % json.dumps(keys)
        value = self.js.evaluate(f"isChat({main}, {json.dumps(chat)})", -1)
        self.assertIsNone(self.js.get_exception())
        return value.to_boolean()

    def test_the_chat_by_its_keys(self):
        self.assertTrue(self.is_chat([f"false_{GOOD}_3EB01", f"true_{GOOD}_3EB02"], GOOD))
        self.assertTrue(self.is_chat([f"false_{OTHER}_3EB01_{GOOD}", f"true_{OTHER}_3EB02"], OTHER))
        self.assertFalse(self.is_chat([f"false_{OTHER}_3EB01_{GOOD}"], GOOD))
        self.assertFalse(self.is_chat([], GOOD))
        self.assertFalse(self.is_chat(["3EB01", "row-7"], GOOD))  # not WhatsApp's keys

    def test_a_group_only_by_the_groups_keys(self):
        # A person's chat with a message quoted from the group ("Reply privately"), should a quote
        # carry a key: never taken for the group.
        self.assertFalse(self.is_chat([f"false_{GOOD}_3EB01", f"false_{OTHER}_3EB09_{GOOD}"], OTHER))

    def test_a_person_under_two_of_their_ids(self):
        keys = [f"false_{GOOD}_3EB01", f"false_{self.PERSON_LID}_3EB02"]
        self.assertTrue(self.is_chat(keys, GOOD))
        self.assertTrue(self.is_chat(keys, self.PERSON_LID))
        self.assertTrue(self.is_chat([f"false_{GOOD}_3EB01", f"false_{OTHER}_3EB09_{GOOD}"], GOOD))


class TextCheckTest(unittest.TestCase):
    """The bridge's check that the words landed as typed (canonical, sameText), run as it is."""

    @classmethod
    def setUpClass(cls):
        invisible = re.search(r"^  const INVISIBLE = .*$", BRIDGE, re.M)[0]
        start, end = SECTION.index("  const EMOTICONS = "), SECTION.index("  function boxEmpty(")
        cls.js = JavaScriptCore.Context()
        cls.js.evaluate(invisible + "\n" + SECTION[start:end], -1)
        exception = cls.js.get_exception()
        assert exception is None, exception and exception.get_message()

    def same(self, landed, wanted):
        value = self.js.evaluate(f"sameText(canonical({json.dumps(landed)}), canonical({json.dumps(wanted)}))", -1)
        self.assertIsNone(self.js.get_exception())
        return value.to_boolean()

    def test_exact(self):
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        for text in ("hello", "On my way \U0001F44D", "?!", "\U0001F44D", "a\n\nb", family):
            self.assertTrue(self.same(text, text), text)

    def test_nothing_landed_is_never_right(self):
        for text in ("\U0001F44D", "?", "!!", "hello", ":-)"):
            self.assertFalse(self.same("", text), text)

    def test_lines_are_kept(self):
        self.assertFalse(self.same("helloworld", "hello\nworld"))
        self.assertFalse(self.same("hello world", "hello\nworld"))
        self.assertFalse(self.same("a\nb", "a\n\nb"))
        self.assertFalse(self.same("hello", "hello\nworld"))

    def test_spaces_and_marks_aside(self):
        self.assertTrue(self.same("a\u00a0 b ", " a  b"))
        self.assertTrue(self.same("\u2764", "\u2764\ufe0f"))
        self.assertTrue(self.same("caf\u00e9", "cafe\u0301"))
        self.assertTrue(self.same("a\u200eb", "ab"))

    def test_whatsapps_emoticon_swap_at_the_end_of_a_line(self):
        self.assertTrue(self.same("see you \U0001F600", "see you :-D"))
        self.assertTrue(self.same("one \U0001F642\ntwo \u2764\ufe0f", "one :-)\ntwo <3"))
        self.assertTrue(self.same("thanks \U0001F44D\U0001F3FD", "thanks (y)"))
        self.assertTrue(self.same("\U0001F606", ">_<"))
        self.assertFalse(self.same("\U0001F600 later", ":-D later"))  # only at the caret
        self.assertFalse(self.same("see you \U0001F642", "see you :-D"))
        self.assertFalse(self.same("see you", "see you :-D"))
        self.assertFalse(self.same("see you \U0001F600\U0001F600", "see you :-D"))


# ---- What's open in WhatsApp's drawers --------------------------------------------------------

class _Markup(HTMLParser):
    """HTML as a tree of plain dicts: {"tag", "attrs", "children"} and {"text"}."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#document", "attrs": {}, "children": []}
        self.open = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {name: value or "" for name, value in attrs}, "children": []}
        self.open[-1]["children"].append(node)
        if tag not in self.VOID:
            self.open.append(node)

    def handle_startendtag(self, tag, attrs):
        self.open[-1]["children"].append({"tag": tag, "attrs": {name: value or "" for name, value in attrs},
                                          "children": []})

    def handle_endtag(self, tag):
        for depth in range(len(self.open) - 1, 0, -1):
            if self.open[depth]["tag"] == tag:
                del self.open[depth:]
                return

    def handle_data(self, data):
        self.open[-1]["children"].append({"text": data})


def mock_template(name):
    """The markup of one of the mock page's <template>s, as a tree."""
    match = re.search(r'<template id="%s">(.*?)</template>' % re.escape(name), MOCK, re.S)
    assert match, name
    return _Markup(match[1]).root


# Just enough of a page for the bridge's own functions, in JavaScriptCore: elements and text from
# a tree, their attributes, fields' values and editability, and querySelector for the selectors
# the bridge uses (tags, #id, .class, [attr], [attr="v"], [attr^="v"], :not(…), descendant and
# child, lists).
FAKE_PAGE = r"""
const Node = { ELEMENT_NODE: 1, TEXT_NODE: 3 };
class FakeText {
  constructor(text, parent) { this.nodeType = 3; this.nodeName = "#text"; this.textContent = text; this.parentElement = parent; }
}
class FakeElement {
  constructor(tree, parent) {
    this.nodeType = 1;
    this.tagName = this.nodeName = tree.tag.toUpperCase();
    this.attributes = { ...tree.attrs };
    this.parentElement = parent;
    this.childNodes = [];
    if (this.tagName === "INPUT" || this.tagName === "TEXTAREA") {
      this.value = tree.attrs.value ?? (this.tagName === "TEXTAREA" ? tree.children.map((c) => c.text || "").join("") : "");
      this.disabled = "disabled" in tree.attrs;
      this.readOnly = "readonly" in tree.attrs;
    }
    add(this, tree.children);
  }
  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  get isConnected() { return true; }
  get isContentEditable() {
    for (let element = this; element; element = element.parentElement) {
      const value = element.getAttribute("contenteditable");
      if (value !== null) return value !== "false";
    }
    return false;
  }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  contains(other) {
    for (let node = other; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  remove() {
    const siblings = this.parentElement.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
  }
  descendants() { return this.children.flatMap((child) => [child, ...child.descendants()]); }
  querySelectorAll(selector) {
    const list = topLevel(selector, /,/).filter((_, i) => i % 2 === 0).map(complex);
    return this.descendants().filter((element) => list.some((steps) => matches(element, steps, steps.length - 1)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function add(parent, trees) {
  trees.forEach((tree) => parent.childNodes.push("text" in tree ? new FakeText(tree.text, parent) : new FakeElement(tree, parent)));
}
// `text` cut at each `separator` character outside brackets and quotes: [part, separator, part, …].
function topLevel(text, separator) {
  const out = [];
  let depth = 0, quote = "", part = "";
  for (const ch of text) {
    if (quote) { if (ch === quote) quote = ""; part += ch; continue; }
    if (ch === '"') quote = ch;
    else if (ch === "[" || ch === "(") depth++;
    else if (ch === "]" || ch === ")") depth--;
    else if (!depth && separator.test(ch)) { out.push(part, ch); part = ""; continue; }
    part += ch;
  }
  out.push(part);
  return out;
}
function compound(text) {
  const tests = [];
  let rest = text, m;
  while (rest) {
    if ((m = /^([a-zA-Z][\w-]*|\*)/.exec(rest))) {
      const tag = m[1].toUpperCase();
      if (tag !== "*") tests.push((e) => e.tagName === tag);
    } else if ((m = /^#([\w-]+)/.exec(rest))) {
      const id = m[1];
      tests.push((e) => e.getAttribute("id") === id);
    } else if ((m = /^\.([\w-]+)/.exec(rest))) {
      const name = m[1];
      tests.push((e) => (e.getAttribute("class") || "").split(/\s+/).includes(name));
    } else if ((m = /^\[([\w-]+)(?:(\^?=)"([^"]*)")?\]/.exec(rest))) {
      const [, name, op, value] = m;
      tests.push((e) => {
        const has = e.getAttribute(name);
        return has !== null && (!op || (op === "=" ? has === value : has.startsWith(value)));
      });
    } else if ((m = /^:not\(([^()]*)\)/.exec(rest))) {
      const inner = compound(m[1]);
      tests.push((e) => !inner(e));
    } else {
      throw new Error("FAKE_PAGE can't read the selector " + text);
    }
    rest = rest.slice(m[0].length);
  }
  return (e) => tests.every((test) => test(e));
}
// A selector as steps, last one first matched: [{test, child}] (child: ">" before it).
function complex(text) {
  const steps = [];
  let child = false;
  topLevel(text.trim(), /[\s>]/).forEach((part, i) => {
    if (i % 2) { child = child || part === ">"; return; }
    if (!part) return;
    steps.push({ test: compound(part), child });
    child = false;
  });
  return steps;
}
function matches(element, steps, i) {
  if (!element || element.nodeType !== 1 || !steps[i].test(element)) return false;
  if (i === 0) return true;
  if (steps[i].child) return matches(element.parentElement, steps, i - 1);
  for (let up = element.parentElement; up; up = up.parentElement) if (matches(up, steps, i - 1)) return true;
  return false;
}
let document = null;
function load(tree) { document = new FakeElement(tree, null); }
// As the mock's openDrawer: into the drawer manager's span.
function openDrawer(key, tree) { add(document.querySelector(`[data-testid="drawer-${key}"] span`), tree.children); }
"""


class DrawerTest(unittest.TestCase):
    """What's open in WhatsApp's drawers (previewOpen, drawerEdit), run as they are on the mock
    page's own markup: its drawers as WhatsApp's are (WAWebMain.react: a container, a <div>, and
    the drawer manager's <span>, there all the time) and what opens in them, its media editor
    (WAWebMediaEditor.react: a hidden <input type=file multiple>, a caption box)."""

    @classmethod
    def setUpClass(cls):
        where = BRIDGE[BRIDGE.index("  const WHERE = {"):BRIDGE.index("  // ---- The chat list")]
        invisible = re.search(r"^  const INVISIBLE = .*$", BRIDGE, re.M)[0]
        start = SECTION.index("  const EMOTICONS = ")
        # And where a reply's words go and which Send it presses (composerIn, sendButton).
        end = SECTION.index("  // Whether `element` is on show")
        send = SECTION[SECTION.index("  // WhatsApp's Send button, by the name"):SECTION.index("  // test()'s value")]
        cls.js = JavaScriptCore.Context()
        cls.js.evaluate("\n".join((FAKE_PAGE, where, invisible, SECTION[start:end], send)), -1)
        exception = cls.js.get_exception()
        assert exception is None, exception and exception.get_message()

    def run_js(self, script):
        value = self.js.evaluate(script, -1)
        exception = self.js.get_exception()
        self.assertIsNone(exception, exception and exception.get_message())
        return value

    def page(self, **drawers):
        """The mock's chat page with `drawer=template` open, e.g. page(middle="media-editor")."""
        self.run_js(f"load({json.dumps(mock_template('chats'))})")
        for key, template in drawers.items():
            self.run_js(f"openDrawer({json.dumps(key)}, {json.dumps(mock_template(template))})")

    def ask(self, expression):
        return self.run_js(expression).to_boolean()

    def test_whatsapps_drawers_are_never_empty(self):
        # What every reply was refused for ("busy (preview)", 12 of 12): with nothing open, each
        # drawer still holds its manager's <div> and <span>.
        self.page()
        for key in ("drawer-left", "drawer-middle"):
            self.assertTrue(self.ask(f'[...find("{key}").children].some((child) => child.childElementCount > 0)'))
        self.assertFalse(self.ask("previewOpen()"))
        self.assertFalse(self.ask("drawerEdit()"))

    def test_a_preview_is_open(self):
        self.page(middle="media-editor")
        self.assertTrue(self.ask("previewOpen()"))
        self.assertFalse(self.ask("drawerEdit()"), "the left drawer's, not the middle's")

    def test_a_preview_by_either_of_its_own(self):
        self.page(middle="media-editor")
        self.run_js('find("drawer-middle").querySelector(\'input[type="file"]\').remove()')
        self.assertTrue(self.ask("previewOpen()"), "by its caption box")
        self.page(middle="media-editor")
        self.run_js('find("drawer-middle").querySelector(\'[data-testid="media-caption-input-container"]\').remove()')
        self.assertTrue(self.ask("previewOpen()"), "by its file input")
        self.run_js('find("drawer-middle").querySelector(\'input[type="file"]\').remove()')
        self.assertFalse(self.ask("previewOpen()"), "neither: not seen, not open")

    def test_what_else_opens_there_isnt_a_preview(self):
        # Beside Status, Calls or Settings: an empty state in the middle drawer.
        self.page(middle="empty-state")
        self.assertFalse(self.ask("previewOpen()"))
        # A media editor anywhere else isn't the chat's preview.
        self.page(right="media-editor")
        self.assertFalse(self.ask("previewOpen()"))

    def test_a_drawer_over_the_chat_list_with_nothing_typed(self):
        # Settings (or Status, the Calls tab) left open: WhatsApp's click just closes it.
        self.page(left="settings")
        self.assertFalse(self.ask("drawerEdit()"))
        self.page(left="new-group")  # its name box, empty as Lexical leaves it (<p><br></p>)
        self.assertFalse(self.ask("drawerEdit()"))

    def test_words_typed_over_the_chat_list(self):
        self.page(left="new-group")
        self.run_js('const line = find("drawer-left").querySelector("[contenteditable] p");'
                    'line.childNodes.unshift(new FakeText("Book club", line))')
        self.assertTrue(self.ask("drawerEdit()"))
        self.page(left="settings")
        self.run_js('find("drawer-left").querySelector("input").value = "  privacy "')
        self.assertTrue(self.ask("drawerEdit()"))

    def test_fields_without_words_of_theirs(self):
        for field in ('<input type="checkbox" value="on">', '<input type="text" value="x" readonly>',
                      '<input type="text" value="x" disabled>', '<input type="file" multiple>',
                      '<input type="hidden" value="x">', '<div contenteditable="false">Book club</div>',
                      '<input type="search" value="   ">'):
            self.page(left="settings")
            self.run_js(f'add(find("drawer-left").querySelector(".panel"), {json.dumps(_Markup(field).root["children"])})')
            self.assertFalse(self.ask("drawerEdit()"), field)
        self.page(left="settings")
        self.run_js(f'add(find("drawer-left").querySelector(".panel"), '
                    f'{json.dumps(_Markup("<input value=x><textarea>notes</textarea>").root["children"])})')
        self.assertTrue(self.ask("drawerEdit()"))

    def test_a_reply_never_reaches_the_preview(self):
        # Seen or not, a preview can't take a reply: the box typed into and the Send pressed are
        # only ever the open chat's own, in #main's footer, and WhatsApp's drawers are beside #main
        # (WAWebMain.react), not in it. The preview's caption box and its Send are never found.
        send = re.search(r"""words\s*\? '(<button aria-label="Send">.*?</button>)'""", MOCK)[1]  # the mock's slot()
        own = ('composerIn(find("main")).getAttribute("title") === "Type a message"',
               'sendButton(find("main")) === document.querySelector("#send-slot button")',
               '!find("main").contains(find("drawer-middle"))')
        # The preview whole, by one of its own alone, and not seen at all.
        for gone in ((), ('input[type="file"]',),
                     ('input[type="file"]', '[data-testid="media-caption-input-container"]')):
            self.page(middle="media-editor")
            self.run_js(f'add(document.querySelector("#send-slot"), {json.dumps(_Markup(send).root["children"])})')
            for selector in gone:
                self.run_js(f'find("drawer-middle").querySelector({json.dumps(selector)}).remove()')
            self.assertEqual(self.ask("previewOpen()"), len(gone) < 2, gone)
            for check in own:
                self.assertTrue(self.ask(check), (gone, check))
        # The preview's Send would pass for one by its icon's name, were it ever looked for there.
        self.assertIn("send", self.run_js(
            'iconNames(find("drawer-middle").querySelector(\'[aria-label="Send"]\'))').to_string())

    def test_the_fake_page_reads_selectors_as_a_page_does(self):
        self.page(middle="media-editor")
        for selector, count in (('[data-testid="drawer-middle"] span', 3), ('[data-testid="drawer-middle"] > div > span', 1),
                                ('[data-testid^="drawer-"]', 3), ('input:not([type="text"])', 1), ("#preview, footer", 2),
                                ('[data-testid="drawer-left"] > span', 0)):
            self.assertEqual(self.run_js(f"document.querySelectorAll({json.dumps(selector)}).length").to_int32(),
                             count, selector)


if __name__ == "__main__":
    unittest.main()
