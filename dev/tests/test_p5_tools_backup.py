"""restore and merge, and the ledger marked binary for git (plan P-0001 step 122; DB-M9, DOC-M8, ISS-0057).

test_dump_restore_roundtrip          dump, then restore into a new file: equal per-table counts, check clean on both
test_restore_refuses_existing_target restore never writes over an existing file
test_restore_refuses_attach          a dump that tries ATTACH (writing another file) is refused, nothing written
test_merge_two_branches              two copies changed apart: both sides' reviews and issues exist, clashing codes
                                     renumbered with a printed mapping, codes unique, no duplicate events, check 0
test_merge_refuses_bad_result        a merge whose result fails check leaves the ledger unchanged
test_gitattributes_written_when_committed  init writes .code-review/.gitattributes (ledger.sqlite binary -diff -merge)
"""
import shutil
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger


def counts(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%'")]
        return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def snapshot(L, path):
    """A consistent copy of L's ledger at path (as the other side of a git merge would be)."""
    L.commit()
    dst = sqlite3.connect(str(path))
    L.con.backup(dst)
    dst.close()
    return path


class Restore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        fixtures.seed_demo(cls.L)
        fixtures.finish_demo(cls.L)

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_dump_restore_roundtrip(self):
        L = self.L
        L.run_ok("dump")
        sql = L.db.with_suffix(".sql")
        new = L.root / "restored" / "ledger.sqlite"
        p = L.run_ok("restore", str(sql), "--to", str(new))
        self.assertIn(f"restored {sql} into {new}", p.stdout)
        self.assertIn("check: clean", p.stdout)
        self.assertEqual(counts(new), counts(L.db))
        self.assertEqual(new.stat().st_mode & 0o777, 0o600)
        a = L.run("check", "--quiet")
        b = L.run("--db", str(new), "check", "--quiet")
        self.assertEqual((a.returncode, b.returncode), (0, 0), a.stdout + b.stdout)

    def test_restore_refuses_existing_target(self):
        L = self.L
        L.run_ok("dump")
        before = L.db.read_bytes()
        p = L.run("restore", str(L.db.with_suffix(".sql")), "--to", str(L.db))
        self.assertEqual(p.returncode, 1)
        self.assertIn("exists; restore writes a new file only", p.stderr)
        self.assertEqual(L.db.read_bytes(), before)

    def test_restore_refuses_attach(self):
        L = self.L
        bad = L.root / "bad.sql"
        evil = L.root / "evil.sqlite"
        bad.write_text(L.module.DUMP_HEADER + "\nBEGIN TRANSACTION;\nATTACH DATABASE '" + str(evil) + "' AS e;\n"
                       "CREATE TABLE e.x(y);\nCOMMIT;\n")
        new = L.root / "never.sqlite"
        p = L.run("restore", str(bad), "--to", str(new))
        self.assertEqual(p.returncode, 1)
        self.assertIn("did not load", p.stderr)
        self.assertFalse(new.exists())
        self.assertFalse(evil.exists())


class Merge(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.addCleanup(self.L.repo.cleanup)
        self.addCleanup(self.L.close)
        self.ids = fixtures.seed_demo(self.L)
        fixtures.finish_demo(self.L)

    def branch_work(self, L, title, step_note=None):
        """One side's work after the branches split: a review with a finding matched to a new issue, a gate."""
        c2 = self.ids["commits"][1]
        rid = L.review(title=title)
        L.review_repository(rid, c2, tag_name="review/R-0002")  # the tag finish_demo made at that commit
        fid = L.finding(rid, point_id="SPEC-M1", summary=f"{title} finding.")
        L.location(fid, "src/auth.py", 1, 1, excerpt="x", excerpt_sha256="0" * 64, blob_sha="1" * 40,
                   permalink="https://example.invalid/x")
        iid = L.issue(first_seen_review_id=rid, title=f"{title} issue")
        L.match(fid, iid)
        L.gate(title=f"{title} question")
        if step_note:
            L.x("UPDATE steps SET progress_note=?, updated_at=? WHERE id=?",
                (step_note, L.clock.tick(), self.ids["steps"][1]))
        L.commit()

    def test_merge_two_branches(self):
        L = self.L
        base_events = L.scalar("SELECT count(*) FROM events")
        theirs = snapshot(L, L.root / "theirs.sqlite")
        self.branch_work(L, "Ours")
        ours_events = L.scalar("SELECT count(*) FROM events")
        # the other branch: the same helpers on a connection to the other copy
        mine = L.con
        L.con = sqlite3.connect(str(theirs))
        L.con.row_factory = sqlite3.Row
        L.con.execute("PRAGMA foreign_keys = ON")
        self.branch_work(L, "Theirs", step_note="Moved on the other branch.")
        their_events = L.scalar("SELECT count(*) FROM events")
        L.con.close()
        L.con = mine

        p = L.run("merge", str(theirs))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("reviews: the other ledger's R-0003 is now R-0004 (both sides made R-0003)", p.stdout)
        self.assertIn("issues: the other ledger's ISS-0005 is now ISS-0006", p.stdout)
        self.assertIn("gates: the other ledger's GATE-0003 is now GATE-0004", p.stdout)
        self.assertIn("ledger.sqlite.bak-merge", p.stdout)
        titles = {r["code"]: r["title"] for r in L.all("SELECT code, title FROM reviews")}
        self.assertEqual(titles["R-0003"], "Ours")
        self.assertEqual(titles["R-0004"], "Theirs")
        issues = {r["title"] for r in L.all("SELECT title FROM issues")}
        self.assertTrue({"Ours issue", "Theirs issue"} <= issues)
        for t in ("reviews", "issues", "gates", "plans", "decisions"):
            self.assertEqual(L.scalar(f"SELECT count(*) - count(DISTINCT code) FROM {t}"), 0, t)
        # the Theirs finding points at the Theirs issue and review
        f = L.one("""SELECT r.code review, i.code issue FROM findings f JOIN reviews r ON r.id=f.review_id
                     JOIN issues i ON i.id=f.issue_id WHERE f.summary='Theirs finding.'""")
        self.assertEqual((f["review"], f["issue"]), ("R-0004", "ISS-0006"))
        # no duplicate events: ours plus the ones only the other side wrote
        self.assertEqual(L.scalar("SELECT count(*) FROM events"), ours_events + their_events - base_events)
        self.assertEqual(L.scalar("""SELECT count(*) FROM (SELECT entity_type, entity_id, event, from_value, to_value,
                                     at FROM events GROUP BY 1,2,3,4,5,6 HAVING count(*) > 1)"""), 0)
        # the step the other side moved later is taken from it (newer updated_at)
        self.assertEqual(L.scalar("SELECT progress_note FROM steps WHERE id=?", (self.ids["steps"][1],)),
                         "Moved on the other branch.")
        c = L.run("check")
        self.assertEqual(c.returncode, 0, c.stdout)
        self.assertTrue(L.db.with_name("ledger.sqlite.bak-merge").exists())
        # merging the same file again adds nothing new beyond what it already holds
        before = counts(L.db)
        L.run_ok("merge", str(theirs))
        after = counts(L.db)
        self.assertEqual(after["events"], before["events"])

    def test_merge_refuses_bad_result(self):
        L = self.L
        theirs = snapshot(L, L.root / "theirs.sqlite")
        con = sqlite3.connect(str(theirs))
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("INSERT INTO review_goals(review_id, goal_id) VALUES (999, 999)")
        con.commit()
        con.close()
        L.commit()
        before = counts(L.db)
        p = L.run("merge", str(theirs))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("not applied", p.stdout)
        self.assertEqual(counts(L.db), before)


class GitAttributes(unittest.TestCase):
    def test_gitattributes_written_when_committed(self):
        repo = fixtures.ScratchRepo()
        self.addCleanup(repo.cleanup)
        L = Ledger(repo, real_init=True)
        self.addCleanup(L.close)
        ga = L.dir / ".gitattributes"
        self.assertTrue(ga.is_file())
        self.assertIn("ledger.sqlite binary -diff -merge", ga.read_text())
        out = repo.git("check-attr", "merge", "diff", "--", ".code-review/ledger.sqlite")
        self.assertIn("merge: unset", out)
        self.assertIn("diff: unset", out)
        # init again keeps the owner's own lines and adds nothing twice
        ga.write_text(ga.read_text() + "*.md text\n")
        L.run_ok("init")
        text = ga.read_text()
        self.assertEqual(text.count("ledger.sqlite binary -diff -merge"), 1)
        self.assertIn("*.md text", text)


if __name__ == "__main__":
    unittest.main()
