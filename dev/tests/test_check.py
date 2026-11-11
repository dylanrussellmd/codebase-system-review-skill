"""Tests for check() and the `ledger.py check` command, and for the MUST_BE_EMPTY list against the schema.

check() runs PRAGMA integrity_check and foreign_key_check and counts the rows of each must-be-empty view; the CLI
exits 1 when anything is found. Each must-be-empty view gets one offending fixture. `# characterization:` comments
mark assertions that pin a known defect, naming the issue, so the step that fixes it changes them on purpose.
"""
import contextlib
import io
import re
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, temp_dir

SCHEMA_SQL = fixtures.REPO_ROOT / "references" / "schema.sql"
SKILL_MD = fixtures.REPO_ROOT / "SKILL.md"


def new_ledger():
    return Ledger(temp_dir("check-"))


def offend(L, view, count=1):
    """Insert `count` rows that make `view` (one of MUST_BE_EMPTY) non-empty, and nothing that fills another one."""
    r1 = L.review()
    if view == "v_unmatched_findings":
        for _ in range(count):
            L.finding(r1)
    elif view == "v_unchecked_known_issues":
        for _ in range(count):
            L.issue(first_seen_review_id=r1, domain_code="spec")
        r2 = L.review()                       # starts after the issues were opened (Clock)
        L.review_domain(r2, "spec")
    elif view == "v_untraceable_refs":
        for _ in range(count):
            iid = L.issue(first_seen_review_id=r1)
            L.location(L.finding(r1, issue_id=iid))   # no review commit, no excerpt
    elif view == "v_uncovered_findings":
        plan = L.plan(reviews=[r1])
        for _ in range(count):
            iid = L.issue(first_seen_review_id=r1)
            L.finding(r1, issue_id=iid)
        assert plan
    elif view == "v_coverage_problems":
        plan = L.plan()
        ph = L.phase(plan, 1)
        for n in range(1, count + 1):
            L.step(plan, ph, n)               # closes nothing
    else:
        raise AssertionError(f"no offending fixture for {view}")
    L.commit()


def corrupt_integrity(db):
    """Make PRAGMA integrity_check fail for real: an index whose stored order disagrees with its declared one."""
    con = sqlite3.connect(str(db), isolation_level=None)
    con.execute("CREATE TABLE scratch(x)")
    con.execute("CREATE INDEX scratch_ix ON scratch(x)")
    con.executemany("INSERT INTO scratch VALUES (?)", [(i,) for i in range(20)])
    con.execute("PRAGMA writable_schema=ON")
    con.execute("UPDATE sqlite_schema SET sql='CREATE INDEX scratch_ix ON scratch(x DESC)' WHERE name='scratch_ix'")
    con.execute("PRAGMA writable_schema=OFF")
    con.close()


def break_foreign_key(db):
    """A dangling reference, inserted on a connection with foreign keys off."""
    con = sqlite3.connect(str(db), isolation_level=None)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO actors(kind, name, parent_actor_id) VALUES ('tool', 'orphan', 9999)")
    con.close()


class CheckFunction(unittest.TestCase):
    """check() called directly on the built module."""

    @classmethod
    def setUpClass(cls):
        cls.mod = fixtures.load_ledger_module()
        cls.views = [v for v, _ in cls.mod.MUST_BE_EMPTY]

    def run_check(self, L, quiet=False):
        L.commit()
        con = self.mod.connect(L.db)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                n = self.mod.check(con, quiet=quiet)
            return n, out.getvalue()
        finally:
            con.close()

    def test_check_clean_ledger(self):
        with new_ledger() as L:
            n, out = self.run_check(L)
        self.assertEqual(n, 0)
        self.assertEqual(out.splitlines(), [f"    0  {label}" for _, label in self.mod.MUST_BE_EMPTY])

    def test_check_quiet(self):
        with new_ledger() as L:
            self.assertEqual(self.run_check(L, quiet=True), (0, ""))
            offend(L, "v_coverage_problems")
            n, out = self.run_check(L, quiet=True)
        self.assertEqual(n, 1)
        self.assertNotIn("    0  ", out)
        self.assertIn("(v_coverage_problems)", out)

    def test_check_counts_integrity_and_fk(self):
        with self.subTest("integrity failure"):
            with new_ledger() as L:
                L.close()
                corrupt_integrity(L.db)
                L.con = L.connect()
                n, out = self.run_check(L)
            # 2: the integrity failure, and (plan step 34) the scratch table and index the corruption needs, which
            # are not in the shipped schema.
            self.assertEqual(n, 2)
            self.assertIn("extra index scratch_ix", out)
            # Only the first integrity_check message is printed (fetchone).
            self.assertEqual([l for l in out.splitlines() if l.startswith("integrity_check:")],
                             ["integrity_check: row 1 missing from index scratch_ix"])
        with self.subTest("broken foreign key"):
            with new_ledger() as L:
                L.close()
                break_foreign_key(L.db)
                L.con = L.connect()
                n, out = self.run_check(L)
            self.assertEqual(n, 1)
            self.assertIn("foreign_key_check: 1 broken references", out)
        with self.subTest("both, plus a view: the count is one per failing check"):
            with new_ledger() as L:
                offend(L, "v_unmatched_findings", count=3)
                L.close()
                corrupt_integrity(L.db)
                break_foreign_key(L.db)
                L.con = L.connect()
                n, out = self.run_check(L)
            self.assertEqual(n, 4)  # integrity, foreign key, the view, and the scratch objects (schema drift)

    def test_check_each_must_be_empty_view(self):
        for view, label in self.mod.MUST_BE_EMPTY:
            with self.subTest(view=view):
                with new_ledger() as L:
                    offend(L, view)
                    n, out = self.run_check(L)
                self.assertEqual(n, 1, out)
                lines = out.splitlines()
                self.assertIn(f"    1  {label}  ({view})", lines)
                for other, other_label in self.mod.MUST_BE_EMPTY:
                    if other != view:
                        self.assertIn(f"    0  {other_label}", lines)
                samples = [l for l in lines if l.startswith("        ")]
                self.assertEqual(len(samples), 1)

    def test_check_shows_at_most_five_samples(self):
        with new_ledger() as L:
            offend(L, "v_unmatched_findings", count=7)
            n, out = self.run_check(L, quiet=True)
        self.assertEqual(n, 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], "    7  Findings not matched to an issue  (v_unmatched_findings)")
        self.assertEqual(len(lines), 6)
        for l in lines[1:]:
            self.assertTrue(l.startswith("        {"), l)
            self.assertLessEqual(len(l), 8 + 200)

    def test_must_be_empty_views_exist(self):
        with new_ledger() as L:
            views = {r["name"] for r in L.all("SELECT name FROM sqlite_schema WHERE type='view'")}
        self.assertEqual(len(self.views), 5)
        for v in self.views:
            self.assertIn(v, views)

    def test_must_be_empty_list_matches_schema_comments(self):
        lines = SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        commented = set()
        for i, line in enumerate(lines):
            m = re.match(r"CREATE VIEW (\w+)", line)
            if not m:
                continue
            j, comment = i - 1, []
            while j >= 0 and lines[j].startswith("--"):
                comment.append(lines[j])
                j -= 1
            if "must be empty" in " ".join(comment).lower():
                commented.add(m.group(1))
        # characterization: ISS-0178 — v_coverage_problems is in MUST_BE_EMPTY and SKILL.md's must-be-empty list,
        # but its schema.sql comment does not say "must be empty". The fix adds the words; then this becomes
        # assertEqual(commented, set(self.views)).
        self.assertEqual(commented, set(self.views) - {"v_coverage_problems"})
        skill = SKILL_MD.read_text(encoding="utf-8")
        for v in self.views:
            self.assertIn(f"`{v}`", skill)


class CheckCommand(unittest.TestCase):
    """`ledger.py check` as a subprocess on the fresh build."""

    @classmethod
    def setUpClass(cls):
        cls.mod = fixtures.load_ledger_module()

    def test_cli_check_clean_exit0(self):
        with new_ledger() as L:
            p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(l.startswith("    0  ") for l in lines), lines)

    def test_cli_check_each_view_exit1(self):
        for view, label in self.mod.MUST_BE_EMPTY:
            with self.subTest(view=view):
                with new_ledger() as L:
                    offend(L, view)
                    p = L.run("check")
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn(f"({view})", p.stdout)
                self.assertIn(f"    1  {label}  ({view})", p.stdout.splitlines())

    def test_check_fails_on_each_view(self):
        # DB-T3: the exit code and the view name, with several offending rows at once.
        for view, _ in self.mod.MUST_BE_EMPTY:
            with self.subTest(view=view):
                with new_ledger() as L:
                    offend(L, view, count=2)
                    p = L.run("check")
                self.assertEqual(p.returncode, 1)
                self.assertRegex(p.stdout, rf"(?m)^\s+2  .*\({view}\)$")

    def test_cli_check_integrity_and_fk_exit1(self):
        with new_ledger() as L:
            L.close()
            break_foreign_key(L.db)
            p = L.run("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("foreign_key_check: 1 broken references", p.stdout)

    def test_cli_check_refuses_other_schema_version(self):
        # Was test_current_cli_check_runs_on_other_schema_version (characterization of ISS-0035: check ran on a
        # schema-2 ledger and exited 0). Plan step 33: every command opens the ledger through open_ledger().
        with new_ledger() as L:
            L.x("UPDATE meta SET value='2' WHERE key='schema_version'")
            p = L.run("check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("is schema 2; this ledger.py only knows 1. Use a newer ledger.py.", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
