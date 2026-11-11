"""Characterization tests for the schema's CHECK, UNIQUE, NOT NULL, primary-key and foreign-key constraints.

One test per table, table-driven with subTest: every good row inserts and every bad row raises
sqlite3.IntegrityError whose message names the constraint. They pin the schema as it is today (version 1). Where
today's behaviour is a known defect the assertion carries a `# characterization:` comment naming the issue, so the
step that fixes it flips that assertion on purpose.
"""
import sqlite3
import unittest

from dev.tests._schema_helpers import SchemaCase

SHA40 = "a" * 40
SHA64 = "b" * 64


def ck(column):
    """Start of SQLite's message for a failed column CHECK, e.g. 'CHECK constraint failed: kind IN'."""
    return f"CHECK constraint failed: {column}"


def uq(*cols):
    return "UNIQUE constraint failed: " + ", ".join(cols)


def nn(col):
    return f"NOT NULL constraint failed: {col}"


FK = "FOREIGN KEY constraint failed"

ACTOR_KINDS = ("owner", "human", "manager", "reviewer", "verifier", "indexer", "matcher", "drafter", "fixer", "tool")
CATEGORIES = ("correctness", "architecture", "user-facing", "security-operations", "documentation", "custom")
REVIEW_SCOPES = ("full", "partial", "targeted", "second", "fix-verification")
REVIEW_STATUSES = ("planning", "awaiting-approval", "approved", "running", "verifying", "indexing", "consolidating",
                   "handed-off", "abandoned")
ISSUE_KINDS = ("defect", "test", "validation", "refactor", "feature", "design", "docs", "logging", "perf", "security",
               "ops", "verify")
ISSUE_STATUSES = ("open", "needs-decision", "planned", "in-progress", "fixed", "verified", "regressed",
                  "accepted-risk", "wont-fix", "duplicate", "obsolete")
RELATIONS = ("duplicate-of", "continuation-of", "regression-of", "related-to", "caused-by", "blocks", "supersedes",
             "split-from", "merged-into")
CONFIDENCES = ("certain", "likely", "possible")


def each(column, values):
    return [(f"{column}={v}", {column: v}) for v in values]


# ---------------------------------------------------------------- step 9: review and issue tables

class ReviewAndIssueTables(SchemaCase):
    def test_constraints_repositories(self):
        self.run_cases(
            "repositories", lambda n: {"name": f"repo{n}", "path": "."},
            good=[("only a name", {"path": None}),
                  ("all optional columns", {"remote_url": "https://example.invalid/r.git", "default_branch": "main",
                                            "permalink_template": "https://x/{sha}/{path}", "notes": "n"})],
            bad=[("no name", {"name": None}, nn("repositories.name"))],
            dup=[("same name", {"path": "other"}, uq("repositories.name"))])

    def test_constraints_actors(self):
        self.run_cases(
            "actors", lambda n: {"kind": "manager", "name": f"actor {n}"},
            good=each("kind", ACTOR_KINDS) + [("with a parent", lambda t: {"parent_actor_id": t.actor()})],
            bad=[("kind robot", {"kind": "robot"}, ck("kind")),
                 ("no kind", {"kind": None}, nn("actors.kind")),
                 ("no name", {"name": None}, nn("actors.name")),
                 ("unknown parent", {"parent_actor_id": 9999}, FK)])

    def test_constraints_domains(self):
        self.run_cases(
            "domains", lambda n: {"code": f"dom-{n}", "name": f"Domain {n}", "category": "custom"},
            good=each("category", CATEGORIES),
            bad=[("category misc", {"category": "misc"}, ck("category")),
                 ("no name", {"name": None}, nn("domains.name"))],
            dup=[("same code", {"name": "Other"}, uq("domains.code"))])

    def test_constraints_design_goals(self):
        self.run_cases(
            "design_goals", lambda n: {"code": f"G-{n:03d}", "text": "Every rule is a setting."},
            good=[("superseded by a goal", lambda t: {"superseded_by": t.L.design_goal()})],
            bad=[("no text", {"text": None}, nn("design_goals.text")),
                 ("unknown successor", {"superseded_by": 9999}, FK)],
            dup=[("same code", {"text": "Other."}, uq("design_goals.code"))])

    def test_constraints_reviews(self):
        self.run_cases(
            "reviews", lambda n: {"code": f"R-9{n:03d}", "title": "t", "scope": "full"},
            good=each("scope", REVIEW_SCOPES) + each("status", REVIEW_STATUSES),
            bad=[("scope whole", {"scope": "whole"}, ck("scope")),
                 ("status done", {"status": "done"}, ck("status")),
                 ("no scope", {"scope": None}, nn("reviews.scope")),
                 ("no title", {"title": None}, nn("reviews.title")),
                 ("unknown previous review", {"previous_review_id": 9999}, FK)],
            dup=[("same code", {"title": "other"}, uq("reviews.code"))])
        rid = self.review()
        self.assertEqual(self.scalar("SELECT status FROM reviews WHERE id=?", (rid,)), "planning")

    def test_constraints_review_repositories(self):
        repo = self.L.default_repository()
        self.run_cases(
            "review_repositories",
            lambda n: {"review_id": self.review(), "repository_id": repo, "commit_sha": SHA40},
            good=[("64-char sha", {"commit_sha": SHA64}),
                  ("tag pushed and dirty", {"tag_pushed": 1, "is_dirty": 1, "dirty_note": "x"}),
                  # characterization: ISS-0153 — only the length is checked, so 40 non-hex characters (and
                  # upper-case hex) are accepted; the fix makes these bad rows.
                  ("40 non-hex chars accepted", {"commit_sha": "z" * 40}),
                  ("upper-case hex accepted", {"commit_sha": "A" * 40})],
            bad=[("39 chars", {"commit_sha": "a" * 39}, ck("length(commit_sha)")),
                 ("41 chars", {"commit_sha": "a" * 41}, ck("length(commit_sha)")),
                 ("no sha", {"commit_sha": None}, nn("review_repositories.commit_sha")),
                 ("tag_pushed 2", {"tag_pushed": 2}, ck("tag_pushed")),
                 ("is_dirty 2", {"is_dirty": 2}, ck("is_dirty")),
                 ("unknown review", {"review_id": 9999}, FK),
                 ("unknown repository", {"repository_id": 9999}, FK)],
            dup=[("same review and repository", {"commit_sha": SHA64},
                  uq("review_repositories.review_id", "review_repositories.repository_id"))])

    def test_constraints_review_goals(self):
        self.run_cases(
            "review_goals", lambda n: {"review_id": self.review(), "goal_id": self.L.design_goal()},
            bad=[("unknown goal", {"goal_id": 9999}, FK)],
            dup=[("same pair", {}, uq("review_goals.review_id", "review_goals.goal_id"))])

    def test_constraints_review_domains(self):
        self.run_cases(
            "review_domains",
            lambda n: {"review_id": self.review(), "domain_code": "spec", "decision": "included", "reason": "r"},
            good=each("decision", ("included", "skipped"))
            + each("owner_change", ("none", "added-by-owner", "removed-by-owner", "modified-by-owner")),
            bad=[("decision maybe", {"decision": "maybe"}, ck("decision")),
                 ("owner_change owner", {"owner_change": "owner"}, ck("owner_change")),
                 ("no reason", {"reason": None}, nn("review_domains.reason")),
                 ("unknown domain", {"domain_code": "no-such-domain"}, FK)],
            dup=[("same review and domain", {"decision": "skipped"},
                  uq("review_domains.review_id", "review_domains.domain_code"))])

    def test_constraints_reviewer_runs(self):
        rid, actor = self.review(), self.actor("reviewer")
        self.run_cases(
            "reviewer_runs",
            lambda n: {"review_id": rid, "domain_code": "spec", "actor_id": actor, "label": f"spec-{n}",
                       "scope": "all"},
            good=each("status", ("queued", "running", "finished", "failed", "cancelled"))
            + [("same label in another review", lambda t: {"review_id": t.review(), "label": "spec-shared"})],
            bad=[("status done", {"status": "done"}, ck("status")),
                 ("no scope", {"scope": None}, nn("reviewer_runs.scope")),
                 ("no actor", {"actor_id": None}, nn("reviewer_runs.actor_id")),
                 ("unknown domain", {"domain_code": "no-such-domain"}, FK)],
            dup=[("same label in the same review", {"scope": "other"},
                  uq("reviewer_runs.review_id", "reviewer_runs.label"))])
        run = self.insert("reviewer_runs", review_id=rid, domain_code="spec", actor_id=actor, label="x", scope="all")
        self.assertEqual(self.scalar("SELECT status FROM reviewer_runs WHERE id=?", (run,)), "queued")

    def test_constraints_requirements(self):
        self.run_cases(
            "requirements", lambda n: {"code": f"REQ-{n:04d}", "text": "It must.", "source": "owner"},
            good=each("status", ("active", "superseded", "withdrawn"))
            + [("superseded by a requirement and first review",
                lambda t: {"superseded_by": t.insert("requirements", code="REQ-9999", text="t", source="s"),
                           "first_review_id": t.review()})],
            bad=[("status done", {"status": "done"}, ck("status")),
                 ("no source", {"source": None}, nn("requirements.source")),
                 ("no text", {"text": None}, nn("requirements.text")),
                 ("unknown first review", {"first_review_id": 9999}, FK)],
            dup=[("same code", {"text": "Other."}, uq("requirements.code"))])

    def test_constraints_issues(self):
        rid = self.review()
        self.run_cases(
            "issues", lambda n: {"code": f"ISS-9{n:03d}", "title": "t", "severity": "M", "first_seen_review_id": rid},
            good=each("kind", ISSUE_KINDS) + each("severity", ("H", "M", "L", "T")) + each("status", ISSUE_STATUSES)
            # characterization: ISS-0044 — an issue with no kind and no domain is accepted; the fix may refuse it.
            + [("no kind and no domain accepted", {"kind": None, "domain_code": None})],
            bad=[("severity X", {"severity": "X"}, ck("severity")),
                 ("kind bug", {"kind": "bug"}, ck("kind")),
                 ("status closed", {"status": "closed"}, ck("status")),
                 ("no severity", {"severity": None}, nn("issues.severity")),
                 ("no first review", {"first_seen_review_id": None}, nn("issues.first_seen_review_id")),
                 ("unknown domain", {"domain_code": "no-such-domain"}, FK),
                 ("unknown theme", {"theme_code": "no-such-theme"}, FK)],
            dup=[("same code", {"title": "other"}, uq("issues.code"))])

    def test_constraints_issue_relations(self):
        a, b = self.issue(), self.issue()
        self.run_cases(
            "issue_relations",
            lambda n: {"from_issue_id": a, "to_issue_id": b, "relation": "related-to", "rationale": "r"},
            good=each("relation", RELATIONS) + each("confidence", CONFIDENCES),
            bad=[("relation blocked-by", {"relation": "blocked-by"}, ck("relation")),
                 ("confidence sure", {"confidence": "sure"}, ck("confidence")),
                 ("to itself", {"to_issue_id": a}, "CHECK constraint failed: from_issue_id <> to_issue_id"),
                 ("no rationale", {"rationale": None}, nn("issue_relations.rationale")),
                 ("unknown issue", {"to_issue_id": 9999}, FK)],
            dup=[("same triple", {"rationale": "again"},
                  uq("issue_relations.from_issue_id", "issue_relations.to_issue_id", "issue_relations.relation"))])
        # The directional relations refuse their own reverse (tr_relation_noreverse_dup) ...
        for rel in ("duplicate-of", "continuation-of", "regression-of", "supersedes", "merged-into", "split-from",
                    "caused-by"):
            with self.subTest(reverse=rel):
                def go():
                    self.accepted("issue_relations", from_issue_id=a, to_issue_id=b, relation=rel, rationale="r")
                    self.refused("issue_relations", "the reverse relation already exists",
                                 from_issue_id=b, to_issue_id=a, relation=rel, rationale="r")
                self.in_savepoint(go)
        # ... but the symmetric ones do not.
        # characterization: ISS-0175 — a related-to (and blocks) pair stored in both directions is accepted.
        for rel in ("related-to", "blocks"):
            with self.subTest(reverse=rel):
                def go():
                    self.accepted("issue_relations", from_issue_id=a, to_issue_id=b, relation=rel, rationale="r")
                    self.accepted("issue_relations", from_issue_id=b, to_issue_id=a, relation=rel, rationale="r")
                self.in_savepoint(go)


# ---------------------------------------------------------------- step 10: finding, decision, plan, test, event tables

class FindingPlanAndEventTables(SchemaCase):
    def setUp(self):
        super().setUp()
        self.r1 = self.review()
        self.iss = self.issue(self.r1)
        self.f1 = self.L.finding(self.r1, issue_id=self.iss)
        self.plan = self.L.plan()
        self.ph = self.L.phase(self.plan, 1)

    def step(self, number=None, **kw):
        return self.L.step(self.plan, self.ph, number or (100 + self.n()), **kw)

    def other_plan_step(self):
        plan = self.L.plan()
        return self.L.step(plan, self.L.phase(plan, 1), 1)

    def test_constraints_finding_goals(self):
        self.run_cases(
            "finding_goals", lambda n: {"finding_id": self.f1, "goal_id": self.L.design_goal()},
            bad=[("unknown goal", {"goal_id": 9999}, FK), ("unknown finding", {"finding_id": 9999}, FK)],
            dup=[("same pair", {}, uq("finding_goals.finding_id", "finding_goals.goal_id"))])

    def test_constraints_finding_requirements(self):
        def base(n):
            req = self.insert("requirements", code=f"REQ-{n:04d}", text="t", source="s")
            return {"finding_id": self.f1, "requirement_id": req, "verdict": "matches"}
        self.run_cases(
            "finding_requirements", base,
            good=each("verdict", ("matches", "differs", "partly", "missing", "untested")),
            bad=[("verdict ok", {"verdict": "ok"}, ck("verdict")),
                 ("no verdict", {"verdict": None}, nn("finding_requirements.verdict")),
                 ("unknown requirement", {"requirement_id": 9999}, FK)],
            dup=[("same pair", {"verdict": "differs"},
                  uq("finding_requirements.finding_id", "finding_requirements.requirement_id"))])

    def test_constraints_review_issue_checks(self):
        self.run_cases(
            "review_issue_checks", lambda n: {"review_id": self.review(), "issue_id": self.iss,
                                              "outcome": "still-present"},
            good=each("outcome", ("still-present", "changed", "fixed", "regressed", "obsolete", "not-checked"))
            # characterization: ISS-0158 — the issue's own first review may record a check of it; whether that is
            # allowed is an open decision, and the fix step flips this case if it is refused.
            + [("check by the issue's own first review accepted", {"review_id": self.r1})],
            bad=[("outcome gone", {"outcome": "gone"}, ck("outcome")),
                 ("no outcome", {"outcome": None}, nn("review_issue_checks.outcome")),
                 ("unknown issue", {"issue_id": 9999}, FK),
                 ("unknown finding", {"finding_id": 9999}, FK)],
            dup=[("one check per review per issue", {"outcome": "fixed"},
                  uq("review_issue_checks.review_id", "review_issue_checks.issue_id"))])

    def test_constraints_decisions(self):
        self.run_cases(
            "decisions", lambda n: {"code": f"D-9{n:03d}", "question": "Which way?"},
            good=each("status", ("open", "decided", "revisit"))
            + [("options a JSON array", {"options": '[{"value": "a", "label": "A"}]'}),
               ("options NULL", {"options": None}),
               # characterization: ISS-0159 — a decided decision with no decided_at is accepted.
               ("decided with no decided_at accepted", {"status": "decided", "answer": "a", "decided_at": None})],
            bad=[("status closed", {"status": "closed"}, ck("status")),
                 ("options not JSON", {"options": "[1,"}, "CHECK constraint failed: options IS NULL OR json_valid"),
                 ("no question", {"question": None}, nn("decisions.question")),
                 ("unknown step", {"step_id": 9999}, FK)],
            dup=[("same code", {"question": "Other?"}, uq("decisions.code"))])
        did = self.L.decision()
        self.assertEqual(self.scalar("SELECT status FROM decisions WHERE id=?", (did,)), "open")

    def test_constraints_decision_issues(self):
        self.run_cases(
            "decision_issues", lambda n: {"decision_id": self.L.decision(), "issue_id": self.iss},
            bad=[("unknown issue", {"issue_id": 9999}, FK), ("unknown decision", {"decision_id": 9999}, FK)],
            dup=[("same pair", {}, uq("decision_issues.decision_id", "decision_issues.issue_id"))])

    def test_constraints_plans(self):
        self.run_cases(
            "plans", lambda n: {"code": f"P-9{n:03d}", "title": "Plan"},
            good=each("status", ("drafting", "awaiting-approval", "approved", "in-progress", "complete", "superseded",
                                 "abandoned"))
            + [("superseded by a plan", {"superseded_by": self.plan})],
            bad=[("status done", {"status": "done"}, ck("status")),
                 ("no title", {"title": None}, nn("plans.title")),
                 ("unknown successor", {"superseded_by": 9999}, FK)],
            dup=[("same code", {"title": "Other"}, uq("plans.code"))])

    def test_constraints_plan_reviews(self):
        self.run_cases(
            "plan_reviews", lambda n: {"plan_id": self.plan, "review_id": self.review()},
            bad=[("unknown review", {"review_id": 9999}, FK), ("unknown plan", {"plan_id": 9999}, FK)],
            dup=[("same pair", {}, uq("plan_reviews.plan_id", "plan_reviews.review_id"))])

    def test_constraints_phases(self):
        self.run_cases(
            "phases", lambda n: {"plan_id": self.plan, "number": 10 + n, "title": "Ph", "goal": "g"},
            good=[("same number in another plan", lambda t: {"plan_id": t.L.plan(), "number": 1})],
            bad=[("no goal", {"goal": None}, nn("phases.goal")),
                 ("no title", {"title": None}, nn("phases.title")),
                 ("unknown plan", {"plan_id": 9999}, FK)],
            dup=[("same plan and number", {"title": "Other"}, uq("phases.plan_id", "phases.number"))])

    def test_constraints_steps(self):
        self.run_cases(
            "steps", lambda n: {"plan_id": self.plan, "phase_id": self.ph, "number": n, "title": "Do it",
                                "change": "c", "proof": "p"},
            good=each("owner_need", ("none", "decision", "confirm", "review", "production"))
            + each("behavior_change", (0, 1))
            + each("status", ("not-started", "doing", "waiting-owner", "done", "dropped")),
            bad=[("status finished", {"status": "finished"}, ck("status")),
                 ("owner_need maybe", {"owner_need": "maybe"}, ck("owner_need")),
                 ("behavior_change 2", {"behavior_change": 2}, ck("behavior_change")),
                 ("no proof", {"proof": None}, nn("steps.proof")),
                 ("no change", {"change": None}, nn("steps.change")),
                 ("phase of another plan", lambda t: {"phase_id": t.L.phase(t.L.plan(), 1)},
                  "step phase belongs to another plan")],
            dup=[("same plan and number", {"title": "Other"}, uq("steps.plan_id", "steps.number"))])
        sid = self.step()
        self.assertEqual(self.scalar("SELECT status FROM steps WHERE id=?", (sid,)), "not-started")

    def test_constraints_step_dependencies(self):
        s1, s2 = self.step(1), self.step(2)
        self.run_cases(
            "step_dependencies", lambda n: {"step_id": s2, "depends_on_step_id": s1},
            bad=[("on a later step", {"step_id": s1, "depends_on_step_id": s2},
                  "a step may only depend on an earlier step of the same plan"),
                 # characterization: the self-dependency is refused by tr_dep_order before the table's own
                 # CHECK (step_id <> depends_on_step_id) is reached, so the CHECK's message is never seen.
                 ("on itself", {"depends_on_step_id": s2},
                  "a step may only depend on an earlier step of the same plan"),
                 ("on a step of another plan",
                  lambda t: {"depends_on_step_id": t.other_plan_step()},
                  "a step may only depend on an earlier step of the same plan")],
            dup=[("same pair", {}, uq("step_dependencies.step_id", "step_dependencies.depends_on_step_id"))])

    def test_constraints_step_findings(self):
        sid = self.step()
        self.run_cases(
            "step_findings", lambda n: {"step_id": sid, "finding_id": self.L.finding(self.r1)},
            good=[("with a multi_reason", {"multi_reason": "two halves"})],
            bad=[("unknown finding", {"finding_id": 9999}, FK), ("unknown step", {"step_id": 9999}, FK)],
            dup=[("same pair", {"multi_reason": "again"}, uq("step_findings.step_id", "step_findings.finding_id"))])

    def test_constraints_step_commits(self):
        sid = self.step()
        self.run_cases(
            "step_commits", lambda n: {"step_id": sid, "commit_sha": f"{n:040x}"},
            good=[("no repository", {"repository_id": None}),
                  # characterization: ISS-0153 — step_commits.commit_sha has no hex or length check.
                  ("non-sha text accepted", {"commit_sha": "not a sha"})],
            bad=[("no sha", {"commit_sha": None}, nn("step_commits.commit_sha")),
                 ("unknown repository", {"repository_id": 9999}, FK)],
            dup=[("same step and sha", {"summary": "again"}, uq("step_commits.step_id", "step_commits.commit_sha"))])

    def test_constraints_guard_tests(self):
        self.run_cases(
            "guard_tests", lambda n: {"issue_id": self.iss, "test_path": "t.py", "test_name": f"test_{n}"},
            good=each("failed_before_fix", (0, 1, None)) + each("passes_after_fix", (0, 1, None))
            + each("still_exists", (0, 1)),
            bad=[("failed_before_fix 2", {"failed_before_fix": 2}, ck("failed_before_fix")),
                 ("passes_after_fix 2", {"passes_after_fix": 2}, ck("passes_after_fix")),
                 ("still_exists 2", {"still_exists": 2}, ck("still_exists")),
                 ("still_exists NULL", {"still_exists": None}, nn("guard_tests.still_exists")),
                 ("no test name", {"test_name": None}, nn("guard_tests.test_name")),
                 ("unknown issue", {"issue_id": 9999}, FK)],
            dup=[("same issue, path and name", {"failed_before_fix": 1},
                  uq("guard_tests.issue_id", "guard_tests.test_path", "guard_tests.test_name"))])

    def test_constraints_test_runs(self):
        self.run_cases(
            "test_runs", lambda n: {"command": "python3 -m unittest", "purpose": "baseline"},
            good=each("purpose", ("baseline", "targeted", "flake-check", "mutation", "before-fix", "after-fix", "ci")),
            bad=[("purpose smoke", {"purpose": "smoke"}, ck("purpose")),
                 ("no command", {"command": None}, nn("test_runs.command")),
                 ("unknown review", {"review_id": 9999}, FK)])

    def test_constraints_artifacts(self):
        self.run_cases(
            "artifacts", lambda n: {"kind": "report", "path": f"reports/{n}.md"},
            good=each("kind", ("brief", "report", "index", "summary-doc", "plan-file", "progress-page",
                               "probe-script", "other"))
            + [("url only", {"path": None, "url": "https://example.invalid/doc"}),
               ("path and url", {"url": "https://example.invalid/doc"})],
            bad=[("neither path nor url", {"path": None, "url": None},
                  "CHECK constraint failed: path IS NOT NULL OR url IS NOT NULL"),
                 ("kind log", {"kind": "log"}, ck("kind")),
                 ("unknown plan", {"plan_id": 9999}, FK)])

    def test_constraints_events(self):
        self.run_cases(
            "events", lambda n: {"entity_type": "issue", "entity_id": self.iss, "event": "note"},
            good=each("entity_type", ("review", "issue", "step", "plan", "decision", "finding", "gate")),
            bad=[("entity_type plan-step", {"entity_type": "plan-step"}, ck("entity_type")),
                 ("no event", {"event": None}, nn("events.event")),
                 ("no entity id", {"entity_id": None}, nn("events.entity_id")),
                 ("unknown actor", {"actor_id": 9999}, FK)])

    def test_current_events_update_and_delete_accepted(self):
        # characterization: ISS-0014 — history is append-only by discipline only: UPDATE and DELETE on events are
        # accepted. The fix step makes both raise.
        eid = self.insert("events", entity_type="issue", entity_id=self.iss, event="note", note="before")
        self.x("UPDATE events SET note='rewritten' WHERE id=?", (eid,))
        self.assertEqual(self.scalar("SELECT note FROM events WHERE id=?", (eid,)), "rewritten")
        self.x("DELETE FROM events WHERE id=?", (eid,))
        self.assertEqual(self.scalar("SELECT count(*) FROM events WHERE id=?", (eid,)), 0)


class ForeignKeysNeedThePragma(SchemaCase):
    def test_current_dangling_reference_accepted_without_pragma(self):
        # The constraints above hold only because the connection runs PRAGMA foreign_keys=ON.
        # characterization: ISS-0157 — a connection without the pragma silently accepts a dangling reference.
        con = sqlite3.connect(str(self.L.db), isolation_level=None)
        self.addCleanup(con.close)
        con.execute("INSERT INTO actors(kind, name, parent_actor_id) VALUES ('tool', 'x', 9999)")
        self.assertEqual(con.execute("SELECT count(*) FROM actors WHERE parent_actor_id=9999").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
