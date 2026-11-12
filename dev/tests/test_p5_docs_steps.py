"""SKILL.md Steps 1-3 run as written on a fresh project (plan P-0001 step 109), and drafters' closes are
review-qualified (step 111).

test_skill_step1_commands_run   every ledger.py line of the fenced blocks of Steps 1, 2a, 2b, 2c (once the interview
                                commands of step 129 are built) and 3, with the recipes' commands for what Steps 1
                                and 2a name (add-requirement, add-domain), runs with
                                exit 0 on a new git project; the review ends running with its domains, the goal, the
                                requirement and the custom domain recorded, and check passes
test_drafter_closes_are_review_qualified
                                a plan over two reviews that share a point id loads closes written R-0001:SPEC-M1 and
                                R-0002:SPEC-M1 to one finding each, and refuses a bare SPEC-M1
"""
import json
import re
import shlex
import sqlite3
import unittest  # sqlite3: read-only checks of the result

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ScratchRepo, finish_demo, seed_demo
from dev.tests.test_p5_docs_recipes import blocks as fenced_blocks
from dev.tests.test_skill_contract import section

SKILL_MD = fixtures.REPO_ROOT / "SKILL.md"
RECIPES = fixtures.REPO_ROOT / "references" / "recipes.md"

PLACEHOLDERS = {
    "<owner name>": "Pat Owner",
    "<model id>": "model-x",
    "<Claude Code or OpenCode>": "Claude Code",
    "<ref>": "session-1",
    "<name>": "app",
    "<path>": ".",
    "<url>": "https://example.com/o/app",
    "https://<host>/<owner>/<repo>/blob/": "https://example.com/o/app/blob/",
}


def skill_lines(text, *titles):
    """The ledger.py lines of the unlabelled fenced blocks in the named sections, in order."""
    out = []
    for t in titles:
        sec = section(text, t)
        for m in re.finditer(r"^```\n(.*?)^```", sec, re.M | re.S):
            out += [l for l in m.group(1).splitlines() if l.startswith("ledger.py ")]
    return out


def recipe(text, title):
    """The ledger.py lines of a recipe section's sh blocks."""
    return [l for lang, b in fenced_blocks(section(text, title)) if lang == "sh"
            for l in b.splitlines() if l.startswith("ledger.py ")]


class SkillSteps(unittest.TestCase):

    def test_skill_step1_commands_run(self):
        skill = SKILL_MD.read_text(encoding="utf-8")
        recipes = RECIPES.read_text(encoding="utf-8")
        repo = ScratchRepo()
        repo.write("src/app.py", "def handler(request):\n    return request\n")
        repo.commit("first")
        L = Ledger(repo, real_init=True)
        self.addCleanup(L.close)
        L.close()  # this test writes only through the CLI
        (repo.path / "prompt.txt").write_text("First read BRIEF.md and follow it exactly.\n", encoding="utf-8")

        def run_line(line):
            for k, v in PLACEHOLDERS.items():
                line = line.replace(k, v)
            line = re.sub(r"<[^<>]+>", "text", line)
            args = shlex.split(line)[1:]
            p = L.run(*args, cwd=repo.path)
            self.assertEqual(p.returncode, 0, f"SKILL.md: {line}\n{p.stdout}\n{p.stderr}")
            return p

        def run_recipe(lines, command):
            self.assertEqual([shlex.split(l)[1] for l in lines], [command])
            self.assertIn(f"`ledger.py {command}", skill, f"SKILL.md no longer names {command}")
            run_line(lines[0].replace("R-0002", "R-0001"))

        step1 = skill_lines(skill, "Step 1:")
        self.assertEqual([shlex.split(l)[1] for l in step1],
                         ["env", "init", "add-actor", "add-actor", "add-repo", "open-gate", "gates", "check"])
        for line in step1:
            run_line(line)
        run_recipe(recipe(recipes, "Add a custom domain"), "add-domain")
        step2a = skill_lines(skill, "2a. Draft the selection")
        self.assertEqual([shlex.split(l)[1] for l in step2a], ["new-review", "env", "scan"])
        for line in step2a:
            run_line(line)
        run_recipe(recipe(recipes, "Add a requirement"), "add-requirement")
        (repo.path / ".code-review" / "reviews" / "R-0001").mkdir(parents=True)
        (repo.path / ".code-review" / "reviews" / "R-0001" / "proposal.md").write_text("Domains: security, tests.\n")
        step2b = skill_lines(skill, "2b. Ask the owner to approve")
        self.assertEqual([shlex.split(l)[1] for l in step2b],
                         ["review-status", "open-gate", "progress-page", "answer-gate", "apply-gate",
                          "review-status"])
        for line in step2b:
            run_line(line)
        step2c = skill_lines(skill, "2c. Interview the owner")
        self.assertEqual([shlex.split(l)[1] for l in step2c],
                         ["interview-ask", "interview-answer", "propose-goal", "approve-goals", "goals"])
        have = all(L.run(shlex.split(l)[1], "--help").returncode == 0 for l in step2c)
        if have:  # the interview commands (step 129) are in this build
            for line in step2c:
                run_line(line)
        for line in skill_lines(skill, "Step 3:"):
            run_line(line)
        con = sqlite3.connect(str(L.db))
        self.addCleanup(con.close)
        q = lambda sql: con.execute(sql).fetchall()
        self.assertEqual(q("SELECT code, status, approved_by_actor_id FROM reviews"), [("R-0001", "running", 1)])
        self.assertEqual(q("SELECT domain_code, decision, proposed_by_rule FROM review_domains WHERE domain_code "
                           "IN ('security', 'tests', 'ui') ORDER BY domain_code"),
                         [("security", "included", "code_files >= 1"), ("tests", "included", "code_files >= 1"),
                          ("ui", "skipped", None)])   # scan proposed them by rule (step 138)
        if have:
            self.assertEqual(q("SELECT code FROM design_goals"), [("G-01",)])
        self.assertEqual(q("SELECT code, source FROM requirements"), [("REQ-0001", "docs/spec.md")])
        self.assertEqual(q("SELECT category FROM domains WHERE code = 'payments'"), [("custom",)])
        self.assertEqual(q("SELECT kind, status FROM gates ORDER BY id"),
                         [("decision", "open"), ("domain-approval", "applied")])
        self.assertEqual(q("SELECT kind, name FROM actors ORDER BY id")[:2],
                         [("owner", "Pat Owner"), ("manager", "review manager")])
        self.assertEqual(q("SELECT r.label, a.kind, a.parent_actor_id FROM reviewer_runs r JOIN actors a ON "
                           "a.id = r.actor_id"), [("security-1", "reviewer", 2)])
        self.assertTrue((repo.path / ".code-review" / "hub.html").exists())
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class DrafterCloses(unittest.TestCase):

    def test_drafter_closes_are_review_qualified(self):
        with Ledger() as L:
            seed_demo(L)
            finish_demo(L)
            L.x("""INSERT INTO plans(code, title) VALUES ('P-0002', 'Both reviews')""")
            for r in ("R-0001", "R-0002"):
                L.x("INSERT INTO plan_reviews(plan_id, review_id) VALUES ((SELECT id FROM plans WHERE code='P-0002'),"
                    " (SELECT id FROM reviews WHERE code=?))", (r,))
            L.commit()
            self.assertEqual(L.scalar("SELECT count(DISTINCT review_id) FROM findings WHERE point_id='SPEC-M1'"), 2)
            phase = {"kind": "phase", "phase": 1, "title": "Fix first", "goal": "The defects."}

            def step(key, closes):
                return {"key": key, "phase": 1, "title": f"Step {key}", "change": "c", "proof": "p", "closes": closes}
            bad = L.root / "bad.jsonl"
            bad.write_text("\n".join(json.dumps(o) for o in (phase, step("A", ["SPEC-M1"]))) + "\n")
            p = L.run("import-steps", "P-0002", str(bad))
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("R-0001:SPEC-M1", p.stderr)
            good = L.root / "good.jsonl"
            good.write_text("\n".join(json.dumps(o) for o in (
                phase, step("A", ["R-0001:SPEC-M1"]), step("B", ["R-0002:SPEC-M1"]))) + "\n")
            L.run_ok("import-steps", "P-0002", str(good))
            rows = L.all("""SELECT s.drafter_key, r.code FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                            JOIN findings f ON f.id = sf.finding_id JOIN reviews r ON r.id = f.review_id
                            WHERE s.plan_id = (SELECT id FROM plans WHERE code='P-0002') ORDER BY s.drafter_key""")
        self.assertEqual([(r["drafter_key"], r["code"]) for r in rows], [("A", "R-0001"), ("B", "R-0002")])


if __name__ == "__main__":
    unittest.main()
