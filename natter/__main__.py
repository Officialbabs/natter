"""Natter: WhatsApp for Linux that feels like part of the desktop.

  natter                      open Natter (or bring it forward)
  natter --background         start hidden, for login: notifications and the tray still work
  natter --chat NAME          open a chat by name
  natter https://wa.me/123…   open a wa.me or whatsapp:// link
  natter FILE…                send files: pick the chat, WhatsApp shows its preview
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        gi.require_version("WebKit", "6.0")
    except (ImportError, ValueError) as error:
        print(f"Natter needs GTK 4, libadwaita and WebKitGTK 6.0 for Python ({error}):", file=sys.stderr)
        print("  sudo apt-get install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0", file=sys.stderr)
        return 1
    from .app import main as run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
