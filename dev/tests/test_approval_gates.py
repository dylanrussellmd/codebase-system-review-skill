"""A review or plan is approved only from an applied approval gate answered approve (ISS-0010).

The rule lives in tr_review_transition and tr_plan_transition, so raw SQL is held to it as the commands are; the
approver is the person who answered the gate. A plan is complete only when every step is done or dropped.
"""
import unittest

from dev.tests._p5_writes_helpers import ok, project, refused
from dev.tests._schema_helpers import SchemaCase

AT = "2026-11-01T00:00:00.000Z"
REVIEW_RULE = "domain-approval gate"
PLAN_RULE = "plan-approval gate"
APPROVER_RULE = "who answered the approval gate"
COMPLETE_RULE = "a plan is complete when every step is done or dropped"


class Sql(SchemaCase):
    def setUp(self):
        super().setUp()
        self.owner = self.L.actor("owner")
        self.mgr = self.L.actor("manager")

    def refuse(self, sql, args, needle):
        def go():
            with self.assertRaises(Exception) as cm:
                self.x(sql, args)
            self.assertIn(needle, str(cm.exception))
        self.in_savepoint(go)

    def gate(self, answer, status="applied", **kw):
        return self.L.gate(kind=kw.pop("kind", "domain-approval"), status=status, answer_value=answer,
                           answered_by_actor_id=self.owner, **kw)

    def approve_review(self, rid, by):
        return ("UPDATE reviews SET status='approved', approved_at=?, approved_by_actor_id=? WHERE id=?", (AT, by, rid))

    def test_review_approval_needs_applied_gate(self):
        rid = self.L.review(status="awaiting-approval", approved_at=None)
        with self.subTest("no gate"):
            self.refuse(*self.approve_review(rid, self.owner), REVIEW_RULE)
        with self.subTest("a withdrawn gate"):
            g = self.gate("approve", status="withdrawn", review_id=rid)
            self.refuse(*self.approve_review(rid, self.owner), REVIEW_RULE)
        self.gate("approve", review_id=rid)
        with self.subTest("approved by the manager"):
            self.refuse(*self.approve_review(rid, self.mgr), APPROVER_RULE)
        self.x(*self.approve_review(rid, self.owner))
        self.assertEqual(self.scalar("SELECT approved_by_actor_id FROM reviews WHERE id=?", (rid,)), self.owner)

    def test_approval_follows_answer(self):
        rid = self.L.review(status="awaiting-approval", approved_at=None)
        self.gate("change", review_id=rid)
        self.refuse(*self.approve_review(rid, self.owner), REVIEW_RULE)
        pid = self.L.plan(status="awaiting-approval")
        self.gate("change", kind="plan-approval", plan_id=pid)
        self.refuse("UPDATE plans SET status='approved', approved_at=?, approved_by_actor_id=? WHERE id=?",
                    (AT, self.owner, pid), PLAN_RULE)
        self.gate("approve", kind="plan-approval", plan_id=pid)
        self.x("UPDATE plans SET status='approved', approved_at=?, approved_by_actor_id=? WHERE id=?",
               (AT, self.owner, pid))

    def test_plan_rules_in_schema(self):
        pid = self.L.plan(status="awaiting-approval")
        self.refuse("UPDATE plans SET status='approved', approved_at=?, approved_by_actor_id=? WHERE id=?",
                    (AT, self.owner, pid), PLAN_RULE)
        self.gate("approve", kind="plan-approval", plan_id=pid)
        self.x("UPDATE plans SET status='approved', approved_at=?, approved_by_actor_id=? WHERE id=?",
               (AT, self.owner, pid))
        self.x("UPDATE plans SET status='in-progress' WHERE id=?", (pid,))
        sid = self.L.step(pid, self.L.phase(pid, 1), 1)
        self.refuse("UPDATE plans SET status='complete' WHERE id=?", (pid,), COMPLETE_RULE)
        self.x("UPDATE steps SET status='dropped', change_note='not needed' WHERE id=?", (sid,))
        self.x("UPDATE plans SET status='complete' WHERE id=?", (pid,))


class Commands(unittest.TestCase):
    def test_review_status_needs_applied_gate(self):
        repo, L, sha = project(self, approved=False)
        ok(L, "review-status", "R-0001", "awaiting-approval", "--note", "domains proposed")
        self.assertIn(REVIEW_RULE, refused(self, L, "review-status", "R-0001", "approved", "--note", "n"))
        gate = ok(L, "open-gate", "domain-approval", "--review", "R-0001", "--title", "Domains", "--question", "Run?")[0]
        self.assertEqual([o["value"] for o in L.all("SELECT json_extract(j.value, '$.value') AS value FROM gates, "
                                                     "json_each(gates.options) j WHERE code=?", (gate,))],
                         ["approve", "change"])
        ok(L, "answer-gate", gate, "--value", "change")
        ok(L, "apply-gate", gate, "--note", "the owner wants changes")
        self.assertIn(REVIEW_RULE, refused(self, L, "review-status", "R-0001", "approved", "--note", "n"))
        gate = ok(L, "open-gate", "domain-approval", "--review", "R-0001", "--title", "Domains", "--question", "Run?")[0]
        ok(L, "answer-gate", gate, "--value", "approve")
        ok(L, "apply-gate", gate, "--note", "approved")
        self.assertIn("answered", refused(self, L, "review-status", "R-0001", "approved", "--note", "n",
                                          "--approved-by", "A-0001"))
        ok(L, "review-status", "R-0001", "approved", "--note", "n")
        self.assertEqual(L.scalar("SELECT approved_by_actor_id FROM reviews"),
                         L.scalar("SELECT answered_by_actor_id FROM gates WHERE code=?", (gate,)))


if __name__ == "__main__":
    unittest.main()
