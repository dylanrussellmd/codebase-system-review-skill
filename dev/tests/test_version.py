"""The tool version (plan step 31): one version, in SKILL.md's frontmatter, stamped into ledger.py by the build with
a content hash; --version; the schema version's copies agree; init never silently downgrades a project's ledger.py."""
import hashlib
import re
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import built_ledger_path, run_ledger

SKILL_MD = fixtures.REPO_ROOT / "SKILL.md"


def skill_version():
    front = SKILL_MD.read_text(encoding="utf-8").split("---\n")[1]
    return re.search(r"(?m)^metadata:\n(?:[ \t]+.*\n)*?[ \t]+version: (\S+)$", front).group(1)


def new_project():
    d = fixtures.temp_dir("ver-") / "proj"
    d.mkdir()
    return d


_variants = {}


def tool(version):
    """A ledger.py identical to the shipped one except for its version (built once per version)."""
    if version not in _variants:
        _variants[version] = M.variant(version=version)
    return _variants[version]


class Stamp(unittest.TestCase):
    def test_version_flag(self):
        mod = fixtures.load_ledger_module()
        p = run_ledger("--version", cwd=fixtures.temp_dir("v-"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, f"ledger.py {skill_version()} (schema {mod.SCHEMA_VERSION})\n")
        self.assertEqual(mod.TOOL_VERSION, skill_version())
        self.assertRegex(mod.TOOL_VERSION, r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")

    def test_built_file_stamps_tool_hash(self):
        mod = fixtures.load_ledger_module()
        self.assertRegex(mod.TOOL_HASH, r"^[0-9a-f]{64}$")
        text = built_ledger_path().read_text(encoding="utf-8")
        self.assertEqual(text.count(mod.TOOL_HASH), 1)
        body = text.replace(mod.TOOL_HASH, "@@" + "TOOL_HASH@@")
        self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(), mod.TOOL_HASH)
        self.assertNotEqual(fixtures.load_ledger_module(tool("9.0.0")).TOOL_HASH, mod.TOOL_HASH)

    def test_schema_version_copies_agree(self):
        """SCHEMA_VERSION (the highest migration), schema.sql's header, its meta seed and its schema_migrations seed
        must agree, or the build fails naming the copy that differs."""
        mod = fixtures.load_ledger_module()
        con = sqlite3.connect(":memory:")
        con.executescript(mod.SCHEMA)
        self.assertEqual(int(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]),
                         mod.SCHEMA_VERSION)
        self.assertEqual(con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], mod.SCHEMA_VERSION)
        con.close()
        self.assertIn(f"schema version {mod.SCHEMA_VERSION}\n", mod.SCHEMA.split("\n", 2)[1] + "\n")
        build = fixtures.load_build_module()
        edits = (
            ("header", lambda t: t.replace(f"— schema version {M.SHIPPED}", f"— schema version {M.NEXT + 1}", 1),
             "header"),
            ("meta seed", lambda t: t.replace(f"('schema_version','{M.SHIPPED}')", f"('schema_version','{M.NEXT}')"),
             "meta"),
            ("migrations seed", lambda t: t.replace("(1, 'initial ledger schema')", "(1, 'something else')"),
             "schema_migrations"),
            ("extra seed row", lambda t: t[:t.index("INSERT INTO schema_migrations")]
             + re.sub(r"'\);", f"'), ({M.NEXT}, 'x');", t[t.index("INSERT INTO schema_migrations"):], count=1),
             "schema_migrations"),
        )
        for label, fn, words in edits:
            with self.subTest(label):
                root = M.copy_sources()
                p = root / "references" / "schema.sql"
                p.write_text(fn(p.read_text(encoding="utf-8")), encoding="utf-8")
                with self.assertRaises(build.BuildError) as cm:
                    build.build(root)
                self.assertIn(words, str(cm.exception))
        with self.subTest("a migration without the schema change"):
            root = M.copy_sources()
            (root / "references" / "migrations" / f"{M.NEXT:04d}-x.sql").write_text("-- x\nSELECT 1;\n")
            with self.assertRaises(build.BuildError) as cm:
                build.build(root)
            self.assertIn("schema", str(cm.exception))

    def test_build_requires_a_skill_version(self):
        build = fixtures.load_build_module()
        cases = {"  version: banana\n": "version", "": "metadata", "version: 1.9.2\n": "metadata"}
        for bad, word in cases.items():
            with self.subTest(bad=bad):
                root = M.copy_sources()
                p = root / "SKILL.md"
                pattern = r"(?m)^metadata:\n  version: .*\n" if bad.startswith("version") else r"(?m)^  version: .*\n"
                text, n = re.subn(pattern, bad, p.read_text(encoding="utf-8"), count=1)
                self.assertEqual(n, 1, "SKILL.md has metadata: then '  version:'")
                p.write_text(text)
                with self.assertRaises(build.BuildError) as cm:
                    build.build(root)
                self.assertIn("version", str(cm.exception))
                self.assertIn(word, str(cm.exception))


class InitToolCopy(unittest.TestCase):
    """init compares TOOL_VERSION (read as text, never run) with the project's .code-review/ledger.py."""

    def setUp(self):
        self.d = new_project()
        self.home = self.d / ".code-review" / "ledger.py"
        self.bak = self.d / ".code-review" / "ledger.py.bak"

    def init(self, ledger_py, *extra):
        return run_ledger("--root", self.d, "init", *extra, ledger_py=ledger_py)

    def test_init_never_downgrades_project_copy(self):
        """test_init_does_not_replace_newer_project_copy / test_init_keeps_newer_project_copy /
        test_init_refuses_downgrade: a newer project copy is byte-identical afterwards and init exits 1."""
        newer = tool("9.0.0")
        self.assertEqual(self.init(newer).returncode, 0)
        before = self.home.read_bytes()
        db_before = (self.d / ".code-review" / "ledger.sqlite").read_bytes()
        p = self.init(built_ledger_path())
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        mine = fixtures.load_ledger_module().TOOL_VERSION
        self.assertIn(f"is ledger.py 9.0.0, newer than this copy ({mine})", p.stderr)
        self.assertIn("--force", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.home.read_bytes(), before)
        self.assertFalse(self.bak.exists())
        self.assertEqual((self.d / ".code-review" / "ledger.sqlite").read_bytes(), db_before,
                         "a refused init must not touch the ledger")

    def test_init_refuses_downgrade_before_creating_a_ledger(self):
        (self.d / ".code-review").mkdir()
        self.home.write_bytes(tool("9.0.0").read_bytes())
        p = self.init(built_ledger_path())
        self.assertEqual(p.returncode, 1)
        self.assertFalse((self.d / ".code-review" / "ledger.sqlite").exists())

    def test_init_upgrades_older_copy(self):
        older = tool("0.1.0")
        self.assertEqual(self.init(older).returncode, 0)
        old = self.home.read_bytes()
        p = self.init(built_ledger_path())
        self.assertEqual(p.returncode, 0, p.stderr)
        mine = fixtures.load_ledger_module().TOOL_VERSION
        self.assertIn(f"note: replacing {self.home} (0.1.0) with this copy ({mine}); the old copy is kept as "
                      f"{self.bak}", p.stdout)
        self.assertEqual(self.home.read_bytes(), built_ledger_path().read_bytes())
        self.assertEqual(self.bak.read_bytes(), old)
        self.assertEqual(M.meta(self.d / ".code-review" / "ledger.sqlite")["written_by_tool"], mine)
        self.assertEqual(M.meta(self.d / ".code-review" / "ledger.sqlite")["created_by_skill"], "0.1.0")

    def test_init_upgrades_unversioned_copy(self):
        """A ledger.py from before versions (no TOOL_VERSION, like commit ea00196's) counts as older."""
        self.assertEqual(self.init(built_ledger_path()).returncode, 0)
        self.home.write_text("#!/usr/bin/env python3\nSCHEMA_VERSION = 1\n")
        p = self.init(built_ledger_path())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"note: replacing {self.home} (no version) with this copy", p.stdout)
        self.assertEqual(self.home.read_bytes(), built_ledger_path().read_bytes())
        self.assertEqual(self.bak.read_text(), "#!/usr/bin/env python3\nSCHEMA_VERSION = 1\n")

    def test_init_force_downgrades(self):
        newer = tool("9.0.0")
        self.assertEqual(self.init(newer).returncode, 0)
        before = self.home.read_bytes()
        p = self.init(built_ledger_path(), "--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--force", p.stdout)
        self.assertEqual(self.home.read_bytes(), built_ledger_path().read_bytes())
        self.assertEqual(self.bak.read_bytes(), before)

    def test_init_same_version_other_contents_warns(self):
        self.assertEqual(self.init(built_ledger_path()).returncode, 0)
        edited = self.home.read_text(encoding="utf-8") + "\n# a local edit\n"
        self.home.write_text(edited, encoding="utf-8")
        p = self.init(built_ledger_path())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("warning:", p.stdout + p.stderr)
        self.assertIn("contents differ", p.stdout + p.stderr)
        self.assertEqual(self.home.read_text(encoding="utf-8"), edited)
        p = self.init(built_ledger_path(), "--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.home.read_bytes(), built_ledger_path().read_bytes())

    def test_init_never_runs_the_project_copy(self):
        """The project copy is read as text: a copy whose code would write a marker when run is never executed."""
        (self.d / ".code-review").mkdir()
        marker = self.d / "ran.txt"
        self.home.write_text(f'TOOL_VERSION = "0.0.1"\nopen({str(marker)!r}, "w").write("ran")\n')
        p = self.init(built_ledger_path())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(marker.exists())

    def test_init_records_tool_version_in_meta(self):
        mod = fixtures.load_ledger_module()
        self.assertEqual(self.init(built_ledger_path()).returncode, 0)
        db = self.d / ".code-review" / "ledger.sqlite"
        meta = M.meta(db)
        self.assertEqual(meta["created_by_skill"], mod.TOOL_VERSION)
        self.assertEqual(meta["written_by_tool"], mod.TOOL_VERSION)
        self.assertEqual(meta["schema_hash"], mod.shipped_schema_hash())
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], mod.SCHEMA_VERSION)
        con.close()
        # A later init by a newer tool records itself as the writer and keeps the creator.
        self.assertEqual(self.init(tool("9.0.0")).returncode, 0)
        meta = M.meta(db)
        self.assertEqual((meta["created_by_skill"], meta["written_by_tool"]), (mod.TOOL_VERSION, "9.0.0"))


if __name__ == "__main__":
    unittest.main()
