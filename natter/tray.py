"""Natter's tray icon: a StatusNotifierItem with a dbusmenu, the protocol Ada's panel hosts."""

from __future__ import annotations

import os
from typing import Callable, Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

from . import APP_ID, NAME, OBJECT_PATH
from .launcher import menu_name
from .system import in_sandbox

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = OBJECT_PATH + "/TrayMenu"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
PIXMAP_SIZES = (22, 32, 48)

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
    <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
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


def tray_host_available(bus: Gio.DBusConnection) -> bool:
    """Whether a panel on this desktop shows tray icons (asks the watcher, waiting up to 500 ms)."""
    try:
        reply = bus.call_sync(WATCHER, WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (WATCHER, "IsStatusNotifierHostRegistered")),
                              GLib.VariantType("(v)"), Gio.DBusCallFlags.NO_AUTO_START, 500, None)
    except GLib.Error:
        return False  # no watcher (ServiceUnknown), so nothing shows tray icons
    return bool(reply.unpack()[0])


def argb(pixbuf: GdkPixbuf.Pixbuf) -> bytes:
    """The pixels as StatusNotifierItem wants them: ARGB32 in network byte order, rows unpadded."""
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    rgba = b"".join(pixels[y * stride: y * stride + width * 4] for y in range(height))
    out = bytearray(len(rgba))
    out[0::4], out[1::4], out[2::4], out[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
    return bytes(out)


def pixmaps(svg: str) -> list[tuple[int, int, bytes]]:
    """The icon drawn at the sizes panels use, for panels that can't find it by name."""
    drawn = []
    for size in PIXMAP_SIZES:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(svg, size, size)
        except GLib.Error:
            return []
        drawn.append((pixbuf.get_width(), pixbuf.get_height(), argb(pixbuf)))
    return drawn


class MenuItem:
    def __init__(self, label: str = "", run: Optional[Callable[[], None]] = None, *, separator: bool = False,
                 enabled: bool = True) -> None:
        self.label, self.run, self.separator, self.enabled = menu_name(label), run, separator, enabled

    def key(self) -> tuple:
        """What the panel sees of it, to tell whether a menu changed."""
        return (self.separator, self.label, self.enabled and self.run is not None)

    def props(self) -> dict:
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", self.label.replace("_", "__")),
                "enabled": GLib.Variant("b", self.enabled and self.run is not None)}


class Tray:
    """The icon in the panel.

    `menu()` builds the menu: once per revision, so an item keeps its id until the menu changes.
    Call `refresh()` when what it shows changes. `on_activate(token)` runs on a click, with the
    panel's activation token ("" if it gave none); a menu item's `run` can read `last_token`.
    `on_host_changed(available)` runs when Natter learns whether a panel shows the icon.
    """

    def __init__(self, bus: Gio.DBusConnection, icon_path: str, on_activate: Callable[[str], None],
                 menu: Callable[[], list[MenuItem]],
                 on_host_changed: Optional[Callable[[bool], None]] = None) -> None:
        self.bus = bus
        self.sandboxed = in_sandbox()
        self.icon_path = "" if self.sandboxed else icon_path  # the panel can't see inside a sandbox
        self.on_activate = on_activate
        self.build_menu = menu
        self.on_host_changed = on_host_changed
        self.unread = 0
        self.last_token = ""
        self.revision = 1
        self.items: Optional[dict[int, MenuItem]] = None  # this revision's menu, by id
        self.next_id = 1
        self.host: Optional[bool] = None
        self.watcher = False
        self.service: Optional[str] = None  # the bus name the watcher knows the item by
        apps = os.path.join(icon_path, "hicolor", "scalable", "apps")
        self.pixmaps = pixmaps(os.path.join(apps, f"{ICON}.svg"))
        self.unread_pixmaps = pixmaps(os.path.join(apps, f"{ICON_UNREAD}.svg"))
        self._ids = [
            bus.register_object(ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0],
                                self._item_call, self._item_prop, None),
            bus.register_object(MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
                                self._menu_call, self._menu_prop, None),
        ]
        self._signals = [bus.signal_subscribe(WATCHER, WATCHER, signal, WATCHER_PATH, None,
                                              Gio.DBusSignalFlags.NONE, lambda *_: self._check_host())
                         for signal in ("StatusNotifierHostRegistered", "StatusNotifierHostUnregistered")]
        self._owner = 0
        if self.sandboxed:
            self.service = bus.get_unique_name()
        else:
            # The name panels know tray items by, and what Ada's lifecycle daemon may look for.
            self._owner = Gio.bus_own_name_on_connection(bus, f"org.kde.StatusNotifierItem-{os.getpid()}-1",
                                                         Gio.BusNameOwnerFlags.NONE, self._named, self._unnamed)
        # Register now, and again whenever the panel restarts.
        self._watch = Gio.bus_watch_name_on_connection(bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                       self._watcher_appeared, self._watcher_vanished)

    def close(self) -> None:
        Gio.bus_unwatch_name(self._watch)
        for subscription in self._signals:
            self.bus.signal_unsubscribe(subscription)
        if self.service == self.bus.get_unique_name():
            # Registered by the connection's own name, which outlives the icon: panels hide passive items.
            self._emit("NewStatus", GLib.Variant("(s)", ("Passive",)))
        if self._owner:
            Gio.bus_unown_name(self._owner)
        for reg in self._ids:
            self.bus.unregister_object(reg)

    def _named(self, _bus, name: str) -> None:
        self.service = name
        self._register()

    def _unnamed(self, bus, _name) -> None:
        if self.service is None:
            self.service = bus.get_unique_name()
            self._register()

    def _watcher_appeared(self, _bus, _name, _owner) -> None:
        self.watcher = True
        self._register()
        self._check_host()

    def _watcher_vanished(self, _bus, _name) -> None:
        self.watcher = False
        self._set_host(False)

    def _register(self) -> None:
        if self.watcher and self.service:
            self.bus.call(WATCHER, WATCHER_PATH, WATCHER, "RegisterStatusNotifierItem",
                          GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, 3000, None, None)

    def _check_host(self) -> None:
        def got(bus, result) -> None:
            try:
                self._set_host(bool(bus.call_finish(result).unpack()[0]))
            except GLib.Error:
                self._set_host(False)

        self.bus.call(WATCHER, WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (WATCHER, "IsStatusNotifierHostRegistered")), GLib.VariantType("(v)"),
                      Gio.DBusCallFlags.NO_AUTO_START, 500, None, got)

    def _set_host(self, available: bool) -> None:
        if available != self.host:
            self.host = available
            if self.on_host_changed:
                self.on_host_changed(available)

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
            "IconPixmap": GLib.Variant("a(iiay)", self.unread_pixmaps if self.unread else self.pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ICON_UNREAD),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", self.unread_pixmaps),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (ICON, [], NAME, tip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _item_call(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation) -> None:
        invocation.return_value(None)
        if method == "ProvideXdgActivationToken":
            self.last_token = params.unpack()[0]
        elif method in ("Activate", "SecondaryActivate", "ContextMenu"):
            # A right click asks for ContextMenu on panels that don't read the dbusmenu, such as swaybar.
            token, self.last_token = self.last_token, ""
            self.on_activate(token)

    # ---- com.canonical.dbusmenu ------------------------------------------------------------

    def refresh(self) -> None:
        """What the menu shows changed: panels fetch it again."""
        self.items = None
        self.revision += 1
        self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                             GLib.Variant("(ui)", (self.revision, 0)))

    def _menu(self) -> dict[int, MenuItem]:
        if self.items is None:
            self.items = self._number(self.build_menu())
        return self.items

    def _number(self, items: list[MenuItem]) -> dict[int, MenuItem]:
        """Ids never reused, so a click meant for an older menu can't land on another item."""
        numbered = {}
        for item in items:
            numbered[self.next_id] = item
            self.next_id += 1
        return numbered

    def _about_to_show(self) -> bool:
        """The menu is opening: build it again, and say whether that changed it."""
        fresh = self.build_menu()
        if self.items is not None:
            if [item.key() for item in fresh] == [item.key() for item in self.items.values()]:
                return False
            self.refresh()
        self.items = self._number(fresh)
        return True

    def _menu_prop(self, _bus, _sender, _path, _iface, name: str) -> Optional[GLib.Variant]:
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_path] if self.icon_path else []),
        }.get(name)

    def _layout(self, parent: int) -> GLib.Variant:
        items = self._menu()
        if parent in items:
            return GLib.Variant("(ia{sv}av)", (parent, items[parent].props(), []))
        children = [GLib.Variant("(ia{sv}av)", (i, item.props(), [])) for i, item in items.items()]
        return GLib.Variant("(ia{sv}av)", (0, {"children-display": GLib.Variant("s", "submenu")}, children))

    def _menu_call(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation) -> None:
        args = params.unpack()
        if method == "GetLayout":
            layout = self._layout(args[0])
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", self.revision), layout))
        elif method == "GetGroupProperties":
            ids, names = args
            items = self._menu()
            found = [(i, {k: v for k, v in items[i].props().items() if not names or k in names})
                     for i in (ids or list(items)) if i in items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (found,)))
        elif method == "GetProperty":
            item = self._menu().get(args[0])
            value = item.props().get(args[1]) if item else None
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", "") if value is None else value,)))
        elif method == "Event":
            invocation.return_value(None)
            self._event(args[0], args[1])
        elif method == "EventGroup":
            missing = [item_id for item_id, event, _data, _time in args[0] if not self._event(item_id, event)]
            invocation.return_value(GLib.Variant("(ai)", (missing,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (args[0] == 0 and self._about_to_show(),)))
        elif method == "AboutToShowGroup":
            ids = args[0]
            updates = [0] if 0 in ids and self._about_to_show() else []
            missing = [i for i in ids if i != 0 and i not in self._menu()]
            invocation.return_value(GLib.Variant("(aiai)", (updates, missing)))
        else:
            invocation.return_value(None)

    def _event(self, item_id: int, event: str) -> bool:
        """A click on one of this revision's items. False if the id isn't one of them."""
        item = (self.items or {}).get(item_id)
        if item is None:
            return False
        if event == "clicked" and item.enabled and item.run:
            GLib.idle_add(self._run, item)
        return True

    def _run(self, item: MenuItem) -> bool:
        item.run()
        self.last_token = ""
        return GLib.SOURCE_REMOVE
