"""Other apps' sound pauses while a voice message is recorded (mediapause.py): the bridge's
"recording" signal (run here in JavaScriptCore against stand-ins for getUserMedia and its
tracks), the pausing and playing again on a fake session bus (never the real one: no player on
this machine is asked anything), the setting and its switch, the wiring through Account and the
app, and the log's one line, counts only."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("JavaScriptCore", "6.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, JavaScriptCore, WebKit  # noqa: E402

from natter import APP_ID, account as natter_account, prefs  # noqa: E402
from natter.account import parse_message  # noqa: E402
from natter.app import Natter  # noqa: E402
from natter.mediapause import (FORGET, MPRIS, PLAYER, QUIT_WAIT_MS, RESUME_DELAY_MS, TIMEOUT_MS, Bus,  # noqa: E402
                               VoicePause, log_line, natter_family, own_name)
from natter.settings import DEFAULTS, Settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = (ROOT / "natter" / "bridge.js").read_text()
START = "  // ---- A voice message being recorded"
END = "  // ---- Watching the page"

# ---- The bridge's signal ----------------------------------------------------------------------

# Just enough of a page: a clock with timers, post(), calling(), and getUserMedia handing out
# tracks that stop (WhatsApp's stop()) or end (WebKit ending them: the microphone switched off).
FAKES = """
var window = globalThis;
let clock = 0;
let timers = [];
let nextTimer = 1;
function setTimeout(run, ms) { const id = nextTimer++; timers.push({ id, at: clock + ms, run, every: 0 }); return id; }
function setInterval(run, ms) { const id = nextTimer++; timers.push({ id, at: clock + ms, run, every: ms }); return id; }
function clearTimeout(id) { timers = timers.filter((timer) => timer.id !== id); }
const clearInterval = clearTimeout;
function advance(ms) {
  const end = clock + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const due = timers.find((timer) => timer.at <= end);
    if (!due) break;
    clock = due.at;
    if (due.every) due.at += due.every;
    else timers = timers.filter((timer) => timer !== due);
    due.run();
  }
  clock = end;
}
const posted = [];
const post = (message) => posted.push(message);
let inCall = false;
function calling() { return inCall; }
class MediaStreamTrack {
  constructor(kind) { this.kind = kind; this.readyState = "live"; this.listeners = {}; }
  addEventListener(type, run) { (this.listeners[type] = this.listeners[type] || []).push(run); }
  stop() { this.readyState = "ended"; }
  end() { this.readyState = "ended"; (this.listeners.ended || []).forEach((run) => run()); }
}
window.MediaStreamTrack = MediaStreamTrack;
class MediaStream {
  constructor(tracks) { this.tracks = tracks; }
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks.filter((track) => track.kind === "audio"); }
}
const streams = [];
window.navigator = { mediaDevices: { getUserMedia(constraints) {
  const tracks = [];
  if (constraints.audio) tracks.push(new MediaStreamTrack("audio"));
  if (constraints.video) tracks.push(new MediaStreamTrack("video"));
  const stream = new MediaStream(tracks);
  return Promise.resolve(stream).then((s) => { streams.push(s); return s; });
} } };
function open(constraints) { window.navigator.mediaDevices.getUserMedia(constraints || { audio: true }); }
const track = (n) => streams[n].getTracks()[0];
"""


def section() -> str:
    return BRIDGE[BRIDGE.index(START):BRIDGE.index(END)]


class Page:
    """The bridge's section in a JavaScriptCore context of its own; each js() is a turn of the
    page's event loop (promises settle between them)."""

    def __init__(self):
        self.context = JavaScriptCore.Context()
        self.js(FAKES + section() + "0;")

    def js(self, script: str):
        value = self.context.evaluate(script, -1)
        exception = self.context.get_exception()
        if exception is not None:
            raise AssertionError(exception.get_message())
        return json.loads(value.to_json(0) or "null")

    def posted(self):
        return [message["on"] for message in self.js("posted")]


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_on_after_the_debounce(self):
        self.page.js("open();")
        self.assertEqual(self.page.js("advance(299); posted"), [])
        self.assertEqual(self.page.js("advance(1); posted"), [{"type": "recording", "on": True}])

    def test_a_moment_of_microphone_says_nothing(self):
        self.page.js("open();")
        self.page.js("advance(200); track(0).stop();")
        self.page.js("advance(5000);")
        self.assertEqual(self.page.posted(), [])

    def test_off_when_the_track_ends(self):
        self.page.js("open();")
        self.page.js("advance(400); track(0).stop();")  # no timer: at once
        self.assertEqual(self.page.posted(), [True, False])

    def test_off_when_webkit_ends_the_track(self):
        # The microphone switched off (Account.stop_media ends the capture), or the device unplugged.
        self.page.js("open();")
        self.page.js("advance(400); track(0).end();")
        self.assertEqual(self.page.posted(), [True, False])

    def test_off_with_the_last_track(self):
        self.page.js("open(); open();")
        self.page.js("advance(400); track(0).stop();")
        self.assertEqual(self.page.posted(), [True])
        self.page.js("track(1).stop();")
        self.assertEqual(self.page.posted(), [True, False])

    def test_no_repeats(self):
        self.page.js("open();")
        self.page.js("advance(400); open();")
        self.page.js("advance(10000); track(0).stop(); track(0).stop();")
        self.page.js("advance(10000); track(1).stop(); track(1).stop(); track(1).end();")
        self.page.js("advance(10000);")
        self.assertEqual(self.page.posted(), [True, False])
        self.page.js("open();")  # another voice message
        self.page.js("advance(400); track(2).stop(); advance(5000);")
        self.assertEqual(self.page.posted(), [True, False, True, False])

    def test_a_track_ended_unheard_is_seen_within_a_second(self):
        self.page.js("open();")
        self.page.js("advance(400); track(0).readyState = 'ended';")  # no stop(), no event
        self.page.js("advance(1000);")
        self.assertEqual(self.page.posted(), [True, False])

    def test_calls_are_not_recordings(self):
        self.page.js("inCall = true; open();")
        self.page.js("advance(5000);")
        self.assertEqual(self.page.posted(), [])

    def test_a_call_starting_ends_it(self):
        self.page.js("open();")
        self.page.js("advance(400); inCall = true; advance(1000);")
        self.assertEqual(self.page.posted(), [True, False])
        self.page.js("inCall = false; advance(5000);")  # the call's over, the track still live: not a new one
        self.assertEqual(self.page.posted(), [True, False])

    def test_a_peer_connection_alone_isnt_a_call(self):
        # WhatsApp makes them for its own checks (rtc.py NOT_CALLS): only calling() counts.
        self.page.js("window.__natterRtc = { active: () => false, counts: () => ({ connections: 2 }) }; open();")
        self.page.js("advance(400);")
        self.assertEqual(self.page.posted(), [True])
        self.assertNotIn("connections", section())

    def test_a_call_within_the_debounce(self):
        self.page.js("open();")
        self.page.js("advance(100); inCall = true; advance(5000);")
        self.assertEqual(self.page.posted(), [])

    def test_only_microphones(self):
        self.page.js("open({ video: true });")
        self.page.js("advance(5000);")
        self.assertEqual(self.page.posted(), [])

    def test_whatsapp_gets_its_stream(self):
        self.assertTrue(self.page.js("let got; window.navigator.mediaDevices.getUserMedia({ audio: true })"
                                     ".then((s) => { got = s; }); 0;") == 0)
        self.assertTrue(self.page.js("got === streams[0]"))

    def test_nothing_while_off(self):
        # No timer runs while nobody records: the page stays idle.
        self.page.js("open();")
        self.page.js("advance(400); track(0).stop();")
        self.assertEqual(self.page.js("timers.length"), 0)

    def test_nothing_of_the_page(self):
        code = section()
        for banned in ("localStorage", "sessionStorage", "indexedDB", "dispatchEvent", "require(", "__d(",
                       "webpack", "querySelector", "MediaRecorder", "getSettings", "label"):
            self.assertNotIn(banned, code)
        self.assertEqual(code.count("post("), 1)
        self.assertIn('post({ type: "recording", on });', code)


class MessageTest(unittest.TestCase):
    def test_parsed(self):
        self.assertEqual(parse_message({"type": "recording", "on": True}), {"type": "recording", "on": True})
        self.assertEqual(parse_message({"type": "recording", "on": False, "extra": "x"}),
                         {"type": "recording", "on": False})
        for bad in ({"type": "recording"}, {"type": "recording", "on": 1}, {"type": "recording", "on": "yes"}):
            self.assertIsNone(parse_message(bad), bad)


# ---- A fake session bus -------------------------------------------------------------------------


class FakeBus:
    """The session bus as VoicePause asks it: players by well-known name, each with its unique
    name, owner's pid, PlaybackStatus and CanPause. Pause and Play are recorded (and change the
    status, as a player would). `held`: answers wait for release(). `fail`: methods that fail."""

    def __init__(self, held=False):
        self.names_of: dict[str, str] = {}
        self.players: dict[str, dict] = {}
        self.done: list[tuple[str, str]] = []
        self.asked: list[tuple] = []
        self.held, self.waiting, self.fail = held, [], set()

    def add(self, name, unique, pid=5000, status="Playing", can_pause=True):
        self.names_of[MPRIS + name] = unique
        self.players.setdefault(unique, {"pid": pid, "PlaybackStatus": status, "CanPause": can_pause})
        return self.players[unique]

    def _answer(self, method, done, value):
        if method in self.fail:
            value = False if method == "call" else None
        if self.held:
            self.waiting.append((done, value))
        else:
            done(value)

    def release(self):
        while self.waiting:
            done, value = self.waiting.pop(0)
            done(value)

    def names(self, done):
        self.asked.append(("names",))
        self._answer("names", done, [":1.1", "org.freedesktop.Notifications", *self.names_of])

    def owner(self, name, done):
        self.asked.append(("owner", name))
        self._answer("owner", done, self.names_of.get(name))

    def pid(self, unique, done):
        self.asked.append(("pid", unique))
        player = self.players.get(unique)
        self._answer("pid", done, player and player["pid"])

    def get(self, unique, prop, done):
        self.asked.append(("get", unique, prop))
        player = self.players.get(unique)
        self._answer("get", done, player[prop] if player else None)

    def call(self, unique, method, done):
        self.asked.append(("call", unique, method))
        player = self.players.get(unique)
        ok = player is not None and "call" not in self.fail
        if ok:
            self.done.append((method, unique))
            player["PlaybackStatus"] = {"Pause": "Paused", "Play": "Playing"}[method]
        self._answer("call", done, ok)


class Timers:
    """GLib.timeout_add as VoicePause uses it: kept until fired."""

    def __init__(self):
        self.timers, self.next = {}, 0

    def later(self, ms, run):
        self.next += 1
        self.timers[self.next] = (ms, run)
        return self.next

    def cancel(self, timer):
        self.timers.pop(timer, None)

    def fire(self):
        """The timer that's due goes off; its milliseconds."""
        (timer, (ms, run)), = self.timers.items()
        del self.timers[timer]
        run()
        return ms


NATTER = 4242  # Natter's process in the fake family


class PauseCase(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.timers = Timers()
        self.clock = [1000.0]
        self.lines = []
        self.on, self.call = True, False
        self.voice = VoicePause(self.bus, enabled=lambda: self.on, calling=lambda: self.call,
                                family=lambda: {NATTER, NATTER + 1, NATTER + 2}, later=self.timers.later,
                                cancel=self.timers.cancel, now=lambda: self.clock[0], write=self.lines.append)
        # Quiet: nothing reaches the log but the one line.
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(self.out))

    def tearDown(self):
        self.assertEqual(self.out.getvalue(), "")

    def record(self, on=True, source="main"):
        self.voice.recording(source, on)

    def stop(self, source="main"):
        """The recording ends, and RESUME_DELAY later its players are asked."""
        self.voice.recording(source, False)
        self.assertEqual(self.timers.fire(), RESUME_DELAY_MS)

    def plays(self):
        return [unique for method, unique in self.bus.done if method == "Play"]

    def pauses(self):
        return [unique for method, unique in self.bus.done if method == "Pause"]


class PausingTest(PauseCase):
    def test_pauses_only_what_plays_and_can_pause(self):
        self.bus.add("spotify", ":1.10")
        self.bus.add("vlc", ":1.11", status="Paused")
        self.bus.add("radio", ":1.12", can_pause=False)
        self.bus.add("totem", ":1.13", status="Stopped")
        self.bus.add("odd", ":1.14", can_pause="yes")  # only a true boolean
        self.record()
        self.assertEqual(self.bus.done, [("Pause", ":1.10")])

    def test_skips_natters_own_by_pid(self):
        self.bus.add("webkit.instance-1", ":1.20", pid=NATTER + 2)  # a web process's media session
        self.bus.add("natterish", ":1.21", pid=NATTER)
        self.bus.add("spotify", ":1.22")
        self.record()
        self.assertEqual(self.pauses(), [":1.22"])
        self.assertNotIn(("get", ":1.20", "PlaybackStatus"), self.bus.asked)  # not even asked

    def test_skips_natters_own_by_name(self):
        self.bus.add(f"{APP_ID}.Sandboxed.instance-7-1", ":1.30")
        self.bus.add(APP_ID, ":1.31")
        self.record()
        self.assertEqual(self.bus.done, [])
        self.assertEqual([ask for ask in self.bus.asked if ask[0] == "owner"], [])

    def test_a_player_with_two_names_is_paused_once(self):
        self.bus.add("vlc", ":1.40")
        self.bus.add("vlc.instance99", ":1.40")
        self.record()
        self.assertEqual(self.bus.done, [("Pause", ":1.40")])

    def test_only_mpris_names(self):
        self.record()
        self.assertEqual([ask for ask in self.bus.asked if ask[0] == "owner"], [])

    def test_a_second_page_recording_changes_nothing(self):
        self.bus.add("spotify", ":1.10")
        self.record(source="main")
        self.record(source="work")
        self.record(source="main")  # said again
        self.assertEqual(len([ask for ask in self.bus.asked if ask == ("names",)]), 1)
        self.voice.recording("main", False)
        self.assertEqual(self.timers.timers, {})  # work still records
        self.stop("work")
        self.assertEqual(self.plays(), [":1.10"])


class ResumingTest(PauseCase):
    def test_plays_again_only_what_it_paused(self):
        self.bus.add("spotify", ":1.10")
        self.bus.add("vlc", ":1.11", status="Paused")  # paused before: stays so
        self.record()
        self.voice.recording("main", False)
        self.assertEqual(self.plays(), [])  # not before RESUME_DELAY
        self.assertEqual(self.timers.fire(), RESUME_DELAY_MS)
        self.assertEqual(self.plays(), [":1.10"])
        self.assertEqual(self.voice._paused, {})  # forgotten once played again

    def test_leaves_alone_a_player_the_person_changed(self):
        for status in ("Playing", "Stopped"):
            with self.subTest(status=status):
                self.bus = FakeBus()
                self.voice.bus = self.bus
                player = self.bus.add("spotify", ":1.10")
                self.record()
                player["PlaybackStatus"] = status  # played or stopped meanwhile
                self.stop()
                self.assertEqual(self.plays(), [])

    def test_a_player_gone_meanwhile(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        del self.bus.players[":1.10"]
        self.stop()
        self.assertEqual(self.plays(), [])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 0"])

    def test_forgotten_after_fifteen_minutes(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.clock[0] += FORGET + 1
        self.stop()
        self.assertEqual(self.plays(), [])
        self.assertEqual(self.voice._paused, {})
        self.assertEqual(FORGET, 15 * 60)

    def test_within_fifteen_minutes(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.clock[0] += FORGET - 1
        self.stop()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_new_recording_within_the_delay(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.voice.recording("main", False)
        self.record()  # the next voice message, at once
        self.assertEqual(self.timers.timers, {})
        self.assertEqual(self.plays(), [])
        self.stop()
        self.assertEqual(self.plays(), [":1.10"])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 1"])

    def test_the_end_while_pausing_waits_for_the_answers(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        for _ in range(4):  # names, owner, pid, status
            done, value = self.bus.waiting.pop(0)
            done(value)
        self.voice.recording("main", False)  # with CanPause still to answer
        self.timers.fire()
        self.assertEqual(self.plays(), [])
        self.bus.release()  # CanPause: the recording's over, so it isn't paused at all
        self.assertEqual(self.bus.done, [])
        self.assertEqual(self.lines, ["natter: voice note: paused 0 players, resumed 0"])

    def test_a_pause_answered_after_the_end_still_plays_again(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        for _ in range(5):  # names, owner, pid, status, CanPause: Pause is sent
            done, value = self.bus.waiting.pop(0)
            done(value)
        self.voice.recording("main", False)
        self.timers.fire()
        self.assertEqual(self.plays(), [])
        self.bus.release()  # Pause answers; then the status is asked and Play sent
        self.bus.release()
        self.bus.release()
        self.assertEqual(self.bus.done, [("Pause", ":1.10"), ("Play", ":1.10")])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 1"])

    def test_quitting_plays_again_at_once(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.voice.quit()
        self.assertEqual(self.timers.timers, {})
        self.assertEqual(self.plays(), [":1.10"])

    def test_quitting_with_a_call_ringing(self):
        # It ends with Natter: what was paused plays again.
        self.bus.add("spotify", ":1.10")
        self.record()
        self.call = True
        self.voice.quit()
        self.assertEqual(self.plays(), [":1.10"])

    def test_quitting_waits_for_the_answers(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        for _ in range(5):  # Pause is sent
            done, value = self.bus.waiting.pop(0)
            done(value)
        self.voice.quit()
        quit_ = []
        self.voice.when_idle(lambda: quit_.append(1))
        self.assertEqual(quit_, [])
        done, value = self.bus.waiting.pop(0)  # Pause answers: the status is asked
        done(value)
        self.assertEqual(quit_, [])
        self.bus.release()  # the status, then Play
        self.assertEqual(self.plays(), [":1.10"])
        self.assertEqual(quit_, [1])
        self.assertEqual(self.timers.fire(), QUIT_WAIT_MS)  # the cap, once over, does nothing
        self.assertEqual(quit_, [1])

    def test_quitting_waits_no_longer_than_the_cap(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        self.voice.quit()
        quit_ = []
        self.voice.when_idle(lambda: quit_.append(1))
        self.assertEqual(self.timers.fire(), QUIT_WAIT_MS)
        self.assertEqual(quit_, [1])
        self.bus.release()
        self.assertEqual(quit_, [1])

    def test_nothing_owed_quits_at_once(self):
        quit_ = []
        self.voice.quit()
        self.voice.when_idle(lambda: quit_.append(1))
        self.assertEqual((quit_, self.timers.timers), ([1], {}))

    def test_quitting_with_the_main_loop_over(self):
        # do_shutdown: answers come to a context of quit_and_wait's own.
        self.bus.add("spotify", ":1.10")
        self.record()
        self.voice.quit_and_wait()
        self.assertEqual(self.plays(), [":1.10"])
        self.assertIsNone(GLib.MainContext.get_thread_default())  # its own context is let go


class CallTest(PauseCase):
    def test_a_call_holds_them_until_it_ends(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.call = True
        self.voice.call_started()
        self.stop()
        self.assertEqual(self.plays(), [])  # not over the call
        self.call = False
        self.voice.call_over()
        self.assertEqual(self.timers.fire(), RESUME_DELAY_MS)
        self.assertEqual(self.plays(), [":1.10"])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 1"])

    def test_a_call_starting_within_the_delay(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.voice.recording("main", False)
        self.call = True
        self.voice.call_started()
        self.assertEqual(self.timers.timers, {})
        self.assertEqual(self.plays(), [])
        self.call = False
        self.voice.call_over()
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_ring_answered_holds_them_until_the_call_ends(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.call = True  # a call rings (rtc.py ring): calling, not connected
        self.stop()
        self.assertEqual(self.plays(), [])
        self.assertEqual(self.voice._paused.keys(), {":1.10"})
        self.voice.call_started()  # answered
        self.voice.call_over()  # still on: nothing
        self.assertEqual(self.timers.timers, {})
        self.call = False
        self.voice.call_over()
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 1"])

    def test_a_ring_that_ends_unanswered_plays_them_again(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.call = True  # declined, missed
        self.stop()
        self.voice.call_over()  # still ringing: nothing yet
        self.assertEqual(self.timers.timers, {})
        self.call = False
        self.voice.call_over()
        self.assertEqual(self.timers.fire(), RESUME_DELAY_MS)
        self.assertEqual(self.plays(), [":1.10"])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 1"])

    def test_a_ring_while_their_status_is_asked(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.bus.held = True
        self.stop()
        self.call = True
        self.bus.release()  # PlaybackStatus answers with the call ringing
        self.assertEqual(self.plays(), [])
        self.assertEqual(self.lines, [])
        self.bus.held, self.call = False, False
        self.voice.call_over()
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_ring_mid_recording_then_none(self):
        self.bus.add("spotify", ":1.10")
        self.record()
        self.call = True
        self.voice.call_over()  # recording still: nothing
        self.call = False
        self.voice.call_over()
        self.assertEqual(self.timers.timers, {})
        self.stop()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_pause_answered_after_the_next_recording_began(self):
        # Stopped and started again while a slow player's Pause was on its way: still its.
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        for _ in range(5):  # Pause is sent
            done, value = self.bus.waiting.pop(0)
            done(value)
        self.voice.recording("main", False)
        self.record()
        self.bus.release()  # Pause answers, then the new round lists it: Paused, so left to the first
        self.bus.release()
        self.bus.release()
        self.assertEqual(self.voice._paused.keys(), {":1.10"})
        self.bus.held = False
        self.stop()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_pause_answered_after_a_call_started_waits_with_the_rest(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        for _ in range(5):
            done, value = self.bus.waiting.pop(0)
            done(value)
        self.call = True
        self.voice.call_started()
        self.bus.release()
        self.assertEqual(self.voice._paused.keys(), {":1.10"})
        self.bus.held = False
        self.voice.recording("main", False)
        self.timers.fire()
        self.assertEqual(self.plays(), [])  # the call is on
        self.call = False
        self.voice.call_over()
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])

    def test_nothing_paused_during_a_call(self):
        self.bus.add("spotify", ":1.10")
        self.call = True
        self.record()
        self.assertEqual(self.bus.asked, [])


class SettingTest(PauseCase):
    def test_off_does_nothing(self):
        self.on = False
        self.bus.add("spotify", ":1.10")
        self.record()
        self.stop()
        self.assertEqual(self.bus.asked, [])
        self.assertEqual(self.lines, [])

    def test_turned_off_mid_recording(self):
        # What it paused while on plays again at the end; nothing more is paused.
        self.bus.add("spotify", ":1.10")
        self.record()
        self.on = False
        self.bus.add("vlc", ":1.11")
        self.record(source="work")
        self.voice.recording("work", False)
        self.assertEqual(self.timers.timers, {})  # main still records
        self.stop()
        self.assertEqual(self.bus.done, [("Pause", ":1.10"), ("Play", ":1.10")])
        self.assertEqual(self.lines, ["natter: voice note: paused 1 player, resumed 1"])

    def test_turned_off_while_pausing(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        self.on = False
        self.bus.release()
        self.bus.release()
        self.assertEqual(self.bus.done, [])

    def test_on_by_default(self):
        self.assertIs(DEFAULTS["pause_other_audio"], True)
        self.assertIs(Settings(Path("/nonexistent/natter-test/settings.json"))["pause_other_audio"], True)


class QuietTest(PauseCase):
    def test_a_bus_that_fails_everywhere(self):
        for method in ("names", "owner", "pid", "get", "call"):
            with self.subTest(method=method):
                self.bus = FakeBus()
                self.voice.bus = self.bus
                self.bus.add("spotify", ":1.10")
                self.bus.fail = {method}
                self.record()
                self.stop()
        self.assertEqual(self.voice._busy, 0)

    def test_an_unknown_pid_is_no_reason_to_skip(self):
        # A sandbox's proxy may not say; the names have told already.
        self.bus.add("spotify", ":1.10", pid=None)
        self.record()
        self.assertEqual(self.pauses(), [":1.10"])

    def test_a_bus_that_raises(self):
        def broken(*_args):
            raise RuntimeError("lab: broken on purpose")
        self.bus.names = broken
        self.record()
        self.stop()
        self.assertEqual(self.voice._busy, 0)
        self.assertEqual(self.lines, ["natter: voice note: paused 0 players, resumed 0"])

    def test_answers_that_never_come(self):
        self.bus.held = True
        self.bus.add("spotify", ":1.10")
        self.record()
        self.voice.recording("main", False)
        self.timers.fire()  # waits for them: GDBus's own timeout answers each, as an error
        for done, _value in self.bus.waiting:
            done(None)
        self.bus.waiting.clear()
        self.assertEqual(self.voice._busy, 0)
        self.assertEqual(self.bus.done, [])


class LogTest(PauseCase):
    def test_the_line(self):
        self.assertEqual(log_line(2, 2), "natter: voice note: paused 2 players, resumed 2")
        self.assertEqual(log_line(1, 0), "natter: voice note: paused 1 player, resumed 0")
        self.assertEqual(log_line(0, 0), "natter: voice note: paused 0 players, resumed 0")

    def test_one_line_per_recording_counts_only(self):
        self.bus.add("spotify", ":1.10")
        self.bus.add("Secret Song Player", ":1.11")
        self.record()
        self.assertEqual(self.lines, [])
        self.stop()
        self.assertEqual(self.lines, ["natter: voice note: paused 2 players, resumed 2"])
        self.record()
        self.stop()
        self.assertEqual(self.lines[1:], ["natter: voice note: paused 2 players, resumed 2"])
        for line in self.lines:
            self.assertNotIn("Secret", line)
            self.assertNotIn(":1.", line)


# ---- The session bus itself, on a stand-in connection ---------------------------------------------


class Connection:
    """A Gio.DBusConnection as Bus calls it: each call kept, answered at once."""

    def __init__(self, reply=None, error=None, raises=None):
        self.reply, self.error, self.raises, self.calls = reply, error, raises, []

    def call(self, *args):
        self.calls.append(args)
        if self.raises:
            raise self.raises
        args[-1](self, "result")

    def call_finish(self, result):
        if self.error:
            raise self.error
        return self.reply


class GioBusTest(unittest.TestCase):
    def ask(self, connection, method, *args):
        got = []
        getattr(Bus(lambda: connection), method)(*args, got.append)
        return got

    def test_listing(self):
        connection = Connection(GLib.Variant("(as)", ([MPRIS + "vlc", ":1.1"],)))
        self.assertEqual(self.ask(connection, "names"), [[MPRIS + "vlc", ":1.1"]])
        name, path, interface, method, args, reply, flags, timeout, cancellable, _done = connection.calls[0]
        self.assertEqual((name, path, interface, method, args), ("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                                 "org.freedesktop.DBus", "ListNames", None))
        self.assertEqual(reply.dup_string(), "(as)")
        self.assertEqual((flags, timeout, cancellable), (Gio.DBusCallFlags.NO_AUTO_START, TIMEOUT_MS, None))
        self.assertEqual(TIMEOUT_MS, 500)

    def test_owner_and_pid(self):
        self.assertEqual(self.ask(Connection(GLib.Variant("(s)", (":1.5",))), "owner", MPRIS + "vlc"), [":1.5"])
        connection = Connection(GLib.Variant("(u)", (4321,)))
        self.assertEqual(self.ask(connection, "pid", ":1.5"), [4321])
        self.assertEqual(connection.calls[0][3], "GetConnectionUnixProcessID")
        self.assertEqual(connection.calls[0][4].unpack(), (":1.5",))

    def test_a_property(self):
        connection = Connection(GLib.Variant("(v)", (GLib.Variant("s", "Playing"),)))
        self.assertEqual(self.ask(connection, "get", ":1.5", "PlaybackStatus"), ["Playing"])
        name, path, interface, method, args = connection.calls[0][:5]
        self.assertEqual((name, path, interface, method), (":1.5", "/org/mpris/MediaPlayer2",
                                                           "org.freedesktop.DBus.Properties", "Get"))
        self.assertEqual(args.unpack(), (PLAYER, "PlaybackStatus"))
        connection = Connection(GLib.Variant("(v)", (GLib.Variant("b", True),)))
        self.assertEqual(self.ask(connection, "get", ":1.5", "CanPause"), [True])

    def test_pause_and_play(self):
        connection = Connection(GLib.Variant("()", ()))
        self.assertEqual(self.ask(connection, "call", ":1.5", "Pause"), [True])
        self.assertEqual(connection.calls[0][:5], (":1.5", "/org/mpris/MediaPlayer2", PLAYER, "Pause", None))
        self.assertEqual(connection.calls[0][7], TIMEOUT_MS)

    def test_failures_answer_none(self):
        timeout = GLib.Error.new_literal(Gio.io_error_quark(), "Timeout was reached", Gio.IOErrorEnum.TIMED_OUT)
        for connection in (Connection(error=timeout), Connection(raises=TypeError("lab")), None):
            with self.subTest(connection=connection):
                self.assertEqual(self.ask(connection, "names"), [None])
                self.assertEqual(self.ask(connection, "get", ":1.5", "PlaybackStatus"), [None])
                self.assertEqual(self.ask(connection, "call", ":1.5", "Play"), [False])

    def test_no_bus(self):
        def gone():
            raise GLib.Error("lab: no bus")
        got = []
        Bus(gone).names(got.append)
        self.assertEqual(got, [None])


# ---- Natter's own processes and names -------------------------------------------------------------


class FamilyTest(unittest.TestCase):
    def test_every_process_under_natter(self):
        with contextlib.ExitStack() as stack:
            import tempfile
            proc = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            tree = {100: [200, 201], 200: [300], 300: [400], 201: [], 400: [], 999: [100]}
            for pid, children in tree.items():
                task = proc / str(pid) / "task" / str(pid)
                task.mkdir(parents=True)
                (task / "children").write_text(" ".join(map(str, children)) + (" " if children else ""))
            self.assertEqual(natter_family(100, proc), {100, 200, 201, 300, 400})
            self.assertEqual(natter_family(100, proc, depth=1), {100, 200, 201})
            self.assertEqual(natter_family(12345, proc), {12345})  # nothing readable: itself

    def test_own_names(self):
        self.assertTrue(own_name(MPRIS + APP_ID))
        self.assertTrue(own_name(f"{MPRIS}{APP_ID}.Sandboxed.instance-12-3"))
        self.assertFalse(own_name(MPRIS + APP_ID + "Other"))
        self.assertFalse(own_name(MPRIS + "spotify"))


# ---- Account and the app ------------------------------------------------------------------------


class AccountTest(unittest.TestCase):
    """The page's say, through Account, to a VoicePause on the fake bus; a page that goes ends it."""

    def setUp(self):
        from test_reopen import Clock, Page as ReopenPage
        self.clock = Clock()
        self.enterContext(mock.patch.object(natter_account, "GLib", self.clock))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.bus, self.timers, self.lines = FakeBus(), Timers(), []
        self.voice = VoicePause(self.bus, enabled=lambda: True, family=lambda: {NATTER}, later=self.timers.later,
                                cancel=self.timers.cancel, write=self.lines.append)
        self.bus.add("spotify", ":1.10")
        self.page = ReopenPage()
        self.page.app.voice_note = lambda account, on: self.voice.recording(account.id, on)

    def plays(self):
        return [unique for method, unique in self.bus.done if method == "Play"]

    def test_the_page_says(self):
        self.page.says("recording", on=True)
        self.assertTrue(self.page.recording)
        self.assertEqual(self.bus.done, [("Pause", ":1.10")])
        self.page.says("recording", on=False)
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_reload_mid_recording_resumes(self):
        self.page.says("recording", on=True)
        self.page.commits()  # Reload, a fresh page, WhatsApp loading itself again
        self.assertFalse(self.page.recording)
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])

    def test_a_crash_mid_recording_resumes(self):
        self.page.says("recording", on=True)
        self.page._on_crash(None, WebKit.WebProcessTerminationReason.CRASHED)
        self.timers.fire()
        self.assertEqual(self.plays(), [":1.10"])

    def test_each_change_once(self):
        told = []
        self.page.app.voice_note = lambda account, on: told.append(on)
        for on in (True, True, False, False, True):
            self.page.says("recording", on=on)
        self.page.commits()
        self.page.commits()
        self.assertEqual(told, [True, False, True, False])

    def test_the_page_going_is_wired(self):
        import inspect
        from natter.account import Account
        for method in (Account._on_load_changed, Account._on_crash, Account.remove):
            self.assertIn("self.set_recording(False)", inspect.getsource(method), method.__name__)


class AppTest(unittest.TestCase):
    def test_voice_note_by_account(self):
        heard = []
        fake = SimpleNamespace(voice_pause=SimpleNamespace(recording=lambda source, on: heard.append((source, on))))
        Natter.voice_note(fake, SimpleNamespace(id="work"), True)
        self.assertEqual(heard, [("work", True)])

    def test_the_call_window_starting_a_call(self):
        told = []
        fake = SimpleNamespace(voice_pause=SimpleNamespace(call_started=lambda: told.append("started"),
                                                           call_over=lambda: told.append("over")),
                               _pending_call=None, withdraw_notification=lambda key: None)
        Natter._on_in_call(fake, None, False)
        Natter._on_in_call(fake, None, True)
        self.assertEqual(told, ["over", "started"])

    def test_a_page_call_connecting_not_ringing(self):
        told = []
        fake = SimpleNamespace(voice_pause=SimpleNamespace(call_started=lambda: told.append("started")),
                               _apply_page_sound=lambda: told.append("sound"))
        Natter.page_connected(fake, True)
        Natter.page_connected(fake, False)
        self.assertEqual(told, ["started", "sound", "sound"])
        import inspect
        source = inspect.getsource(Natter._on_page_call)
        self.assertNotIn("call_started", source)  # it rings too: only page_connected drops them
        self.assertLess(source.index("else:"), source.index("self.voice_pause.call_over()"))
        self.assertIn("self.app.page_connected(on)", inspect.getsource(natter_account.Account.__init__))

    def test_quitting_waits_for_the_players(self):
        import inspect
        self.assertIn("self.voice_pause.when_idle(self.quit)", inspect.getsource(Natter.quit_natter))
        self.assertIn("self.voice_pause.quit_and_wait()", inspect.getsource(Natter.do_shutdown))

    def test_the_microphone_switched_off_ends_it(self):
        ended = []
        accounts = [SimpleNamespace(set_recording=lambda on, n=n: ended.append((n, on))) for n in (1, 2)]
        fake = SimpleNamespace(lookup_action=lambda key: None, _apply_media=lambda key: None, accounts=accounts,
                               window=None, settings={"microphone": False})
        Natter._on_setting(fake, "microphone", False)
        self.assertEqual(ended, [(1, False), (2, False)])
        ended.clear()
        Natter._on_setting(fake, "microphone", True)
        Natter._on_setting(fake, "camera", False)
        self.assertEqual(ended, [])

    def test_made_with_the_setting_and_both_kinds_of_call(self):
        import inspect
        source = inspect.getsource(Natter.do_startup)
        self.assertIn('enabled=lambda: bool(self.settings["pause_other_audio"])', source)
        self.assertIn("calling=lambda: self.calling or bool(self.calls and self.calls.in_call)", source)


class PreferencesTest(unittest.TestCase):
    def setUp(self):
        from test_media_ui import STAND_INS, App, Host, Settings as UiSettings
        self.enterContext(mock.patch.multiple(prefs, **STAND_INS))
        self.host = Host(App(UiSettings()))
        self.page = self.host._privacy()

    def test_under_the_microphone(self):
        microphone, pause, camera = self.page.added()[1].added()
        self.assertEqual(microphone.props["title"], "Microphone")
        self.assertEqual(camera.props["title"], "Camera")
        self.assertEqual((pause.kind, pause.props["title"], pause.props["subtitle"]),
                         ("Adw.SwitchRow", "Pause Other Audio While Recording",
                          "Music and videos in other apps pause while you record a voice message, then play again"))
        self.assertTrue(pause.get_active())
        self.assertIs(self.host._rows["pause_other_audio"], pause)  # followed like every other switch
        self.assertNotIn("pause_other_audio", self.host.device_rows)  # no device line under it

    def test_writes_and_follows_the_setting(self):
        pause = self.host._rows["pause_other_audio"]
        pause.set_active(False)
        self.assertEqual(self.host.settings.written, [("pause_other_audio", False)])
        self.host.settings.change_elsewhere("pause_other_audio", True)
        self.host._on_setting("pause_other_audio", True)
        self.assertTrue(pause.get_active())


class PackagingTest(unittest.TestCase):
    def test_the_flatpak_may_talk_to_players(self):
        manifest = (ROOT / "packaging" / "io.github.Officialbabs.Natter.yml").read_text()
        self.assertIn("\n  - --talk-name=org.mpris.MediaPlayer2.*\n", manifest)
        before = manifest[:manifest.index("--talk-name=org.mpris.MediaPlayer2.*")].rstrip().splitlines()[-2]
        self.assertTrue(before.strip().startswith("#"))

    def test_no_new_dependencies(self):
        import ast
        tree = ast.parse((ROOT / "natter" / "mediapause.py").read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or "." for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "os", "time", "traceback", "pathlib", "typing", "gi.repository",
                                    "."})


if __name__ == "__main__":
    unittest.main()
