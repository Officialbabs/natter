"""Following Ada's Appearance setting, on Ada.

Ada's Settings → Appearance picks the apps' style: Light, Dark or Dune (a warm dark), kept in
~/.config/os-ai/desktop.ini ([Desktop] Theme=paper, midnight or dune). On Ada, following the
system means following Theme= there, and changing along with it.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib


def config_home() -> Path:
    """The user's own config folder. Inside Flatpak, XDG_CONFIG_HOME is the app's private one
    (~/.var/app/…); the host's is in HOST_XDG_CONFIG_HOME, or else at its usual place, where the
    manifest's xdg-config/os-ai:ro makes Ada's folder visible."""
    if Path("/.flatpak-info").exists():
        return Path(os.environ.get("HOST_XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


DESKTOP_INI = config_home() / "os-ai" / "desktop.ini"
# Theme= by name, or by the number Ada's Settings uses for it (0 Midnight, 1 Paper, 2 Dune).
DARK = {"midnight": True, "dune": True, "paper": False, "0": True, "2": True, "1": False}


def on_ada(desktop: Optional[str] = None) -> bool:
    """Whether the desktop is Ada (XDG_CURRENT_DESKTOP names it)."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") if desktop is None else desktop
    return any(name.strip().lower() == "ada" for name in desktop.split(":"))


def ada_dark(text: str) -> Optional[bool]:
    """Whether Ada's apps are dark, from desktop.ini's text; None when it doesn't say."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    theme = parser.get("Desktop", "Theme", fallback="").strip().lower()
    return DARK.get(theme)


class AdaStyle:
    """Ada's light or dark, kept up to date: `on_change()` runs when it changes."""

    def __init__(self, on_change: Callable[[], None], path: Path = DESKTOP_INI) -> None:
        self.path = path
        self.on_change = on_change
        self.dark = self._read()
        self._monitor: Optional[Gio.FileMonitor] = None
        try:
            # The file may be replaced rather than written into: the monitor watches the name.
            self._monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", self._changed)
        except GLib.Error:
            self._monitor = None

    def _read(self) -> Optional[bool]:
        try:
            return ada_dark(self.path.read_text())
        except (OSError, UnicodeDecodeError):
            return None

    def _changed(self, *_args) -> None:
        dark = self._read()
        if dark != self.dark:
            self.dark = dark
            self.on_change()

    def close(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
