"""The README and docs/: their links lead somewhere, the package lists are the ones install.sh
uses (natter/distro.py), and the README keeps the "Flatpak" heading install.sh points people to."""

import re
import unittest
from pathlib import Path

from natter.distro import AUTO, INSTALL, OPTIONAL, PIECE, PIECES, REQUIRED, packages

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\]\(([^)\s]+)\)|<img src=\"([^\"]+)\"")


def anchors(page: Path) -> set[str]:
    """The ids GitHub gives a page's headings: lower case, punctuation dropped, spaces as hyphens."""
    found = set()
    for line in page.read_text().splitlines():
        heading = re.match(r"#{1,6} (.+)", line)
        if heading:
            words = re.sub(r"[^\w\- ]", "", heading.group(1).replace("`", "").lower())
            found.add(words.replace(" ", "-"))
    return found


class LinkTest(unittest.TestCase):
    def test_local_links_lead_somewhere(self):
        for page in PAGES:
            for match in LINK.finditer(page.read_text()):
                target = match.group(1) or match.group(2)
                if re.match(r"[a-z]+:", target):
                    continue  # a web address
                path, _, anchor = target.partition("#")
                found = (page.parent / path).resolve() if path else page
                with self.subTest(page=page.name, link=target):
                    self.assertTrue(found.exists(), found)
                    if anchor:
                        self.assertIn(anchor, anchors(found))

    def test_readme_has_the_flatpak_heading_install_sh_names(self):
        self.assertIn('see \\"Flatpak\\" in README.md', (ROOT / "install.sh").read_text())
        self.assertIn("flatpak", anchors(ROOT / "README.md"))


class PackageListTest(unittest.TestCase):
    def test_packaging_lists_what_install_sh_installs(self):
        text = (ROOT / "docs" / "packaging.md").read_text()
        groups = (
            [piece for piece in PIECES if piece.group == REQUIRED] + [PIECE["codecs"]],
            [PIECE[name] for name in ("webrtc", "nice", "GstWebRTC", "GstSdp", "pipewire")],
            [piece for piece in PIECES if piece.group == OPTIONAL],
        )
        for manager in AUTO:
            listed = [line[len(INSTALL[manager]):].split() for line in text.splitlines()
                      if line.startswith(INSTALL[manager] + " ")]
            with self.subTest(manager=manager):
                self.assertEqual(listed, [packages(group, manager) for group in groups])


if __name__ == "__main__":
    unittest.main()
