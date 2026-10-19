"""Desktop notifications over org.freedesktop.Notifications, one per chat.

Natter talks to the notification server directly rather than through GNotification so it can
replace a chat's notification in place (one bubble per chat, with a count), attach the sender's
picture, and pass the activation token that lets the window come forward on Wayland.
"""

from __future__ import annotations

import base64
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import GdkPixbuf, Gio, GLib, Soup

from . import APP_ID, NAME

BUS = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
AVATAR = 96
MUTE_SECONDS = 3600


@dataclass
class Shown:
    """A notification on screen for one chat."""

    account: str
    chat: str
    title: str
    page_id: int  # the id WhatsApp's page gave it, for routing the click back
    count: int = 1
    server_id: int = 0
    token: str = ""
    sent_at: float = field(default_factory=time.time)


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
    return GLib.Variant(
        "(iiibiiay)",
        (
            pixbuf.get_width(),
            pixbuf.get_height(),
            pixbuf.get_rowstride(),
            pixbuf.get_has_alpha(),
            pixbuf.get_bits_per_sample(),
            pixbuf.get_n_channels(),
            pixbuf.get_pixels(),
        ),
    )


def decode_image(raw: bytes) -> Optional[GdkPixbuf.Pixbuf]:
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(raw)
        loader.close()
        return loader.get_pixbuf()
    except GLib.Error:
        return None


class Notifier:
    """Shows WhatsApp's notifications as Natter's own and reports clicks.

    `on_open(account_id, chat, page_id, token)` runs when one is clicked.
    """

    def __init__(self, settings, on_open: Callable[[str, str, int, str], None]) -> None:
        self.settings = settings
        self.on_open = on_open
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.shown: dict[tuple[str, str], Shown] = {}
        self.by_server: dict[int, Shown] = {}
        self.tokens: dict[int, str] = {}
        self.muted: dict[tuple[str, str], float] = {}
        self.http = Soup.Session(timeout=8, max_conns=4)
        self.capabilities: set[str] = set()
        for signal in ("ActionInvoked", "NotificationClosed", "ActivationToken"):
            self.bus.signal_subscribe(BUS, BUS, signal, PATH, None, Gio.DBusSignalFlags.NONE, self._on_signal)
        self.bus.call(BUS, PATH, BUS, "GetCapabilities", None, GLib.VariantType("(as)"),
                      Gio.DBusCallFlags.NONE, 2000, None, self._got_capabilities)

    def _got_capabilities(self, bus, result) -> None:
        try:
            self.capabilities = set(bus.call_finish(result).unpack()[0])
        except GLib.Error:
            pass

    # ---- Showing ---------------------------------------------------------------------------

    def show(self, account_id: str, account_name: Optional[str], page_id: int, title: str, body: str,
             chat: str, icon: str) -> None:
        """WhatsApp asked for a notification. `chat` identifies the conversation (its tag or title)."""
        key = (account_id, chat or title)
        if not self.settings["notifications"] or self.settings.in_quiet_hours():
            return
        if self.muted.get(key, 0) > time.time():
            return
        current = self.shown.get(key)
        if current:
            current.count += 1
            current.page_id = page_id
            current.title = title
        else:
            current = self.shown[key] = Shown(account_id, chat or title, title, page_id)

        summary = title if self.settings["notify_sender"] else "New message"
        if current.count > 1:
            summary = f"{summary} ({current.count})" if self.settings["notify_sender"] else f"{current.count} new messages"
        if account_name:
            summary = f"{summary} · {account_name}"
        text = body if self.settings["notify_preview"] else ""
        text = GLib.markup_escape_text(text) if "body-markup" in self.capabilities else text

        def send(picture: Optional[GdkPixbuf.Pixbuf]) -> None:
            hints = {
                "desktop-entry": GLib.Variant("s", APP_ID),
                "category": GLib.Variant("s", "im.received"),
                "urgency": GLib.Variant("y", 1),
                # WhatsApp plays its own sound, as set in its Settings → Notifications.
                "suppress-sound": GLib.Variant("b", True),
            }
            if picture is not None and self.settings["notify_sender"]:
                hints["image-data"] = image_data(picture)
            actions = ["default", "Open", "mute", "Mute for an hour"]
            params = GLib.Variant(
                "(susssasa{sv}i)", (NAME, current.server_id, APP_ID, summary, text, actions, hints, -1)
            )
            self.bus.call(BUS, PATH, BUS, "Notify", params, GLib.VariantType("(u)"),
                          Gio.DBusCallFlags.NONE, 5000, None, self._sent, current)

        if icon and self.settings["notify_sender"]:
            self._fetch_picture(icon, send)
        else:
            send(None)

    def _sent(self, bus, result, shown: Shown) -> None:
        try:
            server_id = bus.call_finish(result).unpack()[0]
        except GLib.Error as error:
            print(f"natter: notification failed: {error.message}")
            return
        if shown.server_id and shown.server_id != server_id:
            self.by_server.pop(shown.server_id, None)
        shown.server_id = server_id
        self.by_server[server_id] = shown

    def _fetch_picture(self, icon: str, then: Callable[[Optional[GdkPixbuf.Pixbuf]], None]) -> None:
        if icon.startswith("data:"):
            try:
                raw = base64.b64decode(icon.split(",", 1)[1])
            except (IndexError, ValueError):
                raw = b""
            picture = decode_image(raw) if raw else None
            then(circle(picture) if picture else None)
            return
        try:
            message = Soup.Message.new("GET", icon)
        except TypeError:
            message = None
        if message is None:
            then(None)
            return

        def done(session, result) -> None:
            picture = None
            try:
                data = session.send_and_read_finish(result)
                if message.get_status() == 200 and data.get_size() < 2_000_000:
                    picture = decode_image(data.get_data())
            except GLib.Error:
                pass
            then(circle(picture) if picture else None)

        self.http.send_and_read_async(message, GLib.PRIORITY_DEFAULT, None, done)

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
        self.shown.pop((shown.account, shown.chat), None)
        if shown.server_id:
            self.by_server.pop(shown.server_id, None)
            self.bus.call(BUS, PATH, BUS, "CloseNotification", GLib.Variant("(u)", (shown.server_id,)),
                          None, Gio.DBusCallFlags.NONE, -1, None, None)

    # ---- Signals from the server ----------------------------------------------------------

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
        action = values[1]
        if action == "mute":
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
