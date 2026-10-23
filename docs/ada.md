# Natter Den on Ada: what it offers, and what it needs

Natter Den is a WhatsApp Web client (GTK 4, libadwaita, WebKitGTK). Its app id is
`io.github.Officialbabs.Natter` (it was `io.natter.Natter` before; `./install.sh` moves an old
install over). Two things would make it fit Ada better. Both are small.

## 1. Desktop search

Natter Den answers the interface GNOME Shell uses for app search results,
`org.gnome.Shell.SearchProvider2`, so Ada's search can list WhatsApp chats by name.

| | |
|---|---|
| Bus name | `io.github.Officialbabs.Natter` (D-Bus service: `~/.local/share/dbus-1/services/io.github.Officialbabs.Natter.service`) |
| Object | `/io/github/Officialbabs/Natter/SearchProvider` |
| Interface | `org.gnome.Shell.SearchProvider2` |
| Provider file | `~/.local/share/gnome-shell/search-providers/io.github.Officialbabs.Natter.search-provider.ini` (in the Flatpak, under its exports) |
| Result ids | `<account id>/<chat name>`, e.g. `main/Ana Souza` |
| Result metas | `id`, `name` (the chat), `description` ("WhatsApp chat"), `gicon` (`io.github.Officialbabs.Natter`) |

`GetInitialResultSet(["ana"])` returns the chats whose names contain what was typed, best
first. `ActivateResult(id, terms, timestamp)` opens that chat. `LaunchSearch(terms, timestamp)`
opens Natter Den's own chat search with the terms filled in. Only chat names are searched, never
messages.

Searches are answered from the chat lists Natter Den keeps on disk (`chats.json` per account), not
from WhatsApp's page, so a search never starts WhatsApp. The provider file says
`AutoStart=false`, so a desktop that honours it only asks a Natter Den that's already running.
Natter Den's bus name can still be started over D-Bus: if a search does that, Natter Den answers
from disk and leaves again 30 s after the last search; WhatsApp only starts when a result is
opened.

Ada's search (Super, then type) shows these under "Results from apps" since Ada's 22 October
2026 update; Settings → Keyboard switches it, per app. Opening a result needs one thing from Ada:
on Wayland an app can't bring its own window forward without an activation token, and
`ActivateResult` carries none. So Ada's launcher brings the app forward itself after
`ActivateResult` and `LaunchSearch`, the way its notification clicks do: the app's latest window,
or, with none on screen, `Apps.launch` of the provider's desktop id (".desktop" included), which
reaches the running Natter Den with a token. The change to Ada's `Launcher.qml` is a small
`bringForward(provider)` after both calls; until it's in Ada's own source, a result opens the chat
without bringing the window forward. The search itself is in `natter/search.py`.

## 2. Lifecycle (ada-lifecycled)

`ada-lifecycled` pauses apps that stay hidden, unless they're allowed in the background or
"deliver messages". Its messenger list matches names like slack, signal, element and so on,
and Natter Den's app id (`io.github.Officialbabs.Natter`) isn't on it. A paused Natter Den can't
receive messages, so notifications stop until it's opened.

Ideas, from least to most work:

- Treat apps whose desktop entry says `Categories=…InstantMessaging…` as messengers.
- Or let an app ask to stay running, for example a desktop entry key such as `X-Ada-Background=true`,
  or the `org.freedesktop.portal.Background` portal, which the Flatpak build already asks.

Until then, adding `io.github.Officialbabs.Natter` to `AllowBackground` in
`~/.config/os-ai/lifecycle.ini` does it (replacing `io.natter.Natter` if it was there:
`./install.sh` says when it still is, and leaves the file to you).

Natter Den gives memory back while hidden instead of being paused. It reclaims its own scope's
cold memory (`memory.reclaim`, 64 MB steps, swappiness=100, down to 800 MB, and no further once 1 GB of it or 60% of the computer's swap is in use), the same thing
lifecycled does for paused apps, while still delivering messages.
It only does that where swap is cheap (zram, or zswap enabled), and Ada's launcher scope
(`app-ada-io.github.Officialbabs.Natter-<pid>.scope`) counts as its own, so it doesn't make
another.

Two things for lifecycled, from a WhatsApp page that grew to 12 GB (20 October 2026):

- **Measure an app by what it holds: anon plus swap** (`memory.stat`'s `anon` and
  `memory.swap.current`), not `memory.current`. That read 6.2 GB while the page held 12.3 GB,
  the rest of it in swap, so lifecycled closed other apps instead of the one that was growing.
- **Make launcher scopes with `OOMPolicy=continue`.** systemd's default, `stop`, stops the whole
  app when the kernel ends one of its processes at the unit's `MemoryMax`; with `continue` only
  that process goes, and Natter Den starts a fresh page in its place. It can only be set when
  the scope is made, so Natter Den sets it on scopes it makes itself. On Ada's, which has
  `stop`, it sets no `MemoryMax` (a page past it would stop all of Natter Den) and says so in its
  log; its own guard (`natter/memguard.py`) ends a runaway page alone.

## Replying from notifications

A message's notification has a Reply action (`reply`, next to `default` and `mute`). Ada's
notification server shows it as a button, and Natter Den opens a small reply window for that chat,
without bringing its main window forward. Two additions to Ada would make it better, with no
change to Natter Den:

- **An `ActivationToken` signal before `ActionInvoked`**, as the notification spec has it since
  1.2. Natter Den gives the token to the reply window (or, while it's locked, to the passcode), so
  on Wayland it comes forward with the focus, ready to type in. Without one, it depends on Ada
  letting the window take the focus.
- **KDE's inline reply**: advertise `inline-reply` in `GetCapabilities`, show the action keyed
  `inline-reply` as a text field (with the hints `x-kde-reply-placeholder-text`,
  `x-kde-reply-submit-button-text` and `x-kde-reply-submit-button-icon-name`), and send
  `NotificationReplied(u id, s text)` to the app. Natter Den offers it only when the notification
  shows who wrote, and accepts the text only from the server's own bus name, once per field.
  [ada-inline-reply.md](ada-inline-reply.md) has the changes to Ada's notification server and
  QML in detail.

## Light, Dark and Dune

Ada's Settings → Appearance picks the apps' style (Light, Dark or Dune) and stores it in
`~/.config/os-ai/desktop.ini` as `[Desktop] Theme=paper`, `midnight` or `dune`. Ada's own apps
read it there, but it never reaches GNOME's `color-scheme` setting or the desktop portal, which
is what GTK and libadwaita apps follow. So a GTK app set to follow the system kept whatever
`color-scheme` said (`prefer-dark` here) after Light was picked.

Natter Den's Follow System reads `Theme=` instead when `XDG_CURRENT_DESKTOP` is Ada, and watches
the file, so it changes along with Ada (`natter/ada.py`). Paper is light; Midnight and Dune are
dark. The Flatpak build asks for read-only access to `~/.config/os-ai` for this.

An idea for Ada: set `org.gnome.desktop.interface color-scheme` (`prefer-dark` or `default`)
whenever Theme= changes. Every GTK, libadwaita and portal-aware app would then follow Ada's
choice without reading Ada's own files.
