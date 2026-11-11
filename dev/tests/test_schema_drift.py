"""check compares the ledger's stored schema with the schema this ledger.py ships, object by object, and its
schema_version with SCHEMA_VERSION (plan step 34). serve, gates, fill-refs and tag-review refuse a tampered schema;
init and migrate put back the shipped views and triggers (they hold no data) and refuse other differences."""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, temp_dir


def tamper(L, *statements):
    L.close()
    con = sqlite3.connect(str(L.db))
    for s in statements:
        con.execute(s)
    con.commit()
    con.close()


def new_ledger():
    return Ledger(temp_dir("drift-"))


FOREIGN_TRIGGER = ("CREATE TRIGGER tr_forge AFTER INSERT ON gates BEGIN "
                   "UPDATE gates SET status='answered', answer_value='yes', answered_by='owner' WHERE id=NEW.id; END")


class CheckDrift(unittest.TestCase):
    def assertCheckFails(self, L, *needles):
        p = L.run("check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        for n in needles:
            self.assertIn(n, p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def test_check_passes_fresh_ledger(self):
        L = new_ledger()
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("schema", p.stdout)

    def test_check_reports_schema_drift(self):
        """Dropping a view or changing a trigger's body makes check exit 1 naming the object."""
        L = new_ledger()
        tamper(L, "DROP VIEW v_issue_timeline")
        self.assertCheckFails(L, "differences from the shipped schema", "missing view v_issue_timeline")
        L = new_ledger()
        sql = L.scalar("SELECT sql FROM sqlite_master WHERE name='tr_issue_opened'")
        tamper(L, "DROP TRIGGER tr_issue_opened", sql.replace("BEGIN", "WHEN 0 BEGIN", 1))
        self.assertCheckFails(L, "changed trigger tr_issue_opened")

    def test_check_rejects_foreign_trigger(self):
        L = new_ledger()
        tamper(L, FOREIGN_TRIGGER)
        self.assertCheckFails(L, "extra trigger tr_forge (not in the shipped schema)")

    def test_check_rejects_redefined_view(self):
        L = new_ledger()
        tamper(L, "DROP VIEW v_untraceable_refs", "CREATE VIEW v_untraceable_refs AS SELECT 1 WHERE 0")
        self.assertCheckFails(L, "changed view v_untraceable_refs")

    def test_check_rejects_dropped_trigger(self):
        L = new_ledger()
        tamper(L, "DROP TRIGGER tr_issue_opened")
        self.assertCheckFails(L, "missing trigger tr_issue_opened")

    def test_check_rejects_dropped_must_be_empty_view(self):
        """A must-be-empty view that is gone is reported, not a traceback."""
        L = new_ledger()
        tamper(L, "DROP VIEW v_unmatched_findings")
        p = self.assertCheckFails(L, "missing view v_unmatched_findings")
        self.assertIn("(v_unmatched_findings)", p.stdout)

    def test_check_rejects_extra_and_changed_tables_and_indexes(self):
        L = new_ledger()
        tamper(L, "CREATE TABLE stray(x)", "CREATE INDEX stray_ix ON stray(x)",
               "ALTER TABLE themes ADD COLUMN colour TEXT")
        self.assertCheckFails(L, "extra table stray", "extra index stray_ix", "changed table themes")

    def test_check_rejects_wrong_schema_version(self):
        """test_check_reports_version_mismatch: an older schema_version fails check; an untouched ledger passes."""
        for value in ("0", "1.0", "abc"):
            with self.subTest(value=value):
                L = new_ledger()
                tamper(L, f"UPDATE meta SET value='{value}' WHERE key='schema_version'")
                p = L.run("check")
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn("schema_version", p.stdout + p.stderr)

    def test_whitespace_and_comments_are_not_drift(self):
        """Objects are compared on whitespace-normalized SQL without comments and with simple names unquoted, so a
        view re-created with other spacing, or a table renamed into place by a migration, is not drift."""
        L = new_ledger()
        sql = L.scalar("SELECT sql FROM sqlite_master WHERE name='v_untraceable_refs'")
        respaced = sql.replace("\n", "\n   -- a comment; with a semicolon\n  ").replace(
            "CREATE VIEW v_untraceable_refs", 'CREATE VIEW "v_untraceable_refs"')
        tamper(L, "DROP VIEW v_untraceable_refs", respaced)
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        mod = fixtures.load_ledger_module()
        self.assertEqual(mod.normalize_sql("CREATE TABLE t ( a TEXT DEFAULT 'x  ,  y' , -- c\n b )"),
                         "CREATE TABLE t(a TEXT DEFAULT 'x  ,  y',b)")
        self.assertNotEqual(mod.normalize_sql("SELECT 'a b'"), mod.normalize_sql("SELECT 'a  b'"))


class ShippedObjects(unittest.TestCase):
    def test_shipped_objects_ignores_create_in_comments(self):
        """A comment above a statement that says 'create' (any case) is not taken as the statement's start."""
        mod = fixtures.load_ledger_module()
        schema = ("-- Created rows are logged below.\n/* we create one table */\nCREATE TABLE t (a INTEGER);\n"
                  "-- create an index\nCREATE INDEX t_a ON t (a);\n")
        saved = mod.SCHEMA
        try:
            mod.SCHEMA = schema
            got = [(t, n, s) for t, n, _, s in mod.shipped_objects()]
        finally:
            mod.SCHEMA = saved
            mod._shipped.clear()
        self.assertEqual(got, [("table", "t", "CREATE TABLE t (a INTEGER)"), ("index", "t_a", "CREATE INDEX t_a ON t (a)")])

    def test_shipped_objects_match_sqlite(self):
        """shipped_objects() reads the CREATE statements from SCHEMA's text (no extra connection); it must list
        exactly what SQLite stores in sqlite_master when SCHEMA runs, in the same order and with the same SQL."""
        mod = fixtures.load_ledger_module()
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(mod.SCHEMA)
        stored = con.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                             "AND sql IS NOT NULL ORDER BY rowid").fetchall()
        self.assertEqual([(t, n, s) for t, n, _, s in mod.shipped_objects()], stored)
        # one object per CREATE statement of the schema (35 + 20 + 18 + 18 at schema 2; phase 4 adds many)
        creates = [s for s in mod.split_sql(mod.SCHEMA) if mod.CREATE_OBJECT.match(mod.normalize_sql(s))]
        self.assertEqual(len(stored), len(creates))
        self.assertGreater(len(stored), 35 + 20 + 18 + 18)


class CommandsRefuseTamperedSchema(unittest.TestCase):
    def test_serve_refuses_tampered_schema(self):
        L = new_ledger()
        tamper(L, FOREIGN_TRIGGER)
        p = L.run("serve", "--no-open", "--port", "0", timeout=20)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("does not match the schema this ledger.py ships", p.stderr)
        self.assertIn("extra trigger tr_forge", p.stderr)
        self.assertNotIn("Ledger viewer", p.stdout)

    def test_gates_and_writes_refuse_tampered_schema(self):
        L = new_ledger()
        tamper(L, FOREIGN_TRIGGER)
        before = L.db.read_bytes()
        for args in (["gates"], ["fill-refs"], ["tag-review", "R-0001"]):
            with self.subTest(args=args):
                p = L.run(*args)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("extra trigger tr_forge", p.stderr)
        self.assertEqual(L.db.read_bytes(), before)
        for args in (["show", "1"], ["dump"]):  # reads that help diagnose still work
            with self.subTest(args=args):
                p = L.run(*args)
                self.assertNotIn("Traceback", p.stderr)
                self.assertNotIn("does not match", p.stderr)

    def test_init_restores_views_and_triggers(self):
        L = new_ledger()
        view = L.scalar("SELECT sql FROM sqlite_master WHERE name='v_untraceable_refs'")
        tamper(L, FOREIGN_TRIGGER, "DROP TRIGGER tr_issue_opened",
               "DROP VIEW v_untraceable_refs", "CREATE VIEW v_untraceable_refs AS SELECT 1 WHERE 0")
        p = L.run("init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("removed trigger tr_forge", p.stdout)
        self.assertIn("restored trigger tr_issue_opened", p.stdout)
        self.assertIn("restored view v_untraceable_refs", p.stdout)
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        con = sqlite3.connect(str(L.db))
        self.assertEqual(con.execute("SELECT sql FROM sqlite_master WHERE name='v_untraceable_refs'").fetchone()[0],
                         view)
        con.close()

    def test_init_refuses_table_drift(self):
        L = new_ledger()
        tamper(L, "CREATE TABLE stray(x)")
        p = L.run("init")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("extra table stray", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
