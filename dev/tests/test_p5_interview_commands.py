"""The owner interview and per-domain design goals (plan P-0001 step 129, OWN-5): interview-ask, interview-answer,
propose-goal, approve-goals, reject-goal, link-step-goals and the read-only goals and interview commands, with the
schema-9 rules behind them (codes, append-only answers, goal transitions, an approver for every approved goal, a note
for every rejected one, events for every change) and the owner's approval through a confirm gate (answer_gate(), so
the viewer, answer-gate and import-answers all approve)."""
import json
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, project, refused


def interview_project(test, approved=True):
    """project() with the owner A-0002 added (the manager is A-0001; R-0001 includes spec and tests)."""
    repo, L, sha = project(test, approved=approved)
    ok(L, "add-actor", "owner", "the owner")
    return L


def events(L, entity_type):
    L.commit()
    return L.all("SELECT event, from_value, to_value, actor_id, note FROM events WHERE entity_type=? ORDER BY id",
                 (entity_type,))


class InterviewTest(unittest.TestCase):
    def test_interview_round_trip(self):
        L = interview_project(self)
        self.assertEqual(ok(L, "interview-ask", "--review", "R-0001", "--domain", "spec", "--why",
                            "src/app.py has a handler with no stated contract", "Which inputs must handler accept?",
                            "--actor", "A-0001"), ["IQ-0001"])
        self.assertEqual(ok(L, "interview-ask", "--review", "R-0001", "--why", "no README states the audience",
                            "Who will maintain this after you?", "--actor", "A-0001"), ["IQ-0002"])
        ok(L, "interview-answer", "IQ-0001", "Only JSON bodies; anything else is refused.", "--actor", "A-0001")
        ok(L, "interview-answer", "IQ-0001", "And never more than 1 MB.", "--via", "viewer", "--actor", "A-0001")
        out = json.loads("\n".join(ok(L, "interview", "--json")))
        self.assertEqual([q["code"] for q in out["questions"]], ["IQ-0001", "IQ-0002"])
        q1 = out["questions"][0]
        self.assertEqual((q1["review"], q1["domain"], q1["why_asked"], q1["question"]),
                         ("R-0001", "spec", "src/app.py has a handler with no stated contract",
                          "Which inputs must handler accept?"))
        self.assertEqual([(a["answer"], a["answered_via"]) for a in q1["answers"]],
                         [("Only JSON bodies; anything else is refused.", "chat"), ("And never more than 1 MB.", "viewer")])
        self.assertTrue(all(a["answered_at"].endswith("Z") and len(a["answered_at"]) == 24 for a in q1["answers"]))
        self.assertEqual(out["questions"][1]["domain"], None)
        self.assertEqual(out["questions"][1]["answers"], [])
        with self.subTest("filters and text form"):
            spec = json.loads("\n".join(ok(L, "interview", "--domain", "spec", "--json")))
            self.assertEqual([q["code"] for q in spec["questions"]], ["IQ-0001"])
            text = "\n".join(ok(L, "interview", "--review", "R-0001"))
            self.assertIn("IQ-0001", text)
            self.assertIn("Only JSON bodies", text)
            self.assertIn("Whole project", text)
            self.assertIn("not answered yet", text)
        with self.subTest("events"):
            self.assertEqual([(e["event"], e["actor_id"]) for e in events(L, "question")],
                             [("asked", 1), ("asked", 1), ("answered", 1), ("answered", 1)])
        with self.subTest("refusals"):
            self.assertIn("did not include", refused(self, L, "interview-ask", "--review", "R-0001", "--domain",
                                                     "ui", "--why", "w", "q"))
            self.assertIn("--why", refused(self, L, "interview-ask", "--review", "R-0001", "--domain", "spec", "q"))
            self.assertIn("no question IQ-0009", refused(self, L, "interview-answer", "IQ-0009", "a"))
            self.assertIn("give", refused(self, L, "interview-answer", "IQ-0001", "  "))
        with self.subTest("answers are append-only"):
            for sql in ("UPDATE interview_answers SET answer='changed'", "DELETE FROM interview_answers",
                        "DELETE FROM interview_questions", "UPDATE interview_questions SET question='x'"):
                with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                    L.x(sql)
            L.con.rollback()

    def test_interview_needs_approved_domains(self):
        L = interview_project(self, approved=False)
        self.assertIn("approves the review's domains",
                      refused(self, L, "interview-ask", "--review", "R-0001", "--domain", "spec", "--why", "w", "q"))

    def test_question_codes_sequential(self):
        L = interview_project(self)
        ok(L, "interview-ask", "--review", "R-0001", "--why", "w", "q")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "sequential"):
            L.x("INSERT INTO interview_questions(code, review_id, question, why_asked) VALUES ('IQ-0003', 1, 'q', 'w')")
        self.assertEqual(ok(L, "next-code", "IQ"), ["IQ-0002"])

    def test_question_answered_in_the_hub_through_a_gate(self):
        """interview-ask --gate asks the question in the hub as well; the owner's answer there (answer_gate) is
        recorded as the question's answer, via viewer."""
        L = interview_project(self)
        ok(L, "interview-ask", "--review", "R-0001", "--domain", "spec", "--why", "w", "Must it log?", "--gate")
        code = L.scalar("SELECT g.code FROM gates g JOIN interview_questions q ON q.gate_id=g.id")
        self.assertEqual(code, "GATE-0001")
        mod = fixtures.load_ledger_module(L.ledger_py)
        self.assertEqual(mod.gate_answer_request(L.db, code, {"answer_text": "Every refusal, yes."}), {"ok": True})
        L.con.rollback()
        out = json.loads("\n".join(ok(L, "interview", "--json")))
        self.assertEqual([(a["answer"], a["answered_via"]) for a in out["questions"][0]["answers"]],
                         [("Every refusal, yes.", "viewer")])


class GoalsTest(unittest.TestCase):
    def setUp(self):
        self.L = L = interview_project(self)
        ok(L, "interview-ask", "--review", "R-0001", "--domain", "spec", "--why", "w", "Which inputs?")
        ok(L, "interview-ask", "--review", "R-0001", "--why", "w", "Who maintains it?")
        ok(L, "interview-ask", "--review", "R-0001", "--domain", "tests", "--why", "w", "How fast must tests be?")
        ok(L, "interview-answer", "IQ-0001", "JSON only.")
        ok(L, "interview-answer", "IQ-0002", "Someone new each year.")

    def goals(self, *args):
        return json.loads("\n".join(ok(self.L, "goals", "--json", *args)))["goals"]

    def test_goals_need_approval(self):
        L = self.L
        self.assertEqual(ok(L, "propose-goal", "--domain", "spec", "--from", "IQ-0001",
                            "The handler accepts JSON bodies only.", "--actor", "A-0001"), ["G-01"])
        self.assertEqual(ok(L, "propose-goal", "--from", "IQ-0002", "A new maintainer can run it from the README."),
                         ["G-02"])
        self.assertEqual(self.goals(), [])
        self.assertEqual(ok(L, "goals"), ["no approved design goals (goals --all lists proposed ones too)"])
        allg = self.goals("--all")
        self.assertEqual([(g["code"], g["status"], g["domain"]) for g in allg],
                         [("G-02", "proposed", None), ("G-01", "proposed", "spec")])  # whole project first
        self.assertIn("owner", refused(self, L, "approve-goals", "G-01", "--actor", "A-0001"))  # the manager
        self.assertEqual(ok(L, "approve-goals", "G-01", "G-02", "--actor", "A-0002"),
                         ["G-01: approved", "G-02: approved"])
        got = self.goals()
        self.assertEqual([(g["code"], g["status"], g["approved_by"]) for g in got],
                         [("G-02", "approved", "A-0002"), ("G-01", "approved", "A-0002")])
        self.assertTrue(got[0]["approved_at"].endswith("Z"))
        with self.subTest("--domain gives the domain's goals and the whole-project ones"):
            self.assertEqual([g["code"] for g in self.goals("--domain", "spec")], ["G-02", "G-01"])
            self.assertEqual([g["code"] for g in self.goals("--domain", "tests")], ["G-02"])
            self.assertIn("G-01", "\n".join(ok(L, "goals", "--domain", "spec")))
        with self.subTest("approval writes an event with the approver"):
            ev = [e for e in events(L, "goal") if e["event"] == "status-changed"]
            self.assertEqual([(e["from_value"], e["to_value"], e["actor_id"]) for e in ev],
                             [("proposed", "approved", 2), ("proposed", "approved", 2)])
            self.assertEqual([e["event"] for e in events(L, "goal")][:2], ["proposed", "source-linked"])
        with self.subTest("refusals"):
            self.assertIn("approved", refused(self, L, "approve-goals", "G-01", "--actor", "A-0002"))
            self.assertIn("no design goal G-09", refused(self, L, "approve-goals", "G-09", "--actor", "A-0002"))
            self.assertIn("did not include", refused(self, L, "propose-goal", "--domain", "ui", "--from", "IQ-0001", "x"))
            self.assertIn("IQ-0003", refused(self, L, "propose-goal", "--domain", "spec", "--from", "IQ-0003", "x"))
            self.assertIn("no question IQ-0042", refused(self, L, "propose-goal", "--from", "IQ-0042", "x"))
        with self.subTest("an approved goal is not edited"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "retire it"):
                L.x("UPDATE design_goals SET text='other' WHERE code='G-01'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "approver"):
                L.x("INSERT INTO design_goals(code, text, status) VALUES ('G-03', 't', 'approved')")
            L.con.rollback()

    def test_reject_needs_note(self):
        L = self.L
        ok(L, "propose-goal", "--domain", "tests", "Tests run in under a second.")
        self.assertIn("--note", refused(self, L, "reject-goal", "G-01"))
        self.assertEqual(ok(L, "reject-goal", "G-01", "--note", "a minute is fine", "--actor", "A-0002"),
                         ["G-01: rejected"])
        self.assertEqual(self.goals(), [])
        g = self.goals("--all")[0]
        self.assertEqual((g["status"], g["note"]), ("rejected", "a minute is fine"))
        self.assertIn("rejected", refused(self, L, "approve-goals", "G-01", "--actor", "A-0002"))
        ok(L, "propose-goal", "Another goal.")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):  # rejected needs a note
            L.x("UPDATE design_goals SET status='rejected' WHERE code='G-02'")
        L.con.rollback()

    def test_goal_sources(self):
        L = self.L
        ok(L, "propose-goal", "--domain", "spec", "--from", "IQ-0001,IQ-0002", "Inputs are JSON; errors say why.")
        ok(L, "approve-goals", "G-01", "--actor", "A-0002")
        g = self.goals()[0]
        self.assertEqual([s["code"] for s in g["sources"]], ["IQ-0001", "IQ-0002"])
        self.assertEqual(g["sources"][0]["question"], "Which inputs?")
        self.assertEqual(g["sources"][0]["answers"], ["JSON only."])
        self.assertIn("IQ-0001", "\n".join(ok(L, "goals")))
        self.assertEqual(L.scalar("SELECT count(*) FROM goal_sources"), 2)

    def test_goals_for_brief_view(self):
        L = self.L
        ok(L, "propose-goal", "--domain", "spec", "--from", "IQ-0001", "Inputs are JSON.")
        ok(L, "propose-goal", "--from", "IQ-0002", "Runs from the README.")
        ok(L, "propose-goal", "--domain", "tests", "Not approved yet.")
        ok(L, "approve-goals", "G-01", "G-02", "--actor", "A-0002")
        L.commit()
        rows = L.all("SELECT domain_code, code, text, sources FROM v_goals_for_brief")
        self.assertEqual(rows, [{"domain_code": None, "code": "G-02", "text": "Runs from the README.",
                                 "sources": "IQ-0002"},
                                {"domain_code": "spec", "code": "G-01", "text": "Inputs are JSON.",
                                 "sources": "IQ-0001"}])

    def test_step_goals(self):
        L = self.L
        ok(L, "propose-goal", "--domain", "spec", "Inputs are JSON.")
        ok(L, "propose-goal", "--domain", "spec", "Errors say why.")
        ok(L, "approve-goals", "G-01", "--actor", "A-0002")
        rid = L.id_of("reviews", "R-0001")
        plan = L.plan("P-0001", reviews=[rid])
        phase = L.phase(plan, 1)
        L.step(plan, phase, 1, no_findings_reason="test step")
        L.commit()
        self.assertEqual(ok(L, "link-step-goals", "1", "G-01", "--plan", "P-0001", "--actor", "A-0001"),
                         ["step 1 serves G-01"])
        self.assertIn("approved", refused(self, L, "link-step-goals", "1", "G-02", "--plan", "P-0001"))
        self.assertIn("no step 7", refused(self, L, "link-step-goals", "7", "G-01", "--plan", "P-0001"))
        self.assertEqual(ok(L, "link-step-goals", "1", "G-01", "--plan", "P-0001"), ["step 1 serves G-01 already"])
        L.commit()
        self.assertEqual(L.all("SELECT s.number, g.code FROM step_goals sg JOIN steps s ON s.id=sg.step_id "
                               "JOIN design_goals g ON g.id=sg.goal_id"), [{"number": 1, "code": "G-01"}])
        ev = [e for e in events(L, "goal") if e["event"] == "step-linked"]
        self.assertEqual([e["to_value"] for e in ev], ["P-0001 #1"])
        g = self.goals()[0]
        self.assertEqual(g["steps"], ["P-0001 #1"])

    def test_approval_through_the_gate(self):
        """propose-goal --gate lists the goals in one confirm gate; answering it approve (viewer, answer-gate or
        import-answers: all answer_gate()) approves every goal on it, by the owner who answered."""
        L = self.L
        ok(L, "propose-goal", "--domain", "spec", "--from", "IQ-0001", "Inputs are JSON.", "--gate")
        ok(L, "propose-goal", "--from", "IQ-0002", "Runs from the README.", "--gate")
        L.commit()
        gates = L.all("SELECT code, kind, status, context FROM gates")
        self.assertEqual([(g["code"], g["kind"], g["status"]) for g in gates], [("GATE-0001", "confirm", "open")])
        self.assertIn("G-01", gates[0]["context"])
        self.assertIn("Runs from the README.", gates[0]["context"])
        self.assertEqual(L.scalar("SELECT count(*) FROM v_owner_queue WHERE ref='GATE-0001'"), 1)
        mod = fixtures.load_ledger_module(L.ledger_py)
        self.assertEqual(mod.gate_answer_request(L.db, "GATE-0001", {"answer_value": "approve"}), {"ok": True})
        L.con.rollback()
        got = self.goals()
        owner = L.scalar("SELECT min(id) FROM actors WHERE kind='owner'")
        self.assertEqual([(g["code"], g["approved_by"]) for g in got], [("G-02", f"A-{owner:04d}"),
                                                                       ("G-01", f"A-{owner:04d}")])
        self.assertEqual(L.scalar("SELECT count(*) FROM review_goals"), 2)
        with self.subTest("a goal proposed after the answer opens a new gate"):
            ok(L, "propose-goal", "--domain", "tests", "--review", "R-0001", "Tests take a second.", "--gate")
            L.commit()
            self.assertEqual(L.scalar("SELECT count(*) FROM gates WHERE status='open'"), 1)
            p = L.root / "answers.json"
            p.write_text(json.dumps([{"gate": "GATE-0002", "answer_value": "change",
                                      "answer_text": "say a minute"}]), encoding="utf-8")
            L.run_ok("import-answers", str(p))
            self.assertEqual([g["status"] for g in self.goals("--all") if g["code"] == "G-03"], ["proposed"])

    def test_approve_goals_answers_their_gate(self):
        L = self.L
        ok(L, "propose-goal", "--domain", "spec", "--review", "R-0001", "Inputs are JSON.", "--gate")
        ok(L, "approve-goals", "G-01", "--actor", "A-0002")
        L.commit()
        self.assertEqual(L.one("SELECT status, answer_value, answered_by_actor_id FROM gates"),
                         {"status": "answered", "answer_value": "approve", "answered_by_actor_id": 2})


if __name__ == "__main__":
    unittest.main()
