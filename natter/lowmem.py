"""Natter's answer to the system's memory monitor (Gio.MemoryMonitor): when the computer says it's
short of memory, Natter gives back what it can without the person noticing.

At every warning, low or worse, each page's memory cache goes (WebsiteDataTypes.MEMORY_CACHE):
pictures, scripts and style sheets WebKit keeps decoded in the web process, which it fetches again
from its disk cache when the page next needs them. Nothing else of WhatsApp's: not its cookies,
local storage, IndexedDB, service workers or the disk cache, so the account stays linked and its
chats stay. At a critical warning Natter also drops what it keeps of its own and can make again
(the chats' pictures for notifications, notify.py), collects Python's garbage and hands malloc's
free pages back to the kernel. Never a reload, and never the web process's end: that's the guard's
(memguard.py) and the fresh pages' (app.py), whose rules the warnings don't change.

A monitor may say the same for as long as memory stays short, every few seconds: each level is
answered once in AGAIN, and a clear still under way isn't asked for again meanwhile.

GLib's own monitor listens on the system bus, which Natter hides from itself (system.py). The
desktop portal passes the same warnings on over the session bus, so while the system bus is
hidden, PortalMonitor listens there instead.
"""

from __future__ import annotations

import ctypes
import gc
import os
import time
from typing import Callable, Optional

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, GObject, WebKit

from . import NAME
from .memguard import held, page_sizes
from .system import NO_SYSTEM_BUS, SYSTEM_BUS

AGAIN = 30.0  # seconds before the same level is answered again
CRITICAL = int(Gio.MemoryMonitorWarningLevel.CRITICAL)


def level_name(level: int) -> str:
    """The level's name: low, medium, critical, or the number of one GLib doesn't name."""
    try:
        return Gio.MemoryMonitorWarningLevel(level).value_nick
    except (ValueError, AttributeError):
        return str(level)


def sizes_line(before: int, after: int) -> str:
    """What was held before and after, and what came back, for the log."""
    return f" held {before >> 20} → {after >> 20} MB ({max(0, before - after) >> 20} MB back)"


def trim() -> None:
    """Python's garbage collected, then malloc's free pages back to the kernel (glibc only)."""
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (OSError, AttributeError):
        pass


class PortalMonitor(GObject.Object):
    """The desktop portal's memory warnings (org.freedesktop.portal.MemoryMonitor, on the session
    bus), as a GObject with Gio.MemoryMonitor's low-memory-warning signal."""

    __gsignals__ = {
        "low-memory-warning": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self, bus: Optional[Gio.DBusConnection] = None) -> None:
        super().__init__()
        self._bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._subscription = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.MemoryMonitor", "LowMemoryWarning",
            "/org/freedesktop/portal/desktop", None, Gio.DBusSignalFlags.NONE, self._heard)

    def _heard(self, _bus, _sender, _path, _interface, _signal, parameters) -> None:
        self.emit("low-memory-warning", parameters.unpack()[0])

    def close(self) -> None:
        if self._subscription:
            self._bus.signal_unsubscribe(self._subscription)
            self._subscription = 0


def default_monitor() -> GObject.Object:
    """GLib's monitor, or the portal's while Natter has hidden the system bus from itself."""
    if os.environ.get(SYSTEM_BUS) == NO_SYSTEM_BUS:
        return PortalMonitor()
    return Gio.MemoryMonitor.dup_default()


class LowMemory:
    """Answers the warnings: `managers()` gives the pages' WebsiteDataManagers as they are now,
    `own()` drops Natter's own caches and says what went (critical only). `sizes()` (the pages,
    by pid) and `natter_held()` (Natter itself) are read before and after, for the log."""

    def __init__(self, managers: Callable[[], list], own: Optional[Callable[[], str]] = None,
                 sizes: Callable[[], dict[int, int]] = page_sizes,
                 natter_held: Callable[[], Optional[int]] = lambda: held(os.getpid()),
                 trim: Callable[[], None] = trim, clock: Callable[[], float] = time.monotonic) -> None:
        self.managers = managers
        self.own = own
        self.sizes = sizes
        self.natter_held = natter_held
        self.trim = trim
        self.clock = clock
        self._answered: dict[int, float] = {}  # level -> when it was last answered
        self._clearing: Optional[float] = None  # when the clear under way began
        self._monitor: Optional[GObject.Object] = None
        self._handler = 0

    def start(self, monitor: Optional[GObject.Object] = None) -> None:
        self._monitor = monitor or default_monitor()
        self._handler = self._monitor.connect("low-memory-warning", lambda _monitor, level: self.warned(level))

    def stop(self) -> None:
        if self._monitor is not None and self._handler:
            self._monitor.disconnect(self._handler)
        if isinstance(self._monitor, PortalMonitor):
            self._monitor.close()
        self._monitor, self._handler = None, 0

    def warned(self, level) -> bool:
        """A warning at `level`: the caches go, unless this level was answered within AGAIN.
        Whether it was answered now."""
        level, now = int(level), self.clock()
        last = self._answered.get(level)
        if last is not None and now - last < AGAIN:
            return False
        self._answered[level] = now
        name = level_name(level)
        if self._clearing is None or now - self._clearing >= AGAIN:  # one stuck page doesn't stop the next
            self._clear(name, now)
        if level >= CRITICAL:
            self._drop_own(name)
        return True

    def _clear(self, name: str, now: float) -> None:
        managers = list(self.managers())
        if not managers:
            print(f"natter: memory: the system's memory monitor warns ({name}); no page to clear yet", flush=True)
            return
        before = self.sizes()
        self._clearing = now
        waiting = [len(managers)]

        def cleared(manager, result) -> None:
            try:
                manager.clear_finish(result)
            except GLib.Error as error:
                print(f"natter: memory: couldn't clear a page's memory cache: {error.message}", flush=True)
            waiting[0] -= 1
            if waiting[0]:
                return
            self._clearing = None
            pages = "1 page" if len(managers) == 1 else f"{len(managers)} pages"
            measured = "; they" + sizes_line(sum(before.values()), sum(self.sizes().values())) if before else ""
            print(f"natter: memory: the system's memory monitor warns ({name}): cleared the memory cache of "
                  f"{pages}{measured}", flush=True)

        for manager in managers:
            manager.clear(WebKit.WebsiteDataTypes.MEMORY_CACHE, 0, None, cleared)

    def _drop_own(self, name: str) -> None:
        before = self.natter_held()
        words = self.own() if self.own else ""
        self.trim()
        after = self.natter_held()
        dropped = f"dropped {words} and " if words else ""
        sizes = f"; {NAME}{sizes_line(before, after)}" if before is not None and after is not None else ""
        print(f"natter: memory: the system's memory monitor warns ({name}): {dropped}gave Python's and "
              f"malloc's free memory back{sizes}", flush=True)
