"""WAL journal mode, the checkpoint in dump, short write transactions (plan P-0001 step 39, the CLI half) and the
final-answer trigger on gates (step 38, DB-H5's schema part).
"""
import sqlite3
import threading
import time
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests._core_helpers import NUMBERED, project, ref
from dev.tests.fixtures import Ledger, run_ledger


def journal_mode(db):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


class Wal(unittest.TestCase):

    def test_init_sets_wal(self):
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        self.assertEqual(journal_mode(L.db), "wal")

    def test_migrate_sets_wal(self):
        project_, db = M.restore_v1()
        self.assertEqual(journal_mode(db), "delete")
        p = run_ledger("--root", project_, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(journal_mode(db), "wal")

    def test_dump_after_wal_checkpoint(self):
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        L.x("INSERT INTO actors(kind, name) VALUES ('owner', 'the last write')")
        L.commit()
        self.assertTrue(L.db.with_name("ledger.sqlite-wal").exists())
        self.assertGreater(L.db.with_name("ledger.sqlite-wal").stat().st_size, 0)
        L.run_ok("dump")
        # The checkpoint moved every commit into ledger.sqlite: a copy of that file alone holds the last write.
        self.assertEqual(L.db.with_name("ledger.sqlite-wal").stat().st_size, 0)
        copy = fixtures.temp_dir("walcopy-") / "copy.sqlite"
        copy.write_bytes(L.db.read_bytes())
        con = sqlite3.connect(str(copy))
        self.assertEqual(con.execute("SELECT count(*) FROM actors WHERE name='the last write'").fetchone()[0], 1)
        con.close()
        self.assertIn("the last write", (L.dir / "ledger.sql").read_text(encoding="utf-8"))

    def test_fill_refs_short_lock(self):
        """fill-refs reads git for every reference first and writes them all in one short transaction, so another
        writer is never kept waiting for the git calls."""
        L, sha, rid, _ = project({"src/a.py": NUMBERED}, test=self)
        L.x("PRAGMA journal_mode = WAL")
        for i in range(300):
            ref(L, rid, "src/a.py", 1 + i % 30, None)
        L.commit()
        import subprocess, sys
        p = subprocess.Popen([sys.executable, str(L.ledger_py), "--root", str(L.root), "fill-refs"],
                             env=fixtures.git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.3)
        waits = []
        con = L.connect()
        con.execute("PRAGMA busy_timeout = 5000")
        while p.poll() is None:
            t0 = time.monotonic()
            con.execute("INSERT INTO actors(kind, name) VALUES ('owner', 'concurrent')")
            con.commit()
            waits.append(time.monotonic() - t0)
            time.sleep(0.05)
        con.close()
        out, err = p.communicate()
        self.assertEqual(p.returncode, 0, out.decode() + err.decode())
        self.assertIn(b"filled 300 references", out)
        self.assertTrue(waits, "fill-refs finished before any concurrent write was tried")
        self.assertLess(max(waits), 1.0, waits)


class GateAnswerFinal(unittest.TestCase):

    def test_answer_cannot_change_once_not_open(self):
        L = Ledger()
        self.addCleanup(L.close)
        gid = L.gate("GATE-0001", options=[{"value": "a", "label": "A"}, {"value": "b", "label": "B"}])
        L.commit()
        # open -> answered (in chat): allowed
        L.x("UPDATE gates SET status='answered', answer_value='a', answered_via='chat' WHERE id=? AND status='open'",
            (gid,))
        L.commit()
        for sql in ("UPDATE gates SET answer_value='b' WHERE id=?", "UPDATE gates SET answer_text='later' WHERE id=?",
                    "UPDATE gates SET answer_value='b', status='open' WHERE id=?"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError) as cm:
                    L.x(sql, (gid,))
                L.con.rollback()
                self.assertIn("the answer of a gate that is no longer open cannot be changed", str(cm.exception))
        # Reopening (status 'open', both answers NULL) is the one way back, and it is recorded with the old answer.
        L.x("UPDATE gates SET status='open', answer_value=NULL, answer_text=NULL WHERE id=?", (gid,))
        L.commit()
        ev = L.one("SELECT from_value, note FROM events WHERE entity_type='gate' AND event='answer-cleared'")
        self.assertEqual(dict(ev), {"from_value": "a", "note": "reopened; the answer was: a: "})
        L.x("UPDATE gates SET status='answered', answer_value='a' WHERE id=?", (gid,))
        L.commit()
        # applied / withdrawn and an applied_note are still fine; the answer stays what the owner said.
        L.x("UPDATE gates SET status='applied', applied_note='done' WHERE id=?", (gid,))
        L.commit()
        self.assertEqual(L.scalar("SELECT answer_value FROM gates WHERE id=?", (gid,)), "a")


if __name__ == "__main__":
    unittest.main()
