"""The lock's cover and the page running under it (lock.WarmUp, window.py): the page goes on
starting behind the passcode, out of sight and out of reach, for a bounded time, never with a chat
open; and the lock's own privacy (keys, toasts, closing). None of it needs a display: widgets are
stood in for by fakes, as in test_polish_window.py."""

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, Gtk, WebKit

from natter import lock, window
from natter.lock import WARM_AFTER_READY, WARM_LIMIT, WARM_SETTLE, WarmUp
from natter.window import LOCKED_ACTIONS, LOCKED_KEYS, AccountPage, Window


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def warm():
    clock = Clock()
    return WarmUp(clock), clock


ON = dict(locked=True, on_screen=True)


class WarmUpTest(unittest.TestCase):
    def test_runs_from_the_load_until_its_chats_are_listed_and_settled(self):
        w, clock = warm()
        self.assertTrue(w.want(ready=False, **ON))  # loading, behind the passcode
        clock.now += 8
        self.assertTrue(w.want(ready=True, **ON))  # the chat list is up, its chats not yet listed
        self.assertEqual(w.due(), WARM_AFTER_READY)
        clock.now += 2
        w.chats_listed()
        self.assertTrue(w.want(ready=True, **ON))
        self.assertEqual(w.due(), WARM_SETTLE)  # a moment to draw them
        clock.now += WARM_SETTLE
        self.assertFalse(w.want(ready=True, **ON))
        self.assertTrue(w.drawn)
        self.assertIsNone(w.due())
        clock.now += 60
        self.assertFalse(w.want(ready=True, **ON))  # drawn: out of sight until the next load

    def test_chats_listed_before_it_ran_still_get_their_moment(self):
        # Started hidden at login: it loaded out of sight, and runs once the window shows the lock.
        w, clock = warm()
        w.chats_listed()
        self.assertFalse(w.want(locked=True, on_screen=False, ready=True))
        clock.now += 600
        self.assertTrue(w.want(ready=True, **ON))
        self.assertEqual(w.due(), WARM_SETTLE)

    def test_list_up_without_chats_stops_after_its_limit(self):
        w, clock = warm()
        self.assertTrue(w.want(ready=True, **ON))
        clock.now += WARM_AFTER_READY - 0.5
        self.assertTrue(w.want(ready=True, **ON))
        clock.now += 0.5
        self.assertFalse(w.want(ready=True, **ON))
        self.assertTrue(w.drawn)

    def test_never_up_stops_at_the_overall_limit(self):
        w, clock = warm()
        self.assertTrue(w.want(ready=False, **ON))
        clock.now += WARM_LIMIT - 1
        self.assertTrue(w.want(ready=False, **ON))
        self.assertAlmostEqual(w.due(), 1)
        clock.now += 1
        self.assertFalse(w.want(ready=False, **ON))
        self.assertTrue(w.drawn)

    def test_only_the_time_it_runs_counts(self):
        w, clock = warm()
        self.assertTrue(w.want(ready=False, **ON))
        clock.now += WARM_LIMIT - 10
        self.assertFalse(w.want(locked=True, on_screen=False, ready=False))  # the window closed
        clock.now += 3600
        self.assertIsNone(w.due())
        self.assertTrue(w.want(ready=False, **ON))  # shown again: 10 s left
        self.assertAlmostEqual(w.due(), 10)
        clock.now += 10
        self.assertFalse(w.want(ready=False, **ON))

    def test_the_link_screen_settles_like_a_chat_list(self):
        w, clock = warm()
        self.assertTrue(w.want(ready=False, linking=True, **ON))
        self.assertEqual(w.due(), WARM_SETTLE)
        clock.now += WARM_SETTLE
        self.assertFalse(w.want(ready=False, linking=True, **ON))

    def test_never_with_a_chat_open(self):
        w, clock = warm()
        self.assertTrue(w.want(ready=True, **ON))
        self.assertFalse(w.want(ready=True, chat_open=True, **ON))
        self.assertIsNone(w.due())  # not running: no timer either

    def test_a_load_that_began_with_a_chat_open_never_runs(self):
        # Natter may open that chat again in the new page (Account._reopen_after_load).
        w, clock = warm()
        w.loaded(chat_open=True)
        for ready in (False, True):
            self.assertFalse(w.want(ready=ready, **ON))
        w.loaded(chat_open=False)  # the next load, with no chat open
        self.assertTrue(w.want(ready=False, **ON))

    def test_not_when_unlocked_or_hidden_or_stopped(self):
        w, clock = warm()
        self.assertFalse(w.want(locked=False, on_screen=True, ready=False))
        self.assertFalse(w.want(locked=True, on_screen=False, ready=False))
        self.assertFalse(w.want(ready=False, stopped=True, **ON))
        self.assertTrue(w.want(ready=False, **ON))

    def test_drawn_on_screen_while_unlocked(self):
        w, clock = warm()
        self.assertFalse(w.want(locked=False, on_screen=True, ready=True))
        self.assertTrue(w.drawn)
        self.assertFalse(w.want(ready=True, **ON))  # locked later: hidden at once, as it's drawn

    def test_not_drawn_by_an_unlocked_window_that_isnt_showing_it(self):
        w, clock = warm()
        w.want(locked=False, on_screen=False, ready=True)
        self.assertFalse(w.drawn)

    def test_a_new_load_runs_again(self):
        w, clock = warm()
        w.want(locked=False, on_screen=True, ready=True)
        w.chats_listed()
        w.loaded(chat_open=False)
        self.assertFalse(w.drawn)
        self.assertFalse(w.listed)
        self.assertTrue(w.want(ready=False, **ON))
        self.assertEqual(w.ran(), 0)

    def test_limits(self):
        # The design's: drawn soon after its chats are listed, 15 s after the list is up at the
        # latest, two minutes in all.
        self.assertEqual((WARM_SETTLE, WARM_AFTER_READY, WARM_LIMIT), (3, 15, 120))
        self.assertLess(WARM_SETTLE, WARM_AFTER_READY)
        self.assertLess(WARM_AFTER_READY, WARM_LIMIT)


class Fake:
    """Records the methods called on it, by name, and answers each with another Fake."""

    def __init__(self, **values):
        self.calls = []
        self.__dict__.update(values)

    def __getattr__(self, name):
        def method(*args):
            self.calls.append((name, *args))
            return Fake()
        return method

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Widget(Fake):
    """A widget's visibility, sensitivity, targeting, opacity and accessible states."""

    def __init__(self, **values):
        super().__init__(**{**dict(visible=True, sensitive=True, target=True, opacity=1.0, states={}), **values})

    def get_visible(self):
        return self.visible

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive

    def set_can_target(self, target):
        self.target = target

    def set_opacity(self, opacity):
        self.opacity = opacity

    def set_visible(self, visible):
        if visible != self.visible:
            self.states[Gtk.AccessibleState.HIDDEN] = not visible  # as GTK does when it shows or hides one
        self.visible = visible

    def update_state(self, states, values):
        self.states.update(zip(states, values))


class Action:
    def __init__(self):
        self.enabled = True

    def set_enabled(self, enabled):
        self.enabled = enabled


def account(**values):
    defaults = dict(ready=False, needs_link=False, active_chat="", crashing=False, failed=None, removed=False)
    return SimpleNamespace(**{**defaults, **values})


def fake_window(locked=True, visible=True, active=True, **state):
    """A Window with one account's page, run through Window's own lock methods."""
    clock = Clock()
    page = SimpleNamespace(account=account(**state), warm=WarmUp(clock))
    focus = []
    app = SimpleNamespace(locked=locked, on_focus_changed=focus.append)
    actions = {name: Action() for name in LOCKED_ACTIONS}
    fake = Fake(app=app, content=Widget(), cover=Widget(visible=False), _fade=None, _warm_timer=0,
                _running_behind=False, pages={"a": SimpleNamespace(get_child=lambda: page)},
                stack=SimpleNamespace(get_visible_child=lambda: page))
    fake.get_visible = lambda: visible
    fake.is_active = lambda: active
    fake.lookup_action = actions.__getitem__
    fake._behind_lock = lambda: Window._behind_lock(fake)
    fake._warm_due = lambda: Window._warm_due(fake)
    return fake, page, clock, actions, focus


class CoverTest(unittest.TestCase):
    def setUp(self):
        self.timers = []
        patcher = mock.patch.object(window.GLib, "timeout_add",
                                    lambda ms, run: self.timers.append((ms, run)) or len(self.timers))
        patcher.start()
        self.addCleanup(patcher.stop)
        remove = mock.patch.object(window.GLib, "source_remove", lambda _id: True)
        remove.start()
        self.addCleanup(remove.stop)

    def test_locked_the_page_is_out_of_reach_and_its_keys_do_nothing(self):
        fake, page, clock, actions, focus = fake_window(locked=True)
        page.warm.drawn = True  # an ordinary lock: the page was on screen
        Window._cover(fake, True)
        content, cover = fake.content, fake.cover
        self.assertEqual((content.sensitive, content.target), (False, False))
        self.assertEqual(content.states, {Gtk.AccessibleState.HIDDEN: True})
        self.assertFalse(content.visible)  # drawn already: out of sight to WebKit too
        self.assertEqual((cover.visible, cover.opacity, cover.target), (True, 1, True))  # at once, no fade
        self.assertFalse(any(action.enabled for action in actions.values()))
        self.assertEqual(focus, [False])  # active, but not in use: notifications stay, tones follow DND

    def test_a_page_not_yet_drawn_runs_under_the_cover(self):
        fake, page, clock, actions, focus = fake_window(locked=True)
        Window._cover(fake, True)
        self.assertTrue(fake.content.visible)  # mapped, so WebKit runs it as shown...
        self.assertEqual((fake.content.sensitive, fake.content.target), (False, False))  # ...out of reach
        self.assertEqual(fake.content.states, {Gtk.AccessibleState.HIDDEN: True})
        self.assertTrue(fake.cover.visible)
        self.assertEqual(self.timers[-1][0], WARM_LIMIT * 1000 + 50)

    def test_hidden_again_once_drawn(self):
        fake, page, clock, actions, focus = fake_window(locked=True)
        Window._cover(fake, True)
        page.account.ready = True
        fake._behind_lock()
        page.warm.chats_listed()
        fake._behind_lock()
        self.assertTrue(fake.content.visible)
        ms, run = self.timers[-1]
        self.assertEqual(ms, WARM_SETTLE * 1000 + 50)
        clock.now += ms / 1000
        run()
        self.assertFalse(fake.content.visible)
        self.assertTrue(page.warm.drawn)

    def test_shown_to_webkit_still_hidden_from_screen_readers(self):
        fake, page, clock, actions, focus = fake_window(locked=True)
        page.warm.drawn = True
        Window._cover(fake, True)
        self.assertFalse(fake.content.visible)
        page.warm.loaded(chat_open=False)  # a new page: it runs under the cover again
        fake._behind_lock()
        self.assertTrue(fake.content.visible)
        self.assertEqual(fake.content.states, {Gtk.AccessibleState.HIDDEN: True})

    def test_a_chat_opening_hides_it_at_once(self):
        fake, page, clock, actions, focus = fake_window(locked=True)
        Window._cover(fake, True)
        self.assertTrue(fake.content.visible)
        page.account.active_chat = "Mum"
        fake._behind_lock()
        self.assertFalse(fake.content.visible)

    def test_not_while_the_window_is_hidden(self):
        fake, page, clock, actions, focus = fake_window(locked=True, visible=False)
        Window._cover(fake, True)
        self.assertFalse(fake.content.visible)

    def test_unlocked_the_page_comes_back_and_the_lock_fades(self):
        fake, page, clock, actions, focus = fake_window(locked=False, ready=True)
        fake.content.set_visible(False)
        fake.content.set_sensitive(False)
        fake.cover.set_visible(True)
        fades = []

        class Animation(Fake):
            def play(self):
                fades.append(self)

        with mock.patch.object(window.Adw, "TimedAnimation", SimpleNamespace(new=lambda *a: Animation(args=a))), \
                mock.patch.object(window.Adw, "PropertyAnimationTarget", SimpleNamespace(new=lambda *a: a)):
            Window._cover(fake, False)
        content = fake.content
        self.assertEqual((content.visible, content.sensitive, content.target), (True, True, True))
        self.assertEqual(content.states, {Gtk.AccessibleState.HIDDEN: False})
        self.assertTrue(all(action.enabled for action in actions.values()))
        self.assertFalse(fake.cover.target)  # clicks reach the page while the lock fades
        self.assertEqual(len(fades), 1)
        _widget, start, end, ms, target = fades[0].args
        self.assertEqual((start, end, ms, target[1]), (1, 0, window.UNLOCK_FADE, "opacity"))
        self.assertEqual(focus, [True])
        self.assertTrue(page.warm.drawn)  # on screen now
        Window._faded(fake, fades[0])
        self.assertEqual((fake.cover.visible, fake.cover.opacity), (False, 1))

    def test_locking_again_mid_fade_covers_at_once(self):
        fake, page, clock, actions, focus = fake_window(locked=True)
        fading = Fake()
        fake._fade = fading
        fake.cover.set_opacity(0.4)
        Window._cover(fake, True)
        self.assertEqual(fading.called("reset"), [()])
        self.assertIsNone(fake._fade)
        Window._faded(fake, fading)  # a late "done" from the old fade
        self.assertEqual((fake.cover.visible, fake.cover.opacity), (True, 1))

    def test_unlocked_leaves_the_page_alone(self):
        fake, page, clock, actions, focus = fake_window(locked=False)
        fake._behind_lock()
        self.assertTrue(fake.content.visible)
        self.assertEqual(self.timers, [])

    def test_show_lock_lays_the_cover(self):
        fake = Fake(passcode=Fake(), unlock_button=Fake(), lock_message=Fake(), _checking=False, account=None,
                    get_visible_dialog=lambda: None)
        Window.show_lock(fake, True)
        self.assertEqual(fake.called("_cover"), [(True,)])


class AccountPageTest(unittest.TestCase):
    def page(self, active_chat=""):
        page = AccountPage.__new__(AccountPage)  # never initialised: no widget is made
        page.account = account(active_chat=active_chat)
        page.warm = WarmUp(Clock())
        page.changes = []
        page.changed = lambda: page.changes.append(1)
        return page

    def message(self, text):
        return SimpleNamespace(to_json=lambda _indent: text)

    def test_a_new_page_starts_again(self):
        page = self.page()
        page.warm.drawn = True
        AccountPage._on_load_changed(page, None, WebKit.LoadEvent.STARTED)
        self.assertTrue(page.warm.drawn)
        AccountPage._on_load_changed(page, None, WebKit.LoadEvent.COMMITTED)
        self.assertFalse(page.warm.drawn)
        self.assertFalse(page.warm.skip)
        self.assertEqual(page.changes, [1])

    def test_a_new_page_after_a_chat_isnt_run(self):
        page = self.page(active_chat="Mum")
        AccountPage._on_load_changed(page, None, WebKit.LoadEvent.COMMITTED)
        self.assertTrue(page.warm.skip)

    def test_its_first_chats(self):
        page = self.page()
        for text in ('{"type": "ready", "ready": true}', '{"type": "chats", "rows": []}', "not json", "null",
                     '["chats"]'):
            AccountPage._on_page_message(page, None, self.message(text))
        self.assertFalse(page.warm.listed)
        self.assertEqual(page.changes, [])
        AccountPage._on_page_message(page, None, self.message('{"type": "chats", "rows": [["Mum", 0]]}'))
        self.assertTrue(page.warm.listed)
        AccountPage._on_page_message(page, None, self.message('{"type": "chats", "rows": [["Mum", 0]]}'))
        self.assertEqual(page.changes, [1])  # once a load


class LockKeysTest(unittest.TestCase):
    def test_the_lock_takes_the_keys_that_would_act_on_the_page(self):
        self.assertEqual(set(LOCKED_ACTIONS), {"go-to-chat", "reload", "zoom-in", "zoom-out", "zoom-reset",
                                               "account"})
        acts = {action for _accel, action, _target in window.window_keys()}
        for name in LOCKED_ACTIONS:
            self.assertIn(f"win.{name}", acts)
        for name in LOCKED_KEYS:
            self.assertIn(name, acts)
        # Still there at the lock: quitting, hiding, the list of keys.
        for kept in ("app.quit", "win.hide", "app.shortcuts"):
            self.assertNotIn(kept.split(".")[1], LOCKED_ACTIONS)
            self.assertNotIn(kept, LOCKED_KEYS)

    def test_app_keys_do_nothing_at_the_lock(self):
        made = []
        with mock.patch.object(window.Gtk, "CallbackAction", SimpleNamespace(new=lambda run: made.append(run) or run)), \
                mock.patch.object(window.Gtk, "NamedAction", SimpleNamespace(new=lambda name: name)), \
                mock.patch.object(window.Gtk, "Shortcut", lambda **kw: Fake(**kw)), \
                mock.patch.object(window.Gtk, "ShortcutTrigger", SimpleNamespace(parse_string=lambda s: s)), \
                mock.patch.object(window.Gtk, "ShortcutController", lambda **kw: Fake(**kw)), \
                mock.patch.object(window.Gio, "SimpleAction", SimpleNamespace(new=lambda *a: Fake())):
            activated = []
            fake = Fake(app=SimpleNamespace(locked=True),
                        activate_action=lambda name, args: activated.append(name) or True)
            Window._actions(fake)
        locked_ones = made[:len(LOCKED_KEYS)]
        for run in locked_ones:
            self.assertTrue(run(None, None))  # taken, so the app's own accelerator doesn't act
        self.assertEqual(activated, [])
        fake.app.locked = False
        for run in locked_ones:
            self.assertTrue(run(None, None))  # done here, not again by the app's accelerator
        self.assertEqual(sorted(activated), sorted(LOCKED_KEYS))
        # The name PyGObject gives gtk_widget_activate_action_variant: a stand-in can't tell.
        self.assertTrue(hasattr(window.Gtk.Widget, "activate_action"))
        self.assertIn("self.activate_action(action, None)", inspect.getsource(Window._actions))


class ToastTest(unittest.TestCase):
    def test_none_while_locked(self):
        fake = Fake(app=SimpleNamespace(locked=True), toasts=Fake(), get_visible_dialog=lambda: None)
        Window.toast(fake, "Zoom 110%")
        self.assertEqual(fake.toasts.called("add_toast"), [])


class CloseTest(unittest.TestCase):
    """With "lock when closed", the lock is drawn before the window hides: the desktop keeps the
    last frame of a closed window (for previews), which is then the lock."""

    def window(self, lock_on_hide=True, lock_enabled=True, locked=False, background=True):
        events = []
        settings = {"run_in_background": background, "lock_on_hide": lock_on_hide}
        app = SimpleNamespace(settings=settings, lock_enabled=lock_enabled, locked=locked,
                              settle_first_run=lambda: None, window_closed=lambda: events.append("closed"),
                              quit_natter=lambda: events.append("quit"), lock=lambda: events.append("lock"))
        clock = SimpleNamespace(handlers={}, connect=lambda signal, run: clock.handlers.setdefault(signal, run) and 7,
                                disconnect=lambda _id: clock.handlers.clear())
        fake = Fake(app=app, get_visible=lambda: True, remember_size=lambda: None,
                    set_visible=lambda visible: events.append(("visible", visible)),
                    get_frame_clock=lambda: clock)
        fake._hide_after_frame = lambda: Window._hide_after_frame(fake)
        return fake, events, clock

    def test_locked_then_hidden_after_the_next_frame(self):
        fake, events, clock = self.window()
        timers = []
        with mock.patch.object(window.GLib, "timeout_add", lambda ms, run: timers.append((ms, run)) or 9), \
                mock.patch.object(window.GLib, "source_remove", lambda _id: timers.clear()):
            self.assertTrue(Window._on_close(fake, None))
            self.assertEqual(events, ["lock", "closed"])  # not hidden yet: the lock is drawn first
            self.assertEqual(timers[0][0], window.HIDE_AFTER_LOCK)
            clock.handlers["after-paint"](clock)
        self.assertEqual(events, ["lock", "closed", ("visible", False)])
        self.assertEqual(timers, [])  # the fallback went with it
        self.assertEqual(clock.handlers, {})

    def test_hidden_anyway_if_no_frame_comes(self):
        fake, events, clock = self.window()
        timers = []
        with mock.patch.object(window.GLib, "timeout_add", lambda ms, run: timers.append((ms, run)) or 9), \
                mock.patch.object(window.GLib, "source_remove", lambda _id: None):
            Window._on_close(fake, None)
            timers[0][1]()
        self.assertEqual(events, ["lock", "closed", ("visible", False)])
        self.assertEqual(clock.handlers, {})  # and a late frame does nothing

    def test_otherwise_hidden_at_once(self):
        for kind in (dict(lock_on_hide=False), dict(lock_enabled=False), dict(locked=True)):
            fake, events, _clock = self.window(**kind)
            Window._on_close(fake, None)
            self.assertEqual(events, [("visible", False), "closed"], kind)

    def test_closing_quits_without_run_in_background(self):
        fake, events, _clock = self.window(background=False)
        Window._on_close(fake, None)
        self.assertEqual(events, ["quit"])


class CssTest(unittest.TestCase):
    def test_the_cover_is_opaque(self):
        rule = window.CSS[window.CSS.index(".natter-cover"):]
        rule = rule[:rule.index("}")]
        # A solid colour under the window's: a theme's colour with some transparency can't let the
        # page through.
        self.assertIn("background-color: black", rule)
        self.assertIn("background-image: image(@window_bg_color)", rule)


if __name__ == "__main__":
    unittest.main()
