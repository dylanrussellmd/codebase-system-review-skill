"""check --quiet, the whole owner queue in `gates` (plan P-0001 step 112; WALK-L2, WALK-R2, TEST-T19, TEST-T39) and the
read-only `query` command (step 123; WALK-L14, DOC-M11).

test_check_quiet                            integrity failure and broken references counted; each error view counted
                                            with at most 5 sample rows; --quiet prints only failures
test_cli_gates_lists_open_and_answered_only open/answered gates in created_at order, applied and withdrawn hidden,
                                            'no open gates' when there are none, waiting steps and open decisions
test_query_readonly_refuses_writes          INSERT/UPDATE/ATTACH/PRAGMA writes are refused; the ledger is unchanged
test_query_prints_rows                      a SELECT prints TSV (header first) or --json
"""
import hashlib
import json
import sqlite3
import unittest

from dev.tests._core_helpers import quiet
from dev.tests.fixtures import Ledger


class FakeIntegrity:
    """A connection whose PRAGMA quick_check reports damage; everything else goes to the real connection."""

    def __init__(self, con):
        self.con = con

    def execute(self, sql, *a):
        if sql.strip().upper().startswith("PRAGMA QUICK_CHECK"):
            return self.con.execute("SELECT '*** in database main *** Page 7: btree damaged'")
        return self.con.execute(sql, *a)

    def __getattr__(self, k):
        return getattr(self.con, k)


class CheckQuiet(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.close)
        self.addCleanup(self.L.repo.cleanup)

    def check(self, con=None, **kw):
        self.L.commit()
        return quiet(self.L.module.check, con or self.L.con, **kw)

    def test_check_quiet(self):
        L = self.L
        bad, out = self.check()
        self.assertEqual(bad, 0, out)
        self.assertIn("    0  ", out)  # zero lines without quiet
        bad, out = self.check(quiet=True)
        self.assertEqual((bad, out), (0, ""))

        # every error-tier name in ledger_checks is a view of the ledger
        views = {r[0] for r in L.con.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        for v, label, tier in L.module.ledger_checks(L.con):
            self.assertIn(v, views)

        # integrity failure: counted and printed even with quiet
        bad, out = self.check(FakeIntegrity(L.con), quiet=True)
        self.assertEqual(bad, 1)
        self.assertIn("quick_check: *** in database main *** Page 7: btree damaged", out)

        # a broken reference (inserted with foreign keys off)
        L.x("PRAGMA foreign_keys = OFF")
        L.x("INSERT INTO review_goals(review_id, goal_id) VALUES (999, 999)")
        L.commit()
        bad, out = self.check(quiet=True)
        self.assertGreaterEqual(bad, 1)
        self.assertIn("foreign_key_check: 2 broken references", out)
        L.x("DELETE FROM review_goals WHERE review_id=999")
        L.x("PRAGMA foreign_keys = ON")

        # each error view: counted, at most 5 sample rows, quiet hides only the zero lines
        L.x("CREATE TEMP VIEW v_test_seven AS WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<7) "
            "SELECT i FROM n")
        L.x("INSERT INTO ledger_checks(view, label, tier, position) VALUES ('v_test_seven', 'Seven test rows', "
            "'error', 999)")
        bad, out = self.check(quiet=True)
        self.assertEqual(bad, 1, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "    7  Seven test rows  (v_test_seven)")
        self.assertEqual(len(lines), 6)  # the count line and 5 samples
        self.assertNotIn("    0  ", out)
        L.x("UPDATE ledger_checks SET tier='warning' WHERE view='v_test_seven'")
        bad, out = self.check(quiet=True)
        self.assertEqual(bad, 0)
        self.assertIn("(v_test_seven)  warning", out)
        L.x("DELETE FROM ledger_checks WHERE view='v_test_seven'")

    def test_cli_check_quiet_flag(self):
        p = self.L.run("check", "--quiet")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout, "")
        p = self.L.run("check")
        self.assertIn("quick_check: ok", p.stdout)


class GatesCommand(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.close)
        self.addCleanup(self.L.repo.cleanup)

    def gates(self):
        return self.L.run_ok("gates").stdout.splitlines()

    def test_cli_gates_lists_open_and_answered_only(self):
        L = self.L
        self.assertEqual(self.gates(), ["no open gates"])
        L.gate("GATE-0001", title="Second asked", created_at="2026-11-02T09:00:00.000Z")
        L.gate("GATE-0002", title="First asked", created_at="2026-11-01T09:00:00.000Z", status="answered",
               answer_value="yes", answer_text="and quickly", options=json.dumps([{"value": "yes", "label": "Yes"}]))
        L.gate("GATE-0003", title="Applied one", status="applied")
        L.gate("GATE-0004", title="Withdrawn one", status="withdrawn")
        out = self.gates()
        self.assertEqual(out, ["GATE-0002  [answered] decision: First asked -> yes and quickly",
                               "GATE-0001  [open] decision: Second asked"])

        # the rest of the owner queue: a waiting step and an open decision
        rid = L.review("R-0001")
        pid = L.plan("P-0001", reviews=[rid])
        ph = L.phase(pid, 1)
        L.step(pid, ph, 4, title="Pick the store", status="waiting-owner", owner_need="decision",
               owner_question="SQLite or files?")
        L.decision("D-0001", question="Keep data out of the repo?")
        out = self.gates()
        self.assertIn("step P-0001 #4 [decision] Pick the store: SQLite or files?", out)
        self.assertIn("decision D-0001 [open] Keep data out of the repo?", out)
        L.x("UPDATE gates SET status='withdrawn' WHERE status IN ('open','answered')")
        out = self.gates()
        self.assertEqual(out[0], "no open gates")
        self.assertEqual(len(out), 3)


class QueryCommand(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.close)
        self.addCleanup(self.L.repo.cleanup)
        self.L.design_goal("G-01", text="Every rule is a setting.")
        self.L.design_goal("G-02", text="Tabs\tand\nnew lines")
        self.L.commit()

    def digest(self):
        self.L.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return hashlib.sha256(self.L.db.read_bytes()).hexdigest()

    def test_query_prints_rows(self):
        p = self.L.run_ok("query", "SELECT code, text FROM design_goals ORDER BY code")
        self.assertEqual(p.stdout.splitlines(), ["code\ttext", "G-01\tEvery rule is a setting.",
                                                 "G-02\tTabs\\tand\\nnew lines"])
        p = self.L.run_ok("query", "--json", "WITH g AS (SELECT code FROM design_goals) SELECT count(*) n FROM g")
        self.assertEqual(json.loads(p.stdout), {"columns": ["n"], "rows": [[2]], "truncated": False})
        p = self.L.run_ok("query", "--readonly", "SELECT 1 AS one")
        self.assertEqual(p.stdout, "one\n1\n")

    def test_query_readonly_refuses_writes(self):
        before = self.digest()
        for sql in ("INSERT INTO design_goals(code, text) VALUES ('G-09', 'x')",
                    "UPDATE design_goals SET text='changed'",
                    "DELETE FROM design_goals",
                    "ATTACH DATABASE '/tmp/other.sqlite' AS o",
                    "PRAGMA journal_mode=DELETE",
                    "PRAGMA user_version=5",
                    "SELECT 1; DELETE FROM design_goals",
                    "CREATE TABLE t(x)"):
            p = self.L.run("query", sql)
            self.assertEqual(p.returncode, 1, sql)
            self.assertIn("query:", p.stderr, sql)
            self.assertEqual(p.stdout, "", sql)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.L.scalar("SELECT count(*) FROM design_goals"), 2)


if __name__ == "__main__":
    unittest.main()
