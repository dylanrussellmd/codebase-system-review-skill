"""The viewer's routes are one table of (method, path pattern) -> handler (plan P-0001: SRV-M4, ABS-M11, TEST-R7,
TEST-T76, TEST-T97, TEST-T99).

test_each_route_shape                     every route in ROUTES returns its documented keys on the demo ledger; each
                                          answers 405 to the other method; unknown paths are 404
test_method_routing                       GET /api/sql -> 405 Allow: POST; POST /api/overview -> 405 Allow: GET;
                                          HEAD, PUT, DELETE, OPTIONS and PATCH -> 405 JSON with the security headers
test_post_to_get_route                    a POST to any read route is 405 and changes nothing
test_answer_get_is_405                    GET on the answer route is 405 Allow: POST; the gate stays open
test_sql_get_is_405                       GET /api/sql is 405 Allow: POST
test_hub_snapshot_covers_every_get_route  the published hub embeds an answer for every GET route in ROUTES
"""
import threading
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ServerThread, seed_demo
from dev.tests.test_routes import CONTRACT

SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Cache-Control": "no-store"}


class Viewer(ServerThread):
    def start(self):
        self.module = mod = fixtures.load_ledger_module(fresh=True)
        mod.print = lambda *a, **k: None
        self._srv, self._token = mod.make_server(self.root, self.db, 0)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, name=f"viewer-{self.port}", daemon=True)
        self._thread.start()
        return self


def example_path(pattern, L):
    """A real path for a ROUTES pattern on the demo ledger ('*' segments filled from the ledger)."""
    fill = {"review": "R-0001", "issue": "ISS-0002", "plan": "P-0001", "table": "issues", "gates": "GATE-0001",
            "source": str(L.scalar("SELECT min(id) FROM finding_locations"))}
    return "/api/" + "/".join(fill[pattern[0]] if seg == "*" else seg for seg in pattern)


class MethodRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        seed_demo(cls.L)
        cls.mod = cls.L.module
        cls.srv = Viewer(cls.L).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.L.close()

    def call(self, path, body=None):
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            res = self.mod.route(self.L.root, self.L.db, con, path, {}, body)
        finally:
            con.close()
        return res if isinstance(res, tuple) else (res, 200)

    def assert405(self, r, allow, what):
        self.assertEqual(r.status, 405, f"{what}: {r!r}")
        self.assertEqual(r.headers.get("Allow"), allow, what)
        self.assertTrue(r.headers["Content-Type"].startswith("application/json"), what)
        for k, v in SECURITY_HEADERS.items():
            self.assertEqual(r.headers.get(k), v, f"{k} on {what}")

    def gate_status(self, code="GATE-0001"):
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            return con.execute("SELECT status FROM gates WHERE code=?", (code,)).fetchone()[0]
        finally:
            con.close()

    def test_each_route_shape(self):
        routes = self.mod.ROUTES
        self.assertGreaterEqual(len(routes), 15)
        self.assertEqual(len({(r.method, r.pattern) for r in routes}), len(routes), "one handler per method and path")
        documented = {}
        for name, (method, path, body, status, shape, keys) in CONTRACT.items():
            if status == 200:
                documented[(method, tuple("*" if "{" in s else s for s in path.split("/")[2:]))] = (path, body, shape, keys)
        loc = self.L.scalar("SELECT min(id) FROM finding_locations")
        for r in routes:
            with self.subTest(route=f"{r.method} {'/'.join(r.pattern)}"):
                self.assertIn(r.method, ("GET", "POST"))
                self.assertTrue(callable(r.handler))
                path = example_path(r.pattern, self.L)
                # every route has a documented example in test_routes.CONTRACT
                match = [v for (m, pat), v in documented.items()
                         if m == r.method and len(pat) == len(r.pattern)
                         and all(a == "*" or b == "*" or a == b for a, b in zip(pat, r.pattern))
                         and pat[0] == r.pattern[0]]
                self.assertEqual(len(match), 1, f"no CONTRACT example for {r.method} {path}")
                doc_path, body, shape, keys = match[0]
                if r.pattern != ("gates", "*", "answer"):  # the one write is exercised by test_routes.Contract
                    res, status = self.call(doc_path.format(loc=loc), body)[:2]
                    self.assertEqual(status, 200, res)
                    self.assertIsInstance(res, shape)
                    self.assertEqual(set(res[0]) if shape is list else set(res), keys)
                # the other method on the same path is refused, naming the method that works
                wrong = self.call(path, {} if r.method == "GET" else None)
                self.assertEqual(wrong[1], 405, wrong)
                self.assertEqual(set(wrong[0]), {"error"})
                self.assertEqual(wrong[2], {"Allow": r.method})
        for path in ("/api/nothing", "/api/", "/api/overview/extra", "/api/review/R-0001/x", "/api/gates/GATE-0001",
                     "/x/overview"):
            for body in (None, {}):
                self.assertEqual(self.call(path, body), ({"error": "not found"}, 404), path)
        self.assertEqual(self.gate_status(), "open")

    def test_method_routing(self):
        self.assert405(self.srv.get("/api/sql"), "POST", "GET /api/sql")
        self.assert405(self.srv.post("/api/overview", {}), "GET", "POST /api/overview")
        self.assertIn("POST", self.srv.get("/api/sql").json()["error"])
        for method in ("HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"):
            for path, allow in (("/", "GET"), ("/api/overview", "GET"), ("/api/sql", "POST"),
                                ("/api/gates/GATE-0001/answer", "POST")):
                r = self.srv.request(method, path, token=False)
                self.assert405(r, allow, f"{method} {path}")
                if method != "HEAD":  # a HEAD answer has no body
                    self.assertEqual(set(r.json()), {"error"})
            r = self.srv.request(method, "/nope", token=False)
            self.assertEqual(r.status, 404, f"{method} /nope")
            self.assertIsNone(r.headers.get("Allow"))
            for k, v in SECURITY_HEADERS.items():
                self.assertEqual(r.headers.get(k), v)
        # the page itself is GET only; a wrong host is refused before anything else
        self.assert405(self.srv.request("POST", "/", body={}, token=False), "GET", "POST /")
        r = self.srv.request("OPTIONS", "/api/overview", headers={"Host": "evil.com"})
        self.assertEqual(r.status, 403)
        for h in r.headers.keys():
            self.assertFalse(h.lower().startswith("access-control-"), h)
        # a 405 needs the token like every other /api/ answer to GET and POST
        self.assertEqual(self.srv.get("/api/sql", token=False).status, 403)
        self.assertEqual(self.srv.post("/api/overview", {}, token=False).status, 403)

    def test_post_to_get_route(self):
        before = fixtures.json.dumps(self.call("/api/overview")[0], default=str, sort_keys=True)
        for r in self.mod.ROUTES:
            if r.method != "GET":
                continue
            path = example_path(r.pattern, self.L)
            with self.subTest(path=path):
                resp = self.srv.post(path, {"sql": "SELECT 1", "answer_value": "use-7"})
                self.assert405(resp, "GET", f"POST {path}")
                self.assertEqual(self.srv.get(path).status, 200)
        self.assertEqual(self.srv.post("/api/review/R-0001/x", {}).status, 404)  # unknown stays 404
        self.assertEqual(fixtures.json.dumps(self.call("/api/overview")[0], default=str, sort_keys=True), before)

    def test_answer_get_is_405(self):
        r = self.srv.get("/api/gates/GATE-0001/answer")
        self.assert405(r, "POST", "GET answer")
        self.assertEqual(self.srv.get("/api/gates/GATE-9999/answer").status, 405)  # the method is wrong for any code
        self.assertEqual(self.srv.get("/api/gates/GATE-0001/answer?answer_value=use-7").status, 405)
        self.assertEqual(self.gate_status(), "open")
        self.assertEqual(self.call("/api/gates/GATE-0001/answer")[:2][1], 405)

    def test_sql_get_is_405(self):
        self.assert405(self.srv.get("/api/sql"), "POST", "GET /api/sql")
        self.assert405(self.srv.get("/api/sql?sql=SELECT%201"), "POST", "GET /api/sql?sql=")
        r = self.srv.post("/api/sql", {"sql": "SELECT 1 AS one"})
        self.assertEqual((r.status, r.json()["rows"]), (200, [[1]]))

    def test_hub_snapshot_covers_every_get_route(self):
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            R = self.mod.hub_snapshot(self.L.root, self.L.db, con)["responses"]
        finally:
            con.close()
        for r in self.mod.ROUTES:
            if r.method != "GET":
                self.assertIsNone(r.hub, "the hub is read-only")
                continue
            self.assertTrue(callable(r.hub), f"GET {'/'.join(r.pattern)} has no list of hub paths")
            head = r.pattern[0]
            held = [k for k in R if k.split("?")[0].split("/")[0] == head
                    and len(k.split("?")[0].split("/")) == len(r.pattern)]
            self.assertTrue(held, f"the hub holds no answer for GET /api/{'/'.join(r.pattern)}")
        n = lambda table: self.L.count(table)
        count = lambda head: sum(1 for k in R if k.startswith(head + "/"))
        self.assertEqual((count("review"), count("issue"), count("plan"), count("source")),
                         (n("reviews"), n("issues"), n("plans"), n("finding_locations")))
        self.assertEqual(count("table"), len(R["tables"]))
        self.assertIn("goals", R)
        self.assertEqual(set(R["gates?status=pending"]), {"gates", "other"})


if __name__ == "__main__":
    unittest.main()
