"""Plan P-0001 step 217 (OWN-14, schema 18): a gate can take several of its options. open-gate --multi (with
--preselect for the recommended ones) makes a multi-select gate; its answer is stored as a JSON list of option values,
checked against the options by answer_gate() and by tr_gate_answer_rules; answer-gate takes --value more than once,
import-answers and the viewer take a list. Single-select gates are as they were."""
import json
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, refused, new_ledger

OPTS = ("--option", "ruff=Ruff", "--option", "mypy=mypy", "--option", "vale=Vale:prose linter")


def ledger(test):
    L = new_ledger()
    test.addCleanup(L.close)
    ok(L, "add-actor", "manager", "review manager")
    return L


def multi_gate(L, *extra):
    return ok(L, "open-gate", "decision", "--title", "Tools", "--question", "Which tools may run?", *OPTS,
              "--multi", "--preselect", "ruff,vale", *extra, "--actor", "A-0001")[0]


class MultiSelect(unittest.TestCase):
    def test_multiselect_answer_stored_as_list(self):
        L = ledger(self)
        gate = multi_gate(L, "--no-text")
        g = L.one("SELECT multi, preselect, allow_text FROM gates WHERE code=?", (gate,))
        self.assertEqual((g["multi"], json.loads(g["preselect"]), g["allow_text"]), (1, ["ruff", "vale"], 0))
        out = ok(L, "answer-gate", gate, "--value", "vale", "--value", "ruff", "--value", "vale", "--actor", "A-0001")
        self.assertEqual(out, [f"{gate}: answered (ruff, vale)"])
        stored = L.one("SELECT status, answer_value FROM gates WHERE code=?", (gate,))
        self.assertEqual((stored["status"], json.loads(stored["answer_value"])), ("answered", ["ruff", "vale"]))
        note = L.one("SELECT note FROM events WHERE entity_type='gate' AND event='status-changed' ORDER BY id DESC")
        self.assertIn("ruff, vale", note["note"])
        with self.subTest("an empty choice is an answer: none of them"):
            g2 = multi_gate(L)
            L.commit()
            mod = L.module
            con = mod.connect(L.db)
            try:
                self.assertEqual(mod.answer_gate(con, g2, [], None, "viewer"), {"ok": True})
            finally:
                con.close()
            self.assertEqual(L.one("SELECT answer_value FROM gates WHERE code=?", (g2,))["answer_value"], "[]")
        with self.subTest("single-select gates are as they were"):
            one = ok(L, "open-gate", "decision", "--title", "One", "--question", "Which?", *OPTS, "--actor", "A-0001")[0]
            self.assertEqual(L.one("SELECT multi, preselect FROM gates WHERE code=?", (one,))["multi"], 0)
            self.assertIn("one option", refused(self, L, "answer-gate", one, "--value", "ruff", "--value", "mypy",
                                                "--actor", "A-0001"))
            ok(L, "answer-gate", one, "--value", "mypy", "--actor", "A-0001")
            self.assertEqual(L.one("SELECT answer_value FROM gates WHERE code=?", (one,))["answer_value"], "mypy")
        with self.subTest("the viewer's lists show the labels"):
            L.commit()
            con = L.module.connect(L.db)
            try:
                hist = L.module.route(L.root, L.db, con, "/api/gates", {"status": ["all"]}, None)
            finally:
                con.close()
            label = {g["code"]: g["answer_label"] for g in hist["gates"]}
            self.assertEqual(label[gate], "Ruff, Vale")

    def test_multiselect_refuses_unknown_option(self):
        L = ledger(self)
        self.assertIn("not one of the options", refused(self, L, "open-gate", "decision", "--title", "T", "--question",
                                                       "Q?", *OPTS, "--multi", "--preselect", "black",
                                                       "--actor", "A-0001"))
        self.assertIn("--multi", refused(self, L, "open-gate", "decision", "--title", "T", "--question", "Q?", *OPTS,
                                         "--preselect", "ruff", "--actor", "A-0001"))
        self.assertIn("option", refused(self, L, "open-gate", "decision", "--title", "T", "--question", "Q?",
                                        "--multi", "--actor", "A-0001"))
        gate = multi_gate(L)
        self.assertIn("not one of the options", refused(self, L, "answer-gate", gate, "--value", "ruff", "--value",
                                                        "black", "--actor", "A-0001"))
        L.commit()
        for bad in ('["black"]', '"ruff"', "ruff", '[1]', '{"a": 1}'):
            with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError) as cm:
                L.x("UPDATE gates SET status='answered', answer_value=? WHERE code=?", (bad, gate))
            self.assertIn("option values", str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError):   # preselect checked by the database too
            L.x("UPDATE gates SET preselect='[\"black\"]' WHERE code=?", (gate,))
        L.x("UPDATE gates SET status='answered', answer_value='[\"mypy\"]' WHERE code=?", (gate,))

    def test_import_answers_multiselect(self):
        L = ledger(self)
        gate = multi_gate(L)
        one = ok(L, "open-gate", "decision", "--title", "One", "--question", "Which?", *OPTS, "--actor", "A-0001")[0]
        f = L.dir / "answers.json"
        f.write_text(json.dumps([{"gate": gate, "answer_value": ["mypy", "ruff"], "answer_text": "and nothing else"},
                                 {"gate": one, "answer_value": ["ruff", "mypy"]}]), encoding="utf-8")
        p = L.run("import-answers", str(f))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"{gate}: recorded", p.stdout)
        self.assertIn(f"{one}: not recorded", p.stdout)
        got = L.one("SELECT answer_value, answer_text, answered_via FROM gates WHERE code=?", (gate,))
        self.assertEqual((json.loads(got["answer_value"]), got["answer_text"], got["answered_via"]),
                         (["ruff", "mypy"], "and nothing else", "viewer"))   # in the options' order
        self.assertEqual(L.one("SELECT status FROM gates WHERE code=?", (one,))["status"], "open")

    def test_pending_gate_carries_its_shape(self):
        L = ledger(self)
        gate = multi_gate(L)
        L.commit()
        con = L.module.connect(L.db)
        try:
            d = L.module.route(L.root, L.db, con, "/api/gates", {}, None)
        finally:
            con.close()
        g = next(x for x in d["gates"] if x["code"] == gate)
        self.assertEqual((g["multi"], g["preselect"]), (1, ["ruff", "vale"]))


if __name__ == "__main__":
    unittest.main()
