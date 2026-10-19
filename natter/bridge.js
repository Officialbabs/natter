// Natter's bridge into WhatsApp Web. Runs before WhatsApp's own scripts, in the page.
//
// Up to Natter: notifications (with the sender's picture, opening the right chat), the chat list
// for Ctrl+K and the dock menu, which chat is open, whether WhatsApp wants a phone linked, and
// clicks on the call buttons. Down from Natter: window.__natter.* calls.
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

  const contexts = new Set();
  const sources = new Set();  // started source nodes that haven't ended
  const streams = new Set();  // microphone streams (voice notes, calls) feeding a context
  const elements = new Set();  // <audio>/<video> played through a context
  let lastStart = 0;
  const IDLE_MS = 30000;  // long enough that a burst of message sounds doesn't cycle the pipeline

  function wake(context) {
    if (context && context.state === "suspended") context.resume().catch(() => {});
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
    contexts.forEach((context) => {
      if (context.state === "running" && !callContexts.has(context)) context.suspend().catch(() => {});
    });
  }

  const Context = window.AudioContext || window.webkitAudioContext;
  if (Context) {
    const Tracked = class extends Context {
      constructor(...args) {
        super(...args);
        contexts.add(this);
        this.addEventListener("statechange", () => { if (this.state === "closed") contexts.delete(this); });
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
        }
        return result;
      };
    }
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
      lastStart = performance.now();
      contexts.forEach(wake);
      return play.apply(this, args);
    };
    setInterval(rest, IDLE_MS);
  }

  function audio() {
    return { contexts: [...contexts].map((c) => c.state), sources: playingSources(), streams: liveStreams(),
             media: mediaPlaying(), idle: Math.round(performance.now() - lastStart) };
  }

  // ---- The chat list ------------------------------------------------------------------------
  // WhatsApp only keeps the visible rows in the page. Each row's position comes from its
  // translateY (or aria-rowindex), so Natter can tell how recent the chat is.

  const ROWS = '[role="listitem"], [role="row"]';

  function nameOf(row) {
    // The name is in the row's title cell. Other titled spans hold the last message, which
    // Natter never reads; the title comes first when the cell isn't marked.
    const span = row.querySelector('[data-testid="cell-frame-title"] span[title]') || row.querySelector("span[title]");
    const name = span && span.getAttribute("title");
    return name ? plainName(name) : "";
  }

  function positionOf(row, fallback) {
    const index = Number(row.getAttribute("aria-rowindex"));
    if (index > 0) return index - 1;
    const match = /translateY\((-?[\d.]+)px\)/.exec(row.style.transform || "");
    const height = row.offsetHeight;
    const position = match && height > 0 ? Math.round(Number(match[1]) / height) : NaN;
    return Number.isFinite(position) ? Math.max(0, position) : fallback;
  }

  function chatRows() {
    const list = document.getElementById("pane-side");
    if (!list) return [];
    const rows = [];
    list.querySelectorAll(ROWS).forEach((row, i) => {
      if (row.parentElement && row.parentElement.closest(ROWS)) return;  // a nested row
      const name = nameOf(row);
      if (name) rows.push([name, positionOf(row, i)]);
    });
    return rows.sort((a, b) => a[1] - b[1]).slice(0, 200);
  }

  // The list is in recency order only with no search typed and the first filter chip ("All")
  // chosen. Chips are told by their selected state and place, not by their words.
  function filtered() {
    const side = document.getElementById("side");
    if (!side) return false;
    const chips = [...side.querySelectorAll('[role="tab"][aria-selected], button[aria-pressed]')]
      .filter((chip) => !chip.closest("#pane-side"));
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
    const header = document.querySelector("#main header");
    if (!header) return "";
    const title = header.querySelector('[data-testid="conversation-info-header-chat-title"]')
      || header.querySelector("span[title]") || header.querySelector('span[dir="auto"]');
    if (!title) return "";
    return plainName((title.getAttribute("title") || textOf(title)).replace(/\s+/g, " "));
  }

  // WhatsApp's link screen (a QR code, or a code to type on the phone), told by its parts rather
  // than its words: no chat list, and a canvas or WhatsApp's data-ref (the QR's payload).
  function linkScreen() {
    return !document.getElementById("pane-side")
      && Boolean(document.querySelector("#app canvas, #app [data-ref], #app [data-link-code]"));
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
    const header = document.querySelector("#main header");
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
    const header = document.querySelector("#main header");
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
    const app = document.getElementById("app") || document.body;
    const pane = document.getElementById("pane-side");
    const header = document.querySelector("#main header");
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
    const ready = Boolean(document.getElementById("pane-side"));
    if (ready !== lastReady) {
      lastReady = ready;
      post({ type: "ready", ready });
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
  }

  // ---- Opening a chat -----------------------------------------------------------------------

  function rowFor(name) {
    for (const span of document.querySelectorAll("#pane-side span[title]")) {
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
    return document.querySelector('#side [contenteditable="true"][role="textbox"]')
      || document.querySelector('#side [contenteditable="true"]')
      || document.querySelector('#side input[type="text"], #side input[type="search"], #side input[role="textbox"]');
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
  function openLink(href) {
    const url = new URL(href);
    if (url.protocol !== "https:" || url.hostname !== "wa.me") return false;
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
    const message = document.querySelector('#main footer [contenteditable="true"]');
    if (!message) return false;
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    // Pasted into the message box, WhatsApp opens its preview. (A drop made by a script it
    // accepts and then ignores.) A handler that takes the files cancels the event.
    return !pasteInto(message, transfer);
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

  window.__natter = {
    clickNotification, openChat, openLink, attach, receive, discard, setClass, activeChat, scan, audio, startCall,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch, { once: true });
  } else {
    watch();
  }
})();
