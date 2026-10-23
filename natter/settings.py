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

from . import APP_ID, DEFAULT_APP_ID, OLD_APP_ID


def folder_name(app_id: str = APP_ID) -> str:
    """Natter's folder under each XDG folder: "natter", or one of its own for a lab instance."""
    if app_id in (DEFAULT_APP_ID, OLD_APP_ID):
        return "natter"  # the same folders before and after the rename: sessions carry over
    return "natter-" + app_id.rsplit(".", 1)[-1].lower()


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


FOLDER = folder_name()
CONFIG = _xdg("XDG_CONFIG_HOME", ".config") / FOLDER
DATA = _xdg("XDG_DATA_HOME", ".local/share") / FOLDER
CACHE = _xdg("XDG_CACHE_HOME", ".cache") / FOLDER
AUTOSTART = _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_ID}.desktop"


def private_folder(path: Path) -> Path:
    """`path` made if needed, readable by this user only (WhatsApp sessions live under it)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text` all at once, readable by this user only.

    A crash or a full disk mid-write leaves the old file, never half of the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        os.fchmod(out.fileno(), 0o600)  # a .tmp left over from before may have looser permissions
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)


def _downloads() -> str:
    try:
        from gi.repository import GLib

        found = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    except Exception:
        found = None
    return found or str(Path.home() / "Downloads")


def spell_languages(names: Optional[list[str]] = None) -> list[str]:
    """The user's languages the way spell-check dictionaries are named: "pt_BR", "en".

    A bare language goes when a regional one of it is there already ("pt_BR" covers "pt").
    """
    if names is None:
        try:
            from gi.repository import GLib

            names = GLib.get_language_names()
        except Exception:
            names = [os.environ.get("LANG", "")]
    found: list[str] = []
    for name in names:
        if re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", name) and name not in found:
            found.append(name)
    found = [n for n in found if "_" in n or not any(f.startswith(n + "_") for f in found)]
    return found or ["en_US"]


DEFAULTS: dict[str, Any] = {
    "accounts": [{"id": "main", "name": "Personal"}],
    "run_in_background": True,
    "light_background": True,
    "start_at_login": False,
    "tray_icon": True,
    "notifications": True,
    "notify_sender": True,
    "notify_preview": True,
    # Reply on message notifications (reply.py): off turns it off everywhere, should WhatsApp's
    # page change under it.
    "notify_reply": True,
    "quiet_hours": False,
    "quiet_start": "22:00",
    "quiet_end": "07:00",
    "dnd_until": 0,  # do not disturb: 0 = off, -1 = until turned off, else a Unix time
    "lock_hash": "",  # the app lock's passcode, hashed (lock.py); "" = no lock
    "lock_on_start": True,
    "lock_on_hide": False,
    "lock_idle_minutes": 0,  # 0 = never
    "locked": False,  # internal: locked when Natter last ran (so quitting doesn't unlock it)
    "lock_failures": 0,  # internal: wrong passcodes in a row (lock.Attempts)
    "lock_wait_until": 0,  # internal: no passcode is checked before this (Unix time)
    "lock_lockouts": 0,  # internal: how many waits so far (they grow)
    "privacy_blur": False,
    "blur_when_away": False,
    "dock_recent_chats": True,
    "search_provider": True,
    "spellcheck": True,
    "spell_languages": spell_languages(),
    "download_dir": "",
    "zoom": 1.0,
    "width": 1180,
    "height": 780,
    "maximized": False,
    "appearance": "system",  # "system", "light" or "dark"
    # A themes.py id: "den", "custom", "plain" or one of WhatsApp's ("wa-pink"). Read through
    # themes.chosen(): an older theme ("lagoon") counts as the WhatsApp theme nearest to it, and
    # anything else, hand-edited or missing (an upgrade), as the default, Den.
    "theme": "den",
    # Custom's colour, "#rrggbb". Read through themes.custom_colour(): anything else counts as
    # Den's orange.
    "theme_custom": "#ff7800",
    "hardware_acceleration": True,
    # What WhatsApp may use, in every account (media.py): the desktop's camera permission is
    # one for the whole app, so these are too.
    "microphone": True,
    "camera": True,
    "pause_other_audio": True,  # other apps' players pause while a voice message is recorded (mediapause.py)
    "calls_ring": True,  # keep the linked call window running, so calls ring
    "calls_linked": False,  # internal: the call window has been linked once
    "sw_cleared": False,  # whether WhatsApp's old service-worker data has been cleared, once
    "first_run_done": False,
}


class Settings:
    """Natter's preferences: a dict with defaults, saved on every change."""

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            # Natter's own settings: its folders come first, private.
            for folder in (CONFIG, DATA, CACHE):
                private_folder(folder)
            path = CONFIG / "settings.json"
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

    def disconnect(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def save(self) -> None:
        write_private(self.path, json.dumps(self.values, indent=2, sort_keys=True))

    @property
    def download_dir(self) -> Path:
        return Path(self["download_dir"] or _downloads())

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        if not self["quiet_hours"]:
            return False
        return quiet_at(self["quiet_start"], self["quiet_end"], (now or datetime.now()).time())

    def do_not_disturb(self, now: Optional[float] = None) -> bool:
        """Whether notifications are paused from the tray (for a while, or until turned off)."""
        until = self["dnd_until"]
        if not isinstance(until, (int, float)) or until == 0:
            return False
        return until < 0 or until > (time.time() if now is None else now)

    def notifications_paused(self) -> bool:
        return self.do_not_disturb() or self.in_quiet_hours()


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
        write_private(self.path, json.dumps(self.chats))

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
