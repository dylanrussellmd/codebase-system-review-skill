"""The viewer's API routes, called through route() with a connection (plan P-0001 steps 19 and 20).

Three ledgers: the demo (seed_demo), an "extra" ledger (the demo plus gates in every status, owner-queue items
without a gate, more reviews, an empty plan, a step with two dependencies ...), and an empty one. Bulk data for the
list caps is made in its own ledger.

These are characterization tests: they assert today's behaviour. Where a review point recorded a probed defect, the
assertion pins the current outcome under a '# characterization: ISS-xxxx' comment, so the step that fixes it flips
that assertion on purpose.
"""
import re
import unittest

from dev.tests import _w2_shown_helpers, fixtures
from dev.tests.fixtures import Ledger, seed_demo

PAGE_HTML = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")
ACTIVE = ("open", "needs-decision", "planned", "in-progress", "regressed")

_cache = {}


def demo():
    """(ledger, ids) for the plain demo; shared and never written to by tests."""
    if "demo" not in _cache:
        L = Ledger()
        _cache["demo"] = (L, seed_demo(L))
    return _cache["demo"]


def extra():
    """(ledger, ids, made) for the demo plus extra rows; shared and never written to by tests."""
    if "extra" in _cache:
        return _cache["extra"]
    L = Ledger()
    ids = seed_demo(L)
    made = {}
    r1, r2 = ids["reviews"]["R-0001"], ids["reviews"]["R-0002"]
    # gates in every status; created later than the demo's two open gates
    made["answered"] = L.gate("GATE-0003", "decision", "Answered one", options=[{"value": "a", "label": "A"}],
                              status="answered", answer_value="a", answered_by="owner", answered_via="chat")
    made["nonblocking"] = L.gate("GATE-0004", "other", "Open, not blocking", blocking=0)  # options NULL
    made["applied"] = L.gate("GATE-0005", "decision", "Applied one", status="applied", answer_value="x")
    made["withdrawn"] = L.gate("GATE-0006", "decision", "Withdrawn one", status="withdrawn")
    made["answered_blocking0"] = L.gate("GATE-0007", "other", "Answered, not blocking", blocking=0,
                                        status="answered", answer_text="fine")
    # owner-queue items with no gate: a waiting step (with two dependencies, inserted out of order) and a decision
    s4 = L.step(ids["plan"], ids["phases"][2], 4, "Review the wording", owner_need="review",
                owner_question="Is the wording right?", depends_on=(ids["steps"][3], ids["steps"][1]))
    L.update("steps", s4, status="waiting-owner")
    made["step4"] = s4
    made["d2"] = L.decision("D-0002", "Keep weekends off?", status="open")
    # the same step closing two findings of one issue (issue steps are DISTINCT)
    L.step_finding(ids["steps"][3], ids["findings"]["R-0002/SPEC-M1"])
    # R-0001 gets a T and a second H finding to pin the H, M, L, T then point_id order
    made["t1"] = L.finding(r1, "AAA-T1", severity="T", kind="test", domain_code="spec", summary="A test gap.")
    made["h2"] = L.finding(r1, "ZZZ-H2", severity="H", kind="defect", domain_code="spec", summary="Second high.")
    L.location(made["h2"], "src/auth.py", 2, 2, repository_id=ids["repository"])
    # four more reviews (newest last); R-0003 has a finding that came back, R-0004..R-0006 have none
    for code in ("R-0003", "R-0004", "R-0005", "R-0006"):
        made[code] = L.review(code, commit_sha=ids["commits"][1], repository_id=ids["repository"])
    f = L.finding(made["R-0003"], "SPEC-L9", severity="L", domain_code="spec", summary="Admin bypass again.")
    L.match(f, ids["issues"]["ISS-0003"], "recurrence")
    # issues in the non-active statuses and the active ones the demo lacks
    # (codes are sequential in schema 3: ISS-0005 follows the demo's ISS-0004)
    for code, sev, status in (("ISS-0005", "H", "needs-decision"), ("ISS-0006", "H", "in-progress"),
                              ("ISS-0007", "T", "planned"), ("ISS-0008", "T", "regressed"),
                              ("ISS-0009", "M", "accepted-risk"), ("ISS-0010", "M", "wont-fix"),
                              ("ISS-0011", "L", "duplicate"), ("ISS-0012", "L", "obsolete"),
                              ("ISS-0013", "H", "fixed")):
        L.issue(code, first_seen_review_id=r2, severity=sev, status=status, title=f"Extra {status}")
    # a plan with no steps
    made["p2"] = L.plan("P-0002", "Empty plan")
    L.commit()
    _cache["extra"] = (L, ids, made)
    return _cache["extra"]


def empty():
    if "empty" not in _cache:
        _cache["empty"] = Ledger()
    return _cache["empty"]


def tearDownModule():
    for v in list(_cache.values()) + [(x,) for x in _bulk]:
        (v[0] if isinstance(v, tuple) else v).close()


class RouteCase(unittest.TestCase):
    """call(path, **qs) runs route() on a fresh connection of self.L; returns (status, body)."""
    ledger = staticmethod(lambda: demo()[0])

    @classmethod
    def setUpClass(cls):
        cls.L = cls.ledger()
        cls.mod = cls.L.module

    def call(self, path, body=None, L=None, **qs):
        L = L or self.L
        con = self.mod.connect(L.db)
        try:
            res = self.mod.route(L.root, L.db, con, path, {k: [str(v)] for k, v in qs.items()}, body)
        finally:
            con.close()
        return (res[1], res[0]) if isinstance(res, tuple) else (200, res)

    def ok(self, path, body=None, L=None, **qs):
        status, res = self.call(path, body, L, **qs)
        self.assertEqual(status, 200, f"{path}: {res}")
        return res


# ---------------------------------------------------------------- step 19: overview

class Overview(RouteCase):
    def test_overview_counts(self):
        # step 161: the header is /api/overview, the home page's lists /api/home, the ledger's checks /api/checks
        d = {**self.ok("/api/overview"), **self.ok("/api/home"), **self.ok("/api/checks")}
        self.assertEqual(d["project"], self.L.root.name)
        self.assertEqual({r["severity"]: r["n"] for r in d["open_by_severity"]}, {"M": 2, "L": 1})
        self.assertEqual((d["queue_count"], d["answered_count"]), (2, 0))
        self.assertEqual(d["recurring"], 1)
        self.assertEqual([r["code"] for r in d["reviews"]], ["R-0002", "R-0001"])
        self.assertEqual([(r["points"], r["high"]) for r in d["reviews"]], [(2, 0), (3, 1)])
        # ISS-0237 fixed (step 188): the home page's plans carry the doing count too
        self.assertEqual(d["plans"], [{"code": "P-0001", "title": "Fix plan for R-0001", "status": "in-progress",
                                       "steps": 3, "done": 1, "doing": 0, "waiting": 1}])
        # plan step 69: the checks come from the ledger's ledger_checks table, in its order; step 172: a check is
        # its label, count, what the agent does and the page that shows its rows
        listed = self.L.all("SELECT view, label FROM ledger_checks ORDER BY position, view")
        self.assertEqual([c["label"] for c in d["checks"]], [r["label"] for r in listed])
        self.assertLessEqual({"v_unmatched_findings", "v_unchecked_known_issues", "v_untraceable_refs",
                              "v_uncovered_findings", "v_coverage_problems", "v_unguarded_fixes"},
                             {r["view"] for r in listed})
        n = {r["view"]: c["n"] for r, c in zip(listed, d["checks"])}
        self.assertEqual(n["v_untraceable_refs"], 5)  # fill-refs / tag-review not run on this ledger
        self.assertEqual(sum(v for k, v in n.items() if k != "v_untraceable_refs"), 0)
        self.assertEqual(set(d["labels"]), {"domains", "themes", "enums"})  # enums: schema 6, step 95
        self.assertEqual(d["labels"]["enums"]["issue_status"]["needs-decision"], "Needs your decision")
        self.assertEqual(d["labels"]["domains"]["security"], "Security")
        self.assertEqual(d["labels"]["themes"]["sign-in"], "Sign-in and sessions")

    def test_overview_counts_active_statuses_only(self):
        L, _, _ = extra()
        d = {**self.ok("/api/overview", L=L), **self.ok("/api/home", L=L)}
        want = {}
        for r in L.all("SELECT severity, status FROM issues"):
            if r["status"] in ACTIVE:
                want[r["severity"]] = want.get(r["severity"], 0) + 1
        self.assertEqual({r["severity"]: r["n"] for r in d["open_by_severity"]}, want)
        self.assertEqual(want, {"H": 2, "M": 2, "L": 1, "T": 2})
        # ISS-0107 fixed (step 177): answered gates are counted apart (3 open + step + decision; 2 answered)
        self.assertEqual((d["queue_count"], d["answered_count"]), (5, 2))
        self.assertEqual(d["queue_count"] + d["answered_count"], L.count("v_owner_queue"))
        self.assertEqual([r["code"] for r in d["reviews"]], ["R-0006", "R-0005", "R-0004", "R-0003", "R-0002"])
        self.assertEqual([p["code"] for p in d["plans"]], ["P-0002", "P-0001"])
        # v_plan_totals: a plan with no steps counts 0, not NULL
        self.assertEqual(d["plans"][0], {"code": "P-0002", "title": "Empty plan", "status": "drafting",
                                         "steps": 0, "done": 0, "doing": 0, "waiting": 0})

    def test_overview_empty_ledger(self):
        d = {**self.ok("/api/overview", L=empty()), **self.ok("/api/home", L=empty()),
             **self.ok("/api/checks", L=empty())}
        self.assertEqual((d["open_by_severity"], d["reviews"], d["plans"]), ([], [], []))
        self.assertEqual((d["queue_count"], d["answered_count"], d["recurring"]), (0, 0, 0))
        self.assertEqual([c["n"] for c in d["checks"]], [0] * empty().count("ledger_checks"))
        self.assertGreater(len(d["labels"]["domains"]), 10)


# ---------------------------------------------------------------- step 19: gates

class Gates(RouteCase):
    ledger = staticmethod(lambda: extra()[0])

    def codes(self, **qs):
        return [g["code"] for g in self.ok("/api/gates", **qs)["gates"]]

    def test_gates_status_filters(self):
        pending = self.codes()
        self.assertEqual(sorted(pending), ["GATE-0001", "GATE-0002", "GATE-0003", "GATE-0004", "GATE-0007"])
        self.assertEqual(self.codes(status="pending"), pending)
        self.assertEqual(sorted(self.codes(status="all")), [f"GATE-000{n}" for n in range(1, 8)])
        self.assertEqual(sorted(self.codes(status="open")), ["GATE-0001", "GATE-0002", "GATE-0004"])
        self.assertEqual(sorted(self.codes(status="answered")), ["GATE-0003", "GATE-0007"])
        self.assertEqual(self.codes(status="applied"), ["GATE-0005"])
        self.assertEqual(self.codes(status="withdrawn"), ["GATE-0006"])
        self.assertEqual(self.codes(status="bogus"), [])
        self.assertEqual(self.ok("/api/gates", L=empty()), {"gates": [], "other": []})

    def test_gates_order(self):
        # open before answered; within each, blocking first; then oldest first
        self.assertEqual(self.codes(), ["GATE-0001", "GATE-0002", "GATE-0004", "GATE-0003", "GATE-0007"])
        # the history (step 186): newest first, whatever the status
        want = [r["code"] for r in self.L.all("SELECT code FROM gates ORDER BY created_at DESC, id DESC")]
        self.assertEqual(self.codes(status="all"), want)
        self.assertEqual(want[-2:], ["GATE-0002", "GATE-0001"])

    def test_gates_options_parsed(self):
        g = {x["code"]: x for x in self.ok("/api/gates")["gates"]}
        self.assertEqual(g["GATE-0001"]["options"][0],
                         {"value": "keep-14", "label": "Keep 14 days", "description": "Current behaviour stays the default."})
        self.assertEqual([o["value"] for o in g["GATE-0002"]["options"]], ["approve", "add"])
        self.assertEqual(g["GATE-0004"]["options"], [])  # NULL options -> []
        one = g["GATE-0001"]
        self.assertEqual((one["review_code"], one["plan_code"], one["step_number"], one["issue_code"],
                          one["decision_code"]), ("R-0001", "P-0001", 3, "ISS-0002", "D-0001"))
        self.assertEqual((g["GATE-0004"]["review_code"], g["GATE-0004"]["plan_code"]), (None, None))
        # a history row is the short form: the chosen option's label, no options, context or question (step 160)
        hist = {x["code"]: x for x in self.ok("/api/gates", status="all")["gates"]}
        self.assertEqual((hist["GATE-0003"]["answer_value"], hist["GATE-0003"]["answer_label"]), ("a", "A"))
        self.assertEqual(hist["GATE-0005"]["answer_label"], "x")  # a value its options no longer have: as stored
        self.assertIsNone(hist["GATE-0001"]["answer_label"])
        self.assertFalse({"options", "context", "question"} & set(hist["GATE-0001"]))
        self.assertEqual(hist["GATE-0001"]["review_code"], "R-0001")

    def test_gates_other_queue(self):
        L, _, _ = extra()
        other = self.ok("/api/gates")["other"]
        self.assertEqual([(o["kind"], o["ref"]) for o in other], [("step", "P-0001 #4"), ("decision", "D-0002")])
        self.assertEqual(other[0]["need"], "review")
        self.assertEqual(other[0]["question"], "Is the wording right?")
        self.assertTrue(all(o["kind"] != "gate" for o in other))
        # the demo's waiting step 3 and D-0001 are covered by GATE-0001, so they are not listed again
        self.assertEqual(self.ok("/api/gates", L=demo()[0])["other"], [])
        self.assertNotIn("other", self.ok("/api/gates", status="open"))  # the history has gates only


# ---------------------------------------------------------------- step 19: reviews

class Reviews(RouteCase):
    def test_reviews_list_counts(self):
        rows = self.ok("/api/reviews")
        self.assertEqual([r["code"] for r in rows], ["R-0002", "R-0001"])
        c1, c2 = self.L.repo.commits[:2]
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["R-0001"]["commits"], f"demo {c1[:10]} on main")
        self.assertEqual(by["R-0002"]["commits"], f"demo {c2[:10]} on main")
        self.assertEqual({k: by["R-0001"][k] for k in ("points", "high", "medium", "low", "new_issues", "came_back")},
                         {"points": 3, "high": 1, "medium": 1, "low": 1, "new_issues": 3, "came_back": 0})
        self.assertEqual({k: by["R-0002"][k] for k in ("points", "high", "medium", "low", "new_issues", "came_back")},
                         {"points": 2, "high": 0, "medium": 2, "low": 0, "new_issues": 1, "came_back": 0})
        self.assertEqual(self.ok("/api/reviews", L=empty()), [])

    def test_reviews_list_came_back_and_order(self):
        L, _, _ = extra()
        rows = self.ok("/api/reviews", L=L)
        self.assertEqual([r["code"] for r in rows], ["R-0006", "R-0005", "R-0004", "R-0003", "R-0002", "R-0001"])
        r3 = rows[3]
        self.assertEqual((r3["points"], r3["low"], r3["came_back"], r3["new_issues"]), (1, 1, 1, 0))
        self.assertEqual(rows[0]["points"], 0)
        # the extra H and T findings count in points and high; T has no column of its own
        r1 = rows[-1]
        self.assertEqual((r1["points"], r1["high"], r1["medium"], r1["low"]), (5, 2, 1, 1))

    def test_review_detail_shape(self):
        d = self.ok("/api/review/R-0002")
        self.assertEqual(set(d), {"review", "repos", "goals", "domains", "runs", "checks", "check_counts", "summary",
                                  "metrics", "artifacts", "gates", "findings", "agents", "requirements",
                                  "interview", "review_goals", "test_runs", "relations", "events"})
        r = d["review"]
        self.assertEqual((r["code"], r["previous_code"], r["manager"], r["approved_by"]),
                         ("R-0002", "R-0001", "Review manager", fixtures.DEMO_OWNER))
        self.assertEqual([g["code"] for g in d["goals"]], ["G-01", "G-02"])
        self.assertEqual([(x["name"], x["decision"]) for x in d["domains"]],
                         [("Spec conformance", "included"), ("Security", "included"), ("Database", "skipped")])  # decision, category, name
        self.assertEqual([x["domain_code"] for x in d["runs"]], ["security", "spec"])
        self.assertEqual([(c["issue_code"], c["outcome"]) for c in d["checks"]],
                         [("ISS-0001", "fixed"), ("ISS-0003", "not-checked"), ("ISS-0002", "still-present")])
        self.assertEqual([(s["domain_code"], s["points"]) for s in d["summary"]], [("security", 1), ("spec", 1)])
        self.assertEqual([(m["name"], m["value"]) for m in d["metrics"]], [("mutation_score", 74.5)])
        self.assertEqual(d["artifacts"], [])
        self.assertEqual([g["code"] for g in d["gates"]], ["GATE-0002"])
        self.assertEqual([r["name"] for r in d["repos"]], ["demo"])
        # step 148: a point's list row here, the point in full on its own route
        f = {x["point_id"]: x for x in d["findings"]}
        self.assertEqual(f["SPEC-M1"]["issue_code"], "ISS-0002")
        whole = self.ok(f"/api/finding/{f['SPEC-M1']['id']}")
        self.assertEqual((whole["issue_code"], whole["run_label"]), ("ISS-0002", "spec"))

    def test_review_with_no_findings(self):
        L, _, _ = extra()
        d = self.ok("/api/review/R-0005", L=L)
        for k in ("goals", "domains", "runs", "checks", "summary", "metrics", "artifacts", "gates", "findings"):
            self.assertEqual(d[k], [], k)
        self.assertEqual(len(d["repos"]), 1)

    def test_review_unknown_404(self):
        self.assertEqual(self.call("/api/review/R-9999"), (404, {"error": "no such review"}))
        self.assertEqual(self.call("/api/review/"), (404, {"error": "not found"}))
        self.assertEqual(self.call("/api/review/R-0001/extra"), (404, {"error": "not found"}))

    def test_review_findings_order(self):
        L, _, made = extra()
        d = self.ok("/api/review/R-0001", L=L)
        self.assertEqual([x["point_id"] for x in d["findings"]], ["SEC-H1", "ZZZ-H2", "SPEC-M1", "SPEC-L1", "AAA-T1"])
        whole = [self.ok(f"/api/finding/{x['id']}", L=L) for x in d["findings"]]   # step 148: locations come here
        locs = {x["point_id"]: [(l["path"], l["line_start"], l["line_end"]) for l in x["locations"]] for x in whole}
        self.assertEqual(locs, {"SEC-H1": [("src/auth.py", 1, 3)], "ZZZ-H2": [("src/auth.py", 2, 2)],
                                "SPEC-M1": [("src/requests.py", 2, 4)], "SPEC-L1": [("src/requests.py", 5, 6)],
                                "AAA-T1": []})
        self.assertEqual(whole[0]["locations"][0]["repo"], "demo")


# ---------------------------------------------------------------- step 19: issues

class Issues(RouteCase):
    def codes(self, L=None, **qs):
        return [r["code"] for r in self.ok("/api/issues", L=L, **qs)["rows"]]

    def test_issues_list_order_and_fields(self):
        rows = self.ok("/api/issues")["rows"]
        self.assertEqual([r["code"] for r in rows], ["ISS-0001", "ISS-0002", "ISS-0004", "ISS-0003"])
        self.assertEqual(set(rows[0]), {"code", "title", "severity", "status", "domain_code",
                                        "times_seen", "first_seen", "last_seen"})
        two = rows[1]
        self.assertEqual((two["first_seen"], two["last_seen"], two["times_seen"]), ("R-0001", "R-0002", 2))
        self.assertEqual(self.codes(L=empty()), [])

    def test_issues_filters_exact(self):
        self.assertEqual(self.codes(status="active"), ["ISS-0002", "ISS-0004", "ISS-0003"])
        self.assertEqual(self.codes(status="verified"), ["ISS-0001"])
        self.assertEqual(self.codes(status="ver"), [])  # exact, not LIKE
        self.assertEqual(self.codes(severity="M"), ["ISS-0002", "ISS-0004"])
        self.assertEqual(self.codes(domain_code="spec"), ["ISS-0002", "ISS-0003"])
        self.assertEqual(self.codes(domain_code="spe"), [])
        L, _, _ = extra()
        active = self.codes(L=L, status="active")
        want = [r["code"] for r in L.all("SELECT code, status FROM issues") if r["status"] in ACTIVE]
        self.assertEqual(sorted(active), sorted(want))
        self.assertEqual(len(active), 7)

    def test_issues_search_fields(self):
        self.assertEqual(self.codes(q="ISS-0003"), ["ISS-0003"])                 # code
        self.assertEqual(self.codes(q="hardcoded"), ["ISS-0002"])                # title
        self.assertEqual(self.codes(q="code-compare"), ["ISS-0004"])             # sub
        self.assertEqual(self.codes(q="SPEC-L1"), ["ISS-0003"])                  # finding point_id
        self.assertEqual(self.codes(q="random generator"), ["ISS-0001"])         # finding summary
        self.assertEqual(self.codes(q="requests.py"), ["ISS-0002", "ISS-0003"])   # location path
        self.assertEqual(self.codes(q="PREDICTABLE"), ["ISS-0001"])              # LIKE is case-insensitive
        self.assertEqual(self.codes(q="no such words"), [])
        # ISS-0226 fixed (step 159): % and _ are matched as themselves (test_issues_search_wildcards_literal)
        self.assertEqual(self.codes(q="%"), [])
        self.assertEqual(self.codes(q="_"), [])

    def test_issues_combined(self):
        self.assertEqual(self.codes(status="open", domain_code="security"), ["ISS-0004"])
        self.assertEqual(self.codes(severity="M", domain_code="spec"), ["ISS-0002"])
        self.assertEqual(self.codes(status="active", severity="L"), ["ISS-0003"])
        self.assertEqual(self.codes(q="sign-in", status="verified"), ["ISS-0001"])
        self.assertEqual(self.codes(q="auth.py", severity="M"), ["ISS-0004"])
        self.assertEqual(self.codes(q="auth.py", domain_code="spec"), [])

    def test_issues_route_reports_total(self):
        # ISS-0128 fixed (step 160; was test_issues_list_capped_at_2000): a page of rows and the total, never a
        # silent cut
        L = bulk()
        d = self.ok("/api/issues", L=L)
        self.assertEqual((len(d["rows"]), d["limit"], d["offset"]), (200, 200, 0))
        self.assertEqual(self.ok("/api/issues", L=L, status="")["total"], L.count("issues"))
        self.assertEqual(L.count("issues"), 2105)

    def test_issue_detail_shape(self):
        d = self.ok("/api/issue/ISS-0002")
        self.assertEqual(set(d), {"issue", "findings", "relations", "checks", "timeline", "guard_tests",
                                  "decisions", "steps", "unguarded"})
        i = d["issue"]
        self.assertEqual((i["code"], i["first_seen"], i["last_seen"], i["last_checked"]),
                         ("ISS-0002", "R-0001", "R-0002", "R-0002"))
        self.assertEqual([(f["review_code"], f["point_id"]) for f in d["findings"]],
                         [("R-0001", "SPEC-M1"), ("R-0002", "SPEC-M1")])
        for f in d["findings"]:
            self.assertEqual([(l["path"], l["line_start"], l["repo"]) for l in f["locations"]],
                             [("src/requests.py", 2, "demo")])
        self.assertEqual([(c["review"], c["outcome"]) for c in d["checks"]], [("R-0002", "still-present")])
        self.assertEqual([e["event"] for e in d["timeline"]][:1], ["opened"])
        self.assertEqual(d["guard_tests"], [])
        self.assertEqual(d["decisions"], [])
        self.assertEqual([(s["plan"], s["number"], s["status"]) for s in d["steps"]], [("P-0001", 3, "waiting-owner")])
        g = self.ok("/api/issue/ISS-0001")
        self.assertEqual([(t["test_name"], t["failed_before_fix"]) for t in g["guard_tests"]],
                         [("test_code_uses_secrets", 1)])
        c2 = self.L.repo.commits[1]
        self.assertEqual([(s["number"], s["status"], s["commits"]) for s in g["steps"]], [(2, "done", c2[:10])])

    def test_issue_steps_distinct(self):
        L, _, _ = extra()
        d = self.ok("/api/issue/ISS-0002", L=L)
        # step 3 closes both ISS-0002 findings and is listed once
        self.assertEqual([(s["plan"], s["number"]) for s in d["steps"]], [("P-0001", 3)])

    def test_issue_relations_both_directions(self):
        out = self.ok("/api/issue/ISS-0004")["relations"]
        self.assertEqual(out, [{"dir": "out", "relation": "related-to", "other": "ISS-0001",
                                "other_title": "Predictable sign-in codes", "other_status": "verified",
                                "confidence": "likely",
                                "rationale": "Same sign-in code path as the earlier randomness issue",
                                "review": "R-0002"}])
        inn = self.ok("/api/issue/ISS-0001")["relations"]
        self.assertEqual([(r["dir"], r["relation"], r["other"], r["other_status"]) for r in inn],
                         [("in", "related-to", "ISS-0004", "open")])
        self.assertEqual(self.ok("/api/issue/ISS-0003")["relations"], [])

    def test_issue_unknown_404(self):
        self.assertEqual(self.call("/api/issue/ISS-9999"), (404, {"error": "no such issue"}))
        self.assertEqual(self.call("/api/issue/iss-0001"), (404, {"error": "no such issue"}))  # codes are exact


# ---------------------------------------------------------------- step 20: plans, trends, tables

class Plans(RouteCase):
    def test_plans_list_counts(self):
        rows = self.ok("/api/plans")
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertEqual({k: p[k] for k in ("code", "status", "steps", "done", "doing", "waiting", "reviews")},
                         {"code": "P-0001", "status": "in-progress", "steps": 3, "done": 1, "doing": 0,
                          "waiting": 1, "reviews": "R-0001"})
        L, _, _ = extra()
        rows = self.ok("/api/plans", L=L)
        self.assertEqual([r["code"] for r in rows], ["P-0002", "P-0001"])
        self.assertEqual({k: rows[0][k] for k in ("steps", "done", "doing", "waiting", "reviews")},
                         {"steps": 0, "done": 0, "doing": 0, "waiting": 0, "reviews": None})
        self.assertEqual(rows[1]["waiting"], 2)
        self.assertEqual(self.ok("/api/plans", L=empty()), [])

    def test_plan_detail_shape(self):
        d = self.ok("/api/plan/P-0001")
        self.assertEqual(set(d), {"plan", "reviews", "artifacts", "last_updated", "gates", "discoveries", "events", "phases", "phase_rows", "steps", "unguarded"})
        self.assertEqual(d["plan"]["code"], "P-0001")
        self.assertEqual([s["number"] for s in d["steps"]], [1, 2, 3])
        self.assertEqual([s["phase"] for s in d["steps"]], [1, 2, 2])
        self.assertEqual([s["depends"] for s in d["steps"]], [[], [1], [1]])
        # step 162: a step lists the ids of the points it closes; the points and the step in full are /api/step
        f = demo()[1]["findings"]
        self.assertEqual([s["closes"] for s in d["steps"]],
                         [[f["R-0001/SPEC-L1"]], [f["R-0001/SEC-H1"]], [f["R-0001/SPEC-M1"]]])
        full = [self.ok(f"/api/step/P-0001/{n}") for n in (1, 2, 3)]
        self.assertEqual([[c["point_id"] for c in s["closes"]] for s in full], [["SPEC-L1"], ["SEC-H1"], ["SPEC-M1"]])
        c = full[1]["closes"][0]
        self.assertEqual((c["review"], c["issue"], c["severity"]), ("R-0001", "ISS-0001", "H"))
        c2 = self.L.repo.commits[1]
        self.assertEqual([[x["commit_sha"] for x in s["commits"]] for s in full], [[], [c2], []])
        self.assertEqual([(p["phase"], p["steps"], p["done"]) for p in d["phases"]], [(1, 1, 0), (2, 2, 1)])
        self.assertEqual([p["number"] for p in d["phase_rows"]], [1, 2])
        L, _, _ = extra()
        d = self.ok("/api/plan/P-0001", L=L)
        self.assertEqual(d["steps"][3]["depends"], [1, 3])  # sorted, though inserted 3 then 1
        self.assertEqual(self.ok("/api/plan/P-0002", L=L)["steps"], [])

    def test_plan_unknown_404(self):
        self.assertEqual(self.call("/api/plan/P-9999"), (404, {"error": "no such plan"}))


class Trends(RouteCase):
    def test_trends_shape(self):
        d = self.ok("/api/trends")
        self.assertEqual(set(d), {"metrics", "reviews", "recurring", "hotspots"})
        self.assertEqual([(m["review"], m["value"]) for m in d["metrics"]], [("R-0001", 61.0), ("R-0002", 74.5)])
        self.assertEqual([r["review"] for r in d["reviews"]], ["R-0001", "R-0002"])
        r2 = d["reviews"][1]
        # summed across the review's domains (security 1 + spec 1)
        self.assertEqual({k: r2[k] for k in ("points", "high", "medium", "low", "new_issues", "carried_over",
                                              "came_back", "false_findings")},
                         {"points": 2, "high": 0, "medium": 2, "low": 0, "new_issues": 1, "carried_over": 1,
                          "came_back": 0, "false_findings": 0})
        self.assertEqual([r["code"] for r in d["recurring"]], ["ISS-0002"])
        self.assertEqual([h["path"] for h in d["hotspots"]], ["src/auth.py", "src/requests.py"])
        self.assertEqual(self.ok("/api/trends", L=empty()),
                         {"metrics": [], "reviews": [], "recurring": [], "hotspots": []})

    def test_trends_limits(self):
        L = bulk()
        d = self.ok("/api/trends", L=L)
        self.assertEqual(L.count("v_recurring_issues"), 60)
        self.assertEqual(len(d["recurring"]), 50)
        self.assertEqual(L.count("v_file_hotspots"), 40)
        self.assertEqual(len(d["hotspots"]), 30)


_bulk = []


def bulk():
    """A ledger with 2105 issues (60 seen twice) and 40 cited files, made with plain SQL."""
    if _bulk:
        return _bulk[0]
    L = Ledger()
    r1, r2 = L.review("R-0001"), L.review("R-0002")
    repo = L.default_repository()
    con = L.con
    stamp = "2026-11-01T10:00:00.000Z"
    con.executemany("INSERT INTO issues(code, title, severity, domain_code, kind, sub, first_seen_review_id, "
                    "times_seen, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(f"ISS-{n:04d}", f"Bulk {n}", "M", "spec", "defect", f"bulk-{n}", r1, 2 if n <= 60 else 1,
                      stamp, stamp) for n in range(1, 2106)])
    for n in range(40):
        f = L.finding(r2, f"SPEC-M{n}", severity="M", domain_code="spec", summary=f"bulk {n}")
        L.location(f, f"src/f{n:02d}.py", 1, 1, repository_id=repo)
    L.commit()
    _bulk.append(L)
    return L


class Tables(RouteCase):
    def test_tables_lists_all_objects(self):
        rows = self.ok("/api/tables")
        # sqlite_stat1 (made by PRAGMA optimize, plan step 89) and other internal tables are not listed
        want = self.L.all("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                          "AND name NOT LIKE 'sqlite%' ORDER BY type, name")
        self.assertEqual([(r["name"], r["type"]) for r in rows], [(w["name"], w["type"]) for w in want])
        self.assertFalse([r["name"] for r in rows if r["name"].startswith("sqlite_")])
        # steps 165 and 189: no column list (the page never read it), a label and description, and no count for a view
        self.assertEqual(set(rows[0]), {"name", "type", "label", "description", "for_agent", "count"})
        by = {r["name"]: r for r in rows}
        for name in ("issues", "gates", "events"):
            self.assertEqual(by[name]["count"], self.L.count(name), name)
        self.assertNotIn("count", by["v_owner_queue"])
        self.assertEqual(by["issues"]["count"], 4)
        n_tables = sum(1 for r in rows if r["type"] == "table")
        n_views = sum(1 for r in rows if r["type"] == "view")
        self.assertGreaterEqual((n_tables, n_views), (35, 18))  # at schema version 1: 35 tables, 18 views

    def test_table_view(self):
        d = self.ok("/api/table/v_open_issues")
        self.assertEqual(set(d), {"columns", "rows", "count", "more", "truncated", "offset", "limit"})
        self.assertIsNone(d["count"])   # step 165: a view is counted only when asked
        self.assertEqual(self.ok("/api/table/v_open_issues", count=1)["count"], self.L.count("v_open_issues"))
        self.assertEqual(len(d["rows"]), self.L.count("v_open_issues"))
        self.assertEqual(len(d["columns"]), len(d["rows"][0]))
        d = self.ok("/api/table/issues", limit=2, offset=1)
        self.assertEqual((d["limit"], d["offset"], d["count"], len(d["rows"])), (2, 1, 4, 2))
        code = d["columns"].index("code")
        self.assertEqual([r[code] for r in d["rows"]], ["ISS-0002", "ISS-0003"])
        self.assertEqual(self.ok("/api/table/issues", offset=10)["rows"], [])

    def test_table_paging_clamps(self):
        self.assertEqual(self.ok("/api/table/issues")["limit"], 100)
        self.assertEqual(self.ok("/api/table/issues", limit=0)["limit"], 1)
        self.assertEqual(self.ok("/api/table/issues", limit=-5)["limit"], 1)
        self.assertEqual(self.ok("/api/table/issues", limit=99999)["limit"], 500)
        self.assertEqual(self.ok("/api/table/issues", offset=-4)["offset"], 0)
        # ISS-0085 fixed: a limit or offset that is not a whole number is a 400 with a plain sentence
        self.assertEqual(self.call("/api/table/issues", limit="abc"), (400, {"error": "limit must be a whole number."}))
        self.assertEqual(self.call("/api/table/issues", offset="1.5"),
                         (400, {"error": "offset must be a whole number."}))

    def test_table_name_validation(self):
        for name in ('issues"--', "sqlite_master", "sqlite_schema", "nope", "issues;", "1issues", "%2569ssues"):
            self.assertEqual(self.call(f"/api/table/{name}"), (404, {"error": "no such table"}), name)
        # ../x and deeper paths never reach the table lookup
        self.assertEqual(self.call("/api/table/../x"), (404, {"error": "not found"}))
        self.assertEqual(self.call("/api/table/"), (404, {"error": "not found"}))


# ---------------------------------------------------------------- step 20: other routes and the contract

class OtherRoutes(RouteCase):
    def test_source_route_statuses(self):
        lid = self.L.scalar("SELECT min(id) FROM finding_locations")
        d = self.ok(f"/api/source/{lid}")
        self.assertEqual(d["source"], "git")
        # ISS-0085 fixed (was test_current_source_route_statuses): an unknown reference is 404, a non-number 400
        self.assertEqual(self.call("/api/source/99999"), (404, {"error": "no such reference"}))
        self.assertEqual(self.call("/api/source/abc"), (400, {"error": "The reference id must be a whole number."}))

    def test_unknown_routes_404(self):
        for path in ("/api/nothing", "/api/", "/api/overview/extra", "/api/gates/GATE-0001"):
            self.assertEqual(self.call(path), (404, {"error": "not found"}), path)
        # the answer route and the SQL box are POST-only (body None means GET): 405 naming the method that works
        for path in ("/api/sql", "/api/gates/GATE-0001/answer"):
            res = self.call(path)
            self.assertEqual(res, (405, {"error": "This address takes POST, not GET."}), path)
        con = self.mod.connect(self.L.db)
        try:
            self.assertEqual(self.mod.route(self.L.root, self.L.db, con, "/api/sql", {}, None)[2], {"Allow": "POST"})
        finally:
            con.close()
        # ISS-0019 fixed (step 41): route() requires the 'api' segment, so /x/overview is not the overview
        self.assertEqual(self.call("/x/overview"), (404, {"error": "not found"}))


# Exact top-level keys of each route on the demo ledger. For list routes: the keys of one row.
CONTRACT = {
    "overview": ("GET", "/api/overview", None, 200, dict,
                 {"project", "queue_count", "answered_count", "labels"}),
    "home": ("GET", "/api/home", None, 200, dict,
             {"open_by_severity", "reviews", "plans", "discoveries"}),
    "guide": ("GET", "/api/guide", None, 200, dict, {"guide"}),
    "run": ("GET", "/api/run/{run}", None, 200, dict, {"prompt", "final_message"}),
    "checks": ("GET", "/api/checks", None, 200, dict, {"checks", "recurring"}),
    "decisions": ("GET", "/api/decisions", None, 200, dict, {"decisions"}),
    "gates": ("GET", "/api/gates", None, 200, dict, {"gates", "other"}),
    "gates-all": ("GET", "/api/gates?status=all", None, 200, dict, {"gates", "total", "limit", "offset"}),
    "reviews": ("GET", "/api/reviews", None, 200, list,
                {"code", "title", "scope", "status", "started_at", "commits", "points",
                 "high", "medium", "low", "tests", "new_issues", "came_back"}),
    "review": ("GET", "/api/review/R-0001", None, 200, dict,
               {"review", "repos", "goals", "domains", "runs", "checks", "check_counts", "summary", "metrics",
                "artifacts", "gates", "findings", "agents", "requirements", "interview", "review_goals",
                "test_runs", "relations", "events"}),
    "review-checks": ("GET", "/api/review/R-0001/checks", None, 200, dict, {"rows", "total", "limit", "offset"}),
    "review-search": ("GET", "/api/review/R-0001?q=auth", None, 200, dict, {"match"}),
    "finding": ("GET", "/api/finding/{finding}", None, 200, dict, None),
    "step": ("GET", "/api/step/P-0001/2", None, 200, dict,
             {"number", "change", "proof", "owner_question", "behavior_note", "progress_note", "no_findings_reason",
              "started_at", "done_at", "commits", "closes"}),
    "issues": ("GET", "/api/issues", None, 200, dict, {"rows", "total", "limit", "offset"}),
    "issue": ("GET", "/api/issue/ISS-0002", None, 200, dict,
              {"issue", "findings", "relations", "checks", "timeline", "guard_tests", "decisions", "steps",
               "unguarded"}),
    "plans": ("GET", "/api/plans", None, 200, list,
              {"code", "title", "status", "steps", "done", "doing", "waiting",
               "reviews"}),
    "plan": ("GET", "/api/plan/P-0001", None, 200, dict,
             {"plan", "reviews", "artifacts", "last_updated", "gates", "discoveries", "events", "phases", "phase_rows", "steps", "unguarded"}),
    "trends": ("GET", "/api/trends", None, 200, dict, {"metrics", "reviews", "recurring", "hotspots"}),
    "goals": ("GET", "/api/goals", None, 200, dict, {"goals"}),  # schema 9 (step 129)
    "tables": ("GET", "/api/tables", None, 200, list, {"name", "type", "label", "description", "for_agent", "count"}),
    "table": ("GET", "/api/table/issues", None, 200, dict,
              {"columns", "rows", "count", "more", "truncated", "offset", "limit"}),
    "sql": ("POST", "/api/sql", {"sql": "SELECT 1 AS one"}, 200, dict, {"columns", "rows", "truncated"}),
    "source": ("GET", "/api/source/{loc}", None, 200, dict,
               {"line_start", "line_end", "permalink", "git_command", "source", "lines"}),
    "answer": ("POST", "/api/gates/GATE-0001/answer", {"answer_value": "use-7"}, 200, dict, {"ok"}),
    "review-404": ("GET", "/api/review/R-9999", None, 404, dict, {"error"}),
    "issue-404": ("GET", "/api/issue/ISS-9999", None, 404, dict, {"error"}),
    "plan-404": ("GET", "/api/plan/P-9999", None, 404, dict, {"error"}),
    "table-404": ("GET", "/api/table/nope", None, 404, dict, {"error"}),
    "gate-404": ("POST", "/api/gates/GATE-9999/answer", {"answer_value": "x"}, 404, dict, {"error"}),
    "unknown-404": ("GET", "/api/nothing", None, 404, dict, {"error"}),
    "source-404": ("GET", "/api/source/99999", None, 404, dict, {"error"}),
    "source-400": ("GET", "/api/source/abc", None, 400, dict, {"error"}),
    "sql-get-405": ("GET", "/api/sql", None, 405, dict, {"error"}),
    "overview-post-405": ("POST", "/api/overview", {}, 405, dict, {"error"}),
}


# Keys a route returns only in some cases (source: excerpt_matches when an excerpt is saved and git was read;
# git_error when git could not be read). The page may read them; the contract on the demo does not see them.
# table: total is the hub's own (rows the ledger has, when its copy holds fewer).
# plan: findings is the hub's own (every closed point's row; the viewer asks /api/step when a step is opened).
OPTIONAL_KEYS = {"source": {"git_error", "excerpt_matches"}, "table": {"total"}, "plan": {"findings"}}


class Contract(RouteCase):
    @classmethod
    def setUpClass(cls):
        # the answer route writes, so the contract runs on its own demo ledger
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.mod = cls.L.module

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def test_every_route_contract(self):
        loc = self.L.scalar("SELECT min(id) FROM finding_locations")
        finding = self.L.scalar("SELECT min(id) FROM findings")
        run = self.L.scalar("SELECT min(id) FROM reviewer_runs")
        for name, (method, path, body, status, shape, keys) in CONTRACT.items():
            with self.subTest(route=name):
                p, _, q = path.format(loc=loc, finding=finding, run=run).partition("?")
                got_status, res = self.call(p, body, **dict(x.split("=") for x in q.split("&") if x))
                self.assertEqual(got_status, status, f"{name}: {res}")
                self.assertIsInstance(res, shape)
                got = set(res[0]) if shape is list else set(res)
                if keys is None:   # a point in full: the keys ROUTE_KEYS pins
                    keys = {k.split("[")[0] for k in re.sub(r"\[[^\]]*\]", "[", ROUTE_KEYS["finding/*"]).split()}
                self.assertEqual(got, keys, name)

    def test_page_reads_only_returned_fields(self):
        """Every d.<prop> (or o.<prop>, OVERVIEW.<prop>) the page reads from a dict route is one of its keys."""
        route_keys = {}
        for name, (method, path, body, status, shape, keys) in CONTRACT.items():
            if status == 200 and shape is dict and keys is not None:
                route_keys.setdefault(path.split("/")[2].split("?")[0], set()).update(
                    keys | OPTIONAL_KEYS.get(name, set()))
        decl = re.compile(r"const (\w+) = (?:OVERVIEW = |OVERVIEW \|\| )?await api\([\"`]([a-z]+)")
        checked = 0
        problems = []
        lines = PAGE_HTML.splitlines()
        for i, line in enumerate(lines):
            for m in decl.finditer(line):
                var, route = m.group(1), m.group(2)
                if route not in route_keys:
                    continue  # list routes: the page reads row fields, not top-level keys
                scope = [line[m.end():]]
                for nxt in lines[i + 1:]:
                    if nxt.startswith(("VIEWS.", "function ", "async function ")) or \
                            re.search(rf"const {var} = .*await api\(", nxt):
                        break
                    scope.append(nxt)
                for p in re.findall(rf"(?<![\w.]){var}\.([A-Za-z_]\w*)", "\n".join(scope)):
                    checked += 1
                    if p not in route_keys[route]:
                        problems.append(f"page line {i + 1}: {var}.{p} is not returned by /api/{route}")
        for p in re.findall(r"OVERVIEW\.([A-Za-z_]\w*)", PAGE_HTML):
            checked += 1
            if p not in route_keys["overview"]:
                problems.append(f"OVERVIEW.{p} is not returned by /api/overview")
        self.assertGreater(checked, 30, "the page scan found too few property reads; the pattern is stale")
        self.assertEqual(problems, [])


# ---------------------------------------------------------------- step 172: every field of every route

def full_ledger(L):
    """A demo ledger with a row in every list a route returns: the shown-data demo, an owner queue beyond its
    gates, an interview with a goal written from it, a decision about an issue, and a test guarding a fix."""
    ids = _w2_shown_helpers.build(L)
    r1 = ids["reviews"]["R-0001"]
    L.update("steps", ids["steps"][1], status="waiting-owner")
    L.decision("D-0002", "Keep weekends off?", status="open")
    L.x("INSERT INTO decision_issues(decision_id, issue_id) VALUES (?, ?)",
        (ids["decisions"]["D-0001"], ids["issues"]["ISS-0002"]))
    q = L.insert("interview_questions", code="IQ-0001", review_id=r1, domain_code="spec", question="Which inputs?",
                 why_asked="The parser accepts two formats.")
    L.insert("interview_answers", question_id=q, answer="JSON only.", answered_via="chat")
    goal = L.id_of("design_goals", "G-01")
    L.update("design_goals", goal, review_id=r1)
    L.insert("goal_sources", goal_id=goal, question_id=q)
    L.dependency(ids["steps"][3], ids["steps"][2])
    L.update("issues", L.id_of("issues", ids["found_issue"]), status="fixed",
             after_state="The retry stops after five tries.")   # fixed, with no test guarding it
    L.commit()
    return ids


def merged(a, b):
    """Two shapes of the same answer taken together: every key of either, an empty list filled from the other."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: merged(a[k], b[k]) if k in a and k in b else a.get(k, b.get(k)) for k in [*a, *[k for k in b if k not in a]]}
    if isinstance(a, list) and isinstance(b, list):
        return [merged(a[0], b[0])] if a and b else a or b
    return b if a == [] or a is None else a   # [] next to a list of plain values (a leaf) is that leaf


def shape(v):
    """The keys of an answer, nested: a dict's keys, and for a list the keys of its rows taken together. A list
    of plain values is a leaf; an empty list is []."""
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        out = None
        for x in v:
            s = shape(x)
            if isinstance(s, dict):
                out = s if out is None else merged(out, s)
        return [out] if out else [] if not v else None
    return None


def compact(s):
    """A shape as the text ROUTE_KEYS holds: a{b c[d e]} is key a (a dict with b and c, c a list of rows d, e)."""
    if isinstance(s, list):
        return "[" + compact(s[0]) + "]" if s else "[]"
    if isinstance(s, dict):
        return " ".join(k + ("{" + compact(v) + "}" if isinstance(v, dict) else compact(v) if isinstance(v, list) else "")
                        for k, v in s.items())
    return ""


def names(s):
    """Every key a shape names, at any depth."""
    if isinstance(s, list):
        s = s[0] if s else {}
    for k, v in (s or {}).items():
        yield k
        if v:
            yield from names(v)


# What each GET route returns on full_ledger(), by route pattern: every key, down to the rows of its lists (the
# text compact() gives). A field added to a route is added here and read by the page; one the page stops reading
# is dropped from the route. labels and the rows of Explore's tables are data, not fields.
ROUTE_KEYS = {
    "overview":
        "project queue_count answered_count labels",
    "home":
        "open_by_severity[severity n] reviews[code title status started_at points high] plans[code title "
        "status steps done doing waiting] discoveries",
    "checks":
        "checks[label n action recipe link] recurring",
    "decisions":
        "decisions[code question answer rationale status revisit_when decided_at review issues]",
    "gates?status=pending":
        "gates[code kind title question context options[value label description] allow_text blocking status "
        "answer_value answer_text answered_at created_at review_code plan_code step_number issue_code "
        "decision_code] other[kind ref need title question context]",
    "gates?status=all":
        "gates[code kind title status blocking created_at answered_at answer_value answer_text applied_note "
        "review_code plan_code step_number issue_code decision_code answer_label] total limit offset",
    "reviews":
        "[code title scope status started_at commits points high medium low tests new_issues came_back]",
    "review/*":
        "review{code title scope status request_text approved_at skill_version review_folder summary_doc_url "
        "verdict summary started_at completed_at report_md manager manager_model approved_by previous_code} "
        "repos[name remote_url default_branch notes commit_sha branch commit_subject tag_name tag_pushed "
        "is_dirty dirty_note] goals[code text] test_runs[command purpose passed failed skipped errored "
        "duration_ms notes started_at] relations[from_issue relation to_issue to_title confidence rationale] "
        "events[at event from_value to_value note actor] domains[name decision reason owner_change "
        "reviewers_planned] runs[id domain_code label scope report_path status items_reviewed high_count "
        "medium_count low_count test_count started_at finished_at actor model harness session] checks[outcome "
        "evidence issue_code title status] check_counts[outcome n] summary[domain_code points high medium low "
        "tests confirmed false_findings new_issues carried_over came_back] agents[role kind name model "
        "harness parent] requirements[requirement text verdict where_implemented tested point_id evidence "
        "verdict_count] metrics[name scope value unit method note label] artifacts[kind path url created_at "
        "run] gates[answer_label code kind title status answer_value answer_text] interview[code domain "
        "question why_asked gate answers[answer answered_via answered_at]] review_goals[code domain text "
        "status approved_at note adopted_at retired_at used_by sources[code question]] findings[id point_id "
        "severity domain_code summary match_type verification issue_code]",
    "review/*/checks":
        "rows[outcome evidence issue_code title status] total limit offset",
    "finding/*":
        "id point_id match_type match_confidence match_rationale severity kind domain_code theme_code summary "
        "detail before_state recommendation recommended_test evidence where_report verification "
        "verification_note review_code issue_code run_label verified_by discovered_in_step discovered_in_plan "
        "goals[code text] requirements[code text verdict] locations[id path line_start line_end symbol role "
        "commit_sha repo]",
    "issues":
        "rows[code title severity status domain_code times_seen first_seen last_seen] total limit offset",
    "issue/*":
        "issue{code title domain_code theme_code severity status before_state current_state after_state "
        "recommendation resolution times_seen opened_at resolved_at verified_at first_seen last_seen "
        "last_checked} findings[id point_id match_type match_confidence match_rationale severity kind "
        "domain_code theme_code summary detail before_state recommendation recommended_test evidence "
        "where_report verification verification_note review_code verified_by discovered_in_step "
        "discovered_in_plan locations[id path line_start line_end symbol role commit_sha repo]] relations[dir "
        "relation other other_title other_status confidence rationale review] unguarded[problem] "
        "checks[review started_at outcome evidence] timeline[at event from_value to_value review step "
        "commit_sha actor note] guard_tests[test_path test_name failed_before_fix still_exists "
        "added_commit_sha] decisions[code question answer rationale decided_at] steps[plan number title "
        "status progress_note commits]",
    "plans":
        "[code title status steps done doing waiting reviews]",
    "plan/*":
        "plan{code title status approved_at doc_url page_url narrative_md} reviews[code] artifacts[kind path "
        "url created_at] last_updated gates[code title status step] discoveries[point_id summary severity "
        "review issue found_in_step closing_step] events[at event from_value to_value note actor] "
        "unguarded[code title problem step] phases[phase steps done] phase_rows[number title goal why "
        "first_step last_step] steps[number section title status owner_need behavior_change phase change "
        "proof owner_question behavior_note progress_note no_findings_reason started_at done_at depends "
        "closes search commits[commit_sha branch pr_url summary]]",
    "step/*/*":
        "number change proof owner_question behavior_note progress_note no_findings_reason started_at done_at "
        "commits[commit_sha branch pr_url summary] closes[point_id severity summary review issue]",
    "goals":
        "goals[code domain text status approved_at note adopted_at retired_at used_by sources[code question]]",
    "guide":
        "guide",
    "run/*":
        "prompt final_message",
    "trends":
        "metrics[name scope repository review value unit label better] reviews[review started_at points high "
        "medium low tests confirmed new_issues carried_over came_back false_findings] recurring[code title "
        "severity status times_seen regressions reviews] hotspots[path issues reviews high_points]",
    "tables":
        "[name type label description for_agent count]",
    "table/*?limit=500&offset=0":
        "columns rows count more truncated offset limit",
    "source/*":
        "line_start line_end permalink git_command lines[n text] source",
}
# Keys only the shared copy reads (dev/core.py hub_snapshot, the page's staticApi).
HUB_KEYS = {"search", "total", "findings"}
# Row keys that are data the page walks rather than names it reads: a label map's values, a table's cells.
DATA_KEYS = {"overview": ("labels",), "table/*?limit=500&offset=0": ("columns", "rows")}


class RouteKeys(RouteCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        full_ledger(cls.L)
        cls.mod = cls.L.module
        con = cls.mod.connect(cls.L.db)
        answers = {}
        try:
            for r in cls.mod.ROUTES:
                # the paths the shared copy embeds, and a step (the copy holds every step inside its plan)
                extra = {cls.mod.get_step: ["step/P-0001/2", "step/P-0001/3"], cls.mod.get_plan: ["plan/P-0001?phase=2"]}
                for path in (r.hub(con) if r.hub else []) + extra.get(r.handler, []):
                    p, _, q = path.partition("?")
                    res = cls.mod.route(cls.L.root, cls.L.db, con, "/api/" + p,
                                        {k: [v] for k, v in (x.split("=") for x in q.split("&") if x)}, None)
                    self_name = "/".join(r.pattern) + ("?" + q if q and r.handler is not cls.mod.get_plan else "")
                    answers.setdefault(self_name, []).append(res)
        finally:
            con.close()
        cls.shapes = {}
        for name, got in answers.items():   # the answers of one route (one per review, say), taken together
            s = shape(got)[0] if isinstance(got[0], dict) else shape([row for a in got for row in a])
            for k in DATA_KEYS.get(name, ()):
                s[k] = None
            cls.shapes[name] = s

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def test_route_keys_match_whitelist(self):
        got = {name: compact(s) for name, s in self.shapes.items()}
        self.assertEqual(set(got), set(ROUTE_KEYS))
        for name in got:
            with self.subTest(route=name):
                self.assertEqual(got[name].split(), " ".join(ROUTE_KEYS[name].split()).split(), compact(self.shapes[name]))
        # no list is empty on this ledger, so every row's keys are pinned
        for name, text in got.items():
            self.assertNotIn("[]", text, name)

    def test_every_returned_field_is_read(self):
        """Every key a route returns is named by the page's script (.key, key:"key", or destructured), or is one
        the shared copy alone reads."""
        script = PAGE_HTML[PAGE_HTML.index('"use strict";'):]
        unread = []
        for name, s in self.shapes.items():
            for k in sorted(set(names(s))):
                if k in HUB_KEYS:
                    continue
                if not re.search(r'\.%s\b|key:"%s"|[{,] ?%s[,}]' % (k, k, k), script):
                    unread.append(f"/api/{name}: {k}")
        self.assertEqual(unread, [])


if __name__ == "__main__":
    unittest.main()
