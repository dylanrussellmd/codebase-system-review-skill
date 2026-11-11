"""Characterization tests for the small core helpers: rows/one, repo_dir, file_at, location and api()."""
import sqlite3
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._core_helpers import NUMBERED, PERMALINK, project, ref


def mod():
    return fixtures.load_ledger_module()


class RowsAndOne(unittest.TestCase):
    def test_rows_and_one_shapes(self):
        m = mod()
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        self.assertEqual(m.rows(con, "SELECT * FROM t"), [])
        self.assertIsNone(m.one(con, "SELECT * FROM t WHERE id=?", (1,)))
        con.executemany("INSERT INTO t(name) VALUES (?)", [("a",), ("b",)])
        got = m.rows(con, "SELECT id, name AS label FROM t ORDER BY id")
        self.assertEqual(got, [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}])
        self.assertTrue(all(type(r) is dict for r in got))
        self.assertEqual(m.one(con, "SELECT name FROM t WHERE id=?", (2,)), {"name": "b"})
        # one() takes the first row when there are several.
        self.assertEqual(m.one(con, "SELECT id FROM t ORDER BY id DESC"), {"id": 2})
        con.close()


class RepoDir(unittest.TestCase):
    def test_repo_dir(self):
        m = mod()
        root = fixtures.temp_dir("root-")
        self.assertEqual(m.repo_dir(root, None), root.resolve())
        self.assertEqual(m.repo_dir(root, ""), root.resolve())
        self.assertEqual(m.repo_dir(root, "."), root.resolve())
        self.assertEqual(m.repo_dir(root, "sub"), (root / "sub").resolve())
        # characterization: TEST-T41 (ISS-0285, decision) - a repository path outside the project is not refused
        # today; '../x' resolves next to the project. The owner decided to refuse paths outside the project unless
        # listed explicitly; the root-detection step in phase 3 flips this assertion.
        self.assertEqual(m.repo_dir(root, "../x"), (root.parent / "x").resolve())
        # characterization: TEST-T41 - an absolute path replaces the root entirely.
        self.assertEqual(m.repo_dir(root, "/tmp"), Path("/tmp").resolve())


class FileAt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = mod()
        cls.repo = fixtures.ScratchRepo()
        cls.repo.write("src/a.py", NUMBERED)
        cls.repo.write("-dash.txt", "dash\n")
        cls.sha = cls.repo.commit("files")
        cls.root = cls.repo.path

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def err(self, sha, path):
        with self.assertRaises(RuntimeError) as cm:
            self.m.file_at(self.root, None, sha, path)
        return str(cm.exception)

    def test_file_at_rejects_bad_sha(self):
        for bad in ("zzzzzzz", "", None, "abc", "HEAD", "main", self.sha + "x", "-" + self.sha[:10]):
            with self.subTest(sha=bad):
                self.assertEqual(self.err(bad, "src/a.py"), "commit is not a hex hash")
        # Uppercase hex is refused (HEX is lowercase only, consistent with the schema's commit_sha check).
        self.assertEqual(self.err(self.sha.upper(), "src/a.py"), "commit is not a hex hash")
        for bad in ("-dash.txt", "--output=x", "src/a.py\x00x"):
            with self.subTest(path=bad):
                self.assertEqual(self.err(self.sha, bad), "bad path")
        # A well-formed sha that is not in the repository is git's own error.
        msg = self.err("0" * 40, "src/a.py")
        self.assertNotIn("hex hash", msg)
        self.assertTrue(msg)
        # A path missing at that commit is git's error too.
        self.assertIn("src/missing.py", self.err(self.sha, "src/missing.py"))

    def test_file_at_reads_blob(self):
        self.assertEqual(self.m.file_at(self.root, None, self.sha, "src/a.py"), NUMBERED)
        self.assertEqual(self.m.file_at(self.root, ".", self.sha[:7], "src/a.py"), NUMBERED)
        # A later edit in the working tree does not change what file_at reads at the commit.
        self.repo.write("src/a.py", "changed\n")
        self.addCleanup(self.repo.write, "src/a.py", NUMBERED)
        self.assertEqual(self.m.file_at(self.root, None, self.sha, "src/a.py"), NUMBERED)
        # Invalid UTF-8 is replaced, not raised.
        self.repo.write("bin.dat", b"ok \xff\n")
        sha2 = self.repo.commit("binary")
        self.assertEqual(self.m.file_at(self.root, None, sha2, "bin.dat"), "ok �\n")


class Location(unittest.TestCase):
    def test_location_lookup(self):
        L, sha, rid, rep = project(test=self)
        m = L.module
        lid = ref(L, rid, "src/a.py", 3, 5, symbol="thing")
        L.commit()
        self.assertIsNone(m.location(L.con, 999999))
        loc = m.location(L.con, lid)
        self.assertEqual(loc["id"], lid)
        self.assertEqual((loc["path"], loc["line_start"], loc["line_end"], loc["symbol"]), ("src/a.py", 3, 5, "thing"))
        self.assertEqual(loc["commit_sha"], sha)  # filled from the review's commit by tr_location_commit
        self.assertEqual(loc["repo_path"], ".")
        self.assertEqual(loc["repo_name"], "demo")
        self.assertEqual(loc["permalink_template"], PERMALINK)
        self.assertEqual(loc["repository_id"], rep)


class Api(unittest.TestCase):
    def test_api_closes_connection_on_error(self):
        L, _, _, _ = project(test=self)
        L.commit()
        m = fixtures.load_ledger_module(fresh=True)  # private copy: we patch its globals
        opened = []
        real_connect = m.connect

        def connect(db, readonly=False):
            con = real_connect(db, readonly)
            opened.append(con)
            return con

        def boom(*a):
            raise ValueError("route failed")

        m.connect = connect
        m.route = boom
        with self.assertRaises(ValueError):
            m.api(L.root, L.db, "/api/overview", {}, None)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):  # closed
            opened[0].execute("SELECT 1")

        # The normal path closes it too.
        opened.clear()
        m.route = lambda root, db, con, path, qs, body: {"ok": con.execute("SELECT 1").fetchone()[0]}
        self.assertEqual(m.api(L.root, L.db, "/api/x", {}, None), {"ok": 1})
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
