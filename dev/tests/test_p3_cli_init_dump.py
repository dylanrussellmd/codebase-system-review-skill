"""Atomic, recoverable init and dump, and a custom --db that leaves project files alone (plan P-0001 steps 48, 49
and 50).

init copies ledger.py first (via a temp file and os.replace), builds the schema in a temp file of its own inside
one transaction and links it into place, refuses an incomplete or foreign ledger with one line, and repairs a damaged
.gitignore. dump reads one snapshot (a read transaction), writes a temp file of its own, fsyncs and renames it, and refuses to
overwrite a .sql file it did not write. With --db outside a .code-review folder, init copies no ledger.py and writes
no .gitignore there.
"""
import contextlib
import io
import os
import stat
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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
                                 [".gitattributes", ".gitignore", "ledger.py", "ledger.sqlite"])

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
        self.assertEqual([x.name for x in db.parent.glob("ledger.sqlite*")], [])
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

    def test_gitignore_covers_temp_names(self):
        """Every temp name private_temp, build_ledger and write_launch_file make in .code-review, with SQLite's side
        files, matches a line of GITIGNORE; repaired_gitignore drops the old fixed temp names."""
        import fnmatch
        mod = fixtures.load_ledger_module(fresh=True)
        patterns = [l for l in GITIGNORE.splitlines() if l and not l.startswith("#")]
        folder = fresh_project() / ".code-review"
        folder.mkdir()
        made = []
        mkstemp = mod.tempfile.mkstemp

        def recorded(*a, **kw):
            fd, name = mkstemp(*a, **kw)
            made.append(Path(name).name)
            return fd, name

        with mock.patch.object(mod.tempfile, "mkstemp", recorded):
            for name in ("ledger.sqlite", "ledger.py", "ledger.sql", "ledger.py.bak", ".gitignore", ".gitattributes"):
                fd, _ = mod.private_temp(folder / name)
                os.close(fd)
            self.assertTrue(mod.build_ledger(folder.parent, folder / "ledger.sqlite"))
            mod.write_launch_file(folder / "ledger.sqlite", "http://127.0.0.1:1/#k=x")
        self.assertEqual(len(made), 8)
        names = made + [n + s for n in made if n.endswith(".tmp") for s in ("-journal", "-wal", "-shm")]
        for n in names:
            with self.subTest(name=n):
                self.assertTrue(any(fnmatch.fnmatch(n, p) for p in patterns), n)
        old = "ledger.sqlite.tmp\nledger.py.tmp\nledger.sql.tmp\n"
        new, _ = mod.repaired_gitignore("my-notes.txt\n" + old)
        self.assertEqual(new, "my-notes.txt\n" + GITIGNORE)

    def test_concurrent_inits_build_one_ledger(self):
        """Two inits on a new project, each held after building its schema until the other has built its own."""
        mod = fixtures.load_ledger_module(fresh=True)
        d = fresh_project()
        db = d / ".code-review" / "ledger.sqlite"
        both = threading.Barrier(2, timeout=10)
        held = set()
        stamp = mod.stamp_meta

        def held_stamp(con, **kw):
            me = threading.get_ident()
            if me not in held:
                held.add(me)
                try:
                    both.wait()
                except threading.BrokenBarrierError:
                    pass
            return stamp(con, **kw)

        codes = []

        def run():
            try:
                mod.init(d, db)
                codes.append(0)
            except SystemExit as e:
                codes.append(e.code)

        out = io.StringIO()
        with mock.patch.object(mod, "stamp_meta", held_stamp), contextlib.redirect_stdout(out):
            runs = [threading.Thread(target=run) for _ in range(2)]
            for t in runs:
                t.start()
            for t in runs:
                t.join(60)
        self.assertEqual(codes, [0, 0], out.getvalue())
        self.assertEqual(out.getvalue().count("(created)"), 1, out.getvalue())
        self.assertEqual(out.getvalue().count("(attached to existing)"), 1, out.getvalue())
        self.assertEqual(M.meta(db)["schema_version"], str(mod.SCHEMA_VERSION))
        self.assertEqual(ledger_names(db), shipped_names())
        self.assertEqual(sorted(x.name for x in (d / ".code-review").iterdir()),
                         [".gitattributes", ".gitignore", "ledger.py", "ledger.sqlite"])

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
                         [".gitattributes", ".gitignore", "ledger.py", "ledger.sqlite"])


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
                if t == "meta":  # the record of the last dump (record_dump) is written after it, not into it
                    q = "SELECT * FROM meta WHERE key NOT LIKE 'last_dump_%' ORDER BY 1"
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
        self.assertEqual([f.name for f in self.sql().parent.iterdir() if f.name.startswith("ledger.sql.")], [])

    def test_dump_consistent_under_writer(self):
        L = Ledger()
        self.addCleanup(L.close)
        rid = L.review("R-0001")
        L.commit()
        stop = threading.Event()
        commits, errors = [0], []

        def writer():
            con = L.connect()
            try:
                n = 0
                while not stop.is_set() and n < 100000:
                    cur = con.execute("INSERT INTO findings(review_id, point_id, severity, kind, domain_code, sub, "
                                      "summary) VALUES (?, ?, 'M', 'defect', 'spec', ?, 's')", (rid, f"W-{n}", f"w-{n}"))
                    con.execute("INSERT INTO finding_locations(finding_id, repository_id, path) VALUES (?, ?, 'a.py')",
                                (cur.lastrowid, repo))
                    con.commit()
                    n += 1
                    commits[0] = n
                    time.sleep(0.005)   # a steady writer, not a flood: the ledger stays small
            except Exception as e:  # noqa: BLE001  the test fails with it below
                errors.append(e)
            finally:
                con.close()

        repo = L.default_repository()
        L.review_repository(rid, fixtures.DEFAULT_COMMIT, repository_id=repo)
        L.commit()
        t = threading.Thread(target=writer)
        t.start()
        real_fsync = os.fsync

        def fsync_after_a_commit(fd):
            """The dump's snapshot is open here: wait for the writer to commit once more before the file is done.
            The folder's fsync after the rename passes straight through."""
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                return real_fsync(fd)
            want = commits[0] + 1
            deadline = time.monotonic() + 10
            while commits[0] < want and not errors and time.monotonic() < deadline:
                time.sleep(0.002)
            during.append(commits[0] >= want)
            return real_fsync(fd)

        try:
            for _ in range(3):
                during = []
                con = L.module.connect(L.db)
                with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(os, "fsync", fsync_after_a_commit):
                    L.module.dump(con, L.db)
                con.close()
                self.assertEqual(errors, [], "the writer thread failed")
                self.assertEqual(during, [True], "the writer made no commit while the dump ran")
                mem = sqlite3.connect(":memory:")
                mem.executescript((L.dir / "ledger.sql").read_text(encoding="utf-8"))
                self.assertEqual(mem.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(mem.execute("SELECT count(*) FROM findings WHERE point_id GLOB 'W-*'").fetchone(),
                                 mem.execute("SELECT count(*) FROM finding_locations").fetchone())
                mem.close()
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [], "the writer thread failed")

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
        self.assertEqual(sorted(x.name for x in db2.parent.iterdir()),
                         [".gitattributes", ".gitignore", "ledger.py", "ledger.sqlite"])


if __name__ == "__main__":
    unittest.main()
