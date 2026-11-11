"""Plan P-0001 steps 61-64 (schema 3): one hash rule, code formats and sequence, NOT NULL / uniqueness / range /
shape constraints, answerable gates, cross-table consistency.
"""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.test_p4_integrity_plan import IntegrityCase

GOOD = "0123456789abcdef0123456789abcdef01234567"
BAD_HASHES = (("39 chars", GOOD[:-1]), ("40 non-hex", "g" * 40), ("uppercase", GOOD.upper()), ("short", "abc1234"))


class Hashes(IntegrityCase):
    """Step 61: every commit column takes a full lowercase hex hash (40 or 64)."""

    def test_review_commit_must_be_hex(self):
        for label, sha in BAD_HASHES:
            with self.subTest(label):
                self.raises("INSERT INTO review_repositories(review_id, repository_id, commit_sha) VALUES (?,?,?)",
                            (self.r1, self.L.default_repository(), sha), "CHECK constraint failed")
        for sha in (GOOD, "a" * 64):
            with self.subTest(sha=sha):
                self.in_savepoint(lambda: self.L.review_repository(self.r1, sha))

    def test_commit_columns_require_full_hash(self):
        p = self.L.plan()
        sid = self.L.step(p, self.L.phase(p, 1), 1)
        iid = self.issue(self.r1)
        fid = self.L.finding(self.r1)
        self.L.review_repository(self.r1, GOOD)
        cases = {
            "review_repositories": lambda sha: self.L.review_repository(self.r2, sha),
            "step_commits": lambda sha: self.L.step_commit(sid, sha),
            "finding_locations": lambda sha: self.L.location(fid, commit_sha=sha),
            "issues.change_commit_sha": lambda sha: self.L.update("issues", iid, change_commit_sha=sha),
            "steps.change_commit_sha": lambda sha: self.L.update("steps", sid, change_commit_sha=sha),
            "guard_tests": lambda sha: self.L.guard_test(iid, added_commit_sha=sha),
            "test_runs": lambda sha: self.insert("test_runs", command="make test", purpose="ci", commit_sha=sha),
            "events": lambda sha: self.insert("events", entity_type="issue", entity_id=iid, event="note",
                                              commit_sha=sha),
        }
        for name, write in cases.items():
            for label, sha in BAD_HASHES:
                with self.subTest(column=name, bad=label):
                    def go():
                        with self.assertRaises(sqlite3.IntegrityError) as cm:
                            write(sha)
                        self.assertIn("CHECK constraint failed", str(cm.exception))
                    self.in_savepoint(go)
            with self.subTest(column=name, good=True):
                self.in_savepoint(lambda: write(GOOD))


class Codes(IntegrityCase):
    """Step 61: codes are canonical, sequential, never changed and never reused."""

    TABLES = (("issues", "ISS-", 4), ("reviews", "R-", 4), ("plans", "P-", 4), ("decisions", "D-", 4),
              ("gates", "GATE-", 4), ("requirements", "REQ-", 4), ("design_goals", "G-", 2))

    def make(self, table, code):
        if table == "issues":
            return self.L.issue(code, first_seen_review_id=self.r1)
        if table == "reviews":
            return self.L.review(code)
        if table == "plans":
            return self.L.plan(code)
        if table == "decisions":
            return self.L.decision(code)
        if table == "gates":
            return self.L.gate(code)
        if table == "requirements":
            return self.insert("requirements", code=code, text="A rule.", source="owner")
        return self.L.design_goal(code)

    def test_issue_codes_sequential_and_unique(self):
        for table, prefix, width in self.TABLES:
            n = (self.scalar(f"SELECT max(CAST(substr(code, {len(prefix) + 1}) AS INTEGER)) FROM {table}") or 0)
            nxt = lambda k: f"{prefix}{k:0{width}d}"
            with self.subTest(table=table, case="next number accepted"):
                rid = self.make(table, nxt(n + 1))
            for label, code in (("gap", nxt(n + 3)), ("reused", nxt(n + 1)), ("bad format", prefix + "x1"),
                                ("not padded", f"{prefix}{n + 2}" if width > 1 else prefix + "1x"),
                                ("zero", nxt(0))):
                with self.subTest(table=table, case=label):
                    def go():
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.make(table, code)
                    self.in_savepoint(go)
            with self.subTest(table=table, case="code never changes"):
                self.raises(f"UPDATE {table} SET code=? WHERE id=?", (nxt(n + 2), rid), "never change")

    def test_code_check_constraints(self):
        self.raises("INSERT INTO gates(code, kind, title, question) VALUES ('GATE 1','other','t','q')", (),
                    "")
        self.assertEqual(self.scalar("SELECT count(*) FROM gates WHERE code='GATE 1'"), 0)

    def test_migration_refuses_bad_codes(self):
        """A schema-2 ledger holding a nonconforming code is reported by migrate and left unchanged."""
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE gates SET code='GATE 7' WHERE code='GATE-0002'")
        con.commit()
        con.close()
        before = db.read_bytes()
        p = fixtures.run_ledger("--root", project, "migrate")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing was changed", p.stdout + p.stderr)
        self.assertIn("CHECK constraint failed", p.stdout + p.stderr)
        self.assertEqual(db.read_bytes(), before)


class Constraints(IntegrityCase):
    """Step 62."""

    def test_constraints_refuse_contradictions(self):
        rid = self.r1
        with self.subTest("issue without domain"):
            self.raises("INSERT INTO issues(code, title, kind, severity, first_seen_review_id) "
                        "VALUES ('ISS-0001','t','defect','M',?)", (rid,), "NOT NULL")
        with self.subTest("issue without kind"):
            self.raises("INSERT INTO issues(code, title, domain_code, severity, first_seen_review_id) "
                        "VALUES ('ISS-0001','t','spec','M',?)", (rid,), "NOT NULL")
        with self.subTest("times_seen 0"):
            self.raises("INSERT INTO issues(code, title, domain_code, kind, severity, first_seen_review_id, times_seen)"
                        " VALUES ('ISS-0001','t','spec','defect','M',?,0)", (rid,), "CHECK")
        with self.subTest("match_type without issue"):
            self.raises("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, sub, summary, "
                        "match_type) VALUES (?,'X-1','M','defect','spec','s','t','new')", (rid,), "CHECK")
        with self.subTest("negative run count"):
            self.L.review_domain(rid, "spec")
            self.raises("INSERT INTO reviewer_runs(review_id, domain_code, actor_id, label, scope, high_count) "
                        "VALUES (?,'spec',?,'l','s',-1)", (rid, self.actor("reviewer")), "CHECK")
        with self.subTest("answered gate without an answer"):
            gid = self.L.gate()
            self.raises("UPDATE gates SET status='answered' WHERE id=?", (gid,), "CHECK")

    def test_metrics_unique_with_null_repository(self):
        self.L.metric(self.r1, "mutation_score", 50.0)
        self.raises("INSERT INTO metrics(review_id, name, value, method) VALUES (?, 'mutation_score', 60, 'measured')",
                    (self.r1,), "UNIQUE")

    def test_constraints_findings(self):
        fid = self.L.finding(self.r1, point_id="SEC-H1")
        self.raises("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, sub, summary) "
                    "VALUES (?,'SEC-H1','M','defect','spec','s','t')", (self.r1,), "UNIQUE")
        base = dict(review_id=self.r1, point_id="Z-1", severity="M", kind="defect", domain_code="spec", sub="s",
                    summary="t")
        for col, val in (("severity", "X"), ("kind", "nope"), ("related_point_ids", "not json"),
                         ("verification", "maybe"), ("match_confidence", "sure")):
            with self.subTest(col=col):
                def go():
                    with self.assertRaises(sqlite3.IntegrityError) as cm:
                        self.insert("findings", **{**base, col: val})
                    self.assertIn("CHECK", str(cm.exception))
                self.in_savepoint(go)
        self.raises("UPDATE findings SET match_type='new' WHERE id=?", (fid,), "CHECK")

    def test_constraints_finding_locations(self):
        fid = self.L.finding(self.r1)
        self.L.review_repository(self.r1, GOOD)
        for label, cols in (("uppercase sha", {"commit_sha": GOOD.upper()}), ("short sha", {"commit_sha": "abc1234"}),
                            ("line_start 0", {"line_start": 0}), ("line_end alone", {"line_start": None, "line_end": 4}),
                            ("end before start", {"line_start": 5, "line_end": 2})):
            with self.subTest(label):
                def go():
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert("finding_locations", finding_id=fid, repository_id=self.L.default_repository(),
                                    path="src/a.py", **{"line_start": 1, **cols})
                self.in_savepoint(go)
        with self.subTest("one primary location per finding"):
            self.L.location(fid, "src/a.py")
            with self.assertRaises(sqlite3.IntegrityError):
                self.L.location(fid, "src/b.py", role="primary")
            self.L.location(fid, "src/c.py", role="also")

    def test_reviewer_run_counts_non_negative(self):
        for col in ("items_reviewed", "high_count", "medium_count", "low_count", "test_count"):
            with self.subTest(col=col):
                def go():
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.L.reviewer_run(self.r1, "spec", **{col: -1})
                self.in_savepoint(go)
        self.L.reviewer_run(self.r1, "spec", high_count=0)

    def test_times_seen_at_least_one(self):
        iid = self.issue(self.r1)
        self.raises("UPDATE issues SET times_seen=0 WHERE id=?", (iid,), "CHECK")


class Gates(IntegrityCase):
    """Step 63 (and step 80's option-shape rule)."""

    def test_constraints_gates(self):
        good = [{"value": "a", "label": "A", "description": "The first."}, {"value": "b", "label": "B"}]
        self.L.gate(options=good, allow_text=0)
        self.L.gate(kind="confirm")   # step 93: a plain confirmation gate
        for label, options, allow_text in (("bad json", "[", 1), ("object", '{"a":1}', 1),
                                           ("strings", '["a","b"]', 1), ("no label", '[{"value":"a"}]', 1),
                                           ("number value", '[{"value":1,"label":"x"}]', 1),
                                           ("empty, no text", "[]", 0), ("none, no text", None, 0)):
            with self.subTest(label):
                def go():
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.L.gate(options=options, allow_text=allow_text)
                self.in_savepoint(go)
        for col, val in (("kind", "x"), ("blocking", 2), ("status", "x"), ("answered_via", "x")):
            with self.subTest(col=col):
                def go():
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.L.gate(**{col: val})
                self.in_savepoint(go)
        with self.subTest("options updated to a bad shape"):
            gid = self.L.gate(options=good)
            self.raises("UPDATE gates SET options='[1]' WHERE id=?", (gid,), "gate options")

    def test_gate_must_be_answerable(self):
        self.raises("INSERT INTO gates(code, kind, title, question, allow_text) VALUES ('GATE-0001','other','t','q',0)",
                    (), "CHECK")

    def test_gate_options_must_be_array(self):
        self.raises("INSERT INTO gates(code, kind, title, question, options) VALUES ('GATE-0001','other','t','q',?)",
                    ('{"value":"a","label":"A"}',), "CHECK")

    def test_gate_options_shape_checked(self):
        self.raises("INSERT INTO gates(code, kind, title, question, options) VALUES ('GATE-0001','other','t','q',?)",
                    ('[{"value":"a"}]',), "gate options")


class CrossTable(IntegrityCase):
    """Step 64."""

    def test_actor_parent_cycle_refused(self):
        a = self.actor("manager")
        b = self.L.actor("reviewer", parent_actor_id=a)
        c = self.L.actor("verifier", parent_actor_id=b)
        self.raises("UPDATE actors SET parent_actor_id=? WHERE id=?", (c, a), "cycle")
        self.raises("UPDATE actors SET parent_actor_id=? WHERE id=?", (a, a), "cycle")

    def test_manager_column_requires_manager(self):
        rv = self.actor("reviewer")
        self.raises("INSERT INTO reviews(code, title, scope, manager_actor_id) VALUES ('R-0003','t','full',?)",
                    (rv,), "kind manager")
        self.raises("UPDATE reviews SET manager_actor_id=? WHERE id=?", (rv, self.r1), "kind manager")
        self.L.update("reviews", self.r1, manager_actor_id=self.actor("manager"))

    def test_repository_path_unique(self):
        self.L.repository("one", "a")
        self.raises("INSERT INTO repositories(name, path) VALUES ('two','a')", (), "UNIQUE")

    def test_retired_domain_refused(self):
        self.x("UPDATE domains SET retired_at=? WHERE code='cost'", ("2026-01-01T00:00:00.000Z",))
        self.raises("INSERT INTO issues(code, title, domain_code, kind, severity, first_seen_review_id) "
                    "VALUES ('ISS-0001','t','cost','defect','M',?)", (self.r1,), "retired")
        self.raises("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, sub, summary) "
                    "VALUES (?,'C-1','M','defect','cost','s','t')", (self.r1,), "retired")
        self.raises("INSERT INTO review_domains(review_id, domain_code, decision, reason) "
                    "VALUES (?,'cost','included','x')", (self.r1,), "retired")
        self.raises("INSERT INTO metrics(review_id, domain_code, name, value, method) "
                    "VALUES (?,'cost','m',1,'counted')", (self.r1,), "retired")

    def test_decided_needs_answer(self):
        self.raises("INSERT INTO decisions(code, question, status) VALUES ('D-0001','q','decided')", (), "CHECK")
        did = self.L.decision()
        self.raises("UPDATE decisions SET status='decided' WHERE id=?", (did,), "CHECK")

    def test_design_goal_supersede_consistent(self):
        g1 = self.L.design_goal()
        g2 = self.L.design_goal()
        self.raises("UPDATE design_goals SET superseded_by=? WHERE id=?", (g2, g1), "CHECK")
        self.x("UPDATE design_goals SET superseded_by=?, retired_at=? WHERE id=?", (g2, "2026-01-01T00:00:00.000Z", g1))

    def test_review_approved_needs_time(self):
        self.raises("INSERT INTO reviews(code, title, scope, status) VALUES ('R-0003','t','full','approved')", (),
                    "CHECK")

    def test_started_before_completed(self):
        self.raises("INSERT INTO reviews(code, title, scope, started_at, completed_at) "
                    "VALUES ('R-0003','t','full','2026-02-01T00:00:00.000Z','2026-01-01T00:00:00.000Z')", (), "CHECK")

    def test_requirement_supersede_consistent(self):
        q1 = self.insert("requirements", code="REQ-0001", text="a", source="owner")
        q2 = self.insert("requirements", code="REQ-0002", text="b", source="owner")
        self.raises("UPDATE requirements SET status='superseded' WHERE id=?", (q1,), "CHECK")
        self.raises("UPDATE requirements SET superseded_by=? WHERE id=?", (q2, q1), "CHECK")
        self.x("UPDATE requirements SET status='superseded', superseded_by=? WHERE id=?", (q2, q1))

    def test_step_finding_from_plan_review_only(self):
        p = self.L.plan(reviews=(self.r1,))
        sid = self.L.step(p, self.L.phase(p, 1), 1)
        mine, other = self.L.finding(self.r1), self.L.finding(self.r2)
        self.x("INSERT INTO step_findings(step_id, finding_id) VALUES (?,?)", (sid, mine))
        self.raises("INSERT INTO step_findings(step_id, finding_id) VALUES (?,?)", (sid, other), "plan_reviews")

    def test_location_without_review_repo_flagged(self):
        """Was: no review_repositories row left commit_sha NULL silently (ISS-0162). Now refused."""
        fid = self.L.finding(self.r1)
        self.raises("INSERT INTO finding_locations(finding_id, repository_id, path) VALUES (?,?,'src/a.py')",
                    (fid, self.L.default_repository()), "review_repositories")
        self.L.review_repository(self.r1, GOOD)
        lid = self.insert("finding_locations", finding_id=fid, repository_id=self.L.default_repository(),
                          path="src/a.py", line_start=1)
        self.assertEqual(self.scalar("SELECT commit_sha FROM finding_locations WHERE id=?", (lid,)), GOOD)

    def test_finding_run_from_same_review(self):
        run = self.L.reviewer_run(self.r2, "spec")
        self.raises("INSERT INTO findings(review_id, reviewer_run_id, point_id, severity, kind, domain_code, sub, "
                    "summary) VALUES (?,?,'X-1','M','defect','spec','s','t')", (self.r1, run), "another review")


if __name__ == "__main__":
    unittest.main()
