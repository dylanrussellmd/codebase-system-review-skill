"""One UTC time format (plan P-0001 step 75; ISS-0094, ISS-0049): every time column holds
strftime('%Y-%m-%dT%H:%M:%fZ') text, the tr_time_* triggers refuse any other form, migrate 0005 converts stored times
(offsets to UTC), tag-review records commit times in UTC, and v_unchecked_known_issues compares instants."""
import re
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import Ledger, run_ledger

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
BAD = ("2026-11-10T10:00:00Z", "2026-11-10 10:00:00.000", "2026-11-10T10:00:00.000+02:00", "2026-11-10",
       "2026-13-10T10:00:00.000Z", "yesterday")
# Every column that holds a time (names ending in _at, plus commit_time and the event's `at`).
TIME_COLUMN = re.compile(r"(_at|^at|^commit_time)$")


class TimeFormat(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.close)

    def test_now_format_matches_sqlite_strftime(self):
        mod = self.L.module
        ours = mod.now()
        theirs = self.L.scalar("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        self.assertRegex(ours, STAMP)
        self.assertRegex(theirs, STAMP)
        # what a trigger writes has the same shape
        rid = self.L.review(started_at=ours, updated_at=ours)
        self.L.x("UPDATE reviews SET status='abandoned' WHERE id=?", (rid,))
        self.assertRegex(self.L.scalar("SELECT updated_at FROM reviews WHERE id=?", (rid,)), STAMP)
        self.assertEqual(mod.utc_time("2026-11-10T10:00:00+02:00"), "2026-11-10T08:00:00.000Z")
        self.assertEqual(mod.utc_time("2026-11-10T10:00:00.5Z"), "2026-11-10T10:00:00.500Z")
        self.assertEqual(mod.utc_time("2026-11-10 10:00:00"), "2026-11-10T10:00:00.000Z")

    def test_time_columns_reject_other_formats(self):
        L = self.L
        rid = L.review()
        with self.subTest("insert"):
            for v in BAD:
                with self.assertRaises(sqlite3.IntegrityError, msg=v) as cm:
                    L.review(started_at=v)
                self.assertIn("must be UTC times like", str(cm.exception))
        with self.subTest("update"):
            for v in BAD:
                with self.assertRaises(sqlite3.IntegrityError, msg=v):
                    L.x("UPDATE reviews SET approved_at=? WHERE id=?", (v, rid))
            L.x("UPDATE reviews SET approved_at=? WHERE id=?", ("2026-11-10T10:00:00.250Z", rid))
        with self.subTest("other tables"):
            iid = L.issue(first_seen_review_id=rid)
            with self.assertRaises(sqlite3.IntegrityError):
                L.x("UPDATE issues SET resolved_at='2026-11-10T10:00:00Z' WHERE id=?", (iid,))
            with self.assertRaises(sqlite3.IntegrityError):
                L.insert("events", entity_type="issue", entity_id=iid, event="note", at="2026-11-10 10:00")
        with self.subTest("defaults pass"):
            L.x("INSERT INTO actors(kind, name) VALUES ('tool', 'x')")
        with self.subTest("every time column has an insert and an update guard"):
            trig = {r[0]: r[1] for r in L.x("SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                                            "AND name GLOB 'tr_time_*'")}
            tables = [r[0] for r in L.x("SELECT name FROM sqlite_master WHERE type='table' "
                                        "AND name NOT LIKE 'sqlite_%'")]
            for t in tables:
                cols = [r[1] for r in L.x(f'PRAGMA table_info("{t}")') if TIME_COLUMN.search(r[1])]
                for c in cols:
                    for kind in ("ins", "upd"):
                        sql = trig.get(f"tr_time_{t}_{kind}", "")
                        self.assertIn(f"NEW.{c} ", sql, f"{t}.{c} is not guarded on {kind}")

    def test_v_unchecked_timestamp_precision(self):
        L = self.L
        r1 = L.review("R-0001", started_at="2026-11-01T09:00:00.000Z")
        r2 = L.review("R-0002", started_at="2026-11-02T09:00:00.000Z")
        L.review_domain(r2, "spec")
        L.issue("ISS-0001", first_seen_review_id=r1, opened_at="2026-11-02T09:00:00.500Z")  # 0.5 s after
        L.issue("ISS-0002", first_seen_review_id=r1, opened_at="2026-11-02T08:59:59.500Z")  # 0.5 s before
        self.assertEqual([r["issue"] for r in L.all("SELECT issue FROM v_unchecked_known_issues")], ["ISS-0002"])

    def test_v_unchecked_known_issues(self):
        L = self.L
        r1 = L.review("R-0001")
        known = L.issue("ISS-0001", first_seen_review_id=r1, domain_code="spec")
        skipped = L.issue("ISS-0002", first_seen_review_id=r1, domain_code="security")
        dup = L.issue("ISS-0003", first_seen_review_id=r1, domain_code="spec", status="duplicate")
        obs = L.issue("ISS-0004", first_seen_review_id=r1, domain_code="spec", status="obsolete")
        r2 = L.review("R-0002")
        L.review_domain(r2, "spec")
        L.review_domain(r2, "security", decision="skipped", reason="Out of scope.")
        L.issue("ISS-0005", first_seen_review_id=r2, domain_code="spec")  # first seen in this review
        rows = L.all("SELECT review, issue FROM v_unchecked_known_issues")
        self.assertEqual(rows, [{"review": "R-0002", "issue": "ISS-0001"}])
        L.check_row(r2, known)
        self.assertEqual(L.count("v_unchecked_known_issues"), 0)
        del skipped, dup, obs

    def test_seed_check_not_vacuous(self):
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        fixtures.seed_demo(L)
        fixtures.finish_demo(L)
        # one more issue known before R-0002 started (opened with R-0001's issues), in a domain R-0002 ran, with
        # no R-0002 check: the seed's own times make it 'known', so the view and check must see it
        r1, r2 = L.id_of("reviews", "R-0001"), L.id_of("reviews", "R-0002")
        dom = L.scalar("SELECT domain_code FROM review_domains WHERE review_id=? AND decision='included'", (r2,))
        at = L.scalar("SELECT max(opened_at) FROM issues WHERE first_seen_review_id=?", (r1,))
        n = L.scalar("SELECT count(*) FROM issues") + 1
        L.issue(f"ISS-{n:04d}", first_seen_review_id=r1, domain_code=dom, opened_at=at)
        p = L.run("check")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("v_unchecked_known_issues", p.stdout)


class CommitTime(unittest.TestCase):
    def test_commit_time_is_utc(self):
        L = Ledger()
        self.addCleanup(L.close)
        L.repo.write("a.txt", "a\n")
        L.repo.git("add", "-A")
        env = dict(L.repo.env, GIT_AUTHOR_DATE="2026-11-10T10:00:00+02:00",
                   GIT_COMMITTER_DATE="2026-11-10T10:00:00+02:00")
        L.repo.git("commit", "-q", "-m", "zoned", env=env)
        sha = L.repo.head()
        L.repository("demo", ".")
        L.review("R-0001", commit_sha=sha)
        L.run_ok("tag-review", "R-0001")
        self.assertEqual(L.scalar("SELECT commit_time FROM review_repositories"), "2026-11-10T08:00:00.000Z")
        with self.assertRaises(sqlite3.IntegrityError):
            L.x("UPDATE review_repositories SET commit_time='2026-11-10T10:00:00+02:00'")


class MigrateTimes(unittest.TestCase):
    def test_unchecked_known_issues_time_formats(self):
        """A v1 ledger with mixed time text (no milliseconds, a space, an offset) is converted by migrate: every
        stored time then has the one format, and v_unchecked_known_issues gives the right rows."""
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        r2 = con.execute("SELECT id, started_at FROM reviews WHERE code='R-0002'").fetchone()
        con.execute("UPDATE reviews SET started_at='2026-11-05T12:00:00Z' WHERE id=?", (r2[0],))
        # one R-0001 issue opened 0.5 s after R-0002 started, written with an offset (13:00:00.5+01:00 = 12:00:00.5Z)
        con.execute("UPDATE issues SET opened_at='2026-11-05T13:00:00.500+01:00' WHERE id=(SELECT min(id) FROM issues)")
        con.execute("UPDATE review_repositories SET commit_time='2026-11-05 10:00:00'")
        con.execute("UPDATE events SET at='2026-11-05T11:00:00+01:00' WHERE id=(SELECT min(id) FROM events)")
        con.commit()
        con.close()
        p = run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        con = sqlite3.connect(str(db))
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT started_at FROM reviews WHERE code='R-0002'").fetchone()[0],
                         "2026-11-05T12:00:00.000Z")
        self.assertEqual(con.execute("SELECT opened_at FROM issues WHERE id=(SELECT min(id) FROM issues)")
                         .fetchone()[0], "2026-11-05T12:00:00.500Z")
        self.assertEqual(con.execute("SELECT min(at) FROM events WHERE id=(SELECT min(id) FROM events)")
                         .fetchone()[0], "2026-11-05T10:00:00.000Z")
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            for c in [r[1] for r in con.execute(f'PRAGMA table_info("{t}")') if TIME_COLUMN.search(r[1])]:
                for (v,) in con.execute(f'SELECT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL'):
                    self.assertRegex(v, STAMP, f"{t}.{c}")
        first = con.execute("SELECT code FROM issues WHERE id=(SELECT min(id) FROM issues)").fetchone()[0]
        unchecked = [r[0] for r in con.execute("SELECT issue FROM v_unchecked_known_issues WHERE review='R-0002'")]
        self.assertNotIn(first, unchecked)

    def test_migrate_refuses_unreadable_time(self):
        project, db = M.restore_v1()
        con = sqlite3.connect(str(db))
        con.execute("UPDATE reviews SET completed_at='last tuesday' WHERE code='R-0001'")
        con.commit()
        con.close()
        p = run_ledger("--root", project, "migrate")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("a_stored_time_is_not_a_date", p.stderr)
        self.assertIn("nothing was changed", p.stderr)


if __name__ == "__main__":
    unittest.main()
