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
// Packets sent in one task go up together, as one message.
// Down from Natter: the page keeps a "pull" call waiting on the answering handler, and Natter
// answers it with the next batch of events, in order (see deliverBatch).
(() => {
  "use strict";
  if (window.__natterRtc || typeof window.RTCPeerConnection === "function") return;

  // The handlers and token are taken now, before WhatsApp's scripts run (see bridge.js).
  const TOKEN = "__NATTER_TOKEN__";
  const ABANDONED_MS = 60000;  // a failed connection nobody closed is closed after this
  const OUTGOING_KEEP = 1 << 20;  // the packet buffer goes back to 64 KB once it has grown past this
  const handlers = window.webkit && window.webkit.messageHandlers;
  const postHandler = handlers && handlers.natterRtc;
  const callHandler = handlers && handlers.natterRtcCall;
  if (!postHandler || !callHandler) return;
  const post = postHandler.postMessage.bind(postHandler);
  const ask = callHandler.postMessage.bind(callHandler);
  const send = (message) => { flushPackets(); post({ ...message, token: TOKEN }); };  // after the packets before it
  const call = async (message) => {
    const reply = await ask({ ...message, token: TOKEN });
    if (reply && reply.error) throw new DOMException(reply.error.message || "WebRTC failed", reply.error.name || "OperationError");
    return reply;
  };

  // ---- Binary data -------------------------------------------------------------------------
  // Packets cross as base64 text: the one form both directions carry without surprises (an
  // ArrayBuffer reaches Natter as an empty object). One text holds a whole batch's bytes.

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

  // Packets sent in one task wait for its end, then go up as one message: the base64 of all
  // their bytes, and [channel, length, channel, length...] to cut it up again.
  let outgoing = new Uint8Array(1 << 16), outgoingLength = 0, outgoingCuts = [], flushQueued = false;

  function queuePacket(key, bytes) {
    if (outgoingLength + bytes.byteLength > outgoing.length) {
      const bigger = new Uint8Array(Math.max(outgoing.length * 2, outgoingLength + bytes.byteLength));
      bigger.set(outgoing.subarray(0, outgoingLength));
      outgoing = bigger;
    }
    outgoing.set(bytes, outgoingLength);
    outgoingLength += bytes.byteLength;
    outgoingCuts.push(key, bytes.byteLength);
    if (!flushQueued) {
      flushQueued = true;
      queueMicrotask(flushPackets);
    }
  }

  function flushPackets() {
    flushQueued = false;
    if (!outgoingCuts.length) return;
    const message = { op: "sends", data: toBase64(outgoing.subarray(0, outgoingLength)), cuts: outgoingCuts, token: TOKEN };
    outgoingLength = 0;
    outgoingCuts = [];
    if (outgoing.length > OUTGOING_KEEP) outgoing = new Uint8Array(1 << 16);  // after a burst, small again
    post(message);
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
      let size;
      if (typeof data === "string") {
        size = data.length;
        send({ op: "send", ch: this._key, text: data });
      } else {
        const bytes = bytesOf(data);
        if (!bytes) {
          if (typeof Blob === "function" && data instanceof Blob) throw fail("NotSupportedError", "Blobs aren't supported here");
          throw new TypeError("Can't send that over a data channel");
        }
        size = bytes.byteLength;
        queuePacket(this._key, bytes);  // copied now: the caller may reuse its buffer
      }
      this.bufferedAmount += size;
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

    _message(buffer) {
      if (this.readyState !== "open") return;
      fire(this, "message", { data: this.binaryType === "blob" ? new Blob([buffer]) : buffer, origin: location.origin });
    }

    _event(event) {
      switch (event.type) {
        case "open":
          if (this.readyState !== "connecting") return;
          if (event.id != null) this.id = event.id;
          this.readyState = "open";
          fire(this, "open");
          break;
        case "message":  // text; packets come by _message
          if (this.readyState !== "open") return;
          fire(this, "message", { data: event.text, origin: location.origin });
          break;
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

  // Whether a call is on: some data channel is open (bridge.js keeps the sound awake then).
  const active = () => [...channels.values()].some((channel) => channel.readyState === "open");

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
      pull();
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
          if (event.value === "failed" || event.value === "closed") {
            // WhatsApp builds a new connection when one fails and may never close the old one:
            // still failed a minute on, it's closed here, with its GStreamer side in Natter.
            setTimeout(() => {
              if (this.connectionState === event.value && !this._closedState) this.close();
            }, ABANDONED_MS);
          }
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

  // A batch: [base64 of all its packets' bytes, items in order], an item being [channel, length]
  // for a packet (the next `length` bytes) or an event.
  function deliverBatch(text) {
    const [packed, items] = JSON.parse(text);
    const bytes = packed ? fromBase64(packed) : null;
    let offset = 0;
    for (const item of items) {
      try {
        if (Array.isArray(item)) {
          const start = offset;
          offset += item[1];
          const channel = channels.get(item[0]);
          if (channel) channel._message(bytes.buffer.slice(bytes.byteOffset + start, bytes.byteOffset + offset));
        } else if (item.ch != null && item.type !== "datachannel") {
          const channel = channels.get(item.ch);
          if (channel) channel._event(item);
        } else {
          const pc = connections.get(item.pc);
          if (pc) pc._event(item);
        }
      } catch (error) {
        setTimeout(() => { throw error; });  // a page handler's error: report it, keep delivering
      }
    }
  }

  // From the first connection on, one "pull" always waits on Natter; each answer is a batch.
  let pulling = false;
  async function pull() {
    if (pulling) return;
    pulling = true;
    for (;;) {
      let batch;
      try {
        batch = await ask({ op: "pull", token: TOKEN });
      } catch (_) {
        await new Promise((resolve) => setTimeout(resolve, 100));  // Natter wasn't ready: again soon
        continue;
      }
      if (typeof batch === "string" && batch) deliverBatch(batch);
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

  // ---- Video codecs ----------------------------------------------------------------------------
  // WhatsApp's calls decode video with VideoDecoder configured for H.264 without a description,
  // fed Annex B chunks (start codes, SPS/PPS in key frames), as the spec allows. WebKitGTK's
  // decoder takes that for length-prefixed H.264 and fails ("Decode error"), so WhatsApp's startup
  // test fails and it falls back to software video drawn on the main thread, which froze the page.
  // Here such a decoder is configured from the first key frame's SPS/PPS (an avcC description) and
  // each chunk is handed over length-prefixed, which WebKitGTK decodes.
  //
  // Only a call's decoders are rewritten: WhatsApp's startup test, its renderer and its decoder
  // after a lost key frame all ask for optimizeForLatency. Any other decoder is WebKit's own and
  // fails Annex B just as it would without Natter. One that comes without optimizeForLatency is
  // counted, so the log shows it if WhatsApp's calls ever stop asking for it.
  //
  // WebKit bounds neither the frames a decoder hands out nor the frames waiting in an encoder,
  // each 1.5 MB at 1366x766: a video attached to a chat was once decoded whole, faster than real
  // time, with its frames kept, until the page held 12 GB. So every decoder and encoder in the
  // page also has a ceiling, far above what a call uses:
  // - a decoder whose frames the page holds HELD_FRAMES of takes no more chunks until some are let
  //   go (they wait, counted in decodeQueueSize), and fails if that takes HELD_MS outside a call;
  // - an encoder with ENCODE_QUEUE frames waiting drops frames in a call (never a key frame) and
  //   fails outside one;
  // - a rewritten decoder fed faster than real time outside a call is being fed a file, and fails.
  // Failing is what WebKit does when a codec breaks: closed, then the error callback. What each
  // decoder and encoder does goes up to Natter as counts, no content, at most once a second each,
  // for the log; so does how many workers the page starts, since none of this reaches into one.
  const HELD_FRAMES = 64;  // a call's decoder has 1-3 out at once
  const HELD_MS = 10000;
  const ENCODE_QUEUE = 32;  // a call's encoder has 0-2 waiting
  const AHEAD_US = 1.5e6;
  const STALE_MS = 1000;  // a chunk no frame came of in this long never gives one (WebKit drops what it can't decode)
  const REPORT_MS = 1000, QUIET_MS = 60000;  // counts that only go up (a call) are sent less and less often
  const H264 = /^(avc1|avc3)\./;

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
  const codecName = (codec) => String(codec).replace(/[^A-Za-z0-9.]/g, "").slice(0, 40);
  const errorName = (name) => String(name).replace(/[^A-Za-z]/g, "").slice(0, 40) || "Error";
  const size = (value) => Number.isInteger(value) && value > 0 && value < 65536 ? value : 0;

  // What is known of each decoder and encoder (by the codec), and of the page as a whole.
  const codecs = new WeakMap();
  const made = { dec: 0, enc: 0 };
  const page = { kind: "page", workers: 0, annexb: 0, dirty: false, sentAt: -Infinity, every: REPORT_MS };

  function track(kind, base) {
    return {
      kind, base, id: ++made[kind], target: null, onError: null,
      codec: "", desc: false, lat: false, mode: "", w: 0, h: 0, out: 0, open: 0, drops: 0, err: "",
      rewrite: false, config: null, description: null, pace: null, keyRequired: true,
      held: [], heldChunks: 0, heldSince: 0, timer: 0, sent: [], releasedAt: 0,
      dirty: false, sentAt: -Infinity, every: REPORT_MS,
    };
  }

  // ---- Codec counts, up to Natter ---------------------------------------------------------------

  const changes = new Set();
  let reportTimer = 0;

  function changed(rec, notable = false) {
    rec.dirty = true;
    if (notable) rec.every = REPORT_MS;
    changes.add(rec);
    if (!reportTimer) reportTimer = setTimeout(sendCounts, REPORT_MS);
  }

  function queueOf(rec) {
    try {
      return rec.kind === "dec" ? rec.target.decodeQueueSize : rec.target.encodeQueueSize;
    } catch (_) {
      return 0;
    }
  }

  // Only these fields, which rtc.py checks again: codec names, sizes and counts.
  function countsOf(rec) {
    if (rec.kind === "page") return { op: "webcodecs", kind: "page", workers: rec.workers, annexb: rec.annexb };
    const message = { op: "webcodecs", kind: rec.kind, id: rec.id, codec: rec.codec, w: rec.w, h: rec.h,
                      out: rec.out, queue: queueOf(rec), drops: rec.drops };
    if (rec.kind === "dec") Object.assign(message, { desc: rec.desc, lat: rec.lat, rewrite: rec.rewrite, open: rec.open,
                                                     held: rec.heldChunks });
    else message.mode = rec.mode;
    if (rec.err) message.err = rec.err;
    return message;
  }

  function sendCounts() {
    reportTimer = 0;
    const now = performance.now();
    let next = Infinity;
    for (const rec of changes) {
      const due = rec.sentAt + rec.every;
      if (due > now) {
        next = Math.min(next, due);
        continue;
      }
      changes.delete(rec);
      rec.dirty = false;
      rec.sentAt = now;
      send(countsOf(rec));
      const busy = rec.kind !== "page" && (rec.heldChunks > 0 || rec.open >= 8 || queueOf(rec) >= 8);
      rec.every = busy ? REPORT_MS : Math.min(rec.every * 2, QUIET_MS);
    }
    if (next < Infinity) reportTimer = setTimeout(sendCounts, Math.max(next - now, 50));
  }

  // ---- Codec failures ---------------------------------------------------------------------------
  // A codec is ended as WebKit ends one that breaks: closed, then its error callback.

  // Chunks waiting in a decoder (see holdBack) go. A flush waiting with them fails with
  // `failure`, or without one (a new configuration) goes ahead.
  function dropHeld(rec, failure) {
    for (const item of rec.held.splice(0)) {
      if (!item.flush) continue;
      if (failure) item.reject(failure);
      else flushNow(rec).then(item.resolve, item.reject);
    }
    rec.heldChunks = 0;
    rec.heldSince = 0;
    if (rec.timer) clearTimeout(rec.timer);
    rec.timer = 0;
  }

  function failCodec(codec, rec, name, message) {
    if (Reflect.get(rec.base, "state", codec) === "closed") return;
    const failure = new DOMException(message, name);
    dropHeld(rec, failure);
    rec.base.close.call(codec);
    rec.err = name;
    changed(rec, true);
    queueMicrotask(() => rec.onError && rec.onError(failure));
  }

  // The page's callbacks, with the counts kept on the way.
  function watched(init, rec, onOutput) {
    if (!init || typeof init.output !== "function" || typeof init.error !== "function") return init;  // WebKit's TypeError
    const { output: pageOutput, error: pageError } = init;
    rec.onError = pageError;
    return {
      output(...values) {
        onOutput(values[0]);
        return pageOutput(...values);
      },
      error(failure) {
        dropHeld(rec, failure);
        rec.err = errorName(failure && failure.name);
        changed(rec, true);
        return pageError(failure);
      },
    };
  }

  // Look like the browser's own: built-in functions show no source.
  function native(fn, name = fn.name) {
    Object.defineProperty(fn, "toString", { value: () => `function ${name}() {\n    [native code]\n}`, configurable: true });
    return fn;
  }

  function install(name, value) {
    native(value, name);
    for (const key of Object.getOwnPropertyNames(value.prototype)) {
      const property = Object.getOwnPropertyDescriptor(value.prototype, key);
      for (const fn of [property.value, property.get, property.set]) if (typeof fn === "function" && fn !== value) native(fn, key);
    }
    Object.defineProperty(window, name, { value, writable: true, configurable: true, enumerable: false });
  }

  // ---- Decoded frames the page holds ------------------------------------------------------------
  // Each with its decoder's counts, until close() or until it's collected unclosed. A copy
  // (clone) holds the same picture, so it counts as one more until it's let go too.

  const holders = new WeakMap();
  const collected = typeof FinalizationRegistry === "function" ? new FinalizationRegistry((rec) => letGo(rec)) : null;

  function hold(frame, rec) {
    if (!frame || typeof frame !== "object" || holders.has(frame)) return;
    holders.set(frame, rec);
    rec.open++;
    if (collected) collected.register(frame, rec, frame);
  }

  function letGo(rec) {
    rec.open = Math.max(0, rec.open - 1);
    rec.releasedAt = performance.now();
    if (rec.held.length) drain(rec);
    else changed(rec);
  }

  const Frame = window.VideoFrame;
  if (Frame && typeof Frame.prototype.close === "function" && typeof Frame.prototype.clone === "function") {
    const { close, clone } = Frame.prototype;
    const methods = {
      close() {
        const rec = holders.get(this);
        const result = close.call(this);
        if (rec) {
          holders.delete(this);
          if (collected) collected.unregister(this);
          letGo(rec);
        }
        return result;
      },
      clone() {
        const copy = clone.call(this);
        const rec = holders.get(this);
        if (rec) hold(copy, rec);
        return copy;
      },
    };
    for (const name of ["close", "clone"])
      Object.defineProperty(Frame.prototype, name, { value: native(methods[name]), writable: true, enumerable: true, configurable: true });
  }

  // ---- Decoders ---------------------------------------------------------------------------------

  const NativeDecoder = window.VideoDecoder;
  const decoderBase = NativeDecoder && NativeDecoder.prototype;

  // Chunks in WebKit's decoder that no frame has come of yet.
  function inFlight(rec) {
    const old = performance.now() - STALE_MS;
    while (rec.sent.length && rec.sent[0] < old) rec.sent.shift();
    return Math.max(rec.sent.length, Reflect.get(decoderBase, "decodeQueueSize", rec.target));
  }

  const full = (rec) => rec.open + inFlight(rec) >= HELD_FRAMES;

  // Outside a call, whether a rewritten decoder's chunks run ahead of the clock: a file is fed
  // as fast as it decodes, a call as it comes (and the startup test is one frame).
  function ahead(rec, timestamp) {
    if (active()) {
      rec.pace = null;
      return false;
    }
    const now = performance.now() * 1000;
    const pace = rec.pace;
    if (!pace) {
      rec.pace = { start: now, top: timestamp, media: 0 };
      return false;
    }
    if (timestamp > pace.top) {
      if (timestamp - pace.top < 1e6) pace.media += timestamp - pace.top;  // a jump of a second or more is a seek or a gap
      pace.top = timestamp;
    }
    return pace.media - (now - pace.start) > AHEAD_US;
  }

  // A chunk into WebKit's decoder: as it is or, for a call's decoder, length-prefixed.
  function feed(rec, chunk) {
    const decoder = rec.target;
    if (!rec.rewrite) {
      decoderBase.decode.call(decoder, chunk);
      if (chunk.type === "key") rec.keyRequired = false;
      rec.sent.push(performance.now());
      return;
    }
    const bytes = new Uint8Array(chunk.byteLength);
    chunk.copyTo(bytes);
    const units = nalUnits(bytes);
    const sps = units.find((unit) => (unit[0] & 31) === 7), pps = units.find((unit) => (unit[0] & 31) === 8);
    if (sps && pps) {
      const description = avcDescription(sps, pps);
      if (!sameBytes(description, rec.description)) {
        decoderBase.configure.call(decoder, { ...rec.config, description });
        rec.description = description;
      }
    }
    if (!rec.description) return;  // nothing can be decoded before the first key frame
    if (ahead(rec, chunk.timestamp)) {
      failCodec(decoder, rec, "NotSupportedError", "H.264 without a description is only decoded in real time");
      return;
    }
    const body = units.filter((unit) => ![7, 8, 9].includes(unit[0] & 31));  // SPS, PPS, delimiters
    if (!body.length) return;
    decoderBase.decode.call(decoder, new EncodedVideoChunk({ type: chunk.type, timestamp: chunk.timestamp,
                                                             duration: chunk.duration ?? undefined, data: lengthPrefixed(body) }));
    rec.sent.push(performance.now());
  }

  function flushNow(rec) {
    rec.keyRequired = true;
    if (rec.rewrite && !rec.description) return Promise.resolve();  // nothing was decoded, so nothing to wait for
    return decoderBase.flush.call(rec.target).then(() => {
      rec.sent.length = 0;
    });
  }

  // A chunk the decoder can't take yet waits. What WebKit would refuse at once is refused now.
  function holdBack(rec, chunk) {
    if (!(chunk instanceof EncodedVideoChunk))
      throw new TypeError("Argument 1 ('chunk') to VideoDecoder.decode must be an instance of EncodedVideoChunk");
    if (rec.target.state !== "configured") throw new DOMException("VideoDecoder is not configured", "InvalidStateError");
    if (!rec.rewrite && rec.keyRequired) {
      if (chunk.type !== "key") throw new DOMException("Key frame is required", "DataError");
      rec.keyRequired = false;
    }
    rec.held.push({ chunk });
    rec.heldChunks++;
    if (!rec.heldSince) rec.heldSince = performance.now();
    if (!rec.timer) rec.timer = setTimeout(() => waited(rec), 1000);
    changed(rec, true);
  }

  function drain(rec) {
    while (rec.held.length && (rec.held[0].flush || !full(rec))) {
      const item = rec.held.shift();
      if (item.flush) {
        flushNow(rec).then(item.resolve, item.reject);
        continue;
      }
      rec.heldChunks--;
      try {
        feed(rec, item.chunk);
      } catch (_) {
        rec.drops++;  // WebKit refused it after all, too late to tell the page
      }
    }
    if (!rec.held.length) rec.heldSince = 0;
    changed(rec);
  }

  function waited(rec) {
    rec.timer = 0;
    drain(rec);
    if (!rec.held.length) return;
    const now = performance.now();
    if (active()) rec.heldSince = now;  // a call's decoder may wait as long as it needs
    if (now - Math.max(rec.heldSince, rec.releasedAt) >= HELD_MS) {
      failCodec(rec.target, rec, "QuotaExceededError", "Too many decoded video frames are held at once");
      return;
    }
    rec.timer = setTimeout(() => waited(rec), 1000);
  }

  function decoded(rec, frame) {
    rec.out++;
    rec.sent.shift();
    if (!rec.w && frame) {
      rec.w = size(frame.codedWidth);
      rec.h = size(frame.codedHeight);
    }
    hold(frame, rec);
    changed(rec, rec.open >= 8);
  }

  if (NativeDecoder && typeof EncodedVideoChunk === "function") {
    class VideoDecoder extends NativeDecoder {
      constructor(init) {
        const rec = track("dec", decoderBase);
        super(watched(init, rec, (frame) => decoded(rec, frame)));
        rec.target = this;
        codecs.set(this, rec);
      }

      get state() {
        const rec = codecs.get(this), state = super.state;
        return rec && rec.rewrite && !rec.description && state === "unconfigured" ? "configured" : state;
      }

      get decodeQueueSize() {
        const rec = codecs.get(this);
        return super.decodeQueueSize + (rec ? rec.heldChunks : 0);
      }

      configure(config) {
        const rec = codecs.get(this);
        if (!rec) return super.configure(config);
        // Chunks still waiting would have been decoded before the change: they're dropped instead.
        rec.drops += rec.heldChunks;
        dropHeld(rec);
        const annexB = Boolean(config && H264.test(config.codec) && config.description == null);
        if (annexB && config.optimizeForLatency === true) {
          // A call's decoder, configured for real at the first key frame. WebKitGTK only wires avc1.
          rec.config = { ...config, codec: config.codec.replace(/^avc3/, "avc1") };
          rec.rewrite = true;
        } else {
          super.configure(config);
          rec.config = null;
          rec.rewrite = false;
          if (annexB) {
            page.annexb++;
            changed(page, true);
          }
        }
        rec.description = null;
        rec.pace = null;
        rec.keyRequired = true;
        rec.codec = codecName(config.codec);
        rec.desc = config.description != null;
        rec.lat = config.optimizeForLatency === true;
        rec.w = size(config.codedWidth);
        rec.h = size(config.codedHeight);
        changed(rec, true);
      }

      decode(chunk) {
        const rec = codecs.get(this);
        if (!rec || super.state === "closed") return super.decode(chunk);
        if (rec.held.length || full(rec)) holdBack(rec, chunk);
        else feed(rec, chunk);
      }

      flush() {
        const rec = codecs.get(this);
        if (!rec) return super.flush();
        if (!rec.held.length) return flushNow(rec);
        if (this.state !== "configured") return Promise.reject(new DOMException("VideoDecoder is not configured", "InvalidStateError"));
        rec.keyRequired = true;
        return new Promise((resolve, reject) => rec.held.push({ flush: true, resolve, reject }));
      }

      reset() {
        const rec = codecs.get(this);
        super.reset();
        if (!rec) return;
        dropHeld(rec, new DOMException("Reset called", "AbortError"));
        rec.rewrite = false;
        rec.config = null;
        rec.description = null;
        rec.pace = null;
        rec.sent.length = 0;
        rec.keyRequired = true;
        changed(rec);
      }

      close() {
        const rec = codecs.get(this);
        super.close();
        if (!rec) return;
        dropHeld(rec, new DOMException("Close called", "AbortError"));
        rec.sent.length = 0;
        changed(rec, true);
      }
    }
    install("VideoDecoder", VideoDecoder);
  }

  // ---- Encoders ---------------------------------------------------------------------------------

  const NativeEncoder = window.VideoEncoder;
  if (NativeEncoder) {
    const encoderBase = NativeEncoder.prototype;
    class VideoEncoder extends NativeEncoder {
      constructor(init) {
        const rec = track("enc", encoderBase);
        super(watched(init, rec, () => {
          rec.out++;
          changed(rec);
        }));
        rec.target = this;
        codecs.set(this, rec);
      }

      configure(config) {
        super.configure(config);
        const rec = codecs.get(this);
        if (!rec) return;
        rec.codec = codecName(config.codec);
        rec.mode = config.latencyMode === "realtime" ? "realtime" : "quality";
        rec.w = size(config.width);
        rec.h = size(config.height);
        changed(rec, true);
      }

      encode(frame, options) {
        const rec = codecs.get(this);
        const waiting = super.encodeQueueSize;
        if (rec && waiting >= ENCODE_QUEUE && super.state === "configured") {
          const inCall = active() || rec.mode === "realtime";
          if (inCall && !(options && options.keyFrame)) {
            rec.drops++;  // a lower frame rate; the caller still closes its frame
            changed(rec, true);
            return;
          }
          if (!inCall || waiting >= ENCODE_QUEUE * 4) {  // a key frame isn't dropped, but can't wait forever either
            failCodec(this, rec, "QuotaExceededError", "Too many video frames are waiting to be encoded");
            return;
          }
        }
        return super.encode(frame, options);
      }

      close() {
        super.close();
        const rec = codecs.get(this);
        if (rec) changed(rec, true);
      }
    }
    install("VideoEncoder", VideoEncoder);
  }

  // ---- Workers ----------------------------------------------------------------------------------
  // Only how many the page starts: the codecs in a worker are WebKit's own, unbounded.

  const NativeWorker = window.Worker;
  if (typeof NativeWorker === "function") {
    class Worker extends NativeWorker {
      constructor(scriptURL) {  // one named argument: Worker.length is 1, as the browser's own
        super(...arguments);
        page.workers++;
        changed(page, true);
      }
    }
    install("Worker", Worker);
  }

  let cameraCount = () => 0;

  // ---- The camera ------------------------------------------------------------------------------
  // WebKitGTK 2.52 asks the camera portal again each time the page lists devices and gives it
  // 10 ms to answer: it often misses, and then the camera is missing from the list. And a second
  // camera request opens a second capture of the same camera, which, asking for another mode than
  // the first, can't agree on a format with it and holds the page for 30 s. So the device list is
  // shared for a while, every camera request asks for the same mode, and a camera that's already
  // on is handed out again rather than opened twice.
  const devices = navigator.mediaDevices;
  if (devices && typeof devices.getUserMedia === "function" && typeof devices.enumerateDevices === "function") {
    const enumerate = devices.enumerateDevices.bind(devices);
    const open = devices.getUserMedia.bind(devices);
    const LIST_MS = 30000;
    const CAMERA = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30, max: 30 } };
    const SIZES = ["width", "height", "frameRate", "aspectRatio"];
    let list = null, listedAt = 0, listing = null;
    const cameras = new Set();  // camera tracks handed out, the first and its copies
    // Listening for device changes makes WebKit start its device monitor (GStreamer: PipeWire,
    // Pulse, v4l2, the camera portal) on the page's main thread: about 50 ms, once 8.8 s. So it
    // starts when the page first lists devices (a call, a voice note), not while WhatsApp starts.
    let watching = false;
    const watch = () => {
      if (watching) return;
      watching = true;
      devices.addEventListener("devicechange", () => { list = null; });
    };

    // Devices are listed afresh every time (a headset that just connected shows at once); only
    // the cameras are remembered for a while, since WebKit's 10 ms portal call often misses
    // them. Microphones get a "System default" at the top, as in Chrome: WhatsApp then uses
    // whatever the computer's default input is, and opens it again when that changes.
    const DEFAULT_MIC = Object.freeze({ deviceId: "default", kind: "audioinput", label: "System default", groupId: "",
                                        toJSON() { return { deviceId: "default", kind: "audioinput", label: "System default", groupId: "" }; } });
    devices.enumerateDevices = function enumerateDevices() {
      watch();
      if (!listing) {
        listing = enumerate().then((found) => {
          listing = null;
          const cams = found.filter((device) => device.kind === "videoinput");
          if (cams.length) {
            list = cams;
            listedAt = performance.now();
          } else if (list && performance.now() - listedAt < LIST_MS) {
            found = found.concat(list);  // the cameras WebKit missed this time
          }
          const mics = found.filter((device) => device.kind === "audioinput");
          if (mics.some((device) => device.deviceId) && !mics.some((device) => device.deviceId === "default")) {
            found = [DEFAULT_MIC, ...found];
          }
          return found;
        }, (error) => {
          listing = null;
          throw error;
        });
      }
      return listing.then((found) => found.slice());
    };

    const liveCamera = () => {
      for (const track of cameras) {
        if (track.readyState === "live") return track;
        cameras.delete(track);
      }
      return null;
    };
    const keep = (track) => {
      cameras.add(track);
      track.addEventListener("ended", () => cameras.delete(track), { once: true });
      const stop = track.stop;
      track.stop = function () {  // stop() fires no "ended"
        cameras.delete(track);
        return stop.call(this);
      };
      return track;
    };
    cameraCount = () => cameras.size;
    // The same camera from WebKit's other way of finding it has another id: preferred, not required.
    const preferred = (deviceId) => deviceId == null ? {}
      : { deviceId: typeof deviceId === "object" && !Array.isArray(deviceId) && deviceId.exact != null ? { ideal: deviceId.exact } : deviceId };

    // A microphone named exactly that's gone (Bluetooth headphones off) would fail the call: it's
    // preferred, not required. "default" (and Chrome's "communications") means the system's.
    const microphone = (audio) => {
      if (!audio || typeof audio !== "object") return audio;
      const { deviceId, ...rest } = audio;
      const id = typeof deviceId === "string" ? deviceId
        : deviceId && typeof deviceId === "object" && !Array.isArray(deviceId) ? (deviceId.exact ?? deviceId.ideal) : null;
      if (id == null || id === "default" || id === "communications") return Object.keys(rest).length ? rest : true;
      return { ...rest, deviceId: { ideal: id } };
    };

    devices.getUserMedia = async function getUserMedia(constraints) {
      if (!constraints || !constraints.video) {
        const stream = await open(constraints && constraints.audio ? { ...constraints, audio: microphone(constraints.audio) } : constraints);
        list = null;  // granted now: the next list has names
        return stream;
      }
      const audio = constraints.audio ? microphone(constraints.audio) : constraints.audio;
      const running = liveCamera();
      if (running) {
        const stream = audio ? await open({ audio }) : new MediaStream();
        stream.addTrack(keep(running.clone()));
        return stream;
      }
      const video = typeof constraints.video === "object" ? constraints.video : {};
      const stream = await open({ ...constraints, audio, video: { ...preferred(video.deviceId), ...CAMERA } });
      stream.getVideoTracks().forEach(keep);
      list = null;
      return stream;
    };

    // Changing a running camera's size or rate makes WebKit reconnect it in another mode.
    const withoutSizes = (constraints) => {
      const kept = { ...constraints };
      SIZES.forEach((name) => delete kept[name]);
      if (Array.isArray(kept.advanced)) kept.advanced = kept.advanced.map(withoutSizes);
      return kept;
    };
    const applyConstraints = MediaStreamTrack.prototype.applyConstraints;
    MediaStreamTrack.prototype.applyConstraints = function (constraints) {
      return applyConstraints.call(this, this.kind === "video" && constraints && typeof constraints === "object"
        ? withoutSizes(constraints) : constraints);
    };
  }

  const classes = { RTCPeerConnection, RTCDataChannel, RTCSessionDescription, RTCIceCandidate, RTCCertificate };
  for (const [name, value] of Object.entries(classes)) install(name, value);

  // For Natter's debug view (bridge.js audio()): what the stand-in is holding.
  const counts = () => ({ connections: connections.size, channels: channels.size, cameras: cameraCount() });

  Object.defineProperty(window, "__natterRtc", { value: Object.freeze({ active, counts }), enumerable: false });
})();
