"""The viewer's header, checks, Explore lists, decisions and POST rules (plan P-0001 steps 161, 165, 169, 177, 189,
190): a cheap overview with the counts behind the badge, the ledger's checks on their own route, tables listed with
plain names and descriptions, views not counted until asked, BLOBs sent as their size, and the owner's decisions."""
import json
import re
import sqlite3
import unittest

from dev.tests import _migrate_helpers as M
from dev.tests import fixtures
from dev.tests import test_routes as tr
from dev.tests.fixtures import Ledger, ServerThread, seed_demo


class Case(tr.RouteCase):
    @classmethod
    def build(cls, L):
        return seed_demo(L)

    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.ids = cls.build(cls.L)
        cls.L.commit()
        cls.mod = cls.L.module

    @classmethod
    def tearDownClass(cls):
        cls.L.close()


class Header(Case):
    @classmethod
    def build(cls, L):
        ids = seed_demo(L)
        L.gate("GATE-0003", options=[{"value": "a", "label": "A"}], status="answered", answer_value="a")
        L.gate("GATE-0004", status="applied")
        L.gate("GATE-0005", status="withdrawn")
        L.update("steps", ids["steps"][1], status="waiting-owner")           # a waiting step with no gate
        L.decision("D-0002", "Keep weekends off?", status="open")
        L.decision("D-0003", "Look again later?", status="revisit", revisit_when="after the next release")
        return ids

    def test_overview_is_the_header(self):
        d = self.ok("/api/overview")
        self.assertEqual(set(d), {"project", "schema_version", "queue_count", "answered_count", "gates_open", "labels"})
        self.assertEqual(set(d["labels"]), {"domains", "themes", "enums"})

    def test_overview_queue_counts(self):
        d = self.ok("/api/overview")
        # two open gates, the waiting step, an open decision and one to look at again; the answered gate apart
        self.assertEqual((d["queue_count"], d["answered_count"], d["gates_open"]), (5, 1, 2))
        q = self.L.all("SELECT kind, status FROM v_owner_queue")
        self.assertEqual(d["queue_count"] + d["answered_count"], len(q))
        e = self.ok("/api/overview", L=self._empty())
        self.assertEqual((e["queue_count"], e["answered_count"], e["gates_open"]), (0, 0, 0))

    def _empty(self):
        L = Ledger()
        self.addCleanup(L.close)
        return L

    def test_overview_plans_include_doing(self):
        was = self.L.scalar("SELECT status FROM steps WHERE id=?", (self.ids["steps"][1],))
        self.L.update("steps", self.ids["steps"][1], status="doing")
        self.L.commit()
        try:
            d = self.ok("/api/home")
            self.assertEqual(set(d), {"open_by_severity", "issues_by_status", "reviews", "plans", "discoveries"})
            plan = d["plans"][0]
            self.assertEqual(set(plan), {"code", "title", "status", "steps", "done", "doing", "waiting"})
            listed = self.ok("/api/plans")[0]
            self.assertEqual([plan[k] for k in ("steps", "done", "doing", "waiting")],
                             [listed[k] for k in ("steps", "done", "doing", "waiting")])
            self.assertEqual(plan["doing"], 1)
        finally:
            self.L.update("steps", self.ids["steps"][1], status=was)
            self.L.commit()

    def test_checks_route_matches_check(self):
        d = self.ok("/api/checks")
        self.assertEqual(set(d), {"checks", "recurring"})
        want = [(r["view"], r["label"], r["tier"], self.L.count(r["view"]))
                for r in self.L.all("SELECT view, label, tier FROM ledger_checks ORDER BY position, view")]
        self.assertEqual([(c["view"], c["label"], c["tier"], c["n"]) for c in d["checks"]], want)
        self.assertEqual(d["recurring"], self.L.count("v_recurring_issues"))
        out = self.L.run("check").stdout
        for view, label, tier, n in want:  # the same counts `ledger.py check` prints
            if n:
                self.assertRegex(out, rf"(?m)^\s*{n}  {re.escape(label)}  \({view}\)")
            else:
                self.assertNotRegex(out, rf"\({view}\)")
        self.assertEqual(self.mod.route_seconds("/api/checks"), self.mod.CHECKS_SECONDS)
        self.assertEqual(self.mod.route_seconds("/api/overview"), self.mod.ROUTE_SECONDS)


class Explore(Case):
    def test_explore_lists_descriptions(self):
        tables = self.ok("/api/tables")
        names = {r[0] for r in self.L.con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual({t["name"] for t in tables}, names)
        for t in tables:
            self.assertEqual(set(t) - {"count"}, {"name", "type", "label", "description", "for_agent"}, t["name"])
            self.assertRegex(t["description"], r"^[A-Z].*\.$", t["name"])
            self.assertNotIn("_", t["label"], t["name"])
            self.assertEqual("count" in t, t["type"] == "table", t["name"])
        by = {t["name"]: t for t in tables}
        self.assertEqual((by["v_owner_queue"]["label"], by["v_owner_queue"]["description"]),
                         ("Owner queue", "Everything waiting on the owner."))
        self.assertEqual(by["issues"]["count"], 4)
        self.assertEqual({n for n, t in by.items() if t["for_agent"]},
                         {"v_open_issues", "v_known_issues_for_brief", "v_code_refs"})

    def traced(self, path, **qs):
        con = self.mod.connect(self.L.db)
        seen = []
        con.set_trace_callback(seen.append)
        try:
            res = self.mod.route(self.L.root, self.L.db, con, path, {k: [str(v)] for k, v in qs.items()}, None)
        finally:
            con.close()
        return res, seen

    def test_table_route_no_view_count(self):
        res, seen = self.traced("/api/table/v_issue_timeline")
        self.assertFalse([s for s in seen if "count(*)" in s.lower()], seen)
        self.assertIsNone(res["count"])
        self.assertEqual(set(res), {"columns", "rows", "count", "more", "truncated", "offset", "limit"})
        self.assertFalse(res["more"])
        res, seen = self.traced("/api/table/v_issue_timeline", limit=2)
        self.assertEqual((len(res["rows"]), res["more"], res["count"]), (2, True, None))
        res, seen = self.traced("/api/table/v_issue_timeline", limit=2, count=1)   # counted when asked
        self.assertEqual(res["count"], self.L.count("v_issue_timeline"))
        res, seen = self.traced("/api/table/issues", limit=3)
        self.assertEqual((res["count"], res["more"], len(res["rows"])), (4, True, 3))
        self.assertFalse(self.traced("/api/table/issues", limit=3, offset=3)[0]["more"])
        _, seen = self.traced("/api/tables")
        counted = [s for s in seen if "count(*)" in s.lower()]
        self.assertTrue(counted)
        self.assertFalse([s for s in counted if '"v_' in s], "no view is counted for the list")

    def test_table_cells_follow_the_sql_box_limits(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = seed_demo(L)
        big = "x" * (L.module.SQL_MAX_CELL + 500)
        L.update("issues", ids["issues"]["ISS-0001"], current_state=big)
        L.commit()
        d = self.ok("/api/table/issues", L=L)
        cell = d["rows"][0][d["columns"].index("current_state")]
        self.assertLess(len(cell), len(big))
        self.assertIn("cut:", cell)
        self.assertFalse(d["truncated"])


class Blobs(unittest.TestCase):
    def test_blob_values_encoded(self):
        L = Ledger()
        self.addCleanup(L.close)
        seed_demo(L)
        L.x("CREATE TABLE scratch_blobs (id INTEGER PRIMARY KEY, data BLOB)")
        L.x("INSERT INTO scratch_blobs(data) VALUES (?)", (b"\x00\x01binary\xff" * 10,))
        L.commit()
        with ServerThread(L) as srv:
            d = srv.get("/api/table/scratch_blobs").json()
            self.assertEqual(d["rows"], [[1, {"blob_bytes": 90}]])
            s = srv.post("/api/sql", {"sql": "SELECT data FROM scratch_blobs"}).json()
            self.assertEqual(s["rows"], [[{"blob_bytes": 90}]])
        # a BLOB anywhere in an answer is the same marker, never Python's repr of the bytes
        self.assertEqual(json.loads(json.dumps({"a": [b"abc"]}, default=L.module._json_cell)), {"a": [{"blob_bytes": 3}]})

    def test_post_requires_json(self):
        L = Ledger()
        self.addCleanup(L.close)
        seed_demo(L)
        L.commit()
        body = b'{"sql": "SELECT 1 AS one"}'
        with ServerThread(L) as srv:
            for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", "", "json",
                          "application/jsonx", "text/json"):
                r = srv.request("POST", "/api/sql", body=body, headers={"Content-Type": ctype})
                self.assertEqual((r.status, r.json()), (415, {"error": "JSON only"}), ctype)
            for ctype in ("application/json", "application/json; charset=utf-8", "Application/JSON",   # ISS fixed
                          "APPLICATION/JSON;charset=UTF-8", " application/json ;charset=utf-8"):
                r = srv.request("POST", "/api/sql", body=body, headers={"Content-Type": ctype})
                self.assertEqual((r.status, r.json().get("rows")), (200, [[1]]), ctype)

    test_content_type_case_insensitive = test_post_requires_json


class Decisions(Case):
    @classmethod
    def build(cls, L):
        ids = seed_demo(L)
        L.decision("D-0002", "Ship on Fridays?", status="decided", answer="No.", rationale="Nobody is around.",
                   decided_at="2026-11-03T10:00:00.000Z")       # decided, tied to no issue
        L.decision("D-0003", "Keep weekends off?", status="open")
        L.decision("D-0004", "Is the cache worth it?", status="revisit", revisit_when="after the next release")
        return ids

    def test_decisions_listed_without_issue_link(self):
        d = self.ok("/api/decisions")["decisions"]
        by = {x["code"]: x for x in d}
        self.assertEqual(set(by), {"D-0001", "D-0002", "D-0003", "D-0004"})
        self.assertEqual((by["D-0002"]["answer"], by["D-0002"]["issues"], by["D-0002"]["status"]),
                         ("No.", None, "decided"))
        self.assertEqual(set(d[0]), {"code", "question", "answer", "rationale", "status", "revisit_when", "asked_at",
                                     "decided_at", "review", "issues"})
        # the ones still to settle come first
        self.assertEqual({x["code"] for x in d[:2]}, {"D-0003", "D-0004"})
        self.assertEqual(by["D-0004"]["revisit_when"], "after the next release")
        standing = {r["code"] for r in self.L.all("SELECT code FROM v_standing_decisions")}
        self.assertEqual({x["code"] for x in d if x["status"] == "decided"}, standing)

    def test_owner_queue_decision_columns(self):
        q = {r["ref"]: r for r in self.L.all("SELECT * FROM v_owner_queue")}
        self.assertEqual(list(q["D-0003"]), ["kind", "ref", "need", "title", "question", "status", "blocking",
                                             "created_at", "context"])
        self.assertEqual((q["D-0003"]["question"], q["D-0003"]["context"], q["D-0003"]["title"]),
                         ("Keep weekends off?", None, "A decision to make"))
        self.assertEqual((q["D-0004"]["question"], q["D-0004"]["context"], q["D-0004"]["title"]),
                         ("Is the cache worth it?", "after the next release", "A decision to look at again"))
        gate = q["GATE-0001"]
        self.assertEqual(gate["context"], self.L.scalar("SELECT context FROM gates WHERE code='GATE-0001'"))
        other = {o["ref"]: o for o in self.ok("/api/gates")["other"]}
        self.assertEqual(other["D-0004"]["context"], "after the next release")
        out = self.L.run_ok("gates").stdout
        self.assertIn("decision D-0003 [open] Keep weekends off?", out)
        self.assertIn("decision D-0004 [revisit] Is the cache worth it? (revisit when: after the next release)", out)

    def test_issue_route_returns_unguarded_fixes(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = seed_demo(L)
        L.commit()
        self.assertEqual(self.ok("/api/issue/ISS-0001", L=L)["unguarded"], [])     # verified, with a guard test
        self.assertEqual(self.ok("/api/plan/P-0001", L=L)["unguarded"], [])
        L.x("UPDATE guard_tests SET still_exists = 0")
        L.commit()
        self.assertEqual(self.ok("/api/issue/ISS-0001", L=L)["unguarded"], [{"problem": "guard test removed"}])
        self.assertEqual(self.ok("/api/issue/ISS-0002", L=L)["unguarded"], [])     # not fixed: needs none yet
        plan = self.ok("/api/plan/P-0001", L=L)["unguarded"]
        self.assertEqual([(u["code"], u["problem"]) for u in plan], [("ISS-0001", "guard test removed")])
        self.assertEqual(plan[0]["step"], L.scalar("""SELECT min(s.number) FROM steps s JOIN step_findings sf
            ON sf.step_id=s.id JOIN findings f ON f.id=sf.finding_id WHERE f.issue_id=?""", (ids["issues"]["ISS-0001"],)))
        # relations still read both ways (now through v_issue_graph)
        rel = self.ok("/api/issue/ISS-0001", L=L)["relations"]
        self.assertEqual([(r["dir"], r["relation"], r["other"]) for r in rel], [("in", "related-to", "ISS-0004")])


class Migration(unittest.TestCase):
    def test_migrate_gives_owner_queue_its_context(self):
        project_dir, db = M.restore_v1()
        ledger_py = fixtures.built_ledger_path()
        p = fixtures.run_ledger("--root", project_dir, "migrate", ledger_py=ledger_py)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0011-viewer-wave", p.stdout)
        mod = fixtures.load_ledger_module(ledger_py)
        c = sqlite3.connect(str(db))
        try:
            self.assertEqual(mod.verify_schema(c), [])
            cols = [d[0] for d in c.execute("SELECT * FROM v_owner_queue LIMIT 0").description]
            self.assertEqual(cols[-1], "context")
            c.execute("INSERT INTO decisions(code, question, status, revisit_when) VALUES "
                      "((SELECT 'D-' || printf('%04d', count(*) + 1) FROM decisions), 'Again?', 'revisit', 'in spring')")
            row = c.execute("SELECT question, context FROM v_owner_queue WHERE kind='decision' "
                            "AND question='Again?'").fetchone()
            self.assertEqual(row, ("Again?", "in spring"))
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='tr_gate_answer_rules'").fetchone()[0]
            self.assertIn("10000", sql)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
