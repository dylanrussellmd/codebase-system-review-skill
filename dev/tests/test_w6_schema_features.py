"""Plan P-0001 step 145 (OWN-13, schema 18): the features domain proposes what the reviewed codebase could add to meet
the owner's goals. A proposal (FP-0001) is not a defect: it has its own table and lifecycle (proposed, picked,
declined, built), a value and an effort instead of a severity, and it is never counted in open issues, defect totals,
regressions, coverage or the must-be-empty checks. Picked ones become plan steps; declined ones are not proposed
again for the same goal."""
import json
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, refused, project, counts

REFS = fixtures.REPO_ROOT / "references"

GOOD = """# features: what the demo could add

## Scope and method

Read the goals, the requirement and src/app.py at the reviewed commit.

## Checklist

| check | answer | evidence |
|-------|--------|----------|
{checks}

## Goals

| goal | what the code does toward it | evidence |
|------|------------------------------|----------|
| G-01 | handler answers one request at a time and keeps nothing | `demo:src/app.py:5` |

## Proposals

### FEAT-1 Keep a history of handled requests
- Goal: G-01
- Today: nothing is kept once a request is answered (`demo:src/app.py:5`)
- Proposal: a small store of handled requests with their answers
- Value: high
- Effort: medium
- Risks: the store grows without a limit
- Depends: none
- Done-test: test_history_lists_handled_requests: two requests handled, both listed in order

## Totals

1 goal read, 1 proposal.
"""


def with_goal(test, include=True):
    """R-0001 (approved, running) with an approved whole-project goal G-01, an owner A-0002 and, unless include is
    False, the features domain included; returns (repo, L)."""
    repo, L, _ = project(test, approved=False)
    ok(L, "add-actor", "owner", "the owner")
    ok(L, "propose-goal", "--review", "R-0001", "--source", "owner, chat", "Every request is kept for later.",
       "--actor", "A-0001")
    ok(L, "approve-goals", "G-01", "--actor", "A-0002")
    if include:
        ok(L, "review-domain", "R-0001", "features", "include", "--reason", "the owner approved goals")
    ok(L, "review-status", "R-0001", "awaiting-approval", "--note", "domains proposed")
    ok(L, "review-status", "R-0001", "approved", "--note", "the owner approved the domains")
    ok(L, "review-status", "R-0001", "running", "--note", "reviewers started")
    return repo, L


def propose(L, title="Keep a history of handled requests", goal=("--goal", "G-01"), **kw):
    args = ["propose-feature", "--review", "R-0001", *goal, "--title", title,
            "--today", kw.get("today", "nothing is kept once a request is answered"),
            "--evidence", kw.get("evidence", "demo:src/app.py:5"),
            "--proposal", "a small store of handled requests", "--value", kw.get("value", "high"),
            "--effort", kw.get("effort", "medium"), "--done-test",
            kw.get("done_test", "test_history: two requests handled, both listed"), "--actor", "A-0001"]
    return args


class FeaturesDomain(unittest.TestCase):
    def test_features_domain_seeded(self):
        with fixtures.Ledger(fixtures.temp_dir("w6-feat-")) as L:
            d = L.one("SELECT * FROM domains WHERE code='features'")
            self.assertEqual((d["report_file"], d["retired_at"]), ("features.md", None))
            self.assertIn("goals", d["description"])
            self.assertEqual(L.one("SELECT rule FROM domain_rules WHERE domain_code='features'")["rule"],
                             "goals >= 1 or requirements >= 1")
            checks = L.all("SELECT code, question FROM domain_checks WHERE domain_code='features' ORDER BY position")
            self.assertGreaterEqual(len(checks), 3)
            self.assertEqual(L.scalar("SELECT count(*) FROM gate_kinds WHERE value='feature-pick'"), 1)
        catalog = (REFS / "domains.md").read_text(encoding="utf-8")
        entry = [e for e in catalog.split("\n\n") if e.startswith("**Features")][0]
        for need in ("`features.md`", "propose-feature", "done-test", "not a defect", "feature-pick"):
            self.assertIn(need, " ".join(entry.split()))
        self.assertIn("Which capabilities do you already plan?", catalog)

    def test_scan_proposes_features_once_goals_are_approved(self):
        repo, L = with_goal(self, include=False)
        scan = {d["code"]: d for d in json.loads(L.run_ok("scan", "--review", "R-0001", "--json").stdout)["domains"]}
        self.assertEqual(scan["features"]["decision"], "included")
        self.assertIn("goals = 1", scan["features"]["reason"])

    def test_prompt_is_the_features_template(self):
        repo, L = with_goal(self)
        ok(L, "snapshot", "R-0001", "--actor", "A-0001")
        ok(L, "add-requirement", "Every request gets an answer.", "--source", "owner, chat", "--review", "R-0001")
        text = L.run_ok("prompt", "--review", "R-0001", "--domain", "features").stdout
        for need in ("G-01 Every request is kept for later.", "REQ-0001 Every request gets an answer.",
                     "## Proposals", "- Done-test:", "not defects"):
            self.assertIn(need, text)
        self.assertNotIn("Known issues of this domain", text)


class Proposals(unittest.TestCase):
    def test_feature_proposal_names_goal(self):
        repo, L = with_goal(self)
        checks = "\n".join(f"| {r['code']} | yes | the goals table |" for r in
                           L.all("SELECT code FROM domain_checks WHERE domain_code='features' ORDER BY position"))
        good = L.dir / "features.md"
        good.write_text(GOOD.format(checks=checks), encoding="utf-8")
        p = L.run("lint-report", str(good), "--domain", "features", "--review", "R-0001")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("1 proposal", p.stdout)
        for cut, why in (("- Goal: G-01\n", "names no goal"), ("- Done-test: test_history_lists_handled_requests: "
                                                                 "two requests handled, both listed in order\n",
                                                                 "no done-test"),
                         ("- Value: high\n", "value")):
            bad = L.dir / "bad.md"
            bad.write_text(GOOD.format(checks=checks).replace(cut, ""), encoding="utf-8")
            p = L.run("lint-report", str(bad), "--domain", "features", "--review", "R-0001")
            self.assertEqual(p.returncode, 1, p.stdout)
            self.assertIn(why, p.stdout)
        bad.write_text(GOOD.format(checks=checks).replace("- Goal: G-01", "- Goal: G-09"), encoding="utf-8")
        self.assertIn("G-09 is not an approved goal", L.run("lint-report", str(bad), "--domain", "features",
                                                           "--review", "R-0001").stdout)
        # the command refuses the same: no goal, an unapproved goal, no done-test
        args = propose(L)
        self.assertIn("give --goal or --requirement", refused(self, L, *[a for a in args if a not in ("--goal", "G-01")]))
        self.assertIn("G-07", refused(self, L, *propose(L, goal=("--goal", "G-07"))))
        self.assertIn("--done-test", refused(self, L, *propose(L, done_test=" ")))
        self.assertIn("--value", refused(self, L, *propose(L, value="huge")))
        self.assertEqual(ok(L, *args), ["FP-0001"])
        got = L.one("SELECT * FROM feature_proposals")
        self.assertEqual((got["code"], got["status"], got["value"], got["effort"]), ("FP-0001", "proposed", "high",
                                                                                     "medium"))
        self.assertNotIn("severity", got.keys())

    def test_feature_proposals_not_counted_as_issues(self):
        repo, L = with_goal(self)
        before_check = L.run("check", "--quiet")
        tables = ("issues", "findings", "events", "step_findings")
        before = counts(L, *tables)
        views = ("v_open_issues", "v_review_totals", "v_review_summary", "v_unmatched_findings", "v_uncovered_findings",
                 "v_coverage_problems", "v_recurring_issues", "v_owner_queue")
        snap = lambda: {v: L.all(f"SELECT * FROM {v}") for v in views}
        L.commit()
        seen = snap()
        ok(L, *propose(L))
        ok(L, *propose(L, title="Answer in the reader's language", value="low", effort="large"))
        self.assertEqual(counts(L, *tables), before)
        L.commit()
        self.assertEqual(snap(), seen)          # no count, total, regression or queue moves
        self.assertEqual(L.scalar("SELECT count(*) FROM v_open_issues"), 0)
        self.assertEqual(L.run("check", "--quiet").returncode, before_check.returncode)
        # a picked proposal becomes a plan step that closes no finding, and coverage stays clean
        ok(L, "feature-status", "FP-0001", "pick", "--note", "the owner wants it", "--actor", "A-0002")
        ok(L, "new-plan", "--title", "Plan", "--review", "R-0001", "--actor", "A-0001")
        steps = L.dir / "steps.jsonl"
        steps.write_text("\n".join(json.dumps(o) for o in (
            {"kind": "phase", "phase": 1, "title": "Add", "goal": "Build the picked ideas."},
            {"key": "F", "phase": 1, "title": "Keep a history of handled requests", "change": "Add the store.",
             "proof": "test_history", "features": ["FP-0001"]})) + "\n", encoding="utf-8")
        ok(L, "import-steps", "P-0001", steps, "--actor", "A-0001")
        L.commit()
        self.assertEqual(L.scalar("SELECT count(*) FROM v_coverage_problems"), 0)
        self.assertEqual(L.one("SELECT s.number FROM feature_proposals f JOIN steps s ON s.id = f.step_id "
                               "WHERE f.code='FP-0001'")["number"], 1)
        listed = json.loads(L.run_ok("features", "--review", "R-0001", "--json").stdout)["proposals"]
        self.assertEqual([(p["code"], p["status"], p["step"]) for p in listed],
                         [("FP-0001", "picked", "P-0001#1"), ("FP-0002", "proposed", None)])
        self.assertIn("FP-0002", L.run_ok("features", "--status", "proposed").stdout)
        self.assertNotIn("FP-0001", L.run_ok("features", "--status", "proposed").stdout)
        self.assertEqual(L.scalar("SELECT count(*) FROM issues"), 0)

    def test_declined_proposal_not_reproposed(self):
        repo, L = with_goal(self)
        ok(L, *propose(L))
        self.assertIn("the owner", refused(self, L, "feature-status", "FP-0001", "decline", "--note", "not now",
                                           "--actor", "A-0001"))      # a manager does not decide
        self.assertIn("give --note", refused(self, L, "feature-status", "FP-0001", "decline", "--actor", "A-0002"))
        ok(L, "feature-status", "FP-0001", "decline", "--note", "not what this tool is for", "--actor", "A-0002")
        err = refused(self, L, *propose(L, title="  keep a HISTORY of handled requests "))
        self.assertIn("FP-0001", err)
        self.assertIn("declined", err)
        self.assertEqual(L.scalar("SELECT count(*) FROM feature_proposals"), 1)
        # the goal it served changed: retired and replaced by a new goal, the idea may come back
        ok(L, "propose-goal", "--review", "R-0001", "--source", "owner, chat", "Every request is kept a year.",
           "--actor", "A-0001")
        ok(L, "approve-goals", "G-02", "--actor", "A-0002")
        self.assertEqual(ok(L, *propose(L, goal=("--goal", "G-02"))), ["FP-0002"])
        with sqlite3.connect(str(L.db)) as con:     # the database refuses it too
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                con.execute("""INSERT INTO feature_proposals(code, review_id, goal_id, title, today, evidence, proposal,
                               value, effort, done_test) SELECT 'FP-0009', 1, 1, 'Keep a history of handled requests',
                               't', 'absent', 'p', 'low', 'small', 'd'""")
            self.assertIn("declined", str(cm.exception))
        hist = L.all("""SELECT from_status, to_status, 'A-' || printf('%04d', c.actor_id) actor FROM feature_status_changes c
                        WHERE proposal_id = 1 ORDER BY c.id""")
        self.assertEqual([(h["from_status"], h["to_status"], h["actor"]) for h in hist],
                         [(None, "proposed", "A-0001"), ("proposed", "declined", "A-0002")])

    def test_lifecycle_and_pick_gate(self):
        repo, L = with_goal(self)
        self.assertIn("no proposed", refused(self, L, "feature-gate", "--review", "R-0001", "--actor", "A-0001"))
        ok(L, *propose(L))
        ok(L, *propose(L, title="Answer in the reader's language", value="low", effort="small"))
        gate = ok(L, "feature-gate", "--review", "R-0001", "--actor", "A-0001")[0]
        g = L.one("SELECT * FROM gates WHERE code=?", (gate,))
        self.assertEqual((g["kind"], g["blocking"], g["status"]), ("feature-pick", 0, "open"))
        self.assertIn("FP-0001", g["context"])
        self.assertIn("already", refused(self, L, "feature-gate", "--review", "R-0001", "--actor", "A-0001"))
        self.assertIn("built", refused(self, L, "feature-status", "FP-0001", "built", "--note", "x",
                                       "--actor", "A-0001"))      # only a picked proposal is built
        self.assertIn("not answered", refused(self, L, "feature-status", "FP-0001", "pick", "--gate", gate,
                                              "--note", "x", "--actor", "A-0001"))
        ok(L, "answer-gate", gate, "--text", "FP-0001", "--actor", "A-0001")
        ok(L, "feature-status", "FP-0001", "pick", "--gate", gate, "--note", f"picked in {gate}", "--actor", "A-0001")
        ok(L, "apply-gate", gate, "--note", "FP-0001 picked; FP-0002 stays proposed", "--actor", "A-0001")
        got = L.one("""SELECT f.status, a.kind, f.decided_at IS NOT NULL decided, g.code gate FROM feature_proposals f
                       JOIN actors a ON a.id = f.decided_by_actor_id LEFT JOIN gates g ON g.id = f.gate_id
                       WHERE f.code='FP-0001'""")
        self.assertEqual(tuple(got.values()), ("picked", "owner", 1, gate))
        ok(L, "feature-status", "FP-0001", "built", "--note", "test_history passes", "--actor", "A-0001")
        self.assertIn("built", refused(self, L, "feature-status", "FP-0001", "decline", "--note", "x",
                                       "--actor", "A-0002"))
        text = L.run_ok("features").stdout
        self.assertIn("FP-0001", text)
        self.assertIn("Built", text)

    def test_ideas_route_and_hub(self):
        repo, L = with_goal(self)
        ok(L, *propose(L))
        ok(L, *propose(L, title="Answer in the reader's language", value="low", effort="small"))
        ok(L, *propose(L, title="Mail a weekly summary", value="high", effort="small"))
        L.commit()
        mod = L.module
        con = mod.connect(L.db)
        try:
            d = mod.route(L.root, L.db, con, "/api/ideas", {}, None)
        finally:
            con.close()
        self.assertEqual([g["goal"] for g in d["goals"]], ["G-01"])
        self.assertEqual([p["code"] for p in d["goals"][0]["proposals"]], ["FP-0003", "FP-0001", "FP-0002"])
        self.assertEqual(d["counts"], {"proposed": 3})
        L.run_ok("progress-page")
        hub = (L.dir / "hub.html").read_text(encoding="utf-8")
        self.assertIn('"ideas"', hub)
        self.assertIn("Mail a weekly summary", hub)


class FromV16(unittest.TestCase):
    def test_migration_seeds_features(self):
        from dev.tests.test_w6_schema_order import v16_ledger_py
        old = v16_ledger_py()
        if old is None:
            self.skipTest("git cannot give the schema 16 sources")
        project_dir = fixtures.temp_dir("v16-feat-") / "proj"
        project_dir.mkdir()
        self.assertEqual(fixtures.run_ledger("--root", project_dir, "init", ledger_py=old).returncode, 0)
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0019-feature-proposals", p.stdout)
        con = sqlite3.connect(str(project_dir / ".code-review" / "ledger.sqlite"))
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT report_file FROM domains WHERE code='features'").fetchone()[0],
                         "features.md")
        self.assertGreaterEqual(con.execute("SELECT count(*) FROM domain_checks WHERE domain_code='features'")
                                .fetchone()[0], 3)
        self.assertEqual(con.execute("SELECT count(*) FROM feature_proposals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
