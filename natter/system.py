"""Natter's place on the system: its own systemd scope and the memory limits on it, memory it
gives back, the sandbox, the Background portal, and the GStreamer codecs WhatsApp's media needs.

Desktops start each app in a transient unit named after it (GNOME: app-gnome-<app id>-<pid>.scope,
Ada: app-ada-…, KDE: app-<app id>@<random>.service, Flatpak: app-flatpak-<app id>-<pid>.scope),
so the task manager and the desktop see the app's processes as one group. Natter started any
other way (a terminal, autostart, a script) would otherwise land in whatever unit started it,
with its WebKit processes counted as that app's.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID, NAME
from .distro import GST_PYTHON, MEDIA, install_hint
from .memguard import mem_total, scope_limits

SYSTEMD = "org.freedesktop.systemd1"
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"


def in_sandbox() -> bool:
    """Whether Natter runs inside Flatpak or Snap, where the desktop is reached through portals."""
    return Path("/.flatpak-info").exists() or confined_snap(os.environ.get("SNAP"))


def confined_snap(snap: Optional[str]) -> bool:
    """Whether $SNAP is the folder of a confined snap: its meta/snap.yaml is there and doesn't
    say classic. The same test GLib makes: a classic snap isn't sandboxed, yet a terminal
    started from one passes SNAP on to everything run in it."""
    if not snap:
        return False
    try:
        meta = Path(snap, "meta", "snap.yaml").read_text()
    except (OSError, ValueError):
        return False
    confinement = re.search(r"^confinement:[ \t]*(\S*)", meta, re.M)
    return not (confinement and confinement[1].strip("'\"") == "classic")


# ---- Real-time threads ---------------------------------------------------------------------

# WebKit makes some of its threads real-time by asking RealtimeKit, on the system bus. First it
# caps the process's real-time CPU time at RealtimeKit's limit, 200 ms, as RealtimeKit requires.
# The kernel kills (SIGKILL) a process whose real-time thread runs that long without a pause.
# WebKit's network process has one such thread, "Storage", which runs IndexedDB. Loading
# WhatsApp's database it ran past 200 ms, and the network process died, often seconds after
# Natter started. The page was left cut short, half loaded, or failing every load after.
# Nothing in Natter needs the system bus, so Natter and the WebKit processes it starts don't see
# it: WebKit can't ask, and its threads stay ordinary ones with no cap.
SYSTEM_BUS = "DBUS_SYSTEM_BUS_ADDRESS"
NO_SYSTEM_BUS = "unix:path=/dev/null/natter-has-no-system-bus"
_system_bus_before: Optional[tuple[Optional[str]]] = None  # (the address it had,) once hidden


def hide_system_bus(environ=None, sandboxed: Optional[bool] = None) -> bool:
    """Hide the system bus from Natter and what it starts. Call it before anything connects to
    the system bus (GLib keeps the connection it makes). Inside Flatpak or Snap, WebKit asks a
    portal on the session bus instead, so nothing changes there. True when it hid it."""
    global _system_bus_before
    environ = os.environ if environ is None else environ
    if (in_sandbox() if sandboxed is None else sandboxed) or environ.get(SYSTEM_BUS) == NO_SYSTEM_BUS:
        return False
    _system_bus_before = (environ.get(SYSTEM_BUS),)
    environ[SYSTEM_BUS] = NO_SYSTEM_BUS
    return True


def give_system_bus(launcher) -> None:
    """Give a program Natter starts (not a WebKit one) the system bus Natter hid from itself,
    and take off the camera fix's GStreamer settings, which are for WebKit only (gstfix.py).
    `launcher` is a Gio.SubprocessLauncher or a Gio.AppLaunchContext."""
    from .gstfix import launch_env

    launch_env(launcher)
    if _system_bus_before is None:
        return
    address = _system_bus_before[0]
    if address is None:
        launcher.unsetenv(SYSTEM_BUS)
    elif isinstance(launcher, Gio.SubprocessLauncher):
        launcher.setenv(SYSTEM_BUS, address, True)
    else:
        launcher.setenv(SYSTEM_BUS, address)


def open_uri(uri: str, window=None) -> None:
    """Open `uri` (a link, or a file's file:// address) in the app the desktop has for it.

    Outside Flatpak and Snap GTK starts that app itself, with Natter's environment, so a browser
    it started would have no system bus (hide_system_bus) and would load the camera fix's
    GStreamer tracer (gstfix.py). So Natter starts it, with the system bus given back and the
    tracer taken off. Inside a sandbox GTK asks the OpenURI portal, and nothing was changed."""
    from gi.repository import Gdk, Gtk

    from .gstfix import cleaned

    if _system_bus_before is None and not cleaned():
        Gtk.UriLauncher.new(uri).launch(window, None, None, None)
        return
    display = window.get_display() if window is not None else Gdk.Display.get_default()
    context = display.get_app_launch_context()
    give_system_bus(context)

    def done(_source, result) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri_finish(result)
        except GLib.Error as error:
            print(f"natter: couldn't open {urlparse(uri).scheme} address: {error.message}", flush=True)

    Gio.AppInfo.launch_default_for_uri_async(uri, context, None, done)


def network_processes(pid: Optional[int] = None, proc: Path = Path("/proc")) -> set[int]:
    """The WebKit network processes Natter started: its children named WebKitNetworkProcess."""
    pid = os.getpid() if pid is None else pid
    try:
        tasks = os.listdir(proc / str(pid) / "task")
    except OSError:
        return set()
    found = set()
    for task in tasks:
        for child in _read(proc / str(pid) / "task" / task / "children").split():
            # The name is cut to 15 characters: "WebKitNetworkPr".
            if child.isdigit() and _read(proc / child / "comm").startswith("WebKitNetwork"):
                found.add(int(child))
    return found


def _read(path) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _ask_bus(bus: Gio.DBusConnection, method: str, args: Optional[GLib.Variant], reply: str):
    try:
        return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", method,
                             args, GLib.VariantType(reply), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    except GLib.Error:
        return None


def _has_owner(bus: Gio.DBusConnection, name: str) -> bool:
    return bool(_ask_bus(bus, "NameHasOwner", GLib.Variant("(s)", (name,)), "(b)"))


# ---- Scope ----------------------------------------------------------------------------------

def cgroup2_mount(mountinfo: Optional[str] = None) -> Optional[Path]:
    """Where the cgroup v2 tree is mounted (/sys/fs/cgroup almost always), from mountinfo."""
    for line in (_read("/proc/self/mountinfo") if mountinfo is None else mountinfo).splitlines():
        # ID PARENT MAJOR:MINOR ROOT MOUNT-POINT OPTIONS [OPTIONAL…] - TYPE SOURCE SUPER-OPTIONS
        fields = line.split()
        if "-" in fields and fields[fields.index("-") + 1:fields.index("-") + 2] == ["cgroup2"]:
            # A space in the mount point is written \040, and so on.
            return Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
    return None


def own_cgroup(cgroup: Optional[str] = None, mountinfo: Optional[str] = None) -> Optional[Path]:
    """This process's cgroup (v2) folder, if there is one we can see."""
    mount = cgroup2_mount(mountinfo)
    if mount is None:
        return None
    for line in (_read("/proc/self/cgroup") if cgroup is None else cgroup).splitlines():
        if line.startswith("0::"):
            return mount / line[3:].lstrip("/")
    return None


def own_unit(name: str, app_id: str = APP_ID) -> bool:
    """Whether a cgroup's name is a unit a launcher made for this app.

    The app id must stand whole, so io.github.Officialbabs.Natter doesn't claim the unit of
    io.github.Officialbabs.Natter.LabW1 or of an app whose id merely starts the same way.
    """
    name = name.replace("\\x2d", "-")  # systemd escapes "-" inside the app id
    if not name.startswith("app-"):
        return False
    for suffix in (".scope", ".service"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return bool(re.search(rf"-{re.escape(app_id)}(?:[-@]|$)", stem))
    return False


def in_own_scope(cgroup: Optional[Path] = None) -> bool:
    """Whether Natter already has a unit of its own (its launcher made one, or we did)."""
    cgroup = cgroup or own_cgroup()
    return bool(cgroup and own_unit(cgroup.name))


def scope_name(pid: int) -> str:
    """The XDG name for an app's scope: app-<app id>-<pid>.scope, a "-" in the id escaped."""
    app_id = APP_ID.replace("-", "\\x2d")
    return f"app-{app_id}-{pid}.scope"


# The kernel's backstop for a page that grows without end, reached only when Natter's guard
# (memguard.py) couldn't act: past MemoryMax, with its swap capped by MemorySwapMax, the kernel
# ends the biggest process in the unit, a page (the guard ranks pages first, oom_score_adj).
# What follows is the unit's OOMPolicy: "continue" leaves Natter running to start a fresh page;
# systemd's default, "stop", stops all of Natter. No MemoryHigh: it never kills, and on 20 Oct
# 2026 it held a runaway page at 6 GB while pushing gigabytes of it into swap, and stalled
# Natter's window with it. memguard.scope_limits sizes both from the computer's memory.
_made_scope: Optional[str] = None  # the scope move_to_own_scope made
_launcher_limits: Optional[tuple[Optional[int], Optional[int]]] = None  # (MemoryMax, MemorySwapMax) a launcher set
_told_oom_policy = False


def _cgroup_limit(cgroup: Path, name: str) -> Optional[int]:
    """A cgroup's memory.max or memory.swap.max in bytes; None when there's none ("max")."""
    value = _read(cgroup / name).strip()
    return int(value) if value.isdigit() else None


def _unit_property(bus: Gio.DBusConnection, unit: str, name: str) -> Optional[str]:
    """A string property of a systemd unit (its scope or service interface), None on any trouble."""
    interface = "org.freedesktop.systemd1." + ("Service" if unit.endswith(".service") else "Scope")
    try:
        path = bus.call_sync(SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "GetUnit",
                             GLib.Variant("(s)", (unit,)), GLib.VariantType("(o)"),
                             Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        value = bus.call_sync(SYSTEMD, path, "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (interface, name)), GLib.VariantType("(v)"),
                              Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
    except GLib.Error:
        return None
    return value if isinstance(value, str) else None


def limit_own_scope(accounts: int = 1) -> bool:
    """Set the backstop's MemoryMax and MemorySwapMax, for `accounts` pages, on the unit Natter
    runs in: one a launcher made (Ada's does), or the one move_to_own_scope made. Call it again
    whenever an account is added or removed. True when they were set.

    A launcher's own lower limits stay. Its OOMPolicy can't be changed after the unit starts
    (systemd refuses it), so if it's "stop" that is logged, once: there the kernel's backstop
    stops all of Natter, and only the guard ends a runaway page alone."""
    global _launcher_limits, _told_oom_policy
    cgroup = own_cgroup()
    if in_sandbox() or not in_own_scope(cgroup):
        return False
    memory_max, swap_max = scope_limits(mem_total(), accounts)
    launcher = cgroup.name != _made_scope
    if launcher:
        if _launcher_limits is None:  # read before Natter set anything of its own
            _launcher_limits = (_cgroup_limit(cgroup, "memory.max"), _cgroup_limit(cgroup, "memory.swap.max"))
        memory_max = min(memory_max, _launcher_limits[0] or memory_max)
        swap_max = min(swap_max, swap_max if _launcher_limits[1] is None else _launcher_limits[1])
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call_sync(SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                      "SetUnitProperties",
                      GLib.Variant("(sba(sv))", (cgroup.name, True, [("MemoryMax", GLib.Variant("t", memory_max)),
                                                                     ("MemorySwapMax", GLib.Variant("t", swap_max))])),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as error:
        print(f"natter: memory: couldn't limit {cgroup.name}: {error.message}", flush=True)
        return False
    if launcher and not _told_oom_policy:
        _told_oom_policy = True
        policy = _unit_property(bus, cgroup.name, "OOMPolicy")
        if policy != "continue":
            print(f"natter: memory: past {memory_max >> 20} MB, the kernel ends a page and systemd stops all of "
                  f"{NAME} ({cgroup.name} has OOMPolicy={policy or 'unknown'}, which only its launcher can "
                  "change); the guard ends a runaway page alone, before that", flush=True)
    return True


def move_to_own_scope(timeout: float = 0.5, accounts: int = 1) -> Optional[str]:
    """Put Natter in a transient scope of its own, before it starts any WebKit process, with the
    backstop's limits for `accounts` pages (limit_own_scope changes them later) and
    OOMPolicy=continue, which can only be set now.

    Returns the scope's name, or None when Natter already has one, runs in a sandbox (whose
    launcher made the scope), or there's no systemd user manager to ask.
    """
    global _made_scope
    if in_sandbox() or in_own_scope():
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    except GLib.Error:
        return None
    if not _has_owner(bus, SYSTEMD):
        return None
    pid = os.getpid()
    name = scope_name(pid)
    memory_max, swap_max = scope_limits(mem_total(), accounts)
    properties = [
        ("PIDs", GLib.Variant("au", [pid])),
        ("Description", GLib.Variant("s", NAME)),
        ("Slice", GLib.Variant("s", "app.slice")),
        ("CollectMode", GLib.Variant("s", "inactive-or-failed")),
        ("MemoryMax", GLib.Variant("t", memory_max)),
        ("MemorySwapMax", GLib.Variant("t", swap_max)),
        # The kernel ending a page past MemoryMax leaves Natter running: it starts a fresh page.
        ("OOMPolicy", GLib.Variant("s", "continue")),
    ]
    try:
        try:
            _start_unit(bus, name, properties)
        except GLib.Error as error:
            if "OOMPolicy" not in error.message:
                raise
            # A systemd that can't set OOMPolicy on a scope refuses the whole unit: the rest still helps.
            print(f"natter: memory: systemd won't set OOMPolicy on {name} ({error.message}); past its "
                  f"limit, it stops all of {NAME}", flush=True)
            _start_unit(bus, name, [prop for prop in properties if prop[0] != "OOMPolicy"])
    except GLib.Error as error:
        print(f"natter: couldn't get a scope of its own: {error.message}")
        return None
    _made_scope = name
    # systemd moves the process once the job runs; wait so WebKit's processes follow it.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if in_own_scope():
            return name
        time.sleep(0.02)
    return name


def _start_unit(bus: Gio.DBusConnection, name: str, properties: list) -> None:
    bus.call_sync(SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                  "StartTransientUnit",
                  GLib.Variant("(ssa(sv)a(sa(sv)))", (name, "fail", properties, [])),
                  GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 3000, None)


# ---- Memory ---------------------------------------------------------------------------------

def cheap_swap(swaps: str, zswap: str) -> bool:
    """Whether swapping costs little: to zram, or through zswap (compressed in memory first).

    `swaps` is /proc/swaps, `zswap` is /sys/module/zswap/parameters/enabled. A plain swap
    file or partition doesn't count: pages pushed there come back slowly when WhatsApp
    needs them, and the disk works for it.
    """
    devices = [line.split()[0] for line in swaps.splitlines()[1:] if line.split()]
    if any(device.startswith("/dev/zram") for device in devices):
        return True
    return bool(devices) and zswap.strip() in ("Y", "y", "1")


def can_reclaim(cgroup: Optional[Path], swaps: str, zswap: str) -> bool:
    """Whether giving memory back from `cgroup` is possible and worth it."""
    if not (cgroup and in_own_scope(cgroup) and os.access(cgroup / "memory.reclaim", os.W_OK)):
        return False
    return _read(cgroup / "memory.swap.max").strip() != "0" and cheap_swap(swaps, zswap)


def footprint(cgroup: Optional[Path] = None) -> int:
    """Bytes Natter's unit really holds: its private memory (anon) plus what's been swapped
    out, not the file cache the kernel can drop whenever it likes. 0 when it can't be read."""
    cgroup = cgroup or own_cgroup()
    if cgroup is None:
        return 0
    anon = 0
    for line in _read(cgroup / "memory.stat").splitlines():
        if line.startswith("anon "):
            anon = int(line.split()[1])
            break
    try:
        swapped = int(_read(cgroup / "memory.swap.current") or 0)
    except ValueError:
        swapped = 0
    return anon + swapped


def memory_events(cgroup: Optional[Path] = None) -> dict[str, int]:
    """The kernel's counts for Natter's unit (memory.events), by name: `high`, times it held the
    unit back past a MemoryHigh (a launcher's: Natter sets none); `max`, times it reached its
    MemoryMax; `oom_kill`, processes it ended there. Empty when they can't be read."""
    cgroup = cgroup or own_cgroup()
    events = {}
    for line in (_read(cgroup / "memory.events") if cgroup else "").splitlines():
        name, _, value = line.partition(" ")
        if value.strip().isdigit():
            events[name] = int(value)
    return events


def memory_tight(meminfo: Optional[str] = None) -> bool:
    """Whether the whole computer is short of memory: under a tenth of it available, and swap
    (if any) all but full. Ada's lifecycled asks apps to close then; Natter, hidden, has no
    window to close, so it answers by giving back what it can (a fresh page)."""
    values = {}
    for line in (meminfo if meminfo is not None else _read("/proc/meminfo")).splitlines():
        name, _, rest = line.partition(":")
        if rest.split():
            values[name] = int(rest.split()[0])
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
    swap, swap_free = values.get("SwapTotal", 0), values.get("SwapFree", 0)
    if not total:
        return False
    return available < total / 10 and (swap == 0 or swap_free < swap * 0.15)


# Swap is for what's cold and will be wanted again. WhatsApp's leaked heap never is: parked in
# swap it stays there, and one day it filled the computer's 15 GB (Ada's hibernation room
# included) until Ada said memory was full. Past either of these, Natter stops moving memory to
# swap, and a hidden Natter takes a fresh page instead, the one thing that empties it (app.py).
SWAP_BUDGET_MB = 1024  # of Natter's own memory in swap
SWAP_SHARE = 0.6  # of the computer's swap in use


def own_swap(cgroup: Optional[Path] = None) -> int:
    """Bytes of Natter's unit in swap (0 when it can't be read)."""
    cgroup = cgroup or own_cgroup()
    try:
        return int(_read(cgroup / "memory.swap.current") or 0) if cgroup else 0
    except ValueError:
        return 0


def swap_share(meminfo: Optional[str] = None) -> float:
    """How much of the computer's swap is in use, from 0 to 1 (0 with no swap)."""
    values = {}
    for line in (meminfo if meminfo is not None else _read("/proc/meminfo")).splitlines():
        name, _, rest = line.partition(":")
        if name in ("SwapTotal", "SwapFree") and rest.split():
            values[name] = int(rest.split()[0])
    total = values.get("SwapTotal", 0)
    return (total - values.get("SwapFree", total)) / total if total else 0.0


def swap_full(cgroup: Optional[Path] = None, meminfo: Optional[str] = None) -> bool:
    """Whether swap has taken all it should: Natter's share past its budget, or the computer's
    swap past SWAP_SHARE in use."""
    return own_swap(cgroup) > SWAP_BUDGET_MB * 1024 * 1024 or swap_share(meminfo) > SWAP_SHARE


class Reclaimer:
    """Gives Natter's cold memory back while its window is hidden, the way Ada does for paused
    apps, except Natter keeps running so messages and notifications still arrive.

    Pages WhatsApp hasn't touched in a while go to (compressed) swap in small steps; anything
    it needs again comes straight back. It stops at `floor` MB, when swap has taken all it
    should (swap_full), or when the kernel can't find more to take.
    """

    STEP = "64M"
    # Down to 300 MB (WhatsApp's own heap, swapped out), coming back to the window stuttered
    # while it was pulled back in: typing, scrolling, until it all was. Kept above what
    # WhatsApp works with, and file cache and anon taken alike, only what's really cold goes.
    FLOOR_MB = 800
    SWAPPINESS = 100

    def __init__(self, floor_mb: int = FLOOR_MB, on_done: Optional[Callable[[int, int], None]] = None) -> None:
        self.cgroup: Optional[Path] = None  # the unit the current run gives memory back from
        self.floor = floor_mb * 1024 * 1024
        self.on_done = on_done
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _reclaimable(self) -> Optional[Path]:
        """Natter's cgroup, when memory can be given back from it. Looked up every time: the move
        to Natter's own scope can finish after the Reclaimer is made."""
        cgroup = own_cgroup()
        swaps, zswap = _read("/proc/swaps"), _read("/sys/module/zswap/parameters/enabled")
        return cgroup if can_reclaim(cgroup, swaps, zswap) else None

    @property
    def available(self) -> bool:
        return self._reclaimable() is not None

    def usage(self) -> int:
        try:
            return int((self.cgroup / "memory.current").read_text())
        except (OSError, ValueError, TypeError):
            return 0

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self.cgroup = self._reclaimable()
        if self.cgroup is None:
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
            for command in (f"{self.STEP} swappiness={self.SWAPPINESS}", self.STEP):
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
        while not self._stop.is_set() and self.usage() > self.floor and not swap_full(self.cgroup):
            try:
                if not self._step():
                    break
            except OSError:
                break
            self._stop.wait(0.05)
        after = self.usage()
        if self.on_done:
            GLib.idle_add(self.on_done, before, after)


# ---- Background portal ----------------------------------------------------------------------

BACKGROUND = "org.freedesktop.portal.Background"


class CallSound:
    """A silent sound stream of Natter's own, while a call is on.

    The call's real sound comes from WebKit's sandboxed web process, which the desktop can't tell
    is Natter's. Desktops that pause hidden apps unless they play or record sound (Ada's
    lifecycled does) see this one, from Natter's own process, and leave the call alone.
    """

    def __init__(self) -> None:
        self._pipeline = None

    def start(self) -> None:
        if self._pipeline is not None:
            return
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError):
            return
        if not Gst.is_initialized():
            Gst.init(None)
        for sink in ("pulsesink", "pipewiresink"):
            if not Gst.ElementFactory.find(sink):
                continue
            try:
                # Silence in 100 ms buffers: ten wake-ups a second, next to nothing in CPU.
                pipeline = Gst.parse_launch(
                    "audiotestsrc wave=silence is-live=true samplesperbuffer=4800 ! "
                    f"audio/x-raw,rate=48000,channels=1 ! audioconvert ! {sink} name=sink")
            except GLib.Error:
                continue
            if sink == "pulsesink":
                props = Gst.Structure.new_from_string(
                    f'props, media.role=phone, application.name="{NAME}", application.id="{APP_ID}"')
                props = props[0] if isinstance(props, tuple) else props
                pipeline.get_by_name("sink").set_property("stream-properties", props)
            if pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE:
                self._pipeline, self._null = pipeline, Gst.State.NULL
                return
            pipeline.set_state(Gst.State.NULL)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(self._null)
            self._pipeline = None


class AudioFollower:
    """Keeps the page's sound on the computer's current output.

    WebKit's sound streams can be tied to the device that was the default when they opened, and
    WhatsApp keeps its audio context open for good: Bluetooth headphones connected later got
    nothing, voice notes and calls stayed on the laptop's speaker. When the default changes (or
    a stream opens), Natter tells PipeWire its page's streams follow the default (target.object
    -1). Not a move to a device: WirePlumber would remember that for every WebKit app's sound.
    Through pactl and pw-metadata; where they're missing, nothing changes.
    """

    def __init__(self, app_name: str = NAME, app_id: str = APP_ID,
                 on_change: Optional[Callable[[], None]] = None) -> None:
        self.app_name, self.app_id = app_name, app_id
        self.on_change = on_change  # the devices or the defaults changed (main loop)
        self.pactl = shutil.which("pactl")
        self.metadata = shutil.which("pw-metadata")
        self._process: Optional[Gio.Subprocess] = None
        self._pending = 0
        self._restarts = 0
        self._busy = threading.Lock()
        self._change_pending = 0

    def start(self) -> bool:
        if not self.pactl or not self.metadata or self._process is not None:
            return False
        try:
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
            self._process = launcher.spawnv([self.pactl, "subscribe"])
        except GLib.Error:
            return False
        self._read(Gio.DataInputStream.new(self._process.get_stdout_pipe()))
        self._schedule()
        return True

    def stop(self) -> None:
        if self._process is not None:
            self._process.force_exit()
            self._process = None
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0

    def _read(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._line)

    def _line(self, stream: Gio.DataInputStream, result: Gio.AsyncResult) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:  # pactl ended (PipeWire restarted, say): again, a few times
            self._process = None
            if self._restarts < 5:
                self._restarts += 1
                GLib.timeout_add_seconds(5, lambda: self.start() and False)
            return
        # The default device changed ("on server"), a stream opened, or a device came or went.
        if "on server" in line or "'new' on sink-input" in line:
            self._schedule()
        if self.on_change and ("on server" in line or " on sink #" in line or " on source #" in line) \
                and ("'new'" in line or "'remove'" in line or "on server" in line):
            self._changed()
        self._read(stream)

    def _changed(self) -> None:
        if not self._change_pending:
            def fire() -> bool:
                self._change_pending = 0
                self.on_change()
                return GLib.SOURCE_REMOVE
            self._change_pending = GLib.timeout_add(500, fire)

    def _schedule(self) -> None:
        if not self._pending:
            self._pending = GLib.timeout_add(300, self._follow)

    def _follow(self) -> bool:
        self._pending = 0
        threading.Thread(target=self.follow, name="natter-audio", daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _pactl(self, *args: str) -> str:
        try:
            return subprocess.run([self.pactl, *args], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def follow(self) -> int:
        """Move the page's streams that aren't on the default output there. Returns how many."""
        if not self._busy.acquire(blocking=False):
            return 0
        try:
            default = self._pactl("get-default-sink").strip()
            sinks = json.loads(self._pactl("-f", "json", "list", "sinks", "short") or "[]")
            streams = json.loads(self._pactl("-f", "json", "list", "sink-inputs") or "[]")
            target = next((sink["index"] for sink in sinks if sink.get("name") == default), None)
            if target is None:
                return 0
            moved = 0
            for stream in page_streams(streams, self.app_name, self.app_id):
                node = (stream.get("properties") or {}).get("object.id")
                if stream.get("sink") != target and node:
                    subprocess.run([self.metadata, "--", str(node), "target.object", "-1"],
                                   capture_output=True, timeout=5)
                    moved += 1
            return moved
        except (ValueError, TypeError, KeyError):
            return 0
        finally:
            self._busy.release()


def page_streams(streams: list, app_name: str = NAME, app_id: str = APP_ID) -> list:
    """Natter's page's sound streams among `pactl -f json list sink-inputs`: WebKit's sandboxed
    web process, carrying Natter's app id (or name). Natter's own silent call stream runs in
    python3, not here."""
    found = []
    for stream in streams:
        props = stream.get("properties") or {}
        if props.get("application.process.binary") == "WebKitWebProcess" and (
                props.get("pipewire.access.portal.app_id") == app_id or props.get("application.name") == app_name):
            found.append(stream)
    return found


def has_background_portal(bus: Gio.DBusConnection) -> bool:
    """Whether the desktop's portal offers Background. xdg-desktop-portal runs on most desktops,
    but offers Background only when one of the desktop's backends implements it: reading the
    interface's version tells."""
    try:
        bus.call_sync(PORTAL, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (BACKGROUND, "version")), GLib.VariantType("(v)"),
                      Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        return False
    return True


def request_background(parent_window_handle: str, autostart: bool, reason: str,
                       done: Callable[[bool], None], bus: Optional[Gio.DBusConnection] = None) -> bool:
    """Ask the desktop, through the Background portal, to let Natter run in the background
    and (with `autostart`) start hidden at login. The portal may ask the user first.

    `done(allowed)` is called once the desktop answers. Returns False straight away, without
    calling `done`, when there's no portal to ask.
    """
    try:
        bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION)
    except GLib.Error:
        return False
    if not has_background_portal(bus):
        return False
    token = "natter_" + uuid.uuid4().hex
    sender = bus.get_unique_name().lstrip(":").replace(".", "_")
    subscription = 0

    def listen(request: str) -> None:
        nonlocal subscription
        if subscription:
            bus.signal_unsubscribe(subscription)
        subscription = bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", request,
                                            None, Gio.DBusSignalFlags.NONE, on_response)

    def finish(allowed: bool) -> None:
        nonlocal subscription
        if subscription:
            bus.signal_unsubscribe(subscription)
            subscription = 0
            done(allowed)

    def on_response(_bus, _sender, _path, _iface, _signal, params: GLib.Variant) -> None:
        response, results = params.unpack()
        finish(response == 0 and bool(results.get("background")))

    def on_called(source, result) -> None:
        try:
            request = source.call_finish(result).unpack()[0]
        except GLib.Error as error:
            print(f"natter: the Background portal didn't answer: {error.message}")
            finish(False)
            return
        if subscription and request != expected:
            listen(request)  # a portal too old to use handle_token answers somewhere else

    # Listen before asking, so a quick answer isn't missed.
    expected = f"{PORTAL_PATH}/request/{sender}/{token}"
    listen(expected)
    options = {
        "handle_token": GLib.Variant("s", token),
        "reason": GLib.Variant("s", reason),
        "autostart": GLib.Variant("b", autostart),
        "commandline": GLib.Variant("as", ["natter", "--background"]),
        "dbus-activatable": GLib.Variant("b", False),
    }
    bus.call(PORTAL, PORTAL_PATH, BACKGROUND, "RequestBackground",
             GLib.Variant("(sa{sv})", (parent_window_handle, options)), GLib.VariantType("(o)"),
             Gio.DBusCallFlags.NONE, -1, None, on_called)
    return True


# ---- Media ----------------------------------------------------------------------------------

# What WhatsApp's media needs GStreamer to decode: videos are H.264 with AAC sound, voice
# messages are Opus.
CODECS = (
    ("H.264 video", "video/x-h264"),
    ("AAC audio", "audio/mpeg, mpegversion=(int)4"),
    ("Opus audio", "audio/x-opus"),
)


def media_report(release: Optional[dict[str, str]] = None) -> tuple[list[str], str]:
    """The codecs WhatsApp's videos and voice messages need that GStreamer lacks here, and the
    command that installs them on this distribution ("" when nothing's missing or it's unknown).

    Without GStreamer for Python the check can't run (WebKit plays media without it): then
    nothing is listed, and the hint says so and names the package the check needs. The first
    call can take a moment: GStreamer looks through its plugins. Never raises.
    """
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        unchecked = "Couldn't check for codecs: GStreamer for Python isn't installed"
        command = _install_command(GST_PYTHON, release)
        return [], f"{unchecked}. Install it with: {command}" if command else f"{unchecked} (the Gst 1.0 typelib)."
    try:
        Gst.init(None)
        decoders = Gst.ElementFactory.list_get_elements(Gst.ELEMENT_FACTORY_TYPE_DECODER, Gst.Rank.MARGINAL)
        missing = [label for label, caps in CODECS
                   if not any(factory.can_sink_any_caps(Gst.Caps.from_string(caps)) for factory in decoders)]
    except Exception as error:
        print(f"natter: couldn't ask GStreamer about codecs: {error}")
        return [], ""
    return missing, _install_command(MEDIA, release) if missing else ""


def _install_command(packages: dict[str, str], release: Optional[dict[str, str]]) -> str:
    """install_hint, but "" in a sandbox, whose runtime brings its own packages, and on any trouble."""
    if in_sandbox():
        return ""
    try:
        return install_hint(packages, release)
    except Exception:
        return ""
