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

  // Contexts are held weakly: one WhatsApp drops without close() can still be collected.
  const contexts = new Set();  // WeakRefs to AudioContexts
  const rested = new WeakSet();  // contexts Natter suspended (the only ones its play() wakes)
  const sources = new Set();  // started source nodes that haven't ended
  const streams = new Set();  // microphone streams (voice notes, calls) feeding a context
  const elements = new Set();  // <audio>/<video> played through a context
  const routed = new WeakMap();  // each of those → the context it plays through
  let lastStart = 0;
  const IDLE_MS = 30000;  // long enough that a burst of message sounds doesn't cycle the pipeline

  function wake(context) {
    if (!context) return;
    rested.delete(context);
    if (context.state === "suspended") context.resume().catch(() => {});
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

  function mediaPlaying() {
    // Elements no longer in the page and done playing go: each holds its context and a
    // GStreamer player with its decoded media.
    elements.forEach((element) => {
      if (!element.isConnected && (element.paused || element.ended)) elements.delete(element);
    });
    return [...elements, ...document.querySelectorAll("audio, video")].some((m) => !m.paused && !m.ended);
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
  // recorder left connected) doesn't hold its context. Held weakly: one the page lets go is
  // collected, and goes from here.
  const processors = new Set();  // WeakRefs to those nodes
  const outputs = new WeakMap();  // each → what it's connected to now
  const scripts = new WeakSet();  // which of them are script processors
  const listening = new WeakMap();  // a script processor → its audioprocess listeners
  const fedBy = new WeakMap();  // each → the microphone streams connected into it
  let onProcessorConnected = null;  // set by "Sound that can't be heard", for its log line

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
  // ended, the processor no longer holds its context.
  function feedFrom(source, stream) {
    if (!source || typeof source.connect !== "function" || !stream) return;
    const connect = source.connect;
    source.connect = function (destination, ...rest) {
      const result = connect.call(this, destination, ...rest);
      const feeds = destination && fedBy.get(destination);
      if (feeds) feeds.add(stream);
      return result;
    };
  }

  const ended = (stream) => {
    try { return !stream.getTracks().some((track) => track.readyState === "live"); } catch (_) { return false; }
  };

  // The contexts a connected processor holds up now.
  function processing() {
    const busy = new Set();
    for (const ref of processors) {
      const node = ref.deref();
      if (!node || !node.context || node.context.state === "closed") { processors.delete(ref); continue; }
      const out = outputs.get(node);
      if (!out || !out.size) continue;
      if (scripts.has(node) && typeof node.onaudioprocess !== "function" && !listening.get(node).size) continue;
      const feeds = fedBy.get(node);
      if (feeds && feeds.size && [...feeds].every(ended)) continue;  // a recorder, done
      busy.add(node.context);
    }
    return busy;
  }

  function rest() {
    if (performance.now() - lastStart < IDLE_MS || playingSources() || liveStreams() || mediaPlaying() || calling()) return;
    const busy = processing();
    eachContext((context) => {
      if (context.state === "running" && !callContexts.has(context) && !busy.has(context)) {
        rested.add(context);
        context.suspend().catch(() => {});
      }
    });
  }

  const Context = window.AudioContext || window.webkitAudioContext;
  if (Context) {
    const Tracked = class extends Context {
      constructor(...args) {
        super(...args);
        const ref = new WeakRef(this);
        contexts.add(ref);
        this.addEventListener("statechange", () => { if (this.state === "closed") contexts.delete(ref); });
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
    // context be (processing()).
    const scriptProcessor = Context.prototype.createScriptProcessor;
    if (typeof scriptProcessor === "function") {
      Tracked.prototype.createScriptProcessor = function (...args) {
        lastStart = performance.now();
        wake(this);
        const node = scriptProcessor.apply(this, args);
        trackProcessor(node, true);
        return node;
      };
    }
    window.AudioContext = Tracked;
    if (window.webkitAudioContext) window.webkitAudioContext = Tracked;
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
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
      lastStart = performance.now();
      eachContext((context) => { if (rested.has(context)) wake(context); });  // only what Natter suspended
      return play.apply(this, args);
    };
    setInterval(rest, IDLE_MS);
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
    const held = processing().size;  // contexts a connected worklet or script processor keeps up
    return { contexts: states, sources: playing, streams: live, media, elements: elements.size,
             idle: Math.round(performance.now() - lastStart),
             rtc: window.__natterRtc ? window.__natterRtc.counts() : null,
             sizes: { contexts: states.length, rested: restedCount, calls: callCount, elements: elements.size,
                      sources: playing, streams: live, held, notifications: shown.size } };
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
    "row-name": ['[data-testid="cell-frame-title"] span[title]', "span[title]"],  // in a row
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

  function nameOf(row) {
    // The name is in the row's title cell. Other titled spans hold the last message, which
    // Natter never reads; the title comes first when the cell isn't marked.
    const span = find("row-name", row);
    const name = span && span.getAttribute("title");
    return name ? plainName(name) : "";
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

  // The list's rows with a name, as drawn: [name, row, its translateY, its place in the page].
  function namedRows() {
    const list = find("pane");
    if (!list) return [];
    const found = [];
    list.querySelectorAll(ROWS).forEach((row, i) => {
      if (row.parentElement && row.parentElement.closest(ROWS)) return;  // a nested row
      const name = nameOf(row);
      if (name) found.push([name, row, offsetOf(row), i]);
    });
    return found;
  }

  // Whether the list marks `row` as the open chat's (WhatsApp's aria-selected, on the row or in it).
  const selectedRow = (row) => WHERE["row-selected"].some((selector) => row.matches(selector)
    || Boolean(row.querySelector(selector)));

  function chatRows() {
    const found = namedRows();
    const height = rowHeight(found.map((entry) => entry[2]));
    const rows = found.map(([name, row, offset, i]) => [name, positionOf(row, offset, height, i)]);
    return rows.sort((a, b) => a[1] - b[1]).slice(0, 200);
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
  // empty list, no open chat — is left out rather than called missing.

  let canaryTold = false;

  function canary() {
    const found = [];
    const missing = [];
    const judge = (key, ok) => (ok ? found : missing).push(key);
    judge("app", Boolean(find("app")));
    const pane = find("pane");
    if (!pane) {
      // The link screen has no list; anything else without one is loading or a redesign.
      judge("link-screen", linkScreen());
      return { found, missing };
    }
    found.push("pane");
    const side = find("side");
    judge("side", Boolean(side));
    judge("search", Boolean(find("search")));
    if (side) judge("chips", Boolean(side.querySelector(CHIPS)));
    const row = find("row", pane);
    if (row || pane.querySelector("span[title]")) {  // an empty list can't be judged
      judge("row", Boolean(row));
      if (row) judge("row-name", Boolean(find("row-name", row)));
    }
    const header = find("header");
    if (header) judge("header-name", Boolean(find("header-name", header)));
    return { found, missing };
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
    if (!searching() && !filtered()) {
      const rows = chatRows();
      const signature = JSON.stringify(rows);
      if (signature !== lastRows && rows.length) {
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
        setTimeout(() => post({ type: "canary", ...canary() }), 10000);
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
  // then; and each failure: the element's error (MediaError's code), play() refused (the
  // error's name, not its message) or the context it plays through not running a moment after
  // it started. Each line once a page for what it says, so repeats don't use up the page's
  // MEDIA_LINES. Kinds, codes, states and error names only, never a URL, a name or what plays.
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

    function about(element) {
      const context = routed.get(element);
      return {
        kind: window.HTMLVideoElement && element instanceof window.HTMLVideoElement ? "video" : "audio",
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
    }

    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
      if (!toldOnce.has("support")) tellMedia(support(), "support");
      watchMedia(this);
      const result = play.apply(this, args);
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

  function rowFor(name) {
    const pane = find("pane");
    if (!pane) return null;
    for (const span of pane.querySelectorAll("span[title]")) {
      if (plainName(span.getAttribute("title")) === name) {
        const row = span.closest(ROWS);
        if (row && nameOf(row) === name) return span;
      }
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
  // the chat list shows two of that name, or shows only some chats (namesakes): the click may open
  // nothing, or the other one only later.

  const LAST_ROWS = 12;  // the latest rows of the open chat, where the notified message is looked for

  // A chat's name as Natter compares them (activeChat): spaces as one, no invisible marks, and NFC
  // with U+FE0F aside, which emoji pictures may or may not carry.
  const nameKey = (text) => plainName(String(text || "").replace(/\s+/g, " ")).normalize("NFC").replace(/\uFE0F/g, "");

  function sameName(a, b) {
    const key = nameKey(a);
    return Boolean(key) && key === nameKey(b);
  }

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

  // How many chats in the list (chatRows, those WhatsApp has drawn) have the name `title`. Two or
  // more, and a chat of that name the click didn't open is never taken for the one notified: it may
  // be the other, with the same words, while WhatsApp's click opens nothing, or opens it later.
  // With a search typed or a filter chip on, the list isn't all the chats (scan): not known, null.
  function namesakes(title) {
    if (searching() || filtered()) return null;
    return chatRows().filter(([name]) => sameName(name, title)).length;
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
  // top and has that row among its first TOP_ROWS; else null. The notified chat has just had a
  // message, so it sits up there: one of the same name drawn there would be counted, and one not
  // drawn (the list scrolled down) is never left out. Archived chats aren't in the list at all.
  function topRow(title) {
    if (namesakes(title) !== 1) return null;
    const rows = namedRows();
    const height = rowHeight(rows.map((entry) => entry[2]));
    // A row's place by its aria-rowindex or translateY only, NaN if neither tells it.
    const place = ([, row, offset]) => positionOf(row, offset, height, offset === 0 ? 0 : NaN);
    const named = rows.filter(([name]) => sameName(name, title));
    if (named.length !== 1 || !rows.some((entry) => place(entry) === 0)) return null;
    return place(named[0]) < TOP_ROWS ? named[0][1] : null;
  }

  // Whether the list, showing all the chats from its top, has one row of the name `title` near the
  // top (topRow), and marks that row, and only it, as the chat open.
  function listedAlone(title) {
    const row = topRow(title);
    const marked = namedRows().filter(([, other]) => selectedRow(other));
    return Boolean(row) && marked.length === 1 && marked[0][1] === row;
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
      if (same === null) return failed(["mismatch", "filtered"], notification, before);
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
    // and one told without the click having opened it, told again by all its signs.
    if (!still(found) || composerIn(main) !== box || !sameText(canonical(boxText(box)), wanted)
        || sendButton(main) !== button || previewOpen() || (unopened && !sameChat(main, facts, before))) {
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
