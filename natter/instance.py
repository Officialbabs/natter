"""One Natter Den at a time, whatever happens to its bus name.

GApplication keeps one instance per app id through the D-Bus name alone. On 25 Oct 2026 that
wasn't enough: the desktop froze a hidden Natter Den, something dropped its bus connection, and a
later launch took the free name and became a second full Natter Den, on the same WhatsApp login,
for seven hours. The first one's SIGTERM (GDBus's exit-on-close) waited on its stalled main loop
and never ran. Launches meanwhile hung on the frozen one.

So, as well as the name:

- the instance lock (InstanceLock, take): a primary holds an flock on a file of its own, which
  the kernel lets go of when the process ends, however it ends. A primary that finds it held
  waits a while for the other to end. If the other has provably lost its bus connection (it's
  frozen, as on 25 Oct) it's ended (end_stale); otherwise the new one quits without starting
  WhatsApp;
- the name watchdog (NameWatchdog): a thread, so a stalled main loop doesn't stall it, that asks
  the bus who owns the name. Once the name is surely lost it asks the main loop to quit, and ends
  the process itself if that hasn't happened GRACE seconds later;
- the launch check (probe): before a launch hands its command line to a Natter Den already
  running, it pings it and asks its main loop something, and says so instead of waiting for good
  when either doesn't answer.
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from gi.repository import Gio, GLib

from . import APP_ID, DEFAULT_APP_ID, NAME, OBJECT_PATH
from .memguard import descends, in_flatpak, web_processes

LOCK_WAIT = 20.0  # seconds a new primary waits for an old one, nameless, to end
LOCK_POLL = 0.25
KILL_WAIT = 10.0  # seconds the lock has to come free once a stale holder is ended
WATCH_POLL = 3.0  # seconds between the watchdog's looks
OWNER_TIMEOUT = 2000  # ms the bus has to say who owns the name
GRACE = 10.0  # seconds a Natter Den that lost its name has to quit before it's ended
PING_TIMEOUT = 3000  # ms a running Natter Den has to answer a launch's ping
BUSY_TIMEOUT = 5000  # ms its main loop has to answer after that
PROBE_DEADLINE = 15.0  # seconds the whole launch check may take (connect, ask, ping, main loop)
EXIT_LOST = 1  # the status a Natter Den ended by the watchdog exits with

BUS, BUS_PATH = "org.freedesktop.DBus", "/org/freedesktop/DBus"
NO_OWNER_ERROR = "org.freedesktop.DBus.Error.NameHasNoOwner"
GONE_ERRORS = (NO_OWNER_ERROR, "org.freedesktop.DBus.Error.ServiceUnknown")
NO_REPLY_ERROR = "org.freedesktop.DBus.Error.NoReply"


class _Unknown:
    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()  # the bus didn't say (a timeout, an error): never taken for a loss

NOT_ANSWERING = (f"{NAME} is running but not answering (it may be paused while hidden). "
                 "Try again in a moment.")
BUSY = f"{NAME} is running but busy (catching up after a pause, perhaps). Try again in a moment."
STILL_RUNNING = f"{NAME} is already running but not answering; try again in a minute"


def say(line: str, stream=None) -> None:
    """A line in the journal now, from whichever thread."""
    try:
        print(line, file=stream or sys.stdout, flush=True)
    except Exception:
        pass


# ---- The instance lock ----------------------------------------------------------------------

def lock_path(app_id: str = APP_ID, environ=None, flatpak: Optional[bool] = None) -> Path:
    """Where the lock for `app_id` is: in $XDG_RUNTIME_DIR, which is the user's own and is
    emptied at log-out. In Flatpak that folder is the sandbox's own, a new one for every run, so
    it's in the app's folder there ($XDG_RUNTIME_DIR/app/<id>), which every run of the app
    shares. Without $XDG_RUNTIME_DIR, in a folder of the user's cache."""
    environ = os.environ if environ is None else environ
    if flatpak is None:
        flatpak = Path("/.flatpak-info").exists()
    runtime = environ.get("XDG_RUNTIME_DIR") or ""
    name = f"natter-{app_id}.lock"
    if os.path.isabs(runtime):
        folder = Path(runtime)
        if flatpak:
            folder = folder / "app" / (environ.get("FLATPAK_ID") or app_id)
        return folder / name
    cache = environ.get("XDG_CACHE_HOME") or ""
    if not os.path.isabs(cache):
        cache = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".cache")
    return Path(cache) / "natter-instance" / name


def start_ticks(pid: int, proc: Path = Path("/proc")) -> Optional[int]:
    """When process `pid` started, in clock ticks since boot: with the pid, it names one process."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
        return int(stat.rpartition(")")[2].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class Holder(NamedTuple):
    """What the lock's holder wrote in the file. Only `pid` and `ticks` until it has noted its bus."""
    pid: Optional[int] = None
    ticks: Optional[int] = None
    bus_id: Optional[str] = None  # the bus's own id (GetId)
    unique: Optional[str] = None  # its unique name on that bus


class InstanceLock:
    """An flock on `path`, held for the life of the process. Its file descriptor isn't passed
    on to what Natter starts (WebKit, bwrap), so the lock ends with Natter's own process. The
    file holds the holder's pid, start time and, once it has noted them, its bus and unique
    name on it: for messages, and for end_stale. The lock is what counts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.fd: Optional[int] = None
        self.held = False

    def _open(self) -> int:
        if self.fd is None:
            folder = self.path.parent
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = folder.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise PermissionError(errno.EPERM, "the folder isn't yours alone", str(folder))
            # Not O_TRUNC: the holder's details stay until the lock is taken.
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        return self.fd

    def try_take(self) -> bool:
        """True once this process holds it. Raises OSError when the file can't be had at all."""
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        self.held = True
        self._write()
        return True

    def _write(self, *more: str) -> None:
        try:
            os.ftruncate(self.fd, 0)
            os.pwrite(self.fd, " ".join([str(os.getpid()), str(start_ticks(os.getpid()) or 0), *more]).encode()
                      + b"\n", 0)
        except OSError:
            pass  # only the messages, and ending this one should it freeze, suffer

    def note(self, bus_id: str, unique: str) -> None:
        """Add the bus this holder is on and its unique name there, once it holds the lock."""
        if self.held and bus_id and unique and " " not in bus_id + unique:
            self._write(bus_id, unique)

    def details(self) -> Holder:
        """What the holder wrote, as far as it did."""
        try:
            words = os.pread(self._open(), 256, 0).decode().split()
        except (OSError, UnicodeDecodeError):
            return Holder()
        pid = int(words[0]) if words and words[0].isdigit() and int(words[0]) > 0 else None
        ticks = int(words[1]) if len(words) > 1 and words[1].isdigit() and int(words[1]) > 0 else None
        if len(words) < 4:
            return Holder(pid, ticks)
        return Holder(pid, ticks, words[2], words[3])

    def holder(self) -> Optional[int]:
        """The pid the holder wrote, if it did."""
        return self.details().pid

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)  # the lock goes with it
            self.fd = None
            self.held = False


class Taken(NamedTuple):
    ok: bool  # this process may carry on as the one Natter Den
    how: str  # "taken", "taken over", "ended", "gave up" or "unavailable"
    holder: Optional[int] = None  # the other one's pid, when it said


def pid_words(pid: Optional[int]) -> str:
    return f"pid {pid}" if pid else "pid unknown"


def take(lock: InstanceLock, wait: float = LOCK_WAIT, poll: float = LOCK_POLL,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         log: Callable[[str], None] = say, end_holder: Optional[Callable[[], bool]] = None,
         kill_wait: float = KILL_WAIT) -> Taken:
    """Take the instance lock, waiting up to `wait` seconds for another Natter Den holding it
    to end. Only a primary calls this, so another holder is one without its bus name: quitting,
    or lost. Still held after the wait, `end_holder` (end_stale) may end it, when it can prove
    the holder's bus connection has gone, and the lock is taken once it's free. A lock that
    can't be had at all (no folder for it) doesn't stop Natter: the bus name still keeps a
    second one out, as it did before the lock."""
    try:
        if lock.try_take():
            return Taken(True, "taken")
    except OSError as error:
        log(f"natter: no instance lock ({error.strerror or error}); the bus name alone keeps one {NAME}")
        return Taken(True, "unavailable")
    holder = lock.holder()
    log(f"natter: another {NAME} ({pid_words(holder)}) still runs without its bus name; waiting for it to end")
    deadline = clock() + wait
    while clock() < deadline:
        sleep(poll)
        try:
            if lock.try_take():
                log(f"natter: the other {NAME} ({pid_words(holder)}) has ended; taking over")
                return Taken(True, "taken over", holder)
        except OSError:
            break
    ended = False
    if end_holder is not None:
        try:
            ended = bool(end_holder())
        except Exception:
            traceback.print_exc()
    if ended:
        deadline = clock() + kill_wait
        while clock() < deadline:
            sleep(poll)
            try:
                if lock.try_take():
                    log(f"natter: the other {NAME} ({pid_words(holder)}) has been ended; taking over")
                    return Taken(True, "ended", holder)
            except OSError:
                break
    log(f"natter: another {NAME} ({pid_words(holder)}) still holds {lock.path} after {wait:g} s; "
        "quitting without starting WhatsApp")
    return Taken(False, "gave up", holder)


def primary_line(taken: Taken, app_id: str = APP_ID, pid: Optional[int] = None) -> str:
    """The start-up line saying this is the one Natter Den, and how it got the lock."""
    pid = os.getpid() if pid is None else pid
    how = {"taken": "lock taken", "taken over": f"lock taken over from {pid_words(taken.holder)}",
           "ended": f"lock taken over from {pid_words(taken.holder)}, which was ended",
           "unavailable": "no lock"}.get(taken.how, taken.how)
    return f"natter: primary instance: app id {app_id}, pid {pid}, {how}, on the session bus"


# ---- Talking to the bus from outside the main loop ------------------------------------------

def remote_error(error: GLib.Error) -> str:
    """The D-Bus error name in a GLib.Error, or ''."""
    try:
        name = Gio.DBusError.get_remote_error(error)
        if name:
            return name
    except Exception:
        pass
    message = getattr(error, "message", "") or ""
    if message.startswith("GDBus.Error:"):
        return message[len("GDBus.Error:"):].partition(":")[0]
    return ""


def timed_out(error: GLib.Error) -> bool:
    """Our own wait ran out. The bus's NoReply isn't that: it's sent when the other end left
    without answering."""
    return error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT)


def connect(address: Optional[str] = None, timeout: float = OWNER_TIMEOUT / 1000,
            flags: Optional[Gio.DBusConnectionFlags] = None) -> Gio.DBusConnection:
    """A private connection to the session bus (or `address`), usable from any thread, which
    doesn't end Natter when it closes. Raises GLib.Error, also after `timeout` seconds."""
    if address is None:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    if flags is None:
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    cancellable = Gio.Cancellable()
    timer = threading.Timer(timeout, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        connection = Gio.DBusConnection.new_for_address_sync(address, flags, None, cancellable)
    finally:
        timer.cancel()
    connection.set_exit_on_close(False)
    return connection


def name_owner(connection: Gio.DBusConnection, app_id: str = APP_ID, timeout: int = OWNER_TIMEOUT):
    """Who owns `app_id` on the bus: a unique name, None for no one, or UNKNOWN."""
    try:
        reply = connection.call_sync(BUS, BUS_PATH, BUS, "GetNameOwner", GLib.Variant("(s)", (app_id,)),
                                     GLib.VariantType("(s)"), Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
        return reply.unpack()[0]
    except GLib.Error as error:
        return None if remote_error(error) == NO_OWNER_ERROR else UNKNOWN


def bus_id(connection: Gio.DBusConnection, timeout: int = OWNER_TIMEOUT) -> Optional[str]:
    """The bus's own id (GetId), or None."""
    try:
        reply = connection.call_sync(BUS, BUS_PATH, BUS, "GetId", None, GLib.VariantType("(s)"),
                                     Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
        return reply.unpack()[0] or None
    except GLib.Error:
        return None


class OwnerAsker:
    """name_owner over a private connection of its own, made again when it closes: for the
    watchdog's thread."""

    def __init__(self, app_id: str = APP_ID, timeout: int = OWNER_TIMEOUT,
                 make: Callable[[], Gio.DBusConnection] = connect) -> None:
        self.app_id, self.timeout, self.make = app_id, timeout, make
        self.connection: Optional[Gio.DBusConnection] = None

    def __call__(self):
        try:
            if self.connection is None or self.connection.is_closed():
                self.connection = self.make()
        except GLib.Error:
            self.connection = None
            return UNKNOWN
        return name_owner(self.connection, self.app_id, self.timeout)


# ---- Ending what's left of a lost Natter Den ---------------------------------------------------

PROC = Path("/proc")


def holds_lock(pid: int, fd: int, locks: Optional[str] = None) -> bool:
    """Whether /proc/locks shows `pid` holding the flock on the file open as `fd`."""
    try:
        info = os.fstat(fd)
        text = Path("/proc/locks").read_text() if locks is None else locks
    except OSError:
        return False
    want = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    for line in text.splitlines():
        # "1: FLOCK  ADVISORY  WRITE 1234 00:1a:5678 0 EOF"; a waiter's line has "->" after the number.
        words = line.split()
        if len(words) >= 6 and words[1:4] == ["FLOCK", "ADVISORY", "WRITE"] and words[4] == str(pid) \
                and words[5] == want:
            return True
    return False


def webkit_below(pid: int, proc: Path = PROC) -> set[int]:
    """WebKit's web and network processes under `pid`."""
    from .system import network_processes

    return web_processes(pid, proc) | network_processes(pid, proc)


def webkit_pidfd(pid: int, ancestor: int, proc: Path = PROC) -> Optional[int]:
    """A pidfd for `pid` while it's still one of WebKit's processes under `ancestor`: opened
    first and checked after, so a pid that goes to another process meanwhile is never signalled."""
    try:
        fd = os.pidfd_open(pid)
    except (OSError, AttributeError):
        return None
    try:
        name = (proc / str(pid) / "comm").read_text()
    except OSError:
        name = ""
    if name.startswith("WebKit") and descends(pid, ancestor, proc):
        return fd
    os.close(fd)
    return None


def kill_all(fds: list[int]) -> None:
    for fd in fds:
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass
        finally:
            os.close(fd)


def end_webkit(ancestor: int, proc: Path = PROC) -> None:
    """SIGKILL WebKit's web and network processes under `ancestor` (this process, about to end)."""
    kill_all([fd for fd in (webkit_pidfd(pid, ancestor, proc) for pid in webkit_below(ancestor, proc))
              if fd is not None])


def end_stale(lock: InstanceLock, bus: Optional[Gio.DBusConnection], log: Callable[[str], None] = say,
              proc: Path = PROC, flatpak: Optional[bool] = None, locks: Optional[str] = None) -> bool:
    """End the lock's holder, and its WebKit processes, with SIGKILL, but only when it has
    provably lost its bus connection: it noted the bus it was on, this is the same bus (GetId),
    and its unique name there has no owner. GDBus has then already asked it to stop (its
    exit-on-close SIGTERM), and only a frozen main loop keeps it running. A holder that is
    quitting, or on another bus, still has its unique name, and is left alone. The pid is held
    by a pidfd and checked after: the same start time, and still holding this very lock. Not in
    Flatpak, where each run has a process namespace of its own. True once the signal was sent."""
    flatpak = in_flatpak() if flatpak is None else flatpak
    found = lock.details()
    if flatpak or bus is None or lock.fd is None or None in found or found.pid == os.getpid():
        return False
    if bus_id(bus) != found.bus_id or name_owner(bus, found.unique) is not None:
        return False
    try:
        fd = os.pidfd_open(found.pid)
    except (OSError, AttributeError):
        return False
    family: list[int] = []
    sent = False
    try:
        if start_ticks(found.pid, proc) != found.ticks or not holds_lock(found.pid, lock.fd, locks):
            return False
        # Found while it's alive: once it's gone they're no longer its children.
        family = [child for child in (webkit_pidfd(pid, found.pid, proc) for pid in webkit_below(found.pid, proc))
                  if child is not None]
        log(f"natter: the other {NAME} (pid {found.pid}) lost its bus connection ({found.unique}) but "
            "still runs, frozen perhaps; ending it and its WebKit processes")
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        sent = True
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
        if sent:
            kill_all(family)
        else:
            for child in family:
                os.close(child)


# ---- The name watchdog -----------------------------------------------------------------------

OK, MAYBE, LOST = "ok", "maybe", "lost"


def verdict(closed: bool, owner, mine: str, no_owner_before: bool) -> tuple[str, str]:
    """What one look found, as (OK, MAYBE or LOST, why). LOST only when Natter's connection to
    the bus has closed, another process owns the name, or no one has owned it at two looks in a
    row. A slow bus, a timeout or an error (UNKNOWN) is never a loss."""
    if closed:
        return LOST, "connection closed"
    if owner is UNKNOWN:
        return OK, ""
    if owner is None:
        return (LOST, "no owner") if no_owner_before else (MAYBE, "no owner")
    if owner != mine:
        return LOST, f"owner now {owner}"
    return OK, ""


class NameWatchdog:
    """Looks every `poll` seconds, from a thread of its own, at whether this Natter Den still
    owns its bus name (verdict). Once it surely doesn't, it says so, asks the main loop to quit
    (`on_loss`, which should only queue the quit), and if the process is still there `grace`
    seconds later, runs `before_exit` and ends it with `exit`. A frozen Natter Den's thread runs
    as soon as it's thawed, before a main loop still swapping back in."""

    def __init__(self, own_name: str, closed: Callable[[], bool], on_loss: Callable[[], None],
                 quitting: Callable[[], bool] = lambda: False, ask: Optional[Callable[[], object]] = None,
                 app_id: str = APP_ID, poll: float = WATCH_POLL, grace: float = GRACE,
                 before_exit: Optional[Callable[[], None]] = None, exit: Callable[[int], None] = os._exit,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = say) -> None:
        self.own_name, self.closed, self.on_loss, self.quitting = own_name, closed, on_loss, quitting
        self.ask = ask if ask is not None else OwnerAsker(app_id)
        self.app_id, self.poll, self.grace = app_id, poll, grace
        self.before_exit, self.exit, self.sleep, self.log = before_exit, exit, sleep, log
        self.no_owner = False  # the last look found no owner
        self.lost = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="natter-name-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop looking (a quit has begun: the name it gives up isn't lost). An ending under
        way after a loss goes on."""
        self._stop.set()

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll):
            try:
                if self.check() == LOST:
                    self.end()
                    return
            except Exception:  # one bad look mustn't end the watch
                traceback.print_exc()

    def check(self) -> str:
        """One look: OK, MAYBE or LOST. Says so when it's LOST."""
        if self.quitting() or self._stop.is_set():
            self.no_owner = False
            return OK
        closed = bool(self.closed())
        owner = UNKNOWN if closed else self.ask()
        if self.quitting() or self._stop.is_set():  # a quit began while the bus answered
            self.no_owner = False
            return OK
        state, why = verdict(closed, owner, self.own_name, self.no_owner)
        self.no_owner = state == MAYBE
        if state == LOST:
            self.lost = True
            self.log(f"natter: lost the bus name {self.app_id} ({why}); ending in {self.grace:g} s "
                     f"so a new {NAME} can start")
        return state

    def end(self) -> None:
        """After a loss: the main loop's quit, then, `grace` seconds on, the process's end."""
        try:
            self.on_loss()
        except Exception:
            traceback.print_exc()
        self.sleep(self.grace)
        self.log(f"natter: still running {self.grace:g} s after losing the bus name; ending now")
        if self.before_exit is not None:
            try:
                self.before_exit()
            except Exception:
                traceback.print_exc()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        self.exit(EXIT_LOST)


# ---- Telling the person ----------------------------------------------------------------------

def notice(connection: Optional[Gio.DBusConnection], body: str, sandboxed: Optional[bool] = None) -> bool:
    """A short desktop notification from a Natter Den that hasn't started (or won't): straight to
    the notification server, or through the portal in a sandbox. Never waits more than a
    moment, and never raises. True when it was shown."""
    if connection is None:
        return False
    if sandboxed is None:
        from .system import in_sandbox

        sandboxed = in_sandbox()
    try:
        if sandboxed:
            connection.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                 "org.freedesktop.portal.Notification", "AddNotification",
                                 GLib.Variant("(sa{sv})", ("not-answering", {"title": GLib.Variant("s", NAME),
                                                                              "body": GLib.Variant("s", body)})),
                                 None, Gio.DBusCallFlags.NONE, 2000, None)
        else:
            connection.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                                 "org.freedesktop.Notifications", "Notify",
                                 GLib.Variant("(susssasa{sv}i)", (NAME, 0, DEFAULT_APP_ID, NAME, body, [], {}, 8000)),
                                 GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
        return True
    except GLib.Error:
        return False


# ---- The launch check ------------------------------------------------------------------------

def ping(connection: Gio.DBusConnection, owner: str, path: str = OBJECT_PATH, timeout: int = PING_TIMEOUT):
    """True when `owner` answered a ping, False when it didn't in time, None when it has gone
    (the bus's NoReply: it left without answering) or the ping failed some other way (a launch
    then goes on as before). GDBus answers a ping from a thread of its own."""
    try:
        connection.call_sync(owner, path, "org.freedesktop.DBus.Peer", "Ping", None, None,
                             Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
        return True
    except GLib.Error as error:
        if timed_out(error):
            return False
        return None


def main_loop_answers(connection: Gio.DBusConnection, owner: str, path: str = OBJECT_PATH,
                      timeout: int = BUSY_TIMEOUT):
    """As ping, with a call `owner`'s main loop has to answer: org.gtk.Actions.DescribeAll, the
    one a launch's GApplication makes first."""
    try:
        connection.call_sync(owner, path, "org.gtk.Actions", "DescribeAll", None, None,
                             Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
        return True
    except GLib.Error as error:
        if timed_out(error):
            return False
        return None


def look(app_id: str, path: str, make: Callable[[], Gio.DBusConnection], timeout: int,
         busy_timeout: int) -> tuple[Optional[str], Optional[str], Optional[Gio.DBusConnection]]:
    """The launch check's questions: (the owner, what to tell the person or None, the
    connection, still open for telling them)."""
    try:
        connection = make()
    except GLib.Error:
        return None, None, None
    try:
        owner = name_owner(connection, app_id, timeout)
        if owner is None or owner is UNKNOWN:
            return None, None, connection
        answered = ping(connection, owner, path, timeout)
        if answered is None:
            return None, None, connection
        if not answered:
            return owner, NOT_ANSWERING, connection
        answered = main_loop_answers(connection, owner, path, busy_timeout)
        if answered is None:
            return None, None, connection
        return owner, (None if answered else BUSY), connection
    except BaseException:
        close(connection)
        raise


def close(connection: Optional[Gio.DBusConnection]) -> None:
    if connection is not None:
        try:
            connection.close_sync(None)
        except GLib.Error:
            pass


def probe(app_id: str = APP_ID, path: str = OBJECT_PATH,
          make: Callable[[], Gio.DBusConnection] = connect, timeout: int = PING_TIMEOUT,
          tell: Callable[[Optional[Gio.DBusConnection], str], object] = notice,
          log: Callable[[str], None] = lambda line: say(line, sys.stderr),
          busy_timeout: int = BUSY_TIMEOUT, deadline: float = PROBE_DEADLINE) -> tuple[Optional[str], bool]:
    """Before a launch: (the unique name of the Natter Den running, or None; whether the launch
    may go on). One that doesn't answer a ping is frozen; one whose main loop doesn't answer is
    stalled (swapping back in after a thaw, say). Either way the launch says so and stops,
    rather than hand it a command line it would wait on for good. The questions run on a thread
    of their own, so a bus that is slow to let a connection in (its Hello has no timeout) holds
    the launch up for `deadline` seconds at most; it then goes on as before."""
    done: list = []
    late = threading.Event()
    lock = threading.Lock()

    def run() -> None:
        try:
            found = look(app_id, path, make, timeout, busy_timeout)
        except Exception:
            traceback.print_exc()
            found = (None, None, None)
        with lock:
            if late.is_set():  # the launch went on without it
                close(found[2])
            else:
                done.append(found)

    worker = threading.Thread(target=run, name="natter-probe", daemon=True)
    worker.start()
    worker.join(deadline)
    with lock:
        if not done:
            late.set()
            log(f"natter: the session bus didn't say within {deadline:g} s whether {NAME} is running; going on")
            return None, True
        owner, message, connection = done[0]
    try:
        if message is None:
            return owner, True
        log(f"natter: {message}")
        tell(connection, message)
        return owner, False
    finally:
        close(connection)


def owner_now(app_id: str = APP_ID, make: Callable[[], Gio.DBusConnection] = connect):
    """Who owns `app_id` now (name_owner), over a short-lived connection."""
    try:
        connection = make()
    except GLib.Error:
        return UNKNOWN
    try:
        return name_owner(connection, app_id)
    finally:
        try:
            connection.close_sync(None)
        except GLib.Error:
            pass
