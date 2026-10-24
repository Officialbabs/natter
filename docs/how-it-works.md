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
Natter Den's other touches, each hung off a class Natter Den sets on `<html>`. The privacy blur
uses no CSS filter, which would paint every row again on its own each time WhatsApp's lists
scroll: words turn see-through with a soft shadow in WhatsApp's text colour, and pictures aren't
drawn. *Cover When in the Background* isn't in the page at all: while another window is in front,
an opaque cover in the window's colour lies over the page, and goes the moment Natter Den is
in front again. The bridge tells Natter Den:

- **Notifications:** WhatsApp's `new Notification(…)` comes to Natter Den, which shows it over
  `org.freedesktop.Notifications`, or through GNotification (the notification portal) inside a
  sandbox. A click goes back to the same object, so WhatsApp's own handler opens the chat.
- **The chat list:** the names of the chats WhatsApp shows, and their order. Natter Den keeps the
  names (no message text) in `chats.json`, up to 500 per account, for Ctrl+K, the dock menu and
  desktop search. WhatsApp only keeps the visible rows in the page, so older chats show up once
  you've scrolled past them.
- **The unread count:** from the page's title.
- **A voice message being recorded:** whether a microphone track WhatsApp got is live outside a
  call (below).

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
The same tracer clears the gap marks WebKit before 2.54.2 puts on Web Audio's silent blocks
([WebKit bug 326494](https://bugs.webkit.org/show_bug.cgi?id=326494)): their data is silence anyway,
but pulsesink skips them and clicks when a call's sound comes back. It touches only what WebKit's
Web Audio source pushes, and has nothing to do on a WebKit that no longer marks them. It comes
with the camera fix, so only with GStreamer 1.28 and newer.
`natter/gstfix.py` turns it on for WebKit's processes only; `NATTER_CAMFIX=0` turns it off. It
does nothing in the Flatpak, whose runtime brings its own GStreamer.

## Desktop integration

The tray is a StatusNotifierItem with a dbusmenu, and the dock badge uses the Unity LauncherEntry
API. The dock menu is written as `[Desktop Action]` entries into Natter Den's own `.desktop` file in
`~/.local/share/applications` (never a package's or the Flatpak's), naming chats by a hash rather
than by name on the command line. It's left empty while a passcode is set or privacy blur is on.

Desktop search (`natter/search.py`) lists chats by name, through the interface GNOME Shell uses
for app search results:

| | |
|---|---|
| Bus name | `io.github.Officialbabs.Natter` (D-Bus service: `~/.local/share/dbus-1/services/io.github.Officialbabs.Natter.service`) |
| Object | `/io/github/Officialbabs/Natter/SearchProvider` |
| Interface | `org.gnome.Shell.SearchProvider2` |
| Provider file | `~/.local/share/gnome-shell/search-providers/io.github.Officialbabs.Natter.search-provider.ini` (in the Flatpak, under its exports) |
| Result ids | `<account id>/<chat name>`, e.g. `main/Ana Souza` |
| Result metas | `id`, `name` (the chat), `description` ("WhatsApp chat", followed by " · <account name>" when there are several accounts), `gicon` (`io.github.Officialbabs.Natter`) |

`GetInitialResultSet` returns the chats whose names contain what was typed, best first.
`ActivateResult` opens that chat, and `LaunchSearch` opens Natter Den's own chat search with the
terms filled in. Only chat names are searched, never messages.

Searches are answered from the chat lists on disk, so a search doesn't start WhatsApp. The
provider file says `AutoStart=false`, but Natter Den's bus name is D-Bus-activatable: a desktop
that calls it anyway starts a Natter Den that only answers searches and quits 30 s after the last
one, without opening WhatsApp.

### What it asks of the desktop

Natter Den has been run on Ada 26.04 (based on Ubuntu) and on CachyOS (based on Arch) with KDE
Plasma. Elsewhere, each feature needs the desktop to offer what the right-hand column names; whether
it then works there is untried.

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
| Other audio paused while recording | players that speak MPRIS (`org.mpris.MediaPlayer2`) on the session bus |

On Ada, adding `io.github.Officialbabs.Natter` to `AllowBackground` in
`~/.config/os-ai/lifecycle.ini` keeps Natter Den receiving messages while its window is closed.

### Pausing other audio

While you record a voice message, other apps' players pause, and play again after. The bridge says
"recording" once a microphone track from `getUserMedia` has been live for 300 ms outside a call, and
"not recording" as soon as the last one ends. Natter Den (`natter/mediapause.py`) then asks each
`org.mpris.MediaPlayer2.*` name on the session bus whether it's playing and can pause, and pauses
those that are, skipping players of its own (by name, and by owner process). 400 ms after the
recording ends, it plays again only those it paused that are still paused: one you played, stopped
or closed meanwhile is left alone, and nothing plays again after 15 minutes. A call, ringing or
connected, holds them until it's over; then they play again. A page that reloads or
crashes mid-recording, or Natter Den quitting, ends the recording. Each D-Bus call is asynchronous,
with a 500 ms timeout. The log gets one line per recording with the counts, never the players'
names. *Pause Other Audio While Recording* in Preferences → Privacy turns it off (what it paused
already still plays again); the Flatpak has `--talk-name=org.mpris.MediaPlayer2.*` for it.

A call placed from WhatsApp's own call button can look like a voice message for a moment: if its
microphone comes on more than 300 ms before the call's first data channel is made, other audio
pauses a moment early, and plays again when the call ends.

## Replies from notifications

A reply goes through WhatsApp's own page, the way you would send it by hand, once for each time you
press Send:

1. WhatsApp's own handler for its notification opens the chat by its id. It's clicked even when a
   chat of that name is open already, as it may be another of the same name, and then given a
   moment to open the right one. A notification WhatsApp has closed since (the chat read) is
   clicked too.
2. The bridge checks the open chat is that chat, and that its message box is empty: words you left
   there aren't touched. WhatsApp's messages no longer carry the chat's id, so the chat is told by
   its name, which must be the notification's title, and at least one more sign: the message you
   were notified of among its latest messages (generic ones, such as "📷 Photo" or "Sticker", don't
   count), its picture in the header being the notification's (WhatsApp's picture addresses compared
   by their host and path, as the same picture comes with other query strings), the reply's own
   click having opened it, from no chat or from one with another name, or the chat list marking it
   as the chat open while it's the only chat of that name in the list, the list shows all the chats
   from its top with the chat among its first four rows (up to three pinned chats, then the latest),
   and the notification was still shown when you asked to reply (`listed`). A name alone is never
   enough, as two chats can share one; a chat of that name open before the click needs the message,
   the picture or the list. Only messages that came in count as the message, not your own; where
   WhatsApp no longer marks which way its messages went, any of the latest messages counts, but only
   for a notification still shown when you asked, with the chat list as for `listed` (it needn't
   mark the chat open). A message drawn with a word set apart at its start (bold, a link, an
   @mention) counts for the words after that only when they have four letters or digits at least,
   unless that word is the group sender's name in the notification. The message is looked for as
   WhatsApp draws it: its time or a group's sender's name drawn in with its words, and a long
   message cut short with "Read more" by enough of its start. For a notification WhatsApp had closed
   when you asked to reply, whose click may open nothing, the message must have four letters or
   digits at least (twelve if the notification cut it short), and in a group the sender's name must
   show with it, or on the message that starts that sender's run of messages just above, with no
   message between that WhatsApp says another person sent. One WhatsApp closes after you asked (the
   reply having the chat read) counts as still shown. A chat the click didn't open is never taken
   while the chat list shows two chats of that name, nor while a search is typed or a filter chip is
   on, when the list may leave one out: the click may open nothing, or open the other one only
   later, and the chat open may be the other one, with the same words. Where messages do carry the
   chat's id, as WhatsApp's once did, that id decides instead. The bridge waits for the chat's
   messages as long as the reply may take, less the time typing and Send need: when the message
   isn't the chat's latest, WhatsApp can take some seconds to load the ones around it. Just before
   Send it checks it's still the same chat, with the same name, and for a chat the click didn't
   open, with the message, the picture or the list's mark still there. One case remains: with two
   chats of the same name that aren't both in the visible chat list, and WhatsApp's click not opening
   the chat, or opening it only after a second or so, the open one can be taken for the other if it
   shows the same message. The list's mark only counts near the list's top, where the other, just
   written to, would be drawn too; archived chats aren't in the list at all, so an archived chat
   notifying (a mention in a group, say) with the same name as an open chat near the top is a case
   the list can't rule out.
3. Your words go into the box through the page's own editing commands, a line at a time, and must
   land as you typed them (WhatsApp's own swap of `:-)` for an emoji aside).
4. WhatsApp's Send button is pressed once, not the Enter key, and the reply counts as sent when
   WhatsApp empties the box or shows a new message with your words in the chat.

Anything that doesn't look right stops it before Send, and the reply window says why and keeps your
words: a picture or file waiting to be sent, words typed over the chat list and not yet saved (a new
group's name, a profile name being changed, a search), a voice message being recorded or waiting to
be sent, a draft or a message you were replying to in that chat, a chat that takes no messages from
you, a call. These are checked whether Natter Den's window is open or not. Status, Settings or
another tab left open over the chat list with nothing typed in it just closes, as WhatsApp's own
notification click closes it. If Send was pressed but the reply wasn't seen to go, the reply window
says so and doesn't send again.

WhatsApp only draws a chat and its messages while its page is on screen. So while Natter Den's
window is closed or minimised (or hidden by the desktop some other way, as the page itself says),
or shows another account, Natter Den moves WhatsApp's page under the reply window for as long as
the reply runs: unseen, out of reach and at the size it had in the window, so WhatsApp draws the
chat and its messages as it would there. It's drawn at an opacity of 0.001, not 0, which GTK would
skip drawing altogether, leaving the page without frames; at 0.001 it changes no colour on screen.
The page goes back as soon as the reply ends, the reply window closes, Natter Den locks, or its
window opens on that account, is no longer minimised or gets the focus. A reply typed in a KDE notification brings the reply window up for it, showing the
words being sent. With the page on screen, WhatsApp may show you as online and mark the chat as
read, as replying in WhatsApp itself would.

While a reply is under way nothing else acts on that page (opening chats, links, files and fresh
pages wait), and a hidden window's page isn't throttled meanwhile. A locked Natter Den starts no
send: Reply asks for the passcode first, words typed in a KDE notification while it's locked wait in
the reply window until you press Send, and a reply window the lock closes with words in it comes
back after the passcode. Natter Den's log says how each reply ended, and how the chat was told
(`natter: reply: sent (name+body)`, or `name+avatar`, `name+click`, `name+listed`, `ids`), not who to
or what. When the chat wasn't the one or couldn't be told (`mismatch (other-name)`, `mismatch (unconfirmed)`,
`mismatch (ambiguous)`, `mismatch (filtered)` and the like, or `gone` or `not-ready` once WhatsApp's
notification was clicked), or the reply stopped after the check (`draft (quote)`, `left-as-draft
(no-button)`, `left-as-draft (text)`, `cant-send`, `unsure`), the line ends with what the page was
like, in brackets: whether WhatsApp's page counted itself visible and was drawing, whether it was on
screen and lent to the reply window, whether the chat, its header, footer and message box were
there, the page's size in hundreds of pixels, how many message keys and rows the chat held, and the
shape of its first key (each run of digits as 9, of letters as a). Then the footer: which of the
bridge's places for the message box found it, how many buttons it had, the shapes of its icons'
names, and whether a message being replied to or a preview showed by the box or a picture or file
waited to be sent. Then which signs of the chat held (its name, the click opening it, the message,
the picture, the list), whether the notification had a body and a picture, how many rows the chat
list marked as the chat open and whether the one had the notification's name, whether the chat's
messages were marked with which way they went, whether the notified message was among the latest
ones whichever way they went, or within their words at all, and the kinds of the notification's and
the header's picture addresses (`data`, `blob`, `wa`, `https-other` or `none`). Then where in the
header the name was found (which of the bridge's places for it, and whether it was the first name
there), the kind of the notification's data and the names of its keys, the shape of its tag, the
names of the attributes on the chat, its header, its first message and that message's row, and the
shapes of the values with an `@` or `_` in them of those that hold keys (`id` and `data-` ones). Never a name, words, an id or their
lengths. A notification's id and its Reply and Mute actions name no chat, only a random reference,
as desktops may keep them.

Where the notification server offers KDE's inline reply, Natter Den offers the field only when the
notification shows who wrote, and takes the text only from the server's own bus name, once per
field.

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
own Light, Dark or Dune, from `~/.config/os-ai/desktop.ini`.

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

Desktops start apps in a systemd unit of their own, named after the app id. By the XDG convention
these are `app-gnome-<app id>-<pid>.scope` (GNOME), `app-<app id>@<random>.service` (KDE Plasma)
and `app-flatpak-<app id>-<pid>.scope` (a Flatpak); on Ada it's
`app-ada-io.github.Officialbabs.Natter-<pid>.scope`. When Natter Den finds itself in a unit that
isn't named after it (started from a terminal or a script, say), it asks systemd for
`app-io.github.Officialbabs.Natter-<pid>.scope` before WebKit starts any process, so every WebKit
process, sandbox and proxy joins it. Without a systemd user manager, or inside a sandbox, it leaves
things as they are.
