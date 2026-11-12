"""Plan P-0001 step 92 (decision D-0016): check reads its views from indexes (migration 0021) and runs
foreign_key_check only with --full.

test_rewritten_views_keep_their_rows   every ledger_checks view gives the same rows as its schema-20 text
                                       (dev/tests/data/check_views_schema20.sql) on the seeded demo, on that demo
                                       with problems planted for each view, and on a small scaled ledger
test_rewritten_views_on_1x             (slow) the same on the 1x scaled ledger, with problems planted
test_views_read_their_indexes          the query plans use the new indexes instead of reading every row
test_check_runs_fk_only_with_full      check runs PRAGMA foreign_key_check only with --full
test_schema_upgrade_from_20            migrate brings a ledger of the previous schema to 21 with the new indexes
"""
import contextlib
import io
import random
import re
import sqlite3
import unittest

from dev.tests import fixtures, scale
from dev.tests.helpers import eqp, trace_statements

BEFORE_SQL = (fixtures.REPO_ROOT / "dev" / "tests" / "data" / "check_views_schema20.sql").read_text(encoding="utf-8")
ORDERED = {"v_done_steps_open_issues"}   # the views with an ORDER BY: their order is compared too
# The views whose text migration 0021 changed; planting must make each of them non-empty.
REWRITTEN = ("v_untraceable_refs", "v_coverage_problems", "v_off_plan_coverage", "v_done_steps_open_issues")


def compare(testcase, db, must_have=()):
    """Assert each ledger_checks view of `db` gives the rows of its schema-20 text; return {view: row count}."""
    con = sqlite3.connect(str(db))
    try:
        con.executescript(BEFORE_SQL)
        counts = {}
        for (view,) in con.execute("SELECT view FROM ledger_checks ORDER BY position").fetchall():
            new = con.execute(f"SELECT * FROM {view}").fetchall()
            old = con.execute(f"SELECT * FROM before_{view}").fetchall()
            with testcase.subTest(db=db.name, view=view):
                if view in ORDERED:
                    testcase.assertEqual(new, old)
                else:
                    testcase.assertEqual(sorted(map(repr, new)), sorted(map(repr, old)))
            counts[view] = len(new)
        for view in must_have:
            with testcase.subTest(db=db.name, planted=view):
                testcase.assertGreater(counts[view], 0, "the planted problems should show in this view")
        return counts
    finally:
        con.close()


def plant(db, seed=7, share=0.2):
    """Break a copy of a ledger the ways check's views look for (triggers dropped, foreign keys off), on a random
    `share` of the rows: so the views have rows to compare."""
    rnd = random.Random(seed)
    con = sqlite3.connect(str(db))
    con.isolation_level = None
    x = con.execute
    for (name,) in x("SELECT name FROM sqlite_schema WHERE type='trigger'").fetchall():
        x(f'DROP TRIGGER "{name}"')

    def some(sql):
        ids = [r[0] for r in x(sql).fetchall()]
        return rnd.sample(ids, max(1, int(len(ids) * share))) if ids else []

    x("BEGIN")
    for i in some("SELECT id FROM finding_locations"):
        x(rnd.choice(["UPDATE finding_locations SET commit_sha=NULL WHERE id=?",
                      "UPDATE finding_locations SET commit_sha='" + "c" * 40 + "' WHERE id=?",
                      "UPDATE finding_locations SET line_start=NULL, line_end=NULL, symbol=NULL WHERE id=?",
                      "UPDATE finding_locations SET line_start=NULL, line_end=NULL WHERE id=?",
                      "UPDATE finding_locations SET excerpt='' WHERE id=?",
                      "UPDATE finding_locations SET excerpt=excerpt || char(10) || 'x' WHERE id=?",
                      "UPDATE finding_locations SET excerpt=excerpt || char(10) WHERE id=?",
                      "UPDATE finding_locations SET excerpt_status='not-text', excerpt=NULL WHERE id=?",
                      "UPDATE finding_locations SET whole_file=1, line_start=NULL, line_end=NULL WHERE id=?"]), (i,))
    for i in some("SELECT id FROM findings")[:50]:
        x("DELETE FROM finding_locations WHERE finding_id=?", (i,))
    x("UPDATE review_repositories SET tag_name=NULL WHERE rowid IN (SELECT min(rowid) FROM review_repositories)")
    steps = [r[0] for r in x("SELECT id FROM steps").fetchall()]
    if steps:
        for f in some("SELECT id FROM findings")[:200]:
            x("INSERT OR IGNORE INTO step_findings(step_id, finding_id, multi_reason) VALUES (?,?,?)",
              (rnd.choice(steps), f, rnd.choice([None, "both"])))
        for s in rnd.sample(steps, max(1, len(steps) // 4)):
            x("UPDATE steps SET status=? WHERE id=?", (rnd.choice(["done", "dropped", "doing"]), s))
        for s in rnd.sample(steps, max(1, len(steps) // 10)):
            x("DELETE FROM step_findings WHERE step_id=?", (s,))
        for f in some("SELECT id FROM findings")[:100]:
            x("UPDATE findings SET discovered_in_step_id=? WHERE id=?", (rnd.choice(steps), f))
        x("DELETE FROM plan_reviews WHERE rowid IN (SELECT max(rowid) FROM plan_reviews)")
        # points found during fix work that no step closes
        for f in some("SELECT id FROM findings WHERE issue_id IS NOT NULL")[:5]:
            x("UPDATE findings SET discovered_in_step_id=?, verification='confirmed' WHERE id=?",
              (rnd.choice(steps), f))
            x("""DELETE FROM step_findings WHERE finding_id IN (SELECT id FROM findings
                 WHERE issue_id = (SELECT issue_id FROM findings WHERE id = ?))""", (f,))
            x("UPDATE issues SET status='open' WHERE id = (SELECT issue_id FROM findings WHERE id = ?)", (f,))
    for i in some("SELECT id FROM issues"):
        x("UPDATE issues SET status=? WHERE id=?",
          (rnd.choice(["open", "planned", "in-progress", "needs-decision", "fixed", "accepted-risk"]), i))
    for f in some("SELECT id FROM findings"):
        x("UPDATE findings SET match_type=? WHERE id=?",
          (rnd.choice(["new", "continuation", "still-present", "changed", "recurrence", "regression"]), f))
    for e in some("SELECT id FROM events WHERE event IN ('opened','status-changed')"):
        x("DELETE FROM events WHERE id=?", (e,))
    for c in some("SELECT rowid FROM review_issue_checks"):
        x("DELETE FROM review_issue_checks WHERE rowid=?", (c,))
    if steps:   # last, so nothing above undoes it: a done step on issues still open
        s = rnd.choice([r[0] for r in x("SELECT DISTINCT step_id FROM step_findings").fetchall()] or steps)
        issues = "SELECT f.issue_id FROM step_findings sf JOIN findings f ON f.id = sf.finding_id WHERE sf.step_id = ?"
        x(f"UPDATE issues SET status='open' WHERE id IN ({issues})", (s,))
        x(f"""UPDATE steps SET status='done' WHERE id IN (SELECT sf.step_id FROM step_findings sf
              JOIN findings f ON f.id = sf.finding_id WHERE f.issue_id IN ({issues}))""", (s,))
        x(f"UPDATE findings SET verification='confirmed' WHERE id IN (SELECT finding_id FROM step_findings "
          f"WHERE step_id = ?)", (s,))
    x("COMMIT")
    con.close()


def copy_db(src, name):
    dest = fixtures.temp_dir("w8-check-") / name
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dest))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    return dest


class CheckViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = fixtures.Ledger(fixtures.ScratchRepo())
        cls.ids = fixtures.seed_demo(cls.L)
        fixtures.finish_demo(cls.L)
        cls.L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def test_rewritten_views_keep_their_rows(self):
        counts = compare(self, self.L.db)
        self.assertEqual({v: n for v, n in counts.items() if n and v != "v_unguarded_fixes"}, {})
        planted = copy_db(self.L.db, "demo-planted.sqlite")
        plant(planted, share=0.5)
        compare(self, planted, must_have=REWRITTEN)
        info = scale.build(fixtures.temp_dir("w8-scale-") / "proj", reviews=2, files=20, findings=150, steps=40)
        compare(self, info["db"])
        planted = copy_db(info["db"], "small-planted.sqlite")
        plant(planted)
        compare(self, planted, must_have=REWRITTEN + ("v_history_gaps", "v_match_problems",
                                                       "v_unplanned_discoveries", "v_unchecked_known_issues"))

    @fixtures.slow
    def test_rewritten_views_on_1x(self):
        info = scale.build(fixtures.temp_dir("w8-1x-") / "proj", scale=1)
        compare(self, info["db"])
        planted = copy_db(info["db"], "x1-planted.sqlite")
        plant(planted, share=0.05)
        compare(self, planted, must_have=REWRITTEN)

    def test_views_read_their_indexes(self):
        con = sqlite3.connect(str(self.L.db))
        try:
            plans = {v: " | ".join(eqp(con, f"SELECT count(*) FROM {v}")) for v in (
                "v_untraceable_refs", "v_history_gaps", "v_unchecked_known_issues", "v_unplanned_discoveries")}
        finally:
            con.close()
        self.assertIn("ix_locations_untraceable", plans["v_untraceable_refs"])
        self.assertIn("ix_events_history", plans["v_history_gaps"])
        self.assertIn("ix_issues_domain_known", plans["v_unchecked_known_issues"])
        # either index on discovered_in_step_id will do (SQLite 3.33 picks the foreign-key one): no findings scan
        self.assertRegex(plans["v_unplanned_discoveries"], r"ix_(findings_discovered|fk_findings_discovered_in_step_id)")
        scans = [x for x in plans["v_unplanned_discoveries"].split(" | ")
                 if re.match(r"SCAN (TABLE findings|f)\b", x) and "discovered" not in x]
        self.assertEqual(scans, [])

    def test_check_runs_fk_only_with_full(self):
        mod = self.L.module
        for full in (False, True):
            con = mod.connect(self.L.db)
            try:
                with trace_statements(con) as seen, contextlib.redirect_stdout(io.StringIO()) as out:
                    mod.check(con, full=full)
            finally:
                con.close()
            with self.subTest(full=full):
                self.assertEqual("PRAGMA foreign_key_check" in seen, full)
                self.assertEqual("foreign_key_check: ok" in out.getvalue(), full)
        p = self.L.run("check")
        self.assertIn("check --full runs integrity_check and foreign_key_check", p.stdout)

    def test_schema_upgrade_from_20(self):
        from dev.tests import _migrate_helpers as M
        project_dir, db = M.restore_v1()
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0021-check-speed", p.stdout)
        con = sqlite3.connect(str(db))
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
            self.assertTrue({"ix_events_history", "ix_issues_domain_known", "ix_findings_discovered",
                             "ix_locations_untraceable"} <= names, names)
            cols = [r[2] for r in con.execute("PRAGMA index_info(ix_step_findings_find)")]
            self.assertEqual(cols, ["finding_id", "step_id"])
        finally:
            con.close()
        p = fixtures.run_ledger("--root", project_dir, "check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
