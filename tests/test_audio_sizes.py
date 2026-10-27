"""How much the bridge's idle-audio code holds: bridge.js audio() counts every set and map it keeps
(run here in JavaScriptCore against stand-ins for Web Audio), and audiosizes.py logs the counts
once a page is up and after that only when one passes its limit or keeps growing (app.py's
About shows them too). Counts only, never names or URLs."""

import itertools
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("JavaScriptCore", "6.0")
from gi.repository import GLib, JavaScriptCore  # noqa: E402

from natter import app as natter_app  # noqa: E402
from natter.app import Natter, debug_info  # noqa: E402
from natter.audiosizes import AFTER_CALL, CHECK, FIRST, KEYS, LIMITS, SCRIPT, STREAK, Rule, Watch, debug_line, parse  # noqa: E402

BRIDGE = (Path(__file__).resolve().parent.parent / "natter" / "bridge.js").read_text()
START = "  // ---- Idle audio"
END = "  // ---- Videos aren't pictures"

# Just enough of a page for the idle-audio section: AudioContexts that suspend, resume and close,
# source nodes, a worklet, media elements and streams, a clock, and the notifications map the
# bridge keeps above it (shown).
FAKES = """
var window = globalThis;
let clock = 100000;
var performance = { now: () => clock };
var document = { querySelectorAll: () => [] };
function setTimeout() { return 0; }
function setInterval() { return 0; }
class Target {
  constructor() { this.listeners = {}; }
  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }
  fire(type) { (this.listeners[type] || []).forEach((listener) => listener()); }
}
window.AudioContext = class extends Target {
  constructor() { super(); this.state = "running"; }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
  resume() { this.state = "running"; return Promise.resolve(); }
  close() { this.state = "closed"; this.fire("statechange"); return Promise.resolve(); }
  createMediaStreamSource() { return {}; }
  createMediaElementSource() { return {}; }
};
window.AudioWorkletNode = class { constructor(context) { this.context = context; } };
window.AudioScheduledSourceNode = class extends Target {
  constructor(context) { super(); this.context = context; this.buffer = null; this.loop = false; }
  start() {}
};
window.OscillatorNode = class extends window.AudioScheduledSourceNode {};
window.HTMLMediaElement = class extends Target {
  constructor() { super(); this.paused = true; this.ended = false; this.isConnected = true; }
  play() { this.paused = false; return Promise.resolve(); }
};
class Stream {
  constructor() { this.track = { readyState: "live" }; }
  getTracks() { return [this.track]; }
}
const shown = new Map();
"""


def section() -> str:
    return BRIDGE[BRIDGE.index(START):BRIDGE.index(END)]


def run(script: str):
    context = JavaScriptCore.Context()
    value = context.evaluate(FAKES + section() + script, -1)
    exception = context.get_exception()
    if exception is not None:
        raise AssertionError(exception.get_message())
    return json.loads(value.to_json(0))


def sizes(**counts) -> dict:
    return {key: counts.get(key, 0) for key in KEYS}


class AudioTest(unittest.TestCase):
    """audio() in the page: the sizes of every set and map the bridge keeps for idle audio."""

    def test_every_set_and_map_is_counted(self):
        self.assertEqual(run("audio().sizes;"), sizes())  # the keys audiosizes.py reads, all there

    def test_contexts_those_rested_and_those_in_a_call(self):
        result = run("""
            const made = [new window.AudioContext(), new window.AudioContext(), new window.AudioContext()];
            new window.AudioWorkletNode(made[2], "voip-capture");
            rest();  // idle long enough: the two not in a call are suspended
            const before = audio().sizes;
            made[0].close();
            [before, audio().sizes, audio().contexts];
        """)
        before, after, states = result
        self.assertEqual(before, sizes(contexts=3, rested=2, calls=1))
        self.assertEqual(after, sizes(contexts=2, rested=1, calls=1))  # a closed context is let go
        self.assertEqual(states, ["suspended", "running"])  # what audio() said before, still said

    def test_playing_sources_and_live_streams(self):
        result = run("""
            const context = new window.AudioContext();
            const nodes = [1, 2, 3].map(() => new window.OscillatorNode(context));
            nodes.forEach((node) => node.start());
            const stream = new Stream();
            context.createMediaStreamSource(stream);
            const playing = audio().sizes;
            nodes[0].fire("ended");
            stream.track.readyState = "ended";
            const fewer = audio().sizes;
            context.close();
            [playing, fewer, audio().sizes];
        """)
        playing, fewer, closed = result
        self.assertEqual(playing, sizes(contexts=1, sources=3, streams=1))
        self.assertEqual(fewer, sizes(contexts=1, sources=2))
        self.assertEqual(closed, sizes())  # its sources went with the context

    def test_media_elements(self):
        result = run("""
            const context = new window.AudioContext();
            const kept = new window.HTMLMediaElement();
            const gone = new window.HTMLMediaElement();
            context.createMediaElementSource(kept);
            context.createMediaElementSource(gone);
            const both = audio().sizes.elements;
            gone.isConnected = false;  // out of the page and not playing
            [both, audio().sizes.elements, audio().elements];
        """)
        self.assertEqual(result, [2, 1, 1])

    def test_shown_notifications(self):
        result = run("""
            for (let id = 1; id <= 5; id++) shown.set(id, { title: "Someone", body: "Words" });
            audio().sizes;
        """)
        self.assertEqual(result, sizes(notifications=5))

    def test_counts_only(self):
        result = run("""
            const context = new window.AudioContext();
            context.createMediaElementSource(new window.HTMLMediaElement());
            shown.set(1, { title: "Someone", body: "Words", icon: "https://pps.whatsapp.net/a.jpg" });
            JSON.stringify(audio());
        """)
        self.assertNotIn("Someone", result)
        self.assertNotIn("Words", result)
        self.assertNotIn("http", result)
        answer = json.loads(result)
        self.assertTrue(all(type(answer["sizes"][key]) is int for key in KEYS))
        # What audio() said before is all still there for a debug-eval.
        self.assertEqual(set(answer) - {"sizes"},
                         {"contexts", "sources", "streams", "media", "elements", "idle", "rtc"})

    def test_no_storage_events_or_whatsapp_internals(self):
        code = section()
        for banned in ("dispatchEvent", "post(", "localStorage", "sessionStorage", "indexedDB", "require(", "__d(",
                       "webpack"):
            self.assertNotIn(banned, code)

    def test_the_script_asks_for_the_sizes(self):
        self.assertIn("window.__natter.audio().sizes", SCRIPT)
        self.assertIn("audio,", BRIDGE[BRIDGE.index("window.__natter = {"):])


class ParseTest(unittest.TestCase):
    def test_whole_counts_only(self):
        good = sizes(contexts=2, notifications=300)
        self.assertEqual(parse(good), good)
        self.assertEqual(parse({**good, "extra": "anything"}), good)  # what else it says is dropped
        for bad in (None, [], "sizes", {**good, "contexts": True}, {**good, "sources": 1.5},
                    {**good, "streams": -1}, {**good, "elements": "3"}, {**good, "rested": 10**8},
                    {key: 0 for key in KEYS if key != "calls"}):
            self.assertIsNone(parse(bad), bad)


class RuleTest(unittest.TestCase):
    """Which checks get a line: once at ready, then a size past its limit or growing check after check."""

    def test_at_ready_then_quiet(self):
        rule = Rule()
        self.assertEqual(rule.check(sizes(contexts=1)), "at ready")
        for _ in range(50):
            self.assertIsNone(rule.check(sizes(contexts=1)))

    def test_ups_and_downs_are_never_told(self):
        rule = Rule()
        rule.check(sizes())
        for n in range(60):
            self.assertIsNone(rule.check(sizes(contexts=1 + n % 2, sources=n % 3, elements=n % 4, streams=n % 2)))

    def test_past_a_limit_and_its_doubles(self):
        rule = Rule()
        rule.check(sizes())
        limit = LIMITS["sources"]
        self.assertEqual(rule.check(sizes(sources=limit + 1)), f"growing (sources past {limit})")
        self.assertIsNone(rule.check(sizes(sources=limit * 2)))
        self.assertIsNone(rule.check(sizes(sources=limit)))
        self.assertEqual(rule.check(sizes(sources=limit * 2 + 1)), f"growing (sources past {limit * 2})")
        self.assertEqual(rule.check(sizes(sources=limit * 9)), f"growing (sources past {limit * 4})")
        self.assertIsNone(rule.check(sizes(sources=limit * 16)))  # the limit went past it at once

    def test_past_a_limit_at_ready_isnt_told_again(self):
        rule = Rule()
        self.assertEqual(rule.check(sizes(elements=LIMITS["elements"] + 1)), "at ready")
        self.assertIsNone(rule.check(sizes(elements=LIMITS["elements"] + 1)))

    def test_growing_check_after_check(self):
        rule = Rule()
        rule.check(sizes())
        told = [rule.check(sizes(elements=n)) for n in range(1, STREAK * 3 + 1)]
        self.assertEqual([i + 1 for i, line in enumerate(told) if line], [STREAK, STREAK * 3])  # a run doubles
        self.assertEqual(told[STREAK - 1], f"growing (elements up {STREAK} checks running)")

    def test_a_pause_starts_the_run_again(self):
        rule = Rule()
        rule.check(sizes())
        counts = list(range(1, STREAK)) + [STREAK - 1] + list(range(STREAK, STREAK * 2 - 1))
        self.assertEqual([rule.check(sizes(elements=n)) for n in counts], [None] * len(counts))

    def test_notifications_grow_to_their_cap_untold(self):
        rule = Rule()
        rule.check(sizes())
        self.assertEqual([rule.check(sizes(notifications=n)) for n in range(10, 301, 10)], [None] * 30)
        self.assertEqual(rule.check(sizes(notifications=301)), "growing (notifications past 300)")

    def test_both_reasons_at_once(self):
        rule = Rule()
        rule.check(sizes())
        for n in range(1, STREAK):
            self.assertIsNone(rule.check(sizes(contexts=n)))
        self.assertEqual(rule.check(sizes(contexts=STREAK, sources=LIMITS["sources"] + 1)),
                         f"growing (contexts up {STREAK} checks running; sources past {LIMITS['sources']})")


class Page:
    """Just enough of an Account: its name, its page, and run(), answered by answer()."""

    def __init__(self):
        self.name, self.loaded_at, self.ready, self.removed = "Personal", 1.0, True, False
        self.calling = False
        self.asked = []

    def run(self, body, args=None, done=None):
        self.asked.append((body, args, done))

    def answer(self, value):
        _body, _args, done = self.asked.pop(0)
        done(value)


class WatchTest(unittest.TestCase):
    """One account's checks, with the clock's timers and the log stood in for."""

    def setUp(self):
        self.debug = GLib.getenv("NATTER_DEBUG")
        GLib.unsetenv("NATTER_DEBUG")
        self.addCleanup(lambda: self.debug is not None and GLib.setenv("NATTER_DEBUG", self.debug, True))
        self.timers, self.cancelled, self.lines, self.ids = {}, [], [], itertools.count(1)
        self.page = Page()
        self.watch = Watch(self.page, later=self.later, cancel=self.cancelled.append, write=self.lines.append)

    def later(self, seconds, callback):
        timer = next(self.ids)
        self.timers[timer] = (seconds, callback)
        return timer

    def fire(self):
        """The timer that's due goes off; its seconds."""
        timer = self.watch._timer
        seconds, callback = self.timers.pop(timer)
        self.assertFalse(callback())  # one-shot: the next is planned anew
        return seconds

    def test_once_at_ready_then_only_when_growing(self):
        self.watch.ready()
        self.assertEqual(self.fire(), FIRST)
        body, args, _done = self.page.asked[0]
        self.assertEqual((body, args), (SCRIPT, None))
        self.page.answer(sizes(contexts=1, notifications=2))
        self.assertEqual(self.lines, ["natter: Personal: audio sets at ready: contexts 1, rested 0, calls 0, "
                                      "elements 0, sources 0, streams 0, held 0, notifications 2"])
        for _ in range(20):
            self.assertEqual(self.fire(), CHECK)
            self.page.answer(sizes(contexts=1, notifications=2))
        self.assertEqual(len(self.lines), 1)  # never a line every check
        self.fire()
        self.page.answer(sizes(contexts=1, sources=LIMITS["sources"] + 1))
        self.assertEqual(self.lines[1], f"natter: Personal: audio sets growing (sources past {LIMITS['sources']}): "
                                        f"contexts 1, rested 0, calls 0, elements 0, sources {LIMITS['sources'] + 1}, "
                                        "streams 0, held 0, notifications 0")

    def test_never_every_minute(self):
        self.assertGreaterEqual(CHECK, 300)
        self.assertGreaterEqual(CHECK * STREAK, 3600)

    def test_up_again_on_the_same_page_goes_on(self):
        self.watch.ready()
        self.fire()
        self.page.answer(sizes())
        self.watch.ready()  # a link opened in the page: not a new page
        self.assertEqual(self.fire(), CHECK)
        self.page.answer(sizes())
        self.assertEqual(len(self.lines), 1)

    def test_a_new_page_starts_again(self):
        self.watch.ready()
        self.fire()
        self.page.answer(sizes(sources=3))
        self.fire()  # asked the old page...
        self.page.loaded_at = 2.0  # ...which went before it answered
        self.page.answer(sizes(sources=LIMITS["sources"] + 1))
        self.assertEqual(len(self.lines), 1)  # not the new page's
        self.watch.ready()
        self.assertEqual(self.cancelled, [3])  # the old page's next check
        self.assertEqual(self.fire(), FIRST)
        self.page.answer(sizes())
        self.assertEqual(len(self.lines), 2)
        self.assertIn("audio sets at ready", self.lines[1])

    def test_not_asked_while_the_page_is_down(self):
        self.watch.ready()
        self.page.ready = False
        self.fire()
        self.assertEqual(self.page.asked, [])
        self.assertEqual(self.watch._timer and self.timers[self.watch._timer][0], CHECK)  # still checking

    def test_put_off_during_a_call(self):
        # The check runs JavaScript in the page and logs: not while the call's sound goes through both.
        self.watch.ready()
        self.page.calling = True
        self.assertEqual(self.fire(), FIRST)
        self.assertEqual(self.page.asked, [])
        self.assertEqual(self.fire(), AFTER_CALL)  # tried again a minute on, still in the call
        self.assertEqual(self.page.asked, [])
        self.page.calling = False
        self.assertEqual(self.fire(), AFTER_CALL)
        self.page.answer(sizes())
        self.assertIn("audio sets at ready", self.lines[0])
        self.assertEqual(self.timers[self.watch._timer][0], CHECK)

    def test_no_bridge_no_line(self):
        self.watch.ready()
        self.fire()
        self.page.answer(None)
        self.assertEqual(self.lines, [])
        self.assertIsNone(self.watch.last)

    def test_stops_with_the_account(self):
        self.watch.ready()
        self.watch.stop()
        self.assertEqual(self.cancelled, [1])
        self.assertEqual(self.watch._timer, 0)
        self.watch.ready()  # (the same page)
        self.assertEqual(self.watch._timer, 0)
        self.page.loaded_at = 2.0
        self.watch.ready()
        self.page.removed = True
        self.fire()
        self.assertEqual((self.page.asked, self.watch._timer), ([], 0))

    def test_with_natter_debug_every_check(self):
        GLib.setenv("NATTER_DEBUG", "1", True)
        self.addCleanup(GLib.unsetenv, "NATTER_DEBUG")
        self.watch.ready()
        for _ in range(3):
            self.fire()
            self.page.answer(sizes())
        self.assertEqual([line.split(":")[2] for line in self.lines],
                         [" audio sets at ready", " audio sets checked", " audio sets checked"])

    def test_lines_are_counts_only(self):
        self.watch.ready()
        steps = [sizes(), sizes(sources=99), sizes(sources=99, elements=1)] + \
                [sizes(sources=99, elements=n, notifications=999) for n in range(2, 2 + STREAK)]
        for step in steps:
            self.fire()
            self.page.answer({**step, "title": "Someone", "url": "https://example.org/"})
        self.assertGreater(len(self.lines), 2)
        shape = re.compile(r"natter: Personal: audio sets (at ready|growing \([a-z0-9 ;]+\)): " +
                           ", ".join(rf"{key} \d+" for key in KEYS))
        for line in self.lines:
            self.assertRegex(line, shape)
            self.assertNotIn("Someone", line)
            self.assertNotIn("http", line)

    def test_ask_is_not_a_check(self):
        got = []
        self.watch.ask(got.append)
        self.page.answer(sizes(contexts=2))
        self.assertEqual(got, [sizes(contexts=2)])
        self.watch.ask(got.append)
        self.page.answer({"contexts": "two"})
        self.assertEqual(got[1:], [None])
        self.assertEqual(self.lines, [])


class AboutTest(unittest.TestCase):
    """About's troubleshooting text has each account's sizes, asked of its page as it opens."""

    def test_debug_line(self):
        self.assertEqual(debug_line(sizes(contexts=1, notifications=4)),
                         "Audio sets: contexts 1, rested 0, calls 0, elements 0, sources 0, streams 0, held 0, "
                         "notifications 4")
        self.assertEqual(debug_line(sizes(), 1, 2)[:23], "Audio sets (account 2):")
        self.assertIn("not known", debug_line(None))

    def test_in_the_debug_info(self):
        info = debug_info("in the page", [], False, ["Camera: switch on"], [debug_line(sizes(streams=1))])
        self.assertIn("\nAudio sets: contexts 0, rested 0, calls 0, elements 0, sources 0, streams 1, held 0, "
                      "notifications 0\n", info)
        self.assertNotIn("Audio sets", debug_info("in the page", [], False))

    def test_about_asks_each_account(self):
        made = []

        class Dialog:
            def __init__(self, **props):
                self.infos = []
                made.append(self)

            def add_legal_section(self, *args):
                pass

            def set_debug_info(self, info):
                self.infos.append(info)

            def present(self, *args):
                pass

        adw = SimpleNamespace(AboutDialog=Dialog, get_major_version=lambda: 1, get_minor_version=lambda: 5,
                              get_micro_version=lambda: 0)
        gtk = SimpleNamespace(License=SimpleNamespace(GPL_3_0="gpl3", CUSTOM="custom"),
                              get_major_version=lambda: 4, get_minor_version=lambda: 14, get_micro_version=lambda: 0)
        pages = [Page(), Page()]
        for page in pages:
            page.audio_sizes = Watch(page, later=lambda *args: 1, cancel=lambda timer: None, write=print)
        pages[0].audio_sizes.last = sizes(contexts=1)  # its last check's
        fake = SimpleNamespace(calls_in_page=True, calls=None, camera_fallback=False, window=None,
                               _media=([], ""), accounts=pages,
                               media_state=lambda kind: {"on": True, "desktop": None, "present": None,
                                                         "lockdown": False})
        with mock.patch.multiple(natter_app, Adw=adw, Gtk=gtk):
            Natter.show_about(fake)
        about, = made
        self.assertIn("Audio sets (account 1): contexts 1,", about.infos[-1])
        self.assertIn("Audio sets (account 2): not known", about.infos[-1])
        pages[1].answer(sizes(elements=3))
        self.assertIn("Audio sets (account 2): contexts 0, rested 0, calls 0, elements 3,", about.infos[-1])
        pages[0].answer(None)  # its page went since
        self.assertIn("Audio sets (account 1): not known", about.infos[-1])
        self.assertEqual(len(about.infos), 3)


if __name__ == "__main__":
    unittest.main()
