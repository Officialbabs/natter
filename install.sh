#!/bin/sh
# Natter installer: launcher, menu entry, icons, whatsapp:// links. `./install.sh --uninstall` removes them.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
ICONS="$DATA/icons/hicolor/scalable/apps"
ENTRY="$DATA/applications/io.natter.Natter.desktop"
SEARCH="$DATA/gnome-shell/search-providers/io.natter.Natter.search-provider.ini"
SERVICE="$DATA/dbus-1/services/io.natter.Natter.service"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/natter" "$ENTRY" "$ICONS/io.natter.Natter.svg" "$ICONS/io.natter.Natter-unread.svg" \
          "$CONFIG/autostart/io.natter.Natter.desktop" "$SEARCH" "$SERVICE"
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

# Who opens whatsapp:// links now, asked before Natter's entry joins the list.
handler=""
command -v xdg-mime >/dev/null && handler="$(xdg-mime query default x-scheme-handler/whatsapp 2>/dev/null)"

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

# Desktop search: the provider file desktops look for, and a D-Bus service so a search can
# start Natter (hidden) when it isn't running.
mkdir -p "$(dirname "$SEARCH")" "$(dirname "$SERVICE")"
cp "$HERE/data/io.natter.Natter.search-provider.ini" "$SEARCH"
sed -e "s|@EXEC@|$HOME/.local/bin/natter|" "$HERE/data/io.natter.Natter.service.in" > "$SERVICE"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

# whatsapp:// links: take them only if no other app had them (ZapZap, say), and say so
# explicitly, since Natter's entry in ~/.local would otherwise win on its own.
if command -v xdg-mime >/dev/null; then
    if [ -z "$handler" ] || [ "$handler" = "io.natter.Natter.desktop" ]; then
        xdg-mime default io.natter.Natter.desktop x-scheme-handler/whatsapp
    else
        xdg-mime default "$handler" x-scheme-handler/whatsapp
        echo "whatsapp:// links still open in $handler. To open them in Natter instead:"
        echo "  xdg-mime default io.natter.Natter.desktop x-scheme-handler/whatsapp"
    fi
fi

echo "Done. Open Natter from your apps, or run 'natter'."
