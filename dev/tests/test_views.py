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
        self.L.update("issues", back, status="fixed")
        self.L.update("issues", back, status="regressed")
        self.assertEqual(self.view("v_recurring_issues"), [
            {"code": "ISS-0003", "title": "Back", "severity": "M", "status": "regressed", "times_seen": 1,
             "regressions": 2, "reviews": None},
            {"code": "ISS-0002", "title": "Twice", "severity": "M", "status": "open", "times_seen": 2,
             "regressions": 0, "reviews": "R-0001, R-0002"},
        ])

    def test_current_v_recurring_issues_counts_false_findings(self):
        iid = self.issue(self.r1, code="ISS-0001")
        self.L.finding(self.r1, issue_id=iid)
        self.L.finding(self.r2, issue_id=iid, verification="false")
        # characterization: ISS-0007 — a finding verified false still counts, so one false re-raise makes the
        # issue recurring. The fix excludes false findings.
        self.assertEqual([(r["code"], r["times_seen"], r["reviews"]) for r in self.view("v_recurring_issues")],
                         [("ISS-0001", 2, "R-0001, R-0002")])

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
        self.assertEqual(self.view("v_standing_decisions"), [
            # characterization: ISS-0159 — deciding sets no decided_at, so the view shows NULL.
            {"code": "D-0003", "question": "Keep data out of the repo?", "answer": "yes", "rationale": "Privacy.",
             "revisit_when": None, "decided_at": None, "issues": "ISS-0001, ISS-0002"},
        ])


if __name__ == "__main__":
    unittest.main()
