"""A reply's page lent to its reply card while nobody can see it (loan.py), and the window's and
the app's side of it (window.py lend_page, app.py lend_page, page_seen): lent only while out of
sight, never while locked, and back where and as it was on every way out. Stand-in widgets: no
display, no page."""

import inspect
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, WebKit

from natter import loan as natter_loan
from natter.app import Natter
from natter.loan import FALLBACK_SIZE, PageLoan, page_size
from natter.settings import Settings
from natter.window import Window

HOLDER = natter_loan._holder  # the real one, before any test stands in for it


class Signals:
    """connect, disconnect and emit, as GObject has them."""

    def __init__(self):
        self.handlers, self.next = {}, 0

    def connect(self, signal, handler):
        self.next += 1
        self.handlers[self.next] = (signal, handler)
        return self.next

    def disconnect(self, handler_id):
        del self.handlers[handler_id]

    def emit(self, signal, *args):
        for name, handler in list(self.handlers.values()):
            if name == signal:
                handler(self, *args)


class View(Signals):
    """A WebKit.WebView as the loan moves it."""

    def __init__(self, mapped=False, width=1200, height=800):
        super().__init__()
        self.mapped, self.width, self.height = mapped, width, height
        self.size, self.can_focus, self.focusable, self.can_target = (-1, -1), True, True, True
        self.parent = None

    def get_mapped(self):
        return self.mapped

    def get_width(self):
        return self.width

    def get_height(self):
        return self.height

    def get_size_request(self):
        return self.size

    def set_size_request(self, width, height):
        self.size = (width, height)

    def get_can_focus(self):
        return self.can_focus

    def set_can_focus(self, value):
        self.can_focus = value

    def get_focusable(self):
        return self.focusable

    def set_focusable(self, value):
        self.focusable = value

    def get_can_target(self):
        return self.can_target

    def set_can_target(self, value):
        self.can_target = value


class Bin:
    """A widget with one child (the account's page, an overlay; the holder's viewport)."""

    def __init__(self, child=None):
        self.child = None
        self.set_child(child)

    def get_child(self):
        return self.child

    def set_child(self, child):
        if self.child is not None:
            self.child.parent = None
        self.child = child
        if child is not None:
            assert child.parent is None, "a widget in two places"
            child.parent = self


class Slot:
    """The card's overlay: what's laid over its content."""

    def __init__(self):
        self.overlays = []

    def add_overlay(self, widget):
        self.overlays.append(widget)

    def remove_overlay(self, widget):
        self.overlays.remove(widget)


class Card(Signals):
    """A reply card as the loan sees it."""

    def __init__(self, mapped=True):
        super().__init__()
        self.mapped, self.page_slot = mapped, Slot()

    def get_mapped(self):
        return self.mapped


class Holder:
    def __init__(self):
        self.states = []

    def update_state(self, states, values):
        self.states.append((list(states), list(values)))


def holder():
    return Holder(), Bin()


class LoanTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(natter_loan, "_holder", holder)
        patch.start()
        self.addCleanup(patch.stop)
        self.view, self.card, self.ended = View(), Card(), []
        self.view.set_size_request(40, 30)  # its own, to be had back
        self.home = Bin(self.view)

    def loan(self, size=(1200, 800)):
        return PageLoan(self.view, self.home, self.card, size, ended=self.ended.append)

    def assert_home(self, loan):
        self.assertIs(self.home.get_child(), self.view)
        self.assertEqual((self.view.size, self.view.can_focus, self.view.focusable, self.view.can_target),
                         ((40, 30), True, True, True))
        self.assertEqual(self.card.page_slot.overlays, [])
        self.assertEqual((self.card.handlers, self.view.handlers), ({}, {}))
        self.assertFalse(loan.lent)
        self.assertEqual(self.ended, [loan])

    def test_lent(self):
        loan = self.loan()
        self.assertTrue(loan.lend())
        self.assertIsNone(self.home.get_child())
        holder, = self.card.page_slot.overlays
        self.assertIs(self.view.parent, loan._viewport)
        self.assertEqual(self.view.size, (1200, 800))  # as big as it was: WhatsApp lays out as before
        self.assertEqual((self.view.can_focus, self.view.focusable, self.view.can_target), (False, False, False))
        self.assertEqual(holder.states, [([Gtk.AccessibleState.HIDDEN], [True])])
        self.assertEqual(self.ended, [])

    def test_the_holder_is_unseen_and_out_of_reach(self):
        source = inspect.getsource(HOLDER)
        for part in ("Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.EXTERNAL", "opacity=0",
                     "can_target=False", "can_focus=False", "Gtk.Viewport("):
            self.assertIn(part, source)

    def test_given_back(self):
        loan = self.loan()
        loan.lend()
        loan.give_back()
        self.assert_home(loan)
        loan.give_back()  # again: nothing
        self.assertEqual(len(self.ended), 1)

    def test_laid_out_once_gtk_gives_it_the_lent_size(self):
        loan = self.loan((1000, 700))
        loan.lend()
        self.view.width, self.view.height = 0, 0  # never laid out: WebKit has the card's size
        self.assertFalse(loan.laid_out())
        self.view.width, self.view.height = 1000, 700
        self.assertTrue(loan.laid_out())

    def test_the_card_knows_it_holds_the_page(self):
        told = []
        self.card.loan_ended = lambda: told.append(self.card.holding)
        loan = self.loan()
        loan.lend()
        self.assertIs(self.card.holding, loan)
        loan.give_back()
        self.assertEqual(told, [None])  # told once it's back, and no longer holding it
        self.assertIsNone(self.card.holding)

    def test_back_when_the_card_goes(self):
        loan = self.loan()
        loan.lend()
        self.card.emit("unmap")  # hidden or destroyed
        self.assert_home(loan)

    def test_back_when_the_page_crashes(self):
        loan = self.loan()
        loan.lend()
        self.view.emit("web-process-terminated", WebKit.WebProcessTerminationReason.CRASHED)
        self.assert_home(loan)

    def test_back_when_the_page_loads_again(self):
        loan = self.loan()
        loan.lend()
        for event in (WebKit.LoadEvent.COMMITTED, WebKit.LoadEvent.FINISHED):
            self.view.emit("load-changed", event)
        self.assertTrue(loan.lent)
        self.view.emit("load-changed", WebKit.LoadEvent.STARTED)
        self.assert_home(loan)

    def test_never_when_it_is_on_screen(self):
        self.view.mapped = True
        self.assertFalse(self.loan().lend())
        self.assertIs(self.home.get_child(), self.view)

    def test_never_into_a_card_out_of_sight(self):
        self.card.mapped = False
        self.assertFalse(self.loan().lend())
        self.assertIs(self.home.get_child(), self.view)

    def test_never_from_anywhere_else(self):
        self.home.set_child(None)
        self.assertFalse(self.loan().lend())
        self.assertEqual(self.card.page_slot.overlays, [])

    def test_once(self):
        loan = self.loan()
        self.assertTrue(loan.lend())
        self.assertFalse(loan.lend())
        self.assertEqual(len(self.card.page_slot.overlays), 1)

    def test_never_given_back_unlent(self):
        loan = self.loan()
        loan.give_back()
        self.assertEqual(self.ended, [])
        self.assertEqual(self.view.size, (40, 30))

    def test_its_size(self):
        self.assertEqual(page_size((1200, 800), (1000, 700)), (1200, 800))
        self.assertEqual(page_size((0, 0), (1000, 700)), (1000, 700))  # never laid out (started hidden)
        self.assertEqual(page_size((0, 0), (0, 0)), FALLBACK_SIZE)
        self.assertEqual(page_size((1200, 0), (900, -1)), FALLBACK_SIZE)
        self.assertGreater(min(FALLBACK_SIZE), 0)


# ---- The window's side (window.py) -------------------------------------------------------------


def settings(test, **values):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    found = Settings(Path(folder.name) / "settings.json")
    for key, value in values.items():
        found[key] = value
    return found


class WindowTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(natter_loan, "_holder", holder)
        patch.start()
        self.addCleanup(patch.stop)

    def window(self, default=(1100, 750), **values):
        found = SimpleNamespace(pages={}, loans={}, app=SimpleNamespace(settings=settings(self, **values)),
                                get_default_size=lambda: default, shown=None)
        for name in ("lend_page", "give_back_pages", "give_back_shown"):
            setattr(found, name, types.MethodType(getattr(Window, name), found))
        found.account = None
        return found

    def account(self, found, account_id="main", **view):
        account = SimpleNamespace(id=account_id, view=View(**view))
        home = Bin(account.view)
        found.pages[account_id] = SimpleNamespace(get_child=lambda: home)
        account.home = home
        return account

    def test_lent_at_its_size_and_kept_until_back(self):
        found = self.window()
        account, card = self.account(found), Card()
        loan = found.lend_page(account, card)
        self.assertEqual(loan.size, (1200, 800))
        self.assertIs(found.loans["main"], loan)
        self.assertIsNone(found.lend_page(account, Card()))  # once at a time
        card.emit("unmap")
        self.assertEqual(found.loans, {})
        self.assertIs(account.home.get_child(), account.view)

    def test_never_laid_out_takes_the_windows_size(self):
        found = self.window()
        account = self.account(found, width=0, height=0)
        self.assertEqual(found.lend_page(account, Card()).size, (1100, 750))
        found = self.window(default=(0, 0), width=980, height=640)
        account = self.account(found, width=0, height=0)
        self.assertEqual(found.lend_page(account, Card()).size, (980, 640))

    def test_not_lent_when_it_cant_be(self):
        found = self.window()
        account = self.account(found, mapped=True)
        self.assertIsNone(found.lend_page(account, Card()))
        account = self.account(found, "work")
        self.assertIsNone(found.lend_page(account, Card(mapped=False)))
        self.assertIsNone(found.lend_page(SimpleNamespace(id="gone", view=View()), Card()))
        self.assertEqual(found.loans, {})

    def test_given_back_one_or_all(self):
        found = self.window()
        main, work = self.account(found), self.account(found, "work")
        found.lend_page(main, Card())
        found.lend_page(work, Card())
        found.give_back_pages(work)
        self.assertEqual(list(found.loans), ["main"])
        found.give_back_pages()
        self.assertEqual(found.loans, {})
        self.assertIs(main.home.get_child(), main.view)

    def test_back_when_its_tab_is_shown(self):
        found = self.window()
        main, work = self.account(found), self.account(found, "work")
        found.lend_page(main, Card())
        found.lend_page(work, Card())
        found.account = work
        found.give_back_shown()
        self.assertEqual(list(found.loans), ["main"])

    def test_back_before_the_window_or_a_page_is_shown_or_goes(self):
        self.assertIn("self.give_back_pages(account)", inspect.getsource(Window.remove_account).split("pop")[0])
        source = inspect.getsource(Window.__init__)
        self.assertIn("self.give_back_shown(), app.on_page_changed()", source)  # first, before anything else looks
        # The window shown: only the page it shows (another account's stays with its reply).
        self.assertIn('"notify::visible", lambda *_: (self.get_visible() and self.give_back_shown()', source)

    def test_nothing_back_with_no_tab_shown(self):
        found = self.window()
        main = self.account(found)
        found.lend_page(main, Card())
        found.give_back_shown()
        self.assertEqual(list(found.loans), ["main"])


# ---- The app's side (app.py) -------------------------------------------------------------------


class AppTest(unittest.TestCase):
    def app(self, **values):
        lent = []
        window = SimpleNamespace(lend_page=lambda account, card: lent.append((account, card)) or "loan",
                                 get_visible=lambda: values.get("visible", False), account=None,
                                 give_back_pages=lambda account=None: lent.append(("back", account)))
        found = SimpleNamespace(window=window, locked=values.get("locked", False), calling=values.get("calling", False),
                                lent=lent)
        if values.get("quitting"):
            found._quitting = True
        for name in ("lend_page", "page_seen"):
            setattr(found, name, types.MethodType(getattr(Natter, name), found))
        return found

    def account(self, mapped=False):
        return SimpleNamespace(id="main", view=View(mapped=mapped), calling=False)

    def test_lent_to_the_card_on_screen(self):
        app, account, card = self.app(), self.account(), Card()
        self.assertEqual(app.lend_page(account, lambda: card), "loan")
        self.assertEqual(app.lent, [(account, card)])

    def test_not_lent(self):
        asked = []

        def host():
            asked.append(1)
            return Card()
        for values in ({"locked": True}, {"quitting": True}):
            app = self.app(**values)
            self.assertIsNone(app.lend_page(self.account(), host), values)
        app = self.app()
        self.assertIsNone(app.lend_page(self.account(mapped=True), host))  # on screen already
        self.assertEqual(asked, [], "no card brought up for it")
        app.window = None
        self.assertIsNone(app.lend_page(self.account(), host))
        app = self.app()
        self.assertIsNone(app.lend_page(self.account(), lambda: None))
        self.assertIsNone(app.lend_page(self.account(), lambda: Card(mapped=False)))
        self.assertEqual(app.lent, [])

    def test_lent_with_a_call_in_another_account(self):
        # A call in this account's page refuses the reply before it's asked (Account._reply_refusal).
        app, account, card = self.app(calling=True), self.account(), Card()
        self.assertEqual(app.lend_page(account, lambda: card), "loan")

    def test_visible_means_seen(self):
        app, account = self.app(visible=True), self.account(mapped=True)
        self.assertFalse(app.page_seen(account))  # another account's tab is shown
        app.window.account = account
        self.assertTrue(app.page_seen(account))
        app = self.app(visible=False)
        app.window.account = account
        self.assertFalse(app.page_seen(account))  # in the tray
        app.window = None
        self.assertFalse(app.page_seen(account))

    def test_given_back_at_the_lock_and_when_quitting(self):
        app = SimpleNamespace(lock_enabled=True, locked=False, settings={}, replies={}, notifier=SimpleNamespace(
            conceal=lambda: None), _refresh_tray=lambda: None, events=[])
        app.window = SimpleNamespace(give_back_pages=lambda: app.events.append("back"),
                                     show_lock=lambda locked: app.events.append("lock"))
        Natter.lock(app)
        self.assertEqual(app.events, ["back", "lock"])
        source = inspect.getsource(Natter.quit_natter)
        self.assertLess(source.index("self.window.give_back_pages()"), source.index("end_pages("))

    def test_a_reply_window_offers_itself_and_the_page_is_asked_with_it(self):
        source = inspect.getsource(Natter.send_reply)
        self.assertIn("lend=(lambda: self.lend_page(account, host)) if host is not None else None", source)


if __name__ == "__main__":
    unittest.main()
