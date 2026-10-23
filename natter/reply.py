"""Replying from a notification, in a small window of its own.

Reply on a chat's notification opens it, unless KDE Plasma's reply field was typed in, which
sends from the notification itself (app.on_reply). Other desktops' notifications (Ada's, GNOME's)
can't hold a text field, so the window is the next best thing: a card like the notification, with
who wrote and their latest messages, and a field to type in. Enter sends, Shift+Enter starts a new
line, Escape closes and keeps what was typed for the next Reply to that chat (in memory only).
Once a reply has gone the card goes too; when it hasn't, a line under the field says why, with the
one thing to do about it.

The send itself is WhatsApp's, done in its page (Account.send_reply): its own notification opens
the chat, the words go into that chat's empty message box, and its Send button is pressed once.
Every way of sending here goes through one guarded call (_send), so one press is one message:
nothing is sent again by itself, and an answer that leaves it unclear whether WhatsApp sent it
ends that reply for good (its words stay on show to read, and the field is for a new one).

It isn't a dialog of the main window: replying shouldn't bring that window forward (it's usually
hidden in the tray), and the lock closes every dialog.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from . import NAME
from .launcher import menu_name
from .notify import BODY_CHARS, MESSAGES, ReplyTarget, clip

# The most a quick reply may be, counted as the page counts (bridge.js, in UTF-16 units: an emoji
# can be two). Longer belongs in WhatsApp itself.
LIMIT = 4096
WIDTH, NARROWEST = 360, 320  # about a notification's width
ENTER = (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter)
# A sent reply's window goes at once, but is only destroyed this long after (ms): a screen reader
# asks the window about its "Reply sent", and would say nothing of a window that's gone.
SENT_HEARD = 1500
FIELD_LOW, FIELD_HIGH = 36, 150  # the field's height: a line, and about six
# Their messages: kept whole up to MESSAGES_KEPT, so the window grows with what's typed rather than
# cut them off; past that they give way to it, and they never take more than MESSAGES_HIGH.
MESSAGES_KEPT, MESSAGES_HIGH = 160, 220
YOURS_HIGH = 96  # a reply that's over, on show above the field: about four lines, then it scrolls
TOO_LONG = "Too long for a quick reply"
ACTIONS = {"chat": "Open Chat", "app": f"Open {NAME}"}  # the one thing a problem's line offers, as a link
SEND_ICON = "go-up-symbolic"  # Adwaita has no paper plane; an arrow up, in a round button, reads as Send

CSS = """
.natter-reply button.natter-reply-who { padding: 2px 10px 2px 2px; border-radius: 999px; }
/* Not to be opened while sending, but still to be read. */
.natter-reply button.natter-reply-who:disabled { filter: none; }
.natter-reply button.natter-reply-who:disabled label { color: inherit; }
.natter-reply-bubble { border-radius: 14px; padding: 6px 10px; }
.natter-reply-bubble:first-child { border-top-left-radius: 4px; }
.natter-reply-bubble:first-child:dir(rtl) { border-top-left-radius: 14px; border-top-right-radius: 4px; }
.natter-reply-hidden { padding: 2px 4px 2px 10px; }
.natter-reply-yours { border-radius: 14px 4px 14px 14px; background-color: alpha(@accent_bg_color, 0.18); }
.natter-reply-yours:dir(rtl) { border-radius: 4px 14px 14px 14px; }
.natter-reply-yours label { padding: 6px 10px; }
.natter-reply-field { background-color: alpha(currentColor, 0.08); border-radius: 18px; }
.natter-reply-field:focus-within { outline: 2px solid alpha(@accent_color, 0.5); outline-offset: -2px; }
.natter-reply-field textview, .natter-reply-field textview text { background: none; }
.natter-reply-field.natter-sending overlay { opacity: 0.55; }
.natter-reply-field button.natter-reply-send { min-width: 32px; min-height: 32px; padding: 0; margin: 2px; }
/* Sending is a send under way, not a Send that's off: the spinner on the button at full strength. */
.natter-reply-field.natter-sending button.natter-reply-send:disabled { filter: none; }
.natter-reply-problem.error { color: @error_color; }
.natter-reply-problem.warning { color: @warning_color; }
.natter-reply-problem label > link { color: @accent_color; text-decoration: none; }
.natter-reply-problem label > link:hover { text-decoration: underline; }
/* High contrast (natter-hc, from libadwaita's style: a media query needs GTK 4.20): edges as
   libadwaita's own fields and cards have them then, and the link underlined. */
.natter-reply.natter-hc .natter-reply-field,
.natter-reply.natter-hc .natter-reply-yours { box-shadow: inset 0 0 0 1px alpha(currentColor, 0.5); }
.natter-reply.natter-hc .natter-reply-problem label > link { text-decoration: underline; }
/* Privacy blur, as in WhatsApp's page (style.css): who and what stay blurred until pointed at, and
   what's being typed is blurred only while another window is in front. Blur When in the
   Background blurs it all then. */
.natter-reply.natter-blur .natter-private { filter: blur(7px); }
.natter-reply.natter-blur .natter-private:hover { filter: none; }
.natter-reply.natter-blur:backdrop .natter-reply-field,
.natter-reply.natter-away:backdrop .natter-private,
.natter-reply.natter-away:backdrop .natter-reply-field { filter: blur(7px); }
"""
_styled = False


@dataclass(frozen=True)
class Outcome:
    """How the window shows an answer from the page that isn't "sent".

    `line`: what the line under the field says. `button`: "chat" for Open Chat, "app" to show
    NAME's window on that account. `send`: whether Send stays on for the same words (nothing
    reached WhatsApp, and trying again may work). `done`: WhatsApp may have the words (sent, or
    left in the chat's message box): this reply is over, its words stay on show to read and leave
    the field for a new one, and never go again.
    """

    line: str
    button: str = ""
    send: bool = True
    done: bool = False


# Said under the reply it's about ("Your reply"), and in a notice titled for it (notice): "it".
UNCLEAR = Outcome("Couldn't check whether it was sent", "chat", send=False, done=True)
OUTCOMES = {
    # The reason alone: a notice about it has "Your reply wasn't sent" as its title (notice).
    "locked": Outcome(f"{NAME} locked before your reply could be sent"),
    "calling": Outcome("You can send this once the call ends"),
    # What's open (a picture waiting to be sent, a profile being edited, a voice message being
    # recorded) would be closed by opening another chat, so the page leaves it be.
    "busy": Outcome("Something is open in WhatsApp. Close it, then send again", "app"),
    # Words in its message box, or a message being replied to: Send would send these with them.
    "draft": Outcome("This chat already has an unsent message", "chat"),
    # A group only admins write in, one they've left, a contact they've blocked. Send stays on, in
    # case the chat was only slow to show its message box.
    "cant-send": Outcome("You can't send messages to this chat", "chat"),
    "mismatch": Outcome("Couldn't find this chat in WhatsApp", "chat"),
    # The page no longer has the notification that opens it, and the chat isn't the one open
    # there: once Open Chat has opened it, or a newer message has come, Send works again.
    "gone": Outcome("Couldn't open this chat from here", "chat"),
    "not-ready": Outcome("WhatsApp is still loading. Try again in a moment"),
    # Send stays on for once it's linked again: until then, the page is asked nothing.
    "needs-link": Outcome("WhatsApp needs to be linked again", "app"),
    "too-long": Outcome(TOO_LONG, "chat"),
    "left-as-draft": Outcome("Left unsent in the chat's message box", "chat", send=False, done=True),
    "unsure": UNCLEAR,
    "unknown": UNCLEAR,
}


def outcome(result: str) -> Outcome:
    """How `result` shows. One this window doesn't know may have sent: it's taken as unclear."""
    return OUTCOMES.get(result, UNCLEAR)


def units(text: str) -> int:
    """`text`'s length as the page counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def window_title(name: str, sender: bool) -> str:
    """The title the desktop shows (the dock, the window switcher): the name only where
    notifications may show it."""
    return f"Reply to {menu_name(name)}" if sender and name.strip() else "Reply"


def notice(result: str, name: str, named: bool) -> tuple[str, str]:
    """The notification's title and body for a reply that failed in a window closed meanwhile.
    `named`: it may say who the reply was to."""
    look = outcome(result)
    title = "Your reply may not have been sent" if look is UNCLEAR else "Your reply wasn't sent"
    return title, f"To {menu_name(name)}. {look.line}." if named and name.strip() else f"{look.line}."


def joined(first: str, then: str) -> str:
    """Two replies' words as one, a line apart."""
    return f"{first.rstrip()}\n{then}" if first.strip() else then


def _style() -> None:
    global _styled
    if _styled:
        return
    _styled = True
    css = Gtk.CssProvider()
    css.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class ReplyWindow(Adw.Window):
    """A reply to one chat, for app.on_reply. Talks only to app: send_reply, reply_failed,
    open_account, _open_from_notification, note_activity, reply_drafts, replies and settings."""

    def __init__(self, app, target: ReplyTarget, text: str = "") -> None:
        _style()
        # Adw.Window's least size is 360 × 200 unless it's given one: a short reply window is shorter.
        super().__init__(application=app, default_width=WIDTH, width_request=NARROWEST, height_request=120,
                         resizable=True)
        # Added, not given as css_classes: that would replace the window's own "background" class.
        self.add_css_class("natter-reply")
        self.app = app
        self.target = target
        self.key = (target.account, target.chat)
        self.ref = secrets.token_hex(8)  # for its Show Reply notice: names no chat
        self.sending = False
        self.outcome: Optional[str] = None  # the page's latest answer, while it's on show
        # What came from KDE's reply field while sending, for once that has answered (_replay): its
        # words, and the page's answer to them if they went to it (None: words to send from here).
        self.queued: list[tuple[str, Optional[str]]] = []
        self.revealed = False  # Show pressed with message text hidden in notifications
        self.told: Optional[str] = None  # the answer its notice (app.reply_failed) is about, while it's up
        self.told_named = False  # and whether that notice says who the reply is to
        self._too_long = False  # said once when the reply grows too long, not at every key
        self._closing = 0  # the timer that destroys it once it has gone, sent
        self._refocus = False  # the field gave up the focus for a send, to have it back after
        self.action = ""  # what the problem's line offers (ACTIONS), while it's on show
        style = Adw.StyleManager.get_default()
        self._contrast = style.connect("notify::high-contrast", lambda *_: self._show_contrast())
        self._show_contrast()

        # Who it's from, as the notification says, and the way out. No header bar: the row is the
        # window's handle, to move it by. Who it's from opens the chat (Alt+O), as a click on the
        # notification would.
        self.avatar = Adw.Avatar(size=32, show_initials=True, css_classes=["natter-private"])
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        self.account = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, visible=False,
                                 css_classes=["caption", "dim-label"])
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, css_classes=["natter-private"])
        names.append(self.name)
        names.append(self.account)
        who = Gtk.Box(spacing=10)
        who.append(self.avatar)
        who.append(names)
        self.who = Gtk.Button(child=who, hexpand=True, halign=Gtk.Align.START, tooltip_text="Open Chat",
                              css_classes=["flat", "natter-reply-who"])
        self.who.connect("clicked", lambda *_: self._open_chat())
        close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Close",
                           css_classes=["flat", "circular"])
        close.update_property([Gtk.AccessibleProperty.LABEL], ["Close"])
        close.connect("clicked", lambda *_: self.close())
        top = Gtk.Box(spacing=6, margin_top=8, margin_bottom=4, margin_start=10, margin_end=8)
        top.append(self.who)
        top.append(close)

        # What they wrote: the latest few, as bubbles.
        self.more = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"], visible=False)
        self.bodies = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hidden = Gtk.Label(label="Message text is hidden", xalign=0, wrap=True, css_classes=["dim-label"])
        # Its mnemonic is H: S is Send's.
        show = Gtk.Button(label="S_how", use_underline=True, valign=Gtk.Align.CENTER, css_classes=["flat"])
        show.connect("clicked", lambda *_: self._reveal())
        self.hidden_row = Gtk.Box(spacing=6, halign=Gtk.Align.START, margin_end=40,
                                  css_classes=["card", "natter-reply-bubble", "natter-reply-hidden"])
        self.hidden_row.append(hidden)
        self.hidden_row.append(show)
        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=2, margin_bottom=6,
                              margin_start=12, margin_end=12)
        for widget in (self.more, self.bodies, self.hidden_row):
            self.column.append(widget)
        self.messages = messages = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=MESSAGES_HIGH,
            vexpand=True, child=self.column)
        # Whenever their size or what they show changes: kept whole as far as they may be, and when
        # there's less room than that (a long reply, a newer message), the latest one in sight.
        self._sticking = 0
        messages.get_vadjustment().connect("changed", lambda *_: self._stick())

        # The reply: a field that grows to about six lines, then scrolls, with Send in it.
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, hexpand=True,
                                  top_margin=8, bottom_margin=8, left_margin=14, right_margin=6)
        self.entry.update_property([Gtk.AccessibleProperty.PLACEHOLDER], ["Type a reply"])
        self.buffer = self.entry.get_buffer()
        self.buffer.connect("changed", lambda *_: self._changed())
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(keys)
        # Never less than a line: squeezed, the window grows instead. It grows with what's typed,
        # to about six lines (_fit_field), and the window with it while the messages are kept whole.
        self.field = scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                   vscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                                   propagate_natural_height=True, min_content_height=FIELD_LOW,
                                                   max_content_height=FIELD_HIGH, child=self.entry)
        self._fitting = 0
        scroller.get_vadjustment().connect("changed", lambda *_: self._refit())
        self.placeholder = Gtk.Label(label="Type a reply", xalign=0, valign=Gtk.Align.START, can_target=False,
                                     margin_start=14, margin_top=8, css_classes=["dim-label"],
                                     accessible_role=Gtk.AccessibleRole.PRESENTATION)
        typing = Gtk.Overlay(child=scroller, hexpand=True)
        typing.add_overlay(self.placeholder)
        # Send: round, in the field, at its last line. While sending, a spinner in its place.
        spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)  # Adw.Spinner: 1.6
        spinner.set_size_request(16, 16)
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        self.send_face = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        self.send_face.add_named(Gtk.Image(icon_name=SEND_ICON), "send")
        self.send_face.add_named(spinner, "sending")
        self.send_button = Gtk.Button(child=self.send_face, valign=Gtk.Align.END, tooltip_text="Send", sensitive=False,
                                      css_classes=["circular", "suggested-action", "natter-reply-send"])
        self.send_button.update_property([Gtk.AccessibleProperty.LABEL], ["Send"])
        self.send_button.connect("clicked", lambda *_: self._send())
        self.composer = Gtk.Box(valign=Gtk.Align.END, css_classes=["natter-reply-field"])
        self.composer.append(typing)
        self.composer.append(self.send_button)

        # A problem: a line under the field, and the one thing to do about it, as a link at its end
        # (where it fits, whatever the line's length: a button beside it took half the width).
        self.problem_text = Gtk.Label(xalign=0, hexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                      css_classes=["caption"])
        self.problem_text.connect("activate-link", lambda *_: self._action_clicked() or True)
        self.problem = Gtk.Box(spacing=6, margin_start=6, visible=False, css_classes=["natter-reply-problem"])
        self.problem.append(Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.START,
                                      accessible_role=Gtk.AccessibleRole.PRESENTATION))
        self.problem.append(self.problem_text)

        # A reply that's over (outcome done): its words, to read and copy, just above the field
        # (where they were typed), so they're in sight however long the messages above are.
        self.yours = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
                               css_classes=["natter-private"])
        self.yours.update_property([Gtk.AccessibleProperty.DESCRIPTION], ["Your reply"])
        self.yours_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                                 propagate_natural_width=True, max_content_height=YOURS_HIGH,
                                                 halign=Gtk.Align.END, margin_start=40, child=self.yours,
                                                 css_classes=["natter-reply-yours"])
        self.yours_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=False)
        self.yours_box.append(Gtk.Label(label="Your reply", xalign=1, css_classes=["caption", "dim-label"]))
        self.yours_box.append(self.yours_scroller)

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=2, margin_bottom=12,
                         margin_start=12, margin_end=12)
        for widget in (self.yours_box, self.composer, self.problem):
            bottom.append(widget)
        # The field under the messages in the content, not as a bottom bar: a toolbar view gives a
        # bar its full height first, and with a long reply in a short window, GTK was asked to fit
        # the messages into less than nothing.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(Gtk.WindowHandle(child=top))
        content.append(messages)
        content.append(bottom)
        self.set_content(content)

        # Escape after the input method has had its say (it cancels what's being composed first).
        shortcuts = Gtk.ShortcutController()
        for trigger, run in (("Escape|<Control>w", self.close), ("<Alt>o", self._open_chat), ("<Alt>s", self._send)):
            shortcuts.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                                                action=Gtk.CallbackAction.new(lambda *_, run=run: run() or True)))
        self.add_controller(shortcuts)
        # Writing a reply counts as using Natter, for the lock's idle time.
        activity = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        activity.connect("key-pressed", lambda *_: (app.note_activity(), False)[1])
        self.add_controller(activity)
        moved = Gtk.EventControllerMotion(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        moved.connect("motion", lambda *_: app.note_activity())
        self.add_controller(moved)
        self.connect("close-request", self._on_close_request)

        self.apply_privacy()  # and what it shows
        if text:
            self.buffer.set_text(text)
            self.buffer.place_cursor(self.buffer.get_end_iter())
        self._show_state()
        self.set_focus(self.entry)

    # ---- What it shows ----------------------------------------------------------------------

    def _show_target(self) -> None:
        target, settings = self.target, self.app.settings
        name = menu_name(target.title) or "WhatsApp"
        self.name.set_label(name)
        account = menu_name(target.account_name or "")
        self.account.set_label(account)
        self.account.set_visible(bool(account))
        self.avatar.set_text(name)
        picture = None
        if target.picture is not None and settings["notify_sender"]:
            ok, png = target.picture.save_to_bufferv("png", [], [])
            if ok:
                picture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        self.avatar.set_custom_image(picture)
        child = self.bodies.get_first_child()
        while child is not None:
            self.bodies.remove(child)
            child = self.bodies.get_first_child()
        for body in target.messages[-MESSAGES:]:
            # As wide as its words, like a chat's: room on the far side, as for a reply.
            self.bodies.append(Gtk.Label(label=body, xalign=0, halign=Gtk.Align.START, margin_end=40, wrap=True,
                                         wrap_mode=Pango.WrapMode.WORD_CHAR, lines=4,
                                         ellipsize=Pango.EllipsizeMode.END, selectable=False,
                                         css_classes=["card", "natter-reply-bubble", "natter-private"]))
        self.more.set_label(f"{target.count} new messages")
        self.more.set_visible(target.count > len(target.messages[-MESSAGES:]) and target.count > 1)
        shown = bool(settings["notify_preview"]) or self.revealed
        self.bodies.set_visible(shown and bool(target.messages))
        self.hidden_row.set_visible(not shown and bool(target.messages))
        # Nothing to show (a notification without words): no room kept for it.
        self.messages.set_visible(bool(target.messages) or self.more.get_visible())
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [f"Reply to {name}"])
        latest = target.messages[-1] if target.messages else ""
        self.entry.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                                   [latest if shown else "Message text is hidden" if latest else ""])

    def _show_contrast(self) -> None:
        if Adw.StyleManager.get_default().get_high_contrast():
            self.add_css_class("natter-hc")
        else:
            self.remove_css_class("natter-hc")

    def apply_privacy(self) -> None:
        """Privacy blur, Blur When in the Background, and the title the desktop sees, as the
        settings are now."""
        settings = self.app.settings
        sender = settings["notify_sender"] and not settings["privacy_blur"]
        self.set_title(window_title(self.target.title, sender))
        for name, on in (("natter-blur", settings["privacy_blur"]), ("natter-away", settings["blur_when_away"])):
            if on:
                self.add_css_class(name)
            else:
                self.remove_css_class(name)
        self._show_target()

    def _reveal(self) -> None:
        # A click on Show gives it the focus, and its row then goes: GTK would move the focus on to
        # the messages, and what's typed next would go nowhere. Back to the field instead.
        focus = self.get_focus()
        had = focus is not None and (focus is self.hidden_row or focus.is_ancestor(self.hidden_row))
        self.revealed = True
        self._show_target()
        if not had:
            return
        if self.entry.get_editable():
            self.entry.grab_focus()
        else:  # sending: back to the field once it can be written in (_show_state)
            self.set_focus(None)
            self._refocus = True

    def retarget(self, target: ReplyTarget) -> None:
        """Reply pressed on a newer notification for this chat: its latest messages and the
        notification that opens it."""
        self.target = target
        self._show_target()

    def add_message(self, page_id: int, title: str, body: str) -> None:
        """A new message in this chat while the window is open (app._on_notify_message)."""
        target = self.target
        target.page_id, target.title = page_id, title or target.title
        if body:
            target.messages = (target.messages + [clip(body, BODY_CHARS)])[-MESSAGES:]
        target.count += 1
        self._show_target()
        if self.is_active():
            self.announce("New message", Gtk.AccessibleAnnouncementPriority.LOW)

    def _text(self) -> str:
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

    def put_text(self, text: str) -> None:
        """Words for this chat from KDE's reply field that aren't to be sent as they came: added
        to the field, or once the send under way has answered. (A window that has sent is out of
        app.replies at once, so none come to it.)"""
        if self.sending:
            self.queued.append((text, None))
            return
        self.buffer.set_text(joined(self._text(), text))
        self.buffer.place_cursor(self.buffer.get_end_iter())
        self._show_state()

    def unsent(self) -> bool:
        """Whether it holds a reply that hasn't gone yet: being sent, or written."""
        return self.sending or bool(self._text().strip())

    def inline_failed(self, text: str, result: str) -> None:
        """What was typed in KDE's reply field wasn't sent (`result`): shown here, its words in
        the field to send again, or on show to read if WhatsApp may have them. While a reply from
        here is being sent, after its answer (_replay): put in the field under it, words WhatsApp
        may have would go again with the next Send."""
        if self.sending:
            self.queued.append((text, result))
            return
        look = outcome(result)
        if look.done:
            self._show_yours(text)
        else:
            self.put_text(text)
        self.outcome = result if result in OUTCOMES else "unknown"
        self._show_state()
        self.announce(look.line, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _changed(self) -> None:
        self.placeholder.set_visible(not self._text())
        self._update_send()
        self._refit()

    def _stick(self) -> None:
        # Not straight away: "changed" comes while GTK lays the messages out, and a scroll asked for
        # then is lost (it would stay at the top until something else moved).
        if not self._sticking:
            self._sticking = GLib.idle_add(self._to_latest)

    def _to_latest(self) -> bool:
        self._sticking = 0
        if self.messages.get_visible():
            # Kept whole up to MESSAGES_KEPT: their natural height as least height, at the width
            # they have (never less than their least: GTK warns about a width a widget can't have).
            narrowest = self.column.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            natural = self.column.measure(Gtk.Orientation.VERTICAL, max(self.messages.get_width(), narrowest, 1))[1]
            least = min(natural, MESSAGES_KEPT)
            if self.messages.get_min_content_height() != least:
                self.messages.set_min_content_height(least)
        adjustment = self.messages.get_vadjustment()
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        return GLib.SOURCE_REMOVE

    def _refit(self) -> None:
        # Once GTK has laid out the text (a text view does that when idle), and outside a layout.
        if not self._fitting:
            self._fitting = GLib.idle_add(self._fit_field, priority=GLib.PRIORITY_LOW)

    def _fit_field(self) -> bool:
        """The field as tall as what's typed, from a line to FIELD_HIGH. Its least height, not
        just its natural one: the window keeps its size once it's up, so it grows only for what
        it can't do without, and past MESSAGES_KEPT the messages above give way (and scroll) rather
        than the field scroll while there's room."""
        self._fitting = 0
        # Never narrower than its least width: before it's laid out it has none, and GTK warns
        # about measuring a text view at a width it can't have.
        narrowest = self.entry.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        _least, natural, _b, _b = self.entry.measure(Gtk.Orientation.VERTICAL,
                                                     max(self.entry.get_width(), narrowest, 1))
        height = max(FIELD_LOW, min(natural, FIELD_HIGH))
        if self.field.get_min_content_height() != height:
            self.field.set_min_content_height(height)
        # A scroll bar only once it scrolls: one that could show makes the field as tall as the
        # bar's own least height (about three lines), even with a line in it.
        policy = Gtk.PolicyType.AUTOMATIC if natural > FIELD_HIGH else Gtk.PolicyType.EXTERNAL
        if self.field.get_policy()[1] != policy:
            self.field.set_policy(Gtk.PolicyType.NEVER, policy)
        return GLib.SOURCE_REMOVE

    def _update_send(self) -> None:
        text = self._text().strip()
        long = units(text) > LIMIT
        if long and not self._too_long:
            # The line under the field is only seen: a screen reader hears it too, once.
            self.announce(TOO_LONG, Gtk.AccessibleAnnouncementPriority.MEDIUM)
        self._too_long = long
        look = outcome(self.outcome) if self.outcome and self.outcome != "sent" else None
        blocked = look is not None and not look.send and not look.done
        self.send_button.set_sensitive(bool(text) and not long and not self.sending and not blocked
                                       and self.outcome != "sent")
        self._show_problem()

    def _show_problem(self) -> None:
        """The line under the field: what's wrong, and the one thing to do about it. Too long
        comes first: it's what stops Send now (the page's answer shows again once it's shorter)."""
        look = outcome(self.outcome) if self.outcome and self.outcome != "sent" and not self.sending else None
        if self._too_long:
            line, self.action, kind = TOO_LONG, "chat", "error"
        elif look is not None:
            line, self.action, kind = look.line, look.button, "warning" if look.done else "error"
        else:
            line, self.action, kind = "", "", "error"
        markup = GLib.markup_escape_text(line)
        if self.action:
            # Its words kept together: half of it at the end of one line and half on the next is no link.
            action = GLib.markup_escape_text(ACTIONS[self.action]).replace(" ", "\u00a0")
            markup += f'. <a href="natter:{self.action}"><b>{action}</b></a>'
        if self.problem_text.get_label() != markup:  # at every key: laid out again only for a change
            self.problem_text.set_markup(markup)
        # Unclear whether it went (done) in the warning colour; the rest didn't go, in the error's.
        for name in ("error", "warning"):
            if name == kind:
                self.problem.add_css_class(name)
            else:
                self.problem.remove_css_class(name)
        self.problem.set_visible(bool(line))

    def _show_state(self) -> None:
        sent = self.outcome == "sent"
        focus = self.get_focus()  # before Send is off, and the focus goes with it
        self.send_face.set_visible_child_name("sending" if self.sending else "send")
        if self.sending:
            self.composer.add_css_class("natter-sending")
        else:
            self.composer.remove_css_class("natter-sending")
        self.entry.set_editable(not self.sending and not sent)
        self.entry.set_cursor_visible(not self.sending and not sent)
        if (self.sending or sent) and (focus is self.entry or focus is self.send_button):
            # No caret in words that can't change now (GTK draws one while it has the focus), and
            # Enter has nothing to do. Back to the field when it can be written in again, whether
            # the send came from there or from Send (which is off meanwhile, and loses the focus).
            self.set_focus(None)
            self._refocus = True
        elif not (self.sending or sent) and self._refocus:
            self._refocus = False
            self.entry.grab_focus()
        self.who.set_sensitive(not self.sending and not sent)
        self._update_send()

    # ---- Sending ----------------------------------------------------------------------------

    def _on_key(self, controller: Gtk.EventControllerKey, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        """Enter (or Ctrl+Enter) sends; with Shift it's a new line. An input method composing a
        word gets Enter first, to finish the word."""
        if keyval not in ENTER or state & Gdk.ModifierType.SHIFT_MASK:
            return False
        if self.entry.im_context_filter_keypress(controller.get_current_event()):
            return True
        self._send()
        return True

    def _send(self) -> None:
        """The one way a reply is sent: Enter, Ctrl+Enter, Alt+S and Send all come here. Never
        while a send is under way or Send is off (an answer that may have sent turns it off)."""
        if self.sending:
            return
        text = self._text().strip()
        if units(text) > LIMIT:
            self.announce(TOO_LONG, Gtk.AccessibleAnnouncementPriority.MEDIUM)  # why Enter does nothing
            return
        if not text or not self.send_button.get_sensitive():
            return
        self.sending = True
        self.outcome = None
        self._show_state()
        self.announce("Sending reply", Gtk.AccessibleAnnouncementPriority.MEDIUM)
        self.app.send_reply(self.target, text, self._answered)

    def _answered(self, result: str) -> None:
        if not self.sending:
            return  # Account answers once; an answer it gave up waiting for is only logged
        self.sending = False
        if result == "sent":
            self._sent()
        elif result == "removed":
            self.forget()
        else:
            self.set_outcome(result)
            if not self.get_visible():
                self.app.reply_failed(self, self.outcome or result)  # what it shows now, KDE's answers too

    def _sent(self) -> None:
        self.app.reply_drafts.pop(self.key, None)
        self.buffer.set_text("")
        if self.queued:
            # More came from the notification meanwhile: it stays for that.
            self.outcome = None
            self.announce("Reply sent", Gtk.AccessibleAnnouncementPriority.MEDIUM)
            self._replay()
            if self.outcome and not self.get_visible():
                self.app.reply_failed(self, self.outcome)
            return
        if not self.get_visible():
            self._forget_now()
            return
        self.outcome = "sent"
        self._show_state()
        # Said, then gone at once, as a notification goes once it's answered: out of sight, and out
        # of app.replies (Reply for this chat now opens a new one). Destroyed once a screen reader
        # has asked about what was said (SENT_HEARD).
        self.announce("Reply sent", Gtk.AccessibleAnnouncementPriority.MEDIUM)
        self._unregister()
        self.set_visible(False)
        self._closing = GLib.timeout_add(SENT_HEARD, self._close_after_sent)

    def _close_after_sent(self) -> bool:
        self._closing = 0
        self._forget_now()
        return GLib.SOURCE_REMOVE

    def _replay(self) -> None:
        """What came from KDE's reply field while sending, now that the send has answered: words
        to send into the field, and the page's answers to the rest shown as inline_failed shows
        them (words WhatsApp may have, to read)."""
        queued, self.queued = self.queued, []
        for text, result in queued:
            if result is None:
                self.put_text(text)
            else:
                self.inline_failed(text, result)
        self._show_state()

    def _show_yours(self, text: str) -> None:
        """A reply that's over, as a bubble of yours above the field, under any before it. A scroll
        bar only if it's long: one that could show makes it as tall as the bar's own least height,
        short or not."""
        before = self.yours.get_label() if self.yours_box.get_visible() else ""
        self.yours.set_label(f"{before}\n\n{text.strip()}" if before else text.strip())
        # Its widest: the window's, less its margins, the room kept on the far side, and its padding.
        width = max(self.get_width() - 24 - 40 - 20, 200)
        natural = self.yours.measure(Gtk.Orientation.VERTICAL, width)[1]
        # Its height as least height too: the window keeps its size once it's up, and the messages
        # above give way to it rather than it to them.
        self.yours_scroller.set_min_content_height(min(natural, YOURS_HIGH))
        self.yours_scroller.set_policy(Gtk.PolicyType.NEVER,
                                       Gtk.PolicyType.AUTOMATIC if natural > YOURS_HIGH else Gtk.PolicyType.EXTERNAL)
        self.yours_box.set_visible(True)

    def set_outcome(self, result: str) -> None:
        """Show the page's answer (OUTCOMES); `result` isn't "sent" or "removed"."""
        look = outcome(result)
        self.outcome = result if result in OUTCOMES else "unknown"
        if look.done:
            # Over: its words stay to read, never to send again, and the field is for a new reply.
            self._show_yours(self._text())
            self.buffer.set_text("")
            self.app.reply_drafts.pop(self.key, None)
        self._show_state()
        self.announce(look.line, Gtk.AccessibleAnnouncementPriority.MEDIUM)
        if self.queued:
            self._replay()

    # ---- The way out ------------------------------------------------------------------------

    def _open_chat(self) -> None:
        """The chat in NAME's window (through the lock). Words typed here stay here. Never while
        sending: opening a chat in the page would get in the way of the send."""
        if self.sending or self.outcome == "sent":
            return
        target = self.target
        self.app._open_from_notification(target.account, target.title, target.page_id, "")
        if not self._text().strip():
            self.close()

    def _action_clicked(self) -> None:
        if self.action == "app":
            self.app.open_account(self.target.account)
        elif self.action == "chat":
            self._open_chat()

    def present_again(self, token: str = "") -> None:
        if token:
            self.set_startup_id(token)
        self.present()
        if self.entry.get_editable():
            self.entry.grab_focus()

    def _on_close_request(self, _window) -> bool:
        if self._closing:  # closed in the moment between sent and gone
            GLib.source_remove(self._closing)
            self._closing = 0
        if self.sending:
            # The send goes on, out of sight; its answer comes back here (a notice if it failed).
            self.set_visible(False)
            return True
        if self.outcome != "sent":
            text = self._text()
            if text.strip():
                self.app.reply_drafts[self.key] = text
            else:
                self.app.reply_drafts.pop(self.key, None)
        self._unregister()
        return False

    def close_for_lock(self) -> bool:
        """Natter locked: out of sight, with what was typed kept for after the unlock. True if it
        was on show with words in it, to open again then. A send under way goes on."""
        if self.sending:
            self.set_visible(False)
            return False
        if not self.get_visible():
            return False
        kept = bool(self._text().strip())
        self.close()
        return kept

    def forget(self) -> None:
        """Gone without keeping anything (its account was removed)."""
        self.sending = False
        self._forget_now()

    def _forget_now(self) -> None:
        if self._closing:
            GLib.source_remove(self._closing)
            self._closing = 0
        self._unregister()
        self.destroy()

    def untell(self) -> None:
        """Its notice goes (app.reply_failed): it's on show again, or over."""
        self.told, self.told_named = None, False
        self.app.withdraw_notification(f"reply-{self.ref}")

    def _unregister(self) -> None:
        for timer in ("_fitting", "_sticking"):
            if getattr(self, timer):
                GLib.source_remove(getattr(self, timer))
                setattr(self, timer, 0)
        if self._contrast:  # the style manager outlives it, and would keep it
            Adw.StyleManager.get_default().disconnect(self._contrast)
            self._contrast = 0
        if self.told:
            self.untell()
        if self.app.replies.get(self.key) is self:
            del self.app.replies[self.key]
