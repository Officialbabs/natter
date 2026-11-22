"""What the bridge says about sound in WhatsApp's page (bridge.js, "Sound that can't be heard"),
checked, and the log's line for it.

Voice messages may play through <audio> elements, WebKit's GStreamer player, perhaps through a
Web Audio context as well; or the page may decode sound itself (decodeAudioData, WebCodecs'
AudioDecoder) and play it through a context. Where they can't be heard, the log says how the
page tried: once a page, how WebKit answers for the formats voice messages come in (with WebKit's
and GStreamer's versions), which way sound played, decodeAudioData's answer, an Opus AudioDecoder
supported, configured or failing, a decoded sound started, a page's own worklet or script
processor connected (with its context's state a moment on), and a voice recorder fed by the
microphone again (a voice note resumed after a pause: whether Natter rested its context meanwhile,
and whether that context's clock moves); and
each failure, the element's error, play() refused, the context it plays through not running, or
a context Natter rested not resuming. Each once a page for what it
says. Only kinds, codes, states and error names from fixed lists: never a URL, a name or what
plays.

And how a <video> played (a status that plays as if short of data): for a spell of its playback,
the media seconds played in how many seconds, its waits for data and stalls, the frames it
presented and the gaps between them, the frames WebKit dropped, and how late the page's main
thread ran meanwhile; a page's first spell, then only rough ones, counted apart from the rest.
Only counts, durations and its size, each a whole number within VIDEO_MOST.

And how each voice note recorded (WhatsApp's recorder, a script processor fed by the microphone):
its chunks handed to the page, their size and the sample rates, the seconds they hold against the
seconds they took and those on the context's clock, the longest gap between two, those handed over
more than a chunk late (WebKit gives the page a later chunk's sound for those: that much is lost),
the sound WebKit's audio thread skipped (while an event ran on the buffer it was on), how late the
page's main thread was, the microphone muted, unmuted or ended, and the context suspended; a page's
first recording, then only rough ones, counted apart from the rest. A recording through Natter's
worklet recorder (bridge.js recorderNode) says so: its chunks handed over late are late but whole.

And which script processor the page got when it made one (a voice recorder, say): Natter's worklet
recorder, its module loaded from a blob: or a data: URL, or WebKit's own and why (its context made
while no spare context was ready, or with other options than a spare's, and not rested since; the
worklet's module still loading, failed to load into that context, given up in the page (refused by
its policy, or failing again and again), no AudioWorklet, too many contexts with it, a shape it
doesn't take, its node not made), or the worklet broken; and the module loaded into a context the
page resumed meanwhile (WebKit restarted its output); once a page for each.
"""

from __future__ import annotations

import re
from typing import Optional

from . import NAME

# The bridge's SUPPORT keys, in its order.
SUPPORT_KEYS = ("ogg-opus", "ogg", "webm-opus", "mp4-opus", "mp4-aac", "mpeg")
ANSWERS = ("", "maybe", "probably")  # canPlayType's
WHATS = ("support", "played", "error", "refused", "silent", "decode", "decoder", "started", "processor", "resumed",
         "wake", "stalled", "video", "recording", "recorder")
KINDS = ("audio", "video")
ROUTES = ("element", "webaudio")
STATES = ("", "running", "suspended", "interrupted", "closed")
SOURCES = ("none", "stream", "blob", "https", "data", "file", "other", "mediasource", "audio-other", "video-other")
# The bridge's MEDIA_TYPES: a blob's type is told only if it's one of these (the sender writes it).
MEDIA_TYPES = ("audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/aac", "audio/x-m4a",
               "audio/webm", "audio/wav", "audio/x-wav", "audio/amr", "audio/3gpp", "audio/flac", "video/mp4",
               "video/webm", "video/quicktime", "video/3gpp", "video/ogg")
PROCESSORS = {"worklet": "an AudioWorkletNode", "script": "a ScriptProcessorNode"}  # a page's own player
ERROR_NAME = re.compile(r"[A-Za-z]{0,40}")
ERRORS = {0: "unknown", 1: "aborted", 2: "network", 3: "couldn't decode", 4: "source not supported"}
DECODES = ("resolved", "rejected")  # decodeAudioData's answers
# A play() WebKit dropped (bridge.js watchStart): what started it again, and the log's words.
STALLS = {"play": "WebKit dropped the play(); play() again started it",
          "seek": "WebKit dropped the play(); it started once its place was nudged",
          "still": "neither play() again nor a nudge of its place started it, so something else holds it"}
# An Opus AudioDecoder's steps, and the log's words for each.
DECODER_STEPS = {"supported": "isConfigSupported() says yes", "unsupported": "isConfigSupported() says no",
                 "check-failed": "isConfigSupported() failed", "configured": "configured",
                 "configure-failed": "configure() threw", "error": "an error"}
# A spell of a <video>'s playback (bridge.js watchVideo): its numbers, each a whole number from 0 to
# VIDEO_MOST (milliseconds, counts, or pixels to tens). The frames' are None together where WebKit has
# no requestVideoFrameCallback, the dropped ones' where it has no getVideoPlaybackQuality.
VIDEO_NUMBERS = ("width", "height", "played", "lasted", "waits", "waited", "stalls", "late")
VIDEO_FRAMES = ("frames", "gaps", "longest")
VIDEO_DROPS = ("dropped", "total")
VIDEO_FIELDS = frozenset(("type", "what", "kind", "route", "state", "source", "loop", *VIDEO_NUMBERS, *VIDEO_FRAMES,
                          *VIDEO_DROPS))
VIDEO_MOST = 3600000  # the bridge's
GAP_MS = 100  # the bridge's VIDEO_GAP_MS: the stretches without a frame it counts as gaps are longer than this
LINES_PER_PAGE = 20  # the bridge sends at most 12 a page; this holds even if it didn't
VIDEO_LINES_PER_PAGE = 20  # of videos, counted apart: the bridge sends at most 16 a page
# A voice recording (bridge.js watchRecording): its numbers, each a whole number from 0 to VIDEO_MOST
# (milliseconds or counts); its chunks' size in frames, one of RECORDING_SIZES; the context's sample
# rate and the microphone's (None where WebKit doesn't say it), within RATES; and what ended it.
RECORDING_NUMBERS = ("chunks", "recorded", "clock", "lasted", "longest", "late", "behind", "lost", "skipped",
                     "muted", "unmuted", "ended", "suspended", "rested")
RECORDING_FIELDS = frozenset(("type", "what", "path", "size", "rate", "microphone", "end", *RECORDING_NUMBERS))
# Which script processor recorded (or the page got): WebKit's own, or Natter's worklet recorder.
PATHS = ("native", "worklet")
RECORDING_SIZES = tuple(2 ** power for power in range(7, 15))  # 128 to 16384 frames
RATES = range(1000, 768001)  # the bridge's RATE_LEAST to RATE_MOST
RECORDING_ENDS = {"stopped": "until the microphone stopped", "disconnected": "until the recorder was disconnected",
                  "closed": "until its context closed", "long": "for its first 10 minutes"}
RECORDING_LINES_PER_PAGE = 10  # of recordings, counted apart: the bridge sends at most 6 a page
MODULES = ("blob", "data")  # the kinds of URL the worklet recorder's module loads from
# Why the page got the script processor it got (bridge.js recorderNode), by path; an error's name
# only with those marked True.
RECORDER_WHYS = {
    "worklet": {"ready": False, "broke": False, "restarted": False},
    "native": {"fresh": False, "options": False, "loading": False, "unloaded": True, "refused": True, "none": False,
               "many": False, "closed": False, "shape": False, "failed": True},
}
WORKLET = f"{NAME}'s worklet recorder"
RECORDER_WORDS = {
    "ready": "its module loaded", "broke": "it then broke (an error in its processor), so it records nothing",
    "restarted": "loaded into a context the page resumed meanwhile, so WebKit likely stopped and started that "
                 "context's sound output",
    "fresh": f"its context was made while no spare context with {WORKLET}'s module was ready, and hasn't rested since",
    "options": f"its context was made with options a spare context with {WORKLET}'s module doesn't have (another "
               "sample rate, say), and hasn't rested since",
    "loading": f"{WORKLET}'s module was still loading in that context",
    "unloaded": f"{WORKLET}'s module failed to load into that context",
    "refused": f"{WORKLET}'s module won't load in this page from a blob: or a data: URL (each refused by the page's "
               "policy, or failed 3 times)",
    "none": "this WebKit has no AudioWorklet", "many": f"16 contexts already have {WORKLET}",
    "closed": f"its context closed before {WORKLET}'s module loaded",
    "shape": f"its buffer size or channels aren't ones {WORKLET} takes",
    "failed": f"{WORKLET} couldn't be made",
}


def parse(message: dict) -> Optional[dict]:
    """A "media" message from the bridge, checked: only what's in the lists above, or None."""
    what = message.get("what")
    if what not in WHATS:
        return None
    if what == "support":
        play, source = message.get("play"), message.get("source")
        if not (isinstance(play, dict) and isinstance(source, dict)):
            return None
        if not all(play.get(key) in ANSWERS and isinstance(source.get(key), bool) for key in SUPPORT_KEYS):
            return None
        return {"type": "media", "what": what, "play": {key: play[key] for key in SUPPORT_KEYS},
                "source": {key: source[key] for key in SUPPORT_KEYS}}
    if what == "resumed":
        state, rested, moving = message.get("state"), message.get("rested"), message.get("moving")
        if state not in STATES or not state or not isinstance(rested, bool) or not isinstance(moving, bool):
            return None
        return {"type": "media", "what": what, "state": state, "rested": rested, "moving": moving}
    if what == "wake":
        name = message.get("name")
        if not isinstance(name, str) or not ERROR_NAME.fullmatch(name):
            return None
        return {"type": "media", "what": what, "name": name}
    if what in ("decode", "decoder"):
        name = message.get("name")
        if not isinstance(name, str) or not ERROR_NAME.fullmatch(name):
            return None
        step_key = "result" if what == "decode" else "step"
        step = message.get(step_key)
        if step not in (DECODES if what == "decode" else DECODER_STEPS):
            return None
        if name and step in ("resolved", "supported", "unsupported", "configured"):
            return None  # an error's name only where something failed
        return {"type": "media", "what": what, step_key: step, "name": name}
    if what in ("started", "processor"):
        state = message.get("state")
        if state not in STATES or not state:
            return None
        if what == "started":
            return {"type": "media", "what": what, "state": state}
        kind = message.get("kind")
        if kind not in PROCESSORS:
            return None
        return {"type": "media", "what": what, "kind": kind, "state": state}
    if what == "recording":
        return _recording(message)
    if what == "recorder":
        return _recorder(message)
    kind, route, state, source = (message.get(key) for key in ("kind", "route", "state", "source"))
    if kind not in KINDS or route not in ROUTES or state not in STATES:
        return None
    if source not in SOURCES and source not in MEDIA_TYPES:
        return None
    if (route == "webaudio") != (state != ""):
        return None  # a context's state only for sound through a context
    checked = {"type": "media", "what": what, "kind": kind, "route": route, "state": state, "source": source}
    if what == "error":
        code = message.get("code")
        if type(code) is not int or code not in ERRORS:
            return None
        checked["code"] = code
    elif what == "refused":
        name = message.get("name")
        if not isinstance(name, str) or not ERROR_NAME.fullmatch(name):
            return None
        checked["name"] = name
    elif what == "stalled":
        then = message.get("then")
        if then not in STALLS:
            return None
        checked["then"] = then
    elif what == "video":
        video = _video(message)
        if kind != "video" or video is None:
            return None
        checked.update(video)
    return checked


def _whole(value) -> bool:
    return type(value) is int and 0 <= value <= VIDEO_MOST


def _video(message: dict) -> Optional[dict]:
    """A video spell's own fields, checked: all of them there and nothing else (but the token)."""
    if not VIDEO_FIELDS <= set(message) <= VIDEO_FIELDS | {"token"} or not isinstance(message["loop"], bool):
        return None
    if not all(_whole(message[key]) for key in VIDEO_NUMBERS):
        return None
    if message["width"] % 10 or message["height"] % 10:
        return None
    for group in (VIDEO_FRAMES, VIDEO_DROPS):
        if not (all(message[key] is None for key in group) or all(_whole(message[key]) for key in group)):
            return None
    # Each gap ends at a frame, or one at the spell's end.
    if message["gaps"] is not None and message["gaps"] > message["frames"] + 1:
        return None
    if message["dropped"] is not None and message["dropped"] > message["total"]:
        return None
    return {key: message[key] for key in ("loop", *VIDEO_NUMBERS, *VIDEO_FRAMES, *VIDEO_DROPS)}


def _recorder(message: dict) -> Optional[dict]:
    """Which script processor the page got and why: from the lists above, nothing else (but the token)."""
    fields = {"type", "what", "path", "why", "module", "name"}
    if not fields <= set(message) <= fields | {"token"}:
        return None
    path, why, module, name = (message[key] for key in ("path", "why", "module", "name"))
    if path not in RECORDER_WHYS or why not in RECORDER_WHYS[path]:
        return None
    if module != "" and not (path == "worklet" and module in MODULES):
        return None  # the module's kind only for the worklet
    if not isinstance(name, str) or not ERROR_NAME.fullmatch(name) or (name and not RECORDER_WHYS[path][why]):
        return None  # an error's name only where something failed
    return {"type": "media", "what": "recorder", "path": path, "why": why, "module": module, "name": name}


def _recording(message: dict) -> Optional[dict]:
    """A recording's fields, checked: all of them there and nothing else (but the token)."""
    if not RECORDING_FIELDS <= set(message) <= RECORDING_FIELDS | {"token"}:
        return None
    if message["path"] not in PATHS:
        return None
    if not all(_whole(message[key]) for key in RECORDING_NUMBERS) or message["end"] not in RECORDING_ENDS:
        return None
    size, rate, microphone = message["size"], message["rate"], message["microphone"]
    if type(size) is not int or size not in RECORDING_SIZES or type(rate) is not int or rate not in RATES:
        return None
    if microphone is not None and (type(microphone) is not int or microphone not in RATES):
        return None
    # Its chunks hold what they hold, to the millisecond; those late are some of them, each losing a
    # chunk at most; the idle audio's suspensions are some of the context's.
    chunk = size * 1000 / rate
    if abs(message["recorded"] - message["chunks"] * chunk) > 1 or message["behind"] > message["chunks"]:
        return None
    if message["lost"] > message["behind"] * chunk + 1 or message["rested"] > message["suspended"]:
        return None
    if message["path"] == "worklet" and message["lost"]:
        return None  # its late chunks come whole
    return {key: message[key] for key in ("type", "what", "path", "size", "rate", "microphone", *RECORDING_NUMBERS,
                                          "end")}


def _element(message: dict) -> str:
    """"an <audio> (audio/ogg), from the element itself" or "… through a Web Audio context, suspended"."""
    way = ("from the element itself" if message["route"] == "element"
           else f"through a Web Audio context, {message['state']}")
    return f"an <{message['kind']}> ({message['source']}), {way}"


def _times(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def _spell(message: dict) -> str:
    """A video spell's words: "an <video> (video/mp4) 720x1280 played 12.3 s in 14.1 s: waited 3 times
    (1.6 s), stalled twice; 350 frames, 4 gaps over 100 ms (longest 640 ms), dropped 12 of 360; the
    page's main thread up to 420 ms late"."""
    size = f" {message['width']}x{message['height']}" if message["width"] and message["height"] else ""
    way = "" if message["route"] == "element" else f", through a Web Audio context ({message['state']}),"
    words = (f"{'a looping' if message['loop'] else 'an'} <video> ({message['source']}){size}{way} played "
             f"{message['played'] / 1000:.1f} s in {message['lasted'] / 1000:.1f} s: ")
    waits, stalls = message["waits"], message["stalls"]
    if waits:
        words += f"waited {_times(waits)} ({message['waited'] / 1000:.1f} s), "
        words += f"stalled {_times(stalls)}" if stalls else "never stalled"
    else:
        words += f"never waited, stalled {_times(stalls)}" if stalls else "never waited or stalled"
    if message["frames"] is None:
        words += "; frames not counted"
    else:
        gaps = message["gaps"]
        words += (f"; {message['frames']} frames, {gaps or 'no'} gap{'' if gaps == 1 else 's'} over {GAP_MS} ms "
                  f"(longest {message['longest']} ms)")
    if message["dropped"] is not None:
        words += f", dropped {message['dropped']} of {message['total']}"
    return f"{words}; the page's main thread up to {message['late']} ms late"


def _recorded(message: dict) -> str:
    """A voice recording's words: "a voice recording 39.2 s in 40.1 s (40.0 s on its context's clock, of
    which the audio thread skipped 0.8 s): 459 chunks of 4096 at 48000 Hz (microphone 48000 Hz), 12 handed
    over more than a chunk late (1.0 s of its sound lost), longest gap 420 ms; the page's main thread up to
    380 ms late; the microphone muted once; its context never suspended; measured until the microphone
    stopped"."""
    chunks, behind = message["chunks"], message["behind"]
    worklet = message["path"] == "worklet"
    microphone = f"microphone {message['microphone']} Hz" if message["microphone"] else "the microphone's rate not said"
    if not behind:
        late = "none handed over late"
    elif worklet:
        late = f"{behind} handed over more than a chunk late, but whole (nothing lost)"
    else:
        late = f"{behind} handed over more than a chunk late ({message['lost'] / 1000:.1f} s of its sound lost)"
    through = f" (through {WORKLET})" if worklet else ""
    words = (f"a voice recording{through} {message['recorded'] / 1000:.1f} s in {message['lasted'] / 1000:.1f} s "
             f"({message['clock'] / 1000:.1f} s on its context's clock, of which the audio thread skipped "
             f"{message['skipped'] / 1000:.1f} s): {chunks} chunk{'' if chunks == 1 else 's'} "
             f"of {message['size']} at {message['rate']} Hz ({microphone}), {late}, longest gap "
             f"{message['longest']} ms; the page's main thread up to {message['late']} ms late; ")
    changes = [f"{word} {_times(message[key])}" for key, word in
               (("muted", "muted"), ("unmuted", "unmuted"), ("ended", "ended early")) if message[key]]
    words += f"the microphone {', '.join(changes)}" if changes else "the microphone never muted or ended early"
    suspended, rested = message["suspended"], message["rested"]
    if suspended:
        words += f"; its context suspended {_times(suspended)}"
        if rested:
            words += f" ({_times(rested)} by the idle audio)"
    else:
        words += "; its context never suspended"
    return f"{words}; measured {RECORDING_ENDS[message['end']]}"


def line(message: dict, versions: str = "") -> str:
    """The log's words for a checked message (after "natter: <account>: ")."""
    what = message["what"]
    if what == "support":
        plays = ", ".join(f"{key} {message['play'][key] or 'no'}" for key in SUPPORT_KEYS)
        takes = ", ".join(key for key in SUPPORT_KEYS if message["source"][key]) or "none of them"
        return f"media: {versions + '; ' if versions else ''}<audio> plays {plays}; MediaSource takes {takes}"
    if what == "played":
        return f"media: sound played: {_element(message)}"
    if what == "error":
        return f"media: {_element(message)} failed: MediaError {message['code']} ({ERRORS[message['code']]})"
    if what == "refused":
        return f"media: play() refused for {_element(message)}: {message['name'] or 'no error name'}"
    if what == "stalled":
        return f"media: {_element(message)} said it played while its time stood still; {STALLS[message['then']]}"
    if what == "video":
        return f"media: {_spell(message)}"
    if what == "recording":
        return f"media: {_recorded(message)}"
    if what == "recorder":
        if message["why"] == "restarted":
            return (f"media: {WORKLET}'s module {RECORDER_WORDS['restarted']}"
                    + (f" (from a {message['module']}: URL)" if message["module"] else ""))
        why = RECORDER_WORDS[message["why"]]
        if message["name"]:
            why += f": {message['name']}"
        if message["path"] == "worklet":
            return (f"media: a script processor (a voice recorder, say) made as {WORKLET}; {why}"
                    + (f" (from a {message['module']}: URL)" if message["module"] else ""))
        return f"media: a script processor (a voice recorder, say) made as WebKit's own; {why}"
    if what == "decode":
        if message["result"] == "resolved":
            return "media: decodeAudioData resolved"
        return f"media: decodeAudioData rejected: {message['name'] or 'no error name'}"
    if what == "decoder":
        words = DECODER_STEPS[message["step"]]
        if message["step"] in ("check-failed", "configure-failed", "error"):
            words += f": {message['name'] or 'no error name'}"
        return f"media: WebCodecs AudioDecoder (opus): {words}"
    if what == "processor":
        return (f"media: the page's own player ({PROCESSORS[message['kind']]}) connected; its Web Audio "
                f"context 1.5 s on: {message['state']}")
    if what == "resumed":
        meanwhile = "rested by the idle audio after 10 minutes quiet" if message["rested"] else "kept running"
        clock = "its clock moving" if message["moving"] else "its clock not moving, so nothing is recorded"
        return (f"media: a voice recorder fed by the microphone again (a voice note resumed after a pause, or the "
                f"next one in the same recorder); its Web Audio context ({meanwhile}) 2.5 s on: "
                f"{message['state']}, {clock}")
    if what == "wake":
        return (f"media: a Web Audio context the idle audio rested didn't resume, twice: "
                f"{message['name'] or 'no error name'}")
    if what == "started":
        return (f"media: a decoded sound started (AudioBufferSourceNode); its Web Audio context 1.5 s on: "
                f"{message['state']}")
    return (f"media: {_element(message)}, 1.5 s after it started playing: the context isn't running, so "
            "none of it can be heard")
