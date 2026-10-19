"""Natter's dock icon: the unread badge, and recent chats in its right-click menu."""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from gi.repository import Gio, GLib

from . import APP_ID, OBJECT_PATH
from .system import in_sandbox

DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
ENTRY = DATA_HOME / "applications" / f"{APP_ID}.desktop"
APP_URI = f"application://{APP_ID}.desktop"
LAUNCHER_PATH = OBJECT_PATH + "/LauncherEntry"
MENU_CHATS = 5
MENU_NAME = 60  # characters of a chat's name the menu shows
WRITE_EVERY = 5 * 60  # seconds: rewriting the entry makes the desktop read it again
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Bidi controls and invisible marks, which can reorder or hide what's around a name. U+200D and
# variation selectors stay: emoji are built with them.
INVISIBLE = re.compile(r"[\u061c\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
EXEC_PROGRAM = re.compile(r'^Exec=("(?:[^"\\]|\\.)*"|\S+)', re.M)

LAUNCHER_XML = """
<node>
  <interface name="com.canonical.Unity.LauncherEntry">
    <method name="Query">
      <arg type="s" name="app_uri" direction="out"/>
      <arg type="a{sv}" name="properties" direction="out"/>
    </method>
    <signal name="Update">
      <arg type="s" name="app_uri"/>
      <arg type="a{sv}" name="properties"/>
    </signal>
  </interface>
</node>
"""


class Badge:
    """The unread count on the dock icon, over the Unity LauncherEntry API that Ada's dock reads."""

    def __init__(self, bus: Gio.DBusConnection) -> None:
        self.bus = bus
        self.count = 0
        info = Gio.DBusNodeInfo.new_for_xml(LAUNCHER_XML).interfaces[0]
        try:
            bus.register_object(LAUNCHER_PATH, info, self._on_call, None, None)
        except GLib.Error:
            pass  # another Natter object holds it; signals still go out

    def _props(self) -> dict:
        return {"count": GLib.Variant("x", self.count), "count-visible": GLib.Variant("b", self.count > 0)}

    def _on_call(self, _bus, _sender, _path, _iface, method, _params, invocation) -> None:
        if method == "Query":
            invocation.return_value(GLib.Variant("(sa{sv})", (APP_URI, self._props())))

    def set(self, count: int) -> None:
        if count == self.count:
            return
        self.count = count
        self.bus.emit_signal(None, LAUNCHER_PATH, "com.canonical.Unity.LauncherEntry", "Update",
                             GLib.Variant("(sa{sv})", (APP_URI, self._props())))


# ---- Recent chats as desktop actions --------------------------------------------------------

def chat_key(account_id: str, name: str) -> str:
    """A chat's id on command lines: stable, and without the chat's name in it."""
    return hashlib.sha1(f"{account_id}/{name}".encode()).hexdigest()[:16]


def menu_name(name: str) -> str:
    """A name as a menu shows it: one line, no control or invisible characters, at most MENU_NAME long."""
    text = " ".join(CONTROL.sub(" ", INVISIBLE.sub("", name)).split())
    return text if len(text) <= MENU_NAME else text[: MENU_NAME - 1].rstrip() + "…"


def exec_quote(arg: str) -> str:
    """One Exec= argument, quoted by the desktop entry rules (and then escaped as a string value)."""
    if CONTROL.search(arg):
        raise ValueError("a control character can't go in a desktop entry's Exec=")
    if arg and not re.search(r'[\s"\'\\><~|&;$*?#()`%=]', arg):
        return arg
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def without_chat_actions(text: str) -> str:
    """The entry with Natter's chat actions taken out (other actions and groups stay)."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith("["):
            skipping = line.startswith("[Desktop Action chat-")
        if skipping:
            continue
        if line.startswith("Actions="):
            kept = [a for a in line[len("Actions="):].split(";") if a and not a.startswith("chat-")]
            if not kept:
                continue
            line = "Actions=" + ";".join(kept) + ";"
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def with_chat_actions(text: str, chats: list[tuple[str, str]], launcher: Optional[str] = None) -> str:
    """The entry with one action per chat, as (account id, chat name) pairs.

    Exec= names the chat by its key, so a chat's name never reaches a command line.
    """
    base = without_chat_actions(text)
    actions = []
    for account, name in chats:
        label = menu_name(name)
        try:
            account_arg = exec_quote(account)
        except ValueError:
            continue
        if label:
            actions.append((label, f"--account {account_arg} --chat-id {chat_key(account, name)}"))
    if not actions:
        return base
    found = EXEC_PROGRAM.search(base)
    program = exec_quote(launcher) if launcher else found[1] if found else "natter"
    ids = [f"chat-{i}" for i in range(len(actions))]
    lines = base.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Actions="):
            lines[i] = line.rstrip(";") + ";" + ";".join(ids) + ";"
            break
    else:
        # Actions= belongs in the main group, before any other group starts.
        end = next((i for i, l in enumerate(lines) if l.startswith("[") and i > 0), len(lines))
        lines.insert(end, "Actions=" + ";".join(ids) + ";")
    for action, (label, args) in zip(ids, actions):
        escaped = label.replace("\\", "\\\\")
        lines += ["", f"[Desktop Action {action}]", f"Name={escaped}", f"Exec={program} {args}"]
    return "\n".join(lines) + "\n"


class DockMenu:
    """The recent chats in Natter's own menu entry.

    The desktop reads the whole entry again after every change, so the entry is rewritten only
    when the chats in it change, and at most every WRITE_EVERY seconds; a change that comes
    sooner waits for then.
    """

    def __init__(self, path: Path = ENTRY, root: Path = DATA_HOME) -> None:
        self.path = path
        self.root = root
        self.chats: Optional[frozenset] = None  # the chats the entry has, once Natter knows
        self.written_at = -math.inf
        self.pending: Optional[list[tuple[str, str]]] = None
        self.timer = 0

    def supported(self) -> bool:
        """Whether the entry is the user's own copy, outside a sandbox, so Natter may rewrite it."""
        if in_sandbox():
            return False
        try:
            return self.path.resolve(strict=True).is_relative_to(self.root.resolve())
        except OSError:
            return False

    def set(self, chats: list[tuple[str, str]], force: bool = False) -> bool:
        """Put these chats in the menu. True if the entry changed now."""
        chats = list(chats[:MENU_CHATS])
        if not force and frozenset(chats) == self.chats:
            self.pending = None
            return False
        wait = self.written_at + WRITE_EVERY - time.monotonic()
        if not force and wait > 0:
            self.pending = chats
            if not self.timer:
                self.timer = GLib.timeout_add_seconds(math.ceil(wait), self._later)
            return False
        self.pending = None
        return self._write(chats)

    def _later(self) -> bool:
        self.timer = 0
        if self.pending is not None:
            chats, self.pending = self.pending, None
            self._write(chats)
        return GLib.SOURCE_REMOVE

    def _write(self, chats: list[tuple[str, str]]) -> bool:
        if not self.supported():
            return False
        try:
            text = self.path.read_text()
        except OSError:
            return False
        updated = with_chat_actions(text, chats)
        if updated != text:
            tmp = self.path.with_suffix(".tmp")
            try:
                # Only the user may read it, as it names their recent chats; fchmod too, since
                # O_CREAT keeps the mode of a .tmp left from before.
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with open(fd, "w") as out:
                    os.fchmod(fd, 0o600)
                    out.write(updated)
                tmp.replace(self.path)
            except OSError:
                return False
            self.written_at = time.monotonic()
        self.chats = frozenset(chats)
        return updated != text


_dock_menu = DockMenu()


def set_menu_chats(chats: list[tuple[str, str]], force: bool = False) -> bool:
    """Put these (account id, chat name) pairs in the dock menu. True if the entry changed now.

    Without `force`, a change waits until five minutes after the last write.
    """
    return _dock_menu.set(chats, force)


def menu_supported() -> bool:
    """Whether Natter can put recent chats in its dock menu."""
    return _dock_menu.supported()
