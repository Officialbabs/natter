"""Ctrl+K: jump to any chat, message a number without saving it, or run a command.

With files (dropped on the dock icon or passed on the command line) it becomes "Send To…".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from .launcher import INVISIBLE
from .settings import Chat, phone_number, rank, score

LIMIT = 40
# The microphone and camera switches' commands: the setting, then (name, icon) to turn it off,
# and to turn it on. Each says what it will do, and its icon shows the device as it will be.
DEVICE_COMMANDS = {
    "microphone": (("Turn Off Microphone", "microphone-disabled-symbolic"),
                   ("Turn On Microphone", "audio-input-microphone-symbolic")),
    "camera": (("Turn Off Camera", "camera-disabled-symbolic"), ("Turn On Camera", "camera-video-symbolic")),
}


def device_command(kind: str, on: bool) -> tuple[str, str]:
    """The command for the microphone's or camera's switch (`kind`), `on` or not, as (name, icon)."""
    turn_off, turn_on = DEVICE_COMMANDS[kind]
    return turn_off if on else turn_on


def initials(title: str) -> bool:
    """Whether a chat's avatar shows its initials: not for a chat named by its phone number, whose
    "initials" would be "+4" or "55", so its avatar shows the plain person icon. WhatsApp wraps
    such numbers in direction marks, which don't count."""
    first = INVISIBLE.sub("", title).lstrip()[:1]
    return not (first == "+" or first.isdigit())


@dataclass
class Entry:
    title: str
    subtitle: str
    run: Callable[[], None]
    avatar: bool = True
    icon: str = ""


class Palette(Adw.Dialog):
    def __init__(self, app, files: Optional[list[Path]] = None, query: str = "", how: str = "") -> None:
        self.app = app
        self.files = files or []
        self.how = how  # how the files came (dropped, pasted, shared): for the log (Account.attach)
        super().__init__(title="Send To" if self.files else "Go to Chat", content_width=460, content_height=540)

        self.entry = Gtk.SearchEntry(hexpand=True,
                                     placeholder_text="Search chats" if self.files else "Search chats, or type a phone number")
        # The placeholder goes once something's typed; screen readers keep the name.
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], ["Search chats"])
        if self.files:
            # What's being sent stays in sight, in the title: the search box goes in a bar of its own.
            names = self.files[0].name if len(self.files) == 1 else f"{len(self.files)} files"
            header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Send To", subtitle=names))
            search = Gtk.Box(margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
            search.append(self.entry)
        else:
            header = Adw.HeaderBar(title_widget=self.entry, show_start_title_buttons=False)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, css_classes=["navigation-sidebar"])
        self.list.connect("row-activated", lambda _l, row: self._run(row))
        self.scroller = scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.empty = Adw.StatusPage(icon_name="system-search-symbolic", title="No Chats Yet",
                                    description="Chats show up here once WhatsApp's chat list has loaded.",
                                    css_classes=["compact"])
        self.content = Gtk.Stack()
        self.content.add_named(scroller, "list")
        self.content.add_named(self.empty, "empty")
        body = Adw.ToolbarView(content=self.content)
        body.add_top_bar(header)
        if self.files:
            body.add_top_bar(search)
            note = Gtk.Label(label="WhatsApp shows a preview with a caption box before anything is sent.",
                             wrap=True, css_classes=["caption", "dim-label"], margin_top=6, margin_bottom=8,
                             margin_start=12, margin_end=12)
            body.add_bottom_bar(note)
        self.set_child(body)

        self.entry.connect("search-changed", lambda *_: self.refresh())
        self.entry.connect("activate", lambda *_: self._run(self.list.get_selected_row()))
        self.entry.connect("stop-search", lambda *_: self.close())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(keys)
        self.set_focus(self.entry)
        if query:
            self.entry.set_text(query)
            self.entry.set_position(-1)
        self.refresh()

    # ---- What's listed ---------------------------------------------------------------------

    def entries(self, query: str) -> list[Entry]:
        app = self.app
        several = len(app.accounts) > 1
        found: list[Entry] = []

        digits = phone_number(query)
        if digits and app.window.account:
            account = app.window.account
            found.append(Entry(f"Message +{digits}", "Opens a chat without saving the number",
                               lambda: app.message_number(account, digits, self.files, self.how), avatar=False,
                               icon="chat-message-new-symbolic"))

        chats: list[tuple[Chat, object]] = []
        for account in app.accounts:
            chats += [(chat, account) for chat in account.chats.recent()]
        by_chat = {id(chat): account for chat, account in chats}
        for chat in rank(query, [c for c, _ in chats])[:LIMIT]:
            account = by_chat[id(chat)]
            found.append(Entry(chat.name, account.name if several else "",
                               lambda a=account, n=chat.name: app.go_to(a, n, self.files, self.how)))

        if not self.files and query.strip():
            commands = [
                ("Privacy Blur", "view-conceal-symbolic", lambda: app.activate_action("privacy-blur", None)),
                ("Preferences", "preferences-system-symbolic", lambda: app.activate_action("preferences", None)),
                ("Keyboard Shortcuts", "preferences-desktop-keyboard-shortcuts-symbolic",
                 lambda: app.activate_action("shortcuts", None)),
                ("Reload WhatsApp", "view-refresh-symbolic", lambda: app.window.account and app.window.account.reload()),
                *([("Voice Call", "call-start-symbolic", lambda: app.activate_action("call", None)),
                   ("Video Call", "camera-video-symbolic", lambda: app.activate_action("video-call", None))]
                  if app.can_call else
                  [("Call in Browser", "call-start-symbolic", lambda: app.activate_action("call-in-browser", None))]),
                ("Add Account", "list-add-symbolic", lambda: app.activate_action("add-account", None)),
                *[(*device_command(kind, bool(app.settings[kind])),
                   lambda kind=kind, on=not app.settings[kind]: self._turn_device(kind, on))
                  for kind in DEVICE_COMMANDS],
                *([("Lock", "system-lock-screen-symbolic", lambda: app.activate_action("lock", None))]
                  if app.lock_enabled else []),
                *([("Resume Notifications", "preferences-system-notifications-symbolic",
                    lambda: app.activate_action("resume-notifications", None))]
                  if app.settings.do_not_disturb() else
                  [("Do Not Disturb…", "notifications-disabled-symbolic",
                    lambda: app.activate_action("do-not-disturb", None)),
                   ("Pause Notifications for 1 Hour", "notifications-disabled-symbolic",
                    lambda: app.activate_action("pause-notifications-hour", None)),
                   ("Pause Notifications for 2 Hours", "notifications-disabled-symbolic",
                    lambda: app.activate_action("pause-notifications-two-hours", None)),
                   ("Pause Notifications Until Turned Back On", "notifications-disabled-symbolic",
                    lambda: app.activate_action("pause-notifications", None))]),
            ]
            for account in app.accounts if several else []:
                commands.append((f"Switch to {account.name}", "system-users-symbolic",
                                 lambda a=account: app.window.show_account(a)))
            scored = sorted(((score(query, name), name, icon, run) for name, icon, run in commands),
                            key=lambda s: -s[0])
            found += [Entry(name, "", run, avatar=False, icon=icon) for s, name, icon, run in scored if s >= 2]
        return found

    def _turn_device(self, kind: str, on: bool) -> None:
        """Turn the microphone or camera on or off, as its command said, whatever it has become
        since: a command that switched it over could undo what its name promised."""
        if on:
            self.app.activate_action("allow-media", GLib.Variant("s", kind))
        else:
            self.app.change_action_state(kind, GLib.Variant("b", False))

    def refresh(self) -> None:
        self.list.remove_all()
        query = self.entry.get_text()
        entries = self.entries(query)
        for entry in entries:
            self.list.append(self._row(entry))
        if entries:
            self.content.set_visible_child_name("list")
            self.list.select_row(self.list.get_row_at_index(0))
        else:
            self.empty.set_title("No Matching Chats" if query else "No Chats Yet")
            self.empty.set_description(
                "Only chats seen in WhatsApp's chat list show up here. Scroll through it once to find older ones."
                if query else "Chats show up here once WhatsApp's chat list has loaded.")
            self.content.set_visible_child_name("empty")

    def _row(self, entry: Entry) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
        if entry.avatar:
            box.append(Adw.Avatar(size=32, text=entry.title, show_initials=initials(entry.title)))
        else:
            # As tall as an avatar, so command rows and chat rows are the same height.
            icon = Gtk.Image(icon_name=entry.icon, pixel_size=16, width_request=32, height_request=32)
            box.append(icon)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        labels.append(Gtk.Label(label=entry.title, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        if entry.subtitle:
            labels.append(Gtk.Label(label=entry.subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                    css_classes=["caption", "dim-label"]))
        box.append(labels)
        row = Gtk.ListBoxRow(child=box)
        row.entry = entry
        return row

    # ---- Keys and choosing -----------------------------------------------------------------

    def _on_key(self, _controller, keyval: int, _code: int, _state) -> bool:
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            row = self.list.get_selected_row()
            index = (row.get_index() if row else -1) + (1 if keyval == Gdk.KEY_Down else -1)
            target = self.list.get_row_at_index(max(0, index))
            if target is not None:
                self.list.select_row(target)
                self._reveal(target)
            return True
        return False

    def _reveal(self, row: Gtk.ListBoxRow) -> None:
        """Scroll so the chosen row shows; typing stays in the search box."""
        ok, bounds = row.compute_bounds(self.list)
        if not ok:
            return
        adjustment = self.scroller.get_vadjustment()
        top, bottom = bounds.get_y(), bounds.get_y() + bounds.get_height()
        if top < adjustment.get_value():
            adjustment.set_value(top)
        elif bottom > adjustment.get_value() + adjustment.get_page_size():
            adjustment.set_value(bottom - adjustment.get_page_size())

    def _run(self, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is None:
            return
        entry: Entry = row.entry
        self.close()
        entry.run()
