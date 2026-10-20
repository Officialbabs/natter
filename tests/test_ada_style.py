"""Following Ada's own Light, Dark or Dune setting (natter/ada.py)."""

import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import GLib

from natter.ada import AdaStyle, ada_dark, on_ada


class AdaDarkTest(unittest.TestCase):
    def test_themes(self):
        self.assertTrue(ada_dark("[Desktop]\nTheme=midnight\n"))
        self.assertTrue(ada_dark("[Desktop]\nTheme=Dune\n"))
        self.assertFalse(ada_dark("[Desktop]\nScale=100\nTheme=paper\n"))
        self.assertTrue(ada_dark("[Desktop]\nTheme=0\n"))
        self.assertFalse(ada_dark("[Desktop]\nTheme=1\n"))

    def test_unknown_or_broken_says_nothing(self):
        self.assertIsNone(ada_dark("[Desktop]\nTheme=neon\n"))
        self.assertIsNone(ada_dark("[Desktop]\nScale=100\n"))
        self.assertIsNone(ada_dark("not an ini file ["))
        self.assertIsNone(ada_dark(""))

    def test_on_ada(self):
        self.assertTrue(on_ada("ada"))
        self.assertTrue(on_ada("Ada:GNOME"))
        self.assertFalse(on_ada("ubuntu:GNOME"))
        self.assertFalse(on_ada(""))


class AdaStyleTest(unittest.TestCase):
    def spin(self, until, seconds=3.0):
        context, end = GLib.MainContext.default(), time.monotonic() + seconds
        while not until() and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.01)

    def test_follows_changes_to_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / "desktop.ini"
            ini.write_text("[Desktop]\nTheme=midnight\n")
            changes = []
            style = AdaStyle(lambda: changes.append(style.dark), path=ini)
            self.assertTrue(style.dark)
            # Replaced, as a settings program saving atomically would.
            partial = Path(folder) / "desktop.ini.new"
            partial.write_text("[Desktop]\nTheme=paper\n")
            partial.replace(ini)
            self.spin(lambda: changes)
            style.close()
        self.assertEqual(changes, [False])

    def test_missing_file(self):
        style = AdaStyle(lambda: None, path=Path("/nonexistent/os-ai/desktop.ini"))
        self.assertIsNone(style.dark)
        style.close()


if __name__ == "__main__":
    unittest.main()
