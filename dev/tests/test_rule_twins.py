"""Rule triggers hold on UPDATE as on INSERT (ISS-0005, ISS-0169, ISS-0170).

Every rule trigger tr_X that checks an INSERT has an UPDATE twin tr_X_upd. test_rule_twins_agree finds every pair in
the schema and runs its case: the bad row the INSERT trigger refuses is refused the same way when an UPDATE writes
it, so a condition edited in one twin and not the other fails here. A new pair needs a case in TWINS.
"""
import sqlite3
import unittest

from dev.tests._schema_helpers import SchemaCase

RETIRED = "2026-01-01T00:00:00.000Z"
DOMAIN_RETIRED = "that domain is retired; use a current one"
THEME_RETIRED = "that theme is retired; use a current one"


class TwinCase(SchemaCase):
    def setUp(self):
        super().setUp()
        self.r1 = self.L.review(status="running")
        self.r2 = self.L.review(status="running")
        self.x("UPDATE domains SET retired_at=? WHERE code='cost'", (RETIRED,))
        self.x("INSERT INTO themes(code, name, retired_at) VALUES ('old-theme', 'Old theme', ?)", (RETIRED,))

    def message(self, fn):
        """The IntegrityError message fn raises, inside a savepoint that is rolled back; None when it raises none."""
        def go():
            try:
                fn()
            except sqlite3.IntegrityError as e:
                return str(e)
            return None
        return self.in_savepoint(go)

    def effect(self, fn, probe):
        """probe() after fn(), inside a savepoint that is rolled back."""
        def go():
            fn()
            return probe()
        return self.in_savepoint(go)

    # -- the parent rows the cases share
    def issue(self, **kw):
        return self.L.issue(first_seen_review_id=self.r1, **kw)

    def plan_step(self):
        pid = self.L.plan(reviews=(self.r1,))
        ph = self.L.phase(pid, 1)
        return pid, ph, self.L.step(pid, ph, 1)


def _step_finding(t):
    pid, ph, sid = t.plan_step()
    inside = t.L.finding(t.r1)
    outside = t.L.finding(t.r2)
    return (lambda: t.insert("step_findings", step_id=sid, finding_id=outside),  # not L.step_finding: it adds the review
            lambda: t.x("UPDATE step_findings SET finding_id=? WHERE rowid=?", (outside, t.L.step_finding(sid, inside))))


def _issue_domain(t):
    return (lambda: t.issue(domain_code="cost"),
            lambda: t.x("UPDATE issues SET domain_code='cost' WHERE id=?", (t.issue(),)))


def _issue_theme(t):
    return (lambda: t.issue(theme_code="old-theme"),
            lambda: t.x("UPDATE issues SET theme_code='old-theme' WHERE id=?", (t.issue(),)))


def _review_domain(t):
    return (lambda: t.L.review_domain(t.r1, "cost"),
            lambda: t.x("UPDATE review_domains SET domain_code='cost' WHERE rowid=?", (t.L.review_domain(t.r1, "ui"),)))


def _metric_domain(t):
    return (lambda: t.L.metric(t.r1, domain_code="cost"),
            lambda: t.x("UPDATE metrics SET domain_code='cost' WHERE id=?", (t.L.metric(t.r1, domain_code="ui"),)))


def _goal_domain(t):
    return (lambda: t.L.design_goal(domain_code="cost"),
            lambda: t.x("UPDATE design_goals SET domain_code='cost' WHERE id=?", (t.L.design_goal(domain_code="ui"),)))


def _run_domain(t):
    def update():
        t.L.review_domain(t.r1, "ui")
        t.x("UPDATE reviewer_runs SET domain_code='cost' WHERE id=?", (t.L.reviewer_run(t.r1, "ui"),))
    def insert():
        t.x("UPDATE domains SET retired_at=NULL WHERE code='cost'")
        t.L.review_domain(t.r1, "cost")
        t.x("UPDATE domains SET retired_at=? WHERE code='cost'", (RETIRED,))
        t.L.reviewer_run(t.r1, "cost")
    return insert, update


def _dep_order(t):
    pid, ph, s1 = t.plan_step()
    other = t.L.plan(reviews=(t.r1,))
    s9 = t.L.step(other, t.L.phase(other, 1), 1)
    s2 = t.L.step(pid, ph, 2)
    return (lambda: t.L.dependency(s1, s9),
            lambda: t.x("UPDATE step_dependencies SET depends_on_step_id=? WHERE rowid=?",
                        (s9, t.L.dependency(s1, s2))))


def _step_phase_plan(t):
    pid, ph, s1 = t.plan_step()
    other = t.L.plan(reviews=(t.r1,))
    ph9 = t.L.phase(other, 1)
    return (lambda: t.L.step(pid, ph9, 2),
            lambda: t.x("UPDATE steps SET phase_id=? WHERE id=?", (ph9, s1)))


BAD_OPTIONS = '[{"value": 1, "label": "One"}]'
GOOD_OPTIONS = '[{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]'


def _gate_options(t):
    return (lambda: t.L.gate(options=BAD_OPTIONS),
            lambda: t.x("UPDATE gates SET options=? WHERE id=?", (BAD_OPTIONS, t.L.gate(options=GOOD_OPTIONS))))


def _gate_fixed_options(t):
    return (lambda: t.L.gate(kind="plan-approval", options=GOOD_OPTIONS),
            lambda: t.x("UPDATE gates SET options=? WHERE id=?", (GOOD_OPTIONS, t.L.gate(kind="plan-approval"))))


def _gate_multi(t):
    return (lambda: t.L.gate(options=GOOD_OPTIONS, multi=1, preselect='["z"]'),
            lambda: t.x("UPDATE gates SET preselect='[\"z\"]' WHERE id=?",
                        (t.L.gate(options=GOOD_OPTIONS, multi=1),)))


def _goal_approver(t):
    mgr = t.L.actor("manager")
    owner = t.L.actor("owner")
    return (lambda: t.L.design_goal(status="approved", approved_by_actor_id=mgr, approved_at=RETIRED),
            lambda: t.x("UPDATE design_goals SET status='approved', approved_by_actor_id=?, approved_at=? WHERE id=?",
                        (mgr, RETIRED, t.L.design_goal())))


def _gate_answerer(t):
    mgr = t.L.actor("manager")
    return (lambda: t.L.gate(kind="other", status="answered", answer_text="yes", answered_by_actor_id=mgr),
            lambda: t.x("UPDATE gates SET status='answered', answer_text='yes', answered_by_actor_id=? WHERE id=?",
                        (mgr, t.L.gate(kind="other"))))


def _decision_decider(t):
    mgr = t.L.actor("manager")
    return (lambda: t.insert("decisions", code="D-0001", question="q", status="decided", answer="a",
                             decided_by_actor_id=mgr),
            lambda: t.x("UPDATE decisions SET status='decided', answer='a', decided_by_actor_id=? WHERE id=?",
                        (mgr, t.insert("decisions", code="D-0001", question="q", status="open"))))


def _goal_rejecter(t):
    mgr = t.L.actor("manager")
    return (lambda: t.L.design_goal(status="rejected", note="no", change_actor_id=mgr),
            lambda: t.x("UPDATE design_goals SET status='rejected', note='no', change_actor_id=? WHERE id=?",
                        (mgr, t.L.design_goal())))


def _manager_kind(t):
    owner = t.L.actor("owner")
    mgr = t.L.actor("manager")
    return (lambda: t.L.review(manager_actor_id=owner),
            lambda: t.x("UPDATE reviews SET manager_actor_id=? WHERE id=?", (owner, t.L.review(manager_actor_id=mgr))))


def _relation_reverse(t):
    a, b, c = t.issue(), t.issue(), t.issue()
    def insert():
        t.L.relation(a, b)
        t.L.relation(b, a)
    def update():
        t.L.relation(a, b)
        t.x("UPDATE issue_relations SET from_issue_id=?, to_issue_id=? WHERE id=?", (b, a, t.L.relation(c, a)))
    return insert, update


def _relation_cycle(t):
    a, b, c = t.issue(), t.issue(), t.issue()
    def insert():
        t.L.relation(a, b, "blocks")
        t.L.relation(b, a, "blocks")
    def update():
        t.L.relation(a, b, "blocks")
        t.x("UPDATE issue_relations SET relation='blocks' WHERE id=?", (t.L.relation(b, a, "caused-by"),))
    return insert, update


def _closes_duplicate(t):
    """An AFTER twin: both close the source issue as duplicate (compared by effect, not message)."""
    a, b = t.issue(), t.issue()
    return (lambda: t.L.relation(a, b, "duplicate-of"),
            lambda: t.x("UPDATE issue_relations SET relation='duplicate-of' WHERE id=?", (t.L.relation(a, b),)),
            lambda: t.scalar("SELECT status FROM issues WHERE id=?", (a,)))


TWINS = {
    "tr_step_finding_in_plan": _step_finding,
    "tr_issue_domain_active": _issue_domain,
    "tr_issue_theme_active": _issue_theme,
    "tr_review_domain_active": _review_domain,
    "tr_metric_domain_active": _metric_domain,
    "tr_goal_domain_active": _goal_domain,
    "tr_run_domain_active": _run_domain,
    "tr_dep_order": _dep_order,
    "tr_step_phase_plan": _step_phase_plan,
    "tr_gate_options_shape": _gate_options,
    "tr_gate_fixed_options": _gate_fixed_options,
    "tr_gate_multi_shape": _gate_multi,
    "tr_goal_approver": _goal_approver,
    "tr_gate_answerer": _gate_answerer,
    "tr_decision_decider": _decision_decider,
    "tr_goal_rejecter": _goal_rejecter,
    "tr_review_manager_kind": _manager_kind,
    "tr_relation_noreverse_dup": _relation_reverse,
    "tr_relation_no_cycle": _relation_cycle,
    "tr_relation_closes_duplicate": _closes_duplicate,
}


class RuleTwins(TwinCase):
    def test_rule_twins_agree(self):
        names = {r[0] for r in self.x("SELECT name FROM sqlite_master WHERE type='trigger'")}
        pairs = sorted(n[:-4] for n in names if n.endswith("_upd") and n[:-4] in names)
        self.assertEqual(pairs, sorted(TWINS), "each tr_X/tr_X_upd pair needs a case in TWINS")
        for name in pairs:
            with self.subTest(pair=name):
                case = TWINS[name](self)
                if len(case) == 3:
                    insert, update, probe = case
                    self.assertEqual(self.effect(insert, probe), "duplicate")
                    self.assertEqual(self.effect(update, probe), "duplicate")
                    continue
                insert, update = case
                refused = self.message(insert)
                self.assertIsNotNone(refused, "the INSERT case is not refused")
                self.assertEqual(self.message(update), refused)

    def test_insert_rules_hold_on_update(self):
        """The UPDATE probes of DBS-M3: a step finding moved to an off-plan finding, a relation turned into
        duplicate-of, and a row moved to a retired domain on each table, each refused (or closing) as on INSERT."""
        for name, needle in (("tr_step_finding_in_plan", "plan reviews"), ("tr_issue_domain_active", DOMAIN_RETIRED),
                             ("tr_review_domain_active", DOMAIN_RETIRED), ("tr_metric_domain_active", DOMAIN_RETIRED),
                             ("tr_goal_domain_active", DOMAIN_RETIRED), ("tr_run_domain_active", DOMAIN_RETIRED)):
            with self.subTest(rule=name):
                _, update = TWINS[name](self)
                self.assertIn(needle, self.message(update) or "")
        with self.subTest(rule="duplicate-of closes on UPDATE"):
            _, update, probe = TWINS["tr_relation_closes_duplicate"](self)
            self.assertEqual(self.effect(update, probe), "duplicate")
        with self.subTest(rule="a finding's domain never changes"):
            fid = self.L.finding(self.r1)
            self.assertIn("append-only", self.message(
                lambda: self.x("UPDATE findings SET domain_code='cost' WHERE id=?", (fid,))) or "")

    def test_retired_theme_refused(self):
        fid = self.L.finding(self.r1)
        for label, fn in (("finding insert", lambda: self.L.finding(self.r1, theme_code="old-theme")),
                          ("finding update", lambda: self.x("UPDATE findings SET theme_code='old-theme' WHERE id=?",
                                                            (fid,))),
                          ("issue insert", lambda: self.issue(theme_code="old-theme")),
                          ("issue update", lambda: self.x("UPDATE issues SET theme_code='old-theme' WHERE id=?",
                                                          (self.issue(),)))):
            with self.subTest(label):
                msg = self.message(fn)
                self.assertIsNotNone(msg)
                self.assertTrue(THEME_RETIRED in msg or "append-only" in msg, msg)
        with self.subTest("a current theme is accepted"):
            self.assertIsNone(self.message(lambda: self.issue(theme_code="validation")))


class KnownGaps(TwinCase):
    def test_constraints_close_known_gaps(self):
        """A repository needs a path, an issue cannot move to a retired domain, and a plan is superseded exactly when
        it names its successor."""
        with self.subTest("repository without a path"):
            msg = self.message(lambda: self.insert("repositories", name="other", path=None))
            self.assertIn("NOT NULL constraint failed: repositories.path", msg or "")
        with self.subTest("issue moved to a retired domain"):
            iid = self.issue()
            self.assertIn(DOMAIN_RETIRED, self.message(
                lambda: self.x("UPDATE issues SET domain_code='cost' WHERE id=?", (iid,))) or "")
        pid = self.L.plan(reviews=(self.r1,))
        p2 = self.L.plan(reviews=(self.r1,))
        for label, sql, args in (("superseded without a successor", "UPDATE plans SET status='superseded' WHERE id=?",
                                  (pid,)),
                                 ("a successor without superseded", "UPDATE plans SET superseded_by=? WHERE id=?",
                                  (p2, pid))):
            with self.subTest(label):
                self.assertIn("CHECK constraint failed", self.message(lambda: self.x(sql, args)) or "")
        with self.subTest("superseded with its successor"):
            self.assertIsNone(self.message(lambda: self.x(
                "UPDATE plans SET status='superseded', superseded_by=?, change_note='replaced' WHERE id=?", (p2, pid))))


if __name__ == "__main__":
    unittest.main()
