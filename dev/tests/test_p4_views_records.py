"""Plan P-0001 phase 4, new records and catalogs (steps 86, 87, 94): the requirement trace, every agent of a review,
domain descriptions and the metric catalog, through the views and the viewer API; and migration 0004 on a v1 ledger.
"""
import re
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, run_ledger, seed_demo, temp_dir

SKILL_MD = fixtures.REPO_ROOT / "SKILL.md"


def route(L, path):
    con = L.module.connect(L.db)
    try:
        res = L.module.route(L.root, L.db, con, path, {}, None)
    finally:
        con.close()
    return res


def skill_applies():
    """{report file: the italic 'applies' sentence} from SKILL.md's domain catalog."""
    text = SKILL_MD.read_text(encoding="utf-8")
    return {m.group(1): " ".join(m.group(2).split())
            for m in re.finditer(r"\*\*[^*]+?, `([^`]+)`\.\*\*\s*\*([^*]*[Aa]ppl[^*]*)\*", text)}


class RequirementTrace(unittest.TestCase):
    VERDICTS = ["matches"] * 5 + ["differs"] * 2 + ["partly"] * 2 + ["missing"] + ["untested"] * 2

    def test_requirement_trace_counts_match_report(self):
        with Ledger(temp_dir("p4v-")) as L:
            r = L.review("R-0001")
            f = L.finding(r, "SPEC-M1")
            for n, verdict in enumerate(self.VERDICTS, 1):
                q = L.insert("requirements", code=f"REQ-{n:04d}", text=f"Requirement {n}.", source="spec.md")
                L.insert("review_requirement_checks", review_id=r, requirement_id=q, verdict=verdict,
                         where_implemented=f"src/m{n}.py", tested="no" if verdict == "untested" else "yes",
                         finding_id=f if verdict == "differs" else None, evidence=f"Line {n}.")
            L.commit()
            rows = L.all("SELECT * FROM v_requirement_trace WHERE review='R-0001'")
            self.assertEqual(len(rows), 12)
            counts = {v: self.VERDICTS.count(v) for v in set(self.VERDICTS)}
            for row in rows:
                self.assertEqual(row["verdict_count"], counts[row["verdict"]], row)
            self.assertEqual([r["requirement"] for r in rows], [f"REQ-{n:04d}" for n in range(1, 13)])
            self.assertEqual({r["point_id"] for r in rows if r["verdict"] == "differs"}, {"SPEC-M1"})
            with self.subTest("one verdict per review and requirement"):
                with self.assertRaises(sqlite3.IntegrityError):
                    L.insert("review_requirement_checks", review_id=r, requirement_id=1, verdict="matches")
            with self.subTest("verdict and tested words are checked"):
                for bad in ({"verdict": "fine"}, {"verdict": "matches", "tested": "maybe"}):
                    with self.assertRaises(sqlite3.IntegrityError):
                        L.insert("review_requirement_checks", review_id=L.review("R-0002"), requirement_id=1, **bad)
            d = route(L, "/api/review/R-0001")
        self.assertEqual(len(d["requirements"]), 12)
        self.assertEqual(d["requirements"][0]["requirement"], "REQ-0001")
        self.assertEqual(d["requirements"][0]["verdict_count"], 5)


class ReviewAgents(unittest.TestCase):
    def test_review_lists_every_subagent(self):
        with Ledger() as L:
            ids = seed_demo(L)
            r1 = ids["reviews"]["R-0001"]
            mgr = ids["actors"]["manager"]
            for kind, role in (("indexer", "indexer for security.md"), ("matcher", "matcher")):
                a = L.actor(kind, f"{kind} 1", model="claude-opus-5-5", harness="Claude Code", parent_actor_id=mgr)
                L.insert("review_actors", review_id=r1, actor_id=a, role_label=role)
            L.commit()
            d = route(L, "/api/review/R-0001")
        agents = [(a["kind"], a["role"], a["model"]) for a in d["agents"]]
        self.assertEqual([k for k, _, _ in agents], ["manager", "reviewer", "reviewer", "indexer", "matcher"], agents)
        self.assertTrue(all(m for _, _, m in agents), agents)
        self.assertEqual({a["parent"] for a in d["agents"] if a["kind"] != "manager"}, {"Review manager"})
        self.assertIn(("indexer", "indexer for security.md", "claude-opus-5-5"), agents)


class Catalogs(unittest.TestCase):
    def test_seeded_domains_have_descriptions(self):
        applies = skill_applies()
        with Ledger(temp_dir("p4v-")) as L:
            domains = L.all("SELECT code, report_file, description FROM domains")
        self.assertEqual(len(domains), 27)
        for d in domains:
            with self.subTest(domain=d["code"]):
                self.assertTrue(d["description"] and d["description"].strip())
                if d["report_file"] in applies:   # SKILL.md may later point to the table instead
                    self.assertEqual(d["description"], applies[d["report_file"]])

    def test_metric_names_have_labels(self):
        with Ledger() as L:
            seed_demo(L)
            r = L.scalar("SELECT id FROM reviews WHERE code='R-0002'")
            L.metric(r, "brand_new_metric", 3.0, unit="ms")
            L.commit()
            defs = {row["name"]: row for row in L.all("SELECT * FROM metric_defs")}
            used = {row["name"] for row in L.all("SELECT DISTINCT name FROM metrics")}
            d = route(L, "/api/trends")
            rev = route(L, "/api/review/R-0002")
        # the names the domains and the schema ask for
        for name in ("mutation_score", "line_coverage", "test_cases_asserted", "page_weight", "queries_per_page",
                     "p95_job_ms", "dependency_count"):
            self.assertIn(name, defs)
        self.assertEqual(used - set(defs), set())
        self.assertEqual(defs["brand_new_metric"]["label"], "Brand new metric")   # added by tr_metric_defined
        self.assertEqual(defs["brand_new_metric"]["unit"], "ms")
        labels = {m["name"]: (m["label"], m["better"]) for m in d["metrics"]}
        self.assertEqual(labels["mutation_score"], ("Mutation score", "higher"))
        self.assertEqual({m["name"]: m["label"] for m in rev["metrics"]}["mutation_score"], "Mutation score")


class Migration0004(unittest.TestCase):
    def test_migrated_v1_has_catalogs_and_checks(self):
        """A v1 ledger migrated to the shipped schema gets the seeded checks, domain descriptions, and a catalog row
        for each metric name it already holds."""
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        try:
            names = {r[0] for r in con.execute("SELECT DISTINCT name FROM metrics")}
        finally:
            con.close()
        p = run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        con = sqlite3.connect(str(db))
        try:
            self.assertLessEqual({"v_unmatched_findings", "v_unchecked_known_issues", "v_untraceable_refs",
                                  "v_uncovered_findings", "v_coverage_problems", "v_unguarded_fixes"},
                                 {r[0] for r in con.execute("SELECT view FROM ledger_checks")})
            self.assertEqual(con.execute("SELECT count(*) FROM domains WHERE description IS NULL").fetchone()[0], 0)
            have = {r[0] for r in con.execute("SELECT name FROM metric_defs")}
        finally:
            con.close()
        self.assertEqual(names - have, set())


if __name__ == "__main__":
    unittest.main()
