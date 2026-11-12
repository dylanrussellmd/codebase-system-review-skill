"""Plan P-0001 steps 57-60, 82 and 83 (schema 3): matching bookkeeping, regressions, review-check outcomes,
lifecycle timestamps, after_state on fixed issues, reopening on recurrence.

Matching is an UPDATE of findings.issue_id (Ledger.match); a finding is never inserted already matched.
"""
import sqlite3
import unittest

from dev.tests._schema_helpers import FIXED, ISO_NOW
from dev.tests.test_p4_integrity_plan import IntegrityCase

ACTIVE = ("open", "needs-decision", "planned", "in-progress", "regressed")
ALL = ("open", "needs-decision", "planned", "in-progress", "fixed", "verified", "regressed", "accepted-risk",
       "wont-fix", "duplicate", "obsolete")


class Base(IntegrityCase):
    def setUp(self):
        super().setUp()
        self.r3 = self.review()

    def row(self, iid):
        return self.L.one("SELECT * FROM issues WHERE id=?", (iid,))

    def new_issue(self, status="open", review=None, **kw):
        if status in ("fixed",):
            kw.setdefault("after_state", "It works now.")
        return self.issue(review or self.r1, status=status, **kw)

    def assertFresh(self, v):
        self.assertRegex(v or "", ISO_NOW)
        self.assertNotEqual(v, FIXED)


class Matching(Base):
    """Step 57."""

    def test_insert_with_issue_id_refused(self):
        iid = self.new_issue("verified")
        n = self.scalar("SELECT count(*) FROM findings")
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.insert("findings", review_id=self.r2, point_id="X-1", severity="M", kind="defect",
                        domain_code="spec", sub="x", summary="x", issue_id=iid, match_type="regression")
        self.assertIn("insert the finding first", str(cm.exception))
        self.assertEqual(self.scalar("SELECT count(*) FROM findings"), n)

    def test_match_on_insert_then_update_marks_regression(self):
        iid = self.new_issue("fixed")
        self.L.finding(self.r1, issue_id=iid)
        fid = self.L.finding(self.r2)
        before = len(self.events("issue", iid, "seen"))
        self.L.match(fid, iid, "regression")
        r = self.row(iid)
        self.assertEqual((r["status"], r["times_seen"]), ("regressed", 2))
        self.assertEqual(len(self.events("issue", iid, "seen")), before + 1)

    def test_match_on_insert_counts(self):
        iid = self.new_issue()
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r2, issue_id=iid, match_type="still-present")
        self.assertEqual(self.row(iid)["times_seen"], 2)

    def test_match_updates_times_seen(self):
        iid = self.new_issue(current_state="old")
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r1, issue_id=iid)
        self.assertEqual(self.row(iid)["times_seen"], 1, "two findings of one review count once")
        self.L.finding(self.r2, issue_id=iid, match_type="changed", before_state="new shape")
        r = self.row(iid)
        self.assertEqual((r["times_seen"], r["last_seen_review_id"], r["current_state"]), (2, self.r2, "new shape"))
        self.assertEqual(self.events("issue", iid, "seen")[-1]["to_value"], "changed")

    def test_match_older_finding_keeps_latest_last_seen(self):
        """Was test_current_match_older_finding_moves_last_seen_back (ISS-0011): matching an older review's
        finding later keeps the latest review as last_seen and last_checked."""
        iid = self.new_issue()
        self.L.finding(self.r2, issue_id=iid, match_type="still-present")
        early = self.L.finding(self.r1)
        self.L.match(early, iid)
        r = self.row(iid)
        self.assertEqual((r["last_seen_review_id"], r["last_checked_review_id"]), (self.r2, self.r2))

    def test_match_same_issue_noop(self):
        iid = self.new_issue()
        fid = self.L.finding(self.r1, issue_id=iid)
        before = self.events("issue", iid)
        self.L.match(fid, iid, "changed")
        self.assertEqual(self.events("issue", iid), before)
        self.assertEqual(self.row(iid)["times_seen"], 1)

    def test_rematch_recomputes_old_issue(self):
        a, b = self.new_issue(), self.new_issue()
        self.L.finding(self.r1, issue_id=a)
        moved = self.L.finding(self.r2, issue_id=a, match_type="still-present")
        self.assertEqual(self.row(a)["times_seen"], 2)
        self.L.match(moved, b, "new")
        self.assertEqual((self.row(a)["times_seen"], self.row(b)["times_seen"]), (1, 1))
        self.assertEqual(self.row(a)["last_seen_review_id"], self.r1)
        ev = self.events("issue", a, "unlinked")
        code_b = self.scalar("SELECT code FROM issues WHERE id=?", (b,))
        self.assertEqual([(e["to_value"]) for e in ev], [code_b])

    def test_rematch_recomputes_both_issues(self):
        self.test_rematch_recomputes_old_issue()

    def test_recurrence_reopens_obsolete(self):
        for status in ("obsolete", "duplicate", "wont-fix"):
            with self.subTest(status=status):
                def go():
                    iid = self.new_issue(status)
                    self.L.finding(self.r2, issue_id=iid, match_type="recurrence")
                    self.assertEqual(self.status(iid), "open")
                    code = self.scalar("SELECT code FROM issues WHERE id=?", (iid,))
                    self.assertEqual(self.L.count("v_known_issues_for_brief", "code=?", (code,)), 1)
                self.in_savepoint(go)

    def test_recurrence_reopens_obsolete_issue(self):
        self.test_recurrence_reopens_obsolete()

    def test_recurrence_on_accepted_risk_stays(self):
        iid = self.new_issue("accepted-risk")
        self.L.finding(self.r2, issue_id=iid, match_type="recurrence")
        self.assertEqual(self.status(iid), "accepted-risk")
        self.assertEqual([e["to_value"] for e in self.events("issue", iid, "recurred")], ["recurrence"])

    def test_match_leaves_closed_issue_closed_without_recurrence(self):
        """Was test_current_match_leaves_closed_issue_closed (ISS-0088): only a recurrence (or regression) match
        reopens a closed issue; a still-present match on an obsolete issue leaves it closed."""
        iid = self.new_issue("obsolete")
        self.L.finding(self.r2, issue_id=iid, match_type="still-present")
        self.assertEqual(self.status(iid), "obsolete")


class Regressions(Base):
    """Step 58."""

    def test_match_regresses_fixed_issue(self):
        iid = self.new_issue("fixed")
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r2, point_id="SPEC-M9", issue_id=iid, match_type="regression")
        r = self.row(iid)
        self.assertEqual((r["status"], r["times_seen"]), ("regressed", 2))
        ev = self.events("issue", iid, "status-changed")
        self.assertEqual([(e["from_value"], e["to_value"]) for e in ev], [("fixed", "regressed")])
        self.assertTrue(ev[0]["note"].startswith("matched by finding SPEC-M9"), ev[0]["note"])

    def test_match_regresses_verified_issue(self):
        iid = self.new_issue("verified")
        self.L.finding(self.r2, issue_id=iid, match_type="regression")
        self.assertEqual(self.status(iid), "regressed")

    def test_regression_marks_verified_issue_regressed(self):
        iid = self.new_issue("verified")
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r2, issue_id=iid, match_type="regression")
        r = self.row(iid)
        self.assertEqual((r["status"], r["times_seen"]), ("regressed", 2))
        code = self.scalar("SELECT code FROM issues WHERE id=?", (iid,))
        self.assertEqual(self.L.count("v_recurring_issues", "code=?", (code,)), 1)

    def test_false_finding_does_not_regress(self):
        iid = self.new_issue("verified", current_state="as it was")
        self.L.finding(self.r1, issue_id=iid)
        fid = self.L.finding(self.r2, verification="false", before_state="claimed")
        self.L.match(fid, iid, "still-present")
        r = self.row(iid)
        self.assertEqual((r["status"], r["times_seen"], r["current_state"]), ("verified", 1, "as it was"))
        self.assertEqual(self.events("issue", iid, "seen")[-1]["to_value"], "false")

    def test_false_finding_match_does_not_regress(self):
        iid = self.new_issue("fixed")
        self.L.finding(self.r1, issue_id=iid)
        fid = self.L.finding(self.r2, verification="false")
        self.L.match(fid, iid, "regression")
        self.assertEqual((self.status(iid), self.row(iid)["times_seen"]), ("fixed", 1))

    def test_regression_needs_regression_match(self):
        for mt in ("still-present", "changed", "recurrence", "continuation"):
            with self.subTest(match_type=mt):
                def go():
                    iid = self.new_issue("fixed")
                    self.L.finding(self.r2, issue_id=iid, match_type=mt)
                    self.assertEqual(self.status(iid), "fixed")
                self.in_savepoint(go)
        with self.subTest("regression on an open issue is refused"):
            iid = self.new_issue("open")
            fid = self.L.finding(self.r2)
            self.raises("UPDATE findings SET issue_id=?, match_type='regression' WHERE id=?", (iid, fid),
                        "regression needs a fixed or verified issue")

    def test_check_and_match_order_independent(self):
        with self.subTest("check fixed, then a finding of the same review"):
            def go():
                iid = self.new_issue("fixed", review=self.r1)
                self.L.check_row(self.r2, iid, "fixed")
                fid = self.L.finding(self.r2)
                self.raises("UPDATE findings SET issue_id=?, match_type='regression' WHERE id=?", (iid, fid),
                            "recorded the issue fixed")
            self.in_savepoint(go)
        with self.subTest("a finding, then check fixed in the same review"):
            def go():
                iid = self.new_issue("fixed", review=self.r1)
                self.L.finding(self.r2, issue_id=iid, match_type="regression")
                self.raises("INSERT INTO review_issue_checks(review_id, issue_id, outcome) VALUES (?,?,'fixed')",
                            (self.r2, iid), "raised the issue again")
            self.in_savepoint(go)

    def test_changed_on_fixed_counts_as_came_back(self):
        """A changed match on a fixed issue no longer regresses it (only match_type regression does), so the
        trigger and v_review_summary agree: a fixed issue that came back is matched as a regression."""
        iid = self.new_issue("fixed")
        self.L.finding(self.r2, issue_id=iid, match_type="changed")
        self.assertEqual(self.status(iid), "fixed")
        jid = self.new_issue("fixed")
        self.L.finding(self.r2, issue_id=jid, match_type="regression")
        self.assertEqual(self.status(jid), "regressed")
        came_back = self.scalar("SELECT sum(came_back) FROM v_review_summary WHERE review=(SELECT code FROM reviews "
                                "WHERE id=?)", (self.r2,))
        self.assertEqual(came_back, 1)

    def test_false_finding_verification_change_recounts(self):
        iid = self.new_issue()
        self.L.finding(self.r1, issue_id=iid)
        fid = self.L.finding(self.r2, issue_id=iid, match_type="still-present")
        self.assertEqual(self.row(iid)["times_seen"], 2)
        self.x("UPDATE findings SET verification='false', verification_note='not real' WHERE id=?", (fid,))
        self.assertEqual(self.row(iid)["times_seen"], 1)
        ev = self.events("finding", fid, "verification-changed")
        self.assertEqual([(e["from_value"], e["to_value"]) for e in ev], [("unverified", "false")])


class Checks(Base):
    """Step 59 (and step 82's test_check_obsolete_closes_issue)."""

    def check(self, iid, outcome, review=None, **kw):
        return self.L.check_row(review or self.r2, iid, outcome, **kw)

    def test_check_outcome_matrix(self):
        expect = {
            ("fixed", "fixed"): "verified",
            **{("fixed", s): "fixed" for s in ACTIVE},
            **{(o, s): "regressed" for o in ("regressed", "still-present", "changed") for s in ("fixed", "verified")},
            **{("obsolete", s): "obsolete" for s in ALL if s != "duplicate"},
        }
        for outcome in ("still-present", "changed", "fixed", "regressed", "obsolete", "not-checked"):
            for status in ALL:
                with self.subTest(outcome=outcome, status=status):
                    def go():
                        iid = self.new_issue(status)
                        self.check(iid, outcome, evidence="seen in src/app.py")
                        self.assertEqual(self.status(iid), expect.get((outcome, status), status))
                        self.assertEqual(self.row(iid)["last_checked_review_id"], self.r2)
                        self.assertEqual([e["to_value"] for e in self.events("issue", iid, "checked")], [outcome])
                    self.in_savepoint(go)

    def test_check_outcomes_move_status(self):
        self.test_check_outcome_matrix()

    def test_check_fixed_verifies(self):
        iid = self.new_issue("fixed")
        self.check(iid, "fixed")
        self.assertEqual(self.status(iid), "verified")

    def test_check_fixed_on_open_marks_fixed_with_resolution(self):
        iid = self.new_issue("open")
        self.check(iid, "fixed", evidence="uses secrets now")
        r = self.row(iid)
        self.assertEqual((r["status"], r["after_state"]), ("fixed", "uses secrets now"))
        self.assertEqual(r["resolution"], "Found fixed by a later review, with no plan step recorded.")

    def test_check_on_planned_issue_names_step(self):
        iid = self.new_issue("planned")
        fid = self.L.finding(self.r1, issue_id=iid)
        p = self.L.plan(reviews=(self.r1,))
        self.L.step(p, self.L.phase(p, 1), 7, findings=(fid,))
        self.check(iid, "fixed")
        code = self.scalar("SELECT code FROM plans WHERE id=?", (p,))
        self.assertEqual(self.row(iid)["resolution"], f"Found fixed by a later review while plan step {code} #7 was not done.")

    def test_check_regressed(self):
        for status in ("fixed", "verified"):
            with self.subTest(status=status):
                def go():
                    iid = self.new_issue(status)
                    self.check(iid, "regressed")
                    self.assertEqual(self.status(iid), "regressed")
                self.in_savepoint(go)

    def test_check_other_outcomes_keep_status(self):
        """Rewritten for the new matrix: not-checked never moves an issue; still-present and changed leave an
        active issue as it is."""
        for outcome in ("not-checked", "still-present", "changed"):
            with self.subTest(outcome=outcome):
                def go():
                    iid = self.new_issue("planned")
                    self.check(iid, outcome)
                    self.assertEqual(self.status(iid), "planned")
                self.in_savepoint(go)

    def test_check_obsolete_closes_issue(self):
        iid = self.new_issue("open")
        self.check(iid, "obsolete", evidence="the module is gone")
        r = self.row(iid)
        self.assertEqual(r["status"], "obsolete")
        self.assertIn("the module is gone", r["resolution"])

    def test_check_sets_last_checked(self):
        iid = self.new_issue("open")
        self.check(iid, "not-checked", review=self.r3)
        self.check(iid, "not-checked", review=self.r2)   # an older review checked later
        self.assertEqual(self.row(iid)["last_checked_review_id"], self.r3)

    def test_check_cannot_replay(self):
        iid = self.new_issue("fixed")
        self.check(iid, "fixed")
        self.raises("INSERT OR REPLACE INTO review_issue_checks(review_id, issue_id, outcome) VALUES (?,?,'fixed')",
                    (self.r2, iid), "already recorded a check")
        self.raises("UPDATE review_issue_checks SET outcome='regressed' WHERE issue_id=?", (iid,), "append-only")
        self.raises("DELETE FROM review_issue_checks WHERE issue_id=?", (iid,), "append-only")
        self.assertEqual(self.status(iid), "verified")

    def test_check_by_first_review_refused(self):
        iid = self.new_issue("open", review=self.r1)
        self.raises("INSERT INTO review_issue_checks(review_id, issue_id, outcome) VALUES (?,?,'still-present')",
                    (self.r1, iid), "first found")

    def test_check_finding_from_other_review_refused(self):
        iid = self.new_issue("open")
        fid = self.L.finding(self.r3)
        self.raises("INSERT INTO review_issue_checks(review_id, issue_id, outcome, finding_id) "
                    "VALUES (?,?,'still-present',?)", (self.r2, iid, fid), "another review")


class Timestamps(Base):
    """Step 60."""

    def test_issue_resolved_at_rules(self):
        for target in ALL:
            with self.subTest(status=target):
                def go():
                    start = "planned" if target != "planned" else "in-progress"
                    iid = self.new_issue(start, resolved_at=FIXED, verified_at=FIXED)
                    if target == "verified":   # schema 7: only a later review's fixed check verifies (tested in
                        self.x("DROP TRIGGER tr_issue_verified_needs_check")   # test_p4_views2_checks)
                    self.L.update("issues", iid, status=target, after_state="done")
                    got = self.row(iid)["resolved_at"]
                    if target == "verified":
                        self.assertEqual(got, FIXED, "kept from the fix")
                    elif target in ("fixed", "accepted-risk", "wont-fix", "duplicate", "obsolete"):
                        self.assertFresh(got)
                    else:
                        self.assertIsNone(got)
                self.in_savepoint(go)

    def test_issue_verified_at_rules(self):
        for target in ALL:
            with self.subTest(status=target):
                def go():
                    start = "planned" if target != "planned" else "in-progress"
                    iid = self.new_issue(start, resolved_at=FIXED, verified_at=FIXED)
                    if target == "verified":   # schema 7: only a later review's fixed check verifies (tested in
                        self.x("DROP TRIGGER tr_issue_verified_needs_check")   # test_p4_views2_checks)
                    self.L.update("issues", iid, status=target, after_state="done")
                    got = self.row(iid)["verified_at"]
                    if target == "verified":
                        self.assertFresh(got)
                    else:
                        self.assertIsNone(got)
                self.in_savepoint(go)

    def test_tr_issue_status_timestamps(self):
        iid = self.new_issue("open")
        self.L.update("issues", iid, status="fixed", after_state="ok")
        self.assertIsNotNone(self.row(iid)["resolved_at"])
        self.L.check_row(self.r2, iid, "fixed", evidence="Works.")   # schema 7: a later review's check verifies
        r = self.row(iid)
        self.assertEqual(r["status"], "verified")
        self.assertIsNotNone(r["resolved_at"])
        self.assertIsNotNone(r["verified_at"])
        self.L.update("issues", iid, status="regressed")
        r = self.row(iid)
        self.assertEqual((r["resolved_at"], r["verified_at"]), (None, None))

    def test_step_status_timestamps(self):
        p = self.L.plan()
        ph = self.L.phase(p, 1)
        s = self.L.step(p, ph, 1)
        self.L.update("steps", s, status="done")
        r = self.L.one("SELECT * FROM steps WHERE id=?", (s,))
        self.assertFresh(r["started_at"])
        self.assertFresh(r["done_at"])
        self.L.update("steps", s, status="doing")
        self.assertIsNone(self.scalar("SELECT done_at FROM steps WHERE id=?", (s,)))

    def test_tr_step_status_timestamps(self):
        self.test_step_status_timestamps()

    def test_review_status_completed_at(self):
        rid = self.review(status="consolidating")
        self.L.update("reviews", rid, status="handed-off")
        self.assertFresh(self.scalar("SELECT completed_at FROM reviews WHERE id=?", (rid,)))
        self.raises("UPDATE reviews SET status='consolidating' WHERE id=?", (rid,), "only with change_note")
        self.L.update("reviews", rid, status="consolidating", change_note="reopened to add a missed report")
        self.assertIsNone(self.scalar("SELECT completed_at FROM reviews WHERE id=?", (rid,)))

    def test_timestamps_follow_status(self):
        self.test_tr_issue_status_timestamps()
        self.test_review_status_completed_at()

    def test_decision_decided_at_set(self):
        did = self.L.decision(status="open")
        self.L.update("decisions", did, status="decided", answer="Keep it.")
        self.assertFresh(self.scalar("SELECT decided_at FROM decisions WHERE id=?", (did,)))

    def test_decision_status_event(self):
        did = self.L.decision(status="open")
        self.L.update("decisions", did, status="decided", answer="Keep it.")
        ev = self.events("decision", did, "status-changed")
        self.assertEqual([(e["from_value"], e["to_value"], e["note"]) for e in ev], [("open", "decided", "Keep it.")])

    def test_gate_status_timestamps(self):
        gid = self.L.gate(options=[{"value": "a", "label": "A"}])
        self.L.update("gates", gid, status="answered", answer_value="a", answered_via="chat")
        g = self.L.one("SELECT * FROM gates WHERE id=?", (gid,))
        self.assertFresh(g["answered_at"])
        self.L.update("gates", gid, status="applied", applied_note="Did A.")
        self.assertFresh(self.scalar("SELECT applied_at FROM gates WHERE id=?", (gid,)))

    def test_gate_cannot_skip_answered(self):
        gid = self.L.gate()
        self.raises("UPDATE gates SET status='applied', applied_note='x' WHERE id=?", (gid,), "answer it before")

    def test_issue_severity_event(self):
        iid = self.new_issue()
        self.L.check_row(self.r2, iid, "still-present")
        self.L.update("issues", iid, severity="H", change_note="worse than thought")
        ev = self.events("issue", iid, "severity-changed")
        self.assertEqual([(e["from_value"], e["to_value"], e["note"]) for e in ev], [("M", "H", "worse than thought")])

    def test_issue_severity_stale_context_documented(self):
        """Fixed (ISS-0016): the severity event logs the context of its own UPDATE, never a leftover one (a bare
        severity change is refused)."""
        iid = self.new_issue()
        self.L.check_row(self.r2, iid, "still-present")
        self.raises("UPDATE issues SET severity='H' WHERE id=?", (iid,), "change_note")

    def test_plan_touch_updates_updated_at(self):
        pid = self.L.plan(updated_at=FIXED)
        self.L.update("plans", pid, title="Renamed")
        self.assertFresh(self.scalar("SELECT updated_at FROM plans WHERE id=?", (pid,)))


class FixedNeedsAfter(Base):
    """Step 82."""

    def test_fixed_requires_after_state(self):
        iid = self.issue(self.r1)
        self.raises("UPDATE issues SET status='fixed', change_note='done' WHERE id=?", (iid,), "after_state")
        self.x("UPDATE issues SET status='fixed', after_state='works', change_note='done' WHERE id=?", (iid,))
        self.assertEqual(self.status(iid), "fixed")

    def test_regressed_moves_after_state_to_event(self):
        iid = self.new_issue("fixed", after_state="Codes use secrets.")
        self.L.update("issues", iid, status="regressed", change_note="came back")
        self.assertIsNone(self.row(iid)["after_state"])
        note = self.events("issue", iid, "status-changed")[-1]["note"]
        self.assertEqual(note, "came back (after_state was: Codes use secrets.)")

    def test_status_event_does_not_reuse_stale_note(self):
        iid = self.new_issue()
        self.L.update("issues", iid, status="planned", change_note="in the plan")
        self.assertIsNone(self.row(iid)["change_note"])
        self.raises("UPDATE issues SET status='in-progress' WHERE id=?", (iid,), "change_note")


if __name__ == "__main__":
    unittest.main()
