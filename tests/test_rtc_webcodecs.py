"""The page's video codecs (rtc.js) and their log (rtc.py).

rtc.js rewrites a call's H.264 decoders for WebKitGTK and puts a ceiling on every decoder and
encoder in the page: after a video attached to a chat was decoded whole with its frames kept
(12 GB). Its code runs here in gjs against fake codecs that behave as WebKitGTK's do on the points
that matter (state, key frames, queue sizes, "Decode error" for Annex B without a description,
frames as pooled pictures, a frame made from a frame sharing its picture), on a virtual clock.
Without gjs those tests skip.
"""

import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from natter import rtc

RTC_JS = Path(__file__).resolve().parent.parent / "natter" / "rtc.js"
GJS = shutil.which("gjs") or ("/usr/bin/gjs" if Path("/usr/bin/gjs").exists() else None)
_results: dict = {}


def results(mode: str = "") -> dict:
    """Every scenario's result, from one run of the harness. `mode` "native-rtc" runs it in a
    page whose WebKit has an RTCPeerConnection of its own, "no-calls" in one without the calls'
    answering handler (calls in a window of their own), "no-handlers" in one with neither."""
    if mode not in _results:
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "harness.js"
            script.write_text(HARNESS)
            argv = [GJS, str(script), str(RTC_JS)] + (["bounded", mode] if mode else [])
            done = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        found = {}
        for line in done.stdout.splitlines():
            if line.startswith("RESULT "):
                entry = json.loads(line[7:])
                found[entry["name"]] = entry["result"]
            elif line.startswith("PROBLEMS "):
                found["problems"] = json.loads(line[9:])
        if "problems" not in found:
            raise AssertionError(f"the harness didn't finish: {done.stdout[-2000:]}{done.stderr[-2000:]}")
        _results[mode] = found
    return _results[mode]


@unittest.skipUnless(GJS, "needs gjs")
class HarnessCase(unittest.TestCase):
    def scenario(self, name: str, mode: str = "") -> dict:
        found = results(mode)
        self.assertEqual(found["problems"], [])
        self.assertNotIn("harnessError", found[name], found[name].get("harnessError"))
        return found[name]


class RewriteTest(HarnessCase):
    def test_a_calls_decoder_is_rewritten_and_decodes_every_frame(self):
        call = self.scenario("rewrite")
        self.assertEqual(call["callStateBeforeKey"], "configured")  # as before: configured for real at the first key frame
        self.assertEqual(call["callThrown"], {})
        self.assertEqual(call["call"]["outputs"], 60)
        self.assertEqual(call["call"]["errors"], [])
        configure, first, second = call["call"]["log"]
        self.assertEqual(configure, ["configure", "avc1.42001f", 22])  # an avcC description from the SPS/PPS
        self.assertEqual(first[:3], ["decode", "key", "5"])  # length-prefixed, without SPS, PPS and delimiters
        self.assertEqual(second[:3], ["decode", "delta", "1"])
        self.assertTrue(call["call"]["parsed"])

    def test_any_other_decoder_is_webkits_own(self):
        found = self.scenario("rewrite")
        plain = found["plain"]
        self.assertEqual(plain["configures"], [["configure", "avc1.42001f", None]])
        self.assertTrue(plain["firstBytesUnchanged"])  # Annex B as the page gave it
        self.assertEqual(plain["errors"], ["EncodingError: Decode error"])  # what WebKit says without Natter
        self.assertEqual((plain["outputs"], plain["state"]), (0, "closed"))
        self.assertEqual(found["latencyFalse"]["configures"], [["configure", "avc1.42001f", None]])
        self.assertEqual(found["latencyFalse"]["errors"], ["EncodingError: Decode error"])
        self.assertEqual(found["described"]["configures"], [["configure", "avc1.42001f", 22]])
        self.assertEqual((found["described"]["outputs"], found["described"]["errors"]), (10, []))

    def test_avc3_is_wired_as_avc1(self):
        avc3 = self.scenario("rewrite")["avc3"]
        self.assertEqual(avc3["configures"], [["configure", "avc1.42001f", 22]])
        self.assertEqual((avc3["outputs"], avc3["errors"]), (1, []))

    def test_annex_b_decoders_without_optimize_for_latency_are_counted(self):
        self.assertEqual(self.scenario("rewrite")["annexb"], 2)  # the plain one and optimizeForLatency: false

    def test_whatsapps_startup_test(self):
        probe = self.scenario("probe")
        self.assertEqual(probe["outputs"], 1)
        self.assertEqual(probe["events"], ["output", "flushed"])  # flush() after the frame
        self.assertEqual(probe["errors"], [])

    def test_reset_and_configure_in_a_call_waits_for_the_next_key_frame(self):
        found = self.scenario("requireKeyframe")
        self.assertEqual(found["before"], 10)
        self.assertEqual(found["afterDeltas"], 10)  # deltas before a key frame dropped, quietly as before
        self.assertEqual(found["after"], 15)
        self.assertEqual((found["thrown"], found["errors"], found["state"]), ({}, [], "configured"))

    def test_a_rewritten_decoder_fed_a_file_fails(self):
        found = self.scenario("pace")
        self.assertEqual(found["file"]["errors"], ["NotSupportedError: H.264 without a description is only decoded in real time"])
        self.assertEqual(found["file"]["state"], "closed")
        self.assertLess(found["file"]["outputs"], 300)
        self.assertEqual(list(found["fileThrown"]), ["InvalidStateError"])  # only after it failed, as a closed decoder
        for fed in ("realTime", "inCall"):
            self.assertEqual(found[fed]["errors"], [], fed)
            self.assertEqual(found[fed]["state"], "configured", fed)
        self.assertEqual(found["realTime"]["outputs"], 120)
        self.assertEqual(found["inCall"]["outputs"], 300)


class EncoderTest(HarnessCase):
    def test_a_flood_outside_a_call_fails_once_32_wait(self):
        flood = self.scenario("encoder")["flood"]
        self.assertEqual(flood["errors"], ["QuotaExceededError"])
        self.assertFalse(flood["syncError"])  # told later, as WebKit tells
        self.assertEqual(flood["state"], "closed")
        self.assertEqual(flood["maxQueue"], 32)
        self.assertLessEqual(flood["maxLive"], 32)
        self.assertEqual(flood["thrown"], ["InvalidStateError"])  # encode() on the closed encoder, as WebKit

    def test_in_a_call_frames_are_dropped_but_never_key_frames(self):
        found = self.scenario("encoder")
        call = found["call"]
        self.assertEqual((call["errors"], call["state"]), ([], "configured"))
        self.assertEqual(call["keyEncodes"], 30)  # every key frame asked for reached the encoder
        self.assertLessEqual(call["maxQueue"], 32)
        self.assertGreater(found["drops"]["2"], 0)
        realtime = found["realtime"]  # latencyMode "realtime" counts as a call
        self.assertEqual((realtime["errors"], realtime["state"]), ([], "configured"))
        self.assertGreater(found["drops"]["5"], 0)

    def test_a_page_that_waits_and_the_startup_test_never_trip_it(self):
        found = self.scenario("encoder")
        self.assertEqual((found["waits"]["outputs"], found["waits"]["errors"]), (300, []))
        self.assertEqual((found["probe"]["outputs"], found["probe"]["errors"]), (1, []))
        self.assertEqual((found["drops"]["3"], found["drops"]["4"]), (0, 0))


class HeldFramesTest(HarnessCase):
    def test_no_more_than_64_frames_out_and_the_rest_waits_honestly(self):
        for mode in ("serial", "dispatchAll"):  # WebKit taking chunks one at a time, or all at once
            found = self.scenario("held")[mode]
            self.assertEqual(found["thrown"], {}, mode)
            self.assertEqual(found["first"]["outputs"], 64, mode)
            self.assertEqual(found["first"]["queue"], 236, mode)  # decodeQueueSize counts the chunks waiting
            self.assertEqual(found["first"]["maxLive"], 64, mode)
            self.assertEqual(found["failed"]["maxLive"], 64, mode)

    def test_frames_let_go_copies_and_collected_frames(self):
        for mode in ("serial", "dispatchAll"):
            found = self.scenario("held")[mode]
            self.assertEqual(found["afterTen"], {"outputs": 74, "queue": 226}, mode)
            self.assertEqual(found["copiesHeld"]["outputs"], 74, mode)  # a copy holds the picture
            self.assertEqual(found["copiesClosed"]["outputs"], 79, mode)
            self.assertEqual(found["collected"]["outputs"], 89, mode)  # collected without close()

    def test_held_ten_seconds_outside_a_call_fails(self):
        for mode in ("serial", "dispatchAll"):
            failed = self.scenario("held")[mode]["failed"]
            self.assertEqual(failed["errors"], ["QuotaExceededError: Too many decoded video frames are held at once"], mode)
            self.assertEqual((failed["state"], failed["queue"]), ("closed", 0), mode)
            self.assertEqual(failed["flush"], "QuotaExceededError", mode)  # a flush waiting with them fails too

    def test_a_frame_made_from_a_frame_holds_it_too(self):
        # new VideoFrame(frame, {timestamp}) shares the picture in WebKit: kept while the frame it
        # was made from is let go, it's still held (a decoder with a description, at file speed).
        found = self.scenario("rewrap")
        self.assertEqual(found["described"]["outputs"], 64)
        self.assertEqual(found["described"]["live"], 64)
        self.assertEqual(found["described"]["queue"], 236)
        self.assertEqual(found["describedFailed"]["errors"],
                         ["QuotaExceededError: Too many decoded video frames are held at once"])
        self.assertEqual(found["describedFailed"]["state"], "closed")
        self.assertLessEqual(found["describedFailed"]["maxLive"], 64)
        self.assertEqual(found["openLogged"], 64)

    def test_a_frame_made_again_from_a_made_frame_and_letting_go(self):
        found = self.scenario("rewrap")
        self.assertEqual(found["chain"], {"outputs": 64, "afterLetGo": 74})
        self.assertEqual(found["unrelated"], {"outputs": 10, "live": 10})  # frames not from a decoder aren't counted

    def test_reset_between_key_frames_doesnt_start_over(self):
        # A call's decoder reset and configured again between GOPs, fed at file speed: the pace
        # check starts over each time, the ceiling doesn't.
        found = self.scenario("rewrap")["resetBetween"]
        self.assertEqual(found["outputs"], 64)
        self.assertLessEqual(found["maxLive"], 64)

    def test_fresh_decoders_one_after_another_share_the_pages_ceiling(self):
        # A new decoder for each 30-frame GOP, frames kept: each stays under its own 64, so the
        # page's 128 for all of them together is what holds.
        found = self.scenario("split")
        for kind in ("described", "rewritten"):
            self.assertEqual(found[kind]["outputs"], 128, kind)
            self.assertLessEqual(found[kind]["maxLive"], 128, kind)
            self.assertEqual(found[kind]["errors"], ["QuotaExceededError"] * 4, kind)
            self.assertEqual(found[kind]["states"], ["configured"] * 4 + ["closed"] * 4, kind)
        self.assertEqual(found["pageOpen"], 128)  # in the page's counts for the log
        self.assertEqual(found["afterLetGo"], {"outputs": 138, "errors": []})  # room on the page goes to who waits

    def test_a_group_call_is_never_held_back(self):
        found = self.scenario("groupCall")
        self.assertEqual(found, {"outputs": 32 * 60, "errors": [], "held": 0})

    def test_in_a_call_it_waits_as_long_as_it_takes(self):
        found = self.scenario("held")
        self.assertEqual(found["inCall"], {"outputs": 64, "errors": [], "state": "configured", "queue": 36})
        self.assertEqual(found["afterCall"]["errors"], ["QuotaExceededError: Too many decoded video frames are held at once"])

    def test_what_webkit_refuses_at_once_is_still_refused_at_once(self):
        self.assertEqual(self.scenario("held")["deltaAfterFlush"], "DataError")


class CountsTest(HarnessCase):
    def test_only_the_allowed_fields_and_always_the_token(self):
        found = self.scenario("messages")
        self.assertTrue(found["allTokened"])
        self.assertGreater(found["count"], 10)
        common = {"op", "kind", "token"}
        allowed = {"dec": common | {"id", "codec", "desc", "lat", "rewrite", "w", "h", "out", "open", "held", "queue",
                                    "drops", "err"},
                   "enc": common | {"id", "codec", "mode", "w", "h", "out", "queue", "drops", "err"},
                   "page": common | {"workers", "alive", "annexb", "open"}}
        for kind, keys in found["keys"].items():
            self.assertLessEqual(set(keys), allowed[kind], kind)
        for message in found["sample"] + found["plain"]:
            message = {key: value for key, value in message.items() if key != "token"}
            self.assertIsNotNone(rtc.webcodecs_line(message), message)  # rtc.py takes what rtc.js sends

    def test_at_most_once_a_second_for_each(self):
        self.assertGreaterEqual(self.scenario("messages")["closestMs"], 1000)

    def test_an_annex_b_decoder_without_optimize_for_latency_shows_in_the_log(self):
        plain = dict(self.scenario("messages")["plain"][0])
        plain.pop("token")
        self.assertEqual(rtc.webcodecs_line(plain)[1],
                         f"dec#{plain['id']} avc1.42001f desc=no lat=no rewrite=off 1280x720 out=0 open=0 held=0 queue=0 "
                         "drops=0 err=EncodingError")

    def test_workers_are_only_counted(self):
        found = self.scenario("workers")
        self.assertEqual(found["workers"], 3)
        self.assertEqual(found["thrown"], "TypeError")  # no arguments: the browser's own error
        self.assertTrue(found["isNative"])
        self.assertEqual(found["terminateCalled"], 1)  # the browser's own terminate() still runs
        self.assertTrue(found["terminateLooksNative"])

    def test_workers_alive_and_created(self):
        # 28 Oct 2026: "workers=N" only went up, to 40, as it counted every worker ever started.
        found = self.scenario("workers")
        self.assertEqual(found["alive"], 3)
        self.assertEqual((found["afterTerminate"], found["terminatedCreated"]), (2, 3))  # terminated once, counted once
        self.assertEqual((found["afterCollect"], found["collectedCreated"]), (1, 3))  # one collected, never terminated


class WithoutTheStandInTest(HarnessCase):
    """The ceilings are for every page: also where WebKit has WebRTC of its own, or calls open in
    a window of their own (no answering handler), or no handler at all."""

    def test_every_decoder_and_encoder_still_has_its_ceiling(self):
        for mode in ("native-rtc", "no-calls", "no-handlers"):
            found = self.scenario("bounded", mode)
            self.assertEqual(found["kept"]["outputs"], 64, mode)
            self.assertEqual(found["kept"]["errors"], ["QuotaExceededError"], mode)
            self.assertEqual(found["flood"]["errors"], ["QuotaExceededError"], mode)
            self.assertEqual(found["flood"]["maxQueue"], 32, mode)
            self.assertEqual(found["rewrap"], 64, mode)

    def test_no_stand_in(self):
        for mode in ("native-rtc", "no-calls", "no-handlers"):
            found = self.scenario("bounded", mode)
            self.assertEqual(found["natterRtc"], "undefined", mode)  # bridge.js sees no calls of Natter's
            self.assertEqual(found["added"], [], mode)
            self.assertFalse(found["sinkId"], mode)  # the call's sound is the stand-in's
            self.assertFalse(found["cameraWrapped"], mode)
        self.assertEqual(self.scenario("bounded", "native-rtc")["rtc"], "native")

    def test_counts_go_up_where_there_is_a_handler(self):
        self.assertGreater(self.scenario("bounded", "native-rtc")["codecMessages"], 0)
        self.assertGreater(self.scenario("bounded", "no-calls")["codecMessages"], 0)
        self.assertEqual(self.scenario("bounded", "no-handlers")["codecMessages"], 0)


class LooksNativeTest(HarnessCase):
    def test_tostring_is_the_browsers(self):
        found = self.scenario("looks")
        for key, name in (("decoder", "VideoDecoder"), ("encoder", "VideoEncoder"), ("worker", "Worker"),
                          ("decode", "decode"), ("state", "state"), ("close", "close"), ("clone", "clone"),
                          ("frame", "VideoFrame")):
            self.assertEqual(found[key], f"function {name}() {{\n    [native code]\n}}")
        self.assertEqual(found["names"], ["VideoDecoder", "VideoEncoder", "Worker", "close", "VideoFrame"])
        self.assertEqual(found["workerLength"], 1)

    def test_video_frame_is_the_browsers_own_underneath(self):
        # Wrapped, not subclassed: frames WebKit makes pass instanceof, and the rest is WebKit's.
        found = self.scenario("looks")
        self.assertEqual(found["frameChecks"], {"instanceof": True, "constructor": True, "prototype": True,
                                                "static": True, "withoutNew": "TypeError", "length": True})

    def test_webkits_own_classes_underneath_and_no_new_globals(self):
        found = self.scenario("looks")
        self.assertTrue(found["inheritsSupport"])  # isConfigSupported is WebKit's
        self.assertTrue(found["subclasses"])
        self.assertTrue(found["closeReplaced"])
        self.assertEqual(found["enumerable"], [])  # VideoFrame too
        self.assertEqual(found["added"], ["RTCCertificate", "RTCDataChannel", "RTCIceCandidate", "RTCPeerConnection",
                                          "RTCSessionDescription", "__natterRtc"])
        self.assertEqual(found["ownProps"], [])  # nothing of Natter's on a decoder

    def test_no_storage_and_videoencoder_stays(self):
        text = RTC_JS.read_text()
        for banned in ("localStorage", "sessionStorage", "indexedDB", "caches.", "isConfigSupported"):
            self.assertNotIn(banned, text)
        self.assertIn('install("VideoEncoder", VideoEncoder)', text)


# ---- rtc.py: the counts as log lines ------------------------------------------------------------

DECODER = {"op": "webcodecs", "kind": "dec", "id": 3, "codec": "avc1.640c2a", "desc": False, "lat": False,
           "rewrite": False, "w": 1366, "h": 766, "out": 0, "open": 0, "held": 0, "queue": 0, "drops": 0,
           "err": "EncodingError"}
ENCODER = {"op": "webcodecs", "kind": "enc", "id": 1, "codec": "avc1.42001f", "mode": "realtime", "w": 1280,
           "h": 720, "out": 30, "queue": 2, "drops": 4}


class CodecLineTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(rtc.webcodecs_line(DECODER),
                         ("dec#3", "dec#3 avc1.640c2a desc=no lat=no rewrite=off 1366x766 out=0 open=0 held=0 queue=0 "
                                   "drops=0 err=EncodingError"))
        self.assertEqual(rtc.webcodecs_line(ENCODER),
                         ("enc#1", "enc#1 avc1.42001f mode=realtime 1280x720 out=30 queue=2 drops=4"))
        self.assertEqual(rtc.webcodecs_line({"op": "webcodecs", "kind": "page", "workers": 5, "annexb": 1}),
                         ("page", "workers=5 annexb-without-latency=1 open=0"))
        self.assertEqual(rtc.webcodecs_line({"op": "webcodecs", "kind": "page", "workers": 0, "annexb": 0,
                                             "open": 128}), ("page", "workers=0 annexb-without-latency=0 open=128"))
        self.assertEqual(rtc.webcodecs_line({"op": "webcodecs", "kind": "page", "workers": 40, "alive": 6, "annexb": 0,
                                             "open": 0}),
                         ("page", "workers=6 alive (40 created) annexb-without-latency=0 open=0"))
        for alive in (41, -1, "6", True, None):  # never more alive than started
            self.assertIsNone(rtc.webcodecs_line({"op": "webcodecs", "kind": "page", "workers": 40, "alive": alive,
                                                  "annexb": 0}), alive)
        fresh = {"op": "webcodecs", "kind": "dec", "id": 1, "codec": "", "w": 0, "h": 0}
        self.assertEqual(rtc.webcodecs_line(fresh)[1],
                         "dec#1 ? desc=no lat=no rewrite=off size=? out=0 open=0 held=0 queue=0 drops=0")

    def test_unknown_keys_drop_the_message(self):
        for extra in ({"url": "blob:x"}, {"name": "x"}, {"text": "hello"}, {"token": "x"}, {"workers": 1}):
            self.assertIsNone(rtc.webcodecs_line({**DECODER, **extra}), extra)
        self.assertIsNone(rtc.webcodecs_line({**ENCODER, "open": 1}))  # a decoder's field on an encoder
        self.assertIsNone(rtc.webcodecs_line({"op": "webcodecs", "kind": "page", "workers": 1, "id": 2}))

    def test_long_or_odd_strings_drop_the_message(self):
        for bad in ({"codec": "a" * 41}, {"codec": "avc1 x"}, {"codec": "avc1.4d\n"}, {"err": "E" * 41},
                    {"err": "Encoding Error"}, {"err": "x‮"}, {"codec": ["avc1"]}, {"err": 3}):
            self.assertIsNone(rtc.webcodecs_line({**DECODER, **bad}), bad)
        self.assertIsNone(rtc.webcodecs_line({**ENCODER, "mode": "fast"}))
        self.assertIsNone(rtc.webcodecs_line({**DECODER, "kind": "worker"}))
        self.assertIsNone(rtc.webcodecs_line({**DECODER, "kind": ["dec"]}))
        self.assertIsNone(rtc.webcodecs_line({**DECODER, "op": "send"}))
        self.assertIsNone(rtc.webcodecs_line("dec#1"))

    def test_numbers_and_flags_of_the_wrong_kind_drop_the_message(self):
        for bad in ({"out": -1}, {"out": 1.5}, {"out": True}, {"out": "3"}, {"out": 10 ** 10}, {"id": 0},
                    {"w": 70000}, {"desc": 1}, {"lat": "yes"}, {"rewrite": None}):
            self.assertIsNone(rtc.webcodecs_line({**DECODER, **bad}), bad)
        message = dict(DECODER)
        del message["id"]
        self.assertIsNone(rtc.webcodecs_line(message))


class CodecLogTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.lines = []
        self.timers = []
        self.log = rtc.CodecLog(clock=lambda: self.now, later=lambda ms, done: self.timers.append((ms, done)) or 1,
                                write=self.lines.append)

    def test_one_line_a_second_for_each_codec(self):
        self.log.take(DECODER)
        self.log.take(ENCODER)
        self.now += 0.3
        self.log.take({**DECODER, "out": 5})
        self.log.take({**DECODER, "out": 9})
        self.assertEqual(len(self.lines), 2)
        self.assertTrue(self.lines[0].startswith("natter: webcodecs: dec#3 avc1.640c2a"))
        self.assertEqual(len(self.timers), 1)
        self.now += 0.8
        ms, done = self.timers.pop()
        self.assertEqual(ms, 1000)
        self.assertFalse(done())
        self.assertEqual(len(self.lines), 3)
        self.assertIn(" out=9 ", self.lines[2])  # only the latest of those that waited
        self.assertEqual(self.timers, [])

    def test_not_yet_due_waits_again(self):
        self.log.take(DECODER)
        self.log.take({**DECODER, "out": 1})
        self.now += 0.5
        _ms, done = self.timers.pop()
        done()
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(len(self.timers), 1)

    def test_what_is_dropped_is_never_logged(self):
        self.log.take({**DECODER, "chat": "secret-name"})
        self.log.take({**DECODER, "codec": "secret name.mp4"})
        self.assertEqual(self.lines, [])

    def test_the_page_messages_go_to_the_log(self):
        taken = []
        host = types.SimpleNamespace(_codec_log=types.SimpleNamespace(take=taken.append))
        rtc.Host.post(host, dict(DECODER))
        self.assertEqual(taken, [DECODER])

    def test_without_calls_in_the_page_too(self):
        # Calls in a window of their own: rtc.js still bounds the codecs, and its counts still
        # reach the log; nothing else it could send does anything.
        from natter.account import Account

        taken, posted = [], []
        page = types.SimpleNamespace(rtc=None, _codec_log=types.SimpleNamespace(take=taken.append))
        for message in (dict(DECODER), {"op": "send", "ch": 1, "text": "x"}):
            page._rtc_message = lambda value, message=message: dict(message)
            Account._on_rtc_message(page, None, None)
        self.assertEqual(taken, [DECODER])
        page.rtc = types.SimpleNamespace(post=posted.append)
        Account._on_rtc_message(page, None, None)
        self.assertEqual(posted, [{"op": "send", "ch": 1, "text": "x"}])

    def test_rtc_js_goes_in_every_page(self):
        import inspect

        from natter.account import Account

        source = inspect.getsource(Account.__init__)
        injected, gated = source.index("RTC.replace(\"__NATTER_TOKEN__\""), source.index("if app.calls_in_page:")
        self.assertLess(injected, gated)
        self.assertLess(source.index('register_script_message_handler("natterRtc"'), gated)
        self.assertGreater(source.index('register_script_message_handler_with_reply("natterRtcCall"'), gated)


HARNESS = r"""
// rtc.js's video codec code in gjs, against fake codecs that behave as WebKitGTK's do on the
// points that matter (WebCodecsVideoDecoder.cpp, VideoDecoderGStreamer.cpp), on a virtual clock.
// Prints one "RESULT {json}" line per scenario; the Python test reads them.
const { GLib } = imports.gi;
const System = imports.system;
const TOKEN = "harness-token";
const MODE = ARGV[2] || "";  // "native-rtc", "no-calls", "no-handlers": see results() in the test
const RESULTS = {};
const problems = [];

// ---- A virtual clock: rtc.js's timers and performance.now() --------------------------------
let now = 0;
let lastTimer = 0;
const timers = new Map();
globalThis.setTimeout = (fn, ms = 0, ...args) => {
  const id = ++lastTimer;
  timers.set(id, { at: now + Math.max(0, Number(ms) || 0), fn: () => fn(...args) });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
globalThis.performance = { now: () => now };
globalThis.queueMicrotask = (fn) => Promise.resolve().then(fn).catch((e) => problems.push("microtask: " + e));

async function settle() {
  for (let i = 0; i < 40; i++) await null;
}

async function advance(ms) {
  const end = now + ms;
  for (;;) {
    await settle();
    let next = null;
    for (const [id, timer] of timers) if (timer.at <= end && (!next || timer.at < next[1].at)) next = [id, timer];
    if (!next) break;
    timers.delete(next[0]);
    now = Math.max(now, next[1].at);
    try {
      next[1].fn();
    } catch (e) {
      problems.push("timer: " + e + "\n" + e.stack);
    }
  }
  now = end;
  await settle();
}

const realWait = (ms) => new Promise((resolve) => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => { resolve(); return false; }));

// ---- The page around rtc.js -------------------------------------------------------------------
class DOMException extends Error {
  constructor(message = "", name = "Error") { super(message); this.name = name; }
}
class Event { constructor(type) { this.type = type; } }
class MessageEvent extends Event { constructor(type, init = {}) { super(type); this.data = init.data; } }
class EventTarget {
  addEventListener(type, fn) { ((this.__l ??= {})[type] ??= []).push(fn); }
  removeEventListener(type, fn) { const list = (this.__l ?? {})[type] || []; const i = list.indexOf(fn); if (i >= 0) list.splice(i, 1); }
  dispatchEvent(event) { for (const fn of [...((this.__l ?? {})[event.type] || [])]) fn.call(this, event); return true; }
}
Object.assign(globalThis, { DOMException, Event, MessageEvent, EventTarget, navigator: {},
                            location: { origin: "https://web.whatsapp.com" } });
if (globalThis.window !== globalThis) throw new Error("gjs's window isn't its global object");

const posted = [];
let pullWaiting = null;
globalThis.webkit = { messageHandlers: {
  natterRtc: { postMessage(message) { posted.push({ at: now, message: JSON.parse(JSON.stringify(message)) }); } },
  natterRtcCall: { postMessage(message) {
    if (message.token !== TOKEN) return Promise.reject(new Error("no token"));
    if (message.op === "pull") return new Promise((resolve) => { pullWaiting = resolve; });
    return Promise.resolve({});
  } },
} };
if (MODE === "no-calls") delete globalThis.webkit.messageHandlers.natterRtcCall;
if (MODE === "no-handlers") globalThis.webkit = { messageHandlers: {} };
if (MODE === "native-rtc") globalThis.RTCPeerConnection = function RTCPeerConnection() {};
class FakeAudioContext {}
globalThis.AudioContext = FakeAudioContext;

// ---- Fake WebCodecs, as WebKitGTK's -------------------------------------------------------------
const stats = { live: 0, maxLive: 0 };
let DECODE_MS = 1.5, ENCODE_MS = 5, DISPATCH_ALL = false;

class Picture {  // one decoded picture: a 1.5 MB buffer from openh264dec's pool
  constructor() { this.refs = 0; stats.live++; stats.maxLive = Math.max(stats.maxLive, stats.live); }
  unref() { if (--this.refs === 0) stats.live--; }
}
const pictureOfCollected = new FinalizationRegistry((picture) => picture.unref());

class VideoFrame {
  constructor(source, init = {}) {
    let picture = source;
    if (source instanceof VideoFrame) {  // a frame from a frame: the same picture, another timestamp if asked
      if (source._closed) throw new DOMException("VideoFrame is closed", "InvalidStateError");
      picture = source._picture;
      init = { timestamp: source.timestamp, codedWidth: source.codedWidth, codedHeight: source.codedHeight, ...init };
    }
    if (!(picture instanceof Picture)) throw new TypeError("the harness only makes frames in its decoder");
    this._picture = picture;
    picture.refs++;
    this._closed = false;
    this.timestamp = init.timestamp;
    this.codedWidth = init.codedWidth;
    this.codedHeight = init.codedHeight;
    pictureOfCollected.register(this, picture, this);
  }
  close() {
    if (this._closed) return;
    this._closed = true;
    pictureOfCollected.unregister(this);
    this._picture.unref();
  }
  clone() {
    if (this._closed) throw new DOMException("VideoFrame is closed", "InvalidStateError");
    return new VideoFrame(this._picture, this);
  }
  static get harnessStatic() { return 42; }
}

class EncodedVideoChunk {
  constructor(init) {
    this.type = init.type;
    this.timestamp = init.timestamp;
    this.duration = init.duration ?? null;
    const data = init.data instanceof Uint8Array ? init.data : new Uint8Array(init.data);
    this._data = data.slice();
    this.byteLength = data.byteLength;
  }
  copyTo(dest) { dest.set(this._data); }
}

function lengthPrefixedTypes(bytes) {
  const types = [];
  let at = 0;
  while (at + 4 <= bytes.length) {
    const n = (bytes[at] << 24 | bytes[at + 1] << 16 | bytes[at + 2] << 8 | bytes[at + 3]) >>> 0;
    if (!n || at + 4 + n > bytes.length) return null;
    types.push(bytes[at + 4] & 31);
    at += 4 + n;
  }
  return at === bytes.length ? types : null;
}

class FakeVideoDecoder {
  constructor(init) {
    if (!init || typeof init.output !== "function" || typeof init.error !== "function") throw new TypeError("VideoDecoderInit needs output and error");
    Object.assign(this, { _init: init, _state: "unconfigured", _queue: [], _queued: 0, _busy: 0, _keyRequired: true,
                          _config: null, _flushes: [], _generation: 0, nativeLog: [] });
  }
  static isConfigSupported(config) { return Promise.resolve({ supported: /^avc1\./.test(config.codec), config }); }
  get state() { return this._state; }
  get decodeQueueSize() { return this._queued; }
  configure(config) {
    if (!config || typeof config.codec !== "string" || !config.codec.trim() || !!config.codedWidth !== !!config.codedHeight)
      throw new TypeError("Config is not valid");
    if (this._state === "closed") throw new DOMException("VideoDecoder is closed", "InvalidStateError");
    this._state = "configured";
    this._keyRequired = true;
    const description = config.description ? new Uint8Array(config.description) : null;
    this.nativeLog.push(["configure", config.codec, description ? description.length : null]);
    this._enqueue({ configure: { codec: config.codec, description } });
  }
  decode(chunk) {
    if (this._state !== "configured") throw new DOMException("VideoDecoder is not configured", "InvalidStateError");
    if (this._keyRequired) {
      if (chunk.type !== "key") throw new DOMException("Key frame is required", "DataError");
      this._keyRequired = false;
    }
    this._queued++;
    this._enqueue({ chunk, counted: true });
  }
  flush() {
    if (this._state !== "configured") return Promise.reject(new DOMException("VideoDecoder is not configured", "InvalidStateError"));
    this._keyRequired = true;
    return new Promise((resolve, reject) => {
      const waiting = { resolve, reject };
      this._flushes.push(waiting);
      this._enqueue({ flush: waiting });
    });
  }
  reset() {
    if (this._state === "closed") throw new DOMException("VideoDecoder is closed", "InvalidStateError");
    this._abort(new DOMException("Reset called", "AbortError"));
    this._state = "unconfigured";
  }
  close() {
    if (this._state === "closed") throw new DOMException("VideoDecoder is closed", "InvalidStateError");
    this._abort(new DOMException("Close called", "AbortError"));
    this._state = "closed";
  }
  _abort(error) {
    this._generation++;
    this._queue = [];
    this._queued = 0;
    this._busy = 0;
    this._config = null;
    for (const waiting of this._flushes.splice(0)) waiting.reject(error);
  }
  _fail(error) {  // closeDecoder with an exception
    this._abort(error);
    this._state = "closed";
    this._init.error(error);
  }
  _enqueue(item) {
    this._queue.push(item);
    this._pump();
  }
  // One control message at a time, as WebKit's queue (or, DISPATCH_ALL, every decode handed to
  // the decoder's work queue at once, so decodeQueueSize drops to 0 while frames are still to come).
  _pump() {
    while (this._queue.length && (DISPATCH_ALL ? true : !this._busy)) {
      const item = this._queue.shift();
      if (item.counted) this._queued--;
      const generation = this._generation;
      this._busy++;
      setTimeout(() => {
        if (generation !== this._generation) return;
        this._busy--;
        this._run(item);
        this._pump();
      }, item.chunk ? DECODE_MS * (DISPATCH_ALL ? this._busy : 1) : 0);
      if (!DISPATCH_ALL) break;
    }
  }
  _run(item) {
    if (item.configure) {
      if (!/^avc1\./.test(item.configure.codec)) this._fail(new DOMException("Codec is not supported", "NotSupportedError"));
      else this._config = item.configure;
      return;
    }
    if (item.flush) {
      setTimeout(() => {  // after the outputs already posted
        const i = this._flushes.indexOf(item.flush);
        if (i >= 0) { this._flushes.splice(i, 1); item.flush.resolve(); }
      }, 0);
      return;
    }
    const bytes = new Uint8Array(item.chunk.byteLength);
    item.chunk.copyTo(bytes);
    const types = lengthPrefixedTypes(bytes);
    this.nativeLog.push(["decode", item.chunk.type, types ? types.join(",") : "UNPARSEABLE", Array.from(bytes.subarray(0, 8))]);
    if (!types || !this._config || !this._config.description) {
      this._fail(new DOMException("Decode error", "EncodingError"));
      return;
    }
    if (!types.some((type) => type === 1 || type === 5)) return;  // no picture in it
    const picture = new Picture();
    const generation = this._generation;
    setTimeout(() => {  // the output is a task of its own
      if (generation !== this._generation) { picture.refs++; picture.unref(); return; }
      const frame = new VideoFrame(picture, { timestamp: item.chunk.timestamp, codedWidth: 1280, codedHeight: 720 });
      this._init.output(frame);
    }, 0);
  }
}

class FakeVideoEncoder {
  constructor(init) {
    if (!init || typeof init.output !== "function" || typeof init.error !== "function") throw new TypeError("VideoEncoderInit needs output and error");
    Object.assign(this, { _init: init, _state: "unconfigured", _queue: [], _busy: false, _flushes: [], keyEncodes: 0, maxQueue: 0, encoded: 0 });
  }
  static isConfigSupported(config) { return Promise.resolve({ supported: true, config }); }
  get state() { return this._state; }
  get encodeQueueSize() { return this._queue.length; }
  configure(config) {
    if (!config || !config.codec || !config.width || !config.height) throw new TypeError("Config is not valid");
    if (this._state === "closed") throw new DOMException("VideoEncoder is closed", "InvalidStateError");
    this._state = "configured";
  }
  encode(frame, options) {
    if (this._state !== "configured") throw new DOMException("VideoEncoder is not configured", "InvalidStateError");
    if (!(frame instanceof VideoFrame) || frame._closed) throw new TypeError("Frame is closed");
    const key = Boolean(options && options.keyFrame);
    if (key) this.keyEncodes++;
    this._queue.push({ copy: new VideoFrame(frame._picture, frame), key });  // WebKit's own copy, not the page's clone()
    this.maxQueue = Math.max(this.maxQueue, this._queue.length);
    this._pump();
  }
  _pump() {
    if (this._busy || !this._queue.length) return;
    this._busy = true;
    setTimeout(() => {
      this._busy = false;
      if (this._state !== "configured") return;
      const item = this._queue.shift();
      item.copy.close();
      this.encoded++;
      this._init.output({ type: item.key ? "key" : "delta", byteLength: 100 }, { decoderConfig: { codec: "avc1.42001f" } });
      if (!this._queue.length) for (const waiting of this._flushes.splice(0)) waiting.resolve();
      this._pump();
    }, ENCODE_MS);
  }
  flush() {
    if (this._state !== "configured") return Promise.reject(new DOMException("VideoEncoder is not configured", "InvalidStateError"));
    if (!this._queue.length) return Promise.resolve();
    return new Promise((resolve, reject) => this._flushes.push({ resolve, reject }));
  }
  reset() {
    if (this._state === "closed") throw new DOMException("VideoEncoder is closed", "InvalidStateError");
    this._drop(new DOMException("Reset called", "AbortError"));
    this._state = "unconfigured";
  }
  close() {
    if (this._state === "closed") throw new DOMException("VideoEncoder is closed", "InvalidStateError");
    this._drop(new DOMException("Close called", "AbortError"));
    this._state = "closed";
  }
  _drop(error) {
    for (const item of this._queue.splice(0)) item.copy.close();
    for (const waiting of this._flushes.splice(0)) waiting.reject(error);
  }
}

class FakeWorker {
  constructor(scriptURL) {
    if (!arguments.length) throw new TypeError("Not enough arguments");
    this.url = String(scriptURL);
    this.terminated = 0;
  }
  terminate() {
    this.terminated++;
  }
}

Object.assign(globalThis, { VideoFrame, EncodedVideoChunk, VideoDecoder: FakeVideoDecoder, VideoEncoder: FakeVideoEncoder,
                            Worker: FakeWorker });
globalThis.navigator.mediaDevices = { getUserMedia() {}, enumerateDevices() {}, addEventListener() {} };
globalThis.MediaStreamTrack = class MediaStreamTrack { applyConstraints() {} };
const nativeGetUserMedia = globalThis.navigator.mediaDevices.getUserMedia;
const nativeClose = VideoFrame.prototype.close;
const globalsBefore = new Set(Object.getOwnPropertyNames(globalThis));

// ---- rtc.js -------------------------------------------------------------------------------------
const source = new TextDecoder().decode(GLib.file_get_contents(ARGV[0])[1]).replace("__NATTER_TOKEN__", TOKEN);
(0, eval)(source);
const globalsAdded = Object.getOwnPropertyNames(globalThis).filter((name) => !globalsBefore.has(name));

// ---- H.264 test stream: synthetic NAL units, enough for the parsers on both sides --------------
const SPS = [0x67, 0x42, 0x00, 0x1f, 0xab, 0xcd, 0x40], PPS = [0x68, 0xce, 0x3c, 0x80];
const payload = (first, n, seed) => [first, ...Array.from({ length: n }, (_, i) => 16 + ((i * 7 + seed) % 200))];
const annexB = (units) => new Uint8Array(units.flatMap((unit) => [0, 0, 0, 1, ...unit]));
const avcc = (units) => new Uint8Array(units.flatMap((unit) => [unit.length >> 24, (unit.length >> 16) & 255, (unit.length >> 8) & 255, unit.length & 255, ...unit]));
const DESCRIPTION = new Uint8Array([1, SPS[1], SPS[2], SPS[3], 0xff, 0xe1, 0, SPS.length, ...SPS, 1, 0, PPS.length, ...PPS]);
const FRAME_US = 33333;

// What the page sees as VideoFrame: rtc.js's, where the harness's own class (WebKit's) is shadowed.
const PageFrame = () => globalThis.VideoFrame;

function annexBChunk(i, gop = 30) {
  const key = i % gop === 0;
  const units = key ? [[0x09, 0xf0], SPS, PPS, payload(0x65, 300, i)] : [[0x09, 0xf0], payload(0x41, 120, i)];
  return new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: i * FRAME_US, data: annexB(units) });
}

function avccChunk(i, gop = 30) {
  const key = i % gop === 0;
  return new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: i * FRAME_US,
                                 data: avcc([payload(key ? 0x65 : 0x41, key ? 300 : 120, i)]) });
}

const P13 = { codec: "avc1.42001f", optimizeForLatency: true };  // WhatsApp's call decoders

function decoder({ keep = false } = {}) {
  const seen = { outputs: 0, errors: [], kept: [], events: [] };
  const d = new VideoDecoder({
    output(frame) {
      seen.outputs++;
      seen.events.push("output");
      if (keep) seen.kept.push(frame);
      else frame.close();
    },
    error(e) {
      seen.errors.push(e.name + ": " + e.message);
      seen.events.push("error");
    },
  });
  return [d, seen];
}

// Feeds chunks `every` ms apart (0: all at once); returns what decode() threw, and how often.
async function feed(d, chunks, every) {
  const thrown = {};
  for (const chunk of chunks) {
    try {
      d.decode(chunk);
    } catch (e) {
      thrown[e.name] = (thrown[e.name] || 0) + 1;
    }
    if (every) await advance(every);
  }
  return thrown;
}

// ---- A call: an open data channel, as rtc.py would open one --------------------------------------
let callChannel = null;
async function startCall() {
  const pc = new RTCPeerConnection();
  callChannel = pc.createDataChannel("call");
  await settle();
  deliver([{ ch: callChannel._key, type: "open" }]);
  await settle();
}
async function endCall() {
  deliver([{ ch: callChannel._key, type: "close" }]);
  await settle();
}
function deliver(items) {
  const resolve = pullWaiting;
  pullWaiting = null;
  resolve(JSON.stringify(["", items]));
}

// ---- Scenarios ---------------------------------------------------------------------------------
const scenarios = {
  async rewrite() {
    const out = {};
    // 1a: a call's decoder, fed in real time outside a call: rewritten, every frame decoded.
    let [d, seen] = decoder();
    d.configure(P13);
    out.callStateBeforeKey = d.state;
    out.callThrown = await feed(d, Array.from({ length: 60 }, (_, i) => annexBChunk(i)), 33);
    await advance(100);
    out.call = { outputs: seen.outputs, errors: seen.errors, state: d.state, log: d.nativeLog.slice(0, 3),
                 parsed: d.nativeLog.filter((e) => e[0] === "decode").every((e) => e[2] === "5" || e[2] === "1") };
    // 1b: the same stream without optimizeForLatency, at file speed, frames kept: WebKit's own.
    [d, seen] = decoder({ keep: true });
    d.configure({ codec: "avc1.42001f", codedWidth: 1280, codedHeight: 720 });
    const chunks = Array.from({ length: 30 }, (_, i) => annexBChunk(i));
    out.plainThrown = await feed(d, chunks, 0);
    await advance(200);
    const first = new Uint8Array(chunks[0].byteLength);
    chunks[0].copyTo(first);
    const decodes = d.nativeLog.filter((e) => e[0] === "decode");
    out.plain = { outputs: seen.outputs, errors: seen.errors, state: d.state, configures: d.nativeLog.filter((e) => e[0] === "configure"),
                  firstBytesUnchanged: decodes.length > 0 && decodes[0][3].join() === Array.from(first.subarray(0, 8)).join() };
    // optimizeForLatency false is not a call's decoder either; avc3 with it is, as avc1.
    [d, seen] = decoder();
    d.configure({ codec: "avc1.42001f", optimizeForLatency: false });
    d.decode(annexBChunk(0));
    await advance(50);
    out.latencyFalse = { errors: seen.errors, configures: d.nativeLog.filter((e) => e[0] === "configure") };
    [d, seen] = decoder();
    d.configure({ codec: "avc3.42001f", optimizeForLatency: true });
    d.decode(annexBChunk(0));
    await advance(50);
    out.avc3 = { outputs: seen.outputs, errors: seen.errors, configures: d.nativeLog.filter((e) => e[0] === "configure") };
    // A decoder with a description is WebKit's own from the start.
    [d, seen] = decoder();
    d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(d, Array.from({ length: 10 }, (_, i) => avccChunk(i)), 0);
    await advance(100);
    out.described = { outputs: seen.outputs, errors: seen.errors, configures: d.nativeLog.filter((e) => e[0] === "configure") };
    await advance(3000);
    const pages = posted.filter((p) => p.message.op === "webcodecs" && p.message.kind === "page");
    out.annexb = pages.length ? pages[pages.length - 1].message.annexb : null;
    return out;
  },

  async probe() {
    // WhatsApp's startup test: one key frame, then flush.
    const [d, seen] = decoder();
    d.configure(P13);
    d.decode(annexBChunk(0));
    const flushed = d.flush().then(() => seen.events.push("flushed"), (e) => seen.events.push("flush failed " + e.name));
    await advance(100);
    await flushed;
    return { outputs: seen.outputs, events: seen.events, errors: seen.errors, state: d.state };
  },

  async requireKeyframe() {
    // A lost key frame: reset() and configure() mid-stream, then deltas until the next key frame.
    const [d, seen] = decoder();
    d.configure(P13);
    const thrown = await feed(d, Array.from({ length: 10 }, (_, i) => annexBChunk(i)), 33);
    const before = seen.outputs;
    d.reset();
    d.configure(P13);
    Object.assign(thrown, await feed(d, Array.from({ length: 5 }, (_, i) => annexBChunk(11 + i)), 33));
    const afterDeltas = seen.outputs;
    Object.assign(thrown, await feed(d, Array.from({ length: 5 }, (_, i) => annexBChunk(30 + i)), 33));
    await advance(100);
    return { before, afterDeltas, after: seen.outputs, thrown, errors: seen.errors, state: d.state };
  },

  async pace() {
    const out = {};
    // At file speed outside a call: a file, not a call.
    let [d, seen] = decoder();
    d.configure(P13);
    out.fileThrown = await feed(d, Array.from({ length: 300 }, (_, i) => annexBChunk(i)), 2);
    await advance(100);
    out.file = { outputs: seen.outputs, errors: seen.errors, state: d.state };
    // In real time outside a call, and at file speed in one: never.
    [d, seen] = decoder();
    d.configure(P13);
    await feed(d, Array.from({ length: 120 }, (_, i) => annexBChunk(i)), 34);
    await advance(100);
    out.realTime = { outputs: seen.outputs, errors: seen.errors, state: d.state };
    await startCall();
    [d, seen] = decoder();
    d.configure(P13);
    await feed(d, Array.from({ length: 300 }, (_, i) => annexBChunk(i)), 2);
    await advance(100);
    out.inCall = { outputs: seen.outputs, errors: seen.errors, state: d.state };
    await endCall();
    return out;
  },

  async encoder() {
    const out = {};
    const frames = [];
    const pictureFrame = (i) => new VideoFrame(new Picture(), { timestamp: i * FRAME_US, codedWidth: 1366, codedHeight: 766 });
    async function run(name, { count, every, call = false, mode, backpressure, keyEvery = 0 }) {
      if (call) await startCall();
      const seen = { outputs: 0, errors: [], syncError: false, thrown: [] };
      let inEncode = false;
      const e = new VideoEncoder({ output() { seen.outputs++; }, error(err) { seen.errors.push(err.name); if (inEncode) seen.syncError = true; } });
      e.configure({ codec: "avc1.42001f", width: 1366, height: 766, ...(mode ? { latencyMode: mode } : {}) });
      const liveBefore = stats.live;
      let maxLive = 0;
      for (let i = 0; i < count; i++) {
        if (backpressure) while (e.encodeQueueSize > backpressure) await advance(1);
        const frame = pictureFrame(i);
        inEncode = true;
        try {
          e.encode(frame, keyEvery && i % keyEvery === 0 ? { keyFrame: true } : undefined);
        } catch (err) {
          seen.thrown.push(err.name);
        }
        inEncode = false;
        frame.close();
        maxLive = Math.max(maxLive, stats.live - liveBefore);
        if (every) await advance(every);
      }
      if (e.state === "configured") e.flush().catch(() => {});
      await advance(3000);
      out[name] = { outputs: seen.outputs, errors: seen.errors, syncError: seen.syncError, thrown: [...new Set(seen.thrown)],
                    state: e.state, maxQueue: e.maxQueue, keyEncodes: e.keyEncodes, maxLive };
      if (call) await endCall();
      frames.push(e);
    }
    await run("flood", { count: 200, every: 1 });
    await run("call", { count: 300, every: 1, call: true, keyEvery: 10 });
    await run("waits", { count: 300, every: 1, backpressure: 8 });
    await run("probe", { count: 1, every: 0 });
    await run("realtime", { count: 200, every: 1, mode: "realtime" });
    await advance(3000);
    out.drops = {};
    for (const { message } of posted) if (message.op === "webcodecs" && message.kind === "enc") out.drops[message.id] = message.drops;
    return out;
  },

  async held() {
    const out = {};
    for (const dispatchAll of [false, true]) {
      DISPATCH_ALL = dispatchAll;
      const r = (out[dispatchAll ? "dispatchAll" : "serial"] = {});
      const liveBase = stats.live;
      stats.maxLive = stats.live;
      const [d, seen] = decoder({ keep: true });
      d.configure({ codec: "avc1.42001f", description: DESCRIPTION, codedWidth: 1280, codedHeight: 720 });
      r.thrown = await feed(d, Array.from({ length: 300 }, (_, i) => avccChunk(i)), 0);
      r.queueAtOnce = d.decodeQueueSize;
      await advance(2000);
      r.first = { outputs: seen.outputs, queue: d.decodeQueueSize, maxLive: stats.maxLive - liveBase };
      // Ten let go: ten more decoded.
      for (const frame of seen.kept.splice(0, 10)) frame.close();
      await advance(500);
      r.afterTen = { outputs: seen.outputs, queue: d.decodeQueueSize };
      // Copies count: the originals let go while their copies are kept frees nothing.
      const copies = seen.kept.slice(0, 5).map((frame) => frame.clone());
      for (const frame of seen.kept.splice(0, 5)) frame.close();
      await advance(500);
      r.copiesHeld = { outputs: seen.outputs };
      for (const copy of copies) copy.close();
      await advance(500);
      r.copiesClosed = { outputs: seen.outputs };
      // Frames collected without close() count as let go.
      seen.kept.splice(0, 10);
      for (let i = 0; i < 4; i++) {
        System.gc();
        await realWait(30);
        await advance(1);
      }
      await advance(500);
      r.collected = { outputs: seen.outputs };
      // A flush waiting behind held chunks, and nothing let go for HELD_MS outside a call: the decoder fails.
      const flush = d.flush().then(() => "resolved", (e) => e.name);
      await advance(10500);
      r.failed = { errors: seen.errors, state: d.state, queue: d.decodeQueueSize, flush: await flush, maxLive: stats.maxLive - liveBase };
      for (const frame of seen.kept.splice(0)) frame.close();
      await advance(100);
    }
    // In a call the same decoder waits as long as it takes; after the call, it fails.
    DISPATCH_ALL = false;
    await startCall();
    const [d, seen] = decoder({ keep: true });
    d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(d, Array.from({ length: 100 }, (_, i) => avccChunk(i)), 0);
    await advance(20000);
    out.inCall = { outputs: seen.outputs, errors: [...seen.errors], state: d.state, queue: d.decodeQueueSize };
    await endCall();
    await advance(11000);
    out.afterCall = { errors: seen.errors, state: d.state };
    for (const frame of seen.kept.splice(0)) frame.close();
    // A held chunk is refused at once when WebKit would refuse it: a delta after a flush.
    const [d2] = decoder({ keep: true });
    d2.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(d2, Array.from({ length: 80 }, (_, i) => avccChunk(i)), 0);
    d2.flush().catch(() => {});
    out.deltaAfterFlush = Object.keys(await feed(d2, [avccChunk(81)], 0))[0] || null;
    d2.close();
    return out;
  },

  async rewrap() {
    const out = {};
    // A decoder with a description (WebKit's own, no pace check), fed a file at once; the page
    // gives each frame another timestamp, keeps that, and lets the decoder's frame go.
    let liveBase = stats.live;
    stats.maxLive = stats.live;
    let kept = [], errors = [], outputs = 0;
    let d = new VideoDecoder({
      output(frame) {
        outputs++;
        kept.push(new (PageFrame())(frame, { timestamp: frame.timestamp + 7 }));
        frame.close();
      },
      error(e) { errors.push(e.name + ": " + e.message); },
    });
    d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(d, Array.from({ length: 300 }, (_, i) => avccChunk(i)), 0);
    await advance(2000);
    out.described = { outputs, queue: d.decodeQueueSize, live: stats.live - liveBase };
    await advance(3000);
    const pages = posted.filter((p) => p.message.op === "webcodecs" && p.message.kind === "dec" && p.message.open);
    out.openLogged = pages.length ? pages[pages.length - 1].message.open : null;
    await advance(7000);
    out.describedFailed = { errors: [...errors], state: d.state, maxLive: stats.maxLive - liveBase };
    for (const frame of kept.splice(0)) frame.close();
    await advance(100);
    // A frame made from a made frame counts once; letting the last go makes room.
    liveBase = stats.live;
    outputs = 0;
    d = new VideoDecoder({
      output(frame) {
        outputs++;
        const once = new (PageFrame())(frame, { timestamp: 1 });
        kept.push(new (PageFrame())(once, { timestamp: 2 }));
        once.close();
        frame.close();
      },
      error() {},
    });
    d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(d, Array.from({ length: 100 }, (_, i) => avccChunk(i)), 0);
    await advance(1000);
    out.chain = { outputs };
    for (const frame of kept.splice(0, 10)) frame.close();
    await advance(500);
    out.chain.afterLetGo = outputs;
    d.close();
    for (const frame of kept.splice(0)) frame.close();
    await advance(100);
    // Frames the page makes from its own pictures are none of a decoder's.
    liveBase = stats.live;
    const own = Array.from({ length: 10 }, (_, i) => new (PageFrame())(new Picture(), { timestamp: i }));
    out.unrelated = { outputs: own.length, live: stats.live - liveBase };
    own.forEach((frame) => frame.close());
    // A call's decoder, reset and configured again between 30-frame GOPs, at file speed.
    liveBase = stats.live;
    stats.maxLive = stats.live;
    outputs = 0;
    d = new VideoDecoder({
      output(frame) { outputs++; kept.push(new (PageFrame())(frame, { timestamp: frame.timestamp })); frame.close(); },
      error() {},
    });
    for (let gop = 0; gop < 10; gop++) {
      d.reset();
      d.configure(P13);
      await feed(d, Array.from({ length: 30 }, (_, i) => annexBChunk(gop * 30 + i)), 2);
    }
    await advance(500);
    out.resetBetween = { outputs, maxLive: stats.maxLive - liveBase };
    d.close();
    for (const frame of kept.splice(0)) frame.close();
    await advance(100);
    return out;
  },

  async split() {
    const out = {};
    for (const [name, config, chunkOf] of [["described", { codec: "avc1.42001f", description: DESCRIPTION }, avccChunk],
                                          ["rewritten", P13, annexBChunk]]) {
      const liveBase = stats.live;
      stats.maxLive = stats.live;
      const decoders = [], errors = [], kept = [];
      let outputs = 0;
      for (let gop = 0; gop < 8; gop++) {
        const d = new VideoDecoder({ output(frame) { outputs++; kept.push(frame); },
                                     error(e) { errors.push(e.name); } });
        d.configure(config);
        await feed(d, Array.from({ length: 30 }, (_, i) => chunkOf(gop * 30 + i)), 0);
        decoders.push(d);
        await advance(300);
      }
      await advance(3000);
      if (name === "described") {
        const pages = posted.filter((p) => p.message.op === "webcodecs" && p.message.kind === "page");
        out.pageOpen = pages.length ? pages[pages.length - 1].message.open : null;
      }
      await advance(9000);
      out[name] = { outputs, errors, maxLive: stats.maxLive - liveBase, states: decoders.map((d) => d.state) };
      if (name === "described") {
        // One that waits gets the room another's frames leave.
        const [d, seen] = decoder({ keep: true });
        d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
        await feed(d, Array.from({ length: 10 }, (_, i) => avccChunk(i)), 0);
        await advance(200);
        for (const frame of kept.splice(0, 10)) frame.close();
        await advance(500);
        out.afterLetGo = { outputs: outputs + seen.outputs, errors: seen.errors };
        d.close();
        for (const frame of seen.kept.splice(0)) frame.close();
      }
      for (const d of decoders) if (d.state !== "closed") d.close();
      for (const frame of kept.splice(0)) frame.close();
      await advance(100);
    }
    return out;
  },

  async groupCall() {
    // 32 people's video in a call, each decoder with up to 3 frames out: never held back.
    await startCall();
    const decoders = [], errors = [];
    let outputs = 0, held = 0;
    for (let k = 0; k < 32; k++) {
      const out = [];
      const d = new VideoDecoder({
        output(frame) { outputs++; out.push(frame); while (out.length > 3) out.shift().close(); },
        error(e) { errors.push(e.name); },
      });
      d.configure(P13);
      decoders.push([d, out]);
    }
    const from = posted.length;
    for (let i = 0; i < 60; i++) {
      for (const [d] of decoders) d.decode(annexBChunk(i));
      await advance(33);
    }
    await advance(200);
    for (const { message } of posted.slice(from))
      if (message.op === "webcodecs" && message.held) held = Math.max(held, message.held);
    for (const [d, out] of decoders) {
      out.splice(0).forEach((frame) => frame.close());
      d.close();
    }
    await endCall();
    return { outputs, errors, held };
  },

  async bounded() {
    // MODE: no stand-in. The ceilings all the same.
    const out = {};
    const [d, seen] = decoder({ keep: true });
    d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(d, Array.from({ length: 200 }, (_, i) => avccChunk(i)), 0);
    await advance(12000);
    out.kept = { outputs: seen.outputs, errors: seen.errors.map((e) => e.split(":")[0]) };
    for (const frame of seen.kept.splice(0)) frame.close();
    const errors = [];
    const e = new VideoEncoder({ output() {}, error(err) { errors.push(err.name); } });
    e.configure({ codec: "avc1.42001f", width: 1366, height: 766 });
    for (let i = 0; i < 100 && e.state === "configured"; i++) {
      const frame = new VideoFrame(new Picture(), { timestamp: i * FRAME_US });
      e.encode(frame);
      frame.close();
      await advance(1);
    }
    await advance(1000);
    out.flood = { errors, maxQueue: e.maxQueue };
    const kept = [];
    const r = new VideoDecoder({
      output(frame) { kept.push(new (PageFrame())(frame, { timestamp: 0 })); frame.close(); },
      error() {},
    });
    r.configure({ codec: "avc1.42001f", description: DESCRIPTION });
    await feed(r, Array.from({ length: 100 }, (_, i) => avccChunk(i)), 0);
    await advance(1000);
    out.rewrap = kept.length;
    r.close();
    kept.splice(0).forEach((frame) => frame.close());
    await advance(3000);
    out.natterRtc = typeof globalThis.__natterRtc;
    out.added = globalsAdded.sort();
    out.rtc = globalThis.RTCPeerConnection ? "native" : "none";
    out.sinkId = "setSinkId" in FakeAudioContext.prototype;
    out.cameraWrapped = globalThis.navigator.mediaDevices.getUserMedia !== nativeGetUserMedia;
    out.codecMessages = posted.filter((p) => p.message.op === "webcodecs").length;
    return out;
  },

  async workers() {
    const kept = ["a.js", "b.js", "c.js"].map((url) => new Worker(url));
    let thrown = null;
    try {
      new Worker();
    } catch (e) {
      thrown = e.name;
    }
    await advance(3000);
    const last = () => {
      const pages = posted.filter((p) => p.message.op === "webcodecs" && p.message.kind === "page");
      return pages[pages.length - 1].message;
    };
    const started = last();
    kept[0].terminate();
    kept[0].terminate();  // once is enough
    await advance(3000);
    const terminated = last();
    // One let go without terminate(): ended once collected. One that was terminated, collected
    // too, isn't ended twice.
    kept.splice(0, 2);
    for (let i = 0; i < 4; i++) {
      System.gc();
      await realWait(30);
      await advance(1);
    }
    await advance(3000);
    const collected = last();
    const extra = new Worker("d.js");
    return { workers: started.workers, alive: started.alive, afterTerminate: terminated.alive,
             terminatedCreated: terminated.workers,
             afterCollect: collected.alive, collectedCreated: collected.workers, thrown,
             isNative: extra instanceof FakeWorker, terminateCalled: (extra.terminate(), extra.terminated),
             terminateLooksNative: String(Worker.prototype.terminate).includes("[native code]") };
  },

  async messages() {
    await advance(65000);  // every last count sent
    const sent = posted.map((p) => p.message);
    const codecMessages = posted.filter((p) => p.message.op === "webcodecs");
    const keys = {};
    for (const { message } of codecMessages) {
      const set = (keys[message.kind] ??= new Set());
      for (const key of Object.keys(message)) set.add(key);
    }
    let closest = Infinity;
    const last = {};
    for (const { at, message } of codecMessages) {
      const who = message.kind + "#" + (message.id ?? "");
      if (who in last) closest = Math.min(closest, at - last[who]);
      last[who] = at;
    }
    const plain = codecMessages.map((p) => p.message).filter((m) => m.kind === "dec" && !m.desc && !m.lat && m.err);
    return { count: codecMessages.length, allTokened: sent.every((m) => m.token === TOKEN),
             keys: Object.fromEntries(Object.entries(keys).map(([k, v]) => [k, [...v].sort()])),
             closestMs: closest, plain: plain.slice(0, 1), sample: codecMessages.slice(-3).map((p) => p.message) };
  },

  async looks() {
    const shown = (fn) => String(fn);
    return {
      decoder: shown(VideoDecoder), encoder: shown(VideoEncoder), worker: shown(Worker),
      decode: shown(VideoDecoder.prototype.decode), state: shown(Object.getOwnPropertyDescriptor(VideoDecoder.prototype, "state").get),
      close: shown(VideoFrame.prototype.close), clone: shown(VideoFrame.prototype.clone), frame: shown(PageFrame()),
      names: [VideoDecoder.name, VideoEncoder.name, Worker.name, VideoFrame.prototype.close.name, PageFrame().name],
      frameChecks: await (async () => {
        const [d, seen] = decoder({ keep: true });
        d.configure({ codec: "avc1.42001f", description: DESCRIPTION });
        d.decode(avccChunk(0));
        await advance(50);
        const frame = seen.kept[0];  // one WebKit made, not the page
        let withoutNew = null;
        try {
          PageFrame()(frame);
        } catch (e) {
          withoutNew = e.name;
        }
        const checks = { instanceof: frame instanceof PageFrame(), constructor: frame.constructor === PageFrame(),
                         prototype: PageFrame().prototype === VideoFrame.prototype,
                         static: PageFrame().harnessStatic === 42,
                         withoutNew, length: PageFrame().length === VideoFrame.length };
        frame.close();
        d.close();
        return checks;
      })(),
      workerLength: Worker.length,
      inheritsSupport: VideoDecoder.isConfigSupported === FakeVideoDecoder.isConfigSupported
        && VideoEncoder.isConfigSupported === FakeVideoEncoder.isConfigSupported,
      subclasses: Object.getPrototypeOf(VideoDecoder) === FakeVideoDecoder && Object.getPrototypeOf(VideoEncoder) === FakeVideoEncoder,
      closeReplaced: VideoFrame.prototype.close !== nativeClose,
      enumerable: ["VideoDecoder", "VideoEncoder", "Worker", "VideoFrame"]
        .filter((k) => Object.getOwnPropertyDescriptor(globalThis, k).enumerable),
      added: globalsAdded.sort(),
      ownProps: Object.getOwnPropertyNames((() => { const [d] = decoder(); return d; })()).filter((k) => !k.startsWith("_") && k !== "nativeLog"),
    };
  },
};

async function main() {
  for (const [name, scenario] of Object.entries(scenarios)) {
    const only = ARGV[1];
    if (only && only !== name) continue;
    try {
      RESULTS[name] = await scenario();
    } catch (e) {
      RESULTS[name] = { harnessError: String(e) + "\n" + e.stack };
    }
    print("RESULT " + JSON.stringify({ name, result: RESULTS[name] }));
  }
  print("PROBLEMS " + JSON.stringify(problems));
}

const loop = new GLib.MainLoop(null, false);
main().catch((e) => print("HARNESS " + e + "\n" + e.stack)).finally(() => loop.quit());
loop.run();
"""
