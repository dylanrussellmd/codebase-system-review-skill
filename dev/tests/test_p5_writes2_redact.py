"""ledger.py redact (plan P-0001 step 132: FIX-16): a secret in the append-only history is replaced everywhere."""
import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from dev.tests import fixtures, scale
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

    def test_redact_any_text_column(self):
        L = self.L
        L.x("UPDATE repositories SET remote_url=? WHERE name='demo'", (f"https://bot:{SECRET}1@example.com/demo.git",))
        ok(L, "add-requirement", f"Deploy with the token {SECRET}2.", "--source", "owner, chat")
        ok(L, "interview-ask", "Which token does the deploy use?", "--review", "R-0001", "--why", "the deploy script")
        ok(L, "interview-answer", "IQ-0001", f"The token {SECRET}3.", "--actor", "A-0001")
        L.commit()
        for n, (field, ref, entity) in enumerate((("repositories.remote_url", "demo", "ledger"),
                                                  ("requirements.text", "REQ-0001", "ledger"),
                                                  ("interview_answers.answer", "1", "question")), 1):
            with self.subTest(field):
                if field == "repositories.remote_url":
                    ref = str(L.scalar("SELECT id FROM repositories WHERE name='demo'"))
                before = L.count("events", "event='redacted'")
                ok(L, "redact", field, ref, "--only", f"{SECRET}{n}", "--reason", "a deploy token")
                self.assertEqual(everywhere(L, f"{SECRET}{n}"), [])
                self.assertEqual(L.count("events", "event='redacted'"), before + 1)
                ev = L.one("SELECT entity_type, to_value FROM events WHERE event='redacted' ORDER BY id DESC")
                self.assertEqual((ev["entity_type"], ev["to_value"]), (entity, field))
        self.assertIn("[redacted]", L.scalar("SELECT remote_url FROM repositories WHERE name='demo'"))
        p = run(L, "check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_redact_lists_leftover_files(self):
        L = self.L
        folder = L.db.parent
        (folder / "ledger.sqlite.bak-v22").write_bytes(b"SQLite format 3\0" + SECRET.encode())
        (folder / "reviews" / "R-0001").mkdir(parents=True, exist_ok=True)
        (folder / "reviews" / "R-0001" / "brief.md").write_text(f"The key is {SECRET}.\n", encoding="utf-8")
        (folder / "hub.html").write_text(f"<p>{SECRET}</p>", encoding="utf-8")
        (folder / "reviews" / "R-0001" / "clean.md").write_text("nothing here\n", encoding="utf-8")
        p = run(L, "redact", "findings.summary", "R-0001:SEC-H1", "--only", SECRET, "--reason", "an API key")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in ("ledger.sqlite.bak-v22", "reviews/R-0001/brief.md", "hub.html"):
            self.assertIn(name, p.stdout)
        self.assertNotIn("clean.md", p.stdout)
        self.assertNotIn(SECRET, p.stdout + p.stderr)
        self.assertIn("git", p.stdout)

    def test_redact_names_published_page(self):
        L = self.L
        ok(L, "add-artifact", "progress-page", "--review", "R-0001", "--url", "https://example.com/page")
        out = ok(L, "redact", "findings.summary", "R-0001:SEC-H1", "--only", SECRET, "--reason", "an API key")
        self.assertTrue(any("https://example.com/page" in line and "republish" in line for line in out), out)

    def test_redact_only_from_stdin(self):
        L = self.L
        L.commit()
        argv = ["--root", str(L.root), "redact", "findings.summary", "R-0001:SEC-H1", "--only", "@-", "--reason",
                "an API key"]
        p = fixtures.run_ledger(*argv, ledger_py=L.ledger_py, input=SECRET + "\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(everywhere(L, SECRET), [])
        self.assertEqual(L.scalar("SELECT summary FROM findings WHERE id=?", (self.fid,)),
                         "The API key [redacted] is hard-coded in src/app.py.")

    def test_redact_rehashes_excerpt(self):
        L = self.L
        lid = L.scalar("SELECT id FROM finding_locations WHERE finding_id=?", (self.fid,))
        L.x("UPDATE finding_locations SET line_start=5, line_end=5, excerpt=NULL, excerpt_sha256=NULL, blob_sha=NULL "
            "WHERE id=?", (lid,))
        L.commit()
        ok(L, "fill-refs", "R-0001")
        real = L.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (lid,))
        self.assertEqual(real, "def handler(request):")
        ok(L, "redact", "finding_locations.excerpt", str(lid), "--only", "handler", "--reason", "a name")
        r = L.one("SELECT excerpt, excerpt_sha256, excerpt_redacted FROM finding_locations WHERE id=?", (lid,))
        self.assertEqual(r["excerpt"], "def [redacted](request):")
        self.assertNotEqual(r["excerpt_sha256"], hashlib.sha256(real.encode()).hexdigest())
        self.assertEqual(r["excerpt_sha256"], hashlib.sha256(r["excerpt"].encode()).hexdigest())
        self.assertEqual(r["excerpt_redacted"], 1)
        self.assertTrue(L.module.source(L.root, L.con, lid)["excerpt_matches"])
        p = run(L, "check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_redact_reports_busy_checkpoint(self):
        L = self.L
        reader = sqlite3.connect(str(L.db))
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM findings").fetchone()
        p = run(L, "redact", "findings.summary", "R-0001:SEC-H1", "--only", SECRET, "--reason", "an API key")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("redacted findings.summary of R-0001:SEC-H1", p.stdout)
        self.assertIn("stays in", p.stderr)
        self.assertIn("readers", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(everywhere(L, SECRET), [])
        reader.rollback()
        ok(L, "dump")
        for f in L.db.parent.glob(L.db.name + "*"):
            self.assertNotIn(SECRET.encode(), f.read_bytes(), f.name)

    def test_redact_blocked_vacuum_says_how_to_finish(self):
        """Another writer holds the ledger when redact's VACUUM runs: redact exits 1 with a line that finishes it."""
        L = self.L
        mod = L.module
        writer = sqlite3.connect(str(L.db), isolation_level=None)
        self.addCleanup(writer.close)
        scrub = mod.scrub_ledger

        def blocked(con, db, secret):
            writer.execute("BEGIN IMMEDIATE")
            try:
                return scrub(con, db, secret)
            finally:
                writer.execute("ROLLBACK")

        with mock.patch.object(mod, "scrub_ledger", blocked), mock.patch.object(mod, "BUSY_SECONDS", 0):
            p = run(L, "redact", "findings.summary", "R-0001:SEC-H1", "--only", SECRET, "--reason", "an API key")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("could not rewrite ledger.sqlite", p.stderr)
        line = p.stderr.split("run: ")[1].split("; then")[0]
        ok(L, "dump")
        for f in L.db.parent.glob(L.db.name + "*"):
            self.assertNotIn(SECRET.encode(), f.read_bytes(), f.name)
        L.close()
        done = subprocess.run(line, shell=True, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


class LedgerEntityUpgrade(unittest.TestCase):
    def test_schema_upgrade_to_24(self):
        """A schema-23 ledger migrates through 0024: its events are kept and take the entity type 'ledger'."""
        from dev.tests import _migrate_helpers as M
        project_dir, db = M.restore(23)
        before = M.counts(db)["events"]
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0024-ledger-events", p.stdout)
        con = sqlite3.connect(str(db))
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM events").fetchone()[0], before)
        con.execute("INSERT INTO events(entity_type, entity_id, event) VALUES ('ledger', 0, 'redacted')")
        self.assertEqual(con.execute("SELECT label FROM enum_labels WHERE enum='entity_type' AND value='ledger'")
                         .fetchone()[0], "Ledger")


class MaskedHashUpgrade(unittest.TestCase):
    def test_old_masked_hashes_rewritten(self):
        """A schema-24 row with a masked excerpt and the hash of its real lines has the hash of the masked excerpt
        after migrate; an unmasked row keeps its hash."""
        from dev.tests import _migrate_helpers as M
        project_dir, db = M.restore(24)
        real = "token = 'hunter22'\n"
        masked = "token = '[REDACTED]'\n"
        con = sqlite3.connect(str(db))
        ids = [r[0] for r in con.execute("SELECT id FROM finding_locations ORDER BY id LIMIT 2")]
        con.execute("UPDATE finding_locations SET excerpt=?, excerpt_sha256=?, excerpt_redacted=1 WHERE id=?",
                    (masked, hashlib.sha256(real.encode()).hexdigest(), ids[0]))
        kept = con.execute("SELECT excerpt_sha256 FROM finding_locations WHERE id=?", (ids[1],)).fetchone()[0]
        con.commit()
        con.close()
        p = fixtures.run_ledger("--root", project_dir, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        con = sqlite3.connect(str(db))
        self.addCleanup(con.close)
        got = dict(con.execute("SELECT id, excerpt_sha256 FROM finding_locations WHERE id IN (?, ?)", ids))
        self.assertEqual(got[ids[0]], hashlib.sha256(masked.encode()).hexdigest())
        self.assertEqual(got[ids[1]], kept)


class RedactBytesTest(unittest.TestCase):
    """On a grown ledger (page splits, cells moved within pages) no byte of the redacted text is left in the files."""

    def setUp(self):
        self.info = scale.build(fixtures.temp_dir("redact-bytes-") / "proj", reviews=2, files=20, findings=120,
                                steps=30)
        self.db = self.info["db"]
        con = sqlite3.connect(str(self.db))
        self.code, self.point, self.summary = con.execute(
            "SELECT r.code, f.point_id, f.summary FROM findings f JOIN reviews r ON r.id = f.review_id "
            "ORDER BY f.id LIMIT 1 OFFSET 7").fetchone()
        con.close()

    def copies(self, text):
        return {f.name: f.read_bytes().count(text.encode()) for f in sorted(self.db.parent.glob(self.db.name + "*"))
                if f.read_bytes().count(text.encode())}

    def test_redact_leaves_no_bytes(self):
        for only in (False, True):
            with self.subTest(only=only):
                if only:
                    self.setUp()
                secret = self.summary[10:40] if only else self.summary
                self.assertTrue(self.copies(secret))
                p = fixtures.run_ledger("--root", self.info["root"], "redact", "findings.summary",
                                        f"{self.code}:{self.point}", "--reason", "a secret",
                                        *(("--only", secret) if only else ()))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.copies(secret), {})
                p = fixtures.run_ledger("--root", self.info["root"], "check")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_redact_blocked_vacuum_zeroes_changed_cells(self):
        """With VACUUM blocked, secure_delete has zeroed every cell redact changed: the copies left are the ones in
        space freed before redact, and the line redact prints removes those."""
        mod = fixtures.load_ledger_module(fresh=True)
        secret = self.summary[10:40]
        con = sqlite3.connect(str(self.db))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        live = 0
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for (c,) in [(r[1],) for r in con.execute(f'PRAGMA table_info("{t}")')]:
                live += con.execute(f'SELECT count(*) FROM "{t}" WHERE instr(CAST("{c}" AS TEXT), ?) > 0',
                                    (secret,)).fetchone()[0]
        con.close()
        before = sum(self.copies(secret).values())
        self.assertGreater(before, live, "the grown ledger has no copy in free space")
        writer = sqlite3.connect(str(self.db), isolation_level=None)
        self.addCleanup(writer.close)
        scrub = mod.scrub_ledger

        def blocked(con, db, text):
            writer.execute("BEGIN IMMEDIATE")
            try:
                return scrub(con, db, text)
            finally:
                writer.execute("ROLLBACK")

        def main(*args):
            err = io.StringIO()
            with mock.patch.object(sys, "argv", ["ledger.py", "--root", str(self.info["root"]), *args]), \
                    mock.patch.dict(os.environ, fixtures.git_env(), clear=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                try:
                    mod.main()
                    code = 0
                except SystemExit as e:
                    code = e.code
            return code, err.getvalue()

        with mock.patch.object(mod, "scrub_ledger", blocked), mock.patch.object(mod, "BUSY_SECONDS", 0):
            code, err = main("redact", "findings.summary", f"{self.code}:{self.point}", "--reason", "a secret",
                             "--only", secret)
        self.assertEqual(code, 1, err)
        self.assertEqual(main("dump")[0], 0)
        self.assertEqual(sum(self.copies(secret).values()), before - live)
        writer.close()
        line = err.split("run: ")[1].split("; then")[0]
        done = subprocess.run(line, shell=True, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.copies(secret), {})


if __name__ == "__main__":
    unittest.main()
