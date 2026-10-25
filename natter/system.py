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
from .memguard import mem_total, scope_limits, swap_total

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
# systemd's default, "stop", stops all of Natter, and so does any other kernel OOM kill in the
# unit (memory.events counts them all). So the backstop is set only on a unit whose OOMPolicy is
# "continue": the scope Natter makes for itself. A launcher's unit (KDE's app-…@….service, its
# autostart, GNOME's and Ada's scopes) has "stop", which only the launcher can change, and gets
# no limit: there the guard alone ends a page that grows without end. No MemoryHigh: it never
# kills, and on 20 Oct 2026 it held a runaway page at 6 GB while pushing gigabytes of it into
# swap, and stalled Natter's window with it. memguard.scope_limits sizes both from the
# computer's memory.
_made_scope: Optional[str] = None  # the scope move_to_own_scope made
_made_without_policy = False  # systemd refused OOMPolicy=continue on it
_unit_policy: Optional[tuple[str, Optional[str]]] = None  # (a launcher's unit, its OOMPolicy), once asked
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


def unit_oom_policy(unit: str, bus: Gio.DBusConnection) -> Optional[str]:
    """What systemd does to Natter's unit `unit` when the kernel ends a process in it for memory:
    "continue" (that process only) or "stop" (all of Natter); None when systemd can't say. The
    scope Natter made has what it was made with; a launcher's is asked once, as it can't change."""
    global _unit_policy
    if unit == _made_scope:
        return "stop" if _made_without_policy else "continue"
    if _unit_policy is None or _unit_policy[0] != unit:
        _unit_policy = (unit, _unit_property(bus, unit, "OOMPolicy"))
    return _unit_policy[1]


def pages_end_alone() -> bool:
    """Whether the kernel, ending one of Natter's pages for memory, leaves the rest of Natter
    running: only in a unit of Natter's own whose OOMPolicy is "continue". Elsewhere the guard
    doesn't make its pages the kernel's first choice (memguard's oom_score_adj): there, the
    kernel's choosing one would stop all of Natter."""
    cgroup = own_cgroup()
    if in_sandbox() or not in_own_scope(cgroup):
        return False
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    except GLib.Error:
        return False
    return unit_oom_policy(cgroup.name, bus) == "continue"


def limit_own_scope(accounts: int = 1) -> bool:
    """Set the backstop's MemoryMax and MemorySwapMax, for `accounts` pages, on the unit Natter
    runs in, if its OOMPolicy is "continue": the one move_to_own_scope made, or a launcher's that
    has it. Call it again whenever an account is added or removed. True when they were set.

    A launcher's own lower limits stay. A unit with OOMPolicy "stop" (every launcher's unit
    seen so far: KDE's, GNOME's, Ada's; it can't be changed once the unit runs) gets no limit:
    the kernel ending one page there would stop all of Natter, the very thing the backstop is
    meant to spare. There the guard alone ends a page that grows without end. Said once."""
    global _launcher_limits, _told_oom_policy
    cgroup = own_cgroup()
    if in_sandbox() or not in_own_scope(cgroup):
        return False
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    except GLib.Error as error:
        print(f"natter: memory: couldn't limit {cgroup.name}: {error.message}", flush=True)
        return False
    launcher = cgroup.name != _made_scope
    policy = unit_oom_policy(cgroup.name, bus)
    if policy != "continue":
        if not _told_oom_policy:
            _told_oom_policy = True
            whose = "its launcher's, which only the launcher can change" if launcher else "systemd's default"
            print(f"natter: memory: {cgroup.name} has OOMPolicy={policy or 'unknown'} ({whose}): the kernel ending "
                  f"one page there would stop all of {NAME}, so {NAME} sets no memory limit on it; the guard "
                  "ends a page that grows without end, alone", flush=True)
        return False
    memory_max, swap_max = scope_limits(mem_total(), accounts, swap_total())
    if launcher:
        if _launcher_limits is None:  # read before Natter set anything of its own
            _launcher_limits = (_cgroup_limit(cgroup, "memory.max"), _cgroup_limit(cgroup, "memory.swap.max"))
        memory_max = min(memory_max, _launcher_limits[0] or memory_max)
        swap_max = min(swap_max, swap_max if _launcher_limits[1] is None else _launcher_limits[1])
    try:
        bus.call_sync(SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                      "SetUnitProperties",
                      GLib.Variant("(sba(sv))", (cgroup.name, True, [("MemoryMax", GLib.Variant("t", memory_max)),
                                                                     ("MemorySwapMax", GLib.Variant("t", swap_max))])),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as error:
        print(f"natter: memory: couldn't limit {cgroup.name}: {error.message}", flush=True)
        return False
    return True


def move_to_own_scope(timeout: float = 0.5, accounts: int = 1) -> Optional[str]:
    """Put Natter in a transient scope of its own, before it starts any WebKit process, with the
    backstop's limits for `accounts` pages (limit_own_scope changes them later) and
    OOMPolicy=continue, which can only be set now.

    Returns the scope's name, or None when Natter already has one, runs in a sandbox (whose
    launcher made the scope), or there's no systemd user manager to ask. A systemd that won't
    set OOMPolicy on a scope gets one without it, and then without the limits too (see
    limit_own_scope: under "stop", the kernel ending a page stops all of Natter).
    """
    global _made_scope, _made_without_policy, _told_oom_policy
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
    memory_max, swap_max = scope_limits(mem_total(), accounts, swap_total())
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
    without_policy = False
    try:
        try:
            _start_unit(bus, name, properties)
        except GLib.Error as error:
            if "OOMPolicy" not in error.message:
                raise
            # A systemd that can't set OOMPolicy on a scope refuses the whole unit. The scope
            # still groups Natter's processes, but with systemd's default policy, "stop": no limits.
            print(f"natter: memory: systemd won't set OOMPolicy on {name} ({error.message}): the kernel ending "
                  f"one page there would stop all of {NAME}, so {NAME} sets no memory limit on it; the guard "
                  "ends a page that grows without end, alone", flush=True)
            _start_unit(bus, name, [prop for prop in properties
                                    if prop[0] not in ("OOMPolicy", "MemoryMax", "MemorySwapMax")])
            without_policy = _told_oom_policy = True
    except GLib.Error as error:
        print(f"natter: couldn't get a scope of its own: {error.message}")
        return None
    _made_scope, _made_without_policy = name, without_policy
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


def memory_stat(cgroup: Optional[Path], *names: str) -> dict[str, int]:
    """The named counts of a unit's memory.stat, in bytes, each 0 when it isn't there."""
    found = dict.fromkeys(names, 0)
    for line in (_read(cgroup / "memory.stat") if cgroup else "").splitlines():
        name, _, value = line.partition(" ")
        if name in found and value.strip().isdigit():
            found[name] = int(value)
    return found


def unit_charge(cgroup: Optional[Path] = None) -> dict[str, int]:
    """What the kernel charges Natter's unit beside its pages' private memory, in bytes: `shmem`,
    shared memory, which is where WebKit's GPU buffers live on Intel (the i915 driver backs them
    with shmem: 542 MB of a page's on 24 Oct 2026), and `zswap`, the pool its compressed swap
    takes. No process's RssAnon or VmSwap has either, so no limit of WebKit's or the guard's
    counts them, and nothing here does: they're reported, no more. Zeros when unreadable."""
    return memory_stat(cgroup or own_cgroup(), "shmem", "zswap")


def footprint(cgroup: Optional[Path] = None) -> int:
    """Bytes Natter's unit really holds: its private memory (anon) plus what's been swapped
    out, and its shared memory (unit_charge's shmem, the GPU's buffers), not the file cache the
    kernel can drop whenever it likes. Not the zswap pool: what's in it is swapped already, and
    counted so (memory.swap.current); the step lines report the pool on its own. For the log
    only. 0 when it can't be read."""
    cgroup = cgroup or own_cgroup()
    if cgroup is None:
        return 0
    anon = memory_stat(cgroup, "anon")["anon"]
    try:
        swapped = int(_read(cgroup / "memory.swap.current") or 0)
    except ValueError:
        swapped = 0
    return anon + swapped + unit_charge(cgroup)["shmem"]


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
    (if any) past SWAP_SHARE in use, the Reclaimer's own line. Some desktops ask apps to close
    then; Natter, hidden, has no window to close, so it answers by giving back what it can (a
    fresh page)."""
    values = {}
    for line in (meminfo if meminfo is not None else _read("/proc/meminfo")).splitlines():
        name, _, rest = line.partition(":")
        if rest.split():
            values[name] = int(rest.split()[0])
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
    swap = values.get("SwapTotal", 0)
    if not total:
        return False
    return available < total / 10 and (swap == 0 or swap_share(meminfo) > SWAP_SHARE)


# Swap is for what's cold and will be wanted again. WhatsApp's leaked heap never is: parked in
# swap it stays there, and one day it filled the computer's 15 GB (hibernation room included)
# until the desktop said memory was full. Past either of these, the Reclaimer stops moving
# Natter's memory to swap. They are the Reclaimer's own limits and nothing else's: what it put
# in swap is never a reason for a fresh page (app.py), or it would reload WhatsApp each time it
# had done its job (as it did, every half hour, until October 2026).
SWAP_BUDGET_MB = 1024  # of Natter's own memory in swap
SWAP_SHARE = 0.6  # of the computer's swap in use
# The Reclaimer doesn't add to a computer already waiting on memory, where the kernel reclaims
# anyway: past this much of the last 10 s with some task waiting on memory (PSI "some avg10",
# in %). systemd-oomd ends whole units under pressure, picking the one reclaiming the most.
RECLAIM_PRESSURE = 20.0


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


def memory_stalls(pressure: Optional[str] = None) -> float:
    """The share of the last 10 s some task on the computer waited on memory, in % (PSI's
    "some avg10" in /proc/pressure/memory; `pressure` is that file's text). 0 when unknown."""
    text = _read("/proc/pressure/memory") if pressure is None else pressure
    found = re.search(r"^some\b.*?\bavg10=([0-9.]+)", text, re.M)
    try:
        return float(found[1]) if found else 0.0
    except ValueError:
        return 0.0


class Reclaimer:
    """Gives Natter's cold memory back while its window is hidden, the way a desktop that pauses
    hidden apps may do for them, except Natter keeps running so messages and notifications still
    arrive.

    Pages WhatsApp hasn't touched in a while go to (compressed) swap in small steps; anything
    it needs again comes straight back. It stops at `floor` MB of the unit's private memory
    (memory.stat anon: the floor is about WhatsApp's own heap, not the file cache or the GPU's
    shmem beside it), when swap has taken all it should (swap_full), when the computer is
    already waiting on memory (RECLAIM_PRESSURE), or when the kernel can't find more to take.
    Where swap is full already it doesn't start, and `available` says so (why_not): on 24 Oct
    2026 the unit's swap sat full all morning, and the switch promised what never ran.
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
    def possible(self) -> bool:
        """Whether giving memory back is possible here at all: a unit of Natter's own that may
        swap, and swap that's cheap (can_reclaim)."""
        return self._reclaimable() is not None

    def why_not(self) -> str:
        """Why giving memory back wouldn't do anything now, in words; "" when it would."""
        cgroup = self._reclaimable()
        if cgroup is None:
            return "Giving memory back isn't possible here"
        if swap_full(cgroup):
            return "Swap is full already, so there's nowhere for it to go"
        return ""

    @property
    def available(self) -> bool:
        """Whether a run now would give memory back: possible, and swap not full already."""
        return not self.why_not()

    def usage(self) -> int:
        """Bytes of private memory the unit holds (memory.stat anon), the floor's measure."""
        return memory_stat(self.cgroup, "anon")["anon"] if self.cgroup is not None else 0

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self.cgroup = self._reclaimable()
        if self.cgroup is None or swap_full(self.cgroup):
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
        while (not self._stop.is_set() and self.usage() > self.floor and not swap_full(self.cgroup)
               and memory_stalls() < RECLAIM_PRESSURE):
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
    is Natter's. Desktops that pause hidden apps unless they play or record sound see this one,
    from Natter's own process, and leave the call alone.
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


class LoopMeter:
    """How late Natter's main loop runs during a call: a TICK ms timer, and how late each tick
    comes. Every packet of the call goes through the main loop (rtc.py), so a stall there is a
    gap in the call's sound; at the call's end one line tells how it went (line)."""

    TICK = 20  # ms

    def __init__(self, clock: Callable[[], float] = time.monotonic, later: Optional[Callable] = None,
                 cancel: Optional[Callable[[int], object]] = None) -> None:
        self._clock = clock
        self._later = later or GLib.timeout_add
        self._cancel = cancel or GLib.source_remove
        self._timer = 0
        self._due = 0.0
        self.latest, self.over_50, self.over_100 = 0.0, 0, 0  # ms, and how many ticks were that late

    @property
    def running(self) -> bool:
        return bool(self._timer)

    def start(self) -> None:
        if self._timer:
            return
        self.latest, self.over_50, self.over_100 = 0.0, 0, 0
        self._due = self._clock() + self.TICK / 1000
        self._timer = self._later(self.TICK, self._tick)

    def _tick(self) -> bool:
        now = self._clock()
        late = max(0.0, (now - self._due) * 1000)
        self.latest = max(self.latest, late)
        self.over_50 += late > 50
        self.over_100 += late > 100
        self._due = now + self.TICK / 1000  # GLib plans the next from now too
        return GLib.SOURCE_CONTINUE

    def stop(self) -> None:
        if self._timer:
            self._cancel(self._timer)
            self._timer = 0

    def line(self, packets: int) -> str:
        """The call's line for the log: only counts, no names or ids."""
        return (f"natter: call: main loop late max {self.latest:.0f} ms, over 50 ms {self.over_50} times, "
                f"over 100 ms {self.over_100} times, packets {packets}")


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
# And what plays them, beyond the decoders: gst-libav alone has those, but not these, which come
# with GStreamer's good and base plugins (Arch's gst-libav pulls in neither).
PLAYERS = (
    ("MP4 videos (qtdemux)", ("qtdemux",)),
    ("Ogg voice messages (oggdemux)", ("oggdemux",)),
    ("playback (playbin)", ("playbin",)),
    ("sound (autoaudiosink, pulsesink)", ("autoaudiosink", "pulsesink")),
)
_stub: Optional[bool] = None


def openh264_stub() -> bool:
    """Whether GStreamer's OpenH264 plugin runs on a stand-in library that decodes nothing
    (Fedora's noopenh264, openSUSE's libopenh264 "~noopenh264"): the plugin loads and offers
    openh264dec, but no H.264 video plays. Loads the plugin and asks the library for a decoder,
    as openh264dec would; once a process. Never raises."""
    global _stub
    if _stub is None:
        _stub = False
        try:
            import ctypes

            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
            factory = Gst.ElementFactory.find("openh264dec")
            if factory is not None and factory.load() is not None:
                with open("/proc/self/maps") as maps:
                    paths = sorted({line.split(None, 5)[5].strip() for line in maps
                                    if "/libopenh264" in line and len(line.split(None, 5)) == 6})
                if paths:
                    library = ctypes.CDLL(paths[0])
                    library.WelsCreateDecoder.restype = ctypes.c_long
                    library.WelsCreateDecoder.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
                    library.WelsDestroyDecoder.argtypes = [ctypes.c_void_p]
                    decoder = ctypes.c_void_p()
                    if library.WelsCreateDecoder(ctypes.byref(decoder)) == 0 and decoder.value:
                        library.WelsDestroyDecoder(decoder)
                    else:
                        _stub = True
        except Exception:
            pass
    return _stub


def media_report(release: Optional[dict[str, str]] = None) -> tuple[list[str], str]:
    """What WhatsApp's videos and voice messages need that GStreamer lacks here (codecs, and
    what plays them), and the command that installs it on this distribution ("" when nothing's
    missing or it's unknown).

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
        stub = False
        missing = []
        for label, caps in CODECS:
            able = [factory.get_name() for factory in decoders if factory.can_sink_any_caps(Gst.Caps.from_string(caps))]
            # OpenH264 on its stand-in decodes nothing: it doesn't count.
            if able and set(able) <= {"openh264dec"} and openh264_stub():
                able, stub = [], True
            if not able:
                missing.append(label)
        missing += [label for label, names in PLAYERS if not all(Gst.ElementFactory.find(name) for name in names)]
    except Exception as error:
        print(f"natter: couldn't ask GStreamer about codecs: {error}")
        return [], ""
    if not missing:
        return [], ""
    command = _install_command(MEDIA, release)
    if stub and command.startswith("sudo dnf install "):
        command += " && sudo dnf swap noopenh264 openh264"  # the plugin's there: the library is the stand-in
    return missing, command


def calls_report(lacking: list[str], release: Optional[dict[str, str]] = None) -> tuple[str, str]:
    """What calls in WhatsApp's page lack here, in words, and the command that installs it on this
    distribution ("" when it's unknown). `lacking` are piece names (natter.__main__.calls_missing);
    ("", "") when there are none."""
    from .distro import AUTO, PIECE, packages

    pieces = [PIECE[name] for name in lacking if name in PIECE]
    if not pieces:
        return "", ""
    words = "; ".join(piece.words for piece in pieces)
    return words, _install_command({manager: " ".join(packages(pieces, manager)) for manager in AUTO}, release)


def _install_command(packages: dict[str, str], release: Optional[dict[str, str]]) -> str:
    """install_hint, but "" in a sandbox, whose runtime brings its own packages, and on any trouble."""
    if in_sandbox():
        return ""
    try:
        return install_hint(packages, release)
    except Exception:
        return ""
