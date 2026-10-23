"""Replies from notifications, the page's side: Account.send_reply (account.py) and the bridge's
reply (bridge.js). One reply is one send through WhatsApp's own page, or none: never begun while
locked or in a call, one at a time, never retried, the words only ever as arguments, the log
without names, ids or words, and nothing else acting on the page while it runs. The bridge's
whole run was tried against real Lexical in WebKitGTK (a lab); here, what can be pinned without
a page: its rules, and its text comparison run in JavaScriptCore, and what it takes for open in
WhatsApp's drawers run there on the mock page's own markup."""

import contextlib
import inspect
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
                            REPLY_LIMIT, REPLY_QUEUE_WAIT, REPLY_RESULTS, REPLY_SCRIPT, Account, diag_line,
                            reply_answer, reply_diag, reply_length, reply_text)

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
        self.app = SimpleNamespace(locked=False, reclaimer=SimpleNamespace(stops=0), seen=False)
        self.app.reclaimer.stop = lambda: setattr(self.app.reclaimer, "stops", self.app.reclaimer.stops + 1)
        self.app.page_seen = lambda account: self.app.seen
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


class Loan:
    """A page lent to a reply card (loan.PageLoan), as Account sees it: given back how often, and
    whether GTK has laid it out at its size yet."""

    def __init__(self, laid_out=True):
        self.returns, self.lent, self.ready = 0, True, laid_out

    def laid_out(self):
        return self.ready

    def give_back(self):
        self.returns += 1
        self.lent = False


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
        page.app.seen = True
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

    # ---- The page lent to a reply card while out of sight (loan.py) -----------------------------

    def lent_reply(self, page, mapped=False, laid_out=True):
        """A reply with a lend() that lends: (its results, the loans made)."""
        loans = []

        def lend():
            loans.append(Loan(laid_out))
            return loans[-1]
        page.view.mapped = mapped
        results = []
        page.send_reply(7, GOOD, "On my way", results.append, lend=lend)
        return results, loans

    def test_lent_while_it_runs_out_of_sight(self):
        page = Page()
        results, (loan,) = self.lent_reply(page)
        self.assertEqual(loan.returns, 0)
        page.answer(sent())
        self.assertEqual(loan.returns, 1)  # back with the answer, before it's told
        self.clock.flush()
        self.assertEqual(results, ["sent"])
        page._give_back(page._reply)  # nothing more to give back
        self.assertEqual(loan.returns, 1)

    def test_a_page_never_laid_out_is_asked_once_it_is(self):
        # Lent before GTK has laid it out (Natter started hidden): WebKit has the card's size until
        # then, and WhatsApp would open the chat in a page that small.
        tick = natter_account.REPLY_LAYOUT_TICK / 1000
        page = Page()
        results, (loan,) = self.lent_reply(page, laid_out=False)
        self.assertEqual(self.replies(page), [])
        self.clock.fire(tick)
        self.assertEqual(self.replies(page), [])
        loan.ready = True
        self.clock.fire(tick)
        self.assertEqual(len(self.replies(page)), 1)
        page.answer(sent())
        self.clock.flush()
        self.assertEqual(results, ["sent"])

    def test_the_wait_for_a_layout_is_short(self):
        tick = natter_account.REPLY_LAYOUT_TICK / 1000
        page = Page()
        _results, (loan,) = self.lent_reply(page, laid_out=False)
        for _ in range(natter_account.REPLY_LAYOUT_WAIT // natter_account.REPLY_LAYOUT_TICK - 1):
            self.clock.fire(tick)
        self.assertEqual(self.replies(page), [])
        self.clock.fire(tick)
        self.assertEqual(len(self.replies(page)), 1)  # asked anyway
        self.assertLessEqual(natter_account.REPLY_LAYOUT_WAIT, 2000)  # a frame or two, but for a slow display
        page = Page()
        _results, (loan,) = self.lent_reply(page, laid_out=False)
        loan.give_back()  # the card closed meanwhile: asked as it is
        self.clock.fire(tick)
        self.assertEqual(len(self.replies(page)), 1)

    def test_never_asked_once_answered_or_gone_while_it_waits_for_a_layout(self):
        tick = natter_account.REPLY_LAYOUT_TICK / 1000
        page = Page()
        results, (loan,) = self.lent_reply(page, laid_out=False)
        page._answer(page._reply, "unknown")
        self.assertEqual((loan.returns, page._reply.layout_timer), (1, 0))
        self.clock.fire(tick)
        self.assertEqual(self.replies(page), [])
        page = Page()
        results, (loan,) = self.lent_reply(page, laid_out=False)
        page.removed = True
        self.clock.fire(tick)
        self.clock.flush()
        self.assertEqual((self.replies(page), results, page.replying), ([], ["removed"], 0))

    def test_never_lent_when_it_can_be_seen_or_isnt_asked(self):
        page = Page()
        _results, loans = self.lent_reply(page, mapped=True)
        self.assertEqual(loans, [])
        page.answer(sent())
        page = Page()
        page.send_reply(7, GOOD, "On my way", [].append)  # no card to lend it to
        self.assertEqual(len(self.replies(page)), 1)

    def test_never_lent_for_what_never_reaches_the_page(self):
        for setup in (lambda page: setattr(page.app, "locked", True), lambda page: setattr(page, "in_call", True),
                      lambda page: setattr(page, "trusted", False)):
            page = Page()
            setup(page)
            results, loans = self.lent_reply(page)
            self.clock.flush()
            self.assertEqual((len(results), loans), (1, []))

    def test_back_at_the_guard_and_after(self):
        page = Page()
        results, (loan,) = self.lent_reply(page)
        self.clock.fire(REPLY_GUARD)
        self.assertEqual(loan.returns, 1)  # "unknown" said: back now, while the page may still go on
        self.clock.fire(REPLY_LET_GO - REPLY_GUARD)
        page.answer(sent())
        self.clock.flush()
        self.assertEqual((results, loan.returns), (["unknown"], 1))

    def test_back_whatever_the_answer(self):
        answers = [{"result": result} for result in sorted(PAGE_REPLY_RESULTS)]
        for value in [None, {"result": "mismatch", "why": "no-ids"}] + answers:
            page = Page()
            _results, (loan,) = self.lent_reply(page)
            page.answer(value)
            self.assertEqual(loan.returns, 1, value)

    def test_back_when_the_page_ends(self):
        page = Page()
        _results, (loan,) = self.lent_reply(page)
        page._give_back(page._reply)  # as Account.remove does (a crash or a new load: the loan's own watch)
        self.assertEqual(loan.returns, 1)
        self.assertIn("self._give_back(self._reply)", inspect.getsource(Account.remove))

    def test_visible_is_seen_not_mapped(self):
        # Mapped in a card isn't seen, nor mapped in a window showing another account's tab: the
        # page says what the person can see, as it was before any loan.
        page = Page()
        page.view.mapped = True
        self.reply(page)
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], False)
        page = Page()
        page.app.page_seen = lambda account: page.view.mapped  # as if mapped were seen
        page.send_reply(7, GOOD, "On my way", [].append, lend=lambda: (setattr(page.view, "mapped", True), Loan())[1])
        self.assertTrue(page.view.mapped)  # in the card
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], False)  # as it was before
        page = Page()
        page.app.seen = True
        self.reply(page)
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], True)

    # ---- What the log may say of the page ------------------------------------------------------

    DIAG = {"visibilityState": "hidden", "rafFired": False, "innerWidth": 1000, "innerHeight": 700, "main": True,
            "header": True, "footer": True, "composer": True, "keys": 0, "chatKeys": 0, "rows": 0, "shape": ""}

    def test_the_diag_is_logged_on_the_same_line(self):
        page = Page()
        _results, (loan,) = self.lent_reply(page)
        page.answer({"result": "mismatch", "why": "no-ids", "diag": self.DIAG})
        self.clock.flush()
        self.assertEqual(self.log.getvalue(),
                         "natter: reply: mismatch (no-ids) [page hidden, frames no, mapped no, lent yes, chat yes, "
                         "header yes, footer yes, box yes, size 1000x700, keys 0, chat keys 0, rows 0, key none]\n")

    def test_an_odd_diag_is_left_out(self):
        page = Page()
        self.reply(page, text="Secret words")
        page.answer({"result": "gone", "why": "click", "diag": dict(self.DIAG, shape="Mum")})
        self.clock.flush()
        self.assertEqual(self.log.getvalue(), "natter: reply: gone (click)\n")

    def test_the_diag_takes_only_what_it_knows(self):
        good = dict(self.DIAG, shape="a_9@a.a_9a9a", keys=999, innerWidth=9900, innerHeight=0)
        self.assertEqual(reply_diag({"diag": good}), good)
        self.assertEqual(reply_diag({"diag": {"keys": 3}}), {"keys": 3})
        for value in (None, [], "diag", {}, {"diag": None}, {"diag": []}, {"diag": {}}):
            self.assertIsNone(reply_diag(value), value)
        refused = [
            {"name": "Mum"}, {"title": "Work Team"}, {"text": "On my way"}, {"chat": GOOD},  # unknown fields
            {"shape": "Mum"}, {"shape": "on my way"}, {"shape": GOOD}, {"shape": "false_" + GOOD},
            {"shape": "true_4917612345678@c.us_3EB0"}, {"shape": "a" * 41}, {"shape": 9},
            {"visibilityState": "Mum"}, {"visibilityState": True},
            {"rafFired": "yes"}, {"rafFired": 1}, {"main": None},
            {"keys": True}, {"keys": -1}, {"keys": 1000}, {"keys": 2.0}, {"keys": "3"},
            {"innerWidth": 1024}, {"innerWidth": 10000}, {"innerHeight": -100},
        ]
        for diag in refused:
            self.assertIsNone(reply_diag({"diag": dict(self.DIAG, **diag)}), diag)

    def test_the_diag_line_says_no_names(self):
        line = diag_line(dict(self.DIAG, shape="aaaaa_99999@a.aa_9aa9", mapped=True, lent=False))
        self.assertTrue(line.startswith("[page hidden, frames no, mapped yes, lent no, "), line)
        self.assertTrue(line.endswith("key aaaaa_99999@a.aa_9aa9]"), line)

    def test_the_script_hands_the_diag_back(self):
        self.assertIn("diag: request.diag || null", REPLY_SCRIPT)
        self.assertIn("request.diag = progress.diag;", SECTION)

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


# The page's clock, timers and observers for a whole reply run in JavaScriptCore: time moves only
# when the test says (step), each timer runs in a turn of its own with its promises after it, an
# animation frame comes only while frames are on (a page WebKit draws), and the observers hear of
# a change when the test says the page changed (mutate).
FAKE_RUNTIME = r"""
let now = 0, nextTimer = 1, frames = false, clicked = 0;
const timers = [];
const performance = { now: () => now };
function setTimeout(run, ms) {
  const id = nextTimer++;
  timers.push({ id, at: now + Math.max(0, ms || 0), run });
  return id;
}
function clearTimeout(id) { const i = timers.findIndex((timer) => timer.id === id); if (i >= 0) timers.splice(i, 1); }
function requestAnimationFrame(run) { if (frames) setTimeout(run, 16); return 1; }
const observers = new Set();
class MutationObserver {
  constructor(callback) { this.callback = callback; }
  observe() { observers.add(this); }
  disconnect() { observers.delete(this); }
}
function mutate() { [...observers].forEach((observer) => observer.callback([])); }
// The next timer due by `until` ms, or the clock moved on to it: true while there was one.
function step(until) {
  timers.sort((a, b) => a.at - b.at || a.id - b.id);
  const next = timers[0];
  if (!next || next.at > until) { now = Math.max(now, until); return false; }
  timers.shift();
  now = Math.max(now, next.at);
  next.run();
  return true;
}
const shown = new Map();
function clickNotification(id) { clicked++; }
function calling() { return false; }
function liveStreams() { return false; }
const innerWidth = 1000, innerHeight = 700;
// A message row as WhatsApp's: the row, and its key on the div in it.
function addRow(key) {
  add(find("main").querySelector("#messages"), [{ tag: "div", attrs: { role: "row" },
    children: [{ tag: "div", attrs: { "data-id": key }, children: [{ text: "words" }] }] }]);
  mutate();
}
let answer = null, asked = null, answeredAt = null;
function ask(chat, budget) {
  asked = { pageId: 7, chatId: chat, text: "On my way", budgetMs: budget, visible: false };
  shown.set(7, { tag: chat });
  reply(asked).then((result) => { answer = result; answeredAt = now; });
}
"""


class ReplyWaitTest(unittest.TestCase):
    """The bridge's reply run as it is, from WhatsApp's notification click to the check of the chat
    that opened, on the mock page's own markup with the page's clock in the test's hands: how long
    it waits for the chat's messages, and what it tells Natter's log when none come."""

    BUDGET = REPLY_BUDGET_MS

    def setUp(self):
        where = BRIDGE[BRIDGE.index("  const WHERE = {"):BRIDGE.index("  // ---- The chat list")]
        invisible = re.search(r"^  const INVISIBLE = .*$", BRIDGE, re.M)[0]
        self.js = JavaScriptCore.Context()
        self.run_js("\n".join((FAKE_PAGE, FAKE_RUNTIME, where, invisible, SECTION)))
        self.run_js(f"load({json.dumps(mock_template('chats'))}); document.visibilityState = 'hidden'")
        self.chat_wait = int(re.search(r"const CHAT_WAIT_MS = (\d+);", SECTION)[1])
        self.reserve = self.run_js("TYPE_RESERVE_MS").to_int32()

    def run_js(self, script):
        value = self.js.evaluate(script, -1)
        exception = self.js.get_exception()
        self.assertIsNone(exception, exception and exception.get_message())
        return value

    def until(self, ms):
        """The page's clock on to `ms`, a timer at a time."""
        while self.run_js(f"step({ms})").to_boolean():
            pass

    def answer(self):
        """(answer, why, diag, when), once the reply has answered; None before."""
        if self.run_js("answer === null").to_boolean():
            return None
        found = json.loads(self.run_js("JSON.stringify({answer, why: asked.why, diag: asked.diag, answeredAt})")
                           .to_string())
        return found["answer"], found["why"], found["diag"], found["answeredAt"]

    def later(self, ms, script):
        self.run_js(f"setTimeout(() => {{ {script} }}, {ms})")

    def test_no_messages_wait_out_the_budget(self):
        # The chat opens with no messages keyed, and none come: waited for to the end of the
        # budget less what typing takes, not just the chat's own wait, then "no-ids" with what the
        # page was like.
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.chat_wait + 1000)
        self.assertIsNone(self.answer(), "still waiting past the chat's own wait")
        self.until(self.BUDGET + 1000)
        answer, why, diag, when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "no-ids"))
        self.assertGreaterEqual(when, self.BUDGET - self.reserve)
        self.assertLess(when, self.BUDGET - self.reserve + 1000)
        self.assertEqual(self.run_js("clicked").to_int32(), 1)
        self.assertEqual(diag, {"visibilityState": "hidden", "rafFired": False, "innerWidth": 1000, "innerHeight": 700,
                                "main": True, "header": True, "footer": True, "composer": True, "keys": 0,
                                "chatKeys": 0, "rows": 0, "shape": ""})
        self.assertEqual(reply_diag({"diag": diag}), diag)

    def test_messages_loaded_late_are_waited_for(self):
        # WhatsApp loading the messages around the notified one: they come after the chat's own
        # wait, and the chat is checked by them as ever. (No footer: it goes no further than the
        # check, and says the chat takes no messages.)
        self.run_js('find("main").querySelector("footer").remove()')
        self.later(self.chat_wait + 2000, f'addRow("false_{GOOD}_3EB0LATE")')
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, diag, when = self.answer()
        self.assertEqual((answer, why, diag), ("cant-send", "", None))
        self.assertLess(when, self.chat_wait + 3000)

    def test_time_is_kept_back_for_typing(self):
        # Messages that come too late to leave time for the draft, the words and Send: nothing typed.
        self.later(self.BUDGET - 300, f'addRow("false_{GOOD}_3EB0LATE")')
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, _diag, when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "no-ids"))
        self.assertLess(when, self.BUDGET - self.reserve + 1000)
        self.assertTrue(self.run_js('boxEmpty(composerIn(find("main")))').to_boolean())

    def test_a_page_that_stalled_types_nothing_late(self):
        # The page held up (its timers late) until the messages came with less time left than the
        # rest takes: "busy" (late), nothing typed.
        self.later(self.chat_wait + 1000, f'now += {self.BUDGET - self.chat_wait - 4000}; '
                                          f'addRow("false_{GOOD}_3EB0LATE")')
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, diag, _when = self.answer()
        self.assertEqual((answer, why, diag), ("busy", "late", None))
        self.assertTrue(self.run_js('boxEmpty(composerIn(find("main")))').to_boolean())

    def test_another_chats_messages_are_still_a_mismatch(self):
        self.later(self.chat_wait + 2000, f'addRow("false_{OTHER}_3EB0LATE_{GOOD}")')
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, diag, when = self.answer()
        self.assertEqual((answer, why, diag), ("mismatch", "other-chat", None))
        self.assertLess(when, self.chat_wait + 3000)

    def test_the_wait_ends_with_the_budget(self):
        budget = self.chat_wait + 3000
        self.later(self.chat_wait + 5000, f'addRow("false_{GOOD}_3EB0LATE")')  # after the budget
        self.run_js(f"ask({json.dumps(GOOD)}, {budget})")
        self.until(budget + 10000)
        answer, why, _diag, when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "no-ids"))
        self.assertLess(when, budget + 1000)  # the budget, and the look at the page after it

    def test_no_chat_open_after_the_click(self):
        self.run_js('const main = find("main"); main.parentElement.childNodes.splice('
                    'main.parentElement.childNodes.indexOf(main), 1)')
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, diag, when = self.answer()
        self.assertEqual((answer, why), ("gone", "click"))
        self.assertLess(when, self.chat_wait + 1000)  # no messages to wait for without a chat
        self.assertEqual((diag["main"], diag["header"], diag["footer"], diag["keys"], diag["shape"]),
                         (False, False, False, 0, ""))

    def test_frames_and_the_shape_of_a_key(self):
        # A page WebKit draws, a chat whose keys aren't WhatsApp's: the shape of the first, and
        # none of it as it was.
        self.run_js("frames = true; document.visibilityState = 'visible'")
        self.run_js('addRow("Mum’s chat 4917612345678 ✓")')
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, diag, _when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "no-ids"))
        self.assertEqual((diag["rafFired"], diag["visibilityState"], diag["keys"], diag["chatKeys"], diag["rows"]),
                         (True, "visible", 1, 0, 1))
        self.assertEqual(diag["shape"], "a?a?a?9?")  # runs, not their lengths
        self.assertEqual(reply_diag({"diag": diag}), diag)

    def test_a_long_key_is_cut(self):
        self.run_js(f'addRow("true_{GOOD}_3EB0" + "A".repeat(80) + "_99")')
        self.run_js(f"ask({json.dumps(OTHER)}, {self.BUDGET})")
        self.until(self.BUDGET + 1000)
        answer, why, diag, _when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "other-chat"))
        self.assertIsNone(diag)  # another chat's keys: nothing more to say
        self.run_js('diagnose().then((d) => { answer = d.shape; })')
        self.until(self.BUDGET + 2000)
        shape = self.run_js("answer").to_string()
        self.assertEqual(shape, "a_9@a.a_9a9a")  # its first 40 characters only


if __name__ == "__main__":
    unittest.main()
