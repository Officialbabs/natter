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
from .account import SCHEME, Account, Files
from .links import chat_link
from .launcher import Badge, set_menu_chats
from .notify import Notifier
from .settings import AUTOSTART, CACHE, DATA, Settings
from .tray import MenuItem, Tray

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent / "data" / "icons"


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
        Adw.Application.do_startup(self)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(ICONS))
        Gtk.Window.set_default_icon_name(APP_ID)

        self.web_context = WebKit.WebContext()
        self.web_context.set_cache_model(WebKit.CacheModel.DOCUMENT_VIEWER)
        self.files = Files()
        self.web_context.register_uri_scheme(SCHEME, self.files.serve)
        security = self.web_context.get_security_manager()
        security.register_uri_scheme_as_secure(SCHEME)
        security.register_uri_scheme_as_cors_enabled(SCHEME)
        self._apply_spelling()

        bus = self.get_dbus_connection()
        self.notifier = Notifier(self.settings, self._open_from_notification)
        self.badge = Badge(bus)
        self._apply_tray()
        self._apply_background()
        self.settings.connect(self._on_setting)
        Gio.NetworkMonitor.get_default().connect("network-changed", self._on_network)

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

    def _build(self) -> None:
        for info in self.settings["accounts"]:
            self._make_account(info)
        from .window import Window

        self.window = Window(self)
        self._show_unread()
        self._update_menu_chats()

    def _make_account(self, info: dict) -> Account:
        account = Account(self, info, url=self.dev_url)
        account.connect("unread-changed", lambda *_: self._show_unread())
        account.connect("chats-changed", lambda *_: self._update_menu_chats())
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

    def go_to_chat(self, files: Optional[list[Path]] = None) -> None:
        from .palette import Palette

        self.present_window()
        Palette(self, files).present(self.window)

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
