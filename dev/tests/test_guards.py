"""Guard tests are measured, not typed (plan P-0002 steps 16-19: OWN-R3-7, TQ-H1, DEC-L4, HO-other-4, TPG-M7, TST-M2,
TQ-L1, FIX-12). step-status done --guard and add-guard run each guard with the step's fix undone and in place and
record it only when it fails without the fix and passes with it; v_unproven_steps lists done steps with no proof that
their test failed first; measure-guards and drop-guard re-measure and prune recorded guards; check lists guard tests
that are not in the tree."""
import json
import sqlite3
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._p5_writes2_helpers import ok, project, refused, run

APP_OLD = "def handler(request):\n    return request[:1]\n"
APP_FIX = "def handler(request):\n    if not request:\n        return None\n    return request[:1]\n"
TESTS = ("import unittest\nfrom src.app import handler\n\n\nclass Handler(unittest.TestCase):\n"
         "    def test_empty_request(self):\n        self.assertIsNone(handler(''))\n\n"
         "    def test_first_letter(self):\n        self.assertEqual(handler('ab'), 'a')\n")
GUARD = "tests/test_app.py::Handler::test_empty_request"
LOOSE = "tests/test_app.py::Handler::test_first_letter"
ACT = ("--actor", "A-0002")
REPO_ROOT = Path(__file__).resolve().parents[2]


def plan(test, issues=1):
    """A project whose step 1 (P-0001) closes `issues` issues; src/app.py is APP_OLD at the reviewed commit, and the
    fix commit adds APP_FIX with tests/test_app.py. Returns (repo, L, fix commit)."""
    repo, L, sha = project(test)
    repo.write("src/app.py", APP_OLD)
    repo.commit("handler")
    pid = L.plan("P-0001", reviews=(1,))
    sid = L.step(pid, L.phase(pid, 1), 1)
    for n in range(1, issues + 1):
        fid = L.finding(1, f"SPEC-M{n}", reviewer_run_id=1)
        L.match(fid, L.issue(f"ISS-000{n}", first_seen_review_id=1))
        L.step_finding(sid, fid)
    L.commit()
    repo.write("src/app.py", APP_FIX)
    repo.write("tests/test_app.py", TESTS)
    return repo, L, repo.commit("Refuse empty requests")


def guards(L):
    return L.all("""SELECT i.code, g.test_path || '::' || g.test_name AS test, g.failed_before_fix, g.passes_after_fix,
                    g.measured_commit_sha, g.measured_at IS NOT NULL AS measured
                    FROM guard_tests g JOIN issues i ON i.id = g.issue_id ORDER BY g.id""")


class Measure(unittest.TestCase):
    def test_step_done_requires_measured_failing_guard(self):
        repo, L, fix = plan(self)
        line = refused(self, L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", LOOSE, *ACT)
        self.assertIn("passes with the fix undone", line)
        self.assertIn("test_first_letter", line)
        out = ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD, *ACT)
        self.assertIn(f"ISS-0001: guard test {GUARD} (fails without the fix, passes with it)", out)
        self.assertEqual(guards(L), [{"code": "ISS-0001", "test": GUARD, "failed_before_fix": 1,
                                      "passes_after_fix": 1, "measured_commit_sha": fix, "measured": 1}])

    def test_step_done_guard_needs_a_commit_and_a_passing_test(self):
        repo, L, fix = plan(self)
        self.assertIn("no commit", refused(self, L, "step-status", "1", "done", "--note", "done", "--guard", GUARD,
                                           *ACT))
        repo.write("tests/test_app.py", TESTS.replace("assertIsNone(handler(''))", "assertEqual(1, 2)"))
        broken = repo.commit("break the test")
        self.assertIn("fails with the fix in place", refused(self, L, "step-status", "1", "done", "--note", "done",
                                                              "--commit", broken, "--guard", GUARD, *ACT))

    def test_failed_before_flag_is_gone(self):
        repo, L, fix = plan(self)
        p = run(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD,
                "--failed-before", *ACT)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--failed-before", p.stderr)
        self.assertEqual(guards(L), [])

    def test_step_status_guard_needs_issue_when_step_closes_several(self):
        repo, L, fix = plan(self, issues=2)
        line = refused(self, L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD, *ACT)
        self.assertIn("ISS-0001=", line)
        ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", f"ISS-0002={GUARD}", *ACT)
        self.assertEqual([g["code"] for g in guards(L)], ["ISS-0002"])

    def test_add_guard_refuses_missing_test(self):
        repo, L, fix = plan(self)
        ok(L, "add-commit", "1", fix)
        for path, name, says in (("tests/test_app.py", "Nope.test_x", "Nope.test_x is not in tests/test_app.py"),
                                 ("tests/test_app.py", "Handler::test_gone", "Handler::test_gone is not in"),
                                 ("tests/test_none.py", "Handler::test_empty_request", "tests/test_none.py is not")):
            with self.subTest(name=name, path=path):
                self.assertIn(says, refused(self, L, "add-guard", "ISS-0001", path, name, "--step", "1", *ACT))
        self.assertIn("passes with the fix undone", refused(self, L, "add-guard", "ISS-0001", "tests/test_app.py",
                                                            "Handler::test_first_letter", "--step", "1", *ACT))
        self.assertIn("give --step (a step with a recorded commit) or --commit", refused(self, L, "add-guard", "ISS-0001", "tests/test_app.py",
                                                    "Handler::test_empty_request", *ACT))
        ok(L, "add-guard", "ISS-0001", "tests/test_app.py", "Handler.test_empty_request", "--step", "1", *ACT)
        self.assertEqual(guards(L)[0]["failed_before_fix"], 1)
        self.assertEqual(guards(L)[0]["measured_commit_sha"], fix)

    def test_add_guard_with_a_test_command(self):
        """A project's own runner: {id} is the unittest name; the exit code says pass or fail."""
        repo, L, fix = plan(self)
        out = ok(L, "add-guard", "ISS-0001", "tests/test_app.py", "Handler::test_empty_request", "--commit", fix,
                 "--test-command", "python3 -m unittest -q {id}", *ACT)
        self.assertEqual(out, [f"ISS-0001: guard test {GUARD} (fails without the fix, passes with it)"])
        self.assertIn("passes with the fix undone", refused(
            self, L, "add-guard", "ISS-0001", "tests/test_app.py", "Handler::test_first_letter", "--commit", fix,
            "--test-command", "python3 -m unittest -q {id}", *ACT))

    def test_confirm_guard_refuses_a_test_that_is_not_there(self):
        repo, L, fix = plan(self)
        ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD, *ACT)
        L.x("UPDATE review_repositories SET commit_sha=?", (fix,))
        L.commit()
        self.assertEqual(len(ok(L, "confirm-guard", "R-0001", "ISS-0001")), 1)
        L.x("UPDATE review_repositories SET commit_sha=?", (repo.commits[0],))
        L.commit()
        self.assertIn("--gone", refused(self, L, "confirm-guard", "R-0001", "ISS-0001"))
        ok(L, "confirm-guard", "R-0001", "ISS-0001", "--gone")

    def test_hanging_guard_counts_as_failing(self):
        """A test that hangs (the fix undone often deadlocks) fails after GUARD_SECONDS; the tests after it still run."""
        mod = fixtures.load_ledger_module(fresh=True)
        mod.GUARD_SECONDS = 3
        work = fixtures.temp_dir("guard-hang-")
        (work / "tests").mkdir()
        (work / "tests" / "test_h.py").write_text(
            "import time, unittest\n\n\nclass H(unittest.TestCase):\n    def test_hangs(self):\n"
            "        time.sleep(60)\n\n    def test_ok(self):\n        pass\n", encoding="utf-8")
        got = mod.run_guards(work, [("tests/test_h.py", "H::test_hangs"), ("tests/test_h.py", "H::test_ok")])
        self.assertEqual(got, {("tests/test_h.py", "H::test_hangs"): ("failed", "no result within 3 s"),
                               ("tests/test_h.py", "H::test_ok"): ("passed", "")})

    def test_unmeasured_outcomes_refused_by_sql(self):
        repo, L, fix = plan(self)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "measured"):
            L.x("INSERT INTO guard_tests(issue_id, test_path, test_name, failed_before_fix) VALUES (1,'t.py','t',1)")
        L.con.rollback()

    def test_schema_upgrade_to_23(self):
        """The last released ledger (schema 21) migrates through 0023: guard_tests gains measured_at and
        measured_commit_sha, and its rows stay unmeasured."""
        from dev.tests import _migrate_helpers as M
        project_dir, db = M.restore(21)
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0023-measured-guards", p.stdout)
        con = sqlite3.connect(str(db))
        self.addCleanup(con.close)
        cols = [r[1] for r in con.execute("PRAGMA table_info(guard_tests)")]
        self.assertIn("measured_at", cols)
        self.assertIn("measured_commit_sha", cols)
        self.assertEqual(con.execute("SELECT count(*) FROM guard_tests WHERE measured_at IS NULL").fetchone()[0],
                         con.execute("SELECT count(*) FROM guard_tests").fetchone()[0])
        self.assertEqual(con.execute("SELECT label FROM enum_labels WHERE enum='event' AND value='guard-dropped'")
                         .fetchone()[0], "A guard test was dropped")


class Unproven(unittest.TestCase):
    def test_v_unproven_steps(self):
        repo, L, fix = plan(self)
        L.x("UPDATE steps SET status='done', progress_note='done', change_note='done' WHERE number=1")
        L.commit()
        self.assertEqual([(r["plan"], r["step"]) for r in L.all("SELECT * FROM v_unproven_steps")], [("P-0001", 1)])
        self.assertEqual(L.scalar("SELECT tier FROM ledger_checks WHERE view='v_unproven_steps'"), "warning")
        with self.subTest("an unmeasured guard is not proof"):
            L.x("INSERT INTO guard_tests(issue_id, test_path, test_name, step_id) VALUES (1, 't.py', 'T.test', 1)")
            self.assertEqual(L.count("v_unproven_steps"), 1)
        with self.subTest("a before-fix test run is proof"):
            L.x("INSERT INTO test_runs(step_id, command, purpose, failed) VALUES (1, 'python3 -m unittest', "
                "'before-fix', 1)")
            self.assertEqual(L.count("v_unproven_steps"), 0)
            L.x("DELETE FROM test_runs")
        with self.subTest("a measured guard is proof"):
            self.assertEqual(L.count("v_unproven_steps"), 1)
            L.x("DELETE FROM guard_tests")
            L.commit()
            L.x("UPDATE steps SET status='doing', change_note='again' WHERE number=1")
            L.commit()
            ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD, *ACT)
            self.assertEqual(L.count("v_unproven_steps"), 0)


class Prune(unittest.TestCase):
    def setUp(self):
        self.repo, self.L, self.fix = plan(self)
        ok(self.L, "add-commit", "1", self.fix)
        # two rows as an older tool typed them: recorded, never measured
        for name in ("Handler::test_empty_request", "Handler::test_first_letter"):
            self.L.x("INSERT INTO guard_tests(issue_id, repository_id, test_path, test_name, step_id, "
                     "added_commit_sha) VALUES (1, 1, 'tests/test_app.py', ?, 1, ?)", (name, self.fix))
        self.L.commit()

    def test_drop_guard_records_event(self):
        L = self.L
        self.assertIn("--note", refused(self, L, "drop-guard", "ISS-0001", LOOSE, *ACT))
        self.assertIn("--actor", refused(self, L, "drop-guard", "ISS-0001", LOOSE, "--note", "why"))
        self.assertIn("has no guard test", refused(self, L, "drop-guard", "ISS-0001", "tests/x.py::T::t", "--note",
                                                   "why", *ACT))
        self.assertEqual(ok(L, "drop-guard", "ISS-0001", LOOSE, "--note", "passes without the fix", *ACT),
                         [f"ISS-0001: guard test {LOOSE} dropped"])
        self.assertEqual([g["test"] for g in guards(L)], [GUARD])
        e = L.one("""SELECT e.entity_type, e.entity_id, e.from_value, e.note, a.name AS actor FROM events e
                     JOIN actors a ON a.id = e.actor_id WHERE e.event='guard-dropped'""")
        self.assertEqual(e, {"entity_type": "issue", "entity_id": 1, "from_value": LOOSE,
                             "note": "passes without the fix", "actor": "review manager"})

    def test_measure_guards_records_and_drops(self):
        L = self.L
        out = ok(L, "measure-guards", "--issue", "ISS-0001", *ACT)
        self.assertIn(f"ISS-0001: {GUARD}: fails without the fix, passes with it", out)
        self.assertIn(f"ISS-0001: {LOOSE}: passes with the fix undone (kept; --drop drops it)", out)
        self.assertEqual([(g["test"], g["failed_before_fix"], g["passes_after_fix"], g["measured"])
                          for g in guards(L)], [(GUARD, 1, 1, 1), (LOOSE, 0, 1, 1)])
        out = ok(L, "measure-guards", "--issue", "ISS-0001", "--drop", "--note", "R-0003 TQ-H1", *ACT)
        self.assertIn(f"ISS-0001: {LOOSE}: passes with the fix undone (dropped)", out)
        self.assertEqual([g["test"] for g in guards(L)], [GUARD])
        self.assertEqual(L.scalar("SELECT note FROM events WHERE event='guard-dropped'"),
                         "passes with the fix undone at " + self.fix[:10] + ": R-0003 TQ-H1")

    def test_measure_guards_keeps_what_it_cannot_measure(self):
        L = self.L
        self.repo.write("tests/test_skip.py", "import unittest\n\n\nclass S(unittest.TestCase):\n"
                                              "    @unittest.skip('environment: no browser')\n"
                                              "    def test_s(self):\n        pass\n")
        skip = self.repo.commit("a skipped test")
        ok(L, "add-commit", "1", skip)
        L.x("INSERT INTO guard_tests(issue_id, repository_id, test_path, test_name, step_id) "
            "VALUES (1, 1, 'tests/test_skip.py', 'S::test_s', 1)")
        L.x("UPDATE guard_tests SET test_name='Handler::test_renamed' WHERE test_name='Handler::test_first_letter'")
        L.commit()
        out = ok(L, "measure-guards", "--issue", "ISS-0001", "--drop", "--note", "prune", *ACT)
        self.assertIn(f"ISS-0001: tests/test_app.py::Handler::test_renamed: not measured: Handler::test_renamed is "
                      f"not in tests/test_app.py at {skip[:10]}", out)
        self.assertIn("ISS-0001: tests/test_skip.py::S::test_s: not measured: skipped with the fix in place "
                      "(environment: no browser)", out)
        self.assertEqual(len(guards(L)), 3)
        self.assertEqual(L.scalar("SELECT measured_at FROM guard_tests WHERE test_name='S::test_s'"), None)


class InTheTree(unittest.TestCase):
    def check_lines(self, L):
        p = run(L, "check")
        return p.returncode, [x for x in p.stdout.splitlines() if x.startswith("guard:")]

    def test_check_lists_guard_tests_missing_from_the_tree(self):
        repo, L, fix = plan(self)
        ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD, *ACT)
        code, lines = self.check_lines(L)
        self.assertEqual(lines, [])
        with self.subTest("a name that is not in the file"):
            L.x("INSERT INTO guard_tests(issue_id, repository_id, test_path, test_name) "
                "VALUES (1, 1, 'tests/test_app.py', 'Nope.test_x')")
            L.commit()
            bad, lines = self.check_lines(L)
            self.assertEqual(bad, 1)
            self.assertEqual(lines, [f"guard: ISS-0001 tests/test_app.py::Nope.test_x is not in the tree at "
                                     f"{fix[:10]} (confirm-guard --gone, or add-guard under its new name)"])
            L.x("DELETE FROM guard_tests WHERE test_name='Nope.test_x'")
            L.commit()
        with self.subTest("a guarded test renamed"):
            repo.write("tests/test_app.py", TESTS.replace("test_empty_request", "test_empty_is_none"))
            head = repo.commit("rename")
            bad, lines = self.check_lines(L)
            self.assertEqual(bad, 1)
            self.assertEqual(lines, [f"guard: ISS-0001 {GUARD} is not in the tree at {head[:10]} (confirm-guard "
                                     f"--gone, or add-guard under its new name)"])
            L.x("UPDATE review_repositories SET commit_sha=?", (head,))
            L.commit()
            ok(L, "confirm-guard", "R-0001", "ISS-0001", "--gone")
            self.assertEqual(self.check_lines(L)[1], [])

    def test_rename_guard_records_event(self):
        repo, L, fix = plan(self)
        ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix, "--guard", GUARD, *ACT)
        renamed = "tests/test_app.py::Handler::test_empty_is_none"
        self.assertIn("is not in tests/test_app.py", refused(self, L, "rename-guard", "ISS-0001", GUARD, renamed,
                                                             "--reason", "renamed", *ACT))
        repo.write("tests/test_app.py", TESTS.replace("test_empty_request", "test_empty_is_none"))
        repo.commit("rename")
        self.assertIn("--reason", refused(self, L, "rename-guard", "ISS-0001", GUARD, renamed, *ACT))
        self.assertEqual(ok(L, "rename-guard", "ISS-0001", GUARD, renamed, "--reason", "renamed in a3", *ACT),
                         [f"ISS-0001: guard test {GUARD} -> {renamed}"])
        self.assertEqual([(g["test"], g["failed_before_fix"], g["measured"]) for g in guards(L)], [(renamed, 1, 1)])
        e = L.one("SELECT from_value, to_value, note FROM events WHERE event='guard-renamed'")
        self.assertEqual(e, {"from_value": GUARD, "to_value": renamed, "note": "renamed in a3"})
        self.assertEqual(self.check_lines(L)[1], [])

    def test_repo_guard_records_resolve(self):
        """Every live guard of this project's own ledger names a test in the tree at HEAD."""
        db = REPO_ROOT / ".code-review" / "ledger.sqlite"
        if not db.is_file():
            self.skipTest("this checkout has no .code-review ledger (it stays local, D-0019)")
        mod = fixtures.load_ledger_module()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        missing = mod.guard_rows_not_in_tree(REPO_ROOT, con)
        self.assertEqual([json.dumps(m) for m in missing], [])


if __name__ == "__main__":
    unittest.main()


class PlainDefinitions(unittest.TestCase):
    """check finds a guard test by its plain definition without parsing the file, and the answer matches the parse."""

    SRC = '''import unittest
from dev.tests.base import Base


def top_level():
    pass


class Direct(unittest.TestCase):
    """A docstring."""

    def test_here(self):
        def test_nested():
            pass

    class Inner:
        def test_inner(self):
            pass


class Inherits(Base):
    pass


DOC = """
def test_in_a_string():
    pass
"""
'''
    BASE = "class Base:\n    def test_from_base(self):\n        pass\n"

    def setUp(self):
        self.mod = fixtures.load_ledger_module()

    def read(self, p):
        return self.BASE if p == "dev/tests/base.py" else None

    def test_plain_answers_agree_with_the_parse(self):
        m = self.mod
        cases = {  # name: (plain shortcut, the whole answer)
            "top_level": (True, True),
            "Direct.test_here": (True, True),
            "Direct::test_here": (True, True),
            "Direct.test_nested": (False, False),
            "Direct.test_inner": (False, False),
            "Inherits.test_from_base": (False, True),   # through the base class: only the parse finds it
            "Direct.test_missing": (False, False),
            "Missing.test_here": (False, False),
        }
        for name, (plain, whole) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(m._py_defined_plainly(self.SRC, m._guard_parts(name)), plain)
                self.assertEqual(m.guard_in_source("dev/tests/t.py", name, self.SRC, self.read), whole)
