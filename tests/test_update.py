"""Updates (update.py): versions and tags, GitHub's answers read, how a copy was installed, when a
check is due, the words each surface shows, a check made through a stand-in transport, a release's
archive unpacked and checked, the new copy's start-up check, the updater's command and start, and
what the next start makes of result.json. Nothing reaches the network: every request goes to a
stand-in, and the transport that would reach GitHub is never made."""

import ast
import errno
import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import Gio, GLib

from natter import NAME, VERSION, gstfix, system, update, updater
from natter.settings import DEFAULTS, Settings

ROOT = Path(__file__).resolve().parent.parent
PARTS = [int(part) for part in VERSION.split(".")]
NEWER = "v" + ".".join(map(str, PARTS[:-1] + [PARTS[-1] + 1]))  # the next release after this one
NEWEST = "v" + ".".join(map(str, PARTS[:-1] + [PARTS[-1] + 2]))
SAME = "v" + VERSION
OLDER = "v0.0.1"


def spin(until, seconds=10.0):
    context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.002)
    return until()


def release_json(tag, **more):
    """The API's answer for a release, as GitHub writes it."""
    release = {"url": "https://api.github.com/repos/Officialbabs/natter/releases/1",
               "html_url": update.TAG_PAGE + tag, "id": 1, "tag_name": tag, "target_commitish": "master",
               "name": f"{NAME} {tag}", "draft": False, "prerelease": False, "assets": [],
               "tarball_url": "https://api.github.com/repos/Officialbabs/natter/tarball/" + tag, **more}
    return json.dumps(release, indent=2).encode()


class Transport:
    """A stand-in for GitHub: each request gets the next canned answer, (status, location, body,
    error), or one made by a function of (method, url); with none left it never answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, method, url, headers, limit, cancellable, done):
        self.asked.append((method, url, dict(headers), limit))
        if not self.answers:
            return
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(method, url)
        done(*answer)


class Held:
    """A stand-in for GitHub that answers each request only once the test says what (answer)."""

    def __init__(self):
        self.asked, self.pending = [], []

    def __call__(self, method, url, headers, limit, cancellable, done):
        self.asked.append((method, url))
        self.pending.append(done)

    def answer(self, *reply):
        self.pending.pop(0)(*reply)


def quietly(run, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return run(*args, **kwargs)


def extract(script: str, pattern: str) -> str:
    match = re.search(pattern, (ROOT / script).read_text(), re.S | re.M)
    assert match, pattern
    return match[1]


# ---- Versions, tags and GitHub's answers ------------------------------------------------------

class VersionTest(unittest.TestCase):
    def test_newer_by_number(self):
        self.assertTrue(update.newer("v0.1.10", "0.1.9"))
        self.assertTrue(update.newer("v1.0", "0.9.9"))
        self.assertTrue(update.newer("v0.2.0.1", "0.2.0"))
        self.assertTrue(update.newer(NEWER))

    def test_equal_and_older_are_not(self):
        for tag, version in (("v0.1.9", "0.1.9"), ("v0.1.8", "0.1.9"), ("v0.1.9", "0.1.10"), (SAME, VERSION),
                             (OLDER, VERSION)):
            with self.subTest(tag=tag, version=version):
                self.assertFalse(update.newer(tag, version))

    def test_missing_parts_are_zero(self):
        self.assertEqual(update.version_tuple("0.2"), update.version_tuple("0.2.0"))
        self.assertEqual(update.version_tuple("0.1.10"), (0, 1, 10, 0))
        self.assertFalse(update.newer("v0.2", "0.2.0"))
        self.assertFalse(update.newer("v0.2.0", "0.2"))

    def test_a_version_that_isnt_one_is_never_older(self):
        for version in ("0.1.9.dev1", "", "1", "01.2", "git", None):
            with self.subTest(version=version):
                self.assertIsNone(update.version_tuple(version))
                self.assertFalse(update.newer("v99.0", version))

    def test_a_tag_that_isnt_one_is_never_newer(self):
        for tag in ("v1.0-rc1", "1.0", "", "v", "v99999999.0"):
            with self.subTest(tag=tag):
                self.assertFalse(update.newer(tag, "0.0.1"))


ACCEPTED = ["v0.1", "v0.1.9", "v0.1.10", "v1.2.3.4", "v99999.0", "v0.0.0", "v10.20.30", "v99999.99999.99999.99999"]
REFUSED = ["v", "v1", "v1.0-rc1", "v01.2", "v1.02", "0.1.9", "v1.2.3.4.5", "v123456.0", "v1..2", "v1.2.", " v1.2",
           "v1.2\n", "V1.2", "v١.٢", "v1.2 ", "v1.2/x", "", "vv1.2", "v-1.2"]


class TagTest(unittest.TestCase):
    def test_accepted_and_refused(self):
        for tag in ACCEPTED:
            with self.subTest(tag=tag):
                self.assertTrue(update.release_tag(tag))
                self.assertLessEqual(len(tag), 24)
        for tag in REFUSED + [None, 3, b"v1.2"]:
            with self.subTest(tag=tag):
                self.assertFalse(update.release_tag(tag))

    def test_every_accepted_tag_passes_get_sh(self):
        sane_tag = extract("get.sh", r"^(sane_tag\(\) \{\n.*?\n\})")
        for tag in ACCEPTED:
            with self.subTest(tag=tag):
                done = subprocess.run(["sh", "-c", sane_tag + '\nsane_tag "$1"', "sh", tag], timeout=10)
                self.assertEqual(done.returncode, 0)

    def test_the_updater_reads_tags_the_same_way(self):
        for tag in ACCEPTED + REFUSED:
            with self.subTest(tag=tag):
                self.assertEqual(bool(updater.TAG.fullmatch(tag)), update.release_tag(tag))


class ParseTest(unittest.TestCase):
    def test_the_latest_release(self):
        self.assertEqual(update.parse_latest(release_json("v0.2.0")), "v0.2.0")
        compact = json.dumps({"tag_name": "v0.2.0", "draft": False}, separators=(",", ":")).encode()
        self.assertEqual(update.parse_latest(compact), "v0.2.0")

    def test_drafts_and_pre_releases_are_none_to_offer(self):
        for more in ({"draft": True}, {"prerelease": True}, {"draft": "false"}, {"prerelease": None}):
            with self.subTest(more=more):
                self.assertEqual(update.parse_latest(release_json("v0.2.0", **more)), "")

    def test_a_tag_that_isnt_a_version_is_none_to_offer(self):
        for tag in ("v1.0-rc1", "nightly", "", "v01.2"):
            with self.subTest(tag=tag):
                self.assertEqual(update.parse_latest(release_json(tag)), "")

    def test_what_isnt_an_answer(self):
        for data in (b"[]", b"null", b'"v0.2.0"', b"{}", b'{"tag_name": 5}', b"<html>rate limited</html>", b"\xff\xfe",
                     b'{"tag_name": "v0.2.0"' + b" " * (1 << 20) + b"}"):
            with self.subTest(data=data[:40]):
                self.assertIsNone(update.parse_latest(data))

    def test_nested_beyond_pythons_stack_isnt_an_answer(self):
        # 200 KB, under API_BYTES: json raises RecursionError, which would skip the fallback.
        self.assertIsNone(update.parse_latest(b"[" * 100000 + b"]" * 100000))

    def test_only_the_tag_is_read(self):
        # html_url, tarball_url and assets lead elsewhere: every address is made from REPO and the tag.
        data = release_json("v0.2.0", html_url="https://example.com/evil", tarball_url="https://example.com/x.tgz",
                            assets=[{"browser_download_url": "https://example.com/natter.tar.gz"}])
        self.assertEqual(update.parse_latest(data), "v0.2.0")
        self.assertEqual(update.archive_url("v0.2.0"),
                         "https://github.com/Officialbabs/natter/archive/refs/tags/v0.2.0.tar.gz")
        self.assertEqual(update.release_url("v0.2.0"), "https://github.com/Officialbabs/natter/releases/tag/v0.2.0")


class LocationTest(unittest.TestCase):
    def test_where_the_latest_link_leads(self):
        self.assertEqual(update.tag_from_location(update.TAG_PAGE + "v0.2.0"), "v0.2.0")
        self.assertEqual(update.tag_from_location(update.RELEASES), "")  # no release yet
        self.assertEqual(update.tag_from_location(update.TAG_PAGE + "v1.0-rc1"), "")

    def test_anywhere_else_is_no_answer(self):
        for location in ("https://evil.example/Officialbabs/natter/releases/tag/v0.2.0",
                         "http://github.com/Officialbabs/natter/releases/tag/v0.2.0",
                         "https://github.com/Other/natter/releases/tag/v0.2.0",
                         "https://github.com.evil.example/Officialbabs/natter/releases/tag/v0.2.0",
                         update.TAG_PAGE + "v0.2.0/extra", update.TAG_PAGE + "v0.2.0?x=1", update.TAG_PAGE,
                         update.RELEASES + "/", "/Officialbabs/natter/releases/tag/v0.2.0", ""):
            with self.subTest(location=location):
                self.assertIsNone(update.tag_from_location(location))

    def test_where_an_archive_may_come_from(self):
        start = update.archive_url("v0.2.0")
        codeload = "https://codeload.github.com/Officialbabs/natter/tar.gz/refs/tags/v0.2.0"
        self.assertEqual(update.archive_redirect(codeload, start), codeload)
        self.assertEqual(update.archive_redirect("/Officialbabs/natter/archive/v0.2.0.tar.gz", start),
                         "https://github.com/Officialbabs/natter/archive/v0.2.0.tar.gz")
        for location in ("http://codeload.github.com/x", "https://evil.example/x", "https://github.com.evil.example/x",
                         "https://codeload.github.com:8443/x", "https://user@github.com/x", "ftp://github.com/x",
                         "https://objects.githubusercontent.com/x", "https://codeload.github.com:bad/x"):
            with self.subTest(location=location):
                self.assertIsNone(update.archive_redirect(location, start))

    def test_an_archive_comes_from_this_repository_only(self):
        start = update.archive_url("v0.2.0")
        for location in ("https://github.com/someone/else/archive/refs/tags/v0.2.0.tar.gz",
                         "https://codeload.github.com/someone/else/tar.gz/refs/tags/v0.2.0",
                         "/someone/else/archive/refs/tags/v0.2.0.tar.gz", "../../../../../someone/else/x.tar.gz",
                         "https://codeload.github.com/Officialbabs/natter-fork/tar.gz/refs/tags/v0.2.0",
                         "https://codeload.github.com/Officialbabs/natter"):
            with self.subTest(location=location):
                self.assertIsNone(update.archive_redirect(location, start))
        # GitHub takes its names in any case, and may answer with them so.
        lower = "https://codeload.github.com/officialbabs/natter/tar.gz/refs/tags/v0.2.0"
        self.assertEqual(update.archive_redirect(lower, start), lower)


# ---- How this copy was installed --------------------------------------------------------------

class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        self.source = self.home / ".local/lib/natter/source"
        make_copy(self.source, VERSION)

    def tearDown(self):
        self.tmp.cleanup()

    def launcher(self, folder):
        return f'#!/usr/bin/python3\n"""Run Natter."""\nNATTER_DIR = {json.dumps(str(folder))}\n'

    def channel(self, root=None, launcher=None, **facts):
        root = self.source if root is None else root
        return update.channel(root, self.home, self.launcher(self.source) if launcher is None else launcher, **facts)

    def test_get_sh_copy(self):
        self.assertEqual(self.channel(), "get")

    def test_rules_in_order(self):
        checkout = Path(self.tmp.name).resolve() / "checkout"
        make_copy(checkout, VERSION)
        (checkout / ".git").mkdir()
        meson = Path(self.tmp.name).resolve() / "usr/share/natter"
        make_copy(meson, VERSION)
        unpacked = Path(self.tmp.name).resolve() / "Downloads/natter-0.1.9"
        make_copy(unpacked, VERSION)
        for facts, root, launcher, expected in [
            ({"app_id": "io.github.Officialbabs.Natter.Lab"}, None, None, "lab"),
            ({"app_id": "io.github.Officialbabs.Natter.Lab", "off": True}, None, None, "lab"),
            ({"off": True}, None, None, "off"),
            ({"off": True, "flatpak": True}, None, None, "off"),
            ({"flatpak": True, "sandboxed": True}, None, None, "flatpak"),
            ({"sandboxed": True}, None, None, "package"),  # Snap
            ({"writable": False}, None, None, "package"),
            ({}, meson, self.launcher(meson), "package"),
            ({}, checkout, self.launcher(checkout), "checkout"),
            ({}, unpacked, self.launcher(unpacked), "other"),
            # get.sh's folder, but the natter command starts a checkout: get.sh would move it.
            ({}, None, self.launcher(checkout), "other"),
            ({}, None, "", "other"),
            ({}, None, '#!/usr/bin/python3\nNATTER_DIR = ""  # empty: the checkout this script is in\n', "other"),
        ]:
            with self.subTest(facts=facts, root=root, expected=expected):
                self.assertEqual(self.channel(root, launcher, **facts), expected)

    def test_get_needs_a_whole_copy_without_git(self):
        (self.source / ".git").mkdir()
        self.assertEqual(self.channel(), "checkout")
        (self.source / ".git").rmdir()
        (self.source / "install.sh").unlink()
        self.assertEqual(self.channel(), "other")

    def test_launcher_dir(self):
        self.assertEqual(update.launcher_dir(self.launcher(self.source)), str(self.source))
        self.assertIsNone(update.launcher_dir('NATTER_DIR = ""  # empty: the checkout this script is in'))
        self.assertIsNone(update.launcher_dir("#!/usr/bin/python3\nimport os\n"))
        self.assertIsNone(update.launcher_dir('NATTER_DIR = os.environ["HOME"]\n'))
        self.assertIsNone(update.launcher_dir('NATTER_DIR = "a" + "b"\n'))
        self.assertIsNone(update.launcher_dir(None))

    def test_launcher_dir_reads_what_install_sh_writes(self):
        py_quote = extract("install.sh", r"^(py_quote\(\) \{.*?\})$")
        for folder in ("/home/a b/.local/lib/natter/source", '/tmp/quote"d', "/tmp/back\\slash", "/tmp/dollar$HOME"):
            with self.subTest(folder=folder):
                quoted = subprocess.run(["sh", "-c", py_quote + '\npy_quote "$1"', "sh", folder], capture_output=True,
                                        text=True, timeout=10).stdout
                self.assertEqual(update.launcher_dir(f"NATTER_DIR = {quoted}\n"), folder)

    def test_build_file_and_switch(self):
        build = Path(self.tmp.name) / "update-check"
        self.assertFalse(update.build_off(build))
        build.write_text("off\n")
        self.assertTrue(update.build_off(build))
        build.write_text("on\n")
        self.assertFalse(update.build_off(build))
        self.assertTrue(update.checks_off({"NATTER_UPDATE_CHECK": "0"}))
        for environ in ({}, {"NATTER_UPDATE_CHECK": "1"}, {"NATTER_UPDATE_CHECK": ""}):
            self.assertFalse(update.checks_off(environ))
        self.assertFalse((ROOT / "natter" / "update-check").exists())  # a checkout checks

    def test_packagers_turn_checks_off_at_build_time(self):
        options = (ROOT / "meson_options.txt").read_text()
        self.assertIn("option('update_check', type: 'boolean', value: true", options)
        meson = (ROOT / "meson.build").read_text()
        block = re.search(r"^if not get_option\('update_check'\)\n(.*?)^endif$", meson, re.S | re.M)
        self.assertIsNotNone(block)
        self.assertIn(f"output: '{update.BUILD_FILE.name}'", block[1])
        self.assertIn("install_dir: pkgdatadir / 'natter'", block[1])  # next to update.py
        self.assertIn("configuration: {'UPDATE_CHECK': 'off'}", block[1])
        rendered = Path(self.tmp.name) / "update-check"
        rendered.write_text((ROOT / "data" / "update-check.in").read_text().replace("@UPDATE_CHECK@", "off"))
        self.assertTrue(update.build_off(rendered))

    def test_launcher_python(self):
        self.assertEqual(update.launcher_python(f"#!{sys.executable}\nNATTER_DIR = ''\n"), sys.executable)
        self.assertEqual(update.launcher_python("#!/nonexistent/python3\n"), sys.executable)
        self.assertEqual(update.launcher_python(None), sys.executable)


# ---- When to check, and what to say -----------------------------------------------------------

NOW = 1_800_000_000.0


class DueTest(unittest.TestCase):
    def due(self, checked=NOW - update.CHECK_EVERY, ready=300.0, failed=None, **facts):
        return update.due(NOW, checked, ready, failed, **facts)

    def test_daily(self):
        self.assertTrue(self.due())
        self.assertTrue(self.due(checked=0))
        self.assertFalse(self.due(checked=NOW - update.CHECK_EVERY + 1))
        self.assertFalse(self.due(checked=NOW - 60))

    def test_first_delay(self):
        self.assertFalse(self.due(ready=None))
        self.assertFalse(self.due(ready=update.FIRST_CHECK_DELAY - 1))
        self.assertTrue(self.due(ready=update.FIRST_CHECK_DELAY))

    def test_clock_skew(self):
        self.assertTrue(self.due(checked=NOW + 2 * 3600))
        self.assertFalse(self.due(checked=NOW + 1800))

    def test_retry_after_a_failure(self):
        self.assertFalse(self.due(failed=update.RETRY_AFTER - 1))
        self.assertTrue(self.due(failed=update.RETRY_AFTER))

    def test_held_off(self):
        for fact in ({"allowed": False}, {"switch": False}, {"online": False}, {"calling": True}, {"loading": True},
                     {"linking": True}, {"checking": True}):
            with self.subTest(fact=fact):
                self.assertFalse(self.due(**fact))

    def test_a_stored_time_that_isnt_one(self):
        for checked in ("yesterday", None, True, [1]):
            with self.subTest(checked=checked):
                self.assertTrue(self.due(checked=checked))

    def test_loading(self):
        def account(**state):
            return SimpleNamespace(**{"ready": False, "needs_link": False, "failed": None, "crashing": False,
                                      "loaded_at": 1000.0, **state})

        now = 1000.0 + 60
        self.assertFalse(update.loading([], now))
        self.assertFalse(update.loading([account(ready=True)], now))
        self.assertTrue(update.loading([account(ready=True), account()], now))  # syncing after a scan, or reloading
        self.assertFalse(update.loading([account(needs_link=True)], now))
        self.assertFalse(update.loading([account(failed="offline")], now))
        self.assertFalse(update.loading([account(crashing=True)], now))

    def test_a_page_that_never_gets_ready_holds_nothing_for_good(self):
        # Loaded, not failed, not linking, never ready: WhatsApp changed its page, or Natter gave up
        # reloading a blank one. It holds the check off a while, as any load, then no longer.
        stuck = SimpleNamespace(ready=False, needs_link=False, failed=None, crashing=False, loaded_at=1000.0)
        self.assertTrue(update.loading([stuck], 1000.0 + update.LOAD_HOLD - 1))
        self.assertFalse(update.loading([stuck], 1000.0 + update.LOAD_HOLD))

    def test_retry_grows_with_each_failure_in_a_row(self):
        self.assertFalse(self.due(failed=2 * update.RETRY_AFTER - 1, retry=2 * update.RETRY_AFTER))
        self.assertTrue(self.due(failed=2 * update.RETRY_AFTER, retry=2 * update.RETRY_AFTER))

    def test_linking(self):
        def window(visible, needs_link):
            return SimpleNamespace(get_visible=lambda: visible, account=SimpleNamespace(needs_link=needs_link))

        self.assertTrue(update.linking(window(True, True)))
        self.assertFalse(update.linking(window(False, True)))  # a hidden link screen doesn't hold checks off
        self.assertFalse(update.linking(window(True, False)))
        self.assertFalse(update.linking(None))
        self.assertFalse(update.linking(SimpleNamespace(get_visible=lambda: True, account=None)))


class WordsTest(unittest.TestCase):
    def test_notice(self):
        # Its Update opens the dialog, which asks: the body promises no more than that.
        self.assertEqual(update.notice("get", "v0.2.0"),
                         (f"{NAME} 0.2.0 is available", f"{NAME} can update itself, usually within a minute.", "Update"))
        self.assertEqual(update.notice("package", "v0.2.0")[1:],
                         ("Update it the way you installed it, with your system's package manager.", "Details"))
        for channel in ("checkout", "other", "flatpak", "lab"):
            with self.subTest(channel=channel):
                self.assertEqual(update.notice(channel, "v0.2.0"), (f"{NAME} 0.2.0 is available",
                                                                    "Choose Details to see how to update it.", "Details"))

    def test_banner(self):
        self.assertEqual(update.banner("available", "v0.2.0", "get"),
                         (f"{NAME} 0.2.0 is available", "Update", "app.update", False))
        for channel in ("checkout", "other", "flatpak", "package"):
            self.assertEqual(update.banner("available", "v0.2.0", channel)[1], "Details")
        self.assertEqual(update.banner("preparing", "v0.2.0", "get"), (f"Downloading {NAME} 0.2.0…", "", "", True))
        self.assertEqual(update.banner("failed", "v0.2.0", "get"),
                         (f"Couldn't update to {NAME} 0.2.0", "Details", "app.update", True))
        self.assertIsNone(update.banner("", "v0.2.0", "get"))

    def test_why_words(self):
        whys = ("check", "download", "archive", "space", "cant-run", "helper", "busy", "other", "launcher", "install",
                "timeout", "start", "unfinished")
        for why in whys:
            with self.subTest(why=why):
                words = update.why_words(why, "v0.2.0", "detail here", "0.2.0")
                self.assertNotEqual(words, "Something went wrong.")
                self.assertTrue(words.endswith("."), words)
                self.assertEqual(words[0], words[0].upper())
        self.assertEqual(update.why_words("cant-run", "v0.2.0", "GTK 4 is 4.12; Natter Den needs 4.14 or newer"),
                         f"{NAME} 0.2.0 can't run on this system as it is: GTK 4 is 4.12; Natter Den needs 4.14 or newer.")
        self.assertEqual(update.why_words("install", "v0.2.0", "Couldn't build."),
                         "Installing it stopped with an error (Couldn't build).")
        self.assertEqual(update.why_words("install", "v0.2.0"), "Installing it stopped with an error.")
        self.assertEqual(update.why_words("start", "v0.2.0", new="0.2.0"), f"{NAME} 0.2.0 didn't start.")
        self.assertEqual(update.why_words("start", "v0.2.0", "exit status 1", "0.2.0"),
                         f"{NAME} 0.2.0 didn't start (exit status 1).")
        self.assertEqual(update.why_words("archive", "v0.2.0"), f"The download wasn't a complete copy of {NAME} 0.2.0.")
        self.assertTrue(set(update.TRANSIENT) <= set(whys) and set(update.BEFORE_QUIT) <= set(whys))

    def test_commands(self):
        self.assertEqual(update.command_for("checkout", Path("/home/a b/natter")),
                         "cd '/home/a b/natter' && git pull && ./install.sh")
        self.assertEqual(update.command_for("other"), update.GET_COMMAND)
        self.assertEqual(update.command_for("flatpak"), update.GET_COMMAND + " -s -- --flatpak")
        self.assertEqual(update.command_for("package"), "")
        self.assertEqual(set(update.HOW), {"checkout", "other", "flatpak", "package", "lab"})

    def test_same_as_get_sh_and_install_sh(self):
        self.assertEqual(update.GET_COMMAND, extract("get.sh", r'^GET="([^"\n]*)"$'))
        self.assertEqual(update.REPO, extract("get.sh", r'^REPO="([^"\n]*)"$'))
        self.assertEqual(update.LATEST_API, extract("get.sh", r'^LATEST_API="([^"\n]*)"$'))
        self.assertEqual(update.CHECK, extract("install.sh", r"^CHECK='(.*?)'$"))

    def test_checked_words(self):
        noon = time.mktime((2026, 11, 2, 12, 5, 0, 0, 0, -1))
        self.assertEqual(update.checked_words(noon, noon + 3600), "today at 12:05")
        self.assertEqual(update.checked_words(noon - 86400 * 3, noon), "on 30 Oct")

    def test_checked_words_in_english_whatever_the_locale(self):
        # Gtk sets LC_TIME to the user's: strftime's month would read "Okt" in an English line.
        with mock.patch.object(update.time, "strftime", side_effect=AssertionError("strftime follows LC_TIME")):
            noon = time.mktime((2026, 11, 2, 12, 5, 0, 0, 0, -1))
            self.assertEqual(update.checked_words(noon - 86400 * 3, noon), "on 30 Oct")
            self.assertEqual(update.checked_words(noon, noon + 60), "today at 12:05")

    def test_first_problem(self):
        explained = f"{NAME} can't start:\n  - GTK 4 is 4.12; {NAME} needs 4.14 or newer\n  - libadwaita for Python isn't installed\nInstall…"
        self.assertEqual(update.first_problem(explained), f"GTK 4 is 4.12; {NAME} needs 4.14 or newer")
        self.assertEqual(update.first_problem(f"{NAME} needs Python 3.9 or newer\n"), f"{NAME} needs Python 3.9 or newer")
        self.assertEqual(len(update.first_problem("x" * 500)), 200)
        self.assertEqual(update.first_problem(""), "")


# ---- Asking GitHub ----------------------------------------------------------------------------

class CheckerTest(unittest.TestCase):
    def check(self, transport, feed="", deadline=5.0):
        found = []
        checker = update.Checker(transport, feed, deadline)
        quietly(checker.start, found.append)
        quietly(spin, lambda: found, deadline + 2)
        self.assertEqual(len(found), 1)
        return found[0]

    def test_headers_say_nothing_more(self):
        transport = Transport((200, "", release_json("v0.2.0"), ""))
        self.assertEqual(self.check(transport), "v0.2.0")
        self.assertEqual(transport.asked, [("GET", update.LATEST_API, {"User-Agent": "natter-den",
                                                                       "Accept": "application/vnd.github+json"},
                                            update.API_BYTES)])

    def test_falls_back_to_the_releases_page(self):
        for status, body in ((403, b""), (429, b""), (500, b""), (0, b""), (200, b"<html>"), (301, b"")):
            with self.subTest(status=status):
                transport = Transport((status, "", body, "no answer" if not status else ""),
                                      (302, update.TAG_PAGE + "v0.2.0", b"", ""))
                self.assertEqual(self.check(transport), "v0.2.0")
                self.assertEqual(transport.asked[1], ("HEAD", update.LATEST_PAGE, {"User-Agent": "natter-den"}, 0))
                for _method, _url, headers, _limit in transport.asked:
                    self.assertFalse({"Cookie", "Authorization", "If-None-Match"} & set(headers))

    def test_no_release_yet(self):
        transport = Transport((404, "", b"", ""))
        self.assertEqual(self.check(transport), "")
        self.assertEqual(len(transport.asked), 1)
        self.assertEqual(self.check(Transport((403, "", b"", ""), (302, update.RELEASES, b"", ""))), "")

    def test_no_answer(self):
        for page in ((200, "", b"", ""), (302, "https://example.com/elsewhere", b"", ""), (0, "", b"", "offline"),
                     (302, "", b"", "")):
            with self.subTest(page=page):
                self.assertIsNone(self.check(Transport((403, "", b"", ""), page)))

    def test_an_answer_nested_too_deep_falls_back(self):
        transport = Transport((200, "", b"[" * 100000 + b"]" * 100000, ""), (302, update.TAG_PAGE + "v0.2.0", b"", ""))
        self.assertEqual(self.check(transport), "v0.2.0")
        self.assertEqual([method for method, *_ in transport.asked], ["GET", "HEAD"])

    def test_deadline(self):
        transport = Transport()  # never answers
        self.assertIsNone(self.check(transport, deadline=0.05))
        self.assertEqual(len(transport.asked), 1)

    def test_a_labs_feed(self):
        with tempfile.TemporaryDirectory() as folder:
            feed = Path(folder) / "latest.json"
            feed.write_bytes(release_json("v0.2.0"))
            transport = Transport()
            self.assertEqual(self.check(transport, f"file://{feed}"), "v0.2.0")
            self.assertEqual(transport.asked, [])
            feed.write_bytes(b"not json")
            self.assertIsNone(self.check(transport, f"file://{feed}"))  # no fallback
            self.assertEqual(transport.asked, [])
        local = Transport((200, "", release_json("v0.3.0"), ""))
        self.assertEqual(self.check(local, "http://127.0.0.1:8123/latest"), "v0.3.0")
        self.assertEqual(local.asked[0][1], "http://127.0.0.1:8123/latest")
        elsewhere = Transport((200, "", release_json("v0.3.0"), ""))
        self.assertIsNone(self.check(elsewhere, "https://example.com/latest"))
        self.assertEqual(elsewhere.asked, [])

    def test_the_real_transport_holds_with_checks_off(self):
        answered = []
        transport = update.SoupTransport()
        with mock.patch.dict(os.environ, {"NATTER_UPDATE_CHECK": "0"}):  # an address nothing answers on, all the same
            transport("GET", "http://127.0.0.1:9/", {}, 10, Gio.Cancellable(), lambda *answer: answered.append(answer))
        self.assertEqual(answered, [(0, "", b"", "checks are off (NATTER_UPDATE_CHECK=0)")])
        self.assertIsNone(transport.session)  # nothing was made, let alone sent

    def test_the_user_agent_goes_inside_tls_only(self):
        # Set on each message, never on the session, whose own goes to a proxy's CONNECT in the
        # clear. The session is a stand-in: nothing is sent.
        made, sent = [], []
        session = SimpleNamespace(send_async=lambda message, *rest: sent.append(message))
        with mock.patch.object(update.Soup, "Session", lambda **props: made.append(props) or session), \
                mock.patch.dict(os.environ, {"NATTER_UPDATE_CHECK": "1"}):
            update.SoupTransport()("GET", "http://127.0.0.1:9/", {"User-Agent": update.USER_AGENT}, 10,
                                   Gio.Cancellable(), lambda *answer: None)
        self.assertEqual(len(made), 1)
        self.assertNotIn("user_agent", made[0])
        self.assertEqual(sent[0].get_request_headers().get_one("User-Agent"), update.USER_AGENT)


# ---- A release's archive ----------------------------------------------------------------------

def make_copy(folder: Path, version: str, check: str = "def check():\n    return 0\n") -> Path:
    """A whole copy of Natter at `version`, as far as the update is concerned."""
    (folder / "natter").mkdir(parents=True, exist_ok=True)
    (folder / "bin").mkdir(exist_ok=True)
    (folder / "install.sh").write_text("#!/bin/sh\necho installed\n")
    (folder / "install.sh").chmod(0o755)
    (folder / "bin/natter").write_text('#!/usr/bin/python3\nNATTER_DIR = ""\n')
    (folder / "bin/natter").chmod(0o755)
    (folder / "natter/__init__.py").write_text(f'NAME = "{NAME}"\nVERSION = "{version}"\n')
    (folder / "natter/__main__.py").write_text(check)
    return folder


def release_archive(path: Path, version: str, top: str = "", extra=(), check: str = "def check():\n    return 0\n",
                    skip=(), pax=True) -> Path:
    """A release's archive as GitHub makes it: one top folder (natter-<version>/), a pax global
    header with the commit. `extra` are (TarInfo, bytes) added after."""
    top = top or f"natter-{version}"
    files = {"install.sh": (b"#!/bin/sh\necho installed\n", 0o775), "bin/natter": (b'#!/usr/bin/python3\nNATTER_DIR = ""\n', 0o775),
             "natter/__init__.py": (f'NAME = "{NAME}"\nVERSION = "{version}"\n'.encode(), 0o664),
             "natter/__main__.py": (check.encode(), 0o664), "README.md": (b"# readme\n", 0o664)}
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "0123456789abcdef"} if pax else None) as archive:
        folder = tarfile.TarInfo(top + "/")
        folder.type, folder.mode = tarfile.DIRTYPE, 0o775
        archive.addfile(folder)
        for name in ("bin", "natter"):
            sub = tarfile.TarInfo(f"{top}/{name}/")
            sub.type, sub.mode = tarfile.DIRTYPE, 0o775
            archive.addfile(sub)
        for name, (data, mode) in files.items():
            if name in skip:
                continue
            member = tarfile.TarInfo(f"{top}/{name}")
            member.size, member.mode = len(data), mode
            archive.addfile(member, io.BytesIO(data))
        for member, data in extra:
            archive.addfile(member, io.BytesIO(data) if data is not None else None)
    return path


def member(name, kind=tarfile.REGTYPE, data=b"", link=""):
    info = tarfile.TarInfo(name)
    info.type, info.size, info.linkname = kind, len(data) if kind == tarfile.REGTYPE else 0, link
    return info, data if kind == tarfile.REGTYPE else None


class UnpackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.into = self.dir / "source"

    def tearDown(self):
        self.tmp.cleanup()

    def refused(self, archive, why="archive", **limits):
        with self.assertRaises(update.Failed) as caught:
            update.unpack(archive, self.into, **limits)
        self.assertEqual(caught.exception.why, why)
        return caught.exception

    def test_a_whole_release(self):
        archive = release_archive(self.dir / "good.tar.gz", "0.2.0")
        update.unpack(archive, self.into)
        update.verify(self.into, "v0.2.0")
        self.assertEqual(sorted(str(p.relative_to(self.into)) for p in self.into.rglob("*")),
                         ["README.md", "bin", "bin/natter", "install.sh", "natter", "natter/__init__.py",
                          "natter/__main__.py"])
        self.assertEqual(stat.S_IMODE((self.into / "install.sh").stat().st_mode) & 0o111, 0o111 & ~_umask())
        self.assertEqual(stat.S_IMODE((self.into / "README.md").stat().st_mode) & 0o111, 0)

    def test_refused_members(self):
        top = "natter-0.2.0"
        for name, extra in [
            ("climbs out", [member(f"{top}/../x", data=b"x")]),
            ("absolute", [member("/abs", data=b"x")]),
            ("symlink", [member(f"{top}/link", tarfile.SYMTYPE, link="install.sh")]),
            ("symlink out", [member(f"{top}/out", tarfile.SYMTYPE, link="/etc/passwd")]),
            ("hard link", [member(f"{top}/hard", tarfile.LNKTYPE, link=f"{top}/install.sh")]),
            ("fifo", [member(f"{top}/fifo", tarfile.FIFOTYPE)]),
            ("device", [member(f"{top}/dev", tarfile.CHRTYPE)]),
            ("second top folder", [member("other/x", data=b"x")]),
            ("twice", [member(f"{top}/README.md", data=b"again")]),
        ]:
            with self.subTest(name=name):
                shutil.rmtree(self.into, ignore_errors=True)
                self.refused(release_archive(self.dir / "bad.tar.gz", "0.2.0", extra=extra))
                self.assertFalse((self.dir / "x").exists() or (self.dir / "abs").exists())

    def test_limits(self):
        archive = release_archive(self.dir / "big.tar.gz", "0.2.0")
        self.refused(archive, limit=40)
        shutil.rmtree(self.into)
        self.refused(archive, members=3)

    def test_not_an_archive(self):
        (self.dir / "page.tar.gz").write_bytes(b"<html>not found</html>")
        self.refused(self.dir / "page.tar.gz")
        whole = release_archive(self.dir / "whole.tar.gz", "0.2.0").read_bytes()
        (self.dir / "cut.tar.gz").write_bytes(whole[:len(whole) // 2])
        shutil.rmtree(self.into)
        self.refused(self.dir / "cut.tar.gz")

    def test_disk_full(self):
        archive = release_archive(self.dir / "good.tar.gz", "0.2.0")
        with mock.patch.object(shutil, "copyfileobj", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.refused(archive, "space")

    def test_not_a_whole_copy(self):
        update.unpack(release_archive(self.dir / "no-install.tar.gz", "0.2.0", skip=("install.sh",)), self.into)
        with self.assertRaises(update.Failed) as caught:
            update.verify(self.into, "v0.2.0")
        self.assertEqual((caught.exception.why, caught.exception.detail), ("archive", "it has no install.sh"))
        shutil.rmtree(self.into)
        update.unpack(release_archive(self.dir / "other.tar.gz", "0.1.0"), self.into)
        with self.assertRaises(update.Failed) as caught:
            update.verify(self.into, "v0.2.0")
        self.assertEqual(caught.exception.detail, "its version is 0.1.0")

    def test_a_header_is_never_read_past_the_limit(self):
        # tarfile reads a GNU long name (or a pax record) whole, before check_member sees its member:
        # the stream itself is capped, so a small archive can't make it read megabytes into memory.
        bomb = self.dir / "bomb.tar.gz"
        size = 3 << 20
        with gzip.open(bomb, "wb") as out:
            info = tarfile.TarInfo("././@LongLink")
            info.type, info.size = tarfile.GNUTYPE_LONGNAME, size
            out.write(info.tobuf(format=tarfile.GNU_FORMAT) + b"a" * size + b"\0" * (-size % 512))
            out.write(tarfile.TarInfo("ignored").tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * 1024)
        self.assertLess(bomb.stat().st_size, 64 << 10)
        read = []
        real = gzip.GzipFile.read

        def counted(unzipped, *size):
            data = real(unzipped, *size)
            read.append(len(data))
            return data

        with mock.patch.object(gzip.GzipFile, "read", counted):
            failed = self.refused(bomb, limit=1 << 20, members=10)
        self.assertIn("unpacked", failed.detail)
        self.assertLessEqual(sum(read), (1 << 20) + 10 * 2048 + 1)  # not the 3 MiB

    def test_one_version_only(self):
        # Python keeps the last value a module gives VERSION: a copy that gives it two says nothing sure.
        folder = make_copy(self.dir / "copy", "0.2.0")
        for more in ('VERSION = "0.0.1"\n', "from os import sep as VERSION\n", "def VERSION():\n    pass\n",
                     'VERSION += "1"\n', "from os import *\n", 'for VERSION in "x":\n    pass\n'):
            with self.subTest(more=more):
                (folder / "natter/__init__.py").write_text(f'VERSION = "0.2.0"\n{more}')
                self.assertIsNone(updater.version_in(folder))
                with self.assertRaises(update.Failed):
                    update.verify(folder, "v0.2.0")
        (folder / "natter/__init__.py").write_text('import os\n\nVERSION = "0.2.0"  # the release\nNAME = VERSION\n')
        self.assertEqual(updater.version_in(folder), "0.2.0")
        self.assertEqual(updater.version_in(ROOT), VERSION)  # this copy's own

    def test_check_member(self):
        good = tarfile.TarInfo("natter-0.2.0/natter/app.py")
        self.assertEqual(update.check_member(good), ("natter-0.2.0", "natter/app.py"))
        top = tarfile.TarInfo("natter-0.2.0/")
        top.type = tarfile.DIRTYPE
        self.assertEqual(update.check_member(top), ("natter-0.2.0", ""))
        for name in ("../x", "a/../../x", "/a", "a\\..\\b", "", "."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    update.check_member(tarfile.TarInfo(name))


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


class StagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stages = self.dir / "lib/natter/.update"
        self.archive = release_archive(self.dir / "release.tar.gz", "0.2.0").read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, transport, runs=None, deadline=10.0):
        found = []
        stager = update.Stager(transport, self.stages, sys.executable, deadline,
                               runs=runs or (lambda python, folder, done: done(None)))
        quietly(stager.start, "v0.2.0", found.append)
        quietly(spin, lambda: found, deadline + 5)
        self.assertEqual(len(found), 1)
        return found[0]

    def test_ready(self):
        codeload = "https://codeload.github.com/Officialbabs/natter/tar.gz/refs/tags/v0.2.0"
        (self.stages / "v0.1.0/old").mkdir(parents=True)  # an earlier update's leftovers
        transport = Transport((302, codeload, b"", ""), (200, "", self.archive, ""))
        self.assertIsNone(self.stage(transport))
        self.assertEqual([(m, u) for m, u, _h, _l in transport.asked],
                         [("GET", update.archive_url("v0.2.0")), ("GET", codeload)])
        self.assertEqual({h["User-Agent"] for _m, _u, h, _l in transport.asked}, {"natter-den"})
        self.assertEqual(transport.asked[0][3], update.ARCHIVE_BYTES)
        self.assertEqual(sorted(p.name for p in self.stages.iterdir()), ["v0.2.0"])
        self.assertEqual(sorted(p.name for p in (self.stages / "v0.2.0").iterdir()), ["source"])
        self.assertEqual(updater.version_in(self.stages / "v0.2.0/source"), "0.2.0")
        self.assertEqual(stat.S_IMODE(self.stages.stat().st_mode), 0o700)

    def test_redirects_stay_with_github(self):
        transport = Transport((302, "https://evil.example/natter.tar.gz", b"", ""), (200, "", self.archive, ""))
        self.assertEqual(self.stage(transport), ("download", "redirected away from GitHub"))
        self.assertEqual(len(transport.asked), 1)
        hop = (302, update.archive_url("v0.2.0"), b"", "")
        transport = Transport(hop, hop, hop, hop, (200, "", self.archive, ""))
        self.assertEqual(self.stage(transport), ("download", "too many redirects"))
        self.assertEqual(len(transport.asked), 4)

    def test_failures(self):
        self.assertEqual(self.stage(Transport((404, "", b"", ""))), ("archive", "the release has no archive"))
        self.assertEqual(self.stage(Transport((0, "", b"", "Network is unreachable"))),
                         ("download", "Network is unreachable"))
        self.assertEqual(self.stage(Transport((503, "", b"", ""))), ("download", "HTTP 503"))
        self.assertEqual(self.stage(Transport(), deadline=0.05)[0], "download")
        self.assertEqual(self.stage(Transport((200, "", b"<html>", "")))[0], "archive")
        wrong = release_archive(self.dir / "wrong.tar.gz", "0.1.0").read_bytes()
        self.assertEqual(self.stage(Transport((200, "", wrong, ""))), ("archive", "its version is 0.1.0"))

    def test_cant_run_here(self):
        found = self.stage(Transport((200, "", self.archive, "")),
                           runs=lambda python, folder, done: done("GTK 4 is 4.12; it needs 4.14 or newer"))
        self.assertEqual(found, ("cant-run", "GTK 4 is 4.12; it needs 4.14 or newer"))

    def test_runs_here_runs_the_new_copys_own_check(self):
        explain = ("import sys\ndef check():\n    print('Natter Den can\\'t start:', file=sys.stderr)\n"
                   "    print('  - WebKitGTK 6.0 for Python isn\\'t installed', file=sys.stderr)\n    return 1\n")
        for check, expected in (("def check():\n    return 0\n", None),
                                (explain, "WebKitGTK 6.0 for Python isn't installed")):
            with self.subTest(expected=expected):
                folder = make_copy(Path(tempfile.mkdtemp(dir=self.dir)), "0.2.0", check)
                found = []
                update.runs_here(sys.executable, folder, found.append)
                spin(lambda: found, 30)
                self.assertEqual(found, [expected])


# ---- Starting the updater ---------------------------------------------------------------------

class HelperStartTest(unittest.TestCase):
    def test_the_command(self):
        command = update.helper_command(Path("/c/update/updater.py"), Path("/c/update/plan.json"), Path("/home/me"),
                                        pid=4242, now=1800000000.5, python="/usr/bin/python3")
        self.assertEqual(command, ["systemd-run", "--user", "--quiet", "--collect",
                                   "--unit=natter-update-4242-1800000000", "--service-type=exec",
                                   "--property=StandardInput=null", "--working-directory=/home/me", "--",
                                   "/usr/bin/python3", "-I", "/c/update/updater.py", "/c/update/plan.json"])
        unit = command[4].split("=", 1)[1]
        self.assertFalse(unit.startswith("app-"))
        self.assertFalse(system.own_unit(unit + ".service"))  # Natter wouldn't stay in it
        self.assertFalse(any("RuntimeMaxSec" in word or "Max" in word for word in command))

    def start(self, helper_text, wait=1.0):
        folder = Path(tempfile.mkdtemp())
        try:
            helper, plan = folder / "updater.py", folder / "plan.json"
            helper.write_text(helper_text)
            plan.write_text("{}")
            found = []
            with mock.patch.object(update.shutil, "which", return_value=None):  # never systemd-run here
                quietly(update.start_helper, helper, plan, found.append, folder, wait)
                quietly(spin, lambda: found, wait + 10)
            return found, plan.exists(), sorted(p.name for p in folder.iterdir())
        finally:
            shutil.rmtree(folder)

    def test_quits_only_once_the_plan_is_taken(self):
        takes = "import os, sys\nos.rename(sys.argv[1], sys.argv[1] + '.taken')\n"
        self.assertEqual(self.start(takes), ([True], False, ["plan.json.taken", "updater.py"]))

    def test_an_updater_that_never_takes_it(self):
        self.assertEqual(self.start("import sys\nsys.exit(1)\n", wait=0.5), ([False], False, ["updater.py"]))

    def test_the_updater_is_isolated(self):
        # -I: a module planted beside the copy isn't imported in place of the library's.
        planted = "import os, sys, json\nos.rename(sys.argv[1], sys.argv[1] + '.taken')\n"
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "json.py").write_text("raise SystemExit('planted')\n")
            (folder / "updater.py").write_text(planted)
            (folder / "plan.json").write_text("{}")
            found = []
            with mock.patch.object(update.shutil, "which", return_value=None):
                quietly(update.start_helper, folder / "updater.py", folder / "plan.json", found.append, folder, 5.0)
                quietly(spin, lambda: found, 15)
            self.assertEqual(found, [True])
        finally:
            shutil.rmtree(folder)


class EnvironTest(unittest.TestCase):
    def test_program_environ_is_give_system_bus_as_a_dict(self):
        environ = {"PATH": "/usr/bin", system.SYSTEM_BUS: system.NO_SYSTEM_BUS,
                   gstfix.TRACERS: f"latency;{gstfix.TRACER}"}
        for before in (None, ("unix:path=/run/dbus/system_bus_socket",), (None,)):
            with self.subTest(before=before), mock.patch.dict(os.environ, environ), \
                    mock.patch.object(system, "_system_bus_before", before):
                launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
                system.give_system_bus(launcher)
                given = system.program_environ()
                for name in (system.SYSTEM_BUS, gstfix.TRACERS, "PATH"):
                    self.assertEqual(given.get(name), launcher.getenv(name), name)
                self.assertEqual(given[gstfix.TRACERS], "latency")
                # Natter's own environment is left as it is.
                self.assertEqual((os.environ[system.SYSTEM_BUS], os.environ[gstfix.TRACERS]),
                                 (system.NO_SYSTEM_BUS, f"latency;{gstfix.TRACER}"))

    def test_what_the_updater_passes_on(self):
        environ = {"PATH": "/usr/bin", "HOME": "/home/me", "GIO_LAUNCHED_DESKTOP_FILE": "/x.desktop",
                   "GIO_LAUNCHED_DESKTOP_FILE_PID": "12", "INVOCATION_ID": "abc", "JOURNAL_STREAM": "8:9",
                   "XDG_ACTIVATION_TOKEN": "t", "DESKTOP_STARTUP_ID": "s", "WAYLAND_DISPLAY": "wayland-0"}
        with mock.patch.object(system, "_system_bus_before", None):
            passed = update.helper_environ(environ)
        self.assertEqual(passed, {"PATH": "/usr/bin", "HOME": "/home/me", "WAYLAND_DISPLAY": "wayland-0"})


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual({key: DEFAULTS[key] for key in DEFAULTS if key.startswith("update_")},
                         {"update_check": True, "update_checked_at": 0, "update_latest": "", "update_notified": "",
                          "update_dismissed": ""})


class HelperFileTest(unittest.TestCase):
    def test_standard_library_only(self):
        tree = ast.parse((ROOT / "natter" / "updater.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative imports")
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        self.assertFalse({"natter", "gi"} & imported)

    def test_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run([sys.executable, "-I", str(ROOT / "natter" / "updater.py")], cwd=folder,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 2)

    def test_results_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.json"
            updater.write_result(path, {"tag": "v0.2.0", "from": "0.1.9", "to": "0.2.0", "state": "swapped", "at": 5,
                                        "helper": [12, 34]})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(updater.read_result(path), {"tag": "v0.2.0", "from": "0.1.9", "to": "0.2.0",
                                                         "state": "swapped", "why": "", "detail": "", "log": "",
                                                         "kept": "", "at": 5, "helper": [12, 34]})
            for bad in ({"tag": "v0.2.0", "state": "unknown"}, {"tag": "x", "state": "failed"}, [], "x"):
                path.write_text(json.dumps(bad))
                self.assertIsNone(updater.read_result(path))
            self.assertIsNone(updater.read_result(Path(folder) / "none.json"))


# ---- Natter's side ----------------------------------------------------------------------------

class Window:
    def __init__(self, visible=True):
        self.visible, self.said, self.toasts, self.account = visible, [], [], None

    def get_visible(self):
        return self.visible

    def say(self, text):
        self.said.append(text)

    def toast(self, *args, **kwargs):
        self.toasts.append((args, kwargs))


class Action:
    def __init__(self):
        self.enabled = None

    def set_enabled(self, enabled):
        self.enabled = enabled


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class UpdatesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.home = self.dir / "home"
        self.source = make_copy(self.home / ".local/lib/natter/source", VERSION)
        self.launcher = self.home / ".local/bin/natter"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text(f"#!{sys.executable}\nNATTER_DIR = {json.dumps(str(self.source))}\n")
        self.folder = self.home / ".cache/natter/update"
        self.runtime = self.dir / "run"
        self.runtime.mkdir(mode=0o700)
        self.clock, self.wall = Clock(), Clock(NOW)
        self.timers = mock.patch.object(GLib, "timeout_add_seconds", side_effect=lambda *a: self.later.append(a) or 1)
        self.later = []
        self.timers.start()
        self.addCleanup(self.timers.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, channel="get", transport=None, window=None, environ=None, **app):
        self.told, self.changes, self.withdrawn = [], [], []
        self.actions = {"update": Action(), "update-notes": Action()}
        self.app = SimpleNamespace(
            settings=Settings(self.dir / "settings.json"), window=window, accounts=[], calling=False, calls=None,
            locked=False, lookup_action=self.actions.get, replies={}, reply_drafts={}, banner_shown="",
            withdraw_notification=self.withdrawn.append,
            tell=lambda key, title, body="", button=None, **kw: self.told.append((key, title, body, button, kw)), **app)
        self.transport = Transport() if transport is None else transport

        def changed():
            self.changes.append(1)
            # As Natter._show_banner: the update's banner is up, unless paused notifications' is.
            shown = None if self.app.settings.notifications_paused() else self.app.updates.banner()
            self.app.banner_shown = shown[0] if shown else ""

        updates = update.Updates(self.app, changed, transport=self.transport, root=self.source,
                                 home=self.home, folder=self.folder,
                                 environ={"XDG_RUNTIME_DIR": str(self.runtime), "PATH": "/usr/bin:/bin",
                                          **(environ or {})},
                                 channel_name=channel, clock=self.clock, wall=self.wall)
        self.app.updates = updates
        return updates

    def found(self, tag=NEWER):
        return (200, "", release_json(tag), "")


class UpdatesTest(UpdatesCase):
    def test_an_answer_is_stored(self):
        updates = self.make(transport=Transport(self.found()))
        quietly(updates.check)
        self.assertEqual((self.app.settings["update_checked_at"], self.app.settings["update_latest"]), (int(NOW), NEWER))
        self.assertEqual(updates.latest, NEWER)
        self.assertTrue(self.changes)
        self.assertTrue(self.actions["update"].enabled)
        for answer in (self.found(SAME), self.found(OLDER), (404, "", b"", "")):
            with self.subTest(answer=answer[:2]):
                self.transport.answers.append(answer)
                self.wall.now += 10
                quietly(updates.check)
                self.assertEqual((self.app.settings["update_checked_at"], self.app.settings["update_latest"]),
                                 (int(self.wall.now), ""))
        self.assertFalse(self.actions["update"].enabled)

    def test_no_answer_changes_nothing_stored(self):
        updates = self.make(transport=Transport((0, "", b"", "offline"), (0, "", b"", "offline")))
        quietly(updates.check)
        self.assertEqual((self.app.settings["update_checked_at"], self.app.settings["update_latest"]), (0, ""))
        self.assertEqual((updates.failed_at, updates.check_failed), (self.clock.now, True))
        self.assertEqual(updates.status(), ("Couldn't check: GitHub didn't answer", "check"))
        self.assertNotIn("update_checked_at", self.app.settings.values)

    def test_the_timer_checks_once_a_day(self):
        updates = self.make(transport=Transport(self.found(), self.found()))
        updates.online = lambda: True
        updates.tick()
        self.assertEqual(self.transport.asked, [])  # start-up isn't ready
        updates.started()
        self.clock.now += update.FIRST_CHECK_DELAY - 1
        updates.tick()
        self.assertEqual(self.transport.asked, [])
        self.clock.now += 1
        quietly(updates.tick)
        self.assertEqual(len(self.transport.asked), 1)
        self.wall.now += 3600
        quietly(updates.tick)
        self.assertEqual(len(self.transport.asked), 1)
        self.wall.now += update.CHECK_EVERY
        quietly(updates.tick)
        self.assertEqual(len(self.transport.asked), 2)

    def test_the_timer_holds_off(self):
        for name, setup in [
            ("switch off", lambda u: self.app.settings.__setitem__("update_check", False)),
            ("offline", lambda u: setattr(u, "online", lambda: False)),
            ("a call in a page", lambda u: setattr(self.app, "calling", True)),
            ("the call window", lambda u: setattr(self.app, "calls", SimpleNamespace(in_call=True, ringing=False))),
            ("ringing in the call window", lambda u: setattr(self.app, "calls",
                                                             SimpleNamespace(in_call=False, ringing=True))),
            ("a page loading", lambda u: self.app.accounts.append(
                SimpleNamespace(ready=False, needs_link=False, failed=None, crashing=False, loaded_at=self.clock.now))),
            ("linking", lambda u: setattr(self.app, "window", SimpleNamespace(
                get_visible=lambda: True, account=SimpleNamespace(needs_link=True), say=lambda t: None))),
        ]:
            with self.subTest(name):
                updates = self.make(transport=Transport(self.found()))
                updates.online = lambda: True
                updates.started()
                self.clock.now += update.FIRST_CHECK_DELAY
                setup(updates)
                updates.tick()
                self.assertEqual(self.transport.asked, [])

    def test_retried_an_hour_after_no_answer(self):
        updates = self.make(transport=Transport((0, "", b"", "x"), (0, "", b"", "x"), self.found()))
        updates.online = lambda: True
        updates.started()
        self.clock.now += update.FIRST_CHECK_DELAY
        quietly(updates.tick)
        self.clock.now += update.RETRY_AFTER - 60
        quietly(updates.tick)
        self.assertEqual(len(self.transport.asked), 2)  # the API and the page, once
        self.clock.now += 60
        quietly(updates.tick)
        self.assertEqual(updates.latest, NEWER)

    def test_channels_that_never_check_show_nothing(self):
        updates = self.make("off")
        self.app.settings["update_latest"] = NEWER  # stored before the build turned checks off
        self.assertEqual((updates.latest, updates.banner(), updates.menu_item()), ("", None, None))
        updates.tick()
        self.assertEqual(self.told, [])

    def test_channels_that_never_check(self):
        for channel, environ in (("off", {}), ("lab", {}), ("lab", {"NATTER_UPDATE_CHECK": "0",
                                                                    "NATTER_UPDATE_FEED": "file:///nonexistent"}),
                                 ("get", {"NATTER_UPDATE_CHECK": "0"})):
            with self.subTest(channel=channel, environ=environ):
                updates = self.make(channel, transport=Transport(self.found()), environ=environ)
                updates.online = lambda: True
                updates.started()
                self.clock.now += update.FIRST_CHECK_DELAY
                updates.tick()
                updates.check_now()
                self.assertEqual(self.transport.asked, [])
                self.assertFalse(updates.checks)

    def test_a_labs_feed(self):
        feed = self.dir / "latest.json"
        feed.write_bytes(release_json(NEWER))
        updates = self.make("lab", environ={"NATTER_UPDATE_FEED": f"file://{feed}"})
        updates.online = lambda: True
        updates.started()
        self.clock.now += update.FIRST_CHECK_DELAY
        quietly(updates.tick)
        self.assertEqual((updates.latest, self.transport.asked), (NEWER, []))
        self.assertFalse(updates.can_update)

    def test_check_now(self):
        updates = self.make(transport=Transport(self.found(), self.found(), self.found()))
        updates.check_now()
        self.assertEqual(self.transport.asked, [])  # not before start-up is ready
        updates.started()
        self.app.settings["update_check"] = False  # the switch doesn't hold it, nor the day
        quietly(updates.check_now)
        quietly(updates.check_now)
        self.assertEqual(len(self.transport.asked), 1)
        self.clock.now += update.CHECK_NOW_GAP
        quietly(updates.check_now)
        self.assertEqual(len(self.transport.asked), 2)

    def test_told_once_per_version(self):
        updates = self.make(window=Window(visible=False), transport=Transport(self.found()))
        quietly(updates.check)
        self.assertEqual(self.told, [("update", f"{NAME} {NEWER[1:]} is available",
                                      f"{NAME} can update itself, usually within a minute.",
                                      ("Update", "app.update", None), {})])
        updates.tick()
        self.assertEqual(len(self.told), 1)
        self.assertEqual(self.app.settings["update_notified"], NEWER)

    def test_a_visible_window_tells_it(self):
        window = Window()
        updates = self.make(window=window, transport=Transport(self.found()))
        quietly(updates.check)
        self.assertEqual((self.told, window.said), ([], [f"{NAME} {NEWER[1:]} is available"]))
        self.assertEqual(self.app.settings["update_notified"], NEWER)
        self.app.locked = True  # out of sight behind the lock: a notification, for the next version
        self.transport.answers.append(self.found(NEWEST))
        quietly(updates.check)
        self.assertEqual([told[0] for told in self.told], ["update"])

    def test_paused_notifications_wait(self):
        updates = self.make(window=Window(visible=False), transport=Transport(self.found()))
        self.app.settings["dnd_until"] = -1
        quietly(updates.check)
        self.assertEqual(self.told, [])
        self.app.settings["dnd_until"] = 0
        updates.tick()
        self.assertEqual(len(self.told), 1)

    def test_surfaces(self):
        updates = self.make(transport=Transport(self.found()))
        self.assertEqual((updates.banner(), updates.menu_item()), (None, None))
        self.assertEqual(updates.status(), ("Not checked yet", "check"))
        quietly(updates.check)
        version = NEWER[1:]
        self.assertEqual(updates.banner(), (f"{NAME} {version} is available", "Update", "app.update", False))
        self.assertEqual(updates.menu_item(), f"Update to {NAME} {version}…")
        self.assertEqual(updates.status(), (f"{version} is available", "update"))
        self.app.locked = True
        self.assertIsNone(updates.menu_item())
        self.app.locked = False
        updates.later()
        self.assertIsNone(updates.banner())  # this session
        self.assertIsNotNone(updates.menu_item())
        self.transport.answers.append(self.found(NEWEST))
        quietly(updates.check)
        self.assertEqual(updates.banner()[0], f"{NAME} {NEWEST[1:]} is available")  # a newer one shows again
        updates.dismiss()
        self.assertEqual((updates.banner(), updates.menu_item()), (None, None))
        self.assertEqual(updates.status(), (f"{NEWEST[1:]} is available", "update"))  # Preferences still says
        self.assertTrue(updates.quiet)
        self.assertEqual(self.app.settings["update_dismissed"], NEWEST)

    def test_other_channels_say_details(self):
        updates = self.make("checkout", transport=Transport(self.found()))
        quietly(updates.check)
        self.assertEqual(updates.banner()[1], "Details")
        self.assertEqual(updates.menu_item(), f"{NAME} {NEWER[1:]} Is Available…")
        self.assertEqual(updates.status()[1], "details")
        done = []
        updates.prepare(done.append)
        self.assertEqual(done, [False])  # only get.sh's copy updates itself

    def test_status_while_checking_and_after(self):
        updates = self.make(transport=Transport())  # never answers
        updates.started()
        quietly(updates.check_now)
        self.assertEqual(updates.status(), ("Checking…", "check"))  # the button the focus is on stays
        updates.stop()
        self.app.settings["update_checked_at"] = int(NOW - 60)
        self.assertEqual(updates.status(), (f"Up to date, checked {update.checked_words(NOW - 60, NOW)}", "check"))

    def test_debug_line(self):
        updates = self.make()
        self.assertEqual(updates.debug_line(), "Updates: get.sh copy, updates in place, checks on, newest none found, "
                                               "checked never")
        self.app.settings["update_checked_at"] = int(NOW)
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_check"] = False
        self.assertEqual(updates.debug_line(), f"Updates: get.sh copy, updates in place, checks off, newest {NEWER}, "
                                               f"checked {time.strftime('%Y-%m-%d', time.localtime(NOW))}")
        self.assertIn("checks off", self.make("off").debug_line())

    def test_actions_follow_from_the_start(self):
        self.make()
        self.assertEqual((self.actions["update"].enabled, self.actions["update-notes"].enabled), (False, False))
        self.app.settings["update_latest"] = NEWER
        self.make()
        self.assertTrue(self.actions["update"].enabled)

    def ready_to_check(self, updates):
        updates.online = lambda: True
        updates.started()
        self.clock.now += update.FIRST_CHECK_DELAY

    def test_the_environment_switch_keeps_the_channel(self):
        # NATTER_UPDATE_CHECK=0 stops checks; it doesn't make get.sh's copy a system package.
        updates = self.make(None, transport=Transport(self.found()), environ={"NATTER_UPDATE_CHECK": "0"})
        self.assertEqual((updates.channel, updates.checks), ("get", False))
        self.assertEqual(updates.status(), ("Checks are off (NATTER_UPDATE_CHECK=0)", ""))
        self.assertTrue(updates.debug_line().startswith("Updates: get.sh copy, updates in place, checks off"))
        self.ready_to_check(updates)
        updates.tick()
        updates.check_now()
        self.assertEqual(self.transport.asked, [])
        self.assertEqual(self.make(None).channel, "get")

    def test_turned_off_the_daily_check_under_way_stops(self):
        transport = Held()
        updates = self.make(transport=transport, window=Window(visible=False))
        self.ready_to_check(updates)
        quietly(updates.tick)
        self.app.settings["update_check"] = False  # the app's _on_setting then calls changed()
        quietly(updates.changed)
        self.assertIsNone(updates.checker)
        quietly(transport.answer, 403, "", b"", "")  # rate-limited: the Releases page isn't asked after all
        self.assertEqual([method for method, _url in transport.asked], ["GET"])
        self.assertEqual((self.app.settings["update_latest"], self.told), ("", []))
        self.assertEqual(updates.status(), ("Not checked yet", "check"))

    def test_turned_off_what_was_found_shows_in_preferences_only(self):
        updates = self.make(window=Window(visible=False))
        self.app.settings["update_latest"] = NEWER  # found while notifications were paused, say
        self.app.settings["update_check"] = False
        updates.changed()  # as the app's _on_setting does
        quietly(updates.tick)
        self.assertEqual((updates.banner(), updates.menu_item(), self.told), (None, None, []))
        self.assertEqual(updates.status(), (f"{NEWER[1:]} is available", "update"))
        self.assertTrue(self.actions["update"].enabled)  # Preferences' Update… still opens the dialog
        updates._failed("download", NEWER)  # one under way or failed still shows
        self.assertEqual(updates.banner()[0], f"Couldn't update to {NAME} {NEWER[1:]}")

    def test_no_answer_again_and_again_is_asked_less_often(self):
        never = (0, "", b"", "x")
        updates = self.make(transport=Transport(*[never] * 20, self.found()))
        self.ready_to_check(updates)
        waits = []
        for _ in range(7):
            quietly(updates.tick)
            asked = len(self.transport.asked)
            for minutes in range(1, 49 * 60):
                self.clock.now += 60
                quietly(updates.tick)
                if len(self.transport.asked) > asked:
                    waits.append(minutes // 60)
                    break
        self.assertEqual(waits, [1, 2, 4, 8, 16, 24, 24])  # hours: a day at most
        self.transport.answers[:] = [self.found(), (0, "", b"", "x"), (0, "", b"", "x")]
        self.clock.now += update.CHECK_EVERY
        quietly(updates.tick)
        self.assertEqual((updates.misses, updates.retry), (0, update.RETRY_AFTER))  # an answer starts again

    def test_check_now_says_when_its_too_soon(self):
        window = Window()
        updates = self.make(window=window, transport=Transport())  # never answers
        updates.check_now()
        self.assertEqual(window.toasts, [(("Try again in a minute",), {})])  # start-up isn't ready
        updates.started()
        quietly(updates.check_now)
        updates.check_now()  # while checking: Checking… says it
        self.assertEqual(len(window.toasts), 1)
        updates.stop()
        updates.check_now()
        self.assertEqual(window.toasts[-1], (("Try again in a minute",), {}))
        self.assertEqual(len(self.transport.asked), 1)

    def test_told_once_its_banner_is_up(self):
        # Notifications paused until turned on again: their banner comes first, so the release isn't
        # counted as told (nor read out) until its own banner is up.
        window = Window()
        updates = self.make(window=window, transport=Transport(self.found()))
        self.app.settings["dnd_until"] = -1
        quietly(updates.check)
        self.assertEqual((window.said, self.app.settings["update_notified"], self.told), ([], "", []))
        self.app.settings["dnd_until"] = 0
        updates.changed()  # as the app's _on_setting does
        updates.tick()
        self.assertEqual((window.said, self.app.settings["update_notified"]),
                         ([f"{NAME} {NEWER[1:]} is available"], NEWER))

    def test_a_page_that_never_gets_ready_doesnt_stop_checks(self):
        updates = self.make(transport=Transport(self.found()))
        self.app.accounts.append(SimpleNamespace(ready=False, needs_link=False, failed=None, crashing=False,
                                                 loaded_at=self.clock.now))
        self.ready_to_check(updates)
        updates.tick()
        self.assertEqual(self.transport.asked, [])  # loading, as far as anyone can tell
        self.clock.now += update.LOAD_HOLD
        quietly(updates.tick)
        self.assertEqual(updates.latest, NEWER)


class AfterUpdateTest(UpdatesCase):
    def result(self, **fields):
        self.folder.mkdir(parents=True, exist_ok=True)
        updater.write_result(self.folder / "result.json", {"tag": NEWER, "from": "0.0.9", "to": VERSION,
                                                           "state": "installed", "why": "", "detail": "",
                                                           "at": 1, **fields})

    def read(self):
        return updater.read_result(self.folder / "result.json")

    def test_updated(self):
        window = Window()
        updates = self.make(window=window)
        self.app.settings["update_latest"] = SAME  # found before the update: now it's this one
        self.result(tag=SAME)
        written = []
        real = updater.write_result
        with mock.patch.object(update, "write_result", lambda path, result: written.append(result["state"])
                               or real(path, result)):
            quietly(updates.started)
        self.assertEqual(written, ["started", "told"])  # "started" first: the updater waits for it
        self.assertEqual(self.read()["state"], "told")
        self.assertEqual(window.toasts, [((f"Updated to {NAME} {VERSION}", "What's New", "app.update-notes"),
                                          {"timeout": 8})])
        self.assertTrue(self.actions["update-notes"].enabled)
        self.assertEqual(self.app.settings["update_latest"], "")
        self.assertEqual([delay for delay, *_ in self.later], [update.CLEANUP_DELAY])

    def test_updated_out_of_sight(self):
        updates = self.make(window=Window(visible=False))
        self.result()
        quietly(updates.started)
        self.assertEqual(self.told, [("updated", f"Updated to {NAME} {VERSION}", "",
                                      ("What's New", "app.update-notes", None), {})])

    def test_failed(self):
        updates = self.make(window=Window(visible=False))
        self.result(state="failed", why="install", detail="exit status 1", **{"from": VERSION, "to": NEWER[1:]})
        quietly(updates.started)
        self.assertEqual(updates.failure["why"], "install")
        self.assertFalse(updates.failure["before"])
        self.assertEqual(updates.banner(), (f"Couldn't update to {NAME} {NEWER[1:]}", "Details", "app.update", True))
        self.assertEqual(self.told, [("update-failed", f"Couldn't update to {NAME} {NEWER[1:]}",
                                      f"{NAME} {VERSION} is still installed.", ("Details", "app.update", None), {})])
        self.assertEqual([delay for delay, *_ in self.later], [update.CLEANUP_DELAY])  # the old copy is back
        self.assertTrue(self.actions["update"].enabled)
        updates.told()
        self.assertEqual((updates.failure, self.read()["state"]), (None, "told"))
        self.assertEqual(self.withdrawn, ["update-failed"])  # its notification goes with it

    def test_interrupted(self):
        # An updater gone without a word: how far it got is told by the files, and the words claim
        # nothing either way. A plan no updater took: the updater never started.
        for fields, why in (({"state": "planned", "helper": [2 ** 22 + 1, 5]}, "unfinished"),
                            ({"state": "swapped", "helper": [2 ** 22 + 1, 5]}, "unfinished"),
                            ({"state": "planned"}, "helper")):
            with self.subTest(fields=fields):
                updates = self.make(window=Window())
                self.result(**fields)
                quietly(updates.started)
                self.assertEqual(updates.failure["why"], why)
                self.assertEqual(self.app.window.said, [f"Couldn't update to {NAME} {NEWER[1:]}"])

    def test_interrupted_is_told_once(self):
        for state in ("planned", "swapped"):
            with self.subTest(state=state):
                updates = self.make(window=Window())
                self.result(state=state, helper=[2 ** 22 + 1, 5])
                quietly(updates.started)
                updates.told()  # its dialog was shown
                found = self.read()
                self.assertEqual((found["state"], found["why"]), ("told", "unfinished"))  # kept: its stage stays
                updates = self.make(window=Window())  # the next start
                quietly(updates.started)
                self.assertEqual((updates.failure, self.app.window.said, self.later), (None, [], []))

    def test_what_the_notice_says_is_true(self):
        # Out of sight: "… is still installed" only when this is that version and nothing was left aside.
        for fields, body in (({"from": VERSION, "to": NEWER[1:]}, f"{NAME} {VERSION} is still installed."),
                             ({"from": VERSION, "to": NEWER[1:], "kept": "~/.local/lib/natter/.update/x/old"},
                              f"{NAME} {NEWER[1:]} didn't start (exit status 1)."),
                             ({"from": "0.0.9", "to": VERSION}, f"{NAME} {VERSION} didn't start (exit status 1).")):
            with self.subTest(fields=fields):
                updates = self.make(window=Window(visible=False))
                tag = "v" + fields["to"]
                self.result(**{"tag": tag, "state": "failed", "why": "start", "detail": "exit status 1", **fields})
                quietly(updates.started)
                self.assertEqual(self.told[-1][1:3], (f"Couldn't update to {NAME} {fields['to']}", body))

    def test_paused_notifications_wait_after_an_update(self):
        for fields, key in (({}, "updated"), ({"state": "failed", "why": "install", "from": VERSION, "to": NEWER[1:]},
                                              "update-failed")):
            with self.subTest(key=key):
                updates = self.make(window=Window(visible=False))
                self.app.settings["dnd_until"] = -1  # Do Not Disturb: "no notifications from Natter Den"
                self.result(**fields)
                quietly(updates.started)
                self.assertEqual(self.told, [])
                updates.tick()
                self.assertEqual(self.told, [])
                self.app.settings["dnd_until"] = 0
                updates.tick()
                self.assertEqual([told[0] for told in self.told], [key])
                updates.tick()
                self.assertEqual(len(self.told), 1)  # once

    def test_an_updater_still_at_work(self):
        updates = self.make()
        self.result(state="planned", helper=[os.getpid(), updater.start_ticks(os.getpid())])
        quietly(updates.started)
        self.assertIsNone(updates.failure)

    def test_a_stage_left_behind_goes(self):
        updates = self.make()
        self.result(state="told")
        (updates.stages / NEWER / "old").mkdir(parents=True)
        updates.started()
        self.assertEqual([delay for delay, *_ in self.later], [update.CLEANUP_DELAY])
        self.later[0][1]()
        self.assertTrue(spin(lambda: not updates.stages.exists(), 10))

    def test_a_failures_stage_is_kept(self):
        updates = self.make()
        self.result(state="told", why="unfinished")
        (updates.stages / NEWER / "old").mkdir(parents=True)
        updates.started()
        self.assertEqual(self.later, [])

    def test_nothing_to_say(self):
        for fields in ({"state": "told"}, {"to": "0.0.1"}):
            with self.subTest(fields=fields):
                updates = self.make(window=Window())
                self.result(**fields)
                quietly(updates.started)
                self.assertEqual((updates.failure, self.app.window.toasts, self.told), (None, [], []))
        updates = self.make()
        updates.started()
        updates.started()  # once
        self.assertIsNone(updates.failure)


class Widget:
    """Stands in for a GTK or libadwaita widget: keeps what it was made with, what went in it,
    its responses and what was connected to it."""

    def __init__(self, kind, *args, **props):
        self.kind, self.args, self.props = kind, args, props
        self.children, self.responses, self.handlers, self.calls, self.extra = [], [], {}, [], None

    def append(self, child):
        self.children.append(child)

    def set_extra_child(self, child):
        self.extra = child

    def add_response(self, response, label):
        self.responses.append((response, label))

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, *args))

    def labels(self):
        """The text of every label in it, depth first."""
        found = [self.props["label"]] if "label" in self.props else []
        for child in self.children + ([self.extra] if self.extra else []):
            found += child.labels()
        return found


class Kind:
    """A widget class (made into a Widget when called) or an enum (its values are names)."""

    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **props):
        return Widget(self.name, *args, **props)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return f"{self.name}.{name}"


class Module:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Kind(f"{self.name}.{name}")


class DialogTest(UpdatesCase):
    def setUp(self):
        super().setUp()
        self.copied = []
        clipboard = SimpleNamespace(set=self.copied.append)
        stand_ins = SimpleNamespace(Adw=Module("Adw"), Gtk=Module("Gtk"), Pango=Module("Pango"),
                                    Gdk=SimpleNamespace(Display=SimpleNamespace(
                                        get_default=lambda: SimpleNamespace(get_clipboard=lambda: clipboard))),
                                    icon_button=lambda icon, tooltip: Widget("icon_button", icon, tooltip))
        patch = mock.patch.object(update, "widgets", lambda: stand_ins)
        patch.start()
        self.addCleanup(patch.stop)

    def make(self, channel="get", **kwargs):
        updates = super().make(channel, **kwargs)
        self.app.update_now = lambda warned=False: self.told.append(("update_now", warned))
        self.app.settings["update_latest"] = NEWER
        return updates

    def test_get_asks(self):
        updates = self.make()
        alert = update.dialog_for(self.app)
        self.assertEqual(alert.props, {"heading": f"Update to {NAME} {NEWER[1:]}?",
                                       "body": f"{NAME} closes, updates itself and opens again, usually within a "
                                               "minute. Your chats and settings stay as they are."})
        self.assertEqual(alert.responses, [("skip", "_Skip This Version"), ("later", "_Later"), ("update", "_Update")])
        self.assertIn(("set_response_appearance", "update", "Adw.ResponseAppearance.SUGGESTED"), alert.calls)
        self.assertIn(("set_default_response", "later"), alert.calls)
        self.assertIn(("set_close_response", "later"), alert.calls)
        self.assertEqual((alert.extra.kind, alert.extra.props["label"], alert.extra.props["uri"]),
                         ("Gtk.LinkButton", f"What's New in {NEWER[1:]}", update.release_url(NEWER)))
        answer = alert.handlers["response"]
        answer(alert, "update")
        self.assertEqual(self.told, [("update_now", False)])  # warned of no replies: asked before quitting
        answer(alert, "later")
        self.assertIsNone(updates.banner())  # for this session
        answer(alert, "skip")
        self.assertEqual(self.app.settings["update_dismissed"], NEWER)

    def test_get_warns_of_unsent_replies(self):
        self.make()
        self.app.reply_drafts = {("main", "Ana"): "Half a thought"}
        self.assertTrue(update.dialog_for(self.app).props["body"].endswith(" Replies you haven't sent will be lost."))
        self.app.reply_drafts = {}
        self.app.replies = {("main", "Ana"): SimpleNamespace(unsent=lambda: True)}
        alert = update.dialog_for(self.app)
        self.assertTrue(alert.props["body"].endswith(" Replies you haven't sent will be lost."))
        alert.handlers["response"](alert, "update")
        self.assertEqual(self.told[-1], ("update_now", True))  # warned: Update needn't ask again

    def test_replies_written_since_are_asked_about(self):
        self.make()
        alert = update.replies_dialog(self.app)
        self.assertEqual(alert.props, {"heading": f"Update {NAME}?", "body": "Replies you haven't sent will be lost."})
        self.assertEqual(alert.responses, [("cancel", "_Cancel"), ("update", "_Update")])
        self.assertIn(("set_close_response", "cancel"), alert.calls)
        alert.handlers["response"](alert, "cancel")
        self.assertEqual(self.told, [])
        alert.handlers["response"](alert, "update")
        self.assertEqual(self.told, [("update_now", True)])

    def test_others_say_how(self):
        for channel, command in (("checkout", f"cd {self.source} && git pull && ./install.sh"),
                                 ("other", update.GET_COMMAND), ("flatpak", update.GET_FLATPAK_COMMAND),
                                 ("package", None)):
            with self.subTest(channel=channel):
                self.make(channel)
                alert = update.dialog_for(self.app)
                self.assertEqual(alert.props, {"heading": f"{NAME} {NEWER[1:]} Is Available", "body": update.HOW[channel]})
                self.assertEqual(alert.responses, [("skip", "_Skip This Version"), ("close", "_Close")])
                labels = alert.extra.labels()
                self.assertEqual(labels, ([command] if command else []) + [f"What's New in {NEWER[1:]}"])
                if command:
                    row = alert.extra.children[0]
                    copy = row.children[1]
                    self.assertEqual(copy.args, ("edit-copy-symbolic", "Copy Command"))
                    copy.handlers["clicked"](copy)
                    self.assertEqual(self.copied[-1], command)

    def test_failure(self):
        updates = self.make(window=Window())
        self.folder.mkdir(parents=True)
        updater.write_result(self.folder / "result.json",
                             {"tag": NEWER, "from": VERSION, "to": NEWER[1:], "state": "failed", "why": "busy"})
        quietly(updates.started)
        alert = update.dialog_for(self.app)
        self.assertEqual(alert.props["heading"], f"Couldn't Update to {NAME} {NEWER[1:]}")
        self.assertEqual(alert.props["body"],
                         f"{NAME} didn't close in time, so nothing was changed. {NAME} {VERSION} is still installed. "
                         "The log is in " + update.home_path(self.folder / "update.log") + ".")
        self.assertEqual(alert.responses, [("close", "_Close"), ("retry", "_Try Again")])
        self.assertEqual(alert.extra.labels(), [f"To update by hand, quit {NAME} and run:", update.GET_COMMAND])
        # Shown, it's told: the banner says what's available again.
        self.assertIsNone(updates.failure)
        self.assertEqual(updater.read_result(self.folder / "result.json")["state"], "told")
        self.assertEqual(updates.banner()[0], f"{NAME} {NEWER[1:]} is available")
        alert.handlers["response"](alert, "close")
        self.assertIsNotNone(updates.banner())  # Close isn't Later
        alert.handlers["response"](alert, "retry")
        self.assertEqual(self.told[-1], ("update_now", False))  # it warned of no replies

    def test_failure_words_say_whats_installed(self):
        # The rollback couldn't put the old copy back (kept), or this is the new version: then
        # nothing says the old one is still installed.
        for fields in ({"kept": "~/.local/lib/natter/.update/v9/old"}, {"from": "0.0.9", "to": VERSION, "tag": SAME}):
            with self.subTest(fields=fields):
                updates = self.make(window=Window())
                self.folder.mkdir(parents=True, exist_ok=True)
                updater.write_result(self.folder / "result.json",
                                     {"tag": NEWER, "from": VERSION, "to": NEWER[1:], "state": "failed", "why": "start",
                                      **fields})
                quietly(updates.started)
                body = update.dialog_for(self.app).props["body"]
                self.assertNotIn("is still installed", body)
                self.assertIn("didn't start.", body)

    def test_failure_before_quitting(self):
        updates = self.make(window=Window())
        updates._failed("cant-run", NEWER, "GTK 4 is 4.10; it needs 4.14")
        alert = update.dialog_for(self.app)
        self.assertEqual(alert.props["body"], f"{NAME} {NEWER[1:]} can't run on this system as it is: GTK 4 is 4.10; "
                                              "it needs 4.14. Nothing was changed.")
        self.assertEqual(alert.responses, [("close", "_Close")])  # trying again won't help

    def test_nothing_to_show(self):
        self.make()
        self.app.settings["update_latest"] = ""
        self.assertIsNone(update.dialog_for(self.app))


class PrepareTest(UpdatesCase):
    def setUp(self):
        super().setUp()
        self.archive = release_archive(self.dir / "release.tar.gz", NEWER[1:]).read_bytes()

    def prepared(self, updates):
        done = []
        quietly(updates.prepare, done.append)
        quietly(spin, lambda: done, 30)
        return done

    def test_prepared_and_started(self):
        updates = self.make(transport=Transport(self.found(), (200, "", self.archive, "")))
        self.app.settings["update_latest"] = NEWER  # told an hour ago: checked again first
        self.app.settings["update_checked_at"] = int(NOW - 3600)
        self.assertEqual(self.prepared(updates), [True])
        self.assertEqual([url for _m, url, _h, _l in self.transport.asked],
                         [update.LATEST_API, update.archive_url(NEWER)])
        self.assertEqual(updates.banner(), (f"Downloading {NAME} {NEWER[1:]}…", "", "", True))
        self.assertTrue(updates.working)
        started = []
        with mock.patch.object(update, "start_helper", lambda helper, plan, done, home: started.append(
                (helper, plan, home)) or done(True)):
            done = []
            quietly(updates.launch, False, done.append)
        self.assertEqual(done, [True])
        helper, plan_file, home = started[0]
        self.assertEqual((helper, plan_file, home), (self.folder / "updater.py", self.folder / "plan.json", self.home))
        self.assertEqual(helper.read_text(), (ROOT / "natter" / "updater.py").read_text())
        for path in (helper, plan_file, self.folder / "result.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        plan = json.loads(plan_file.read_text())
        self.assertEqual(updater.plan_problem(plan, str(plan_file), str(self.home)), "")
        self.assertEqual((plan["tag"], plan["from"], plan["to"], plan["args"], plan["pid"]),
                         (NEWER, VERSION, NEWER[1:], [], os.getpid()))
        self.assertEqual(plan["lock"], str(self.runtime / "natter-io.github.Officialbabs.Natter.lock"))
        self.assertEqual(self.read_state(), "planned")

    def read_state(self):
        return updater.read_result(self.folder / "result.json")["state"]

    def test_the_newest_of_told_and_found(self):
        archive = release_archive(self.dir / "newest.tar.gz", NEWEST[1:]).read_bytes()
        updates = self.make(transport=Transport(self.found(NEWEST), (200, "", archive, "")))
        self.app.settings["update_latest"] = NEWER
        self.assertEqual(self.prepared(updates), [True])
        self.assertEqual(updates.target, NEWEST)
        self.assertEqual(self.transport.asked[1][1], update.archive_url(NEWEST))

    def test_a_release_no_longer_offered_isnt_installed(self):
        # Told NEWEST; at the click GitHub's latest is this version, or NEWEST made a pre-release,
        # or an older one than told: nothing is downloaded, and what's still out is offered instead.
        for answer, latest in ((self.found(SAME), ""), ((200, "", release_json(NEWEST, prerelease=True), ""), ""),
                               (self.found(NEWER), NEWER)):
            with self.subTest(latest=latest, answer=answer[2][:60]):
                window = Window()
                updates = self.make(window=window, transport=Transport(answer, (200, "", self.archive, "")))
                self.app.settings["update_latest"] = NEWEST
                self.app.settings["update_checked_at"] = int(NOW - 3600)  # told an hour ago: checked again first
                self.assertEqual(self.prepared(updates), [False])
                self.assertEqual(len(self.transport.asked), 1)  # the check, no download
                self.assertEqual((updates.working, updates.failure, updates.latest), (False, None, latest))
                self.assertEqual(window.toasts, [((f"{NAME} {NEWEST[1:]} is no longer available",), {})])

    def test_fresh_enough_isnt_asked_again(self):
        updates = self.make(transport=Transport((200, "", self.archive, "")))
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW - 60)
        self.assertEqual(self.prepared(updates), [True])
        self.assertEqual(len(self.transport.asked), 1)

    def test_failures_change_nothing(self):
        for answers, why in ((((0, "", b"", "x"), (0, "", b"", "x")), "check"), (((404, "", b"", ""),), "archive"),
                             (((0, "", b"", "reset"),), "download")):
            with self.subTest(why=why):
                updates = self.make(transport=Transport(*answers))
                self.app.settings["update_latest"] = NEWER
                self.app.settings["update_checked_at"] = int(NOW - (60 if why != "check" else 3600))
                self.assertEqual(self.prepared(updates), [False])
                self.assertEqual((updates.failure["why"], updates.failure["before"], updates.working), (why, True, False))
                self.assertEqual(updater.version_in(self.source), VERSION)
                self.assertEqual(updates.banner()[0], f"Couldn't update to {NAME} {NEWER[1:]}")
                with mock.patch.object(update, "start_helper", lambda *a: self.fail("started")):
                    done = []
                    updates.launch(False, done.append)
                    self.assertEqual(done, [False])

    def test_cant_run_here(self):
        failing = "import sys\ndef check():\n    print('  - GTK 4 is 4.10; it needs 4.14', file=sys.stderr)\n    return 1\n"
        archive = release_archive(self.dir / "old-gtk.tar.gz", NEWER[1:], check=failing).read_bytes()
        updates = self.make(transport=Transport((200, "", archive, "")))
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW)
        self.assertEqual(self.prepared(updates), [False])
        self.assertEqual((updates.failure["why"], updates.failure["detail"]), ("cant-run", "GTK 4 is 4.10; it needs 4.14"))

    def test_held_then_reused(self):
        updates = self.make(transport=Transport((200, "", self.archive, "")))
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW)
        self.assertEqual(self.prepared(updates), [True])
        updates.hold()  # a call came in meanwhile
        self.assertEqual(updates.banner()[1], "Update")
        self.assertFalse(updates.working)
        self.assertEqual(self.prepared(updates), [True])
        self.assertEqual(len(self.transport.asked), 1)  # the stage was used again

    def test_the_updater_doesnt_start(self):
        updates = self.make(transport=Transport((200, "", self.archive, "")))
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW)
        self.assertEqual(self.prepared(updates), [True])
        (self.folder).mkdir(parents=True, exist_ok=True)
        (self.folder / "update.log").write_text("the last one\n")
        with mock.patch.object(update, "start_helper", lambda helper, plan, done, home: done(False)):
            done = []
            quietly(updates.launch, True, done.append)
        self.assertEqual(done, [False])
        self.assertEqual((updates.failure["why"], updates.failure["before"]), ("helper", True))
        self.assertEqual(self.read_state(), "failed")
        self.assertEqual((self.folder / "update.log.1").read_text(), "the last one\n")
        self.assertEqual(json.loads((self.folder / "plan.json").read_text())["args"], ["--background"])

    def test_a_failure_out_of_sight_is_notified(self):
        updates = self.make(window=Window(visible=False), transport=Transport((0, "", b"", "reset")))
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW)
        self.assertEqual(self.prepared(updates), [False])
        self.assertEqual(self.told[-1], ("update-failed", f"Couldn't update to {NAME} {NEWER[1:]}",
                                         "The download didn't finish. Check your connection and try again. "
                                         "Nothing was changed.", ("Details", "app.update", None), {}))

    def launched(self, updates, answer=True):
        """Prepared, then launched with a stand-in for the updater that takes the plan (`answer`
        True), never does (None), or doesn't start (False)."""
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW)
        self.assertEqual(self.prepared(updates), [True])
        started, done = [], []

        def start_helper(helper, plan, then, home):
            started.append(then)
            if answer is not None:
                if answer:
                    os.unlink(plan)  # taken
                then(answer)

        with mock.patch.object(update, "start_helper", start_helper):
            quietly(updates.launch, False, done.append)
        return started, done

    def test_staying_after_all(self):
        # The updater took its plan, then a call came in: it's told to change nothing, and the
        # update is offered again, with what was downloaded.
        updates = self.make(transport=Transport((200, "", self.archive, "")))
        self.folder.mkdir(parents=True, mode=0o700)
        (self.folder / updater.CANCEL).write_text("")  # an earlier stay's: gone before a new plan
        _started, done = self.launched(updates)
        self.assertEqual(done, [True])
        self.assertFalse((self.folder / updater.CANCEL).exists())
        quietly(updates.stay)
        self.assertTrue((self.folder / updater.CANCEL).exists())
        self.assertEqual((updates.working, updates.banner()[1]), (False, "Update"))
        self.assertEqual(self.prepared(updates), [True])
        self.assertEqual(len(self.transport.asked), 1)  # the stage was used again

    def test_quitting_takes_back_a_plan_not_taken(self):
        updates = self.make(transport=Transport((200, "", self.archive, "")))
        started, done = self.launched(updates, answer=None)
        plan = self.folder / "plan.json"
        self.assertTrue(plan.exists())
        updates.stop()  # Natter quits as the updater starts: Natter's environment doesn't stay on disk
        self.assertFalse(plan.exists())
        started[0](True)  # an updater found nothing to take after all
        self.assertEqual(done, [])

    def test_stop_ends_a_download(self):
        updates = self.make(transport=Transport())  # never answers
        self.app.settings["update_latest"] = NEWER
        self.app.settings["update_checked_at"] = int(NOW)
        done = []
        quietly(updates.prepare, done.append)
        stager = updates.stager
        updates.stop()
        self.assertTrue(stager.cancellable.is_cancelled())
        self.assertEqual(done, [])


if __name__ == "__main__":
    unittest.main()
