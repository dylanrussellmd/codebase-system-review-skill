"""Platform checks: the Python 3.9 minimum, the SQLite features the schema needs, the read-only URI used for the
ledger path, and foreign keys on every connection the tool opens (plan P-0001 steps 2-5).

Run from the repository root:  python3 -m unittest dev.tests.test_platform -v
"""
import ast
import io
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from dev.tests import fixtures
from dev.tests import _platform_helpers as H


# ---------------------------------------------------------------- step 2: Python 3.9+

class PythonMinimum(unittest.TestCase):
    def test_running_python_meets_minimum(self):
        self.assertGreaterEqual(sys.version_info[:2], H.MIN_PYTHON)

    def test_sources_parse_as_python_39(self):
        """No 3.10+ syntax (match, except*, type aliases ...) in the tool, the build or the tests."""
        for label, text in H.python_sources():
            with self.subTest(source=label):
                try:
                    H.parse_as(text, label)
                except SyntaxError as e:
                    self.fail(f"{label} is not Python 3.9 syntax: {e}")

    def test_no_310_only_stdlib_calls_in_tool(self):
        """Names the tool might reach for that do not exist on 3.9."""
        newer = {"pairwise": (3, 10), "bit_count": (3, 10), "aiter": (3, 10), "anext": (3, 10),
                 "TaskGroup": (3, 11), "UTC": (3, 11), "blobopen": (3, 11), "serialize": (3, 11),
                 "deserialize": (3, 11), "setlimit": (3, 11), "getlimit": (3, 11), "batched": (3, 12),
                 "autocommit": (3, 12), "setconfig": (3, 12), "getconfig": (3, 12), "walk_up": (3, 12)}
        text = (H.REPO_ROOT / "dev" / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        used |= {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords if k.arg}
        for name, since in newer.items():
            if since > H.MIN_PYTHON:
                with self.subTest(name=name):
                    self.assertNotIn(name, used, f"dev/core.py uses .{name} (Python {since[0]}.{since[1]}+)")


class BuiltToolSmoke(unittest.TestCase):
    """The built ledger.py runs init, check, schema and serve on the running Python."""

    def test_built_ledger_init_check_schema(self):
        proj = fixtures.temp_dir("smoke-") / "proj"
        proj.mkdir()
        p = fixtures.run_ledger("--root", proj, "init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((proj / ".code-review" / "ledger.sqlite").exists())
        p = fixtures.run_ledger("--root", proj, "check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = fixtures.run_ledger("--root", proj, "schema")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("CREATE TABLE", p.stdout)

    def test_built_ledger_serves_page_and_api(self):
        L = fixtures.Ledger()
        self.addCleanup(L.close)
        with fixtures.ServerThread(L, mode="process") as srv:
            r = srv.get("/", token=False)
            self.assertEqual(r.status, 200)
            r = srv.get("/api/overview")
            self.assertEqual(r.status, 200)
            self.assertEqual(r.json()["project"], L.root.name)


# ---------------------------------------------------------------- step 3: SQLite features of the schema

class SqliteFeatures(unittest.TestCase):
    def test_json_functions_available(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT json_valid('[]'), json_array_length('[]'), json_type('[]')").fetchone(),
                         (1, 0, "array"))

    def test_schema_loads_on_this_sqlite(self):
        con = H.fresh_schema_connection()
        self.addCleanup(con.close)
        self.assertGreater(con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 10)
        # The json_valid CHECKs are live: invalid JSON in a JSON column is refused.
        self.assertIn("json_valid", H.schema_text())

    def test_schema_needs_no_sql_newer_than_json1(self):
        """The schema uses nothing past JSON1 (built in from SQLite 3.38, a compile option before): no window
        functions, UPSERT, RETURNING, generated columns, STRICT tables, ->> or iif(). Keeps the minimum SQLite low."""
        import re
        sql = re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", H.schema_text())).lower()
        for pat in (r"\bover\s*\(", r"\bon\s+conflict\b", r"\breturning\b", r"\bgenerated\s+always\b", r"\bstrict\b",
                    r"->>", r"\biif\s*\(", r"\bunixepoch\s*\(", r"\bmaterialized\b", r"\bfilter\s*\("):
            with self.subTest(pattern=pat):
                self.assertIsNone(re.search(pat, sql))


# ---------------------------------------------------------------- step 4: ? and # in the ledger path

class ReadonlyUri(unittest.TestCase):
    def ledger_in(self, folder):
        base = fixtures.temp_dir("uri-")
        repo = fixtures.ScratchRepo(base / folder / "proj")
        L = fixtures.Ledger(repo)
        self.addCleanup(L.close)
        L.commit()
        return base, L

    def test_readonly_connect_plain_path_is_readonly(self):
        base, L = self.ledger_in("plain")
        con = L.module.connect(L.db, readonly=True)
        self.addCleanup(con.close)
        self.assertGreater(con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 10)
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            con.execute("CREATE TABLE probe(x)")
        res = L.module.run_readonly(L.db, "SELECT count(*) FROM issues")
        self.assertEqual(res["rows"], [[0]])

    def test_readonly_uri_with_hash_or_question_mark(self):
        # SEC-V1 / DOC-V5 (ISS-0036) fixed (step 37): connect() percent-encodes the path into a file: URI, so a ? or
        # # in the path no longer cuts the file name or drops mode=ro, and no stray database appears.
        for folder in ("a#b?c", "a#b", "a?b"):
            with self.subTest(folder=folder):
                base, L = self.ledger_in(folder)
                stray = base / "a"
                con = L.module.connect(L.db, readonly=True)
                try:
                    self.assertGreater(con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 10)
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        con.execute("CREATE TABLE probe(x)")
                finally:
                    con.close()
                self.assertFalse(stray.exists(), H.tree_files(base))
                res = L.module.run_readonly(L.db, "SELECT count(*) FROM issues")
                self.assertEqual(res["rows"], [[0]])
                self.assertIsNone(L.scalar("SELECT name FROM sqlite_master WHERE name='probe'"))


# ---------------------------------------------------------------- step 5: foreign keys on every connection

class ForeignKeys(unittest.TestCase):
    def test_connect_enables_foreign_keys(self):
        L = fixtures.Ledger()
        self.addCleanup(L.close)
        L.commit()
        m = L.module
        for ro in (False, True):
            with self.subTest(readonly=ro):
                con = m.connect(L.db, readonly=ro)
                try:
                    self.assertEqual(H.fk_pragma(con), 1)
                    self.assertEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                    self.assertIs(con.row_factory, sqlite3.Row)
                    if ro:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "attempt to write a readonly database"):
                            con.execute("INSERT INTO meta(key, value) VALUES ('probe', 'x')")
                    else:
                        with self.assertRaises(sqlite3.IntegrityError) as cm:
                            con.execute(H.ORPHAN_SQL)
                        self.assertTrue(H.is_fk_error(cm.exception), cm.exception)
                finally:
                    con.close()

    def test_only_connect_opens_sqlite(self):
        """Every connection goes through connect(), so the PRAGMA cannot be skipped by a new code path."""
        tree = ast.parse((H.REPO_ROOT / "dev" / "core.py").read_text(encoding="utf-8"))
        callers = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "connect"
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == "sqlite3"):
                        callers.append(fn.name)
        self.assertTrue(callers)
        # sqlite_problem() opens an in-memory database to probe for the JSON functions, and query_plan() one to hold
        # the reviewed project's schema for EXPLAIN QUERY PLAN; neither opens a ledger.
        self.assertEqual(set(callers), {"connect", "sqlite_problem", "query_plan"})

    def test_foreign_keys_enforced_on_every_tool_connection(self):
        """Drive each command and API route in-process; every connection the tool opens refuses an orphan."""
        L = fixtures.Ledger()
        self.addCleanup(L.close)
        L.commit()
        m = fixtures.load_ledger_module(L.ledger_py, fresh=True)
        real = m.connect
        seen, opened = [], []
        # main() leaves its connection open when a command ends in sys.exit; close every one at the end.
        self.addCleanup(lambda: [c.close() for c in opened])

        def spy(db, readonly=False, **kw):
            con = real(db, readonly=readonly, **kw)
            fk = H.fk_pragma(con)
            try:
                con.execute(H.ORPHAN_SQL)
                outcome = "accepted"
            except sqlite3.IntegrityError as e:
                outcome = "fk" if H.is_fk_error(e) else str(e)
            except sqlite3.OperationalError as e:
                msg = str(e)
                outcome = "readonly" if "readonly" in msg else "empty" if "no such table" in msg else msg
            con.rollback()
            seen.append((readonly, fk, outcome))
            opened.append(con)
            return con

        m.connect = spy
        out = io.StringIO()
        new_proj = fixtures.temp_dir("fk-") / "proj"
        new_proj.mkdir()
        with redirect_stdout(out), redirect_stderr(out):
            m.init(new_proj, new_proj / ".code-review" / "ledger.sqlite")
            for cmd in (["check"], ["gates"], ["dump"], ["fill-refs"], ["show", "1"], ["tag-review", "R-9999"]):
                argv = sys.argv
                sys.argv = ["ledger.py", "--root", str(L.root), *cmd]
                try:
                    m.main()
                except SystemExit:
                    pass
                finally:
                    sys.argv = argv
            for path in ("/api/overview", "/api/gates", "/api/reviews", "/api/issues", "/api/plans", "/api/trends",
                         "/api/tables"):
                m.api(L.root, L.db, path, {}, None)
            m.api(L.root, L.db, "/api/sql", {}, {"sql": "SELECT 1"})
        self.assertGreaterEqual(len(seen), 15, seen)
        # init's connection opens the new, still empty file (before the schema): it must have the PRAGMA too.
        self.assertEqual(seen[0], (False, 1, "empty"))
        for ro, fk, outcome in seen[1:]:
            self.assertEqual(fk, 1)
            self.assertEqual(outcome, "readonly" if ro else "fk")
        self.assertIn(True, [s[0] for s in seen])

    def test_foreign_keys_on_every_connection(self):
        """An agent connection that forgets the PRAGMA can insert an orphan; check reports it and exits non-zero."""
        L = fixtures.Ledger()
        self.addCleanup(L.close)
        L.commit()
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        raw = sqlite3.connect(str(L.db))  # no PRAGMA foreign_keys: SQLite's default is off
        try:
            self.assertEqual(H.fk_pragma(raw), 0)
            raw.execute(H.ORPHAN_SQL)
            raw.commit()
        finally:
            raw.close()
        p = L.run("check")
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertRegex(p.stdout, r"foreign_key_check: [12] broken references")


if __name__ == "__main__":
    unittest.main()
