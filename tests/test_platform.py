"""Natter on the system: its folders, its scope, swap, the sandbox, distributions, versions, the
codec check, the Background portal, and the files that install it."""

import contextlib
import ctypes
import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import warnings
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from unittest import mock

from natter import DEFAULT_APP_ID, NAME, OLD_APP_ID, VERSION, distro, system
from natter.__main__ import FLOORS, explain, too_old
from natter.distro import DESKTOP, GST_PYTHON, MEDIA, image_based, install_hint, os_release, package_manager
from natter.settings import folder_name, private_folder, spell_languages, write_private
from natter.system import (Reclaimer, can_reclaim, cgroup2_mount, cheap_swap, confined_snap, in_sandbox,
                           media_report, own_cgroup, own_unit, scope_name)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class FoldersTest(Temp):
    def test_names(self):
        self.assertEqual(folder_name(DEFAULT_APP_ID), "natter")
        self.assertEqual(folder_name(OLD_APP_ID), "natter")  # the rename keeps every session
        self.assertEqual(folder_name("io.natter.Natter.LabW1"), "natter-labw1")
        self.assertEqual(folder_name("io.github.Officialbabs.Natter.Dev"), "natter-dev")

    def test_private_folder(self):
        made = private_folder(self.dir / "a" / "natter")
        self.assertEqual(mode(made), 0o700)
        loose = self.dir / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        private_folder(loose)
        self.assertEqual(mode(loose), 0o700)

    def test_write_private(self):
        path = self.dir / "settings.json"
        path.write_text("old")
        path.chmod(0o644)
        leftover = self.dir / "settings.json.tmp"
        leftover.write_text("half")
        leftover.chmod(0o666)
        write_private(path, "new")
        self.assertEqual(path.read_text(), "new")
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json"])

    def test_spell_languages(self):
        self.assertEqual(spell_languages(["pt_BR.UTF-8", "pt_BR", "pt.UTF-8", "pt", "en", "C"]), ["pt_BR", "en"])
        self.assertEqual(spell_languages(["de_DE@euro", "de_DE", "de"]), ["de_DE"])
        self.assertEqual(spell_languages(["fil_PH", "fil", "C"]), ["fil_PH"])
        self.assertEqual(spell_languages(["C", "POSIX"]), ["en_US"])


class ScopeTest(unittest.TestCase):
    def test_launchers_units_count_as_own(self):
        for unit in [f"app-gnome-{DEFAULT_APP_ID}-12345.scope",  # GNOME Shell
                     f"app-{DEFAULT_APP_ID}@3f2a1c0d9e8b4a7f.service",  # KDE Plasma
                     f"app-flatpak-{DEFAULT_APP_ID}-4242.scope",  # flatpak run
                     f"app-ada-{DEFAULT_APP_ID}-4242.scope",  # Ada's launcher
                     f"app-{DEFAULT_APP_ID}-7.scope"]:  # Natter's own
            self.assertTrue(own_unit(unit, DEFAULT_APP_ID), unit)

    def test_escaped_dash(self):
        self.assertTrue(own_unit("app-gnome-org.example.Chat\\x2dApp-12.scope", "org.example.Chat-App"))
        self.assertTrue(own_unit("app-ada-org.example.Chat-App-12.scope", "org.example.Chat-App"))  # unescaped

    def test_other_units_do_not(self):
        for unit in ["app-ada-com.anthropic.Claude-9571.scope",  # started from another app
                     "app-gnome-org.gnome.Ptyxis-77.scope",
                     "vte-spawn-1234.scope",  # a terminal tab
                     "session-2.scope",  # a login session
                     f"dbus-:1.2-{DEFAULT_APP_ID}@0.service",  # D-Bus activation
                     f"app-gnome-{DEFAULT_APP_ID}Beta-1.scope",  # an id that only starts the same
                     f"app-gnome-{DEFAULT_APP_ID}.LabW1-1.scope",  # a lab instance
                     f"app-gnome-{OLD_APP_ID}-1.scope",  # Natter before the rename
                     f"app-gnome-{DEFAULT_APP_ID}-1.slice",
                     "app.slice"]:
            self.assertFalse(own_unit(unit, DEFAULT_APP_ID), unit)

    def test_scope_name_is_own(self):
        self.assertTrue(own_unit(scope_name(99)))

    def test_cgroup_mount(self):
        unified = ("35 24 0:30 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime shared:9 - "
                   "cgroup2 cgroup2 rw,nsdelegate,memory_recursiveprot\n")
        hybrid = ("30 24 0:26 / /sys/fs/cgroup ro,nosuid shared:9 - tmpfs tmpfs ro,mode=755\n"
                  "31 30 0:27 / /sys/fs/cgroup/unified rw,nosuid shared:10 - cgroup2 cgroup2 rw,nsdelegate\n"
                  "32 30 0:28 / /sys/fs/cgroup/systemd rw shared:11 - cgroup cgroup rw,xattr,name=systemd\n")
        spaced = "40 24 0:31 / /mnt/my\\040cgroups rw shared:12 - cgroup2 none rw\n"
        self.assertEqual(cgroup2_mount(unified), Path("/sys/fs/cgroup"))
        self.assertEqual(cgroup2_mount(hybrid), Path("/sys/fs/cgroup/unified"))
        self.assertEqual(cgroup2_mount(spaced), Path("/mnt/my cgroups"))
        self.assertIsNone(cgroup2_mount("32 30 0:28 / /sys/fs/cgroup/systemd rw - cgroup cgroup rw\n"))
        self.assertIsNone(cgroup2_mount(""))

        unit = f"app-gnome-{DEFAULT_APP_ID}-12.scope"
        cgroup = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n"
        self.assertEqual(own_cgroup(cgroup, hybrid),
                         Path("/sys/fs/cgroup/unified/user.slice/user-1000.slice/user@1000.service/app.slice") / unit)
        self.assertIsNone(own_cgroup("1:name=systemd:/user.slice\n", hybrid))  # no v2 line
        self.assertIsNone(own_cgroup(cgroup, "32 30 0:28 / /sys/fs/cgroup rw - cgroup cgroup rw\n"))  # v1 only


SWAPS = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"


class ReclaimTest(Temp):
    def test_cheap_swap(self):
        zram = SWAPS + "/dev/zram0                              partition\t8388604\t\t1024\t\t100\n"
        disk = SWAPS + "/swap/swapfile                          file\t\t15728636\t6885280\t\t-1\n"
        self.assertTrue(cheap_swap(zram, "N\n"))
        self.assertTrue(cheap_swap(disk, "Y\n"))  # zswap compresses in front of the swap file
        self.assertFalse(cheap_swap(disk, "N\n"))
        self.assertFalse(cheap_swap(disk, ""))  # no zswap in this kernel
        self.assertFalse(cheap_swap(SWAPS, "Y\n"))  # zswap with nowhere to swap to
        self.assertFalse(cheap_swap("", ""))

    def test_can_reclaim(self):
        zram = SWAPS + "/dev/zram0 partition 8388604 0 100\n"
        unit = self.dir / scope_name(1)
        unit.mkdir()
        self.assertFalse(can_reclaim(unit, zram, "N"))  # no memory.reclaim: an older kernel
        (unit / "memory.reclaim").write_text("")
        self.assertTrue(can_reclaim(unit, zram, "N"))  # memory.swap.max unreadable counts as allowed
        (unit / "memory.swap.max").write_text("max\n")
        self.assertTrue(can_reclaim(unit, zram, "N"))
        self.assertFalse(can_reclaim(unit, SWAPS + "/swapfile file 1 0 -1\n", "N"))  # swap only on disk
        (unit / "memory.swap.max").write_text("0\n")
        self.assertFalse(can_reclaim(unit, zram, "N"))  # this unit may not swap at all
        foreign = self.dir / "vte-spawn-1.scope"
        foreign.mkdir()
        (foreign / "memory.reclaim").write_text("")
        self.assertFalse(can_reclaim(foreign, zram, "N"))  # not Natter's own: leave it alone
        self.assertFalse(can_reclaim(None, zram, "N"))

    def test_reclaimer_finds_its_unit_when_asked(self):
        terminal, unit = self.dir / "vte-spawn-1.scope", self.dir / scope_name(1)
        for folder in (terminal, unit):
            folder.mkdir()
            (folder / "memory.reclaim").write_text("")
            (folder / "memory.current").write_text("0\n")  # under the floor: a run ends at once
        reclaimer = Reclaimer()  # made while Natter is still in the terminal's unit
        with mock.patch.object(system, "cheap_swap", return_value=True):
            with mock.patch.object(system, "own_cgroup", return_value=terminal):
                self.assertFalse(reclaimer.available)
                self.assertFalse(reclaimer.start())
            with mock.patch.object(system, "own_cgroup", return_value=unit):  # the move to its scope finished
                self.assertTrue(reclaimer.available)
                self.assertTrue(reclaimer.start())
                reclaimer._thread.join(5)
        self.assertEqual(reclaimer.cgroup, unit)


class SandboxTest(Temp):
    def snap(self, yaml=None) -> str:
        """A snap's folder, as $SNAP names it, with `yaml` as its meta/snap.yaml (None: no file)."""
        folder = self.dir / "snap" / "app" / "12"
        (folder / "meta").mkdir(parents=True, exist_ok=True)
        if yaml is not None:
            (folder / "meta" / "snap.yaml").write_text(yaml)
        return str(folder)

    def test_confined_snap(self):
        self.assertTrue(confined_snap(self.snap("name: app\nversion: '1'\nconfinement: strict\n")))
        self.assertTrue(confined_snap(self.snap("name: app\nconfinement: devmode\n")))
        self.assertTrue(confined_snap(self.snap("name: app\n")))  # strict unless it says otherwise
        self.assertFalse(confined_snap(self.snap("name: code\nconfinement: classic\n")))  # not sandboxed
        self.assertFalse(confined_snap(self.snap("name: code\nconfinement: 'classic'\n")))

    def test_inherited_snap_variable(self):
        self.assertFalse(confined_snap(self.snap()))  # no snap.yaml: a removed or refreshed snap
        self.assertFalse(confined_snap(str(self.dir / "gone")))
        self.assertFalse(confined_snap(""))
        self.assertFalse(confined_snap(None))
        folder = self.snap()
        (Path(folder) / "meta" / "snap.yaml").mkdir()  # not a file
        self.assertFalse(confined_snap(folder))

    def test_in_sandbox(self):
        if Path("/.flatpak-info").exists():
            self.skipTest("runs in a Flatpak")
        strict, classic = self.snap("confinement: strict\n"), str(self.dir / "classic")
        (Path(classic) / "meta").mkdir(parents=True)
        (Path(classic) / "meta" / "snap.yaml").write_text("confinement: classic\n")
        with mock.patch.dict(os.environ, {"SNAP": strict}):
            self.assertTrue(in_sandbox())
        with mock.patch.dict(os.environ, {"SNAP": classic}):  # a terminal opened from a classic snap
            self.assertFalse(in_sandbox())
        with mock.patch.dict(os.environ):
            os.environ.pop("SNAP", None)
            self.assertFalse(in_sandbox())


class DistroTest(Temp):
    def setUp(self):
        super().setUp()
        # Whether this machine booted from an ostree image doesn't change what's tested.
        self.booted = self.dir / "ostree-booted"
        patch = mock.patch.object(distro, "OSTREE_BOOTED", self.booted)
        patch.start()
        self.addCleanup(patch.stop)

    def manager(self, text: str):
        return package_manager(os_release(text))

    def test_os_release(self):
        fields = os_release('# comment\nNAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'
                            "VERSION_ID='22'\nEMPTY=\nbroken line\n")
        self.assertEqual(fields["NAME"], "Linux Mint")
        self.assertEqual(fields["ID_LIKE"], "ubuntu debian")
        self.assertEqual(fields["VERSION_ID"], "22")
        self.assertEqual(fields["EMPTY"], "")

    def test_package_manager(self):
        self.assertEqual(self.manager("ID=ubuntu\nID_LIKE=debian\n"), "apt")
        self.assertEqual(self.manager('ID=linuxmint\nID_LIKE="ubuntu debian"\n'), "apt")
        self.assertEqual(self.manager("ID=debian\n"), "apt")
        self.assertEqual(self.manager("ID=fedora\n"), "dnf")
        self.assertEqual(self.manager('ID="rocky"\nID_LIKE="rhel centos fedora"\n'), "dnf")
        self.assertEqual(self.manager("ID=arch\n"), "pacman")
        self.assertEqual(self.manager("ID=manjaro\nID_LIKE=arch\n"), "pacman")
        self.assertEqual(self.manager('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n'), "zypper")
        self.assertEqual(self.manager('ID="sles"\nID_LIKE="suse"\n'), "zypper")
        self.assertIsNone(self.manager("ID=nixos\n"))
        self.assertIsNone(self.manager(""))

    def test_like_fedora_with_packages_of_their_own(self):
        # Their ID_LIKE says fedora, but dnf there doesn't know Fedora's package names.
        self.assertIsNone(self.manager('ID="mageia"\nID_LIKE="mandriva fedora"\n'))
        self.assertIsNone(self.manager('ID="openmandriva"\nID_LIKE="mandriva fedora"\n'))
        self.assertEqual(install_hint(DESKTOP, os_release('ID="mageia"\nID_LIKE="mandriva fedora"\n')), "")

    def test_image_based(self):
        silverblue = os_release('ID=fedora\nVARIANT_ID=silverblue\nVERSION_ID=42\n')
        self.assertTrue(image_based(silverblue))
        self.assertEqual(package_manager(silverblue), "rpm-ostree")
        self.assertEqual(install_hint(DESKTOP, silverblue), "rpm-ostree install " + DESKTOP["dnf"])
        for variant in ("kinoite", "sericea", "onyx"):
            self.assertEqual(self.manager(f"ID=fedora\nVARIANT_ID={variant}\n"), "rpm-ostree", variant)
        bazzite = os_release('ID=bazzite\nID_LIKE="fedora"\nVARIANT_ID=bazzite-gnome\n')
        self.assertTrue(image_based(bazzite))
        self.assertEqual(package_manager(bazzite), "rpm-ostree")
        steamos = os_release("ID=steamos\nID_LIKE=arch\nVARIANT_ID=steamdeck\n")
        self.assertTrue(image_based(steamos))
        self.assertIsNone(package_manager(steamos))  # pacman can't change SteamOS's image
        self.assertEqual(install_hint(MEDIA, steamos), "")
        workstation = os_release("ID=fedora\nVARIANT_ID=workstation\n")
        self.assertFalse(image_based(workstation))
        self.assertEqual(package_manager(workstation), "dnf")

    def test_ostree_booted(self):
        self.booted.write_text("")
        self.assertTrue(image_based(os_release("ID=fedora\nVARIANT_ID=cosmic-atomic\n")))
        self.assertEqual(self.manager("ID=fedora\nVARIANT_ID=cosmic-atomic\n"), "rpm-ostree")
        self.assertIsNone(self.manager("ID=endless\nID_LIKE=debian\n"))  # apt can't change the image

    def test_hints(self):
        ubuntu = os_release("ID=ubuntu\nID_LIKE=debian\n")
        self.assertEqual(install_hint(DESKTOP, ubuntu), "sudo apt install " + DESKTOP["apt"])
        self.assertIn("gir1.2-webkit-6.0", install_hint(DESKTOP, ubuntu))
        self.assertIn("gstreamer1.0-libav", install_hint(MEDIA, ubuntu))
        self.assertTrue(install_hint(MEDIA, os_release("ID=fedora\n")).startswith("sudo dnf install "))
        self.assertEqual(install_hint(DESKTOP, os_release("ID=nixos\n")), "")
        self.assertEqual(set(DESKTOP), set(MEDIA))
        self.assertEqual(set(DESKTOP), set(GST_PYTHON))
        # GStreamer for Python comes with the rest, so the codec check can run.
        self.assertIn(GST_PYTHON["apt"], DESKTOP["apt"].split())
        self.assertIn(GST_PYTHON["zypper"], DESKTOP["zypper"].split())


class MediaTest(unittest.TestCase):
    """media_report, and what it says when it can't ask GStreamer."""

    def setUp(self):
        for patch in (mock.patch.object(system, "in_sandbox", return_value=False),
                      mock.patch.object(distro, "OSTREE_BOOTED", Path("/nonexistent/ostree-booted"))):
            patch.start()
            self.addCleanup(patch.stop)

    def without_gst(self):
        """GStreamer for Python not installed: its typelib can't be found."""
        import gi

        require = gi.require_version

        def require_version(namespace, version):
            if namespace == "Gst":
                raise ValueError("Namespace Gst not available")
            return require(namespace, version)

        return mock.patch.object(gi, "require_version", require_version)

    def test_unchecked(self):
        with self.without_gst():
            for text, package in [("ID=ubuntu\nID_LIKE=debian\n", "gir1.2-gstreamer-1.0"),
                                  ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "typelib-1_0-Gst-1_0"),
                                  ("ID=fedora\n", "gstreamer1"),
                                  ("ID=arch\n", "gstreamer")]:
                missing, hint = media_report(os_release(text))
                self.assertEqual(missing, [], text)  # nothing claimed missing: it wasn't checked
                self.assertTrue(hint.startswith("Couldn't check for codecs"), hint)
                self.assertTrue(hint.endswith(" " + package), hint)
            missing, hint = media_report(os_release("ID=nixos\n"))
            self.assertEqual(missing, [])
            self.assertTrue(hint.startswith("Couldn't check for codecs"), hint)
            self.assertIn("Gst 1.0", hint)

    def test_unchecked_in_sandbox(self):
        with self.without_gst(), mock.patch.object(system, "in_sandbox", return_value=True):
            missing, hint = media_report(os_release("ID=ubuntu\n"))
        self.assertEqual(missing, [])
        self.assertNotIn("sudo", hint)
        self.assertTrue(hint.startswith("Couldn't check for codecs"), hint)

    def test_checked(self):
        import gi

        try:
            gi.require_version("Gst", "1.0")
        except ValueError:
            self.skipTest("needs GStreamer for Python")
        ubuntu = os_release("ID=ubuntu\nID_LIKE=debian\n")
        missing, hint = media_report(ubuntu)
        self.assertLessEqual(set(missing), {label for label, _ in system.CODECS})
        self.assertEqual(hint, "sudo apt install " + MEDIA["apt"] if missing else "")


class VersionGateTest(unittest.TestCase):
    def test_floors(self):
        self.assertEqual(FLOORS, {"PyGObject": (3, 42), "GTK": (4, 14), "libadwaita": (1, 5), "WebKitGTK": (2, 42)})

    def test_comparisons(self):
        self.assertEqual(too_old({"GTK": (4, 14, 0), "libadwaita": (1, 5, 0),
                                  "WebKitGTK": (2, 42, 0), "PyGObject": (3, 42, 0)}), [])  # exactly the floor
        self.assertEqual(too_old({"GTK": (4, 22, 4), "libadwaita": (1, 9, 1), "WebKitGTK": (2, 52, 6)}), [])
        self.assertEqual(too_old({"GTK": (5, 0, 0)}), [])  # a newer major with a lower minor
        self.assertEqual(too_old({"GTK": (4, 12, 4)}), [f"GTK is 4.12.4; {NAME} needs 4.14 or newer"])
        self.assertEqual(len(too_old({"WebKitGTK": (2, 41, 99), "PyGObject": (3, 40, 1),
                                      "libadwaita": (1, 4, 0)})), 3)

    def explained(self, release: str) -> str:
        """What Natter says at start-up on the distribution `release` describes, GTK missing."""
        said = io.StringIO()
        with mock.patch.object(distro, "os_release", return_value=os_release(release)), \
                mock.patch.object(distro, "OSTREE_BOOTED", Path("/nonexistent/ostree-booted")), \
                contextlib.redirect_stderr(said):
            explain(["GTK 4 for Python isn't installed"])
        return said.getvalue()

    def test_explain(self):
        ubuntu = self.explained("ID=ubuntu\nID_LIKE=debian\n")
        self.assertIn("sudo apt install python3-gi", ubuntu)
        self.assertIn("too old", ubuntu)
        silverblue = self.explained("ID=fedora\nVARIANT_ID=silverblue\n")
        self.assertIn(f"Flatpak ({DEFAULT_APP_ID})", silverblue)
        self.assertIn("rpm-ostree install python3-gobject", silverblue)
        self.assertNotIn("sudo", silverblue)
        for release in ("ID=steamos\nID_LIKE=arch\n", 'ID=mageia\nID_LIKE="mandriva fedora"\n'):
            said = self.explained(release)
            self.assertIn(f"Flatpak ({DEFAULT_APP_ID})", said)
            self.assertNotIn("install python", said)  # no package names to give


def die_with_parent() -> None:
    """In a child process: be stopped when the test run is, even if it's killed outright."""
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class PortalTest(unittest.TestCase):
    """request_background against a stand-in portal on a private bus (never the session's)."""

    PORTAL_XML = """
    <node>
      <interface name="org.freedesktop.portal.Background">
        <method name="RequestBackground">
          <arg type="s" name="parent_window" direction="in"/>
          <arg type="a{sv}" name="options" direction="in"/>
          <arg type="o" name="handle" direction="out"/>
        </method>
        <property name="version" type="u" access="read"/>
      </interface>
      <interface name="org.freedesktop.portal.Settings">
        <property name="version" type="u" access="read"/>
      </interface>
    </node>
    """

    def setUp(self):
        if not shutil.which("dbus-daemon"):
            self.skipTest("needs dbus-daemon")
        from gi.repository import Gio, GLib

        # PyGObject asks asyncio for its loop policy on every iteration, deprecated in Python 3.14:
        # quiet, here and in the stand-in portal's thread, for the whole test.
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)

        self.Gio, self.GLib = Gio, GLib
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = Path(folder.name) / "bus.conf"
        config.write_text(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
         "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
        <busconfig>
          <type>session</type>
          <listen>unix:dir={folder.name}</listen>
          <auth>EXTERNAL</auth>
          <policy context="default">
            <allow send_destination="*" eavesdrop="true"/>
            <allow eavesdrop="true"/>
            <allow own="*"/>
          </policy>
        </busconfig>""")
        daemon = subprocess.Popen(["dbus-daemon", f"--config-file={config}", "--nofork", "--nopidfile",
                                   "--print-address=1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  preexec_fn=die_with_parent)
        self.addCleanup(daemon.stdout.close)
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.terminate)
        address = daemon.stdout.readline().strip()
        self.client = self.connect(address)
        self.portal = self.connect(address)
        self.asked = []

    def wait(self, until, seconds: float = 5) -> None:
        """Run GLib's main context until `until()` is true, for `seconds` at most."""
        context, deadline = self.GLib.MainContext.default(), time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.005)

    def connect(self, address: str):
        """A connection to the test bus, or a failed test after 5 s (never a hang)."""
        Gio, got = self.Gio, []
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        Gio.DBusConnection.new_for_address(address, flags, None, None, lambda _source, result: got.append(
            Gio.DBusConnection.new_for_address_finish(result)))
        self.wait(lambda: got)
        if not got:
            self.fail(f"couldn't connect to the test bus at {address!r}")
        self.addCleanup(got[0].close_sync, None)
        return got[0]

    def serve(self, response: int, background: bool, old: bool = False,
              interface: str = "org.freedesktop.portal.Background") -> None:
        """Be the portal, offering `interface`: answer RequestBackground with `response` on the
        request's path. It answers from a thread of its own, the way a real portal answers from
        its own process, since Natter asks whether it offers Background synchronously."""
        Gio, GLib = self.Gio, self.GLib
        info = Gio.DBusNodeInfo.new_for_xml(self.PORTAL_XML).lookup_interface(interface)

        def on_call(conn, sender, _path, _iface, _method, params, invocation):
            window, options = params.unpack()
            self.asked.append(options)
            caller = sender.lstrip(":").replace(".", "_")
            token = "other" if old else options["handle_token"]
            request = f"/org/freedesktop/portal/desktop/request/{caller}/{token}"
            invocation.return_value(GLib.Variant("(o)", (request,)))
            if old:
                time.sleep(0.2)  # an old portal can only be heard once its reply has arrived
            results = {"background": GLib.Variant("b", background), "autostart": GLib.Variant("b", background)}
            conn.emit_signal(None, request, "org.freedesktop.portal.Request", "Response",
                             GLib.Variant("(ua{sv})", (response, results)))

        context, stop, registered = GLib.MainContext.new(), threading.Event(), []

        def run():
            context.push_thread_default()
            # register_object_with_closures2 is GLib 2.84's name for it.
            register = getattr(self.portal, "register_object_with_closures2", None) or self.portal.register_object
            registered.append(register("/org/freedesktop/portal/desktop", info, on_call,
                                       lambda *_: GLib.Variant("u", 1), None))
            while not stop.is_set():
                context.iteration(True)
            self.portal.unregister_object(registered[0])
            context.pop_thread_default()

        thread = threading.Thread(target=run, name="test-portal", daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(context.wakeup)
        self.addCleanup(stop.set)
        self.wait(lambda: registered)
        reply = self.portal.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                      "RequestName", GLib.Variant("(su)", ("org.freedesktop.portal.Desktop", 4)),
                                      None, Gio.DBusCallFlags.NONE, 1000, None)
        self.assertEqual(reply.unpack()[0], 1)

    def ask(self, autostart: bool = True) -> list:
        from natter.system import request_background

        answers = []
        started = request_background("", autostart, "Messages arrive while the window is closed",
                                     answers.append, bus=self.client)
        if started:
            self.wait(lambda: answers)
        return [started] + answers

    def test_no_portal(self):
        self.assertEqual(self.ask(), [False])

    def test_portal_without_background(self):
        # xdg-desktop-portal runs, but none of the desktop's backends offers Background.
        self.serve(0, True, interface="org.freedesktop.portal.Settings")
        self.assertEqual(self.ask(), [False])
        self.assertEqual(self.asked, [])  # never asked

    def test_allowed(self):
        self.serve(0, True)
        self.assertEqual(self.ask(), [True, True])
        options = self.asked[0]
        self.assertTrue(options["autostart"])
        self.assertEqual(options["commandline"], ["natter", "--background"])
        self.assertEqual(options["reason"], "Messages arrive while the window is closed")
        self.assertTrue(re.fullmatch(r"natter_[0-9a-f]{32}", options["handle_token"]))

    def test_refused(self):
        self.serve(0, False)
        self.assertEqual(self.ask(), [True, False])

    def test_cancelled(self):
        self.serve(1, True)
        self.assertEqual(self.ask(False), [True, False])

    def test_portal_without_handle_token(self):
        self.serve(0, True, old=True)
        self.assertEqual(self.ask(), [True, True])


class PackagingTest(unittest.TestCase):
    """The files that install Natter agree with each other and with the code."""

    def test_data_files_carry_the_app_id(self):
        for name in [f"icons/hicolor/scalable/apps/{DEFAULT_APP_ID}.svg",
                     f"icons/hicolor/scalable/apps/{DEFAULT_APP_ID}-unread.svg",
                     f"icons/hicolor/symbolic/apps/{DEFAULT_APP_ID}-symbolic.svg",
                     f"icons/hicolor/symbolic/apps/{DEFAULT_APP_ID}-unread-symbolic.svg",
                     f"{DEFAULT_APP_ID}.search-provider.ini", f"{DEFAULT_APP_ID}.service.in",
                     f"{DEFAULT_APP_ID}.metainfo.xml"]:
            self.assertTrue((DATA / name).is_file(), name)
        self.assertFalse(list(DATA.rglob(f"{OLD_APP_ID}*")))

    def test_search_provider(self):
        ini = (DATA / f"{DEFAULT_APP_ID}.search-provider.ini").read_text()
        self.assertIn(f"DesktopId={DEFAULT_APP_ID}.desktop\n", ini)
        self.assertIn(f"BusName={DEFAULT_APP_ID}\n", ini)
        self.assertIn("ObjectPath=/" + DEFAULT_APP_ID.replace(".", "/") + "/SearchProvider\n", ini)
        self.assertIn("AutoStart=false\n", ini)
        self.assertIn(f"Name={DEFAULT_APP_ID}\n", (DATA / f"{DEFAULT_APP_ID}.service.in").read_text())

    def test_desktop_entry(self):
        entry = (ROOT / "natter.desktop.in").read_text()
        self.assertIn(f"Icon={DEFAULT_APP_ID}\n", entry)
        self.assertIn(f"StartupWMClass={DEFAULT_APP_ID}\n", entry)
        self.assertIn("Exec=@EXEC@ %U\n", entry)

    def test_versions_agree(self):
        metainfo = ElementTree.parse(DATA / f"{DEFAULT_APP_ID}.metainfo.xml").getroot()
        self.assertEqual(metainfo.findtext("id"), DEFAULT_APP_ID)
        self.assertEqual(metainfo.findtext("launchable"), f"{DEFAULT_APP_ID}.desktop")
        self.assertEqual(metainfo.find("releases/release").get("version"), VERSION)
        meson = (ROOT / "meson.build").read_text()
        self.assertIn(f"version: '{VERSION}'", meson)
        self.assertIn(f"app_id = '{DEFAULT_APP_ID}'", meson)

    def test_names_agree(self):
        """The name the menu, the software centre and the installer show is the window's."""
        self.assertIn(f"\nName={NAME}\n", (ROOT / "natter.desktop.in").read_text())
        metainfo = ElementTree.parse(DATA / f"{DEFAULT_APP_ID}.metainfo.xml").getroot()
        self.assertEqual(metainfo.findtext("name"), NAME)
        self.assertIn(f'\nAPP_NAME="{NAME}" ', (ROOT / "install.sh").read_text())

    def test_installers_agree(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn(f"\nAPP_ID={DEFAULT_APP_ID}\n", install)
        self.assertIn(f"\nOLD_APP_ID={OLD_APP_ID} ", install)
        launcher = (ROOT / "bin" / "natter").read_text().splitlines()
        self.assertTrue(launcher[0].startswith("#!"))
        self.assertEqual(sum(line.startswith('NATTER_DIR = ""') for line in launcher), 1)  # what both rewrite

    def test_flatpak_manifest(self):
        manifest = (ROOT / "packaging" / f"{DEFAULT_APP_ID}.yml").read_text()
        self.assertIn(f"\nid: {DEFAULT_APP_ID}\n", manifest)
        self.assertIn("\nruntime: org.gnome.Platform\nruntime-version: '50'\n", manifest)
        args = re.findall(r"^\s*- (--\S+)", manifest, re.M)
        self.assertIn("--talk-name=org.kde.StatusNotifierWatcher", args)
        # Sandboxed, notify.py uses GNotification (the portal), never the server directly.
        self.assertNotIn("--talk-name=org.freedesktop.Notifications", args)
        self.assertFalse([a for a in args if a.startswith("--own-name") or "com.canonical.Unity" in a])


if __name__ == "__main__":
    unittest.main()
