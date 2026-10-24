"""What the reports of October 2026 asked of calls (rtc.py, account.py, app.py): the page's
packets go out from a thread of their own, not GLib's main loop; every time Natter loads a page
again or ends it, one log line says why (and whether a call was on); a page WhatsApp loads again
by itself is logged too; WebKit's network process stopping during a call reloads the page only
once the call is over; a lost network process is one that's really gone; a ringing call's
notification is known in more languages than English, and logged with whether Natter shows it.
Natter's own code on stand-in parts; no network beyond this computer's own addresses."""

import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import types
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import WebKit

from natter import account as natter_account
from natter import rtc
from natter.account import FEATURES_OFF, RINGING, Account, say_reload
from natter.app import Natter, network_process_alive

Reason = WebKit.WebProcessTerminationReason


def usable() -> bool:
    try:
        return rtc.available() and rtc._gstwebrtc is not None
    except Exception:
        return False


class Out(unittest.TestCase):
    """What's printed, kept."""

    def setUp(self):
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))

    def lines(self, start="natter: page:"):
        return [line for line in self.out.getvalue().splitlines() if line.startswith(start)]


# ---- Packets out, from the Sender's thread ------------------------------------------------------

class FakeChannel:
    """A Channel as the Sender sees it: what it was asked to do, and from which thread."""

    def __init__(self, done, key=1):
        self.key, self.done, self.released = key, done, False

    def send_now(self, data=None, text=None):
        self.done.append((self.key, data if text is None else text, threading.current_thread().name))

    def close_now(self):
        self.done.append((self.key, "close", threading.current_thread().name))


class SenderTest(unittest.TestCase):
    def test_in_order_from_a_thread_of_its_own(self):
        done = []
        sender = rtc.Sender()
        channel = FakeChannel(done)
        for i in range(50):
            sender.put(channel, data=bytes([i]))
        sender.put(channel, text="hi")
        sender.put(channel, close=True)
        self.assertTrue(sender.idle())
        self.assertEqual([item for _key, item, _thread in done], [bytes([i]) for i in range(50)] + ["hi", "close"])
        self.assertEqual({thread for _key, _item, thread in done}, {"natter-rtc-send"})

    def test_what_waits_for_a_channel_let_go_is_dropped(self):
        done = []
        sender = rtc.Sender()
        kept, gone = FakeChannel(done, 1), FakeChannel(done, 2)
        with sender.lock:  # as Host._close holds it: nothing goes out meanwhile
            for i in range(5):
                sender.put(gone, data=b"x")
                sender.put(kept, data=b"y")
            gone.released = True
        self.assertTrue(sender.idle())
        self.assertEqual([key for key, _item, _thread in done], [1] * 5)

    def test_one_failure_doesnt_stop_the_rest(self):
        done = []
        sender = rtc.Sender()
        broken = FakeChannel(done, 1)
        broken.send_now = lambda data=None, text=None: 1 / 0
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sender.put(broken, data=b"x")
            sender.put(FakeChannel(done, 2), data=b"y")
            self.assertTrue(sender.idle())
        self.assertEqual([key for key, _item, _thread in done], [2])
        self.assertIn("natter: rtc: a packet couldn't be sent (ZeroDivisionError)", out.getvalue())

    @unittest.skipUnless(rtc.Gst is not None, "needs GStreamer")
    def test_the_main_loop_only_queues(self):
        # The page's packets, as account.py posts them: nothing is sent on the caller's thread.
        done = []
        host = rtc.Host()
        self.addCleanup(host.close)
        # A Channel's own send, on a stand-in for its GStreamer side (send_now).
        fake = SimpleNamespace(sent=0, host=host, released=False, send_now=FakeChannel(done, 7).send_now,
                               release=lambda: None)
        fake.send = lambda data=None, text=None: rtc.Channel.send(fake, data, text)
        host._channels[7] = fake
        payload = os.urandom(300) + os.urandom(200)
        host.post({"op": "sends", "data": base64.b64encode(payload).decode(), "cuts": [7, 300, 7, 200]})
        self.assertTrue(host.sender.idle())
        self.assertEqual([item for _key, item, _thread in done], [payload[:300], payload[300:]])
        self.assertNotIn(threading.current_thread().name, {thread for _key, _item, thread in done})
        self.assertEqual((fake.sent, host.packets), (2, 2))


class PackTest(unittest.TestCase):
    def test_the_same_as_json_writes_it(self):
        events = [{"pc": 1, "type": "ice", "value": "connected"},
                  {"ch": 3, "type": "message", "bytes": b"\x00\x01\xff" * 50},
                  {"ch": 3, "type": "message", "text": 'quote " and ü'},
                  {"ch": 4, "type": "message", "bytes": b""},
                  {"ch": 4, "type": "message", "bytes": os.urandom(1200)}]
        packed = rtc.pack(events)
        chunks = b"".join(event["bytes"] for event in events if "bytes" in event)
        items = [[event["ch"], len(event["bytes"])] if "bytes" in event else event for event in events]
        self.assertEqual(json.loads(packed), [base64.b64encode(chunks).decode(), items])

    def test_no_packets(self):
        self.assertEqual(json.loads(rtc.pack([{"pc": 1, "type": "negotiation-needed"}])),
                         ["", [{"pc": 1, "type": "negotiation-needed"}]])
        self.assertEqual(json.loads(rtc.pack([])), ["", []])


@unittest.skipUnless(rtc.Gst is not None and usable(), "needs GStreamer's WebRTC parts and libnice")
class LoopbackTest(Out):
    """Two Hosts joined on this computer's own addresses (test_rtc_host's Side)."""

    def setUp(self):
        super().setUp()
        from gi.repository import JavaScriptCore

        import test_rtc_host
        self.harness = test_rtc_host
        self.context = JavaScriptCore.Context.new()
        self.sides = []

    def tearDown(self):
        for side in self.sides:
            side.host.close()
        self.spin(lambda: False, 0.2)

    def spin(self, until, seconds=20.0):
        from gi.repository import GLib

        context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.002)
        return until()

    def connected(self):
        caller, answerer = self.harness.Side(self.context), self.harness.Side(self.context)
        self.sides += [caller, answerer]
        self.harness.HostTest.call(self, caller, answerer)
        return caller, answerer

    def test_packets_arrive_whole_and_in_order(self):
        caller, answerer = self.connected()
        got = []
        batch = answerer.batch

        def kept(text):
            if text:
                data, items = json.loads(text)
                data, at = base64.b64decode(data), 0
                for item in items:
                    if isinstance(item, list):
                        got.append(data[at:at + item[1]])
                        at += item[1]
            return batch(text)
        answerer.batch = kept
        sent = [bytes([i]) * (100 + i) for i in range(40)]
        for first in range(0, 40, 4):
            group = sent[first:first + 4]
            cuts = [x for packet in group for x in (1, len(packet))]
            caller.host.post({"op": "sends", "data": base64.b64encode(b"".join(group)).decode(), "cuts": cuts})
        # The channel is unordered and unreliable, but on this computer's own addresses nothing is lost.
        self.assertTrue(self.spin(lambda: len(got) == len(sent), 10), f"{len(got)} of {len(sent)} arrived")
        self.assertEqual(sorted(got), sorted(sent))
        self.assertIn("natter: rtc: a call is on (first sign: the page made a call's channel)", self.out.getvalue())

    def test_closing_with_packets_still_queued(self):
        # The page goes (a reload) while packets wait for the Sender: they're dropped, nothing breaks.
        caller, _answerer = self.connected()
        connection = caller.host._connections[1]
        blob = base64.b64encode(os.urandom(1000)).decode()
        for _ in range(300):
            caller.host.post({"op": "sends", "data": blob, "cuts": [1, 1000]})
        caller.host.close()
        self.assertTrue(caller.host.sender.idle(5))
        from gi.repository import Gst
        self.assertEqual(connection.pipeline.get_state(0)[1], Gst.State.NULL)
        self.assertEqual(caller.host._channels, {})


@unittest.skipUnless(rtc.Gst is not None, "needs GStreamer")
class CallLineTest(Out):
    """A call's lines: its first sign when it comes on, and whether it ever connected."""

    def spin(self, seconds=0.05):
        from gi.repository import GLib

        context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.002)

    def test_a_ring_that_never_connected(self):
        host = rtc.Host(lambda _on: None)
        self.addCleanup(host.close)
        host.ring(0.05, why="a ringing notification")
        self.spin(0.2)
        self.assertEqual(self.lines("natter: rtc:"), ["natter: rtc: a call is on (first sign: a ringing notification)",
                                                       "natter: rtc: the call is over, never connected"])

    def test_one_that_connected(self):
        host = rtc.Host(lambda _on: None)
        self.addCleanup(host.close)
        channel = SimpleNamespace(key=7, channel=SimpleNamespace(get_property=lambda _name: "call"))
        host._channels[7] = channel
        host._count(channel, True)
        del host._channels[7]
        with mock.patch.object(rtc, "GRACE", 0.05):
            host._count(channel, False)
        self.spin(0.2)
        self.assertEqual(self.lines("natter: rtc:"),
                         ["natter: rtc: a call is on (first sign: the far side opened a call's channel)",
                          "natter: rtc: the call is over"])


# ---- Every reload or end, one line ----------------------------------------------------------------

class Timers:
    SOURCE_REMOVE, SOURCE_CONTINUE = False, True

    def __init__(self):
        self.added = []

    def timeout_add_seconds(self, seconds, run, *args):
        self.added.append((seconds, run, args))
        return len(self.added)

    def source_remove(self, _timer):
        pass

    def idle_add(self, run, *args):
        self.added.append((0, run, args))
        return len(self.added)

    def getenv(self, _name):
        return None

    def run_all(self):
        while self.added:
            _seconds, run, args = self.added.pop(0)
            run(*args)


class View:
    def __init__(self):
        self.loaded, self.terminated, self.responsive = [], 0, True

    def load_uri(self, uri):
        self.loaded.append(uri)

    def terminate_web_process(self):
        self.terminated += 1

    def get_is_web_process_responsive(self):
        return self.responsive


class Page:
    """An Account as its reload paths see it: Account's own code, on stand-in parts."""

    def __init__(self, calling=False, accounts=1):
        self.app = SimpleNamespace(accounts=[None] * accounts, ended_for_memory=lambda: None)
        self.id, self.name, self.home = "a1", "Personal", "https://web.whatsapp.com/"
        self.view, self.calling = View(), calling
        self.rtc = SimpleNamespace(calling=calling)
        self.rtc.close = lambda: setattr(self.rtc, "calling", False)  # the call goes with the page
        self.active_chat, self._reopen, self._reopen_up = "", "", None
        self.ready, self.removed, self.crashing, self.memory_stopped, self.reloading = True, False, False, False, False
        self.failed = self.needs_link = None
        self.fresh_at = self.loaded_at = 0.0
        self._reload_timer, self._link, self._link_timer, self._page_timer = 0, None, 0, 0
        self._crashes, self._memory_ends, self._blank_reloads = [], [], []
        self._in_page, self._held_actions, self._fresh_held = 0, [], False
        self._allowed_at, self._ended_at, self.refused_since_load = {}, {}, {}
        self._fresh_after_call, self._held_media, self._chats_later = set(), [], []
        self._pending, self.recording, self.emitted = {}, False, []

    def __getattr__(self, name):
        return types.MethodType(getattr(Account, name), self)

    def emit(self, *signal):
        self.emitted.append(signal)

    def _watch_page(self, *_args):
        pass

    def _trusted(self, uri=None):
        return True

    def _apply_throttle(self):
        pass

    def set_recording(self, on):
        self.recording = on


class ReloadLineTest(Out):
    def setUp(self):
        super().setUp()
        self.timers = Timers()
        self.enterContext(mock.patch.object(natter_account, "GLib", self.timers))

    def test_the_line(self):
        say_reload(Page(), "a test")
        say_reload(Page(calling=True, accounts=2), "a test", ended=True)
        self.assertEqual(self.lines(), ["natter: page: reloaded because a test",
                                        "natter: page: ended because a test, during a call (Personal)"])

    def test_reload_and_fresh_page_say_why(self):
        page = Page()
        page.reload()
        page.reload("the network came back")
        page.fresh_page("of its memory: a page holds 4000 MB")
        self.assertEqual(self.lines(), ["natter: page: reloaded because Reload or Try Again was chosen",
                                        "natter: page: reloaded because the network came back",
                                        "natter: page: reloaded because of its memory: a page holds 4000 MB"])
        self.assertEqual(page.view.terminated, 1)

    def test_a_fresh_page_held_for_a_reply_keeps_its_reason(self):
        page = Page()
        page._in_page = 1
        page.fresh_page("the camera was switched off")
        self.assertEqual(self.lines(), [])
        page._in_page = 0
        page._release_held()
        self.assertEqual(self.lines(), ["natter: page: reloaded because the camera was switched off"])

    def test_never_a_fresh_page_in_a_call(self):
        page = Page(calling=True)
        page.fresh_page("of its memory")
        self.assertEqual((self.lines(), page.view.loaded, page.view.terminated), ([], [], 0))

    def test_a_crash(self):
        page = Page()
        page._on_crash(None, Reason.CRASHED)
        self.assertEqual(self.lines(), [])  # said when it loads
        self.timers.run_all()
        self.assertEqual(self.lines(), ["natter: page: reloaded because it stopped (crashed)"])
        self.assertEqual(page.view.loaded, [page.home])

    def test_a_crash_during_a_call(self):
        page = Page(calling=True)
        page._on_crash(None, Reason.CRASHED)
        self.timers.run_all()
        self.assertEqual(self.lines(), ["natter: page: reloaded because it stopped (crashed), during a call"])

    def test_ended_for_its_memory(self):
        page = Page()
        page.app.ended_for_memory = lambda: "the guard"
        page._on_crash(None, Reason.CRASHED)
        self.timers.run_all()
        page._on_crash(None, Reason.CRASHED)
        self.assertEqual(self.lines(), ["natter: page: reloaded because it was ended for its memory (the guard)",
                                        "natter: page: ended because it was ended for its memory (the guard) "
                                        "again: not reloading"])

    def test_a_blank_page(self):
        page = Page()
        page.ready = False
        page._reload_blank("came back empty")
        self.assertEqual(self.lines(), ["natter: page: reloaded because it came back empty"])

    def test_quitting_and_removing(self):
        page = Page()
        page._end_link = lambda: None
        page.end_page()
        self.assertEqual(self.lines(), ["natter: page: ended because Natter Den is quitting"])

    def test_whatsapps_own_reload(self):
        page = Page()
        page._end_link = lambda: None
        page._own_load = True  # the first load: Natter's
        page._on_load_changed(None, WebKit.LoadEvent.COMMITTED)
        self.assertEqual(self.lines(), [])
        page._on_load_changed(None, WebKit.LoadEvent.COMMITTED)  # nothing of Natter's started it
        self.assertEqual(self.lines(), ["natter: page: reloaded because the page loaded itself again "
                                        "(WhatsApp's own reload)"])
        page.reload()
        page._on_load_changed(None, WebKit.LoadEvent.COMMITTED)
        self.assertEqual(len(self.lines()), 2)  # the reload's own line only

    def test_not_answering(self):
        page = Page(calling=True)
        page._unresponsive_at = None
        page.view.responsive = False
        page._on_responsive(page.view, None)
        page._on_responsive(page.view, None)  # said once
        page.view.responsive = True
        page._on_responsive(page.view, None)
        lines = self.lines()
        self.assertEqual(lines[0], "natter: page: not answering, during a call")
        self.assertRegex(lines[1], r"^natter: page: answering again after \d+\.\d s$")
        self.assertEqual(len(lines), 2)


class NetworkLostTest(Out):
    def setUp(self):
        super().setUp()
        self.timers = Timers()
        self.enterContext(mock.patch.object(natter_account, "GLib", self.timers))

    def test_not_during_a_call(self):
        page = Page(calling=True)
        page.network_lost()
        page.network_lost()  # every look until the call is over: said once
        self.assertEqual(page.view.terminated, 0)
        self.assertEqual(self.lines(), ["natter: page: WebKit's network process stopped during a call; "
                                        "reloading once the call is over"])
        page.calling = page.rtc.calling = False
        page._on_page_call(False)  # the call is over
        self.assertEqual(page.view.terminated, 1)
        self.timers.run_all()
        self.assertEqual(page.view.loaded, [page.home])
        self.assertEqual(self.lines()[1:], ["natter: page: reloaded because WebKit's network process stopped"])

    def test_without_a_call_at_once(self):
        page = Page()
        page.network_lost()
        self.assertEqual(page.view.terminated, 1)
        self.timers.run_all()
        self.assertEqual(self.lines(), ["natter: page: reloaded because WebKit's network process stopped"])

    def lost_in_a_call(self):
        page = Page(calling=True)
        page.network_lost()
        self.out.truncate(0)
        self.out.seek(0)
        return page

    def call_over(self, page):
        """What rtc.close leads to on the next idle: the Host says the call is over."""
        page.calling = page.rtc.calling = False
        page._on_page_call(False)
        self.timers.run_all()

    def test_a_crash_meanwhile_is_one_reload(self):
        page = self.lost_in_a_call()
        page._on_crash(None, Reason.CRASHED)
        self.call_over(page)
        self.assertEqual((page.view.loaded, page.view.terminated, len(page._crashes)), ([page.home], 0, 1))
        self.assertEqual(self.lines(), ["natter: page: reloaded because it stopped (crashed), during a call"])

    def test_ended_for_its_memory_meanwhile_keeps_that_reason(self):
        page = self.lost_in_a_call()
        page.app.ended_for_memory = lambda: "the guard"
        page._on_crash(None, Reason.CRASHED)
        self.call_over(page)
        self.assertEqual((page.view.loaded, page.view.terminated, page._crashes), ([page.home], 0, []))
        self.assertEqual(self.lines(), ["natter: page: reloaded because it was ended for its memory "
                                        "(the guard, during a call)"])

    def test_reload_meanwhile_is_a_new_process_once(self):
        page = self.lost_in_a_call()
        page.reload()
        self.call_over(page)
        self.assertEqual((page.view.loaded, page.view.terminated), ([page.home], 1))
        self.assertEqual(self.lines(), ["natter: page: reloaded because Reload or Try Again was chosen, "
                                        "during a call"])

    def test_a_call_that_only_starts_after_changes_nothing(self):
        page = Page()
        page._on_page_call(False)
        self.assertEqual(page.view.terminated, 0)


class NetworkProcessTest(Out):
    def proc(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def test_alive_or_gone(self):
        proc = self.proc()
        (proc / "10").mkdir()
        (proc / "10" / "comm").write_text("WebKitNetworkPr\n")
        (proc / "11").mkdir()
        (proc / "11" / "comm").write_text("bash\n")  # its pid went to something else
        (proc / "12" / "comm").mkdir(parents=True)  # can't be read now: still counts as there
        self.assertTrue(network_process_alive(10, proc))
        self.assertFalse(network_process_alive(11, proc))
        self.assertTrue(network_process_alive(12, proc))
        self.assertFalse(network_process_alive(13, proc))  # gone

    def test_a_look_that_finds_none_while_it_lives(self):
        lost = []
        fake = SimpleNamespace(_network_processes={101}, _network_looks=0, _quitting=False,
                               _last_look=time.monotonic(), _note_oom_kills=lambda events, now: None,
                               accounts=[SimpleNamespace(network_lost=lambda: lost.append(1))])
        with mock.patch("natter.app.network_processes", return_value=set()), \
                mock.patch("natter.app.network_process_alive", return_value=True):
            Natter._check_network_process(fake)
        self.assertEqual((lost, fake._network_processes), ([], {101}))
        with mock.patch("natter.app.network_processes", return_value={202}), \
                mock.patch("natter.app.network_process_alive", return_value=False):
            Natter._check_network_process(fake)
        self.assertEqual((lost, fake._network_processes), ([1], {202}))


# ---- Ringing --------------------------------------------------------------------------------------

class RingingTest(Out):
    def test_words(self):
        for text in ("Incoming voice call", "Incoming video call", "incoming call…", "Eingehender Sprachanruf",
                     "Eingehender Videoanruf", "Llamada de voz entrante", "Videollamada entrante",
                     "Appel vocal entrant", "Appel vidéo entrant", "Chiamata vocale in arrivo",
                     "Videochiamata in arrivo", "Chamada de voz recebida", "Recebendo chamada de vídeo",
                     "Inkomende spraakoproep", "Входящий аудиозвонок", "Входящий видеозвонок",
                     "Gelen sesli arama", "Gelen görüntülü arama", "Przychodzące połączenie głosowe",
                     "Panggilan suara masuk", "Simu ya sauti inayoingia", "مكالمة صوتية واردة",
                     "इनकमिंग वॉइस कॉल"):
            self.assertTrue(RINGING.fullmatch(text), text)
        for text in ("Missed voice call", "I had an incoming call earlier", "call me", "Photo", "llamada"):
            self.assertIsNone(RINGING.fullmatch(text), text)

    def page(self, notifications=True, paused=False, muted=False, visible=False):
        rang = []
        settings = mock.MagicMock()
        settings.__getitem__.side_effect = {"notifications": notifications}.__getitem__
        settings.notifications_paused.return_value = paused
        app = SimpleNamespace(settings=settings,
                              notifier=SimpleNamespace(muted={("a1", "tag"): time.time() + 60} if muted else {}),
                              window=SimpleNamespace(get_visible=lambda: visible))
        page = SimpleNamespace(app=app, id="a1", rtc=SimpleNamespace(ring=lambda why: rang.append(why)))
        return page, rang

    def test_logged_with_whether_its_shown(self):
        for options, said in (({}, "(window hidden); shown"),
                              ({"visible": True}, "(window shown); shown"),
                              ({"notifications": False}, "not shown: notifications are off"),
                              ({"paused": True}, "not shown: notifications are paused"),
                              ({"muted": True}, "not shown: that chat's notifications are muted")):
            page, rang = self.page(**options)
            Account._rang(page, "tag")
            self.assertEqual(rang, ["a ringing notification"])
            self.assertTrue(self.lines("natter: rtc:")[-1].startswith("natter: rtc: a ringing notification from the page"))
            self.assertIn(said, self.lines("natter: rtc:")[-1])

    def test_never_a_name(self):
        page, _rang = self.page()
        Account._rang(page, "Mum")
        self.assertNotIn("Mum", self.out.getvalue())


class ThrottlingTest(unittest.TestCase):
    def test_hidden_timers_never_slow_further(self):
        self.assertIn("HiddenPageDOMTimerThrottlingAutoIncreases", FEATURES_OFF)


if __name__ == "__main__":
    unittest.main()
