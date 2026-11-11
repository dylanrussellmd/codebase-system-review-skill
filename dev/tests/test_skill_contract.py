"""The SKILL.md contract (plan P-0001 phase 5: steps 107-110, 114-116, 118-120, 124, 126, 128): what SKILL.md and the
reference files tell an agent to run, name or write must exist in the tool and the schema, and the rules the owner
asked for must be stated where an agent following SKILL.md literally will meet them.

Words come from a fresh ledger (the lookup tables and enum_labels, the single source of status words), commands and
flags from the built ledger.py's --help, so the lint follows the tool rather than a copy of its lists.
"""
import re
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, run_ledger, temp_dir

REPO = fixtures.REPO_ROOT
SKILL_MD = REPO / "SKILL.md"
REFS = REPO / "references"

# Column (as SKILL.md writes it) -> (lookup table, None) or (None, enum_labels enum)
WORD_LISTS = {
    "issues.status": ("issue_statuses", None),
    "findings.match_type": ("match_types", None),
    "gates.kind": ("gate_kinds", None),
    "reviews.status": (None, "review_status"),
    "plans.status": (None, "plan_status"),
    "steps.status": (None, "step_status"),
    "steps.owner_need": (None, "owner_need"),
    "findings.verification": (None, "verification"),
    "review_issue_checks.outcome": (None, "check_outcome"),
    "metrics.method": (None, "metric_method"),
    "artifacts.kind": (None, "artifact_kind"),
}
GLOSSARY = ("ledger", "review", "domain", "point", "finding", "issue", "plan", "step", "gate", "decision", "hub")
GLOBAL_FLAGS = {"--root", "--db", "--version", "--help", "-h"}
# The owner-interview commands of step 129, being built alongside step 130's SKILL.md text: until the CLI has them,
# SKILL.md may name them (and their flags are not checked); once it has them, they are checked like any other.
PENDING = {"interview-ask": {"--review", "--domain", "--why", "--actor"},
           "interview-answer": {"--via", "--actor"},
           "propose-goal": {"--domain", "--from", "--actor"},
           "approve-goals": {"--actor"},
           "goals": {"--domain", "--json"},
           "interview": {"--review", "--domain"}}


def skill_text():
    return SKILL_MD.read_text(encoding="utf-8")


def doc_files():
    """SKILL.md and every Markdown reference file (not the schema)."""
    return [SKILL_MD] + sorted(REFS.rglob("*.md"))


def body(text):
    """The text after the frontmatter."""
    return text.split("---\n", 2)[2] if text.startswith("---\n") else text


def code_spans(text):
    """Every inline code span (one may run across a line break), every line of a fenced block, and every indented
    example line that starts with ledger.py or python3."""
    fenced, prose, fence = [], [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        (fenced if fence else prose).append(line.strip() if fence else line)
    spans = [" ".join(s.split()) for s in re.findall(r"`([^`]+)`", "\n".join(prose))]
    indented = [l.strip() for l in prose if l.startswith("    ") and l.strip().startswith(("ledger.py", "python3"))]
    return spans + fenced + indented


def section(text, title):
    """The section whose heading starts with title, up to the next heading of the same or a higher level."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"(#+) (.*)", line)
        if m and m.group(2).startswith(title):
            level = len(m.group(1))
            j = i + 1
            while j < len(lines) and not (re.match(r"#+ ", lines[j]) and len(lines[j]) - len(lines[j].lstrip("#")) <= level):
                j += 1
            return "\n".join(lines[i:j])
    raise AssertionError(f"no section {title!r}")


def slug(heading):
    """GitHub's anchor for a heading."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


class Tool:
    """The built CLI's commands and each command's flags, read from --help once."""
    _cache = None

    @classmethod
    def get(cls):
        if cls._cache is None:
            lp = fixtures.built_ledger_path()
            top = run_ledger("--help", ledger_py=lp, cwd=temp_dir("help-")).stdout
            names = re.search(r"\{([a-z,-]+)\}", top).group(1).split(",")
            flags = {}
            for n in names:
                h = run_ledger(n, "--help", ledger_py=lp, cwd=temp_dir("help-")).stdout
                flags[n] = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*|-[a-z])\b", h)) | GLOBAL_FLAGS
            cls._cache = (set(names), flags)
        return cls._cache


class Schema:
    _cache = None

    @classmethod
    def get(cls):
        if cls._cache is None:
            with Ledger(temp_dir("contract-")) as L:
                tables = {r["name"] for r in L.all("SELECT name FROM sqlite_schema WHERE type='table'")}
                views = {r["name"] for r in L.all("SELECT name FROM sqlite_schema WHERE type='view'")}
                cols = {t: {r["name"] for r in L.all(f'PRAGMA table_info("{t}")')} for t in tables | views}
                words = {}
                for col, (table, enum) in WORD_LISTS.items():
                    words[col] = ({r["value"] for r in L.all(f"SELECT value FROM {table}")} if table else
                                  {r["value"] for r in L.all("SELECT value FROM enum_labels WHERE enum=?", (enum,))})
                domains = {r["code"]: dict(r) for r in L.all("SELECT * FROM domains")}
            cls._cache = (tables, views, cols, words, domains)
        return cls._cache


def ledger_commands(span):
    """[(command, the rest of the span)] for each `ledger.py [global flags] <command> ...` in a span, and for a span that
    starts with a command name itself (`add-actor drafter ...`)."""
    names, _ = Tool.get()
    span = re.split(r"(?:^|\s)#\s", span, maxsplit=1)[0]
    span = re.sub(r"<[^<>]*>", "PLACEHOLDER", span)   # <project root>, <model id> ...
    out = []
    for m in re.finditer(r"ledger\.py((?:\s+--\S+(?:\s+(?!-)\S+)?)*)\s+([a-z][a-z0-9-]*)(.*?)(?=ledger\.py|$)", span):
        out.append((m.group(2), m.group(3)))
    first = span.split(" ", 1)
    if not out and (first[0] in names or first[0] in PENDING):
        out.append((first[0], first[1] if len(first) > 1 else ""))
    return out


class Commands(unittest.TestCase):

    def test_skill_names_only_existing_commands(self):
        names, flags = Tool.get()
        problems = []
        for f in doc_files():
            for span in code_spans(f.read_text(encoding="utf-8")):
                for cmd, rest in ledger_commands(span):
                    if cmd not in names and cmd in PENDING:
                        bad = set(re.findall(r"(?<![\w'=-])(--[a-z][a-z-]*)", rest)) - PENDING[cmd] - GLOBAL_FLAGS
                        if bad:
                            problems.append(f"{f.name}: `{span}` gives {cmd} {sorted(bad)}, not in step 129's design")
                        continue
                    if cmd not in names:
                        if "ledger.py" in span:
                            problems.append(f"{f.name}: `{span}` names ledger.py {cmd}, which the CLI does not have")
                        continue
                    for flag in re.findall(r"(?<![\w'=-])(--[a-z][a-z-]*)", rest):
                        if flag not in flags[cmd]:
                            problems.append(f"{f.name}: `{span}` gives {cmd} the flag {flag}, which it does not take")
        self.assertEqual(problems, [])

    def test_skill_uses_the_write_commands(self):
        text = skill_text()
        for cmd in ("add-actor", "add-repo", "new-review", "review-status", "snapshot", "add-run", "open-gate",
                    "answer-gate", "apply-gate", "load-index", "match", "tag-review", "fill-refs", "set-report",
                    "set-narrative", "import-steps", "step-status", "issue-status", "report-finding", "query",
                    "progress-page", "progress-md", "import-answers", "dump", "check", "gates"):
            self.assertRegex(text, rf"ledger\.py {cmd}\b", f"SKILL.md never tells the agent to run ledger.py {cmd}")

    def test_skill_steps_have_no_hand_written_sql(self):
        """Writes in Steps 0-9 are commands; no INSERT/UPDATE in SKILL.md itself."""
        self.assertNotRegex(skill_text(), r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b")

    def test_model_agnostic(self):
        """No model or vendor is assumed: harness names appear only in SKILL.md's harness table and README's install
        table (goal G-10)."""
        for f in doc_files() + [REPO / "README.md"]:
            lines = f.read_text(encoding="utf-8").split("\n")
            for no, line in enumerate(lines, 1):
                if line.startswith("|"):
                    continue   # the compatibility tables
                with self.subTest(file=f.name, line=no):
                    self.assertNotRegex(line, r"(?i)\b(anthropic|claude(?! ?\.md)|opus|sonnet|gpt|gemini)\b|opencode",
                                        f"{f.name}:{no}: {line}")

    def test_no_harness_command_blocks(self):
        """Claude Code runs !`cmd` spans and ```! fences of SKILL.md when it loads the skill: none may exist."""
        for f in doc_files():
            with self.subTest(file=f.name):
                self.assertNotRegex(f.read_text(encoding="utf-8"), r"!`|^\s*```!")

    def test_backtick_spans_stay_on_one_line(self):
        fence = False
        for no, line in enumerate(skill_text().split("\n"), 1):
            if line.lstrip().startswith("```"):
                fence = not fence
                continue
            if not fence:
                self.assertEqual(line.count("`") % 2, 0, f"SKILL.md line {no} splits a code span: {line}")


class Words(unittest.TestCase):

    def test_skill_lists_every_word_list(self):
        """Each `table.column`: `a`, `b` ... list in SKILL.md is exactly the schema's words (fails on a missing or an
        invented value, e.g. 'production action' or a gate kind the schema refuses)."""
        _, _, _, words, _ = Schema.get()
        listed = {}
        for m in re.finditer(r"^- `([a-z_]+\.[a-z_]+)`[^:\n]*: ((?:`[^`]+`[,.]?\s*)+)", skill_text(), re.M):
            listed[m.group(1)] = re.findall(r"`([^`]+)`", m.group(2))
        self.assertEqual(set(listed), set(WORD_LISTS), "SKILL.md's 'Words the ledger accepts' lists")
        for col, vals in listed.items():
            with self.subTest(column=col):
                self.assertEqual(set(vals), words[col])

    def test_skill_gate_kinds_exist(self):
        _, _, _, words, _ = Schema.get()
        kinds = words["gates.kind"]
        bad = []
        for f in doc_files():
            text = " ".join(f.read_text(encoding="utf-8").split())
            for m in re.finditer(r"`([a-z-]+)` gate\b|gates? of kind `([a-z-]+)`|open-gate ([a-z-]+)\b", text):
                k = m.group(1) or m.group(2) or m.group(3)
                if k not in kinds:
                    bad.append(f"{f.name}: gate kind {k!r}")
        self.assertEqual(bad, [])

    def test_owner_need_maps_to_gate_kinds(self):
        line = re.search(r"^- `steps\.owner_need`.*$", skill_text(), re.M).group(0)
        self.assertRegex(line, r"gates `decision`, `confirm`, `other`, `production-action`")
        self.assertNotIn("production action", skill_text())

    def test_skill_mentions_every_status(self):
        """Every issue status appears in the 'What moves an issue' table, each with how it is recorded."""
        _, _, _, words, _ = Schema.get()
        table = section(skill_text(), "Words the ledger accepts").split("What moves an issue", 1)[1]
        for s in words["issues.status"]:
            self.assertRegex(table, rf"(?<![\w-]){s}(?![\w-])", f"issue status {s} has no row saying when it is set")
        for s in ("in-progress", "complete", "superseded"):
            self.assertRegex(skill_text() + (REFS / "recipes.md").read_text(), rf"`{s}`")
        self.assertIn("review-status R-0001 abandoned", skill_text())

    def test_skill_mentions_listed_columns(self):
        docs = "\n".join(f.read_text(encoding="utf-8") for f in doc_files())
        for name in ("match_rationale", "still_exists", "last_confirmed_review_id", "tag_pushed",
                     "finding_locations.role", "page_url", "review_issue_checks.finding_id",
                     "report_path", "owner_change", "category", "source"):
            self.assertIn(name, docs, f"the docs never say when to set {name}")
        # each is set by a command the docs name
        for cmd in ("confirm-guard", "tag-review R-0001 --pushed", "add-artifact progress-page", "finish-run",
                    "review-domain", "add-domain", "add-requirement"):
            self.assertIn(f"ledger.py {cmd}", docs, f"the docs never name ledger.py {cmd}")

    def test_docs_name_existing_tables_columns_and_views(self):
        tables, views, cols, _, _ = Schema.get()
        bad = []
        for f in doc_files():
            text = f.read_text(encoding="utf-8")
            for span in re.findall(r"`([^`\n]+)`", text):
                for m in re.finditer(r"\b([a-z_]+)\.([a-z_]+)\b", span):
                    t, c = m.groups()
                    if c in ("md", "jsonl", "sql", "sqlite", "tsv", "py", "html", "txt"):
                        continue   # a file name such as domains.md
                    if t in tables | views and c not in cols[t]:
                        bad.append(f"{f.name}: {t}.{c}")
                for v in re.findall(r"\bv_[a-z_]+\b", span):
                    if v not in views:
                        bad.append(f"{f.name}: view {v}")
        self.assertEqual(bad, [])


class Structure(unittest.TestCase):

    def test_skill_md_budget(self):
        text = skill_text()
        # Step 124 set 350 lines (from 845); the owner interview the owner asked for later (OWN-5, step 130) added
        # about 20, so the budget is 375.
        self.assertLessEqual(len(text.splitlines()), 375, "SKILL.md is over its 375-line budget")
        for ref in set(re.findall(r"references/[A-Za-z0-9_./#-]+", text)):
            path, _, anchor = ref.rstrip(".").partition("#")
            p = REPO / path
            with self.subTest(ref=ref):
                self.assertTrue(p.exists(), f"SKILL.md names {path}, which does not exist")
                if anchor:
                    heads = [slug(h) for h in re.findall(r"^#+ (.+)$", p.read_text(encoding="utf-8"), re.M)]
                    self.assertIn(anchor, heads, f"{path} has no heading for #{anchor}")

    def test_reference_anchors_exist(self):
        for f in doc_files():
            for ref in set(re.findall(r"references/[A-Za-z0-9_./-]+\.md#[a-z0-9-]+", f.read_text(encoding="utf-8"))):
                path, anchor = ref.split("#")
                heads = [slug(h) for h in re.findall(r"^#+ (.+)$", (REPO / path).read_text(encoding="utf-8"), re.M)]
                self.assertIn(anchor, heads, f"{f.name} links {ref}")

    def test_skill_says_when_to_read_each_reference(self):
        text = section(skill_text(), "Reference files")
        for f in sorted(REFS.rglob("*.md")):
            rel = f.relative_to(REPO).as_posix()
            self.assertIn(rel, text, f"SKILL.md's Reference files never says when to read {rel}")

    def test_glossary_terms_defined_before_use(self):
        text = body(skill_text())
        text = text.replace(re.search(r"^# .*$", text, re.M).group(0), "", 1)   # the title
        start = text.index("## Words used here")
        end = text.index("\n## ", start + 1)
        gloss = text[start:end]
        for term in GLOSSARY:
            with self.subTest(term=term):
                self.assertRegex(gloss, rf"\*\*{term}\*\*")
                first = re.search(rf"\b{term}s?\b", text, re.I)
                self.assertGreaterEqual(first.start(), start, f"{term!r} is used before the glossary")

    def test_domain_catalog_moved_with_applies_index(self):
        _, _, _, _, domains = Schema.get()
        text = skill_text()
        index = re.search(r"^Index \(code: applies when\):(.*?)\n\n", text, re.M | re.S).group(1)
        for code in domains:
            self.assertIn(f"`{code}`", index, f"SKILL.md's domain index lacks {code}")
        catalog = (REFS / "domains.md").read_text(encoding="utf-8")
        applies = {m.group(1): " ".join(m.group(2).split())
                   for m in re.finditer(r"\*\*[^*]+?, `([^`]+)`\.\*\*\s*\*([^*]*[Aa]ppl[^*]*)\*", catalog)}
        for d in domains.values():
            with self.subTest(domain=d["code"]):
                self.assertEqual(applies.get(d["report_file"]), d["description"])

    def test_catalog_mentions_generated_settings_and_response_handling(self):
        catalog = " ".join((REFS / "domains.md").read_text(encoding="utf-8").split())
        self.assertIn("settings forms generated from the settings schema", catalog)
        self.assertIn("handle every response the callee can give", catalog)

    def test_no_scheduling_terms(self):
        for f in doc_files():
            text = f.read_text(encoding="utf-8")
            with self.subTest(file=f.name):
                self.assertNotRegex(text, r"engine-A|[Tt]ime off|time-off|progress\.json|AskUserQuestion")


class OwnerRules(unittest.TestCase):

    def test_one_writer_names_the_viewer(self):
        text = " ".join(section(skill_text(), "The tool").split())
        self.assertRegex(text, r"\*\*One writer\.\*\* Only the manager writes, except: the viewer and `import-answers` "
                               r"write gate answers")
        self.assertIn("`BEGIN IMMEDIATE`", text)
        self.assertIn("`--actor`", text)

    def test_implementers_write_their_own_steps_only(self):
        """Owner decision D-0011, said the same way in SKILL.md and the implementer brief."""
        own = "`step-status`, `add-commit`, `add-guard`, `report-finding`"
        text = " ".join(section(skill_text(), "The tool").split())
        self.assertIn(f"an implementer writes its own steps ({own}) under its own actor", text)
        self.assertIn("Reviewers, verifiers, indexers, matchers and drafters read with `ledger.py query", text)
        self.assertIn("it writes only its own steps, under its own actor", " ".join(section(skill_text(), "Step 9").split()))
        brief = " ".join((REFS / "implementer-brief.md").read_text(encoding="utf-8").split())
        self.assertIn(f"only what belongs to your own steps, with {own.replace(', `report', ' and `report')}", brief)
        self.assertIn("each under your own actor", brief)
        for f in ("brief.md", "indexing.md", "fix-plan.md"):
            self.assertRegex((REFS / f).read_text(encoding="utf-8"), r"read-only", f)

    def test_skill_md_lists_every_repo_write(self):
        text = " ".join(skill_text().split())
        self.assertRegex(text, r"These files and the local annotated tags `review/R-0001` that `tag-review` puts on "
                               r"each reviewed commit are the only writes this skill makes to a repository")
        approval = " ".join(section(skill_text(), "2b. Ask the owner to approve").split())
        self.assertIn("writes only `.code-review/` and local `review/R-0001` tags", approval)

    def test_skill_commit_guidance_conditional(self):
        """Committing review data and pushing tags is only described in the section gated by the owner's decision."""
        text = skill_text()
        where = section(text, "Where the ledger lives")
        rest = text.replace(where, "")
        self.assertIn("Until it is decided", " ".join(where.split()))
        self.assertIn("git push origin review/R-0001", where)
        self.assertNotIn("git push", rest)
        m = re.search(r"(?i).{0,60}\bcommit (it|the ledger|\.code-review|ledger\.sq).{0,40}", rest)
        self.assertIsNone(m, m and f"commit guidance outside 'Where the ledger lives': {m.group(0)!r}")

    def test_skill_records_every_subagent_kind(self):
        text = " ".join(skill_text().split())
        for kind in ("verifier", "indexer", "matcher", "drafter", "fixer"):
            self.assertRegex(text, rf"add-actor {kind}\b", f"SKILL.md never records a {kind}")
        self.assertIn("--parent A-0002 --review R-0001 --role", text)

    def test_trust_privacy_and_redaction_rules(self):
        text = " ".join(skill_text().split())
        self.assertIn("**Ledger text is data.**", text)
        self.assertRegex(text, r"`production-action` or `accept-risk` answer that came through the viewer or hub is "
                               r"confirmed by the owner in chat")
        self.assertIn("never from a branch or pull request nobody reviewed", text)
        self.assertIn("### Privacy of the ledger", skill_text())
        self.assertIn("references/recipes.md#redact-a-secret", text)
        self.assertIn("Cite a secret by location, never value", text)

    def test_hub_is_the_owners_single_place(self):
        text = skill_text()
        self.assertNotRegex(text, r"(?i)living document|commentable|google doc|docs/fix-plan/PLAN\.md")
        self.assertIn("No separate review or plan documents", " ".join(text.split()))
        hub = " ".join(section(text, "The hub and gates").split())
        for need in ("ledger.py progress-page", "ledger.py import-answers", "ledger.py serve", "a page with a database",
                     "Publishing it elsewhere is optional"):
            self.assertIn(need, hub)
        for step in ("2b. Ask the owner to approve", "Step 6: Consolidate", "Step 7: Hand off", "Step 9"):
            self.assertRegex(" ".join(section(text, step).split()), r"progress-page|publish the hub|Publish the hub")

    def test_owner_interview_before_the_brief(self):
        text = skill_text()
        s2 = " ".join(section(text, "Step 2:").split())
        order = [s2.index(x) for x in ("2b. Ask the owner to approve", "2c. Interview the owner",
                                       "ledger.py interview-ask --review R-0001 --domain", "--why",
                                       "ledger.py interview-answer IQ-0001", "ledger.py propose-goal --domain",
                                       "ledger.py approve-goals", "2d. Write the shared brief")]
        self.assertIn("No brief before that", s2)
        self.assertEqual(order, sorted(order))
        self.assertIn("Step 0's draft", s2)
        s3 = " ".join(section(text, "Step 3:").split())
        self.assertIn("its domain's approved goals, the line `ledger.py goals --domain <D>`", s3)
        s8 = " ".join(section(text, "Step 8:").split())
        self.assertIn("each step names the approved goals", s8)
        self.assertIn("conflicts with an approved goal is put to the owner", s8)
        self.assertIn("ledger.py goals", " ".join(section(text, "Step 9").split()))
        brief = " ".join((REFS / "brief.md").read_text(encoding="utf-8").split())
        self.assertIn("goals --domain", brief)
        self.assertIn("ledger.py goals", " ".join((REFS / "implementer-brief.md").read_text(encoding="utf-8").split()))

    def test_step9_makes_discoveries_a_hard_rule(self):
        s9 = " ".join(section(skill_text(), "Step 9").split())
        self.assertIn("**Discoveries (mandatory):**", s9)
        self.assertIn("ledger.py report-finding", s9)
        self.assertIn("`v_unplanned_discoveries` must be empty before each commit", s9)
        self.assertIn("`git show <commit>:<path>`", s9)
        self.assertIn("never from an edited working file", s9)
        self.assertIn("references/implementer-brief.md", s9)
        brief = " ".join((REFS / "implementer-brief.md").read_text(encoding="utf-8").split())
        self.assertIn("never in your edited working file", brief)

    def test_review_artifacts_stay_in_the_ledger_folder(self):
        text = skill_text()
        self.assertIn(".code-review/reviews/R-0001/BRIEF.md", text)
        self.assertNotRegex(text, r"(?i)outside the repositories \(scratchpad\)|scratch folder for the brief")
        recipe = section((REFS / "recipes.md").read_text(encoding="utf-8"), "Record an artifact")
        self.assertIn("never record a path in a scratch folder", " ".join(recipe.split()))


if __name__ == "__main__":
    unittest.main()
