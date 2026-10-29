"""Saving what WhatsApp's page hands over: which of WebKit's questions become downloads
(Account._on_policy, _on_create), where and under what name a download is saved, and what's said
when it can't be (Account._on_download), and the bridge keeping a blob: URL alive for its download
(bridge.js "Downloads", run here in JavaScriptCore against stand-ins).

Lab, WebKitGTK 2.52.6, 29 Oct 2026: a link with target="_blank" to a blob: URL, and window.open()
of one, did nothing at all (no file, no message), and a blob: download whose file can't be
written fails with only "Load request cancelled"."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("JavaScriptCore", "6.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, JavaScriptCore, WebKit  # noqa: E402

from natter import account  # noqa: E402
from natter.account import Account, parse_message  # noqa: E402

WHATSAPP = "https://web.whatsapp.com/"
BLOB = "blob:https://web.whatsapp.com/0b8c1e0e-4f7a-4f53-9d0e-2c1d5e6f7a8b"
NAV, NEW_WINDOW, RESPONSE = (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
                             WebKit.PolicyDecisionType.RESPONSE)


# ---- WebKit's questions --------------------------------------------------------------------------

class Decision:
    """A policy decision: a navigation (or new window) to `uri`, or a response from it."""

    def __init__(self, uri, main=True, mime="text/markdown", supported=True, clicked=True):
        self.uri, self.main, self.mime, self.supported, self.clicked = uri, main, mime, supported, clicked
        self.verdict = None

    def get_navigation_action(self):
        kind = WebKit.NavigationType.LINK_CLICKED if self.clicked else WebKit.NavigationType.OTHER
        return SimpleNamespace(get_request=lambda: SimpleNamespace(get_uri=lambda: self.uri),
                               get_navigation_type=lambda: kind, is_user_gesture=lambda: self.clicked)

    def get_response(self):
        return SimpleNamespace(get_uri=lambda: self.uri, get_mime_type=lambda: self.mime)

    def is_main_frame_main_resource(self):
        return self.main

    def is_mime_type_supported(self):
        return self.supported

    def use(self):
        self.verdict = "use"

    def ignore(self):
        self.verdict = "ignore"

    def download(self):
        self.verdict = "download"


class Page:
    """Just enough of an Account for its policy: the view's page, what it saved and what went outside."""
    _on_policy = Account._on_policy
    _trusted = Account._trusted
    _may_save = getattr(Account, "_may_save", None)
    _save = getattr(Account, "_save", None)
    _download = getattr(Account, "_download", None)
    _on_create = getattr(Account, "_on_create", None)

    def __init__(self, page=WHATSAPP):
        self.saved, self.outside, self.dev_url = [], [], None
        self.view = SimpleNamespace(get_uri=lambda: page, download_uri=self.saved.append, get_root=lambda: None)

    def open_link(self, _uri):
        return False

    def _open_outside(self, uri):
        self.outside.append(uri)


class PolicyTest(unittest.TestCase):
    def ask(self, kind, decision, page=WHATSAPP):
        account_ = Page(page)
        handled = account_._on_policy(None, decision, kind)
        return handled, decision.verdict, account_.saved, account_.outside

    def test_a_blob_opened_in_a_new_window_is_saved(self):
        # WhatsApp's download arrow: a link with target="_blank". It did nothing at all.
        self.assertEqual(self.ask(NEW_WINDOW, Decision(BLOB)), (True, "ignore", [BLOB], []))

    def test_a_data_address_opened_in_a_new_window_is_saved(self):
        uri = "data:text/markdown;base64,IyBIaQo="
        self.assertEqual(self.ask(NEW_WINDOW, Decision(uri)), (True, "ignore", [uri], []))

    def test_a_data_address_without_a_click_is_not_saved(self):
        # It has no origin to check: any frame could make one, in a loop.
        uri = "data:text/markdown;base64,IyBIaQo="
        self.assertEqual(self.ask(NEW_WINDOW, Decision(uri, clicked=False)), (True, "ignore", [], []))
        self.assertEqual(self.ask(NEW_WINDOW, Decision(BLOB, clicked=False)), (True, "ignore", [BLOB], []))
        for mime in ("text/markdown", "application/octet-stream"):
            self.assertEqual(self.ask(RESPONSE, Decision(uri, main=False, mime=mime, supported=False)),
                             (True, "ignore", [], []))
        for gesture in (False, True):
            account_ = Page()
            action = SimpleNamespace(get_request=lambda: SimpleNamespace(get_uri=lambda: uri),
                                     is_user_gesture=lambda g=gesture: g)
            account_._on_create(None, action)
            self.assertEqual(account_.saved, [uri] if gesture else [])

    def test_only_whatsapps_own_blobs_are_saved(self):
        other = "blob:https://example.org/0b8c1e0e-4f7a-4f53-9d0e-2c1d5e6f7a8b"
        self.assertEqual(self.ask(NEW_WINDOW, Decision(other)), (True, "ignore", [], []))
        # Nor anything once the page isn't WhatsApp's.
        self.assertEqual(self.ask(NEW_WINDOW, Decision(BLOB), page="https://example.org/"), (True, "ignore", [], []))

    def test_links_still_open_in_the_browser(self):
        uri = "https://example.org/article"
        self.assertEqual(self.ask(NEW_WINDOW, Decision(uri)), (True, "ignore", [], [uri]))

    def test_window_open_of_a_blob_is_saved(self):
        # window.open() asks no policy: WebKit goes straight to "create".
        account_ = Page()
        action = SimpleNamespace(get_request=lambda: SimpleNamespace(get_uri=lambda: BLOB))
        self.assertIsNone(account_._on_create(None, action))
        self.assertEqual((account_.saved, account_.outside), ([BLOB], []))
        page = Page("https://example.org/")
        page._on_create(None, action)
        self.assertEqual(page.saved, [])

    def test_window_open_of_anything_else_opens_nothing(self):
        account_ = Page()
        action = SimpleNamespace(get_request=lambda: SimpleNamespace(get_uri=lambda: "https://example.org/"))
        self.assertIsNone(account_._on_create(None, action))
        self.assertEqual((account_.saved, account_.outside), ([], []))

    def test_a_blob_in_a_frame_is_saved(self):
        for mime in ("text/markdown", "application/octet-stream", "application/pdf"):
            self.assertEqual(self.ask(RESPONSE, Decision(BLOB, main=False, mime=mime)), (True, "download", [], []))
        # A page of the page's own making still shows in its frame, and other frames are WebKit's.
        self.assertEqual(self.ask(RESPONSE, Decision(BLOB, main=False, mime="text/html")), (False, None, [], []))
        self.assertEqual(self.ask(RESPONSE, Decision("https://example.org/", main=False)), (False, None, [], []))
        self.assertEqual(self.ask(RESPONSE, Decision(BLOB, main=False), page="https://example.org/"),
                         (True, "ignore", [], []))

    def test_a_data_address_navigated_to_is_saved_afresh(self):
        # WebKit 2.52 starts no download for a data: response turned into one.
        uri = "data:application/octet-stream;base64,IyBIaQo="
        self.assertEqual(self.ask(RESPONSE, Decision(uri, supported=False)), (True, "ignore", [uri], []))

    def test_a_blob_navigated_to_is_saved_as_before(self):
        self.assertEqual(self.ask(RESPONSE, Decision(BLOB)), (True, "download", [], []))
        self.assertEqual(self.ask(NAV, Decision(BLOB)), (False, None, [], []))  # WebKit's own download link
        other = "blob:https://example.org/0b8c1e0e-4f7a-4f53-9d0e-2c1d5e6f7a8b"
        self.assertEqual(self.ask(RESPONSE, Decision(other)), (True, "ignore", [], []))


# ---- Where it's saved ----------------------------------------------------------------------------

def webkit_error(domain, code, message):
    return GLib.Error.new_literal(GLib.quark_from_string(domain) if isinstance(domain, str) else domain,
                                  message, int(code))


CANCELLED_BY_USER = (WebKit.download_error_quark(), WebKit.DownloadError.CANCELLED_BY_USER, "User cancelled the download")
LOAD_CANCELLED = (WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED, "Load request cancelled")


class Download:
    """A WebKit download: its handlers, and what Natter set on it."""

    def __init__(self, uri=BLOB, mime="text/markdown", length=31):
        self.uri, self.mime, self.length = uri, mime, length
        self.handlers, self.destination, self.overwrite, self.cancelled, self.received = {}, None, False, False, 0

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def get_request(self):
        return SimpleNamespace(get_uri=lambda: self.uri)

    def get_response(self):
        return SimpleNamespace(get_mime_type=lambda: self.mime, get_content_length=lambda: self.length)

    def set_destination(self, path):
        self.destination = path

    def get_destination(self):
        return self.destination

    def set_allow_overwrite(self, allow):
        self.overwrite = allow

    def get_received_data_length(self):
        return self.received

    def cancel(self):
        self.cancelled = True

    def decide(self, suggested):
        handled = self.handlers["decide-destination"](self, suggested)
        if self.cancelled:  # WebKit then fails it, as cancelled by the user
            self.fail(*CANCELLED_BY_USER)
        return handled

    def fail(self, domain, code, message):
        self.handlers["failed"](self, webkit_error(domain, code, message))
        self.handlers["finished"](self)


class SaveTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.chosen, self.downloads = self.root / "Chosen", self.root / "Downloads"
        self.done = []
        self.account = SimpleNamespace(app=SimpleNamespace(settings=SimpleNamespace(download_dir=self.chosen)),
                                       emit=lambda *args: self.done.append(args[1:]), _download_names={})
        for patcher in (mock.patch.object(account, "_downloads_folder", lambda: self.downloads),
                        mock.patch("builtins.print")):
            patcher.start()
            self.addCleanup(patcher.stop)
        for folder in (self.chosen, self.downloads):
            self.addCleanup(lambda f=folder: f.exists() and f.chmod(0o755))

    def download(self, **kwargs):
        download = Download(**kwargs)
        Account._on_download(self.account, None, download)
        return download

    def test_saved_in_the_chosen_folder_under_the_pages_name(self):
        # WebKit's name for it has underscores for spaces ("Lab_Findings.md").
        self.account._download_names[BLOB] = ("Lab Findings.md", 0.0)
        download = self.download()
        self.assertTrue(download.decide("Lab_Findings.md"))
        self.assertEqual(download.destination, str(self.chosen / "Lab Findings.md"))
        # The empty file is Natter's, made at once, so WebKit may write over it.
        self.assertTrue((self.chosen / "Lab Findings.md").exists())
        self.assertTrue(download.overwrite)
        self.assertNotIn(BLOB, self.account._download_names)  # used once

    def test_a_name_taken_gets_a_number(self):
        self.chosen.mkdir()
        (self.chosen / "notes.md").write_text("mine")
        download = self.download()
        download.decide("notes.md")
        self.assertEqual(download.destination, str(self.chosen / "notes (2).md"))
        self.assertEqual((self.chosen / "notes.md").read_text(), "mine")

    def test_a_blob_without_a_name_gets_its_types_extension(self):
        download = self.download()
        download.decide("")
        self.assertEqual(Path(download.destination).name, "download.md")
        download = self.download(mime="application/octet-stream")
        download.decide("")
        self.assertEqual(Path(download.destination).name, "download")

    def test_a_data_address_is_not_named_after_its_data(self):
        uri = "data:text/markdown;base64,IyBIaQo="
        download = self.download(uri=uri)
        download.decide("markdown;base64,IyBIaQo=")
        self.assertEqual(Path(download.destination).name, "download.md")
        download = self.download(uri=uri)
        download.decide("notes.md")  # a download link's own name
        self.assertEqual(Path(download.destination).name, "notes.md")

    def test_a_folder_that_cant_be_written_falls_back_to_downloads(self):
        self.chosen.mkdir()
        self.chosen.chmod(0o555)
        download = self.download()
        download.decide("notes.md")
        self.assertEqual(download.destination, str(self.downloads / "notes.md"))
        self.assertFalse(download.cancelled)

    def test_nowhere_to_write_says_why(self):
        for folder in (self.chosen, self.downloads):
            folder.mkdir()
            folder.chmod(0o555)
        download = self.download()
        self.assertTrue(download.decide("notes.md"))  # never left to WebKit's own choice
        self.assertTrue(download.cancelled)
        self.assertIsNone(download.destination)
        self.assertEqual(self.done, [(str(self.chosen / "notes.md"), False, os.strerror(13))])

    def test_a_slip_never_hands_the_choice_to_webkit(self):
        self.account.app.settings = None  # anything that breaks while choosing
        download = self.download()
        self.assertTrue(download.decide("notes.md"))
        self.assertTrue(download.cancelled)
        self.assertEqual(len(self.done), 1)
        self.assertFalse(self.done[0][1])

    def test_load_request_cancelled_says_what_the_folder_says(self):
        download = self.download()
        download.decide("notes.md")
        path = download.destination
        with mock.patch.object(account, "room_problem", lambda folder, size: os.strerror(28)):
            download.fail(*LOAD_CANCELLED)
        self.assertEqual(self.done, [(path, False, "No space left on device")])
        self.assertFalse(Path(path).exists())  # the empty file goes

    def test_other_failures_keep_webkits_reason(self):
        download = self.download()
        download.decide("notes.md")
        download.fail(WebKit.network_error_quark(), WebKit.NetworkError.FAILED, "Connection reset")
        self.assertEqual(self.done[-1][1:], (False, "Connection reset"))

    def test_a_revoked_blob_is_said_plainly(self):
        self.account._download_names[BLOB] = ("notes.md", 0.0)
        download = self.download()
        download.fail("WebKitBlobResource", 1, "")  # before any destination
        self.assertEqual(self.done, [("notes.md", False, "WhatsApp's page let go of the file before it was saved")])

    def test_finished(self):
        download = self.download()
        download.decide("notes.md")
        download.handlers["finished"](download)
        self.assertEqual(self.done, [(str(self.chosen / "notes.md"), True, "")])

    def test_room_problem(self):
        folder = self.root
        self.assertIsNone(account.room_problem(folder, 10))
        full = SimpleNamespace(f_bavail=1, f_frsize=4096, f_flag=0)
        with mock.patch.object(account.os, "statvfs", lambda _: full):
            self.assertEqual(account.room_problem(folder, 10_000), "No space left on device")
            self.assertIsNone(account.room_problem(folder, 4096))
        self.assertEqual(account.room_problem(self.root / "gone", 1), "No such file or directory")
        self.root.chmod(0o555)
        try:
            self.assertEqual(account.room_problem(self.root, 1), "Permission denied")
        finally:
            self.root.chmod(0o755)


class ToastTest(unittest.TestCase):
    def toast(self, path, ok, reason):
        from natter.app import Natter
        toasts = []
        window = SimpleNamespace(get_visible=lambda: True, toast=lambda *args, **_: toasts.append(args))
        fake = SimpleNamespace(locked=False, window=window, downloaded=set(), tell=lambda *args, **_: None)
        Natter._on_download(fake, None, path, ok, reason)
        return toasts[0][0]

    def test_a_failure_says_the_folder_and_why(self):
        path = str(Path.home() / "Downloads" / "Lab Findings.md")
        self.assertEqual(self.toast(path, False, "No space left on device"),
                         "Couldn't save “Lab Findings.md” in ~/Downloads: No space left on device")
        self.assertEqual(self.toast("Lab Findings.md", False, "WhatsApp's page let go of the file before it was saved"),
                         "Couldn't save “Lab Findings.md”: WhatsApp's page let go of the file before it was saved")
        self.assertEqual(self.toast(path, True, ""), "Saved “Lab Findings.md”")


class NameMessageTest(unittest.TestCase):
    def test_parsed(self):
        message = parse_message({"type": "download", "url": BLOB, "name": "Lab Findings‮.md\n", "token": "x"})
        self.assertEqual(message, {"type": "download", "url": BLOB, "name": "Lab Findings.md"})
        for bad in ({"url": "https://web.whatsapp.com/x"}, {"url": 5}, {"url": "blob:" + "x" * 600}, {"name": None}):
            self.assertIsNone(parse_message({"type": "download", "url": BLOB, "name": "a.md", **bad}), bad)

    def test_kept_a_while_and_bounded(self):
        page = SimpleNamespace(_token="t", _download_names={}, _trusted=lambda: True)

        def say(url, name):
            value = SimpleNamespace(to_json=lambda _: json.dumps({"type": "download", "url": url, "name": name,
                                                                  "token": "t"}))
            Account._on_message(page, None, value)
        say(BLOB, "a.md")
        self.assertEqual(page._download_names[BLOB][0], "a.md")
        for i in range(40):
            say(f"{BLOB}{i}", "b.md")
        self.assertEqual(len(page._download_names), account.DOWNLOAD_NAMES_KEPT)
        self.assertNotIn(BLOB, page._download_names)


# ---- The bridge ----------------------------------------------------------------------------------

BRIDGE = (Path(__file__).resolve().parent.parent / "natter" / "bridge.js").read_text()
START = "  // ---- Downloads"
END = "  // ---- Replying from a notification"

# A clock and timers to move by hand, URL's object URLs, links and window.open.
FAKES = """
let clock = 1e12;
Date.now = () => clock;
let timers = [];
function setTimeout(fn, ms) { timers.push({ at: clock + ms, fn }); return timers.length; }
function advance(ms) {
  clock += ms;
  const due = timers.filter((t) => t.at <= clock);
  timers = timers.filter((t) => t.at > clock);
  due.forEach((t) => t.fn());
}
const posted = [];
const post = (message) => posted.push(message);
class Blob { constructor(parts, options) { this.type = (options && options.type) || ""; } }
class File extends Blob { constructor(parts, name, options) { super(parts, options); this.name = name; } }
let next = 0;
const revoked = [];
const URL = {
  createObjectURL(object) { return "blob:https://web.whatsapp.com/" + (++next); },
  revokeObjectURL(url) { revoked.push(url); },
};
class EventTarget { dispatchEvent(event) { return true; } }
class HTMLElement extends EventTarget { click() { this.clicks = (this.clicks || 0) + 1; } }
class HTMLAnchorElement extends HTMLElement {
  constructor(href, download) { super(); this.href = href; this.download = download; }
  getAttribute(name) { return name === "download" && this.download !== undefined ? this.download : null; }
  closest(selector) { return this; }
}
const listeners = [];
const opened = [];
const window = {
  HTMLAnchorElement, EventTarget,
  addEventListener(type, fn, capture) { listeners.push({ type, fn, capture }); },
  open(url) { opened.push(String(url)); return null; },
};
const blob = () => URL.createObjectURL(new Blob(["# Hi"], { type: "text/markdown" }));
"""


def section() -> str:
    return BRIDGE[BRIDGE.index(START):BRIDGE.index(END)]


def run(script: str):
    context = JavaScriptCore.Context()
    value = context.evaluate(FAKES + section() + script, -1)
    exception = context.get_exception()
    if exception is not None:
        raise AssertionError(exception.get_message())
    return json.loads(value.to_json(0))


class KeptAliveTest(unittest.TestCase):
    def test_a_download_links_blob_is_revoked_a_minute_late(self):
        result = run("""
            const url = blob();
            const link = new HTMLAnchorElement(url, "Lab Findings.md");
            link.click();
            URL.revokeObjectURL(url);  // at once, as WhatsApp may
            const early = revoked.slice();
            advance(59000);
            const still = revoked.slice();
            advance(1000);
            [early, still, revoked, link.clicks, posted];
        """)
        early, still, after, clicks, posted = result
        self.assertEqual((early, still, after, clicks), ([], [], ["blob:https://web.whatsapp.com/1"], 1))
        self.assertEqual(posted, [{"type": "download", "url": "blob:https://web.whatsapp.com/1",
                                   "name": "Lab Findings.md"}])

    def test_window_open_keeps_it_too_and_names_the_file(self):
        result = run("""
            const url = URL.createObjectURL(new File(["# Hi"], "notes.md", { type: "application/octet-stream" }));
            window.open(url, "_blank");
            URL.revokeObjectURL(url);
            const early = revoked.slice();
            advance(60000);
            [opened, early, revoked, posted];
        """)
        url = "blob:https://web.whatsapp.com/1"
        self.assertEqual(result, [[url], [], [url], [{"type": "download", "url": url, "name": "notes.md"}]])

    def test_a_click_sent_to_a_link_counts(self):
        # FileSaver's way: a click event sent to a link that isn't in the page.
        result = run("""
            const url = blob();
            new HTMLAnchorElement(url, "saved.md").dispatchEvent({ type: "click" });
            URL.revokeObjectURL(url);
            [revoked, posted.map((m) => m.name)];
        """)
        self.assertEqual(result, [[], ["saved.md"]])

    def test_the_users_own_click_counts_once(self):
        result = run("""
            const url = blob();
            const link = new HTMLAnchorElement(url, "mine.md");
            const capture = listeners.find((l) => l.type === "click" && l.capture);
            capture.fn({ isTrusted: true, target: link });
            capture.fn({ isTrusted: false, target: link });  // a.click()'s own event: told already
            URL.revokeObjectURL(url);
            [revoked, posted.length];
        """)
        self.assertEqual(result, [[], 1])

    def test_other_urls_are_revoked_at_once(self):
        result = run("""
            const picture = blob();
            URL.revokeObjectURL(picture);  // never clicked or opened: an image, say
            new HTMLAnchorElement("https://example.org/", null).click();
            window.open("https://example.org/");
            [revoked, posted];
        """)
        self.assertEqual(result, [["blob:https://web.whatsapp.com/1"], []])

    def test_a_late_revoke_is_at_once(self):
        result = run("""
            const url = blob();
            new HTMLAnchorElement(url, "a.md").click();
            advance(61000);
            URL.revokeObjectURL(url);
            revoked;
        """)
        self.assertEqual(result, ["blob:https://web.whatsapp.com/1"])

    def test_remembered_urls_are_bounded(self):
        result = run("""
            const urls = [];
            for (let i = 0; i < 40; i++) {
              const url = blob();
              urls.push(url);
              new HTMLAnchorElement(url, "a.md").click();
            }
            urls.forEach((url) => URL.revokeObjectURL(url));
            revoked.length;
        """)
        self.assertEqual(result, 8)  # the oldest, past the 32 kept, go at once

    def test_only_saving_changes(self):
        """The section only keeps URLs and tells Natter names: nothing of WhatsApp's, no storage."""
        code = section()
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "require(", "__d(", "fetch("):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
