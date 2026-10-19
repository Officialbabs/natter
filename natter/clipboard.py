"""Ctrl+V for what WebKit can't paste into WhatsApp: pictures and files.

Text stays with WebKit. A screenshot, an image copied from another app, or files copied in
the file manager are read here and pasted into the open chat as files, which brings up
WhatsApp's preview with a caption box, the same as pasting in a browser does.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

from .settings import CACHE, private_folder

PASTED = CACHE / "pasted"
FILE_TYPES = ("x-special/gnome-copied-files", "application/vnd.portal.files", "text/uri-list")
KEEP_SECONDS = 24 * 3600


def has_pastable(formats: Gdk.ContentFormats) -> Optional[str]:
    """"files", "image", or None when it's something WebKit pastes itself (text)."""
    if formats.contain_gtype(Gdk.FileList) or any(formats.contain_mime_type(t) for t in FILE_TYPES):
        return "files"
    if formats.contain_gtype(Gdk.Texture) or any(t.startswith("image/") for t in (formats.get_mime_types() or [])):
        return "image"
    return None


def save_picture(texture: Gdk.Texture, what: str) -> Path:
    """A picture that came without a file (pasted, or dragged from a browser) saved as a PNG."""
    path = private_folder(PASTED) / time.strftime(f"{what} %Y-%m-%d at %H.%M.%S.png")
    texture.save_to_png(str(path))
    return path


def tidy(folder: Path = PASTED, keep: float = KEEP_SECONDS) -> None:
    """Forget pictures pasted more than a day ago."""
    cutoff = time.time() - keep
    for item in folder.glob("*.png") if folder.is_dir() else []:
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass


def read(clipboard: Gdk.Clipboard, then: Callable[[list[Path]], None]) -> bool:
    """Read pictures or files off the clipboard; `then(paths)` gets them ([] if there were none).

    Returns False straight away when the clipboard holds nothing for Natter (text: WebKit's).
    """
    kind = has_pastable(clipboard.get_formats())
    if kind is None:
        return False

    def got_files(board, result) -> None:
        try:
            files = board.read_value_finish(result)
        except GLib.Error:
            files = None
        paths = [Path(f.get_path()) for f in (files.get_files() if files else []) if f.get_path()]
        then([p for p in paths if p.is_file()])

    def got_image(board, result) -> None:
        try:
            texture = board.read_texture_finish(result)
        except GLib.Error:
            texture = None
        if texture is None:
            then([])
            return
        then([save_picture(texture, "Pasted image")])

    if kind == "files":
        clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, got_files)
    else:
        clipboard.read_texture_async(None, got_image)
    return True
