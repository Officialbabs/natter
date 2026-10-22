"""install.sh installing what Natter needs: the packages each distribution calls it by
(natter/distro.py), what's missing here (natter/__main__.py), and install.sh itself, run in a
scratch home with stand-ins for sudo and the package managers that only log what they're asked
and "install" by taking names out of the test-only NATTER_TEST_MISSING file. Nothing real is
installed, and nothing runs as root."""

import contextlib
import fcntl
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from natter import NAME, distro
from natter.__main__ import CALL_ELEMENTS, TYPELIBS, check, missing_pieces, problems
from natter.distro import (AUTO, DESKTOP, GST_PYTHON, MEDIA, OPTIONAL, PIECE, PIECES, RECOMMENDED, REQUIRED,
                           known_packages, os_release, pretend_missing, report, too_old_release)

ROOT = Path(__file__).resolve().parent.parent

# /etc/os-release as each distribution writes it (trimmed to what Natter reads).
RELEASES = {
    "ubuntu-24.04": 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\n'
                    "VERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\n",
    "ubuntu-22.04": 'NAME="Ubuntu"\nVERSION_ID="22.04"\nID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 22.04.5 LTS"\n'
                    "VERSION_CODENAME=jammy\nUBUNTU_CODENAME=jammy\n",
    "debian-13": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\n'
                 "VERSION_CODENAME=trixie\nID=debian\n",
    "fedora-41": 'NAME="Fedora Linux"\nVERSION_ID=41\nID=fedora\nPRETTY_NAME="Fedora Linux 41 (Workstation Edition)"\n'
                 "VARIANT_ID=workstation\n",
    "arch": 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\n',
    "tumbleweed": 'NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n'
                  'VERSION_ID="20261020"\nPRETTY_NAME="openSUSE Tumbleweed"\n',
    "unknown": 'NAME="NixOS"\nID=nixos\nVERSION_ID="25.05"\nPRETTY_NAME="NixOS 25.05 (Warbler)"\n',
}
MANAGERS = {"ubuntu-24.04": "apt", "debian-13": "apt", "fedora-41": "dnf", "arch": "pacman", "tumbleweed": "zypper"}


def release(name: str) -> dict:
    return os_release(RELEASES[name])


def knows_all(command):
    """A package manager whose lists have every package asked about."""
    return answer(command, set())


def answer(command, unavailable):
    """What apt-cache, dnf, pacman or zypper say about the packages `command` asks for, all of
    them known but `unavailable`."""
    asked = [word for word in command[1:] if not word.startswith("-") and word not in
             ("policy", "repoquery", "%{name}\n", "search", "package")]
    known = [name for name in asked if name not in unavailable]
    program = command[0]
    if program == "apt-cache":
        if not asked:
            return " 100 /var/lib/dpkg/status\n 500 http://archive.example/ubuntu noble/main amd64 Packages\n"
        return "".join(f"{name}:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n" for name in known)
    if program == "dnf":
        return "".join(f"{name}\n" for name in known)
    if program == "pacman":
        return "".join(f"Repository      : extra\nName            : {name}\nVersion         : 1.0\n\n" for name in known)
    if program == "zypper":
        return "".join(f"S | Name | Type | Version | Arch | Repository\n--+------+------+--\n"
                       f"  | {name}-x | package | 1.0 | x86_64 | oss\n" for name in known)
    raise AssertionError(command)


def lines(found: list) -> dict:
    """report()'s lines, by their first word."""
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in found}


class PackagesTest(unittest.TestCase):
    """Which packages each distribution's package manager installs for what's missing."""

    def setUp(self):
        patch = mock.patch.object(distro, "OSTREE_BOOTED", Path("/nonexistent/ostree-booted"))
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_piece_named_everywhere(self):
        self.assertEqual(len(PIECE), len(PIECES))
        for piece in PIECES:
            self.assertIn(piece.group, (REQUIRED, RECOMMENDED, OPTIONAL))
            self.assertEqual(set(piece.packages), set(AUTO), piece.name)
            for manager, names in piece.packages.items():
                self.assertTrue(names.strip(), (piece.name, manager))
                self.assertNotRegex(names, r"[^A-Za-z0-9 _.+-]", (piece.name, manager))  # shell-safe words

    def test_required_is_what_the_start_up_check_needs(self):
        required = {piece.name for piece in PIECES if piece.group == REQUIRED}
        from natter.__main__ import LIBRARIES

        self.assertLessEqual({namespace for namespace, _, _ in LIBRARIES} | {"gi"}, required)
        self.assertIn("Gst", required)  # GStreamer for Python: the codec check and calls
        self.assertLessEqual(set(TYPELIBS), set(PIECE))
        self.assertLessEqual(set(CALL_ELEMENTS), {piece.name for piece in PIECES if piece.group == RECOMMENDED})
        self.assertEqual({piece.name for piece in PIECES if piece.group == OPTIONAL}, {"cc", "pkg-config", "gst-dev"})

    def test_calls_need_what_rtc_checks_for(self):
        source = (ROOT / "natter" / "rtc.py").read_text()
        available = re.search(r"def available\(\).*?return all\(.*?for name in \(([^)]*)\)\)", source, re.S)
        self.assertIsNotNone(available)
        wanted = set(re.findall(r'"([^"]+)"', available[1]))
        self.assertEqual(wanted, {name for names in CALL_ELEMENTS.values() for name in names})
        for namespace in ("GstSdp", "GstWebRTC"):
            self.assertIn(f'gi.require_version("{namespace}", "1.0")', source)
            self.assertEqual(PIECE[namespace].group, RECOMMENDED)

    def test_old_tables_follow_the_pieces(self):
        self.assertEqual(DESKTOP["apt"], "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 gir1.2-soup-3.0 "
                                         "gir1.2-gdkpixbuf-2.0 gir1.2-gstreamer-1.0")
        self.assertEqual(MEDIA, {manager: PIECE["codecs"].packages[manager] for manager in AUTO})
        self.assertEqual(GST_PYTHON["dnf"], "gstreamer1")

    def plan(self, name, missing, optional=True, old=(), ask=knows_all):
        return lines(report(release(name), set(missing), list(old), optional=optional, ask=ask))

    def test_ubuntu_24_04(self):
        found = self.plan("ubuntu-24.04", {"gi", "WebKit", "codecs", "GstSdp", "cc", "gst-dev"})
        self.assertEqual(found["manager"], "apt")
        self.assertEqual(found["required"], "python3-gi gir1.2-webkit-6.0")
        self.assertEqual(found["recommended"],
                         "gstreamer1.0-libav gstreamer1.0-plugins-good gstreamer1.0-plugins-base gir1.2-gst-plugins-base-1.0")
        self.assertEqual(found["optional"], "gcc libc6-dev libgstreamer1.0-dev")
        self.assertIn("WebKitGTK 6.0", found["required-words"])
        self.assertNotIn("too-old", found)

    def test_debian_13(self):
        found = self.plan("debian-13", {"Adw", "webrtc", "nice", "GstWebRTC", "pkg-config"})
        self.assertEqual(found["manager"], "apt")
        self.assertEqual(found["required"], "gir1.2-adw-1")
        self.assertEqual(found["recommended"], "gstreamer1.0-plugins-bad gstreamer1.0-nice gir1.2-gst-plugins-bad-1.0")
        self.assertEqual(found["optional"], "pkg-config")

    def test_fedora_41(self):
        found = self.plan("fedora-41", {"gi", "Gtk", "codecs", "nice", "cc", "pkg-config", "gst-dev"})
        self.assertEqual(found["manager"], "dnf")
        self.assertEqual(found["required"], "python3-gobject gtk4")
        self.assertEqual(found["recommended"],
                         "gstreamer1-plugin-openh264 gstreamer1-plugin-libav gstreamer1-plugins-good libnice-gstreamer1")
        self.assertEqual(found["optional"], "gcc pkgconf-pkg-config gstreamer1-devel")

    def test_arch(self):
        found = self.plan("arch", {"WebKit", "Gst", "webrtc", "GstWebRTC", "GstSdp", "nice", "gst-dev"})
        self.assertEqual(found["manager"], "pacman")
        self.assertEqual(found["required"], "webkitgtk-6.0 gstreamer")
        self.assertEqual(found["recommended"], "gst-plugins-bad libnice gst-plugins-bad-libs gst-plugins-base-libs")
        self.assertEqual(found["optional"], "gstreamer")  # GStreamer's headers come with it there

    def test_tumbleweed(self):
        found = self.plan("tumbleweed", {"gi", "Gtk", "Soup", "codecs", "nice", "cc"})
        self.assertEqual(found["manager"], "zypper")
        self.assertEqual(found["required"], "python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Soup-3_0")
        self.assertEqual(found["recommended"],
                         "gstreamer-plugin-openh264 gstreamer-plugins-libav gstreamer-plugins-good gstreamer-libnice")
        self.assertEqual(found["optional"], "gcc")

    def test_ubuntu_22_04_is_too_old(self):
        # It has packages by these names (gir1.2-webkit-6.0 too, in jammy-updates), but GTK 4.6
        # and libadwaita 1.1: installing them wouldn't make Natter run.
        found = self.plan("ubuntu-22.04", {"WebKit", "codecs"})
        self.assertIn("Ubuntu 22.04.5 LTS has a GTK, libadwaita or WebKitGTK older than", found["too-old"])
        self.assertNotIn("manager", found)
        self.assertNotIn("required", found)
        # Its packages already there, but too old: the version check says so.
        found = self.plan("ubuntu-22.04", set(), old=[f"GTK is 4.6.9; {NAME} needs 4.14 or newer"])
        self.assertEqual(found["too-old"], f"GTK is 4.6.9; {NAME} needs 4.14 or newer")
        # Nothing required missing: no reason to say it.
        self.assertNotIn("too-old", self.plan("ubuntu-22.04", {"codecs"}))

    def test_too_old_releases(self):
        mint21 = os_release('NAME="Linux Mint"\nVERSION_ID="21.3"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'
                            'PRETTY_NAME="Linux Mint 21.3"\nUBUNTU_CODENAME=jammy\n')
        self.assertIn("built on Ubuntu 22.04", too_old_release(mint21))
        lmde6 = os_release('PRETTY_NAME="LMDE 6 (faye)"\nID=linuxmint\nID_LIKE=debian\nDEBIAN_CODENAME=bookworm\n')
        self.assertIn("built on Debian 12", too_old_release(lmde6))
        self.assertTrue(too_old_release(os_release('ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n')))
        self.assertTrue(too_old_release(os_release("ID=fedora\nVERSION_ID=39\n")))
        self.assertTrue(too_old_release(os_release('ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"\n')))
        self.assertTrue(too_old_release(os_release('ID="opensuse-leap"\nVERSION_ID="15.6"\n')))
        for name in ("ubuntu-24.04", "debian-13", "fedora-41", "arch", "tumbleweed", "unknown"):
            self.assertEqual(too_old_release(release(name)), "", name)
        self.assertEqual(too_old_release(os_release('ID=debian\nVERSION_CODENAME=forky\n')), "")  # testing: no VERSION_ID
        mint22 = os_release('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\nUBUNTU_CODENAME=noble\n')
        self.assertEqual(too_old_release(mint22), "")

    def test_unknown_distribution(self):
        found = self.plan("unknown", {"Gtk", "codecs", "cc"})
        self.assertNotIn("manager", found)
        self.assertNotIn("required", found)
        self.assertEqual(found["required-words"], "GTK 4 for Python")
        self.assertIn("H.264", found["recommended-words"])
        self.assertEqual(found["optional-words"], "a C compiler")

    def test_image_based_installs_nothing(self):
        silverblue = os_release("ID=fedora\nVARIANT_ID=silverblue\nVERSION_ID=42\n")
        found = lines(report(silverblue, {"Gtk"}, ask=knows_all))
        self.assertNotIn("manager", found)  # rpm-ostree: explain() offers it, install.sh doesn't run it

    def test_optional_only_where_the_camera_needs_the_fix(self):
        found = self.plan("ubuntu-24.04", {"cc", "codecs"}, optional=False)
        self.assertNotIn("optional", found)
        self.assertNotIn("optional-words", found)
        self.assertIn("recommended", found)

    def test_nothing_missing(self):
        self.assertEqual(self.plan("ubuntu-24.04", set()), {"manager": "apt"})

    def test_unavailable_recommended_are_left_out(self):
        def ask(command):
            return answer(command, {"gstreamer1-plugin-openh264"})

        found = self.plan("fedora-41", {"codecs", "Gtk"}, ask=ask)
        self.assertEqual(found["required"], "gtk4")
        self.assertEqual(found["recommended"], "gstreamer1-plugin-libav gstreamer1-plugins-good")
        self.assertIn("gstreamer1-plugin-openh264", found["note"])
        self.assertIn("RPM Fusion", found["note"])
        found = self.plan("ubuntu-24.04", {"nice"}, ask=lambda command: answer(command, {"gstreamer1.0-nice"}))
        self.assertNotIn("recommended", found)
        self.assertNotIn("RPM Fusion", found["note"])

    def test_unavailable_required_means_too_old(self):
        found = self.plan("debian-13", {"WebKit", "codecs"}, ask=lambda command: answer(command, {"gir1.2-webkit-6.0"}))
        self.assertEqual(found["too-old"], "Debian GNU/Linux 13 (trixie) has no gir1.2-webkit-6.0")
        self.assertNotIn("manager", found)

    def test_lists_it_cant_read(self):
        """Without apt's lists (or any answer), every package is tried: nothing's called too old."""
        def no_lists(command):
            return " 100 /var/lib/dpkg/status\n" if command == ["apt-cache", "policy"] else ""

        found = self.plan("ubuntu-24.04", {"WebKit"}, ask=no_lists)
        self.assertEqual(found["required"], "gir1.2-webkit-6.0")
        found = self.plan("fedora-41", {"WebKit"}, ask=lambda command: None)
        self.assertEqual(found["required"], "webkitgtk6.0")

    def test_known_packages(self):
        asked = []

        def ask(command):
            asked.append(command)
            return answer(command, {"b"})

        for manager in AUTO:
            self.assertEqual(known_packages(manager, ["a", "b"], ask), {"a"}, manager)
        self.assertEqual(known_packages("apt", [], ask), set())
        self.assertIsNone(known_packages("rpm-ostree", ["a"], ask))
        self.assertIn(["dnf", "-q", "-C", "repoquery", "--qf", "%{name}\n", "a", "b", "bash"], asked)  # cache only
        self.assertTrue(all("--no-refresh" in command for command in asked if command[0] == "zypper"))
        # apt: known without a version to install doesn't count.
        policy = "a:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\nb:\n  Installed: (none)\n  Candidate: 2\n"
        self.assertEqual(known_packages("apt", ["a", "b"], lambda command: policy if len(command) > 2 else
                                        " 500 http://x y/main amd64 Packages\n"), {"b"})
        # Not even the package manager itself is in its lists: they couldn't be read.
        self.assertIsNone(known_packages("zypper", ["a"], lambda command: "No matching items found.\n"))
        self.assertIsNone(known_packages("pacman", ["a"], lambda command: "error: failed to initialize alpm library\n"))
        self.assertIsNone(known_packages("dnf", ["a"], lambda command: ""))


class HookTest(unittest.TestCase):
    """NATTER_TEST_MISSING: test-only, and it can only add to what's missing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name, "missing")
        self.addCleanup(self.tmp.cleanup)

    def pretend(self, names: str):
        self.file.write_text(names)
        return mock.patch.dict(os.environ, {distro.TEST_MISSING: str(self.file)})

    def test_off_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(distro.TEST_MISSING, None)
            self.assertEqual(pretend_missing(), set())
        self.assertEqual(pretend_missing({distro.TEST_MISSING: str(self.file)}), set())  # no such file yet

    def test_adds_problems(self):
        real = problems()
        with self.pretend("Adw\nWebKit"):
            self.assertEqual(pretend_missing(), {"Adw", "WebKit"})
            said = problems()
            self.assertIn("libadwaita for Python isn't installed", said)
            self.assertIn("WebKitGTK 6.0 for Python isn't installed", said)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(check(), 1)
        with self.pretend("gi"):
            self.assertEqual(problems(), ["PyGObject (GTK for Python) isn't installed"])
        with self.pretend(""):
            self.assertEqual(problems(), real)  # nothing named: nothing hidden or added

    def test_missing_pieces(self):
        real = missing_pieces()
        with self.pretend("gi codecs"):
            found = missing_pieces()
        self.assertLessEqual(real, found)  # never hides what's really missing
        self.assertLessEqual({"gi", "codecs", *TYPELIBS, *CALL_ELEMENTS}, found)  # without PyGObject: unknown, so missing

    def test_missing_pieces_without_a_compiler(self):
        found = missing_pieces({"PATH": "/nonexistent", "CC": "ccache gcc"})
        self.assertLessEqual({"cc", "pkg-config", "gst-dev"}, found)


# ---- install.sh, end to end -------------------------------------------------------------------

FAKE_MANAGER = r'''
"""Stand-in for apt-get, apt-cache, dnf, pacman and zypper: logs its arguments, answers queries
from FAKE_UNAVAILABLE, and "installs" by taking the pieces whose packages it got out of the
NATTER_TEST_MISSING file. Never runs anything real."""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from natter.distro import PIECES
from tests.test_install_deps import answer

program, args = Path(sys.argv[2]).name, sys.argv[3:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join([program, *args]) + "\n")
unavailable = set(os.environ.get("FAKE_UNAVAILABLE", "").split())
if program == "apt-cache" or "repoquery" in args or "-Si" in args or "search" in args:
    sys.stdout.write(answer([program, *args], unavailable))
    sys.exit(0)
if os.environ.get("FAKE_FAIL"):
    sys.exit(100)
manager = {"apt-get": "apt"}.get(program, program)
got = set(args)
if got & unavailable:
    print(f"E: Unable to locate package {sorted(got & unavailable)[0]}")
    sys.exit(100)
missing = Path(os.environ["NATTER_TEST_MISSING"])
left = [name for name in missing.read_text().split()
        if not set(next(p for p in PIECES if p.name == name).packages[manager].split()) <= got]
missing.write_text("\n".join(left))
'''

# Never on the scripts' PATH: real package managers, ways to root, and what would reach a
# desktop session (install.sh's own D-Bus and xdg calls get stand-ins).
KEPT_OUT = {"sudo", "su", "doas", "pkexec", "apt", "apt-get", "apt-cache", "aptitude", "dpkg", "dnf", "dnf5", "yum",
            "rpm", "rpm-ostree", "pacman", "yay", "paru", "zypper", "flatpak", "dbus-daemon", "dbus-launch",
            "dbus-run-session", "at-spi-bus-launcher", "Xvfb", "gdbus", "xdg-mime", "update-desktop-database",
            "gtk-update-icon-cache", "cc", "gcc"}


def die_with_parent() -> None:
    import ctypes

    ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


@unittest.skipUnless(os.path.exists("/usr/bin/python3") and sys.platform == "linux", "needs /usr/bin/python3")
class InstallScriptTest(unittest.TestCase):
    """install.sh, run whole in a scratch home, with stand-ins on PATH."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        # Everything in /usr/bin but KEPT_OUT, linked into one folder that stands for PATH.
        cls.system = base / "system-bin"
        cls.system.mkdir()
        for folder in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                target = cls.system / entry
                if entry not in KEPT_OUT and not target.exists():
                    target.symlink_to(Path(folder, entry))
        cls.helper = base / "fake_manager.py"
        cls.helper.write_text(FAKE_MANAGER)
        cls.registry = base / "gst-registry.bin"  # one GStreamer registry for every run: built once
        # Natter must run here for real: the stand-ins only take away what the tests pretend is missing.
        if subprocess.run(["/usr/bin/python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                           "from natter.__main__ import check; sys.exit(check())", str(ROOT)],
                          capture_output=True, timeout=120).returncode != 0:
            cls.tmp.cleanup()
            raise unittest.SkipTest("Natter doesn't run with /usr/bin/python3 here")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.home = self.dir / "home"
        self.home.mkdir()
        self.fake = self.dir / "fake-bin"
        self.fake.mkdir()
        self.log = self.dir / "log"
        self.log.write_text("")
        self.missing = self.dir / "missing"
        for name in ("apt-get", "apt-cache", "dnf", "pacman", "zypper"):
            self.script(name, f'exec "{sys.executable}" "{self.helper}" "{ROOT}" "$0" "$@"')
        self.script("sudo", 'echo "sudo $*" >> "$FAKE_LOG"\nexec "$@"')
        for name in ("gdbus", "xdg-mime", "update-desktop-database", "gtk-update-icon-cache"):
            self.script(name, "exit 1" if name == "gdbus" else "exit 0")  # no session bus: nothing's running
        # A compiler that only makes the file it's asked for: the camera fix "builds" in no time.
        self.script("cc", 'while [ $# -gt 0 ]; do if [ "$1" = -o ]; then : > "$2"; fi; shift; done')

    def script(self, name: str, body: str) -> Path:
        path = self.fake / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return path

    def run_install(self, *args, distribution="ubuntu-24.04", missing="", answers=None, env=None):
        """install.sh with `args`, on `distribution`, the pieces `missing` pretended missing.
        With `answers`, typed on a terminal of its own (stdin stays /dev/null, as under get.sh
        and `curl … | sh`); without, there's no terminal at all."""
        os_release_file = self.dir / "os-release"
        os_release_file.write_text(RELEASES[distribution])
        self.missing.write_text(missing)
        environ = {
            "PATH": f"{self.fake}:{self.system}", "HOME": str(self.home), "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(self.home / ".local/share"), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"), "XDG_RUNTIME_DIR": str(self.dir),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus", "GST_REGISTRY": str(self.registry),
            "NATTER_TEST_MISSING": str(self.missing), "NATTER_TEST_OS_RELEASE": str(os_release_file),
            "FAKE_LOG": str(self.log), **(env or {}),
        }
        command = ["sh", str(ROOT / "install.sh"), *args]
        if answers is None:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=environ,
                                  timeout=300, start_new_session=True, preexec_fn=die_with_parent)
            return done.returncode, done.stdout + done.stderr
        master, slave = os.openpty()

        def terminal():
            die_with_parent()
            os.setsid()
            fcntl.ioctl(1, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=environ,
                                   preexec_fn=terminal)
        os.close(slave)
        os.write(master, answers.encode())  # typed ahead: read when asked
        out = b""
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO: the terminal's last user is gone
                break
            if not chunk:
                break
            out += chunk
        os.close(master)
        return process.wait(timeout=300), out.decode(errors="replace").replace("\r\n", "\n")

    def logged(self) -> list[str]:
        return self.log.read_text().splitlines()

    def installs(self) -> list[str]:
        """What the package managers were asked to install (by sudo or not), not their queries."""
        return [line for line in self.logged() if line.split()[0] in ("apt-get", "dnf", "pacman", "zypper")
                and ("install" in line.split() or "-S" in line.split())]

    def sudo(self) -> list[str]:
        return [line for line in self.logged() if line.startswith("sudo ")]

    def installed(self) -> bool:
        return (self.home / ".local/bin/natter").exists()

    def test_yes_on_the_terminal(self):
        code, out = self.run_install(missing="gi codecs cc", answers="\n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Required, for", out)
        self.assertIn("Recommended, for", out)
        self.assertIn("Optional, to build the camera fix", out)
        self.assertIn("Install the required and recommended ones now (sudo apt-get install …)? [Y/n]", out)
        self.assertIn("And the optional ones? [Y/n]", out)
        sudo = self.sudo()
        self.assertEqual(len(sudo), 1, self.logged())
        words = sudo[0].split()
        self.assertEqual(words[:4], ["sudo", "apt-get", "install", "-y"])
        self.assertLessEqual({"python3-gi", "gir1.2-webkit-6.0", "gstreamer1.0-libav", "gcc", "libc6-dev"}, set(words))
        self.assertEqual(self.missing.read_text().split(), [])  # all "installed"
        self.assertIn("Done.", out)
        self.assertTrue(self.installed())

    def test_yes_but_not_the_optional_ones(self):
        code, out = self.run_install(missing="WebKit cc", answers="y\nn\n")
        self.assertEqual(code, 0, out)
        (install,) = self.installs()
        self.assertIn("gir1.2-webkit-6.0", install)
        self.assertNotIn("gcc", install)
        self.assertIn("To install them yourself:\n  sudo apt-get install gcc libc6-dev", out)
        self.assertTrue(self.installed())

    def test_no_on_the_terminal(self):
        code, out = self.run_install(missing="WebKit codecs", answers="n\n")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("sudo", "\n".join(self.logged()))
        self.assertIn("To install them yourself:\n  sudo apt-get install gir1.2-webkit-6.0 gstreamer1.0-libav", out)
        self.assertIn("can't run without the required ones", out)
        self.assertFalse(self.installed())

    def test_no_asks_nothing_more_while_required_is_missing(self):
        code, out = self.run_install(missing="WebKit cc", answers="n\ny\n")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("[Y/n]"), 1, out)  # the optional ones are no use without the required ones
        self.assertEqual(self.installs(), [])
        self.assertIn("  sudo apt-get install gir1.2-webkit-6.0 gcc libc6-dev", out)

    def test_no_to_recommended_then_the_optional_ones(self):
        code, out = self.run_install(missing="nice cc", answers="n\ny\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Install the optional ones now (sudo apt-get install …)? [Y/n]", out)
        self.assertNotIn("And the optional ones?", out)
        (install,) = self.installs()
        self.assertEqual(install, "apt-get install -y gcc libc6-dev")
        self.assertIn("  sudo apt-get install gstreamer1.0-nice", out)

    def test_no_but_only_recommended_missing(self):
        code, out = self.run_install(missing="nice", answers="n\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.installs(), [])
        self.assertIn("sudo apt-get install gstreamer1.0-nice", out)
        self.assertTrue(self.installed())

    def test_no_deps(self):
        code, out = self.run_install("--no-deps", missing="Gtk codecs", answers="y\ny\n")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("[Y/n]", out)
        self.assertIn("  sudo apt-get install gir1.2-gtk-4.0 gstreamer1.0-libav", out)
        self.assertFalse(self.installed())
        # Natter runs: nothing is asked or listed, the install goes on as before.
        code, out = self.run_install("--no-deps", missing="codecs nice", answers="y\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("[Y/n]", out)
        self.assertTrue(self.installed())

    def test_no_terminal(self):
        code, out = self.run_install(missing="Adw nice")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.installs(), [])
        self.assertIn("no terminal to ask on", out)
        self.assertIn("  sudo apt-get install gir1.2-adw-1 gstreamer1.0-nice", out)
        self.assertFalse(self.installed())

    def test_yes_without_a_terminal(self):
        code, out = self.run_install("--yes", missing="Adw nice cc")
        self.assertEqual(code, 0, out)
        self.assertNotIn("[Y/n]", out)
        (install,) = self.installs()
        self.assertTrue(install.startswith("apt-get install -y gir1.2-adw-1 gstreamer1.0-nice gcc"), install)
        self.assertEqual(self.sudo(), ["sudo " + install])
        self.assertTrue(self.installed())

    def test_install_fails(self):
        code, out = self.run_install("--yes", missing="Soup", env={"FAKE_FAIL": "1"})
        self.assertEqual(code, 1, out)
        self.assertIn("Couldn't install them", out)
        self.assertIn("apt-get update", out)
        self.assertIn("  sudo apt-get install gir1.2-soup-3.0", out)
        self.assertFalse(self.installed())
        self.log.write_text("")
        code, out = self.run_install("--yes", distribution="arch", missing="Soup", env={"FAKE_FAIL": "1"})
        self.assertEqual(code, 1, out)
        self.assertIn("pacman -Syu", out)
        self.assertNotIn("apt-get", out)

    def test_too_old(self):
        code, out = self.run_install("--yes", distribution="ubuntu-22.04", missing="WebKit codecs")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.logged(), [])  # not even asked
        self.assertIn("too old for the native version", out)
        self.assertIn('"Flatpak" in README.md', out)
        self.assertFalse(self.installed())

    def test_package_missing_from_the_release(self):
        code, out = self.run_install("--yes", distribution="debian-13", missing="WebKit",
                                     env={"FAKE_UNAVAILABLE": "gir1.2-webkit-6.0"})
        self.assertEqual(code, 1, out)
        self.assertEqual(self.installs(), [])
        self.assertIn("has no gir1.2-webkit-6.0", out)
        self.assertIn("Flatpak", out)

    def test_root_without_sudo(self):
        (self.fake / "sudo").unlink()
        self.script("id", 'if [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi')
        code, out = self.run_install("--yes", missing="Gtk")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.installs(), ["apt-get install -y gir1.2-gtk-4.0"])
        self.assertEqual(self.sudo(), [])
        self.assertTrue(self.installed())

    def test_neither_root_nor_sudo(self):
        (self.fake / "sudo").unlink()
        code, out = self.run_install("--yes", missing="Gtk", answers="y\n")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.installs(), [])
        self.assertIn("There's no sudo here", out)
        self.assertIn("  apt-get install gir1.2-gtk-4.0", out)

    def test_other_package_managers(self):
        expected = {
            "fedora-41": "sudo dnf install -y python3-gobject gtk4 libadwaita webkitgtk6.0",
            "arch": "sudo pacman -S --needed --noconfirm python-gobject gtk4 libadwaita webkitgtk-6.0",
            "tumbleweed": "sudo zypper --non-interactive install python3-gobject python3-gobject-Gdk",
        }
        for distribution, start in expected.items():
            with self.subTest(distribution):
                self.log.write_text("")
                code, out = self.run_install("--yes", distribution=distribution, missing="gi")
                self.assertEqual(code, 0, out)
                (install,) = self.sudo()
                self.assertTrue(install.startswith(start), install)
                self.assertIn("Done.", out)

    def test_rpm_fusion_note(self):
        code, out = self.run_install("--yes", distribution="fedora-41", missing="codecs",
                                     env={"FAKE_UNAVAILABLE": "gstreamer1-plugin-openh264"})
        self.assertEqual(code, 0, out)
        self.assertIn("RPM Fusion", out)
        (install,) = self.sudo()
        self.assertNotIn("openh264", install)
        self.assertIn("gstreamer1-plugin-libav", install)

    def test_unknown_distribution(self):
        code, out = self.run_install("--yes", distribution="unknown", missing="Gtk")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.logged(), [])
        self.assertIn("GTK 4 for Python isn't installed", out)
        self.assertIn("from your distribution's packages", out)
        code, out = self.run_install("--yes", distribution="unknown", missing="nice")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.logged(), [])
        self.assertIn("libnice's GStreamer plugin", out)

    def test_nothing_missing(self):
        code, out = self.run_install(missing="", answers="")
        self.assertEqual(code, 0, out)
        self.assertNotIn("[Y/n]", out)
        self.assertNotIn("needs packages", out)
        self.assertEqual(self.installs(), [])


class ScriptTest(unittest.TestCase):
    def test_syntax(self):
        for shell in ("dash", "bash"):
            if shutil.which(shell):
                done = subprocess.run([shell, "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_reads_answers_from_the_terminal(self):
        install = (ROOT / "install.sh").read_text()
        self.assertIn("read -r answer </dev/tty", install)
        self.assertIn("from natter.__main__ import deps", install)
        # sudo only in run_install, which runs only after a yes (or --yes).
        self.assertEqual(len(re.findall(r"set -- sudo ", install)), 1)

    def test_usage(self):
        with tempfile.TemporaryDirectory() as home:
            done = subprocess.run(["sh", str(ROOT / "install.sh"), "--bogus"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", ""), "HOME": home})
        self.assertEqual(done.returncode, 2)
        self.assertIn("--yes", done.stderr)
        self.assertIn("--no-deps", done.stderr)


if __name__ == "__main__":
    unittest.main()
