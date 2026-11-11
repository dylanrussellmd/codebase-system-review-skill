"""import-findings, import-checks, load-index and match (plan P-0001 step 106: WALK-H3, WALK-L17, WALK-R11,
WALK-R12, ABS-H3)."""
import json
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import counts, index_row, ok, project, refused

TABLES = ("findings", "finding_locations", "finding_goals", "finding_requirements", "review_issue_checks", "events")


def tsv(*rows):
    f = fixtures.temp_dir("tsv-") / "points.tsv"
    f.write_text("".join(r + "\n" for r in rows))
    return f


class ImportFindingsTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.L, self.sha = project(self, files={"src/app.py": "a\nb\nc\ndef go():\n  pass\n",
                                                           "README.md": "# hi\n"})

    def test_import_findings_rejects_bad_tsv(self):
        L = self.L
        good = index_row("SPEC-M1")
        before = counts(L, *TABLES)
        cases = [("\t".join(index_row("SPEC-M2").split("\t")[:15]), "15 columns, expected 16"),   # a column short
                 (index_row("SPEC-M1"), "id used twice"),
                 (index_row("SPEC-M2", theme="nonsense"), "unknown theme 'nonsense'"),
                 (index_row("SPEC-M2", domain="nonsense"), "unknown domain 'nonsense'"),
                 (index_row("SPEC-M2", locations="demo:src/missing.py:1-2"), "does not exist at the reviewed commit"),
                 (index_row("SPEC-M2", locations="demo:../etc/passwd"), "must be relative"),
                 (index_row("SPEC-M2", sev="X"), "severity 'X'"),
                 (index_row("SPEC-M2", related="SPEC-Z9"), "related id SPEC-Z9"),
                 (index_row("SPEC-M2", goals="G-09"), "unknown design goal G-09")]
        for bad, why in cases:
            err = refused(self, L, "import-findings", "R-0001", "spec-A", tsv(good, bad))
            self.assertIn(why, err)
            self.assertEqual(counts(L, *TABLES), before, why)
        self.assertIn("no reviewer run 'nope'", refused(self, L, "import-findings", "R-0001", "nope", tsv(good)))

    def test_load_index_roundtrip(self):
        """Three rows: one with two locations (the first primary), one about a whole file, one with a symbol only;
        related ids become JSON; goals and requirement verdicts are linked; known.tsv fills review_issue_checks with
        the finding it names."""
        L = self.L
        L.design_goal("G-01")
        L.insert("requirements", code="REQ-0001", text="It must refuse junk.", source="owner")
        prior = L.review("R-0002", status="handed-off", commit_sha=self.sha)   # an older review's known issue
        L.x("UPDATE reviews SET started_at='2026-01-01T00:00:00.000Z' WHERE id=?", (prior,))
        L.issue("ISS-0001", first_seen_review_id=prior, opened_at="2026-01-01T00:00:00.000Z")
        L.commit()
        points = tsv(index_row("SPEC-M1", locations="demo:src/app.py:2-3;demo:src/app.py:4#go", related="SPEC-L1",
                               goals="G-01", requirements="REQ-0001=differs", known="ISS-0001"),
                     index_row("SPEC-L1", sev="L", kind="docs", theme="docs-repo", locations="demo:README.md",
                               related="SPEC-M1, SPEC-M3"),
                     index_row("SPEC-M3", locations="src/app.py#go", domain="tests", kind="test", sev="T"))
        known = fixtures.temp_dir("known-") / "known.tsv"
        known.write_text("ISS-0001\tstill-present\tline 2 still accepts junk\tSPEC-M1\n")
        out = ok(L, "load-index", "R-0001", "spec-A", points, known)
        self.assertEqual(out[0], "R-0001 spec-A: loaded 3 points (L 1, M 1, T 1) unmatched; next: fill-refs, then match")
        f = {r["point_id"]: r for r in L.all("SELECT * FROM findings")}
        self.assertEqual(f["SPEC-M3"]["domain_code"], "tests")
        self.assertEqual(f["SPEC-M1"]["domain_code"], "spec")   # from the run
        self.assertEqual(json.loads(f["SPEC-L1"]["related_point_ids"]), ["SPEC-M1", "SPEC-M3"])
        self.assertIsNone(f["SPEC-M1"]["issue_id"])
        locs = L.all("""SELECT f.point_id, fl.path, fl.line_start, fl.line_end, fl.symbol, fl.role, fl.whole_file,
                        fl.commit_sha FROM finding_locations fl JOIN findings f ON f.id=fl.finding_id ORDER BY fl.id""")
        self.assertEqual([(l["point_id"], l["line_start"], l["line_end"], l["symbol"], l["role"], l["whole_file"])
                          for l in locs],
                         [("SPEC-M1", 2, 3, None, "primary", 0), ("SPEC-M1", 4, 4, "go", "also", 0),
                          ("SPEC-L1", None, None, None, "primary", 1), ("SPEC-M3", None, None, "go", "primary", 0)])
        self.assertTrue(all(l["commit_sha"] == self.sha for l in locs))
        self.assertEqual(L.scalar("SELECT count(*) FROM finding_goals"), 1)
        self.assertEqual(L.scalar("SELECT verdict FROM finding_requirements"), "differs")
        self.assertEqual(L.scalar("SELECT finding_id FROM review_issue_checks"), f["SPEC-M1"]["id"])
        ok(L, "fill-refs", "R-0001")
        self.assertEqual(L.scalar("SELECT line_start FROM finding_locations WHERE symbol='go' AND role='primary'"), 4)

    def known_issue(self):
        """ISS-0001, first seen by an earlier review R-0002 (a review checks only issues it did not find)."""
        prior = self.L.review("R-0002", status="handed-off", commit_sha=self.sha)
        self.L.x("UPDATE reviews SET started_at='2026-01-01T00:00:00.000Z' WHERE id=?", (prior,))
        self.L.issue("ISS-0001", first_seen_review_id=prior, opened_at="2026-01-01T00:00:00.000Z")
        self.L.commit()

    def test_load_known_sets_finding_id(self):
        L = self.L
        self.known_issue()
        ok(L, "import-findings", "R-0001", "spec-A", tsv(index_row("SPEC-M1")))
        known = fixtures.temp_dir("known-") / "known.tsv"
        known.write_text("ISS-0001\tnot-checked\t\t\n")
        before = counts(L, "review_issue_checks")
        bad = fixtures.temp_dir("known-") / "bad.tsv"
        bad.write_text("ISS-0001\tfixed\t\tSPEC-M1\n")
        self.assertIn("a fixed check needs evidence", refused(self, L, "import-checks", "R-0001", bad))
        bad.write_text("ISS-0001\tstill-present\tseen\tSPEC-Q1\n")
        self.assertIn("no point SPEC-Q1", refused(self, L, "import-checks", "R-0001", bad))
        bad.write_text("ISS-0001\tstill-present\tseen\n")
        self.assertIn("3 columns, expected 4", refused(self, L, "import-checks", "R-0001", bad))
        self.assertEqual(counts(L, "review_issue_checks"), before)
        known.write_text("ISS-0001\tstill-present\tline 2\tSPEC-M1\n")
        ok(L, "import-checks", "R-0001", known, "--run", "spec-A")
        self.assertEqual(L.one("SELECT finding_id, actor_id, outcome FROM review_issue_checks"),
                         {"finding_id": 1, "actor_id": L.scalar("SELECT actor_id FROM reviewer_runs"),
                          "outcome": "still-present"})

    def test_import_checks_requires_evidence(self):
        L = self.L
        self.known_issue()
        known = fixtures.temp_dir("known-") / "known.tsv"
        for outcome in ("still-present", "changed", "fixed", "regressed", "obsolete"):
            known.write_text(f"ISS-0001\t{outcome}\t\t\n")
            self.assertIn(f"a {outcome} check needs evidence", refused(self, L, "import-checks", "R-0001", known))
        known.write_text("ISS-0001\tnot-checked\t\t\n")   # an honest not-checked needs none
        ok(L, "import-checks", "R-0001", known)

    def test_fingerprint_ignores_symbol(self):
        L = self.L
        ok(L, "import-findings", "R-0001", "spec-A", tsv(
            index_row("SPEC-M1", sub="parser-accepts-junk", locations="demo:src/app.py:4#go"),
            index_row("SPEC-M2", sub="parser-accepts-junk", locations="demo:src/app.py:2-3")))
        fps = [r[0] for r in L.x("SELECT fingerprint FROM findings ORDER BY id")]
        self.assertEqual(fps, ["spec:validation:parser-accepts-junk"] * 2)
        mod = fixtures.load_ledger_module(L.ledger_py)
        self.assertEqual(mod.fingerprint("spec", "validation", "x"), "spec:validation:x")


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.L, self.sha = project(self)
        ok(self.L, "import-findings", "R-0001", "spec-A", tsv(index_row("SPEC-M1"), index_row("SPEC-M2"),
                                                              index_row("SPEC-L1", sev="L")))

    def test_match_new_allocates_next_code(self):
        L = self.L
        self.assertEqual(ok(L, "match", "R-0001", "SPEC-M1", "new", "--confidence", "certain"),
                         ["SPEC-M1 -> ISS-0001 new"])
        self.assertEqual(ok(L, "match", "R-0001", "SPEC-M2", "new"), ["SPEC-M2 -> ISS-0002 new"])
        i = L.one("SELECT title, domain_code, theme_code, sub, severity, fingerprint, before_state, recommendation, "
                  "first_seen_review_id, times_seen FROM issues WHERE code='ISS-0001'")
        self.assertEqual(i, {"title": "Point SPEC-M1 is wrong.", "domain_code": "spec", "theme_code": "validation",
                             "sub": "spec-m1", "severity": "M", "fingerprint": "spec:validation:spec-m1",
                             "before_state": "It accepts anything.", "recommendation": "Validate it.",
                             "first_seen_review_id": 1, "times_seen": 1})
        self.assertEqual(L.scalar("SELECT match_type FROM findings WHERE point_id='SPEC-M1'"), "new")
        self.assertIn("already matched", refused(self, L, "match", "R-0001", "SPEC-M1", "ISS-0001", "--type", "new"))
        self.assertIn("give --type", refused(self, L, "match", "R-0001", "SPEC-L1", "ISS-0001"))
        self.assertIn("relation 'related-to'", refused(self, L, "match", "R-0001", "SPEC-L1", "new", "--relations",
                                                       "related-to"))
        self.assertIn("needs its relation", refused(self, L, "match", "R-0001", "SPEC-L1", "new", "--type",
                                                    "continuation"))
        ok(L, "match", "R-0001", "SPEC-L1", "new", "--relations", "related-to:ISS-0001;caused-by:ISS-0002",
           "--rationale", "same parser")
        self.assertEqual(L.all("SELECT relation, rationale, established_in_review_id FROM issue_relations ORDER BY id"),
                         [{"relation": "related-to", "rationale": "same parser", "established_in_review_id": 1},
                          {"relation": "caused-by", "rationale": "same parser", "established_in_review_id": 1}])
        self.assertEqual(L.count("v_unmatched_findings"), 0)
        self.assertEqual(L.count("v_match_problems"), 0)

    def test_matcher_extra_relations_parsed(self):
        L = self.L
        ok(L, "match", "R-0001", "SPEC-M1", "new")
        f = fixtures.temp_dir("match-") / "m.tsv"
        f.write_text("SPEC-M2\tNEW\tnew\tcertain\tits own problem\trelated-to:ISS-0001\n"
                     "SPEC-L1\tISS-0001\tnew\tlikely\tsame thing\t\n")
        self.assertEqual(ok(L, "match", "R-0001", "--file", f),
                         ["SPEC-M2 -> ISS-0002 new", "SPEC-L1 -> ISS-0001 new", "R-0001: matched 2 points"])
        self.assertEqual(L.scalar("SELECT count(*) FROM issue_relations WHERE relation='related-to'"), 1)
        mod = fixtures.load_ledger_module(L.ledger_py)
        self.assertEqual(mod.parse_relations("related-to:ISS-0001; caused-by:ISS-0002"),
                         [("related-to", "ISS-0001"), ("caused-by", "ISS-0002")])
        f.write_text("SPEC-M2\tISS-0001\tnew\tlikely\tx\tsideways ISS-0002\n")
        before = counts(L, "issues", "issue_relations", "events")
        self.assertIn("is not relation:ISS-0001", refused(self, L, "match", "R-0001", "--file", f))
        self.assertEqual(counts(L, "issues", "issue_relations", "events"), before)

    def test_match_false_finding_creates_obsolete_issue(self):
        L = self.L
        L.x("UPDATE findings SET verification='false', verification_note='the check exists at line 9' "
            "WHERE point_id='SPEC-M2'")
        L.commit()
        self.assertEqual(ok(L, "match", "R-0001", "SPEC-M2", "new"),
                         ["SPEC-M2 -> ISS-0001 new (false finding: closed as obsolete)"])
        i = L.one("SELECT status, resolution, times_seen, resolved_at IS NOT NULL done FROM issues")
        self.assertEqual(i, {"status": "obsolete", "resolution": "false finding: the check exists at line 9",
                             "times_seen": 1, "done": 1})
        self.assertEqual(L.count("v_open_issues"), 0)
        self.assertEqual(L.count("v_unmatched_findings", "point_id='SPEC-M2'"), 0)


if __name__ == "__main__":
    unittest.main()
