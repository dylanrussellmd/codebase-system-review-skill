"""brief, prompt and lint-report (plan P-0001 step 139; OWN-9, G-09): the brief and every reviewer prompt are
rendered from fixed templates, the scan's inventory, the approved goals, the known issues and the domain's checklist
(domain_checks, seeded from references/domains.md), byte-identical for identical inputs; lint-report checks a report
against the format and the checklist."""
import re
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, project, refused

REFS = fixtures.REPO_ROOT / "references"


def briefed(test):
    """project() (R-0001 running, domains spec and tests, a spec run) with the owner, approved goals, a requirement
    and one known issue per domain."""
    repo, L, sha = project(test)
    ok(L, "add-actor", "owner", "the owner")
    ok(L, "propose-goal", "--source", "owner, chat", "--review", "R-0001", "One validator per input.", "--actor", "A-0001")
    ok(L, "propose-goal", "--domain", "spec", "--source", "owner, chat", "--review", "R-0001",
       "An empty request is refused.", "--actor", "A-0001")
    ok(L, "propose-goal", "--domain", "tests", "--source", "owner, chat", "--review", "R-0001",
       "Every refusal has a test.", "--actor", "A-0001")
    ok(L, "propose-goal", "--domain", "spec", "--source", "owner, chat", "--review", "R-0001",
       "Not approved yet.", "--actor", "A-0001")
    ok(L, "approve-goals", "G-01", "G-02", "G-03", "--actor", "A-0002")
    ok(L, "add-requirement", "An empty request is refused, never a crash.", "--source", "README.md", "--actor", "A-0001")
    rid = L.one("SELECT id FROM reviews WHERE code='R-0001'")["id"]
    L.issue(first_seen_review_id=rid, domain_code="spec", title="handler crashes on an empty request")
    L.issue(first_seen_review_id=rid, domain_code="tests", title="No test covers handler")
    L.commit()
    return repo, L, sha


def checklist(L, domain):
    L.commit()
    return L.all("SELECT * FROM domain_checks WHERE domain_code=? ORDER BY position", (domain,))


def report(L, domain="spec", *, drop_check=None, findings=None, sections=None, known=None, answers=None):
    """A report in the format `brief` prints, as text."""
    rows = []
    for c in checklist(L, domain):
        if c["code"] == drop_check:
            continue
        answer, evidence = (answers or {}).get(c["code"], ("yes", "src/app.py:5 handles it"))
        rows.append(f"| {c['code']} | {answer} | {evidence} |")
    if known is None:
        known = [f"| {r['code']} | still-present | src/app.py:5 still reads it unchecked | |"
                 for r in L.all("SELECT code FROM v_known_issues_for_brief WHERE domain_code=? ORDER BY code", (domain,))]
    if findings is None:
        findings = ["### SPEC-H1 [H] handler crashes on an empty request.", "",
                    "- Where: `demo:src/app.py:5-6#handler`", "- Before: It reads the request unchecked.",
                    "- Fix: Refuse an empty request.", "- Test: test_empty_request: returns None", "- Check: spec.2"]
    parts = {"Scope and method": ["Everything, read at the reviewed commit."],
             "Checklist": ["| check | answer | evidence |", "|---|---|---|"] + rows,
             "Known issues": ["| issue | outcome | evidence | point |", "|---|---|---|---|"] + known,
             "Findings": findings,
             "Totals": ["1 item reviewed."]}
    out = [f"# {domain} report", ""]
    for title in sections or list(parts):
        out += [f"## {title}", ""] + parts[title] + [""]
    return "\n".join(out)


def lint(L, text, *args, name="spec.md"):
    f = L.dir / "reviews" / "R-0001" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return L.run("lint-report", f, "--domain", "spec", *args)


class Generated(unittest.TestCase):
    def test_brief_is_generated(self):
        repo, L, sha = briefed(self)
        first = L.run_ok("brief", "--review", "R-0001").stdout
        self.assertEqual(L.run_ok("brief", "--review", "R-0001").stdout, first)
        self.assertNotRegex(first, r"20\d\d-\d\d-\d\dT")
        self.assertNotIn(str(L.root), first)
        for need in ("# Review brief: R-0001 First review", f"| demo | {sha} | main | clean |",
                     "## Design goals the owner approved", "- G-01 One validator per input.",
                     "- G-02 An empty request is refused.", "ledger.py goals --domain <domain>",
                     "REQ-0001 An empty request is refused, never a crash. (README.md)",
                     "ISS-0001 [open, M] handler crashes on an empty request",
                     "| spec.1 | Is every stated requirement listed, with its source quoted? |",
                     "| tests.1 |", "## Rules for reviewers", "Read-only.", "## Report format",
                     "## Checklist", "### SPEC-H1 [H]"):
            self.assertIn(need, first)
        self.assertNotIn("Not approved yet", first)      # a proposed goal is not served
        self.assertNotIn("| ui.1 |", first)              # only the review's domains
        inventory = first[first.index("## The system"):first.index("## Design goals")]
        self.assertIn("python 1", inventory)
        with self.subTest("written to a file inside the ledger's folder only"):
            out = L.dir / "reviews" / "R-0001" / "BRIEF.md"
            self.assertIn("wrote", L.run_ok("brief", "--review", "R-0001", "--out", out).stdout)
            self.assertEqual(out.read_text(encoding="utf-8"), first)
            self.assertIn("not inside the ledger's folder",
                          refused(self, L, "brief", "--review", "R-0001", "--out", L.root / "BRIEF.md"))
        with self.subTest("a new approved goal changes the brief"):
            ok(L, "approve-goals", "G-04", "--actor", "A-0002")
            self.assertIn("Not approved yet", L.run_ok("brief", "--review", "R-0001").stdout)

    def test_prompt_is_generated(self):
        repo, L, sha = briefed(self)
        args = ("prompt", "--review", "R-0001", "--domain", "spec", "--scope", "src/ only", "--catalog",
                REFS / "domains.md")
        first = L.run_ok(*args).stdout
        self.assertEqual(L.run_ok(*args).stdout, first)                          # same inputs, the same bytes
        self.assertTrue(first.startswith("First read .code-review/reviews/R-0001/BRIEF.md and follow it exactly.\n"))
        self.assertNotRegex(first, r"20\d\d-\d\d-\d\dT")
        self.assertNotIn(str(L.root), first)
        for need in ("Spec conformance (`spec`)", "Scope: src/ only", ".code-review/reviews/R-0001/spec.md",
                     "- G-01 One validator per input.", "- G-02 An empty request is refused.",
                     "ledger.py goals --domain spec", "ISS-0001 [open, M] handler crashes on an empty request",
                     "- spec.1 Is every stated requirement listed, with its source quoted?",
                     "Evidence: A row per requirement"):
            self.assertIn(need, first)
        for other in ("G-03", "ISS-0002", "tests.1", "Not approved yet"):
            self.assertNotIn(other, first)                                         # another domain's, or not approved
        self.assertIn("**Spec conformance (requirements tracing), `spec.md`.**", first)   # the catalog entry
        bare = L.run_ok("prompt", "--review", "R-0001", "--domain", "spec").stdout   # this build has no catalog
        self.assertIn("read the domain's entry in the skill's references/domains.md", bare)
        with self.subTest("the tool command is the one the manager gives"):
            named = L.run_ok(*args, "--tool", "python3 /skill/scripts/ledger.py --root /proj").stdout
            self.assertIn("python3 /skill/scripts/ledger.py --root /proj goals --domain spec", named)
        with self.subTest("refusals"):
            self.assertIn("did not include", refused(self, L, "prompt", "--review", "R-0001", "--domain", "ui"))
            self.assertIn("no review R-0009", refused(self, L, "prompt", "--review", "R-0009", "--domain", "spec"))

    def test_prompt_changes_only_with_its_inputs(self):
        repo, L, sha = briefed(self)
        args = ("prompt", "--review", "R-0001", "--domain", "tests")
        first = L.run_ok(*args).stdout
        ok(L, "propose-goal", "--domain", "spec", "--source", "owner", "--review", "R-0001", "Another spec goal.",
           "--actor", "A-0001")
        ok(L, "approve-goals", "G-05", "--actor", "A-0002")
        self.assertEqual(L.run_ok(*args).stdout, first)          # a goal of another domain is not an input
        ok(L, "propose-goal", "--domain", "tests", "--source", "owner", "--review", "R-0001", "Tests run offline.",
           "--actor", "A-0001")
        ok(L, "approve-goals", "G-06", "--actor", "A-0002")
        self.assertIn("G-06 Tests run offline.", L.run_ok(*args).stdout)


class LintReport(unittest.TestCase):
    def test_lint_report(self):
        repo, L, sha = briefed(self)
        p = lint(L, report(L), "--review", "R-0001")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"spec\.md: ok: 4 checks answered, 1 known issue reported, 1 finding \(H 1\)")

        def problems(text, *args):
            p = lint(L, text, "--review", "R-0001", *args)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            return p.stdout

        with self.subTest("an unanswered checklist item"):
            self.assertIn("checklist item spec.3 is not answered", problems(report(L, drop_check="spec.3")))
        with self.subTest("an answer without evidence, or not one of the words"):
            out = problems(report(L, answers={"spec.1": ("yes", ""), "spec.2": ("maybe", "x y z"),
                                              "spec.3": ("not applicable", "")}))
            self.assertIn("spec.1: give the evidence", out)
            self.assertIn("spec.2: answer 'maybe' is not one of yes, no, partly, not applicable", out)
            self.assertIn("spec.3: say why it does not apply", out)
        with self.subTest("a finding without a location, a severity or a fix"):
            out = problems(report(L, findings=["### SPEC-H1 [H] No location.", "", "- Fix: Do it.", "",
                                               "### SPEC-M2 No severity.", "", "- Where: `demo:src/app.py:1`",
                                               "- Fix: Do it.", "",
                                               "### SPEC-L3 [L] No fix.", "", "- Where: `demo:src/app.py:1`"]))
            self.assertIn("SPEC-H1: no location (- Where: `repo:path:start-end#symbol`)", out)
            self.assertIn("SPEC-M2: no severity", out)
            self.assertIn("SPEC-L3: no fix (- Fix: ...)", out)
        with self.subTest("a location that is not at the reviewed commit"):
            out = problems(report(L, findings=["### SPEC-H1 [H] Wrong lines.", "",
                                               "- Where: `demo:src/app.py:400-401`", "- Fix: Do it.", "",
                                               "### SPEC-H2 [H] Wrong file.", "",
                                               "- Where: `demo:src/nope.py:1`", "- Fix: Do it."]))
            self.assertIn("SPEC-H1:", out)
            self.assertIn("SPEC-H2: src/nope.py does not exist at commit", out)
        with self.subTest("a missing section, a known issue not reported, a duplicate id, an unknown check"):
            self.assertIn("no section '## Totals'", problems(report(L, sections=["Scope and method", "Checklist",
                                                                                "Known issues", "Findings"])))
            self.assertIn("known issue ISS-0001 is not reported", problems(report(L, known=[])))
            out = problems(report(L, findings=["### SPEC-H1 [H] One.", "", "- Where: `demo:src/app.py:1`",
                                               "- Fix: Do it.", "- Check: spec.9", "",
                                               "### SPEC-H1 [H] Two.", "", "- Where: `demo:src/app.py:1`",
                                               "- Fix: Do it."]))
            self.assertIn("SPEC-H1: the id is used twice", out)
            self.assertIn("SPEC-H1: spec.9 is not a checklist item of spec", out)
        with self.subTest("a finding that answers a check takes its severity when it gives none"):
            p = lint(L, report(L, findings=["### SPEC-1 handler crashes.", "", "- Where: `demo:src/app.py:5`",
                                            "- Fix: Refuse it.", "- Check: spec.2"]), "--review", "R-0001")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("SPEC-1: severity H from checklist item spec.2", p.stdout)
        with self.subTest("without --review only the form is checked"):
            p = lint(L, report(L, known=[], findings=["### SPEC-H2 [H] Wrong file.", "",
                                                      "- Where: `demo:src/nope.py:1`", "- Fix: Do it."]))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with self.subTest("the same report gives the same output"):
            bad = report(L, drop_check="spec.3")
            self.assertEqual(problems(bad), problems(bad))
        p = L.run("lint-report", L.dir / "nope.md", "--domain", "spec")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot read", p.stderr)

    def test_lint_report_records_the_answers(self):
        repo, L, sha = briefed(self)
        text = report(L, answers={"spec.1": ("no", "README.md:3 is not listed"),
                                  "spec.4": ("not applicable", "one requirement only")})
        self.assertIn("give --review", refused(self, L, "lint-report", L.dir / "x.md", "--domain", "spec", "--record"))
        p = lint(L, report(L, drop_check="spec.3"), "--review", "R-0001", "--record", "--actor", "A-0001")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(L.one("SELECT count(*) n FROM checklist_answers")["n"], 0)     # a failing report records nothing
        p = lint(L, text, "--review", "R-0001", "--record", "--actor", "A-0001")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("recorded 4 checklist answers on R-0001", p.stdout)
        L.commit()
        got = {r["check_code"]: (r["answer"], r["evidence"], r["report"]) for r in L.all("SELECT * FROM checklist_answers")}
        self.assertEqual(got["spec.1"], ("no", "README.md:3 is not listed", "spec.md"))
        self.assertEqual(got["spec.4"], ("not-applicable", "one requirement only", "spec.md"))
        p = lint(L, report(L), "--review", "R-0001", "--record", "--actor", "A-0001")     # a corrected report
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(L.one("SELECT answer FROM checklist_answers WHERE check_code='spec.1'")["answer"], "yes")
        self.assertEqual(L.one("SELECT count(*) n FROM checklist_answers")["n"], 4)


class ChecklistsAreData(unittest.TestCase):
    def test_checklists_equal_the_catalog(self):
        """references/domains.md's 'Checklists' table and the seeded domain_checks rows are the same."""
        doc = (REFS / "domains.md").read_text(encoding="utf-8")
        table = []
        for line in doc[doc.index("## Checklists"):].split("\n## ")[0].split("\n"):
            m = re.match(r"\| `([a-z-]+)\.(\d+)` \| ([HMLT]) \| (.+?) \| (.+?) \|$", line)
            if m:
                table.append((f"{m.group(1)}.{m.group(2)}", m.group(1), int(m.group(2)), m.group(3), m.group(4),
                              m.group(5)))
        with fixtures.Ledger(fixtures.temp_dir("checks-")) as L:
            seeded = [(r["code"], r["domain_code"], r["position"], r["severity"], r["question"], r["evidence"])
                      for r in L.all("SELECT * FROM domain_checks ORDER BY rowid")]
            domains = {r["code"] for r in L.all("SELECT code FROM domains")}
        self.assertEqual(table, seeded)
        self.assertGreater(len(table), 100)
        self.assertEqual({t[1] for t in table}, domains, "every catalog domain has a checklist")
        for code, domain, n, sev, question, evidence in table:
            self.assertTrue(question.endswith("?"), code)
            self.assertTrue(evidence.endswith("."), code)

    def test_brief_rules_equal_the_reference(self):
        """The reviewer rules and the report format the tool prints are the ones references/brief.md gives."""
        repo, L, sha = briefed(self)
        brief = L.run_ok("brief", "--review", "R-0001").stdout
        ref = (REFS / "brief.md").read_text(encoding="utf-8")
        norm = lambda text: " ".join(text.split())
        for title, ref_title in (("## Rules for reviewers", "### Rules for reviewers"),
                                 ("## Report format", "### Report format")):
            mine = brief[brief.index(title) + len(title):]
            mine = mine[:mine.index("\n## ")] if "\n## " in mine else mine
            theirs = ref[ref.index(ref_title):]
            theirs = theirs[theirs.index("\n") + 1:]
            theirs = re.split(r"\n##+ ", theirs)[0]
            self.assertEqual(norm(mine), norm(theirs).replace("<review>", "R-0001"), title)


if __name__ == "__main__":
    unittest.main()
