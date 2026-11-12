"""What the viewer and the commands say when they run and when they fail (plan P-0001 steps 200, 202, 203): one
line on stderr for each viewer request with no token or query value in it, each gate answer's outcome, nothing for
a client that goes away, one line and exit 2 for a command that cannot run, and a plain sentence for a busy port."""
import io
import re
import socket
import sqlite3
import threading
import time
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ServerThread, seed_demo

LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (GET|POST|HEAD|PUT) (\S+) (\d{3}) (\d+) ms$")


class RequestLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.L.commit()
        cls.srv = ServerThread(cls.L).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.L.close()

    def lines(self, action):
        """The request-log lines one action produced."""
        before = len(self.srv.log)
        action()
        time.sleep(0.05)
        return self.srv.log[before:]

    def test_server_logs_requests_and_errors(self):
        got = self.lines(lambda: self.srv.get("/api/issues?q=secret-words&status=active"))
        self.assertEqual(len(got), 1)
        m = LINE.match(got[0])
        self.assertTrue(m, got[0])
        self.assertEqual((m.group(1), m.group(2), m.group(3)), ("GET", "/api/issues?q&status", "200"))  # names, no values
        self.assertEqual(len(self.lines(lambda: self.srv.get("/", token=False))), 1)
        refused = self.lines(lambda: self.srv.get("/api/overview", token=False))
        self.assertRegex(refused[0], r" GET /api/overview 403 \d+ ms$")
        self.assertRegex(self.lines(lambda: self.srv.get("/api/review/R-9999"))[0], r" 404 \d+ ms$")
        self.assertRegex(self.lines(lambda: self.srv.request("PUT", "/api/overview"))[0], r"PUT /api/overview 405 ")
        # a client that goes away mid-request leaves nothing behind
        def reset():
            s = socket.create_connection(("127.0.0.1", self.srv.port))
            s.sendall(b"GET /api/overview HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % self.srv.port)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            s.close()
        gone = self.lines(reset)
        self.assertFalse([x for x in gone if "Traceback" in x or "ConnectionReset" in x or "BrokenPipe" in x], gone)
        # a request line the library refuses is answered and logged too, with what is known of it
        bad = self.lines(lambda: self.srv.raw(b"GET / HTTP/9.9\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.srv.port))
        self.assertRegex(bad[0], r"Z \S+ \S+ 505 \d+ ms$")
        for line in self.srv.log:
            self.assertNotIn(self.srv.token, line)
            self.assertNotIn("secret-words", line)

    def test_gate_answers_are_logged(self):
        ok = self.lines(lambda: self.srv.post("/api/gates/GATE-0001/answer", {"answer_value": "use-7"}))
        self.assertEqual([x for x in ok if "answer" in x and "POST" not in x], ["answer GATE-0001: saved"])
        self.assertRegex([x for x in ok if "POST" in x][0], r"POST /api/gates/GATE-0001/answer 200 \d+ ms$")
        again = self.lines(lambda: self.srv.post("/api/gates/GATE-0001/answer", {"answer_value": "use-7"}))
        self.assertIn("answer GATE-0001: not saved (409: This question was already answered.)", again)
        bad = self.lines(lambda: self.srv.post("/api/gates/GATE-0002/answer", {"answer_value": "nope", "answer_text": "my private words"}))
        self.assertIn("answer GATE-0002: not saved (400: That choice is not one of the options.)", bad)
        self.assertFalse([x for x in self.srv.log if "my private words" in x])   # what was answered is never logged
        hold = sqlite3.connect(str(self.L.db))
        try:
            hold.execute("BEGIN IMMEDIATE")
            mod_busy = self.lines(lambda: self.srv.post("/api/gates/GATE-0002/answer", {"answer_value": "approve"},
                                                        timeout=20))
        finally:
            hold.rollback()
            hold.close()
        self.assertTrue([x for x in mod_busy if x.startswith("answer GATE-0002: not saved (503:")], mod_busy)

    def test_log_goes_to_stderr(self):
        mod = fixtures.load_ledger_module(fresh=True)
        err, real = io.StringIO(), mod.sys.stderr
        mod.sys.stderr = err
        try:
            mod.log_to_stderr("one line")   # (the fixtures keep each test module's log in memory)
        finally:
            mod.sys.stderr = real
        self.assertEqual(err.getvalue(), "one line\n")


class Serve(unittest.TestCase):
    def serve_in_thread(self, L, open_browser, opened):
        """Run serve() of a private module copy with webbrowser.open recorded; returns (printed lines, stop)."""
        mod = fixtures.load_ledger_module(fresh=True)
        captured, printed = [], []

        class Capturing(mod.ThreadingHTTPServer):
            def __init__(s, *a, **k):
                super().__init__(*a, **k)
                captured.append(s)

        mod.ThreadingHTTPServer = Capturing
        mod.print = lambda *a, **k: printed.append(" ".join(map(str, a)))
        mod.webbrowser.open = opened
        t = threading.Thread(target=lambda: mod.serve(L.root, L.db, 0, open_browser), daemon=True)
        t.start()
        deadline = time.time() + 5
        while (not captured or not printed) and time.time() < deadline:
            time.sleep(0.01)

        def stop():
            captured[0].shutdown()
            captured[0].server_close()
            t.join(5)
        return printed, stop

    def test_no_open_flag(self):
        L = Ledger()
        self.addCleanup(L.close)
        calls = []
        printed, stop = self.serve_in_thread(L, False, lambda url: calls.append(url) or True)
        time.sleep(0.8)
        stop()
        self.assertEqual(calls, [])                      # --no-open: the browser is left alone
        self.assertEqual(len(printed), 1)

    test_no_open_skips_browser = test_no_open_flag

    def test_browser_open_failure_prints_hint(self):
        L = Ledger()
        self.addCleanup(L.close)
        calls = []
        printed, stop = self.serve_in_thread(L, True, lambda url: calls.append(url) and False)
        deadline = time.time() + 5
        while len(printed) < 2 and time.time() < deadline:
            time.sleep(0.02)
        stop()
        self.assertEqual(len(calls), 1)
        self.assertIn("#k=", calls[0])
        self.assertEqual(printed[1], "No browser could be opened from here; open the address above in your browser.")
        # a browser that did open: nothing more is said
        printed, stop = self.serve_in_thread(L, True, lambda url: True)
        time.sleep(0.8)
        stop()
        self.assertEqual(len(printed), 1)

    def test_serve_port_in_use_message(self):
        L = Ledger()
        self.addCleanup(L.close)
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            port = held.getsockname()[1]
            p = L.run("serve", "--port", str(port), "--no-open", timeout=20)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(p.stderr.strip(), f"serve: port {port} is in use; try --port {port + 1}")
        self.assertNotIn("Traceback", p.stderr)


class CliErrors(unittest.TestCase):
    def test_cli_errors_are_one_line(self):
        L = Ledger()
        self.addCleanup(L.close)
        seed_demo(L)
        L.commit()
        junk = L.root / "junk.sqlite"
        junk.write_bytes(b"SQLite format 3\x00" + b"\xff" * 5000)
        probes = [("--db", junk, "gates"), ("--db", junk, "check"), ("--db", junk, "dump"),
                  ("--db", L.root, "gates"),                                   # a folder where the ledger should be
                  ("set-report", "R-0001", L.root / "no-such-file.md"),
                  ("import-answers", L.root / "missing.json")]
        for args in probes:
            p = L.run(*args)
            self.assertNotEqual(p.returncode, 0, args)
            self.assertNotIn("Traceback", p.stderr, args)
            self.assertLessEqual(len([x for x in p.stderr.splitlines() if x.strip()]), 2, (args, p.stderr))
        # an operating-system failure in a command that is not a write: one line, exit 2
        mod = fixtures.load_ledger_module(fresh=True)
        for exc, words in ((PermissionError(13, "Permission denied", "/srv/ledger.sqlite"), "Permission denied"),
                           (OSError(28, "No space left on device"), "No space left on device"),
                           (FileNotFoundError(2, "No such file or directory", "git"), "git"),
                           (mod.subprocess.TimeoutExpired(["git", "show"], 30), "did not finish in 30 s"),
                           (sqlite3.DatabaseError("database disk image is malformed"), "ledger.sql")):
            def boom(e=exc):
                raise e
            mod._main = boom
            err, real = io.StringIO(), mod.sys.stderr
            mod.sys.stderr = err
            try:
                with self.assertRaises(SystemExit) as stop:
                    mod.main()
            finally:
                mod.sys.stderr = real
            self.assertEqual(stop.exception.code, 2, exc)
            said = err.getvalue()
            self.assertEqual(said.count("\n"), 1, said)
            self.assertTrue(said.startswith("ledger.py: "), said)
            self.assertIn(words, said)
            self.assertNotIn("Traceback", said)

    def test_check_reports_corruption_without_traceback(self):
        L = Ledger()
        self.addCleanup(L.close)
        seed_demo(L)
        L.commit()
        L.x("PRAGMA wal_checkpoint(TRUNCATE)")
        L.con.close()
        L.con = None
        raw = bytearray(L.db.read_bytes())
        page = int.from_bytes(raw[16:18], "big") or 65536
        con = sqlite3.connect(str(L.db))
        root = con.execute("SELECT rootpage FROM sqlite_master WHERE name='events'").fetchone()[0]
        con.close()
        start = (root - 1) * page
        raw[start + 8:start + page] = b"\xa5" * (page - 8)        # the events table's first page, turned to noise
        L.db.write_bytes(bytes(raw))
        for args in (("check",), ("check", "--full")):
            p = fixtures.run_ledger("--root", L.root, *args)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertNotIn("Traceback", p.stdout + p.stderr)
            name = "integrity_check" if "--full" in args else "quick_check"
            self.assertRegex(p.stdout, rf"(?m)^{name}: (?!ok)")       # the output of the check that ran


if __name__ == "__main__":
    unittest.main()
