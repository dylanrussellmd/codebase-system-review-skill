"""Static checks on dev/page.html (plan P-0001 steps 173 and 195; UI-L20, UI-R1, UI-R8).

test_page_shared_helpers_are_the_only_copies  one builder each for error boxes, expandable items and fact tiles; the
                                              tone of every tag comes from TONE; the shared CSS classes exist
test_page_static_helpers                      counts, answers and severity columns go through their helper; no long line
test_page_tone_and_filters                    every step status and owner need has a label and a tone; the plan filter
                                              is built from the labels
test_dark_palette_declared_once               the dark colours are written once
test_no_inline_layout_styles                  the only style attribute left is a progress bar segment's width
"""
import re
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger

PAGE_HTML = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")
CSS = PAGE_HTML[PAGE_HTML.index("<style>"):PAGE_HTML.index("</style>")]
SCRIPT = PAGE_HTML[PAGE_HTML.index('"use strict";'):]
MAX_LINE = 230  # characters: a longer line of the page is an expression to split


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

    def test_page_static_helpers(self):
        """Step 194: counts are worded by plural() or noun(), answers by answerText(), the four severity-count
        columns by sevCols(), and no line of the script is longer than MAX_LINE."""
        nouns = "points?|rows?|steps?|issues?|checks?|questions?|answers?|reviews?|phases?|defects?|decisions?"
        written = [m.group(0) for m in re.finditer(r"\$\{[^}`]*\} (?:%s)\b" % nouns, SCRIPT)]
        written += re.findall(r'fact\([^,()]*(?:\([^()]*\))?[^,()]*, "(?:[a-z ]* )?(?:%s)(?: [a-z ]*)?"\)' % nouns, SCRIPT)
        written += re.findall(r'\? "Step " : "Steps "', SCRIPT)
        self.assertEqual(written, [], "a count worded by hand")
        self.assertEqual(SCRIPT.count("optLabel("), 1, "only answerText() reads a gate's chosen option")
        for line in SCRIPT.splitlines():
            if "You answered" in line:
                self.assertIn("answerText(", line)
        self.assertEqual(SCRIPT.count("const sevCols ="), 1)
        self.assertEqual(SCRIPT.count('key:"medium"'), 1, "the severity-count columns are sevCols()")
        self.assertGreaterEqual(SCRIPT.count("sevCols()"), 2)
        first = PAGE_HTML[:PAGE_HTML.index('"use strict";')].count("\n") + 1
        long = [f"{n}: {len(l)}" for n, l in enumerate(SCRIPT.splitlines(), first) if len(l) > MAX_LINE]
        self.assertEqual(long, [], f"lines longer than {MAX_LINE} characters")

    def test_page_tone_and_filters(self):
        """Step 195: one dark palette, no layout in style attributes, and every step status and owner need the
        ledger can store has a label the page reads and an entry in TONE; the plan's filter is built from them."""
        self.test_dark_palette_declared_once()
        self.test_no_inline_layout_styles()
        L = Ledger()
        try:
            enums = L.module.enum_labels(L.con)
            steps = L.scalar("SELECT sql FROM sqlite_master WHERE name='steps'")
            stored = {name: re.findall(r"'([^']+)'", re.search(r"CHECK \(%s IN\s*\(([^)]*)\)" % col, steps).group(1))
                      for name, col in (("step", "status"), ("need", "owner_need"))}
        finally:
            L.close()
        tone = SCRIPT[SCRIPT.index("const TONE = {"):SCRIPT.index("const tone =")]
        for name, enum in (("step", "step_status"), ("need", "owner_need")):
            self.assertIn(f'{name}:"{enum}"', SCRIPT, "ENUM_OF feeds the page's label map from the API")
            self.assertEqual(set(enums[enum]), set(stored[name]), f"{enum}: a stored value has no label")
            entry = re.search(r"^  %s:\{([^}]*)\}" % name, tone, re.M).group(1)
            keys = {k.strip('"') for k in re.findall(r'("[\w-]+"|\w+):"', entry)}
            self.assertEqual(keys, set(stored[name]), f"TONE.{name}")
        plan = SCRIPT[SCRIPT.index("VIEWS.plan ="):SCRIPT.index("function lineChart(")]
        self.assertIn("Object.entries(L.step)", plan)
        self.assertIn("Object.entries(L.need)", plan)
        for copied in ('"st-done"', '"Needs your decision"', '["decision","confirm","review","production"]'):
            self.assertNotIn(copied, plan)

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
