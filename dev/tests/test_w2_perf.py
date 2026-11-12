"""Size and time budgets of the viewer's routes and large pages on generated ledgers (plan P-0001 steps 148, 160 to
162, 165, 167, 168, 153). x1 is 10,000 findings and a 600-step plan; x10 is ten times that. Skipped unless
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


# The budget of each route on the x1 ledger (plan step 153): seconds and, where it matters, bytes of the answer.
# Every GET route not named here has the default. Sizes are of the uncompressed body (the wire is gzipped).
ROUTE_BUDGETS_1X = {
    "overview": (0.020, None), "home": (0.100, None), "checks": (0.600, None), "review/*": (0.150, 1_500_000),
    "plan/*": (0.100, 1_000_000), "tables": (0.100, None), "table/*": (0.100, None), "trends": (0.300, None),
    "issues": (0.100, None), "gates": (0.100, None), "reviews": (0.100, None),
}
DEFAULT_ROUTE_BUDGET = (0.050, None)


class EveryRouteBudget(unittest.TestCase):
    @slow
    def test_route_budgets_1x(self):
        """One address of every GET route on the x1 ledger, against ROUTE_BUDGETS_1X."""
        info = ledger(1)
        mod = fixtures.load_ledger_module()
        con = mod.connect(info["db"], readonly=True)
        try:
            samples = {}
            for r in mod.ROUTES:
                if r.method != "GET":
                    continue
                paths = r.hub(con) if r.hub else []
                if r.handler is mod.get_step:
                    paths = ["step/P-0001/1"]
                samples["/".join(r.pattern)] = paths[-1]   # the last: the largest review, the newest run
        finally:
            con.close()
        slow_ones = []
        for name, path in samples.items():
            p, _, q = path.partition("?")
            took, size, _ = measure(info, "/api/" + p, **dict(x.split("=") for x in q.split("&") if x))
            seconds, most = ROUTE_BUDGETS_1X.get(name, DEFAULT_ROUTE_BUDGET)
            if took > seconds or (most and size > most):
                slow_ones.append(f"{path}: {took * 1000:.0f} ms (budget {seconds * 1000:.0f}), {size:,} bytes")
        self.assertEqual(slow_ones, [])

    @slow
    def test_sql_box_deadline_within_budget(self):
        mod = fixtures.load_ledger_module()
        self.assertLessEqual(mod.SQL_SECONDS, 10)   # the plan's deadline for /api/sql

    @slow
    def test_check_and_dump_budget_1x(self):
        info = ledger(1)
        for args, budget in ((("check", "--quiet"), 2.0), (("dump",), 2.0)):
            t = time.perf_counter()
            p = fixtures.run_ledger("--root", info["root"], *args, timeout=600)
            took = time.perf_counter() - t
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertLess(took, budget, args)

    @slow
    def test_overview_and_review_budget_10x(self):
        info = ledger(10)
        self.assertLess(measure(info, "/api/overview")[0], 0.5)
        import sqlite3
        con = sqlite3.connect(str(info["db"]))
        biggest = con.execute("""SELECT r.code FROM findings f JOIN reviews r ON r.id=f.review_id GROUP BY r.id
                                 ORDER BY count(*) DESC LIMIT 1""").fetchone()[0]
        con.close()
        self.assertLess(measure(info, f"/api/review/{biggest}")[0], 3.0)

    # characterization: step 92 measured check at 7.1 s on the x10 ledger; the plan's budget is 5 s. Remove the
    # decorator when the default check meets it.
    @unittest.expectedFailure
    @slow
    def test_check_budget_5s_10x(self):
        info = ledger(10)
        t = time.perf_counter()
        p = fixtures.run_ledger("--root", info["root"], "check", "--quiet", timeout=600)
        self.assertEqual(p.returncode, 0)
        self.assertLess(time.perf_counter() - t, 5.0)


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

    def test_plan_page_budget_1x(self):
        best = None
        for _ in range(3):
            page, took = self.open("#/plan/P-0001")
            best = took if best is None else min(best, took)
        self.assertLess(best, 0.8)
        self.assertEqual(page.page_errors, [])

    def test_plan_page_heap_1x(self):
        page, _ = self.open("#/plan/P-0001")
        self.assertGreater(page.count(".steps details.item"), 10)
        heap = page.js("performance.memory ? performance.memory.usedJSHeapSize : null")
        if heap is None:
            self.skipTest("this browser does not report its heap size")
        self.assertLess(heap, 20_000_000)


if __name__ == "__main__":
    unittest.main()
