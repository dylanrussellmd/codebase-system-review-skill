"""The large pages' routes (plan P-0001 steps 148, 162, 166, 167, 168): a review as list rows with each point in
full on its own route, a plan as step rows with a search text and each step in full on its own route, an issue's
locations in one query, the reviews list with its tests count, and trends with tests and confirmed, kept until the
ledger changes."""
import sqlite3
import unittest

from dev.tests import _migrate_helpers as M
from dev.tests import fixtures
from dev.tests import test_routes as tr
from dev.tests.fixtures import Ledger, seed_demo


def build_extra(L):
    """The demo plus what the counts need: a missing-test point, a false finding, a point that came back, checks."""
    ids = seed_demo(L)
    r1, r2 = ids["reviews"]["R-0001"], ids["reviews"]["R-0002"]
    L.finding(r1, "AAA-T1", severity="T", kind="test", domain_code="tests", summary="A test gap.")
    f = L.finding(r2, "SEC-L9", severity="L", domain_code="security", summary="Not real after all.")
    L.update("findings", f, verification="false", verification_note="Misread.")
    L.review("R-0003", commit_sha=ids["commits"][1], repository_id=ids["repository"])
    back = L.finding(L.id_of("reviews", "R-0003"), "SPEC-L9", severity="L", domain_code="spec",
                     summary="Admin bypass again.", detail="The export path skips the check.")
    L.match(back, ids["issues"]["ISS-0001"], "recurrence")
    L.update("issues", ids["issues"]["ISS-0001"], status="regressed", change_review_id=L.id_of("reviews", "R-0003"))
    L.location(back, "src/export.py", 4, 6, repository_id=ids["repository"], symbol="export_all")
    return ids


class Case(tr.RouteCase):
    build = staticmethod(build_extra)

    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.ids = cls.build(cls.L)
        cls.L.commit()
        cls.mod = cls.L.module

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def traced(self, path, **qs):
        con, seen = self.mod.connect(self.L.db), []
        con.set_trace_callback(seen.append)
        try:
            res = self.mod.route(self.L.root, self.L.db, con, path, {k: [str(v)] for k, v in qs.items()}, None)
        finally:
            con.close()
        return res, [s for s in seen if s.lstrip().upper().startswith(("SELECT", "WITH"))]


class Review(Case):
    def test_review_route_sends_list_rows(self):
        d = self.ok("/api/review/R-0001")
        self.assertEqual(set(d["findings"][0]), {"id", "point_id", "severity", "domain_code",
                                                 "summary", "match_type", "verification", "issue_code"})
        self.assertEqual([f["point_id"] for f in d["findings"]], ["SEC-H1", "SPEC-M1", "SPEC-L1", "AAA-T1"])
        self.assertIn("check_counts", d)
        self.assertEqual(sum(c["n"] for c in d["check_counts"]), len(d["checks"]))
        # the per-domain summary equals the view it stands in for
        want = self.L.all("SELECT * FROM v_review_summary WHERE review='R-0001'")
        for w in want:   # step 172: the review's page shows each area's counts; the review itself is its page
            del w["review"], w["started_at"]
        self.assertEqual(d["summary"], want)

    def test_review_summary_equals_view(self):
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            self.assertEqual(self.mod.review_summary(con), self.L.all("SELECT * FROM v_review_summary"))
            for code in ("R-0001", "R-0002", "R-0003"):
                self.assertEqual(self.mod.review_summary(con, self.L.id_of("reviews", code)),
                                 self.L.all("SELECT * FROM v_review_summary WHERE review=?", (code,)), code)
        finally:
            con.close()
        self.assertGreater(sum(r["came_back"] for r in self.L.all("SELECT came_back FROM v_review_summary")), 0)

    def test_finding_route_returns_locations(self):
        fid = self.ids["findings"]["R-0001/SEC-H1"]
        f = self.ok(f"/api/finding/{fid}")
        self.assertEqual((f["point_id"], f["review_code"], f["issue_code"]), ("SEC-H1", "R-0001", "ISS-0001"))
        self.assertTrue(f["detail"] or f["before_state"] or f["recommendation"])
        self.assertEqual([(x["path"], x["line_start"], x["repo"]) for x in f["locations"]], [("src/auth.py", 1, "demo")])
        self.assertEqual(set(f["locations"][0]), {"id", "path", "line_start", "line_end", "symbol", "role",
                                                  "commit_sha", "repo"})
        self.assertIn("run_label", f)
        self.assertEqual(self.call("/api/finding/999999"), (404, {"error": "no such point"}))
        self.assertEqual(self.call("/api/finding/abc"), (400, {"error": "The point id must be a whole number."}))

    def test_review_search_on_the_server(self):
        ids = lambda code, q: sorted(self.ok(f"/api/review/{code}", q=q)["match"])  # noqa: E731
        by = {v: k.split("/")[1] for k, v in self.ids["findings"].items() if k.startswith("R-0001/")}
        names = lambda q: sorted(by.get(i, "?") for i in ids("R-0001", q))  # noqa: E731
        self.assertEqual(set(self.ok("/api/review/R-0001", q="auth")), {"match"})
        self.assertEqual(names("requests.py"), ["SPEC-L1", "SPEC-M1"])       # a cited path
        self.assertEqual(names("src/auth"), ["SEC-H1"])
        self.assertEqual(names("ISS-0003"), ["SPEC-L1"])                    # the issue's code
        self.assertEqual(names("sec-h1"), ["SEC-H1"])                       # the point id, any case
        self.assertEqual(names("%"), [])                                    # % and _ are themselves
        self.assertEqual(names("no such words"), [])
        self.assertEqual(len(ids("R-0003", "export path skips")), 1)        # the detail
        self.assertEqual(len(ids("R-0003", "export_all")), 1)               # a cited symbol
        self.assertEqual(ids("R-0001", "export_all"), [])                   # another review's point is not matched
        self.assertEqual(self.call("/api/review/R-9999", q="x")[0], 404)

    def test_review_checks_first_rows_then_pages(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = seed_demo(L)
        r3 = L.review("R-0003", commit_sha=ids["commits"][1], repository_id=ids["repository"])
        for n in range(120):
            i = L.issue(None, first_seen_review_id=ids["reviews"]["R-0001"], title=f"Old {n}")
            L.check_row(r3, i, "still-present" if n % 3 else "fixed", evidence="Looked.")
        L.commit()
        d = self.ok("/api/review/R-0003", L=L)
        self.assertEqual(len(d["checks"]), self.mod.REVIEW_CHECKS)
        self.assertEqual({c["outcome"]: c["n"] for c in d["check_counts"]}, {"fixed": 40, "still-present": 80})
        page = self.ok("/api/review/R-0003/checks", L=L, limit=100, offset=50)
        self.assertEqual((page["total"], page["limit"], page["offset"], len(page["rows"])), (120, 100, 50, 70))
        every = self.ok("/api/review/R-0003/checks", L=L, limit=500)["rows"]
        self.assertEqual(every[:50], d["checks"])
        self.assertEqual(every[50:], page["rows"])
        self.assertEqual(self.call("/api/review/R-9999/checks")[0], 404)
        # the hub's copy holds every row
        con = self.mod.connect(L.db, readonly=True)
        try:
            snap = self.mod.hub_snapshot(L.root, L.db, con)["responses"]
        finally:
            con.close()
        self.assertEqual(len(snap["review/R-0003/checks"]["rows"]), 120)
        self.assertEqual(len(snap["review/R-0003"]["checks"]), self.mod.REVIEW_CHECKS)
        self.assertEqual({k for k in snap if k.startswith("finding/")},
                         {f"finding/{r['id']}" for r in L.all("SELECT id FROM findings")})


class Issue(Case):
    def test_issue_route_constant_statements(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = seed_demo(L)
        before = self.ok("/api/issue/ISS-0002", L=L)
        self.L, keep = L, self.L
        try:
            _, few = self.traced("/api/issue/ISS-0002")
            r2 = ids["reviews"]["R-0002"]
            for n in range(50):
                f = L.finding(r2, f"SPEC-L{n + 20}", severity="L", domain_code="spec", summary=f"Again {n}.")
                L.match(f, ids["issues"]["ISS-0002"], "still-present")
                L.location(f, f"src/more{n}.py", 1, 2, repository_id=ids["repository"])
            L.commit()
            d, many = self.traced("/api/issue/ISS-0002")
        finally:
            self.L = keep
        self.assertEqual(len(many), len(few))
        self.assertEqual(len(d["findings"]), len(before["findings"]) + 50)
        self.assertEqual([f["locations"] for f in d["findings"][:len(before["findings"])]],
                         [f["locations"] for f in before["findings"]])
        self.assertEqual(set(before["findings"][0]["locations"][0]),
                         {"id", "path", "line_start", "line_end", "symbol", "role", "commit_sha", "repo"})
        self.assertEqual([loc["path"] for f in d["findings"][-2:] for loc in f["locations"]],
                         ["src/more48.py", "src/more49.py"])


class Lists(Case):
    def test_reviews_route_counts_match(self):
        got = {r["code"]: r for r in self.ok("/api/reviews")}
        old = self.L.all("""SELECT r.code,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.verification<>'false') points,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.verification<>'false' AND f.severity='H') high,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.verification<>'false' AND f.severity='M') medium,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.verification<>'false' AND f.severity='L') low,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.verification<>'false' AND f.severity='T') tests
            FROM reviews r""")
        self.assertEqual(len(got), len(old))
        for o in old:
            for k in ("points", "high", "medium", "low", "tests"):
                self.assertEqual(got[o["code"]][k], o[k], (o["code"], k))
        self.assertEqual(got["R-0001"]["tests"], 1)
        self.assertEqual(got["R-0003"]["came_back"], 1)
        # the counts can come from the index alone: it holds every findings column the view reads
        self.assertEqual([r[2] for r in self.L.con.execute("PRAGMA index_info(ix_findings_review_counts)")],
                         ["review_id", "verification", "severity", "match_type", "issue_id"])
        if sqlite3.sqlite_version_info >= (3, 39):   # an older planner picks its own way on a ledger this small
            plan = " ".join(r[3] for r in self.L.con.execute("EXPLAIN QUERY PLAN SELECT * FROM v_review_totals"))
            self.assertIn("ix_findings_review_counts", plan)

    def test_trends_has_tests_and_confirmed(self):
        d = self.ok("/api/trends")
        self.assertEqual(set(d), {"metrics", "reviews", "recurring", "hotspots"})
        self.assertEqual(set(d["reviews"][0]), {"review", "started_at", "points", "high", "medium", "low", "tests",
                                                "confirmed", "new_issues", "carried_over", "came_back", "false_findings"})
        want = self.L.all("""SELECT review, min(started_at) started_at, sum(points) points, sum(high) high,
            sum(medium) medium, sum(low) low, sum(tests) tests, sum(confirmed) confirmed, sum(new_issues) new_issues,
            sum(carried_over) carried_over, sum(came_back) came_back, sum(false_findings) false_findings
            FROM v_review_summary GROUP BY review ORDER BY started_at""")
        self.assertEqual(d["reviews"], want)
        by = {r["review"]: r for r in d["reviews"]}
        self.assertEqual((by["R-0001"]["tests"], by["R-0002"]["false_findings"], by["R-0003"]["came_back"]), (1, 1, 1))
        self.assertGreater(by["R-0001"]["confirmed"], 0)

    def test_trends_cache_invalidates(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = build_extra(L)
        L.commit()
        mod = fixtures.load_ledger_module(fresh=True)
        self.addCleanup(mod.CACHE.close)
        builds = []
        real = mod.review_summary
        mod.review_summary = lambda con, review_id=None: builds.append(1) or real(con, review_id)
        first = mod.api(L.root, L.db, "/api/trends", {}, None)
        again = mod.api(L.root, L.db, "/api/trends", {}, None)
        self.assertIs(again, first)                       # the kept answer
        self.assertEqual(len(builds), 1)
        L.finding(ids["reviews"]["R-0002"], "SPEC-L7", severity="L", domain_code="spec", summary="One more.")
        L.commit()                                        # a write by any other connection
        third = mod.api(L.root, L.db, "/api/trends", {}, None)
        self.assertEqual(len(builds), 2)
        by = lambda d: {r["review"]: r["points"] for r in d["reviews"]}  # noqa: E731
        self.assertEqual(by(third)["R-0002"], by(first)["R-0002"] + 1)
        self.assertIs(mod.api(L.root, L.db, "/api/trends", {}, None), third)
        # called outside the running viewer (the hub, a test): never from the cache
        con = mod.connect(L.db, readonly=True)
        try:
            mod.route(L.root, L.db, con, "/api/trends", {}, None)
        finally:
            con.close()
        self.assertEqual(len(builds), 3)
        self.assertEqual(mod.route_seconds("/api/trends"), mod.TRENDS_SECONDS)


class Plan(Case):
    def test_plan_route_sends_rows_and_one_phase_in_full(self):
        d = self.ok("/api/plan/P-0001")
        self.assertEqual(set(d), {"plan", "reviews", "artifacts", "last_updated", "gates", "discoveries", "events", "phases", "phase_rows", "steps", "unguarded"})
        row = {"number", "section", "title", "status", "owner_need", "behavior_change", "phase", "depends",
               "position", "closes", "search"}
        full = {"change", "proof", "owner_question", "behavior_note", "progress_note", "no_findings_reason",
                "started_at", "done_at", "commits"}
        by = {s["number"]: s for s in d["steps"]}
        self.assertEqual(set(by[1]), row | full)            # phase 1, the default, in full
        self.assertEqual(set(by[2]), row)
        self.assertEqual(set(by[3]), row)
        two = {s["number"]: s for s in self.ok("/api/plan/P-0001", phase=2)["steps"]}
        self.assertEqual((set(two[1]), set(two[2]), set(two[3])), (row, row | full, row | full))
        f = self.ids["findings"]
        self.assertEqual(by[2]["closes"], [f["R-0001/SEC-H1"]])
        self.assertEqual(by[3]["depends"], [1])
        self.assertEqual(self.call("/api/plan/P-0001", phase="x")[0], 400)

    def test_plan_search_text_covers_every_phase(self):
        d = self.ok("/api/plan/P-0001")
        for s in d["steps"]:
            text = s["search"]
            self.assertEqual(text, text.lower())
            self.assertLessEqual(len(text), self.mod.STEP_SEARCH)
            full = self.ok(f"/api/step/P-0001/{s['number']}")
            for part in (str(s["number"]), s["title"], full["change"], full["proof"]):
                self.assertIn(part.lower(), text, s["number"])
            for c in full["closes"]:
                self.assertIn(c["point_id"].lower(), text)
                self.assertIn((c["issue"] or "").lower(), text)
        long = Ledger()
        self.addCleanup(long.close)
        ids = seed_demo(long)
        long.update("steps", ids["steps"][2], change="word " * 2000)
        long.commit()
        s2 = [s for s in self.ok("/api/plan/P-0001", L=long)["steps"] if s["number"] == 2][0]
        self.assertEqual(len(s2["search"]), self.mod.STEP_SEARCH)
        self.assertIn("sec-h1", s2["search"])               # ids and proof come before the long change text

    def test_step_route(self):
        s = self.ok("/api/step/P-0001/2")
        self.assertEqual(set(s), {"number", "change", "proof", "owner_question", "behavior_note", "progress_note",
                                  "no_findings_reason", "started_at", "done_at", "commits", "closes"})
        self.assertEqual([(c["point_id"], c["review"], c["issue"], c["severity"]) for c in s["closes"]],
                         [("SEC-H1", "R-0001", "ISS-0001", "H")])
        self.assertTrue(s["closes"][0]["summary"])
        self.assertEqual(len(s["commits"]), self.L.scalar(
            "SELECT count(*) FROM step_commits WHERE step_id=?", (self.ids["steps"][2],)))
        self.assertEqual(self.call("/api/step/P-0001/99"), (404, {"error": "no such step"}))
        self.assertEqual(self.call("/api/step/P-9999/1"), (404, {"error": "no such step"}))
        self.assertEqual(self.call("/api/step/P-0001/two")[0], 400)

    def test_hub_plan_is_whole(self):
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            snap = self.mod.hub_snapshot(self.L.root, self.L.db, con)["responses"]
        finally:
            con.close()
        plan = snap["plan/P-0001"]
        self.assertTrue(all("change" in s and "commits" in s for s in plan["steps"]))
        closed = {i for s in plan["steps"] for i in s["closes"]}
        self.assertEqual(set(plan["findings"]), closed)
        self.assertFalse([k for k in snap if k.startswith("step/")])


class Migration(unittest.TestCase):
    def test_migrate_adds_the_counts_index(self):
        project_dir, db = M.restore_v1()
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        c = sqlite3.connect(str(db))
        try:
            self.assertEqual(c.execute("SELECT tbl_name FROM sqlite_master WHERE name='ix_findings_review_counts'")
                             .fetchone(), ("findings",))
            self.assertEqual(fixtures.load_ledger_module().verify_schema(c), [])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
