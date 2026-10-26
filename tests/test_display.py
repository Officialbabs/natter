"""Natter says why when there's no display, rather than crashing once GTK fails to start."""

import unittest

from natter.__main__ import no_display


class NoDisplayTest(unittest.TestCase):
    def test_wayland_or_x11_is_a_display(self):
        self.assertFalse(no_display({"WAYLAND_DISPLAY": "wayland-0"}))
        self.assertFalse(no_display({"DISPLAY": ":0"}))

    def test_neither_is_no_display(self):
        self.assertTrue(no_display({}))
        self.assertTrue(no_display({"WAYLAND_DISPLAY": "", "DISPLAY": ""}))

    def test_broadway_brings_its_own(self):
        self.assertFalse(no_display({"GDK_BACKEND": "broadway"}))


if __name__ == "__main__":
    unittest.main()
