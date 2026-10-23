"""Natter's app lock: a passcode kept as a salted PBKDF2 hash, never the passcode itself.

The lock keeps WhatsApp out of sight of someone at the computer while you're away. It isn't
encryption: WhatsApp's data is on disk as before, and whoever can edit Natter's settings can
remove the lock (by deleting "lock_hash" from settings.json).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable, Optional

ITERATIONS = 600_000  # PBKDF2-SHA256, OWASP's figure for 2023 on: about a quarter second here
SHORTEST = 4  # characters
TRIES = 5  # wrong passcodes in a row before Natter makes you wait
WAIT_SECONDS = 30  # the first wait (Attempts.WAITS)
IDLE_CHOICES = (0, 1, 5, 15, 30, 60)  # minutes; 0 = never
# How long a page may run under the lock's cover (WarmUp), in seconds of running there: in all,
# for each page load; once WhatsApp's chat list is up; and once its first chats have been listed
# (or its link screen is up), for what it draws next to them.
WARM_LIMIT = 120
WARM_AFTER_READY = 15
WARM_SETTLE = 3


def make_hash(passcode: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, iterations)
    return f"pbkdf2-sha256${iterations}${salt.hex()}${digest.hex()}"


def check(passcode: str, stored) -> bool:
    """Whether `passcode` is the one `stored` was made from (in constant time). A damaged or
    missing hash never matches."""
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2-sha256":
            return False
        found = hashlib.pbkdf2_hmac("sha256", str(passcode).encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(found, bytes.fromhex(digest))
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


class Attempts:
    """Wrong passcodes in a row: after TRIES of them, none is checked for a while, longer each
    time (WAITS). Kept in `store` (Natter's settings), so quitting and starting again doesn't
    give more tries; the time is the wall clock's, so a restart or a suspend doesn't either."""

    WAITS = (30, 60, 300, 900)  # seconds, after the 1st, 2nd, 3rd and every later lockout

    def __init__(self, store: Optional[dict] = None, clock=time.time) -> None:
        self.store = store if store is not None else {}
        self.clock = clock

    def _get(self, key: str, default):
        try:
            value = self.store[key]
        except KeyError:
            return default
        return value if isinstance(value, (int, float)) else default

    @property
    def wrong(self) -> int:
        return int(self._get("lock_failures", 0))

    def wait(self) -> int:
        """Seconds left before another try is checked (0 = now)."""
        return max(0, int(self._get("lock_wait_until", 0) - self.clock() + 0.999))

    def failed(self) -> None:
        wrong = self.wrong + 1
        if wrong >= TRIES:
            lockouts = int(self._get("lock_lockouts", 0))
            self.store["lock_wait_until"] = self.clock() + self.WAITS[min(lockouts, len(self.WAITS) - 1)]
            self.store["lock_lockouts"] = lockouts + 1
            wrong = 0
        self.store["lock_failures"] = wrong

    def succeeded(self) -> None:
        for key, value in (("lock_failures", 0), ("lock_wait_until", 0), ("lock_lockouts", 0)):
            if self._get(key, 0) != value:
                self.store[key] = value


class WarmUp:
    """Whether an account's page runs under the lock's cover now (Window._behind_lock), for one
    page load at a time.

    Out of sight, WebKit runs a page as a background tab: nothing is drawn, requestAnimationFrame
    never comes and timers run once a second, so WhatsApp would finish starting only after the
    unlock, in front of you. Under the cover it runs as if shown, but nothing of it reaches the
    screen. It runs there only while the lock is on screen, never with a chat open, and only until
    it has been drawn once since it loaded: its first chats listed (or its link screen up) and a
    moment to draw them, else WARM_AFTER_READY seconds after its chat list came up, else
    WARM_LIMIT. Only the time it runs there counts: closing the window pauses it, and the next
    page load starts it again. Then it's out of sight, as any page behind the lock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.loaded(False)

    def loaded(self, chat_open: bool) -> None:
        """A new page has replaced the old one. With `chat_open` (a chat was open in the old
        one), this load isn't run under the cover at all. Natter opens that chat again in it
        (Account._reopen_when_up), but only once unlocked, never under the cover, so this is
        caution now rather than a need."""
        self.drawn = False
        self.skip = chat_open
        self.listed = False  # its first chats were listed (the bridge's "chats", with rows)
        self.spent = 0.0  # seconds it ran under the cover before the current stretch
        self.since: Optional[float] = None  # when the current stretch began
        self.up_at: Optional[float] = None  # ran() when its chat list was first seen up
        self.listed_at: Optional[float] = None  # ran() when that and its first chats were both seen

    def chats_listed(self) -> None:
        self.listed = True

    def ran(self) -> float:
        """Seconds this page load has run under the cover."""
        return self.spent + (self.clock() - self.since if self.since is not None else 0.0)

    def want(self, locked: bool, on_screen: bool, ready: bool, linking: bool = False,
             chat_open: bool = False, stopped: bool = False) -> bool:
        """Whether the page runs under the cover now. `on_screen`: the window is up and this is
        the account it shows. `ready`: WhatsApp's chat list is up; `linking`: its link screen is.
        `stopped`: it can't load (offline, crashing, removed)."""
        if not locked:
            if on_screen and (ready or linking):
                self.drawn = True  # drawn for real: on screen and unlocked
            return self._pause()
        if self.drawn or self.skip or chat_open or stopped or not on_screen:
            return self._pause()
        if self.since is None:
            self.since = self.clock()
        ran = self.ran()
        if (ready or linking) and self.up_at is None:
            self.up_at = ran
        if (linking or (ready and self.listed)) and self.listed_at is None:
            self.listed_at = ran
        if self.due() == 0:
            self.drawn = True
            return self._pause()
        return True

    def due(self) -> Optional[float]:
        """Seconds until want() answers otherwise with nothing else changing, while it's running
        under the cover; None while it isn't."""
        if self.since is None:
            return None
        ends = [WARM_LIMIT]
        if self.up_at is not None:
            ends.append(self.up_at + WARM_AFTER_READY)
        if self.listed_at is not None:
            ends.append(self.listed_at + WARM_SETTLE)
        return max(0.0, min(ends) - self.ran())

    def _pause(self) -> bool:
        if self.since is not None:
            self.spent += self.clock() - self.since
            self.since = None
        return False


def idle_label(minutes: int) -> str:
    if minutes == 0:
        return "Never"
    if minutes < 60:
        return f"After {minutes} Minute{'s' if minutes != 1 else ''}"
    return f"After {minutes // 60} Hour{'s' if minutes >= 120 else ''}"


def too_short(passcode: Optional[str]) -> bool:
    return len(passcode or "") < SHORTEST
