"""The Natter application: one instance, one window, and everything that lives outside it."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, WebKit

from . import APP_ID, NAME, VERSION
from . import clipboard
from .account import Account
from .links import chat_link
from .launcher import Badge, set_menu_chats
from .notify import Notifier
from .search import SearchProvider
from .settings import AUTOSTART, CACHE, DATA, Settings
from .system import Reclaimer, move_to_own_scope
from .tray import MenuItem, Tray

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent / "data" / "icons"
MEMORY_LIMIT = 1024  # MB
HIDDEN_BEFORE_RECLAIM = 90  # seconds hidden before Natter gives memory back
RECLAIM_AGAIN = 15 * 60  # and again this often while it stays hidden


class Natter(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.set_application_name(NAME)
        self.settings = Settings()
        self.accounts: list[Account] = []
        self.window = None
        self.tray: Optional[Tray] = None
        self.dev_url: Optional[str] = None
        self._held = False
        self._menu_update = 0
        self._reclaim_timer = 0
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start in the background, without opening the window", None)
        self.add_main_option("chat", ord("c"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open the chat with this name", "NAME")
        self.add_main_option("account", ord("a"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Which account --chat is in (its id)", "ID")
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

        # WebKit frees caches and collects garbage once a page passes a third of this limit.
        # Measured with a real account: 1 GB cuts Natter from ~1.5 GB to ~1.1 GB at the same
        # idle CPU; 640 MB made it collect constantly and end up bigger. 0 turns it off.
        limit = int(GLib.getenv("NATTER_MEMORY_LIMIT") or MEMORY_LIMIT)
        if limit:
            pressure = WebKit.MemoryPressureSettings.new()
            pressure.set_memory_limit(limit)
            self.web_context = WebKit.WebContext(memory_pressure_settings=pressure)
        else:
            self.web_context = WebKit.WebContext()
        # A disk cache, so WhatsApp's code (several MB) isn't downloaded again on every start, and
        # a small memory cache. (DOCUMENT_VIEWER turns both off.)
        self.web_context.set_cache_model(WebKit.CacheModel.DOCUMENT_BROWSER)
        self._apply_spelling()

        bus = self.get_dbus_connection()
        self.notifier = Notifier(self.settings, self._open_from_notification)
        self.badge = Badge(bus)
        self.reclaimer = Reclaimer(on_done=self._reclaimed)
        clipboard.tidy()
        self.search = SearchProvider(self, bus)
        self._apply_tray()
        self._apply_background()
        self.settings.connect(self._on_setting)
        Gio.NetworkMonitor.get_default().connect("network-changed", self._on_network)
        if self.get_flags() & Gio.ApplicationFlags.IS_SERVICE:
            # Started over D-Bus (by a search, say) rather than from a launcher: with no command
            # line coming, bring WhatsApp up hidden, as --background does, so it isn't an empty
            # process the tray and notifications can't use.
            GLib.idle_add(lambda: self.start_hidden() and False)

        for name, run, accels in [
            ("preferences", self.show_preferences, ["<Control>comma"]),
            ("shortcuts", self.show_shortcuts, ["<Control>question"]),
            ("about", self.show_about, []),
            ("add-account", lambda: self.add_account(), []),
            ("show", self.present_window, []),
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
        open_chat.connect("activate", lambda _a, v: self.go_to(self.account_by_id(v[0]), v[1]))
        self.add_action(open_chat)
        open_file = Gio.SimpleAction.new("open-file", GLib.VariantType("s"))
        open_file.connect("activate", lambda _a, v: Gtk.FileLauncher.new(Gio.File.new_for_path(v.get_string()))
                          .launch(self.window, None, None, None))
        self.add_action(open_file)
        if GLib.getenv("NATTER_DEBUG"):
            # gdbus call --session --dest io.natter.Natter --object-path /io/natter/Natter \
            #   --method org.gtk.Actions.Activate debug-snapshot "[<'/tmp/natter.png'>]" "{}"
            snap = Gio.SimpleAction.new("debug-snapshot", GLib.VariantType("s"))
            snap.connect("activate", lambda _a, v: self.window and self.window.save_snapshot(v.get_string()))
            self.add_action(snap)
            # ... debug-eval "[<'return navigator.userAgent'>]" "{}"  prints the result
            run = Gio.SimpleAction.new("debug-eval", GLib.VariantType("s"))
            run.connect("activate", lambda _a, v: self.window and self.window.account and self.window.account.run(
                v.get_string(), None, lambda value: print("natter: eval →", value, flush=True)))
            self.add_action(run)
            # debug-page-snapshot "[<'/tmp/page.png'>]": WebKit paints the page itself (not the screen).
            page = Gio.SimpleAction.new("debug-page-snapshot", GLib.VariantType("s"))
            page.connect("activate", lambda _a, v: self.window and self.window.account and self._page_snapshot(
                self.window.account, v.get_string()))
            self.add_action(page)
            # debug-attach "[<'/path/file.png'>]": what Ctrl+V does with a picture once it's read.
            attach = Gio.SimpleAction.new("debug-attach", GLib.VariantType("s"))
            attach.connect("activate", lambda _a, v: self.window and self.window.account and self.window.account.attach(
                [Path(v.get_string())], lambda ok: print("natter: attached:", ok, flush=True)))
            self.add_action(attach)
            # debug-drop "[<'/path/file.pdf'>]": what dropping that file on the chat does.
            dropped = Gio.SimpleAction.new("debug-drop", GLib.VariantType("s"))
            dropped.connect("activate", lambda _a, v: self.send_files([Path(v.get_string())]))
            self.add_action(dropped)

    def _build(self) -> None:
        for info in self.settings["accounts"]:
            self._make_account(info)
        from .window import Window

        self.window = Window(self)
        self.window.connect("notify::visible", lambda w, _p: self._on_window_visible(w.get_visible()))
        self._on_window_visible(False)  # started hidden (--background) counts as hidden; showing cancels it
        self._show_unread()
        self._update_menu_chats()

    def _make_account(self, info: dict) -> Account:
        account = Account(self, info, url=self.dev_url)
        account.connect("unread-changed", lambda *_: self._show_unread())
        account.connect("chats-changed", lambda *_: self._update_menu_chats())
        account.connect("state-changed", lambda a: a.ready and self._update_menu_chats())
        account.connect("notify-message", self._on_notify_message)
        account.connect("unnotify", lambda a, page_id: self.notifier.withdraw_page(a.id, page_id))
        account.connect("download-done", self._on_download)
        self.accounts.append(account)
        return account

    # ---- Command line: first start and every later launch ---------------------------------

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        first = self.window is None
        if first:
            self.dev_url = options.get("url")
            self._build()
        links, files = [], []
        for arg in options.get(GLib.OPTION_REMAINING, []) or []:
            if chat_link(arg):
                links.append(arg)
                continue
            gfile = command_line.create_file_for_arg(arg)
            if gfile.get_path() and Path(gfile.get_path()).is_file():
                files.append(Path(gfile.get_path()))
            else:
                command_line.printerr(f"natter: not a WhatsApp link or a file: {arg}\n")

        background = first and options.get("background") and not (links or files or options.get("chat"))
        if not background:
            self.present_window()
        account = self.account_by_id(options.get("account")) or (self.window.account if self.window else None)
        if options.get("chat") and account:
            self.go_to(account, options["chat"])
        for link in links:
            (self.window.account or self.accounts[0]).open_link(link)
        if files:
            self.go_to_chat(files)
        return 0

    def present_window(self) -> None:
        if self.window is None:
            self._build()
        self.window.present()

    def account_by_id(self, account_id: Optional[str]) -> Optional[Account]:
        return next((a for a in self.accounts if a.id == account_id), None)

    # ---- Chats -----------------------------------------------------------------------------

    def go_to_chat(self, files: Optional[list[Path]] = None, query: str = "") -> None:
        from .palette import Palette

        self.present_window()
        Palette(self, files, query).present(self.window)

    def start_hidden(self) -> None:
        """Bring WhatsApp up without a window: started for a search, say."""
        if self.window is None:
            self._build()

    def go_to(self, account: Optional[Account], name: str, files: Optional[list[Path]] = None) -> None:
        if account is None:
            return
        self.present_window()
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
            self.window.show_unread(total)

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

    def _on_download(self, _account: Account, path: str, _ok: bool) -> None:
        name = Path(path).name
        if self.window and self.window.get_visible():
            self.window.toast(f"Saved “{name}”", "Open", "app.open-file", GLib.Variant("s", path))

    def _on_network(self, monitor: Gio.NetworkMonitor, available: bool) -> None:
        if available:
            for account in self.accounts:
                if account.failed:
                    account.reload()

    # ---- Memory while hidden --------------------------------------------------------------

    def _on_window_visible(self, visible: bool) -> None:
        if self._reclaim_timer:
            GLib.source_remove(self._reclaim_timer)
            self._reclaim_timer = 0
        if visible:
            self.reclaimer.stop()
        elif self.settings["light_background"] and self.reclaimer.available:
            self._reclaim_timer = GLib.timeout_add_seconds(HIDDEN_BEFORE_RECLAIM, self._reclaim)

    def _reclaim(self) -> bool:
        self.reclaimer.start()
        self._reclaim_timer = GLib.timeout_add_seconds(RECLAIM_AGAIN, self._reclaim)
        return GLib.SOURCE_REMOVE

    def _reclaimed(self, before: int, after: int) -> bool:
        if GLib.getenv("NATTER_DEBUG"):
            print(f"natter: gave back {(before - after) >> 20} MB ({before >> 20} → {after >> 20} MB)", flush=True)
        return GLib.SOURCE_REMOVE

    # ---- Dock menu and tray ---------------------------------------------------------------

    def _update_menu_chats(self) -> None:
        if self._menu_update:
            return

        def write() -> bool:
            self._menu_update = 0
            chats = []
            if self.settings["dock_recent_chats"]:
                merged = [(c.seen, a.id, c.name) for a in self.accounts for c in a.chats.recent(5)]
                chats = [(account_id, name) for _seen, account_id, name in sorted(merged, reverse=True)[:5]]
            set_menu_chats(chats)
            return GLib.SOURCE_REMOVE

        self._menu_update = GLib.timeout_add_seconds(3, write)

    def _tray_menu(self) -> list[MenuItem]:
        visible = bool(self.window and self.window.get_visible())
        items = [MenuItem(f"Hide {NAME}" if visible else f"Show {NAME}", self._toggle_window)]
        total = sum(a.unread for a in self.accounts)
        if total:
            items.append(MenuItem(f"{total} unread", None, enabled=False))
        recent = [(c.seen, a, c.name) for a in self.accounts for c in a.chats.recent(5)]
        recent = sorted(recent, key=lambda r: -r[0])[:5]
        if recent:
            items.append(MenuItem(separator=True))
            for _seen, account, name in recent:
                items.append(MenuItem(name, lambda a=account, n=name: self.go_to(a, n)))
        items += [MenuItem(separator=True),
                  MenuItem("Go to Chat…", lambda: self.go_to_chat()),
                  MenuItem("Preferences", self.show_preferences),
                  MenuItem(separator=True),
                  MenuItem(f"Quit {NAME}", self.quit_natter)]
        return items

    def _toggle_window(self) -> None:
        if self.window and self.window.get_visible() and self.window.is_active():
            self.window.close()
        else:
            self.present_window()

    def _apply_tray(self) -> None:
        if self.settings["tray_icon"] and self.tray is None:
            self.tray = Tray(self.get_dbus_connection(), str(ICONS), self._toggle_window, self._tray_menu)
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
        if not self.settings["start_at_login"]:
            AUTOSTART.unlink(missing_ok=True)
            return
        launcher = shutil.which("natter") or str(HERE.parent / "bin" / "natter")
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(
            "[Desktop Entry]\nType=Application\nName=Natter\nComment=WhatsApp, ready in the background\n"
            f"Exec={launcher} --background\nIcon={APP_ID}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")

    def _apply_spelling(self) -> None:
        self.web_context.set_spell_checking_enabled(self.settings["spellcheck"])
        self.web_context.set_spell_checking_languages(self.settings["spell_languages"])

    def _on_setting(self, key: str, value) -> None:
        if key == "privacy_blur":
            self.lookup_action("privacy-blur").set_state(GLib.Variant("b", value))
            for account in self.accounts:
                account.apply_privacy()
        elif key == "zoom":
            for account in self.accounts:
                account.view.set_zoom_level(value)
        elif key == "tray_icon":
            self._apply_tray()
        elif key == "run_in_background":
            self._apply_background()
        elif key == "start_at_login":
            self._apply_autostart()
        elif key in ("spellcheck", "spell_languages"):
            self._apply_spelling()
        elif key == "dock_recent_chats":
            self._update_menu_chats()
        elif key == "light_background" and self.window:
            self._on_window_visible(self.window.get_visible())
        elif key == "blur_when_away" and self.window:
            self.on_focus_changed(self.window.is_active())

    # ---- Accounts --------------------------------------------------------------------------

    def add_account(self) -> Account:
        taken = {a.id for a in self.accounts}
        n = 2
        while f"account-{n}" in taken:
            n += 1
        info = {"id": f"account-{n}", "name": f"Account {len(self.accounts) + 1}"}
        self.settings["accounts"] = self.settings["accounts"] + [info]
        account = self._make_account(info)
        if self.window:
            self.window.add_account(account)
            self.window.show_account(account)
            self.window.toast("Scan the code with WhatsApp on your phone to link this account")
        return account

    def rename_account(self, account: Account, name: str) -> None:
        account.name = name
        self.settings["accounts"] = [dict(a, name=name) if a["id"] == account.id else a
                                     for a in self.settings["accounts"]]
        if self.window:
            self.window.rename_account(account)

    def remove_account(self, account: Account) -> None:
        if len(self.accounts) < 2:
            return
        self.notifier.withdraw(account.id)
        if self.window:
            self.window.remove_account(account)
        self.accounts.remove(account)
        self.settings["accounts"] = [a for a in self.settings["accounts"] if a["id"] != account.id]
        account.view.terminate_web_process()
        for folder in (DATA / "accounts" / account.id, CACHE / "accounts" / account.id):
            shutil.rmtree(folder, ignore_errors=True)
        self._show_unread()
        self._update_menu_chats()

    # ---- Dialogs ---------------------------------------------------------------------------

    def show_preferences(self) -> None:
        from .prefs import Preferences

        self.present_window()
        Preferences(self).present(self.window)

    def show_shortcuts(self) -> None:
        dialog = Adw.ShortcutsDialog()
        for title, rows in [
            ("Natter", [("Go to chat, or message a number", "<Control>k"),
                        ("Privacy blur", "<Control><Shift>b"),
                        ("Switch account", "<Control>1"),
                        ("Preferences", "<Control>comma"),
                        ("Hide window", "<Control>w"),
                        ("Quit", "<Control>q")]),
            ("View", [("Zoom in", "<Control>plus"), ("Zoom out", "<Control>minus"),
                      ("Reset zoom", "<Control>0"), ("Reload WhatsApp", "<Control>r")]),
            ("WhatsApp", [("New chat", "<Control><Alt>n"), ("Search chats", "<Control><Alt>slash"),
                          ("Next chat", "<Control><Alt><Shift>bracketright"),
                          ("Previous chat", "<Control><Alt><Shift>bracketleft"),
                          ("Archive chat", "<Control><Alt>e"), ("Mute chat", "<Control><Alt><Shift>m")]),
        ]:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in rows:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self.window)

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=NAME, application_icon=APP_ID, version=VERSION,
            developer_name="Natter contributors",
            comments="WhatsApp for Linux that feels like part of your desktop.\n\n"
                     "Natter shows WhatsApp Web. It isn't made by or affiliated with WhatsApp or Meta.",
            license_type=Gtk.License.GPL_3_0)
        about.present(self.window)

    def quit_natter(self) -> None:
        for account in self.accounts:
            account.chats.save()
        self.notifier.withdraw()
        if self.tray:
            self.tray.close()
        if self.window and self.window.get_visible():
            self.window.remember_size()
        self.quit()


def main(argv: list[str]) -> int:
    return Natter().run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
