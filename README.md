# Natter

WhatsApp for Linux that feels like part of your desktop. Natter shows WhatsApp Web in
a native GTK 4 / libadwaita window and connects it to the rest of the desktop:
notifications, the dock, the tray and the keyboard.

Natter isn't made by or affiliated with WhatsApp or Meta. It runs the real WhatsApp Web
and nothing else, the same way your browser would.

## What it does that a browser tab (or ZapZap) doesn't

| | |
|---|---|
| **Native** | libadwaita window that follows Ada's light/dark style, on the system's WebKitGTK instead of a bundled Chromium. |
| **One notification per chat** | New messages replace that chat's notification and add a count, with the sender's picture. Clicking one opens that exact chat. *Mute for an hour* is right on the notification. |
| **Quiet hours** | No notifications in a time window you choose; the unread count still updates. |
| **Unread badge** | On the dock icon, in the tray and on each account's tab. |
| **Ctrl+K** | Jump to any chat by typing part of its name. Type a phone number to message someone without saving them. Commands too. |
| **Recent chats in the dock** | Right-click Natter's dock icon to open one of your five most recent chats. |
| **Paste and drop pictures and files** | Ctrl+V a screenshot, an image copied from any app, or files copied in the file manager, or drag files onto a chat, and WhatsApp shows its preview with a caption box. With no chat open, Natter asks where to send them. |
| **Send files from anywhere** | Drop files on Natter's dock icon (or `natter FILE…`), pick the chat, and WhatsApp shows its preview with a caption box. |
| **wa.me links** | `wa.me/…`, `api.whatsapp.com/send…` and `whatsapp://` links open in Natter, both from other apps and from inside chats. Other links open in your browser. |
| **Privacy blur** | Ctrl+Shift+B blurs names, pictures and messages until you point at them. It can also blur everything while Natter is in the background. |
| **Multiple accounts** | Each with its own session. Ctrl+1, Ctrl+2… switch between them. |
| **Stays out of the way** | Closing the window keeps Natter in the tray. It can start hidden at login. |
| **Light in the background** | While the window is closed, Natter hands memory WhatsApp isn't using back to the system, and keeps running so messages still arrive. |
| **One process group** | Natter and its WebKit processes run in one systemd scope (`app-io.natter.Natter-<pid>.scope`), however it was started, so the Task Manager shows them as one app. |
| **Desktop search, ready** | Answers `org.gnome.Shell.SearchProvider2` with your chats by name, for desktops that ask apps for search results. See [docs/ada.md](docs/ada.md). |

## Install

Natter needs PyGObject with GTK 4, libadwaita and WebKitGTK 6.0. For calls, it also needs
GStreamer's WebRTC plugins:

```sh
sudo apt-get install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 \
                     gstreamer1.0-plugins-bad gstreamer1.0-nice
./install.sh            # adds Natter to your apps and `natter` to ~/.local/bin
```

Open Natter and scan the code with WhatsApp on your phone (*Settings → Linked devices*).
`./install.sh --uninstall` removes the launcher; your sessions stay in `~/.local/share/natter`
until you delete that folder.

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
`~/.local/share/natter/accounts/<id>/`. A small script (`natter/bridge.js`) runs in
WhatsApp Web before WhatsApp's own code and tells Natter:

- **notifications**: WhatsApp's `new Notification(…)` comes to Natter, which shows it over
  `org.freedesktop.Notifications`. A click goes back to the same object, so WhatsApp's own
  handler opens the chat.
- **the chat list**: the names of the chats WhatsApp shows, and their order. Natter keeps
  the names (never message text) in `chats.json` for Ctrl+K and the dock menu. WhatsApp
  only keeps the visible rows in the page, so older chats show up once you've scrolled past them.
- **the unread count**: from the page title.

To open a chat by name, the bridge clicks its row, or types the name into WhatsApp's search
box and clicks the result. Files go in as a paste into the message box (Ctrl+V) or a drop on the
open chat. WhatsApp's Content-Security-Policy won't let its page fetch anything from Natter, so
Natter hands the bytes over itself, 4 MB per call, and the bridge puts the file back together.
Files dragged onto the window are caught by Natter (WhatsApp's page is sandboxed and can't read
them), and the bridge hides that drag from WhatsApp, which would otherwise show its drop zone
for a drop that never arrives and then ignore the files Natter hands it.

The tray is a StatusNotifierItem with a dbusmenu, the badge uses the Unity LauncherEntry API,
and the dock menu is written as `[Desktop Action]` entries into Natter's own `.desktop` file.

## WebKit settings, and why

- **No service worker.** WhatsApp's service worker passes every download through itself, and in
  WebKitGTK (2.52) some arrive damaged: emoji sprite sheets drew as slivers, blank cells and
  magnified squares, different ones each start. Without it they draw whole. It only does
  offline caching, which a desktop app doesn't need.
- **A disk cache** (`DOCUMENT_BROWSER`), so WhatsApp's code isn't downloaded on every start: with
  it, the chat list is up in about 15 s instead of 25–150 s.
- **No process swapping or back/forward cache**: one site, no history.

## Memory and CPU

Measured on one account (Oct 2026, WebKitGTK 2.52, two minutes after start): ZapZap used
about 980 MB and 0% CPU when idle. Natter uses about 1.1–1.2 GB. Most of that is WhatsApp's
own page, which WebKit's JavaScript engine holds in more memory than Chromium's does. Two
things keep it down:

- **A 1 GB WebKit memory limit**, so WebKit frees caches and collects garbage early. It took
  Natter from ~1.5 GB to ~1.1 GB at the same CPU. A tighter 640 MB limit was worse: WebKit
  collected constantly and ended up bigger. `NATTER_MEMORY_LIMIT=<MB>` overrides it (0 = off).
- **Idle audio**: WhatsApp keeps a Web Audio context open, and in WebKitGTK an open context
  keeps a GStreamer pipeline running all day. The bridge suspends it after 30 s of silence.
- **Memory back while hidden**: 90 s after the window closes (and every 15 min after), Natter
  asks the kernel to reclaim its scope's cold memory in 64 MB steps (`memory.reclaim`), the
  way Ada does for paused apps, down to 300 MB. Measured: 1,231 MB → 385 MB in 15 s, settling
  around 570 MB as WhatsApp pulls back what it uses. It goes to zswap first, so it's
  compressed, not gone to disk. Preferences → *Use Less Memory in the Background*.
- **No spare processes**: process swapping and the back/forward cache are off (one site, no
  history). WhatsApp's cross-origin isolation headers still make WebKit start a fresh process,
  leaving the first, blank one idle: about 100 MB, all of it cold, so the reclaim takes it.

Right after linking a phone, WhatsApp spends a while writing your history into its database,
which shows up as CPU in WebKit's network process (`persist…` thread) and page workers.

## When WhatsApp changes

The bridge depends on a few landmarks in WhatsApp Web's page: `#pane-side`, `#main header`,
the search box in `#side`, and `span[title]` for names. If Ctrl+K stops opening chats after a
WhatsApp update, those selectors are where to look. `tests/mock-whatsapp.html` copies those
landmarks, so you can try changes without your account:

```sh
NATTER_DEBUG=1 bin/natter --url "file://$PWD/tests/mock-whatsapp.html"
```

`NATTER_DEBUG=1` also turns on WebKit's inspector (right-click → Inspect Element), prints the
page's console, and adds two D-Bus actions for poking at a running Natter: `debug-snapshot`
saves the window as a PNG, `debug-eval` runs JavaScript in the page. Any program of yours can
call those, so don't leave a debug Natter running.

## Process names and grouping

`natter` is a Python script (not a shell wrapper), so the main process is called `natter`.
At start-up, if Natter isn't already in a scope of its own (Ada's launcher makes
`app-ada-io.natter.Natter-<pid>.scope`), it asks systemd for `app-io.natter.Natter-<pid>.scope`
before WebKit starts any process, so every WebKit process, sandbox and proxy joins it.

## Tests

```sh
python3 -m unittest discover -s tests
```
