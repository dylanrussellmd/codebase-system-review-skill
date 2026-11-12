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


if __name__ == "__main__":
    unittest.main()
