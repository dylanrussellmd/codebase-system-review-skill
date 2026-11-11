"""init's SQLite preflight and its ledger.py hash line, the note for a SQLite older than 3.34, a reader that closes
early, and the commands set-doc-url and plan-add-review."""
import hashlib
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from dev.tests import fixtures
from dev.tests._p5_writes2_helpers import ok, project, refused, run
from dev.tests.fixtures import built_ledger_path, run_ledger, temp_dir


def fresh_project(name="proj"):
    d = temp_dir("w1cli-") / name
    d.mkdir()
    return d


def init_in_process(mod, root):
    """mod.main() for `init` on root; returns the exit message or code (None when it finished)."""
    with mock.patch.object(sys, "argv", ["ledger.py", "--root", str(root), "init"]):
        try:
            mod.main()
        except SystemExit as e:
            return e.code
    return None


class Preflight(unittest.TestCase):
    def setUp(self):
        self.mod = fixtures.load_ledger_module(fresh=True)

    def assertRefused(self, d, code, *needles):
        self.assertIsInstance(code, str)
        self.assertEqual(len(code.splitlines()), 1)
        for n in needles:
            self.assertIn(n, code)
        self.assertEqual(list(d.iterdir()), [], "init left something behind")

    def test_init_reports_missing_json1(self):
        d = fresh_project()
        real = sqlite3.connect

        class NoJson:
            def execute(self, sql, *a):
                raise sqlite3.OperationalError("no such function: json_valid")

            def close(self):
                pass

        with mock.patch.object(self.mod.sqlite3, "connect",
                               lambda *a, **k: NoJson() if a[:1] == (":memory:",) else real(*a, **k)):
            code = init_in_process(self.mod, d)
        self.assertRefused(d, code, "init:", sqlite3.sqlite_version, "JSON")

    def test_init_reports_old_sqlite(self):
        d = fresh_project()
        with mock.patch.object(self.mod.sqlite3, "sqlite_version_info", (3, 30, 1)), \
                mock.patch.object(self.mod.sqlite3, "sqlite_version", "3.30.1"):
            code = init_in_process(self.mod, d)
        self.assertRefused(d, code, "init:", "3.30.1", "3.31")

    def test_old_sqlite_note_names_the_version(self):
        """Below 3.34 a refused CHECK names only its table: init and check say so once, on stderr."""
        with mock.patch.object(self.mod.sqlite3, "sqlite_version_info", (3, 33, 0)), \
                mock.patch.object(self.mod.sqlite3, "sqlite_version", "3.33.0"):
            w = self.mod.old_sqlite_note()
        self.assertIn("3.33.0", w)
        self.assertIn("3.34", w)
        self.assertEqual(len(w.splitlines()), 1)
        with mock.patch.object(self.mod.sqlite3, "sqlite_version_info", (3, 34, 0)):
            self.assertIsNone(self.mod.old_sqlite_note())

    def test_init_and_check_note_this_sqlite_only_when_old(self):
        d = fresh_project()
        old = sqlite3.sqlite_version_info < (3, 34, 0)
        for cmd in ("init", "check"):
            p = run_ledger("--root", d, cmd)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            noted = [l for l in p.stderr.splitlines() if l.startswith("note: SQLite")]
            self.assertEqual(len(noted), 1 if old else 0, p.stderr)
            self.assertEqual(p.stderr.splitlines()[-1:], noted, "printed after the command's own output")
        p = run_ledger("--root", d, "gates")
        self.assertNotIn("note: SQLite", p.stderr)
        p = run_ledger("--root", d / "nowhere", "check")
        self.assertNotIn("note: SQLite", p.stderr, "a refusal stays one line")


class InitCopy(unittest.TestCase):
    def test_init_prints_copy_sha256(self):
        d = fresh_project()
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        home = d / ".code-review" / "ledger.py"
        digest = hashlib.sha256(home.read_bytes()).hexdigest()
        self.assertEqual(digest, hashlib.sha256(built_ledger_path().read_bytes()).hexdigest())
        self.assertIn(f"copied ledger.py to {home} (sha256 {digest})", p.stdout.splitlines())


class EarlyReader(unittest.TestCase):
    def pipe_into_closed_reader(self, *args):
        d = fresh_project()
        self.assertEqual(run_ledger("--root", d, "init").returncode, 0)
        p = subprocess.Popen([sys.executable, str(built_ledger_path()), "--root", str(d), *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=fixtures.git_env())
        p.stdout.readline()
        p.stdout.close()  # what `| head -1` does
        err = p.stderr.read().decode()
        p.stderr.close()
        return p.wait(timeout=60), err

    def test_reader_that_closes_early_ends_quietly(self):
        """`ledger.py schema | head -1`: exit 0 and nothing on stderr, no BrokenPipeError traceback."""
        code, err = self.pipe_into_closed_reader("schema")
        self.assertEqual((code, err), (0, ""))

    def test_reader_that_closes_early_on_a_ledger_command(self):
        code, err = self.pipe_into_closed_reader("query", "SELECT name, sql FROM sqlite_master")
        self.assertEqual((code, err), (0, ""))


class DocUrl(unittest.TestCase):
    def test_set_doc_url_on_a_review_and_a_plan(self):
        repo, L, sha = project(self)
        self.assertEqual(ok(L, "set-doc-url", "R-0001", "https://docs.example/review", "--actor", "A-0002"),
                         ["R-0001: document https://docs.example/review"])
        self.assertEqual(L.scalar("SELECT summary_doc_url FROM reviews WHERE code='R-0001'"),
                         "https://docs.example/review")
        plan = ok(L, "new-plan", "--title", "Fix it", "--review", "R-0001", "--actor", "A-0002")[0]
        self.assertEqual(ok(L, "set-doc-url", plan, "https://docs.example/plan"),
                         [f"{plan}: document https://docs.example/plan"])
        self.assertEqual(L.scalar("SELECT doc_url FROM plans WHERE code=?", (plan,)), "https://docs.example/plan")
        self.assertIsNone(L.scalar("SELECT page_url FROM plans WHERE code=?", (plan,)))

    def test_set_doc_url_refusals(self):
        repo, L, sha = project(self)
        self.assertIn("http", refused(self, L, "set-doc-url", "R-0001", "javascript:alert(1)"))
        self.assertIn("http", refused(self, L, "set-doc-url", "R-0001", "docs/review.md"))
        self.assertIn("R-0001 or P-0001", refused(self, L, "set-doc-url", "ISS-0001", "https://docs.example/x"))
        self.assertIn("R-0009", refused(self, L, "set-doc-url", "R-0009", "https://docs.example/x"))


class PlanAddReview(unittest.TestCase):
    def two_reviews(self):
        repo, L, sha = project(self)
        ok(L, "new-review", "--title", "Second", "--scope", "second", "--previous", "R-0001", "--actor", "A-0002")
        plan = ok(L, "new-plan", "--title", "Fix it", "--review", "R-0001", "--actor", "A-0002")[0]
        return L, plan

    def covered(self, L, plan):
        return [r["code"] for r in L.all("SELECT r.code FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id "
                                         "JOIN plans p ON p.id = pr.plan_id WHERE p.code=? ORDER BY r.code", (plan,))]

    def test_plan_add_review(self):
        L, plan = self.two_reviews()
        self.assertEqual(ok(L, "plan-add-review", plan, "R-0002", "--actor", "A-0002"),
                         [f"{plan}: covers R-0001, R-0002"])
        self.assertEqual(self.covered(L, plan), ["R-0001", "R-0002"])

    def test_plan_add_review_refusals(self):
        L, plan = self.two_reviews()
        self.assertIn("already covers", refused(self, L, "plan-add-review", plan, "R-0001"))
        self.assertIn("R-0007", refused(self, L, "plan-add-review", plan, "R-0007"))
        self.assertIn("P-0009", refused(self, L, "plan-add-review", "P-0009", "R-0002"))
        ok(L, "plan-status", plan, "abandoned", "--note", "Replaced.", "--actor", "A-0002")
        self.assertIn("abandoned", refused(self, L, "plan-add-review", plan, "R-0002"))
        self.assertEqual(self.covered(L, plan), ["R-0001"])


if __name__ == "__main__":
    unittest.main()
