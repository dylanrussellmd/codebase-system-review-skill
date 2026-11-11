"""report-finding: a defect found during fix work is recorded at a commit, matched and planned (plan P-0001 step
127: OWN-3, OWN-4)."""
import hashlib
import unittest

from dev.tests._p5_writes_helpers import APP, counts, ok, project, refused

TABLES = ("findings", "finding_locations", "issues", "step_findings", "steps", "gates", "events")


class ReportFindingTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.L, self.sha = project(self)
        L = self.L
        fid = L.finding(1, "SPEC-M1", reviewer_run_id=1)
        L.location(fid, "src/app.py", 1, 1)
        L.match(fid, L.issue("ISS-0001", first_seen_review_id=1))
        self.plan = L.plan("P-0001", reviews=(1,))
        self.ph = L.phase(self.plan, 1)
        s1 = L.step(self.plan, self.ph, 1)
        s2 = L.step(self.plan, self.ph, 2)
        L.step_finding(s1, fid, "first half")
        L.step_finding(s2, fid, "second half")
        L.commit()

    def report(self, *extra, where="src/app.py:4-5#handler"):
        return ["report-finding", "--step", "1", "--severity", "M", "--domain", "server", "--where", where,
                "--summary", "The handler accepts an empty request.", "--before", "handler() returns 200 for {}.",
                "--fix", "Refuse an empty request with 400.", "--test", "test_empty_request_refused: 400", *extra]

    def test_report_finding_requires_fields(self):
        L = self.L
        before = counts(L, *TABLES)
        full = self.report("--new-issue")
        for flag in ("--where", "--before", "--fix", "--summary"):
            i = full.index(flag)
            args = full[:i] + full[i + 2:]
            self.assertIn(f"a discovery needs {flag}", refused(self, L, *args))
        i = full.index("--before")
        self.assertIn("a discovery needs --before", refused(self, L, *(full[:i + 1] + ["  "] + full[i + 2:])))
        self.assertEqual(counts(L, *TABLES), before)

    def test_report_finding_amends_plan(self):
        L = self.L
        out = ok(L, *self.report("--into-step", "2", "--actor", "A-0001"))
        self.assertEqual(out[0], f"R-0001 FIX-1: recorded (found in step 1, 1 location at {self.sha[:10]})")
        self.assertEqual(out[1], "FIX-1 -> ISS-0002 new")
        self.assertEqual(out[-1], "FIX-1 planned in step 2 (Step 2)")
        f = L.one("SELECT id, discovered_in_step_id, before_state, recommendation, recommended_test, severity "
                  "FROM findings WHERE point_id='FIX-1'")
        self.assertEqual(f["discovered_in_step_id"], 1)
        self.assertEqual(f["recommended_test"], "test_empty_request_refused: 400")
        self.assertEqual(L.scalar("SELECT step_id FROM step_findings WHERE finding_id=?", (f["id"],)), 2)
        self.assertEqual(L.count("v_unplanned_discoveries"), 0)
        self.assertEqual(L.count("v_untraceable_refs", "point_id='FIX-1' AND problem <> 'commit not tagged'"), 0)
        # a second one into a new step of the reporting step's phase, matched to the issue it names
        out = ok(L, *self.report("--issue", "ISS-0002", "--new-step", "Refuse empty requests everywhere"))
        self.assertEqual(out[1], "FIX-2 -> ISS-0002 new")
        self.assertEqual(out[-1], "FIX-2 planned in new step 3 (phase 1: Refuse empty requests everywhere)")
        s = L.one("SELECT phase_id, change, proof, drafter_key FROM steps WHERE number=3")
        self.assertEqual(s, {"phase_id": self.ph, "change": "Refuse an empty request with 400.",
                             "proof": "test_empty_request_refused: 400", "drafter_key": "R-0001:FIX-2"})
        self.assertEqual(L.count("v_coverage_problems"), 0)
        self.assertEqual(L.count("v_match_problems"), 0)
        # without --issue or --new-issue, a likely known issue stops the report and lists the candidates
        before = counts(L, *TABLES)
        err = refused(self, L, *self.report("--into-step", "2"))
        self.assertIn("may be a known issue", err)
        self.assertIn("ISS-0002", err)
        self.assertEqual(counts(L, *TABLES), before)
        # a done step cannot take it
        L.x("UPDATE steps SET status='done', change_note='x' WHERE number=2")
        L.commit()
        self.assertIn("step 2 is done", refused(self, L, *self.report("--new-issue", "--into-step", "2")))

    def test_report_finding_opens_gate_when_unplaceable(self):
        L = self.L
        out = ok(L, *self.report("--new-issue"))
        self.assertIn("asked the owner in GATE-0001", out[-1])
        g = L.one("SELECT kind, plan_id, issue_id, blocking, status FROM gates")
        self.assertEqual(g, {"kind": "decision", "plan_id": self.plan, "issue_id": L.id_of("issues", "ISS-0002"),
                             "blocking": 0, "status": "open"})
        self.assertEqual([r["point_id"] for r in L.all("SELECT point_id FROM v_unplanned_discoveries")], ["FIX-1"])

    def test_report_finding_refuses_lines_not_at_commit(self):
        """Lines past the end of the file at the commit, a symbol not in the cited lines, a commit the repository
        does not have, and lines that exist only in the edited working file: exit 1, nothing recorded."""
        L = self.L
        before = counts(L, *TABLES)
        self.assertIn("past its end", refused(self, L, *self.report("--new-issue", where="src/app.py:9-14")))
        self.assertIn("'handler' is not in src/app.py lines 1-3",
                      refused(self, L, *self.report("--new-issue", where="src/app.py:1-3#handler")))
        self.assertIn("is not in the repository",
                      refused(self, L, *self.report("--new-issue", "--commit", "0123456789abcdef0123")))
        self.assertIn("does not exist at commit", refused(self, L, *self.report("--new-issue", where="src/new.py:1")))
        self.repo.write("src/app.py", APP + "".join(f"edit {n}\n" for n in range(20)))   # uncommitted edits
        self.assertIn("past its end", refused(self, L, *self.report("--new-issue", where="src/app.py:15-16")))
        self.assertEqual(counts(L, *TABLES), before)

    def test_report_finding_fills_excerpt_at_commit(self):
        L = self.L
        self.repo.write("src/app.py", APP.replace("def handler", "async def handler"))
        later = self.repo.commit("later")
        self.repo.write("src/app.py", "uncommitted\n")   # never read: the commit is
        ok(L, *self.report("--new-issue", "--commit", later[:12], where="src/app.py:4-5#handler"))
        loc = L.one("SELECT commit_sha, line_start, line_end, excerpt, excerpt_sha256, blob_sha, source "
                    "FROM finding_locations fl JOIN findings f ON f.id=fl.finding_id WHERE f.point_id='FIX-1'")
        want = "\n".join(self.repo.git("show", f"{later}:src/app.py").splitlines()[3:5])
        self.assertEqual(loc["commit_sha"], later)
        self.assertEqual((loc["line_start"], loc["line_end"], loc["excerpt"]), (4, 5, want))
        self.assertEqual(loc["excerpt_sha256"], hashlib.sha256(want.encode()).hexdigest())
        self.assertEqual(loc["blob_sha"], self.repo.git("rev-parse", f"{later}:src/app.py"))
        self.assertEqual(loc["source"], "commit")
        # by default the review's reviewed commit; a symbol alone is looked up there
        ok(L, *self.report("--new-issue", where="src/app.py#handler"))
        loc = L.one("SELECT commit_sha, line_start, excerpt FROM finding_locations fl JOIN findings f "
                    "ON f.id=fl.finding_id WHERE f.point_id='FIX-2'")
        self.assertEqual(loc, {"commit_sha": self.sha, "line_start": 5, "excerpt": "def handler(request):"})


if __name__ == "__main__":
    unittest.main()
