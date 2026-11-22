// Natter's bridge into WhatsApp Web. Runs before WhatsApp's own scripts, in the page.
//
// Up to Natter: notifications (with the sender's picture, opening the right chat), the chat list
// for Ctrl+K and the dock menu, which chat is open, whether WhatsApp wants a phone linked, clicks
// on the call buttons, and how WhatsApp looks (read only). Down from Natter: window.__natter.* calls.
(() => {
  "use strict";
  if (window.__natter) return;

  // Natter writes a fresh token here for each page and ignores messages without it, so nothing
  // else that can reach its message handler (a frame in the page, say) can talk to it. The
  // handler is taken now, before WhatsApp's scripts run, so they can't wrap it to read the token.
  const TOKEN = "__NATTER_TOKEN__";
  const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.natter;
  const send = handler ? handler.postMessage.bind(handler) : null;
  const post = (message) => {
    try { if (send) send({ ...message, token: TOKEN }); } catch (_) { /* not in Natter */ }
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const NAME_LIMIT = 200;
  // Bidi controls and invisible marks: Natter drops them from what the page tells it (INVISIBLE in
  // account.py), so they're dropped from names here too, or the names it hands back wouldn't match.
  const INVISIBLE = /[\u200B\u200E\u200F\u202A-\u202E\u2060\u2066-\u2069\uFEFF]/g;
  const plainName = (text) => text.replace(INVISIBLE, "").trim().slice(0, NAME_LIMIT);

  // ---- Notifications ------------------------------------------------------------------------
  // WhatsApp's `new Notification(...)` lands here and is shown by Natter, so a click can come
  // back to the same object and run WhatsApp's own handler, which opens that chat.

  const shown = new Map();
  // Those closed since (WhatsApp closes them once their chat is read), as many: a reply to one
  // still clicks it, and checks the open chat against what it said (sameChat). Each with when it
  // was closed (closedAt, the page's clock): one closed after the reply was asked counts as shown.
  const closed = new Map();
  // Each page load starts somewhere new, so a notification from before a reload can't be taken for
  // a newer one with the same id (by a click on it, say). Natter takes ids below 2**31: room for 2**30.
  let nextId = 1 + Math.floor(Math.random() * 2 ** 30);
  const ICON_TYPES = ["image/jpeg", "image/png", "image/webp"];

  async function iconData(url) {
    if (!url) return "";
    if (/^https:/i.test(url)) {
      // Natter fetches these itself (the picture host sends no CORS headers): WhatsApp's only.
      try {
        const parsed = new URL(url);
        return parsed.hostname.endsWith(".whatsapp.net") && !parsed.username && !parsed.password ? parsed.href : "";
      } catch (_) {
        return "";
      }
    }
    if (!/^(blob|data):/.test(url)) return "";
    try {
      const blob = await (await fetch(url)).blob();
      if (blob.size > 2e6 || !ICON_TYPES.includes(blob.type)) return "";
      return await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => resolve("");
        reader.readAsDataURL(blob);
      });
    } catch (_) {
      return "";
    }
  }

  class NatterNotification extends EventTarget {
    constructor(title, options = {}) {
      super();
      this.id = nextId++;
      this.title = String(title ?? "");
      this.body = String(options.body ?? "");
      this.tag = String(options.tag ?? "");
      this.icon = options.icon ? String(options.icon) : "";
      this.silent = Boolean(options.silent);
      this.data = options.data ?? null;
      this.onclick = this.onclose = this.onshow = this.onerror = null;
      shown.set(this.id, this);
      if (shown.size > 300) shown.delete(shown.keys().next().value);
      iconData(this.icon).then((icon) => post({
        type: "notify", id: this.id, title: this.title, body: this.body, tag: this.tag, icon,
      }));
      setTimeout(() => this._fire("show"), 0);
    }
    _fire(type) {
      const event = new Event(type, { cancelable: true });
      this.dispatchEvent(event);
      const handler = this["on" + type];
      if (typeof handler === "function") handler.call(this, event);
      return event;
    }
    close() {
      if (shown.delete(this.id)) {
        post({ type: "unnotify", id: this.id });
        this.closedAt = performance.now();
        closed.set(this.id, this);
        if (closed.size > 300) closed.delete(closed.keys().next().value);
      }
      this._fire("close");
    }
    static get permission() { return "granted"; }
    static requestPermission(callback) {
      if (typeof callback === "function") callback("granted");
      return Promise.resolve("granted");
    }
    static get maxActions() { return 0; }
  }
  Object.defineProperty(window, "Notification", { value: NatterNotification, writable: true, configurable: true });

  // One WhatsApp closed since is clicked too: its handler, if it still acts, opens that chat.
  function clickNotification(id) {
    const notification = shown.get(id) || closed.get(id);
    if (!notification) return false;
    notification._fire("click");
    return true;
  }

  // ---- Idle audio ---------------------------------------------------------------------------
  // WhatsApp keeps a Web Audio context open. In WebKitGTK an open context keeps a GStreamer
  // pipeline running, a few percent of CPU all day, so Natter suspends each context once nothing
  // has played through it for a while, and resumes it before anything plays. What counts is
  // what's live now (a sound not yet ended, a microphone track still on), not a tally that a
  // missed event would leave stuck.

  // Contexts are held weakly, so a closed one the page lets go can be collected (WebKit 2.52 keeps an
  // open one alive however the page drops it).
  const contexts = new Set();  // WeakRefs to AudioContexts
  const rested = new WeakSet();  // contexts Natter suspended (the only ones its play() wakes)
  const sources = new Set();  // started source nodes that haven't ended
  const streams = new Set();  // microphone streams (voice notes, calls) feeding a context
  const elements = new Set();  // <audio>/<video> played through a context
  const routed = new WeakMap();  // each of those → the context it plays through
  const started = new Set();  // elements the page started, until they stop: WhatsApp's own `new Audio()`s too
  let lastStart = 0;
  const IDLE_MS = 30000;  // long enough that a burst of message sounds doesn't cycle the pipeline
  const WAKE_AGAIN_MS = 1000;
  const lastRested = new WeakMap();  // each context → when Natter last suspended it
  let onWakeFailed = null;  // set by "Sound that can't be heard", for its log line

  // A resume() that fails is tried once more. For a context Natter rested, a second failure is
  // told too: the page never suspended that one itself, so it wouldn't find out.
  function wake(context) {
    if (!context) return;
    const ours = rested.has(context);
    rested.delete(context);
    if (context.state !== "suspended") return;
    context.resume().catch(() => {
      setTimeout(() => {
        if (context.state !== "suspended") return;
        context.resume().catch((error) => { if (ours && onWakeFailed) onWakeFailed(error); });
      }, WAKE_AGAIN_MS);
    });
  }

  function eachContext(visit) {
    for (const ref of contexts) {
      const context = ref.deref();
      if (!context || context.state === "closed") contexts.delete(ref);
      else visit(context);
    }
  }

  function playingSources() {
    sources.forEach((node) => { if (node.context.state === "closed") sources.delete(node); });
    return sources.size;
  }

  function liveStreams() {
    streams.forEach((stream) => {
      if (!stream.getTracks().some((track) => track.readyState === "live")) streams.delete(stream);
    });
    return streams.size;
  }

  // A media element done playing for now: paused, ended, or failed to load (WebKit fires no
  // "pause" then, and paused stays false).
  const stopped = (m) => m.paused || m.ended || Boolean(m.error);

  function mediaPlaying() {
    // Elements no longer in the page and done playing go: each holds its context and a
    // GStreamer player with its decoded media.
    elements.forEach((element) => {
      if (!element.isConnected && stopped(element)) elements.delete(element);
    });
    return [...elements, ...document.querySelectorAll("audio, video")].some((m) => !stopped(m));
  }

  function playingStarted() {
    started.forEach((element) => { if (stopped(element)) started.delete(element); });
    return started.size;
  }

  // A call's sound goes through audio worklets, which have no start or end to watch: while
  // Natter's data channels (rtc.js) are open, a call is on and nothing rests.
  function calling() {
    try { return Boolean(window.__natterRtc && window.__natterRtc.active()); } catch (_) { return false; }
  }

  // Contexts carrying a call's sound: WhatsApp's call worklets ("voip-…" processors) run in them.
  const callContexts = new WeakSet();

  // Sound a page decodes itself plays through a worklet (not a call's) or a script processor,
  // for as long as it's connected, with no start or end to watch. Such a node keeps its context
  // up while it's connected (a script processor only with onaudioprocess set or an audioprocess
  // listener), and connecting one, or giving a script processor its handler, wakes a context
  // Natter suspended, as a start does. One fed only by microphone streams that have all ended (a
  // recorder, paused or done) holds its context only RECORDER_HOLD_MS more, connected or not
  // (processing()). Held weakly: one the page lets go is collected, and goes from here.
  const processors = new Set();  // WeakRefs to those nodes
  const outputs = new WeakMap();  // each → what it's connected to now
  const scripts = new WeakSet();  // which of them are script processors
  const listening = new WeakMap();  // a script processor → its audioprocess listeners
  const fedBy = new WeakMap();  // each → the microphone streams connected into it
  const quietSince = new WeakMap();  // a recorder → when its microphone streams were first seen ended
  // A recorder whose microphone stopped (a voice note paused, or done) holds its context this
  // long: see processing().
  const RECORDER_HOLD_MS = 10 * 60000;
  let onProcessorConnected = null;  // set by "Sound that can't be heard", for its log line
  let onRecorderResumed = null;  // the same, for a recorder fed again after a pause
  let onRecorderFed = null;  // the same, for how each recording into a script processor went
  let onRecorderPath = null;  // the same, for which kind of script processor the page got, and why
  // Natter's worklet recorder ("A recorder a late main thread can't break"): { spare(args),
  // fresh(context, args), rested(context), make(context, args) }; the script processors it made; the
  // audio tracks the page's getUserMedia handed it, until they end; and WebKit's AudioWorkletNode for
  // it, as it was before the idle audio wrapped it (below).
  let recorderShim = null;
  const shims = new WeakSet();
  const microphoneTracks = new Set();
  const nativeWorkletNode = window.AudioWorkletNode;
  // Makes a context as the page's `new AudioContext()` would, but not the page's (yet): the worklet
  // recorder's spares. Set below, once the idle audio has its own AudioContext.
  let spareContext = null;
  const starting = new WeakSet();  // spares just handed to the page, until they start (or it suspends or closes one)

  // Something starts to play through a processor: as a start does.
  function processorBusy(node) {
    lastStart = performance.now();
    wake(node.context);
  }

  function trackProcessor(node, script) {
    if (!node || typeof node.connect !== "function" || typeof node.disconnect !== "function") return;
    outputs.set(node, new Set());
    fedBy.set(node, new Set());
    processors.add(new WeakRef(node));
    const connect = node.connect;
    const disconnect = node.disconnect;
    node.connect = function (destination, ...rest) {
      const result = connect.call(this, destination, ...rest);
      const out = outputs.get(this);
      if (out && destination) {
        out.add(destination);
        processorBusy(this);
        if (onProcessorConnected) onProcessorConnected(this, scripts.has(this));
      }
      return result;
    };
    node.disconnect = function (...args) {
      const result = disconnect.apply(this, args);
      const out = outputs.get(this);
      // disconnect() or disconnect(output) cuts every connection; disconnect(node, …) that one.
      if (out) { if (args.length && typeof args[0] === "object" && args[0]) out.delete(args[0]); else out.clear(); }
      return result;
    };
    if (script) {
      scripts.add(node);
      listening.set(node, new Map());
      // onaudioprocess stays the engine's own (on the prototype); setting it here wakes too.
      let handler;
      for (let on = node; on && !handler; on = Object.getPrototypeOf(on)) {
        handler = Object.getOwnPropertyDescriptor(on, "onaudioprocess");
      }
      let own = handler && "value" in handler ? handler.value : undefined;
      try {
        Object.defineProperty(node, "onaudioprocess", {
          configurable: true, enumerable: true,
          get() { return handler && handler.get ? handler.get.call(this) : own; },
          set(value) {
            if (handler && handler.set) handler.set.call(this, value); else own = value;
            if (typeof value === "function") processorBusy(this);
          },
        });
      } catch (_) { /* left as it was: only a set handler isn't seen waking */ }
      const add = node.addEventListener;
      const remove = node.removeEventListener;
      if (typeof add === "function" && typeof remove === "function") {
        // As the DOM counts them: a listener once for each capture flag; `once` ones stay counted.
        const capture = (options) => Boolean(typeof options === "object" && options ? options.capture : options);
        node.addEventListener = function (type, listener, options) {
          const map = listening.get(this);
          if (type === "audioprocess" && listener && map) {
            if (!map.has(listener)) map.set(listener, new Set());
            map.get(listener).add(capture(options));
            processorBusy(this);
          }
          return add.apply(this, arguments);
        };
        node.removeEventListener = function (type, listener, options) {
          const map = listening.get(this);
          const flags = type === "audioprocess" && map && map.get(listener);
          if (flags) {
            flags.delete(capture(options));
            if (!flags.size) map.delete(listener);
          }
          return remove.apply(this, arguments);
        };
      }
    }
  }

  // A microphone source connected into a processor: a recorder's. Once all such streams have
  // ended, the processor holds its context only a while (processing()). One fed again after
  // that is a voice note resumed after a pause, or the next one recorded into the same recorder.
  function feedFrom(source, stream) {
    if (!source || typeof source.connect !== "function" || !stream) return;
    const connect = source.connect;
    source.connect = function (destination, ...rest) {
      const result = connect.call(this, destination, ...rest);
      const feeds = destination && fedBy.get(destination);
      if (feeds && !feeds.has(stream)) {
        const again = feeds.size > 0 && [...feeds].every(ended);
        const quiet = quietSince.get(destination);
        feeds.add(stream);
        quietSince.delete(destination);
        if (again && onRecorderResumed) onRecorderResumed(destination, quiet);
        if (onRecorderFed && scripts.has(destination)) onRecorderFed(destination, stream);
      }
      return result;
    };
  }

  const ended = (stream) => {
    try { return !stream.getTracks().some((track) => track.readyState === "live"); } catch (_) { return false; }
  };

  // The contexts a connected processor holds up now. A recorder whose microphone streams have all
  // ended holds its own for RECORDER_HOLD_MS from then, connected or not: paused, WhatsApp records
  // on into the same context, and one Natter rested meanwhile would open its output stream again
  // right as the microphone opens again (when that's a Bluetooth headset, as it switches to its
  // headset profile); an output stream that fails to open then stops the context's clock, and the
  // recording with it. So a paused voice note's context runs on as it would without Natter.
  function processing() {
    const busy = new Set();
    const now = performance.now();
    for (const ref of processors) {
      const node = ref.deref();
      if (!node || !node.context || node.context.state === "closed") { processors.delete(ref); continue; }
      const feeds = fedBy.get(node);
      if (feeds && feeds.size && [...feeds].every(ended)) {  // a recorder, stopped
        if (!quietSince.has(node)) quietSince.set(node, now);
        if (now - quietSince.get(node) < RECORDER_HOLD_MS) busy.add(node.context);
        continue;
      }
      const out = outputs.get(node);
      if (!out || !out.size) continue;
      if (scripts.has(node) && typeof node.onaudioprocess !== "function" && !listening.get(node).size) continue;
      busy.add(node.context);
    }
    return busy;
  }

  // A context rested here, once suspended, may take the worklet recorder's module (recorderShim.rested):
  // only then, as WebKit restarts a playing context's sound output for it. One that couldn't then (a
  // microphone on) may at a later look.
  function rest() {
    if (performance.now() - lastStart < IDLE_MS || playingSources() || liveStreams() || mediaPlaying() || calling()) return;
    const busy = processing();
    const loadRested = (context) => {
      if (!recorderShim) return;
      try { recorderShim.rested(context); } catch (_) { /* WebKit's own script processors there, as before */ }
    };
    eachContext((context) => {
      if (context.state === "running" && !callContexts.has(context) && !busy.has(context)) {
        rested.add(context);
        lastRested.set(context, performance.now());
        context.suspend().then(() => loadRested(context), () => {});
      } else if (context.state === "suspended" && rested.has(context)) loadRested(context);
    });
  }

  const Context = window.AudioContext || window.webkitAudioContext;
  if (Context) {
    function adopt(context) {
      const ref = new WeakRef(context);
      contexts.add(ref);
      context.addEventListener("statechange", () => { if (context.state === "closed") contexts.delete(ref); });
    }
    // A spare handed over starts as WebKit's own new context does: in a task of its own after the
    // page's (WebKit's constructor posts one), so what the page does to it first in its microtasks
    // (suspend() or close() it) comes first, and it doesn't start then. The task is a MessageChannel's,
    // made now, before the page's scripts run: a hidden page holds timers back, not these.
    const handedOver = [];  // spares handed over, until that task
    const startHandedOver = () => handedOver.splice(0).forEach((spare) => {
      if (starting.delete(spare)) wake(spare);
    });
    let startTask = () => setTimeout(startHandedOver, 0);  // where there's no MessageChannel
    try {
      const channel = new MessageChannel();
      channel.port1.onmessage = startHandedOver;
      startTask = channel.port2.postMessage.bind(channel.port2, null);
    } catch (_) { /* a timer's task, then */ }
    const Tracked = class extends Context {
      // A new context like a spare the worklet recorder has ready (its module loaded) is that spare
      // (recorderShim.spare): it starts as a new one would, a moment on, unless the page suspends or
      // closes it first. Any other is WebKit's new one, as before (recorderShim.fresh). Not for a
      // page's own subclass: its contexts are its own.
      constructor(...args) {
        let spare = null;
        if (recorderShim && new.target === Tracked) {
          try { spare = recorderShim.spare(args); } catch (_) { spare = null; }
        }
        if (spare) {
          adopt(spare);
          starting.add(spare);
          handedOver.push(spare);
          startTask();
          return spare;
        }
        super(...args);
        adopt(this);
        if (recorderShim) {
          try { recorderShim.fresh(this, args); } catch (_) { /* WebKit's own script processors, as before */ }
        }
      }
      suspend(...args) {
        starting.delete(this);
        return super.suspend(...args);
      }
      // A closing context stays as it was until it has closed: resumed meanwhile, it would open its output.
      close(...args) {
        starting.delete(this);
        return super.close(...args);
      }
      createMediaStreamSource(stream) {
        streams.add(stream);
        lastStart = performance.now();
        wake(this);
        const source = super.createMediaStreamSource(stream);
        feedFrom(source, stream);
        return source;
      }
      createMediaElementSource(element) {
        throughContext(this, element);
        return super.createMediaElementSource(element);
      }
    };
    // An element played through a context: the context wakes now and at each play, and the
    // element counts as playing (mediaPlaying) while it does, wherever it is.
    function throughContext(context, element) {
      if (!context || !element || typeof element.addEventListener !== "function") return;
      elements.add(element);
      routed.set(element, context);
      wake(context);
      element.addEventListener("play", () => wake(context));
    }
    // Sound a page decodes itself (a wasm decoder) plays through a script processor or a worklet,
    // which have no start() to watch: making one wakes a context Natter suspended, as a start
    // does, and so does connecting it (trackProcessor); while it's connected rest() leaves its
    // context be (processing()). Natter's worklet recorder (recorderShim) counts as one, as WebKit's.
    const scriptProcessor = Context.prototype.createScriptProcessor;
    if (typeof scriptProcessor === "function") {
      Tracked.prototype.createScriptProcessor = function (...args) {
        lastStart = performance.now();
        wake(this);
        let made = { why: "none" };
        if (recorderShim) {
          try { made = recorderShim.make(this, args); } catch (_) { made = { why: "failed" }; }
        }
        const node = made.node || scriptProcessor.apply(this, args);
        trackProcessor(node, true);
        if (onRecorderPath) onRecorderPath(made.node ? "worklet" : "native", made.why, made.kind || "", made.name || "");
        return node;
      };
    }
    window.AudioContext = Tracked;
    if (window.webkitAudioContext) window.webkitAudioContext = Tracked;
    spareContext = () => Reflect.construct(Context, [], Tracked);
    const Worklet = window.AudioWorkletNode;
    if (Worklet) {
      window.AudioWorkletNode = class AudioWorkletNode extends Worklet {
        constructor(context, name, options) {
          super(context, name, options);
          if (/voip/i.test(String(name))) callContexts.add(context);  // a call's: never rested
          else {
            lastStart = performance.now();
            trackProcessor(this, false);
          }
          wake(context);
        }
      };
    }
    // The constructor form of createMediaElementSource.
    const ElementSource = window.MediaElementAudioSourceNode;
    if (ElementSource) {
      window.MediaElementAudioSourceNode = class MediaElementAudioSourceNode extends ElementSource {
        constructor(context, options) {
          throughContext(context, options && options.mediaElement);
          super(context, options);
        }
        // What createMediaElementSource() makes is one too.
        static [Symbol.hasInstance](node) { return node instanceof ElementSource; }
      };
    }
    const StreamSource = window.MediaStreamAudioSourceNode;
    if (StreamSource) {
      window.MediaStreamAudioSourceNode = class extends StreamSource {
        constructor(context, options) {
          if (options && options.mediaStream) {
            streams.add(options.mediaStream);
            lastStart = performance.now();
            wake(context);
          }
          super(context, options);
          if (options && options.mediaStream) feedFrom(this, options.mediaStream);
        }
      };
    }

    // AudioBufferSourceNode has a start() of its own in some engines, so each kind is patched.
    for (const Node of [window.AudioScheduledSourceNode, window.AudioBufferSourceNode,
                        window.OscillatorNode, window.ConstantSourceNode]) {
      if (!Node || !Object.prototype.hasOwnProperty.call(Node.prototype, "start")) continue;
      const start = Node.prototype.start;
      Node.prototype.start = function (...args) {
        lastStart = performance.now();
        wake(this.context);
        const result = start.apply(this, args);
        if (!sources.has(this)) {
          sources.add(this);
          this.addEventListener("ended", () => sources.delete(this), { once: true });
          // A one-shot sound silenced by disconnect() rather than stop() never ends: it goes
          // once it would have, so it can't keep the contexts awake for good.
          if (this.buffer && !this.loop) setTimeout(() => sources.delete(this), this.buffer.duration * 1000 + 2000);
        }
        return result;
      };
    }
    // WebKitGTK puts up a media player (MPRIS) in the desktop's media controls for an <audio> as
    // it plays, and takes it down only once the element is gone or another sound in the page
    // starts: only the element that last began to play has one (one play()ed before its data
    // came begins when it comes, which play()'s promise tells). WhatsApp keeps its elements to play
    // again, so the last voice note's or tone's player stayed there, paused, for good. So once that
    // element stops and nothing else plays, no microphone is on and no call is, a context's
    // resume() counts as a sound starting, which takes the player down, and plays nothing: a
    // running context goes on as it was, one Natter rested rests again. How soon:
    // - RELEASE_ENDED_MS after it ends, a looping one (a ringtone) is paused, or its source changes;
    // - RELEASE_PAUSED_MS after a pause made in the page (the person's click or key just before,
    //   not typing in a text box), to carry on from the desktop's controls;
    // - RELEASE_HELD_MS after a pause from outside it: those controls, or an app that paused it for
    //   its own sound and plays it again after (Handset does, for a phone, within 30 minutes).
    // One due while anything else plays, a microphone is on or a call is (a ringtone paused for the
    // call) is owed, and tried again with the idle check. A context the page suspended itself is
    // left alone; with none to resume, the player stays.
    const RELEASE_ENDED_MS = 2000;
    const RELEASE_PAUSED_MS = 120000;
    const RELEASE_HELD_MS = 30 * 60000;
    const INPUT_MS = 1500;  // a pause this soon after the person's click or key is the page's
    const stopping = new WeakSet();  // elements whose stopping is watched
    let primary = null;  // WeakRef: the element that last began to play, the one with a player
    let releaseRound = 0;  // a later start or stop makes an earlier timer count for nothing
    let owed = false;  // a release came due while something was on
    let lastInput = -Infinity;
    if (typeof window.addEventListener === "function") {
      for (const type of ["pointerup", "click", "keydown"]) {
        // Only the person's: Natter's own presses (press()) aren't trusted, and typing in the
        // message box or search pauses nothing.
        window.addEventListener(type, (event) => {
          const target = event.target;
          if (event.isTrusted && !(target && (target.isContentEditable || /^(INPUT|TEXTAREA)$/.test(target.tagName)))) {
            lastInput = performance.now();
          }
        }, true);
      }
    }

    function began(element) {
      started.add(element);
      primary = new WeakRef(element);
      releaseRound++;  // nothing is left over now
      owed = false;
    }

    function releaseLater(element, ms) {
      started.delete(element);
      const last = primary && primary.deref();
      if (last && last !== element) return;  // another started since: this one's player went then
      const round = ++releaseRound;
      setTimeout(() => { if (round === releaseRound) release(); }, ms);
    }

    function pauseDelay(element) {
      if (element.ended || element.loop) return RELEASE_ENDED_MS;
      return performance.now() - lastInput <= INPUT_MS ? RELEASE_PAUSED_MS : RELEASE_HELD_MS;
    }

    function release() {
      owed = false;
      playingStarted();
      // WebKit gives a muted element no player, nor one with no source (NETWORK_EMPTY: it never
      // plays): either goes on beside it unheeded.
      const holds = (m) => !stopped(m) && !m.muted && m.networkState !== 0;
      if ([...started, ...elements, ...document.querySelectorAll("audio, video")].some(holds)
          || liveStreams() || calling()) {
        owed = true;
        return;
      }
      let running = null, resting = null;
      eachContext((context) => {
        if (context.state === "running") running = running || context;
        else if (context.state === "suspended" && rested.has(context)) resting = resting || context;
      });
      if (running) {
        running.resume().catch(() => {});
        return;
      }
      if (!resting) return;
      const since = lastStart;
      resting.resume().then(() => {
        // Back to rest, unless something started meanwhile: then it stays up for that.
        if (lastStart === since && rested.has(resting) && resting.state === "running") resting.suspend().catch(() => {});
      }, () => {});
    }

    function watchStopping(element) {
      if (stopping.has(element) || typeof element.addEventListener !== "function") return;
      stopping.add(element);
      // "play" also when the desktop's controls play it again, no play() called.
      element.addEventListener("play", () => began(element));
      element.addEventListener("pause", () => releaseLater(element, pauseDelay(element)));
      // "ended" comes after "pause", and after the page's own listeners: one that rewinds the
      // element has made `ended` false by then.
      element.addEventListener("ended", () => releaseLater(element, RELEASE_ENDED_MS));
      // A new source, or load(), stops it without a "pause".
      element.addEventListener("emptied", () => releaseLater(element, RELEASE_ENDED_MS));
    }

    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
      lastStart = performance.now();
      // An element played through a context (createMediaElementSource) needs that one running
      // first; one played from the element itself (WhatsApp's voice messages) needs none. Waking
      // every context Natter rested held the page while each opened its sound stream again,
      // just as the element opened its own, and a voice message resumed after a pause could then
      // stay stopped, its time standing still.
      const via = routed.get(this);
      if (via && rested.has(via)) wake(via);
      watchStopping(this);
      const result = play.apply(this, args);
      if (!this.paused) began(this);
      // Its promise settles as WebKit makes it the one with the player: at once, or when its data
      // comes, taking the player back from one started meanwhile.
      if (result && typeof result.then === "function") result.then(() => { if (!this.paused) began(this); }, () => {});
      return result;
    };
    setInterval(() => {
      if (owed) release();
      rest();
    }, IDLE_MS);
  }

  // For Natter's debug info and log (audiosizes.py): what's live, and how much each set and map
  // the bridge keeps here holds, once the same pruning rest() does has run, so one that grows
  // without end shows. Counts only: no names, no URLs, nothing of what's in them.
  function audio() {
    const states = [];
    let restedCount = 0;
    let callCount = 0;
    eachContext((context) => {
      states.push(context.state);
      if (rested.has(context)) restedCount++;
      if (callContexts.has(context)) callCount++;
    });
    const playing = playingSources();
    const live = liveStreams();
    const media = mediaPlaying();  // after it, elements holds only what it keeps
    const playingNow = playingStarted();
    const held = processing().size;  // contexts processing() holds up: a connected worklet or script processor, or a recorder lately stopped
    return { contexts: states, sources: playing, streams: live, media, elements: elements.size,
             idle: Math.round(performance.now() - lastStart),
             rtc: window.__natterRtc ? window.__natterRtc.counts() : null,
             sizes: { contexts: states.length, rested: restedCount, calls: callCount, elements: elements.size,
                      sources: playing, streams: live, held, notifications: shown.size, started: playingNow } };
  }

  // ---- Videos aren't pictures ---------------------------------------------------------------
  // Once an attachment is ready, WhatsApp loads its full preview into an Image to pick a
  // background colour, and for a video (sent as a video or as a document) that's a blob: URL of
  // the whole file. WebKitGTK decodes a video given to an <img> as an animated image: every frame
  // at once, all kept (over 8 GB for a 96 s screen recording), on the page's main thread. So an
  // <img> never gets a blob of a video: it gets an empty data URL, which fails to load the way any
  // undecodable picture does. <video> elements still get the blob.
  {
    const VIDEO_URLS_KEPT = 256;  // URLs WhatsApp never revokes don't pile up
    const videoUrls = new Set();
    const createObjectURL = URL.createObjectURL;
    const revokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = function (object) {
      const url = createObjectURL.call(this, object);
      if (object instanceof Blob && /^video\//i.test(object.type)) {
        videoUrls.add(url);
        if (videoUrls.size > VIDEO_URLS_KEPT) videoUrls.delete(videoUrls.values().next().value);
      }
      return url;
    };
    URL.revokeObjectURL = function (url) {
      videoUrls.delete(String(url));
      return revokeObjectURL.call(this, url);
    };
    const src = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
    if (src && src.set) {
      Object.defineProperty(HTMLImageElement.prototype, "src", {
        ...src,
        set(value) { src.set.call(this, videoUrls.has(String(value)) ? "data:," : value); },
      });
    }
  }

  // ---- Where things are in WhatsApp's page ----------------------------------------------------
  // Every WhatsApp selector Natter leans on, in one table, likeliest candidate first. A redesign
  // breaks these before anything else; canary() checks them against the live page once the chat
  // list is up, so a layout change turns up in Natter's log by name instead of as features
  // quietly gone.

  const WHERE = {
    "app": ["#app"],                                       // everything WhatsApp draws
    "pane": ["#pane-side"],                                // the chat list
    "row": ['[role="listitem"]', '[role="row"]'],          // one chat in the list
    // A chat's name in its row, read by rowName alone (never find), as the canary judges it: the
    // title cell's titled span, else that cell's words (its span[dir=auto]'s, else all of it); in a
    // row with no title cell, a titled span, with rowName's rules: another one holds the last message.
    "row-name": ['[data-testid="cell-frame-title"] span[title]', '[data-testid="cell-frame-title"] [dir="auto"]',
                 '[data-testid="cell-frame-title"]', "span[title]"],
    // Not judged by the canary: what tells a chat's row (chatLike), the Archived row's icon (a row
    // with it and no chat's cell is the list's own), and the parts of a row that are never its name:
    // its second line (the last message, a draft), its status and its time; and only where its
    // title cell is gone, a community's name over a group's and a label.
    "row-cell": ['[data-testid="cell-frame-container"]', '[data-testid="cell-frame-title"]'],
    "row-own": ['[data-icon^="archive"]'],
    "row-other": ['[data-testid="cell-frame-secondary"]', '[data-testid="last-msg-status"]',
                  '[data-testid="chat-list-timestamp"]'],
    "row-beside": ['[data-testid="subgroup-identity"]', '[data-testid="cell-frame-label"]'],
    "row-selected": ['[aria-selected="true"]'],            // the open chat's mark, on its row or in it
    "side": ["#side"],                                     // the panel with the list and search
    "search": ['#side [contenteditable="true"][role="textbox"]', '#side [contenteditable="true"]',
               '#side input[type="text"], #side input[type="search"], #side input[role="textbox"]'],
    "chips": ['[role="tab"][aria-selected]', "button[aria-pressed]"],  // filter chips, in #side
    "header": ["#main header"],                            // the open chat's header
    "header-name": ['[data-testid="conversation-info-header-chat-title"]', "span[title]",
                    'span[dir="auto"]'],                   // the chat's name, in the header
    "link-screen": ["#app canvas", "#app [data-ref]", "#app [data-link-code]"],  // the QR screen
    // For replies (below); not judged by the canary, as most need a chat open to be seen.
    "main": ["#main"],                                     // the open chat
    "footer": ["footer"],                                  // its bottom bar, in #main
    "composer": ['[contenteditable="true"][role="textbox"]', '[data-lexical-editor="true"]',
                 '[contenteditable="true"]'],              // its message box, in the footer
    "msg-key": ["[data-id]"],                              // its messages, in #main: their ids (once "<mine>_<chat id>_<id>…")
    "msg-row": ['[role="row"]'],                           // a message's row, in #main
    // WhatsApp's drawers. Their containers are in the page all the time and never empty: each
    // holds WhatsApp's drawer manager, a <div> with an empty <span> in it while nothing is open.
    // So what's open is told by what's inside (previewOpen, drawerEdit), never by the drawer.
    "drawer-left": ['[data-testid="drawer-left"]'],        // over the chat list: Status, Settings, a new group…
    "drawer-middle": ['[data-testid="drawer-middle"]'],    // over the chat: a preview, beside Status an empty state…
    // In the middle drawer, WhatsApp's media editor, the preview of a picture or file waiting to be
    // sent: its hidden "Add file" input (always there while it's open), its caption box.
    "preview": ['input[type="file"][multiple]', '[data-testid="media-caption-input-container"]'],
  };

  function find(key, root = document) {
    for (const candidate of WHERE[key]) {
      const found = root.querySelector(candidate);
      if (found) return found;
    }
    return null;
  }

  // ---- The chat list ------------------------------------------------------------------------
  // WhatsApp only keeps the visible rows in the page. Each row's position comes from its
  // translateY (or aria-rowindex), so Natter can tell how recent the chat is.

  const ROWS = WHERE.row.join(", ");
  const CHIPS = WHERE.chips.join(", ");
  const CELLS = WHERE["row-cell"].join(", ");
  const OWN = WHERE["row-own"].join(", ");
  const NOT_NAME = WHERE["row-other"].join(", ");
  const BESIDE = WHERE["row-beside"].join(", ");
  // What a chat's name read from words never is: a time ("10:42", "9.05 pm", in any script's
  // digits), a date, or a bare count (an unread badge).
  const NOT_A_NAME = /^(\p{Nd}{1,2}[:.]\p{Nd}{2}(\s?[ap]\.?m\.?)?|\p{Nd}{1,2}[/.-]\p{Nd}{1,2}[/.-]\p{Nd}{2,4}|\p{Nd}{1,4}\+?)$/iu;

  // Whether `element` has words of its own: text in it, or an emoji picture (its alt), not only in
  // elements within it. An icon's (an SVG's <title>) aren't anyone's, nor a picture hidden from
  // screen readers (a chat's photo).
  const ownWords = (element) => !element.closest("svg") && [...element.childNodes].some((node) =>
    (node.nodeType === Node.TEXT_NODE ? node.textContent.trim()
      : node.tagName === "IMG" && (node.getAttribute("alt") || "").trim() && node.getAttribute("aria-hidden") !== "true"));

  // How many elements in `row`, itself included, have words of their own.
  const wordsIn = (row) => [row, ...row.querySelectorAll("*")].filter(ownWords).length;

  // Of `elements`, those not inside another of them.
  const outermost = (elements) => elements.filter((one) => !elements.some((other) => other !== one && other.contains(one)));

  // A chat's name in its row and the element it's read from, [element, name], or null for none
  // (WHERE "row-name"). In the row's title cell: its titled span's title, else its words with emoji
  // pictures put back, from the one place in it with words (its one outer span[dir=auto], else its
  // one element with words of its own), never a guess between two (a group's label and its name).
  // Only in a row with no title cell, a titled span: one with no other words before it in the row,
  // and not a community's name over a group's or a label (row-beside). Never in the row's second
  // line or by its status or time, nor holding them (row-other), where WhatsApp's other titled span
  // holds the last message. Never a time or a bare count read from anything but the title cell's
  // titled span: no name, rather than the wrong one.
  function rowName(row) {
    const places = WHERE["row-name"];
    const last = places.length - 1;
    let at = 0;
    let element = null;
    while (at < last && !(element = row.querySelector(places[at]))) at++;
    if (!element) {
      element = row.querySelector(places[last]);
      if (!element || element.closest(BESIDE) || element.querySelector(BESIDE)) return null;
      const all = [...row.querySelectorAll("*")];
      if (all.slice(0, all.indexOf(element)).some((other) => !other.contains(element) && ownWords(other))) return null;
    }
    if (element.closest(NOT_NAME) || element.querySelector(NOT_NAME)) return null;
    if (at === 1 || at === 2) {
      const cell = at === 1 ? element.closest(places[2]) : element;
      const spots = at === 1 ? outermost([...cell.querySelectorAll(places[1])])
        : [cell, ...cell.querySelectorAll("*")].filter(ownWords);
      if (spots.length !== 1) return null;
    }
    const name = nameIn(element);
    return name && (at === 0 || !NOT_A_NAME.test(name)) ? [element, name] : null;
  }

  function nameOf(row) {
    const found = rowName(row);
    return found ? found[1] : "";
  }

  // WhatsApp's list is virtual: each row sits at translateY(place × row height). The row height
  // is the smallest gap between rows, read from their styles: asking the row for its height
  // (offsetHeight) made the page lay itself out again on every scan, while WhatsApp was busiest.
  function offsetOf(row) {
    const match = /translateY\((-?[\d.]+)px\)/.exec(row.style.transform || "");
    return match ? Number(match[1]) : NaN;
  }

  function rowHeight(offsets) {
    const sorted = offsets.filter(Number.isFinite).sort((a, b) => a - b);
    let height = Infinity;
    for (let i = 1; i < sorted.length; i++) {
      const gap = sorted[i] - sorted[i - 1];
      if (gap > 1 && gap < height) height = gap;
    }
    return Number.isFinite(height) ? height : NaN;
  }

  function positionOf(row, offset, height, fallback) {
    const index = Number(row.getAttribute("aria-rowindex"));
    if (index > 0) return index - 1;
    const position = Number.isFinite(offset) && height > 0 ? Math.round(offset / height) : NaN;
    return Number.isFinite(position) ? Math.max(0, position) : fallback;
  }

  // The list's rows as drawn, the outer ones (a row may hold rows of its own): {row, offset: its
  // translateY, i: its place in the page, cell, own, chat, sure, name}. `chat`: it may be a chat's,
  // with WhatsApp's cell for a chat (row-cell) or two elements with words of their own (a name and
  // a time, say): not a placeholder still loading (no words, or one), nor the list's own row
  // (`own`: the Archived row, by its icon, without a chat's cell, and no more words than its own
  // and a count). `sure`: a chat's for certain, its cell there or no row's anywhere: only theirs
  // reach Natter and the canary. Only a row that may be a chat's has a name read (`name`, "" for none).
  function listRows() {
    const list = find("pane");
    if (!list) return [];
    const rows = [];
    list.querySelectorAll(ROWS).forEach((row, i) => {
      if (!(row.parentElement && row.parentElement.closest(ROWS))) rows.push({ row, offset: offsetOf(row), i });
    });
    for (const entry of rows) {
      entry.cell = Boolean(entry.row.querySelector(CELLS));
      const words = entry.cell ? 0 : wordsIn(entry.row);  // only needed without the cell
      entry.own = !entry.cell && words <= 2 && Boolean(entry.row.querySelector(OWN));
      entry.chat = !entry.own && (entry.cell || words >= 2);
      entry.name = entry.chat ? nameOf(entry.row) : "";
    }
    const cells = rows.some((entry) => entry.cell);
    for (const entry of rows) entry.sure = entry.chat && (entry.cell || !cells);
    return rows;
  }

  // The list's chats with a name, as drawn, those surely a chat's (listRows): [name, row, its
  // translateY, its place in the page].
  function namedRows(rows = listRows()) {
    return rows.filter((entry) => entry.sure && entry.name).map((entry) => [entry.name, entry.row, entry.offset, entry.i]);
  }

  // Whether the list marks `row` as the open chat's (WhatsApp's aria-selected, on the row or in it).
  const selectedRow = (row) => WHERE["row-selected"].some((selector) => row.matches(selector)
    || Boolean(row.querySelector(selector)));

  function chatRows(rows = listRows()) {
    const found = namedRows(rows);
    const height = rowHeight(found.map((entry) => entry[2]));
    const places = found.map(([name, row, offset, i]) => [name, positionOf(row, offset, height, i)]);
    return places.sort((a, b) => a[1] - b[1]).slice(0, 200);
  }

  // A chat's name as Natter compares them (activeChat): spaces as one, no invisible marks, and NFC
  // with U+FE0F aside, which emoji pictures may or may not carry.
  const nameKey = (text) => plainName(String(text || "").replace(/\s+/g, " ")).normalize("NFC").replace(/\uFE0F/g, "");

  function sameName(a, b) {
    const key = nameKey(a);
    return Boolean(key) && key === nameKey(b);
  }

  // Whether the list reads the open chat's row as a name its header doesn't show (namesOf, compared
  // as a reply told by the list compares them: sameName): names misread, the last message's words
  // taken for them, say. Told only with a chat open and one row drawn marked as it, with a name;
  // false otherwise.
  function misread(rows = listRows()) {
    const header = find("header");
    const marked = activeChat() ? rows.filter((entry) => selectedRow(entry.row)) : [];
    return marked.length === 1 && Boolean(marked[0].name)
      && !namesOf(header).some((name) => sameName(name.name, marked[0].name));
  }

  // The list is in recency order only with no search typed and the first filter chip ("All")
  // chosen. Chips are told by their selected state and place, not by their words.
  function filtered() {
    const side = find("side");
    if (!side) return false;
    const chips = [...side.querySelectorAll(CHIPS)]
      .filter((chip) => !chip.closest(WHERE.pane.join(", ")));
    return chips.some((chip) => {
      if (chip.getAttribute("aria-selected") !== "true" && chip.getAttribute("aria-pressed") !== "true") return false;
      const group = chip.closest('[role="tablist"]') || side;
      return chips.find((other) => group.contains(other)) !== chip;
    });
  }

  // Text with WhatsApp's emoji pictures put back as the characters they stand for, icons aside.
  function textOf(element) {
    return [...element.childNodes].map((node) => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent;
      if (node.tagName === "IMG") return node.getAttribute("alt") || "";
      return node.nodeType === Node.ELEMENT_NODE && !isIcon(node) ? textOf(node) : "";
    }).join("");
  }

  // An icon's drawing, whose words (an SVG <title>) are its name, not what anyone reads.
  const isIcon = (node) => String(node.tagName).toLowerCase() === "svg";

  const nameIn = (element) => plainName((element.getAttribute("title") || textOf(element)).replace(/\s+/g, " "));

  // The chat's name in a chat's header: its title, or its words with emoji pictures put back.
  function titleOf(header) {
    const title = header && find("header-name", header);
    return title ? nameIn(title) : "";
  }

  // Every name the header may show the chat's as, for each of WHERE's places for it in turn: each
  // element's title and its words. The first is titleOf's.
  function namesOf(header) {
    const names = [];
    if (header) {
      WHERE["header-name"].forEach((selector, at) => header.querySelectorAll(selector).forEach((element) => {
        for (const name of [nameIn(element), plainName(textOf(element).replace(/\s+/g, " "))]) {
          if (name && !names.some((other) => other.name === name)) names.push({ name, at });
        }
      }));
    }
    return names;
  }

  function activeChat() {
    return titleOf(find("header"));
  }

  // WhatsApp's link screen (a QR code, or a code to type on the phone), told by its parts rather
  // than its words: no chat list, and a canvas or WhatsApp's data-ref (the QR's payload).
  function linkScreen() {
    return !find("pane") && Boolean(document.querySelector(WHERE["link-screen"].join(", ")));
  }

  // ---- The canary ---------------------------------------------------------------------------
  // Which WHERE entries still match the live page, told once the chat list is up (the rows need
  // a moment to arrive) and again whenever Python asks. What can't be judged right now — an
  // empty list, one still loading, no open chat — is left out rather than called missing.
  // The chats' names are judged as Natter reads them (nameOf), on the list's first CANARY_ROWS
  // rows by place, the chats' among them (listRows, surely a chat's and with words drawn): found
  // when most have a name and the open chat's row, when it's drawn and marked, has a name its
  // header shows (misread). With the counts of all the rows drawn, never a name. It first looks a
  // moment after the list is up, again while anything is missing or the names can't be judged yet,
  // and tells Natter once a page: at the first look with nothing missing, or else the last, with
  // the list as last seen and its names as last judged (a search typed then, say, or WhatsApp
  // between screens); a last look that finds only the open chat's row misread looks once more a
  // moment on, as a chat being switched to has its row marked before its header is drawn.

  const CANARY_ROWS = 12;
  const CANARY_LOOKS = [10000, 30000, 120000];  // ms after the chat list is first up
  const CANARY_AGAIN = 5000;  // ms on, the last look once more, for the open chat's row misread
  const ROW_KEYS = ["row", "row-name"];
  let canaryTold = false;
  let canaryList = null;  // the canary's latest look with the chat list up
  let canaryRows = null;  // its latest look that judged the list's rows, the open chat's row misread aside
  let namesMisread = false;  // the canary told Natter the open chat's row reads another name

  // What the canary finds ({found, missing}, the counts, and `misread`: row-name missing only for
  // the open chat's row, which isn't told to Natter).
  function canary() {
    const found = [];
    const missing = [];
    const judge = (key, ok) => (ok ? found : missing).push(key);
    judge("app", Boolean(find("app")));
    const pane = find("pane");
    if (!pane) {
      // Looked at only once the list has been up: without it now, WhatsApp is between screens (the
      // link screen, its own loading, in use in another window), and nothing of it can be judged.
      if (linkScreen()) found.push("link-screen");
      return { found, missing };
    }
    found.push("pane");
    const side = find("side");
    judge("side", Boolean(side));
    judge("search", Boolean(find("search")));
    if (side) judge("chips", Boolean(side.querySelector(CHIPS)));
    const rows = listRows();
    // The chats' rows: surely a chat's (listRows), and not one still loading, words not drawn yet.
    const chats = rows.filter((entry) => entry.sure && wordsIn(entry.row) > 0);
    const height = rowHeight(rows.map((entry) => entry.offset));
    const first = new Set(rows.map((entry) => [positionOf(entry.row, entry.offset, height, entry.i), entry])
      .sort((a, b) => a[0] - b[0]).slice(0, CANARY_ROWS).map(([, entry]) => entry));
    const judged = chats.filter((entry) => first.has(entry));
    const named = (list) => list.filter((entry) => entry.name).length;
    const most = named(judged) * 2 > judged.length;
    if (!rows.length) {
      // No rows but a chat's parts: the rows have changed. An empty list can't be judged.
      if (pane.querySelector(`span[title], ${CELLS}`)) judge("row", false);
    } else if (judged.length >= (rows.some((entry) => entry.cell) ? Math.min(3, rows.length) : 3)) {
      // Fewer chats than that: placeholders still loading, say, or only the list's own row. Without
      // WhatsApp's cell for a chat anywhere, three chats, as a banner has two lines of words.
      judge("row", true);
      judge("row-name", most && !misread(rows));
    }
    const header = find("header");
    if (header) judge("header-name", Boolean(find("header-name", header)));
    return { found, missing, rows: rows.length, chatLike: chats.length, named: named(chats),
             misread: missing.includes("row-name") && most };
  }

  // Whether a look of the canary's judged the list's rows.
  const rowsJudged = (look) => ROW_KEYS.some((key) => look.found.includes(key) || look.missing.includes(key));

  // `look` with the list's rows as the look `judged` judged them, and its counts; null: not judged.
  function withRows(look, judged) {
    const keep = (keys, from) => [...keys.filter((key) => !ROW_KEYS.includes(key)),
                                  ...from.filter((key) => ROW_KEYS.includes(key))];
    if (!judged) return { ...look, found: keep(look.found, []), missing: keep(look.missing, []), misread: false };
    return { ...look, found: keep(look.found, judged.found), missing: keep(look.missing, judged.missing),
             rows: judged.rows, chatLike: judged.chatLike, named: judged.named, misread: judged.misread };
  }

  // The canary's look `at` (of CANARY_LOOKS; `again`: the last once more): told to Natter, or
  // looked at again later while something's missing or the names can't be judged yet.
  function canaryLook(at, again = false) {
    const look = canary();
    if (look.found.includes("pane")) canaryList = look;
    if (rowsJudged(look) && !look.misread) canaryRows = look;
    const named = look.found.includes("row-name") || look.missing.includes("row-name");
    if (at + 1 < CANARY_LOOKS.length && (look.missing.length || !named)) {
      setTimeout(() => canaryLook(at + 1), CANARY_LOOKS[at + 1] - CANARY_LOOKS[at]);
      return;
    }
    if (look.misread && !again) {
      setTimeout(() => canaryLook(at, true), CANARY_AGAIN);
      return;
    }
    // The list as last seen, its rows as last judged when it can't judge them, or when it's an
    // earlier look's that found the open chat's row misread, not seen again.
    let result = look.found.includes("pane") ? look : canaryList || look;
    if (!rowsJudged(result) || (result !== look && result.misread)) result = withRows(result, canaryRows);
    const { misread: wrong, ...told } = result;
    if (wrong) namesMisread = true;
    post({ type: "canary", ...told });
  }

  // ---- Calls --------------------------------------------------------------------------------
  // WhatsApp Web disables its voice and video call buttons in WebKitGTK. Natter turns them back
  // on and offers the call another way. They're found by label or icon name, not by class.

  const enabledCalls = new WeakSet();

  function callKind(button) {
    const icons = [...button.querySelectorAll("[data-icon]")].map((icon) => icon.getAttribute("data-icon"));
    const words = [button.getAttribute("aria-label"), button.getAttribute("title"), ...icons].join(" ");
    return /call/i.test(words) ? (/video/i.test(words) ? "video" : "voice") : "";
  }

  function enableCalls() {
    if (typeof window.RTCPeerConnection === "function") return;  // WhatsApp's own buttons work
    const header = find("header");
    if (!header) return;
    header.querySelectorAll('button, [role="button"]').forEach((button) => {
      if (!callKind(button)) return;
      if (button.hasAttribute("disabled") || button.getAttribute("aria-disabled") === "true") {
        button.removeAttribute("disabled");
        button.removeAttribute("aria-disabled");
        enabledCalls.add(button);
      }
    });
  }

  // With calls working in the page (rtc.js), Natter's call buttons press WhatsApp's own.
  function startCall(video) {
    const header = find("header");
    if (!header) return "no-chat";
    const button = [...header.querySelectorAll('button, [role="button"]')]
      .find((b) => callKind(b) === (video ? "video" : "voice"));
    if (!button) return "no-call-button";
    if (button.hasAttribute("disabled") || button.getAttribute("aria-disabled") === "true") return "unavailable";
    press(button);
    return "";
  }

  window.addEventListener("click", (event) => {
    for (let node = event.target; node instanceof Element; node = node.parentElement) {
      if (enabledCalls.has(node)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        post({ type: "call", video: callKind(node) === "video" });
        return;
      }
    }
  }, true);

  // ---- A recorder a late main thread can't break ------------------------------------------
  // WhatsApp records a voice note through a script processor, and WebKitGTK 2.52's copies each
  // chunk out for the page only as that chunk's audioprocess event runs on the main thread: an event
  // run more than a chunk late copies a buffer the audio thread is already filling again, so the
  // page gets later sound in its place (lost, and heard twice: a crack), and while the two take turns
  // on a buffer the audio thread skips a slice. So where it can, createScriptProcessor makes an
  // AudioWorkletNode dressed as a script processor (recorderNode): its processor (recorderProcessor)
  // gathers each chunk on the audio thread and posts it whole to the main thread, where the page gets
  // it in an audioprocess event shaped like WebKit's. A late main thread gets its chunks late, but
  // whole and in order. What the page writes into an event's outputBuffer is posted back and plays
  // two chunks after its chunk began, as with WebKit's (silence where it comes back too late). The
  // node is processed only while its output leads somewhere, as WebKit's: unconnected, an
  // AudioWorkletNode would be processed regardless, so it's connected into an idle GainNode of
  // Natter's meanwhile.
  // A worklet's module loads per context, asynchronously (it's done about 10 ms after the context
  // first runs), and a page records into a context it made a moment before, or in the same task: so
  // Natter makes a context ahead (a spare, spareContext), suspended before it ever runs, and loads the
  // module into it; the page's next `new AudioContext()` like it (WebKit's own rate, or that rate
  // asked for) is that spare, and the next spare is made as it's handed over. The module loads only
  // into a context that isn't playing: the first worklet processor registered in a playing one makes
  // WebKit stop and start its sound output again (BaseAudioContext::workletIsReady), right as a
  // microphone opens when it's a recording's. So besides spares, only a context the idle audio rested
  // takes it (rested), once suspended, and none while the page's getUserMedia is unanswered or a track
  // from it is live. A page's getUserMedia asks WebKit at once, in the page's call (a request that
  // follows a click counts as the click's only then), but the page gets the answer once modules
  // loading then have (SPARE_HOLD_MOST_MS at most), so a context made or recorded into once the
  // microphone opens has it. Modules load from a blob: URL, or a data: one where WhatsApp's
  // Content-Security-Policy refuses that (a kind refused, a SecurityError, isn't tried again in the
  // page, so a policy that reports refusals hears of each at most once; any other failure fails only
  // that context's load, until that kind has failed RECORDER_FAILS_MOST times in the page). A script
  // processor made in a context with no module (made while no spare was ready, or with other
  // options, until it has rested; or its load failed), or where neither kind may load, or of a shape
  // the worklet doesn't take, is WebKit's own, as before, for as long as the page keeps it; the log
  // says once a page which it was, and why (onRecorderPath). The idle audio (above) asks it for each
  // context made, each it rested and each script processor asked for (recorderShim), and counts what
  // it makes as a script processor; without it, they're WebKit's own.
  {
    const Context = window.AudioContext || window.webkitAudioContext;  // the idle audio's: WebKit's createGain
    if (Context && nativeWorkletNode && window.EventTarget) {
      const RECORDER_PROCESSOR = "natter-script-processor";  // never a call's ("voip-…")
      const RECORDER_SIZES = [256, 512, 1024, 2048, 4096, 8192, 16384];  // WebKit's, but 0 (its own pick)
      const RECORDER_CHANNELS_MOST = 32;
      const RECORDER_CONTEXTS_MOST = 16;  // contexts open at once with the module: each runs a worklet thread
      // The worklet's processor, as its module's source (it runs in the worklet, so it uses nothing
      // here). It gathers `size` frames, posts them with the context's frame at the first, and plays
      // what the page wrote for the chunk two before the one it gathers, at the same place in it.
      function recorderProcessor(name) {
        registerProcessor(name, class extends AudioWorkletProcessor {
          constructor(options) {
            super();
            const given = options.processorOptions;
            this.size = given.size;
            this.channels = given.channels;
            this.filled = 0;  // frames of the chunk being gathered
            this.first = 0;  // the context's frame its first frame came at
            this.count = 0;  // chunks posted
            this.chunk = this.fresh();
            this.written = new Map();  // chunk: what the page wrote for it, until it has played
            this.port.onmessage = (event) => {
              const data = event.data;
              if (data && data.n >= this.count - 2 && Array.isArray(data.output)) this.written.set(data.n, data.output);
            };
          }
          fresh() {
            const chunk = [];
            for (let c = 0; c < this.channels; c++) chunk.push(new Float32Array(this.size));
            return chunk;
          }
          process(inputs, outputs) {
            const input = inputs[0] || [];  // no channels while nothing is connected in: silence
            const output = outputs[0] || [];
            const frames = output.length ? output[0].length : 128;
            if (!this.filled) this.first = currentFrame;
            for (let c = 0; c < this.channels; c++) if (input[c]) this.chunk[c].set(input[c], this.filled);
            const written = this.written.get(this.count - 2);
            if (written) {
              for (let c = 0; c < output.length; c++) {
                if (written[c]) output[c].set(written[c].subarray(this.filled, this.filled + frames));
              }
            }
            this.filled += frames;
            if (this.filled >= this.size) {
              this.port.postMessage({ n: this.count, frame: this.first, input: this.chunk },
                                    this.chunk.map((channel) => channel.buffer));
              this.written.delete(this.count - 2);
              this.count++;
              this.filled = 0;
              this.chunk = this.fresh();
            }
            return true;  // false would end it for good
          }
        });
      }
      const RECORDER_MODULE = `(${recorderProcessor})(${JSON.stringify(RECORDER_PROCESSOR)});\n`;
      // Taken now, before the page's scripts run (the AudioWorkletNode before the idle audio wrapped it).
      const NativeWorklet = nativeWorkletNode;
      const NativeProcessor = window.ScriptProcessorNode;
      const NativeBuffer = window.AudioBuffer;
      const ProcessingEvent = window.AudioProcessingEvent;
      const createGain = Context.prototype.createGain;
      const Targets = window.EventTarget && window.EventTarget.prototype;
      const rawListen = Targets && Targets.addEventListener;
      const rawUnlisten = Targets && Targets.removeEventListener;
      const rawDispatch = Targets && Targets.dispatchEvent;
      const objectURL = window.URL && window.URL.createObjectURL;  // (blob: modules can't load without)
      const revokeURL = window.URL && window.URL.revokeObjectURL;
      const recorderModules = new WeakMap();  // each context → { state, kind, … } of its module
      const withModule = new Set();  // WeakRefs to the contexts loading or holding it
      const refusedKinds = new Map();  // a kind of module URL refused in this page → the error's name
      const RECORDER_FAILS_MOST = 3;  // failures of a kind in a page that give it up, as a refusal does
      const failures = new Map();  // each kind of module URL → its loads that failed in this page
      const waiting = [];  // [context, its audioWorklet, its entry], waiting for its turn to load
      let loadingNow = false;
      const settling = [];  // held getUserMedia calls' resolves: called once nothing loads
      const SPARE_HOLD_MOST_MS = 250;  // the longest the page's getUserMedia waits for modules loading
      const LATENCIES = ["interactive", "balanced", "playback"];  // AudioContextLatencyCategory
      let spare = null;  // the context made ahead (spareContext), its module loading or loaded
      let spareRate = 0;  // its sampleRate: WebKit's own pick, as a new AudioContext() has it
      let noSpares = "";  // why none can be had, for good: "none" (no AudioWorklet) or "refused" (both kinds)
      let asking = 0;  // the page's getUserMedia calls not yet answered
      const parked = new WeakMap();  // each context → Natter's idle GainNode
      let constructible = typeof ProcessingEvent === "function";
      const nameOf = (error) => {
        try {
          const name = error && error.name;
          return typeof name === "string" && /^[A-Za-z]{1,40}$/.test(name) ? name : "";
        } catch (_) { return ""; }
      };
      if (!("audioWorklet" in Context.prototype)) noSpares = "none";
      if (typeof objectURL !== "function") refusedKinds.set("blob", "");

      // Tracks that ended go at each getUserMedia call, as one says so ("ended", which stop() doesn't
      // fire), and as a rested context is looked at for its module: an ended track held here keeps
      // WebKit's capturer behind it. So at most the last call's stay held, until the next.
      const dropEnded = () => microphoneTracks.forEach((track) => {
        if (track.readyState !== "live") microphoneTracks.delete(track);
      });
      const microphoneOn = () => {
        dropEnded();
        return asking > 0 || microphoneTracks.size > 0;
      };
      // A context of the page's may take the module only while Natter has it rested and suspended, and
      // no microphone is opening or on: resumed as it registers, its output would stop and start again.
      const restful = (context) => rested.has(context) && context.state === "suspended" && !microphoneOn();

      const unlist = (context) => withModule.forEach((ref) => {
        const other = ref.deref();
        if (!other || other === context || other.state === "closed") withModule.delete(ref);
      });

      // An entry's state: "fresh" (a context of the page's with no module yet: made while no spare was
      // ready, or `unlike` one; it loads once rested), "none" (no AudioWorklet here), "many"
      // (RECORDER_CONTEXTS_MOST already), "loading", "ready", "unloaded" (its load failed, with the
      // error's name: it loads again once rested), "refused" (both kinds) or "closed" (the context,
      // before it loaded). One context loads at a time, in turn: WebKitGTK 2.52 hangs the page's main
      // thread for good when a dozen or so worklet modules load from blob: URLs at once (one after
      // another they don't).
      function loadInto(context, entry) {
        let worklet = null;
        try { worklet = context.audioWorklet; } catch (_) { /* none */ }
        if (!worklet || typeof worklet.addModule !== "function") {
          entry.state = "none";
          return;
        }
        unlist(null);
        if (withModule.size >= RECORDER_CONTEXTS_MOST) {
          entry.state = "many";
          return;
        }
        withModule.add(new WeakRef(context));
        entry.state = "loading";
        waiting.push([context, worklet, entry]);
        loadNext();
      }

      function loadNext() {
        if (loadingNow) return;
        const next = waiting.shift();
        if (!next) {
          settling.splice(0).forEach((resolve) => resolve());
          return;
        }
        const [context, worklet, entry] = next;
        const done = (state, kind = "", name = "") => {
          entry.state = state;
          entry.kind = kind;
          entry.name = name;
          if (state !== "ready") unlist(context);
          if (entry.spare) spareSettled(context, entry);
          loadingNow = false;
          loadNext();
        };
        loadingNow = true;
        const attempt = () => {
          if (context.state === "closed") return done("closed");
          // Resumed, or a microphone on, before its turn: next time it rests.
          if (!entry.spare && !restful(context)) return done("fresh");
          const kind = ["blob", "data"].find((one) => !refusedKinds.has(one));
          if (!kind) return done("refused");
          let url = "";
          // The page's policy refusing that kind is a SecurityError (WebKit's Worklet::addModule checks
          // the Content-Security-Policy first): it isn't tried again in the page, and the next kind is
          // tried now. Anything else (AbortError for a network error or a load cancelled, as for a
          // cross-origin one; TypeError, …) fails only this context's load, until that kind has failed
          // RECORDER_FAILS_MOST times in the page.
          const failed = (error) => {
            if (url && kind === "blob") revokeURL.call(URL, url);
            if (context.state === "closed") return done("closed");
            const name = nameOf(error);
            const count = (failures.get(kind) || 0) + 1;
            failures.set(kind, count);
            if (name !== "SecurityError" && count < RECORDER_FAILS_MOST) return done("unloaded", "", name);
            refusedKinds.set(kind, name);
            return attempt();
          };
          try {
            url = kind === "blob" ? objectURL.call(URL, new Blob([RECORDER_MODULE], { type: "text/javascript" }))
              : `data:text/javascript,${encodeURIComponent(RECORDER_MODULE)}`;
            worklet.addModule(url).then(() => {
              if (kind === "blob") revokeURL.call(URL, url);
              // Closed meanwhile (closing a context doesn't stop its worklet's thread: the load still ends).
              if (context.state === "closed") return done("closed");
              // Resumed while it loaded: its output was likely restarted as the module registered.
              if (!entry.spare && context.state === "running" && onRecorderPath) onRecorderPath("worklet", "restarted", kind, "");
              return done("ready", kind);
            }, failed);
          } catch (error) { failed(error); }
          return undefined;
        };
        attempt();
      }

      // A spare: made suspended, before it ever runs (WebKit then opens no output for it, and the
      // module registering restarts none), and the module loaded into it at once. A spare whose module
      // doesn't load is closed, and the next made as the page next makes a context or asks for the
      // microphone; where none ever can load (no AudioWorklet, both kinds refused), no more are made,
      // and at RECORDER_CONTEXTS_MOST none until the page makes a context again.
      function makeSpare() {
        dropStale();
        if (spare || noSpares || !spareContext) return;
        unlist(null);
        if (withModule.size >= RECORDER_CONTEXTS_MOST) return;
        let context;
        try {
          context = spareContext();
          context.suspend().catch(() => {});
        } catch (_) {
          return;  // tried again as the page makes a context, or asks for the microphone
        }
        spare = context;
        spareRate = context.sampleRate;
        const entry = { state: "loading", kind: "", spare: true };
        recorderModules.set(context, entry);
        loadInto(context, entry);
        if (entry.state !== "loading") spareSettled(context, entry);
      }

      function spareSettled(context, entry) {
        if (entry.state === "ready" || spare !== context) return;
        spare = null;
        if (entry.state === "none" || entry.state === "refused") noSpares = entry.state;
        try { context.close().catch(() => {}); } catch (_) { /* gone */ }
      }

      // A spare no longer suspended (closed, or interrupted) can't be the page's new context: it's let
      // go (closed, if it isn't) for another to be made.
      function dropStale() {
        if (!spare || spare.state === "suspended") return;
        const stale = spare;
        spare = null;
        if (stale.state === "closed") return;
        try { stale.close().catch(() => {}); } catch (_) { /* gone */ }
      }

      // Whether `new AudioContext(...args)` makes a context like a spare: no options, or WebKit's own
      // rate asked for, and any latencyHint (WebKitGTK 2.52 makes the same context for each); nothing else.
      function like(args) {
        const options = args[0];
        if (options === undefined || options === null) return true;
        if (typeof options !== "object") return false;
        try {
          if (Object.keys(options).some((key) => key !== "sampleRate" && key !== "latencyHint")) return false;
          const { sampleRate, latencyHint } = options;
          return (sampleRate === undefined || (typeof sampleRate === "number" && Math.fround(sampleRate) === spareRate))
            && (latencyHint === undefined || LATENCIES.includes(latencyHint)
                || (typeof latencyHint === "number" && Number.isFinite(latencyHint)));
        } catch (_) { return false; }
      }

      // The page's `new AudioContext(...args)`: the spare, if its module has loaded and it's like that
      // one (the next is made at once), or null for WebKit's new one.
      function takeSpare(args) {
        dropStale();
        const context = spare;
        const entry = context && recorderModules.get(context);
        if (!entry || entry.state !== "ready" || !like(args)) {
          makeSpare();
          return null;
        }
        spare = null;
        makeSpare();
        return context;
      }

      // WebKit's new context for the page: no module, until it rests.
      function freshContext(context, args) {
        recorderModules.set(context, { state: "fresh", kind: "", unlike: spareRate > 0 && !like(args) });
      }

      // A context the idle audio has just rested (suspended): its module loads now, if it may.
      function restedContext(context) {
        const entry = recorderModules.get(context);
        if (!entry || noSpares || !["fresh", "many", "unloaded"].includes(entry.state) || !restful(context)) return;
        loadInto(context, entry);
      }

      // The page's getUserMedia gets its answer once the modules loading now have (SPARE_HOLD_MOST_MS at
      // most; a spare is made now if none is at hand); its calls unanswered and the audio tracks they
      // hand over, until they end, keep modules from loading into the page's contexts.
      function settled() {
        makeSpare();
        if (!loadingNow && !waiting.length) return null;
        return new Promise((resolve) => {
          settling.push(resolve);
          setTimeout(resolve, SPARE_HOLD_MOST_MS);
        });
      }
      const devices = window.navigator && window.navigator.mediaDevices;
      if (devices && typeof devices.getUserMedia === "function") {
        const open = devices.getUserMedia;
        devices.getUserMedia = function getUserMedia(...args) {
          dropEnded();
          asking++;
          let asked;
          try {
            // WebKit's is asked now, in the page's call: only then does a request that follows a click
            // count as the click's (MediaDevices::getUserMedia looks as it's called), and WebKit refuses
            // one that doesn't, unasked, once Natter has refused one. Only the answer waits.
            const opened = open.apply(this, args);
            const held = settled();
            asked = held ? Promise.all([opened, held]).then(([stream]) => stream) : opened;
          } catch (error) {
            asking--;
            throw error;
          }
          return Promise.resolve(asked).then((stream) => {
            asking--;
            try {
              for (const track of stream.getAudioTracks()) {
                if (track.readyState !== "live") continue;
                microphoneTracks.add(track);
                rawListen.call(track, "ended", () => microphoneTracks.delete(track));
              }
            } catch (_) { /* the stream is the page's all the same */ }
            return stream;
          }, (error) => {
            asking--;
            throw error;
          });
        };
      }

      // The worklet as a script processor for createScriptProcessor(args), where it can be: { node }
      // with its module's kind, or why not (a state above, "shape" or "failed", with the error's name).
      // A context with no module yet tells why ("fresh", or "options" where it isn't like a spare;
      // "unloaded", with the error's name), but where none ever can have one, that.
      function recorderNode(context, args) {
        const entry = recorderModules.get(context) || { state: "none" };
        const state = (entry.state === "fresh" || entry.state === "unloaded") && noSpares ? noSpares : entry.state;
        if (state === "refused") return { why: "refused", name: refusedKinds.get("data") || "" };
        if (state === "fresh" && entry.unlike) return { why: "options" };
        if (state === "unloaded") return { why: "unloaded", name: entry.name };
        if (state !== "ready") return { why: state };
        const given = (i, otherwise) => (args[i] === undefined ? otherwise : Number(args[i]));  // WebIDL's defaults
        const [size, inputs, outputs] = [given(0, 0), given(1, 2), given(2, 2)];
        const channels = (n) => Number.isInteger(n) && n >= 1 && n <= RECORDER_CHANNELS_MOST;
        if (!RECORDER_SIZES.includes(size) || !channels(inputs) || !channels(outputs)) return { why: "shape" };
        let node;
        try {
          node = new NativeWorklet(context, RECORDER_PROCESSOR, {
            numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [outputs], channelCount: inputs,
            channelCountMode: "explicit", channelInterpretation: "speakers", processorOptions: { size, channels: inputs },
          });
        } catch (error) {
          return { why: "failed", name: nameOf(error) };
        }
        dress(node, context, size, inputs, outputs);
        return { node, why: "ready", kind: entry.kind };
      }

      // An audioprocess event as WebKit's (an AudioProcessingEvent), or one like it where that can't
      // be made.
      function processingEvent(playbackTime, inputBuffer, outputBuffer) {
        if (constructible) {
          try {
            return new ProcessingEvent("audioprocess", { playbackTime, inputBuffer, outputBuffer });
          } catch (_) { constructible = false; }
        }
        const event = new Event("audioprocess");
        for (const [key, value] of Object.entries({ playbackTime, inputBuffer, outputBuffer })) {
          Object.defineProperty(event, key, { value, enumerable: true });
        }
        return event;
      }

      const audioBuffer = (context, numberOfChannels, length, sampleRate) => (typeof NativeBuffer === "function"
        ? new NativeBuffer({ numberOfChannels, length, sampleRate })
        : context.createBuffer(numberOfChannels, length, sampleRate));

      // The worklet node made to look and act like WebKit's script processor: one, by its prototype;
      // bufferSize; onaudioprocess, an event handler as WebKit's (a listener of its own, where it was
      // first set); each chunk handed over in an audioprocess event, and what the page wrote back.
      function dress(node, context, size, inputs, outputs) {
        const port = node.port;
        const connect = node.connect;
        const disconnect = node.disconnect;
        shims.add(node);
        if (NativeProcessor && NativeProcessor.prototype) {
          try { Object.setPrototypeOf(node, NativeProcessor.prototype); } catch (_) { /* stays a worklet to look at */ }
        }
        Object.defineProperty(node, "bufferSize", { configurable: true, enumerable: true, get: () => size });
        let handler = null;
        let relay = null;
        Object.defineProperty(node, "onaudioprocess", {
          configurable: true, enumerable: true,
          get() { return handler; },
          set(value) {
            handler = typeof value === "function" ? value : null;
            if (handler && !relay) {
              relay = function (event) { if (handler) return handler.call(this, event); return undefined; };
              rawListen.call(node, "audioprocess", relay);
            } else if (!handler && relay) {
              rawUnlisten.call(node, "audioprocess", relay);
              relay = null;
            }
          },
        });
        // Its output always leads somewhere: into Natter's idle GainNode while the page's leads nowhere.
        const park = () => {
          try {
            if (!parked.has(context)) parked.set(context, createGain.call(context));
            connect.call(node, parked.get(context));
          } catch (_) { /* a closed context: nothing is processed anyway */ }
        };
        node.disconnect = function (...args) {
          const result = disconnect.apply(this, args);
          if (!args.length || typeof args[0] === "number") park();  // every connection cut
          return result;
        };
        park();
        port.onmessage = (message) => {
          const data = message.data;
          if (!data || !Array.isArray(data.input) || !Number.isFinite(data.frame)) return;
          const rate = context.sampleRate;
          const inputBuffer = audioBuffer(context, inputs, size, rate);
          for (let c = 0; c < inputs; c++) if (data.input[c]) inputBuffer.copyToChannel(data.input[c], c);
          const outputBuffer = audioBuffer(context, outputs, size, rate);
          // When what the page writes plays, as WebKit's says: two chunks after its chunk began.
          rawDispatch.call(node, processingEvent((data.frame + 2 * size) / rate, inputBuffer, outputBuffer));
          try {
            const output = [];
            for (let c = 0; c < outputs; c++) {
              const channel = new Float32Array(size);
              outputBuffer.copyFromChannel(channel, c);
              output.push(channel);
            }
            port.postMessage({ n: data.n, output }, output.map((channel) => channel.buffer));
          } catch (_) { /* the page took its outputBuffer's data away: silence */ }
        };
        // An error in the processor ends it (WebKit's node then plays silence and posts nothing).
        rawListen.call(node, "processorerror", () => {
          if (onRecorderPath) onRecorderPath("worklet", "broke", recorderModules.get(context).kind, "");
        });
        rawListen.call(context, "statechange", function closed() {
          if (context.state !== "closed") return;
          port.onmessage = null;
          try { port.close(); } catch (_) { /* gone */ }
          rawUnlisten.call(context, "statechange", closed);
        });
      }

      recorderShim = { spare: takeSpare, fresh: freshContext, rested: restedContext, make: recorderNode };
      makeSpare();  // the first, now, before the page's scripts run
    }
  }

  // ---- A voice message being recorded -------------------------------------------------------
  // A microphone on outside a call is a voice message being recorded (replies wait for it too).
  // Natter pauses other apps' sound meanwhile (mediapause.py), so the page says when one starts
  // and when it ends: on once a microphone track has been live RECORDING_MS (one opened only for
  // a moment says nothing), off as soon as the last one ends or the microphone is switched off
  // (WebKit ends its tracks), and within a second of a call starting. Each change once. Only the
  // tracks getUserMedia hands the page are looked at, and only whether they're live: nothing of
  // what they carry.
  {
    const RECORDING_MS = 300;
    const RECORDING_LOOK_MS = 1000;  // while on: a call that starts, a track that ends unheard
    const microphones = new Set();  // audio tracks from getUserMedia, until they end
    let recording = false;
    let onTimer = 0;
    let lookTimer = 0;

    // A call is on once its channels are open (calling). Not a peer connection alone: WhatsApp
    // makes those for its own checks (rtc.py NOT_CALLS). Before a call's channels open, Natter
    // knows it's ringing (rtc.py ring) and pauses nothing then.
    const recordingNow = () => {
      microphones.forEach((track) => { if (track.readyState !== "live") microphones.delete(track); });
      return microphones.size > 0 && !calling();
    };

    function tell(on) {
      if (on === recording) return;
      recording = on;
      if (lookTimer) clearInterval(lookTimer);
      lookTimer = on ? setInterval(look, RECORDING_LOOK_MS) : 0;
      post({ type: "recording", on });
    }

    function look() {
      if (!recordingNow()) {
        if (onTimer) clearTimeout(onTimer);
        onTimer = 0;
        tell(false);
      } else if (!recording && !onTimer) {
        onTimer = setTimeout(() => {
          onTimer = 0;
          if (recordingNow()) tell(true);
        }, RECORDING_MS);
      }
    }

    function heard(stream) {
      const tracks = stream && typeof stream.getAudioTracks === "function" ? stream.getAudioTracks() : [];
      for (const track of tracks) {
        if (microphones.has(track)) continue;
        microphones.add(track);
        track.addEventListener("ended", look);
      }
      if (tracks.length) look();
    }

    const devices = window.navigator && window.navigator.mediaDevices;
    if (devices && typeof devices.getUserMedia === "function") {
      const open = devices.getUserMedia;
      devices.getUserMedia = function getUserMedia(...args) {
        return open.apply(this, args).then((stream) => {
          try { heard(stream); } catch (_) { /* the stream is WhatsApp's all the same */ }
          return stream;
        });
      };
    }
    const Track = window.MediaStreamTrack;
    if (Track && typeof Track.prototype.stop === "function") {
      const stop = Track.prototype.stop;
      Track.prototype.stop = function (...args) {
        const result = stop.apply(this, args);
        if (microphones.has(this)) look();
        return result;
      };
    }
  }

  // ---- Watching the page --------------------------------------------------------------------
  // Until the chat list is up (loading, the link screen) the page is small and all of #app is
  // watched. Then only the list and the open chat's header are, plus, shallowly, #app and the
  // elements around them, to notice when WhatsApp replaces either.

  let lastRows = "";
  let lastActive = null;
  let lastReady = null;
  let lastState = "";
  let scanQueued = false;
  const watched = { app: null, pane: null, header: null, home: null };
  const changes = new MutationObserver(queueScan);
  const layout = new MutationObserver(() => { rewatch(); queueScan(); });

  function rewatch() {
    const app = find("app") || document.body;
    const pane = find("pane");
    const header = find("header");
    // #main's parent stays while chats open and close, so #main coming back is seen there.
    const home = header ? header.closest("#main").parentElement
      : watched.home && watched.home.isConnected ? watched.home : null;
    if (app === watched.app && pane === watched.pane && header === watched.header && home === watched.home) return;
    Object.assign(watched, { app, pane, header, home });
    changes.disconnect();
    layout.disconnect();
    if (!app) return;
    if (!pane) {
      changes.observe(app, { childList: true, subtree: true });
      return;
    }
    changes.observe(pane, { childList: true, subtree: true, attributes: true,
                            attributeFilter: ["style", "title", "aria-selected"] });
    if (header) {
      changes.observe(header, { childList: true, subtree: true, characterData: true, attributes: true,
                                attributeFilter: ["title", "disabled", "aria-disabled"] });
    }
    for (const start of [pane.parentElement, home]) {
      for (let node = start; node && node !== app && app.contains(node); node = node.parentElement) {
        layout.observe(node, { childList: true });
      }
    }
    layout.observe(app, { childList: true });
  }

  function scan() {
    scanQueued = false;
    rewatch();
    enableCalls();
    // Names misread never reach Natter's list of chats: not while the open chat's row reads another
    // name than its header, nor on a page the canary found that on.
    if (!searching() && !filtered() && !namesMisread) {
      const listed = listRows();
      const rows = chatRows(listed);
      const signature = JSON.stringify(rows);
      if (signature !== lastRows && rows.length && !misread(listed)) {
        lastRows = signature;
        post({ type: "chats", rows });
      }
    }
    const active = activeChat();
    if (active !== lastActive) {
      lastActive = active;
      post({ type: "active", name: active });
    }
    const ready = Boolean(find("pane"));
    if (ready !== lastReady) {
      lastReady = ready;
      post({ type: "ready", ready });
      if (ready) reportLook();
      if (ready && !canaryTold) {
        canaryTold = true;
        setTimeout(() => canaryLook(0), CANARY_LOOKS[0]);
      }
    }
    const state = ready ? "chats" : linkScreen() ? "needs-link" : "";
    if (state && state !== lastState) {
      lastState = state;
      post({ type: "state", value: state });
    }
  }

  function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    setTimeout(scan, 800);
  }

  function watch() {
    if (!document.body) return setTimeout(watch, 500);
    rewatch();
    queueScan();
    watchLook();
  }

  // ---- Sound that can't be heard, for the log ----------------------------------------------
  // Voice messages may play through <audio> elements (WebKit's GStreamer player), perhaps through
  // a Web Audio context as well (createMediaElementSource): then the element's own output is muted
  // and the sound comes only through the context. A page may also decode sound itself, with
  // decodeAudioData or WebCodecs' AudioDecoder, and play it through a context. Where it can't be
  // heard, the log says how the page tried: at its first sound, how WebKit answers for the
  // formats voice messages come in; once for each way sound played (from the element, or
  // through a context, and the context's state); decodeAudioData resolved or rejected; an Opus
  // AudioDecoder supported, configured or failing; a decoded sound started, and its context's
  // state then; a page's own worklet or script processor connected, and its context's state
  // then; a recorder fed by the microphone again (a voice note resumed after a pause), whether
  // Natter rested its context meanwhile, and its state and clock then; and each failure: the
  // element's error (MediaError's code), play() refused (the error's name, not its message), the
  // context it plays through not running a moment after it started, or a context Natter rested
  // not resuming. Each line once a page for what it says, so repeats don't use up the page's
  // MEDIA_LINES. Kinds, codes, states and error names only, never a URL, a name or what plays.
  // How a <video> played, and how a voice note recorded, have lines of their own, each counted apart
  // (watchVideo, watchRecording, below).
  {
    const MEDIA_LINES = 12;
    const MEDIA_TYPES_KEPT = 64;
    // The media types a source is told as; any other is "audio-other" or "video-other" (a blob's
    // type comes from the message, which its sender writes).
    const MEDIA_TYPES = ["audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/aac",
                         "audio/x-m4a", "audio/webm", "audio/wav", "audio/x-wav", "audio/amr", "audio/3gpp",
                         "audio/flac", "video/mp4", "video/webm", "video/quicktime", "video/3gpp", "video/ogg"];
    const SUPPORT = {  // what voice messages, and the sounds around them, come as
      "ogg-opus": 'audio/ogg; codecs="opus"', "ogg": "audio/ogg", "webm-opus": 'audio/webm; codecs="opus"',
      "mp4-opus": 'audio/mp4; codecs="opus"', "mp4-aac": 'audio/mp4; codecs="mp4a.40.2"', "mpeg": "audio/mpeg",
    };
    let mediaLines = 0;
    const toldOnce = new Set();  // what each line said (its key): at most MEDIA_LINES of them
    // `once`: the line's key; the same key is never sent twice. The first line of a page says
    // how WebKit answers for voice messages' formats first.
    const tellMedia = (message, once) => {
      if (mediaLines >= MEDIA_LINES || toldOnce.has(once)) return;
      if (once !== "support" && !toldOnce.has("support")) tellMedia(support(), "support");
      if (mediaLines >= MEDIA_LINES) return;
      toldOnce.add(once);
      mediaLines++;
      post({ type: "media", ...message });
    };
    // An error's name ("NotAllowedError"), never its message; "" for anything else.
    const errorName = (error) => {
      try {
        const name = error && error.name;
        return typeof name === "string" && /^[A-Za-z]{1,40}$/.test(name) ? name : "";
      } catch (_) { return ""; }
    };

    // What each object URL is of: a Blob's type without its parameters, or a MediaSource.
    const mediaTypes = new Map();
    const createObjectURL = URL.createObjectURL;
    URL.createObjectURL = function (object) {
      const url = createObjectURL.call(this, object);
      let type = "";
      try {
        if (object instanceof Blob) type = String(object.type).split(";")[0].trim().toLowerCase();
        else if (window.MediaSource && object instanceof window.MediaSource) type = "mediasource";
      } catch (_) { /* not one of those */ }
      if (type !== "mediasource" && !MEDIA_TYPES.includes(type)) {
        type = type.startsWith("audio/") ? "audio-other" : type.startsWith("video/") ? "video-other" : "";
      }
      if (type) {
        mediaTypes.set(url, type);
        if (mediaTypes.size > MEDIA_TYPES_KEPT) mediaTypes.delete(mediaTypes.keys().next().value);
      }
      return url;
    };

    function sourceOf(element) {
      const url = String(element.currentSrc || element.src || "");
      if (!url) return element.srcObject ? "stream" : "none";
      if (mediaTypes.has(url)) return mediaTypes.get(url);
      const scheme = url.slice(0, url.indexOf(":") + 1).toLowerCase();
      return { "blob:": "blob", "https:": "https", "data:": "data", "file:": "file" }[scheme] || "other";
    }

    const isVideo = (element) => Boolean(window.HTMLVideoElement) && element instanceof window.HTMLVideoElement;
    function about(element) {
      const context = routed.get(element);
      return {
        kind: isVideo(element) ? "video" : "audio",
        route: context ? "webaudio" : "element",
        source: sourceOf(element),
        state: context ? String(context.state) : "",
      };
    }
    // The key of a line about an element: what, its code or error name, its kind, route, state
    // and source.
    const keyOf = (what, detail, now) => `${what} ${detail} ${now.kind} ${now.route} ${now.state} ${now.source}`;

    function support() {
      const probe = document.createElement("audio");
      const play = {};
      const source = {};
      for (const [key, type] of Object.entries(SUPPORT)) {
        try { play[key] = String(probe.canPlayType(type)); } catch (_) { play[key] = ""; }
        try { source[key] = Boolean(window.MediaSource && window.MediaSource.isTypeSupported(type)); } catch (_) { source[key] = false; }
      }
      return { what: "support", play, source };
    }

    const watchedMedia = new WeakSet();
    function watchMedia(element) {
      if (watchedMedia.has(element)) return;
      watchedMedia.add(element);
      element.addEventListener("error", () => {
        const error = element.error;
        const code = error && Number.isInteger(error.code) ? error.code : 0;
        const now = about(element);
        tellMedia({ what: "error", code, ...now }, keyOf("error", code, now));
      });
      element.addEventListener("playing", () => {
        const now = about(element);
        tellMedia({ what: "played", ...now }, keyOf("played", "", now));
        setTimeout(() => {
          const context = routed.get(element);
          if (context && context.state !== "running" && !element.paused && !element.ended) {
            const later = about(element);
            tellMedia({ what: "silent", ...later }, keyOf("silent", "", later));
          }
        }, 1500);
      });
      if (isVideo(element)) watchVideo(element);
    }

    // WebKitGTK 2.52.6 can drop a play() that comes while the element is still settling a seek it
    // was given before its data came: the element then says it plays, has its data and no error,
    // and its time stands still until something asks it to play again. WhatsApp does just that
    // when a voice message is played again after a pause (a new source, the place it paused at,
    // then play()), and the message went silent until the person moved its slider (a lab of that
    // order stalled every time, Natter's scripts or not). So a play() of that kind (away from the
    // start, while the element still loads or seeks) is looked at once it has settled: STALL_MS on,
    // or again each STALL_MS while it still settles, up to SETTLE_LOOKS (SETTLED_MS more once it has,
    // as a pipeline only just settled takes a moment to start). One still standing then is
    // asked to play() again, which starts it; if it still doesn't move, its place is nudged (a real
    // seek, which starts it too). A live stream (a call's sound, srcObject) is left alone, and any
    // later play() ends the look. The log says how it went, once a page for each kind of element
    // and outcome, and only when it went somewhere: a pause or a new source in between says nothing.
    const STALL_MS = 1000;
    const SETTLE_LOOKS = 5;
    const SETTLED_MS = 250;
    const HAVE_FUTURE_DATA = 3;
    const playRound = new WeakMap();  // each element → its latest play()'s count: a later play() ends an earlier look
    const timeOf = (element) => Number(element.currentTime) || 0;
    const going = (element) => !element.srcObject && !element.paused && !element.ended && !element.error;
    const settled = (element) => !element.seeking && element.readyState >= HAVE_FUTURE_DATA;
    // Says it plays, has its data, and its time hasn't left `from`.
    const standing = (element, from) => going(element) && settled(element) && Math.abs(timeOf(element) - from) < 0.01;
    // Played on by itself: its time past `from`, or played to its end.
    const movedOn = (element, from) => !element.error
      && (element.ended || (going(element) && settled(element) && timeOf(element) - from >= 0.05));

    function watchStart(element, from, round) {
      const ours = () => playRound.get(element) === round;
      const told = (then) => {
        const now = about(element);
        tellMedia({ what: "stalled", then, ...now }, keyOf("stalled", then, now));
      };
      let looks = 0;
      let waited = false;
      const look = () => {
        if (!ours() || !going(element)) return;
        if (!settled(element)) {  // still loading or settling its seek: look again in a while
          waited = true;
          if (++looks < SETTLE_LOOKS) setTimeout(look, STALL_MS);
          return;
        }
        if (waited) {  // only just settled: a moment for it to start
          waited = false;
          setTimeout(look, SETTLED_MS);
          return;
        }
        if (!standing(element, from)) return;
        const again = play.call(element);  // not through this wrapper: no look of its own
        if (again && typeof again.catch === "function") again.catch(() => {});
        setTimeout(() => {
          if (!ours()) return;
          if (!standing(element, from)) {
            if (movedOn(element, from)) told("play");
            return;
          }
          element.currentTime = from + 0.001;
          setTimeout(() => {
            if (!ours()) return;
            if (standing(element, from + 0.001)) told("still");
            else if (movedOn(element, from + 0.001)) told("seek");
          }, STALL_MS);
        }, STALL_MS);
      };
      setTimeout(look, STALL_MS);
    }

    // Videos that play as if short of data: a status plays from a blob of its whole file, so the
    // network is out of it. For each <video> that plays (not a call's stream), a spell of its
    // playback is measured, from "playing" (or a seek settled) until it pauses, ends, fails, seeks
    // or takes a new source, or VIDEO_SPELL_MS of it (and the next spell starts there, going on
    // from it): the media seconds it played in how many seconds; each wait for data ("waiting",
    // until it plays again) and how long it took, and each "stalled"; the frames it presented
    // (requestVideoFrameCallback) and the stretches without one over VIDEO_GAP_MS (between frames,
    // and from its last to the spell's end), while it plays and doesn't wait (a wait is counted as
    // one, not as a gap); the frames WebKit dropped of all that reached its sink
    // (getVideoPlaybackQuality); and how late a light timer on the page's main thread ran
    // meanwhile, every VIDEO_TICK_MS while a video plays and not at all otherwise. Waits say its
    // data came too slowly; gaps or drops without them, its frames weren't drawn in time; a late
    // main thread, the page held things up. A hidden page draws no frames and runs its timers
    // late, so its spells are dropped, and those still playing start again when it shows. The log
    // has a page's first spell (to compare with), then only rough ones (a wait or a stall, a gap or
    // a late main thread over VIDEO_ROUGH_MS, two frames or more dropped and over
    // VIDEO_DROPPED_SHARE of them, or more than VIDEO_LOST_MS of its media behind the clock, which
    // is how a slow playback clock shows: no wait, and its time running behind; a looping one's
    // media time is only roughly known, so not for that): VIDEO_LINES a page, apart from MEDIA_LINES so videos can't
    // crowd the sound's lines out, and VIDEO_LINES_EACH for one element's one source. Once they
    // are used up nothing is measured. Measuring isn't free: while a frame callback is asked for,
    // WebKit runs the page's rendering update at the display's rate. So at most VIDEO_SPELLS_EACH
    // spells of one element's one source are measured, told or not (a whole status), and of one
    // that loops only its first (its later rounds only repeat it). It never changes how a video
    // plays. Kinds, counts, durations and sizes only.
    const VIDEO_LINES = 16;
    const VIDEO_LINES_EACH = 2;
    const VIDEO_SPELLS_EACH = 5;
    const VIDEO_SPELL_MS = 20000;
    const VIDEO_TICK_MS = 100;
    const VIDEO_GAP_MS = 100;
    const VIDEO_ROUGH_MS = 150;
    const VIDEO_LOST_MS = 400;  // a smooth start loses well under 100 ms
    const VIDEO_DROPPED_SHARE = 0.05;
    const VIDEO_MOST = 3600000;  // no number sent is larger (playback.py takes none that is)
    const spells = new Map();  // each <video> playing now → its spell
    const videoCounts = new WeakMap();  // each <video> → its source last measured, its spells measured and lines told
    let videoLines = 0;
    let videoTimer = 0;
    let lastTick = 0;
    let heldAt = -Infinity;  // when a tick last ran more than VIDEO_GAP_MS late
    // A frame of the page's videos was called back. WebKit without hardware acceleration calls
    // back none, so until one has, a spell without frames isn't rough for that.
    let framesSeen = false;
    const counted = (value) => Math.min(Math.max(Math.round(value), 0), VIDEO_MOST);
    const sizeOf = (value) => Math.min(Math.round((Number(value) || 0) / 10) * 10, VIDEO_MOST);  // to tens of pixels
    const srcOf = (element) => String(element.currentSrc || element.src || "");  // kept here, never sent
    // Whether the page's main thread was held since `since`: a tick ran late meanwhile, or one is
    // overdue now.
    const heldSince = (since) => heldAt > since || performance.now() - lastTick - VIDEO_TICK_MS > VIDEO_GAP_MS;
    // The frames GStreamer's sink dropped and showed since it last started counting, as WebKit
    // reads them (its totalVideoFrames is those shown), or null where WebKit doesn't count them.
    const qualityOf = (element) => {
      try {
        const quality = element.getVideoPlaybackQuality();
        return [Number(quality.droppedVideoFrames) || 0, Number(quality.totalVideoFrames) || 0];
      } catch (_) { return null; }
    };

    function watchVideo(element) {
      element.addEventListener("playing", () => {
        const spell = spells.get(element);
        if (!spell) begin(element);
        else if (spell.waitingSince !== null) {  // its wait is over: a frame is due from now
          const now = performance.now();
          spell.waited += now - spell.waitingSince;
          spell.waitingSince = null;
          spell.frameAt = now;
        }
      });
      element.addEventListener("waiting", () => {
        const spell = spells.get(element);
        if (!spell || spell.waitingSince !== null) return;
        spell.waits++;
        spell.waitingSince = performance.now();
        spell.frameAt = null;  // no gap while it waits: the wait is told as a wait
      });
      element.addEventListener("stalled", () => {
        const spell = spells.get(element);
        if (spell) spell.stalls++;
      });
      for (const type of ["pause", "ended", "emptied", "error"]) element.addEventListener(type, () => end(element));
      // A seek ends a spell, its time at the last look (one that loops seeks back to its start
      // each time round, which isn't one), and the next starts once it has settled: at "seeked",
      // or at "playing" when it waits for data first.
      element.addEventListener("seeking", () => { if (!element.loop) end(element, "seek"); });
      element.addEventListener("seeked", () => { if (going(element) && settled(element)) begin(element); });
      if (going(element) && settled(element)) begin(element);  // playing already (autoplay)
    }

    // `before`: the spell that ran out just now (VIDEO_SPELL_MS of it), which this one goes on from.
    function begin(element, before = null) {
      if (spells.has(element) || videoLines >= VIDEO_LINES || document.hidden || !going(element)) return;
      const src = srcOf(element);
      let counts = videoCounts.get(element);
      if (!counts || counts.src !== src) counts = { src, spells: 0, lines: 0 };
      if (counts.lines >= VIDEO_LINES_EACH || counts.spells >= (element.loop ? 1 : VIDEO_SPELLS_EACH)) return;
      counts.spells++;
      videoCounts.set(element, counts);
      const now = performance.now();
      const quality = qualityOf(element);
      const spell = {
        began: now, counts, about: about(element), loop: Boolean(element.loop),
        width: sizeOf(element.videoWidth), height: sizeOf(element.videoHeight),
        at: timeOf(element), played: 0, waits: 0, waited: 0, waitingSince: null, stalls: 0, late: 0,
        // frameAt: since when no frame has come (null while it waits); seen: the last look's counts
        frames: null, gaps: 0, longest: 0, frameAt: now, presented: null, callback: 0,
        quality: quality && [0, 0], seen: quality,
      };
      if (before) {
        // A wait running across is split between the two (any wait is rough); a stretch without a
        // frame goes on whole (a gap is rough by its length).
        spell.presented = before.presented;
        if (before.waitingSince !== null) Object.assign(spell, { waits: 1, waitingSince: now, frameAt: null });
        else spell.frameAt = before.frameAt;
      }
      spells.set(element, spell);
      if (typeof element.requestVideoFrameCallback === "function") {
        spell.frames = 0;
        const next = (when, metadata) => {
          if (spells.get(element) !== spell) return;
          framesSeen = true;
          const at = Number.isFinite(when) ? when : performance.now();
          const presented = metadata && Number.isFinite(metadata.presentedFrames) ? metadata.presentedFrames : null;
          // Frames counted since the last call. WebKit counts one when its sink hands it on, not
          // when it is on screen, so several counted at once were shown one by one only if the
          // page's main thread was held meanwhile (it called back late), and then the gap is
          // shared between them. Otherwise the picture itself stood still: a gap.
          const count = presented !== null && spell.presented !== null ? Math.max(1, presented - spell.presented) : 1;
          spell.presented = presented;
          spell.frames += count;
          if (spell.frameAt !== null) stretch(spell, (at - spell.frameAt) / (heldSince(spell.frameAt) ? count : 1));
          spell.frameAt = spell.waitingSince === null ? at : null;
          spell.callback = element.requestVideoFrameCallback(next);
        };
        spell.callback = element.requestVideoFrameCallback(next);
      }
      if (!videoTimer) {
        lastTick = now;
        videoTimer = setInterval(tick, VIDEO_TICK_MS);
      }
    }

    // `ms` without a frame: a gap if over VIDEO_GAP_MS.
    function stretch(spell, ms) {
      if (ms > VIDEO_GAP_MS) spell.gaps++;
      spell.longest = Math.max(spell.longest, ms);
    }

    // The media seconds played since the last look: its time moved on, or (one that loops) went
    // back to its start and on from there.
    function sample(element, spell) {
      const time = timeOf(element);
      spell.played += time >= spell.at ? time - spell.at : spell.loop ? time : 0;
      spell.at = time;
    }

    // The frames dropped and shown since the last look, added to the spell's. The sink counts from
    // 0 again after a pause, a seek or a loop, and a new source brings a new player counting from
    // 0, while WebKit keeps each count until the new one is past 0: one that went down started
    // again, and all of it is new. WebKit's counts stop at "pause", "ended" or a new source, so a
    // look then adds nothing, and the spell loses at most the frames since the last tick.
    function tally(element, spell) {
      const quality = spell.seen && qualityOf(element);
      if (!quality) return;
      for (const i of [0, 1]) spell.quality[i] += quality[i] >= spell.seen[i] ? quality[i] - spell.seen[i] : quality[i];
      spell.seen = quality;
    }

    function tick() {
      const now = performance.now();
      const late = now - lastTick - VIDEO_TICK_MS;
      lastTick = now;
      if (late > VIDEO_GAP_MS) heldAt = now;
      for (const [element, spell] of [...spells]) {
        spell.late = Math.max(spell.late, late);
        sample(element, spell);
        tally(element, spell);
        if (now - spell.began < VIDEO_SPELL_MS) continue;
        end(element, "onward");
        begin(element, spell);  // it plays on: its next spell starts now
      }
    }

    // `how`: "hidden" drops the spell (a hidden page's); "seek" leaves its time where the last look
    // saw it (a seek has moved it); "onward" leaves the stretch since its last frame to the next
    // spell (this one ran out, and it plays on).
    function end(element, how = "") {
      const spell = spells.get(element);
      if (!spell) return;
      const now = performance.now();
      if (how !== "seek") sample(element, spell);
      tally(element, spell);
      spell.late = Math.max(spell.late, now - lastTick - VIDEO_TICK_MS);  // its timer due meanwhile
      // The stretch since its last frame (or since it started, or its wait ended), unless the
      // main thread was held across it: then its frames may have been shown and not called back,
      // and it's told as late.
      if (spell.frames !== null && spell.waitingSince === null && how !== "onward" && !heldSince(spell.frameAt)) {
        stretch(spell, now - spell.frameAt);
      }
      spells.delete(element);
      if (spell.callback && typeof element.cancelVideoFrameCallback === "function") {
        element.cancelVideoFrameCallback(spell.callback);
      }
      if (!spells.size) {
        clearInterval(videoTimer);
        videoTimer = 0;
      }
      if (how === "hidden" || videoLines >= VIDEO_LINES) return;
      if (spell.waitingSince !== null) spell.waited += now - spell.waitingSince;
      const dropped = spell.quality ? counted(spell.quality[0]) : null;
      const total = spell.quality ? counted(spell.quality[0] + spell.quality[1]) : null;
      const frames = spell.frames === null ? null : counted(spell.frames);
      const longest = frames === null ? null : counted(spell.longest);
      const late = counted(spell.late);
      const lasted = counted(now - spell.began);
      const played = counted(spell.played * 1000);
      const rough = spell.waits > 0 || spell.stalls > 0 || late > VIDEO_ROUGH_MS
        || (longest !== null && longest > VIDEO_ROUGH_MS && framesSeen)
        || (dropped !== null && dropped >= 2 && dropped > total * VIDEO_DROPPED_SHARE)
        || (!spell.loop && lasted - played > VIDEO_LOST_MS);
      if (videoLines > 0 && !rough) return;
      spell.counts.lines++;
      videoLines++;
      post({
        type: "media", what: "video", ...spell.about, loop: spell.loop, width: spell.width, height: spell.height,
        played, lasted,
        waits: counted(spell.waits), waited: counted(spell.waited), stalls: counted(spell.stalls),
        frames, gaps: frames === null ? null : counted(spell.gaps), longest, dropped, total, late,
      });
    }

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        for (const element of [...spells.keys()]) end(element, "hidden");
      } else {
        // One still waiting or seeking starts its spell at "playing" or "seeked": one begun now
        // would take its wait for playing.
        for (const element of started) {
          if (isVideo(element) && watchedMedia.has(element) && settled(element)) begin(element);
        }
      }
    });

    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
      if (!toldOnce.has("support")) tellMedia(support(), "support");
      watchMedia(this);
      const from = timeOf(this);
      const settling = from > 0 && (this.seeking || this.readyState < HAVE_FUTURE_DATA);
      const result = play.apply(this, args);
      const round = (playRound.get(this) || 0) + 1;
      playRound.set(this, round);
      if (settling) watchStart(this, from, round);
      if (result && typeof result.then === "function") {
        result.then(null, (error) => {
          const name = errorName(error);
          const now = about(this);
          // AbortError: a pause() or a new source came first, which is no failure.
          if (name !== "AbortError") tellMedia({ what: "refused", name, ...now }, keyOf("refused", name, now));
        });
      }
      return result;
    };

    // Sound the page decodes itself: decodeAudioData's answer, and a decoded sound started.
    const decoded = new WeakSet();  // AudioBuffers decodeAudioData made
    const tellDecode = (result, error) => {
      const name = result === "rejected" ? errorName(error) : "";
      tellMedia({ what: "decode", result, name }, `decode ${result} ${name}`);
    };
    const Base = window.BaseAudioContext || window.AudioContext || window.webkitAudioContext;
    const decode = Base && Base.prototype && Base.prototype.decodeAudioData;
    if (typeof decode === "function") {
      Base.prototype.decodeAudioData = function (data, ok, fail, ...rest) {
        const done = typeof ok === "function" ? function (buffer) {
          if (buffer && typeof buffer === "object") decoded.add(buffer);
          tellDecode("resolved");
          return ok.apply(this, arguments);
        } : ok;
        const failed = typeof fail === "function" ? function (error) {
          tellDecode("rejected", error);
          return fail.apply(this, arguments);
        } : fail;
        let result;
        try {
          result = decode.call(this, data, done, failed, ...rest);
        } catch (error) {
          tellDecode("rejected", error);
          throw error;
        }
        if (result && typeof result.then === "function") {
          // Watched beside the page's own handlers: the page still gets the same promise.
          result.then((buffer) => {
            if (buffer && typeof buffer === "object") decoded.add(buffer);
            tellDecode("resolved");
          }, (error) => tellDecode("rejected", error));
        }
        return result;
      };
    }
    for (const Node of [window.AudioScheduledSourceNode, window.AudioBufferSourceNode]) {
      if (!Node || !Object.prototype.hasOwnProperty.call(Node.prototype, "start")) continue;
      const start = Node.prototype.start;
      Node.prototype.start = function (...args) {
        let buffer = null;
        try { buffer = this.buffer; } catch (_) { /* not a buffer source */ }
        const context = this.context;
        if (buffer && decoded.has(buffer) && context) {
          // Its context's state a moment on: Natter's start() has woken it by then, if it can.
          setTimeout(() => {
            const state = String(context.state);
            tellMedia({ what: "started", state }, `started ${state}`);
          }, 1500);
        }
        return start.apply(this, args);
      };
    }

    // A page's own player connected (a worklet, not a call's, or a script processor: sound
    // decoded in wasm or a worker plays that way, with no other line): its context's state a
    // moment on, once a page for each kind and state.
    const toldProcessors = new WeakSet();
    onProcessorConnected = (node, script) => {
      const context = node.context;
      if (!context || toldProcessors.has(node)) return;
      toldProcessors.add(node);
      setTimeout(() => {
        const kind = script ? "script" : "worklet";
        const state = String(context.state);
        tellMedia({ what: "processor", kind, state }, `processor ${kind} ${state}`);
      }, 1500);
    };

    // A recorder fed by the microphone again (a voice note resumed after a pause, or the next one
    // recorded into the same recorder): whether Natter rested its context meanwhile, and after a
    // wake's second try could have brought it back, its state and whether its clock moves (it
    // doesn't when its output stream failed to open: then nothing is recorded). Kinds only.
    onRecorderResumed = (node, quiet) => {
      const context = node.context;
      if (!context) return;
      const since = lastRested.get(context);
      const restedThen = since !== undefined && quiet !== undefined && since >= quiet;
      const before = Number(context.currentTime) || 0;
      setTimeout(() => {
        const state = String(context.state);
        const moving = (Number(context.currentTime) || 0) - before > 0.5;
        tellMedia({ what: "resumed", state, rested: restedThen, moving },
                  `resumed ${state} ${restedThen} ${moving}`);
      }, WAKE_AGAIN_MS + 1500);
    };
    // How each voice note recorded. WhatsApp records through a script processor fed by the
    // microphone (a recorder). WebKitGTK 2.52 fills one of its two buffers on the audio thread, a
    // chunk (bufferSize frames) at a time, and hands each full one to the page in an audioprocess
    // event on the main thread, copying it out for the page only as the event runs, while the audio
    // thread fills the other. An event that runs more than a chunk after its chunk filled copies out
    // a buffer that is being filled again: the page gets part or all of a later chunk's sound in place
    // of its own, so that much of the recording is lost and a later bit of it comes twice (and while
    // an event runs on the buffer the audio thread is on, the audio thread skips what it would write:
    // that sound is lost too). Every chunk is still handed over, the late ones in a burst, so their
    // count doesn't show it. An event's playbackTime is the context's clock as it runs (plus a
    // chunk), not where its chunk was: less a chunk for each chunk before it, it's the same for each
    // that ran as soon as its chunk filled, later by as much as one ran late, and later for good by
    // what the audio thread skipped before its chunk filled (its clock runs on as it skips). A late
    // one is followed by less late ones in its burst, down to one on time, while a skip moves all
    // the chunks after it on alike. So each is held against the earliest of it and those
    // RECORDING_AFTER_MS after it, once they have come (a burst at the very start is seen from those
    // after it, and the skips drop out of it), and the sound skipped is how much later the last
    // chunk sits than the earliest from the first on (with any lateness of the last, less than a
    // chunk as it ends a burst).
    // Natter's worklet recorder (recorderNode, in the idle audio) hands chunks over whole however
    // late: each event's playbackTime is where its chunk was, so the offsets stay put (but for sound
    // not gathered: the worklet not processed meanwhile), and how late each came is the context's
    // clock as it runs, past its chunk's end. Its chunks handed over more than a chunk late are late
    // but whole: nothing is lost.
    // A recording runs from its first chunk after a microphone feeds the recorder until the
    // microphone stops, the recorder is disconnected, its context closes, or RECORDING_MOST_MS
    // on. For each: its chunks, their size and the context's sample rate (and the microphone's,
    // where WebKit says it); the seconds they hold, the seconds they took (to the last look that
    // found the microphone still on, where that's later: a clock that stops for good hands over no
    // chunk after it), and the seconds on the context's clock meanwhile; the sound the audio thread
    // skipped; the longest wait between two (or since the last, while the microphone was still on);
    // those handed over more than a chunk late, and the sound lost that way; how late the worst ran
    // (the page's main thread was as late); the microphone muted, unmuted or ended (other than by
    // the page: its stop() fires nothing); and the context suspended (and how often by the idle
    // audio). Natter's own listeners go through EventTarget's addEventListener as it was before the
    // page's scripts ran, so neither the page nor the idle audio (listening) counts them, and they
    // only note a few numbers: the page's handler gets each event as it would without them, and the
    // sound is never read. A timer looks for the end every RECORDING_LOOK_MS while a recording runs,
    // and not at all otherwise. The log has a page's first recording (to compare with), then only
    // rough ones (a chunk handed over late; a chunk's worth skipped or more; a wait between two of
    // two chunks and a QUANTUM or more, as a main thread held long enough to hand one over late
    // makes; the context's clock behind by more than two chunks and 1%; the microphone muted or
    // ended; the context suspended; or no chunk at all with the microphone seen on at a look
    // RECORDING_EMPTY_MS or more after it began): RECORDING_LINES a page, apart from the others.
    // Once they are used up nothing is measured. Counts, durations and rates only.
    const RECORDING_LINES = 6;
    const RECORDING_LOOK_MS = 1000;
    const RECORDING_MOST_MS = 10 * 60000;
    const RECORDING_EMPTY_MS = 1000;
    const RECORDING_AFTER_MS = 2000;
    const RECORDING_AFTER_MOST = 512;  // chunks after, for the smallest chunks
    const QUANTUM = 128;  // frames: WebKit's audio thread writes a chunk this many at a time
    const RECORDING_SIZES = [128, 256, 512, 1024, 2048, 4096, 8192, 16384];  // bufferSize's (WebKit's at least 128)
    const RATE_LEAST = 1000;  // sample rates told are within these (playback.py takes no other)
    const RATE_MOST = 768000;
    const Events = window.EventTarget && window.EventTarget.prototype;
    const listen = Events && Events.addEventListener;
    const unlisten = Events && Events.removeEventListener;
    const recordings = new Map();  // each recorder recording now → its recording
    let recordingLines = 0;
    let recordingTimer = 0;
    const rateOf = (value) => {
      const rate = Math.round(Number(value));
      return rate >= RATE_LEAST && rate <= RATE_MOST ? rate : null;
    };
    // Whether any of a recording's microphone tracks is still on (no iterator: this runs each chunk).
    const onAir = (recording) => {
      for (let i = 0; i < recording.tracks.length; i++) if (recording.tracks[i].readyState === "live") return true;
      return false;
    };

    // Natter's own listener, taken off when the recording ends.
    function hear(recording, target, type, listener) {
      if (!target || typeof target.addEventListener !== "function") return;
      try {
        listen.call(target, type, listener);
        recording.heard.push([target, type, listener]);
      } catch (_) { /* not an EventTarget: not heard */ }
    }

    function hearTrack(recording, track) {
      recording.tracks.push(track);
      hear(recording, track, "mute", () => recording.muted++);
      hear(recording, track, "unmute", () => recording.unmuted++);
      hear(recording, track, "ended", () => recording.ended++);
    }

    function watchRecording(node, stream) {
      if (typeof listen !== "function" || typeof unlisten !== "function") return;
      let tracks;
      try { tracks = [...stream.getTracks()]; } catch (_) { return; }
      const current = recordings.get(node);
      if (current && onAir(current)) {  // one more microphone into the recording under way
        for (const track of tracks) if (!current.tracks.includes(track)) hearTrack(current, track);
        return;
      }
      // One whose microphones have stopped, its end not seen yet (no chunk or look since): this is
      // the next one.
      if (current) finish(current, "stopped");
      const context = node.context;
      const rate = context ? rateOf(context.sampleRate) : null;
      const size = Number(node.bufferSize);
      if (recordingLines >= RECORDING_LINES || !rate || !RECORDING_SIZES.includes(size) || !tracks.length) return;
      let microphone = null;
      for (const track of tracks) {
        try { microphone = microphone || rateOf(track.getSettings().sampleRate); } catch (_) { /* not said */ }
      }
      const chunk = size / rate;
      const after = Math.min(Math.ceil(RECORDING_AFTER_MS / 1000 / chunk), RECORDING_AFTER_MOST);
      const began = performance.now();
      const recording = {
        node, context, tracks: [], size, rate, microphone, chunk, began, path: shims.has(node) ? "worklet" : "native",
        // first, last: the first and last chunk's playbackTime; firstAt, lastAt: when they ran;
        // onAt: the last look that found the microphone on and the recorder still recording;
        // offsets: the latest chunks' playbackTime less a chunk for each before it, those still to
        // be judged and those after them; base: the earliest of the first chunk's and those after it
        chunks: 0, first: 0, last: 0, firstAt: 0, lastAt: 0, onAt: began, after, offsets: new Float64Array(after + 1),
        base: 0, longest: 0, late: 0, behind: 0, lost: 0, muted: 0, unmuted: 0, ended: 0, suspended: 0, rested: 0,
        heard: [],
      };
      recordings.set(node, recording);
      for (const track of tracks) hearTrack(recording, track);
      hear(recording, node, "audioprocess", (event) => handedOver(recording, event));
      hear(recording, context, "statechange", () => {
        const state = context.state;
        if (state === "closed") finish(recording, "closed");
        else if (state === "suspended" || state === "interrupted") {
          recording.suspended++;
          if (rested.has(context)) recording.rested++;
        }
      });
      if (!recordingTimer) recordingTimer = setInterval(lookAtRecordings, RECORDING_LOOK_MS);
    }

    // A chunk handed to the page. Each chunk: a few numbers, nothing kept.
    function handedOver(recording, event) {
      const now = performance.now();
      if (!onAir(recording) || now - recording.began >= RECORDING_MOST_MS) {
        finish(recording, onAir(recording) ? "long" : "stopped");
        return;
      }
      const time = Number(event.playbackTime) || 0;
      const count = recording.chunks;
      const offsets = recording.offsets;
      offsets[count % offsets.length] = time - count * recording.chunk;
      if (recording.path === "worklet") {
        const late = (Number(recording.context.currentTime) || 0) - (time - recording.chunk);
        recording.late = Math.max(recording.late, late);
        if (late >= recording.chunk + QUANTUM / recording.rate) recording.behind++;  // late, but whole
      }
      if (count) {
        recording.longest = Math.max(recording.longest, now - recording.lastAt);
      } else {
        recording.first = time;
        recording.firstAt = now;
      }
      if (count >= recording.after) judge(recording, count - recording.after, count);
      recording.chunks++;
      recording.last = time;
      recording.lastAt = now;
    }

    // How late chunk `k` was handed over: past the earliest of it and those after it, up to chunk
    // `newest`. A chunk and a QUANTUM late or more, its buffer was being filled again: what was
    // written into it by then, a chunk at most, is lost. (Natter's worklet's chunks: only where
    // the offsets start from, for the sound not gathered; how late each came is measured as it comes.)
    function judge(recording, k, newest) {
      const offsets = recording.offsets;
      let earliest = Infinity;
      for (let i = k; i <= newest; i++) earliest = Math.min(earliest, offsets[i % offsets.length]);
      if (!k) recording.base = earliest;
      if (recording.path === "worklet") return;
      const late = offsets[k % offsets.length] - earliest;
      const chunk = recording.chunk;
      recording.late = Math.max(recording.late, late);
      if (late >= chunk + QUANTUM / recording.rate) {
        recording.behind++;
        recording.lost += Math.min(late - chunk, chunk);
      }
    }

    // Each recording's end, or while it runs on, the wait since its last chunk (a clock that stops
    // for good hands over none after it) and that its microphone was on.
    function lookAtRecordings() {
      const now = performance.now();
      for (const recording of [...recordings.values()]) {
        const out = outputs.get(recording.node);
        if (recording.context.state === "closed") finish(recording, "closed");
        else if (!onAir(recording)) finish(recording, "stopped");
        else if (recording.chunks && !(out && out.size)) finish(recording, "disconnected");
        else {
          if (recording.chunks) recording.longest = Math.max(recording.longest, now - recording.lastAt);
          recording.onAt = now;
          if (now - recording.began >= RECORDING_MOST_MS) finish(recording, "long");
        }
      }
    }

    // `end`: what ended it: "stopped" (its microphone), "disconnected" (the recorder), "closed" (its
    // context) or "long" (RECORDING_MOST_MS of it).
    function finish(recording, end) {
      if (recordings.get(recording.node) !== recording) return;
      recordings.delete(recording.node);
      for (const [target, type, listener] of recording.heard) {
        try { unlisten.call(target, type, listener); } catch (_) { /* gone with it */ }
      }
      if (!recordings.size) {
        clearInterval(recordingTimer);
        recordingTimer = 0;
      }
      const chunks = recording.chunks;
      for (let k = Math.max(0, chunks - recording.after); k < chunks; k++) judge(recording, k, chunks - 1);
      const chunk = recording.chunk * 1000;
      // From the first chunk to the last one's end, or to the last look that found the microphone
      // on where that's later; with no chunk, from the start to then (or to now, while it's on).
      const lasted = chunks ? Math.max(recording.lastAt + chunk, recording.onAt) - recording.firstAt
        : (onAir(recording) ? performance.now() : recording.onAt) - recording.began;
      const clock = chunks ? (recording.last - recording.first) * 1000 + chunk : 0;
      const offsets = recording.offsets;
      const skipped = chunks ? (offsets[(chunks - 1) % offsets.length] - recording.base) * 1000 : 0;
      if (recordingLines >= RECORDING_LINES || (!chunks && lasted < RECORDING_EMPTY_MS)) return;
      const rough = !chunks || recording.behind > 0 || skipped >= chunk
        || recording.longest >= 2 * chunk + QUANTUM / recording.rate * 1000
        || lasted - clock > 2 * chunk + lasted / 100 || recording.muted > 0 || recording.ended > 0
        || recording.suspended > 0;
      if (recordingLines > 0 && !rough) return;
      recordingLines++;
      post({
        type: "media", what: "recording", path: recording.path, size: recording.size, rate: recording.rate,
        microphone: recording.microphone,
        chunks: counted(chunks), recorded: counted(chunks * chunk), clock: counted(clock), lasted: counted(lasted),
        longest: counted(recording.longest), late: counted(recording.late * 1000), behind: counted(recording.behind),
        lost: counted(recording.lost * 1000), skipped: counted(skipped), muted: counted(recording.muted),
        unmuted: counted(recording.unmuted),
        ended: counted(recording.ended), suspended: counted(recording.suspended), rested: counted(recording.rested),
        end,
      });
    }
    onRecorderFed = watchRecording;

    // Which script processor the page got, Natter's worklet recorder or WebKit's own, and why, once a
    // page for each: the worklet's module loaded (and from which kind of URL), its context made while no
    // spare was ready or with other options (and not rested since), still loading, failed to load into
    // it (the error's name), refused (both kinds, by the page's policy or after failing again and again;
    // the last error's name), no AudioWorklet here, too many contexts with it, a shape it doesn't take,
    // or its node failed to be made (the error's name); or the worklet broke; and the module loaded
    // into a rested context the page resumed meanwhile ("restarted": its output likely was).
    onRecorderPath = (path, why, module, name) => {
      tellMedia({ what: "recorder", path, why, module, name }, `recorder ${path} ${why} ${module} ${name}`);
    };

    onWakeFailed = (error) => {
      const name = errorName(error);
      tellMedia({ what: "wake", name }, `wake ${name}`);
    };

    // WebCodecs' AudioDecoder, for Opus only: supported, configured, or its error's name.
    const Decoder = window.AudioDecoder;
    if (typeof Decoder === "function") {
      const opus = (config) => {
        try { return Boolean(config) && String(config.codec).toLowerCase() === "opus"; } catch (_) { return false; }
      };
      const tellDecoder = (step, error) => {
        const name = error === undefined ? "" : errorName(error);
        tellMedia({ what: "decoder", step, name }, `decoder ${step} ${name}`);
      };
      const decoderOf = new WeakMap();  // each decoder → what its error callback knows it by
      const opusDecoders = new WeakSet();  // those, for decoders configured for Opus now
      class AudioDecoder extends Decoder {
        constructor(init) {
          let given = init;
          const mine = {};  // stands for the decoder in its error callback, which comes first
          if (init && typeof init === "object" && typeof init.error === "function") {
            const error = init.error;
            given = Object.create(init, { error: { enumerable: true, value(problem) {
              if (opusDecoders.has(mine)) tellDecoder("error", problem);
              return error.apply(this, arguments);
            } } });
          }
          super(given);
          decoderOf.set(this, mine);
        }
        configure(config) {
          const isOpus = opus(config);
          try {
            super.configure(config);
          } catch (problem) {
            if (isOpus) tellDecoder("configure-failed", problem);
            throw problem;
          }
          const mine = decoderOf.get(this);
          if (mine) { if (isOpus) opusDecoders.add(mine); else opusDecoders.delete(mine); }
          if (isOpus) tellDecoder("configured");
        }
        static isConfigSupported(config) {
          const result = super.isConfigSupported(config);
          if (opus(config) && result && typeof result.then === "function") {
            result.then((answer) => tellDecoder(answer && answer.supported ? "supported" : "unsupported"),
                        (problem) => tellDecoder("check-failed", problem));
          }
          return result;
        }
      }
      Object.defineProperty(window, "AudioDecoder", { value: AudioDecoder, writable: true, configurable: true });
    }
  }

  // ---- Opening a chat -----------------------------------------------------------------------

  // Where to press for the chat named `name` in the list: where its row has its name (rowName).
  function rowFor(name) {
    const pane = find("pane");
    if (!pane) return null;
    for (const row of pane.querySelectorAll(ROWS)) {
      const found = rowName(row);
      if (found && found[1] === name) return found[0];
    }
    return null;
  }

  function press(element) {
    const box = element.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, view: window, button: 0,
                 clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 };
    element.dispatchEvent(new PointerEvent("pointerdown", { ...at, buttons: 1, pointerType: "mouse" }));
    element.dispatchEvent(new MouseEvent("mousedown", { ...at, buttons: 1 }));
    element.dispatchEvent(new PointerEvent("pointerup", { ...at, pointerType: "mouse" }));
    element.dispatchEvent(new MouseEvent("mouseup", at));
    element.dispatchEvent(new MouseEvent("click", at));
  }

  function searchBox() {
    return find("search");
  }

  function setSearch(text) {
    const box = searchBox();
    if (!box) return false;
    box.focus();
    if (box.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand(text ? "insertText" : "delete", false, text || null);
    } else {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(box, text);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return true;
  }

  function searching() {
    const box = searchBox();
    return Boolean(box && (box.isContentEditable ? box.textContent : box.value));
  }

  function focusComposer() {
    const composer = document.querySelector('#main footer [contenteditable="true"]');
    if (composer) composer.focus();
    return Boolean(composer);
  }

  async function openChat(name) {
    name = plainName(name);  // names Natter saved before it dropped those marks itself still have them
    if (activeChat() === name) return focusComposer() || true;
    let target = rowFor(name);
    if (!target && setSearch(name)) {
      for (let i = 0; i < 40 && !target; i++) {
        await sleep(100);
        target = rowFor(name);
      }
    }
    if (!target) {
      if (searching()) setSearch("");
      return false;
    }
    press(target);
    for (let i = 0; i < 20 && activeChat() !== name; i++) await sleep(100);
    if (searching()) setSearch("");
    await sleep(50);
    focusComposer();
    return activeChat() === name;
  }

  // A wa.me link, clicked the way one in a message is, so WhatsApp can open its chat without a
  // reload. Says whether anything in the page took the click; if nothing did, the click goes
  // nowhere and Natter loads the link's address instead.
  // Links WhatsApp opens itself when one is clicked in its page: chats (wa.me), group invites
  // (its "Join group" preview) and channels.
  const PAGE_LINKS = /^https:\/\/(wa\.me\/|chat\.whatsapp\.com\/|whatsapp\.com\/channel\/)/;

  function openLink(href) {
    const url = new URL(href);
    if (!PAGE_LINKS.test(url.href)) return false;
    const link = document.createElement("a");
    link.href = url.href;
    link.rel = "noopener noreferrer";
    let taken = true;  // unless the click comes back to the window untouched
    const untouched = (event) => {
      if (event.target !== link) return;
      taken = event.defaultPrevented;
      event.preventDefault();
    };
    window.addEventListener("click", untouched);
    document.body.appendChild(link);
    try {
      link.click();
    } finally {
      link.remove();
      window.removeEventListener("click", untouched);
    }
    return taken;
  }

  // ---- Attaching files ----------------------------------------------------------------------
  // WhatsApp's Content-Security-Policy won't let the page fetch anything Natter could serve, so
  // Natter hands files over itself, a few MB per call, and they're put back together here.
  // Then they're pasted into the message box, which brings up WhatsApp's own preview with a
  // caption box, whether they came from Ctrl+V, a drop, the dock or Send To.

  const incoming = new Map();

  // Files dragged in from outside are dropped through Natter, so the page doesn't see that
  // drag: WebKit would show WhatsApp a drag of "Files" it can't read, WhatsApp would put up its
  // drop zone for a drop that never comes, and then ignore the files Natter hands over.
  for (const type of ["dragenter", "dragover", "dragleave", "drop"]) {
    window.addEventListener(type, (event) => {
      if (event.isTrusted && event.dataTransfer && [...event.dataTransfer.types].includes("Files")) {
        event.stopImmediatePropagation();
        if (type === "dragover") event.preventDefault();  // "a drop is welcome here"
      }
    }, true);
  }

  function fromBase64(text) {
    if (Uint8Array.fromBase64) return Uint8Array.fromBase64(text);
    const raw = atob(text);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function receive(id, chunk) {
    if (!incoming.has(id)) incoming.set(id, []);
    incoming.get(id).push(fromBase64(chunk));
    return true;
  }

  function discard(ids) {
    ids.forEach((id) => incoming.delete(id));
  }

  function pasteInto(composer, transfer) {
    composer.focus();
    return composer.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  }

  async function attach(items) {
    const parts = items.map((item) => incoming.get(item.id));
    discard(items.map((item) => item.id));
    if (parts.some((part) => !part)) return false;
    const files = items.map((item, i) => new File(parts[i], item.name, { type: item.type }));
    if (files.some((file, i) => file.size !== items[i].size)) return false;  // a piece went missing
    return handOver(files);
  }

  // WhatsApp's preview (picture, video or document, with a caption box) opens in the middle
  // drawer, over the chat. While it's open the chat's message box drops files (its attach
  // command does nothing when a middle drawer exists), so they go in through the preview's own
  // "Add file" input instead: the handler of its + button and of a drop on it, any kind of file.
  // WhatsApp's attach menu adds its inputs to <body> itself; the preview's sits inside it.
  function previewPicker() {
    const drawer = find("drawer-middle");
    const inDrawer = drawer && drawer.querySelector(WHERE.preview[0]);
    if (inDrawer) return inDrawer;
    return [...document.querySelectorAll('#app input[type="file"][multiple]')]
      .find((input) => input.parentElement !== document.body) || null;
  }

  function handOver(files) {
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    const picker = previewPicker();
    if (picker) {
      picker.files = transfer.files;
      picker.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    const message = document.querySelector('#main footer [contenteditable="true"]');
    if (!message) return false;
    // Pasted into the message box, WhatsApp opens its preview. (A drop made by a script it
    // accepts and then ignores.) A handler that takes the files cancels the event.
    return !pasteInto(message, transfer);
  }

  // Files straight from disk: Natter clicks an input of its own (WebKit counts a script Natter
  // runs as the user's doing), WebKit asks Natter which files, and Natter answers with the ones
  // dropped or pasted (account.py). They're never copied through Natter or the page, however big.
  // null: WebKit never asked (Natter then sends them piece by piece).
  const PICK_TAG = "application/x-natter-files";
  const PICK_WAIT_MS = 3000;

  function pick() {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.accept = PICK_TAG;
      input.style.display = "none";
      let settled = false;
      const settle = (value) => {
        if (settled) return;
        settled = true;
        input.remove();
        resolve(value);
      };
      input.addEventListener("change", () => settle(input.files.length ? handOver([...input.files]) : false), { once: true });
      input.addEventListener("cancel", () => settle(false), { once: true });
      document.documentElement.appendChild(input);
      input.click();
      setTimeout(() => settle(null), PICK_WAIT_MS);
    });
  }

  // ---- Downloads ------------------------------------------------------------------------------
  // WhatsApp saves a file by clicking a link to a blob: URL of it, or by opening that URL, and may
  // revoke the URL straight after. A URL opened in a new window Natter saves itself
  // (Account._on_policy), a moment later, and a revoked blob can't be fetched any more. So a blob:
  // URL clicked or opened in the last minute is revoked a minute after that instead. Natter is told
  // the file's name too (the link's download name, or the File's), which it saves it under.
  {
    const KEEP_MS = 60000;
    const KEPT = 32;  // URLs WhatsApp never revokes don't pile up
    const used = new Map();  // a blob: URL and when it was clicked or opened
    const names = new Map();  // a blob: URL of a File and the File's name
    const bound = (map) => { if (map.size > KEPT) map.delete(map.keys().next().value); };
    const createObjectURL = URL.createObjectURL;
    const revokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = function (object) {
      const url = createObjectURL.call(this, object);
      try {
        if (typeof File === "function" && object instanceof File && object.name) {
          names.set(url, String(object.name));
          bound(names);
        }
      } catch (_) { /* not a File */ }
      return url;
    };
    URL.revokeObjectURL = function (url) {
      const key = String(url);
      const at = used.get(key);
      const wait = at === undefined ? 0 : at + KEEP_MS - Date.now();
      if (wait > 0) {
        setTimeout(() => URL.revokeObjectURL(key), wait);
        return undefined;
      }
      used.delete(key);
      names.delete(key);
      return revokeObjectURL.call(this, url);
    };

    function saving(url, name) {
      const key = String(url || "");
      if (!key.startsWith("blob:")) return;
      used.delete(key);
      used.set(key, Date.now());
      bound(used);
      post({ type: "download", url: key, name: String(name || names.get(key) || "") });
    }
    const clicked = (link) => {
      try { if (link && link.href) saving(link.href, link.getAttribute("download")); } catch (_) { /* not a link */ }
    };
    // a.click() and a click event sent to a link, in the page or not (FileSaver's way)...
    const Link = window.HTMLAnchorElement;
    if (Link && Link.prototype) {
      const click = Link.prototype.click;
      if (typeof click === "function") {
        Link.prototype.click = function () { clicked(this); return click.apply(this, arguments); };
      }
      const dispatch = window.EventTarget && window.EventTarget.prototype.dispatchEvent;
      if (typeof dispatch === "function") {
        Link.prototype.dispatchEvent = function (event) {
          if (event && event.type === "click") clicked(this);
          return dispatch.apply(this, arguments);
        };
      }
    }
    // ...the user's own click on one (seen on its way down, before the link acts)...
    window.addEventListener("click", (event) => {
      const target = event.isTrusted && event.target;
      if (target && typeof target.closest === "function") clicked(target.closest("a[href]"));
    }, true);
    // ...and window.open().
    const open = window.open;
    if (typeof open === "function") {
      window.open = function (url) {
        saving(url, "");
        return open.apply(this, arguments);
      };
    }
  }

  // ---- Replying from a notification ---------------------------------------------------------
  // A reply goes the way the person's own hands would take it, once: WhatsApp's own handler for
  // its notification opens that chat (unless it's open already), the words go into the chat's
  // empty message box through the page's own editing commands, and WhatsApp's Send button is
  // pressed once. Never the Enter key (what it does follows WhatsApp's "Enter is send" setting),
  // never twice, never again after a failure, and nothing of WhatsApp's code or storage. Anything
  // that doesn't look exactly right ends it before Send, with the reason: nothing is guessed.
  // The answers (account.py REPLY_RESULTS):
  //   "sent"           Send pressed, and WhatsApp took the words out of the box (or drew a new message)
  //   "unsure"         Send pressed, not seen to go: it may have, so it's never tried again
  //   "left-as-draft"  typed, not sent: the words may be in the chat's message box
  //   "draft" (the chat has words of the person's own in its box, or a message being replied to),
  //   "busy" (something's in the way), "cant-send" (the chat takes no messages from them),
  //   "mismatch" (the chat that opened isn't that one, or can't be told to be), "gone" (no way to
  //   open it), "not-ready",
  //   "calling", "too-long": nothing typed

  const CHAT_ID = /^\d{5,24}(-\d{5,24})?@(c\.us|g\.us|lid)$/;  // people and groups (account.py CHAT_ID)
  const REPLY_LIMIT = 4096;  // UTF-16 code units, as String.length counts them (account.py REPLY_LIMIT)
  const CHAT_WAIT_MS = 10000;  // for the chat to open
  const DRAFT_QUIET_MS = 250;  // a chat just opened puts its draft back in the box: until the box is still
  const DRAFT_WAIT_MS = 1500;
  const LINE_WAIT_MS = 2000;  // for each line to land in the box
  const SEND_WAIT_MS = 5000;  // for WhatsApp's Send button, which turns up a timer tick after the words
  const SENT_WAIT_MS = 10000;  // for the box to empty once Send is pressed
  const ROWS_WAIT_MS = 1000;  // once a chat's first messages are drawn, for the rest of it
  const FRAME_WAIT_MS = 300;  // for an animation frame, in what Natter's log is told (diagnose)
  // Kept back from a wait for a chat's messages, for what comes after it: the draft put back, a
  // line typed, the Send button. Less than that left once the chat is checked, and nothing is typed.
  const TYPE_RESERVE_MS = DRAFT_WAIT_MS + LINE_WAIT_MS + SEND_WAIT_MS;
  // WhatsApp's own swap of a text emoticon just before the caret for an emoji ("Replace text
  // with emoji"); the thumbs up and down in the skin tone the person picked.
  const EMOTICONS = [["(y)", "\u{1F44D}"], ["(Y)", "\u{1F44D}"], ["(n)", "\u{1F44E}"], ["(N)", "\u{1F44E}"],
    [":-)", "\u{1F642}"], [":-(", "\u{1F641}"], [":-p", "\u{1F61B}"], [":-P", "\u{1F61B}"], [":-\\", "\u{1F615}"],
    [":-D", "\u{1F600}"], [":-o", "\u{1F62E}"], [";-)", "\u{1F609}"], [":-*", "\u{1F618}"], [":-|", "\u{1F610}"],
    [":'(", "\u{1F622}"], ["^_^", "\u{1F601}"], ["<3", "\u2764"], [">_<", "\u{1F606}"]];
  const SKIN_TONE = /^[\u{1F3FB}-\u{1F3FF}]?$/u;
  const BLOCKS = /^(P|DIV|LI|H[1-6]|BLOCKQUOTE|PRE)$/;

  // The box's words as the person sees them: a line per paragraph and per line break, emoji
  // pictures as the characters they stand for. A block that ends in a line break gets one more
  // <br> from the editor, for the caret; that one isn't a line.
  function boxText(element) {
    let out = "";
    let first = true;
    for (const node of element.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        out += node.textContent;
      } else if (node.nodeType !== Node.ELEMENT_NODE) {
        continue;
      } else if (node.tagName === "BR") {
        out += "\n";
      } else if (node.tagName === "IMG") {
        out += node.getAttribute("alt") || node.getAttribute("data-plain-text") || "";
      } else if (BLOCKS.test(node.tagName)) {
        let inner = boxText(node);
        if (node.lastChild && node.lastChild.nodeName === "BR") inner = inner.slice(0, -1);
        out += (first ? "" : "\n") + inner;
      } else {
        out += boxText(node);
      }
      first = false;
    }
    return out;
  }

  // Words compared as they read: the same characters (NFC), the same lines, spaces and invisible
  // marks aside. U+FE0F (which emoji pictures may or may not carry) aside too.
  function canonical(text) {
    return text.normalize("NFC").replace(INVISIBLE, "").replace(/\uFE0F/g, "").replace(/\u00A0/g, " ")
      .split("\n").map((line) => line.replace(/[ \t]+/g, " ").trim()).join("\n").replace(/^\n+|\n+$/g, "");
  }

  // Whether `landed` (canonical) is what typing `wanted` (canonical) gives: exactly, but for
  // WhatsApp's emoticon swap, which only ever happens at the caret, the end of each line typed.
  function sameText(landed, wanted) {
    if (landed === wanted) return true;
    const got = landed.split("\n");
    const want = wanted.split("\n");
    return got.length === want.length && want.every((line, i) => line === got[i] || EMOTICONS.some(([text, emoji]) => {
      if (!line.endsWith(text)) return false;
      const head = line.slice(0, -text.length);
      return got[i].startsWith(head + emoji) && SKIN_TONE.test(got[i].slice(head.length + emoji.length));
    }));
  }

  function boxEmpty(box) {
    return !canonical(boxText(box)) && !box.querySelector("img, [data-lexical-decorator]");
  }

  // WhatsApp's media editor, open in the middle drawer: a picture or file waiting to be sent. What
  // else WhatsApp opens there (an empty state beside Status, Calls or Settings; business tools)
  // doesn't count. Not seen is not open: a reply still only types into the chat's own empty box,
  // checks the words landed there, and presses that box's own Send, never the preview's.
  function previewOpen() {
    const drawer = find("drawer-middle");
    return Boolean(drawer && find("preview", drawer));
  }

  // Where someone types: boxes and text fields, not checkboxes, files or buttons.
  const FIELDS = "textarea, [contenteditable], input:not([type]), "
    + "text search email tel url number password".split(" ").map((type) => `input[type="${type}"]`).join(", ");

  function hasWords(field) {
    if (field.isContentEditable) return !boxEmpty(field);
    return "value" in field && !field.disabled && !field.readOnly && String(field.value).trim() !== "";
  }

  // Whether the left drawer holds words typed and not yet saved: a new group's name, a contact's,
  // a profile name being changed, a search. WhatsApp's click on a notification closes that drawer
  // and they'd be lost. An empty field (Settings' search, the Calls tab's) is nothing to lose, and
  // a profile shows its name as words, not in a field, until its pencil is pressed. Choices
  // without words (people picked for a new group, nothing typed yet) aren't seen.
  function drawerEdit() {
    const drawer = find("drawer-left");
    return Boolean(drawer) && [...drawer.querySelectorAll(FIELDS)].some(hasWords);
  }

  // The chats the open chat's messages belong to, by their keys, when they're "<mine>_<chat id>_<id>…".
  // WhatsApp now keys them by the message's own id alone, which names no chat: then there are none.
  function chatIds(main) {
    const ids = new Set();
    main.querySelectorAll(WHERE["msg-key"].join(", ")).forEach((row) => {
      const parts = (row.getAttribute("data-id") || "").split("_");
      if (parts.length >= 3 && (parts[0] === "true" || parts[0] === "false")) ids.add(parts[1]);
    });
    return ids;
  }

  // Whether the open chat `main` is `chat`, by its messages' keys. A group's are all the group's:
  // anything else there means another chat, say a person's holding a message quoted from the group
  // ("Reply privately"), should a quote carry a key of its own. A person's chat may hold their
  // keys under two of their ids (WhatsApp moving people to new ids), so there theirs is enough.
  function isChat(main, chat) {
    const ids = chatIds(main);
    return ids.has(chat) && (!chat.endsWith("@g.us") || ids.size === 1);
  }

  // Whether the open chat shows no messages yet: WhatsApp has opened it and is still drawing them
  // (or loading those around the one notified, which can take seconds).
  function keyless() {
    const main = find("main");
    return Boolean(main) && !main.querySelector(WHERE["msg-key"].join(", "));
  }

  // The ids of the messages in `main`, whatever their shape, for seeing one more turn up.
  function messageIds(main) {
    return new Set([...main.querySelectorAll(WHERE["msg-key"].join(", "))]
      .map((row) => row.getAttribute("data-id")).filter(Boolean));
  }

  // ---- The chat told without its id
  // With keys that name no chat, the chat that opened is told by its name in the header, which must
  // be the notification's title, and at least one more sign that it's the chat notified: the
  // notified message among its latest rows, the header's picture the notification's, the reply's own
  // click having opened it (from no chat, or from one with another name), or the chat list marking it
  // as the chat open, the only one of that name there and near its top, once a notification still
  // shown was clicked (listed). A name alone never: two chats can have the same one. Nor is a chat taken on its message
  // while its notification can be clicked: the click (WhatsApp's open by id) comes first. One
  // WhatsApp closed may not open its chat when clicked, so its message is held to more (`strict`):
  // longer words, and a group's sender's too. And a chat the click didn't open is never taken while
  // the chat list shows two of that name, shows only some chats, or names that can't be read
  // (namesakes): the click may open nothing, or the other one only later.

  const LAST_ROWS = 12;  // the latest rows of the open chat, where the notified message is looked for

  // Words as a message and its notification both have them: NFC, without spaces, invisible marks,
  // U+FE0F or WhatsApp's formatting marks (* _ ~ `), which a message shows as styles instead.
  const flat = (text) => String(text || "").normalize("NFC").replace(INVISIBLE, "").replace(/[\s\uFE0F*_~`]/g, "");

  // What WhatsApp's notifications say for a message without words of its own (a picture, a voice
  // message, a sticker, one deleted, a call missed, several at once), as far as Natter knows them;
  // and the marks they start with for a picture, video, voice message, document, place, contact or
  // poll. None of it is taken for the message: a chat with no such message doesn't show those words.
  const MEDIA_MARK = /^(\u{1F4F7}|\u{1F4F8}|\u{1F3A5}|\u{1F4F9}|\u{1F39E}|\u{1F3AC}|\u{1F3A4}|\u{1F3B5}|\u{1F3A7}|\u{1F4C4}|\u{1F4CE}|\u{1F4C1}|\u{1F4CD}|\u{1F5FA}|\u{1F464}|\u{1F465}|\u{1F4CA}|\u{1F5BC}|\u{1F6AB}|\u{1F4DE}|\u{1F4F2}|\u{1F517})/u;
  const GENERIC = /^(photo|image|picture|video|gif|audio|voicemessage|voicenote|sticker|document|file|location|livelocation|contact|contacts|poll|message|newmessage|\d+newmessages|\d+messages|thismessagewasdeleted|youdeletedthismessage|waitingforthismessage.*|missed(voice|video)?call|viewonce.*|incoming(voice|video)?call|reacted.*)$/;

  function generic(text) {
    const words = flat(text).toLowerCase().replace(/^[^\p{L}\p{N}]+/u, "").replace(/[.!…]+$/u, "");
    return MEDIA_MARK.test(String(text || "").trim()) || GENERIC.test(words);
  }

  // What a notification's body may be found as in its chat: the body, and in a group the words
  // after "<sender>: " (`strict`: with the sender's name in the same row; `label`: that name, flat). One cut short ("…")
  // stands for how the message starts. Under two characters (one emoji, say), or generic: nothing,
  // and a generic body nothing at all. `strict`: under four letters or digits (twelve for one cut
  // short), nothing either: "ok" or "Yes" could be any chat's.
  function bodies(body, strict) {
    const text = String(body || "");
    if (generic(text)) return [];
    const found = [];
    const add = (part, sender, label) => {
      let words = flat(part);
      const start = words.endsWith("…");
      if (start) words = words.slice(0, -1);
      const enough = strict ? (words.match(/[\p{L}\p{N}]/gu) || []).length >= (start ? 12 : 4) : [...words].length >= 2;
      if (enough && !generic(part)) found.push({ words, start, sender, label });
    };
    add(text, "", "");
    const colon = text.indexOf(": ");
    if (colon > 0) {
      const label = flat(text.slice(0, colon));
      add(text.slice(colon + 2), strict ? label : "", label);
    }
    return found;
  }

  // Each element's words in `element`, itself included, emoji pictures as theirs and icons aside
  // (as textOf), to `visit`.
  function eachText(element, visit) {
    const text = [...element.childNodes].map((node) => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent;
      if (node.tagName === "IMG") return node.getAttribute("alt") || node.getAttribute("data-plain-text") || "";
      return node.nodeType === Node.ELEMENT_NODE && !isIcon(node) ? eachText(node, visit) : "";
    }).join("");
    visit(text);
    return text;
  }

  // The open chat's latest message rows: WhatsApp's rows, else what carries its messages' keys.
  function latestRows(main) {
    const rows = [...main.querySelectorAll(WHERE["msg-row"].join(", "))];
    return (rows.length ? rows : [...main.querySelectorAll(WHERE["msg-key"].join(", "))]).slice(-LAST_ROWS);
  }

  // The words of each element in `element`, flat.
  function textsIn(element) {
    const texts = [];
    eachText(element, (text) => texts.push(flat(text)));
    return texts;
  }

  // Whether WhatsApp marks which way `main`'s messages went (message-in, message-out) at all. Without
  // those classes a message that came in can't be told from the person's own (hasMessage).
  const directed = (main) => Boolean(main.querySelector(".message-in, .message-out"));
  // Whether the person sent the message in `row` (WhatsApp's class for one's own), not received it.
  const ownRow = (row) => Boolean(row.querySelector(".message-out")) && !row.querySelector(".message-in");
  // Whether `row` holds a message that came in (WhatsApp's class for one).
  const inRow = (row) => row.matches(".message-in") || Boolean(row.querySelector(".message-in"));

  // Who WhatsApp says sent `row`'s message, flat: the name in its copyable text's
  // data-pre-plain-text ("[10:42, 23/10/2026] Name: "), or "" where it says none.
  function saidBy(row) {
    const text = row.querySelector("[data-pre-plain-text]");
    const found = text && /^\[[^\]]*\]\s*([\s\S]*):\s*$/.exec(text.getAttribute("data-pre-plain-text"));
    return found ? flat(found[1]) : "";
  }

  // Where `element` sits in `row`: each element up to the row, by its tag and its attributes' names.
  function placeIn(element, row) {
    const steps = [];
    for (let node = element; node && node !== row; node = node.parentElement) {
      steps.push(node.tagName + "[" + node.getAttributeNames().sort().join(" ") + "]");
    }
    return steps.join("<");
  }

  // The innermost element in `row` whose words are `sender` (flat), as textsIn reads them, or null.
  function senderIn(row, sender) {
    const all = [...row.querySelectorAll("*")].filter((element) => !isIcon(element) && flat(textOf(element)) === sender);
    return all.find((element) => !all.some((other) => other !== element && element.contains(other))) || null;
  }

  // Whether `row`, a group's message, shows `sender` (flat) as who sent it. WhatsApp names the
  // sender on the first row of a run of their messages only: so the row itself, or else the nearest
  // row above it in the same run of messages coming in that shows the name, with no row between
  // (nor `row`) holding anything where that row has the name: another sender's name, so another run.
  // Nor any two of those rows that WhatsApp says were sent by different people (saidBy), wherever
  // it draws their names.
  function sentBy(main, row, sender) {
    if (textsIn(row).includes(sender)) return true;
    const rows = [...main.querySelectorAll(WHERE["msg-row"].join(", "))];
    const at = rows.indexOf(row);
    if (at < 0 || !inRow(row)) return false;
    for (let i = at - 1; i >= 0 && inRow(rows[i]) && !ownRow(rows[i]); i--) {
      const name = senderIn(rows[i], sender);
      if (!name) continue;
      const place = placeIn(name, rows[i]);
      if (new Set(rows.slice(i, at + 1).map(saidBy).filter(Boolean)).size > 1) return false;
      return rows.slice(i + 1, at + 1).every((other) => ![...other.querySelectorAll("*")]
        .some((element) => placeIn(element, other) === place && flat(textOf(element))));
    }
    return false;
  }

  // A time as WhatsApp draws one by each message (flat): "10:42", "9.05pm".
  const TIME = /^\d{1,2}[:.]\d{2}(?:[ap]\.?m\.?)?$/i;
  const letters = (words) => (words.match(/[\p{L}\p{N}]/gu) || []).length;
  // A long message WhatsApp shows cut short ("Read more"): this many letters or digits of it at least.
  const CUT_LETTERS = 24;

  // Whether `row` shows `want` (one of bodies'), by the normalised rule: an element there with just
  // its words (for a body cut short, starting with them), the row's time drawn in after them aside;
  // the row's words as a whole, its time aside, being an element's words (a sender's label) and then
  // those; or a long message WhatsApp shows cut short, CUT_LETTERS of it at least, that they start with.
  // A label that isn't the notified sender's may be a word of the message set apart (bold, a link, an
  // @mention): the words after it must then have four letters or digits at least.
  function holds(row, want) {
    const texts = textsIn(row);
    // The row's time: the words of the last element in it that has only a time.
    const time = texts.filter((words) => TIME.test(words)).pop() || "";
    const untimed = (words) => (time && words.endsWith(time) ? words.slice(0, -time.length) : words);
    const same = (words) => (want.start ? words.startsWith(want.words) : words === want.words);
    if (texts.some((words) => same(words) || same(untimed(words)))) return true;
    const whole = untimed(texts[texts.length - 1] || "");
    const after = (label) => whole.slice(label.length);
    if (texts.some((label) => label && label !== whole && whole.startsWith(label) && same(after(label))
        && (label === want.label || letters(after(label)) >= 4))) {
      return true;
    }
    return !want.start && texts.some((words) => {
      const shown = untimed(words).replace(/(…|\.\.\.)$/u, "");
      return shown !== want.words && want.words.startsWith(shown) && letters(shown) >= CUT_LETTERS;
    });
  }

  // Whether the notified message shows in `main`: something in one of its latest rows that came in
  // (inRow: not the person's own), with its words (holds), and with `strict` a group's sender's name
  // there too, or on the row that starts its run (bodies, sentBy). Where WhatsApp marks no row with
  // which way it went (directed), any of those rows, but only as `sole()` allows (chatSigns): the
  // words might be the person's own.
  function hasMessage(main, body, strict, sole = () => false) {
    const wanted = bodies(body, strict);
    if (!wanted.length) return false;
    const marked = directed(main);
    if (!marked && !sole()) return false;
    return latestRows(main).some((row) => (!marked || inRow(row))
      && wanted.some((want) => holds(row, want) && (!want.sender || sentBy(main, row, want.sender))));
  }

  // For the log: whether the notified body is in any of the latest rows by the normalised rule
  // (holds), whichever way they went, and whether it's within any of their words as a whole at all.
  function messageFacts(main, body) {
    const wanted = bodies(body, false);
    const rows = latestRows(main);
    return {
      messageAnywhere: rows.some((row) => wanted.some((want) => holds(row, want))),
      messageWords: rows.some((row) => {
        const whole = textsIn(row).pop() || "";
        return wanted.some((want) => whole.includes(want.words));
      }),
    };
  }

  // Whether `icon` may be told a chat by: a blob: picture or one from WhatsApp's picture hosts. Not
  // a data: URL or one from its static host, which may be a stand-in that every chat without a
  // picture has.
  function pictureAddress(icon) {
    if (/^blob:/i.test(icon)) return true;
    const host = /^https:\/\/([^/?#]*)/i.exec(icon);
    const name = host ? host[1].toLowerCase() : "";
    return /^[a-z0-9.-]+\.whatsapp\.net$/.test(name) && name !== "static.whatsapp.net";
  }

  // A picture's address as pictures are told apart: a blob: one whole, and one on WhatsApp's picture
  // hosts by its host and path, as the same picture comes with other query strings (its signature,
  // expiry, size). "" for one that can't tell a chat (pictureAddress).
  function pictureKey(address) {
    const text = String(address || "");
    if (!pictureAddress(text)) return "";
    if (/^blob:/i.test(text)) return text;
    const parts = /^https:\/\/([^/?#]*)([^?#]*)/i.exec(text);
    return `https://${parts[1].toLowerCase()}${parts[2] || "/"}`;
  }

  // What kind of address a picture has, for the log: data, blob, wa (https, on *.whatsapp.net),
  // https-other (any other address) or none. Never the address.
  function pictureKind(address) {
    const text = String(address || "");
    if (!text) return "none";
    if (/^data:/i.test(text)) return "data";
    if (/^blob:/i.test(text)) return "blob";
    const host = /^https:\/\/([^/?#]*)/i.exec(text);
    return host && /^([a-z0-9-]+\.)*whatsapp\.net$/i.test(host[1]) ? "wa" : "https-other";
  }

  // The pictures in a header, not those in the chat's name (its emoji are pictures too).
  function headerPictures(header) {
    const title = find("header-name", header);
    return [...header.querySelectorAll("img")].filter((img) => !(title && title.contains(img)));
  }

  // Whether `main`'s header shows the notification's picture, by its address (pictureKey).
  function samePicture(main, icon) {
    const header = find("header", main);
    const key = pictureKey(icon);
    return Boolean(header && key) && headerPictures(header).some((img) => pictureKey(img.getAttribute("src")) === key);
  }

  // Which of the header's names (namesOf) is `title`, or undefined.
  const nameAmong = (names, title) => names.find((name) => sameName(name.name, title));

  // Each sign of whether the chat open in `main` is `notification`'s (its title, body and icon),
  // without its id. `before`: the open chat before the reply's click ({main, names: its namesOf,
  // closed: whether the notification clicked had been closed}), or null for no click.
  function chatSigns(main, notification, before) {
    const name = Boolean(nameAmong(namesOf(find("header", main)), notification.title));
    // The words in rows not marked with which way they went: only for a notification still shown
    // as of when the reply was asked and clicked, with its chat near the list's top (topRow).
    const sole = () => Boolean(before) && !before.closed && Boolean(topRow(notification.title));
    return {
      name,
      // The click opened it: from no chat, or from one with names, none of them this one.
      navigated: Boolean(before) && name && (!before.main
        || (before.names.length > 0 && !nameAmong(before.names, notification.title))),
      body: hasMessage(main, notification.body, !before || before.closed, sole),
      avatar: samePicture(main, notification.icon),
      // The list marks it as the chat open, the only one of that name there, once a notification
      // still shown as of when the reply was asked was clicked (WhatsApp's open by its id).
      listed: Boolean(before) && !before.closed && name && listedAlone(notification.title),
    };
  }

  // Whether the chat open in `main` is `notification`'s, told without its id: by its name, and the
  // notified message there, its picture, the reply's click having opened it, or the list marking it
  // as the chat open (`before`, as for chatSigns). Which, for Natter's log ("name+body"); "" if it
  // isn't, or can't be told.
  function sameChat(main, notification, before) {
    if (!main || !notification) return "";
    const signs = chatSigns(main, notification, before);
    if (!signs.name) return "";
    return signs.body ? "name+body" : signs.avatar ? "name+avatar" : signs.navigated ? "name+click"
      : signs.listed ? "name+listed" : "";
  }

  // How many chats in the list (those WhatsApp has drawn) have the name `title`. Two or more, and
  // a chat of that name the click didn't open is never taken for the one notified: it may be the
  // other, with the same words, while WhatsApp's click opens nothing, or opens it later.
  // Not known, null: with a search typed or a filter chip on, the list isn't all the chats (scan);
  // and names that can't be read can't be counted: no chat drawn, one drawn that may be a chat's
  // (listRows: with WhatsApp's cell for one or not) without a name to read, or the open chat's row
  // read as a name its header doesn't show (misread). A row with the name that isn't told for a
  // chat's counts all the same: it may be one.
  function namesakes(title) {
    if (searching() || filtered()) return null;
    const rows = listRows();
    const chats = rows.filter((entry) => entry.chat);
    if (!chats.length || chats.some((entry) => !entry.name) || misread(rows)) return null;
    return rows.filter((entry) => sameName(entry.chat ? entry.name : nameOf(entry.row), title)).length;
  }

  // The rows the chat list marks as the open chat's (selectedRow): how many, and whether there's
  // just one and it has the name `title`.
  function listMark(title) {
    const marked = namedRows().filter(([, row]) => selectedRow(row));
    return { selected: marked.length, selectedName: marked.length === 1 && sameName(marked[0][0], title) };
  }

  // The places at the top of the list where a chat just written to sits: up to three pinned
  // chats, then the latest.
  const TOP_ROWS = 4;

  // The list's one row of the name `title`, when the list shows all the chats (namesakes) from its
  // top and has that row among its first TOP_ROWS chats; else null. The notified chat has just had
  // a message, so it sits up there: one of the same name drawn there would be counted, and one not
  // drawn (the list scrolled down) is never left out. Archived chats aren't in the list at all. A
  // place above it that only the list's own row is drawn at (listRows: the Archived row, told by
  // its icon) isn't a chat's; any other is, drawn or not, or still loading, words or none. The
  // rows' height is told by all the rows drawn, those still loading too.
  function topRow(title) {
    if (namesakes(title) !== 1) return null;
    const rows = listRows();
    const height = rowHeight(rows.map((entry) => entry.offset));
    // A row's place by its aria-rowindex or translateY only, NaN if neither tells it.
    const place = (entry) => positionOf(entry.row, entry.offset, height, entry.offset === 0 ? 0 : NaN);
    const named = rows.filter((entry) => entry.chat && sameName(entry.name, title));
    // The list's top drawn: a chat with its name there, or the list's own row.
    if (named.length !== 1 || !rows.some((entry) => place(entry) === 0 && (entry.name || entry.own))) return null;
    const at = place(named[0]);
    let above = 0;
    for (let p = 0; p < at && above < TOP_ROWS; p++) {
      const there = rows.filter((entry) => place(entry) === p);
      if (!there.length || !there.every((entry) => entry.own)) above++;
    }
    return Number.isFinite(at) && above < TOP_ROWS ? named[0].row : null;
  }

  // Whether the list, showing all the chats from its top, has one row of the name `title` near the
  // top (topRow), and marks that row, and only it, of all its rows, as the chat open.
  function listedAlone(title) {
    const row = topRow(title);
    const marked = listRows().filter((entry) => selectedRow(entry.row));
    return Boolean(row) && marked.length === 1 && marked[0].row === row;
  }

  // ---- What the log may say of the page
  // When a reply finds the chat isn't the one, or can't tell (account.py reply_diag): whether
  // WhatsApp is drawing the page at all, what the open chat holds, by counts and shapes, which signs
  // of the chat held, what the notification carried, and the names of the attributes on the chat,
  // its header and its first message, and the shapes of those values with an @ or _ in them. Never
  // a name, words, an id or their lengths.

  const DIAG_NAMES = 16;  // attribute or key names, at most, for each element or object
  const DIAG_VALUES = 8;

  // A value's shape: each run of digits as 9, of letters as a, of anything else but _ @ . : - as ?,
  // from its first 40 characters.
  const shapeOf = (value) => String(value).slice(0, 40).replace(/[0-9]+/g, "9").replace(/[A-Za-z]+/g, "a")
    .replace(/[^9a_@.:-]+/g, "?");

  // A name the log may have: code's (an attribute's, a key's), in kebab case, never one like an id.
  function diagName(name) {
    const kebab = String(name).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
    return /^[a-z][a-z0-9-]{0,31}$/.test(kebab) && (kebab.match(/[0-9]/g) || []).length <= 2 ? kebab : "";
  }

  const diagNames = (names) => [...new Set(names.map(diagName).filter(Boolean))].sort().slice(0, DIAG_NAMES);
  // The shapes of the names of the icons in `element` (as iconNames reads them), not the names.
  const iconShapes = (element) => [...new Set([...element.querySelectorAll("svg title, [data-icon]")]
    .map((icon) => shapeOf(icon.getAttribute("data-icon") || icon.textContent || "")).filter(Boolean))].sort()
    .slice(0, DIAG_VALUES);
  const attributeNames = (element) => (element ? diagNames(element.getAttributeNames()) : []);

  // Attributes whose values are keys rather than words: the log may have their values' shapes.
  const keyAttribute = (name) => name === "id" || (name.startsWith("data-") && diagName(name) === name);

  // What the reply knew of the page and the notification (null if it had none) when it stopped.
  // `before` as for chatSigns.
  function diagnose(notification, before) {
    const main = find("main");
    const rows = main ? [...main.querySelectorAll(WHERE["msg-key"].join(", "))] : [];
    const keys = rows.map((row) => row.getAttribute("data-id") || "");
    const count = (n) => Math.min(n, 999);
    const size = (n) => (Number.isFinite(n) && n > 0 ? Math.min(Math.floor(n / 100) * 100, 9900) : 0);
    const header = main && find("header", main);
    // The first message's element, and the elements around it up to its row.
    const first = rows[0] || null;
    const around = [];
    for (let node = first && first.parentElement; node && node !== main && around.length < 8; node = node.parentElement) {
      around.push(node);
      if (node.getAttribute("role") === "row") break;
    }
    const values = [];
    for (const element of [main, header, first, ...around]) {
      for (const name of element ? element.getAttributeNames() : []) {
        const value = element.getAttribute(name) || "";
        if (/[@_]/.test(value) && keyAttribute(name)) {
          values.push(`${diagName(name)}=${shapeOf(value)}`);
        }
      }
    }
    const footer = main && find("footer", main);
    const box = main && composerIn(main);
    const picture = header && headerPictures(header)[0];
    const facts = {
      visibilityState: document.visibilityState, innerWidth: size(innerWidth), innerHeight: size(innerHeight),
      main: Boolean(main), header: Boolean(header), footer: Boolean(footer),
      composer: Boolean(box), keys: count(keys.length),
      // The footer: which of WHERE's places for the message box found it (-1, none), its buttons,
      // the shapes of its icons' names, a message being replied to (or a link's preview) beside the
      // box, and WhatsApp's media editor open.
      composerAt: footer ? WHERE.composer.findIndex((selector) => footer.querySelector(selector)) : -1,
      buttons: count(footer ? footer.querySelectorAll(CONTROLS).length : 0),
      icons: footer ? iconShapes(footer) : [],
      quote: Boolean(box && besideTheBox(main, box)), preview: previewOpen(),
      chatKeys: count(keys.filter((key) => /^(true|false)_/.test(key)).length),
      rows: count(main ? main.querySelectorAll('[role="row"]').length : 0), shape: keys.length ? shapeOf(keys[0]) : "",
      // Whether WhatsApp marks which way messages went (directed), and the kind of the header's
      // picture's address (pictureKind).
      dirClasses: Boolean(main) && directed(main),
      headerImgKind: pictureKind(picture ? picture.getAttribute("src") : ""),
      mainAttrs: attributeNames(main), headerAttrs: attributeNames(header), keyAttrs: attributeNames(first),
      rowAttrs: diagNames(around.flatMap((element) => element.getAttributeNames())),
      values: [...new Set(values)].sort().slice(0, DIAG_VALUES),
    };
    if (notification) {
      const data = notification.data;
      Object.assign(facts, main ? chatSigns(main, notification, before)
        : { name: false, navigated: false, body: false, avatar: false, listed: false });
      // How many rows the list marks as the chat open, and whether the one has the name; the body
      // anywhere in the latest rows, or within their words at all; the kind of the notification's
      // picture's address.
      const mark = listMark(notification.title);
      Object.assign(facts, { selected: count(mark.selected), selectedName: mark.selectedName },
                    main ? messageFacts(main, notification.body) : { messageAnywhere: false, messageWords: false },
                    { iconKind: pictureKind(notification.icon) });
      // Where in the header the name was: which of WHERE's places for it (-1, none), and whether
      // it was the first name there (titleOf's).
      const names = namesOf(header);
      const named = nameAmong(names, notification.title);
      Object.assign(facts, { nameAt: named ? named.at : -1, nameFirst: Boolean(named) && names[0] === named });
      Object.assign(facts, {
        hasBody: Boolean(notification.body), hasIcon: Boolean(notification.icon),
        dataType: data === null ? "null" : Array.isArray(data) ? "array" : typeof data,
        dataKeys: data && typeof data === "object" && !Array.isArray(data) ? diagNames(Object.keys(data)) : [],
        tagShape: shapeOf(notification.tag || ""),
      });
    }
    // Whether WebKit draws the page: an animation frame raced against a timer.
    return new Promise((resolve) => {
      let over = false;
      const finish = (fired) => {
        if (over) return;
        over = true;
        resolve({ ...facts, rafFired: fired });
      };
      requestAnimationFrame(() => finish(true));
      setTimeout(() => finish(false), FRAME_WAIT_MS);
    });
  }

  // The keys of the person's own messages in `main`, for seeing one more turn up (keys with chat ids).
  function ownKeys(main, chat) {
    return new Set([...main.querySelectorAll(WHERE["msg-key"].join(", "))].map((row) => row.getAttribute("data-id"))
      .filter((key) => key && key.startsWith(`true_${chat}_`)));
  }

  // The open chat's messages just before Send, to see the one sent turn up (sentSince), and the
  // words sent.
  function beforeSend(main, chat, words) {
    return { own: ownKeys(main, chat), ids: messageIds(main), keyed: chatIds(main).size > 0, words: flat(words) };
  }

  // Whether the words have gone since `before` (beforeSend): a new message of the person's own, by
  // keys with chat ids; with keys without, which don't say whose a message is, a new message with
  // just these words, as WhatsApp draws the one sent (not one of theirs coming in meanwhile, nor
  // one marked as coming in); or the box emptied.
  function sentSince(main, chat, before) {
    if (!main.isConnected) return false;
    if ([...ownKeys(main, chat)].some((key) => !before.own.has(key))) return true;
    if (!before.keyed && [...main.querySelectorAll(WHERE["msg-key"].join(", "))].some((message) => {
      const id = message.getAttribute("data-id");
      const row = message.closest(WHERE["msg-row"].join(", ")) || message;
      return id && !before.ids.has(id) && !inRow(row) && textsIn(row).includes(before.words);
    })) return true;
    const now = composerIn(main);
    return Boolean(now && boxEmpty(now));
  }

  function composerIn(main) {
    const footer = find("footer", main);
    return footer && find("composer", footer);
  }

  // The names of the icons in `element`, which WhatsApp doesn't translate (its labels it does).
  function iconNames(element) {
    return [...element.querySelectorAll("svg title, [data-icon]")]
      .map((icon) => icon.getAttribute("data-icon") || icon.textContent || "").join(" ").toLowerCase();
  }

  const CONTROLS = 'button, [role="button"]';
  const VOICE_CONTROLS = /ptt|mic|voice|record|audio|pause|stop|delete|trash|play/;  // a voice message's own
  const ROW_END = /ptt|mic|voice|record|audio|send/;  // the button at the end of the message box's row

  // Whether the footer of the open chat `main`, with no message box in it, holds a voice message
  // being recorded or waiting to be sent (its controls, its player), or with `strict` controls of
  // any kind. Words alone (a group only admins write in, one you've left) hold nothing of yours.
  function footerInUse(main, strict) {
    const footer = find("footer", main);
    if (!footer || composerIn(main)) return false;
    if (footer.querySelector("audio")) return true;
    return [...footer.querySelectorAll(CONTROLS)].some((control) => strict || VOICE_CONTROLS.test(iconNames(control)));
  }

  // Whether `element` is on show, more than a pixel high, with words or a picture in it. Words
  // only in HTML: an icon's (SVG) are names, not what anyone reads.
  function showing(element) {
    const seen = (node) => node.getClientRects().length > 0 && node.getBoundingClientRect().height > 1;
    return seen(element) && (Boolean(element instanceof HTMLElement && element.innerText.trim())
      || [...element.querySelectorAll("img, video, canvas")].some(seen));
  }

  // Whether the footer of `main` shows anything besides the row of its (empty) message box: the
  // box, and the buttons beside it as far as the voice message button. A message being replied to
  // shows there, and Send would send these words as a reply to it. WhatsApp names none of it, so
  // it's read from the layout: anything there with words or a picture counts.
  function besideTheBox(main, box) {
    const footer = find("footer", main);
    if (!footer || !footer.contains(box)) return false;
    const controls = [...footer.querySelectorAll(CONTROLS)].filter((control) => !box.contains(control));
    const end = controls.find((control) => ROW_END.test(iconNames(control))) || controls[controls.length - 1];
    // The box and what holds it, up to the footer: the row is the first of them that holds `end`
    // too (none: the footer itself is the row), and only what's beside the row and above it counts.
    const up = [];
    for (let node = box; node !== footer; node = node.parentElement) up.push(node);
    const row = end ? up.findIndex((node) => node.contains(end)) : -1;
    return row >= 0 && up.slice(row).some((inner) =>
      [...inner.parentElement.children].some((other) => other !== inner && showing(other)));
  }

  // WhatsApp's Send button, by the name of its icon (its label is translated). It only exists
  // while there are words in the box; until then its place holds the voice note button, which
  // this never takes for it. More than one, or a scheduling one, is not a button to guess at.
  function sendButton(main) {
    const footer = find("footer", main);
    if (!footer) return null;
    const found = [...footer.querySelectorAll(CONTROLS)].filter((button) => {
      const names = iconNames(button);
      return /(^|[\s_-])send([\s_-]|$)/.test(names) && !/mic|ptt|voice|record|audio|schedul|later|clock/.test(names);
    });
    if (found.length !== 1) return null;
    const button = found[0];
    return button.disabled || button.getAttribute("aria-disabled") === "true" ? null : button;
  }

  // test()'s value as soon as it's truthy, looked at whenever the page changes, or null after
  // `ms`. Changes are seen at once in a hidden page; timers there wait for WebKit's hidden-page
  // throttling, which Natter turns off during a reply (account.py).
  function until(test, ms) {
    return new Promise((resolve) => {
      const now = test();
      if (now || ms <= 0) {
        resolve(now || null);
        return;
      }
      let over = false;
      const finish = (value) => {
        if (over) return;
        over = true;
        observer.disconnect();
        clearTimeout(timer);
        resolve(value || null);
      };
      const observer = new MutationObserver(() => { const value = test(); if (value) finish(value); });
      observer.observe(find("app") || document.body,
                       { childList: true, subtree: true, characterData: true, attributes: true });
      const timer = setTimeout(() => finish(test()), ms);
    });
  }

  // Once nothing in `root` has changed for `ms`, or after `most`.
  function quiet(root, ms, most) {
    return new Promise((resolve) => {
      let timer = 0;
      const finish = () => {
        observer.disconnect();
        clearTimeout(timer);
        clearTimeout(end);
        resolve();
      };
      const observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(finish, ms); });
      observer.observe(root, { childList: true, subtree: true, characterData: true });
      timer = setTimeout(finish, ms);
      const end = setTimeout(finish, most);
    });
  }

  // One turn of the page's event loop, which a hidden page doesn't hold back as it does timers.
  // An edit's own update (the editor applies it a microtask later) has landed by then.
  function settle() {
    return new Promise((resolve) => {
      const channel = new MessageChannel();
      channel.port1.onmessage = () => {
        channel.port1.close();
        resolve();
      };
      channel.port2.postMessage(null);
    });
  }

  // The chat id and words of each reply queued or under way: the same reply asked for twice
  // while the first is still going is the same act, sent once.
  const replying = new Set();
  let replies = Promise.resolve();

  // One at a time, in the order asked. `request`: {pageId, chatId, text, budgetMs, visible, sinceMs}.
  // Its budget runs from now, waiting included: Send is never pressed after it. `sinceMs`: how long
  // ago Natter asked (before lending the page, which may have WhatsApp close the notification). The answer is
  // one of those above; why (for Natter's log, never with names or words) goes on request.why,
  // which once the chat was found to be the one says how ("ids", "name+body", sameChat), and what
  // the page was like when it wasn't, or couldn't be told (diagnose), on request.diag.
  function reply(request) {
    const { pageId, chatId, text, budgetMs, visible, sinceMs } = request && typeof request === "object" ? request : {};
    const budget = Number.isFinite(budgetMs) ? Math.min(Math.max(budgetMs, 0), 60000) : 0;
    const deadline = performance.now() + budget;
    const asked = performance.now() - (Number.isFinite(sinceMs) ? Math.min(Math.max(sinceMs, 0), 60000) : 0);
    const progress = { stage: "", diag: null, how: "" };
    const tell = ([answer, why]) => {
      try {
        request.why = why || progress.how || "";
        request.diag = progress.diag;
      } catch (_) { /* a frozen request: no reason told */ }
      return answer;
    };
    const key = JSON.stringify([chatId, text]);
    if (replying.has(key)) return Promise.resolve(tell(["busy", "repeat"]));
    replying.add(key);
    const run = () => replyOnce(pageId, chatId, text, deadline, Boolean(visible), asked, progress)
      // A mistake of Natter's own: said for what may be in WhatsApp by then.
      .catch(() => [{ "": "busy", typed: "left-as-draft", pressed: "unsure" }[progress.stage], "exception"])
      .then((answer) => {
        replying.delete(key);
        return tell(answer);
      });
    replies = replies.then(run, run);
    return replies;
  }

  async function replyOnce(pageId, chat, text, deadline, visible, asked, progress) {
    const left = () => Math.max(0, deadline - performance.now());
    if (typeof text !== "string" || !text.trim()) return ["too-long", "blank"];
    const words = text.replace(/\r\n?/g, "\n").trim();
    if (words.length > REPLY_LIMIT) return ["too-long", ""];
    if (typeof chat !== "string" || !CHAT_ID.test(chat)) return ["gone", "chat-id"];
    if (calling()) return ["calling", ""];
    // A microphone on outside a call is a voice message being recorded, in the open chat: another
    // chat would end it, and that chat has no message box meanwhile. Seen whether the page is or not.
    if (liveStreams()) return ["busy", "recording"];
    if (!find("pane")) return ["not-ready", ""];
    // A picture or file waiting to be sent: another chat would drop it, and Send would send it.
    if (previewOpen()) return ["busy", "preview"];
    if (!left()) return ["busy", "queue"];
    // What the notification said, for telling its chat without its id: only if it's that chat's.
    // One WhatsApp closed since (its chat read) still says it, and is still clicked.
    const notification = shown.get(pageId) || closed.get(pageId) || null;
    const facts = notification && notification.tag === chat ? notification : null;
    // The chat open in `main`, if it's this one: by its messages' keys while they carry chat ids
    // (isChat), else by its name and another sign (sameChat). With how, and its name then. Once the
    // chat open before the click had keys with chat ids, one with no messages yet is waited for.
    const at = (main, before) => {
      if (!main || (before && before.keyed && !main.querySelector(WHERE["msg-key"].join(", ")))) return null;
      const how = chatIds(main).size ? (isChat(main, chat) ? "ids" : "") : facts ? sameChat(main, facts, before) : "";
      const named = how && facts ? nameAmong(namesOf(find("header", main)), facts.title) : null;
      return how ? { main, how, name: named ? named.name : "" } : null;
    };
    // Still that chat: the same #main, with its keys still this chat's, or else the same name.
    const still = (found) => find("main") === found.main && (chatIds(found.main).size ? isChat(found.main, chat)
      : Boolean(found.name) && namesOf(find("header", found.main)).some((name) => name.name === found.name));
    const failed = async (answer, told, before) => {
      progress.diag = await diagnose(told, before);
      return answer;
    };
    // Open already, told by its keys: no click. Told by its name and message, it's clicked all the
    // same: another chat of the same name may be open, with the same words.
    const first = find("main");
    let found = first && chatIds(first).size ? at(first, null) : null;
    // The chat open before the click (below), and whether the chat was told without the click
    // having opened it: by its message or picture, in a chat of that name open before.
    let before = null;
    let unopened = false;
    // What the page was like, for an answer after the check (draft, left-as-draft, cant-send, unsure).
    const described = (answer) => failed(answer, notification, before);
    if (!found) {
      if (!notification) return failed(["gone", "notification"], null, null);
      if (notification.tag !== chat) return failed(["mismatch", "tag"], notification, null);
      // WhatsApp's click closes what's open over the chat list and leaves the open chat, whether
      // anyone sees the page or not (it keeps both while Natter's window is hidden): words typed
      // there (a group being made, a profile name being changed), a voice message waiting to be
      // sent. In sight, any controls in place of the open chat's message box, as someone may be
      // using them. Status, Settings or the Calls tab left open over the list just close.
      if (drawerEdit()) return ["busy", "drawer"];
      const open = find("main");
      if (open && footerInUse(open, visible)) return ["busy", "open-chat"];
      // The chat open before the click, to tell whether the click opened another.
      // Closed counts as of when the reply was asked: lending the page may have WhatsApp mark the
      // chat read and close it since, and one shown then is held to no more than one shown now.
      before = {
        main: open, names: open ? namesOf(find("header", open)) : [],
        keyed: Boolean(open) && chatIds(open).size > 0,
        closed: !shown.has(pageId) && !(notification.closedAt > asked),
      };
      clickNotification(pageId);  // WhatsApp's own handler: it opens the chat by its id
      // WhatsApp opens it a moment later, and until then the chat open before is still there. One of
      // the same name is given that moment, so as not to be taken for the one the click opens. (The
      // page's timers aren't held back during a reply, hidden or not: account.py.)
      if (open && nameAmong(before.names, notification.title)) {
        await new Promise((resolve) => setTimeout(resolve, Math.min(ROWS_WAIT_MS, left())));
        await quiet(find("app") || document.body, DRAFT_QUIET_MS, Math.min(DRAFT_WAIT_MS, left()));
      }
      const there = () => at(find("main"), before);
      found = await until(there, Math.min(CHAT_WAIT_MS, left()));
      if (!found && keyless()) {
        // A chat open with no messages yet: WhatsApp may still be loading them, those around the
        // notified one when it isn't the chat's latest. Waited for, within the budget, less the
        // time the rest takes, then a moment more for the rest of the chat to be drawn.
        const most = () => Math.max(0, left() - TYPE_RESERVE_MS);
        await until(() => there() || !keyless(), most());
        found = await until(there, Math.min(ROWS_WAIT_MS, most()));
      }
      // Found too late to leave time for the draft and Send (the page held up, say): nothing typed.
      if (found && left() < DRAFT_WAIT_MS + SEND_WAIT_MS) return ["busy", "late"];
      if (!found) {
        const now = find("main");
        const why = !now ? "click" : chatIds(now).size ? "other-chat"
          : nameAmong(namesOf(find("header", now)), notification.title) ? "unconfirmed" : "other-name";
        return failed([now ? "mismatch" : left() ? "gone" : "not-ready", why], notification, before);
      }
      await quiet(found.main, DRAFT_QUIET_MS, Math.min(DRAFT_WAIT_MS, left()));
      if (!still(found)) return failed(["mismatch", "moved"], notification, before);
      unopened = found.how !== "ids" && !chatSigns(found.main, facts, before).navigated;
      const same = unopened ? namesakes(facts.title) : 0;
      if (same === null) {
        return failed(["mismatch", searching() || filtered() ? "filtered" : "unnamed"], notification, before);
      }
      if (same > 1) return failed(["mismatch", "ambiguous"], notification, before);
    }
    progress.how = found.how;
    const main = found.main;
    const box = composerIn(main);
    // No message box: a voice message of theirs in its place, or a chat that takes none from them.
    if (!box) return footerInUse(main, false) ? ["busy", "no-composer"] : described(["cant-send", ""]);
    if (!boxEmpty(box)) return ["draft", ""];  // the person's own words are never touched
    // Looked at once, before typing: words with a link in them bring a preview up there too.
    if (besideTheBox(main, box)) return described(["draft", "quote"]);
    box.focus();
    const range = document.createRange();
    range.selectNodeContents(box);
    range.collapse(false);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    await settle();
    // Only ever into this box: the editing commands act wherever the caret is.
    const inBox = () => box.isConnected && document.activeElement === box && selection.rangeCount > 0
      && box.contains(selection.getRangeAt(0).endContainer);
    const lines = words.split("\n");
    for (let i = 0; i < lines.length; i++) {
      if (!inBox() || !left()) return [progress.stage ? "left-as-draft" : "busy", !left() ? "late" : "focus"];
      progress.stage = "typed";
      if (i) document.execCommand("insertLineBreak");
      if (lines[i]) document.execCommand("insertText", false, lines[i]);
      // Each edit lands before the next: the editor reads the caret from the page for each one.
      await settle();
      const wanted = canonical(lines.slice(0, i + 1).join("\n"));
      if (!(await until(() => sameText(canonical(boxText(box)), wanted), Math.min(LINE_WAIT_MS, left())))) {
        return described(["left-as-draft", "text"]);
      }
    }
    const wanted = canonical(words);
    const button = await until(() => sendButton(main), Math.min(SEND_WAIT_MS, left()));
    if (!left()) return ["left-as-draft", "late"];
    if (!button) return described(["left-as-draft", "no-button"]);
    // Still this chat, this box, these words, this button, nothing waiting to be sent with them;
    // and one told without the click having opened it, told again by all its signs, and still with
    // no other chat of its name in the list, nor names there that can't be counted (namesakes).
    const others = () => { const same = namesakes(facts.title); return same === null || same > 1; };
    if (!still(found) || composerIn(main) !== box || !sameText(canonical(boxText(box)), wanted)
        || sendButton(main) !== button || previewOpen() || (unopened && (!sameChat(main, facts, before) || others()))) {
      return ["left-as-draft", "moved"];
    }
    if (chatIds(main).size) progress.how = "ids";  // keys with chat ids, this chat's (still)
    const pressed = beforeSend(main, chat, words);
    progress.stage = "pressed";
    button.click();  // the one press
    const sent = await until(() => sentSince(main, chat, pressed), SENT_WAIT_MS);
    return sent ? ["sent", ""] : described(["unsure", ""]);
  }

  // ---- Classes Natter's style sheet hangs off -----------------------------------------------

  // WhatsApp sets <html>'s classes too and may replace them all, so Natter's are put back.
  const wanted = new Set();

  function applyClasses() {
    const list = document.documentElement.classList;
    wanted.forEach((name) => { if (!list.contains(name)) list.add(name); });
  }

  function setClass(name, on) {
    if (on) wanted.add(name); else wanted.delete(name);
    document.documentElement.classList.toggle(name, Boolean(on));
  }

  new MutationObserver(applyClasses).observe(document.documentElement, {
    attributes: true, attributeFilter: ["class"],
  });

  // ---- How WhatsApp looks (read only) -------------------------------------------------------
  // Natter's theme (themes.py) redefines WhatsApp's colour names on <body> and #main, and leaves
  // <html>'s alone, so those still say what WhatsApp itself draws. Natter is told whether they're
  // there, whether they're dark, whether WhatsApp's own dark class agrees, and which style WebKit
  // tells the page. If the names are gone, or the class doesn't follow them, Natter takes its
  // theme off the page; if WhatsApp has a light or dark look of its own, Natter says so (app.py,
  // page_look_changed). This only reads: it changes nothing in the page and stores nothing.

  const LOOK_SETTLE_MS = 2000;  // WhatsApp's dark class follows a change of style a moment later
  const darkStyle = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let lastLook = "";
  let lookTimer = 0;

  function look() {
    const rgb = getComputedStyle(document.documentElement).getPropertyValue("--WDS-surface-default-RGB").trim();
    const parts = rgb ? rgb.split(",").map((part) => Number(part.trim())) : [];
    const tokens = parts.length === 3 && parts.every((n) => Number.isFinite(n) && n >= 0 && n <= 255);
    const luma = tokens ? 0.299 * parts[0] + 0.587 * parts[1] + 0.114 * parts[2] : 255;
    return {
      tokens, dark: tokens && luma < 128,
      darkClass: Boolean(document.body && document.body.classList.contains("dark")),
      media: Boolean(darkStyle && darkStyle.matches),
    };
  }

  // Once the chat list is up (the link screen's look is fixed), after things settle, and only
  // when something changed.
  function reportLook() {
    if (lookTimer) clearTimeout(lookTimer);
    lookTimer = setTimeout(() => {
      lookTimer = 0;
      if (!lastReady) return;
      const now = look();
      const signature = JSON.stringify(now);
      if (signature === lastLook) return;
      lastLook = signature;
      post({ type: "look", ...now });
    }, LOOK_SETTLE_MS);
  }

  function watchLook() {
    if (darkStyle && darkStyle.addEventListener) darkStyle.addEventListener("change", reportLook);
    new MutationObserver(reportLook).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  }

  window.__natter = {
    clickNotification, openChat, openLink, attach, pick, receive, discard, setClass, activeChat, scan, audio, startCall,
    canary, reply,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch, { once: true });
  } else {
    watch();
  }
})();
