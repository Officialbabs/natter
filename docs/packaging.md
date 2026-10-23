# Installing and packaging

The one-line command in the [README](../README.md#install) does what the first two sections say,
for you. The package names here are the ones `install.sh` uses, from `natter/distro.py`. Natter Den
has been run on Ada 26.04 (based on Ubuntu) and on CachyOS (based on Arch) with KDE Plasma; nothing
in this file has been tried on another distribution.

## What get.sh and install.sh do

`get.sh` downloads the latest Natter Den from GitHub as an archive (it needs curl or wget, and tar;
no git), puts it in `~/.local/lib/natter/source`, replacing the copy there only once the download
is complete, and runs its `install.sh`. It stops while Natter Den is running. With `--flatpak` it
builds and installs the Flatpak instead.

`install.sh` checks what Natter Den needs. If packages are missing, it lists them in three groups
and asks before installing them with your package manager (apt-get, dnf, pacman or zypper,
through `sudo`), once for the required and recommended ones and once for the optional ones:

- **Required:** for Natter Den to run.
- **Recommended:** the codecs WhatsApp's videos and voice messages need, and GStreamer's WebRTC
  parts for calls.
- **Optional:** a C compiler, `pkg-config` and GStreamer's headers, to build the camera fix. Asked
  for only with GStreamer 1.28 or newer.

It asks on the terminal, so it also works through `curl … | sh`. Then it picks the Python that has
everything (the system's `/usr/bin/python3` first), writes `~/.local/bin/natter` for it, pointing at
its own folder, and installs the menu entry, icons, desktop search files and the `whatsapp://`
handler (unless another app already has it). It says if `~/.local/bin` isn't on your `PATH`.

- While Natter Den is running it stops and asks you to quit it first; `--force` goes ahead anyway.
- `--yes` installs missing packages without asking. `--no-deps` never installs: when a required
  package is missing, it prints the command and stops. A run with no terminal to ask on installs
  nothing either.
- On a release too old for Natter Den (Ubuntu 22.04, Debian 12, openSUSE Leap 15…) it says so and
  points to the Flatpak.
- `--uninstall` removes what it installed, and leaves your sessions and settings in
  `~/.local/share/natter`, `~/.config/natter` and `~/.cache/natter`.

## By hand

The packages Natter Den needs, with the GStreamer codecs WhatsApp's videos (H.264, AAC) and voice
messages (Opus) need:

```sh
# Debian, Ubuntu and derivatives
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 gir1.2-soup-3.0 gir1.2-gdkpixbuf-2.0 gir1.2-gstreamer-1.0 gstreamer1.0-libav gstreamer1.0-plugins-good gstreamer1.0-plugins-base
# Fedora and RHEL-likes
sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 libsoup3 gdk-pixbuf2 gstreamer1 gstreamer1-plugin-openh264 gstreamer1-plugin-libav gstreamer1-plugins-good
# Arch and derivatives
sudo pacman -S --needed python-gobject gtk4 libadwaita webkitgtk-6.0 libsoup3 gdk-pixbuf2 gstreamer gst-libav gst-plugins-good gst-plugins-base
# openSUSE
sudo zypper install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-WebKit-6_0 typelib-1_0-Soup-3_0 typelib-1_0-GdkPixbuf-2_0 typelib-1_0-Gst-1_0 gstreamer-plugin-openh264 gstreamer-plugins-libav gstreamer-plugins-good
```

On Fedora, `gstreamer1-plugin-openh264` comes from the fedora-cisco-openh264 repository; on a
RHEL-like, `gstreamer1-plugin-libav` from EPEL. RPM Fusion's ffmpeg is the other way to H.264.

For calls inside Natter Den, GStreamer's WebRTC plugins (webrtcbin, SCTP, DTLS), libnice's
GStreamer plugin, and the GstWebRTC and GstSdp introspection data:

```sh
# Debian, Ubuntu and derivatives
sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-nice gir1.2-gst-plugins-bad-1.0 gir1.2-gst-plugins-base-1.0
# Fedora and RHEL-likes
sudo dnf install gstreamer1-plugins-bad-free libnice-gstreamer1 gstreamer1-plugins-bad-free-libs gstreamer1-plugins-base
# Arch and derivatives
sudo pacman -S --needed gst-plugins-bad libnice gst-plugins-bad-libs gst-plugins-base-libs
# openSUSE
sudo zypper install gstreamer-plugins-bad gstreamer-libnice typelib-1_0-GstWebRTC-1_0 typelib-1_0-GstSdp-1_0
```

For the camera fix, with GStreamer 1.28 or newer:

```sh
# Debian, Ubuntu and derivatives
sudo apt install gcc libc6-dev pkg-config libgstreamer1.0-dev
# Fedora and RHEL-likes
sudo dnf install gcc pkgconf-pkg-config gstreamer1-devel
# Arch and derivatives
sudo pacman -S --needed gcc pkgconf gstreamer
# openSUSE
sudo zypper install gcc pkgconf-pkg-config gstreamer-devel
```

On Fedora Atomic, `rpm-ostree install` can layer Fedora's packages instead (they take effect after
a restart); the Flatpak is the other way there, and on Bazzite and SteamOS.

Then download Natter Den and install it:

```sh
mkdir -p ~/.local/lib/natter/source && cd ~/.local/lib/natter/source
curl -fsSL https://github.com/Officialbabs/natter/archive/refs/heads/master.tar.gz | tar -xz --strip-components=1
./install.sh   # adds Natter Den to your apps and `natter` to ~/.local/bin
```

## Flatpak

The Flatpak builds on GNOME's runtime (`org.gnome.Platform//50`), which brings GTK, libadwaita,
WebKitGTK and GStreamer, so it doesn't depend on the system's versions. It hasn't been built and
tried yet. Building it needs `flatpak` and `flatpak-builder` (on Ubuntu and Debian:
`sudo apt install flatpak flatpak-builder`) and the Flathub remote. From Natter Den's source folder,
for you alone (`get.sh --flatpak` does the same, building in `~/.cache/natter-den-flatpak`):

```sh
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.gnome.Sdk//50 org.gnome.Platform//50
flatpak-builder --user --install --force-clean build-flatpak packaging/io.github.Officialbabs.Natter.yml
flatpak run io.github.Officialbabs.Natter
```

In the Flatpak, notifications go through the notification portal, start at login through the
Background portal, and there's no dock badge or recent chats in the dock menu (the tray shows the
count). The manifest adds no modules for GStreamer's WebRTC plugins, so whether calls work in the
Flatpak depends on what the runtime has. The camera fix isn't used there.

## Distribution packages

`meson.build` installs Natter Den the way distribution packages want it: the `natter` package in
`/usr/share/natter`, the `natter` command, desktop file, icons, AppStream metainfo, D-Bus service,
search provider, and the camera fix when a C compiler and GStreamer's headers are there. The
Flatpak builds with it too.

```sh
meson setup build --prefix=/usr && meson install -C build
```

On Arch and its derivatives, [packaging/aur/natter-den-git](../packaging/aur/natter-den-git) has a
PKGBUILD (`makepkg -si`); it isn't on the AUR yet. Neither the meson build nor the PKGBUILD has
been run on a real distribution yet.

## Coming from io.natter.Natter

Natter Den's app id is `io.github.Officialbabs.Natter` (it was `io.natter.Natter`). Quit the old
Natter first (*Quit* in its tray menu, or Ctrl+Q in its window): `install.sh` stops while it's
running, since both would use the same WhatsApp session. Then run `install.sh` again: it removes
the old menu entry, icons and search files, moves start-at-login over to the new name, and moves
the `whatsapp://` handler if it was the old one's. Your accounts, chats and settings stay where they
were (both ids use the `natter` folders), so there's nothing to link again. Docks remember apps by
id, so pin Natter Den again. With `install.sh --force` and the old Natter still running, the new
one won't open WhatsApp until the old one quits: it brings the old window forward instead.

On Ada, if `AllowBackground` in `~/.config/os-ai/lifecycle.ini` names `io.natter.Natter`, replace
it with `io.github.Officialbabs.Natter`. `install.sh` says when the old id is still there and
leaves the file to you.

## Flathub checklist

What's still to do before submitting Natter Den to Flathub:

- **Build and try the Flatpak**, calls and the camera included, and run Flathub's linter:
  `flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest packaging/io.github.Officialbabs.Natter.yml`.
- **Screenshots.** The metainfo (`data/io.github.Officialbabs.Natter.metainfo.xml`) has no
  `<screenshots>` yet, and Flathub requires them: images at stable URLs (a tagged commit or a
  release, not a branch that moves), showing no one's real chats, names or numbers.
  `docs/screenshots/` has two, taken with the test page.
- **The source.** The manifest builds from this checkout (`type: dir`); Flathub's needs a `git`
  source with a tag and commit.
- **Checks.** `meson test -C build` runs `appstreamcli validate` and `desktop-file-validate`, but
  skips each when it isn't installed.
- **Permissions a reviewer may ask about**, such as `--filesystem=xdg-config/os-ai:ro` (for Follow
  System on Ada).
