# Development

## Tests

```sh
python3 -m unittest discover -s tests
```

## When WhatsApp changes

The bridge (`natter/bridge.js`) finds its way around WhatsApp Web's page by a few landmarks, kept
in one table there (`WHERE`). When a WhatsApp update moves one, the feature that leans on it stops
(Ctrl+K stops opening chats, say). Once the chat list is up, the bridge's canary checks the chat
list's landmarks (and the open chat's header, when a chat is open) against the live page, and
Natter Den's log names any it didn't find (`natter: <account>: WhatsApp's page has changed: … not found`).

| Landmark | What leans on it |
|---|---|
| `#pane-side` and its rows (`[role="listitem"]`, `[role="row"]`) | the chat list, Ctrl+K, the dock menu, desktop search, and replies (two chats of the same name in it) |
| `[data-testid="cell-frame-title"] span[title]` in a row (else its `span[title]`) | chat names |
| `#main header`, with the title in `[data-testid="conversation-info-header-chat-title"]` | which chat is open |
| the link screen: a `canvas`, `[data-ref]` or `[data-link-code]` in `#app` (the QR code), with no `#pane-side` | telling the link screen from a page still loading |
| filter chips: `[role="tab"][aria-selected]` (or `button[aria-pressed]`) in `#side` | not taking a filtered list's order for the recent chats |
| call buttons in `#main header`, by their label, title or icon name ("call", "video") | starting calls from Natter Den, and turning WhatsApp's disabled buttons back on when calls don't run in the page |
| the search box in `#side`, and the message box in `#main footer` | opening chats by name, pasting files |
| WhatsApp's colour names (`--WDS-accent`, `--WDS-surface-default-RGB` and the rest) and its `body.dark` class | themes: when they're gone, the theme comes off that page and WhatsApp's own colours stay (`natter/themes.py`) |
| the open chat's name in its header, its picture (an `img` in `#main header`), its messages' rows (`#main [role="row"]`, with WhatsApp's `message-in` and `message-out` classes, and who sent each in its `data-pre-plain-text`) and keys (`[data-id]`: a bare message id now, once `<mine>_<chat id>_<id>`), its message box in `#main footer`, and the Send button by its icon's name ("send") | replies from notifications |
| `[data-testid="drawer-middle"]` and `[data-testid="drawer-left"]`, always in the page and never empty, so what's open is read from inside them: WhatsApp's media editor in the middle one (its `input[type="file"][multiple]` or `[data-testid="media-caption-input-container"]`), and fields with words in them in the left one | replies waiting rather than closing what's open, files added to an open preview |
| the row of the message box in `#main footer` (the box, up to the voice message button by its icon's name), with nothing else shown in the footer | replies not going out as a reply to a message picked in WhatsApp; a voice message's controls by their icons' names |

The canary doesn't judge the reply landmarks, as most need a chat open. When one of those moves,
replies stop before Send and the log says which (`natter: reply: mismatch (unconfirmed)`,
`left-as-draft (no-button)`); a footer laid out anew could make every reply stop as
`draft (quote)`. *Show Reply Button* in Preferences → Notifications takes Reply off notifications
until there's a fix.

## The test page

`tests/mock-whatsapp.html` copies most of these landmarks (not WhatsApp's colour names or its
`body.dark` class), so you can try changes without your account. Its messages are keyed by bare ids,
as WhatsApp's are now; add `?prefixed` to the address for the old keys with chat ids. Run it as a
separate instance, with its own app id and folders, so it doesn't touch the Natter Den you use:

```sh
NATTER_APP_ID=io.github.Officialbabs.Natter.Dev XDG_CONFIG_HOME=/tmp/nd/config \
XDG_DATA_HOME=/tmp/nd/data XDG_CACHE_HOME=/tmp/nd/cache \
NATTER_DEBUG=1 bin/natter --url "file://$PWD/tests/mock-whatsapp.html"
```

An app id other than Natter Den's own (or its old `io.natter.Natter`) gets folders of its own too
(`natter-dev` here). A separate instance still shares the desktop's notification server and its
portal permission store with the Natter Den you use.

`NATTER_DEBUG=1` also turns on WebKit's inspector (right-click → Inspect Element), prints the
page's console, and adds D-Bus actions for poking at a running Natter Den: `debug-snapshot` and
`debug-page-snapshot` save a PNG (only into the instance's cache folder, under `snapshots/`),
`debug-eval` runs JavaScript in the page, `debug-attach` and `debug-drop` hand it a file. `--url`
only works for a separate app id like the one above. On Natter Den's own app id the debug actions
need the token Natter Den prints at start (`natter: debug token …`) in front of their argument
(`"<token>:return document.title"`), but they still reach your logged-in WhatsApp, so don't leave
a debug Natter Den running.
