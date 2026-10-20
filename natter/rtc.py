"""WebRTC data channels for WhatsApp's calls, with GStreamer's webrtcbin.

WebKitGTK as distributions build it has no WebRTC, but WhatsApp's calls only need data channels:
its own call engine (WebAssembly, in the page) makes and encrypts the audio and video and sends
the packets over unordered data channels. rtc.js gives the page RTCPeerConnection and
RTCDataChannel; this module does the networking. One Host per page, one webrtcbin per
RTCPeerConnection, and everything for the page goes back in order, in batches.

GStreamer calls back from its own threads; those only queue events here. The page is told on
GLib's main loop, and what the page asks is done there too.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import itertools
import json
import re
import secrets
import ssl
import subprocess
import threading
import time
from typing import Callable, Optional

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstSdp", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("JavaScriptCore", "6.0")
    from gi.repository import GLib, Gst, GstSdp, GstWebRTC, JavaScriptCore
except (ImportError, ValueError):
    Gst = None

# What WebRTC states are called in the page, by webrtcbin's enum values.
GATHERING = {0: "new", 1: "gathering", 2: "complete"}
ICE = {0: "new", 1: "checking", 2: "connected", 3: "completed", 4: "failed", 5: "disconnected", 6: "closed"}
CONNECTION = {0: "new", 1: "connecting", 2: "connected", 3: "disconnected", 4: "failed", 5: "closed"}
SIGNALING = {0: "stable", 1: "closed", 2: "have-local-offer", 3: "have-remote-offer",
             4: "have-local-pranswer", 5: "have-remote-pranswer"}
ICE_FROM_CONNECTION = {"connecting": "checking", "connected": "connected", "disconnected": "disconnected",
                       "failed": "failed", "closed": "closed"}
SDP_TYPES = {"offer": 1, "pranswer": 2, "answer": 3, "rollback": 4}
MAX_MESSAGE = 262144  # what the page is told when the far side doesn't say (RFC 8841's default is 64 KB)
FLUSH_MS = 2  # batches go to the page at most this often (the first after a pause, at once)
# WhatsApp's own checks open these channels for a moment: they aren't calls.
NOT_CALLS = ("sctp-prewarm", "__transfer_probe__")
DEBUG = bool(GLib.getenv("NATTER_DEBUG")) if Gst is not None else False


def _log(text: str) -> None:
    """With NATTER_DEBUG: what the page's connections do (no addresses, keys or packets)."""
    if DEBUG:
        print(f"natter: rtc {text}", flush=True)


def available() -> bool:
    """Whether this computer has what calls need: GStreamer's WebRTC parts and their Python bindings."""
    if Gst is None:
        return False
    if not Gst.is_initialized():
        Gst.init(None)
    return all(Gst.ElementFactory.find(name) for name in ("webrtcbin", "sctpenc", "sctpdec", "nicesrc", "dtlsenc"))


# What webrtcbin accepts as ICE credentials in a description (RFC 8839's ice-chars). WhatsApp's
# relays log in with tokens that browsers take but webrtcbin's check doesn't, so those are set
# on the ICE agent directly, which has no such check (see _for_webrtcbin).
ICE_CHARS = re.compile(r"^[A-Za-z0-9+/]+$")
UFRAG = re.compile(r"^a=ice-ufrag:([^\r\n]*)", re.M)
PWD = re.compile(r"^a=ice-pwd:([^\r\n]*)", re.M)
CANDIDATE_LINE = re.compile(r"^a=(candidate:[^\r\n]+)\r?\n", re.M)
END_OF_CANDIDATES = re.compile(r"^a=end-of-candidates\r?\n?", re.M)

try:
    _gobject = ctypes.CDLL("libgobject-2.0.so.0")
    _gobject.g_object_unref.argtypes = [ctypes.c_void_p]
    _gstwebrtc = ctypes.CDLL("libgstwebrtc-1.0.so.0")
    _gstwebrtc.gst_webrtc_ice_set_remote_credentials.argtypes = [ctypes.c_void_p] * 2 + [ctypes.c_char_p] * 2
    _gstwebrtc.gst_webrtc_ice_set_remote_credentials.restype = ctypes.c_int
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
except (OSError, AttributeError):
    _gstwebrtc = None


def _object_property(pointer: Optional[int], name: str) -> Optional[int]:
    """An object property of a GObject, by pointer, with a reference of its own (or None).

    webrtcbin's ICE objects are only reached this way, never as Python objects: they're never
    sunk, so PyGObject would take webrtcbin's reference for its own and free them under it.
    """
    if not pointer:
        return None
    out = ctypes.c_void_p()
    _gobject.g_object_get(ctypes.c_void_p(pointer), name.encode(), ctypes.byref(out), None)
    return out.value


def _set_remote_credentials(webrtc, ufrag: str, pwd: str) -> bool:
    """The far side's ICE login, given to webrtcbin's ICE agent itself (main loop only)."""
    if _gstwebrtc is None:
        return False
    held = []
    try:
        element = ctypes.pythonapi.PyCapsule_GetPointer(webrtc.__gpointer__, None)
        agent = _object_property(element, "ice-agent")
        held.append(agent)
        pointer = element
        for name in ("sctp-transport", "transport", "transport", "stream"):  # SCTP, DTLS, ICE, stream
            pointer = _object_property(pointer, name)
            held.append(pointer)
        if not agent or not pointer:
            return False
        return bool(_gstwebrtc.gst_webrtc_ice_set_remote_credentials(agent, pointer, ufrag.encode(), pwd.encode()))
    finally:
        for pointer in held:
            if pointer:
                _gobject.g_object_unref(pointer)


def _ice_ok(ufrag: str, pwd: str) -> bool:
    return bool(ICE_CHARS.match(ufrag) and ICE_CHARS.match(pwd) and 4 <= len(ufrag) <= 256 and 22 <= len(pwd) <= 256)


def _for_webrtcbin(sdp: str) -> tuple[str, Optional[tuple[str, str]], list[tuple[int, str]]]:
    """A remote description as webrtcbin will take it.

    When its ICE credentials are ones webrtcbin's check refuses, they're swapped for stand-ins and
    its candidates taken out (webrtcbin would start checks with the stand-ins): then also the real
    (ufrag, pwd) and the candidates as (media index, candidate), to set once it's taken.
    """
    ufrag, pwd = UFRAG.search(sdp), PWD.search(sdp)
    if not ufrag or not pwd or _ice_ok(ufrag.group(1), pwd.group(1)):
        return sdp, None, []
    held = []
    sections = re.split(r"(?=^m=)", sdp, flags=re.M)
    for index, section in enumerate(sections[1:]):
        held += [(index, "a=" + line) for line in CANDIDATE_LINE.findall(section)]
    sdp = CANDIDATE_LINE.sub("", END_OF_CANDIDATES.sub("", sdp))
    sdp = UFRAG.sub("a=ice-ufrag:natter" + secrets.token_hex(5), sdp)
    sdp = PWD.sub("a=ice-pwd:" + secrets.token_hex(12), sdp)
    return sdp, (ufrag.group(1), pwd.group(1)), held


def _structure(text: str):
    """A Gst.Structure from its text form (older bindings return it with the unparsed rest)."""
    parsed = Gst.Structure.new_from_string(text)
    return parsed[0] if isinstance(parsed, tuple) else parsed


def _camel(name: str) -> str:
    head, *rest = name.split("-")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _plain(value):
    """A GStreamer stats value as JSON can carry it."""
    if hasattr(value, "value_nick"):
        return value.value_nick
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if hasattr(value, "value_nick"):
        return value.value_nick
    return str(value)


def _parse_sdp(text: str):
    result, message = GstSdp.SDPMessage.new()
    if result != GstSdp.SDPResult.OK or GstSdp.sdp_message_parse_buffer(text.encode(), message) != GstSdp.SDPResult.OK:
        raise ValueError("The session description couldn't be read")
    return message


def _max_message_size(sdp: str) -> int:
    match = re.search(r"^a=max-message-size:(\d+)", sdp, re.M)
    return int(match.group(1)) if match else MAX_MESSAGE


def _server_url(server: dict, url: str) -> Optional[tuple[str, str]]:
    """("stun"|"turn", webrtcbin's form of the server) for one of the page's ICE server URLs."""
    match = re.match(r"^(stuns?|turns?):([^?]+)(?:\?transport=(udp|tcp))?$", url.strip(), re.I)
    if not match:
        return None
    scheme, address, transport = match.group(1).lower(), match.group(2), match.group(3)
    if ":" not in address.rsplit("]", 1)[-1]:
        address += ":5349" if scheme in ("stuns", "turns") else ":3478"
    if scheme.startswith("stun"):
        return "stun", f"stun://{address}"
    user = GLib.Uri.escape_string(server.get("username") or "", None, False)
    password = GLib.Uri.escape_string(server.get("credential") or "", None, False)
    query = f"?transport={transport}" if transport else ""
    return "turn", f"{scheme}://{user}:{password}@{address}{query}"


CERTIFICATE_DAYS = 30
FINGERPRINT = re.compile(r"^a=fingerprint:\S+ [^\r\n]+", re.M)


def make_certificate() -> tuple[str, str]:
    """A new ECDSA P-256 certificate for DTLS: (PEM of the certificate and its key, its SHA-256 fingerprint).

    With Python's cryptography where it's installed, else the openssl command.
    """
    try:
        pem = _certificate_with_cryptography()
    except ImportError:
        result = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                                 "-nodes", "-keyout", "-", "-out", "-", "-days", str(CERTIFICATE_DAYS), "-subj", "/CN=WebRTC"],
                                capture_output=True, text=True, timeout=10, check=True)
        pem = result.stdout
    block = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S)
    if not block:
        raise ValueError("No certificate was made")
    return pem, fingerprint(block.group(0))


def _certificate_with_cryptography() -> str:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WebRTC")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=CERTIFICATE_DAYS))
                   .sign(key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM).decode()
            + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode())


def fingerprint(certificate_pem: str) -> str:
    """The certificate's SHA-256 fingerprint as SDP writes it (AB:CD:…)."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate_pem)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def with_fingerprint(sdp: str, value: Optional[str]) -> str:
    """`sdp` with its a=fingerprint lines saying `value` (webrtcbin's own is wrong for a certificate
    it's given: it hashes the key as well)."""
    return FINGERPRINT.sub("a=fingerprint:sha-256 " + value, sdp) if value else sdp


class Channel:
    def __init__(self, host: "Host", key: int, channel, connection: int) -> None:
        self.host, self.key, self.channel, self.connection = host, key, channel, connection
        self.sent = self.received = 0
        self._ids = [channel.connect("on-open", lambda _c: self._emit("open", id=self._id())),
                     channel.connect("on-close", lambda _c: self._emit("close")),
                     channel.connect("on-error", lambda _c, error: self._emit("error", message=str(error))),
                     channel.connect("on-message-data", self._on_data),
                     channel.connect("on-message-string", lambda _c, text: self._emit("message", text=text or ""))]

    def _id(self) -> Optional[int]:
        value = self.channel.get_property("id")
        return value if value is not None and value >= 0 else None

    def _emit(self, kind: str, **fields) -> None:
        if kind in ("open", "close"):
            GLib.idle_add(self.host._count, self, kind == "open")
        if kind in ("open", "close", "error"):
            _log(f"ch{self.key} (pc{self.connection}) {kind}{' ' + fields['message'] if 'message' in fields else ''}"
                 f" - sent {self.sent}, received {self.received}")
        self.host.emit({"ch": self.key, "type": kind, **fields})

    def _on_data(self, _channel, data) -> None:
        self.received += 1
        payload = data.get_data() if data is not None else b""
        self.host.emit({"ch": self.key, "type": "message", "bytes": payload or b""})

    def init(self) -> dict:
        get = self.channel.get_property
        retransmits, lifetime = get("max-retransmits"), get("max-packet-lifetime")
        return {"label": get("label") or "", "ordered": bool(get("ordered")), "protocol": get("protocol") or "",
                "negotiated": bool(get("negotiated")), "id": self._id(),
                "maxRetransmits": retransmits if retransmits is not None and retransmits >= 0 else None,
                "maxPacketLifeTime": lifetime if lifetime is not None and lifetime >= 0 else None}

    def open(self) -> bool:
        return int(self.channel.get_property("ready-state")) == int(GstWebRTC.WebRTCDataChannelState.OPEN)

    def send(self, data: Optional[bytes] = None, text: Optional[str] = None) -> None:
        self.sent += 1
        try:
            if text is not None:
                if hasattr(self.channel, "send_string_full"):
                    self.channel.send_string_full(text)
                else:
                    self.channel.emit("send-string", text)
            elif hasattr(self.channel, "send_data_full"):
                self.channel.send_data_full(GLib.Bytes.new(data))
            else:
                self.channel.emit("send-data", GLib.Bytes.new(data))
        except GLib.Error as error:
            self._emit("error", message=error.message)

    def close(self) -> None:
        self.channel.emit("close")

    def release(self) -> None:
        for handler in self._ids:
            self.channel.disconnect(handler)
        self._ids = []
        self.host._count(self, False)


class Connection:
    def __init__(self, host: "Host", key: int, config: dict, certificate: Optional[tuple[str, str]] = None) -> None:
        self.host, self.key = host, key
        self._mids: list[Optional[str]] = []
        self._own_ice = False
        self._remote_text: Optional[str] = None  # the far side's description as the page gave it
        # The page's certificate, if it gave one: webrtcbin takes it when it makes its DTLS
        # element, during the first offer or answer, and never later.
        self._pem, self._fingerprint = certificate or (None, None)
        self.pipeline = Gst.Pipeline.new(f"natter-rtc-{key}")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", None)
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.configure(config)
        self.pipeline.add(self.webrtc)
        self._ids = [self.webrtc.connect("on-negotiation-needed", lambda _w: self._emit("negotiation-needed")),
                     self.webrtc.connect("on-ice-candidate", self._on_candidate),
                     self.webrtc.connect("on-data-channel", self._on_channel),
                     self.webrtc.connect("notify::ice-gathering-state", self._on_gathering),
                     self.webrtc.connect("notify::ice-connection-state", self._on_ice),
                     self.webrtc.connect("notify::connection-state", self._on_connection),
                     self.webrtc.connect("notify::signaling-state",
                                         lambda w, _p: self._emit("signaling", value=SIGNALING.get(int(w.props.signaling_state))))]
        if self._pem:
            self._ids.append(self.webrtc.connect("deep-element-added", self._on_element))
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_element(self, _bin, _parent, element) -> None:
        factory = element.get_factory()
        if factory is not None and factory.get_name() == "dtlssrtpdec":
            element.set_property("pem", self._pem)

    def configure(self, config: dict) -> None:
        stun = None
        for server in config.get("iceServers") or []:
            for url in server.get("urls") or []:
                found = _server_url(server, url)
                if not found:
                    continue
                kind, value = found
                if kind == "stun" and stun is None:
                    stun = value  # webrtcbin takes one STUN server
                    self.webrtc.set_property("stun-server", value)
                elif kind == "turn":
                    self.webrtc.emit("add-turn-server", value)
        policy = GstWebRTC.WebRTCICETransportPolicy.RELAY if config.get("iceTransportPolicy") == "relay" \
            else GstWebRTC.WebRTCICETransportPolicy.ALL
        self.webrtc.set_property("ice-transport-policy", policy)

    def _emit(self, kind: str, **fields) -> None:
        if kind in ("ice", "connection", "signaling", "gathering"):
            _log(f"pc{self.key} {kind} {fields.get('value')}")
        self.host.emit({"pc": self.key, "type": kind, **fields})

    def _local_sdp(self) -> str:
        description = self.webrtc.get_property("local-description")
        return with_fingerprint(description.sdp.as_text(), self._fingerprint) if description else ""

    def _mid(self, mline: int) -> Optional[str]:
        # Kept from the description's text (see describe), so GStreamer's threads never touch
        # its objects: they're only safe to read on the main loop.
        return self._mids[mline] if 0 <= mline < len(self._mids) else None

    def _on_candidate(self, _webrtc, mline: int, candidate: str) -> None:
        if not candidate:
            return
        candidate = candidate[2:] if candidate.startswith("a=") else candidate
        self._emit("candidate", candidate=candidate, sdpMLineIndex=mline, sdpMid=self._mid(mline))

    def _on_ice(self, webrtc, _param) -> None:
        state = ICE.get(int(webrtc.props.ice_connection_state))
        if state != "new":
            self._own_ice = True
            self._emit("ice", value="connected" if state == "completed" else state)

    def _on_connection(self, webrtc, _param) -> None:
        state = CONNECTION.get(int(webrtc.props.connection_state))
        # webrtcbin works out its ICE state from media transceivers, so with data channels alone
        # it stays "new": then it follows the connection's state, as browsers' does here.
        if not self._own_ice and state in ICE_FROM_CONNECTION:
            self._emit("ice", value=ICE_FROM_CONNECTION[state])
        self._emit("connection", value=state)

    def _on_gathering(self, webrtc, _param) -> None:
        state = GATHERING.get(int(webrtc.props.ice_gathering_state))
        if state != "complete":
            self._emit("gathering", value=state)
            return

        def complete():
            # The full description, with every candidate, then the end-of-candidates event.
            if self._ids:
                self._emit("candidate", candidate=None, sdp=self._local_sdp())
                self._emit("gathering", value=state)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(complete)

    def _on_channel(self, _webrtc, channel) -> None:
        self.host.adopt(self, channel)

    def _promise(self, done: Callable) -> "Gst.Promise":
        # webrtcbin answers on its own thread: the answer is handled on the main loop.
        return Gst.Promise.new_with_change_func(lambda promise: GLib.idle_add(done, promise.get_reply()))

    def offer(self, reply: Callable[[dict], None], restart: bool = False) -> None:
        options = _structure("options, ice-restart=(boolean)true") if restart else None

        def done(answer):
            description = answer.get_value("offer") if answer is not None and answer.has_field("offer") else None
            reply({"sdp": with_fingerprint(description.sdp.as_text(), self._fingerprint)} if description
                  else _error(answer, "The offer couldn't be made"))
        self.webrtc.emit("create-offer", options, self._promise(done))

    def answer(self, reply: Callable[[dict], None]) -> None:
        def done(answer):
            description = answer.get_value("answer") if answer is not None and answer.has_field("answer") else None
            reply({"sdp": with_fingerprint(description.sdp.as_text(), self._fingerprint)} if description
                  else _error(answer, "The answer couldn't be made"))
        self.webrtc.emit("create-answer", None, self._promise(done))

    def describe(self, local: bool, kind: str, sdp: str, reply: Callable[[dict], None]) -> None:
        if kind not in SDP_TYPES or kind == "rollback":
            reply({"error": {"name": "NotSupportedError", "message": f"Descriptions of type {kind} aren't supported"}})
            return
        real, held = None, []
        if local:
            sdp = with_fingerprint(sdp, self._fingerprint)
        else:
            self._remote_text = sdp
            sdp, real, held = _for_webrtcbin(sdp)
            if real:
                _log(f"pc{self.key} far side's ICE login set directly (ufrag {len(real[0])} characters, "
                     f"password {len(real[1])}, {len(held)} candidate(s))")
        try:
            description = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType(SDP_TYPES[kind]), _parse_sdp(sdp))
        except ValueError as error:
            reply({"error": {"name": "OperationError", "message": str(error)}})
            return

        if local:
            self._mids = _mids(sdp)

        def done(answer):
            if answer is not None and answer.has_field("error"):
                reply(_error(answer, "The description wasn't accepted"))
                return
            if real:
                if not _set_remote_credentials(self.webrtc, *real):
                    reply({"error": {"name": "OperationError", "message": "The far side's ICE login couldn't be set"}})
                    return
                for mline, candidate in held:
                    self.webrtc.emit("add-ice-candidate", mline, candidate)
            reply(self.state())
        signal = "set-local-description" if local else "set-remote-description"
        self.webrtc.emit(signal, description, self._promise(done))

    def state(self) -> dict:
        local, remote = self.webrtc.get_property("local-description"), self.webrtc.get_property("remote-description")
        signaling = SIGNALING.get(int(self.webrtc.props.signaling_state))
        remote_sdp = (self._remote_text or remote.sdp.as_text()) if remote else ""
        has_data = "m=application" in (remote_sdp or self._local_sdp())
        return {"local": {"type": _sdp_type(local), "sdp": with_fingerprint(local.sdp.as_text(), self._fingerprint)} if local else None,
                "remote": {"type": _sdp_type(remote), "sdp": remote_sdp} if remote else None,
                "signalingState": signaling, "stable": signaling == "stable",
                "sctp": {"maxMessageSize": _max_message_size(remote_sdp)} if has_data and remote else None}

    def candidate(self, candidate: str, mid: Optional[str], mline: Optional[int], reply: Callable[[dict], None]) -> None:
        if mline is None:
            mline = self._mline_for(mid)
        if mline is None:
            reply({"error": {"name": "OperationError", "message": "No media section for that candidate"}})
            return
        self.webrtc.emit("add-ice-candidate", mline, ("a=" + candidate) if candidate and not candidate.startswith("a=") else candidate)
        reply({})

    def _mline_for(self, mid: Optional[str]) -> Optional[int]:
        remote = self.webrtc.get_property("remote-description")
        if remote is None:
            return None
        for index in range(remote.sdp.medias_len()):
            if mid is None or remote.sdp.get_media(index).get_attribute_val("mid") == mid:
                return index
        return None

    def stats(self, reply: Callable[[dict], None]) -> None:
        def done(answer):
            stats = []
            if answer is not None:
                for index in range(answer.n_fields()):
                    value = answer.get_value(answer.nth_field_name(index))
                    if isinstance(value, Gst.Structure):
                        stats.append(_stat(value))
            reply({"stats": stats})
        self.webrtc.emit("get-stats", None, self._promise(done))

    def channel(self, message: dict):
        options = ["options"]
        if message.get("ordered") is False:
            options.append("ordered=(boolean)false")
        if message.get("maxRetransmits") is not None:
            options.append(f"max-retransmits=(int){int(message['maxRetransmits'])}")
        if message.get("maxPacketLifeTime") is not None:
            options.append(f"max-packet-lifetime=(int){int(message['maxPacketLifeTime'])}")
        if message.get("negotiated"):
            options.append("negotiated=(boolean)true")
        if message.get("id") is not None:
            options.append(f"id=(int){int(message['id'])}")
        if message.get("protocol"):
            options.append(f"protocol=(string){Gst.Value.serialize(str(message['protocol']))}")
        structure = _structure(", ".join(options))
        return self.webrtc.emit("create-data-channel", str(message.get("label") or ""), structure)

    def close(self) -> None:
        for handler in self._ids:
            self.webrtc.disconnect(handler)
        self._ids = []
        self.pipeline.set_state(Gst.State.NULL)


def _mids(sdp: str) -> list[Optional[str]]:
    """Each media section's mid, in order."""
    mids = []
    for section in re.split(r"^m=", sdp, flags=re.M)[1:]:
        match = re.search(r"^a=mid:(\S+)", section, re.M)
        mids.append(match.group(1) if match else None)
    return mids


def _sdp_type(description) -> str:
    return {1: "offer", 2: "pranswer", 3: "answer", 4: "rollback"}.get(int(description.type), "offer")


def _error(answer, fallback: str) -> dict:
    message = fallback
    if answer is not None and answer.has_field("error"):
        error = answer.get_value("error")
        message = getattr(error, "message", None) or str(error) or fallback
    return {"error": {"name": "OperationError", "message": message}}


# ---- The page's video codecs ---------------------------------------------------------------------
# rtc.js keeps counts of what each VideoDecoder and VideoEncoder in the page does, and of the
# workers it starts: codec names, sizes and numbers, never content. They go to the log, so a
# runaway (a video decoded whole with its frames kept, as once held 12 GB) shows what did it.

LOG_EVERY = 1.0  # seconds between two lines for the same codec, at least
_CODEC_NAME = re.compile(r"[A-Za-z0-9.]{1,40}")
_ERROR_NAME = re.compile(r"[A-Za-z]{1,40}")
_FIELDS = {"dec": {"op", "kind", "id", "codec", "desc", "lat", "rewrite", "w", "h", "out", "open", "held", "queue",
                   "drops", "err"},
           "enc": {"op", "kind", "id", "codec", "mode", "w", "h", "out", "queue", "drops", "err"},
           "page": {"op", "kind", "workers", "annexb"}}


def webcodecs_line(message: dict) -> Optional[tuple[str, str]]:
    """(which codec, its log line) for one of rtc.js's counts messages, or None for anything else.

    Only the fields rtc.js sends, each as it sends it: a message with another field, a long or
    odd string, or a value of the wrong kind is dropped whole.
    """
    if not isinstance(message, dict) or message.get("op") != "webcodecs":
        return None
    kind = message.get("kind")
    if not isinstance(kind, str) or kind not in _FIELDS or not set(message) <= _FIELDS[kind]:
        return None

    def count(key: str, limit: int = 10 ** 9) -> Optional[int]:
        value = message.get(key, 0)
        return value if type(value) is int and 0 <= value <= limit else None

    def yes(key: str, words=("no", "yes")) -> Optional[str]:
        value = message.get(key, False)
        return words[value] if type(value) is bool else None

    if kind == "page":
        workers, annexb = count("workers"), count("annexb")
        if workers is None or annexb is None:
            return None
        return "page", f"workers={workers} annexb-without-latency={annexb}"

    codec, error = message.get("codec", ""), message.get("err", "")
    if not isinstance(codec, str) or (codec and not _CODEC_NAME.fullmatch(codec)):
        return None
    if not isinstance(error, str) or (error and not _ERROR_NAME.fullmatch(error)):
        return None
    numbers = {key: count(key) for key in ("out", "open", "held", "queue", "drops")}
    ident, width, height = count("id"), count("w", 65535), count("h", 65535)
    if None in numbers.values() or not ident or width is None or height is None:
        return None
    parts = [f"{kind}#{ident}", codec or "?"]
    if kind == "dec":
        flags = (yes("desc"), yes("lat"), yes("rewrite", ("off", "on")))
        if None in flags:
            return None
        parts += [f"desc={flags[0]}", f"lat={flags[1]}", f"rewrite={flags[2]}"]
    else:
        mode = message.get("mode", "")
        if mode not in ("", "quality", "realtime"):
            return None
        parts.append(f"mode={mode or '?'}")
    parts.append(f"{width}x{height}" if width and height else "size=?")
    keys = ("out", "open", "held", "queue", "drops") if kind == "dec" else ("out", "queue", "drops")
    parts += [f"{key}={numbers[key]}" for key in keys]
    if error:
        parts.append(f"err={error}")
    return f"{kind}#{ident}", " ".join(parts)


class CodecLog:
    """rtc.js's codec counts as log lines, one a second at most for each codec (and the page's).

    rtc.js sends them no more often than that; a line that comes sooner waits for its second,
    and only the latest one waiting is kept.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 later: Optional[Callable[[int, Callable[[], bool]], int]] = None,
                 write: Optional[Callable[[str], None]] = None) -> None:
        self._clock = clock
        self._later = later or GLib.timeout_add
        self._write = write or (lambda line: print(line, flush=True))
        self._logged: dict[str, float] = {}  # codec -> when its last line went out
        self._waiting: dict[str, str] = {}
        self._timer = 0

    def take(self, message: dict) -> None:
        found = webcodecs_line(message)
        if found is None:
            return
        key, line = found
        if key not in self._waiting and self._due(key, self._clock()):
            self._log(key, line)
            return
        self._waiting[key] = line
        if not self._timer:
            self._timer = self._later(int(LOG_EVERY * 1000), self._catch_up)

    def _due(self, key: str, now: float) -> bool:
        return key not in self._logged or now - self._logged[key] >= LOG_EVERY

    def _log(self, key: str, line: str) -> None:
        now = self._clock()
        self._logged[key] = now
        self._write(f"natter: webcodecs: {line}")
        if len(self._logged) > 256:  # codecs long gone
            for old in [k for k, at in self._logged.items() if now - at > 60]:
                del self._logged[old]

    def _catch_up(self) -> bool:
        self._timer = 0
        for key, line in list(self._waiting.items()):
            if self._due(key, self._clock()):
                del self._waiting[key]
                self._log(key, line)
        if self._waiting:
            self._timer = self._later(int(LOG_EVERY * 1000), self._catch_up)
        return False  # GLib.SOURCE_REMOVE


def pack(events: list) -> str:
    """A batch for the page: [base64 of all its packets' bytes, items], an item being
    [channel, length] for a packet or the event itself."""
    chunks, items = [], []
    for event in events:
        payload = event.get("bytes")
        if payload is None:
            items.append(event)
        else:
            chunks.append(payload)
            items.append([event["ch"], len(payload)])
    return json.dumps([base64.b64encode(b"".join(chunks)).decode() if chunks else "", items], separators=(",", ":"))


def _stat(structure) -> dict:
    stat = {}
    for index in range(structure.n_fields()):
        name = structure.nth_field_name(index)
        stat[_camel(name)] = _plain(structure.get_value(name))
    stat.setdefault("id", structure.get_name())
    if isinstance(stat.get("type"), int):
        try:
            stat["type"] = GstWebRTC.WebRTCStatsType(stat["type"]).value_nick
        except ValueError:
            pass
    if "timestamp" in stat and isinstance(stat["timestamp"], (int, float)):
        stat["timestamp"] = float(stat["timestamp"])
    return stat


class Host:
    """The page's peer connections and data channels.

    The page keeps a "pull" waiting (`pull`), answered with the next batch of events, in order.
    """

    def __init__(self, on_call: Optional[Callable[[bool], None]] = None) -> None:
        if not Gst.is_initialized():
            Gst.init(None)
        self._on_call = on_call  # on_call(True) when a call's channel opens, on_call(False) when the last closes
        self._calling: set[int] = set()
        self._connections: dict[int, Connection] = {}
        self._channels: dict[int, Channel] = {}
        self._remote_keys = itertools.count(1 << 30)  # channels the far side opened, apart from the page's own
        self._certificates: dict[str, tuple[str, str]] = {}  # id -> (PEM, fingerprint), for this page only
        self._certificate_ids = itertools.count(1)
        self._lock = threading.Lock()
        self._outbox: list[dict] = []
        self._flush = 0
        self._waiting = None  # (reply, JS context) of the page's waiting pull
        self._sent_at = 0.0
        self._codec_log = CodecLog()

    # ---- To the page ----

    def emit(self, event: dict) -> None:
        """Queue an event for the page (from any thread)."""
        with self._lock:
            self._outbox.append(event)
            self._schedule()

    def pull(self, reply, context) -> None:
        """The page waits for its next batch (main loop): `reply` is answered once there is one."""
        if self._waiting is not None:
            self._answer("")
        with self._lock:
            self._waiting = (reply, context)
            if self._outbox:
                self._schedule()

    def _schedule(self) -> None:
        """Send what's queued FLUSH_MS after the last batch, or now if that was longer ago (under the lock)."""
        if self._flush or self._waiting is None:
            return
        wait = FLUSH_MS - (time.monotonic() - self._sent_at) * 1000
        self._flush = GLib.timeout_add(max(0, int(wait + 0.999)), self._send)

    def _answer(self, text: str) -> None:
        reply, context = self._waiting
        self._waiting = None
        reply.return_value(JavaScriptCore.Value.new_string(context, text))

    def _send(self) -> bool:
        with self._lock:
            self._flush = 0
            if self._waiting is None or not self._outbox:
                return GLib.SOURCE_REMOVE
            events, self._outbox, self._sent_at = self._outbox, [], time.monotonic()
        self._answer(pack(events))
        return GLib.SOURCE_REMOVE

    @property
    def calling(self) -> bool:
        return bool(self._calling)

    def _count(self, channel: "Channel", is_open: bool) -> bool:
        """Keep count of open call channels (main loop)."""
        before = bool(self._calling)
        label = channel.channel.get_property("label") or ""
        if is_open and label not in NOT_CALLS and channel.key in self._channels:
            self._calling.add(channel.key)
        else:
            self._calling.discard(channel.key)
        if bool(self._calling) != before:
            _log("call " + ("started" if self._calling else "ended"))
            if self._on_call:
                self._on_call(bool(self._calling))
        return GLib.SOURCE_REMOVE

    def adopt(self, connection: Connection, gst_channel) -> None:
        """A channel the far side opened (from GStreamer's thread)."""
        key = next(self._remote_keys)

        def take():
            channel = Channel(self, key, gst_channel, connection.key)
            self._channels[key] = channel
            self.emit({"pc": connection.key, "ch": key, "type": "datachannel", "init": channel.init(), "open": channel.open()})
            return GLib.SOURCE_REMOVE
        GLib.idle_add(take)

    # ---- From the page ----

    def post(self, message: dict) -> None:
        """Something the page did that needs no answer."""
        op = message.get("op")
        if op == "sends":
            self._sends(message)
            return
        if op == "webcodecs":
            self._codec_log.take(message)
            return
        if op != "send":
            _log(f"{op} pc{message.get('pc')}" + (f" ch{message.get('ch')} {message.get('label')!r}" if op == "channel" else "")
                 + (f" ({len((message.get('config') or {}).get('certificates') or [])} certificate(s),"
                    f" {len((message.get('config') or {}).get('iceServers') or [])} ICE server(s))" if op == "create" else ""))
        if op == "send":
            channel = self._channels.get(message.get("ch"))
            if channel:
                if "text" in message:
                    channel.send(text=str(message["text"]))
                else:
                    channel.send(data=base64.b64decode(message.get("data") or ""))
        elif op == "create":
            key = message.get("pc")
            if isinstance(key, int) and key not in self._connections:
                config = message.get("config") or {}
                given = [self._certificates[c] for c in config.get("certificates") or [] if c in self._certificates]
                self._connections[key] = Connection(self, key, config, given[0] if given else None)
        elif op == "channel":
            connection, key = self._connections.get(message.get("pc")), message.get("ch")
            if connection and isinstance(key, int) and key not in self._channels:
                gst_channel = connection.channel(message)
                if gst_channel is None:
                    self.emit({"ch": key, "type": "error", "message": "The data channel couldn't be made"})
                    self.emit({"ch": key, "type": "close"})
                else:
                    self._channels[key] = Channel(self, key, gst_channel, connection.key)
        elif op == "close-channel":
            channel = self._channels.get(message.get("ch"))
            if channel:
                channel.close()
        elif op == "configure":
            connection = self._connections.get(message.get("pc"))
            if connection:
                connection.configure(message.get("config") or {})
        elif op == "restart-ice":
            connection = self._connections.get(message.get("pc"))
            if connection:
                connection._emit("negotiation-needed")
        elif op == "close":
            self._close(message.get("pc"))

    def _sends(self, message: dict) -> None:
        """Packets the page sent in one go: one base64 text, cut up by [channel, length, ...]."""
        try:
            data = base64.b64decode(message.get("data") or "")
        except ValueError:
            return
        cuts, offset = message.get("cuts") or [], 0
        for i in range(0, len(cuts) - 1, 2):
            key, size = cuts[i], cuts[i + 1]
            if not isinstance(size, int) or size < 0:
                return
            channel = self._channels.get(key)
            if channel:
                channel.send(data=data[offset:offset + size])
            offset += size

    def call(self, message: dict, reply: Callable[[dict], None]) -> None:
        """Something the page asked that has an answer; `reply` is called once, on the main loop."""
        op = message.get("op")
        if DEBUG:
            answer = reply

            def reply(result: dict, op=op, pc=message.get("pc"), kind=message.get("type")) -> None:
                error = result.get("error") if isinstance(result, dict) else None
                if op != "stats":
                    _log(f"{op}{' ' + kind if kind else ''} pc{pc} " + (f"failed: {error.get('message')}" if error else "ok"))
                answer(result)
        if op == "certificate":
            self._certificate(message.get("algorithm") or {}, reply)
            return
        connection = self._connections.get(message.get("pc"))
        if connection is None:
            reply({"error": {"name": "InvalidStateError", "message": "The connection is closed"}})
        elif op == "offer":
            connection.offer(reply, restart=bool(message.get("iceRestart")))
        elif op == "answer":
            connection.answer(reply)
        elif op in ("set-local", "set-remote"):
            connection.describe(op == "set-local", str(message.get("type")), str(message.get("sdp") or ""), reply)
        elif op == "candidate":
            mline = message.get("sdpMLineIndex")
            connection.candidate(str(message.get("candidate") or ""), message.get("sdpMid"),
                                 mline if isinstance(mline, int) else None, reply)
        elif op == "stats":
            connection.stats(reply)
        else:
            reply({"error": {"name": "NotSupportedError", "message": "Not supported"}})

    def _certificate(self, algorithm: dict, reply: Callable[[dict], None]) -> None:
        name, curve = str(algorithm.get("name") or "").upper(), str(algorithm.get("namedCurve") or "P-256")
        if name != "ECDSA" or curve != "P-256":
            reply({"error": {"name": "NotSupportedError", "message": "Only ECDSA P-256 certificates are supported"}})
            return

        def make():
            try:
                made = make_certificate()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                GLib.idle_add(reply, {"error": {"name": "OperationError", "message": f"No certificate: {error}"}})
                return
            GLib.idle_add(self._keep_certificate, made, reply)
        threading.Thread(target=make, name="natter-rtc-certificate", daemon=True).start()

    def _keep_certificate(self, made: tuple[str, str], reply: Callable[[dict], None]) -> bool:
        key = str(next(self._certificate_ids))
        self._certificates[key] = made
        reply({"id": key, "expires": (time.time() + CERTIFICATE_DAYS * 86400) * 1000,
               "fingerprints": [{"algorithm": "sha-256", "value": made[1].lower()}]})
        return GLib.SOURCE_REMOVE

    def _close(self, key) -> None:
        connection = self._connections.pop(key, None)
        if connection is None:
            return
        for channel_key, channel in list(self._channels.items()):
            if channel.connection == key:
                if channel.sent or channel.received:
                    _log(f"ch{channel_key} (pc{key}) closed - sent {channel.sent}, received {channel.received}")
                channel.release()
                del self._channels[channel_key]
        connection.close()

    def close(self) -> None:
        """Close everything (the page went away)."""
        for channel in self._channels.values():
            channel.release()
        self._channels.clear()
        for key in list(self._connections):
            self._close(key)
        self._certificates.clear()
        if self._waiting is not None:
            self._answer("")
        with self._lock:
            self._outbox.clear()
            if self._flush:
                GLib.source_remove(self._flush)
                self._flush = 0
