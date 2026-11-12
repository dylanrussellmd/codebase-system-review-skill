"""Gate answers in the viewer (plan P-0001 step 38): the answer is read and written in one BEGIN IMMEDIATE
transaction, a lost race is a 409 and a locked ledger a 503, and nothing is reported saved unless it was stored."""
import sqlite3
import threading
import time
import unittest

from dev.tests.fixtures import Ledger, seed_demo
from dev.tests.test_server import MakeServerThread, load_fresh


class GateAnswer(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        seed_demo(self.L)
        self.L.commit()
        self.addCleanup(self.L.close)

    def gate(self, code):
        con = self.L.connect()
        try:
            return dict(con.execute("SELECT * FROM gates WHERE code=?", (code,)).fetchone())
        finally:
            con.close()

    def events(self, gate_id):
        return [tuple(r) for r in self.L.con.execute(
            "SELECT event, from_value, to_value, note FROM events WHERE entity_type='gate' AND entity_id=?", (gate_id,))]

    def answer(self, mod, code, body):
        res = mod.api(self.L.root, self.L.db, f"/api/gates/{code}/answer", {}, body)
        return (res[1], res[0]) if isinstance(res, tuple) else (200, res)

    def test_answer_success_records_and_logs(self):
        mod = load_fresh()
        gid = self.gate("GATE-0001")["id"]
        self.assertEqual(self.answer(mod, "GATE-0001", {"answer_value": "use-7", "answer_text": "Seven."}),
                         (200, {"ok": True}))
        g = self.gate("GATE-0001")
        self.assertEqual((g["status"], g["answer_value"], g["answer_text"], g["answered_by"], g["answered_via"]),
                         ("answered", "use-7", "Seven.", "owner", "viewer"))
        self.assertIsNotNone(g["answered_at"])
        self.assertIn(("status-changed", "open", "answered", "use-7: Seven."), self.events(gid))

    def test_answer_concurrent_second_gets_409(self):
        mod = load_fresh()
        real_one = mod.one

        def slow_one(con, sql, args=()):
            r = real_one(con, sql, args)
            if "FROM gates WHERE code=?" in sql:
                time.sleep(0.3)  # widen the window between the read and the write
            return r

        mod.one = slow_one
        out = {}
        threads = [threading.Thread(target=lambda v=v: out.__setitem__(v, self.answer(mod, "GATE-0002",
                                                                                         {"answer_value": v})))
                   for v in ("approve", "add")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        statuses = sorted(s for s, _ in out.values())
        self.assertEqual(statuses, [200, 409], out)
        winner = [v for v, (s, _) in out.items() if s == 200][0]
        loser = [res for s, res in out.values() if s == 409][0]
        self.assertEqual(loser, {"error": "This question was already answered."})
        self.assertEqual(self.gate("GATE-0002")["answer_value"], winner)

    test_gate_answer_concurrent_second_gets_409 = test_answer_concurrent_second_gets_409
    test_gate_answer_race_second_gets_409 = test_answer_concurrent_second_gets_409

    def test_answer_race_rowcount(self):
        # the read says open but the row is already answered (a stale read): the UPDATE changes no row -> 409
        mod = load_fresh()
        con = self.L.connect()
        con.execute("UPDATE gates SET status='answered', answer_value='approve', answered_by='owner', "
                    "answered_via='chat' WHERE code='GATE-0002'")
        con.commit()
        con.close()
        real_one = mod.one

        def stale(c, sql, args=()):
            r = real_one(c, sql, args)
            if r and "FROM gates WHERE code=?" in sql:
                r = dict(r, status="open")
            return r

        mod.one = stale
        self.assertEqual(self.answer(mod, "GATE-0002", {"answer_value": "add"}),
                         (409, {"error": "This question was already answered."}))
        g = self.gate("GATE-0002")
        self.assertEqual((g["answer_value"], g["answered_via"]), ("approve", "chat"))

    test_answer_race_returns_conflict = test_answer_race_rowcount
    test_gate_answer_race_loses_nothing = test_answer_race_rowcount

    def test_answer_while_locked_returns_503(self):
        mod = load_fresh()
        mod.ANSWER_BUSY_MS = 300
        holder = sqlite3.connect(str(self.L.db), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            t = time.monotonic()
            self.assertEqual(self.answer(mod, "GATE-0001", {"answer_value": "use-7"}),
                             (503, {"error": "The ledger is busy; your answer was not saved, try again."}))
            self.assertLess(time.monotonic() - t, 3)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(self.gate("GATE-0001")["status"], "open")
        self.assertEqual(self.answer(mod, "GATE-0001", {"answer_value": "use-7"}), (200, {"ok": True}))

    def test_answer_validation_leaves_no_transaction(self):
        mod = load_fresh()
        self.assertEqual(self.answer(mod, "GATE-0001", {"answer_value": "nope"})[0], 400)
        self.assertEqual(self.answer(mod, "GATE-9999", {"answer_value": "x"})[0], 404)
        # nothing left holding the write lock
        w = sqlite3.connect(str(self.L.db), timeout=0.2)
        w.execute("UPDATE meta SET value=value WHERE key='project_name'")
        w.commit()
        w.close()

    def test_answer_over_http_conflict(self):
        with MakeServerThread(self.L) as srv:
            self.assertEqual(srv.post("/api/gates/GATE-0001/answer", {"answer_value": "use-7"}).status, 200)
            r = srv.post("/api/gates/GATE-0001/answer", {"answer_value": "keep-14"})
            self.assertEqual((r.status, r.json()), (409, {"error": "This question was already answered."}))


if __name__ == "__main__":
    unittest.main()
