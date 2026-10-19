"""One WhatsApp account: its own WebKit session (cookies, storage, cache) and page."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, GObject, Gtk, WebKit

from . import WHATSAPP_URL
from .links import chat_link, is_whatsapp, unread_in
from .settings import CACHE, DATA, ChatIndex

HERE = Path(__file__).resolve().parent
BRIDGE = (HERE / "bridge.js").read_text()
STYLE = (HERE / "style.css").read_text()
WHATSAPP_PAGES = ["https://web.whatsapp.com/*"]
CHUNK = 4 * 1024 * 1024  # bytes per call when handing a file to the page
LARGEST = 256 * 1024 * 1024  # bigger than this: drag it into the window instead

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


class Account(GObject.Object):
    """A WhatsApp session in a WebView, and what Natter learns from it."""

    __gsignals__ = {
        "unread-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "chats-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "notify-message": (GObject.SignalFlags.RUN_FIRST, None, (int, str, str, str, str)),
        "unnotify": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "download-done": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app, info: dict, url: Optional[str] = None) -> None:
        super().__init__()
        self.app = app
        self.id: str = info["id"]
        self.name: str = info.get("name") or "WhatsApp"
        self.unread = 0
        self.active_chat = ""
        self.ready = False
        self.failed: Optional[str] = None
        self.home = url or WHATSAPP_URL
        self.chats = ChatIndex(DATA / "accounts" / self.id / "chats.json")
        self._pending: list[Callable[[], None]] = []
        self._fallback: dict[int, WebKit.Notification] = {}

        data, cache = DATA / "accounts" / self.id / "web", CACHE / "accounts" / self.id
        data.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        self.session = WebKit.NetworkSession.new(str(data), str(cache))
        self.session.set_persistent_credential_storage_enabled(False)
        cookies = self.session.get_cookie_manager()
        cookies.set_persistent_storage(str(data / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        cookies.set_accept_policy(WebKit.CookieAcceptPolicy.NO_THIRD_PARTY)
        self.session.connect("download-started", self._on_download)

        content = WebKit.UserContentManager()
        allow = None if url else WHATSAPP_PAGES
        content.add_script(WebKit.UserScript.new(BRIDGE, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                 WebKit.UserScriptInjectionTime.START, allow, None))
        content.add_style_sheet(WebKit.UserStyleSheet.new(STYLE, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                          WebKit.UserStyleLevel.USER, allow, None))
        content.register_script_message_handler("natter", None)
        content.connect("script-message-received::natter", self._on_message)

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
             javascript_can_access_clipboard=True,
             javascript_can_open_windows_automatically=False,
             media_playback_requires_user_gesture=False)
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
        self.view.connect("notify::title", self._on_title)
        self.view.connect("decide-policy", self._on_policy)
        self.view.connect("permission-request", self._on_permission)
        self.view.connect("show-notification", self._on_webkit_notification)
        self.view.connect("web-process-terminated", self._on_crash)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("context-menu", self._on_context_menu)
        self.view.connect("create", lambda *_: None)
        self.view.load_uri(self.home)

    # ---- Calls into the page ---------------------------------------------------------------

    def run(self, body: str, args: Optional[dict] = None, done: Optional[Callable] = None) -> None:
        """Run `body` (a JS function body) with `args` as local variables; `done(result)` gets the value."""
        if args:
            body = f"const {{{', '.join(args)}}} = {json.dumps(args)};\n{body}"

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

        self.view.call_async_javascript_function(body, -1, None, None, None, None, finished)

    def when_ready(self, then: Callable[[], None]) -> None:
        if self.ready:
            then()
        else:
            self._pending.append(then)

    def open_chat(self, name: str, done: Optional[Callable[[bool], None]] = None) -> None:
        def go() -> None:
            self.run("return window.__natter ? await window.__natter.openChat(name) : false", {"name": name},
                     lambda ok: done(bool(ok)) if done else None)

        self.chats.touch(name)
        self.when_ready(go)

    def click_notification(self, page_id: int, title: str) -> None:
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
        target = chat_link(uri)
        if target is None:
            return False
        self.ready = False
        self.view.load_uri(target)
        return True

    def attach(self, paths: list[Path], done: Optional[Callable[[bool], None]] = None) -> None:
        """Put files in the open chat, which brings up WhatsApp's preview with a caption box.

        WhatsApp's Content-Security-Policy keeps the page from fetching local files, so they go
        in through Natter's own calls, CHUNK bytes at a time, base64-encoded.
        """
        finish = (lambda ok: done(bool(ok))) if done else (lambda ok: None)
        items = []
        for n, path in enumerate(paths):
            try:
                if path.stat().st_size > LARGEST:
                    finish(False)
                    return
            except OSError:
                finish(False)
                return
            kind = Gio.content_type_guess(str(path), None)[0]
            items.append({"id": f"{time.monotonic_ns()}-{n}", "name": path.name,
                          "type": Gio.content_type_get_mime_type(kind) or "application/octet-stream"})

        def pieces():
            for item, path in zip(items, paths):
                with open(path, "rb") as handle:
                    while data := handle.read(CHUNK):
                        yield item["id"], base64.b64encode(data).decode("ascii")

        stream = pieces()

        def step(_result=None) -> None:
            try:
                piece = next(stream)
            except (StopIteration, OSError):
                self.run("return window.__natter ? await window.__natter.attach(items) : false",
                         {"items": items}, finish)
                return
            self.run("return window.__natter ? window.__natter.receive(id, chunk) : false",
                     {"id": piece[0], "chunk": piece[1]}, step)

        step()

    def set_class(self, name: str, on: bool) -> None:
        self.run("if (window.__natter) window.__natter.setClass(name, on)", {"name": name, "on": on})

    def apply_privacy(self) -> None:
        self.set_class("natter-blur", self.app.settings["privacy_blur"])

    def reload(self) -> None:
        self.failed = None
        self.ready = False
        self.view.load_uri(self.home)
        self.emit("state-changed")

    # ---- What the page tells Natter --------------------------------------------------------

    def _on_message(self, _content, value) -> None:
        try:
            message = json.loads(value.to_json(0))
        except (TypeError, ValueError):
            return
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "notify":
            self.emit("notify-message", int(message.get("id", 0)), str(message.get("title", "")),
                      str(message.get("body", "")), str(message.get("tag", "")), str(message.get("icon", "")))
            self.chats.touch(str(message.get("title", "")))
        elif kind == "unnotify":
            self.emit("unnotify", int(message.get("id", 0)))
        elif kind == "chats":
            rows = [(str(r[0]), int(r[1])) for r in message.get("rows", []) if isinstance(r, list) and len(r) == 2]
            if self.chats.add(rows):
                self.emit("chats-changed")
        elif kind == "active":
            self.active_chat = str(message.get("name", ""))
            if self.active_chat and self.chats.touch(self.active_chat):
                self.emit("chats-changed")
        elif kind == "ready":
            self.ready = bool(message.get("ready"))
            if self.ready:
                self.apply_privacy()
                pending, self._pending = self._pending, []
                for then in pending:
                    then()
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
                if chat_link(uri) and self.open_link(uri):
                    return True
                if urlparse(uri).scheme in ("http", "https", "mailto", "tel"):
                    Gtk.UriLauncher.new(uri).launch(self.view.get_root(), None, None, None)
                return True
        elif kind == WebKit.PolicyDecisionType.RESPONSE:
            if not decision.is_mime_type_supported() and decision.is_main_frame_main_resource():
                decision.download()
                return True
        return False

    def _on_permission(self, _view, request) -> bool:
        allowed = (WebKit.NotificationPermissionRequest, WebKit.UserMediaPermissionRequest,
                   WebKit.DeviceInfoPermissionRequest, getattr(WebKit, "ClipboardPermissionRequest", ()))
        if isinstance(request, allowed):
            request.allow()
        else:
            request.deny()
        return True

    def _on_download(self, _session, download: WebKit.Download) -> None:
        def destination(dl, suggested: str) -> bool:
            folder = self.app.settings.download_dir
            folder.mkdir(parents=True, exist_ok=True)
            name = Path(suggested or "download").name
            target = folder / name
            stem, suffix, n = target.stem, target.suffix, 1
            while target.exists():
                n += 1
                target = folder / f"{stem} ({n}){suffix}"
            dl.set_destination(str(target))
            return True

        download.connect("decide-destination", destination)
        download.connect("finished", lambda dl: dl.get_destination() and self.emit("download-done", dl.get_destination(), True))
        download.connect("failed", lambda dl, error: None)

    # ---- Trouble ---------------------------------------------------------------------------

    def _on_load_changed(self, _view, event) -> None:
        if event == WebKit.LoadEvent.STARTED:
            self.ready = False
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
        print(f"natter: {self.name}'s page stopped ({reason.value_nick}); reloading")
        GLib.timeout_add_seconds(1, lambda: self.reload() and False)

    def _on_context_menu(self, _view, menu: WebKit.ContextMenu, hit: WebKit.HitTestResult) -> bool:
        drop = {WebKit.ContextMenuAction.OPEN_LINK_IN_NEW_WINDOW, WebKit.ContextMenuAction.OPEN_IMAGE_IN_NEW_WINDOW,
                WebKit.ContextMenuAction.OPEN_VIDEO_IN_NEW_WINDOW, WebKit.ContextMenuAction.OPEN_AUDIO_IN_NEW_WINDOW,
                WebKit.ContextMenuAction.OPEN_FRAME_IN_NEW_WINDOW, WebKit.ContextMenuAction.GO_BACK,
                WebKit.ContextMenuAction.GO_FORWARD, WebKit.ContextMenuAction.STOP, WebKit.ContextMenuAction.RELOAD,
                WebKit.ContextMenuAction.OPEN_LINK, WebKit.ContextMenuAction.DOWNLOAD_LINK_TO_DISK}
        for item in list(menu.get_items()):
            if item.get_stock_action() in drop:
                menu.remove(item)
        if hit.context_is_link() and not is_whatsapp(hit.get_link_uri() or ""):
            uri = hit.get_link_uri()
            action = Gio.SimpleAction.new("open-in-browser", None)
            action.connect("activate", lambda *_: Gtk.UriLauncher.new(uri).launch(self.view.get_root(), None, None, None))
            menu.prepend(WebKit.ContextMenuItem.new_separator())
            menu.prepend(WebKit.ContextMenuItem.new_from_gaction(action, "Open Link in Browser", None))
        return False

