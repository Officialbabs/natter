"""Natter's place among the desktop's processes: its own systemd scope, and memory it gives back.

Ada (like GNOME) starts every app in a transient scope, app-ada-<app id>-<pid>.scope, so the
Task Manager and the lifecycle daemon see the app's processes as one group. Natter started any
other way (a terminal, autostart, `natter` from a script) would otherwise land in whatever
scope started it, with its WebKit processes counted as part of that app.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID, NAME

CGROUP_ROOT = Path("/sys/fs/cgroup")


def own_cgroup() -> Optional[Path]:
    """This process's cgroup (v2) folder, if there is one we can see."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return CGROUP_ROOT / line[3:].lstrip("/")
    except OSError:
        pass
    return None


def in_own_scope(cgroup: Optional[Path] = None) -> bool:
    """Whether Natter already has a scope of its own (Ada's launcher makes one, or we did)."""
    cgroup = cgroup or own_cgroup()
    return bool(cgroup and cgroup.name.endswith(".scope") and APP_ID in cgroup.name)


def scope_name(pid: int) -> str:
    """The XDG name for an app's scope: app-<app id>-<pid>.scope."""
    return f"app-{APP_ID}-{pid}.scope"


def move_to_own_scope(timeout: float = 2.0) -> Optional[str]:
    """Put Natter in a transient scope of its own, before it starts any WebKit process.

    Returns the scope's name, or None if Natter already has one or systemd isn't there.
    """
    if in_own_scope():
        return None
    pid = os.getpid()
    name = scope_name(pid)
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        properties = [
            ("PIDs", GLib.Variant("au", [pid])),
            ("Description", GLib.Variant("s", NAME)),
            ("Slice", GLib.Variant("s", "app.slice")),
            ("CollectMode", GLib.Variant("s", "inactive-or-failed")),
        ]
        bus.call_sync("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                      "org.freedesktop.systemd1.Manager", "StartTransientUnit",
                      GLib.Variant("(ssa(sv)a(sa(sv)))", (name, "fail", properties, [])),
                      GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as error:
        print(f"natter: couldn't get a scope of its own: {error.message}")
        return None
    # systemd moves the process once the job runs; wait so WebKit's processes follow it.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if in_own_scope():
            return name
        time.sleep(0.02)
    return name


class Reclaimer:
    """Gives Natter's cold memory back while its window is hidden, the way Ada does for paused
    apps, except Natter keeps running so messages and notifications still arrive.

    Pages WhatsApp hasn't touched in a while go to (compressed) swap in small steps; anything
    it needs again comes straight back. It stops at `floor` MB, or when the kernel can't find
    more to take.
    """

    STEP = "64M"

    def __init__(self, floor_mb: int = 300, on_done: Optional[Callable[[int, int], None]] = None) -> None:
        self.cgroup = own_cgroup()
        self.floor = floor_mb * 1024 * 1024
        self.on_done = on_done
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return bool(self.cgroup and in_own_scope(self.cgroup) and os.access(self.cgroup / "memory.reclaim", os.W_OK))

    def usage(self) -> int:
        try:
            return int((self.cgroup / "memory.current").read_text())
        except (OSError, ValueError, TypeError):
            return 0

    def start(self) -> bool:
        if not self.available or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="natter-reclaim", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _step(self) -> bool:
        """Ask the kernel for one step. False when it has nothing more to give."""
        fd = os.open(self.cgroup / "memory.reclaim", os.O_WRONLY)
        try:
            for command in (f"{self.STEP} swappiness=200", self.STEP):
                try:
                    os.write(fd, command.encode())
                    return True
                except OSError as error:
                    if error.errno != errno.EINVAL:  # EAGAIN: nothing left to take
                        return False
                    # EINVAL: a kernel without the swappiness option; try it plain.
            return False
        finally:
            os.close(fd)

    def _run(self) -> None:
        before = self.usage()
        while not self._stop.is_set() and self.usage() > self.floor:
            try:
                if not self._step():
                    break
            except OSError:
                break
            self._stop.wait(0.05)
        after = self.usage()
        if self.on_done:
            GLib.idle_add(self.on_done, before, after)
