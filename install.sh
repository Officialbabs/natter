#!/bin/sh
# Natter for one user, run from this checkout: the `natter` command, the menu entry, icons,
# desktop search and whatsapp:// links. `./install.sh --uninstall` removes them. Either stops
# while Natter runs, unless --force is given too.
# First it installs the packages Natter needs and this system lacks, with the distribution's
# package manager and sudo, once the person agrees on the terminal: --yes agrees without asking,
# --no-deps never installs (it says what to install, and stops if Natter can't run without it).
# With no terminal to ask on and no --yes, nothing is installed, as with --no-deps.
# (Distribution packages and the Flatpak install with meson.build instead.)
set -e
APP_ID=io.github.Officialbabs.Natter
OLD_APP_ID=io.natter.Natter  # Natter's id before the rename: an install of it is moved over
APP_NAME="Natter Den"  # the name people see, as NAME in natter/__init__.py
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/natter"
APPS="$DATA/applications"
ENTRY="$APPS/$APP_ID.desktop"
ICONS="$DATA/icons/hicolor/scalable/apps"
SYMBOLIC="$DATA/icons/hicolor/symbolic/apps"
SEARCH="$DATA/gnome-shell/search-providers"
SERVICES="$DATA/dbus-1/services"
AUTOSTART="$CONFIG/autostart"
# The camera fix's GStreamer plugin (natter/camfix), where natter/gstfix.py looks for it.
CAMFIX_DIR="$HOME/.local/lib/natter/gstreamer-1.0"

# $1 made safe as the replacement in a sed s|…|…| command.
sed_escape() { printf '%s\n' "$1" | sed -e 's/[\\&|]/\\&/g'; }

# $1 as one argument of a desktop entry's Exec=: quoted when it needs to be, then escaped
# again as a string value, as the Desktop Entry spec says.
exec_quote() {
    case "$1" in
        *[!A-Za-z0-9_./+-]*)
            printf '"%s"' "$(printf '%s\n' "$1" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g')" ;;
        *) printf '%s' "$1" ;;
    esac
}

# $1 as one argument of a D-Bus service's Exec=, which D-Bus splits the way a shell does.
shell_quote() { printf "'%s'" "$(printf '%s\n' "$1" | sed "s/'/'\\\\''/g")"; }

# $1 as a Python string literal.
py_quote() { printf '"%s"' "$(printf '%s\n' "$1" | sed -e 's/[\\"]/\\&/g')"; }

# Everything an install under the id $1 puts in place, but the autostart entry.
remove_installed() {
    rm -f "$APPS/$1.desktop" "$ICONS/$1.svg" "$ICONS/$1-unread.svg" \
          "$SYMBOLIC/$1-symbolic.svg" "$SYMBOLIC/$1-unread-symbolic.svg" \
          "$SEARCH/$1.search-provider.ini" "$SERVICES/$1.service"
}

# An install from before the rename: its entry, icons and search files go, and its autostart
# entry moves to the new name, starting the new launcher.
retire_old() {
    remove_installed "$OLD_APP_ID"
    if [ -f "$AUTOSTART/$OLD_APP_ID.desktop" ]; then
        sed -e "s|^Exec=.*|Exec=$(sed_escape "$EXEC") --background|" -e "s|^Icon=.*|Icon=$(sed_escape "$ICONS/$APP_ID.svg")|" \
            "$AUTOSTART/$OLD_APP_ID.desktop" > "$AUTOSTART/$APP_ID.desktop"
        rm -f "$AUTOSTART/$OLD_APP_ID.desktop"
    fi
}

refresh_caches() {
    if command -v update-desktop-database >/dev/null; then update-desktop-database -q "$APPS" || true; fi
    if command -v gtk-update-icon-cache >/dev/null; then gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true; fi
}

# The camera fix: WebKit's camera freezes on its first frame with GStreamer 1.28 and newer, and
# natter/camfix/nattercamfix.c, a GStreamer tracer, prevents it (natter/gstfix.py turns it on only
# where it's needed). Built when there's a C compiler, pkg-config and GStreamer's headers; Natter
# runs without it. $1: the Python that runs Natter, which tells whether GStreamer needs it.
build_camfix() {
    python="$1"
    # The compiler, as make takes $CC: a command that may carry arguments ("ccache gcc", "gcc -m64").
    # Split into this function's own arguments ($1 is the program); no file name globbing.
    set -f
    # shellcheck disable=SC2086
    set -- ${CC:-cc}
    set +f
    if ! command -v "$1" >/dev/null 2>&1 || ! command -v pkg-config >/dev/null 2>&1 ||
        ! pkg-config --exists gstreamer-1.0 2>/dev/null; then
        # A hint only where the camera needs it: GStreamer 1.28 or newer (or a version unknown).
        if "$python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from natter.gstfix import FIRST_AFFECTED, gstreamer_version
v = gstreamer_version(); sys.exit(0 if v is None or v >= FIRST_AFFECTED else 1)' "$HERE" 2>/dev/null; then
            echo "Note: skipped the camera fix, which needs a C compiler, pkg-config and GStreamer's headers (on Debian and Ubuntu: build-essential pkg-config libgstreamer1.0-dev); without it the camera can freeze in calls. Run this again once they're installed."
        fi
        return 0
    fi
    echo "==> Building the camera fix into $CAMFIX_DIR"
    mkdir -p "$CAMFIX_DIR"
    # Built beside the old one and moved over it, so a running Natter keeps the copy it loaded.
    # shellcheck disable=SC2046  # pkg-config's flags are meant to split
    if "$@" -shared -fPIC -O2 -Wall -o "$CAMFIX_DIR/libnattercamfix.so.new" "$HERE/natter/camfix/nattercamfix.c" \
            $(pkg-config --cflags --libs gstreamer-1.0); then
        mv -f "$CAMFIX_DIR/libnattercamfix.so.new" "$CAMFIX_DIR/libnattercamfix.so"
    else
        rm -f "$CAMFIX_DIR/libnattercamfix.so.new"
        echo "Note: the camera fix didn't build (see above); $APP_NAME works without it, but the camera can freeze in calls."
    fi
}

# Whether a Natter with the id $1 is running: it owns that name on the session bus.
running() {
    command -v gdbus >/dev/null &&
        gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
            --method org.freedesktop.DBus.NameHasOwner "$1" 2>/dev/null | grep -q true
}

UNINSTALL=""
FORCE=""
YES=""
NO_DEPS=""
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --force) FORCE=1 ;;
        --yes | -y) YES=1 ;;
        --no-deps) NO_DEPS=1 ;;
        *) echo "Usage: $0 [--yes | --no-deps] [--force] | --uninstall [--force]" >&2; exit 2 ;;
    esac
done

# A running Natter rewrites its menu entry as its recent chats change, and one from before the
# rename holds the WhatsApp session the new one would open: it has to quit first.
if [ -z "$FORCE" ]; then
    for id in "$OLD_APP_ID" "$APP_ID"; do
        if running "$id"; then
            if [ "$id" = "$OLD_APP_ID" ]; then what="The old Natter"; else what="$APP_NAME"; fi
            echo "$what ($id) is running. Quit it first (Quit in its tray menu, or Ctrl+Q in its window),"
            echo "then run this again. To go ahead while it runs: $0${*:+ $*} --force"
            exit 1
        fi
    done
fi

if [ -n "$UNINSTALL" ]; then
    rm -f "$LAUNCHER" "$AUTOSTART/$APP_ID.desktop" "$AUTOSTART/$OLD_APP_ID.desktop" "$CAMFIX_DIR/libnattercamfix.so"
    rmdir "$CAMFIX_DIR" "$(dirname "$CAMFIX_DIR")" 2>/dev/null || true
    remove_installed "$APP_ID"
    remove_installed "$OLD_APP_ID"
    refresh_caches
    echo "$APP_NAME removed. Your WhatsApp sessions and settings are still in $DATA/natter and $CONFIG/natter"
    echo "(and a cache in ${XDG_CACHE_HOME:-$HOME/.cache}/natter); delete those folders too to sign out everywhere."
    exit 0
fi

# The Python to run Natter with: the system's own first, since that's the one distributions
# give PyGObject to, then the first python3 on PATH. The check is Natter's own start-up check.
CHECK='import sys
if sys.version_info < (3, 9):
    sys.exit("Natter Den needs Python 3.9 or newer, and this is Python %d.%d: install the Natter Den Flatpak instead (see README.md)." % sys.version_info[:2])
sys.path.insert(0, sys.argv[1]); from natter.__main__ import check; sys.exit(check())'
find_python() {
    PYTHON=""
    for candidate in /usr/bin/python3 "$(command -v python3 || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c "$CHECK" "$HERE" >/dev/null 2>&1; then
            PYTHON="$candidate"
            return 0
        fi
    done
    return 1
}

# The first Python there is, which says what's missing (it needs no PyGObject for that).
any_python() {
    if [ -x /usr/bin/python3 ]; then echo /usr/bin/python3; else command -v python3 || true; fi
}

# What's missing here and the packages that bring it on this distribution, a group per line:
# natter/__main__.py's deps(), with the package names in natter/distro.py.
DEPS='import sys
if sys.version_info < (3, 9):
    sys.exit(1)
sys.path.insert(0, sys.argv[1]); from natter.__main__ import deps; sys.exit(deps())'

# The rest of the report's line that starts with the word $1 ("" when there's none).
field() { printf '%s\n' "$REPORT" | sed -n "s/^$1 //p" | head -n 1; }

# Whether there's a terminal to ask on. Under "curl … | sh" stdin is the script, but /dev/tty is
# still the person's terminal.
have_tty() { (: </dev/tty >/dev/tty) 2>/dev/null; }

# Ask $1 on the terminal: yes unless the answer is no. --yes says yes without asking.
ask() {
    if [ -n "$YES" ]; then return 0; fi
    printf '%s [Y/n] ' "$1" >/dev/tty
    answer=""
    if ! read -r answer </dev/tty; then
        echo >/dev/tty
        return 1
    fi
    case "$answer" in
        "" | [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# The package manager's install command, as people type it.
install_words() {
    case "$MANAGER" in
        apt) echo "apt-get install" ;;
        dnf) echo "dnf install" ;;
        pacman) echo "pacman -S --needed" ;;
        zypper) echo "zypper install" ;;
    esac
}

# The command that installs the packages $@, to show.
shown() { echo "${SUDO:+sudo }$(install_words) $*"; }

# Install the packages $@, as root, answering the package manager's own question: the person
# agreed to these already.
run_install() {
    case "$MANAGER" in
        apt) set -- apt-get install -y "$@" ;;
        dnf) set -- dnf install -y "$@" ;;
        pacman) set -- pacman -S --needed --noconfirm "$@" ;;
        zypper) set -- zypper --non-interactive install "$@" ;;
    esac
    if [ -n "$SUDO" ]; then set -- sudo "$@"; fi
    if have_tty; then "$@" </dev/tty; else "$@" </dev/null; fi
}

# This system is too old for Natter as it is, for the reason $1: the Flatpak is the way.
too_old() {
    echo "$APP_NAME can't run here as it is: $1."
    echo "This system is too old for the native version of $APP_NAME. Install its Flatpak instead, which"
    echo "brings everything it needs and runs on older systems too: see \"Flatpak\" in README.md."
    if [ -f "$HERE/get.sh" ]; then
        echo "  sh \"$HERE/get.sh\" --flatpak   # builds and installs it for you"
    fi
}

# Offer to install what Natter needs and this system lacks, in three groups: required (Natter
# doesn't run without it), recommended (WhatsApp's media and calls) and optional (the camera
# fix's build tools, where GStreamer needs the fix). Never runs sudo unless the person agrees,
# or gave --yes. Exits when Natter still can't run; on a distribution whose packages Natter
# doesn't know by name, it leaves that to the start-up check's own explanation.
get_missing() {
    py="${PYTHON:-$(any_python)}"  # the one that runs Natter, when there is one
    if [ -z "$py" ]; then return 0; fi
    REPORT="$("$py" -c "$DEPS" "$HERE" 2>/dev/null)" || REPORT=""
    MANAGER="$(field manager)"
    required="$(field required)"
    recommended="$(field recommended)"
    optional="$(field optional)"
    reason="$(field too-old)"
    if [ -n "$reason" ] && [ -z "$PYTHON" ]; then
        too_old "$reason"
        exit 1
    fi
    if [ -z "$MANAGER" ]; then
        # A distribution whose packages Natter doesn't know by name: what's missing, in words.
        # (Natter can't run either: the start-up check's own explanation says so, below.)
        if [ -n "$PYTHON" ]; then
            words="$(field recommended-words)"
            if [ -n "$words" ]; then echo "Recommended, and not installed here (your distribution's packages have them): $words."; fi
            words="$(field optional-words)"
            if [ -n "$words" ]; then echo "Optional, to build the camera fix, and not installed here: $words."; fi
        fi
        return 0
    fi
    if [ -z "$required$recommended$optional" ]; then return 0; fi

    if [ "$(id -u)" -eq 0 ]; then SUDO=""; can=1
    elif command -v sudo >/dev/null 2>&1; then SUDO=sudo; can=1
    else SUDO=""; can=""
    fi
    echo "$APP_NAME needs packages that aren't installed here:"
    if [ -n "$required" ]; then
        echo "  Required, for $APP_NAME to run:"
        echo "    $required"
    fi
    if [ -n "$recommended" ]; then
        echo "  Recommended, for WhatsApp's videos and voice messages, and for calls:"
        echo "    $recommended"
    fi
    if [ -n "$optional" ]; then
        echo "  Optional, to build the camera fix (without it the camera can freeze in calls):"
        echo "    $optional"
    fi
    printf '%s\n' "$REPORT" | sed -n 's/^note /Note: /p'

    # Lists of package names, one word each: unquoted, they join with single spaces.
    # shellcheck disable=SC2086
    main="$(echo $required $recommended)"
    chosen=""
    left="$main $optional"
    if [ -n "$NO_DEPS" ]; then
        :
    elif [ -z "$can" ]; then
        echo "There's no sudo here to install them with: install them as root."
    elif [ -z "$YES" ] && ! have_tty; then
        echo "There's no terminal to ask on, so nothing was installed: run this with --yes to install them."
    else
        left=""
        if [ -n "$main" ]; then
            if [ -n "$required" ] && [ -n "$recommended" ]; then what="the required and recommended ones"
            elif [ -n "$required" ]; then what="the required ones"
            else what="the recommended ones"
            fi
            if ask "Install $what now ($(shown "…"))?"; then chosen="$main"; else left="$main"; fi
        fi
        if [ -n "$optional" ] && [ -n "$required" ] && [ -z "$chosen" ]; then
            left="$left $optional"  # no use without the required ones
        elif [ -n "$optional" ]; then
            if [ -n "$chosen" ]; then question="And the optional ones?"; else question="Install the optional ones now ($(shown "…"))?"; fi
            if ask "$question"; then chosen="$chosen $optional"; else left="$left $optional"; fi
        fi
    fi
    # shellcheck disable=SC2086
    chosen="$(echo $chosen)" left="$(echo $left)"
    installed=""
    if [ -n "$chosen" ]; then
        echo "==> Installing them: $(shown "$chosen")"
        # shellcheck disable=SC2086
        if run_install $chosen; then
            installed="$chosen"
        else
            echo "Couldn't install them (see above)."
            case "$MANAGER" in
                apt) echo "If some couldn't be found or downloaded, update the package lists first (${SUDO:+sudo }apt-get update)." ;;
                pacman) echo "If some couldn't be found or downloaded, update the system first (${SUDO:+sudo }pacman -Syu)." ;;
            esac
            # shellcheck disable=SC2086
            left="$(echo $chosen $left)"
        fi
        find_python || true
    fi
    if [ -n "$left" ]; then
        echo "To install them yourself:"
        echo "  $(shown "$left")"
    fi
    if [ -z "$PYTHON" ]; then
        if [ -n "$installed" ] && [ -n "$main" ] && [ "${installed#"$main"}" != "$installed" ]; then
            "$py" -c "$CHECK" "$HERE" || true  # what's still wrong, and what to do about it
        else
            echo "$APP_NAME can't run without the required ones: install them, then run this again."
        fi
        exit 1
    fi
}

find_python || true
if [ -z "$NO_DEPS" ] || [ -z "$PYTHON" ]; then
    get_missing
fi
if [ -z "$PYTHON" ]; then
    first="$(command -v python3 || true)"
    if [ -z "$first" ]; then
        echo "$APP_NAME needs Python 3 with PyGObject, GTK 4, libadwaita and WebKitGTK 6.0."
    else
        "$first" -c "$CHECK" "$HERE" || true  # says what's missing, and how to install it here
    fi
    exit 1
fi

# Who opens whatsapp:// links now, asked before Natter's entry joins the list.
handler=""
if command -v xdg-mime >/dev/null; then
    handler="$(xdg-mime query default x-scheme-handler/whatsapp 2>/dev/null || true)"
fi

echo "==> Installing the natter command (with $PYTHON)"
mkdir -p "$BIN"
# A copy rather than a link, so it can name its Python and this checkout. mv replaces the link
# that older installers made, not the file it pointed at.
sed -e "1s|.*|#!$(sed_escape "$PYTHON")|" \
    -e "s|^NATTER_DIR = \"\".*|NATTER_DIR = $(sed_escape "$(py_quote "$HERE")")|" \
    "$HERE/bin/natter" > "$LAUNCHER.new"
chmod 755 "$LAUNCHER.new"
mv -f "$LAUNCHER.new" "$LAUNCHER"
"$PYTHON" -m compileall -q "$HERE/natter" >/dev/null 2>&1 || true
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN isn't on your PATH, so typing 'natter' in a terminal won't find it. The menu entry works anyway." ;;
esac
build_camfix "$PYTHON"

echo "==> Installing the menu entry, icons and desktop search"
EXEC="$(exec_quote "$LAUNCHER")"
mkdir -p "$APPS" "$ICONS" "$SYMBOLIC" "$SEARCH" "$SERVICES"
cp "$HERE/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$HERE/data/icons/hicolor/scalable/apps/$APP_ID-unread.svg" "$ICONS/"
# The tray icon in the panel's own colour (tray.py).
cp "$HERE/data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
   "$HERE/data/icons/hicolor/symbolic/apps/$APP_ID-unread-symbolic.svg" "$SYMBOLIC/"
# The icon by its full path, for shells (a Qt one, say) that don't look icons up by name in
# ~/.local/share/icons, which has no index.theme.
sed -e "s|@EXEC@|$(sed_escape "$EXEC")|" -e "s|^Icon=.*|Icon=$(sed_escape "$ICONS/$APP_ID.svg")|" \
    "$HERE/natter.desktop.in" > "$ENTRY.new"
# Keep the recent-chat actions Natter already wrote, if any.
if [ -f "$ENTRY" ] && grep -q '^\[Desktop Action chat-' "$ENTRY"; then
    grep '^Actions=' "$ENTRY" >> "$ENTRY.new" || true
    { echo; sed -n '/^\[Desktop Action chat-/,$p' "$ENTRY"; } >> "$ENTRY.new"
fi
mv "$ENTRY.new" "$ENTRY"
# Desktop search: the provider file desktops look for (AutoStart=false: only a running Natter
# is asked), and the D-Bus service behind its bus name.
cp "$HERE/data/$APP_ID.search-provider.ini" "$SEARCH/"
sed -e "s|@EXEC@|$(sed_escape "$(shell_quote "$LAUNCHER")")|" "$HERE/data/$APP_ID.service.in" > "$SERVICES/$APP_ID.service"

retire_old
refresh_caches
# Ada's background allow list names apps by id: an entry for the old one no longer counts.
LIFECYCLE="$CONFIG/os-ai/lifecycle.ini"
if [ -f "$LIFECYCLE" ] && grep -qF "$OLD_APP_ID" "$LIFECYCLE"; then
    echo "Note: $LIFECYCLE names $OLD_APP_ID. Replace it with $APP_ID there, so $APP_NAME keeps receiving messages while hidden."
fi

# whatsapp:// links: take them only if no other app had them (ZapZap, say) or the old Natter
# did, and say so explicitly, since Natter's entry in ~/.local would otherwise win on its own.
if command -v xdg-mime >/dev/null; then
    mkdir -p "$CONFIG"  # xdg-mime writes mimeapps.list there and fails if the folder isn't there yet
    case "$handler" in
        "" | "$APP_ID.desktop" | "$OLD_APP_ID.desktop")
            xdg-mime default "$APP_ID.desktop" x-scheme-handler/whatsapp ;;
        *)
            xdg-mime default "$handler" x-scheme-handler/whatsapp
            echo "whatsapp:// links still open in $handler. To open them in $APP_NAME instead:"
            echo "  xdg-mime default $APP_ID.desktop x-scheme-handler/whatsapp" ;;
    esac
fi

# WhatsApp's videos and voice messages play through GStreamer: say if codecs are missing, or
# if they couldn't be checked.
"$PYTHON" -c '
import sys
sys.path.insert(0, sys.argv[1])
from natter.system import media_report
missing, hint = media_report()
if missing:
    print("Some WhatsApp media needs codecs GStreamer lacks here:", ", ".join(missing))
    if hint:
        print("  " + hint)
elif hint:
    print(hint)
' "$HERE" 2>/dev/null || true

# Again at the end: an old Natter still running (--force) may have written its entries back.
retire_old
refresh_caches
# Both would open the same WhatsApp sessions, so the old one has to go before the new one starts.
if running "$OLD_APP_ID"; then
    echo "Natter from before the rename is still running. Quit it (Quit in its tray menu, or Ctrl+Q)"
    echo "before opening the new one: both would use the same WhatsApp session."
fi

echo "Done. Open $APP_NAME from your apps, or run 'natter'."
