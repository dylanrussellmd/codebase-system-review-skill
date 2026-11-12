"""ledger.py redact (plan P-0001 step 132: FIX-16): a secret in the append-only history is replaced everywhere."""
import sqlite3
import unittest

from dev.tests._p5_writes2_helpers import ok, project, refused, run

SECRET = "sk-live-4f9a2c77e1b0d3aa"


def everywhere(L, needle):
    """Every (table, column, rowid) whose text holds needle."""
    L.commit()
    hits = []
    for (t,) in L.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        cols = [r[1] for r in L.con.execute(f'PRAGMA table_info("{t}")')]
        for c in cols:
            for (rid,) in L.con.execute(f'SELECT rowid FROM "{t}" WHERE instr(CAST("{c}" AS TEXT), ?) > 0',
                                        (needle,)):
                hits.append((t, c, rid))
    return hits


class RedactTest(unittest.TestCase):
    def setUp(self):
        repo, L, sha = project(self)
        self.L = L
        summary = f"The API key {SECRET} is hard-coded in src/app.py."
        fid = L.finding(1, "SEC-H1", reviewer_run_id=1, summary=summary, before_state=f"It sends {SECRET}.")
        L.location(fid, "src/app.py", 1, 1)
        L.commit()
        ok(L, "match", "R-0001", "SEC-H1", "new", "--type", "new", "--rationale", "first review")
        ok(L, "tag-review", "R-0001")
        ok(L, "fill-refs", "R-0001")
        self.fid = fid

    def test_redact_replaces_text_everywhere(self):
        L = self.L
        self.assertTrue({t for t, c, r in everywhere(L, SECRET)} >= {"findings", "events", "issues"},
                        everywhere(L, SECRET))
        self.assertIn("never the value", refused(self, L, "redact", "findings.summary", "R-0001:SEC-H1",
                                                  "--only", SECRET, "--reason", f"leaked {SECRET}"))
        self.assertIn("not in", refused(self, L, "redact", "findings.summary", "R-0001:SEC-H1", "--only",
                                        "sk-other-0000000", "--reason", "a secret"))
        out = ok(L, "redact", "findings.summary", "R-0001:SEC-H1", "--only", SECRET, "--reason",
                 "an API key: a secret", "--actor", "A-0002")
        self.assertIn("findings.summary of R-0001:SEC-H1", out[0])
        self.assertEqual(everywhere(L, SECRET), [])
        self.assertEqual(L.scalar("SELECT summary FROM findings WHERE id=?", (self.fid,)),
                         "The API key [redacted] is hard-coded in src/app.py.")
        ev = L.one("SELECT entity_type, entity_id, from_value, to_value, note, actor_id FROM events "
                   "WHERE event='redacted'")
        self.assertEqual((ev["entity_type"], ev["entity_id"], ev["from_value"], ev["to_value"], ev["actor_id"]),
                         ("finding", self.fid, None, "findings.summary", 2))
        self.assertIn("an API key: a secret", ev["note"])
        self.assertNotIn(SECRET, ev["note"])
        # the append-only guards are back, exactly as shipped
        self.assertEqual(L.module.verify_schema(L.con), [])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            L.x("UPDATE findings SET summary='x' WHERE id=?", (self.fid,))
        L.con.rollback()
        p = run(L, "check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ok(L, "dump")
        self.assertNotIn(SECRET, L.db.with_suffix(".sql").read_text(encoding="utf-8"))

    def test_redact_whole_field(self):
        L = self.L
        ok(L, "redact", "findings.before_state", str(self.fid), "--reason", "a secret")
        self.assertEqual(L.scalar("SELECT before_state FROM findings WHERE id=?", (self.fid,)), "[redacted]")

    def test_redact_refuses_non_text(self):
        L = self.L
        for target, ref, why in (("findings.review_id", "1", "not a text column"),
                                 ("findings.id", "1", "not a text column"),
                                 ("findings.point_id", "1", "not a text column"),
                                 ("findings.verification", "1", "not a text column"),
                                 ("issues.code", "ISS-0001", "not a text column"),
                                 ("findings.nothing", "1", "no column"),
                                 ("nosuch.summary", "1", "no table"),
                                 ("findings.summary", "99", "no row"),
                                 ("findings", "1", "TABLE.COLUMN")):
            with self.subTest(target):
                self.assertIn(why, refused(self, L, "redact", target, ref, "--reason", "a secret"))
        self.assertIn("--reason", refused(self, L, "redact", "findings.summary", "1"))


if __name__ == "__main__":
    unittest.main()
