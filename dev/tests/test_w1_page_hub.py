"""The hub is the viewer page as it is, plus the data (plan P-0001, deconfliction K01).

test_hub_is_the_unpatched_page   the file written by progress-page, with its data script taken out, is dev/page.html
                                 but for the title; HUB_PATCHES holds only the data script
test_page_owns_the_hub_hooks     the page declares STATIC and the functions the hub runs on; core.py has no copy
test_viewer_page_has_no_snapshot the page served by the viewer carries no data, so STATIC is null there
"""
import re
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger

PAGE_HTML = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")
CORE_PY = (fixtures.REPO_ROOT / "dev" / "core.py").read_text(encoding="utf-8")
DATA_SCRIPT = re.compile(r"<script>window\.LEDGER_SNAPSHOT = .*?;</script>\n", re.S)


class HubIsThePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        fixtures.seed_demo(cls.L)
        cls.L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_hub_is_the_unpatched_page(self):
        m = self.L.module
        self.assertEqual(len(m.HUB_PATCHES), 1, "HUB_PATCHES holds the data script and nothing else")
        old, new = m.HUB_PATCHES[0]
        self.assertIn("window.LEDGER_SNAPSHOT", new)
        self.assertEqual(m.PAGE.count(old), 1)
        html, n = m.hub_page(self.L.root, self.L.db)
        self.assertGreater(n, 10)
        self.assertEqual(len(DATA_SCRIPT.findall(html)), 1)
        page = DATA_SCRIPT.sub("", html)
        self.assertEqual(page, m.PAGE.replace("<title>Review ledger</title>", "<title>Review ledger: proj</title>"))

    def test_page_owns_the_hub_hooks(self):
        for name in ("const STATIC =", "function setPath(", "function currentPath(", "function nav(",
                     "function staticApi(", "function md(", "function hubGateBody(", "VIEWS.guide ="):
            with self.subTest(name):
                self.assertEqual(PAGE_HTML.count(name), 1, "page.html declares it once")
                self.assertNotIn(name, CORE_PY, "core.py keeps no copy")
        # the page never writes the address bar or reads the route past these two
        self.assertEqual(len(re.findall(r"history\.replaceState\(", PAGE_HTML)), 3)  # takeToken, setPath (twice)

    def test_viewer_page_has_no_snapshot(self):
        self.assertNotIn("window.LEDGER_SNAPSHOT =", self.L.module.PAGE)


if __name__ == "__main__":
    unittest.main()
