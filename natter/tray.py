"""Natter's tray icon: a StatusNotifierItem with a dbusmenu, the protocol Ada's panel hosts."""

from __future__ import annotations

import os
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID, NAME

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/io/natter/Natter/TrayMenu"
WATCHER = "org.kde.StatusNotifierWatcher"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

ICON = APP_ID
ICON_UNREAD = f"{APP_ID}-unread"


class MenuItem:
    def __init__(self, label: str = "", run: Optional[Callable[[], None]] = None, *, separator: bool = False,
                 enabled: bool = True) -> None:
        self.label, self.run, self.separator, self.enabled = label, run, separator, enabled

    def props(self) -> dict:
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", self.label.replace("_", "__")),
                "enabled": GLib.Variant("b", self.enabled and self.run is not None)}


class Tray:
    """The icon in the panel. `menu()` builds the menu fresh each time it opens."""

    def __init__(self, bus: Gio.DBusConnection, icon_path: str, on_activate: Callable[[], None],
                 menu: Callable[[], list[MenuItem]]) -> None:
        self.bus = bus
        self.icon_path = icon_path
        self.on_activate = on_activate
        self.build_menu = menu
        self.unread = 0
        self.items: list[MenuItem] = []
        self.revision = 1
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._ids = [
            bus.register_object(ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0],
                                self._item_call, self._item_prop, None),
            bus.register_object(MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
                                self._menu_call, self._menu_prop, None),
        ]
        self._owner = Gio.bus_own_name_on_connection(bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # Register now, and again whenever the panel restarts.
        self._watch = Gio.bus_watch_name_on_connection(bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                       self._register, None)

    def close(self) -> None:
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._owner)
        for reg in self._ids:
            self.bus.unregister_object(reg)

    def _register(self, bus, *_args) -> None:
        bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                 GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, 3000, None, None)

    def set_unread(self, count: int) -> None:
        if count == self.unread:
            return
        was = self.unread
        self.unread = count
        self._emit("NewToolTip")
        if bool(was) != bool(count):
            self._emit("NewIcon")
            self._emit("NewStatus", GLib.Variant("(s)", (self._status(),)))

    def _status(self) -> str:
        return "NeedsAttention" if self.unread else "Active"

    def _emit(self, signal: str, params: Optional[GLib.Variant] = None) -> None:
        self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", signal, params)

    # ---- org.kde.StatusNotifierItem --------------------------------------------------------

    def _item_prop(self, _bus, _sender, _path, _iface, name: str) -> Optional[GLib.Variant]:
        tip = f"{self.unread} unread" if self.unread else "No unread messages"
        values = {
            "Category": GLib.Variant("s", "Communications"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", NAME),
            "Status": GLib.Variant("s", self._status()),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", ICON_UNREAD if self.unread else ICON),
            "IconThemePath": GLib.Variant("s", self.icon_path),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ICON_UNREAD),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (ICON, [], NAME, tip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _item_call(self, _bus, _sender, _path, _iface, method: str, _params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            self.on_activate()
        invocation.return_value(None)

    # ---- com.canonical.dbusmenu ------------------------------------------------------------

    def _menu_prop(self, _bus, _sender, _path, _iface, name: str) -> Optional[GLib.Variant]:
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_path]),
        }.get(name)

    def _layout(self) -> GLib.Variant:
        self.items = self.build_menu()
        children = [GLib.Variant("(ia{sv}av)", (i + 1, item.props(), [])) for i, item in enumerate(self.items)]
        root = {"children-display": GLib.Variant("s", "submenu")}
        return GLib.Variant("(ia{sv}av)", (0, root, children))

    def _menu_call(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation) -> None:
        if method == "GetLayout":
            self.revision += 1
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", self.revision), self._layout()))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            if not self.items:
                self.items = self.build_menu()
            found = [(i, self.items[i - 1].props()) for i in ids if 0 < i <= len(self.items)]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (found,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            props = self.items[item_id - 1].props() if 0 < item_id <= len(self.items) else {}
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            item_id, event = params.unpack()[:2]
            self._event(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _time in params.unpack()[0]:
                self._event(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([0], [])))
        else:
            invocation.return_value(None)

    def _event(self, item_id: int, event: str) -> None:
        if event == "clicked" and 0 < item_id <= len(self.items):
            item = self.items[item_id - 1]
            if item.run:
                GLib.idle_add(lambda: item.run() and False)
