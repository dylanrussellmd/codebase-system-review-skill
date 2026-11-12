"""compare-runs (plan P-0001 step 141; OWN-9, K55): how far two runs of a review of the same commit differ in
proposed and approved domains, checklist answers, the issues their findings matched, severities and plan coverage.
The agreement rates are stored as metrics (domain_agreement, finding_agreement, severity_agreement), per domain and
overall, so drift is measured over time."""
import json
import unittest

from dev.tests._p5_writes_helpers import ok, project, refused, index_row

APP = "".join(f"line {n}\n" for n in range(1, 21))


def run_review(L, code, run, points, matches, domains=("spec", "tests")):
    """A rerun of the same commit: new-review, the domains, approval, a run, its points loaded and matched."""
    if code != "R-0001":
        ok(L, "new-review", "--title", f"Run {code}", "--scope", "full", "--actor", "A-0001")
        for d in domains:
            ok(L, "review-domain", code, d, "include", "--reason", "rerun")
        ok(L, "review-status", code, "awaiting-approval", "--note", "n")
        ok(L, "review-status", code, "approved", "--note", "n")
        ok(L, "review-status", code, "running", "--note", "n")
        ok(L, "add-run", code, "spec", run, "--scope", "all", "--model", "m1")
    f = L.dir / "reviews" / code / "points.tsv"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(points) + "\n", encoding="utf-8")
    ok(L, "load-index", code, run, f, "--actor", "A-0001")
    for point, target in matches:
        ok(L, "match", code, point, target, "--type", "new" if target == "new" else "still-present", "--rationale",
           "test", "--actor", "A-0001")


class CompareRuns(unittest.TestCase):
    def setUp(self):
        repo, L, sha = project(self, files={"src/app.py": APP})
        self.L = L
        run_review(L, "R-0001", "spec-A", [
            index_row("A-1", sev="H", sub="one", locations="demo:src/app.py:1"),
            index_row("A-2", sev="M", sub="two", locations="demo:src/app.py:2"),
            index_row("A-3", sev="L", sub="three", locations="demo:src/app.py:3")],
            [("A-1", "new"), ("A-2", "new"), ("A-3", "new")])
        run_review(L, "R-0002", "spec-B", [
            index_row("B-1", sev="H", sub="one", locations="demo:src/app.py:1"),
            index_row("B-2", sev="L", sub="two", locations="demo:src/app.py:2"),
            index_row("B-4", sev="M", sub="four", locations="demo:src/app.py:4")],
            [("B-1", "ISS-0001"), ("B-2", "ISS-0002"), ("B-4", "new")], domains=("spec", "security"))

    def test_compare_runs(self):
        L = self.L
        out = json.loads(L.run_ok("compare-runs", "R-0001", "R-0002", "--json").stdout)
        self.assertTrue(out["same_commits"])
        d = out["domains"]
        self.assertEqual((d["shared"], d["only_a"], d["only_b"]), (["spec"], ["tests"], ["security"]))
        self.assertAlmostEqual(d["agreement"], 33.3, places=1)
        f = out["findings"]
        self.assertEqual((f["shared"], f["only_a"], f["only_b"]), (["ISS-0001", "ISS-0002"], ["ISS-0003"], ["ISS-0004"]))
        self.assertEqual(f["agreement"], 50.0)
        self.assertEqual(out["severity"]["changed"], [{"issue": "ISS-0002", "a": "M", "b": "L"}])
        self.assertEqual(out["severity"]["agreement"], 50.0)
        self.assertEqual(out["by_domain"]["spec"]["finding_agreement"], 50.0)
        self.assertEqual(out["plan_coverage"], {"a": None, "b": None})
        self.assertEqual(L.run_ok("compare-runs", "R-0001", "R-0002", "--json").stdout,
                         L.run_ok("compare-runs", "R-0001", "R-0002", "--json").stdout)
        text = L.run_ok("compare-runs", "R-0001", "R-0002").stdout
        self.assertIn("findings: 2 shared, 1 only in R-0001, 1 only in R-0002 (agreement 50%)", text)
        self.assertIn("ISS-0002: M in R-0001, L in R-0002", text)

    def test_agreement_metrics_recorded(self):
        L = self.L
        out = L.run_ok("compare-runs", "R-0001", "R-0002", "--record", "--actor", "A-0001").stdout
        self.assertIn("recorded 5 agreement metrics on R-0002", out)
        L.commit()
        got = {(r["name"], r["domain_code"], r["scope"]): (r["value"], r["unit"], r["method"])
               for r in L.all("""SELECT m.* FROM metrics m JOIN reviews r ON r.id = m.review_id WHERE r.code='R-0002'""")}
        self.assertEqual(got[("domain_agreement", None, "vs R-0001")][1:], ("%", "counted"))
        self.assertEqual(got[("finding_agreement", None, "vs R-0001")][0], 50.0)
        self.assertEqual(got[("severity_agreement", None, "vs R-0001")][0], 50.0)
        self.assertEqual(got[("finding_agreement", "spec", "vs R-0001 spec")][0], 50.0)
        defs = {r["name"]: (r["unit"], r["better"]) for r in L.all("SELECT * FROM metric_defs WHERE name LIKE '%_agreement'")}
        self.assertEqual(defs, {n: ("%", "higher") for n in ("domain_agreement", "finding_agreement",
                                                             "severity_agreement")})
        L.run_ok("compare-runs", "R-0001", "R-0002", "--record", "--actor", "A-0001")   # again: replaced, not added
        L.commit()
        self.assertEqual(L.one("SELECT count(*) n FROM metrics WHERE name LIKE '%_agreement'")["n"], 5)

    def test_compare_runs_refusals(self):
        L = self.L
        self.assertIn("no review R-0009", refused(self, L, "compare-runs", "R-0001", "R-0009"))
        self.assertIn("two different reviews", refused(self, L, "compare-runs", "R-0001", "R-0001"))


if __name__ == "__main__":
    unittest.main()
