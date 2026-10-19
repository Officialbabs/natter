"""Natter's chats in the desktop's search, over org.gnome.Shell.SearchProvider2.

That interface is what GNOME Shell (and KRunner, through a bridge) asks apps for results with,
so it's the one a desktop search API is most likely to take. A desktop finds Natter through
an .ini file (see install.sh) naming the bus name and this object path.

Only chat names Natter has seen in WhatsApp's list are searched, never messages.
"""

from __future__ import annotations

from typing import Optional

from gi.repository import Gio, GLib

from . import APP_ID
from .settings import rank, score

PATH = "/" + APP_ID.replace(".", "/") + "/SearchProvider"
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
    def __init__(self, app, bus: Gio.DBusConnection) -> None:
        self.app = app
        self.bus = bus
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self._id = bus.register_object(PATH, info, self._on_call, None, None)

    def close(self) -> None:
        self.bus.unregister_object(self._id)

    def search(self, terms: list[str]) -> list[str]:
        if not self.app.settings["search_provider"]:
            return []
        self.app.start_hidden()  # started by the desktop just for this: chats load from disk
        query = " ".join(terms).strip()
        if not query:
            return []
        chats = [(c, a) for a in self.app.accounts for c in a.chats.recent()]
        owner = {id(c): a for c, a in chats}
        # Desktop search wants sure hits: names that contain what was typed, not letters in order.
        ranked = [c for c in rank(query, [c for c, _ in chats]) if score(query, c.name) >= 2]
        return [result_id(owner[id(c)].id, c.name) for c in ranked[:LIMIT]]

    def metas(self, identifiers: list[str]) -> list[dict]:
        several = len(self.app.accounts) > 1
        found = []
        for identifier in identifiers:
            account_id, name = split_id(identifier)
            account = self.app.account_by_id(account_id)
            if account is None:
                continue
            found.append({
                "id": GLib.Variant("s", identifier),
                "name": GLib.Variant("s", name),
                "description": GLib.Variant("s", f"WhatsApp chat · {account.name}" if several else "WhatsApp chat"),
                "gicon": GLib.Variant("s", APP_ID),
            })
        return found

    def _on_call(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation) -> None:
        args = params.unpack()
        if method == "GetInitialResultSet":
            invocation.return_value(GLib.Variant("(as)", (self.search(args[0]),)))
        elif method == "GetSubsearchResultSet":
            invocation.return_value(GLib.Variant("(as)", (self.search(args[1]),)))
        elif method == "GetResultMetas":
            invocation.return_value(GLib.Variant("(aa{sv})", (self.metas(args[0]),)))
        elif method == "ActivateResult":
            account_id, name = split_id(args[0])
            invocation.return_value(None)
            GLib.idle_add(lambda: self.app.go_to(self.app.account_by_id(account_id), name) and False)
        elif method == "LaunchSearch":
            invocation.return_value(None)
            GLib.idle_add(lambda: self.app.go_to_chat(query=" ".join(args[0])) and False)
        else:
            invocation.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method)
