"""Natter's chats in the desktop's search, over org.gnome.Shell.SearchProvider2.

That interface is what GNOME Shell (and KRunner, through a bridge) asks apps for results with,
so it's the one a desktop search API is most likely to take. A desktop finds Natter through
an .ini file (see install.sh) naming the bus name and this object path.

Only chat names Natter has seen in WhatsApp's list are searched, never messages.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gio, GLib

from . import APP_ID, OBJECT_PATH
from .launcher import menu_name
from .settings import Chat, rank, score

PATH = OBJECT_PATH + "/SearchProvider"
LIMIT = 8

XML = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""


def result_id(account_id: str, name: str) -> str:
    return f"{account_id}/{name}"


def split_id(identifier: str) -> tuple[str, str]:
    account_id, _, name = identifier.partition("/")
    return account_id, name


class SearchProvider:
    """Answers the desktop's search with chats by name.

    `chats()` lists (account id, account name, Chat) for every account, from what Natter keeps on
    disk, so a search never starts WhatsApp. `open_chat(account_id, name)` and
    `launch_search(query)` act on what the user picked; `enabled()` says whether search is on.
    """

    def __init__(self, bus: Gio.DBusConnection, chats: Callable[[], list[tuple[str, str, Chat]]],
                 open_chat: Callable[[str, str], None], launch_search: Callable[[str], None],
                 enabled: Callable[[], bool]) -> None:
        self.bus = bus
        self.chats = chats
        self.open_chat = open_chat
        self.launch_search = launch_search
        self.enabled = enabled
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self._id = bus.register_object(PATH, info, self._on_call, None, None)

    def close(self) -> None:
        self.bus.unregister_object(self._id)

    def search(self, terms: list[str]) -> list[str]:
        query = " ".join(terms).strip()
        if not query or not self.enabled():
            return []
        found = self.chats()
        owner = {id(chat): account_id for account_id, _name, chat in found}
        # Desktop search wants sure hits: names that contain what was typed, not letters in order.
        ranked = [c for c in rank(query, [chat for _a, _n, chat in found]) if score(query, c.name) >= 2]
        return [result_id(owner[id(c)], c.name) for c in ranked[:LIMIT]]

    def metas(self, identifiers: list[str]) -> list[dict]:
        accounts = {account_id: account_name for account_id, account_name, _chat in self.chats()}
        several = len(accounts) > 1
        found = []
        for identifier in identifiers:
            account_id, name = split_id(identifier)
            if account_id not in accounts:
                continue
            found.append({
                "id": GLib.Variant("s", identifier),
                "name": GLib.Variant("s", menu_name(name)),
                "description": GLib.Variant("s", f"WhatsApp chat · {menu_name(accounts[account_id])}" if several
                                            else "WhatsApp chat"),
                "gicon": GLib.Variant("s", APP_ID),
            })
        return found

    def _activate(self, account_id: str, name: str) -> bool:
        # Only chats Natter offered: anything on the bus can call this.
        if any(a == account_id and chat.name == name for a, _n, chat in self.chats()):
            self.open_chat(account_id, name)
        return GLib.SOURCE_REMOVE

    def _launch(self, query: str) -> bool:
        self.launch_search(query)
        return GLib.SOURCE_REMOVE

    def _on_call(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation) -> None:
        args = params.unpack()
        if method == "GetInitialResultSet":
            invocation.return_value(GLib.Variant("(as)", (self.search(args[0]),)))
        elif method == "GetSubsearchResultSet":
            invocation.return_value(GLib.Variant("(as)", (self.search(args[1]),)))
        elif method == "GetResultMetas":
            invocation.return_value(GLib.Variant("(aa{sv})", (self.metas(args[0]),)))
        elif method == "ActivateResult":
            invocation.return_value(None)
            GLib.idle_add(self._activate, *split_id(args[0]))
        elif method == "LaunchSearch":
            invocation.return_value(None)
            GLib.idle_add(self._launch, " ".join(args[0]))
        else:
            invocation.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method)
