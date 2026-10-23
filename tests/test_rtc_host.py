"""Calls' data channels (rtc.py's Host) on real webrtcbins: two Hosts in this process, one the
page that calls and one the far side, joined on this computer's own addresses (no STUN or TURN
server, and libnice's UPnP off, so nothing leaves the computer). What the crash hunt of October
2026 found: a call that ended with the window hidden started a fresh page inside the Host's own
close (KeyError, and the call's webrtcbin left running in Natter for good); calls whose channel
the far side opened never counted as calls; and a channel with a protocol couldn't be made."""

import ctypes
import json
import time
import unittest
import warnings

from natter import rtc

if rtc.Gst is not None:
    from gi.repository import GLib, Gst, JavaScriptCore


def usable() -> bool:
    try:
        return rtc.available() and rtc._gstwebrtc is not None
    except Exception:
        return False


def no_upnp(webrtc) -> None:
    """libnice asks the router to open ports (UPnP) when it gathers: not from a test."""
    element = ctypes.pythonapi.PyCapsule_GetPointer(webrtc.__gpointer__, None)
    ice = rtc._object_property(element, "ice-agent")
    agent = rtc._object_property(ice, "agent")
    try:
        if agent:
            rtc._gobject.g_object_set(ctypes.c_void_p(agent), b"upnp", ctypes.c_int(0), None)
    finally:
        for pointer in (agent, ice):
            if pointer:
                rtc._gobject.g_object_unref(pointer)


class Reply:
    """What WebKit hands the page's waiting pull: the batch goes to its side on the main loop."""

    def __init__(self, side):
        self.side = side

    def return_value(self, value):
        GLib.idle_add(self.side.batch, value.to_string())


class Side:
    """One page's Host, its events read as rtc.js would: candidates passed to the other side."""

    def __init__(self, context, on_call=None):
        self.context, self.peer, self.calls, self.events = context, None, [], []
        self.host = rtc.Host(on_call or self.calls.append)
        self.host.pull(Reply(self), context)

    def batch(self, text):
        if text:
            _data, items = json.loads(text)
            for event in items:
                if isinstance(event, dict):
                    self.events.append(event)
                    if event.get("type") == "candidate" and event.get("candidate") and self.peer:
                        self.peer.host.call({"op": "candidate", "pc": 1, "candidate": event["candidate"],
                                             "sdpMid": event.get("sdpMid"),
                                             "sdpMLineIndex": event.get("sdpMLineIndex")}, lambda _r: None)
            if self.host._waiting is None and self.host._connections:
                self.host.pull(Reply(self), self.context)
        return False

    def create(self):
        self.host.post({"op": "create", "pc": 1, "config": {"iceServers": []}})
        no_upnp(self.host._connections[1].webrtc)

    def opened(self):
        return {event["ch"] for event in self.events
                if event.get("type") == "open" or (event.get("type") == "datachannel" and event.get("open"))}


@unittest.skipUnless(rtc.Gst is not None and usable(), "needs GStreamer's WebRTC parts and libnice")
class HostTest(unittest.TestCase):
    def setUp(self):
        # PyGObject asks asyncio for its loop policy on every iteration, deprecated in Python 3.14.
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        self.context = JavaScriptCore.Context.new()
        self.sides = []

    def tearDown(self):
        for side in self.sides:
            side.host.close()
        self.spin(lambda: False, 0.2)

    def side(self, on_call=None):
        made = Side(self.context, on_call)
        self.sides.append(made)
        return made

    def spin(self, until, seconds=20.0):
        context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.005)
        return until()

    def call(self, caller, answerer, protocol=None):
        """The caller's page opens a call channel and offers; the far side answers. Returns once
        both sides have it open."""
        caller.peer, answerer.peer = answerer, caller
        caller.create()
        answerer.create()
        channel = {"op": "channel", "pc": 1, "ch": 1, "label": "call", "ordered": False, "maxRetransmits": 0}
        if protocol:
            channel["protocol"] = protocol
        caller.host.post(channel)
        done = {}

        def answered(reply):
            answerer.host.call({"op": "set-local", "pc": 1, "type": "answer", "sdp": reply["sdp"]},
                               lambda _r: caller.host.call({"op": "set-remote", "pc": 1, "type": "answer",
                                                            "sdp": reply["sdp"]}, lambda _r: done.setdefault("ok", 1)))

        def offered(reply):
            caller.host.call({"op": "set-local", "pc": 1, "type": "offer", "sdp": reply["sdp"]},
                             lambda _r: answerer.host.call({"op": "set-remote", "pc": 1, "type": "offer",
                                                            "sdp": reply["sdp"]},
                                                           lambda _r: answerer.host.call({"op": "answer", "pc": 1},
                                                                                         answered)))
        caller.host.call({"op": "offer", "pc": 1}, offered)
        self.assertTrue(self.spin(lambda: caller.opened() and answerer.opened()), "the call never connected")

    def test_the_far_sides_channel_counts_as_a_call(self):
        # webrtcbin opens it on its own thread before the Host takes it: its "on-open" is gone by then.
        caller, answerer = self.side(), self.side()
        self.call(caller, answerer)
        self.assertTrue(self.spin(lambda: caller.calls and answerer.calls))
        self.assertTrue(answerer.host.calling)
        self.assertEqual((caller.calls, answerer.calls), ([True], [True]))

    def test_a_call_ending_in_a_fresh_page(self):
        # The window hidden, the call over: the page's close of its connection ends the call, and
        # what that sets off (Account.fresh_page) closes the whole Host.
        events = []
        caller = self.side(on_call=lambda calling: (events.append(calling),
                                                    not calling and caller.host.close()))
        answerer = self.side()
        self.call(caller, answerer)
        self.assertTrue(self.spin(lambda: events == [True]))
        connection = caller.host._connections[1]
        caller.host.post({"op": "close", "pc": 1})  # raised KeyError before
        self.assertTrue(self.spin(lambda: events == [True, False]))
        state = connection.pipeline.get_state(0)[1]
        self.assertEqual(state, Gst.State.NULL)  # the call's webrtcbin stopped, not left running
        self.assertEqual(connection._ids, [])
        self.assertEqual((caller.host._channels, caller.host._connections), ({}, {}))

    def test_closing_the_host_in_a_call(self):
        # The page went away mid-call: what on_call(False) sets off may close it again.
        caller = self.side(on_call=lambda calling: not calling and caller.host.close())
        answerer = self.side()
        self.call(caller, answerer)
        connection = caller.host._connections[1]
        caller.host.close()  # raised RuntimeError (the channels changed while it went through them)
        self.spin(lambda: False, 0.3)
        self.assertEqual(connection.pipeline.get_state(0)[1], Gst.State.NULL)
        self.assertFalse(caller.host.calling)

    def test_a_channel_with_a_protocol(self):
        caller, answerer = self.side(), self.side()
        self.call(caller, answerer, protocol='wa "proto" ü')
        self.assertEqual(caller.host._channels[1].channel.get_property("protocol"), 'wa "proto" ü')
        far = next(event for event in answerer.events if event.get("type") == "datachannel")
        self.assertEqual(far["init"]["protocol"], 'wa "proto" ü')


@unittest.skipUnless(rtc.Gst is not None and usable(), "needs GStreamer's WebRTC parts and libnice")
class CountTest(unittest.TestCase):
    def test_on_call_is_told_from_the_main_loop(self):
        # Never from inside the Host: what it starts may close the Host under its feet.
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        told = []
        host = rtc.Host(told.append)
        channel = type("Channel", (), {})()
        channel.key = 7
        channel.channel = type("Gst", (), {"get_property": lambda _self, _name: "call"})()
        host._channels[7] = channel
        host._count(channel, True)
        self.assertTrue(host.calling)
        self.assertEqual(told, [])
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not told and time.monotonic() < deadline:
            context.iteration(False)
        self.assertEqual(told, [True])
        del host._channels[7]
        host._count(channel, False)
        while told == [True] and time.monotonic() < deadline + 5:
            context.iteration(False)
        self.assertEqual(told, [True, False])


if __name__ == "__main__":
    unittest.main()
