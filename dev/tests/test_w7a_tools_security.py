"""The security and operations tools (plan P-0001 step 220; FIX-31, decision D-0015): Semgrep CE with a rule set
fetched once (only with --online) and read offline after; gitleaks over the whole history up to the reviewed
commit, with every secret value left out of the ledger and the kept output; OSV-Scanner against the lockfiles,
offline, reading advisory databases downloaded only with --online; actionlint and zizmor when the commit has GitHub
Actions workflows. Each imports from a recorded output, so none needs the tool installed."""
import json
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._w7a_tools_helpers import APP_PY, DATA, env_with, leads, ok, shim, tool_project

VERSION = "import sys\nif '--version' in sys.argv or 'version' in sys.argv[1:2] or '-version' in sys.argv:\n" \
          "    print(%r); sys.exit(0)\n"
WORKFLOW = "on: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


class Semgrep(unittest.TestCase):
    def test_rules_fetched_once_then_offline(self):
        repo, L, sha = tool_project(self)
        rules = fixtures.temp_dir("w7a-reg-") / "registry.yml"
        rules.write_text("rules:\n  - id: x\n    languages: [python]\n    severity: INFO\n    message: m\n"
                         "    pattern: print(...)\n")
        script = VERSION % "1.185.0" + (
            "import shutil\nout = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--sarif-output='))\n"
            f"shutil.copy({str(DATA / 'semgrep.sarif')!r}, out)\n")
        env = env_with(shim("semgrep", script))
        env["LEDGER_SEMGREP_RULES_URL"] = rules.as_uri()
        out = ok(L, "tool-run", "semgrep", "--review", "R-0001", "--actor", "A-0001", env=env)
        self.assertIn("not run", out[0])
        self.assertIn("--online", out[0])
        self.assertEqual(L.scalar("SELECT status FROM tool_runs"), "skipped")
        ok(L, "tool-run", "semgrep", "--review", "R-0001", "--online", "--actor", "A-0001", env=env)
        cached = L.dir / "tools" / "semgrep-rules.yml"
        self.assertEqual(cached.read_bytes(), rules.read_bytes())
        run = L.one("SELECT * FROM tool_runs WHERE id=2")
        self.assertEqual((run["status"], run["online"]), ("ran", 1))
        rules.write_text("rules: []\n")  # the registry changed; the cached copy is what runs
        ok(L, "tool-run", "semgrep", "--review", "R-0001", "--actor", "A-0001", env=env)
        L.commit()
        a, b = L.all("SELECT rules_sha256, online FROM tool_runs WHERE id IN (2, 3) ORDER BY id")
        self.assertEqual(a["rules_sha256"], b["rules_sha256"])
        self.assertEqual(b["online"], 0)


class Gitleaks(unittest.TestCase):
    def test_gitleaks_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "gitleaks", "--review", "R-0001", "--from", str(DATA / "gitleaks.json"), "--actor",
           "A-0001")
        self.assertEqual([(r, p, n, lv) for _, r, p, n, lv in leads(L)],
                         [("aws-access-token", "config.py", 1, "error"), ("github-pat", "config.py", 2, "error")])
        msg = L.scalar("SELECT message FROM tool_results WHERE rule='github-pat'")
        self.assertIn("948b260630", msg)
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "security")

    def test_gitleaks_values_redacted(self):
        """No secret value reaches the ledger, its dump or the kept output, even from an unredacted report."""
        repo, L, sha = tool_project(self)
        value = "tok" + "".join(chr(ord("a") + (n * 7) % 26) for n in range(30)) + "9Q"
        doc = json.loads((DATA / "gitleaks.json").read_text())
        for f in doc:
            f["Secret"] = value
            f["Match"] = f"TOKEN = \"{value}\""
            f["Line"] = f"TOKEN = \"{value}\""
        raw = fixtures.temp_dir("w7a-leak-") / "gitleaks.json"
        raw.write_text(json.dumps(doc))
        ok(L, "tool-run", "gitleaks", "--review", "R-0001", "--from", str(raw), "--actor", "A-0001")
        L.run_ok("dump")
        L.commit()
        kept = L.root / L.scalar("SELECT output_path FROM tool_runs")
        for where, data in (("kept output", kept.read_bytes()), ("ledger.sql", (L.dir / "ledger.sql").read_bytes()),
                            ("ledger.sqlite", L.db.read_bytes())):
            with self.subTest(where=where):
                self.assertNotIn(value.encode(), data)
        self.assertIn(b"REDACTED", kept.read_bytes())
        self.assertEqual(L.count("tool_results"), 2)

    def test_gitleaks_reads_the_history_up_to_the_commit(self):
        repo, L, sha = tool_project(self)
        script = VERSION % "8.28.0" + (
            "import os, shutil\na = sys.argv\n"
            "assert a[1] == 'git' and '--redact' in a and a[-1] == '.', a\n"
            f"assert '--log-opts={sha}' in a, a\n"
            "assert os.path.isdir('.git'), os.getcwd()\n"
            f"shutil.copy({str(DATA / 'gitleaks.json')!r}, a[a.index('--report-path') + 1])\nsys.exit(1)\n")
        out = ok(L, "tool-run", "gitleaks", "--review", "R-0001", "--actor", "A-0001",
                 env=env_with(shim("gitleaks", script)))
        self.assertIn("exit 1, 2 leads", out[0])
        self.assertEqual(L.scalar("SELECT status FROM tool_runs"), "ran")


class Dependencies(unittest.TestCase):
    def test_osv_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "osv-scanner", "--review", "R-0001", "--from", str(DATA / "osv-documented.json"),
           "--actor", "A-0001")
        got = [(r, p, lv) for _, r, p, _, lv in leads(L)]
        self.assertEqual(got, [("GHSA-j8r2-6x86-q33q", "requirements.txt", "warning"),
                               ("GHSA-x84v-xcm2-53pg", "requirements.txt", "error")])
        msg = L.scalar("SELECT message FROM tool_results WHERE rule='GHSA-x84v-xcm2-53pg'")
        self.assertIn("requests 2.19.0", msg)
        self.assertIn("CVE-2018-18074", msg)
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics WHERE scope='all'")}
        self.assertEqual((m["vulnerable_packages"], m["vulnerabilities"]), (1, 2))
        ok(L, "tool-run", "osv-scanner", "--review", "R-0001", "--from", str(DATA / "osv-no-database.json"),
           "--actor", "A-0001")
        self.assertEqual(L.count("tool_results", "tool_run_id=2"), 0)

    def test_osv_offline_without_databases_is_skipped(self):
        repo, L, sha = tool_project(self, files={"src/app.py": APP_PY, "requirements.txt": "requests==2.19.0\n"})
        script = VERSION % "osv-scanner version: 2.2.4" + "a = sys.argv\nassert '--offline' in a, a\nsys.exit(3)\n"
        env = env_with(shim("osv-scanner", script))
        out = ok(L, "tool-run", "osv-scanner", "--review", "R-0001", "--actor", "A-0001", env=env)
        self.assertIn("not run", out[0])
        self.assertIn("--online", L.scalar("SELECT fallback FROM tool_runs"))
        # with the databases cached, it runs offline against the lockfile
        (L.dir / "tools" / "osv-db" / "osv-scanner" / "PyPI").mkdir(parents=True)
        (L.dir / "tools" / "osv-db" / "osv-scanner" / "PyPI" / "all.zip").write_bytes(b"zip")
        script = VERSION % "osv-scanner version: 2.2.4" + (
            "import os, shutil\na = sys.argv\nassert '--offline' in a and '--offline-vulnerabilities' in a, a\n"
            "assert os.environ['OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY'].endswith('osv-db')\n"
            f"shutil.copy({str(DATA / 'osv-documented.json')!r}, a[a.index('--output') + 1])\nsys.exit(1)\n")
        out = ok(L, "tool-run", "osv-scanner", "--review", "R-0001", "--actor", "A-0001",
                 env=env_with(shim("osv-scanner", script)))
        self.assertIn("2 leads", out[0])

    def test_osv_without_lockfiles_is_skipped(self):
        repo, L, sha = tool_project(self)
        script = VERSION % "osv-scanner version: 2.2.4" + "sys.exit(9)\n"
        out = ok(L, "tool-run", "osv-scanner", "--review", "R-0001", "--online", "--actor", "A-0001",
                 env=env_with(shim("osv-scanner", script)))
        self.assertIn("no lockfile", out[0])


class Workflows(unittest.TestCase):
    def test_actionlint_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "actionlint", "--review", "R-0001", "--from", str(DATA / "actionlint.json"), "--actor",
           "A-0001")
        self.assertEqual(leads(L), [("LD-0001", "actionlint.expression", ".github/workflows/ci.yml", 7, "error")])
        self.assertEqual(L.scalar("SELECT domain_code FROM tool_results"), "operations")

    def test_zizmor_import(self):
        repo, L, sha = tool_project(self)
        ok(L, "tool-run", "zizmor", "--review", "R-0001", "--from", str(DATA / "zizmor.sarif"), "--actor", "A-0001")
        got = {r for _, r, _, _, _ in leads(L)}
        self.assertIn("zizmor/template-injection", got)
        self.assertEqual(L.scalar("SELECT version FROM tool_runs"), "1.32.0")
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "operations")

    def test_workflow_tools_need_workflows(self):
        repo, L, sha = tool_project(self)
        for tool, version in (("actionlint", "1.7.8"), ("zizmor", "zizmor 1.32.0")):
            env = env_with(shim(tool, VERSION % version + "sys.exit(7)\n"))
            out = ok(L, "tool-run", tool, "--review", "R-0001", "--actor", "A-0001", env=env)
            self.assertIn("no GitHub Actions workflows", out[0])

    def test_workflow_tools_run_offline(self):
        repo, L, sha = tool_project(self, files={"src/app.py": APP_PY, ".github/workflows/ci.yml": WORKFLOW})
        script = VERSION % "zizmor 1.32.0" + (
            "a = sys.argv\nassert '--offline' in a and '--format' in a and a[-1] == '.', a\n"
            f"sys.stdout.write(open({str(DATA / 'zizmor.sarif')!r}).read())\n")
        out = ok(L, "tool-run", "zizmor", "--review", "R-0001", "--actor", "A-0001",
                 env=env_with(shim("zizmor", script)))
        self.assertIn("5 leads", out[0])
        script = VERSION % "1.7.8" + (
            "a = sys.argv\nassert a[1:3] == ['-format', '{{json .}}'] and '-shellcheck=' in a, a\n"
            f"sys.stdout.write(open({str(DATA / 'actionlint.json')!r}).read())\nsys.exit(1)\n")
        out = ok(L, "tool-run", "actionlint", "--review", "R-0001", "--actor", "A-0001",
                 env=env_with(shim("actionlint", script)))
        self.assertIn("1 lead", out[0])


if __name__ == "__main__":
    unittest.main()
