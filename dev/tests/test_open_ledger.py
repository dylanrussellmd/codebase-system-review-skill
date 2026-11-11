"""Every command opens the ledger through open_ledger() (plan step 33): a missing ledger, a file that is not SQLite, a
missing schema_version row and a ledger newer than the tool each give one line and exit 1 with no traceback; a ledger
older than the tool refuses writes (pointing to migrate) and is read with a warning."""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, run_ledger, temp_dir

READS = (["check"], ["gates"], ["show", "1"], ["dump"])
WRITES = (["fill-refs"], ["tag-review", "R-0001"], ["serve", "--no-open", "--port", "0"])
ALL = READS + WRITES + (["migrate"],)


def set_version(db, value):
    con = sqlite3.connect(str(db))
    if value is None:
        con.execute("DELETE FROM meta WHERE key='schema_version'")
    else:
        con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (value,))
    con.commit()
    con.close()


class Refusals(unittest.TestCase):
    def assertCleanRefusal(self, p, words):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(words, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)

    def test_commands_refuse_newer_schema(self):
        L = Ledger(temp_dir("open-"))
        L.close()
        set_version(L.db, "99")
        before = L.db.read_bytes()
        for args in ALL + (["init"],):
            with self.subTest(args=args):
                p = run_ledger("--root", L.root, *args, timeout=20)
                self.assertCleanRefusal(p, "is schema 99; this ledger.py only knows 1. Use a newer ledger.py.")
        self.assertEqual(L.db.read_bytes(), before)

    def test_commands_refuse_missing_version_row(self):
        L = Ledger(temp_dir("open-"))
        L.close()
        set_version(L.db, None)
        for args in ALL + (["init"],):
            with self.subTest(args=args):
                p = run_ledger("--root", L.root, *args, timeout=20)
                self.assertCleanRefusal(p, "has no schema_version in its meta table")

    def test_commands_refuse_non_sqlite_file(self):
        d = temp_dir("open-") / "proj"
        (d / ".code-review").mkdir(parents=True)
        db = d / ".code-review" / "ledger.sqlite"
        for content, words in ((b"this is not a database at all" * 100, "is not a ledger"),
                               (b"", "is not a ledger")):
            db.write_bytes(content)
            for args in ALL + (["init"],):
                with self.subTest(args=args, size=len(content)):
                    p = run_ledger("--root", d, *args, timeout=20)
                    self.assertCleanRefusal(p, words)
                    self.assertEqual(db.read_bytes(), content)

    def test_commands_refuse_without_ledger(self):
        d = temp_dir("open-") / "proj"
        d.mkdir()
        for args in ALL:
            with self.subTest(args=args):
                p = run_ledger("--root", d, *args, timeout=20)
                self.assertCleanRefusal(p, "no ledger at")
        self.assertFalse((d / ".code-review").exists())

    def test_cli_check_refuses_wrong_schema(self):
        """A tool of schema 2 on a schema-1 ledger: writes refuse and name migrate; reads warn and run; check fails."""
        project, db = M.restore_v1()
        v2 = M.v2_ledger_py()
        before = db.read_bytes()
        for args in WRITES:
            with self.subTest(args=args):
                p = run_ledger("--root", project, *args, ledger_py=v2, timeout=20)
                self.assertCleanRefusal(p, "is schema 1; this ledger.py is schema 2. Run: python3 ")
                self.assertIn("ledger.py migrate", p.stderr)
        for args in (["gates"], ["show", "1"], ["dump"]):
            with self.subTest(args=args):
                p = run_ledger("--root", project, *args, ledger_py=v2, timeout=20)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("warning:", p.stderr)
                self.assertIn("ledger.py migrate", p.stderr)
        db.with_suffix(".sql").unlink()
        p = run_ledger("--root", project, "check", ledger_py=v2)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("schema_version: the ledger is schema 1; this ledger.py is schema 2", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
