"""bridge.js never lets an <img> load a blob of a video (WebKitGTK decodes every frame of it)."""

import json
import unittest
from pathlib import Path

import gi

gi.require_version("JavaScriptCore", "6.0")
from gi.repository import JavaScriptCore  # noqa: E402

BRIDGE = (Path(__file__).resolve().parent.parent / "natter" / "bridge.js").read_text()
START = "  // ---- Videos aren't pictures"
END = "  // ---- Where things are in WhatsApp's page"

# Just enough of a browser for the guard: Blob, URL's object URLs and HTMLImageElement's src.
FAKES = """
class Blob { constructor(parts, options) { this.type = (options && options.type) || ""; } }
let next = 0;
const URL = {
  createObjectURL(object) { return "blob:file:///" + (++next); },
  revokeObjectURL(url) { this.revoked.push(url); },
  revoked: [],
};
const loads = [];
class HTMLImageElement {}
Object.defineProperty(HTMLImageElement.prototype, "src", {
  get() { return this._src || ""; },
  set(value) { this._src = value; loads.push(value); },
  enumerable: true, configurable: true,
});
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


class GuardTest(unittest.TestCase):
    def test_a_video_blob_never_reaches_an_image(self):
        result = run("""
            const video = URL.createObjectURL(new Blob([], {type: "video/mp4"}));
            const image = new HTMLImageElement();
            image.src = video;
            [loads, image.src];
        """)
        self.assertEqual(result, [["data:,"], "data:,"])

    def test_pictures_and_other_urls_load_as_before(self):
        result = run("""
            const picture = URL.createObjectURL(new Blob([], {type: "image/png"}));
            const untyped = URL.createObjectURL(new Blob([]));
            for (const url of [picture, untyped, "https://example.org/a.jpg", "data:image/png;base64,AA=="]) {
              new HTMLImageElement().src = url;
            }
            loads;
        """)
        self.assertEqual(result, ["blob:file:///1", "blob:file:///2", "https://example.org/a.jpg",
                                  "data:image/png;base64,AA=="])

    def test_a_revoked_url_is_forgotten(self):
        result = run("""
            const video = URL.createObjectURL(new Blob([], {type: "Video/WebM"}));
            URL.revokeObjectURL(video);
            new HTMLImageElement().src = video;
            [loads, URL.revoked];
        """)
        self.assertEqual(result, [["blob:file:///1"], ["blob:file:///1"]])

    def test_remembered_urls_are_bounded(self):
        result = run("""
            const first = URL.createObjectURL(new Blob([], {type: "video/mp4"}));
            for (let i = 0; i < 300; i++) URL.createObjectURL(new Blob([], {type: "video/mp4"}));
            const last = "blob:file:///301";
            new HTMLImageElement().src = first;
            new HTMLImageElement().src = last;
            loads;
        """)
        self.assertEqual(result, ["blob:file:///1", "data:,"])

    def test_the_getter_still_works(self):
        result = run("""
            const image = new HTMLImageElement();
            image.src = "https://example.org/a.jpg";
            image.src;
        """)
        self.assertEqual(result, "https://example.org/a.jpg")

    def test_only_loads_change(self):
        """The guard swaps what an <img> loads: no events made up, no storage, nothing of WhatsApp's."""
        code = section()
        for forbidden in ("dispatchEvent", "localStorage", "sessionStorage", "indexedDB", "require(", "__d("):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
