"""Natter's place on the system: its own systemd scope, memory it gives back, the sandbox,
the Background portal, and the GStreamer codecs WhatsApp's media needs.

Desktops start each app in a transient unit named after it (GNOME: app-gnome-<app id>-<pid>.scope,
Ada: app-ada-…, KDE: app-<app id>@<random>.service, Flatpak: app-flatpak-<app id>-<pid>.scope),
so the task manager and the desktop see the app's processes as one group. Natter started any
other way (a terminal, autostart, a script) would otherwise land in whatever unit started it,
with its WebKit processes counted as that app's.
"""

from __future__ import annotations

import errno
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID, NAME
from .distro import GST_PYTHON, MEDIA, install_hint

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


def move_to_own_scope(timeout: float = 0.5) -> Optional[str]:
    """Put Natter in a transient scope of its own, before it starts any WebKit process.

    Returns the scope's name, or None when Natter already has one, runs in a sandbox (whose
    launcher made the scope), or there's no systemd user manager to ask.
    """
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
    try:
        properties = [
            ("PIDs", GLib.Variant("au", [pid])),
            ("Description", GLib.Variant("s", NAME)),
            ("Slice", GLib.Variant("s", "app.slice")),
            ("CollectMode", GLib.Variant("s", "inactive-or-failed")),
        ]
        bus.call_sync(SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                      "StartTransientUnit",
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


class Reclaimer:
    """Gives Natter's cold memory back while its window is hidden, the way Ada does for paused
    apps, except Natter keeps running so messages and notifications still arrive.

    Pages WhatsApp hasn't touched in a while go to (compressed) swap in small steps; anything
    it needs again comes straight back. It stops at `floor` MB, or when the kernel can't find
    more to take.
    """

    STEP = "64M"

    def __init__(self, floor_mb: int = 300, on_done: Optional[Callable[[int, int], None]] = None) -> None:
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
