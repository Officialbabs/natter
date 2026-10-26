"""Quitting (app.py quit_natter, do_shutdown, end_pages; Account.end_page; MemoryGuard.stop).

WebKit's web processes used to be left to end on their own once Natter had gone, each running its
own exit with its threads still going, and now and then one crashed there (the kernel's
"WebKitWebProces[…]: segfault … in libc.so.6", in malloc) or hung. Now Natter ends them first:
the guard stops, then the pages (their calls' connections closed first), then the sandbox anchor,
each with WebKit's terminate; one still running QUIT_GRACE later Natter ends itself; and it quits
once they're gone, QUIT_WAIT at most, with the main loop running meanwhile. Whatever fails on the
way, it still quits. Real processes here are the test's own children, never anything else."""

import contextlib
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib

from natter import memguard
from natter.account import Account
from natter.app import QUIT_GRACE, QUIT_WAIT, Natter, end_pages, process_ended


class Log:
    """Stands in for anything: each call made on it goes, with its name, into the shared `events`."""

    def __init__(self, name, events, **values):
        self._name, self._events = name, events
        self.__dict__.update(values)

    def __getattr__(self, attribute):
        if attribute.startswith("__"):
            raise AttributeError(attribute)
        return lambda *args, **kwargs: self._events.append(f"{self._name}.{attribute}")


def run_loop(until, seconds=5.0):
    """Run the default main loop until `until()` or `seconds` have passed."""
    context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.002)


def broken(*_args):
    raise RuntimeError("lab: broken on purpose")


class Pages:
    """Web processes, by pid, that end only when told to (`end`, or `kill` when `killable`), and
    a count of the looks at them and of the kills sent."""

    def __init__(self, *pids, killable=True):
        self.running = set(pids)
        self.looks = 0
        self.killed = []
        self.killable = killable

    def find(self):
        return set(self.running)

    def ended(self, pid):
        self.looks += 1
        return pid not in self.running

    def kill(self, pid):
        self.killed.append(pid)
        if self.killable:
            self.running.discard(pid)
        return self.killable

    def args(self):
        return {"find": self.find, "ended": self.ended, "kill": self.kill}


class EndPagesTest(unittest.TestCase):
    def test_pages_first_then_the_anchor_then_quit_once_they_are_gone(self):
        events = []
        pages = Pages(11, 12)
        accounts = [Log("first", events), Log("second", events)]
        anchor = Log("anchor", events)
        end_pages(accounts, anchor, lambda: events.append("quit"), **pages.args())
        self.assertEqual(events, ["first.end_page", "second.end_page", "anchor.terminate_web_process"])
        run_loop(lambda: pages.looks > 3, seconds=1)
        self.assertNotIn("quit", events)  # still running: no quit yet, and the main loop runs meanwhile
        pages.running.clear()
        run_loop(lambda: "quit" in events)
        self.assertEqual(events[-1], "quit")
        self.assertEqual(events.count("quit"), 1)
        self.assertEqual(pages.killed, [])  # WebKit ended them: nothing for Natter to end itself

    def test_quits_at_once_when_they_end_at_once(self):
        events = []
        pages = Pages(11)
        account = Log("page", events)
        account.end_page = lambda: (events.append("page.end_page"), pages.running.clear())
        end_pages([account], None, lambda: events.append("quit"), **pages.args())
        self.assertEqual(events, ["page.end_page", "quit"])

    def test_nothing_to_end_quits_at_once(self):
        events = []
        end_pages([], None, lambda: events.append("quit"), find=lambda: self.fail("no need to look"))
        self.assertEqual(events, ["quit"])

    def test_one_webkit_did_not_end_is_ended_after_the_grace(self):
        # A web process no view has (WebKit's terminate doesn't reach it): Natter ends it itself.
        events = []
        pages = Pages(11, 12)
        account = Log("page", events)
        account.end_page = lambda: pages.running.discard(11)  # the page's own: WebKit's terminate
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            end_pages([account], None, lambda: events.append("quit"), grace=0.05, **pages.args())
            run_loop(lambda: "quit" in events)
        self.assertEqual(pages.killed, [12])
        self.assertEqual(events, ["quit"])
        self.assertLess(time.monotonic() - started, QUIT_WAIT)  # once it's gone, not after the full wait
        self.assertIn("ended 1 of WebKit's web processes itself", out.getvalue())

    def test_one_that_does_not_end_holds_quitting_up_for_wait_at_most(self):
        events = []
        pages = Pages(11, killable=False)
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            end_pages([Log("page", events)], None, lambda: events.append("quit"), wait=0.15, grace=0.05,
                      **pages.args())
            run_loop(lambda: "quit" in events)
        self.assertIn("quit", events)
        self.assertEqual(pages.killed, [11])  # tried once, not over and over
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("quitting with 1 of WebKit's web processes still running", out.getvalue())

    def test_a_page_that_fails_to_end_does_not_stop_the_others_nor_the_quit(self):
        events = []
        pages = Pages()
        first = Log("first", events)
        first.end_page = broken
        with contextlib.redirect_stderr(io.StringIO()) as err:
            end_pages([first, Log("second", events)], Log("anchor", events), lambda: events.append("quit"),
                      **pages.args())
        self.assertEqual(events, ["second.end_page", "anchor.terminate_web_process", "quit"])
        self.assertIn("broken on purpose", err.getvalue())

    def test_a_look_at_proc_that_fails_does_not_stop_the_quit(self):
        events = []
        with contextlib.redirect_stderr(io.StringIO()):
            end_pages([Log("page", events)], None, lambda: events.append("quit"), find=broken)
        self.assertEqual(events, ["page.end_page", "quit"])

    def test_a_look_that_fails_while_it_waits_still_quits(self):
        # A timer that raises is dropped: without the guard, the quit would never come.
        events = []
        pages = Pages(11)
        looks = []

        def ended(pid):
            looks.append(pid)
            if len(looks) > 1:
                raise OSError("lab: /proc went away")
            return False

        with contextlib.redirect_stderr(io.StringIO()) as err:
            end_pages([Log("page", events)], None, lambda: events.append("quit"), find=pages.find, ended=ended,
                      kill=pages.kill)
            self.assertNotIn("quit", events)  # the first look: still running
            run_loop(lambda: "quit" in events, seconds=2)
        self.assertEqual(events.count("quit"), 1)
        self.assertIn("/proc went away", err.getvalue())

    def test_a_kill_that_fails_still_quits(self):
        events = []
        pages = Pages(11)
        with contextlib.redirect_stderr(io.StringIO()):
            end_pages([Log("page", events)], None, lambda: events.append("quit"), grace=0, find=pages.find,
                      ended=pages.ended, kill=broken)
            run_loop(lambda: "quit" in events, seconds=2)
        self.assertEqual(events.count("quit"), 1)

    def test_without_a_main_loop_it_waits_there_and_then(self):
        # do_shutdown: the main loop has stopped.
        events = []
        pages = Pages(11)
        timer = threading.Timer(0.05, pages.running.clear)
        timer.start()
        self.addCleanup(timer.cancel)
        end_pages([Log("page", events)], None, **pages.args())
        self.assertEqual(pages.running, set())
        self.assertEqual(events, ["page.end_page"])

    def test_without_a_main_loop_it_waits_wait_at_most(self):
        pages = Pages(11, killable=False)
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            end_pages([Log("page", [])], None, wait=0.1, **pages.args())
        self.assertLess(time.monotonic() - started, 0.6)

    def test_the_cap_is_about_a_second(self):
        self.assertLessEqual(QUIT_WAIT, 1.0)
        self.assertLess(QUIT_GRACE, QUIT_WAIT)


class RealProcessTest(unittest.TestCase):
    """end_pages's own look at /proc, and its own kill, with stand-in web processes: children of
    the test named as WebKit's are (memguard.web_processes finds them by name, and memguard.kill
    sends its SIGKILL only to a web process below Natter)."""

    def child(self, name):
        code = f"open('/proc/self/comm', 'w').write({name!r})\nimport time\ntime.sleep(30)\n"
        process = subprocess.Popen([sys.executable, "-I", "-c", code])
        self.addCleanup(lambda: (process.kill(), process.wait()))
        deadline = time.monotonic() + 5
        while memguard._read(f"/proc/{process.pid}/comm").strip() != name and time.monotonic() < deadline:
            time.sleep(0.01)
        return process

    def test_waits_for_the_web_process_it_ended(self):
        page = self.child("WebKitWebProces")
        self.assertIn(page.pid, memguard.web_processes())
        events = []

        def end_page():
            events.append("end_page")
            threading.Timer(0.1, page.kill).start()  # as WebKit's terminate: a SIGKILL, a moment later

        account = SimpleNamespace(end_page=end_page)
        end_pages([account], None, lambda: events.append(("quit", process_ended(page.pid))),
                  kill=lambda pid: self.fail("WebKit ended it"))
        self.assertEqual(events, ["end_page"])
        run_loop(lambda: len(events) > 1)
        self.assertEqual(events, ["end_page", ("quit", True)])  # a zombie not yet reaped counts as ended

    def test_ends_one_webkit_did_not_end_itself(self):
        page = self.child("WebKitWebProces")
        other = self.child("python3")  # not a web process: left alone
        events = []
        with contextlib.redirect_stdout(io.StringIO()) as out:
            end_pages([SimpleNamespace(end_page=lambda: None)], None, lambda: events.append("quit"), grace=0.05)
            run_loop(lambda: events)
        self.assertEqual(events, ["quit"])
        self.assertTrue(process_ended(page.pid))
        self.assertEqual(page.wait(timeout=5), -9)  # SIGKILL
        self.assertIsNone(other.poll())
        self.assertIn("ended 1 of WebKit's web processes itself", out.getvalue())

    def test_ended(self):
        page = self.child("WebKitWebProces")
        self.assertFalse(process_ended(page.pid))
        page.kill()
        deadline = time.monotonic() + 5
        while not process_ended(page.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(process_ended(page.pid))  # a zombie
        page.wait()
        self.assertTrue(process_ended(page.pid))  # gone

    def test_ended_reads_a_name_with_brackets_and_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            (proc / "7").mkdir()
            (proc / "7" / "stat").write_text("7 (a) b (c) S 1 1 1 0 -1 0\n")
            (proc / "8").mkdir()
            (proc / "8" / "stat").write_text("8 (a) Z (c)) Z 1 1 1 0 -1 0\n")
            self.assertFalse(process_ended(7, proc))
            self.assertTrue(process_ended(8, proc))
            self.assertTrue(process_ended(9, proc))


def quitting_app(events, accounts=(), anchor=None, guard=True):
    """What quit_natter uses of the app, each part logging to `events`."""
    app = SimpleNamespace(
        accounts=list(accounts), replies={}, tray=Log("tray", events), window=None, ada_style=None,
        desktop_media=None, calls=None, memory_guard=Log("guard", events) if guard else None,
        notifier=Log("notifier", events), reclaimer=Log("reclaimer", events), call_sound=Log("call_sound", events),
        audio_follower=Log("audio_follower", events),
        voice_pause=Log("voice_pause", events, when_idle=lambda then: then()),
        withdraw_notification=lambda key: events.append("withdraw"),
        quit=lambda: events.append("quit"))
    if anchor is not None:
        app._sandbox_anchor = anchor
    return app


def account(name, events):
    return Log(name, events, chats=Log(f"{name}.chats", events))


@contextlib.contextmanager
def web_processes(pages):
    """end_pages's look at /proc, its process_ended and its kill, on `pages` instead."""
    with mock.patch("natter.app.web_processes", lambda **_: pages.find()), \
            mock.patch("natter.app.process_ended", pages.ended), \
            mock.patch("natter.app.kill_web_process", lambda pid, **_: pages.kill(pid)):
        yield


class QuitNatterTest(unittest.TestCase):
    def test_the_guard_first_the_pages_last_then_quit(self):
        events = []
        pages = Pages(21, 22)
        app = quitting_app(events, [account("page", events)], anchor=Log("anchor", events))
        with web_processes(pages):
            Natter.quit_natter(app)
            self.assertEqual(events[0], "guard.stop")
            self.assertEqual(events[-2:], ["page.end_page", "anchor.terminate_web_process"])
            self.assertLess(events.index("page.chats.save"), events.index("page.end_page"))
            self.assertLess(events.index("tray.close"), events.index("page.end_page"))
            # What a voice message paused plays again while the main loop still runs.
            self.assertLess(events.index("voice_pause.quit"), events.index("page.end_page"))
            self.assertNotIn("quit", events)  # their processes still run
            pages.running.clear()
            run_loop(lambda: "quit" in events)
        self.assertEqual(events[-1], "quit")
        self.assertTrue(app._quitting)
        self.assertEqual(pages.killed, [])

    def test_asked_again_meanwhile_it_is_under_way(self):
        events = []
        pages = Pages(21)
        app = quitting_app(events, [account("page", events)])
        with web_processes(pages):
            Natter.quit_natter(app)
            before = list(events)
            Natter.quit_natter(app)  # Ctrl+Q again, or SIGTERM
            self.assertEqual(events, before)
            pages.running.clear()
            run_loop(lambda: "quit" in events)
        self.assertEqual(events.count("quit"), 1)
        self.assertEqual(events.count("page.end_page"), 1)

    def test_a_step_that_fails_still_ends_the_pages_and_quits(self):
        events = []
        app = quitting_app(events, [account("page", events)], anchor=Log("anchor", events))
        app.tray.close = broken
        with web_processes(Pages()), self.assertRaises(RuntimeError):
            Natter.quit_natter(app)
        self.assertEqual(events[-3:], ["page.end_page", "anchor.terminate_web_process", "quit"])

    def test_ending_the_pages_that_fails_still_quits(self):
        events = []
        app = quitting_app(events, [account("page", events)])
        with mock.patch("natter.app.end_pages", broken), contextlib.redirect_stderr(io.StringIO()) as err:
            Natter.quit_natter(app)
        self.assertEqual(events[-1], "quit")
        self.assertIn("broken on purpose", err.getvalue())

    def test_without_pages_it_quits_at_once(self):
        events = []
        Natter.quit_natter(quitting_app(events, guard=False))
        self.assertEqual(events[-1], "quit")

    def test_the_name_watch_stops_before_anything(self):
        # The name this quit gives up isn't a lost one (instance.NameWatchdog).
        events = []
        app = quitting_app(events, [account("page", events)])
        app.name_watch = Log("name_watch", events)
        with web_processes(Pages()):
            Natter.quit_natter(app)
        self.assertEqual(events[:2], ["name_watch.stop", "guard.stop"])

    def test_sigterm_quits_the_same_way(self):
        app = SimpleNamespace(quit_natter=mock.Mock())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(Natter._on_terminate(app), GLib.SOURCE_REMOVE)
        app.quit_natter.assert_called_once_with()
        self.assertIn("asked to stop (SIGTERM)", out.getvalue())


class ShutdownTest(unittest.TestCase):
    def shutdown(self, app):
        chained = []
        with mock.patch.object(Adw.Application, "do_shutdown", lambda self: chained.append(self)):
            Natter.do_shutdown(app)
        self.assertEqual(chained, [app])

    def test_ends_the_pages_when_quit_natter_did_not(self):
        # Ctrl+C in a terminal (PyGObject's own quit), a window that couldn't be made.
        events = []
        app = SimpleNamespace(accounts=[account("page", events)], _sandbox_anchor=Log("anchor", events))
        with web_processes(Pages()):
            self.shutdown(app)
        self.assertEqual(events, ["page.end_page", "anchor.terminate_web_process"])
        self.assertTrue(app._quitting)

    def test_not_again_after_quit_natter(self):
        events = []
        app = quitting_app(events, [account("page", events)], anchor=Log("anchor", events))
        with web_processes(Pages()):
            Natter.quit_natter(app)
            del events[:]
            self.shutdown(app)
        self.assertEqual(events, [])

    def test_the_name_watch_stops_too(self):
        events = []
        app = SimpleNamespace(accounts=[], name_watch=Log("name_watch", events))
        self.shutdown(app)
        self.assertEqual(events, ["name_watch.stop"])

    def test_chains_up_whatever_happens(self):
        app = SimpleNamespace(accounts=[account("page", [])])
        with mock.patch("natter.app.end_pages", broken), self.assertRaises(RuntimeError):
            self.shutdown(app)


class EndPageTest(unittest.TestCase):
    def page(self, events, rtc=True):
        fake = SimpleNamespace(removed=False, ready=True, _reload_timer=GLib.timeout_add_seconds(60, lambda: False),
                               rtc=Log("rtc", events) if rtc else None, view=Log("view", events))
        fake._watch_page = lambda seconds, then: events.append(("watch_page", seconds, then))
        fake._end_link = lambda: events.append("end_link")
        return fake

    def test_calls_close_then_the_process_ends_and_nothing_loads_it_again(self):
        events = []
        fake = self.page(events)
        timer = fake._reload_timer
        Account.end_page(fake)
        self.assertEqual(events, [("watch_page", 0, None), "end_link", "rtc.close", "view.terminate_web_process"])
        self.assertTrue(fake.removed)
        self.assertFalse(fake.ready)
        self.assertEqual(fake._reload_timer, 0)
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(timer))

    def test_without_calls_in_the_page(self):
        events = []
        Account.end_page(self.page(events, rtc=False))
        self.assertEqual(events[-1], "view.terminate_web_process")

    def test_the_process_ends_whatever_fails_before_it(self):
        events = []
        fake = self.page(events)
        fake.rtc.close = broken
        with self.assertRaises(RuntimeError):
            Account.end_page(fake)
        self.assertEqual(events[-1], "view.terminate_web_process")
        self.assertTrue(fake.removed)
        self.assertFalse(fake.ready)

    def test_nothing_runs_in_the_page_after(self):
        # Account.run (the bridge's calls, the audio check) does nothing once the account is removed.
        fake = self.page([])
        Account.end_page(fake)
        answers = []
        fake._trusted = lambda: self.fail("not even looked at")
        Account.run(fake, "return 1", None, answers.append)
        run_loop(lambda: answers, seconds=1)
        self.assertEqual(answers, [None])


class GuardStopTest(unittest.TestCase):
    def guard(self, look):
        guard = memguard.MemoryGuard(memguard.GiB, lambda pid, size: None, poll=0.01, ceiling=memguard.GiB,
                                     proc=Path("/nonexistent"), pid=1, flatpak=False, killer=lambda pid: True,
                                     idle_add=lambda fn, *args: None)
        guard.check = look
        return guard

    def test_back_once_the_thread_has_stopped(self):
        looking = threading.Event()
        guard = self.guard(lambda: (looking.set(), time.sleep(0.1)))
        guard.start()
        self.assertTrue(looking.wait(2))
        guard.stop()  # partway through a look: back once it's done
        self.assertFalse(guard._thread.is_alive())

    def test_a_look_that_hangs_holds_it_up_for_wait_at_most(self):
        release = threading.Event()
        looking = threading.Event()
        guard = self.guard(lambda: (looking.set(), release.wait(5)))
        guard.start()
        self.addCleanup(release.set)
        self.assertTrue(looking.wait(2))
        started = time.monotonic()
        guard.stop(wait=0.05)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(guard._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
