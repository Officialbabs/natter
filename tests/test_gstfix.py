"""The camera fix (gstfix.py, camfix/nattercamfix.c): the GStreamer environment Natter sets, where it
does nothing, what programs Natter opens get back, and the tracer itself (the camera's videorate and
Web Audio's gap marks), built and loaded into a GStreamer of its own (a few plugins, a scratch
registry: no device is opened)."""

import inspect
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from natter import gstfix

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "natter" / "camfix" / "nattercamfix.c"
NEW = (1, 28, 2)
# What a child Python needs to find gi and GStreamer outside the usual places (a venv reaching the
# system's packages through PYTHONPATH, jhbuild, gst-env, nix-shell): passed on to the children
# these tests start, unlike the rest of this environment and GStreamer's own settings.
PASSED_ON = ("PATH", "PYTHONPATH", "GI_TYPELIB_PATH", "LD_LIBRARY_PATH")


def child_env(**settings) -> dict:
    env = {name: os.environ[name] for name in PASSED_ON if name in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    return {**env, **settings}


def compiler() -> list[str]:
    """$CC as make and install.sh take it: a command that may carry arguments ("ccache gcc")."""
    return shlex.split(os.environ.get("CC") or "cc") or ["cc"]


class Temp(unittest.TestCase):
    """A home with the plugin where install.sh puts it (an empty file will do for these)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = {"HOME": str(self.dir / "home"), "XDG_CACHE_HOME": str(self.dir / "cache")}
        self.plugins = gstfix.user_plugin_dir(self.env)
        self.plugins.mkdir(parents=True)
        (self.plugins / gstfix.PLUGIN).write_bytes(b"")
        self.no_flatpak = str(self.dir / "no-flatpak-info")
        recorded = mock.patch.object(gstfix, "RECORDED_DIR", self.dir / "no-camfix-dir")
        recorded.start()
        self.addCleanup(recorded.stop)
        # These describe the fix as it is once on by default; DefaultTest and WiringTest, until then.
        default = mock.patch.object(gstfix, "ON_BY_DEFAULT", True)
        default.start()
        self.addCleanup(default.stop)
        self.addCleanup(setattr, gstfix, "STATUS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, env=None, version=NEW, **kwargs):
        env = self.env if env is None else env
        kwargs.setdefault("flatpak_info", self.no_flatpak)
        return gstfix.apply(env, version=version, **kwargs)


class EnvironmentTest(Temp):
    def test_turns_the_tracer_on(self):
        fix = self.apply()
        self.assertTrue(fix.on)
        self.assertEqual(self.env["GST_PLUGIN_PATH"], str(self.plugins))
        self.assertNotIn("GST_PLUGIN_PATH_1_0", self.env)
        self.assertEqual(self.env["GST_TRACERS"], "nattercamfix")
        self.assertEqual(fix.plugin, str(self.plugins / gstfix.PLUGIN))
        self.assertEqual(fix.gstreamer, "1.28.2")
        # The home folder as ~: the line goes into bug reports.
        self.assertEqual(fix.line(), "GStreamer camera fix: on, GStreamer 1.28.2, "
                                     "~/.local/lib/natter/gstreamer-1.0/libnattercamfix.so")
        self.assertIs(gstfix.STATUS, fix)
        self.assertEqual(gstfix.debug_line(), fix.line())
        gstfix.STATUS = None
        self.assertIn("not checked", gstfix.debug_line())

    def test_prepends_to_the_plugin_path(self):
        self.env["GST_PLUGIN_PATH"] = "/opt/a:/opt/b"
        self.apply()
        self.assertEqual(self.env["GST_PLUGIN_PATH"], f"{self.plugins}:/opt/a:/opt/b")

    def test_uses_the_versioned_path_when_it_is_set(self):
        """GStreamer reads GST_PLUGIN_PATH_1_0 when it's set (even empty) and then ignores the other."""
        self.env.update(GST_PLUGIN_PATH_1_0="/opt/one", GST_PLUGIN_PATH="/opt/plain")
        self.apply()
        self.assertEqual(self.env["GST_PLUGIN_PATH_1_0"], f"{self.plugins}:/opt/one")
        self.assertEqual(self.env["GST_PLUGIN_PATH"], "/opt/plain")
        env = dict(self.env, GST_PLUGIN_PATH_1_0="")
        self.apply(env)
        self.assertEqual(env["GST_PLUGIN_PATH_1_0"], str(self.plugins))

    def test_keeps_the_tracers_already_there(self):
        self.env["GST_TRACERS"] = "latency(flags=pipeline+element);leaks;"
        self.apply()
        self.assertEqual(self.env["GST_TRACERS"], "latency(flags=pipeline+element);leaks;nattercamfix")

    def test_adds_nothing_twice(self):
        self.env.update(GST_PLUGIN_PATH="/opt/a", GST_TRACERS="leaks")
        first = self.apply()
        after = dict(self.env)
        second = self.apply()
        self.assertEqual(self.env, after)
        self.assertTrue(second.on)
        self.assertEqual(second.changed, {})
        self.assertEqual(set(first.changed), {"GST_PLUGIN_PATH", "GST_TRACERS", "GST_REGISTRY"})
        # Listed already, with parameters or a trailing slash: still once.
        env = dict(self.env, GST_TRACERS="nattercamfix(x=1)", GST_PLUGIN_PATH=f"{self.plugins}/")
        self.assertEqual(self.apply(env).changed, {})

    def test_a_registry_of_its_own(self):
        self.apply()
        registry = Path(self.env["GST_REGISTRY"])
        self.assertEqual(registry, self.dir / "cache" / "natter" / "gstreamer-1.0" / f"registry.{platform.machine()}.bin")
        self.assertTrue(registry.parent.is_dir())  # WebKit's sandbox only hands over a folder that exists
        self.assertFalse(registry.exists())  # GStreamer writes it

    def test_keeps_a_registry_someone_chose(self):
        for name in ("GST_REGISTRY", "GST_REGISTRY_1_0"):
            env = dict(self.env, **{name: "/elsewhere/registry.bin"})
            fix = self.apply(env)
            self.assertTrue(fix.on)
            self.assertEqual(env[name], "/elsewhere/registry.bin")
            self.assertNotIn("GST_REGISTRY", fix.changed)

    def test_home_cache_without_xdg(self):
        env = {"HOME": self.env["HOME"]}
        self.apply(env)
        self.assertTrue(env["GST_REGISTRY"].startswith(f"{self.env['HOME']}/.cache/natter/gstreamer-1.0/"))

    def test_natters_own_environment_by_default(self):
        self.assertIs(inspect.signature(gstfix.apply).parameters["environ"].default, os.environ)

    def test_the_recorded_folder_comes_first(self):
        """meson.build writes where it installed the plugin into camfix-dir, next to gstfix.py."""
        installed = self.dir / "usr" / "lib" / "natter" / "gstreamer-1.0"
        installed.mkdir(parents=True)
        (installed / gstfix.PLUGIN).write_bytes(b"")
        gstfix.RECORDED_DIR.write_text(f"{installed}\n")
        self.assertEqual(gstfix.plugin_dirs(self.env), [installed, self.plugins])
        self.assertEqual(self.apply().plugin, str(installed / gstfix.PLUGIN))

    def test_main_applies_it_before_anything_starts_gstreamer(self):
        main = (ROOT / "natter" / "__main__.py").read_text()
        body = main[main.index("def main()"):]
        self.assertLess(body.index("fix_camera()"), body.index("if check():"))  # check() loads WebKit
        self.assertLess(body.index("fix_camera()"), body.index("from .app import"))
        # Natter's own GStreamer starts right after, without the tracer, before check() starts it.
        self.assertLess(body.index("fix_camera()"), body.index("start_gstreamer()"))
        self.assertLess(body.index("start_gstreamer()"), body.index("if check():"))


class OwnGStreamerTest(Temp):
    """Natter's own process starts GStreamer without the tracer (start_own): a crash there would
    take the whole app, and the capture pipelines it fixes are only in WebKit's web process."""

    def test_the_tracer_is_left_out_while_it_starts_and_put_back_after(self):
        for before, during in (("nattercamfix", None), ("leaks;nattercamfix", "leaks"),
                               ("nattercamfix(x=1);latency", "latency"), ("latency", "latency")):
            with self.subTest(before):
                env, seen = {"GST_TRACERS": before}, []
                self.assertTrue(gstfix.start_own(env, init=lambda: seen.append(env.get("GST_TRACERS"))))
                self.assertEqual(seen, [during])
                self.assertEqual(env, {"GST_TRACERS": before})  # for WebKit's processes

    def test_without_tracers_nothing_changes(self):
        env, seen = {}, []
        gstfix.start_own(env, init=lambda: seen.append(dict(env)))
        self.assertEqual((env, seen), ({}, [{}]))

    def test_put_back_even_when_gstreamer_wont_start(self):
        env = {"GST_TRACERS": "nattercamfix"}

        def fail():
            raise RuntimeError("no GStreamer")
        self.assertFalse(gstfix.start_own(env, init=fail))
        self.assertEqual(env, {"GST_TRACERS": "nattercamfix"})

    def test_natters_own_environment_by_default(self):
        self.assertIs(inspect.signature(gstfix.start_own).parameters["environ"].default, os.environ)


class GatingTest(Temp):
    def assert_off(self, fix, words):
        self.assertFalse(fix.on)
        self.assertIn(words, fix.reason)
        self.assertEqual(fix.changed, {})
        self.assertFalse([name for name in self.env if name.startswith("GST_")])

    def test_not_in_the_flatpak(self):
        flatpak = self.dir / ".flatpak-info"
        flatpak.write_text("[Application]\nname=io.github.Officialbabs.Natter\n")
        self.assert_off(self.apply(flatpak_info=str(flatpak)), "Flatpak")

    def test_not_in_a_snap(self):
        snap = self.dir / "snap"
        (snap / "meta").mkdir(parents=True)
        (snap / "meta" / "snap.yaml").write_text("name: natter\nconfinement: strict\n")
        self.env["SNAP"] = str(snap)
        self.assert_off(self.apply(), "Snap")

    def test_a_terminal_from_a_classic_snap_is_still_native(self):
        snap = self.dir / "code"
        (snap / "meta").mkdir(parents=True)
        (snap / "meta" / "snap.yaml").write_text("name: code\nconfinement: classic\n")
        self.env["SNAP"] = str(snap)
        self.assertTrue(self.apply().on)
        self.env["SNAP"] = str(self.dir / "not-a-snap")
        self.assertIsNone(gstfix.sandbox(self.env, self.no_flatpak))

    def test_not_without_the_plugin(self):
        (self.plugins / gstfix.PLUGIN).unlink()
        self.assert_off(self.apply(), "not installed")

    def test_not_before_gstreamer_1_28(self):
        for version in ((1, 24, 2), (1, 26, 11), (1, 27, 90)):
            self.assert_off(self.apply(version=version), f"GStreamer {'.'.join(map(str, version))}")
        for version in ((1, 28, 0), (1, 29, 1), (2, 0, 0)):
            env = dict(self.env)
            self.assertTrue(self.apply(env, version=version).on, version)

    def test_not_when_the_version_is_unknown(self):
        with mock.patch.object(gstfix, "gstreamer_version", return_value=None):
            self.assert_off(self.apply(version=None), "unknown")

    def test_switched_off(self):
        self.env["NATTER_CAMFIX"] = "0"
        fix = self.apply()
        self.assertFalse(fix.on)
        self.assertEqual(sorted(self.env), ["HOME", "NATTER_CAMFIX", "XDG_CACHE_HOME"])

    def test_never_stops_natter_starting(self):
        with mock.patch.object(gstfix, "_apply", side_effect=RuntimeError("boom")):
            fix = self.apply()
        self.assertFalse(fix.on)
        self.assertIn("boom", fix.reason)

    def test_reads_the_version_without_starting_gstreamer(self):
        script = textwrap.dedent("""
            import ctypes, json, sys
            sys.path.insert(0, sys.argv[1])
            from natter.gstfix import gstreamer_version
            version = gstreamer_version()
            try:
                started = bool(ctypes.CDLL("libgstreamer-1.0.so.0").gst_is_initialized())
            except OSError:
                started = None
            print(json.dumps([version, started]))
        """)
        result = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT)], capture_output=True, text=True,
                                timeout=60, env=child_env(HOME=self.env["HOME"]))
        version, started = json.loads(result.stdout)
        if started is None:
            self.skipTest("no libgstreamer-1.0")
        self.assertEqual(len(version), 3)
        self.assertTrue(all(isinstance(part, int) for part in version))
        self.assertFalse(started)


class LaunchEnvTest(Temp):
    def test_gives_back_what_was_there(self):
        self.env.update(GST_PLUGIN_PATH="/opt/a:/opt/b", GST_TRACERS="leaks")
        self.apply()
        child = dict(self.env)
        changes = gstfix.launch_env(child, self.env)
        self.assertEqual(changes, {"GST_PLUGIN_PATH": "/opt/a:/opt/b", "GST_TRACERS": "leaks", "GST_REGISTRY": None})
        self.assertEqual(child["GST_PLUGIN_PATH"], "/opt/a:/opt/b")
        self.assertEqual(child["GST_TRACERS"], "leaks")
        self.assertNotIn("GST_REGISTRY", child)

    def test_unsets_what_was_not_there(self):
        before = dict(self.env)
        self.apply()
        child = dict(self.env)
        gstfix.launch_env(child, self.env)
        self.assertEqual(child, before)

    def test_a_natter_started_by_a_natter(self):
        """The variables alone are enough: no apply() in this process."""
        self.env.update(GST_PLUGIN_PATH_1_0=f"{self.plugins}:/opt/x", GST_TRACERS="nattercamfix;stats",
                        GST_REGISTRY=str(gstfix.private_registry(self.env)))
        child = dict(self.env)
        gstfix.launch_env(child, self.env)
        self.assertEqual((child["GST_PLUGIN_PATH_1_0"], child["GST_TRACERS"]), ("/opt/x", "stats"))
        self.assertNotIn("GST_REGISTRY", child)

    def test_nothing_to_do(self):
        self.env.update(GST_PLUGIN_PATH="/opt/a", GST_TRACERS="leaks", GST_REGISTRY="/mine.bin")
        self.apply(version=(1, 26, 6))
        child = dict(self.env)
        self.assertEqual(gstfix.launch_env(child, self.env), {})
        self.assertEqual(child, self.env)

    def test_gio_launchers(self):
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        self.env["GST_TRACERS"] = "leaks"
        self.apply()
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        for name, value in self.env.items():
            launcher.setenv(name, value, True)
        gstfix.launch_env(launcher, self.env)
        self.assertEqual(launcher.getenv("GST_TRACERS"), "leaks")
        self.assertIsNone(launcher.getenv("GST_PLUGIN_PATH"))
        self.assertIsNone(launcher.getenv("GST_REGISTRY"))

        context = Gio.AppLaunchContext()
        gstfix.launch_env(context, self.env)
        environment = dict(entry.split("=", 1) for entry in context.get_environment())
        self.assertEqual(environment.get("GST_TRACERS"), "leaks")
        self.assertNotIn("GST_PLUGIN_PATH", environment)
        self.assertNotIn("GST_REGISTRY", environment)


class DefaultTest(Temp):
    """apply() changes Natter's whole environment, which every program Natter starts inherits: on
    without NATTER_CAMFIX=1 only once system.py takes it off them (WiringTest)."""

    def test_only_when_switched_on_until_then(self):
        with mock.patch.object(gstfix, "ON_BY_DEFAULT", False):
            fix = self.apply()
            self.assertFalse(fix.on)
            self.assertEqual(fix.line(), "GStreamer camera fix: off, needed with GStreamer 1.28.2 but not on "
                                         "by default yet (NATTER_CAMFIX=1 turns it on)")
            self.assertEqual(fix.plugin, str(self.plugins / gstfix.PLUGIN))  # for the About dialog
            self.assertFalse([name for name in self.env if name.startswith("GST_")])
            self.env["NATTER_CAMFIX"] = "1"
            self.assertTrue(self.apply().on)
            self.assertEqual(self.env["GST_TRACERS"], "nattercamfix")
            # Switched on, it's still only where it's needed.
            env = dict(self.env)
            del env["GST_TRACERS"]
            self.assertIn("not needed", self.apply(env, version=(1, 26, 6)).reason)
            self.assertNotIn("GST_TRACERS", env)


class WiringTest(unittest.TestCase):
    """The fix is on by default exactly when system.py keeps its settings from the programs Natter
    starts: the call window and others (give_system_bus) and the apps for links and files
    (open_uri). Applying one without the other fails here."""

    def setUp(self):
        try:
            import gi

            for namespace, version in (("Gio", "2.0"), ("Gdk", "4.0"), ("Gtk", "4.0")):
                gi.require_version(namespace, version)
            from gi.repository import Gtk  # noqa: F401  (open_uri's)
        except (ImportError, ValueError) as error:
            self.skipTest(f"no GTK 4 for Python ({error})")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.plugins = gstfix.user_plugin_dir({"HOME": tmp.name})
        self.plugins.mkdir(parents=True)
        (self.plugins / gstfix.PLUGIN).write_bytes(b"")
        self.addCleanup(setattr, gstfix, "STATUS", gstfix.STATUS)

    def kept_from_programs_natter_starts(self) -> dict:
        from gi.repository import Gio
        from natter import system

        environ = {"HOME": str(self.home), "XDG_CACHE_HOME": str(self.home / "cache"), "NATTER_CAMFIX": "1"}
        with mock.patch.dict(os.environ, environ):
            for name in [name for name in os.environ if name.startswith("GST_")]:
                del os.environ[name]  # put back with the rest when the patch ends
            fix = gstfix.apply(os.environ, flatpak_info=str(self.home / "no-flatpak-info"), dirs=[self.plugins],
                               version=NEW)
            self.assertTrue(fix.on, fix.reason)
            self.assertEqual(os.environ.get(gstfix.TRACERS), gstfix.TRACER)

            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)  # with the environment as it is now
            system.give_system_bus(launcher)
            context = Gio.AppLaunchContext()
            window = mock.Mock()
            window.get_display.return_value.get_app_launch_context.return_value = context
            with mock.patch.object(Gio.AppInfo, "launch_default_for_uri_async") as launch, \
                    mock.patch("gi.repository.Gtk.UriLauncher") as gtk:
                # Nothing is started: both ways open_uri() launches are stand-ins here, and no app
                # takes this scheme should another way appear.
                system.open_uri("natter-wiring-test:nothing", window)
            opened = dict(entry.split("=", 1) for entry in context.get_environment())
        return {"give_system_bus": launcher.getenv(gstfix.TRACERS) is None,
                "open_uri": launch.called and not gtk.new.called and gstfix.TRACERS not in opened}

    def test_on_by_default_only_once_programs_natter_starts_go_without_it(self):
        kept = self.kept_from_programs_natter_starts()
        if gstfix.ON_BY_DEFAULT:
            self.assertTrue(all(kept.values()), f"the camera fix is on by default, but programs Natter starts "
                                                f"would inherit its GStreamer settings: {kept}")
        else:
            self.assertFalse(all(kept.values()), "system.py keeps the camera fix's settings from the programs "
                                                 "Natter starts now: set gstfix.ON_BY_DEFAULT = True")


# ---- The tracer, built and loaded -------------------------------------------------------------

# Run in a GStreamer of its own (see Plugin.gst_env). Prints the videorate settings each case ends up with.
PROBE = r"""
import json, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)

def settings(rate):
    return [rate.get_property("skip-to-first"), rate.get_property("drop-only"), rate.get_property("average-period")]

def capture_tail(name):
    # GStreamerCapturer::setupPipeline + GStreamerVideoCapturer::createConverter (WebKit 2.52-2.54):
    # the converter bin is made first, unnamed, and joins the pipeline afterwards.
    pipeline = Gst.Pipeline.new(name)
    converter = Gst.Bin.new(None)
    convert = Gst.ElementFactory.make("videoconvert")
    rate = Gst.ElementFactory.make("videorate", "videorate")
    converter.add(convert)
    converter.add(rate)
    convert.link(rate)
    converter.add_pad(Gst.GhostPad.new("sink", convert.get_static_pad("sink")))
    converter.add_pad(Gst.GhostPad.new("src", rate.get_static_pad("src")))
    return pipeline, converter, rate

out = {}
pipeline, converter, rate = capture_tail("Video_capturer_pipeline0_7DE6440DD080")
out["before_joining"] = settings(rate)
pipeline.add(converter)
out["webkit"] = settings(rate)

direct = Gst.Pipeline.new("Video_capturer_pipeline1_7DE6440DD1B0")
late = Gst.ElementFactory.make("videorate")
direct.add(late)
out["added_into_the_pipeline"] = settings(late)

other, other_bin, other_rate = capture_tail("playbin0")
other.add(other_bin)
out["other_pipeline"] = settings(other_rate)
lookalike = Gst.Bin.new("Video_capturer_pipeline2_0")  # a bin, not a pipeline
lone = Gst.ElementFactory.make("videorate", "videorate")
lookalike.add(lone)
out["bin_named_like_one"] = settings(lone)

ready = Gst.Pipeline.new("Video_capturer_pipeline3_0")
fixed = Gst.ElementFactory.make("videorate")
for key, value in (("skip-to-first", True), ("drop-only", True), ("average-period", 1)):
    fixed.set_property(key, value)
ready.add(fixed)  # already set: no change, no log line
out["already_set"] = settings(fixed)

def replica(name, hold):
    # capture2's replica: system clock, base time 0 (so a live source's PTS is CLOCK_MONOTONIC),
    # live videotestsrc at 30/s, WebKit's converter, a 30/1 capsfilter, fakesink. Opens no device.
    pipeline, converter, rate = capture_tail(name)
    pipeline.use_clock(Gst.SystemClock.obtain())
    pipeline.set_base_time(0)
    pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
    src = Gst.ElementFactory.make("videotestsrc")
    src.set_property("is-live", True)
    srccaps = Gst.ElementFactory.make("capsfilter")
    srccaps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420,width=160,height=90,framerate=30/1"))
    caps = Gst.ElementFactory.make("capsfilter")
    caps.set_property("caps", Gst.Caps.from_string("video/x-raw,width=160,height=90,framerate=30/1"))
    sink = Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", False)
    first = []
    def probe(pad, info):
        first.append(info.get_buffer().pts)
        return Gst.PadProbeReturn.REMOVE  # the first buffer is enough
    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
    for element in (src, srccaps):
        pipeline.add(element)
    pipeline.add(converter)
    for element in (caps, sink):
        pipeline.add(element)
    assert src.link(srccaps) and srccaps.link(converter) and converter.link(caps) and caps.link(sink)
    pipeline.set_state(Gst.State.PLAYING)
    time.sleep(hold)
    counts = {key: rate.get_property(key) for key in ("in", "out")}
    pipeline.set_state(Gst.State.NULL)
    counts["first_pts_s"] = first[0] / Gst.SECOND if first else None
    counts["now_s"] = Gst.SystemClock.obtain().get_time() / Gst.SECOND
    return counts

if len(sys.argv) > 1:
    out["replica"] = replica("Video_capturer_pipeline4_7DE6440DD2E0", float(sys.argv[1]))
print(json.dumps(out))
"""


# Natter's own start (gstfix.start_own), then WebKit's capture pipeline in that process.
OWN_PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from natter import gstfix
started = gstfix.start_own()
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
pipeline = Gst.Pipeline.new("Video_capturer_pipeline0_7DE6440DD080")
rate = Gst.ElementFactory.make("videorate")
pipeline.add(rate)
print(json.dumps({"started": started, "env": os.environ.get("GST_TRACERS"),
                  "tracers": [type(tracer).__name__ for tracer in Gst.tracing_get_active_tracers()],
                  "settings": [rate.get_property(key) for key in ("skip-to-first", "drop-only", "average-period")]}))
"""


# WebKit's Web Audio source as WebKit 2.52 makes it: a bin of type WebKitWebAudioSrc, made without
# a factory, around an appsrc whose src pad it ghosts, added to its pipeline before it streams.
# Prints the flags of each buffer as it leaves the appsrc and as it reaches the sink.
AUDIO_PROBE = r"""
import json
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst
Gst.init(None)

class WebAudioSrc(Gst.Bin):
    __gtype_name__ = "WebKitWebAudioSrc"

class OtherSrc(Gst.Bin):
    __gtype_name__ = "NatterLabOtherAudioSrc"

MARKS = Gst.BufferFlags.GAP | Gst.BufferFlags.DROPPABLE

def flags(buffer):
    return [name for name, flag in (("gap", Gst.BufferFlags.GAP), ("droppable", Gst.BufferFlags.DROPPABLE),
                                    ("marker", Gst.BufferFlags.MARKER)) if buffer.has_flags(flag)]

def run(kind):
    pipeline = Gst.Pipeline.new(None)
    src = kind()
    appsrc = Gst.ElementFactory.make("appsrc")
    appsrc.set_property("format", Gst.Format.TIME)
    src.add(appsrc)
    src.add_pad(Gst.GhostPad.new("src", appsrc.get_static_pad("src")))
    sink = Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", False)
    pipeline.add(src)
    pipeline.add(sink)
    assert src.link(sink)
    seen = {"inside": [], "sink": []}
    def watch(where):
        def probe(pad, info):
            if info.type & Gst.PadProbeType.BUFFER_LIST:
                listed = info.get_buffer_list()
                seen[where].extend(flags(listed.get(i)) for i in range(listed.length()))
            else:
                seen[where].append(flags(info.get_buffer()))
            return Gst.PadProbeReturn.OK
        return probe
    for pad, where in ((appsrc.get_static_pad("src"), "inside"), (sink.get_static_pad("sink"), "sink")):
        pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, watch(where))
    pipeline.set_state(Gst.State.PLAYING)
    def buffer(n, marks):
        made = Gst.Buffer.new_wrapped(bytes(512))
        made.pts = n * Gst.SECOND // 100
        made.set_flags(marks)
        return made
    appsrc.emit("push-buffer", buffer(0, MARKS))  # silence, as WebKit marks it
    appsrc.emit("push-buffer", buffer(1, Gst.BufferFlags.MARKER))  # sound, another flag
    appsrc.emit("push-buffer", buffer(2, Gst.BufferFlags.GAP))
    listed = Gst.BufferList.new()
    for n in (3, 4):
        listed.insert(-1, buffer(n, MARKS))
    appsrc.emit("push-buffer-list", listed)
    appsrc.emit("end-of-stream")
    message = pipeline.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    assert message is not None and message.type == Gst.MessageType.EOS, message
    return seen

print(json.dumps({"other_first": run(OtherSrc), "webkit": run(WebAudioSrc), "other_after": run(OtherSrc)}))
"""


def toolchain() -> bool:
    return bool(shutil.which(compiler()[0]) and shutil.which("pkg-config")) and subprocess.run(
        ["pkg-config", "--exists", "gstreamer-1.0"], capture_output=True).returncode == 0


# A child Python as the probes run it: PYTHONPATH counts (unlike with -I), the user's site packages
# and the current folder don't.
CHILD = [sys.executable, "-s", "-P"]
# What the probes need, checked where they run: in their environment, not this process's.
REACHABLE = "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None)"


@unittest.skipUnless(toolchain(), "needs a C compiler, pkg-config and GStreamer's headers")
class PluginTest(unittest.TestCase):
    NEEDED = ("coreelements", "videotestsrc", "videorate", "videoconvertscale", "app")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.build = cls.dir / "build"
        cls.build.mkdir()
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gstreamer-1.0"], capture_output=True,
                               text=True, check=True).stdout.split()
        # As install.sh builds it, and with warnings as errors: the source stays -Wall clean.
        cls.compiled = subprocess.run([*compiler(), "-shared", "-fPIC", "-O2", "-Wall", "-Werror",
                                       "-o", str(cls.build / gstfix.PLUGIN), str(SOURCE), *flags],
                                      capture_output=True, text=True, timeout=120)
        # A GStreamer of its own: only the plugins the probe uses, a scratch registry.
        system = subprocess.run(["pkg-config", "--variable=pluginsdir", "gstreamer-1.0"], capture_output=True,
                                text=True).stdout.strip()
        cls.system = cls.dir / "system"
        cls.system.mkdir()
        for name in cls.NEEDED:
            library = Path(system, f"libgst{name}.so")
            if library.exists():
                (cls.system / library.name).symlink_to(library)
        cls.reachable = subprocess.run([*CHILD, "-c", REACHABLE], capture_output=True, text=True, timeout=60,
                                       env=cls.gst_env(tracer=False, registry="check.bin"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.assertEqual(self.compiled.returncode, 0, self.compiled.stderr)
        self.assertEqual(self.compiled.stderr, "")
        missing = [name for name in self.NEEDED if not (self.system / f"libgst{name}.so").exists()]
        if missing:
            self.skipTest(f"GStreamer plugins missing: {', '.join(missing)}")

    @classmethod
    def gst_env(cls, tracer=True, registry="registry.bin"):
        """A GStreamer of its own: none of this environment's GStreamer settings, the plugins in
        cls.system, a scratch registry, and the tracer just built."""
        env = child_env(HOME=str(cls.dir), LANG="C.UTF-8", XDG_CACHE_HOME=str(cls.dir / "cache"),
                        GST_REGISTRY=str(cls.dir / registry), GST_PLUGIN_SYSTEM_PATH_1_0=str(cls.system),
                        GST_DEBUG_NO_COLOR="1")
        if tracer:
            env.update(GST_PLUGIN_PATH_1_0=str(cls.build), GST_TRACERS="nattercamfix", GST_DEBUG="nattercamfix:4")
        return env

    def probe(self, env, *args, script=PROBE):
        if self.reachable.returncode != 0:
            last = (self.reachable.stderr.strip().splitlines() or ["?"])[-1]
            self.skipTest(f"no GStreamer for Python in the probes' environment ({last})")
        result = subprocess.run([*CHILD, "-c", script, *args], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1]), result.stderr

    def test_the_plugin_describes_itself(self):
        result = subprocess.run(["gst-inspect-1.0", "nattercamfix"], capture_output=True, text=True, timeout=60,
                                env=self.gst_env()) if shutil.which("gst-inspect-1.0") else None
        if result is None:
            self.skipTest("no gst-inspect-1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\n\s+License\s+GPL\n")
        self.assertRegex(result.stdout, r"\+----GstTracer\n\s+\+----NatterCamFix\n")

    def test_sets_webkits_capture_videorate_and_nothing_else(self):
        found, log = self.probe(self.gst_env())
        on, default = [True, True, 1], [False, False, 0]
        self.assertEqual(found["before_joining"], default)  # not in a capture pipeline yet
        self.assertEqual(found["webkit"], on)
        self.assertEqual(found["added_into_the_pipeline"], on)
        self.assertEqual(found["other_pipeline"], default)
        self.assertEqual(found["bin_named_like_one"], default)
        self.assertEqual(found["already_set"], on)
        changes = [line for line in log.splitlines() if " INFO " in line and "nattercamfix" in line]
        self.assertEqual(len(changes), 2, log)  # one line per videorate changed
        self.assertIn("Video_capturer_pipeline0_7DE6440DD080: set skip-to-first=true, drop-only=true, "
                      "average-period=1 (were false, false, 0)", changes[0])

    def test_natters_own_process_runs_without_it(self):
        # The same pipeline as WebKit's capture: in Natter's own process (start_own) it's left as it
        # is, while the environment WebKit's processes get still has the tracer.
        if self.reachable.returncode != 0:
            self.skipTest("no GStreamer for Python in the probes' environment")
        result = subprocess.run([*CHILD, "-c", OWN_PROBE, str(ROOT)], capture_output=True, text=True, timeout=120,
                                env=self.gst_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(found["started"])
        self.assertEqual(found["settings"], [False, False, 0])
        self.assertNotIn("NatterCamFix", found["tracers"])
        self.assertEqual(found["env"], "nattercamfix")

    def test_clears_the_gap_marks_on_webkits_web_audio_and_nothing_else(self):
        found, log = self.probe(self.gst_env(), script=AUDIO_PROBE)
        marked = [["gap", "droppable"], ["marker"], ["gap"], ["gap", "droppable"], ["gap", "droppable"]]
        webkit = found["webkit"]
        self.assertEqual(webkit["inside"], marked)  # as WebKit pushes them, inside its bin
        self.assertEqual(webkit["sink"], [[], ["marker"], [], [], []])  # the silence still arrives, unmarked
        for other in ("other_first", "other_after"):  # any other source keeps its marks
            self.assertEqual(found[other], {"inside": marked, "sink": marked})
        self.assertEqual(log.count("found WebKit's Web Audio source"), 1, log)

    def test_without_the_tracer_web_audio_keeps_its_gap_marks(self):
        found, _log = self.probe(self.gst_env(tracer=False, registry="plain.bin"), script=AUDIO_PROBE)
        self.assertEqual(found["webkit"]["sink"], found["webkit"]["inside"])
        self.assertIn(["gap", "droppable"], found["webkit"]["sink"])

    def test_without_the_tracer_nothing_changes(self):
        found, _log = self.probe(self.gst_env(tracer=False, registry="plain.bin"))
        self.assertEqual(found["webkit"], [False, False, 0])

    def test_the_replica_runs_at_30_a_second_instead_of_flooding(self):
        """capture2's frozen first frame without a camera: with the tracer, about 30 frames a second
        at the live time; without it, thousands of copies of the first frame stamped from 0 up."""
        hold = "0.5"
        fixed, _log = self.probe(self.gst_env(), hold)
        flood, _log = self.probe(self.gst_env(tracer=False, registry="plain.bin"), hold)
        fixed, flood = fixed["replica"], flood["replica"]
        self.assertLessEqual(fixed["out"], fixed["in"] + 1, fixed)
        self.assertLess(fixed["out"], 40, fixed)  # ~15 in 0.5 s
        self.assertGreater(fixed["first_pts_s"], fixed["now_s"] - 5, fixed)  # the live time
        self.assertLess(flood["first_pts_s"], 1, flood)  # from 0 ...
        if flood["now_s"] > 60:  # ... up to the live time (the uptime): the longer it's been up, the longer the freeze
            self.assertGreater(flood["out"], 50 * max(flood["in"], 1), flood)


class InstallTest(unittest.TestCase):
    def build_camfix(self, folder, env=None):
        """install.sh's own build_camfix(), run by itself."""
        install = (ROOT / "install.sh").read_text()
        function = re.search(r"^build_camfix\(\) \{\n.*?^\}\n", install, re.M | re.S)
        self.assertIsNotNone(function)
        script = f'{function[0]}\nHERE="$1"; CAMFIX_DIR="$2"; APP_NAME="Natter Den"\nbuild_camfix "$3"\n'
        return subprocess.run(["sh", "-ec", script, "sh", str(ROOT), str(folder), sys.executable],
                              capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, **(env or {})))

    def test_builds_into_the_folder_gstfix_looks_in(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn('CAMFIX_DIR="$HOME/.local/lib/natter/gstreamer-1.0"', install)
        self.assertEqual(gstfix.user_plugin_dir({"HOME": "/home/x"}), Path("/home/x/.local/lib/natter/gstreamer-1.0"))
        self.assertIn('"$CAMFIX_DIR/libnattercamfix.so"', install[install.index('if [ -n "$UNINSTALL" ]'):])
        self.assertIn('build_camfix "$PYTHON"', install)

    @unittest.skipUnless(toolchain(), "needs a C compiler, pkg-config and GStreamer's headers")
    def test_install_sh_builds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "lib", "natter", "gstreamer-1.0")
            result = self.build_camfix(folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"==> Building the camera fix into {folder}", result.stdout)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), [gstfix.PLUGIN])  # no .new left behind

    @unittest.skipUnless(toolchain() and shutil.which("env"), "needs a C compiler, pkg-config and GStreamer's headers")
    def test_install_sh_takes_a_cc_with_arguments(self):
        """As make does: CC="ccache gcc" or "gcc -m64". Here a wrapper first ("env"), as ccache is,
        and a flag after: both have to reach the command line for the build to work."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "lib", "natter", "gstreamer-1.0")
            result = self.build_camfix(folder, {"CC": " ".join(["env", *compiler(), "-O0"])})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("skipped", result.stdout)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), [gstfix.PLUGIN])

    def test_install_sh_skips_without_a_compiler(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "lib")
            for cc in (str(Path(tmp, "no-such-cc")), f"{Path(tmp, 'no-such-ccache')} cc -O2"):
                result = self.build_camfix(folder, {"CC": cc})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(folder.exists())
                self.assertLessEqual(len(result.stdout.splitlines()), 1)  # a one-line hint at most
                self.assertNotIn("Building", result.stdout)

    def test_meson_builds_it_optionally(self):
        meson = (ROOT / "meson.build").read_text()
        self.assertIn("add_languages('c', required: false", meson)
        self.assertIn("dependency('gstreamer-1.0', required: false)", meson)
        self.assertIn("shared_module('nattercamfix', 'natter' / 'camfix' / 'nattercamfix.c'", meson)
        self.assertIn("exclude_directories: ['__pycache__', 'camfix']", meson)
        self.assertIn("output: 'camfix-dir'", meson)
        self.assertEqual(gstfix.RECORDED_DIR.name, "camfix-dir")
        self.assertEqual((ROOT / "natter" / "camfix" / "camfix-dir.in").read_text(), "@DIR@\n")

    def test_licence(self):
        source = SOURCE.read_text()
        self.assertTrue(source.startswith("/* SPDX-License-Identifier: GPL-3.0-or-later"))
        self.assertIn("license: 'GPL-3.0-or-later'", (ROOT / "meson.build").read_text())
        self.assertIn('"GPL"', source)  # GStreamer's name for it in GST_PLUGIN_DEFINE


if __name__ == "__main__":
    unittest.main()
