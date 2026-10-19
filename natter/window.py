"""Natter's window: a slim header bar over one WhatsApp page per account."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk

from . import APP_ID, NAME
from . import clipboard
from .account import Account

CSS = """
headerbar.natter-header { min-height: 38px; padding-top: 0; padding-bottom: 0; }
headerbar.natter-header button { min-height: 28px; min-width: 28px; padding: 2px 6px; }
.natter-drop { background: alpha(@accent_bg_color, 0.18); border: 3px dashed @accent_color; border-radius: 12px;
               margin: 10px; color: @accent_color; }
"""


SHORTCUTS = [
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
]


def shortcuts_dialog() -> Adw.Dialog:
    """The keyboard shortcuts. Adw.ShortcutsDialog is new in libadwaita 1.8; older ones get a list."""
    if hasattr(Adw, "ShortcutsDialog"):
        dialog = Adw.ShortcutsDialog()
        for title, rows in SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in rows:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        return dialog
    page = Adw.PreferencesPage()
    for title, rows in SHORTCUTS:
        group = Adw.PreferencesGroup(title=title)
        for label, accel in rows:
            row = Adw.ActionRow(title=label)
            row.add_suffix(Gtk.ShortcutLabel(accelerator=accel, valign=Gtk.Align.CENTER))
            group.add(row)
        page.add(group)
    view = Adw.ToolbarView(content=page)
    view.add_top_bar(Adw.HeaderBar())
    return Adw.Dialog(title="Keyboard Shortcuts", content_width=460, content_height=620, child=view)


class AccountPage(Gtk.Overlay):
    """An account's page, with a notice over it when WhatsApp can't be reached or keeps stopping."""

    def __init__(self, account: Account, settings) -> None:
        super().__init__(child=account.view)
        self.account = account
        self.settings = settings
        # Where dragged files will go, while they're over the window.
        self.drop_hint = Gtk.Label(css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER)
        hint = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                       valign=Gtk.Align.CENTER)
        hint.append(Gtk.Image(icon_name="document-send-symbolic", pixel_size=64))
        hint.append(self.drop_hint)
        self.drop_zone = Adw.Bin(child=hint, css_classes=["natter-drop"], visible=False, can_target=False)
        self.add_overlay(self.drop_zone)
        retry = Gtk.Button(label="Try Again", css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda *_: account.reload())
        self.software = Gtk.Button(label="Turn Off Hardware Acceleration", css_classes=["pill"])
        self.software.connect("clicked", lambda *_: self._turn_off_acceleration())
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(retry)
        buttons.append(self.software)
        self.notice = Adw.StatusPage(child=buttons, visible=False, css_classes=["background"])
        self.add_overlay(self.notice)
        account.connect("state-changed", lambda *_: self._show_state())

    def _show_state(self) -> None:
        account = self.account
        if account.crashing:
            self.notice.set_icon_name("dialog-warning-symbolic")
            self.notice.set_title("WhatsApp Keeps Stopping")
            self.notice.set_description("Its page closed three times in two minutes, so Natter stopped reopening it.")
        elif account.failed:
            self.notice.set_icon_name("network-offline-symbolic")
            self.notice.set_title("Can't Reach WhatsApp")
            self.notice.set_description(f"{account.failed}\nNatter tries again as soon as you're back online.")
        self.software.set_visible(bool(account.crashing) and self.settings["hardware_acceleration"])
        self.notice.set_visible(bool(account.crashing or account.failed))

    def _turn_off_acceleration(self) -> None:
        self.settings["hardware_acceleration"] = False  # Natter turns it off for every account
        self.account.reload()


class Window(Adw.ApplicationWindow):
    def __init__(self, app) -> None:
        settings = app.settings
        super().__init__(application=app, title=NAME, icon_name=APP_ID,
                         default_width=settings["width"], default_height=settings["height"],
                         maximized=settings["maximized"])
        self.app = app
        self.pages: dict[str, Adw.ViewStackPage] = {}

        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.connect("notify::visible-child", lambda *_: (app.on_page_changed(), self._show_call()))
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.title = Adw.WindowTitle(title=NAME)

        self.header = Adw.HeaderBar(css_classes=["natter-header"])
        go = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Go to Chat (Ctrl+K)",
                        action_name="win.go-to-chat")
        self.header.pack_start(go)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("_Add Account", "app.add-account")
        if app.calls is not None:
            section.append("_Set Up Calls…", "app.call-window")
        section.append("_Reload", "win.reload")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("_Preferences", "app.preferences")
        section.append("_Keyboard Shortcuts", "app.shortcuts")
        section.append("_About Natter", "app.about")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("_Quit", "app.quit")
        menu.append_section(None, section)
        self.header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                            tooltip_text="Main Menu"))
        self.header.pack_end(Gtk.ToggleButton(icon_name="view-conceal-symbolic", action_name="app.privacy-blur",
                                              tooltip_text="Privacy Blur (Ctrl+Shift+B)"))
        # Calls: with WhatsApp calling from its own page (rtc.py), its call buttons in the chat are
        # the ones to use, so the header has none. Otherwise Natter's call window, else the browser.
        self.call: Optional[Gtk.Button] = None
        if app.calls is not None:
            self.call = Gtk.Button(icon_name="call-start-symbolic", tooltip_text="Voice Call",
                                   action_name="app.call", visible=False)
        elif not app.calls_in_page:
            self.call = Gtk.Button(icon_name="call-start-symbolic", tooltip_text="Call in Browser",
                                   action_name="app.call-in-browser", visible=False)
        if self.call is not None:
            self.header.pack_end(self.call)

        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(self.header)
        self.set_content(view)

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._actions()
        self._drops()
        self.connect("close-request", self._on_close)
        self.connect("notify::is-active", lambda *_: app.on_focus_changed(self.is_active()))

        snapshot = os.environ.get("NATTER_SNAPSHOT")
        if snapshot:
            path, _, delay = snapshot.partition(":")
            GLib.timeout_add_seconds(int(delay or 8), lambda: self.save_snapshot(path) and False)

    # ---- Accounts --------------------------------------------------------------------------

    def add_account(self, account: Account) -> None:
        page = self.stack.add_titled_with_icon(AccountPage(account, self.app.settings), account.id, account.name,
                                               "user-available-symbolic")
        self.pages[account.id] = page
        for signal in ("state-changed", "chats-changed"):
            account.connect(signal, lambda a: a is self.account and self._show_call())
        self._show_switcher()

    def remove_account(self, account: Account) -> None:
        page = self.pages.pop(account.id, None)
        if page is not None:
            self.stack.remove(page.get_child())
        self._show_switcher()

    def rename_account(self, account: Account) -> None:
        if account.id in self.pages:
            self.pages[account.id].set_title(account.name)

    def _show_switcher(self) -> None:
        self.header.set_title_widget(self.switcher if len(self.pages) > 1 else self.title)

    def focus_in_page(self) -> bool:
        """Whether typing goes to the WhatsApp page (and not, say, an open dialog)."""
        account, focus = self.account, self.get_focus()
        return bool(account and focus and (focus is account.view or focus.is_ancestor(account.view)))

    @property
    def account(self) -> Optional[Account]:
        child = self.stack.get_visible_child()
        return child.account if isinstance(child, AccountPage) else None

    def show_account(self, account: Account) -> None:
        if account.id in self.pages:
            self.stack.set_visible_child_name(account.id)
        account.view.grab_focus()

    def show_status(self, total: int) -> None:
        """The unread count, and whether an account needs linking, in the title and on each tab."""
        for account in self.app.accounts:
            page = self.pages.get(account.id)
            if page is not None:
                page.set_badge_number(account.unread)
                page.set_needs_attention(account.unread > 0 or account.needs_link)
        linking = any(a.needs_link for a in self.app.accounts)
        self.title.set_subtitle("Needs linking" if linking else f"{total} unread" if total else "")
        self.set_title(f"({total}) {NAME}" if total else NAME)

    def _show_call(self) -> None:
        if self.call is None:
            return
        account = self.account
        self.call.set_visible(bool(account and account.active_chat and not account.needs_link))

    # ---- Actions ---------------------------------------------------------------------------

    def _actions(self) -> None:
        def add(name: str, callback, param: Optional[str] = None) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType(param) if param else None)
            action.connect("activate", callback)
            self.add_action(action)

        add("go-to-chat", lambda *_: self.app.go_to_chat())
        add("reload", lambda *_: self.account and self.account.reload())
        add("hide", lambda *_: self.close())
        add("zoom-in", lambda *_: self._zoom(+0.1))
        add("zoom-out", lambda *_: self._zoom(-0.1))
        add("zoom-reset", lambda *_: self._zoom(0))
        add("account", self._switch_account, "i")

        # Natter's own keys are caught before the page sees them, so WhatsApp can't swallow them.
        keys = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        for accel, action, target in [
            ("<Control>k", "win.go-to-chat", None),
            ("<Control><Shift>b", "app.privacy-blur", None),
            ("<Control>comma", "app.preferences", None),
            ("<Control>question", "app.shortcuts", None),
            ("<Control>q", "app.quit", None),
            ("<Control>w", "win.hide", None),
            ("<Control>r", "win.reload", None),
            ("F5", "win.reload", None),
            ("<Control>plus", "win.zoom-in", None),
            ("<Control>equal", "win.zoom-in", None),
            ("<Control>minus", "win.zoom-out", None),
            ("<Control>0", "win.zoom-reset", None),
            *[(f"<Control>{i}", "win.account", GLib.Variant("i", i - 1)) for i in range(1, 10)],
        ]:
            shortcut = Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(accel),
                                    action=Gtk.NamedAction.new(action))
            if target is not None:
                shortcut.set_arguments(target)
            keys.add_shortcut(shortcut)
        # Pictures and files on the clipboard; text falls through to WebKit's own paste.
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>v|<Shift>Insert"),
                                       action=Gtk.CallbackAction.new(lambda *_: self.app.paste())))
        self.add_controller(keys)

    def _drops(self) -> None:
        """Files and pictures dropped on the chat go through Natter, not WebKit: WhatsApp's page
        runs sandboxed and can't read files from your folders, but Natter can hand them over."""
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, Gdk.Texture])
        drop.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drop.connect("enter", self._drag_enter)
        drop.connect("leave", lambda *_: self._drag_hint(False))
        drop.connect("drop", self._on_drop)
        self.stack.add_controller(drop)

    def _drag_enter(self, _target, _x, _y) -> Gdk.DragAction:
        self._drag_hint(True)
        return Gdk.DragAction.COPY

    def _drag_hint(self, on: bool) -> None:
        page = self.stack.get_visible_child()
        if not isinstance(page, AccountPage):
            return
        chat = page.account.active_chat
        page.drop_hint.set_label(f"Drop to send to {chat}" if chat else "Drop, then choose a chat")
        page.drop_zone.set_visible(on)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        self._drag_hint(False)
        if isinstance(value, Gdk.FileList):
            paths = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
            paths = [p for p in paths if p.is_file()]
        elif isinstance(value, Gdk.Texture):
            paths = [clipboard.save_picture(value, "Dropped image")]
        else:
            paths = []
        if not paths:
            self.toast("Only files can be dropped here (not folders)")
            return False
        self.app.send_files(paths)
        return True

    def _switch_account(self, _action, param: GLib.Variant) -> None:
        index = param.get_int32()
        if 0 <= index < len(self.app.accounts):
            self.show_account(self.app.accounts[index])

    def _zoom(self, step: float) -> None:
        zoom = 1.0 if step == 0 else round(min(2.0, max(0.5, self.app.settings["zoom"] + step)), 2)
        self.app.settings["zoom"] = zoom
        self.toast(f"Zoom {round(zoom * 100)}%", timeout=1)

    def toast(self, title: str, button: Optional[str] = None, action: Optional[str] = None,
              target: Optional[GLib.Variant] = None, timeout: int = 4) -> None:
        toast = Adw.Toast(title=title, timeout=timeout, use_markup=False)
        if button and action:
            toast.set_button_label(button)
            toast.set_action_name(action)
            if target is not None:
                toast.set_action_target_value(target)
        self.toasts.add_toast(toast)

    # ---- Closing ---------------------------------------------------------------------------

    def remember_size(self) -> None:
        settings = self.app.settings
        settings["maximized"] = self.is_maximized()
        if not self.is_maximized():
            settings["width"], settings["height"] = self.get_width(), self.get_height()

    def _on_close(self, _window) -> bool:
        self.remember_size()
        self.app.settle_first_run()  # a new Natter started at login decides now whether closing quits
        if self.app.settings["run_in_background"]:
            self.set_visible(False)
            self.app.window_closed()
        else:
            self.app.quit_natter()
        return True

    # ---- For checking how it looks ---------------------------------------------------------

    def save_snapshot(self, path: str) -> None:
        """Save the window's contents as a PNG. A widget paintable fills in on the next frame."""
        # A toplevel has no render node of its own. Its child holds the content and any open dialog.
        content = Gtk.Window.get_child(self)
        paintable = Gtk.WidgetPaintable.new(content)
        content.queue_draw()

        def save() -> bool:
            width, height = content.get_width(), content.get_height()
            snapshot = Gtk.Snapshot()
            # The window paints the background behind the header bar; the content doesn't.
            dark = Adw.StyleManager.get_default().get_dark()
            background = Gdk.RGBA()
            background.parse("#222226" if dark else "#fafafb")
            snapshot.append_color(background, Graphene.Rect().init(0, 0, width, height))
            paintable.snapshot(snapshot, width, height)
            node = snapshot.to_node()
            if node is None:
                print("natter: nothing to snapshot yet")
                return GLib.SOURCE_REMOVE
            texture = self.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, width, height))
            texture.save_to_png(path)
            print(f"natter: saved {path}")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(250, save)
