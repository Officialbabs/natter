"""A call as Natter's main loop sees it (app.py, account.py, rtc.py's Host): on from its first
sign, through a reconnect's moment without channels (a Wi-Fi roam), and what waits meanwhile.
A stall of that loop is a gap in the call's sound: every packet goes through it. Natter's own
code on stand-in parts; no network, no sound."""

import contextlib
import io
import tempfile
import time
import types
import unittest
import warnings
from pathlib import Path
from unittest import mock

from natter import rtc
from natter.account import RINGING, Account
from natter.app import Natter, held_by_page
from natter.memguard import MemoryGuard, MiB
from natter.settings import ChatIndex
from natter.system import LoopMeter

if rtc.Gst is not None:
    from gi.repository import GLib


def usable() -> bool:
    try:
        return rtc.available() and rtc._gstwebrtc is not None
    except Exception:
        return False


def no_upnp(webrtc) -> None:
    """libnice asks the router to open ports (UPnP) when it gathers: not from a test."""
    from test_rtc_host import no_upnp as off
    off(webrtc)


def spin(seconds=0.05):
    context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)


class Log:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *args: self.events.append(name)


class Window:
    def __init__(self, visible=True):
        self.visible = visible

    def get_visible(self):
        return self.visible


class Page:
    """An Account as the call sees it: a Host of its own, `calling` from it."""

    def __init__(self, app):
        self.rtc = rtc.Host(lambda on: app._on_page_call(self, on), lambda _on: app._apply_page_sound())
        self.muted = []
        self.view = types.SimpleNamespace(get_is_muted=lambda: None, set_is_muted=self.muted.append)

    @property
    def calling(self):
        return self.rtc.calling

    @property
    def in_call(self):
        return self.rtc.in_call


class App:
    calling = Natter.calling
    in_call = Natter.in_call
    _on_page_call = Natter._on_page_call
    _on_window_visible = Natter._on_window_visible
    _measure_call = Natter._measure_call
    _call_packets_now = Natter._call_packets_now
    _fresh_pages_for_memory = Natter._fresh_pages_for_memory

    def __init__(self):
        self.window, self.settings, self.memory_guard = Window(), {"light_background": True}, None
        self.reclaimer = types.SimpleNamespace(available=True, stops=0)
        self.reclaimer.stop = lambda: setattr(self.reclaimer, "stops", self.reclaimer.stops + 1)
        self.call_sound, self.written = Log(), []
        self.loop_meter = LoopMeter()
        self._reclaim_timer, self._hidden_at, self._call_ended_at, self._call_packets = 0, None, None, 0
        self._call_held, self._after_call, self._memory_waiting = {}, {}, None
        self._refresh_tray = self._check_media = self._apply_page_sound = lambda: None
        self._write_menu_chats = lambda: self.written.append("dock")
        self._reclaim = lambda: False
        self.accounts = [Page(self)]


@unittest.skipUnless(rtc.Gst is not None and usable(), "needs GStreamer's WebRTC parts")
class CallTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(mock.patch("natter.app.pages_held", lambda guard=None: 0))
        self.app = App()
        self.page = self.app.accounts[0]
        self.addCleanup(self.app.loop_meter.stop)
        self.addCleanup(self.page.rtc.close)

    def channel(self, key=7):
        channel = types.SimpleNamespace(key=key, channel=types.SimpleNamespace(get_property=lambda _name: "call"))
        channel.release = lambda: self.page.rtc._count(channel, False)
        self.page.rtc._channels[key] = channel
        return channel

    # ---- L2: the first sign of a call ----

    def test_the_reclaimer_stops_when_the_call_is_placed(self):
        # Natter's call button (start_call → Account.ringing → Host.ring): before anything connects.
        self.page.rtc.ring()
        spin()
        self.assertEqual(self.app.reclaimer.stops, 1)
        self.assertEqual(self.app.call_sound.events, ["start"])
        self.assertTrue(self.app.loop_meter.running)

    def test_the_reclaimer_stops_when_a_calls_channel_is_made(self):
        self.page.rtc.post({"op": "create", "pc": 1, "config": {"iceServers": []}})
        no_upnp(self.page.rtc._connections[1].webrtc)
        self.page.rtc.post({"op": "channel", "pc": 1, "ch": 1, "label": "call", "ordered": False})
        spin()
        self.assertFalse(self.page.rtc.connected)  # nothing open yet
        self.assertEqual(self.app.reclaimer.stops, 1)

    def test_a_ringing_calls_notification(self):
        for body in ("Incoming voice call", "Incoming video call", "\U0001F4DE Incoming call"):
            self.assertTrue(RINGING.fullmatch(body), body)
        for body in ("Incoming call? Call me back", "Missed voice call", "Photo"):
            self.assertFalse(RINGING.fullmatch(body), body)

    def test_do_not_disturb_keeps_a_ringing_call_muted(self):
        # WhatsApp's ringtone, or a message that reads like a ringing call's notification: muted.
        self.app.settings = types.SimpleNamespace(notifications_paused=lambda: True)
        self.app._apply_page_sound = types.MethodType(Natter._apply_page_sound, self.app)
        self.app.window, self.app.locked = None, False
        with mock.patch.object(rtc, "GRACE", 0.1):
            self.page.rtc.ring()
            spin()
            self.assertTrue(self.app.calling)
            self.assertEqual(self.page.muted[-1:], [True])
            channel = self.channel()
            self.page.rtc._count(channel, True)  # answered: the call's sound plays
            spin()
            self.assertEqual(self.page.muted[-1:], [False])
            self.page.rtc._count(channel, False)
            spin(0.02)
            self.assertEqual(self.page.muted[-1:], [False])  # the grace is still the call
            spin(0.2)
        self.assertEqual(self.page.muted[-1:], [True])

    def test_a_call_button_that_didnt_start_isnt_a_call(self):
        toasts, runs = [], []
        account = types.SimpleNamespace(ringing=lambda: runs.append("ring"), when_ready=lambda then: then(),
                                        run=lambda body, args, done: done(reason))
        fake = types.SimpleNamespace(calls_in_page=True, window=types.SimpleNamespace(toast=toasts.append),
                                     _held_while_locked=lambda again: False)
        for reason in (None, "no-chat", ""):
            Natter.start_call(fake, account, False)
        self.assertEqual(runs, ["ring"])
        self.assertEqual(toasts, ["The call didn't start", "Open a chat first, then call"])

    # ---- L1: the grace ----

    def test_a_roam_changes_nothing(self):
        with mock.patch.object(rtc, "GRACE", 0.3):
            channel = self.channel()
            self.page.rtc._count(channel, True)
            spin()
            self.app.window.visible = False
            self.page.rtc._count(channel, False)  # all its channels closed for a moment
            spin(0.1)
            self.page.rtc._count(self.channel(8), True)  # back, within the grace
            spin(0.4)
        self.assertEqual(self.app.call_sound.events, ["start"])  # never stopped
        self.assertEqual((self.app.reclaimer.stops, self.app._reclaim_timer, self.app.written), (1, 0, []))
        self.assertTrue(self.app.loop_meter.running)
        self.assertNotIn("natter: call:", self.out.getvalue())

    def test_after_the_grace_everything_comes_back(self):
        self.app.window.visible = False
        with mock.patch.object(rtc, "GRACE", 0.1):
            channel = self.channel()
            self.page.rtc._count(channel, True)
            spin()
            self.page.rtc._count(channel, False)
            spin(0.02)
            self.assertEqual(self.app.call_sound.events, ["start"])
            self.assertTrue(self.app.calling)
            spin(0.2)
        self.assertEqual(self.app.call_sound.events, ["start", "stop"])
        self.assertFalse(self.app.calling)
        self.assertNotEqual(self.app._reclaim_timer, 0)  # hidden: the Reclaimer is armed again
        GLib.source_remove(self.app._reclaim_timer)
        self.assertEqual(self.app.written, ["dock"])  # and the dock's entry written
        self.assertIn("natter: call: main loop late max ", self.out.getvalue())

    # ---- L3: what waits during a call ----

    def test_hiding_during_a_call_waits(self):
        self.page.rtc.ring()
        spin()
        self.app.window.visible = False
        self.app._on_window_visible(False)
        self.assertEqual((self.app._reclaim_timer, self.app.written), (0, []))
        self.app.window.visible = True

    def test_no_page_sizes_read_during_a_call(self):
        self.page.rtc.ring()
        spin()
        self.app.window.visible = False
        with mock.patch("natter.app.page_sizes", side_effect=AssertionError("read on the main loop")):
            self.assertFalse(self.app._fresh_pages_for_memory())
        self.app.window.visible = True

    # ---- L4: the line ----

    def test_one_line_at_the_end_with_the_packets(self):
        self.page.rtc.packets = 100  # an earlier call's
        self.page.rtc.ring()
        spin()
        self.page.rtc.packets += 42
        self.page.rtc.close()
        spin()
        lines = [line for line in self.out.getvalue().splitlines() if line.startswith("natter: call:")]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^natter: call: main loop late max \d+ ms, over 50 ms \d+ times, "
                                   r"over 100 ms \d+ times, packets 42$")


class LoopMeterTest(unittest.TestCase):
    def test_how_late_each_tick_came(self):
        clock, timers = [100.0], []
        meter = LoopMeter(clock=lambda: clock[0], later=lambda ms, tick: timers.append((ms, tick)) or 5,
                          cancel=timers.append)
        meter.start()
        ((ms, tick),) = timers
        self.assertEqual(ms, 20)
        for late in (0.001, 0.060, 0.130, 0.020):
            clock[0] += 0.020 + late
            self.assertTrue(tick())
        meter.stop()
        self.assertEqual(timers[-1], 5)
        self.assertFalse(meter.running)
        self.assertEqual(meter.line(42), "natter: call: main loop late max 130 ms, over 50 ms 2 times, "
                                         "over 100 ms 1 times, packets 42")

    def test_each_call_counts_afresh(self):
        clock = [0.0]
        meter = LoopMeter(clock=lambda: clock[0], later=lambda ms, tick: 1, cancel=lambda timer: None)
        meter.start()
        clock[0] += 0.2
        meter._tick()
        meter.stop()
        meter.start()
        self.assertEqual(meter.line(0), "natter: call: main loop late max 0 ms, over 50 ms 0 times, "
                                        "over 100 ms 0 times, packets 0")


class ChatsDuringACallTest(unittest.TestCase):
    """Chats seen during a call wait: each change is a write to disk (with fsync) on the main loop."""

    def page(self, folder):
        page = types.SimpleNamespace(chats=ChatIndex(Path(folder) / "chats.json"), calling=True,
                                     _chats_later=[], emitted=[])
        page.emit = page.emitted.append
        for name in ("_seen_chats", "_chats_after_call"):
            setattr(page, name, types.MethodType(getattr(Account, name), page))
        return page

    def test_saved_once_after_the_call_in_the_order_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            page = self.page(folder)
            with mock.patch("natter.account.time.time", return_value=1000.0):
                self.assertFalse(page._seen_chats([("Ann", 0), ("Bob", 1)]))
            with mock.patch("natter.account.time.time", return_value=1300.0):
                self.assertFalse(page._seen_chats([("Cy", 0)]))
            self.assertFalse((Path(folder) / "chats.json").exists())  # nothing written in the call
            page.calling = False
            with mock.patch.object(page.chats, "save", wraps=page.chats.save) as save:
                page._chats_after_call()
            self.assertEqual(save.call_count, 1)
            self.assertEqual([chat.name for chat in page.chats.recent()], ["Cy", "Ann", "Bob"])
            self.assertEqual(page.chats.chats["Bob"], 940.0)
            self.assertEqual(page.emitted, ["chats-changed"])
            self.assertEqual(page._chats_later, [])

    def test_out_of_a_call_at_once(self):
        with tempfile.TemporaryDirectory() as folder:
            page = self.page(folder)
            page.calling = False
            self.assertTrue(page._seen_chats([("Ann", 0)]))
            self.assertTrue((Path(folder) / "chats.json").exists())


class HeldByPageTest(unittest.TestCase):
    def test_the_guards_last_look_when_recent(self):
        guard = MemoryGuard(1 << 30, idle_add=lambda *args: None)
        guard._sizes = (time.monotonic(), {5: 300 * MiB})
        with mock.patch("natter.app.page_sizes", side_effect=AssertionError("read on the main loop")):
            self.assertEqual(held_by_page(guard), {5: 300 * MiB})
        guard._sizes = (time.monotonic() - 60, {5: 300 * MiB})  # the guard stopped looking
        with mock.patch("natter.app.page_sizes", return_value={5: 1}):
            self.assertEqual(held_by_page(guard), {5: 1})
            self.assertEqual(held_by_page(None), {5: 1})


if __name__ == "__main__":
    unittest.main()
