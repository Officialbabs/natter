"""One WhatsApp account: its own WebKit session (cookies, storage, cache) and page."""

from __future__ import annotations

import base64
import collections
import json
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
from gi.repository import Gio, GLib, GObject, Gtk, JavaScriptCore, WebKit

from . import NAME, WHATSAPP_URL, rtc, themes
from .links import chat_link, is_whatsapp, page_link, same_site, short_link, unread_in
from .media import KINDS as MEDIA_KINDS, refusal, refusals
from .settings import CACHE, DATA, ChatIndex
from .system import open_uri

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
PICK_TAG = "application/x-natter-files"  # the accept= of the bridge's own file input (bridge.js pick)
LARGEST = 256 * 1024 * 1024  # the most the piece-by-piece fallback sends (attach has no limit of its own)
READY_WAIT = 20  # seconds a call into the page waits for WhatsApp's chat list before giving up
LINK_WAIT = 4  # seconds a wa.me link opened in the page gets to open its chat before Natter loads it
# Sometimes WhatsApp's page arrives cut short (its head, no body, no app) and WebKit calls it
# loaded, or stops arriving and never finishes. Either way nothing comes of it, so Natter loads it
# again: a few times, not forever (_watch_page).
BLANK_CHECK = 3  # seconds after the page loads before Natter looks for WhatsApp's app in it
STALL_CHECK = 15  # seconds between looks at a page still loading
STALL_LIMIT = 45  # seconds a loading page may go without any progress (slow is fine, stopped isn't)
BLANK_TRIES, BLANK_WINDOW = 3, 10 * 60  # reloads of a blank or stalled page within this many seconds
# WebKit hands a page the microphone or camera again without asking Natter while it captures,
# and for about this long after its last capture ended (its idle watchdog then forgets).
GRANT_REUSE = 10 * 60

# Replies from notifications (send_reply, bridge.js reply).
REPLY_LIMIT = 4096  # UTF-16 code units, as the page counts them (reply_length): a quick reply, not an essay
REPLY_BUDGET_MS = 25000  # the page's own time to reach the chat and press Send; it never presses later
# How long Natter waits for the page's answer: the budget, WhatsApp emptying the box after the
# press (10 s), and slack. Then "unknown", and the page is left alone until it answers, or until
# REPLY_LET_GO, when Natter stops waiting for it at all.
REPLY_GUARD = 45
REPLY_LET_GO = 120
REPLY_QUEUE_WAIT = 60  # seconds a reply may wait behind another before it's answered "busy", untouched
REPLY_HELD = 8  # page actions kept for after a reply (_held), the latest
# WhatsApp's ids for people and groups, as its notifications' tags carry them: not channels,
# status, calls or anything else (bridge.js CHAT_ID).
CHAT_ID = re.compile(r"\d{5,24}(-\d{5,24})?@(c\.us|g\.us|lid)")
# What send_reply answers. Only "sent" withdraws anything; "unsure" and "unknown" may have been
# sent and are never tried again; "left-as-draft" is typed in the chat's message box, not sent.
# The rest typed nothing: "locked", "calling", "busy", "draft" (the chat has a draft of its own),
# "cant-send" (the chat takes no messages from them), "mismatch" (the chat that opened isn't that
# one), "gone" (no way to open it), "not-ready", "needs-link" (WhatsApp shows its link screen),
# "removed", "too-long" (or nothing to send).
REPLY_RESULTS = frozenset({"sent", "locked", "calling", "busy", "draft", "cant-send", "mismatch", "gone",
                           "not-ready", "needs-link", "removed", "too-long", "unsure", "unknown",
                           "left-as-draft"})
PAGE_REPLY_RESULTS = REPLY_RESULTS - {"locked", "needs-link", "removed", "unknown"}  # what the bridge answers
REPLY_SCRIPT = """if (!window.__natter || !window.__natter.reply) return {result: "not-ready", why: "no-bridge"};
const result = await window.__natter.reply(request);
return {result, why: request.why || ""};"""

# After a crash the page reloads after 1, 5, 30, then 120 s; a crash more than ten minutes
# after the one before counts as the first again. Three within two minutes is a crash loop:
# Natter stops reloading until asked to.
BACKOFF = (1, 5, 30, 120)
STEADY = 10 * 60
LOOP_CRASHES, LOOP_SECONDS = 3, 120
# A page ended for its memory gets a fresh one after a second, outside that count. Ended again
# within this long, it's the same again (a video still being added), so it stays down until
# Try Again.
MEMORY_AGAIN = 10 * 60

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
    if kind == "canary":
        # The bridge's selector check: which WHERE entries matched WhatsApp's live page.
        found, missing = message.get("found"), message.get("missing")
        if not (isinstance(found, list) and isinstance(missing, list)):
            return None
        if not all(isinstance(key, str) and 0 < len(key) <= 24 for key in found + missing):
            return None
        return {"type": kind, "found": found[:16], "missing": missing[:16]}
    if kind == "look":
        # WhatsApp's own look, as the bridge reads it (reportLook): whether its colour names are
        # there, whether they're dark, whether its dark class is on, and the style WebKit tells it.
        look = {"tokens": message.get("tokens"), "dark": message.get("dark"),
                "dark_class": message.get("darkClass"), "media": message.get("media")}
        return {"type": kind, **look} if all(isinstance(value, bool) for value in look.values()) else None
    return None


def reply_length(text: str) -> int:
    """`text`'s length as the page counts it (String.length: UTF-16 code units), for REPLY_LIMIT.
    An emoji counts two or more."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def reply_text(text) -> str:
    """A reply's words as they go to the page: line breaks as \\n. "" for what isn't text."""
    return text.replace("\r\n", "\n").replace("\r", "\n") if isinstance(text, str) else ""


def reply_answer(value) -> tuple[str, str]:
    """The bridge's answer to a reply (REPLY_SCRIPT), checked: (result, why). Anything else, or
    nothing (the page went, or the call failed), is "unknown": it may have been sent."""
    if not isinstance(value, dict) or value.get("result") not in PAGE_REPLY_RESULTS:
        return "unknown", "lost" if value is None else "odd-answer"
    why = value.get("why")
    return value["result"], why if isinstance(why, str) and re.fullmatch(r"[a-z-]{1,24}", why) else ""


@dataclass(eq=False)
class _Reply:
    """One reply asked for (Account.send_reply), from the queue to its answer."""

    page_id: int
    chat_id: str
    text: str
    done: Callable[[str], None]
    timer: int = 0  # waiting in the queue (REPLY_QUEUE_WAIT), then in the page (REPLY_GUARD, REPLY_LET_GO)
    in_page: bool = False  # the page has it and hasn't answered
    answered: bool = False


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


def attach_line(paths: list[Path], how: str = "") -> str:
    """The log line for files handed to WhatsApp (Account.attach): how many, how big, their
    types and how they came, never their names. What WhatsApp does with a file goes by its type
    and size: a video is decoded in the page, and one took the page to 12 GB (20 Oct 2026)."""
    size, types = 0, []
    for path in paths:
        try:
            size += Path(path).stat().st_size
        except OSError:
            pass
        kind = Gio.content_type_get_mime_type(Gio.content_type_guess(str(path), None)[0]) or "application/octet-stream"
        if kind not in types:
            types.append(kind)
    count = f"{len(paths)} file{'' if len(paths) == 1 else 's'}"
    return f"natter: attach: {count}, {size / 2 ** 20:.1f} MB, {', '.join(types)}, via {how or 'unknown'}"


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
        # WhatsApp asked for the microphone or camera (kind) and Natter said no: "off" for its
        # own switch, "desktop" for the desktop's answer about the camera (media.refusal).
        "media-refused": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # The page was ended for the memory it held (_on_memory_ended): reloaded, or stopped (True).
        "memory-ended": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
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
        self.reloading = False  # WhatsApp loads again (Reload, a fresh page, after a crash) until it's back
        self.crashing = False  # crashed again and again: not reloading until reload()
        self.memory_stopped = False  # crashing, because it was ended for its memory twice (_on_memory_ended)
        self.removed = False
        self.failed: Optional[str] = None
        self.dev_url = url
        self.home = url or WHATSAPP_URL
        self.chats = ChatIndex(DATA / "accounts" / self.id / "chats.json")
        self._pending: dict[int, tuple[Callable[[], None], Optional[Callable[[], None]]]] = {}  # by timeout id
        self._fallback: dict[int, WebKit.Notification] = {}
        self._handlers: list[tuple[GObject.Object, int]] = []
        self._crashes: list[float] = []
        self._memory_ends: list[float] = []  # time.monotonic() of each page ended for its memory
        # WhatsApp's calls in the page: data channels done by GStreamer (rtc.py), when it's here.
        self.rtc: Optional[rtc.Host] = None
        self._picked: Optional[list[str]] = None  # files waiting for WebKit to ask for them (attach)
        self.fresh_at = 0.0  # time.monotonic() of the last fresh page (fresh_page, a memory kill)
        self._reload_timer = 0
        self._page_timer = 0  # looks at a page that's loading or just loaded (_watch_page)
        self._progress = (-1.0, 0.0)  # the loading page's progress, and since when (_page_stalled)
        self._blank_reloads: list[float] = []
        self._link: Optional[str] = None  # while a link opens in the page: the chat that was open before
        self._link_timer = 0
        # Only the bridge knows this, so messages from anything else in the page (a frame, say) are ignored.
        self._token = secrets.token_hex(16)
        # The theme's style sheet on the page (apply_theme), and what the page last said of its own
        # look (the "look" message): None until it says, and again for each new page.
        self._theme_sheet: Optional[WebKit.UserStyleSheet] = None
        self._theme_css = ""
        self.theme_off = False  # the page doesn't draw with the colour names the theme sets (app.py)
        self.look: Optional[dict] = None
        self.look_dark: Optional[bool] = None  # Natter's style when the page said it (app.py)
        # The microphone and the camera, by kind (stop_media, media_allowed_again). WebKit reuses
        # what Natter allowed without asking again, until Natter itself loads a page: when Natter
        # last allowed each since its own last load, and when its capture last ended. Not cleared
        # when WhatsApp reloads itself, which leaves WebKit's grants as they were.
        self._allowed_at: dict[str, Optional[float]] = dict.fromkeys(MEDIA_KINDS)
        self._ended_at: dict[str, Optional[float]] = dict.fromkeys(MEDIA_KINDS)
        # Refused since the page last loaded. WebKit remembers a refusal until then, and refuses
        # a repeat that doesn't follow a click without asking Natter.
        self.refused_since_load = dict.fromkeys(MEDIA_KINDS, False)
        # The backstop (_on_capture_state) may end each kind's capture once, until it reads none.
        self._backstop = dict.fromkeys(MEDIA_KINDS, True)
        # Kinds switched off during a call whose grant WebKit may still reuse: the page loads
        # afresh once the call is over (stop_media, _on_page_call).
        self._fresh_after_call: set[str] = set()
        # WhatsApp's camera requests waiting on Natter's own question to the desktop (_on_media_request).
        self._held_media: list = []
        # Replies from notifications (send_reply): waiting their turn, the one whose turn it is,
        # how many the page has and hasn't answered, and what else would act on the page
        # meanwhile, held until it has (_held), a fresh page among them.
        self._replies: collections.deque[_Reply] = collections.deque()
        self._reply: Optional[_Reply] = None
        self._in_page = 0
        self._held_actions: list[Callable[[], None]] = []
        self._fresh_held = False

        data, cache = DATA / "accounts" / self.id / "web", CACHE / "accounts" / self.id
        data.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        self.session = WebKit.NetworkSession.new(str(data), str(cache))
        self.session.set_persistent_credential_storage_enabled(False)
        cookies = self.session.get_cookie_manager()
        cookies.set_persistent_storage(str(data / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        cookies.set_accept_policy(WebKit.CookieAcceptPolicy.NO_THIRD_PARTY)
        self._connect(self.session, "download-started", self._on_download)

        # Kept: the theme's style sheet is swapped in it while the page runs (apply_theme).
        self.content = content = WebKit.UserContentManager()
        self._allow = allow = None if url else WHATSAPP_PAGES
        bridge = BRIDGE.replace("__NATTER_TOKEN__", self._token)
        content.add_script(WebKit.UserScript.new(bridge, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                 WebKit.UserScriptInjectionTime.START, allow, None))
        content.add_style_sheet(WebKit.UserStyleSheet.new(STYLE, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                          WebKit.UserStyleLevel.USER, allow, None))
        self.apply_theme()
        content.register_script_message_handler("natter", None)
        self._connect(content, "script-message-received::natter", self._on_message)
        # rtc.js in every page: the ceilings on the page's video codecs, and their counts for the
        # log, are for every page. Its WebRTC stand-in only with calls in the page, which give it
        # the answering handler too.
        content.add_script(WebKit.UserScript.new(RTC.replace("__NATTER_TOKEN__", self._token),
                                                 WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                 WebKit.UserScriptInjectionTime.START, allow, None))
        content.register_script_message_handler("natterRtc", None)
        self._connect(content, "script-message-received::natterRtc", self._on_rtc_message)
        self._codec_log = None if app.calls_in_page else rtc.CodecLog(later=GLib.timeout_add)
        if app.calls_in_page:
            self.rtc = rtc.Host(self._on_page_call)
            content.register_script_message_handler_with_reply("natterRtcCall", None)
            self._connect(content, "script-message-with-reply-received::natterRtcCall", self._on_rtc_call)

        settings = WebKit.Settings()
        _set(settings,
             enable_site_specific_quirks=True,
             enable_developer_extras=bool(GLib.getenv("NATTER_DEBUG")),
             # Off only once the camera's sandbox anchor is gone (app._on_anchor_gone).
             enable_media_stream=not app.media_lost,
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
        if app.calls_in_page:
            # Devices listed by name before the first call (Natter grants WhatsApp the microphone
            # anyway): WhatsApp's microphone menu shows a headset from the start.
            _features(settings, off=("ExposeCaptureDevicesAfterCapture",), on=())

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
                                ("run-file-chooser", self._on_file_chooser),
                                ("query-permission-state", self._on_permission_query),
                                ("notify::camera-capture-state", self._on_capture_state),
                                ("notify::microphone-capture-state", self._on_capture_state),
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
            if self._held(go):
                return
            self.run("return window.__natter ? await window.__natter.openChat(name) : false", {"name": name},
                     lambda ok: done(bool(ok)) if done else None)

        # The chat list learns the name from the bridge once the chat is open (the "active"
        # message), so a name that doesn't match any chat never lands in it.
        self.when_ready(go, (lambda: done(False)) if done else None)

    def click_notification(self, page_id: int, title: str) -> None:
        """Run the page's click handler for its notification `page_id`.

        If the page doesn't have it (it has reloaded since, say), the chat `title` opens instead.
        """
        if self._held(lambda: self.click_notification(page_id, title)):
            return
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

    # ---- Replying from a notification ------------------------------------------------------
    # One reply is one act of the person's, so one send through WhatsApp's own page, the way
    # their hands would do it (bridge.js reply): never retried, never two at once in a page,
    # never begun while Natter is locked or a call is on. The words go to the page as arguments,
    # never in a script, and Natter's log has the outcome only: no names, ids or words.

    @property
    def replying(self) -> int:
        """Replies the page has and hasn't answered. Meanwhile nothing else acts on the page (_held)."""
        return self._in_page

    def send_reply(self, page_id: int, chat_id: str, text: str, done: Callable[[str], None]) -> None:
        """Send `text` to WhatsApp's chat `chat_id`, the one the page's notification `page_id`
        came from: WhatsApp's handler for that notification opens it (unless it's open already),
        and only if the chat that opens has that id do the words go into its message box, only
        if the box was empty, and Send is pressed once. `done(result)` later, once, with one of
        REPLY_RESULTS. One at a time in the page, in the order asked; the same words for the same
        chat asked for again while the first is still going are "busy" (one act, one send).
        """
        job = _Reply(page_id, chat_id, reply_text(text), done)
        refused = self._reply_refusal()
        if self.removed:
            self._answer(job, "removed")
        elif not (_page_id(page_id) and isinstance(chat_id, str) and CHAT_ID.fullmatch(chat_id)):
            self._answer(job, "gone", "not-a-chat")  # a call's, the WebKit fallback's, a status's
        elif not job.text.strip() or reply_length(job.text) > REPLY_LIMIT:
            self._answer(job, "too-long")
        elif refused:
            self._answer(job, refused)
        elif any(other.chat_id == job.chat_id and other.text == job.text for other in self._replies_asked()):
            self._answer(job, "busy", "repeat")
        else:
            self._replies.append(job)
            job.timer = GLib.timeout_add_seconds(REPLY_QUEUE_WAIT, self._reply_waited, job)
            self._next_reply()

    def _replies_asked(self) -> list[_Reply]:
        return ([self._reply] if self._reply is not None and not self._reply.answered else []) + list(self._replies)

    def _reply_refusal(self) -> Optional[str]:
        """Why no reply may begin now, at the moment the page would be touched; None if one may."""
        if self.app.locked:
            return "locked"
        if self.calling:
            return "calling"
        return None

    def _next_reply(self) -> None:
        if self._reply is not None or not self._replies:
            return
        job = self._reply = self._replies.popleft()
        if job.timer:
            GLib.source_remove(job.timer)
            job.timer = 0
        self.when_ready(lambda: self._start_reply(job), lambda: self._end_turn(
            job, "removed" if self.removed else "needs-link" if self.needs_link else "not-ready"))

    def _reply_waited(self, job: _Reply) -> bool:
        """REPLY_QUEUE_WAIT passed with `job` still behind another: it never reached the page."""
        job.timer = 0
        if job in self._replies:
            self._replies.remove(job)
            self._answer(job, "busy", "queue")
        return GLib.SOURCE_REMOVE

    def _start_reply(self, job: _Reply) -> None:
        # Checked again now: the lock or a call may have come on while it waited.
        refused = "removed" if self.removed else self._reply_refusal()
        if refused is None and not (self.ready and self._trusted()):
            refused = "not-ready"  # the page is somewhere else (an error page, say), or loading again
        if refused:
            self._end_turn(job, refused)
            return
        job.in_page = True
        self._in_page += 1
        self._apply_throttle()
        self.app.reclaimer.stop()  # nothing of the page's goes to swap under it
        job.timer = GLib.timeout_add_seconds(REPLY_GUARD, self._reply_guard, job)
        request = {"pageId": job.page_id, "chatId": job.chat_id, "text": job.text, "budgetMs": REPLY_BUDGET_MS,
                   "visible": self.view.get_mapped()}
        self.run(REPLY_SCRIPT, {"request": request}, lambda value: self._reply_answered(job, value))

    def _reply_answered(self, job: _Reply, value) -> None:
        result, why = reply_answer(value)
        if job.answered:  # after the guard: Natter has said "unknown" already
            print(f"natter: reply: {result}" + (f" ({why})" if why else "") + ", answered after the wait ran out",
                  flush=True)
        self._leave_page(job)
        self._end_turn(job, result, why)

    def _reply_guard(self, job: _Reply) -> bool:
        """The page hasn't answered in REPLY_GUARD: "unknown" (it may have sent). The page keeps it
        until it answers, or for REPLY_LET_GO in all, so nothing else switches chats under it."""
        if not job.answered:
            self._answer(job, "unknown", "late")
            job.timer = GLib.timeout_add_seconds(REPLY_LET_GO - REPLY_GUARD, self._reply_guard, job)
            return GLib.SOURCE_REMOVE
        job.timer = 0
        self._leave_page(job)
        self._end_turn(job, "unknown")
        return GLib.SOURCE_REMOVE

    def _leave_page(self, job: _Reply) -> None:
        if job.timer:
            GLib.source_remove(job.timer)
            job.timer = 0
        if job.in_page:
            job.in_page = False
            self._in_page -= 1
            self._apply_throttle()

    def _end_turn(self, job: _Reply, result: str, why: str = "") -> None:
        """`job` is over (answered `result` unless it was already): the next reply's turn, and
        then, with none in the page, what was held for it."""
        self._answer(job, result, why)
        if self._reply is job:
            self._reply = None
        self._next_reply()
        if not self._in_page:
            self._release_held()

    def _answer(self, job: _Reply, result: str, why: str = "") -> None:
        if job.answered:
            return
        job.answered = True
        print(f"natter: reply: {result}" + (f" ({why})" if why else ""), flush=True)
        GLib.idle_add(lambda: job.done(result) and False)

    def _held(self, run: Callable[[], None]) -> bool:
        """While the page has a reply, what else would act on it (open a chat, a link, files, a
        fresh page) waits until it has answered: switching chats between typing and Send would
        leave the reply as a draft. True if `run` waits. Checked when the page would be touched."""
        if not self._in_page:
            return False
        self._held_actions = (self._held_actions + [run])[-REPLY_HELD:]
        return True

    def _release_held(self) -> None:
        actions, self._held_actions = self._held_actions, []
        if self._fresh_held:
            # First: whatever was held then runs in the new page, once it's up.
            self._fresh_held = False
            self.fresh_page()
        for run in actions:
            run()

    def _drop_replies(self) -> None:
        """The account goes: every reply not yet in the page is answered "removed"."""
        self._held_actions, self._fresh_held = [], False
        while self._replies:
            job = self._replies.popleft()
            if job.timer:
                GLib.source_remove(job.timer)
                job.timer = 0
            self._answer(job, "removed")

    def _apply_throttle(self) -> None:
        """Hidden, WebKit runs the page's timers once a second at most. WhatsApp's calls need them
        on time, and so do replies (WhatsApp shows its Send button a timer tick after the words):
        throttling is off while either is on, so neither turns it back on under the other."""
        throttling = ("HiddenPageDOMTimerThrottling",)
        busy = self.calling or self._in_page > 0
        _features(self.view.get_settings(), off=throttling if busy else (), on=() if busy else throttling)

    def open_link(self, uri: str) -> bool:
        """Open a wa.me / whatsapp:// link, in the page if WhatsApp takes it there.

        The bridge clicks it the way one in a chat is clicked. If nothing in the page takes the
        click, or the chat doesn't open within LINK_WAIT seconds, the link's WhatsApp Web address
        is loaded instead, which reloads WhatsApp.
        """
        if not (page_link(uri) or chat_link(uri)):
            return False
        if self._held(lambda: self.open_link(uri)):
            return True
        shown = page_link(uri)
        if shown:
            self._open_in_page(shown)
            return True
        target = chat_link(uri)
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

    def _open_in_page(self, link: str) -> None:
        """A group invite or channel. Clicked in the page, WhatsApp shows its own preview (Join
        group, the channel). Before the page is up, or if the click isn't taken, WhatsApp Web's
        own address for it is loaded (it reads accept?code= and accept?channel_invite_code=):
        not the browser, whose landing page would only send it back to Natter."""
        code = link.rstrip("/").rsplit("/", 1)[-1]
        address = WHATSAPP_URL + ("accept?channel_invite_code=" if "/channel/" in link else "accept?code=") + quote(code)
        if not self.ready or self._link is not None:
            self._load(address)
            return

        def clicked(taken) -> None:
            if not taken:
                self._load(address)

        self.run("return window.__natter ? window.__natter.openLink(href) : false", {"href": link}, clicked)

    def _link_expired(self, target: str) -> bool:
        self._link_timer = 0
        self._load(target)
        return GLib.SOURCE_REMOVE

    def _end_link(self) -> None:
        self._link = None
        if self._link_timer:
            GLib.source_remove(self._link_timer)
            self._link_timer = 0

    def attach(self, paths: list[Path], done: Optional[Callable[[bool], None]] = None, how: str = "") -> None:
        """Put files in the open chat, which brings up WhatsApp's preview with a caption box (or
        adds them to the preview when it's already open). `how` they came is for the log
        (attach_line): "drop", "paste", "share" (the dock icon, `natter FILE…`) or "debug".

        The bridge clicks a file input of its own and WebKit asks which files (_on_file_chooser):
        WhatsApp then gets them straight from disk, whatever their size, with nothing copied
        through Natter or the page. Where WebKit doesn't ask, they go piece by piece (_send_copies).
        """
        finish = (lambda ok: done(bool(ok))) if done else (lambda ok: None)
        if not paths:
            finish(False)
            return
        if self._held(lambda: self.attach(paths, done, how)):
            return
        print(attach_line(paths, how), flush=True)
        self._picked = [str(Path(p).resolve()) for p in paths]  # plain paths: a file:// URI gives empty files

        def picked(result) -> None:
            if result is None:  # WebKit never asked: older WebKit, or the page wasn't ready
                self._picked = None
                self._send_copies(paths, finish)
            else:
                finish(result)
        self.run("return window.__natter ? await window.__natter.pick() : null", None, picked)

    def _on_file_chooser(self, _view, request) -> bool:
        if PICK_TAG not in (request.get_mime_types() or []):
            return False  # WhatsApp's own attach menu: the usual file dialog
        files, self._picked = self._picked, None
        if files and self._trusted():
            request.select_files(files)
        else:
            request.cancel()
        return True

    def _send_copies(self, paths: list[Path], finish: Callable[[bool], None]) -> None:
        """Files into the page piece by piece: CHUNK bytes at a time, base64-encoded, through
        Natter's own calls (WhatsApp's Content-Security-Policy keeps the page from fetching
        local files). The bridge checks each file arrived whole."""
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

    def apply_theme(self) -> None:
        """The theme on WhatsApp's page: a user style sheet that only redefines WhatsApp's own
        colour names (themes.page_css), never an element in the page. WebKit applies a change to
        the page as it is, without a reload, and keeps the sheet for every page after (a reload,
        a fresh page, a crash). Only the theme's own sheet comes out, never style.css, so privacy
        blur doesn't flicker. None for Plain, or while the page doesn't draw with those names
        (theme_off: app.py's page_look_changed). Custom's colour comes from its own setting."""
        settings = self.app.settings
        css = "" if self.theme_off else themes.page_css(settings["theme"], settings["theme_custom"])
        if css == self._theme_css:
            return
        if self._theme_sheet is not None:
            self.content.remove_style_sheet(self._theme_sheet)
            self._theme_sheet = None
        self._theme_css = css
        if css:
            self._theme_sheet = WebKit.UserStyleSheet.new(css, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                          WebKit.UserStyleLevel.USER, self._allow, None)
            self.content.add_style_sheet(self._theme_sheet)

    def set_hardware_acceleration(self, on: bool) -> None:
        self.view.get_settings().set_hardware_acceleration_policy(_acceleration(on))

    def fresh_page(self) -> None:
        """WhatsApp again in a new web process. After a call, WhatsApp keeps its call engine
        loaded: twenty threads and memory that WebKit can't give back (it only grows), well over
        a gigabyte. Only a new process lets it go. The chat that was open opens again. A page
        that stopped (crashing) stays stopped until Try Again. With a reply in the page, once it
        has answered (_held): a new page under it would turn "sent" into "unknown"."""
        if self.removed or self.calling or self.crashing:
            return
        if self._in_page:
            self._fresh_held = True
            return
        self.fresh_at = time.monotonic()
        self._reopen_after_load()
        if self.rtc:
            self.rtc.close()
        self.view.terminate_web_process()  # reported as TERMINATED_BY_API: not a crash
        self._load(self.home)

    def _reopen_after_load(self) -> None:
        """Open the chat that's open now again once the page is back."""
        chat = self.active_chat
        if chat:
            GLib.timeout_add_seconds(1, lambda: self.open_chat(chat) and False)

    def reload(self) -> None:
        """Load WhatsApp again, also after a crash loop."""
        self.failed = None
        self.crashing = self.memory_stopped = False
        self._crashes.clear()
        self._memory_ends.clear()
        self._load(self.home)
        self.emit("state-changed")

    def _load(self, uri: str) -> None:
        if self._reload_timer:
            GLib.source_remove(self._reload_timer)
            self._reload_timer = 0
        self._end_link()
        # A load Natter starts ends what WebKit reuses without asking (stop_media), what it
        # remembers refusing, and the requests the old page made.
        self._allowed_at = dict.fromkeys(MEDIA_KINDS)
        self._ended_at = dict.fromkeys(MEDIA_KINDS)
        self.refused_since_load = dict.fromkeys(MEDIA_KINDS, False)
        self._fresh_after_call = set()
        self._held_media = []
        self.ready = False
        self.look = self.look_dark = None  # the new page says again how it looks
        self._set_reloading(uri == self.home)  # not for a link opened in the page
        self.view.load_uri(uri)

    def _set_reloading(self, reloading: bool) -> None:
        if reloading != self.reloading:
            self.reloading = reloading
            self.emit("state-changed")

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
        self._watch_page(0, None)
        self._end_link()
        self._drop_replies()
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
        if not self._trusted():
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
        if message is None:
            return
        if self.rtc is not None:
            self.rtc.post(message)
        elif message.get("op") == "webcodecs" and self._codec_log is not None:  # all rtc.js sends without calls
            self._codec_log.take(message)

    def _on_rtc_call(self, _content, value, reply) -> bool:
        message = self._rtc_message(value) if self.rtc is not None else None
        if message is None:
            reply.return_error_message("Not available")
            return True
        context = value.get_context()
        if message.get("op") == "pull":  # the page waits for its next batch of events
            self.rtc.pull(reply, context)
            return True

        def answer(result: dict) -> None:
            reply.return_value(JavaScriptCore.Value.new_from_json(context, json.dumps(result)))
        self.rtc.call(message, answer)
        return True

    @property
    def calling(self) -> bool:
        """A call is on in this account's page."""
        return self.rtc is not None and self.rtc.calling

    def _on_page_call(self, active: bool) -> None:
        self._apply_throttle()
        self.emit("in-call", active)
        if not active and self._fresh_after_call:
            # A device switched off during the call: WebKit would still hand it out without asking.
            kinds, self._fresh_after_call = self._fresh_after_call, set()
            if any(self._media_blocked(kind) for kind in kinds):
                self.fresh_page()

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
        elif kind == "canary":
            if message["missing"]:
                print(f"natter: {self.name}: WhatsApp's page has changed: {', '.join(message['missing'])} "
                      f"not found; what relies on them is off until {NAME} catches up", flush=True)
                self.app.page_changed(message["missing"])
            elif GLib.getenv("NATTER_DEBUG"):
                print(f"natter: {self.name}: canary: all of WhatsApp's page found", flush=True)
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
            if needs_link:
                self._set_reloading(False)  # back, on the link screen (also after reloading on it)
            if needs_link != self.needs_link:
                self.needs_link = needs_link
                if needs_link:
                    self._drop_pending()
                self.emit("state-changed")
        elif kind == "call":
            self.emit("call-requested", message["video"])
        elif kind == "look":
            self.look = {key: message[key] for key in ("tokens", "dark", "dark_class", "media")}
            self.app.page_look_changed(self, fresh=True)

    def _set_ready(self, ready: bool) -> None:
        self.ready = ready
        if ready:
            self.reloading = False  # state-changed goes out below
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
            open_uri(uri, self.view.get_root())

    def _on_permission(self, _view, request) -> bool:
        if not self._trusted():
            request.deny()
            return True
        if isinstance(request, WebKit.UserMediaPermissionRequest):
            self._on_media_request(request)
            return True
        settings = self.app.settings
        allowed = (WebKit.NotificationPermissionRequest, getattr(WebKit, "ClipboardPermissionRequest", ()))
        if isinstance(request, allowed):
            request.allow()
        elif isinstance(request, WebKit.DeviceInfoPermissionRequest) and (settings["microphone"] or settings["camera"]):
            request.allow()  # device names (WebKit 2.52 never asks this any more)
        else:
            request.deny()
        return True

    def _on_media_request(self, request: WebKit.UserMediaPermissionRequest) -> None:
        """WhatsApp asks for the microphone, the camera, or both (screen sharing is neither, and
        these switches leave it alone). Natter's switches decide, and for the camera natively the
        desktop's answer too (media.refusals). What's refused is said (media-refused) and noted,
        for media_allowed_again; what's allowed is noted for stop_media.

        With nothing stored for the camera, the desktop asks when WebKit lists devices through
        its portal; but where WebKit can find the camera without it (app.camera_fallback), a
        listing that missed the portal never asked. So such a request waits while Natter asks
        the desktop itself (app.ask_camera), and only a yes lets it through."""
        audio = WebKit.user_media_permission_is_for_audio_device(request)
        video = WebKit.user_media_permission_is_for_video_device(request)
        gate = self.app.camera_gate()
        if video and gate == "ask" and self.app.camera_fallback and not self._media_refusals(gate, audio, video):
            self._held_media.append(request)
            self.app.ask_camera(lambda answer: self._asked_desktop(request, audio, video, answer))
            return
        self._answer_media(request, gate, audio, video)

    def _asked_desktop(self, request, audio: bool, video: bool, answer: Optional[str]) -> None:
        """The desktop answered Natter's question for a request that waited on it: a yes lets it
        through, unless something else refuses it by now; anything else refuses its camera."""
        if self.removed or request not in self._held_media:
            return  # the page has loaded again since, or gone: WebKit has let go of the request
        self._held_media.remove(request)
        gate = self.app.camera_gate()
        if answer != "yes":
            gate = "no"
        elif gate == "ask":
            gate = "yes"  # the store says so too, a moment later
        self._answer_media(request, gate, audio, video)

    def _media_refusals(self, gate: Optional[str], audio: bool, video: bool) -> list[tuple[str, str]]:
        settings = self.app.settings
        return refusals(bool(settings["microphone"]), bool(settings["camera"]), gate, audio, video)

    def _answer_media(self, request, gate: Optional[str], audio: bool, video: bool) -> None:
        refused = self._media_refusals(gate, audio, video)
        if refused:
            request.deny()
            for kind, reason in refused:
                self.refused_since_load[kind] = True
                self.emit("media-refused", kind, reason)
            if ("camera", "waiting") in refused:
                self.app.desktop_media.refresh()  # read the store now, not at its next try
            return
        now = time.monotonic()
        for kind, asked in (("microphone", audio), ("camera", video)):
            if asked:
                self._allowed_at[kind] = now
        request.allow()

    def _on_permission_query(self, _view, query) -> bool:
        """navigator.permissions.query(): what Natter would answer (see _on_permission), so the
        page lists devices by name and doesn't ask first. A device switched off, or a camera the
        desktop keeps from Natter, reads "denied", as a site blocked in a browser does, and the
        page sees one nameless device of that kind: not the camera's name and id, which WebKit
        lists even where the portal refused it (udev). That only changes what the page sees:
        refusing is _on_permission's job."""
        name = query.get_name()
        if name not in ("microphone", "camera", "notifications") or not self._trusted():
            return False
        if name in MEDIA_KINDS and self._media_blocked(name):
            query.finish(WebKit.PermissionState.DENIED)
        else:
            query.finish(WebKit.PermissionState.GRANTED)
        return True

    # ---- The microphone and the camera (app.py's switches) -------------------------------------
    # Only as a browser does for a site: WebKit's permission answers, its capture state, and a
    # page loaded afresh. Never MUTED for off: it keeps the page's tracks alive, and whether a
    # muted capture lets go of the device isn't known.

    def _capture_state(self, kind: str) -> WebKit.MediaCaptureState:
        return getattr(self.view, f"get_{kind}_capture_state")()

    def _end_capture(self, kind: str) -> None:
        # Its own notify can still read active: the backstop waits for "none" before acting again.
        self._backstop[kind] = False
        getattr(self.view, f"set_{kind}_capture_state")(WebKit.MediaCaptureState.NONE)

    def _media_blocked(self, kind: str) -> bool:
        return refusal(kind, bool(self.app.settings[kind]), self.app.camera_gate()) is not None

    def stop_media(self, kind: str) -> bool:
        """The microphone or camera (`kind`) was switched off, or the desktop said no to the
        camera: end WhatsApp's use of it now. True when it was capturing.

        While it captures, ending the capture ends the page's tracks of that kind, lets the
        device go, and makes WebKit forget what it allowed (both kinds: the other's next request
        is asked again). Once it reads none, that does nothing, yet WebKit would still hand the
        device out again without asking (_grant_lives). Only a load Natter starts ends that, so
        allowed since then, the page loads afresh (back on the chat that was open): now, or in a
        call once the call is over (_on_page_call). Meanwhile the backstop sees to anything
        reused (_on_capture_state)."""
        if self.removed:
            return False
        if self._capture_state(kind) != WebKit.MediaCaptureState.NONE:  # active or muted
            self._end_capture(kind)
            return True
        if self._grant_lives(kind):
            if self.calling:
                self._fresh_after_call.add(kind)
            else:
                self.fresh_page()
        return False

    def _grant_lives(self, kind: str) -> bool:
        """Whether WebKit may still hand out what Natter allowed of `kind` without asking. It
        forgets every grant GRANT_REUSE after the page last stopped capturing anything (its timer
        starts once neither kind captures), so not while either does, and never a grant no
        capture has ended since."""
        allowed = self._allowed_at[kind]
        if allowed is None:
            return False
        if any(self._capture_state(other) != WebKit.MediaCaptureState.NONE for other in MEDIA_KINDS):
            return True
        ended = max((at for at in self._ended_at.values() if at is not None), default=None)
        return ended is None or ended < allowed or time.monotonic() - ended < GRANT_REUSE

    def media_allowed_again(self, kind: str) -> None:
        """The microphone or camera (`kind`) may be used again. A request that follows a click
        (WhatsApp's call and camera buttons) reaches Natter at once. One that doesn't (a preview
        that starts by itself) WebKit keeps refusing while it remembers Natter's no, until the
        page loads again: so if Natter refused it since, and nobody's in a call, it reloads (not
        a page that stopped: that waits for Try Again)."""
        if (self.refused_since_load[kind] and not self.calling and not self.removed and not self.crashing
                and not self._media_blocked(kind)):
            self.reload()

    def _on_capture_state(self, _view, pspec) -> None:
        """The backstop. Should the page capture a kind that's off all the same (a grant WebKit
        reused, or one from before WhatsApp reloaded itself), end it: once, until it reads none,
        as ending it makes a notify that still reads active, and acting on that would loop."""
        kind = "camera" if pspec.name.startswith("camera") else "microphone"
        if self._capture_state(kind) == WebKit.MediaCaptureState.NONE:
            self._ended_at[kind] = time.monotonic()
            self._backstop[kind] = True
        elif self._backstop[kind] and self._media_blocked(kind):
            self._end_capture(kind)

    def lose_media(self) -> None:
        """No microphone or camera for WhatsApp's pages from now until Natter restarts
        (app._on_anchor_gone): WebKit's media streams off for every page loaded from here on.
        The page that's up keeps them, call and all: the portal named its web process while the
        anchor lived, and goes by that. A new web process (a fresh page, one after a crash) is
        what it can't name any more, and that page has no device listing to hang on."""
        self.view.get_settings().set_enable_media_stream(False)

    def devices_changed(self) -> None:
        """A microphone or speaker came or went, or the default changed: WhatsApp looks again
        (WebKit doesn't tell it when only the default changes, nor while the window's hidden)."""
        if self.ready:
            self.run("navigator.mediaDevices && navigator.mediaDevices.dispatchEvent(new Event('devicechange'))")

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
            changed = self.failed is not None or self.needs_link
            self.failed = None  # a page arrived after all (an automatic reload, a link): not offline
            self.needs_link = False  # the new page says again whether it shows the link screen
            self.look = self.look_dark = None  # and how it looks (WhatsApp may load itself again, as on logging out)
            # Any page load, WhatsApp's own too, ends what WebKit remembers refusing (not what
            # it allowed: _allowed_at stays, see stop_media).
            self.refused_since_load = dict.fromkeys(MEDIA_KINDS, False)
            if changed:
                self.emit("state-changed")
            self._end_link()
            if self.rtc:
                self.rtc.close()  # the old page's connections went with it
            self._progress = (-1.0, time.monotonic())
            self._watch_page(STALL_CHECK, self._page_stalled)
        elif event == WebKit.LoadEvent.FINISHED and self.failed is None:
            self._watch_page(BLANK_CHECK, self._check_blank)
            self.emit("state-changed")

    def _watch_page(self, seconds: int, then: Optional[Callable[[], bool]]) -> None:
        """Look at WhatsApp's page in `seconds` (replacing the look already planned); none if `then` is None."""
        if self._page_timer:
            GLib.source_remove(self._page_timer)
            self._page_timer = 0
        if then is not None and not self.removed and self._trusted():
            self._page_timer = GLib.timeout_add_seconds(seconds, then)

    def _page_stalled(self) -> bool:
        """While the page loads: has it moved on since the last look?"""
        if self.ready or self.needs_link or self.failed:
            self._page_timer = 0
            return GLib.SOURCE_REMOVE
        progress, since = self._progress
        now = self.view.get_estimated_load_progress()
        if now != progress:
            self._progress = (now, time.monotonic())
        elif time.monotonic() - since >= STALL_LIMIT:
            self._page_timer = 0
            self._reload_blank(f"stopped loading ({now:.0%} for {STALL_LIMIT} s)")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _check_blank(self) -> bool:
        self._page_timer = 0
        if not (self.ready or self.needs_link):
            self.run("return document.readyState === 'complete' && !document.getElementById('app')",
                     done=lambda blank: blank is True and self._reload_blank("came back empty"))
        return GLib.SOURCE_REMOVE

    def _reload_blank(self, what: str) -> None:
        if self.removed or self.ready or self.needs_link:
            return
        now = time.monotonic()
        self._blank_reloads = [at for at in self._blank_reloads if now - at < BLANK_WINDOW]
        if len(self._blank_reloads) >= BLANK_TRIES:
            print(f"natter: {self.name}'s page {what} again; leaving it (Reload tries again)", flush=True)
            self._set_reloading(False)  # Natter has stopped trying: the header shouldn't say it hasn't
            return
        self._blank_reloads.append(now)
        print(f"natter: {self.name}'s page {what}; loading it again", flush=True)
        self._load(self.home)

    def _on_load_failed(self, _view, _event, uri: str, error: GLib.Error) -> bool:
        if error.matches(WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED) or \
           error.matches(WebKit.policy_error_quark(), WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False
        self.failed = error.message
        self.reloading = False
        self._watch_page(0, None)  # the error page: Natter reloads when the network's back (app.py)
        self.emit("state-changed")
        return True

    def _on_crash(self, _view, reason) -> None:
        self.ready = False
        self._watch_page(0, None)  # the crash path below reloads it
        self._end_link()
        if self.rtc:
            self.rtc.close()
        if reason == WebKit.WebProcessTerminationReason.TERMINATED_BY_API or self.removed:
            return
        if reason == WebKit.WebProcessTerminationReason.EXCEEDED_MEMORY_LIMIT:
            self._on_memory_ended("WebKit")
            return
        # Natter's guard, and the kernel at the unit's limit, end a page with SIGKILL: to WebKit, a crash.
        by = self.app.ended_for_memory() if reason == WebKit.WebProcessTerminationReason.CRASHED else None
        if by:
            self._on_memory_ended(by)
            return
        self._crashes = self._crashes[-(LOOP_CRASHES + len(BACKOFF)):] + [time.monotonic()]
        delay = reload_delay(self._crashes)
        if delay is None:
            print(f"natter: {self.name}'s page stopped ({reason.value_nick}), "
                  f"{LOOP_CRASHES} times in {LOOP_SECONDS} s; not reloading")
            self.crashing = True
            self.reloading = False
            self._drop_pending()
        else:
            self.reloading = True  # from the moment it's known, not only once the load starts
            print(f"natter: {self.name}'s page stopped ({reason.value_nick}); reloading in {delay} s")
            if self._reload_timer:
                GLib.source_remove(self._reload_timer)
            self._reload_timer = GLib.timeout_add_seconds(delay, self._reload_after_crash)
        self.emit("state-changed")

    def _reload_after_crash(self) -> bool:
        self._reload_timer = 0
        self._load(self.home)
        return GLib.SOURCE_REMOVE

    def _on_memory_ended(self, by: str) -> None:
        """The page was ended for the memory it held, `by` WebKit at Natter's kill threshold
        (app.py MEMORY_KILL), usually for what a call left behind, or by Natter's guard or the
        kernel (memguard.py) for a page growing without end: a video's decoded frames took one to
        12 GB on 20 Oct 2026. Not a crash: a fresh page after a second, kept out of the
        crash-loop count, back on the chat that was open. Ended again within MEMORY_AGAIN, it
        would only do the same again, so it stays down until Try Again (memory_stopped)."""
        now = time.monotonic()
        self._memory_ends = [at for at in self._memory_ends if now - at < MEMORY_AGAIN] + [now]
        if self._reload_timer:
            GLib.source_remove(self._reload_timer)
            self._reload_timer = 0
        if len(self._memory_ends) > 1:
            print(f"natter: {self.name}'s page was ended for its memory ({by}), again within "
                  f"{MEMORY_AGAIN // 60} minutes; not reloading", flush=True)
            self.crashing = self.memory_stopped = True
            self.reloading = False
            self._drop_pending()
        else:
            print(f"natter: {self.name}'s page was ended for its memory ({by}); a fresh page", flush=True)
            self.fresh_at = now
            self._reopen_after_load()
            self._reload_timer = GLib.timeout_add_seconds(1, self._reload_after_crash)
            self.reloading = True
        self.emit("state-changed")
        self.emit("memory-ended", self.memory_stopped)

    def network_lost(self) -> None:
        """WebKit's network process died (app.py), and this page's database, websocket and loads
        with it. A reload in the same web process kept failing, so a new one, back on the chat
        that was open. Counted like a crash: one that keeps happening stops in a crash loop."""
        if self.removed or self.crashing:
            return
        self.ready = False
        self._end_link()
        if self.rtc:
            self.rtc.close()  # a call is over: its signalling went with the websocket
        self.fresh_at = time.monotonic()
        self._reopen_after_load()
        self.view.terminate_web_process()  # reported as TERMINATED_BY_API: not counted twice
        self._crashes = self._crashes[-(LOOP_CRASHES + len(BACKOFF)):] + [time.monotonic()]
        delay = reload_delay(self._crashes)
        if self._reload_timer:
            GLib.source_remove(self._reload_timer)
            self._reload_timer = 0
        if delay is None:
            print(f"natter: {self.name}'s page lost its connections {LOOP_CRASHES} times in "
                  f"{LOOP_SECONDS} s; not reloading", flush=True)
            self.crashing = True
            self.reloading = False
            self._drop_pending()
        else:
            self.reloading = True  # from the moment it's known, not only once the load starts
            self._reload_timer = GLib.timeout_add_seconds(delay, self._reload_after_crash)
        self.emit("state-changed")

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
