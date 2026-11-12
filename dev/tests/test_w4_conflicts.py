"""The deconfliction pass (plan P-0001 step 143; OWN-11, K33): each step's touches (declared in steps.jsonl, and
the files and symbols of the findings it closes) are kept in step_touches; `ledger.py conflicts` records every pair
of steps that touch the same thing, and why (opposite directions said so); `resolve-conflict` records the judgement.
Unresolved conflicts block plan approval. A step added after approval is checked against the plan: its conflict is
a warning that blocks only that step's start."""
import json
import unittest

from dev.tests._p5_writes_helpers import ok, project, refused, index_row

APP = "".join(f"line {n}\n" for n in range(1, 21)).replace("line 5\n", "def handler(request):\n")


def planned(test, touches=True):
    """R-0001 with three matched points and P-0001 drafted with three steps: 1 and 2 touch the same column in
    opposite directions and the same file (their findings' locations), 3 touches only its own file."""
    repo, L, sha = project(test, files={"src/app.py": APP, "src/other.py": APP})
    f = L.dir / "reviews" / "R-0001" / "points.tsv"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join([index_row("SPEC-1", sub="a", locations="demo:src/app.py:5#handler"),
                            index_row("SPEC-2", sub="b", locations="demo:src/app.py:5#handler"),
                            index_row("SPEC-3", sub="c", locations="demo:src/other.py:2")]) + "\n")
    ok(L, "load-index", "R-0001", "spec-A", f, "--actor", "A-0001")
    for p in ("SPEC-1", "SPEC-2", "SPEC-3"):
        ok(L, "match", "R-0001", p, "new", "--type", "new", "--rationale", "r", "--actor", "A-0001")
    ok(L, "new-plan", "--title", "Plan", "--review", "R-0001", "--actor", "A-0001")
    t1 = [{"kind": "column", "target": "findings.severity", "direction": "tightens"}] if touches else []
    t2 = [{"kind": "column", "target": "findings.severity", "direction": "loosens"}] if touches else []
    steps = L.dir / "steps.jsonl"
    steps.write_text("\n".join(json.dumps(o) for o in (
        {"kind": "phase", "phase": 1, "title": "One", "goal": "G."},
        {"key": "A", "phase": 1, "title": "Tighten", "change": "c", "proof": "p", "closes": ["SPEC-1"], "touches": t1},
        {"key": "B", "phase": 1, "title": "Loosen", "change": "c", "proof": "p", "closes": ["SPEC-2"], "touches": t2},
        {"key": "C", "phase": 1, "title": "Other", "change": "c", "proof": "p", "closes": ["SPEC-3"]})) + "\n")
    ok(L, "import-steps", "P-0001", steps, "--actor", "A-0001")
    return L


def approve(L, expect_ok=True):
    gate = ok(L, "open-gate", "plan-approval", "--plan", "P-0001", "--title", "Approve", "--question", "Go?",
              "--option", "approve=Yes", "--actor", "A-0001")[0]
    ok(L, "add-actor", "owner", "the owner")
    ok(L, "answer-gate", gate, "--value", "approve", "--text", "yes", "--actor", "A-0001")
    ok(L, "apply-gate", gate, "--note", "approved", "--actor", "A-0001")
    ok(L, "plan-status", "P-0001", "awaiting-approval", "--note", "ready", "--actor", "A-0001")
    args = ("plan-status", "P-0001", "approved", "--note", "approved", "--actor", "A-0001")
    return ok(L, *args) if expect_ok else args


class Conflicts(unittest.TestCase):
    def test_touches_recorded(self):
        L = planned(self)
        L.commit()
        got = sorted((r["number"], r["kind"], r["target"], r["direction"], r["source"]) for r in L.all(
            "SELECT s.number, t.* FROM step_touches t JOIN steps s ON s.id = t.step_id"))
        self.assertIn((1, "column", "findings.severity", "tightens", "declared"), got)
        self.assertIn((1, "file", "demo:src/app.py", "changes", "finding"), got)
        self.assertIn((1, "symbol", "demo:src/app.py#handler", "changes", "finding"), got)
        self.assertIn((3, "file", "demo:src/other.py", "changes", "finding"), got)
        with self.subTest("a touch must have a known kind and direction"):
            bad = L.dir / "bad.jsonl"
            bad.write_text(json.dumps({"key": "D", "phase": 1, "title": "t", "change": "c", "proof": "p",
                                       "no_findings_reason": "x", "touches": [{"kind": "planet", "target": "x"}]}) + "\n")
            self.assertIn("kind 'planet'", refused(self, L, "import-steps", "P-0001", bad, "--actor", "A-0001"))

    def test_conflicts_lists_overlaps(self):
        L = planned(self)
        out = L.run_ok("conflicts", "--plan", "P-0001", "--actor", "A-0001").stdout
        self.assertIn("steps 1 and 2: column findings.severity: 1 tightens, 2 loosens", out)
        self.assertIn("steps 1 and 2: file demo:src/app.py: both change it", out)
        self.assertNotIn("steps 1 and 3", out)
        self.assertNotIn("steps 2 and 3", out)
        self.assertIn("P-0001: 3 unresolved conflicts between 1 pair of steps", out)
        again = L.run_ok("conflicts", "--plan", "P-0001", "--actor", "A-0001").stdout
        self.assertEqual(again, out)
        data = json.loads(L.run_ok("conflicts", "--plan", "P-0001", "--json").stdout)
        self.assertEqual([(c["a"], c["b"], c["kind"], c["target"]) for c in data["conflicts"]],
                         [(1, 2, "column", "findings.severity"), (1, 2, "file", "demo:src/app.py"),
                          (1, 2, "symbol", "demo:src/app.py#handler")])
        self.assertTrue(data["conflicts"][0]["opposite"])
        L.commit()
        self.assertEqual(L.one("SELECT count(*) n FROM v_unresolved_conflicts")["n"], 3)
        p = L.run("check", "--quiet")
        self.assertEqual(p.returncode, 1)
        self.assertIn("v_unresolved_conflicts", p.stdout)

    def test_plan_approval_refused_with_open_conflicts(self):
        L = planned(self)
        L.run_ok("conflicts", "--plan", "P-0001", "--actor", "A-0001")
        err = refused(self, L, *approve(L, expect_ok=False))
        self.assertIn("unresolved conflicts", err)
        self.assertIn("give --note", refused(self, L, "resolve-conflict", "P-0001", "1", "2", "--resolution", "order",
                                             "--actor", "A-0001"))
        out = ok(L, "resolve-conflict", "P-0001", "1", "2", "--resolution", "order", "--note",
                 "step 2 builds on step 1's check", "--actor", "A-0001")
        self.assertEqual(out, ["P-0001 steps 1 and 2: 3 conflicts resolved (order)"])
        L.commit()
        dep = L.all("""SELECT a.number a, b.number b FROM step_dependencies d JOIN steps a ON a.id = d.step_id
                       JOIN steps b ON b.id = d.depends_on_step_id""")
        self.assertEqual([(r["a"], r["b"]) for r in dep], [(2, 1)])     # order records the dependency
        ok(L, "plan-status", "P-0001", "approved", "--note", "approved", "--actor", "A-0001")
        self.assertIn("0 unresolved", L.run_ok("conflicts", "--plan", "P-0001").stdout)
        with self.subTest("owner and merge"):
            self.assertIn("not one of", refused(self, L, "resolve-conflict", "P-0001", "1", "3", "--resolution",
                                                "maybe", "--note", "x", "--actor", "A-0001"))
            self.assertIn("no conflict between steps 1 and 3",
                          refused(self, L, "resolve-conflict", "P-0001", "1", "3", "--resolution", "no-conflict",
                                  "--note", "x", "--actor", "A-0001"))

    def test_new_step_checked_against_plan(self):
        L = planned(self)
        L.run_ok("conflicts", "--plan", "P-0001", "--actor", "A-0001")
        ok(L, "resolve-conflict", "P-0001", "1", "2", "--resolution", "no-conflict", "--note",
           "different lines", "--actor", "A-0001")
        approve(L)
        out = ok(L, "report-finding", "--step", "3", "--plan", "P-0001", "--severity", "L", "--domain", "spec",
                 "--where", "src/app.py:5#handler", "--summary", "handler has no docstring.", "--before", "none",
                 "--fix", "Document it.", "--test", "test_doc: has one", "--new-issue", "--new-step", "Document handler",
                 "--actor", "A-0001")
        self.assertTrue(any("conflicts with steps 1, 2" in line for line in out), out)
        L.commit()
        self.assertEqual(L.one("SELECT count(*) n FROM v_unresolved_conflicts")["n"], 0)    # approval stands
        self.assertEqual(L.one("SELECT count(*) n FROM v_late_step_conflicts")["n"], 4)   # file and symbol, each with 1 and 2
        p = L.run("check", "--quiet")
        self.assertIn("v_late_step_conflicts", p.stdout + p.stderr)
        self.assertIn("step 4 of P-0001 conflicts with steps 1, 2", L.run_ok("gates").stdout)
        ok(L, "step-status", "1", "doing", "--note", "other steps start", "--plan", "P-0001", "--actor", "A-0001")
        err = refused(self, L, "step-status", "4", "doing", "--note", "start", "--plan", "P-0001", "--actor", "A-0001")
        self.assertIn("unresolved conflicts", err)
        ok(L, "resolve-conflict", "P-0001", "1", "4", "--resolution", "no-conflict", "--note", "docs only",
           "--actor", "A-0001")
        ok(L, "resolve-conflict", "P-0001", "2", "4", "--resolution", "merge", "--note", "done with step 2",
           "--actor", "A-0001")
        ok(L, "step-status", "4", "doing", "--note", "start", "--plan", "P-0001", "--actor", "A-0001")


if __name__ == "__main__":
    unittest.main()
