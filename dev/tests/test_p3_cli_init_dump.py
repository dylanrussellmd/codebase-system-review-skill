"""Atomic, recoverable init and dump, and a custom --db that leaves project files alone (plan P-0001 steps 48, 49
and 50).

init copies ledger.py first (via a temp file and os.replace), builds the schema in ledger.sqlite.tmp inside one
transaction and renames it into place, refuses an incomplete or foreign ledger with one line, and repairs a damaged
.gitignore. dump reads one snapshot (a read transaction), writes <db>.sql.tmp, fsyncs and renames it, and refuses to
overwrite a .sql file it did not write. With --db outside a .code-review folder, init copies no ledger.py and writes
no .gitignore there.
"""
import contextlib
import io
import sqlite3
import subprocess
import sys
import threading
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, built_ledger_path, run_ledger, seed_demo

GITIGNORE = fixtures.load_ledger_module().GITIGNORE


def fresh_project(name="proj"):
    d = fixtures.temp_dir("p3init-") / name
    d.mkdir()
    return d


def limited(kb, *args):
    """Run ledger.py under `ulimit -f kb` (bash: 1024-byte blocks): any file write past kb KiB fails (EFBIG)."""
    cmd = " ".join(["ulimit -f %d;" % kb, "exec", sys.executable, str(built_ledger_path())]
                   + ["'%s'" % str(a) for a in args])
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=fixtures.git_env(), timeout=60)


def shipped_names():
    mod = fixtures.load_ledger_module()
    return sorted((t, n) for t, n, _, _ in mod.shipped_objects())


def ledger_names(db):
    con = sqlite3.connect(str(db))
    try:
        return sorted(con.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                                  "AND sql IS NOT NULL").fetchall())
    finally:
        con.close()


class AtomicInit(unittest.TestCase):

    def test_init_interrupted_is_recoverable(self):
        mod = fixtures.load_ledger_module()
        # 40 KiB: the copy of ledger.py fails; 300 KiB: the copy fits but the ledger (about 400 KiB) does not.
        for kb in (40, 300):
            with self.subTest(limit_kib=kb):
                d = fresh_project()
                p = limited(kb, "--root", d, "init")
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertFalse((d / ".code-review" / "ledger.sqlite").exists(), "a half-built ledger was left")
                p = run_ledger("--root", d, "init")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                db = d / ".code-review" / "ledger.sqlite"
                self.assertEqual(M.meta(db)["schema_version"], str(mod.SCHEMA_VERSION))
                self.assertEqual(ledger_names(db), shipped_names())
                self.assertEqual((d / ".code-review" / "ledger.py").read_bytes(), built_ledger_path().read_bytes())
                self.assertEqual(sorted(x.name for x in (d / ".code-review").iterdir()),
                                 [".gitignore", "ledger.py", "ledger.sqlite"])

    def test_init_atomic(self):
        mod = fixtures.load_ledger_module(fresh=True)
        d = fresh_project()
        db = d / ".code-review" / "ledger.sqlite"
        good = mod.SCHEMA
        mod.SCHEMA = good + "\nINSERT INTO no_such_table VALUES (1);\n"
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                mod.init(d, db)
        finally:
            mod.SCHEMA = good
        self.assertIn("no_such_table", str(cm.exception.code))
        self.assertFalse(db.exists())
        self.assertFalse(db.with_name("ledger.sqlite.tmp").exists())
        with contextlib.redirect_stdout(io.StringIO()):
            mod.init(d, db)
        self.assertEqual(ledger_names(db), shipped_names())

    def test_init_on_empty_file(self):
        d = fresh_project()
        (d / ".code-review").mkdir()
        db = d / ".code-review" / "ledger.sqlite"
        db.write_bytes(b"")
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip().count("\n"), 0, p.stderr)
        self.assertIn(f"incomplete or foreign ledger at {db}", p.stderr)
        self.assertIn("move it aside and run init again", p.stderr)
        self.assertEqual(db.read_bytes(), b"")

    def test_init_missing_version_row_clean_error(self):
        d = fresh_project()
        self.assertEqual(run_ledger("--root", d, "init").returncode, 0)
        db = d / ".code-review" / "ledger.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("DELETE FROM meta WHERE key='schema_version'")
        con.commit()
        con.close()
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(f"incomplete or foreign ledger at {db}", p.stderr)
        (d / "x.sqlite").write_bytes(b"not sqlite at all" * 100)
        p = run_ledger("--root", d, "--db", d / "x.sqlite", "init")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("incomplete or foreign ledger at", p.stderr)

    def test_init_repairs_gitignore(self):
        d = fresh_project()
        self.assertEqual(run_ledger("--root", d, "init").returncode, 0)
        gi = d / ".code-review" / ".gitignore"
        for damaged in ("", "ledger.sqlite-journal\nledger.sql", "ledger.sqlite-journal\nledger.sqlite-wal\n"):
            with self.subTest(damaged=damaged):
                gi.write_text(damaged)
                p = run_ledger("--root", d, "init")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(gi.read_text(), GITIGNORE)
                self.assertIn(f"repaired {gi}", p.stdout)
                self.assertIn("ledger.sqlite-wal", gi.read_text())
        # Lines of the owner's own are kept.
        gi.write_text("my-notes.txt\n")
        run_ledger("--root", d, "init")
        self.assertEqual(gi.read_text(), "my-notes.txt\n" + GITIGNORE)

    def test_init_is_idempotent(self):
        d = fresh_project()
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        db = d / ".code-review" / "ledger.sqlite"
        self.assertIn(f"ledger at {db} (created)", p.stdout)
        con = sqlite3.connect(str(db))
        con.execute("INSERT INTO actors(kind, name) VALUES ('owner', 'kept')")
        con.commit()
        con.close()
        before = M.counts(db)
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"ledger at {db} (attached to existing)", p.stdout)
        self.assertIn("already exists", p.stdout)
        self.assertEqual(M.counts(db), before)
        self.assertEqual((d / ".code-review" / "ledger.py").read_bytes(), built_ledger_path().read_bytes())
        self.assertEqual(sorted(x.name for x in (d / ".code-review").iterdir()),
                         [".gitignore", "ledger.py", "ledger.sqlite"])


class AtomicDump(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def sql(self):
        return self.L.dir / "ledger.sql"

    def test_dump_is_deterministic(self):
        self.L.run_ok("dump")
        first = self.sql().read_bytes()
        self.L.run_ok("dump")
        self.assertEqual(self.sql().read_bytes(), first)
        mod = self.L.module
        self.assertTrue(first.decode("utf-8").startswith(mod.DUMP_HEADER + "\n"))

    def test_dump_round_trips(self):
        self.L.run_ok("dump")
        con = sqlite3.connect(":memory:")
        con.executescript(self.sql().read_text(encoding="utf-8"))
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        src = sqlite3.connect(str(self.L.db))
        for (t,) in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            with self.subTest(table=t):
                q = f'SELECT * FROM "{t}" ORDER BY 1'
                self.assertEqual(con.execute(q).fetchall(), src.execute(q).fetchall())
        src.close()
        con.close()

    def test_dump_failure_keeps_old_file(self):
        self.L.run_ok("dump")
        before = self.sql().read_bytes()
        self.assertGreater(len(before), 40 * 1024)
        self.L.x("INSERT INTO actors(kind, name) VALUES ('owner', 'after the dump')")
        self.L.commit()
        p = limited(20, "--root", self.L.root, "dump")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("dump: could not write", p.stderr)
        self.assertEqual(self.sql().read_bytes(), before)
        self.assertFalse(self.sql().with_name("ledger.sql.tmp").exists())

    def test_dump_consistent_under_writer(self):
        L = Ledger()
        self.addCleanup(L.close)
        rid = L.review("R-0001")
        L.commit()
        stop = threading.Event()

        def writer():
            con = L.connect()
            n = 0
            while not stop.is_set() and n < 2000:
                cur = con.execute("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, summary) "
                                  "VALUES (?, ?, 'M', 'defect', 'spec', 's')", (rid, f"W-{n}"))
                con.execute("INSERT INTO finding_locations(finding_id, repository_id, path) VALUES (?, ?, 'a.py')",
                            (cur.lastrowid, L.default_repository()))
                con.commit()
                n += 1
            con.close()

        L.default_repository()
        L.commit()
        t = threading.Thread(target=writer)
        t.start()
        try:
            for _ in range(3):
                con = L.module.connect(L.db)
                with contextlib.redirect_stdout(io.StringIO()):
                    L.module.dump(con, L.db)
                con.close()
                mem = sqlite3.connect(":memory:")
                mem.executescript((L.dir / "ledger.sql").read_text(encoding="utf-8"))
                self.assertEqual(mem.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(mem.execute("SELECT count(*) FROM findings WHERE point_id GLOB 'W-*'").fetchone(),
                                 mem.execute("SELECT count(*) FROM finding_locations").fetchone())
                mem.close()
        finally:
            stop.set()
            t.join()

    def test_dump_refuses_foreign_sql(self):
        d = fresh_project()
        self.assertEqual(run_ledger("--root", d, "--db", d / "data.sqlite", "init").returncode, 0)
        foreign = d / "data.sql"
        foreign.write_text("CREATE TABLE important(x);\n")
        p = run_ledger("--root", d, "--db", d / "data.sqlite", "dump")
        self.assertEqual(p.returncode, 1)
        self.assertIn("was not written by ledger.py dump", p.stderr)
        self.assertEqual(foreign.read_text(), "CREATE TABLE important(x);\n")
        # A dump made by an older ledger.py (it started with iterdump's own first line) is still replaced.
        foreign.write_text("BEGIN TRANSACTION;\nCOMMIT;\n")
        p = run_ledger("--root", d, "--db", d / "data.sqlite", "dump")
        self.assertEqual(p.returncode, 0, p.stderr)


class CustomDb(unittest.TestCase):

    def test_init_custom_db_keeps_project_files(self):
        d = fresh_project()
        (d / "src").mkdir()
        mine = "print('my own ledger.py')\n"
        (d / "src" / "ledger.py").write_text(mine)
        p = run_ledger("--root", d, "--db", d / "src" / "x.sqlite", "init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((d / "src" / "ledger.py").read_text(), mine)
        self.assertFalse((d / "src" / ".gitignore").exists())
        self.assertNotIn("copied ledger.py", p.stdout)

    def test_db_override(self):
        d = fresh_project()
        db = d / "elsewhere" / "my.sqlite"
        p = run_ledger("--root", d, "--db", db, "init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(db.exists())
        self.assertEqual(sorted(x.name for x in db.parent.iterdir()), ["my.sqlite"])
        self.assertFalse((d / ".code-review").exists())
        p = run_ledger("--root", d, "--db", db, "check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # A --db inside a .code-review folder still gets the copy and the .gitignore.
        db2 = d / ".code-review" / "ledger.sqlite"
        p = run_ledger("--root", d, "--db", db2, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(x.name for x in db2.parent.iterdir()), [".gitignore", "ledger.py", "ledger.sqlite"])


if __name__ == "__main__":
    unittest.main()
