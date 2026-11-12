"""The documentation tools (plan P-0001 step 222; FIX-31, decision D-0015): Vale with the writing standard's rules
shipped in references/vale/, lychee offline for local links and anchors, and two scans built into ledger.py: every
path, script and environment variable a document names exists at the commit (doc-references), and no table,
column, enumeration value or route name is shown to users (internal-names). The Vale and lychee samples are real
outputs; the built-in scans run here."""
import json
import shutil
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._w7a_tools_helpers import DATA, env_with, leads, ok, shared_project, shim, tool_project

VALE = fixtures.REPO_ROOT / "references" / "vale"
SAMPLES = DATA / "vale-samples"


def sample_files():
    return {p.relative_to(SAMPLES).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(SAMPLES.rglob("*")) if p.is_file()}


class Vale(unittest.TestCase):
    def test_vale_rules_match_samples(self):
        """Every shipped rule raises its alert on the sample text (Vale 3.26.0 run on vale-samples/), and the import
        gives one lead per alert, at its file and line."""
        doc = json.loads((DATA / "vale.json").read_text(encoding="utf-8"))
        raised = {a["Check"] for alerts in doc.values() for a in alerts}
        shipped = {f"{p.parent.name}.{p.stem}" for p in (VALE / "styles").glob("*/*.yml")}
        self.assertGreaterEqual(len(shipped), 20)
        self.assertEqual(shipped - raised, set(), "rules that raised nothing on the samples")
        repo, L, sha = tool_project(self, files=sample_files())
        out = ok(L, "tool-run", "vale", "--review", "R-0001", "--from", str(DATA / "vale.json"), "--actor", "A-0001")
        self.assertIn("27 leads", out[0])
        got = leads(L)
        self.assertIn(("Plain.History", "docs/guide.md", 3, "error"), [g[1:] for g in got])
        self.assertIn(("Plain.LabelLead", "docs/guide.md", 7, "warning"), [g[1:] for g in got])
        self.assertIn(("Plain.CommentRestates", "src/notes.py", 3, "warning"), [g[1:] for g in got])
        L.commit()
        self.assertEqual(L.scalar("SELECT domain_code FROM tool_results WHERE rule='PlainUI.Exclaim'"), "ui")
        self.assertEqual(L.scalar("SELECT domain_code FROM tool_results WHERE rule='Plain.Wordy'"), "docs-user")
        self.assertEqual(L.scalar("SELECT version FROM tool_runs"), None)

    def test_vale_runs_the_shipped_rules_offline(self):
        """tool-run vale runs a copy of references/vale/ without its Packages line (vale sync would go online)."""
        script = ("import sys, pathlib\nif '--version' in sys.argv or '-v' in sys.argv:\n    print('vale version 3.26.0')"
                  "; sys.exit(0)\ncfg = pathlib.Path(next(a.split('=', 1)[1] for a in sys.argv if "
                  "a.startswith('--config=')))\ntext = cfg.read_text()\nassert not __import__('re').search('(?m)^Packages', text), text\n"
                  "assert (cfg.parent / 'styles' / 'Plain' / 'History.yml').is_file()\n"
                  "assert '--no-exit' in sys.argv and sys.argv[-1] == '.', sys.argv\n"
                  f"sys.stdout.write(open({str(DATA / 'vale.json')!r}).read())\n")
        repo, L, sha = tool_project(self, files=sample_files())
        out = ok(L, "tool-run", "vale", "--review", "R-0001", "--actor", "A-0001", env=env_with(shim("vale", script)))
        self.assertIn("vale 3.26.0", out[0])
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["version"], run["pinned_version"]), ("ran", "3.26.0", "3.26.0"))
        self.assertIn("--config={rules}/.vale.ini", run["command"])
        self.assertTrue(run["rules_sha256"])
        self.assertEqual(L.count("tool_results"), 27)

    @unittest.skipUnless(shutil.which("vale"), "Vale is not installed")
    def test_vale_matches_the_recording(self):
        """With Vale installed, the shipped rules give the recorded alerts on the samples again."""
        repo, L, sha = tool_project(self, files=sample_files())
        ok(L, "tool-run", "vale", "--review", "R-0001", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "vale", "--review", "R-0001", "--from", str(DATA / "vale.json"), "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message FROM tool_results WHERE tool_run_id=? ORDER BY code",
                      (i,)) for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])


LYCHEE_FILES = {
    "README.md": "# Demo\n\nSee [the guide](docs/guide.md), [setup](docs/setup.md) and [the API](docs/guide.md#api).\n"
                 "Also [the cache section](#cache) and [the site](https://example.com/).\n\n## Usage\n",
    "docs/guide.md": "# Guide\n\n## Install\n\nBack to [the readme](../README.md#usage).\n",
}


class Lychee(unittest.TestCase):
    def test_lychee_import(self):
        """lychee's JSON (offline, with fragments): one lead per broken local link or anchor, in the file that has
        it; links it did not check (offline) are counted, not leads."""
        repo, L, sha = tool_project(self, files=LYCHEE_FILES)
        out = ok(L, "tool-run", "lychee", "--review", "R-0001", "--from", str(DATA / "lychee.json"),
                 "--actor", "A-0001")
        got = [g[1:] for g in leads(L)]
        self.assertEqual(sorted({g[0] for g in got}), ["lychee.broken-anchor", "lychee.broken-link"], out)
        self.assertTrue(all(g[1] == "README.md" for g in got), got)
        L.commit()
        msgs = [r["message"] for r in L.all("SELECT message FROM tool_results ORDER BY code")]
        self.assertTrue(any("docs/setup.md" in m for m in msgs), msgs)
        self.assertTrue(any("#api" in m for m in msgs), msgs)
        self.assertFalse(any("example.com" in m for m in msgs), "an unchecked web link is not a lead")
        self.assertEqual(L.scalar("SELECT domain_code FROM tool_results LIMIT 1"), "docs-developer")
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics WHERE scope='all'")}
        self.assertGreaterEqual(m["links_checked"], 3)

    def test_lychee_runs_offline_unless_asked(self):
        script = ("import sys\nif '--version' in sys.argv:\n    print('lychee 0.21.0'); sys.exit(0)\n"
                  "open('/dev/stderr', 'w').write(' '.join(sys.argv))\n"
                  "assert ('--offline' in sys.argv) != ('ONLINE' in __import__('os').environ), sys.argv\n"
                  "out = sys.argv[sys.argv.index('--output') + 1]\n"
                  f"open(out, 'w').write(open({str(DATA / 'lychee.json')!r}).read())\nsys.exit(2)\n")
        repo, L, sha = tool_project(self, files=LYCHEE_FILES)
        env = env_with(shim("lychee", script))
        ok(L, "tool-run", "lychee", "--review", "R-0001", "--unpinned", "--actor", "A-0001", env=env)
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["exit_code"], run["online"]), ("ran", 2, 0))
        self.assertIn("--offline", run["command"])
        self.assertIn("--include-fragments", run["command"])
        env["ONLINE"] = "1"
        ok(L, "tool-run", "lychee", "--review", "R-0001", "--unpinned", "--online", "--actor", "A-0001", env=env)
        self.assertNotIn("--offline", L.one("SELECT command FROM tool_runs WHERE id=2")["command"])

    @unittest.skipUnless(shutil.which("lychee"), "lychee is not installed")
    def test_lychee_matches_the_recording(self):
        repo, L, sha = tool_project(self, files=LYCHEE_FILES)
        ok(L, "tool-run", "lychee", "--review", "R-0001", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "lychee", "--review", "R-0001", "--from", str(DATA / "lychee.json"), "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start FROM tool_results WHERE tool_run_id=? ORDER BY code", (i,))
                for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])
        self.assertIn("docs/setup.md", L.scalar("SELECT group_concat(message) FROM tool_results WHERE tool_run_id=1"))


DOC_FILES = {
    "README.md": "# Demo\n\nRun `python3 scripts/build.py` or `npm run build`, then `npm run serve`.\n"
                 "The code is in `src/app.py` (see `src/app.py:12`) and [the notes](docs/notes.md).\n"
                 "Settings live in `config/missing.toml`. Set `APP_TOKEN` and `$APP_REGION` first; `HOME` is yours.\n"
                 "```sh\nmake test\nmake deploy\n./tools/release.sh\n```\n"
                 "Paths like `<your file>.py`, `*.md` and `https://example.com/a.py` are not references.\n",
    "docs/notes.md": "# Notes\n\nSee [the readme](../README.md) and `../src/app.py`.\n",
    "CHANGELOG.md": "Removed `src/old.py`.\n",
    "scripts/build.py": "import os\nprint(os.environ.get('APP_TOKEN'))\n",
    "src/app.py": "x = 1\n",
    "package.json": '{"name": "demo", "scripts": {"build": "node build.js"}}\n',
    "Makefile": "test:\n\tpython3 -m unittest\n",
}


class DocReferences(unittest.TestCase):
    def test_doc_reference_scan(self):
        """Every path, script and environment variable the docs name is checked at the commit."""
        repo, L, sha = tool_project(self, files=DOC_FILES)
        out = ok(L, "tool-run", "doc-references", "--review", "R-0001", "--actor", "A-0001")
        self.assertIn("(built in)", L.scalar("SELECT command FROM tool_runs"))
        got = sorted((r, p, n, m) for r, p, n, m in
                     ((x["rule"], x["path"], x["line_start"], x["message"])
                      for x in (L.commit() or L.all("SELECT * FROM tool_results"))))
        found = {(r, m.split("'")[1]) for r, _, _, m in got}
        self.assertEqual(found, {("doc-references.missing-path", "config/missing.toml"),
                                 ("doc-references.missing-path", "./tools/release.sh"),
                                 ("doc-references.missing-script", "npm run serve"),
                                 ("doc-references.missing-script", "make deploy"),
                                 ("doc-references.unknown-env", "APP_REGION")}, out)
        lines = {m.split("'")[1]: (p, n) for _, p, n, m in got}
        self.assertEqual(lines["config/missing.toml"], ("README.md", 5))
        self.assertEqual(lines["make deploy"], ("README.md", 8))
        L.commit()
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "docs-developer")
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics WHERE scope='all'")}
        self.assertEqual(m["doc_references_missing"], 5)
        self.assertGreaterEqual(m["doc_references"], 12)
        # the same commit gives the same rows
        ok(L, "tool-run", "doc-references", "--review", "R-0001", "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message, fingerprint FROM tool_results WHERE tool_run_id=? "
                      "ORDER BY code", (i,)) for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])

    def test_doc_reference_scan_without_docs(self):
        repo, L, sha = shared_project(self)
        out = ok(L, "tool-run", "doc-references", "--review", "R-0001", "--actor", "A-0001")
        self.assertIn("not run: no documentation", out[0])


SCHEMA_SQL = ("CREATE TABLE user_accounts (\n  id INTEGER PRIMARY KEY,\n  display_name TEXT NOT NULL,\n"
              "  email TEXT,\n  state TEXT CHECK (state IN ('active', 'on_hold')),\n"
              "  CONSTRAINT one_email UNIQUE (email)\n);\n"
              "CREATE TYPE plan_kind AS ENUM ('free', 'team_plan');\n")
PAGE_HTML = ("<html><body>\n<h1>Accounts</h1>\n<p>No user_accounts yet.</p>\n"
             "<input placeholder=\"display_name\">\n<p>Your plan: team_plan</p>\n"
             "<p>Enter your email and display name.</p>\n<script>const q = 'SELECT display_name FROM user_accounts';"
             "</script>\n<!-- user_accounts -->\n</body></html>\n")
APP_JS = ("export function show(el) {\n  el.textContent = 'Saved to /api/accounts';\n"
          "  fetch('/api/accounts');\n  el.title = \"Account state: on_hold\";\n}\n")
SERVER_PY = ("from flask import Flask\napp = Flask(__name__)\n\n@app.route('/api/accounts')\ndef accounts():\n"
             "    return 'ok'\n")


class InternalNames(unittest.TestCase):
    def test_internal_name_scan(self):
        """Tables, columns, enumeration values and routes the code defines, found in text a user sees."""
        files = {"db/schema.sql": SCHEMA_SQL, "templates/accounts.html": PAGE_HTML, "static/app.js": APP_JS,
                 "server.py": SERVER_PY}
        repo, L, sha = tool_project(self, files=files)
        out = ok(L, "tool-run", "internal-names", "--review", "R-0001", "--actor", "A-0001")
        L.commit()
        got = [(r["rule"], r["path"], r["line_start"]) for r in
               L.all("SELECT rule, path, line_start FROM tool_results ORDER BY code")]
        self.assertEqual(sorted(got), sorted([
            ("internal-names.table", "templates/accounts.html", 3),
            ("internal-names.column", "templates/accounts.html", 4),
            ("internal-names.enum", "templates/accounts.html", 5),
            ("internal-names.route", "static/app.js", 2),
            ("internal-names.enum", "static/app.js", 4)]), out)
        msg = L.scalar("SELECT message FROM tool_results WHERE rule='internal-names.table'")
        self.assertIn("user_accounts", msg)
        self.assertIn("db/schema.sql", msg)
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "ui")

    def test_internal_name_scan_needs_names_and_text(self):
        repo, L, sha = shared_project(self, files={"templates/a.html": "<p>Hello</p>\n"})
        out = ok(L, "tool-run", "internal-names", "--review", "R-0001", "--actor", "A-0001")
        self.assertIn("not run: no table, column", out[0])


if __name__ == "__main__":
    unittest.main()
