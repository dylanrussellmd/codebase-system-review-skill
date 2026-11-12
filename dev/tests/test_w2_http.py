"""The viewer over HTTP (plan P-0001 steps 170, 171, 175 and 169): gzip for large answers, loopback-only binding
on 127.0.0.1 and [::1], the page a wrong address gets, and the JSON media type and BLOB rules."""
import gzip
import http.client
import json
import socket
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, ServerThread, seed_demo


def ipv6_loopback():
    try:
        s = socket.socket(socket.AF_INET6)
    except OSError:
        return False
    try:
        s.bind(("::1", 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        ids = seed_demo(cls.L)
        r = ids["reviews"]["R-0001"]
        for n in range(60):  # enough points for a review answer well past the gzip threshold
            cls.L.finding(r, f"BULK-L{n + 10}", severity="L", summary="The same sentence again. " * 8)
        cls.L.commit()
        cls.srv = ServerThread(cls.L).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.L.close()

    # -- step 170
    def test_gzip_when_accepted(self):
        plain = self.srv.get("/api/review/R-0001")
        self.assertIsNone(plain.headers.get("Content-Encoding"))
        self.assertGreater(len(plain.body), 8000)
        for accept in ("gzip", "gzip, deflate, br", "br;q=1.0, GZIP;q=0.8"):
            r = self.srv.get("/api/review/R-0001", headers={"Accept-Encoding": accept})
            self.assertEqual((r.status, r.headers.get("Content-Encoding")), (200, "gzip"), accept)
            self.assertEqual(r.headers.get("Vary"), "Accept-Encoding")
            self.assertEqual(int(r.headers["Content-Length"]), len(r.body))
            self.assertEqual(json.loads(gzip.decompress(r.body)), plain.json())
            self.assertLess(len(r.body) * 3, len(plain.body), "gzip saves at least two thirds of a review answer")
        page = self.srv.get("/", headers={"Accept-Encoding": "gzip"}, token=False)
        self.assertEqual(page.headers.get("Content-Encoding"), "gzip")
        self.assertIn(b"<!doctype html>", gzip.decompress(page.body))
        self.assertIn("script-src", page.headers["Content-Security-Policy"])

    def test_no_gzip_without_header(self):
        for headers in ({}, {"Accept-Encoding": "identity"}, {"Accept-Encoding": "gzip;q=0"},
                        {"Accept-Encoding": "br, deflate"}):
            r = self.srv.get("/api/review/R-0001", headers=headers)
            self.assertIsNone(r.headers.get("Content-Encoding"), headers)
            self.assertEqual(r.headers.get("Vary"), "Accept-Encoding")
            self.assertEqual(r.json()["review"]["code"], "R-0001")

    def test_small_responses_plain(self):
        r = self.srv.get("/api/plans", headers={"Accept-Encoding": "gzip"})
        self.assertLess(len(r.body), 8000)
        self.assertIsNone(r.headers.get("Content-Encoding"))
        self.assertIsNone(r.headers.get("Vary"))
        self.assertEqual(r.json()[0]["code"], "P-0001")
        e = self.srv.get("/api/review/R-9999", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((e.status, e.headers.get("Content-Encoding")), (404, None))

    # -- step 175
    def test_wrong_host_gets_explanation_page(self):
        for host in ("example:1", "evil.com", f"127.0.0.1.evil.com:{self.srv.port}", ""):
            r = self.srv.get("/", headers={"Host": host}, token=False)
            self.assertEqual(r.status, 403, host)
            self.assertTrue(r.headers["Content-Type"].startswith("text/html"), host)
            self.assertIn("Content-Security-Policy", r.headers)
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
            text = r.text
            self.assertIn(f"http://127.0.0.1:{self.srv.port}/", text)
            self.assertNotIn(self.srv.token, text)
            self.assertNotIn("<script", text)
            self.assertNotIn(host or "\0", text)  # the page never repeats the name it was asked under
        with_token = self.srv.get("/", headers={"Host": "example:1"})
        self.assertNotIn(self.srv.token, with_token.text)
        for method, path in (("GET", "/api/overview"), ("POST", "/api/sql"), ("GET", "/favicon.ico"), ("POST", "/")):
            r = self.srv.request(method, path, body={} if method == "POST" else None, headers={"Host": "example:1"})
            self.assertEqual((r.status, r.json()), (403, {"error": "wrong host"}), (method, path))

    # -- step 171
    def test_binds_loopback_only(self):
        mod = fixtures.load_ledger_module(fresh=True)
        srv, _ = mod.make_server(self.L.root, self.L.db, 0, ipv6=True)
        try:
            bound = [srv.server_address[0]] + [s.server_address[0] for s in srv.also]
            self.assertEqual(bound[0], "127.0.0.1")
            self.assertEqual(bound[1:], ["::1"] if ipv6_loopback() else [])
            self.assertEqual(srv.addresses[0], f"127.0.0.1:{srv.server_address[1]}")
            self.assertEqual(len(srv.addresses), len(bound))
            for s in srv.also:
                self.assertEqual(s.server_address[1], srv.server_address[1])
        finally:
            for s in [srv] + srv.also:
                s.server_close()
        plain, _ = mod.make_server(self.L.root, self.L.db, 0)
        try:
            self.assertEqual(plain.also, [])
        finally:
            plain.server_close()

    def test_server_binds_loopback_only(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # no packet is sent: this only asks which address would be used
            mine = probe.getsockname()[0]
        except OSError:
            mine = None
        finally:
            probe.close()
        if not mine or mine.startswith("127."):
            self.skipTest("this machine has no address besides loopback")
        with self.assertRaises(OSError):
            socket.create_connection((mine, self.srv.port), timeout=1).close()

    def test_no_ipv6_serves_ipv4_alone(self):
        mod = fixtures.load_ledger_module(fresh=True)
        mod._ipv6_loopback_server = lambda port, handler: None
        srv, _ = mod.make_server(self.L.root, self.L.db, 0, ipv6=True)
        try:
            self.assertEqual((srv.also, len(srv.addresses)), ([], 1))
        finally:
            srv.server_close()

    @unittest.skipUnless(ipv6_loopback(), "this machine has no IPv6 loopback")
    def test_ipv6_loopback_served(self):
        self.assertIn(f"http://[::1]:{self.srv.port}/#k=", self.srv.printed)
        c = http.client.HTTPConnection("::1", self.srv.port, timeout=5)
        try:
            c.request("GET", "/", headers={"Host": f"[::1]:{self.srv.port}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn(b"<!doctype html>", r.read())
            c.request("GET", "/api/overview", headers={"Host": f"[::1]:{self.srv.port}",
                                                       "X-Ledger-Token": self.srv.token})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
