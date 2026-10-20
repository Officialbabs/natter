"""Desktop notifications, one per chat.

Natter talks to org.freedesktop.Notifications directly rather than through GNotification so it
can replace a chat's notification in place (one bubble per chat, with a count), attach the
sender's picture, and pass the activation token that lets the window come forward on Wayland.
Inside a sandbox, or with no notification server on the bus, it uses GNotification, which goes
through the portal or GLib's own backends.
"""

from __future__ import annotations

import base64
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import GdkPixbuf, Gio, GLib, Soup

from . import APP_ID, DEFAULT_APP_ID, NAME
from .launcher import INVISIBLE

# The app icon as a file path: the spec allows one, and it works where icon names in
# ~/.local/share/icons aren't looked up (Ada's shell). It's named by the default id, as in
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
    icon: str = ""
    account_name: Optional[str] = None
    count: int = 1
    server_id: int = 0  # the notification server's id for it
    posted: bool = False  # shown through GNotification instead
    sending: bool = False  # its picture or Notify call is under way
    again: bool = False  # newer content arrived meanwhile
    closed: bool = False  # withdrawn

    @property
    def gid(self) -> str:
        return f"{self.account}/{self.chat}"


class Notifier:
    """Shows WhatsApp's notifications as Natter's own and reports clicks.

    `on_open(account_id, chat, page_id, token)` runs when one is clicked.
    """

    def __init__(self, app, on_open: Callable[[str, str, int, str], None]) -> None:
        self.app = app
        self.settings = app.settings
        self.on_open = on_open
        self.bus = app.get_dbus_connection()
        self.shown: dict[tuple[str, str], Shown] = {}
        self.by_server: dict[int, Shown] = {}
        self.tokens: dict[int, str] = {}
        self.muted: dict[tuple[str, str], float] = {}
        self.avatars: OrderedDict[tuple[str, str], tuple[str, GdkPixbuf.Pixbuf]] = OrderedDict()
        self.http = Soup.Session(timeout=8, max_conns=4)
        self.server: Optional[str] = None  # the notification server's unique name, when Natter talks to it
        self.capabilities: Optional[set[str]] = None  # None until the server says
        self._warned = False
        for name, target, run in [("notification-open", "(sssi)", self._open_action),
                                  ("notification-mute", "(ss)", self._mute_action)]:
            action = Gio.SimpleAction.new(name, GLib.VariantType(target))
            action.connect("activate", lambda _a, value, run=run: run(*value.unpack()))
            app.add_action(action)
        if in_sandbox():
            return
        for signal in ("ActionInvoked", "NotificationClosed", "ActivationToken"):
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
            shown.page_id, shown.title, shown.body, shown.icon = page_id, title, body, icon
            shown.account_name = account_name
        else:
            shown = self.shown[key] = Shown(account_id, key[1], title, page_id, body, icon, account_name)
        self._deliver(shown)

    def conceal(self) -> None:
        """Show the notifications already up again, now hiding who wrote and what (Natter locked)."""
        for shown in list(self.shown.values()):
            if not shown.closed:
                self._deliver(shown)

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
        if shown.icon and self._reveal()[0]:
            self._picture(shown, lambda picture: self._send(shown, picture))
        else:
            self._send(shown, None)

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
        actions = (["default", "Open", "mute", "Mute Chat for 1 Hour"] if capabilities and "actions" in capabilities
                   else [])
        params = GLib.Variant("(susssasa{sv}i)", (NAME, shown.server_id, APP_ICON, summary, text, actions, hints, -1))
        self.bus.call(BUS, PATH, BUS, "Notify", params, GLib.VariantType("(u)"),
                      Gio.DBusCallFlags.NONE, 5000, None, self._sent, shown)

    def _post(self, shown: Shown, picture: Optional[GdkPixbuf.Pixbuf], sender: bool, preview: bool) -> None:
        """Show it through GNotification: the same id per chat replaces it in place."""
        summary, text = texts(shown.title, shown.body, shown.count, shown.account_name, sender, preview, False)
        notification = Gio.Notification.new(summary)
        if text:
            notification.set_body(text)
        notification.set_category("im.received")
        if picture is not None:
            ok, png = picture.save_to_bufferv("png", [], [])
            if ok:
                notification.set_icon(Gio.BytesIcon.new(GLib.Bytes.new(png)))
        # The desktop may keep the target on disk: it names the chat only when the bubble does.
        title = shown.title if sender else ""
        notification.set_default_action_and_target(
            "app.notification-open", GLib.Variant("(sssi)", (shown.account, shown.chat, title, shown.page_id)))
        notification.add_button_with_target(
            "Mute Chat for 1 Hour", "app.notification-mute", GLib.Variant("(ss)", (shown.account, shown.chat)))
        self.app.send_notification(shown.gid, notification)
        shown.posted = True

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
        self._finish(shown)

    def _finish(self, shown: Shown) -> None:
        shown.sending = False
        if shown.closed:
            self._close_on_server(shown)
        elif shown.again:
            self._deliver(shown)

    def _picture(self, shown: Shown, then: Callable[[Optional[GdkPixbuf.Pixbuf]], None]) -> None:
        """The sender's picture, circled: kept per chat, fetched again when its address changes."""
        key, url = (shown.account, shown.chat), shown.icon
        cached = self.avatars.get(key)
        if cached and cached[0] == url:
            self.avatars.move_to_end(key)
            then(cached[1])
            return

        def got(raw: Optional[bytes]) -> None:
            picture = decode_image(raw) if raw else None
            if picture is None:
                then(cached[1] if cached else None)
                return
            picture = circle(picture)
            self.avatars[key] = (url, picture)
            self.avatars.move_to_end(key)
            while len(self.avatars) > AVATARS:
                self.avatars.popitem(last=False)
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
        if shown.posted:
            self.app.withdraw_notification(shown.gid)
            shown.posted = False
        # A Notify under way closes its bubble once the server says which one it is.
        if not shown.sending:
            self._close_on_server(shown)

    def _close_on_server(self, shown: Shown) -> None:
        if not shown.server_id:
            return
        self.by_server.pop(shown.server_id, None)
        if self.server:
            self.bus.call(BUS, PATH, BUS, "CloseNotification", GLib.Variant("(u)", (shown.server_id,)),
                          None, Gio.DBusCallFlags.NONE, -1, None, None)
        shown.server_id = 0

    # ---- Clicks ----------------------------------------------------------------------------

    def _open_action(self, account_id: str, chat: str, title: str, page_id: int) -> None:
        shown = self.shown.get((account_id, chat))
        if shown:
            title, page_id = shown.title, shown.page_id
            self._close(shown)
        # GTK takes the activation token from the click itself.
        self.on_open(account_id, title, page_id, "")

    def _mute_action(self, account_id: str, chat: str) -> None:
        self.muted[(account_id, chat)] = time.time() + MUTE_SECONDS
        shown = self.shown.get((account_id, chat))
        if shown:
            self._close(shown)

    def _on_signal(self, _bus, _sender, _path, _iface, signal: str, params: GLib.Variant) -> None:
        values = params.unpack()
        server_id = values[0]
        if signal == "ActivationToken":
            self.tokens[server_id] = values[1]
            return
        shown = self.by_server.get(server_id)
        if signal == "NotificationClosed":
            if shown and self.shown.get((shown.account, shown.chat)) is shown:
                del self.shown[(shown.account, shown.chat)]
            # Some servers report the close before the click that caused it: keep the
            # click target around for a little while.
            GLib.timeout_add_seconds(30, self._forget, server_id)
            return
        if signal != "ActionInvoked" or shown is None:
            return
        token = self.tokens.pop(server_id, "")
        if values[1] == "mute":
            self.muted[(shown.account, shown.chat)] = time.time() + MUTE_SECONDS
            self._close(shown)
        else:
            self._close(shown)
            self.on_open(shown.account, shown.title, shown.page_id, token)

    def _forget(self, server_id: int) -> bool:
        shown = self.by_server.get(server_id)
        if shown and self.shown.get((shown.account, shown.chat)) is not shown:
            del self.by_server[server_id]
        self.tokens.pop(server_id, None)
        return GLib.SOURCE_REMOVE
