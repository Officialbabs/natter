"""Tests for Natter Den's WebKit/JSC/Skia memory footprint defaults, external-launch env cleanup,
AdaMemoryMonitor, hidden cache trimming, WebKit feature trims, and hardwareConcurrency cap."""

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, WebKit

from natter import account, lowmem, system

ROOT = Path(__file__).resolve().parent.parent


class WebKitMemoryEnvTest(unittest.TestCase):
    def tearDown(self):
        system._webkit_memory_added.clear()

    def test_sets_proven_jsc_skia_and_malloc_defaults(self):
        env = {}
        added = system.tune_webkit_memory(env)
        self.assertEqual(env["JSC_forceMiniVMMode"], "1")
        self.assertEqual(env["JSC_freeRetiredWasmCode"], "1")
        self.assertEqual(env["JSC_numberOfDFGCompilerThreads"], "1")
        self.assertEqual(env["JSC_numberOfFTLCompilerThreads"], "1")
        self.assertEqual(env["JSC_numberOfGCMarkers"], "2")
        self.assertEqual(env["MALLOC_ARENA_MAX"], "2")
        self.assertEqual(env["WEBKIT_SKIA_ENABLE_CPU_RENDERING"], "1")
        self.assertEqual(env["WEBKIT_SKIA_CPU_PAINTING_THREADS"], "2")
        self.assertEqual(added, system.WEBKIT_MEMORY_ENV)

    def test_respects_user_overrides_and_opt_out(self):
        env = {"JSC_forceMiniVMMode": "0", "WEBKIT_SKIA_ENABLE_CPU_RENDERING": "0"}
        added = system.tune_webkit_memory(env)
        self.assertEqual(env["JSC_forceMiniVMMode"], "0")
        self.assertEqual(env["WEBKIT_SKIA_ENABLE_CPU_RENDERING"], "0")
        self.assertNotIn("JSC_forceMiniVMMode", added)
        self.assertIn("MALLOC_ARENA_MAX", added)

        off = {system.MEMORY_TUNE_SWITCH: "0"}
        self.assertEqual(system.tune_webkit_memory(off), {})
        self.assertEqual(off, {system.MEMORY_TUNE_SWITCH: "0"})

    def test_stripped_from_external_programs(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in system.WEBKIT_MEMORY_ENV:
                os.environ.pop(key, None)
            os.environ["JSC_forceMiniVMMode"] = "custom"
            system._webkit_memory_added.clear()
            system.tune_webkit_memory()
            self.assertIn("WEBKIT_SKIA_ENABLE_CPU_RENDERING", system._webkit_memory_added)
            self.assertNotIn("JSC_forceMiniVMMode", system._webkit_memory_added)
            child_env = system.program_environ()
            self.assertNotIn("WEBKIT_SKIA_ENABLE_CPU_RENDERING", child_env)
            self.assertEqual(child_env.get("JSC_forceMiniVMMode"), "custom")
            for key in tuple(system._webkit_memory_added):
                os.environ.pop(key, None)


class FeatureTrimsTest(unittest.TestCase):
    def test_heavy_unused_features_are_disabled_while_calls_stay_intact(self):
        for name in ("TemporaryTileCohortRetention", "WebGL", "WebGLDraftExtensions",
                     "CanvasUsesAcceleratedDrawing"):
            self.assertIn(name, account.FEATURES_OFF)
        # Voice/video calls and messaging features must remain untouched.
        for kept in ("WebCodecsVideo", "MediaDevices", "MediaStream"):
            self.assertNotIn(kept, account.FEATURES_OFF)


class AdaMemoryMonitorAndHiddenTrimTest(unittest.TestCase):
    def test_ada_levels_map_to_glib_warnings(self):
        heard = []

        class FakeBus:
            def __init__(self):
                self.sub = None
                self.unsub = []

            def signal_subscribe(self, sender, iface, member, path, arg0, flags, cb):
                self.sub = (sender, iface, member, path)
                self.cb = cb
                return 42

            def signal_unsubscribe(self, sid):
                self.unsub.append(sid)

        bus = FakeBus()
        mon = lowmem.AdaMemoryMonitor(heard.append, bus=bus)
        self.assertEqual(bus.sub, ("org.ada.MemoryLevel", "org.ada.MemoryLevel", "Changed", "/org/ada/MemoryLevel"))
        for lvl in ("normal", "moderate", "low", "critical"):
            bus.cb(None, ":1.9", "/org/ada/MemoryLevel", "org.ada.MemoryLevel", "Changed", GLib.Variant("(s)", (lvl,)))
        self.assertEqual(heard, [
            int(Gio.MemoryMonitorWarningLevel.LOW),
            int(Gio.MemoryMonitorWarningLevel.MEDIUM),
            int(Gio.MemoryMonitorWarningLevel.CRITICAL),
        ])
        mon.close()
        self.assertEqual(bus.unsub, [42])

    def test_clear_hidden_clears_only_memory_cache_and_trims(self):
        calls = []
        trims = []

        class FakeMgr:
            def clear(self, types, timespan, cancellable, cb):
                calls.append((types, timespan))
                cb(self, "ok")

            def clear_finish(self, result):
                return True

        low = lowmem.LowMemory(lambda: [FakeMgr()], trim=lambda: trims.append(True), clock=lambda: 100.0)
        self.assertTrue(low.clear_hidden())
        self.assertEqual(calls, [(WebKit.WebsiteDataTypes.MEMORY_CACHE, 0)])
        self.assertEqual(trims, [True])


class HardwareConcurrencyCapTest(unittest.TestCase):
    def test_rtc_js_caps_hardware_concurrency_when_over_4_and_keeps_valid_js(self):
        rtc_js = (ROOT / "natter" / "rtc.js").read_text()
        self.assertIn("hardwareConcurrency", rtc_js)
        # Validate syntax and runtime behavior under Node.js if available.
        node = subprocess.run(["which", "node"], capture_output=True, text=True)
        if node.returncode == 0:
            probe = """
            const vm = require('vm');
            const fs = require('fs');
            const code = fs.readFileSync(process.argv[1], 'utf8');
            class Navigator {}
            Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
              get() { return 12; }, configurable: true, enumerable: true
            });
            const nav = new Navigator();
            const sandbox = {
              window: {}, navigator: nav, Navigator,
              performance: { now: () => 0 },
              Event: class {}, MessageEvent: class {}, DOMException: class extends Error {},
              Worker: class {},
            };
            sandbox.window = sandbox;
            vm.runInNewContext(code, sandbox);
            if (nav.hardwareConcurrency !== 4) {
              console.error('expected 4, got', nav.hardwareConcurrency);
              process.exit(1);
            }
            """
            res = subprocess.run(["node", "-e", probe, str(ROOT / "natter" / "rtc.js")],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
