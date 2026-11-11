"""The viewer's HTTP server over real HTTP: make_server, Host check, CORS, security headers, the token guard and a
gate answered in the viewer as the CLI sees it. Plus static checks on dev/page.html (label maps cover the schema's
enum values; no outside URLs and no HTML injection sinks).

These are the security-test baseline (plan P-0001 steps 17 and 21). Tests named test_current_... or marked
'# characterization:' pin today's behaviour including known defects; the fix step for the named issue flips them.
"""
import re
import sys
import threading
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ServerThread, seed_demo

ROOT = fixtures.REPO_ROOT
PAGE_HTML = (ROOT / "dev" / "page.html").read_text(encoding="utf-8")
SCHEMA_SQL = (ROOT / "references" / "schema.sql").read_text(encoding="utf-8")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class MakeServerThread(ServerThread):
    """ServerThread that starts the viewer through make_server(root, db, 0) instead of serve(), so the server binds
    a free port itself (no race between free_port() and bind) and the token comes back from make_server."""

    def start(self):
        mod = load_fresh()
        self.module = mod
        self._srv, self._token = mod.make_server(self.root, self.db, 0)
        self._srv.daemon_threads = True
        # handler exceptions are recorded instead of printed (tests that expect one assert on handler_errors)
        self.handler_errors = []
        self._srv.handle_error = lambda request, addr: self.handler_errors.append(sys.exc_info()[1])
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, name=f"viewer-{self.port}", daemon=True)
        self._thread.start()
        return self


def load_fresh():
    mod = fixtures.load_ledger_module(fresh=True)
    mod.print = lambda *a, **k: None
    return mod


_seeded = None


def seeded_ledger():
    """One demo ledger shared by the read-only tests in this module (tests that write make their own)."""
    global _seeded
    if _seeded is None:
        L = Ledger()
        seed_demo(L)
        _seeded = L
    return _seeded


def http(port, method, path, headers=None, body=None):
    """A request with exactly the given headers (http.client adds Host unless skip_host; we control it)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        return fixtures.Response(r.status, r.headers, r.read())
    finally:
        c.close()


def tearDownModule():
    if _seeded is not None:
        _seeded.close()


# ---------------------------------------------------------------- step 17: make_server

class MakeServer(unittest.TestCase):
    def test_make_server_binds_free_port_and_serves_index(self):
        L = seeded_ledger()
        mod = load_fresh()
        self.assertTrue(callable(getattr(mod, "make_server", None)), "core.make_server is missing")
        srv, token = mod.make_server(L.root, L.db, 0)
        try:
            host, port = srv.server_address[:2]
            self.assertEqual(host, "127.0.0.1")
            self.assertNotEqual(port, 0, "port=0 should bind a free port")
            self.assertRegex(token, r"^[A-Za-z0-9_\-]{16,}$")
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            page = http(port, "GET", "/", {"Host": f"127.0.0.1:{port}"})
            self.assertEqual(page.status, 200)
            self.assertIn("text/html", page.headers["Content-Type"])
            self.assertIn(f'const TOKEN = "{token}";', page.text)
            # the Host allow-list follows the port actually bound, not the 0 that was asked for
            self.assertEqual(http(port, "GET", "/", {"Host": "127.0.0.1:0"}).status, 403)
            ov = http(port, "GET", "/api/overview", {"Host": f"localhost:{port}", "X-Ledger-Token": token})
            self.assertEqual(ov.status, 200)
            self.assertEqual(ov.json()["project"], L.root.name)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_make_server_tokens_differ_per_server(self):
        L = seeded_ledger()
        mod = load_fresh()
        a, ta = mod.make_server(L.root, L.db, 0)
        b, tb = mod.make_server(L.root, L.db, 0)
        a.server_close(); b.server_close()
        self.assertNotEqual(ta, tb)

    def test_serve_still_works_through_fixture(self):
        # fixtures.ServerThread patches the module-level ThreadingHTTPServer and calls serve(); that must keep working
        with ServerThread(seeded_ledger()) as srv:
            self.assertEqual(srv.get("/api/overview").status, 200)


# ---------------------------------------------------------------- step 17: static page checks

def schema_enums():
    """{(table, column): [values]} for every `CHECK (col IN ('a','b',...))` in schema.sql."""
    out = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", SCHEMA_SQL, re.S):
        table, body = m.group(1), m.group(2)
        for c in re.finditer(r"CHECK \((\w+) IN\s*\(([^)]*)\)\)", body):
            vals = re.findall(r"'([^']*)'", c.group(2))
            if vals:
                out[(table, c.group(1))] = vals
    return out


def page_label_maps():
    """{map name: set of keys} from `const L = {...}` in page.html."""
    m = re.search(r"const L = \{\n(.*?)\n\};", PAGE_HTML, re.S)
    assert m, "const L label maps not found in page.html"
    maps = {}
    for line in m.group(1).splitlines():
        lm = re.match(r"\s*(\w+):\{(.*)\},?\s*$", line)
        if not lm:
            continue
        keys = set()
        for k in re.finditer(r'(?:"([^"]+)"|([A-Za-z_][\w]*))\s*:', lm.group(2)):
            keys.add(k.group(1) or k.group(2))
        maps[lm.group(1)] = keys
    return maps


# Which schema column each page label map translates.
LABEL_MAP_COLUMNS = {
    "sev": [("findings", "severity"), ("issues", "severity")],
    "issue": [("issues", "status")],
    "match": [("findings", "match_type")],
    "verif": [("findings", "verification")],
    "review": [("reviews", "status")],
    "scope": [("reviews", "scope")],
    "step": [("steps", "status")],
    "need": [("steps", "owner_need")],
    "gate": [("gates", "kind")],
    "gstat": [("gates", "status")],
    "rel": [("issue_relations", "relation")],
    "relIn": [("issue_relations", "relation")],
    "check": [("review_issue_checks", "outcome")],
    "kind": [("findings", "kind"), ("issues", "kind")],
    "run": [("reviewer_runs", "status")],
    "plan": [("plans", "status")],
    "decision": [("decisions", "status")],
}


class PageStatic(unittest.TestCase):
    def test_label_maps_cover_schema_enums(self):
        enums, maps = schema_enums(), page_label_maps()
        self.assertGreater(len(enums), 20, "schema enum parser found too few CHECK ... IN lists")
        missing = []
        for name, cols in LABEL_MAP_COLUMNS.items():
            self.assertIn(name, maps, f"page label map L.{name} is gone")
            for col in cols:
                self.assertIn(col, enums, f"schema has no enum for {col}")
                for v in enums[col]:
                    if v not in maps[name]:
                        missing.append(f"L.{name} lacks {v!r} ({col[0]}.{col[1]})")
        self.assertEqual(missing, [], "page label maps miss schema values:\n" + "\n".join(missing))

    def test_page_static_rules(self):
        script = PAGE_HTML
        # no outside URLs: the only absolute URL allowed is the SVG namespace string
        urls = set(re.findall(r"""(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s"'`<>)]*""", script))
        self.assertEqual(urls - {"http://www.w3.org/2000/svg"}, set(), "page.html refers to outside URLs")
        for bad in ("<script src", "<link ", "@import", "<iframe", "<img src=\"http"):
            self.assertNotIn(bad, script, f"page.html loads an outside resource ({bad})")
        # no HTML sinks: all data goes through text nodes (h/put/add)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, script, f"page.html uses {sink}")
        self.assertEqual(script.count("@@TOKEN@@"), 1, "the token placeholder must appear exactly once")


# ---------------------------------------------------------------- step 21: Host check, CORS, headers

class HostCorsHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = MakeServerThread(seeded_ledger()).start()
        cls.port = cls.srv.port
        cls.token = cls.srv.token

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def req(self, method, path, host=True, token=True, headers=None, body=None):
        h = {}
        if host is True:
            h["Host"] = f"127.0.0.1:{self.port}"
        elif host:
            h["Host"] = host
        if token:
            h["X-Ledger-Token"] = self.token
        h.update(headers or {})
        return http(self.port, method, path, h, body)

    def test_host_check(self):
        p = self.port
        for host in (f"127.0.0.1:{p}", f"localhost:{p}"):
            self.assertEqual(self.req("GET", "/", host=host, token=False).status, 200, host)
            self.assertEqual(self.req("GET", "/api/overview", host=host).status, 200, host)
        bad_hosts = ["evil.com:%d" % p, "evil.com", f"127.0.0.1:{p + 1}", f"localhost:{p + 1}", "127.0.0.1",
                     "localhost", f"LOCALHOST:{p}", f"[::1]:{p}", f"127.0.0.1:{p}.evil.com"]
        for host in bad_hosts:
            for method, path in (("GET", "/"), ("GET", "/api/overview"), ("POST", "/api/sql")):
                body = b'{"sql":"SELECT 1"}' if method == "POST" else None
                r = self.req(method, path, host=host, headers={"Content-Type": "application/json"}, body=body)
                self.assertEqual(r.status, 403, f"{method} {path} Host {host}")
                self.assertEqual(r.json(), {"error": "wrong host"})
        # no Host header at all (HTTP/1.1 request without Host)
        for path in ("/", "/api/overview"):
            r = self.req("GET", path, host=False)
            self.assertEqual(r.status, 403, path)
            self.assertEqual(r.json(), {"error": "wrong host"})

    # The names other reports used for the same property.
    test_host_header_required = test_host_check

    def test_host_check_raw_http10_without_host(self):
        raw = self.srv.raw(b"GET / HTTP/1.0\r\n\r\n")
        self.assertTrue(raw.startswith(b"HTTP/1.0 403"), raw[:60])

    def test_no_cors_and_options_refused(self):
        origin = {"Origin": "http://evil.com"}
        for method, path, body in (("GET", "/", None), ("GET", "/api/overview", None), ("GET", "/api/nope", None),
                                   ("POST", "/api/sql", b'{"sql":"SELECT 1"}')):
            r = self.req(method, path, headers=dict(origin, **{"Content-Type": "application/json"}), body=body)
            for h in r.headers.keys():
                self.assertFalse(h.lower().startswith("access-control-"), f"{method} {path} sent {h}")
        for path in ("/", "/api/overview", "/api/sql", "/api/gates/GATE-0001/answer"):
            r = self.req("OPTIONS", path, headers=dict(origin, **{"Access-Control-Request-Method": "POST"}))
            self.assertFalse(200 <= r.status < 300, f"OPTIONS {path} -> {r.status}")
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))
            # characterization: today OPTIONS falls to BaseHTTPRequestHandler's 501 (no do_OPTIONS)
            self.assertEqual(r.status, 501, f"OPTIONS {path}")

    test_no_cors_headers = test_no_cors_and_options_refused

    def test_security_headers(self):
        cases = [
            ("GET", "/", True, None, 200, True),
            ("GET", "/api/overview", True, None, 200, False),
            ("GET", "/api/review/R-9999", True, None, 404, False),
            ("GET", "/nope", True, None, 404, False),
            ("GET", "/api/overview", False, None, 403, False),          # missing token
            ("GET", "/", "evil.com", None, 403, False),                  # wrong host
            ("POST", "/api/sql", True, b"not json", 400, False),
        ]
        for method, path, token, body, status, html in cases:
            host = token if isinstance(token, str) else True
            r = self.req(method, path, host=host, token=token is True,
                         headers={"Content-Type": "application/json"} if method == "POST" else None, body=body)
            self.assertEqual(r.status, status, f"{method} {path}")
            for k, v in SECURITY_HEADERS.items():
                self.assertEqual(r.headers.get(k), v, f"{k} on {method} {path}")
            csp = r.headers.get("Content-Security-Policy")
            if html:
                self.assertIn("text/html", r.headers["Content-Type"])
                for part in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'",
                             "base-uri 'none'", "form-action 'none'", "img-src data:"):
                    self.assertIn(part, csp)
                # characterization: ISS-0218 script-src still allows 'unsafe-inline'
                self.assertIn("script-src 'unsafe-inline'", csp)
            else:
                self.assertTrue(r.headers["Content-Type"].startswith("application/json"), f"{method} {path}")
                self.assertIsNone(csp, f"JSON response {method} {path} should carry no CSP")

    def test_token_required_for_api(self):
        self.assertEqual(self.req("GET", "/api/overview", token=False).status, 403)
        r = self.req("GET", "/api/overview", token=False, headers={"X-Ledger-Token": "wrong-token-value-xxxxxxx"})
        self.assertEqual(r.status, 403)
        self.assertEqual(r.json(), {"error": "missing token; reload the page"})
        self.assertEqual(self.req("GET", "/api/overview").status, 200)
        for path, body in (("/api/sql", b'{"sql":"SELECT 1"}'), ("/api/gates/GATE-0001/answer", b"{}")):
            r = self.req("POST", path, token=False, headers={"Content-Type": "application/json"}, body=body)
            self.assertEqual(r.status, 403, path)

    def test_token_not_required_for_page(self):
        r = self.req("GET", "/", token=False)
        self.assertEqual(r.status, 200)
        # characterization: GET outside /api/ needs no token and is a plain 404
        self.assertEqual(self.req("GET", "/x/overview", token=False).status, 404)

    def test_current_non_ascii_token_drops_connection(self):
        # characterization: ISS-0216 compare_digest raises TypeError on a non-ASCII header; the handler dies and
        # the client gets no response at all (the fix makes this a 403)
        data = ("GET /api/overview HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Ledger-Token: é\r\n"
                "Connection: close\r\n\r\n" % self.port).encode("latin-1")
        n = len(self.srv.handler_errors)
        raw = self.srv.raw(data)
        self.assertEqual(raw, b"")
        self.assertTrue(any(isinstance(e, TypeError) for e in self.srv.handler_errors[n:]))

    def test_current_post_needs_json_content_type(self):
        r = self.req("POST", "/api/sql", headers={"Content-Type": "text/plain"}, body=b'{"sql":"SELECT 1"}')
        self.assertEqual(r.status, 415)
        r = self.req("POST", "/api/sql", headers={"Content-Type": "application/json"}, body=b"x" * 200_001)
        self.assertEqual(r.status, 413)


# ---------------------------------------------------------------- step 21: gate answers through HTTP

class GateAnswerOverHttp(unittest.TestCase):
    def setUp(self):
        self.L = Ledger()
        self.ids = seed_demo(self.L)
        self.srv = MakeServerThread(self.L).start()

    def tearDown(self):
        self.srv.stop()
        self.L.close()

    def gate(self, code):
        con = self.L.connect()
        try:
            return con.execute("SELECT * FROM gates WHERE code=?", (code,)).fetchone()
        finally:
            con.close()

    def test_gate_answer_via_viewer_visible_to_cli(self):
        before = self.L.run_ok("gates").stdout
        self.assertRegex(before, r"GATE-0001  \[open\] decision: Default notice period\n")
        r = self.srv.post("/api/gates/GATE-0001/answer", {"answer_value": "use-7", "answer_text": "Seven is enough."})
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        out = self.L.run_ok("gates").stdout
        self.assertIn("GATE-0001  [answered] decision: Default notice period -> use-7 Seven is enough.\n", out)
        self.assertIn("GATE-0002  [open]", out)
        g = self.gate("GATE-0001")
        self.assertEqual((g["answered_by"], g["answered_via"]), ("owner", "viewer"))
        self.assertIsNotNone(g["answered_at"])
        # a second answer is refused and the first stays
        r = self.srv.post("/api/gates/GATE-0001/answer", {"answer_value": "keep-14"})
        self.assertEqual(r.status, 409)
        self.assertIn("-> use-7 Seven is enough.", self.L.run_ok("gates").stdout)

    def test_current_post_outside_api_prefix_skips_token(self):
        # characterization: ISS-0019 the token guard only covers paths starting with /api/, while do_POST hands
        # every path to route(), which drops the first segment. A POST to /x/... with no token is served and the
        # gate answer is stored. The fix makes these 403/404 and leaves the gate open.
        r = self.srv.request("POST", "/x/gates/GATE-0002/answer", body={"answer_value": "approve"}, token=False)
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        self.assertEqual(self.gate("GATE-0002")["status"], "answered")
        r = self.srv.request("POST", "/x/sql", body={"sql": "SELECT code FROM gates ORDER BY code"}, token=False)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["rows"], [["GATE-0001"], ["GATE-0002"]])
        self.assertEqual(self.srv.request("POST", "/api/sql", body={"sql": "SELECT 1"}, token=False).status, 403)

    def test_current_concurrent_answers_can_both_report_saved(self):
        # characterization: ISS-0026 the answer route reads the gate, then runs UPDATE ... WHERE status='open'
        # without checking the row count, so when a second answer lands between the read and the write both
        # callers get {"ok": true} while only one answer is stored. Forced deterministically here by flipping the
        # gate to answered between route()'s SELECT and its UPDATE. The fix returns 409 to the loser.
        mod = self.srv.module
        con = mod.connect(self.L.db)
        real_one = mod.one
        state = {"done": False}

        def one_then_race(c, sql, args=()):
            r = real_one(c, sql, args)
            if not state["done"] and "FROM gates WHERE code=?" in sql:
                state["done"] = True
                other = self.L.connect()
                other.execute("UPDATE gates SET status='answered', answer_value='approve', answered_by='owner', "
                              "answered_via='chat' WHERE code='GATE-0002'")
                other.commit()
                other.close()
            return r

        mod.one = one_then_race
        try:
            res = mod.route(self.L.root, self.L.db, con, "/api/gates/GATE-0002/answer", {},
                            {"answer_value": "add"})
        finally:
            mod.one = real_one
            con.close()
        self.assertEqual(res, {"ok": True})  # reported saved ...
        g = self.gate("GATE-0002")
        self.assertEqual((g["answer_value"], g["answered_via"]), ("approve", "chat"))  # ... but not stored


if __name__ == "__main__":
    unittest.main()
