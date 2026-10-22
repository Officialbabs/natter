#!/bin/sh
# Natter Den in one command, for one user:
#
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh
#
# Gets Natter Den's source into ~/.local/lib/natter/source (or updates it there) and runs its
# install.sh. Run it again to update. With --flatpak it builds and installs the Flatpak instead,
# for systems too old to run Natter Den as it is:
#
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh -s -- --flatpak
#
# Everything is in main, called on the last line, so a download cut short runs nothing.
set -eu

APP_ID=io.github.Officialbabs.Natter
APP_NAME="Natter Den"
# NATTER_REPO: another repository to clone, for testing get.sh only.
REPO="${NATTER_REPO:-https://github.com/Officialbabs/natter.git}"
GET="curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh"
# Next to the camera fix (install.sh's CAMFIX_DIR), away from the WhatsApp data in ~/.local/share/natter.
SOURCE="$HOME/.local/lib/natter/source"
# shellcheck disable=SC2088  # the tilde is for reading, not for expanding
SHOWN="~/.local/lib/natter/source"  # SOURCE as people read it
BUILD="${XDG_CACHE_HOME:-$HOME/.cache}/natter-den-flatpak"

have() { command -v "$1" >/dev/null 2>&1; }

# The checkout an install from a checkout runs from, as install.sh writes it into the launcher.
launcher_dir() {
    if [ -f "$HOME/.local/bin/natter" ]; then
        sed -n 's/^NATTER_DIR = "\(.*\)"$/\1/p' "$HOME/.local/bin/natter"
    fi
}

# The command that installs the packages $@ with this system's package manager.
install_command() {
    if have rpm-ostree; then echo "rpm-ostree install $*   # then restart"
    elif have apt; then echo "sudo apt install $*"
    elif have dnf; then echo "sudo dnf install $*"
    elif have pacman; then echo "sudo pacman -S --needed $*"
    elif have zypper; then echo "sudo zypper install $*"
    else echo "(install $* from your distribution's packages)"
    fi
}

# What the Flatpak's build needs and isn't here: flatpak, flatpak-builder, both or neither.
# (Only the missing ones: rpm-ostree refuses to install a package the system already has.)
flatpak_missing() {
    missing=""
    have flatpak || missing="flatpak"
    have flatpak-builder || missing="${missing:+$missing }flatpak-builder"
    echo "$missing"
}

# Natter Den's source in SOURCE: cloned the first time, brought up to date after that.
get_source() {
    if ! have git; then
        echo "$APP_NAME needs git to fetch its source. Install it, then run this again:"
        echo "  $(install_command git)"
        exit 1
    fi
    if [ -e "$SOURCE/.git" ]; then
        # Natter Den runs from SOURCE: updating it underneath a running one could break it.
        if running; then
            echo "$APP_NAME is running. Quit it first (Quit in its tray menu, or Ctrl+Q in its window),"
            echo "then run this again."
            exit 1
        fi
        echo "==> Updating $SHOWN"
        if ! GIT_TERMINAL_PROMPT=0 git -C "$SOURCE" pull --ff-only --quiet </dev/null; then
            echo "Couldn't update $SHOWN (see above). To start over: rm -rf $SHOWN, then run this again."
            exit 1
        fi
    elif [ -e "$SOURCE" ]; then
        echo "$SHOWN is there but isn't a git checkout. Move it out of the way, then run this again."
        exit 1
    else
        echo "==> Getting $APP_NAME into $SHOWN"
        mkdir -p "$(dirname "$SOURCE")"
        if ! GIT_TERMINAL_PROMPT=0 git clone --quiet "$REPO" "$SOURCE" </dev/null; then
            echo "Couldn't download $APP_NAME from $REPO (see above). Check your connection, then run this again."
            exit 1
        fi
    fi
}

# Whether Natter Den runs here as the system is: its own start-up check, with the Pythons
# install.sh tries.
runs_here() {
    for python in /usr/bin/python3 "$(command -v python3 || true)"; do
        if [ -n "$python" ] && [ -x "$python" ] &&
            "$python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from natter.__main__ import check; sys.exit(check())' \
                "$SOURCE" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

# Whether Natter Den is running: it owns its name on the session bus (as install.sh checks).
running() {
    have gdbus &&
        gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
            --method org.freedesktop.DBus.NameHasOwner "$APP_ID" 2>/dev/null | grep -q true
}

install_native() {
    if have pacman; then
        if pacman -Qq natter-den-git >/dev/null 2>&1; then
            echo "$APP_NAME is already installed as a system package (natter-den-git). To update it, run"
            echo "makepkg -si again in packaging/aur/natter-den-git of a fresh copy of the source."
            exit 0
        fi
        echo "Note: this installs $APP_NAME for you alone. For a system-wide Arch package instead, run"
        echo "makepkg -si in packaging/aur/natter-den-git of the source."
    fi
    old="$(launcher_dir)"
    get_source

    status=0
    sh "$SOURCE/install.sh" </dev/null || status=$?
    if [ "$status" -ne 0 ]; then
        if runs_here; then
            exit "$status"  # install.sh said why
        fi
        echo
        echo "Once they're installed, run this again. If your distribution's are too old, or you'd rather"
        echo "not install them, the $APP_NAME Flatpak brings everything it needs (GNOME's runtime, a large"
        echo "download the first time):"
        tools="$(flatpak_missing)"
        if [ -n "$tools" ]; then
            echo "  $(install_command "$tools")"
        fi
        echo "  $GET -s -- --flatpak"
        exit "$status"
    fi

    if [ -n "$old" ] && [ "$old" != "$SOURCE" ]; then
        echo "Note: $APP_NAME now runs from $SHOWN; $old is no longer used."
    fi
    echo
    echo "Start:     $APP_NAME in your apps, or 'natter'"
    echo "Update:    $GET"
    echo "Uninstall: $SHOWN/install.sh --uninstall && rm -rf ~/.local/lib/natter"
    echo "           (your WhatsApp sessions stay in ~/.local/share/natter until you delete it)"
}

install_flatpak() {
    tools="$(flatpak_missing)"
    if [ -n "$tools" ]; then
        echo "The $APP_NAME Flatpak is built with flatpak-builder. Install what's missing, then run this again:"
        echo "  $(install_command "$tools")"
        exit 1
    fi
    get_source
    echo "==> Adding Flathub and GNOME's runtime, for you alone"
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user -y --or-update flathub org.gnome.Sdk//50 org.gnome.Platform//50 </dev/null
    echo "==> Building and installing the $APP_NAME Flatpak"
    mkdir -p "$BUILD"
    flatpak-builder --user --install --force-clean --state-dir="$BUILD/state" \
        "$BUILD/build" "$SOURCE/packaging/$APP_ID.yml" </dev/null
    checkout="$(launcher_dir)"
    if [ -n "$checkout" ]; then
        echo "Note: $APP_NAME is also installed from $checkout, and that menu entry hides the Flatpak's."
        echo "Remove it with: \"$checkout/install.sh\" --uninstall"
    fi
    echo
    echo "Start:     $APP_NAME in your apps, or 'flatpak run $APP_ID'"
    echo "Update:    $GET -s -- --flatpak"
    echo "Uninstall: flatpak uninstall --user $APP_ID && rm -rf ~/.local/lib/natter/source ~/.cache/natter-den-flatpak"
    echo "           (your WhatsApp sessions stay in ~/.var/app/$APP_ID until you delete it)"
}

main() {
    if [ "$(id -u)" -eq 0 ]; then
        echo "Run this as yourself, not as root: it installs $APP_NAME for one user."
        exit 1
    fi
    case "$*" in
        "") install_native ;;
        --flatpak) install_flatpak ;;
        *) echo "Usage: get.sh [--flatpak]" >&2; exit 2 ;;
    esac
}

main "$@"
