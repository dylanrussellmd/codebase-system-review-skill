"""env, ui-capture and ui-audit (plan P-0001 step 135; OWN-6, OWN-8): the tool finds what the environment offers
(git, Python, SQLite, Playwright and a browser, node and Lighthouse, the harness and its MCP connectors) and records
it on a review, each capability with how to enable it. It reads connector names from the harness's config files and
never a value from them. Without Playwright the two UI commands print the install line and exit 2."""
import json
import os
import sqlite3
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, project

SECRET = "figd_SECRET-TOKEN-VALUE"
INSTALL_LINE = "python3 -m pip install playwright"


def fake_home(root):
    """A HOME and a project folder holding fake harness config files that name a figma server (with a token)."""
    home = fixtures.temp_dir("w4-home-")
    (home / ".gitconfig").write_text("")
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.jsonc").write_text(
        '{\n  // user servers\n  "mcp": {"penpot": {"type": "remote", "url": "https://design.example/mcp", '
        '"enabled": false,},},\n}\n')
    (root / "opencode.json").write_text(json.dumps(
        {"mcp": {"figma": {"type": "local", "command": ["npx", "figma-mcp"],
                           "environment": {"FIGMA_TOKEN": SECRET}}}}))
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"figma": {"command": "npx", "env": {"FIGMA_TOKEN": SECRET}}, "tracker": {"url": "x"}}}))
    (home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"notes": {"command": "notes-mcp"}},
         "projects": {str(root): {"mcpServers": {"canvas": {"command": "c"}}},
                      "/somewhere/else": {"mcpServers": {"elsewhere": {"command": "e"}}}}}))
    return home


def env_for(home, **extra):
    e = fixtures.git_env({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                          "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")})
    for k in [k for k in e if k.startswith(("OPENCODE", "CLAUDE"))]:
        del e[k]
    e.update(extra)
    return e


def no_playwright_path():
    """A folder to put first on PYTHONPATH: its playwright package cannot be imported."""
    d = fixtures.temp_dir("w4-nopw-")
    (d / "playwright").mkdir()
    (d / "playwright" / "__init__.py").write_text("raise ImportError('no playwright here')\n")
    return str(d)


class Env(unittest.TestCase):
    def test_env_detects_fake_harness(self):
        repo, L, _ = project(self)
        home = fake_home(L.root)
        e = env_for(home, OPENCODE="1")
        out = json.loads(L.run_ok("env", "--json", env=e).stdout)
        caps = {c["capability"]: c for c in out["capabilities"]}
        self.assertEqual([c["capability"] for c in out["capabilities"]],
                         sorted(caps, key=lambda k: (caps[k]["position"], k)))
        for name in ("git", "python", "sqlite", "playwright", "browser", "node", "lighthouse", "harness",
                     "design-tool", "subagents", "questions", "publishing"):
            self.assertIn(name, caps)
            self.assertTrue(caps[name]["how_to_enable"].strip(), name)
        self.assertTrue(caps["git"]["available"] and caps["python"]["available"] and caps["sqlite"]["available"])
        self.assertEqual(caps["harness"]["detail"], "OpenCode")
        self.assertTrue(caps["connector:figma"]["available"])
        self.assertIn("opencode.json", caps["connector:figma"]["detail"])
        self.assertIn(".mcp.json", caps["connector:figma"]["detail"])
        self.assertFalse(caps["connector:penpot"]["available"])       # "enabled": false in the user's config
        self.assertTrue(caps["connector:canvas"]["available"])        # this project's servers in ~/.claude.json
        self.assertTrue(caps["connector:notes"]["available"])
        self.assertNotIn("connector:elsewhere", caps)                 # another project's server
        self.assertTrue(caps["design-tool"]["available"])
        self.assertIn("figma", caps["design-tool"]["detail"])
        with self.subTest("recorded on the review, names only"):
            first = L.run_ok("env", "--review", "R-0001", "--has", "subagents", "--actor", "A-0001", env=e).stdout
            L.commit()
            got = {r["capability"]: r for r in L.all(
                "SELECT e.* FROM review_environment e JOIN reviews r ON r.id = e.review_id WHERE r.code='R-0001'")}
            self.assertEqual(set(got), set(caps))
            self.assertEqual(got["connector:figma"]["available"], 1)
            self.assertEqual(got["subagents"]["available"], 1)
            self.assertEqual(got["questions"]["available"], 0)
            self.assertTrue(all(r["how_to_enable"] for r in got.values()))
            dump = "\n".join(sqlite3.connect(str(L.db)).iterdump())
            for value in (SECRET, "design.example", "figma-mcp", "FIGMA_TOKEN"):
                self.assertNotIn(value, dump)
                self.assertNotIn(value, first)
            self.assertEqual(L.run_ok("env", "--review", "R-0001", "--has", "subagents", "--actor", "A-0001",
                                      env=e).stdout, first)       # same environment, same output
        with self.subTest("a capability that went away is recorded as gone"):
            (L.root / ".mcp.json").unlink()
            (L.root / "opencode.json").unlink()
            L.run_ok("env", "--review", "R-0001", env=e)
            L.commit()
            row = L.one("SELECT available, detail FROM review_environment WHERE capability='connector:figma'")
            self.assertEqual((row["available"], row["detail"]), (0, "no longer found"))
        with self.subTest("the harness can be named, and other harness variables are read"):
            self.assertIn("Claude Code", L.run_ok("env", env=env_for(home, CLAUDECODE="1")).stdout)
            other = json.loads(L.run_ok("env", "--json", env=env_for(home)).stdout)
            self.assertFalse({c["capability"]: c for c in other["capabilities"]}["harness"]["available"])
            named = json.loads(L.run_ok("env", "--json", "--harness", "Aider", env=env_for(home)).stdout)
            self.assertEqual({c["capability"]: c for c in named["capabilities"]}["harness"]["detail"], "Aider")

    def test_env_needs_no_ledger(self):
        d = fixtures.temp_dir("w4-noledger-")
        p = fixtures.run_ledger("--root", d, "env", env=env_for(fake_home(d)))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("connector:figma", p.stdout)
        self.assertFalse((d / ".code-review").exists())

    def test_env_refuses_an_unknown_review_or_capability(self):
        repo, L, _ = project(self)
        p = L.run("env", "--review", "R-0009")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertIn("no review R-0009", p.stderr)
        p = L.run("env", "--has", "telepathy")
        self.assertEqual(p.returncode, 1)
        self.assertIn("telepathy", p.stderr)

    def test_env_without_playwright(self):
        repo, L, _ = project(self)
        e = env_for(fake_home(L.root), PYTHONPATH=no_playwright_path())
        out = json.loads(L.run_ok("env", "--json", env=e).stdout)
        caps = {c["capability"]: c for c in out["capabilities"]}
        self.assertFalse(caps["playwright"]["available"])
        self.assertIn(INSTALL_LINE, caps["playwright"]["how_to_enable"])
        self.assertIn(INSTALL_LINE, L.run_ok("env", env=e).stdout)

    def test_ui_commands_without_playwright(self):
        repo, L, _ = project(self)
        e = env_for(fake_home(L.root), PYTHONPATH=no_playwright_path())
        for args in (("ui-capture", "http://127.0.0.1:9/", "--review", "R-0001"),
                     ("ui-audit", "http://127.0.0.1:9/")):
            with self.subTest(cmd=args[0]):
                p = L.run(*args, env=e)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(INSTALL_LINE, p.stderr)
                self.assertIn("references/environment.md", p.stderr)
                self.assertNotIn("Traceback", p.stderr)
        self.assertFalse((L.dir / "reviews" / "R-0001" / "ui").exists())

    def test_ui_capture_refuses_a_folder_outside_the_ledger(self):
        repo, L, _ = project(self)
        outside = fixtures.temp_dir("w4-out-")
        p = L.run("ui-capture", "http://127.0.0.1:9/", "--review", "R-0001", "--out", outside)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("not inside the ledger's folder", p.stderr)
        self.assertEqual(list(outside.iterdir()), [])


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Planted</title>
<style>
body { font-size: 16px; color: #111111; background: #ffffff; margin: 24px; }
@media (prefers-color-scheme: dark) { body { color: #eeeeee; background: #111111; } }
.faint { color: #bbbbbb; background: #ffffff; font-size: 13px; padding: 4px 8px; }
</style></head><body>
<h1>Planted page</h1>
<p class="faint" id="planted">This grey text on white fails contrast.</p>
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==">
<input type="text" id="unlabelled">
<button></button>
</body></html>
"""


@unittest.skipUnless(os.environ.get("LEDGER_BROWSER_TESTS") == "1", "browser tests run with LEDGER_BROWSER_TESTS=1")
class UiCommands(unittest.TestCase):
    def test_ui_capture_and_audit(self):
        repo, L, _ = project(self)
        page = L.dir / "planted.html"
        page.write_text(PAGE, encoding="utf-8")
        p = L.run("ui-capture", page, "--review", "R-0001", "--widths", "390,1280", "--themes", "light,dark",
                  "--actor", "A-0001", timeout=180)
        if p.returncode == 2:
            self.skipTest(p.stderr.strip())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        shots = sorted(f.name for f in (L.dir / "reviews" / "R-0001" / "ui").iterdir())
        self.assertEqual(shots, ["planted-html-1280-dark.png", "planted-html-1280-light.png",
                                 "planted-html-390-dark.png", "planted-html-390-light.png"])
        L.commit()
        rows = L.all("SELECT kind, path, sha256 FROM artifacts ORDER BY path")
        self.assertEqual([Path(r["path"]).name for r in rows], shots)
        self.assertTrue(all(r["kind"] == "other" and len(r["sha256"]) == 64 for r in rows))
        light = (L.dir / "reviews" / "R-0001" / "ui" / "planted-html-390-light.png").read_bytes()
        dark = (L.dir / "reviews" / "R-0001" / "ui" / "planted-html-390-dark.png").read_bytes()
        self.assertTrue(light.startswith(b"\x89PNG") and light != dark)
        again = L.run_ok("ui-capture", page, "--review", "R-0001", "--actor", "A-0001", timeout=180).stdout
        self.assertEqual(L.one("SELECT count(*) n FROM artifacts")["n"], 4, again)   # recorded once per file

        p = L.run("ui-audit", page, "--json", timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        a = out["pages"][0]
        self.assertTrue(any("fails contrast" in f["text"] and f["ratio"] < 4.5 for f in a["contrast_failures"]),
                        a["contrast_failures"])
        self.assertFalse(any("Planted page" in f["text"] for f in a["contrast_failures"]))
        kinds = {m["problem"] for m in a["missing_names"]}
        self.assertEqual(kinds, {"image without alt text", "field without a label", "button without a name"})
        inv = a["inventory"]
        self.assertIn("rgb(187, 187, 187)", inv["colours"])
        self.assertIn("13px", inv["font_sizes"])
        self.assertIn("24px", inv["spacing"])
        self.assertGreater(a["page_weight_bytes"], 500)
        self.assertIsInstance(a["layout_shift"], (int, float))
        self.assertIn("lighthouse", out)
        self.assertEqual(json.loads(L.run_ok("ui-audit", page, "--json", timeout=180).stdout), out)
        text = L.run_ok("ui-audit", page, timeout=180).stdout
        self.assertIn("contrast", text)
        self.assertIn("rgb(187, 187, 187)", text)


if __name__ == "__main__":
    unittest.main()
