"""Updates: whether a newer release of Natter Den is out, telling the user, and, for the copy get.sh
installed (~/.local/lib/natter/source), updating it in place.

At most once a day Natter asks GitHub's API for the latest release, and when that fails, where
the Releases page's latest link leads, as get.sh does (Checker). Nothing is sent but the request:
a constant User-Agent, no cookies, no ETag, no version and no ids. What's found goes in the
settings (update_*), and the banner, the tray, a notification and Preferences tell the user
(Updates). How to update depends on how this copy was installed (channel), and only get.sh's copy
updates itself: it downloads the exact tag it told the user about, unpacks and checks it, and runs
the new copy's own start-up check, all while Natter still runs (Stager). Then it starts the updater
(updater.py, a script of its own that outlives Natter) and quits. The updater swaps the folders,
runs the new install.sh and starts Natter again, putting the old copy back if any of that fails.
The next start reads what happened from result.json (Updates.started).

Never checked: a lab instance (unless NATTER_UPDATE_FEED gives it a feed of its own), a build a
packager turned checks off in (the file update-check, meson's -Dupdate_check=false), and any run
with NATTER_UPDATE_CHECK=0. Otherwise checks run only from the minute timer (Updates.tick), once
start-up has been ready a while, or when the user asks (Check Now, Update). With Check for Updates
off, the timer doesn't check, and only Preferences tells of a release found before.

The app's side (app.py) calls, from start to quit:
  Updates(app, changed)      in _build, never for a search-only service start
  started()                  once start-up is ready (_now_ready), and at the end of a _build that comes later
  tick()                     every minute (_minute)
  banner(), menu_item()      the banner's and the tray's update, if any
  status(), check_now()      Preferences' status row and its Check Now
  prepare(then), hold(), launch(hidden, then)   Natter.update_now's steps, between its guards
  stay()                     the updater has its plan, but Natter stays after all (a call came in)
  stop()                     in quit_natter
dialog_for(app) is app.update's dialog; open_notes() is app.update-notes. Updates enables both
actions itself, and calls `changed()` whenever what the banner, the tray or Preferences show changes.
"""

from __future__ import annotations

import errno
import gzip
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import gi

gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup

from . import APP_ID, DEFAULT_APP_ID, NAME, VERSION
from . import updater
from .instance import lock_path
from .memguard import in_flatpak
from .settings import CACHE, private_folder, write_private
from .system import give_system_bus, in_sandbox, open_uri, program_environ
from .updater import launcher_dir, read_result, write_result

REPO = "https://github.com/Officialbabs/natter"
LATEST_API = "https://api.github.com/repos/Officialbabs/natter/releases/latest"
LATEST_PAGE = REPO + "/releases/latest"
RELEASES = REPO + "/releases"
TAG_PAGE = REPO + "/releases/tag/"
GET_COMMAND = "curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh | sh"
GET_FLATPAK_COMMAND = GET_COMMAND + " -s -- --flatpak"
# GitHub refuses requests without a User-Agent. A constant one: it says nothing the address doesn't.
USER_AGENT = "natter-den"
ARCHIVE_HOSTS = ("github.com", "codeload.github.com")  # where a release's archive may come from, redirects too
REPO_PATH = urlsplit(REPO).path + "/"  # and the repository it must be in there

FIRST_CHECK_DELAY = 120  # seconds after start-up is ready: out of WhatsApp's own load
CHECK_EVERY = 24 * 3600
SKEW = 3600  # a check time stored further ahead than this is the clock's mistake: checked again
RETRY_AFTER = 3600  # after a check GitHub didn't answer, doubled for each more in a row up to a day (in memory only)
CHECK_NOW_GAP = 60  # Check Now, at most once in this many seconds
FRESH_ENOUGH = 600  # Update checks again first when the last answer is older
LOAD_HOLD = 600  # seconds a page loading or syncing holds checks and Update off at most: one never gets ready
CHECK_DEADLINE = 30  # seconds for a whole check, both requests
DOWNLOAD_DEADLINE = 120
RUNS_HERE_WAIT = 60  # seconds for the new copy's start-up check
HELPER_WAIT = 15  # seconds for the updater to start and take the plan
CLEANUP_DELAY = 60  # seconds after an update is ready before its stage (with the old copy) goes
API_BYTES = 1 << 20
ARCHIVE_BYTES = 16 << 20  # a release is about 1 MB
UNPACKED_BYTES = 64 << 20
MEMBERS = 2000
REDIRECTS = 3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the folder this Natter Den runs from
BUILD_FILE = HERE / "update-check"  # "off": a packager's build (meson -Dupdate_check=false)
FOLDER = CACHE / "update"  # updater.py, plan.json, result.json, update.log
# The failures that happen before Natter quits, which change nothing, and those worth trying again.
BEFORE_QUIT = ("check", "download", "archive", "space", "cant-run", "helper")
TRANSIENT = ("check", "download", "busy", "other")
# Variables Natter's own start gave it that the Natter the updater starts mustn't take for its
# own: the launcher sets the desktop file's again, and systemd's are for the unit Natter ran in.
NOT_PASSED_ON = ("GIO_LAUNCHED_DESKTOP_FILE", "GIO_LAUNCHED_DESKTOP_FILE_PID", "DESKTOP_STARTUP_ID",
                 "XDG_ACTIVATION_TOKEN", "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "MANAGERPID",
                 "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC",
                 "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE", "TRIGGER_UNIT", "TRIGGER_PATH",
                 "TRIGGER_TIMER_REALTIME_USEC", "TRIGGER_TIMER_MONOTONIC_USEC", "CREDENTIALS_DIRECTORY",
                 "RUNTIME_DIRECTORY", "STATE_DIRECTORY", "CACHE_DIRECTORY", "LOGS_DIRECTORY",
                 "CONFIGURATION_DIRECTORY")
# install.sh's CHECK, as it is there (tests/test_update.py keeps them the same): the start-up check
# of the copy in argv[1], run before that copy is installed.
CHECK = f"""import sys
if sys.version_info < (3, 9):
    sys.exit("{NAME} needs Python 3.9 or newer, and this is Python %d.%d: install the {NAME} Flatpak instead (see README.md)." % sys.version_info[:2])
sys.path.insert(0, sys.argv[1]); from natter.__main__ import check; sys.exit(check())"""
CHANNEL_WORDS = {"get": "get.sh copy, updates in place", "checkout": "git checkout", "other": "unpacked copy",
                 "flatpak": "Flatpak", "package": "system package", "off": "packaged with checks off",
                 "lab": "lab instance"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # as the rest is English


def say(line: str) -> None:
    print(f"natter: updates: {line}", flush=True)


# ---- Versions and tags ------------------------------------------------------------------------

def release_tag(tag) -> bool:
    """Whether `tag` is a release to offer: v and two to four numbers ("v0.2.0"). Stricter than
    get.sh's sane_tag, which accepts every one of these: "v1.0-rc1", "v01.2" and "v" aren't."""
    return isinstance(tag, str) and updater.TAG.fullmatch(tag) is not None


def version_tuple(version) -> Optional[tuple[int, ...]]:
    """A version as four numbers to compare ("0.1.10" → (0, 1, 10, 0)); None when it isn't one
    (a development build's)."""
    if not release_tag("v" + version if isinstance(version, str) else None):
        return None
    parts = [int(part) for part in version.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def newer(tag, version: str = VERSION) -> bool:
    """Whether release `tag` is newer than `version`, number by number (0.1.10 > 0.1.9). Nothing
    is newer than a version that isn't one."""
    found = version_tuple(tag[1:]) if release_tag(tag) else None
    running = version_tuple(version)
    return found is not None and running is not None and found > running


def version_of(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def release_url(tag: str) -> str:
    return TAG_PAGE + tag


def archive_url(tag: str) -> str:
    return f"{REPO}/archive/refs/tags/{tag}.tar.gz"


def parse_latest(data: bytes) -> Optional[str]:
    """The tag in the API's answer about the latest release: "" when there's none to offer (a
    draft, a pre-release, or a tag that isn't a version), None when the answer isn't one at all
    (too big, not JSON, not an object, no tag_name), and the Releases page is asked instead.
    Nothing else is read: every address is made from REPO and the tag, never taken from here."""
    if len(data) > API_BYTES:
        return None
    try:
        found = json.loads(bytes(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):  # RecursionError: nested beyond Python's stack
        return None
    if not isinstance(found, dict) or not isinstance(found.get("tag_name"), str):
        return None
    if found.get("draft", False) is not False or found.get("prerelease", False) is not False:
        return ""
    return found["tag_name"] if release_tag(found["tag_name"]) else ""


def tag_from_location(location: str) -> Optional[str]:
    """Where the Releases page's latest link leads (its redirect's Location): the tag of the
    release page it names, "" for the Releases page itself (no release yet) or a tag that isn't a
    version, None for anywhere else (get.sh's rule)."""
    location = (location or "").strip()
    if location == RELEASES:
        return ""
    if not location.startswith(TAG_PAGE):
        return None
    tag = location[len(TAG_PAGE):]
    if not tag or any(c in tag for c in "/?#") or any(c.isspace() for c in tag):
        return None
    return tag if release_tag(tag) else ""


def archive_redirect(location: str, url: str) -> Optional[str]:
    """Where a redirect from `url` to `location` leads, if a release's archive may come from
    there: GitHub or its codeload, over https, on the usual port, with no user in it, and this
    repository's (its names in any case, as GitHub takes them)."""
    target = urljoin(url, (location or "").strip())
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname not in ARCHIVE_HOSTS or port not in (None, 443) \
            or parts.username or parts.password or not parts.path.lower().startswith(REPO_PATH.lower()):
        return None
    return target


# ---- How this copy was installed --------------------------------------------------------------

def channel(root: Path, home: Path, launcher: Optional[str], app_id: str = APP_ID, flatpak: bool = False,
            sandboxed: bool = False, writable: bool = True, off: bool = False) -> str:
    """How this copy of Natter, in `root`, updates, by the first rule that fits:
    "lab"       another app id (NATTER_APP_ID): never updates
    "off"       a packager's build turned checks off: its packages update it
    "flatpak"   the Flatpak get.sh builds: told how to build the new one
    "package"   Snap, meson's layout (<prefix>/share/natter), or a folder it can't write: its packages
    "get"       get.sh's copy (~/.local/lib/natter/source, whole, no .git) that the `natter`
                command (`launcher`, its text) starts: updates in place
    "checkout"  a git checkout: told to pull
    "other"     anything else (an unpacked copy, get.sh's folder with the command elsewhere): told get.sh's line
    """
    if app_id != DEFAULT_APP_ID:
        return "lab"
    if off:
        return "off"
    if flatpak:
        return "flatpak"
    git = (root / ".git").exists()
    if sandboxed or not writable or (root.name == "natter" and root.parent.name == "share" and not git):
        return "package"
    named = launcher_dir(launcher or "")
    if root == (home / ".local" / "lib" / "natter" / "source").resolve() and not git \
            and (root / "install.sh").is_file() and (root / "bin" / "natter").is_file() \
            and named is not None and Path(named).resolve() == root:
        return "get"
    return "checkout" if git else "other"


def build_off(path: Path = BUILD_FILE) -> bool:
    try:
        return path.read_text().strip() == "off"
    except (OSError, ValueError):
        return False


def checks_off(environ=None) -> bool:
    """NATTER_UPDATE_CHECK=0: no checks at all (packagers, CI), whatever the channel."""
    return (os.environ if environ is None else environ).get("NATTER_UPDATE_CHECK", "").strip() == "0"


def read_launcher(path: Path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as text:
            return text.read(256 * 1024)
    except OSError:
        return None


def launcher_python(text: Optional[str]) -> str:
    """The Python the `natter` command runs with (its #! line, as install.sh writes it), else this one."""
    first = (text or "").split("\n", 1)[0]
    if first.startswith("#!"):
        words = first[2:].split()
        if words and os.path.isabs(words[0]) and os.access(words[0], os.X_OK):
            return words[0]
    return sys.executable


def helper_environ(environ=None) -> dict[str, str]:
    """Natter's environment for the updater to pass on (to install.sh and the new Natter): as
    for any program Natter starts (system.program_environ), less what was this start's own."""
    passed = program_environ(environ)
    for name in NOT_PASSED_ON:
        passed.pop(name, None)
    return passed


# ---- When to check, and what to say -----------------------------------------------------------

def due(now: float, checked_at, since_ready: Optional[float], since_failed: Optional[float], *,
        allowed: bool = True, switch: bool = True, online: bool = True, calling: bool = False,
        loading: bool = False, linking: bool = False, checking: bool = False, retry: float = RETRY_AFTER) -> bool:
    """Whether the daily check is due (Updates.tick). Never when the channel has none (`allowed`),
    the switch is off, offline, in a call, while a page loads or syncs, while the user links an
    account, or with a check already under way. Then FIRST_CHECK_DELAY after start-up was ready,
    and CHECK_EVERY after the last answer, or at once when that's more than SKEW in the future (a
    clock that was wrong). A check GitHub didn't answer is tried again `retry` later."""
    if not allowed or not switch or not online or calling or loading or linking or checking:
        return False
    if since_ready is None or since_ready < FIRST_CHECK_DELAY:
        return False
    if since_failed is not None and since_failed < retry:
        return False
    checked = checked_at if isinstance(checked_at, (int, float)) and not isinstance(checked_at, bool) else 0
    return now - checked >= CHECK_EVERY or checked > now + SKEW


def loading(accounts, now: float) -> bool:
    """Whether a page is loading or syncing: one not ready that isn't showing its link screen, offline or
    stopped (the sync after a QR scan, a reload), loaded (Account.loaded_at, time.monotonic's) less
    than LOAD_HOLD before `now`. One that never gets ready (WhatsApp changed its page, or Natter
    stopped reloading it) would otherwise hold checks and Update off for good, when a new
    release is what's needed."""
    return any(not a.ready and not a.needs_link and a.failed is None and not a.crashing
               and now - a.loaded_at < LOAD_HOLD for a in accounts)


def in_a_call(app) -> bool:
    """Whether a call is on: in an account's page (app.calling, from when it rings), or in the
    call window, ringing or connected."""
    calls = app.calls
    return bool(app.calling or (calls is not None and (calls.in_call or calls.ringing)))


def linking(window) -> bool:
    """Whether the user is linking an account: the window shows one that needs linking. A hidden
    or forgotten link screen doesn't count, or it would hold checks off for good."""
    account = getattr(window, "account", None) if window is not None else None
    return bool(account is not None and account.needs_link and window.get_visible())


def notice(channel_name: str, tag: str) -> tuple[str, str, str]:
    """The notification about a new release: (title, body, button)."""
    title = f"{NAME} {version_of(tag)} is available"
    if channel_name == "get":
        return title, f"{NAME} can update itself, usually within a minute.", "Update"
    if channel_name == "package":
        return title, "Update it the way you installed it, with your system's package manager.", "Details"
    return title, "Choose Details to see how to update it.", "Details"


def banner(state: str, tag: str, channel_name: str) -> Optional[tuple[str, str, str, bool]]:
    """The banner for an update in `state` ("available", "preparing", "failed"): (title, button,
    action, urgent). Urgent ones come before media that can't play (app.banner_for)."""
    version = version_of(tag)
    if state == "available":
        return f"{NAME} {version} is available", "Update" if channel_name == "get" else "Details", "app.update", False
    if state == "preparing":
        return f"Downloading {NAME} {version}…", "", "", True
    if state == "failed":
        return f"Couldn't update to {NAME} {version}", "Details", "app.update", True
    return None


def sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def why_words(why: str, tag: str, detail: str = "", new: str = "") -> str:
    """Why an update failed, in a sentence (the failure dialog). `new` is the version it was to be."""
    version = version_of(tag)
    if why == "cant-run":
        return f"{NAME} {version} can't run on this system as it is" + (f": {sentence(detail)}" if detail else ".")
    if why == "install":
        return f"Installing it stopped with an error ({detail.strip().rstrip('.')})." if detail.strip() \
            else "Installing it stopped with an error."
    if why == "start":  # its detail is how it ended (updater.ending)
        return f"{NAME} {new or version} didn't start ({detail.strip().rstrip('.')})." if detail.strip() \
            else f"{NAME} {new or version} didn't start."
    return {
        "check": "GitHub didn't answer. Check your connection and try again.",
        "download": "The download didn't finish. Check your connection and try again.",
        "archive": f"The download wasn't a complete copy of {NAME} {version}.",
        "space": "There isn't enough disk space.",
        "helper": "The updater couldn't start.",
        "busy": f"{NAME} didn't close in time, so nothing was changed.",
        "other": f"Another {NAME} started meanwhile, so nothing was changed.",
        "launcher": f"The natter command now starts another copy of {NAME}, so nothing was changed.",
        "timeout": "Installing it took too long.",
        "unfinished": "The update was interrupted before it finished.",
    }.get(why, "Something went wrong.")


def checked_words(at: float, now: float) -> str:
    """When the last check was answered: "today at 14:05", or "on 2 Nov" (in English, as the rest
    of the line, whatever LC_TIME says)."""
    then, today = time.localtime(at), time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return f"today at {then.tm_hour:02}:{then.tm_min:02}"
    return f"on {then.tm_mday} {MONTHS[then.tm_mon - 1]}"


def first_problem(text: str) -> str:
    """What a start-up check said stops it, in a line: its first "- " item (__main__.explain's),
    else its first line."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    items = [line[1:].strip() for line in lines if line.startswith("- ")]
    return ((items or lines or [""])[0])[:updater.DETAIL]


def home_path(path) -> str:
    return updater.home_path(path, str(Path.home()))


# ---- Talking to GitHub ------------------------------------------------------------------------
# A transport makes one request, never following a redirect: send(method, url, headers, limit,
# cancellable, done), and done(status, location, body, error) once. Status 0 (with `error`) when
# there was no answer, or it was cut short or bigger than `limit`; else the answer's status, its
# Location, and for a 200 to a GET, its body. Tests give their own; Natter's is SoupTransport.

class SoupTransport:
    """libsoup 3, as notify.py fetches pictures: a session of its own with no cookie jar, no
    Accept-Language and no User-Agent of its own, made at the first request. Each request sets
    its User-Agent itself: the session's would go to a proxy too, outside TLS."""

    def __init__(self) -> None:
        self.session: Optional[Soup.Session] = None

    def __call__(self, method: str, url: str, headers: dict[str, str], limit: int, cancellable: Gio.Cancellable,
                 done: Callable[[int, str, bytes, str], None]) -> None:
        if checks_off():  # NATTER_UPDATE_CHECK=0 holds whatever asked
            done(0, "", b"", "checks are off (NATTER_UPDATE_CHECK=0)")
            return
        if self.session is None:
            self.session = Soup.Session(timeout=15, idle_timeout=30, max_conns=2)
        try:
            message = Soup.Message.new_from_uri(method, GLib.Uri.parse(url, GLib.UriFlags.NONE))
        except GLib.Error as error:
            done(0, "", b"", error.message)
            return
        message.add_flags(Soup.MessageFlags.NO_REDIRECT)
        for name, value in headers.items():
            message.get_request_headers().replace(name, value)
        chunks: list[bytes] = []
        size = 0

        def close(stream: Gio.InputStream) -> None:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)

        def read(stream: Gio.InputStream, result) -> None:
            nonlocal size
            try:
                data = stream.read_bytes_finish(result)
            except GLib.Error as error:
                done(0, "", b"", error.message)
                return
            if data.get_size() == 0:
                close(stream)
                done(200, "", b"".join(chunks), "")
                return
            size += data.get_size()
            if size > limit:
                close(stream)
                done(0, "", b"", "the answer was too big")
                return
            chunks.append(data.get_data())
            stream.read_bytes_async(64 * 1024, GLib.PRIORITY_DEFAULT, cancellable, read)

        def opened(session: Soup.Session, result) -> None:
            try:
                stream = session.send_finish(result)
            except GLib.Error as error:
                done(0, "", b"", error.message)
                return
            status = message.get_status()
            location = message.get_response_headers().get_one("Location") or ""
            if method != "GET" or status != 200:
                close(stream)
                done(status, location, b"", "")
                return
            if message.get_response_headers().get_content_length() > limit:
                close(stream)
                done(0, "", b"", "the answer was too big")
                return
            stream.read_bytes_async(64 * 1024, GLib.PRIORITY_DEFAULT, cancellable, read)

        self.session.send_async(message, GLib.PRIORITY_DEFAULT, cancellable, opened)


def feed_transport(feed: str):
    """A lab's feed (NATTER_UPDATE_FEED) in place of GitHub's API: a file:// path, or an
    http://127.0.0.1:<port>/ address, through `transport`. None when it's neither."""
    if feed.startswith("file://"):
        path = feed[len("file://"):]

        def read(method, url, headers, limit, cancellable, done) -> None:
            try:
                with open(path, "rb") as answer:
                    body = answer.read(limit + 1)
            except OSError as error:
                done(0, "", b"", error.strerror or str(error))
                return
            if len(body) > limit:
                done(0, "", b"", "the answer was too big")
            else:
                done(200, "", body, "")
        return read
    if re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/[^\s]*", feed):
        return feed
    return None


class Checker:
    """One check for the latest release: GitHub's API (LATEST_API), and when it doesn't answer as it
    should (403 or 429 when rate-limited, 5xx, an error), the Releases page's latest link, which
    leads to the release's page (get.sh's way). done(tag) once: the latest release's tag, "" when
    GitHub answered with none to offer (a 404: no release yet), None when it didn't answer. A lab's
    feed is asked alone. Everything ends after `deadline` seconds."""

    def __init__(self, transport, feed: str = "", deadline: float = CHECK_DEADLINE) -> None:
        self.transport = transport
        self.feed = feed
        self.deadline = deadline
        self.cancellable = Gio.Cancellable()
        self.done: Optional[Callable[[Optional[str]], None]] = None
        self.timer = 0

    def start(self, done: Callable[[Optional[str]], None]) -> None:
        self.done = done
        self.timer = GLib.timeout_add(int(self.deadline * 1000), self._too_late)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if self.feed:
            source = feed_transport(self.feed)
            if source is None:
                say("NATTER_UPDATE_FEED is neither a file:// path nor an http://127.0.0.1 address")
                self._finish(None)
            elif isinstance(source, str):
                self.transport("GET", source, headers, API_BYTES, self.cancellable, self._fed)
            else:
                source("GET", self.feed, headers, API_BYTES, self.cancellable, self._fed)
            return
        self.transport("GET", LATEST_API, headers, API_BYTES, self.cancellable, self._api)

    def cancel(self) -> None:
        self.cancellable.cancel()
        self.done = None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _too_late(self) -> bool:
        self.timer = 0
        say(f"GitHub didn't answer within {self.deadline:g} s")
        self.cancellable.cancel()
        self._finish(None)
        return GLib.SOURCE_REMOVE

    def _finish(self, tag: Optional[str]) -> None:
        done, self.done = self.done, None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if done is not None:
            done(tag)

    def _fed(self, status: int, _location: str, body: bytes, error: str) -> None:
        tag = parse_latest(body) if status == 200 else None
        if tag is None:
            say(f"the feed answered {status or error}")
        self._finish(tag)

    def _api(self, status: int, _location: str, body: bytes, error: str) -> None:
        if self.done is None:
            return
        if status == 200:
            tag = parse_latest(body)
            if tag is not None:
                if not tag:
                    say("the latest release isn't one to offer (a draft, a pre-release, or not a version)")
                self._finish(tag)
                return
            say("GitHub's API answered with something else; asking the Releases page")
        elif status == 404:
            self._finish("")  # no release yet
            return
        else:
            say(f"GitHub's API answered {status or error}; asking the Releases page")
        self.transport("HEAD", LATEST_PAGE, {"User-Agent": USER_AGENT}, 0, self.cancellable, self._page)

    def _page(self, status: int, location: str, _body: bytes, error: str) -> None:
        if self.done is None:
            return
        tag = tag_from_location(location) if status in (301, 302, 303, 307, 308) else None
        if tag is None:
            say(f"the Releases page answered {status or error}")
        elif not tag and location.strip() != RELEASES:
            say("the latest release's tag isn't a version")
        self._finish(tag)


# ---- Getting a release ready ------------------------------------------------------------------

class Failed(Exception):
    """A step that failed: `why` is why_words's key, `detail` what it adds."""

    def __init__(self, why: str, detail: str = "") -> None:
        super().__init__(why)
        self.why = why
        self.detail = detail


def check_member(member: tarfile.TarInfo) -> tuple[str, str]:
    """(its top folder, its path under that) for an archive member that may be unpacked: a
    regular file or a folder, by a relative name with no "..". Raises ValueError for anything
    else: links, devices, fifos, absolute names, names that climb out."""
    if not (member.isreg() or member.isdir()):
        raise ValueError(f"{member.name!r} isn't a file or a folder")
    name = member.name
    if name.startswith("/") or "\\" in name or "\0" in name:
        raise ValueError(f"{name!r} isn't a plain relative name")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"{name!r} isn't a plain relative name")
    return parts[0], "/".join(parts[1:])


class Capped:
    """A stream's reads, refused once more than `limit` bytes would have been read: tarfile reads a
    member's header whole (a long name, a pax record), before check_member can see it."""

    def __init__(self, stream, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or self.count + size > self.limit:
            size = self.limit - self.count + 1
        data = self.stream.read(size)
        self.count += len(data)
        if self.count > self.limit:
            raise Failed("archive", f"more than {self.limit >> 20} MiB unpacked")
        return data


def unpack(archive: Path, folder: Path, limit: int = UNPACKED_BYTES, members: int = MEMBERS) -> None:
    """Unpack a release's archive into `folder` (made new), its one top folder taken off, as
    GitHub makes them. Each member is checked (check_member) and written here, never by tarfile:
    only files and folders, files with no more than their owner's execute bit. It's read as a
    stream, capped: the files' sizes and the headers', a kilobyte or two a member, together.
    Raises Failed: "archive" for an archive that isn't one or breaks these rules, "space" when the
    disk is full."""
    folder.mkdir(mode=0o755)
    top, count, total = None, 0, 0
    try:
        with gzip.open(archive, "rb") as unzipped, \
                tarfile.open(fileobj=Capped(unzipped, limit + members * 2048), mode="r|") as tar:
            for member in tar:
                count += 1
                if count > members:
                    raise Failed("archive", f"more than {members} files")
                try:
                    first, path = check_member(member)
                except ValueError as refused:
                    raise Failed("archive", str(refused))
                if top is None:
                    top = first
                elif first != top:
                    raise Failed("archive", "more than one top folder")
                if not path:
                    continue
                target = folder / path
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                total += member.size
                if total > limit:
                    raise Failed("archive", f"more than {limit >> 20} MiB unpacked")
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                data = tar.extractfile(member)
                mode = 0o755 if member.mode & 0o100 else 0o644
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(data, out, 64 * 1024)
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as error:
        raise Failed("archive", f"it isn't a whole archive ({type(error).__name__})")
    except OSError as error:
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            raise Failed("space")
        raise Failed("archive", error.strerror or str(error))
    if top is None:
        raise Failed("archive", "it's empty")


def verify(folder: Path, tag: str) -> None:
    """Raise Failed("archive") unless `folder` holds a whole copy of Natter at `tag`'s version
    (VERSION read from its __init__.py, never imported)."""
    for name in ("install.sh", "bin/natter", "natter/__init__.py", "natter/__main__.py"):
        if not (folder / name).is_file():
            raise Failed("archive", f"it has no {name}")
    found = updater.version_in(folder)
    if found != version_of(tag):
        raise Failed("archive", f"its version is {found or 'missing'}")


def runs_here(python: str, folder: Path, done: Callable[[Optional[str]], None], wait: float = RUNS_HERE_WAIT) -> None:
    """Run the start-up check of the copy in `folder` (its own __main__.check, as install.sh
    does) with `python`, as a program Natter starts. done(None) when it passes, else done(what
    it said stops it, first_problem's line)."""
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    give_system_bus(launcher)
    launcher.set_cwd(str(Path.home()))
    try:
        process = launcher.spawnv([python, "-I", "-c", CHECK, str(folder)])
    except GLib.Error as error:
        done(f"couldn't run {python}: {error.message}")
        return
    cancellable = Gio.Cancellable()
    late = []

    def too_late() -> bool:
        late.append(True)
        cancellable.cancel()
        process.force_exit()
        return GLib.SOURCE_REMOVE

    timer = GLib.timeout_add(int(wait * 1000), too_late)

    def finished(process: Gio.Subprocess, result) -> None:
        if not late:
            GLib.source_remove(timer)
        try:
            _ok, out, err = process.communicate_finish(result)
        except GLib.Error:
            done(f"its start-up check didn't finish within {wait:g} s")
            return
        if process.get_if_exited() and process.get_exit_status() == 0:
            done(None)
            return
        said = (err.get_data() if err is not None else b"") or (out.get_data() if out is not None else b"")
        done(first_problem(said.decode(errors="replace")) or "its start-up check failed")

    process.communicate_async(None, cancellable, finished)


class Stager:
    """Gets one release ready to install, beside the installed copy (<stages>/<tag>/source),
    while Natter runs: downloads the tag's archive (redirects followed by hand, only to GitHub),
    unpacks it on a thread, checks it's a whole copy of that version, and runs its own start-up
    check. done(None) once it's ready, else done((why, detail)). Nothing outside the stage changes."""

    def __init__(self, transport, stages: Path, python: str, deadline: float = DOWNLOAD_DEADLINE,
                 runs: Callable = runs_here) -> None:
        self.transport = transport
        self.stages = stages
        self.python = python
        self.deadline = deadline
        self.runs = runs
        self.cancellable = Gio.Cancellable()
        self.done: Optional[Callable[[Optional[tuple[str, str]]], None]] = None
        self.timer = 0
        self.tag = ""

    @property
    def stage(self) -> Path:
        return self.stages / self.tag

    def start(self, tag: str, done: Callable[[Optional[tuple[str, str]]], None]) -> None:
        self.tag, self.done = tag, done
        try:
            # Whatever an earlier update left (its stage, the copy from before it) goes now.
            shutil.rmtree(self.stages, ignore_errors=True)
            private_folder(self.stages)
            self.stage.mkdir(mode=0o700)
        except OSError as error:
            self._finish("space" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "download",
                         error.strerror or str(error))
            return
        say(f"downloading {tag}")
        self.timer = GLib.timeout_add(int(self.deadline * 1000), self._too_late)
        self._get(archive_url(tag), 0)

    def cancel(self) -> None:
        self.cancellable.cancel()
        self.done = None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _too_late(self) -> bool:
        self.timer = 0
        self.cancellable.cancel()
        self._finish("download", f"it took over {self.deadline:g} s")
        return GLib.SOURCE_REMOVE

    def _finish(self, why: Optional[str], detail: str = "") -> None:
        done, self.done = self.done, None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if why is not None:
            say(f"couldn't get {self.tag} ready: {why}{f' ({detail})' if detail else ''}")
        if done is not None:
            done(None if why is None else (why, detail))

    def _get(self, url: str, hops: int) -> None:
        def got(status: int, location: str, body: bytes, error: str) -> None:
            if self.done is None:
                return
            if status in (301, 302, 303, 307, 308):
                target = archive_redirect(location, url)
                if target is None or hops >= REDIRECTS:
                    self._finish("download", "redirected away from GitHub" if target is None else "too many redirects")
                else:
                    self._get(target, hops + 1)
            elif status == 200:
                self._save(body)
            elif status == 404:
                self._finish("archive", "the release has no archive")
            else:
                self._finish("download", f"HTTP {status}" if status else error)

        self.transport("GET", url, {"User-Agent": USER_AGENT}, ARCHIVE_BYTES, self.cancellable, got)

    def _save(self, body: bytes) -> None:
        archive = self.stage / "source.tar.gz"
        try:
            with open(os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600),
                      "wb") as out:
                out.write(body)
        except OSError as error:
            self._finish("space" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "download",
                         error.strerror or str(error))
            return
        if self.timer:  # the download is done: unpacking and the check have their own limits
            GLib.source_remove(self.timer)
            self.timer = 0
        threading.Thread(target=self._unpack, args=(archive,), name="natter-update-unpack", daemon=True).start()

    def _unpack(self, archive: Path) -> None:
        try:
            unpack(archive, self.stage / "source")
            archive.unlink()
            verify(self.stage / "source", self.tag)
            failed = None
        except Failed as error:
            failed = error
        except OSError as error:
            failed = Failed("space" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "archive",
                            error.strerror or str(error))
        GLib.idle_add(self._unpacked, failed)

    def _unpacked(self, failed: Optional[Failed]) -> bool:
        if self.done is None:
            return GLib.SOURCE_REMOVE
        if failed is not None:
            self._finish(failed.why, failed.detail)
        else:
            self.runs(self.python, self.stage / "source", self._checked)
        return GLib.SOURCE_REMOVE

    def _checked(self, problem: Optional[str]) -> None:
        if self.done is None:
            return
        self._finish(None if problem is None else "cant-run", problem or "")


# ---- Starting the updater ---------------------------------------------------------------------

def helper_command(helper: Path, plan: Path, home: Path, pid: Optional[int] = None, now: Optional[float] = None,
                   python: str = sys.executable) -> list[str]:
    """The updater as a transient service of the user's systemd: no child of Natter, so neither
    Natter's scope (its memory limit) nor the end of a launcher's unit ends it; no terminal, so
    install.sh never asks; the user manager's limits, not a shell's. Not named app-…: own_unit
    would take it for Natter's, and Natter would stay in it. No limit on the unit as a whole: each
    of the updater's steps has its own, and a Natter it started that's still in the unit would end
    with it."""
    pid = os.getpid() if pid is None else pid
    now = time.time() if now is None else now
    return ["systemd-run", "--user", "--quiet", "--collect", f"--unit=natter-update-{pid}-{int(now)}",
            "--service-type=exec", "--property=StandardInput=null", f"--working-directory={home}",
            "--", python, "-I", str(helper), str(plan)]


def start_helper(helper: Path, plan: Path, done: Callable[[bool], None], home: Optional[Path] = None,
                 wait: float = HELPER_WAIT, python: str = sys.executable) -> None:
    """Start the updater on `plan`: through systemd-run (helper_command), or when there's none or
    it fails, as a process of Natter's (as app.restart's is), which takes a session of its own.
    done(True) once the updater has taken the plan (it moves it away), and only then may Natter
    quit; done(False) if no updater has within `wait` seconds, and the plan is withdrawn."""
    home = Path.home() if home is None else home
    deadline = time.monotonic() + wait
    answered = []

    def finish(ok: bool) -> None:
        if not answered:
            answered.append(ok)
            done(ok)

    def taken() -> bool:
        if not plan.exists():
            finish(True)
            return GLib.SOURCE_REMOVE
        if time.monotonic() < deadline:
            return GLib.SOURCE_CONTINUE
        try:
            withdrawn = plan.with_name(plan.name + ".withdrawn")
            os.rename(plan, withdrawn)  # taken back, unless an updater takes it this very moment
            withdrawn.unlink()
        except FileNotFoundError:
            finish(True)
            return GLib.SOURCE_REMOVE
        except OSError:
            pass
        say("the updater didn't start")
        finish(False)
        return GLib.SOURCE_REMOVE

    def watch() -> None:
        GLib.timeout_add(100, taken)

    def fallback() -> None:
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)  # its input from /dev/null
        launcher.set_cwd(str(home))
        give_system_bus(launcher)
        try:
            launcher.spawnv([python, "-I", str(helper), str(plan)])
        except GLib.Error as error:
            say(f"couldn't start the updater ({error.message})")
            finish(False)
            return
        watch()

    if shutil.which("systemd-run") is None:
        fallback()
        return
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
    give_system_bus(launcher)
    try:
        process = launcher.spawnv(helper_command(helper, plan, home, python=python))
    except GLib.Error:
        fallback()
        return
    cancellable = Gio.Cancellable()

    def too_late() -> bool:
        cancellable.cancel()
        process.force_exit()
        return GLib.SOURCE_REMOVE

    timer = GLib.timeout_add(int(wait * 1000), too_late)

    def ran(process: Gio.Subprocess, result) -> None:
        try:
            process.wait_check_finish(result)
            ok = True
        except GLib.Error as error:
            say(f"systemd-run didn't start the updater ({error.message}); starting it as before")
            ok = False
        if not cancellable.is_cancelled():
            GLib.source_remove(timer)
        if ok:
            watch()
            return
        # A slow systemd-run may still have started one: the plan goes to whichever takes it first.
        nonlocal deadline
        deadline = max(deadline, time.monotonic() + 5)
        fallback()

    process.wait_check_async(cancellable, ran)


# ---- Natter's side ----------------------------------------------------------------------------

class Updates:
    """Natter's side of updates, made with the window (see the module's docstring for the calls).

    What it shows: the newest release found (settings update_latest, once newer than VERSION) is
    "known", and "quiet" once Skip This Version was chosen for it; the banner, the tray and a
    notification tell of it while Check for Updates is on (`offered`), Preferences whatever the
    switch. In memory: whether a check is under way, when the last one failed and how many have in
    a row, a failure to tell (`failure`), an update being prepared or started (`working`), what's
    still to be told of one (`untold`), and the tag whose banner Later hid for this session."""

    def __init__(self, app, changed: Optional[Callable[[], None]] = None, *, transport=None,
                 root: Path = ROOT, home: Optional[Path] = None, folder: Optional[Path] = None,
                 environ=None, app_id: str = APP_ID, channel_name: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.app = app
        self.settings = app.settings
        self.on_change = changed
        self.environ = os.environ if environ is None else environ
        self.root = root
        self.home = Path.home() if home is None else home
        self.folder = FOLDER if folder is None else folder
        self.launcher = self.home / ".local" / "bin" / "natter"
        self.stages = root.parent / ".update"
        if channel_name is None:
            channel_name = channel(root, self.home, read_launcher(self.launcher), app_id, flatpak=in_flatpak(),
                                   sandboxed=in_sandbox(), writable=os.access(root.parent, os.W_OK), off=build_off())
        self.channel = channel_name
        # NATTER_UPDATE_CHECK=0 holds every check, a lab's feed's too, whatever the channel: the
        # channel stays what it is, for Preferences to say the rest truly.
        self.env_off = checks_off(self.environ)
        # A lab checks only against a feed of its own.
        self.feed = self.environ.get("NATTER_UPDATE_FEED", "") if self.channel == "lab" else ""
        self.transport = SoupTransport() if transport is None else transport
        self.clock = clock
        self.wall = wall
        self.ready_at: Optional[float] = None
        self.failed_at: Optional[float] = None  # the last check GitHub didn't answer
        self.check_failed = False
        self.misses = 0  # checks in a row GitHub didn't answer
        self.asked_at: Optional[float] = None  # Check Now
        self.checker: Optional[Checker] = None
        self.by_timer = False  # the check under way is the daily one: Check for Updates turned off ends it
        self.waiting: list[Callable[[Optional[str]], None]] = []  # what waits on the check under way
        self.stager: Optional[Stager] = None
        self.state = ""  # "preparing" or "launching" while an update is under way
        self.target = ""  # the tag being prepared or installed
        self.staged = ""  # the tag staged, checked and ready this session
        self.failure: Optional[dict] = None  # tag, from, to, why, detail, before (nothing changed), kept
        self.untold = ""  # "updated" or "failed": still to be told, once it can be seen (_tell_outcome)
        self.hidden_tag = ""  # Later hid this tag's banner for this session
        self.notes = False  # app.update-notes may open the release page (after the "updated" notice)
        self._enable()

    # ---- What's known ---------------------------------------------------------------------

    @property
    def checks(self) -> bool:
        return not self.env_off and (self.channel not in ("lab", "off") or bool(self.feed))

    @property
    def latest(self) -> str:
        """The newest release found, while it's newer than this one and this copy checks; else ""."""
        tag = self.settings["update_latest"]
        return tag if self.checks and release_tag(tag) and newer(tag) else ""

    @property
    def quiet(self) -> bool:
        return bool(self.latest) and self.latest == self.settings["update_dismissed"]

    @property
    def offered(self) -> str:
        """The release the banner, the tray and a notification tell of: the one known, unless it's
        quiet or Check for Updates is off (Preferences still says it); else ""."""
        tag = self.latest
        return tag if tag and not self.quiet and self.settings["update_check"] else ""

    @property
    def can_update(self) -> bool:
        return self.channel == "get"

    @property
    def working(self) -> bool:
        return self.state in ("preparing", "launching")

    @property
    def retry(self) -> float:
        """How long after a check GitHub didn't answer the timer asks again: RETRY_AFTER, doubled
        for each more in a row, CHECK_EVERY at most. A failure that lasts (the repository moved, a
        firewall) costs two requests a day, not 48."""
        return min(RETRY_AFTER * 2 ** max(0, self.misses - 1), CHECK_EVERY)

    def changed(self) -> None:
        """What the surfaces show may have changed: the actions follow, then the app (`changed`).
        Check for Updates turned off ends the daily check under way (Check Now's and Update's go on)."""
        if self.checker is not None and self.by_timer and not self.waiting and not self.settings["update_check"]:
            self.checker.cancel()
            self.checker = None
            say("Check for Updates was turned off; the check under way stops")
        self._enable()
        if self.on_change is not None:
            self.on_change()

    def _enable(self) -> None:
        """app.update while there's a release or a failure to tell of; app.update-notes once the update
        was told (anything on the bus can call an action)."""
        look = getattr(self.app, "lookup_action", None)
        for name, enabled in (("update", bool(self.latest or self.failure)), ("update-notes", self.notes)):
            action = look(name) if look is not None else None
            if action is not None:
                action.set_enabled(enabled)

    def banner(self) -> Optional[tuple[str, str, str, bool]]:
        """The window's banner for updates, (title, button, action, urgent), else None."""
        if self.working:
            return banner("preparing", self.target, self.channel)
        if self.failure is not None:
            return banner("failed", self.failure["tag"], self.channel)
        tag = self.offered
        if tag and self.hidden_tag != tag:
            return banner("available", tag, self.channel)
        return None

    def menu_item(self) -> Optional[str]:
        """The tray menu item's label (before its separator; it runs app.update), else None."""
        tag = self.offered
        if not tag or self.working or getattr(self.app, "locked", False):
            return None
        version = version_of(tag)
        return f"Update to {NAME} {version}…" if self.channel == "get" else f"{NAME} {version} Is Available…"

    def status(self) -> tuple[str, str]:
        """Preferences' status line under "{NAME} {VERSION}", and its button: "update", "details",
        "check" (Check Now) or "" (none). While checking, Check Now stays (a click on it then does
        nothing), so the focus a keyboard put on it stays too."""
        if self.env_off:
            return "Checks are off (NATTER_UPDATE_CHECK=0)", ""
        tag = self.latest
        if self.checker is not None:
            return "Checking…", "check"
        if tag:
            return f"{version_of(tag)} is available", "" if self.working else (
                "update" if self.channel == "get" else "details")
        if self.check_failed:
            return "Couldn't check: GitHub didn't answer", "check"
        at = self.settings["update_checked_at"]
        if not isinstance(at, (int, float)) or isinstance(at, bool) or at <= 0:
            return "Not checked yet", "check"
        return f"Up to date, checked {checked_words(at, self.wall())}", "check"

    def debug_line(self) -> str:
        """The About dialog's line (app.debug_info): never more than the channel, the switch, a tag and a date."""
        at = self.settings["update_checked_at"]
        checked = time.strftime("%Y-%m-%d", time.localtime(at)) \
            if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 else "never"
        on = self.checks and bool(self.settings["update_check"])
        return (f"Updates: {CHANNEL_WORDS.get(self.channel, self.channel)}, checks {'on' if on else 'off'}, "
                f"newest {self.latest or 'none found'}, checked {checked}")

    def _visible(self) -> bool:
        """Whether the window can be seen: shown, and not behind the lock."""
        window = self.app.window
        return window is not None and window.get_visible() and not getattr(self.app, "locked", False)

    def _toast(self, title: str) -> None:
        """A toast, when the window can be seen (over Preferences while that's open, Window.toast)."""
        if self._visible():
            self.app.window.toast(title)

    # ---- Checking ---------------------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> None:
        """Every minute (app._minute): a found release or an update's outcome not told yet is
        told, and the daily check made when it's due (due)."""
        now = self.wall() if now is None else now
        self._tell_found()
        self._tell_outcome()
        app = self.app
        since_ready = None if self.ready_at is None else self.clock() - self.ready_at
        since_failed = None if self.failed_at is None else self.clock() - self.failed_at
        if due(now, self.settings["update_checked_at"], since_ready, since_failed, allowed=self.checks,
               switch=bool(self.settings["update_check"]), online=self.online(), calling=in_a_call(app),
               loading=loading(app.accounts, self.clock()), linking=linking(app.window),
               checking=self.checker is not None or self.working, retry=self.retry):
            self.check(timer=True)

    def online(self) -> bool:
        return Gio.NetworkMonitor.get_default().get_network_available()

    def check_now(self) -> None:
        """Check Now in Preferences: whatever the switch and the day, once a minute at most. A
        click sooner, or before start-up is ready, says to try again; one while checking, nothing."""
        if not self.checks or self.checker is not None:
            return
        if self.ready_at is None or (self.asked_at is not None and self.clock() - self.asked_at < CHECK_NOW_GAP):
            self._toast("Try again in a minute")
            return
        self.asked_at = self.clock()
        self.check()

    def check(self, then: Optional[Callable[[Optional[str]], None]] = None, timer: bool = False) -> None:
        """Ask GitHub now; `then(tag)` with its answer (Checker's). `timer`: the daily check."""
        if then is not None:
            self.waiting.append(then)
        if self.checker is not None:
            return
        if not self.checks:
            self._answered(None)
            return
        self.checker = Checker(self.transport, self.feed)
        self.by_timer = timer
        self.changed()
        self.checker.start(self._checked)

    def _checked(self, tag: Optional[str]) -> None:
        self.checker = None
        if tag is None:
            self.failed_at = self.clock()
            self.check_failed = True
            self.misses += 1
            hours = int(self.retry // 3600)
            say(f"GitHub didn't answer; trying again in {hours} hour{'s' if hours > 1 else ''}")
        else:
            self.failed_at = None
            self.check_failed = False
            self.misses = 0
            found = tag if tag and newer(tag) else ""
            self.settings["update_checked_at"] = int(self.wall())
            self.settings["update_latest"] = found
            say(f"{found} is out" if found else f"up to date ({tag or 'no release yet'})")
        self.changed()
        self._tell_found()
        self._answered(tag)

    def _answered(self, tag: Optional[str]) -> None:
        waiting, self.waiting = self.waiting, []
        for then in waiting:
            then(tag)

    def _tell_found(self) -> None:
        """A release found and not told yet: a visible window's banner tells it (read out too),
        once it's the banner shown (Natter.banner_shown: paused notifications, say, come first);
        out of sight (hidden, or locked), a notification, unless notifications are paused, when it
        waits for a later minute. One per version, none with Check for Updates off."""
        tag = self.offered
        if not tag or self.settings["update_notified"] == tag:
            return
        app = self.app
        if self._visible():
            if self.hidden_tag == tag or self.working or self.failure is not None:
                self.settings["update_notified"] = tag  # its dialog has been seen, or is being followed
                return
            shown = self.banner()
            if shown is None or getattr(app, "banner_shown", "") != shown[0]:
                return  # another banner is up: told once this one is
            self.settings["update_notified"] = tag
            app.window.say(shown[0])
            return
        if self.settings.notifications_paused():
            return
        title, body, button = notice(self.channel, tag)
        app.tell("update", title, body, (button, "app.update", None))
        self.settings["update_notified"] = tag

    # ---- The dialog's answers ---------------------------------------------------------------

    def dismiss(self) -> None:
        """Skip This Version: quiet until a newer one."""
        if self.latest:
            self.settings["update_dismissed"] = self.latest
        self.changed()

    def later(self) -> None:
        """Later or Close: the banner goes for this session."""
        self.hidden_tag = self.latest
        self.changed()

    def told(self) -> None:
        """The failure dialog was shown: the failure is told (result.json too, an interrupted
        update's as well, so the next start doesn't tell it again), its notification goes, and
        the banner says what's available again."""
        if self.failure is None:
            return
        found = read_result(self.folder / "result.json")
        if found is not None and found["state"] in ("failed", "planned", "swapped"):
            # With its why: a told one without is an update that went well, whose stage goes.
            found.update(state="told", why=found["why"] or self.failure["why"])
            self._write_result(found)
        if self.untold == "failed":
            self.untold = ""
        self.app.withdraw_notification("update-failed")
        self.failure = None
        self.changed()

    def open_notes(self) -> None:
        """app.update-notes: the running version's release page."""
        if self.notes:
            open_uri(release_url("v" + VERSION), getattr(self.app, "window", None))

    # ---- Updating in place (Natter.update_now) ----------------------------------------------

    def prepare(self, then: Callable[[bool], None]) -> None:
        """Get the update ready while Natter runs: check again when the last answer is older than
        FRESH_ENOUGH, then stage the newest of the tag told and the tag found (never an older one,
        and none when GitHub no longer offers the one told). then(True) once it's ready to start
        (the banner says it's downloading meanwhile); then(False) after a failure, which is told,
        or when there's nothing to install now; nothing has changed then."""
        told = self.latest
        if not self.can_update or self.working or not told:
            then(False)
            return
        self.state, self.target, self.failure = "preparing", told, None
        self.changed()
        at = self.settings["update_checked_at"]
        fresh = isinstance(at, (int, float)) and not isinstance(at, bool) and 0 <= self.wall() - at < FRESH_ENOUGH

        def checked(tag: Optional[str]) -> None:
            if self.state != "preparing":
                return
            if tag is None:
                self._failed("check", told)
                then(False)
                return
            found = tag if tag and newer(tag) else ""
            if not found or version_tuple(found[1:]) < version_tuple(told[1:]):
                # Pulled, or made a pre-release, since it was told: nothing the user wasn't told of
                # is installed. A newer one than this that's still out is offered again instead.
                say(f"{told} is no longer offered; nothing changed")
                self.state = ""
                self.changed()
                self._toast(f"{NAME} {version_of(told)} is no longer available")
                then(False)
                return
            if version_tuple(found[1:]) > version_tuple(told[1:]):
                self.target = found
            self._stage(then)

        if fresh:
            self._stage(then)
        else:
            self.check(checked)

    def _stage(self, then: Callable[[bool], None]) -> None:
        tag = self.target
        self.changed()
        if self.staged == tag and (self.stages / tag / "source").is_dir():
            try:
                verify(self.stages / tag / "source", tag)
                then(True)
                return
            except Failed:
                self.staged = ""

        def ready(failed: Optional[tuple[str, str]]) -> None:
            self.stager = None
            if self.state != "preparing":
                return
            if failed is not None:
                self._failed(failed[0], tag, failed[1])
                then(False)
                return
            self.staged = tag
            then(True)

        self.stager = Stager(self.transport, self.stages, launcher_python(read_launcher(self.launcher)))
        self.stager.start(tag, ready)

    def hold(self) -> None:
        """Prepared, but something came up meanwhile (a call): back to available. The stage is
        kept, and the next Update for the same tag uses it."""
        if self.state == "preparing":
            self.state = ""
            self.changed()

    def plan(self, hidden: bool) -> dict:
        """The updater's plan (updater.plan_problem says what it takes)."""
        tag = self.target
        return {"tag": tag, "from": VERSION, "to": version_of(tag), "pid": os.getpid(),
                "ticks": updater.start_ticks(os.getpid()) or 0, "source": str(self.root),
                "stage": str(self.stages / tag), "launcher": str(self.launcher),
                "lock": str(lock_path(environ=self.environ)),
                "result": str(self.folder / "result.json"), "log": str(self.folder / "update.log"),
                "args": ["--background"] if hidden else [], "env": helper_environ(self.environ)}

    def launch(self, hidden: bool, then: Callable[[bool], None]) -> None:
        """Start the updater on what prepare staged, to start Natter again `hidden` or not.
        then(True) once it has taken its plan: Natter must quit now (quit_natter), and the updater
        waits for that, unless Natter stays after all (stay). then(False) when it couldn't start:
        told, and Natter keeps running, the installed copy untouched."""
        if self.state != "preparing" or self.staged != self.target:
            then(False)
            return
        self.state = "launching"
        self.changed()
        tag = self.target
        plan_file = self.folder / "plan.json"
        plan = self.plan(hidden)
        problem = updater.plan_problem(plan, str(plan_file), str(self.home))
        if problem:
            say(f"not updating: {problem}")
            self._failed("helper", tag)
            then(False)
            return
        try:
            private_folder(self.folder)
            try:
                (self.folder / updater.CANCEL).unlink()  # an earlier stay's, which would stop this updater
            except FileNotFoundError:
                pass
            # A copy of its own, out of the folder it swaps; it starts with -I, so nothing beside it is imported.
            write_private(self.folder / "updater.py", (HERE / "updater.py").read_text())
            if (self.folder / "update.log").exists():
                os.replace(self.folder / "update.log", self.folder / "update.log.1")
            self._write_result({"tag": tag, "from": VERSION, "to": version_of(tag), "state": "planned",
                                "why": "", "detail": "", "log": home_path(self.folder / "update.log")})
            write_private(plan_file, json.dumps(plan))
        except OSError as error:
            say(f"couldn't write the updater's plan ({error.strerror or error})")
            self._failed("helper", tag)
            then(False)
            return

        def started(ok: bool) -> None:
            if self.state != "launching":
                return  # Natter quit meanwhile, and took the plan back (stop)
            if ok:
                say(f"updating to {tag}, quitting; log in {home_path(self.folder / 'update.log')}")
                then(True)
                return
            self._write_result({"tag": tag, "from": VERSION, "to": version_of(tag), "state": "failed",
                                "why": "helper", "detail": "", "log": home_path(self.folder / "update.log")})
            self._failed("helper", tag)
            then(False)

        start_helper(self.folder / "updater.py", plan_file, started, self.home)

    def stay(self) -> None:
        """The updater has taken its plan, but Natter stays after all (a call came in as it
        started): the updater is told to change nothing (updater.CANCEL, beside the plan), and the
        update is available again, what was downloaded kept for the next Update."""
        if self.state != "launching":
            return
        try:
            write_private(self.folder / updater.CANCEL, "")
            say("staying after all (a call came in); the updater changes nothing")
        except OSError as error:
            say(f"couldn't tell the updater to change nothing ({error.strerror or error})")
        self.state = ""
        self.changed()

    def stop(self) -> None:
        """Natter quits: a check or a download under way ends (a started updater goes on), and a
        plan no updater has taken yet is taken back, as it holds Natter's environment."""
        if self.checker is not None:
            self.checker.cancel()
            self.checker = None
        if self.stager is not None:
            self.stager.cancel()
            self.stager = None
        self.waiting = []
        if self.state == "launching":
            plan = self.folder / "plan.json"
            withdrawn = plan.with_name(plan.name + ".withdrawn")
            try:
                os.rename(plan, withdrawn)  # unless an updater takes it this very moment
                withdrawn.unlink()
            except OSError:
                return  # taken: the updater goes on
            self.state = ""

    def _failed(self, why: str, tag: str, detail: str = "") -> None:
        self.state = ""
        self.failure = {"tag": tag, "from": VERSION, "to": version_of(tag), "why": why, "detail": detail,
                        "before": True, "kept": ""}
        self.untold = "failed"
        self.changed()
        self._tell_outcome()

    def _write_result(self, result: dict) -> None:
        try:
            private_folder(self.folder)
            write_result(self.folder / "result.json", dict(result, at=int(self.wall())))
        except OSError as error:
            say(f"couldn't write {home_path(self.folder / 'result.json')} ({error.strerror or error})")

    # ---- After an update --------------------------------------------------------------------

    def started(self) -> None:
        """Start-up is ready (Natter._now_ready): from now on checks may be due, and what the
        updater left in result.json is read. Installed and now running: the updater is told
        (state "started"), then the user, and the stage goes a minute later. Failed: told (the
        banner, and a notification when out of sight). Swapped or planned with no updater left:
        it was interrupted. Once only."""
        if self.ready_at is not None:
            return
        self.ready_at = self.clock()
        stored = self.settings["update_latest"]
        if stored and not (release_tag(stored) and newer(stored)):
            self.settings["update_latest"] = ""  # this version or older: an update has happened
        found = read_result(self.folder / "result.json")
        if found is None:
            return
        state = found["state"]
        if state == "told":
            if not found["why"] and self.channel == "get" and self.stages.is_dir():
                # Updated, but Natter quit before its stage went: it goes now.
                GLib.timeout_add_seconds(CLEANUP_DELAY, self._clean_up)
            return
        if state in ("installed", "started"):
            if found["to"] != VERSION:
                return  # another copy's to read
            found["state"] = "started"  # what the updater waits for
            self._write_result(found)
            say(f"updated to {VERSION}")
            if self.channel == "get":
                GLib.timeout_add_seconds(CLEANUP_DELAY, self._clean_up)
            self.notes = True
            self.untold = "updated"
            self.changed()
            self._tell_outcome()
            found["state"] = "told"
            self._write_result(found)
            return
        if state in ("planned", "swapped") and found.get("helper") and updater.alive(*found["helper"]):
            return  # the updater is still at work, and says how it went
        if state == "failed":
            why = found["why"] or "unfinished"
        elif state == "planned" and not found.get("helper"):
            why = "helper"  # no updater took the plan: Natter ended as it started one
        else:
            # Stopped on the way (a power cut, the updater ended). How far it got is in the files,
            # not in how far it noted it had: the words say only what's true either way.
            why = "unfinished"
        stage = self.stages / found["tag"]
        kept = found["kept"]
        if self.channel == "get" and state == "failed" and updater.version_in(self.root) == found["from"]:
            GLib.timeout_add_seconds(CLEANUP_DELAY, self._clean_up)  # the copy from before is back in its place
        elif (stage / "old").is_dir():
            kept = kept or home_path(stage / "old")
        self.failure = {"tag": found["tag"], "from": found["from"] or VERSION, "to": found["to"], "why": why,
                        "detail": found["detail"], "before": why in BEFORE_QUIT, "kept": kept}
        say(f"the update to {found['tag']} failed: {why}")
        self.untold = "failed"
        self.changed()
        self._tell_outcome()

    def _tell_outcome(self) -> None:
        """What became of an update, told once (`untold`): in a visible window, a toast for one
        that's done, and the banner, read out, for one that failed; out of sight (hidden, or
        locked), a notification, unless notifications are paused, when it waits for a later
        minute (tick) and the banner says it meanwhile."""
        if not self.untold:
            return
        app = self.app
        visible = self._visible()
        if not visible and self.settings.notifications_paused():
            return
        kind, self.untold = self.untold, ""
        if kind == "updated":
            title = f"Updated to {NAME} {VERSION}"
            if visible:
                app.window.toast(title, "What's New", "app.update-notes", timeout=8)
            else:
                app.tell("updated", title, button=("What's New", "app.update-notes", None))
            return
        failure = self.failure
        if failure is None:
            return  # its dialog was shown meanwhile
        title = f"Couldn't update to {NAME} {version_of(failure['tag'])}"
        if visible:
            app.window.say(title)
        elif failure["before"]:
            app.tell("update-failed", title, f"{why_words(failure['why'], failure['tag'], failure['detail'])} "
                                             "Nothing was changed.", ("Details", "app.update", None))
        else:
            app.tell("update-failed", title, still_installed(failure) or why_words(
                failure["why"], failure["tag"], failure["detail"], failure["to"]), ("Details", "app.update", None))

    def _clean_up(self) -> bool:
        """The stage, and the copy from before kept in it, on a thread: it's a whole source tree.
        Not while another update is being got ready there."""
        if self.working or self.stager is not None:
            return GLib.SOURCE_REMOVE
        stages = self.stages
        threading.Thread(target=shutil.rmtree, args=(stages,), kwargs={"ignore_errors": True},
                         name="natter-update-clean", daemon=True).start()
        return GLib.SOURCE_REMOVE


def still_installed(failure: dict) -> str:
    """The sentence saying the version from before the update is still installed, when it is as
    far as this Natter can tell (it's that version, and the old copy wasn't left aside), else ""."""
    if failure["from"] == VERSION and not failure["kept"]:
        return f"{NAME} {failure['from']} is still installed."
    return ""


# ---- Dialogs (app.update) ---------------------------------------------------------------------

def unsent(app) -> bool:
    """Whether a reply is still being written or sent (as app.restart asks)."""
    return any(window.unsent() for window in app.replies.values()) or any(
        text.strip() for text in app.reply_drafts.values())


def command_for(channel_name: str, root: Path = ROOT) -> str:
    """The way to update this copy by hand, for channels that have one."""
    if channel_name == "checkout":
        return f"cd {shlex.quote(str(root))} && git pull && ./install.sh"
    if channel_name in ("other", "get"):
        return GET_COMMAND
    if channel_name == "flatpak":
        return GET_FLATPAK_COMMAND
    return ""


HOW = {
    "checkout": f"This copy runs from a git checkout. Quit {NAME}, then update it in a terminal:",
    "other": f"Quit {NAME}, then run this in a terminal:",
    "flatpak": f"Quit {NAME}, then run this in a terminal to build the new Flatpak:",
    "package": "Update it the way you installed it, with your system's package manager.",
    "lab": "This is a lab instance: it never updates itself.",
}


def widgets():
    """GTK and libadwaita, for the dialogs only, imported when one is made: the rest of this module
    needs no display (tests/test_update.py gives stand-ins)."""
    from types import SimpleNamespace

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gtk, Pango

    from .prefs import icon_button

    return SimpleNamespace(Adw=Adw, Gdk=Gdk, Gtk=Gtk, Pango=Pango, icon_button=icon_button)


def _command_row(app, command: str):
    """The command, selectable, with a button that copies it."""
    ui = widgets()
    Gtk = ui.Gtk
    row = Gtk.Box(spacing=6)
    # It wraps, so all of it can be read before it's run, in a dialog as narrow as a phone: at
    # spaces, and inside the address after a "/" (Pango's line breaking, UAX #14), as addresses
    # wrap anywhere. The copy button copies it whole, as one line.
    label = Gtk.Label(label=command, selectable=True, wrap=True, wrap_mode=ui.Pango.WrapMode.WORD, xalign=0,
                      hexpand=True, css_classes=["monospace"])
    # Out of the focus chain (selectable made it focusable): as the dialog's first focus it would
    # open with all of it selected. The copy button is the way in from the keyboard.
    label.set_focusable(False)
    copy = ui.icon_button("edit-copy-symbolic", "Copy Command")
    copy.connect("clicked", lambda *_: ui.Gdk.Display.get_default().get_clipboard().set(command))
    row.append(label)
    row.append(copy)
    return row


def _whats_new(app, tag: str):
    """The release page's link, opened as Natter opens links (system.open_uri)."""
    Gtk = widgets().Gtk
    link = Gtk.LinkButton(label=f"What's New in {version_of(tag)}", uri=release_url(tag), halign=Gtk.Align.START)

    def opened(button) -> bool:
        open_uri(button.get_uri(), getattr(app, "window", None))
        return True

    link.connect("activate-link", opened)
    return link


def _answered(app, response: str, warned: bool = False) -> None:
    updates = app.updates
    if response == "skip":
        updates.dismiss()
    elif response in ("later", "close"):
        updates.later()
    elif response == "update":
        app.update_now(warned)


def update_dialog(app):
    """The dialog for a release that's out: get.sh's copy asks whether to update now; any other
    says how to update it. None when there's nothing newer."""
    ui = widgets()
    Adw, Gtk = ui.Adw, ui.Gtk
    updates = app.updates
    tag = updates.latest
    if not tag:
        return None
    version = version_of(tag)
    warned = False  # of replies not sent: Update then needn't ask again (Natter._update_prepared)
    if updates.channel == "get":
        body = (f"{NAME} closes, updates itself and opens again, usually within a minute. Your chats and settings "
                "stay as they are.")
        warned = unsent(app)
        if warned:
            body += " Replies you haven't sent will be lost."
        alert = Adw.AlertDialog(heading=f"Update to {NAME} {version}?", body=body)
        alert.set_extra_child(_whats_new(app, tag))
        alert.add_response("skip", "_Skip This Version")
        alert.add_response("later", "_Later")
        alert.add_response("update", "_Update")
        alert.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("later")
        alert.set_close_response("later")
    else:
        alert = Adw.AlertDialog(heading=f"{NAME} {version} Is Available", body=HOW.get(updates.channel, HOW["other"]))
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        command = command_for(updates.channel, updates.root) if updates.channel != "lab" else ""
        if command:
            extra.append(_command_row(app, command))
        extra.append(_whats_new(app, tag))
        alert.set_extra_child(extra)
        alert.add_response("skip", "_Skip This Version")
        alert.add_response("close", "_Close")
        alert.set_default_response("close")
        alert.set_close_response("close")
    alert.connect("response", lambda _alert, response: _answered(app, response, warned))
    return alert


def replies_dialog(app):
    """Asked right before Natter quits for an update, when replies were written after the update
    dialog was shown without a word about them (Natter._update_prepared): they'd be lost. Its
    Update starts the update again, with what was downloaded."""
    Adw = widgets().Adw
    alert = Adw.AlertDialog(heading=f"Update {NAME}?", body="Replies you haven't sent will be lost.")
    alert.add_response("cancel", "_Cancel")
    alert.add_response("update", "_Update")
    alert.set_response_appearance("update", Adw.ResponseAppearance.DESTRUCTIVE)
    alert.set_default_response("cancel")
    alert.set_close_response("cancel")
    alert.connect("response", lambda _alert, response: response == "update" and app.update_now(True))
    return alert


def failure_dialog(app):
    """The dialog for an update that failed: why, what's installed now, and the way by hand."""
    ui = widgets()
    Adw, Gtk = ui.Adw, ui.Gtk
    failure = app.updates.failure
    if failure is None:
        return None
    tag, why = failure["tag"], failure["why"]
    body = why_words(why, tag, failure["detail"], failure["to"])
    if failure["before"]:
        body += " Nothing was changed."
    else:
        installed = still_installed(failure)
        if installed:
            body += " " + installed
        body += f" The log is in {home_path(app.updates.folder / 'update.log')}."
    if failure["kept"]:
        body += f" The copy from before is in {failure['kept']}."
    alert = Adw.AlertDialog(heading=f"Couldn't Update to {NAME} {version_of(tag)}", body=body)
    extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    extra.append(Gtk.Label(label=f"To update by hand, quit {NAME} and run:", xalign=0, wrap=True))
    extra.append(_command_row(app, GET_COMMAND))
    alert.set_extra_child(extra)
    alert.add_response("close", "_Close")
    if why in TRANSIENT:
        alert.add_response("retry", "_Try Again")
        alert.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
    alert.set_default_response("close")
    alert.set_close_response("close")
    # Close leaves the banner to say what's available again (told): it isn't Later. Try Again
    # warned of no replies: written meanwhile, they're asked about before Natter quits.
    alert.connect("response", lambda _alert, response: response == "retry" and app.update_now())
    return alert


def dialog_for(app):
    """app.update's dialog: a failure to tell first (shown, it's told), else the release that's out."""
    if app.updates.failure is not None:
        alert = failure_dialog(app)
        app.updates.told()
        return alert
    return update_dialog(app)
