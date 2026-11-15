"""WAL journal mode, the checkpoint in dump, short write transactions (plan P-0001 step 39, the CLI half) and the
final-answer trigger on gates (step 38, DB-H5's schema part).
"""
import os
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
        # Since step 101 fill-refs reads git through one batch process and finishes in well under a second, so
        # the concurrent writes start at once (they used to wait 0.3 s for fill-refs to reach its git calls).
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


class AtomicWrites(unittest.TestCase):
    """Every file the tool writes goes through a temp file of its own: no planted link is followed and two writers
    never share one."""

    def test_write_atomic_refuses_symlinked_tmp(self):
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        fixtures.seed_demo(L)
        L.commit()
        away = fixtures.temp_dir("planted-")
        planted = {}
        for name in ("ledger.sql", "hub.html", "ledger.py", ".gitignore", ".gitattributes"):
            victim = away / f"{name}.victim"
            victim.write_text("keep\n")
            link = L.dir / f"{name}.tmp"
            os.symlink(str(victim), str(link))
            planted[link] = victim
        for cmd in (("init",), ("dump",), ("progress-page",)):
            p = L.run(*cmd)
            self.assertEqual(p.returncode, 0, f"{cmd}: {p.stdout}{p.stderr}")
        for link, victim in planted.items():
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(victim.read_text(), "keep\n", victim)
        # a planted link at a target or at the reviews folder is refused, and nothing is written through it
        hub = L.dir / "hub.html"
        hub.unlink()
        os.symlink(str(away / "hub.html.victim"), str(hub))
        p = L.run("progress-page")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("link", p.stderr)
        self.assertEqual((away / "hub.html.victim").read_text(), "keep\n")
        outside = fixtures.temp_dir("planted-reviews-")
        if (L.dir / "reviews").exists():
            os.rename(str(L.dir / "reviews"), str(away / "reviews-was"))
        os.symlink(str(outside), str(L.dir / "reviews"))
        lcov = away / "cov.lcov"
        lcov.write_text("SF:src/app.py\nDA:1,1\nend_of_record\n")
        p = L.run("tool-run", "coverage", "--review", "R-0001", "--from", str(lcov), "--actor", "A-0001")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(list(outside.rglob("*")), [])

    def test_write_atomic_concurrent_writers(self):
        mod = fixtures.load_ledger_module()
        target = fixtures.temp_dir("atomic-") / "out.bin"
        contents = [bytes([65 + n]) * 3_000_000 for n in range(2)]
        errors, start = [], threading.Barrier(2)

        def write(data):
            start.wait()
            for _ in range(15):
                try:
                    mod.write_atomic(target, data)
                except OSError as e:
                    errors.append(e)
        threads = [threading.Thread(target=write, args=(c,)) for c in contents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(target.read_bytes(), contents)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["out.bin"])

    def test_concurrent_dumps_each_complete(self):
        """Two dumps at once, with a commit between their snapshots, both succeed and leave one whole dump."""
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        fixtures.seed_demo(L)
        L.con.executemany("INSERT INTO actors(kind, name) VALUES ('owner', ?)", [(f"bulk {n}",) for n in range(20000)])
        L.commit()  # big enough that the two dumps overlap
        mod = fixtures.load_ledger_module()
        out = L.db.with_suffix(".sql")
        for round_ in range(3):
            with self.subTest(round=round_):
                cons = [sqlite3.connect(str(L.db), check_same_thread=False) for _ in range(2)]
                writer = sqlite3.connect(str(L.db))
                exits, start = [], threading.Barrier(3)

                def run(con):
                    start.wait()
                    try:
                        mod.dump(con, L.db)
                    except SystemExit as e:
                        exits.append(str(e))
                threads = [threading.Thread(target=run, args=(c,)) for c in cons]
                for t in threads:
                    t.start()
                start.wait()
                writer.execute("INSERT INTO actors(kind, name) VALUES ('owner', ?)", (f"written during {round_}",))
                writer.commit()
                for t in threads:
                    t.join()
                writer.close()
                for c in cons:
                    c.close()
                self.assertEqual(exits, [])
                got = out.read_text(encoding="utf-8")
                self.assertTrue(got.endswith("COMMIT;\n"), got[-200:])
                fresh = sqlite3.connect(":memory:")
                fresh.executescript(got.split("\n", 1)[1])
                self.assertIn(fresh.execute("SELECT count(*) FROM actors WHERE name LIKE 'written during%'")
                              .fetchone()[0], (round_, round_ + 1))
                fresh.close()
                self.assertEqual([p.name for p in L.dir.iterdir() if p.name.startswith("ledger.sql.")], [])


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
