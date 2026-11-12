"""The viewer's input rules (plan P-0001 steps 156 to 160): encoded codes in a path, numbers and text checked before
use, the gate answer rules through the viewer, answer-gate and import-answers, literal % and _ in the issue search,
and lists that report their total and come a page at a time."""
import json
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import test_routes as tr
from dev.tests.fixtures import Ledger, seed_demo

OPTS = [{"value": "a", "label": "Option A"}, {"value": "b", "label": "Option B"}]


class Case(tr.RouteCase):
    """Route tests on this module's own demo ledger (test_routes closes its shared ones when it is done)."""

    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.L.commit()
        cls.mod = cls.L.module

    @classmethod
    def tearDownClass(cls):
        cls.L.close()


class Encoded(Case):
    def test_encoded_codes_resolve(self):
        self.assertEqual(self.ok("/api/review/R%2D0001")["review"]["code"], "R-0001")
        self.assertEqual(self.ok("/api/issue/ISS%2D0002")["issue"]["code"], "ISS-0002")
        self.assertEqual(self.ok("/api/table/%69ssues")["columns"][0], "id")
        self.assertEqual(self.mod.allowed_methods("/api/gates/GATE%2D0001/answer"), ["POST"])
        # a slash that arrives encoded stays inside its segment: it names no code and reaches no other route
        self.assertEqual(self.call("/api/review/R-0001%2Fx")[0], 404)
        L = Ledger()
        try:
            seed_demo(L)
            L.commit()
            status, res = self.call("/api/gates/GATE%2D0001/answer", {"answer_value": "use-7"}, L=L)
            self.assertEqual((status, res), (200, {"ok": True}))
            L.con.rollback()
            self.assertEqual(L.scalar("SELECT status FROM gates WHERE code='GATE-0001'"), "answered")
        finally:
            L.close()


class BadInput(Case):
    def test_bad_inputs_get_400_plain_message(self):
        probes = [("/api/table/issues", None, {"limit": "abc"}), ("/api/table/issues", None, {"offset": "x"}),
                  ("/api/source/abc", None, {}), ("/api/sql", {"sql": 5}, {}),
                  ("/api/gates/GATE-0001/answer", {"answer_value": 5}, {}),
                  ("/api/issues", None, {"limit": "many"}), ("/api/gates", None, {"status": "all", "offset": "1e3"})]
        for path, body, qs in probes:
            status, res = self.call(path, body, **qs)
            self.assertEqual(status, 400, (path, res))
            self.assertNotRegex(res["error"], r"int\(|attribute|Traceback|invalid literal", path)
            self.assertRegex(res["error"], r"^[A-Za-z].*\.$", path)

    def test_answer_non_string_400(self):
        for body in ({"answer_value": 5}, {"answer_text": ["x"]}, {"answer_value": "use-7", "answer_text": {"a": 1}}):
            self.assertEqual(self.call("/api/gates/GATE-0001/answer", body), (400, {"error": "An answer is text."}))
        self.assertEqual(self.L.scalar("SELECT status FROM gates WHERE code='GATE-0001'"), "open")

    def test_sql_non_string_is_400(self):
        for sql in (5, None, ["SELECT 1"], {"q": 1}):
            self.assertEqual(self.call("/api/sql", {"sql": sql}), (400, {"error": "sql must be text."}), sql)

    def test_table_non_int_params_400(self):
        for k in ("limit", "offset"):
            for v in ("abc", "1.5", "", "0x10", "١٢"):
                self.assertEqual(self.call("/api/table/issues", **{k: v}),
                                 (400, {"error": f"{k} must be a whole number."}), (k, v))

    def test_source_route_status_codes(self):
        lid = self.L.scalar("SELECT min(id) FROM finding_locations")
        self.assertEqual(self.call(f"/api/source/{lid}")[0], 200)
        self.assertEqual(self.call("/api/source/99999")[0], 404)
        self.assertEqual(self.call("/api/source/abc")[0], 400)
        self.assertEqual(self.call("/api/source/-1")[0], 404)

    def test_table_paging_clamps(self):
        get = lambda **qs: self.ok("/api/table/issues", **qs)
        self.assertEqual((get(limit=0)["limit"], get(limit=99999)["limit"], get(offset=-5)["offset"]), (1, 500, 0))
        for name in ("nope", 'issues"', "sqlite_master"):
            self.assertEqual(self.call(f"/api/table/{name}")[0], 404, name)
        self.assertEqual(self.call("/api/table/issues", limit="bad")[0], 400)
        self.assertGreater(len(self.ok("/api/table/v_issue_timeline")["rows"]), 0)


class AnswerRules(unittest.TestCase):
    """One set of answer rules, whoever records the answer: the viewer's route, answer-gate and import-answers."""

    def setUp(self):
        self.L = L = Ledger()
        self.addCleanup(L.close)
        seed_demo(L)
        L.gate("GATE-0003", options=OPTS, allow_text=0)
        L.gate("GATE-0004", options=OPTS, allow_text=1)
        L.gate("GATE-0005", allow_text=1)
        L.gate("GATE-0006", options=OPTS, status="answered", answer_value="a")
        L.gate("GATE-0007", options=OPTS, status="applied", answer_value="a")
        L.gate("GATE-0008", options=OPTS, status="withdrawn")
        L.commit()

    def viewer(self, code, value=None, text=None):
        con = self.L.module.connect(self.L.db)
        try:
            res = self.L.module.route(self.L.root, self.L.db, con, f"/api/gates/{code}/answer", {},
                                      {"answer_value": value, "answer_text": text})
        finally:
            con.close()
        return (res[1], res[0]["error"]) if isinstance(res, tuple) else (200, "")

    def command(self, code, value=None, text=None):
        args = ["answer-gate", code] + (["--value", value] if value is not None else []) \
            + (["--text", text] if text is not None else [])
        p = self.L.run(*args)
        return (200 if p.returncode == 0 else 400), p.stdout + p.stderr

    def imported(self, code, value=None, text=None):
        f = fixtures.temp_dir() / "answers.json"
        f.write_text(json.dumps({code: {"answer_value": value, "answer_text": text}}), encoding="utf-8")
        p = self.L.run("import-answers", str(f))
        return (200 if p.returncode == 0 and "left as it is" not in p.stdout else 400), p.stdout + p.stderr

    def gate(self, code):
        self.L.con.rollback()  # see what the command or the viewer just committed
        return self.L.one("SELECT * FROM gates WHERE code=?", (code,))

    def test_gate_answer_validation_matrix(self):
        long = "x" * 20_000
        refused = [  # (gate, value, text, viewer status, words of the sentence)
            ("GATE-0003", "zzz", None, 400, "not one of the options"),        # test_answer_invalid_option_400
            ("GATE-0003", None, None, 400, "Choose an option"),               # test_answer_empty_400
            ("GATE-0004", "  ", " \n ", 400, "Choose an option"),
            ("GATE-0003", None, "my words", 400, "Choose one of the options"),  # test_answer_text_not_allowed_400
            ("GATE-0003", "a", "and a note", 400, "no note"),
            ("GATE-0004", "a", long, 400, "too long"),
            ("GATE-0005", None, long, 400, "too long"),
        ]
        for via in (self.viewer, self.command, self.imported):
            for code, value, text, status, words in refused:
                if via == self.command and value is not None and not value.strip() and text is not None:
                    continue  # the command refuses blank text itself
                got, said = via(code, value, text)
                self.assertNotEqual(got, 200, (via.__name__, code, value, said))
                if via == self.viewer:
                    self.assertEqual(got, status, (code, said))
                self.assertIn(words, said, (via.__name__, code))
                self.assertEqual(self.gate(code)["status"], "open", (via.__name__, code))
        self.assertEqual(self.viewer("GATE-9999", "a")[0], 404)     # test_answer_unknown_404

    def test_answer_not_open_409(self):
        for code, word in (("GATE-0006", "already answered"), ("GATE-0007", "acted on"), ("GATE-0008", "withdrawn")):
            status, said = self.viewer(code, "a")
            self.assertEqual(status, 409, code)
            self.assertIn(word, said)
            self.assertIn(word, self.command(code, "b")[1])
            self.assertEqual(self.gate(code)["answer_value"], "a" if code != "GATE-0008" else None)

    def test_answer_success_side_effects(self):
        for n, via in enumerate((self.viewer, self.command, self.imported)):
            code = f"GATE-{9 + n:04d}"
            self.L.gate(code, options=OPTS, allow_text=1)
            self.L.commit()
            self.assertEqual(via(code, "b", " a note ")[0], 200, via.__name__)
            g = self.gate(code)
            self.assertEqual((g["status"], g["answer_value"], g["answer_text"]), ("answered", "b", "a note"))
            self.assertEqual(g["answered_via"], "chat" if via == self.command else "viewer")
            self.assertIsNotNone(g["answered_at"])
            owner = self.L.scalar("SELECT id FROM actors WHERE kind='owner'")
            self.assertEqual(g["answered_by_actor_id"], owner)
            self.assertEqual(self.L.scalar("""SELECT to_value FROM events WHERE entity_type='gate' AND entity_id=?
                                              ORDER BY id DESC""", (g["id"],)), "answered")
        exactly = "y" * 10_000
        self.assertEqual(self.viewer("GATE-0005", None, exactly)[0], 200)

    def test_trigger_holds_the_same_rules(self):
        con = self.L.con
        upd = "UPDATE gates SET status='answered', answer_value=?, answer_text=? WHERE code=?"
        for args, words in ((("a", "note", "GATE-0003"), "no free text"), (("a", "x" * 10_001, "GATE-0004"), "too long"),
                            ((None, "x" * 10_001, "GATE-0005"), "too long")):
            with self.assertRaises(sqlite3.IntegrityError) as e:
                con.execute(upd, args)
            self.assertIn(words, str(e.exception))
        con.execute(upd, ("a", "x" * 10_000, "GATE-0004"))
        con.rollback()


class Search(Case):
    def codes(self, L=None, **qs):
        return [r["code"] for r in self.ok("/api/issues", L=L, **qs)["rows"]]

    def test_issues_search_wildcards_literal(self):
        every = ["ISS-0001", "ISS-0002", "ISS-0004", "ISS-0003"]
        self.assertEqual(self.codes(), every)
        self.assertEqual(self.codes(q="%"), [])
        self.assertEqual(self.codes(q="_"), [])
        self.assertEqual(self.codes(q="\\"), [])
        self.assertEqual(self.codes(q="ISS-000_"), [])
        L = Ledger()
        try:
            ids = seed_demo(L)
            r = ids["reviews"]["R-0002"]
            L.issue("ISS-0005", first_seen_review_id=r, title="Coverage is 50% of the target")
            L.issue("ISS-0006", first_seen_review_id=r, title="snake_case names leak")
            L.issue("ISS-0007", first_seen_review_id=r, title="Back\\slash in a path")
            L.commit()
            self.assertEqual(self.codes(L=L, q="%"), ["ISS-0005"])
            self.assertEqual(self.codes(L=L, q="50%"), ["ISS-0005"])
            self.assertEqual(self.codes(L=L, q="e_c"), ["ISS-0006"])
            self.assertEqual(self.codes(L=L, q="\\"), ["ISS-0007"])
            self.assertEqual(self.codes(L=L, q="%", severity="H"), [])
        finally:
            L.close()
        # the search still covers code, title, sub, point id, summary and path, and filters stay exact and ANDed
        for q, want in (("ISS-0003", ["ISS-0003"]), ("hardcoded", ["ISS-0002"]), ("code-compare", ["ISS-0004"]),
                        ("SPEC-L1", ["ISS-0003"]), ("random generator", ["ISS-0001"]),
                        ("requests.py", ["ISS-0002", "ISS-0003"])):
            self.assertEqual(self.codes(q=q), want, q)
        self.assertEqual(self.codes(q="auth.py", severity="M"), ["ISS-0004"])
        self.assertEqual(self.codes(status="ver"), [])


class Paging(Case):
    def test_issues_route_reports_total(self):
        d = self.ok("/api/issues")
        self.assertEqual(set(d), {"rows", "total", "limit", "offset"})
        self.assertEqual((d["total"], d["limit"], d["offset"], len(d["rows"])), (4, 200, 0, 4))
        d = self.ok("/api/issues", limit=2, offset=1)
        self.assertEqual(([r["code"] for r in d["rows"]], d["total"], d["limit"], d["offset"]),
                         (["ISS-0002", "ISS-0004"], 4, 2, 1))
        self.assertEqual(self.ok("/api/issues", severity="M", limit=1)["total"], 2)
        self.assertEqual(self.ok("/api/issues", limit=0)["limit"], 1)
        self.assertEqual(self.ok("/api/issues", limit=99999)["limit"], 500)
        self.assertEqual(self.ok("/api/issues", offset=-3)["offset"], 0)
        self.assertEqual(self.ok("/api/issues", offset=50)["rows"], [])

    def test_issues_truncation_flag(self):
        L = Ledger()
        self.addCleanup(L.close)
        r1, stamp = L.review("R-0001"), "2026-11-01T10:00:00.000Z"
        L.con.executemany("INSERT INTO issues(code, title, severity, domain_code, kind, sub, first_seen_review_id, "
                          "times_seen, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [(f"ISS-{n:04d}", f"Bulk {n}", "M", "spec", "defect", f"bulk-{n}", r1, 1, stamp, stamp)
                           for n in range(1, 2002)])
        L.commit()
        d = self.ok("/api/issues", L=L, status="")
        self.assertEqual((d["total"], len(d["rows"])), (2001, 200))
        last = self.ok("/api/issues", L=L, limit=500, offset=2000)
        self.assertEqual(len(last["rows"]), d["total"] - 2000)

    def test_gates_history_paged(self):
        L = Ledger()
        try:
            seed_demo(L)
            big = "context " * 2000
            for n in range(450):
                L.gate(f"GATE-{3 + n:04d}", options=OPTS, status="applied", answer_value="b", context=big,
                       created_at=f"2026-11-02T10:{n // 60:02d}:{n % 60:02d}.000Z")
            L.commit()
            d = self.ok("/api/gates", L=L, status="all")
            self.assertEqual((d["total"], d["limit"], d["offset"], len(d["gates"])), (452, 200, 0, 200))
            self.assertLess(len(json.dumps(d)), 200_000)
            self.assertNotIn("context", d["gates"][0])
            self.assertNotIn("options", d["gates"][0])
            self.assertEqual(d["gates"][0]["answer_label"], "Option B")
            # newest first (step 186), then the next pages
            self.assertEqual(d["gates"][0]["code"], "GATE-0452")
            times = [g["created_at"] for g in d["gates"]]
            self.assertEqual(times, sorted(times, reverse=True))
            rest = self.ok("/api/gates", L=L, status="all", offset=400)
            self.assertEqual(len(rest["gates"]), 52)
            # the inbox keeps whole gates in its own order, with no paging
            p = self.ok("/api/gates", L=L)
            self.assertIn("context", p["gates"][0])
            self.assertEqual([g["code"] for g in p["gates"]], ["GATE-0001", "GATE-0002"])
        finally:
            L.close()


if __name__ == "__main__":
    unittest.main()
