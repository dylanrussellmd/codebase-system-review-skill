"""progress-md, progress-page (the published hub) and import-answers (plan P-0001 steps 113 and 125; WALK-L15, OWN-2).

test_progress_md_matches_plan_progress  the phase rows and totals equal v_plan_progress; every step is listed
test_progress_page_is_self_contained    one file, no outside resource, every view the page can ask for embedded
                                        (every review, issue, plan, table and code reference), every step title in it
test_hub_patches_apply                  the hub is page.html as it is, plus one data script
test_hub_data_cannot_close_its_script   ledger text holding </script> or <!-- stays inside the data
test_import_answers_marks_gates_answered answers from the hub's database are recorded through answer_gate()
test_hub_answers_round_trip             answer in the hub's database -> import-answers -> applied -> republished hub
                                        shows the gate applied
"""
import json
import re
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger


def snapshot_of(html):
    m = re.search(r"<script>window\.LEDGER_SNAPSHOT = (.*?);</script>", html, re.S)
    assert m, "no snapshot in the page"
    return json.loads(m.group(1))


class HubBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.ids = fixtures.seed_demo(cls.L)
        fixtures.finish_demo(cls.L)
        cls.L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()


class ProgressMd(HubBase):
    def test_progress_md_matches_plan_progress(self):
        L = self.L
        text = L.run_ok("progress-md", "P-0001").stdout
        phases = L.all("SELECT * FROM v_plan_progress WHERE plan='P-0001' ORDER BY phase")
        keys = ("steps", "done", "doing", "waiting_owner", "not_started", "dropped")
        rows = [l for l in text.splitlines() if re.match(r"^\| \d+ \| ", l)]
        table = [l for l in rows if len(l.split("|")) == 10]  # the phase table has 8 cells
        self.assertEqual(len(table), len(phases))
        for line, p in zip(table, phases):
            cells = [c.strip() for c in line.strip("|").split("|")]
            self.assertEqual(int(cells[0]), p["phase"])
            self.assertEqual([int(c) for c in cells[2:]], [p[k] for k in keys])
        total = next(l for l in text.splitlines() if "**All phases**" in l)
        cells = [c.strip() for c in total.strip("|").split("|")]
        self.assertEqual([int(c) for c in cells[2:]], [sum(p[k] for p in phases) for k in keys])
        for s in L.all("SELECT number, title, status FROM steps"):
            self.assertIn(f"| {s['number']} | {s['status']} | {s['title']} |", text)
        self.assertTrue(text.startswith("# Progress of P-0001: "))
        out = L.root / "out" / "PROGRESS.md"
        L.run_ok("progress-md", "P-0001", "-o", str(out))
        self.assertEqual(out.read_text().split("\n", 6)[6], text.split("\n", 6)[6])  # same, but for the time line
        p = L.run("progress-md", "P-0999")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no plan P-0999", p.stderr)


class ProgressPage(HubBase):
    def build(self, *args):
        out = self.L.root / "hub-test.html"
        p = self.L.run_ok("progress-page", *args, "-o", str(out))
        self.assertIn("no outside requests", p.stdout)
        return out.read_text(encoding="utf-8")

    def test_progress_page_is_self_contained(self):
        L = self.L
        html = self.build("P-0001")
        # no outside resource: no script or style from elsewhere, no link elements, no url() or @import
        self.assertIsNone(re.search(r"<(script|img|iframe|link)\b[^>]*\b(src|href)\s*=", html, re.I))
        self.assertNotIn("@import", html)
        self.assertIsNone(re.search(r"url\(\s*['\"]?https?:", html))
        snap = snapshot_of(html)
        self.assertEqual(snap["start"], "plan/P-0001")
        R = snap["responses"]
        want = {"overview", "gates?status=pending", "gates?status=all", "reviews", "plans", "trends", "issues",
                "tables"}
        want |= {"review/" + r["code"] for r in L.all("SELECT code FROM reviews")}
        want |= {"issue/" + r["code"] for r in L.all("SELECT code FROM issues")}
        want |= {"plan/" + r["code"] for r in L.all("SELECT code FROM plans")}
        want |= {"source/" + str(r["id"]) for r in L.all("SELECT id FROM finding_locations")}
        want |= {"table/" + r["name"] for r in L.all(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite%'")}
        self.assertEqual(want - set(R), set())
        for s in L.all("SELECT title FROM steps"):
            self.assertIn(json.dumps(s["title"], ensure_ascii=False)[1:-1], html)
        # the same counts the viewer's /api/overview gives
        m = L.module
        con = m.connect(L.db, readonly=True)
        try:
            live = m.route(L.root, L.db, con, "/api/overview", {}, None)
        finally:
            con.close()
        self.assertEqual(R["overview"], json.loads(json.dumps(live, default=str)))
        self.assertIn('["guide","#/guide","How it works"]', html)
        self.assertEqual(snap["active"], [r[0] for r in L.con.execute(
            "SELECT value FROM issue_statuses WHERE active=1 ORDER BY position")])
        # default file: hub.html beside the ledger, owner-only
        p = L.run_ok("progress-page")
        hub = L.db.parent / "hub.html"
        self.assertTrue(hub.is_file(), p.stdout)
        self.assertEqual(hub.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(snapshot_of(hub.read_text())["start"])
        self.assertEqual(L.run("progress-page", "P-0999").returncode, 1)

    def test_hub_patches_apply(self):
        m = self.L.module
        (marker, script), = m.HUB_PATCHES  # the data script is the only change to the page
        self.assertEqual(m.PAGE.count(marker), 1)
        html, _ = m.hub_page(self.L.root, self.L.db)
        page = re.sub(r"<script>window\.LEDGER_SNAPSHOT = .*?;</script>\n", "", html, flags=re.S)
        self.assertEqual(page, m.PAGE.replace("<title>Review ledger</title>", "<title>Review ledger: proj</title>"))
        with self.assertRaises(m.LedgerError):
            m.hub_page(self.L.root, self.L.db, page=m.PAGE.replace(marker, "<script>"))


class HubEscaping(unittest.TestCase):
    def test_hub_data_cannot_close_its_script(self):
        L = Ledger()
        self.addCleanup(L.repo.cleanup)
        self.addCleanup(L.close)
        evil = "</script><script>alert(1)</script><!--  "
        L.gate("GATE-0001", title=evil, question="Q " + evil)
        L.commit()
        html, n = L.module.hub_page(L.root, L.db)
        self.assertNotIn("alert(1)</script>", html)
        self.assertEqual(html.count("<script>"), 2)  # the data script and the page's own
        snap = snapshot_of(html)
        self.assertEqual(snap["responses"]["gates?status=pending"]["gates"][0]["title"], evil)


class ImportAnswers(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.repo.cleanup)
        self.addCleanup(self.L.close)
        opts = json.dumps([{"value": "a", "label": "Option A"}, {"value": "b", "label": "Option B"}])
        self.L.gate("GATE-0001", title="Pick one", options=opts, allow_text=0)
        self.L.gate("GATE-0002", title="Say something")
        self.L.gate("GATE-0003", title="Pick again", options=opts)
        self.L.commit()

    def write(self, data):
        p = self.L.root / "answers.json"
        p.write_text(json.dumps(data))
        return p

    def gate(self, code):
        return self.L.one("SELECT * FROM gates WHERE code=?", (code,))

    def test_import_answers_marks_gates_answered(self):
        L = self.L
        f = self.write([{"gate": "GATE-0001", "answer_value": "b", "answer_text": None,
                         "answered_at": "2026-11-11T10:00:00.000Z"},
                        {"id": "GATE-0002", "data": {"answer_value": None, "answer_text": "Ship it"}}])
        p = L.run_ok("import-answers", str(f))
        self.assertIn("GATE-0001: recorded", p.stdout)
        self.assertIn("GATE-0002: recorded", p.stdout)
        self.assertIn("import-answers: 2 recorded, 0 already answered, 0 refused", p.stdout)
        g = self.gate("GATE-0001")
        self.assertEqual((g["status"], g["answer_value"], g["answered_via"]), ("answered", "b", "viewer"))
        g = self.gate("GATE-0002")
        self.assertEqual((g["status"], g["answer_text"], g["answered_via"]), ("answered", "Ship it", "viewer"))
        self.assertEqual(self.gate("GATE-0003")["status"], "open")
        # importing the same file again changes nothing
        p = L.run_ok("import-answers", str(f))
        self.assertIn("GATE-0001: already answered or closed; left as it is", p.stdout)
        # a choice that is not an option, a gate that does not exist, and junk are refused (exit 1)
        f = self.write({"answers": {"GATE-0003": {"answer_value": "zzz"}, "GATE-0099": {"answer_text": "x"},
                                    "nonsense": {"answer_text": "x"}}})
        p = L.run("import-answers", str(f))
        self.assertEqual(p.returncode, 1)
        self.assertIn("GATE-0003: not recorded: That choice is not one of the options.", p.stdout)
        self.assertIn("GATE-0099: not recorded: no such gate", p.stdout)
        self.assertIn("nonsense: not an answer to a gate (skipped)", p.stdout)
        self.assertEqual(self.gate("GATE-0003")["status"], "open")
        p = L.run("import-answers", str(L.root / "missing.json"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot read", p.stderr)

    def test_hub_answers_round_trip(self):
        L = self.L
        # the hub's database holds one document per answered gate (what hubGateBody writes)
        docs = [{"id": "GATE-0003", "data": {"gate": "GATE-0003", "answer_value": "a", "answer_text": "because",
                                             "answered_at": "2026-11-11T10:00:00.000Z"}}]
        L.run_ok("import-answers", str(self.write(docs)))
        g = self.gate("GATE-0003")
        self.assertEqual((g["status"], g["answer_value"], g["answer_text"]), ("answered", "a", "because"))
        L.x("UPDATE gates SET status='applied', applied_note='Did A.' WHERE code='GATE-0003'")
        L.commit()
        html, _ = L.module.hub_page(L.root, L.db)
        R = snapshot_of(html)["responses"]
        allg = {g["code"]: g for g in R["gates?status=all"]["gates"]}
        self.assertEqual(allg["GATE-0003"]["status"], "applied")
        self.assertNotIn("GATE-0003", [g["code"] for g in R["gates?status=pending"]["gates"]])


if __name__ == "__main__":
    unittest.main()
