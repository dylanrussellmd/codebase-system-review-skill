"""Continuations count as new issues (step 84, ISS-0228); the review document and the plan narrative live in the
ledger (step 96, ISS-0299); fix-time discoveries are recorded and must be planned (step 97, ISS-0301)."""
import sqlite3
import unittest

from dev.tests.fixtures import Ledger, temp_dir


def route(L, path):
    mod = L.module
    L.commit()
    con = mod.connect(L.db)
    try:
        res = mod.route(L.root, L.db, con, path, {}, None)
    finally:
        con.close()
    return res[0] if isinstance(res, tuple) else res


class Continuation(unittest.TestCase):
    def test_continuation_counts_as_new_issue(self):
        L = Ledger()
        self.addCleanup(L.close)
        r1, r2 = L.review("R-0001"), L.review("R-0002")
        old = L.issue("ISS-0001", first_seen_review_id=r1)
        L.finding(r1, issue_id=old)
        new = L.issue("ISS-0002", first_seen_review_id=r2)
        L.finding(r2, issue_id=new, match_type="continuation")
        L.relation(new, old, "continuation-of", "The fix moved the bug.")
        L.finding(r2, issue_id=old, match_type="still-present")
        row = L.one("SELECT new_issues, carried_over, came_back FROM v_review_summary WHERE review='R-0002'")
        self.assertEqual(row, {"new_issues": 1, "carried_over": 1, "came_back": 0})
        self.assertEqual([r["new_issues"] for r in route(L, "/api/reviews") if r["code"] == "R-0002"], [1])
        # the issue page's timeline labels it (page.html), from this relation and the 'seen' event
        d = route(L, "/api/issue/ISS-0002")
        self.assertIn(("seen", "continuation"), [(e["event"], e["to_value"]) for e in d["timeline"]])
        self.assertEqual([(x["dir"], x["relation"], x["other"]) for x in d["relations"]],
                         [("out", "continuation-of", "ISS-0001")])


class ReportAndNarrative(unittest.TestCase):
    def test_report_and_narrative_round_trip(self):
        L = Ledger()
        self.addCleanup(L.close)
        L.review("R-0001")
        L.plan("P-0001")
        d = temp_dir("md-")
        (d / "report.md").write_text("# R-0001\n\nVerdict: *fine* — ünïcode.\n", encoding="utf-8")
        p = L.run_ok("set-report", "R-0001", str(d / "report.md"))
        self.assertIn("R-0001: stored", p.stdout)
        p = L.run_ok("set-narrative", "P-0001", "-", input="How to read this plan.\n")
        self.assertEqual(route(L, "/api/review/R-0001")["review"]["report_md"],
                         "# R-0001\n\nVerdict: *fine* — ünïcode.\n")
        self.assertEqual(route(L, "/api/plan/P-0001")["plan"]["narrative_md"], "How to read this plan.\n")
        for args, words in ((("set-report", "R-0002", str(d / "report.md")), "no review R-0002"),
                            (("set-narrative", "X-1", "-"), "is not a plan code"),
                            (("set-report", "R-0001", str(d / "missing.md")), "cannot read")):
            p = L.run(*args, input="")
            self.assertNotEqual(p.returncode, 0, args)
            self.assertIn(words, p.stderr)


class Discoveries(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.close)
        L = self.L
        self.r1 = L.review("R-0001")
        self.pl = L.plan("P-0001", reviews=[self.r1])
        ph = L.phase(self.pl, 1)
        self.f0 = L.finding(self.r1)
        self.s1 = L.step(self.pl, ph, 1, findings=[self.f0])
        self.ph = ph

    def test_discovered_in_step_round_trip(self):
        L = self.L
        fid = L.finding(self.r1, point_id="FOUND-1", discovered_in_step_id=self.s1)
        self.assertEqual(L.scalar("SELECT discovered_in_step_id FROM findings WHERE id=?", (fid,)), self.s1)
        with self.assertRaises(sqlite3.IntegrityError):
            L.finding(self.r1, point_id="FOUND-2", discovered_in_step_id=9999)
        L.commit()
        self.assertEqual([f["discovered_in_step_id"] for f in route(L, "/api/review/R-0001")["findings"]
                          if f["point_id"] == "FOUND-1"], [self.s1])

    def test_v_unplanned_discoveries_flags_unplanned_and_clears_when_a_step_closes_it(self):
        L = self.L
        fid = L.finding(self.r1, point_id="FOUND-1", discovered_in_step_id=self.s1, summary="Found while fixing.")
        iid = L.issue("ISS-0001", first_seen_review_id=self.r1)
        L.match(fid, iid)
        rows = L.all("SELECT * FROM v_unplanned_discoveries")
        self.assertEqual([(r["plan"], r["found_in_step"], r["point_id"], r["issue"]) for r in rows],
                         [("P-0001", 1, "FOUND-1", "ISS-0001")])
        p = L.run("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("v_unplanned_discoveries", p.stdout)
        with self.subTest("a dropped step does not close it"):
            s2 = L.step(self.pl, self.ph, 2, findings=[fid], status="dropped")
            self.assertEqual(L.count("v_unplanned_discoveries"), 1)
            L.x("DELETE FROM step_findings WHERE step_id=?", (s2,))
        with self.subTest("a step closing another finding of the same issue closes it"):
            other = L.finding(self.r1, point_id="OTHER-1")
            L.match(other, iid, "still-present")
            s3 = L.step(self.pl, self.ph, 3, findings=[other])
            self.assertEqual(L.count("v_unplanned_discoveries"), 0)
            L.x("DELETE FROM step_findings WHERE step_id=?", (s3,))
        with self.subTest("a step closing it clears it"):
            L.step(self.pl, self.ph, 4, findings=[fid])
            self.assertEqual(L.count("v_unplanned_discoveries"), 0)
        with self.subTest("a closed issue is not flagged"):
            L.x("DELETE FROM step_findings WHERE finding_id=?", (fid,))
            L.x("UPDATE issues SET status='wont-fix', change_note='Not worth fixing.', resolution='Not worth fixing.' WHERE id=?", (iid,))
            self.assertEqual(L.count("v_unplanned_discoveries"), 0)


if __name__ == "__main__":
    unittest.main()
