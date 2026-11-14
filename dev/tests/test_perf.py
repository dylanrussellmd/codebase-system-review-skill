"""Scale and performance tests on large generated ledgers (dev/tests/scale.py).

Everything except the small generator smoke test is skipped unless LEDGER_SLOW_TESTS=1, so the default suite stays
fast. Run them with:

    LEDGER_SLOW_TESTS=1 python3 -m unittest dev.tests.test_perf -v

test_scale_generator_small         (fast) a small generated ledger passes check: the generator matches the schema
test_scale_build_many_reviews      (fast) the G-20 shape (15 reviews x 300 findings) builds and passes check, and its
                                   dates are the same whatever the clock says
test_large_ledger_checks_clean     the 1x ledger (10,000 findings, 600-step plan) passes check, all five views at 0
test_insert_throughput_budget      2,000 findings with 1-3 locations, one transaction per report: < 0.5 s to load and
                                   < 0.8 s to match (best of three runs, so a busy machine does not fail it); the
                                   generator makes its rows before the clock starts. Measured (perf_baseline.json
                                   insert_budget.*): 0.21 s and 0.32 s here, about 1.7 times that on a CI runner
test_perf_ledger_builds            the 10x ledger builds and passes check
test_baseline_measurements         the baseline measures gzip ratios on real prose and git growth per ledger commit
"""
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

from dev.tests import fixtures, helpers, scale
from dev.tests.fixtures import slow

INSERT_BUDGET_S = 0.5
MATCH_BUDGET_S = 0.8


def must_be_empty_counts(db):
    con = sqlite3.connect(str(db))
    try:
        views = [r[0] for r in con.execute("SELECT view FROM ledger_checks WHERE tier='error' ORDER BY position")]
        return {v: con.execute(f'SELECT count(*) FROM "{v}"').fetchone()[0] for v in views}
    finally:
        con.close()


class ScaleGenerator(unittest.TestCase):

    def assert_clean(self, info):
        p = scale.check(info)
        self.assertEqual(p.returncode, 0, f"check failed on the generated ledger:\n{p.stdout}\n{p.stderr}")
        counts = must_be_empty_counts(info["db"])
        self.assertGreaterEqual(len(counts), 5)   # the error-tier checks of ledger_checks (6 since v_unplanned_discoveries)
        self.assertEqual(counts, {v: 0 for v in counts})

    def test_scale_generator_small(self):
        info = scale.build(fixtures.temp_dir("scale-") / "proj", reviews=2, files=20, findings=120, steps=30)
        self.assertEqual(info["counts"]["findings"], 240)
        self.assertEqual(info["counts"]["steps"], 30)
        self.assert_clean(info)

    def test_scale_build_many_reviews(self):
        info = scale.build(fixtures.temp_dir("scale-") / "g20", **scale.G20)
        self.assertEqual(info["counts"]["findings"], 15 * 300)
        self.assert_clean(info)
        small = dict(reviews=3, files=10, findings=20, steps=10)
        dates = []
        for shift in (0, 5 * 365 * 86400):  # today, and five years on
            real = time.time
            with mock.patch("time.time", lambda: real() + shift):
                b = scale.build(fixtures.temp_dir("scale-") / "clock", **small)
            con = sqlite3.connect(str(b["db"]))
            try:
                dates.append(con.execute("SELECT code, started_at, approved_at FROM reviews ORDER BY id").fetchall()
                             + con.execute("SELECT code, created_at FROM gates ORDER BY id").fetchall())
            finally:
                con.close()
        self.assertEqual(dates[0], dates[1])
        self.assertEqual([r[1][:10] for r in dates[0][:3]], ["2025-01-01", "2025-01-08", "2025-01-15"])

    @slow
    def test_large_ledger_checks_clean(self):
        info = scale.build(fixtures.temp_dir("scale-") / "x1", scale=1)
        c = info["counts"]
        self.assertEqual(c["findings"], 10000)
        self.assertEqual(c["steps"], 600)
        self.assertEqual(c["gates"], 200)
        self.assertGreaterEqual(c["finding_locations"], 10000)
        self.assert_clean(info)

    @slow
    def test_insert_throughput_budget(self):
        best_insert = best_match = None
        for attempt in range(3):
            info = scale.build(fixtures.temp_dir("budget-") / "proj", reviews=1, files=50, plan=False, tag=False)
            t = info["timings"]
            self.assertEqual(t["insert_findings"]["n"], 2000)
            self.assertEqual(t["match_new_issue"]["n"] + t["match_existing_issue"]["n"], 2000)
            ins = t["insert_findings"]["s"]
            match = t["match_new_issue"]["s"] + t["match_existing_issue"]["s"]
            best_insert = ins if best_insert is None else min(best_insert, ins)
            best_match = match if best_match is None else min(best_match, match)
            if best_insert < helpers.budget(INSERT_BUDGET_S) and best_match < helpers.budget(MATCH_BUDGET_S):
                break
        helpers.assert_within(self, best_insert, INSERT_BUDGET_S, "loading 2,000 findings")
        helpers.assert_within(self, best_match, MATCH_BUDGET_S, "matching 2,000 findings")

    @slow
    def test_perf_ledger_builds(self):
        info = scale.build(fixtures.temp_dir("scale-") / "x10", scale=10)
        self.assertEqual(info["counts"]["findings"], 100000)
        self.assertEqual(info["counts"]["steps"], 6000)
        self.assert_clean(info)

    @slow
    def test_baseline_measurements(self):
        # Real prose: the repository's own Markdown (README.md, SKILL.md); the review reports are not in the repo.
        rows, raw = scale.baseline(scales=(1,), reports=fixtures.REPO_ROOT, work=fixtures.temp_dir("baseline-"),
                                   verbose=False)
        names = {n: v for n, v, _ in rows}
        self.assertEqual(names["x1.check_exit"], 0)
        self.assertEqual(names["x1_prose.check_exit"], 0)
        self.assertEqual(raw["x1_prose"]["prose"], "report prose")
        for tag in ("x1", "x1_prose"):
            for k in ("api_review_gzip_ratio", "api_plan_gzip_ratio", "ledger_gzip_ratio", "dump_gzip_ratio"):
                self.assertGreater(names[f"{tag}.{k}"], 1.0, f"{tag}.{k}")
        g = raw["x1"]["git_growth"]
        self.assertEqual(len(g["loose_kib_per_commit"]), 5)
        self.assertTrue(all(k > 0 for k in g["loose_kib_per_commit"]))


class MachineBudgets(unittest.TestCase):
    """Every speed budget is a multiple of a reference workload's time on the baseline machine, so a slower or busy
    machine gets budgets in proportion and a real slowdown in the code still fails."""

    def test_budget_scales_by_the_reference(self):
        base = helpers.BASELINE_REFERENCE_S
        with mock.patch.object(helpers, "_factor", None), \
                mock.patch.object(helpers, "reference_seconds", lambda: 2 * base):
            self.assertAlmostEqual(helpers.machine_factor(), 2.0)
            self.assertAlmostEqual(helpers.budget(0.5), 1.0)
            with self.assertRaises(AssertionError) as e:
                helpers.assert_within(self, 1.2, 0.5, "the thing")
        self.assertIn("x2.00", str(e.exception))
        with mock.patch.object(helpers, "_factor", None), \
                mock.patch.object(helpers, "reference_seconds", lambda: base / 3):
            self.assertEqual(helpers.machine_factor(), 1.0)   # a faster machine keeps the budgets as written

    @slow
    def test_budgets_scale_with_machine(self):
        """At half speed (one CPU shared with a busy loop) the 1x budgets pass, and the plan route, slow until the
        plan-page performance fix (PERF-H1), is still an expected failure."""
        if not helpers.can_slow_down():
            self.skipTest("environment: no CPU affinity on this machine")
        env = dict(fixtures.git_env(), LEDGER_SLOW_TESTS="1")
        env.pop("LEDGER_BROWSER_TESTS", None)
        p = helpers.run_slowed([sys.executable, "-m", "unittest", "-v",
                                "dev.tests.test_p4_views_checks.Speed.test_check_budget_1x",
                                "dev.tests.test_w2_perf.RouteBudgets.test_review_route_budget_1x",
                                "dev.tests.test_w2_perf.RouteBudgets.test_plan_route_budget_1x",
                                "dev.tests.test_w2_perf.EveryRouteBudget.test_check_and_dump_budget_1x"],
                               cwd=str(fixtures.REPO_ROOT), env=env, capture_output=True, text=True, timeout=1200)
        self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        self.assertRegex(p.stderr, r"test_plan_route_budget_1x .*\.\.\. expected failure")


if __name__ == "__main__":
    unittest.main()
