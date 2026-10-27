"""What the bridge says about sound in WhatsApp's page (bridge.js, "Sound that can't be heard"),
checked, and the log's line for it.

Voice messages may play through <audio> elements, WebKit's GStreamer player, perhaps through a
Web Audio context as well; or the page may decode sound itself (decodeAudioData, WebCodecs'
AudioDecoder) and play it through a context. Where they can't be heard, the log says how the
page tried: once a page, how WebKit answers for the formats voice messages come in (with WebKit's
and GStreamer's versions), which way sound played, decodeAudioData's answer, an Opus AudioDecoder
supported, configured or failing, a decoded sound started, and a page's own worklet or script
processor connected (with its context's state a moment on); and each failure, the element's
error, play() refused, or the context it plays through not running. Each once a page for what it
says. Only kinds, codes, states and error names from fixed lists: never a URL, a name or what
plays.
"""

from __future__ import annotations

import re
from typing import Optional

# The bridge's SUPPORT keys, in its order.
SUPPORT_KEYS = ("ogg-opus", "ogg", "webm-opus", "mp4-opus", "mp4-aac", "mpeg")
ANSWERS = ("", "maybe", "probably")  # canPlayType's
WHATS = ("support", "played", "error", "refused", "silent", "decode", "decoder", "started", "processor")
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
# An Opus AudioDecoder's steps, and the log's words for each.
DECODER_STEPS = {"supported": "isConfigSupported() says yes", "unsupported": "isConfigSupported() says no",
                 "check-failed": "isConfigSupported() failed", "configured": "configured",
                 "configure-failed": "configure() threw", "error": "an error"}
LINES_PER_PAGE = 20  # the bridge sends at most 12 a page; this holds even if it didn't


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
    return checked


def _element(message: dict) -> str:
    """"an <audio> (audio/ogg), from the element itself" or "… through a Web Audio context, suspended"."""
    way = ("from the element itself" if message["route"] == "element"
           else f"through a Web Audio context, {message['state']}")
    return f"an <{message['kind']}> ({message['source']}), {way}"


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
    if what == "started":
        return (f"media: a decoded sound started (AudioBufferSourceNode); its Web Audio context 1.5 s on: "
                f"{message['state']}")
    return (f"media: {_element(message)}, 1.5 s after it started playing: the context isn't running, so "
            "none of it can be heard")
