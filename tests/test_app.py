"""Tests for the app's own helpers: dock menu chat ids, WebKit memory settings, autostart, the
first run's defaults, debug actions and --url."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from gi.repository import GLib

from natter import DEFAULT_APP_ID, OLD_APP_ID
from natter.app import (MEMORY_LIMIT, MEMORY_THRESHOLDS, Natter, autostart_exec, debug_argument, debug_token,
                        dev_url, find_chat, launcher_path, memory_pressure, old_natter_running, snapshot_path)
from natter.launcher import chat_key
from natter.prefs import runs_autostart
from natter.settings import ChatIndex, Settings

LAB_ID = "io.github.Officialbabs.Natter.Lab"


class ChatIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.main = ChatIndex(folder / "main.json")
        self.main.add([("Mum", 0), ("Ana Souza", 1)], when=1000)
        self.work = ChatIndex(folder / "work.json")
        self.work.add([("Team Chat", 0), ("Mum", 1)], when=1000)
        self.indexes = [("main", self.main), ("work", self.work)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_to_the_right_account(self):
        self.assertEqual(find_chat(chat_key("main", "Ana Souza"), self.indexes), ("main", "Ana Souza"))
        self.assertEqual(find_chat(chat_key("work", "Mum"), self.indexes), ("work", "Mum"))
        self.assertEqual(find_chat(chat_key("main", "Mum"), self.indexes), ("main", "Mum"))

    def test_reads_what_is_on_disk(self):
        again = [("main", ChatIndex(self.main.path)), ("work", ChatIndex(self.work.path))]
        self.assertEqual(find_chat(chat_key("work", "Team Chat"), again), ("work", "Team Chat"))

    def test_unknown_keys(self):
        self.assertIsNone(find_chat(chat_key("main", "Team Chat"), self.indexes))  # other account's chat
        self.assertIsNone(find_chat(chat_key("gone", "Mum"), self.indexes))
        self.assertIsNone(find_chat("not-a-key", self.indexes))
        self.assertIsNone(find_chat(chat_key("main", "Mum"), []))


class MemoryTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(memory_pressure(None, None), (MEMORY_LIMIT, *MEMORY_THRESHOLDS))
        self.assertEqual(memory_pressure("", ""), (2048, 0.65, 0.85))

    def test_the_lines_sit_above_an_idle_page(self):
        # At 1536 MB and (0.55, 0.8) the strict line, 1229 MB, lay under an idle page's 1260 MB
        # resident, so WebKit's full release ran at every 30 s poll all day (24 Oct 2026).
        limit, conservative, strict = memory_pressure(None, None)
        self.assertEqual((limit, conservative, strict), (2048, 0.65, 0.85))
        self.assertGreater(limit * conservative, 1300)
        self.assertGreater(limit * strict, 1700)

    def test_overrides(self):
        self.assertEqual(memory_pressure("1536", None), (1536, 0.65, 0.85))
        self.assertEqual(memory_pressure(None, "0.4,0.7"), (2048, 0.4, 0.7))
        self.assertEqual(memory_pressure("1024", " 0.2 , 0.3 "), (1024, 0.2, 0.3))

    def test_zero_keeps_webkit_defaults(self):
        self.assertIsNone(memory_pressure("0", "0.4,0.7"))
        self.assertIsNone(memory_pressure("-5", None))

    def test_nonsense_falls_back(self):
        self.assertEqual(memory_pressure("lots", None), (2048, 0.65, 0.85))
        for thresholds in ("0.8,0.55", "0.5", "0.5,0.6,0.7", "0,0.5", "0.5,1", "a,b", "0.5,0.5"):
            self.assertEqual(memory_pressure(None, thresholds)[1:], MEMORY_THRESHOLDS, thresholds)


class AutostartTest(unittest.TestCase):
    def test_desktops(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "KDE", "XFCE", "X-Cinnamon", "Budgie:GNOME", "Ada", "COSMIC"):
            self.assertTrue(runs_autostart(desktop), desktop)
        for desktop in ("sway", "Hyprland", "i3", "niri", "river", "", "SomethingNew"):
            self.assertFalse(runs_autostart(desktop), desktop)

    def test_exec_line(self):
        self.assertEqual(autostart_exec("/opt/natter/bin/natter", DEFAULT_APP_ID), "/opt/natter/bin/natter --background")
        self.assertEqual(autostart_exec("/home/me/My Apps/natter", DEFAULT_APP_ID),
                         '"/home/me/My Apps/natter" --background')
        self.assertEqual(autostart_exec("/opt/natter/bin/natter", "io.natter.Natter.Lab"),
                         'env "NATTER_APP_ID=io.natter.Natter.Lab" /opt/natter/bin/natter --background')

    def test_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            natter = Path(tmp, "natter")
            natter.write_text("#!/bin/sh\n")
            natter.chmod(0o755)
            self.assertEqual(launcher_path(str(natter)), str(natter))  # the command that started Natter
            # Started some other way (python3 -m natter, a test): not that file.
            other = launcher_path(str(Path(tmp, "__main__.py")))
            self.assertNotEqual(other, str(Path(tmp, "__main__.py")))
            self.assertTrue(other.endswith("natter"), other)
            natter.chmod(0o644)
            self.assertNotEqual(launcher_path(str(natter)), str(natter))  # not runnable


class FakeBus:
    """Answers NameHasOwner from a set of names, and records what was asked."""

    def __init__(self, owned=(), fail=False):
        self.owned, self.fail, self.asked = set(owned), fail, []

    def call_sync(self, dest, path, iface, method, params, reply, flags, timeout, cancellable):
        self.asked.append((dest, method, params.unpack()[0]))
        if self.fail:
            raise GLib.Error("no bus")
        return GLib.Variant("(b)", (params.unpack()[0] in self.owned,))


class OldNatterTest(unittest.TestCase):
    def test_running(self):
        bus = FakeBus({OLD_APP_ID})
        self.assertTrue(old_natter_running(bus, DEFAULT_APP_ID))
        self.assertEqual(bus.asked, [("org.freedesktop.DBus", "NameHasOwner", OLD_APP_ID)])

    def test_not_running(self):
        self.assertFalse(old_natter_running(FakeBus(), DEFAULT_APP_ID))
        self.assertFalse(old_natter_running(FakeBus(fail=True), DEFAULT_APP_ID))
        self.assertFalse(old_natter_running(None, DEFAULT_APP_ID))

    def test_lab_instances_have_their_own_folders(self):
        bus = FakeBus({OLD_APP_ID})
        self.assertFalse(old_natter_running(bus, "io.natter.Natter.LabW1"))
        self.assertEqual(bus.asked, [])  # not even asked


class FakeApp:
    """What Natter.settle_first_run reads: the settings, whether they're new, and the tray host."""

    def __init__(self, folder, new_install, tray, values=None):
        path = Path(folder, "settings.json")
        if values is not None:
            path.write_text(json.dumps(values))  # what an earlier Natter saved
        self.settings = Settings(path)
        self._new_install = new_install
        self.tray = tray
        self.asked = 0

    def has_tray_host(self):
        self.asked += 1
        return self.tray

    def settle_first_run(self, hidden=False):
        Natter.settle_first_run(self, hidden)


class FirstRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, new_install, tray, values=None):
        return FakeApp(self.tmp.name, new_install, tray, values)

    def test_new_install_without_tray_quits_on_close(self):
        app = self.app(True, tray=False)
        app.settle_first_run()
        self.assertIs(app.settings.values["run_in_background"], False)
        self.assertTrue(app.settings["first_run_done"])

    def test_new_install_with_tray_keeps_running(self):
        app = self.app(True, tray=True)
        app.settle_first_run()
        self.assertNotIn("run_in_background", app.settings.values)
        self.assertTrue(app.settings["run_in_background"])
        self.assertTrue(app.settings["first_run_done"])

    def test_upgrade_keeps_its_settings(self):
        # A settings file from before first_run_done, on a desktop without tray icons.
        app = self.app(False, tray=False, values={"zoom": 1.2})
        app.settle_first_run()
        self.assertTrue(app.settings["run_in_background"])
        self.assertNotIn("run_in_background", app.settings.values)
        self.assertTrue(app.settings["first_run_done"])
        self.assertEqual(app.asked, 0)  # didn't even ask about the tray

    def test_upgrade_started_hidden(self):
        app = self.app(False, tray=False, values={})
        app.settle_first_run(hidden=True)
        self.assertTrue(app.settings["first_run_done"])
        self.assertTrue(app.settings["run_in_background"])

    def test_hidden_start_waits_for_the_first_close(self):
        app = self.app(True, tray=False)  # at login the panel isn't up yet
        app.settle_first_run(hidden=True)
        self.assertEqual(app.asked, 0)
        self.assertFalse(app.settings["first_run_done"])
        self.assertTrue(app.settings["run_in_background"])  # stays running hidden, as --background asks
        app.tray = True  # the panel came up; the tray icon heard of it
        app.settle_first_run()  # the window closes
        self.assertTrue(app.settings["run_in_background"])
        self.assertTrue(app.settings["first_run_done"])

    def test_hidden_start_without_a_panel(self):
        app = self.app(True, tray=False)
        app.settle_first_run(hidden=True)
        app.settle_first_run()
        self.assertFalse(app.settings["run_in_background"])
        self.assertTrue(app.settings["first_run_done"])

    def test_own_choice_wins(self):
        app = self.app(True, tray=False)
        app.settle_first_run(hidden=True)
        app.settings["run_in_background"] = False  # switched off and on in Preferences before closing
        app.settings["run_in_background"] = True
        app.settle_first_run()
        self.assertTrue(app.settings["run_in_background"])
        self.assertTrue(app.settings["first_run_done"])

    def test_only_once(self):
        app = self.app(True, tray=True)
        app.settle_first_run()
        app.tray = False
        app.settle_first_run()
        self.assertTrue(app.settings["run_in_background"])
        self.assertEqual(app.asked, 1)


class DebugTest(unittest.TestCase):
    def test_token(self):
        token = debug_token(DEFAULT_APP_ID)
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertNotEqual(token, debug_token(DEFAULT_APP_ID))
        self.assertEqual(debug_token(LAB_ID), "")  # a lab instance needs none

    def test_argument_needs_the_token(self):
        token = debug_token(DEFAULT_APP_ID)
        self.assertEqual(debug_argument(f"{token}:return 1", token), "return 1")
        self.assertEqual(debug_argument(f"{token}:", token), "")
        self.assertEqual(debug_argument(f"{token}:a:b", token), "a:b")
        for wrong in ("return 1", f"{token}return 1", f"{token[:-1]}:x", f":{token}:x", "", f"{token.upper()}:x",
                      "é" * 40):
            self.assertIsNone(debug_argument(wrong, token), wrong)

    def test_lab_instances_take_arguments_as_they_are(self):
        self.assertEqual(debug_argument("return 1", ""), "return 1")
        self.assertEqual(debug_argument("/tmp/x.png", ""), "/tmp/x.png")

    def test_snapshots_stay_in_their_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "cache", "snapshots")
            inside = folder.resolve()
            self.assertEqual(snapshot_path("natter.png", folder), inside / "natter.png")
            self.assertEqual(snapshot_path(str(folder / "a" / "b.png"), folder), inside / "a" / "b.png")
            for outside in ("/tmp/natter.png", "../natter.png", "a/../../natter.png", str(Path.home() / ".bashrc"),
                            "", ".", str(folder)):
                self.assertIsNone(snapshot_path(outside, folder), outside)
            folder.mkdir(parents=True)
            os.symlink(Path(tmp, "elsewhere.png"), folder / "link.png")
            self.assertIsNone(snapshot_path("link.png", folder))  # a link out of the folder


class DevUrlTest(unittest.TestCase):
    URL = "file:///tmp/mock-whatsapp.html"

    def test_lab_instance_on_its_first_command_line(self):
        self.assertEqual(dev_url(self.URL, False, LAB_ID), (self.URL, ""))

    def test_not_for_the_natter_you_use(self):
        for remote in (False, True):
            url, warning = dev_url(self.URL, remote, DEFAULT_APP_ID)
            self.assertIsNone(url)
            self.assertIn("--url ignored", warning)

    def test_not_from_a_later_launch(self):
        url, warning = dev_url(self.URL, True, LAB_ID)
        self.assertIsNone(url)
        self.assertIn("already running", warning)

    def test_no_url_no_warning(self):
        for app_id in (DEFAULT_APP_ID, LAB_ID):
            for remote in (False, True):
                self.assertEqual(dev_url(None, remote, app_id), (None, ""))
                self.assertEqual(dev_url("", remote, app_id), (None, ""))


if __name__ == "__main__":
    unittest.main()
