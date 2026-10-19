# Natter

WhatsApp for Linux that feels like part of your desktop. Natter shows WhatsApp Web in
a native GTK 4 / libadwaita window and connects it to the rest of the desktop:
notifications, the dock, the tray and the keyboard.

Natter isn't made by or affiliated with WhatsApp or Meta. It runs the real WhatsApp Web
and nothing else, the same way your browser would.

## What it does that a browser tab (or ZapZap) doesn't

| | |
|---|---|
| **Native and light** | libadwaita window that follows Ada's light/dark style. WebKitGTK instead of a bundled Chromium. |
| **One notification per chat** | New messages replace that chat's notification and add a count, with the sender's picture. Clicking one opens that exact chat. *Mute for an hour* is right on the notification. |
| **Quiet hours** | No notifications in a time window you choose; the unread count still updates. |
| **Unread badge** | On the dock icon, in the tray and on each account's tab. |
| **Ctrl+K** | Jump to any chat by typing part of its name. Type a phone number to message someone without saving them. Commands too. |
| **Recent chats in the dock** | Right-click Natter's dock icon to open one of your five most recent chats. |
| **Send files from anywhere** | Drop files on Natter's dock icon (or `natter FILE…`), pick the chat, and WhatsApp shows its preview with a caption box. |
| **wa.me links** | `wa.me/…`, `api.whatsapp.com/send…` and `whatsapp://` links open in Natter, both from other apps and from inside chats. Other links open in your browser. |
| **Privacy blur** | Ctrl+Shift+B blurs names, pictures and messages until you point at them. It can also blur everything while Natter is in the background. |
| **Multiple accounts** | Each with its own session. Ctrl+1, Ctrl+2… switch between them. |
| **Stays out of the way** | Closing the window keeps Natter in the tray. It can start hidden at login. |

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
box and clicks the result. Files go in as a drop on the open chat, from `natter://` links that
only work for files you picked.

The tray is a StatusNotifierItem with a dbusmenu, the badge uses the Unity LauncherEntry API,
and the dock menu is written as `[Desktop Action]` entries into Natter's own `.desktop` file.

## When WhatsApp changes

The bridge depends on a few landmarks in WhatsApp Web's page: `#pane-side`, `#main header`,
the search box in `#side`, and `span[title]` for names. If Ctrl+K stops opening chats after a
WhatsApp update, those selectors are where to look. `tests/mock-whatsapp.html` copies those
landmarks, so you can try changes without your account:

```sh
NATTER_DEBUG=1 bin/natter --url "file://$PWD/tests/mock-whatsapp.html"
```

`NATTER_DEBUG=1` also turns on WebKit's inspector (right-click → Inspect Element).

## Tests

```sh
python3 -m unittest discover -s tests
```
