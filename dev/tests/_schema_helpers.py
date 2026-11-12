"""Shared base class for the schema tests (constraints, triggers, views, check).

SchemaCase gives every test a fresh ledger (copied from the template `init` made, in a plain folder, no git) with
the connection in autocommit mode, so each table-driven case runs inside its own SAVEPOINT and is rolled back
afterwards: cases never see each other's rows.

run_cases(table, base, good=..., bad=..., dup=...) is the table-driven runner:
  base(n)   returns the column dict of a valid row; n is a fresh number per call (use it for codes and names).
  good      (label, overrides) pairs: base(n) updated with overrides must insert.
  bad       (label, overrides, needle) triples: must raise sqlite3.IntegrityError whose message contains needle.
  dup       (label, second_overrides, needle) triples: base(n) inserts, then the same row updated with
            second_overrides must raise IntegrityError containing needle.
overrides may be a dict or a callable(test) returning a dict (for values that need parent rows made first).
"""
import itertools
import sqlite3
import unittest

from dev.tests.fixtures import Ledger, temp_dir

ISO_NOW = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"
FIXED = "2000-01-01T00:00:00.000Z"   # a fixed past timestamp, never what a trigger's 'now' writes
# SQLite before 3.34 reports a failed unnamed CHECK as "CHECK constraint failed: <table>" without the rule text, so on
# those versions a refusal is only required to be a CHECK failure on the right table. (Found while fixing; the vague
# message itself is a tracked defect.)
OLD_CHECK_MESSAGES = sqlite3.sqlite_version_info < (3, 34, 0)


class SchemaCase(unittest.TestCase):
    def setUp(self):
        self.L = Ledger(temp_dir("schema-"))
        self.L.con.isolation_level = None          # autocommit; cases use explicit savepoints
        self.addCleanup(self.L.close)
        self._n = itertools.count(1)

    # -- small helpers
    def n(self):
        return next(self._n)

    def x(self, sql, args=()):
        return self.L.con.execute(sql, args)

    def scalar(self, sql, args=()):
        return self.L.scalar(sql, args)

    def insert(self, table, **cols):
        return self.L.insert(table, **cols)

    def accepted(self, table, **cols):
        return self.insert(table, **cols)

    def refused(self, table, needle, **cols):
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.insert(table, **cols)
        msg = str(cm.exception)
        if OLD_CHECK_MESSAGES and msg == f"CHECK constraint failed: {table}" and needle not in msg:
            return msg
        self.assertIn(needle, msg)
        return msg

    def _resolve(self, overrides):
        return overrides(self) if callable(overrides) else dict(overrides)

    def in_savepoint(self, fn):
        self.x("SAVEPOINT tcase")
        try:
            return fn()
        finally:
            self.x("ROLLBACK TO tcase")
            self.x("RELEASE tcase")

    # -- the table-driven runner
    def run_cases(self, table, base, good=(), bad=(), dup=()):
        def row(overrides=None):
            cols = base(self.n())
            if overrides is not None:
                cols.update(self._resolve(overrides))
            return cols

        with self.subTest(table=table, case="base row"):
            self.in_savepoint(lambda: self.accepted(table, **row()))
        for label, overrides in good:
            with self.subTest(table=table, good=label):
                self.in_savepoint(lambda: self.accepted(table, **row(overrides)))
        for label, overrides, needle in bad:
            with self.subTest(table=table, bad=label):
                self.in_savepoint(lambda: self.refused(table, needle, **row(overrides)))
        for label, second, needle in dup:
            with self.subTest(table=table, dup=label):
                def go():
                    first = row()
                    self.accepted(table, **dict(first))
                    self.refused(table, needle, **{**first, **self._resolve(second)})
                self.in_savepoint(go)

    # -- parent rows
    def review(self, code=None, **kw):
        return self.L.review(code, **kw)

    def actor(self, kind="manager"):
        return self.L.actor(kind)

    def issue(self, review_id=None, **kw):
        return self.L.issue(first_seen_review_id=review_id or self.review(), **kw)
