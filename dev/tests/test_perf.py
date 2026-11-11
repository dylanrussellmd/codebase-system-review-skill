"""Scale and performance tests on large generated ledgers (dev/tests/scale.py).

Everything except the small generator smoke test is skipped unless LEDGER_SLOW_TESTS=1, so the default suite stays
fast. Run them with:

    LEDGER_SLOW_TESTS=1 python3 -m unittest dev.tests.test_perf -v

test_scale_generator_small         (fast) a small generated ledger passes check: the generator matches the schema
test_large_ledger_checks_clean     the 1x ledger (10,000 findings, 600-step plan) passes check, all five views at 0
test_insert_throughput_budget      2,000 findings with 1-3 locations, one transaction per report: < 0.5 s to load and
                                   < 0.4 s to match (best of three runs, so a busy machine does not fail it)
test_perf_ledger_builds            the 10x ledger builds and passes check
test_baseline_measurements         the baseline measures gzip ratios on real prose and git growth per ledger commit
"""
import sqlite3
import unittest

from dev.tests import fixtures, scale
from dev.tests.fixtures import slow

INSERT_BUDGET_S = 0.5
MATCH_BUDGET_S = 0.4


def must_be_empty_counts(db):
    views = [v for v, _ in fixtures.load_ledger_module().MUST_BE_EMPTY]
    con = sqlite3.connect(str(db))
    try:
        return {v: con.execute(f'SELECT count(*) FROM "{v}"').fetchone()[0] for v in views}
    finally:
        con.close()


class ScaleGenerator(unittest.TestCase):

    def assert_clean(self, info):
        p = scale.check(info)
        self.assertEqual(p.returncode, 0, f"check failed on the generated ledger:\n{p.stdout}\n{p.stderr}")
        counts = must_be_empty_counts(info["db"])
        self.assertEqual(len(counts), 5)
        self.assertEqual(counts, {v: 0 for v in counts})

    def test_scale_generator_small(self):
        info = scale.build(fixtures.temp_dir("scale-") / "proj", reviews=2, files=20, findings=120, steps=30)
        self.assertEqual(info["counts"]["findings"], 240)
        self.assertEqual(info["counts"]["steps"], 30)
        self.assert_clean(info)

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
            if best_insert < INSERT_BUDGET_S and best_match < MATCH_BUDGET_S:
                break
        self.assertLess(best_insert, INSERT_BUDGET_S, f"loading 2,000 findings took {best_insert:.3f} s")
        self.assertLess(best_match, MATCH_BUDGET_S, f"matching 2,000 findings took {best_match:.3f} s")

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


if __name__ == "__main__":
    unittest.main()
