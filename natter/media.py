"""The microphone and the camera: what NAME lets WhatsApp use (its own two switches, in the
settings), and what the desktop says about the camera.

Nothing here opens a device. The desktop is asked through its permission store and its camera
portal, and the hardware is looked up in udev's notes and PipeWire's list of inputs.

The microphone has no desktop permission: WebKit records through PulseAudio, which asks nobody,
so its switch is NAME's say alone. The camera has one, kept by the camera portal; but natively
WebKit can also find the camera without the portal (app.py lets it read udev's notes, for when
the portal answers too late), and that way skips the desktop's answer. So NAME keeps the
desktop's answer itself (media_allowed), as the portal would have: its "no" and its lockdown,
none while it can't be read, and with nothing stored, a question of NAME's own (ask_camera_again).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID, NAME
from .system import PORTAL, PORTAL_PATH, in_own_scope, in_sandbox

KINDS = ("microphone", "camera")
STORE = "org.freedesktop.impl.portal.PermissionStore"
STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
TABLE, ENTRY = "devices", "camera"  # where the camera portal keeps each app's answer
CAMERA = "org.freedesktop.portal.Camera"
REQUEST = "org.freedesktop.portal.Request"
REGISTRY = "org.freedesktop.host.portal.Registry"
LOCKDOWN = "org.freedesktop.impl.portal.Lockdown"
BACKENDS = "org.freedesktop.impl.portal.desktop."  # the desktop's portal backends: .gtk, .gnome, .kde…
PROPERTIES = "org.freedesktop.DBus.Properties"
ANSWERS = ("yes", "no", "ask")
UDEV_DATA = Path("/run/udev/data")
# What the portal answers when nothing is stored, or the store has no such table yet.
NOT_FOUND = "org.freedesktop.portal.Error.NotFound"
# What a bus answers when it doesn't have a method or an interface: an older desktop.
UNKNOWN = ("org.freedesktop.DBus.Error.UnknownMethod", "org.freedesktop.DBus.Error.UnknownInterface",
           "org.freedesktop.DBus.Error.UnknownObject")
# What the bus answers when there's nobody by that name to ask.
NO_SERVICE = ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner")
# Seconds before the store is read again after it couldn't be (the last again and again), while
# its answer isn't known: till then WhatsApp has no camera, as a "no" may be stored.
STORE_RETRY = (2, 5, 15, 30)
# Seconds NAME waits for the desktop to answer a question of its own (a dialog nobody answers,
# behind the window): then it takes it as no answer. A late answer still counts, from the store.
ASK_LIMIT = 120


# ---- What WhatsApp may use (pure) -----------------------------------------------------------

def refusal(kind: str, on: bool, desktop: Optional[str]) -> Optional[str]:
    """Why WhatsApp can't have `kind` now: "off" when NAME's switch is off, "desktop" when the
    desktop has said no to NAME's camera (or has cameras off for every app), "waiting" while its
    answer can't be read yet; None when it can. `desktop` is the answer NAME keeps itself
    (App.camera_gate): None where the portal keeps it (Flatpak) or there's none, "ask" when
    nothing is stored (WhatsApp's request then waits for NAME to ask, Account._on_media_request)."""
    if not on:
        return "off"
    if kind == "camera" and desktop == "no":
        return "desktop"
    if kind == "camera" and desktop == "waiting":
        return "waiting"
    return None


def refusals(on_mic: bool, on_cam: bool, desktop: Optional[str], audio: bool, video: bool) -> list[tuple[str, str]]:
    """Each kind a request for the microphone (`audio`) and the camera (`video`) is refused, as
    (kind, reason). A request for both is one request: WebKit can only allow or deny it whole,
    and allow() would hand over a device of each kind, so one refused kind refuses all of it.
    Screen sharing isn't either kind, and these switches leave it alone."""
    found = []
    for kind, asked, on in (("microphone", audio, on_mic), ("camera", video, on_cam)):
        reason = refusal(kind, on, desktop) if asked else None
        if reason:
            found.append((kind, reason))
    return found


def media_allowed(on_mic: bool, on_cam: bool, desktop_answer: Optional[str], audio: bool, video: bool) -> bool:
    """Whether NAME lets WhatsApp have what it asked for: the microphone (`audio`), the camera
    (`video`), or both at once."""
    return not refusals(on_mic, on_cam, desktop_answer, audio, video)


def device_subtitle(kind: str, on: bool, desktop: Optional[str], present: Optional[bool], lockdown: bool,
                    sandboxed: bool, restart: bool = False) -> str:
    """The line under the microphone's or camera's switch: the switch is NAME's choice, the line
    what the desktop or the hardware makes of it. Off wins over everything else. `desktop` is
    the desktop's answer for the camera ("yes", "no", "ask", "waiting" while it can't be read
    natively, or None when it can't be read at all: in the Flatpak, until it's known); `present`
    None when it isn't known; `restart` when the camera can't work again until NAME restarts
    (App._on_anchor_gone)."""
    if not on:
        return "WhatsApp can't use it"
    if restart:
        return f"Restart {NAME} to use it"
    if kind == "microphone":
        return "No microphone found" if present is False else "WhatsApp can use it for calls and voice messages"
    if lockdown:
        return "Your desktop has the camera turned off for all apps"
    if desktop == "no":
        return "Blocked by your desktop's privacy settings"
    if desktop == "waiting":
        return "Waiting for your desktop's privacy settings"
    if present is False:
        return "No camera found"
    if desktop == "ask" or (sandboxed and desktop != "yes"):
        # In the Flatpak NAME can only tell "yes" from the rest, and the rest is mostly nothing
        # stored yet. If it's a "no", How to Allow… (prefs.py) says what to do.
        return "Your desktop will ask the first time WhatsApp uses it"
    return "WhatsApp can use it for video calls"


def stored_answer(permissions: dict, app_id: str = APP_ID) -> str:
    """NAME's answer in the store's camera entry ({app id: [answer]}): "yes", "no", or "ask" for
    nothing stored (the desktop asks then, and stores what's answered). A stored "ask" is a no:
    the desktop asks every time, but its portal hands the camera over only for "yes", so only
    NAME's own way to it (udev) would ever open it, whatever was answered. Ask Again… puts a
    real answer in its place."""
    found = permissions.get(app_id) or []
    answer = found[0] if found else ""
    if answer == "ask":
        return "no"
    return answer if answer in ANSWERS else "ask"


def remote_error(error: GLib.Error) -> str:
    """The D-Bus error name behind a GLib.Error, "" when it isn't one."""
    return Gio.DBusError.get_remote_error(error) or ""


# ---- The hardware, without opening it -------------------------------------------------------

def udev_camera(folder: Path = UDEV_DATA) -> Optional[bool]:
    """Whether udev knows a camera: a video4linux device (major 81) that captures, which is
    what WebKit's own way of finding cameras lists. None when udev's notes can't be read."""
    try:
        entries = [entry for entry in folder.iterdir() if entry.name.startswith("c81:")]
    except OSError:
        return None
    for entry in entries:
        try:
            text = entry.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"^E:ID_V4L_CAPABILITIES=.*:capture:", text, re.M):
            return True
    return False


def has_microphone(sources: str) -> bool:
    """Whether `pactl list sources short` lists an input: a source that isn't the monitor of an
    output (what's playing, not a microphone)."""
    for line in sources.splitlines():
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) > 1 and not fields[1].endswith(".monitor"):
            return True
    return False


def microphone_present(run=subprocess.run) -> Optional[bool]:
    """Whether the computer has a microphone, from PipeWire's (or PulseAudio's) list of inputs:
    listing them opens none. None without pactl (the Flatpak's runtime has none) or on trouble.
    It waits on a program: call it off the main thread."""
    pactl = shutil.which("pactl")
    if not pactl:
        return None
    try:
        listed = run([pactl, "list", "sources", "short"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return has_microphone(listed.stdout) if listed.returncode == 0 else None


# ---- The desktop's answer -------------------------------------------------------------------

def camera_answer(bus: Optional[Gio.DBusConnection], sandboxed: Optional[bool] = None) -> Optional[str]:
    """The desktop's stored answer to NAME using the camera: "yes", "no", or "ask" (also when
    nothing is stored: the desktop asks then). None inside Flatpak or Snap, where the store
    can't be read, and when it can't be asked. Only reads."""
    if bus is None or (in_sandbox() if sandboxed is None else sandboxed):
        return None
    try:
        reply = bus.call_sync(STORE, STORE_PATH, STORE, "Lookup", GLib.Variant("(ss)", (TABLE, ENTRY)),
                              GLib.VariantType("(a{sas}v)"), Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error as error:
        return "ask" if remote_error(error) == NOT_FOUND else None
    return stored_answer(reply.unpack()[0])


class DesktopMedia:
    """What the desktop says about the camera, kept up to date: its stored answer for NAME
    (natively; in the Flatpak only whether it's yes), whether that's known yet, whether there's
    a camera, and whether the desktop has cameras off for every app (lockdown). `on_change()`
    runs on the main loop when any of them changes. Only reads: it never writes the store, and
    never opens the camera."""

    def __init__(self, on_change: Callable[[], None], bus: Optional[Gio.DBusConnection] = None,
                 sandboxed: Optional[bool] = None) -> None:
        self.on_change = on_change
        self.sandboxed = in_sandbox() if sandboxed is None else sandboxed
        self.answer: Optional[str] = None
        # Natively, whether the store has answered: the answer, nothing stored, or no store at
        # all. Until then a "no" may be stored. Retried (STORE_RETRY) until it answers.
        self.known = False
        self._retries, self._retry_timer = 0, 0
        self.lockdown = False
        self._portal_present: Optional[bool] = None
        self._lockdowns: dict[str, bool] = {}  # by backend: the portal uses one, which isn't said
        self._asked: Optional[str] = None  # in the Flatpak, what the desktop last answered NAME's own ask
        self._subscriptions: list[int] = []
        self._closed = False
        if bus is None:
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error:
                bus = None
        self.bus = bus
        if bus is None:
            self.known = True  # no session bus: no desktop permission to keep
            return
        if not self.sandboxed:
            self._subscribe(STORE, STORE, "Changed", STORE_PATH, TABLE, self._on_store_changed)
            # Any backend's: which one the portal takes its lockdown from is its configuration's say.
            self._subscribe(None, PROPERTIES, "PropertiesChanged", PORTAL_PATH, LOCKDOWN, self._on_lockdown_changed)
        self._subscribe(PORTAL, PROPERTIES, "PropertiesChanged", PORTAL_PATH, CAMERA, self._on_portal_changed)
        self.refresh()

    @property
    def present(self) -> Optional[bool]:
        """Whether there's a camera: the portal's word (PipeWire's cameras), and natively also
        udev's, which is what WebKit's own way of finding cameras goes by. None when unknown."""
        if self._portal_present or self.sandboxed:
            return self._portal_present
        found = udev_camera()
        if found:
            return True
        return found if self._portal_present is None else False

    def _subscribe(self, sender, interface: str, member: str, path: str, arg0: str, handler) -> None:
        self._subscriptions.append(self.bus.signal_subscribe(
            sender, interface, member, path, arg0, Gio.DBusSignalFlags.NONE,
            lambda _bus, _sender, _path, _iface, _signal, params: self._closed or handler(params)))

    def close(self) -> None:
        self._closed = True
        if self._retry_timer:
            GLib.source_remove(self._retry_timer)
            self._retry_timer = 0
        if self.bus is not None:
            for subscription in self._subscriptions:
                self.bus.signal_unsubscribe(subscription)
        self._subscriptions = []

    def _set(self, **values) -> None:
        changed = False
        for name, value in values.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed and not self._closed:
            self.on_change()

    def _call(self, dest: str, path: str, interface: str, method: str, args: Optional[GLib.Variant],
              reply: str, then: Callable[[Optional[GLib.Variant], Optional[GLib.Error]], None]) -> None:
        """An asynchronous call: `then(value, None)` or `then(None, error)`, never after close()."""
        def called(bus, result) -> None:
            try:
                value, error = bus.call_finish(result), None
            except GLib.Error as failed:
                value, error = None, failed
            if not self._closed:
                then(value, error)

        self.bus.call(dest, path, interface, method, args, GLib.VariantType(reply), Gio.DBusCallFlags.NONE,
                      5000, None, called)

    def refresh(self) -> None:
        """Ask the desktop again, for when something may have changed that it doesn't announce
        (a switch turned on, an answer given)."""
        if self.bus is None or self._closed:
            return
        self._call(PORTAL, PORTAL_PATH, PROPERTIES, "Get", GLib.Variant("(ss)", (CAMERA, "IsCameraPresent")), "(v)",
                   lambda value, _error: self._set(_portal_present=None if value is None else bool(value.unpack()[0])))
        if self.sandboxed:
            self._probe()
            return
        self._look_up()
        self._ask_backends()

    def _look_up(self) -> None:
        self._call(STORE, STORE_PATH, STORE, "Lookup", GLib.Variant("(ss)", (TABLE, ENTRY)), "(a{sas}v)",
                   self._looked_up)

    def _looked_up(self, value: Optional[GLib.Variant], error: Optional[GLib.Error]) -> None:
        if value is not None:
            self._set(answer=stored_answer(value.unpack()[0]), known=True)
        elif error is not None and remote_error(error) == NOT_FOUND:
            self._set(answer="ask", known=True)  # the desktop has never been asked
        elif error is not None and remote_error(error) in NO_SERVICE:
            self._set(answer=None, known=True)  # no store at all: no desktop permission to keep
        elif not self.known:
            # Not yet read (a store too busy to answer in time, or one that failed to start): a
            # "no" may be stored, so the camera waits (App.camera_gate) and the store is asked again.
            self._retry()
        # Anything else keeps the answer known so far: a "no" mustn't lapse because the computer
        # was slow.

    def _retry(self) -> None:
        if self._retry_timer or self._closed:
            return
        delay = STORE_RETRY[min(self._retries, len(STORE_RETRY) - 1)]
        self._retries += 1
        if self._retries <= len(STORE_RETRY):  # then the same again and again: said once
            print(f"natter: the desktop's permission store didn't answer; asking again in {delay} s", flush=True)
        self._retry_timer = GLib.timeout_add_seconds(delay, self._retried)

    def _retried(self) -> bool:
        self._retry_timer = 0
        if not self.known and not self._closed:
            self._look_up()
        return False  # GLib.SOURCE_REMOVE

    def _on_store_changed(self, params: GLib.Variant) -> None:
        _table, entry, deleted, _data, permissions = params.unpack()
        if entry == ENTRY:
            self._set(answer="ask" if deleted else stored_answer(permissions), known=True)

    def _ask_backends(self) -> None:
        self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", None, "(as)",
                   self._backends)

    def _backends(self, value: Optional[GLib.Variant], _error) -> None:
        for name in value.unpack()[0] if value is not None else []:
            if name.startswith(BACKENDS):
                self._call(name, PORTAL_PATH, PROPERTIES, "Get", GLib.Variant("(ss)", (LOCKDOWN, "disable-camera")),
                           "(v)", lambda found, _error, name=name: found is not None and self._lockdown(
                               name, bool(found.unpack()[0])))

    def _lockdown(self, backend: str, off: bool) -> None:
        self._lockdowns[backend] = off
        self._set(lockdown=any(self._lockdowns.values()))

    def _on_lockdown_changed(self, params: GLib.Variant) -> None:
        # The signal comes from a backend's unique name, which doesn't say which backend it is:
        # ask them all again.
        _interface, changed, invalidated = params.unpack()
        if "disable-camera" in changed or "disable-camera" in invalidated:
            self._ask_backends()

    def _on_portal_changed(self, params: GLib.Variant) -> None:
        _interface, changed, invalidated = params.unpack()
        if "IsCameraPresent" in changed:
            self._set(_portal_present=bool(changed["IsCameraPresent"]))
        elif "IsCameraPresent" in invalidated:
            self.refresh()

    def _probe(self) -> None:
        """In the Flatpak: whether the desktop's answer is yes, without a dialog. The portal
        hands back a PipeWire connection only then (a connection: no camera is opened), closed
        here at once; otherwise it refuses, and says "disabled" under lockdown."""
        def probed(bus, result) -> None:
            try:
                _value, fds = bus.call_with_unix_fd_list_finish(result)
            except GLib.Error as error:
                if self._closed:
                    return
                Gio.DBusError.strip_remote_error(error)
                lockdown = "disabled" in error.message.lower()
                self._set(answer=self._asked if self._asked == "no" else None, lockdown=lockdown)
                return
            for fd in fds.steal_fds() if fds is not None else []:
                os.close(fd)
            if not self._closed:
                self._set(answer="yes", lockdown=False)

        self.bus.call_with_unix_fd_list(PORTAL, PORTAL_PATH, CAMERA, "OpenPipeWireRemote",
                                        GLib.Variant("(a{sv})", ({},)), GLib.VariantType("(h)"),
                                        Gio.DBusCallFlags.NONE, 5000, None, None, probed)

    def asked(self, answer: Optional[str]) -> None:
        """What the desktop answered NAME's own ask (ask_camera_again). Natively the store says
        it too, and is read again; in the Flatpak that answer is all NAME learns of a "no"."""
        if self.sandboxed and answer in ("yes", "no"):
            self._asked = answer
            self._set(answer=answer)
        self.refresh()


# ---- Asking again ---------------------------------------------------------------------------

def private_bus() -> Optional[Gio.DBusConnection]:
    """A session bus connection of NAME's own, apart from GApplication's: the portal settles who
    a caller is at its first call on a connection, and ask_camera_again has to make that call."""
    try:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        return Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
    except GLib.Error:
        return None


def ask_camera_again(bus: Gio.DBusConnection, done: Callable[[Optional[str]], None],
                     sandboxed: Optional[bool] = None, forget: bool = True,
                     later: Optional[Callable[[int, Callable[[], bool]], int]] = None) -> None:
    """Have the desktop ask whether NAME may use the camera: after the user said yes to that in
    NAME (Preferences → Ask Again…), or, without `forget`, for WhatsApp's request when nothing
    is stored and WebKit didn't ask (Account._on_media_request). `done(answer)` runs once, on the
    main loop: "yes", "no", or None when the desktop couldn't be asked, or didn't answer within
    ASK_LIMIT (`later` is GLib.timeout_add_seconds), or its portal stopped before it answered.

    Natively a stored "no" is never asked again, so to ask again NAME first forgets its own
    answer, and only its own (DeletePermission for APP_ID: never another app's row, and never Set
    or SetPermission, as the desktop's own dialog makes the decision). Then NAME asks straight
    away itself, rather than leaving it to WebKit: with nothing stored, WebKitGTK 2.52 sends a
    request of its own at every device listing in WhatsApp's page, and its dialogs stack while
    the page waits on them. The portal stores the answer under APP_ID, the id WebKit's
    sandboxes carry.

    In the Flatpak the store can be neither read nor changed from inside: only AccessCamera,
    which asks when nothing is stored and otherwise answers at once.

    `bus` should be a private connection (private_bus): Register has to be its first call."""
    sandboxed = in_sandbox() if sandboxed is None else sandboxed
    later = later or GLib.timeout_add_seconds
    token = "natter_" + uuid.uuid4().hex
    sender = (bus.get_unique_name() or ":").lstrip(":").replace(".", "_")
    expected = f"{PORTAL_PATH}/request/{sender}/{token}"
    state = {"subscription": 0, "owner": 0, "timer": 0, "done": False}

    def finish(answer: Optional[str]) -> None:
        if state["done"]:
            return
        state["done"] = True
        for key in ("subscription", "owner"):
            if state[key]:
                bus.signal_unsubscribe(state[key])
                state[key] = 0
        if state["timer"]:
            GLib.source_remove(state["timer"])
            state["timer"] = 0
        done(answer)

    def too_long() -> bool:
        state["timer"] = 0
        print(f"natter: the desktop didn't answer about the camera in {ASK_LIMIT} s", flush=True)
        finish(None)
        return False  # GLib.SOURCE_REMOVE

    def owner_changed(params: GLib.Variant) -> None:
        if not params.unpack()[2]:  # gone: a portal that starts again knows nothing of the question
            print("natter: the camera portal stopped before it answered", flush=True)
            finish(None)

    def listen(request: str) -> None:
        if state["subscription"]:
            bus.signal_unsubscribe(state["subscription"])
        state["subscription"] = bus.signal_subscribe(
            PORTAL, REQUEST, "Response", request, None, Gio.DBusSignalFlags.NONE,
            lambda _bus, _sender, _path, _iface, _signal, params: finish("yes" if params.unpack()[0] == 0 else "no"))
        if not state["owner"]:
            state["owner"] = bus.signal_subscribe(
                "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", PORTAL,
                Gio.DBusSignalFlags.NONE, lambda _bus, _sender, _path, _iface, _signal, params: owner_changed(params))
        if not state["timer"]:
            state["timer"] = later(ASK_LIMIT, too_long)

    def access() -> None:
        def called(source, result) -> None:
            try:
                request = source.call_finish(result).unpack()[0]
            except GLib.Error as error:
                # Lockdown answers with an error, not a Response (WebKit waits forever on that).
                print(f"natter: the camera portal didn't ask: {error.message}", flush=True)
                finish(None)
                return
            if not state["done"] and request != expected:
                listen(request)  # a portal too old to use handle_token answers somewhere else

        listen(expected)  # before asking, so a quick answer isn't missed
        bus.call(PORTAL, PORTAL_PATH, CAMERA, "AccessCamera",
                 GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", token)},)), GLib.VariantType("(o)"),
                 Gio.DBusCallFlags.NONE, -1, None, called)

    def forget_answer() -> None:
        def forgotten(source, result) -> None:
            try:
                source.call_finish(result)
            except GLib.Error as error:
                if remote_error(error) != NOT_FOUND:  # nothing stored is as good as forgotten
                    print(f"natter: couldn't forget the desktop's camera answer: {error.message}", flush=True)
                    finish(None)
                    return
            access()

        bus.call(STORE, STORE_PATH, STORE, "DeletePermission", GLib.Variant("(sss)", ("devices", "camera", APP_ID)),
                 None, Gio.DBusCallFlags.NONE, 5000, None, forgotten)

    def registered(source, result) -> None:
        try:
            source.call_finish(result)
        except GLib.Error as error:
            # A portal without the Registry tells host apps apart by their systemd unit, which
            # names NAME only in a scope of its own. Unknown to the portal, the answer would be
            # stored for no app in particular, and given to every app it can't tell apart.
            if remote_error(error) not in UNKNOWN or not in_own_scope():
                print(f"natter: not asking for the camera: the desktop doesn't know it's {NAME} asking "
                      f"({error.message})", flush=True)
                finish(None)
                return
        if forget:
            forget_answer()
        else:
            access()

    if sandboxed:
        access()
        return
    bus.call(PORTAL, PORTAL_PATH, REGISTRY, "Register", GLib.Variant("(sa{sv})", (APP_ID, {})), None,
             Gio.DBusCallFlags.NONE, 5000, None, registered)
