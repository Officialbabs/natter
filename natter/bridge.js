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

  function activeChat() {
    const header = document.querySelector("#main header");
    if (!header) return "";
    const span = header.querySelector("span[title]") || header.querySelector('span[dir="auto"]');
    return span ? (span.getAttribute("title") || span.textContent || "").trim() : "";
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
  // Natter hands over natter:// links it made for the chosen files; they're dropped on the open
  // chat as if dragged there, which brings up WhatsApp's own preview with a caption box.

  async function attach(items) {
    const main = document.querySelector("#main");
    if (!main) return false;
    const files = [];
    for (const item of items) {
      const blob = await (await fetch(item.url)).blob();
      files.push(new File([blob], item.name, { type: item.type || blob.type }));
    }
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    const box = main.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, dataTransfer: transfer,
                 clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 };
    main.dispatchEvent(new DragEvent("dragenter", at));
    main.dispatchEvent(new DragEvent("dragover", at));
    await sleep(250);
    const zone = document.elementFromPoint(at.clientX, at.clientY) || main;
    zone.dispatchEvent(new DragEvent("dragenter", at));
    zone.dispatchEvent(new DragEvent("dragover", at));
    zone.dispatchEvent(new DragEvent("drop", at));
    await sleep(600);
    if (document.querySelector('#app [role="dialog"], #app span[data-icon="send"], #app [data-icon="wds-ic-send-filled"]')) return true;
    // No preview came up: paste them into the message box instead, which WhatsApp also takes.
    const composer = document.querySelector('#main footer [contenteditable="true"]');
    if (!composer) return false;
    composer.focus();
    composer.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
    return true;
  }

  // ---- Classes Natter's style sheet hangs off -----------------------------------------------

  function setClass(name, on) {
    document.documentElement.classList.toggle(name, Boolean(on));
  }

  window.__natter = { clickNotification, openChat, attach, setClass, activeChat, scan };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch, { once: true });
  } else {
    watch();
  }
})();
