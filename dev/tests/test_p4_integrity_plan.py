"""Plan P-0001 steps 54-56 (schema 3): delete behaviour, plan-structure guards on UPDATE, relation rules.

Each test drives the ledger with plain SQL (the Ledger helpers only make parent rows), so it pins what the database
itself refuses or does.
"""
import sqlite3
import unittest

from dev.tests._schema_helpers import SchemaCase
from dev.tests.fixtures import Ledger, seed_demo


class IntegrityCase(SchemaCase):
    def setUp(self):
        super().setUp()
        self.r1 = self.review()
        self.r2 = self.review()

    def raises(self, sql, args=(), needle=""):
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.x(sql, args)
        msg = str(cm.exception)
        needles = needle if isinstance(needle, tuple) else (needle,)
        self.assertTrue(any(n in msg for n in needles), f"{msg!r} names none of {needles}")
        return msg

    def status(self, iid):
        return self.scalar("SELECT status FROM issues WHERE id=?", (iid,))

    def events(self, entity_type, entity_id, event=None):
        sql = "SELECT event, from_value, to_value, note FROM events WHERE entity_type=? AND entity_id=?"
        args = [entity_type, entity_id]
        if event:
            sql += " AND event=?"
            args.append(event)
        return self.L.all(sql + " ORDER BY id", args)


class Deletes(IntegrityCase):
    """Step 54: a referenced review cannot be deleted; the cascade keys fire only for unprotected parents."""

    def test_review_delete_refused_while_referenced(self):
        self.issue(self.r1)
        n = self.scalar("SELECT count(*) FROM reviews")
        self.raises("DELETE FROM reviews WHERE id=?", (self.r1,), "append-only")
        # the plain foreign keys refuse it as well (the trigger dropped for the probe, inside a savepoint)
        def go():
            self.x("DROP TRIGGER tr_review_no_delete")
            self.raises("DELETE FROM reviews WHERE id=?", (self.r1,), "FOREIGN KEY constraint failed")
        self.in_savepoint(go)
        self.assertEqual(self.scalar("SELECT count(*) FROM reviews"), n)

    def test_cascades(self):
        """Every ON DELETE CASCADE key removes its children when its parent row is deleted (on the seeded demo, with
        the append-only triggers dropped and foreign keys deferred, inside a transaction that is rolled back); a
        plan with no steps is the one cascade reachable in normal use."""
        with self.subTest("an empty plan is deleted with its phases and plan_reviews"):
            def go():
                pid = self.L.plan(reviews=(self.r1,))
                self.L.phase(pid, 1)
                self.x("DELETE FROM plans WHERE id=?", (pid,))
                self.assertEqual(self.scalar("SELECT count(*) FROM phases WHERE plan_id=?", (pid,)), 0)
                self.assertEqual(self.scalar("SELECT count(*) FROM plan_reviews WHERE plan_id=?", (pid,)), 0)
            self.in_savepoint(go)
        with self.subTest("a plan with steps is protected by the steps"):
            def go():
                pid = self.L.plan()
                self.L.step(pid, self.L.phase(pid, 1), 1)
                self.raises("DELETE FROM plans WHERE id=?", (pid,), "append-only")
            self.in_savepoint(go)
        demo = Ledger()
        self.addCleanup(demo.close)
        seed_demo(demo)
        con = demo.con
        fks = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            for row in con.execute(f'PRAGMA foreign_key_list("{t}")'):
                if row[6] == "CASCADE":
                    fks.append((t, row[2], row[3], row[4] or "id"))
        self.assertGreaterEqual(len(fks), 17, fks)
        exercised = 0
        for child, parent, col, key in fks:
            with self.subTest(child=child, parent=parent):
                con.execute("BEGIN")
                try:
                    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                               "AND sql LIKE '%BEFORE DELETE%'").fetchall():
                        con.execute(f'DROP TRIGGER "{name}"')
                    con.execute("PRAGMA defer_foreign_keys = ON")
                    parents = [r[0] for r in con.execute(f'SELECT DISTINCT "{col}" FROM "{child}"')]
                    if parents:
                        exercised += 1
                        marks = ",".join("?" * len(parents))
                        con.execute(f'DELETE FROM "{parent}" WHERE "{key}" IN ({marks})', parents)
                        self.assertEqual(con.execute(f'SELECT count(*) FROM "{child}" WHERE "{col}" IN ({marks})',
                                                     parents).fetchone()[0], 0)
                finally:
                    con.rollback()
        self.assertGreaterEqual(exercised, 10, "the demo should give most cascade keys children to delete")


class PlanGuards(IntegrityCase):
    """Step 55: dependency order and step/phase/plan consistency hold on UPDATE as well as INSERT."""

    def setUp(self):
        super().setUp()
        self.p = self.L.plan()
        self.ph = self.L.phase(self.p, 1)
        self.s1 = self.L.step(self.p, self.ph, 1)
        self.s2 = self.L.step(self.p, self.ph, 2)
        self.s3 = self.L.step(self.p, self.ph, 3)

    def test_dep_order_insert(self):
        self.x("INSERT INTO step_dependencies VALUES (?, ?)", (self.s2, self.s1))
        self.raises("INSERT INTO step_dependencies VALUES (?, ?)", (self.s1, self.s3), "earlier step")
        self.raises("INSERT INTO step_dependencies VALUES (?, ?)", (self.s1, self.s1), "earlier step")
        other = self.L.plan()
        o1 = self.L.step(other, self.L.phase(other, 1), 1)
        self.raises("INSERT INTO step_dependencies VALUES (?, ?)", (self.s3, o1), "earlier step")

    def test_dep_order_update_refused(self):
        self.x("INSERT INTO step_dependencies VALUES (?, ?)", (self.s2, self.s1))
        self.raises("UPDATE step_dependencies SET depends_on_step_id=? WHERE step_id=?", (self.s3, self.s2),
                    "earlier step")
        self.raises("UPDATE step_dependencies SET step_id=? WHERE step_id=?", (self.s1, self.s2), "earlier step")

    def test_step_renumber_keeps_dep_order(self):
        self.x("INSERT INTO step_dependencies VALUES (?, ?)", (self.s3, self.s1))
        self.x("UPDATE steps SET number=5 WHERE id=?", (self.s3,))   # still after step 1
        self.assertEqual(self.scalar("SELECT number FROM steps WHERE id=?", (self.s3,)), 5)

    def test_renumber_cannot_create_backward_dependency(self):
        self.x("INSERT INTO step_dependencies VALUES (?, ?)", (self.s3, self.s1))
        self.raises("UPDATE steps SET number=10 WHERE id=?", (self.s1,), "earlier step")

    def test_renumber_cannot_create_forward_dependency(self):
        self.x("INSERT INTO step_dependencies VALUES (?, ?)", (self.s2, self.s1))
        self.raises("UPDATE steps SET number=0 WHERE id=?", (self.s2,), "earlier step")

    def test_step_phase_plan_insert(self):
        other = self.L.plan()
        oph = self.L.phase(other, 1)
        self.raises("INSERT INTO steps(plan_id, phase_id, number, title, change, proof) VALUES (?,?,?,?,?,?)",
                    (self.p, oph, 9, "t", "c", "p"), "another plan")

    def test_step_phase_plan_update_refused(self):
        other = self.L.plan()
        oph = self.L.phase(other, 1)
        self.raises("UPDATE steps SET phase_id=? WHERE id=?", (oph, self.s1), "another plan")
        self.raises("UPDATE steps SET plan_id=? WHERE id=?", (other, self.s3), ("another plan", "earlier step"))
        self.raises("UPDATE phases SET plan_id=? WHERE id=?", (other, self.ph), "another plan")

    def test_dependency_rules_hold_on_update(self):
        self.test_dep_order_update_refused()
        self.test_step_phase_plan_update_refused()


class Relations(IntegrityCase):
    """Steps 55-56: no reverse duplicates (related-to is symmetric), no blocks or duplicate cycles, on INSERT and
    UPDATE; recording duplicate-of or merged-into closes the source issue."""

    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = (self.issue(self.r1) for _ in range(3))

    def rel(self, f, t, relation):
        return self.L.relation(f, t, relation)

    def test_relation_reverse_refused(self):
        for relation in ("duplicate-of", "related-to"):
            with self.subTest(relation=relation, op="insert"):
                def go():
                    self.rel(self.a, self.b, relation)
                    self.raises("INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, rationale) "
                                "VALUES (?,?,?,'x')", (self.b, self.a, relation), ("reverse relation", "cycle"))
                self.in_savepoint(go)
        with self.subTest("blocks 2-cycle"):
            def go():
                self.rel(self.a, self.b, "blocks")
                self.raises("INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, rationale) "
                            "VALUES (?,?,'blocks','x')", (self.b, self.a), "cycle")
            self.in_savepoint(go)
        with self.subTest("update into a reverse"):
            def go():
                self.rel(self.a, self.b, "related-to")
                rid = self.rel(self.c, self.b, "related-to")
                self.raises("UPDATE issue_relations SET from_issue_id=?, to_issue_id=? WHERE id=?",
                            (self.b, self.a, rid), "reverse relation")
            self.in_savepoint(go)
        with self.subTest("update into a blocks cycle"):
            def go():
                self.rel(self.a, self.b, "blocks")
                rid = self.rel(self.c, self.a, "related-to")
                self.raises("UPDATE issue_relations SET relation='blocks', from_issue_id=?, to_issue_id=? "
                            "WHERE id=?", (self.b, self.a, rid), "cycle")
            self.in_savepoint(go)

    def test_relation_symmetric_allowed(self):
        """Distinct pairs and a blocks chain without a cycle are accepted (was: reverse related-to and blocks
        allowed; ISS-0167 refuses those now)."""
        self.rel(self.a, self.b, "related-to")
        self.rel(self.a, self.c, "related-to")
        self.rel(self.a, self.b, "blocks")
        self.rel(self.b, self.c, "blocks")
        self.rel(self.a, self.c, "blocks")
        self.assertEqual(self.scalar("SELECT count(*) FROM issue_relations"), 5)

    def test_relation_cycle_refused(self):
        self.rel(self.a, self.b, "blocks")
        self.rel(self.b, self.c, "blocks")
        self.raises("INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, rationale) "
                    "VALUES (?,?,'blocks','x')", (self.c, self.a), "cycle")
        d = self.issue(self.r1)
        e = self.issue(self.r1)
        self.rel(d, e, "duplicate-of")
        self.raises("INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, rationale) "
                    "VALUES (?,?,'merged-into','x')", (e, d), "cycle")

    def test_duplicate_relation_closes_source(self):
        actor = self.actor("matcher")
        self.L.relation(self.a, self.b, "duplicate-of", "Same bug.", established_by_actor_id=actor,
                        established_in_review_id=self.r2)
        self.assertEqual(self.status(self.a), "duplicate")
        self.assertEqual(self.status(self.b), "open")
        ev = self.events("issue", self.a, "status-changed")
        self.assertEqual([(e["from_value"], e["to_value"]) for e in ev], [("open", "duplicate")])
        self.assertIn("Same bug.", ev[0]["note"])
        code_a = self.scalar("SELECT code FROM issues WHERE id=?", (self.a,))
        self.assertEqual(self.L.count("v_open_issues", "code=?", (code_a,)), 0)
        with self.subTest("merged-into closes too; a closed source stays as it is"):
            fixed = self.issue(self.r1, status="fixed", after_state="ok")
            self.rel(self.c, self.b, "merged-into")
            self.rel(fixed, self.b, "duplicate-of")
            self.assertEqual((self.status(self.c), self.status(fixed)), ("duplicate", "fixed"))


if __name__ == "__main__":
    unittest.main()
