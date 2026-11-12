"""Plan P-0001 step 225 (FIX-32): answers given on a published page are imported as part of the loop.

test_gates_reminds_to_import_when_page_published  gates, check and progress-page say nothing until a published page
                                                  is recorded, then name it and when answers were last imported
test_import_answers_records_event                 each import-answers run is an answers-imported event on the plan
test_hub_shows_last_import                        the home route gives the page and the time of the last import
test_skill_says_when_to_import                    SKILL.md says to import at the start of a session, before each
                                                  batch and before telling the owner what is waiting
"""
import json
import re
import unittest

from dev.tests import fixtures

URL = "https://pages.example.com/hub"
NOTE = "note: the owner may have answered on the published page"


def published(test):
    L = fixtures.Ledger(fixtures.ScratchRepo())
    test.addCleanup(L.close)
    fixtures.seed_demo(L)
    L.commit()
    return L


def ok(L, *args):
    p = L.run(*args)
    if p.returncode not in (0, 1) or p.stderr.strip().startswith("Traceback"):
        raise AssertionError(f"ledger.py {' '.join(map(str, args))} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
    return p.stdout


class ImportReminder(unittest.TestCase):
    def test_gates_reminds_to_import_when_page_published(self):
        L = published(self)
        for cmd in (("gates",), ("check",), ("progress-page",)):
            with self.subTest(before=cmd):
                self.assertNotIn(NOTE, ok(L, *cmd))
        p = L.run("add-artifact", "progress-page", "--plan", "P-0001", "--url", URL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for cmd in (("gates",), ("check",), ("progress-page",)):
            with self.subTest(after=cmd):
                out = ok(L, *cmd)
                self.assertIn(f"{NOTE} {URL} (answers last imported: never)", out)
                self.assertIn("import-answers FILE", out)
        answers = L.root / "answers.json"
        answers.write_text("[]", encoding="utf-8")
        p = L.run("import-answers", str(answers))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        at = L.scalar("SELECT at FROM events WHERE event = 'answers-imported'")
        self.assertIn(f"(answers last imported: {at})", ok(L, "gates"))

    def test_import_answers_records_event(self):
        L = published(self)
        gate = L.one("SELECT code, options FROM gates WHERE status = 'open' ORDER BY id LIMIT 1")
        value = json.loads(gate["options"])[0]["value"] if gate["options"] else None
        answers = L.root / "answers.json"
        answers.write_text(json.dumps([{"gate": gate["code"], "answer_value": value, "answer_text": "Yes."},
                                       {"gate": "GATE-9999", "answer_text": "?"}]), encoding="utf-8")
        p = L.run("import-answers", str(answers))
        self.assertIn(f"{gate['code']}: recorded", p.stdout, p.stdout + p.stderr)
        events = L.all("SELECT entity_type, entity_id, to_value, note FROM events WHERE event = 'answers-imported'")
        self.assertEqual(events, [{"entity_type": "plan", "entity_id": L.scalar("SELECT id FROM plans"),
                                   "to_value": "1 recorded, 0 already answered, 1 refused",
                                   "note": "from answers.json"}])
        L.run("import-answers", str(answers))
        self.assertEqual(L.count("events", "event = 'answers-imported'"), 2)
        self.assertEqual(L.scalar("SELECT label FROM enum_labels WHERE enum = 'event' AND value = 'answers-imported'"),
                         "Answers brought in from the published page")

    def test_hub_shows_last_import(self):
        L = published(self)
        with fixtures.ServerThread(L) as srv:
            home = srv.get("/api/home").json()
        self.assertIsNone(home["published_page"])
        self.assertIsNone(home["answers_imported_at"])
        L.run("add-artifact", "progress-page", "--plan", "P-0001", "--url", URL)
        answers = L.root / "answers.json"
        answers.write_text("[]", encoding="utf-8")
        L.run("import-answers", str(answers))
        with fixtures.ServerThread(L) as srv:
            home = srv.get("/api/home").json()
        self.assertEqual(home["published_page"], URL)
        self.assertEqual(home["answers_imported_at"], L.scalar("SELECT max(at) FROM events "
                                                               "WHERE event = 'answers-imported'"))
        page = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")
        self.assertIn('id:"answers-imported"', page)
        self.assertIn(" were last brought in ", page)

    def test_skill_says_when_to_import(self):
        raw = (fixtures.REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        bullets = [" ".join(b.split()) for b in re.split(r"\n- ", raw)]
        rule = next((b for b in bullets if b.startswith("**Collect answers**")), "")
        self.assertIn("import-answers", rule)
        for when in ("start of every session", "before every batch", "before telling the owner what is waiting"):
            self.assertIn(when, rule)
        self.assertLessEqual(len((fixtures.REPO_ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()), 375)


if __name__ == "__main__":
    unittest.main()
