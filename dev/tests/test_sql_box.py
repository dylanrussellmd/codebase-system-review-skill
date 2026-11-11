"""The viewer's read-only SQL box: run_readonly() and POST /api/sql (plan P-0001 step 22).

The authorizer allows SELECT, READ, FUNCTION and RECURSIVE only. Every probe that the security review found denied
(section 3.2 of its report) must give 400 and leave the ledger file byte-for-byte unchanged. Probed defects are pinned
under '# characterization: ISS-xxxx' comments; the phase 3 SQL-box-limits step flips them.
"""
import hashlib
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, seed_demo
from dev.tests.test_server import MakeServerThread


HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)
# SQLite before 3.34 reports a pragma table function the authorizer refuses on a read-only file as "no such table".
OLD_PRAGMA_REFUSAL = sqlite3.sqlite_version_info < (3, 34, 0)


def refusal_words(sql, words):
    """What the refusal must say. SQLite before 3.35 cannot parse RETURNING, and SQLite before 3.34 words a refused
    pragma table function as a missing table; either way the statement is still refused."""
    if "RETURNING" in sql.upper() and not HAS_RETURNING:
        return "syntax error"
    if "pragma_" in sql.lower() and OLD_PRAGMA_REFUSAL:
        return "no such table"
    return words


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SqlBox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.L.commit()
        cls.mod = cls.L.module
        cls.scratch = fixtures.temp_dir("sqlbox-")

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def run_sql(self, sql):
        res = self.mod.run_readonly(self.L.db, sql)
        return (res[1], res[0]) if isinstance(res, tuple) else (200, res)

    def assert_refused(self, sql, words="not authorized"):
        words = refusal_words(sql, words)
        status, res = self.run_sql(sql)
        self.assertEqual(status, 400, f"{sql!r} -> {res}")
        self.assertTrue(res["error"].startswith("The query did not run: "), res)
        if words:
            self.assertIn(words, res["error"], sql)

    def test_sql_allows_select_and_cte(self):
        status, d = self.run_sql("SELECT code, severity FROM issues ORDER BY code")
        self.assertEqual(status, 200)
        self.assertEqual(d["columns"], ["code", "severity"])
        self.assertEqual(d["rows"], [["ISS-0001", "H"], ["ISS-0002", "M"], ["ISS-0003", "L"], ["ISS-0004", "M"]])
        self.assertIs(d["truncated"], False)
        status, d = self.run_sql("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5) "
                                 "SELECT sum(x) AS s FROM c")
        self.assertEqual((status, d["rows"]), (200, [[15]]))
        for sql in ("SELECT name FROM sqlite_master LIMIT 1", "SELECT length(randomblob(8))",
                    "SELECT json_extract('{\"a\":1}', '$.a')", "SELECT sqlite_version()",
                    "SELECT * FROM v_owner_queue", "  select 1 ;  ", "SELECT 1;"):
            self.assertEqual(self.run_sql(sql)[0], 200, sql)
        self.assertEqual(self.run_sql("SELECT 1 WHERE 0")[1], {"columns": ["1"], "rows": [], "truncated": False})

    # The 19 probes the security review found denied (report section 3.2).
    DENIED = [
        "INSERT INTO meta(key, value) VALUES ('x', 'y')",
        "UPDATE issues SET title = 'x'",
        "DELETE FROM events",
        "WITH x AS (SELECT 1) DELETE FROM events",
        "DELETE FROM events RETURNING id",
        "CREATE TABLE t(x)",
        "CREATE TEMP TABLE t(x)",
        "ANALYZE",
        "REINDEX",
        "ATTACH DATABASE '{scratch}/attached.db' AS a",
        "ATTACH DATABASE ':memory:' AS m",
        "VACUUM INTO '{scratch}/copy.db'",
        "PRAGMA journal_mode=WAL",
        "PRAGMA table_info(issues)",
        "SELECT * FROM pragma_table_info('issues')",
        "SELECT * FROM pragma_database_list",
        "SELECT load_extension('nothing')",
        "BEGIN",
        "SAVEPOINT s",
    ]

    def test_sql_box_denies_writes(self):
        self.assertEqual(len(self.DENIED), 19)
        before = sha256(self.L.db)
        files = sorted(p.name for p in self.L.dir.iterdir())
        for sql in self.DENIED:
            sql = sql.format(scratch=self.scratch)
            with self.subTest(sql=sql):
                # VACUUM INTO reports 'authorization denied'; every other probe 'not authorized'
                self.assert_refused(sql, "authoriz")
        self.assertEqual(sha256(self.L.db), before, "the ledger file changed")
        self.assertEqual(sorted(p.name for p in self.L.dir.iterdir()), files)
        self.assertEqual(list(self.scratch.iterdir()), [], "ATTACH or VACUUM INTO created a file")

    test_sql_denies_writes = test_sql_box_denies_writes

    def test_sql_authorizer(self):
        for sql in ("DELETE FROM issues", "PRAGMA foreign_keys=OFF", "ATTACH ':memory:' AS m",
                    "SELECT load_extension('x')"):
            self.assert_refused(sql)
        self.assertEqual(self.run_sql("SELECT count(*) FROM issues")[1]["rows"], [[4]])
        self.assertIs(self.run_sql("SELECT 1 FROM (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                                   "WHERE x < 1001) SELECT x FROM c)")[1]["truncated"], True)
        # functions that do not exist stay unavailable
        for sql in ("SELECT readfile('/etc/passwd')", "SELECT writefile('/tmp/x', 'y')"):
            self.assert_refused(sql, "no such function")

    def test_sql_single_statement(self):
        status, res = self.run_sql("SELECT 1; SELECT 2")
        self.assertEqual(status, 400)
        self.assertIn("one statement at a time", res["error"])
        before = sha256(self.L.db)
        self.assertEqual(self.run_sql("SELECT 1; DELETE FROM events")[0], 400)
        self.assertEqual(sha256(self.L.db), before)

    def test_sql_empty(self):
        for sql in ("", "   ", ";", " ; ", None):
            self.assertEqual(self.run_sql(sql), (400, {"error": "Write a query first."}), repr(sql))
        # every trailing ';' is stripped (rstrip), so a doubled one still runs
        self.assertEqual(self.run_sql("SELECT 1;;")[0], 200)

    def test_sql_syntax_error(self):
        status, res = self.run_sql("SELEC 1")
        self.assertEqual(status, 400)
        self.assertEqual(res["error"], 'The query did not run: near "SELEC": syntax error')
        self.assertEqual(self.run_sql("SELECT * FROM no_such_table")[0], 400)

    def test_sql_truncates_at_1000(self):
        cte = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < {n}) SELECT x FROM c"
        status, d = self.run_sql(cte.format(n=1000))
        self.assertEqual((status, len(d["rows"]), d["truncated"]), (200, 1000, False))
        status, d = self.run_sql(cte.format(n=5000))
        self.assertEqual((status, len(d["rows"]), d["truncated"]), (200, 1000, True))
        self.assertEqual((d["rows"][0], d["rows"][-1]), ([1], [1000]))

    def test_current_non_string_sql_raises(self):
        # characterization: ISS-0085 a non-string sql reaches .strip() and raises (the server answers 500 with the
        # Python message); the fix makes it a 400
        for bad in (5, ["SELECT 1"], {"a": 1}):
            with self.assertRaises(AttributeError):
                self.mod.run_readonly(self.L.db, bad)

    def test_current_sql_has_no_time_limit(self):
        # characterization: ISS-0071 run_readonly sets no progress handler, timeout or size cap; a long query runs
        # to the end. Pinned by watching the connection run_readonly opens.
        mod = fixtures.load_ledger_module(fresh=True)
        calls = []
        real_connect = mod.connect

        class Watch:
            def __init__(self, con):
                self._con = con

            def __getattr__(self, name):
                calls.append(name)
                return getattr(self._con, name)

        mod.connect = lambda db, readonly=False: Watch(real_connect(db, readonly))
        res = mod.run_readonly(self.L.db, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                                          "WHERE x < 200000) SELECT count(*) FROM c")
        self.assertEqual(res["rows"], [[200000]])
        self.assertIn("set_authorizer", calls)
        self.assertNotIn("set_progress_handler", calls)
        self.assertNotIn("setlimit", calls)


class SqlBoxOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.srv = MakeServerThread(cls.L).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.L.close()

    def test_sql_route(self):
        r = self.srv.post("/api/sql", {"sql": "SELECT code FROM gates ORDER BY code"})
        self.assertEqual((r.status, r.json()),
                         (200, {"columns": ["code"], "rows": [["GATE-0001"], ["GATE-0002"]], "truncated": False}))
        before = sha256(self.L.db)
        for sql in SqlBox.DENIED:
            if "{scratch}" in sql:
                continue
            r = self.srv.post("/api/sql", {"sql": sql})
            self.assertEqual(r.status, 400, sql)
            self.assertIn(refusal_words(sql, "authoriz"), r.json()["error"], sql)
        self.assertEqual(sha256(self.L.db), before)
        self.assertEqual(self.srv.post("/api/sql", {"sql": ""}).json(), {"error": "Write a query first."})
        self.assertEqual(self.srv.post("/api/sql", {}).status, 400)  # missing sql is empty
        self.assertEqual(self.srv.post("/api/sql", {"sql": "SELECT 1; SELECT 2"}).status, 400)

    def test_current_sql_route_bad_types_and_blobs(self):
        # characterization: ISS-0085 a non-string sql gives 500 with the Python message
        r = self.srv.post("/api/sql", {"sql": 5})
        self.assertEqual(r.status, 500)
        self.assertIn("'int' object has no attribute 'strip'", r.json()["error"])
        # characterization: ISS-0071 blobs are sent as str(bytes), about 4x their size, with no cap
        r = self.srv.post("/api/sql", {"sql": "SELECT x'41ff' AS b"})
        self.assertEqual(r.json()["rows"], [["b'A\\xff'"]])
        # a JSON body that is not an object is treated as {} (no sql -> empty)
        r = self.srv.post("/api/sql", ["SELECT 1"])
        self.assertEqual((r.status, r.json()), (400, {"error": "Write a query first."}))


if __name__ == "__main__":
    unittest.main()
