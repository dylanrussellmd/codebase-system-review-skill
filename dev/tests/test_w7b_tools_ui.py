"""The UI tools (plan P-0001 step 223; FIX-31, decision D-0015): axe-core run by ui-audit on every page and imported as
leads; Project Wallace's inventory of CSS values as metrics; Impeccable's check for the generic generated look; odiff
between each screenshot and the last review's; Stylelint with one value-restricting rule built from the project's
design tokens (Style Dictionary JSON, else its CSS custom properties). Each runs only when the commit has a UI or CSS.
The samples are real outputs (axe-core 4.14.0 trimmed to its violations, wallace-cli 4.0.5, Impeccable 4.8.2,
Stylelint 18.0.0); the scratch folders are replaced with /project."""
import json
import os
import shutil
import unittest
import zlib
import struct
from pathlib import Path
from unittest import mock

from dev.tests import browser, fixtures
from dev.tests._w7a_tools_helpers import (DATA, assert_matches_recording, env_with, leads, ok, shared_project,
                                          shim, tool_project)

STYLES_CSS = (".low { color: #aaaaaa; }\n.btn { color: #6366f1; padding: 12px 20px; }\n")
TOKENS = {"tokens/colors.json": json.dumps({"color": {"brand": {"primary": {"value": "#6366f1"},
                                                                "accent": {"value": "#a855f7"}},
                                                      "text": {"body": {"value": "#222222"},
                                                               "muted": {"value": "#666666"}}}}),
          "tokens/size.json": json.dumps({"size": {"spacing": {"sm": {"$value": "8px", "$type": "dimension"},
                                                               "md": {"$value": "16px", "$type": "dimension"}}},
                                          "font": {"size": {"body": {"value": "15px"}}}}),
          "src/app.css": (".btn { color: #6366f1; padding: 8px 16px; font-size: 14px; }\n"
                          ".btn:hover { color: var(--color-brand-accent); }\n"
                          ".note { color: var(--color-text-body); padding: var(--size-spacing-sm) 0; "
                          "font-size: var(--font-size-body); }\n"
                          ".card { margin: 13px; color: #333; background-color: transparent; }\n"
                          ".title { font-size: 15px; }\n")}


def at_repo(name, repo):
    """A recorded sample with its /project folder pointed at the scratch repository."""
    f = fixtures.temp_dir("w7b-sample-") / name
    f.write_text((DATA / name).read_text(encoding="utf-8").replace("/project", str(repo.path)), encoding="utf-8")
    return str(f)


def png(width, height, rgb):
    """A plain one-colour PNG."""
    raw = b"".join(b"\0" + bytes(rgb) * width for _ in range(height))
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Axe(unittest.TestCase):
    def test_axe_import(self):
        """axe-core's violations, from one axe.run() result or from ui-audit --json: one lead per rule and element."""
        repo, L, sha = tool_project(self)
        out = ok(L, "tool-run", "axe", "--review", "R-0001", "--from", str(DATA / "axe.json"), "--actor", "A-0001")
        self.assertIn("4.14.0", out[0])
        got = {(g[1], g[4]) for g in leads(L)}
        self.assertEqual(got, {("axe.button-name", "error"), ("axe.color-contrast", "error"),
                               ("axe.image-alt", "error"), ("axe.label", "error")})
        L.commit()
        msg = L.scalar("SELECT message FROM tool_results WHERE rule='axe.label'")
        self.assertEqual(msg, "page.html: input: Form elements must have labels")
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "ui")
        raw = json.loads((DATA / "axe.json").read_text())
        audit = {"pages": [{"page": "index.html", "width": 390, "theme": "dark",
                            "axe": {"available": True, "version": "4.14.0",
                                    "violations": raw["violations"][:1]}},
                           {"page": "settings.html", "width": 390, "theme": "dark",
                            "axe": {"available": False, "note": "axe-core not found"}}]}
        f = fixtures.temp_dir("w7b-audit-") / "audit.json"
        f.write_text(json.dumps(audit))
        ok(L, "tool-run", "axe", "--review", "R-0001", "--from", str(f), "--actor", "A-0001")
        L.commit()
        self.assertEqual([r["message"] for r in L.all("SELECT message FROM tool_results WHERE tool_run_id=2")],
                         ["index.html at 390 px, dark: button: Buttons must have discernible text"])

    def test_axe_runs_inside_ui_audit(self):
        repo, L, sha = shared_project(self)
        p = L.run("tool-run", "axe", "--review", "R-0001", "--actor", "A-0001")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ui-audit", p.stderr)

    def test_axe_env_detected(self):
        folder = fixtures.temp_dir("w7b-axe-") / "axe-core"
        folder.mkdir()
        (folder / "axe.min.js").write_text("window.axe = {};\n")
        (folder / "package.json").write_text('{"name": "axe-core", "version": "4.14.0"}\n')
        repo, L, sha = shared_project(self)
        env = env_with(bare=True)
        env["LEDGER_AXE"] = str(folder)
        tools = {t["tool"]: t for t in json.loads(L.run_ok("env", "--json", env=env).stdout)["tools"]}
        self.assertEqual((tools["axe"]["installed"], tools["axe"]["pinned"]), ("4.14.0", "4.14.0"))
        env["LEDGER_AXE"] = str(folder / "missing")
        tools = {t["tool"]: t for t in json.loads(L.run_ok("env", "--json", env=env).stdout)["tools"]}
        self.assertIsNone(tools["axe"]["installed"])

    def test_ui_audit_ignores_project_node_modules(self):
        """A stand-in axe.min.js or lighthouse in the reviewed project's node_modules never runs."""
        mod = fixtures.load_ledger_module(fresh=True)
        work = fixtures.temp_dir("w7b-planted-")
        root = work / "proj"
        planted = root / "node_modules" / "axe-core" / "axe.min.js"
        planted.parent.mkdir(parents=True)
        planted.write_text("window.planted = true;\n")
        (root / "node_modules" / ".bin").mkdir()
        (root / "node_modules" / ".bin" / "lighthouse").write_text("#!/bin/sh\necho planted\n")
        mise = work / "mise"
        mise.mkdir()

        class Page:
            def __init__(self):
                self.scripts = []

            def add_script_tag(self, path):
                self.scripts.append(path)

            def evaluate(self, *args):
                return {"violations": [], "version": "4.14.0"}

        def pinned(folder):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "axe.min.js").write_text("window.axe = {};\n")
            return folder / "axe.min.js"

        env = {k: v for k, v in os.environ.items() if k != "LEDGER_AXE"}
        env["MISE_DATA_DIR"] = str(mise)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mod, "_GLOBAL_NODE_MODULES", [""]):
            with self.subTest("none pinned"):
                self.assertIsNone(mod.axe_script(root))
                page = Page()
                got = mod.ui_axe(page, root)
                self.assertEqual(page.scripts, [])
                self.assertFalse(got["available"])
                self.assertIn("not installed", got["note"])
                self.assertIn("npm install -g axe-core@", got["note"])
            with self.subTest("pinned by LEDGER_AXE"):
                own = pinned(work / "own" / "axe-core")
                with mock.patch.dict(os.environ, {"LEDGER_AXE": str(own.parent)}):
                    page = Page()
                    self.assertTrue(mod.ui_axe(page, root)["available"])
                    self.assertEqual(page.scripts, [str(own)])
            with self.subTest("npm's global folder"):
                glob = pinned(work / "global" / "axe-core")
                with mock.patch.object(mod, "_GLOBAL_NODE_MODULES", [str(glob.parent.parent)]):
                    self.assertEqual(mod.axe_script(root), glob)
            with self.subTest("mise's install of the pinned version"):
                version = mod.read_tools_lock().get("axe-core", "4.14.0")
                got = pinned(mise / "installs" / "npm-axe-core" / version / "lib" / "node_modules" / "axe-core")
                self.assertEqual(mod.axe_script(root), got)
        which = {"npx": "/usr/bin/npx"}
        with mock.patch.object(mod.shutil, "which", lambda name, *a, **k: which.get(name)):
            self.assertFalse(mod.lighthouse_installed(root))
        seen = []

        class Done:
            stdout = b'{"categories": {}}'

        which["lighthouse"] = str(work / "global" / "bin" / "lighthouse")
        with mock.patch.object(mod.shutil, "which", lambda name, *a, **k: which.get(name)), \
                mock.patch.object(mod.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)) or Done()):
            mod._lighthouse("http://127.0.0.1:5/#/issues", root)
        self.assertTrue(seen)
        argv, kw = seen[0]
        cwd = Path(kw.get("cwd") or Path.cwd()).resolve()
        self.assertFalse(cwd == root.resolve() or root.resolve() in cwd.parents, cwd)
        self.assertNotIn(str(root / "node_modules"), " ".join(argv))


# The stylesheet wallace.json and impeccable.json were recorded from: Impeccable reads it inside the page.
RECORDED_CSS = (":root { --color-text: #222222; --space-2: 8px; }\n"
                "body { margin: 0; padding: 16px; font-size: 15px; color: #333; }\n"
                ".low { color: #aaaaaa; background: #ffffff; }\n"
                ".card { background: linear-gradient(135deg, #6366f1, #a855f7); padding: 24px; border-radius: 12px; "
                "box-shadow: 0 4px 24px rgba(0,0,0,.08); }\n"
                ".btn { color: #6366f1; padding: 8px 16px; font-size: 14px; }\n.btn:hover { color: #4f46e5; }\n"
                "h1 { font-size: 1rem; margin: 0 0 8px; color: #222222; }\n"
                "@media (max-width: 600px) { .card { padding: 12px; } }\n")
RECORDED_PAGE = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<title>Demo</title>\n"
                 f"<style>\n{RECORDED_CSS}</style>\n</head>\n<body>\n<h1>Demo</h1>\n<p class=\"low\">Muted note</p>\n"
                 "<div class=\"card\"><p>Card text</p><a class=\"btn\" href=\"#\">Go</a></div>\n</body>\n</html>\n")


class Wallace(unittest.TestCase):
    def test_wallace_inventory(self):
        """Distinct colours, font sizes and other values become metrics that trend between reviews; a category
        with more than its limit is a lead."""
        repo, L, sha = tool_project(self, files={"web/styles.css": STYLES_CSS})
        ok(L, "tool-run", "wallace", "--review", "R-0001", "--from", str(DATA / "wallace.json"), "--actor", "A-0001")
        L.commit()
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics WHERE scope='all'")}
        self.assertEqual((m["css_colors"], m["css_font_sizes"], m["css_border_radii"], m["css_declarations"]),
                         (8, 3, 1, 20))
        self.assertEqual(L.count("tool_results"), 0)
        doc = json.loads((DATA / "wallace.json").read_text())
        doc["values"]["colors"]["unique"] = {f"#0000{n:02x}": 1 for n in range(20)}
        doc["values"]["colors"]["totalUnique"] = 20
        f = fixtures.temp_dir("w7b-wal-") / "wallace.json"
        f.write_text(json.dumps(doc))
        ok(L, "tool-run", "wallace", "--review", "R-0001", "--from", str(f), "--actor", "A-0001")
        self.assertEqual([g[1] for g in leads(L)], ["wallace.many-colors"])

    def test_wallace_matches_the_recording(self):
        assert_matches_recording(self, "wallace", DATA / "wallace.json", {"web/styles.css": RECORDED_CSS})

    def test_wallace_reads_every_stylesheet(self):
        script = ("import sys\ncss = sys.stdin.read()\nassert '.low' in css and '.extra' in css, css\n"
                  f"sys.stdout.write(open({str(DATA / 'wallace.json')!r}).read())\n")
        repo, L, sha = tool_project(self, files={"web/styles.css": STYLES_CSS, "web/extra.css": ".extra{}\n"})
        out = ok(L, "tool-run", "wallace", "--review", "R-0001", "--unpinned", "--actor", "A-0001",
                 env=env_with(shim("wallace", script)))
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual(run["status"], "ran", out)
        self.assertIn("wallace --json", run["command"])
        repo, L, sha = shared_project(self)
        out = ok(L, "tool-run", "wallace", "--review", "R-0001", "--unpinned", "--actor", "A-0001",
                 env=env_with(shim("wallace", script)))
        self.assertIn("not run: the commit has no stylesheets", out[0])


class Impeccable(unittest.TestCase):
    def test_impeccable_import(self):
        repo, L, sha = tool_project(self, files={"templates/page.html": "<p>x</p>\n"})
        ok(L, "tool-run", "impeccable", "--review", "R-0001", "--from", at_repo("impeccable.json", repo),
           "--actor", "A-0001")
        got = sorted(g[1:] for g in leads(L))
        self.assertEqual(got, [("impeccable.ai-color-palette", "templates/page.html", None, "warning"),
                               ("impeccable.low-contrast", "templates/page.html", None, "warning"),
                               ("impeccable.low-contrast", "templates/page.html", None, "warning")])
        L.commit()
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='generic_look_findings'"), 1)

    def test_impeccable_matches_the_recording(self):
        assert_matches_recording(self, "impeccable", DATA / "impeccable.json", {"templates/page.html": RECORDED_PAGE})

    def test_impeccable_runs_on_the_ui_only(self):
        script = ("import sys, os\nif '--version' in sys.argv:\n    print('4.8.2'); sys.exit(0)\n"
                  "assert sys.argv[1:] == ['detect', '--json', '.'], sys.argv\n"
                  "assert os.environ.get('DO_NOT_TRACK') == '1'\n"
                  f"sys.stdout.write(open({str(DATA / 'impeccable.json')!r}).read().replace('/project', os.getcwd()))"
                  "\nsys.exit(2)\n")
        repo, L, sha = tool_project(self, files={"templates/page.html": "<p>x</p>\n"})
        ok(L, "tool-run", "impeccable", "--review", "R-0001", "--actor", "A-0001", env=env_with(shim("impeccable",
                                                                                                    script)))
        self.assertEqual(L.scalar("SELECT status FROM tool_runs"), "ran")
        self.assertEqual(L.count("tool_results"), 3)
        repo, L, sha = shared_project(self)
        out = ok(L, "tool-run", "impeccable", "--review", "R-0001", "--actor", "A-0001",
                 env=env_with(shim("impeccable", script)))
        self.assertIn("not run", out[0])


ODIFF_SHIM = """import sys, filecmp
if '--version' in sys.argv:
    print('odiff 4.5.0 - SIMD first pixel-by-pixel image comparison tool'); sys.exit(0)
a, b, diff = sys.argv[1:4]
assert '--parsable-stdout' in sys.argv and '--fail-on-layout' in sys.argv, sys.argv
if filecmp.cmp(a, b, shallow=False):
    print('0'); sys.exit(0)
if open(a, 'rb').read()[16:24] != open(b, 'rb').read()[16:24]:
    print('layout'); sys.exit(21)
open(diff, 'wb').write(b'PNG diff')
print('229;0.20'); sys.exit(22)
"""


class Odiff(unittest.TestCase):
    def screenshots(self, L, review, shots):
        folder = L.dir / "reviews" / review / "ui"
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in shots.items():
            (folder / name).write_bytes(data)
            ok(L, "add-artifact", "other", "--path", str(folder / name), "--review", review, "--actor", "A-0001")

    def test_odiff_against_last_review(self):
        repo, L, sha = tool_project(self)
        red, blue = png(4, 4, (255, 0, 0)), png(4, 4, (0, 0, 255))
        self.screenshots(L, "R-0001", {"home-390-light.png": red, "home-1280-light.png": red,
                                       "gone-390-light.png": red, "wide-390-light.png": red})
        env = env_with(shim("odiff", ODIFF_SHIM))
        out = ok(L, "tool-run", "odiff", "--review", "R-0001", "--unpinned", "--actor", "A-0001", env=env)
        self.assertIn("not run", out[0])
        self.assertIn("follows up no review", L.scalar("SELECT fallback FROM tool_runs"))
        ok(L, "review-status", "R-0001", "handed-off", "--note", "done", "--actor", "A-0001")
        ok(L, "new-review", "--title", "Second", "--scope", "full", "--previous", "R-0001", "--actor", "A-0001")
        self.screenshots(L, "R-0002", {"home-390-light.png": blue, "home-1280-light.png": red,
                                       "new-390-light.png": blue, "wide-390-light.png": png(5, 4, (255, 0, 0))})
        ok(L, "tool-run", "odiff", "--review", "R-0002", "--actor", "A-0001", env=env)
        L.commit()
        run = L.one("SELECT * FROM tool_runs WHERE id=2")
        self.assertEqual((run["status"], run["version"]), ("ran", "4.5.0"))
        got = [(r["rule"], r["message"]) for r in L.all("SELECT rule, message FROM tool_results ORDER BY code")]
        self.assertEqual([g[0] for g in got], ["odiff.changed", "odiff.layout-changed"])
        self.assertIn("home-390-light.png: 0.2% of pixels (229) differ from R-0001's (diff image "
                      ".code-review/reviews/R-0002/ui/odiff/home-390-light.png)", got[0][1])
        self.assertTrue((L.dir / "reviews" / "R-0002" / "ui" / "odiff" / "home-390-light.png").is_file())
        m = {(r["name"], r["scope"]): r["value"] for r in L.all("SELECT name, scope, value FROM metrics")}
        self.assertEqual(m[("screenshot_changed_percent", "home-390-light.png")], 0.2)
        self.assertEqual(m[("screenshot_changed_percent", "home-1280-light.png")], 0)
        self.assertEqual((m[("screenshots_compared", "all")], m[("screenshots_new", "all")],
                          m[("screenshots_gone", "all")]), (3, 1, 1))

    @unittest.skipUnless(shutil.which("odiff"), "environment: odiff is not installed")
    def test_odiff_real(self):
        repo, L, sha = tool_project(self)
        self.screenshots(L, "R-0001", {"a.png": png(8, 8, (255, 0, 0))})
        ok(L, "review-status", "R-0001", "handed-off", "--note", "done", "--actor", "A-0001")
        ok(L, "new-review", "--title", "Second", "--scope", "full", "--previous", "R-0001", "--actor", "A-0001")
        self.screenshots(L, "R-0002", {"a.png": png(8, 8, (0, 0, 255))})
        ok(L, "tool-run", "odiff", "--review", "R-0002", "--unpinned", "--actor", "A-0001")
        L.commit()
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='screenshot_changed_percent'"), 100)


STYLELINT_SHIM = """import json, os, sys
if '--version' in sys.argv:
    print('18.0.0'); sys.exit(0)
cfg = json.load(open(sys.argv[sys.argv.index('--config') + 1]))
allowed = cfg['rules']['declaration-property-value-allowed-list']
text = json.dumps(allowed)
assert 'color-brand-primary' in text and 'size-spacing-sm' in text and 'font-size-body' in text, text
open(os.environ.get('SEEN', os.devnull), 'w').write(text)
out = sys.argv[sys.argv.index('--output-file') + 1]
open(out, 'w').write(open(%r).read().replace('/project', os.getcwd()))
sys.exit(2)
""" % str(DATA / "stylelint.json")


class Stylelint(unittest.TestCase):
    def test_stylelint_token_rules(self):
        """A raw value where a token should be is a lead naming the token that holds it (Style Dictionary JSON)."""
        repo, L, sha = tool_project(self, files=TOKENS)
        ok(L, "tool-run", "stylelint", "--review", "R-0001", "--actor", "A-0001", env=env_with(shim("stylelint",
                                                                                                   STYLELINT_SHIM)))
        L.commit()
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual(run["status"], "ran")
        self.assertIn("--config {rules}", run["command"])
        got = [(r["line_start"], r["message"]) for r in
               L.all("SELECT line_start, message FROM tool_results WHERE rule='stylelint.raw-value' ORDER BY code")]
        self.assertEqual(got, [
            (1, "color: #6366f1 is a raw value; var(--color-brand-primary) holds it"),
            (1, "font-size: 14px is a raw value; use a font-size token (tokens/colors.json, tokens/size.json)"),
            (1, "padding: 8px 16px is a raw value; 8px: var(--size-spacing-sm), 16px: var(--size-spacing-md)"),
            (4, "color: #333 is a raw value; use a color token (tokens/colors.json, tokens/size.json)"),
            (4, "margin: 13px is a raw value; use a spacing token (tokens/colors.json, tokens/size.json)"),
            (5, "font-size: 15px is a raw value; var(--font-size-body) holds it")])
        self.assertEqual(L.scalar("SELECT DISTINCT path FROM tool_results"), "src/app.css")
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='design_tokens'"), 7)
        # the same file imported gives the same rows: the tokens are read from the commit either way
        ok(L, "tool-run", "stylelint", "--review", "R-0001", "--from", at_repo("stylelint.json", repo),
           "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message FROM tool_results WHERE tool_run_id=? ORDER BY code",
                      (i,)) for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])

    def test_stylelint_reads_custom_properties_without_tokens(self):
        mod = fixtures.load_ledger_module()
        repo = fixtures.ScratchRepo()
        repo.write("web/app.css", ":root { --brand: #6366f1; --space-2: 8px; --radius-md: 6px; --x: bold; }\n"
                                  ".a { color: #6366f1; }\n")
        sha = repo.commit("css")
        found = mod.design_tokens(repo.path, sha)
        self.assertEqual(found["kind"], "custom-properties")
        self.assertEqual([(t["css"], t["category"]) for t in found["tokens"]],
                         [("--brand", "color"), ("--radius-md", "radius"), ("--space-2", "spacing")])
        cfg = mod.stylelint_config(found["tokens"])
        rules = cfg["rules"]["declaration-property-value-allowed-list"]
        self.assertEqual(len(rules), 3)
        self.assertIsNone(mod.stylelint_config([]))

    def test_stylelint_needs_css_and_tokens(self):
        env = env_with(shim("stylelint", STYLELINT_SHIM))
        repo, L, sha = tool_project(self, files={"web/app.css": ".a { color: red; }\n"})
        out = ok(L, "tool-run", "stylelint", "--review", "R-0001", "--actor", "A-0001", env=env)
        self.assertIn("no design tokens", out[0])
        repo, L, sha = shared_project(self)
        out = ok(L, "tool-run", "stylelint", "--review", "R-0001", "--actor", "A-0001", env=env)
        self.assertIn("plain CSS files", out[0])

    @unittest.skipUnless(shutil.which("stylelint"), "environment: Stylelint is not installed")
    def test_stylelint_matches_the_recording(self):
        repo, L, sha = tool_project(self, files=TOKENS)
        ok(L, "tool-run", "stylelint", "--review", "R-0001", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "stylelint", "--review", "R-0001", "--from", at_repo("stylelint.json", repo),
           "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message FROM tool_results WHERE tool_run_id=? ORDER BY code",
                      (i,)) for i in (1, 2))
        self.assertTrue(a)
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])


AXE_PAGE = ("<!doctype html><html lang=\"en\"><head><title>Planted</title></head><body><main><h1>Planted</h1>"
            "<img src=\"data:image/gif;base64,R0lGODlhAQABAAAAACw=\"><input type=\"text\"><button></button>"
            "</main></body></html>\n")


@unittest.skipUnless(os.environ.get("LEDGER_BROWSER_TESTS") == "1", "browser tests run with LEDGER_BROWSER_TESTS=1")
class UiAuditAxe(unittest.TestCase):
    def test_ui_audit_runs_axe_on_each_page(self):
        mod = fixtures.load_ledger_module()
        found = mod.axe_script(Path.cwd())
        if found is None:
            browser.unavailable("axe-core is not installed (LEDGER_AXE)")
        repo, L, sha = tool_project(self)
        page = L.dir / "planted.html"
        page.write_text(AXE_PAGE, encoding="utf-8")
        env = fixtures.git_env({"LEDGER_AXE": str(found)})
        p = L.run("ui-audit", page, "--json", env=env, timeout=180)
        if p.returncode == 2:
            browser.unavailable(p.stderr.strip())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        axe = json.loads(p.stdout)["pages"][0]["axe"]
        self.assertTrue(axe["available"])
        self.assertTrue({"button-name", "image-alt", "label"} <= {v["id"] for v in axe["violations"]}, axe)
        self.assertIn("axe-core", L.run_ok("ui-audit", page, env=env, timeout=180).stdout)
        audit = L.dir / "audit.json"
        audit.write_text(p.stdout, encoding="utf-8")
        ok(L, "tool-run", "axe", "--review", "R-0001", "--from", str(audit), "--actor", "A-0001")
        self.assertIn(("axe.label", "error"), {(g[1], g[4]) for g in leads(L)})
        env["LEDGER_AXE"] = str(L.dir / "no-such-folder")
        none = json.loads(L.run_ok("ui-audit", page, "--json", env=env, timeout=180).stdout)["pages"][0]["axe"]
        self.assertFalse(none["available"])


if __name__ == "__main__":
    unittest.main()
