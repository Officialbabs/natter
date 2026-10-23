"""Which Linux this is, so a missing piece comes with the command that installs it.

No PyGObject here: the version check uses it before it knows PyGObject works.

Test-only hooks, read from the environment (tests/test_install_deps.py sets them; nothing else should):
  NATTER_TEST_MISSING     a file naming pieces (PIECES' names) to report missing, as if they weren't
                          installed. It can only add problems, never hide one: what's really missing
                          is still reported. Fake package managers "install" by taking names out.
  NATTER_TEST_OS_RELEASE  an os-release file to read instead of the system's.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import NAME

OS_RELEASE = (Path("/etc/os-release"), Path("/usr/lib/os-release"))
TEST_MISSING = "NATTER_TEST_MISSING"
TEST_OS_RELEASE = "NATTER_TEST_OS_RELEASE"
OSTREE_BOOTED = Path("/run/ostree-booted")

# os-release ID / ID_LIKE → package manager. Derivatives name their parent in ID_LIKE
# (Mint: "ubuntu debian", Rocky: "rhel centos fedora", Manjaro: "arch").
MANAGERS = {
    "debian": "apt", "ubuntu": "apt",
    # Mageia and OpenMandriva say they're like Fedora, but their packages have other names.
    "mageia": None, "openmandriva": None, "mandriva": None,
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf",
    # Arch and the systems built on it, by their own ID too: CachyOS's os-release hook writes
    # ID_LIKE=arch, but installs without it have been seen (and so have other derivatives').
    "arch": "pacman", "cachyos": "pacman", "endeavouros": "pacman", "manjaro": "pacman", "garuda": "pacman",
    "artix": "pacman",
    "opensuse": "zypper", "suse": "zypper", "sles": "zypper",
}
INSTALL = {
    "apt": "sudo apt install",
    "dnf": "sudo dnf install",
    "pacman": "sudo pacman -S --needed",
    "zypper": "sudo zypper install",
    "rpm-ostree": "rpm-ostree install",  # Fedora's own packages, layered onto the image
}
# Systems whose own files are an image that updates whole, so packages don't install the usual
# way: Fedora Atomic's desktops (by VARIANT_ID), Bazzite and SteamOS (by ID).
ATOMIC_VARIANTS = {"silverblue", "kinoite", "sericea", "onyx"}
IMAGE_IDS = {"steamos", "bazzite"}

REQUIRED, RECOMMENDED, OPTIONAL = "required", "recommended", "optional"
GROUPS = (REQUIRED, RECOMMENDED, OPTIONAL)
# The managers install.sh runs itself (rpm-ostree changes an image, which only takes effect after a restart).
AUTO = ("apt", "dnf", "pacman", "zypper")


@dataclass(frozen=True)
class Piece:
    """Something Natter checks for here, and the packages that bring it with each package manager."""

    name: str  # as natter.__main__.missing_pieces() and NATTER_TEST_MISSING call it
    group: str  # REQUIRED: Natter doesn't run without it; RECOMMENDED: media and calls; OPTIONAL: the camera fix
    words: str
    packages: dict  # apt, dnf, pacman, zypper → package names, space-separated


# Every name checked against each distribution's current repositories (Ubuntu 24.04, Debian 13,
# Fedora 43, Arch, openSUSE Tumbleweed; October 2026). Fedora and Arch put the introspection data
# (typelibs) in the library's own package. openSUSE's python3-gobject(-Gdk) are capabilities its
# python3XX-gobject packages provide; its openh264 comes from the openh264 repository it enables
# by default, Fedora's from fedora-cisco-openh264.
PIECES = (
    Piece("gi", REQUIRED, "PyGObject (GTK for Python)",
          {"apt": "python3-gi", "dnf": "python3-gobject", "pacman": "python-gobject", "zypper": "python3-gobject"}),
    Piece("Gtk", REQUIRED, "GTK 4 for Python",
          {"apt": "gir1.2-gtk-4.0", "dnf": "gtk4", "pacman": "gtk4", "zypper": "python3-gobject-Gdk typelib-1_0-Gtk-4_0"}),
    Piece("Adw", REQUIRED, "libadwaita for Python",
          {"apt": "gir1.2-adw-1", "dnf": "libadwaita", "pacman": "libadwaita", "zypper": "typelib-1_0-Adw-1"}),
    Piece("WebKit", REQUIRED, "WebKitGTK 6.0 for Python",
          {"apt": "gir1.2-webkit-6.0", "dnf": "webkitgtk6.0", "pacman": "webkitgtk-6.0", "zypper": "typelib-1_0-WebKit-6_0"}),
    Piece("Soup", REQUIRED, "libsoup 3 for Python",
          {"apt": "gir1.2-soup-3.0", "dnf": "libsoup3", "pacman": "libsoup3", "zypper": "typelib-1_0-Soup-3_0"}),
    Piece("GdkPixbuf", REQUIRED, "GdkPixbuf for Python",
          {"apt": "gir1.2-gdkpixbuf-2.0", "dnf": "gdk-pixbuf2", "pacman": "gdk-pixbuf2", "zypper": "typelib-1_0-GdkPixbuf-2_0"}),
    # The codec check and calls ask GStreamer through it.
    Piece("Gst", REQUIRED, "GStreamer for Python",
          {"apt": "gir1.2-gstreamer-1.0", "dnf": "gstreamer1", "pacman": "gstreamer", "zypper": "typelib-1_0-Gst-1_0"}),
    # What WhatsApp's videos (H.264, AAC, in MP4) and voice messages (Opus) need from GStreamer.
    Piece("codecs", RECOMMENDED, "codecs for WhatsApp's videos and voice messages (H.264, AAC, Opus)",
          {"apt": "gstreamer1.0-libav gstreamer1.0-plugins-good gstreamer1.0-plugins-base",
           "dnf": "gstreamer1-plugin-openh264 gstreamer1-plugin-libav gstreamer1-plugins-good",
           "pacman": "gst-libav gst-plugins-good gst-plugins-base",
           "zypper": "gstreamer-plugin-openh264 gstreamer-plugins-libav gstreamer-plugins-good"}),
    # Calls (rtc.available()): webrtcbin with SCTP and DTLS, libnice's ICE, and their Python bindings.
    Piece("webrtc", RECOMMENDED, "GStreamer's WebRTC plugins (webrtcbin, SCTP, DTLS), for calls",
          {"apt": "gstreamer1.0-plugins-bad", "dnf": "gstreamer1-plugins-bad-free", "pacman": "gst-plugins-bad",
           "zypper": "gstreamer-plugins-bad"}),
    Piece("nice", RECOMMENDED, "libnice's GStreamer plugin, for calls",
          {"apt": "gstreamer1.0-nice", "dnf": "libnice-gstreamer1", "pacman": "libnice", "zypper": "gstreamer-libnice"}),
    Piece("GstWebRTC", RECOMMENDED, "GstWebRTC for Python, for calls",
          {"apt": "gir1.2-gst-plugins-bad-1.0", "dnf": "gstreamer1-plugins-bad-free-libs",
           "pacman": "gst-plugins-bad-libs", "zypper": "typelib-1_0-GstWebRTC-1_0"}),
    Piece("GstSdp", RECOMMENDED, "GstSdp for Python, for calls",
          {"apt": "gir1.2-gst-plugins-base-1.0", "dnf": "gstreamer1-plugins-base", "pacman": "gst-plugins-base-libs",
           "zypper": "typelib-1_0-GstSdp-1_0"}),
    # The camera fix's build (install.sh's build_camfix), wanted with GStreamer 1.28 or newer only.
    Piece("cc", OPTIONAL, "a C compiler",
          {"apt": "gcc libc6-dev", "dnf": "gcc", "pacman": "gcc", "zypper": "gcc"}),
    Piece("pkg-config", OPTIONAL, "pkg-config",
          {"apt": "pkg-config", "dnf": "pkgconf-pkg-config", "pacman": "pkgconf", "zypper": "pkgconf-pkg-config"}),
    Piece("gst-dev", OPTIONAL, "GStreamer's development headers",
          {"apt": "libgstreamer1.0-dev", "dnf": "gstreamer1-devel", "pacman": "gstreamer", "zypper": "gstreamer-devel"}),
)
PIECE = {piece.name: piece for piece in PIECES}


def packages(pieces: Iterable[Piece], manager: str) -> list[str]:
    """The packages that bring `pieces` with `manager`, in order, each once."""
    names: list[str] = []
    for piece in pieces:
        names += [name for name in piece.packages.get(manager, "").split() if name not in names]
    return names


def _table(pieces: Iterable[Piece]) -> dict[str, str]:
    pieces = list(pieces)
    return {manager: " ".join(packages(pieces, manager)) for manager in AUTO}


# What Natter needs to run: PyGObject, GTK 4, libadwaita, WebKitGTK 6.0, libsoup 3, GdkPixbuf, and
# GStreamer for Python, which the codec check and calls use.
DESKTOP = _table(piece for piece in PIECES if piece.group == REQUIRED)
# What WhatsApp's videos (H.264, AAC) and voice messages (Opus) need from GStreamer.
MEDIA = _table([PIECE["codecs"]])
# GStreamer for Python, which the codec check needs to ask GStreamer what it can play.
GST_PYTHON = _table([PIECE["Gst"]])

# Releases older than Natter needs (__main__.FLOORS: GTK 4.14, libadwaita 1.5, WebKitGTK 2.42),
# though most have packages by the same names (Ubuntu 22.04: GTK 4.6, libadwaita 1.1). By
# os-release ID, the first release that's new enough.
FIRST_NEW_ENOUGH = {
    "ubuntu": (24, 4), "debian": (13,), "fedora": (40,),
    "rhel": (10,), "centos": (10,), "rocky": (10,), "almalinux": (10,), "ol": (10,),
    "opensuse-leap": (16,), "sles": (16,),
}
# Derivatives (Mint, Pop!_OS, elementary, LMDE…) say which Ubuntu or Debian they're built on.
OLD_UBUNTU = {"bionic": "18.04", "focal": "20.04", "jammy": "22.04", "kinetic": "22.10", "lunar": "23.04",
              "mantic": "23.10"}
OLD_DEBIAN = {"stretch": "9", "buster": "10", "bullseye": "11", "bookworm": "12"}
# Codec packages a system's enabled repositories may lack: Fedora's openh264 is in its
# fedora-cisco-openh264 repository, a RHEL-like's gstreamer1-plugin-libav in EPEL. RPM Fusion has
# neither, but its full ffmpeg gives gstreamer1-plugin-libav the H.264 decoder ffmpeg-free lacks.
RPM_FUSION = {"gstreamer1-plugin-libav", "gstreamer1-plugin-openh264"}

def os_release(text: Optional[str] = None) -> dict[str, str]:
    """os-release's fields. It's KEY=value lines, values quoted the shell's way."""
    if text is None:
        test = os.environ.get(TEST_OS_RELEASE)  # test-only (see the top of this file)
        for path in [Path(test)] if test else OS_RELEASE:
            try:
                text = path.read_text()
                break
            except OSError:
                continue
        else:
            return {}
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.startswith("#"):
            continue
        try:
            words = shlex.split(value)
        except ValueError:
            continue
        fields[key] = words[0] if words else ""
    return fields


def image_based(release: dict[str, str]) -> bool:
    """Whether the system is an image that updates whole (Fedora Atomic, Bazzite, SteamOS…),
    where the Flatpak is the way to install Natter."""
    return (OSTREE_BOOTED.exists() or release.get("VARIANT_ID") in ATOMIC_VARIANTS
            or release.get("ID") in IMAGE_IDS)


def package_manager(release: dict[str, str]) -> Optional[str]:
    """apt, dnf, pacman or zypper, from the distribution's ID and the ones it's like; rpm-ostree
    on Fedora Atomic. None where Natter can't name the packages, and the Flatpak is the way."""
    manager = None
    for word in [release.get("ID", "")] + release.get("ID_LIKE", "").split():
        key = word if word in MANAGERS else word.split("-")[0]  # "opensuse-tumbleweed", "opensuse-leap"
        if key in MANAGERS:
            manager = MANAGERS[key]
            break
    if image_based(release):
        return "rpm-ostree" if manager == "dnf" else None
    return manager


def install_hint(packages: dict[str, str], release: Optional[dict[str, str]] = None) -> str:
    """The command that installs `packages` here, or "" on a distribution Natter doesn't know
    (and on an image-based one, Fedora Atomic aside: see image_based)."""
    manager = package_manager(os_release() if release is None else release)
    if manager is None:
        return ""
    names = packages.get("dnf" if manager == "rpm-ostree" else manager)
    return f"{INSTALL[manager]} {names}" if names else ""


def pretend_missing(environ=None) -> set[str]:
    """Test-only: the pieces NATTER_TEST_MISSING's file names, to report missing on top of what
    really is (see the top of this file). Empty without it."""
    path = (os.environ if environ is None else environ).get(TEST_MISSING)
    if not path:
        return set()
    try:
        return set(Path(path).read_text().split())
    except OSError:
        return set()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:2])


def too_old_release(release: dict[str, str]) -> str:
    """Why this release is too old for Natter as it is, in words; "" when it isn't, or isn't known."""
    distro = release.get("ID", "")
    label = release.get("PRETTY_NAME") or " ".join(filter(None, (release.get("NAME") or distro, release.get("VERSION_ID"))))
    old = f"has a GTK, libadwaita or WebKitGTK older than {NAME} needs"
    ubuntu = release.get("UBUNTU_CODENAME") or (release.get("VERSION_CODENAME") if distro == "ubuntu" else None)
    if ubuntu in OLD_UBUNTU:
        return f"{label} {old}" if distro == "ubuntu" else f"{label} is built on Ubuntu {OLD_UBUNTU[ubuntu]}, which {old}"
    debian = release.get("DEBIAN_CODENAME") or (release.get("VERSION_CODENAME") if distro == "debian" else None)
    if debian in OLD_DEBIAN:
        return f"{label} {old}" if distro == "debian" else f"{label} is built on Debian {OLD_DEBIAN[debian]}, which {old}"
    first = FIRST_NEW_ENOUGH.get(distro)
    version = _version(release.get("VERSION_ID", ""))
    if first and version and version < first:
        return f"{label} {old}"
    return ""


def _ask(command: list[str], timeout: float = 30) -> Optional[str]:
    """A package manager's answer (its output, in English), None when it couldn't be asked."""
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C", LANGUAGE="C"))
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


# A package every system with that manager has in its lists: when it isn't found, they couldn't be read.
SENTINEL = {"dnf": "bash", "pacman": "pacman", "zypper": "zypper"}


def known_packages(manager: str, names: list[str], ask: Callable[[list[str]], Optional[str]] = _ask) -> Optional[set[str]]:
    """Those of `names` the package manager's own lists have, read without downloading anything
    (and without root); None when it can't tell: it couldn't be asked, or has no lists yet."""
    if not names:
        return set()
    if manager == "apt":
        # Without its lists (apt-get update never ran, or they were cleaned) apt knows only what's
        # installed. Its package files come first in its policy, "… amd64 Packages" each.
        sources = ask(["apt-cache", "policy"])
        if sources is None or not re.search(r" Packages$", sources, re.M):
            return None
        # A package it knows without a version to install ("Candidate: (none)") doesn't count.
        out = ask(["apt-cache", "policy", *names])
        if out is None:
            return None
        found, current = set(), None
        for line in out.splitlines():
            if not line.startswith(" ") and line.endswith(":"):
                current = line[:-1]
            elif current and line.strip().startswith("Candidate:") and line.split(":", 1)[1].strip() != "(none)":
                found.add(current)
        return found & set(names)
    if manager not in SENTINEL:
        return None
    asked = [*names, SENTINEL[manager]]
    if manager == "dnf":
        out = ask(["dnf", "-q", "-C", "repoquery", "--qf", "%{name}\n", *asked])  # -C: its cache only
        found = None if out is None else {line.strip() for line in out.splitlines()}
    elif manager == "pacman":
        out = ask(["pacman", "-Si", *asked])
        found = None if out is None else set(re.findall(r"^Name\s*:\s*(\S+)", out, re.M))
    else:
        # zypper by capability, as zypper install takes them (python3-gobject is one), one at a
        # time: its table doesn't say which capability a package matched.
        found = set()
        for name in asked:
            out = ask(["zypper", "--non-interactive", "--no-refresh", "--quiet", "search", "--match-exact",
                       "--provides", "--type", "package", name])
            if out is None:
                return None
            rows = [line.split("|") for line in out.splitlines() if line.count("|") >= 2]
            if any(row[1].strip() not in ("", "Name") and not set("".join(row)) <= set("-+ ") for row in rows):
                found.add(name)
    if found is None or SENTINEL[manager] not in found:
        return None
    return found & set(names)


def report(release: dict[str, str], missing: Iterable[str], old: Iterable[str] = (), optional: bool = True,
           ask: Callable[[list[str]], Optional[str]] = _ask) -> list[str]:
    """What install.sh reads (natter.__main__.deps prints it): one line each, a word then the rest.

      manager apt|dnf|pacman|zypper   the package manager install.sh may run ("" elsewhere: none)
      required PACKAGE…               what brings each group's `missing` pieces with it
      recommended PACKAGE…
      optional PACKAGE…               (only with `optional`: GStreamer 1.28 or newer, the camera fix's)
      required-words WORDS; …         the missing pieces in words, for any distribution
      recommended-words …, optional-words …
      too-old REASON                  Natter can't run and installing won't change that: the Flatpak
      note TEXT                       for the person, as it is

    `missing` are piece names (PIECES); `old` the versions too old to run, in words (FLOORS).
    Packages the manager's lists don't have are left out, with a note; when one of the required
    ones is among them, the release is too old for Natter."""
    missing = set(missing)
    old = list(old)
    groups = {group: [piece for piece in PIECES if piece.name in missing and piece.group == group
                      and (optional or group != OPTIONAL)] for group in GROUPS}
    manager = package_manager(release)
    lines = []
    for group in GROUPS:
        if groups[group]:
            lines.append(f"{group}-words " + "; ".join(piece.words for piece in groups[group]))
    if old:
        return lines + ["too-old " + "; ".join(old)]
    reason = too_old_release(release) if groups[REQUIRED] else ""
    if reason:
        return lines + [f"too-old {reason}"]
    if manager not in AUTO:
        return lines
    names = {group: packages(groups[group], manager) for group in GROUPS}
    known = known_packages(manager, [name for group in GROUPS for name in names[group]], ask)
    if known is not None:  # None: it can't tell (no lists yet?), and they're all tried
        lacking = {group: [name for name in names[group] if name not in known] for group in GROUPS}
        if lacking[REQUIRED]:
            label = release.get("PRETTY_NAME") or release.get("ID", "This system")
            return lines + [f"too-old {label} has no {', '.join(lacking[REQUIRED])}"]
        gone = lacking[RECOMMENDED] + lacking[OPTIONAL]
        if gone:
            names = {group: [name for name in names[group] if name in known] for group in GROUPS}
            note = f"note Not in your package sources, so left out: {' '.join(gone)}."
            if manager == "dnf" and RPM_FUSION & set(gone):
                note += (" For H.264 video, enable Fedora's fedora-cisco-openh264 repository (on a RHEL-like,"
                         " EPEL has gstreamer1-plugin-libav), or RPM Fusion's ffmpeg:"
                         " https://rpmfusion.org/Howto/Multimedia")
            lines.append(note)
    lines.insert(0, f"manager {manager}")
    for group in GROUPS:
        if names[group]:
            lines.append(f"{group} " + " ".join(names[group]))
    return lines
