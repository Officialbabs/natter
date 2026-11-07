"""Warnings from the system's memory monitor (lowmem.py, wired in app.py): each page's memory
cache goes and nothing else of WhatsApp's, Natter's own caches go only at a critical warning,
each level is answered once in 30 s, and the log says what came back."""

import contextlib
import inspect
import io
import unittest
from types import SimpleNamespace

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, GObject, WebKit

from natter import NAME, lowmem
from natter.app import Natter
from natter.lowmem import AGAIN, LowMemory, level_name
from natter.memguard import MiB
from natter.notify import Notifier

Level = Gio.MemoryMonitorWarningLevel


class Manager:
    """A WebsiteDataManager: what it was asked to clear; answers at once unless `held`."""

    def __init__(self, fail=False, held=False):
        self.calls, self.fail, self.held, self.waiting = [], fail, held, []

    def clear(self, types, timespan, cancellable, callback):
        self.calls.append((types, timespan))
        if self.held:
            self.waiting.append(callback)
        else:
            callback(self, "result")

    def clear_finish(self, result):
        if self.fail:
            raise GLib.Error("the web process went away")
        return True

    def answer(self):
        self.waiting.pop(0)(self, "result")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Monitor(GObject.Object):
    """The system's memory monitor, as far as Natter listens to it."""

    __gsignals__ = {"low-memory-warning": (GObject.SignalFlags.RUN_LAST, None, (Level,))}


class LowMemoryTest(unittest.TestCase):
    def setUp(self):
        self.managers = [Manager(), Manager()]
        self.clock = Clock()
        self.page_sizes = [{11: 900 * MiB, 12: 600 * MiB}, {11: 850 * MiB, 12: 580 * MiB}]
        self.natter_sizes = [200 * MiB, 180 * MiB]
        self.own, self.trims = [], []
        self.low = LowMemory(lambda: self.managers, own=lambda: self.own.append(True) or "3 chat pictures",
                             sizes=lambda: self.page_sizes.pop(0) if self.page_sizes else {},
                             natter_held=lambda: self.natter_sizes.pop(0) if self.natter_sizes else None,
                             trim=lambda: self.trims.append(True), clock=self.clock)
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_a_low_warning_clears_each_pages_memory_cache_and_nothing_else(self):
        self.assertTrue(self.low.warned(Level.LOW))
        for manager in self.managers:
            self.assertEqual(manager.calls, [(WebKit.WebsiteDataTypes.MEMORY_CACHE, 0)])
        self.assertEqual(self.own, [])
        self.assertEqual(self.trims, [])

    def test_never_cookies_storage_workers_or_the_disk_cache(self):
        kept = (WebKit.WebsiteDataTypes.COOKIES | WebKit.WebsiteDataTypes.LOCAL_STORAGE
                | WebKit.WebsiteDataTypes.INDEXEDDB_DATABASES | WebKit.WebsiteDataTypes.SERVICE_WORKER_REGISTRATIONS
                | WebKit.WebsiteDataTypes.DISK_CACHE | WebKit.WebsiteDataTypes.DOM_CACHE
                | WebKit.WebsiteDataTypes.SESSION_STORAGE)
        for level in (Level.LOW, Level.MEDIUM, Level.CRITICAL):
            self.low.warned(level)
        for manager in self.managers:
            for types, _timespan in manager.calls:
                self.assertFalse(int(types) & int(kept))

    def test_the_log_says_what_came_back(self):
        self.low.warned(Level.MEDIUM)
        line = self.out.getvalue().strip()
        self.assertEqual(line, "natter: memory: the system's memory monitor warns (medium): cleared the memory "
                               "cache of 2 pages; they held 1500 → 1430 MB (70 MB back)")

    def test_unmeasured_it_says_the_level(self):
        self.page_sizes = []
        self.low.warned(Level.LOW)
        self.assertEqual(self.out.getvalue().strip(),
                         "natter: memory: the system's memory monitor warns (low): cleared the memory cache of 2 pages")

    def test_a_page_that_grew_meanwhile_gave_nothing_back(self):
        self.page_sizes = [{11: 900 * MiB}, {11: 950 * MiB}]
        self.managers = [Manager()]
        self.low.warned(Level.LOW)
        self.assertIn("1 page; they held 900 → 950 MB (0 MB back)", self.out.getvalue())

    def test_each_level_once_in_thirty_seconds(self):
        self.assertTrue(self.low.warned(Level.LOW))
        self.clock.now += AGAIN - 1
        self.assertFalse(self.low.warned(Level.LOW))
        self.assertEqual(len(self.managers[0].calls), 1)
        self.clock.now += 1
        self.assertTrue(self.low.warned(Level.LOW))
        self.assertEqual(len(self.managers[0].calls), 2)

    def test_a_worse_level_is_answered_at_once(self):
        self.low.warned(Level.LOW)
        self.clock.now += 5
        self.assertTrue(self.low.warned(Level.MEDIUM))
        self.assertTrue(self.low.warned(Level.CRITICAL))
        self.assertEqual(len(self.managers[0].calls), 3)
        self.assertFalse(self.low.warned(Level.CRITICAL))

    def test_critical_drops_natters_own_too(self):
        self.low.warned(Level.CRITICAL)
        self.assertEqual(self.own, [True])
        self.assertEqual(self.trims, [True])
        self.assertIn("natter: memory: the system's memory monitor warns (critical): dropped 3 chat pictures and gave "
                      f"Python's and malloc's free memory back; {NAME} held 200 → 180 MB (20 MB back)",
                      self.out.getvalue())
        self.clock.now += 10
        self.low.warned(Level.CRITICAL)
        self.assertEqual(self.own, [True])  # once in 30 s, like the clear

    def test_critical_with_nothing_of_its_own_to_drop(self):
        self.low.own = lambda: ""
        self.natter_sizes = []
        self.low.warned(Level.CRITICAL)
        self.assertIn("warns (critical): gave Python's and malloc's free memory back\n", self.out.getvalue())

    def test_a_clear_under_way_isnt_asked_for_again(self):
        held = Manager(held=True)
        self.managers = [held]
        self.low.warned(Level.LOW)
        self.clock.now += 5
        self.low.warned(Level.MEDIUM)  # answered, but the clear asked for at LOW is still out
        self.assertEqual(len(held.calls), 1)
        self.assertEqual(self.out.getvalue(), "")
        held.answer()
        self.assertIn("warns (low): cleared the memory cache of 1 page", self.out.getvalue())
        self.clock.now += 5
        self.low.warned(Level.CRITICAL)
        self.assertEqual(len(held.calls), 2)

    def test_a_page_that_never_answers_doesnt_stop_the_next(self):
        held = Manager(held=True)
        self.managers = [held]
        self.low.warned(Level.LOW)
        self.clock.now += AGAIN
        self.low.warned(Level.LOW)
        self.assertEqual(len(held.calls), 2)

    def test_one_line_once_every_page_has_answered(self):
        self.managers = [Manager(held=True), Manager(held=True)]
        self.low.warned(Level.LOW)
        self.managers[0].answer()
        self.assertEqual(self.out.getvalue(), "")
        self.managers[1].answer()
        self.assertEqual(self.out.getvalue().count("natter: memory:"), 1)

    def test_a_failed_clear_is_said_and_the_rest_goes_on(self):
        self.managers = [Manager(fail=True), Manager()]
        self.low.warned(Level.LOW)
        out = self.out.getvalue()
        self.assertIn("natter: memory: couldn't clear a page's memory cache: the web process went away", out)
        self.assertIn("cleared the memory cache of 2 pages", out)

    def test_no_page_yet(self):
        self.managers = []
        self.low.warned(Level.MEDIUM)
        self.assertEqual(self.out.getvalue().strip(),
                         "natter: memory: the system's memory monitor warns (medium); no page to clear yet")

    def test_it_listens_to_the_monitor_until_stopped(self):
        monitor = Monitor()
        self.low.start(monitor)
        monitor.emit("low-memory-warning", Level.LOW)
        self.assertEqual(len(self.managers[0].calls), 1)
        self.low.stop()
        self.clock.now += AGAIN
        monitor.emit("low-memory-warning", Level.LOW)
        self.assertEqual(len(self.managers[0].calls), 1)
        self.low.stop()  # again: nothing

    def test_level_names(self):
        self.assertEqual([level_name(level) for level in (50, 100, 255, 7)], ["low", "medium", "critical", "7"])

    def test_the_real_monitor_has_the_signal(self):
        monitor = Gio.MemoryMonitor.dup_default()
        self.assertTrue(GObject.signal_lookup("low-memory-warning", type(monitor)))

    def test_trim_runs_here(self):
        lowmem.trim()  # glibc's malloc_trim, or nothing where there's none


class WiringTest(unittest.TestCase):
    """Where app.py starts and stops it, and what it hands it."""

    def test_started_with_the_services_and_stopped_on_quitting(self):
        services = inspect.getsource(Natter._start_services)
        self.assertIn("LowMemory(self._page_data_managers, own=self._drop_own_caches)", services)
        self.assertLess(services.index("self.low_memory.start()"), services.index("then()"))
        self.assertIn("self.low_memory.stop()", inspect.getsource(Natter.quit_natter))

    def test_the_pages_still_natters(self):
        def account(removed):
            manager = object()
            session = SimpleNamespace(get_website_data_manager=lambda: manager)
            return SimpleNamespace(removed=removed, session=session), manager

        (kept, manager), (gone, _) = account(False), account(True)
        fake = SimpleNamespace(accounts=[kept, gone])
        self.assertEqual(Natter._page_data_managers(fake), [manager])

    def test_natters_own_caches_are_the_chats_pictures(self):
        fake = SimpleNamespace(notifier=SimpleNamespace(avatars={}))
        fake.notifier.forget_pictures = lambda: Notifier.forget_pictures(fake.notifier)
        fake.notifier.avatars.update({("a", "Mum"): ("1", None), ("a", "Work"): ("2", None)})
        self.assertEqual(Natter._drop_own_caches(fake), "2 chat pictures")
        self.assertEqual(fake.notifier.avatars, {})
        self.assertEqual(Natter._drop_own_caches(fake), "")


if __name__ == "__main__":
    unittest.main()
