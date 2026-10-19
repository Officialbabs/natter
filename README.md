# Natter

WhatsApp for Linux that feels like part of your desktop. Natter shows WhatsApp Web in
a native GTK 4 / libadwaita window and connects it to the rest of the desktop:
notifications, the dock, the tray and the keyboard. It works on GNOME, KDE Plasma, Xfce,
Sway, Ada and the rest, installed from a checkout or as a Flatpak.

Natter is unofficial: it isn't made by, endorsed by or affiliated with WhatsApp or Meta. It
runs the real WhatsApp Web and nothing else, the same way your browser would.

## What it does that a browser tab (or ZapZap) doesn't

| | |
|---|---|
| **Native** | libadwaita window in the system's light or dark style (or the one you pick), on the system's WebKitGTK instead of a bundled Chromium. |
| **One notification per chat** | New messages replace that chat's notification and add a count, with the sender's picture. Clicking one opens that exact chat. *Mute for an hour* is right on the notification. |
| **Quiet hours** | No notifications in a time window you choose; the unread count still updates. |
| **Unread count** | In the tray, on each account's tab, and on the dock icon where the dock shows badges. |
| **Ctrl+K** | Jump to any chat by typing part of its name. Type a phone number to message someone without saving them. Commands too. |
| **Recent chats in the dock** | Right-click Natter's dock icon to open one of your five most recent chats. |
| **Paste and drop pictures and files** | Ctrl+V a screenshot, an image copied from any app, or files copied in the file manager, or drag files onto a chat, and WhatsApp shows its preview with a caption box. With no chat open, Natter asks where to send them. |
| **Send files from anywhere** | Drop files on Natter's dock icon (or `natter FILE…`), pick the chat, and WhatsApp shows its preview with a caption box. |
| **wa.me links** | `wa.me/…`, `api.whatsapp.com/send…` and `whatsapp://` links open in Natter, both from other apps and from inside chats. Other links open in your browser. |
| **Privacy blur** | Ctrl+Shift+B blurs names, pictures and messages until you point at them. It can also blur everything while Natter is in the background. |
| **Multiple accounts** | Each with its own session. Ctrl+1, Ctrl+2… switch between them. |
| **Stays out of the way** | Closing the window keeps Natter in the tray. It can start hidden at login. |
| **Lighter** | About a third less memory than ZapZap with the same account, at the same idle CPU ([measured](#natter-and-zapzap-side-by-side)). |
| **Light in the background** | While the window is closed, Natter hands memory WhatsApp isn't using back to the system, and keeps running so messages still arrive. |
| **One process group** | Natter and its WebKit processes run in one systemd unit, however it was started, so the task manager shows them as one app. |
| **Desktop search** | Answers `org.gnome.Shell.SearchProvider2` with your chats by name, for desktops that ask apps for search results. See [docs/ada.md](docs/ada.md). |

**Calls** happen in Natter, with WhatsApp's own call buttons in the chat. WebKitGTK as
distributions build it has no WebRTC, but WhatsApp's calls only use one part of it, data
channels: its own call engine makes and encrypts the sound and pictures in the page. Natter
gives the page those data channels, done by GStreamer, so calls run in the same WhatsApp page
as everything else, with no second browser behind it. That needs GStreamer's WebRTC plugins
(see [Install](#from-a-checkout)); without them, calls open in your browser. Calls are new:
voice calls work, and video calls are still being finished.

## On your desktop

Natter speaks the freedesktop standards, so most of it works everywhere. What depends on the
desktop:

| | GNOME | KDE Plasma | Ada | Xfce | Sway |
|---|---|---|---|---|---|
| Notifications, one per chat, click to open | ✓ | ✓ | ✓ | ✓ | ✓ with a notification daemon (mako, dunst, SwayNC) |
| Tray icon with unread count and menu | with the AppIndicator extension (on by default in Ubuntu) | ✓ | ✓ | ✓ Status Tray plugin | ✓ Waybar's tray; swaybar's shows the icon and opens Natter on a click, but has no menu |
| Unread badge on the dock icon | Ubuntu Dock, Dash to Dock | ✓ | ✓ | – | – |
| Recent chats in the dock's right-click menu | ✓ | ✓ | ✓ | – | – |
| Chats in desktop search | ✓ Activities search, while Natter runs | – | ready ([docs/ada.md](docs/ada.md)) | – | – |
| Start hidden at login | ✓ | ✓ | ✓ | ✓ | if your session runs XDG autostart (`dex -a`, systemd's autostart generator) |
| Window comes forward from a notification | ✓ | ✓ | ✓ | ✓ | if the daemon passes an activation token |
| One process group, memory back while hidden | ✓ | ✓ | ✓ | ✓ with a systemd user session | ✓ with a systemd user session |

Without a tray, a Natter running in the background would be out of sight, so on a desktop
with no tray host closing the window quits it, unless you've turned *Keep Running When
Closed* on yourself.

Giving memory back while hidden also needs cheap swap: zram (Fedora's default) or zswap.
Without either, pages would go to a swap file or partition on disk and come back slowly, so
Natter leaves memory alone.

In the Flatpak, notifications go through the notification portal, start at login goes
through the Background portal (where the desktop's portals offer it, as GNOME's and KDE's do;
elsewhere the switch turns itself back off), and the dock badge and recent chats in the dock
menu aren't available (the tray shows the count). Desktop search works the same.

## Install

### Flatpak

The Flatpak brings its own GTK, libadwaita, WebKitGTK and codecs, so it runs on any
distribution, old ones included. To build and install it from a checkout:

```sh
flatpak install flathub org.gnome.Sdk//50 org.gnome.Platform//50
flatpak-builder --user --install --force-clean build-flatpak packaging/io.github.Officialbabs.Natter.yml
flatpak run io.github.Officialbabs.Natter
```

### From a checkout

Natter needs Python 3 with PyGObject and these, or newer:

| | Oldest |
|---|---|
| GTK | 4.14 |
| libadwaita | 1.5 |
| WebKitGTK (6.0 API) | 2.42 |
| PyGObject | 3.42 |
| libsoup | 3.0 |
| GdkPixbuf | 2.0 |

Ubuntu 24.04, Debian 13, Fedora 40, Arch and openSUSE Tumbleweed have them (and their newer
releases and derivatives: Linux Mint 22, Pop!_OS 24.04 and so on). On Debian 12, Ubuntu 22.04
or openSUSE Leap 15, use the Flatpak. Use it too on systems that install apps as Flatpaks:
Fedora Atomic (Silverblue, Kinoite…), Bazzite and SteamOS (on Fedora Atomic, `rpm-ostree install`
can layer the packages instead), and on Mageia or OpenMandriva, whose packages Natter doesn't
know by name. Natter checks when it starts and says what's missing.

The packages, with the GStreamer codecs WhatsApp's videos (H.264, AAC) and voice messages
(Opus) need, and GStreamer for Python, which Natter uses to check for those codecs:

```sh
# Debian, Ubuntu and derivatives
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 gir1.2-soup-3.0 \
                 gir1.2-gdkpixbuf-2.0 gir1.2-gstreamer-1.0 gstreamer1.0-libav gstreamer1.0-plugins-good
# Fedora (GStreamer for Python comes with gstreamer1)
sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 libsoup3 \
                 gstreamer1-plugin-openh264 gstreamer1-plugin-libav
# Arch and derivatives (GStreamer for Python comes with gstreamer)
sudo pacman -S --needed python-gobject gtk4 libadwaita webkitgtk-6.0 libsoup3 gst-libav gst-plugins-good
# openSUSE
sudo zypper install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 \
                    typelib-1_0-WebKit-6_0 typelib-1_0-Soup-3_0 typelib-1_0-Gst-1_0 \
                    gstreamer-plugin-openh264 gstreamer-plugins-libav
```

For calls inside Natter, also GStreamer's WebRTC parts and their Python bindings: the "bad"
plugins (webrtcbin, SCTP, DTLS, SRTP), libnice's GStreamer plugin, and the GstWebRTC and GstSdp
introspection data. Without them Natter still runs, and calls open in your browser.

```sh
# Debian, Ubuntu and derivatives
sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-nice gir1.2-gst-plugins-bad-1.0 gir1.2-gst-plugins-base-1.0
```

Then:

```sh
./install.sh            # adds Natter to your apps and `natter` to ~/.local/bin
```

`install.sh` picks the Python that has everything (the system's `/usr/bin/python3` first),
writes `~/.local/bin/natter` for it, pointing at this checkout, and installs the menu entry,
icons, desktop search files and the `whatsapp://` handler (unless another app already has
it). It says if `~/.local/bin` isn't on your `PATH`, or if codecs are missing. While Natter is
running it stops and asks you to quit it first; `--force` goes ahead anyway.

Open Natter and scan the code with WhatsApp on your phone (*Settings → Linked devices*).
`./install.sh --uninstall` removes it all; your sessions stay in `~/.local/share/natter`
until you delete that folder.

### Packages

`meson.build` installs Natter the way distribution packages want it: the `natter` package in
`/usr/share/natter`, the `natter` command, desktop file, icons, AppStream metainfo, D-Bus
service and search provider. The Flatpak builds with it too.

```sh
meson setup build --prefix=/usr && meson install -C build
```

### Coming from io.natter.Natter

Natter's app id is now `io.github.Officialbabs.Natter` (it was `io.natter.Natter`). Quit the
old Natter first (*Quit* in its tray menu, or Ctrl+Q in its window): `./install.sh` stops while
it's running, since both would use the same WhatsApp session. Then run `./install.sh` again: it
removes the old menu entry, icons and search files, moves start-at-login over to the new name,
and moves the `whatsapp://` handler if it was Natter's. Your accounts, chats and settings stay
where they were, so there's nothing to link again. Docks remember apps by id, so pin Natter
again. (With `./install.sh --force` and the old Natter still running, the new one won't open
WhatsApp until the old one quits: it brings the old window forward instead.)

On Ada, if you let the old Natter run in the background by adding `io.natter.Natter` to
`AllowBackground` in `~/.config/os-ai/lifecycle.ini` (Ada's background allow list), replace it
there with `io.github.Officialbabs.Natter`. `./install.sh` reminds you when the old id is still
in that file; it doesn't change the file itself.

## Use

```sh
natter                       # open (or bring forward)
natter --background          # start hidden: notifications and the tray still work
natter --chat "Mum"          # open a chat by name
natter https://wa.me/1555…   # open a chat by number
natter photo.jpg report.pdf  # send files: pick the chat, then caption and send
```

| Keys | |
|---|---|
| Ctrl+K | Go to chat, message a number, run a command |
| Ctrl+Shift+B | Privacy blur |
| Ctrl+1 … Ctrl+9 | Switch account |
| Ctrl+, | Preferences |
| Ctrl+plus / minus / 0 | Zoom |
| Ctrl+W / Ctrl+Q | Hide / quit |

WhatsApp's own shortcuts (Ctrl+Alt+N for a new chat, and so on) work as usual.

## How it works

Each account is a WebKit network session with its own folder in
`~/.local/share/natter/accounts/<id>/`. Natter's folders (`~/.config/natter`,
`~/.local/share/natter`, `~/.cache/natter`) are readable by you only, and its settings and
chat lists are written whole, so a crash never leaves half a file. A small script
(`natter/bridge.js`) runs in WhatsApp Web before WhatsApp's own code and tells Natter:

- **notifications**: WhatsApp's `new Notification(…)` comes to Natter, which shows it over
  `org.freedesktop.Notifications` (or the notification portal in the Flatpak). A click goes back
  to the same object, so WhatsApp's own handler opens the chat.
- **the chat list**: the names of the chats WhatsApp shows, and their order. Natter keeps
  the names (never message text) in `chats.json` for Ctrl+K, the dock menu and desktop search.
  WhatsApp only keeps the visible rows in the page, so older chats show up once you've scrolled
  past them.
- **the unread count**: from the page title.
- **calls**: a press on WhatsApp's call buttons, which Natter hands to the browser.

To open a chat by name, the bridge clicks its row, or types the name into WhatsApp's search
box and clicks the result. Files go in as a paste into the message box (Ctrl+V) or a drop on the
open chat. WhatsApp's Content-Security-Policy won't let its page fetch anything from Natter, so
Natter hands the bytes over itself, 4 MB per call, and the bridge puts the file back together.
Files dragged onto the window are caught by Natter (WhatsApp's page is sandboxed and can't read
them), and the bridge hides that drag from WhatsApp, which would otherwise show its drop zone
for a drop that never arrives and then ignore the files Natter hands it.

The tray is a StatusNotifierItem with a dbusmenu, the badge uses the Unity LauncherEntry API,
and the dock menu is written as `[Desktop Action]` entries into Natter's own `.desktop` file in
`~/.local/share/applications` (never into a package's or the Flatpak's), naming chats by a
hash rather than by name on the command line.

Desktop search answers from the chat lists on disk: a search doesn't start WhatsApp. The
provider file says `AutoStart=false`, so the desktop only asks a Natter that's already running.

## WebKit settings, and why

- **No service worker.** WhatsApp's service worker passes every download through itself, and in
  WebKitGTK (2.52) some arrive damaged: emoji sprite sheets drew as slivers, blank cells and
  magnified squares, different ones each start. Without it they draw whole. It only does
  offline caching, which a desktop app doesn't need.
- **A disk cache** (`DOCUMENT_BROWSER`), so WhatsApp's code isn't downloaded on every start: with
  it, the chat list is up in about 15 s instead of 25–150 s.
- **No process swapping or back/forward cache**: one site, no history.
- **Hardware acceleration** on, unless you turn it off in Preferences → *Troubleshooting* (for a
  blank or flickering page on some graphics drivers).

## Memory and CPU

### Natter and ZapZap side by side

Both running at once with the same WhatsApp account, idle, no calls, on one laptop (Intel
Core 5 120U, 16 GB, swap with zswap) on 19 October 2026: ZapZap 7.5.1 (Flatpak, Qt WebEngine
on org.kde.Platform 6.11) and Natter 0.1.0 (WebKitGTK 2.52.6).

| | ZapZap | Natter |
|---|---|---|
| In memory (PSS) | 1,236 MB | 1,000 MB |
| Moved out to swap | 680 MB | 215 MB |
| **Total** | **1,916 MB** | **1,215 MB** |
| CPU while idle (over 30 s) | 2.0% | 1.6% |
| Biggest process | web engine renderer, 1,044 MB | WebKit web process, 880 MB |
| The app's own process | 115 MB | 53 MB |

Natter used about 700 MB (37%) less memory at the same idle CPU. Just after starting the gap
was the same: 90 seconds in, ZapZap used 1,964 MB; Natter, 12 minutes in, 1,197 MB.

How it was measured: every process in each app's systemd scopes (ZapZap's Flatpak runs its
web engine in scopes of their own, which count too), memory as PSS from
`/proc/<pid>/smaps_rollup` plus what was swapped out, CPU as user and system time over the
window. `tools/compare-memory.py` does all of it. It's one machine and one account: chat
history, open chats and media all change the numbers, for both apps.

### What keeps Natter's down

Most of Natter's memory is WhatsApp's own page.

- **A 1,536 MB WebKit memory limit**, checked every 30 s: WebKit frees caches past 55% of it
  and collects garbage hard past 80%. (An earlier 1 GB limit took Natter from ~1.5 GB to
  ~1.1 GB at the same CPU; a tighter 640 MB limit was worse: WebKit collected constantly and
  ended up bigger.) `NATTER_MEMORY_LIMIT=<MB>` overrides the limit (0 = WebKit's own) and
  `NATTER_MEMORY_THRESHOLDS=0.55,0.8` the two thresholds.
- **Idle audio**: WhatsApp keeps a Web Audio context open, and in WebKitGTK an open context
  keeps a GStreamer pipeline running all day. The bridge suspends it after 30 s of silence.
- **Memory back while hidden**: 90 s after the window closes (and every 15 min after), Natter
  asks the kernel to reclaim its unit's cold memory in 64 MB steps (`memory.reclaim`), the
  way Ada does for paused apps, down to 300 MB. Measured: 1,231 MB → 385 MB in 15 s, settling
  around 570 MB as WhatsApp pulls back what it uses. It only does this where swapping is cheap
  (zram, or zswap in front of the swap file), so the memory is compressed, not gone to disk,
  and not where the unit's swap is turned off. Preferences → *Use Less Memory in the Background*.
- **No spare processes**: process swapping and the back/forward cache are off (one site, no
  history). WhatsApp's cross-origin isolation headers still make WebKit start a fresh process,
  leaving the first, blank one idle: about 100 MB, all of it cold, so the reclaim takes it.

Right after linking a phone, WhatsApp spends a while writing your history into its database,
which shows up as CPU in WebKit's network process (`persist…` thread) and page workers.

## When WhatsApp changes

The bridge finds its way around WhatsApp Web's page by a few landmarks. When a WhatsApp update
moves one, the feature that leans on it stops (Ctrl+K stops opening chats, say), and this list
says where to look in `natter/bridge.js`:

| Landmark | What leans on it |
|---|---|
| `#pane-side` and its rows (`[role="listitem"]`, `[role="row"]`) | the chat list, Ctrl+K, the dock menu, desktop search |
| `[data-testid="cell-frame-title"]` in a row (else its `span[title]`) | chat names |
| `#main header`, with the title in `[data-testid="conversation-info-header-chat-title"]` | which chat is open |
| the link screen: a `canvas` or `[data-ref]` in `#app` (the QR code), with no `#pane-side` | telling the link screen from a page still loading |
| filter chips: `[role="tab"][aria-selected]` in `#side` | not taking a filtered list's order for the recent chats |
| call buttons in `#main header`, by their label, title or icon name ("call", "video") | calls opening in the browser |
| the search box in `#side`, and the message box in `#main footer` | opening chats by name, pasting files |

The link screen, filter chips and call buttons are the least sure: so far only the mock
confirms them. `tests/mock-whatsapp.html` copies these landmarks, so you can try changes
without your account. Run it as a separate instance, with its own app id and folders, so it
doesn't touch the Natter you use:

```sh
NATTER_APP_ID=io.github.Officialbabs.Natter.Dev XDG_CONFIG_HOME=/tmp/nd/config \
XDG_DATA_HOME=/tmp/nd/data XDG_CACHE_HOME=/tmp/nd/cache \
NATTER_DEBUG=1 bin/natter --url "file://$PWD/tests/mock-whatsapp.html"
```

Any `NATTER_APP_ID` other than Natter's own gets folders of its own too (`natter-dev` here).
`NATTER_DEBUG=1` also turns on WebKit's inspector (right-click → Inspect Element), prints the
page's console, and adds D-Bus actions for poking at a running Natter: `debug-snapshot` and
`debug-page-snapshot` save a PNG (only into the instance's cache folder, under `snapshots/`),
`debug-eval` runs JavaScript in the page, `debug-attach` and `debug-drop` hand it a file.
`--url` only works for a separate app id like the one above. On Natter's own app id the
debug actions need the token Natter prints at start (`natter: debug token …`) in front of
their argument (`"<token>:return document.title"`), but they still reach your logged-in
WhatsApp, so don't leave a debug Natter running.

## Flathub checklist

What's still to do before submitting Natter to Flathub:

- **Screenshots.** The metainfo (`data/io.github.Officialbabs.Natter.metainfo.xml`) has no
  `<screenshots>` yet, and Flathub requires them: images at stable URLs (a tagged commit or a
  release, not a branch that moves), showing no one's real chats, names or numbers.
- **The source.** The manifest builds from this checkout (`type: dir`); Flathub's needs a `git`
  source with a tag and commit.
- **Checks.** `meson test -C build` runs `appstreamcli validate` and `desktop-file-validate`;
  Flathub's own linter is `flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest
  packaging/io.github.Officialbabs.Natter.yml`.

## Process names and grouping

`natter` is a Python script (not a shell wrapper), so the main process is called `natter`.
Its program name is the app id, so on X11 the window's `WM_CLASS` matches `StartupWMClass` in
the desktop file and the dock puts the window under Natter's icon.

Desktops start apps in a systemd unit of their own: `app-gnome-<app id>-<pid>.scope` on
GNOME, `app-ada-…` on Ada, `app-<app id>@<random>.service` on KDE Plasma,
`app-flatpak-<app id>-<pid>.scope` for a Flatpak. When Natter finds itself in some other unit
(started from a terminal, autostart or a script), it asks systemd for
`app-io.github.Officialbabs.Natter-<pid>.scope` before WebKit starts any process, so every
WebKit process, sandbox and proxy joins it. Without a systemd user manager, or inside a
sandbox, it leaves things as they are.

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

GPL-3.0-or-later. See [COPYING](COPYING).
