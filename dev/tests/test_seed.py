"""The demo data set (fixtures.seed_demo and the dev/tests/seed_demo.py CLI) asserts what it shows."""
import hashlib
import re
import subprocess
import sys
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import DemoExpectationError, Ledger, finish_demo, seed_demo

SEED_CLI = fixtures.REPO_ROOT / "dev" / "tests" / "seed_demo.py"
# sha256 of the personal first name the demo used to hard-code (kept as a hash so the name is not in the repo).
OLD_OWNER_NAME_SHA256 = "c0e7aec81a1a194e9f54f6b297f6544041188c741ae8f55c2133b5c510a7dc1d"


def run_cli(*args):
    return subprocess.run([sys.executable, str(SEED_CLI), *map(str, args)], capture_output=True, text=True,
                          timeout=120, env=fixtures.git_env())


class SeedDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.ids = seed_demo(cls.L)

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def status(self, code):
        return self.L.scalar("SELECT status FROM issues WHERE code=?", (code,))

    def test_seed_demo_end_state(self):
        L = self.L
        self.assertEqual(self.status("ISS-0001"), "verified")
        self.assertEqual(L.scalar("SELECT times_seen FROM issues WHERE code='ISS-0002'"), 2)
        self.assertEqual(self.status("ISS-0004"), "open")
        self.assertEqual([r["status"] for r in L.all("SELECT status FROM gates ORDER BY code")], ["open", "open"])
        self.assertEqual(L.count("v_coverage_problems"), 0)
        self.assertEqual(sorted(self.ids["issues"]), ["ISS-0001", "ISS-0002", "ISS-0003", "ISS-0004"])
        self.assertEqual(L.count("findings"), 5, "the regression probe must be rolled back")

    def test_seed_demo_asserts(self):
        L, ids = self.L, self.ids
        c1, c2 = ids["commits"]
        self.assertEqual(L.repo.commits, [c1, c2])
        # dependency order: the dependency that closed a cycle was refused and left nothing behind
        self.assertEqual(L.all("SELECT step_id, depends_on_step_id FROM step_dependencies ORDER BY 1"),
                         [{"step_id": ids["steps"][2], "depends_on_step_id": ids["steps"][1]},
                          {"step_id": ids["steps"][3], "depends_on_step_id": ids["steps"][1]}])
        # every location took its review's commit
        for r in L.all("""SELECT rv.code, fl.commit_sha FROM finding_locations fl JOIN findings f ON f.id=fl.finding_id
                          JOIN reviews rv ON rv.id=f.review_id"""):
            self.assertEqual(r["commit_sha"], c1 if r["code"] == "R-0001" else c2)
        # the exact event history the triggers write for the demo (schema 3: text edits, checks and created rows
        # are events too, and reviews move through every status on the way)
        self.assertEqual(
            [e[2:] for e in L.events("issue", ids["issues"]["ISS-0001"])],
            [("opened", None, "open"),
             ("current-state-changed", None, "make_code uses random.randint, which is not suitable for secrets."),
             ("seen", None, "new"), ("after-state-changed", None, "Codes come from secrets.randbelow."),
             ("status-changed", "open", "fixed"), ("status-changed", "fixed", "verified"), ("checked", None, "fixed")])
        self.assertEqual([e[2] for e in L.events("issue", ids["issues"]["ISS-0002"])],
                         ["opened", "current-state-changed", "seen", "current-state-changed", "seen", "checked"])
        self.assertEqual([e[3:] for e in L.events("step") if e[2] == "status-changed"],
                         [("not-started", "doing"), ("doing", "done"), ("not-started", "waiting-owner")])
        walk = [("planning", "awaiting-approval"), ("awaiting-approval", "approved"), ("approved", "running")]
        self.assertEqual([e[3:] for e in L.events("review") if e[2] == "status-changed"],
                         walk + [("running", "verifying"), ("verifying", "indexing"), ("indexing", "consolidating"),
                                 ("consolidating", "handed-off")] + walk)

    def test_seed_demo_check_is_not_vacuous(self):
        L = self.L
        self.assertEqual(L.count("v_unchecked_known_issues"), 0)
        L.con.execute("SAVEPOINT probe")
        try:
            L.x("DROP TRIGGER tr_check_no_delete")   # checks are append-only (schema 3); undone with the probe
            L.x("DELETE FROM review_issue_checks WHERE review_id=?", (self.ids["reviews"]["R-0002"],))
            rows = L.all("SELECT review, issue FROM v_unchecked_known_issues ORDER BY issue")
            self.assertEqual(rows, [{"review": "R-0002", "issue": c} for c in ("ISS-0001", "ISS-0002", "ISS-0003")])
        finally:
            L.con.execute("ROLLBACK TO probe")
            L.con.execute("RELEASE probe")

    def test_seed_demo_timestamps_are_consistent(self):
        stamp = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        for table, col in (("reviews", "started_at"), ("issues", "opened_at"), ("findings", "created_at"),
                           ("gates", "created_at"), ("review_issue_checks", "checked_at")):
            for v in [r[0] for r in self.L.x(f"SELECT {col} FROM {table}")]:
                self.assertRegex(v, stamp, f"{table}.{col}")
        r2 = self.L.scalar("SELECT started_at FROM reviews WHERE code='R-0002'")
        self.assertEqual(self.L.count("issues", "first_seen_review_id=? AND opened_at >= ?",
                                      (self.ids["reviews"]["R-0001"], r2)), 0,
                         "R-0001's issues must be opened before R-0002 started")


class SeedDemoEndToEnd(unittest.TestCase):
    def test_seed_demo_runs_and_check_passes(self):
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        seed_demo(L)
        out = finish_demo(L)
        self.assertIn("0  Code references that cannot be traced", out)
        self.assertEqual(L.count("v_untraceable_refs"), 0)
        self.assertEqual(L.count("finding_locations", "excerpt IS NULL OR blob_sha IS NULL OR permalink IS NULL"), 0)
        tags = L.repo.tags()
        self.assertEqual(sorted(tags), ["review/R-0001", "review/R-0002"])

    def test_seed_demo_refs_resolve(self):
        L = Ledger()
        self.addCleanup(L.close)
        seed_demo(L)
        p = L.run("fill-refs")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("filled 5 references, 0 could not be read", p.stdout)
        self.assertEqual(L.scalar("SELECT excerpt FROM finding_locations WHERE path='src/auth.py' ORDER BY id"),
                         'import random\ndef make_code():\n    return f"{random.randint(0, 999999):06d}"')

    def test_seed_demo_needs_a_repo(self):
        L = Ledger(fixtures.temp_dir("plain-"))
        self.addCleanup(L.close)
        with self.assertRaises(ValueError):
            seed_demo(L)

    def test_seed_demo_end_to_end(self):
        target = fixtures.temp_dir("cli-") / "demo"
        p = run_cli(target)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("serve", p.stdout)
        self.assertTrue((target / ".code-review" / "ledger.sqlite").exists())
        self.assertTrue((target / ".code-review" / "ledger.py").exists())
        L = fixtures.run_ledger("--root", target, "check")
        self.assertEqual(L.returncode, 0, L.stdout)
        p = fixtures.run_ledger("--root", target, "gates")
        self.assertIn("GATE-0001", p.stdout)

    def test_seed_demo_refuses_a_non_empty_folder(self):
        target = fixtures.temp_dir("cli-")
        (target / "keep.txt").write_text("mine\n")
        p = run_cli(target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not empty", p.stderr)
        self.assertEqual(sorted(x.name for x in target.iterdir()), ["keep.txt"])

    def test_seed_demo_exits_nonzero_on_failed_expectation(self):
        # A ledger.py whose schema lost the step-order rule: the demo's "a dependency cycle is refused"
        # expectation fails, and the CLI must say so and exit 1 (it used to print FAIL and exit 0).
        mod = fixtures.load_ledger_module()
        text = fixtures.built_ledger_path().read_text(encoding="utf-8")
        broken_schema = mod.SCHEMA + "\nDROP TRIGGER tr_dep_order;\n"
        self.assertEqual(text.count(mod.SCHEMA), 1)
        bad = fixtures.temp_dir("bad-") / "ledger.py"
        bad.write_text(text.replace(mod.SCHEMA, broken_schema), encoding="utf-8")
        with self.assertRaises(DemoExpectationError):
            L = Ledger(ledger_py=bad)
            self.addCleanup(L.close)
            seed_demo(L)
        p = run_cli(fixtures.temp_dir("cli-") / "demo", "--ledger", bad)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("FAIL", p.stderr)
        self.assertIn("dependency cycle", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_seed_demo_has_no_personal_name(self):
        for path in (SEED_CLI, fixtures.REPO_ROOT / "dev" / "tests" / "fixtures.py"):
            words = set(re.findall(r"[A-Za-z]+", path.read_text(encoding="utf-8")))
            hits = [w for w in words if hashlib.sha256(w.lower().encode()).hexdigest() == OLD_OWNER_NAME_SHA256]
            self.assertEqual(hits, [], f"{path.name} still contains the owner's personal name")
        self.assertEqual(fixtures.DEMO_OWNER, "Demo Owner")


if __name__ == "__main__":
    unittest.main()
