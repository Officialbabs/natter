"""Preferences: everything is applied the moment it's changed."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import APP_ID
from .launcher import menu_supported
from .settings import parse_clock
from .system import in_sandbox

SYNC = GObject.BindingFlags.SYNC_CREATE
APPEARANCES = [("system", "Follow System"), ("light", "Light"), ("dark", "Dark")]
# Desktops that start what's in ~/.config/autostart themselves, as XDG_CURRENT_DESKTOP names them.
AUTOSTART_DESKTOPS = {"ada", "budgie", "budgie-desktop", "cinnamon", "x-cinnamon", "cosmic", "dde", "deepin",
                      "enlightenment", "gnome", "kde", "lxde", "lxqt", "mate", "pantheon", "ukui", "unity", "xfce"}


def runs_autostart(desktop: str) -> bool:
    """Whether the desktop (XDG_CURRENT_DESKTOP, e.g. "ubuntu:GNOME") starts apps at login by
    itself. Sway, Hyprland, i3 and most window managers don't: they run what their config says."""
    return any(name.strip().lower() in AUTOSTART_DESKTOPS for name in (desktop or "").split(":"))


class Preferences(Adw.PreferencesDialog):
    def __init__(self, app) -> None:
        super().__init__(search_enabled=False)
        self.app = app
        self.settings = app.settings
        self.add(self._general())
        self.add(self._notifications())
        self.add(self._privacy())
        self.add(self._accounts())

    def _switch(self, key: str, title: str, subtitle: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.settings[key]))
        row.connect("notify::active", lambda r, _p: self.settings.__setitem__(key, r.get_active()))
        return row

    # ---- Pages -----------------------------------------------------------------------------

    def _general(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.media = Adw.PreferencesGroup(visible=False)
        page.add(self.media)
        self.app.media(self._show_media)

        tray_host = self.app.has_tray_host()
        running = Adw.PreferencesGroup(title="Running")
        running.add(self._switch("run_in_background", "Keep Running When Closed",
                                 "Closing the window leaves Natter in the tray, still notifying you" if tray_host else
                                 "Closing the window keeps Natter running, still notifying you. "
                                 "Open it again from your apps"))
        login = self._switch("start_at_login", "Start at Login", "Starts hidden, ready to notify you")
        if not runs_autostart(GLib.getenv("XDG_CURRENT_DESKTOP") or ""):
            command = f"flatpak run {APP_ID} --background" if in_sandbox() else "natter --background"
            login.set_subtitle(f"Your desktop may not start apps by itself: add “exec {command}” to its config")
        running.add(login)
        if self.app.reclaimer.available:
            running.add(self._switch("light_background", "Use Less Memory in the Background",
                                     "While the window is closed, Natter hands memory it isn't using back to the system"))
        tray = self._switch("tray_icon", "Tray Icon", "Shows the unread count; click it to open Natter")
        if not tray_host:
            tray.set_sensitive(False)
            tray.set_subtitle("Your desktop doesn't show tray icons")
        running.add(tray)
        if menu_supported():
            running.add(self._switch("dock_recent_chats", "Recent Chats in Dock Menu",
                                     "Right-click Natter's dock icon to jump straight to a chat"))
        running.add(self._switch("search_provider", "Chats in Desktop Search",
                                 "Lets the desktop's search find chats by name, on desktops that ask apps for results"))
        page.add(running)

        look = Adw.PreferencesGroup(title="Appearance")
        keys = [key for key, _ in APPEARANCES]
        style = Adw.ComboRow(title="Appearance", model=Gtk.StringList.new([label for _, label in APPEARANCES]))
        style.set_selected(keys.index(self.settings["appearance"]) if self.settings["appearance"] in keys else 0)
        style.connect("notify::selected", lambda r, _p: self.settings.__setitem__("appearance", keys[r.get_selected()]))
        look.add(style)
        zoom = Adw.SpinRow.new_with_range(50, 200, 10)
        zoom.set_title("Zoom")
        zoom.set_subtitle("Ctrl+plus and Ctrl+minus change it too")
        zoom.set_value(round(self.settings["zoom"] * 100))
        zoom.connect("notify::value", lambda r, _p: self.settings.__setitem__("zoom", round(r.get_value() / 100, 2)))
        look.add(zoom)
        page.add(look)

        spelling = Adw.PreferencesGroup(title="Spelling")
        spell = self._switch("spellcheck", "Spell Check")
        spelling.add(spell)
        languages = Adw.EntryRow(title="Spell Check Languages (e.g. en_US, pt_BR)",
                                 text=", ".join(self.settings["spell_languages"]), show_apply_button=True)
        languages.connect("apply", lambda r: self.settings.__setitem__(
            "spell_languages", [p.strip() for p in r.get_text().replace(";", ",").split(",") if p.strip()]))
        spell.bind_property("active", languages, "sensitive", SYNC)
        spelling.add(languages)
        page.add(spelling)

        files = Adw.PreferencesGroup(title="Downloads")
        self.folder = Adw.ActionRow(title="Save Files To", subtitle=str(self.settings.download_dir))
        choose = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_folder)
        self.folder.add_suffix(choose)
        files.add(self.folder)
        page.add(files)

        trouble = Adw.PreferencesGroup(title="Troubleshooting")
        trouble.add(self._switch("hardware_acceleration", "Hardware Acceleration",
                                 "Turn off if WhatsApp's page is blank or flickers"))
        page.add(trouble)
        return page

    def _show_media(self, missing: list[str], hint: str) -> None:
        """What's missing to play WhatsApp's videos and voice messages, and the command that adds it.
        A hint with nothing missing says why Natter couldn't check."""
        if missing:
            about = f"Some videos and voice messages can't play. Missing: {', '.join(missing)}."
            subtitle = f"{about}\nInstall them with: {hint}" if hint else about
        elif hint:
            subtitle = hint
        else:
            return
        row = Adw.ActionRow(title="Media", subtitle=subtitle, subtitle_selectable=True, use_markup=False)
        row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        if missing and hint:
            copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy Command", valign=Gtk.Align.CENTER,
                              css_classes=["flat"])
            copy.connect("clicked", lambda button: button.get_clipboard().set(hint))
            row.add_suffix(copy)
        self.media.add(row)
        self.media.set_visible(True)

    def _notifications(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Notifications", icon_name="preferences-system-notifications-symbolic")
        group = Adw.PreferencesGroup(
            description="One notification per chat, replaced as new messages come in. Clicking it opens that chat. "
                        "Sounds follow WhatsApp's own setting, under Settings → Notifications in WhatsApp.")
        shown = self._switch("notifications", "Show Notifications")
        group.add(shown)
        sender = self._switch("notify_sender", "Show Who It's From", "Their name and picture")
        preview = self._switch("notify_preview", "Show Message Text")
        for row in (sender, preview):
            shown.bind_property("active", row, "sensitive", SYNC)
            group.add(row)
        page.add(group)

        quiet = Adw.PreferencesGroup(title="Quiet Hours",
                                     description="No notifications in this window. The unread count still updates.")
        expander = Adw.ExpanderRow(title="Quiet Hours", show_enable_switch=True,
                                   enable_expansion=bool(self.settings["quiet_hours"]), expanded=True)
        expander.connect("notify::enable-expansion",
                         lambda r, _p: self.settings.__setitem__("quiet_hours", r.get_enable_expansion()))
        for key, title in (("quiet_start", "From"), ("quiet_end", "Until")):
            row = Adw.EntryRow(title=title, text=self.settings[key], show_apply_button=True)
            row.connect("apply", self._set_clock, key)
            row.connect("changed", lambda r: r.remove_css_class("error"))
            expander.add_row(row)
        quiet.add(expander)
        page.add(quiet)
        return page

    def _privacy(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Privacy", icon_name="view-conceal-symbolic")
        group = Adw.PreferencesGroup(description="For working in public or sharing your screen.")
        group.add(self._switch("privacy_blur", "Privacy Blur",
                               "Blurs names, pictures and messages until you point at them. Ctrl+Shift+B"))
        group.add(self._switch("blur_when_away", "Blur When in the Background",
                               "Blurs everything while another window is active"))
        page.add(group)
        return page

    def _accounts(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Accounts", icon_name="system-users-symbolic")
        self.account_group = Adw.PreferencesGroup(
            title="Accounts",
            description="Each account has its own WhatsApp session. Link one by scanning its code with your phone. "
                        "Ctrl+1, Ctrl+2… switch between them.")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Account", css_classes=["flat"],
                         valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: (self.app.activate_action("add-account", None), self._fill_accounts()))
        self.account_group.set_header_suffix(add)
        self.account_rows: list[Gtk.Widget] = []
        self._fill_accounts()
        page.add(self.account_group)
        return page

    def _fill_accounts(self) -> None:
        for row in self.account_rows:
            self.account_group.remove(row)
        self.account_rows = []
        for account in self.app.accounts:
            row = Adw.EntryRow(title="Name", text=account.name, show_apply_button=True)
            row.connect("apply", lambda r, a=account: self.app.rename_account(a, r.get_text().strip() or a.name))
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove Account",
                                valign=Gtk.Align.CENTER, css_classes=["flat"],
                                sensitive=len(self.app.accounts) > 1)
            remove.connect("clicked", lambda _b, a=account: self._confirm_remove(a))
            row.add_suffix(remove)
            self.account_group.add(row)
            self.account_rows.append(row)

    # ---- Handlers --------------------------------------------------------------------------

    def _set_clock(self, row: Adw.EntryRow, key: str) -> None:
        parsed = parse_clock(row.get_text())
        if parsed is None:
            row.add_css_class("error")
            return
        text = parsed.strftime("%H:%M")
        row.set_text(text)
        self.settings[key] = text

    def _choose_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Save Files To", initial_folder=Gio.File.new_for_path(str(self.settings.download_dir)))

        def chosen(dlg, result) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.settings["download_dir"] = folder.get_path()
                self.folder.set_subtitle(folder.get_path())

        dialog.select_folder(self.get_root(), None, chosen)

    def _confirm_remove(self, account) -> None:
        alert = Adw.AlertDialog(
            heading=f"Remove “{account.name}”?",
            body="Natter forgets this account on this computer. "
                 "To unlink it fully, also remove it under Linked devices on your phone.")
        alert.add_response("cancel", "Cancel")
        alert.add_response("remove", "Remove")
        alert.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")

        def answered(dlg, result) -> None:
            if dlg.choose_finish(result) == "remove":
                self.app.remove_account(account)
                self._fill_accounts()

        alert.choose(self, None, answered)

