"""The skill depends on no one model or harness (plan P-0001 step 144, owner goal G-10).

test_model_agnostic             no shipped file names a Claude or Anthropic model or product outside the places that
                                must: the compatibility tables, env's detection list, the instruction-file list,
                                the answer-store adapter, the tool catalog's rows (each with its fallback),
                                CHANGELOG.md and the migrations already applied
test_features_have_fallbacks    every harness feature SKILL.md and references/ name sits beside its fallback, in the
                                same paragraph, bullet or table row
test_hub_answers_without_store  (browser) the hub with no answer store takes an answer, keeps it in the browser and
                                downloads it as answers.json, which import-answers records
"""
import ast
import json
import re
import subprocess
import unittest

from dev.tests import browser, fixtures

ROOT = fixtures.REPO_ROOT
NAMES = re.compile(r"(?i)claude|anthropic")
APPLIED_MIGRATIONS = 16   # 0001-0016 were applied by released versions: their text stays as it was

# (file pattern, line pattern, why the name may stand there). A line that names the product passes only if one
# entry matches both its file and its text.
ALLOWED = [
    (r"\.md$", r"^\| (OpenCode|Claude Code)\b", "a row of a compatibility table (first cell: a harness)"),
    (r".", r"AGENTS\.md, CLAUDE\.md, \.cursorrules", "the instruction files read as inputs"),
    (r"^dev/core\.py$", r'"\(\^\|/\)\(readme\|.*agents\|claude\)', "scan's instruction-file names"),
    (r"^dev/page\.html$", r"window\.claude\b", "the answer-store adapter for an artifact page (one option)"),
    (r"^dev/tests/test_browser_p5_tools_hub\.py$", r"window\.claude\b", "a stand-in for that answer store"),
    (r"^dev/tests/test_w4_env\.py$", r".", "the tests of env's detection list"),
    (r"^dev/tests/", r'"CLAUDE"|CLAUDECODE', "clearing the detection variables for a test"),
    (r"^dev/tests/", r"\(\?i\)|assertNot(In|Regex)\(", "a check that the name is absent"),
    (r"^dev/tests/data/", r"-- e\.g\. Claude Code", "a copy of the first schema, as released"),
    (r"^references/tools\.md$", r"^\|", "a row of the tool catalog, each with its fallback"),
]
EXEMPT = re.compile(r"^(CHANGELOG\.md|scripts/ledger\.py|dev/tests/test_w5_model_agnostic\.py)$|"
                    r"^\.code-review/|__pycache__|\.pyc$")


def shipped_files():
    """Every file of the repository, committed or new (git's ignore rules apply)."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    return sorted({p for p in out if p and (ROOT / p).is_file() and not EXEMPT.search(p)})


def detection_lines():
    """The line numbers of dev/core.py's detection list: the functions env uses to recognise a harness."""
    tree = ast.parse((ROOT / "dev" / "core.py").read_text(encoding="utf-8"))
    out = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ("running_harness", "mcp_connectors"):
            out.update(range(node.lineno, node.end_lineno + 1))
    return out


class ModelAgnostic(unittest.TestCase):
    def test_model_agnostic(self):
        detection = detection_lines()
        bad = []
        for path in shipped_files():
            m = re.match(r"references/migrations/(\d{4})-", path)
            if m and int(m.group(1)) <= APPLIED_MIGRATIONS:
                continue
            try:
                text = (ROOT / path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.split("\n"), 1):
                if not NAMES.search(line):
                    continue
                if path == "dev/core.py" and n in detection:
                    continue
                if any(re.search(f, path) and re.search(t, line) for f, t, _ in ALLOWED):
                    continue
                if path == "references/tools.jsonl" and str(json.loads(line).get("fallback") or "").strip():
                    continue   # a tool of the catalog that names a harness, with the way without it
                bad.append(f"{path}:{n}: {line.strip()[:120]}")
        self.assertEqual(bad, [])

    def test_features_have_fallbacks(self):
        feature = re.compile(r"(?i)subagent tool|question tool|answer store|design (canvas|connector)|"
                             r"published (page|copy)|publishing it elsewhere|artifact page|cloud browser")
        fallback = re.compile(r"(?i)\b(without|else|otherwise|when it has one|optional|fallback|in turn)\b")
        files = [ROOT / "SKILL.md"] + sorted((ROOT / "references").rglob("*.md"))
        missing = []
        for f in files:
            lines = f.read_text(encoding="utf-8").split("\n")
            header = ""
            for block in paragraphs(lines):
                text = " ".join(block)
                if block[0].startswith("|"):
                    header = block[0] if not header else header
                if not feature.search(text):
                    continue
                if block[0].startswith("|") and "without" in header and block[0].rstrip("| ").split("|")[-1].strip():
                    continue
                if not fallback.search(text):
                    missing.append(f"{f.relative_to(ROOT)}: {text[:140]}")
        self.assertEqual(missing, [])


def paragraphs(lines):
    """Blocks of text: a paragraph ends at a blank line; each bullet and each table row is a block of its own."""
    out, cur = [], []
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("- ", "| ", "```", "#")) or re.match(r"\d+\. ", s):
            if cur:
                out.append(cur)
            cur = [s] if s and not s.startswith(("```", "#")) else []
            if s.startswith("|"):
                out.append(cur)
                cur = []
            continue
        cur.append(s)
    if cur:
        out.append(cur)
    return [b for b in out if b]


class HubWithoutStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = fixtures.Ledger()
        fixtures.seed_demo(cls.L)
        fixtures.finish_demo(cls.L)
        cls.L.commit()
        cls.hub = cls.L.root / "hub.html"
        cls.L.run_ok("progress-page", "-o", str(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_hub_answers_without_store(self):
        L = self.L
        p = browser.ViewerPage(self.browser, self.hub.as_uri())
        self.addCleanup(p.close)
        p.page.goto(self.hub.as_uri() + "#inbox")
        p.settle()
        code = L.scalar("SELECT code FROM gates WHERE status='open' AND options IS NOT NULL ORDER BY id")
        card = p.page.locator(".card.gate", has=p.page.locator(f"input[name='opt-{code}']"))
        card.locator("input[type=radio]").first.check()
        card.locator("button").click()
        p.settle(quiet_ms=60)
        link = p.page.locator(".card.gate a[download]")
        self.assertEqual(link.count(), 1)
        self.assertEqual(link.get_attribute("download"), "answers.json")
        saved = p.js("async a => await (await fetch(a.href)).text()", link.element_handle())
        self.assertEqual([a["gate"] for a in json.loads(saved)], [code])
        # the answer is still there after a reload of the same file
        p.page.reload()
        p.settle()
        p.js("location.hash = 'inbox'")
        p.settle(quiet_ms=60)
        self.assertIn("You answered", p.page.locator(".card.gate", has=p.page.locator("a[download]")).inner_text())
        f = L.root / "answers.json"
        f.write_text(saved)
        self.assertIn(f"{code}: recorded", L.run_ok("import-answers", str(f)).stdout)
        self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], []))


if __name__ == "__main__":
    unittest.main()
