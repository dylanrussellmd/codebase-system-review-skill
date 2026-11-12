"""Schema 9 (plan P-0001 step 129): migration 0009-interview-goals upgrades a ledger of the previous schema. Existing
design goals become approved whole-project goals (approved by the owner actor, made if the ledger has none, at the
time they were adopted), the interview and goal tables exist, events take the new entity types, and the result
matches the shipped schema."""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M


class Migrate8To9(unittest.TestCase):
    def test_migrate_keeps_goals_as_approved_whole_project_goals(self):
        project_dir, db = M.restore_v1()
        before = sqlite3.connect(str(db))
        goals = before.execute("SELECT code, text, adopted_at FROM design_goals ORDER BY code").fetchall()
        before.close()
        self.assertTrue(goals)
        ledger_py = fixtures.built_ledger_path()
        p = fixtures.run_ledger("--root", project_dir, "migrate", ledger_py=ledger_py)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0009-interview-goals", p.stdout)
        mod = fixtures.load_ledger_module(ledger_py)
        c = sqlite3.connect(str(db))
        try:
            self.assertEqual(mod.verify_schema(c), [])
            got = c.execute("""SELECT g.code, g.text, g.status, g.domain_code, a.kind, g.approved_at
                               FROM design_goals g JOIN actors a ON a.id = g.approved_by_actor_id
                               ORDER BY g.code""").fetchall()
            self.assertEqual(got, [(code, text, "approved", None, "owner", at) for code, text, at in goals])
            self.assertEqual(c.execute("SELECT count(*) FROM v_goals_for_brief").fetchone()[0], len(goals))
            for t in ("interview_questions", "interview_answers", "goal_sources", "step_goals", "goal_statuses"):
                c.execute(f"SELECT * FROM {t}")
            self.assertEqual([r[0] for r in c.execute("SELECT value FROM goal_statuses ORDER BY position")],
                             ["proposed", "approved", "rejected", "retired"])
            self.assertEqual(c.execute("SELECT next_code FROM v_next_codes WHERE prefix='IQ'").fetchone()[0],
                             "IQ-0001")
        finally:
            c.close()
        p = fixtures.run_ledger("--root", project_dir, "goals", ledger_py=ledger_py)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(goals[0][0], p.stdout)
        p = fixtures.run_ledger("--root", project_dir, "interview", ledger_py=ledger_py)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no interview questions", p.stdout)
        p = fixtures.run_ledger("--root", project_dir, "check", ledger_py=ledger_py)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
