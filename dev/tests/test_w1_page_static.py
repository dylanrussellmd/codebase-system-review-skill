"""Static checks on dev/page.html (plan P-0001 steps 173 and 195; UI-L20, UI-R1, UI-R8).

test_page_shared_helpers_are_the_only_copies  one builder each for error boxes, expandable items and fact tiles; the
                                              tone of every tag comes from TONE; the shared CSS classes exist
test_dark_palette_declared_once               the dark colours are written once
test_no_inline_layout_styles                  the only style attribute left is a progress bar segment's width
"""
import re
import unittest

from dev.tests import fixtures

PAGE_HTML = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")
CSS = PAGE_HTML[PAGE_HTML.index("<style>"):PAGE_HTML.index("</style>")]
SCRIPT = PAGE_HTML[PAGE_HTML.index('"use strict";'):]


class PageStatic(unittest.TestCase):
    def test_page_shared_helpers_are_the_only_copies(self):
        for decl in ("const plural =", "function errorBox(", "function answerText(", "const TONE =", "function item(",
                     "const fact =", "function filterBar("):
            self.assertEqual(SCRIPT.count(decl), 1, decl)
        self.assertEqual(SCRIPT.count('class:"status err"'), 1, "error boxes are built by errorBox()")
        self.assertEqual(len(re.findall(r'h\("details", \{class:\s?"item"', SCRIPT)), 1, "items are built by item()")
        tiles = re.findall(r'h\("div", null, h\("b", null, [^)]*\), h\("span", null,', SCRIPT)
        self.assertEqual(len(tiles), 1, "fact tiles are built by fact()")
        # no tag picks its colour beside the tag: a tone is looked up in TONE
        self.assertEqual(re.findall(r'class:\s?"tag " \+ \(', SCRIPT), [])
        self.assertNotIn("issueTone", SCRIPT)
        for cls in (".field{", ".card-link{", ".visually-hidden{"):
            self.assertEqual(CSS.count(cls), 1, cls)

    def test_dark_palette_declared_once(self):
        self.assertEqual(CSS.count("--bg:#121715"), 1)
        self.assertEqual(CSS.count("color-scheme:dark"), 1)

    def test_no_inline_layout_styles(self):
        self.assertNotIn("style=", PAGE_HTML.replace("<style>", ""))
        styles = re.findall(r'style:\s?(["`][^"`]*["`])', SCRIPT)
        self.assertEqual(len(styles), 3)
        for s in styles:
            self.assertRegex(s, r"^`width:\$\{pct\(\w+\)\}`$")


if __name__ == "__main__":
    unittest.main()
