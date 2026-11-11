"""Characterization tests for the event and updated_at triggers (schema version 1).

Rows are inserted with fixed timestamps (the Clock and FIXED); a trigger's own 'now' is asserted only to be a
fresh ledger timestamp different from the fixed one. Assertions that pin a known defect carry a
`# characterization:` comment naming the issue, so the step that fixes it changes them on purpose.
"""
import unittest

from dev.tests._schema_helpers import FIXED, ISO_NOW, SchemaCase

RESOLVING = ("fixed", "accepted-risk", "wont-fix", "duplicate", "obsolete")
REOPENING = ("open", "regressed", "needs-decision")
ALL_STATUSES = ("open", "needs-decision", "planned", "in-progress", "fixed", "verified", "regressed",
                "accepted-risk", "wont-fix", "duplicate", "obsolete")


class TriggerCase(SchemaCase):
    def setUp(self):
        super().setUp()
        self.r1 = self.review()
        self.r2 = self.review()

    def issue_row(self, iid):
        return self.L.one("SELECT * FROM issues WHERE id=?", (iid,))

    def events(self, entity_type, entity_id, event=None):
        sql = "SELECT * FROM events WHERE entity_type=? AND entity_id=?"
        args = [entity_type, entity_id]
        if event:
            sql += " AND event=?"
            args.append(event)
        return self.L.all(sql + " ORDER BY id", args)

    def assertFresh(self, value):
        self.assertRegex(value or "", ISO_NOW)
        self.assertNotEqual(value, FIXED)


class IssueTriggers(TriggerCase):
    def test_issue_insert_logs_opened(self):
        actor = self.actor("matcher")
        iid = self.issue(self.r1, change_actor_id=actor, change_note="new in R1")
        ev = self.events("issue", iid)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["event"], e["from_value"], e["to_value"]), ("opened", None, "open"))
        self.assertEqual((e["review_id"], e["actor_id"], e["note"]), (self.r1, actor, "new in R1"))
        with self.subTest("to_value follows the inserted status"):
            iid2 = self.issue(self.r1, status="needs-decision")
            self.assertEqual(self.events("issue", iid2)[0]["to_value"], "needs-decision")

    def test_issue_status_event_context(self):
        iid = self.issue(self.r1)
        actor, plan = self.actor("fixer"), self.L.plan()
        step = self.L.step(plan, self.L.phase(plan, 1), 1)
        self.L.update("issues", iid, status="fixed", change_actor_id=actor, change_review_id=self.r2,
                      change_step_id=step, change_commit_sha="c" * 40, change_note="fixed it")
        e = self.events("issue", iid, "status-changed")[-1]
        self.assertEqual((e["from_value"], e["to_value"]), ("open", "fixed"))
        self.assertEqual((e["actor_id"], e["review_id"], e["step_id"], e["commit_sha"], e["note"]),
                         (actor, self.r2, step, "c" * 40, "fixed it"))

    def test_issue_resolved_at_rules(self):
        for target in ALL_STATUSES:
            with self.subTest(status=target):
                def go():
                    start = "planned" if target != "planned" else "in-progress"
                    iid = self.issue(self.r1, status=start, resolved_at=FIXED, verified_at=FIXED)
                    self.L.update("issues", iid, status=target)
                    got = self.issue_row(iid)["resolved_at"]
                    if target in RESOLVING:
                        self.assertFresh(got)
                    elif target in REOPENING:
                        self.assertIsNone(got)
                    else:
                        # planned, in-progress and verified keep it.
                        self.assertEqual(got, FIXED)
                self.in_savepoint(go)

    def test_issue_verified_at_rules(self):
        for target in ALL_STATUSES:
            with self.subTest(status=target):
                def go():
                    start = "planned" if target != "planned" else "in-progress"
                    iid = self.issue(self.r1, status=start, resolved_at=FIXED, verified_at=FIXED)
                    self.L.update("issues", iid, status=target)
                    got = self.issue_row(iid)["verified_at"]
                    if target == "verified":
                        self.assertFresh(got)
                    elif target in ("open", "regressed"):
                        self.assertIsNone(got)
                    else:
                        # characterization: ISS-0159 — needs-decision, planned, in-progress and the resolving
                        # statuses keep a stale verified_at (needs-decision clears resolved_at but not verified_at).
                        self.assertEqual(got, FIXED)
                self.in_savepoint(go)

    def test_issue_status_noop_no_event(self):
        iid = self.issue(self.r1)
        before = len(self.events("issue", iid))
        self.L.update("issues", iid, status="open", change_note="same status")
        self.L.update("issues", iid, title="renamed")
        self.assertEqual(len(self.events("issue", iid)), before)
        self.assertEqual(self.events("issue", iid, "status-changed"), [])

    def test_issue_touch_updated_at(self):
        iid = self.issue(self.r1, updated_at=FIXED)
        self.L.update("issues", iid, title="renamed")
        self.assertFresh(self.issue_row(iid)["updated_at"])
        with self.subTest("an explicit updated_at is kept"):
            self.L.update("issues", iid, title="again", updated_at="2001-01-01T00:00:00.000Z")
            self.assertEqual(self.issue_row(iid)["updated_at"], "2001-01-01T00:00:00.000Z")
        with self.subTest("no recursion: the bump itself logs nothing and the row is intact"):
            self.assertEqual(self.issue_row(iid)["title"], "again")
            self.assertEqual([e["event"] for e in self.events("issue", iid)], ["opened"])


class MatchTrigger(TriggerCase):
    def test_match_updates_times_seen(self):
        iid = self.issue(self.r1)
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r1, issue_id=iid)
        self.assertEqual(self.issue_row(iid)["times_seen"], 1, "two findings of one review count once")
        self.L.finding(self.r2, issue_id=iid)
        row = self.issue_row(iid)
        self.assertEqual(row["times_seen"], 2)
        self.assertEqual((row["last_seen_review_id"], row["last_checked_review_id"]), (self.r2, self.r2))

    def test_match_copies_current_state(self):
        iid = self.issue(self.r1, current_state="old state")
        self.L.finding(self.r2, issue_id=iid, before_state="new state")
        self.assertEqual(self.issue_row(iid)["current_state"], "new state")
        with self.subTest("a finding with no before_state keeps it"):
            self.L.finding(self.r2, issue_id=iid)
            self.assertEqual(self.issue_row(iid)["current_state"], "new state")

    def test_match_regresses_fixed_or_verified_issue(self):
        for status in ("fixed", "verified"):
            with self.subTest(status=status):
                def go():
                    iid = self.issue(self.r1, status=status, change_actor_id=self.actor("fixer"))
                    fid = self.L.finding(self.r2, point_id="SPEC-M9", issue_id=iid, match_type="regression")
                    row = self.issue_row(iid)
                    self.assertEqual(row["status"], "regressed")
                    self.assertIsNone(row["change_actor_id"])
                    ev = self.events("issue", iid, "status-changed")
                    self.assertEqual([(e["from_value"], e["to_value"], e["review_id"], e["note"]) for e in ev],
                                     [(status, "regressed", self.r2, "matched by finding SPEC-M9")])
                    self.assertTrue(fid)
                self.in_savepoint(go)

    def test_current_match_leaves_closed_issue_closed(self):
        # characterization: ISS-0088 — a finding matched to an obsolete (or other closed, non-fixed) issue does not
        # reopen it.
        for status in ("obsolete", "wont-fix", "accepted-risk", "duplicate"):
            with self.subTest(status=status):
                def go():
                    iid = self.issue(self.r1, status=status)
                    self.L.finding(self.r2, issue_id=iid, match_type="recurrence")
                    self.assertEqual(self.issue_row(iid)["status"], status)
                    self.assertEqual(self.events("issue", iid, "status-changed"), [])
                self.in_savepoint(go)

    def test_match_logs_seen_event(self):
        iid = self.issue(self.r1)
        self.L.finding(self.r2, point_id="SPEC-L4", issue_id=iid, match_type="still-present")
        ev = self.events("issue", iid, "seen")
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["to_value"], e["review_id"], e["note"], e["actor_id"]),
                         ("still-present", self.r2, "SPEC-L4", None))

    def test_match_same_issue_noop(self):
        iid = self.issue(self.r1)
        fid = self.L.finding(self.r1, issue_id=iid)
        before = self.events("issue", iid)
        self.L.match(fid, iid, "changed")      # same issue_id again, other match_type
        self.L.update("findings", fid, summary="edited")
        self.assertEqual(self.events("issue", iid), before)
        with self.subTest("unmatching logs nothing"):
            self.L.update("findings", fid, issue_id=None)
            self.assertEqual(self.events("issue", iid), before)

    def test_current_match_older_finding_moves_last_seen_back(self):
        iid = self.issue(self.r1)
        late = self.L.finding(self.r2, issue_id=iid)
        early = self.L.finding(self.r1)
        self.assertEqual(self.issue_row(iid)["last_seen_review_id"], self.r2)
        self.L.match(early, iid)
        # characterization: ISS-0011 — matching an older review's finding later moves last_seen (and last_checked)
        # back to the older review. The fix keeps the latest review.
        row = self.issue_row(iid)
        self.assertEqual(row["last_seen_review_id"], self.r1)
        self.assertEqual(row["last_checked_review_id"], self.r1)
        self.assertTrue(late)


class StepAndPlanTriggers(TriggerCase):
    def setUp(self):
        super().setUp()
        self.plan = self.L.plan()
        self.ph = self.L.phase(self.plan, 1)

    def step_row(self, sid):
        return self.L.one("SELECT * FROM steps WHERE id=?", (sid,))

    def new_step(self, **kw):
        return self.L.step(self.plan, self.ph, self.n(), **kw)

    def test_step_status_timestamps(self):
        sid = self.new_step()
        self.L.update("steps", sid, status="doing")
        started = self.step_row(sid)["started_at"]
        self.assertFresh(started)
        self.assertIsNone(self.step_row(sid)["done_at"])
        with self.subTest("doing again keeps the first started_at"):
            self.x("UPDATE steps SET started_at=? WHERE id=?", (FIXED, sid))
            self.L.update("steps", sid, status="waiting-owner")
            self.L.update("steps", sid, status="doing")
            self.assertEqual(self.step_row(sid)["started_at"], FIXED)
        with self.subTest("done sets done_at"):
            self.L.update("steps", sid, status="done")
            self.assertFresh(self.step_row(sid)["done_at"])
        with self.subTest("leaving done clears done_at"):
            # characterization: ISS-0159 — leaving done clears done_at (probed); pinned as today's rule.
            self.L.update("steps", sid, status="doing")
            self.assertIsNone(self.step_row(sid)["done_at"])
        with self.subTest("a jump straight to done leaves started_at empty"):
            # characterization: ISS-0159 — not-started -> done never sets started_at.
            s2 = self.new_step(started_at=None)
            self.L.update("steps", s2, status="done")
            self.assertIsNone(self.step_row(s2)["started_at"])
            self.assertFresh(self.step_row(s2)["done_at"])

    def test_step_status_event(self):
        actor = self.actor("fixer")
        sid = self.new_step()
        self.L.update("steps", sid, status="doing", progress_note="Started the change.", change_actor_id=actor,
                      change_commit_sha="d" * 40)
        ev = self.events("step", sid)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["event"], e["from_value"], e["to_value"]), ("status-changed", "not-started", "doing"))
        self.assertEqual((e["actor_id"], e["step_id"], e["commit_sha"], e["note"]),
                         (actor, sid, "d" * 40, "Started the change."))
        with self.subTest("same status, no event"):
            self.L.update("steps", sid, status="doing", progress_note="still going")
            self.assertEqual(len(self.events("step", sid)), 1)

    def test_step_touch_updated_at(self):
        sid = self.new_step(updated_at=FIXED)
        self.L.update("steps", sid, title="Renamed")
        self.assertFresh(self.step_row(sid)["updated_at"])
        with self.subTest("an explicit updated_at is kept"):
            self.L.update("steps", sid, title="Again", updated_at="2001-01-01T00:00:00.000Z")
            self.assertEqual(self.step_row(sid)["updated_at"], "2001-01-01T00:00:00.000Z")
            self.assertEqual(self.events("step", sid), [])

    def test_plan_status_event(self):
        pid = self.L.plan(updated_at=FIXED)
        self.L.update("plans", pid, status="awaiting-approval")
        self.L.update("plans", pid, status="approved")
        self.L.update("plans", pid, status="approved")
        ev = self.events("plan", pid)
        self.assertEqual([(e["event"], e["from_value"], e["to_value"]) for e in ev],
                         [("status-changed", "drafting", "awaiting-approval"),
                          ("status-changed", "awaiting-approval", "approved")])
        # characterization: ISS-0064 — plan events carry no actor.
        self.assertEqual([e["actor_id"] for e in ev], [None, None])
        self.assertFresh(self.L.scalar("SELECT updated_at FROM plans WHERE id=?", (pid,)))
        with self.subTest("an edit that is not a status change leaves updated_at alone"):
            # Pinned, not yet a recorded issue: plans have no touch trigger (unlike issues and steps), so an edit that
            # is not a status change leaves updated_at as it was.
            self.x("UPDATE plans SET updated_at=? WHERE id=?", (FIXED, pid))
            self.L.update("plans", pid, title="Renamed")
            self.assertEqual(self.L.scalar("SELECT updated_at FROM plans WHERE id=?", (pid,)), FIXED)


if __name__ == "__main__":
    unittest.main()
