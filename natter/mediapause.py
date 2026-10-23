"""Other apps' sound pauses while a voice message is recorded, and plays again after.

The page says when a voice message starts and stops being recorded (bridge.js "recording").
At the start, every player on the session bus that says over MPRIS that it's playing, and that
it can pause, is paused. At the end, after RESUME_DELAY, only those Natter paused play again, and
only if they're still paused: one the person played, stopped or closed meanwhile is left as it
is. Never Natter's own players (WebKit's media session may put one up). A call, ringing or
connected, holds them until it's over, then they play again as after a recording. With the setting off
nothing is paused; those paused before it went off still play again. Every call to the bus is
asynchronous, with a short timeout, so the main loop never waits on a player; one that fails or
doesn't answer is passed over without a word. Bus is all that talks to D-Bus, so tests give it a fake.
"""

from __future__ import annotations

import os
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID, DEFAULT_APP_ID

MPRIS = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
TIMEOUT_MS = 500  # for each call to the bus
RESUME_DELAY_MS = 400  # after the recording ends: its last sound isn't talked over
FORGET = 15 * 60  # seconds: players paused longer ago than this aren't played again
QUIT_WAIT_MS = 2 * TIMEOUT_MS  # quitting waits this long at most for the players: a status, then Play
PROC = Path("/proc")


def natter_family(pid: Optional[int] = None, proc: Path = PROC, depth: int = 8) -> set[int]:
    """Natter's process and every process under it, down to `depth`: WebKit's web processes, and
    the D-Bus proxy its sandbox puts between them and the bus. In Flatpak the bus sees other
    numbers for them, and the names (own_name) are what tell."""
    pid = os.getpid() if pid is None else pid
    found, level = {pid}, {pid}
    for _ in range(depth):
        below = set()
        for parent in level:
            try:
                tasks = os.listdir(proc / str(parent) / "task")
            except OSError:
                continue
            for task in tasks:
                try:
                    children = (proc / str(parent) / "task" / task / "children").read_text().split()
                except OSError:
                    continue
                below.update(int(child) for child in children if child.isdigit())
        level = below - found
        if not level:
            break
        found |= level
    return found


def own_name(name: str, app_ids: tuple[str, ...] = (APP_ID, DEFAULT_APP_ID)) -> bool:
    """A player name of Natter's own: WebKit names its media session after the app."""
    return any(name == MPRIS + app_id or name.startswith(MPRIS + app_id + ".") for app_id in app_ids)


def log_line(paused: int, resumed: int) -> str:
    """The log's one line for a recording: how many, never which."""
    return f"natter: voice note: paused {paused} player{'' if paused == 1 else 's'}, resumed {resumed}"


class Bus:
    """The session bus, as VoicePause uses it. Each call answers `done` once, with None (or
    False) for any failure, a timeout among them."""

    def __init__(self, connection: Callable[[], Optional[Gio.DBusConnection]], timeout_ms: int = TIMEOUT_MS) -> None:
        self._connection = connection
        self.timeout_ms = timeout_ms

    def _call(self, name: str, path: str, interface: str, method: str, args: Optional[GLib.Variant],
              reply: str, done: Callable[[Optional[tuple]], None]) -> None:
        try:
            bus = self._connection()
        except Exception:
            bus = None
        if bus is None:
            done(None)
            return

        def finished(source, result) -> None:
            try:
                value = source.call_finish(result).unpack()
            except GLib.Error:
                value = None
            done(value)

        try:
            bus.call(name, path, interface, method, args, GLib.VariantType.new(reply),
                     Gio.DBusCallFlags.NO_AUTO_START, self.timeout_ms, None, finished)
        except (GLib.Error, TypeError, ValueError):
            done(None)

    def _bus(self, method: str, args: Optional[GLib.Variant], reply: str, done: Callable) -> None:
        self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", method, args, reply,
                   lambda value: done(value[0] if value else None))

    def names(self, done: Callable[[Optional[list]], None]) -> None:
        self._bus("ListNames", None, "(as)", done)

    def owner(self, name: str, done: Callable[[Optional[str]], None]) -> None:
        self._bus("GetNameOwner", GLib.Variant("(s)", (name,)), "(s)", done)

    def pid(self, unique: str, done: Callable[[Optional[int]], None]) -> None:
        self._bus("GetConnectionUnixProcessID", GLib.Variant("(s)", (unique,)), "(u)", done)

    def get(self, unique: str, prop: str, done: Callable[[object], None]) -> None:
        self._call(unique, MPRIS_PATH, "org.freedesktop.DBus.Properties", "Get",
                   GLib.Variant("(ss)", (PLAYER, prop)), "(v)", lambda value: done(value[0] if value else None))

    def call(self, unique: str, method: str, done: Callable[[bool], None]) -> None:
        self._call(unique, MPRIS_PATH, PLAYER, method, None, "()", lambda value: done(value is not None))


class VoicePause:
    """Pauses other apps' players while a voice message is recorded, and plays them again after.

    `recording(source, on)` is each page's say (by account id); `call_started()` (a call has
    connected) and a ring hold what would play again; `call_over()` plays it again once no call
    is on; `quit()` plays it again at once. `enabled()` is the setting, and only pausing asks it.
    `calling()` is whether a call is on, rtc.py's ringing and grace too: nothing pauses then,
    and what would play again waits."""

    def __init__(self, bus, enabled: Callable[[], bool], calling: Callable[[], bool] = lambda: False,
                 family: Callable[[], set[int]] = natter_family, later=GLib.timeout_add,
                 cancel=GLib.source_remove, now: Callable[[], float] = time.monotonic,
                 write: Callable[[str], None] = lambda line: print(line, flush=True)) -> None:
        self.bus, self.enabled, self.calling, self.family = bus, enabled, calling, family
        self._later, self._cancel, self._now, self._write = later, cancel, now, write
        self._recording: set[str] = set()  # the pages recording now
        self._paused: dict[str, float] = {}  # unique bus name: when Natter paused it
        self._seen: set[str] = set()  # this round's players, by unique name: one may have several names
        # Each pausing and each resuming is a round; a new one, or a call, leaves the answers
        # still to come from the last one with nothing to do.
        self._round = 0
        self._busy = 0  # calls out to the bus not answered yet
        self._timer = 0  # RESUME_DELAY after the recording ended
        self._resume_after_busy = False
        self._resuming = False
        self._held = False  # a call rang when they'd have played again: they wait for call_over
        self._quitting = False
        self._waiters: list = []  # when_idle's
        self._counts: Optional[list[int]] = None  # [paused, resumed] for the line; None: no line due

    # ---- What Natter tells it ------------------------------------------------------------------

    def recording(self, source: str, on: bool) -> None:
        if on:
            first = not self._recording
            self._recording.add(source)
            if first:
                self._start()
        elif source in self._recording:
            self._recording.discard(source)
            if not self._recording:
                self._end(RESUME_DELAY_MS)

    def call_started(self) -> None:
        """A call has connected: what Natter paused waits for it to end (call_over), so the call
        isn't talked over, and then plays again. Nothing more pauses meanwhile (_live)."""
        self._round += 1  # a resume under way stops where it is: the players wait with the rest
        self._stop_timer()
        self._resume_after_busy = False
        self._resuming = False
        self._held = True

    def call_over(self) -> None:
        """No call is on any more: what a call that only rang held plays again, as after a recording."""
        if self._held and not self._recording and not self._calling():
            self._held = False
            self._end(RESUME_DELAY_MS)

    def quit(self) -> None:
        """Natter quits: what it paused plays again now, while the main loop still runs, a call
        that only rings or not (it ends with Natter)."""
        self._quitting = True
        self._recording.clear()
        self._stop_timer()
        self._held = False
        self._due()

    def idle(self) -> bool:
        """Nothing out on the bus, and nothing waiting to play again but what a ringing call holds."""
        return not self._busy and not self._timer and not self._resume_after_busy

    def when_idle(self, then: Callable[[], None], wait_ms: int = QUIT_WAIT_MS) -> None:
        """`then()` once idle, or after `wait_ms` whatever the players still owe (quitting)."""
        if self.idle():
            then()
            return
        called = False

        def once() -> bool:
            nonlocal called
            if not called:
                called = True
                if once in self._waiters:
                    self._waiters.remove(once)
                then()
            return False

        self._waiters.append(once)
        self._later(wait_ms, once)

    def quit_and_wait(self, wait_ms: int = QUIT_WAIT_MS) -> None:
        """quit() with the main loop over (do_shutdown, Ctrl+C): the bus answers into a context
        of its own, looked at here for `wait_ms` at most. Answers still owed from before go
        unheard, so only what's paused already plays again."""
        context = GLib.MainContext.new()
        context.push_thread_default()
        try:
            self.quit()
            deadline = time.monotonic() + wait_ms / 1000
            while not self.idle() and time.monotonic() < deadline:
                if not context.iteration(False):
                    time.sleep(0.005)
        finally:
            context.pop_thread_default()

    # ---- Pausing ---------------------------------------------------------------------------------

    def _start(self) -> None:
        self._stop_timer()  # a recording within RESUME_DELAY of the last: the last's players wait for this one's end
        self._resume_after_busy = False
        self._resuming = False
        self._held = False  # this recording's end plays them again
        self._forget_old()
        if not self.enabled() or self._calling():
            return
        if self._counts is None:
            self._counts = [0, 0]
        self._round += 1
        self._seen = set()
        round_ = self._round
        self._ask(self.bus.names, done=lambda names: self._listed(round_, names))

    def _live(self, round_: int) -> bool:
        return round_ == self._round and bool(self._recording) and self.enabled() and not self._calling()

    def _listed(self, round_: int, names) -> None:
        if not self._live(round_) or not isinstance(names, list):
            return
        try:
            ours = self.family()
        except OSError:
            ours = {os.getpid()}
        for name in names:
            if isinstance(name, str) and name.startswith(MPRIS) and not own_name(name):
                self._ask(self.bus.owner, name, done=lambda unique: self._owned(round_, unique, ours))

    def _owned(self, round_: int, unique, ours: set[int]) -> None:
        if not self._live(round_) or not isinstance(unique, str) or not unique or unique in self._seen:
            return
        self._seen.add(unique)
        self._ask(self.bus.pid, unique, done=lambda pid: self._owner_pid(round_, unique, pid, ours))

    def _owner_pid(self, round_: int, unique: str, pid, ours: set[int]) -> None:
        # Not known (a sandbox's proxy may not say): the names have told already.
        if pid in ours or not self._live(round_):
            return
        self._ask(self.bus.get, unique, "PlaybackStatus", done=lambda status: self._status(round_, unique, status))

    def _status(self, round_: int, unique: str, status) -> None:
        if status == "Playing" and self._live(round_):
            self._ask(self.bus.get, unique, "CanPause", done=lambda can: self._can_pause(round_, unique, can))

    def _can_pause(self, round_: int, unique: str, can) -> None:
        if can is True and self._live(round_):
            self._ask(self.bus.call, unique, "Pause", done=lambda ok: self._paused_one(round_, unique, ok))

    def _paused_one(self, round_: int, unique: str, ok: bool) -> None:
        if not ok:
            return
        if self._counts is not None:
            self._counts[0] += 1
        self._paused[unique] = self._now()  # a call since holds it with the rest

    # ---- Playing again ---------------------------------------------------------------------------

    def _end(self, delay_ms: int) -> None:
        self._stop_timer()
        self._timer = self._later(delay_ms, self._due)

    def _due(self) -> bool:
        self._timer = 0
        if self._recording:
            return GLib.SOURCE_REMOVE
        if self._busy:
            self._resume_after_busy = True  # once the pausing's answers are in
        else:
            self._resume()
        return GLib.SOURCE_REMOVE

    def _resume(self) -> None:
        self._resume_after_busy = False
        self._forget_old()
        if not self._paused:
            self._finish()
            return
        if self._calling():
            self._held = True  # a call is on: played again once it's over (call_over)
            return
        self._round += 1
        round_ = self._round
        self._resuming = True
        self._busy += 1  # each player is asked before any answer can end the round
        try:
            for unique in list(self._paused):
                self._ask(self.bus.get, unique, "PlaybackStatus",
                          done=lambda status, unique=unique: self._resume_one(round_, unique, status))
        finally:
            self._answered()

    def _resume_one(self, round_: int, unique: str, status) -> None:
        if round_ != self._round:
            return  # another recording has begun, which keeps it paused (or a call: it's forgotten)
        if self._calling():
            self._held = True  # a call rang meanwhile: it waits, as in _resume
            return
        self._paused.pop(unique, None)
        if status == "Paused":
            self._ask(self.bus.call, unique, "Play", done=lambda ok: ok and self._played())

    def _played(self) -> None:
        if self._counts is not None:
            self._counts[1] += 1

    def _finish(self) -> None:
        self._resuming = False
        self._held = False
        self._paused.clear()
        if self._counts is not None:
            self._write(log_line(*self._counts))
        self._counts = None

    # ---- Underneath ------------------------------------------------------------------------------

    def _ask(self, method: Callable, *args, done: Callable) -> None:
        """`method(*args, answer)` on the bus, counted until it answers (once)."""
        self._busy += 1
        answered = False

        def answer(value=None) -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                done(value)
            except Exception:
                traceback.print_exc()
            finally:
                self._answered()

        try:
            method(*args, answer)
        except Exception:
            answer(None)

    def _answered(self) -> None:
        self._busy -= 1
        if self._busy:
            return
        if self._resume_after_busy and not self._recording:
            self._resume()
        elif self._resuming:
            self._resuming = False
            if not self._held:
                self._finish()
        if self.idle():
            for waiter in list(self._waiters):
                waiter()

    def _calling(self) -> bool:
        return not self._quitting and self.calling()

    def _forget_old(self) -> None:
        now = self._now()
        for unique, at in list(self._paused.items()):
            if now - at > FORGET:
                del self._paused[unique]

    def _stop_timer(self) -> None:
        if self._timer:
            self._cancel(self._timer)
            self._timer = 0
