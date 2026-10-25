"""Reply from a notification: which notifications offer it and how (notify.py), what KDE's reply
field may send and what it may not, the targets a desktop may keep (opaque references), the
reply window's keys, its one way of sending and what each answer from the page does (reply.py),
the app's side of it, locked and not (app.py), and starting again when calls have lost the
microphone and camera. A stand-in notification server and stand-in widgets: no display, no
bus, nothing reaches the desktop."""

import base64
import io
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from collections import OrderedDict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from natter import NAME, notify, reply
from natter.app import RESTARTER, Natter, banner_for, own_children, restart_command, start_again
from natter.notify import REPLY_GRACE, Notifier, ReplyTarget, replyable
from natter.reply import LIMIT, OUTCOMES, UNCLEAR, ReplyWindow, joined, notice, outcome, units, window_title
from natter.account import REPLY_RESULTS
from natter.settings import DEFAULTS, Settings

PERSON = "4917612345678@c.us"
GROUP = "120363041234567890@g.us"
SERVER = ":1.42"


def settings(test, **values):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    found = Settings(Path(folder.name) / "settings.json")
    for key, value in values.items():
        found[key] = value
    return found


class Bus:
    """The notification server as Natter talks to it: Notify answers with the next id at once."""

    def __init__(self):
        self.calls, self.next_id = [], 100

    def call(self, _name, _path, _iface, method, params, _reply, _flags, _timeout, _cancel, callback=None, data=None):
        self.calls.append((method, params.unpack() if params is not None else None))
        if method == "Notify" and callback is not None:
            replaces = params.unpack()[1]
            self.next_id += 0 if replaces else 1
            callback(self, replaces or self.next_id, data)

    def call_finish(self, result):
        return GLib.Variant("(u)", (result,))

    def notified(self):
        """Each Notify: (actions, hints as plain values)."""
        return [(params[5], {key: value for key, value in params[6].items()})
                for method, params in self.calls if method == "Notify"]

    def closed(self):
        return [params[0] for method, params in self.calls if method == "CloseNotification"]


class Notification:
    """A Gio.Notification as notify.py makes one, for GNotification's path."""

    made = []

    def __init__(self, title):
        self.title, self.default, self.buttons = title, None, []
        Notification.made.append(self)

    @classmethod
    def new(cls, title):
        return cls(title)

    def set_body(self, body):
        self.body = body

    def set_category(self, category):
        pass

    def set_icon(self, icon):
        pass

    def set_default_action_and_target(self, action, target):
        self.default = (action, target.unpack())

    def add_button_with_target(self, label, action, target):
        self.buttons.append((label, action, target.unpack()))


class NotifierTest(unittest.TestCase):
    def make(self, capabilities=("actions", "body"), locked=False, server=SERVER, **values):
        found = Notifier.__new__(Notifier)  # never initialised: no bus, no actions on an app
        self.opened, self.replies, self.posted, self.withdrawn, self.accounts = [], [], {}, [], []
        found.app = SimpleNamespace(locked=locked, send_notification=self.posted.__setitem__,
                                    withdraw_notification=self.withdrawn.append, open_account=self.accounts.append)
        found.settings = settings(self, **values)
        found.on_open = lambda *args: self.opened.append(args)
        found.on_reply = lambda *args: self.replies.append(args)
        found.bus = Bus()
        found.shown, found.by_server, found.by_ref, found.tokens, found.muted = {}, {}, {}, {}, {}
        found.avatars, found.given = OrderedDict(), OrderedDict()
        found.server = server
        found.capabilities = None if capabilities is None else set(capabilities)
        found._warned = False
        timers = mock.patch.object(GLib, "timeout_add_seconds", side_effect=self.later)
        timers.start()
        self.addCleanup(timers.stop)
        self.timers = []
        return found

    def later(self, seconds, run, *data):
        self.timers.append((seconds, run, data))
        return len(self.timers)

    def run_timers(self):
        timers, self.timers = self.timers, []
        for _seconds, run, data in timers:
            run(*data)

    def message(self, notifier, tag=PERSON, page_id=7, title="Mum", body="Dinner?"):
        notifier.show("main", None, page_id, title, body, tag, "")
        return notifier.shown[("main", tag or title)]

    def signal(self, notifier, name, *values, sender=SERVER):
        types_ = {"ActionInvoked": "(us)", "NotificationClosed": "(uu)", "ActivationToken": "(us)",
                  "NotificationReplied": "(us)"}
        notifier._on_signal(None, sender, notify.PATH, notify.BUS, name, GLib.Variant(types_[name], values))


class ReplyableTest(unittest.TestCase):
    def test_chats(self):
        for tag in (PERSON, GROUP, "4917612345678-1600000000@g.us", "123456789012345@lid"):
            self.assertTrue(replyable(tag, 7), tag)

    def test_not_chats(self):
        for tag in ("", "Mum", "status@broadcast", "1203@newsletter", "120363041234567890@newsletter",
                    "call-accepted-elsewhere:123@c.us", "call-link-joined:x", "waiting-room:x", "wa-web-call-active",
                    "false_4917612345678@c.us_ABC", "4917612345678@c.us ", "1234@c.us"):
            self.assertFalse(replyable(tag, 7), tag)

    def test_webkits_own_notifications_and_none(self):
        for page_id in (0, -1, -40):
            self.assertFalse(replyable(PERSON, page_id))


class ActionsTest(NotifierTest):
    def test_reply_comes_before_mute(self):
        notifier = self.make()
        self.message(notifier)
        (actions, hints), = notifier.bus.notified()
        self.assertEqual(actions, ["default", "Open", "reply", "Reply", "mute", "Mute Chat for 1 Hour"])
        self.assertFalse([key for key in hints if key.startswith("x-kde-reply")])

    def test_kdes_reply_field_when_the_bubble_says_who(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        self.message(notifier)
        (actions, hints), = notifier.bus.notified()
        self.assertEqual(actions[2:4], ["inline-reply", "Reply"])
        self.assertEqual({key: hints[key] for key in hints if key.startswith("x-kde-reply")},
                         {"x-kde-reply-placeholder-text": "Type a reply", "x-kde-reply-submit-button-text": "Send",
                          "x-kde-reply-submit-button-icon-name": "document-send"})

    def test_the_reply_window_when_the_sender_is_hidden(self):
        for values in ({"notify_sender": False}, {"privacy_blur": True}):
            notifier = self.make(capabilities=("actions", "body", "inline-reply"), **values)
            self.message(notifier)
            (actions, hints), = notifier.bus.notified()
            self.assertEqual(actions[2:4], ["reply", "Reply"], values)
            self.assertFalse([key for key in hints if key.startswith("x-kde-reply")], values)

    def test_locked_a_plain_reply_that_asks_for_the_passcode(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"), locked=True)
        self.message(notifier)
        (actions, hints), = notifier.bus.notified()
        self.assertEqual(actions[2:4], ["reply", "Reply"])
        self.assertFalse([key for key in hints if key.startswith("x-kde-reply")])

    def test_turned_off(self):
        notifier = self.make(notify_reply=False)
        self.message(notifier)
        (actions, _hints), = notifier.bus.notified()
        self.assertEqual(actions, ["default", "Open", "mute", "Mute Chat for 1 Hour"])

    def test_a_server_without_actions(self):
        notifier = self.make(capabilities=("body",))
        self.message(notifier)
        (actions, _hints), = notifier.bus.notified()
        self.assertEqual(actions, [])

    def test_only_a_chats_messages(self):
        for tag, page_id in (("", 7), ("status@broadcast", 7), ("call-accepted-elsewhere:1@c.us", 7), (PERSON, -3)):
            notifier = self.make()
            notifier.show("main", None, page_id, "Mum", "hi", tag, "")
            (actions, _hints), = notifier.bus.notified()
            self.assertNotIn("Reply", actions, tag)

    def test_locking_takes_the_reply_field_away(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        self.message(notifier)
        notifier.app.locked = True
        notifier.conceal()
        (_first, _), (actions, hints) = notifier.bus.notified()
        self.assertEqual(actions[2:4], ["reply", "Reply"])
        self.assertFalse([key for key in hints if key.startswith("x-kde-reply")])

    def test_turning_reply_off_takes_it_off_bubbles_already_up(self):
        notifier = self.make()
        self.message(notifier)
        notifier.settings["notify_reply"] = False
        notifier.refresh()
        (first, _), (actions, _hints) = notifier.bus.notified()
        self.assertIn("Reply", first)
        self.assertEqual(actions, ["default", "Open", "mute", "Mute Chat for 1 Hour"])

    def test_the_latest_three_messages(self):
        notifier = self.make()
        for n in range(5):
            shown = self.message(notifier, page_id=10 + n, body=f"message {n}")
        self.assertEqual(shown.messages, ["message 2", "message 3", "message 4"])
        self.assertEqual((shown.count, shown.page_id), (5, 14))
        target = notifier.target(shown)
        self.assertEqual((target.chat, target.title, target.page_id, target.count), (PERSON, "Mum", 14, 5))
        target.messages.append("more")  # a copy: the window's changes stay the window's
        self.assertEqual(len(shown.messages), 3)


class AvatarTest(NotifierTest):
    """The sender's picture: cached per chat by a digest of its address and kept decoded; the
    address itself (a data: URL is the whole picture, base64) is dropped once decoded, so no
    Shown and no cache entry holds hundreds of kilobytes of string."""

    def data_url(self, colour):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 8, 8)
        pixbuf.fill(colour)
        ok, png = pixbuf.save_to_bufferv("png", [], [])
        self.assertTrue(ok)
        return "data:image/png;base64," + base64.b64encode(bytes(png)).decode()

    def test_cached_by_digest_and_kept_decoded(self):
        notifier = self.make()
        url = self.data_url(0xff0000ff)
        decoded, original = [], notify.decode_image
        with mock.patch.object(notify, "decode_image", side_effect=lambda raw: decoded.append(1) or original(raw)):
            notifier.show("main", None, 7, "Mum", "Dinner?", PERSON, url)
            shown = notifier.shown[("main", PERSON)]
            self.assertEqual(len(decoded), 1)
            self.assertIsInstance(shown.icon, GdkPixbuf.Pixbuf)
            self.assertEqual(shown.icon_key, notify.avatar_key(url))
            self.assertEqual(shown.icon_url, "")
            digest, picture = notifier.avatars[("main", PERSON)]
            self.assertEqual(digest, notify.avatar_key(url))
            self.assertEqual(len(digest), 32)
            self.assertIs(picture, shown.icon)
            self.assertNotIn(url, repr(vars(shown)) + repr(notifier.avatars))
            (_actions, hints), = notifier.bus.notified()
            self.assertIn("image-data", hints)
            self.assertIs(notifier.target(shown).picture, shown.icon)
            notifier.show("main", None, 8, "Mum", "Hello?", PERSON, url)  # the same picture: from the cache
            self.assertEqual(len(decoded), 1)
            self.assertIs(shown.icon, picture)
            other = self.data_url(0x00ff00ff)
            notifier.show("main", None, 9, "Mum", "Still there?", PERSON, other)  # a new one: fetched, cached
            self.assertEqual(len(decoded), 2)
            self.assertIsNot(shown.icon, picture)
            self.assertEqual(notifier.avatars[("main", PERSON)][0], notify.avatar_key(other))
            self.assertEqual(shown.icon_url, "")
            notifier.show("main", None, 10, "Mum", "?", PERSON, "")  # none named: none shown
            self.assertIsNone(shown.icon)
            self.assertEqual((shown.icon_key, shown.icon_url), ("", ""))

    def test_one_that_cannot_be_had_keeps_the_picture_the_bubble_has(self):
        notifier = self.make()
        url = self.data_url(0xff0000ff)
        notifier.show("main", None, 7, "Mum", "Dinner?", PERSON, url)
        shown = notifier.shown[("main", PERSON)]
        picture = shown.icon
        broken = "data:image/png;base64,AAAA"
        notifier.show("main", None, 8, "Mum", "Hello?", PERSON, broken)
        self.assertIs(shown.icon, picture)  # the old one stays on the bubble
        self.assertEqual(shown.icon_url, broken)  # tried again at the next delivery
        self.assertEqual(notifier.avatars[("main", PERSON)][0], notify.avatar_key(url))
        self.assertEqual(len(notifier.bus.notified()), 2)

    def test_avatar_key(self):
        self.assertEqual(notify.avatar_key(""), "")
        self.assertEqual(notify.avatar_key("https://pps.whatsapp.net/a"), notify.avatar_key("https://pps.whatsapp.net/a"))
        self.assertNotEqual(notify.avatar_key("https://pps.whatsapp.net/a"), notify.avatar_key("https://pps.whatsapp.net/b"))
        self.assertEqual(len(notify.avatar_key("x" * 300_000)), 32)


class SignalTest(NotifierTest):
    def test_reply_opens_the_window_and_leaves_the_bubble(self):
        notifier = self.make()
        shown = self.message(notifier)
        self.signal(notifier, "ActivationToken", shown.server_id, "token-1")
        self.signal(notifier, "ActionInvoked", shown.server_id, "reply")
        (target, token, text, refused), = self.replies
        self.assertEqual((target.chat, token, text, refused), (PERSON, "token-1", None, False))
        self.assertEqual(self.opened, [])
        self.assertIs(notifier.shown[("main", PERSON)], shown)
        self.assertEqual(notifier.bus.closed(), [])

    def test_open_still_opens(self):
        notifier = self.make()
        shown = self.message(notifier)
        self.signal(notifier, "ActionInvoked", shown.server_id, "default")
        self.assertEqual(self.opened, [("main", "Mum", 7, "")])
        self.assertEqual(self.replies, [])

    def test_only_the_servers_own_signals(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        for name, values in (("ActionInvoked", (shown.server_id, "reply")),
                             ("NotificationReplied", (shown.server_id, "hello")),
                             ("ActionInvoked", (shown.server_id, "default"))):
            self.signal(notifier, name, *values, sender=":1.99")
        self.assertEqual((self.replies, self.opened), ([], []))

    def test_kdes_text_once_per_field(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        self.signal(notifier, "NotificationReplied", shown.server_id, "On my way")
        self.signal(notifier, "NotificationReplied", shown.server_id, "On my way")
        (first, _, text, refused), (_second, _, again, refused_again) = self.replies
        self.assertEqual((first.chat, text, refused), (PERSON, "On my way", False))
        self.assertEqual((again, refused_again), ("On my way", True))  # kept for the window, never sent

    def test_a_newer_message_gives_a_new_field(self):
        # The same bubble, posted again: a second reply to that chat isn't lost.
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        self.signal(notifier, "NotificationReplied", shown.server_id, "first")
        self.message(notifier, page_id=8, body="And dessert?")
        self.signal(notifier, "NotificationReplied", shown.server_id, "second")
        self.assertEqual([(text, refused) for _t, _k, text, refused in self.replies],
                         [("first", False), ("second", False)])

    def test_typed_while_locking_is_answered_not_dropped(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        notifier.app.locked = True
        notifier.conceal()  # the field on screen was replaced by one without it
        self.signal(notifier, "NotificationReplied", shown.server_id, "typed before the lock")
        (_target, _token, text, refused), = self.replies
        self.assertEqual((text, refused), ("typed before the lock", False))  # app.on_reply keeps it: locked

    def test_a_bubble_without_a_field(self):
        notifier = self.make()  # no inline-reply: Reply opened the window instead
        shown = self.message(notifier)
        self.signal(notifier, "NotificationReplied", shown.server_id, "hello")
        (_target, _token, text, refused), = self.replies
        self.assertEqual((text, refused), ("hello", True))

    def test_a_bubble_natter_has_forgotten(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        server_id = shown.server_id
        self.signal(notifier, "NotificationClosed", server_id, 2)
        self.run_timers()  # forgotten after REPLY_GRACE
        self.signal(notifier, "NotificationReplied", server_id, "hello")
        self.assertEqual(self.replies, [(None, "", "hello", True)])  # said to be lost, not dropped

    def test_another_apps_bubble(self):
        # A server that sends every app's signals to every app: a reply typed in another app's
        # notification is none of Natter's business.
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        self.message(notifier)
        self.signal(notifier, "NotificationReplied", 4242, "hello")
        self.signal(notifier, "ActivationToken", 4242, "token")
        self.assertEqual((self.replies, notifier.tokens), ([], {}))

    def test_closed_then_replied(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        server_id = shown.server_id
        self.signal(notifier, "NotificationClosed", server_id, 2)
        self.signal(notifier, "NotificationReplied", server_id, "just in time")
        self.assertEqual([(text, refused) for _t, _k, text, refused in self.replies], [("just in time", False)])

    def test_a_field_long_gone(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        server_id = shown.server_id
        self.signal(notifier, "NotificationClosed", server_id, 2)
        later = time.monotonic() + REPLY_GRACE + 1
        with mock.patch.object(notify.time, "monotonic", return_value=later):
            self.signal(notifier, "NotificationReplied", server_id, "too late")
        self.assertEqual([(text, refused) for _t, _k, text, refused in self.replies], [("too late", True)])
        self.run_timers()  # and after REPLY_GRACE, the bubble is forgotten
        self.assertNotIn(server_id, notifier.by_server)
        self.assertNotIn(shown.ref, notifier.by_ref)

    def test_withdrawn_bubbles_are_known_a_while(self):
        notifier = self.make(capabilities=("actions", "body", "inline-reply"))
        shown = self.message(notifier)
        server_id = shown.server_id
        notifier.withdraw("main", PERSON)
        self.assertEqual(notifier.bus.closed(), [server_id])
        self.signal(notifier, "NotificationReplied", server_id, "crossed")
        self.assertEqual([(text, refused) for _t, _k, text, refused in self.replies], [("crossed", False)])
        self.run_timers()
        self.assertNotIn(server_id, notifier.by_server)
        self.assertNotIn(shown.ref, notifier.by_ref)


class PortalTest(NotifierTest):
    """GNotification (the Flatpak, or no server): buttons, and targets that name no chat."""

    def post(self, **values):
        notifier = self.make(server=None, **values)
        Notification.made.clear()
        with mock.patch.object(notify.Gio, "Notification", Notification):
            shown = self.message(notifier)
        notification, = Notification.made
        return notifier, shown, notification

    def test_buttons_and_targets(self):
        _notifier, shown, notification = self.post()
        self.assertRegex(shown.ref, r"^[0-9a-f]{16}$")
        self.assertEqual(notification.default, ("app.notification-open", ("main", shown.ref, "Mum", 7)))
        self.assertEqual(notification.buttons, [("Reply", "app.notification-reply", ("main", shown.ref)),
                                                ("Mute Chat for 1 Hour", "app.notification-mute", ("main", shown.ref))])
        everything = repr((notification.default, notification.buttons))
        self.assertNotIn(PERSON, everything)
        self.assertNotIn("4917612345678", everything)

    def test_the_id_names_no_chat(self):
        # GNOME Shell keeps a notification's id on disk with it, sender hidden or not.
        for values in ({}, {"notify_sender": False}):
            notifier, shown, _notification = self.post(**values)
            (gid,) = self.posted
            self.assertEqual(gid, f"chat-{shown.ref}")
            self.assertNotIn("4917612345678", gid)
            self.assertNotIn("Mum", gid)
            # Its next message replaces it in place; so does hiding who it's from at the lock.
            with mock.patch.object(notify.Gio, "Notification", Notification):
                self.message(notifier, page_id=8, body="Still there?")
                notifier.app.locked = True
                notifier.conceal()
            self.assertEqual(list(self.posted), [gid])
            notifier.withdraw("main", PERSON)
            self.assertEqual(self.withdrawn[-1], gid)

    def test_no_name_when_the_bubble_shows_none(self):
        _notifier, shown, notification = self.post(notify_sender=False)
        self.assertEqual(notification.default, ("app.notification-open", ("main", shown.ref, "", 7)))
        self.assertNotIn("Mum", repr((notification.default, notification.buttons)))

    def test_reply_turned_off(self):
        _notifier, _shown, notification = self.post(notify_reply=False)
        self.assertEqual([label for label, _a, _t in notification.buttons], ["Mute Chat for 1 Hour"])

    def test_the_actions(self):
        notifier, shown, _notification = self.post()
        notifier._reply_action("main", shown.ref)
        (target, token, text, refused), = self.replies
        self.assertEqual((target.chat, token, text, refused), (PERSON, "", None, False))
        notifier._reply_action("main", "0123456789abcdef")  # from before Natter last started
        self.assertEqual(self.accounts, ["main"])
        notifier._reply_action("work", shown.ref)  # another account's reference is no reference
        self.assertEqual(self.accounts, ["main", "work"])
        notifier._open_action("main", shown.ref, "", 0)
        self.assertEqual(self.opened, [("main", "Mum", 7, "")])  # the bubble's own name and page id

    def test_mute_by_reference(self):
        notifier, shown, _notification = self.post()
        notifier._mute_action("main", "0123456789abcdef")
        self.assertEqual(notifier.muted, {})
        notifier._mute_action("main", shown.ref)
        self.assertIn(("main", PERSON), notifier.muted)
        self.assertNotIn(("main", PERSON), notifier.shown)

    def test_references_go_a_while_after_the_bubble(self):
        notifier, shown, _notification = self.post()
        notifier.withdraw("main", PERSON)
        self.assertIn(shown.ref, notifier.by_ref)  # a click may still come
        self.run_timers()
        self.assertNotIn(shown.ref, notifier.by_ref)


# ---- The reply window (reply.py) ---------------------------------------------------------------


class Fake:
    """Records the methods called on it, by name."""

    def __init__(self, **values):
        self.calls = []
        self.__dict__.update(values)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, *args))
            return None
        return method

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Buffer(Fake):
    def __init__(self, text=""):
        super().__init__(text=text)

    def get_text(self, _start, _end, _hidden):
        return self.text

    def set_text(self, text, *_length):
        self.text = text


class Switch(Fake):
    def __init__(self, sensitive=True):
        super().__init__(sensitive=sensitive)

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive

    def get_sensitive(self):
        return self.sensitive


class Label(Fake):
    def measure(self, _orientation, _for_size):
        return 20, 20, -1, -1


class Shown(Fake):
    """A widget whose visibility is looked at."""

    def __init__(self, visible=False):
        super().__init__(visible=visible)

    def set_visible(self, visible):
        self.visible = visible

    def get_visible(self):
        return self.visible


class Markup(Fake):
    """A label given markup: what it says is the markup."""

    def __init__(self):
        super().__init__(label="")

    def set_markup(self, markup):
        self.calls.append(("set_markup", markup))
        self.label = markup

    def get_label(self):
        return self.label


class Entry(Fake):
    def __init__(self):
        super().__init__(editable=True, filtered=False)

    def set_editable(self, editable):
        self.editable = editable

    def get_editable(self):
        return self.editable

    def im_context_filter_keypress(self, _event):
        return self.filtered


METHODS = ("_text", "_send", "_answered", "_sent", "set_outcome", "_update_send", "_show_problem", "_show_state",
           "put_text", "unsent", "_on_close_request", "close_for_lock", "inline_failed", "_on_key", "_unregister",
           "_forget_now", "forget", "_open_chat", "_action_clicked", "present_again", "untell", "_show_yours",
           "_replay", "_reveal", "_close_after_sent", "show_inline", "inline_answered", "loan_ended")


def target(**values):
    return ReplyTarget(**{"account": "main", "chat": PERSON, "title": "Mum", "page_id": 7,
                          "messages": ["Dinner?"], **values})


class App(Fake):
    """The app as a reply window sees it."""

    def __init__(self, test, **values):
        super().__init__(replies={}, reply_drafts={}, settings=settings(test, **values), sends=[], failed=[], hosts=[])

    def send_reply(self, target, text, done, host=None):
        self.sends.append((target, text, done))
        self.hosts.append(host)

    def reply_failed(self, window, result):
        self.failed.append(result)


def window(test, text="", **values):
    app = App(test, **values)
    found = Fake(app=app, target=target(), key=("main", PERSON), ref="0123456789abcdef", sending=False, outcome=None,
                 queued=[], revealed=False, told=None, told_named=False, _too_long=False, _closing=0, _fitting=0,
                 _sticking=0, _refocus=False, _contrast=0, action="", hidden_row=Fake(), holding=None,
                 _hide_when_back=False, _inline_draft="",
                 visible=True, buffer=Buffer(text), entry=Entry(), send_button=Switch(), send_face=Fake(),
                 composer=Fake(), who=Switch(), problem=Shown(), problem_text=Markup(), yours=Label(),
                 yours_box=Fake(), yours_scroller=Fake())
    found.get_width = lambda: 360
    found.get_visible = lambda: found.visible
    # Recorded with the rest: what's said comes before the window goes.
    found.set_visible = lambda visible: (found.calls.append(("set_visible", visible)),
                                         setattr(found, "visible", visible))
    for name in METHODS:
        setattr(found, name, types.MethodType(getattr(ReplyWindow, name), found))
    app.replies[found.key] = found
    found._update_send()
    return found


class Event:
    pass


def key(found, keyval, state=0):
    controller = SimpleNamespace(get_current_event=lambda: Event())
    return found._on_key(controller, keyval, 0, Gdk.ModifierType(state))


class KeysTest(unittest.TestCase):
    def test_enter_sends(self):
        for keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            found = window(self, "On my way")
            self.assertTrue(key(found, keyval))
            self.assertEqual([text for _t, text, _d in found.app.sends], ["On my way"], keyval)

    def test_ctrl_enter_sends_too(self):
        found = window(self, "On my way")
        self.assertTrue(key(found, Gdk.KEY_Return, Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(len(found.app.sends), 1)

    def test_shift_enter_is_a_new_line(self):
        found = window(self, "On my way")
        self.assertFalse(key(found, Gdk.KEY_Return, Gdk.ModifierType.SHIFT_MASK))  # the text view's own
        self.assertEqual(found.app.sends, [])

    def test_an_input_method_finishing_a_word(self):
        found = window(self, "On my way")
        found.entry.filtered = True
        self.assertTrue(key(found, Gdk.KEY_Return))
        self.assertEqual(found.app.sends, [])

    def test_other_keys_type(self):
        found = window(self, "On my way")
        self.assertFalse(key(found, Gdk.KEY_a))


class SendTest(unittest.TestCase):
    def test_one_press_one_send(self):
        found = window(self, "  On my way \n")
        found._send()
        (sent_to, text, _done), = found.app.sends
        self.assertEqual((sent_to.chat, text), (PERSON, "On my way"))
        self.assertTrue(found.sending)
        self.assertEqual(found.called("announce")[0][0], "Sending reply")
        # Enter again, Ctrl+Enter, the button: nothing more while it's under way.
        key(found, Gdk.KEY_Return)
        key(found, Gdk.KEY_Return, Gdk.ModifierType.CONTROL_MASK)
        found._send()
        self.assertEqual(len(found.app.sends), 1)
        self.assertFalse(found.send_button.sensitive)
        self.assertFalse(found.entry.editable)

    def test_nothing_to_send(self):
        for text in ("", "   \n\t"):
            found = window(self, text)
            found._send()
            self.assertEqual(found.app.sends, [])
            self.assertFalse(found.send_button.sensitive)

    def test_too_long_counts_as_the_page_does(self):
        found = window(self, "👍" * (LIMIT // 2))
        self.assertTrue(found.send_button.sensitive)
        found.buffer.text += "👍"
        found._update_send()
        self.assertFalse(found.send_button.sensitive)
        # Said under the field, with the way to send it: the chat itself.
        self.assertTrue(found.problem.visible)
        self.assertEqual(found.action, "chat")
        self.assertTrue(found.problem_text.label.startswith(reply.TOO_LONG + ". <a "))
        found._send()
        self.assertEqual(found.app.sends, [])
        self.assertEqual(units("👍"), 2)
        self.assertEqual(units("é"), 1)

    def test_too_long_is_said_too(self):
        found = window(self, "👍" * (LIMIT // 2))
        said = lambda: [text for text, _priority in found.called("announce")]  # noqa: E731
        found.buffer.text += "👍"
        found._update_send()
        found.buffer.text += "!"
        found._update_send()  # once as it goes over, not at every key
        self.assertEqual(said(), [reply.TOO_LONG])
        key(found, Gdk.KEY_Return)  # and why Enter does nothing
        self.assertEqual(said(), [reply.TOO_LONG, reply.TOO_LONG])
        self.assertEqual(found.app.sends, [])
        found.buffer.text = "On my way"
        found._update_send()
        found.buffer.text = "👍" * (LIMIT // 2 + 1)
        found._update_send()  # over again: said again
        self.assertEqual(said().count(reply.TOO_LONG), 3)

    def test_sent_goes_at_once(self):
        found = window(self, "On my way")
        found.app.reply_drafts[found.key] = "old"
        found._send()
        with mock.patch.object(GLib, "timeout_add", return_value=9) as later:
            found.app.sends[0][2]("sent")
        self.assertEqual(found.outcome, "sent")
        self.assertNotIn(found.key, found.app.reply_drafts)
        self.assertFalse(found.send_button.sensitive)
        # Said, then out of sight straight away, as an answered notification goes.
        said = found.calls.index(("announce", "Reply sent", Gtk.AccessibleAnnouncementPriority.MEDIUM))
        self.assertGreater(found.calls.index(("set_visible", False)), said)
        self.assertFalse(found.visible)
        # Out of the app's replies at once: Reply on that chat now opens a new window, and KDE's
        # words for it go there (or to its kept words), not into this one.
        self.assertNotIn(found.key, found.app.replies)
        # Destroyed a moment later, once a screen reader has asked about what was said.
        self.assertEqual(found.called("destroy"), [])
        self.assertEqual(later.call_args[0][0], reply.SENT_HEARD)
        self.assertFalse(later.call_args[0][1]())
        self.assertEqual(found.called("destroy"), [()])
        self.assertEqual(found.app.failed, [])

    def test_a_new_window_for_the_chat_meanwhile_stays(self):
        found = window(self, "On my way")
        found._send()
        with mock.patch.object(GLib, "timeout_add", return_value=9) as later:
            found.app.sends[0][2]("sent")
        newer = found.app.replies[found.key] = Fake()  # Reply pressed again before it was destroyed
        later.call_args[0][1]()
        self.assertIs(found.app.replies[found.key], newer)

    def test_sent_out_of_sight_just_goes(self):
        found = window(self, "On my way")
        found._send()
        self.assertTrue(found._on_close_request(None))  # hidden, not closed: the send goes on
        self.assertFalse(found.visible)
        found.app.sends[0][2]("sent")
        self.assertEqual(found.called("destroy"), [()])
        self.assertNotIn(found.key, found.app.replies)
        self.assertEqual(found.app.failed, [])

    def test_failed_out_of_sight_says_so(self):
        found = window(self, "On my way")
        found._send()
        found._on_close_request(None)
        found.app.sends[0][2]("not-ready")
        self.assertEqual(found.app.failed, ["not-ready"])
        self.assertIn(found.key, found.app.replies)  # its notice's Show Reply brings it back
        self.assertEqual(found.buffer.text, "On my way")

    def test_closing_keeps_what_was_typed(self):
        found = window(self, "Half a thought")
        self.assertFalse(found._on_close_request(None))
        self.assertEqual(found.app.reply_drafts[found.key], "Half a thought")
        self.assertNotIn(found.key, found.app.replies)
        empty = window(self, "")
        empty.app.reply_drafts[empty.key] = "stale"
        empty._on_close_request(None)
        self.assertNotIn(empty.key, empty.app.reply_drafts)

    def test_its_notice_goes_with_it(self):
        found = window(self, "On my way")
        found.told = "not-ready"
        found._on_close_request(None)
        self.assertEqual(found.app.called("withdraw_notification"), [("reply-0123456789abcdef",)])
        self.assertIsNone(found.told)

    def test_the_lock_closes_it_but_not_a_send(self):
        found = window(self, "Half a thought")
        self.assertTrue(found.close_for_lock())  # words in it: to open again after the unlock
        self.assertEqual(found.called("close"), [()])
        empty = window(self, "")
        self.assertFalse(empty.close_for_lock())
        self.assertEqual(empty.called("close"), [()])
        sending = window(self, "On my way")
        sending._send()
        self.assertFalse(sending.close_for_lock())
        self.assertEqual((sending.called("close"), sending.visible, sending.sending), ([], False, True))
        hidden = window(self, "Failed")
        hidden.visible = False  # failed out of sight: its notice brings it back
        self.assertFalse(hidden.close_for_lock())
        self.assertEqual(hidden.called("close"), [])

    def test_text_from_kde_while_sending_waits(self):
        found = window(self, "first")
        found._send()
        found.put_text("second")
        self.assertEqual(found.buffer.text, "first")
        with mock.patch.object(GLib, "timeout_add", return_value=9) as later:
            found.app.sends[0][2]("sent")
        self.assertEqual((found.buffer.text, found.outcome), ("second", None))  # stays open for its own Send
        later.assert_not_called()
        self.assertEqual(len(found.app.sends), 1)

    def kde_answered_while_sending(self, inline, own, visible=True):
        """A reply from the window under way when KDE's field gets the page's answer `inline`."""
        found = window(self, "from the window")
        found._send()
        found.visible = visible
        found.inline_failed("from KDE", inline)
        self.assertEqual(found.buffer.text, "from the window")  # nothing changes under the send
        with mock.patch.object(GLib, "timeout_add", return_value=9):
            found.app.sends[0][2](own)
        return found

    def test_kde_words_whatsapp_may_have_never_go_back_in_the_box(self):
        for inline in ("unsure", "unknown", "left-as-draft"):
            for own in ("sent", "busy", "unknown"):
                found = self.kde_answered_while_sending(inline, own)
                label = (inline, own)
                self.assertNotIn("from KDE", found.buffer.text, label)
                self.assertIn("from KDE", found.yours.called("set_label")[-1][0], label)
                self.assertEqual(found.outcome, inline, label)
                key(found, Gdk.KEY_Return)
                self.assertEqual([text for _t, text, _d in found.app.sends].count("from KDE"), 0, label)
                self.assertEqual(found.called("destroy"), [], label)  # stays open to say so

    def test_kde_words_that_didnt_reach_whatsapp_wait_in_the_box(self):
        found = self.kde_answered_while_sending("busy", "sent")
        self.assertEqual((found.buffer.text, found.outcome), ("from KDE", "busy"))
        self.assertTrue(found.send_button.sensitive)

    def test_kde_answers_for_a_window_out_of_sight_are_told(self):
        found = self.kde_answered_while_sending("unknown", "sent", visible=False)
        self.assertEqual(found.app.failed, ["unknown"])
        self.assertEqual(found.called("destroy"), [])
        found = self.kde_answered_while_sending("unknown", "busy", visible=False)
        self.assertEqual(found.app.failed, ["unknown"])

    def test_open_chat_never_while_sending(self):
        # Opening a chat in the page would get in the way of the send (Alt+O, or who it's from).
        found = window(self, "On my way")
        found._send()
        found._open_chat()
        self.assertEqual(found.app.called("_open_from_notification"), [])
        self.assertFalse(found.who.sensitive)
        found.app.sends[0][2]("not-ready")
        self.assertTrue(found.who.sensitive)
        found._open_chat()
        self.assertEqual(found.app.called("_open_from_notification"), [("main", "Mum", 7, "")])
        self.assertEqual(found.called("close"), [])  # words in it: it stays

    def test_open_chat_with_nothing_typed_closes(self):
        found = window(self, "")
        found._open_chat()
        self.assertEqual(len(found.app.called("_open_from_notification")), 1)
        self.assertEqual(found.called("close"), [()])

    def test_sending_shows_on_the_button(self):
        found = window(self, "On my way")
        found._send()
        self.assertEqual(found.send_face.called("set_visible_child_name")[-1], ("sending",))
        self.assertEqual(found.composer.called("add_css_class")[-1], ("natter-sending",))
        self.assertFalse(found.problem.visible)  # nothing wrong while it's under way
        found.app.sends[0][2]("busy")
        self.assertEqual(found.send_face.called("set_visible_child_name")[-1], ("send",))
        self.assertEqual(found.composer.called("remove_css_class")[-1], ("natter-sending",))

    def test_the_card_offers_itself_to_hold_the_page(self):
        found = window(self, "On my way")
        found._send()
        (host,) = found.app.hosts
        self.assertIs(host(), found)

    def test_words_from_kde_sent_with_the_card_holding_the_page(self):
        found = window(self, "")
        found.show_inline("On my way")
        self.assertEqual((found.buffer.text, found.sending), ("On my way", True))
        self.assertFalse(found.entry.editable)  # on show, not to change
        self.assertFalse(found.send_button.sensitive)
        self.assertEqual(found.app.sends, [], "sent already, from KDE's field")
        self.assertTrue(found._on_close_request(None))  # closed while it goes: hidden, the send goes on
        found.visible = True
        with mock.patch.object(GLib, "timeout_add", return_value=9):
            found.inline_answered("sent")
        self.assertEqual((found.outcome, found.visible), ("sent", False))  # gone, as after Send
        failed = window(self, "")
        failed.show_inline("On my way")
        failed.inline_answered("busy")
        self.assertEqual((failed.buffer.text, failed.outcome, failed.sending), ("On my way", "busy", False))
        self.assertTrue(failed.entry.editable)
        self.assertTrue(failed.send_button.sensitive)  # to send again from here

    def test_words_kept_for_the_chat_stay_with_a_card_holding_the_page(self):
        found = window(self, "Half a thought")  # kept from before, in the field as it came up
        found.show_inline("On my way")
        self.assertEqual(found.buffer.text, "On my way")
        with mock.patch.object(GLib, "timeout_add", return_value=9):
            found.inline_answered("sent")
        # Gone as after any send, the words kept for the chat's next card, as before it came up.
        self.assertEqual((found.outcome, found.visible), ("sent", False))
        self.assertEqual(found.app.reply_drafts, {found.key: "Half a thought"})
        more = window(self, "Half a thought")
        more.show_inline("On my way")
        more.put_text("And another")  # from KDE's field meanwhile: it stays for that
        with mock.patch.object(GLib, "timeout_add", return_value=9):
            more.inline_answered("sent")
        self.assertEqual((more.buffer.text, more.visible), ("Half a thought\nAnd another", True))
        self.assertEqual(more.app.reply_drafts, {})
        failed = window(self, "Half a thought")
        failed.show_inline("On my way")
        failed.inline_answered("busy")
        self.assertEqual(failed.buffer.text, "On my way\nHalf a thought")

    def test_a_card_sent_stays_while_it_holds_the_page_for_another_reply(self):
        # Sent, while the next reply to the chat (from KDE's field) has the page lent to it: hidden,
        # the page would go out of sight under that one. Out of the way at once, gone once it's back.
        found = window(self, "On my way")
        found._send()
        found.holding = object()
        found.app.sends[0][2]("sent")
        self.assertEqual((found.outcome, found.visible), ("sent", True))
        self.assertNotIn(found.key, found.app.replies)  # Reply for the chat makes a new one
        found.holding = None
        with mock.patch.object(GLib, "timeout_add", return_value=9):
            found.loan_ended()
        self.assertEqual((found.visible, found._closing), (False, 9))
        found.loan_ended()  # again: nothing
        self.assertEqual(found.called("set_visible"), [(False,)])
        other = window(self, "On my way")
        other.loan_ended()  # a loan for its own send: nothing
        self.assertEqual((other.visible, other.called("set_visible")), (True, []))

    def test_the_card_stays_up_while_it_sends(self):
        # It holds WhatsApp's page while the page is out of sight (app.lend_page): only closing it,
        # or the lock, takes it away before the answer.
        found = window(self, "On my way")
        found._send()
        self.assertTrue(found.visible)
        self.assertEqual(found.called("set_visible"), [])

    def test_show_gives_the_focus_back_to_the_box(self):
        # A click on Show takes the focus, and its row goes: the box has it again, not the messages.
        found = window(self, "", notify_preview=False)
        show = Fake(is_ancestor=lambda ancestor: ancestor is found.hidden_row)
        found.get_focus = lambda: show
        found._reveal()
        self.assertTrue(found.revealed)
        self.assertEqual(found.entry.called("grab_focus"), [()])
        found.get_focus = lambda: found.entry  # Alt+H: the focus was never moved
        found._reveal()
        self.assertEqual(found.entry.called("grab_focus"), [()])


class OutcomeTest(unittest.TestCase):
    def answer(self, result, text="On my way"):
        found = window(self, text)
        found._send()
        found.app.sends[0][2](result)
        return found

    def test_the_table(self):
        expected = {  # result: (Open Chat or Open NAME, Send again with the same words, over)
            "locked": ("", True, False), "calling": ("", True, False), "busy": ("app", True, False),
            "draft": ("chat", True, False), "cant-send": ("chat", True, False), "mismatch": ("chat", True, False),
            "gone": ("chat", True, False),
            "not-ready": ("", True, False), "needs-link": ("app", True, False), "too-long": ("chat", True, False),
            "left-as-draft": ("chat", False, True), "unsure": ("chat", False, True), "unknown": ("chat", False, True),
        }
        self.assertEqual({result: (look.button, look.send, look.done) for result, look in OUTCOMES.items()}, expected)
        for look in OUTCOMES.values():
            self.assertFalse(look.line.endswith("."), look.line)  # no full stop: the link or a notice adds it
            self.assertEqual(look.line[0], look.line[0].upper())
            self.assertLessEqual(len(look.line), 60, look.line)  # a line under the field, at 360 px
            self.assertIn(look.button, ("", *reply.ACTIONS))

    def test_every_answer_the_page_gives(self):
        # Account's answers, less "sent" (the window closes) and "removed" (it goes with its account).
        self.assertEqual(set(OUTCOMES), REPLY_RESULTS - {"sent", "removed"})

    def test_anything_else_may_have_sent(self):
        self.assertIs(outcome("exception"), UNCLEAR)
        found = self.answer("exception")
        self.assertEqual(found.outcome, "unknown")
        self.assertFalse(found.send_button.sensitive)

    def test_nothing_reached_whatsapp_try_again(self):
        for result in ("locked", "calling", "busy", "draft", "cant-send", "mismatch", "gone", "not-ready"):
            found = self.answer(result)
            self.assertEqual(found.buffer.text, "On my way", result)
            self.assertTrue(found.send_button.sensitive, result)
            self.assertTrue(found.entry.editable, result)
            look = OUTCOMES[result]
            self.assertTrue(found.problem.visible, result)
            self.assertTrue(found.problem_text.label.startswith(GLib.markup_escape_text(look.line)), result)
            self.assertEqual(found.problem.called("add_css_class")[-1], ("error",), result)  # it didn't go
            self.assertIn((look.line, Gtk.AccessibleAnnouncementPriority.MEDIUM), found.called("announce"))

    def test_once_linked_again(self):
        # Send stays on, for after linking: until then the page is asked nothing, so nothing's lost.
        found = self.answer("needs-link")
        self.assertEqual(found.buffer.text, "On my way")
        self.assertTrue(found.send_button.sensitive)
        self.assertEqual(found.action, "app")
        link = "Open " + NAME
        self.assertIn(f'<a href="natter:app"><b>{link.replace(" ", chr(0xa0))}</b></a>', found.problem_text.label)

    def test_whatsapp_may_have_it(self):
        for result in ("left-as-draft", "unsure", "unknown"):
            found = window(self, "On my way")
            found.app.reply_drafts[found.key] = "On my way"
            found._send()
            found.app.sends[0][2](result)
            # Its words to read, never to send again; the box is empty for a new reply.
            self.assertEqual(found.yours.called("set_label")[-1], ("On my way",), result)
            self.assertEqual(found.buffer.text, "", result)
            self.assertNotIn(found.key, found.app.reply_drafts)
            self.assertFalse(found.send_button.sensitive, result)
            key(found, Gdk.KEY_Return)
            self.assertEqual(len(found.app.sends), 1, result)
            # Under the reply it's about, in the warning colour: it may have gone.
            self.assertEqual(found.problem.called("add_css_class")[-1], ("warning",), result)
            self.assertTrue(found.problem.visible, result)
            found.buffer.text = "Did that go?"  # a new reply is a new press
            found._update_send()
            self.assertTrue(found.send_button.sensitive, result)
            self.assertTrue(found.problem.visible, result)  # still said, until the next send

    def test_one_answer_per_send(self):
        for first in ("unknown", "not-ready"):
            found = self.answer(first)
            found.app.sends[0][2]("sent")  # a second answer for the same send changes nothing
            self.assertEqual(found.outcome, first)
            self.assertEqual(found.called("destroy"), [])

    def test_account_removed(self):
        found = self.answer("removed")
        self.assertEqual(found.called("destroy"), [()])
        self.assertNotIn(found.key, found.app.replies)

    def test_the_lines_link(self):
        found = self.answer("busy")
        found._action_clicked()
        self.assertEqual(found.app.called("open_account"), [("main",)])
        found = self.answer("draft")
        found._action_clicked()
        self.assertEqual(found.app.called("_open_from_notification"), [("main", "Mum", 7, "")])
        self.assertEqual(len(found.app.sends), 1)  # never a send
        found = self.answer("not-ready")  # nothing to do but wait: no link
        self.assertEqual(found.action, "")
        self.assertNotIn("<a ", found.problem_text.label)
        found._action_clicked()
        self.assertEqual((found.app.called("open_account"), found.app.called("_open_from_notification")), ([], []))

    def test_too_long_comes_first_then_the_answer_again(self):
        found = self.answer("busy")
        found.buffer.text = "👍" * (LIMIT // 2 + 1)
        found._update_send()
        self.assertEqual(found.action, "chat")
        self.assertTrue(found.problem_text.label.startswith(reply.TOO_LONG))
        found.buffer.text = "On my way"
        found._update_send()
        self.assertEqual(found.action, "app")
        self.assertTrue(found.problem_text.label.startswith(OUTCOMES["busy"].line))

    def test_the_line_is_markup_made_safe(self):
        found = window(self, "On my way")
        with mock.patch.dict(OUTCOMES, {"busy": reply.Outcome("<b>Fish & chips</b>", "app")}):
            found._send()
            found.app.sends[0][2]("busy")
        self.assertTrue(found.problem_text.label.startswith("&lt;b&gt;Fish &amp; chips&lt;/b&gt;. <a "))

    def test_a_new_send_takes_the_line_away(self):
        found = self.answer("not-ready")
        found._send()
        self.assertFalse(found.problem.visible)
        with mock.patch.object(GLib, "timeout_add", return_value=9):
            found.app.sends[1][2]("sent")
        self.assertFalse(found.problem.visible)

    def test_the_line_set_only_when_it_changes(self):
        found = self.answer("not-ready")
        before = len(found.problem_text.called("set_markup"))
        for _ in range(3):
            found._update_send()  # as at every key
        self.assertEqual(len(found.problem_text.called("set_markup")), before)


class LayoutTest(unittest.TestCase):
    """What the window does with room: the field grows with its words, the messages are kept
    whole as far as they may be (the window grows instead), the latest message stays in sight, and
    no caret sits in words that can't change."""

    def field(self, natural):
        found = window(self)
        found.entry.measure = lambda _orientation, _width: (natural, natural, -1, -1)
        found.entry.get_width = lambda: 300
        policies = []
        found.field = Fake(min_height=reply.FIELD_LOW, policy=(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL))
        found.field.get_min_content_height = lambda: found.field.min_height
        found.field.set_min_content_height = lambda height: setattr(found.field, "min_height", height)
        found.field.get_policy = lambda: found.field.policy
        found.field.set_policy = lambda h, v: (policies.append(v), setattr(found.field, "policy", (h, v)))
        ReplyWindow._fit_field(found)
        return found.field.min_height, found.field.policy[1], policies

    def test_the_box_grows_with_the_words(self):
        self.assertEqual(self.field(34)[:2], (reply.FIELD_LOW, Gtk.PolicyType.EXTERNAL))  # a line
        self.assertEqual(self.field(100)[:2], (100, Gtk.PolicyType.EXTERNAL))
        # Past about six lines it scrolls, and only then has a scroll bar (one makes it taller).
        self.assertEqual(self.field(400)[:2], (reply.FIELD_HIGH, Gtk.PolicyType.AUTOMATIC))
        self.assertEqual(self.field(34)[2], [])  # nothing set that's already so

    def messages(self, natural, width=336, least=40):
        found = window(self)
        adjustment = Gtk.Adjustment(value=0, lower=0, upper=300, page_size=120)
        found.messages = Fake(least=0, width=width)
        found.messages.get_vadjustment = lambda: adjustment
        found.messages.get_visible = lambda: True
        found.messages.get_width = lambda: found.messages.width
        found.messages.get_min_content_height = lambda: found.messages.least
        found.messages.set_min_content_height = lambda height: setattr(found.messages, "least", height)
        measured = []
        found.column = Fake()
        found.column.measure = lambda orientation, size: (
            measured.append((orientation, size)),
            (least, least, -1, -1) if orientation == Gtk.Orientation.HORIZONTAL else (natural, natural, -1, -1))[1]
        found._to_latest = types.MethodType(ReplyWindow._to_latest, found)
        return found, adjustment, measured

    def test_the_latest_message_in_sight(self):
        found, adjustment, _measured = self.messages(80)
        with mock.patch.object(GLib, "idle_add", return_value=5) as later:
            ReplyWindow._stick(found)
            ReplyWindow._stick(found)  # once per layout
        self.assertEqual(later.call_count, 1)
        self.assertEqual(adjustment.get_value(), 0)  # not during the layout: GTK would lose it
        later.call_args[0][0]()
        self.assertEqual(adjustment.get_value(), 180)

    def test_the_messages_kept_whole(self):
        # What's typed (or a problem's line) grows the window rather than cut a message off, as
        # far as MESSAGES_KEPT; past that they give way, and scroll with the latest in sight.
        for natural, least in ((80, 80), (reply.MESSAGES_KEPT, reply.MESSAGES_KEPT), (400, reply.MESSAGES_KEPT)):
            found, _adjustment, measured = self.messages(natural)
            found._to_latest()
            self.assertEqual(found.messages.least, least, natural)
            self.assertEqual(measured[-1], (Gtk.Orientation.VERTICAL, 336))  # at the width they have
        found, _adjustment, measured = self.messages(80, width=0, least=120)
        found._to_latest()  # not laid out yet: never narrower than they can be
        self.assertEqual(measured[-1], (Gtk.Orientation.VERTICAL, 120))

    def test_no_caret_while_sending(self):
        found = window(self, "On my way")
        found.get_focus = lambda: found.entry
        found._send()
        self.assertIn(("set_focus", None), found.calls)
        found.get_focus = lambda: None
        self.assertNotIn(("grab_focus",), found.entry.calls)
        found.app.sends[0][2]("not-ready")
        self.assertIn(("grab_focus",), found.entry.calls)  # back to the words, to send again

    def test_the_focus_back_after_send_was_clicked(self):
        # Send has the focus once it's clicked, and goes from sight while sending: the box has it after.
        for answer in ("busy", "not-ready", "calling"):
            found = window(self, "On my way")
            found.get_focus = lambda: found.send_button
            found._send()
            found.get_focus = lambda: None
            found.app.sends[0][2](answer)
            self.assertEqual(found.entry.called("grab_focus"), [()], answer)


class WordsTest(unittest.TestCase):
    def test_title_follows_privacy(self):
        self.assertEqual(window_title("Mum", True), "Reply to Mum")
        self.assertEqual(window_title("Mum", False), "Reply")
        self.assertEqual(window_title("  ", True), "Reply")
        self.assertEqual(window_title("Mum‮", True), "Reply to Mum")  # no direction tricks in a title

    def test_notices(self):
        self.assertEqual(notice("not-ready", "Mum", True),
                         ("Your reply wasn't sent", "To Mum. WhatsApp is still loading. Try again in a moment."))
        self.assertEqual(notice("unsure", "Mum", False),
                         ("Your reply may not have been sent", "Couldn't check whether it was sent."))
        self.assertEqual(notice("left-as-draft", "Mum", True),
                         ("Your reply wasn't sent", "To Mum. Left unsent in the chat's message box."))
        self.assertNotIn("Mum", repr(notice("locked", "Mum", False)))
        self.assertEqual(notice("locked", "Mum", False),
                         ("Your reply wasn't sent", f"{NAME} locked before your reply could be sent."))
        for result in OUTCOMES:  # the body never says the title again
            title, body = notice(result, "Mum", False)
            self.assertNotIn(title.lower(), body.lower(), result)

    def test_joined(self):
        self.assertEqual(joined("", "second"), "second")
        self.assertEqual(joined("first \n", "second"), "first\nsecond")


# ---- The app's side (app.py) -------------------------------------------------------------------


class Window(Fake):
    """A reply window as the app sees it."""

    def __init__(self, active=False, sending=False, outcome=None, words=False, chat=PERSON):
        super().__init__(ref="fedcba9876543210", active=active, sending=sending, outcome=outcome, words=words,
                         target=target(chat=chat), told=None, told_named=False, visible=True)

    def is_active(self):
        return self.active

    def get_visible(self):
        return self.visible

    def close_for_lock(self):
        self.calls.append(("close_for_lock",))
        return self.words and not self.sending


class AppTest(unittest.TestCase):
    def make(self, locked=False, **values):
        app = Fake(locked=locked, settings=settings(self, **values), replies={}, reply_drafts={}, held=[],
                   held_keys=[], held_tokens=[], accounts=[], window=None, _after_unlock=[])
        account = Fake(id="main", name="Personal", removed=False, active_chat="", sent=[])
        account.lends = []
        account.send_reply = lambda page_id, chat, text, done, lend=None: (
            account.sent.append((page_id, chat, text, done)), account.lends.append(lend))
        app.accounts.append(account)
        app.account = account
        app.account_by_id = lambda account_id: account if account_id == "main" else None
        app.notifier = Fake()

        def held(run, key="show", token=""):
            if not app.locked:
                return False
            app.held.append(run)
            app.held_keys.append(key)
            app.held_tokens.append(token)
            return True
        app._held_while_locked = held
        for name in ("on_reply", "_keep_reply", "_send_inline", "send_reply", "reply_failed", "show_reply",
                     "_wait_for_unlock"):
            setattr(app, name, types.MethodType(getattr(Natter, name), app))
        return app

    def test_reply_while_locked_waits_for_the_passcode(self):
        app = self.make(locked=True)
        app.on_reply(target(), "token", None, False)
        self.assertEqual(app.called("open_reply"), [])
        self.assertEqual(app.held_keys, [f"reply:main/{PERSON}"])  # waits with an Open or a file, not instead
        self.assertEqual(app.held_tokens, ["token"])  # the passcode comes forward with the click's token
        app.locked = False
        app.held.pop()()
        (opened,), = app.called("open_reply")
        self.assertEqual(opened.chat, PERSON)

    def test_text_while_locked_is_never_sent(self):
        app = self.make(locked=True)
        app.on_reply(target(), "", "On my way", False)
        self.assertEqual(app.account.sent, [])
        self.assertEqual(app.reply_drafts, {("main", PERSON): "On my way"})
        self.assertEqual(len(app.held), 1)  # the reply window, after the passcode

    def test_refused_text_waits_for_send(self):
        app = self.make()
        app.on_reply(target(), "", "again", True)
        self.assertEqual(app.account.sent, [])
        self.assertEqual(app.reply_drafts, {("main", PERSON): "again"})
        self.assertEqual(len(app.called("open_reply")), 1)
        app.on_reply(target(), "", "and again", True)
        self.assertEqual(app.reply_drafts, {("main", PERSON): "again\nand again"})

    def test_text_goes_into_an_open_window(self):
        app = self.make()
        open_window = app.replies[("main", PERSON)] = Window()
        app.on_reply(target(), "", "On my way", False)
        self.assertEqual(app.account.sent, [])
        self.assertEqual(open_window.called("put_text"), [("On my way",)])

    def test_text_from_kde_is_sent(self):
        app = self.make()
        app.on_reply(target(), "", "On my way", False)
        (page_id, chat, text, done), = app.account.sent
        self.assertEqual((page_id, chat, text), (7, PERSON, "On my way"))
        with redirect_stdout(io.StringIO()):
            done("sent")
        self.assertEqual(app.notifier.called("withdraw_page"), [("main", 7)])
        self.assertEqual(app.called("_reply_window"), [])

    def test_text_from_kde_that_wasnt_sent_opens_the_window(self):
        app = self.make()
        made = Window()
        app._reply_window = lambda t: made
        app.on_reply(target(), "", "On my way", False)
        with redirect_stdout(io.StringIO()):
            app.account.sent[0][3]("draft")
        self.assertEqual(made.called("inline_failed"), [("On my way", "draft")])
        self.assertEqual(made.called("present_again"), [()])

    def test_text_from_kde_failing_while_locked_names_nobody(self):
        app = self.make()
        made = Window()
        app._reply_window = lambda t: made
        app.on_reply(target(), "", "On my way", False)
        app.locked = True
        with redirect_stdout(io.StringIO()):
            app.account.sent[0][3]("unknown")
        self.assertEqual(made.called("present_again"), [])
        (key_, title, body, button), = app.called("tell")
        self.assertEqual((key_, title), ("reply-fedcba9876543210", "Your reply may not have been sent"))
        self.assertNotIn("Mum", body)
        self.assertEqual(button[:2], ("Show Reply", "app.show-reply"))

    def kde_host(self, app):
        """The page's lend() for the last send from KDE's field, run: the host it was offered."""
        (lend,) = app.account.lends[-1:]
        lend(False)
        (_account, host, hidden), = app.called("lend_page")[-1:]
        self.assertIs(hidden, False)  # as the page asked
        return host

    def test_text_from_kde_out_of_sight_brings_up_a_card_to_hold_the_page(self):
        app = self.make()
        made = Window(sending=False)
        made.visible = False
        made.set_visible = lambda visible: made.calls.append(("set_visible", visible))

        def make_window(found_target):
            app.replies[(found_target.account, found_target.chat)] = made
            return made
        app._reply_window = make_window
        app.on_reply(target(), "", "On my way", False)
        self.assertEqual(app.called("_reply_window"), [], "not before the page is asked")
        host = self.kde_host(app)
        self.assertIs(host(), made)
        self.assertEqual(made.called("show_inline"), [("On my way",)])
        self.assertEqual(made.called("set_visible"), [(True,)])  # shown, not presented: no focus asked for
        self.assertEqual((made.called("present"), made.called("present_again")), ([], []))
        made.sending = True  # as show_inline left it
        with redirect_stdout(io.StringIO()):
            app.account.sent[0][3]("sent")
        # Its answer is the card's, as for Send: it goes once sent, and says why when not.
        self.assertEqual(made.called("inline_answered"), [("sent",)])
        self.assertEqual(made.called("inline_failed"), [])

    def test_text_from_kde_with_a_card_up_already_lends_to_it(self):
        app = self.make()
        made = Window()
        app._reply_window = lambda t: made
        app.on_reply(target(), "", "On my way", False)
        app.replies[("main", PERSON)] = made  # Reply pressed before the page was asked
        self.assertIs(self.kde_host(app)(), made)
        self.assertEqual(made.called("show_inline"), [])
        with redirect_stdout(io.StringIO()):
            app.account.sent[0][3]("draft")
        self.assertEqual(made.called("inline_failed"), [("On my way", "draft")])  # as before

    def test_text_from_kde_with_a_failed_card_out_of_sight_shows_it_to_hold_the_page(self):
        # Its reply failed out of sight after these words were asked for: shown again (its notice
        # goes) rather than leaving the page out of sight, and it shows this answer as before.
        app = self.make()
        made = Window()
        made.visible = False
        made.set_visible = lambda visible: (made.calls.append(("set_visible", visible)),
                                            setattr(made, "visible", visible))
        app._reply_window = lambda t: made
        app.on_reply(target(), "", "On my way", False)
        app.replies[("main", PERSON)] = made
        self.assertIs(self.kde_host(app)(), made)
        self.assertEqual((made.called("untell"), made.called("set_visible")), ([()], [(True,)]))
        self.assertEqual(made.called("show_inline"), [])
        with redirect_stdout(io.StringIO()):
            app.account.sent[0][3]("draft")
        self.assertEqual(made.called("inline_failed"), [("On my way", "draft")])
        busy = Window(sending=True)  # closed while its own reply goes: left as it is
        busy.visible = False
        del app.replies[("main", PERSON)]
        app.on_reply(target(), "", "Again", False)
        self.assertEqual(len(app.account.sent), 2)
        app.replies[("main", PERSON)] = busy
        self.assertIs(self.kde_host(app)(), busy)
        self.assertEqual(busy.called("set_visible"), [])

    def test_text_from_kde_seen_or_unlent_goes_as_before(self):
        app = self.make()
        made = Window()
        app._reply_window = lambda t: made
        app.on_reply(target(), "", "On my way", False)  # the page on screen: no host asked for
        with redirect_stdout(io.StringIO()):
            app.account.sent[0][3]("busy")
        self.assertEqual(made.called("inline_answered"), [])
        self.assertEqual(made.called("inline_failed"), [("On my way", "busy")])

    def test_reply_turned_off_opens_the_chat(self):
        app = self.make(notify_reply=False)
        app.on_reply(target(), "token", None, False)
        self.assertEqual(app.called("_open_from_notification"), [("main", "Mum", 7, "token")])
        self.assertEqual(app.called("open_reply"), [])

    def test_a_reply_for_a_gone_notification_is_said_to_be_lost(self):
        app = self.make()
        app.on_reply(None, "", "hello", True)
        (_key, title, body), = app.called("tell")
        self.assertEqual(title, "Your reply wasn't sent")
        self.assertNotIn("hello", body)

    def test_never_started_while_locked(self):
        app = self.make(locked=True)
        answers = []
        with redirect_stdout(io.StringIO()):
            app.send_reply(target(), "On my way", answers.append)
        self.assertEqual((answers, app.account.sent), (["locked"], []))

    def test_checked_before_the_page(self):
        app = self.make()
        answers = []
        with redirect_stdout(io.StringIO()):
            app.send_reply(target(account="gone"), "hi", answers.append)
            app.send_reply(target(), "👍" * (LIMIT // 2 + 1), answers.append)
        self.assertEqual((answers, app.account.sent), (["removed", "too-long"], []))

    def test_answers(self):
        app = self.make()
        answers, out = [], io.StringIO()
        with redirect_stdout(out):
            app.send_reply(target(), "On my way", answers.append)
            app.account.sent[0][3]("sent")
            app.send_reply(target(), "On my way", answers.append)
            app.account.sent[1][3](None)  # lost after it started
        self.assertEqual(answers, ["sent", "unknown"])
        self.assertEqual(out.getvalue(), "")  # the page's answers are Account's to log

    def test_kde_sending_keeps_the_words_kept_for_the_chat(self):
        # Words a reply window kept when it closed aren't what KDE's field sent: they stay.
        app = self.make()
        app.reply_drafts[("main", PERSON)] = "Half a thought"
        app.on_reply(target(), "", "ok", False)
        (_page_id, _chat, text, done), = app.account.sent
        self.assertEqual(text, "ok")
        done("sent")
        self.assertEqual(app.reply_drafts, {("main", PERSON): "Half a thought"})

    def test_only_the_notification_it_answers_goes(self):
        # A newer message moves a reply window's target on while its reply is out (add_message).
        app = self.make()
        moving = target()
        app.send_reply(moving, "On my way", lambda result: None)
        (page_id, _chat, _text, done), = app.account.sent
        moving.page_id = 9
        done("sent")
        self.assertEqual((page_id, app.notifier.called("withdraw_page")), (7, [("main", 7)]))

    def test_the_log_says_only_the_answer(self):
        app = self.make(locked=True)
        out = io.StringIO()
        with redirect_stdout(out):
            app.send_reply(target(), "On my way", lambda result: None)
        self.assertEqual(out.getvalue(), "natter: reply: locked\n")

    def test_show_reply(self):
        app = self.make(locked=True)
        shown = app.replies[("main", PERSON)] = Window()
        app.show_reply("0000000000000000")
        app.show_reply(shown.ref)
        self.assertEqual(shown.called("present_again"), [])
        app.locked = False
        app.held.pop()()
        self.assertEqual(shown.called("present_again"), [()])
        self.assertEqual(shown.called("untell"), [()])  # its notice goes

    def notify(self, app, window_open=None, locked=False, on_screen=False):
        app.locked = locked
        if window_open is not None:
            app.replies[("main", PERSON)] = window_open
        app.account.active_chat = "Mum" if on_screen else ""
        app.window = SimpleNamespace(is_active=lambda: on_screen, account=app.account)
        Natter._on_notify_message(app, app.account, 9, "Mum", "Still there?", PERSON, "")
        return app.notifier.called("show")

    def test_a_reply_window_in_use_is_the_chat_on_screen(self):
        app = self.make()
        in_use = Window(active=True)
        self.assertEqual(self.notify(app, in_use), [])
        self.assertEqual(in_use.called("add_message"), [(9, "Mum", "Still there?")])

    def test_a_reply_window_about_to_close_still_notifies(self):
        # Sending, or showing it sent, it closes by itself: what came meanwhile keeps its bubble.
        for closing in (Window(active=True, sending=True), Window(active=True, outcome="sent")):
            self.assertEqual(len(self.notify(self.make(), closing)), 1)
            self.assertEqual(closing.called("add_message"), [(9, "Mum", "Still there?")])

    def test_a_reply_window_in_the_background_follows_and_the_bubble_shows(self):
        app = self.make()
        behind = Window(active=False)
        self.assertEqual(len(self.notify(app, behind)), 1)
        self.assertEqual(behind.called("add_message"), [(9, "Mum", "Still there?")])

    def test_locked_the_chat_on_screen_still_notifies(self):
        self.assertEqual(self.notify(self.make(), on_screen=True), [])
        self.assertEqual(len(self.notify(self.make(), locked=True, on_screen=True)), 1)

    def test_lock_closes_reply_windows(self):
        app = self.make()
        app.lock_enabled = True
        app.window = Fake()
        first, second = Window(), Window(sending=True)
        app.replies = {("main", "a"): first, ("main", "b"): second}
        Natter.lock(app)
        self.assertEqual((first.called("close_for_lock"), second.called("close_for_lock")), ([()], [()]))
        self.assertEqual(app.notifier.called("conceal"), [()])

    def test_a_notice_names_the_chat_only_where_notifications_may(self):
        app = self.make()
        failed = Window()
        app.reply_failed(failed, "not-ready")
        app.settings["notify_sender"] = False
        app.reply_failed(failed, "not-ready")
        (key_, title, named, button), (_key, _title, unnamed, _button) = app.called("tell")
        self.assertEqual((key_, title), ("reply-fedcba9876543210", "Your reply wasn't sent"))
        self.assertEqual(named, "To Mum. WhatsApp is still loading. Try again in a moment.")
        self.assertNotIn("Mum", unnamed)
        self.assertEqual(button[:2], ("Show Reply", "app.show-reply"))
        self.assertEqual(failed.told, "not-ready")

    def test_a_click_on_the_notice_shows_the_reply(self):
        app = self.make()
        told = []
        app.tell = lambda *args, **kwargs: told.append(kwargs)
        app.reply_failed(Window(), "busy")
        action, value = told[0]["default"]
        self.assertEqual((action, value.get_string()), ("app.show-reply", "fedcba9876543210"))
        # Natter.tell itself: that as the click, not the window.
        with mock.patch("natter.app.Gio.Notification") as made, mock.patch("natter.app.app_icon"):
            Natter.tell(Fake(), "reply-x", "Your reply wasn't sent", default=(action, value))
        made.new.return_value.set_default_action_and_target.assert_called_once_with(action, value)
        made.new.return_value.set_default_action.assert_not_called()

    def test_the_lock_tells_again_naming_nobody(self):
        app = self.make()
        app.lock_enabled = True
        app.window = Fake()
        failed, quiet = Window(), Window()
        app.replies = {("main", "a"): failed, ("main", "b"): quiet}
        app.reply_failed(failed, "unknown")
        self.assertIn("Mum", app.called("tell")[0][2])
        Natter.lock(app)
        (key_, title, body, _button) = app.called("tell")[1]
        self.assertEqual((key_, title), ("reply-fedcba9876543210", "Your reply may not have been sent"))
        self.assertNotIn("Mum", body)
        self.assertEqual(len(app.called("tell")), 2)  # nothing for a window that had no notice
        # Once: it names nobody now, and the next lock has nothing to take away.
        app.locked = False
        Natter.lock(app)
        self.assertEqual(len(app.called("tell")), 2)

    def test_a_notice_naming_nobody_isnt_told_again_at_the_lock(self):
        # Told again, it would come up again on screen, after being dismissed perhaps.
        app = self.make(notify_sender=False)
        app.lock_enabled = True
        app.window = Fake()
        failed = app.replies[("main", "a")] = Window()
        app.reply_failed(failed, "not-ready")
        Natter.lock(app)
        app.locked = False
        Natter.lock(app)
        self.assertEqual(len(app.called("tell")), 1)

    def test_words_the_lock_closed_come_back_after_the_unlock(self):
        app = self.make()
        app.lock_enabled = True
        app.window = Fake()
        typing, empty, sending = Window(words=True), Window(chat=GROUP), Window(words=True, sending=True, chat="c")
        app.replies = {("main", PERSON): typing, ("main", GROUP): empty, ("main", "c"): sending}
        Natter.lock(app)
        self.assertEqual([key_ for key_, _run in app._after_unlock], [f"reply:main/{PERSON}"])
        self.assertEqual(app.called("present_window"), [])  # the lock doesn't bring the window forward
        app._after_unlock[0][1]()
        (opened,), = app.called("open_reply")
        self.assertIs(opened, typing.target)
        # Reply pressed for that chat meanwhile takes its place, and both don't open.
        app._after_unlock, app.locked = [], False
        Natter.lock(app)
        app._wait_for_unlock(f"reply:main/{PERSON}", lambda: None)
        self.assertEqual(len(app._after_unlock), 1)

    def test_turning_reply_off(self):
        app = self.make()
        app.window = None
        Natter._on_setting(app, "notify_reply", False)
        self.assertEqual(app.notifier.called("refresh"), [()])

    def test_removing_an_account_takes_its_replies(self):
        app = self.make()
        work = Fake(id="work", app_handlers=[])
        app.accounts.append(work)
        app._memory_told, app._after_call, app._call_held, app.window = set(), {}, {}, Fake()
        mine, theirs = Window(), Window()
        app.replies = {("main", "a"): mine, ("work", "b"): theirs}
        app.reply_drafts = {("main", "c"): "kept", ("work", "d"): "dropped"}
        with mock.patch("natter.app.limit_own_scope"):
            Natter.remove_account(app, work)
        self.assertEqual((mine.called("forget"), theirs.called("forget")), ([], [()]))
        self.assertEqual(app.reply_drafts, {("main", "c"): "kept"})

    def test_privacy_settings_reach_reply_windows(self):
        app = self.make()
        open_window = app.replies[("main", PERSON)] = Window()
        app.window = None
        app.lookup_action = lambda name: Fake()
        for key_ in ("privacy_blur", "blur_when_away", "notify_sender", "notify_preview"):
            Natter._on_setting(app, key_, True)
        self.assertEqual(len(open_window.called("apply_privacy")), 4)


# ---- Starting again (app.restart) ---------------------------------------------------------------


class RestartTest(unittest.TestCase):
    def make(self, **values):
        toasts = []
        app = Fake(**{"media_lost": True, "restart_args": None, "calling": False, "locked": False, "replies": {},
                      "reply_drafts": {}, "accounts": [], "dev_url": None, "toasts": toasts, "_restart_wait": 0,
                      "_restart_alert": None, "held": [],
                      "window": SimpleNamespace(get_visible=lambda: True,
                                                toast=lambda *args, **kw: toasts.append(args)), **values})
        for name in ("restart", "_restart_now", "_restart_after_reply"):
            setattr(app, name, types.MethodType(getattr(Natter, name), app))
        app._held_while_locked = lambda run, key="show": app.locked and (app.held.append((key, run)) or True)
        return app

    def test_only_once_it_would_help(self):
        app = self.make(media_lost=False)
        Natter.restart(app)
        self.assertEqual(app.called("quit_natter"), [])

    def test_not_during_a_call(self):
        app = self.make(calling=True)
        Natter.restart(app)
        self.assertEqual(app.called("quit_natter"), [])
        self.assertEqual(app.toasts, [(f"Restart {NAME} once the call ends",)])

    def test_after_a_reply_being_sent(self):
        sending = SimpleNamespace(replying=1)
        app = self.make(accounts=[sending])
        with mock.patch.object(GLib, "timeout_add", return_value=3) as later:
            Natter.restart(app)
            Natter.restart(app)  # pressed again meanwhile: still the one wait
        self.assertEqual(app.called("quit_natter"), [])
        self.assertEqual(later.call_count, 1)
        self.assertEqual(later.call_args[0][0], 500)
        sending.replying = 0
        with redirect_stdout(io.StringIO()):
            self.assertFalse(later.call_args[0][1]())  # answered: now
        self.assertEqual(app.called("quit_natter"), [()])

    def test_asks_before_losing_a_reply(self):
        app = self.make(accounts=[SimpleNamespace(replying=0)])
        app.reply_drafts = {("main", PERSON): "Half a thought"}
        app.present_window = lambda: False  # no window to ask in: nothing happens
        Natter.restart(app)
        self.assertEqual(app.called("quit_natter"), [])

    def test_asks_past_the_passcode(self):
        app = self.make(locked=True, accounts=[SimpleNamespace(replying=0)])
        app.reply_drafts = {("main", PERSON): "Half a thought"}
        app.present_window = lambda: self.fail("asked over the lock")
        Natter.restart(app)
        self.assertEqual([key_ for key_, _run in app.held], ["restart"])
        self.assertEqual(app.called("quit_natter"), [])

    def test_asks_once(self):
        app = self.make(accounts=[SimpleNamespace(replying=0)], _restart_alert=object())
        app.reply_drafts = {("main", PERSON): "Half a thought"}
        app.present_window = lambda: self.fail("asked twice")
        Natter.restart(app)

    def test_back_as_it_was(self):
        app = self.make(accounts=[SimpleNamespace(replying=0)])
        with redirect_stdout(io.StringIO()):
            Natter.restart(app)
        self.assertEqual((app.restart_args, app.called("quit_natter")), ([], [()]))
        hidden = self.make(accounts=[], dev_url="file:///tmp/lab.html")
        hidden.window = SimpleNamespace(get_visible=lambda: False)
        with redirect_stdout(io.StringIO()):
            Natter.restart(hidden)
        self.assertEqual(hidden.restart_args, ["--background", "--url", "file:///tmp/lab.html"])
        Natter.restart(hidden)  # once
        self.assertEqual(len(hidden.called("quit_natter")), 1)

    def test_its_notice_goes_with_it(self):
        # The new Natter could neither take it away nor act on its Restart.
        withdrawn, quit_ = [], []
        told = SimpleNamespace(told="busy", untell=lambda: withdrawn.append("reply-told"))
        quiet = SimpleNamespace(told=None, untell=lambda: self.fail("nothing to take away"))
        app = SimpleNamespace(accounts=[], tray=None, window=None, memory_guard=None, ada_style=None,
                              desktop_media=None, calls=None, notifier=Fake(), reclaimer=Fake(), call_sound=Fake(),
                              audio_follower=Fake(), voice_pause=Fake(when_idle=lambda then: then()), withdraw_notification=withdrawn.append,
                              quit=lambda: quit_.append(1), replies={("main", PERSON): told, ("main", GROUP): quiet})
        Natter.quit_natter(app)
        # A failed reply's notice too: its words end with this Natter, and Show Reply would find nothing.
        self.assertEqual((withdrawn, quit_), (["media-lost", "reply-told"], [1]))

    def test_the_banner(self):
        restart = (f"Restart {NAME} to use the microphone and camera", "Restart", "app.restart", "")
        self.assertEqual(banner_for(0, True, 1000.0, restart=True), restart)
        self.assertEqual(banner_for(-1, True, 1000.0, restart=True)[0], "Notifications paused")
        self.assertEqual(banner_for(0, True, 1000.0)[2], "app.preferences-page")

    def test_the_command(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "natter"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
            self.assertEqual(restart_command(["--background"], str(script)), [str(script), "--background"])
        self.assertEqual(restart_command([], "/src/natter/natter/__main__.py", "/usr/bin/python3"),
                         ["/usr/bin/python3", "-m", "natter"])

    def test_started_by_a_process_of_its_own(self):
        launcher = mock.Mock()
        given = []
        with mock.patch("natter.app.Gio.SubprocessLauncher.new", return_value=launcher), \
                mock.patch("natter.app.give_system_bus", side_effect=given.append), \
                mock.patch("natter.app.own_children", return_value=[11, 12]):
            self.assertTrue(start_again(["--background"]))
        (command,), _options = launcher.spawnv.call_args
        self.assertEqual(command[:4], [sys.executable, "-I", "-c", RESTARTER])
        self.assertEqual(command[5], f"{os.getpid()},11,12")
        self.assertEqual(command[-1], "--background")
        self.assertEqual(given, [launcher])  # the desktop's environment, not the one Natter made itself
        if command[7:9] == ["-m", "natter"]:  # from a checkout: started where the package is
            launcher.set_cwd.assert_called_once_with(str(Path(reply.__file__).resolve().parent.parent))

    def test_couldnt_start_again(self):
        launcher = mock.Mock()
        launcher.spawnv.side_effect = GLib.Error("no Python")
        with mock.patch("natter.app.Gio.SubprocessLauncher.new", return_value=launcher), \
                mock.patch("natter.app.give_system_bus"), redirect_stderr(io.StringIO()) as said:
            self.assertFalse(start_again([]))
        self.assertIn("start", said.getvalue())

    def test_the_restarter_waits_then_becomes_natter(self):
        # A stand-in for the ending Natter, and `echo` for the new one: it starts only once that's gone.
        ending = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
        started = time.monotonic()
        out = subprocess.run([sys.executable, "-I", "-c", RESTARTER, "5", str(ending.pid), "/bin/echo", "started"],
                             capture_output=True, text=True, timeout=10,
                             env={"PATH": "/usr/bin:/bin", "GIO_LAUNCHED_DESKTOP_FILE_PID": "1"})
        ending.wait()
        self.assertEqual(out.stdout, "started\n")
        self.assertGreater(time.monotonic() - started, 0.3)

    def test_no_second_natter(self):
        # The old one still there when the wait is over: nothing starts.
        ending = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        self.addCleanup(ending.wait)
        self.addCleanup(ending.kill)
        out = subprocess.run([sys.executable, "-I", "-c", RESTARTER, "0.3", str(ending.pid), "/bin/echo", "started"],
                             capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((out.returncode, out.stdout), (1, ""))
        self.assertIn("still running", out.stderr)

    def test_own_children(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for task, children in (("40", "41 42"), ("43", "44")):
                (proc / "40" / "task" / task).mkdir(parents=True)
                (proc / "40" / "task" / task / "children").write_text(children)
            self.assertEqual(sorted(own_children(40, proc)), [41, 42, 44])
            self.assertEqual(own_children(99, proc), [])


class SettingsTest(unittest.TestCase):
    def test_reply_is_on_by_default(self):
        self.assertIs(DEFAULTS["notify_reply"], True)


if __name__ == "__main__":
    unittest.main()
