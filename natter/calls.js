// Natter's script in the call window's WhatsApp page (Chromium, see callwindow.py). It opens a
// chat and starts a call there, and tells the window when a call rings, starts and ends.
// Messages go out as console.debug("__natter_calls__" + JSON), which the window reads.
(() => {
  "use strict";
  if (window.__natterCalls) return;

  const post = (message) => console.debug("__natter_calls__" + JSON.stringify(message));
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const INVISIBLE = /[​‎‏‪-‮⁠⁦-⁩﻿]/g;
  const plainName = (text) => (text || "").replace(INVISIBLE, "").trim().slice(0, 200);
  const ROWS = '[role="listitem"], [role="row"]';

  // ---- Chats (the same way Natter's main window finds them) ---------------------------------

  function textOf(element) {
    return [...element.childNodes].map((node) => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent;
      if (node.tagName === "IMG") return node.getAttribute("alt") || "";
      return node.nodeType === Node.ELEMENT_NODE ? textOf(node) : "";
    }).join("");
  }

  function nameOf(row) {
    const span = row.querySelector('[data-testid="cell-frame-title"] span[title]') || row.querySelector("span[title]");
    return span ? plainName(span.getAttribute("title")) : "";
  }

  function activeChat() {
    const header = document.querySelector("#main header");
    if (!header) return "";
    const title = header.querySelector('[data-testid="conversation-info-header-chat-title"]')
      || header.querySelector("span[title]") || header.querySelector('span[dir="auto"]');
    return title ? plainName((title.getAttribute("title") || textOf(title)).replace(/\s+/g, " ")) : "";
  }

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

  async function openChat(name) {
    name = plainName(name);
    if (activeChat() === name) return true;
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
    for (let i = 0; i < 30 && activeChat() !== name; i++) await sleep(100);
    if (searching()) setSearch("");
    return activeChat() === name;
  }

  // ---- Calls: found by what buttons say or show, not by class -------------------------------

  function words(element) {
    const icons = [...element.querySelectorAll("[data-icon]")].map((icon) => icon.getAttribute("data-icon"));
    return [element.getAttribute("aria-label"), element.getAttribute("title"), element.getAttribute("data-icon"),
            ...icons].filter(Boolean).join(" ");
  }

  const buttons = (root = document) => [...root.querySelectorAll('button, [role="button"]')];

  function callButton(video) {
    const header = document.querySelector("#main header");
    if (!header) return null;
    return buttons(header).find((button) => {
      const said = words(button);
      return /call/i.test(said) && /video/i.test(said) === video;
    }) || null;
  }

  async function startCall(name, video) {
    if (!document.getElementById("pane-side")) return { ok: false, reason: "not-ready" };
    if (!(await openChat(name))) return { ok: false, reason: "no-chat" };
    let button = null;
    for (let i = 0; i < 30 && !button; i++) {
      button = callButton(video);
      if (!button) await sleep(100);
    }
    if (!button) return { ok: false, reason: "no-call-button" };
    press(button);
    // Some versions ask "Start call?" first: confirm it.
    await sleep(600);
    const confirm = buttons().find((b) => /^(call|start call|voice call|video call)$/i.test((b.getAttribute("aria-label") || b.textContent || "").trim())
      && b.closest('[role="dialog"]'));
    if (confirm) press(confirm);
    return { ok: true };
  }

  // An incoming call shows answer and decline buttons together; a call in progress has an end
  // button. Their labels are matched loosely (English for now; icons as a fallback).
  const ANSWER = /\b(accept|answer)\b|call-accept|accept-call|voice-call-accept|video-call-accept/i;
  const DECLINE = /\b(decline|reject|ignore)\b|call-decline|decline-call|call-reject/i;
  const HANG_UP = /\b(end call|end|leave|hang up)\b|end-call|call-end|hangup/i;

  function ringingParts() {
    const all = buttons();
    const answer = all.find((b) => ANSWER.test(words(b) || b.textContent || ""));
    const decline = all.find((b) => DECLINE.test(words(b) || b.textContent || ""));
    return answer && decline ? { answer, decline } : null;
  }

  function callerName(parts) {
    // The nearest box holding both buttons, then its first titled or plain text that isn't a button.
    let box = parts.answer.parentElement;
    while (box && !box.contains(parts.decline)) box = box.parentElement;
    if (!box) return "";
    const titled = box.querySelector("span[title]");
    if (titled) return plainName(titled.getAttribute("title"));
    const label = [...box.querySelectorAll("span, div, h1, h2")].map((e) => (e.childElementCount ? "" : e.textContent))
      .find((text) => text && text.trim() && !ANSWER.test(text) && !DECLINE.test(text));
    return plainName(label || "");
  }

  function inCall() {
    return buttons().some((b) => HANG_UP.test(words(b)) && !ANSWER.test(words(b)));
  }

  function answer() {
    const parts = ringingParts();
    if (!parts) return false;
    press(parts.answer);
    return true;
  }

  function decline() {
    const parts = ringingParts();
    if (!parts) return false;
    press(parts.decline);
    return true;
  }

  // ---- Watching ------------------------------------------------------------------------------

  let lastState = "";
  let seenState = "";
  let seenSince = 0;
  let lastRinging = false;
  let lastInCall = false;
  const SETTLE_MS = 2500;  // WhatsApp redraws its QR code every few minutes: don't report the blink

  function check() {
    const state = document.getElementById("pane-side") ? "ready"
      : document.querySelector("#app canvas, #app [data-ref], #app [data-link-code]") ? "needs-link" : "loading";
    if (state !== seenState) {
      seenState = state;
      seenSince = performance.now();
    }
    const settled = state === "ready" || performance.now() - seenSince >= SETTLE_MS;
    if (state !== lastState && settled) {
      lastState = state;
      post({ type: "state", value: state });
    }
    const parts = ringingParts();
    const talking = !parts && inCall();
    if (parts && !lastRinging) {
      const video = /video/i.test(words(parts.answer)) || /video/i.test(parts.answer.closest("div")?.textContent || "");
      post({ type: "incoming", name: callerName(parts), video });
    } else if (!parts && lastRinging) {
      post({ type: "incoming-ended" });
    }
    lastRinging = Boolean(parts);
    if (talking !== lastInCall) {
      lastInCall = talking;
      post({ type: "in-call", value: talking });
    }
  }

  // ---- What the call screen is made of (for building Natter's own call window) --------------
  // The structure only: layers, sizes, control labels and icons, video and audio elements.

  function box(element) {
    const r = element.getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
  }

  function callSnapshot(reason) {
    const layers = [...document.querySelectorAll("body *")].filter((element) => {
      const style = getComputedStyle(element);
      if (style.position !== "fixed" && style.position !== "absolute") return false;
      const [, , width, height] = box(element);
      return width >= 200 && height >= 150;
    }).slice(0, 60).map((element) => ({
      tag: element.tagName.toLowerCase(), role: element.getAttribute("role") || "",
      testid: element.getAttribute("data-testid") || "", label: (element.getAttribute("aria-label") || "").slice(0, 60),
      position: getComputedStyle(element).position, z: getComputedStyle(element).zIndex, box: box(element),
      inApp: Boolean(element.closest("#app")), parentTestid: element.parentElement?.getAttribute("data-testid") || "",
    }));
    const controls = buttons().map((button) => ({
      label: (button.getAttribute("aria-label") || button.getAttribute("title") || "").slice(0, 60),
      icons: [...button.querySelectorAll("[data-icon]")].map((icon) => icon.getAttribute("data-icon")).join(","),
      box: box(button), inMain: Boolean(button.closest("#main")), inSide: Boolean(button.closest("#side")),
    })).filter((control) => (control.label || control.icons) && !control.inSide);
    const media = [...document.querySelectorAll("video, audio, canvas")].map((element) => ({
      tag: element.tagName.toLowerCase(), box: box(element),
      stream: Boolean(element.srcObject), playing: element.paused === false,
    }));
    post({ type: "dom", reason, path: location.pathname, layers, controls, media,
           frames: [...document.querySelectorAll("iframe")].map((frame) => ({ src: (frame.src || "").slice(0, 80), box: box(frame) })) });
  }

  const snapshotted = new Set();
  setInterval(() => {
    const parts = ringingParts();
    const talking = !parts && inCall();
    for (const [reason, now] of [["ringing", Boolean(parts)], ["in-call", talking]]) {
      if (now && !snapshotted.has(reason)) {
        snapshotted.add(reason);
        setTimeout(() => callSnapshot(reason), 1500);  // once the call screen has settled
      } else if (!now) {
        snapshotted.delete(reason);
      }
    }
  }, 700);

  setInterval(check, 700);
  window.__natterCalls = { openChat, startCall, answer, decline, check, activeChat, snapshot: callSnapshot };
})();
