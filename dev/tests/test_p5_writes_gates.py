"""open-gate, answer-gate and apply-gate, with answer_gate() shared by the viewer and the CLI (plan P-0001 step 105:
ABS-H3, ABS-M5)."""
import json
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import counts, new_ledger, ok, project, refused

OPTS = ["--option", "keep=Keep it", "--option", "drop=Drop it:remove the column"]


def gate_ledger(test):
    repo, L, _ = project(test, approved=False)
    return L


class GateCommandsTest(unittest.TestCase):
    def test_open_gate_validates_and_allocates(self):
        L = gate_ledger(self)
        self.assertEqual(ok(L, "open-gate", "decision", "--title", "Keep?", "--question", "Keep the column?", *OPTS,
                            "--review", "R-0001", "--actor", "A-0001"), ["GATE-0001"])
        g = L.one("SELECT kind, options, allow_text, blocking, review_id, opened_by_actor_id FROM gates")
        self.assertEqual(json.loads(g["options"]), [{"value": "keep", "label": "Keep it"},
                                                    {"value": "drop", "label": "Drop it",
                                                     "description": "remove the column"}])
        self.assertEqual((g["allow_text"], g["blocking"], g["review_id"], g["opened_by_actor_id"]), (1, 1, 1, 1))
        self.assertIn("unique", refused(self, L, "open-gate", "confirm", "--title", "T", "--question", "Q",
                                        "--option", "a=A", "--option", "a=B"))
        self.assertIn("needs at least one --option", refused(self, L, "open-gate", "confirm", "--title", "T",
                                                             "--question", "Q", "--no-text"))
        self.assertIn("kind 'nope'", refused(self, L, "open-gate", "nope", "--title", "T", "--question", "Q"))
        self.assertEqual(ok(L, "open-gate", "confirm", "--title", "T", "--question", "Q"), ["GATE-0002"])

    def test_answer_gate_cli_uses_viewer_rules(self):
        L = gate_ledger(self)
        ok(L, "open-gate", "decision", "--title", "Keep?", "--question", "Keep?", *OPTS, "--no-text")
        self.assertIn("not one of the options", refused(self, L, "answer-gate", "GATE-0001", "--value", "maybe"))
        self.assertIn("Choose one of the options", refused(self, L, "answer-gate", "GATE-0001", "--text", "hmm"))
        self.assertIn("no such gate", refused(self, L, "answer-gate", "GATE-0009", "--value", "keep"))
        # step 158: a gate without free text takes its option alone
        self.assertIn("no note", refused(self, L, "answer-gate", "GATE-0001", "--value", "keep", "--text", "for now"))
        self.assertEqual(ok(L, "answer-gate", "GATE-0001", "--value", "keep"), ["GATE-0001: answered (keep)"])
        g = L.one("SELECT status, answered_via, answered_by, answered_by_actor_id FROM gates")
        owner = L.scalar("SELECT id FROM actors WHERE kind='owner'")
        self.assertEqual(g, {"status": "answered", "answered_via": "chat", "answered_by": "owner",
                             "answered_by_actor_id": owner})
        self.assertIn("already answered", refused(self, L, "answer-gate", "GATE-0001", "--value", "drop"))

    def test_answer_gate_rules_shared(self):
        """The same inputs give the same refusals through the viewer's API and the CLI."""
        L = gate_ledger(self)
        ok(L, "open-gate", "decision", "--title", "Keep?", "--question", "Keep?", *OPTS, "--no-text")
        ok(L, "open-gate", "decision", "--title", "Why?", "--question", "Why?")
        mod = fixtures.load_ledger_module(L.ledger_py)
        cases = [("GATE-0001", {"answer_value": "maybe"}, ["--value", "maybe"]),
                 ("GATE-0001", {"answer_text": "hmm"}, ["--text", "hmm"]),
                 ("GATE-0002", {}, []),
                 ("GATE-0009", {"answer_value": "keep"}, ["--value", "keep"])]
        for code, body, args in cases:
            api_res = mod.gate_answer_request(L.db, code, body)
            self.assertIsInstance(api_res, tuple)
            cli = refused(self, L, "answer-gate", code, *args)
            self.assertEqual(cli, f"answer-gate: {api_res[0]['error']}; nothing was written", (code, body))
        self.assertEqual(mod.gate_answer_request(L.db, "GATE-0001", {"answer_value": "keep"}), {"ok": True})
        self.assertEqual(L.scalar("SELECT answered_via FROM gates WHERE code='GATE-0001'"), "viewer")
        cli = refused(self, L, "answer-gate", "GATE-0001", "--value", "keep")
        self.assertEqual(cli, f"answer-gate: {mod.gate_answer_request(L.db, 'GATE-0001', {'answer_value': 'keep'})[0]['error']}; nothing was written")

    def test_apply_gate_writes_decision(self):
        L = gate_ledger(self)
        L.issue("ISS-0001", first_seen_review_id=1)
        L.commit()
        ok(L, "open-gate", "decision", "--title", "Keep?", "--question", "Keep the column?", *OPTS,
           "--issue", "ISS-0001", "--review", "R-0001")
        ok(L, "answer-gate", "GATE-0001", "--value", "drop", "--text", "nobody reads it")
        out = ok(L, "apply-gate", "GATE-0001", "--note", "Planned the removal in step 3.", "--actor", "A-0001")
        self.assertEqual(out, ["GATE-0001: applied", "decision D-0001: drop - nobody reads it"])
        d = L.one("SELECT code, question, answer, rationale, status, review_id, decided_by_actor_id FROM decisions")
        owner = L.scalar("SELECT id FROM actors WHERE kind='owner'")
        self.assertEqual(d, {"code": "D-0001", "question": "Keep the column?", "answer": "drop - nobody reads it",
                             "rationale": "Planned the removal in step 3.", "status": "decided", "review_id": 1,
                             "decided_by_actor_id": owner})
        g = L.one("SELECT status, applied_note, decision_id FROM gates")
        self.assertEqual(g, {"status": "applied", "applied_note": "Planned the removal in step 3.", "decision_id": 1})
        self.assertEqual(L.scalar("SELECT count(*) FROM decision_issues"), 1)
        # an open decision already linked to the gate is decided rather than a new one made
        did = L.decision("D-0002")
        L.commit()
        ok(L, "open-gate", "decision", "--title", "T", "--question", "Q", "--decision", "D-0002")
        ok(L, "answer-gate", "GATE-0002", "--text", "yes")
        ok(L, "apply-gate", "GATE-0002", "--note", "done")
        self.assertEqual(L.one("SELECT status, answer FROM decisions WHERE id=?", (did,)),
                         {"status": "decided", "answer": "yes"})
        self.assertEqual(L.scalar("SELECT count(*) FROM decisions"), 2)

    def test_applied_gate_requires_note(self):
        L = gate_ledger(self)
        ok(L, "open-gate", "confirm", "--title", "T", "--question", "Q")
        self.assertIn("only an answered gate", refused(self, L, "apply-gate", "GATE-0001", "--note", "x"))
        ok(L, "answer-gate", "GATE-0001", "--text", "fine")
        before = counts(L, "events", "decisions")
        self.assertIn("give --note", refused(self, L, "apply-gate", "GATE-0001"))
        self.assertIn("give --note", refused(self, L, "apply-gate", "GATE-0001", "--note", "  "))
        self.assertEqual(counts(L, "events", "decisions"), before)
        self.assertEqual(L.scalar("SELECT status FROM gates"), "answered")
        ok(L, "apply-gate", "GATE-0001", "--note", "Noted.")
        self.assertEqual(L.one("SELECT status, applied_note FROM gates"), {"status": "applied", "applied_note": "Noted."})
        self.assertEqual(counts(L, "decisions"), {"decisions": 0})   # a confirm gate records no decision

    def test_viewer_route_uses_answer_gate(self):
        """The viewer's POST answer goes through answer_gate(): its refusal texts and the owner actor."""
        L = new_ledger()
        self.addCleanup(L.close)
        L.gate("GATE-0001", options=[{"value": "a", "label": "A"}], allow_text=0)
        L.commit()
        mod = fixtures.load_ledger_module(L.ledger_py)
        self.assertEqual(mod.api(L.root, L.db, "/api/gates/GATE-0001/answer", {}, {"answer_text": "x"}),
                         ({"error": "Choose one of the options."}, 400))
        self.assertEqual(mod.api(L.root, L.db, "/api/gates/GATE-0001/answer", {}, {"answer_value": 3}),
                         ({"error": "An answer is text."}, 400))
        self.assertEqual(mod.api(L.root, L.db, "/api/gates/GATE-0001/answer", {}, {"answer_value": "a"}),
                         {"ok": True})


class OwnerAnswersTest(unittest.TestCase):
    """Only the owner or another human answers a gate, decides a decision or rejects a goal (ISS-0427)."""

    def test_owner_gates_answered_by_owner_only(self):
        L = gate_ledger(self)   # A-0001 is the manager
        ok(L, "add-actor", "human", "Ann")   # A-0002
        for kind, extra in (("production-action", ()), ("decision", OPTS), ("other", ())):
            with self.subTest(kind=kind):
                gate = ok(L, "open-gate", kind, "--title", "T", "--question", "Q", *extra)[0]
                answer = ("--value", "keep") if extra else ("--text", "go ahead")
                before = counts(L, "events", "gates", "actors")
                self.assertIn("owner", refused(self, L, "answer-gate", gate, *answer, "--by", "A-0001"))
                self.assertEqual(counts(L, "events", "gates", "actors"), before)
                self.assertEqual(L.scalar("SELECT status FROM gates WHERE code=?", (gate,)), "open")
                ok(L, "answer-gate", gate, *answer, "--by", "A-0002")
                self.assertEqual(L.scalar("SELECT answered_by_actor_id FROM gates WHERE code=?", (gate,)), 2)
        with self.subTest("the schema holds the same rule"):
            gid = L.gate(kind="other")
            with self.assertRaisesRegex(Exception, "answered by the owner"):
                L.x("UPDATE gates SET status='answered', answer_text='yes', answered_by_actor_id=1 WHERE id=?", (gid,))
            L.con.rollback()
            did = L.insert("decisions", code="D-0001", question="Which?", status="open")
            with self.assertRaisesRegex(Exception, "decided by the owner"):
                L.x("UPDATE decisions SET status='decided', answer='a', decided_by_actor_id=1 WHERE id=?", (did,))
            L.con.rollback()

    def test_apply_refuses_unconfirmed_viewer_production_action(self):
        """A production-action or accept-risk answer from the viewer is applied only after the owner confirms it in
        chat (answer-gate --confirm); a chat answer applies at once (ISS-0427)."""
        L = gate_ledger(self)
        for kind in ("production-action", "accept-risk"):
            with self.subTest(kind=kind):
                gate = ok(L, "open-gate", kind, "--title", "Deploy?", "--question", "Deploy to production?")[0]
                L.module.answer_gate(L.con, gate, None, "yes, deploy", via="viewer")
                L.commit()
                before = counts(L, "events", "gates")
                self.assertIn("--confirm", refused(self, L, "apply-gate", gate, "--note", "deployed"))
                self.assertEqual(counts(L, "events", "gates"), before)
                with self.assertRaisesRegex(Exception, "confirmed by the owner in chat"):
                    L.x("UPDATE gates SET status='applied', applied_note='x' WHERE code=?", (gate,))
                L.con.rollback()
                self.assertIn("owner", refused(self, L, "answer-gate", gate, "--confirm", "--by", "A-0001"))
                ok(L, "answer-gate", gate, "--confirm", "--text", "yes, go")
                g = L.one("SELECT confirmed_at, confirmed_by_actor_id FROM gates WHERE code=?", (gate,))
                self.assertIsNotNone(g["confirmed_at"])
                self.assertIn("answer-confirmed", [e[2] for e in L.events("gate", L.id_of("gates", gate))])
                ok(L, "apply-gate", gate, "--note", "deployed")
        with self.subTest("an answer given in chat needs no confirmation"):
            gate = ok(L, "open-gate", "production-action", "--title", "Deploy?", "--question", "Deploy?")[0]
            ok(L, "answer-gate", gate, "--text", "yes")
            self.assertIn("in chat", refused(self, L, "answer-gate", gate, "--confirm"))
            ok(L, "apply-gate", gate, "--note", "deployed")

    def test_reject_goal_needs_owner(self):
        L = gate_ledger(self)   # A-0001 is the manager
        ok(L, "add-actor", "owner", "the owner")   # A-0002
        ok(L, "propose-goal", "--domain", "spec", "Answers are kept.", "--review", "R-0001", "--gate")
        gate = L.scalar("SELECT g.code FROM gates g JOIN design_goals d ON d.gate_id = g.id")
        self.assertIn("owner", refused(self, L, "reject-goal", "G-01", "--note", "no", "--actor", "A-0001"))
        self.assertIn("owner", refused(self, L, "reject-goal", "G-01", "--note", "no"))
        self.assertEqual(L.scalar("SELECT status FROM gates WHERE code=?", (gate,)), "open")
        self.assertEqual(L.scalar("SELECT status FROM design_goals"), "proposed")
        with self.assertRaisesRegex(Exception, "rejected by the owner"):
            L.x("UPDATE design_goals SET status='rejected', note='no', change_actor_id=1")
        L.con.rollback()
        ok(L, "reject-goal", "G-01", "--note", "not needed", "--actor", "A-0002")
        self.assertEqual(L.scalar("SELECT status FROM design_goals"), "rejected")
        self.assertNotEqual(L.scalar("SELECT status FROM gates WHERE code=?", (gate,)), "open")


if __name__ == "__main__":
    unittest.main()
