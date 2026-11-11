"""Every viewer API failure is one JSON {"error": sentence} with an accurate status (plan P-0001: RES-M8, SRV-L1,
SRV-R9, DB-X3, TEST-T98, TEST-T107, TEST-T108).

test_api_errors_are_plain                     bad numbers and types are 400 with a plain sentence, no Python text
test_source_unknown_id_404                    /api/source/<unknown id> is 404
test_unknown_api_paths_404                    unknown paths and extra segments are 404 JSON
test_get_unknown_path_404                     /favicon.ico and other non-API paths are 404 JSON
test_route_exception_500_and_server_survives  a failing route is 500 with a fixed sentence; the detail goes to stderr
test_lock_error_is_503                        a locked or busy ledger is 503 with the busy sentence
test_stdlib_errors_are_json                   errors the HTTP library raises itself are JSON with the security headers
"""
import contextlib
import io
import sqlite3
import sys
import threading
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ServerThread, seed_demo

SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Cache-Control": "no-store"}
PYTHON_TEXT = ("int(", "invalid literal", "object has no attribute", "sqlite", "Traceback", "Error:", "NoneType")
BUSY = "The ledger is busy (the agent is writing to it); try again in a moment."
FAILED = "Something went wrong; see the terminal where ledger.py serve is running."


class Viewer(ServerThread):
    """The viewer started with make_server(), keeping the module so a test can replace route()."""

    def start(self):
        self.module = mod = fixtures.load_ledger_module(fresh=True)
        mod.print = lambda *a, **k: None
        self._srv, self._token = mod.make_server(self.root, self.db, 0)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, name=f"viewer-{self.port}", daemon=True)
        self._thread.start()
        return self


class ApiErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.srv = Viewer(cls.L).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.L.close()

    def assertJsonError(self, r, status, what):
        self.assertEqual(r.status, status, f"{what}: {r!r}")
        self.assertTrue(r.headers["Content-Type"].startswith("application/json"), what)
        body = r.json()
        self.assertEqual(set(body), {"error"}, what)
        self.assertTrue(body["error"].strip(), what)
        for k, v in SECURITY_HEADERS.items():
            self.assertEqual(r.headers.get(k), v, f"{k} on {what}")
        return body["error"]

    def test_api_errors_are_plain(self):
        probes = (("GET", "/api/source/abc", None), ("GET", "/api/source/1.5", None),
                  ("GET", "/api/table/issues?limit=abc", None), ("GET", "/api/table/issues?offset=x", None),
                  ("POST", "/api/sql", {"sql": 5}), ("POST", "/api/sql", {"sql": ["SELECT 1"]}),
                  ("POST", "/api/gates/GATE-0001/answer", {"answer_value": 3}),
                  ("POST", "/api/gates/GATE-0001/answer", {"answer_text": {"a": 1}}))
        for method, path, body in probes:
            with self.subTest(path=path, body=body):
                message = self.assertJsonError(self.srv.request(method, path, body=body), 400, path)
                for text in PYTHON_TEXT:
                    self.assertNotIn(text, message)
        self.assertEqual(self.srv.get("/api/table/issues?limit=abc").json(), {"error": "limit must be a whole number."})
        self.assertEqual(self.srv.get("/api/source/abc").json(), {"error": "The reference id must be a whole number."})
        self.assertEqual(self.srv.post("/api/sql", {"sql": 5}).json(), {"error": "sql must be text."})
        self.assertEqual(self.L.scalar("SELECT status FROM gates WHERE code='GATE-0001'"), "open")

    def test_source_unknown_id_404(self):
        r = self.srv.get("/api/source/99999")
        self.assertEqual(self.assertJsonError(r, 404, "source"), "no such reference")
        lid = self.L.scalar("SELECT min(id) FROM finding_locations")
        self.assertEqual(self.srv.get(f"/api/source/{lid}").status, 200)

    def test_unknown_api_paths_404(self):
        for path in ("/api/nothing", "/api/", "/api/review/R-0001/x", "/api/overview/extra", "/api/gates/GATE-0001",
                     "/api/issue", "/api/table/issues/rows"):
            with self.subTest(path=path):
                self.assertEqual(self.assertJsonError(self.srv.get(path), 404, path), "not found")
        r = self.srv.post("/api/nothing", {})
        self.assertEqual(self.assertJsonError(r, 404, "POST /api/nothing"), "not found")

    def test_get_unknown_path_404(self):
        for path in ("/favicon.ico", "/index.html", "/robots.txt", "/x/overview"):
            with self.subTest(path=path):
                r = self.srv.get(path, token=False)
                self.assertEqual(self.assertJsonError(r, 404, path), "not found")

    def failing(self, exc):
        """Replace route() with one that raises exc; returns (response, what the server wrote to stderr)."""
        mod, real = self.srv.module, self.srv.module.route

        def boom(root, db, con, path, qs, body):
            raise exc

        mod.route = boom
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                r = self.srv.get("/api/overview")
        finally:
            mod.route = real
        return r, err.getvalue()

    def test_route_exception_500_and_server_survives(self):
        r, logged = self.failing(RuntimeError("secret detail 'int' object has no attribute"))
        self.assertEqual(self.assertJsonError(r, 500, "500"), FAILED)
        self.assertNotIn("secret detail", r.text)
        # the terminal gets what failed: the request, the exception type and the traceback
        self.assertIn("GET /api/overview", logged)
        self.assertIn("Traceback", logged)
        self.assertIn("RuntimeError: secret detail", logged)
        self.assertEqual(self.srv.get("/api/overview").status, 200)
        r, logged = self.failing(sqlite3.OperationalError("no such table: nope"))
        self.assertEqual(self.assertJsonError(r, 500, "sqlite error"), FAILED)
        self.assertIn("no such table: nope", logged)

    def test_lock_error_is_503(self):
        for text in ("database is locked", "database table is locked: issues", "database is busy"):
            with self.subTest(text=text):
                r, logged = self.failing(sqlite3.OperationalError(text))
                self.assertEqual(self.assertJsonError(r, 503, text), BUSY)
                self.assertEqual(logged, "", "a busy ledger is not a fault to log")
        self.assertEqual(self.srv.get("/api/overview").status, 200)

    def test_stdlib_errors_are_json(self):
        host = f"Host: 127.0.0.1:{self.srv.port}\r\n".encode()
        for data, status in ((b"GET / HTTP/9.9\r\n" + host + b"\r\n", 505),
                             (b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n" + host + b"\r\n", 414),
                             (b"BREW / HTTP/1.1\r\n" + host + b"\r\n", 501)):
            with self.subTest(status=status):
                raw = self.srv.raw(data)
                self.assertNotIn(b"<html", raw.lower())
                if not raw.startswith(b"HTTP/"):
                    # before Python 3.12 the library answers an unknown HTTP version in HTTP/0.9 style: body only
                    self.assertEqual(set(fixtures.json.loads(raw)), {"error"})
                    continue
                head, _, body = raw.partition(b"\r\n\r\n")
                self.assertRegex(head[:15], rb"^HTTP/1\.[01] %d" % status)
                lower = head.lower()
                self.assertIn(b"content-type: application/json", lower)
                for k, v in SECURITY_HEADERS.items():
                    self.assertIn(f"{k}: {v}".lower().encode(), lower)
                self.assertNotIn(b"<html", raw.lower())
                self.assertEqual(set(fixtures.json.loads(body)), {"error"})


if __name__ == "__main__":
    unittest.main()
