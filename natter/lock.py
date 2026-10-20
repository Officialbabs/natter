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
from typing import Optional

ITERATIONS = 600_000  # PBKDF2-SHA256, OWASP's figure for 2023 on: about a quarter second here
SHORTEST = 4  # characters
TRIES = 5  # wrong passcodes in a row before Natter makes you wait
WAIT_SECONDS = 30  # the first wait (Attempts.WAITS)
IDLE_CHOICES = (0, 1, 5, 15, 30, 60)  # minutes; 0 = never


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


def idle_label(minutes: int) -> str:
    if minutes == 0:
        return "Never"
    if minutes < 60:
        return f"After {minutes} Minute{'s' if minutes != 1 else ''}"
    return f"After {minutes // 60} Hour{'s' if minutes >= 120 else ''}"


def too_short(passcode: Optional[str]) -> bool:
    return len(passcode or "") < SHORTEST
