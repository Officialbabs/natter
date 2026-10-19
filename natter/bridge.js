// Natter's bridge into WhatsApp Web. Runs before WhatsApp's own scripts, in the page.
//
// Up to Natter: notifications (with the sender's picture, opening the right chat), the chat list
// for Ctrl+K and the dock menu, which chat is open, and opening a chat by name.
// Down from Natter: window.__natter.* calls.
(() => {
  "use strict";
  if (window.__natter) return;

  const post = (message) => {
    try { window.webkit.messageHandlers.natter.postMessage(message); } catch (_) { /* not in Natter */ }
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // ---- Notifications ------------------------------------------------------------------------
  // WhatsApp's `new Notification(...)` lands here and is shown by Natter, so a click can come
  // back to the same object and run WhatsApp's own handler, which opens that chat.

  const shown = new Map();
  let nextId = 1;

  async function iconData(url) {
    if (!url) return "";
    if (/^https:/.test(url)) return url;  // Natter fetches it: the picture host sends no CORS headers
    if (!/^(blob|data):/.test(url)) return "";
    try {
      const blob = await (await fetch(url)).blob();
      if (blob.size > 2e6) return "";
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
  // pipeline running, a few percent of CPU all day, so Natter suspends each context once
  // nothing has played through it for a few seconds, and resumes it before anything plays.

  const contexts = new Set();
  let playing = 0;
  let streams = 0;
  const IDLE_MS = 30000;  // long enough that a burst of message sounds doesn't cycle the pipeline

  function wake(context) {
    if (context && context.state === "suspended") context.resume().catch(() => {});
  }

  function mediaPlaying() {
    return [...document.querySelectorAll("audio, video")].some((m) => !m.paused && !m.ended);
  }

  function rest() {
    if (playing > 0 || streams > 0 || mediaPlaying()) return;
    contexts.forEach((context) => {
      if (context.state === "running") context.suspend().catch(() => {});
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
        // The microphone (voice notes, calls): stay awake while its tracks are live.
        streams++;
        stream.getTracks().forEach((track) => track.addEventListener("ended", () => { streams = Math.max(0, streams - 1); }, { once: true }));
        wake(this);
        return super.createMediaStreamSource(stream);
      }
      createMediaElementSource(element) {
        wake(this);
        element.addEventListener("play", () => wake(this));
        return super.createMediaElementSource(element);
      }
    };
    window.AudioContext = Tracked;
    if (window.webkitAudioContext) window.webkitAudioContext = Tracked;

    // AudioBufferSourceNode has a start() of its own in some engines, so each kind is patched.
    for (const Node of [window.AudioScheduledSourceNode, window.AudioBufferSourceNode,
                        window.OscillatorNode, window.ConstantSourceNode]) {
      if (!Node || !Object.prototype.hasOwnProperty.call(Node.prototype, "start")) continue;
      const start = Node.prototype.start;
      Node.prototype.start = function (...args) {
        if (!this.__natterCounted) {
          this.__natterCounted = true;
          playing++;
          this.addEventListener("ended", () => { playing = Math.max(0, playing - 1); }, { once: true });
        }
        wake(this.context);
        return start.apply(this, args);
      };
    }
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
      contexts.forEach(wake);
      return play.apply(this, args);
    };
    setInterval(rest, IDLE_MS);
  }

  function audio() {
    return { contexts: [...contexts].map((c) => c.state), playing, streams, media: mediaPlaying() };
  }

  // ---- The chat list ------------------------------------------------------------------------
  // WhatsApp only keeps the visible rows in the page. Each row's position comes from its
  // translateY (or aria-rowindex), so Natter can tell how recent the chat is.

  const ROWS = '[role="listitem"], [role="row"]';

  function nameOf(row) {
    // The first titled span in a row is the chat's name; later ones hold the last message,
    // which Natter never reads.
    const span = row.querySelector("span[title]");
    const name = span && span.getAttribute("title");
    return name && name.trim() ? name.trim() : "";
  }

  function positionOf(row, fallback) {
    const index = Number(row.getAttribute("aria-rowindex"));
    if (index > 0) return index - 1;
    const match = /translateY\((-?[\d.]+)px\)/.exec(row.style.transform || "");
    const height = row.offsetHeight;
    if (match && height > 0) return Math.round(Number(match[1]) / height);
    return fallback;
  }

  function chatRows() {
    const list = document.querySelector("#pane-side");
    if (!list) return [];
    const rows = [];
    list.querySelectorAll(ROWS).forEach((row, i) => {
      if (row.parentElement && row.parentElement.closest(ROWS)) return;  // a nested row
      const name = nameOf(row);
      if (name) rows.push([name, positionOf(row, i)]);
    });
    return rows.sort((a, b) => a[1] - b[1]);
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
    return (title.getAttribute("title") || textOf(title)).replace(/\s+/g, " ").trim();
  }

  let lastRows = "";
  let lastActive = null;
  let scanQueued = false;

  function scan() {
    scanQueued = false;
    const rows = chatRows();
    const signature = JSON.stringify(rows);
    if (signature !== lastRows && rows.length) {
      lastRows = signature;
      post({ type: "chats", rows });
    }
    const active = activeChat();
    if (active !== lastActive) {
      lastActive = active;
      post({ type: "active", name: active });
    }
    const ready = Boolean(document.querySelector("#pane-side"));
    if (ready !== lastReady) {
      lastReady = ready;
      post({ type: "ready", ready });
    }
  }
  let lastReady = null;

  function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    setTimeout(scan, 800);
  }

  function watch() {
    const root = document.querySelector("#app") || document.body;
    if (!root) return setTimeout(watch, 500);
    new MutationObserver(queueScan).observe(root, {
      childList: true, subtree: true, attributes: true, attributeFilter: ["style", "title"],
    });
    queueScan();
  }

  // ---- Opening a chat -----------------------------------------------------------------------

  function rowFor(name) {
    for (const span of document.querySelectorAll("#pane-side span[title]")) {
      if (span.getAttribute("title") === name) {
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

  function pasteInto(composer, transfer) {
    composer.focus();
    return composer.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  }

  async function attach(items) {
    const files = items.map((item) => new File(incoming.get(item.id) || [], item.name, { type: item.type }));
    items.forEach((item) => incoming.delete(item.id));
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

  window.__natter = { clickNotification, openChat, attach, receive, setClass, activeChat, scan, audio };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch, { once: true });
  } else {
    watch();
  }
})();
