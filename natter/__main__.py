"""Natter: WhatsApp for Linux that feels like part of the desktop.

  natter                      open Natter (or bring it forward)
  natter --background         start hidden, for login: notifications and the tray still work
  natter --chat NAME          open a chat by name
  natter https://wa.me/123…   open a wa.me or whatsapp:// link
  natter FILE…                send files: pick the chat, WhatsApp shows its preview
"""

from __future__ import annotations

import sys

from . import APP_ID, DEFAULT_APP_ID

# The oldest versions Natter runs with. Ubuntu 24.04, Debian 13 and Fedora 40 have them.
FLOORS = {"PyGObject": (3, 42), "GTK": (4, 14), "libadwaita": (1, 5), "WebKitGTK": (2, 42)}
# Python reaches each library through its GObject introspection data, packaged separately.
LIBRARIES = (("Gtk", "4.0", "GTK 4"), ("Adw", "1", "libadwaita"), ("WebKit", "6.0", "WebKitGTK 6.0"),
             ("Soup", "3.0", "libsoup 3"), ("GdkPixbuf", "2.0", "GdkPixbuf"))


def dotted(version) -> str:
    return ".".join(str(part) for part in version)


def too_old(found: dict[str, tuple[int, ...]]) -> list[str]:
    """Which of `found` (name → version) are older than Natter needs, in words."""
    return [f"{name} is {dotted(version)}; Natter needs {dotted(FLOORS[name])} or newer"
            for name, version in found.items() if tuple(version[:len(FLOORS[name])]) < FLOORS[name]]


def problems() -> list[str]:
    """What stops Natter from running here, in words; empty when nothing does."""
    try:
        import gi
    except ImportError:
        return ["PyGObject (GTK for Python) isn't installed"]
    old = too_old({"PyGObject": tuple(gi.version_info)})
    missing = []
    for namespace, version, name in LIBRARIES:
        try:
            gi.require_version(namespace, version)
        except ValueError:
            missing.append(f"{name} for Python isn't installed")
    if missing:
        return old + missing
    from gi.repository import Adw, Gtk, WebKit

    return old + too_old({
        "GTK": (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()),
        "libadwaita": (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()),
        "WebKitGTK": (WebKit.get_major_version(), WebKit.get_minor_version(), WebKit.get_micro_version()),
    })


def explain(trouble: list[str]) -> None:
    from .distro import DESKTOP, image_based, install_hint, os_release

    release = os_release()
    lines = ["Natter can't start:"] + [f"  - {line}" for line in trouble]
    hint = install_hint(DESKTOP, release)
    flatpak = f"the Natter Flatpak ({DEFAULT_APP_ID})"
    if image_based(release):
        lines.append(f"This system installs apps as Flatpaks: install {flatpak}, which brings everything it needs.")
        if hint:
            lines += ["Or layer the packages onto the system (they take effect after a restart):", f"  {hint}"]
    elif hint:
        lines += ["Install or update them with:", f"  {hint}",
                  f"If your distribution's versions are too old, {flatpak} brings its own."]
    else:
        lines.append(f"Install them from your distribution's packages, or install {flatpak}, which brings its own.")
    print("\n".join(lines), file=sys.stderr)


def check() -> int:
    """The start-up check, which install.sh also runs to pick a Python: 0 when Natter can run
    here, otherwise 1 after saying what's missing and how to get it."""
    trouble = problems()
    if trouble:
        explain(trouble)
    return 1 if trouble else 0


def main() -> int:
    try:
        from gi.repository import GLib

        # Before GTK starts: on X11 the window's WM_CLASS comes from it, and the desktop
        # matches that to StartupWMClass in Natter's desktop entry.
        GLib.set_prgname(APP_ID)
    except ImportError:
        pass
    if check():
        return 1
    from gi.repository import Gio

    asking = any(arg in ("-h", "--help", "-v", "--version") or arg.startswith("--help-") for arg in sys.argv[1:])
    if not asking:
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error as error:
            print(f"Natter can't start: there's no D-Bus session bus ({error.message}).\n"
                  "It runs inside a desktop session, which is where notifications, the tray and links reach it.",
                  file=sys.stderr)
            return 1
    from .app import main as run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
