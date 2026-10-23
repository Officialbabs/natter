"""An account's page, lent to a reply card while nobody can see it (Window.lend_page).

WebKit stops drawing a page whose widget isn't on screen: with Natter Den's window closed or
minimised (or suspended by the desktop), or that account's tab not the one shown, the page gets no
animation frames and no resize or intersection observers. WhatsApp draws a chat's messages from
those, so a reply sent then found the chat open and empty, without the messages it's checked by.
For as long as the reply runs, the page goes under the reply card, which is on screen: unseen
(opacity 0), out of the pointer's and keyboard's way and hidden from screen readers, at the size it
had in the window, inside a scrolled window that doesn't follow its size, so WhatsApp lays it out
as before. It goes back where and as it was on every way out (give_back): the reply's answer, the
card hidden or gone, the page crashing or loading again, and whatever the app gives it back for
(the lock, the window shown or no longer minimised, the account removed, quitting).
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gtk, WebKit

# The page's size when it has never been laid out (Natter started hidden) and no window size is
# known: never 0 × 0, which WhatsApp takes for a phone's screen, or nothing at all.
FALLBACK_SIZE = (1000, 700)


def page_size(allocated: tuple[int, int], window: tuple[int, int]) -> tuple[int, int]:
    """The size the lent page keeps: its last one in the window, else the window's (its default or
    saved size), else FALLBACK_SIZE."""
    for width, height in (allocated, window):
        if width > 0 and height > 0:
            return width, height
    return FALLBACK_SIZE


def toplevel_hidden(root) -> bool:
    """Whether the window `root` is minimised or suspended (out of sight on the desktop, though GTK
    keeps what's in it mapped): WebKit counts a page in it hidden."""
    surface = root.get_surface() if root is not None and hasattr(root, "get_surface") else None
    if surface is None or not hasattr(surface, "get_state"):  # none yet, or not a window's (Gdk.Toplevel)
        return False
    hidden = Gdk.ToplevelState.MINIMIZED | getattr(Gdk.ToplevelState, "SUSPENDED", 0)
    return bool(surface.get_state() & hidden)


def out_of_sight(view) -> bool:
    """Whether WebKit counts `view`'s page out of sight: not on screen, or in a window minimised or
    suspended."""
    return not view.get_mapped() or toplevel_hidden(view.get_root())


def _holder() -> tuple[Gtk.Widget, Gtk.Widget]:
    """What holds the page in the card: a scrolled window that neither scrolls nor follows its
    child's size, and the viewport in it. Unseen and out of reach, the page in it included."""
    viewport = Gtk.Viewport(scroll_to_focus=False)
    holder = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                child=viewport, opacity=0, can_target=False, can_focus=False)
    return holder, viewport


class PageLoan:
    """`view`, out of its account's page `home` (a Gtk.Overlay, the view its main child) and into
    `host`, a reply card with a `page_slot` (a Gtk.Overlay over its content), at `size`.
    `ended(loan)` once it's back. Meanwhile the card's `holding` is the loan, and its `loan_ended()`
    (if it has one) is called once it's back. `hidden`: the page says it's hidden (its
    visibilityState) though GTK has it on screen."""

    def __init__(self, view, home, host, size: tuple[int, int],
                 ended: Optional[Callable[["PageLoan"], None]] = None, hidden: bool = False) -> None:
        self.view, self.home, self.host, self.size, self.ended = view, home, host, size, ended
        self.hidden = hidden
        self.lent = False
        self._holder = self._viewport = None
        # How the view was: its size request, focus and pointer settings, and whether its window's
        # focus was in it.
        self._kept: tuple = ()
        self._handlers: list = []

    def lend(self) -> bool:
        """Into the card, if the page is in its own place and out of sight (or `hidden`), and the
        card is on screen. False, changing nothing, otherwise."""
        view, home, host = self.view, self.home, self.host
        if (self.lent or not (self.hidden or out_of_sight(view)) or home.get_child() is not view
                or not host.get_mapped()):
            return False
        root = view.get_root()
        focus = root.get_focus() if root is not None and hasattr(root, "get_focus") else None
        focused = focus is not None and (focus is view or focus.is_ancestor(view))
        self._kept = (view.get_size_request(), view.get_can_focus(), view.get_focusable(), view.get_can_target(),
                      focused)
        self._holder, self._viewport = _holder()
        home.set_child(None)
        view.set_size_request(*self.size)
        view.set_can_focus(False)
        view.set_focusable(False)
        view.set_can_target(False)
        self._viewport.set_child(view)
        host.page_slot.add_overlay(self._holder)
        # After it's in the card: GTK marks a widget it shows as shown to screen readers too.
        self._holder.update_state([Gtk.AccessibleState.HIDDEN], [True])
        self.lent = True
        host.holding = self
        # "unmap" runs after the card's own handler: by then the page is out of sight with it, and
        # moving it disturbs nothing GTK is going through.
        self._handlers = [(host, host.connect("unmap", self.give_back)),
                          (view, view.connect("web-process-terminated", self.give_back)),
                          (view, view.connect("load-changed", self._on_load_changed))]
        return True

    def laid_out(self) -> bool:
        """Whether GTK has laid the view out at the lent size: until it has, a page never laid out
        before has the card's size in WebKit (Account._when_laid_out waits for it)."""
        width, height = self.size
        return self.view.get_width() >= width and self.view.get_height() >= height

    def _on_load_changed(self, _view, event) -> None:
        if event == WebKit.LoadEvent.STARTED:
            self.give_back()

    def give_back(self, *_args) -> None:
        """Back where and as it was. Again, or never lent: nothing."""
        if not self.lent:
            return
        self.lent = False
        for target, handler in self._handlers:
            target.disconnect(handler)
        self._handlers = []
        view = self.view
        self._viewport.set_child(None)
        self.host.page_slot.remove_overlay(self._holder)
        self._holder = self._viewport = None
        (width, height), can_focus, focusable, can_target, focused = self._kept
        view.set_size_request(width, height)
        view.set_can_focus(can_focus)
        view.set_focusable(focusable)
        view.set_can_target(can_target)
        self.home.set_child(view)
        # Lent out of a window on screen (minimised): its focus back in the page if it was there.
        # GTK moved it elsewhere, or nowhere, when the page left; nobody used that window meanwhile,
        # as it's given back as soon as it has the focus again (Window._on_surface_state).
        if focused:
            view.grab_focus()
        if getattr(self.host, "holding", None) is self:
            self.host.holding = None
        if self.ended is not None:
            self.ended(self)
        loan_ended = getattr(self.host, "loan_ended", None)
        if loan_ended is not None:
            loan_ended()

