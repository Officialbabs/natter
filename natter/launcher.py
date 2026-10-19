"""Natter's dock icon: the unread badge, and recent chats in its right-click menu."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from gi.repository import Gio, GLib

from . import APP_ID

ENTRY = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications" / f"{APP_ID}.desktop"
APP_URI = f"application://{APP_ID}.desktop"
LAUNCHER_PATH = "/io/natter/Natter/LauncherEntry"
MENU_CHATS = 5

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

def exec_quote(arg: str) -> str:
    """One Exec= argument, quoted by the desktop entry rules (and then escaped as a string value)."""
    if arg and not re.search(r'[\s"\'\\><~|&;$*?#()`%=]', arg):
        return arg
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def value_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", " ").replace("\r", " ").replace("\t", " ")


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
    """The entry with one action per chat, as (account id, chat name) pairs."""
    base = without_chat_actions(text)
    if not chats:
        return base
    found = re.search(r"^Exec=(\S+)", base, re.M)
    program = launcher or (found[1] if found else "natter")
    ids = [f"chat-{i}" for i in range(len(chats))]
    lines = base.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Actions="):
            lines[i] = line.rstrip(";") + ";" + ";".join(ids) + ";"
            break
    else:
        # Actions= belongs in the main group, before any other group starts.
        end = next((i for i, l in enumerate(lines) if l.startswith("[") and i > 0), len(lines))
        lines.insert(end, "Actions=" + ";".join(ids) + ";")
    for action, (account, name) in zip(ids, chats):
        lines += ["", f"[Desktop Action {action}]", f"Name={value_escape(name)}",
                  f"Exec={program} --account {exec_quote(account)} --chat {exec_quote(name)}"]
    return "\n".join(lines) + "\n"


def set_menu_chats(chats: list[tuple[str, str]], path: Path = ENTRY) -> bool:
    """Write the recent chats into Natter's menu entry, if Natter is installed. True if it changed."""
    try:
        text = path.read_text()
    except OSError:
        return False
    updated = with_chat_actions(text, chats[:MENU_CHATS])
    if updated == text:
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(updated)
    tmp.replace(path)
    return True
