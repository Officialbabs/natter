"""Settings and per-account state, kept as small JSON files under the XDG folders."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as clock
from pathlib import Path
from typing import Any, Optional

from . import APP_ID


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


CONFIG = _xdg("XDG_CONFIG_HOME", ".config") / "natter"
DATA = _xdg("XDG_DATA_HOME", ".local/share") / "natter"
CACHE = _xdg("XDG_CACHE_HOME", ".cache") / "natter"
AUTOSTART = _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_ID}.desktop"


def _downloads() -> str:
    try:
        from gi.repository import GLib

        found = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    except Exception:
        found = None
    return found or str(Path.home() / "Downloads")


def _locale_language() -> str:
    lang = os.environ.get("LANG", "en_US").split(".")[0]
    return lang if re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", lang) else "en_US"


DEFAULTS: dict[str, Any] = {
    "accounts": [{"id": "main", "name": "Personal"}],
    "run_in_background": True,
    "light_background": True,
    "start_at_login": False,
    "tray_icon": True,
    "notifications": True,
    "notify_sender": True,
    "notify_preview": True,
    "notify_sound": True,
    "quiet_hours": False,
    "quiet_start": "22:00",
    "quiet_end": "07:00",
    "privacy_blur": False,
    "blur_when_away": False,
    "dock_recent_chats": True,
    "search_provider": True,
    "spellcheck": True,
    "spell_languages": [_locale_language()],
    "download_dir": "",
    "zoom": 1.0,
    "width": 1180,
    "height": 780,
    "maximized": False,
}


class Settings:
    """Natter's preferences: a dict with defaults, saved on every change."""

    def __init__(self, path: Path = CONFIG / "settings.json") -> None:
        self.path = path
        self.values: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self.values = loaded
        except (OSError, ValueError):
            pass
        self._listeners: list = []

    def __getitem__(self, key: str) -> Any:
        if key in self.values:
            return self.values[key]
        default = DEFAULTS[key]
        return json.loads(json.dumps(default)) if isinstance(default, (list, dict)) else default

    def __setitem__(self, key: str, value: Any) -> None:
        if self[key] == value:
            return
        self.values[key] = value
        self.save()
        for listener in list(self._listeners):
            listener(key, value)

    def connect(self, listener) -> None:
        """Call `listener(key, value)` after every change."""
        self._listeners.append(listener)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, indent=2, sort_keys=True))
        tmp.replace(self.path)

    @property
    def download_dir(self) -> Path:
        return Path(self["download_dir"] or _downloads())

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        if not self["quiet_hours"]:
            return False
        return quiet_at(self["quiet_start"], self["quiet_end"], (now or datetime.now()).time())


def parse_clock(text: str) -> Optional[clock]:
    match = re.fullmatch(r"\s*(\d{1,2})[:.]?(\d{2})\s*", text or "")
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    return clock(hour, minute) if hour < 24 and minute < 60 else None


def quiet_at(start: str, end: str, now: clock) -> bool:
    """Whether `now` falls in start–end. A span like 22:00–07:00 runs past midnight."""
    begin, finish = parse_clock(start), parse_clock(end)
    if begin is None or finish is None or begin == finish:
        return False
    if begin < finish:
        return begin <= now < finish
    return now >= begin or now < finish


@dataclass
class Chat:
    name: str
    seen: float


class ChatIndex:
    """The chats Natter has seen in an account, newest first. Names only: never messages."""

    LIMIT = 500

    def __init__(self, path: Path) -> None:
        self.path = path
        self.chats: dict[str, float] = {}
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self.chats = {str(k): float(v) for k, v in loaded.items()}
        except (OSError, ValueError, TypeError):
            pass

    def add(self, rows, when: Optional[float] = None) -> bool:
        """Remember chats seen in WhatsApp's list as (name, position) pairs; position 0 is the newest.

        A row further down counts as a minute older per place, so scrolling through old chats
        doesn't push them above today's. Saves, and returns True, only when the order changed.
        """
        stamp = when or time.time()
        before = [c.name for c in self.recent(20)]
        known = len(self.chats)
        for name, position in rows:
            name = (name or "").strip()
            if name:
                self.chats[name] = max(self.chats.get(name, 0.0), stamp - 60.0 * max(position, 0))
        if len(self.chats) > self.LIMIT:
            for name, _ in sorted(self.chats.items(), key=lambda kv: kv[1])[: len(self.chats) - self.LIMIT]:
                del self.chats[name]
        changed = len(self.chats) != known or [c.name for c in self.recent(20)] != before
        if changed:
            self.save()
        return changed

    def touch(self, name: str) -> bool:
        return self.add([(name, 0)])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.chats))

    def recent(self, limit: int = 0) -> list[Chat]:
        ordered = [Chat(n, s) for n, s in sorted(self.chats.items(), key=lambda kv: -kv[1])]
        return ordered[:limit] if limit else ordered

    def search(self, query: str, limit: int = 30) -> list[Chat]:
        return rank(query, self.recent())[:limit]


def _fold(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def score(query: str, name: str) -> float:
    """How well `name` matches what was typed: prefix > word start > substring > letters in order."""
    q, n = _fold(query).strip(), _fold(name)
    if not q:
        return 1.0
    if n.startswith(q):
        return 4.0
    if any(word.startswith(q) for word in re.split(r"[\s\-_.,()]+", n)):
        return 3.0
    if q in n:
        return 2.0
    pos = 0
    for ch in q:
        pos = n.find(ch, pos) + 1
        if not pos:
            return 0.0
    return 1.0 - min(0.9, (pos - len(q)) / max(len(n), 1))


def rank(query: str, chats: list[Chat]) -> list[Chat]:
    scored = [(score(query, c.name), c) for c in chats]
    return [c for s, c in sorted(scored, key=lambda sc: (-sc[0], -sc[1].seen)) if s > 0]


def phone_number(text: str) -> Optional[str]:
    """A phone number typed or pasted in any common format, as digits with country code."""
    if not re.fullmatch(r"\s*\+?[\d\s().\-/]{6,24}\s*", text or ""):
        return None
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits if 7 <= len(digits) <= 15 else None
