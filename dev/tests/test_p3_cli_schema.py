"""Schema 2 checks (plan P-0001 steps 44, 45, 46, 47 and 51) and migration 0002 that brings them to a v1 ledger.

- step 44: reviews.summary_doc_url, finding_locations.permalink, step_commits.pr_url and
  repositories.permalink_template hold only http(s) URLs.
- step 45: finding_locations.path is relative, with no leading - or /, no backslash, no .. segment and no control
  characters.
- step 46: repositories.path is relative with no .. segment unless allow_outside_root = 1 (D-0007: refuse unless
  listed explicitly).
- step 47: review_repositories.commit_sha and step_commits.commit_sha are full lowercase hex; reviews.code is R-NNNN.
- step 51: finding_locations.excerpt_redacted.
"""
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, run_ledger

SHA = "a" * 40


class SchemaChecks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.L = Ledger(fixtures.temp_dir("p3schema-") / "proj")
        L = cls.L
        cls.rep = L.repository("demo", ".")
        cls.rid = L.review("R-0001", commit_sha=SHA, repository_id=cls.rep)
        cls.fid = L.finding(cls.rid)
        cls.pid = L.plan("P-0001")
        cls.ph = L.phase(cls.pid, 1)
        cls.sid = L.step(cls.pid, cls.ph, 1)
        L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def refused(self, fn):
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            fn()
        self.L.con.rollback()
        self.assertIn("CHECK constraint failed", str(cm.exception))

    def accepted(self, fn):
        fn()
        self.L.con.rollback()

    def test_url_columns_check(self):
        L = self.L
        writes = {
            "reviews.summary_doc_url": lambda u: L.x("UPDATE reviews SET summary_doc_url=? WHERE id=?", (u, self.rid)),
            "finding_locations.permalink": lambda u: L.location(self.fid, "src/a.py", 1, permalink=u),
            "step_commits.pr_url": lambda u: L.step_commit(self.sid, SHA, repository_id=self.rep, pr_url=u),
            "repositories.permalink_template": lambda u: L.repository("other", "sub", permalink_template=u),
        }
        for col, write in writes.items():
            for bad in ("javascript:x", "JavaScript:alert(1)", "data:text/html,x", " https://x", "file:///etc/passwd",
                        "//evil.example/", "HTTPS://upper.example/"):
                with self.subTest(col=col, url=bad):
                    self.refused(lambda: write(bad))
            for good in ("https://github.com/o/r/blob/x", "http://localhost:3000/x", None):
                with self.subTest(col=col, url=good):
                    self.accepted(lambda: write(good))

    def test_location_path_check(self):
        L = self.L
        for bad in ("../x", "a/../../x", "a/..", "..", "-x", "/etc/x", "a\\b", "a\x01b", "a\nb", "a\x7fb", ""):
            with self.subTest(path=bad):
                self.refused(lambda: L.location(self.fid, bad, 1))
        for good in ("src/a.py", "a$(touch X)'b;c", "a..b/c", ".github/x.yml", "dir with space/f", "./x"):
            with self.subTest(path=good):
                self.accepted(lambda: L.location(self.fid, good, 1))

    def test_repository_path_check(self):
        L = self.L
        for bad in ("../x", "/x", "a/../../x", "..", "a\\b", "-C", "a\x01b"):
            with self.subTest(path=bad):
                self.refused(lambda: L.repository("bad", bad))
        # '.' is the setup repository's own path: one repository per path (schema 3, plan step 64)
        for good in ("", None, "sub", "sub/repo"):
            with self.subTest(path=good):
                self.accepted(lambda: L.repository("good", good))
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            L.repository("again", ".")
        L.con.rollback()
        self.assertIn("UNIQUE constraint failed: repositories.path", str(cm.exception))
        # Listed explicitly (D-0007): a sibling checkout may be outside the root, but never look like an option.
        for good in ("../sibling", "/abs/checkout"):
            with self.subTest(outside=good):
                self.accepted(lambda: L.repository("sib", good, allow_outside_root=1))
        self.refused(lambda: L.repository("sib", "-x", allow_outside_root=1))
        self.refused(lambda: L.repository("sib", "x", allow_outside_root=2))

    def test_review_code_check(self):
        L = self.L
        for bad in ("../x", "R-1", "R-12a4", "R-0001 ", "--message=x", "r-0001", "R-"):
            with self.subTest(code=bad):
                self.refused(lambda: L.review(bad))
        for good in ("R-0002",):
            with self.subTest(code=good):
                self.accepted(lambda: L.review(good))
        # schema 3 (plan step 61): codes are also sequential, so a well-formed code out of sequence is refused
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            L.review("R-12345")
        L.con.rollback()
        self.assertIn("sequential", str(cm.exception))

    def test_commit_sha_checks(self):
        L = self.L
        rid2 = L.review()
        for bad in ("--message=x" + "0" * 29, "A" * 40, "abc1234", "g" * 40):
            with self.subTest(sha=bad):
                self.refused(lambda: L.review_repository(rid2, bad, repository_id=self.rep))
                self.refused(lambda: L.step_commit(self.sid, bad, repository_id=self.rep))
        L.con.rollback()
        for good in ("0123456789abcdef0123456789abcdef01234567", "f" * 64):
            with self.subTest(sha=good):
                self.accepted(lambda: L.step_commit(self.sid, good, repository_id=self.rep))

    def test_excerpt_redacted_column(self):
        L = self.L
        lid = L.location(self.fid, "src/a.py", 1)
        self.assertEqual(L.scalar("SELECT excerpt_redacted FROM finding_locations WHERE id=?", (lid,)), 0)
        self.refused(lambda: L.location(self.fid, "src/a.py", 1, excerpt_redacted=2))


class Migration0002(unittest.TestCase):
    """test_migrate_0002: a v1 ledger gains the checks; planted non-http links are dropped; a row that breaks a check
    makes migrate fail and change nothing."""

    def test_migrate_v1_drops_non_http_links_and_matches_fresh(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE finding_locations SET permalink='javascript:alert(1)' WHERE id=(SELECT min(id) FROM "
                    "finding_locations)")
        con.execute("UPDATE reviews SET summary_doc_url='javascript:x'")
        con.execute("UPDATE step_commits SET pr_url='https://example.invalid/pr/1'")
        con.commit()
        n_links = con.execute("SELECT count(*) FROM finding_locations WHERE permalink GLOB 'http*'").fetchone()[0]
        con.close()
        before = M.counts(db)
        p = run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0002-checks-and-columns", p.stdout)
        fresh = fixtures.temp_dir("fresh-") / "proj"
        fresh.mkdir()
        self.assertEqual(run_ledger("--root", fresh, "init").returncode, 0)
        self.assertEqual(M.objects(db), M.objects(fresh / ".code-review" / "ledger.sqlite"))
        after = M.counts(db)
        for t, n in before.items():
            if t not in ("meta", "schema_migrations"):
                self.assertEqual(after[t], n + M.SEEDED_LATER.get(t, 0), t)
        con = sqlite3.connect(str(db))
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM finding_locations WHERE permalink GLOB 'http*'")
                             .fetchone()[0], n_links)
            self.assertEqual(con.execute("SELECT count(*) FROM finding_locations WHERE permalink NOT GLOB 'http*'")
                             .fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM reviews WHERE summary_doc_url IS NOT NULL")
                             .fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT DISTINCT pr_url FROM step_commits").fetchall(),
                             [("https://example.invalid/pr/1",)])
            self.assertEqual(con.execute("SELECT DISTINCT excerpt_redacted FROM finding_locations").fetchall(), [(0,)])
            self.assertEqual(con.execute("SELECT DISTINCT allow_outside_root FROM repositories").fetchall(), [(0,)])
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            con.close()
        self.assertEqual(run_ledger("--root", project, "check").returncode, 0)

    def test_migrate_v1_with_bad_path_changes_nothing(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE finding_locations SET path='../../etc/passwd' WHERE id=(SELECT min(id) FROM "
                    "finding_locations)")
        con.commit()
        con.close()
        before = db.read_bytes()
        p = run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("0002-checks-and-columns failed: CHECK constraint failed", p.stderr)
        self.assertIn("nothing was changed", p.stderr)
        self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
