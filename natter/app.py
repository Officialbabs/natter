"""The Natter application: one instance, one window, and everything that lives outside it."""

from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Iterable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, WebKit

from . import APP_ID, DEFAULT_APP_ID, NAME, OLD_APP_ID, VERSION, WHATSAPP_URL
from . import clipboard, lock as applock, media, rtc, themes
from .account import Account
from .media import KINDS as MEDIA_KINDS, DesktopMedia
from .ada import AdaStyle, on_ada
from .calls import CallWindow, available as calls_available
from .distro import os_release
from .links import chat_link, page_link
from .launcher import Badge, chat_key, exec_quote, set_menu_chats
from .notify import Notifier
from .search import SearchProvider
from .settings import AUTOSTART, CACHE, CONFIG, DATA, Chat, ChatIndex, Settings, private_folder
from .system import (SWAP_BUDGET_MB, AudioFollower, CallSound, Reclaimer, footprint, in_sandbox, limit_own_scope,
                     media_report, memory_tight, move_to_own_scope, network_processes, open_uri, own_swap,
                     request_background, swap_full)
from .tray import MenuItem, Tray, tray_host_available

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent / "bin" / "natter"  # in a checkout


def icons_folder() -> Path:
    """Natter's icons: the checkout's data/icons, or the icons folder meson installed them to."""
    svg = Path("hicolor", "scalable", "apps", f"{DEFAULT_APP_ID}.svg")
    for folder in (HERE.parent / "data" / "icons", *(Path(d) / "icons" for d in GLib.get_system_data_dirs())):
        if (folder / svg).is_file():
            return folder
    return HERE.parent / "data" / "icons"


ICONS = icons_folder()


def app_icon() -> Gio.Icon:
    """Natter's icon for its own notifications: the file itself, which every notification server
    can show (Ada's doesn't look icons up by name in ~/.local/share/icons), else the theme name."""
    svg = ICONS / "hicolor" / "scalable" / "apps" / f"{DEFAULT_APP_ID}.svg"
    return Gio.FileIcon.new(Gio.File.new_for_path(str(svg))) if svg.is_file() else Gio.ThemedIcon.new(DEFAULT_APP_ID)
MEMORY_LIMIT = 1536  # MB
MEMORY_THRESHOLDS = (0.55, 0.8)  # conservative, strict: fractions of the limit
MEMORY_POLL = 30  # seconds between WebKit's memory checks
# Past this many times the limit, WebKit ends the page and Natter starts a fresh one (a plan,
# not a crash: Account._on_crash). Without it the limit only ever freed caches, while what
# WhatsApp's call engine keeps after a call grew on to several gigabytes. 2.5 leaves room for a
# call on top of an idle page (about 1.2 GB): ending the page during a call ends the call.
MEMORY_KILL = 2.5
MEMORY_CHECK = 60  # seconds between Natter's own checks (_check_memory)
NETWORK_CHECK = 5  # seconds between looks at WebKit's network process (_check_network_process)
# Every second at first: WhatsApp's busiest start is when a dying network process is likeliest,
# and one that dies and is replaced between two looks would go unnoticed.
NETWORK_CHECK_EARLY, NETWORK_EARLY_LOOKS = 1, 120
# Looks that come this much later than due mean Natter was frozen (Ada pauses hidden apps,
# sometimes seconds after they hide). Thawed, it gets a moment to give memory back before the
# freeze, and the swapping that comes with it, can blame it again (_thawed).
THAW_GAP = 20
FRESH_AGAIN = 30 * 60  # seconds before the same account gets another fresh page for memory
HIDDEN_BEFORE_RECLAIM = 90  # seconds hidden before Natter gives memory back
FRESH_AFTER_CALL = 120  # seconds hidden after a call before WhatsApp gets a fresh page (Account.fresh_page)
RECLAIM_AGAIN = 15 * 60  # and again this often while it stays hidden
SEARCH_LINGER = 30_000  # ms a Natter started only for desktop search stays after the last search
MEDIA_CHECK_DELAY = 10  # seconds after WhatsApp is up, so the check doesn't slow its start
SNAPSHOTS = CACHE / "snapshots"  # the only folder debug snapshots are saved to
CALLS_START_DELAY = 15  # seconds after WhatsApp is up before the call window starts, to ring for calls
CALL_FAILURES = {
    "no-chat": "The call window couldn't find “{chat}” in its chats",
    "no-call-button": "WhatsApp doesn't offer a call for “{chat}”",
    "start": "The call window couldn't start",
}
CALL_FAILURES_IN_PAGE = {
    "no-chat": "Open a chat first, then call",
    "no-call-button": "WhatsApp doesn't offer a call here",
    "unavailable": "WhatsApp doesn't offer calls here yet",
}
APPEARANCES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT,
               "dark": Adw.ColorScheme.FORCE_DARK}
# Under Preferences → Style when WhatsApp has been set to a light or dark look of its own, in its
# settings or synced from another device: Natter only ever asks it to follow the system.
def style_hint(dark: bool) -> str:
    """Says which look WhatsApp kept and where to change it: its own Theme setting, inside WhatsApp,
    not the Theme row this hint sits next to. `dark` is Natter's style, so WhatsApp's is the other."""
    return (f"WhatsApp is set to {'Light' if dark else 'Dark'} in its own settings. To match {NAME}, choose "
            "“System default” for WhatsApp’s theme there.")
# Do Not Disturb's choices, in the order the chooser lists them: seconds, or None for until
# turned back on. The first is picked when it opens.
DO_NOT_DISTURB = (("For 1 Hour", 3600), ("For 2 Hours", 7200), ("Until Turned Back On", None))
# The Preferences pages the preferences-page action opens (prefs.py names them). Anything on the
# session bus can call the action, so other names are ignored rather than passed on.
PREFERENCES_PAGES = ("general", "notifications", "privacy", "accounts")
# udev's notes on each device, which WebKit's own way of finding cameras needs (_make_web_context).
UDEV_DATA = "/run/udev/data"
# What a switch turned off says when WhatsApp was using that device (_apply_media).
MEDIA_STOPPED = {"microphone": "Microphone turned off. WhatsApp can't hear you",
                 "camera": "Camera turned off. WhatsApp's video has stopped"}
WEBSITE = "https://github.com/Officialbabs/natter"
# The About dialog's "What's New", for VERSION: the newest <release> in the metainfo says the same.
RELEASE_NOTES = (f"<p>First release as {DEFAULT_APP_ID}, with a Flatpak, and support for GNOME, KDE Plasma, "
                 "Xfce, Sway and Ada.</p>")


def color_scheme(appearance, ada_dark: Optional[bool]) -> Adw.ColorScheme:
    """The style Natter asks libadwaita for. Following the system (what Preferences shows for
    "system" and for anything it doesn't know) means following Ada's own setting on Ada (ada.py),
    which never reaches GTK apps; elsewhere, or when Ada's file doesn't say, libadwaita follows
    the desktop itself."""
    scheme = APPEARANCES.get(appearance, Adw.ColorScheme.DEFAULT) if isinstance(appearance, str) \
        else Adw.ColorScheme.DEFAULT
    if scheme == Adw.ColorScheme.DEFAULT and ada_dark is not None:
        return Adw.ColorScheme.FORCE_DARK if ada_dark else Adw.ColorScheme.FORCE_LIGHT
    return scheme


def page_look(look: dict, dark: bool) -> str:
    """What WhatsApp's page says of its own look (account.py's "look" message), for Natter's window
    in the dark style or not: "broken" when WhatsApp's colour names are gone or its dark class
    doesn't follow them, so the theme can't follow WhatsApp either; "media" when WebKit tells the
    page another style than the window's (GTK_THEME=…:dark, a GTK theme ending in -dark); "own"
    when WhatsApp has a light or dark look of its own; otherwise ""."""
    if not look["tokens"] or look["dark"] != look["dark_class"]:
        return "broken"
    if look["media"] != dark:
        return "media"
    return "own" if look["dark"] != dark else ""


def awake_time() -> float:
    """Seconds on a clock that keeps counting while the computer sleeps (CLOCK_BOOTTIME), so a
    night with the lid closed counts as idle; the monotonic clock stops then."""
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


def memory_pressure(limit: Optional[str], thresholds: Optional[str]) -> Optional[tuple[int, float, float]]:
    """WebKit's memory limit in MB and its conservative and strict thresholds, from
    NATTER_MEMORY_LIMIT and NATTER_MEMORY_THRESHOLDS ("0.55,0.8"). None keeps WebKit's own."""
    try:
        megabytes = int(limit) if limit else MEMORY_LIMIT
    except ValueError:
        megabytes = MEMORY_LIMIT
    if megabytes <= 0:
        return None
    try:
        conservative, strict = (float(part) for part in (thresholds or "").split(","))
    except ValueError:
        conservative, strict = MEMORY_THRESHOLDS
    if not 0 < conservative < strict < 1:
        conservative, strict = MEMORY_THRESHOLDS
    return megabytes, conservative, strict


def find_chat(key: str, indexes: Iterable[tuple[str, ChatIndex]]) -> Optional[tuple[str, str]]:
    """The (account id, chat name) a dock menu's --chat-id stands for, if Natter still knows the chat."""
    for account_id, index in indexes:
        for chat in index.recent():
            if chat_key(account_id, chat.name) == key:
                return account_id, chat.name
    return None


def launcher_path(argv0: Optional[str] = None) -> str:
    """The `natter` command for the login entry: the one that started this Natter
    (~/.local/bin/natter, /usr/bin/natter…), else the one on PATH, else the checkout's."""
    started = os.path.abspath(argv0 if argv0 is not None else (sys.argv[0] if sys.argv else ""))
    if os.path.basename(started) == "natter" and os.path.isfile(started) and os.access(started, os.X_OK):
        return started
    return shutil.which("natter") or str(LAUNCHER)


def old_natter_running(bus: Optional[Gio.DBusConnection], app_id: str = APP_ID) -> bool:
    """Whether Natter from before the rename (io.natter.Natter) is running. It keeps its WhatsApp
    session in the same folders, and two WebKits writing one session can damage it."""
    if app_id != DEFAULT_APP_ID or bus is None:
        return False
    try:
        return bool(bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "NameHasOwner", GLib.Variant("(s)", (OLD_APP_ID,)), GLib.VariantType("(b)"),
                                  Gio.DBusCallFlags.NONE, 1000, None).unpack()[0])
    except GLib.Error:
        return False


def autostart_exec(launcher: str, app_id: str = APP_ID) -> str:
    """The Exec= line that starts this Natter hidden at login."""
    command = f"{exec_quote(launcher)} --background"
    if app_id != DEFAULT_APP_ID:
        command = f"env {exec_quote('NATTER_APP_ID=' + app_id)} {command}"
    return command


def dev_url(url: Optional[str], remote: bool, app_id: str = APP_ID) -> tuple[Optional[str], str]:
    """The page --url asks for, if this start may load it, and otherwise a warning. Only a lab
    instance (its own NATTER_APP_ID) may, on the command line that starts it: the Natter that
    holds your WhatsApp session only ever shows WhatsApp."""
    if not url:
        return None, ""
    if app_id == DEFAULT_APP_ID:
        return None, "natter: --url ignored: only a separate instance (NATTER_APP_ID=…) loads another page"
    if remote:
        return None, f"natter: --url ignored: {NAME} is already running, and only takes it when it starts"
    return url, ""


def debug_token(app_id: str = APP_ID) -> str:
    """The secret NATTER_DEBUG's actions want in front of their argument. Anything on the session
    bus can call them, and Natter's own instance holds your WhatsApp; a lab instance needs none."""
    return secrets.token_hex(16) if app_id == DEFAULT_APP_ID else ""


def debug_argument(value: str, token: str) -> Optional[str]:
    """A debug action's argument with "<token>:" taken off the front; None when that isn't there."""
    if not token:
        return value
    prefix = token + ":"
    if not secrets.compare_digest(value[:len(prefix)].encode(), prefix.encode()):
        return None
    return value[len(prefix):]


def snapshot_path(path: str, folder: Path = SNAPSHOTS) -> Optional[Path]:
    """Where a debug snapshot asked for as `path` is saved: inside `folder` (a bare name goes
    there), else None."""
    inside = folder.resolve()
    target = (inside / path).resolve()
    return target if target != inside and target.is_relative_to(inside) else None


def banner_for(dnd_until, media_missing: bool, now: float) -> Optional[tuple[str, str, str, str]]:
    """What the window's banner says, as (title, button, action, page), else None. Only states
    that last go there, one at a time: paused notifications first, as they're the one that keeps
    messages from you, then media that can't play. `page` is the action's target, if it takes one."""
    if isinstance(dnd_until, (int, float)) and dnd_until != 0 and (dnd_until < 0 or dnd_until > now):
        if dnd_until < 0:
            return "Notifications paused", "Resume", "app.resume-notifications", ""
        until = time.strftime("%H:%M", time.localtime(dnd_until))
        return f"Notifications paused until {until}", "Resume", "app.resume-notifications", ""
    if media_missing:
        return "Some videos and voice messages can't play", "Details", "app.preferences-page", "general"
    return None


def media_debug(kind: str, state: dict, fallback: bool = False) -> str:
    """The debug info's line for the microphone or the camera, from App.media_state: NAME's
    switch, and for the camera what the desktop says, whether there's one, and whether WebKit's
    own way of finding it (udev, _make_web_context) is on. A camera the desktop blocks while
    the switch is on is the mismatch a bug report should show."""
    def word(value: Optional[bool]) -> str:
        return "unknown" if value is None else "yes" if value else "no"

    line = f"{kind.capitalize()}: switch {'on' if state['on'] else 'off'}"
    if kind != "camera":
        return line
    present = "no (restart needed)" if state.get("restart") else word(state["present"])
    line += f", desktop {state['desktop'] or 'unknown'}, present {present}, fallback {'on' if fallback else 'off'}"
    return line + (", turned off for all apps" if state["lockdown"] else "")


def debug_info(calls: str, missing: Optional[list[str]], sandboxed: bool, devices: Iterable[str] = ()) -> str:
    """The About dialog's troubleshooting text, for bug reports: versions and the desktop, never
    anything from WhatsApp. `missing` is None while the codec check hasn't answered; `devices`
    are media_debug's lines."""
    def version(library) -> str:
        return f"{library.get_major_version()}.{library.get_minor_version()}.{library.get_micro_version()}"

    if missing is None:
        codecs = "not checked yet"
    else:
        codecs = ", ".join(missing) if missing else "nothing missing"
    return "\n".join([
        f"{NAME} {VERSION} ({APP_ID})",
        f"GTK {version(Gtk)}",
        f"libadwaita {version(Adw)}",
        f"WebKitGTK {version(WebKit)}",
        f"PyGObject {gi.__version__}",
        f"Python {platform.python_version()}",
        f"System: {os_release().get('PRETTY_NAME') or 'unknown'}",
        f"Desktop: {GLib.getenv('XDG_CURRENT_DESKTOP') or 'unknown'} ({GLib.getenv('XDG_SESSION_TYPE') or 'unknown'})",
        f"Sandboxed (Flatpak or Snap): {'yes' if sandboxed else 'no'}",
        f"Calls: {calls}",
        f"Codecs for videos and voice messages: {codecs}",
        *devices,
    ]) + "\n"


class Natter(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.set_application_name(NAME)
        # Asked before Settings, which may write the file: an upgrade keeps its settings as they are.
        self._new_install = not (CONFIG / "settings.json").exists()
        self.settings = Settings()
        self.accounts: list[Account] = []
        self.window = None
        self.tray: Optional[Tray] = None
        self.tray_host: Optional[bool] = None
        self.dev_url: Optional[str] = None
        self.downloaded: set[str] = set()  # what open-file may open: Natter's own downloads
        self._held = False
        self._reclaim_timer = 0
        self._after_call: set = set()  # accounts that had a call since their page started
        # The app lock (lock.py): while locked, the window shows only the passcode, and what
        # would show a chat waits for the unlock.
        self.locked = False
        self._attempts = applock.Attempts(self.settings)
        self._after_unlock: Optional[Callable[[], None]] = None
        self._last_activity = awake_time()
        self._fresh_timer = 0
        self._media: Optional[tuple[list[str], str]] = None
        self._media_waiting: list[Callable[[list[str], str], None]] = []
        self._media_checked = False
        self._media_banner = False  # some media can't play, and Preferences hasn't been opened since
        self._told: set[str] = set()  # one-time notices already given this session
        self._linking: set[str] = set()  # accounts whose "needs linking" was seen or announced
        self._tray_menu_shown: list[tuple] = []
        self.calls: Optional[CallWindow] = None
        # Calls made by WhatsApp in Natter's own page, its data channels done by GStreamer (rtc.py).
        self.calls_in_page = False
        self._pending_call: Optional[tuple[str, bool]] = None  # (chat, video) waiting for the call window
        self.ada_style: Optional[AdaStyle] = None  # Ada's own light or dark, on Ada (do_startup)
        # The line under Preferences → Style while WhatsApp has a look of its own (_show_style_hint),
        # and the Preferences dialog that's open, which shows it too.
        self.style_hint = ""
        self.preferences = None
        # The microphone and the camera (media.py): what the desktop says about the camera, once
        # the window's services start; whether WebKit may find the camera itself (udev); whether
        # calls lost their devices for this run (_on_anchor_gone); whether there's a microphone;
        # and what wants to hear of any change (watch_media).
        self.desktop_media: Optional[DesktopMedia] = None
        self.camera_fallback = False
        self.media_lost = False
        self.microphone_present: Optional[bool] = None
        self._media_watchers: dict[int, Callable[[], None]] = {}
        self._media_watch_next = 0
        self._asking_camera = False
        self._camera_gate_was: Optional[str] = None  # the desktop's answer as Natter last acted on it
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start in the background, without opening the window", None)
        self.add_main_option("chat", ord("c"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open the chat with this name", "NAME")
        self.add_main_option("account", ord("a"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Which account --chat is in (its id)", "ID")
        self.add_main_option("chat-id", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open a chat from the dock menu", "KEY")
        self.add_main_option("url", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Load this page instead of WhatsApp Web (for development)", "URL")
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Show the version", None)
        self.add_main_option(GLib.OPTION_REMAINING, 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING_ARRAY,
                             "wa.me links, whatsapp:// links, or files to send", "[LINK|FILE…]")
        self.connect("handle-local-options", self._local_options)

    def _local_options(self, _app, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"{NAME} {VERSION}")
            return 0
        return -1

    # ---- Start-up --------------------------------------------------------------------------

    def do_startup(self) -> None:
        # Only the first instance gets here. Group Natter's processes under a scope of its
        # own before WebKit starts any (Ada's launcher already makes one; a terminal doesn't).
        self.scope = move_to_own_scope()
        if self.scope is None:
            limit_own_scope()  # a launcher's scope (Ada's): the same backstop as Natter's own
        Adw.Application.do_startup(self)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(ICONS))
        Gtk.Window.set_default_icon_name(DEFAULT_APP_ID)  # a separate instance (NATTER_APP_ID) has no icon of its own
        if on_ada():
            # Ada's Light, Dark or Dune, which "Follow System" follows there, changing along with it.
            self.ada_style = AdaStyle(on_change=self._apply_appearance)
        self._apply_appearance()
        self._start_theme()
        # Decided now however Natter was started (D-Bus starts it as a service for a search, and
        # for its menu entry on some desktops): WebKit's processes, started later, need it set.
        self.calls_in_page = GLib.getenv("NATTER_CALLS") != "window" and rtc.available()
        self._make_web_context()
        self._apply_spelling()

        bus = self.get_dbus_connection()
        self.notifier = Notifier(self, self._open_from_notification)
        self.badge = Badge(bus)
        self.reclaimer = Reclaimer(on_done=self._reclaimed)
        self.call_sound = CallSound()
        # The page's sound follows the computer's output (Bluetooth headphones connected later).
        self.audio_follower = AudioFollower(on_change=self._devices_changed)
        self._window_services = False
        clipboard.tidy()
        self.settings.connect(self._on_setting)
        GLib.timeout_add_seconds(60, self._minute)
        GLib.timeout_add_seconds(20, self._check_idle)
        GLib.timeout_add_seconds(MEMORY_CHECK, self._check_memory)
        self._network_processes: set[int] = set()
        self._network_looks = 0
        self._last_look = time.monotonic()
        self._quitting = False
        GLib.timeout_add_seconds(NETWORK_CHECK_EARLY, self._check_network_process)
        # Asked to stop (the desktop closing apps to make room, logging out): quit properly, so
        # the tray icon goes and what Natter knows is saved, instead of just vanishing.
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._on_terminate)
        Gio.NetworkMonitor.get_default().connect("network-changed", self._on_network)
        if self.get_flags() & Gio.ApplicationFlags.IS_SERVICE:
            # Started over D-Bus by a search: answer from the chat lists on disk, without
            # WhatsApp, and leave once the searching stops (GLib waits 10 s for the first
            # call). Opening a result builds the window.
            self.set_inactivity_timeout(SEARCH_LINGER)

        for name, run, accels in [
            ("preferences", self.show_preferences, ["<Control>comma"]),
            ("shortcuts", self.show_shortcuts, ["<Control>question"]),
            ("about", self.show_about, []),
            ("add-account", lambda: self.add_account(), []),
            ("show", self.present_window, []),
            ("call-in-browser", self.call_in_browser, []),
            ("call", lambda: self.start_call(self.window.account if self.window else None, False), []),
            ("video-call", lambda: self.start_call(self.window.account if self.window else None, True), []),
            ("call-answer", self.answer_call, []),
            ("call-decline", self.decline_call, []),
            ("call-window", self.show_call_window, []),
            ("lock", self.lock_now, ["<Control>l"]),
            ("do-not-disturb", self.choose_do_not_disturb, []),
            ("pause-notifications-hour", lambda: self.pause_notifications(3600), []),
            ("pause-notifications-two-hours", lambda: self.pause_notifications(7200), []),
            ("pause-notifications", lambda: self.pause_notifications(None), []),
            ("resume-notifications", self.resume_notifications, []),
            ("quit", self.quit_natter, ["<Control>q"]),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, run=run: run())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        blur = Gio.SimpleAction.new_stateful("privacy-blur", None, GLib.Variant("b", self.settings["privacy_blur"]))
        blur.connect("change-state", lambda a, v: self.settings.__setitem__("privacy_blur", v.get_boolean()))
        self.add_action(blur)
        self.set_accels_for_action("app.privacy-blur", ["<Control><Shift>b"])
        # The microphone and camera switches, as check items (the menu, the palette), and
        # allow-media to turn one on: never off, so a toast's Turn On can't undo itself later.
        for kind in MEDIA_KINDS:
            switch = Gio.SimpleAction.new_stateful(kind, None, GLib.Variant("b", bool(self.settings[kind])))
            switch.connect("change-state", lambda _a, v, kind=kind: self.settings.__setitem__(kind, v.get_boolean()))
            self.add_action(switch)
        allow = Gio.SimpleAction.new("allow-media", GLib.VariantType("s"))
        allow.connect("activate", lambda _a, v: v.get_string() in MEDIA_KINDS
                      and self.settings.__setitem__(v.get_string(), True))
        self.add_action(allow)
        page = Gio.SimpleAction.new("preferences-page", GLib.VariantType("s"))
        page.connect("activate", lambda _a, v: v.get_string() in PREFERENCES_PAGES
                     and self.show_preferences(v.get_string()))
        self.add_action(page)
        open_chat = Gio.SimpleAction.new("open-chat", GLib.VariantType("(ss)"))
        open_chat.connect("activate", lambda _a, v: self.open_chat(v[0], v[1]))
        self.add_action(open_chat)
        open_file = Gio.SimpleAction.new("open-file", GLib.VariantType("s"))
        open_file.connect("activate", lambda _a, v: self._open_file(v.get_string()))
        self.add_action(open_file)
        if GLib.getenv("NATTER_DEBUG"):
            self._debug_actions()

    def _debug_actions(self) -> None:
        """D-Bus actions for poking at a running Natter, with NATTER_DEBUG set. Natter's own instance
        prints a token at start, and each argument has to start with it and a colon:

          gdbus call --session --dest <app id> --object-path /<app id with / for .> \\
            --method org.gtk.Actions.Activate debug-snapshot "[<'<token>:natter.png'>]" "{}"
        """
        token = debug_token()
        if token:
            print(f"natter: debug token {token}", flush=True)

        def add(name: str, run: Callable[[str], None]) -> None:
            def activate(_action, value: GLib.Variant) -> None:
                argument = debug_argument(value.get_string(), token)
                if argument is None:
                    print(f"natter: {name} refused: its argument doesn't start with the debug token",
                          file=sys.stderr, flush=True)
                else:
                    run(argument)

            action = Gio.SimpleAction.new(name, GLib.VariantType("s"))
            action.connect("activate", activate)
            self.add_action(action)

        # The window as it's drawn, as a PNG in SNAPSHOTS.
        add("debug-snapshot", lambda path: self.window and self._snapshot(path, self.window.save_snapshot))
        # "return navigator.userAgent": runs in the page and prints the result.
        add("debug-eval", lambda script: self.window and self.window.account and self.window.account.run(
            script, None, lambda value: print("natter: eval →", value, flush=True)))
        # WebKit paints the page itself (not the screen), as a PNG in SNAPSHOTS.
        add("debug-page-snapshot", lambda path: self.window and self.window.account and self._snapshot(
            path, lambda target: self._page_snapshot(self.window.account, target)))
        # "/path/file.png": what Ctrl+V does with a picture once it's read.
        add("debug-attach", lambda path: self.window and self.window.account and self.window.account.attach(
            [Path(path)], lambda ok: print("natter: attached:", ok, flush=True)))
        # "/path/file.pdf": what dropping that file on the chat does.
        add("debug-drop", lambda path: self.send_files([Path(path)]))

    def _snapshot(self, path: str, save: Callable[[str], None]) -> None:
        target = snapshot_path(path)
        if target is None:
            print(f"natter: snapshots are only saved in {SNAPSHOTS}", file=sys.stderr, flush=True)
            return
        private_folder(SNAPSHOTS)
        save(str(target))

    def do_dbus_register(self, connection: Gio.DBusConnection, object_path: str) -> bool:
        # Here rather than at start-up: this runs before Natter takes its bus name, so a search
        # that started Natter over D-Bus doesn't arrive before there's anything to answer it.
        # Privacy blur keeps chat names out of the desktop's search too.
        self.search = SearchProvider(connection, self._search_chats, self.open_chat,
                                     lambda query: self.go_to_chat(query=query),
                                     lambda: self.settings["search_provider"] and not self.settings["privacy_blur"]
                                     and not self.locked and not (self.window is None and self.lock_enabled))
        return Adw.Application.do_dbus_register(self, connection, object_path)

    def _make_web_context(self) -> None:
        if self.calls_in_page:
            # WhatsApp's call engine runs threads that share memory (SharedArrayBuffer). WebKitGTK
            # marks WhatsApp's page cross-origin isolated, which is what makes sharing safe, but
            # leaves the engine switch off: web processes started from now on have it on.
            os.environ.setdefault("JSC_useSharedArrayBuffer", "1")
        # The cache model is global in WebKit, and the default context's (a web browser's) has
        # WebKit keep finished web processes: the first, blank one stayed alive for 5 minutes
        # after every start. Setting it before Natter's context exists turns that off.
        WebKit.WebContext.get_default().set_cache_model(WebKit.CacheModel.DOCUMENT_BROWSER)
        found = memory_pressure(GLib.getenv("NATTER_MEMORY_LIMIT"), GLib.getenv("NATTER_MEMORY_THRESHOLDS"))
        if found:
            # WebKit frees caches past the conservative share of the limit and collects hard past
            # the strict one. It refuses a conservative threshold at or above the strict one, so
            # whichever keeps that true goes first.
            limit, conservative, strict = found
            pressure = WebKit.MemoryPressureSettings.new()
            pressure.set_memory_limit(limit)
            if conservative < pressure.get_strict_threshold():
                pressure.set_conservative_threshold(conservative)
                pressure.set_strict_threshold(strict)
            else:
                pressure.set_strict_threshold(strict)
                pressure.set_conservative_threshold(conservative)
            pressure.set_poll_interval(MEMORY_POLL)
            pressure.set_kill_threshold(MEMORY_KILL)
            self.web_context = WebKit.WebContext(memory_pressure_settings=pressure)
        else:
            self.web_context = WebKit.WebContext()
        if (self.calls_in_page and not in_sandbox() and os.path.isdir(UDEV_DATA)
                and hasattr(self.web_context, "add_path_to_sandbox")):
            # WebKitGTK 2.52 finds cameras through the desktop's camera portal, and gives it 10 ms
            # to answer. On a busy computer it's late, the camera is missing, and WhatsApp says
            # "Camera not found". WebKit then looks for cameras itself (GStreamer's v4l2 devices),
            # which needs udev's notes on each device, missing from its sandbox: read-only, they're
            # all it lacks (the device files are there already). Before any web process starts,
            # so before the anchor below: WebKit aborts if a path comes later. In the Flatpak,
            # WebKit's sandbox has no cameras at all, so there it's the portal or nothing.
            # That way skips the portal, and the desktop's "no" with it: Natter keeps that itself
            # (camera_gate, Account._on_permission).
            self.web_context.add_path_to_sandbox(UDEV_DATA, True)
            self.camera_fallback = True
        # Now that the process cache is sized (to none), a web browser's caches: 1 GB on disk and
        # 128 MB in memory, so WhatsApp's code isn't fetched again on every start. Its page was
        # ready in ~4.6 s instead of ~15.8 s with DOCUMENT_BROWSER's small ones.
        self.web_context.set_cache_model(WebKit.CacheModel.WEB_BROWSER)
        if self.calls_in_page:
            # WebKit tells desktop portals who each sandboxed web process is with the details of
            # the first sandbox it started, and the portal checks that sandbox is still running.
            # Natter's first web process is a blank one that goes away at once, so every later
            # one was refused the camera (and WebKit then waits for an answer forever). A blank
            # page started first and kept for good keeps that identity valid.
            self._sandbox_anchor = WebKit.WebView(web_context=self.web_context,
                                                  network_session=WebKit.NetworkSession.new_ephemeral())
            self._sandbox_anchor.connect("web-process-terminated", self._on_anchor_gone)
            self._sandbox_anchor.load_html("<!doctype html><title></title>", "about:blank")

    def _on_anchor_gone(self, _view, reason) -> None:
        """The anchor's web process ended. Another can't take its place: WebKit names every
        sandbox after the first it started, and that one is gone, so the portal now refuses every
        web process's camera with an error, not an answer. WebKit 2.52 waits for an answer on the
        page's main thread, spinning, for good: WhatsApp's next device listing (a call, a voice
        message) would never finish, and would hold a processor core while it waits. Nothing in a
        page's permissions stops a listing (WebKit lists before it asks), so the pages lose the
        microphone and camera altogether until NAME starts again: calls and voice messages say
        they can't, rather than hang. Only natively: in the Flatpak each sandbox is its own."""
        if self._quitting or self.media_lost:
            return
        nick = getattr(reason, "value_nick", str(reason))
        if in_sandbox():
            print(f"natter: WebKit's first sandbox stopped ({nick})", flush=True)
            return
        print(f"natter: WebKit's first sandbox stopped ({nick}); the microphone and camera are off "
              f"for WhatsApp until {NAME} restarts", flush=True)
        self.media_lost = True
        for account in self.accounts:
            account.lose_media()
        if self.window is not None and self.window.get_visible():
            self.window.toast(f"Restart {NAME} to use the microphone and camera again", timeout=10)
        elif self.window is not None:
            self.tell("media-lost", f"Restart {NAME} to use the microphone and camera",
                      "Calls and voice messages can't use them until then.")
        self._media_changed()

    def _build(self, hidden: bool = False) -> bool:
        """The window first, then WhatsApp in it. If the window can't be made, Natter quits
        rather than run WhatsApp where nobody can see it."""
        from .window import Window

        bus = self.get_dbus_connection()
        if old_natter_running(bus):
            # Its session is this one's: say so, and bring that Natter forward instead of opening it twice.
            title = "The old Natter is still running"
            body = ("It uses the same WhatsApp session. Quit it (its tray icon → Quit, or Ctrl+Q in its "
                    f"window) and start {NAME} again.")  # the old install by its old name, this one by its new
            print(f"natter: {title} ({OLD_APP_ID}). {body}", file=sys.stderr, flush=True)
            self.tell("old-natter", title, body)
            try:
                bus.call_sync(OLD_APP_ID, "/" + OLD_APP_ID.replace(".", "/"), "org.freedesktop.Application",
                              "Activate", GLib.Variant("(a{sv})", ({},)), None,
                              Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
            except GLib.Error:
                pass
            self.quit()
            return False
        self._start_window_services()  # before the window: its header asks which kind of calls there are
        try:
            self.window = Window(self)
            for info in self.settings["accounts"]:
                self._make_account(info)
        except Exception:
            traceback.print_exc()
            print("natter: couldn't open the window; quitting", file=sys.stderr, flush=True)
            self.window = None
            self.quit()
            return False
        self.window.connect("notify::visible", lambda w, _p: self._on_visible_changed(w.get_visible()))
        if not self.lock_enabled:
            # The lock was removed by hand (Forgot Passcode?): its state and its count of wrong
            # tries go with it, so a passcode set later starts clean.
            self.settings["locked"] = False
            self._attempts.succeeded()
        if self.settings["lock_on_start"] or self.settings["locked"] or (hidden and self.settings["lock_on_hide"]):
            self.lock()
        self.settle_first_run(hidden)
        self._rename_autostart()
        self._apply_tray()
        self._apply_background()
        self._on_window_visible(False)  # started hidden (--background) counts as hidden; showing cancels it
        self._show_unread()
        self._show_banner()  # a pause outlasts quitting: say so from the start
        return True

    def settle_first_run(self, hidden: bool = False) -> None:
        """Once, on a new install: without tray icons, a Natter still running after its window
        closes is out of sight, so closing quits, unless the person has chosen already. An upgrade
        keeps its settings. Started `hidden` (at login, maybe before the panel), it's settled when
        the window first closes, by which time the tray icon knows whether a panel shows it."""
        if self.settings["first_run_done"]:
            return
        if self._new_install and "run_in_background" not in self.settings.values:
            if hidden:
                return
            if not self.has_tray_host():
                self.settings["run_in_background"] = False
        self.settings["first_run_done"] = True

    def _make_account(self, info: dict) -> Account:
        account = Account(self, info, url=self.dev_url)
        # Kept, so they can be let go when the account is removed (remove_account).
        account.app_handlers = [account.connect(signal, handler) for signal, handler in (
            ("unread-changed", lambda *_: self._show_unread()),
            ("state-changed", self._on_account_state),
            ("notify-message", self._on_notify_message),
            ("unnotify", lambda a, page_id: self.notifier.withdraw_page(a.id, page_id)),
            ("download-done", self._on_download),
            ("call-requested", lambda a, video: self.start_call(a, video)),
            ("in-call", self._on_page_call),
        )]
        self.accounts.append(account)
        self.window.add_account(account)
        return account

    def do_activate(self) -> None:
        self.present_window()

    # ---- Command line: first start and every later launch ---------------------------------

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        first = self.window is None
        url, warning = dev_url(options.get("url"), command_line.get_is_remote())
        if warning:
            command_line.printerr_literal(warning + "\n")
        if url:
            self.dev_url = url
        links, files = [], []
        for arg in options.get(GLib.OPTION_REMAINING, []) or []:
            if chat_link(arg) or page_link(arg):
                links.append(arg)
                continue
            gfile = command_line.create_file_for_arg(arg)
            if gfile.get_path() and Path(gfile.get_path()).is_file():
                files.append(Path(gfile.get_path()))
            else:
                command_line.printerr_literal(f"natter: not a WhatsApp link or a file: {arg}\n")

        wanted = links or files or options.get("chat") or options.get("chat-id")
        if first and options.get("background") and not wanted:
            return 0 if self._build(hidden=True) else 1
        if not self.present_window():
            return 1
        account = self.account_by_id(options.get("account")) or self.window.account
        if options.get("chat") and account:
            self.go_to(account, options["chat"])
        if options.get("chat-id"):
            found = find_chat(options["chat-id"], [(a.id, a.chats) for a in self.accounts])
            if found:
                self.go_to(self.account_by_id(found[0]), found[1])
            else:
                self.window.toast("That chat isn't in the chat list any more")
        for link in links:
            if not self._held_while_locked(lambda link=link: (self.window.account or self.accounts[0]).open_link(link)):
                (self.window.account or self.accounts[0]).open_link(link)
        if files:
            self.go_to_chat(files)
        return 0

    def _devices_changed(self) -> None:
        for account in self.accounts:
            account.devices_changed()
        self._check_microphone()  # one may have come or gone

    def _start_window_services(self) -> None:
        """What a Natter with a window runs, however it was started: also when D-Bus started it
        as a service (for a search, or for its menu entry) and then it was opened."""
        if self._window_services:
            return
        self._window_services = True
        self.audio_follower.start()
        self.desktop_media = DesktopMedia(self._on_desktop_media, self.get_dbus_connection())
        self._check_microphone()
        if calls_available() and not self.calls_in_page:
            microphone, camera = self._call_window_media()
            self.calls = CallWindow(DATA / "calls", debug=bool(GLib.getenv("NATTER_DEBUG")),
                                    microphone=microphone, camera=camera)
            self.calls.connect("state-changed", self._on_calls_state)
            self.calls.connect("call-failed", self._on_call_failed)
            self.calls.connect("incoming", self._on_incoming_call)
            self.calls.connect("incoming-ended", lambda *_: self.withdraw_notification("call"))
            self.calls.connect("in-call", self._on_in_call)
            self._calls_keep_running()

    def present_window(self) -> bool:
        if self.window is None and not self._build():
            return False
        self.window.present()
        return True

    def account_by_id(self, account_id: Optional[str]) -> Optional[Account]:
        return next((a for a in self.accounts if a.id == account_id), None)

    # ---- The app lock -----------------------------------------------------------------------

    @property
    def lock_enabled(self) -> bool:
        return bool(self.settings["lock_hash"])

    def lock(self) -> None:
        """Hide WhatsApp behind the passcode."""
        if not self.lock_enabled or self.locked:
            return
        self.locked = True
        self.settings["locked"] = True  # still locked after quitting and starting again
        if self.window:
            self.window.show_lock(True)
        self.notifier.conceal()  # those already shown stop saying who wrote and what
        self._refresh_tray()

    def lock_now(self) -> None:
        if self.lock_enabled:
            self.lock()
        elif self.window:
            self.window.toast("Set a passcode first, in Preferences → Privacy", "Preferences", "app.preferences-page",
                              GLib.Variant("s", "privacy"))

    def check_passcode(self, passcode: str, done: Callable[[bool, str], None]) -> None:
        """Check `passcode` off the main thread; `done(ok, message)` on it. Wrong ones count
        towards the waits (lock.Attempts), wherever they're typed."""
        wait = self._attempts.wait()
        if wait:
            done(False, f"Too many tries. Try again in {wait} seconds.")
            return
        stored = self.settings["lock_hash"]

        def check() -> None:
            try:
                ok = applock.check(passcode, stored)
            except Exception:  # never leave the lock screen waiting
                ok = False
            GLib.idle_add(lambda: self._checked(ok, done) and False)
        threading.Thread(target=check, name="natter-passcode", daemon=True).start()

    def _checked(self, ok: bool, done: Callable[[bool, str], None]) -> None:
        if ok:
            self._attempts.succeeded()
            done(True, "")
            return
        self._attempts.failed()
        wait = self._attempts.wait()
        done(False, f"Wrong passcode. Try again in {wait} seconds." if wait else "Wrong passcode")

    def try_unlock(self, passcode: str, done: Callable[[bool, str], None]) -> None:
        def checked(ok: bool, message: str) -> None:
            if ok:
                self.locked = False
                self.settings["locked"] = False
                self.note_activity()
                if self.window:
                    self.window.show_lock(False)
                self._refresh_tray()
            done(ok, message)
            if ok:
                waiting, self._after_unlock = self._after_unlock, None
                if waiting:
                    waiting()
        self.check_passcode(passcode, checked)

    def _held_while_locked(self, run: Callable[[], None]) -> bool:
        """While locked, what would show a chat waits for the unlock (the latest such): True if
        `run` waits. The window comes forward, showing the passcode."""
        if not self.locked:
            return False
        self._after_unlock = run
        self.present_window()
        return True

    def note_activity(self) -> None:
        """Someone used the window (for locking after being idle)."""
        self._last_activity = awake_time()

    def _check_idle(self) -> bool:
        minutes = self.settings["lock_idle_minutes"]
        if minutes and self.lock_enabled and not self.locked and not self.calling \
                and awake_time() - self._last_activity > minutes * 60:
            self.lock()
        return GLib.SOURCE_CONTINUE

    # ---- Do not disturb ----------------------------------------------------------------------

    def pause_notifications(self, seconds: Optional[int]) -> None:
        """No notifications for `seconds`, or until turned back on (None)."""
        self.settings["dnd_until"] = -1 if seconds is None else int(time.time() + seconds)

    def resume_notifications(self) -> None:
        self.settings["dnd_until"] = 0

    def _minute(self) -> bool:
        """Once a minute, by the wall clock (a timer would stop counting while the computer
        sleeps): do not disturb ends when it said it would, and the page's own sounds follow."""
        until = self.settings["dnd_until"]
        if isinstance(until, (int, float)) and 0 < until <= time.time():
            self.settings["dnd_until"] = 0  # → _on_setting refreshes the tray and the banner
        self._apply_page_sound()
        return GLib.SOURCE_CONTINUE

    def _apply_page_sound(self) -> None:
        """WhatsApp plays its message tones in the page itself: while notifications are paused,
        and nobody's using Natter's window or calling, the page is muted. In use, it isn't, so a
        voice note still plays."""
        active = bool(self.window and self.window.is_active())
        muted = self.settings.notifications_paused() and not self.calling and not active
        for account in self.accounts:
            if account.view.get_is_muted() != muted:
                account.view.set_is_muted(muted)

    def _dnd_items(self) -> list[MenuItem]:
        """One tray item for do not disturb. Its choices were a submenu, but Ada's panel unfolds
        submenus into loose items and drops their name ("For 1 Hour", of what?), so they're asked
        in Natter's window instead (choose_do_not_disturb)."""
        if self.settings.do_not_disturb():
            return [MenuItem("Resume Notifications", self.resume_notifications)]
        return [MenuItem("Do Not Disturb…", self._from_tray(self.choose_do_not_disturb))]

    def choose_do_not_disturb(self) -> None:
        """How long to pause notifications for, asked in the window. Nothing in it is private, so
        it's asked over the lock screen too, without unlocking."""
        if not self.present_window():
            return
        name = NAME.replace(" ", " ")  # a line may break before the name, not inside it
        alert = Adw.AlertDialog(heading="Do Not Disturb", body=f"No notifications or message sounds from {name}.")
        # The lengths are one choice in a list, so the answer is just Cancel or Pause: as responses
        # they didn't fit side by side, and stacked, Cancel read as a fourth length.
        choices = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        picked: list[tuple[Gtk.CheckButton, Optional[int]]] = []
        for label, seconds in DO_NOT_DISTURB:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=not picked)
            if picked:
                check.set_group(picked[0][0])
            row = Adw.ActionRow(title=label, activatable_widget=check)
            row.add_prefix(check)
            choices.append(row)
            picked.append((check, seconds))
        alert.set_extra_child(choices)
        alert.add_response("cancel", "_Cancel")
        alert.add_response("pause", "_Pause")
        alert.set_response_appearance("pause", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("pause")
        alert.set_close_response("cancel")

        def answered(_alert, response: str) -> None:
            if response == "pause":
                self.pause_notifications(next((seconds for check, seconds in picked if check.get_active()),
                                              DO_NOT_DISTURB[0][1]))

        alert.connect("response", answered)
        alert.present(self.window)

    # ---- Chats -----------------------------------------------------------------------------

    def go_to_chat(self, files: Optional[list[Path]] = None, query: str = "") -> None:
        from .palette import Palette

        if self._held_while_locked(lambda: self.go_to_chat(files, query)):
            return
        if self.present_window():
            Palette(self, files, query).present(self.window)

    def open_chat(self, account_id: str, name: str) -> None:
        """Open a chat given by account id: from desktop search or the open-chat action."""
        if self.window is None and not self._build():
            return
        self.go_to(self.account_by_id(account_id), name)

    def go_to(self, account: Optional[Account], name: str, files: Optional[list[Path]] = None) -> None:
        if self._held_while_locked(lambda: self.go_to(account, name, files)):
            return
        if account is None or not self.present_window():
            return
        self.window.show_account(account)

        def opened(ok: bool) -> None:
            if not ok:
                self.window.toast(f"Couldn't find “{name}” in WhatsApp's chat list")
            elif files:
                account.attach(files, lambda sent: sent or self.window.toast("WhatsApp didn't take the files"))

        account.open_chat(name, opened)

    def message_number(self, account: Account, digits: str, files: Optional[list[Path]] = None) -> None:
        self.present_window()
        self.window.show_account(account)
        account.open_link(f"https://wa.me/{digits}")
        if files:
            account.when_ready(lambda: GLib.timeout_add(1500, lambda: account.attach(files) and False))

    def _search_chats(self) -> list[tuple[str, str, Chat]]:
        """Every account's chats for desktop search. A Natter started just for a search reads
        them from disk; WhatsApp only starts when a result is opened."""
        if self.window is None:
            self.hold()
            self.release()  # restarts the countdown to leaving
            indexes = [(info["id"], info.get("name") or "WhatsApp",
                        ChatIndex(DATA / "accounts" / info["id"] / "chats.json"))
                       for info in self.settings["accounts"]]
        else:
            indexes = [(a.id, a.name, a.chats) for a in self.accounts]
        return [(account_id, name, chat) for account_id, name, index in indexes for chat in index.recent()]

    def call_in_browser(self) -> None:
        """WebKitGTK is built without WebRTC, so WhatsApp can't call from inside Natter: open
        WhatsApp Web in the browser, where it can."""
        open_uri(WHATSAPP_URL, self.window)
        if self.window and "calls" not in self._told:
            self._told.add("calls")
            self.window.toast("Calls open in your browser — find the chat there and press call. "
                              "The first time, link the browser with your phone.", timeout=10)

    # ---- Calls ------------------------------------------------------------------------------
    # WhatsApp can't call from WebKitGTK (no WebRTC), so calls go to Natter's call window, a
    # separate process running WhatsApp in Qt WebEngine (Chromium), linked once as its own device.

    def _calls_keep_running(self) -> None:
        """Ring for calls: keep the linked call window running in the background."""
        if self.calls:
            self.calls.keep_running = bool(self.settings["calls_ring"] and self.settings["calls_linked"])

    @property
    def can_call(self) -> bool:
        return self.calls_in_page or self.calls is not None

    def start_call(self, account: Optional[Account], video: bool) -> None:
        if self._held_while_locked(lambda: self.start_call(account, video)):
            return
        if account is None:
            return
        if self.calls_in_page:
            # WhatsApp calls from its own page: press its call button in the open chat.
            def done(reason) -> None:
                if reason and self.window:
                    self.window.toast(CALL_FAILURES_IN_PAGE.get(reason, "The call didn't start"))
            account.when_ready(lambda: account.run("return window.__natter.startCall(video)", {"video": video}, done))
            return
        if self.calls is None or account is not self.accounts[0]:
            # No Qt WebEngine here, or a second account (the call window holds the first).
            self.call_in_browser()
            return
        chat = account.active_chat
        if not chat:
            self.window.toast("Open a chat first, then call")
            return
        self._pending_call = (chat, video)
        if not self.settings["calls_linked"] and "calls-link" not in self._told:
            self._told.add("calls-link")
            self.window.toast("The call window opens. Link it once with your phone, like a new device.", timeout=8)
        self.calls.call(chat, video)

    def _on_calls_state(self, _calls: CallWindow, state: str) -> None:
        if state == "ready":
            if not self.settings["calls_linked"]:
                self.settings["calls_linked"] = True
                if self.window:
                    self.window.toast("Call window linked: calls start here, and ring here too")
            if self._pending_call:
                chat, video = self._pending_call
                self.calls.call(chat, video)  # it asked before WhatsApp was up there
        elif state == "needs-link" and self.settings["calls_linked"]:
            # Unlinked from the phone: ring only once it's linked again.
            self.settings["calls_linked"] = False
            self.tell("calls-link", "The call window needs linking again",
                      f"Calls won't ring until you link it. Start a call in {NAME} to see the code.")

    def _on_call_failed(self, _calls: CallWindow, reason: str) -> None:
        if reason in ("not-ready", "loading"):
            return  # the call goes out once the call window's WhatsApp is up
        chat = self._pending_call[0] if self._pending_call else ""
        self._pending_call = None
        if self.window:
            self.window.toast(CALL_FAILURES.get(reason, "The call didn't start ({reason})").format(chat=chat, reason=reason))

    def _on_in_call(self, _calls: CallWindow, active: bool) -> None:
        if active:
            self._pending_call = None
            self.withdraw_notification("call")

    def show_call_window(self) -> None:
        # The call window is a whole WhatsApp of its own: not while Natter is locked.
        if self._held_while_locked(self.show_call_window):
            return
        if self.calls:
            self.calls.show()
        else:
            self.call_in_browser()

    def _on_incoming_call(self, _calls: CallWindow, caller: str, video: bool) -> None:
        kind = "video call" if video else "voice call"
        if self.locked or self.settings["privacy_blur"]:
            caller = ""  # who's calling isn't shown then, as for messages
        notification = Gio.Notification.new(f"{caller} is calling" if caller else f"Incoming WhatsApp {kind}")
        notification.set_body(f"WhatsApp {kind}" if caller else "")
        notification.set_icon(Gio.ThemedIcon.new("call-start-symbolic"))
        notification.set_priority(Gio.NotificationPriority.URGENT)
        if hasattr(notification, "set_category"):  # GLib 2.70+
            notification.set_category("call.incoming")
        notification.set_default_action("app.call-window")
        notification.add_button("Answer", "app.call-answer")
        notification.add_button("Decline", "app.call-decline")
        self.send_notification("call", notification)

    def answer_call(self) -> None:
        if self._held_while_locked(self.answer_call):
            return
        self.withdraw_notification("call")
        if self.calls:
            self.calls.answer()

    def decline_call(self) -> None:
        self.withdraw_notification("call")
        if self.calls:
            self.calls.decline()

    def _page_snapshot(self, account: Account, path: str) -> None:
        def done(view, result) -> None:
            try:
                view.get_snapshot_finish(result).save_to_png(path)
                print(f"natter: saved page {path}", flush=True)
            except GLib.Error as error:
                print(f"natter: page snapshot failed: {error.message}", flush=True)

        account.view.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, done)

    def paste(self, force: bool = False) -> bool:
        """Ctrl+V in the page. True when Natter takes it (pictures, files); False leaves it to WebKit."""
        if self.locked:
            return False
        window = self.window
        account = window.account if window else None
        if account is None or not (force or window.focus_in_page()):
            return False

        def got(paths: list[Path]) -> None:
            if paths:
                self.send_files(paths)
            else:
                account.view.execute_editing_command(WebKit.EDITING_COMMAND_PASTE)

        return clipboard.read(window.get_clipboard(), got)

    def send_files(self, paths: list[Path]) -> None:
        """Files pasted or dropped on the window: into the open chat, or ask which chat."""
        if self._held_while_locked(lambda: self.send_files(paths)):
            return
        window = self.window
        account = window.account if window else None
        if account is None or not paths:
            return
        if not account.active_chat:
            self.go_to_chat(paths)
            return
        def done(ok: bool) -> None:
            if GLib.getenv("NATTER_DEBUG"):
                print(f"natter: {len(paths)} file(s) → taken: {ok}", flush=True)
            if not ok:
                window.toast("WhatsApp didn't take the files")

        account.attach(paths, done)

    # ---- Unread, notifications, focus -----------------------------------------------------

    def _show_unread(self) -> None:
        total = sum(a.unread for a in self.accounts)
        self.badge.set(total)
        if self.tray:
            self.tray.set_unread(total)
        if self.window:
            self.window.show_status(total)
        self._refresh_tray()

    def _on_account_state(self, account: Account) -> None:
        self._show_unread()  # the window says when an account needs linking
        hidden = not (self.window and self.window.get_visible())
        if account.ready:
            self._linking.discard(account.id)  # linked again (not just reloading: that says nothing yet)
        elif account.needs_link and account.id not in self._linking:
            self._linking.add(account.id)
            if hidden:
                self.tell(f"link-{account.id}", "WhatsApp needs to be linked again",
                          f"Open {NAME} and scan the code with your phone ({account.name})."
                          if len(self.accounts) > 1 else f"Open {NAME} and scan the code with your phone.")
        if account.ready:
            self._check_media()
            if account.look is not None:
                self.page_look_changed(account)  # it may have said so while it wasn't ready (a link opening)
        else:
            self._show_style_hint()  # a page that isn't up (reloading, linking) no longer counts
            if (self.calls and account is self.accounts[0] and self.calls.keep_running
                    and not self.calls.running and "calls-start" not in self._told):
                self._told.add("calls-start")
                GLib.timeout_add_seconds(CALLS_START_DELAY, lambda: self.calls.keep_running and self.calls.start() and False)

    def page_changed(self, missing: list[str]) -> None:
        """The bridge's canary found WhatsApp's layout changed under it: say so, once a session.
        Messages keep working either way; it's Natter's extras around the page that pause."""
        if "page-changed" in self._told:
            return
        self._told.add("page-changed")
        self.tell("page-changed", "WhatsApp has changed its layout",
                  f"Some of {NAME}'s extras may pause until an update ({', '.join(missing)}).")

    def tell(self, key: str, title: str, body: str = "", button: Optional[tuple[str, str, GLib.Variant]] = None) -> None:
        """A desktop notification from Natter itself, not from a chat. Clicking it opens the window."""
        notification = Gio.Notification.new(title)
        if body:
            notification.set_body(body)
        notification.set_icon(app_icon())
        notification.set_default_action("app.show")
        if button:
            notification.add_button_with_target_value(*button)
        self.send_notification(key, notification)

    def _on_notify_message(self, account: Account, page_id: int, title: str, body: str, tag: str, icon: str) -> None:
        if self.window and self.window.is_active() and self.window.account is account and account.active_chat == title:
            return  # it's the chat on screen
        several = len(self.accounts) > 1
        self.notifier.show(account.id, account.name if several else None, page_id, title, body, tag or title, icon)

    def _open_from_notification(self, account_id: str, title: str, page_id: int, token: str) -> None:
        if self._held_while_locked(lambda: self._open_from_notification(account_id, title, page_id, token)):
            return
        account = self.account_by_id(account_id)
        if account is None:
            return
        if token:
            self.window.set_startup_id(token)
        self.present_window()
        self.window.show_account(account)
        account.click_notification(page_id, title)

    def on_focus_changed(self, active: bool) -> None:
        self._apply_page_sound()
        if active:
            account = self.window.account
            if account:
                self.notifier.withdraw(account.id)
        away = not active and self.settings["blur_when_away"]
        for account in self.accounts:
            account.set_class("natter-away", away)

    def on_page_changed(self) -> None:
        if self.window and self.window.is_active() and self.window.account:
            self.notifier.withdraw(self.window.account.id)

    def _on_download(self, _account: Account, path: str, ok: bool, reason: str) -> None:
        name = f"“{Path(path).name}”" if path else "the file"
        if ok:
            self.downloaded.add(path)
            open_it = ("Open", "app.open-file", GLib.Variant("s", path))
        if self.window and self.window.get_visible():
            if ok:
                self.window.toast(f"Saved {name}", *open_it)
            else:
                self.window.toast(f"Couldn't save {name}: {reason}")
        elif ok:
            self.tell(f"download-{path}", f"Saved {name}", str(Path(path).parent), open_it)
        else:
            self.tell(f"download-{path}", f"Couldn't save {name}", reason)

    def _open_file(self, path: str) -> None:
        if path in self.downloaded:
            open_uri(Gio.File.new_for_path(path).get_uri(), self.window)

    def _on_network(self, monitor: Gio.NetworkMonitor, available: bool) -> None:
        if available:
            for account in self.accounts:
                if account.failed:
                    account.reload()

    # ---- Media -----------------------------------------------------------------------------

    def media(self, done: Callable[[list[str], str], None]) -> None:
        """Find out what's missing to play WhatsApp's videos and voice messages, and how to install
        it, then call `done(missing, hint)`. GStreamer can take seconds to look through its plugins,
        so it's asked on a thread of its own, once; `done` runs on the main thread."""
        if self._media is not None:
            done(*self._media)
            return
        self._media_waiting.append(done)
        if len(self._media_waiting) == 1:
            threading.Thread(target=lambda: GLib.idle_add(self._media_found, media_report()),
                             name="natter-media", daemon=True).start()

    def _media_found(self, found: tuple[list[str], str]) -> bool:
        self._media = found
        waiting, self._media_waiting = self._media_waiting, []
        for done in waiting:
            done(*found)
        return GLib.SOURCE_REMOVE

    def _check_media(self) -> None:
        """Once a session, once WhatsApp is up and the window shows: say if some media can't play."""
        if self._media_checked or not (self.window and self.window.get_visible()):
            return
        if not any(a.ready for a in self.accounts):
            return
        self._media_checked = True

        def found(missing: list[str], _hint: str) -> None:
            # A banner, not a toast: it lasts without covering WhatsApp's message box.
            self._media_banner = bool(missing)
            self._show_banner()

        def check() -> bool:
            self.media(found)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add_seconds(MEDIA_CHECK_DELAY, check)

    def _show_banner(self) -> None:
        """The window's banner, the one place for states that last (banner_for): notifications
        paused, else media that can't play, until Preferences, where the details are, is opened."""
        if self.window is None:
            return
        shown = banner_for(self.settings["dnd_until"], self._media_banner, time.time())
        if shown is None:
            self.window.hide_banner()
            return
        title, button, action, page = shown
        self.window.set_banner(title, button, action, GLib.Variant("s", page) if page else None)

    # ---- The microphone and the camera ------------------------------------------------------
    # Two switches of Natter's own, for every account (the settings "microphone" and "camera"),
    # and what the desktop says about the camera (media.py). They act only as a browser does for
    # a site it has blocked: WebKit's permission answers, its capture state, and a fresh page
    # (account.py). Nothing runs in WhatsApp's page for them.

    def camera_gate(self) -> Optional[str]:
        """The desktop's answer about the camera that Natter keeps itself (media.refusal):
        natively, where WebKit can find the camera without the portal (camera_fallback) and the
        call window's Chromium always does. None in the Flatpak, where the portal keeps it, and
        while it isn't known."""
        if self.desktop_media is None or self.desktop_media.sandboxed:
            return None
        return self.desktop_media.answer

    def media_state(self, kind: str) -> dict:
        """What's known about the microphone or the camera (`kind`), for Preferences' line under
        its switch (media.device_subtitle) and the debug info: on (Natter's switch), desktop (the
        desktop's answer, camera only), present (None when unknown), lockdown (the desktop has
        cameras off for every app), sandboxed, and restart (calls lost both until Natter
        restarts, _on_anchor_gone)."""
        desktop = self.desktop_media
        sandboxed = desktop.sandboxed if desktop is not None else in_sandbox()
        if kind == "camera":
            answer = desktop.answer if desktop is not None else None
            present = desktop.present if desktop is not None else None
            lockdown = bool(desktop is not None and desktop.lockdown)
        else:
            answer, present, lockdown = None, self.microphone_present, False
        return {"on": bool(self.settings[kind]), "desktop": answer, "present": False if self.media_lost else present,
                "lockdown": lockdown, "sandboxed": sandboxed, "restart": self.media_lost}

    def watch_media(self, callback: Callable[[], None]) -> int:
        """Call `callback()` whenever anything media_state says may have changed. Returns a
        handle for unwatch_media."""
        self._media_watch_next += 1
        self._media_watchers[self._media_watch_next] = callback
        return self._media_watch_next

    def unwatch_media(self, handle: int) -> None:
        self._media_watchers.pop(handle, None)

    def _media_changed(self) -> None:
        for callback in list(self._media_watchers.values()):
            callback()

    def _call_window_media(self) -> tuple[bool, bool]:
        """What the call window may let WhatsApp use, as (microphone, camera). Chromium there
        opens the camera itself (v4l2) without asking the portal, so the desktop's "no" is
        Natter's to keep there too."""
        return (bool(self.settings["microphone"]),
                media.refusal("camera", bool(self.settings["camera"]), self.camera_gate()) is None)

    def _apply_media(self, kind: str) -> None:
        """A switch changed, from wherever: every account's page follows, the call window too.
        Off ends what's using it now; On lets WhatsApp ask again."""
        if self.settings[kind]:  # the value now: an earlier listener may have changed it back
            for account in self.accounts:
                account.media_allowed_again(kind)
        else:
            self._media_stopped([account for account in self.accounts if account.stop_media(kind)],
                                MEDIA_STOPPED[kind])
        if self.calls is not None:
            self.calls.set_media(*self._call_window_media())
        if self.desktop_media is not None:
            self.desktop_media.refresh()  # turned on to try again: what does the desktop say now?
        self._media_changed()

    def _media_stopped(self, accounts: list[Account], title: str) -> None:
        """Say so when turning a device off ended WhatsApp's use of it (a call, a voice message),
        naming the accounts when there are several."""
        if not accounts or self.window is None:
            return
        if len(self.accounts) > 1:
            title += f" ({', '.join(account.name for account in accounts)})"
        self.window.toast(title)

    def _on_desktop_media(self) -> None:
        """The desktop's answer, its camera or its lockdown changed (DesktopMedia). A new "no"
        does what the switch turned off would; a "no" gone, what turning it on would. While Natter
        asks again itself, the answers it passes through on the way don't count."""
        if not self._asking_camera:
            gate, was = self.camera_gate(), self._camera_gate_was
            self._camera_gate_was = gate
            if gate == "no" and was != "no":
                self._media_stopped([account for account in self.accounts if account.stop_media("camera")],
                                    "Your desktop blocked the camera. WhatsApp's video has stopped")
            elif was == "no" and gate != "no":
                for account in self.accounts:
                    account.media_allowed_again("camera")
        if self.calls is not None:
            self.calls.set_media(*self._call_window_media())
        self._media_changed()

    def ask_camera_again(self, done: Callable[[Optional[str]], None]) -> None:
        """Have the desktop ask again whether Natter may use the camera (media.ask_camera_again),
        once the user has said yes to that in Natter. `done(answer)`: "yes", "no", or None when
        the desktop couldn't be asked. One question at a time."""
        bus = None if self._asking_camera else media.private_bus()
        if bus is None:
            done(None)
            return
        self._asking_camera = True

        def asked(answer: Optional[str]) -> None:
            self._asking_camera = False
            bus.close(None, None)
            if self.desktop_media is not None:
                self.desktop_media.asked(answer)  # and reads the store again
            self._on_desktop_media()
            done(answer)

        media.ask_camera_again(bus, asked)

    def _check_microphone(self) -> None:
        """Whether there's a microphone (media.microphone_present), on a thread: pactl is a
        program to wait on. At start, and whenever an input comes or goes (AudioFollower)."""
        def check() -> None:
            found = media.microphone_present()
            GLib.idle_add(lambda: self._microphone_found(found) and False)

        threading.Thread(target=check, name="natter-microphone", daemon=True).start()

    def _microphone_found(self, found: Optional[bool]) -> None:
        if found != self.microphone_present:
            self.microphone_present = found
            self._media_changed()

    # ---- Memory while hidden --------------------------------------------------------------

    def _on_visible_changed(self, visible: bool) -> None:
        if not visible and self.settings["lock_on_hide"]:
            self.lock()
        self._on_window_visible(visible)

    def _on_window_visible(self, visible: bool) -> None:
        if self._reclaim_timer:
            GLib.source_remove(self._reclaim_timer)
            self._reclaim_timer = 0
        self._schedule_fresh_pages(visible)
        self._refresh_tray()
        if visible:
            self.reclaimer.stop()
            self._check_media()
            return
        self._write_menu_chats()
        # Now, not on a timer: Ada may freeze a hidden Natter within seconds, and a frozen one
        # runs nothing, so a page past its limit would be swapped out whole instead.
        self._fresh_pages_for_memory()
        if self.settings["light_background"] and self.reclaimer.available and not self.calling:
            self._reclaim_timer = GLib.timeout_add_seconds(HIDDEN_BEFORE_RECLAIM, self._reclaim)

    @property
    def calling(self) -> bool:
        """A call is on in one of the accounts' pages."""
        return any(account.calling for account in self.accounts)

    def _on_page_call(self, account: Account, active: bool) -> None:
        # During a call nothing squeezes the page: no reclaim, and a sound stream the desktop
        # can see (see CallSound) so it isn't paused either. Afterwards, as before, and once
        # Natter has been hidden a while, a fresh page for the memory the call left behind.
        if not active:
            self._after_call.add(account)
        self._apply_page_sound()
        if self.calling:
            if self._reclaim_timer:
                GLib.source_remove(self._reclaim_timer)
                self._reclaim_timer = 0
            self.reclaimer.stop()
            self.call_sound.start()
        else:
            self.call_sound.stop()
            if self.window and not self.window.get_visible():
                self._on_window_visible(False)

    def _schedule_fresh_pages(self, visible: bool) -> None:
        if self._fresh_timer:
            GLib.source_remove(self._fresh_timer)
            self._fresh_timer = 0
        if not visible and self._after_call and not self.calling:
            self._fresh_timer = GLib.timeout_add_seconds(FRESH_AFTER_CALL, self._fresh_pages)

    def _fresh_pages(self) -> bool:
        self._fresh_timer = 0
        if self.calling or (self.window and self.window.get_visible()):
            return GLib.SOURCE_REMOVE
        for account in list(self._after_call):
            if account in self.accounts and not account.calling:
                if GLib.getenv("NATTER_DEBUG"):
                    print(f"natter: a fresh page for {account.name}, for the memory its call left behind", flush=True)
                account.fresh_page()
            self._after_call.discard(account)
        return GLib.SOURCE_REMOVE

    def _reclaim(self) -> bool:
        # Past the limit, what's there is WhatsApp's own and live (WebKit can't collect it):
        # pushing it to swap only hides it, and filled 9.7 GB of swap one day. A fresh page frees it.
        if not self._fresh_pages_for_memory():
            self.reclaimer.start()
        self._reclaim_timer = GLib.timeout_add_seconds(RECLAIM_AGAIN, self._reclaim)
        return GLib.SOURCE_REMOVE

    def _memory_past_limit(self) -> bool:
        """Natter's unit holds more than its accounts' pages should: their limit each, and Natter."""
        limit = (memory_pressure(GLib.getenv("NATTER_MEMORY_LIMIT"), None) or (MEMORY_LIMIT, 0, 0))[0]
        return footprint() > (limit * max(1, len(self.accounts)) + 300) * 1024 * 1024

    def _fresh_pages_for_memory(self) -> bool:
        """While hidden and not calling: fresh pages for accounts past the limit, or when the
        whole computer is short of memory. True if any was started."""
        if self.calling or (self.window and self.window.get_visible()):
            return False
        why = self._memory_reason()
        if not why:
            return False
        now, started = time.monotonic(), False
        for account in self.accounts:
            if account.ready and not account.calling and now - account.fresh_at > FRESH_AGAIN:
                print(f"natter: a fresh page for {account.name}: {why} ({footprint() >> 20} MB held, "
                      f"{own_swap() >> 20} MB of it in swap)", flush=True)
                account.fresh_page()
                self._after_call.discard(account)
                started = True
        return started

    def _memory_reason(self) -> str:
        """Why Natter should give memory back now, in words; "" when it needn't. A fresh page
        is the only way to free what's in swap (the old process's slots go with it)."""
        held = footprint()
        if self._memory_past_limit():
            return "past its limit"
        if own_swap() > SWAP_BUDGET_MB * 1024 * 1024:
            return "much of it in swap"
        if held > MEMORY_LIMIT * 1024 * 1024 and (memory_tight() or swap_full()):
            return "the computer is short of memory"
        return ""

    def _check_memory(self) -> bool:
        self._fresh_pages_for_memory()
        return GLib.SOURCE_CONTINUE

    def _check_network_process(self) -> bool:
        """WebKit's network process holds all of WhatsApp's connections: its websocket, its
        database, every load. When it dies WebKit starts another, but the page stays cut off from
        it (its database calls fail, its loads fail) and WhatsApp never recovers on its own."""
        now = time.monotonic()
        if now - self._last_look > THAW_GAP:
            self._thawed(now - self._last_look)
        self._last_look = now
        found = network_processes()
        lost = self._network_processes - found
        self._network_processes = found
        if lost and not self._quitting:
            print("natter: WebKit's network process stopped; WhatsApp gets a fresh page", flush=True)
            for account in self.accounts:
                account.network_lost()
        self._network_looks += 1
        if self._network_looks == NETWORK_EARLY_LOOKS:
            GLib.timeout_add_seconds(NETWORK_CHECK, self._check_network_process)
            return GLib.SOURCE_REMOVE  # the early, quicker looks are over
        return GLib.SOURCE_CONTINUE

    def _reclaimed(self, before: int, after: int) -> bool:
        # Logged, like every memory decision, so the next incident can be read from the journal.
        print(f"natter: gave back {(before - after) >> 20} MB ({before >> 20} → {after >> 20} MB; "
              f"{own_swap() >> 20} MB in swap)", flush=True)
        return GLib.SOURCE_REMOVE

    def _thawed(self, paused: float) -> None:
        print(f"natter: running again after about {paused:.0f} s paused; {footprint() >> 20} MB held", flush=True)
        self._fresh_pages_for_memory()

    def _on_terminate(self) -> bool:
        print("natter: asked to stop (SIGTERM); quitting", flush=True)
        self.quit_natter()
        return GLib.SOURCE_REMOVE

    # ---- Closing the window ----------------------------------------------------------------

    def window_closed(self) -> None:
        """The window was closed with Natter set to keep running."""
        if "still-running" in self._told or (self.tray and self.has_tray_host()):
            return
        self._told.add("still-running")
        self.tell("still-running", f"{NAME} is still running", "Open it again from your apps.")

    # ---- Dock menu and tray ---------------------------------------------------------------

    def _recent_chats(self, limit: int = 5) -> list[tuple[Account, str]]:
        merged = [(c.seen, a, c.name) for a in self.accounts for c in a.chats.recent(limit)]
        return [(account, name) for _seen, account, name in sorted(merged, key=lambda r: -r[0])[:limit]]

    def _write_menu_chats(self, force: bool = False) -> None:
        chats = []
        if self.settings["dock_recent_chats"] and not self.settings["privacy_blur"] and not self.lock_enabled:
            chats = [(account.id, name) for account, name in self._recent_chats()]
        set_menu_chats(chats, force=force)

    def _tray_menu(self) -> list[MenuItem]:
        tray = self._from_tray
        if self.window and self.window.get_visible():
            items = [MenuItem(f"Hide {NAME}", self.window.close)]
        else:
            items = [MenuItem(f"Show {NAME}", tray(self.present_window))]
        several = len(self.accounts) > 1
        for account in self.accounts:
            if account.needs_link:
                items.append(MenuItem(f"{account.name}: Needs Linking" if several else "Needs Linking", None,
                                      enabled=False))
        # Only what's reached for in passing. No chats (no names on the panel), no unread count
        # (the icon shows it), and labels that say what they'll do: panels such as Ada's don't
        # draw a menu's checkmarks. The rest is in Preferences.
        if not self.locked:
            blur = self.settings["privacy_blur"]
            items.append(MenuItem("Turn Off Privacy Blur" if blur else "Turn On Privacy Blur",
                                  lambda: self.settings.__setitem__("privacy_blur", not blur)))
        items += [*self._dnd_items(),
                  *([MenuItem(f"Lock {NAME}", self.lock)] if self.lock_enabled and not self.locked else []),
                  MenuItem(separator=True),
                  MenuItem(f"Quit {NAME}", self.quit_natter)]
        return items

    def _from_tray(self, run: Callable[[], None]) -> Callable[[], None]:
        """A tray menu action that brings the window forward, with the panel's activation token."""
        def go() -> None:
            if self.tray and self.tray.last_token and self.window:
                self.window.set_startup_id(self.tray.last_token)
            run()

        return go

    def _refresh_tray(self) -> None:
        """Have the panel fetch the tray menu again, when it would read differently."""
        if self.tray is None:
            return
        shown = [item.key() for item in self._tray_menu()]
        if shown != self._tray_menu_shown:
            self._tray_menu_shown = shown
            self.tray.refresh()

    def _toggle_window(self) -> None:
        if self.window and self.window.get_visible() and self.window.is_active():
            self.window.close()
        else:
            self.present_window()

    def _on_tray_activate(self, token: str) -> None:
        if token and self.window:
            self.window.set_startup_id(token)
        self._toggle_window()

    def _on_tray_host(self, available: bool) -> None:
        self.tray_host = available

    def has_tray_host(self) -> bool:
        """Whether the desktop shows tray icons."""
        if self.tray_host is None:
            self.tray_host = tray_host_available(self.get_dbus_connection())
        return self.tray_host

    def _apply_tray(self) -> None:
        if self.settings["tray_icon"] and self.tray is None:
            self.tray = Tray(self.get_dbus_connection(), str(ICONS), self._on_tray_activate, self._tray_menu,
                             self._on_tray_host)
            self.tray.set_unread(sum(a.unread for a in self.accounts))
        elif not self.settings["tray_icon"] and self.tray is not None:
            self.tray.close()
            self.tray = None

    def _apply_background(self) -> None:
        if self.settings["run_in_background"] and not self._held:
            self.hold()
            self._held = True
        elif not self.settings["run_in_background"] and self._held:
            self.release()
            self._held = False

    def _rename_autostart(self) -> None:
        """An autostart entry written before the rename still says the old name in the desktop's
        startup list: put the current one in, and leave the rest of the entry (an X-GNOME- or
        Hidden= line the desktop's own settings wrote) as it is."""
        if in_sandbox() or not AUTOSTART.exists():
            return
        try:
            text = AUTOSTART.read_text()
        except OSError:
            return
        renamed = re.sub(r"^Name=.*$", f"Name={NAME}", text, count=1, flags=re.M)
        if renamed != text:
            partial = AUTOSTART.with_name(AUTOSTART.name + ".new")
            try:
                partial.write_text(renamed)
                partial.replace(AUTOSTART)
            except OSError:
                partial.unlink(missing_ok=True)

    def _apply_autostart(self) -> None:
        on = self.settings["start_at_login"]
        if in_sandbox():
            # Inside Flatpak, the desktop's Background portal asks and writes the entry.
            def done(granted: bool) -> None:
                if on and not granted:
                    self.settings["start_at_login"] = False
                    if self.window and self.window.get_visible():
                        self.window.toast(f"{NAME} wasn't allowed to start at login")
                    else:  # switched from the tray with the window closed
                        self.tell("autostart", f"{NAME} wasn't allowed to start at login")

            if not request_background("", on, f"Start {NAME} hidden at login, so messages arrive", done) and on:
                self.settings["start_at_login"] = False
                self._no_login_portal()
            return
        if not on:
            AUTOSTART.unlink(missing_ok=True)
            return
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(
            f"[Desktop Entry]\nType=Application\nName={NAME}\nComment=WhatsApp, ready in the background\n"
            f"Exec={autostart_exec(launcher_path())}\nIcon={APP_ID}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")

    def _no_login_portal(self) -> None:
        """No Background portal: the desktop can't start the Flatpak at login, but its own
        startup settings can."""
        command = f"flatpak run {APP_ID} --background"
        alert = Adw.AlertDialog(heading="Can't Start at Login",
                                body="Your desktop can't start Flatpak apps at login. Add this command to its "
                                     "startup settings:")
        # A label of its own, so the command can be selected. It wraps between words only: broken
        # inside the app id, the command reads wrong, so the dialog grows to fit the id instead.
        alert.set_extra_child(Gtk.Label(label=command, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD,
                                        css_classes=["monospace"]))
        alert.add_response("close", "_Close")
        alert.add_response("copy", "Co_py Command")  # "_Close" has C
        alert.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("copy")
        alert.set_close_response("close")

        def answered(_alert, response: str) -> None:
            if response == "copy":
                Gdk.Display.get_default().get_clipboard().set(command)

        alert.connect("response", answered)
        if not (self.window and self.window.get_visible()):
            self.present_window()  # switched from the tray with the window closed: show why it didn't stick
        alert.present(self.window)

    def _apply_spelling(self) -> None:
        self.web_context.set_spell_checking_enabled(self.settings["spellcheck"])
        self.web_context.set_spell_checking_languages(self.settings["spell_languages"])

    def _apply_appearance(self) -> None:
        # WebKit follows the style, so WhatsApp's page gets a matching prefers-color-scheme, and
        # WhatsApp, left on its own "System default", follows that. The theme follows notify::dark.
        ada = self.ada_style.dark if self.ada_style is not None and on_ada() else None
        Adw.StyleManager.get_default().set_color_scheme(color_scheme(self.settings["appearance"], ada))

    def _start_theme(self) -> None:
        """The theme's colours for Natter's own window: above libadwaita's (and Yaru's) style
        sheet, below the application's own CSS (window.py), which uses the same named colours, and
        below the user's gtk.css. Loaded again whenever the style changes, as each theme has a
        light and a dark, and libadwaita's high contrast keeps its own surfaces."""
        self.theme_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.theme_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_THEME + 1)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self._apply_theme_window())
        style.connect("notify::high-contrast", lambda *_: self._apply_theme_window())
        self._apply_theme_window()

    def _apply_theme_window(self) -> None:
        """The theme's colours for Natter's own window, in the style it's in now, and while
        Preferences is open, the Theme picker's swatches (Plain or not; Custom's in the theme made
        from its colour). Those take every theme's colours to work out, so they wait for a picker
        to show them rather than slow the start and the first switch to the other style.
        Libadwaita 1.6 and later (GTK 4.16) read CSS variables too; before that, named colours are
        all GTK understands."""
        style = Adw.StyleManager.get_default()
        dark, high_contrast = style.get_dark(), style.get_high_contrast()
        variables = (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16)
        custom = self.settings["theme_custom"]
        css = themes.gtk_css(themes.chosen(self.settings["theme"]), dark, high_contrast, variables, custom)
        if self.preferences is not None:
            css += themes.swatch_css(dark, high_contrast, custom)
        self.theme_css.load_from_string(css)

    def _apply_theme(self) -> None:
        """Another theme, or another colour for Custom: Natter's window, every account's page, and
        the colour each page shows before WhatsApp paints. Nothing reloads."""
        self._apply_theme_window()
        for account in self.accounts:
            account.apply_theme()
        if self.window is not None:
            self.window.repaint_pages()

    def page_look_changed(self, account: Account, fresh: bool = False) -> None:
        """An account's page said how WhatsApp looks (bridge.js reportLook). Natter only reads
        it: if the theme can't follow WhatsApp there, it comes off that page (and goes back on once
        it can); if WhatsApp has a light or dark look of its own, Natter says how to match."""
        if not account.ready or account.look is None:
            return
        dark = Adw.StyleManager.get_default().get_dark()
        if fresh:
            account.look_dark = dark  # the style Natter had when the page said this
        found = page_look(account.look, dark)
        broken = found == "broken"
        if broken != account.theme_off:
            account.theme_off = broken
            account.apply_theme()
            if broken:
                print(f"natter: {account.name}'s page doesn't draw with the colour names the theme sets; "
                      "WhatsApp's own colours stay there", flush=True)
            else:
                print(f"natter: {account.name}'s page draws with the theme's colour names again", flush=True)
        # A report from before Natter's style changed says "media" until the page's next one (about
        # 2 s later): only one taken under the current style can say WebKit and the window disagree.
        if found == "media" and account.look_dark == dark and f"page-style-{account.id}" not in self._told:
            # WebKit tells the page "dark" whatever libadwaita is asked (GTK_THEME, a -dark GTK
            # theme): nothing Natter can change from here.
            self._told.add(f"page-style-{account.id}")
            print(f"natter: {account.name}'s page was given another style than the window's "
                  "(GTK_THEME or a dark GTK theme?)", flush=True)
        self._show_style_hint()

    def _show_style_hint(self) -> None:
        """The line under Preferences → Style, while any account's WhatsApp has a light or dark
        look of its own, and a toast the first time this run."""
        dark = Adw.StyleManager.get_default().get_dark()
        own = any(a.ready and a.look is not None and a.look_dark == dark and page_look(a.look, dark) == "own"
                  for a in self.accounts)
        hint = style_hint(dark) if own else ""
        if hint == self.style_hint:
            return
        self.style_hint = hint
        if self.preferences is not None:
            self.preferences.show_style_hint(hint)
        if hint and self.window is not None and "whatsapp-look" not in self._told:
            self._told.add("whatsapp-look")
            self.window.toast("WhatsApp has its own light or dark setting", "Details", "app.preferences-page",
                              GLib.Variant("s", "general"), timeout=8)

    def _on_setting(self, key: str, value) -> None:
        if key == "dnd_until":
            self._apply_page_sound()
            self._refresh_tray()
            self._show_banner()
        elif key in ("quiet_hours", "quiet_start", "quiet_end"):
            self._apply_page_sound()
        elif key == "lock_hash":
            self._attempts.succeeded()  # a new or no passcode: wrong tries at the old one don't count
            if not value:
                self._after_unlock = None
                self.settings["locked"] = False
            if not value and self.locked:
                self.locked = False  # the lock was removed
                if self.window:
                    self.window.show_lock(False)
            self._refresh_tray()
            self._write_menu_chats(force=True)
        if key in ("calls_ring", "calls_linked"):
            self._calls_keep_running()
            if self.calls and key == "calls_ring":
                if value and self.settings["calls_linked"]:
                    self.calls.start()
                elif not value and not self.calls.in_call:
                    self.calls.stop()
        if key in MEDIA_KINDS:
            action = self.lookup_action(key)
            if action is not None:
                action.set_state(GLib.Variant("b", bool(self.settings[key])))
            self._apply_media(key)
        if key == "privacy_blur":
            self.lookup_action("privacy-blur").set_state(GLib.Variant("b", value))
            for account in self.accounts:
                account.apply_privacy()
            self._refresh_tray()
            self._write_menu_chats(force=True)
        elif key == "zoom":
            for account in self.accounts:
                account.view.set_zoom_level(value)
        elif key == "appearance":
            self._apply_appearance()
        elif key in ("theme", "theme_custom"):
            self._apply_theme()
        elif key == "hardware_acceleration":
            for account in self.accounts:
                account.set_hardware_acceleration(value)
        elif key == "start_at_login":
            self._apply_autostart()
        if key in ("spellcheck", "spell_languages"):
            self._apply_spelling()
        elif self.window is None:
            return  # the rest is about the window, the tray and the accounts in it
        elif key == "tray_icon":
            self._apply_tray()
        elif key == "run_in_background":
            self._apply_background()
        elif key == "dock_recent_chats":
            self._write_menu_chats(force=True)
        elif key == "light_background":
            self._on_window_visible(self.window.get_visible())
        elif key == "blur_when_away":
            self.on_focus_changed(self.window.is_active())

    # ---- Accounts --------------------------------------------------------------------------

    def add_account(self) -> Optional[Account]:
        if self.window is None and not self._build():
            return None
        taken = {a.id for a in self.accounts}
        n = 2
        while f"account-{n}" in taken:
            n += 1
        info = {"id": f"account-{n}", "name": f"Account {len(self.accounts) + 1}"}
        self.settings["accounts"] = self.settings["accounts"] + [info]
        account = self._make_account(info)
        self.window.show_account(account)
        self.window.toast("Scan the code with WhatsApp on your phone to link this account")
        return account

    def rename_account(self, account: Account, name: str) -> None:
        account.name = name
        self.settings["accounts"] = [dict(a, name=name) if a["id"] == account.id else a
                                     for a in self.settings["accounts"]]
        if self.window:
            self.window.rename_account(account)
        self._refresh_tray()

    def remove_account(self, account: Account) -> None:
        if len(self.accounts) < 2:
            return
        self.notifier.withdraw(account.id)
        self.window.remove_account(account)
        self.accounts.remove(account)
        self._after_call.discard(account)
        for handler in getattr(account, "app_handlers", []):
            account.disconnect(handler)
        self.settings["accounts"] = [a for a in self.settings["accounts"] if a["id"] != account.id]

        def removed() -> None:
            # Only once WebKit has let go of the session's files.
            for folder in (DATA / "accounts" / account.id, CACHE / "accounts" / account.id):
                shutil.rmtree(folder, ignore_errors=True)

        account.remove(removed)
        self._show_unread()
        self._write_menu_chats(force=True)
        self._show_style_hint()  # the hint may have been about this account's page

    # ---- Dialogs ---------------------------------------------------------------------------

    def show_preferences(self, page: str = "") -> None:
        """Preferences, open at `page` (one of PREFERENCES_PAGES), else at the first, General."""
        from .prefs import Preferences

        if self._held_while_locked(lambda: self.show_preferences(page)):
            return
        if not self.present_window():
            return
        preferences = Preferences(self)
        if page:
            preferences.set_visible_page_name(page)
        if self.style_hint:
            preferences.show_style_hint(self.style_hint)
        # Kept while it's open, so a hint that comes or goes meanwhile shows there too.
        self.preferences = preferences
        preferences.connect("closed", lambda dialog: self.preferences is dialog and setattr(self, "preferences", None))
        # Its swatches' colours, before it's first drawn (_apply_theme_window). Once it's closed
        # they stay until the window's colours are next loaded.
        self._apply_theme_window()
        preferences.present(self.window)
        if self._media_banner and page in ("", "general"):
            # General lists what's missing and how to install it: the banner has done its job.
            self._media_banner = False
            self._show_banner()

    def show_shortcuts(self) -> None:
        from .window import shortcuts_dialog

        if self.present_window():
            shortcuts_dialog(self).present(self.window)

    def show_about(self) -> None:
        # DEFAULT_APP_ID's icon, which a lab instance (its own NATTER_APP_ID) has too. The developer
        # is the metainfo's <developer>; Adw.AboutDialog.new_from_appdata would read it, but only
        # from a GResource, and Natter ships none.
        about = Adw.AboutDialog(
            application_name=NAME, application_icon=DEFAULT_APP_ID, version=VERSION,
            developer_name=f"{NAME} contributors", website=WEBSITE, issue_url=f"{WEBSITE}/issues",
            comments="WhatsApp for Linux that feels like part of your desktop",
            license_type=Gtk.License.GPL_3_0, release_notes=RELEASE_NOTES,
            debug_info_filename="natter-debug.txt")
        about.add_legal_section("WhatsApp", None, Gtk.License.CUSTOM,
                                f"{NAME} shows WhatsApp Web. It isn't made by, endorsed by or affiliated with "
                                "WhatsApp or Meta.")
        if self.calls_in_page:
            calls = "in the page (GStreamer WebRTC)"
        else:
            calls = "the call window" if self.calls is not None else "the browser"

        devices = [media_debug(kind, self.media_state(kind), self.camera_fallback) for kind in ("camera", "microphone")]

        def checked(missing: list[str], _hint: str) -> None:
            about.set_debug_info(debug_info(calls, missing, in_sandbox(), devices))

        about.set_debug_info(debug_info(calls, self._media[0] if self._media else None, in_sandbox(), devices))
        if self._media is None:
            self.media(checked)  # the codec check, if it hasn't run yet: it fills in the last line
        about.present(self.window)

    def quit_natter(self) -> None:
        """Quit for real: save what Natter knows and take its notifications and tray icon away."""
        self._quitting = True  # WebKit's processes end now: not a lost network process
        for account in self.accounts:
            account.chats.save()
        self.notifier.withdraw()
        if self.tray:
            self.tray.close()
            self.tray = None
        if self.window:
            self._write_menu_chats(force=True)
            if self.window.get_visible():
                self.window.remember_size()
        self.reclaimer.stop()
        self.call_sound.stop()
        self.audio_follower.stop()
        if self.ada_style is not None:
            self.ada_style.close()
            self.ada_style = None
        if self.desktop_media is not None:
            self.desktop_media.close()
            self.desktop_media = None
        if self.calls:
            self.calls.keep_running = False
            self.calls.stop()
        self.quit()


def main(argv: list[str]) -> int:
    return Natter().run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
