# Natter on Ada: what it offers, and what it needs

Natter is a WhatsApp Web client (GTK 4, libadwaita, WebKitGTK). Its app id is
`io.github.Officialbabs.Natter` (it was `io.natter.Natter` before; `./install.sh` moves an old
install over). Two things would make it fit Ada better. Both are small.

## 1. Desktop search

Natter answers the interface GNOME Shell uses for app search results,
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
opens Natter's own chat search with the terms filled in. Only chat names are searched, never
messages.

Searches are answered from the chat lists Natter keeps on disk (`chats.json` per account), not
from WhatsApp's page, so a search never starts WhatsApp. The provider file says
`AutoStart=false`: a search only asks a Natter that's already running, and never launches one.
(If something does start it over D-Bus anyway, Natter answers from disk and leaves again once
the searching stops; WhatsApp only starts when a result is opened.)

If Ada's search API takes another shape, Natter can answer that too. The search itself is in
`natter/search.py`.

## 2. Lifecycle (ada-lifecycled)

`ada-lifecycled` pauses apps that stay hidden, unless they're allowed in the background or
"deliver messages". Its messenger list matches names like slack, signal, element and so on,
and Natter's app id (`io.github.Officialbabs.Natter`) isn't on it. A paused Natter can't
receive messages, so notifications stop until it's opened.

Ideas, from least to most work:

- Treat apps whose desktop entry says `Categories=…InstantMessaging…` as messengers.
- Or let an app ask to stay running, for example a desktop entry key such as `X-Ada-Background=true`,
  or the `org.freedesktop.portal.Background` portal, which the Flatpak build already asks.

Until then, adding `io.github.Officialbabs.Natter` to `AllowBackground` in
`~/.config/os-ai/lifecycle.ini` does it (replacing `io.natter.Natter` if it was there:
`./install.sh` says when it still is, and leaves the file to you).

Natter keeps itself light while hidden instead of being paused. It reclaims its own scope's
cold memory (`memory.reclaim`, 64 MB steps, swappiness=200, down to 300 MB), the same thing
lifecycled does for paused apps, so it ends up about as small while still delivering messages.
It only does that where swap is cheap (zram, or zswap enabled), and Ada's launcher scope
(`app-ada-io.github.Officialbabs.Natter-<pid>.scope`) counts as its own, so it doesn't make
another.
