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
`AutoStart=false`: a search only asks a Natter Den that's already running, and never launches one.
(If something does start it over D-Bus anyway, Natter Den answers from disk and leaves again once
the searching stops; WhatsApp only starts when a result is opened.)

If Ada's search API takes another shape, Natter Den can answer that too. The search itself is in
`natter/search.py`.

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

Natter Den keeps itself light while hidden instead of being paused. It reclaims its own scope's
cold memory (`memory.reclaim`, 64 MB steps, swappiness=100, down to 800 MB, and no further once 1 GB of it or 60% of the computer's swap is in use), the same thing
lifecycled does for paused apps, so it ends up about as small while still delivering messages.
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
  the scope is made, so Natter Den sets it on scopes it makes itself, sets `MemoryMax` and
  `MemorySwapMax` on Ada's, and says in its log when Ada's is `stop`. Its own guard
  (`natter/memguard.py`) ends a runaway page well before either.

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
