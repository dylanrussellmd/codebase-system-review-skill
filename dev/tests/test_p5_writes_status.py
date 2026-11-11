"""issue-status, step-status and import-steps (plan P-0001 step 99: ABS-H3)."""
import json
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import counts, ok, project, refused


def plan_with_points(test, points=("SPEC-M1", "SPEC-M2", "SPEC-L1")):
    repo, L, sha = project(test)
    for pid in points:
        fid = L.finding(1, pid, reviewer_run_id=1)
        L.location(fid, "src/app.py", 1, 1)
    pid = L.plan("P-0001", reviews=(1,))
    L.commit()
    return repo, L, pid


def write_jsonl(items):
    f = fixtures.temp_dir("steps-") / "steps.jsonl"
    f.write_text("".join(json.dumps(o) + "\n" for o in items))
    return f


PHASES = [{"kind": "phase", "phase": 1, "title": "Safety net", "goal": "Tests first."},
          {"kind": "phase", "phase": 2, "title": "Fix first", "goal": "Defects."}]


class IssueStatusTest(unittest.TestCase):
    def test_issue_status_command_logs_context(self):
        repo, L, pid = plan_with_points(self)
        L.issue("ISS-0001", first_seen_review_id=1)
        ph = L.phase(pid, 1)
        L.step(pid, ph, 1, findings=(1,))
        L.commit()
        self.assertIn("give --note", refused(self, L, "issue-status", "ISS-0001", "in-progress"))
        self.assertIn("needs --after", refused(self, L, "issue-status", "ISS-0001", "fixed", "--note", "done"))
        self.assertIn("not one of", refused(self, L, "issue-status", "ISS-0001", "closed", "--note", "x"))
        self.assertIn("needs --resolution", refused(self, L, "issue-status", "ISS-0001", "wont-fix", "--note", "x"))
        sha = repo.head()
        out = ok(L, "issue-status", "ISS-0001", "fixed", "--note", "step 1 fixed it", "--after", "It refuses now.",
                 "--step", "1", "--commit", sha, "--review", "R-0001", "--actor", "A-0001")
        self.assertEqual(out, ["ISS-0001: open -> fixed"])
        ev = L.one("""SELECT from_value, to_value, actor_id, review_id, step_id, commit_sha, note FROM events
                      WHERE entity_type='issue' AND event='status-changed'""")
        self.assertEqual(ev, {"from_value": "open", "to_value": "fixed", "actor_id": 1, "review_id": 1,
                              "step_id": 1, "commit_sha": sha, "note": "step 1 fixed it"})
        self.assertEqual(L.one("SELECT after_state, change_note FROM issues"),
                         {"after_state": "It refuses now.", "change_note": None})   # context cleared

    def test_step_status_waiting_owner_needs_question(self):
        repo, L, pid = plan_with_points(self)
        ph = L.phase(pid, 1)
        L.step(pid, ph, 1, findings=(1,), drafter_key="A-1")
        L.commit()
        before = counts(L, "events")
        self.assertIn("needs --question", refused(self, L, "step-status", "1", "waiting-owner", "--note", "stuck"))
        self.assertEqual(counts(L, "events"), before)
        ok(L, "step-status", "A-1", "waiting-owner", "--question", "Keep the old URL?")
        self.assertEqual(L.one("SELECT status, owner_question FROM steps"),
                         {"status": "waiting-owner", "owner_question": "Keep the old URL?"})
        self.assertIn("give --note", refused(self, L, "step-status", "P-0001#1", "doing"))
        ok(L, "step-status", "P-0001#1", "done", "--note", "Old URLs redirect now.")
        self.assertEqual(L.one("SELECT status, progress_note FROM steps"),
                         {"status": "done", "progress_note": "Old URLs redirect now."})


class ImportStepsTest(unittest.TestCase):
    def test_import_steps_rejects_unknown_point_and_bad_depends(self):
        repo, L, pid = plan_with_points(self)
        before = counts(L, "phases", "steps", "step_findings", "step_dependencies", "events")
        for bad, why in (
                ({"key": "A-1", "phase": 1, "order": 1, "title": "T", "change": "C", "proof": "P",
                  "closes": ["SPEC-H9"]}, "no point SPEC-H9"),
                ({"key": "A-1", "phase": 1, "order": 1, "title": "T", "change": "C", "proof": "P",
                  "closes": ["SPEC-M1"], "depends": ["A-2"]}, "not earlier"),
                ({"key": "A-1", "phase": 1, "order": 1, "title": "T", "change": "C", "proof": "P",
                  "closes": ["SPEC-M1"], "depends": ["Z-9"]}, "unknown step"),
                ({"key": "A-1", "phase": 7, "order": 1, "title": "T", "change": "C", "proof": "P",
                  "closes": ["SPEC-M1"]}, "phase 7 is not a phase"),
                ({"key": "A-1", "phase": 1, "order": 1, "title": "T", "change": "C", "proof": "P",
                  "closes": ["SPEC-M2"]}, "closed by 2 steps")):
            f = write_jsonl(PHASES + [bad, {"key": "A-2", "phase": 2, "order": 1, "title": "T2", "change": "C",
                                            "proof": "P", "closes": ["SPEC-M2"]}])
            err = refused(self, L, "import-steps", "P-0001", f)
            self.assertIn(why, err)
            self.assertEqual(counts(L, "phases", "steps", "step_findings", "step_dependencies", "events"), before)

    def test_import_steps_round_trip(self):
        repo, L, pid = plan_with_points(self)
        f = write_jsonl(PHASES + [
            {"key": "B-1", "phase": 2, "order": 1, "title": "Fix the parser", "change": "C", "proof": "P",
             "closes": ["R-0001:SPEC-M1", "SPEC-M2"], "depends": ["A-1"],
             "multi_reason": {"SPEC-M2": "both halves"}},
            {"key": "A-1", "phase": 1, "order": 1, "title": "Pin it", "change": "C", "proof": "P",
             "closes": ["SPEC-M2", "SPEC-L1"], "multi_reason": {"SPEC-M2": "pinned first"}, "behavior_change": True,
             "owner_need": "confirm", "owner_question": "OK?"}])
        out = ok(L, "import-steps", "P-0001", f, "--actor", "A-0001")
        self.assertEqual(out[0], "P-0001: imported 2 phases and 2 steps (1-2)")
        self.assertEqual(out[1], "coverage: 0 points of the plan's reviews closed by no step, 0 coverage problems")
        self.assertEqual(L.all("SELECT number, drafter_key, behavior_change, owner_need FROM steps ORDER BY number"),
                         [{"number": 1, "drafter_key": "A-1", "behavior_change": 1, "owner_need": "confirm"},
                          {"number": 2, "drafter_key": "B-1", "behavior_change": 0, "owner_need": "none"}])
        self.assertEqual(L.scalar("SELECT count(*) FROM step_dependencies"), 1)
        self.assertEqual(L.count("v_uncovered_findings"), 0)
        self.assertEqual(L.count("v_coverage_problems"), 0)
        # a second file continues the numbering and may depend on steps already loaded
        f2 = write_jsonl([{"key": "B-2", "phase": 2, "order": 2, "title": "Later", "change": "C", "proof": "P",
                           "closes": [], "no_findings_reason": "the follow-up review", "depends": ["B-1", 1]}])
        self.assertEqual(ok(L, "import-steps", "P-0001", f2)[0], "P-0001: imported 0 phases and 1 steps (3-3)")
        self.assertEqual(L.scalar("SELECT count(*) FROM step_dependencies"), 3)


if __name__ == "__main__":
    unittest.main()
