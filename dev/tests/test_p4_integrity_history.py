"""Plan P-0001 steps 70-72, 74, 79 and 80 (schema 3): append-only history, fresh change context, an event for every
tracked change, actors on owner-facing events, review/plan/step transitions and approvals, gate answer rules.
"""
import json
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.test_p4_integrity_plan import IntegrityCase


class AppendOnly(IntegrityCase):
    """Step 70."""

    def test_history_is_append_only(self):
        iid = self.issue(self.r1)
        fid = self.L.finding(self.r1, issue_id=iid)
        self.L.review_repository(self.r1, "b" * 40)
        lid = self.L.location(fid)
        r3 = self.review()
        self.L.check_row(r3, iid, "still-present")
        jid = self.issue(self.r1)
        rel = self.L.relation(iid, jid)
        did = self.L.decision()
        gid = self.L.gate()
        p = self.L.plan()
        sid = self.L.step(p, self.L.phase(p, 1), 1)
        deletes = {"events": "1", "review_issue_checks": f"issue_id={iid}", "findings": f"id={fid}",
                   "finding_locations": f"id={lid}", "issues": f"id={jid}", "issue_relations": f"id={rel}",
                   "reviews": f"id={r3}", "decisions": f"id={did}", "gates": f"id={gid}", "steps": f"id={sid}"}
        for table, where in deletes.items():
            with self.subTest(delete=table):
                n = self.scalar(f"SELECT count(*) FROM {table}")
                self.raises(f"DELETE FROM {table} WHERE {where}", (), "append-only")
                self.assertEqual(self.scalar(f"SELECT count(*) FROM {table}"), n)
        updates = (("events", "note='x'", "1"), ("review_issue_checks", "outcome='fixed'", f"issue_id={iid}"),
                   ("findings", "summary='rewritten'", f"id={fid}"), ("findings", "severity='H'", f"id={fid}"),
                   ("finding_locations", "path='other.py'", f"id={lid}"),
                   ("finding_locations", "commit_sha='" + "c" * 40 + "'", f"id={lid}"))
        for table, sets, where in updates:
            with self.subTest(update=table, sets=sets):
                self.raises(f"UPDATE {table} SET {sets} WHERE {where}", (), "append-only")
        with self.subTest("matching, verification and fill-refs updates are allowed"):
            self.x("UPDATE findings SET verification='confirmed', verification_note='checked', match_confidence='likely'"
                   " WHERE id=?", (fid,))
            self.x("UPDATE finding_locations SET excerpt='x = 1', excerpt_sha256='h', blob_sha='b', line_end=1 "
                   "WHERE id=?", (lid,))

    def test_events_are_append_only(self):
        self.issue(self.r1)
        n = self.scalar("SELECT count(*) FROM events")
        self.assertGreater(n, 0)
        self.raises("UPDATE events SET note='edited'", (), "append-only")
        self.raises("DELETE FROM events", (), "append-only")
        self.assertEqual(self.scalar("SELECT count(*) FROM events"), n)

    def test_events_are_immutable(self):
        self.test_events_are_append_only()

    def test_findings_cannot_be_deleted(self):
        fid = self.L.finding(self.r1)
        self.raises("DELETE FROM findings WHERE id=?", (fid,), "append-only")

    def test_review_delete_restricted(self):
        r3 = self.review()
        self.raises("DELETE FROM reviews WHERE id=?", (r3,), "abandoned")
        self.L.update("reviews", r3, status="abandoned")

    def test_decision_answer_change_logged(self):
        did = self.L.decision(status="open")
        self.L.update("decisions", did, status="decided", answer="Keep 14 days.")
        self.L.update("decisions", did, answer="Use 7 days.", rationale="The owner changed their mind.")
        ev = self.events("decision", did, "answer-changed")
        self.assertEqual([(e["from_value"], e["to_value"]) for e in ev], [("Keep 14 days.", "Use 7 days.")])


class ChangeContext(IntegrityCase):
    """Step 71."""

    def test_issue_status_event_context(self):
        iid = self.issue(self.r1)
        actor, p = self.actor("fixer"), self.L.plan()
        step = self.L.step(p, self.L.phase(p, 1), 1)
        self.x("UPDATE issues SET status='fixed', after_state='ok', change_actor_id=?, change_review_id=?, "
               "change_step_id=?, change_commit_sha=?, change_note='fixed it' WHERE id=?",
               (actor, self.r2, step, "c" * 40, iid))
        e = self.L.one("SELECT * FROM events WHERE entity_type='issue' AND entity_id=? AND event='status-changed'",
                       (iid,))
        self.assertEqual((e["actor_id"], e["review_id"], e["step_id"], e["commit_sha"], e["note"]),
                         (actor, self.r2, step, "c" * 40, "fixed it"))
        self.x("UPDATE issues SET status='fixed', change_note='same status' WHERE id=?", (iid,))
        self.assertEqual(len(self.events("issue", iid, "status-changed")), 1)

    def test_status_change_requires_fresh_context(self):
        iid = self.issue(self.r1)
        self.L.check_row(self.r2, iid, "still-present", evidence="still there")
        self.raises("UPDATE issues SET status='planned' WHERE id=?", (iid,), "change_note")
        self.x("UPDATE issues SET status='planned', change_note='in P-0001' WHERE id=?", (iid,))
        e = self.events("issue", iid, "status-changed")[-1]
        self.assertEqual(e["note"], "in P-0001")
        self.assertIsNone(self.scalar("SELECT actor_id FROM events WHERE entity_type='issue' AND entity_id=? "
                                      "AND event='status-changed'", (iid,)))

    def test_context_cleared_after_log(self):
        iid = self.issue(self.r1, change_actor_id=self.actor("matcher"), change_note="new")
        row = self.L.one("SELECT change_actor_id, change_review_id, change_step_id, change_commit_sha, change_note "
                         "FROM issues WHERE id=?", (iid,))
        self.assertEqual(set(row.values()), {None})
        self.x("UPDATE issues SET severity='H', change_note='worse', change_actor_id=? WHERE id=?",
               (self.actor("matcher"), iid))
        row = self.L.one("SELECT change_actor_id, change_note FROM issues WHERE id=?", (iid,))
        self.assertEqual(set(row.values()), {None})
        self.assertEqual(self.events("issue", iid, "severity-changed")[-1]["note"], "worse")

    def test_status_context_not_reused(self):
        iid = self.issue(self.r1)
        self.x("UPDATE issues SET status='planned', change_note='first' WHERE id=?", (iid,))
        self.raises("UPDATE issues SET status='in-progress' WHERE id=?", (iid,), "change_note")

    def test_waiting_owner_logs_owner_question(self):
        p = self.L.plan()
        sid = self.L.step(p, self.L.phase(p, 1), 1, owner_question="Keep 14 days?")
        self.x("UPDATE steps SET status='waiting-owner' WHERE id=?", (sid,))
        self.assertEqual(self.events("step", sid, "status-changed")[-1]["note"], "Keep 14 days?")
        self.raises("UPDATE steps SET status='doing' WHERE id=?", (sid,), "change_note")
        self.x("UPDATE steps SET status='doing', change_note='answered: keep 14' WHERE id=?", (sid,))
        self.assertEqual(self.events("step", sid, "status-changed")[-1]["note"], "answered: keep 14")
        self.assertIsNone(self.scalar("SELECT change_note FROM steps WHERE id=?", (sid,)))


class Events(IntegrityCase):
    """Step 72: the twelve A3 statements each write exactly one event."""

    def test_every_tracked_change_writes_an_event(self):
        iid = self.issue(self.r1, title="Old title")
        jid = self.issue(self.r1)
        fid = self.L.finding(self.r1, summary="Old summary")
        gid = self.L.gate(options=[{"value": "a", "label": "A"}, {"value": "b", "label": "B"}])
        p = self.L.plan(title="Old plan")
        ph = self.L.phase(p, 1)
        sid = self.L.step(p, ph, 1, title="Old step")
        statements = (
            ("UPDATE issues SET title='New title' WHERE id=?", (iid,), ("issue", iid, "title-changed", "Old title",
                                                                       "New title")),
            ("UPDATE issues SET recommendation='Do X' WHERE id=?", (iid,), ("issue", iid, "recommendation-changed",
                                                                           None, "Do X")),
            ("UPDATE issues SET before_state='was' WHERE id=?", (iid,), ("issue", iid, "before-state-changed", None,
                                                                        "was")),
            ("UPDATE findings SET verification='confirmed' WHERE id=?", (fid,),
             ("finding", fid, "verification-changed", "unverified", "confirmed")),
            ("INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, rationale) VALUES (?,?,'related-to','x')",
             (iid, jid), ("relation", None, "created", "ISS-0001", "related-to ISS-0002")),
            ("INSERT INTO metrics(review_id, name, value, method) VALUES (?, 'pages', 3, 'counted')", (self.r1,),
             ("review", self.r1, "metric-recorded", None, "3.0")),
            ("INSERT INTO review_issue_checks(review_id, issue_id, outcome) VALUES (?,?,'not-checked')", (self.r2, iid),
             ("issue", iid, "checked", None, "not-checked")),
            ("INSERT INTO gates(code, kind, title, question) VALUES ('GATE-0002','other','New gate','q')", (),
             ("gate", None, "opened", None, "open")),
            ("INSERT INTO steps(plan_id, phase_id, number, title, change, proof) VALUES (?,?,2,'New step','c','p')",
             (p, ph), ("step", None, "created", None, "not-started")),
            ("UPDATE steps SET title='Renamed step' WHERE id=?", (sid,), ("step", sid, "title-changed", "Old step",
                                                                         "Renamed step")),
            ("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, sub, summary) "
             "VALUES (?,'NEW-1','M','defect','spec','s','A new point.')", (self.r1,),
             ("finding", None, "created", None, "NEW-1")),
            ("UPDATE plans SET title='New plan' WHERE id=?", (p,), ("plan", p, "title-changed", "Old plan",
                                                                   "New plan")),
            ("UPDATE gates SET answer_value='a' WHERE id=?", (gid,), None),
            ("UPDATE gates SET answer_value='b' WHERE id=?", (gid,), ("gate", gid, "answer-changed", "a", "b")),
        )
        for sql, args, want in statements:
            with self.subTest(sql=sql[:60]):
                last = self.scalar("SELECT max(id) FROM events")
                self.x(sql, args)
                new = self.L.all("SELECT entity_type, entity_id, event, from_value, to_value FROM events WHERE id>?",
                                 (last,))
                if want is None:
                    continue
                self.assertEqual(len(new), 1, new)
                got = new[0]
                self.assertEqual((got["entity_type"], got["event"], got["from_value"]), (want[0], want[2], want[3]))
                if want[1] is not None:
                    self.assertEqual(got["entity_id"], want[1])
                if want[4] is not None:
                    self.assertEqual(got["to_value"], want[4])

    def test_decision_inserted_decided_logged(self):
        did = self.L.decision(status="decided", answer="Yes.")
        ev = self.events("decision", did)
        self.assertEqual([(e["event"], e["to_value"], e["note"]) for e in ev], [("created", "decided", "Yes.")])
        self.assertIsNotNone(self.scalar("SELECT decided_at FROM decisions WHERE id=?", (did,)))

    def test_timeline_shows_every_check(self):
        iid = self.issue(self.r1)
        code = self.scalar("SELECT code FROM issues WHERE id=?", (iid,))
        for r, outcome in ((self.r2, "still-present"), (self.review(), "changed"), (self.review(), "not-checked")):
            self.L.check_row(r, iid, outcome)
        rows = self.L.all("SELECT event, to_value FROM v_issue_timeline WHERE code=? AND event='checked'", (code,))
        self.assertEqual([r["to_value"] for r in rows], ["still-present", "changed", "not-checked"])

    def test_v_issue_timeline(self):
        iid = self.issue(self.r1, change_actor_id=self.actor("matcher"))
        code = self.scalar("SELECT code FROM issues WHERE id=?", (iid,))
        self.L.check_row(self.r2, iid, "still-present")
        rows = self.L.all("SELECT * FROM v_issue_timeline WHERE code=?", (code,))
        self.assertEqual([r["event"] for r in rows], ["opened", "checked"])
        self.assertTrue(rows[0]["actor"])
        self.assertEqual(rows[1]["review"], self.scalar("SELECT code FROM reviews WHERE id=?", (self.r2,)))


class Actors(IntegrityCase):
    """Step 74."""

    def test_owner_events_have_actor(self):
        owner, mgr = self.actor("owner"), self.actor("manager")
        gid = self.L.gate(options=[{"value": "a", "label": "A"}])
        self.x("UPDATE gates SET status='answered', answer_value='a', answer_text='go', answered_by_actor_id=? "
               "WHERE id=?", (owner, gid))
        self.x("UPDATE gates SET status='applied', applied_note='Did A.', change_actor_id=? WHERE id=?", (mgr, gid))
        ev = self.events("gate", gid, "status-changed")
        got = [(e["to_value"], e["note"]) for e in ev]
        self.assertEqual(got, [("answered", "a: go"), ("applied", "Did A.")])
        self.assertEqual([r[0] for r in self.x("SELECT actor_id FROM events WHERE entity_type='gate' AND entity_id=? "
                                               "AND event='status-changed' ORDER BY id", (gid,))], [owner, mgr])
        r3 = self.review(status="consolidating")
        self.x("UPDATE reviews SET status='handed-off', change_actor_id=?, change_note='done' WHERE id=?", (mgr, r3))
        p = self.L.plan()
        self.x("UPDATE plans SET status='awaiting-approval', change_actor_id=? WHERE id=?", (mgr, p))
        for etype, eid in (("review", r3), ("plan", p)):
            self.assertEqual(self.scalar("SELECT actor_id FROM events WHERE entity_type=? AND entity_id=? "
                                         "AND event='status-changed' ORDER BY id DESC LIMIT 1", (etype, eid)), mgr)
        self.assertIsNone(self.scalar("SELECT change_actor_id FROM plans WHERE id=?", (p,)))

    def test_every_event_has_actor(self):
        self.test_owner_events_have_actor()

    def test_gate_status_event_note(self):
        self.test_owner_events_have_actor()

    def test_decided_decision_has_decided_at(self):
        did = self.L.decision()
        self.x("UPDATE decisions SET status='decided', answer='yes' WHERE id=?", (did,))
        self.assertIsNotNone(self.scalar("SELECT decided_at FROM decisions WHERE id=?", (did,)))

    def test_gate_answer_records_owner_actor(self):
        """A viewer answer sets answered_by_actor_id to the owner's actors row (made on first use) and the
        answered event carries it."""
        mod = fixtures.load_ledger_module()
        gid = self.L.gate("GATE-0001", options=[{"value": "a", "label": "A"}])
        self.assertIsNone(self.scalar("SELECT id FROM actors WHERE kind='owner'"))
        res = mod.answer_gate(self.L.db, "GATE-0001", {"answer_value": "a"})
        self.assertEqual(res, {"ok": True})
        owner = self.scalar("SELECT id FROM actors WHERE kind='owner'")
        self.assertIsNotNone(owner)
        self.assertEqual(self.scalar("SELECT answered_by_actor_id FROM gates WHERE id=?", (gid,)), owner)
        self.assertEqual(self.scalar("SELECT actor_id FROM events WHERE entity_type='gate' AND entity_id=? "
                                     "AND event='status-changed'", (gid,)), owner)

    def test_migration_backfills_answered_by(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE gates SET status='answered', answer_value='approve', answered_by='owner', "
                    "answered_via='viewer' WHERE code='GATE-0002'")
        con.commit()
        con.close()
        p = fixtures.run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        con = sqlite3.connect(str(db))
        try:
            owner = con.execute("SELECT id FROM actors WHERE kind='owner'").fetchone()[0]
            self.assertEqual(con.execute("SELECT answered_by_actor_id FROM gates WHERE code='GATE-0002'").fetchone()[0],
                             owner)
        finally:
            con.close()


class Transitions(IntegrityCase):
    """Step 79."""

    def test_review_status_transitions(self):
        rid = self.review(status="planning")
        self.raises("UPDATE reviews SET status='handed-off', approved_at=? WHERE id=?",
                    ("2026-11-01T00:00:00.000Z", rid), "a review moves")
        self.raises("UPDATE reviews SET status='running' WHERE id=?", (rid,), "")
        path = ("awaiting-approval", "planning", "awaiting-approval", "approved", "running", "verifying", "running",
                "verifying", "indexing", "consolidating", "handed-off")
        for st in path:
            extra = ", approved_at='2026-11-01T00:00:00.000Z'" if st == "approved" else ""
            self.x(f"UPDATE reviews SET status=?{extra} WHERE id=?", (st, rid))
        self.assertEqual(self.scalar("SELECT status FROM reviews WHERE id=?", (rid,)), "handed-off")
        with self.subTest("skipping ahead after approval is allowed; back past running is not"):
            r = self.review(status="approved")
            self.x("UPDATE reviews SET status='indexing' WHERE id=?", (r,))
            self.raises("UPDATE reviews SET status='approved' WHERE id=?", (r,), "a review moves")
        with self.subTest("abandoned from anywhere"):
            r = self.review(status="planning")
            self.x("UPDATE reviews SET status='abandoned' WHERE id=?", (r,))

    def test_review_cannot_run_before_approval(self):
        rid = self.review(status="awaiting-approval")
        gid = self.L.gate(kind="domain-approval", review_id=rid, options=[{"value": "approve", "label": "Approve"}])
        self.raises("UPDATE reviews SET status='approved', approved_at='2026-11-01T00:00:00.000Z' WHERE id=?", (rid,),
                    "domain-approval gate")
        self.raises("UPDATE reviews SET status='approved' WHERE id=?", (rid,), "")   # no approved_at
        self.x("UPDATE gates SET status='answered', answer_value='approve' WHERE id=?", (gid,))
        self.raises("UPDATE reviews SET status='approved', approved_at='2026-11-01T00:00:00.000Z' WHERE id=?", (rid,),
                    "domain-approval gate")
        self.x("UPDATE gates SET status='applied', applied_note='Approved as listed.' WHERE id=?", (gid,))
        self.x("UPDATE reviews SET status='approved', approved_at='2026-11-01T00:00:00.000Z' WHERE id=?", (rid,))

    def test_findings_refused_before_approval(self):
        for status in ("planning", "awaiting-approval", "abandoned"):
            with self.subTest(status=status):
                rid = self.review(status=status)
                self.raises("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, sub, summary) "
                            "VALUES (?,'X-1','M','defect','spec','s','t')", (rid,), "approved review")

    def test_reviewer_run_requires_approved_included_domain(self):
        rid = self.review(status="planning")
        self.L.review_domain(rid, "spec")
        self.L.review_domain(rid, "security", "skipped")
        actor = self.actor("reviewer")
        sql = "INSERT INTO reviewer_runs(review_id, domain_code, actor_id, label, scope) VALUES (?,?,?,?, 'all')"
        self.raises(sql, (rid, "spec", actor, "spec-A"), "approved review")
        for st in ("awaiting-approval", "approved"):
            self.x(f"UPDATE reviews SET status=?{', approved_at=?' if st == 'approved' else ''} WHERE id=?",
                   (st, "2026-11-01T00:00:00.000Z", rid) if st == "approved" else (st, rid))
        self.x(sql, (rid, "spec", actor, "spec-A"))
        self.raises(sql, (rid, "security", actor, "sec-A"), "included")

    def test_plan_status_transitions(self):
        p = self.L.plan()
        self.raises("UPDATE plans SET status='in-progress' WHERE id=?", (p,), "a plan moves")
        gid = self.L.gate(kind="plan-approval", plan_id=p, options=[{"value": "ok", "label": "OK"}])
        self.x("UPDATE plans SET status='awaiting-approval' WHERE id=?", (p,))
        self.raises("UPDATE plans SET status='approved', approved_at='2026-11-01T00:00:00.000Z' WHERE id=?", (p,),
                    "plan-approval gate")
        self.x("UPDATE gates SET status='answered', answer_value='ok' WHERE id=?", (gid,))
        self.x("UPDATE gates SET status='applied', applied_note='Approved.' WHERE id=?", (gid,))
        self.raises("UPDATE plans SET status='approved' WHERE id=?", (p,), "CHECK")
        self.x("UPDATE plans SET status='approved', approved_at='2026-11-01T00:00:00.000Z' WHERE id=?", (p,))
        self.x("UPDATE plans SET status='in-progress' WHERE id=?", (p,))
        self.x("UPDATE plans SET status='abandoned' WHERE id=?", (p,))
        self.raises("UPDATE plans SET status='drafting' WHERE id=?", (p,), "change_note")

    def test_blocking_gate_stops_step(self):
        p = self.L.plan()
        sid = self.L.step(p, self.L.phase(p, 1), 1)
        gid = self.L.gate(kind="step-input", step_id=sid, blocking=1, options=[{"value": "a", "label": "A"}])
        self.raises("UPDATE steps SET status='doing', change_note='go' WHERE id=?", (sid,), "blocking gate")
        self.x("UPDATE gates SET status='answered', answer_value='a' WHERE id=?", (gid,))
        self.raises("UPDATE steps SET status='done', change_note='go' WHERE id=?", (sid,), "blocking gate")
        self.x("UPDATE gates SET status='applied', applied_note='Used A.' WHERE id=?", (gid,))
        self.x("UPDATE steps SET status='doing', change_note='go' WHERE id=?", (sid,))


class GateAnswers(IntegrityCase):
    """Step 80."""

    def gate(self, **kw):
        kw.setdefault("options", [{"value": "keep", "label": "Keep"}, {"value": "change", "label": "Change"}])
        return self.L.gate(**kw)

    def test_answered_gate_cannot_be_overwritten(self):
        gid = self.gate()
        self.x("UPDATE gates SET status='answered', answer_value='keep', answered_by='owner', answered_via='viewer' "
               "WHERE id=?", (gid,))
        n = self.scalar("SELECT count(*) FROM events")
        self.raises("UPDATE gates SET answer_value='change' WHERE id=?", (gid,), "cannot be changed")
        self.raises("UPDATE gates SET answered_via='chat' WHERE id=?", (gid,), "cannot be changed")
        self.raises("UPDATE gates SET answered_by_actor_id=? WHERE id=?", (self.actor("owner"), gid),
                    "cannot be changed")
        self.assertEqual(self.scalar("SELECT count(*) FROM events"), n)

    def test_applied_gate_requires_note(self):
        gid = self.gate()
        self.x("UPDATE gates SET status='answered', answer_value='keep' WHERE id=?", (gid,))
        self.raises("UPDATE gates SET status='applied' WHERE id=?", (gid,), "CHECK")

    def test_answer_value_must_be_an_option(self):
        gid = self.gate()
        self.raises("UPDATE gates SET status='answered', answer_value='neither' WHERE id=?", (gid,), "option values")
        nid = self.gate(allow_text=0)
        self.raises("UPDATE gates SET status='answered', answer_text='my own words' WHERE id=?", (nid,),
                    "no free text")
        # step 158: a gate without free text takes its option alone
        self.raises("UPDATE gates SET status='answered', answer_value='change', answer_text='and why' WHERE id=?",
                    (nid,), "no free text")
        self.x("UPDATE gates SET status='answered', answer_value='change' WHERE id=?", (nid,))

    def test_gate_options_shape_checked(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.L.gate(options=json.dumps([{"label": "no value"}]))

    def test_open_to_applied_refused(self):
        gid = self.gate()
        self.raises("UPDATE gates SET status='applied', applied_note='x' WHERE id=?", (gid,), "answer it before")
        self.x("UPDATE gates SET status='withdrawn' WHERE id=?", (gid,))
        self.raises("UPDATE gates SET status='answered', answer_value='keep' WHERE id=?", (gid,),
                    ("answer it before", "cannot be changed"))


if __name__ == "__main__":
    unittest.main()
