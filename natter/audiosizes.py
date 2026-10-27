"""How much the bridge's idle-audio code holds (bridge.js audio()), for the log and About's
troubleshooting text.

The bridge keeps sets of the page's AudioContexts (and of those it suspended, and those carrying
a call), of the media elements played through them, of the sources still playing and of the
microphone streams feeding them, and a map of the notifications it showed; and how many contexts
a connected worklet or script processor keeps from being suspended ("held"). Each is pruned as what
is in it ends, so each should stay small; one that doesn't is a leak. That shows here the way a
selector WhatsApp dropped shows in the canary's line: a line once the chat list is up, then only
when a size passes its limit or keeps growing check after check, never a line every check. Only
counts: no names, no URLs, nothing of what the sets hold.
"""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import GLib

# The sizes audio() reports, in the order they're written.
KEYS = ("contexts", "rested", "calls", "elements", "sources", "streams", "held", "notifications")
# Past these a size is told, and its limit doubles: a few lines however far it grows. The bridge
# keeps its last 300 notifications (bridge.js), so past that means its cap stopped working.
LIMITS = {"contexts": 8, "rested": 8, "calls": 4, "elements": 32, "sources": 32, "streams": 4, "held": 4,
          "notifications": 300}
# Sizes told when they grow at every check for a while. Not the notifications: that map grows
# with each message up to its cap by design, and only passing the cap is news.
GROWING = ("contexts", "rested", "calls", "elements", "sources", "streams", "held")
FIRST = 10  # seconds after the chat list is up, as the canary's (bridge.js scan)
CHECK = 600  # seconds between checks after that
AFTER_CALL = 60  # seconds before a check that came during a call is tried again
STREAK = 6  # checks in a row a size grew (an hour) before that's told; doubles each time it is
SIZE_MAX = 10**7  # a count past this isn't one

SCRIPT = "return window.__natter && window.__natter.audio ? window.__natter.audio().sizes : null"


def parse(value) -> Optional[dict[str, int]]:
    """audio()'s sizes as the page answered, checked: a whole number for each of KEYS (anything
    else it says is dropped), or None for an answer that isn't that."""
    if not isinstance(value, dict):
        return None
    sizes = {key: value.get(key) for key in KEYS}
    if not all(isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= SIZE_MAX
               for size in sizes.values()):
        return None
    return sizes


def describe(sizes: dict[str, int]) -> str:
    """The sizes as the log and the debug info write them: "contexts 1, rested 1, calls 0, …"."""
    return ", ".join(f"{key} {sizes[key]}" for key in KEYS)


def debug_line(sizes: Optional[dict[str, int]], index: int = 0, accounts: int = 1) -> str:
    """About's troubleshooting line for one account's sizes (the `index`th of `accounts`)."""
    which = f" (account {index + 1})" if accounts > 1 else ""
    return f"Audio sets{which}: {describe(sizes) if sizes else 'not known (WhatsApp is not up)'}"


class Rule:
    """Which checks of one page get a line. The first, at ready, always does. After it a check
    does when a size passes its limit, which then doubles, or has grown at each of the last
    checks for a streak, which then doubles too: a set that leaks for days is told a handful of
    times, and one that settles isn't told again."""

    def __init__(self) -> None:
        self.last: Optional[dict[str, int]] = None
        self.limits = dict(LIMITS)
        self.streaks = dict.fromkeys(GROWING, STREAK)
        self.runs = dict.fromkeys(GROWING, 0)  # checks in a row each grew at

    def check(self, sizes: dict[str, int]) -> Optional[str]:
        """What this check is told as ("at ready", "growing (…)"), or None for no line."""
        last, self.last = self.last, sizes
        reasons = []
        for key in KEYS:
            size = sizes[key]
            if size > self.limits[key]:
                if last is not None:  # at ready the line has them all anyway
                    reasons.append(f"{key} past {self.limits[key]}")
                while size > self.limits[key]:
                    self.limits[key] *= 2
            if key in self.runs and last is not None:
                self.runs[key] = self.runs[key] + 1 if size > last[key] else 0
                if self.runs[key] >= self.streaks[key]:
                    reasons.append(f"{key} up {self.runs[key]} checks running")
                    self.runs[key] = 0
                    self.streaks[key] *= 2
        if last is None:
            return "at ready"
        return f"growing ({'; '.join(reasons)})" if reasons else None


class Watch:
    """One account's checks: FIRST seconds after each new page's chat list is up (ready()), then
    every CHECK seconds while the account lasts, skipped while its page isn't up, and put off
    while a call is on. Each page starts with a Rule of its own, as its bridge starts with empty
    sets."""

    def __init__(self, account, later: Optional[Callable[[int, Callable[[], bool]], int]] = None,
                 cancel: Optional[Callable[[int], object]] = None,
                 write: Optional[Callable[[str], None]] = None) -> None:
        self.account = account  # its name, loaded_at, ready, removed, calling and run() are what's used
        self._later = later or GLib.timeout_add_seconds
        self._cancel = cancel or GLib.source_remove
        self._write = write or (lambda line: print(line, flush=True))
        self.rule = Rule()
        self.page: Optional[float] = None  # the account's loaded_at for the page being checked
        self.last: Optional[dict[str, int]] = None  # that page's latest sizes
        self._timer = 0

    def ready(self) -> None:
        """The account's chat list is up. On a new page, its checks start again."""
        if self.page == self.account.loaded_at:
            return  # up again on the same page (a link opened in it): its checks go on
        self.page = self.account.loaded_at
        self.rule = Rule()
        self.last = None
        self._plan(FIRST)

    def stop(self) -> None:
        if self._timer:
            self._cancel(self._timer)
            self._timer = 0

    def ask(self, done: Callable[[Optional[dict[str, int]]], None]) -> None:
        """The sizes now, for About: `done(sizes)`, or `done(None)` when the page can't say. Not
        a check: nothing is logged."""
        self.account.run(SCRIPT, None, lambda value: done(parse(value)))

    def _plan(self, seconds: int) -> None:
        self.stop()
        self._timer = self._later(seconds, self._check)

    def _check(self) -> bool:
        self._timer = 0
        if self.account.removed:
            return GLib.SOURCE_REMOVE
        if self.account.calling:
            self._plan(AFTER_CALL)  # not during a call: the page and Natter's main loop carry its sound
            return GLib.SOURCE_REMOVE
        self._plan(CHECK)
        if self.account.ready:
            page = self.page
            self.account.run(SCRIPT, None, lambda value: self._answered(page, value))
        return GLib.SOURCE_REMOVE

    def _answered(self, page: Optional[float], value) -> None:
        sizes = parse(value)
        if sizes is None or page != self.page or page != self.account.loaded_at:
            return  # no bridge, or the page it asked has gone since
        self.last = sizes
        told = self.rule.check(sizes)
        if told is None and GLib.getenv("NATTER_DEBUG"):
            told = "checked"
        if told is not None:
            self._write(f"natter: {self.account.name}: audio sets {told}: {describe(sizes)}")
