"""new-plan and plan-status (plan P-0001 step 131: FIX-17)."""
import json
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes2_helpers import ok, project, refused


def steps_file(points):
    f = fixtures.temp_dir("steps-") / "steps.jsonl"
    items = [{"kind": "phase", "phase": 1, "title": "Fix first", "goal": "Defects."}]
    items += [{"key": f"A-{n}", "phase": 1, "title": f"Fix {p}", "change": "Change it.", "proof": "test_x: passes",
               "closes": [p]} for n, p in enumerate(points, 1)]
    f.write_text("".join(json.dumps(o) + "\n" for o in items))
    return f


class NewPlanTest(unittest.TestCase):
    def test_new_plan_and_plan_status(self):
        repo, L, sha = project(self)
        fid = L.finding(1, "SPEC-M1", reviewer_run_id=1)
        L.location(fid, "src/app.py", 1, 1)
        L.commit()
        with self.subTest("new-plan refuses an unknown review and a plan of no review"):
            self.assertIn("no review R-0009", refused(self, L, "new-plan", "--title", "T", "--review", "R-0009"))
            self.assertIn("--review", refused(self, L, "new-plan", "--title", "T"))
            self.assertIn("--title", refused(self, L, "new-plan", "--review", "R-0001"))
        with self.subTest("new-plan prints P-0001 with its reviews"):
            out = ok(L, "new-plan", "--title", "Fix plan for R-0001", "--review", "R-0001", "--actor", "A-0002")
            self.assertEqual(out[0], "P-0001")
            self.assertIn("covers R-0001", out[1])
            self.assertEqual(L.one("SELECT code, title, status FROM plans"),
                             {"code": "P-0001", "title": "Fix plan for R-0001", "status": "drafting"})
            self.assertEqual(L.all("SELECT review_id FROM plan_reviews"), [{"review_id": 1}])
            self.assertEqual(L.one("SELECT actor_id FROM events WHERE entity_type='plan' AND event='created'"),
                             {"actor_id": 2})
        with self.subTest("import-steps loads into it"):
            self.assertIn("imported 1 phases and 1 steps",
                          "\n".join(ok(L, "import-steps", "P-0001", steps_file(["SPEC-M1"]), "--actor", "A-0002")))
        with self.subTest("plan-status needs a note and a known status"):
            self.assertIn("--note", refused(self, L, "plan-status", "P-0001", "awaiting-approval"))
            self.assertIn("not one of", refused(self, L, "plan-status", "P-0001", "finished", "--note", "x"))
            self.assertIn("a plan moves", refused(self, L, "plan-status", "P-0001", "in-progress", "--note", "x"))
        ok(L, "plan-status", "P-0001", "awaiting-approval", "--note", "ready for the owner", "--actor", "A-0002")
        with self.subTest("approved is refused before the plan-approval gate is applied"):
            self.assertIn("plan-approval gate", refused(self, L, "plan-status", "P-0001", "approved", "--note", "x"))
            gate = ok(L, "open-gate", "plan-approval", "--plan", "P-0001", "--title", "Approve", "--question",
                      "Start?", "--option", "approve=Approve", "--actor", "A-0002")[0]
            self.assertIn("plan-approval gate", refused(self, L, "plan-status", "P-0001", "approved", "--note", "x"))
            ok(L, "answer-gate", gate, "--value", "approve")
            self.assertIn("plan-approval gate", refused(self, L, "plan-status", "P-0001", "approved", "--note", "x"))
            ok(L, "apply-gate", gate, "--note", "approved", "--actor", "A-0002")
        with self.subTest("and records approved_at and the approver after"):
            self.assertEqual(ok(L, "plan-status", "P-0001", "approved", "--note", f"the owner approved ({gate})",
                                "--actor", "A-0002"), ["P-0001: awaiting-approval -> approved"])
            p = L.one("SELECT status, approved_at IS NOT NULL AS has_at, approved_by_actor_id FROM plans")
            self.assertEqual(p, {"status": "approved", "has_at": 1, "approved_by_actor_id": 1})
            ev = L.one("""SELECT from_value, to_value, actor_id, note FROM events WHERE entity_type='plan'
                          AND event='status-changed' ORDER BY id DESC LIMIT 1""")
            self.assertEqual(ev, {"from_value": "awaiting-approval", "to_value": "approved", "actor_id": 2,
                                  "note": f"the owner approved ({gate})"})
        with self.subTest("complete is refused while a step is still to do"):
            ok(L, "plan-status", "P-0001", "in-progress", "--note", "work started", "--actor", "A-0002")
            self.assertIn("step 1", refused(self, L, "plan-status", "P-0001", "complete", "--note", "x"))
            ok(L, "step-status", "1", "dropped", "--note", "not needed", "--plan", "P-0001")
            ok(L, "plan-status", "P-0001", "complete", "--note", "every step done", "--actor", "A-0002")
        with self.subTest("superseded names its successor"):
            ok(L, "new-plan", "--title", "Second plan", "--review", "R-0001")
            self.assertIn("no plan P-0009", refused(self, L, "plan-status", "P-0001", "superseded", "--note", "x",
                                                    "--superseded-by", "P-0009"))
            ok(L, "plan-status", "P-0001", "superseded", "--note", "replaced", "--superseded-by", "P-0002")
            self.assertEqual(L.scalar("SELECT superseded_by FROM plans WHERE code='P-0001'"), 2)


if __name__ == "__main__":
    unittest.main()
