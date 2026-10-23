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
from natter.account import (BRIDGE, CHAT_ID, PAGE_HIDDEN_SCRIPT, PAGE_REPLY_RESULTS, REPLY_BUDGET_MS, REPLY_GUARD,
                            REPLY_LET_GO, REPLY_LIMIT, REPLY_QUEUE_WAIT, REPLY_RESULTS, REPLY_SCRIPT, Account,
                            diag_line, reply_answer, reply_diag, reply_length, reply_text)

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
        self.settings, self.mapped, self.terminated, self.root = Settings(), False, 0, None

    def get_settings(self):
        return self.settings

    def get_mapped(self):
        return self.mapped

    def get_root(self):
        return self.root  # none: as a window that isn't minimised (loan.out_of_sight)

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
        self._chats_later = []
        self.runs, self.loaded, self.emitted = [], [], []

    calling = call_connected = property(lambda self: self.in_call)

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
        self.assertEqual(type(args["request"].pop("sinceMs")), int)  # how long ago its turn came
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
            page.answer({"result": result, "why": "unconfirmed"})
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

    def test_how_the_chat_was_told_is_logged(self):
        for how in ("ids", "name+body", "name+avatar", "name+click"):
            self.assertEqual(reply_answer({"result": "sent", "why": how}), ("sent", how))
        page = Page()
        self.reply(page)
        page.answer(sent("name+body"))
        self.clock.flush()
        self.assertEqual(self.log.getvalue(), "natter: reply: sent (name+body)\n")

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
        page.answer({"result": "mismatch", "why": "unconfirmed"})
        self.clock.flush()
        self.assertEqual(self.log.getvalue(), "natter: reply: mismatch (unconfirmed)\n")
        self.assertNotIn(GOOD, self.log.getvalue())

    # ---- The page lent to a reply card while out of sight (loan.py) -----------------------------

    def lent_reply(self, page, mapped=False, laid_out=True):
        """A reply with a lend() that lends: (its results, the loans made)."""
        loans = []

        def lend(hidden):
            loans.append(Loan(laid_out))
            loans[-1].hidden = hidden
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
        self.assertEqual(page.runs[0][0], PAGE_HIDDEN_SCRIPT)  # on screen: the page is asked first
        self.assertEqual(self.replies(page), [])
        page.answer("visible", 0)
        self.assertEqual(loans, [])
        self.assertEqual(len(self.replies(page)), 1)
        page.answer(sent())
        page = Page()
        page.view.mapped = True
        page.send_reply(7, GOOD, "On my way", [].append)  # no card to lend it to: nothing asked first
        self.assertEqual([body for body, _a, _d in page.runs], [REPLY_SCRIPT])

    def test_lent_from_a_window_on_screen_when_the_page_is_hidden(self):
        # Mapped to GTK, its window not minimised, and still hidden to WebKit: lent all the same.
        page = Page()
        results, loans = self.lent_reply(page, mapped=True)
        page.answer("hidden", 0)
        (loan,) = loans
        self.assertIs(loan.hidden, True)
        self.assertEqual(len(self.replies(page)), 1)
        page.answer(sent())
        self.clock.flush()
        self.assertEqual((results, loan.returns), (["sent"], 1))
        page = Page()
        _results, loans = self.lent_reply(page, mapped=True)
        page.answer(None, 0)  # no answer: as on screen
        self.assertEqual((loans, len(self.replies(page))), ([], 1))

    def test_lent_from_a_minimised_window_without_asking(self):
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        page = Page()
        page.view.root = SimpleNamespace(get_surface=lambda: SimpleNamespace(
            get_state=lambda: Gdk.ToplevelState.MINIMIZED))
        _results, (loan,) = self.lent_reply(page, mapped=True)
        self.assertIs(loan.hidden, False)
        self.assertEqual([body for body, _a, _d in page.runs], [REPLY_SCRIPT])

    def test_never_asked_or_lent_once_answered_while_the_page_says(self):
        page = Page()
        results, loans = self.lent_reply(page, mapped=True)
        self.clock.fire(REPLY_GUARD)
        page.answer("hidden", 0)
        self.clock.flush()
        self.assertEqual((results, loans, self.replies(page)), (["unknown"], [], []))

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
        for value in [None, {"result": "mismatch", "why": "unconfirmed"}] + answers:
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
        page.send_reply(7, GOOD, "On my way", [].append,
                        lend=lambda hidden: (setattr(page.view, "mapped", True), Loan())[1])
        self.assertTrue(page.view.mapped)  # in the card
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], False)  # as it was before
        page = Page()
        page.app.seen = True
        self.reply(page)
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], True)

    def test_a_page_that_says_its_hidden_isnt_seen(self):
        # On screen to GTK in a window on that account, and hidden to WebKit: nobody sees it.
        page = Page()
        page.app.seen = True
        _results, loans = self.lent_reply(page, mapped=True)
        page.answer("hidden", 0)
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], False)
        page = Page()
        page.app.seen = True
        _results, loans = self.lent_reply(page, mapped=True)
        page.answer("visible", 0)
        self.assertIs(self.replies(page)[0][1]["request"]["visible"], True)

    def test_the_page_is_told_how_long_ago_its_turn_came(self):
        # Stamped before the loan, which may have WhatsApp close the notification (bridge.js reply).
        clock = [100.0]
        page = Page()
        with mock.patch.object(natter_account, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            def lend(hidden):
                clock[0] += 0.6  # the loan, and WhatsApp reading the chat meanwhile
                return Loan(laid_out=False)
            page.send_reply(7, GOOD, "On my way", [].append, lend=lend)
            clock[0] += 0.15  # laid out a moment later
            page._reply.loan.ready = True
            self.clock.fire(natter_account.REPLY_LAYOUT_TICK / 1000)
        self.assertEqual(self.replies(page)[0][1]["request"]["sinceMs"], 750)

    # ---- What the log may say of the page ------------------------------------------------------

    DIAG = {"visibilityState": "hidden", "rafFired": False, "innerWidth": 1000, "innerHeight": 700, "main": True,
            "header": True, "footer": True, "composer": True, "keys": 0, "chatKeys": 0, "rows": 0, "shape": ""}

    def test_the_diag_is_logged_on_the_same_line(self):
        page = Page()
        _results, (loan,) = self.lent_reply(page)
        page.answer({"result": "mismatch", "why": "unconfirmed", "diag": self.DIAG})
        self.clock.flush()
        self.assertEqual(self.log.getvalue(),
                         "natter: reply: mismatch (unconfirmed) [page hidden, frames no, mapped no, lent yes, "
                         "chat yes, "
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

    SIGNS = {"name": True, "navigated": False, "body": False, "avatar": False, "hasBody": True, "hasIcon": True,
             "nameAt": 2, "nameFirst": False,
             "dataType": "object", "dataKeys": ["chat-id", "msg-key"], "tagShape": "9@a.a",
             "mainAttrs": ["class", "id", "tabindex"], "headerAttrs": ["class"], "keyAttrs": ["class", "data-id"],
             "rowAttrs": ["aria-rowindex", "role"], "values": ["data-id=9a9a9", "data-x=a_a"]}

    def test_the_diag_takes_the_signs_and_names_it_knows(self):
        good = dict(self.DIAG, **self.SIGNS)
        self.assertEqual(reply_diag({"diag": good}), good)
        self.assertEqual(reply_diag({"diag": dict(good, dataKeys=[], values=[], dataType="null")})["values"], [])
        refused = [
            {"dataKeys": ["Mum"]}, {"dataKeys": ["mum dad"]}, {"dataKeys": ["4917612345678"]}, {"dataKeys": ["a123"]},
            {"dataKeys": ["chat-id", "chat-id"]}, {"dataKeys": [f"key-{n}" for n in range(17)]}, {"dataKeys": "id"},
            {"dataKeys": [GOOD]}, {"dataKeys": ["a" * 33]}, {"dataKeys": [1]}, {"mainAttrs": ["data-id=3"]},
            {"rowAttrs": ["Role"]}, {"headerAttrs": ["ä"]},
            {"values": ["data-id"]}, {"values": ["data-id=Mum"]}, {"values": [f"data-id={GOOD}"]},
            {"values": ["Mum=9a"]}, {"values": ["data-id=9"] * 9}, {"values": "data-id=9"},
            {"tagShape": GOOD}, {"tagShape": "Mum"}, {"dataType": "Mum"}, {"dataType": "map"},
            {"name": "yes"}, {"navigated": 1}, {"body": None}, {"avatar": "Mum"}, {"hasBody": "Did you eat?"},
            {"title": "Mum"}, {"body": "Did you eat?"},
            {"nameAt": -2}, {"nameAt": 3}, {"nameAt": 10}, {"nameAt": "Mum"}, {"nameAt": True}, {"nameAt": 1.0}, {"nameFirst": "Mum"},
        ]
        for diag in refused:
            self.assertIsNone(reply_diag({"diag": dict(good, **diag)}), diag)

    FOOTER = {"composerAt": 0, "buttons": 2, "icons": ["a-a-a", "a-a-a-a"], "quote": False, "preview": False}

    def test_the_diag_takes_the_footer(self):
        good = dict(self.DIAG, **self.FOOTER)
        self.assertEqual(reply_diag({"diag": good}), good)
        self.assertEqual(reply_diag({"diag": dict(good, composerAt=-1, icons=[])})["composerAt"], -1)
        refused = [
            {"composerAt": -2}, {"composerAt": natter_account.REPLY_DIAG_BOX_PLACES}, {"composerAt": True},
            {"composerAt": "0"}, {"buttons": -1}, {"buttons": 1000}, {"buttons": "2"},
            {"icons": ["ic-send-filled"]}, {"icons": ["Mum"]}, {"icons": ["a a"]}, {"icons": [""]}, {"icons": "a-a"},
            {"icons": ["a-a", "a-a"]}, {"icons": [f"a{'-a' * n}" for n in range(9)]}, {"icons": [GOOD]},
            {"quote": "yes"}, {"preview": 1},
        ]
        for diag in refused:
            self.assertIsNone(reply_diag({"diag": dict(good, **diag)}), diag)

    def test_the_diags_places_are_the_bridges(self):
        js = JavaScriptCore.Context()
        js.evaluate(WHERE, -1)
        self.assertEqual(js.evaluate('WHERE["header-name"].length', -1).to_int32(), natter_account.REPLY_DIAG_PLACES)
        self.assertEqual(js.evaluate("WHERE.composer.length", -1).to_int32(), natter_account.REPLY_DIAG_BOX_PLACES)
        good = dict(self.DIAG, **self.SIGNS)
        self.assertIsNotNone(reply_diag({"diag": dict(good, nameAt=natter_account.REPLY_DIAG_PLACES - 1)}))
        self.assertIsNone(reply_diag({"diag": dict(good, nameAt=natter_account.REPLY_DIAG_PLACES)}))

    def test_the_diag_line_with_the_footer(self):
        line = diag_line(dict(self.DIAG, **self.FOOTER))
        self.assertIn("key none, box at 0, buttons 2, icons (a-a-a a-a-a-a), quote no, preview no]", line)
        line = diag_line(dict(self.FOOTER, composerAt=-1, icons=[], quote=True))
        self.assertEqual(line, "[box at none, buttons 2, icons (none), quote yes, preview no]")

    def test_the_diag_line_with_the_signs(self):
        line = diag_line(dict(self.DIAG, **self.SIGNS, mapped=True, lent=False))
        self.assertTrue(line.endswith(
            "key none, name yes, opened no, message no, picture no, notified body yes, notified picture yes, "
            "name at 2, name first no, data object (chat-id msg-key), tag 9@a.a, chat attrs (class id tabindex), header attrs (class), "
            "key attrs (class data-id), row attrs (aria-rowindex role), values (data-id=9a9a9 data-x=a_a)]"), line)
        self.assertIn("data null,", diag_line(dict(self.SIGNS, dataType="null", dataKeys=[])))
        self.assertIn("values (none)", diag_line(dict(self.SIGNS, values=[])))
        self.assertIn("name at none,", diag_line(dict(self.SIGNS, nameAt=-1)))

    LIST = {"listed": False, "selected": 1, "selectedName": True, "dirClasses": False, "messageAnywhere": True,
            "messageWords": True, "iconKind": "data", "headerImgKind": "wa"}

    def test_the_diag_takes_the_list_the_classes_and_the_pictures(self):
        good = dict(self.DIAG, **self.SIGNS, **self.LIST)
        self.assertEqual(reply_diag({"diag": good}), good)
        for kind in natter_account.REPLY_DIAG_PICTURE:
            self.assertIsNotNone(reply_diag({"diag": dict(good, iconKind=kind, headerImgKind=kind)}), kind)
        refused = [
            {"listed": "yes"}, {"listed": 1}, {"selected": -1}, {"selected": 1000}, {"selected": True},
            {"selected": "1"}, {"selectedName": "Mum"}, {"dirClasses": None}, {"messageAnywhere": "Did you eat?"},
            {"messageWords": 0}, {"iconKind": "https://pps.whatsapp.net/v/mum.jpg"}, {"iconKind": "ftp"},
            {"iconKind": True}, {"headerImgKind": "blob:file:///mum"}, {"headerImgKind": ""},
        ]
        for diag in refused:
            self.assertIsNone(reply_diag({"diag": dict(good, **diag)}), diag)

    def test_the_diag_line_with_the_list_the_classes_and_the_pictures(self):
        line = diag_line(dict(self.SIGNS, **self.LIST))
        self.assertIn("name yes, opened no, message no, picture no, listed no, notified body yes, notified picture yes, "
                      "selected 1, selected name yes, direction classes no, message anywhere yes, message words yes, "
                      "notified picture kind data, header picture kind wa, name at 2,", line)
        self.assertEqual(diag_line({"listed": True}), "[listed yes]")

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
        self.assertIn('if (besideTheBox(main, box)) return described(["draft", "quote"]);', before_typing)
        # Once, before typing: a link typed brings its preview up there, and that's not theirs. (The
        # log's account of the page looks too, once the answer is given: diagnose.)
        self.assertEqual(len(re.findall(r"(?<!function )besideTheBox\(", SECTION[start:])), 1)

    def test_a_chat_that_takes_no_messages_isnt_busy(self):
        self.assertIn('footerInUse(main, false) ? ["busy", "no-composer"] : described(["cant-send", ""])', SECTION)

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
        self.assertIn("|| previewOpen() ||", before_press)  # looked at again just before Send

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


class ClosedNotificationTest(unittest.TestCase):
    """WhatsApp's notifications as the bridge keeps them (NatterNotification), run as they are: those
    WhatsApp closed are kept as many as those shown, each with when it was closed."""

    def setUp(self):
        notifications = BRIDGE[BRIDGE.index("  const shown = new Map();"):BRIDGE.index("  // ---- Idle audio")]
        self.js = JavaScriptCore.Context()
        self.js.evaluate("const window = globalThis; let clock = 0; const performance = { now: () => clock };"
                         "const post = () => {}; const setTimeout = () => 0;"
                         "class EventTarget { dispatchEvent() { return true; } }"
                         "class Event { constructor(type) { this.type = type; } }\n" + notifications, -1)
        exception = self.js.get_exception()
        self.assertIsNone(exception, exception and exception.get_message())

    def test_closed_ones_kept_as_shown_ones_are(self):
        self.js.evaluate("const made = []; for (let i = 0; i < 310; i++) made.push(new Notification('', {}));"
                         "made.forEach((n, i) => { clock = 1000 + i; n.close(); });", -1)
        self.assertEqual(self.js.evaluate("closed.size", -1).to_int32(), 300)
        self.assertFalse(self.js.evaluate("clickNotification(made[9].id)", -1).to_boolean())  # the oldest go
        self.assertTrue(self.js.evaluate("clickNotification(made[10].id)", -1).to_boolean())
        self.assertEqual(self.js.evaluate("closed.get(made[309].id).closedAt", -1).to_int32(), 1309)
        self.assertEqual(self.js.evaluate("shown.size", -1).to_int32(), 0)


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
  get style() { return { transform: (/transform:\s*([^;]+)/.exec(this.attributes.style || "") || [, ""])[1] }; }
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
  getAttributeNames() { return Object.keys(this.attributes); }
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
  matches(selector) {
    return topLevel(selector, /,/).filter((_, i) => i % 2 === 0).map(complex)
      .some((steps) => matches(this, steps, steps.length - 1));
  }
  closest(selector) {
    for (let element = this; element; element = element.parentElement) if (element.matches(selector)) return element;
    return null;
  }
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


# The bridge's parts the reply leans on from outside its section, as the bridge has them: names
# as Natter compares them, and the open chat's name in a header (titleOf).
NAMES = "\n".join((re.search(r"^  const NAME_LIMIT = .*$", BRIDGE, re.M)[0],
                   re.search(r"^  const INVISIBLE = .*$", BRIDGE, re.M)[0],
                   re.search(r"^  const plainName = .*$", BRIDGE, re.M)[0],
                   BRIDGE[BRIDGE.index("  // Text with WhatsApp's emoji pictures"):
                          BRIDGE.index("  function activeChat()")]))
WHERE = BRIDGE[BRIDGE.index("  const WHERE = {"):BRIDGE.index("  // ---- The chat list")]
# The chat list as the bridge reads it (chatRows), for replies telling two chats of a name apart,
# and whether it shows all the chats (a filter chip on, a search typed).
CHAT_LIST = "\n".join((BRIDGE[BRIDGE.index("  // ---- The chat list"):BRIDGE.index("  // Text with WhatsApp's emoji pictures")],
                       BRIDGE[BRIDGE.index("  function searchBox()"):BRIDGE.index("  function setSearch(")],
                       BRIDGE[BRIDGE.index("  function searching()"):BRIDGE.index("  function focusComposer()")]))


def words(*parts):
    """A message's words as WhatsApp draws them: text, and emoji as pictures (("img", "👍"))."""
    return [{"tag": "img", "attrs": {"alt": part[1], "class": "emoji", "src": "data:image/gif;base64,R0lGOD"},
             "children": []} if isinstance(part, tuple) else {"text": part} for part in parts]


def row(key, *parts, sender=None, styled=None, way="in", said=None):
    """A message's row as the live page has it: div[role=row] > div[data-id] (message-in, or
    message-out for the person's own), in a group its sender's name above its words (on the first
    row of a run of theirs), its words in a span, and its time. `styled`: words shown bold. `said`:
    who sent it, as WhatsApp's data-pre-plain-text has it."""
    text = words(*parts)
    if styled:
        text = [{"tag": "strong", "attrs": {}, "children": words(styled)}] + text
    pre = {"data-pre-plain-text": f"[10:42, 23/10/2026] {said}: "} if said else {}
    inner = [{"tag": "div", "attrs": {"class": "copyable-text", **pre}, "children": [
        {"tag": "span", "attrs": {"dir": "ltr", "class": "selectable-text copyable-text"},
         "children": [{"tag": "span", "attrs": {}, "children": text}]}]}]
    if sender:
        inner.insert(0, {"tag": "div", "attrs": {}, "children": [
            {"tag": "span", "attrs": {"dir": "auto", "class": "sender"}, "children": [{"text": sender}]}]})
    return {"tag": "div", "attrs": {"role": "row"}, "children": [
        {"tag": "div", "attrs": {"data-id": key, "class": f"message-{way} focusable-list-item"}, "children": [
            *inner, {"tag": "span", "attrs": {"class": "time"}, "children": [{"text": "10:42"}]}]}]}


def live_row(key, *parts, sender=None, joined=False, more=False, way=None):
    """A message's row as today's live page may draw it: atomic class names, no message-in or
    message-out (`way`: with that one), data-testid and tabindex on the keyed element, its words in a
    span[dir] (emoji as pictures), a group's sender's name above them, and its time. `joined`: the
    sender's name and the time drawn in the words' own span. `more`: a long message cut short, with
    WhatsApp's "Read more" after it."""
    text = words(*parts)
    time = {"tag": "span", "attrs": {"class": "x1c4vz4f x2lah0s"}, "children": [{"text": "10:42"}]}
    label = [{"tag": "span", "attrs": {"class": "x1lliihq"}, "children": [{"text": sender}]}] if sender else []
    if joined:
        inner = [{"tag": "span", "attrs": {"dir": "ltr", "class": "x1iyjqo2 _ao3e"}, "children": label + text + [time]}]
    else:
        inner = ([{"tag": "div", "attrs": {"class": "x78zum5"}, "children": label}] if label else []) + [
            {"tag": "div", "attrs": {"class": "x9f619"}, "children": [
                {"tag": "span", "attrs": {"dir": "ltr", "class": "x1iyjqo2 _ao3e"},
                 "children": [{"tag": "span", "attrs": {}, "children": text}]}]}]
        if more:
            inner.append({"tag": "div", "attrs": {"role": "button", "class": "x1ypdohk"}, "children": words("Read more")})
        inner.append({"tag": "div", "attrs": {"class": "x13yyeie"}, "children": [time]})
    classes = "x9f619 xjbqb8w" + (f" message-{way}" if way else "")
    return {"tag": "div", "attrs": {"role": "row", "class": "x1n2onr6", "tabindex": "-1"}, "children": [
        {"tag": "div", "attrs": {"data-id": key, "class": classes, "data-testid": "msg-container", "tabindex": "-1"},
         "children": inner}]}


def list_row(i, name, marked=False, on_row=False):
    """A chat in the list as the live page draws it: a listitem at its place, a row in it marked
    aria-selected (or the listitem itself, `on_row`), and the name in a titled span."""
    selected = {"aria-selected": "true" if marked else "false"}
    return {"tag": "div", "attrs": {"role": "listitem", "style": f"transform: translateY({i * 72}px)",
                                    **(selected if on_row else {})}, "children": [
        {"tag": "div", "attrs": {"role": "row", "tabindex": "-1", **({} if on_row else selected)}, "children": [
            {"tag": "div", "attrs": {"data-testid": "cell-frame-title"}, "children": [
                {"tag": "span", "attrs": {"dir": "auto", "title": name}, "children": words(name)}]}]}]}


def chat_list(*names, chip=0, search="", on_row=False, start=0):
    """#side with the chat list: each name a chat, the one starting with "*" marked as open; its search
    box (`search` typed), and its filter chips, the `chip`th on. `start`: the place of the first row
    drawn (the list scrolled down)."""
    rows = [list_row(start + i, name.lstrip("*"), name.startswith("*"), on_row) for i, name in enumerate(names)]
    chips = [{"tag": "button", "attrs": {"role": "tab", "aria-selected": str(i == chip).lower()}, "children": words(word)}
             for i, word in enumerate(("All", "Unread", "Groups"))]
    return {"tag": "div", "attrs": {"id": "side"}, "children": [
        {"tag": "div", "attrs": {"id": "search", "contenteditable": "true", "role": "textbox"},
         "children": words(search) if search else []},
        {"tag": "div", "attrs": {"role": "tablist"}, "children": chips},
        {"tag": "div", "attrs": {"id": "pane-side"}, "children": [
            {"tag": "div", "attrs": {"id": "grid", "role": "grid"}, "children": rows}]}]}


def header(title, picture="", title_attr=True, emoji=None):
    """The open chat's header as the mock draws it: its picture, its name (the name's emoji as a
    picture, `emoji`), and WhatsApp's call buttons."""
    name = words(title, ("img", emoji)) if emoji else words(title)
    children = [{"tag": "span", "attrs": {"dir": "auto", "data-testid": "conversation-info-header-chat-title",
                                          **({"title": title + (emoji or "")} if title_attr else {})},
                 "children": name}]
    if picture:
        children.insert(0, {"tag": "img", "attrs": {"src": picture, "alt": "", "draggable": "false"}, "children": []})
    children.append({"tag": "button", "attrs": {"aria-label": "Voice call", "disabled": ""}, "children": []})
    return {"tag": "header", "attrs": {}, "children": children}


def chat_main(title="", rows=(), picture="", **named):
    """#main for a chat, with its header, rows and an empty message box."""
    return {"tag": "div", "attrs": {"id": "main", "tabindex": "-1"}, "children": [
        header(title, picture, **named) if title else {"tag": "header", "attrs": {}, "children": []},
        {"tag": "div", "attrs": {"id": "messages", "role": "application"}, "children": list(rows)},
        {"tag": "footer", "attrs": {}, "children": [
            {"tag": "div", "attrs": {"contenteditable": "true", "role": "textbox"}, "children": []}]}]}


def bare(n):
    """A message id as live WhatsApp keys its rows: no chat in it."""
    return f"3EB0{n:04X}A1B2C3D4E5F6"


class SameChatTest(unittest.TestCase):
    """The bridge's check that the open chat is the notification's without its id (sameChat), run as
    it is on stand-in chats shaped as the live page's."""

    @classmethod
    def setUpClass(cls):
        cls.js = JavaScriptCore.Context()
        cls.js.evaluate("\n".join((FAKE_PAGE, WHERE, CHAT_LIST, NAMES, SECTION)), -1)
        exception = cls.js.get_exception()
        assert exception is None, exception and exception.get_message()

    def run_js(self, script):
        value = self.js.evaluate(script, -1)
        exception = self.js.get_exception()
        self.assertIsNone(exception, exception and exception.get_message())
        return value

    def same(self, main, title="Mum", body="", icon="", before=None, closed=False, listing=None):
        """sameChat on `main` (a tree) for a notification; before: None (no click), or the chat open
        before the click: "none" (no chat) or its title. closed: the notification clicked was closed.
        listing: the chat list beside it (chat_list), or none."""
        self.run_js(f"load({json.dumps({'tag': 'div', 'attrs': {'id': 'app'}, 'children': [listing, main] if listing else [main]})})")
        names = [{"name": before, "at": 0}] if before and before != "none" else []
        clicked = "null" if before is None else ("{main: %s, names: %s, closed: %s}" % (
            "null" if before == "none" else "{}", json.dumps(names), json.dumps(closed)))
        notification = json.dumps({"title": title, "body": body, "icon": icon, "tag": GOOD})
        return self.run_js(f'sameChat(find("main"), {notification}, {clicked})').to_string()

    def test_the_notified_message_there(self):
        main = chat_main("Mum", [row(bare(1), "Did you eat?"), row(bare(2), "See you")])
        self.assertEqual(self.same(main, body="Did you eat?"), "name+body")
        self.assertEqual(self.same(main, body="Did  you\neat? "), "name+body")  # spaces as they come
        self.assertEqual(self.same(main, body="Did you sleep?"), "")

    def test_its_words_as_whatsapp_draws_them(self):
        main = chat_main("Mum", [row(bare(1), "Yes mum ", ("img", "\U0001F44D")), row(bare(2), " later", styled="see"),
                                 row(bare(3), "snake_case and 2*3")])
        self.assertEqual(self.same(main, body="Yes mum \U0001F44D"), "name+body")  # emoji as pictures
        self.assertEqual(self.same(main, body="*see* later"), "name+body")  # bold, not its marks
        self.assertEqual(self.same(main, body="snake_case and 2*3"), "name+body")  # marks aside on both sides
        main = chat_main("Mum", [row(bare(1), "Love you ", ("img", "❤"))])
        self.assertEqual(self.same(main, body="Love you \u2764\ufe0f"), "name+body")  # U+FE0F or not

    def test_a_body_cut_short(self):
        main = chat_main("Mum", [row(bare(1), "This is a long message about dinner tonight")])
        self.assertEqual(self.same(main, body="This is a long message ab…"), "name+body")
        self.assertEqual(self.same(main, body="This is a long message ab"), "")  # not cut: all of it
        self.assertEqual(self.same(main, body="Another long message ab…"), "")

    def test_only_the_latest_rows(self):
        rows = [row(bare(1), "Did you eat?")] + [row(bare(n), f"filler {n}") for n in range(2, 14)]
        self.assertEqual(self.same(chat_main("Mum", rows), body="Did you eat?"), "")
        self.assertEqual(self.same(chat_main("Mum", rows[1:] + rows[:1]), body="Did you eat?"), "name+body")

    def test_too_short_or_generic_bodies_are_no_sign(self):
        for body in ("k", "\U0001F44D", "\U0001F4F7 Photo", "Photo", "\U0001F3A5 Video",
                     "\U0001F3A4 Voice message (0:12)",
                     "Voice message", "Sticker", "GIF", "\U0001F4C4 report.pdf", "\U0001F4CD Location", "Contact",
                     "\U0001F4CA POLL: Lunch?", "This message was deleted", "3 new messages", "New message",
                     "Missed voice call", "Waiting for this message. This may take a while.", "", "   "):
            main = chat_main("Mum", [row(bare(1), body), row(bare(2), "k"), row(bare(3), "Lunch?")])
            self.assertEqual(self.same(main, body=body), "", body)  # even with the same words there
        # A group's: the words after the sender's name are judged too.
        main = chat_main("Mum", [row(bare(1), "\U0001F4F7 Photo", sender="Ana"), row(bare(2), "k", sender="Ana")])
        for body in ("Ana: \U0001F4F7 Photo", "Ana: k"):
            self.assertEqual(self.same(main, body=body), "", body)

    def test_a_groups_body_after_its_sender(self):
        main = chat_main("Football Sunday", [row(bare(1), "See you at 5", sender="Ana")])

        def same(body, **named):
            return self.same(main, title="Football Sunday", body=body, **named)
        clicked = {"before": "Football Sunday"}  # clicked, with a chat of that name open before
        self.assertEqual(same("Ana: See you at 5", **clicked), "name+body")
        self.assertEqual(same("Ana: See you at 6", **clicked), "")
        self.assertEqual(same("Ana: See you at…", **clicked), "name+body")
        self.assertEqual(same("Bo: See you at 5", **clicked), "name+body")  # the words, whoever said them
        # Not clicked, or a closed one clicked (which may open nothing): the sender's name in the
        # same row too, and more words for a body cut short.
        for strict in ({}, {"before": "Football Sunday", "closed": True}):
            self.assertEqual(same("Ana: See you at 5", **strict), "name+body", strict)
            self.assertEqual(same("Bo: See you at 5", **strict), "", strict)
            self.assertEqual(same("Ana: See you at…", **strict), "", strict)

    def test_a_groups_sender_on_the_first_row_of_their_run(self):
        # WhatsApp names the sender on the first of their messages in a row only: strict, the name
        # is taken from there, never across another sender's run or one of the person's own.
        def same(*rows):
            return self.same(chat_main("Football Sunday", list(rows)), title="Football Sunday",
                             body="Ana: See you at 5")
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana"), row(bare(2), "See you at 5")), "name+body")
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana"), row(bare(2), "Running late"),
                              row(bare(3), "See you at 5")), "name+body")
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana"), row(bare(2), "See you at 5", sender="Bo")), "")
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana"), row(bare(2), "Hello", sender="Bo"),
                              row(bare(3), "See you at 5")), "")
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana"), row(bare(2), "Me too", way="out"),
                              row(bare(3), "See you at 5")), "")
        self.assertEqual(same(row(bare(1), "Ana", sender="Bo"), row(bare(2), "See you at 5")), "")  # words, not a name
        # Another sender's run whose name sits elsewhere: told by who WhatsApp says sent each row.
        other = row(bare(2), "Hello", said="+44 7700 900123")
        other["children"][0]["children"].insert(0, {"tag": "span", "attrs": {"role": "button"}, "children": [{"text": "~ Bo"}]})
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana", said="Ana"), other,
                              row(bare(3), "See you at 5", said="+44 7700 900123")), "")
        self.assertEqual(same(row(bare(1), "Hi all", sender="Ana", said="Ana"), row(bare(2), "Running late", said="Ana"),
                              row(bare(3), "See you at 5", said="Ana")), "name+body")
        self.assertEqual(same(row(bare(1), "See you at 5")), "")  # nobody named

    def test_short_words_only_once_clicked(self):
        # "ok" could be any chat's: a sign only once WhatsApp's click has had its say.
        main = chat_main("Mum", [row(bare(1), "ok"), row(bare(2), "Okay"), row(bare(3), "\U0001F602\U0001F602")])
        for body in ("ok", "\U0001F602\U0001F602"):
            self.assertEqual(self.same(main, body=body, before="Mum"), "name+body", body)
            self.assertEqual(self.same(main, body=body), "", body)
            self.assertEqual(self.same(main, body=body, before="Mum", closed=True), "", body)
        self.assertEqual(self.same(main, body="Okay"), "name+body")  # four letters
        main = chat_main("Mum", [row(bare(1), "Okay see you then")])
        self.assertEqual(self.same(main, body="Ok…", before="Mum"), "name+body")
        self.assertEqual(self.same(main, body="Ok…"), "")
        self.assertEqual(self.same(main, body="Okay see you th…"), "name+body")  # twelve letters

    def test_the_persons_own_messages_are_no_sign(self):
        mine = row(bare(1), "Good afternoon")
        mine["children"][0]["attrs"]["class"] = "message-out focusable-list-item"
        self.assertEqual(self.same(chat_main("Mum", [mine]), body="Good afternoon", before="Mum"), "")
        # Nor a row with neither of WhatsApp's classes, as if it had renamed them: only one that came in.
        mine["children"][0]["attrs"]["class"] = "focusable-list-item"
        self.assertEqual(self.same(chat_main("Mum", [mine]), body="Good afternoon", before="Mum"), "")
        self.assertEqual(self.same(chat_main("Mum", [row(bare(1), "Good afternoon")]), body="Good afternoon",
                                   before="Mum"), "name+body")

    def test_the_name_first(self):
        main = chat_main("Mum", [row(bare(1), "Did you eat?")], picture="blob:file:///mum")
        self.assertEqual(self.same(main, title="Dad", body="Did you eat?", icon="blob:file:///mum", before="none"), "")
        self.assertEqual(self.same(chat_main("", [row(bare(1), "Did you eat?")]), body="Did you eat?"), "")
        self.assertEqual(self.same(main, title="", body="Did you eat?"), "")
        self.assertEqual(self.same(main, title="Mum\u200e ", body="Did you eat?"), "name+body")  # marks aside

    def test_the_name_wherever_the_header_has_it(self):
        # A titled line under the name (a group's members) found first, and a name with no title.
        def chat(*spans):
            main = chat_main("", [row(bare(1), "Did you eat?")])
            main["children"][0]["children"] = [{"tag": "span", "attrs": attrs, "children": children}
                                               for attrs, children in spans]
            return main
        members = ({"title": "Ana, Bo, You"}, words("Ana, Bo, You"))
        name = ({"dir": "auto"}, words("Mum"))
        self.assertEqual(self.same(chat(members, name), body="Did you eat?"), "name+body")
        self.assertEqual(self.same(chat(members), body="Did you eat?"), "")
        # A badge drawn in the name: its icon's words aren't the name's.
        badge = {"tag": "svg", "attrs": {}, "children": [{"tag": "title", "attrs": {}, "children": words("verified")}]}
        self.assertEqual(self.same(chat(({"dir": "auto"}, words("Mum ") + [badge])), body="Did you eat?"),
                         "name+body")

    def test_a_name_with_emoji_as_whatsapp_draws_it(self):
        main = chat_main("Book Club ", [row(bare(1), "Chapter 3 tonight")], title_attr=False, emoji="\U0001F4DA")
        self.assertEqual(self.same(main, title="Book Club \U0001F4DA", body="Chapter 3 tonight"), "name+body")
        self.assertEqual(self.same(main, title="Book Club", body="Chapter 3 tonight"), "")

    def test_the_picture(self):
        for icon in ("blob:file:///mum", "https://pps.whatsapp.net/v/t61/mum.jpg?oh=1&oe=2"):
            main = chat_main("Mum", [row(bare(1), "See you")], picture=icon)
            self.assertEqual(self.same(main, body="Did you eat?", icon=icon), "name+avatar", icon)
            self.assertEqual(self.same(main, body="Did you eat?", icon=icon.replace("mum", "dad")), "", icon)
        self.assertEqual(self.same(chat_main("Mum", [row(bare(1), "See you")], picture="blob:file:///mum"),
                                   body="Did you eat?", icon="blob:file:///mumx"), "")  # a blob: one whole
        # Stand-ins that every chat without a picture may have, and pictures from elsewhere.
        for stand_in in ("data:image/png;base64,iVBORw0KGgo=", "https://static.whatsapp.net/rsrc.php/default.png",
                         "https://example.com/mum.jpg", "https://pps.whatsapp.net.example.com/mum.jpg",
                         "https://x@pps.whatsapp.net/mum.jpg"):
            main = chat_main("Mum", [row(bare(1), "See you")], picture=stand_in)
            self.assertEqual(self.same(main, body="Did you eat?", icon=stand_in), "", stand_in)
        self.assertEqual(self.same(chat_main("Mum", [row(bare(1), "See you")]), body="Did you eat?", icon=""), "")

    def test_the_click_opening_it(self):
        main = chat_main("Mum", [row(bare(1), "See you")])
        self.assertEqual(self.same(main, body="Did you eat?", before="none"), "name+click")  # from no chat
        self.assertEqual(self.same(main, body="Did you eat?", before="Work Team"), "name+click")  # from another
        self.assertEqual(self.same(main, body="Did you eat?", before="Mum"), "")  # one of the same name
        self.assertEqual(self.same(main, body="Did you eat?", before=""), "")  # one with no name to tell
        self.assertEqual(self.same(main, body="Did you eat?"), "")  # no click

    def test_the_strongest_sign_is_said(self):
        main = chat_main("Mum", [row(bare(1), "Did you eat?")], picture="blob:file:///mum")
        self.assertEqual(self.same(main, body="Did you eat?", icon="blob:file:///mum", before="none"), "name+body")
        self.assertEqual(self.same(main, body="Hello?", icon="blob:file:///mum", before="none"), "name+avatar")

    # ---- The list marking the chat open (listed)

    def test_the_list_marking_the_chat_open(self):
        # Clicked, its notification still shown, and the chat open the only one of its name in the
        # list and the one the list marks as open: told by that, with no message or picture to tell it by.
        main = chat_main("Mum", [row(bare(1), "See you")])
        clicked = {"body": "Did you eat?", "before": "Mum"}
        self.assertEqual(self.same(main, listing=chat_list("*Mum", "Work Team"), **clicked), "name+listed")
        self.assertEqual(self.same(main, listing=chat_list("Work Team", "*Mum", on_row=True), **clicked), "name+listed")
        self.assertEqual(self.same(main, listing=chat_list("*Mum\u200e"), **clicked), "name+listed")  # as names compare
        self.assertEqual(self.same(main, listing=chat_list("A", "B", "C", "*Mum", "D"), **clicked), "name+listed")
        # The message still said first, when it's there too.
        there = chat_main("Mum", [row(bare(1), "Did you eat?")])
        self.assertEqual(self.same(there, listing=chat_list("*Mum"), **clicked), "name+body")

    def test_the_list_isnt_enough_when(self):
        main = chat_main("Mum", [row(bare(1), "See you")])
        clicked = {"body": "Did you eat?", "before": "Mum"}
        for listing, why in ((chat_list("*Mum", "Mum"), "two of the name"),
                             (chat_list("Mum", "*Mum"), "two of the name, the other unmarked"),
                             (chat_list("*Mum", "Work Team", chip=1), "a filter chip on"),
                             (chat_list("*Mum", "Work Team", search="Mu"), "a search typed"),
                             (chat_list("Mum", "*Work Team"), "a chat of the name open, another marked"),
                             (chat_list("Mum", "Work Team"), "none marked"),
                             (chat_list("*Mum", "*Work Team"), "two marked"),
                             (chat_list("*Work Team"), "the name not in the list"),
                             (chat_list("A", "B", "C", "D", "*Mum"), "the chat below the list's first four"),
                             (chat_list("A", "*Mum", "B", start=1), "the list's top not drawn"),
                             (None, "no list")):
            self.assertEqual(self.same(main, listing=listing, **clicked), "", why)
        good = chat_list("*Mum", "Work Team")
        self.assertEqual(self.same(main, listing=good, body="Did you eat?"), "")  # not clicked
        self.assertEqual(self.same(main, listing=good, closed=True, **clicked), "")  # closed: may open nothing
        self.assertEqual(self.same(main, listing=good, title="Dad", body="Did you eat?", before="Mum"), "")
        self.assertEqual(self.same(main, listing=chat_list("*Dad"), title="Dad", body="Did you eat?", before="Mum"), "")

    # ---- The message where WhatsApp marks no row with which way it went

    def test_a_message_without_which_way_it_went(self):
        # As the live page now draws them: any of the latest rows, while the list shows all the chats
        # and one of the name (it needn't mark the chat open).
        main = chat_main("Mum", [live_row(bare(1), "Did you eat?"), live_row(bare(2), "See you")])
        sole = chat_list("Mum", "Work Team")
        self.assertEqual(self.same(main, body="Did you eat?", listing=sole, before="Mum"), "name+body")
        # Not for a notification closed, or not clicked: the click may open nothing, and the words
        # may be the person's own.
        self.assertEqual(self.same(main, body="Did you eat?", listing=sole), "")
        self.assertEqual(self.same(main, body="Did you eat?", listing=sole, before="Mum", closed=True), "")
        self.assertEqual(self.same(main, body="Did you sleep?", listing=sole, before="Mum"), "")
        for listing, why in ((None, "no list"), (chat_list("Mum", "Mum"), "two of the name"),
                             (chat_list("Mum", chip=1), "a filter chip on"), (chat_list("Mum", search="Mu"), "a search"),
                             (chat_list("Work Team"), "the name not in the list"),
                             (chat_list("A", "B", "C", "D", "Mum"), "the chat below the list's first four"),
                             (chat_list("A", "Mum", start=1), "the list's top not drawn")):
            self.assertEqual(self.same(main, body="Did you eat?", listing=listing, before="Mum"), "", why)
        # Where WhatsApp's classes are there, as before: only a message that came in.
        for rows in ([live_row(bare(1), "Did you eat?"), live_row(bare(2), "See you", way="in")],
                     [live_row(bare(1), "Did you eat?", way="out")]):
            self.assertEqual(self.same(chat_main("Mum", rows), body="Did you eat?", listing=sole, before="Mum"), "")
        self.assertEqual(self.same(chat_main("Mum", [live_row(bare(1), "Did you eat?", way="in")]), body="Did you eat?",
                                   listing=sole, before="Mum"), "name+body")

    def test_its_words_as_the_live_page_draws_them(self):
        sole = chat_list("Mum", "Football Sunday")

        def same(rows, body, title="Mum", before=None, **named):
            return self.same(chat_main(title, rows), title=title, body=body, listing=sole, before=before or title, **named)
        # The time drawn in the words' own span, emoji as pictures (by alt, or data-plain-text).
        self.assertEqual(same([live_row(bare(1), "Did you eat?", joined=True)], "Did you eat?"), "name+body")
        self.assertEqual(same([live_row(bare(1), "Yes mum ", ("img", "\U0001F44D"), joined=True)], "Yes mum \U0001F44D"),
                         "name+body")
        plain = live_row(bare(1), "Love you ", ("img", "\u2764"))
        emoji = plain["children"][0]["children"][0]["children"][0]["children"][0]["children"][1]
        emoji["attrs"]["data-plain-text"] = emoji["attrs"].pop("alt")
        self.assertEqual(same([plain], "Love you \u2764"), "name+body")
        # Only the row's own time is set aside: words that end with another are still words.
        self.assertEqual(same([live_row(bare(1), "Meet at 10:30", joined=True)], "Meet at 10:30"), "name+body")
        self.assertEqual(same([live_row(bare(1), "Meet at 10:30", joined=True)], "Meet at", before="Mum"), "")
        # A group's sender's name in the same span, before the words.
        group = [live_row(bare(1), "See you at 5", sender="Ana", joined=True)]
        self.assertEqual(same(group, "Bo: See you at 5", title="Football Sunday"), "name+body")
        self.assertEqual(same(group, "Ana: ok", title="Football Sunday"), "")  # other words
        self.assertEqual(same([live_row(bare(1), "ok", sender="Ana", joined=True)], "Ana: ok", title="Football Sunday"),
                         "name+body")  # the sender's own label: short words will do
        # Closed (strict), with WhatsApp's classes: the sender too.
        group = [live_row(bare(1), "See you at 5", sender="Ana", joined=True, way="in")]
        for body, told in (("Ana: See you at 5", "name+body"), ("Bo: See you at 5", "")):
            self.assertEqual(same(group, body, title="Football Sunday", closed=True), told, body)
        # Words that end with the body, with no element of their own before it: not it.
        self.assertEqual(same([live_row(bare(1), "Look ok", joined=True)], "ok", before="Mum"), "")
        self.assertEqual(same([live_row(bare(1), "Look ok")], "ok", before="Mum"), "")
        # A word of the message set apart (bold, a link, an @mention) isn't a sender's label: short
        # words after it are not the body; four letters or more are.
        for tag, first in (("strong", "Look"), ("a", "web.site"), ("span", "@Sam")):
            def apart(rest):
                tree = live_row(bare(1), rest, joined=True)
                tree["children"][0]["children"][0]["children"].insert(0, {"tag": tag, "attrs": {}, "children": words(first)})
                return [tree]
            self.assertEqual(same(apart(" ok"), "ok"), "", tag)
            self.assertEqual(same(apart(" see you"), "see you"), "name+body", tag)
        # A long message WhatsApp shows cut short, with "Read more": enough of it.
        long = "This is a long message about dinner tonight and what we should all bring along"
        self.assertEqual(same([live_row(bare(1), long[:44] + "…", more=True)], long), "name+body")
        self.assertEqual(same([live_row(bare(1), long[:44], more=True)], long), "name+body")
        self.assertEqual(same([live_row(bare(1), long[:14] + "…", more=True)], long, before="Mum"), "")  # too little
        self.assertEqual(same([live_row(bare(1), long[:44] + "…", more=True)], long.replace("dinner", "lunch")), "")

    # ---- The picture by its path

    def test_the_picture_by_its_path(self):
        picture = "https://pps.whatsapp.net/v/t61.24694-24/1234_5678_n.jpg?ccb=11-4&oh=01_A&oe=6720&_nc_sid=e6ed6c"
        main = chat_main("Mum", [row(bare(1), "See you")], picture=picture)
        for icon in ("https://pps.whatsapp.net/v/t61.24694-24/1234_5678_n.jpg?stp=dst-jpg_s96x96&oh=01_B&oe=6721",
                     "https://PPS.whatsapp.net/v/t61.24694-24/1234_5678_n.jpg", picture + "#x"):
            self.assertEqual(self.same(main, body="Did you eat?", icon=icon), "name+avatar", icon)
        for icon in ("https://pps.whatsapp.net/v/t61.24694-24/9999_5678_n.jpg?ccb=11-4&oh=01_A&oe=6720",
                     "https://pps.whatsapp.net/v/t61.24694-24/1234_5678_N.jpg", "https://pps.whatsapp.net/",
                     "https://mmg.whatsapp.net/v/t61.24694-24/1234_5678_n.jpg", "http://pps.whatsapp.net/v/t61.24694-24/1234_5678_n.jpg",
                     "https://x@pps.whatsapp.net/v/t61.24694-24/1234_5678_n.jpg", "blob:" + picture):
            self.assertEqual(self.same(main, body="Did you eat?", icon=icon), "", icon)
        # WhatsApp's static host, a stand-in many chats may have: never, at any address.
        stand_in = "https://static.whatsapp.net/rsrc.php/v4/default.png?x=1"
        main = chat_main("Mum", [row(bare(1), "See you")], picture=stand_in)
        self.assertEqual(self.same(main, body="Did you eat?", icon=stand_in.replace("x=1", "x=2")), "")

    def test_kinds_of_picture_address(self):
        for address, kind in (("data:image/png;base64,iVBO", "data"), ("blob:https://web.whatsapp.com/1-2", "blob"),
                              ("https://pps.whatsapp.net/v/x.jpg?oh=1", "wa"), ("https://static.whatsapp.net/a.png", "wa"),
                              ("https://example.com/a.jpg", "https-other"), ("https://whatsapp.net.example.com/a", "https-other"),
                              ("http://pps.whatsapp.net/a.jpg", "https-other"), ("", "none"), (None, "none")):
            self.assertEqual(self.run_js(f"pictureKind({json.dumps(address)})").to_string(), kind, address)

    def test_sent_seen(self):
        def seen(keys_before, keys_after, box="", words="x", way="out"):
            main = chat_main("Mum", [row(key, "x", way=way) for key in keys_after])
            if box:
                main["children"][2]["children"][0]["children"] = [{"text": box}]
            self.run_js(f"load({json.dumps({'tag': 'div', 'attrs': {'id': 'app'}, 'children': [main]})})")
            before = ("{own: new Set(%s), ids: new Set(%s), keyed: %s, words: %s}"
                      % (json.dumps([k for k in keys_before if k.startswith(f"true_{GOOD}_")]), json.dumps(keys_before),
                         json.dumps(any(k.startswith(("true_", "false_")) for k in keys_before)), json.dumps(words)))
            return self.run_js(f'sentSince(find("main"), {json.dumps(GOOD)}, {before})').to_boolean()
        self.assertTrue(seen([bare(1)], [bare(1), bare(2)], box="On my way"))  # a new message with the words, bare keys
        # A new one with the words, marked as coming in: theirs, not the one sent.
        self.assertFalse(seen([bare(1)], [bare(1), bare(2)], box="On my way", way="in"))
        self.assertTrue(seen([bare(1)], [bare(1), bare(2)], box="On my way", way="plain"))  # no mark: as before
        # Theirs coming in meanwhile, with other words, and the words still in the box: not sent.
        self.assertFalse(seen([bare(1)], [bare(1), bare(2)], box="On my way", words="On my way"))
        self.assertFalse(seen([bare(1)], [bare(1)], box="On my way"))
        self.assertTrue(seen([bare(1)], [bare(1)]))  # the box emptied
        old = f"false_{GOOD}_3EB01"
        self.assertFalse(seen([old], [old, f"false_{GOOD}_3EB02"], box="On my way"))  # theirs, keys with ids
        self.assertTrue(seen([old], [old, f"true_{GOOD}_3EB03"], box="On my way"))  # the person's own

    def test_what_the_log_may_say(self):
        main = chat_main("Mum", [row(f"false_{GOOD}_3EB0AB12", "Did you eat?")], picture="blob:file:///mum")
        main["attrs"]["data-chat"] = GOOD
        main["attrs"]["data-447700900104"] = "x"  # a name like an id: never said
        main["attrs"]["aria-label"] = "Mum: mail jo_x@y"  # words: never their shape either
        self.run_js(f"load({json.dumps({'tag': 'div', 'attrs': {'id': 'app'}, 'children': [main]})})")
        notification = {"title": "Mum", "body": "Did you eat?", "icon": "", "tag": GOOD,
                        "data": {"chatId": GOOD, GOOD: 1, "msgKey": "x", "Mum": 2, "id": 3}}
        self.run_js("let told = null; document.visibilityState = 'hidden';"
                    "let requestAnimationFrame = () => 1, timers = [], innerWidth = 1000, innerHeight = 700;"
                    "function setTimeout(run) { timers.push(run); return 1; }"
                    f"diagnose({json.dumps(notification)}, {{main: null, title: ''}})"
                    ".then((facts) => { told = facts; });"
                    "timers.forEach((run) => run());")
        self.run_js("1")  # the promise's turn
        diag = json.loads(self.run_js("JSON.stringify(told)").to_string())
        self.assertEqual({key: diag[key] for key in ("name", "navigated", "body", "avatar", "hasBody", "hasIcon",
                                                     "dataType", "dataKeys", "tagShape")},
                         {"name": True, "navigated": True, "body": True, "avatar": False, "hasBody": True,
                          "hasIcon": False, "dataType": "object", "dataKeys": ["chat-id", "id", "msg-key"],
                          "tagShape": "9@a.a"})
        self.assertEqual((diag["nameAt"], diag["nameFirst"]), (0, True))
        self.assertEqual(diag["mainAttrs"], ["aria-label", "data-chat", "id", "tabindex"])
        self.assertEqual(diag["headerAttrs"], [])
        self.assertEqual(diag["keyAttrs"], ["class", "data-id"])
        self.assertEqual(diag["rowAttrs"], ["role"])
        self.assertEqual(diag["values"], ["data-chat=9@a.a", "data-id=a_9@a.a_9a9a9"])
        self.assertEqual(reply_diag({"diag": diag}), diag)
        for value in json.dumps(diag), diag_line(diag):
            for secret in ("Mum", "eat", "447700900104", "4917612345678", "3EB0", "blob:", "file:", "mum"):
                self.assertNotIn(secret, value)


# The page's clock, timers and observers for a whole reply run in JavaScriptCore: time moves only
# when the test says (step), each timer runs in a turn of its own with its promises after it, an
# animation frame comes only while frames are on (a page WebKit draws), and the observers hear of
# a change when the test says the page changed (mutate).
FAKE_RUNTIME = r"""
let now = 0, nextTimer = 1, frames = false, clicked = 0, onClick = null;
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
const shown = new Map(), closed = new Map();
// WhatsApp's own handler for its notification: whatever the test has it do (onClick).
function clickNotification(id) { clicked++; if (onClick) onClick(); mutate(); }
function calling() { return false; }
function liveStreams() { return false; }
const innerWidth = 1000, innerHeight = 700;
// A message row (a tree, as the tests' row()), into the open chat.
function addRow(tree) {
  add(find("main").querySelector("#messages"), [tree]);
  mutate();
}
// The open chat's header, drawn anew (a tree, as the tests' header()).
function setHeader(tree) {
  const old = find("main").querySelector("header");
  old.childNodes = [];
  add(old, tree.children);
  mutate();
}
// #main out of the page, and back.
let gone = null;
function closeChat() { gone = find("main"); gone.remove(); mutate(); }
function reopenChat() { gone.parentElement.childNodes.push(gone); mutate(); }
// Typing, for a run past the check: the box's focus and caret, the page's editing commands (into
// the box with the focus, while `typing`), a turn of the event loop, and a button's press (its
// click, then onPress). Every element is on show, its words its innerText.
let typing = true, pressed = 0, onPress = null;
FakeElement.prototype.focus = function () { document.activeElement = this; };
FakeElement.prototype.click = function () { pressed++; if (onPress) onPress(this); mutate(); };
FakeElement.prototype.getClientRects = function () { return [1]; };
FakeElement.prototype.getBoundingClientRect = function () { return { height: 20 }; };
Object.defineProperty(FakeElement.prototype, "innerText", { get() { return this.textContent; } });
const HTMLElement = FakeElement;
FakeElement.prototype.createRange = function () {
  return { endContainer: null, selectNodeContents(node) { this.endContainer = node; }, collapse() {} };
};
FakeElement.prototype.execCommand = function (command, _ui, text) {
  const box = this.activeElement;
  if (!typing || !box) return false;
  add(box, [command === "insertLineBreak" ? { tag: "br", attrs: {}, children: [] } : { text }]);
  mutate();
  return true;
};
const selection = {
  ranges: [], get rangeCount() { return this.ranges.length; }, removeAllRanges() { this.ranges = []; },
  addRange(range) { this.ranges.push(range); }, getRangeAt(i) { return this.ranges[i]; },
};
function getSelection() { return selection; }
class MessageChannel {
  constructor() {
    const port1 = { onmessage: null, close() {} };
    this.port1 = port1;
    this.port2 = { postMessage() { setTimeout(() => port1.onmessage && port1.onmessage({}), 0); } };
  }
}
let answer = null, asked = null, answeredAt = null;
// A reply to the notification 7, from `chat`, saying `said` ({title, body, icon}), with `more` in
// the request.
function ask(chat, budget, said = {}, more = {}) {
  asked = { pageId: 7, chatId: chat, text: "On my way", budgetMs: budget, visible: false, ...more };
  shown.set(7, { tag: chat, title: "", body: "", icon: "", data: null, ...said });
  reply(asked).then((result) => { answer = result; answeredAt = now; });
}
"""


class ReplyWaitTest(unittest.TestCase):
    """The bridge's reply run as it is, from WhatsApp's notification click to the check of the chat
    that opened, on the mock page's own markup with the page's clock in the test's hands: how long
    it waits for the chat's messages, how it tells the chat, and what it tells Natter's log when the
    chat isn't the one. Most runs that get past the check stop at the box (words of the person's own
    in it: "draft"), or for want of a box ("cant-send"), with how they told the chat; those with a
    Send button (send_button) type, and press it."""

    BUDGET = REPLY_BUDGET_MS
    MUM = {"title": "Mum", "body": "Did you eat?"}

    def setUp(self):
        self.js = JavaScriptCore.Context()
        self.run_js("\n".join((FAKE_PAGE, FAKE_RUNTIME, WHERE, CHAT_LIST, NAMES, SECTION)))
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

    def header(self, title, **named):
        self.run_js(f"setHeader({json.dumps(header(title, **named))})")

    def add_row(self, tree):
        self.run_js(f"addRow({json.dumps(tree)})")

    def words_in_the_box(self):
        """Words of the person's own in the box: a run past the check stops there, as "draft"."""
        self.run_js('add(find("main").querySelector("footer [contenteditable]"), [{text: "my own"}])')

    def run_reply(self, said=None, chat=GOOD, until=None):
        self.run_js(f"ask({json.dumps(chat)}, {self.BUDGET}, {json.dumps(said or self.MUM)})")
        self.until(until or self.BUDGET + 1000)
        return self.answer()

    # ---- Each sign of the chat, without ids -----------------------------------------------------

    def test_open_already_with_the_message_there(self):
        # Clicked all the same (WhatsApp's handler does nothing with its chat open), and told once
        # WhatsApp has had a moment to open another.
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.words_in_the_box()
        answer, why, diag, when = self.run_reply()
        self.assertEqual((answer, why, diag), ("draft", "name+body", None))
        self.assertEqual(self.run_js("clicked").to_int32(), 1)
        self.assertLess(when, 2000)

    def test_open_already_with_its_picture(self):
        self.header("Mum", picture="blob:file:///mum")
        self.add_row(row(bare(1), "See you"))
        self.words_in_the_box()
        answer, why, _diag, _when = self.run_reply(dict(self.MUM, icon="blob:file:///mum"))
        self.assertEqual((answer, why), ("draft", "name+avatar"))
        self.assertEqual(self.run_js("clicked").to_int32(), 1)

    def test_open_already_by_its_keys_isnt_clicked(self):
        self.run_js('find("main").querySelector("footer").remove()')
        self.add_row(row(f"false_{GOOD}_3EB0AB12", "Did you eat?"))
        self.assertEqual(self.run_reply()[:2], ("cant-send", "ids"))
        self.assertEqual(self.run_js("clicked").to_int32(), 0)

    def switch_to(self, rows, footer=False):
        """WhatsApp's open of another chat of the same name in #main: its header drawn anew, its rows,
        and its footer (none: it takes no messages, so a run that gets this far says "cant-send")."""
        script = (f'find("main").querySelector("#messages").childNodes = []; '
                  f'setHeader({json.dumps(header("Mum"))}); '
                  + "".join(f"addRow({json.dumps(tree)}); " for tree in rows))
        if not footer:
            script += 'find("main").querySelector("footer").remove(); mutate();'
        return script

    def test_another_chat_of_the_same_name_with_the_same_words_open(self):
        # Another "Mum" open, with the notified words in it and the person's own words in its box:
        # never taken for the one notified. WhatsApp's click opens that one a moment later.
        for delay in (0, 50, 400, 900):
            self.setUp()
            self.header("Mum")
            self.add_row(row(bare(1), "Did you eat?"))
            self.words_in_the_box()
            switch = self.switch_to([row(bare(2), "Did you eat?")])
            self.run_js(f"onClick = () => setTimeout(() => {{ {switch} }}, {delay})")
            answer, why, _diag, _when = self.run_reply()
            self.assertEqual((answer, why), ("cant-send", "name+body"), delay)
            self.assertEqual(self.run_js("clicked").to_int32(), 1)

    def test_the_same_short_words_in_another_chat_of_the_same_name(self):
        self.header("Mum")
        self.add_row(row(bare(1), "ok"))
        self.words_in_the_box()
        self.run_js(f"onClick = () => setTimeout(() => {{ {self.switch_to([row(bare(2), 'ok')])} }}, 300)")
        self.assertEqual(self.run_reply(dict(self.MUM, body="ok"))[:2], ("cant-send", "name+body"))

    def test_open_already_by_name_alone_is_clicked_and_checked_again(self):
        # Its name, nothing else: clicked, and WhatsApp's handler opens the chat with the message.
        self.header("Mum")
        self.words_in_the_box()
        self.run_js(f"onClick = () => addRow({json.dumps(row(bare(1), 'Did you eat?'))})")
        answer, why, _diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("draft", "name+body"))
        self.assertEqual(self.run_js("clicked").to_int32(), 1)

    def test_opened_by_the_click_from_another_chat(self):
        self.header("Work Team")
        self.add_row(row(bare(1), "Standup at 10"))
        self.words_in_the_box()
        self.run_js(f"onClick = () => setTimeout(() => setHeader({json.dumps(header('Mum'))}), 300)")
        answer, why, _diag, when = self.run_reply()
        self.assertEqual((answer, why), ("draft", "name+click"))
        self.assertLess(when, 2000)

    def test_opened_by_the_click_from_no_chat(self):
        self.header("Mum")
        self.words_in_the_box()
        self.run_js("closeChat()")
        self.run_js("onClick = () => setTimeout(reopenChat, 300)")
        answer, why, _diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("draft", "name+click"))

    def close_it(self, said):
        """The reply asked for, with WhatsApp having closed its notification (the chat read)."""
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET}, {json.dumps(said)}); "
                    "closed.set(7, shown.get(7)); shown.delete(7)")
        self.until(self.BUDGET + 1000)
        return self.answer()

    def test_a_closed_notification_still_says_what_it_said(self):
        # WhatsApp closed it (the chat read): still clicked, and the open chat still told by it.
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.words_in_the_box()
        self.assertEqual(self.close_it(self.MUM)[:2], ("draft", "name+body"))
        self.assertEqual(self.run_js("clicked").to_int32(), 1)
        # Its chat opened by the click, from another.
        self.setUp()
        self.header("Work Team")
        self.words_in_the_box()
        self.run_js(f"onClick = () => setHeader({json.dumps(header('Mum'))})")
        self.assertEqual(self.close_it(self.MUM)[:2], ("draft", "name+click"))
        self.run_js("answer = null; closed.clear()")
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET}, {json.dumps(self.MUM)}); shown.delete(7)")
        self.until(2 * self.BUDGET + 2000)
        self.assertEqual(self.answer()[:2], ("gone", "notification"))

    def test_closed_after_it_was_asked_counts_as_shown(self):
        # Lending the page had WhatsApp read the chat and close the notification after the reply was
        # asked: as a notification still shown, short words will do. Closed before: they won't.
        for closed_at, found in ((800, ("draft", "name+body")), (400, ("mismatch", "unconfirmed"))):
            self.setUp()
            self.header("Mum")
            self.add_row(row(bare(1), "ok"))
            self.words_in_the_box()
            self.run_js(f"now = 1000; ask({json.dumps(GOOD)}, {self.BUDGET}, {json.dumps(dict(self.MUM, body='ok'))}, "
                        f"{{sinceMs: 500}}); shown.get(7).closedAt = {closed_at}; closed.set(7, shown.get(7)); "
                        "shown.delete(7)")
            self.until(self.BUDGET + 2000)
            self.assertEqual(self.answer()[:2], found, closed_at)

    # ---- Typed and sent --------------------------------------------------------------------------

    SEND = {"tag": "button", "attrs": {"aria-label": "Send"}, "children": [
        {"tag": "span", "attrs": {"data-icon": "wds-ic-send-filled"}, "children": []}]}
    MIC = {"tag": "button", "attrs": {"aria-label": "Voice message"}, "children": [
        {"tag": "span", "attrs": {"data-icon": "ic-mic-outlined"}, "children": []}]}
    FOOTER = ("footer", "composer", "composerAt", "buttons", "icons", "quote", "preview")

    def send_button(self, button=None, empties=True):
        """A button in the footer (Send, unless `button`); Send pressed empties the box (`empties`)."""
        self.run_js(f'add(find("main").querySelector("#send-slot"), [{json.dumps(button or self.SEND)}])')
        if empties:
            self.run_js('onPress = () => { find("main").querySelector("footer [contenteditable]").childNodes = []; }')

    def list_rows(self, *names, start=0):
        """The chat list's rows, as the live page draws them (list_row): each name in a titled span,
        the one starting with "*" marked as the chat open; the first at place `start`."""
        rows = [list_row(start + i, name.lstrip("*"), name.startswith("*")) for i, name in enumerate(names)]
        self.run_js(f'add(find("pane").querySelector("#grid"), {json.dumps(rows)})')

    def assert_private(self, diag):
        self.assertEqual(reply_diag({"diag": diag}), diag)
        for value in json.dumps(diag), diag_line(diag):
            for secret in ("Mum", "eat", "On my way", "mic", "send", "3EB0", "4917612345678"):
                self.assertNotIn(secret, value)

    def test_typed_and_sent(self):
        self.list_rows("Mum", "Work Team")
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.send_button()
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why, diag), ("sent", "name+body", None))
        self.assertEqual(self.run_js("pressed").to_int32(), 1)

    def test_two_of_the_name_in_the_list_and_a_click_that_opens_nothing(self):
        # Another "Mum" may be open with the same words, and WhatsApp's click open nothing: which of
        # the two can't be told, so nothing is typed.
        self.list_rows("Mum", "Work Team", "Mum")
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.send_button()
        diag = self.assert_mismatch(self.run_reply(), "ambiguous")
        self.assertEqual((diag["name"], diag["navigated"], diag["body"]), (True, False, True))
        self.assertEqual(self.run_js("pressed").to_int32(), 0)
        # Its picture, rather than its words: the same.
        self.setUp()
        self.list_rows("Mum", "Mum")
        self.header("Mum", picture="blob:file:///mum")
        self.add_row(row(bare(1), "See you"))
        self.send_button()
        self.assert_mismatch(self.run_reply(dict(self.MUM, icon="blob:file:///mum")), "ambiguous")
        self.assertEqual(self.run_js("pressed").to_int32(), 0)

    def test_a_list_showing_only_some_chats(self):
        # A filter chip on, or a search typed: the other chat of the name may be left out of the list,
        # so one the click didn't open isn't taken, even with one of the name in it.
        for script in ('find("side").querySelectorAll(\'[role="tab"]\').forEach((chip, i) => '
                       'chip.attributes["aria-selected"] = String(i === 1))',
                       'add(find("search"), [{text: "Mu"}])'):
            self.setUp()
            self.list_rows("Mum", "Work Team")
            self.header("Mum")
            self.add_row(row(bare(1), "Did you eat?"))
            self.send_button()
            self.run_js(script)
            self.assert_mismatch(self.run_reply(), "filtered")
            self.assertEqual(self.run_js("pressed").to_int32(), 0)
        # The click opening it, from another name: told by that, whatever the list shows.
        self.setUp()
        self.list_rows("Work Team")
        self.header("Work Team")
        self.add_row(row(bare(1), "Standup at 10"))
        self.send_button()
        self.run_js('add(find("search"), [{text: "Mu"}]); onClick = () => { '
                    'find("main").querySelector("#messages").childNodes = []; '
                    f'setHeader({json.dumps(header("Mum"))}); addRow({json.dumps(row(bare(2), "Did you eat?"))}); }}')
        self.assertEqual(self.run_reply()[:2], ("sent", "name+body"))

    def test_two_of_the_name_and_a_click_slow_to_open(self):
        # The click opens the one notified only after the check: nothing typed in either.
        self.list_rows("Mum", "Mum")
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.send_button()
        switch = self.switch_to([row(bare(2), "Did you eat?")], footer=True)
        self.run_js(f"onClick = () => setTimeout(() => {{ {switch} }}, 2000)")
        self.assert_mismatch(self.run_reply(), "ambiguous")
        self.assertEqual(self.run_js("pressed").to_int32(), 0)

    def test_two_of_the_name_and_the_click_opening_it(self):
        # The click opened it, from a chat of another name: told by that, whatever the list holds.
        self.list_rows("Mum", "Mum", "Work Team")
        self.header("Work Team")
        self.add_row(row(bare(1), "Standup at 10"))
        self.send_button()
        self.run_js(f'onClick = () => {{ find("main").querySelector("#messages").childNodes = []; '
                    f'setHeader({json.dumps(header("Mum"))}); addRow({json.dumps(row(bare(2), "Did you eat?"))}); }}')
        self.assertEqual(self.run_reply()[:2], ("sent", "name+body"))
        self.assertEqual(self.run_js("pressed").to_int32(), 1)

    def test_told_again_by_its_signs_just_before_send(self):
        # Told by its message, with the click opening nothing: just before Send the message must still
        # be there. Gone (another chat of the name drawn in the same place), and Send isn't pressed.
        self.list_rows("Mum", "Work Team")
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.later(3000, 'find("main").querySelector("#messages").childNodes = []; '
                         f'add(find("main").querySelector("#send-slot"), [{json.dumps(self.SEND)}]); mutate();')
        self.assertEqual(self.run_reply()[:2], ("left-as-draft", "moved"))
        self.assertEqual(self.run_js("pressed").to_int32(), 0)

    # ---- The owner's live page (0.1.1): no direction classes, the click opening nothing new ------

    def test_the_owners_live_case(self):
        # The right chat open in the main window, its row marked in the list, its messages without
        # message-in or message-out, and WhatsApp's click opening nothing new (it was open): sent, by
        # the notified message where it shows, and by the list where it doesn't.
        for rows, how in (([live_row(bare(1), "Hi"), live_row(bare(2), "Did you eat?")], "name+body"),
                          ([live_row(bare(1), "Hi"), live_row(bare(2), "\U0001F4F7 Photo")], "name+listed")):
            self.setUp()
            self.list_rows("Work Team", "*Mum", "Football Sunday")
            self.header("Mum", picture="https://pps.whatsapp.net/v/t61/mum.jpg?oh=1")
            for tree in rows:
                self.add_row(tree)
            self.send_button()
            said = dict(self.MUM, icon="data:image/png;base64,iVBORw0KGgo=")
            self.assertEqual(self.run_reply(said)[:2], ("sent", how), how)
            self.assertEqual((self.run_js("clicked").to_int32(), self.run_js("pressed").to_int32()), (1, 1), how)

    def test_the_message_alone_without_direction_classes(self):
        # No row marked in the list: the message, with one chat of the name in it, is enough.
        self.list_rows("Mum", "Work Team")
        self.header("Mum")
        self.add_row(live_row(bare(1), "Did you eat?"))
        self.send_button()
        self.assertEqual(self.run_reply()[:2], ("sent", "name+body"))

    def assert_refused(self, found, why="unconfirmed"):
        diag = self.assert_mismatch(found, why)
        self.assertEqual(self.run_js("pressed").to_int32(), 0)
        self.assert_private(diag)
        return diag

    def test_listed_traps(self):
        # Each with the chat of that name open, the click opening nothing new, and no message or
        # picture to tell it by but the list (and, without direction classes, the words).
        def trap(*names, body="Did you eat?", script="", close=False, start=0):
            self.setUp()
            self.list_rows(*names, start=start)
            self.header("Mum")
            self.add_row(live_row(bare(1), "Did you eat?"))
            self.send_button()
            if script:
                self.run_js(script)
            said = dict(self.MUM, body=body)
            return self.close_it(said) if close else self.run_reply(said)
        # Two of the name in the list, one marked: neither the list nor the words.
        diag = self.assert_refused(trap("*Mum", "Mum"))
        self.assertEqual({key: diag[key] for key in ("name", "navigated", "body", "avatar", "listed", "selected",
                                                     "selectedName", "dirClasses", "messageAnywhere", "messageWords")},
                         {"name": True, "navigated": False, "body": False, "avatar": False, "listed": False,
                          "selected": 1, "selectedName": True, "dirClasses": False, "messageAnywhere": True,
                          "messageWords": True})
        # A filter chip on, or a search typed.
        diag = self.assert_refused(trap("*Mum", "Work Team", script='find("side").querySelectorAll(\'[role="tab"]\')'
                                        '.forEach((chip, i) => { chip.attributes["aria-selected"] = String(i === 1); })'))
        self.assertEqual((diag["listed"], diag["selected"], diag["selectedName"]), (False, 1, True))
        self.assert_refused(trap("*Mum", "Work Team", script='add(find("search"), [{text: "Mu"}])'))
        # A notification closed before the reply was asked: no list (it may open nothing), and the
        # words only as a closed one's (here, other words).
        diag = self.assert_refused(trap("*Mum", "Work Team", body="Are you home yet?", close=True))
        self.assertEqual((diag["listed"], diag["selectedName"], diag["messageAnywhere"]), (False, True, False))
        # A chat of the name open, but another row marked as open, or none.
        diag = self.assert_refused(trap("Mum", "*Work Team", body="Are you home yet?"))
        self.assertEqual((diag["listed"], diag["selected"], diag["selectedName"]), (False, 1, False))
        diag = self.assert_refused(trap("Mum", "Work Team", body="Are you home yet?"))
        self.assertEqual((diag["listed"], diag["selected"]), (False, 0))
        # Two rows marked.
        self.assert_refused(trap("*Mum", "*Work Team", body="Are you home yet?"))
        # Without direction classes, the words with two of the name in the list: no.
        diag = self.assert_refused(trap("Mum", "Work Team", "Mum"))
        self.assertEqual((diag["body"], diag["messageAnywhere"], diag["dirClasses"]), (False, True, False))
        # The list scrolled down to an old chat of the name, open and marked, the list's top not drawn:
        # another of the name, just written to, may be up there. Neither the list nor the words.
        diag = self.assert_refused(trap("Work Team", "*Mum", "Football Sunday", start=30))
        self.assertEqual((diag["listed"], diag["body"], diag["selected"], diag["selectedName"], diag["messageAnywhere"]),
                         (False, False, 1, True, True))
        # The top drawn, but the chat far below it: not one just written to.
        self.assert_refused(trap("A", "B", "C", "D", "E", "*Mum", body="Are you home yet?"))
        self.assert_refused(trap("A", "B", "C", "D", "E", "Mum"))

    def test_listed_told_again_just_before_send(self):
        # Told by the list: just before Send the list must still mark it. Another row marked by then
        # (another chat open in its place), and Send isn't pressed.
        self.list_rows("*Mum", "Work Team")
        self.header("Mum")
        self.add_row(live_row(bare(1), "See you"))
        self.later(3000, f'add(find("main").querySelector("#send-slot"), [{json.dumps(self.SEND)}]); '
                         'find("pane").querySelectorAll(\'[role="row"]\').forEach((row, at) => { '
                         'row.attributes["aria-selected"] = String(at === 1); }); mutate();')
        self.assertEqual(self.run_reply()[:2], ("left-as-draft", "moved"))
        self.assertEqual(self.run_js("pressed").to_int32(), 0)
        # The list still marking it: sent.
        self.setUp()
        self.list_rows("*Mum", "Work Team")
        self.header("Mum")
        self.add_row(live_row(bare(1), "See you"))
        self.later(3000, f'add(find("main").querySelector("#send-slot"), [{json.dumps(self.SEND)}]); mutate();')
        self.run_js('onPress = () => { find("main").querySelector("footer [contenteditable]").childNodes = []; }')
        self.assertEqual(self.run_reply()[:2], ("sent", "name+listed"))

    def test_the_words_without_direction_classes_told_again_just_before_send(self):
        # Told by the words, with one of the name in the list: a second of the name drawn by then
        # (the list scrolled), and Send isn't pressed.
        self.list_rows("Mum", "Work Team")
        self.header("Mum")
        self.add_row(live_row(bare(1), "Did you eat?"))
        self.later(3000, f'add(find("main").querySelector("#send-slot"), [{json.dumps(self.SEND)}]); '
                         f'add(find("pane").querySelector("#grid"), [{json.dumps(list_row(2, "Mum"))}]); mutate();')
        self.assertEqual(self.run_reply()[:2], ("left-as-draft", "moved"))
        self.assertEqual(self.run_js("pressed").to_int32(), 0)

    def test_the_kinds_of_the_pictures(self):
        # The log says what kind of address each picture had, never the address.
        self.list_rows("*Mum")
        self.header("Mum", picture="https://pps.whatsapp.net/v/t61/mum.jpg?oh=1")
        self.add_row(row(bare(1), "See you"))
        diag = self.assert_refused(self.close_it(dict(self.MUM, icon="data:image/png;base64,iVBORw0KGgo=")))
        self.assertEqual((diag["iconKind"], diag["headerImgKind"], diag["avatar"], diag["dirClasses"]),
                         ("data", "wa", False, True))
        for value in json.dumps(diag), diag_line(diag):
            for secret in ("pps", "whatsapp.net", "iVBOR", "base64"):
                self.assertNotIn(secret, value)

    # ---- After the check: what the log has of the footer ----------------------------------------

    def test_a_message_being_replied_to(self):
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.run_js('{ const footer = find("main").querySelector("footer"); footer.childNodes = []; add(footer, '
                    + json.dumps([{"tag": "div", "attrs": {"class": "quote"}, "children": words("Did you eat?")},
                                  {"tag": "div", "attrs": {}, "children": [
                                      {"tag": "div", "attrs": {"contenteditable": "true", "role": "textbox"},
                                       "children": []}, self.MIC]}]) + "); }")
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("draft", "quote"))
        self.assertEqual({key: diag[key] for key in self.FOOTER},
                         {"footer": True, "composer": True, "composerAt": 0, "buttons": 1, "icons": ["a-a-a"],
                          "quote": True, "preview": False})
        self.assert_private(diag)

    def test_no_send_button(self):
        # Only the voice message button, and the box found by its last place (no role).
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.run_js('delete find("main").querySelector("footer [contenteditable]").attributes.role')
        self.send_button(self.MIC, empties=False)
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("left-as-draft", "no-button"))
        self.assertEqual({key: diag[key] for key in self.FOOTER},
                         {"footer": True, "composer": True, "composerAt": 2, "buttons": 1, "icons": ["a-a-a"],
                          "quote": False, "preview": False})
        self.assert_private(diag)

    def test_words_that_dont_land(self):
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.send_button()
        self.run_js("typing = false")
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("left-as-draft", "text"))
        self.assertEqual((diag["composerAt"], diag["buttons"], diag["icons"]), (0, 1, ["a-a-a-a"]))
        self.assert_private(diag)

    def test_send_not_seen_to_go(self):
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        self.send_button(empties=False)
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("unsure", "name+body"))
        self.assertEqual(self.run_js("pressed").to_int32(), 1)
        self.assertEqual((diag["composerAt"], diag["buttons"], diag["quote"], diag["preview"]), (0, 1, False, False))
        self.assert_private(diag)

    # ---- Wrong chats: never typed in -------------------------------------------------------------

    def assert_mismatch(self, found, why):
        answer, told, diag, _when = found
        self.assertEqual((answer, told), ("mismatch", why))
        self.assertTrue(self.run_js('boxEmpty(composerIn(find("main")))').to_boolean())
        self.assertEqual(reply_diag({"diag": diag}), diag)
        return diag

    def test_a_closed_notification_that_opens_nothing(self):
        # Its click may open nothing: a group of the same name open, with the words from another
        # sender, or only short ones, isn't the one.
        said = {"title": "Family", "body": "Mum: Good morning"}
        self.header("Family")
        self.add_row(row(bare(1), "Good morning", sender="Dad"))
        self.assert_mismatch(self.close_it(said), "unconfirmed")
        self.setUp()
        self.header("Family")
        self.add_row(row(bare(1), "Good morning", sender="Mum"))
        self.words_in_the_box()
        self.assertEqual(self.close_it(said)[:2], ("draft", "name+body"))
        self.setUp()
        self.header("Mum")
        self.add_row(row(bare(1), "ok"))
        self.assert_mismatch(self.close_it(dict(self.MUM, body="ok")), "unconfirmed")

    def test_the_persons_own_words_are_no_sign(self):
        self.header("Mum")
        mine = row(bare(1), "Good afternoon")
        mine["children"][0]["attrs"]["class"] = "message-out focusable-list-item"
        self.add_row(mine)
        self.assert_mismatch(self.run_reply(dict(self.MUM, body="Good afternoon")), "unconfirmed")

    def test_another_chat_of_the_same_name_with_other_messages(self):
        self.header("Mum")
        self.add_row(row(bare(1), "Happy birthday!"))
        diag = self.assert_mismatch(self.run_reply(), "unconfirmed")  # the click opened nothing new
        self.assertEqual((diag["name"], diag["navigated"], diag["body"], diag["avatar"]), (True, False, False, False))
        self.assertEqual(self.run_js("clicked").to_int32(), 1)

    def test_the_same_name_and_no_message_or_picture_to_tell_it_by(self):
        stand_in = "data:image/png;base64,iVBORw0KGgo="
        self.header("Mum", picture=stand_in)
        self.add_row(row(bare(1), "See you"))
        self.assert_mismatch(self.run_reply(dict(self.MUM, icon=stand_in)), "unconfirmed")

    def test_a_generic_body_alone(self):
        for body in ("\U0001F4F7 Photo", "Sticker", "This message was deleted"):
            self.setUp()
            self.header("Mum")
            self.add_row(row(bare(1), body))  # even with the same words there
            self.assert_mismatch(self.run_reply(dict(self.MUM, body=body)), "unconfirmed")

    def test_a_group_by_its_senders_words(self):
        said = {"title": "Football Sunday", "body": "Ana: See you at 5"}
        self.header("Football Sunday")
        self.add_row(row(bare(1), "See you at 5", sender="Ana"))
        self.words_in_the_box()
        self.assertEqual(self.run_reply(said)[:2], ("draft", "name+body"))
        self.setUp()
        self.header("Football Sunday")
        self.add_row(row(bare(1), "See you at 6", sender="Ana"))  # a group of the same name
        self.assert_mismatch(self.run_reply(said), "unconfirmed")

    def test_another_name(self):
        self.header("Mum")
        self.add_row(row(bare(1), "Did you eat?"))
        diag = self.assert_mismatch(self.run_reply(dict(self.MUM, title="Dad")), "other-name")
        self.assertFalse(diag["name"])
        # Opened by the click, with the message, under another name: still not it.
        self.setUp()
        self.header("Work Team")
        self.run_js(f"onClick = () => {{ setHeader({json.dumps(header('Mum'))}); "
                    f"addRow({json.dumps(row(bare(1), 'Did you eat?'))}); }}")
        self.assert_mismatch(self.run_reply(dict(self.MUM, title="Dad")), "other-name")

    def test_the_chat_moving_on_meanwhile(self):
        # Opened by the click, then another chat of the same name drawn in its place: not typed in.
        self.header("Work Team")
        self.words_in_the_box()
        self.run_js(f"onClick = () => setHeader({json.dumps(header('Mum'))})")
        self.later(100, "closeChat()")
        self.later(150, "reopenChat()")
        answer, why, _diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("draft", "name+click"))  # the same #main back: still it
        self.setUp()
        self.header("Work Team")
        self.run_js(f"onClick = () => setHeader({json.dumps(header('Mum'))})")
        self.later(100, f"setHeader({json.dumps(header('Mum2'))})")
        self.assertEqual(self.run_reply()[:2], ("mismatch", "moved"))

    def test_a_tag_for_another_chat(self):
        self.run_js(f"ask({json.dumps(GOOD)}, {self.BUDGET}, {json.dumps(self.MUM)}); "
                    f"shown.get(7).tag = {json.dumps(OTHER)}")
        self.until(self.BUDGET + 1000)
        answer, why, diag, _when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "tag"))
        self.assertEqual(diag["tagShape"], "9@a.a")
        self.assertEqual(self.run_js("clicked").to_int32(), 0)

    # ---- Waiting for the chat's messages ---------------------------------------------------------

    def test_no_messages_wait_out_the_budget(self):
        # The chat opens with no messages, and none come: waited for to the end of the budget less
        # what typing takes, not just the chat's own wait, then "unconfirmed" with what the page was like.
        self.header("Mum")
        self.assertIsNone(self.run_reply(until=self.chat_wait + 1000), "still waiting past the chat's own wait")
        self.until(self.BUDGET + 1000)
        answer, why, diag, when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "unconfirmed"))
        self.assertGreaterEqual(when, self.BUDGET - self.reserve)
        self.assertLess(when, self.BUDGET - self.reserve + 1000)
        self.assertEqual(self.run_js("clicked").to_int32(), 1)
        self.assertEqual({key: diag[key] for key in ("visibilityState", "rafFired", "innerWidth", "innerHeight", "main",
                                                     "header", "footer", "composer", "keys", "chatKeys", "rows",
                                                     "shape", "name", "navigated", "body", "avatar", "hasBody",
                                                     "hasIcon", "dataType", "dataKeys", "tagShape")},
                         {"visibilityState": "hidden", "rafFired": False, "innerWidth": 1000, "innerHeight": 700,
                          "main": True, "header": True, "footer": True, "composer": True, "keys": 0, "chatKeys": 0,
                          "rows": 0, "shape": "", "name": True, "navigated": False, "body": False, "avatar": False,
                          "hasBody": True, "hasIcon": False, "dataType": "null", "dataKeys": [], "tagShape": "9@a.a"})
        self.assertEqual(reply_diag({"diag": diag}), diag)

    def test_messages_loaded_late_are_waited_for(self):
        # WhatsApp loading the messages around the notified one: they come after the chat's own
        # wait, and the chat is told by them. (No footer: it goes no further than the check, and
        # says the chat takes no messages.)
        self.header("Mum")
        self.run_js('find("main").querySelector("footer").remove()')
        self.later(self.chat_wait + 2000, f"addRow({json.dumps(row(bare(1), 'Did you eat?'))})")
        answer, why, diag, when = self.run_reply()
        self.assertEqual((answer, why), ("cant-send", "name+body"))
        self.assertLess(when, self.chat_wait + 3000)
        self.assertEqual({key: diag[key] for key in self.FOOTER},
                         {"footer": False, "composer": False, "composerAt": -1, "buttons": 0, "icons": [],
                          "quote": False, "preview": False})
        self.assertEqual(reply_diag({"diag": diag}), diag)

    def test_keys_with_chat_ids_before_are_waited_for(self):
        # The chat open before had keys with chat ids: one opened with its name and no messages yet
        # is told by its keys once they come, not by the click.
        self.header("Work Team")
        self.add_row(row(f"false_{OTHER}_3EB0AB12", "Standup at 10"))
        self.run_js('find("main").querySelector("footer").remove()')
        self.run_js(f'onClick = () => {{ find("main").querySelector("#messages").childNodes = []; '
                    f'setHeader({json.dumps(header("Mum"))}); }}')
        self.later(2000, f'addRow({json.dumps(row(f"false_{GOOD}_3EB0CD34", "Did you eat?"))})')
        self.assertEqual(self.run_reply()[:2], ("cant-send", "ids"))
        self.setUp()
        self.header("Work Team")
        self.add_row(row(f"false_{OTHER}_3EB0AB12", "Standup at 10"))
        self.run_js(f'onClick = () => {{ find("main").querySelector("#messages").childNodes = []; '
                    f'setHeader({json.dumps(header("Mum"))}); }}')
        self.later(2000, f'addRow({json.dumps(row(f"false_{OTHER}_3EB0CD34", "Did you eat?"))})')
        self.assert_mismatch(self.run_reply(), "other-chat")

    def test_keyed_messages_loaded_late_are_waited_for(self):
        self.run_js('find("main").querySelector("footer").remove()')
        self.later(self.chat_wait + 2000, f'addRow({json.dumps(row(f"false_{GOOD}_3EB0LATE", "x"))})')
        answer, why, diag, when = self.run_reply()
        self.assertEqual((answer, why, diag["footer"]), ("cant-send", "ids", False))
        self.assertLess(when, self.chat_wait + 3000)

    def test_time_is_kept_back_for_typing(self):
        # Messages that come too late to leave time for the draft, the words and Send: nothing typed.
        self.header("Mum")
        self.later(self.BUDGET - 300, f"addRow({json.dumps(row(bare(1), 'Did you eat?'))})")
        answer, why, _diag, when = self.run_reply()
        self.assertEqual((answer, why), ("mismatch", "unconfirmed"))
        self.assertLess(when, self.BUDGET - self.reserve + 1000)
        self.assertTrue(self.run_js('boxEmpty(composerIn(find("main")))').to_boolean())

    def test_a_page_that_stalled_types_nothing_late(self):
        # The page held up (its timers late) until the messages came with less time left than the
        # rest takes: "busy" (late), nothing typed.
        self.header("Mum")
        self.later(self.chat_wait + 1000, f'now += {self.BUDGET - self.chat_wait - 4000}; '
                                          f"addRow({json.dumps(row(bare(1), 'Did you eat?'))})")
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why, diag), ("busy", "late", None))
        self.assertTrue(self.run_js('boxEmpty(composerIn(find("main")))').to_boolean())

    def test_another_chats_keys_are_still_a_mismatch(self):
        self.later(self.chat_wait + 2000, f'addRow({json.dumps(row(f"false_{OTHER}_3EB0LATE_{GOOD}", "x"))})')
        answer, why, diag, when = self.run_reply()
        self.assertEqual((answer, why), ("mismatch", "other-chat"))
        self.assertEqual((diag["keys"], diag["chatKeys"]), (1, 1))
        rows_wait = int(re.search(r"const ROWS_WAIT_MS = (\d+);", SECTION)[1])
        self.assertLess(when, self.chat_wait + 2000 + rows_wait + 1000)

    def test_the_wait_ends_with_the_budget(self):
        budget = self.chat_wait + 3000
        self.header("Mum")
        self.later(self.chat_wait + 5000, f"addRow({json.dumps(row(bare(1), 'Did you eat?'))})")  # after the budget
        self.run_js(f"ask({json.dumps(GOOD)}, {budget}, {json.dumps(self.MUM)})")
        self.until(budget + 10000)
        answer, why, _diag, when = self.answer()
        self.assertEqual((answer, why), ("mismatch", "unconfirmed"))
        self.assertLess(when, budget + 1000)  # the budget, and the look at the page after it

    def test_no_chat_open_after_the_click(self):
        self.run_js("closeChat()")
        answer, why, diag, when = self.run_reply()
        self.assertEqual((answer, why), ("gone", "click"))
        self.assertLess(when, self.chat_wait + 1000)  # no messages to wait for without a chat
        self.assertEqual((diag["main"], diag["header"], diag["footer"], diag["keys"], diag["shape"], diag["name"]),
                         (False, False, False, 0, "", False))

    def test_frames_and_the_shape_of_a_key(self):
        # A page WebKit draws, a chat whose keys name no chat and no name to tell it by: the shape
        # of the first key, and none of it as it was.
        self.run_js("frames = true; document.visibilityState = 'visible'")
        self.add_row(row("Mum’s chat 4917612345678 ✓", "x"))
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("mismatch", "other-name"))
        self.assertEqual((diag["rafFired"], diag["visibilityState"], diag["keys"], diag["chatKeys"], diag["rows"]),
                         (True, "visible", 1, 0, 1))
        self.assertEqual(diag["shape"], "a?a?a?9?")  # runs, not their lengths
        self.assertEqual(reply_diag({"diag": diag}), diag)

    def test_the_live_pages_keys(self):
        # As the live page had it: rows with bare ids, a header with no name Natter could match.
        self.add_row(row("3EB0A1B2C3D4E5F60718", "x"))
        answer, why, diag, _when = self.run_reply()
        self.assertEqual((answer, why), ("mismatch", "other-name"))
        self.assertEqual((diag["keys"], diag["chatKeys"], diag["shape"]), (1, 0, "9a9a9a9a9a9a9a9"))
        self.assertEqual((diag["keyAttrs"], diag["rowAttrs"]), (["class", "data-id"], ["role"]))

    def test_a_long_key_is_cut(self):
        self.add_row(row(f"true_{GOOD}_3EB0" + "A" * 80 + "_99", "x"))
        answer, why, diag, _when = self.run_reply(chat=OTHER)
        self.assertEqual((answer, why), ("mismatch", "other-chat"))
        self.assertEqual(diag["shape"], "a_9@a.a_9a9a")  # its first 40 characters only


if __name__ == "__main__":
    unittest.main()
