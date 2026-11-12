"""The code tools (plan P-0001 step 219; FIX-31, decision D-0015): the ast-grep rule packs shipped in
references/rules/ast-grep/ (decision placement, time, resilience, server entry points, client calls, logging), each
rule with a message and a test; scc for size and complexity; jscpd for copies; and the built-in git-history analysis
(churn, files that change together, code one person knows). Each imports from a recorded output, so none needs the
tool installed; the rule packs' own tests run when ast-grep is installed."""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._w7a_tools_helpers import DATA, env_with, leads, ok, shim, tool_project

PACKS = fixtures.REPO_ROOT / "references" / "rules" / "ast-grep"


def rule_files():
    return sorted((PACKS / "rules").glob("*/*.yml"))


def field(text, name):
    m = re.search(rf"(?m)^{name}:\s*(.+)$", text)
    return m.group(1).strip() if m else None


class RulePacks(unittest.TestCase):
    def test_every_rule_has_a_message_a_domain_and_a_test(self):
        mod = fixtures.load_ledger_module()
        con = mod.connect(fixtures.Ledger(fixtures.temp_dir("w7a-dom-")).db)
        try:
            domains = {r[0] for r in con.execute("SELECT code FROM domains")}
        finally:
            con.close()
        files = rule_files()
        self.assertGreaterEqual(len(files), 12)
        packs = {f.parent.name for f in files}
        self.assertEqual(packs, {"decision-placement", "time-concurrency", "resilience", "server", "client-api",
                                 "logging"})
        ids = set()
        for f in files:
            text = f.read_text(encoding="utf-8")
            with self.subTest(rule=f.name):
                rid = field(text, "id")
                self.assertEqual(rid, f"{f.parent.name}.{f.stem}")
                self.assertIn(f.parent.name, domains)
                self.assertTrue(field(text, "message"))
                self.assertIn(field(text, "severity"), ("warning", "hint", "info"))
                self.assertNotIn(rid, ids)
                ids.add(rid)
                test = PACKS / "tests" / f"{rid}-test.yml"
                self.assertTrue(test.is_file(), f"no test file {test.name}")
                t = test.read_text(encoding="utf-8")
                self.assertEqual(field(t, "id"), rid)
                self.assertRegex(t, r"(?m)^valid:\n  - ")
                self.assertRegex(t, r"(?m)^invalid:\n  - ")

    def test_rule_packs_match_their_samples(self):
        """Each rule matches its invalid samples and none of its valid ones (ast-grep's own test runner)."""
        exe = shutil.which("ast-grep") or os.environ.get("LEDGER_AST_GREP")
        if not exe:
            self.skipTest("ast-grep is not installed (pip install ast-grep-cli)")
        p = subprocess.run([exe, "test", "-c", str(PACKS / "sgconfig.yml"), "--skip-snapshot-tests"],
                           capture_output=True, text=True, timeout=120, cwd=str(PACKS))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"{len(rule_files())} passed", p.stdout)

    def test_ast_grep_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "ast-grep", "--review", "R-0001", "--from", str(DATA / "ast-grep.jsonl"), "--actor",
           "A-0001")
        L.commit()
        got = {(r["rule"], r["path"], r["line_start"], r["domain_code"], r["level"])
               for r in L.all("SELECT * FROM tool_results")}
        self.assertIn(("time-concurrency.python-naive-now", "src/app.py", 14, "time-concurrency", "warning"), got)
        self.assertIn(("resilience.python-requests-no-timeout", "src/app.py", 10, "resilience", "warning"), got)
        self.assertIn(("client-api.js-fetch", "src/util.js", 2, "client-api", "note"), got)
        self.assertEqual(len(got), 7)

    def test_ast_grep_runs_the_shipped_packs(self):
        repo, L, sha = tool_project(self)
        script = ("import sys\nif '--version' in sys.argv:\n    print('ast-grep 0.51.2'); sys.exit(0)\n"
                  "a = sys.argv\nassert a[1] == 'scan' and a[a.index('--config') + 1].endswith('sgconfig.yml'), a\n"
                  "assert '--json=stream' in a and a[-1] == '.', a\n"
                  f"sys.stdout.write(open({str(DATA / 'ast-grep.jsonl')!r}).read())\n")
        out = ok(L, "tool-run", "ast-grep", "--review", "R-0001", "--actor", "A-0001",
                 env=env_with(shim("ast-grep", script)))
        self.assertIn("7 leads", out[0])
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["version"], run["pinned_version"]), ("ran", "0.51.2", "0.51.2"))
        self.assertIn("--config {rules}", run["command"])
        self.assertEqual(len(run["rules_sha256"]), 64)


class SizeAndCopies(unittest.TestCase):
    def test_scc_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "scc", "--review", "R-0001", "--from", str(DATA / "scc.json"), "--actor", "A-0001")
        L.commit()
        m = {(r["name"], r["scope"]): r["value"] for r in L.all("SELECT name, scope, value FROM metrics")}
        self.assertEqual(m[("code_lines", "all")], 37 + 26 + 1 + 1 + 8)
        self.assertEqual(m[("complexity", "all")], 13)
        self.assertEqual(m[("code_files", "all")], 7)
        self.assertEqual(m[("complexity", "src/util.js")], 8)
        self.assertEqual([(c, r, p) for c, r, p, _, _ in leads(L)],
                         [("LD-0001", "scc.complex-file", "src/app.py"), ("LD-0002", "scc.complex-file", "src/util.js"),
                          ("LD-0003", "scc.complex-file", "tests/test_app.py")])
        self.assertEqual(L.scalar("SELECT domain_code FROM tool_results WHERE code='LD-0001'"), "abstraction")

    def test_scc_runs_on_the_export(self):
        repo, L, sha = tool_project(self)
        script = ("import json, os, sys\nif '--version' in sys.argv:\n    print('scc version 3.6.0'); sys.exit(0)\n"
                  "assert sys.argv[1:] == ['--format', 'json', '--by-file', '--no-cocomo', '.'], sys.argv\n"
                  "assert sorted(os.listdir('src')) == ['app.py', 'util.js'], os.listdir('src')\n"
                  "assert not os.path.exists('.git') and not os.path.exists('.code-review')\n"
                  f"sys.stdout.write(open({str(DATA / 'scc.json')!r}).read())\n")
        out = ok(L, "tool-run", "scc", "--review", "R-0001", "--actor", "A-0001", env=env_with(shim("scc", script)))
        self.assertIn("scc 3.6.0", out[0])

    def test_jscpd_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "jscpd", "--review", "R-0001", "--from", str(DATA / "jscpd.json"), "--actor", "A-0001")
        L.commit()
        r = L.one("SELECT * FROM tool_results")
        self.assertEqual((r["rule"], r["path"], r["line_start"], r["line_end"], r["domain_code"]),
                         ("jscpd.clone", "src/util.js", 11, 19, "abstraction"))
        self.assertIn("src/util.js:21-29", r["message"])
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics WHERE scope='all'")}
        self.assertEqual((m["duplicated_lines"], m["clones"]), (9, 1))
        self.assertAlmostEqual(m["duplicated_percent"], 10.23, places=2)


class History(unittest.TestCase):
    def test_git_history_metrics(self):
        """Churn, files that change together while living apart, and code only one author has touched."""
        repo = fixtures.ScratchRepo()
        for n in range(6):
            repo.write("core/rules.py", f"rules = {n}\n")
            repo.write("web/rules_view.js", f"// view {n}\n")
            if n < 2:
                repo.write("docs/guide.md", f"guide {n}\n")
            repo.commit(f"change {n}")
        for n in range(3):
            repo.write("core/rules.py", f"rules = {n + 10}\n")
            repo.git("add", "-A")
            repo.git("commit", "-q", "--author", "Ann Other <ann@example.com>", "-m", f"ann {n}")
        repo.write("gone.txt", "x\n")
        repo.commit("add a file")
        repo.delete("gone.txt")
        repo.commit("remove it")
        from dev.tests._p5_writes_helpers import new_ledger
        from dev.tests._w7a_tools_helpers import skill_ledger_path
        L = new_ledger(repo)
        self.addCleanup(L.close)
        L.ledger_py = skill_ledger_path()
        ok(L, "add-repo", "demo", ".")
        ok(L, "add-actor", "manager", "review manager")
        ok(L, "new-review", "--title", "History", "--scope", "full", "--actor", "A-0001")
        out = ok(L, "tool-run", "git-history", "--review", "R-0001", "--actor", "A-0001", env=env_with(bare=True))
        self.assertIn("git-history", out[0])
        L.commit()
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["output_format"], run["version"]), ("ran", "json", None))
        m = {(r["name"], r["scope"]): r["value"] for r in L.all("SELECT name, scope, value FROM metrics")}
        self.assertEqual(m[("history_commits", "all")], 11)
        self.assertEqual(m[("history_authors", "all")], 2)
        self.assertEqual(m[("file_commits", "core/rules.py")], 9)
        self.assertEqual(m[("file_authors", "core/rules.py")], 2)
        self.assertNotIn(("file_commits", "gone.txt"), m)
        got = {(r["rule"], r["path"], r["domain_code"]): r["message"] for r in L.all("SELECT * FROM tool_results")}
        self.assertIn(("git-history.hotspot", "core/rules.py", "hotspots"), got)
        self.assertIn("9 commits", got[("git-history.hotspot", "core/rules.py", "hotspots")])
        self.assertIn(("git-history.co-change", "core/rules.py", "hotspots"), got)
        self.assertIn("web/rules_view.js", got[("git-history.co-change", "core/rules.py", "hotspots")])
        self.assertIn(("git-history.single-author", "web/rules_view.js", "handover"), got)
        self.assertNotIn(("git-history.single-author", "core/rules.py", "handover"), got)
        self.assertNotIn(("git-history.hotspot", "docs/guide.md", "hotspots"), got)
        # the same commit gives the same output bytes
        ok(L, "tool-run", "git-history", "--review", "R-0001", "--actor", "A-0001")
        L.commit()
        a, b = [r["output_sha256"] for r in L.all("SELECT output_sha256 FROM tool_runs ORDER BY id")]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
