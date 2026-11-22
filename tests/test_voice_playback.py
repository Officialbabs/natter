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
import re
import struct
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("JavaScriptCore", "6.0")
gi.require_version("WebKit", "6.0")
from gi.repository import JavaScriptCore, WebKit  # noqa: E402

from natter import playback, system  # noqa: E402
from natter.account import Account, parse_message  # noqa: E402

BRIDGE = (Path(__file__).resolve().parent.parent / "natter" / "bridge.js").read_text()
# The idle audio, and Natter's worklet recorder it asks for script processors.
IDLE = (BRIDGE[BRIDGE.index("  // ---- Idle audio"):BRIDGE.index("  // ---- Videos aren't pictures")]
        + BRIDGE[BRIDGE.index("  // ---- A recorder a late main thread"):BRIDGE.index("  // ---- A voice message being")])
HEARD = BRIDGE[BRIDGE.index("  // ---- Sound that can't be heard"):BRIDGE.index("  // ---- Opening a chat")]

# A page for the idle-audio section and the one after: contexts that suspend and resume (or, with
# stuck = true, never resume), worklets, script processors, element sources, media elements that
# play, fail or are refused, blobs and object URLs, timers run by hand, and a MessageChannel whose
# messages are tasks of their own, run between turns (turns()) or by hand (runTasks).
FAKES = """
var window = globalThis;
let clock = 100000;
var performance = { now: () => clock };
const timers = [];
function setTimeout(callback) { timers.push(callback); return timers.length; }
function setInterval() { return 0; }
function clearInterval() {}
function runTimers() { timers.splice(0).forEach((callback) => callback()); }
const tasks = [];
function runTasks() { tasks.splice(0).forEach((task) => task()); }
var MessageChannel = class {
  constructor() {
    const port1 = { onmessage: null };
    this.port1 = port1;
    this.port2 = { postMessage(data) { tasks.push(() => { if (port1.onmessage) port1.onmessage({ data }); }); } };
  }
};
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
  dispatchEvent(event) { (this.listeners[event.type] || []).slice().forEach((listener) => listener.call(this, event)); return true; }
}
window.EventTarget = Target;
class NativeElementSource { constructor(context, options) { this.context = context; this.mediaElement = options.mediaElement; } }
class FakeNode extends Target {
  constructor(context) { super(); this.context = context; this.connections = 0; this.onaudioprocess = null; }
  connect(destination) { this.connections++; return destination; }
  disconnect() { this.connections = 0; }
}
const destination = { kind: "destination" };
window.AudioContext = class extends Target {
  constructor() { super(); this.state = "running"; this.sampleRate = 48000; }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
  resume() { if (!stuck) this.state = "running"; return Promise.resolve(); }
  close() { this.state = "closed"; this.fire("statechange"); return Promise.resolve(); }
  createMediaStreamSource(stream) { const node = new FakeNode(this); node.mediaStream = stream; return node; }
  createMediaElementSource(element) { return new NativeElementSource(this, { mediaElement: element }); }
  createScriptProcessor(size = 4096) { return Object.assign(new FakeNode(this), { bufferSize: size }); }
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
    this.plays = (this.plays || 0) + 1;
    this.paused = false;
    return this.later || Promise.resolve();  // a test's own: its data comes when it says
  }
};
window.HTMLAudioElement = class extends window.HTMLMediaElement {
  canPlayType(type) { return /ogg/.test(type) ? (/opus/.test(type) ? "probably" : "maybe") : /mpeg/.test(type) ? "probably" : ""; }
};
window.HTMLVideoElement = class extends window.HTMLMediaElement {};
var HTMLMediaElement = window.HTMLMediaElement;
var document = Object.assign(new Target(), { hidden: false, querySelectorAll: () => [],
                                             createElement: () => new window.HTMLAudioElement() });
window.MediaSource = class { static isTypeSupported(type) { return /webm|mp4/.test(type); } };
var Blob = class { constructor(type) { this.type = type; } };
let urls = 0;
var URL = { createObjectURL: () => `blob:https://web.whatsapp.com/${++urls}-secret`, revokeObjectURL() {} };
const shown = new Map();
"""


def run(script: str, heard: bool = False, fakes: str = ""):
    """The idle-audio section (and with `heard`, the one after it) in a fresh context, then `script`;
    `fakes`, more stand-ins, before the sections."""
    context = JavaScriptCore.Context()
    value = context.evaluate(FAKES + fakes + IDLE + (HEARD if heard else "") + script, -1)
    exception = context.get_exception()
    if exception is not None:
        raise AssertionError(exception.get_message())
    return json.loads(value.to_json(0))


def turns(*scripts: str, heard: bool = False, fakes: str = ""):
    """As run(), but each script a turn of the page's event loop of its own in the same context (promises
    settle between them, then the tasks queued run, each with its own): the value of each."""
    context = JavaScriptCore.Context()
    values = []
    for script in (FAKES + fakes + IDLE + (HEARD if heard else "") + scripts[0], *scripts[1:]):
        while values and context.evaluate("tasks.length", -1).to_int32():
            context.evaluate("tasks.shift()();", -1)
        value = context.evaluate(script, -1)
        exception = context.get_exception()
        if exception is not None:
            raise AssertionError(exception.get_message())
        values.append(json.loads(value.to_json(0) or "null"))
    return values


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

    def test_an_element_played_by_itself_wakes_no_context(self):
        # WhatsApp's voice messages play from the element itself: they need no context, and waking
        # the ones Natter rested held the page as each opened its sound stream again, just as the
        # element opened its own. One played through a context wakes that one.
        result = run("""
            const context = new window.AudioContext();
            const other = new window.AudioContext();
            rest();
            const note = new window.HTMLAudioElement();
            note.play();
            const alone = [context.state, other.state];
            const routedOne = new window.HTMLAudioElement();
            context.createMediaElementSource(routedOne);
            rest();
            routedOne.play();
            [alone, context.state, other.state];
        """)
        self.assertEqual(result, [["suspended", "suspended"], "running", "suspended"])

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
        fakes = re.sub(r"  createScriptProcessor\(.*\n", "", FAKES)
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

    # A play() WebKitGTK 2.52.6 dropped: WhatsApp's resumed voice message (a new source, the place
    # it paused at, then play() before its data came); then the element says it plays, has its data
    # and no error, and its time stands still. The stand-in element keeps its time where the test
    # puts it.
    STALLED = """
        const note = new window.HTMLAudioElement();
        const resume = (at) => {
          Object.assign(note, { paused: true, ended: false, seeking: false, error: null, srcObject: null,
                                readyState: 0, currentTime: at });
          note.play();
          note.readyState = 4;
        };
        note.currentSrc = URL.createObjectURL(new Blob("audio/ogg"));
        resume(3.9);
    """
    STALLS = "posted.filter((m) => m.what === 'stalled').map((m) => m.then)"

    def test_a_dropped_play_is_asked_again(self):
        posted = run(self.STALLED + """
            runTimers();  // a second on: still standing, asked to play again
            const asked = note.plays;
            note.currentTime = 4.4;  // it plays now
            runTimers();
            [asked, posted.filter((m) => m.what === "stalled")];
        """, heard=True)
        self.assertEqual(posted, [2, [{"type": "media", "what": "stalled", "then": "play", "kind": "audio",
                                       "route": "element", "source": "audio/ogg", "state": ""}]])

    def test_then_its_place_is_nudged(self):
        posted = run(self.STALLED + """
            runTimers();  // asked again: still standing
            runTimers();  // nudged
            const nudged = note.currentTime;
            note.currentTime = 4.5;  // it plays now
            runTimers();
            [nudged, %s];
        """ % self.STALLS, heard=True)
        self.assertEqual(posted, [3.901, ["seek"]])

    def test_one_that_still_stands_is_told(self):
        posted = run(self.STALLED + """
            runTimers(); runTimers(); runTimers();
            [note.plays, %s];
        """ % self.STALLS, heard=True)
        self.assertEqual(posted, [2, ["still"]])

    def test_one_still_settling_is_looked_at_again(self):
        # Its data or its seek still to come a second on: looked at each second, up to five times.
        posted = run(self.STALLED + """
            note.readyState = 1;
            runTimers();
            note.readyState = 4; note.seeking = true;
            runTimers();
            const waited = [note.plays, timers.length];
            note.seeking = false;
            runTimers();  // only just settled: a moment more
            const settling = note.plays;
            runTimers();  // settled and standing: asked to play again
            const asked = note.plays;
            for (let i = 0; i < 3; i++) runTimers();
            note.plays = 0;
            resume(3.9);
            note.readyState = 0;  // never settles: given up after five looks
            const left = [];
            for (let i = 0; i < 5; i++) { runTimers(); left.push(timers.length); }
            [waited, settling, asked, note.plays, left];
        """, heard=True)
        self.assertEqual(posted, [[1, 1], 1, 2, 1, [1, 1, 1, 1, 0]])

    def test_only_a_play_while_it_loads_or_seeks_away_from_the_start_is_looked_at(self):
        # A first play from the start, and a play of one that has its data and isn't seeking.
        posted = run(self.STALLED + """
            for (let i = 0; i < 3; i++) runTimers();
            const plays = [];
            for (const [at, readyState, seeking] of [[0, 0, false], [3.9, 4, false], [3.9, 4, true], [3.9, 2, false]]) {
              Object.assign(note, { paused: true, readyState, seeking, currentTime: at, plays: 0 });
              note.play();
              Object.assign(note, { readyState: 4, seeking: false });
              runTimers();
              plays.push(note.plays);
              for (let i = 0; i < 3; i++) runTimers();
            }
            plays;
        """, heard=True)
        self.assertEqual(posted, [1, 1, 2, 2])

    def test_one_that_plays_or_waits_is_left_alone(self):
        # Its time moves; paused; ended; still seeking or waiting for data, for good; failed; a live
        # stream; or played again since.
        posted = run(self.STALLED + """
            const cases = [
              () => { note.currentTime = 4.2; },
              () => { note.paused = true; },
              () => { note.ended = true; },
              () => { note.seeking = true; },
              () => { note.readyState = 2; },
              () => { note.error = { code: 3 }; },
              () => { note.srcObject = {}; },
              () => { note.play(); note.currentTime = 9; },
            ];
            for (let i = 0; i < 3; i++) runTimers();
            posted.length = 0;
            const asked = [];
            for (const change of cases) {
              note.plays = 0;
              resume(3.9);
              change();
              for (let i = 0; i < 7; i++) runTimers();
              asked.push(note.plays);
            }
            [asked, posted.filter((m) => m.what === "stalled").length];
        """, heard=True)
        self.assertEqual(posted, [[1, 1, 1, 1, 1, 1, 1, 2], 0])

    def test_a_later_play_ends_the_look(self):
        # Played again from 5 (its own look), then its time back where the first play() was:
        # only the later look counts, and its element has moved.
        posted = run(self.STALLED + """
            resume(5);
            note.currentTime = 3.9;
            for (let i = 0; i < 3; i++) runTimers();
            [note.plays, posted.filter((m) => m.what === "stalled").length];
        """, heard=True)
        self.assertEqual(posted, [2, 0])

    def test_a_later_play_ends_a_look_that_asked_again(self):
        # Asked again, then paused and resumed (a new look of its own) before the next step: the
        # earlier look neither nudges nor tells.
        posted = run(self.STALLED + """
            runTimers();  // asked again: its next step waits
            note.plays = 0;
            resume(3.9);
            runTimers();  // the earlier step ends; the new look asks again
            const first = [note.currentTime, note.plays];
            runTimers();  // the new look nudges; an earlier nudge would have come before it
            note.currentTime = 4.5;
            runTimers();
            [first, %s];
        """ % self.STALLS, heard=True)
        self.assertEqual(posted, [[3.9, 2], ["seek"]])

    def test_a_later_play_ends_a_look_that_nudged(self):
        # Nudged, then played again from elsewhere before its last step: the earlier look tells
        # nothing, though the element stands where it was nudged to.
        posted = run(self.STALLED + """
            runTimers(); runTimers();  // asked again, then nudged to 3.901
            resume(5);
            note.currentTime = 3.901;
            for (let i = 0; i < 3; i++) runTimers();
            %s;
        """ % self.STALLS, heard=True)
        self.assertEqual(posted, [])

    def test_a_look_that_went_nowhere_says_nothing(self):
        # Paused after it was asked again, or failing after its nudge: no line claims it started.
        posted = run(self.STALLED + """
            runTimers();
            note.paused = true;
            runTimers();
            const paused = [note.currentTime, timers.length];
            resume(3.9);
            runTimers(); runTimers();
            note.error = { code: 3 };
            runTimers();
            [paused, %s];
        """ % self.STALLS, heard=True)
        self.assertEqual(posted, [[3.9, 0], []])

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
        # (And which script processor it got: WebKit's own, as here there's no AudioWorklet.)
        self.assertEqual(posted, [{"type": "media", "what": "recorder", "path": "native", "why": "none", "module": "",
                                   "name": ""},
                                  {"type": "media", "what": "processor", "kind": "worklet", "state": "suspended"},
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


class VideoSpellTest(unittest.TestCase):
    """How a <video> played, a spell at a time (bridge.js watchVideo): data that came too slowly
    (waits, stalls) told apart from frames not drawn in time (gaps, drops) and a busy main thread."""

    # A status video: a decrypted blob, WebKit's frame callbacks and playback quality, its frames
    # shown when the test says. The video timer (every 100 ms) run by hand; the idle audio's own
    # (every 30 s) isn't one.
    VIDEO = """
        const intervals = new Map();
        let intervalIds = 0;
        setInterval = function (callback, ms) { intervals.set(++intervalIds, { callback, ms }); return intervalIds; };
        clearInterval = function (id) { intervals.delete(id); };
        const videoTimers = () => [...intervals.values()].filter((interval) => interval.ms === 100);
        const ticking = () => videoTimers().length;
        const ticks = () => videoTimers().forEach((interval) => interval.callback());
        class Video extends window.HTMLVideoElement {
          constructor() {
            super();
            Object.assign(this, { videoWidth: 718, videoHeight: 1281, currentTime: 0, readyState: 4, seeking: false,
                                  loop: false, srcObject: null, presented: 0, dropped: 0, shown: 0, asked: 0 });
            this.currentSrc = URL.createObjectURL(new Blob("video/mp4"));
            this.callbacks = new Map();
            this.counts = [0, 0];
          }
          requestVideoFrameCallback(callback) { this.callbacks.set(++this.asked, callback); return this.asked; }
          cancelVideoFrameCallback(id) { this.callbacks.delete(id); }
          // WebKit reads GStreamer's sink when asked (not once it has ended): the frames it dropped
          // and showed since it last started counting, each kept until the sink's is past 0.
          getVideoPlaybackQuality() {
            if (!this.ended && this.dropped) this.counts[0] = this.dropped;
            if (!this.ended && this.shown) this.counts[1] = this.shown;
            return { droppedVideoFrames: this.counts[0], totalVideoFrames: this.counts[1] };
          }
          recount() { this.dropped = this.shown = 0; }  // the sink counts from 0 again: a pause, a seek, a loop
          // `count` frames handed on since the last callback, shown one by one or all at once.
          frame(count = 1) {
            this.presented += count;
            this.shown += count;
            const callbacks = [...this.callbacks.values()];
            this.callbacks.clear();
            callbacks.forEach((callback) => callback(clock, { presentedFrames: this.presented }));
          }
          start() { this.play(); this.fire("playing"); }
          stop() { this.paused = true; this.recount(); this.fire("pause"); }
          finish() { this.paused = this.ended = true; this.recount(); this.fire("pause"); this.fire("ended"); }
          // Another source: WebKit's new player counts from 0, its time at 0, before "emptied".
          another() {
            this.currentSrc = URL.createObjectURL(new Blob("video/mp4"));
            Object.assign(this, { counts: [0, 0], presented: 0, currentTime: 0 });
            this.recount();
            this.fire("emptied");
          }
        }
        // The clock moves `ms` on in 20 ms steps, the video timer running every 100 ms; `video`
        // (left out: none moves) shows a frame every 40 ms, 25 a second, its time moving with it.
        function go(ms, video = null) {
          const until = clock + ms;
          while (clock < until) {
            clock += 20;
            if (video && clock % 40 === 0) { video.currentTime += 0.04; video.frame(); }
            if (clock % 100 === 0) ticks();
          }
        }
        // The main thread held `ms`: the timer runs once after it, late. `video`, if given, went on
        // meanwhile, its frames shown and its callback called once, after.
        function block(ms, video = null) {
          clock += ms;
          if (video) { video.currentTime += ms / 1000; video.frame(ms / 40); }
          ticks();
        }
        const videos = () => posted.filter((message) => message.what === "video");
    """
    # The page's first spell, smooth: told, to compare with.
    FIRST = """
        const first = new Video();
        first.start();
        go(1000, first);
        first.stop();
    """
    SMOOTH = {"type": "media", "what": "video", "kind": "video", "route": "element", "source": "video/mp4",
              "state": "", "loop": False, "width": 720, "height": 1280, "played": 1000, "lasted": 1000,
              "waits": 0, "waited": 0, "stalls": 0, "frames": 25, "gaps": 0, "longest": 40, "dropped": 0,
              "total": 25, "late": 0}

    def spells(self, script: str, first: bool = True):
        """`script` after the stand-ins (and with `first`, the page's first spell)."""
        return run(self.VIDEO + (self.FIRST if first else "") + script, heard=True)

    def test_a_smooth_first_spell_is_told(self):
        self.assertEqual(self.spells("videos();"), [self.SMOOTH])

    def test_later_smooth_spells_are_not(self):
        # The same one played again, another, and ones with a frame or two dropped: none is rough.
        posted = self.spells("""
            first.start();
            go(2000, first);
            first.stop();
            const other = new Video();
            other.start();
            other.dropped = 1;  // 1 of 26
            go(1000, other);
            other.stop();
            const many = new Video();
            many.start();
            many.dropped = 2;  // 2 of 102
            go(4000, many);
            many.stop();
            [videos().length, ticking()];
        """)
        self.assertEqual(posted, [1, 0])

    def test_a_rough_spell_is_told_with_its_numbers(self):
        posted = self.spells("""
            const status = new Video();
            status.start();
            go(1000, status);  // 25 frames
            status.fire("waiting");  // its data runs out for 600 ms: a wait, not a gap
            go(600);
            status.fire("playing");
            go(1000, status);  // 25 frames
            go(300);  // no frame for 320 ms, not waiting: a gap
            go(500, status);  // 13 frames
            status.fire("stalled");
            block(400, status);  // the main thread held: 10 frames shown meanwhile, one callback after
            status.dropped += 6;
            go(200, status);  // 5 frames
            status.stop();
            videos().slice(1);
        """)
        self.assertEqual(posted, [{**self.SMOOTH, "played": 3120, "lasted": 4000, "waits": 1, "waited": 600,
                                   "stalls": 1, "frames": 78, "gaps": 1, "longest": 320, "dropped": 6, "total": 84,
                                   "late": 300}])

    def test_each_kind_of_rough_is_told(self):
        cases = {
            "wait": ("status.fire('waiting'); go(400); status.fire('playing');", {"waits": 1, "waited": 400}),
            "still waiting": ("status.fire('waiting'); go(400);", {"waits": 1, "waited": 600}),
            "stall": ("status.fire('stalled');", {"stalls": 1}),
            "gap": ("go(240);", {"gaps": 1, "longest": 280}),
            "dropped": ("status.dropped = 2;", {"dropped": 2, "total": 32}),
            "late": ("block(400, status);", {"late": 300, "gaps": 0}),
        }
        for name, (rough, numbers) in cases.items():
            with self.subTest(name):
                told = self.spells("""
                    const status = new Video();
                    status.start();
                    go(1000, status);
                    %s
                    go(200, status);
                    status.stop();
                    videos().slice(1);
                """ % rough)
                self.assertEqual(len(told), 1)
                self.assertEqual({key: told[0][key] for key in numbers}, numbers)

    def test_gaps_counted_and_gaps_that_are_rough(self):
        # 100 ms or less between frames is no gap; more is counted, but rough only over 150 ms.
        told = self.spells("""
            const status = new Video();
            status.start();
            go(1000, status);
            go(60);  // 80 ms between frames
            go(400, status);
            go(60);  // 120 ms: a gap, not a rough one
            go(400, status);
            status.stop();
            const quiet = videos().length;
            status.start();
            go(400, status);
            go(100);  // 120 ms
            go(400, status);
            status.fire("stalled");
            status.stop();
            [quiet, videos().slice(1).map((message) => [message.gaps, message.longest])];
        """)
        self.assertEqual(told, [1, [[1, 120]]])

    def test_frames_shown_late_are_no_gap(self):
        # The page's main thread held: WebKit showed the frames meanwhile and calls back once with
        # all of them, the gap shared between them. One frozen as long is a gap.
        told = self.spells("""
            const drawn = new Video();
            drawn.start();
            go(400, drawn);
            block(600, drawn);
            go(400, drawn);
            drawn.stop();
            const frozen = new Video();
            frozen.start();
            go(400, frozen);
            block(600);
            go(400, frozen);
            frozen.stop();
            videos().slice(1).map((message) => [message.frames, message.gaps, message.longest, message.late]);
        """)
        self.assertEqual(told, [[35, 0, 40, 500], [20, 1, 640, 500]])

    def test_frames_counted_at_once_on_time_are_a_gap(self):
        # WebKit counts a frame when its sink hands it on: frames that come all at once after a
        # stall, the page's main thread on time, were never shown one by one. The picture stood still.
        told = self.spells("""
            const burst = new Video();
            burst.start();
            go(1000, burst);
            go(600);
            burst.currentTime += 0.6;
            burst.frame(15);
            go(400, burst);
            burst.stop();
            videos().slice(1).map((message) => [message.frames, message.gaps, message.longest, message.late]);
        """)
        self.assertEqual(told, [[50, 1, 600, 0]])

    def test_a_picture_that_stands_still_until_the_spell_ends(self):
        # No frame comes, and none after: the stretch since the last (or since it started, or its
        # wait ended) is a gap too, whatever ends the spell.
        cases = {
            "then paused": ("go(1000, status); go(3000); status.stop();", {"gaps": 1, "longest": 3000}),
            "then another source": ("go(1000, status); go(3000); status.another();", {"gaps": 1, "longest": 3000}),
            "until it ended": ("go(1000, status); go(3000); status.currentTime += 3; status.finish();",
                               {"gaps": 1, "longest": 3000, "played": 4000}),
            "from the start": ("go(3000); status.stop();", {"frames": 0, "gaps": 1, "longest": 3000}),
            "before its first frame": ("go(2000); go(1000, status); status.stop();", {"gaps": 1, "longest": 2040}),
            "after a wait": ("go(1000, status); status.fire('waiting'); go(400); status.fire('playing'); go(2000); "
                             "go(400, status); status.stop();", {"waits": 1, "gaps": 1, "longest": 2040}),
        }
        for name, (script, numbers) in cases.items():
            with self.subTest(name):
                told = self.spells("""
                    const status = new Video();
                    status.start();
                    %s
                    videos().slice(1);
                """ % script)
                self.assertEqual(len(told), 1)
                self.assertEqual({key: told[0][key] for key in numbers}, numbers)
                self.assertEqual(parse_message({**told[0], "token": "t"}), told[0])

    def test_frames_never_called_back_make_no_spell_rough(self):
        # WebKit without hardware acceleration calls back no frame: until the page has had one, a
        # spell without frames is told only as the page's first.
        told = self.spells("""
            const one = new Video(), two = new Video();
            one.requestVideoFrameCallback = two.requestVideoFrameCallback = function () { return ++this.asked; };
            one.start();
            go(1000, one);
            one.stop();
            two.start();
            go(1000, two);
            two.stop();
            videos().map((message) => [message.frames, message.gaps, message.longest]);
        """, first=False)
        self.assertEqual(told, [[0, 1, 1000]])

    def test_where_webkit_counts_no_frames(self):
        # No requestVideoFrameCallback or getVideoPlaybackQuality: those numbers are null.
        told = self.spells("""
            const plain = new Video();
            plain.requestVideoFrameCallback = undefined;
            plain.getVideoPlaybackQuality = undefined;
            plain.start();
            go(1000, plain);
            plain.fire("stalled");
            plain.stop();
            videos().slice(1);
        """)
        self.assertEqual(told, [{**self.SMOOTH, "stalls": 1, "frames": None, "gaps": None, "longest": None,
                                 "dropped": None, "total": None}])

    def test_frames_shown_while_it_waits(self):
        # No gaps between them (the wait is told as one), and the first after it plays again is
        # the first of its stretch.
        told = self.spells("""
            const status = new Video();
            status.start();
            go(1000, status);
            status.fire("waiting");
            go(40, status);
            go(400);
            go(40, status);
            go(400);
            status.fire("playing");
            go(200, status);
            status.stop();
            videos().slice(1).map((message) => [message.waits, message.waited, message.gaps, message.longest]);
        """)
        self.assertEqual(told, [[1, 880, 0, 40]])

    def test_a_main_thread_held_until_it_stops(self):
        # Paused before the late timer could run: its lateness is read then, and the stretch
        # without a frame meanwhile is no gap (its frames may have been shown, not called back).
        told = self.spells("""
            const status = new Video();
            status.start();
            go(1000, status);
            clock += 400;
            status.stop();
            videos().slice(1).map((message) => [message.late, message.gaps, message.longest]);
        """)
        self.assertEqual(told, [[300, 0, 40]])

    def test_a_later_spell_counts_from_where_it_started(self):
        # Its time and WebKit's counts already past 0: only what this spell played and dropped.
        told = self.spells("""
            first.start();
            first.dropped = 3;
            go(1000, first);
            first.fire("stalled");
            first.stop();
            videos().slice(1).map((message) => [message.played, message.dropped, message.total]);
        """)
        self.assertEqual(told, [[1000, 3, 28]])

    def test_drops_counted_across_a_pause(self):
        # GStreamer's sink counts from 0 again after a pause, and WebKit keeps its last counts until
        # the new ones are past 0: each spell still has its own.
        told = self.spells("""
            const status = new Video();
            status.start();
            status.dropped = 20;
            go(10000, status);  // 250 shown, 20 dropped
            status.stop();
            status.start();
            status.dropped = 8;
            go(4000, status);  // 100 shown, 8 dropped
            status.stop();
            videos().slice(1).map((message) => [message.dropped, message.total]);
        """)
        self.assertEqual(told, [[20, 270], [8, 108]])

    def test_a_new_source_keeps_the_last_counts(self):
        # WebKit's player for the new source counts from 0 before "emptied": the spell keeps what
        # its last look saw, and is rough for its drops.
        told = self.spells("""
            const status = new Video();
            status.start();
            status.dropped = 4;
            go(1000, status);
            status.another();
            videos().slice(1).map((message) => [message.played, message.dropped, message.total]);
        """)
        self.assertEqual(told, [[1000, 4, 29]])

    def test_a_spell_ends_at_each_stop(self):
        # Paused, ended, failed, a new source, a seek (its time where the last look saw it), or
        # VIDEO_SPELL_MS of it. Each is the page's first spell, so told.
        cases = {
            "pause": ("status.stop();", 1000),
            "ended": ("status.finish();", 1000),
            "error": ("status.error = { code: 3 }; status.fire('error');", 1000),
            "new source": ("status.another();", 1000),
            "seek": ("status.seeking = true; status.currentTime = 9; status.fire('seeking');", 1000),
            "time": ("go(19000, status);", 20000),
        }
        for name, (stop, lasted) in cases.items():
            with self.subTest(name):
                told = self.spells("""
                    const status = new Video();
                    status.start();
                    go(1000, status);
                    %s
                    videos().map((message) => [message.lasted, message.played]);
                """ % stop, first=False)
                self.assertEqual(told, [[lasted, lasted]])

    def test_a_seek_starts_the_next_spell_once_settled(self):
        # Within its data: at "seeked" ("playing" doesn't come). One that waits for data after it:
        # at "playing", its wait before it no part of the spell.
        told = self.spells("""
            const status = new Video();
            status.start();
            go(1000, status);
            status.seeking = true;
            status.currentTime = 5;
            status.recount();
            status.fire("seeking");
            status.seeking = false;
            status.fire("seeked");
            const measured = ticking();
            go(1000, status);
            status.fire("stalled");
            status.stop();
            const other = new Video();
            other.start();
            go(1000, other);
            Object.assign(other, { seeking: true, readyState: 1 });
            other.fire("seeking");
            Object.assign(other, { seeking: false, readyState: 2 });
            other.fire("seeked");
            const waiting = ticking();
            go(500);
            other.readyState = 4;
            other.fire("playing");
            go(1000, other);
            other.fire("stalled");
            other.stop();
            [measured, waiting, videos().map((message) => [message.lasted, message.played, message.stalls])];
        """, first=False)
        self.assertEqual(told, [1, 0, [[1000, 1000, 0], [1000, 1000, 1], [1000, 1000, 1]]])

    def test_a_wait_runs_on_into_the_next_spell(self):
        # A long video: a spell each VIDEO_SPELL_MS, the next one at once. A wait across the change
        # is split between the two.
        told = self.spells("""
            const long = new Video();
            long.start();
            go(19000, long);
            long.fire("waiting");
            go(3000);
            const waiting = ticking();
            long.fire("playing");
            go(1000, long);
            long.stop();
            [waiting, ticking(), videos().map((message) => [message.lasted, message.waits, message.waited])];
        """, first=False)
        self.assertEqual(told, [1, 0, [[20000, 1, 1000], [3000, 1, 2000]]])

    def test_a_picture_standing_still_runs_on_into_the_next_spell(self):
        # No frame from 19 s to 21 s, across the change at 20 s: the next spell has the gap whole,
        # and the first is told for the second its time stood still.
        told = self.spells("""
            const long = new Video();
            long.start();
            go(19000, long);
            go(2000);
            go(1000, long);
            long.stop();
            videos().slice(1).map((message) => [message.lasted, message.frames, message.gaps, message.longest]);
        """)
        self.assertEqual(told, [[20000, 475, 0, 40], [2000, 25, 1, 2040]])

    def test_one_that_loops_isnt_told_for_its_time_behind(self):
        # A GIF going round every half second: its time, read every 100 ms, loses a little at each
        # round, more than VIDEO_LOST_MS in all; that's no slow clock.
        told = self.spells("""
            const gif = new Video();
            gif.loop = true;
            gif.start();
            for (let i = 0; i < 12; i++) {
              go(450, gif);
              gif.currentTime = 0;
              gif.recount();
              gif.fire("seeking");
              gif.fire("seeked");
            }
            gif.stop();
            videos().length;  // the page's first spell only
        """)
        self.assertEqual(told, 1)

    def test_one_that_loops_plays_on(self):
        # A GIF goes back to its start each time round (a seek, its sink counting from 0 again):
        # the same spell, its time and frames counted on.
        told = self.spells("""
            const gif = new Video();
            gif.loop = true;
            gif.start();
            go(1000, gif);
            gif.currentTime = 0;
            gif.recount();
            gif.fire("seeking");
            gif.fire("seeked");
            go(1000, gif);
            gif.fire("stalled");
            gif.stop();
            videos().slice(1).map((message) => [message.loop, message.lasted, message.played, message.frames,
                                                message.total]);
        """)
        self.assertEqual(told, [[True, 2000, 2000, 50, 50]])

    def test_one_playing_already_when_first_played(self):
        told = self.spells("""
            const autoplayed = new Video();
            autoplayed.paused = false;
            autoplayed.play();
            const measured = ticking();
            go(1000, autoplayed);
            autoplayed.fire("stalled");
            autoplayed.stop();
            [measured, videos().slice(1).map((message) => message.lasted)];
        """)
        self.assertEqual(told, [1, [1000]])

    def test_audio_and_streams_are_left_alone(self):
        # Neither is measured, though either would be the page's first spell.
        told = self.spells("""
            const note = new window.HTMLAudioElement();
            note.play();
            note.fire("playing");
            const call = new Video();
            call.srcObject = {};
            call.start();
            call.fire("stalled");
            go(1000, call);
            call.stop();
            [ticking(), ["waiting", "stalled", "seeking"].some((type) => type in note.listeners), call.asked,
             videos().length];
        """, first=False)
        self.assertEqual(told, [0, False, 0, 0])

    def test_the_timer_runs_only_while_a_video_plays(self):
        told = self.spells("""
            const counts = [ticking()];
            const one = new Video(), two = new Video();
            one.start();
            two.start();
            counts.push(ticking());
            go(500, one);
            one.stop();
            counts.push(ticking());
            two.paused = true;
            two.ended = true;
            two.fire("ended");
            counts.push(ticking(), one.callbacks.size, two.callbacks.size);
            counts;
        """, first=False)
        self.assertEqual(told, [0, 1, 1, 0, 0, 0])

    def test_a_hidden_page_drops_its_spells(self):
        # It draws no frames and runs its timers late. Shown again, what still plays is measured.
        told = self.spells("""
            const status = new Video();
            status.start();
            go(500, status);
            document.hidden = true;
            document.fire("visibilitychange");
            const hidden = [ticking(), videos().length];
            status.fire("playing");
            block(3000);
            const still = ticking();
            document.hidden = false;
            document.fire("visibilitychange");
            go(500, status);
            status.fire("stalled");
            status.stop();
            [hidden, still, videos().map((message) => message.lasted)];
        """, first=False)
        self.assertEqual(told, [[0, 0], 0, [500]])

    def test_one_waiting_when_the_page_shows_starts_at_playing(self):
        # Its spell would miss the wait: it starts once it plays again.
        told = self.spells("""
            const status = new Video();
            status.start();
            go(500, status);
            document.hidden = true;
            document.fire("visibilitychange");
            status.readyState = 2;
            status.fire("waiting");
            document.hidden = false;
            document.fire("visibilitychange");
            const waiting = ticking();
            go(2000);
            status.readyState = 4;
            status.fire("playing");
            go(500, status);
            status.fire("stalled");
            status.stop();
            [waiting, videos().map((message) => [message.lasted, message.waits])];
        """, first=False)
        self.assertEqual(told, [0, [[500, 0]]])

    def test_lines_of_their_own(self):
        # The sound's lines used up, a video's still told.
        told = self.spells("""
            const types = ["audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/aac", "audio/webm"];
            for (let i = 0; i < 20; i++) {
              const note = new window.HTMLAudioElement();
              note.currentSrc = URL.createObjectURL(new Blob(types[i % types.length]));
              note.play();
              note.error = { code: i % 5 };
              note.fire("error");
            }
            const sounds = posted.length;
            const status = new Video();
            status.start();
            go(1000, status);
            status.stop();
            [sounds, videos().length];
        """, first=False)
        self.assertEqual(told, [12, 1])
        # The videos' used up (VIDEO_LINES a page): nothing more is measured, and the sound's are still told.
        told = self.spells("""
            for (let i = 0; i < 18; i++) {
              const status = new Video();
              status.start();
              go(200, status);
              status.fire("stalled");
              status.stop();
            }
            const late = new Video();
            late.start();
            const measured = ticking();
            late.stop();
            const note = new window.HTMLAudioElement();
            note.play();
            note.error = { code: 4 };
            note.fire("error");
            [videos().length, measured, posted.filter((message) => message.what !== "video").map((m) => m.what)];
        """, first=False)
        self.assertEqual(told, [16, 0, ["support", "played", "error"]])

    def test_a_few_lines_for_one_element(self):
        # A video rough each spell: VIDEO_LINES_EACH for its source, then it isn't measured; its
        # next source, and other elements, still are.
        told = self.spells("""
            const long = new Video();
            long.start();
            for (let i = 0; i < 4; i++) {
              long.fire("stalled");
              go(20000, long);
            }
            const longLines = [videos().length, ticking()];
            long.another();  // another video in the same element
            long.start();
            long.fire("stalled");
            long.stop();
            const status = new Video();
            status.start();
            status.fire("stalled");
            status.stop();
            [longLines, videos().length];
        """, first=False)
        self.assertEqual(told, [[2, 0], 4])

    def test_a_few_spells_for_one_element(self):
        # Measuring keeps the page's rendering going, so told or not: VIDEO_SPELLS_EACH spells of
        # one source (no frame callback asked for after), one of one that loops; its next source
        # is measured again.
        told = self.spells("""
            const long = new Video();
            long.start();
            go(120000, long);
            const counts = [ticking(), long.callbacks.size];
            long.another();
            long.start();
            counts.push(ticking());
            long.stop();
            const gif = new Video();
            gif.loop = true;
            gif.start();
            go(40000, gif);
            counts.push(ticking(), gif.callbacks.size, videos().length);
            gif.stop();
            counts;
        """)
        self.assertEqual(told, [0, 0, 1, 0, 0, 1])

    def test_kinds_counts_and_sizes_only(self):
        told = self.spells("""
            const status = new Video();
            status.currentSrc = URL.createObjectURL(new Blob("video/mp4; name=Secret Person"));
            status.start();
            status.fire("stalled");
            status.stop();
            JSON.stringify(videos());
        """)
        self.assertIn('"source":"video/mp4"', told)
        for secret in ("Secret", "secret", "blob:", "http", "whatsapp"):
            self.assertNotIn(secret, told)

    def test_what_it_sends_is_taken(self):
        told = self.spells("""
            const status = new Video();
            status.start();
            go(1000, status);
            go(300);
            status.dropped = 3;
            go(500, status);
            status.fire("waiting");
            go(600);
            status.fire("playing");
            status.fire("stalled");
            status.stop();
            const plain = new Video();
            plain.requestVideoFrameCallback = plain.getVideoPlaybackQuality = undefined;
            plain.videoWidth = plain.videoHeight = 0;
            plain.start();
            plain.fire("stalled");
            plain.stop();
            videos();
        """)
        self.assertEqual(len(told), 3)
        for message in told:
            self.assertEqual(parse_message({**message, "token": "t"}), message)
        self.assertEqual(playback.line(told[1]),
                         "media: an <video> (video/mp4) 720x1280 played 1.5 s in 2.4 s: waited once (0.6 s), stalled "
                         "once; 38 frames, 1 gap over 100 ms (longest 320 ms), dropped 3 of 41; the page's main thread "
                         "up to 0 ms late")
        self.assertEqual(playback.line(told[2]),
                         "media: an <video> (video/mp4) played 0.0 s in 0.0 s: never waited, stalled once; frames not "
                         "counted; the page's main thread up to 0 ms late")

    def test_the_numbers_match(self):
        for bridge, ours in (("VIDEO_GAP_MS", playback.GAP_MS), ("VIDEO_MOST", playback.VIDEO_MOST)):
            self.assertIn(f"const {bridge} = {ours};", HEARD)
        lines = {name: int(re.search(rf"const {name} = (\d+);", HEARD).group(1))
                 for name in ("MEDIA_LINES", "VIDEO_LINES")}
        self.assertLessEqual(lines["MEDIA_LINES"], playback.LINES_PER_PAGE)
        self.assertLessEqual(lines["VIDEO_LINES"], playback.VIDEO_LINES_PER_PAGE)


class RecordingTest(unittest.TestCase):
    """How a voice note recorded (bridge.js watchRecording): chunks WebKit handed the page more than a
    chunk late (their sound a later chunk's by then), sound its audio thread skipped, a context clock
    that stood still, the microphone muted or ended, the context suspended; noted beside the page's own
    handler without changing what it gets, or what the idle audio counts."""

    # WhatsApp's recorder: a script processor of 4096 frames at 48 kHz fed by a microphone track, its
    # handler the page's (onaudioprocess). The stand-in WebKit hands a chunk over when the test says,
    # one event to the page's handler and then each listener: its playbackTime the context's clock
    # then, plus a chunk. The look for a recording's end (every second) is run by hand.
    RECORDER = """
        const intervals = new Map();
        let intervalIds = 0;
        setInterval = function (callback, ms) { intervals.set(++intervalIds, { callback, ms }); return intervalIds; };
        clearInterval = function (id) { intervals.delete(id); };
        const looks = () => [...intervals.values()].filter((interval) => interval.ms === 1000);
        const look = () => looks().forEach((interval) => interval.callback());
        const CHUNK = 4096 / 48000;  // seconds
        class Track extends Target {
          constructor(rate) { super(); Object.assign(this, { kind: "audio", readyState: "live", rate }); }
          getSettings() { return this.rate ? { sampleRate: this.rate, deviceId: "Secret Person's microphone" } : {}; }
          stop() { this.readyState = "ended"; }  // as in WebKit: the page's own stop() fires no "ended"
        }
        const stream = (track) => ({ getTracks: () => [track] });
        const handed = [];  // the events the page's handler got
        let context, node, track;
        // A new recorder, recording: its context, the processor (the page's handler set, or none), and
        // a microphone track feeding it.
        function recorder({ page = true, rate = 48000 } = {}) {
          context = new window.AudioContext();
          context.currentTime = 0;
          node = context.createScriptProcessor(4096, 1, 1);
          if (page) node.onaudioprocess = (event) => handed.push(event);
          track = new Track(rate);
          context.createMediaStreamSource(stream(track)).connect(node);
          node.connect(destination);
        }
        // The same recorder fed by the microphone again: a voice note resumed, or the next one.
        function again() {
          track = new Track(48000);
          context.createMediaStreamSource(stream(track)).connect(node);
        }
        function handOver(to = node) {
          const event = Object.freeze({ type: "audioprocess", playbackTime: to.context.currentTime + CHUNK,
                                        inputBuffer: Object.freeze({ sound: "Secret" }) });
          if (typeof to.onaudioprocess === "function") to.onaudioprocess(event);
          (to.listeners.audioprocess || []).slice().forEach((listener) => listener(event));
        }
        // `count` chunks fill, each handed over as it does: both clocks move on a chunk each.
        function record(count, to = node) {
          for (let i = 0; i < count; i++) {
            clock += CHUNK * 1000;
            to.context.currentTime += CHUNK;
            handOver(to);
          }
        }
        // The page's main thread held while `count` chunks filled: they're handed over at once after.
        function hold(count) {
          clock += count * CHUNK * 1000;
          context.currentTime += count * CHUNK;
          for (let i = 0; i < count; i++) handOver();
        }
        // The page stops its microphone: the next chunk ends the recording.
        function stop() {
          track.stop();
          record(1);
        }
        const recordings = () => posted.filter((message) => message.what === "recording");
    """
    # The page's first recording, smooth: told, to compare with.
    FIRST = """
        recorder();
        record(100);
        stop();
    """
    SMOOTH = {"type": "media", "what": "recording", "path": "native", "size": 4096, "rate": 48000, "microphone": 48000,
              "chunks": 100,
              "recorded": 8533, "clock": 8533, "lasted": 8533, "longest": 85, "late": 0, "behind": 0, "lost": 0,
              "skipped": 0, "muted": 0, "unmuted": 0, "ended": 0, "suspended": 0, "rested": 0, "end": "stopped"}

    def recorded(self, script: str, first: bool = True):
        """`script` after the stand-ins (and with `first`, the page's first recording)."""
        return run(self.RECORDER + (self.FIRST if first else "") + script, heard=True)

    def test_a_smooth_first_recording_is_told(self):
        self.assertEqual(self.recorded("[recordings(), handed.length, looks().length];"), [[self.SMOOTH], 101, 0])

    def test_later_smooth_ones_are_not(self):
        # The same recorder again (resumed after a pause), and another one: neither is rough.
        told = self.recorded("""
            again();
            record(50);
            stop();
            recorder();
            record(30);
            stop();
            [recordings().length, looks().length];
        """)
        self.assertEqual(told, [1, 0])

    def test_chunks_handed_over_late(self):
        # The main thread held while five chunks filled: they're handed over together after it, the
        # first four, three and two chunks late (WebKit had filled their buffers again by then: their
        # sound is lost), the next one chunk late (its buffer only just begun again) and the last on
        # time.
        told = self.recorded("""
            again();
            record(20);
            hold(5);
            record(20);
            stop();
            recordings();
        """)
        self.assertEqual(told, [self.SMOOTH, {
            **self.SMOOTH, "chunks": 45, "recorded": 3840, "clock": 3840, "lasted": 3840, "longest": 427,
            "late": 341, "behind": 3, "lost": 256}])

    def test_a_burst_at_the_start(self):
        # The first four chunks handed over together: known late from those that came after. And one
        # with the main thread held at its very end, the last three (one late) judged with what came.
        told = self.recorded("""
            recorder();
            hold(4);
            record(30);
            stop();
            recorder();
            record(30);
            hold(3);
            track.stop();
            look();
            recordings().map((m) => [m.chunks, m.behind, m.lost, m.late, m.longest]);
        """, first=False)
        self.assertEqual(told, [[34, 2, 171, 256, 85], [33, 1, 85, 171, 256]])

    def test_a_clock_that_stood_still(self):
        # The context's clock (its output) held a second: no chunk filled, none came, the recording
        # took a second longer than it holds.
        told = self.recorded("""
            again();
            record(10);
            clock += 1000;
            record(10);
            stop();
            recordings().slice(1);
        """)
        self.assertEqual(told, [{**self.SMOOTH, "chunks": 20, "recorded": 1707, "clock": 1707, "lasted": 2707,
                                 "longest": 1085}])

    def test_a_clock_that_stopped_for_good(self):
        # The context's clock held from the tenth chunk until the microphone stopped 20 s on (its
        # output failed, say): no chunk after it, so the looks that found the microphone still on
        # measure the wait, and the recording took 20 s longer than it holds.
        told = self.recorded("""
            again();
            record(10);
            for (let i = 0; i < 20; i++) {
              clock += 1000;
              look();
            }
            track.stop();
            look();
            recordings().slice(1);
        """)
        self.assertEqual(told, [{**self.SMOOTH, "chunks": 10, "recorded": 853, "clock": 853, "lasted": 20768,
                                 "longest": 20000}])

    def test_sound_the_audio_thread_skipped(self):
        # The page's handler held a chunk's buffer 300 ms: WebKit's audio thread, wanting it next,
        # skipped 215 ms meanwhile, so the next chunk is handed over late but whole (its buffer not
        # filled again) and all those after it on time, 215 ms on. And a main thread held while three
        # chunks filled, then 40 quanta (107 ms) skipped as they're handed over: one lost to
        # lateness, the chunks after the skips not late.
        told = self.recorded("""
            again();
            record(30);
            clock += 300;
            context.currentTime += 0.3;
            handOver();
            record(30);
            stop();
            again();
            record(30);
            hold(3);
            clock += 40 * 128 / 48;
            context.currentTime += 40 * 128 / 48000;
            record(30);
            stop();
            recordings().slice(1).map((m) => [m.chunks, m.behind, m.lost, m.late, m.skipped, m.longest]);
        """)
        self.assertEqual(told, [[61, 0, 0, 0, 215, 300], [63, 1, 85, 171, 107, 256]])

    def test_each_kind_of_rough_is_told(self):
        # Each alone, after the page's first: chunks handed over late, by a main thread held; sound
        # skipped, by a page's handler slower than the chunks come (the audio thread skipping a tenth
        # of a chunk as it waits for each: each handed over a tenth of a chunk later than the one
        # before, no gap); a gap over two chunks (its context's clock held half a second in a long
        # one: not behind by 1%); a clock running 5% slow (no gap); the microphone muted, or ended;
        # the context suspended.
        kinds = {
            "late": "record(10); hold(3); record(10);",
            "slower": "for (let i = 0; i < 20; i++) { clock += CHUNK * 1100; context.currentTime += CHUNK * 1.1; "
                      "handOver(); }",
            "gap": "record(600); clock += 500; record(100);",
            "slow": "for (let i = 0; i < 100; i++) { clock += CHUNK * 50; record(1); }",
            "muted": 'record(5); track.fire("mute"); record(5);',
            "ended": 'record(5); track.readyState = "ended"; track.fire("ended");',
            "suspended": 'record(5); context.state = "suspended"; context.fire("statechange"); record(5);',
        }
        for kind, script in kinds.items():
            told = self.recorded(f"again(); {script} stop(); recordings().length;")
            self.assertEqual(told, 2, kind)

    def test_small_things_are_not_rough(self):
        # Two chunks handed over at once (the first a chunk late, its buffer only just begun again),
        # one a millisecond past a chunk late (less than WebKit's quantum written meanwhile), less
        # than a chunk's worth skipped (31 quanta), a gap under two chunks, a clock 0.5% slow, the
        # microphone unmuted (it was muted before), the context running again.
        told = self.recorded("""
            again();
            record(10);
            hold(2);
            record(10);
            clock += 2 * CHUNK * 1000 + 1;
            context.currentTime += 2 * CHUNK + 0.001;
            handOver();
            handOver();
            clock += CHUNK * 1000 - 1;
            context.currentTime += CHUNK - 0.001;
            handOver();
            record(10);
            clock += 31 * 128 / 48;
            context.currentTime += 31 * 128 / 48000;
            record(10);
            clock += CHUNK * 900;
            record(1);
            for (let i = 0; i < 100; i++) { clock += CHUNK * 5; record(1); }
            track.fire("unmute");
            context.fire("statechange");
            record(5);
            stop();
            [recordings().length, handed.length];
        """)
        self.assertEqual(told, [1, 101 + 152])

    def test_two_microphones_into_one_recording(self):
        # Another microphone into the recorder under way: the recording runs until both stop.
        told = self.recorded("""
            recorder();
            const other = new Track(16000);
            context.createMediaStreamSource(stream(other)).connect(node);
            record(3);
            track.stop();
            record(2);
            other.fire("mute");
            other.stop();
            record(1);
            [looks().length, recordings().map((m) => [m.chunks, m.muted, m.microphone])];
        """, first=False)
        self.assertEqual(told, [0, [[5, 1, 48000]]])

    def test_the_next_one_soon_after(self):
        # A note stopped with its recorder disconnected (no chunk comes to end it), and the next one
        # fed into it before a look has seen the end: two recordings, the clock's run between them in
        # neither. Both smooth; then the same with the next one made rough, told with its own chunks.
        # Natter's listeners are off the recorder after each.
        told = self.recorded("""
            const next = (mute) => {
              again();
              record(30);
              track.stop();
              node.disconnect();
              clock += 500;
              context.currentTime += 0.5;
              again();
              node.connect(destination);
              if (mute) track.fire("mute");
              record(30);
              stop();
            };
            next(false);
            const smooth = recordings().length;
            next(true);
            [smooth, recordings().slice(1), node.listeners.audioprocess.length];
        """)
        self.assertEqual(told, [1, [{**self.SMOOTH, "chunks": 30, "recorded": 2560, "clock": 2560, "lasted": 2560,
                                     "muted": 1}], 0])

    def test_the_microphone_and_the_context_counted(self):
        told = self.recorded("""
            again();
            record(5);
            track.fire("mute");
            record(5);
            track.fire("unmute");
            stop();
            again();
            record(5);
            context.state = "suspended";
            context.fire("statechange");
            context.state = "running";
            context.fire("statechange");
            rested.add(context);  // as the idle audio's rest() would
            context.state = "suspended";
            context.fire("statechange");
            record(5);
            stop();
            again();
            record(5);
            track.readyState = "ended";  // ended other than by the page: unplugged, say
            track.fire("ended");
            record(1);
            recordings().slice(1).map((m) => [m.chunks, m.muted, m.unmuted, m.ended, m.suspended, m.rested, m.end]);
        """)
        self.assertEqual(told, [[10, 1, 1, 0, 0, 0, "stopped"], [10, 0, 0, 0, 2, 1, "stopped"],
                                [5, 0, 0, 1, 0, 0, "stopped"]])

    def test_no_chunk_at_all(self):
        # A microphone on a moment (no chunk yet, its end seen at the first look a second on) says
        # nothing; one on for seconds with none is told, as long as the looks saw it on, even where a
        # chunk is two seconds long (16384 frames at 8 kHz; its context closed with the microphone
        # still on: as long as until then).
        told = self.recorded("""
            again();
            clock += 300;
            track.stop();
            clock += 700;
            look();
            again();
            for (let i = 0; i < 3; i++) {
              clock += 1000;
              look();
            }
            clock += 500;
            track.stop();
            clock += 500;
            look();
            context = new window.AudioContext();
            context.sampleRate = 8000;
            node = context.createScriptProcessor(16384, 1, 1);
            node.onaudioprocess = () => {};
            track = new Track(8000);
            context.createMediaStreamSource(stream(track)).connect(node);
            node.connect(destination);
            clock += 1000;
            look();
            clock += 1500;
            context.close();
            [recordings().slice(1), looks().length];
        """)
        self.assertEqual(told, [[{**self.SMOOTH, "chunks": 0, "recorded": 0, "clock": 0, "lasted": 3000,
                                  "longest": 0},
                                 {**self.SMOOTH, "size": 16384, "rate": 8000, "microphone": 8000, "chunks": 0,
                                  "recorded": 0, "clock": 0, "lasted": 2500, "longest": 0, "end": "closed"}], 0])

    def test_how_each_ends(self):
        # Each made rough (its microphone muted), so each is told. Two at once share one timer.
        told = self.recorded("""
            const counts = [looks().length];
            recorder();
            const first = { context, node, track };
            recorder();
            counts.push(looks().length);
            first.track.fire("mute");
            track.fire("mute");
            record(3, first.node);
            record(3);
            first.node.disconnect();
            look();
            counts.push(looks().length);
            context.close();
            counts.push(looks().length);
            recorder();
            track.fire("mute");
            record(3);
            clock += 600000;
            look();
            recorder();
            track.fire("mute");
            record(3);
            clock += 600000;
            record(1);
            [counts, recordings().slice(1).map((m) => [m.end, m.chunks])];
        """)
        self.assertEqual(told, [[0, 1, 1, 0], [["disconnected", 3], ["closed", 3], ["long", 3], ["long", 3]]])

    def test_the_microphones_rate(self):
        told = self.recorded("""
            for (const rate of [16000, 0, 999, 44100.4]) {
              recorder({ rate });
              track.fire("mute");
              record(2);
              stop();
            }
            recordings().map((m) => m.microphone);
        """, first=False)
        self.assertEqual(told, [16000, None, None, 44100])

    def test_only_recorders(self):
        # A script processor the page plays through, and a worklet fed by the microphone: neither is a
        # recorder of this kind, and neither is listened to.
        told = self.recorded("""
            const player = new window.AudioContext();
            const output = player.createScriptProcessor(4096, 1, 1);
            output.onaudioprocess = () => {};
            output.connect(destination);
            record(50, output);
            const worklet = new window.AudioWorkletNode(player, "recorder");
            worklet.bufferSize = 4096;  // even one that says it has a chunk size
            player.createMediaStreamSource(stream(new Track(48000))).connect(worklet);
            worklet.connect(destination);
            [output.listeners.audioprocess || [], worklet.listeners.audioprocess || [], looks().length,
             posted.map((m) => m.what)];
        """, first=False)
        self.assertEqual(told, [[], [], 0, ["support", "recorder"]])

    def test_the_page_gets_each_event_as_it_was(self):
        # The page's handler gets each event once, as WebKit made it (frozen here: a write would throw).
        told = self.recorded("""
            recorder();
            record(3);
            hold(4);
            stop();
            [handed.length, handed.map((event) => Math.round((event.playbackTime - CHUNK) / CHUNK)),
             handed.every((event) => Object.keys(event).join() === "type,playbackTime,inputBuffer"),
             node.listeners.audioprocess.length];
        """, first=False)
        self.assertEqual(told, [8, [1, 2, 3, 7, 7, 7, 7, 8], True, 0])

    def test_natters_listener_is_not_the_pages(self):
        # A recorder with no handler of the page's yet holds no context up, though Natter listens, and
        # Natter's listening wakes nothing; once the page listens, it does. At the end Natter's
        # listeners are off the recorder, the microphone and the context.
        told = self.recorded("""
            context = new window.AudioContext();
            context.currentTime = 0;
            node = context.createScriptProcessor(4096, 1, 1);
            track = new Track(48000);
            const source = context.createMediaStreamSource(stream(track));
            const was = lastStart;
            clock += 5000;
            source.connect(node);
            const listened = [lastStart === was, listening.get(node).size, node.listeners.audioprocess.length];
            node.connect(destination);
            record(10);
            const none = [processing().size, audio().sizes.held];
            const pages = [];
            const page = (event) => pages.push(event.playbackTime);
            node.addEventListener("audioprocess", page);
            record(10);
            const one = [processing().size, listening.get(node).size, pages.length];
            stop();
            [listened, none, one, node.listeners.audioprocess.length, node.listeners.audioprocess[0] === page,
             ["mute", "unmute", "ended"].map((type) => (track.listeners[type] || []).length),
             context.listeners.statechange.length];
        """, first=False)
        self.assertEqual(told, [[True, 0, 1], [0, 0], [1, 1, 10], 1, True, [0, 0, 0], 1])

    def test_lines_of_their_own(self):
        # Rough ones told up to RECORDING_LINES a page (the first among them); then nothing is measured.
        told = self.recorded("""
            for (let i = 0; i < 7; i++) {
              again();
              record(5);
              track.fire("mute");
              stop();
            }
            again();
            const measured = [node.listeners.audioprocess.length, looks().length];
            const note = new window.HTMLAudioElement();
            note.play();
            note.error = { code: 4 };
            note.fire("error");
            [recordings().length, measured, posted.filter((m) => m.what !== "recording").map((m) => m.what)];
        """)
        self.assertEqual(told, [6, [0, 0], ["support", "recorder", "error"]])
        # Several under way as the last line is used: those that end after say nothing.
        told = self.recorded("""
            const all = [];
            for (let i = 0; i < 8; i++) {
              recorder();
              all.push({ node, track });
            }
            for (const one of all) {
              track = one.track;
              node = one.node;
              track.fire("mute");
              record(3);
              stop();
            }
            [recordings().length, looks().length];
        """, first=False)
        self.assertEqual(told, [6, 0])
        # The sound's lines used up, a recording's still told.
        told = self.recorded("""
            const types = ["audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/aac", "audio/webm"];
            for (let i = 0; i < 20; i++) {
              const note = new window.HTMLAudioElement();
              note.currentSrc = URL.createObjectURL(new Blob(types[i % types.length]));
              note.play();
              note.error = { code: i % 5 };
              note.fire("error");
            }
            const sounds = posted.length;
            recorder();
            record(5);
            stop();
            [sounds, recordings().length];
        """, first=False)
        self.assertEqual(told, [12, 1])

    def test_counts_durations_and_rates_only(self):
        told = self.recorded("""
            again();
            track.fire("mute");
            record(3);
            stop();
            JSON.stringify(recordings());
        """)
        for secret in ("Secret", "secret", "deviceId", "blob:", "http", "whatsapp"):
            self.assertNotIn(secret, told)

    def test_what_it_sends_is_taken(self):
        told = self.recorded("""
            again();
            record(20);
            hold(5);
            track.fire("mute");
            context.state = "suspended";
            context.fire("statechange");
            record(20);
            stop();
            recorder({ rate: 0 });
            clock += 1000;
            look();
            clock += 1000;
            look();
            track.stop();
            clock += 500;
            look();
            recordings();
        """)
        self.assertEqual(len(told), 3)
        for message in told:
            self.assertEqual(parse_message({**message, "token": "t"}), message)
        self.assertEqual(playback.line(told[0]),
                         "media: a voice recording 8.5 s in 8.5 s (8.5 s on its context's clock, of which the audio "
                         "thread skipped 0.0 s): 100 chunks of 4096 at 48000 Hz (microphone 48000 Hz), none handed "
                         "over late, longest gap 85 ms; the page's main thread up to 0 ms late; the microphone never "
                         "muted or ended early; its context never suspended; measured until the microphone stopped")
        self.assertEqual(playback.line(told[1]),
                         "media: a voice recording 3.8 s in 3.8 s (3.8 s on its context's clock, of which the audio "
                         "thread skipped 0.0 s): 45 chunks of 4096 at 48000 Hz (microphone 48000 Hz), 3 handed over "
                         "more than a chunk late (0.3 s of its sound lost), longest gap 427 ms; the page's main thread "
                         "up to 341 ms late; the microphone muted once; its context suspended once; measured until the "
                         "microphone stopped")
        self.assertEqual(playback.line(told[2]),
                         "media: a voice recording 0.0 s in 2.0 s (0.0 s on its context's clock, of which the audio "
                         "thread skipped 0.0 s): 0 chunks of 4096 at 48000 Hz (the microphone's rate not said), none "
                         "handed over late, longest gap 0 ms; the page's main thread up to 0 ms late; the microphone "
                         "never muted or ended early; its context never suspended; measured until the microphone "
                         "stopped")

    def test_the_numbers_match(self):
        numbers = {name: int(re.search(rf"const {name} = (\d+);", HEARD).group(1))
                   for name in ("RECORDING_LINES", "RATE_LEAST", "RATE_MOST")}
        self.assertLessEqual(numbers["RECORDING_LINES"], playback.RECORDING_LINES_PER_PAGE)
        self.assertEqual(range(numbers["RATE_LEAST"], numbers["RATE_MOST"] + 1), playback.RATES)
        sizes = re.search(r"const RECORDING_SIZES = \[([\d, ]+)\];", HEARD).group(1)
        self.assertEqual(tuple(int(size) for size in sizes.split(", ")), playback.RECORDING_SIZES)
        self.assertIn("const RECORDING_MOST_MS = 10 * 60000;", HEARD)
        self.assertEqual(playback.RECORDING_ENDS["long"], "for its first 10 minutes")


# Natter Den's worklet recorder (bridge.js recorderNode) against stand-ins for WebKit's AudioWorklet: each
# context's audioWorklet.addModule is noted in `loads`, with the context's state then (`stateThen`), and
# settles when a test says (`loads[i].ok()`, `loads[i].fail(name)`), the processor's name registering in
# that context as it loads. The bridge makes its first spare context as it starts: loads[0]. Nodes keep
# what they're connected to (`links`); an AudioWorkletNode keeps its options and has a port whose
# messages to the worklet are noted (`sent`); the worklet's to the page are handed over by hand
# (`chunk`). AudioBuffers and AudioProcessingEvents as WebKit's, and an Event for the rest.
WORKLETS = """
class LinkNode extends Target {
  constructor(context) { super(); this.context = context; this.links = []; this.numberOfInputs = 1; this.numberOfOutputs = 1; }
  connect(destination) { this.links.push(destination); return destination; }
  disconnect(...args) {
    if (!args.length || typeof args[0] === "number") this.links = [];
    else this.links = this.links.filter((other) => other !== args[0]);
  }
}
class Port {
  constructor() { this.sent = []; this.onmessage = null; this.closed = false; }
  postMessage(data, transfer) { this.sent.push({ data, transfer }); }
  close() { this.closed = true; }
}
const loads = [];
const registered = new WeakSet();  // contexts the processor's name is registered in
const made = [];  // the AudioWorkletNodes made
window.AudioContext.prototype.createGain = function () { return new LinkNode(this); };
Object.defineProperty(window.AudioContext.prototype, "audioWorklet", { get() {
  const context = this;
  return { addModule(url) {
    const load = { context, url, kind: url.slice(0, url.indexOf(":")), stateThen: context.state, settled: false };
    loads.push(load);
    return { then(ok, fail) {
      load.ok = () => { load.settled = true; registered.add(context); ok(); };
      load.fail = (name) => { load.settled = true; fail({ name, message: "Secret https://web.whatsapp.com/x" }); };
    } };
  } };
} });
window.ScriptProcessorNode = class ScriptProcessorNode extends LinkNode {};
window.AudioWorkletNode = class AudioWorkletNode extends LinkNode {
  constructor(context, name, options) {
    super(context);
    if (!registered.has(context)) throw { name: "InvalidStateError", message: "No ScriptProcessor was registered" };
    Object.assign(this, { name, options, port: new Port() });
    made.push(this);
  }
};
window.AudioBuffer = class AudioBuffer {
  constructor({ numberOfChannels, length, sampleRate }) {
    Object.assign(this, { numberOfChannels, length, sampleRate, channels: [] });
    for (let c = 0; c < numberOfChannels; c++) this.channels.push(new Float32Array(length));
  }
  getChannelData(c) { return this.channels[c]; }
  copyToChannel(source, c) { this.channels[c].set(source); }
  copyFromChannel(target, c) { target.set(this.channels[c]); }
};
window.AudioProcessingEvent = class AudioProcessingEvent {
  constructor(type, init) { this.type = type; Object.assign(this, init); }
};
var Event = class Event { constructor(type) { this.type = type; } };
"""

# A recorder of WhatsApp's kind on Natter's worklet: a context whose module has loaded (the spare,
# once every module loading has: `settleLoads`, then started), a script processor of 4096 frames at
# 48 kHz fed by a microphone track, the page's handler on it (`handed`). The worklet hands chunk n over
# as it would: its frames from the context's frame `frame`, channel c carrying (frame + i) / 1e6 + c;
# `record`, `hold` and `stop` as RecordingTest's. `suspendedThen`: every module loaded into a context
# suspended then.
RECORDER = """
const CHUNK = 4096 / 48000;
class Track extends Target {
  constructor(rate) { super(); Object.assign(this, { kind: "audio", readyState: "live", rate }); }
  getSettings() { return { sampleRate: this.rate }; }
  stop() { this.readyState = "ended"; }
}
const stream = (track) => ({ getTracks: () => [track], getAudioTracks: () => [track] });
const handed = [];
let context, node, track, sent = 0;
function settleLoads() {
  for (let load = loads.find((one) => !one.settled); load; load = loads.find((one) => !one.settled)) load.ok();
}
function readyContext() {
  settleLoads();
  const made = new window.AudioContext();
  made.resume();  // as it starts, a moment on
  made.currentTime = 0;
  return made;
}
const suspendedThen = () => loads.every((load) => load.stateThen === "suspended");
const loadsOf = (one) => loads.filter((load) => load.context === one).map((load) => load.kind);
function chunk(to, n, frame = n * to.bufferSize, channels = 1) {
  const input = [];
  for (let c = 0; c < channels; c++) {
    const channel = new Float32Array(to.bufferSize);
    for (let i = 0; i < channel.length; i++) channel[i] = (frame + i) / 1e6 + c;
    input.push(channel);
  }
  to.port.onmessage({ data: { n, frame, input } });
}
function recorder() {
  context = readyContext();
  node = context.createScriptProcessor(4096, 1, 1);
  node.onaudioprocess = (event) => handed.push(event);
  track = new Track(48000);
  context.createMediaStreamSource(stream(track)).connect(node);
  node.connect(destination);
  sent = 0;
}
// `count` chunks fill, each handed over as it does.
function record(count) {
  for (let i = 0; i < count; i++) {
    clock += CHUNK * 1000;
    context.currentTime += CHUNK;
    chunk(node, sent++);
  }
}
// The page's main thread held while `count` chunks filled: they're handed over at once after.
function hold(count) {
  clock += count * CHUNK * 1000;
  context.currentTime += count * CHUNK;
  for (let i = 0; i < count; i++) chunk(node, sent++);
}
function stop() {
  track.stop();
  record(1);
}
const recordings = () => posted.filter((message) => message.what === "recording");
const paths = () => posted.filter((message) => message.what === "recorder").map((m) => [m.path, m.why, m.module, m.name]);
"""

# The worklet's side: its module's source run in a context of its own, as in the AudioWorklet's
# global scope: registerProcessor, AudioWorkletProcessor with a port (`sent`), currentFrame.
PROCESSOR_SCOPE = """
let currentFrame = 0;
const processors = {};
function registerProcessor(name, processor) { processors[name] = processor; }
class AudioWorkletProcessor {
  constructor() { this.port = { sent: [], onmessage: null, postMessage(data, transfer) { this.sent.push({ data, transfer }); } }; }
}
"""


def module_source() -> str:
    """The worklet recorder's module, as the bridge hands it to addModule (a data: URL here)."""
    return run("""
        new window.AudioContext();
        loads[0].fail("SecurityError");
        decodeURIComponent(loads[1].url.slice("data:text/javascript,".length));
    """, fakes=WORKLETS)


class WorkletRecorderTest(unittest.TestCase):
    """A script processor made where Natter Den's worklet recorder can be is the worklet dressed as one
    (bridge.js recorderNode): the page gets each chunk whole however late its main thread runs. Its
    module loads only into a context that isn't playing: a spare the bridge makes ahead, suspended
    before it ever ran, that the page's next context like it is, whatever order the page makes things in;
    or one the idle audio rested, while no microphone is on. Where it can't be (no module in that
    context, refused, none, too many contexts, a shape it doesn't take, its node not made) it's
    WebKit's own, as before, and the log says which and why, once a page."""

    def shim(self, script: str, fakes: str = ""):
        """`script` after the stand-ins and RECORDER's helpers (`fakes`: more stand-ins)."""
        return run(RECORDER + script, heard=True, fakes=WORKLETS + fakes)

    def turns(self, *scripts: str, fakes: str = ""):
        """As shim(), each script a turn of the page's event loop of its own (promises settle between
        them): the value of each."""
        return turns(RECORDER + scripts[0], *scripts[1:], heard=True, fakes=WORKLETS + fakes)

    def test_a_spare_made_ahead_is_the_pages_next_context(self):
        # Made as the bridge starts, suspended before it ever ran, its module loading. A context the
        # page makes before that has loaded is WebKit's own, with no module loading into it. Once it
        # has, the page's next context is the spare, and a script processor made in the same task is
        # the worklet; the next spare is made at once. The spare starts a moment on, as a new one would.
        told = self.turns("""
            const spareMade = loads[0].context;
            const listed = (one) => [...contexts].some((ref) => ref.deref() === one);
            const before = [loads.length, loads[0].kind, spareMade.state, spareMade instanceof window.AudioContext,
                            listed(spareMade)];
            const early = new window.AudioContext();
            const earlyNode = early.createScriptProcessor(4096, 1, 1);
            loads[0].ok();
            context = new window.AudioContext();
            node = context.createScriptProcessor(4096, 2, 1);
            const worklet = made[0];
            [before, early === spareMade, loadsOf(early), context === spareMade, listed(context), context.state,
             loads.length, loads[1].context !== context && loads[1].context !== early, loads[1].context.state,
             node === worklet, node instanceof window.ScriptProcessorNode, node instanceof window.AudioWorkletNode,
             node.bufferSize, worklet.name, worklet.options, /voip/i.test(worklet.name), callContexts.has(context),
             scripts.has(node), shims.has(node), shims.has(earlyNode), scripts.has(earlyNode), paths()];
        """, """
            [context.state, early.state, loads[1].context.state, suspendedThen()];
        """)
        self.assertEqual(told, [[
            [1, "blob", "suspended", True, False], False, [], True, True, "running", 2, True, "suspended",
            True, True, False, 4096, "natter-script-processor",
            {"numberOfInputs": 1, "numberOfOutputs": 1, "outputChannelCount": [1], "channelCount": 2,
             "channelCountMode": "explicit", "channelInterpretation": "speakers",
             "processorOptions": {"size": 4096, "channels": 2}},
            False, False, True, True, False, True,
            [["native", "fresh", "", ""], ["worklet", "ready", "blob", ""]]],
            ["running", "running", "suspended", True]])

    def test_a_spare_starts_a_moment_on_unless_the_page_suspends_it_first(self):
        # As a new context: left alone, it starts once the page's task is done; suspended by the page in
        # the task it made it in, it never starts.
        told = self.turns("""
            settleLoads();
            const alone = new window.AudioContext();
            settleLoads();
            const kept = new window.AudioContext();
            kept.suspend();
            [alone === loads[0].context, kept === loads[1].context, alone.state, kept.state];
        """, "[alone.state, kept.state];")
        self.assertEqual(told, [[True, True, "suspended", "suspended"], ["running", "suspended"]])

    def test_what_the_page_does_first_in_its_microtasks_comes_first(self):
        # WebKit's own new context starts in a task of its own: a page that suspends or closes it in a
        # microtask first (an await on) stops it before it ever starts, and so with a spare, which then
        # never opens an output. WebKit's close() takes a while (the state changes once the output has
        # closed): a spare closing isn't started meanwhile either.
        closing = """
            const resumed = [];
            const closing = [];
            const base = window.AudioContext.prototype;
            const resume = base.resume;
            base.resume = function () { resumed.push(this); return resume.call(this); };
            base.close = function () { closing.push(this); return new Promise(() => {}); };
            const closed = () => closing.splice(0).forEach((one) => { one.state = "closed"; one.fire("statechange"); });
        """
        told = self.turns("""
            settleLoads();
            const kept = new window.AudioContext();
            let seen = "";
            Promise.resolve().then(() => { seen = kept.state; kept.suspend(); });
            settleLoads();
            const shut = new window.AudioContext();
            Promise.resolve().then(() => shut.close());
            settleLoads();
            const alone = new window.AudioContext();
            [kept === loads[0].context, shut === loads[1].context, alone === loads[2].context];
        """, """
            const then = [seen, kept.state, shut.state, alone.state, resumed.length === 1 && resumed[0] === alone];
            closed();
            [...then, shut.state];
        """, fakes=closing)
        self.assertEqual(told, [[True, True, True], ["suspended", "suspended", "suspended", "running", True, "closed"]])

    def test_a_timers_task_where_there_is_no_message_channel(self):
        told = self.turns("""
            settleLoads();
            context = new window.AudioContext();
        """, """
            const then = context.state;
            runTimers();
            [then, context.state];
        """, fakes="MessageChannel = undefined;")
        self.assertEqual(told[1], ["suspended", "running"])

    def test_only_a_context_like_the_spare_is_it(self):
        # WebKit's own rate asked for, or none, and any latencyHint (WebKitGTK makes the same context for
        # each): the spare. Another rate, any other option, a latencyHint WebKit would refuse, or a page's
        # own subclass of AudioContext: WebKit's own, the spare kept for the next.
        told = self.shim("""
            class Mine extends window.AudioContext {}
            const spareNow = () => loads[loads.length - 1].context;
            const kinds = [];
            for (const options of [{ sampleRate: 16000 }, { sinkId: "" }, { latencyHint: "fast" }, "44100", "mine",
                                   undefined, { sampleRate: 48000, latencyHint: "playback" }, { latencyHint: 0.02 },
                                   null, {}]) {
              settleLoads();
              const was = spareNow();
              const made = options === "mine" ? new Mine() : new window.AudioContext(options);
              kinds.push(made === was);
              if (options === "mine") kinds.push(made instanceof Mine);
            }
            const other = new window.AudioContext({ sampleRate: 16000 });
            other.createScriptProcessor(4096, 1, 1);
            [kinds, paths()];
        """)
        self.assertEqual(told, [[False, False, False, False, False, True, True, True, True, True, True],
                                [["native", "options", "", ""]]])

    def test_no_module_loads_into_a_context_as_the_page_makes_it(self):
        # WebKit stops and starts a playing context's sound output as the first worklet processor
        # registers in it: a context of the page's (another rate, or made while no spare was ready)
        # gets none, and its script processors are WebKit's own.
        told = self.shim("""
            const early = new window.AudioContext();
            settleLoads();
            const other = new window.AudioContext({ sampleRate: 16000 });
            [loadsOf(early), loadsOf(other), loads.length, shims.has(early.createScriptProcessor(4096, 1, 1)),
             shims.has(other.createScriptProcessor(4096, 1, 1)), suspendedThen(), paths()];
        """)
        self.assertEqual(told, [[], [], 1, False, False, True, [["native", "fresh", "", ""], ["native", "options", "", ""]]])

    def test_a_context_of_the_pages_takes_it_once_rested(self):
        # The idle audio rests it: once suspended, its module loads, and its next script processor is the
        # worklet. One the page keeps (WhatsApp's), from its second use on.
        told = self.turns("""
            settleLoads();
            context = new window.AudioContext({ sampleRate: 16000 });
            const first = shims.has(context.createScriptProcessor(4096, 1, 1));
            clock += 31000;
            rest();
            [first, context.state, loadsOf(context)];
        """, """
            const loading = [loadsOf(context), suspendedThen()];
            loads[loads.length - 1].ok();
            clock += 31000;
            rest();
        """, """
            [loading, loadsOf(context), shims.has(context.createScriptProcessor(4096, 1, 1)), paths()];
        """)
        self.assertEqual(told[0], [False, "suspended", []])
        self.assertEqual(told[2], [[["blob"], True], ["blob"], True,
                                   [["native", "options", "", ""], ["worklet", "ready", "blob", ""]]])

    def test_not_while_a_microphone_is_opening_or_on(self):
        # A context rested while the page's getUserMedia is unanswered, or a track from it is live, takes
        # no module (one resumed for a recording would have its output restarted as the microphone opens);
        # at the idle audio's next look after the microphone is off, it does.
        microphone = """
            let answer = null;
            const asked = [];
            window.navigator = { mediaDevices: { getUserMedia(constraints) {
              asked.push(constraints);
              return new Promise((resolve) => { answer = resolve; });
            } } };
        """
        told = self.turns("""
            settleLoads();
            context = new window.AudioContext({ sampleRate: 16000 });
            let got = null;
            window.navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => { got = stream; });
            clock += 31000;
            rest();
            asked.length;
        """, """
            const opening = loadsOf(context);
            track = new Track(48000);
            answer(stream(track));
        """, """
            clock += 31000;
            rest();
        """, """
            const on = [Boolean(got), loadsOf(context)];
            track.stop();
            clock += 31000;
            rest();
        """, """
            [opening, on, context.state, loadsOf(context), suspendedThen()];
        """, fakes=microphone)
        self.assertEqual(told[0], 1)
        self.assertEqual(told[4], [[], [True, []], "suspended", ["blob"], True])

    def test_a_context_resumed_before_its_turn_isnt_loaded(self):
        # Two rested: the second is resumed while the first loads. Its turn passes; it loads once rested again.
        told = self.turns("""
            settleLoads();
            const one = new window.AudioContext({ sampleRate: 16000 });
            const two = new window.AudioContext({ sampleRate: 16000 });
            clock += 31000;
            rest();
        """, """
            const first = [loadsOf(one), loadsOf(two)];
            wake(two);
            loads[loads.length - 1].ok();
            const passed = [loadsOf(two), two.state, shims.has(two.createScriptProcessor(4096, 1, 1))];
            clock += 31000;
            rest();
        """, """
            [first, passed, loadsOf(two), suspendedThen(), paths()];
        """)
        self.assertEqual(told[2], [[["blob"], []], [[], "running", False], ["blob"], True,
                                   [["native", "options", "", ""]]])

    def test_a_module_landing_in_a_resumed_context_is_told(self):
        # The page resumed it while its module loaded: its output was likely restarted. Once a page.
        told = self.turns("""
            settleLoads();
            context = new window.AudioContext({ sampleRate: 16000 });
            clock += 31000;
            rest();
        """, """
            context.resume();
            loads[loads.length - 1].ok();
            posted.filter((message) => message.what === "recorder");
        """)
        self.assertEqual(told[1], [{"type": "media", "what": "recorder", "path": "worklet", "why": "restarted",
                                    "module": "blob", "name": ""}])
        message = told[1][0]
        self.assertEqual(parse_message({**message, "token": "t"}), message)
        self.assertEqual(playback.line(message),
                         "media: Natter Den's worklet recorder's module loaded into a context the page resumed "
                         "meanwhile, so WebKit likely stopped and started that context's sound output (from a blob: URL)")

    def test_getusermedia_waits_for_modules_loading(self):
        # The page made a context (the spare: the next one's module loads) and asks for the microphone:
        # WebKit's getUserMedia is asked at once, but the page gets its answer once that module has loaded,
        # so a context the page makes then is a spare. With nothing loading it doesn't wait; and it waits
        # SPARE_HOLD_MOST_MS at most.
        microphone = """
            const asked = [];
            window.navigator = { mediaDevices: { getUserMedia(constraints) {
              asked.push(this === window.navigator.mediaDevices);
              const track = new Target();
              Object.assign(track, { kind: "audio", readyState: "live" });
              return Promise.resolve({ getTracks: () => [track], getAudioTracks: () => [track] });
            } } };
        """
        told = self.turns("""
            settleLoads();
            const first = new window.AudioContext();
            let got = null;
            window.navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => { got = stream; });
            [asked.length, loads.length, loads[1].settled];
        """, """
            const held = [asked.length, got];
            settleLoads();
        """, """
            const answered = [asked.length, Boolean(got)];
            context = new window.AudioContext({ sampleRate: 48000 });
            const spared = [context === loads[1].context, shims.has(context.createScriptProcessor(4096, 1, 1))];
            settleLoads();
            window.navigator.mediaDevices.getUserMedia({ audio: true });
            const free = asked.length;
            new window.AudioContext();
            let late = null;
            window.navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => { late = stream; });
            const bounded = [asked.length, loads[loads.length - 1].settled];
        """, """
            bounded.push(late);
            runTimers();
        """, """
            [held, answered, spared, free, bounded, Boolean(late), asked];
        """, fakes=microphone)
        self.assertEqual(told[0], [1, 2, False])
        self.assertEqual(told[4], [[1, None], [1, True], [True, True], 2, [3, False, None], True, [True, True, True]])

    def test_getusermedia_is_asked_in_the_pages_call(self):
        # WebKit's getUserMedia counts a request as the click's only while that click is handled
        # (MediaDevices::getUserMedia looks as it's called), so it's asked in the page's call even while a
        # module loads; the page's answer waits. Thrown or refused, the call is no longer unanswered: a
        # context rested once the microphone is off takes the module.
        microphone = """
            const asked = [];
            let refuse = "";
            let inClick = false;
            window.navigator = { mediaDevices: { getUserMedia() {
              asked.push(inClick);
              if (refuse === "throw") throw { name: "TypeError", message: "Secret" };
              if (refuse) return Promise.reject({ name: refuse, message: "Secret" });
              track = new Track(48000);
              return Promise.resolve(stream(track));
            } } };
            function click(then) { inClick = true; try { return then(); } finally { inClick = false; } }
        """
        told = self.turns("""
            let got = null, threw = "", refused = "";
            const ask = () => window.navigator.mediaDevices.getUserMedia({ audio: true });
            click(ask).then((answer) => { got = answer; });
            refuse = "throw";
            try { click(ask); } catch (error) { threw = error.name; }
            refuse = "NotAllowedError";
            click(ask).catch((error) => { refused = error.name; });
            [asked, loads.length, loads[0].settled];
        """, """
            const before = [got, threw, refused];
            loads[0].ok();
        """, """
            const after = Boolean(got);
            track.stop();
            context = new window.AudioContext({ sampleRate: 16000 });
            clock += 31000;
            rest();
        """, """
            [before, after, loadsOf(context)];
        """, fakes=microphone)
        self.assertEqual(told[0], [[True, True, True], 1, False])
        self.assertEqual(told[3], [[None, "TypeError", "NotAllowedError"], True, ["blob"]])

    def test_ended_microphones_are_let_go(self):
        # Note after note recorded into spares: every context's module is ready, so nothing looks for a
        # microphone on. The tracks that ended go all the same as the page next asks for one (stop() fires
        # no "ended"), or as one says it ended: WebKit keeps an ended track's capturer while it's held.
        microphone = """
            window.navigator = { mediaDevices: { getUserMedia() {
              track = new Track(48000);
              return Promise.resolve(stream(track));
            } } };
            const counts = [];
            const note = () => window.navigator.mediaDevices.getUserMedia({ audio: true });
        """
        note = """
            counts.push(microphoneTracks.size);
            context = new window.AudioContext();
            node = context.createScriptProcessor(4096, 1, 1);
            context.createMediaStreamSource(stream(track)).connect(node);
            track.stop();
            context.close();
            settleLoads();
            note();
        """
        told = self.turns("settleLoads(); note();", *[note] * 5, """
            counts.push(microphoneTracks.size);
            track.fire("ended");
            [counts, microphoneTracks.size, made.length, loads.length];
        """, fakes=microphone)
        self.assertEqual(told[-1], [[1, 1, 1, 1, 1, 1], 0, 5, 6])

    def test_webkits_own_where_it_cant_be(self):
        told = self.turns("""
            const kinds = [];
            context = readyContext();
            // Shapes it doesn't take: no input or output channels, more than 32, WebKit's own size
            // (0, or none given), a size WebKit refuses (WebKit's own then throws, as before).
            for (const args of [[4096, 0, 1], [4096, 1, 0], [4096, 33, 1], [0, 1, 1], [], [1000, 1, 1], [4096.5, 1, 1]]) {
              kinds.push(shims.has(context.createScriptProcessor(...args)));
            }
            const defaults = context.createScriptProcessor(4096);  // two channels in and out
            // Its node not made.
            registered.delete(context);
            kinds.push(shims.has(context.createScriptProcessor(4096, 1, 1)));
            // A context closed while it waits its turn to load (the next spare's loading).
            const closing = new window.AudioContext({ sampleRate: 16000 });
            clock += 31000;
            rest();
        """, """
            closing.close();
            settleLoads();
            kinds.push(shims.has(closing.createScriptProcessor(4096, 1, 1)));
            [kinds, shims.has(defaults), made[0].options.channelCount, made[0].options.outputChannelCount, loads.length,
             loadsOf(closing), paths()];
        """)
        self.assertEqual(told[1], [[False] * 9, True, 2, [2], 2, [], [
            ["native", "shape", "", ""], ["worklet", "ready", "blob", ""], ["native", "failed", "", "InvalidStateError"],
            ["native", "closed", "", ""]]])

    def test_refused_kinds_arent_tried_again(self):
        # A policy that refuses blob: modules (a SecurityError: WebKit's Worklet::addModule checks the
        # Content-Security-Policy): the spare tries data:, and the next one data: only. One that refuses
        # both: that spare is closed, no more are made, and the page's script processors are WebKit's own.
        told = self.shim("""
            loads[0].fail("SecurityError");
            const before = loads.length;
            loads[1].ok();
            const a = new window.AudioContext();
            const fromData = shims.has(a.createScriptProcessor(4096, 1, 1));
            const next = loads[2].context;
            loads[2].fail("SecurityError");
            const b = new window.AudioContext();
            clock += 31000;
            rest();
            [before, loads.map((load) => [load.context === a ? "a" : load.context === next ? "next" : "?", load.kind]),
             loads.length, fromData, next.state, b === next, shims.has(b.createScriptProcessor(4096, 1, 1)), paths()];
        """)
        self.assertEqual(told, [2, [["a", "blob"], ["a", "data"], ["next", "data"]], 3, True, "closed", False, False, [
            ["worklet", "ready", "data", ""], ["native", "refused", "", "SecurityError"]]])

    def test_a_spare_closed_while_it_loads_refuses_nothing(self):
        # Closing a context doesn't stop its worklet's thread (WebKitGTK 2.52): its load still ends, as
        # loaded. That spare isn't the page's next context; a new one is made as the page makes that, and
        # the same kind is tried for it.
        told = self.shim("""
            const first = loads[0].context;
            first.close();
            loads[0].ok();
            const fresh = new window.AudioContext();
            loads[1].ok();
            const next = new window.AudioContext();
            [loads.map((load) => load.kind), first.state, fresh === first, next === loads[1].context,
             shims.has(next.createScriptProcessor(4096, 1, 1)), shims.has(fresh.createScriptProcessor(4096, 1, 1)), paths()];
        """)
        self.assertEqual(told, [["blob", "blob", "blob"], "closed", False, True, True, False,
                                [["worklet", "ready", "blob", ""], ["native", "fresh", "", ""]]])

    def test_a_context_closed_while_its_module_loads_has_none(self):
        # Closing it doesn't stop the load (WebKitGTK 2.52): it still ends, as loaded, in a closed context.
        told = self.turns("""
            settleLoads();
            context = new window.AudioContext({ sampleRate: 16000 });
            clock += 31000;
            rest();
        """, """
            context.close();
            loads[1].ok();
            [loadsOf(context), shims.has(context.createScriptProcessor(4096, 1, 1)), paths()];
        """)
        self.assertEqual(told[1], [["blob"], False, [["native", "closed", "", ""]]])

    def test_data_urls_where_there_are_no_object_urls(self):
        told = self.shim("""
            const kinds = loads.map((load) => load.kind);
            [kinds, shims.has(readyContext().createScriptProcessor(4096, 1, 1))];
        """, fakes="URL.createObjectURL = undefined;")
        self.assertEqual(told, [["data"], True])

    def test_a_spare_no_longer_suspended_is_let_go(self):
        # A spare ready, then interrupted (WebKit's) or closed: it can't be the page's new context. It's let
        # go (closed, if it isn't), and the next spare made at once.
        told = self.shim("""
            loads[0].ok();
            const stale = loads[0].context;
            stale.state = "interrupted";
            const first = new window.AudioContext();
            const after = [first === stale, stale.state, loads.length];
            loads[1].ok();
            const second = new window.AudioContext();
            loads[2].ok();
            loads[2].context.close();
            const third = new window.AudioContext();
            loads[3].ok();
            const fourth = new window.AudioContext();
            [after, second === loads[1].context, third === loads[2].context, fourth === loads[3].context, loads.length,
             shims.has(fourth.createScriptProcessor(4096, 1, 1))];
        """)
        self.assertEqual(told, [[False, "closed", 2], True, False, True, 5, True])

    def test_a_failed_load_fails_only_that_context(self):
        # A load that fails but for the page's policy (AbortError for a network error or a load cancelled,
        # TypeError, …) fails only that context's: a spare is closed, and the next made (and tried with
        # the same kind) as the page next makes a context, not before; a context of the page's is tried
        # again at the idle audio's next look while it's rested. After 3 failures of a kind in the page,
        # that kind is given up as a refused one is, and the next tried at once.
        told = self.turns("""
            const first = loads[0].context;
            loads[0].fail("AbortError");
            const spareGone = [first.state, loads.length];
            const page = new window.AudioContext({ sampleRate: 16000 });
            clock += 31000;
            rest();
        """, """
            loads[1].ok();
            loads[2].fail("TypeError");
            const unloaded = shims.has(page.createScriptProcessor(4096, 1, 1));
            clock += 31000;
            rest();
        """, """
            loads[3].fail("AbortError");
            const tried = loads.map((load) => [load.context === page ? "page" : "spare", load.kind]);
            loads[4].ok();
            const loaded = shims.has(page.createScriptProcessor(4096, 1, 1));
            const spared = new window.AudioContext();
            [spareGone, tried, unloaded, loaded, spared === loads[1].context, loads[5].kind,
             posted.filter((message) => message.what === "recorder")];
        """)
        self.assertEqual(told[2][:6], [["closed", 1], [["spare", "blob"], ["spare", "blob"], ["page", "blob"],
                                                      ["page", "blob"], ["page", "data"]],
                                       False, True, True, "data"])
        unloaded = {"type": "media", "what": "recorder", "path": "native", "why": "unloaded", "module": "",
                    "name": "TypeError"}
        self.assertEqual(told[2][6], [unloaded, {**unloaded, "path": "worklet", "why": "ready", "module": "data",
                                                 "name": ""}])
        self.assertEqual(parse_message({**unloaded, "token": "t"}), unloaded)
        self.assertEqual(playback.line(unloaded),
                         "media: a script processor (a voice recorder, say) made as WebKit's own; Natter Den's worklet "
                         "recorder's module failed to load into that context: TypeError")

    def test_a_context_that_misses_its_turn_counts_once(self):
        # WhatsApp's long-lived context rested, queued behind a spare's load, and resumed before its turn,
        # over and over: each time it misses its turn it comes off the count of contexts with the module,
        # so the count never fills with it and spares go on being made.
        cycle = ("""
            settleLoads();
            new window.AudioContext().close();  // the spare taken: the next one's module loads
            clock += 31000;
            rest();
        """, """
            wake(context);
            loads[loads.length - 1].ok();
        """)
        told = self.turns("""
            settleLoads();
            context = new window.AudioContext({ sampleRate: 16000 });
        """, *(cycle * 20), """
            settleLoads();
            const next = new window.AudioContext();
            [shims.has(next.createScriptProcessor(4096, 1, 1)), paths().slice(-1)];
        """)
        self.assertEqual(told[-1], [True, [["worklet", "ready", "blob", ""]]])

    def test_one_load_at_a_time(self):
        # WebKitGTK 2.52 hangs the page when a dozen load from blob: URLs at once: each waits its turn.
        told = self.turns("""
            const three = [0, 1, 2].map(() => new window.AudioContext({ sampleRate: 16000 }));
            clock += 31000;
            rest();
        """, """
            const counts = [loads.length];
            loads[0].ok();
            counts.push(loads.length);
            loads[1].fail("SecurityError");  // that kind isn't tried again: data: for it, then for the next
            counts.push(loads.length);
            loads[2].ok();
            counts.push(loads.length);
            loads[3].ok();
            counts.push(loads.length);
            [counts, loads.map((load) => [three.indexOf(load.context), load.kind])];
        """)
        self.assertEqual(told[1], [[1, 2, 3, 4, 5], [[-1, "blob"], [0, "blob"], [0, "data"], [1, "data"], [2, "data"]]])

    def test_at_most_16_contexts_with_it(self):
        # Spares count: with 16 contexts holding it no spare is made, and a context rested then takes none.
        # One closed, the next context the page makes has a spare made after it.
        told = self.turns("""
            const all = [];
            for (let i = 0; i < 16; i++) all.push(readyContext());
            const extra = new window.AudioContext();
            clock += 31000;
            rest();
        """, """
            const kinds = all.map((one) => shims.has(one.createScriptProcessor(4096, 1, 1)));
            const counted = [loads.length, shims.has(extra.createScriptProcessor(4096, 1, 1))];
            all[0].close();
            const after = new window.AudioContext();
            settleLoads();
            const again = new window.AudioContext();
            [kinds.filter(Boolean).length, counted, loads.length, shims.has(after.createScriptProcessor(4096, 1, 1)),
             shims.has(again.createScriptProcessor(4096, 1, 1)), paths()];
        """)
        self.assertEqual(told[1], [16, [16, False], 17, False, True, [
            ["worklet", "ready", "blob", ""], ["native", "many", "", ""], ["native", "fresh", "", ""]]])

    def test_each_chunk_in_an_event_as_webkits(self):
        # The page's handler where it was first set, then its listeners; an AudioProcessingEvent with
        # the chunk's sound in a buffer of its own, an outputBuffer of silence, and as playbackTime when
        # what the page writes plays: two chunks after the chunk began. What the page wrote goes back.
        told = self.shim("""
            context = readyContext();
            node = context.createScriptProcessor(4096, 2, 1);
            const got = [];
            node.onaudioprocess = () => got.push("first");
            node.addEventListener("audioprocess", (event) => got.push("listener"));
            node.onaudioprocess = function (event) {
              got.push(["handler", this === node, event.type, event instanceof window.AudioProcessingEvent,
                        event.playbackTime * 48000, event.inputBuffer instanceof window.AudioBuffer,
                        event.inputBuffer.numberOfChannels, event.inputBuffer.length, event.inputBuffer.sampleRate,
                        event.inputBuffer.getChannelData(1)[5], event.outputBuffer.numberOfChannels,
                        event.outputBuffer.length, event.outputBuffer.getChannelData(0).every((v) => v === 0)]);
              event.outputBuffer.getChannelData(0).fill(0.25);
            };
            chunk(node, 0, 1000, 2);
            const port = made[0].port;
            const back = port.sent.map(({ data, transfer }) => [data.n, data.output.length, data.output[0][4095],
                                                                 transfer.length === 1 && transfer[0] === data.output[0].buffer]);
            // The handler cleared, then set again: it comes after the listener now.
            node.onaudioprocess = null;
            const cleared = node.onaudioprocess;
            chunk(node, 1, 5096, 2);
            node.onaudioprocess = () => got.push("again");
            chunk(node, 2, 9192, 2);
            [got, back, cleared, port.sent.map(({ data }) => data.n)];
        """)
        self.assertEqual(told, [
            [["handler", True, "audioprocess", True, 1000 + 8192, True, 2, 4096, 48000,
              struct.unpack("f", struct.pack("f", 1005 / 1e6 + 1))[0],
              1, 4096, True], "listener", "listener", "listener", "again"],
            [[0, 1, 0.25, True]], None, [0, 1, 2]])

    def test_an_event_like_it_where_none_can_be_made(self):
        told = self.shim("""
            context = readyContext();
            node = context.createScriptProcessor(4096, 1, 1);
            const got = [];
            node.onaudioprocess = (event) => got.push([event instanceof Event, event.type, event.playbackTime * 48000,
                                                      event.inputBuffer.length, event.outputBuffer.length]);
            chunk(node, 0);
            chunk(node, 1);
            got;
        """, fakes="window.AudioProcessingEvent = class { constructor() { throw new TypeError('no'); } };")
        self.assertEqual(told, [[True, "audioprocess", 8192, 4096, 4096], [True, "audioprocess", 12288, 4096, 4096]])

    def test_processed_only_while_its_output_leads_somewhere(self):
        # Unconnected, an AudioWorkletNode would be processed regardless: it's connected into Natter's
        # idle GainNode, which the idle audio doesn't count, and again whenever every connection is cut.
        told = self.shim("""
            context = readyContext();
            node = context.createScriptProcessor(4096, 1, 1);
            const gain = node.links[0];
            const kind = (links) => links.map((link) => link === gain ? "idle" : link === destination ? "out" : "other");
            const steps = [kind(node.links)];
            node.connect(destination);
            steps.push(kind(node.links), outputs.get(node).size);
            node.disconnect();
            steps.push(kind(node.links), outputs.get(node).size);
            node.connect(destination);
            node.disconnect(destination);
            steps.push(kind(node.links));
            node.connect(destination);
            node.disconnect(0);
            steps.push(kind(node.links));
            const other = readyContext().createScriptProcessor(4096, 1, 1);
            [steps, gain instanceof LinkNode && gain.context === context, gain.links.length, other.links[0] !== gain];
        """)
        self.assertEqual(told, [[["idle"], ["idle", "out"], 1, ["idle"], 0, ["idle"], ["idle"]], True, 0, True])

    def test_it_counts_as_a_script_processor_for_the_idle_audio(self):
        # Made and connected it wakes a context Natter rested; with a handler and connected it holds
        # its context up, without one it doesn't; fed by a microphone that ended, it holds it
        # RECORDER_HOLD_MS; its name is never a call's.
        told = self.shim("""
            context = readyContext();
            rest();
            clock += 31000;
            rest();
            const restedThen = context.state;
            node = context.createScriptProcessor(4096, 1, 1);
            const woken = context.state;
            node.connect(destination);
            clock += 31000;
            rest();
            const noHandler = context.state;
            node.onaudioprocess = () => {};
            clock += 31000;
            rest();
            const handler = [context.state, processing().has(context)];
            track = new Track(48000);
            context.createMediaStreamSource(stream(track)).connect(node);
            track.stop();
            node.disconnect();
            node.onaudioprocess = null;
            clock += 31000;
            const held = processing().has(context);
            clock += 10 * 60000;
            rest();
            [restedThen, woken, noHandler, handler, held, context.state, callContexts.has(context), audio().sizes.calls];
        """)
        self.assertEqual(told, ["suspended", "running", "suspended", ["running", True], True, "suspended", False, 0])

    def test_closed_with_its_context(self):
        told = self.shim("""
            context = readyContext();
            node = context.createScriptProcessor(4096, 1, 1);
            const port = made[0].port;
            const before = [typeof port.onmessage, port.closed];
            context.close();
            [before, port.onmessage, port.closed];
        """)
        self.assertEqual(told, [["function", False], None, True])

    def test_an_error_in_its_processor_is_told(self):
        told = self.shim("""
            context = readyContext();
            node = context.createScriptProcessor(4096, 1, 1);
            node.fire("processorerror");
            paths();
        """)
        self.assertEqual(told, [["worklet", "ready", "blob", ""], ["worklet", "broke", "blob", ""]])

    def test_a_recording_through_it_is_late_but_whole(self):
        # A page's first recording, smooth; then one with the main thread held while five chunks
        # filled: handed over four, three, two, one and no chunks late, whole. Those more than a chunk
        # late are told as late but whole, none lost; how late is the context's clock past each chunk.
        told = self.shim("""
            recorder();
            record(100);
            stop();
            recorder();
            record(20);
            hold(5);
            record(20);
            stop();
            recorder();
            record(30);
            stop();
            [recordings(), handed.length, handed.slice(0, 3).map((event) => Math.round(event.playbackTime * 48000))];
        """)
        smooth = {**RecordingTest.SMOOTH, "path": "worklet"}
        self.assertEqual(told, [[smooth, {**smooth, "chunks": 45, "recorded": 3840, "clock": 3840, "lasted": 3840,
                                          "longest": 427, "late": 341, "behind": 3}], 101 + 46 + 31, [8192, 12288, 16384]])
        for message in told[0]:
            self.assertEqual(parse_message({**message, "token": "t"}), message)
        self.assertEqual(playback.line(told[0][1]),
                         "media: a voice recording (through Natter Den's worklet recorder) 3.8 s in 3.8 s (3.8 s on its "
                         "context's clock, of which the audio thread skipped 0.0 s): 45 chunks of 4096 at 48000 Hz "
                         "(microphone 48000 Hz), 3 handed over more than a chunk late, but whole (nothing lost), longest "
                         "gap 427 ms; the page's main thread up to 341 ms late; the microphone never muted or ended early; "
                         "its context never suspended; measured until the microphone stopped")

    def test_sound_it_didnt_gather(self):
        # Its chunks' frames jump on where the worklet wasn't processed meanwhile (its context's clock
        # ran on, its output led nowhere): that much is sound not gathered.
        told = self.shim("""
            recorder();
            record(10);
            context.currentTime += 0.5;
            clock += 500;
            sent += Math.round(0.5 / CHUNK);  // its frames 0.5 s on (as near as a chunk)
            record(10);
            stop();
            recordings().map((m) => [m.chunks, m.skipped, m.behind, m.lost]);
        """)
        self.assertEqual(told, [[20, 512, 0, 0]])

    def test_nothing_is_lost_through_it(self):
        # Even were its chunks' frames ever out of step (one from five chunks back), a recording through
        # it counts none lost: its chunks come whole; how late each came is the clock's alone (that one,
        # five chunks).
        told = self.shim("""
            recorder();
            record(10);
            clock += CHUNK * 1000;
            context.currentTime += CHUNK;
            chunk(node, 10, 5 * 4096);
            record(10);
            stop();
            recordings().map((m) => [m.path, m.chunks, m.behind, m.lost]);
        """)
        self.assertEqual(told, [["worklet", 21, 1, 0]])

    def test_its_lines_are_taken(self):
        told = self.shim("""
            context = new window.AudioContext();
            context.createScriptProcessor(4096, 1, 1);
            loads[0].fail("SecurityError");
            loads[1].ok();
            const spared = new window.AudioContext();
            spared.createScriptProcessor(4096, 1, 1);
            spared.createScriptProcessor(4096, 0, 1);
            node = spared.createScriptProcessor(4096, 1, 1);
            node.fire("processorerror");
            new window.AudioContext({ sampleRate: 16000 }).createScriptProcessor(4096, 1, 1);
            posted.filter((message) => message.what === "recorder");
        """)
        lines = []
        for message in told:
            self.assertEqual(parse_message({**message, "token": "t"}), message)
            lines.append(playback.line(message))
        self.assertEqual(lines, [
            "media: a script processor (a voice recorder, say) made as WebKit's own; its context was made while no "
            "spare context with Natter Den's worklet recorder's module was ready, and hasn't rested since",
            "media: a script processor (a voice recorder, say) made as Natter Den's worklet recorder; its module "
            "loaded (from a data: URL)",
            "media: a script processor (a voice recorder, say) made as WebKit's own; its buffer size or channels aren't "
            "ones Natter Den's worklet recorder takes",
            "media: a script processor (a voice recorder, say) made as Natter Den's worklet recorder; it then broke (an "
            "error in its processor), so it records nothing (from a data: URL)",
            "media: a script processor (a voice recorder, say) made as WebKit's own; its context was made with options a "
            "spare context with Natter Den's worklet recorder's module doesn't have (another sample rate, say), and "
            "hasn't rested since"])

    def test_the_numbers_match(self):
        self.assertIn("const RECORDER_CONTEXTS_MOST = 16;", IDLE)
        self.assertIn("16 contexts", playback.RECORDER_WORDS["many"])
        self.assertIsNone(re.search(r"voip", re.search(r'const RECORDER_PROCESSOR = "([^"]+)"', IDLE).group(1), re.I))
        self.assertIn("const SPARE_HOLD_MOST_MS = 250;", IDLE)
        fails = re.search(r"const RECORDER_FAILS_MOST = (\d+);", IDLE).group(1)
        self.assertIn(f"failed {fails} times", playback.RECORDER_WORDS["refused"])


class RecorderDocsTest(unittest.TestCase):
    """docs/how-it-works.md tells which script processor a page gets, and when, as the bridge and the
    log do: the log's lines as playback.py writes them, the order of events rather than a load time
    (a context made on getUserMedia's answer would get WebKit's own, which spares are there for), and
    getUserMedia's longest wait as the bridge's."""

    DOCS = " ".join((Path(__file__).resolve().parent.parent / "docs" / "how-it-works.md").read_text().split())
    LINE = {"type": "media", "what": "recorder", "path": "worklet", "why": "ready", "module": "blob", "name": ""}

    def test_the_lines_quoted_are_the_logs(self):
        self.assertIn(f"`natter: <account>: {playback.line(self.LINE)}`", self.DOCS)
        fresh = playback.line({**self.LINE, "path": "native", "why": "fresh", "module": ""})
        self.assertIn("`… " + fresh[fresh.index("made as WebKit's own; "):] + "`", self.DOCS)
        self.assertIn(f"`natter: <account>: {playback.line({**self.LINE, 'why': 'restarted'})}`", self.DOCS)

    def test_when_the_page_gets_which(self):
        self.assertNotRegex(self.DOCS, r"\d+ to \d+ ms after the context is made")
        self.assertIn("it's done about 10 ms after the context first runs", self.DOCS)
        self.assertIn("in a context made after `getUserMedia` would be WebKit's own", self.DOCS)
        hold = re.search(r"const SPARE_HOLD_MOST_MS = (\d+);", IDLE).group(1)
        self.assertIn(f"waits for modules loading at that moment ({hold} ms at most;", self.DOCS)
        self.assertIn("`getUserMedia` reaches WebKit at once, inside the page's call", self.DOCS)
        fails = re.search(r"const RECORDER_FAILS_MOST = (\d+);", IDLE).group(1)
        self.assertIn(f"nor is one that has failed to load {fails} times", self.DOCS)
        self.assertIn("WebKit stops and starts that context's sound output again", self.DOCS)


class WorkletProcessorTest(unittest.TestCase):
    """The worklet recorder's processor, in a context of its own as in the AudioWorklet: it gathers
    exactly a chunk, posts it whole (its buffers handed over), and plays what the page wrote back for
    a chunk two chunks on, at the same place in it; silence where that came too late or not at all."""

    def processor(self, script: str):
        context = JavaScriptCore.Context()
        value = context.evaluate(PROCESSOR_SCOPE + module_source() + script, -1)
        exception = context.get_exception()
        if exception is not None:
            raise AssertionError(exception.get_message())
        return json.loads(value.to_json(0))

    def test_the_module_stands_alone(self):
        source = module_source()
        self.assertTrue(source.isascii())
        self.assertEqual(self.processor("Object.keys(processors);"), ["natter-script-processor"])

    def test_it_gathers_each_chunk_whole(self):
        # Two channels of 512 frames: four quanta make a chunk, posted with the context's frame at its
        # first, its own buffers transferred; nothing connected in is silence.
        told = self.processor("""
            const p = new processors["natter-script-processor"]({ processorOptions: { size: 512, channels: 2 } });
            const quantum = (q, c) => Float32Array.from({ length: 128 }, (_, i) => q * 128 + i + c * 10000);
            const counts = [];
            currentFrame = 7000;
            for (let q = 0; q < 10; q++) {
              const inputs = q === 9 ? [[]] : [[quantum(q, 0), quantum(q, 1)]];
              const outputs = [[new Float32Array(128)]];
              if (!p.process(inputs, outputs)) throw new Error("ended");
              counts.push(p.port.sent.length);
              currentFrame += 128;
            }
            const [first, second] = p.port.sent;
            const whole = (array, from) => array.every((v, i) => v === from + i);
            [counts, first.data.n, first.data.frame, second.data.n, second.data.frame, first.data.input.length,
             whole(first.data.input[0], 0), whole(first.data.input[1], 10000), whole(second.data.input[0], 512),
             first.transfer.length === 2 && first.transfer[0] === first.data.input[0].buffer
               && first.transfer[1] === first.data.input[1].buffer,
             first.data.input[0] !== second.data.input[0], p.filled, Array.from(p.chunk[0].slice(126, 130))];
        """)
        self.assertEqual(told, [[0, 0, 0, 1, 1, 1, 1, 2, 2, 2], 0, 7000, 1, 7512, 2, True, True, True, True, True, 256,
                                [1150, 1151, 0, 0]])

    def test_it_plays_what_the_page_wrote_two_chunks_on(self):
        told = self.processor("""
            const p = new processors["natter-script-processor"]({ processorOptions: { size: 256, channels: 1 } });
            const written = (n) => [Float32Array.from({ length: 256 }, (_, i) => n * 1000 + i + 1)];
            const played = [];
            for (let q = 0; q < 14; q++) {
              const chunkNow = Math.floor(q / 2);
              // What the page wrote for chunk 0 comes as chunk 1 is gathered; chunk 1's half way through
              // chunk 3 (it plays from there on); chunk 2's once chunk 5 began (too late: dropped), with
              // chunk 4's (early: kept). Nothing for chunk 3: silence.
              if (q === 2) p.port.onmessage({ data: { n: 0, output: written(0) } });
              if (q === 7) p.port.onmessage({ data: { n: 1, output: written(1) } });
              if (q === 10) p.port.onmessage({ data: { n: 2, output: written(2) } });
              if (q === 10) p.port.onmessage({ data: { n: 4, output: written(4) } });
              const out = new Float32Array(128);
              p.process([[new Float32Array(128)]], [[out]]);
              played.push([chunkNow, out[0], out[127]]);
              currentFrame += 128;
            }
            [played, [...p.written.keys()]];
        """)
        self.assertEqual(told, [[[0, 0, 0], [0, 0, 0], [1, 0, 0], [1, 0, 0], [2, 1, 128], [2, 129, 256],
                                 [3, 0, 0], [3, 1129, 1256], [4, 0, 0], [4, 0, 0], [5, 0, 0], [5, 0, 0],
                                 [6, 4001, 4128], [6, 4129, 4256]], []])


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
        stalled = {**self.PLAYED, "what": "stalled", "then": "seek", "route": "element", "state": ""}
        self.assertEqual(parse_message(stalled), stalled)
        for then in ("", "maybe", None, 1):
            self.assertIsNone(parse_message({**stalled, "then": then}), then)
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

    def test_a_dropped_play(self):
        stalled = {**self.PLAYED, "what": "stalled", "then": "seek", "route": "element", "state": ""}
        self.assertEqual(playback.line(stalled),
                         "media: an <audio> (audio/ogg), from the element itself said it played while its time stood "
                         "still; WebKit dropped the play(); it started once its place was nudged")
        self.assertEqual(playback.line({**stalled, "then": "still"}),
                         "media: an <audio> (audio/ogg), from the element itself said it played while its time stood "
                         "still; neither play() again nor a nudge of its place started it, so something else holds it")

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

    VIDEO = {"type": "media", "what": "video", "kind": "video", "route": "element", "state": "", "source": "video/mp4",
             "loop": False, "width": 720, "height": 1280, "played": 12300, "lasted": 14100, "waits": 3,
             "waited": 1600, "stalls": 2, "frames": 350, "gaps": 4, "longest": 640, "dropped": 12, "total": 360,
             "late": 420}

    def test_a_video_spell(self):
        video = self.VIDEO
        self.assertEqual(parse_message({**video, "token": "t"}), video)
        numbers = playback.VIDEO_NUMBERS + playback.VIDEO_FRAMES + playback.VIDEO_DROPS
        for change in (dict.fromkeys(playback.VIDEO_FRAMES), dict.fromkeys(playback.VIDEO_DROPS),
                       dict.fromkeys(playback.VIDEO_FRAMES + playback.VIDEO_DROPS), {"lasted": playback.VIDEO_MOST},
                       {"loop": True, "width": 0, "height": 0}, {"route": "webaudio", "state": "running"},
                       {"frames": 0, "gaps": 1, "longest": 14100}, {"gaps": 351}):
            self.assertEqual(parse_message({**video, **change}), {**video, **change}, change)
        for key in numbers:
            for value in (-1, playback.VIDEO_MOST + 1, 1.5, "3", True, None, [3]):
                self.assertIsNone(parse_message({**video, key: value}), (key, value))
        for change in ({"width": 725}, {"height": 1}, {"gaps": 352}, {"dropped": 361}, {"loop": "no"}, {"loop": 0},
                       {"loop": None}, {"kind": "audio"}, {"source": "blob:https://web.whatsapp.com/x"},
                       {"source": "video/x-secret"}, {"state": "running"}, {"name": "Secret"},
                       {"url": "https://mmg.whatsapp.net/v"}):
            self.assertIsNone(parse_message({**video, **change}), change)
        for key in video:
            self.assertIsNone(parse_message({other: value for other, value in video.items() if other != key}), key)

    def test_video_lines(self):
        self.assertEqual(playback.line(self.VIDEO),
                         "media: an <video> (video/mp4) 720x1280 played 12.3 s in 14.1 s: waited 3 times (1.6 s), "
                         "stalled twice; 350 frames, 4 gaps over 100 ms (longest 640 ms), dropped 12 of 360; the "
                         "page's main thread up to 420 ms late")
        smooth = {**self.VIDEO, "played": 1000, "lasted": 1000, "waits": 0, "waited": 0, "stalls": 0, "frames": 25,
                  "gaps": 0, "longest": 40, "dropped": 0, "total": 25, "late": 0}
        self.assertEqual(playback.line(smooth),
                         "media: an <video> (video/mp4) 720x1280 played 1.0 s in 1.0 s: never waited or stalled; 25 "
                         "frames, no gaps over 100 ms (longest 40 ms), dropped 0 of 25; the page's main thread up to "
                         "0 ms late")
        self.assertIn(": waited once (0.6 s), never stalled; 25 frames, 1 gap over 100 ms (longest 320 ms),",
                      playback.line({**smooth, "waits": 1, "waited": 600, "gaps": 1, "longest": 320}))
        self.assertIn(": never waited, stalled once;", playback.line({**smooth, "stalls": 1}))
        self.assertEqual(playback.line({**smooth, "width": 0, "height": 0, "stalls": 3,
                                        **dict.fromkeys(playback.VIDEO_FRAMES + playback.VIDEO_DROPS)}),
                         "media: an <video> (video/mp4) played 1.0 s in 1.0 s: never waited, stalled 3 times; frames "
                         "not counted; the page's main thread up to 0 ms late")
        self.assertTrue(playback.line({**smooth, "loop": True, "route": "webaudio", "state": "running", "width": 480,
                                       "height": 480}).startswith(
            "media: a looping <video> (video/mp4) 480x480, through a Web Audio context (running), played 1.0 s in "))

    RECORDING = {"type": "media", "what": "recording", "path": "native", "size": 4096, "rate": 48000, "microphone": 48000,
                 "chunks": 459, "recorded": 39168, "clock": 40000, "lasted": 40100, "longest": 420, "late": 380,
                 "behind": 12, "lost": 1000, "skipped": 800, "muted": 1, "unmuted": 0, "ended": 0, "suspended": 0,
                 "rested": 0, "end": "stopped"}

    def test_a_recording(self):
        recording = self.RECORDING
        self.assertEqual(parse_message({**recording, "token": "t"}), recording)
        for change in ({"microphone": None}, {"microphone": 8000}, {"microphone": 1000},
                       {"rate": 768000, "recorded": 2448, "lost": 64}, {"rate": 44100, "recorded": 42632},
                       {"size": 128, "recorded": 1224, "lost": 0, "behind": 0},
                       {"size": 16384, "chunks": 100, "recorded": 34133}, {"suspended": 2, "rested": 2},
                       {"end": "disconnected"}, {"end": "closed"}, {"end": "long"}, {"lost": 1024},
                       {"path": "worklet", "lost": 0},
                       {"chunks": 0, "recorded": 0, "clock": 0, "behind": 0, "lost": 0, "longest": 0, "late": 0},
                       {"lasted": playback.VIDEO_MOST}):
            self.assertEqual(parse_message({**recording, **change}), {**recording, **change}, change)
        for key in playback.RECORDING_NUMBERS:
            for value in (-1, playback.VIDEO_MOST + 1, 1.5, "3", True, None, [3]):
                self.assertIsNone(parse_message({**recording, key: value}), (key, value))
        for change in ({"size": 4000, "recorded": 38250}, {"size": 64, "recorded": 612, "behind": 0, "lost": 0},
                       {"size": 32768, "recorded": 313344}, {"size": "4096"}, {"size": True},
                       {"size": None}, {"size": 4096.0}, {"microphone": 48000.0}, {"rate": 999}, {"rate": 768001}, {"rate": 48000.0}, {"rate": True},
                       {"rate": None}, {"microphone": 0}, {"microphone": 999}, {"microphone": "48000"},
                       {"microphone": 48000.5}, {"microphone": True}, {"end": "paused"}, {"end": None},
                       {"recorded": 39170}, {"chunks": 11, "recorded": 939}, {"lost": 1026}, {"behind": 0},
                       {"rested": 1}, {"name": "Secret"}, {"kind": "audio"}, {"state": "running"},
                       {"path": "worklet"}, {"path": "script"}, {"path": None}, {"path": ""},
                       {"url": "https://mmg.whatsapp.net/v"}):
            self.assertIsNone(parse_message({**recording, **change}), change)
        for key in recording:
            self.assertIsNone(parse_message({other: value for other, value in recording.items() if other != key}), key)

    RECORDER = {"type": "media", "what": "recorder", "path": "worklet", "why": "ready", "module": "blob", "name": ""}

    def test_a_recorder(self):
        recorder = self.RECORDER
        self.assertEqual(parse_message({**recorder, "token": "t"}), recorder)
        for change in ({"module": "data"}, {"why": "broke"}, {"why": "restarted"},
                       {"path": "native", "why": "loading", "module": ""},
                       {"path": "native", "why": "fresh", "module": ""}, {"path": "native", "why": "options", "module": ""},
                       {"path": "native", "why": "refused", "module": "", "name": "SecurityError"},
                       {"path": "native", "why": "refused", "module": ""},
                       {"path": "native", "why": "unloaded", "module": "", "name": "AbortError"},
                       {"path": "native", "why": "unloaded", "module": ""},
                       {"path": "native", "why": "failed", "module": "", "name": "NotSupportedError"},
                       *({"path": "native", "why": why, "module": ""} for why in ("none", "many", "closed", "shape"))):
            self.assertEqual(parse_message({**recorder, **change}), {**recorder, **change}, change)
        for change in ({"path": "native"}, {"path": "native", "why": "loading"}, {"why": "loading"},
                       {"path": "script"}, {"why": "fine"}, {"module": "https"}, {"module": None}, {"module": "Blob"},
                       {"name": "SecurityError"}, {"path": "native", "why": "loading", "module": "", "name": "Error"},
                       {"path": "native", "why": "refused", "module": "", "name": "Secret Person"},
                       {"path": "native", "why": "refused", "module": "", "name": None}, {"url": "blob:https://x"},
                       {"path": "native", "why": "unloaded", "module": "blob", "name": "AbortError"},
                       {"path": "native", "why": "shape", "module": "data"},
                       {"path": "native", "why": "fresh", "module": "blob"}, {"path": "native", "why": "restarted", "module": ""},
                       {"why": "fresh"}, {"why": "restarted", "name": "Error"}):
            self.assertIsNone(parse_message({**recorder, **change}), change)
        for key in recorder:
            self.assertIsNone(parse_message({other: value for other, value in recorder.items() if other != key}), key)

    def test_recorder_lines(self):
        native = {**self.RECORDER, "path": "native", "module": ""}
        self.assertEqual(playback.line({**native, "why": "refused", "name": "SecurityError"}),
                         "media: a script processor (a voice recorder, say) made as WebKit's own; Natter Den's worklet "
                         "recorder's module won't load in this page from a blob: or a data: URL (each refused by the "
                         "page's policy, or failed 3 times): SecurityError")
        self.assertEqual(playback.line({**native, "why": "unloaded", "name": "AbortError"}),
                         "media: a script processor (a voice recorder, say) made as WebKit's own; Natter Den's worklet "
                         "recorder's module failed to load into that context: AbortError")
        self.assertEqual(playback.line({**native, "why": "many"}),
                         "media: a script processor (a voice recorder, say) made as WebKit's own; 16 contexts already "
                         "have Natter Den's worklet recorder")
        self.assertEqual(playback.line(self.RECORDER),
                         "media: a script processor (a voice recorder, say) made as Natter Den's worklet recorder; its "
                         "module loaded (from a blob: URL)")
        self.assertEqual(playback.line({**native, "why": "fresh"}),
                         "media: a script processor (a voice recorder, say) made as WebKit's own; its context was made "
                         "while no spare context with Natter Den's worklet recorder's module was ready, and hasn't "
                         "rested since")
        self.assertEqual(playback.line({**self.RECORDER, "why": "restarted", "module": "data"}),
                         "media: Natter Den's worklet recorder's module loaded into a context the page resumed "
                         "meanwhile, so WebKit likely stopped and started that context's sound output (from a data: URL)")
        for path, whys in playback.RECORDER_WHYS.items():
            for why in whys:
                self.assertTrue(playback.line({**self.RECORDER, "path": path, "why": why,
                                               "module": "" if path == "native" else "data"}).startswith("media: "))

    def test_recording_lines(self):
        self.assertEqual(playback.line(self.RECORDING),
                         "media: a voice recording 39.2 s in 40.1 s (40.0 s on its context's clock, of which the audio "
                         "thread skipped 0.8 s): 459 chunks of 4096 at 48000 Hz (microphone 48000 Hz), 12 handed over "
                         "more than a chunk late (1.0 s of its sound lost), longest gap 420 ms; the page's main thread "
                         "up to 380 ms late; the microphone muted once; its context never suspended; measured until "
                         "the microphone stopped")
        self.assertIn(" (the microphone's rate not said), none handed over late, ",
                      playback.line({**self.RECORDING, "microphone": None, "behind": 0, "lost": 0}))
        self.assertIn("; the microphone muted twice, unmuted once, ended early 3 times; its context suspended 4 times "
                      "(twice by the idle audio); measured until its context closed",
                      playback.line({**self.RECORDING, "muted": 2, "unmuted": 1, "ended": 3, "suspended": 4,
                                     "rested": 2, "end": "closed"}))
        self.assertIn("; the microphone never muted or ended early; its context suspended once; measured until the "
                      "recorder was disconnected",
                      playback.line({**self.RECORDING, "muted": 0, "suspended": 1, "end": "disconnected"}))
        self.assertTrue(playback.line({**self.RECORDING, "chunks": 1, "recorded": 85, "behind": 0, "lost": 0,
                                       "end": "long"}).endswith("; measured for its first 10 minutes"))
        self.assertIn(": 1 chunk of 4096 at ", playback.line({**self.RECORDING, "chunks": 1, "recorded": 85}))

    def test_only_listed_types(self):
        played = ParseTest.PLAYED
        for source in ("audio/ogg", "video/mp4", "audio-other", "video-other"):
            self.assertEqual(parse_message({**played, "source": source}), {**played, "source": source})
        for source in ("audio/x-secret-person", "video/secret", "audio/", "audio-Other"):
            self.assertIsNone(parse_message({**played, "source": source}), source)


class AccountLineTest(unittest.TestCase):
    """The account writes a few lines a page, with WebKit's and GStreamer's versions on the first."""

    TOKEN = "f" * 32

    def account(self, policy=WebKit.HardwareAccelerationPolicy.ALWAYS):
        settings = SimpleNamespace(get_hardware_acceleration_policy=lambda: policy)
        account = SimpleNamespace(_token=self.TOKEN, _trusted=lambda: True, name="one", loaded_at=1.0,
                                  _media_lines=(None, 0, 0, 0), view=SimpleNamespace(get_settings=lambda: settings))
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

    def test_videos_are_counted_apart(self):
        # Videos' lines used up first leave the sound its own, and the other way round.
        account = self.account()
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(30):
                self.send(account, ParseTest.VIDEO)
            for _ in range(30):
                self.send(account, ParseTest.PLAYED)
            self.send(account, ParseTest.VIDEO)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), playback.VIDEO_LINES_PER_PAGE + playback.LINES_PER_PAGE)
        self.assertEqual(lines[0], "natter: one: media: an <video> (video/mp4) 720x1280 played 12.3 s in 14.1 s: "
                                   "waited 3 times (1.6 s), stalled twice; 350 frames, 4 gaps over 100 ms (longest "
                                   "640 ms), dropped 12 of 360; the page's main thread up to 420 ms late")
        self.assertEqual(lines[-1], "natter: one: media: sound played: an <audio> (audio/ogg), through a Web Audio "
                                    "context, running")

    def test_recordings_are_counted_apart(self):
        # Recordings' lines used up leave the sound and the videos theirs, and the other way round.
        account = self.account()
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(30):
                self.send(account, ParseTest.RECORDING)
            for _ in range(30):
                self.send(account, ParseTest.PLAYED)
                self.send(account, ParseTest.VIDEO)
            self.send(account, ParseTest.RECORDING)
            account.loaded_at = 2.0  # a new page: lines again
            self.send(account, ParseTest.RECORDING)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), playback.RECORDING_LINES_PER_PAGE + playback.LINES_PER_PAGE
                         + playback.VIDEO_LINES_PER_PAGE + 1)
        self.assertEqual(lines[0], "natter: one: media: a voice recording 39.2 s in 40.1 s (40.0 s on its context's "
                                   "clock, of which the audio thread skipped 0.8 s): 459 chunks of 4096 at 48000 Hz "
                                   "(microphone 48000 Hz), 12 handed over more than a chunk late (1.0 s of its sound "
                                   "lost), longest gap 420 ms; the page's main thread up to 380 ms late; the "
                                   "microphone muted once; its context never suspended; measured until the "
                                   "microphone stopped")
        self.assertEqual(sum("a voice recording" in line for line in lines), playback.RECORDING_LINES_PER_PAGE + 1)
        self.assertEqual(lines[-1], lines[0])

    def test_no_frames_counted_without_hardware_acceleration(self):
        # WebKit calls back none then: the line says they weren't counted, and the first says why.
        account = self.account(WebKit.HardwareAccelerationPolicy.NEVER)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("natter.account.media_gstreamer_version", return_value="1.28.8"):
            self.send(account, ParseTest.SUPPORT)
            self.send(account, {**ParseTest.VIDEO, "frames": 0, "gaps": 1, "longest": 14100})
        lines = out.getvalue().splitlines()
        self.assertRegex(lines[0], r"^natter: one: media: WebKitGTK \d+\.\d+\.\d+, GStreamer 1\.28\.8, hardware "
                                   r"acceleration off; <audio> plays ")
        self.assertEqual(lines[1], "natter: one: media: an <video> (video/mp4) 720x1280 played 12.3 s in 14.1 s: "
                                   "waited 3 times (1.6 s), stalled twice; frames not counted, dropped 12 of 360; the "
                                   "page's main thread up to 420 ms late")


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
