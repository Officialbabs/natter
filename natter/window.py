"""Natter's window: a header bar over one WhatsApp page per account."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango

from . import DEFAULT_APP_ID, NAME
from . import clipboard, themes
from .account import Account
from .prefs import shown_folder

# The header bar is GNOME's own, unstyled: its buttons and window controls are the size and
# spacing they are in other apps, and they don't move when the lock page's plain header shows.
CSS = """
/* Over the page, so WhatsApp's pane divider doesn't run through the words. The scrim covers it
   all, edge to edge, and the dashed outline sits inside it: an outline with a margin of its own
   would leave a strip of the page showing around it at full strength. */
.natter-drop { background: alpha(@window_bg_color, 0.92); }
.natter-drop-edge { border: 2px dashed @accent_color; border-radius: 12px; margin: 10px; }
/* The Reload button's arrow turns while WhatsApp loads again (GTK skips it with animations off). */
@keyframes natter-turn { from { -gtk-icon-transform: rotate(0turn); } to { -gtk-icon-transform: rotate(1turn); } }
button.natter-reloading image { animation: natter-turn 1s linear infinite; }
"""

ACCOUNT_ICON = "avatar-default-symbolic"
# Narrower than this, two accounts' tabs don't fit in the header beside its buttons: they go to
# the bottom of the window, and the header shows the title again.
NARROW = "max-width: 550sp"
# At most one toast about WhatsApp being refused the microphone, or the camera, this many seconds
# apart: it may ask several times in a row (each device it tries, each press of its button).
REFUSED_GAP = 10.0


def rgba(value: str) -> Gdk.RGBA:
    """One of themes.py's colours, as GTK takes it."""
    color = Gdk.RGBA()
    color.parse(value)
    return color


def window_keys() -> list[tuple[str, str, Optional[GLib.Variant]]]:
    """The window's own keys, as (accelerator, action, target)."""
    return [
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
    ]


def shortcut_sections(lock: bool = True) -> list[tuple[str, list[tuple[str, str]]]]:
    """The keyboard shortcuts as (section, [(what, keys)]). The keys are written the way
    Gtk.ShortcutLabel reads them: alternatives separated by spaces, a range as first...last.
    Lock is there only with a passcode set, as in Go to Chat's commands."""
    return [
        (NAME, [("Go to chat, or message a number", "<Control>k"),
                ("Privacy blur", "<Control><Shift>b"),
                *([("Lock", "<Control>l")] if lock else []),
                ("Switch account", "<Control>1...<Control>9"),
                ("Preferences", "<Control>comma"),
                ("Keyboard shortcuts", "<Control>question"),
                ("Hide window", "<Control>w"),
                ("Quit", "<Control>q")]),
        ("View", [("Zoom in", "<Control>plus <Control>equal"), ("Zoom out", "<Control>minus"),
                  ("Reset zoom", "<Control>0"), ("Reload WhatsApp", "<Control>r F5")]),
        ("WhatsApp", [("New chat", "<Control><Alt>n"), ("Search chats", "<Control><Alt>slash"),
                      ("Next chat", "<Control><Alt><Shift>bracketright"),
                      ("Previous chat", "<Control><Alt><Shift>bracketleft"),
                      ("Archive chat", "<Control><Alt>e"), ("Mute chat", "<Control><Alt><Shift>m")]),
    ]


def shortcuts_dialog(app=None) -> Adw.Dialog:
    """The keyboard shortcuts. Adw.ShortcutsDialog is new in libadwaita 1.8; older ones get a list."""
    sections = shortcut_sections(lock=app is None or app.lock_enabled)
    if hasattr(Adw, "ShortcutsDialog"):
        dialog = Adw.ShortcutsDialog()
        for title, rows in sections:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in rows:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        return dialog
    page = Adw.PreferencesPage()
    for title, rows in sections:
        group = Adw.PreferencesGroup(title=title)
        for label, accel in rows:
            row = Adw.ActionRow(title=label)
            row.add_suffix(Gtk.ShortcutLabel(accelerator=accel, valign=Gtk.Align.CENTER))
            group.add(row)
        page.add(group)
    view = Adw.ToolbarView(content=page)
    view.add_top_bar(Adw.HeaderBar())
    return Adw.Dialog(title="Keyboard Shortcuts", content_width=460, content_height=620, child=view)


def main_menu(app) -> Gio.Menu:
    """The header's main menu. Each item's underlined letter is its own (Alt+letter picks it)."""
    menu = Gio.Menu()
    section = Gio.Menu()
    go = Gio.MenuItem.new("_Go to Chat…", "win.go-to-chat")  # its header button made way for Reload
    go.set_attribute_value("accel", GLib.Variant("s", "<Control>k"))  # the shortcut is the window's own
    section.append_item(go)
    section.append("_Do Not Disturb…", "app.do-not-disturb")
    section.append("Add A_ccount", "app.add-account")  # A is About's, D Do Not Disturb's
    if app.calls is not None:
        section.append("_Set Up Calls…", "app.call-window")
    menu.append_section(None, section)
    # Check items: their actions hold true or false (the settings), which GTK shows as a tick.
    section = Gio.Menu()
    section.append("_Microphone", "app.microphone")
    section.append("Cam_era", "app.camera")  # C is Add Account's
    menu.append_section(None, section)
    section = Gio.Menu()
    section.append("_Preferences", "app.preferences")
    section.append("_Keyboard Shortcuts", "app.shortcuts")
    section.append(f"_About {NAME}", "app.about")
    menu.append_section(None, section)
    section = Gio.Menu()
    section.append("_Quit", "app.quit")
    menu.append_section(None, section)
    return menu


def drop_description(chat: str, blurred: bool) -> str:
    """Under "Drop to Send": where the files will go. With privacy blur on, the chat's name
    isn't written out in large letters, as an incoming call's caller isn't."""
    if not chat:
        return "You'll choose the chat next"
    return "" if blurred else f"To {chat}"


def tab_look(needs_link: bool, reloading: bool, unread: int) -> tuple[str, int]:
    """An account's tab: its icon and badge. With several accounts the tabs replace the header's
    subtitle, so a tab says itself that its account is reloading or needs linking (and one that
    needs linking has no chats to count)."""
    if reloading:
        return "view-refresh-symbolic", unread
    if needs_link:
        return "dialog-warning-symbolic", 0
    return ACCOUNT_ICON, unread


def refused_toast(kind: str, reason: str, account: str = "") -> tuple[str, str, str, GLib.Variant]:
    """The toast for WhatsApp being refused the microphone or the camera (`kind`), as (title,
    button, action, target). Refused by NAME's own switch ("off"), it offers to turn it on, never
    to switch it over, so an old toast can't turn it off again. Refused because the desktop said no
    ("desktop"), it points to Privacy, which says what NAME can do about that. `account` names the
    account WhatsApp asked in, for when there are several."""
    if reason == "desktop":
        title = f"Your desktop blocks the {kind} for {NAME}"
        button, action, target = "Details", "app.preferences-page", GLib.Variant("s", "privacy")
    else:
        title = f"The {kind} is off in {NAME}"
        button, action, target = "Turn On", "app.allow-media", GLib.Variant("s", kind)
    if account:
        title = f"{account}: {title[0].lower()}{title[1:]}"
    return title, button, action, target


def refused_due(last: Optional[float], now: float, gap: float = REFUSED_GAP) -> bool:
    """Whether a toast about a kind may show at `now` (monotonic seconds), its last one having
    shown at `last` (None: not yet)."""
    return last is None or now - last >= gap


def switcher_places(accounts: int, narrow: bool) -> tuple[bool, bool]:
    """Where the accounts' tabs go: (in the header, along the bottom). One account has none."""
    several = accounts > 1
    return several and not narrow, several and narrow


class AccountPage(Gtk.Overlay):
    """An account's page, with a notice over it when WhatsApp can't be reached or keeps stopping."""

    def __init__(self, account: Account, settings) -> None:
        super().__init__(child=account.view)
        self.account = account
        self.settings = settings
        # The page takes the colour of WhatsApp's start-up screen, in the theme and style, until
        # WhatsApp paints it: WebKit's own is white, a flash in the dark style.
        self._style_changed = Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.paint())
        self.paint()
        # Where dragged files will go, while they're over the window.
        self.drop_hint = Adw.StatusPage(icon_name="document-send-symbolic", title="Drop to Send")
        edge = Adw.Bin(child=self.drop_hint, css_classes=["natter-drop-edge"])
        self.drop_zone = Adw.Bin(child=edge, css_classes=["natter-drop"], visible=False, can_target=False)
        self.add_overlay(self.drop_zone)
        # At least 200 px wide, so Try Again on its own isn't a small pill; shown together, both take
        # the wider one's width.
        retry = Gtk.Button(label="Try Again", css_classes=["pill", "suggested-action"], width_request=200)
        retry.connect("clicked", lambda *_: account.reload())
        self.software = Gtk.Button(label="Turn Off Hardware Acceleration", css_classes=["pill"], width_request=200)
        self.software.connect("clicked", lambda *_: self._turn_off_acceleration())
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(retry)
        buttons.append(self.software)
        # WebKit's own words for what went wrong, as small print: the description says it plainly.
        # It's beside the buttons' box, not in it, or a long error would stretch the buttons.
        self.detail = Gtk.Label(css_classes=["caption", "dim-label"], wrap=True, selectable=True,
                                justify=Gtk.Justification.CENTER, max_width_chars=60, visible=False)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.append(buttons)
        content.append(self.detail)
        self.notice = Adw.StatusPage(child=content, visible=False, css_classes=["background"])
        self.add_overlay(self.notice)
        account.connect("state-changed", lambda *_: self._show_state())

    def paint(self) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        self.account.view.set_background_color(rgba(themes.page_background(self.settings["theme"], dark)))

    def forget(self) -> None:
        """The account is gone: its page stops following the style."""
        Adw.StyleManager.get_default().disconnect(self._style_changed)

    def _show_state(self) -> None:
        account = self.account
        if account.crashing:
            self.notice.set_icon_name("dialog-warning-symbolic")
            self.notice.set_title("WhatsApp Keeps Stopping")
            self.notice.set_description("Its page closed three times in two minutes, so it won't reopen on its own.")
        elif account.failed:
            self.notice.set_icon_name("network-offline-symbolic")
            self.notice.set_title("Can't Reach WhatsApp")
            self.notice.set_description(GLib.markup_escape_text(
                f"Check your internet connection. {NAME} tries again as soon as you're back online.", -1))
        self.detail.set_label(account.failed or "")
        self.detail.set_visible(bool(account.failed) and not account.crashing)
        self.software.set_visible(bool(account.crashing) and self.settings["hardware_acceleration"])
        self.notice.set_visible(bool(account.crashing or account.failed))

    def _turn_off_acceleration(self) -> None:
        self.settings["hardware_acceleration"] = False  # Natter turns it off for every account
        self.account.reload()


def status_line(linking: bool, reloading: bool, unread: int) -> str:
    """The line under Natter's name in the header: what's going on, else the unread count."""
    if linking:
        return "Needs linking"
    if reloading:
        return "Reloading…"
    return f"{unread} unread" if unread else ""


class Window(Adw.ApplicationWindow):
    def __init__(self, app) -> None:
        settings = app.settings
        # With a breakpoint (below) the window has no minimum size of its own; libadwaita leaves
        # it to these. This is the smallest GNOME apps take, a phone's.
        super().__init__(application=app, title=NAME, icon_name=DEFAULT_APP_ID,
                         default_width=settings["width"], default_height=settings["height"],
                         maximized=settings["maximized"], width_request=360, height_request=294)
        self.app = app
        self.pages: dict[str, Adw.ViewStackPage] = {}
        self._refused_at: dict[str, float] = {}  # when each kind's last refused_toast showed

        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.connect("notify::visible-child",
                           lambda *_: (app.on_page_changed(), self._show_call(), self._show_subtitle(),
                                       self._show_reloading()))
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        self.title = Adw.WindowTitle(title=NAME)
        self.narrow = False
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW))
        # Signals, not setters: _show_switcher already swaps the header's title widget.
        narrow.connect("apply", lambda *_: self._set_narrow(True))
        narrow.connect("unapply", lambda *_: self._set_narrow(False))
        self.add_breakpoint(narrow)

        self.header = Adw.HeaderBar()
        # Natter's own buttons are on the left, over WhatsApp's chat list: it works like a sidebar,
        # and GNOME apps keep the main menu over theirs. The middle is left for the title or the
        # accounts' tabs, the right for the window controls and the call button, which belongs to
        # the chat open on that side.
        self.header.pack_start(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=main_menu(app),
                                              primary=True, tooltip_text="Main Menu"))
        self.reload_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload (Ctrl+R)",
                                        action_name="win.reload")
        self.header.pack_start(self.reload_button)
        self.header.pack_start(Gtk.ToggleButton(icon_name="view-conceal-symbolic", action_name="app.privacy-blur",
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

        # What lasts (notifications paused, media that can't play) shows under the header until
        # it's over; toasts are for what passes.
        self.banner = Adw.Banner(revealed=False, use_markup=False)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(self.header)
        view.add_top_bar(self.banner)
        view.add_bottom_bar(self.switcher_bar)
        # WhatsApp's page never scrolls under the bars, so they'd never get their edge from it.
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        # The app lock (lock.py): WhatsApp stays loaded behind it, out of sight and out of reach.
        self.locks = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.locks.add_named(view, "content")
        self.locks.add_named(self._lock_page(), "locked")
        self.set_content(self.locks)
        activity = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        activity.connect("key-pressed", lambda *_: (app.note_activity(), False)[1])
        self.add_controller(activity)
        moved = Gtk.EventControllerMotion(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        moved.connect("motion", lambda *_: app.note_activity())
        self.add_controller(moved)

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

    # ---- The app lock ------------------------------------------------------------------------

    def _lock_page(self) -> Gtk.Widget:
        self.passcode = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Passcode", activates_default=False)
        # A placeholder isn't a name: without one, a screen reader calls it just a password field.
        self.passcode.update_property([Gtk.AccessibleProperty.LABEL], ["Passcode"])
        self.passcode.connect("activate", lambda *_: self._unlock())
        self.passcode.connect("changed", lambda *_: self._passcode_changed())
        self._checking = False
        self.unlock_button = Gtk.Button(label="Unlock", css_classes=["pill", "suggested-action"],
                                        halign=Gtk.Align.CENTER, sensitive=False)
        self.unlock_button.connect("clicked", lambda *_: self._unlock())
        forgot = Gtk.Button(label="Forgot Passcode?", css_classes=["flat"], halign=Gtk.Align.CENTER)
        forgot.connect("clicked", lambda *_: self._forgot_passcode())
        self.lock_message = Gtk.Label(css_classes=["error", "caption"], wrap=True, visible=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.passcode)
        box.append(self.lock_message)
        box.append(self.unlock_button)
        box.append(forgot)
        status = Adw.StatusPage(icon_name="system-lock-screen-symbolic", title=f"{NAME} Is Locked",
                                description="Enter your passcode to unlock",
                                child=Adw.Clamp(child=box, maximum_size=320))
        page = Adw.ToolbarView(content=status)
        page.add_top_bar(Adw.HeaderBar(show_title=False))
        return page

    def show_lock(self, locked: bool) -> None:
        if locked:
            # Dialogs sit above the content: Go to Chat lists chats, Preferences could undo the lock.
            for _ in range(8):
                dialog = self.get_visible_dialog()
                if dialog is None:
                    break
                dialog.force_close()
        # Insensitive, the page behind the lock takes no focus, keys or paste.
        self.locks.get_child_by_name("content").set_sensitive(not locked)
        self.locks.set_visible_child_name("locked" if locked else "content")
        self.passcode.set_text("")
        # Emptying a field that's empty already says nothing ("changed" doesn't come), so a wrong
        # try's red outline would still be there the next time the window locks.
        self.passcode.remove_css_class("error")
        self.lock_message.set_visible(False)
        self._checking = False
        self._can_unlock()
        if locked:
            self.passcode.grab_focus()
        elif self.account:
            self.account.view.grab_focus()

    def _passcode_changed(self) -> None:
        self.passcode.remove_css_class("error")
        self._can_unlock()

    def _can_unlock(self) -> None:
        # An empty try would count towards the waits after too many wrong ones (lock.Attempts).
        self.unlock_button.set_sensitive(bool(self.passcode.get_text()) and not self._checking)

    def _unlock(self) -> None:
        if self._checking or not self.passcode.get_text():
            return  # still checking, or nothing to check
        self._checking = True
        self._can_unlock()

        def done(ok: bool, message: str) -> None:
            self._checking = False
            self._can_unlock()
            if ok:
                return
            self.passcode.add_css_class("error")
            self.passcode.select_region(0, -1)
            self.passcode.grab_focus()
            self.lock_message.set_label(message)
            self.lock_message.set_visible(True)
            # The red outline and the message are only seen: a screen reader hears it too.
            self.passcode.announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)

        self.app.try_unlock(self.passcode.get_text(), done)

    def _forgot_passcode(self) -> None:
        """How to take a forgotten lock off. The passcode is kept only as a hash (lock.py), so it
        can't be told; the way out is the settings file, which the lock never claimed to guard."""
        alert = Adw.AlertDialog(
            heading="Forgot Passcode?",
            body=f"It can't be recovered, but the lock can be removed. Quit {NAME}, open the file below in a "
                 f"text editor, empty the quotes after “lock_hash” so they read \"\", save it and start {NAME} "
                 f"again. WhatsApp stays linked.")
        # The path has a Copy button rather than being selectable: a selectable label would take the
        # dialog's first focus, and that selects all of it.
        path = shown_folder(self.app.settings.path)  # with the home folder as ~
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy Path", valign=Gtk.Align.CENTER,
                          css_classes=["flat"])
        copy.update_property([Gtk.AccessibleProperty.LABEL], ["Copy Path"])  # an icon has no words to read out
        copy.connect("clicked", lambda button: button.get_clipboard().set(path))
        place = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        # A long path (a Flatpak's is) wraps anywhere, but without the hyphens Pango would add: they
        # aren't in the file's name.
        hyphens = Pango.AttrList()
        hyphens.insert(Pango.attr_insert_hyphens_new(False))
        place.append(Gtk.Label(label=path, wrap=True, wrap_mode=Pango.WrapMode.CHAR, attributes=hyphens,
                               css_classes=["monospace"]))
        place.append(copy)
        alert.set_extra_child(place)
        alert.add_response("close", "_Close")
        alert.add_response("quit", "_Quit")
        alert.set_close_response("close")
        alert.set_default_response("close")
        alert.connect("response",
                      lambda _alert, response: response == "quit" and self.app.activate_action("quit", None))
        alert.present(self)

    # ---- Accounts --------------------------------------------------------------------------

    def add_account(self, account: Account) -> None:
        page = self.stack.add_titled_with_icon(AccountPage(account, self.app.settings), account.id, account.name,
                                               ACCOUNT_ICON)
        self.pages[account.id] = page
        for signal in ("state-changed", "chats-changed"):
            account.connect(signal, lambda a: a is self.account and self._show_call())
        account.connect("media-refused", self._media_refused)
        self._show_switcher()

    def remove_account(self, account: Account) -> None:
        page = self.pages.pop(account.id, None)
        if page is not None:
            page.get_child().forget()
            self.stack.remove(page.get_child())
        self._show_switcher()

    def repaint_pages(self) -> None:
        """Another theme: each page's colour before WhatsApp paints follows it."""
        for page in self.pages.values():
            page.get_child().paint()

    def rename_account(self, account: Account) -> None:
        if account.id in self.pages:
            self.pages[account.id].set_title(account.name)

    def _set_narrow(self, narrow: bool) -> None:
        self.narrow = narrow
        self._show_switcher()

    def _show_switcher(self) -> None:
        in_header, at_bottom = switcher_places(len(self.pages), self.narrow)
        self.header.set_title_widget(self.switcher if in_header else self.title)
        self.switcher_bar.set_reveal(at_bottom)

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
        """The unread count, whether an account needs linking or is reloading, in the title and on each tab."""
        for account in self.app.accounts:
            page = self.pages.get(account.id)
            if page is not None:
                icon, badge = tab_look(account.needs_link, account.reloading, account.unread)
                page.set_icon_name(icon)
                page.set_badge_number(badge)
                page.set_needs_attention(account.unread > 0 or account.needs_link)
        self._show_subtitle()
        self._show_reloading()
        self.set_title(f"({total}) {NAME}" if total else NAME)

    def _show_subtitle(self) -> None:
        """The line under the name. With one account, about it. With several, the title shows
        only in a narrow window, over the account on screen (the tabs along the bottom show the
        others), so it's about that one: not "Needs linking" over an account that's linked."""
        accounts = self.app.accounts
        shown = accounts if len(accounts) <= 1 else [a for a in (self.account,) if a is not None]
        self.title.set_subtitle(status_line(any(a.needs_link for a in shown), any(a.reloading for a in shown),
                                            sum(a.unread for a in shown)))

    def _show_reloading(self) -> None:
        """The Reload arrow turns while the account it reloads (the one shown) loads again."""
        account = self.account
        if account and account.reloading:
            self.reload_button.add_css_class("natter-reloading")
        else:
            self.reload_button.remove_css_class("natter-reloading")

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
        for accel, action, target in window_keys():
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
        description = drop_description(page.account.active_chat, self.app.settings["privacy_blur"])
        page.drop_hint.set_description(GLib.markup_escape_text(description, -1))  # a chat's name isn't markup
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

    def _media_refused(self, account: Account, kind: str, reason: str) -> None:
        """WhatsApp asked for the microphone or the camera, and NAME said no (Account's
        media-refused): say why, with what to do about it."""
        if kind not in ("microphone", "camera") or reason not in ("off", "desktop") or self.app.locked:
            return  # behind the lock it would wait unseen, and be out of date by the time it showed
        now = time.monotonic()
        if not refused_due(self._refused_at.get(kind), now):
            return
        self._refused_at[kind] = now
        several = len(self.app.accounts) > 1
        self.toast(*refused_toast(kind, reason, account.name if several else ""), timeout=8)

    def _switch_account(self, _action, param: GLib.Variant) -> None:
        if self.app.locked:
            return
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

    def set_banner(self, title: str, button: str = "", action: str = "",
                   target: Optional[GLib.Variant] = None) -> None:
        """Show (or change) the banner under the header until hide_banner, with a button that
        runs `action` when both are given."""
        self.banner.set_title(title)
        self.banner.set_button_label(button if button and action else "")
        # The action goes last: set before its target, GTK checks the new action against the old
        # target (or none) and warns that their types don't match.
        self.banner.set_action_name(None)
        self.banner.set_action_target_value(target)
        self.banner.set_action_name(action or None)
        self.banner.set_revealed(True)

    def hide_banner(self) -> None:
        self.banner.set_revealed(False)

    # ---- Closing ---------------------------------------------------------------------------

    def remember_size(self) -> None:
        settings = self.app.settings
        settings["maximized"] = self.is_maximized()
        if not self.is_maximized():
            settings["width"], settings["height"] = self.get_width(), self.get_height()

    def _on_close(self, _window) -> bool:
        if not self.get_visible():
            # Nobody clicks the close button of a hidden window: it's the desktop closing apps
            # (Ada, to make room when memory is full), which opens them again from the dock.
            self.app.quit_natter()
            return True
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
            settings = self.app.settings
            snapshot.append_color(rgba(themes.window_background(settings["theme"], dark, settings["theme_custom"])),
                                  Graphene.Rect().init(0, 0, width, height))
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
