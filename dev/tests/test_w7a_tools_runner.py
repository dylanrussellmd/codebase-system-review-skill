"""The tool runner (plan P-0001 step 218; FIX-31, decision D-0015): `tool-run` runs a picked tool on an export of the
reviewed commit and records tool, version, pinned version, command, exit code, duration and the output's sha256;
`--from` imports a file a tool wrote (SARIF 2.1.0, JUnit XML, LCOV, Cobertura). Every result becomes a lead (LD-0001),
never a finding. v_unaddressed_tool_results lists the leads no finding cites and no reviewer dismissed, and `check`
fails while it has rows. A tool that is not installed is recorded as absent with its fallback, and that is not a
failure. The samples are recorded outputs, so nothing here needs the tools installed."""
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import refused
from dev.tests._w7a_tools_helpers import DATA, env_with, leads, ok, shim, tool_project

SEMGREP_SHIM = """import shutil, sys
if "--version" in sys.argv:
    print("1.185.0"); sys.exit(0)
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--sarif-output="))
assert sys.argv[-1] == ".", sys.argv
shutil.copy(%r, out)
""" % str(DATA / "semgrep.sarif")


class Runner(unittest.TestCase):
    def test_tool_run_records_version_and_hash(self):
        repo, L, sha = tool_project(self)
        rules = fixtures.temp_dir("w7a-rules-") / "rules.yml"
        rules.write_text("rules: []\n")
        env = env_with(shim("semgrep", SEMGREP_SHIM))
        out = ok(L, "tool-run", "semgrep", "--review", "R-0001", "--domain", "resilience", "--rules", str(rules),
                 "--actor", "A-0001", env=env)
        self.assertIn("semgrep 1.185.0", out[0])
        self.assertIn("1 lead", " ".join(out))
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["tool"], run["status"], run["version"], run["pinned_version"], run["exit_code"]),
                         ("semgrep", "ran", "1.185.0", "1.185.0", 0))
        self.assertEqual(run["commit_sha"], sha)
        self.assertEqual(run["output_format"], "sarif")
        self.assertIn("--sarif-output={out}", run["command"])
        self.assertNotIn(str(L.root), run["command"])
        self.assertGreaterEqual(run["duration_ms"], 0)
        stored = L.root / run["output_path"]
        self.assertTrue(stored.is_file(), run["output_path"])
        self.assertTrue(run["output_path"].startswith(".code-review/reviews/R-0001/tools/"))
        self.assertEqual(run["output_sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
        self.assertEqual(run["rules_sha256"], hashlib.sha256(rules.read_bytes()).hexdigest())
        self.assertEqual(leads(L), [("LD-0001", "requests-without-timeout", "src/app.py", 10, "warning")])
        self.assertEqual(L.scalar("SELECT domain_code FROM tool_results"), "resilience")
        self.assertEqual(L.count("findings"), 0, "a lead is never a finding")

    def test_rerun_gives_the_same_rows(self):
        """Same commit, same version, same output: the same leads (codes aside), in the same order."""
        repo, L, sha = tool_project(self)
        for _ in range(2):
            ok(L, "tool-run", "sarif", "--review", "R-0001", "--domain", "security", "--from",
               str(DATA / "zizmor.sarif"), "--actor", "A-0001")
        L.commit()
        a, b = (L.all("""SELECT rule, level, path, line_start, line_end, message, fingerprint FROM tool_results
                         WHERE tool_run_id=? ORDER BY code""", (i,)) for i in (1, 2))
        self.assertTrue(a)
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])
        self.assertEqual(len({r["fingerprint"] for r in a}), len(a))

    def test_sarif_import_creates_leads(self):
        repo, L, sha = tool_project(self)
        out = ok(L, "tool-run", "sarif", "--review", "R-0001", "--domain", "resilience", "--from",
                 str(DATA / "semgrep.sarif"), "--actor", "A-0001")
        self.assertIn("imported", out[0])
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["output_format"], run["version"]), ("imported", "sarif", "1.185.0"))
        self.assertEqual(leads(L), [("LD-0001", "requests-without-timeout", "src/app.py", 10, "warning")])
        msg = L.scalar("SELECT message FROM tool_results")
        self.assertEqual(msg, "requests call without a timeout")
        # a SARIF file from another tool keeps that tool's rule ids and levels
        ok(L, "tool-run", "sarif", "--review", "R-0001", "--domain", "operations", "--from",
           str(DATA / "zizmor.sarif"), "--actor", "A-0001")
        got = L.all("SELECT rule, path FROM tool_results WHERE tool_run_id=2 ORDER BY code")
        self.assertTrue(got)
        self.assertTrue(all(r["path"] == ".github/workflows/ci.yml" for r in got), got)

    def test_junit_lcov_cobertura_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "junit", "--review", "R-0001", "--domain", "tests", "--from", str(DATA / "junit.xml"),
           "--actor", "A-0001")
        self.assertEqual(leads(L), [("LD-0001", "junit.failure", "tests/test_app.py", 15, "error")])
        self.assertIn("tests.test_app.AreaTest.test_fails", L.scalar("SELECT message FROM tool_results"))
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics WHERE scope='all'")}
        self.assertEqual((m["tests_passed"], m["tests_failed"]), (2, 1))
        ok(L, "tool-run", "lcov", "--review", "R-0001", "--domain", "tests", "--from", str(DATA / "coverage.lcov"),
           "--actor", "A-0001")
        line = L.scalar("SELECT value FROM metrics WHERE name='line_coverage' AND scope='src/app.py'")
        self.assertAlmostEqual(line, 62.5)
        ok(L, "tool-run", "cobertura", "--review", "R-0001", "--domain", "tests", "--from",
           str(DATA / "coverage.xml"), "--actor", "A-0001")
        self.assertEqual(L.scalar("SELECT output_format FROM tool_runs WHERE id=3"), "cobertura")
        self.assertAlmostEqual(L.scalar("""SELECT value FROM metrics WHERE name='line_coverage' AND scope='all'"""),
                               74.29, places=2)
        # LCOV and Cobertura of the same run agree on the file
        self.assertEqual(L.count("metrics", "name='line_coverage' AND scope='src/app.py'"), 1)

    def test_a_bad_file_is_refused_and_nothing_written(self):
        repo, L, sha = tool_project(self)
        bad = fixtures.temp_dir("w7a-bad-") / "bad.sarif"
        bad.write_text("{not json")
        line = refused(self, L, "tool-run", "sarif", "--review", "R-0001", "--from", str(bad), "--actor", "A-0001")
        self.assertIn("SARIF", line)
        self.assertEqual(L.count("tool_runs"), 0)
        self.assertIn("not one of", refused(self, L, "tool-run", "nosuchtool", "--review", "R-0001",
                                            "--actor", "A-0001"))

    def test_tool_absent_recorded(self):
        repo, L, sha = tool_project(self)
        p = L.run("tool-run", "semgrep", "--review", "R-0001", "--actor", "A-0001", env=env_with(bare=True))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("absent", p.stdout)
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["version"]), ("absent", None))
        self.assertTrue(run["fallback"].strip())
        self.assertEqual(run["pinned_version"], "1.185.0")
        self.assertEqual(L.count("tool_results"), 0)
        p = L.run("check")
        self.assertNotIn("v_unaddressed_tool_results", p.stdout)

    def test_env_reports_pinned_and_installed(self):
        repo, L, sha = tool_project(self)
        env = env_with(shim("semgrep", SEMGREP_SHIM), bare=True)
        out = json.loads(L.run_ok("env", "--json", env=env).stdout)
        tools = {t["tool"]: t for t in out["tools"]}
        self.assertEqual((tools["semgrep"]["installed"], tools["semgrep"]["pinned"]), ("1.185.0", "1.185.0"))
        self.assertEqual(list(tools), sorted(tools))
        text = L.run_ok("env", env=env).stdout
        self.assertRegex(text, r"(?m)^yes\s+tool:semgrep\s+1\.185\.0 \(pinned 1\.185\.0\)")
        bare = env_with(bare=True)
        tools = {t["tool"]: t for t in json.loads(L.run_ok("env", "--json", env=bare).stdout)["tools"]}
        self.assertIsNone(tools["semgrep"]["installed"])
        self.assertTrue(tools["semgrep"]["how_to_install"].strip())
        self.assertRegex(L.run_ok("env", env=bare).stdout, r"(?m)^no\s+tool:semgrep\s+not installed \(pinned ")


    def test_lock_file_pins_every_adapter(self):
        mod = fixtures.load_ledger_module()
        pins = mod.read_tools_lock(fixtures.REPO_ROOT / "references" / "tools.lock")
        for name, adapter in mod.TOOL_ADAPTERS.items():
            if adapter.binary:
                with self.subTest(tool=name):
                    self.assertIn(name, pins)
                    self.assertRegex(pins[name], r"^\d+(\.\d+)+$")


class Leads(unittest.TestCase):
    def test_unaddressed_leads_check(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "sarif", "--review", "R-0001", "--domain", "security", "--from",
           str(DATA / "zizmor.sarif"), "--actor", "A-0001")
        n = L.count("tool_results")
        self.assertGreaterEqual(n, 2)
        p = L.run("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("v_unaddressed_tool_results", p.stdout)
        self.assertEqual(L.count("v_unaddressed_tool_results"), n)
        self.assertIn("give --reason", refused(self, L, "dismiss-lead", "LD-0001", "--actor", "A-0001"))
        ok(L, "dismiss-lead", "LD-0001", "--reason", "the trigger runs on our own pushes only", "--actor", "A-0001")
        self.assertEqual(L.count("v_unaddressed_tool_results"), n - 1)
        self.assertIn("already", refused(self, L, "dismiss-lead", "LD-0001", "--reason", "again", "--actor", "A-0001"))
        # a finding citing the lead's lines addresses it (no explicit link needed) ...
        L.commit()
        first = L.one("SELECT * FROM tool_results WHERE code='LD-0002'")
        rid = L.scalar("SELECT id FROM reviews WHERE code='R-0001'")
        fid = L.finding(rid, "OPS-M1", domain_code="spec")
        L.location(fid, path=first["path"], line_start=first["line_start"], commit_sha=sha)
        L.commit()
        addressed = L.count("v_unaddressed_tool_results")
        self.assertLess(addressed, n - 1)
        # ... and cite-lead links any other one to a finding of the same review
        rest = [r["lead"] for r in L.all("SELECT lead FROM v_unaddressed_tool_results ORDER BY lead")]
        for code in rest:
            ok(L, "cite-lead", code, "R-0001:OPS-M1", "--actor", "A-0001")
        self.assertEqual(L.count("v_unaddressed_tool_results"), 0)
        self.assertNotIn("v_unaddressed_tool_results", L.run("check").stdout)
        L.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            L.x("UPDATE tool_results SET message='changed' WHERE code='LD-0001'")
        with self.assertRaises(sqlite3.DatabaseError):
            L.x("DELETE FROM tool_runs")

    def test_dismissal_carries_to_the_next_review(self):
        """The same tool, rule and fingerprint in a later review keeps its dismissal, saying where it came from."""
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "sarif", "--review", "R-0001", "--from", str(DATA / "semgrep.sarif"), "--actor", "A-0001")
        ok(L, "dismiss-lead", "LD-0001", "--reason", "fetch is only called in tests", "--actor", "A-0001")
        ok(L, "review-status", "R-0001", "handed-off", "--note", "done", "--actor", "A-0001")
        ok(L, "new-review", "--title", "Second", "--scope", "full", "--previous", "R-0001", "--actor", "A-0001")
        ok(L, "tool-run", "sarif", "--review", "R-0002", "--from", str(DATA / "semgrep.sarif"), "--actor", "A-0001")
        L.commit()
        r = L.one("SELECT * FROM tool_results WHERE code='LD-0002'")
        self.assertIn("fetch is only called in tests", r["dismissed_reason"])
        self.assertIn("LD-0001", r["dismissed_reason"])
        self.assertEqual(r["carried_from_id"], L.scalar("SELECT id FROM tool_results WHERE code='LD-0001'"))
        self.assertEqual(L.count("v_unaddressed_tool_results"), 0)

    def test_lint_report_wants_the_domains_leads(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "sarif", "--review", "R-0001", "--domain", "spec", "--from", str(DATA / "semgrep.sarif"),
           "--actor", "A-0001")
        mod = L.module
        con = mod.connect(L.db)
        try:
            rv = mod.review_row(con, "R-0001")
            problems = mod.lint_report(con, L.root, "# Spec\n", "spec", rv)[0]
            self.assertTrue(any("LD-0001" in msg for _, msg in problems), problems)
            text = "# Spec\n\n## Leads\n\n| Lead | Disposition | Why |\n|---|---|---|\n" \
                   "| LD-0001 | dismissed | the URL is a constant |\n"
            problems = mod.lint_report(con, L.root, text, "spec", rv)[0]
            self.assertFalse(any("LD-0001" in msg for _, msg in problems), problems)
            other = mod.lint_report(con, L.root, "# Tests\n", "tests", rv)[0]
            self.assertFalse(any("LD-0001" in msg for _, msg in other), other)
        finally:
            con.close()

    def test_schema_upgrade_from_19(self):
        """A ledger of the previous schema gains the two tables, the check and the labels on migrate."""
        from dev.tests import _migrate_helpers as M
        project_dir, db = M.restore_v1()
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0020-tool-runs", p.stdout)
        con = sqlite3.connect(str(db))
        try:
            self.assertEqual(con.execute("SELECT tier FROM ledger_checks WHERE view='v_unaddressed_tool_results'")
                             .fetchone(), ("error",))
            self.assertEqual(con.execute("SELECT count(*) FROM v_unaddressed_tool_results").fetchone(), (0,))
            self.assertEqual(con.execute("SELECT next_code FROM v_next_codes WHERE prefix='LD'").fetchone(),
                             ("LD-0001",))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
