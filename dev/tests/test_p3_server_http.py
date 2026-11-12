"""The viewer's HTTP layer (plan P-0001 steps 41, 42, 43): the token guard runs on the dispatched path and compares
bytes, POST bodies are parsed strictly with a socket timeout, the token is kept out of the served page (it travels
in the launch URL's #k= fragment), and the page's script is allowed by hash instead of 'unsafe-inline'."""
import base64
import hashlib
import re
import socket
import time
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ServerThread, seed_demo
from dev.tests.test_server import MakeServerThread, http, load_fresh

ROUTES = [("GET", "/api/overview"), ("GET", "/api/gates"), ("GET", "/api/reviews"), ("GET", "/api/review/R-0001"),
          ("GET", "/api/issues"), ("GET", "/api/issue/ISS-0001"), ("GET", "/api/plans"), ("GET", "/api/plan/P-0001"),
          ("GET", "/api/trends"), ("GET", "/api/tables"), ("GET", "/api/table/issues"), ("GET", "/api/source/1"),
          ("POST", "/api/sql"), ("POST", "/api/gates/GATE-0002/answer")]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.srv = MakeServerThread(cls.L).start()
        cls.port, cls.token = cls.srv.port, cls.srv.token

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.L.close()

    def req(self, method, path, token=True, host=None, headers=None, body=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is True:
            h["X-Ledger-Token"] = self.token
        elif token:
            h["X-Ledger-Token"] = token
        if method == "POST":
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        return http(self.port, method, path, h, body)

    def gate_status(self, code):
        return self.L.scalar("SELECT status FROM gates WHERE code=?", (code,))

    def raw(self, data, timeout=5):
        t = time.monotonic()
        out = self.srv.raw(data, timeout=timeout)
        return out, time.monotonic() - t


# ---------------------------------------------------------------- step 41

class TokenGuard(Base):
    def test_post_outside_api_prefix_is_rejected(self):
        for path, body in (("/x/gates/GATE-0002/answer", b'{"answer_value":"approve"}'),
                           ("/x/sql", b'{"sql":"SELECT 1"}'), ("/sql", b"{}"), ("/apix/sql", b"{}")):
            for token in (False, True):
                r = self.req("POST", path, token=token, body=body)
                self.assertIn(r.status, (403, 404), path)
        self.assertEqual(self.gate_status("GATE-0002"), "open")
        for path in ("/x/overview", "/apioverview", "/x/api/overview"):
            self.assertEqual(self.req("GET", path).status, 404, path)

    def test_token_and_host_required(self):
        for method, path in ROUTES:
            body = b"{}" if method == "POST" else None
            for token in (False, "wrong-token-value-xxxxxxxxxxxxxxxxxx", self.token + "x", self.token[:-1]):
                r = self.req(method, path, token=token, body=body)
                self.assertEqual(r.status, 403, f"{method} {path} token={token!r}")
            r = self.req(method, path, host="evil.com", body=body)
            self.assertEqual((r.status, r.json()), (403, {"error": "wrong host"}), f"{method} {path}")
        self.assertEqual(self.gate_status("GATE-0002"), "open")

    def test_token_required_for_api(self):
        self.assertEqual(self.req("GET", "/api/overview", token=False).status, 403)
        r = self.req("GET", "/api/overview", token="nope")
        self.assertEqual(r.status, 403)
        self.assertIn("#k=", r.json()["error"])
        self.assertEqual(self.req("GET", "/api/overview").status, 200)

    test_api_requires_token = test_token_required_for_api

    def test_token_not_required_for_page(self):
        self.assertEqual(self.req("GET", "/", token=False).status, 200)

    def test_non_ascii_token_403(self):
        data = ("GET /api/overview HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Ledger-Token: \xe9\r\n"
                "Connection: close\r\n\r\n" % self.port).encode("latin-1")
        out, took = self.raw(data)
        self.assertTrue(out.startswith(b"HTTP/1.0 403") or out.startswith(b"HTTP/1.1 403"), out[:80])
        self.assertLess(took, 2)
        r = self.req("GET", "/api/overview", token="é" * 3)
        self.assertEqual(r.status, 403)

    test_token_non_ascii_403 = test_non_ascii_token_403


class PostBodies(Base):
    def post_raw(self, length_header, body=b"", timeout=5):
        head = "POST /api/sql HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Ledger-Token: %s\r\nContent-Type: application/json\r\n" \
               % (self.port, self.token)
        if length_header is not None:
            head += "Content-Length: %s\r\n" % length_header
        return self.raw(head.encode() + b"Connection: close\r\n\r\n" + body, timeout=timeout)

    def status(self, out):
        m = re.match(rb"HTTP/1\.[01] (\d{3})", out)
        return int(m.group(1)) if m else None

    def test_post_bad_content_length(self):
        for value, code in (("abc", 400), ("-1", 400), ("1e3", 400), ("+5", 400), (" 5x", 400),
                            ("999999999999", 413)):
            out, took = self.post_raw(value, b'{"sql":"SELECT 1"}')
            self.assertEqual(self.status(out), code, value)
            self.assertLess(took, 2, value)
        self.assertEqual(self.req("GET", "/api/overview").status, 200)  # still serving

    test_bad_content_length_400 = test_post_bad_content_length
    test_post_non_int_length_400 = test_post_bad_content_length
    test_post_negative_length_400 = test_post_bad_content_length

    def test_post_missing_length_is_empty_body(self):
        out, _ = self.post_raw(None)
        self.assertEqual(self.status(out), 400)
        self.assertIn(b"Write a query first.", out)

    def test_post_size_limit(self):
        self.assertEqual(self.req("POST", "/api/sql", body=b"x" * 200_001).status, 413)
        body = b'{"sql":"SELECT 1"}'
        body += b" " * (200_000 - len(body))
        r = self.req("POST", "/api/sql", body=body)
        self.assertEqual((r.status, r.json()["rows"]), (200, [[1]]))

    def test_post_bad_json_400(self):
        r = self.req("POST", "/api/sql", body=b"{nope")
        self.assertEqual((r.status, r.json()), (400, {"error": "bad JSON"}))
        r = self.req("POST", "/api/sql", body=b"")
        self.assertEqual((r.status, r.json()), (400, {"error": "Write a query first."}))  # empty body is {}

    def test_post_non_object_json(self):
        for body in (b"[1,2]", b'"SELECT 1"', b"5", b"null"):
            r = self.req("POST", "/api/sql", body=body)
            self.assertEqual((r.status, r.json()), (400, {"error": "Send a JSON object"}), body)
        r = self.req("POST", "/api/gates/GATE-0002/answer", body=b'["approve"]')
        self.assertEqual(r.status, 400)
        self.assertEqual(self.gate_status("GATE-0002"), "open")


class SlowClient(unittest.TestCase):
    def test_slow_client_times_out(self):
        L = Ledger()
        self.addCleanup(L.close)
        mod = load_fresh()
        self.assertEqual(mod.REQUEST_TIMEOUT, 10)
        mod.REQUEST_TIMEOUT = 0.5
        srv, token = mod.make_server(L.root, L.db, 0)
        srv.daemon_threads = True
        errors = []
        srv.handle_error = lambda *a: errors.append(a)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(("POST /api/sql HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Ledger-Token: %s\r\nContent-Type: "
                   "application/json\r\nContent-Length: 100\r\n\r\n{\"sql\":" % (port, token)).encode())
        t = time.monotonic()
        got = s.recv(1000)  # the server gives up on the half-sent body and closes
        s.close()
        self.assertLess(time.monotonic() - t, 3)
        self.assertTrue(got == b"" or got.startswith(b"HTTP/1.0 400"), got[:60])
        self.assertEqual(errors, [])
        r = http(port, "GET", "/api/overview", {"Host": f"127.0.0.1:{port}", "X-Ledger-Token": token})
        self.assertEqual(r.status, 200)


# ---------------------------------------------------------------- step 42

class TokenOutOfPage(Base):
    def test_page_has_no_token(self):
        page = self.req("GET", "/", token=False).text
        self.assertNotIn(self.token, page)
        self.assertNotIn("@@TOKEN@@", page)
        for cand in set(re.findall(r"[A-Za-z0-9_\-]{32,}", page)):
            self.assertEqual(self.req("GET", "/api/overview", token=cand).status, 403, cand[:12])

    def test_page_token_substituted_and_unique(self):
        mod = load_fresh()
        tokens = set()
        for _ in range(3):
            srv, tok = mod.make_server(self.L.root, self.L.db, 0)
            srv.server_close()
            self.assertGreaterEqual(len(tok), 32)
            tokens.add(tok)
        self.assertEqual(len(tokens), 3)
        self.assertNotIn("@@TOKEN@@", mod.PAGE)

    def test_api_accepts_launch_token(self):
        # serve() prints the launch URL; its #k= fragment is the only place the token appears
        with ServerThread(self.L) as srv:
            url = srv.launch_url
            m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/#k=([A-Za-z0-9_\-]{32,})", url)
            self.assertIsNotNone(m, url)
            self.assertEqual(int(m.group(1)), srv.port)
            r = srv.request("GET", "/api/overview", token=False, headers={"X-Ledger-Token": m.group(2)})
            self.assertEqual(r.status, 200)
            self.assertNotIn(m.group(2), srv.request("GET", "/", token=False).text)

    def test_launch_url_printed_by_cli(self):
        with ServerThread(self.L, mode="process") as srv:
            self.assertRegex(srv.launch_url, r"^http://127\.0\.0\.1:%d/#k=[A-Za-z0-9_\-]{32,}$" % srv.port)
            self.assertEqual(srv.get("/api/overview").status, 200)


@unittest.skipUnless(hasattr(__import__("os"), "umask"), "POSIX permissions only")
class LedgerFilesPrivate(unittest.TestCase):
    def test_serve_sets_private_umask(self):
        import os
        mod = load_fresh()
        seen = []
        real = os.umask
        mod.make_server = lambda *a, **k: (_ for _ in ()).throw(SystemExit(0))
        os.umask = lambda m: seen.append(m) or real(m)
        try:
            with self.assertRaises(SystemExit):
                mod.serve(fixtures.temp_dir("umask-"), fixtures.temp_dir("umask-") / "x.sqlite", 0, False)
        finally:
            os.umask = real
        self.assertEqual(seen[:1], [0o077])

    def test_ledger_files_private(self):
        # init's part of step 42 (umask 077, 0700 folder, 0600 files; done by p3-cli in init())
        L = Ledger(real_init=True)
        self.addCleanup(L.close)
        self.assertEqual(L.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(L.db.stat().st_mode & 0o777, 0o600)
        for name in ("ledger.py", ".gitignore"):
            self.assertEqual((L.dir / name).stat().st_mode & 0o077, 0, name)
        # An existing ledger made with an open umask is made private by the next init.
        import os
        os.chmod(str(L.dir), 0o755)
        os.chmod(str(L.db), 0o644)
        L.run_ok("dump")
        os.chmod(str(L.dir / "ledger.sql"), 0o644)
        L.run_ok("init")
        self.assertEqual(L.dir.stat().st_mode & 0o777, 0o700)
        for name in ("ledger.sqlite", "ledger.sql"):
            self.assertEqual((L.dir / name).stat().st_mode & 0o777, 0o600, name)


# ---------------------------------------------------------------- step 43

class Csp(Base):
    def test_csp_has_no_unsafe_inline_script(self):
        r = self.req("GET", "/", token=False)
        csp = r.headers["Content-Security-Policy"]
        script = re.search(r"script-src ([^;]*)", csp).group(1)
        self.assertNotIn("unsafe-inline", script)
        hashes = re.findall(r"'sha256-([A-Za-z0-9+/=]+)'", script)
        bodies = re.findall(r"<script>(.*?)</script>", r.text, re.S)
        self.assertEqual(len(bodies), 1)
        want = base64.b64encode(hashlib.sha256(bodies[0].encode("utf-8")).digest()).decode()
        self.assertEqual(hashes, [want])
        self.assertNotRegex(r.text, r"<[^>]+\son[a-z]+\s*=", "inline event handler attributes are not covered")

    def test_security_headers(self):
        r = self.req("GET", "/", token=False)
        self.assertIn("text/html", r.headers["Content-Type"])
        csp = r.headers["Content-Security-Policy"]
        for part in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'",
                     "form-action 'none'"):
            self.assertIn(part, csp)
        for k, v in (("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                     ("X-Content-Type-Options", "nosniff")):
            self.assertEqual(r.headers.get(k), v, k)

    def test_csp_on_page_only(self):
        for method, path, token, body in (("GET", "/api/overview", True, None), ("GET", "/api/overview", False, None),
                                          ("GET", "/nope", True, None), ("POST", "/api/sql", True, b"{bad")):
            r = self.req(method, path, token=token, body=body)
            self.assertTrue(r.headers["Content-Type"].startswith("application/json"), path)
            self.assertIsNone(r.headers.get("Content-Security-Policy"), path)
            for k, v in (("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"),
                         ("Referrer-Policy", "no-referrer"), ("X-Content-Type-Options", "nosniff")):
                self.assertEqual(r.headers.get(k), v, f"{k} {path}")


if __name__ == "__main__":
    unittest.main()
