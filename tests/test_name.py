"""The app's name in one place: what the code shows takes it from NAME (natter/__init__.py), so a
rename is one line and no window, menu or notification keeps the old name."""

import ast
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "natter"
# Dotted names are app ids (io.github.Officialbabs.Natter, io.natter.Natter, a lab's …Natter.Dev).
# They outlive a rename, so they don't count.
DOTTED = re.compile(r"[\w-]+(?:\.[\w-]+)+")
# app.py's notice that the install from before the rename is still running calls that install
# by the name it had then (its body names this one by NAME).
OLD_INSTALL = {
    "The old Natter is still running",
}


def spelled_out(source: str, defines_name: bool = False) -> list[tuple[int, str]]:
    """(line, text) of every string literal in `source` that spells the name out. Comments never
    reach the tree; docstrings and other strings standing alone as statements are never shown,
    and NAME's own definition (where `defines_name`) has to say it."""
    tree = ast.parse(source)
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            allowed.add(id(node.value))
        elif defines_name and isinstance(node, ast.Assign) and \
                any(isinstance(target, ast.Name) and target.id == "NAME" for target in node.targets):
            allowed.add(id(node.value))
    return sorted((node.lineno, node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in allowed
                  and node.value not in OLD_INSTALL and "Natter" in DOTTED.sub("", node.value))


class NameTest(unittest.TestCase):
    def test_strings_take_the_name_from_NAME(self):
        found = [f"natter/{path.name}:{line}: {text!r}" for path in sorted(PACKAGE.glob("*.py"))
                 for line, text in spelled_out(path.read_text(), defines_name=path.name == "__init__.py")]
        self.assertEqual(found, [], "use NAME (from . import NAME), or a sentence that needs no name")

    def test_what_counts(self):
        source = '\n'.join([
            '"""Natter: a module docstring."""',
            'from . import NAME',
            'NAME = "Natter Den"',
            'APP_ID = "io.github.Officialbabs.Natter"  # Natter in a comment',
            'LAB = "io.github.Officialbabs.Natter.Lab"',
            'OLD = "The old Natter is still running"',
            'def f(n):',
            '    """Natter in a docstring."""',
            '    toast("Natter is still running")',
            '    menu.append("_About Natter", "app.about")',
            '    return f"Natter Den has {n} unread", f"Open {NAME}", "Scope of io.natter.Natter in Natter"',
        ])
        self.assertEqual(spelled_out(source, defines_name=True),
                         [(9, "Natter is still running"), (10, "_About Natter"), (11, "Natter Den has "),
                          (11, "Scope of io.natter.Natter in Natter")])
        self.assertIn((3, "Natter Den"), spelled_out(source))  # NAME is defined in __init__.py only


if __name__ == "__main__":
    unittest.main()
