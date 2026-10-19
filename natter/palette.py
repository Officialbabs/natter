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
from gi.repository import Adw, Gdk, Gtk, Pango

from .settings import Chat, phone_number, rank, score

LIMIT = 40


@dataclass
class Entry:
    title: str
    subtitle: str
    run: Callable[[], None]
    avatar: bool = True
    icon: str = ""


class Palette(Adw.Dialog):
    def __init__(self, app, files: Optional[list[Path]] = None) -> None:
        self.app = app
        self.files = files or []
        if self.files:
            names = self.files[0].name if len(self.files) == 1 else f"{len(self.files)} files"
            title = f"Send {names} To…"
        else:
            title = "Go to Chat"
        super().__init__(title=title, content_width=460, content_height=540)

        self.entry = Gtk.SearchEntry(hexpand=True,
                                     placeholder_text="Search chats" if self.files else "Search chats, or type a phone number")
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
                               lambda: app.message_number(account, digits, self.files), avatar=False,
                               icon="chat-message-new-symbolic"))

        chats: list[tuple[Chat, object]] = []
        for account in app.accounts:
            chats += [(chat, account) for chat in account.chats.recent()]
        by_chat = {id(chat): account for chat, account in chats}
        for chat in rank(query, [c for c, _ in chats])[:LIMIT]:
            account = by_chat[id(chat)]
            found.append(Entry(chat.name, account.name if several else "",
                               lambda a=account, n=chat.name: app.go_to(a, n, self.files)))

        if not self.files and query.strip():
            commands = [
                ("Privacy Blur", "view-conceal-symbolic", lambda: app.activate_action("privacy-blur", None)),
                ("Preferences", "preferences-system-symbolic", lambda: app.activate_action("preferences", None)),
                ("Keyboard Shortcuts", "preferences-desktop-keyboard-shortcuts-symbolic",
                 lambda: app.activate_action("shortcuts", None)),
                ("Reload WhatsApp", "view-refresh-symbolic", lambda: app.window.account and app.window.account.reload()),
                ("Add Account", "list-add-symbolic", lambda: app.activate_action("add-account", None)),
            ]
            for account in app.accounts if several else []:
                commands.append((f"Switch to {account.name}", "system-users-symbolic",
                                 lambda a=account: app.window.show_account(a)))
            scored = sorted(((score(query, name), name, icon, run) for name, icon, run in commands),
                            key=lambda s: -s[0])
            found += [Entry(name, "", run, avatar=False, icon=icon) for s, name, icon, run in scored if s >= 2]
        return found

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
                "Natter knows the chats it has seen in WhatsApp's list. Scroll through it once to find older ones."
                if query else "Chats show up here once WhatsApp's chat list has loaded.")
            self.content.set_visible_child_name("empty")

    def _row(self, entry: Entry) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
        if entry.avatar:
            box.append(Adw.Avatar(size=32, text=entry.title, show_initials=True))
        else:
            icon = Gtk.Image(icon_name=entry.icon, pixel_size=16, width_request=32)
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
