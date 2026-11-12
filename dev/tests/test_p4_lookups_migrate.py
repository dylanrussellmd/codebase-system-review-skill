"""Plan P-0001 schema 6 (migration 0006-lookups): the lookup tables of step 95, metrics.name referencing metric_defs
(step 94), RESTRICT instead of CASCADE on the review and finding keys (step 70), and migrate listing the rows a new
rule would refuse before it changes anything (step 61).
"""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, temp_dir

# Children of reviews and findings whose keys were ON DELETE CASCADE until schema 6.
RESTRICTED = {
    "review_repositories": "reviews", "review_goals": "reviews", "review_domains": "reviews",
    "reviewer_runs": "reviews", "review_actors": "reviews", "findings": "reviews",
    "review_requirement_checks": "reviews", "review_issue_checks": "reviews", "metrics": "reviews",
    "finding_locations": "findings", "finding_goals": "findings", "finding_requirements": "findings",
}


class Restrict(unittest.TestCase):

    def test_review_and_finding_keys_restrict(self):
        with Ledger(temp_dir("p4l-")) as L:
            for child, parent in RESTRICTED.items():
                with self.subTest(child=child):
                    acts = {r[2]: r[6] for r in L.x(f"PRAGMA foreign_key_list({child})") if r[2] == parent}
                    self.assertEqual(acts, {parent: "RESTRICT"})

    def test_review_delete_still_refused_by_the_trigger_first(self):
        """Behaviour is unchanged: the append-only trigger refuses the delete before any key is looked at."""
        with Ledger(temp_dir("p4l-")) as L:
            r = L.review("R-0001")
            f = L.finding(r, "SPEC-M1")
            L.location(f)
            L.commit()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                L.x("DELETE FROM reviews WHERE id=?", (r,))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                L.x("DELETE FROM findings WHERE id=?", (f,))
            self.assertEqual(L.count("finding_locations"), 1)


class MetricNames(unittest.TestCase):

    def test_metrics_name_references_metric_defs(self):
        with Ledger(temp_dir("p4l-")) as L:
            fks = {(r[3], r[2], r[4]) for r in L.x("PRAGMA foreign_key_list(metrics)")}
            self.assertIn(("name", "metric_defs", "name"), fks)
            r = L.review("R-0001")
            L.insert("metrics", review_id=r, name="brand_new_metric", value=1, method="counted")
            L.commit()
            self.assertEqual(L.scalar("SELECT label FROM metric_defs WHERE name='brand_new_metric'"),
                             "Brand new metric")
            self.assertEqual(L.x("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                L.x("DELETE FROM metric_defs WHERE name='brand_new_metric'")


class Upgrade(unittest.TestCase):

    def test_v1_ledger_migrates_to_lookups(self):
        project, db = M.restore_v1()
        before = M.counts(db)
        p = fixtures.run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0006-lookups", p.stdout)
        after = M.counts(db)
        for table, n in before.items():
            if table in ("meta", "schema_migrations"):
                continue
            with self.subTest(table=table):
                self.assertEqual(after[table], n + M.SEEDED_LATER.get(table, 0))
        for table in ("issue_statuses", "severities", "match_types", "relation_types", "gate_kinds", "enum_labels"):
            self.assertGreater(after[table], 0)
        p = fixtures.run_ledger("--root", project, "check")
        for bad in ("schema_version", "differs from the shipped", "broken references", "check: ERR"):
            self.assertNotIn(bad, p.stdout + p.stderr)
        con = sqlite3.connect(str(db))
        try:
            for child, parent in RESTRICTED.items():
                acts = {r[2]: r[6] for r in con.execute(f"PRAGMA foreign_key_list({child})") if r[2] == parent}
                self.assertEqual(acts, {parent: "RESTRICT"}, child)
            idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        finally:
            con.close()
        with Ledger(temp_dir("p4l-")) as fresh:
            want = {r[0] for r in fresh.x("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        self.assertEqual(idx, want)

    def test_migrate_lists_rows_a_new_rule_refuses(self):
        """Step 61: before a migration rebuilds a table with new rules, migrate lists every row of it they refuse
        (table, code or id, rule, the values) and changes nothing."""
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE gates SET code='GATE 7' WHERE code='GATE-0002'")
        con.execute("UPDATE gates SET code='gate-0001' WHERE code='GATE-0001'")
        con.commit()
        con.close()
        before = db.read_bytes()
        p = fixtures.run_ledger("--root", project, "migrate")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing was changed", out)
        self.assertIn("CHECK constraint failed for 2 existing rows", out)
        self.assertIn("gates GATE 7: code GLOB 'GATE-", out)
        self.assertIn("gates gate-0001: code GLOB 'GATE-", out)
        self.assertIn("(code='GATE 7')", out)
        self.assertEqual(db.read_bytes(), before)

    def test_migrate_lists_rows_without_a_code(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        sha = con.execute("SELECT commit_sha FROM review_repositories LIMIT 1").fetchone()[0]
        con.execute("UPDATE review_repositories SET commit_sha=upper(commit_sha) WHERE commit_sha=?", (sha,))
        con.commit()
        con.close()
        p = fixtures.run_ledger("--root", project, "migrate")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("review_repositories 1: ", out)
        self.assertIn(f"commit_sha='{sha.upper()}'", out)
        self.assertIn("nothing was changed", out)


if __name__ == "__main__":
    unittest.main()
