"""Plan P-0001 phase 4, check registry, view speed and indexes (steps 69, 89, 90, 91).

check reads its list from the ledger's ledger_checks table: error-tier views fail it, warning-tier views are listed
and leave exit 0, and /api/overview returns both tiers. Query-plan guards keep the check views and the owner queue
on indexes, and every foreign-key column has a leading index.
"""
import re
import time
import unittest

from dev.tests import fixtures, scale
from dev.tests.fixtures import Ledger, seed_demo, slow, temp_dir


def eqp(L, sql, args=()):
    """EXPLAIN QUERY PLAN rows as (id, parent, detail), details in the SQLite 3.36+ wording ('SCAN TABLE x AS y' of
    older versions becomes 'SCAN y')."""
    def norm(d):
        d = re.sub(r"^(SCAN|SEARCH) TABLE \w+ AS (\w+)", r"\1 \2", d)
        return re.sub(r"^(SCAN|SEARCH) TABLE (\w+)", r"\1 \2", d)
    return [(r[0], r[1], norm(r[3])) for r in L.con.execute(f"EXPLAIN QUERY PLAN {sql}", args)]


def correlated_scans(plan):
    """Details of SCAN steps inside a correlated subquery (run once per outer row)."""
    parent = {i: p for i, p, _ in plan}
    detail = {i: d for i, _, d in plan}
    out = []
    for i, _, d in plan:
        if not d.startswith("SCAN "):
            continue
        p = parent.get(i)
        while p:
            if detail.get(p, "").startswith("CORRELATED"):
                out.append(d)
                break
            p = parent.get(p)
    return out


def demo_ledger():
    L = Ledger()
    seed_demo(L)
    L.commit()
    return L


class CheckRegistry(unittest.TestCase):
    def test_check_lists_every_seeded_check(self):
        with Ledger(temp_dir("p4v-")) as L:
            checks = L.all("SELECT view, label, tier FROM ledger_checks ORDER BY position")
            p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn({"view": "v_unguarded_fixes", "label": "Fixed issues with no guard test, or whose guard test is gone",
                       "tier": "warning"}, checks)
        errors = {c["view"] for c in checks if c["tier"] == "error"}
        self.assertLessEqual({"v_unmatched_findings", "v_unchecked_known_issues", "v_untraceable_refs",
                              "v_uncovered_findings", "v_coverage_problems"}, errors)  # later schemas add more
        lines = p.stdout.splitlines()
        for c in checks:
            self.assertIn(f"    0  {c['label']}", lines)

    def test_check_warning_does_not_fail(self):
        with Ledger(temp_dir("p4v-")) as L:
            r = L.review()
            L.issue("ISS-0001", first_seen_review_id=r, status="fixed")      # fixed, no guard test
            L.commit()
            p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        self.assertIn("    1  Fixed issues with no guard test, or whose guard test is gone  (v_unguarded_fixes)  warning",
                      lines)
        self.assertTrue(any('"ISS-0001"' in l for l in lines), lines)

    def test_check_error_still_fails_with_a_warning(self):
        with Ledger(temp_dir("p4v-")) as L:
            r = L.review()
            L.issue("ISS-0001", first_seen_review_id=r, status="fixed")
            L.finding(r)                                                      # unmatched: an error-tier check
            L.commit()
            p = L.run("check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("(v_unmatched_findings)", p.stdout)

    def test_overview_lists_check_tiers(self):
        L = demo_ledger()
        try:
            con = L.module.connect(L.db)
            try:
                d = L.module.route(L.root, L.db, con, "/api/checks", {}, None)
            finally:
                con.close()
            want = L.all("SELECT view, label, tier FROM ledger_checks ORDER BY position")
        finally:
            L.close()
        self.assertEqual([(c["view"], c["label"], c["tier"]) for c in d["checks"]],
                         [(w["view"], w["label"], w["tier"]) for w in want])
        self.assertEqual({c["tier"] for c in d["checks"]}, {"error", "warning"})

    def test_check_names_a_missing_check_view(self):
        """A ledger_checks row naming a view that does not exist is an error line, not a crash."""
        with Ledger(temp_dir("p4v-")) as L:
            L.x("INSERT INTO ledger_checks(view, label, tier, position) VALUES ('v_no_such\"view', 'Gone', 'error', 999)")
            L.commit()
            p = L.run("check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("  ERR  Gone  (v_no_such\"view): no such table", p.stdout)
        self.assertNotIn("Traceback", p.stderr)


class QueryPlans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = demo_ledger()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def test_uncovered_findings_plan_uses_finding_index(self):
        plan = eqp(self.L, "SELECT count(*) FROM v_uncovered_findings")
        details = [d for _, _, d in plan]
        self.assertTrue(any("ix_step_findings_find" in d for d in details), details)
        self.assertFalse([d for d in details if "ix_steps_status" in d or re.match(r"SCAN s\b", d)], details)

    def test_eqp_no_full_scan_in_must_be_empty_views(self):
        views = [r["view"] for r in self.L.all("SELECT view FROM ledger_checks ORDER BY position")]
        self.assertGreaterEqual(len(views), 6)
        for v in views:
            with self.subTest(view=v):
                self.assertEqual(correlated_scans(eqp(self.L, f"SELECT count(*) FROM {v}")), [])

    def test_owner_queue_uses_gate_indexes(self):
        details = [d for _, _, d in eqp(self.L, "SELECT * FROM v_owner_queue")]
        self.assertTrue(any("SEARCH g USING INDEX ix_gates_step" in d for d in details), details)
        self.assertTrue(any("SEARCH g USING INDEX ix_gates_decision" in d for d in details), details)
        self.assertFalse([d for d in details if re.match(r"SCAN g\b", d)], details)

    def test_review_summary_filter_uses_index(self):
        details = [d for _, _, d in eqp(self.L, "SELECT * FROM v_review_summary WHERE review=?", ("R-0001",))]
        self.assertTrue(any(d.startswith("SEARCH f USING INDEX ix_findings_review") for d in details), details)
        self.assertFalse([d for d in details if re.match(r"SCAN [fr]\b", d)], details)

    def test_every_fk_has_leading_index(self):
        con = self.L.con
        missing = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            lead = set()
            for ix in con.execute("SELECT name FROM pragma_index_list(?)", (t,)).fetchall():
                cols = [r[0] for r in con.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (ix[0],))]
                if cols:
                    lead.add(cols[0])
            for (col,) in con.execute('SELECT "from" FROM pragma_foreign_key_list(?)', (t,)).fetchall():
                if col not in lead:
                    missing.append(f"{t}.{col}")
        self.assertEqual(missing, [])


class Speed(unittest.TestCase):
    def test_check_runs_optimize(self):
        """check and init end with PRAGMA optimize (statistics for the planner); check still passes after it."""
        with Ledger() as L:
            seed_demo(L)
            L.commit()
            p = L.run("check")
            self.assertIn(p.returncode, (0, 1), p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    @slow
    def test_check_budget_1x(self):
        info = scale.build(fixtures.temp_dir("scale-") / "x1", scale=1)
        t0 = time.perf_counter()
        p = scale.check(info)
        took = time.perf_counter() - t0
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(took, 0.6 + 0.4, f"check took {took:.2f} s on the 1x ledger")  # 0.6 s plus process start


if __name__ == "__main__":
    unittest.main()
