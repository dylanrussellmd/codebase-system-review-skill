"""Size and time budgets of the viewer's routes and large pages on generated ledgers (plan P-0001 steps 148, 160 to
162, 165, 167, 168). x1 is 10,000 findings and a 600-step plan; x10 is ten times that. Skipped unless
LEDGER_SLOW_TESTS=1 (building x10 takes over a minute); the page budgets also need LEDGER_BROWSER_TESTS=1.

    LEDGER_SLOW_TESTS=1 LEDGER_BROWSER_TESTS=1 python3 -m unittest dev.tests.test_w2_perf -v

Times are the best of three runs, so a busy machine does not fail a budget the code meets.
"""
import json
import time
import unittest

from dev.tests import browser, fixtures, scale
from dev.tests.fixtures import slow

_built = {}


def ledger(n):
    if n not in _built:
        _built[n] = scale.build(fixtures.temp_dir("w2perf-") / f"x{n}", scale=n)
    return _built[n]


def measure(info, path, **qs):
    """(best seconds of three, bytes of the JSON answer, the answer) for one route on a built ledger."""
    mod = fixtures.load_ledger_module()
    best, res = None, None
    for _ in range(3):
        con = mod.connect(info["db"], readonly=True)
        try:
            t = time.perf_counter()
            res = mod.route(info["root"], info["db"], con, path, {k: [str(v)] for k, v in qs.items()}, None)
            took = time.perf_counter() - t
        finally:
            con.close()
        best = took if best is None else min(best, took)
    if isinstance(res, tuple):
        raise AssertionError(f"{path}: {res}")
    return best, len(json.dumps(res, ensure_ascii=False, default=str).encode()), res


class RouteBudgets(unittest.TestCase):
    @slow
    def test_review_route_budget_1x(self):
        took, size, d = measure(ledger(1), "/api/review/R-0005")
        self.assertEqual(len(d["findings"]), 2000)
        self.assertLess(size, 1_500_000)
        self.assertLess(took, 0.150)

    @slow
    def test_plan_route_budget_1x(self):
        took, size, d = measure(ledger(1), "/api/plan/P-0001")
        self.assertEqual(len(d["steps"]), 600)
        self.assertEqual(len({i for s in d["steps"] for i in s["closes"]}), 10000)
        self.assertLess(size, 1_000_000)
        self.assertLess(took, 0.100)

    @slow
    def test_overview_header_budget(self):
        took, size, _ = measure(ledger(10), "/api/overview")
        self.assertLess(took, 0.020)

    @slow
    def test_tables_route_budget_10x(self):
        self.assertLess(measure(ledger(10), "/api/tables")[0], 0.300)
        self.assertLess(measure(ledger(10), "/api/table/findings")[0], 0.300)

    @slow
    def test_reviews_route_budget_10x(self):
        took, _, rows = measure(ledger(10), "/api/reviews")
        self.assertGreater(sum(r["points"] for r in rows), 80_000)
        self.assertLess(took, 0.100)

    @slow
    def test_trends_budget_10x(self):
        took, _, d = measure(ledger(10), "/api/trends")
        self.assertEqual(len(d["reviews"]), 5)
        self.assertLess(took, 1.5)

    @slow
    def test_gates_history_page_budget_10x(self):
        took, size, d = measure(ledger(10), "/api/gates", status="all")
        self.assertEqual((d["total"], len(d["gates"])), (ledger(10)["counts"]["gates"], 200))
        self.assertLess(size, 200_000)

    @slow
    def test_issues_page_budget_10x(self):
        took, size, d = measure(ledger(10), "/api/issues", status="")
        self.assertEqual((d["total"], len(d["rows"])), (ledger(10)["counts"]["issues"], 200))
        self.assertLess(took, 0.300)


class PageBudgets(unittest.TestCase):
    """The review and plan pages of the x1 ledger in a real browser."""

    @classmethod
    def setUpClass(cls):
        import os
        if os.environ.get("LEDGER_SLOW_TESTS") != "1":
            raise unittest.SkipTest("slow test skipped: set LEDGER_SLOW_TESTS=1 to run it")
        cls.browser = browser.require_browser()
        info = ledger(1)
        cls.server = fixtures.ServerThread(root=info["root"], db=info["db"]).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def open(self, hash_):
        page = browser.ViewerPage(self.browser, self.server.url, token=self.server.token)
        self.addCleanup(page.close)
        t = time.perf_counter()
        page.goto(hash_)
        return page, time.perf_counter() - t

    def test_review_page_budget_1x(self):
        best = None
        for _ in range(3):
            page, took = self.open("#/review/R-0005")
            best = took if best is None else min(best, took)
        self.assertLess(best, 1.0)
        self.assertLess(page.js("document.querySelectorAll('*').length"), 10_000)
        self.assertEqual(page.text(".tools ~ div p.note"), "2000 of 2000 points")
        self.assertEqual(page.page_errors, [])

    def test_review_show_more_append(self):
        fifth = []
        for _ in range(3):
            page, _ = self.open("#/review/R-0005")
            first = page.page.locator("details.item").first.element_handle()
            took = page.js("""() => { const out = [];
                for (let n = 0; n < 5; n++) { const b = document.querySelector(".points").parentNode.querySelector("button");
                  const t = performance.now(); b.click(); out.push(performance.now() - t); }
                return out; }""")
            fifth.append(took[4])
            self.assertEqual(page.count("details.item"), 1100)
            # the rows already there are the same elements: nothing was drawn again
            self.assertTrue(page.page.evaluate("e => e === document.querySelector('details.item')", first))
        self.assertLess(min(fifth), 30, fifth)   # the fifth click builds its 200 rows and nothing else

    def test_plan_page_heap_1x(self):
        page, _ = self.open("#/plan/P-0001")
        self.assertGreater(page.count(".steps details.item"), 10)
        heap = page.js("performance.memory ? performance.memory.usedJSHeapSize : null")
        if heap is None:
            self.skipTest("this browser does not report its heap size")
        self.assertLess(heap, 20_000_000)


if __name__ == "__main__":
    unittest.main()
