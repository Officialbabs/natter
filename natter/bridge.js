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
      if (shown.delete(this.id)) post({ type: "unnotify", id: this.id });
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

  function clickNotification(id) {
    const notification = shown.get(id);
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

  function rest() {
    if (performance.now() - lastStart < IDLE_MS || playingSources() || liveStreams() || mediaPlaying() || calling()) return;
    eachContext((context) => {
      if (context.state === "running" && !callContexts.has(context)) {
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
        return super.createMediaStreamSource(stream);
      }
      createMediaElementSource(element) {
        elements.add(element);
        wake(this);
        element.addEventListener("play", () => wake(this));
        return super.createMediaElementSource(element);
      }
    };
    window.AudioContext = Tracked;
    if (window.webkitAudioContext) window.webkitAudioContext = Tracked;
    const Worklet = window.AudioWorkletNode;
    if (Worklet) {
      window.AudioWorkletNode = class AudioWorkletNode extends Worklet {
        constructor(context, name, options) {
          super(context, name, options);
          if (/voip/i.test(String(name))) {
            callContexts.add(context);
            wake(context);
          }
        }
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
    return { contexts: states, sources: playing, streams: live, media, elements: elements.size,
             idle: Math.round(performance.now() - lastStart),
             rtc: window.__natterRtc ? window.__natterRtc.counts() : null,
             sizes: { contexts: states.length, rested: restedCount, calls: callCount, elements: elements.size,
                      sources: playing, streams: live, notifications: shown.size } };
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
    "msg-key": ["[data-id]"],                              // its messages, in #main: "<mine>_<chat id>_<id>…"
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

  function chatRows() {
    const list = find("pane");
    if (!list) return [];
    const found = [];
    list.querySelectorAll(ROWS).forEach((row, i) => {
      if (row.parentElement && row.parentElement.closest(ROWS)) return;  // a nested row
      const name = nameOf(row);
      if (name) found.push([name, row, offsetOf(row), i]);
    });
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

  // Text with WhatsApp's emoji pictures put back as the characters they stand for.
  function textOf(element) {
    return [...element.childNodes].map((node) => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent;
      if (node.tagName === "IMG") return node.getAttribute("alt") || "";
      return node.nodeType === Node.ELEMENT_NODE ? textOf(node) : "";
    }).join("");
  }

  function activeChat() {
    const header = find("header");
    if (!header) return "";
    const title = find("header-name", header);
    if (!title) return "";
    return plainName((title.getAttribute("title") || textOf(title)).replace(/\s+/g, " "));
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

  // ---- Replying from a notification ---------------------------------------------------------
  // A reply goes the way the person's own hands would take it, once: WhatsApp's own handler for
  // its notification opens that chat (unless it's open already), the words go into the chat's
  // empty message box through the page's own editing commands, and WhatsApp's Send button is
  // pressed once. Never the Enter key (what it does follows WhatsApp's "Enter is send" setting),
  // never twice, never again after a failure, and nothing of WhatsApp's code or storage. Anything
  // that doesn't look exactly right ends it before Send, with the reason: nothing is guessed.
  // The answers (account.py REPLY_RESULTS):
  //   "sent"           Send pressed, and WhatsApp took the words out of the box
  //   "unsure"         Send pressed, not seen to go: it may have, so it's never tried again
  //   "left-as-draft"  typed, not sent: the words may be in the chat's message box
  //   "draft" (the chat has words of the person's own in its box, or a message being replied to),
  //   "busy" (something's in the way), "cant-send" (the chat takes no messages from them),
  //   "mismatch" (the chat that opened isn't that one), "gone" (no way to open it), "not-ready",
  //   "calling", "too-long": nothing typed

  const CHAT_ID = /^\d{5,24}(-\d{5,24})?@(c\.us|g\.us|lid)$/;  // people and groups (account.py CHAT_ID)
  const REPLY_LIMIT = 4096;  // UTF-16 code units, as String.length counts them (account.py REPLY_LIMIT)
  const CHAT_WAIT_MS = 10000;  // for the chat to open
  const DRAFT_QUIET_MS = 250;  // a chat just opened puts its draft back in the box: until the box is still
  const DRAFT_WAIT_MS = 1500;
  const LINE_WAIT_MS = 2000;  // for each line to land in the box
  const SEND_WAIT_MS = 5000;  // for WhatsApp's Send button, which turns up a timer tick after the words
  const SENT_WAIT_MS = 10000;  // for the box to empty once Send is pressed
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

  // The chats the open chat's messages belong to, by their keys: "<mine>_<chat id>_<id>…".
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

  // Whether the open chat shows no message keys yet: WhatsApp has opened it and is still drawing
  // its messages (or loading those around the one notified, which can take seconds).
  function keyless() {
    const main = find("main");
    return Boolean(main) && !chatIds(main).size;
  }

  // What Natter's log may say of the page when a reply found no chat to check (account.py
  // reply_diag): whether WhatsApp is drawing it at all, and what the open chat holds, by counts and
  // the shape of its first message key (each run of digits as 9, of letters as a, of anything else
  // as ?). Never a name, words, an id or their lengths.
  function diagnose() {
    const main = find("main");
    const rows = main ? [...main.querySelectorAll(WHERE["msg-key"].join(", "))] : [];
    const keys = rows.map((row) => row.getAttribute("data-id") || "");
    const count = (n) => Math.min(n, 999);
    const size = (n) => (Number.isFinite(n) && n > 0 ? Math.min(Math.floor(n / 100) * 100, 9900) : 0);
    const shape = (key) => key.slice(0, 40).replace(/[0-9]+/g, "9").replace(/[A-Za-z]+/g, "a")
      .replace(/[^9a_@.:-]+/g, "?");
    const facts = {
      visibilityState: document.visibilityState, innerWidth: size(innerWidth), innerHeight: size(innerHeight),
      main: Boolean(main), header: Boolean(main && find("header")), footer: Boolean(main && find("footer", main)),
      composer: Boolean(main && composerIn(main)), keys: count(keys.length),
      chatKeys: count(keys.filter((key) => /^(true|false)_/.test(key)).length),
      rows: count(main ? main.querySelectorAll('[role="row"]').length : 0), shape: keys.length ? shape(keys[0]) : "",
    };
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

  // The keys of the person's own messages in `main`, for seeing one more turn up.
  function ownKeys(main, chat) {
    return new Set([...main.querySelectorAll(WHERE["msg-key"].join(", "))].map((row) => row.getAttribute("data-id"))
      .filter((key) => key && key.startsWith(`true_${chat}_`)));
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

  // One at a time, in the order asked. `request`: {pageId, chatId, text, budgetMs, visible}.
  // Its budget runs from now, waiting included: Send is never pressed after it. The answer is
  // one of those above; why (for Natter's log, never with names or words) goes on request.why,
  // and what the page was like when no chat could be checked (diagnose) on request.diag.
  function reply(request) {
    const { pageId, chatId, text, budgetMs, visible } = request && typeof request === "object" ? request : {};
    const budget = Number.isFinite(budgetMs) ? Math.min(Math.max(budgetMs, 0), 60000) : 0;
    const deadline = performance.now() + budget;
    const progress = { stage: "", diag: null };
    const tell = ([answer, why]) => {
      try {
        request.why = why || "";
        request.diag = progress.diag;
      } catch (_) { /* a frozen request: no reason told */ }
      return answer;
    };
    const key = JSON.stringify([chatId, text]);
    if (replying.has(key)) return Promise.resolve(tell(["busy", "repeat"]));
    replying.add(key);
    const run = () => replyOnce(pageId, chatId, text, deadline, Boolean(visible), progress)
      // A mistake of Natter's own: said for what may be in WhatsApp by then.
      .catch(() => [{ "": "busy", typed: "left-as-draft", pressed: "unsure" }[progress.stage], "exception"])
      .then((answer) => {
        replying.delete(key);
        return tell(answer);
      });
    replies = replies.then(run, run);
    return replies;
  }

  async function replyOnce(pageId, chat, text, deadline, visible, progress) {
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
    const there = () => {
      const main = find("main");
      return main && isChat(main, chat) ? main : null;
    };
    let main = there();
    if (!main) {
      const notification = shown.get(pageId);
      if (!notification) return ["gone", "notification"];
      if (notification.tag !== chat) return ["mismatch", "tag"];
      // WhatsApp's click closes what's open over the chat list and leaves the open chat, whether
      // anyone sees the page or not (it keeps both while Natter's window is hidden): words typed
      // there (a group being made, a profile name being changed), a voice message waiting to be
      // sent. In sight, any controls in place of the open chat's message box, as someone may be
      // using them. Status, Settings or the Calls tab left open over the list just close.
      if (drawerEdit()) return ["busy", "drawer"];
      const open = find("main");
      if (open && footerInUse(open, visible)) return ["busy", "open-chat"];
      clickNotification(pageId);  // WhatsApp's own handler: it opens the chat by its id
      main = await until(there, Math.min(CHAT_WAIT_MS, left()));
      if (!main && keyless()) {
        // A chat open with no messages keyed yet: WhatsApp may still be loading them, those around
        // the notified one when it isn't the chat's latest. Waited for, within the budget, less
        // the time the rest takes.
        await until(() => !keyless(), Math.max(0, left() - TYPE_RESERVE_MS));
        main = there();
        if (main && left() < DRAFT_WAIT_MS + SEND_WAIT_MS) return ["busy", "late"];
      }
      if (!main) {
        const open = find("main");
        if (open && chatIds(open).size) return ["mismatch", "other-chat"];
        const answer = open ? ["mismatch", "no-ids"] : [left() ? "gone" : "not-ready", "click"];
        progress.diag = await diagnose();
        return answer;
      }
      await quiet(main, DRAFT_QUIET_MS, Math.min(DRAFT_WAIT_MS, left()));
      if (there() !== main) return ["mismatch", "moved"];
    }
    const box = composerIn(main);
    // No message box: a voice message of theirs in its place, or a chat that takes none from them.
    if (!box) return footerInUse(main, false) ? ["busy", "no-composer"] : ["cant-send", ""];
    if (!boxEmpty(box)) return ["draft", ""];  // the person's own words are never touched
    // Looked at once, before typing: words with a link in them bring a preview up there too.
    if (besideTheBox(main, box)) return ["draft", "quote"];
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
        return ["left-as-draft", "text"];
      }
    }
    const wanted = canonical(words);
    const button = await until(() => sendButton(main), Math.min(SEND_WAIT_MS, left()));
    if (!left()) return ["left-as-draft", "late"];
    if (!button) return ["left-as-draft", "no-button"];
    // Still this chat, this box, these words, this button, nothing waiting to be sent with them.
    if (there() !== main || composerIn(main) !== box || !sameText(canonical(boxText(box)), wanted)
        || sendButton(main) !== button || previewOpen()) {
      return ["left-as-draft", "moved"];
    }
    const before = ownKeys(main, chat);
    progress.stage = "pressed";
    button.click();  // the one press
    const sent = await until(() => {
      if (!main.isConnected) return false;
      if ([...ownKeys(main, chat)].some((key) => !before.has(key))) return true;
      const now = composerIn(main);
      return Boolean(now && boxEmpty(now));
    }, SENT_WAIT_MS);
    return sent ? ["sent", ""] : ["unsure", ""];
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
