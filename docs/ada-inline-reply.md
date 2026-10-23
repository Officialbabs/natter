# Replying inside Ada's notifications

What Ada needs so that a person can type a reply in the notification itself, the way KDE Plasma
does it. Natter Den already supports this (`natter/notify.py`), so it would work with no change to
Natter Den.

Today Ada doesn't list `inline-reply` in its capabilities. So Natter Den sends a plain `reply`
action instead. Ada shows that action as a "Reply" pill, and clicking it opens Natter Den's
small reply window.

Both ways of replying (this inline field and the reply window) send the message through the same
part of Natter Den (`Account.send_reply`), so they succeed and fail alike. The "busy (preview)"
refusal of every reply seen on 2026-10-23 was Natter Den's own and is fixed in
`natter/bridge.js` (`previewOpen`, `drawerEdit`): WhatsApp's drawers are never empty, and the
bridge now looks for its media editor and for words typed, not at whether a drawer holds
anything.

This was read from Ada 2026.10.22.1223: the QML in `/usr/share/ada/qml` (paths below are relative
to it), the QML API in `/usr/lib/ada/desktop/qml/Ada/ada_qml.qmltypes`, and the symbols in
`libada_qml.so`. The source of `AdaNotifications` (`notifications.h`/`.cpp`) isn't on this
machine, so the C++ below guesses the names of its members.

## 1. What Natter Den sends and expects

Natter Den offers the inline field only when all of these hold (`notify.py:413-425`):
- the server lists `actions` and `inline-reply`;
- the notification is a message in a chat;
- Reply is on in Preferences;
- the notification shows who wrote: Natter Den isn't locked, "show sender" is on and privacy
  blur is off.

Otherwise it sends `reply` and Ada shows the pill, as it does today.

| | |
|---|---|
| `actions` | `default` "Open", `inline-reply` "Reply", `mute` "Mute Chat for 1 Hour" |
| hints | `x-kde-reply-placeholder-text` "Type a reply", `x-kde-reply-submit-button-text` "Send", `x-kde-reply-submit-button-icon-name` "document-send"; also `desktop-entry`, `category` `im.received`, `urgency` 1, `suppress-sound`, `image-data` |
| `replaces_id` | Each chat has one notification. A new message replaces it in place, under the same id. |
| reply | `NotificationReplied(u id, s text)` on `/org/freedesktop/Notifications`, interface `org.freedesktop.Notifications`, sent from the unique name that owns `org.freedesktop.Notifications`. Natter Den ignores the same signal from any other sender (`notify.py:662-666`). |

How Natter Den handles what comes back:

- **Text.** It sends the text to the chat. Empty or blank text counts as a click on Reply and
  opens the reply window (`app.py:1535`).
- **One reply per post.** Each post of a notification takes one reply. A second reply to the
  same post isn't sent: its text goes into the reply window (`notify.py:475-483, 711`).
- **Grace period.** A reply still counts for 30 s after the notification was closed or replaced
  (`REPLY_GRACE`). Inside that window, it doesn't matter whether Ada sends the reply or the close
  first.
- **After sending.** If the message was sent, Natter Den closes the notification itself
  (`app.py:1618-1621`). If not, it opens the reply window with the words and the reason.
- **`ActionInvoked(id, "inline-reply")`.** This opens the reply window. Ada never needs to send
  it.
- **Capabilities.** Natter Den reads them when it starts, and again whenever the owner of
  `org.freedesktop.Notifications` changes (`notify.py:279-298`). So a restarted Ada shell is
  picked up without restarting Natter Den.

## 2. AdaNotifications (C++, `libada_qml.so`)

1. **Capability.** Add `"inline-reply"` to the array that `GetCapabilities` returns
   (`NotifyServer::call`). Add `<signal name="NotificationReplied"><arg type="u" name="id"/><arg
   type="s" name="text"/></signal>` to the interface XML next to `ActionInvoked`.
2. **Notify.** `notify()` already gets the caller's unique name as its first argument:
   `NotifyServer::call` passes GDBus's `sender` into it. Keep it on the `Entry`, and refresh it on
   every Notify, including one that replaces an earlier notification. Actions come in key/label
   pairs.
   ```cpp
   // Entry
   QString sender;               // unique name of the latest Notify for this id
   bool inlineReply = false;
   QString replyPlaceholder, replySend;

   // notify()
   e.sender = sender;
   e.inlineReply = false;
   for (qsizetype i = 0; i + 1 < actions.size(); i += 2)
       e.inlineReply |= actions[i] == u"inline-reply";
   e.replyPlaceholder = plainLine(hints.value(u"x-kde-reply-placeholder-text"_s).toString(), 60);
   e.replySend = plainLine(hints.value(u"x-kde-reply-submit-button-text"_s).toString(), 20);
   // x-kde-reply-submit-button-icon-name may be ignored: the field draws Ada's own send glyph.
   ```
   If a replacing Notify doesn't include `inline-reply`, the field goes away. Natter Den does
   this when it locks.
3. **`get(id)`.** Leave `inline-reply` out of `actions`, so it never shows as a pill. Instead,
   pass the field to QML:
   ```cpp
   if (e.inlineReply)
       map[u"reply"_s] = QVariantMap{
           {u"placeholder"_s, e.replyPlaceholder.isEmpty() ? u"Reply"_s : e.replyPlaceholder},
           {u"send"_s, e.replySend.isEmpty() ? u"Send"_s : e.replySend}};
   ```
4. **`reply(id, text)`.** Add this next to `invoke()` (`notifications.h:196` in the qmltypes):
   ```cpp
   Q_INVOKABLE void reply(uint id, const QString &text);

   void AdaNotifications::reply(uint id, const QString &text)
   {
       const Entry *e = entry(id);
       if (!e || !e->inlineReply || e->sender.isEmpty() || m_locked || text.trimmed().isEmpty())
           return;                          // nothing goes out while locked, or to no one
       wake(*e);                            // as invoke() does (ada::wakeApp) before ActionInvoked
       const QByteArray to = e->sender.toUtf8(), words = text.toUtf8();
       g_dbus_connection_emit_signal(m_bus, to.constData(),   // targeted: never nullptr here
           "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
           "NotificationReplied", g_variant_new("(us)", id, words.constData()), nullptr);
       close(id, 2);                        // then NotificationClosed(id, 2) and removed(id)
   }
   ```
   Don't send the reply through `emitSignal()`. The disassembly shows it passes no destination,
   so the signal would be broadcast and every program on the bus would get the words. A broadcast
   is fine for `ActionInvoked` and `NotificationClosed`, but not here. Keep the words out of logs,
   out of `evidence()`, out of summaries and out of Ask Ada.
5. **Closing.** Send the reply, then close the notification with reason 2. Closing it takes the
   notification off the banners and out of the list. It also means a notification answers only
   once, which is what Natter Den expects. Leave expiry as it is: `expire()` closes only
   transient notifications (`close(id, 1)`). Natter Den's notifications aren't transient, so a
   banner that times out moves to the list still open, and its field still works there.

## 3. The popup (QML)

What it should look like: a banner for a chat message shows the reply field at all times, not
only under the pointer, because answering is the point of the notification. The "Reply" pill
goes. "Open" and "Mute" stay in the hover row, as now.

```
╭───────────────────────────────────────────╮
│ (●)  Natter Den                      now  │
│      Sam                                  │
│      Are we still on for six?             │
│      ╭─────────────────────────────────╮  │
│      │ Type a reply                (↑) │  │
│      ╰─────────────────────────────────╯  │
│      [Open] [Mute Chat for 1 Hour]        │  ← under the pointer
╰───────────────────────────────────────────╯
```

### `desktop/notify/NoteCard.qml`

Add the field after the body text (line 195), using the card's palette (`card.t`, `card.ink`).
Its keys work like the island's reply field (`desktop/agent/ReplyField.qml:266-291`). The send
glyph is the island's (`desktop/agent/Glyph.qml:47`). Import that directory under an alias,
because `Agent` is already a singleton in the Ada module:

```qml
import "../agent" as Island

    property bool replies: false                    // the host can take typing (banner, list)
    readonly property var replyHints: note.reply ?? null
    readonly property bool canReply: replies && replyHints !== null && !Notifications.locked
    readonly property bool overReply: canReply && replyHover.hovered
    readonly property bool replyKeys: canReply && (input.activeFocus || overReply)
    readonly property bool replyHeld: canReply && (input.activeFocus || input.text !== "")
    signal replied(string text)
    function sendReply() {
        if (input.text.trim() === "") return
        const words = input.text
        input.text = ""
        input.focus = false
        card.replied(words)
    }
    // Gone, replaced without it, or the screen locked: dropped, never sent.
    onCanReplyChanged: if (!canReply) { input.text = ""; input.focus = false }

        // after the body ShellText (line 195)
        Item {
            visible: card.canReply
            width: parent.width
            height: visible ? box.height + 8 : 0
            Rectangle {
                id: box
                y: 8
                width: parent.width
                height: Math.max(32, Math.min(input.contentHeight, 54) + 14)  // up to 3 lines
                radius: Math.min(height / 2, 16)
                color: card.t ? (input.activeFocus ? card.t.fillh : card.t.fill2)
                              : Qt.alpha(Theme.shell.text, input.activeFocus ? 0.11 : 0.06)
                HoverHandler { id: replyHover; cursorShape: Qt.IBeamCursor }
                // A MouseArea takes the press, so the card's TapHandler (line 70) doesn't open the chat too.
                MouseArea { anchors.fill: parent; onPressed: input.forceActiveFocus() }
                TextEdit {
                    id: input
                    x: 13; y: 7
                    width: parent.width - x - send.width - 8
                    height: parent.height - 14
                    clip: true
                    wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.PlainText
                    color: card.ink
                    selectionColor: Qt.alpha(Theme.shell.accent, 0.35)
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                    Accessible.role: Accessible.EditableText
                    Accessible.name: card.replyHints?.placeholder ?? "Reply"
                    Keys.onPressed: event => {
                        if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                                && !(event.modifiers & Qt.ShiftModifier)) {
                            card.sendReply(); event.accepted = true          // Enter sends
                        } else if (event.key === Qt.Key_Escape) {
                            input.focus = false; event.accepted = true       // keys back, words stay
                        } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                            event.accepted = true
                        }
                    }
                    ShellText {
                        width: parent.width
                        visible: input.text === "" && input.preeditText === ""
                        text: card.replyHints?.placeholder ?? ""
                        textFormat: Text.PlainText
                        font: input.font
                        color: card.subtle
                        elide: Text.ElideRight
                    }
                }
                T.AbstractButton {
                    id: send
                    anchors { right: parent.right; bottom: parent.bottom; margins: 4 }
                    width: 24; height: 24
                    focusPolicy: Qt.NoFocus
                    enabled: input.text.trim() !== ""
                    onClicked: card.sendReply()
                    Accessible.role: Accessible.Button
                    Accessible.name: card.replyHints?.send ?? "Send"
                    background: Rectangle { radius: 12; color: send.enabled ? Theme.shell.accent : "transparent" }
                    contentItem: Item {
                        Island.Glyph { anchors.centerIn: parent; name: "send"; size: 13
                                       color: send.enabled ? "#ffffff" : card.subtle }
                    }
                }
            }
        }
```

### `desktop/notify/Banners.qml`

How Ada handles the keyboard now: banners never take it. Their surface is
`KeyboardInteractivityNone` (lines 15-16 and 175-176). The island already solves the same
problem for its own reply field (`AgentIsland.qml:470-483, 1629-1638`). It asks for the keyboard
*on demand* only while its field is pointed at or typed in. The compositor then gives the keys
on a click, and takes them back on a click anywhere else. Banners should do the same:

```qml
    // Banners with a reply under way, and the ones whose field is pointed at or typed in, by slot.
    property var held: []
    property var keyed: []
    function marked(list, slot, on) { const o = list.filter(s => s !== slot); return on ? o.concat([slot]) : o }
    readonly property bool paused: hovering.length > 0 || dragging || held.length > 0       // line 29

        LayerShell.Window.keyboardInteractivity: banners.keyed.length > 0                   // line 176
            ? LayerShell.Window.KeyboardInteractivityOnDemand
            : LayerShell.Window.KeyboardInteractivityNone

                NoteCard {                                                                   // line 282
                    replies: true
                    onReplied: text => Notifications.reply(banner.nid, text)  // C++ closes it; onRemoved retires the card
                    onReplyHeldChanged: banners.held = banners.marked(banners.held, banner.slot, replyHeld)
                    onReplyKeysChanged: banners.keyed = banners.marked(banners.keyed, banner.slot, replyKeys)
                }
                Component.onDestruction: { /* also drop banner.slot from held and keyed */ }
```

Also change these:

- **Joining (lines 74-88).** When a card has a reply under way, a newer notification from the
  same app must not take that card over. It would be another chat, and the words would go to
  that chat. Join only when `model.get(same).nid === id`, or when that card's slot isn't in
  `held`. Otherwise insert a new card.
- **Three at most (lines 93-98).** Skip cards in `held`, as urgent ones are skipped.
- **Swipe (line 329).** Set `enabled: !banner.card.overReply && !banner.card.replyHeld`, so
  dragging in the field selects text instead of swiping the card away.
- **Lock (line 131).** No change: `clear()` destroys the cards, and their unsent words with them.
- **`evidence()` (lines 143-161).** You may add `replying: held.includes(row.slot)`. Never add
  the words.
- **Comment (lines 15-16).** It should now say: except when the person clicks into a reply
  field.

### `desktop/notify/GroupStack.qml` (the list under the clock)

Add `replies: true` and `onReplied: text => Notifications.reply(cardItem.note.id, text)` to the
`NoteCard` in `Card` (lines 317-331). The panel's surface already holds the keyboard
(`desktop/Overlay.qml:73`, `KeyboardInteractivityExclusive`), so there's no focus work there.
Esc in the field drops the focus first. A second Esc closes the panel, as now. Locking closes
the panel (`desktop/Main.qml:241`), and `canReply` turns false with `Notifications.locked`, so
any unsent words go.

### Lock screen

No change. `lock/NotificationStack.qml` only sees the redacted `Notifications.lockView` (lines
16-28), and nothing on it opens or sends before the password. Keep `reply` out of
`publishLockView()`.

## 4. Checking it

- **Capability.**
  `gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications --method org.freedesktop.Notifications.GetCapabilities`
  should list `inline-reply`.
- **Targeted signal.** Run
  `dbus-monitor "type='signal',interface='org.freedesktop.Notifications'"`. A reply should show
  as `NotificationReplied` with `destination=:1.N` (Natter Den's name), never with a null
  destination, followed by `NotificationClosed` with reason 2.
- **In Natter Den.**
  - When a message arrives, the banner shows the field. Point at it, click and type: the banner
    stays and the window you were in loses the keys. Press Enter: the message is sent and the
    banner goes.
  - Press Esc, or click another window: the keys go back to that window.
  - Lock Natter Den: the next notification has a Reply pill and no field.
  - Lock Ada while words are typed but not sent: they're gone, and nothing was sent.
