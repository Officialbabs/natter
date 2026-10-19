"""The Natter application: one instance, one window, and everything that lives outside it."""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Iterable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, WebKit

from . import APP_ID, DEFAULT_APP_ID, NAME, OLD_APP_ID, VERSION, WHATSAPP_URL
from . import clipboard, rtc
from .account import Account
from .calls import CallWindow, available as calls_available
from .links import chat_link
from .launcher import Badge, chat_key, exec_quote, set_menu_chats
from .notify import Notifier
from .search import SearchProvider
from .settings import AUTOSTART, CACHE, CONFIG, DATA, Chat, ChatIndex, Settings, private_folder
from .system import CallSound, Reclaimer, in_sandbox, media_report, move_to_own_scope, request_background
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
    return Gio.FileIcon.new(Gio.File.new_for_path(str(svg))) if svg.is_file() else Gio.ThemedIcon.new(APP_ID)
MEMORY_LIMIT = 1536  # MB
MEMORY_THRESHOLDS = (0.55, 0.8)  # conservative, strict: fractions of the limit
MEMORY_POLL = 30  # seconds between WebKit's memory checks
HIDDEN_BEFORE_RECLAIM = 90  # seconds hidden before Natter gives memory back
RECLAIM_AGAIN = 15 * 60  # and again this often while it stays hidden
SEARCH_LINGER = 30_000  # ms a Natter started only for desktop search stays after the last search
MEDIA_CHECK_DELAY = 10  # seconds after WhatsApp is up, so the check doesn't slow its start
SNAPSHOTS = CACHE / "snapshots"  # the only folder debug snapshots are saved to
CALLS_START_DELAY = 15  # seconds after WhatsApp is up before the call window starts, to ring for calls
CALL_FAILURES = {
    "no-chat": "The call window couldn't find “{chat}” in its chats",
    "no-call-button": "WhatsApp doesn't offer a call for “{chat}”",
    "start": "Natter's call window couldn't start",
}
CALL_FAILURES_IN_PAGE = {
    "no-chat": "Open a chat first, then call",
    "no-call-button": "WhatsApp doesn't offer a call here",
    "unavailable": "WhatsApp doesn't offer calls here yet",
}
APPEARANCES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT,
               "dark": Adw.ColorScheme.FORCE_DARK}


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
        return None, "natter: --url ignored: Natter is already running, and only takes it when it starts"
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
        self._media: Optional[tuple[list[str], str]] = None
        self._media_waiting: list[Callable[[list[str], str], None]] = []
        self._media_checked = False
        self._told: set[str] = set()  # one-time notices already given this session
        self._linking: set[str] = set()  # accounts whose "needs linking" was seen or announced
        self._tray_menu_shown: list[tuple] = []
        self.calls: Optional[CallWindow] = None
        # Calls made by WhatsApp in Natter's own page, its data channels done by GStreamer (rtc.py).
        self.calls_in_page = False
        self._pending_call: Optional[tuple[str, bool]] = None  # (chat, video) waiting for the call window
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start in the background, without opening the window", None)
        self.add_main_option("chat", ord("c"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open the chat with this name", "NAME")
        self.add_main_option("account", ord("a"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Which account --chat is in (its id)", "ID")
        self.add_main_option("chat-id", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open a chat from Natter's dock menu", "KEY")
        self.add_main_option("url", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Load this page instead of WhatsApp Web (for development)", "URL")
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Show Natter's version", None)
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
        Adw.Application.do_startup(self)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(ICONS))
        Gtk.Window.set_default_icon_name(APP_ID)
        self._apply_appearance()
        service = bool(self.get_flags() & Gio.ApplicationFlags.IS_SERVICE)
        self.calls_in_page = not service and GLib.getenv("NATTER_CALLS") != "window" and rtc.available()
        self._make_web_context()
        self._apply_spelling()

        bus = self.get_dbus_connection()
        self.notifier = Notifier(self, self._open_from_notification)
        self.badge = Badge(bus)
        self.reclaimer = Reclaimer(on_done=self._reclaimed)
        self.call_sound = CallSound()
        clipboard.tidy()
        self.settings.connect(self._on_setting)
        Gio.NetworkMonitor.get_default().connect("network-changed", self._on_network)
        if calls_available() and not self.calls_in_page and not self.get_flags() & Gio.ApplicationFlags.IS_SERVICE:
            self.calls = CallWindow(DATA / "calls", debug=bool(GLib.getenv("NATTER_DEBUG")))
            self.calls.connect("state-changed", self._on_calls_state)
            self.calls.connect("call-failed", self._on_call_failed)
            self.calls.connect("incoming", self._on_incoming_call)
            self.calls.connect("incoming-ended", lambda *_: self.withdraw_notification("call"))
            self.calls.connect("in-call", self._on_in_call)
            self._calls_keep_running()
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
            ("call-window", lambda: self.calls.show() if self.calls else self.call_in_browser(), []),
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
                                     lambda: self.settings["search_provider"] and not self.settings["privacy_blur"])
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
            self.web_context = WebKit.WebContext(memory_pressure_settings=pressure)
        else:
            self.web_context = WebKit.WebContext()
        # Now that the process cache is sized (to none), a web browser's caches: 1 GB on disk and
        # 128 MB in memory, so WhatsApp's code isn't fetched again on every start. Its page was
        # ready in ~4.6 s instead of ~15.8 s with DOCUMENT_BROWSER's small ones.
        self.web_context.set_cache_model(WebKit.CacheModel.WEB_BROWSER)

    def _build(self, hidden: bool = False) -> bool:
        """The window first, then WhatsApp in it. If the window can't be made, Natter quits
        rather than run WhatsApp where nobody can see it."""
        from .window import Window

        bus = self.get_dbus_connection()
        if old_natter_running(bus):
            # Its session is this one's: say so, and bring that Natter forward instead of opening it twice.
            title = "The old Natter is still running"
            body = ("It uses the same WhatsApp session. Quit it (its tray icon → Quit, or Ctrl+Q in its "
                    "window) and start Natter again.")
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
        self.window.connect("notify::visible", lambda w, _p: self._on_window_visible(w.get_visible()))
        self.settle_first_run(hidden)
        self._apply_tray()
        self._apply_background()
        self._on_window_visible(False)  # started hidden (--background) counts as hidden; showing cancels it
        self._show_unread()
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
        account.connect("unread-changed", lambda *_: self._show_unread())
        account.connect("chats-changed", lambda *_: self._refresh_tray())
        account.connect("state-changed", self._on_account_state)
        account.connect("notify-message", self._on_notify_message)
        account.connect("unnotify", lambda a, page_id: self.notifier.withdraw_page(a.id, page_id))
        account.connect("download-done", self._on_download)
        account.connect("call-requested", lambda a, video: self.start_call(a, video))
        account.connect("in-call", self._on_page_call)
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
            if chat_link(arg):
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
                self.window.toast("That chat isn't in Natter's list any more")
        for link in links:
            (self.window.account or self.accounts[0]).open_link(link)
        if files:
            self.go_to_chat(files)
        return 0

    def present_window(self) -> bool:
        if self.window is None and not self._build():
            return False
        self.window.present()
        return True

    def account_by_id(self, account_id: Optional[str]) -> Optional[Account]:
        return next((a for a in self.accounts if a.id == account_id), None)

    # ---- Chats -----------------------------------------------------------------------------

    def go_to_chat(self, files: Optional[list[Path]] = None, query: str = "") -> None:
        from .palette import Palette

        if self.present_window():
            Palette(self, files, query).present(self.window)

    def open_chat(self, account_id: str, name: str) -> None:
        """Open a chat given by account id: from desktop search or the open-chat action."""
        if self.window is None and not self._build():
            return
        self.go_to(self.account_by_id(account_id), name)

    def go_to(self, account: Optional[Account], name: str, files: Optional[list[Path]] = None) -> None:
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
        Gtk.UriLauncher.new(WHATSAPP_URL).launch(self.window, None, None, None)
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
            self.window.toast("Natter's call window opens. Link it once with your phone, like a new device.", timeout=8)
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
            self.tell("calls-link", "Natter's call window needs linking again",
                      "Calls won't ring until you link it. Start a call in Natter to see the code.")

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

    def _on_incoming_call(self, _calls: CallWindow, caller: str, video: bool) -> None:
        kind = "video call" if video else "voice call"
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
        if not account.needs_link:
            self._linking.discard(account.id)
        elif account.id not in self._linking:
            self._linking.add(account.id)
            if hidden:
                self.tell(f"link-{account.id}", "WhatsApp needs to be linked again",
                          f"Open Natter and scan the code with your phone ({account.name})."
                          if len(self.accounts) > 1 else "Open Natter and scan the code with your phone.")
        if account.ready:
            self._check_media()
            if (self.calls and account is self.accounts[0] and self.calls.keep_running
                    and not self.calls.running and "calls-start" not in self._told):
                self._told.add("calls-start")
                GLib.timeout_add_seconds(CALLS_START_DELAY, lambda: self.calls.keep_running and self.calls.start() and False)

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
        account = self.account_by_id(account_id)
        if account is None:
            return
        if token:
            self.window.set_startup_id(token)
        self.present_window()
        self.window.show_account(account)
        account.click_notification(page_id, title)

    def on_focus_changed(self, active: bool) -> None:
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
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.window, None, None, None)

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
            if missing and self.window:
                self.window.toast("Some videos and voice messages can't play here", "Details",
                                  "app.preferences", timeout=0)

        def check() -> bool:
            self.media(found)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add_seconds(MEDIA_CHECK_DELAY, check)

    # ---- Memory while hidden --------------------------------------------------------------

    def _on_window_visible(self, visible: bool) -> None:
        if self._reclaim_timer:
            GLib.source_remove(self._reclaim_timer)
            self._reclaim_timer = 0
        self._refresh_tray()
        if visible:
            self.reclaimer.stop()
            self._check_media()
            return
        self._write_menu_chats()
        if self.settings["light_background"] and self.reclaimer.available and not self.calling:
            self._reclaim_timer = GLib.timeout_add_seconds(HIDDEN_BEFORE_RECLAIM, self._reclaim)

    @property
    def calling(self) -> bool:
        """A call is on in one of the accounts' pages."""
        return any(account.calling for account in self.accounts)

    def _on_page_call(self, _account: Account, _active: bool) -> None:
        # During a call nothing squeezes the page: no reclaim, and a sound stream the desktop
        # can see (see CallSound) so it isn't paused either. Afterwards, as before.
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

    def _reclaim(self) -> bool:
        self.reclaimer.start()
        self._reclaim_timer = GLib.timeout_add_seconds(RECLAIM_AGAIN, self._reclaim)
        return GLib.SOURCE_REMOVE

    def _reclaimed(self, before: int, after: int) -> bool:
        if GLib.getenv("NATTER_DEBUG"):
            print(f"natter: gave back {(before - after) >> 20} MB ({before >> 20} → {after >> 20} MB)", flush=True)
        return GLib.SOURCE_REMOVE

    # ---- Closing the window ----------------------------------------------------------------

    def window_closed(self) -> None:
        """The window was closed with Natter set to keep running."""
        if "still-running" in self._told or (self.tray and self.has_tray_host()):
            return
        self._told.add("still-running")
        self.tell("still-running", "Natter is still running — open it from your apps")

    # ---- Dock menu and tray ---------------------------------------------------------------

    def _recent_chats(self, limit: int = 5) -> list[tuple[Account, str]]:
        merged = [(c.seen, a, c.name) for a in self.accounts for c in a.chats.recent(limit)]
        return [(account, name) for _seen, account, name in sorted(merged, key=lambda r: -r[0])[:limit]]

    def _write_menu_chats(self, force: bool = False) -> None:
        chats = []
        if self.settings["dock_recent_chats"] and not self.settings["privacy_blur"]:
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
                items.append(MenuItem(f"{account.name} needs linking" if several else "Needs linking", None,
                                      enabled=False))
        total = sum(a.unread for a in self.accounts)
        if total:
            items.append(MenuItem(f"{total} unread", None, enabled=False))
        recent = self._recent_chats()
        if recent:
            items.append(MenuItem(separator=True))
            if self.settings["privacy_blur"]:
                items.append(MenuItem("Recent chats hidden", None, enabled=False))
            else:
                items += [MenuItem(name, tray(lambda a=account, n=name: self.go_to(a, n))) for account, name in recent]
        items += [MenuItem(separator=True),
                  MenuItem("Go to Chat…", tray(self.go_to_chat)),
                  MenuItem("Preferences", tray(self.show_preferences)),
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
        shown = [(item.label, item.separator, item.enabled) for item in self._tray_menu()]
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

    def _apply_autostart(self) -> None:
        on = self.settings["start_at_login"]
        if in_sandbox():
            # Inside Flatpak, the desktop's Background portal asks and writes the entry.
            def done(granted: bool) -> None:
                if on and not granted:
                    self.settings["start_at_login"] = False
                    if self.window:
                        self.window.toast("Natter wasn't allowed to start at login")

            if not request_background("", on, "Start Natter hidden at login, so messages arrive", done) and on:
                self.settings["start_at_login"] = False
                self._no_login_portal()
            return
        if not on:
            AUTOSTART.unlink(missing_ok=True)
            return
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(
            "[Desktop Entry]\nType=Application\nName=Natter\nComment=WhatsApp, ready in the background\n"
            f"Exec={autostart_exec(launcher_path())}\nIcon={APP_ID}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")

    def _no_login_portal(self) -> None:
        """No Background portal: the desktop can't start the Flatpak at login, but its own
        startup settings can."""
        command = f"flatpak run {APP_ID} --background"
        shown = command.replace("-", "-⁠")  # word joiners: a line may break before "--background", not inside it
        alert = Adw.AlertDialog(heading="Your desktop can't start Flatpak apps at login",
                                body="To start Natter at login anyway, add this command to your desktop's own "
                                     f"startup settings:\n\n{shown}")
        alert.add_response("close", "Close")
        alert.add_response("copy", "Copy Command")
        alert.set_default_response("copy")

        def answered(_alert, response: str) -> None:
            if response == "copy":
                Gdk.Display.get_default().get_clipboard().set(command)

        alert.connect("response", answered)
        alert.present(self.window)

    def _apply_spelling(self) -> None:
        self.web_context.set_spell_checking_enabled(self.settings["spellcheck"])
        self.web_context.set_spell_checking_languages(self.settings["spell_languages"])

    def _apply_appearance(self) -> None:
        # WebKit follows the style, so WhatsApp's page gets a matching prefers-color-scheme.
        scheme = APPEARANCES.get(self.settings["appearance"], Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _on_setting(self, key: str, value) -> None:
        if key in ("calls_ring", "calls_linked"):
            self._calls_keep_running()
            if self.calls and key == "calls_ring":
                if value and self.settings["calls_linked"]:
                    self.calls.start()
                elif not value and not self.calls.in_call:
                    self.calls.stop()
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
        elif key == "hardware_acceleration":
            for account in self.accounts:
                account.set_hardware_acceleration(value)
        elif key == "start_at_login":
            self._apply_autostart()
        elif key in ("spellcheck", "spell_languages"):
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
        self.settings["accounts"] = [a for a in self.settings["accounts"] if a["id"] != account.id]

        def removed() -> None:
            # Only once WebKit has let go of the session's files.
            for folder in (DATA / "accounts" / account.id, CACHE / "accounts" / account.id):
                shutil.rmtree(folder, ignore_errors=True)

        account.remove(removed)
        self._show_unread()
        self._write_menu_chats(force=True)

    # ---- Dialogs ---------------------------------------------------------------------------

    def show_preferences(self) -> None:
        from .prefs import Preferences

        if self.present_window():
            Preferences(self).present(self.window)

    def show_shortcuts(self) -> None:
        from .window import shortcuts_dialog

        if self.present_window():
            shortcuts_dialog().present(self.window)

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=NAME, application_icon=APP_ID, version=VERSION,
            developer_name="Natter contributors",
            comments="WhatsApp for Linux that feels like part of your desktop.\n\n"
                     "Natter shows WhatsApp Web. It isn't made by or affiliated with WhatsApp or Meta.",
            license_type=Gtk.License.GPL_3_0)
        about.present(self.window)

    def quit_natter(self) -> None:
        """Quit for real: save what Natter knows and take its notifications and tray icon away."""
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
        if self.calls:
            self.calls.keep_running = False
            self.calls.stop()
        self.quit()


def main(argv: list[str]) -> int:
    return Natter().run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
