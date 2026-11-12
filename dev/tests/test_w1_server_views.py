"""The viewer reads its counts from the schema's views (plan P-0001: ABS-M7, DOC-X8, TEST-T81, TEST-T181).

test_v_open_issues                 only the active statuses; H, M, L, T then first seen; primary locations only
test_issues_active_filter          /api/issues: status=active, exact filters, q over six fields, filters ANDed, order
test_v_plan_totals                 one row per plan; 0, never NULL, for a plan with no steps
test_v_review_totals               one row per review; real findings only; came_back from match_types.came_back
test_v_pending_gates               open and answered gates, the ones still to answer first
test_overview_counts_equal_views   /api/overview, /api/reviews, /api/plans and /api/gates numbers are the views' own
test_migrate_adds_totals_views     a ledger of an older schema gains the three views and matches the shipped schema
"""
import re
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, seed_demo

ACTIVE = ("open", "needs-decision", "planned", "in-progress", "regressed")
SEVERITY = {"H": 0, "M": 1, "L": 2, "T": 3}
_made = {}


def demo():
    if "demo" not in _made:
        L = Ledger()
        _made["demo"] = (L, seed_demo(L))
    return _made["demo"]


def extra():
    """The demo plus issues in every status, a second-role location, a false finding, a recurrence, gates in every
    status and a plan with no steps. Shared; tests do not write to it."""
    if "extra" in _made:
        return _made["extra"]
    L = Ledger()
    ids = seed_demo(L)
    r1, r2 = ids["reviews"]["R-0001"], ids["reviews"]["R-0002"]
    for code, sev, status in (("ISS-0005", "H", "needs-decision"), ("ISS-0006", "H", "in-progress"),
                              ("ISS-0007", "T", "planned"), ("ISS-0008", "T", "regressed"),
                              ("ISS-0009", "M", "accepted-risk"), ("ISS-0010", "M", "wont-fix"),
                              ("ISS-0011", "L", "duplicate"), ("ISS-0012", "L", "obsolete"),
                              ("ISS-0013", "H", "fixed")):
        L.issue(code, first_seen_review_id=r2 if code != "ISS-0006" else r1, severity=sev, status=status,
                title=f"Extra {status}")
    # ISS-0005: a real finding with a primary and an 'also' location, and a false finding with its own primary
    real = L.finding(r2, "ZZZ-H1", severity="H", domain_code="spec", summary="Real one.")
    L.location(real, "src/primary.py", 3, 4, repository_id=ids["repository"], symbol="main")
    L.location(real, "src/also.py", 1, 1, repository_id=ids["repository"], role="also")
    L.match(real, L.id_of("issues", "ISS-0005"), "still-present")
    false = L.finding(r2, "ZZZ-M2", severity="M", domain_code="spec", summary="Not real.", verification="false")
    L.location(false, "src/false.py", 1, 1, repository_id=ids["repository"])
    L.match(false, L.id_of("issues", "ISS-0012"), "still-present")
    r3 = L.review("R-0003", commit_sha=ids["commits"][1], repository_id=ids["repository"])
    back = L.finding(r3, "SPEC-L9", severity="L", domain_code="spec", summary="Back again.")
    L.match(back, ids["issues"]["ISS-0001"], "recurrence")
    L.finding(r3, "AAA-T1", severity="T", kind="test", domain_code="spec", summary="A test gap.")
    L.review("R-0004", commit_sha=ids["commits"][1], repository_id=ids["repository"])  # no findings
    L.gate("GATE-0003", "decision", "Answered one", options=[{"value": "a", "label": "A"}], status="answered",
           answer_value="a", answered_by="owner", answered_via="chat")
    L.gate("GATE-0004", "other", "Open, not blocking", blocking=0)
    L.gate("GATE-0005", "decision", "Applied one", status="applied", answer_value="x")
    L.gate("GATE-0006", "decision", "Withdrawn one", status="withdrawn")
    L.plan("P-0002", "Empty plan")
    L.commit()
    _made["extra"] = (L, ids)
    return _made["extra"]


def tearDownModule():
    for L, _ in _made.values():
        L.close()


def api(L, path, **qs):
    mod = L.module
    con = mod.connect(L.db, readonly=True)
    try:
        res = mod.route(L.root, L.db, con, path, {k: [str(v)] for k, v in qs.items()}, None)
    finally:
        con.close()
    assert not isinstance(res, tuple), f"{path}: {res}"
    return res


class OpenIssues(unittest.TestCase):
    def test_v_open_issues(self):
        L, _ = extra()
        got = L.all("SELECT * FROM v_open_issues")
        issues = {r["code"]: r for r in L.all(
            """SELECT i.code, i.status, i.severity, r.started_at FROM issues i
               JOIN reviews r ON r.id = i.first_seen_review_id""")}
        self.assertEqual({r["code"] for r in got}, {c for c, i in issues.items() if i["status"] in ACTIVE})
        self.assertEqual({r["status"] for r in got}, set(ACTIVE), "the ledger has an issue in each active status")
        self.assertEqual(len(issues) - len(got), 6)  # verified, accepted-risk, wont-fix, duplicate, obsolete, fixed
        keys = [(SEVERITY[r["severity"]], r["first_seen_at"]) for r in got]
        self.assertEqual(keys, sorted(keys), "most severe first, then first seen")
        self.assertEqual([r["severity"] for r in got], sorted((r["severity"] for r in got), key=SEVERITY.get))
        self.assertEqual([r["code"] for r in got if r["severity"] == "H"], ["ISS-0006", "ISS-0005"])  # R-0001 first
        by = {r["code"]: r for r in got}
        self.assertEqual(by["ISS-0005"]["locations"], "src/primary.py#main")  # not src/also.py
        self.assertIsNone(by["ISS-0006"]["locations"])
        for r in got:
            self.assertNotIn("false.py", r["locations"] or "")


class IssuesRoute(unittest.TestCase):
    def codes(self, L=None, **qs):
        return [r["code"] for r in api(L or demo()[0], "/api/issues", **qs)["rows"]]

    def test_issues_active_filter(self):
        L, _ = extra()
        rows = L.all("SELECT code, status, severity, domain_code FROM issues")
        self.assertEqual(sorted(self.codes(L, status="active")),
                         sorted(r["code"] for r in rows if r["status"] in ACTIVE))
        self.assertEqual(self.codes(L, status="active"),
                         [r["code"] for r in api(L, "/api/issues")["rows"] if r["status"] in ACTIVE])
        for status in ACTIVE + ("fixed", "obsolete"):
            self.assertEqual(sorted(self.codes(L, status=status)),
                             sorted(r["code"] for r in rows if r["status"] == status), status)
        # exact, never a prefix
        self.assertEqual(self.codes(status="verified"), ["ISS-0001"])
        self.assertEqual(self.codes(status="ver"), [])
        self.assertEqual(self.codes(severity="M"), ["ISS-0002", "ISS-0004"])
        self.assertEqual(self.codes(severity="m"), [])
        self.assertEqual(self.codes(domain_code="spec"), ["ISS-0002", "ISS-0003"])
        self.assertEqual(self.codes(domain_code="spe"), [])
        # q: code, title, sub, a finding's point id and summary, a location's path
        self.assertEqual(self.codes(q="ISS-0003"), ["ISS-0003"])
        self.assertEqual(self.codes(q="hardcoded"), ["ISS-0002"])
        self.assertEqual(self.codes(q="code-compare"), ["ISS-0004"])
        self.assertEqual(self.codes(q="SPEC-L1"), ["ISS-0003"])
        self.assertEqual(self.codes(q="random generator"), ["ISS-0001"])
        self.assertEqual(self.codes(q="requests.py"), ["ISS-0002", "ISS-0003"])
        self.assertEqual(self.codes(q="no such words"), [])
        # filters are ANDed
        self.assertEqual(self.codes(status="open", domain_code="security"), ["ISS-0004"])
        self.assertEqual(self.codes(status="active", severity="L"), ["ISS-0003"])
        self.assertEqual(self.codes(q="sign-in", status="verified"), ["ISS-0001"])
        self.assertEqual(self.codes(q="auth.py", domain_code="spec"), [])
        # order: H, M, L, T, then the order the issues were opened
        self.assertEqual(self.codes(), ["ISS-0001", "ISS-0002", "ISS-0004", "ISS-0003"])
        sev = [SEVERITY[r["severity"]] for r in api(L, "/api/issues")["rows"]]
        self.assertEqual(sev, sorted(sev))


class Totals(unittest.TestCase):
    def test_v_plan_totals(self):
        L, _ = extra()
        got = {r["plan"]: r for r in L.all("SELECT * FROM v_plan_totals")}
        self.assertEqual(set(got), {"P-0001", "P-0002"})
        keys = ("steps", "done", "doing", "waiting_owner", "not_started", "dropped")
        self.assertEqual({k: got["P-0002"][k] for k in keys}, dict.fromkeys(keys, 0))
        steps = [r["status"] for r in L.all(
            "SELECT s.status FROM steps s JOIN plans p ON p.id = s.plan_id WHERE p.code = 'P-0001'")]
        self.assertEqual({k: got["P-0001"][k] for k in keys},
                         {"steps": len(steps), "done": steps.count("done"), "doing": steps.count("doing"),
                          "waiting_owner": steps.count("waiting-owner"), "not_started": steps.count("not-started"),
                          "dropped": steps.count("dropped")})
        # the same numbers as the per-phase view, summed
        for k in keys:
            self.assertEqual(got["P-0001"][k], L.scalar(f"SELECT sum({k}) FROM v_plan_progress WHERE plan='P-0001'"))
        p = L.one("SELECT title, status, created_at, approved_at FROM plans WHERE code='P-0001'")
        self.assertEqual({k: got["P-0001"][k] for k in p}, p)
        self.assertEqual(got["P-0001"]["reviews"], "R-0001")
        self.assertIsNone(got["P-0002"]["reviews"])

    def test_v_review_totals(self):
        L, _ = extra()
        got = {r["review"]: r for r in L.all("SELECT * FROM v_review_totals")}
        self.assertEqual(set(got), {r["code"] for r in L.all("SELECT code FROM reviews")})
        came_back = {r["value"] for r in L.all("SELECT value FROM match_types WHERE came_back = 1")}
        self.assertIn("recurrence", came_back)
        for code, t in got.items():
            f = L.all("""SELECT f.severity, f.match_type FROM findings f JOIN reviews r ON r.id = f.review_id
                         WHERE r.code = ? AND f.verification <> 'false'""", (code,))
            want = {"points": len(f), "high": sum(x["severity"] == "H" for x in f),
                    "medium": sum(x["severity"] == "M" for x in f), "low": sum(x["severity"] == "L" for x in f),
                    "tests": sum(x["severity"] == "T" for x in f),
                    "new_issues": sum(x["match_type"] in ("new", "continuation") for x in f),
                    "came_back": sum(x["match_type"] in came_back for x in f)}
            self.assertEqual({k: t[k] for k in want}, want, code)
        self.assertEqual(got["R-0004"]["points"], 0)  # 0, not NULL, for a review with no findings
        self.assertEqual((got["R-0003"]["points"], got["R-0003"]["came_back"], got["R-0003"]["tests"]), (2, 1, 1))
        # R-0002's false finding is not a point
        self.assertEqual(got["R-0002"]["points"], L.count("findings", "review_id=?", (L.id_of("reviews", "R-0002"),)) - 1)
        self.assertEqual([r["review"] for r in L.all("SELECT review FROM v_review_totals WHERE review = 'R-0003'")],
                         ["R-0003"])

    def test_v_pending_gates(self):
        L, _ = extra()
        got = L.all("SELECT * FROM v_pending_gates")
        self.assertEqual([(g["code"], g["status"]) for g in got],
                         [("GATE-0001", "open"), ("GATE-0002", "open"), ("GATE-0004", "open"),
                          ("GATE-0003", "answered")])
        self.assertEqual({g["code"] for g in got},
                         {r["ref"] for r in L.all("SELECT ref FROM v_owner_queue WHERE kind = 'gate'")})
        g1 = L.one("SELECT g.id, g.kind, g.title, g.blocking, r.code review_code FROM gates g "
                   "LEFT JOIN reviews r ON r.id = g.review_id WHERE g.code = 'GATE-0001'")
        self.assertEqual({k: got[0][k] for k in g1}, g1)

    def test_overview_counts_equal_views(self):
        for L in (demo()[0], extra()[0]):
            o = {**api(L, "/api/overview"), **api(L, "/api/home")}
            totals = {r["review"]: r for r in L.all("SELECT * FROM v_review_totals")}
            self.assertEqual([r["code"] for r in o["reviews"]],
                             [r["code"] for r in L.all("SELECT code FROM reviews ORDER BY started_at DESC LIMIT 5")])
            for r in o["reviews"]:
                self.assertEqual((r["points"], r["high"]), (totals[r["code"]]["points"], totals[r["code"]]["high"]))
            reviews = api(L, "/api/reviews")
            self.assertEqual(len(reviews), len(totals))
            for r in reviews:
                for k in ("points", "high", "medium", "low", "new_issues", "came_back"):
                    self.assertEqual(r[k], totals[r["code"]][k], (r["code"], k))
            plans = {r["plan"]: r for r in L.all("SELECT * FROM v_plan_totals")}
            for rows in (o["plans"], api(L, "/api/plans")):
                self.assertEqual(len(rows), len(plans))
                for p in rows:
                    t = plans[p["code"]]
                    self.assertEqual((p["steps"], p["done"], p["waiting"]), (t["steps"], t["done"], t["waiting_owner"]))
            for p in api(L, "/api/plans"):
                self.assertEqual((p["doing"], p["reviews"]), (plans[p["code"]]["doing"], plans[p["code"]]["reviews"]))
            self.assertEqual(o["queue_count"], L.count("v_owner_queue", "NOT (kind = 'gate' AND status = 'answered')"))
            self.assertEqual([g["code"] for g in api(L, "/api/gates")["gates"]],
                             [r["code"] for r in L.all("SELECT code FROM v_pending_gates")])
            self.assertEqual(o["queue_count"] + o["answered_count"], L.count("v_owner_queue"))
            self.assertEqual(sum(r["n"] for r in o["open_by_severity"]), L.count("v_open_issues"))
            for r in o["open_by_severity"]:
                self.assertEqual(r["n"], L.count("v_open_issues", "severity = ?", (r["severity"],)))
            self.assertEqual(sorted(r["code"] for r in api(L, "/api/issues", status="active")["rows"]),
                             sorted(r["code"] for r in L.all("SELECT code FROM v_open_issues")))

    def test_core_names_the_active_statuses_once(self):
        """DOC-X8: the viewer's SQL never spells the active statuses out; it reads issue_statuses.active."""
        core = (fixtures.REPO_ROOT / "dev" / "core.py").read_text(encoding="utf-8")
        a, b = core.index("def enum_labels("), core.index("def serve(")
        spelled = re.findall(r"""['"](?:needs-decision|in-progress|regressed)['"]""", core[a:b])
        self.assertEqual(spelled, [])


class Migration(unittest.TestCase):
    def test_migrate_adds_totals_views(self):
        project_dir, db = M.restore_v1()
        ledger_py = fixtures.built_ledger_path()
        p = fixtures.run_ledger("--root", project_dir, "migrate", ledger_py=ledger_py)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0010-totals-views", p.stdout)
        mod = fixtures.load_ledger_module(ledger_py)
        c = sqlite3.connect(str(db))
        try:
            self.assertEqual(mod.verify_schema(c), [])
            self.assertGreaterEqual(c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 10)
            n = c.execute("SELECT count(*) FROM reviews").fetchone()[0]
            self.assertGreater(n, 0)
            self.assertEqual(c.execute("SELECT count(*) FROM v_review_totals").fetchone()[0], n)
            self.assertEqual(c.execute("SELECT sum(points) FROM v_review_totals").fetchone()[0],
                             c.execute("SELECT count(*) FROM findings WHERE verification <> 'false'").fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM v_plan_totals").fetchone()[0],
                             c.execute("SELECT count(*) FROM plans").fetchone()[0])
            self.assertEqual(c.execute("SELECT sum(steps) FROM v_plan_totals").fetchone()[0],
                             c.execute("SELECT count(*) FROM steps").fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM v_pending_gates").fetchone()[0],
                             c.execute("SELECT count(*) FROM gates WHERE status IN ('open','answered')").fetchone()[0])
        finally:
            c.close()
        p = fixtures.run_ledger("--root", project_dir, "check", ledger_py=ledger_py)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
