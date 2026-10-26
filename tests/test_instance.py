"""25 Oct 2026: two Natter Dens on the same WhatsApp login for seven hours. The desktop froze a
hidden one, something dropped its bus connection, and a later launch took the free bus name.
What stops that now (natter/instance.py, app.py, __main__.py): the instance lock, the bus-name
watchdog, the launch's ping, and their lines in the journal."""

import contextlib
import fcntl
import io
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from natter import instance
from natter.app import Natter, main as app_main, stop_name_watch
from natter.instance import (BUSY, LOST, MAYBE, NOT_ANSWERING, OK, STILL_RUNNING, UNKNOWN, Holder, InstanceLock,
                             NameWatchdog, OwnerAsker, Taken, end_stale, end_webkit, holds_lock, lock_path,
                             name_owner, primary_line, probe, start_ticks, take, verdict)

OWN, OTHER = ":1.10", ":1.99"


def timeout_error():
    return GLib.Error.new_literal(Gio.io_error_quark(), "Timeout was reached", Gio.IOErrorEnum.TIMED_OUT)


def bus_error(name):
    return Gio.DBusError.new_for_dbus_error(name, "said the bus")


class Clock:
    """A clock that moves on only when slept on."""

    def __init__(self, then=None):
        self.now, self.slept, self.then = 0.0, [], then

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.then:
            self.then(len(self.slept))


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "run" / "natter-a.b.lock"

    def hold(self, pid=4242):
        """Another process's hold on the lock (flock treats each open of the file on its own):
        call what it returns to let go."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"{pid} 1761390000\n".encode())
        held = [fd]

        def let_go():
            if held:
                os.close(held.pop())

        self.addCleanup(let_go)
        return let_go

    def lock(self):
        lock = InstanceLock(self.path)
        self.addCleanup(lock.release)
        return lock


class LockPathTest(unittest.TestCase):
    def test_in_the_runtime_folder_by_app_id(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/x"}
        self.assertEqual(lock_path("io.github.Officialbabs.Natter", env, flatpak=False),
                         Path("/run/user/1000/natter-io.github.Officialbabs.Natter.lock"))
        # A lab (NATTER_APP_ID) has a lock of its own.
        self.assertNotEqual(lock_path("io.github.Officialbabs.Natter.Dev", env, flatpak=False),
                            lock_path("io.github.Officialbabs.Natter", env, flatpak=False))

    def test_in_flatpak_the_folder_every_run_of_the_app_shares(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/1000", "FLATPAK_ID": "io.github.Officialbabs.Natter"}
        self.assertEqual(lock_path("io.github.Officialbabs.Natter", env, flatpak=True),
                         Path("/run/user/1000/app/io.github.Officialbabs.Natter/"
                              "natter-io.github.Officialbabs.Natter.lock"))

    def test_without_a_runtime_folder_in_the_users_cache(self):
        for env in ({"XDG_CACHE_HOME": "/home/x/.cache-elsewhere", "HOME": "/home/x"},
                    {"XDG_RUNTIME_DIR": "relative", "XDG_CACHE_HOME": "/home/x/.cache-elsewhere"}):
            with self.subTest(env):
                self.assertEqual(lock_path("a.b", env, flatpak=False),
                                 Path("/home/x/.cache-elsewhere/natter-instance/natter-a.b.lock"))
        self.assertEqual(lock_path("a.b", {"HOME": "/home/x"}, flatpak=False),
                         Path("/home/x/.cache/natter-instance/natter-a.b.lock"))


class LockTest(Temp):
    def test_taken(self):
        lock = self.lock()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            taken = take(lock)
        self.assertEqual(taken, Taken(True, "taken"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(int(self.path.read_text().split()[0]), os.getpid())
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        # And it is held: another try fails.
        other = InstanceLock(self.path)
        self.addCleanup(other.release)
        self.assertFalse(other.try_take())

    def test_held_then_freed_is_taken_over(self):
        let_go = self.hold(4242)
        clock = Clock(then=lambda looks: looks == 3 and let_go())
        lines = []
        taken = take(self.lock(), sleep=clock.sleep, clock=clock, log=lines.append)
        self.assertEqual(taken, Taken(True, "taken over", 4242))
        self.assertEqual(lines, ["natter: another Natter Den (pid 4242) still runs without its bus name; "
                                 "waiting for it to end",
                                 "natter: the other Natter Den (pid 4242) has ended; taking over"])
        self.assertEqual(int(self.path.read_text().split()[0]), os.getpid())

    def test_held_throughout_gives_up_after_the_wait(self):
        self.hold(4242)
        clock, lines = Clock(), []
        taken = take(self.lock(), wait=20, poll=0.25, sleep=clock.sleep, clock=clock, log=lines.append)
        self.assertEqual(taken, Taken(False, "gave up", 4242))
        self.assertGreaterEqual(clock.now, 20)
        self.assertLess(clock.now, 21)
        self.assertIn("waiting for it to end", lines[0])
        self.assertIn("(pid 4242) still holds", lines[-1])
        self.assertIn("quitting without starting WhatsApp", lines[-1])
        self.assertEqual(self.path.read_text().split()[0], "4242")  # its details left as they were

    def test_not_passed_on_to_what_natter_starts(self):
        lock = self.lock()
        self.assertTrue(lock.try_take())
        self.assertFalse(os.get_inheritable(lock.fd))
        # A child started without closing descriptors (as GLib's spawn may) hasn't got it.
        found = subprocess.run([sys.executable, "-c", "import os; print('\\n'.join(os.path.realpath("
                                "'/proc/self/fd/' + fd) for fd in os.listdir('/proc/self/fd')))"],
                               close_fds=False, capture_output=True, text=True, check=True).stdout
        self.assertNotIn(str(self.path), found.splitlines())

    def test_ends_with_the_process(self):
        script = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
                  "from natter.instance import InstanceLock\n"
                  f"assert InstanceLock({str(self.path)!r}).try_take()\nprint('held', flush=True)\n"
                  "import time; time.sleep(30)\n")
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), "held")
        lock = self.lock()
        self.assertFalse(lock.try_take())
        self.assertEqual(lock.holder(), child.pid)
        child.kill()
        child.wait()
        self.assertTrue(lock.try_take())

    def test_the_file_says_who_and_where(self):
        lock = self.lock()
        self.assertTrue(lock.try_take())
        me = os.getpid()
        self.assertEqual(lock.details(), Holder(me, start_ticks(me)))
        lock.note("bus-a", ":1.5")
        self.assertEqual(lock.details(), Holder(me, start_ticks(me), "bus-a", ":1.5"))
        self.assertEqual(lock.holder(), me)
        lock.note("bus b", ":1.5")  # nothing odd is written
        self.assertEqual(lock.details().bus_id, "bus-a")

    def test_not_noted_unless_held(self):
        self.hold(4242)
        lock = self.lock()
        self.assertFalse(lock.try_take())
        lock.note("bus-a", ":1.5")
        self.assertEqual(lock.details(), Holder(4242, 1761390000))

    def test_a_holder_ended_after_the_wait_is_taken_over(self):
        let_go = self.hold(4242)
        clock, lines, asked = Clock(), [], []

        def end_holder():
            asked.append(clock.now)
            clock.then = lambda looks: let_go()
            return True

        taken = take(self.lock(), wait=20, poll=0.25, sleep=clock.sleep, clock=clock, log=lines.append,
                     end_holder=end_holder)
        self.assertEqual(taken, Taken(True, "ended", 4242))
        self.assertEqual(len(asked), 1)
        self.assertGreaterEqual(asked[0], 20)  # only after the wait
        self.assertEqual(lines[-1], "natter: the other Natter Den (pid 4242) has been ended; taking over")

    def test_a_holder_not_ended_or_slow_to_go_gives_up(self):
        self.hold(4242)
        for ended in (False, True, RuntimeError):
            with self.subTest(ended=ended):
                clock = Clock()

                def end_holder():
                    if ended is RuntimeError:
                        raise RuntimeError
                    return ended

                with contextlib.redirect_stderr(io.StringIO()):
                    taken = take(self.lock(), wait=20, poll=0.25, sleep=clock.sleep, clock=clock,
                                 log=lambda _: None, end_holder=end_holder, kill_wait=10)
                self.assertEqual(taken, Taken(False, "gave up", 4242))
                self.assertLess(clock.now, 31)

    def test_a_folder_others_can_write_to_is_not_used(self):
        self.path.parent.mkdir(mode=0o777)
        os.chmod(self.path.parent, 0o777)
        lines = []
        self.assertEqual(take(self.lock(), log=lines.append), Taken(True, "unavailable"))
        self.assertFalse(self.path.exists())
        self.assertIn("no instance lock", lines[0])


HOLDER = """
import os, subprocess, sys, time
sys.path.insert(0, {root!r})
from natter.instance import InstanceLock
lock = InstanceLock({path!r})
assert lock.try_take()
if {note!r}:
    lock.note(*{note!r})
kids = []
for name in {kids!r}:
    kid = subprocess.Popen([sys.executable, "-c", "import sys, time; open('/proc/self/comm', 'w').write(sys.argv[1]); "
                            "print('named', flush=True); time.sleep(60)", name], stdout=subprocess.PIPE, text=True)
    assert kid.stdout.readline().strip() == "named"
    kids.append(str(kid.pid))
print("held", *kids, flush=True)
time.sleep(60)
"""


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def gone_soon(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not alive(pid)


class EndStaleTest(Temp):
    """Ending a holder that has provably lost its bus connection (end_stale), with real processes:
    a holder that took the lock and noted its bus, and its children."""

    def holder(self, note=("bus-a", ":1.5"), kids=()):
        script = HOLDER.format(root=str(Path(__file__).resolve().parent.parent), path=str(self.path),
                               note=list(note) if note else None, kids=list(kids))
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        words = child.stdout.readline().split()
        self.assertEqual(words[:1], ["held"])
        kid_pids = [int(word) for word in words[1:]]
        for pid in kid_pids:
            self.addCleanup(lambda pid=pid: alive(pid) and os.kill(pid, 9))
        lock = self.lock()
        self.assertFalse(lock.try_take())  # opened, not held
        return child, kid_pids, lock

    def bus(self, bus_id="bus-a", unique_gone=True):
        return FakeBus(GLib.Variant("(s)", (bus_id,)),
                       bus_error("org.freedesktop.DBus.Error.NameHasNoOwner") if unique_gone
                       else GLib.Variant("(s)", (":1.5",)))

    def end(self, lock, bus, **more):
        lines = []
        ended = end_stale(lock, bus, log=lines.append, flatpak=False, **more)
        return ended, lines

    def test_a_frozen_holder_whose_connection_has_gone_is_ended_with_its_webkit_processes(self):
        child, (network, page, other), lock = self.holder(kids=("WebKitNetworkPr", "WebKitWebProces", "firefox"))
        os.kill(child.pid, signal.SIGSTOP)  # frozen, as the desktop freezes a hidden app
        bus = self.bus()
        ended, lines = self.end(lock, bus)
        self.assertTrue(ended)
        self.assertEqual([call[2:4] for call in bus.calls], [("GetId", None), ("GetNameOwner", (":1.5",))])
        self.assertEqual(child.wait(5), -signal.SIGKILL)
        self.assertTrue(gone_soon(network))
        self.assertTrue(gone_soon(page))
        self.assertTrue(alive(other))  # only WebKit's: not a browser it opened, say
        self.assertIn(f"(pid {child.pid}) lost its bus connection (:1.5) but still runs", lines[0])
        self.assertTrue(lock.try_take())

    def test_left_alone_unless_it_has_provably_lost_its_connection(self):
        child, _kids, lock = self.holder()
        cases = {
            "its connection is still there (quitting, say)": dict(bus=self.bus(unique_gone=False)),
            "another bus": dict(bus=self.bus(bus_id="bus-b")),
            "the bus doesn't say which it is": dict(bus=FakeBus(timeout_error(), timeout_error())),
            "the bus doesn't say whether it's there": dict(bus=FakeBus(GLib.Variant("(s)", ("bus-a",)),
                                                                      timeout_error())),
            "no bus": dict(bus=None),
            "in Flatpak": dict(bus=self.bus(), flatpak=True),
            "not holding this lock, by /proc/locks": dict(bus=self.bus(), locks=""),
        }
        for why, case in cases.items():
            with self.subTest(why):
                lines = []
                ended = end_stale(lock, case["bus"], log=lines.append, flatpak=case.get("flatpak", False),
                                  locks=case.get("locks"))
                self.assertFalse(ended)
                self.assertEqual(lines, [])
                self.assertTrue(alive(child.pid))

    def test_a_holder_that_never_noted_its_bus_is_left_alone(self):
        child, _kids, lock = self.holder(note=None)
        self.assertEqual(self.end(lock, self.bus()), (False, []))
        self.assertTrue(alive(child.pid))

    def test_a_pid_now_some_other_process_is_left_alone(self):
        child, _kids, lock = self.holder()
        found = lock.details()
        with mock.patch.object(InstanceLock, "details", lambda self: found._replace(ticks=found.ticks + 1)):
            self.assertEqual(self.end(lock, self.bus()), (False, []))
        self.assertTrue(alive(child.pid))

    def test_never_this_process(self):
        lock = self.lock()
        self.assertTrue(lock.try_take())
        lock.note("bus-a", ":1.5")
        self.assertEqual(self.end(lock, self.bus()), (False, []))

    def test_through_take(self):
        child, _kids, lock = self.holder()
        os.kill(child.pid, signal.SIGSTOP)
        lines = []
        taken = take(lock, wait=0.3, poll=0.05, log=lines.append,
                     end_holder=lambda: end_stale(lock, self.bus(), log=lines.append, flatpak=False))
        self.assertEqual(taken, Taken(True, "ended", child.pid))
        self.assertEqual(lock.holder(), os.getpid())
        self.assertEqual(primary_line(taken, "a.b", 7),
                         f"natter: primary instance: app id a.b, pid 7, lock taken over from pid {child.pid}, "
                         "which was ended, on the session bus")


class HoldsLockTest(Temp):
    def test_by_proc_locks(self):
        lock = self.lock()
        self.assertTrue(lock.try_take())
        info = os.fstat(lock.fd)
        where = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        me = os.getpid()
        self.assertTrue(holds_lock(me, lock.fd))  # the real /proc/locks
        self.assertTrue(holds_lock(me, lock.fd, f"1: FLOCK  ADVISORY  WRITE {me} {where} 0 EOF\n"))
        for text in (f"1: FLOCK  ADVISORY  WRITE {me + 1} {where} 0 EOF",
                     f"1: -> FLOCK  ADVISORY  WRITE {me} {where} 0 EOF",
                     f"1: POSIX  ADVISORY  WRITE {me} {where} 0 EOF",
                     f"1: FLOCK  ADVISORY  WRITE {me} 00:00:1 0 EOF", ""):
            with self.subTest(text):
                self.assertFalse(holds_lock(me, lock.fd, text))


class EndWebKitTest(Temp):
    def test_only_webkits_processes_below(self):
        child = subprocess.Popen([sys.executable, "-c", HOLDER.format(
            root=str(Path(__file__).resolve().parent.parent), path=str(self.path), note=None,
            kids=["WebKitNetworkPr", "sleeper"])], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        network, sleeper = [int(word) for word in child.stdout.readline().split()[1:]]
        self.addCleanup(lambda: alive(sleeper) and os.kill(sleeper, 9))
        end_webkit(child.pid)
        self.assertTrue(gone_soon(network))
        self.assertTrue(alive(sleeper))
        self.assertTrue(alive(child.pid))


def startup_app(**more):
    """What _take_instance uses of the app."""
    events = []
    more.setdefault("get_dbus_connection", lambda: None)
    more.setdefault("get_flags", lambda: Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    app = SimpleNamespace(quit=lambda: events.append("quit"), settings={"accounts": []}, events=events, **more)
    app._take_instance = types.MethodType(Natter._take_instance, app)
    app._start_name_watch = lambda: events.append("watch")
    return app


class Far(Exception):
    """Start-up got as far as the test needs."""


class StartupTest(unittest.TestCase):
    def startup(self, app, taken, take=None):
        out, err = io.StringIO(), io.StringIO()
        take = take or (lambda lock, **_: taken)

        def scope(**_):
            app.events.append("scope")
            return "scope"

        def far():
            raise Far

        with mock.patch("natter.app.take_instance", take), \
                mock.patch("natter.app.lock_path", return_value=Path("/nonexistent/natter-a.b.lock")), \
                mock.patch.object(Adw.Application, "do_startup", lambda self: app.events.append("gtk")), \
                mock.patch("natter.app.instance_notice", lambda bus, body: app.events.append(("notice", body))), \
                mock.patch("natter.app.move_to_own_scope", scope), mock.patch("natter.app.memory_events", far), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                Natter.do_startup(app)
            except Far:
                pass
        return out.getvalue(), err.getvalue()

    def test_held_gives_up_without_starting_pages(self):
        app = startup_app()
        out, err = self.startup(app, Taken(False, "gave up", 4242))
        self.assertTrue(app._gave_up)
        self.assertEqual(app.events, ["gtk", ("notice", STILL_RUNNING), "quit"])  # no scope, no pages
        self.assertIn(STILL_RUNNING, err)
        self.assertNotIn("primary instance", out)
        # The launch that started it, and anything activating it meanwhile, finds nothing to open.
        app.present_window = lambda *_: app.events.append("window")
        self.assertEqual(Natter.do_command_line(app, None), 1)
        Natter.do_activate(app)
        self.assertNotIn("window", app.events)

    def test_a_search_start_gives_up_without_a_notification(self):
        app = startup_app(get_flags=lambda: Gio.ApplicationFlags.IS_SERVICE | Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        _out, err = self.startup(app, Taken(False, "gave up", 4242))
        self.assertTrue(app._gave_up)
        self.assertEqual(app.events, ["gtk", "quit"])
        self.assertIn(STILL_RUNNING, err)

    def test_a_stale_holder_may_be_ended_and_the_bus_is_noted(self):
        bus = SimpleNamespace(get_unique_name=lambda: OWN)
        app = startup_app(get_dbus_connection=lambda: bus)
        seen = {}

        def take(lock, end_holder=None):
            seen.update(lock=lock, end_holder=end_holder)
            lock.note = lambda *args: seen.update(noted=args)
            return Taken(True, "taken")

        with mock.patch("natter.app.end_stale", lambda lock, on: seen.update(ended=(lock, on)) or True), \
                mock.patch("natter.app.bus_id", lambda on: "bus-a"):
            self.startup(app, None, take)
            self.assertTrue(seen["end_holder"]())
        self.assertEqual(seen["ended"], (seen["lock"], bus))
        self.assertEqual(seen["noted"], ("bus-a", OWN))

    def test_taken_says_so_and_watches_the_name(self):
        app = startup_app()
        out, _ = self.startup(app, Taken(True, "taken over", 4242))
        self.assertFalse(app._gave_up)
        self.assertEqual(app.events[:2], ["watch", "scope"])
        self.assertIn(f"natter: primary instance: app id {instance.APP_ID}, pid {os.getpid()}, "
                      "lock taken over from pid 4242, on the session bus", out)


class VerdictTest(unittest.TestCase):
    def test_the_table(self):
        rows = [
            # closed, owner, no owner at the last look → state
            ((True, OWN, False), (LOST, "connection closed")),
            ((True, UNKNOWN, False), (LOST, "connection closed")),
            ((False, OTHER, False), (LOST, f"owner now {OTHER}")),
            ((False, None, True), (LOST, "no owner")),
            ((False, None, False), (MAYBE, "no owner")),
            ((False, UNKNOWN, False), (OK, "")),  # a timeout or an error
            ((False, UNKNOWN, True), (OK, "")),
            ((False, OWN, False), (OK, "")),
        ]
        for (closed, owner, before), expected in rows:
            with self.subTest(closed=closed, owner=owner, before=before):
                self.assertEqual(verdict(closed, owner, OWN, before), expected)


class FakeBus:
    """A connection whose calls answer, or raise, as told."""

    def __init__(self, *answers):
        self.answers, self.calls, self.closed = list(answers), [], False

    def call_sync(self, name, path, interface, method, params, reply_type, flags, timeout, cancellable):
        self.calls.append((name, interface, method, params.unpack() if params is not None else None, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def is_closed(self):
        return False

    def close_sync(self, cancellable):
        self.closed = True


class OwnerTest(unittest.TestCase):
    def test_what_the_bus_says(self):
        self.assertEqual(name_owner(FakeBus(GLib.Variant("(s)", (OWN,))), "a.b"), OWN)
        self.assertIsNone(name_owner(FakeBus(bus_error("org.freedesktop.DBus.Error.NameHasNoOwner")), "a.b"))
        self.assertIs(name_owner(FakeBus(timeout_error()), "a.b"), UNKNOWN)
        self.assertIs(name_owner(FakeBus(bus_error("org.freedesktop.DBus.Error.AccessDenied")), "a.b"), UNKNOWN)

    def test_asked_with_a_short_timeout(self):
        bus = FakeBus(GLib.Variant("(s)", (OWN,)))
        name_owner(bus, "a.b")
        self.assertEqual(bus.calls, [("org.freedesktop.DBus", "org.freedesktop.DBus", "GetNameOwner", ("a.b",), 2000)])

    def test_a_connection_that_cannot_be_made_is_unknown(self):
        def make():
            raise timeout_error()

        self.assertIs(OwnerAsker("a.b", make=make)(), UNKNOWN)


def watchdog(answers, closed=lambda: False, quitting=lambda: False, **more):
    events = []
    answers = list(answers)

    def ask():
        events.append("ask")
        return answers.pop(0)

    dog = NameWatchdog(OWN, closed, on_loss=lambda: events.append("on_loss"), quitting=quitting, ask=ask,
                       app_id="a.b", poll=0.01, grace=10, before_exit=lambda: events.append("before_exit"),
                       exit=lambda status: events.append(("exit", status)),
                       sleep=lambda seconds: events.append(("sleep", seconds)), log=events.append, **more)
    return dog, events


class WatchdogTest(unittest.TestCase):
    def test_connection_closed(self):
        dog, events = watchdog([])
        dog.closed = lambda: True
        self.assertEqual(dog.check(), LOST)
        self.assertEqual(events, ["natter: lost the bus name a.b (connection closed); ending in 10 s "
                                  "so a new Natter Den can start"])  # the bus isn't asked

    def test_another_owner(self):
        dog, events = watchdog([OTHER])
        self.assertEqual(dog.check(), LOST)
        self.assertIn(f"(owner now {OTHER})", events[-1])

    def test_no_owner_twice_is_a_loss_once_is_not(self):
        dog, events = watchdog([None, None])
        self.assertEqual(dog.check(), MAYBE)
        self.assertFalse(dog.lost)
        self.assertEqual(dog.check(), LOST)
        self.assertIn("(no owner)", events[-1])

    def test_no_owner_then_owned_again_starts_over(self):
        dog, _ = watchdog([None, OWN, None])
        self.assertEqual([dog.check(), dog.check(), dog.check()], [MAYBE, OK, MAYBE])

    def test_timeouts_and_errors_never_count(self):
        dog, events = watchdog([UNKNOWN, None, UNKNOWN, None, UNKNOWN, UNKNOWN])
        self.assertEqual([dog.check() for _ in range(6)], [OK, MAYBE, OK, MAYBE, OK, OK])
        self.assertFalse(dog.lost)
        self.assertNotIn("on_loss", events)

    def test_not_while_quitting(self):
        quitting = [True]
        dog, events = watchdog([OTHER], quitting=lambda: quitting[0])
        dog.closed = lambda: True
        self.assertEqual(dog.check(), OK)
        self.assertEqual(events, [])

    def test_a_quit_that_begins_while_the_bus_answers(self):
        quitting = [False]
        dog, events = watchdog([])
        dog.ask = lambda: quitting.__setitem__(0, True) or None  # the quit releases the name
        dog.quitting = lambda: quitting[0]
        self.assertEqual(dog.check(), OK)
        self.assertEqual(events, [])

    def test_stopped_it_looks_no_more(self):
        dog, events = watchdog([OTHER])
        dog.stop()
        self.assertEqual(dog.check(), OK)
        self.assertEqual(events, [])


class GraceTest(unittest.TestCase):
    def test_quit_asked_then_ended_after_the_grace(self):
        dog, events = watchdog([OTHER])
        dog.start()
        dog._thread.join(5)
        self.assertFalse(dog._thread.is_alive())
        self.assertEqual(events[0], "ask")
        self.assertIn("lost the bus name", events[1])
        self.assertEqual(events[2:4], ["on_loss", ("sleep", 10)])
        self.assertIn("ending now", events[4])
        self.assertEqual(events[5:], ["before_exit", ("exit", instance.EXIT_LOST)])

    def test_a_main_loop_that_never_runs_does_not_hold_it_up(self):
        # Frozen, then thawed with the main loop still swapping in: the quit it queues never
        # runs, and the thread ends the process all the same.
        dog, events = watchdog([None, None])
        quits = []
        dog.on_loss = lambda: GLib.idle_add(lambda: quits.append(1))
        dog.start()
        dog._thread.join(5)
        self.assertEqual(quits, [])
        self.assertEqual(events[-1], ("exit", instance.EXIT_LOST))

    def test_a_look_that_fails_does_not_end_the_watch(self):
        dog, events = watchdog([OTHER])
        looks = iter([RuntimeError("broken on purpose")])

        def ask():
            for error in looks:
                raise error
            return OTHER

        dog.ask = ask
        with contextlib.redirect_stderr(io.StringIO()):
            dog.start()
            dog._thread.join(5)
        self.assertEqual(events[-1], ("exit", instance.EXIT_LOST))

    def test_stopped_before_a_loss_it_never_ends_the_process(self):
        answered = threading.Event()
        dog, events = watchdog([OWN] * 1000)
        dog.ask = lambda: answered.set() or OWN
        dog.start()
        self.assertTrue(answered.wait(2))
        dog.stop()
        dog._thread.join(2)
        self.assertFalse(dog._thread.is_alive())
        self.assertNotIn(("exit", instance.EXIT_LOST), events)

    def test_real_exit_is_os_exit(self):
        dog = NameWatchdog(OWN, lambda: False, lambda: None, ask=lambda: OWN)
        self.assertIs(dog.exit, os._exit)
        self.assertEqual(dog.grace, 10)
        self.assertGreaterEqual(dog.poll, 2)
        self.assertLessEqual(dog.poll, 5)


class WiringTest(unittest.TestCase):
    def test_a_loss_quits_on_the_main_loop(self):
        made = {}

        class Dog:
            def __init__(self, own, closed, on_loss, **more):
                made.update(own=own, closed=closed, on_loss=on_loss, **more)

            def start(self):
                made["started"] = True

        bus = SimpleNamespace(get_unique_name=lambda: OWN, is_closed=lambda: False)
        quits = []
        app = SimpleNamespace(get_dbus_connection=lambda: bus, quit_natter=lambda: quits.append(1))
        with mock.patch("natter.app.NameWatchdog", Dog):
            Natter._start_name_watch(app)
        self.assertEqual(made["own"], OWN)
        self.assertTrue(made["started"])
        self.assertFalse(made["quitting"]())
        app._quitting = True
        self.assertTrue(made["quitting"]())
        made["on_loss"]()  # from the watchdog's thread: queued, not run
        self.assertEqual(quits, [])
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        while not quits and time.monotonic() < deadline:
            context.iteration(False)
        self.assertEqual(quits, [1])
        ended = []
        with mock.patch("natter.app.in_flatpak", lambda: False), \
                mock.patch("natter.app.end_webkit", ended.append):
            made["before_exit"]()
        self.assertEqual(ended, [os.getpid()])

    def test_a_quit_stops_it_first(self):
        events = []
        app = SimpleNamespace(name_watch=SimpleNamespace(stop=lambda: events.append("watch.stop")))
        stop_name_watch(app)
        self.assertEqual(events, ["watch.stop"])
        stop_name_watch(SimpleNamespace())  # none started (a remote, a give-up)


class ProbeTest(unittest.TestCase):
    def probe(self, *answers):
        bus, told, lines = FakeBus(*answers), [], []
        found = probe("a.b", "/a/b", make=lambda: bus, tell=lambda connection, body: told.append(body),
                      log=lines.append)
        self.assertTrue(bus.closed)
        return found, bus, told, lines

    NOW = GLib.Variant("(s)", (OWN,))
    PONG = GLib.Variant("()", ())

    def test_nothing_running(self):
        found, bus, told, lines = self.probe(bus_error("org.freedesktop.DBus.Error.NameHasNoOwner"))
        self.assertEqual(found, (None, True))
        self.assertEqual(len(bus.calls), 1)  # no ping
        self.assertEqual((told, lines), ([], []))

    def test_answered(self):
        found, bus, told, lines = self.probe(self.NOW, self.PONG, GLib.Variant("()", ()))
        self.assertEqual(found, (OWN, True))
        self.assertEqual(bus.calls[1], (OWN, "org.freedesktop.DBus.Peer", "Ping", None, 3000))
        self.assertEqual(bus.calls[2], (OWN, "org.gtk.Actions", "DescribeAll", None, 5000))
        self.assertEqual((told, lines), ([], []))

    def test_not_answered(self):
        found, bus, told, lines = self.probe(self.NOW, timeout_error())
        self.assertEqual(found, (OWN, False))
        self.assertEqual(len(bus.calls), 2)
        self.assertEqual(told, [NOT_ANSWERING])
        self.assertEqual(lines, ["natter: Natter Den is running but not answering (it may be paused while "
                                 "hidden). Try again in a moment."])

    def test_a_stalled_main_loop_is_busy(self):
        # GDBus answers the ping from its own thread; the main loop, swapping back in after a
        # thaw, doesn't answer what GApplication would ask it next.
        found, _bus, told, lines = self.probe(self.NOW, self.PONG, timeout_error())
        self.assertEqual(found, (OWN, False))
        self.assertEqual(told, [BUSY])
        self.assertEqual(lines, [f"natter: {BUSY}"])

    def test_gone_meanwhile_or_odd_answers_go_on(self):
        no_reply = bus_error("org.freedesktop.DBus.Error.NoReply")  # it left without answering
        for answers in ((self.NOW, bus_error("org.freedesktop.DBus.Error.ServiceUnknown")),
                        (self.NOW, bus_error("org.freedesktop.DBus.Error.AccessDenied")),
                        (self.NOW, no_reply), (self.NOW, self.PONG, no_reply),
                        (self.NOW, self.PONG, bus_error("org.freedesktop.DBus.Error.UnknownMethod")),
                        (timeout_error(),)):
            with self.subTest(answers[-1]):
                found, _bus, told, _lines = self.probe(*answers)
                self.assertEqual(found, (None, True))
                self.assertEqual(told, [])

    def test_no_connection_goes_on(self):
        def make():
            raise timeout_error()

        self.assertEqual(probe("a.b", make=make, tell=lambda *_: None, log=lambda _: None), (None, True))

    def test_a_bus_slow_to_let_it_in_holds_the_launch_up_for_the_deadline_at_most(self):
        # GDBus's Hello, while connecting, has no timeout of its own.
        release, bus, lines = threading.Event(), FakeBus(bus_error("org.freedesktop.DBus.Error.NameHasNoOwner")), []

        def make():
            release.wait(10)
            return bus

        started = time.monotonic()
        found = probe("a.b", make=make, tell=lambda *_: None, log=lines.append, deadline=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(found, (None, True))
        self.assertIn("didn't say within 0.2 s", lines[0])
        release.set()  # the connection made late is closed, not left open
        deadline = time.monotonic() + 5
        while not bus.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(bus.closed)


@contextlib.contextmanager
def launcher(probed, ran):
    """__main__.main with its checks passing, its probe answering `probed` and app.main logging to `ran`."""
    from natter import gstfix, system

    with mock.patch.object(gstfix, "apply", lambda: None), mock.patch.object(gstfix, "start_own", lambda: None), \
            mock.patch("natter.__main__.check", return_value=0), mock.patch.object(system, "hide_system_bus"), \
            mock.patch.object(Gio, "bus_get_sync", lambda *_: object()), \
            mock.patch.object(instance, "probe", lambda: probed or ran.append("probe") or (None, True)), \
            mock.patch("natter.app.main", lambda argv, owner=None: ran.append(("run", argv[1:], owner)) or 0):
        yield


class LaunchTest(unittest.TestCase):
    def main(self, argv, probed=None):
        from natter.__main__ import main

        ran = []
        with launcher(probed, ran), mock.patch.object(sys, "argv", ["natter", *argv]):
            status = main()
        return status, ran

    def test_unanswered_exits_without_handing_over(self):
        status, ran = self.main(["--chat", "Mum"], probed=(OWN, False))
        self.assertEqual((status, ran), (1, []))

    def test_answered_hands_over_as_before(self):
        status, ran = self.main(["--chat", "Mum"], probed=(OWN, True))
        self.assertEqual((status, ran), (0, [("run", ["--chat", "Mum"], OWN)]))

    def test_the_service_and_help_are_not_probed(self):
        for argv in (["--gapplication-service"], ["--help"], ["--version"]):
            with self.subTest(argv):
                status, ran = self.main(argv, probed=(OWN, False))
                self.assertEqual((status, ran), (0, [("run", argv, None)]))

    def test_background_is_probed(self):
        status, ran = self.main(["--background"])
        self.assertEqual(ran, ["probe", ("run", ["--background"], None)])


class FakeApp:
    def __init__(self, statuses, made):
        self.statuses, self.restart_args = statuses, None
        made.append(self)

    def run(self, argv):
        return self.statuses.pop(0)

    def set_default(self):
        pass


class RetryTest(unittest.TestCase):
    """The search service leaving between a launch's look and its command line (D5)."""

    def main(self, statuses, owner, now):
        made = []
        with mock.patch("natter.app.Natter", lambda: FakeApp(statuses, made)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = app_main(["natter"], owner, now=lambda: now)
        return status, len(made), err.getvalue()

    def test_gone_meanwhile_runs_once_more(self):
        for now in (None, OTHER):
            with self.subTest(now=now):
                status, runs, err = self.main([1, 0], OWN, now)
                self.assertEqual((status, runs), (0, 2))
                self.assertIn("ended before it answered; trying once more", err)

    def test_only_once(self):
        self.assertEqual(self.main([1, 1, 0], OWN, None)[:2], (1, 2))

    def test_not_when_it_is_still_there_or_unknown(self):
        for now in (OWN, UNKNOWN):
            with self.subTest(now=now):
                self.assertEqual(self.main([1, 0], OWN, now)[:2], (1, 1))

    def test_not_when_the_first_run_became_the_one_natter_den(self):
        # It holds the lock still: another run in this process would wait on its own lock.
        made = []

        def first():
            app = FakeApp([1, 0], made)
            app.instance_lock = object()
            return app

        with mock.patch("natter.app.Natter", first), contextlib.redirect_stderr(io.StringIO()):
            status = app_main(["natter"], OWN, now=lambda: None)
        self.assertEqual((status, len(made)), (1, 1))

    def test_not_when_this_was_the_first(self):
        self.assertEqual(self.main([1, 0], None, None)[:2], (1, 1))
        self.assertEqual(self.main([0, 0], OWN, None)[:2], (0, 1))


class LogLineTest(unittest.TestCase):
    def test_primary(self):
        self.assertEqual(primary_line(Taken(True, "taken"), "a.b", 7),
                         "natter: primary instance: app id a.b, pid 7, lock taken, on the session bus")
        self.assertEqual(primary_line(Taken(True, "taken over", 4242), "a.b", 7),
                         "natter: primary instance: app id a.b, pid 7, lock taken over from pid 4242, "
                         "on the session bus")
        self.assertEqual(primary_line(Taken(True, "taken over"), "a.b", 7),
                         "natter: primary instance: app id a.b, pid 7, lock taken over from pid unknown, "
                         "on the session bus")
        self.assertEqual(primary_line(Taken(True, "unavailable"), "a.b", 7),
                         "natter: primary instance: app id a.b, pid 7, no lock, on the session bus")

    def test_no_bus_address(self):
        line = primary_line(Taken(True, "taken"), "a.b", 7)
        self.assertNotIn("unix:", line)
        self.assertNotIn("path=", line)

    def test_the_messages(self):
        self.assertEqual(STILL_RUNNING, "Natter Den is already running but not answering; try again in a minute")
        self.assertEqual(NOT_ANSWERING, "Natter Den is running but not answering (it may be paused while hidden). "
                                        "Try again in a moment.")


if __name__ == "__main__":
    unittest.main()
