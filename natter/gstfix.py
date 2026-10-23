"""Natter's fix for the camera freezing on its first frame (WebKitGTK with GStreamer 1.28 or newer).

WebKit's camera capture pipeline keeps buffers on absolute clock time while its "videorate" fills
the gap from 0 with copies of the first frame: the page shows one still frame, for longer the
longer the computer has been on. WebKit gave that videorate the settings that prevent it until it stopped doing so on
GStreamer 1.28 (WebKit bug 324582; the fix, PR 74373, isn't in any release yet). Natter puts them
back with a small GStreamer tracer, natter/camfix/nattercamfix.c, which install.sh (or meson)
builds when a C compiler and GStreamer's headers are there. The same tracer clears the gap marks
WebKit (before 2.54.2) puts on Web Audio's silent blocks, which make pulsesink click when a call's
sound starts again; it needs nothing more from here, so it's on only where the camera fix is
(GStreamer 1.28 and newer).

apply() turns the tracer on through GStreamer's environment: the plugin's folder first in
GST_PLUGIN_PATH_1_0 (or GST_PLUGIN_PATH), "nattercamfix" in GST_TRACERS, and a registry of
Natter's own in GST_REGISTRY, so the one other GStreamer programs share isn't rewritten each time
one of them starts with a different set of plugins. GStreamer reads these once, when it starts,
in each WebKit process (WebKit's sandbox passes them on and gives the web process the plugin's
and the registry's folders), so apply() runs first thing in __main__. It changes nothing in the
Flatpak or a Snap (their runtimes bring their own GStreamer), without the plugin, or with a
GStreamer older than 1.28, where WebKit still does it itself.

The capture pipelines it fixes are only ever in WebKit's web process. Natter's own process runs
GStreamer too (calls' webrtcbin, rtc.py), and there a crash takes the whole app with it, not one
page: so start_own() starts Natter's GStreamer with the tracer left out, right after apply(),
before anything else does. The settings stay in the environment for the processes WebKit starts.

The settings are for WebKit only, and everything Natter starts inherits its environment: a
browser, a media player, the call window. launch_env() and cleaned() take them off those, through
system.py's give_system_bus() and open_uri(). The fix is on by default only where they do
(ON_BY_DEFAULT, kept in step by tests/test_gstfix.py's WiringTest); otherwise NATTER_CAMFIX=1
turns it on. NATTER_CAMFIX=0 turns it off, to compare.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, MutableMapping, Optional

TRACER = "nattercamfix"
PLUGIN = "libnattercamfix.so"
FIRST_AFFECTED = (1, 28, 0)  # WebKit stops setting videorate up itself from this GStreamer on
SWITCH = "NATTER_CAMFIX"  # "0": leave GStreamer as it is; "1": turn the fix on where it's needed
# On without NATTER_CAMFIX=1: only where system.py keeps the settings from the programs Natter opens.
ON_BY_DEFAULT = True
PLUGIN_PATHS = ("GST_PLUGIN_PATH_1_0", "GST_PLUGIN_PATH")  # GStreamer reads the first one that's set
TRACERS = "GST_TRACERS"
REGISTRY = "GST_REGISTRY"
# meson.build records where it installed the plugin in this file, next to this module.
RECORDED_DIR = Path(__file__).with_name("camfix-dir")


@dataclass(frozen=True)
class Fix:
    """What apply() did, for the About dialog's troubleshooting text."""

    on: bool
    reason: str  # in words: why it's on or off
    plugin: Optional[str] = None
    gstreamer: Optional[str] = None  # the version found, "1.28.2"
    changed: dict = field(default_factory=dict)  # each variable apply() set → the value it got

    def line(self) -> str:
        return f"GStreamer camera fix: {'on' if self.on else 'off'}, {self.reason}"


STATUS: Optional[Fix] = None  # the last apply()'s


def debug_line() -> str:
    """The About dialog's line about the fix."""
    return STATUS.line() if STATUS is not None else "GStreamer camera fix: not checked"


def user_plugin_dir(environ) -> Path:
    """Where install.sh builds the plugin, for one user."""
    return Path(environ.get("HOME") or os.path.expanduser("~"), ".local", "lib", "natter", "gstreamer-1.0")


def plugin_dirs(environ) -> list[Path]:
    """The folders the plugin may be in, the one meson recorded first."""
    dirs = []
    try:
        recorded = RECORDED_DIR.read_text().strip()
    except (OSError, ValueError):
        recorded = ""
    if recorded:
        dirs.append(Path(recorded))
    return dirs + [user_plugin_dir(environ)]


def private_registry(environ) -> Path:
    """Natter's own GStreamer registry. In a folder of its own: WebKit lets its web process write to
    the registry's folder, and Natter's cache folder holds other things."""
    cache = environ.get("XDG_CACHE_HOME") or os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".cache")
    return Path(cache, "natter", "gstreamer-1.0", f"registry.{platform.machine() or 'unknown'}.bin")


def gstreamer_version() -> Optional[tuple[int, int, int]]:
    """GStreamer's version, read without starting GStreamer: gst_version() needs no gst_init(), and
    starting it here would settle its plugins and tracers before apply() has set them. Through
    ctypes, since PyGObject with gst-python's overrides refuses Gst.version() before Gst.init()."""
    try:
        library = ctypes.CDLL("libgstreamer-1.0.so.0")
        parts = [ctypes.c_uint() for _ in range(4)]
        library.gst_version(*(ctypes.byref(part) for part in parts))
        return (parts[0].value, parts[1].value, parts[2].value)
    except (OSError, AttributeError):
        pass
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        return tuple(Gst.version()[:3])
    except Exception:  # no typelib, or gst-python's "call Gst.init() first"
        return None


def sandbox(environ, flatpak_info: str = "/.flatpak-info") -> Optional[str]:
    """"Flatpak" or "Snap" when Natter runs in one, else None. A classic snap that isn't Natter
    doesn't count: a terminal started from one passes SNAP on to everything run in it."""
    if os.path.exists(flatpak_info):
        return "Flatpak"
    snap = environ.get("SNAP")
    if not snap:
        return None
    try:
        meta = Path(snap, "meta", "snap.yaml").read_text()
    except (OSError, ValueError):
        return None  # not a snap's folder
    confinement = re.search(r"^confinement:[ \t]*(\S*)", meta, re.M)
    if not (confinement and confinement[1].strip("'\"") == "classic"):
        return "Snap"
    try:
        inside = Path(__file__).resolve().is_relative_to(Path(snap).resolve())
    except OSError:
        inside = False
    return "Snap" if inside else None


def dotted(version: Iterable[int]) -> str:
    return ".".join(str(part) for part in version)


def _entries(value: str, separator: str) -> list[str]:
    return [entry.strip() for entry in value.split(separator) if entry.strip()]


def _tracer_name(entry: str) -> str:
    return entry.split("(", 1)[0].strip()  # GST_TRACERS entries are "name" or "name(params)"


def apply(environ: MutableMapping[str, str] = os.environ, *, flatpak_info: str = "/.flatpak-info",
          dirs: Optional[list[Path]] = None, version: Optional[tuple[int, ...]] = None) -> Fix:
    """Turn the camera fix on in `environ` when it's needed and the plugin is there. Call it before
    anything starts GStreamer, in Natter or WebKit. Safe to call again: it adds nothing twice.
    `flatpak_info`, `dirs` and `version` are for tests."""
    global STATUS
    try:
        STATUS = _apply(environ, flatpak_info, dirs, version)
    except Exception as error:  # a workaround never stops Natter from starting
        STATUS = Fix(False, f"failed ({type(error).__name__}: {error})")
    return STATUS


def _apply(environ, flatpak_info, dirs, version) -> Fix:
    if environ.get(SWITCH) == "0":
        return Fix(False, f"turned off ({SWITCH}=0)")
    where = sandbox(environ, flatpak_info)
    if where:
        return Fix(False, f"not used in the {where} (its runtime brings its own GStreamer)")
    plugin = next((folder / PLUGIN for folder in (plugin_dirs(environ) if dirs is None else dirs)
                   if (folder / PLUGIN).is_file()), None)
    if plugin is None:
        return Fix(False, "not installed (install.sh and meson build it where there's a C compiler and "
                          "GStreamer's headers)")
    found = gstreamer_version() if version is None else tuple(version)
    if not found:
        return Fix(False, "GStreamer's version is unknown", str(plugin))
    if tuple(found[:3]) < FIRST_AFFECTED:
        return Fix(False, f"not needed with GStreamer {dotted(found)} (WebKit sets videorate up itself)",
                   str(plugin), dotted(found))
    if not ON_BY_DEFAULT and environ.get(SWITCH) != "1":
        return Fix(False, f"needed with GStreamer {dotted(found)} but not on by default yet ({SWITCH}=1 turns it on)",
                   str(plugin), dotted(found))

    changed = {}
    folder = os.path.normpath(str(plugin.parent))
    path_var = next((name for name in PLUGIN_PATHS if name in environ), PLUGIN_PATHS[1])
    current = environ.get(path_var, "")
    if folder not in {os.path.normpath(entry) for entry in _entries(current, os.pathsep)}:
        changed[path_var] = folder + (os.pathsep + current if current else "")
    tracers = environ.get(TRACERS, "")
    if TRACER not in {_tracer_name(entry) for entry in _entries(tracers, ";")}:
        changed[TRACERS] = tracers.strip().rstrip(";") + ";" + TRACER if tracers.strip() else TRACER
    # A registry of Natter's own, unless someone chose one already.
    if REGISTRY not in environ and "GST_REGISTRY_1_0" not in environ:
        registry = private_registry(environ)
        try:
            # WebKit hands the web process this folder only if it's there when the process starts.
            registry.parent.mkdir(parents=True, exist_ok=True)
            changed[REGISTRY] = str(registry)
        except OSError:
            pass  # then the shared one
    environ.update(changed)
    home = (environ.get("HOME") or os.path.expanduser("~")).rstrip("/") + "/"
    shown = "~/" + str(plugin)[len(home):] if home != "/" and str(plugin).startswith(home) else str(plugin)
    return Fix(True, f"GStreamer {dotted(found)}, {shown}", str(plugin), dotted(found), changed)


def without_tracer(tracers: str) -> Optional[str]:
    """GST_TRACERS' value with the camera fix's tracer taken out; None when nothing's left."""
    kept = [entry for entry in tracers.split(";") if _tracer_name(entry) != TRACER]
    return ";".join(kept) if any(entry.strip() for entry in kept) else None


def start_own(environ: MutableMapping[str, str] = os.environ, init=None) -> bool:
    """Start GStreamer in Natter's own process without the camera fix's tracer (see the top of
    this file), and put GST_TRACERS back as it was for the processes WebKit starts. GStreamer
    reads GST_TRACERS once, when it starts, so call it after apply() and before anything else
    starts GStreamer. `init` (for tests) starts it; by default Gst.init. True when it started.
    Without GStreamer's Python bindings it does nothing: the start-up check says what's missing."""
    tracers = environ.get(TRACERS)
    hidden = tracers is not None and TRACER in {_tracer_name(entry) for entry in _entries(tracers, ";")}
    if hidden:
        kept = without_tracer(tracers)
        if kept is None:
            del environ[TRACERS]
        else:
            environ[TRACERS] = kept
    try:
        if init is None:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            init = (lambda: None) if Gst.is_initialized() else (lambda: Gst.init(None))
        init()
        return True
    except Exception:  # no Gst bindings, or a GStreamer that won't start: check() says so
        return False
    finally:
        if hidden:
            environ[TRACERS] = tracers


def cleaned(environ: Optional[MutableMapping[str, str]] = None) -> dict[str, Optional[str]]:
    """What apply() added, taken out of `environ` (os.environ by default): each variable that
    changes → its value without it, None to unset it. Works from the variables themselves, so a
    Natter started by a Natter is covered too."""
    environ = os.environ if environ is None else environ
    folders = {os.path.normpath(str(folder)) for folder in plugin_dirs(environ)}
    if STATUS is not None and STATUS.plugin:
        folders.add(os.path.normpath(os.path.dirname(STATUS.plugin)))
    changes: dict[str, Optional[str]] = {}
    for name in PLUGIN_PATHS:
        if name in environ:
            entries = environ[name].split(os.pathsep)
            kept = [entry for entry in entries if not (entry.strip() and os.path.normpath(entry.strip()) in folders)]
            if kept != entries:
                changes[name] = os.pathsep.join(kept) if any(entry.strip() for entry in kept) else None
    if TRACERS in environ and any(_tracer_name(entry) == TRACER for entry in environ[TRACERS].split(";")):
        changes[TRACERS] = without_tracer(environ[TRACERS])
    if environ.get(REGISTRY) == str(private_registry(environ)):
        changes[REGISTRY] = None
    return changes


def launch_env(launcher, environ: Optional[MutableMapping[str, str]] = None) -> dict[str, Optional[str]]:
    """Take the camera fix's GStreamer settings off a program Natter starts. `launcher` is a
    Gio.SubprocessLauncher, a Gio.AppLaunchContext (or Gdk's), or a dict of environment variables
    for subprocess. Returns what it changed (see cleaned())."""
    changes = cleaned(environ)
    if not changes:
        return changes
    if isinstance(launcher, MutableMapping):
        for name, value in changes.items():
            if value is None:
                launcher.pop(name, None)
            else:
                launcher[name] = value
        return changes
    from gi.repository import Gio

    for name, value in changes.items():
        if value is None:
            launcher.unsetenv(name)
        elif isinstance(launcher, Gio.SubprocessLauncher):
            launcher.setenv(name, value, True)
        else:
            launcher.setenv(name, value)
    return changes
