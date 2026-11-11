"""Plan P-0001 phase 4, implementer p4-views2: the must-be-empty checks and reporting views of steps 58, 65, 67, 68,
70, 76, 77, 78, 83 and 93 (migration 0007-views), and check's quick_check (step 92).

Each test seeds the smallest ledger that shows one rule; the check-level tests run the built module's check() and
assert it counts the failure and names the view.
"""
import contextlib
import io
import re
import sqlite3
import time
import unittest

from dev.tests import fixtures
from dev.tests._schema_helpers import SchemaCase

SCHEMA_SQL = fixtures.REPO_ROOT / "references" / "schema.sql"
SHA_B = "b" * 40


class Base(SchemaCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = fixtures.load_ledger_module()

    def setUp(self):
        super().setUp()
        self.r1 = self.review("R-0001")

    def view(self, name, where="", order=""):
        return self.L.all(f"SELECT * FROM {name} {where} {order}")

    def run_check(self, **kw):
        con = self.mod.connect(self.L.db)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                n = self.mod.check(con, quiet=True, **kw)
            return n, out.getvalue()
        finally:
            con.close()

    def assert_check_names(self, view):
        n, out = self.run_check()
        self.assertGreaterEqual(n, 1, out)
        self.assertIn(f"({view})", out)

    def traced(self, fid, path="src/app.py", line=1, **kw):
        """A fully traceable location: the review's commit, tagged, one line with its excerpt."""
        lid = self.L.location(fid, path, line, line, excerpt=kw.pop("excerpt", "x = 1"), **kw)
        self.x("UPDATE review_repositories SET tag_name = 'review/' || (SELECT code FROM reviews WHERE id = "
               "review_repositories.review_id) WHERE tag_name IS NULL")
        return lid

    def abandon_review(self, rid):
        self.x("UPDATE reviews SET status='abandoned', change_note='stopped' WHERE id=?", (rid,))


# ---------------------------------------------------------------- step 58: false findings and v_real_findings
class FalseFindings(Base):
    def test_v_real_findings(self):
        self.L.finding(self.r1, "SPEC-M1")
        self.L.finding(self.r1, "SPEC-M2", verification="false")
        self.L.finding(self.r1, "SPEC-M3", verification="partly")
        self.assertEqual([r["point_id"] for r in self.view("v_real_findings", order="ORDER BY point_id")],
                         ["SPEC-M1", "SPEC-M3"])

    def test_v_unmatched_findings(self):
        iid = self.issue(self.r1, code="ISS-0001")
        self.L.finding(self.r1, "SPEC-M1")
        self.L.finding(self.r1, "SPEC-M2", issue_id=iid)
        self.L.finding(self.r1, "SPEC-M3", verification="false")
        self.assertEqual([r["point_id"] for r in self.view("v_unmatched_findings", order="ORDER BY point_id")],
                         ["SPEC-M1", "SPEC-M3"])

    def test_unmatched_false_finding_fails_check(self):
        self.L.finding(self.r1, "SPEC-M1", verification="false")
        self.assert_check_names("v_unmatched_findings")

    def test_false_finding_not_in_open_issues(self):
        r2 = self.review("R-0002")
        iid = self.issue(self.r1, code="ISS-0001", status="obsolete", resolution="false finding: misread the code")
        self.L.finding(self.r1, "SPEC-M1", verification="false", issue_id=iid)
        self.L.review_domain(r2, "spec")
        self.assertEqual(self.view("v_open_issues"), [])
        self.assertEqual(self.view("v_unchecked_known_issues"), [])

    def test_false_finding_must_be_covered(self):
        """Coverage includes false findings: the plan's 'record false findings' step closes them."""
        plan = self.L.plan("P-0001", reviews=[self.r1])
        self.L.finding(self.r1, "SPEC-M1", verification="false")
        self.assertEqual([r["point_id"] for r in self.view("v_uncovered_findings")], ["SPEC-M1"])

    def test_false_findings_not_recurring(self):
        r2 = self.review("R-0002")
        iid = self.issue(self.r1, code="ISS-0001")
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(r2, issue_id=iid, verification="false", match_type="still-present")
        self.assertEqual(self.view("v_recurring_issues"), [])

    def test_review_summary_counts_real_findings(self):
        iid = self.issue(self.r1)
        self.L.finding(self.r1, "SPEC-H1", severity="H", issue_id=iid)
        self.L.finding(self.r1, "SPEC-H2", severity="H", verification="false", issue_id=iid)
        r = self.L.one("SELECT points, high, false_findings, new_issues FROM v_review_summary")
        self.assertEqual(r, {"points": 1, "high": 1, "false_findings": 1, "new_issues": 1})

    def test_came_back_counted_from_events(self):
        """came_back counts the issues this review's findings sent back (regressed, or reopened by a recurrence)."""
        r2 = self.review("R-0002")
        fixed = self.issue(self.r1, status="fixed")
        obsolete = self.issue(self.r1, status="obsolete")
        still_open = self.issue(self.r1)
        self.L.finding(r2, "A-1", issue_id=fixed, match_type="regression")
        self.L.finding(r2, "A-2", issue_id=obsolete, match_type="recurrence")
        self.L.finding(r2, "A-3", issue_id=still_open, match_type="still-present")
        self.assertEqual(self.scalar("SELECT came_back FROM v_review_summary WHERE review='R-0002'"), 2)


# ---------------------------------------------------------------- step 65: abandoned, superseded, dropped
class Forgiveness(Base):
    def test_check_passes_after_abandon(self):
        iid = self.issue(self.r1, code="ISS-0001")
        r2 = self.review("R-0002")
        self.L.review_domain(r2, "spec")
        self.L.finding(r2, "SPEC-M1")                 # unmatched
        plan = self.L.plan("P-0001", reviews=[r2])
        ph = self.L.phase(plan, 1)
        self.L.step(plan, ph, 1)                      # closes nothing
        views = ("v_unmatched_findings", "v_unchecked_known_issues", "v_uncovered_findings", "v_coverage_problems")
        self.assertTrue(all(self.view(v) for v in views), [self.view(v) for v in views])
        self.abandon_review(r2)
        self.x("UPDATE plans SET status='abandoned', change_note='review abandoned' WHERE id=?", (plan,))
        for v in views:
            with self.subTest(v):
                self.assertEqual(self.view(v), [])
        self.assertTrue(iid)

    def test_superseded_plan_is_not_checked(self):
        f = self.L.finding(self.r1, "SPEC-M1")
        old = self.L.plan("P-0001", reviews=[self.r1])
        self.L.step(old, self.L.phase(old, 1), 1)
        new = self.L.plan("P-0002", reviews=[self.r1])
        self.L.step(new, self.L.phase(new, 1), 1, findings=(f,))
        self.x("UPDATE plans SET status='superseded', superseded_by=? WHERE id=?", (new, old))
        self.assertEqual(self.view("v_uncovered_findings"), [])
        self.assertEqual(self.view("v_coverage_problems"), [])

    def test_dropped_step_does_not_cover(self):
        plan = self.L.plan("P-0001", reviews=[self.r1])
        ph = self.L.phase(plan, 1)
        f = self.L.finding(self.r1, "SPEC-M1")
        s = self.L.step(plan, ph, 1, findings=(f,))
        self.assertEqual(self.view("v_uncovered_findings"), [])
        self.L.update("steps", s, status="dropped")
        self.assertEqual([r["point_id"] for r in self.view("v_uncovered_findings")], ["SPEC-M1"])
        with self.subTest("a decided decision on the dropped step makes it cover"):
            self.L.decision("D-0001", "Drop it?", step_id=s, status="decided", answer="yes, not needed")
            self.assertEqual(self.view("v_uncovered_findings"), [])

    def test_v_coverage_problems(self):
        plan = self.L.plan("P-0001", reviews=[self.r1])
        ph = self.L.phase(plan, 1)
        f1 = self.L.finding(self.r1, "SPEC-M1")
        f2 = self.L.finding(self.r1, "SPEC-M2")
        self.L.step(plan, ph, 1)                                       # closes nothing -> row
        self.L.step(plan, ph, 2, status="dropped")                     # dropped, closes nothing -> none
        self.L.step(plan, ph, 3, no_findings_reason="the follow-up review that verifies the plan")  # by design
        a = self.L.step(plan, ph, 4, findings=(f1, f2))
        b = self.L.step(plan, ph, 5)
        self.L.step_finding(b, f1)                                     # f1 twice, no reason -> row
        c = self.L.step(plan, ph, 6)
        self.L.step_finding(c, f2, multi_reason="tests first")         # f2: one link has no reason -> row
        got = sorted((r["problem"], r["item"]) for r in self.view("v_coverage_problems"))
        self.assertEqual(got, [("finding closed by several steps without a reason", "SPEC-M1"),
                               ("finding closed by several steps without a reason", "SPEC-M2"),
                               ("step closes nothing", "1")])
        with self.subTest("twice with reasons on every link -> none"):
            self.x("UPDATE step_findings SET multi_reason='both parts' WHERE step_id IN (?, ?, ?)", (a, b, c))
            self.assertEqual([r["item"] for r in self.view("v_coverage_problems")], ["1"])
        self.assertTrue(a)


# ---------------------------------------------------------------- step 67: code references
class CodeRefs(Base):
    def test_location_takes_review_commit(self):
        fid = self.L.finding(self.r1, "SPEC-M1")
        self.L.review_repository(self.r1, SHA_B)
        lid = self.L.location(fid)
        self.assertEqual(self.scalar("SELECT commit_sha FROM finding_locations WHERE id=?", (lid,)), SHA_B)
        with self.subTest("an explicit commit is kept"):
            l2 = self.L.location(fid, "src/b.py", commit_sha="c" * 40)
            self.assertEqual(self.scalar("SELECT commit_sha FROM finding_locations WHERE id=?", (l2,)), "c" * 40)
        with self.subTest("no review_repositories row: stays NULL and is untraceable"):
            # the location's repository must be one the review looked at (tr_location_repo_in_review), so the probe
            # lifts that guard inside a savepoint
            def probe():
                f2 = self.L.finding(self.r1, "SPEC-M2")
                other = self.L.repository("engine", "engine")
                self.x("DROP TRIGGER tr_location_repo_in_review")
                l3 = self.insert("finding_locations", finding_id=f2, repository_id=other, path="x.py",
                                 line_start=1, line_end=1, excerpt="x")
                self.assertIsNone(self.scalar("SELECT commit_sha FROM finding_locations WHERE id=?", (l3,)))
                return [r["problem"] for r in self.view("v_untraceable_refs", "WHERE point_id='SPEC-M2'")]
            self.assertEqual(self.in_savepoint(probe), ["no commit"])

    def test_v_code_refs_git_command(self):
        fid = self.L.finding(self.r1, "SPEC-M1", issue_id=self.issue(self.r1, code="ISS-0001"))
        self.L.review_repository(self.r1, SHA_B, tag_name="review/R-0001")
        self.L.location(fid, "src/a.py", 3, 5, excerpt="a\nb\nc")
        self.L.location(fid, "src/b.py", None, None, whole_file=0, symbol="make", role="also")
        rows = self.view("v_code_refs", order="ORDER BY location_id")
        self.assertEqual([(r["git_command"], r["tag_name"], r["has_excerpt"], r["commit_from"]) for r in rows], [
            (f"git -C '.' show '{SHA_B}:src/a.py' | sed -n '3,5p'", "review/R-0001", 1, "location"),
            (f"git -C '.' show '{SHA_B}:src/b.py'", "review/R-0001", 0, "location")])
        with self.subTest("a NULL commit falls back to the review's commit"):
            def probe():
                self.x("DROP TRIGGER tr_location_commit")
                self.x("DROP TRIGGER tr_location_immutable")
                self.x("UPDATE finding_locations SET commit_sha = NULL")
                return [(r["commit_sha"], r["commit_from"], r["git_command"]) for r in
                        self.view("v_code_refs", order="ORDER BY location_id")][:1]
            self.assertEqual(self.in_savepoint(probe), [
                (SHA_B, "review", f"git -C '.' show '{SHA_B}:src/a.py' | sed -n '3,5p'")])

    def test_v_untraceable_refs(self):
        """Each problem gives its row; a fully filled location gives none; an empty excerpt is 'no excerpt'."""
        rid = self.review("R-0002")
        self.L.review_repository(rid, SHA_B)          # not tagged yet
        cases = {
            "no lines or symbol": dict(line_start=None, line_end=None),
            "symbol not resolved to lines": dict(line_start=None, line_end=None, symbol="make"),
            "no excerpt": dict(),
            "excerpt does not cover its lines (past the end of the file?)": dict(line_end=3, excerpt="one"),
            "commit differs from the review commit": dict(commit_sha="c" * 40, excerpt="x"),
            "commit not tagged": dict(excerpt="x"),
        }
        for n, (label, kw) in enumerate(cases.items(), 1):
            fid = self.L.finding(rid, f"SPEC-M{n}")
            kw = dict(kw)
            self.L.location(fid, "src/app.py", kw.pop("line_start", 1), kw.pop("line_end", 1), **kw)
        empty = self.L.finding(rid, "SPEC-M9")
        self.L.location(empty, "src/app.py", 1, 1, excerpt="")
        self.L.finding(rid, "SPEC-M10")               # no location at all
        got = {r["point_id"]: r["problem"] for r in self.view("v_untraceable_refs")}
        want = {f"SPEC-M{n}": label for n, label in enumerate(cases, 1)}
        want.update({"SPEC-M9": "no excerpt", "SPEC-M10": "no location"})
        self.assertEqual(got, want)
        with self.subTest("fully filled -> none"):
            fid = self.L.finding(self.r1, "OK-1")
            self.traced(fid)
            self.assertEqual(self.view("v_untraceable_refs", "WHERE review='R-0001'"), [])

    test_v_untraceable_refs_empty_excerpt = test_v_untraceable_refs

    def test_discovery_may_cite_a_later_commit(self):
        """A defect found during fix work cites the code as it is then, not the review's commit."""
        plan = self.L.plan("P-0001", reviews=[self.r1])
        s = self.L.step(plan, self.L.phase(plan, 1), 1)
        self.L.review_repository(self.r1, SHA_B, tag_name="review/R-0001")
        found = self.L.finding(self.r1, "FIX-1", discovered_in_step_id=s)
        plain = self.L.finding(self.r1, "FIX-2")
        for fid in (found, plain):
            self.L.location(fid, commit_sha="c" * 40, excerpt="x")
        self.assertEqual([(r["point_id"], r["problem"]) for r in self.view("v_untraceable_refs")],
                         [("FIX-2", "commit differs from the review commit")])

    def test_finding_without_location_is_untraceable(self):
        self.L.finding(self.r1, "SPEC-M1")
        self.assertEqual([(r["point_id"], r["path"], r["problem"]) for r in self.view("v_untraceable_refs")],
                         [("SPEC-M1", None, "no location")])
        with self.subTest("an abandoned review's findings are not required to be traced"):
            self.abandon_review(self.r1)
            self.assertEqual(self.view("v_untraceable_refs"), [])

    def test_domainless_issue_must_be_checked(self):
        """issues.domain_code is NOT NULL, so every issue falls under a domain v_unchecked_known_issues covers."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("issues", code="ISS-0001", title="No domain", domain_code=None, kind="defect", severity="M",
                        first_seen_review_id=self.r1)
        iid = self.issue(self.r1, domain_code="security")
        r2 = self.review("R-0002")
        self.L.review_domain(r2, "security")
        self.assertEqual([r["issue"] for r in self.view("v_unchecked_known_issues")], ["ISS-0001"])
        self.assertTrue(iid)


# ---------------------------------------------------------------- step 68: NULLs, grouping, order
class Reporting(Base):
    def test_v_plan_progress(self):
        plan = self.L.plan("P-0001")
        self.L.phase(plan, 1)
        ph2 = self.L.phase(plan, 2)
        for n, st in enumerate(("done", "doing", "waiting-owner", "not-started", "dropped", "done"), 1):
            s = self.L.step(plan, ph2, n)
            if st != "not-started":
                self.L.update("steps", s, status=st, owner_question="Ok?" if st == "waiting-owner" else None)
        got = [(r["phase"], r["steps"], r["done"], r["doing"], r["waiting_owner"], r["not_started"], r["dropped"])
               for r in self.view("v_plan_progress")]
        self.assertEqual(got, [(1, 0, 0, 0, 0, 0, 0), (2, 6, 2, 1, 1, 1, 1)])

    def test_v_unguarded_fixes(self):
        a = self.issue(self.r1, code="ISS-0001", status="fixed")
        b = self.issue(self.r1, code="ISS-0002", status="verified")
        self.L.guard_test(b, still_exists=0)
        c = self.issue(self.r1, code="ISS-0003", status="fixed")
        self.L.guard_test(c)
        self.issue(self.r1, code="ISS-0004", status="fixed", kind="docs")
        self.issue(self.r1, code="ISS-0005", status="open")
        self.assertEqual([(r["code"], r["problem"]) for r in self.view("v_unguarded_fixes", order="ORDER BY code")],
                         [("ISS-0001", "no guard test"), ("ISS-0002", "guard test removed")])
        self.assertTrue(a)
        self.assertIn("COALESCE(i.kind, '') NOT IN", self.scalar(
            "SELECT sql FROM sqlite_schema WHERE name='v_unguarded_fixes'"))

    def test_v_file_hotspots(self):
        demo = self.L.default_repository()
        engine = self.L.repository("engine", "engine")
        r2 = self.review("R-0002")
        i1, i2 = self.issue(self.r1), self.issue(self.r1)
        f1 = self.L.finding(self.r1, "A-1", severity="H", issue_id=i1)
        f2 = self.L.finding(r2, "A-2", issue_id=i2)
        f3 = self.L.finding(self.r1, "A-3", issue_id=i1)
        f4 = self.L.finding(self.r1, "A-4", verification="false", issue_id=i2)
        self.L.location(f1, "src/app.py", repository_id=demo)
        self.L.location(f2, "src/app.py", repository_id=demo)
        self.L.location(f3, "src/app.py", repository_id=engine)
        self.L.location(f4, "src/app.py", repository_id=engine)
        got = [(r["repository"], r["path"], r["issues"], r["reviews"], r["high_points"])
               for r in self.view("v_file_hotspots")]
        self.assertEqual(got, [("demo", "src/app.py", 2, 2, 1), ("engine", "src/app.py", 1, 1, 0)])

    def test_locations_keep_repo(self):
        self.L.default_repository()
        engine = self.L.repository("engine", "engine")
        iid = self.issue(self.r1, code="ISS-0001")
        f1 = self.L.finding(self.r1, "A-1", issue_id=iid)
        f2 = self.L.finding(self.r1, "A-2", issue_id=iid)
        f3 = self.L.finding(self.r1, "A-3", issue_id=iid, verification="false")
        self.L.location(f1, "src/app.py")
        self.L.location(f2, "src/app.py", repository_id=engine)
        self.L.location(f3, "src/false.py")
        locs = self.scalar("SELECT locations FROM v_open_issues WHERE code='ISS-0001'")
        self.assertEqual(sorted(locs.split(",")), ["demo:src/app.py", "engine:src/app.py"])

    def test_issue_order_numeric(self):
        def probe():
            self.x("DROP TRIGGER tr_issue_code")               # codes are sequential; jump to the 5-digit range
            self.issue(self.r1, code="ISS-9999")
            self.issue(self.r1, code="ISS-10000")
            return [r["code"] for r in self.view("v_known_issues_for_brief")]
        self.assertEqual(self.in_savepoint(probe), ["ISS-9999", "ISS-10000"])


# ---------------------------------------------------------------- step 70: history gaps
class HistoryGaps(Base):
    def test_history_gap_fails_check(self):
        self.issue(self.r1, code="ISS-0001")
        self.L.gate("GATE-0001", "other", "Ok", "Ok?")
        self.assertEqual(self.view("v_history_gaps"), [])
        self.x("DROP TRIGGER tr_issue_opened")
        self.issue(self.r1, code="ISS-0002")                      # no opened event
        self.x("DROP TRIGGER tr_gate_status")
        self.x("UPDATE gates SET status='answered', answer_text='yes', answered_by='owner', answered_via='chat'")
        self.assertEqual(sorted((r["kind"], r["ref"], r["problem"]) for r in self.view("v_history_gaps")), [
            ("gate", "GATE-0001", "answered with no event"),
            ("issue", "ISS-0002", "no opened event")])
        # restore the shipped triggers for check's schema comparison, keeping the rows
        for name in ("tr_issue_opened", "tr_gate_status"):
            self.x(re.search(rf"CREATE TRIGGER {name} .*?\nEND;", self.mod.SCHEMA, re.S).group(0))
        self.assert_check_names("v_history_gaps")

    def test_status_rewritten_without_event_is_a_gap(self):
        iid = self.issue(self.r1, code="ISS-0001")
        self.x("DROP TRIGGER tr_issue_status")
        self.L.update("issues", iid, status="planned")
        self.assertEqual([(r["ref"], r["problem"]) for r in self.view("v_history_gaps")],
                         [("ISS-0001", "status planned not in its history (last logged: open)")])


# ---------------------------------------------------------------- steps 77 and 83: consistency checks
class Consistency(Base):
    def setUp(self):
        super().setUp()
        self.r2 = self.review("R-0002")

    def test_v_match_problems(self):
        old = self.issue(self.r1, code="ISS-0001")
        cont = self.issue(self.r2, code="ISS-0002")
        self.L.finding(self.r2, "A-1", issue_id=cont, match_type="continuation")          # no relation
        self.L.finding(self.r2, "A-2", issue_id=old, match_type="new")                    # new on an old issue
        self.L.finding(self.r2, "A-3", issue_id=old, match_type="recurrence")             # never closed
        mine = self.issue(self.r2, code="ISS-0003")
        self.L.finding(self.r2, "A-4", issue_id=mine, match_type="still-present")         # opened by this review
        fixed = self.issue(self.r1, code="ISS-0004", status="fixed", resolved_at="2026-10-01T00:00:00.000Z")
        self.L.finding(self.r2, "A-5", issue_id=fixed, match_type="still-present")        # should be regression
        got = sorted((r["point_id"], r["issue"], r["problem"]) for r in self.view("v_match_problems"))
        self.assertEqual(got, [
            ("A-1", "ISS-0002", "continuation with no continuation-of relation"),
            ("A-2", "ISS-0001", "matched as new to an issue first seen in another review"),
            ("A-3", "ISS-0001", "recurrence of an issue that was never closed"),
            ("A-4", "ISS-0003", "matched as seen before to an issue this review opened"),
            ("A-5", "ISS-0004", "still present on a fixed issue: match it as a regression"),
        ])
        with self.subTest("the relation clears the continuation"):
            self.L.relation(cont, old, "continuation-of", "Picks up where ISS-0001 left off.")
            self.assertNotIn("A-1", [r["point_id"] for r in self.view("v_match_problems")])

    def test_continuation_without_relation_fails_check(self):
        old = self.issue(self.r1, code="ISS-0001")
        cont = self.issue(self.r2, code="ISS-0002")
        self.L.finding(self.r2, "A-1", issue_id=cont, match_type="continuation")
        self.assertTrue(old)
        self.assert_check_names("v_match_problems")

    def test_v_off_plan_coverage(self):
        plan = self.L.plan("P-0001", reviews=[self.r1])
        ph = self.L.phase(plan, 1)
        mine = self.L.finding(self.r1, "A-1")
        other = self.L.finding(self.r2, "B-1")
        s = self.L.step(plan, ph, 1, findings=(mine, other))      # the fixture adds R-0002 to the plan
        self.assertEqual(self.view("v_off_plan_coverage"), [])
        with self.assertRaises(sqlite3.IntegrityError):          # a new off-plan link is refused
            p2 = self.L.plan("P-0002", reviews=[self.r1])
            self.insert("step_findings", step_id=self.L.step(p2, self.L.phase(p2, 1), 1), finding_id=other)
        self.x("DELETE FROM plan_reviews WHERE plan_id=? AND review_id=?", (plan, self.r2))
        self.assertEqual([(r["plan"], r["step"], r["review"], r["point_id"]) for r in self.view("v_off_plan_coverage")],
                         [("P-0001", 1, "R-0002", "B-1")])
        self.assertTrue(s)
        self.assert_check_names("v_off_plan_coverage")

    def test_v_done_steps_open_issues(self):
        plan = self.L.plan("P-0001", reviews=[self.r1])
        ph = self.L.phase(plan, 1)
        i1 = self.issue(self.r1, code="ISS-0001", status="planned")
        i2 = self.issue(self.r1, code="ISS-0002", status="planned")
        i3 = self.issue(self.r1, code="ISS-0003", status="fixed")
        f1 = self.L.finding(self.r1, "A-1", issue_id=i1)
        f2 = self.L.finding(self.r1, "A-2", issue_id=i2)
        f3 = self.L.finding(self.r1, "A-3", issue_id=i3)
        s1 = self.L.step(plan, ph, 1, findings=(f1, f2, f3))
        self.L.step(plan, ph, 2, findings=(f2,), )
        self.x("UPDATE step_findings SET multi_reason='two parts' WHERE finding_id=?", (f2,))
        self.L.update("steps", s1, status="done")
        # ISS-0002 still has an unfinished step; ISS-0003 is fixed
        self.assertEqual([(r["step"], r["issue"], r["issue_status"]) for r in self.view("v_done_steps_open_issues")],
                         [(1, "ISS-0001", "planned")])
        self.assert_check_names("v_done_steps_open_issues")

    def test_v_closed_without_reason(self):
        def probe():
            self.x("DROP TRIGGER tr_issue_closed_needs_reason_ins")
            self.issue(self.r1, code="ISS-0001", status="wont-fix", resolution=None)
            self.issue(self.r1, code="ISS-0002", status="accepted-risk", resolution=" ")
            self.issue(self.r1, code="ISS-0003", status="accepted-risk", resolution="Owner accepts it (D-0001).")
            return [(r["code"], r["status"]) for r in self.view("v_closed_without_reason", order="ORDER BY code")]
        self.assertEqual(self.in_savepoint(probe), [("ISS-0001", "wont-fix"), ("ISS-0002", "accepted-risk")])
        with self.subTest("the trigger refuses closing without a resolution"):
            iid = self.issue(self.r1)
            for status in ("accepted-risk", "wont-fix"):
                with self.assertRaises(sqlite3.IntegrityError) as cm:
                    self.x("UPDATE issues SET status=?, change_note='closing' WHERE id=?", (status, iid))
                self.assertIn("needs a resolution", str(cm.exception))
                self.refused("issues", "needs a resolution", code="ISS-0002", title="t", domain_code="spec",
                             kind="defect", severity="M", first_seen_review_id=self.r1, status=status)
            self.L.update("issues", iid, status="wont-fix", resolution="Not worth it: the page is retired.")

    def test_minor_integrity(self):
        a, b = self.issue(self.r1, code="ISS-0001"), self.issue(self.r1, code="ISS-0002")
        with self.subTest("related-to stored in both directions"):
            def probe():
                self.L.relation(a, b)
                self.x("DROP TRIGGER tr_relation_noreverse_dup")
                self.L.relation(b, a)
                return [(r["from_issue"], r["to_issue"], r["problem"]) for r in self.view("v_relation_problems")]
            self.assertEqual(self.in_savepoint(probe),
                             [("ISS-0002", "ISS-0001", "related-to stored in both directions")])
        with self.subTest("check row citing another issue's finding"):
            fb = self.L.finding(self.r2, "B-1", issue_id=b, match_type="still-present")
            self.L.check_row(self.r2, a, "still-present", finding_id=fb)
            self.assertEqual([(r["review"], r["issue"], r["point_id"], r["finding_issue"])
                              for r in self.view("v_check_finding_owner")], [("R-0002", "ISS-0001", "B-1", "ISS-0002")])
        with self.subTest("severity T on a finding that is not a test"):
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                self.L.finding(self.r1, "C-1", severity="T", kind="defect")
            self.assertIn("severity T", str(cm.exception))
            self.L.finding(self.r1, "C-2", severity="T", kind="test")


# ---------------------------------------------------------------- step 78: verified needs a later fixed check
class Verification(Base):
    def setUp(self):
        super().setUp()
        self.iid = self.issue(self.r1, code="ISS-0001")
        self.L.update("issues", self.iid, status="fixed", after_state="Uses secrets now.")
        self.resolved = self.scalar("SELECT resolved_at FROM issues WHERE id=?", (self.iid,))

    def later_review(self):
        return self.review(started_at=self.scalar("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', ?, '+1 day')",
                                                        (self.resolved,)))

    def test_verified_only_via_later_review_check(self):
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.L.update("issues", self.iid, status="verified")
        self.assertIn("verified only by a later review's fixed check", str(cm.exception))
        with self.subTest("a fixed check of a review that started before the fix does not verify"):
            early = self.review(started_at="2000-01-01T00:00:00.000Z")
            with self.assertRaises(sqlite3.IntegrityError):
                self.L.check_row(early, self.iid, "fixed", evidence="Looked fixed.")
        r3 = self.later_review()
        self.L.check_row(r3, self.iid, "fixed", evidence="The code uses secrets.token_hex now.")
        self.assertEqual(self.scalar("SELECT status FROM issues WHERE id=?", (self.iid,)), "verified")

    def test_fixed_check_requires_evidence(self):
        r3 = self.later_review()
        for outcome in ("fixed", "regressed", "changed"):
            with self.subTest(outcome):
                for ev in (None, "  "):
                    with self.assertRaises(sqlite3.IntegrityError) as cm:
                        self.L.check_row(r3, self.iid, outcome, evidence=ev)
                    self.assertIn("needs evidence", str(cm.exception))
        self.L.check_row(r3, self.iid, "obsolete")       # outcomes that need no evidence


# ---------------------------------------------------------------- step 73: which tables are event-logged
class EventsComment(unittest.TestCase):
    def test_events_comment_lists_logged_tables(self):
        text = SCHEMA_SQL.read_text(encoding="utf-8")
        block = text[:text.index("CREATE TABLE events (")].rsplit("-- ---------- History", 1)[1]
        def listed(label):
            m = re.search(rf"^-- {label}: (.*?)(?:\.\s*$|\n-- [A-Z])", block, re.M | re.S)
            self.assertIsNotNone(m, f"no '{label}:' line above the events table")
            return {w.strip() for w in re.sub(r"\s*\n--\s*", " ", m.group(1)).split(",")}
        logged, not_logged = listed("Logged"), listed("Not logged")
        tables = set(re.findall(r"^CREATE TABLE (\w+)", text, re.M)) - {"events"}
        writers = set()
        for m in re.finditer(r"^CREATE TRIGGER \w+ AFTER (?:INSERT|UPDATE[^\n]*?|DELETE) ON (\w+)(.*?)\nEND;", text,
                             re.M | re.S):
            if "INSERT INTO events" in m.group(2):
                writers.add(m.group(1))
        self.assertEqual(logged, writers)
        self.assertEqual(logged | not_logged, tables, sorted(tables - logged - not_logged))
        self.assertEqual(logged & not_logged, set())


# ---------------------------------------------------------------- step 93: gate kinds for owner needs
class GateKinds(Base):
    def test_gate_kinds_cover_owner_needs(self):
        text = SCHEMA_SQL.read_text(encoding="utf-8")
        mapping = dict(re.findall(r"^--\s+owner_need (\w+) -> gate kind ([\w-]+)", text, re.M))
        needs = re.search(r"owner_need\s+TEXT NOT NULL DEFAULT 'none' CHECK \(owner_need IN\s*\(([^)]*)\)", text)
        if needs:
            needs = set(re.findall(r"'([\w-]+)'", needs.group(1)))
        else:   # the lookup table (schema 6) holds the words
            needs = {r["value"] for r in self.L.all("SELECT * FROM owner_needs")} if self.scalar(
                "SELECT 1 FROM sqlite_schema WHERE name='owner_needs'") else set()
        self.assertTrue(needs)
        self.assertEqual(set(mapping), needs - {"none"})
        for need, kind in mapping.items():
            with self.subTest(need=need, kind=kind):
                self.in_savepoint(lambda: self.L.gate(None, kind, f"For {need}", "Ok?"))


# ---------------------------------------------------------------- step 92: quick_check by default
class QuickCheck(Base):
    def traced_statements(self, **kw):
        con = self.mod.connect(self.L.db)
        seen = []
        con.set_trace_callback(seen.append)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.mod.check(con, **kw)
            return [s for s in seen if "PRAGMA" in s.upper()], out.getvalue()
        finally:
            con.close()

    def test_check_uses_quick_check(self):
        stmts, out = self.traced_statements()
        self.assertIn("PRAGMA quick_check", stmts)
        self.assertNotIn("PRAGMA integrity_check", stmts)
        self.assertIn("quick_check: ok", out)
        stmts, out = self.traced_statements(full=True)
        self.assertIn("PRAGMA integrity_check", stmts)
        self.assertNotIn("PRAGMA quick_check", stmts)
        self.assertIn("integrity_check: ok", out)

    def test_cli_check_full(self):
        self.L.commit()
        p = self.L.run("check", "--full")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("integrity_check: ok", p.stdout)
        p = self.L.run("check")
        self.assertIn("quick_check: ok", p.stdout)

    @fixtures.slow
    def test_check_budget_10x(self):
        """At 10x (a ~0.9 GB ledger) check with quick_check takes well under the time with --full. Measured on the
        reviewer's machine (2026-11-11): quick_check 2.1 s, integrity_check 5.1 s, foreign_key_check 1.1 s, all
        views 3.0 s, so the plan's 2 s budget for the whole check is not met by quick_check alone."""
        from dev.tests import scale
        info = scale.build(fixtures.temp_dir("budget10-") / "proj", scale=10)

        def timed(full):
            con = self.mod.connect(info["db"])
            try:
                t = time.perf_counter()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.mod.check(con, quiet=True, full=full)
                return time.perf_counter() - t
            finally:
                con.close()
        quick, full = timed(False), timed(True)
        self.assertLess(quick, 0.8 * full, (quick, full))

if __name__ == "__main__":
    unittest.main()
