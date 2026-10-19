// WebRTC data channels for WhatsApp's calls. Runs before WhatsApp's own scripts, in the page.
//
// This WebKit is built without WebRTC, but WhatsApp's calls only use RTCPeerConnection for data
// channels: its own call engine (WebAssembly) makes and encrypts the audio and video, and sends
// the packets over unordered data channels to WhatsApp's relays or the other phone. So this file
// gives the page RTCPeerConnection and RTCDataChannel, and Natter (rtc.py) does the networking
// with GStreamer's webrtcbin. No media tracks: addTrack and the like say they're not supported.
//
// Up to Natter: one message handler for everything (window.webkit.messageHandlers.natterRtc),
// and a second, answering one for calls that return something (createOffer, getStats...).
// Down from Natter: window.__natterRtc.deliver(events), a batch of events in order.
(() => {
  "use strict";
  if (window.__natterRtc || typeof window.RTCPeerConnection === "function") return;

  // The handlers and token are taken now, before WhatsApp's scripts run (see bridge.js).
  const TOKEN = "__NATTER_TOKEN__";
  const handlers = window.webkit && window.webkit.messageHandlers;
  const postHandler = handlers && handlers.natterRtc;
  const callHandler = handlers && handlers.natterRtcCall;
  if (!postHandler || !callHandler) return;
  const post = postHandler.postMessage.bind(postHandler);
  const ask = callHandler.postMessage.bind(callHandler);
  const send = (message) => post({ ...message, token: TOKEN });
  const call = async (message) => {
    const reply = await ask({ ...message, token: TOKEN });
    if (reply && reply.error) throw new DOMException(reply.error.message || "WebRTC failed", reply.error.name || "OperationError");
    return reply;
  };

  // ---- Binary data -------------------------------------------------------------------------
  // Packets cross as base64 text: the one form both directions carry without surprises.

  function toBase64(bytes) {
    if (typeof bytes.toBase64 === "function") return bytes.toBase64();
    let text = "";
    for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(text);
  }

  function fromBase64(text) {
    if (typeof Uint8Array.fromBase64 === "function") return Uint8Array.fromBase64(text);
    const raw = atob(text);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function bytesOf(data) {
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    if (typeof SharedArrayBuffer === "function" && data instanceof SharedArrayBuffer) return new Uint8Array(data);
    return null;
  }

  // ---- Events --------------------------------------------------------------------------------

  // The `onfoo = handler` properties, kept beside addEventListener as browsers do.
  function eventProperties(proto, names) {
    for (const name of names) {
      const key = Symbol(name);
      Object.defineProperty(proto, "on" + name, {
        configurable: true, enumerable: true,
        get() { return this[key] || null; },
        set(handler) {
          if (this[key]) this.removeEventListener(name, this[key]);
          this[key] = typeof handler === "function" ? handler : null;
          if (this[key]) this.addEventListener(name, this[key]);
        },
      });
    }
  }

  function fire(target, name, init = {}) {
    let event;
    if (name === "message") event = new MessageEvent("message", init);
    else {
      event = new Event(name);
      for (const [key, value] of Object.entries(init)) Object.defineProperty(event, key, { value, enumerable: true });
    }
    target.dispatchEvent(event);
  }

  const fail = (name, message) => new DOMException(message, name);

  // ---- Descriptions, candidates, certificates -----------------------------------------------

  class RTCSessionDescription {
    constructor(init = {}) {
      this.type = init.type || "";
      this.sdp = init.sdp || "";
    }
    toJSON() { return { type: this.type, sdp: this.sdp }; }
  }

  class RTCIceCandidate {
    constructor(init = {}) {
      if (init.sdpMid == null && init.sdpMLineIndex == null) throw new TypeError("sdpMid and sdpMLineIndex are both null");
      this.candidate = init.candidate || "";
      this.sdpMid = init.sdpMid ?? null;
      this.sdpMLineIndex = init.sdpMLineIndex ?? null;
      this.usernameFragment = init.usernameFragment ?? null;
      const parts = this.candidate.replace(/^a=/, "").replace(/^candidate:/, "").split(/\s+/);
      this.foundation = parts[0] || null;
      this.component = parts[1] === "1" ? "rtp" : parts[1] === "2" ? "rtcp" : null;
      this.protocol = parts[2] ? parts[2].toLowerCase() : null;
      this.priority = parts[3] ? Number(parts[3]) : null;
      this.address = parts[4] || null;
      this.port = parts[5] ? Number(parts[5]) : null;
      const at = parts.indexOf("typ");
      this.type = at >= 0 ? parts[at + 1] : null;
    }
    toJSON() {
      return { candidate: this.candidate, sdpMid: this.sdpMid, sdpMLineIndex: this.sdpMLineIndex,
               usernameFragment: this.usernameFragment };
    }
  }

  class RTCCertificate {
    constructor(id, expires, fingerprints) {
      Object.defineProperty(this, "_id", { value: id });
      Object.defineProperty(this, "_fingerprints", { value: fingerprints });
      this.expires = expires;
    }
    getFingerprints() { return this._fingerprints.map((f) => ({ ...f })); }
  }

  // ---- Data channels -----------------------------------------------------------------------

  const channels = new Map();  // Natter's channel id -> RTCDataChannel
  let nextChannel = 1;

  class RTCDataChannel extends EventTarget {
    constructor(pc, key, init) {
      super();
      Object.defineProperty(this, "_pc", { value: pc });
      Object.defineProperty(this, "_key", { value: key });
      this.label = init.label || "";
      this.ordered = init.ordered !== false;
      this.maxPacketLifeTime = init.maxPacketLifeTime ?? null;
      this.maxRetransmits = init.maxRetransmits ?? null;
      this.protocol = init.protocol || "";
      this.negotiated = Boolean(init.negotiated);
      this.id = init.id ?? null;
      this.readyState = init.readyState || "connecting";
      this.bufferedAmount = 0;
      this.bufferedAmountLowThreshold = 0;
      this.binaryType = "arraybuffer";
      channels.set(key, this);
    }

    get priority() { return "low"; }

    send(data) {
      if (this.readyState !== "open") throw fail("InvalidStateError", "The data channel isn't open");
      let message;
      if (typeof data === "string") message = { ch: this._key, text: data };
      else {
        const bytes = bytesOf(data);
        if (!bytes) {
          if (typeof Blob === "function" && data instanceof Blob) throw fail("NotSupportedError", "Blobs aren't supported here");
          throw new TypeError("Can't send that over a data channel");
        }
        message = { ch: this._key, data: toBase64(bytes), size: bytes.byteLength };
      }
      const size = message.size ?? message.text.length;
      this.bufferedAmount += size;
      send({ op: "send", ...message });
      // Natter takes it at once: what's still buffered there comes back in "buffered" events.
      queueMicrotask(() => this._sent(size));
    }

    _sent(size) {
      const before = this.bufferedAmount;
      this.bufferedAmount = Math.max(0, before - size);
      if (before > this.bufferedAmountLowThreshold && this.bufferedAmount <= this.bufferedAmountLowThreshold)
        fire(this, "bufferedamountlow");
    }

    close() {
      if (this.readyState === "closing" || this.readyState === "closed") return;
      this.readyState = "closing";
      send({ op: "close-channel", ch: this._key });
    }

    _event(event) {
      switch (event.type) {
        case "open":
          if (this.readyState !== "connecting") return;
          if (event.id != null) this.id = event.id;
          this.readyState = "open";
          fire(this, "open");
          break;
        case "message": {
          if (this.readyState !== "open") return;
          let data = event.text;
          if (data === undefined) {
            const bytes = fromBase64(event.data);
            data = this.binaryType === "blob" ? new Blob([bytes]) : bytes.buffer;
          }
          fire(this, "message", { data, origin: location.origin });
          break;
        }
        case "error":
          fire(this, "error", { error: fail("OperationError", event.message || "The data channel failed") });
          break;
        case "closing":
          if (this.readyState === "open" || this.readyState === "connecting") {
            this.readyState = "closing";
            fire(this, "closing");
          }
          break;
        case "close":
          this._closed();
          break;
      }
    }

    _closed(tell = true) {
      if (this.readyState === "closed") return;
      this.readyState = "closed";
      channels.delete(this._key);
      if (tell) fire(this, "close");
    }
  }
  eventProperties(RTCDataChannel.prototype, ["open", "message", "error", "closing", "close", "bufferedamountlow"]);

  // ---- Peer connections --------------------------------------------------------------------

  const connections = new Map();  // Natter's connection id -> RTCPeerConnection
  let nextConnection = 1;

  // What Natter needs of a configuration: STUN/TURN servers, policies, certificates.
  function plainConfiguration(config = {}) {
    const servers = (config.iceServers || []).map((server) => ({
      urls: [].concat(server.urls || server.url || []).map(String),
      username: server.username == null ? undefined : String(server.username),
      credential: server.credential == null ? undefined : String(server.credential),
    }));
    return {
      iceServers: servers,
      iceTransportPolicy: config.iceTransportPolicy || "all",
      bundlePolicy: config.bundlePolicy || "balanced",
      rtcpMuxPolicy: config.rtcpMuxPolicy || "require",
      certificates: (config.certificates || []).map((certificate) => certificate._id).filter(Boolean),
    };
  }

  class RTCPeerConnection extends EventTarget {
    constructor(configuration = {}) {
      super();
      const key = nextConnection++;
      Object.defineProperty(this, "_key", { value: key });
      Object.defineProperty(this, "_configuration", { value: { ...configuration }, writable: true });
      this.localDescription = null;
      this.remoteDescription = null;
      this.currentLocalDescription = null;
      this.pendingLocalDescription = null;
      this.currentRemoteDescription = null;
      this.pendingRemoteDescription = null;
      this.signalingState = "stable";
      this.iceGatheringState = "new";
      this.iceConnectionState = "new";
      this.connectionState = "new";
      this.canTrickleIceCandidates = null;
      this.sctp = null;
      Object.defineProperty(this, "_lastOffer", { value: null, writable: true });
      Object.defineProperty(this, "_lastAnswer", { value: null, writable: true });
      connections.set(key, this);
      send({ op: "create", pc: key, config: plainConfiguration(configuration) });
    }

    static async generateCertificate(algorithm) {
      const name = typeof algorithm === "string" ? algorithm : algorithm && algorithm.name;
      if (!/^(ECDSA|RSASSA-PKCS1-v1_5)$/i.test(name || "")) throw fail("NotSupportedError", "Unsupported certificate algorithm");
      const reply = await call({ op: "certificate", algorithm: { ...algorithm, name } });
      return new RTCCertificate(reply.id, reply.expires, reply.fingerprints);
    }

    get _closedState() { return this.signalingState === "closed"; }

    _check() {
      if (this._closedState) throw fail("InvalidStateError", "The connection is closed");
    }

    getConfiguration() { return { ...this._configuration }; }

    setConfiguration(configuration = {}) {
      this._check();
      this._configuration = { ...configuration };
      send({ op: "configure", pc: this._key, config: plainConfiguration(configuration) });
    }

    createDataChannel(label, init = {}) {
      this._check();
      if (init.maxPacketLifeTime != null && init.maxRetransmits != null)
        throw new TypeError("maxPacketLifeTime and maxRetransmits can't both be set");
      if (init.negotiated && init.id == null) throw new TypeError("A negotiated channel needs an id");
      const key = nextChannel++;
      const channel = new RTCDataChannel(this, key, { ...init, label: String(label ?? "") });
      send({ op: "channel", pc: this._key, ch: key, label: channel.label, ordered: channel.ordered,
             maxPacketLifeTime: channel.maxPacketLifeTime, maxRetransmits: channel.maxRetransmits,
             protocol: channel.protocol, negotiated: channel.negotiated, id: channel.id });
      return channel;
    }

    async createOffer(options) {
      if (typeof options === "function") throw new TypeError("The callback form isn't supported");
      this._check();
      const reply = await call({ op: "offer", pc: this._key, iceRestart: Boolean(options && options.iceRestart) });
      this._lastOffer = reply.sdp;
      return { type: "offer", sdp: reply.sdp };
    }

    async createAnswer() {
      this._check();
      if (this.signalingState !== "have-remote-offer") throw fail("InvalidStateError", "There's no offer to answer");
      const reply = await call({ op: "answer", pc: this._key });
      this._lastAnswer = reply.sdp;
      return { type: "answer", sdp: reply.sdp };
    }

    async setLocalDescription(description) {
      this._check();
      let type = description && description.type;
      let sdp = description && description.sdp;
      if (!type) type = this.signalingState === "have-remote-offer" ? "answer" : "offer";
      if (!sdp) sdp = type === "offer" ? (this._lastOffer || (await this.createOffer()).sdp)
                                       : (this._lastAnswer || (await this.createAnswer()).sdp);
      const reply = await call({ op: "set-local", pc: this._key, type, sdp });
      this._described(reply);
    }

    async setRemoteDescription(description) {
      this._check();
      if (!description || !description.type) throw new TypeError("A description needs a type");
      const reply = await call({ op: "set-remote", pc: this._key, type: description.type, sdp: description.sdp || "" });
      this._described(reply);
    }

    async addIceCandidate(candidate) {
      this._check();
      if (!this.remoteDescription) throw fail("InvalidStateError", "There's no remote description yet");
      const init = candidate == null ? {} : candidate instanceof RTCIceCandidate ? candidate.toJSON() : candidate;
      if (init.candidate && init.sdpMid == null && init.sdpMLineIndex == null)
        throw new TypeError("sdpMid and sdpMLineIndex are both null");
      await call({ op: "candidate", pc: this._key, candidate: init.candidate || "",
                   sdpMid: init.sdpMid ?? null, sdpMLineIndex: init.sdpMLineIndex ?? null });
    }

    async getStats(selector) {
      this._check();
      const report = new Map();
      try {
        const reply = await call({ op: "stats", pc: this._key });
        for (const stat of reply.stats || []) report.set(stat.id, Object.freeze(stat));
      } catch (_) {
        // An empty report rather than a failure: stats only ever go to logs.
      }
      return report;
    }

    restartIce() { send({ op: "restart-ice", pc: this._key }); }

    close() {
      if (this._closedState) return;
      this.signalingState = "closed";
      this.iceConnectionState = "closed";
      this.connectionState = "closed";
      // As in browsers, its channels close at once and quietly: no close events.
      for (const channel of [...channels.values()]) if (channel._pc === this) channel._closed(false);
      connections.delete(this._key);
      send({ op: "close", pc: this._key });
    }

    // No media: WhatsApp's call engine doesn't add tracks.
    addTrack() { throw fail("NotSupportedError", "Media tracks aren't supported here"); }
    removeTrack() { throw fail("NotSupportedError", "Media tracks aren't supported here"); }
    addTransceiver() { throw fail("NotSupportedError", "Media tracks aren't supported here"); }
    getSenders() { return []; }
    getReceivers() { return []; }
    getTransceivers() { return []; }

    _described(reply) {
      const local = reply.local ? new RTCSessionDescription(reply.local) : null;
      const remote = reply.remote ? new RTCSessionDescription(reply.remote) : null;
      this.localDescription = local;
      this.remoteDescription = remote;
      this.currentLocalDescription = reply.stable ? local : null;
      this.pendingLocalDescription = reply.stable ? null : local;
      this.currentRemoteDescription = reply.stable ? remote : null;
      this.pendingRemoteDescription = reply.stable ? null : remote;
      if (remote && this.canTrickleIceCandidates === null)
        this.canTrickleIceCandidates = /a=ice-options:[^\r\n]*trickle/.test(remote.sdp);
      if (reply.sctp && !this.sctp) this.sctp = { maxMessageSize: reply.sctp.maxMessageSize, state: "connecting",
                                                  maxChannels: null, transport: null, onstatechange: null };
      this._state("signalingState", "signalingstatechange", reply.signalingState);
    }

    _state(property, name, value) {
      if (!value || this[property] === value || this._closedState) return;
      this[property] = value;
      fire(this, name);
    }

    _event(event) {
      if (this._closedState) return;
      switch (event.type) {
        case "candidate": {
          const candidate = event.candidate
            ? new RTCIceCandidate({ candidate: event.candidate, sdpMid: event.sdpMid, sdpMLineIndex: event.sdpMLineIndex,
                                    usernameFragment: event.usernameFragment })
            : null;
          if (event.sdp && this.localDescription) this.localDescription = new RTCSessionDescription({ type: this.localDescription.type, sdp: event.sdp });
          fire(this, "icecandidate", { candidate });
          break;
        }
        case "gathering":
          this._state("iceGatheringState", "icegatheringstatechange", event.value);
          break;
        case "ice":
          this._state("iceConnectionState", "iceconnectionstatechange", event.value);
          break;
        case "connection":
          if (this.sctp && event.value === "connected") this.sctp.state = "connected";
          this._state("connectionState", "connectionstatechange", event.value);
          break;
        case "signaling":
          this._state("signalingState", "signalingstatechange", event.value);
          break;
        case "negotiation-needed":
          fire(this, "negotiationneeded");
          break;
        case "datachannel": {
          const channel = new RTCDataChannel(this, event.ch, { ...event.init, readyState: event.open ? "open" : "connecting" });
          fire(this, "datachannel", { channel });
          break;
        }
      }
    }
  }
  eventProperties(RTCPeerConnection.prototype, ["icecandidate", "icecandidateerror", "icegatheringstatechange",
                                                "iceconnectionstatechange", "connectionstatechange",
                                                "signalingstatechange", "negotiationneeded", "datachannel", "track"]);

  // ---- From Natter ---------------------------------------------------------------------------

  function deliver(events) {
    for (const event of events) {
      try {
        if (event.ch != null && event.type !== "datachannel") {
          const channel = channels.get(event.ch);
          if (channel) channel._event(event);
        } else {
          const pc = connections.get(event.pc);
          if (pc) pc._event(event);
        }
      } catch (error) {
        setTimeout(() => { throw error; });  // a page handler's error: report it, keep delivering
      }
    }
  }

  // ---- Call sound out ----------------------------------------------------------------------
  // Without AudioContext.setSinkId, WhatsApp plays the other side through an <audio> element fed
  // by a MediaStreamDestination, which this WebKit never starts ("startPlayback" times out).
  // With it, the call's worklet goes straight to the context's destination, which plays. Sound
  // goes to the system's current output either way, so choosing a device is only remembered.
  const Context = window.AudioContext;
  if (Context && typeof Context.prototype.setSinkId !== "function") {
    const sinks = new WeakMap();
    Object.defineProperty(Context.prototype, "sinkId", {
      configurable: true, enumerable: true,
      get() { return sinks.get(this) ?? ""; },
    });
    Object.defineProperty(Context.prototype, "setSinkId", {
      configurable: true, writable: true,
      value: async function setSinkId(sinkId) {
        sinks.set(this, typeof sinkId === "string" ? sinkId : "");
      },
    });
  }

  // ---- Call video in --------------------------------------------------------------------------
  // WhatsApp decodes video with VideoDecoder configured for H.264 without a description, fed
  // Annex B chunks (start codes, SPS/PPS in key frames), as the spec allows. WebKitGTK's decoder
  // takes that for length-prefixed H.264 and fails ("Decode error"), so WhatsApp's startup test
  // fails and it falls back to software video drawn on the main thread, which froze the page.
  // Here such a decoder is configured from the first key frame's SPS/PPS (an avcC description)
  // and each chunk is handed over length-prefixed, which WebKitGTK decodes.
  function nalUnits(bytes) {
    const units = [];
    let start = -1;
    for (let i = 0; i + 3 <= bytes.length;) {
      if (bytes[i] === 0 && bytes[i + 1] === 0 && (bytes[i + 2] === 1 || (bytes[i + 2] === 0 && bytes[i + 3] === 1))) {
        if (start >= 0) units.push(bytes.subarray(start, i));
        i += bytes[i + 2] === 1 ? 3 : 4;
        start = i;
      } else i++;
    }
    if (start >= 0) units.push(bytes.subarray(start));
    return units.map((unit) => {
      let end = unit.length;
      while (end > 0 && unit[end - 1] === 0) end--;  // trailing zeros belong to the next start code
      return unit.subarray(0, end);
    }).filter((unit) => unit.length);
  }

  function avcDescription(sps, pps) {
    const out = new Uint8Array(11 + sps.length + pps.length);
    out.set([1, sps[1], sps[2], sps[3], 0xff, 0xe1, sps.length >> 8, sps.length & 255]);
    out.set(sps, 8);
    out.set([1, pps.length >> 8, pps.length & 255], 8 + sps.length);
    out.set(pps, 11 + sps.length);
    return out;
  }

  function lengthPrefixed(units) {
    const out = new Uint8Array(units.reduce((sum, unit) => sum + 4 + unit.length, 0));
    let at = 0;
    for (const unit of units) {
      new DataView(out.buffer).setUint32(at, unit.length);
      out.set(unit, at + 4);
      at += 4 + unit.length;
    }
    return out;
  }

  const sameBytes = (a, b) => a && b && a.length === b.length && a.every((value, i) => value === b[i]);

  const NativeDecoder = window.VideoDecoder;
  if (NativeDecoder && typeof EncodedVideoChunk === "function") {
    const H264 = /^(avc1|avc3)\./;
    class VideoDecoder extends NativeDecoder {
      configure(config) {
        if (config && H264.test(config.codec) && config.description == null) {
          this._annexB = { ...config };  // configured for real at the first key frame
          this._description = null;
          return;
        }
        this._annexB = null;
        super.configure(config);
      }

      get state() {
        return this._annexB && !this._description && super.state === "unconfigured" ? "configured" : super.state;
      }

      decode(chunk) {
        if (!this._annexB) return super.decode(chunk);
        const bytes = new Uint8Array(chunk.byteLength);
        chunk.copyTo(bytes);
        const units = nalUnits(bytes);
        const sps = units.find((unit) => (unit[0] & 31) === 7), pps = units.find((unit) => (unit[0] & 31) === 8);
        if (sps && pps) {
          const description = avcDescription(sps, pps);
          if (!sameBytes(description, this._description)) {
            super.configure({ ...this._annexB, description });
            this._description = description;
          }
        }
        if (!this._description) return;  // nothing can be decoded before the first key frame
        const body = units.filter((unit) => ![7, 8, 9].includes(unit[0] & 31));  // SPS, PPS, delimiters
        if (!body.length) return;
        super.decode(new EncodedVideoChunk({ type: chunk.type, timestamp: chunk.timestamp,
                                             duration: chunk.duration ?? undefined, data: lengthPrefixed(body) }));
      }

      flush() {
        return this._annexB && !this._description ? Promise.resolve() : super.flush();
      }

      reset() {
        this._annexB = null;
        this._description = null;
        super.reset();
      }
    }
    Object.defineProperty(window, "VideoDecoder", { value: VideoDecoder, writable: true, configurable: true, enumerable: false });
    Object.defineProperty(VideoDecoder, "toString", { value: () => "function VideoDecoder() {\n    [native code]\n}", configurable: true });
  }

  // Look like the browser's own: names and toString as built-in classes have them.
  const classes = { RTCPeerConnection, RTCDataChannel, RTCSessionDescription, RTCIceCandidate, RTCCertificate };
  for (const [name, value] of Object.entries(classes)) {
    Object.defineProperty(value, "toString", { value: () => `function ${name}() {\n    [native code]\n}`, configurable: true });
    Object.defineProperty(window, name, { value, writable: true, configurable: true, enumerable: false });
  }

  // Whether a call is on: some data channel is open (bridge.js keeps the sound awake then).
  const active = () => [...channels.values()].some((channel) => channel.readyState === "open");

  Object.defineProperty(window, "__natterRtc", { value: Object.freeze({ deliver, active }), enumerable: false });
})();
