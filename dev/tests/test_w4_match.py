"""match --auto (plan P-0001 step 140; OWN-9, K31): a finding is matched by the tool when exactly one issue in an
active status fits a rule (the same fingerprint; the same domain and sub; the same symbol in the same file) and no
closed issue does, as still-present with the rule in match_rationale. Everything else is listed for the agent with
the candidates ranked by the same rules, a closed candidate first with the type the ledger requires. Two runs on
the same ledger make the same matches and print the same text."""
import json
import unittest

from dev.tests._p5_writes_helpers import ok, project, refused, index_row

APP = "".join(f"line {n}\n" for n in range(1, 11)).replace("line 5\n", "def handler(request):\n") \
    .replace("line 8\n", "def parse(text):\n")


def load(L, review, run, rows):
    f = L.dir / "reviews" / review / f"{run}.tsv"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(rows) + "\n", encoding="utf-8")
    ok(L, "load-index", review, run, f, "--actor", "A-0001")


def second_review(L):
    ok(L, "review-status", "R-0001", "verifying", "--note", "n")
    ok(L, "review-status", "R-0001", "indexing", "--note", "n")
    ok(L, "review-status", "R-0001", "consolidating", "--note", "n")
    ok(L, "review-status", "R-0001", "handed-off", "--note", "n")
    ok(L, "new-review", "--title", "Second", "--scope", "second", "--previous", "R-0001", "--actor", "A-0001")
    ok(L, "review-domain", "R-0002", "spec", "include", "--reason", "again")
    ok(L, "review-domain", "R-0002", "tests", "include", "--reason", "again")
    ok(L, "review-status", "R-0002", "awaiting-approval", "--note", "n")
    ok(L, "review-status", "R-0002", "approved", "--note", "n")
    ok(L, "review-status", "R-0002", "running", "--note", "n")
    ok(L, "add-run", "R-0002", "spec", "spec-B", "--scope", "all", "--model", "m1")


class MatchAuto(unittest.TestCase):
    def setUp(self):
        repo, L, sha = project(self, files={"src/app.py": APP, "src/other.py": APP})
        self.L = L
        load(L, "R-0001", "spec-A", [
            index_row("SPEC-1", sub="empty-request", locations="demo:src/app.py:5#handler"),
            index_row("SPEC-2", sub="quoted-commas", locations="demo:src/app.py:8#parse"),
            index_row("SPEC-3", sub="old-thing", locations="demo:src/other.py:2"),
            index_row("SPEC-4", sub="twin-a", locations="demo:src/other.py:5#handler"),
            index_row("SPEC-5", sub="twin-b", locations="demo:src/other.py:5#handler")])
        for p in ("SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4", "SPEC-5"):
            ok(L, "match", "R-0001", p, "new", "--type", "new", "--rationale", "first review", "--actor", "A-0001")
        ok(L, "issue-status", "ISS-0003", "fixed", "--note", "fixed", "--after", "gone", "--actor", "A-0001")
        second_review(L)
        load(L, "R-0002", "spec-B", [
            index_row("SPEC2-1", sub="empty-request", locations="demo:src/other.py:1"),    # fingerprint: ISS-0001
            index_row("SPEC2-2", sub="renamed", locations="demo:src/app.py:8#parse"),      # symbol: ISS-0002
            index_row("SPEC2-3", sub="old-thing", locations="demo:src/other.py:3"),        # fingerprint: fixed ISS-0003
            index_row("SPEC2-4", sub="brand-new", locations="demo:src/other.py:9"),        # no candidate
            index_row("SPEC2-5", sub="twin-c", locations="demo:src/other.py:5#handler")])  # ISS-0004 and ISS-0005

    def test_match_auto(self):
        L = self.L
        out = L.run_ok("match", "R-0002", "--auto", "--actor", "A-0001").stdout
        L.commit()
        got = {r["point_id"]: (r["issue"], r["match_type"], r["match_confidence"], r["match_rationale"])
               for r in L.all("""SELECT f.point_id, i.code issue, f.match_type, f.match_confidence, f.match_rationale
                                 FROM findings f LEFT JOIN issues i ON i.id = f.issue_id
                                 JOIN reviews r ON r.id = f.review_id WHERE r.code = 'R-0002'""")}
        self.assertEqual(got["SPEC2-1"], ("ISS-0001", "still-present", "certain", "match --auto: same fingerprint spec:validation:empty-request"))
        self.assertEqual(got["SPEC2-2"], ("ISS-0002", "still-present", "certain", "match --auto: same symbol parse in src/app.py"))
        for p in ("SPEC2-3", "SPEC2-4", "SPEC2-5"):
            self.assertEqual(got[p][0], None, p)
        self.assertIn("matched 2 points; 3 left for you", out)
        self.assertIn("SPEC2-3: a closed issue fits: judge the type\n  ISS-0003 (fixed) same fingerprint "
                      "spec:validation:old-thing: --type regression", out)
        self.assertIn("SPEC2-4: no candidate: match it new", out)
        five = out[out.index("SPEC2-5"):]
        self.assertIn("ISS-0004 (open) same symbol handler in src/other.py", five)
        self.assertIn("ISS-0005 (open) same symbol handler in src/other.py", five)
        self.assertLess(out.index("SPEC2-3"), out.index("SPEC2-4"))
        with self.subTest("a second run matches nothing more"):
            again = L.run_ok("match", "R-0002", "--auto", "--actor", "A-0001").stdout
            self.assertIn("matched 0 points; 3 left for you", again)

    def test_match_auto_is_repeatable(self):
        L = self.L
        first = L.run_ok("match", "R-0002", "--auto", "--dry-run", "--json").stdout
        self.assertEqual(L.run_ok("match", "R-0002", "--auto", "--dry-run", "--json").stdout, first)
        self.assertEqual(L.one("SELECT count(*) n FROM findings WHERE issue_id IS NOT NULL AND point_id LIKE 'SPEC2-%'")["n"], 0)
        plan = json.loads(first)
        self.assertEqual([m["point"] for m in plan["matched"]], ["SPEC2-1", "SPEC2-2"])
        self.assertEqual([m["point"] for m in plan["left"]], ["SPEC2-3", "SPEC2-4", "SPEC2-5"])
        self.assertEqual(plan["left"][0]["candidates"][0], {"issue": "ISS-0003", "status": "fixed",
                                                           "rule": "same fingerprint spec:validation:old-thing",
                                                           "type": "regression"})
        done = L.run_ok("match", "R-0002", "--auto", "--json", "--actor", "A-0001").stdout
        self.assertEqual(json.loads(done)["matched"], plan["matched"])

    def test_match_auto_refusals(self):
        L = self.L
        self.assertIn("--auto takes no POINT", refused(self, L, "match", "R-0002", "SPEC2-1", "--auto"))
        self.assertIn("no review R-0009", refused(self, L, "match", "R-0009", "--auto"))


if __name__ == "__main__":
    unittest.main()
