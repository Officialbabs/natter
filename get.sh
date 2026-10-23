#!/bin/sh
# Natter Den in one command, for one user:
#
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh
#
# Downloads Natter Den's latest release (not the latest commit on master) into
# ~/.local/lib/natter/source, replacing the copy there, and runs its install.sh. Needs only curl (or
# wget) and tar: no git. Run it again to update to the newest release. With --flatpak it builds and
# installs the Flatpak instead, for systems too old to run Natter Den as it is:
#
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh -s -- --flatpak
#
# Everything is in main, called on the last line, so a download cut short runs nothing.
set -eu

APP_ID=io.github.Officialbabs.Natter
APP_NAME="Natter Den"
REPO="https://github.com/Officialbabs/natter"
LATEST_API="https://api.github.com/repos/Officialbabs/natter/releases/latest"
# NATTER_ARCHIVE: another .tar.gz to install, for testing get.sh only. No release is looked up.
ARCHIVE="${NATTER_ARCHIVE:-}"
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

# Whether $1 is a tag to download: v, then only letters, digits, dots, underscores and hyphens.
sane_tag() {
    case "$1" in
        v | v*[!0-9A-Za-z._-]*) return 1 ;;
        v*) return 0 ;;
        *) return 1 ;;
    esac
}

# Where the Releases page's link to the latest release leads: .../releases/tag/<tag> once there is one.
latest_redirect() {
    case "$fetch" in
        curl*) curl -fsIL -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" ;;
        # wget can't say where it ended up, but -S shows the Location of each redirect.
        *) wget -S --spider "$REPO/releases/latest" 2>&1 ;;
    esac
}

# The tag of Natter Den's latest release, from GitHub's API or, when that fails (it answers 60
# requests an hour from one address), from latest_redirect. Fails without a sane tag: with 2 when
# GitHub answered but has no release (the latest link leads to the Releases page itself), else 1.
latest_release() {
    page=""
    # shellcheck disable=SC2086  # $fetch is the command and its options
    tag="$($fetch "$LATEST_API" </dev/null 2>/dev/null |
        sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if ! sane_tag "$tag"; then
        page="$(latest_redirect </dev/null 2>/dev/null || true)"
        tag="$(printf '%s\n' "$page" | sed -n 's|.*/releases/tag/\([^/?#[:space:]]*\).*|\1|p' | head -n 1)"
    fi
    if sane_tag "$tag"; then
        echo "$tag"
    elif printf '%s\n' "$page" | grep -Eq "^(.*[[:space:]])?$REPO/releases([[:space:]].*)?\$"; then
        return 2
    else
        return 1
    fi
}

# Natter Den's source in SOURCE: its latest release from GitHub, as an archive (no git needed). A
# new copy is unpacked next to SOURCE and only then put in its place, so a failed download leaves
# the installed one as it was.
get_source() {
    if have curl; then fetch="curl -fsSL"
    elif have wget; then fetch="wget -qO-"
    else
        echo "$APP_NAME needs curl or wget to download itself. Install one, then run this again:"
        echo "  $(install_command curl)"
        exit 1
    fi
    if ! have tar; then
        echo "$APP_NAME needs tar to unpack itself. Install it, then run this again:"
        echo "  $(install_command tar)"
        exit 1
    fi
    if [ -e "$SOURCE" ] && ! [ -f "$SOURCE/install.sh" ]; then
        echo "$SHOWN is there but isn't $APP_NAME. Move it out of the way, then run this again."
        exit 1
    fi
    # Natter Den runs from SOURCE: replacing it underneath a running one could break it.
    if [ -e "$SOURCE" ] && running; then
        echo "$APP_NAME is running. Quit it first (Quit in its tray menu, or Ctrl+Q in its window),"
        echo "then run this again."
        exit 1
    fi
    release=""
    if [ -z "$ARCHIVE" ]; then
        found=0
        release="$(latest_release)" || found=$?
        if [ "$found" -eq 2 ]; then
            echo "There's no release of $APP_NAME on GitHub yet ($REPO/releases)."
            echo "Run this again once there is one."
            exit 1
        elif [ "$found" -ne 0 ]; then
            echo "Couldn't find $APP_NAME's latest release on GitHub."
            echo "Check your connection, then run this again in a moment."
            exit 1
        fi
        ARCHIVE="$REPO/archive/refs/tags/$release.tar.gz"
    fi
    mkdir -p "$(dirname "$SOURCE")"
    work="$(mktemp -d "$(dirname "$SOURCE")/.download.XXXXXX")"
    trap 'rm -rf "$work"' EXIT
    echo "==> Downloading $APP_NAME${release:+ $release}"
    # shellcheck disable=SC2086  # $fetch is the command and its options
    if ! $fetch "$ARCHIVE" >"$work/source.tar.gz" </dev/null; then
        echo "Couldn't download $APP_NAME. Check your connection, then run this again."
        exit 1
    fi
    mkdir "$work/source"
    if ! tar -xzf "$work/source.tar.gz" -C "$work/source" --strip-components=1 ||
        ! [ -f "$work/source/install.sh" ]; then
        echo "The download wasn't a complete copy of $APP_NAME. Run this again in a moment."
        exit 1
    fi
    if [ -e "$SOURCE" ]; then
        echo "==> Updating $SHOWN"
        mv "$SOURCE" "$work/old"
    else
        echo "==> Putting $APP_NAME in $SHOWN"
    fi
    mv "$work/source" "$SOURCE"
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
    if have pacman && pacman -Qq natter-den-git >/dev/null 2>&1; then
        echo "$APP_NAME is already installed as a system package (natter-den-git): update it the way"
        echo "you installed it."
        exit 0
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
