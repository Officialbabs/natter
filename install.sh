#!/bin/sh
# Natter installer: launcher, menu entry, icons, whatsapp:// links. `./install.sh --uninstall` removes them.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
ICONS="$DATA/icons/hicolor/scalable/apps"
ENTRY="$DATA/applications/io.natter.Natter.desktop"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/natter" "$ENTRY" "$ICONS/io.natter.Natter.svg" "$ICONS/io.natter.Natter-unread.svg" \
          "$CONFIG/autostart/io.natter.Natter.desktop"
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
    echo "Natter removed. Your WhatsApp sessions and settings are still in $DATA/natter and $CONFIG/natter;"
    echo "delete those folders too to sign out everywhere."
    exit 0
fi

python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("WebKit", "6.0")' 2>/dev/null || {
    echo "Natter needs PyGObject with GTK 4, libadwaita and WebKitGTK 6.0:"
    echo "  sudo apt-get install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0"
    echo "For calls, also: gstreamer1.0-plugins-bad gstreamer1.0-nice"
    exit 1
}

echo "==> Installing launcher, icons and menu entry"
mkdir -p "$HOME/.local/bin" "$DATA/applications" "$ICONS"
ln -sf "$HERE/bin/natter" "$HOME/.local/bin/natter"
cp "$HERE/data/icons/hicolor/scalable/apps/"*.svg "$ICONS/"
# Keep the recent-chat actions Natter already wrote, if any.
sed -e "s|@EXEC@|$HOME/.local/bin/natter|" "$HERE/natter.desktop.in" > "$ENTRY.new"
if [ -f "$ENTRY" ] && grep -q '^\[Desktop Action chat-' "$ENTRY"; then
    grep '^Actions=' "$ENTRY" >> "$ENTRY.new" || true
    sed -n '/^\[Desktop Action chat-/,$p' "$ENTRY" | sed '1s/^/\n/' >> "$ENTRY.new"
fi
mv "$ENTRY.new" "$ENTRY"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
command -v xdg-mime >/dev/null && xdg-mime default io.natter.Natter.desktop x-scheme-handler/whatsapp || true

echo "Done. Open Natter from your apps, or run 'natter'."
