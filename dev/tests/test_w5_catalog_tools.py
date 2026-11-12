"""The tool catalog (plan P-0001 step 142): references/tools.md and references/tools.jsonl.

test_tool_catalog          every catalog domain (and the review process) has a section; every tool ledger.py calls is
                           listed with its fallback
test_tables_match_jsonl    each domain's table holds exactly the must-have and strong rows of tools.jsonl
test_jsonl_rows            every row names a catalog domain, a rating and the fields a reader needs
test_shortlist_and_design  the cross-domain shortlist and the one-way-to-run-every-tool design are there
"""
import json
import re
import unittest

from dev.tests import fixtures

REPO = fixtures.REPO_ROOT
TOOLS_MD = REPO / "references" / "tools.md"
TOOLS_JSONL = REPO / "references" / "tools.jsonl"
RATINGS = ("must-have", "strong", "situational", "skip")
# Domains added after the tool surveys of 2026-11-11; each gets a section when a survey covers it.
NOT_SURVEYED = {"features"}
FIELDS = ("domain", "group", "tool", "rating", "link", "adds", "call", "deterministic", "install_licence", "offline",
          "harness", "fallback", "maturity", "reason", "sends_data", "runs_project_code")


def catalog_domains():
    """The domain codes seeded in references/schema.sql, plus the review process itself."""
    schema = (REPO / "references" / "schema.sql").read_text(encoding="utf-8")
    seed = schema[schema.index("INSERT INTO domains("):]
    seed = seed[:seed.index(";")]
    return [d for d in re.findall(r"\('([a-z-]+)',", seed) if d not in NOT_SURVEYED] + ["process"]


def sections(text):
    """{domain code: the text of its '### Name (`code`)' section}."""
    out = {}
    for m in re.finditer(r"^### [^\n]*\(`([a-z-]+)`\)\n(.*?)(?=^##|\Z)", text, re.M | re.S):
        out[m.group(1)] = m.group(2)
    return out


def table_rows(block):
    """The cells of each body row of the Markdown tables in block."""
    rows = []
    for line in block.split("\n"):
        if line.startswith("| ") and not line.startswith("| Tool |"):
            rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]])
    return rows


def tool_name(cell):
    m = re.match(r"\[(.*)\]\(", cell)
    return (m.group(1) if m else cell).replace("\\|", "|")


def jsonl():
    return [json.loads(line) for line in TOOLS_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]


class ToolCatalog(unittest.TestCase):

    def test_tool_catalog(self):
        text = TOOLS_MD.read_text(encoding="utf-8")
        secs = sections(text)
        for d in catalog_domains():
            with self.subTest(domain=d):
                self.assertIn(d, secs, f"references/tools.md has no section for the domain {d}")
                self.assertRegex(secs[d], r"(?m)^Top 3 \(each optional, with the fallback in its row\): \S")
                self.assertIn(f"domain `{d}`", secs[d], "no line pointing to the full list")
        # Every tool ledger.py runs or imports is in "Tools the skill calls" with a fallback.
        core = (REPO / "dev" / "core.py").read_text(encoding="utf-8")
        called = set(re.findall(r'subprocess\.(?:run|Popen)\(\["([a-z]+)"', core))
        if "npx" in called:
            called.discard("npx")
            called |= set(re.findall(r'"npx", "--no-install", "([a-z]+)"', core))
        if "from playwright" in core:
            called.add("playwright")
        self.assertTrue({"git", "lighthouse", "playwright"} <= called, called)
        block = text[text.index("## Tools the skill calls"):text.index("## Cross-domain shortlist")]
        rows = table_rows(block)
        for tool in sorted(called):
            with self.subTest(tool=tool):
                row = next((r for r in rows if tool in r[0].lower()), None)
                self.assertIsNotNone(row, f"{tool} is called by ledger.py but not listed")
                self.assertGreater(len(row[2]), 10, f"{tool} has no fallback")
        self.assertIn("npx --no-install lighthouse", block)

    def test_tables_match_jsonl(self):
        secs = sections(TOOLS_MD.read_text(encoding="utf-8"))
        rows = jsonl()
        for d in catalog_domains():
            with self.subTest(domain=d):
                want = sorted(r["tool"].replace("`", "").replace("<", "&lt;").replace(">", "&gt;")
                              for r in rows if r["domain"] == d and r["rating"] in ("must-have", "strong"))
                got = sorted(tool_name(r[0]) for r in table_rows(secs[d]))
                self.assertEqual(got, want)
                self.assertTrue(all(len(r) == 9 and r[7] for r in table_rows(secs[d])), "a row without a fallback")

    def test_jsonl_rows(self):
        rows = jsonl()
        domains = set(catalog_domains())
        self.assertGreater(len(rows), 600)
        self.assertEqual({r["domain"] for r in rows}, domains, "every domain has rows, and only catalog domains")
        for r in rows:
            with self.subTest(tool=r.get("tool"), domain=r.get("domain")):
                self.assertTrue(set(FIELDS) <= set(r), set(FIELDS) - set(r))
                self.assertIn(r["rating"], RATINGS)
                self.assertTrue(r["tool"].strip() and r["adds"].strip())
                self.assertIsInstance(r["sends_data"], bool)
                self.assertIsInstance(r["runs_project_code"], bool)
        self.assertEqual(len({(r["domain"], r["tool"]) for r in rows}), len(rows), "one row per tool and domain")

    def test_shortlist_and_design(self):
        text = TOOLS_MD.read_text(encoding="utf-8")
        short = text[text.index("## Cross-domain shortlist"):text.index("## One way to run every tool")]
        for tool in ("ast-grep", "Semgrep", "Coverage", "Mutation", "SARIF", "JUnit", "gitleaks", "OSV-Scanner", "Syft",
                     "Playwright", "axe-core", "Vale", "lychee", "scc", "oasdiff"):
            self.assertIn(tool, short)
        design = text[text.index("## One way to run every tool"):text.index("## Correctness and architecture")]
        for words in ("version, command, exit code", "sha256", "must-be-empty", "offline", "Pinned", "runs code",
                      "online", "not machine-checked"):
            self.assertIn(words, design)
        self.assertIn("## Not verified", text)


if __name__ == "__main__":
    unittest.main()
