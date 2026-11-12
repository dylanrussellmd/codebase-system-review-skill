"""The review-tool migrations (plan P-0001 steps 135-143) upgrade an older ledger: after `migrate` the frozen v1 demo
ledger has each new table with its seed, matches the shipped schema and passes the commands that read them."""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M


class MigrateReviewTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_dir, cls.db = M.restore_v1()
        cls.ledger_py = fixtures.built_ledger_path()
        cls.migrated = fixtures.run_ledger("--root", cls.project_dir, "migrate", ledger_py=cls.ledger_py)
        cls.mod = fixtures.load_ledger_module(cls.ledger_py)

    def setUp(self):
        self.assertEqual(self.migrated.returncode, 0, self.migrated.stdout + self.migrated.stderr)
        self.con = sqlite3.connect(str(self.db))
        self.addCleanup(self.con.close)

    def run_ok(self, *args):
        p = fixtures.run_ledger("--root", self.project_dir, *args, ledger_py=self.ledger_py)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def test_result_matches_the_shipped_schema(self):
        self.assertEqual(self.mod.verify_schema(self.con), [])

    def test_0012_review_environment(self):
        self.assertIn("0012-review-environment", self.migrated.stdout)
        self.assertEqual(self.con.execute("SELECT count(*) FROM review_environment").fetchone()[0], 0)
        review = self.con.execute("SELECT code FROM reviews ORDER BY id LIMIT 1").fetchone()[0]
        self.run_ok("env", "--review", review)
        self.assertGreater(self.con.execute("SELECT count(*) FROM review_environment").fetchone()[0], 8)

    def test_0013_domain_rules(self):
        self.assertIn("0013-domain-rules", self.migrated.stdout)
        fresh = fixtures.Ledger(fixtures.temp_dir("w4-fresh-"))
        self.addCleanup(fresh.close)
        want = [(r["domain_code"], r["rule"]) for r in fresh.all("SELECT * FROM domain_rules ORDER BY 1")]
        self.assertEqual(self.con.execute("SELECT domain_code, rule FROM domain_rules ORDER BY 1").fetchall(), want)
        self.assertEqual(len(want), 28)   # 27, and features (schema 19)
        self.assertEqual(self.con.execute("SELECT count(*) FROM review_domains WHERE proposed_by_rule IS NOT NULL")
                         .fetchone()[0], 0)   # earlier proposals were made by a person

    def test_0014_domain_checks(self):
        self.assertIn("0014-domain-checks", self.migrated.stdout)
        fresh = fixtures.Ledger(fixtures.temp_dir("w4-fresh-"))
        self.addCleanup(fresh.close)
        want = [tuple(r[k] for k in ("code", "severity", "question")) for r in
                fresh.all("SELECT * FROM domain_checks ORDER BY code")]
        self.assertEqual(self.con.execute("SELECT code, severity, question FROM domain_checks ORDER BY code").fetchall(),
                         want)
        self.assertGreater(len(want), 100)
        self.assertEqual(self.con.execute("SELECT count(*) FROM checklist_answers").fetchone()[0], 0)

    def test_0015_agreement_metrics(self):
        self.assertIn("0015-agreement-metrics", self.migrated.stdout)
        got = self.con.execute("SELECT name, unit, better FROM metric_defs WHERE name LIKE '%_agreement' "
                               "ORDER BY name").fetchall()
        self.assertEqual(got, [(n, "%", "higher") for n in ("domain_agreement", "finding_agreement",
                                                            "severity_agreement")])

    def test_0016_step_conflicts(self):
        self.assertIn("0016-step-conflicts", self.migrated.stdout)
        touches = self.con.execute("SELECT count(*) FROM step_touches WHERE source='finding'").fetchone()[0]
        links = self.con.execute("""SELECT count(DISTINCT sf.step_id) FROM step_findings sf
                                    JOIN finding_locations fl ON fl.finding_id = sf.finding_id""").fetchone()[0]
        self.assertGreater(links, 0)
        self.assertGreaterEqual(touches, links)     # the steps of the old plans have their findings' files
        self.assertEqual(self.con.execute("SELECT count(*) FROM step_conflicts").fetchone()[0], 0)
        views = {r[0] for r in self.con.execute("SELECT view FROM ledger_checks")}
        self.assertLessEqual({"v_unresolved_conflicts", "v_late_step_conflicts"}, views)


if __name__ == "__main__":
    unittest.main()
