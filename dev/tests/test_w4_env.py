"""env, ui-capture and ui-audit (plan P-0001 step 135; OWN-6, OWN-8): the tool finds what the environment offers
(git, Python, SQLite, Playwright and a browser, node and Lighthouse, the harness and its MCP connectors) and records
it on a review, each capability with how to enable it. It reads connector names from the harness's config files and
never a value from them. Without Playwright the two UI commands print the install line and exit 2."""
import json
import os
import sqlite3
import unittest
from pathlib import Path

from dev.tests import browser, fixtures
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

    def test_ui_targets_refuse_key_in_base(self):
        mod = fixtures.load_ledger_module()
        for targets, base in ((["#/issues"], "http://h:1/#k=abcdef"), (["http://h:1/#k=abcdef#/issues"], None),
                              (["http://h:1/#k=abcdef/#/issues"], None), (["page.html#k=abcdef"], None)):
            with self.subTest(targets=targets, base=base):
                with self.assertRaises(mod.WriteError) as e:
                    mod._ui_targets(targets, base)
                self.assertNotIn("abcdef", str(e.exception))
                self.assertIn("key", str(e.exception))
        repo, L, _ = project(self)
        for cmd in ("ui-capture", "ui-audit"):
            p = L.run(cmd, "#/issues", "--base", "http://127.0.0.1:9/#k=abcdef",
                      *(["--review", "R-0001"] if cmd == "ui-capture" else []))
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertNotIn("abcdef", p.stdout + p.stderr)
            self.assertEqual(len(p.stderr.strip().split("\n")), 1, p.stderr)
        self.assertFalse((L.dir / "reviews" / "R-0001" / "ui").exists())
        # Lighthouse is never handed a key, even one that reached it some other way
        seen = []

        class Done:
            stdout = b'{"categories": {}}'
        old = mod.lighthouse_installed, mod.subprocess.run
        mod.lighthouse_installed = lambda root: True
        mod.subprocess.run = lambda argv, **kw: seen.append(argv) or Done()
        try:
            mod._lighthouse("http://127.0.0.1:5/#k=abcdef/#/issues", L.root)
        finally:
            mod.lighthouse_installed, mod.subprocess.run = old
        self.assertTrue(seen)
        self.assertNotIn("abcdef", " ".join(seen[0]))

    def test_ui_viewer_is_read_only(self):
        # the viewer ui-capture serves for itself reads, and refuses every POST
        import threading
        import urllib.error
        import urllib.request
        mod = fixtures.load_ledger_module()
        repo, L, _ = project(self)
        L.commit()
        srv, key = mod.make_server(L.root, L.db, 0, quiet=True, read_only=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        got = opener.open(urllib.request.Request(url + "overview", headers={"X-Ledger-Token": key}))
        self.assertEqual(got.status, 200)
        post = urllib.request.Request(url + "gates/G-0001/answer", data=b"{}", method="POST",
                                      headers={"X-Ledger-Token": key, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            opener.open(post)
        self.assertEqual(e.exception.code, 403)
        self.assertIn("read-only", e.exception.read().decode())

    def test_ui_targets_names(self):
        mod = fixtures.load_ledger_module()
        d = fixtures.temp_dir("w4-names-")
        page = d / "page.html"
        page.write_text("<h1>x</h1>")
        on_http = mod._ui_targets(["#/issues", "#/inbox", "#/issue/ISS-0001", "#/", "#/issues?status=open&q=Secret"],
                                  "http://127.0.0.1:8765")
        self.assertEqual([n for n, _ in on_http], ["issues", "inbox", "issue-iss-0001", "home", "issues-status-q"])
        self.assertEqual(on_http[0][1], "http://127.0.0.1:8765/#/issues")
        self.assertEqual(mod._ui_targets(["#/issues"], "http://h:1/app/")[0][1], "http://h:1/app/#/issues")
        on_file = mod._ui_targets(["#/issues", "#/inbox", "#/issue/ISS-0001", "#/"], str(page))
        self.assertEqual([n for n, _ in on_file], ["issues", "inbox", "issue-iss-0001", "home"])
        self.assertEqual(on_file[0][1], page.resolve().as_uri() + "#/issues")
        (name, url), = mod._ui_targets([f"{page}#/issues"], None)
        self.assertEqual(name, "issues")
        self.assertTrue(url.startswith("file://"))
        self.assertEqual(mod.urlsplit(url).fragment, "/issues")
        self.assertEqual(mod._ui_targets([str(page)], None)[0][0], "page-html")
        self.assertEqual([n for n, _ in mod._ui_targets(["http://a.example:3000/", "http://b.example/docs/start?x=1"],
                                                        None)], ["home", "start"])
        with self.assertRaises(mod.WriteError) as e:
            mod._ui_targets([str(d / "missing.html") + "#/inbox"], None)
        self.assertIn("hub.html#/inbox", str(e.exception))


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
            browser.unavailable(p.stderr.strip())
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

    def test_ui_capture_route_on_keyed_base(self):
        # with no --base, ui-capture serves the ledger itself, keyed, and the page gets the key without the address
        repo, L, _ = project(self)
        p = L.run("ui-capture", "#/issues", "#/inbox", "--review", "R-0001", "--widths", "390", "--themes", "light",
                  "--actor", "A-0001", timeout=180)
        if p.returncode == 2:
            browser.unavailable(p.stderr.strip())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        folder = L.dir / "reviews" / "R-0001" / "ui"
        self.assertEqual(sorted(f.name for f in folder.iterdir()), ["inbox-390-light.png", "issues-390-light.png"])
        self.assertIn("issues-390-light.png: Issues", p.stdout)
        self.assertIn("inbox-390-light.png: Waiting on you", p.stdout)
        self.assertNotIn("could not be loaded", p.stdout)
        self.assertNotIn("k=", p.stdout + p.stderr)
        for f in folder.iterdir():
            self.assertEqual(f.stat().st_mode & 0o777, 0o600, f.name)
        self.assertNotEqual((folder / "issues-390-light.png").read_bytes(), (folder / "inbox-390-light.png").read_bytes())
        p = L.run("ui-audit", "#/issues", "#/inbox", "--json", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pages = json.loads(p.stdout)["pages"]
        self.assertEqual({(x["page"], x["width"], x["theme"]) for x in pages},
                         {(r, w, t) for r in ("#/issues", "#/inbox") for w in (390, 1280) for t in ("light", "dark")})
        self.assertNotEqual([x["inventory"] for x in pages if x["page"] == "#/issues"],
                            [x["inventory"] for x in pages if x["page"] == "#/inbox"])

    def test_ui_capture_viewer_routes_show_data(self):
        # a running viewer's key comes from LEDGER_VIEWER_KEY and goes into no output
        repo, L, _ = project(self)
        L.commit()
        with fixtures.ServerThread(L) as srv:
            env = fixtures.git_env({"LEDGER_VIEWER_KEY": srv.token})
            p = L.run("ui-capture", "#/inbox", "#/issues", "--base", srv.url, "--review", "R-0001", "--widths", "390",
                      "--themes", "light", "--actor", "A-0001", env=env, timeout=180)
            if p.returncode == 2:
                browser.unavailable(p.stderr.strip())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("inbox-390-light.png: Waiting on you", p.stdout)
            self.assertNotIn(srv.token, p.stdout + p.stderr)
            audit = L.run("ui-audit", "#/inbox", "--base", srv.url, "--json", env=env, timeout=300)
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        self.assertNotIn(srv.token, audit.stdout + audit.stderr)
        self.assertNotIn("k=", audit.stdout)
        self.assertEqual(len(json.loads(audit.stdout)["pages"]), 4)
        L.commit()
        paths = [r["path"] for r in L.all("SELECT path FROM artifacts")]
        self.assertEqual(sorted(Path(x).name for x in paths), ["inbox-390-light.png", "issues-390-light.png"])
        self.assertFalse(any(srv.token in x or "k-" in Path(x).name for x in paths), paths)

    def test_ui_capture_error_page_fails(self):
        # a viewer whose key was not given draws its missing-key page: one line, exit 1, nothing kept or recorded
        repo, L, _ = project(self)
        L.commit()
        with fixtures.ServerThread(L) as srv:
            p = L.run("ui-capture", "#/issues", "--base", srv.url, "--review", "R-0001", "--widths", "390",
                      "--themes", "light", "--actor", "A-0001", timeout=180)
        if p.returncode == 2:
            browser.unavailable(p.stderr.strip())
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().split("\n")), 1, p.stderr)
        self.assertIn("could not be loaded", p.stderr)
        self.assertIn("LEDGER_VIEWER_KEY", p.stderr)
        folder = L.dir / "reviews" / "R-0001" / "ui"
        self.assertEqual(list(folder.iterdir()) if folder.exists() else [], [])
        L.commit()
        self.assertEqual(L.all("SELECT path FROM artifacts"), [])
        for n in ("a", "b"):
            (L.dir / f"{n}.html").write_text(PAGE, encoding="utf-8")
        p = L.run("ui-capture", L.dir / "a.html", L.dir / "b.html", "--review", "R-0001", "--widths", "390",
                  "--themes", "light", "--actor", "A-0001", timeout=180)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("same image", p.stderr)
        self.assertEqual(list(folder.iterdir()) if folder.exists() else [], [])


class TakeToken(browser.ViewerTestCase):
    def test_take_token_accepts_slash(self):
        page = self.new_page(token=None)
        page.page.goto(self.server.url + "#k=" + self.server.token + "/#/inbox")
        page.settle()
        self.assertEqual(page.text("h1"), "Waiting on you")
        self.assertEqual(page.js("location.hash"), "#/inbox")


if __name__ == "__main__":
    unittest.main()
