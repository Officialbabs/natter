"""get.sh, run whole in a scratch home: it finds the tag of the latest release (from GitHub's API,
or else from where the Releases page's latest link leads), downloads that tag's archive and runs
its install.sh. Stand-ins for curl and wget answer from canned replies and log what they're asked,
so nothing reaches the network, and the archive's install.sh only says it ran."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
GET_SH = ROOT / "get.sh"
API = "https://api.github.com/repos/Officialbabs/natter/releases/latest"
LATEST = "https://github.com/Officialbabs/natter/releases/latest"
TAG_PAGE = "https://github.com/Officialbabs/natter/releases/tag/"
ARCHIVES = "https://github.com/Officialbabs/natter/archive/refs/tags/"
RELEASES = "https://github.com/Officialbabs/natter/releases"
NOT_FOUND = "Couldn't find Natter Den's latest release on GitHub.\n" \
            "Check your connection, then run this again in a moment.\n"
NO_RELEASE = f"There's no release of Natter Den on GitHub yet ({RELEASES}).\n" \
             "Run this again once there is one.\n"

# What get.sh needs from the system. curl and wget are stand-ins; gdbus (so nothing is running),
# pacman and flatpak stay out.
TOOLS = ("sh", "sed", "head", "tar", "gzip", "mkdir", "mktemp", "mv", "rm", "dirname", "id", "cat", "grep")
SHELLS = [shell for shell in ("dash", "bash") if shutil.which(shell)] or ["sh"]

# FAKE_API: a file with the API's answer (none: the API fails, as when it's rate-limited).
# FAKE_REDIRECT: where the Releases page's latest link leads (none: github.com can't be reached).
FAKE_CURL = r"""#!/bin/sh
echo "curl $*" >> "$FAKE_LOG"
for url; do :; done
effective=""
for arg; do [ "$arg" = "%{url_effective}" ] && effective=yes; done
case "$url" in
    https://api.github.com/repos/Officialbabs/natter/releases/latest)
        [ -n "$FAKE_API" ] || exit 22
        cat "$FAKE_API" ;;
    https://github.com/Officialbabs/natter/releases/latest)
        [ -n "$effective" ] || exit 2
        printf %s "${FAKE_REDIRECT:-$url}"
        [ -n "$FAKE_REDIRECT" ] || exit 6 ;;
    https://github.com/Officialbabs/natter/archive/refs/tags/*.tar.gz) cat "$FAKE_TARBALL" ;;
    file://*) cat "${url#file://}" ;;
    *) exit 22 ;;
esac
"""

# As wget 1.x reports a redirect with -S --spider, on stderr.
FAKE_WGET = r"""#!/bin/sh
echo "wget $*" >> "$FAKE_LOG"
for url; do :; done
spider=0
for arg; do case "$arg" in -S | --spider) spider=$((spider + 1)) ;; esac; done
case "$url" in
    https://api.github.com/repos/Officialbabs/natter/releases/latest)
        [ -n "$FAKE_API" ] || exit 8
        cat "$FAKE_API" ;;
    https://github.com/Officialbabs/natter/releases/latest)
        [ "$spider" -eq 2 ] || exit 2
        {
            echo "Spider mode enabled. Check if remote file exists."
            echo "--2026-10-23 10:00:00--  $url"
            if [ -z "$FAKE_REDIRECT" ]; then
                echo "Resolving github.com (github.com)... failed: Temporary failure in name resolution."
                exit 4
            fi
            echo "HTTP request sent, awaiting response... "
            echo "  HTTP/1.1 302 Found"
            echo "  Content-Type: text/html; charset=utf-8"
            echo "  Location: $FAKE_REDIRECT"
            echo "Location: $FAKE_REDIRECT [following]"
            echo "--2026-10-23 10:00:01--  $FAKE_REDIRECT"
            echo "  HTTP/1.1 200 OK"
            echo "Remote file exists and could contain further links,"
        } >&2 ;;
    https://github.com/Officialbabs/natter/archive/refs/tags/*.tar.gz) cat "$FAKE_TARBALL" ;;
    *) exit 8 ;;
esac
"""


def release_json(tag: str, pretty: bool = True) -> str:
    """The API's answer for a release, as GitHub writes it (pretty) or on one line."""
    release = {"url": "https://api.github.com/repos/Officialbabs/natter/releases/1",
               "html_url": TAG_PAGE + tag, "id": 1, "tag_name": tag, "target_commitish": "master",
               "name": f"Natter Den {tag}", "draft": False, "prerelease": False, "assets": [],
               "tarball_url": "https://api.github.com/repos/Officialbabs/natter/tarball/" + tag}
    return json.dumps(release, indent=2) if pretty else json.dumps(release, separators=(",", ":"))


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs " + ", ".join(TOOLS))
class GetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.system = base / "system-bin"
        cls.system.mkdir()
        for tool in dict.fromkeys(TOOLS + tuple(SHELLS)):
            (cls.system / tool).symlink_to(shutil.which(tool))
        # A release's archive: everything in one top folder, as GitHub makes it.
        cls.tarball = base / "natter.tar.gz"
        with tarfile.open(cls.tarball, "w:gz") as archive:
            script = b'#!/bin/sh\necho "install.sh from the archive ran"\n'
            member = tarfile.TarInfo("natter-0.1.0/install.sh")
            member.size, member.mode = len(script), 0o755
            archive.addfile(member, io.BytesIO(script))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_get(self, *args, fetcher="curl", shell=None, api=None, redirect=None, source=None, env=None):
        """get.sh with `args` in a new scratch home, with a stand-in `fetcher` (curl, wget or both).
        `api` is the API's answer (None: it fails), `redirect` where the latest link leads (None:
        github.com can't be reached), `source` what's already in ~/.local/lib/natter/source."""
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
        home, fake, log = folder / "home", folder / "fake-bin", folder / "log"
        home.mkdir()
        fake.mkdir()
        log.write_text("")
        for name in (("curl", "wget") if fetcher == "both" else (fetcher,)):
            (fake / name).write_text(FAKE_CURL if name == "curl" else FAKE_WGET)
            (fake / name).chmod(0o755)
        for name in ("flatpak", "flatpak-builder"):
            (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "$FAKE_LOG"\n')
            (fake / name).chmod(0o755)
        if api is not None:
            (folder / "api.json").write_text(api)
        installed = home / ".local/lib/natter/source"
        if source is not None:
            installed.mkdir(parents=True)
            (installed / "install.sh").write_text(source)
        environ = {
            "PATH": f"{fake}:{self.system}", "HOME": str(home), "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"), "XDG_RUNTIME_DIR": str(folder),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus", "FAKE_LOG": str(log),
            "FAKE_API": str(folder / "api.json") if api is not None else "", "FAKE_REDIRECT": redirect or "",
            "FAKE_TARBALL": str(self.tarball), **(env or {}),
        }
        done = subprocess.run([shell or SHELLS[0], str(GET_SH), *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, env=environ, timeout=60)
        logged = log.read_text().splitlines()
        urls = [line.split()[-1] for line in logged if line.split()[0] in ("curl", "wget")]
        return SimpleNamespace(code=done.returncode, out=done.stdout + done.stderr, logged=logged, urls=urls,
                               archives=[url for url in urls if "/archive/" in url or url.startswith("file:")],
                               source=installed)

    def assertInstalled(self, run, tag):
        self.assertEqual(run.code, 0, run.out)
        self.assertIn(f"==> Downloading Natter Den {tag}\n", run.out)
        self.assertEqual(run.archives, [f"{ARCHIVES}{tag}.tar.gz"])
        self.assertTrue((run.source / "install.sh").is_file(), run.out)
        self.assertIn("install.sh from the archive ran\n", run.out)

    def assertNothingDownloaded(self, run, message=NOT_FOUND):
        self.assertEqual(run.code, 1, run.out)
        self.assertTrue(run.out.endswith(message), run.out)
        self.assertEqual(run.archives, [])
        self.assertNotIn("master", " ".join(run.urls))
        self.assertNotIn("==> Downloading", run.out)

    def test_the_api_names_the_release(self):
        for fetcher in ("curl", "wget"):
            for shell in SHELLS:
                with self.subTest(fetcher=fetcher, shell=shell):
                    run = self.run_get(fetcher=fetcher, shell=shell, api=release_json("v0.1.0"))
                    self.assertInstalled(run, "v0.1.0")
                    self.assertEqual(run.urls[0], API)
                    self.assertNotIn(LATEST, run.urls)  # no need to ask the Releases page
                    self.assertIn("==> Putting Natter Den in ~/.local/lib/natter/source\n", run.out)
                    self.assertIn("Update:    curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/"
                                  "master/get.sh | sh\n", run.out)

    def test_the_api_on_one_line(self):
        run = self.run_get(api=release_json("v0.1.0", pretty=False))
        self.assertInstalled(run, "v0.1.0")

    def test_curl_is_used_when_there_are_both(self):
        run = self.run_get(fetcher="both", api=release_json("v0.1.0"))
        self.assertInstalled(run, "v0.1.0")
        self.assertTrue(all(line.startswith("curl ") for line in run.logged), run.logged)

    def test_the_archive_is_the_tags(self):
        for tag in ("v0.1.0", "v2.3.4-rc.1", "v10.0_beta"):
            with self.subTest(tag=tag):
                run = self.run_get(api=release_json(tag))
                self.assertInstalled(run, tag)
                self.assertEqual(run.archives, [f"https://github.com/Officialbabs/natter/archive/refs/tags/{tag}.tar.gz"])

    def test_the_releases_page_when_the_api_fails(self):
        for fetcher in ("curl", "wget"):
            for shell in SHELLS:
                with self.subTest(fetcher=fetcher, shell=shell):
                    run = self.run_get(fetcher=fetcher, shell=shell, redirect=TAG_PAGE + "v0.1.0")
                    self.assertInstalled(run, "v0.1.0")
                    self.assertEqual(run.urls[:2], [API, LATEST])

    def test_curl_asks_where_the_latest_link_leads(self):
        run = self.run_get(redirect=TAG_PAGE + "v0.1.0")
        asked = next(line for line in run.logged if line.endswith(" " + LATEST))
        self.assertIn(" -w %{url_effective} ", asked)
        self.assertIn(" -o /dev/null ", asked)

    def test_neither_way_finds_a_release(self):
        # No connection: the API fails and github.com can't be reached either.
        for fetcher in ("curl", "wget"):
            for shell in SHELLS:
                with self.subTest(fetcher=fetcher, shell=shell):
                    run = self.run_get(fetcher=fetcher, shell=shell)
                    self.assertNothingDownloaded(run)
                    self.assertFalse(run.source.exists())
                    self.assertEqual(run.urls, [API, LATEST])

    def test_no_release_yet(self):
        # GitHub answers, but there's no release yet (or only drafts and pre-releases): the API says
        # 404 and the latest link leads to the Releases page itself. Not the connection's fault.
        for fetcher in ("curl", "wget"):
            for shell in SHELLS:
                with self.subTest(fetcher=fetcher, shell=shell):
                    run = self.run_get(fetcher=fetcher, shell=shell, redirect=RELEASES)
                    self.assertNothingDownloaded(run, NO_RELEASE)
                    self.assertNotIn("connection", run.out)
                    self.assertFalse(run.source.exists())
                    self.assertEqual(run.urls, [API, LATEST])

    def test_other_pages_arent_taken_for_no_release(self):
        for page in (RELEASES + "/", RELEASES + "?page=2", RELEASES + "/latest", RELEASES + "x",
                     "https://github.com/login?return_to=" + RELEASES):
            with self.subTest(page=page):
                run = self.run_get(redirect=page)
                self.assertNothingDownloaded(run)

    def test_the_installed_copy_stays_when_no_release_is_found(self):
        run = self.run_get(source="# the installed one\n")
        self.assertNothingDownloaded(run)
        self.assertEqual((run.source / "install.sh").read_text(), "# the installed one\n")
        self.assertEqual(os.listdir(run.source.parent), ["source"])  # no download folder left

    def test_bad_tags_are_refused(self):
        bad = ("0.1.0", "v", "latest", "v0.1.0 x", "v1/../../master", "v0.1.0;id", "$(id)", "v0.1.0\nv0.2.0",
               'v1"x', "", "vé")
        for tag in bad:
            for fetcher in ("curl", "wget"):
                with self.subTest(tag=tag, fetcher=fetcher):
                    # The API's bad tag counts as the API failing: the latest link is asked instead.
                    run = self.run_get(fetcher=fetcher, api=release_json(tag), redirect=TAG_PAGE + "v0.1.0")
                    self.assertInstalled(run, "v0.1.0")
                    run = self.run_get(fetcher=fetcher, api=release_json(tag))
                    self.assertNothingDownloaded(run)
        for page in ("v0.1.0;id", "v1%2F..", "", "0.1.0", "v$(id)"):
            with self.subTest(page=page):
                run = self.run_get(redirect=TAG_PAGE + page)
                self.assertNothingDownloaded(run)

    def test_natter_archive_skips_the_lookup(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                run = self.run_get(shell=shell, api=release_json("v0.1.0"), redirect=TAG_PAGE + "v0.1.0",
                                   env={"NATTER_ARCHIVE": f"file://{self.tarball}"})
                self.assertEqual(run.code, 0, run.out)
                self.assertEqual(run.urls, [f"file://{self.tarball}"])
                self.assertIn("==> Downloading Natter Den\n", run.out)
                self.assertIn("install.sh from the archive ran\n", run.out)

    def test_an_update_replaces_the_installed_copy(self):
        run = self.run_get(api=release_json("v0.2.0"), source="# the installed one\n")
        self.assertInstalled(run, "v0.2.0")
        self.assertIn("==> Updating ~/.local/lib/natter/source\n", run.out)
        self.assertIn("install.sh from the archive ran", (run.source / "install.sh").read_text())
        self.assertEqual(os.listdir(run.source.parent), ["source"])

    def test_the_flatpak_is_built_from_the_release(self):
        run = self.run_get("--flatpak", api=release_json("v0.1.0"))
        self.assertEqual(run.code, 0, run.out)
        self.assertIn("==> Downloading Natter Den v0.1.0\n", run.out)
        self.assertEqual(run.archives, [ARCHIVES + "v0.1.0.tar.gz"])
        built = [line for line in run.logged if line.startswith("flatpak-builder ")]
        self.assertEqual(len(built), 1, run.logged)
        self.assertTrue(built[0].endswith(f"{run.source}/packaging/io.github.Officialbabs.Natter.yml"), built)
        self.assertIn("Update:    curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/get.sh"
                      " | sh -s -- --flatpak\n", run.out)

    def test_the_flatpak_waits_for_a_release_too(self):
        for redirect, message in ((None, NOT_FOUND), (RELEASES, NO_RELEASE)):
            with self.subTest(redirect=redirect):
                run = self.run_get("--flatpak", redirect=redirect)
                self.assertNothingDownloaded(run, message)
                self.assertFalse(any(line.startswith("flatpak") for line in run.logged), run.logged)

    def test_usage(self):
        run = self.run_get("--bogus")
        self.assertEqual(run.code, 2)
        self.assertEqual(run.out, "Usage: get.sh [--flatpak]\n")
        self.assertEqual(run.logged, [])


class ScriptTest(unittest.TestCase):
    def test_syntax(self):
        for shell in ("sh", "dash", "bash"):
            if shutil.which(shell):
                done = subprocess.run([shell, "-n", str(GET_SH)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_installs_releases_not_master(self):
        text = GET_SH.read_text()
        self.assertNotIn("refs/heads", text)
        self.assertIn("/archive/refs/tags/", text)
        self.assertEqual(text.splitlines()[-1], 'main "$@"')


if __name__ == "__main__":
    unittest.main()
