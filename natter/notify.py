"""Desktop notifications, one per chat.

Natter talks to org.freedesktop.Notifications directly rather than through GNotification so it
can replace a chat's notification in place (one bubble per chat, with a count), attach the
sender's picture, and pass the activation token that lets the window come forward on Wayland.
Inside a sandbox, or with no notification server on the bus, it uses GNotification, which goes
through the portal or GLib's own backends.

A message's notification can also offer Reply (reply.py does the replying). On KDE Plasma the
reply is typed in the notification itself (its inline-reply); everywhere else Reply opens a small
reply window. What a desktop may keep of a notification (its id, its action targets) names no
chat: each notification has a random reference instead.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import GdkPixbuf, Gio, GLib, Soup

from . import APP_ID, DEFAULT_APP_ID, NAME
from .launcher import INVISIBLE

# The app icon as a file path: the spec allows one, and it works where icon names in
# ~/.local/share/icons aren't looked up (some shells). It's named by the default id, as in
# app.py: a separate instance (NATTER_APP_ID) has no icon of its own.
_SVG = Path(__file__).resolve().parent.parent / "data" / "icons" / "hicolor" / "scalable" / "apps" / f"{DEFAULT_APP_ID}.svg"
APP_ICON = str(_SVG) if _SVG.is_file() else DEFAULT_APP_ID

try:
    from .system import in_sandbox
except ImportError:
    def in_sandbox() -> bool:
        return False

BUS = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
AVATAR = 96
AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_SIDE = 512  # larger pictures are scaled down as they're decoded
AVATARS = 64  # chats whose picture Natter keeps
SUMMARY_CHARS = 200
BODY_CHARS = 500
MUTE_SECONDS = 3600
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
DATA_URL = re.compile(r"data:image/(?:jpeg|png|webp);base64,")
# A WhatsApp chat's id, which WhatsApp's page uses as a message notification's tag (in WebKit; it
# uses the message's id only in Chrome on a Mac): a person (c.us, lid) or a group (g.us). Calls,
# waiting rooms, status, reactions and channels have tags of other shapes, so they get no Reply,
# and if WhatsApp ever changes its tags, Reply goes rather than answering the wrong thing.
CHAT_ID = re.compile(r"\d{5,24}(-\d{5,24})?@(c\.us|g\.us|lid)")
MESSAGES = 3  # a chat's latest messages the reply window shows
# Seconds a notification is still answerable after it was replaced or closed: some servers say
# it closed before they send what was typed in it, and the person may have been typing in the
# one on screen when a newer message replaced it.
REPLY_GRACE = 30
# The server's ids for Natter's notifications, the latest this many, to tell a reply typed in one
# Natter has forgotten from one typed in another app's: servers that don't address their signals
# send every app's to every app.
GIVEN_IDS = 512


def clip(text: str, limit: int) -> str:
    """`text` without control characters, cut to `limit` characters with an ellipsis."""
    text = CONTROL.sub("", text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape(text: str) -> str:
    """Body text made safe for servers that read markup in it."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def texts(title: str, body: str, count: int, account: Optional[str], sender: bool, preview: bool,
          markup: bool) -> tuple[str, str]:
    """A chat's notification summary and body. `markup`: the server reads markup in the body.

    The summary drops invisible characters, so a chat's name can't hide or reorder the count and
    account after it; the body keeps them, as right-to-left messages need their direction marks.
    """
    if sender:
        summary = f"{title} ({count})" if count > 1 else title
    else:
        summary = f"{count} new messages" if count > 1 else "New message"
    if account:
        summary = f"{summary} · {account}"
    text = clip(body, BODY_CHARS) if preview else ""
    return clip(" ".join(INVISIBLE.sub("", summary).split()), SUMMARY_CHARS), escape(text) if markup else text


def replyable(tag: str, page_id: int) -> bool:
    """Whether a notification can be answered: one of WhatsApp's own (not WebKit's fallback, whose
    ids are negative) for a message in a chat."""
    return page_id > 0 and bool(CHAT_ID.fullmatch(tag or ""))


def avatar_key(url: str) -> str:
    """A short digest of a picture's address, to tell one picture from another without keeping
    the address: a data: URL is the whole picture. "" for no address."""
    return hashlib.sha256(url.encode("utf-8", "surrogateescape")).hexdigest()[:32] if url else ""


def picture_allowed(url: str) -> bool:
    """Whether Natter fetches this picture: from WhatsApp's servers over https, or inline."""
    if DATA_URL.match(url):
        return True
    try:
        uri = GLib.Uri.parse(url, GLib.UriFlags.NONE)
    except GLib.Error:
        return False
    return uri.get_scheme() == "https" and (uri.get_host() or "").lower().endswith(".whatsapp.net")


def image_type(raw: bytes) -> Optional[str]:
    """JPEG, PNG or WebP, from the bytes themselves; None for anything else."""
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _fit(loader: GdkPixbuf.PixbufLoader, width: int, height: int) -> None:
    if max(width, height) > AVATAR_SIDE:
        scale = AVATAR_SIDE / max(width, height)
        loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))


def decode_image(raw: bytes) -> Optional[GdkPixbuf.Pixbuf]:
    """The picture, if it's a JPEG, PNG or WebP that decodes. No other loader sees the bytes."""
    kind = image_type(raw)
    if kind is None:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_mime_type(kind)
    except GLib.Error:
        return None
    loader.connect("size-prepared", _fit)
    ok = True
    try:
        loader.write(raw)
    except GLib.Error:
        ok = False
    try:
        loader.close()
    except GLib.Error:
        ok = False
    return loader.get_pixbuf() if ok else None


def circle(pixbuf: GdkPixbuf.Pixbuf, size: int = AVATAR) -> GdkPixbuf.Pixbuf:
    """The picture cropped square, scaled, and cut to a circle with a soft edge."""
    side = min(pixbuf.get_width(), pixbuf.get_height())
    square = pixbuf.new_subpixbuf((pixbuf.get_width() - side) // 2, (pixbuf.get_height() - side) // 2, side, side)
    scaled = square.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR).add_alpha(False, 0, 0, 0)
    stride, pixels = scaled.get_rowstride(), bytearray(scaled.get_pixels())
    radius = size / 2
    for y in range(size):
        for x in range(size):
            edge = radius - math.hypot(x + 0.5 - radius, y + 0.5 - radius)
            if edge < 1:
                at = y * stride + x * 4 + 3
                pixels[at] = int(pixels[at] * max(0.0, edge))
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(pixels)), GdkPixbuf.Colorspace.RGB, True, 8, size, size, stride
    )


def image_data(pixbuf: GdkPixbuf.Pixbuf) -> GLib.Variant:
    """The image-data hint. Its pixels go in as one block: made from Python bytes, PyGObject built
    them a byte at a time, 130-560 ms per notification on Natter's main thread."""
    return GLib.Variant.new_tuple(
        GLib.Variant("i", pixbuf.get_width()),
        GLib.Variant("i", pixbuf.get_height()),
        GLib.Variant("i", pixbuf.get_rowstride()),
        GLib.Variant("b", pixbuf.get_has_alpha()),
        GLib.Variant("i", pixbuf.get_bits_per_sample()),
        GLib.Variant("i", pixbuf.get_n_channels()),
        GLib.Variant.new_from_bytes(GLib.VariantType("ay"), pixbuf.read_pixel_bytes(), True),
    )


@dataclass
class Shown:
    """A chat's notification: on screen, or on its way there."""

    account: str
    chat: str
    title: str
    page_id: int  # the id WhatsApp's page gave it, for routing the click back
    body: str = ""
    # The sender's picture, decoded and circled, as the avatars cache has it; a digest of the
    # address WhatsApp gave for it (avatar_key), to tell a new picture from the cached one; and
    # that address itself only until the picture is decoded (a data: URL is the whole picture,
    # base64: hundreds of kilobytes a bubble, which stayed in every Shown and the cache before).
    icon: Optional[GdkPixbuf.Pixbuf] = None
    icon_key: str = ""
    icon_url: str = ""
    account_name: Optional[str] = None
    count: int = 1
    server_id: int = 0  # the notification server's id for it
    posted: bool = False  # shown through GNotification instead
    sending: bool = False  # its picture or Notify call is under way
    again: bool = False  # newer content arrived meanwhile
    closed: bool = False  # withdrawn
    replyable: bool = False  # a message in a chat (replyable()), so it offers Reply
    messages: list[str] = field(default_factory=list)  # the latest MESSAGES bodies, for the reply window
    # What the desktop is given in its place, in GNotification's id and action targets: the desktop
    # may keep those on disk (GNOME Shell does, the id too), and the chat's id is a phone number.
    ref: str = field(default_factory=lambda: secrets.token_hex(8))
    posts: int = 0  # Notify calls made for it so far
    # KDE's reply field, one per post that had one: the post's number, and when it was replaced
    # or closed (0 while it's on screen). Each takes one reply (_take_field).
    fields: dict[int, float] = field(default_factory=dict)

    @property
    def gid(self) -> str:
        """Its GNotification id: the same for every post of this bubble, so each replaces the last."""
        return f"chat-{self.ref}"


@dataclass
class ReplyTarget:
    """What the reply window needs of a chat's notification, copied from it: the notification
    may go (read elsewhere, withdrawn) while the reply is being written."""

    account: str
    chat: str  # WhatsApp's id for the chat (the notification's tag)
    title: str  # the chat's name
    page_id: int  # the page's notification for the latest message, which opens the chat
    messages: list[str] = field(default_factory=list)
    count: int = 1
    picture: Optional[GdkPixbuf.Pixbuf] = None  # the sender's circled picture, if Natter has it
    account_name: Optional[str] = None  # with several accounts


class Notifier:
    """Shows WhatsApp's notifications as Natter's own and reports clicks.

    `on_open(account_id, chat, page_id, token)` runs when one is clicked, and
    `on_reply(target, token, text, refused)` when Reply is: `text` is None for a Reply button, or
    what was typed in KDE's reply field. `refused` text came for a field that had answered
    already, or had gone: it's never sent as it is, but kept for the reply window.
    """

    def __init__(self, app, on_open: Callable[[str, str, int, str], None],
                 on_reply: Optional[Callable[[Optional[ReplyTarget], str, Optional[str], bool], None]] = None) -> None:
        self.app = app
        self.settings = app.settings
        self.on_open = on_open
        self.on_reply = on_reply
        self.bus = app.get_dbus_connection()
        self.shown: dict[tuple[str, str], Shown] = {}
        self.by_server: dict[int, Shown] = {}
        self.by_ref: dict[str, Shown] = {}
        self.given: OrderedDict[int, None] = OrderedDict()  # GIVEN_IDS
        self.tokens: dict[int, str] = {}
        self.muted: dict[tuple[str, str], float] = {}
        # Each chat's picture, decoded and circled, with the digest of the address it came from
        # (avatar_key): never the address itself, which may be the picture in base64.
        self.avatars: OrderedDict[tuple[str, str], tuple[str, GdkPixbuf.Pixbuf]] = OrderedDict()
        self.http = Soup.Session(timeout=8, max_conns=4)
        self.server: Optional[str] = None  # the notification server's unique name, when Natter talks to it
        self.capabilities: Optional[set[str]] = None  # None until the server says
        self._warned = False
        # Anything on the session bus can call these (GApplication exports its actions): they take
        # a reference to a notification Natter shows, never a chat, and none of them carries text.
        for name, target, run in [("notification-open", "(sssi)", self._open_action),
                                  ("notification-mute", "(ss)", self._mute_action),
                                  ("notification-reply", "(ss)", self._reply_action)]:
            action = Gio.SimpleAction.new(name, GLib.VariantType(target))
            action.connect("activate", lambda _a, value, run=run: run(*value.unpack()))
            app.add_action(action)
        if in_sandbox():
            return
        # NotificationReplied is KDE's: what was typed in a notification's reply field.
        for signal in ("ActionInvoked", "NotificationClosed", "ActivationToken", "NotificationReplied"):
            self.bus.signal_subscribe(BUS, BUS, signal, PATH, None, Gio.DBusSignalFlags.NONE, self._on_signal)
        try:
            self.server = self.bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                GLib.Variant("(s)", (BUS,)), GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 500, None).unpack()[0]
            self.capabilities = set(self.bus.call_sync(
                BUS, PATH, BUS, "GetCapabilities", None, GLib.VariantType("(as)"),
                Gio.DBusCallFlags.NO_AUTO_START, 500, None).unpack()[0])
        except GLib.Error:
            pass
        # The server can restart, or start after Natter: ask it again whenever it changes.
        Gio.bus_watch_name_on_connection(self.bus, BUS, Gio.BusNameWatcherFlags.NONE,
                                         self._server_appeared, self._server_vanished)

    def _server_appeared(self, bus, _name, owner: str) -> None:
        if owner != self.server:
            self._forget_server()
            self.server = owner
        if self.capabilities is None:
            bus.call(BUS, PATH, BUS, "GetCapabilities", None, GLib.VariantType("(as)"),
                     Gio.DBusCallFlags.NO_AUTO_START, 2000, None, self._got_capabilities, owner)

    def _server_vanished(self, _bus, _name) -> None:
        self._forget_server()
        self.server = None

    def _forget_server(self) -> None:
        """A server went away, and its bubbles with it."""
        self.capabilities = None
        self._warned = False
        for shown in self.shown.values():
            shown.server_id = 0
        self.by_server.clear()
        self.given.clear()  # a new server counts its ids from the start again
        self.tokens.clear()

    def _got_capabilities(self, bus, result, owner: str) -> None:
        try:
            capabilities = set(bus.call_finish(result).unpack()[0])
        except GLib.Error:
            return
        if owner == self.server:
            self.capabilities = capabilities

    # ---- Showing ---------------------------------------------------------------------------

    def show(self, account_id: str, account_name: Optional[str], page_id: int, title: str, body: str,
             chat: str, icon: str) -> None:
        """WhatsApp asked for a notification. `chat` identifies the conversation (its tag or title)."""
        key = (account_id, chat or title)
        if not self.settings["notifications"] or self.settings.notifications_paused():
            return
        now = time.time()
        if self.muted.get(key, 0) > now:
            return
        if len(self.muted) > 32:  # mutes that ran out go
            self.muted = {k: until for k, until in self.muted.items() if until > now}
        shown = self.shown.get(key)
        if shown:
            shown.count += 1
            shown.page_id, shown.title, shown.body = page_id, title, body
            shown.account_name = account_name
        else:
            shown = self.shown[key] = Shown(account_id, key[1], title, page_id, body, account_name=account_name)
            self.by_ref[shown.ref] = shown
        self._take_icon(shown, icon)
        if body:
            shown.messages = (shown.messages + [clip(body, BODY_CHARS)])[-MESSAGES:]
        shown.replyable = replyable(key[1], page_id)  # Reply opens the chat by the latest message's notification
        self._deliver(shown)

    def conceal(self) -> None:
        """Show the notifications already up again, now hiding who wrote and what (Natter locked).
        Their Reply stays, without KDE's reply field: it asks for the passcode first."""
        self.refresh()

    def refresh(self) -> None:
        """Show the notifications already up again, as the settings and the lock now say."""
        for shown in list(self.shown.values()):
            if not shown.closed:
                self._deliver(shown)

    def target(self, shown: Shown) -> ReplyTarget:
        """A copy of what the reply window needs of `shown`."""
        cached = self.avatars.get((shown.account, shown.chat))
        return ReplyTarget(shown.account, shown.chat, shown.title, shown.page_id, list(shown.messages), shown.count,
                           shown.icon or (cached[1] if cached else None), shown.account_name)

    def _take_icon(self, shown: Shown, url: str) -> None:
        """The picture WhatsApp named for `shown`'s latest message: the cached one when it's the
        same (by digest), else its address, kept only until it's fetched (_picture). The old
        picture stays on the bubble meanwhile, and if the new one can't be had."""
        key, digest = (shown.account, shown.chat), avatar_key(url)
        cached = self.avatars.get(key)
        if cached and cached[0] == digest:
            self.avatars.move_to_end(key)
            shown.icon, shown.icon_key, shown.icon_url = cached[1], digest, ""
        elif digest:
            shown.icon_key, shown.icon_url = digest, url
        else:
            shown.icon, shown.icon_key, shown.icon_url = None, "", ""

    def _reply_ok(self, shown: Shown) -> bool:
        """Whether `shown` offers Reply: a chat's message, unless Preferences turned Reply off."""
        return shown.replyable and bool(self.settings["notify_reply"])

    def _reveal(self) -> tuple[bool, bool]:
        """Whether notifications may show who it's from, and the message."""
        blurred = self.settings["privacy_blur"] or getattr(self.app, "locked", False)
        return self.settings["notify_sender"] and not blurred, self.settings["notify_preview"] and not blurred

    def _deliver(self, shown: Shown) -> None:
        """Show the chat's latest content. One call per chat at a time; the latest content waits."""
        if shown.sending:
            shown.again = True
            return
        shown.sending = True
        if shown.icon_url and self._reveal()[0]:
            self._picture(shown, lambda picture: self._send(shown, picture))
        else:
            self._send(shown, shown.icon)

    def _send(self, shown: Shown, picture: Optional[GdkPixbuf.Pixbuf]) -> None:
        shown.again = False
        if shown.closed:
            self._finish(shown)
            return
        sender, preview = self._reveal()
        picture = picture if sender else None
        if self.server is None:
            self._post(shown, picture, sender, preview)
            self._finish(shown)
            return
        if shown.posted:
            self.app.withdraw_notification(shown.gid)
            shown.posted = False
        capabilities = self.capabilities
        markup = capabilities is None or "body-markup" in capabilities
        summary, text = texts(shown.title, shown.body, shown.count, shown.account_name, sender, preview, markup)
        hints = {
            "desktop-entry": GLib.Variant("s", APP_ID),
            "category": GLib.Variant("s", "im.received"),
            "urgency": GLib.Variant("y", 1),
            # WhatsApp plays its own sound, as set in its Settings → Notifications.
            "suppress-sound": GLib.Variant("b", True),
        }
        if picture is not None:
            hints["image-data"] = image_data(picture)
        actions: list[str] = []
        inline = False
        if capabilities and "actions" in capabilities:
            actions = ["default", "Open"]
            if self._reply_ok(shown):
                # KDE's reply field only when the bubble says who it's to, so nobody types to a chat
                # they can't see (and so never while locked). Otherwise Reply opens the reply
                # window, which does say.
                inline = sender and "inline-reply" in capabilities
                actions += ["inline-reply" if inline else "reply", "Reply"]
            actions += ["mute", "Mute Chat for 1 Hour"]
        if inline:
            hints["x-kde-reply-placeholder-text"] = GLib.Variant("s", "Type a reply")
            hints["x-kde-reply-submit-button-text"] = GLib.Variant("s", "Send")
            hints["x-kde-reply-submit-button-icon-name"] = GLib.Variant("s", "document-send")
        self._posting(shown, inline)
        params = GLib.Variant("(susssasa{sv}i)", (NAME, shown.server_id, APP_ICON, summary, text, actions, hints, -1))
        self.bus.call(BUS, PATH, BUS, "Notify", params, GLib.VariantType("(u)"),
                      Gio.DBusCallFlags.NONE, 5000, None, self._sent, shown)

    def _post(self, shown: Shown, picture: Optional[GdkPixbuf.Pixbuf], sender: bool, preview: bool) -> None:
        """Show it through GNotification: the same id per bubble replaces it in place."""
        summary, text = texts(shown.title, shown.body, shown.count, shown.account_name, sender, preview, False)
        notification = Gio.Notification.new(summary)
        if text:
            notification.set_body(text)
        notification.set_category("im.received")
        if picture is not None:
            ok, png = picture.save_to_bufferv("png", [], [])
            if ok:
                notification.set_icon(Gio.BytesIcon.new(GLib.Bytes.new(png)))
        # The desktop may keep the targets on disk: they name the chat by the bubble's random
        # reference, and the open target has its name only when the bubble shows it (it opens the
        # chat by name when Natter has started again since, and no longer knows the reference).
        title = shown.title if sender else ""
        notification.set_default_action_and_target(
            "app.notification-open", GLib.Variant("(sssi)", (shown.account, shown.ref, title, shown.page_id)))
        if self._reply_ok(shown):
            notification.add_button_with_target(
                "Reply", "app.notification-reply", GLib.Variant("(ss)", (shown.account, shown.ref)))
        notification.add_button_with_target(
            "Mute Chat for 1 Hour", "app.notification-mute", GLib.Variant("(ss)", (shown.account, shown.ref)))
        self.app.send_notification(shown.gid, notification)
        shown.posted = True

    # ---- KDE's reply field ------------------------------------------------------------------
    # Each Notify call gives the bubble a new field, and may replace one the person is typing in.
    # What KDE sends back names only the bubble, so every field posted lately is answerable, each
    # once: a newer message mustn't make a reply typed meanwhile vanish, and one field can't send
    # twice.

    def _posting(self, shown: Shown, inline: bool) -> None:
        now = time.monotonic()
        shown.fields = {post: gone or now for post, gone in shown.fields.items()
                        if not gone or now - gone <= REPLY_GRACE}
        shown.posts += 1
        if inline:
            shown.fields[shown.posts] = 0.0

    def _retire_fields(self, shown: Shown) -> None:
        """The bubble left the screen: its field still answers for REPLY_GRACE."""
        now = time.monotonic()
        shown.fields = {post: gone or now for post, gone in shown.fields.items()}

    def _take_field(self, shown: Shown) -> bool:
        """Whether text from KDE for `shown` is an answer from one of its fields that hasn't
        answered yet (the latest such), which then has."""
        now = time.monotonic()
        live = [post for post, gone in shown.fields.items() if not gone or now - gone <= REPLY_GRACE]
        if not live:
            return False
        del shown.fields[max(live)]
        return True

    def _sent(self, bus, result, shown: Shown) -> None:
        try:
            server_id = bus.call_finish(result).unpack()[0]
        except GLib.Error as error:
            if not self._warned:
                self._warned = True
                print(f"natter: notifications failed: {error.message}", flush=True)
            self._finish(shown)
            return
        if shown.server_id and shown.server_id != server_id:
            self.by_server.pop(shown.server_id, None)
        shown.server_id = server_id
        self.by_server[server_id] = shown
        self.given[server_id] = None
        self.given.move_to_end(server_id)
        while len(self.given) > GIVEN_IDS:
            self.given.popitem(last=False)
        self._finish(shown)

    def _finish(self, shown: Shown) -> None:
        shown.sending = False
        if shown.closed:
            self._close_on_server(shown)
        elif shown.again:
            self._deliver(shown)

    def _picture(self, shown: Shown, then: Callable[[Optional[GdkPixbuf.Pixbuf]], None]) -> None:
        """The sender's picture, circled: kept per chat, by a digest of its address (avatar_key),
        fetched again when that changes. Once decoded, the bubble keeps the picture and drops
        the address; one that can't be had is tried again at the next delivery, with the picture
        the bubble already has meanwhile."""
        key, url, digest = (shown.account, shown.chat), shown.icon_url, shown.icon_key
        cached = self.avatars.get(key)
        if cached and cached[0] == digest:
            self.avatars.move_to_end(key)
            shown.icon, shown.icon_url = cached[1], ""
            then(cached[1])
            return

        def got(raw: Optional[bytes]) -> None:
            picture = decode_image(raw) if raw else None
            if picture is None:
                then(shown.icon or (cached[1] if cached else None))
                return
            picture = circle(picture)
            self.avatars[key] = (digest, picture)
            self.avatars.move_to_end(key)
            while len(self.avatars) > AVATARS:
                self.avatars.popitem(last=False)
            if shown.icon_key == digest:  # not a newer address that came meanwhile
                shown.icon, shown.icon_url = picture, ""
            then(picture)

        self._download(url, got)

    def _download(self, url: str, then: Callable[[Optional[bytes]], None]) -> None:
        """The picture's bytes, if Natter fetches from there and it's no bigger than AVATAR_BYTES."""
        if not picture_allowed(url):
            then(None)
            return
        inline = DATA_URL.match(url)
        if inline:
            encoded = url[inline.end():]
            try:
                raw = base64.b64decode(encoded) if len(encoded) <= AVATAR_BYTES * 4 // 3 + 4 else b""
            except ValueError:
                raw = b""
            then(raw or None)
            return
        message = Soup.Message.new_from_uri("GET", GLib.Uri.parse(url, GLib.UriFlags.NONE))
        message.add_flags(Soup.MessageFlags.NO_REDIRECT)  # a redirect could leave WhatsApp's servers
        chunks: list[bytes] = []
        size = 0

        def stop(stream: Gio.InputStream) -> None:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
            then(None)

        def read(stream: Gio.InputStream, result) -> None:
            nonlocal size
            try:
                data = stream.read_bytes_finish(result)
            except GLib.Error:
                then(None)
                return
            if data.get_size() == 0:
                then(b"".join(chunks))
                return
            size += data.get_size()
            if size > AVATAR_BYTES:
                stop(stream)
                return
            chunks.append(data.get_data())
            stream.read_bytes_async(64 * 1024, GLib.PRIORITY_DEFAULT, None, read)

        def opened(session: Soup.Session, result) -> None:
            try:
                stream = session.send_finish(result)
            except GLib.Error:
                then(None)
                return
            if message.get_status() != 200 or message.get_response_headers().get_content_length() > AVATAR_BYTES:
                stop(stream)
                return
            stream.read_bytes_async(64 * 1024, GLib.PRIORITY_DEFAULT, None, read)

        self.http.send_async(message, GLib.PRIORITY_DEFAULT, None, opened)

    # ---- Clearing --------------------------------------------------------------------------

    def withdraw(self, account_id: Optional[str] = None, chat: Optional[str] = None) -> None:
        """Take notifications off screen: one chat's, one account's, or all of them."""
        for key, shown in list(self.shown.items()):
            if (account_id is None or key[0] == account_id) and (chat is None or key[1] == chat):
                self._close(shown)

    def withdraw_page(self, account_id: str, page_id: int) -> None:
        for shown in list(self.shown.values()):
            if shown.account == account_id and shown.page_id == page_id:
                self._close(shown)

    def _close(self, shown: Shown) -> None:
        if self.shown.get((shown.account, shown.chat)) is shown:
            del self.shown[(shown.account, shown.chat)]
        shown.closed = True
        self._retire_fields(shown)
        # A click on it in the desktop's list can still come for a little while (GNotification).
        GLib.timeout_add_seconds(REPLY_GRACE, self._drop_ref, shown.ref)
        if shown.posted:
            self.app.withdraw_notification(shown.gid)
            shown.posted = False
        # A Notify under way closes its bubble once the server says which one it is.
        if not shown.sending:
            self._close_on_server(shown)

    def _drop_ref(self, ref: str) -> bool:
        shown = self.by_ref.get(ref)
        if shown is not None and self.shown.get((shown.account, shown.chat)) is not shown:
            del self.by_ref[ref]
        return GLib.SOURCE_REMOVE

    def _close_on_server(self, shown: Shown) -> None:
        if not shown.server_id:
            return
        if self.server:
            self.bus.call(BUS, PATH, BUS, "CloseNotification", GLib.Variant("(u)", (shown.server_id,)),
                          None, Gio.DBusCallFlags.NONE, -1, None, None)
        # Known by its id a while longer: the server may send a click or a reply it had already
        # taken before it heard of the close.
        GLib.timeout_add_seconds(REPLY_GRACE, self._forget, shown.server_id)
        shown.server_id = 0

    # ---- Clicks ----------------------------------------------------------------------------

    def _by_ref(self, account_id: str, ref: str) -> Optional[Shown]:
        shown = self.by_ref.get(ref)
        return shown if shown is not None and shown.account == account_id else None

    def _open_action(self, account_id: str, ref: str, title: str, page_id: int) -> None:
        shown = self._by_ref(account_id, ref)
        if shown:
            title, page_id = shown.title, shown.page_id
            self._close(shown)
        # GTK takes the activation token from the click itself.
        self.on_open(account_id, title, page_id, "")

    def _mute_action(self, account_id: str, ref: str) -> None:
        # A notification from before Natter last started has a reference nobody knows: it mutes
        # nothing, as its chat can't be told from it.
        shown = self._by_ref(account_id, ref)
        if shown:
            self.muted[(account_id, shown.chat)] = time.time() + MUTE_SECONDS
            self._close(shown)

    def _reply_action(self, account_id: str, ref: str) -> None:
        shown = self._by_ref(account_id, ref)
        if shown is None:
            # From before Natter last started: the window, on that account (through the lock).
            self.app.open_account(account_id)
        elif self.on_reply is not None:
            # Not closed: the desktop takes it away itself, and it's the chat's way back meanwhile.
            self.on_reply(self.target(shown), "", None, False)

    def _on_signal(self, _bus, sender: str, _path, _iface, signal: str, params: GLib.Variant) -> None:
        # Only the server's own: GLib before 2.80.1 let any program send a signal in its name
        # straight to Natter (CVE-2024-34397), and KDE's replies come that way.
        if sender != self.server:
            return
        values = params.unpack()
        server_id = values[0]
        shown = self.by_server.get(server_id)
        if signal == "ActivationToken":
            if shown is not None:  # not another app's, from a server that sends them to everyone
                self.tokens[server_id] = values[1]
            return
        if signal == "NotificationClosed":
            if shown and self.shown.get((shown.account, shown.chat)) is shown:
                del self.shown[(shown.account, shown.chat)]
            if shown:
                self._retire_fields(shown)
            # Some servers report the close before the click that caused it: keep the
            # click target around for a little while.
            GLib.timeout_add_seconds(REPLY_GRACE, self._forget, server_id)
            return
        if signal == "NotificationReplied":
            if shown is not None or server_id in self.given:  # Natter's, if it has forgotten it
                self._replied(shown, values[1])
            return
        if signal != "ActionInvoked" or shown is None:
            return
        token = self.tokens.pop(server_id, "")
        if values[1] == "mute":
            self.muted[(shown.account, shown.chat)] = time.time() + MUTE_SECONDS
            self._close(shown)
        elif values[1] in ("reply", "inline-reply"):
            # A Reply button (or KDE's field, sent empty): the reply window. The bubble stays
            # until the reply is sent; the server usually takes it away itself.
            if self.on_reply is not None:
                self.on_reply(self.target(shown), token, None, False)
        else:
            self._close(shown)
            self.on_open(shown.account, shown.title, shown.page_id, token)

    def _replied(self, shown: Optional[Shown], text: str) -> None:
        """KDE sent what was typed in a bubble's reply field. Sent on only as the answer of a field
        Natter posted, once; anything else is kept for the reply window (or, for a bubble Natter
        no longer knows, said to be lost), never sent and never dropped without a word."""
        if self.on_reply is None:
            return
        if shown is None:
            self.on_reply(None, "", text, True)
            return
        self.on_reply(self.target(shown), "", text, not self._take_field(shown))

    def _forget(self, server_id: int) -> bool:
        shown = self.by_server.get(server_id)
        if shown and self.shown.get((shown.account, shown.chat)) is not shown:
            del self.by_server[server_id]
            if self.by_ref.get(shown.ref) is shown:
                del self.by_ref[shown.ref]
        self.tokens.pop(server_id, None)
        return GLib.SOURCE_REMOVE
