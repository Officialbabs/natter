# Development

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests never reach the network: the update check and download go to stand-ins
(`tests/test_update.py`), and the updater runs in a home folder of its own
(`tests/test_update_helper.py`).

## When WhatsApp changes

The bridge (`natter/bridge.js`) finds its way around WhatsApp Web's page by a few landmarks, kept
in one table there (`WHERE`). When a WhatsApp update moves one, the feature that leans on it stops
(Ctrl+K stops opening chats, say). Once the chat list is up, the bridge's canary checks the chat
list's landmarks (and the open chat's header, when a chat is open) against the live page, and
Natter Den's log names any it didn't find (`natter: <account>: WhatsApp's page has changed: … not found`).
It judges the chat names on the list's first dozen rows, the chats' among them (not placeholders still
loading, nor the Archived row or a banner), as Natter Den reads them: found when most of them have
one, and the open chat's row, if the list marks it, has a name its header shows. While anything is
missing or the list is still loading, it looks again at 30 s and at 2 minutes, and it reports once a
page, with the counts of all the rows drawn and never a name
(`row-name not found (rows 17, chat-like 17, named 0)`): at the first look with nothing missing, or
else the last, with the list as last seen and its names as last judged (WhatsApp between screens
then, or a search typed). A last look that finds only the open chat's row misread looks once more
5 s on, as a chat being switched to has its row marked before its header is drawn. With everything
found but some chats' names unread, the log says so with the counts
(`canary: some chats' names can't be read (…)`), without a notification.
Names it can't trust go nowhere: while the open chat's row reads another name, and on a page where
the canary found that, Natter Den saves none of the list's names; and replies that need the list to
count the chats of a name stop while a row that may be a chat's has a name that can't be read
(`mismatch (unnamed)`).

| Landmark | What leans on it |
|---|---|
| `#pane-side` and its rows (`[role="listitem"]`, `[role="row"]`) | the chat list, Ctrl+K, the dock menu, desktop search, and replies (two chats of the same name in it) |
| the open chat's row marked `[aria-selected="true"]` (on a `#pane-side` row or in it), and the rows' places (`aria-rowindex` or `translateY`) | replies telling an open chat by the list marking it, near the list's top (`name+listed`) |
| a row's title cell, `[data-testid="cell-frame-title"]`: its `span[title]`, else its words when they're in one place in it (one outer `[dir="auto"]`, else one element); only in a row with no title cell, a `span[title]` with no other words before it, not a community's name or a label (`[data-testid="subgroup-identity"]`, `[data-testid="cell-frame-label"]`); never in the row's second line (`[data-testid="cell-frame-secondary"]`) or by its status or time (`[data-testid="last-msg-status"]`, `[data-testid="chat-list-timestamp"]`), where the last message's titled span is, nor holding them | chat names |
| a chat's row told by `[data-testid="cell-frame-container"]` or its title cell, or by two pieces of text; the Archived row by its icon (`[data-icon^="archive"]`, without a chat's cell) | telling chats from placeholders and the Archived row, for the canary and replies (only rows with the cell, where any row has it, are saved or judged by the canary; any row that may be a chat's must have a name for replies to count the chats of a name) |
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
(`natter-dev` here), and a bus name and instance lock of its own (`natter-<app id>.lock`, see
[One at a time](how-it-works.md#one-at-a-time)), so it runs beside yours. A separate instance still
shares the desktop's notification server and its portal permission store with the Natter Den you
use.

A separate instance never asks GitHub for updates ([Updates](how-it-works.md#updates)), and never
updates itself. To try the update notices in one, `NATTER_UPDATE_FEED` gives it a feed in place of
GitHub's API: a `file://` path, or an `http://127.0.0.1:<port>/` address, with the API's answer for
a release (`{"tag_name": "v9.9.9"}` is enough). It's read as GitHub would be, two minutes after
start-up is ready and then once a day; Natter Den's own app id ignores it.
`NATTER_UPDATE_CHECK=0` turns checks off altogether, a feed's too: for CI, and for packagers' test
runs.

`NATTER_DEBUG=1` also turns on WebKit's inspector (right-click → Inspect Element), prints the
page's console, and adds D-Bus actions for poking at a running Natter Den: `debug-snapshot` and
`debug-page-snapshot` save a PNG (only into the instance's cache folder, under `snapshots/`),
`debug-eval` runs JavaScript in the page, `debug-attach` and `debug-drop` hand it a file. `--url`
only works for a separate app id like the one above. On Natter Den's own app id the debug actions
need the token Natter Den prints at start (`natter: debug token …`) in front of their argument
(`"<token>:return document.title"`), but they still reach your logged-in WhatsApp, so don't leave
a debug Natter Den running.
