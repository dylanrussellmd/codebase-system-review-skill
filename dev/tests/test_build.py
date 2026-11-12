"""dev/build.py: scripts/ledger.py is exactly the build of its sources, and broken embeds fail the build."""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from dev.tests import fixtures

ROOT = fixtures.REPO_ROOT
SOURCES = ("dev/build.py", "dev/core.py", "dev/page.html", "references/schema.sql", "scripts/ledger.py", "SKILL.md")


def copy_tree():
    """A temporary copy of the files the build reads and writes; returns its root."""
    root = fixtures.temp_dir("build-")
    for rel in SOURCES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(ROOT / rel), str(root / rel))
    shutil.copytree(str(ROOT / "references" / "migrations"), str(root / "references" / "migrations"))
    return root


def run_build(root, *args):
    return subprocess.run([sys.executable, str(root / "dev/build.py"), *args], capture_output=True, text=True,
                          timeout=60, env=fixtures.git_env())


class BuildMatchesShipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = fixtures.load_build_module()

    def test_built_file_matches_sources(self):
        self.assertEqual(self.build.build().encode("utf-8"), (ROOT / "scripts/ledger.py").read_bytes(),
                         "scripts/ledger.py is stale: run python3 dev/build.py and commit it")

    # The same property under the names other reports used for it.
    test_generated_matches_sources = test_built_file_matches_sources
    test_page_matches_build = test_built_file_matches_sources

    def test_build_is_reproducible(self):
        a = hashlib.sha256(self.build.build().encode()).hexdigest()
        b = hashlib.sha256(self.build.build().encode()).hexdigest()
        self.assertEqual(a, b)

    def test_build_is_current(self):
        p = run_build(ROOT, "--check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_build_check_fails_after_source_edit(self):
        root = copy_tree()
        self.assertEqual(run_build(root, "--check").returncode, 0)
        page = root / "dev/page.html"
        page.write_text(page.read_text() + "<!-- edited -->\n")
        before = (root / "scripts/ledger.py").read_bytes()
        p = run_build(root, "--check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("scripts/ledger.py", p.stdout + p.stderr)
        self.assertIn("edited", p.stdout + p.stderr, "the diff summary should show the changed line")
        self.assertEqual((root / "scripts/ledger.py").read_bytes(), before, "--check must not write")
        p = run_build(root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(run_build(root, "--check").returncode, 0)

    def test_build_writes_through_a_temp_file(self):
        root = copy_tree()
        (root / "scripts/ledger.py").write_text("stale\n")
        self.assertEqual(run_build(root).returncode, 0)
        self.assertEqual((root / "scripts/ledger.py").read_text(encoding="utf-8"), self.build.build(ROOT))
        self.assertEqual(sorted(p.name for p in (root / "scripts").iterdir()), ["ledger.py"],
                         "no temporary file may be left behind")

    def test_build_help_does_not_write(self):
        root = copy_tree()
        target = root / "scripts/ledger.py"
        target.write_text("sentinel\n")
        os.utime(str(target), (1_000_000_000, 1_000_000_000))
        for args in (["--help"], ["-h"], ["--bogus"], ["extra-arg"]):
            p = run_build(root, *args)
            self.assertIn("usage", (p.stdout + p.stderr).lower(), args)
            self.assertEqual(p.returncode, 0 if args[0] in ("--help", "-h") else 2, args)
            self.assertEqual(target.read_text(), "sentinel\n", args)
            self.assertEqual(target.stat().st_mtime, 1_000_000_000, args)


class BuildGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = fixtures.load_build_module()

    def edited(self, rel, fn):
        root = copy_tree()
        p = root / rel
        p.write_text(fn(p.read_text()))
        return root

    def assertRejected(self, root, words):
        with self.assertRaises(self.build.BuildError) as cm:
            self.build.build(root)
        self.assertIn(words, str(cm.exception))
        before = (root / "scripts/ledger.py").read_bytes()
        p = run_build(root)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(words, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual((root / "scripts/ledger.py").read_bytes(), before, "a failed build must not write")

    def test_build_rejects_triple_quotes(self):
        self.assertRejected(self.edited("references/schema.sql", lambda t: t + '\n-- """\n'), "triple quotes")
        self.assertRejected(self.edited("dev/page.html", lambda t: t + "<!-- ''' \"\"\" -->\n"), "triple quotes")

    def test_build_rejects_trailing_backslash(self):
        self.assertRejected(self.edited("references/schema.sql", lambda t: t.rstrip("\n") + "\n-- \\"),
                            "must not end with")
        self.assertRejected(self.edited("dev/page.html", lambda t: t.rstrip("\n") + '"'), "must not end with")

    def test_build_rejects_missing_or_duplicate_placeholder(self):
        self.assertRejected(self.edited("dev/core.py", lambda t: t.replace("@@PAGE@@", "nothing")), "@@PAGE@@")
        self.assertRejected(self.edited("dev/core.py", lambda t: t + "\n# @@SCHEMA@@\n"), "@@SCHEMA@@")

    def test_build_rejects_placeholder_inside_payload(self):
        self.assertRejected(self.edited("dev/page.html", lambda t: t + "<!-- @@SCHEMA@@ -->\n"), "@@SCHEMA@@")

    def test_built_module_imports_and_schema_executes(self):
        mod = fixtures.load_ledger_module()
        self.assertNotIn("@@SCHEMA@@", mod.SCHEMA)
        self.assertNotIn("@@PAGE@@", mod.PAGE)
        self.assertEqual(mod.SCHEMA, (ROOT / "references/schema.sql").read_text())
        self.assertEqual(mod.PAGE, (ROOT / "dev/page.html").read_text())
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(mod.SCHEMA)
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(int(v), mod.SCHEMA_VERSION)
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
