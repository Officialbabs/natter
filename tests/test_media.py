"""Tests for the microphone and camera: the switches' settings and what they allow (media.py),
the desktop's answer and asking it again, WhatsApp's page following the switches (account.py),
the app's side (app.py), the call window (calls.py, callwindow.py), and udev in WebKit's sandbox."""

import ast
import inspect
import os
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import Gio, GLib

from natter import APP_ID, NAME
from natter import account as natter_account
from natter import app as natter_app
from natter import media
from natter.account import GRANT_REUSE, Account
from natter.app import UDEV_DATA, Natter, debug_info, media_debug
from natter.calls import CallWindow
from natter.media import (DesktopMedia, ask_camera_again, camera_answer, device_subtitle, has_microphone,
                          media_allowed, refusals, stored_answer, udev_camera)
from natter.settings import DEFAULTS, Settings

ROOT = Path(__file__).resolve().parent.parent


def dbus_error(name: str, message: str = "it failed") -> GLib.Error:
    return Gio.DBusError.new_for_dbus_error(name, message)


# ---- The settings and what they allow -------------------------------------------------------


class SettingsTest(unittest.TestCase):
    def test_both_on_by_default(self):
        self.assertIs(DEFAULTS["microphone"], True)
        self.assertIs(DEFAULTS["camera"], True)
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder) / "settings.json")
            self.assertTrue(settings["microphone"] and settings["camera"])


class AllowedTest(unittest.TestCase):
    def test_both_on_allows_everything(self):
        for audio, video in ((True, False), (False, True), (True, True), (False, False)):
            self.assertTrue(media_allowed(True, True, None, audio, video))
            self.assertTrue(media_allowed(True, True, "yes", audio, video))
            self.assertTrue(media_allowed(True, True, "ask", audio, video))

    def test_microphone_off(self):
        self.assertFalse(media_allowed(False, True, None, True, False))
        self.assertFalse(media_allowed(False, True, None, True, True))  # one request: refused whole
        self.assertTrue(media_allowed(False, True, None, False, True))

    def test_camera_off(self):
        self.assertFalse(media_allowed(True, False, None, False, True))
        self.assertFalse(media_allowed(True, False, None, True, True))
        self.assertTrue(media_allowed(True, False, None, True, False))

    def test_the_desktops_no_refuses_anything_with_the_camera(self):
        self.assertFalse(media_allowed(True, True, "no", False, True))
        self.assertFalse(media_allowed(True, True, "no", True, True))
        self.assertTrue(media_allowed(True, True, "no", True, False))  # the microphone has no desktop say

    def test_screen_sharing_is_neither(self):
        # A screen-sharing request is for neither device (WebKit's display flag), so nothing refuses it.
        self.assertTrue(media_allowed(False, False, "no", False, False))

    def test_refusals_say_which_and_why(self):
        self.assertEqual(refusals(False, True, "no", True, True), [("microphone", "off"), ("camera", "desktop")])
        self.assertEqual(refusals(False, False, "no", True, True), [("microphone", "off"), ("camera", "off")])
        self.assertEqual(refusals(True, True, None, True, True), [])


class SubtitleTest(unittest.TestCase):
    def line(self, kind, on=True, desktop="yes", present=True, lockdown=False, sandboxed=False, **more):
        return device_subtitle(kind, on, desktop, present, lockdown, sandboxed, **more)

    def test_microphone(self):
        self.assertEqual(self.line("microphone", desktop=None), "WhatsApp can use it for calls and voice messages")
        self.assertEqual(self.line("microphone", desktop=None, present=None),
                         "WhatsApp can use it for calls and voice messages")  # unknown isn't missing
        self.assertEqual(self.line("microphone", desktop=None, present=False), "No microphone found")
        self.assertEqual(self.line("microphone", on=False, desktop=None, present=False), "WhatsApp can't use it")

    def test_camera(self):
        self.assertEqual(self.line("camera"), "WhatsApp can use it for video calls")
        self.assertEqual(self.line("camera", desktop="ask"), "Your desktop will ask the first time WhatsApp uses it")
        self.assertEqual(self.line("camera", desktop="no"), "Blocked by your desktop's privacy settings")
        self.assertEqual(self.line("camera", lockdown=True), "Your desktop has the camera turned off for all apps")
        self.assertEqual(self.line("camera", present=False), "No camera found")
        self.assertEqual(self.line("camera", desktop=None, present=None), "WhatsApp can use it for video calls")

    def test_off_wins_over_every_desktop_state(self):
        for desktop in ("yes", "no", "ask", None):
            for lockdown in (False, True):
                for present in (True, False, None):
                    self.assertEqual(self.line("camera", False, desktop, present, lockdown), "WhatsApp can't use it")
        self.assertEqual(self.line("camera", on=False, restart=True), "WhatsApp can't use it")

    def test_lockdown_and_a_no_come_before_a_missing_camera(self):
        self.assertEqual(self.line("camera", desktop="no", present=False, lockdown=True),
                         "Your desktop has the camera turned off for all apps")
        self.assertEqual(self.line("camera", desktop="no", present=False), "Blocked by your desktop's privacy settings")

    def test_sandboxed(self):
        # Inside Flatpak only a yes can be read; the rest may be nothing stored yet.
        self.assertEqual(self.line("camera", desktop=None, sandboxed=True),
                         "Your desktop will ask the first time WhatsApp uses it")
        self.assertEqual(self.line("camera", desktop="yes", sandboxed=True), "WhatsApp can use it for video calls")
        self.assertEqual(self.line("camera", desktop="no", sandboxed=True), "Blocked by your desktop's privacy settings")
        self.assertEqual(self.line("microphone", desktop=None, sandboxed=True),
                         "WhatsApp can use it for calls and voice messages")

    def test_restart(self):
        for kind in ("microphone", "camera"):
            self.assertEqual(self.line(kind, present=False, restart=True), f"Restart {NAME} to use it")

    def test_wording(self):
        lines = {self.line(kind, on, desktop, present, lockdown, sandboxed, restart=restart)
                 for kind in ("microphone", "camera") for on in (True, False) for desktop in ("yes", "no", "ask", None)
                 for present in (True, False, None) for lockdown in (True, False) for sandboxed in (True, False)
                 for restart in (True, False)}
        for line in lines:
            self.assertFalse(line.endswith("."), line)
            self.assertNotIn("’", line)  # straight apostrophes, as the rest of Preferences
            self.assertEqual(line[0], line[0].upper(), line)

    def test_takes_media_state_as_it_comes(self):
        state = {"on": True, "desktop": "no", "present": True, "lockdown": False, "sandboxed": False, "restart": False}
        self.assertEqual(device_subtitle("camera", **state), "Blocked by your desktop's privacy settings")


# ---- The hardware ----------------------------------------------------------------------------


class HardwareTest(unittest.TestCase):
    def test_microphones_are_inputs_not_monitors(self):
        listed = ("57\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
                  "58\talsa_input.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n")
        self.assertTrue(has_microphone(listed))
        self.assertFalse(has_microphone(listed.splitlines()[0]))
        self.assertFalse(has_microphone(""))

    def test_microphone_present_without_pactl(self):
        with mock.patch.object(media.shutil, "which", return_value=None):
            self.assertIsNone(media.microphone_present())

    def test_microphone_present_runs_only_a_listing(self):
        ran = []

        def run(argv, **_kwargs):
            ran.append(argv)
            return SimpleNamespace(returncode=0, stdout="1\talsa_input.usb-headset\tPipeWire\ts16le\tIDLE\n")

        with mock.patch.object(media.shutil, "which", return_value="/usr/bin/pactl"):
            self.assertTrue(media.microphone_present(run))
            self.assertEqual(ran, [["/usr/bin/pactl", "list", "sources", "short"]])
            failed = lambda argv, **_kwargs: SimpleNamespace(returncode=1, stdout="")
            self.assertIsNone(media.microphone_present(failed))

    def test_udev_cameras(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            self.assertFalse(udev_camera(folder))
            (folder / "c81:1").write_text("E:ID_V4L_VERSION=2\nE:ID_V4L_CAPABILITIES=:\n")  # metadata node
            (folder / "c189:2").write_text("E:ID_V4L_CAPABILITIES=:capture:\n")  # not video4linux
            self.assertFalse(udev_camera(folder))
            (folder / "c81:0").write_text("E:ID_V4L_VERSION=2\nE:ID_V4L_CAPABILITIES=:capture:\n")
            self.assertTrue(udev_camera(folder))
            self.assertIsNone(udev_camera(folder / "missing"))


# ---- The desktop's answer --------------------------------------------------------------------


class FakeBus:
    """A session bus that answers from `replies` ({method: Variant or GLib.Error, or a function of
    the call's destination}), records every call, and delivers signals on emit()."""

    def __init__(self, replies=None, name=":1.42"):
        self.replies = dict(replies or {})
        self.calls, self.subscriptions, self.unsubscribed, self.closed_fds = [], {}, [], []
        self._next, self.name = 0, name

    def get_unique_name(self):
        return self.name

    def _reply(self, dest, method):
        reply = self.replies.get(method)
        return reply(dest) if callable(reply) else reply

    def call_sync(self, dest, path, iface, method, args, reply_type, flags, timeout, cancellable):
        self.calls.append((dest, iface, method, args.unpack() if args is not None else None))
        reply = self._reply(dest, method)
        if isinstance(reply, GLib.Error):
            raise reply
        return reply

    def call(self, dest, path, iface, method, args, reply_type, flags, timeout, cancellable, callback):
        self.calls.append((dest, iface, method, args.unpack() if args is not None else None))
        callback(self, (dest, method))

    def call_finish(self, result):
        reply = self._reply(*result)
        if isinstance(reply, GLib.Error):
            raise reply
        return reply

    def call_with_unix_fd_list(self, dest, path, iface, method, args, reply_type, flags, timeout, fds,
                               cancellable, callback):
        self.calls.append((dest, iface, method, args.unpack()))
        callback(self, (dest, method))

    def call_with_unix_fd_list_finish(self, result):
        reply = self._reply(*result)
        if isinstance(reply, GLib.Error):
            raise reply
        return reply

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self._next += 1
        self.subscriptions[self._next] = (sender, iface, member, path, arg0, callback)
        return self._next

    def signal_unsubscribe(self, subscription):
        self.unsubscribed.append(subscription)
        self.subscriptions.pop(subscription, None)

    def emit(self, member, params, path=None, arg0=None):
        for sender, iface, wanted, subscribed_path, subscribed_arg0, callback in list(self.subscriptions.values()):
            if wanted == member and (path is None or path == subscribed_path) \
                    and (arg0 is None or subscribed_arg0 in (None, arg0)):
                callback(self, ":1.7", subscribed_path, iface, member, params)

    def methods(self):
        return [call[2] for call in self.calls]


def lookup(rows):
    return GLib.Variant("(a{sas}v)", (rows, GLib.Variant("s", "")))


class AnswerTest(unittest.TestCase):
    def test_stored_answers(self):
        self.assertEqual(stored_answer({APP_ID: ["yes"]}), "yes")
        self.assertEqual(stored_answer({APP_ID: ["no"], "other.App": ["yes"]}), "no")
        self.assertEqual(stored_answer({APP_ID: ["ask"]}), "ask")
        self.assertEqual(stored_answer({"other.App": ["no"]}), "ask")  # nothing for Natter: the desktop asks
        self.assertEqual(stored_answer({APP_ID: []}), "ask")
        self.assertEqual(stored_answer({APP_ID: ["maybe"]}), "ask")

    def test_lookup(self):
        bus = FakeBus({"Lookup": lookup({APP_ID: ["no"], "": ["yes"]})})
        self.assertEqual(camera_answer(bus, sandboxed=False), "no")
        self.assertEqual(bus.calls, [(media.STORE, media.STORE, "Lookup", ("devices", "camera"))])

    def test_nothing_stored_means_the_desktop_asks(self):
        bus = FakeBus({"Lookup": dbus_error(media.NOT_FOUND, "No entry for camera")})
        self.assertEqual(camera_answer(bus, sandboxed=False), "ask")

    def test_unknown(self):
        self.assertIsNone(camera_answer(FakeBus({"Lookup": dbus_error("org.freedesktop.DBus.Error.ServiceUnknown")}),
                                        sandboxed=False))
        self.assertIsNone(camera_answer(None, sandboxed=False))
        bus = FakeBus({"Lookup": lookup({APP_ID: ["no"]})})
        self.assertIsNone(camera_answer(bus, sandboxed=True))  # the store isn't reachable from a sandbox
        self.assertEqual(bus.calls, [])


class DesktopMediaTest(unittest.TestCase):
    def watch(self, sandboxed=False, **replies):
        defaults = {"Lookup": lookup({APP_ID: ["yes"]}), "Get": GLib.Variant("(v)", (GLib.Variant("b", True),)),
                    "ListNames": GLib.Variant("(as)", (["org.freedesktop.portal.Desktop"],))}
        bus = FakeBus({**defaults, **replies})
        changes = []
        with mock.patch.object(media, "udev_camera", return_value=None):
            desktop = DesktopMedia(lambda: changes.append(1), bus, sandboxed=sandboxed)
        return bus, desktop, changes

    def test_reads_and_follows_the_store(self):
        bus, desktop, changes = self.watch()
        self.assertEqual(desktop.answer, "yes")
        self.assertTrue(desktop._portal_present)
        self.assertTrue(changes)
        changed = GLib.Variant("(ssbva{sas})", ("devices", "camera", False, GLib.Variant("s", ""), {APP_ID: ["no"]}))
        bus.emit("Changed", changed, arg0="devices")
        self.assertEqual(desktop.answer, "no")
        other = GLib.Variant("(ssbva{sas})", ("devices", "speakers", False, GLib.Variant("s", ""), {APP_ID: ["yes"]}))
        bus.emit("Changed", other, arg0="devices")
        self.assertEqual(desktop.answer, "no")  # another entry
        deleted = GLib.Variant("(ssbva{sas})", ("devices", "camera", True, GLib.Variant("s", ""), {}))
        bus.emit("Changed", deleted, arg0="devices")
        self.assertEqual(desktop.answer, "ask")

    def test_a_slow_store_keeps_a_no(self):
        bus, desktop, _changes = self.watch(Lookup=lookup({APP_ID: ["no"]}))
        self.assertEqual(desktop.answer, "no")
        bus.replies["Lookup"] = dbus_error("org.freedesktop.DBus.Error.NoReply", "Timeout was reached")
        desktop.refresh()
        self.assertEqual(desktop.answer, "no")
        bus.replies["Lookup"] = dbus_error("org.freedesktop.DBus.Error.ServiceUnknown")
        desktop.refresh()
        self.assertIsNone(desktop.answer)  # no store at all

    def test_camera_present(self):
        bus, desktop, _changes = self.watch(Get=GLib.Variant("(v)", (GLib.Variant("b", False),)))
        with mock.patch.object(media, "udev_camera", return_value=False):
            self.assertFalse(desktop.present)
        with mock.patch.object(media, "udev_camera", return_value=True):
            self.assertTrue(desktop.present)  # WebKit's own way finds it natively
        changed = GLib.Variant("(sa{sv}as)", (media.CAMERA, {"IsCameraPresent": GLib.Variant("b", True)}, []))
        bus.emit("PropertiesChanged", changed, arg0=media.CAMERA)
        self.assertTrue(desktop._portal_present)
        _bus, sandboxed, _changes = self.watch(sandboxed=True, Get=GLib.Variant("(v)", (GLib.Variant("b", False),)),
                                               OpenPipeWireRemote=dbus_error("org.freedesktop.portal.Error.NotAllowed"))
        with mock.patch.object(media, "udev_camera", return_value=True):
            self.assertFalse(sandboxed.present)  # the Flatpak's WebKit has the portal only

    def test_lockdown(self):
        def get(dest):
            on = dest == "org.freedesktop.impl.portal.desktop.gtk"
            return GLib.Variant("(v)", (GLib.Variant("b", on),))

        names = GLib.Variant("(as)", (["org.freedesktop.impl.portal.desktop.gtk", "org.freedesktop.impl.portal.desktop.ada",
                                       ":1.5", "org.freedesktop.portal.Desktop"],))
        bus, desktop, _changes = self.watch(ListNames=names, Get=get)
        self.assertTrue(desktop.lockdown)
        asked = [call[0] for call in bus.calls if call[2] == "Get" and call[3] == (media.LOCKDOWN, "disable-camera")]
        self.assertEqual(asked, ["org.freedesktop.impl.portal.desktop.gtk", "org.freedesktop.impl.portal.desktop.ada"])
        bus.replies["Get"] = GLib.Variant("(v)", (GLib.Variant("b", False),))
        bus.emit("PropertiesChanged", GLib.Variant("(sa{sv}as)", (media.LOCKDOWN, {"disable-camera": GLib.Variant("b", False)}, [])),
                 arg0=media.LOCKDOWN)
        self.assertFalse(desktop.lockdown)

    def test_flatpak_reads_only_whether_its_yes(self):
        fds = []
        reply = (GLib.Variant("(h)", (0,)), SimpleNamespace(steal_fds=lambda: fds))
        bus, desktop, _changes = self.watch(sandboxed=True, OpenPipeWireRemote=reply)
        self.assertEqual(desktop.answer, "yes")
        self.assertNotIn("Lookup", bus.methods())  # the store isn't reachable from inside
        self.assertNotIn("ListNames", bus.methods())
        bus.replies["OpenPipeWireRemote"] = dbus_error("org.freedesktop.portal.Error.NotAllowed", "Permission denied")
        desktop.refresh()
        self.assertIsNone(desktop.answer)  # not yes: unset, no and ask look the same
        desktop.asked("no")  # Natter's own ask was refused
        self.assertEqual(desktop.answer, "no")
        bus.replies["OpenPipeWireRemote"] = dbus_error("org.freedesktop.portal.Error.NotAllowed", "Camera access disabled")
        desktop.refresh()
        self.assertTrue(desktop.lockdown)

    def test_closed_hears_nothing(self):
        bus, desktop, changes = self.watch()
        desktop.close()
        self.assertEqual(bus.subscriptions, {})
        count = len(changes)
        desktop.refresh()
        self.assertEqual(len(changes), count)

    def test_never_writes(self):
        bus, desktop, _changes = self.watch()
        desktop.refresh()
        desktop.asked("yes")
        self.assertFalse({"Set", "SetPermission", "SetValue", "Delete", "DeletePermission"} & set(bus.methods()))


class AskAgainTest(unittest.TestCase):
    def ask(self, sandboxed=False, response=0, **replies):
        defaults = {"Register": GLib.Variant("()", ()), "DeletePermission": GLib.Variant("()", ()),
                    "AccessCamera": lambda dest: GLib.Variant("(o)", (self.request(bus),))}
        bus = FakeBus({**defaults, **replies})
        answers = []
        ask_camera_again(bus, answers.append, sandboxed=sandboxed)
        if bus.subscriptions and response is not None:
            bus.emit("Response", GLib.Variant("(ua{sv})", (response, {})))
        return bus, answers

    @staticmethod
    def request(bus):
        token = next(call[3][0]["handle_token"] for call in bus.calls if call[2] == "AccessCamera")
        return f"/org/freedesktop/portal/desktop/request/1_42/{token}"

    def test_natively_forgets_only_its_own_answer_then_asks(self):
        bus, answers = self.ask()
        self.assertEqual(bus.methods(), ["Register", "DeletePermission", "AccessCamera"])
        self.assertEqual(bus.calls[0][3], (APP_ID, {}))
        self.assertEqual(bus.calls[1][3], ("devices", "camera", APP_ID))
        self.assertEqual(answers, ["yes"])
        self.assertEqual(bus.subscriptions, {})  # stops listening once answered

    def test_refused(self):
        _bus, answers = self.ask(response=1)
        self.assertEqual(answers, ["no"])

    def test_nothing_stored_is_as_good_as_forgotten(self):
        bus, answers = self.ask(DeletePermission=dbus_error(media.NOT_FOUND))
        self.assertIn("AccessCamera", bus.methods())
        self.assertEqual(answers, ["yes"])

    def test_not_asked_unless_the_desktop_knows_its_natter(self):
        # The answer would be stored for no app in particular, and given to every unnamed one.
        with mock.patch.object(media, "in_own_scope", return_value=False):
            bus, answers = self.ask(Register=dbus_error("org.freedesktop.portal.Error.Failed"))
        self.assertEqual(bus.methods(), ["Register"])
        self.assertEqual(answers, [None])
        with mock.patch.object(media, "in_own_scope", return_value=False):
            bus, answers = self.ask(Register=dbus_error("org.freedesktop.DBus.Error.UnknownMethod"))
        self.assertEqual(bus.methods(), ["Register"])
        # Without the Registry, a scope of Natter's own names it to the portal.
        with mock.patch.object(media, "in_own_scope", return_value=True):
            bus, answers = self.ask(Register=dbus_error("org.freedesktop.DBus.Error.UnknownMethod"))
        self.assertEqual(bus.methods(), ["Register", "DeletePermission", "AccessCamera"])
        self.assertEqual(answers, ["yes"])

    def test_lockdown_answers_with_an_error(self):
        bus, answers = self.ask(AccessCamera=dbus_error("org.freedesktop.portal.Error.NotAllowed", "disabled"),
                                response=None)
        self.assertEqual(answers, [None])
        self.assertEqual(bus.subscriptions, {})

    def test_flatpak_only_asks(self):
        bus, answers = self.ask(sandboxed=True)
        self.assertEqual(bus.methods(), ["AccessCamera"])
        self.assertEqual(answers, ["yes"])

    def test_answers_once(self):
        bus, answers = self.ask()
        bus.emit("Response", GLib.Variant("(ua{sv})", (1, {})))
        self.assertEqual(answers, ["yes"])


def calls_in(tree) -> list[tuple[str, ast.Call]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    found.append((argument.value, node))
    return found


class StoreWritesTest(unittest.TestCase):
    """Natter never writes the desktop's permission store, except to forget its own camera
    answer when the user asked to be asked again."""

    def setUp(self):
        self.tree = ast.parse((ROOT / "natter" / "media.py").read_text())

    def test_never_sets(self):
        names = [name for name, _call in calls_in(self.tree)]
        for method in ("Set", "SetPermission", "SetValue", "Delete"):
            self.assertNotIn(method, names)
        # Nor through DesktopMedia's own call helper (_call), which passes its method on.
        attributes = {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({"call", "call_sync"} - attributes)
        sent = {name for name, call in calls_in(self.tree)
                if isinstance(call.func, ast.Attribute) and call.func.attr in ("call", "call_sync", "_call")}
        self.assertFalse({"Set", "SetPermission", "SetValue", "Delete"} & sent)
        self.assertIn("DeletePermission", sent)

    def test_deletes_only_its_own_camera_answer_and_only_when_asking_again(self):
        functions = [node for node in ast.walk(self.tree) if isinstance(node, (ast.FunctionDef,))]
        deleting = [(function.name, call) for function in functions for name, call in calls_in(function)
                    if name == "DeletePermission"]
        owners = {name for name, _call in deleting}
        # Nested functions show up under ask_camera_again and the one they're in.
        self.assertEqual(len({id(call) for _name, call in deleting}), 1)
        self.assertIn("ask_camera_again", owners)
        call = deleting[0][1]
        variant = call.args[call.args.index(next(a for a in call.args if isinstance(a, ast.Constant)
                                                     and a.value == "DeletePermission")) + 1]
        table, entry, app_id = variant.args[1].elts
        self.assertEqual((table.value, entry.value), ("devices", "camera"))
        self.assertIsInstance(app_id, ast.Name)
        self.assertEqual(app_id.id, "APP_ID")

    def test_nothing_new_touches_the_page(self):
        # Ban safety: the switches act as a browser would, never through WhatsApp's page.
        for function in (Account._on_permission, Account._on_media_request, Account._on_permission_query,
                         Account.stop_media, Account.media_allowed_again, Account._on_capture_state,
                         Account.lose_media, Account._end_capture, Natter._apply_media, Natter._on_desktop_media,
                         Natter.ask_camera_again, Natter._on_anchor_gone, Natter.media_state):
            source = inspect.getsource(function)
            for banned in (".run(", "evaluate_javascript", "call_async_javascript_function", "dispatchEvent",
                           "devices_changed"):
                self.assertNotIn(banned, source, function.__name__)
        for banned in ("evaluate_javascript", "call_async_javascript_function", "localStorage", "indexedDB"):
            self.assertNotIn(banned, (ROOT / "natter" / "media.py").read_text())


# ---- WhatsApp's page follows the switches (account.py) -------------------------------------------


class State:
    NONE, ACTIVE, MUTED = "none", "active", "muted"


class Request:
    def __init__(self, audio=False, video=False):
        self.audio, self.video, self.answer = audio, video, None

    def allow(self):
        self.answer = "allow"

    def deny(self):
        self.answer = "deny"


class MediaRequest(Request):
    pass


class NotificationRequest(Request):
    pass


class DeviceInfoRequest(Request):
    pass


class GeolocationRequest(Request):
    pass


WEBKIT = SimpleNamespace(
    UserMediaPermissionRequest=MediaRequest, NotificationPermissionRequest=NotificationRequest,
    DeviceInfoPermissionRequest=DeviceInfoRequest, ClipboardPermissionRequest=type("Clipboard", (Request,), {}),
    user_media_permission_is_for_audio_device=lambda request: request.audio,
    user_media_permission_is_for_video_device=lambda request: request.video,
    MediaCaptureState=State, PermissionState=SimpleNamespace(GRANTED="granted", DENIED="denied"),
    LoadEvent=SimpleNamespace(COMMITTED="committed", FINISHED="finished"))


class View:
    """What the switches use of a WebView: capture states, and its settings."""

    def __init__(self):
        self.state = {"camera": State.NONE, "microphone": State.NONE}
        self.set_to, self.media_stream, self.loaded = [], True, []
        self.get_camera_capture_state = lambda: self.state["camera"]
        self.get_microphone_capture_state = lambda: self.state["microphone"]
        self.set_camera_capture_state = lambda value: self.set_to.append(("camera", value))
        self.set_microphone_capture_state = lambda value: self.set_to.append(("microphone", value))

    def get_settings(self):
        return SimpleNamespace(set_enable_media_stream=lambda on: setattr(self, "media_stream", on))

    def load_uri(self, uri):
        self.loaded.append(uri)


class Page:
    """An Account as the switches see it: Account's own code, on stand-in parts."""

    def __init__(self, settings, gate=None, trusted=True, calling=False):
        self.app = SimpleNamespace(settings=settings, camera_gate=lambda: gate)
        self.view, self.calling, self.removed, self.trusted = View(), calling, False, trusted
        self.emitted, self.fresh, self.reloads = [], 0, 0
        self.home, self._reload_timer, self.reloading = "https://web.whatsapp.com/", 0, False
        self.ready = self.needs_link = False
        self.failed = self.rtc = self.look = self.look_dark = None
        self._allowed_at = dict.fromkeys(media.KINDS)
        self._ended_at = dict.fromkeys(media.KINDS)
        self.refused_since_load = dict.fromkeys(media.KINDS, False)
        self._backstop = dict.fromkeys(media.KINDS, True)
        self._lose_media_after_call = False

    def __getattr__(self, name):
        return types.MethodType(getattr(Account, name), self)

    def _trusted(self, uri=None):
        return self.trusted

    def emit(self, *signal):
        self.emitted.append(signal)

    def fresh_page(self):
        self.fresh += 1

    def reload(self):
        self.reloads += 1

    def _end_link(self):
        pass

    def _watch_page(self, *_args):
        pass

    def _set_reloading(self, reloading):
        self.reloading = reloading


def settings(test, **values):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    found = Settings(Path(folder.name) / "settings.json")
    for key, value in values.items():
        found[key] = value
    return found


class PageTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(natter_account, "WebKit", WEBKIT)
        patch.start()
        self.addCleanup(patch.stop)

    def page(self, gate=None, trusted=True, calling=False, **values):
        return Page(settings(self, **values), gate, trusted, calling)

    def ask(self, page, request):
        self.assertTrue(page._on_permission(None, request))
        return request.answer


class PermissionTest(PageTest):
    def test_follows_the_switches(self):
        for microphone in (True, False):
            for camera in (True, False):
                for gate in (None, "yes", "no", "ask"):
                    for audio, video in ((True, False), (False, True), (True, True)):
                        page = self.page(gate, microphone=microphone, camera=camera)
                        expected = media_allowed(microphone, camera, gate, audio, video)
                        self.assertEqual(self.ask(page, MediaRequest(audio, video)), "allow" if expected else "deny",
                                         (microphone, camera, gate, audio, video))

    def test_an_untrusted_page_is_always_refused(self):
        page = self.page(trusted=False)
        for request in (MediaRequest(True, True), NotificationRequest(), DeviceInfoRequest()):
            self.assertEqual(self.ask(page, request), "deny")
        self.assertEqual(page.emitted, [])

    def test_a_refusal_is_said_and_noted(self):
        page = self.page(camera=False)
        self.assertEqual(self.ask(page, MediaRequest(False, True)), "deny")
        self.assertEqual(page.emitted, [("media-refused", "camera", "off")])
        self.assertEqual(page.refused_since_load, {"microphone": False, "camera": True})
        page = self.page("no", microphone=False)
        self.ask(page, MediaRequest(True, True))
        self.assertEqual(page.emitted, [("media-refused", "microphone", "off"), ("media-refused", "camera", "desktop")])
        self.assertEqual(page._allowed_at, {"microphone": None, "camera": None})

    def test_allowed_kinds_are_noted(self):
        page = self.page()
        with mock.patch.object(natter_account.time, "monotonic", return_value=50.0):
            self.ask(page, MediaRequest(True, False))
        self.assertEqual(page._allowed_at, {"microphone": 50.0, "camera": None})
        self.assertEqual(page.emitted, [])

    def test_whatsapps_own_loads_keep_allowed_and_clear_refused(self):
        page = self.page(camera=False)
        self.ask(page, MediaRequest(True, False))
        self.ask(page, MediaRequest(False, True))
        page._on_load_changed(None, WEBKIT.LoadEvent.COMMITTED)
        self.assertEqual(page.refused_since_load, {"microphone": False, "camera": False})
        self.assertIsNotNone(page._allowed_at["microphone"])  # WebKit keeps its grants then

    def test_natters_own_loads_clear_both(self):
        page = self.page(camera=False)
        self.ask(page, MediaRequest(True, False))
        self.ask(page, MediaRequest(False, True))
        page._ended_at["microphone"] = 1.0
        page._load(page.home)
        self.assertEqual(page._allowed_at, {"microphone": None, "camera": None})
        self.assertEqual(page._ended_at, {"microphone": None, "camera": None})
        self.assertEqual(page.refused_since_load, {"microphone": False, "camera": False})
        self.assertEqual(page.view.loaded, [page.home])

    def test_device_names_while_either_is_on(self):
        self.assertEqual(self.ask(self.page(microphone=False), DeviceInfoRequest()), "allow")
        self.assertEqual(self.ask(self.page(camera=False), DeviceInfoRequest()), "allow")
        self.assertEqual(self.ask(self.page(microphone=False, camera=False), DeviceInfoRequest()), "deny")

    def test_the_rest_as_before(self):
        self.assertEqual(self.ask(self.page(microphone=False, camera=False), NotificationRequest()), "allow")
        self.assertEqual(self.ask(self.page(), GeolocationRequest()), "deny")


class QueryTest(PageTest):
    def query(self, page, name):
        answers = []
        query = SimpleNamespace(get_name=lambda: name, finish=answers.append)
        handled = page._on_permission_query(None, query)
        return handled, answers

    def test_per_switch(self):
        page = self.page(camera=False)
        self.assertEqual(self.query(page, "camera"), (True, ["denied"]))
        self.assertEqual(self.query(page, "microphone"), (True, ["granted"]))
        page.app.settings["camera"] = True
        page.app.settings["microphone"] = False
        self.assertEqual(self.query(page, "camera"), (True, ["granted"]))
        self.assertEqual(self.query(page, "microphone"), (True, ["denied"]))

    def test_notifications_unchanged(self):
        page = self.page(microphone=False, camera=False)
        self.assertEqual(self.query(page, "notifications"), (True, ["granted"]))
        self.assertEqual(self.query(page, "geolocation"), (False, []))

    def test_untrusted(self):
        page = self.page(trusted=False)
        for name in ("camera", "microphone", "notifications"):
            self.assertEqual(self.query(page, name), (False, []))


class OffTest(PageTest):
    def test_ends_only_what_captures(self):
        for state in (State.ACTIVE, State.MUTED):
            page = self.page(camera=False)
            page.view.state["camera"] = state
            self.assertTrue(page.stop_media("camera"))
            self.assertEqual(page.view.set_to, [("camera", State.NONE)])
            self.assertEqual(page.fresh, 0)
        page = self.page(camera=False)
        page.view.state["microphone"] = State.ACTIVE  # the other kind
        self.assertFalse(page.stop_media("camera"))
        self.assertEqual(page.view.set_to, [])

    def test_a_grant_webkit_would_reuse_takes_a_fresh_page(self):
        page = self.page()
        self.ask(page, MediaRequest(False, True))
        page.app.settings["camera"] = False
        self.assertFalse(page.stop_media("camera"))
        self.assertEqual((page.fresh, page.view.set_to), (1, []))

    def test_nothing_allowed_nothing_to_do(self):
        page = self.page(camera=False)
        self.assertFalse(page.stop_media("camera"))
        self.assertEqual((page.fresh, page.reloads, page.view.set_to), (0, 0, []))

    def test_not_in_a_call(self):
        page = self.page(calling=True)
        self.ask(page, MediaRequest(False, True))
        page.app.settings["camera"] = False
        page.stop_media("camera")
        self.assertEqual(page.fresh, 0)  # the backstop sees to it instead

    def test_webkit_forgets_after_a_while(self):
        page = self.page()
        self.ask(page, MediaRequest(False, True))
        page.app.settings["camera"] = False
        with mock.patch.object(natter_account.time, "monotonic", return_value=1000.0):
            page._on_capture_state(None, SimpleNamespace(name="camera-capture-state"))  # ended at 1000
        with mock.patch.object(natter_account.time, "monotonic", return_value=1000.0 + GRANT_REUSE - 1):
            page.stop_media("camera")
        self.assertEqual(page.fresh, 1)
        with mock.patch.object(natter_account.time, "monotonic", return_value=1000.0 + GRANT_REUSE + 1):
            page.stop_media("camera")
        self.assertEqual(page.fresh, 1)

    def test_backstop_is_edge_triggered(self):
        page = self.page(camera=False)
        notify = SimpleNamespace(name="camera-capture-state")
        page.view.state["camera"] = State.ACTIVE
        page._on_capture_state(None, notify)
        page._on_capture_state(None, notify)  # NONE's own notify, still reading active
        self.assertEqual(page.view.set_to, [("camera", State.NONE)])
        page.view.state["camera"] = State.NONE
        page._on_capture_state(None, notify)
        page.view.state["camera"] = State.MUTED
        page._on_capture_state(None, notify)
        self.assertEqual(page.view.set_to, [("camera", State.NONE)] * 2)

    def test_backstop_leaves_what_may_capture(self):
        page = self.page(microphone=False)
        page.view.state["camera"] = State.ACTIVE
        page._on_capture_state(None, SimpleNamespace(name="camera-capture-state"))
        self.assertEqual(page.view.set_to, [])
        blocked = self.page("no")  # the desktop's no counts as off
        blocked.view.state["camera"] = State.ACTIVE
        blocked._on_capture_state(None, SimpleNamespace(name="camera-capture-state"))
        self.assertEqual(blocked.view.set_to, [("camera", State.NONE)])

    def test_stopping_arms_the_backstop_only_once_its_none(self):
        page = self.page(camera=False)
        page.view.state["camera"] = State.ACTIVE
        page.stop_media("camera")
        page._on_capture_state(None, SimpleNamespace(name="camera-capture-state"))  # still active
        self.assertEqual(page.view.set_to, [("camera", State.NONE)])

    def test_never_muted(self):
        source = inspect.getsource(natter_account)
        self.assertNotIn("MediaCaptureState.MUTED", source)


class OnTest(PageTest):
    def test_reloads_only_after_a_refusal(self):
        page = self.page(camera=False)
        page.media_allowed_again("camera")
        self.assertEqual(page.reloads, 0)
        self.ask(page, MediaRequest(False, True))
        page.app.settings["camera"] = True
        page.media_allowed_again("camera")
        self.assertEqual(page.reloads, 1)

    def test_not_in_a_call(self):
        page = self.page(camera=False, calling=True)
        self.ask(page, MediaRequest(False, True))
        page.app.settings["camera"] = True
        page.media_allowed_again("camera")
        self.assertEqual(page.reloads, 0)  # WhatsApp's own buttons still reach Natter

    def test_not_while_still_refused(self):
        page = self.page("no")
        self.ask(page, MediaRequest(False, True))
        page.media_allowed_again("camera")
        self.assertEqual(page.reloads, 0)

    def test_the_other_kinds_refusal_doesnt_count(self):
        page = self.page(microphone=False)
        self.ask(page, MediaRequest(True, False))
        page.media_allowed_again("camera")
        self.assertEqual(page.reloads, 0)


class LoseMediaTest(PageTest):
    def test_off_and_a_new_page(self):
        page = self.page()
        page.lose_media()
        self.assertFalse(page.view.media_stream)
        self.assertEqual(page.reloads, 1)

    def test_after_the_call(self):
        page = self.page(calling=True)
        page.lose_media()
        self.assertFalse(page.view.media_stream)
        self.assertEqual(page.reloads, 0)
        self.assertTrue(page._lose_media_after_call)


# ---- The app's side (app.py) ---------------------------------------------------------------------


class Action:
    def __init__(self):
        self.states = []

    def set_state(self, value):
        self.states.append(value.unpack())


class Window:
    def __init__(self, visible=True):
        self.toasts, self.visible = [], visible

    def toast(self, title, *rest, **_kwargs):
        self.toasts.append(title)

    def get_visible(self):
        return self.visible


class FakeAccount:
    def __init__(self, name, capturing=()):
        self.name, self.capturing, self.done = name, set(capturing), []

    def stop_media(self, kind):
        self.done.append(("off", kind))
        return kind in self.capturing

    def media_allowed_again(self, kind):
        self.done.append(("on", kind))

    def lose_media(self):
        self.done.append(("lost",))


class App:
    """A Natter as the switches see it: Natter's own code, on stand-in parts."""

    def __init__(self, test, accounts=(), window=True, gate=None):
        self.settings = settings(test)
        self.accounts, self.window = list(accounts), Window() if window else None
        self.actions = {kind: Action() for kind in media.KINDS}
        self.calls = SimpleNamespace(media=[], set_media=lambda *values: self.calls.media.append(values))
        self.desktop_media = SimpleNamespace(answer=gate, sandboxed=False, present=True, lockdown=False,
                                             refreshed=0, refresh=lambda: None)
        self.media_lost, self.microphone_present, self.camera_fallback = False, None, False
        self._media_watchers, self._media_watch_next, self._asking_camera = {}, 0, False
        self._camera_gate_was, self._quitting, self.told = None, False, []
        self.settings.connect(lambda key, value: Natter._on_setting(self, key, value))

    def __getattr__(self, name):
        return types.MethodType(getattr(Natter, name), self)

    def lookup_action(self, name):
        return self.actions.get(name)

    def tell(self, *args):
        self.told.append(args)


class AppTest(unittest.TestCase):
    def test_off_reaches_every_account_and_says_so_only_when_capturing(self):
        accounts = [FakeAccount("Personal", ["camera"]), FakeAccount("Work")]
        app = App(self, accounts)
        app.settings["camera"] = False
        self.assertEqual([a.done for a in accounts], [[("off", "camera")], [("off", "camera")]])
        self.assertEqual(app.actions["camera"].states, [False])
        self.assertEqual(app.window.toasts, ["Camera turned off. WhatsApp's video has stopped (Personal)"])
        self.assertEqual(app.calls.media, [(True, False)])
        app.settings["microphone"] = False
        self.assertEqual(len(app.window.toasts), 1)  # nothing was using the microphone

    def test_one_account_isnt_named(self):
        app = App(self, [FakeAccount("Personal", ["microphone"])])
        app.settings["microphone"] = False
        self.assertEqual(app.window.toasts, ["Microphone turned off. WhatsApp can't hear you"])

    def test_on_lets_whatsapp_ask_again(self):
        accounts = [FakeAccount("Personal")]
        app = App(self, accounts)
        app.settings["camera"] = False
        app.settings["camera"] = True
        self.assertEqual(accounts[0].done, [("off", "camera"), ("on", "camera")])
        self.assertEqual(app.actions["camera"].states, [False, True])
        self.assertEqual(app.window.toasts, [])

    def test_watchers_hear_of_it(self):
        app = App(self)
        heard = []
        handle = app.watch_media(lambda: heard.append(1))
        app.settings["camera"] = False
        self.assertEqual(heard, [1])
        app.unwatch_media(handle)
        app.settings["camera"] = True
        self.assertEqual(heard, [1])

    def test_the_desktops_no_acts_like_off(self):
        accounts = [FakeAccount("Personal", ["camera"])]
        app = App(self, accounts)
        app.desktop_media.answer = "no"
        app._on_desktop_media()
        self.assertEqual(accounts[0].done, [("off", "camera")])
        self.assertEqual(app.calls.media, [(True, False)])  # Chromium never asks the portal
        app.desktop_media.answer = "yes"
        app._on_desktop_media()
        self.assertEqual(accounts[0].done, [("off", "camera"), ("on", "camera")])
        app._on_desktop_media()  # nothing new
        self.assertEqual(len(accounts[0].done), 2)

    def test_while_asking_again_the_answers_on_the_way_dont_count(self):
        accounts = [FakeAccount("Personal")]
        app = App(self, accounts)
        app.desktop_media.answer = "no"
        app._on_desktop_media()
        app._asking_camera = True
        app.desktop_media.answer = "ask"  # forgotten, the dialog still up
        app._on_desktop_media()
        self.assertEqual(accounts[0].done, [("off", "camera")])
        app._asking_camera = False
        app.desktop_media.answer = "yes"
        app._on_desktop_media()
        self.assertEqual(accounts[0].done, [("off", "camera"), ("on", "camera")])

    def test_the_gate_is_native_only(self):
        app = App(self, gate="no")
        self.assertEqual(app.camera_gate(), "no")
        app.desktop_media.sandboxed = True
        self.assertIsNone(app.camera_gate())  # the portal keeps it there
        app.desktop_media = None
        self.assertIsNone(app.camera_gate())

    def test_media_state(self):
        app = App(self, gate="no")
        app.microphone_present = False
        self.assertEqual(app.media_state("camera"), {"on": True, "desktop": "no", "present": True, "lockdown": False,
                                                     "sandboxed": False, "restart": False})
        self.assertEqual(app.media_state("microphone"), {"on": True, "desktop": None, "present": False,
                                                         "lockdown": False, "sandboxed": False, "restart": False})
        app.media_lost = True
        self.assertEqual((app.media_state("camera")["present"], app.media_state("camera")["restart"]), (False, True))
        self.assertEqual(device_subtitle("camera", **app.media_state("camera")), f"Restart {NAME} to use it")

    def test_allow_media_only_turns_on(self):
        source = inspect.getsource(Natter.do_startup)
        self.assertIn('"allow-media"', source)
        self.assertIn("__setitem__(v.get_string(), True)", source)
        for kind in media.KINDS:
            self.assertIn(kind, media.KINDS)

    def test_the_anchor_gone(self):
        accounts = [FakeAccount("Personal")]
        app = App(self, accounts)
        heard = []
        app.watch_media(lambda: heard.append(1))
        with mock.patch.object(natter_app, "in_sandbox", return_value=False):
            app._on_anchor_gone(None, SimpleNamespace(value_nick="crashed"))
        self.assertTrue(app.media_lost)
        self.assertEqual(accounts[0].done, [("lost",)])
        self.assertEqual(heard, [1])
        self.assertEqual(len(app.window.toasts), 1)
        self.assertIn(NAME, app.window.toasts[0])
        app._on_anchor_gone(None, SimpleNamespace(value_nick="crashed"))
        self.assertEqual(accounts[0].done, [("lost",)])  # once

    def test_the_anchor_gone_in_the_flatpak_or_quitting(self):
        accounts = [FakeAccount("Personal")]
        app = App(self, accounts)
        with mock.patch.object(natter_app, "in_sandbox", return_value=True):
            app._on_anchor_gone(None, SimpleNamespace(value_nick="crashed"))
        app._quitting = True
        with mock.patch.object(natter_app, "in_sandbox", return_value=False):
            app._on_anchor_gone(None, SimpleNamespace(value_nick="terminated-by-api"))
        self.assertFalse(app.media_lost)
        self.assertEqual(accounts[0].done, [])

    def test_ask_again_one_at_a_time(self):
        app = App(self)
        answers = []
        app._asking_camera = True
        with mock.patch.object(media, "private_bus", side_effect=AssertionError("asked twice")):
            app.ask_camera_again(answers.append)
        self.assertEqual(answers, [None])

    def test_ask_again_reads_the_answer(self):
        app = App(self)
        closed, answers, asked = [], [], []
        bus = SimpleNamespace(close=lambda *args: closed.append(True))
        app.desktop_media.asked = asked.append
        with mock.patch.object(media, "private_bus", return_value=bus), \
                mock.patch.object(media, "ask_camera_again", lambda b, done: done("yes")):
            app.ask_camera_again(answers.append)
        self.assertEqual((answers, asked, closed, app._asking_camera), (["yes"], ["yes"], [True], False))


class DebugInfoTest(unittest.TestCase):
    def test_lines(self):
        state = {"on": True, "desktop": "yes", "present": True, "lockdown": False, "sandboxed": False, "restart": False}
        self.assertEqual(media_debug("camera", state, True), "Camera: switch on, desktop yes, present yes, fallback on")
        self.assertEqual(media_debug("microphone", dict(state, desktop=None)), "Microphone: switch on")
        self.assertEqual(media_debug("camera", dict(state, on=False, desktop=None, present=None)),
                         "Camera: switch off, desktop unknown, present unknown, fallback off")
        self.assertEqual(media_debug("camera", dict(state, present=False, restart=True, lockdown=True), True),
                         "Camera: switch on, desktop yes, present no (restart needed), fallback on, "
                         "turned off for all apps")

    def test_in_the_debug_info(self):
        info = debug_info("in the page", [], False, ["Camera: switch on", "Microphone: switch off"])
        self.assertTrue(info.endswith("Camera: switch on\nMicrophone: switch off\n"))
        self.assertNotIn("Camera", debug_info("in the page", [], False))


# ---- udev in WebKit's sandbox (app.py _make_web_context) ------------------------------------------


class Log(list):
    def thing(self, name):
        log = self

        class Thing:
            def __init__(self, *args, **kwargs):
                log.append((name, args, kwargs))

            def __getattr__(self, method):
                return lambda *args: log.append((f"{name}.{method}", args)) or 0.9

        return Thing


def web_context(test, calls_in_page=True, sandboxed=False, udev=True):
    log = Log()
    webkit = SimpleNamespace(
        WebContext=log.thing("WebContext"), WebView=log.thing("WebView"),
        MemoryPressureSettings=SimpleNamespace(new=lambda: log.thing("MemoryPressureSettings")()),
        NetworkSession=SimpleNamespace(new_ephemeral=lambda: "ephemeral"),
        CacheModel=SimpleNamespace(DOCUMENT_BROWSER="document", WEB_BROWSER="browser"))
    webkit.WebContext.get_default = staticmethod(lambda: log.thing("default WebContext")())
    app = SimpleNamespace(calls_in_page=calls_in_page, camera_fallback=False, _on_anchor_gone=lambda *a: None)
    real_isdir = os.path.isdir
    with mock.patch.object(natter_app, "WebKit", webkit), \
            mock.patch.object(natter_app, "in_sandbox", return_value=sandboxed), \
            mock.patch.object(natter_app.os.path, "isdir", lambda path: udev if path == UDEV_DATA else real_isdir(path)), \
            mock.patch.dict(os.environ, {"JSC_useSharedArrayBuffer": "1"}):
        Natter._make_web_context(app)
    return app, log


class UdevTest(unittest.TestCase):
    def test_before_any_web_view_and_read_only(self):
        app, log = web_context(self)
        names = [entry[0] for entry in log]
        self.assertIn("WebContext.add_path_to_sandbox", names)
        self.assertEqual(log[names.index("WebContext.add_path_to_sandbox")][1], ("/run/udev/data", True))
        self.assertLess(names.index("WebContext.add_path_to_sandbox"), names.index("WebView"))
        self.assertLess(names.index("WebContext"), names.index("WebContext.add_path_to_sandbox"))
        self.assertTrue(app.camera_fallback)
        # The anchor stays, watched.
        self.assertIn(("WebView.connect", ("web-process-terminated", app._on_anchor_gone)), log)

    def test_only_natively_and_for_calls_in_the_page(self):
        for setup in ({"sandboxed": True}, {"calls_in_page": False}, {"udev": False}):
            app, log = web_context(self, **setup)
            self.assertNotIn("WebContext.add_path_to_sandbox", [entry[0] for entry in log], setup)
            self.assertFalse(app.camera_fallback)
        _app, log = web_context(self, sandboxed=True)
        self.assertIn("WebView", [entry[0] for entry in log])  # the anchor's still made in the Flatpak

    def test_the_system_bus_stays_hidden(self):
        self.assertIn("hide_system_bus", (ROOT / "natter" / "__main__.py").read_text())


# ---- The call window (calls.py, callwindow.py) ------------------------------------------------------


class CallWindowTest(unittest.TestCase):
    def test_started_with_the_switches(self):
        window = CallWindow(Path("/nonexistent"), microphone=False, camera=True)
        started = []

        class Launcher:
            def setenv(self, *args):
                pass

            def unsetenv(self, *args):
                pass

            def spawnv(self, argv):
                started.append(argv)
                raise GLib.Error("not in a test")

        with mock.patch("natter.calls.Gio.SubprocessLauncher.new", lambda flags: Launcher()):
            window.start()
        self.assertIn("--no-microphone", started[0])
        self.assertNotIn("--no-camera", started[0])

    def test_told_when_they_change(self):
        window = CallWindow(Path("/nonexistent"))
        sent = []
        window._send_now = sent.append
        window.set_media(True, True)
        self.assertEqual(sent, [])  # nothing new
        window.set_media(True, False)
        self.assertEqual(sent, [])  # not running: it hears at start
        self.assertFalse(window.camera)
        window.process = object()  # starting, not listening yet
        window.set_media(False, False)
        window.set_media(True, False)
        self.assertEqual(window.queue, [{"cmd": "media", "microphone": True, "camera": False}])
        window.connection = object()
        window.set_media(True, True)
        self.assertEqual(sent, [{"cmd": "media", "microphone": True, "camera": True}])

    def test_permissions_follow_the_switches(self):
        try:
            from natter.callwindow import allowed
        except ImportError:
            self.skipTest("needs Qt WebEngine for Python")
        for microphone in (True, False):
            for camera in (True, False):
                self.assertEqual(allowed("MediaAudioCapture", microphone, camera), microphone)
                self.assertEqual(allowed("MediaVideoCapture", microphone, camera), camera)
                self.assertEqual(allowed("MediaAudioVideoCapture", microphone, camera), microphone and camera)
                # Screen sharing and the rest as before.
                for other in ("DesktopVideoCapture", "DesktopAudioVideoCapture", "Notifications"):
                    self.assertTrue(allowed(other, microphone, camera))

    def test_no_longer_grants_everything(self):
        source = textwrap.dedent(inspect.getsource(__import__("natter.callwindow", fromlist=["Page"]).Page._permission)) \
            if _has_qt() else ""
        if not source:
            self.skipTest("needs Qt WebEngine for Python")
        self.assertIn("allows(", source)

    def test_the_app_passes_them_on(self):
        source = inspect.getsource(Natter._start_window_services)
        self.assertIn("microphone=microphone, camera=camera", source)


def _has_qt() -> bool:
    try:
        import natter.callwindow  # noqa: F401
    except ImportError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
