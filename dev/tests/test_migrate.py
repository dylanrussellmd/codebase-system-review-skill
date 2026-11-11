"""The schema migration runner and `ledger.py migrate` (plan step 32).

Every ledger made before migrations existed is schema 1 (references/schema.sql at commit ea00196, frozen in
dev/tests/data/). The shipped tool migrates it to the shipped schema (M.SHIPPED); the runner, the backup, the refusals
and the rollback are also proven with a test-only schema one past it (M.NEXT) built from a copy of the sources
(dev/tests/_migrate_helpers.py): it rebuilds the themes table with the SQLite 12-step recipe.
"""
import os
import sqlite3
import stat
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import run_ledger


def init_fresh(ledger_py):
    d = fixtures.temp_dir("fresh-") / "proj"
    d.mkdir()
    p = run_ledger("--root", d, "init", ledger_py=ledger_py)
    assert p.returncode == 0, p.stdout + p.stderr
    return d / ".code-review" / "ledger.sqlite"


class FrozenV1(unittest.TestCase):
    def test_v1_fixture_is_frozen(self):
        self.assertEqual(M.sha256(M.V1_SCHEMA), M.V1_SCHEMA_SHA256, "dev/tests/data/schema-v1.sql must not change")
        _, db = M.restore_v1()
        self.assertEqual(M.objects(db), M.objects_of_sql(M.V1_SCHEMA.read_text(encoding="utf-8")))
        meta = M.meta(db)
        self.assertEqual(meta["schema_version"], "1")
        self.assertNotIn("created_by_skill", meta)
        c = M.counts(db)
        self.assertGreaterEqual(c["findings"], 5)
        self.assertGreaterEqual(c["finding_locations"], 5)
        self.assertGreater(c["events"], 5)


class Embedding(unittest.TestCase):
    def test_build_embeds_migrations(self):
        mod = fixtures.load_ledger_module()
        files = sorted((fixtures.REPO_ROOT / "references" / "migrations").glob("*.sql"))
        self.assertEqual([m[0] for m in mod.MIGRATIONS], list(range(1, len(files) + 1)))
        self.assertEqual([f"{m[0]:04d}-{m[1]}.sql" for m in mod.MIGRATIONS], [f.name for f in files])
        for m, f in zip(mod.MIGRATIONS, files):
            self.assertEqual(m[3], f.read_text(encoding="utf-8"))
        self.assertEqual(mod.SCHEMA_VERSION, mod.MIGRATIONS[-1][0])
        self.assertEqual(mod.MIGRATIONS[0][2], "initial ledger schema")
        v2 = fixtures.load_ledger_module(M.v2_ledger_py())
        self.assertEqual(v2.SCHEMA_VERSION, M.NEXT)
        self.assertEqual(v2.MIGRATIONS[-1][:3], (M.NEXT, "test-only", "test only: themes gains a description column"))

    def test_build_rejects_bad_migrations(self):
        build = fixtures.load_build_module()
        cases = (
            (f"{M.NEXT + 1:04d}-gap.sql", "-- a gap\nSELECT 1;\n", "numbered 0001"),
            (f"{M.NEXT:04d}-Bad_Name.sql", "-- bad name\n", "name"),
            (f"{M.NEXT:04d}-nodesc.sql", "SELECT 1;\n", "description"),
            (f"{M.NEXT:04d}-separator.sql", "-- has a separator\n-- migration: 0009-x.sql\n", "migration:"),
        )
        for name, text, words in cases:
            with self.subTest(name=name):
                root = M.copy_sources()
                (root / "references" / "migrations" / name).write_text(text)
                with self.assertRaises(build.BuildError) as cm:
                    build.build(root)
                self.assertIn(words, str(cm.exception))

    def test_split_sql(self):
        mod = fixtures.load_ledger_module()
        sql = ("-- head; with a semicolon\nCREATE TABLE a(x TEXT DEFAULT ';');\n"
               "CREATE TRIGGER t AFTER INSERT ON a BEGIN UPDATE a SET x = 'y;'; DELETE FROM a WHERE 0; END;\n"
               "INSERT INTO a VALUES ('1'); INSERT INTO a VALUES ('2');\n-- tail\n")
        got = mod.split_sql(sql)
        self.assertEqual(len(got), 4, got)
        self.assertTrue(got[0].startswith("-- head; with a semicolon\nCREATE TABLE a"), got[0])
        self.assertTrue(got[1].endswith("END;"), got[1])
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        for s in got:
            con.execute(s)
        self.assertEqual(con.execute("SELECT count(*) FROM a").fetchone()[0], 2)


class Migrate(unittest.TestCase):
    def assertMatchesFresh(self, db, ledger_py, before_counts):
        mod = fixtures.load_ledger_module(ledger_py)
        fresh = init_fresh(ledger_py)
        self.assertEqual(M.objects(db), M.objects(fresh), "migrated schema differs from a fresh init")
        after = M.counts(db)
        for table, n in before_counts.items():
            if table not in ("meta", "schema_migrations"):  # meta gains written_by_tool etc.; one row per migration
                self.assertEqual(after[table], n, f"rows of {table} changed")
        con = sqlite3.connect(str(db))
        try:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], mod.SCHEMA_VERSION)
            self.assertEqual([r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")],
                             list(range(1, mod.SCHEMA_VERSION + 1)))
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            con.close()
        meta = M.meta(db)
        self.assertEqual(meta["schema_version"], str(mod.SCHEMA_VERSION))
        self.assertTrue(meta.get("created_by_skill"))
        self.assertEqual(meta["written_by_tool"], mod.TOOL_VERSION)
        self.assertEqual(meta["schema_hash"], mod.shipped_schema_hash())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_migrate_backup_is_private(self):
        """The .bak-v<N> copy holds the whole ledger, so it is 0600 like the ledger, and init makes an older,
        world-readable backup private too."""
        project, db = M.restore_v1()
        p = run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        backup = db.with_name("ledger.sqlite.bak-v1")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        os.chmod(str(backup), 0o644)
        p = run_ledger("--root", project, "init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_migrate_v1_to_current(self):
        """test_upgrade_from_every_version / test_migrate_v1_to_v2_matches_fresh: the frozen v1 ledger, migrated by
        the shipped tool and by the test-only tool one schema past it, equals a fresh init of that tool, keeps every
        row, passes check, and records the migrations."""
        for ledger_py in (fixtures.built_ledger_path(), M.v2_ledger_py()):
            mod = fixtures.load_ledger_module(ledger_py)
            with self.subTest(schema=mod.SCHEMA_VERSION):
                project, db = M.restore_v1()
                before = M.counts(db)
                meta_before = M.meta(db)
                original = db.read_bytes()
                p = run_ledger("--root", project, "migrate", ledger_py=ledger_py)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertMatchesFresh(db, ledger_py, before)
                meta_after = M.meta(db)
                for k, v in meta_before.items():
                    if k != "schema_version":
                        self.assertEqual(meta_after[k], v, k)
                self.assertEqual(meta_after["created_by_skill"], "unknown (made before ledger.py had versions)")
                backup = db.with_name("ledger.sqlite.bak-v1")
                if mod.SCHEMA_VERSION == 1:
                    self.assertIn("already schema 1; nothing to migrate", p.stdout)
                    self.assertFalse(backup.exists())
                else:
                    self.assertIn(f"migrated {db} from schema 1 to {mod.SCHEMA_VERSION}", p.stdout)
                    self.assertIn(str(backup), p.stdout)
                    self.assertTrue(backup.exists())
                    self.assertEqual(M.counts(backup), before)
                    self.assertEqual(M.meta(backup)["schema_version"], "1")
                    self.assertNotEqual(backup.read_bytes(), b"")
                    if mod.SCHEMA_VERSION == M.NEXT:
                        con = sqlite3.connect(str(db))
                        self.assertEqual(con.execute("SELECT count(*) FROM themes WHERE description IS NULL")
                                         .fetchone()[0], 0)
                        con.close()
                    self.assertNotEqual(db.read_bytes(), original)
                    # A second run has nothing to do and keeps the first backup.
                    p = run_ledger("--root", project, "migrate", ledger_py=ledger_py)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertIn("nothing to migrate", p.stdout)
                    self.assertEqual(M.counts(backup), before)
                p = run_ledger("--root", project, "check", ledger_py=ledger_py)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                # The project's copy of the tool is untouched by migrate (init refreshes it).
                self.assertFalse((project / ".code-review" / "ledger.py").exists())

    def test_migrate_twice_makes_a_new_backup_name(self):
        project, db = M.restore_v1()
        first = db.with_name("ledger.sqlite.bak-v1")
        first.write_bytes(b"an older backup")
        p = run_ledger("--root", project, "migrate", ledger_py=M.v2_ledger_py())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(first.read_bytes(), b"an older backup", "an existing backup must never be overwritten")
        self.assertTrue(db.with_name("ledger.sqlite.bak-v1.2").exists())

    def test_migrate_refuses_newer_db(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        con.commit()
        con.close()
        before = db.read_bytes()
        for ledger_py in (fixtures.built_ledger_path(), M.v2_ledger_py()):
            p = run_ledger("--root", project, "migrate", ledger_py=ledger_py)
            self.assertEqual(p.returncode, 1)
            self.assertIn("is schema 99; this ledger.py only knows", p.stderr)
            self.assertIn("Use a newer ledger.py", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(sorted(x.name for x in db.parent.iterdir()), ["ledger.sqlite"])

    def test_migrate_refuses_below_baseline(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
        con.commit()
        con.close()
        before = db.read_bytes()
        p = run_ledger("--root", project, "migrate", ledger_py=M.v2_ledger_py())
        self.assertEqual(p.returncode, 1)
        self.assertIn("is schema 0, older than any schema this ledger.py can migrate", p.stderr)
        self.assertEqual(db.read_bytes(), before)

    def test_migrate_rolls_back_on_failure(self):
        """A failing step, or one that leaves a broken foreign key or a schema unlike the shipped one, leaves the
        ledger byte-identical, removes the backup it made and exits 1 naming the migration."""
        failing = M.REBUILD_THEMES + "INSERT INTO no_such_table VALUES (1);\n"
        cases = (
            ("sql error", M.variant(migration=failing, schema_edit=M.schema_v2), "no such table: no_such_table"),
            ("foreign key", M.variant(
                migration=M.REBUILD_THEMES + "UPDATE issues SET theme_code = 'no-such-theme';\n",
                schema_edit=M.schema_v2), "foreign_key_check"),
            ("schema differs", M.variant(migration=M.REBUILD_THEMES + "CREATE TABLE stray(x);\n",
                                         schema_edit=M.schema_v2), "extra table stray"),
        )
        for label, ledger_py, words in cases:
            with self.subTest(label):
                project, db = M.restore_v1()
                before = db.read_bytes()
                p = run_ledger("--root", project, "migrate", ledger_py=ledger_py)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn(M.TEST_MIGRATION, p.stderr)
                self.assertIn(words, p.stderr)
                self.assertIn("nothing was changed", p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(db.read_bytes(), before, "a failed migration must leave the ledger byte-identical")
                self.assertEqual(sorted(x.name for x in db.parent.iterdir()), ["ledger.sqlite"])

    def test_migrate_restores_tampered_views_and_triggers(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("DROP TRIGGER tr_issue_opened")
        con.execute("CREATE TRIGGER tr_forged AFTER INSERT ON gates BEGIN UPDATE gates SET status='answered'; END")
        con.commit()
        con.close()
        for ledger_py in (fixtures.built_ledger_path(), M.v2_ledger_py()):
            with self.subTest(str(ledger_py)):
                p = run_ledger("--root", project, "migrate", ledger_py=ledger_py)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        names = {n for _, n in M.objects(db)}
        self.assertIn("tr_issue_opened", names)
        self.assertNotIn("tr_forged", names)

    def test_commands_refuse_writes_until_migrated(self):
        project, db = M.restore_v1()
        v2 = M.v2_ledger_py()
        before = db.read_bytes()
        p = run_ledger("--root", project, "init", ledger_py=v2)
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"is schema 1; this ledger.py is schema {M.NEXT}. Run: python3 ", p.stderr)
        self.assertIn("ledger.py migrate", p.stderr)
        self.assertEqual(db.read_bytes(), before)
        p = run_ledger("--root", project, "migrate", ledger_py=v2)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run_ledger("--root", project, "init", ledger_py=v2)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"already exists (schema {M.NEXT})", p.stdout)


if __name__ == "__main__":
    unittest.main()
