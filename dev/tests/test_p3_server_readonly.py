"""Viewer reads (plan P-0001 steps 37, 52 and the viewer half of 39): read-only connections with a percent-encoded
URI, a missing ledger is never re-created, /api/tables quotes names, per-request deadlines, and the SQL box limits
(time, text, cell and response size)."""
import json
import os
import shutil
import sqlite3
import threading
import time
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, seed_demo
from dev.tests.test_server import MakeServerThread, load_fresh

LONG_CTE = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class ReadonlyConnect(unittest.TestCase):
    def test_connect_readonly_refuses_writes(self):
        L = Ledger()
        self.addCleanup(L.close)
        L.commit()
        m = L.module
        for ro in (False, True):
            con = m.connect(L.db, readonly=ro)
            try:
                self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertIs(con.row_factory, sqlite3.Row)
                self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1 if ro else 0)
                if ro:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        con.execute("INSERT INTO meta(key, value) VALUES ('probe', 'x')")
            finally:
                con.close()

    def test_connect_never_creates_a_missing_file(self):
        m = load_fresh()
        d = fixtures.temp_dir("nocreate-")
        for ro in (False, True):
            with self.assertRaises(sqlite3.OperationalError):
                m.connect(d / "missing.sqlite", readonly=ro)
        self.assertEqual(tree(d), [])
        con = m.connect(d / "made.sqlite", create=True)  # init and backups ask for it explicitly
        con.close()
        self.assertEqual(tree(d), ["made.sqlite"])

    def test_readonly_uri_with_hash_in_path(self):
        for folder in ("a#b?c", "a%20b", "a?b"):
            with self.subTest(folder=folder):
                base = fixtures.temp_dir("uri-")
                L = Ledger(fixtures.ScratchRepo(base / folder / "proj"))
                self.addCleanup(L.close)
                seed_demo(L)
                L.commit()
                before = tree(base)
                res = L.module.run_readonly(L.db, "SELECT count(*) FROM issues")
                self.assertEqual(res["rows"], [[4]])
                con = L.module.connect(L.db, readonly=True)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        con.execute("CREATE TABLE probe(x)")
                finally:
                    con.close()
                with MakeServerThread(L) as srv:
                    r = srv.post("/api/sql", {"sql": "SELECT code FROM issues ORDER BY code LIMIT 1"})
                    self.assertEqual((r.status, r.json()["rows"]), (200, [["ISS-0001"]]))
                self.assertEqual(tree(base), before)


class ViewerReadsAreReadonly(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        seed_demo(self.L)
        self.L.commit()
        self.addCleanup(self.L.close)

    def test_get_routes_use_readonly_connection(self):
        mod = load_fresh()
        modes = []
        real = mod.connect

        def watch(db, readonly=False, **kw):
            modes.append(readonly)
            return real(db, readonly, **kw)

        mod.connect = watch
        for path in ("/api/overview", "/api/gates", "/api/reviews", "/api/review/R-0001", "/api/issues",
                     "/api/issue/ISS-0001", "/api/plans", "/api/trends", "/api/tables", "/api/table/issues"):
            del modes[:]
            res = mod.api(self.L.root, self.L.db, path, {}, None)
            self.assertNotIsInstance(res, tuple, path)
            self.assertEqual(modes, [True], path)
        del modes[:]
        mod.api(self.L.root, self.L.db, "/api/sql", {}, {"sql": "SELECT 1"})
        self.assertEqual(set(modes), {True})
        del modes[:]
        res = mod.api(self.L.root, self.L.db, "/api/gates/GATE-0001/answer", {}, {"answer_value": "use-7"})
        self.assertEqual(res, {"ok": True})
        self.assertEqual(modes, [True, False], "only the answer route opens a writable connection")

    def test_reads_use_readonly_connection(self):
        # a write through the connection api() hands to a read route fails
        mod = load_fresh()
        mod.route = lambda root, db, con, path, qs, body: con.execute(
            "UPDATE meta SET value='x' WHERE key='project_name'")
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            mod.api(self.L.root, self.L.db, "/api/overview", {}, None)
        self.assertEqual(self.L.scalar("SELECT value FROM meta WHERE key='project_name'"), self.L.root.name)

    test_viewer_get_is_readonly = test_reads_use_readonly_connection

    def test_missing_ledger_not_recreated(self):
        with MakeServerThread(self.L) as srv:
            self.L.con.close()
            self.L.con = None
            os.remove(self.L.db)
            for method, path, body in (("GET", "/api/overview", None), ("GET", "/api/gates", None),
                                       ("POST", "/api/sql", {"sql": "SELECT 1"}),
                                       ("POST", "/api/gates/GATE-0001/answer", {"answer_value": "use-7"})):
                r = srv.request(method, path, body=body)
                self.assertEqual((r.status, r.json()), (503, {"error": "ledger not found"}), path)
                self.assertFalse(self.L.db.exists(), path)

    test_viewer_does_not_create_ledger = test_missing_ledger_not_recreated

    def test_tables_route_with_odd_name(self):
        self.L.con.execute('CREATE VIEW "a""b" AS SELECT 1 AS "x""y"')
        self.L.commit()
        mod = load_fresh()
        res = mod.api(self.L.root, self.L.db, "/api/tables", {}, None)
        odd = [t for t in res if t["name"] == 'a"b']
        self.assertEqual(odd, [{"name": 'a"b', "type": "view", "columns": ['x"y'], "count": 1}])

    def test_route_deadline(self):
        mod = load_fresh()
        mod.ROUTE_SECONDS = 0.2
        real = mod.route

        def slow(root, db, con, path, qs, body):
            con.execute(LONG_CTE).fetchone()

        mod.route = slow
        t = time.monotonic()
        res = mod.api(self.L.root, self.L.db, "/api/overview", {}, None)
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(res[1], 400)
        self.assertIn("took too long", res[0]["error"])
        mod.route = real


class SqlBoxLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def setUp(self):
        self.mod = load_fresh()

    def run_sql(self, sql):
        res = self.mod.run_readonly(self.L.db, sql)
        return (res[1], res[0]) if isinstance(res, tuple) else (200, res)

    def test_sql_box_limits_are_the_planned_ones(self):
        self.assertEqual((self.mod.SQL_SECONDS, self.mod.SQL_MAX_TEXT, self.mod.SQL_MAX_CELL, self.mod.SQL_MAX_RESPONSE),
                         (4, 20_000, 10_000, 5_000_000))

    def test_sql_box_times_out(self):
        before = threading.active_count()
        t = time.monotonic()
        status, res = self.run_sql(LONG_CTE)  # the real 4 s budget
        took = time.monotonic() - t
        self.assertEqual(status, 400)
        self.assertEqual(res["error"], "The query took too long (over 4 s); add a LIMIT or a WHERE.")
        self.assertGreater(took, 3.5)
        self.assertLess(took, 6)
        self.assertLessEqual(threading.active_count(), before)
        self.assertEqual(self.run_sql("SELECT 1")[0], 200)

    # test_sql_query_deadline (step 39's name for the same property) is this test; not aliased, to keep it to one 4 s run

    def test_sql_box_does_not_block_writer(self):
        # rollback journal (today's default): the reader holds a shared lock, but it is stopped at the deadline,
        # so a writer with the normal 5 s busy timeout still commits
        self.mod.SQL_SECONDS = 0.5
        out = {}
        th = threading.Thread(target=lambda: out.update(r=self.run_sql(LONG_CTE)))
        th.start()
        time.sleep(0.1)
        con = sqlite3.connect(str(self.L.db), timeout=5)
        t = time.monotonic()
        con.execute("UPDATE meta SET value=value WHERE key='project_name'")
        con.commit()
        con.close()
        self.assertLess(time.monotonic() - t, 2)
        th.join(5)
        self.assertEqual(out["r"][0], 400)

    def test_reader_does_not_block_writer_in_wal(self):
        base = fixtures.temp_dir("wal-")
        db = base / "ledger.sqlite"
        shutil.copy(str(self.L.db), str(db))
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        con.close()
        self.mod.SQL_SECONDS = 1.5
        out = {}
        th = threading.Thread(target=lambda: out.update(r=self.mod.run_readonly(db, LONG_CTE)))
        th.start()
        time.sleep(0.2)
        w = sqlite3.connect(str(db), timeout=5)
        t = time.monotonic()
        w.execute("UPDATE meta SET value='wal-write' WHERE key='project_name'")
        w.commit()
        w.close()
        self.assertLess(time.monotonic() - t, 1)
        th.join(6)
        self.assertEqual(out["r"][1], 400)

    def test_sql_box_caps_cells(self):
        status, res = self.run_sql("SELECT zeroblob(10000000) AS b, x'41ff' AS small")
        self.assertEqual(status, 200)
        self.assertEqual(res["rows"], [[{"blob_bytes": 10000000}, {"blob_bytes": 2}]])
        self.assertLess(len(json.dumps(res)), 100_000)
        status, res = self.run_sql("SELECT printf('%.*c', 50000, 'a') AS t, 'short' AS s")
        cell = res["rows"][0][0]
        self.assertTrue(cell.startswith("a" * 10_000), cell[:20])
        self.assertLess(len(cell), 10_100)
        self.assertIn("50,000 characters", cell)
        self.assertEqual(res["rows"][0][1], "short")

    def test_sql_box_rejects_long_text(self):
        sql = "SELECT 1 -- " + "x" * 20_000
        status, res = self.run_sql(sql)
        self.assertEqual(status, 400)
        self.assertIn("too long", res["error"])
        self.assertEqual(self.run_sql("SELECT 1 -- " + "x" * 19_000)[0], 200)

    def test_sql_box_response_cap(self):
        self.mod.SQL_MAX_RESPONSE = 50_000
        status, res = self.run_sql("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 900) "
                                   "SELECT x, printf('%.*c', 1000, 'b') FROM c")
        self.assertEqual(status, 200)
        self.assertIs(res["truncated"], True)
        self.assertLess(len(json.dumps(res)), 55_000)
        self.assertGreater(len(res["rows"]), 10)


if __name__ == "__main__":
    unittest.main()
