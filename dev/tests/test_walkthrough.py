"""A scripted walkthrough that follows SKILL.md literally (plan P-0001 step 104: WALK-T1).

One scratch project goes through a first review R-0001 (Steps 1-7), a fix plan P-0001 (Step 8), the fix work with a
discovery (Step 9), and a second review R-0002 (Steps 1-7 again) that checks the known issues, verifies the fixes,
finds one regressed and records a continuation. Every write is the `ledger.py` command SKILL.md gives, run the way it
says (the skill's copy with --root); reads go through `ledger.py query`. Each block is headed by the SKILL.md Step it
comes from, in SKILL.md's order, so a change to SKILL.md's commands or their order should change this test too.

Every write is a command (plan P-0001 step 131): the walkthrough runs no SQL. It records a requirement, a goal
(proposed with its source, approved by the owner), a baseline and a before-fix test run, each run's result,
confirmed and false verifications, metrics, artifacts, the verdict, the plan and its statuses, step commits and guard
tests, so the final `check` has no v_unguarded_fixes warning.

By default each command runs in this process through the built tool's main() (the same parsing and code as the CLI,
about 5 seconds in all); LEDGER_SLOW_TESTS=1 runs every one as its own `python3 ledger.py --root ...` process (about
20 seconds). LEDGER_PY_UNDER_TEST=/path/to/ledger.py runs it against another build.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from dev.tests import fixtures

LEDGER_PY = os.environ.get("LEDGER_PY_UNDER_TEST") or None
T = "\t"

APP_V1 = 'def parse(text):\n    return text.split(",")\n\n\ndef handler(request):\n    data = parse(request)\n' \
         '    return data[0]\n'
APP_V2 = 'def parse(text):\n    return text.split(",")\n\n\ndef handler(request):\n    if not request:\n' \
         '        return None\n    data = parse(request)\n    return data[0]\n'
APP_V3 = 'import csv\n\n\ndef parse(text):\n    """Split one CSV line into fields, keeping quoted commas."""\n' \
         '    return next(csv.reader([text]))\n\n\ndef handler(request):\n    if not request:\n        return None\n' \
         '    data = parse(request)\n    return data[0]\n'
TEST_V2 = 'import unittest\nfrom src.app import handler\n\n\nclass T(unittest.TestCase):\n' \
          '    def test_empty_request(self):\n        self.assertIsNone(handler(""))\n'

def point(pid, sev, kind, theme, sub, loc, summary, before, fix, test, known=""):
    """One row of a points.tsv (references/formats/index.md: 16 columns)."""
    return T.join([pid, sev, kind, theme, sub, loc, "L1", summary, before, fix, test, "", known, "", "", ""])


class Walkthrough(unittest.TestCase):
    def setUp(self):
        self.repo = fixtures.ScratchRepo()
        self.root = self.repo.path
        self.db = self.root / ".code-review" / "ledger.sqlite"
        self.ledger_py = LEDGER_PY or fixtures.built_ledger_path()
        self.env = fixtures.git_env()

    # ------------------------------------------------------------ helpers
    def run_cmd(self, *args, expect=0):
        argv = ["--root", str(self.root), *map(str, args)]
        if fixtures.SLOW:
            p = fixtures.run_ledger(*argv, ledger_py=self.ledger_py, env=self.env)
        else:
            p = self.run_in_process(argv)
        self.assertEqual(p.returncode, expect,
                         f"ledger.py {' '.join(map(str, args))} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
        self.assertNotIn("Traceback", p.stderr)
        return p.stdout

    def run_in_process(self, argv):
        mod = fixtures.load_ledger_module(self.ledger_py)
        out, err, rc = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", [str(self.ledger_py), *argv]), \
                mock.patch.dict(os.environ, self.env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                mod.main()
            except SystemExit as e:
                if isinstance(e.code, str):
                    print(e.code, file=sys.stderr)
                    rc = 1
                else:
                    rc = e.code or 0
        return subprocess.CompletedProcess(argv, rc, out.getvalue(), err.getvalue())

    def code(self, *args):
        """A command that prints a new code on its first line (A-0001, R-0001, GATE-0001)."""
        return self.run_cmd(*args).splitlines()[0].strip()

    def query(self, sql):
        return json.loads(self.run_cmd("query", "--json", sql))["rows"]

    def file(self, folder, name, lines):
        p = self.root / ".code-review" / folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def approve_domains(self, review, owner, mgr):
        """Step 2b, in SKILL.md's order: awaiting-approval, the domain-approval gate, the hub; the owner's chat
        answer, apply the gate, approve the review."""
        proposal = self.file(f"reviews/{review}", "proposal.md",
                             ["# Proposal", "", "Domains: spec, tests. Skipped: ui (no pages)."])
        self.run_cmd("review-status", review, "awaiting-approval", "--note", "domains proposed", "--actor", mgr)
        gate = self.code("open-gate", "domain-approval", "--review", review, "--title", "Approve the review domains",
                         "--question", "Run these domains?", "--context", f"@{proposal}",
                         "--option", "approve=Approve as listed", "--option", "change=Add or remove domains",
                         "--option", "goals=Edit the goals", "--actor", mgr)
        self.run_cmd("progress-page")
        self.assertIn(f"{gate}  [open] domain-approval", self.run_cmd("gates"))
        self.run_cmd("answer-gate", gate, "--value", "approve", "--text", "Go ahead", "--actor", mgr)
        self.run_cmd("apply-gate", gate, "--note", "approved as listed", "--actor", mgr)
        self.run_cmd("review-status", review, "approved", "--note", f"the owner approved the domains ({gate})",
                     "--approved-by", owner, "--actor", mgr)

    # ------------------------------------------------------------ the walkthrough
    def test_skill_walkthrough_script(self):
        started = time.monotonic()
        self.repo.write("src/app.py", APP_V1)
        self.repo.write("src/test_app.py", "import unittest\n")
        first = self.repo.commit("first")

        # ===== R-0001 =====
        # --- Step 1: init, the owner and the manager, the repository, gates, check; the ledger-location gate.
        self.assertIn("(created)", self.run_cmd("init"))
        self.assertTrue((self.root / ".code-review" / "ledger.py").exists())
        owner = self.code("add-actor", "owner", "Project Owner")
        mgr = self.code("add-actor", "manager", "review manager", "--model", "m-1", "--harness", "Claude Code",
                        "--session", "s-1")
        self.assertEqual((owner, mgr), ("A-0001", "A-0002"))
        self.run_cmd("add-repo", "demo", ".", "--remote", "https://example.invalid/demo.git", "--permalink-template",
                     "https://example.invalid/demo/blob/{sha}/{path}#L{start}-L{end}", "--actor", mgr)
        where = self.code("open-gate", "decision", "--title", "Where does the review ledger live?", "--question",
                          "Keep review data out of the repository, or commit one form of it?",
                          "--option", "outside=Outside the repository",
                          "--option", "commit-dump=Keep ledger.sql in git",
                          "--option", "commit-binary=Keep ledger.sqlite in git", "--non-blocking", "--actor", mgr)
        self.assertIn(f"{where}  [open] decision", self.run_cmd("gates"))
        self.run_cmd("check", "--quiet")
        self.query("SELECT * FROM v_open_issues")  # Step 1.2: read the history (empty on a first review)
        # --- Step 2a: start the review with its domains, included and skipped.
        r1 = self.code("new-review", "--title", "First review", "--scope", "full", "--request",
                       "Review the whole tool", "--manager", mgr, "--domain", "spec=the rules", "--domain",
                       "tests=it has tests", "--skip", "ui=no pages", "--actor", mgr)
        self.assertEqual(r1, "R-0001")
        # Step 1.3-1.4 (recorded once the review exists): the baseline test run and a requirement.
        self.assertIn("baseline", self.run_cmd("record-test-run", "--purpose", "baseline", "--command",
                                              "python3 -m unittest", "--review", r1, "--passed", "0", "--failed",
                                              "0", "--exit-code", "5", "--note", "no tests yet", "--actor", mgr))
        self.assertEqual(self.code("add-requirement", "An empty request is refused, never a crash.", "--source",
                                   "owner, chat 2026-11-11", "--review", r1, "--actor", mgr), "REQ-0001")
        # --- Step 2b: the owner approves the domains (and answers where the ledger lives).
        self.approve_domains(r1, owner, mgr)
        self.run_cmd("answer-gate", where, "--value", "outside", "--text", "Keep it out of the repo", "--actor", mgr)
        self.run_cmd("apply-gate", where, "--note", ".code-review/ stays excluded; nothing is committed",
                     "--actor", mgr)
        self.assertEqual(self.query("SELECT code FROM v_standing_decisions"), [["D-0001"]])
        # --- Step 2c: a goal the project states, proposed and approved by the owner; 2d: the brief, an artifact.
        self.assertEqual(self.code("propose-goal", "--source", "owner, chat 2026-11-11", "--review", r1,
                                   "One validator per input.", "--actor", mgr), "G-01")
        self.run_cmd("approve-goals", "G-01", "--actor", owner)
        # Step 2c (step 129): the owner interview, a goal proposed from it, the owner's approval through its gate,
        # and a reviewer of the domain looking the approved goals up.
        iq = self.code("interview-ask", "--review", r1, "--domain", "spec", "--why", "handler() takes any request",
                       "What must handler() do with an empty request?", "--actor", mgr)
        self.assertEqual(iq, "IQ-0001")
        self.run_cmd("interview-answer", iq, "Refuse it, never crash.", "--via", "chat", "--actor", mgr)
        self.assertEqual(self.code("propose-goal", "--domain", "spec", "--from", iq, "An empty request is refused.",
                                   "--gate", "--actor", mgr), "G-02")
        self.assertNotIn("G-02", self.run_cmd("goals", "--domain", "spec"))  # not served until approved
        goal_gate = self.query("SELECT code FROM gates WHERE status = 'open' AND kind = 'confirm'")[0][0]
        self.run_cmd("answer-gate", goal_gate, "--value", "approve", "--text", "Yes, as written", "--actor", mgr)
        self.run_cmd("apply-gate", goal_gate, "--note", "goals approved; the brief gives them", "--actor", mgr)
        self.assertIn("Refuse it, never crash.", self.run_cmd("interview", "--review", r1))
        self.assertIn("G-02  An empty request is refused.", self.run_cmd("goals", "--domain", "spec"))
        brief = self.file(f"reviews/{r1}", "BRIEF.md", ["# Brief", "", "Goals: G-01, G-02 (ledger.py goals)."])
        self.run_cmd("add-artifact", "brief", "--path", brief, "--review", r1, "--actor", mgr)
        # --- Step 3: snapshot, one run per reviewer (add-run records its actor), running.
        self.assertIn("clean", self.run_cmd("snapshot", r1, "--actor", mgr))
        prompt = self.file(f"reviews/{r1}", "prompt.txt", ["First read BRIEF.md and follow it exactly."])
        self.run_cmd("add-run", r1, "spec", "spec-A", "--scope", "everything", "--model", "m-2", "--prompt",
                     f"@{prompt}", "--status", "running", "--actor", mgr)
        self.run_cmd("add-run", r1, "tests", "tests-A", "--scope", "everything", "--model", "m-2", "--prompt",
                     f"@{prompt}", "--status", "running", "--actor", mgr)
        self.run_cmd("review-status", r1, "running", "--note", "reviewers started", "--actor", mgr)
        # --- Step 4: verifying; each run finished with its report; every other agent is an actor with its role.
        self.run_cmd("review-status", r1, "verifying", "--note", "reports arrived", "--actor", mgr)
        for label, name, high in (("spec-A", "spec.md", "1"), ("tests-A", "tests.md", "0")):
            report = self.file(f"reviews/{r1}", name, [f"# {label}", "", "Points in the index."])
            self.assertIn("-> finished", self.run_cmd("finish-run", r1, label, "--report", report, "--items", "1",
                                                      "--high", high, "--actor", mgr))
        self.code("add-actor", "verifier", "verifier 1", "--model", "m-2", "--parent", mgr, "--review", r1,
                  "--role", "verifier for spec.md", "--actor", mgr)
        # --- Step 5: indexing; 5a indexers; 5c load-index per report, tag-review, fill-refs; 5d match; check.
        self.run_cmd("review-status", r1, "indexing", "--note", "indexing the reports", "--actor", mgr)
        self.code("add-actor", "indexer", "indexer 1", "--parent", mgr, "--review", r1, "--role",
                  "indexer for spec.md", "--actor", mgr)
        spec_tsv = self.file(f"reviews/{r1}/index", "spec-points.tsv", [
            point("SPEC-H1", "H", "defect", "validation", "empty-request", "demo:src/app.py:5-7#handler",
                  "handler crashes on an empty request.", "It reads data[0] unchecked.", "Refuse empty input.",
                  "test_empty_request: returns None"),
            point("SPEC-M1", "M", "defect", "validation", "parse-quotes", "demo:src/app.py:1-2#parse",
                  "parse splits quoted commas.", "It splits on every comma.", "Use csv.",
                  "test_parse_quoted: keeps quoted commas")])
        tests_tsv = self.file(f"reviews/{r1}/index", "tests-points.tsv", [
            point("TEST-T1", "T", "test", "validation", "handler-untested", "demo:src/test_app.py",
                  "No test covers handler.", "The test file is empty.", "Add tests.",
                  "test_handler: asserts the first field")])
        self.assertIn("loaded 2 points", self.run_cmd("load-index", r1, "spec-A", spec_tsv, "--actor", mgr))
        self.assertIn("loaded 1 points", self.run_cmd("load-index", r1, "tests-A", tests_tsv, "--actor", mgr))
        self.assertIn("review/R-0001", self.run_cmd("tag-review", r1))
        self.assertIn("filled 3 references", self.run_cmd("fill-refs", r1))
        # Step 4, after 5c: the verdicts the manager checked; 5c: a metric the report measured.
        self.run_cmd("verify", f"{r1}:SPEC-H1", "confirmed", "--note", "read src/app.py:5-7 at the reviewed commit",
                     "--actor", mgr)
        self.run_cmd("verify", "SPEC-M1", "partly", "--review", r1, "--note", "only quoted commas", "--actor", mgr)
        self.run_cmd("add-metric", r1, "test_cases_asserted", "0", "--method", "counted", "--domain", "tests",
                     "--scope", "src/test_app.py", "--unit", "cases", "--actor", mgr)
        self.assertIn("v_unmatched_findings", self.run_cmd("check", "--quiet", expect=1))
        self.code("add-actor", "matcher", "matcher 1", "--parent", mgr, "--review", r1, "--role",
                  "matcher for spec", "--actor", mgr)
        matches = self.file(f"reviews/{r1}/index", "matches.tsv", [
            T.join(["SPEC-H1", "NEW", "new", "certain", "first review", ""]),
            T.join(["SPEC-M1", "NEW", "new", "likely", "first review", "related-to:ISS-0001"])])
        self.assertIn("matched 2 points", self.run_cmd("match", r1, "--file", matches, "--actor", mgr))
        self.assertIn("TEST-T1 -> ISS-0003 new", self.run_cmd(
            "match", r1, "TEST-T1", "new", "--type", "new", "--rationale", "first review",
            "--relations", "caused-by:ISS-0001", "--actor", mgr))
        self.run_cmd("check", "--quiet")
        # --- Step 6: consolidating; store the review report; publish the hub.
        self.run_cmd("review-status", r1, "consolidating", "--note", "writing the review", "--actor", mgr)
        self.run_cmd("set-report", r1, self.file(f"reviews/{r1}", "review.md", ["# R-0001", "", "Three points."]))
        self.run_cmd("set-verdict", r1, "--verdict", "Usable once empty requests are refused.", "--summary",
                     "Two domains, three points.", "--actor", mgr)
        self.run_cmd("progress-page")
        # --- Step 7: check, dump, handed-off, the hub.
        self.run_cmd("check", "--quiet")
        self.run_cmd("dump")
        self.run_cmd("review-status", r1, "handed-off", "--note", "handed off to the owner", "--actor", mgr)
        self.run_cmd("progress-page")
        self.assertEqual(self.query("SELECT code, status FROM issues ORDER BY code"),
                         [["ISS-0001", "open"], ["ISS-0002", "open"], ["ISS-0003", "open"]])

        # ===== P-0001 =====
        # --- Step 8a: create the plan over R-0001 and store its narrative.
        plan = self.code("new-plan", "--title", f"Fix plan for {r1}", "--review", r1, "--actor", mgr)
        self.assertEqual(plan, "P-0001")
        self.run_cmd("set-narrative", plan, self.file("plans", "P-0001.md", ["# Fix plan", "", "Crash first."]))
        # --- Step 8b: a drafter's steps.jsonl, loaded with import-steps.
        self.code("add-actor", "drafter", "drafter 1", "--parent", mgr, "--review", r1, "--role",
                  "drafter for validation", "--actor", mgr)
        steps = self.file("plans", "steps.jsonl", [json.dumps(o) for o in (
            {"kind": "phase", "phase": 1, "title": "Fix first", "goal": "Stop the crash.", "why": "It loses input."},
            {"key": "A-1", "phase": 1, "title": "Refuse empty requests", "change": "Check the input.",
             "proof": "test_empty_request: returns None", "closes": ["SPEC-H1", "TEST-T1"]},
            {"key": "A-2", "phase": 1, "title": "Parse quoted commas", "change": "Use csv.",
             "proof": "test_parse_quoted: keeps quoted commas", "closes": ["SPEC-M1"], "depends": ["A-1"]})])
        out = self.run_cmd("import-steps", plan, steps, "--actor", mgr)
        self.assertIn("0 points of the plan's reviews closed by no step, 0 coverage problems", out)
        # --- Step 8c: check (v_uncovered_findings and v_coverage_problems empty).
        self.run_cmd("check", "--quiet")
        # --- Step 8d: the plan-approval gate, awaiting-approval, the hub; the owner approves; apply, approved, and
        #     the plan's issues to planned.
        gate = self.code("open-gate", "plan-approval", "--plan", plan, "--title", "Approve the fix plan",
                         "--question", "Start work in this order?", "--option", "approve=Approve the plan",
                         "--option", "change=Change it first", "--actor", mgr)
        self.run_cmd("plan-status", plan, "awaiting-approval", "--note", "the steps are ready for the owner",
                     "--actor", mgr)
        self.run_cmd("progress-page", plan)
        self.run_cmd("answer-gate", gate, "--value", "approve", "--text", "Go ahead.", "--actor", mgr)
        self.run_cmd("apply-gate", gate, "--note", "plan approved; work starts with phase 1", "--actor", mgr)
        self.run_cmd("plan-status", plan, "approved", "--note", f"the owner approved the plan ({gate})",
                     "--actor", mgr)
        for iss in ("ISS-0001", "ISS-0002", "ISS-0003"):
            self.run_cmd("issue-status", iss, "planned", "--note", "in P-0001", "--actor", mgr)
        self.assertEqual(self.run_cmd("gates").strip(), "no open gates")

        # --- Step 9, step 1: the implementer; the plan in-progress; start (step doing, its issues in-progress);
        #     a discovery reported with report-finding; commit; done (progress note, each issue fixed).
        fixer = self.code("add-actor", "fixer", "implementer 1", "--parent", mgr, "--review", r1, "--role",
                          "implementer of steps 1-2", "--actor", mgr)
        self.run_cmd("plan-status", plan, "in-progress", "--note", "work started", "--actor", fixer)
        self.run_cmd("step-status", "1", "doing", "--note", "refusing empty requests", "--plan", plan,
                     "--actor", fixer)
        step1 = [r[0] for r in self.query("SELECT issue FROM v_step_issues WHERE step = 1 ORDER BY issue")]
        self.assertEqual(step1, ["ISS-0001", "ISS-0003"])
        for iss in step1:
            self.run_cmd("issue-status", iss, "in-progress", "--note", "step 1 started", "--step", "1", "--plan",
                         plan, "--actor", fixer)
        self.repo.write("src/test_app.py", TEST_V2)  # proof first: the test fails before the fix
        self.run_cmd("record-test-run", "--purpose", "before-fix", "--command", "python3 -m unittest", "--step", "1",
                     "--plan", plan, "--failed", "1", "--exit-code", "1", "--actor", fixer)
        self.repo.write("src/app.py", APP_V2)
        out = self.run_cmd("report-finding", "--step", "1", "--plan", plan, "--severity", "L", "--domain", "tests",
                           "--where", "src/app.py:1-2#parse", "--summary",
                           "parse has no docstring saying it splits on commas.", "--before", "parse is undocumented.",
                           "--fix", "Document parse.", "--test", "test_doc: parse has a docstring", "--new-issue",
                           "--into-step", "2", "--actor", fixer)
        self.assertIn("FIX-1 -> ISS-0004 new", out)
        self.assertIn("planned in step 2", out)
        self.assertEqual(self.query("SELECT count(*) FROM v_unplanned_discoveries"), [[0]])
        fix1 = self.repo.commit("Refuse empty requests")
        # Commit, then done: the commit and the step's guard test are recorded with it.
        self.assertIn(f"commit {fix1[:10]} on main", self.run_cmd(
            "step-status", "1", "done", "--note", "done", "--progress-note",
            "An empty request now returns nothing instead of crashing.", "--commit", fix1, "--guard",
            "src/test_app.py::test_empty_request", "--failed-before", "--plan", plan, "--actor", fixer))
        for iss, after in (("ISS-0001", "handler returns None for an empty request"),
                           ("ISS-0003", "test_empty_request covers handler")):
            self.run_cmd("issue-status", iss, "fixed", "--note", "step 1 done", "--after", after, "--step", "1",
                         "--plan", plan, "--commit", fix1, "--actor", fixer)
        # --- Step 9, after each step: check, dump, progress-md, the hub, gates.
        self.run_cmd("check", "--quiet")
        self.run_cmd("dump")
        self.assertIn("1 of 2 steps done", self.run_cmd("progress-md", plan))
        self.run_cmd("progress-page", plan)
        self.assertEqual(self.run_cmd("gates").strip(), "no open gates")

        # --- Step 9, step 2 (it also closes the discovery FIX-1); then the plan is complete.
        self.run_cmd("step-status", "2", "doing", "--note", "parsing with csv", "--plan", plan, "--actor", fixer)
        step2 = [r[0] for r in self.query("SELECT issue FROM v_step_issues WHERE step = 2 ORDER BY issue")]
        self.assertEqual(step2, ["ISS-0002", "ISS-0004"])
        for iss in step2:
            self.run_cmd("issue-status", iss, "in-progress", "--note", "step 2 started", "--step", "2", "--plan",
                         plan, "--actor", fixer)
        self.repo.write("src/app.py", APP_V3)
        fix2 = self.repo.commit("Parse with csv")
        self.run_cmd("add-commit", "2", fix2, "--plan", plan, "--pr", "https://example.invalid/demo/pull/2",
                     "--actor", fixer)
        self.run_cmd("step-status", "2", "done", "--note", "done", "--progress-note", "Quoted commas are kept.",
                     "--commit", fix2, "--guard", "ISS-0002=src/test_app.py::test_parse_quoted", "--plan", plan,
                     "--actor", fixer)
        self.run_cmd("add-guard", "ISS-0004", "src/test_app.py", "test_doc", "--step", "2", "--plan", plan,
                     "--commit", fix2, "--actor", fixer)
        for iss in step2:
            self.run_cmd("issue-status", iss, "fixed", "--note", "step 2 done", "--after",
                         "parse uses csv.reader and has a docstring", "--step", "2", "--plan", plan,
                         "--commit", fix2, "--actor", fixer)
        self.run_cmd("plan-status", plan, "complete", "--note", "every step is done", "--actor", mgr)
        self.run_cmd("check", "--quiet")
        self.run_cmd("dump")
        md = self.run_cmd("progress-md", plan)
        self.assertIn("Plan status: complete. 2 of 2 steps done", md)

        # ===== R-0002: the second review checks the known issues =====
        # --- Step 1: init again (same copy: nothing to refresh), gates, check; read the history.
        self.assertIn("(attached to existing)", self.run_cmd("init"))
        self.assertEqual(self.run_cmd("gates").strip(), "no open gates")
        self.run_cmd("check", "--quiet")
        self.assertEqual(self.query("SELECT review, commit_sha FROM v_last_reviewed_commit"), [["R-0001", first]])
        self.assertEqual(self.query("SELECT code, status FROM v_known_issues_for_brief ORDER BY code"),
                         [[c, "fixed"] for c in ("ISS-0001", "ISS-0002", "ISS-0003", "ISS-0004")])
        # --- Step 2a: the review of the fixed commit, the same domains.
        r2 = self.code("new-review", "--title", "Second review", "--scope", "second", "--request",
                       "Verify the fix plan", "--manager", mgr, "--domain", "spec=the rules", "--domain",
                       "tests=it has tests", "--skip", "ui=no pages", "--previous", r1, "--actor", mgr)
        self.assertEqual(r2, "R-0002")
        # --- Step 2b.
        self.approve_domains(r2, owner, mgr)
        # --- Step 3.
        self.run_cmd("snapshot", r2, "--actor", mgr)
        self.run_cmd("add-run", r2, "spec", "spec-B", "--scope", "everything", "--model", "m-2", "--status",
                     "running", "--actor", mgr)
        self.run_cmd("add-run", r2, "tests", "tests-B", "--scope", "everything", "--model", "m-2", "--status",
                     "running", "--actor", mgr)
        self.run_cmd("review-status", r2, "running", "--note", "reviewers started", "--actor", mgr)
        # --- Step 4: the reports arrive.
        self.run_cmd("review-status", r2, "verifying", "--note", "reports arrived", "--actor", mgr)
        for label, name in (("spec-B", "spec.md"), ("tests-B", "tests.md")):
            report = self.file(f"reviews/{r2}", name, [f"# {label}", "", "One point."])
            self.run_cmd("finish-run", r2, label, "--report", report, "--items", "1", "--actor", mgr)
        # --- Step 5c: each report's points and its known-issues table, in one load-index; tag-review, fill-refs.
        self.run_cmd("review-status", r2, "indexing", "--note", "indexing the reports", "--actor", mgr)
        idx = f"reviews/{r2}/index"
        spec2 = self.file(idx, "spec-points.tsv", [
            point("SPEC2-M1", "M", "defect", "validation", "parse-multiline", "demo:src/app.py:4-6#parse",
                  "parse drops every line after the first.", "next(csv.reader(...)) reads only the first row.",
                  "Refuse or join multi-line input.", "test_parse_multiline: refuses two lines", known="ISS-0002")])
        known_spec = self.file(idx, "spec-known.tsv", [
            T.join(["ISS-0001", "fixed", "handler returns None for an empty request (src/app.py:10-11)", ""]),
            T.join(["ISS-0002", "fixed", "parse uses csv.reader (src/app.py:4-6)", ""])])
        tests2 = self.file(idx, "tests-points.tsv", [
            point("TEST2-L1", "L", "test", "validation", "no-csv-test", "demo:src/test_app.py",
                  "Nothing tests parse.", "The tests cover handler only.", "Test parse.",
                  "test_parse: splits fields")])
        known_tests = self.file(idx, "tests-known.tsv", [
            T.join(["ISS-0003", "regressed", "src/test_app.py imports src.app, which is not a package: the test "
                                             "never runs", ""]),
            T.join(["ISS-0004", "fixed", "parse has a docstring", ""])])
        self.assertIn("recorded 2 checks of known issues",
                      self.run_cmd("load-index", r2, "spec-B", spec2, known_spec, "--actor", mgr))
        self.assertIn("recorded 2 checks of known issues",
                      self.run_cmd("load-index", r2, "tests-B", tests2, known_tests, "--actor", mgr))
        self.run_cmd("tag-review", r2)
        self.run_cmd("fill-refs", r2)
        # Step 4, after 5c: SPEC2-M1 confirmed; TEST2-L1 is false (test_parse_quoted tests parse).
        self.run_cmd("verify", f"{r2}:SPEC2-M1", "confirmed", "--note", "two lines give one row", "--actor", mgr)
        self.assertIn("unverified -> false", self.run_cmd(
            "verify", f"{r2}:TEST2-L1", "false", "--note", "test_parse_quoted calls parse", "--actor", mgr))
        # --- Step 5d: the new point grew out of the csv fix: a continuation of ISS-0002.
        self.assertIn("ISS-0005 continuation", self.run_cmd(
            "match", r2, "SPEC2-M1", "new", "--type", "continuation", "--rationale", "grew out of the csv fix",
            "--relations", "continuation-of:ISS-0002", "--actor", mgr))
        self.assertIn("closed as obsolete", self.run_cmd(
            "match", r2, "TEST2-L1", "new", "--type", "new", "--rationale", "test_parse_quoted calls parse",
            "--actor", mgr))
        self.run_cmd("check", "--quiet")
        # --- Step 6 and 7.
        self.run_cmd("review-status", r2, "consolidating", "--note", "writing the review", "--actor", mgr)
        self.run_cmd("set-report", r2, self.file(f"reviews/{r2}", "review.md", ["# R-0002", "", "Three verified."]))
        self.run_cmd("check", "--quiet")
        self.run_cmd("dump")
        self.run_cmd("review-status", r2, "handed-off", "--note", "handed off to the owner", "--actor", mgr)

        # ===== The end state =====
        self.assertEqual(self.query("SELECT code, status FROM issues ORDER BY code"), [
            ["ISS-0001", "verified"], ["ISS-0002", "verified"], ["ISS-0003", "regressed"],
            ["ISS-0004", "verified"], ["ISS-0005", "open"], ["ISS-0006", "obsolete"]])
        self.assertIn(["ISS-0005", "continuation-of", "ISS-0002"],
                      self.query("SELECT from_issue, relation, to_issue FROM v_issue_graph"))
        self.assertEqual(self.query("SELECT f.point_id, s.number FROM findings f JOIN steps s "
                                    "ON s.id = f.discovered_in_step_id"), [["FIX-1", 1]])
        self.assertGreaterEqual(self.query("SELECT count(*) FROM v_review_agents WHERE review = 'R-0001'")[0][0], 8)
        self.assertNotIn("v_unguarded_fixes", self.run_cmd("check"))
        self.assertEqual(self.query("SELECT count(*) FROM v_unguarded_fixes"), [[0]])
        self.assertEqual(self.query("SELECT s.number, count(*) FROM step_commits c JOIN steps s ON s.id = c.step_id "
                                    "GROUP BY s.number"), [[1, 1], [2, 1]])
        self.assertEqual(self.query("SELECT purpose FROM test_runs ORDER BY id"), [["baseline"], ["before-fix"]])
        self.assertEqual(self.query("SELECT count(*) FROM reviewer_runs WHERE status <> 'finished'"), [[0]])
        out = self.run_cmd("progress-page", plan)
        self.assertIn("no outside requests", out)
        self.assertGreater((self.root / ".code-review" / "hub.html").stat().st_size, 10_000)
        # Everything the walkthrough wrote is inside the excluded .code-review/: the repository is untouched.
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertLess(time.monotonic() - started, 60, "the walkthrough got slow; see the module docstring")

    def test_plan_commands_listed(self):
        # was test_current_no_plan_command (step 104): plans are created and moved by commands now (step 131)
        commands = fixtures.run_ledger("--help", ledger_py=self.ledger_py).stdout
        listed = commands.split("{", 1)[1].split("}", 1)[0].split(",")
        self.assertNotIn("add-goal", listed)  # D-0013: goals are only proposed and approved by the owner
        for c in ("new-plan", "plan-status", "add-requirement", "add-domain", "review-domain",
                  "finish-run", "verify", "record-test-run", "add-commit", "add-guard", "confirm-guard",
                  "set-verdict", "add-metric", "add-artifact", "redact"):
            self.assertIn(c, listed)

if __name__ == "__main__":
    unittest.main()
