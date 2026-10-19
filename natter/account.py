"""One WhatsApp account: its own WebKit session (cookies, storage, cache) and page."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
from gi.repository import Gio, GLib, GObject, Gtk, JavaScriptCore, WebKit

from . import WHATSAPP_URL, rtc
from .links import chat_link, is_whatsapp, same_site, short_link, unread_in
from .settings import CACHE, DATA, ChatIndex

HERE = Path(__file__).resolve().parent
BRIDGE = (HERE / "bridge.js").read_text()
RTC = (HERE / "rtc.js").read_text()
STYLE = (HERE / "style.css").read_text()
WHATSAPP_PAGES = ["https://web.whatsapp.com/*"]
CHUNK = 4 * 1024 * 1024  # bytes per call when handing a file to the page
# During a call, smaller pieces with a pause between them: each piece holds the page (and Natter)
# for a moment, and the call's packets go through the same page.
CALL_CHUNK = 256 * 1024
CALL_GAP_MS = 15
LARGEST = 256 * 1024 * 1024  # bigger than this: drag it into the window instead
READY_WAIT = 20  # seconds a call into the page waits for WhatsApp's chat list before giving up
LINK_WAIT = 4  # seconds a wa.me link opened in the page gets to open its chat before Natter loads it

# After a crash the page reloads after 1, 5, 30, then 120 s; a crash more than ten minutes
# after the one before counts as the first again. Three within two minutes is a crash loop:
# Natter stops reloading until asked to.
BACKOFF = (1, 5, 30, 120)
STEADY = 10 * 60
LOOP_CRASHES, LOOP_SECONDS = 3, 120

# What the page may tell Natter: anything longer is cut, anything of the wrong kind is dropped.
NAME_LIMIT = 200  # a chat's name, a notification's title
BODY_LIMIT = 2000
ROWS_LIMIT = 200
ICON_TYPES = ("image/jpeg", "image/png", "image/webp")
ICON_LIMIT = 2_000_000  # bytes
# Bidi controls and invisible marks, dropped from all of it: they can disguise a name
# ("report\u202efdp.exe") and nothing needs them to show one. The bridge drops the same ones from
# chat names so the names still match. The zero-width joiner (U+200D) and variation selectors
# stay: emoji are made with them.
INVISIBLE = frozenset("\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff")

OUTSIDE = ("http", "https", "mailto", "tel")  # links Natter hands to the browser (or mail, or phone) app

# No user agent of Natter's own: WebKitGTK's site-specific quirks already present WhatsApp
# Web with desktop Safari, and WebKit keeps that quirk current.


# WebKit features Natter turns off: it only ever shows one site, so it has no pages to go
# back to and nothing to swap processes for. (WhatsApp's cross-origin isolation headers still
# make WebKit start a fresh process for it, leaving the first, blank one idle: ~100 MB of
# cold memory that the background reclaim hands back.)
FEATURES_OFF = ("ProcessSwapOnCrossSiteNavigation", "UsesBackForwardCache", "SpeechRecognition",
                # WhatsApp's service worker passes every download through itself, and in WebKitGTK
                # some arrive damaged: emoji sprite sheets drew as slivers, blanks and magnified
                # squares. Without it they draw whole; it's only there for offline caching.
                "ServiceWorkers")


# NATTER_FEATURES_ON / NATTER_FEATURES_OFF (comma-separated identifiers) are for experiments.
FEATURES_ON = tuple(f for f in (GLib.getenv("NATTER_FEATURES_ON") or "").split(",") if f)
FEATURES_OFF = FEATURES_OFF + tuple(f for f in (GLib.getenv("NATTER_FEATURES_OFF") or "").split(",") if f)


def _features(settings: WebKit.Settings, off=FEATURES_OFF, on=FEATURES_ON) -> None:
    features = WebKit.Settings.get_all_features()
    for i in range(features.get_length()):
        feature = features.get(i)
        if feature.get_identifier() in off:
            settings.set_feature_enabled(feature, False)
        elif feature.get_identifier() in on:
            settings.set_feature_enabled(feature, True)


def _set(settings: WebKit.Settings, **values) -> None:
    """Set what this WebKit version has; older ones lack some of these."""
    for name, value in values.items():
        if settings.find_property(name.replace("_", "-")) is not None:
            settings.set_property(name.replace("_", "-"), value)


def _acceleration(on: bool) -> WebKit.HardwareAccelerationPolicy:
    return WebKit.HardwareAccelerationPolicy.ALWAYS if on else WebKit.HardwareAccelerationPolicy.NEVER


# ---- Checking what the page says ------------------------------------------------------------

def clean(value, limit: int, lines: bool = False) -> Optional[str]:
    """`value` as plain text: no control or INVISIBLE characters (line breaks kept if `lines`), at most `limit` long.

    None when it isn't text at all.
    """
    if not isinstance(value, str):
        return None
    kept = []
    for c in value[: limit * 2]:
        if c == "\n" and lines:
            kept.append(c)
        elif c in "\t\n\r":
            kept.append(" ")
        # Cs: half a surrogate pair, which GLib can't take
        elif unicodedata.category(c) not in ("Cc", "Cs") and c not in INVISIBLE:
            kept.append(c)
    return "".join(kept).strip()[:limit]


def clean_icon(value) -> str:
    """A notification's picture as the page may hand it over: WhatsApp's servers' or a small JPEG, PNG or WebP."""
    if not isinstance(value, str) or not value:
        return ""
    if value.startswith("data:"):
        header, _, data = value.partition(",")
        if (header[5:] in (f"{kind};base64" for kind in ICON_TYPES) and len(data) * 3 // 4 <= ICON_LIMIT
                and re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", data)):
            return value
        return ""
    parsed = urlparse(value)
    # Natter fetches these itself, so only from WhatsApp, and nothing a fetcher could read another way.
    if (parsed.scheme == "https" and (parsed.hostname or "").endswith(".whatsapp.net") and parsed.port is None
            and len(value) <= 2048 and not re.search(r"[\\@\s]", value)):
        return value
    return ""


def _page_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**31


def parse_message(message) -> Optional[dict]:
    """A message from the bridge, checked and cleaned; None unless it's one Natter knows, in its shape."""
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "notify":
        title, body = clean(message.get("title"), NAME_LIMIT), clean(message.get("body"), BODY_LIMIT, lines=True)
        tag = clean(message.get("tag"), NAME_LIMIT)
        if not _page_id(message.get("id")) or title is None or body is None or tag is None:
            return None
        return {"type": kind, "id": message["id"], "title": title, "body": body, "tag": tag,
                "icon": clean_icon(message.get("icon"))}
    if kind == "unnotify":
        return {"type": kind, "id": message["id"]} if _page_id(message.get("id")) else None
    if kind == "chats":
        rows = message.get("rows")
        if not isinstance(rows, list):
            return None
        cleaned = []
        for row in rows[:ROWS_LIMIT]:
            if not (isinstance(row, list) and len(row) == 2 and isinstance(row[0], str)
                    and isinstance(row[1], int) and not isinstance(row[1], bool) and 0 <= row[1] < 10**6):
                return None
            name = clean(row[0], NAME_LIMIT)
            if name:
                cleaned.append((name, row[1]))
        return {"type": kind, "rows": cleaned}
    if kind == "active":
        name = clean(message.get("name"), NAME_LIMIT)
        return None if name is None else {"type": kind, "name": name}
    if kind == "ready":
        return {"type": kind, "ready": message["ready"]} if isinstance(message.get("ready"), bool) else None
    if kind == "state":
        value = message.get("value")
        return {"type": kind, "value": value} if value in ("needs-link", "chats") else None
    if kind == "call":
        return {"type": kind, "video": message["video"]} if isinstance(message.get("video"), bool) else None
    return None


def reload_delay(crashes: list[float]) -> Optional[int]:
    """Seconds to wait before reloading a page that crashed at these times; None in a crash loop.

    The times are oldest first, and the last one is now.
    """
    now = crashes[-1]
    if sum(1 for t in crashes if now - t <= LOOP_SECONDS) >= LOOP_CRASHES:
        return None
    streak = 1
    for earlier, later in zip(reversed(crashes[:-1]), reversed(crashes[1:])):
        if later - earlier > STEADY:
            break
        streak += 1
    return BACKOFF[min(streak, len(BACKOFF)) - 1]


def file_name(suggested: str) -> str:
    """A name to save a download under: no folders, control or invisible characters, not hidden, never empty."""
    name = re.sub(r"^[.\s]+", "", clean(Path(suggested or "").name, 1024) or "")
    if not name:
        return "download"
    stem, suffix = os.path.splitext(name)
    if len(suffix) > 16:
        stem, suffix = name, ""
    # Most file systems stop at 255 bytes; this leaves room for " (2)".
    return stem.encode()[: NAME_LIMIT - len(suffix.encode())].decode(errors="ignore") + suffix


def _free_path(folder: Path, name: str) -> Path:
    """`folder`/`name`, or "name (2)" and so on when that's taken."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        n += 1
        target = folder / f"{stem} ({n}){suffix}"
    return target


def _downloads_folder() -> Path:
    return Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or Path.home() / "Downloads")


class Account(GObject.Object):
    """A WhatsApp session in a WebView, and what Natter learns from it."""

    __gsignals__ = {
        "unread-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "chats-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "notify-message": (GObject.SignalFlags.RUN_FIRST, None, (int, str, str, str, str)),
        "unnotify": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "download-done": (GObject.SignalFlags.RUN_FIRST, None, (str, bool, str)),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "call-requested": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "in-call": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, app, info: dict, url: Optional[str] = None) -> None:
        super().__init__()
        self.app = app
        self.id: str = info["id"]
        self.name: str = info.get("name") or "WhatsApp"
        self.unread = 0
        self.active_chat = ""
        self.ready = False
        self.needs_link = False  # WhatsApp shows its link screen (QR code)
        self.crashing = False  # crashed again and again: not reloading until reload()
        self.removed = False
        self.failed: Optional[str] = None
        self.dev_url = url
        self.home = url or WHATSAPP_URL
        self.chats = ChatIndex(DATA / "accounts" / self.id / "chats.json")
        self._pending: dict[int, tuple[Callable[[], None], Optional[Callable[[], None]]]] = {}  # by timeout id
        self._fallback: dict[int, WebKit.Notification] = {}
        self._handlers: list[tuple[GObject.Object, int]] = []
        self._crashes: list[float] = []
        # WhatsApp's calls in the page: data channels done by GStreamer (rtc.py), when it's here.
        self.rtc: Optional[rtc.Host] = None
        self._reload_timer = 0
        self._link: Optional[str] = None  # while a link opens in the page: the chat that was open before
        self._link_timer = 0
        # Only the bridge knows this, so messages from anything else in the page (a frame, say) are ignored.
        self._token = secrets.token_hex(16)

        data, cache = DATA / "accounts" / self.id / "web", CACHE / "accounts" / self.id
        data.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        self.session = WebKit.NetworkSession.new(str(data), str(cache))
        self.session.set_persistent_credential_storage_enabled(False)
        cookies = self.session.get_cookie_manager()
        cookies.set_persistent_storage(str(data / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        cookies.set_accept_policy(WebKit.CookieAcceptPolicy.NO_THIRD_PARTY)
        self._connect(self.session, "download-started", self._on_download)

        content = WebKit.UserContentManager()
        allow = None if url else WHATSAPP_PAGES
        bridge = BRIDGE.replace("__NATTER_TOKEN__", self._token)
        content.add_script(WebKit.UserScript.new(bridge, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                 WebKit.UserScriptInjectionTime.START, allow, None))
        content.add_style_sheet(WebKit.UserStyleSheet.new(STYLE, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                          WebKit.UserStyleLevel.USER, allow, None))
        content.register_script_message_handler("natter", None)
        self._connect(content, "script-message-received::natter", self._on_message)
        if app.calls_in_page:
            self.rtc = rtc.Host(self._to_rtc_page, self._on_page_call)
            content.add_script(WebKit.UserScript.new(RTC.replace("__NATTER_TOKEN__", self._token),
                                                     WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                     WebKit.UserScriptInjectionTime.START, allow, None))
            content.register_script_message_handler("natterRtc", None)
            self._connect(content, "script-message-received::natterRtc", self._on_rtc_message)
            content.register_script_message_handler_with_reply("natterRtcCall", None)
            self._connect(content, "script-message-with-reply-received::natterRtcCall", self._on_rtc_call)

        settings = WebKit.Settings()
        _set(settings,
             enable_site_specific_quirks=True,
             enable_developer_extras=bool(GLib.getenv("NATTER_DEBUG")),
             enable_media_stream=True,
             enable_webrtc=True,
             enable_mediasource=True,
             enable_media_capabilities=True,
             enable_encrypted_media=False,
             enable_back_forward_navigation_gestures=False,
             enable_page_cache=False,
             enable_smooth_scrolling=True,
             enable_spatial_navigation=False,
             enable_write_console_messages_to_stdout=bool(GLib.getenv("NATTER_DEBUG")),
             # Natter's own paste (pictures, files) hands the page a DataTransfer, and Ctrl+V of text
             # is WebKit's paste command, so the page itself never needs to read the clipboard.
             javascript_can_access_clipboard=False,
             javascript_can_open_windows_automatically=False,
             media_playback_requires_user_gesture=False)
        settings.set_hardware_acceleration_policy(_acceleration(app.settings["hardware_acceleration"]))
        _features(settings)

        self.view = WebKit.WebView(
            web_context=app.web_context,
            network_session=self.session,
            user_content_manager=content,
            settings=settings,
            website_policies=WebKit.WebsitePolicies(autoplay=WebKit.AutoplayPolicy.ALLOW),
            vexpand=True,
            hexpand=True,
        )
        self.view.set_zoom_level(app.settings["zoom"])
        for signal, handler in (("notify::title", self._on_title),
                                ("decide-policy", self._on_policy),
                                ("permission-request", self._on_permission),
                                ("show-notification", self._on_webkit_notification),
                                ("web-process-terminated", self._on_crash),
                                ("load-failed", self._on_load_failed),
                                ("load-changed", self._on_load_changed),
                                ("context-menu", self._on_context_menu),
                                ("create", lambda *_: None)):
            self._connect(self.view, signal, handler)
        if app.settings["sw_cleared"]:
            self.view.load_uri(self.home)
        else:
            self._clear_old_offline_data()

    def _connect(self, target: GObject.Object, signal: str, handler: Callable) -> None:
        self._handlers.append((target, target.connect(signal, handler)))

    def _clear_old_offline_data(self) -> None:
        """Unregister the service worker WhatsApp installed before Natter turned them off (once), then load.

        Only its registration goes: the page itself may use the caches it filled (DOM_CACHE).
        """
        def cleared(manager, result) -> None:
            try:
                manager.clear_finish(result)
                self.app.settings["sw_cleared"] = True
            except GLib.Error as error:
                print(f"natter: couldn't remove WhatsApp's old service worker: {error.message}")
            if not self.removed:
                self.view.load_uri(self.home)

        self.session.get_website_data_manager().clear(
            WebKit.WebsiteDataTypes.SERVICE_WORKER_REGISTRATIONS, 0, None, cleared)

    def _trusted(self, uri: Optional[str] = None) -> bool:
        """Whether `uri` (by default the page's own) is WhatsApp Web, or the page given with --url."""
        if uri is None:
            uri = self.view.get_uri() or ""
        return is_whatsapp(uri) or bool(self.dev_url and same_site(uri, self.dev_url))

    # ---- Calls into the page ---------------------------------------------------------------

    def run(self, body: str, args: Optional[dict] = None, done: Optional[Callable] = None) -> None:
        """Run `body` (a JS function body) with `args` as local variables; `done(result)` gets the value.

        Only while the page is WhatsApp's: otherwise `done(None)`. The arguments travel as one JSON
        string, so a file's worth of them isn't pasted into the script's source.
        """
        if self.removed or not self._trusted():
            if done:
                GLib.idle_add(lambda: done(None) and False)
            return
        arguments = None
        if args:
            body = f"const {{{', '.join(args)}}} = JSON.parse(__natter_args);\n{body}"
            arguments = GLib.Variant("a{sv}", {"__natter_args": GLib.Variant("s", json.dumps(args))})

        def finished(view, result) -> None:
            value = None
            try:
                js = view.call_async_javascript_function_finish(result)
                if js is not None and not js.is_undefined() and not js.is_null():
                    value = json.loads(js.to_json(0) or "null")
            except GLib.Error as error:
                if GLib.getenv("NATTER_DEBUG"):
                    print(f"natter: page call failed: {error.message}")
            if done:
                done(value)

        self.view.call_async_javascript_function(body, -1, arguments, None, None, None, finished)

    def when_ready(self, then: Callable[[], None], otherwise: Optional[Callable[[], None]] = None,
                   timeout: float = READY_WAIT) -> None:
        """`then()` once WhatsApp's chat list is up.

        `otherwise()` instead if it isn't up within `timeout` seconds, if WhatsApp wants a phone
        linked first, or if the account goes.
        """
        if self.ready:
            then()
            return
        if self.removed or self.needs_link:
            if otherwise:
                otherwise()
            return

        def expire() -> bool:
            if self._pending.pop(timer, None) and otherwise:
                otherwise()
            return GLib.SOURCE_REMOVE

        timer = GLib.timeout_add(int(timeout * 1000), expire)
        self._pending[timer] = (then, otherwise)

    def _run_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for timer, (then, _otherwise) in pending.items():
            GLib.source_remove(timer)
            then()

    def _drop_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for timer, (_then, otherwise) in pending.items():
            GLib.source_remove(timer)
            if otherwise:
                otherwise()

    def open_chat(self, name: str, done: Optional[Callable[[bool], None]] = None) -> None:
        def go() -> None:
            self.run("return window.__natter ? await window.__natter.openChat(name) : false", {"name": name},
                     lambda ok: done(bool(ok)) if done else None)

        # The chat list learns the name from the bridge once the chat is open (the "active"
        # message), so a name that doesn't match any chat never lands in it.
        self.when_ready(go, (lambda: done(False)) if done else None)

    def click_notification(self, page_id: int, title: str) -> None:
        """Run the page's click handler for its notification `page_id`.

        If the page doesn't have it (it has reloaded since, say), the chat `title` opens instead.
        """
        if page_id < 0:
            notification = self._fallback.pop(page_id, None)
            if notification is not None:
                notification.clicked()
            else:
                self.open_chat(title)
            return

        def clicked(ok) -> None:
            if not ok:
                self.open_chat(title)

        self.run("return window.__natter ? window.__natter.clickNotification(id) : false", {"id": page_id}, clicked)

    def open_link(self, uri: str) -> bool:
        """Open a wa.me / whatsapp:// link, in the page if WhatsApp takes it there.

        The bridge clicks it the way one in a chat is clicked. If nothing in the page takes the
        click, or the chat doesn't open within LINK_WAIT seconds, the link's WhatsApp Web address
        is loaded instead, which reloads WhatsApp.
        """
        target = chat_link(uri)
        if target is None:
            return False
        if not self.ready or self._link is not None:
            self._load(target)
            return True
        # Calls wait while the link opens, as they would for a reload.
        self.ready = False
        self._link = self.active_chat

        def clicked(taken) -> None:
            if self._link is None:
                return  # the page reloaded meanwhile
            if taken:
                self._link_timer = GLib.timeout_add_seconds(LINK_WAIT, self._link_expired, target)
            else:
                self._load(target)

        self.run("return window.__natter ? window.__natter.openLink(href) : false", {"href": short_link(uri)}, clicked)
        return True

    def _link_expired(self, target: str) -> bool:
        self._link_timer = 0
        self._load(target)
        return GLib.SOURCE_REMOVE

    def _end_link(self) -> None:
        self._link = None
        if self._link_timer:
            GLib.source_remove(self._link_timer)
            self._link_timer = 0

    def attach(self, paths: list[Path], done: Optional[Callable[[bool], None]] = None) -> None:
        """Put files in the open chat, which brings up WhatsApp's preview with a caption box.

        WhatsApp's Content-Security-Policy keeps the page from fetching local files, so they go
        in through Natter's own calls, CHUNK bytes at a time, base64-encoded. The bridge checks
        each file arrived whole.
        """
        finish = (lambda ok: done(bool(ok))) if done else (lambda ok: None)
        items = []
        for n, path in enumerate(paths):
            try:
                size = path.stat().st_size
            except OSError:
                finish(False)
                return
            if size > LARGEST:
                finish(False)
                return
            kind = Gio.content_type_guess(str(path), None)[0]
            items.append({"id": f"{time.monotonic_ns()}-{n}", "name": path.name, "size": size,
                          "type": Gio.content_type_get_mime_type(kind) or "application/octet-stream"})

        def pieces():
            for item, path in zip(items, paths):
                with open(path, "rb") as handle:
                    while True:  # at least one piece, so an empty file arrives too
                        size = CALL_CHUNK if self.calling else CHUNK
                        data = handle.read(size)
                        yield item["id"], base64.b64encode(data).decode("ascii")
                        if len(data) < size:
                            break

        stream = pieces()

        def failed() -> None:
            self.run("if (window.__natter) window.__natter.discard(ids)", {"ids": [item["id"] for item in items]})
            finish(False)

        def step(taken=True) -> None:
            if not taken:
                failed()
                return
            try:
                piece = next(stream)
            except StopIteration:
                self.run("return window.__natter ? await window.__natter.attach(items) : false",
                         {"items": items}, finish)
                return
            except OSError:
                failed()
                return
            def next_piece(taken) -> None:
                if taken and self.calling:
                    GLib.timeout_add(CALL_GAP_MS, lambda: step(taken) and False)
                else:
                    step(taken)
            self.run("return window.__natter ? window.__natter.receive(id, chunk) : false",
                     {"id": piece[0], "chunk": piece[1]}, next_piece)

        step()

    def set_class(self, name: str, on: bool) -> None:
        self.run("if (window.__natter) window.__natter.setClass(name, on)", {"name": name, "on": on})

    def apply_privacy(self) -> None:
        self.set_class("natter-blur", self.app.settings["privacy_blur"])

    def set_hardware_acceleration(self, on: bool) -> None:
        self.view.get_settings().set_hardware_acceleration_policy(_acceleration(on))

    def reload(self) -> None:
        """Load WhatsApp again, also after a crash loop."""
        self.failed = None
        self.crashing = False
        self._crashes.clear()
        self._load(self.home)
        self.emit("state-changed")

    def _load(self, uri: str) -> None:
        if self._reload_timer:
            GLib.source_remove(self._reload_timer)
            self._reload_timer = 0
        self._end_link()
        self.ready = False
        self.view.load_uri(uri)

    def remove(self, done: Callable[[], None]) -> None:
        """Stop this account for good: its page closes and WebKit forgets the session. Then `done()`."""
        self.removed = True
        for target, handler in self._handlers:
            target.disconnect(handler)
        self._handlers.clear()
        if self.rtc:
            self.rtc.close()
            self.rtc = None
        if self._reload_timer:
            GLib.source_remove(self._reload_timer)
            self._reload_timer = 0
        self._end_link()
        self._drop_pending()
        self._fallback.clear()
        self.ready = False
        self.view.terminate_web_process()

        def cleared(manager, result) -> None:
            try:
                manager.clear_finish(result)
            except GLib.Error as error:
                print(f"natter: couldn't clear {self.name}'s data: {error.message}")
            done()

        self.session.get_website_data_manager().clear(WebKit.WebsiteDataTypes.ALL, 0, None, cleared)

    # ---- What the page tells Natter --------------------------------------------------------

    # ---- WebRTC for calls (rtc.js, rtc.py) ---------------------------------------------------

    def _rtc_message(self, value) -> Optional[dict]:
        if self.rtc is None or not self._trusted():
            return None
        try:
            raw = json.loads(value.to_json(0) or "null")
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.pop("token", None) != self._token:
            return None
        return raw

    def _on_rtc_message(self, _content, value) -> None:
        message = self._rtc_message(value)
        if message is not None:
            self.rtc.post(message)

    def _on_rtc_call(self, _content, value, reply) -> bool:
        message = self._rtc_message(value)
        if message is None:
            reply.return_error_message("Not available")
            return True
        context = value.get_context()

        def answer(result: dict) -> None:
            reply.return_value(JavaScriptCore.Value.new_from_json(context, json.dumps(result)))
        self.rtc.call(message, answer)
        return True

    @property
    def calling(self) -> bool:
        """A call is on in this account's page."""
        return self.rtc is not None and self.rtc.calling

    def _on_page_call(self, active: bool) -> None:
        # Hidden, WebKit runs the page's timers once a second; WhatsApp's call needs them on time.
        throttling = ("HiddenPageDOMTimerThrottling",)
        _features(self.view.get_settings(), off=throttling if active else (), on=() if active else throttling)
        self.emit("in-call", active)

    def _to_rtc_page(self, events: list) -> None:
        """Events for the page's RTCPeerConnections, in order (from rtc.Host)."""
        if self.removed or not self._trusted():
            return
        args = GLib.Variant("a{sv}", {"events": GLib.Variant("s", json.dumps(events))})
        self.view.call_async_javascript_function(
            "window.__natterRtc && window.__natterRtc.deliver(JSON.parse(events))",
            -1, args, None, None, None, None)

    def _on_message(self, _content, value) -> None:
        if not self._trusted():
            return
        try:
            raw = json.loads(value.to_json(0) or "null")
        except (TypeError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("token") != self._token:
            return
        message = parse_message(raw)
        if message is None:
            if GLib.getenv("NATTER_DEBUG"):
                print(f"natter: ignored a message from the page ({str(raw.get('type'))[:20]})")
            return
        kind = message["type"]
        if kind == "notify":
            self.emit("notify-message", message["id"], message["title"], message["body"], message["tag"],
                      message["icon"])
            self.chats.touch(message["title"])
        elif kind == "unnotify":
            self.emit("unnotify", message["id"])
        elif kind == "chats":
            if self.chats.add(message["rows"]):
                self.emit("chats-changed")
        elif kind == "active":
            changed = message["name"] != self.active_chat
            self.active_chat = message["name"]
            if self._link is not None and self.active_chat != self._link:
                self._end_link()  # the link opened its chat in the page
                self._set_ready(True)
            if self.active_chat and self.chats.touch(self.active_chat):
                self.emit("chats-changed")
            if changed:
                self.emit("state-changed")  # the window shows the call button only with a chat open
        elif kind == "ready":
            self._set_ready(message["ready"])
        elif kind == "state":
            needs_link = message["value"] == "needs-link"
            if needs_link != self.needs_link:
                self.needs_link = needs_link
                if needs_link:
                    self._drop_pending()
                self.emit("state-changed")
        elif kind == "call":
            self.emit("call-requested", message["video"])

    def _set_ready(self, ready: bool) -> None:
        self.ready = ready
        if ready:
            self.apply_privacy()
            self._run_pending()
        self.emit("state-changed")

    def _on_title(self, view, _pspec) -> None:
        count = unread_in(view.get_title() or "")
        if count != self.unread:
            self.unread = count
            self.emit("unread-changed")

    def _on_webkit_notification(self, _view, notification: WebKit.Notification) -> bool:
        # Only reached if the bridge didn't load: show it anyway, without a picture.
        page_id = -notification.get_id()
        self._fallback[page_id] = notification
        notification.connect("closed", lambda *_: self._fallback.pop(page_id, None))
        self.emit("notify-message", page_id, notification.get_title() or "", notification.get_body() or "",
                  notification.get_tag() or "", "")
        return True

    # ---- Navigation, permissions, downloads ------------------------------------------------

    def _on_policy(self, _view, decision, kind) -> bool:
        if kind in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri() or ""
            new_window = kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION
            clicked = action.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED or action.is_user_gesture()
            if new_window or (clicked and not is_whatsapp(uri) and not uri.startswith(("blob:", "data:", "about:"))):
                decision.ignore()
                if not (chat_link(uri) and self.open_link(uri)):
                    self._open_outside(uri)
                return True
        elif kind == WebKit.PolicyDecisionType.RESPONSE and decision.is_main_frame_main_resource():
            uri = decision.get_response().get_uri() or ""
            # A blob: address holds data the page made, and has the origin of the address inside it.
            blob = uri.startswith("blob:")
            if not (self._trusted(uri[len("blob:"):] if blob else uri) or uri.startswith("about:")):
                # Natter's bridge, permissions and file hand-off are for WhatsApp Web only, so what
                # comes from anywhere else (after a redirect, say) is neither shown nor saved. Nor is
                # it opened in the browser: a navigation the user asked for went there already, when
                # WebKit asked about it above (it asks again for each redirect, with the same click).
                decision.ignore()
                return True
            if blob or not decision.is_mime_type_supported():
                decision.download()  # saved, not shown in WhatsApp's place
                return True
        return False

    def _open_outside(self, uri: str) -> None:
        if urlparse(uri).scheme in OUTSIDE:
            Gtk.UriLauncher.new(uri).launch(self.view.get_root(), None, None, None)

    def _on_permission(self, _view, request) -> bool:
        allowed = (WebKit.NotificationPermissionRequest, WebKit.UserMediaPermissionRequest,
                   WebKit.DeviceInfoPermissionRequest, getattr(WebKit, "ClipboardPermissionRequest", ()))
        if isinstance(request, allowed) and self._trusted():
            request.allow()
        else:
            request.deny()
        return True

    def _on_download(self, _session, download: WebKit.Download) -> None:
        reported = []

        def destination(dl, suggested: str) -> bool:
            name = file_name(suggested)
            try:
                target = _free_path(self.app.settings.download_dir, name)
            except (OSError, TypeError, ValueError) as error:
                print(f"natter: can't save downloads in the chosen folder ({error}); using Downloads")
                try:
                    target = _free_path(_downloads_folder(), name)
                except OSError:
                    return False  # WebKit tries its default, and reports the failure
            dl.set_destination(str(target))
            return True

        def failed(dl, error: GLib.Error) -> None:
            reported.append(True)
            if not error.matches(WebKit.download_error_quark(), WebKit.DownloadError.CANCELLED_BY_USER):
                self.emit("download-done", dl.get_destination() or "", False, error.message)

        def finished(dl) -> None:
            if not reported and dl.get_destination():  # "finished" follows "failed" too
                self.emit("download-done", dl.get_destination(), True, "")

        download.connect("decide-destination", destination)
        download.connect("failed", failed)
        download.connect("finished", finished)

    # ---- Trouble ---------------------------------------------------------------------------

    def _on_load_changed(self, _view, event) -> None:
        # Not ready once a new page replaces the old one. (A load that never gets that far, a
        # download or a blocked navigation, leaves the old page, still ready, where it was.)
        if event == WebKit.LoadEvent.COMMITTED:
            self.ready = False
            self._end_link()
            if self.rtc:
                self.rtc.close()  # the old page's connections went with it
        elif event == WebKit.LoadEvent.FINISHED and self.failed is None:
            self.emit("state-changed")

    def _on_load_failed(self, _view, _event, uri: str, error: GLib.Error) -> bool:
        if error.matches(WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED) or \
           error.matches(WebKit.policy_error_quark(), WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False
        self.failed = error.message
        self.emit("state-changed")
        return True

    def _on_crash(self, _view, reason) -> None:
        self.ready = False
        self._end_link()
        if self.rtc:
            self.rtc.close()
        if reason == WebKit.WebProcessTerminationReason.TERMINATED_BY_API or self.removed:
            return
        self._crashes = self._crashes[-(LOOP_CRASHES + len(BACKOFF)):] + [time.monotonic()]
        delay = reload_delay(self._crashes)
        if delay is None:
            print(f"natter: {self.name}'s page stopped ({reason.value_nick}), "
                  f"{LOOP_CRASHES} times in {LOOP_SECONDS} s; not reloading")
            self.crashing = True
            self._drop_pending()
        else:
            print(f"natter: {self.name}'s page stopped ({reason.value_nick}); reloading in {delay} s")
            if self._reload_timer:
                GLib.source_remove(self._reload_timer)
            self._reload_timer = GLib.timeout_add_seconds(delay, self._reload_after_crash)
        self.emit("state-changed")

    def _reload_after_crash(self) -> bool:
        self._reload_timer = 0
        self._load(self.home)
        return GLib.SOURCE_REMOVE

    def _on_context_menu(self, _view, menu: WebKit.ContextMenu, hit: WebKit.HitTestResult) -> bool:
        drop = {WebKit.ContextMenuAction.OPEN_LINK_IN_NEW_WINDOW, WebKit.ContextMenuAction.OPEN_IMAGE_IN_NEW_WINDOW,
                WebKit.ContextMenuAction.OPEN_VIDEO_IN_NEW_WINDOW, WebKit.ContextMenuAction.OPEN_AUDIO_IN_NEW_WINDOW,
                WebKit.ContextMenuAction.OPEN_FRAME_IN_NEW_WINDOW, WebKit.ContextMenuAction.GO_BACK,
                WebKit.ContextMenuAction.GO_FORWARD, WebKit.ContextMenuAction.STOP, WebKit.ContextMenuAction.RELOAD,
                WebKit.ContextMenuAction.OPEN_LINK, WebKit.ContextMenuAction.DOWNLOAD_LINK_TO_DISK}
        for item in list(menu.get_items()):
            if item.get_stock_action() in drop:
                menu.remove(item)
        uri = (hit.get_link_uri() or "") if hit.context_is_link() else ""
        if urlparse(uri).scheme in OUTSIDE and not is_whatsapp(uri):
            action = Gio.SimpleAction.new("open-in-browser", None)
            action.connect("activate", lambda *_: self._open_outside(uri))
            menu.prepend(WebKit.ContextMenuItem.new_separator())
            menu.prepend(WebKit.ContextMenuItem.new_from_gaction(action, "Open Link in Browser", None))
        return False
