"""Characterization tests for the reporting views: each returns exactly the expected rows for a positive fixture and
none for a negative one. `# characterization:` comments mark assertions that pin a known defect, naming the issue.
"""
import unittest

from dev.tests._schema_helpers import SchemaCase


class ReportingViews(SchemaCase):
    def setUp(self):
        super().setUp()
        self.r1 = self.review("R-0001")
        self.r2 = self.review("R-0002")

    def view(self, name, order=""):
        return self.L.all(f"SELECT * FROM {name} {order}")

    def test_v_recurring_issues(self):
        with self.subTest("negative: seen once, never regressed"):
            once = self.issue(self.r1, code="ISS-0001")
            self.L.finding(self.r1, issue_id=once)
            self.assertEqual(self.view("v_recurring_issues"), [])
        # path 1: raised by two reviews
        twice = self.issue(self.r1, code="ISS-0002", title="Twice")
        self.L.finding(self.r1, issue_id=twice)
        self.L.finding(self.r2, issue_id=twice)
        # path 2: seen once but came back after a fix (twice: fixed -> regressed -> fixed -> regressed)
        back = self.issue(self.r1, code="ISS-0003", title="Back", status="fixed")
        self.L.update("issues", back, status="regressed")
        self.L.update("issues", back, status="fixed", after_state="Fixed again.")   # schema 3 needs after_state
        self.L.update("issues", back, status="regressed")
        self.assertEqual(self.view("v_recurring_issues"), [
            {"code": "ISS-0003", "title": "Back", "severity": "M", "status": "regressed", "times_seen": 1,
             "regressions": 2, "reviews": None},
            {"code": "ISS-0002", "title": "Twice", "severity": "M", "status": "open", "times_seen": 2,
             "regressions": 0, "reviews": "R-0001, R-0002"},
        ])

    def test_v_recurring_issues_ignores_false_findings(self):
        # Was test_current_v_recurring_issues_counts_false_findings (characterization of ISS-0007). Step 58
        # (schema 3): a false finding is not a sighting, so times_seen stays 1 and the issue is not recurring.
        iid = self.issue(self.r1, code="ISS-0001")
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r2, issue_id=iid, verification="false")
        self.assertEqual(self.L.scalar("SELECT times_seen FROM issues WHERE id=?", (iid,)), 1)
        self.assertEqual(self.view("v_recurring_issues"), [])

    def test_v_issue_graph(self):
        a = self.issue(self.r1, code="ISS-0001")
        b = self.issue(self.r1, code="ISS-0002")
        c = self.issue(self.r1, code="ISS-0003")
        with self.subTest("negative: no relations"):
            self.assertEqual(self.view("v_issue_graph"), [])
        self.L.relation(a, b, "duplicate-of", "Same bug.", confidence="certain", established_in_review_id=self.r2)
        self.L.relation(c, a, "related-to", "Nearby.")
        # characterization: ISS-0055 — the view's comment says "both directions", but each relation appears once,
        # in its stored direction.
        self.assertEqual(self.view("v_issue_graph", "ORDER BY from_issue"), [
            {"from_issue": "ISS-0001", "relation": "duplicate-of", "to_issue": "ISS-0002", "confidence": "certain",
             "rationale": "Same bug.", "established_in": "R-0002"},
            {"from_issue": "ISS-0003", "relation": "related-to", "to_issue": "ISS-0001", "confidence": "likely",
             "rationale": "Nearby.", "established_in": None},
        ])

    def test_v_metric_trends(self):
        with self.subTest("negative: no metrics"):
            self.assertEqual(self.view("v_metric_trends"), [])
        # R-0003 starts before R-0001: the series is ordered by started_at, not by code.
        r0 = self.review("R-0003", started_at="2026-10-01T00:00:00.000Z")
        repo = self.L.repository("engine", "engine")
        self.L.metric(self.r1, "mutation_score", 60.0, unit="%")
        self.L.metric(r0, "mutation_score", 40.0, unit="%")
        self.L.metric(self.r2, "mutation_score", 70.0, unit="%", repository_id=repo)
        self.L.metric(self.r1, "coverage", 80.0, method="counted")
        got = [(r["name"], r["scope"], r["repository"], r["review"], r["value"], r["method"])
               for r in self.view("v_metric_trends")]
        self.assertEqual(got, [
            ("coverage", "all", "all", "R-0001", 80.0, "counted"),
            ("mutation_score", "all", "all", "R-0003", 40.0, "measured"),
            ("mutation_score", "all", "all", "R-0001", 60.0, "measured"),
            ("mutation_score", "all", "engine", "R-0002", 70.0, "measured"),
        ])

    def test_v_standing_decisions(self):
        i1 = self.issue(self.r1, code="ISS-0001")
        i2 = self.issue(self.r1, code="ISS-0002")
        open_d = self.L.decision("D-0001", "Open one?")
        self.L.decision("D-0002", "Revisit one?", status="revisit", answer="maybe")
        with self.subTest("negative: open and revisit decisions are excluded"):
            self.assertEqual(self.view("v_standing_decisions"), [])
        d = self.L.decision("D-0003", "Keep data out of the repo?", options=[{"value": "yes", "label": "Yes"}])
        for iid in (i1, i2):
            self.insert("decision_issues", decision_id=d, issue_id=iid)
        self.insert("decision_issues", decision_id=open_d, issue_id=i1)
        self.L.update("decisions", d, status="decided", answer="yes", rationale="Privacy.")
        rows = self.view("v_standing_decisions")
        # ISS-0159 fixed by step 60 (schema 3): deciding sets decided_at.
        self.assertRegex(rows[0].pop("decided_at") or "", r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(rows, [
            {"code": "D-0003", "question": "Keep data out of the repo?", "answer": "yes", "rationale": "Privacy.",
             "revisit_when": None, "issues": "ISS-0001, ISS-0002"},
        ])


class PlanAndQueueViews(SchemaCase):
    """Plan P-0001 phase 4 (steps 66, 85, 88, 89, 91, 93): the owner queue, the brief, coverage, the review summary
    and the plan helper views."""

    def setUp(self):
        super().setUp()
        self.r1 = self.review("R-0001")
        self.plan = self.L.plan("P-0001", reviews=[self.r1])
        self.ph = self.L.phase(self.plan, 1)

    def view(self, name, order=""):
        return self.L.all(f"SELECT * FROM {name} {order}")

    def queue(self):
        return [(r["kind"], r["ref"], r["question"], r["status"], r["blocking"])
                for r in self.view("v_owner_queue", "ORDER BY kind, ref")]

    # ---- step 66: v_owner_queue
    def test_v_owner_queue(self):
        with self.subTest("negative: nothing waiting"):
            self.assertEqual(self.view("v_owner_queue"), [])
        s1 = self.L.step(self.plan, self.ph, 1, owner_need="decision", owner_question="Which file?")
        self.L.update("steps", s1, status="waiting-owner")
        self.L.gate("GATE-0001", "decision", "Open one", "Open?")
        self.L.gate("GATE-0002", "other", "Answered one", "Answered?", status="answered", answer_text="yes",
                    answered_by="owner", answered_via="chat")
        applied = self.L.gate("GATE-0003", "other", "Applied one", "Applied?", status="answered", answer_text="ok",
                              answered_by="owner", answered_via="chat")
        self.L.update("gates", applied, status="applied", applied_note="Done.")
        self.L.gate("GATE-0004", "other", "Withdrawn one", "Withdrawn?", status="withdrawn")
        self.L.decision("D-0001", "Ungated open?", status="open")
        self.L.decision("D-0002", "Ungated revisit?", status="revisit", answer="no", revisit_when="next review")
        self.L.decision("D-0003", "Decided?", status="decided", answer="yes")
        self.assertEqual(self.queue(), [
            # step 190: a decision's question is its question; what reopens it is in context
            ("decision", "D-0001", "Ungated open?", "open", 0),
            ("decision", "D-0002", "Ungated revisit?", "revisit", 0),
            ("gate", "GATE-0001", "Open?", "open", 1),
            ("gate", "GATE-0002", "Answered?", "answered", 1),
            ("step", "P-0001 #1", "Which file?", "open", 0),
        ])

    def test_owner_queue_no_duplicates(self):
        """DB-L1: a decision on a step that has an open or answered gate is listed once, as the gate."""
        s1 = self.L.step(self.plan, self.ph, 1, owner_need="decision", owner_question="Which way?")
        s2 = self.L.step(self.plan, self.ph, 2, owner_need="decision", owner_question="Which other way?")
        self.L.update("steps", s1, status="waiting-owner")
        self.L.update("steps", s2, status="waiting-owner")
        self.L.gate("GATE-0001", "decision", "On step 1", "Which way?", step_id=s1)
        self.L.gate("GATE-0002", "decision", "On step 2", "Which other way?", step_id=s2, status="answered",
                    answer_text="that way", answered_by="owner", answered_via="viewer")
        self.L.decision("D-0001", "Which way?", step_id=s1)
        self.L.decision("D-0002", "Which other way?", step_id=s2, status="revisit", answer="this way")
        self.assertEqual([r[1] for r in self.queue()], ["GATE-0001", "GATE-0002"])
        with self.subTest("an applied gate no longer hides the decision"):
            self.x("UPDATE gates SET status='answered', answer_text='yes', answered_by='owner', "
                   "answered_via='chat' WHERE code='GATE-0001'")
            self.x("UPDATE gates SET status='applied', applied_note='done' WHERE code='GATE-0001'")
            self.assertEqual([r[1] for r in self.queue()], ["D-0001", "GATE-0002", "P-0001 #1"])

    def test_owner_queue_excludes_gated_steps(self):
        """ABS-T4: a waiting-owner step with an open gate appears once, as the gate."""
        s1 = self.L.step(self.plan, self.ph, 1, owner_need="confirm", owner_question="Confirm?")
        self.L.update("steps", s1, status="waiting-owner")
        self.L.gate("GATE-0001", "other", "Confirm step 1", "Confirm?", step_id=s1)
        self.assertEqual(self.queue(), [("gate", "GATE-0001", "Confirm?", "open", 1)])

    def test_owner_queue_step_blocking_computed(self):
        """DB-R19: a waiting step blocks only while an unfinished step depends on it."""
        s1 = self.L.step(self.plan, self.ph, 1, owner_need="review", owner_question="Wording right?")
        s2 = self.L.step(self.plan, self.ph, 2, owner_need="review", owner_question="Layout right?")
        s3 = self.L.step(self.plan, self.ph, 3, depends_on=(s1,))
        self.L.step(self.plan, self.ph, 4, depends_on=(s2,), status="dropped")
        for s in (s1, s2):
            self.L.update("steps", s, status="waiting-owner")
        self.assertEqual([(r[1], r[4]) for r in self.queue()], [("P-0001 #1", 1), ("P-0001 #2", 0)])
        self.L.update("steps", s3, status="dropped")
        self.assertEqual([(r[1], r[4]) for r in self.queue()], [("P-0001 #1", 0), ("P-0001 #2", 0)])

    def test_owner_queue_step_question_never_blank(self):
        """FIX-5: a waiting step with no owner_question shows its progress note, else its change text."""
        s1 = self.L.step(self.plan, self.ph, 1, change="Add a CI workflow.", owner_need="review")
        s2 = self.L.step(self.plan, self.ph, 2, change="Rename the column.", owner_need="review", owner_question=" ",
                         progress_note="Is the new name clear?")
        for s in (s1, s2):
            self.L.update("steps", s, status="waiting-owner")
        self.assertEqual([r[2] for r in self.queue()], ["Add a CI workflow.", "Is the new name clear?"])

    # ---- steps 85 and 93: v_known_issues_for_brief
    def test_v_known_issues_for_brief(self):
        """TEST-T182: duplicate and obsolete issues are excluded; guard_tests lists only still_exists=1."""
        keep = self.issue(self.r1, code="ISS-0001", domain_code="security", status="fixed")
        self.issue(self.r1, code="ISS-0002", status="duplicate")
        self.issue(self.r1, code="ISS-0003", status="obsolete")
        self.issue(self.r1, code="ISS-0004", domain_code="spec")
        self.L.guard_test(keep, "tests/test_a.py", "test_kept", still_exists=1)
        self.L.guard_test(keep, "tests/test_b.py", "test_gone", still_exists=0)
        got = [(r["domain_code"], r["code"], r["guard_tests"]) for r in self.view("v_known_issues_for_brief")]
        self.assertEqual(got, [("security", "ISS-0001", "tests/test_a.py::test_kept"), ("spec", "ISS-0004", None)])

    def test_known_issues_view_has_locations_and_decisions(self):
        """WALK-M9 / DB-M11 (test_brief_view_has_locations): an issue with two primary locations and standing
        decisions returns both; an 'also' location and an open decision are left out."""
        iid = self.issue(self.r1, code="ISS-0001")
        f1 = self.L.finding(self.r1, "SPEC-M1", issue_id=iid)
        f2 = self.L.finding(self.r1, "SPEC-M2", issue_id=iid)
        self.L.location(f1, "src/a.py", 1, 2, symbol="make_code")
        self.L.location(f2, "src/b.py", 3, 4)
        self.L.location(f2, "src/c.py", 5, 6, role="also")
        for code, status, answer in (("D-0001", "decided", "Keep it."), ("D-0002", "open", None),
                                     ("D-0003", "revisit", "Later.")):
            d = self.L.decision(code, f"Question {code}?", status=status, answer=answer)
            self.insert("decision_issues", decision_id=d, issue_id=iid)
        r = self.L.one("SELECT locations, decisions FROM v_known_issues_for_brief WHERE code='ISS-0001'")
        self.assertEqual(sorted(r["locations"].split(",")), ["src/a.py#make_code", "src/b.py"])
        self.assertEqual(r["decisions"], "D-0001 (decided): Question D-0001? -> Keep it.; "
                                         "D-0003 (revisit): Question D-0003? -> Later.")
        with self.subTest("an issue with neither"):
            self.issue(self.r1, code="ISS-0002")
            self.assertEqual(self.L.one("SELECT locations, decisions FROM v_known_issues_for_brief "
                                        "WHERE code='ISS-0002'"), {"locations": None, "decisions": None})

    # ---- step 93: false findings must be matched too
    def test_unmatched_includes_false(self):
        self.L.finding(self.r1, "SPEC-M1", verification="false")
        iid = self.issue(self.r1, code="ISS-0001", status="obsolete")
        self.L.finding(self.r1, "SPEC-M2", verification="false", issue_id=iid)
        self.assertEqual([r["point_id"] for r in self.view("v_unmatched_findings")], ["SPEC-M1"])

    # ---- steps 88 and 89: v_uncovered_findings
    def test_v_uncovered_findings(self):
        """TEST-T189: a finding of the plan's review with no step is a row; one closed only by another plan's step is
        still a row; a false finding is too (step 58, schema 7: false findings are closed by a step); one closed by
        this plan's step is not."""
        bare = self.L.finding(self.r1, "SPEC-M1")
        other_plan = self.L.finding(self.r1, "SPEC-M2")
        self.L.finding(self.r1, "SPEC-M3", verification="false")
        mine = self.L.finding(self.r1, "SPEC-M4")
        p2 = self.L.plan("P-0002", reviews=[self.r1])
        self.L.step(p2, self.L.phase(p2, 1), 1, findings=(other_plan,))
        self.L.step(self.plan, self.ph, 1, findings=(mine,))
        self.assertTrue(bare)
        self.assertEqual([(r["plan"], r["review"], r["point_id"]) for r in self.view(
                             "v_uncovered_findings", "WHERE plan = 'P-0001' ORDER BY point_id")],
                         [("P-0001", "R-0001", "SPEC-M1"), ("P-0001", "R-0001", "SPEC-M2"),
                          ("P-0001", "R-0001", "SPEC-M3")])

    def test_superseding_plan_coverage(self):
        """WALK-M14: a plan that supersedes an earlier one over the same review has no uncovered findings for work
        done in the old plan; findings of the old plan's unfinished steps still need a step of the new plan."""
        done_f = self.L.finding(self.r1, "SPEC-M1")
        open_f = self.L.finding(self.r1, "SPEC-M2")
        s1 = self.L.step(self.plan, self.ph, 1, findings=(done_f,))
        self.L.step(self.plan, self.ph, 2, findings=(open_f,))
        self.L.update("steps", s1, status="done")
        p2 = self.L.plan("P-0002", reviews=[self.r1])
        self.L.update("plans", self.plan, superseded_by=p2)
        uncovered = lambda: [(r["plan"], r["point_id"]) for r in self.view("v_uncovered_findings")]
        self.assertEqual(uncovered(), [("P-0002", "SPEC-M2")])
        self.L.step(p2, self.L.phase(p2, 1), 1, findings=(open_f,))
        self.assertEqual(uncovered(), [])
        with self.subTest("only a done step of the superseded plan counts"):
            self.L.update("steps", s1, status="doing")
            self.assertEqual(uncovered(), [("P-0002", "SPEC-M1")])

    def test_uncovered_findings_same_rows(self):
        """PERF-H1: on ledgers without superseded plans the rewritten view returns the old definition's rows."""
        old = ("SELECT p.code AS plan, r.code AS review, f.point_id, f.severity, f.summary FROM plans p "
               "JOIN plan_reviews pr ON pr.plan_id = p.id JOIN reviews r ON r.id = pr.review_id "
               "JOIN findings f ON f.review_id = r.id WHERE 1 "     # step 58: false findings too
               "AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps s ON s.id = sf.step_id "
               "WHERE sf.finding_id = f.id AND s.plan_id = p.id)")
        new = "SELECT * FROM v_uncovered_findings"

        def same():
            self.assertEqual(self.L.all(f"{old} EXCEPT {new}"), [])
            self.assertEqual(self.L.all(f"{new} EXCEPT {old}"), [])

        same()
        fs = [self.L.finding(self.r1, f"SPEC-M{n}") for n in range(1, 7)]
        self.L.finding(self.r1, "SPEC-M9", verification="false")
        self.L.step(self.plan, self.ph, 1, findings=fs[:2])
        p2 = self.L.plan("P-0002", reviews=[self.r1])
        self.L.step(p2, self.L.phase(p2, 1), 1, findings=fs[2:4])
        same()
        self.assertEqual(len(self.view("v_uncovered_findings")), 5 + 5)  # each plan: the 4 the other closes or none
                                                                          # do, and the false finding

    # ---- step 88: plan helper views
    def test_v_step_issues(self):
        iid = self.issue(self.r1, code="ISS-0001", title="Weak codes")
        f1 = self.L.finding(self.r1, "SEC-H1", issue_id=iid)
        f2 = self.L.finding(self.r1, "SEC-H2")
        self.L.step(self.plan, self.ph, 1, title="Use secrets", findings=(f1, f2))
        self.L.step(self.plan, self.ph, 2)
        got = [(r["plan"], r["step"], r["step_status"], r["point_id"], r["issue"], r["issue_status"])
               for r in self.view("v_step_issues")]
        self.assertEqual(got, [("P-0001", 1, "not-started", "SEC-H1", "ISS-0001", "open"),
                               ("P-0001", 1, "not-started", "SEC-H2", None, None)])

    def test_stale_step_listed_after_fixed_check(self):
        """WALK-L21: a review's check of 'fixed' closes the issue (tr_issue_checked); the step still to do that
        closes it is then listed in v_stale_steps, until it is marked done."""
        iid = self.issue(self.r1, code="ISS-0001")
        f = self.L.finding(self.r1, "SPEC-M1", issue_id=iid)
        s = self.L.step(self.plan, self.ph, 1, findings=(f,))
        self.L.step(self.plan, self.ph, 2)                     # closes nothing: never stale
        self.assertEqual(self.view("v_stale_steps"), [])
        r2 = self.review("R-0002")
        self.L.check_row(r2, iid, "fixed")
        self.assertEqual(self.L.scalar("SELECT status FROM issues WHERE id=?", (iid,)), "fixed")
        self.assertEqual(self.view("v_stale_steps"), [{"plan": "P-0001", "step": 1, "title": "Step 1",
                                                       "status": "not-started", "issues": "ISS-0001 fixed"}])
        self.L.update("steps", s, status="done")
        self.assertEqual(self.view("v_stale_steps"), [])

    def test_v_last_reviewed_commit(self):
        """WALK-R14: per repository, the commit of the latest handed-off review (not a later running one)."""
        repo = self.L.default_repository()
        other = self.L.repository("engine", "engine")
        sha = lambda c: c * 40
        self.assertEqual(self.view("v_last_reviewed_commit"), [])
        for code, status, done, c, rp in (("R-0002", "handed-off", "2026-10-01T00:00:00.000Z", "a", repo),
                                          ("R-0003", "handed-off", "2026-10-05T00:00:00.000Z", "b", repo),
                                          ("R-0004", "running", None, "c", repo),
                                          ("R-0005", "handed-off", "2026-10-02T00:00:00.000Z", "d", other)):
            rid = self.review(code, status=status, started_at="2026-09-01T00:00:00.000Z", completed_at=done)
            self.L.review_repository(rid, sha(c), repository_id=rp)
        self.assertEqual([(r["repository"], r["review"], r["commit_sha"]) for r in self.view("v_last_reviewed_commit")],
                         [("demo", "R-0003", sha("b")), ("engine", "R-0005", sha("d"))])

    # ---- step 91: v_review_summary
    def test_v_review_summary(self):
        """TEST-T186: each sum column per review and domain; false findings counted apart (step 58, schema 7: the
        other counts are of real findings); came_back from the issue's events, so a recurrence on an issue that is
        still open does not count (v_match_problems lists that match)."""
        r2 = self.review("R-0002", started_at="2026-12-01T00:00:00.000Z")
        old = self.issue(self.r1)
        rows = (("H", "spec", "confirmed", None), ("M", "spec", "false", None), ("L", "spec", "confirmed", None),
                ("T", "security", "confirmed", None))
        for n, (sev, dom, ver, _) in enumerate(rows, 1):
            self.L.finding(self.r1, f"P-{n}", severity=sev, domain_code=dom, verification=ver,
                           kind="test" if sev == "T" else "defect", issue_id=self.issue(self.r1))
        self.L.finding(r2, "Q-1", severity="M", issue_id=old, match_type="still-present")
        self.L.finding(r2, "Q-2", severity="H", issue_id=old, match_type="recurrence")
        got = [(r["review"], r["domain_code"], r["points"], r["high"], r["medium"], r["low"], r["tests"],
                r["confirmed"], r["false_findings"], r["new_issues"], r["carried_over"], r["came_back"])
               for r in self.view("v_review_summary")]
        self.assertEqual(got, [("R-0001", "security", 1, 0, 0, 0, 1, 1, 0, 1, 0, 0),
                               ("R-0001", "spec", 2, 1, 0, 1, 0, 2, 1, 2, 0, 0),
                               ("R-0002", "spec", 2, 1, 1, 0, 0, 0, 0, 0, 1, 0)])
        self.assertEqual([r["domain_code"] for r in self.L.all("SELECT * FROM v_review_summary WHERE review=?",
                                                               ("R-0002",))], ["spec"])


if __name__ == "__main__":
    unittest.main()
