"""What the pages are given to show (plan P-0001 steps 119, 184, 191, 192, 193, 196, 197, 198): runs, test runs,
files, relations and history on a review; links, sources, questions and discoveries on a plan; a goal's lifecycle;
each check with what the agent does about it; the How it works text; the check for files outside the ledger."""
import re
import sqlite3
import unittest

from dev.tests import _migrate_helpers as M
from dev.tests import _w2_shown_helpers as H
from dev.tests import fixtures
from dev.tests import test_routes as tr
from dev.tests.fixtures import Ledger

RECIPES = (fixtures.REPO_ROOT / "references" / "recipes.md").read_text(encoding="utf-8")


class Case(tr.RouteCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.ids = H.build(cls.L)
        cls.mod = cls.L.module

    @classmethod
    def tearDownClass(cls):
        cls.L.close()


class Review(Case):
    def test_review_api_exposes_runs_requirements_artifacts(self):
        d = self.ok("/api/review/R-0001")
        run = [r for r in d["runs"] if r["id"] == self.ids["run"]][0]
        self.assertEqual((run["started_at"], run["finished_at"], run["report_path"], run["harness"], run["session"]),
                         ("2026-11-01T09:00:00.000Z", "2026-11-01T09:20:00.000Z",
                          ".code-review/reviews/R-0001/security.md", "a test harness", "session-42"))
        self.assertFalse({"prompt", "final_message"} & set(run))            # long text comes when a row is opened
        text = self.ok(f"/api/run/{run['id']}")
        self.assertEqual((text["prompt"], text["final_message"]),
                         ("Review the sign-in code for weak randomness.", "One high point: codes are predictable."))
        self.assertEqual(self.call("/api/run/99999"), (404, {"error": "no such reviewer run"}))
        self.assertEqual((d["requirements"][0]["verdict"], d["requirements"][0]["requirement"]), ("differs", "REQ-0001"))
        self.assertEqual([(a["kind"], a["path"], a["url"], a["run"]) for a in d["artifacts"]],
                         [("brief", ".code-review/reviews/R-0001/BRIEF.md", None, None),
                          ("report", ".code-review/reviews/R-0001/security.md", "https://example.com/reports/security",
                           run["label"])])
        t = d["test_runs"][0]
        self.assertEqual((t["command"], t["purpose"], t["passed"], t["failed"], t["duration_ms"]),
                         ("python -m unittest", "baseline", 41, 2, 12300))
        repo = d["repos"][0]
        self.assertEqual((repo["remote_url"], repo["default_branch"], repo["notes"]),
                         ("https://example.com/demo.git", "main", "The only repository."))
        self.assertEqual((d["review"]["skill_version"], d["review"]["review_folder"]),
                         ("2.4.0", ".code-review/reviews/R-0001"))

    def test_review_api_returns_relations(self):
        d = self.ok("/api/review/R-0002")
        made = [(x["from_issue"], x["relation"], x["to_issue"], x["rationale"]) for x in d["relations"]]
        self.assertIn(("ISS-0002", "blocks", "ISS-0003", "Same function."), made)
        self.assertEqual(len(made), self.L.scalar("SELECT count(*) FROM issue_relations WHERE established_in_review_id=?",
                                                  (self.ids["reviews"]["R-0002"],)))
        self.assertEqual(self.ok("/api/review/R-0001")["relations"], [])
        events = self.ok("/api/review/R-0001")["events"]
        self.assertTrue(events)
        self.assertEqual(set(events[0]), {"at", "event", "from_value", "to_value", "note", "actor"})
        self.assertEqual([e["at"] for e in events], sorted(e["at"] for e in events))

    def test_finding_carries_goals_requirements_and_where_it_was_found(self):
        f = self.ok(f"/api/finding/{self.ids['findings']['R-0001/SPEC-M1']}")
        self.assertEqual([g["code"] for g in f["goals"]], ["G-01"])
        self.assertEqual([(q["code"], q["verdict"]) for q in f["requirements"]], [("REQ-0001", "differs")])
        self.assertEqual((f["discovered_in_step"], f["discovered_in_plan"]), (None, None))
        found = self.ok(f"/api/finding/{self.ids['found']}")
        self.assertEqual((found["discovered_in_step"], found["discovered_in_plan"]), (2, "P-0001"))
        on_issue = self.ok(f"/api/issue/{self.ids['found_issue']}")["findings"][0]
        self.assertEqual((on_issue["discovered_in_step"], on_issue["discovered_in_plan"]), (2, "P-0001"))
        self.assertIn("verified_by", on_issue)


class Plan(Case):
    def test_plan_api_returns_links_and_sources(self):
        d = self.ok("/api/plan/P-0001")
        self.assertEqual((d["plan"]["doc_url"], d["plan"]["page_url"]),
                         ("https://example.com/plan-doc", "https://example.com/plan-page"))
        self.assertEqual([r["code"] for r in d["reviews"]],
                         [r["code"] for r in self.L.all("""SELECT r.code FROM plan_reviews pr JOIN reviews r
                                                           ON r.id=pr.review_id ORDER BY r.started_at""")])
        self.assertTrue(d["reviews"])
        self.assertEqual([(a["kind"], a["path"]) for a in d["artifacts"]], [("plan-file", ".code-review/plans/P-0001.md")])
        self.assertEqual(d["last_updated"], self.L.scalar("SELECT max(updated_at) FROM steps"))
        self.assertEqual([(p["number"], p["first_step"], p["last_step"]) for p in d["phase_rows"]], [(1, 1, 1), (2, 2, 3)])
        self.assertEqual([g["code"] for g in d["gates"]], ["GATE-0001"])     # the open question on step 3
        self.assertEqual(d["gates"][0]["step"], 3)
        self.assertTrue(d["events"])

    def test_plan_lists_discoveries(self):
        d = self.ok("/api/plan/P-0001")
        self.assertEqual([(x["point_id"], x["review"], x["found_in_step"], x["issue"], x["closing_step"])
                          for x in d["discoveries"]], [("FOUND-1", "R-0001", 2, self.ids["found_issue"], 3)])
        self.assertEqual(self.ok("/api/home")["discoveries"], 1)
        L = Ledger()
        self.addCleanup(L.close)
        ids = fixtures.seed_demo(L)
        L.finding(ids["reviews"]["R-0001"], "FOUND-9", summary="Not planned.", discovered_in_step_id=ids["steps"][1])
        L.commit()
        late = self.ok("/api/plan/P-0001", L=L)["discoveries"]
        self.assertEqual([(x["point_id"], x["issue"], x["closing_step"]) for x in late], [("FOUND-9", None, None)])


class Goals(Case):
    def test_goals_list_has_lifecycle(self):
        goals = {g["code"]: g for g in self.ok("/api/goals")["goals"]}
        old = goals[self.L.scalar("SELECT code FROM design_goals WHERE id=?", (self.ids["retired"],))]
        self.assertEqual((old["status"], old["adopted_at"], old["retired_at"]),
                         ("retired", "2026-01-05T10:00:00.000Z", "2026-06-01T10:00:00.000Z"))
        self.assertIsNone(old["used_by"])
        self.assertEqual(goals["G-01"]["used_by"], "R-0001, R-0002")           # the reviews that judged against it
        self.assertIsNone(goals["G-01"]["retired_at"])
        self.assertTrue(goals["G-01"]["adopted_at"])


class Checks(Case):
    def test_every_check_has_recipe(self):
        headings = set(re.findall(r"^#### `([a-z_]+)`$", RECIPES, re.M))
        views = [r["view"] for r in self.L.all("SELECT view FROM ledger_checks")]
        for name in views + ["quick_check", "integrity_check", "foreign_key_check", "check_schema"]:
            self.assertIn(name, headings, f"recipes.md 'A failing check' has no heading for {name}")
        self.assertEqual(set(self.mod.CHECK_ACTIONS), set(views))             # one sentence per check, none stale
        checks = self.ok("/api/checks")["checks"]
        self.assertEqual(len(checks), len(views))
        for c, view in zip(checks, [r["view"] for r in self.L.all(
                "SELECT view FROM ledger_checks ORDER BY position, view")]):
            self.assertEqual(set(c), {"label", "n", "action", "recipe", "link"})   # step 172: what the page shows
            self.assertEqual(c["action"], self.mod.CHECK_ACTIONS[view])
            self.assertRegex(c["action"], r"^[A-Z].*\.$")
            self.assertEqual(c["recipe"], f'references/recipes.md, under "A failing check": {view}')
            self.assertEqual(c["link"] is None, c["n"] == 0, view)

    def test_check_links_to_the_page_with_its_rows(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = fixtures.seed_demo(L)
        L.finding(ids["reviews"]["R-0002"], "SPEC-L9", severity="L", summary="Unmatched probe.")
        L.commit()
        by = {c["recipe"].rsplit(": ", 1)[1]: c for c in self.ok("/api/checks", L=L)["checks"]}
        self.assertEqual(by["v_unmatched_findings"]["link"], "#/review/R-0002?match=none")
        self.assertEqual(by["v_untraceable_refs"]["link"], "#/explore/v_untraceable_refs")
        self.assertIsNone(by["v_coverage_problems"]["link"])

    def test_check_flags_artifact_outside_ledger_folder(self):
        L = Ledger()
        self.addCleanup(L.close)
        ids = fixtures.seed_demo(L)
        r1 = ids["reviews"]["R-0001"]
        inside = L.root / ".code-review" / "reviews" / "R-0001"
        inside.mkdir(parents=True)
        (inside / "BRIEF.md").write_text("brief", encoding="utf-8")
        L.insert("artifacts", review_id=r1, kind="brief", path=".code-review/reviews/R-0001/BRIEF.md")
        L.insert("artifacts", review_id=r1, kind="summary-doc", url="https://example.com/doc")       # a link: fine
        L.insert("artifacts", review_id=r1, kind="probe-script", path="/tmp/scratch/probe.py")       # not evidence
        L.commit()
        self.assertEqual(L.count("v_artifacts_outside_ledger"), 0)
        clean = L.run("check")
        self.assertNotIn("not on disk", clean.stdout)
        L.insert("artifacts", review_id=r1, kind="report", path="/tmp/scratch/review/security.md")
        L.insert("artifacts", review_id=r1, kind="index", path="notes/index.tsv")
        L.insert("artifacts", review_id=r1, kind="report", path=".code-review/reviews/R-0001/gone.md")
        L.commit()
        self.assertEqual([(r["review"], r["kind"], r["path"]) for r in L.all("SELECT * FROM v_artifacts_outside_ledger")],
                         [("R-0001", "report", "/tmp/scratch/review/security.md"), ("R-0001", "index", "notes/index.tsv")])
        p = L.run("check")
        self.assertRegex(p.stdout, r"(?m)^\s+2  Review files recorded outside the ledger's folder  "
                                   r"\(v_artifacts_outside_ledger\)  warning$")
        self.assertIn("a recorded review file is not on disk: .code-review/reviews/R-0001/gone.md", p.stdout)
        self.assertNotIn("not on disk: .code-review/reviews/R-0001/BRIEF.md", p.stdout)
        self.assertEqual(p.returncode, clean.returncode)                      # a warning: it does not fail check
        self.assertEqual(L.scalar("SELECT tier FROM ledger_checks WHERE view='v_artifacts_outside_ledger'"), "warning")
        # a ledger kept in another folder: paths under that folder are inside
        L.x("INSERT OR REPLACE INTO meta(key, value) VALUES ('ledger_location', '/srv/ledgers/demo')")
        L.insert("artifacts", review_id=r1, kind="brief", path="/srv/ledgers/demo/reviews/R-0001/BRIEF.md")
        self.assertNotIn("/srv/ledgers/demo/reviews/R-0001/BRIEF.md",
                         [r["path"] for r in L.all("SELECT path FROM v_artifacts_outside_ledger")])


class Guide(Case):
    def test_guide_route(self):
        text = self.ok("/api/guide")["guide"]
        for part in ("## How a review runs", "## Words used here", "| Point |", "**Waiting on you**"):
            self.assertIn(part, text)
        self.assertNotIn("Claude", text)                                      # whichever agent runs the skill
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            snap = self.mod.hub_snapshot(self.L.root, self.L.db, con)["responses"]
        finally:
            con.close()
        self.assertEqual(snap["guide"], {"guide": text})
        self.assertIn(f"run/{self.ids['run']}", snap)


class Labels(Case):
    def test_event_names_have_labels(self):
        labels = self.ok("/api/overview")["labels"]["enums"]
        self.assertEqual(labels["event"]["checked"], "Looked for by a review")
        used = {r["event"] for r in self.L.all("SELECT DISTINCT event FROM events")}
        self.assertEqual(used - set(labels["event"]), set())
        for enum in ("location_role", "owner_change", "confidence", "metric_method", "verdict", "tested", "actor_kind",
                     "artifact_kind", "test_purpose"):
            self.assertTrue(labels[enum], enum)


class Migration(unittest.TestCase):
    def test_migrate_adds_event_labels_and_the_artifact_check(self):
        project_dir, db = M.restore_v1()
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        c = sqlite3.connect(str(db))
        try:
            fresh = Ledger()
            self.addCleanup(fresh.close)
            want = [tuple(r.values()) for r in fresh.all(
                "SELECT value, label, position FROM enum_labels WHERE enum='event' ORDER BY position")]
            self.assertEqual(c.execute("SELECT value, label, position FROM enum_labels WHERE enum='event' "
                                       "ORDER BY position").fetchall(), want)
            self.assertGreater(len(want), 15)
            self.assertEqual(c.execute("SELECT label, tier, position FROM ledger_checks "
                                       "WHERE view='v_artifacts_outside_ledger'").fetchone(),
                             ("Review files recorded outside the ledger's folder", "warning", 110))
            c.execute("SELECT * FROM v_artifacts_outside_ledger").fetchall()
            self.assertEqual(fixtures.load_ledger_module().verify_schema(c), [])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
