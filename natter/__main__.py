"""Natter Den: an unofficial WhatsApp Web client for Linux.

  natter                      open Natter (or bring it forward)
  natter --background         start hidden, for login: notifications and the tray still work
  natter --chat NAME          open a chat by name
  natter https://wa.me/123…   open a wa.me or whatsapp:// link
  natter FILE…                send files: pick the chat, WhatsApp shows its preview
"""

from __future__ import annotations

import os
import sys

from . import APP_ID, DEFAULT_APP_ID, NAME

# The oldest versions Natter runs with. Ubuntu 24.04, Debian 13 and Fedora 40 have them.
FLOORS = {"PyGObject": (3, 42), "GTK": (4, 14), "libadwaita": (1, 5), "WebKitGTK": (2, 42)}
# Python reaches each library through its GObject introspection data, packaged separately.
LIBRARIES = (("Gtk", "4.0", "GTK 4"), ("Adw", "1", "libadwaita"), ("WebKit", "6.0", "WebKitGTK 6.0"),
             ("Soup", "3.0", "libsoup 3"), ("GdkPixbuf", "2.0", "GdkPixbuf"))


def dotted(version) -> str:
    return ".".join(str(part) for part in version)


def too_old(found: dict[str, tuple[int, ...]]) -> list[str]:
    """Which of `found` (name → version) are older than Natter needs, in words."""
    return [f"{name} is {dotted(version)}; {NAME} needs {dotted(FLOORS[name])} or newer"
            for name, version in found.items() if tuple(version[:len(FLOORS[name])]) < FLOORS[name]]


def problems() -> list[str]:
    """What stops Natter from running here, in words; empty when nothing does."""
    old, missing = _problems()
    return old + missing


def _problems() -> tuple[list[str], list[str]]:
    """problems(), as (versions too old, what isn't installed). NATTER_TEST_MISSING (test-only,
    see distro.py) can add to what isn't installed, never take anything away."""
    from .distro import pretend_missing

    pretend = pretend_missing()
    try:
        if "gi" in pretend:
            raise ImportError("NATTER_TEST_MISSING")
        import gi
    except ImportError:
        return [], ["PyGObject (GTK for Python) isn't installed"]
    old = too_old({"PyGObject": tuple(gi.version_info)})
    missing = []
    for namespace, version, name in LIBRARIES:
        try:
            gi.require_version(namespace, version)
            if namespace in pretend:
                raise ValueError("NATTER_TEST_MISSING")
        except ValueError:
            missing.append(f"{name} for Python isn't installed")
    if missing:
        return old, missing
    from gi.repository import Adw, Gtk, WebKit

    return old + too_old({
        "GTK": (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()),
        "libadwaita": (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()),
        "WebKitGTK": (WebKit.get_major_version(), WebKit.get_minor_version(), WebKit.get_micro_version()),
    }), []


# The GStreamer elements calls need, by the piece (distro.PIECES) whose packages bring them: what
# rtc.available() checks (tests/test_install_deps.py keeps it a part of this) and what webrtcbin
# makes inside itself, which it can't do without: SRTP's (dtlssrtpenc/dec make srtpenc/dec),
# DTLS's decoder, nicesink and rtpbin (GStreamer's good plugins, with the codecs).
CALL_ELEMENTS = {
    "webrtc": ("webrtcbin", "sctpenc", "sctpdec", "dtlsenc", "dtlsdec", "dtlssrtpenc", "dtlssrtpdec",
               "srtpenc", "srtpdec"),
    "nice": ("nicesrc", "nicesink"),
    "codecs": ("rtpbin",),
}
# The typelibs calls need, beyond the ones Natter runs without.
CALL_TYPELIBS = ("Gst", "GstWebRTC", "GstSdp")
# Other elements, by piece: the microphone and the camera's v4l2 fallback (good plugins), and
# the camera through the desktop's portal, which hands over a PipeWire stream.
DEVICE_ELEMENTS = {"codecs": ("pulsesrc", "v4l2src"), "pipewire": ("pipewiresrc",)}
# The introspection data for each piece that is one, as gi.require_version takes it.
TYPELIBS = {"Gtk": "4.0", "Adw": "1", "WebKit": "6.0", "Soup": "3.0", "GdkPixbuf": "2.0", "Gst": "1.0",
            "GstWebRTC": "1.0", "GstSdp": "1.0"}


def lacking(elements: dict[str, tuple[str, ...]]) -> set[str]:
    """The pieces among `elements` (piece → GStreamer elements) that GStreamer lacks one of here."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return {piece for piece, names in elements.items() if not all(Gst.ElementFactory.find(name) for name in names)}


def calls_missing() -> list[str]:
    """What calls in WhatsApp's page lack here, as piece names (distro.PIECES); empty when they
    have it all. Never raises: what can't be checked counts as missing."""
    try:
        import gi

        absent = []
        for namespace in CALL_TYPELIBS:
            try:
                gi.require_version(namespace, "1.0")
            except ValueError:
                absent.append(namespace)
        if "Gst" in absent:
            return absent + list(CALL_ELEMENTS)
        return absent + sorted(lacking(CALL_ELEMENTS))
    except Exception:
        return [*CALL_TYPELIBS, *CALL_ELEMENTS]


def missing_pieces(environ=None) -> set[str]:
    """The pieces (distro.PIECES) that aren't here: what install.sh offers to install. One that
    can't be checked (GStreamer's plugins, without GStreamer for Python) counts as missing."""
    import os
    import shlex
    import shutil
    import subprocess

    from .distro import pretend_missing

    environ = os.environ if environ is None else environ
    missing = pretend_missing(environ)
    try:
        if "gi" in missing:
            raise ImportError("NATTER_TEST_MISSING")
        import gi
    except ImportError:
        missing |= {"gi", *TYPELIBS}
    else:
        for namespace, version in TYPELIBS.items():
            try:
                gi.require_version(namespace, version)
            except ValueError:
                missing.add(namespace)
    unknown = {"codecs", *CALL_ELEMENTS, *DEVICE_ELEMENTS}
    if "Gst" in missing:
        missing |= unknown
    else:
        try:
            from .system import media_report

            missing |= lacking({piece: CALL_ELEMENTS.get(piece, ()) + DEVICE_ELEMENTS.get(piece, ())
                                for piece in unknown})
            if media_report()[0]:
                missing.add("codecs")
        except Exception:
            missing |= unknown
    # The camera fix's build, as install.sh's build_camfix does it: $CC (make's: "ccache gcc"…).
    try:
        compiler = shlex.split(environ.get("CC") or "cc")[0]
    except (ValueError, IndexError):
        compiler = "cc"
    if not shutil.which(compiler, path=environ.get("PATH")):
        missing.add("cc")
    pkg_config = shutil.which("pkg-config", path=environ.get("PATH"))
    if not pkg_config:
        missing |= {"pkg-config", "gst-dev"}
    else:
        try:
            headers = subprocess.run([pkg_config, "--exists", "gstreamer-1.0"], timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            headers = False
        if not headers:
            missing.add("gst-dev")
    return missing


def deps() -> int:
    """For install.sh: what's missing here, by group, and the packages that bring it on this
    distribution (distro.report says how it's written). The camera fix's build tools only where
    GStreamer is 1.28 or newer, where the fix is needed."""
    from .distro import PIECE, REQUIRED, os_release, report
    from .gstfix import FIRST_AFFECTED, gstreamer_version

    missing = missing_pieces()
    old = [] if any(PIECE[name].group == REQUIRED for name in missing if name in PIECE) else _problems()[0]
    version = gstreamer_version()
    stub = False
    if "codecs" in missing and "Gst" not in missing:
        # The stand-in note only when H.264 is what's missing: with another decoder for it, the
        # stand-in is beside the point.
        try:
            from .system import media_report, openh264_stub

            stub = any("H.264" in words for words in media_report()[0]) and openh264_stub()
        except Exception:
            pass
    print("\n".join(report(os_release(), missing, old, optional=version is not None and version >= FIRST_AFFECTED,
                           stub=stub)))
    return 0


def explain(trouble: list[str]) -> None:
    from .distro import DESKTOP, image_based, install_hint, os_release

    release = os_release()
    lines = [f"{NAME} can't start:"] + [f"  - {line}" for line in trouble]
    hint = install_hint(DESKTOP, release)
    flatpak = f"the {NAME} Flatpak ({DEFAULT_APP_ID})"
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


def no_display(environ=None) -> bool:
    """Whether there's no display for GTK to open: without one GTK's start-up fails and the app
    crashes later, instead of saying why. GDK_BACKEND=broadway (development) has a display of its own."""
    environ = os.environ if environ is None else environ
    if environ.get("GDK_BACKEND", "").startswith("broadway"):
        return False
    return not (environ.get("WAYLAND_DISPLAY") or environ.get("DISPLAY"))


def main() -> int:
    try:
        from gi.repository import GLib

        # Before GTK starts: on X11 the window's WM_CLASS comes from it, and the desktop
        # matches that to StartupWMClass in Natter's desktop entry.
        GLib.set_prgname(APP_ID)
    except ImportError:
        pass
    from .gstfix import apply as fix_camera, start_own as start_gstreamer

    # Before anything starts GStreamer, in Natter or WebKit: it reads these settings once.
    # The camera's first-frame freeze with GStreamer 1.28 and newer (gstfix.py; on by default
    # only where system.py keeps its settings from the programs Natter opens).
    fix_camera()
    # Natter's own GStreamer, without the fix's tracer: it's for WebKit's capture pipelines, and
    # a crash in Natter's process would close the whole app (gstfix.py).
    start_gstreamer()
    if check():
        return 1
    from .system import hide_system_bus

    hide_system_bus()  # before anything connects to it: WebKit's real-time threads (system.py)
    from gi.repository import Gio

    asking = any(arg in ("-h", "--help", "-v", "--version") or arg.startswith("--help-") for arg in sys.argv[1:])
    if not asking:
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error as error:
            print(f"{NAME} can't start: there's no D-Bus session bus ({error.message}).\n"
                  "It runs inside a desktop session, which is where notifications, the tray and links reach it.",
                  file=sys.stderr)
            return 1
        if no_display():
            print(f"{NAME} can't start: there's no display to open its window on (neither WAYLAND_DISPLAY "
                  "nor DISPLAY is set).\nIt runs inside a desktop session.", file=sys.stderr)
            return 1
    owner = None
    # D-Bus starts the service only while no Natter Den owns the name: nothing to look at then.
    if not asking and "--gapplication-service" not in sys.argv[1:]:
        from .instance import probe

        # One already running gets this launch's command line, and GLib waits on its answer for
        # good: a frozen one, or one whose main loop is stalled, is told apart first (instance.py).
        owner, go_on = probe()
        if not go_on:
            return 1
    from .app import main as run

    return run(sys.argv, owner)


if __name__ == "__main__":
    sys.exit(main())
