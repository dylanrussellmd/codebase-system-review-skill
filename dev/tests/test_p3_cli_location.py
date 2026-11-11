"""A ledger kept outside the repository (plan P-0001 step 53, OWN-1).

With CODE_REVIEW_DIR set, the ledger, its copy of ledger.py and its dump live in that folder and nothing is written
inside the project repository; --db still wins over the variable. init records the folder in meta ledger_location,
and check warns when the ledger is used from another place.
"""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import ScratchRepo, run_ledger


class LedgerLocation(unittest.TestCase):

    def setUp(self):
        base = fixtures.temp_dir("loc-")
        self.repo = ScratchRepo(base / "proj", exclude_ledger=False)
        self.repo.write("a.py", "x = 1\n")
        self.repo.commit("one")
        self.outside = (base / "reviews" / "proj").resolve()
        self.env = fixtures.git_env({"CODE_REVIEW_DIR": str(self.outside)})

    def run_(self, *args, env=None):
        return run_ledger(*args, cwd=self.repo.path, env=env or self.env)

    def assertRepoClean(self):
        self.assertEqual(self.repo.git("status", "--porcelain", "--ignored"), "")

    def test_ledger_location_override(self):
        p = self.run_("init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        db = self.outside / "ledger.sqlite"
        self.assertIn(f"ledger at {db} (created)", p.stdout)
        self.assertEqual(sorted(x.name for x in self.outside.iterdir()), ["ledger.py", "ledger.sqlite"])
        for args in (["check"], ["gates"], ["dump"], ["fill-refs"]):
            with self.subTest(args=args):
                p = self.run_(*args)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.outside / "ledger.sql").exists())
        srv = fixtures.ServerThread(root=self.repo.path, db=db, mode="process")
        with srv:
            self.assertEqual(srv.get("/api/overview").status, 200)
        self.assertRepoClean()
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()[0],
                         str(self.outside))
        con.close()
        # The copy in that folder finds the same ledger.
        p = run_ledger("check", cwd=self.repo.path, env=self.env, ledger_py=self.outside / "ledger.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRepoClean()

    def test_db_flag_wins_over_env(self):
        other = fixtures.temp_dir("dbflag-") / "x.sqlite"
        p = self.run_("--db", other, "init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(other.exists())
        self.assertFalse(self.outside.exists())
        self.assertRepoClean()

    def test_check_warns_when_ledger_moved(self):
        self.assertEqual(self.run_("init").returncode, 0)
        moved = self.outside.parent / "moved"
        self.outside.rename(moved)
        env = fixtures.git_env({"CODE_REVIEW_DIR": str(moved)})
        p = self.run_("check", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"warning: this ledger was set up at {self.outside}", p.stderr)
        p = self.run_("init", env=env)
        self.assertIn(f"now records {moved}", p.stdout)
        self.assertNotIn("warning", self.run_("check", env=env).stderr)


if __name__ == "__main__":
    unittest.main()
