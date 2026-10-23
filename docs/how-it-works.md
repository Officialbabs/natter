# How Natter Den works

Natter Den is a GTK 4 and libadwaita app in Python. Its window holds a WebKitGTK view of
`https://web.whatsapp.com`, and two scripts of its own in that page connect WhatsApp Web to the
desktop.

## Accounts and folders

Each account is a WebKit network session of its own, with its data in
`~/.local/share/natter/accounts/<id>/` and its cache in `~/.cache/natter/accounts/<id>/`.
Settings are in `~/.config/natter/settings.json`. Natter Den's folders (`~/.config/natter`,
`~/.local/share/natter`, `~/.cache/natter`) are readable by you only, and its settings and chat
lists are written to a temporary file that then replaces the old one, so a crash mid-write leaves
the old file rather than half of the new one.

## The page scripts

`natter/bridge.js` runs in WhatsApp Web's top frame before WhatsApp's own code (WebKit's user
scripts, at document start, on `https://web.whatsapp.com/*` only). `natter/rtc.js` runs there too:
it puts ceilings on what the page's video decoders and encoders hold, and with calls in the page it
stands in for WebRTC (below). A user style sheet, `natter/style.css`, adds the privacy blur and
Natter Den's other touches, each hung off a class Natter Den sets on `<html>`. The bridge tells
Natter Den:

- **Notifications:** WhatsApp's `new Notification(…)` comes to Natter Den, which shows it over
  `org.freedesktop.Notifications`, or through GNotification (the notification portal) inside a
  sandbox. A click goes back to the same object, so WhatsApp's own handler opens the chat.
- **The chat list:** the names of the chats WhatsApp shows, and their order. Natter Den keeps the
  names (no message text) in `chats.json`, up to 500 per account, for Ctrl+K, the dock menu and
  desktop search. WhatsApp only keeps the visible rows in the page, so older chats show up once
  you've scrolled past them.
- **The unread count:** from the page's title.

To open a chat by name, the bridge clicks its row, or types the name into WhatsApp's search box and
clicks the result.

Files you paste or drop go to WhatsApp through a file input of the bridge's own: the bridge clicks
it, WebKit asks Natter Den which files, and WhatsApp reads them from disk. Where WebKit doesn't ask
(an older WebKit, or a page that isn't ready), Natter Den hands the bytes over itself, 4 MB per call
(256 KB during a call), up to 256 MB, and the bridge puts the file back together. Files dragged onto
the window are caught by Natter Den, and the bridge hides that drag from WhatsApp's page.

The bridge also keeps a video's object URL out of `<img>` elements: WhatsApp loads an attachment's
full preview into an image, and WebKitGTK decodes a video given to an image as an animated image,
every frame at once. An `<img>` gets an empty address instead, which fails to load the way any
picture that can't be decoded does; `<video>` elements are untouched.

## Calls

WhatsApp Web disables its call buttons where the browser has no WebRTC, as in the WebKitGTK
Natter Den has been run with. Natter Den has three ways to call, picked at start (all of them
experimental):

1. **In the page**, when GStreamer's WebRTC plugins and their Python bindings are installed
   (`rtc.available()`). `rtc.js` gives the page `RTCPeerConnection` and `RTCDataChannel` (data
   channels only), and `natter/rtc.py` does the networking with GStreamer's `webrtcbin`. This
   relies on WhatsApp's own call engine, in the page, making and encrypting the sound and pictures
   itself and sending them over data channels. Natter Den's call actions press WhatsApp's own
   call buttons. `NATTER_CALLS=window` turns this way off.
2. **A call window**, when Qt WebEngine for Python (PyQt6) is installed: a separate process
   (`natter/calls.py`, `natter/callwindow.py`) running WhatsApp Web in Qt WebEngine, linked once to
   your phone as a device of its own. It holds the first account only.
3. **Your browser**, otherwise, and for the other accounts' calls when the call window is used.

In the last two, the bridge turns WhatsApp's disabled call buttons back on and hands a press on
them to Natter Den.

With calls in the page, and outside a sandbox, WebKit also gets read-only access to
`/run/udev/data`, so it can find the camera itself when the desktop's camera portal answers too
late. As that way doesn't ask the desktop, Natter Den keeps the desktop's own answer for the camera:
a camera the desktop refused stays refused, and with nothing stored yet the desktop is asked before
WhatsApp gets it (`natter/media.py`).

With GStreamer 1.28 and newer, WebKit's camera picture freezes on its first frame
([WebKit bug 324582](https://bugs.webkit.org/show_bug.cgi?id=324582)). `natter/camfix/nattercamfix.c`,
a GStreamer tracer that `install.sh` or meson builds when a C compiler, `pkg-config` and
GStreamer's headers are there, puts back the `videorate` settings WebKit used to set.
`natter/gstfix.py` turns it on for WebKit's processes only; `NATTER_CAMFIX=0` turns it off. It
does nothing in the Flatpak, whose runtime brings its own GStreamer.

## Desktop integration

The tray is a StatusNotifierItem with a dbusmenu, and the dock badge uses the Unity LauncherEntry
API. The dock menu is written as `[Desktop Action]` entries into Natter Den's own `.desktop` file in
`~/.local/share/applications` (never a package's or the Flatpak's), naming chats by a hash rather
than by name on the command line. It's left empty while a passcode is set or privacy blur is on.

Desktop search answers from the chat lists on disk, so a search doesn't start WhatsApp. The
provider file says `AutoStart=false`, but Natter Den's bus name is D-Bus-activatable: a desktop
that calls it anyway starts a Natter Den that only answers searches and quits 30 s after the last
one, without opening WhatsApp.

### What it asks of the desktop

Natter Den has only been tried on Ada. Elsewhere, each feature needs the desktop to offer what the
right-hand column names; whether it then works there is untried.

| Feature | Needs |
|---|---|
| Notifications, one per chat | a notification server (`org.freedesktop.Notifications`), or the notification portal in the Flatpak |
| Reply window | a notification server that shows actions |
| Typing the reply in the notification | a notification server with KDE's inline reply (`x-kde-reply-*` hints) |
| Tray icon and menu | a StatusNotifierItem host with dbusmenu (on GNOME, the AppIndicator extension) |
| Unread badge on the dock icon | a dock that reads `com.canonical.Unity.LauncherEntry`; not in the Flatpak |
| Recent chats in the dock menu | a dock that shows desktop actions, and the desktop entry `install.sh` installs |
| Chats in desktop search | a shell that reads `org.gnome.Shell.SearchProvider2` providers from where Natter Den's file is (GNOME Shell doesn't read `~/.local/share`) |
| Start hidden at login | XDG autostart, or the Background portal in the Flatpak |
| Window forward from a notification | an activation token from the notification server (on Wayland) |
| One process group, memory back while hidden | a systemd user session; for memory, zram or zswap too |

## Replies from notifications

A reply goes through WhatsApp's own page, the way you would send it by hand, once for each time you
press Send:

1. WhatsApp's own handler for its notification opens the chat by its id (unless that chat is open
   already).
2. The bridge checks the open chat is that chat, by the chat id in its messages' keys rather than
   by its name, and that its message box is empty: words you left there aren't touched.
3. Your words go into the box through the page's own editing commands, a line at a time, and must
   land as you typed them (WhatsApp's own swap of `:-)` for an emoji aside).
4. WhatsApp's Send button is pressed once, not the Enter key, and the reply counts as sent when
   WhatsApp empties the box or shows your new message.

Anything that doesn't look right stops it before Send, and the reply window says why and keeps your
words: a picture or file waiting to be sent, words typed over the chat list and not yet saved (a new
group's name, a profile name being changed, a search), a voice message being recorded or waiting to
be sent, a draft or a message you were replying to in that chat, a chat that takes no messages from
you, a call. These are checked whether Natter Den's window is open or not. Status, Settings or
another tab left open over the chat list with nothing typed in it just closes, as WhatsApp's own
notification click closes it. If Send was pressed but the reply wasn't seen to go, the reply window
says so and doesn't send again.

While a reply is under way nothing else acts on that page (opening chats, links, files and fresh
pages wait), and a hidden window's page isn't throttled meanwhile. A locked Natter Den starts no
send: Reply asks for the passcode first, words typed in a KDE notification while it's locked wait in
the reply window until you press Send, and a reply window the lock closes with words in it comes
back after the passcode. Natter Den's log says how each reply ended (`natter: reply: sent`), not who
to or what. A notification's id and its Reply and Mute actions name no chat, only a random
reference, as desktops may keep them.

## Themes

WhatsApp Web paints through its own colour names (`--WDS-accent`,
`--WDS-systems-bubble-surface-outgoing` and so on). WhatsApp's chat themes change a few of them (the
accent, links, read ticks, the sent bubble and the chat background; Default blue, Default mono and
Sky blue the bubbles' small grey text too). A Natter Den theme redefines those, the accent names
around them (the label on the accent, the brand colour, the chosen filter) and the start-up
screen's, and nothing else. `natter/themes.py` writes them; `natter/account.py` hands the result to
WebKit.

- **WhatsApp's 22 chat themes** are copied as colour values, in their tonal form, light and dark,
  with WhatsApp's names in the order its picker has them.
- **Den** (the default, from the icon's orange) is a ramp at that hue with WhatsApp green's
  lightness and chroma at every step, each colour name on the step green uses.
- **Custom** keeps the colour you pick as the accent in light wherever white reads on it (3:1), or
  else takes the nearest colour of the same hue and chroma that white reads on; in dark it's a step
  lighter. Its other names come from a ramp at its hue.
- **Plain** adds no style sheet at all.

The label on the accent is white on Den and Custom. WhatsApp's themes keep WhatsApp's own label,
white in light and near-black in dark, except Lemon, Warm yellow and Cream, where white has under
3:1: Natter Den paints a chat theme across the page, not only the open chat, so those fills also
label the chat list's unread badges, and there the label is near-black in light too. A pale
yellow, cream or lime picked for Custom does the same. WhatsApp's surfaces and text keep
WhatsApp's colours, and the window keeps libadwaita's greys, at most faintly turned towards the
theme. Light, Dark or Follow System passes through to WhatsApp; on Ada, Follow System follows Ada's
own Light, Dark or Dune.

So that a theme changes how WhatsApp looks and nothing else, it is:

- **A user style sheet and nothing more:** WebKit applies it at the user level to WhatsApp's
  pages, top frame only. It's never put in the page as an element, and no script changes the page
  for it (the bridge only reads whether WhatsApp's colour names are there).
- **Colour values only:** custom properties named `--WDS-*` or `--splashscreen-*`, every one
  `!important`, on `html`, `body`, `#main` and `body.dark` only. No `url()`, `@import` or
  `@font-face`, so nothing is loaded. Custom's colour must be `#rrggbb` to get in.
- **Not WhatsApp's own settings:** Natter Den doesn't read or write WhatsApp's storage
  (localStorage, sessionStorage, IndexedDB), doesn't turn on, choose or change WhatsApp's own chat
  theme, wallpaper or app theme or the flags behind them, and sends no events for a theme.

`tests/test_themes.py` and `tests/test_theme_wiring.py` hold the theme code to these rules.

## WebKit settings

- **No service worker.** WhatsApp's service worker passes every download through itself, and in
  WebKitGTK 2.52 some arrived damaged (emoji sprite sheets drew as slivers and blank cells).
  Without it they draw whole. A registration left from before is cleared once.
- **A web browser's cache model** on Natter Den's own web context, so WhatsApp's code isn't
  downloaded on every start. Before that, WebKit's default context gets the document browser's
  model, which sizes WebKit's cache of finished web processes to none, so the first, blank one
  doesn't stay around.
- **No process swapping or back/forward cache:** one site, no history.
- **Hardware acceleration** on, unless you turn it off in Preferences → General →
  *Troubleshooting*, or with *Turn Off Hardware Acceleration* on the notice shown when the page
  has closed three times in two minutes (for a blank or flickering page on some graphics drivers).

## Process names and grouping

`natter` is a Python script (not a shell wrapper), so the main process is called `natter`. Its
program name is the app id, so on X11 the window's `WM_CLASS` matches `StartupWMClass` in the
desktop file and the dock puts the window under Natter Den's icon.

Desktops start apps in a systemd unit of their own, named after the app id: on Ada,
`app-ada-io.github.Officialbabs.Natter-<pid>.scope`. By the XDG convention other desktops' are
`app-gnome-<app id>-<pid>.scope` (GNOME), `app-<app id>@<random>.service` (KDE Plasma) and
`app-flatpak-<app id>-<pid>.scope` (a Flatpak); only Ada's has been seen. When Natter Den finds
itself in a unit that isn't named after it (started from a terminal or a script, say), it asks
systemd for `app-io.github.Officialbabs.Natter-<pid>.scope` before WebKit starts any process, so
every WebKit process, sandbox and proxy joins it. Without a systemd user manager, or inside a
sandbox, it leaves things as they are.
