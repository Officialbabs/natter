<img src="data/icons/hicolor/scalable/apps/io.github.Officialbabs.Natter.svg" width="96" height="96" alt="">

# Natter Den

Natter Den is an unofficial WhatsApp Web client for Linux. It shows WhatsApp Web in a GTK 4 and
libadwaita window, on the system's WebKitGTK, with desktop notifications, a tray icon and keyboard
shortcuts. It isn't made by, endorsed by or affiliated with WhatsApp or Meta: you link it to your
phone as a linked device, as you would WhatsApp Web in a browser.

<p>
  <img src="docs/screenshots/natter-den-light.png" width="49%" alt="Natter Den in the light style: the chat list on the left, an open chat on the right">
  <img src="docs/screenshots/natter-den-dark.png" width="49%" alt="Natter Den in the dark style">
</p>

*Shown with the test page (tests/mock-whatsapp.html) in place of WhatsApp, so no real chats appear.*

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh
```

[get.sh](get.sh) downloads Natter Den into `~/.local/lib/natter/source` and runs its `install.sh`,
which installs it for you alone. If packages are missing, it lists them and asks before installing
them with your package manager (through `sudo`). Then open Natter Den from your apps and scan the
code from *Linked devices* in WhatsApp on your phone.

- **Update:** run the same command again.
- **Uninstall:** `~/.local/lib/natter/source/install.sh --uninstall && rm -rf ~/.local/lib/natter`
  (your sessions and settings stay in `~/.local/share/natter`, `~/.config/natter` and
  `~/.cache/natter` until you delete them).

The steps by hand, and the packages for each distribution, are in
[docs/packaging.md](docs/packaging.md).

### Flatpak

For systems too old for Natter Den as it is (see [Requirements](#requirements)), and for those
that install apps as Flatpaks (Fedora Atomic, Bazzite, SteamOS), `get.sh` can build and install
the Flatpak instead, on GNOME's runtime:

```sh
curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh -s -- --flatpak
```

- **Update:** run the same command again.
- **Uninstall:** `flatpak uninstall --user io.github.Officialbabs.Natter && rm -rf ~/.local/lib/natter/source ~/.cache/natter-den-flatpak`
  (your data stays in `~/.var/app/io.github.Officialbabs.Natter`).

The Flatpak is experimental: it hasn't been tested yet, calls in it included.

## Features

- A libadwaita window in the system's light or dark style, or the one you pick.
- One notification per chat: new messages replace it and add a count, with the sender's picture.
  Clicking it opens that chat, and *Mute Chat for 1 Hour* is on the notification.
- Quiet hours, and *Do not disturb* for one hour, two, or until you turn it off (from the tray or
  Ctrl+K). The unread count still updates.
- The unread count in the tray, on each account's tab, and on the dock icon where the dock shows
  badges (the Unity launcher API).
- Ctrl+K: go to a chat by name, message a phone number without saving it, or run a command.
- Your five most recent chats in the dock icon's right-click menu (installs made by `install.sh`
  only; the list is empty while a passcode is set or privacy blur is on).
- Paste (Ctrl+V) or drop pictures and files on a chat, and WhatsApp's own preview opens with a
  caption box. With no chat open, Natter Den asks which chat.
- `whatsapp://` links from other apps open in Natter Den (if another app already had them,
  `install.sh` leaves them there and prints the command that moves them), and so do `wa.me`,
  `api.whatsapp.com/send`, group invite and channel links clicked inside WhatsApp. Other links
  open in your browser.
- Themes in Preferences → General → Appearance: Den, WhatsApp's 22 chat themes, Custom (from a
  colour you pick) or Plain. A theme is a local style sheet over WhatsApp's colours
  ([how](docs/how-it-works.md#themes)).
- Privacy blur (Ctrl+Shift+B) hides names, pictures and messages until you point at them. It can
  also come on whenever Natter Den is in the background.
- App lock: a passcode when Natter Den starts, when its window closes, or after it's been idle;
  Ctrl+L locks it now. While it's locked, or with privacy blur on, notifications don't say who
  wrote or what.
- Microphone and camera switches in Preferences → Privacy, the main menu and Ctrl+K.
- Several accounts, each with its own session; Ctrl+1 to Ctrl+9 switch between them.
- A tray icon whose menu shows or hides the window, turns privacy blur on or off, pauses
  notifications, locks (with a passcode set) and quits. Closing the window keeps Natter Den
  running there, and it can start hidden at login.
- While the window is closed, it asks the kernel to take back memory WhatsApp isn't using, and a
  guard ends and reloads a WhatsApp page that keeps growing ([details](docs/memory.md)).
- Chats by name for desktop search (`org.gnome.Shell.SearchProvider2`), used by Ada's search
  ([docs/ada.md](docs/ada.md)).

Experimental:

- **Reply from a notification.** Reply opens a small window with the chat's latest messages.
  Natter Den puts your words in WhatsApp's message box and presses its Send button once. It stops
  before Send, keeping your words, when the open chat isn't the right one, there's a draft in the
  box, or something else is open over the chat ([how](docs/how-it-works.md#replies-from-notifications)).
  *Show Reply Button* in Preferences → Notifications turns it off.
- **Typing the reply in the notification**, where the notification server offers KDE's inline
  reply. Not tried yet on any desktop; Ada's notifications don't offer it.
- **Calls**, with WhatsApp's own call buttons, in Natter Den's window when GStreamer's WebRTC
  plugins are installed. Without them, calls go to a separate call window if Qt WebEngine for
  Python (PyQt6) is installed, linked to your phone as one more device; otherwise to your browser.
- **A camera fix** for GStreamer 1.28 and newer, where WebKit's camera picture freezes on its first
  frame ([WebKit bug 324582](https://bugs.webkit.org/show_bug.cgi?id=324582)): a small GStreamer
  plugin, built by `install.sh` only when a C compiler, `pkg-config` and GStreamer's headers are
  there (on Ubuntu and Debian: `sudo apt install gcc libc6-dev pkg-config libgstreamer1.0-dev`,
  then run the install command again). `NATTER_CAMFIX=0` turns it off.

## Use

```sh
natter                       # open, or bring the window forward
natter --background          # start hidden: notifications and the tray still work
natter --chat "Mum"          # open a chat by name
natter https://wa.me/1555…   # open a chat by number
natter photo.jpg report.pdf  # send files: pick the chat, then caption and send
```

Ctrl+? lists the keyboard shortcuts.

## Requirements

Python 3.9 or newer with PyGObject, GStreamer for Python, and at least:

| | Oldest |
|---|---|
| GTK | 4.14 |
| libadwaita | 1.5 |
| WebKitGTK (6.0 API) | 2.42 |
| PyGObject | 3.42 |
| libsoup | 3.0 |
| GdkPixbuf | 2.0 |

Natter Den checks these when it starts and says what's missing. WhatsApp's videos and voice
messages need GStreamer's codecs for H.264, AAC and Opus; calls in the window need GStreamer's
WebRTC plugins.

`install.sh` knows the package names for apt, dnf, pacman and zypper. From their package
versions, these should be new enough: Ubuntu 24.04, Debian 13, Fedora 40, RHEL 10 and its
rebuilds, openSUSE Tumbleweed and Leap 16, Arch, and their newer releases and derivatives.
Ubuntu 22.04, Debian 12 and openSUSE Leap 15 are too old; use the [Flatpak](#flatpak) there, and
on Mageia and OpenMandriva, whose packages `install.sh` can't name. So far Natter Den has only
been run on Ada 26.04, which is based on Ubuntu.

## Known limitations

- Only tried on Ada. The desktop features use common interfaces
  ([listed here](docs/how-it-works.md#what-it-asks-of-the-desktop)), but nothing has been tried on
  GNOME, KDE Plasma, Xfce or Sway yet.
- Natter Den works through WhatsApp Web's page. When a WhatsApp update moves something it relies
  on, the feature that leans on it stops, and the log names what it couldn't find
  ([docs/development.md](docs/development.md#when-whatsapp-changes)).
- Only `whatsapp://` links are registered with the desktop: `https://wa.me/…` and other WhatsApp
  links clicked in other apps open in your browser.
- GNOME Shell reads search providers only from the system's folders, not `~/.local/share`, so an
  install made by `get.sh` doesn't appear in GNOME's search.
- Without a tray (a StatusNotifierItem host), closing the window quits Natter Den unless *Keep
  Running When Closed* is on.
- Giving memory back while hidden needs zram or zswap, and a systemd user session.
- On Ada, `ada-lifecycled` pauses apps that stay hidden, and a paused Natter Den gets no messages
  until it's opened. Adding `io.github.Officialbabs.Natter` to `AllowBackground` in
  `~/.config/os-ai/lifecycle.ini` keeps it running ([docs/ada.md](docs/ada.md)).
- If WhatsApp's page keeps growing (one did while a video was being attached), the memory guard
  ends and reloads it. Ended again within ten minutes, it stays down until you press *Try Again*.
- Without GStreamer's codecs some videos and voice messages don't play. On Fedora, H.264 needs
  the fedora-cisco-openh264 repository or RPM Fusion.
- The Qt WebEngine call window holds the first account only; calls from other accounts open in
  your browser.

## Privacy

- Natter Den's window shows only `https://web.whatsapp.com`. Pages from anywhere else are neither
  shown nor saved there; links to other sites open in your browser.
- Each account's WhatsApp session is a WebKit session of its own, kept in
  `~/.local/share/natter/accounts/`. Settings are in `~/.config/natter` and caches in
  `~/.cache/natter`. These folders are readable by you only.
- For Ctrl+K, the dock menu and desktop search, Natter Den keeps the names of up to 500 chats per
  account in `chats.json`, and no message text. The dock menu's five are also written into Natter
  Den's desktop entry in `~/.local/share/applications`, readable by you only.
- The app lock's passcode is kept as a salted PBKDF2 hash. The lock hides WhatsApp from someone at
  your computer; it doesn't encrypt anything.
- Natter Den's scripts don't read or write WhatsApp's storage (localStorage, sessionStorage,
  IndexedDB), and a theme doesn't change WhatsApp's own settings. Removing an account in
  Preferences deletes that account's session and folders.
- Its log says how each reply ended, not who it went to or what it said, and lists files handed
  to WhatsApp by number, size and type, not by name.
- A notification's id and its Reply and Mute actions carry a random reference, not the chat's
  name.

## Tests

`python3 -m unittest discover -s tests`; [docs/development.md](docs/development.md) has more.

## More

- [How it works](docs/how-it-works.md): the page scripts, notifications, replies, themes, WebKit
  settings and process grouping
- [Memory](docs/memory.md): how Natter Den keeps its memory down
- [Packaging](docs/packaging.md): installing by hand, the Flatpak and meson builds, moving from
  `io.natter.Natter`, the Flathub checklist
- [Development](docs/development.md): when WhatsApp changes, the test page, debug options
- [Natter Den on Ada](docs/ada.md)

## License

GPL-3.0-or-later. See [COPYING](COPYING).
