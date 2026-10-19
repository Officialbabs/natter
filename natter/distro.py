"""Which Linux this is, so a missing piece comes with the command that installs it.

No PyGObject here: the version check uses it before it knows PyGObject works.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional

OS_RELEASE = (Path("/etc/os-release"), Path("/usr/lib/os-release"))
OSTREE_BOOTED = Path("/run/ostree-booted")

# os-release ID / ID_LIKE → package manager. Derivatives name their parent in ID_LIKE
# (Mint: "ubuntu debian", Rocky: "rhel centos fedora", Manjaro: "arch").
MANAGERS = {
    "debian": "apt", "ubuntu": "apt",
    # Mageia and OpenMandriva say they're like Fedora, but their packages have other names.
    "mageia": None, "openmandriva": None, "mandriva": None,
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf",
    "arch": "pacman",
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

# What Natter needs: PyGObject, GTK 4, libadwaita, WebKitGTK 6.0, libsoup 3, GdkPixbuf, and
# GStreamer for Python, which only the codec check uses (Fedora and Arch ship it with GStreamer).
DESKTOP = {
    "apt": "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 gir1.2-soup-3.0 gir1.2-gdkpixbuf-2.0 "
           "gir1.2-gstreamer-1.0",
    "dnf": "python3-gobject gtk4 libadwaita webkitgtk6.0 libsoup3 gdk-pixbuf2",
    "pacman": "python-gobject gtk4 libadwaita webkitgtk-6.0 libsoup3 gdk-pixbuf2",
    "zypper": "python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 "
              "typelib-1_0-WebKit-6_0 typelib-1_0-Soup-3_0 typelib-1_0-GdkPixbuf-2_0 typelib-1_0-Gst-1_0",
}
# What WhatsApp's videos (H.264, AAC) and voice messages (Opus) need from GStreamer.
MEDIA = {
    "apt": "gstreamer1.0-libav gstreamer1.0-plugins-good gstreamer1.0-plugins-base",
    "dnf": "gstreamer1-plugin-openh264 gstreamer1-plugin-libav gstreamer1-plugins-good",
    "pacman": "gst-libav gst-plugins-good gst-plugins-base",
    "zypper": "gstreamer-plugin-openh264 gstreamer-plugins-libav gstreamer-plugins-good",
}
# GStreamer for Python, which the codec check needs to ask GStreamer what it can play.
GST_PYTHON = {
    "apt": "gir1.2-gstreamer-1.0",
    "dnf": "gstreamer1",
    "pacman": "gstreamer",
    "zypper": "typelib-1_0-Gst-1_0",
}


def os_release(text: Optional[str] = None) -> dict[str, str]:
    """os-release's fields. It's KEY=value lines, values quoted the shell's way."""
    if text is None:
        for path in OS_RELEASE:
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
