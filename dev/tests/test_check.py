"""Tests for check() and the `ledger.py check` command, and for the ledger_checks list against the schema.

check() runs PRAGMA integrity_check and foreign_key_check and counts the rows of each view in the ledger's
ledger_checks table (plan step 69; it was the MUST_BE_EMPTY constant); the CLI exits 1 when an error-tier view has
rows (a warning-tier view is listed but does not fail). Each must-be-empty view gets one offending fixture. `# characterization:` comments
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


def seeded_checks():
    """[(view, label, tier)] of a fresh ledger's ledger_checks, in check's order."""
    with new_ledger() as L:
        return [(r["view"], r["label"], r["tier"]) for r in L.all(
            "SELECT view, label, tier FROM ledger_checks ORDER BY position, view")]


def must_be_empty():
    """The error tier as (view, label) pairs: the twin of SKILL.md's must-be-empty list."""
    return [(v, label) for v, label, tier in seeded_checks() if tier == "error"]


def traced(L, fid):
    """Give finding fid a fully traceable location (its review's commit, tagged, one line with its excerpt), so it
    fills no other check (step 76: a finding with no location is untraceable)."""
    L.location(fid, excerpt="x = 1")
    L.x("UPDATE review_repositories SET tag_name = 'review/' || (SELECT code FROM reviews WHERE id = review_id) "
        "WHERE tag_name IS NULL")
    return fid


@contextlib.contextmanager
def without_trigger(L, name):
    """Write rows the shipped triggers would refuse or log (a scratch way to make a history gap); the trigger is
    put back afterwards, so the schema still matches."""
    sql = L.scalar("SELECT sql FROM sqlite_schema WHERE name=?", (name,))
    L.x(f"DROP TRIGGER {name}")
    try:
        yield
    finally:
        L.x(sql)


def offend(L, view, count=1):
    """Insert `count` rows that make `view` (an error-tier check) non-empty, and nothing that fills another one."""
    r1 = L.review()
    if view == "v_unmatched_findings":
        for _ in range(count):
            traced(L, L.finding(r1))
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
            traced(L, L.finding(r1, issue_id=iid))
        assert plan
    elif view == "v_coverage_problems":
        plan = L.plan()
        ph = L.phase(plan, 1)
        for n in range(1, count + 1):
            L.step(plan, ph, n)               # closes nothing
    elif view == "v_unplanned_discoveries":
        plan = L.plan(reviews=[r1])
        ph = L.phase(plan, 1)
        f0 = traced(L, L.finding(r1, issue_id=L.issue(first_seen_review_id=r1)))
        step = L.step(plan, ph, 1, findings=[f0])
        r2 = L.review()                       # discoveries recorded under a review the plan does not cover
        for _ in range(count):
            traced(L, L.finding(r2, issue_id=L.issue(first_seen_review_id=r2), discovered_in_step_id=step))
    elif view == "v_history_gaps":
        with without_trigger(L, "tr_issue_opened"):
            for _ in range(count):
                L.issue(first_seen_review_id=r1)          # no opened event
    elif view == "v_match_problems":
        old = L.issue(first_seen_review_id=r1)
        r2 = L.review()
        for _ in range(count):
            traced(L, L.finding(r2, issue_id=old, match_type="new"))   # 'new' on an issue R-1 opened
    elif view == "v_off_plan_coverage":
        plan = L.plan(reviews=[r1])
        ph = L.phase(plan, 1)
        fs = [traced(L, L.finding(r1, issue_id=L.issue(first_seen_review_id=r1))) for _ in range(count)]
        L.step(plan, ph, 1, findings=fs)
        L.x("DELETE FROM plan_reviews WHERE plan_id=?", (plan,))  # the review left the plan; its links stay
    elif view == "v_done_steps_open_issues":
        plan = L.plan(reviews=[r1])
        ph = L.phase(plan, 1)
        fs = [traced(L, L.finding(r1, issue_id=L.issue(first_seen_review_id=r1, status="planned")))
              for _ in range(count)]
        step = L.step(plan, ph, 1, findings=fs)
        L.update("steps", step, status="done", change_note="Done, but its issues were left planned.")
    elif view == "v_closed_without_reason":
        with without_trigger(L, "tr_issue_closed_needs_reason_ins"):
            for _ in range(count):
                L.insert("issues", code=L._code("ISS-"), title="Declined", domain_code="spec", kind="defect",
                         severity="M", status="wont-fix", first_seen_review_id=r1)
    elif view == "v_relation_problems":
        with without_trigger(L, "tr_relation_noreverse_dup"):
            for _ in range(count):
                a, b = L.issue(first_seen_review_id=r1), L.issue(first_seen_review_id=r1)
                L.relation(a, b)
                L.relation(b, a)
    elif view == "v_check_finding_owner":
        b = L.issue(first_seen_review_id=r1)
        others = [L.issue(first_seen_review_id=r1) for _ in range(count)]
        r2 = L.review()
        fb = traced(L, L.finding(r2, issue_id=b, match_type="still-present"))
        for a in others:
            L.check_row(r2, a, "still-present", finding_id=fb)   # cites ISS-b's finding as evidence for a
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
        cls.checks = seeded_checks()
        cls.must_be_empty = must_be_empty()
        cls.views = [v for v, _ in cls.must_be_empty]

    def run_check(self, L, quiet=False, full=False):
        L.commit()
        con = self.mod.connect(L.db)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                n = self.mod.check(con, quiet=quiet, full=full)
            return n, out.getvalue()
        finally:
            con.close()

    def test_check_clean_ledger(self):
        with new_ledger() as L:
            n, out = self.run_check(L)
        self.assertEqual(n, 0)
        # step 92: the first line names the integrity pass that ran (quick_check unless check --full)
        self.assertEqual(out.splitlines(), ["quick_check: ok (check --full runs the full integrity_check)"]
                         + [f"    0  {label}" for _, label, _ in self.checks])

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
                n, out = self.run_check(L, full=True)   # step 92: index contents are checked by --full only
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
                n, out = self.run_check(L, full=True)
            self.assertEqual(n, 4)  # integrity, foreign key, the view, and the scratch objects (schema drift)

    def test_check_each_must_be_empty_view(self):
        for view, label in self.must_be_empty:
            with self.subTest(view=view):
                with new_ledger() as L:
                    offend(L, view)
                    n, out = self.run_check(L)
                self.assertEqual(n, 1, out)
                lines = out.splitlines()
                self.assertIn(f"    1  {label}  ({view})", lines)
                for other, other_label, _ in self.checks:
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
        self.assertLessEqual({"v_unmatched_findings", "v_unchecked_known_issues", "v_untraceable_refs",
                              "v_uncovered_findings", "v_coverage_problems"}, set(self.views))
        self.assertEqual([(v, t) for v, _, t in self.checks if t == "warning"],
                         [("v_unguarded_fixes", "warning"), ("v_artifacts_outside_ledger", "warning")])
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
        # Was a characterization of ISS-0178 (v_coverage_problems' comment lacked "must be empty"); step 65
        # (schema 7) rewrote that comment with the words.
        self.assertEqual(commented, set(self.views))
        skill = SKILL_MD.read_text(encoding="utf-8")
        for v in self.views:
            self.assertIn(f"`{v}`", skill)


class CheckCommand(unittest.TestCase):
    """`ledger.py check` as a subprocess on the fresh build."""

    @classmethod
    def setUpClass(cls):
        cls.mod = fixtures.load_ledger_module()
        cls.checks = seeded_checks()
        cls.must_be_empty = must_be_empty()

    def test_cli_check_clean_exit0(self):
        with new_ledger() as L:
            p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        for _, label, _ in self.checks:
            self.assertIn(f"    0  {label}", lines)
        self.assertEqual(lines[0], "quick_check: ok (check --full runs the full integrity_check)")
        self.assertTrue(all(l.startswith("    0  ") for l in lines[1:]), lines)

    def test_cli_check_each_view_exit1(self):
        for view, label in self.must_be_empty:
            with self.subTest(view=view):
                with new_ledger() as L:
                    offend(L, view)
                    p = L.run("check")
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn(f"({view})", p.stdout)
                self.assertIn(f"    1  {label}  ({view})", p.stdout.splitlines())

    def test_check_fails_on_each_view(self):
        # DB-T3: the exit code and the view name, with several offending rows at once.
        for view, _ in self.must_be_empty:
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
            L.x("UPDATE meta SET value='99' WHERE key='schema_version'")
            p = L.run("check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"is schema 99; this ledger.py only knows {L.module.SCHEMA_VERSION}. Use a newer ledger.py.",
                      p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
