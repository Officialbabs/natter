"""Voice messages that can't be heard: the bridge's idle-audio code wakes a context it suspended
for every way a page plays sound (a worklet or a script processor of any name, the constructor
form of a media element source), and the bridge tells the log how sound in the page played or
failed (bridge.js "Sound that can't be heard", playback.py), in kinds and codes only. Run here
in JavaScriptCore against stand-ins for Web Audio and media elements, as test_audio_sizes.py.

And outside the page: Natter's sound streams the sound server opened muted are told in the log
(system.py AudioFollower), and the start-up check knows the elements WebKit's player sends sound
through."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("JavaScriptCore", "6.0")
from gi.repository import JavaScriptCore  # noqa: E402

from natter import playback, system  # noqa: E402
from natter.account import Account, parse_message  # noqa: E402

BRIDGE = (Path(__file__).resolve().parent.parent / "natter" / "bridge.js").read_text()
IDLE = BRIDGE[BRIDGE.index("  // ---- Idle audio"):BRIDGE.index("  // ---- Videos aren't pictures")]
HEARD = BRIDGE[BRIDGE.index("  // ---- Sound that can't be heard"):BRIDGE.index("  // ---- Opening a chat")]

# A page for the idle-audio section and the one after: contexts that suspend and resume (or, with
# stuck = true, never resume), worklets, script processors, element sources, media elements that
# play, fail or are refused, blobs and object URLs, and timers run by hand.
FAKES = """
var window = globalThis;
let clock = 100000;
var performance = { now: () => clock };
const timers = [];
function setTimeout(callback) { timers.push(callback); return timers.length; }
function setInterval() { return 0; }
function runTimers() { timers.splice(0).forEach((callback) => callback()); }
const posted = [];
const post = (message) => posted.push(JSON.parse(JSON.stringify(message)));
let stuck = false;
class Target {
  constructor() { this.listeners = {}; }
  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }
  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter((other) => other !== listener);
  }
  fire(type) { (this.listeners[type] || []).forEach((listener) => listener()); }
}
class NativeElementSource { constructor(context, options) { this.context = context; this.mediaElement = options.mediaElement; } }
class FakeNode extends Target {
  constructor(context) { super(); this.context = context; this.connections = 0; this.onaudioprocess = null; }
  connect(destination) { this.connections++; return destination; }
  disconnect() { this.connections = 0; }
}
const destination = { kind: "destination" };
window.AudioContext = class extends Target {
  constructor() { super(); this.state = "running"; }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
  resume() { if (!stuck) this.state = "running"; return Promise.resolve(); }
  close() { this.state = "closed"; this.fire("statechange"); return Promise.resolve(); }
  createMediaStreamSource(stream) { const node = new FakeNode(this); node.mediaStream = stream; return node; }
  createMediaElementSource(element) { return new NativeElementSource(this, { mediaElement: element }); }
  createScriptProcessor() { return new FakeNode(this); }
  decodeAudioData(data, ok, fail) {
    if (data === "bad") {
      const error = { name: "EncodingError", message: "Secret Person's https://mmg.whatsapp.net/x" };
      if (fail) Promise.resolve().then(() => fail(error));
      return Promise.reject(error);
    }
    const buffer = { duration: 1, decodedFrom: String(data) };
    if (ok) Promise.resolve().then(() => ok(buffer));
    return Promise.resolve(buffer);
  }
};
window.AudioWorkletNode = class extends FakeNode {};
// WebCodecs: Opus is supported; configure() throws for a codec "bad"; fail() is the decoder's error.
window.AudioDecoder = class {
  constructor(init) { this.init = init; this.state = "unconfigured"; }
  configure(config) {
    if (config.codec === "bad" || config.bad) throw { name: "NotSupportedError", message: "Secret" };
    this.state = "configured";
  }
  fail(name) { this.init.error({ name, message: "Secret Person https://x" }); }
  static isConfigSupported(config) { return Promise.resolve({ supported: config.codec === "opus", config }); }
};
window.MediaElementAudioSourceNode = NativeElementSource;
window.AudioScheduledSourceNode = class extends Target {
  constructor(context) { super(); this.context = context; this.buffer = null; this.loop = false; }
  start() {}
};
window.HTMLMediaElement = class extends Target {
  constructor() { super(); this.paused = true; this.ended = false; this.isConnected = true; this.src = ""; this.currentSrc = ""; this.error = null; this.refuse = null; }
  play() {
    if (this.refuse) return Promise.reject({ name: this.refuse, message: "a message with a https://url" });
    this.paused = false;
    return this.later || Promise.resolve();  // a test's own: its data comes when it says
  }
};
window.HTMLAudioElement = class extends window.HTMLMediaElement {
  canPlayType(type) { return /ogg/.test(type) ? (/opus/.test(type) ? "probably" : "maybe") : /mpeg/.test(type) ? "probably" : ""; }
};
window.HTMLVideoElement = class extends window.HTMLMediaElement {};
var HTMLMediaElement = window.HTMLMediaElement;
var document = { querySelectorAll: () => [], createElement: () => new window.HTMLAudioElement() };
window.MediaSource = class { static isTypeSupported(type) { return /webm|mp4/.test(type); } };
var Blob = class { constructor(type) { this.type = type; } };
let urls = 0;
var URL = { createObjectURL: () => `blob:https://web.whatsapp.com/${++urls}-secret`, revokeObjectURL() {} };
const shown = new Map();
"""


def run(script: str, heard: bool = False):
    """The idle-audio section (and with `heard`, the one after it) in a fresh context, then `script`."""
    context = JavaScriptCore.Context()
    value = context.evaluate(FAKES + IDLE + (HEARD if heard else "") + script, -1)
    exception = context.get_exception()
    if exception is not None:
        raise AssertionError(exception.get_message())
    return json.loads(value.to_json(0))


class WakeTest(unittest.TestCase):
    """A context Natter rested wakes for any way a page plays sound into it."""

    def rested_then(self, make: str):
        return run(f"""
            const context = new window.AudioContext();
            rest();  // idle long enough: suspended
            const before = context.state;
            {make}
            [before, context.state, rested.has(context)];
        """)

    def test_a_worklet_of_any_name(self):
        # A page decoding a voice message itself (a wasm Opus player) plays it through a worklet.
        self.assertEqual(self.rested_then('new window.AudioWorkletNode(context, "opus-player");'),
                         ["suspended", "running", False])

    def test_a_script_processor(self):
        self.assertEqual(self.rested_then("context.createScriptProcessor(4096, 1, 1);"),
                         ["suspended", "running", False])

    def test_the_constructor_form_of_an_element_source(self):
        self.assertEqual(self.rested_then("""
            const element = new window.HTMLMediaElement();
            new window.MediaElementAudioSourceNode(context, { mediaElement: element });
        """), ["suspended", "running", False])

    def test_an_element_source_by_its_constructor_keeps_the_context_up_while_it_plays(self):
        # Out of the page and playing: before, rest() didn't know of it and cut a long note off.
        result = run("""
            const context = new window.AudioContext();
            const element = new window.HTMLMediaElement();
            element.isConnected = false;
            new window.MediaElementAudioSourceNode(context, { mediaElement: element });
            element.play();
            clock += 60000;  // a long voice message, long after anything started
            rest();
            const playing = context.state;
            element.paused = true;
            rest();
            [playing, context.state];
        """)
        self.assertEqual(result, ["running", "suspended"])

    def test_it_wakes_again_at_each_play(self):
        result = run("""
            const context = new window.AudioContext();
            const element = new window.HTMLMediaElement();
            new window.MediaElementAudioSourceNode(context, { mediaElement: element });
            clock += 60000;
            rest();
            const asleep = context.state;
            element.fire("play");
            [asleep, context.state];
        """)
        self.assertEqual(result, ["suspended", "running"])

    def test_what_create_media_element_source_makes_is_still_one(self):
        result = run("""
            const context = new window.AudioContext();
            const node = context.createMediaElementSource(new window.HTMLMediaElement());
            [node instanceof window.MediaElementAudioSourceNode, audio().sizes.elements];
        """)
        self.assertEqual(result, [True, 1])

    def test_a_call_worklet_is_still_a_call(self):
        result = run("""
            const context = new window.AudioContext();
            new window.AudioWorkletNode(context, "voip-playback");
            rest();
            [context.state, audio().sizes.calls];
        """)
        self.assertEqual(result, ["running", 1])

    def test_a_worklet_holds_rest_off_while_it_is_connected(self):
        # A long voice message through a page's own player: no suspend mid-note, however long.
        result = run("""
            const context = new window.AudioContext();
            const node = new window.AudioWorkletNode(context, "opus-player");
            node.connect(destination);
            rest();
            const soon = context.state;
            clock += 31000;
            rest();
            const later = context.state;
            clock += 600000;
            rest();
            const muchLater = context.state;
            node.disconnect();
            rest();
            [soon, later, muchLater, context.state];
        """)
        self.assertEqual(result, ["running", "running", "running", "suspended"])

    def test_connecting_a_worklet_wakes_its_context(self):
        # Made early (or disconnected between notes) and rested: connecting it to play wakes it.
        result = run("""
            const context = new window.AudioContext();
            const node = new window.AudioWorkletNode(context, "opus-player");
            clock += 31000;
            rest();
            const before = context.state;
            node.connect(destination);
            const connected = context.state;
            clock += 600000;
            rest();
            const held = context.state;
            node.disconnect();
            clock += 31000;
            rest();
            const after = context.state;
            node.connect(destination);
            [before, connected, held, after, context.state, rested.has(context)];
        """)
        self.assertEqual(result, ["suspended", "running", "running", "suspended", "running", False])

    def test_a_script_processor_wakes_when_given_its_handler(self):
        result = run("""
            const context = new window.AudioContext();
            const node = context.createScriptProcessor(4096, 1, 1);
            node.connect(destination);
            clock += 31000;
            rest();
            const before = context.state;
            const handler = () => {};
            node.onaudioprocess = handler;
            [before, context.state, node.onaudioprocess === handler];
        """)
        self.assertEqual(result, ["suspended", "running", True])

    def test_an_audioprocess_listener_holds_rest_off(self):
        # A page may listen with addEventListener rather than set onaudioprocess.
        result = run("""
            const context = new window.AudioContext();
            const node = context.createScriptProcessor(4096, 1, 1);
            node.connect(destination);
            clock += 31000;
            rest();
            const idle = context.state;
            const listener = () => {};
            node.addEventListener("audioprocess", listener);
            const woken = context.state;
            clock += 600000;
            rest();
            const busy = context.state;
            node.removeEventListener("audioprocess", listener, true);  // another listener: still on
            rest();
            const still = context.state;
            node.removeEventListener("audioprocess", listener);
            rest();
            [idle, woken, busy, still, context.state, node.listeners.audioprocess.length];
        """)
        self.assertEqual(result, ["suspended", "running", "running", "running", "suspended", 0])

    def test_a_recorder_left_connected_lets_its_context_rest(self):
        # A voice-note recorder's worklet, fed by the microphone, left connected after it stops:
        # ten minutes on (a paused voice note's hold), its context rests.
        result = run("""
            const context = new window.AudioContext();
            const track = { readyState: "live" };
            const stream = { getTracks: () => [track] };
            const recorder = new window.AudioWorkletNode(context, "recorder");
            context.createMediaStreamSource(stream).connect(recorder);
            recorder.connect(destination);
            clock += 600000;
            rest();
            const recording = [context.state, audio().sizes.held];
            track.readyState = "ended";
            rest();
            const quiet = [context.state, audio().sizes.held];
            clock += 600000;
            rest();
            [recording, quiet, context.state, audio().sizes.held];
        """)
        self.assertEqual(result, [["running", 1], ["running", 1], "suspended", 0])

    def test_a_paused_voice_note_keeps_its_context_running(self):
        # WhatsApp's recorder: a script processor fed by the microphone. Paused, the microphone
        # stops (and the processor may be disconnected); resumed, a new stream feeds the same
        # processor. Resting the context meanwhile would make the resume reopen its output stream.
        result = run("""
            const context = new window.AudioContext();
            const first = { readyState: "live" };
            const recorder = context.createScriptProcessor(4096, 1, 1);
            recorder.onaudioprocess = () => {};
            context.createMediaStreamSource({ getTracks: () => [first] }).connect(recorder);
            recorder.connect(destination);
            clock += 40000;
            first.readyState = "ended";  // paused
            recorder.disconnect();
            const states = [];
            for (let minute = 1; minute <= 9; minute++) {
              clock += 60000;
              rest();
              states.push(context.state);
            }
            const second = { readyState: "live" };
            context.createMediaStreamSource({ getTracks: () => [second] }).connect(recorder);  // resumed
            recorder.connect(destination);
            clock += 120000;
            second.readyState = "ended";  // paused again: its hold starts afresh
            clock += 60000;
            rest();
            [states.every((state) => state === "running"), context.state];
        """)
        self.assertEqual(result, [True, "running"])

    def test_a_failed_wake_is_tried_again_then_told(self):
        context = JavaScriptCore.Context()
        context.evaluate(FAKES + IDLE + HEARD + """
            const ctx = new window.AudioContext();
            rest();
            let tries = 0;
            ctx.resume = function () { tries++; return Promise.reject({ name: "InvalidStateError", message: "x" }); };
            wake(ctx);
        """, -1)
        self.assertIsNone(context.get_exception())
        context.evaluate("runTimers();", -1)  # the second try, a second later
        self.assertIsNone(context.get_exception())
        result = json.loads(context.evaluate("JSON.stringify([tries, posted.filter((m) => m.what === 'wake')])", -1).to_string())
        self.assertEqual(result, [2, [{"type": "media", "what": "wake", "name": "InvalidStateError"}]])

    def test_a_failed_wake_of_a_context_natter_never_rested_is_not_told(self):
        # One the page suspended itself, or one WebKit hasn't started yet: tried again, not blamed
        # on the idle audio.
        context = JavaScriptCore.Context()
        context.evaluate(FAKES + IDLE + HEARD + """
            const ctx = new window.AudioContext();
            ctx.suspend();
            let tries = 0;
            ctx.resume = function () { tries++; return Promise.reject({ name: "InvalidStateError", message: "x" }); };
            wake(ctx);
        """, -1)
        self.assertIsNone(context.get_exception())
        context.evaluate("runTimers();", -1)
        self.assertIsNone(context.get_exception())
        result = json.loads(context.evaluate("JSON.stringify([tries, posted.filter((m) => m.what === 'wake')])", -1).to_string())
        self.assertEqual(result, [2, []])

    def test_held_contexts_are_counted(self):
        result = run("""
            const one = new window.AudioContext();
            const two = new window.AudioContext();
            new window.AudioWorkletNode(one, "opus-player").connect(destination);
            new window.AudioWorkletNode(one, "other-player").connect(destination);
            two.createScriptProcessor(4096, 1, 1).connect(destination);  // no handler: not held
            audio().sizes.held;
        """)
        self.assertEqual(result, 1)

    def test_a_worklet_never_connected_lets_its_context_rest(self):
        result = run("""
            const context = new window.AudioContext();
            new window.AudioWorkletNode(context, "opus-player");
            rest();
            const soon = context.state;
            clock += 31000;
            rest();
            [soon, context.state];
        """)
        self.assertEqual(result, ["running", "suspended"])

    def test_disconnecting_one_output_of_two(self):
        result = run("""
            const context = new window.AudioContext();
            const node = new window.AudioWorkletNode(context, "opus-player");
            const gain = { kind: "gain" };
            node.connect(gain);
            node.connect(destination);
            node.disconnect(gain);
            clock += 31000;
            rest();
            const one = context.state;
            node.disconnect(destination);
            rest();
            [one, context.state];
        """)
        self.assertEqual(result, ["running", "suspended"])

    def test_a_script_processor_holds_rest_off_while_it_processes(self):
        result = run("""
            const context = new window.AudioContext();
            const node = context.createScriptProcessor(4096, 1, 1);
            node.connect(destination);
            clock += 31000;
            rest();
            const idle = context.state;  // no onaudioprocess: nothing plays through it
            context.resume();
            node.onaudioprocess = () => {};
            clock += 31000;
            rest();
            const busy = context.state;
            node.onaudioprocess = null;
            rest();
            [idle, busy, context.state];
        """)
        self.assertEqual(result, ["suspended", "running", "suspended"])

    def test_only_that_context_stays_up(self):
        result = run("""
            const playing = new window.AudioContext();
            const other = new window.AudioContext();
            new window.AudioWorkletNode(playing, "opus-player").connect(destination);
            clock += 31000;
            rest();
            [playing.state, other.state];
        """)
        self.assertEqual(result, ["running", "suspended"])

    def test_processors_go_with_their_context(self):
        # Held weakly, and pruned once their context is closed: nothing builds up.
        result = run("""
            const context = new window.AudioContext();
            new window.AudioWorkletNode(context, "opus-player").connect(destination);
            context.createScriptProcessor(4096, 1, 1);
            new window.AudioWorkletNode(context, "voip-playback");  // a call's: not one of them
            const before = processors.size;
            context.close();
            processing();
            [before, processors.size];
        """)
        self.assertEqual(result, [2, 0])

    def test_no_script_processor_where_there_is_none(self):
        self.assertTrue(run('"createScriptProcessor" in new window.AudioContext();'))
        context = JavaScriptCore.Context()
        fakes = FAKES.replace("  createScriptProcessor() { return new FakeNode(this); }\n", "")
        value = context.evaluate(fakes + IDLE + '"createScriptProcessor" in new window.AudioContext();', -1)
        self.assertIsNone(context.get_exception())
        self.assertFalse(value.to_boolean())  # feature detection still says no


class PlayerReleaseTest(unittest.TestCase):
    """WebKit's media player for the element that started last goes from the desktop's media
    controls once nothing plays: a context's resume() takes it down (bridge.js release)."""

    # Timers that keep their delay, run by advance(ms) in the order they come due; the idle check
    # run by tick(); the person's click (input()); each context's resume() counted.
    LATER = """
        const later = [];
        setTimeout = function (callback, ms) { later.push({ callback, at: clock + (ms || 0) }); return later.length; };
        const ticks = [];
        setInterval = function (callback) { ticks.push(callback); return ticks.length; };
        function tick() { ticks.forEach((callback) => callback()); }  // the idle check, every 30 s
        function advance(ms) {
          const until = clock + ms;
          for (;;) {
            const due = later.filter((timer) => timer.at <= until).sort((a, b) => a.at - b.at)[0];
            if (!due) break;
            later.splice(later.indexOf(due), 1);
            clock = Math.max(clock, due.at);
            due.callback();
          }
          clock = until;
        }
        const heard = {};
        window.addEventListener = function (type, listener) { (heard[type] = heard[type] || []).push(listener); };
        function input(trusted = true, type = "click", target = null) {
          (heard[type] || []).forEach((listener) => listener({ isTrusted: trusted, target }));
        }
        let resumes = 0;
        function counted(context) {
          const resume = context.resume;
          context.resume = function () { resumes++; return resume.call(this); };
          return context;
        }
        function finish(element) {
          element.paused = true;
          element.ended = true;
          element.fire("pause");
          element.fire("ended");
        }
        function pause(element) {
          element.paused = true;
          element.fire("pause");
        }
    """

    def evaluate(self, *scripts: str):
        """Each script in turn in one context (promises settle in between); the last one's value."""
        context = JavaScriptCore.Context()
        value = None
        for script in (FAKES + self.LATER + IDLE + scripts[0],) + scripts[1:]:
            value = context.evaluate(script, -1)
            exception = context.get_exception()
            if exception is not None:
                raise AssertionError(exception.get_message())
        return json.loads(value.to_json(0))

    def test_an_ended_voice_note_takes_its_player_down(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            finish(note);
            advance(1999);
            const before = resumes;
            advance(1);
            [before, resumes, context.state];
        """)
        self.assertEqual(result, [0, 1, "running"])

    def test_one_the_person_paused_keeps_it_two_minutes(self):
        # Long enough to carry on from the desktop's controls.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            input(true, "keydown");
            clock += 1500;
            pause(note);
            advance(119999);
            const before = resumes;
            advance(1);
            [before, resumes];
        """)
        self.assertEqual(result, [0, 1])

    def test_one_paused_from_outside_keeps_it_half_an_hour(self):
        # The desktop's controls, or Handset pausing it for a phone and playing it again after;
        # Natter's own presses aren't the person's.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            input();  // the person's click started it
            note.play();
            clock += 1501;
            input(false);  // Natter's own press
            pause(note);
            advance(30 * 60000 - 1);
            const before = resumes;
            advance(1);
            [before, resumes];
        """)
        self.assertEqual(result, [0, 1])

    def test_ended_even_when_the_page_rewinds_it(self):
        # WhatsApp's own listeners come first: one that rewinds makes `ended` false.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.addEventListener("pause", () => { note.ended = false; });  // WhatsApp's, from before
            note.play();
            note.paused = true;
            note.ended = true;
            note.fire("pause");
            note.fire("ended");
            advance(2000);
            resumes;
        """)
        self.assertEqual(result, 1)

    def test_a_looping_ringtone_paused_counts_as_ended(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const ringtone = new window.HTMLAudioElement();
            ringtone.loop = true;
            ringtone.play();
            pause(ringtone);
            advance(2000);
            resumes;
        """)
        self.assertEqual(result, 1)

    def test_a_new_source_stops_it_without_a_pause(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            note.paused = true;  // load() sets it, firing no "pause"
            note.fire("emptied");
            advance(2000);
            resumes;
        """)
        self.assertEqual(result, 1)

    def test_not_while_something_plays_or_listens(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            const other = new window.HTMLAudioElement();
            other.play();  // still playing
            note.play();
            finish(note);
            advance(5000);
            const playing = resumes;
            other.paused = true;
            const track = { readyState: "live" };
            context.createMediaStreamSource({ getTracks: () => [track] });  // a voice note being recorded
            note.play();
            finish(note);
            advance(5000);
            const recording = resumes;
            track.readyState = "ended";
            window.__natterRtc = { active: () => true };
            note.play();
            finish(note);
            advance(5000);
            [playing, recording, resumes];
        """)
        self.assertEqual(result, [0, 0, 0])

    def test_one_due_while_something_plays_is_owed(self):
        # A video in the page the bridge never saw start (autoplay): the idle check tries again.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const video = new window.HTMLVideoElement();
            video.paused = false;
            document.querySelectorAll = () => [video];
            const note = new window.HTMLAudioElement();
            note.play();
            finish(note);
            advance(5000);
            tick();
            const playing = resumes;
            video.paused = true;
            tick();
            [playing, resumes];
        """)
        self.assertEqual(result, [0, 1])

    def test_muted_or_failed_media_holds_nothing(self):
        # WebKit gives a muted element no player; one whose load failed stays unpaused, unended.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const gif = new window.HTMLVideoElement();
            gif.muted = true;
            gif.play();
            const broken = new window.HTMLAudioElement();
            broken.play();
            broken.error = { code: 4 };
            const note = new window.HTMLAudioElement();
            note.play();
            finish(note);
            advance(2000);
            [resumes, audio().sizes.started];
        """)
        self.assertEqual(result, [1, 1])  # the muted one still plays: counted, not held

    def test_one_due_during_a_call_is_owed_until_it_is_over(self):
        # A ringtone paused when the call is answered: its release comes due during the call.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const ringtone = new window.HTMLAudioElement();
            ringtone.loop = true;
            let onCall = false;
            window.__natterRtc = { active: () => onCall };
            ringtone.play();
            onCall = true;
            pause(ringtone);
            advance(5000);
            tick();
            const during = resumes;
            onCall = false;
            tick();
            const after = resumes;
            tick();
            [during, after, resumes];
        """)
        self.assertEqual(result, [0, 1, 1])

    def test_a_context_natter_rested_rests_again(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            input();
            pause(note);
            advance(60000);
            rest();  // nothing for a minute: suspended by Natter
            advance(60000);  // the paused note's two minutes are up
            [resumes, context.state];
        """, "[resumes, context.state, rested.has(context)];")
        self.assertEqual(result, [1, "suspended", True])

    def test_one_that_starts_meanwhile_stays_up(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            input();
            pause(note);
            advance(60000);
            rest();
            advance(60000);  // resume() asked for; before it's done, a sound starts
            clock += 10;
            new window.AudioScheduledSourceNode(context).start();
        """, "[resumes, context.state];")
        self.assertEqual(result, [1, "running"])

    def test_a_context_the_page_suspended_is_left_alone(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            context.suspend();  // WhatsApp's own doing, not Natter's
            finish(note);
            advance(5000);
            [resumes, context.state];
        """)
        self.assertEqual(result, [0, "suspended"])

    def test_played_again_from_the_desktop_controls(self):
        # WebKit plays it with no play() from the page: its "play" event still counts, and holds
        # another element's release off while it plays.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            pause(note);
            advance(60000);
            note.paused = false;
            note.fire("play");  // the desktop's Play
            const tone = new window.HTMLAudioElement();
            tone.play();
            finish(tone);  // the tone had the player last; the note plays on
            advance(30 * 60000);
            tick();
            const playing = resumes;
            finish(note);
            tick();
            [playing, resumes];
        """)
        self.assertEqual(result, [0, 1])

    def test_typing_is_not_the_persons_pause(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            input(true, "keydown", { isContentEditable: true });  // the message box
            input(true, "keydown", { tagName: "INPUT" });  // search
            pause(note);  // from outside, a moment after
            advance(120000);
            resumes;
        """)
        self.assertEqual(result, 0)

    def test_media_with_no_source_holds_nothing(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const empty = new window.HTMLAudioElement();
            empty.networkState = 0;  // NETWORK_EMPTY: play() with no src, never playing
            empty.play();
            const note = new window.HTMLAudioElement();
            note.play();
            finish(note);
            advance(2000);
            resumes;
        """)
        self.assertEqual(result, 1)

    def test_a_failed_element_holds_the_idle_audio_off_no_more(self):
        result = self.evaluate("""
            const context = new window.AudioContext();
            const failed = new window.HTMLAudioElement();
            context.createMediaElementSource(failed);
            failed.play();
            failed.error = { code: 4 };  // WebKit leaves paused false
            const inPage = audio().media;
            failed.isConnected = false;
            [inPage, audio().sizes.elements];
        """)
        self.assertEqual(result, [False, 0])

    def test_one_whose_data_comes_later_takes_the_player_back(self):
        # WebKit makes it current again when its data comes (its play() promise settles then).
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            let ready;
            note.later = new Promise((resolve) => { ready = resolve; });
            note.play();  // before its data
            const tone = new window.HTMLAudioElement();
            tone.play();
            finish(tone);
            advance(2000);  // the note plays on: owed
        """, "ready();", """
            tick();
            input();
            pause(note);
            tick();
            advance(119999);
            const before = resumes;
            advance(1);
            [before, resumes];
        """)
        self.assertEqual(result, [0, 1])

    def test_a_refused_play_leaves_the_player_where_it_was(self):
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            const tone = new window.HTMLAudioElement();
            tone.refuse = "NotAllowedError";
            tone.play();  // never plays: the note keeps its player
            input();
            pause(note);
            advance(120000);
            resumes;
        """)
        self.assertEqual(result, 1)

    def test_an_element_used_again(self):
        # A new source and play(): "emptied" comes before "play", which cancels its release.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            note.play();
            finish(note);
            note.ended = false;
            note.play();
            note.fire("emptied");
            note.fire("play");
            advance(5000);
            resumes;
        """)
        self.assertEqual(result, 0)

    def test_a_new_start_settles_what_was_owed(self):
        # Owed while unwatched media plays; a note started then is the one with the player, and
        # its pause gets its full two minutes.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const video = new window.HTMLVideoElement();
            video.paused = false;
            document.querySelectorAll = () => [video];
            const tone = new window.HTMLAudioElement();
            tone.play();
            finish(tone);
            advance(2000);  // owed
            const note = new window.HTMLAudioElement();
            note.play();
            video.paused = true;
            input();
            pause(note);
            tick();
            const early = resumes;
            advance(120000);
            [early, resumes];
        """)
        self.assertEqual(result, [0, 1])

    def test_only_the_element_that_started_last_counts(self):
        # Only it has a player: an earlier one stopping later neither releases nor cuts its time.
        result = self.evaluate("""
            const context = counted(new window.AudioContext());
            const note = new window.HTMLAudioElement();
            const tone = new window.HTMLAudioElement();
            note.play();
            tone.play();
            input();
            pause(tone);  // two minutes
            finish(note);
            advance(110000);
            const early = resumes;
            advance(10000);
            const due = resumes;
            advance(30 * 60000);
            [early, due, resumes];
        """)
        self.assertEqual(result, [0, 1, 1])


class HeardTest(unittest.TestCase):
    """What the page tells the log about its sound."""

    def test_a_voice_message_that_plays(self):
        posted = run("""
            const element = new window.HTMLAudioElement();
            element.currentSrc = URL.createObjectURL(new Blob("audio/ogg; codecs=opus"));
            element.play();
            element.fire("playing");
            runTimers();
            posted;
        """, heard=True)
        self.assertEqual(posted, [
            {"type": "media", "what": "support",
             "play": {"ogg-opus": "probably", "ogg": "maybe", "webm-opus": "", "mp4-opus": "", "mp4-aac": "",
                      "mpeg": "probably"},
             "source": {"ogg-opus": False, "ogg": False, "webm-opus": True, "mp4-opus": True, "mp4-aac": True,
                        "mpeg": False}},
            {"type": "media", "what": "played", "kind": "audio", "route": "element", "source": "audio/ogg", "state": ""},
        ])

    def test_an_error(self):
        posted = run("""
            const element = new window.HTMLAudioElement();
            element.currentSrc = URL.createObjectURL(new Blob("audio/ogg"));
            element.play();
            element.error = { code: 4, message: "no decoder for https://somewhere" };
            element.fire("error");
            posted.slice(1);
        """, heard=True)
        self.assertEqual(posted, [{"type": "media", "what": "error", "code": 4, "kind": "audio", "route": "element",
                                   "source": "audio/ogg", "state": ""}])

    def test_refused_but_not_aborted(self):
        # The rejections land once evaluate returns: they're read in a second evaluation.
        context = JavaScriptCore.Context()
        context.evaluate(FAKES + IDLE + HEARD + """
            const element = new window.HTMLAudioElement();
            element.currentSrc = "https://static.example/chime.mp3";
            element.refuse = "AbortError";
            element.play();
            element.refuse = "NotAllowedError";
            element.play();
        """, -1)
        self.assertIsNone(context.get_exception())
        posted = json.loads(context.evaluate("JSON.stringify(posted.slice(1))", -1).to_string())
        self.assertEqual(posted, [{"type": "media", "what": "refused", "name": "NotAllowedError", "kind": "audio",
                                   "route": "element", "source": "https", "state": ""}])

    def test_through_a_context_that_stays_suspended(self):
        posted = run("""
            const context = new window.AudioContext();
            const element = new window.HTMLAudioElement();
            element.currentSrc = URL.createObjectURL(new window.MediaSource());
            context.createMediaElementSource(element);
            clock += 60000;
            rest();
            stuck = true;  // resume() does nothing
            element.play();
            element.fire("play");
            element.fire("playing");
            runTimers();
            posted.slice(1);
        """, heard=True)
        self.assertEqual(posted, [
            {"type": "media", "what": "played", "kind": "audio", "route": "webaudio", "source": "mediasource",
             "state": "suspended"},
            {"type": "media", "what": "silent", "kind": "audio", "route": "webaudio", "source": "mediasource",
             "state": "suspended"},
        ])

    def test_through_a_context_that_wakes(self):
        posted = run("""
            const context = new window.AudioContext();
            const element = new window.HTMLAudioElement();
            context.createMediaElementSource(element);
            clock += 60000;
            rest();
            element.play();
            element.fire("playing");
            runTimers();
            posted.slice(1).map((message) => [message.what, message.state]);
        """, heard=True)
        self.assertEqual(posted, [["played", "running"]])  # Natter's play() woke it: nothing more said

    def test_repeats_are_told_once(self):
        posted = run("""
            for (let i = 0; i < 40; i++) {
              const element = new window.HTMLAudioElement();
              element.play();
              element.fire("playing");  // the same way each time: told once
              element.error = { code: 3 };
              element.fire("error");  // the same error each time: told once
            }
            posted.map((message) => message.what);
        """, heard=True)
        self.assertEqual(posted, ["support", "played", "error"])

    def test_repeats_leave_room_for_what_comes_later(self):
        # A page open for days: a new failure after hundreds of the same one is still told.
        posted = run("""
            for (let i = 0; i < 500; i++) {
              const element = new window.HTMLAudioElement();
              element.play();
              element.error = { code: 3 };
              element.fire("error");
            }
            const element = new window.HTMLAudioElement();
            element.error = { code: 4 };
            element.play();
            element.fire("error");
            posted.map((message) => [message.what, message.code]);
        """, heard=True)
        self.assertEqual(posted, [["support", None], ["error", 3], ["error", 4]])

    def test_a_few_lines_a_page(self):
        posted = run("""
            const types = ["audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/aac", "audio/webm",
                           "audio/wav", "audio/amr", "audio/3gpp", "audio/flac"];
            for (let i = 0; i < 40; i++) {
              const element = new window.HTMLAudioElement();
              element.currentSrc = URL.createObjectURL(new Blob(types[i % types.length]));
              element.play();
              element.fire("playing");  // a new source each time: each a line, up to the limit
              element.error = { code: i % 5 };
              element.fire("error");
            }
            posted.map((message) => message.what);
        """, heard=True)
        self.assertEqual(posted[:3], ["support", "played", "error"])
        self.assertEqual(len(posted), 12)
        self.assertEqual(posted.count("support"), 1)

    def test_the_source_tells_lines_apart(self):
        posted = run("""
            for (const type of ["audio/ogg", "audio/ogg", "audio/mpeg"]) {
              const element = new window.HTMLAudioElement();
              element.currentSrc = URL.createObjectURL(new Blob(type));
              element.play();
              element.fire("playing");
              element.error = { code: 4 };
              element.fire("error");
            }
            posted.slice(1).map((message) => [message.what, message.source]);
        """, heard=True)
        self.assertEqual(posted, [["played", "audio/ogg"], ["error", "audio/ogg"], ["played", "audio/mpeg"],
                                  ["error", "audio/mpeg"]])

    def test_a_video_does_not_hide_an_audio(self):
        # An autoplaying GIF (a <video>) told first leaves room for the voice message's line.
        posted = run("""
            for (const element of [new window.HTMLVideoElement(), new window.HTMLAudioElement()]) {
              element.currentSrc = "https://example.org/x";
              element.play();
              element.fire("playing");
            }
            posted.slice(1).map((message) => message.kind);
        """, heard=True)
        self.assertEqual(posted, ["video", "audio"])

    def test_a_type_from_the_sender_is_not_told(self):
        posted = run("""
            for (const type of ["audio/x-secret-person", "video/x-secret-person", "audio/ogg", "text/plain"]) {
              const element = new window.HTMLAudioElement();
              element.currentSrc = URL.createObjectURL(new Blob(type));
              element.play();
              element.fire("playing");
            }
            posted.slice(1).map((message) => message.source);
        """, heard=True)
        self.assertEqual(posted, ["audio-other", "video-other", "audio/ogg", "blob"])

    def test_the_type_lists_match(self):
        listed = BRIDGE[BRIDGE.index("const MEDIA_TYPES = ["):]
        listed = listed[listed.index("[") + 1:listed.index("]")]
        self.assertEqual(tuple(json.loads(f"[{listed}]")), playback.MEDIA_TYPES)

    def test_a_voice_note_resumed_after_a_pause(self):
        # Told once a page for each answer: whether Natter rested the context meanwhile, its
        # state a moment on and whether its clock moved.
        posted = run("""
            const context = new window.AudioContext();
            context.currentTime = 0;
            rest();  // rested before the voice note: not meanwhile
            const recorder = context.createScriptProcessor(4096, 1, 1);
            recorder.onaudioprocess = () => {};
            const first = { readyState: "live" };
            context.createMediaStreamSource({ getTracks: () => [first] }).connect(recorder);
            recorder.connect(destination);
            runTimers();
            first.readyState = "ended";
            clock += 30000;
            rest();  // a short pause: held, not rested
            const second = { readyState: "live" };
            context.createMediaStreamSource({ getTracks: () => [second] }).connect(recorder);
            context.currentTime = 1.5;
            runTimers();
            second.readyState = "ended";
            clock += 30000;
            rest();
            clock += 700000;
            rest();  // a long one: rested
            const third = { readyState: "live" };
            context.createMediaStreamSource({ getTracks: () => [third] }).connect(recorder);
            runTimers();  // its clock stands still
            posted.filter((message) => message.what === "resumed");
        """, heard=True)
        self.assertEqual(posted, [
            {"type": "media", "what": "resumed", "state": "running", "rested": False, "moving": True},
            {"type": "media", "what": "resumed", "state": "running", "rested": True, "moving": False},
        ])

    def test_a_page_player_connected(self):
        # Sound decoded in wasm or a worker plays through one of these, with no other line.
        posted = run("""
            const context = new window.AudioContext();
            clock += 31000;
            rest();
            stuck = true;  // the context never wakes
            const player = new window.AudioWorkletNode(context, "opus-player");
            player.connect(destination);
            player.connect(destination);  // told once
            new window.AudioWorkletNode(context, "voip-playback").connect(destination);  // a call's: not told
            const script = context.createScriptProcessor(4096, 1, 1);
            script.connect(destination);
            new window.AudioWorkletNode(context, "never-connected");
            runTimers();
            posted.slice(1);
        """, heard=True)
        self.assertEqual(posted, [{"type": "media", "what": "processor", "kind": "worklet", "state": "suspended"},
                                  {"type": "media", "what": "processor", "kind": "script", "state": "suspended"}])

    def test_refused_once_for_the_same_name_and_source(self):
        context = JavaScriptCore.Context()
        context.evaluate(FAKES + IDLE + HEARD + """
            for (let i = 0; i < 30; i++) {
              const element = new window.HTMLAudioElement();
              element.currentSrc = "https://static.example/chime.mp3";
              element.refuse = "NotAllowedError";
              element.play();
            }
            const other = new window.HTMLAudioElement();
            other.refuse = "NotSupportedError";
            other.play();
        """, -1)
        self.assertIsNone(context.get_exception())
        posted = json.loads(context.evaluate(
            "JSON.stringify(posted.slice(1).map((message) => [message.name, message.source]))", -1).to_string())
        self.assertEqual(posted, [["NotAllowedError", "https"], ["NotSupportedError", "none"]])

    def later(self, script: str, then: str):
        """`script`, then `then` once its promises have settled (a second evaluation)."""
        context = JavaScriptCore.Context()
        context.evaluate(FAKES + IDLE + HEARD + script, -1)
        self.assertIsNone(context.get_exception(), context.get_exception() and context.get_exception().get_message())
        value = context.evaluate(then, -1)
        self.assertIsNone(context.get_exception(), context.get_exception() and context.get_exception().get_message())
        return json.loads(value.to_json(0))

    def test_decode_audio_data(self):
        posted = self.later("""
            const context = new window.AudioContext();
            let gotBuffer = null, gotError = null;
            context.decodeAudioData("voice").then((buffer) => { gotBuffer = buffer; });
            context.decodeAudioData("voice", () => {});  // the callback form: the same answer, told once
            context.decodeAudioData("bad").catch((error) => { gotError = error.name; });
            context.decodeAudioData("bad", null, () => {});
        """, "[posted.slice(1), gotBuffer.decodedFrom, gotError];")
        self.assertEqual(posted, [[{"type": "media", "what": "decode", "result": "resolved", "name": ""},
                                   {"type": "media", "what": "decode", "result": "rejected", "name": "EncodingError"}],
                                  "voice", "EncodingError"])  # the page's own answers are untouched

    def test_a_decoded_sound_started(self):
        posted = self.later("""
            const context = new window.AudioContext();
            let decodedBuffer = null;
            context.decodeAudioData("voice").then((buffer) => { decodedBuffer = buffer; });
            const chime = new window.AudioScheduledSourceNode(context);
            chime.buffer = { duration: 1 };  // not decoded here: nothing told
            chime.start();
        """, """
            clock += 60000;
            rest();  // suspended; Natter's start() wakes it
            for (let i = 0; i < 3; i++) {
              const node = new window.AudioScheduledSourceNode(context);
              node.buffer = decodedBuffer;
              node.start();
            }
            runTimers();
            posted.slice(1).map((message) => [message.what, message.state || message.result]);
        """)
        self.assertEqual(posted, [["decode", "resolved"], ["started", "running"]])

    def test_a_decoded_sound_in_a_context_that_stays_suspended(self):
        posted = self.later("""
            const context = new window.AudioContext();
            let decodedBuffer = null;
            context.decodeAudioData("voice", (buffer) => { decodedBuffer = buffer; });
        """, """
            clock += 60000;
            rest();
            stuck = true;
            const node = new window.AudioScheduledSourceNode(context);
            node.buffer = decodedBuffer;
            node.start();
            runTimers();
            posted.slice(1).map((message) => [message.what, message.state || message.result]);
        """)
        self.assertEqual(posted, [["decode", "resolved"], ["started", "suspended"]])

    def test_an_opus_audio_decoder(self):
        posted = self.later("""
            let pageErrors = 0;
            window.AudioDecoder.isConfigSupported({ codec: "opus", sampleRate: 48000, numberOfChannels: 1 });
            window.AudioDecoder.isConfigSupported({ codec: "mp4a.40.2" });  // not Opus: not told
            const decoder = new window.AudioDecoder({ output() {}, error() { pageErrors++; } });
            decoder.configure({ codec: "opus", sampleRate: 48000, numberOfChannels: 1 });
            decoder.fail("EncodingError");
            decoder.fail("EncodingError");  // told once
            const aac = new window.AudioDecoder({ output() {}, error() { pageErrors++; } });
            aac.configure({ codec: "mp4a.40.2" });
            aac.fail("DataError");  // not Opus: not told
            const broken = new window.AudioDecoder({ output() {}, error() {} });
            let threw = "";
            try { broken.configure({ codec: "opus", bad: true }); } catch (error) { threw = error.name; }
            [decoder instanceof window.AudioDecoder, threw, pageErrors];
        """, "[posted.slice(1).map((message) => [message.what, message.step, message.name])];")
        self.assertEqual(posted, [[["decoder", "configured", ""], ["decoder", "error", "EncodingError"],
                                   ["decoder", "configure-failed", "NotSupportedError"],
                                   ["decoder", "supported", ""]]])

    def test_the_page_still_gets_its_decoders_answers(self):
        result = self.later("""
            let pageErrors = 0, supported = null, ok = false;
            window.AudioDecoder.isConfigSupported({ codec: "opus" }).then((answer) => { supported = answer.supported; });
            const decoder = new window.AudioDecoder({ output() {}, error() { pageErrors++; } });
            decoder.configure({ codec: "opus" });
            decoder.fail("EncodingError");
            decoder.fail("EncodingError");
            ok = decoder.state === "configured" && decoder instanceof window.AudioDecoder;
        """, "[pageErrors, supported, ok];")
        self.assertEqual(result, [2, True, True])

    def test_kinds_and_codes_only(self):
        posted = run("""
            const element = new window.HTMLAudioElement();
            element.currentSrc = URL.createObjectURL(new Blob("audio/ogg; codecs=opus; name=Secret Person"));
            element.play();
            element.fire("playing");
            element.error = { code: 3, message: "Secret Person's https://mmg.whatsapp.net/x" };
            element.fire("error");
            const video = new window.HTMLVideoElement();
            video.src = "data:video/mp4;base64,AAAA";
            video.play();
            video.fire("playing");
            const context = new window.AudioContext();
            context.decodeAudioData("bad", null, () => {});
            const decoder = new window.AudioDecoder({ output() {}, error() {} });
            decoder.configure({ codec: "opus" });
            decoder.fail("Secret Person https://x");  // not a name: left out
            const sent = new window.HTMLAudioElement();  // a type the sender wrote
            sent.currentSrc = URL.createObjectURL(new Blob("audio/x-person-0044-7700"));
            sent.play();
            sent.fire("playing");
            JSON.stringify(posted);
        """, heard=True)
        self.assertIn("audio-other", posted)
        for secret in ("Secret", "blob:", "http", "whatsapp", "base64", "person", "7700"):
            self.assertNotIn(secret, posted)

    def test_nothing_of_whatsapp_or_storage(self):
        for banned in ("localStorage", "sessionStorage", "indexedDB", "require(", "__d(", "webpack", "error.message",
                       "querySelector"):
            self.assertNotIn(banned, HEARD)


class ParseTest(unittest.TestCase):
    """playback.py takes only what the bridge sends, from its lists."""

    SUPPORT = {"type": "media", "what": "support",
               "play": {key: "probably" for key in playback.SUPPORT_KEYS},
               "source": {key: False for key in playback.SUPPORT_KEYS}}
    PLAYED = {"type": "media", "what": "played", "kind": "audio", "route": "webaudio", "source": "audio/ogg",
              "state": "running"}

    def test_through_parse_message(self):
        self.assertEqual(parse_message({**self.PLAYED, "token": "t"}), self.PLAYED)
        self.assertEqual(parse_message(self.SUPPORT), self.SUPPORT)
        error = {**self.PLAYED, "what": "error", "code": 3, "route": "element", "state": ""}
        self.assertEqual(parse_message(error), error)
        refused = {**self.PLAYED, "what": "refused", "name": "NotAllowedError"}
        self.assertEqual(parse_message(refused), refused)
        resumed = {"type": "media", "what": "resumed", "state": "running", "rested": True, "moving": False}
        self.assertEqual(parse_message(resumed), resumed)
        wake = {"type": "media", "what": "wake", "name": "InvalidStateError"}
        self.assertEqual(parse_message(wake), wake)
        for change in ({"state": ""}, {"state": "asleep"}, {"rested": "yes"}, {"moving": 1}, {"rested": None}):
            self.assertIsNone(parse_message({**resumed, **change}), change)
        for name in ("Not allowed", "x" * 41, 7, None):
            self.assertIsNone(parse_message({**wake, "name": name}), name)

    def test_anything_else_is_dropped(self):
        for change in ({"what": "sang"}, {"kind": "img"}, {"route": "speakers"}, {"state": "asleep"},
                       {"source": "blob:https://web.whatsapp.com/abc"}, {"source": "https://mmg.whatsapp.net/v"},
                       {"source": "audio/ogg; name=Someone"}, {"source": 3}, {"state": ""},
                       {"route": "element"}):
            self.assertIsNone(parse_message({**self.PLAYED, **change}), change)
        for change in ({"code": 5}, {"code": "4"}, {"code": True}, {}):
            self.assertIsNone(parse_message({**self.PLAYED, "what": "error", **change}), change)
        for name in ("Not allowed", "x" * 41, 7, None):
            self.assertIsNone(parse_message({**self.PLAYED, "what": "refused", "name": name}), name)
        self.assertIsNone(parse_message({**self.SUPPORT, "play": {**self.SUPPORT["play"], "ogg": "yes"}}))
        self.assertIsNone(parse_message({**self.SUPPORT, "source": {**self.SUPPORT["source"], "ogg": "no"}}))
        self.assertIsNone(parse_message({**self.SUPPORT, "play": []}))

    def test_lines(self):
        self.assertEqual(
            playback.line(parse_message({**self.SUPPORT, "play": {**self.SUPPORT["play"], "ogg": "maybe", "mpeg": ""},
                                         "source": {**self.SUPPORT["source"], "webm-opus": True}}),
                          "WebKitGTK 2.54.2, GStreamer 1.28.8"),
            "media: WebKitGTK 2.54.2, GStreamer 1.28.8; <audio> plays ogg-opus probably, ogg maybe, webm-opus probably, "
            "mp4-opus probably, mp4-aac probably, mpeg no; MediaSource takes webm-opus")
        self.assertEqual(playback.line(self.PLAYED),
                         "media: sound played: an <audio> (audio/ogg), through a Web Audio context, running")
        self.assertEqual(playback.line({**self.PLAYED, "what": "error", "code": 4, "route": "element", "state": ""}),
                         "media: an <audio> (audio/ogg), from the element itself failed: MediaError 4 "
                         "(source not supported)")
        self.assertEqual(playback.line({**self.PLAYED, "what": "refused", "name": "NotAllowedError"}),
                         "media: play() refused for an <audio> (audio/ogg), through a Web Audio context, running: "
                         "NotAllowedError")
        self.assertIn("the context isn't running", playback.line({**self.PLAYED, "what": "silent",
                                                                  "state": "suspended"}))

    def test_decoding(self):
        decode = {"type": "media", "what": "decode", "result": "rejected", "name": "EncodingError"}
        decoder = {"type": "media", "what": "decoder", "step": "error", "name": "EncodingError"}
        started = {"type": "media", "what": "started", "state": "suspended"}
        for message in (decode, decoder, started, {**decode, "result": "resolved", "name": ""},
                        {**decoder, "step": "configured", "name": ""}, {**decoder, "step": "configure-failed"}):
            self.assertEqual(parse_message({**message, "token": "t", "extra": "Secret"}), message)
        for message in ({**decode, "result": "maybe"}, {**decode, "name": "Secret Person"},
                        {**decode, "result": "resolved"}, {**decoder, "step": "decoded"},
                        {**decoder, "step": "supported"}, {**decoder, "name": None}, {**started, "state": ""},
                        {**started, "state": "asleep"}, {"type": "media", "what": "decode"}):
            self.assertIsNone(parse_message(message), message)
        self.assertEqual(playback.line(decode), "media: decodeAudioData rejected: EncodingError")
        self.assertEqual(playback.line({**decode, "result": "resolved", "name": ""}), "media: decodeAudioData resolved")
        self.assertEqual(playback.line(decoder), "media: WebCodecs AudioDecoder (opus): an error: EncodingError")
        self.assertEqual(playback.line({**decoder, "step": "unsupported", "name": ""}),
                         "media: WebCodecs AudioDecoder (opus): isConfigSupported() says no")
        self.assertEqual(playback.line(started), "media: a decoded sound started (AudioBufferSourceNode); its Web "
                                                 "Audio context 1.5 s on: suspended")

    def test_a_voice_note_resumed_and_a_wake_that_failed(self):
        resumed = {"type": "media", "what": "resumed", "state": "running", "rested": True, "moving": False}
        self.assertEqual(playback.line(resumed),
                         "media: a voice recorder fed by the microphone again (a voice note resumed after a pause, "
                         "or the next one in the same recorder); its Web Audio context (rested by the idle audio "
                         "after 10 minutes quiet) 2.5 s on: running, its clock not moving, so nothing is recorded")
        self.assertEqual(playback.line({**resumed, "rested": False, "moving": True}),
                         "media: a voice recorder fed by the microphone again (a voice note resumed after a pause, "
                         "or the next one in the same recorder); its Web Audio context (kept running) 2.5 s on: "
                         "running, its clock moving")
        self.assertEqual(playback.line({"type": "media", "what": "wake", "name": "InvalidStateError"}),
                         "media: a Web Audio context the idle audio rested didn't resume, twice: InvalidStateError")

    def test_a_page_player(self):
        message = {"type": "media", "what": "processor", "kind": "worklet", "state": "running"}
        self.assertEqual(parse_message({**message, "token": "t", "name": "Secret"}), message)
        for change in ({"kind": "recorder"}, {"kind": None}, {"state": ""}, {"state": "asleep"}):
            self.assertIsNone(parse_message({**message, **change}), change)
        self.assertEqual(playback.line(message), "media: the page's own player (an AudioWorkletNode) connected; "
                                                 "its Web Audio context 1.5 s on: running")
        self.assertIn("(a ScriptProcessorNode)", playback.line({**message, "kind": "script"}))

    def test_only_listed_types(self):
        played = ParseTest.PLAYED
        for source in ("audio/ogg", "video/mp4", "audio-other", "video-other"):
            self.assertEqual(parse_message({**played, "source": source}), {**played, "source": source})
        for source in ("audio/x-secret-person", "video/secret", "audio/", "audio-Other"):
            self.assertIsNone(parse_message({**played, "source": source}), source)


class AccountLineTest(unittest.TestCase):
    """The account writes a few lines a page, with WebKit's and GStreamer's versions on the first."""

    TOKEN = "f" * 32

    def account(self):
        account = SimpleNamespace(_token=self.TOKEN, _trusted=lambda: True, name="one", loaded_at=1.0,
                                  _media_lines=(None, 0))
        account._media_line = lambda message: Account._media_line(account, message)
        return account

    def send(self, account, message):
        value = json.dumps({**message, "token": self.TOKEN})
        Account._on_message(account, None, SimpleNamespace(to_json=lambda _indent: value))

    def test_versions_and_a_limit_a_page(self):
        account = self.account()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("natter.account.media_gstreamer_version", return_value="1.28.8"):
            self.send(account, ParseTest.SUPPORT)
            for _ in range(30):
                self.send(account, ParseTest.PLAYED)
            account.loaded_at = 2.0  # a new page: lines again
            self.send(account, ParseTest.PLAYED)
            self.send(account, {**ParseTest.PLAYED, "source": "blob:https://web.whatsapp.com/x"})  # dropped
        lines = out.getvalue().splitlines()
        self.assertRegex(lines[0], r"^natter: one: media: WebKitGTK \d+\.\d+\.\d+, GStreamer 1\.28\.8; <audio> plays ")
        self.assertEqual(len(lines), playback.LINES_PER_PAGE + 1)
        self.assertTrue(all(line.startswith("natter: one: media: ") for line in lines))


def stream(index, mute=False, volume=65536, role="music", binary="WebKitWebProcess", name="Natter Den"):
    return {"index": index, "sink": 58, "mute": mute, "corked": False,
            "volume": {"front-left": {"value": volume, "value_percent": f"{volume * 100 // 65536}%"},
                       "front-right": {"value": volume, "value_percent": f"{volume * 100 // 65536}%"}},
            "properties": {"application.name": name, "application.process.binary": binary, "media.role": role,
                           "object.id": str(100 + index)}}


class SilentStreamTest(unittest.TestCase):
    """A page stream the sound server opened muted is told, once, and left as it is."""

    def test_silenced(self):
        self.assertEqual(system.silenced(stream(1, mute=True)), "muted")
        self.assertEqual(system.silenced(stream(1, volume=0)), "at 0% volume")
        self.assertEqual(system.silenced(stream(1)), "")
        self.assertEqual(system.silenced({"index": 1}), "")
        self.assertEqual(system.silenced({"index": 1, "volume": {"mono": {"value": 0}, "x": "?"}}), "")

    def test_lines(self):
        self.assertEqual(system.silent_line(stream(1, mute=True), "muted"),
                         "natter: sound: a sound stream of the page with media.role music (<audio>, perhaps Web Audio "
                         "too: voice messages may play through either) is muted "
                         "by the sound server, which can give every app's music streams what it remembers for that "
                         "role: unmute or raise it in the desktop's sound settings while one plays")
        self.assertIn("(Web Audio: calls, and sound the page decodes itself)",
                      system.silent_line(stream(1, role="webaudio"), "muted"))
        self.assertIn("without a role", system.silent_line(stream(1, role=""), "muted"))
        self.assertIn("media.role musicSecretx is", system.silent_line(stream(1, role="music Secret \"x\""), "muted"))

    def test_follow_tells_once_and_changes_nothing(self):
        follower = system.AudioFollower(app_name="Natter Den", app_id="io.github.Officialbabs.Natter")
        follower.pactl, follower.metadata = "/usr/bin/pactl", "/usr/bin/pw-metadata"
        streams = [stream(1, mute=True), stream(2), stream(3, mute=True, binary="firefox", name="Firefox")]
        answers = {("get-default-sink",): "out\n",
                   ("-f", "json", "list", "sinks", "short"): json.dumps([{"index": 58, "name": "out"}]),
                   ("-f", "json", "list", "sink-inputs"): json.dumps(streams)}
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(follower, "_pactl", side_effect=lambda *args: answers[args]), \
                mock.patch("natter.system.subprocess.run") as ran:
            self.assertEqual(follower.follow(), 0)
            follower.follow()
        self.assertEqual(out.getvalue().count("natter: sound: "), 1)  # stream 1 alone, and once
        self.assertIn("is muted", out.getvalue())
        ran.assert_not_called()  # nothing unmuted, nothing moved (all on the default)


class StartUpCheckTest(unittest.TestCase):
    def test_the_players_sound_chain(self):
        names = {name for _, names in system.PLAYERS for name in names}
        self.assertLessEqual({"audioconvert", "audioresample", "volume", "scaletempo"}, names)

    def test_a_missing_volume_is_told(self):
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ValueError, ImportError):
            self.skipTest("needs GStreamer for Python")
        Gst.init(None)
        find = Gst.ElementFactory.find
        with mock.patch.object(Gst.ElementFactory, "find", lambda name: None if name == "volume" else find(name)):
            missing, _hint = system.media_report({"ID": "arch"})
        self.assertIn("the player's sound (audioconvert, audioresample, volume)", missing)


if __name__ == "__main__":
    unittest.main()
