"""A review's issue links a page at a time, a page read that runs too long, and an address with an empty first
path segment (plan P-0001 step 199).

test_review_relations_paged          /api/review/<code> sends the first REVIEW_RELATIONS links and their total; the
                                     rest come from /api/review/<code>/relations; the hub holds every link
test_page_timeout_sentence           a page read stopped by its deadline answers with the page's own sentence
test_double_slash_path               GET // is the page and //api/... the API, as with one slash
test_review_relations_show_more      (browser) the review page draws the first links and Show more adds the rest,
                                     in the viewer and in the hub
test_double_slash_address_draws      (browser) the viewer opened at //#k=<token>#/reviews draws the Reviews page
test_review_page_budget_10x          (browser, LEDGER_SLOW_TESTS=1) the largest review of the x10 ledger loads in
                                     under 1.5 s with fewer than 10,000 elements
"""
import sqlite3
import time
import unittest

from dev.tests import browser, fixtures, scale
from dev.tests import test_routes as tr
from dev.tests.fixtures import Ledger, ServerThread, seed_demo, slow

LINKS = 250


def with_links(L):
    """The demo plus review R-0003 with LINKS links between issues."""
    ids = seed_demo(L)
    r1 = ids["reviews"]["R-0001"]
    r3 = L.review("R-0003", commit_sha=ids["commits"][1], repository_id=ids["repository"])
    made = [L.issue(None, first_seen_review_id=r1, title=f"Linked {n}") for n in range(LINKS + 1)]
    for n in range(LINKS):
        L.relation(made[n], made[n + 1], established_in_review_id=r3, rationale=f"Link {n}.")
    return ids


class Paged(tr.RouteCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        with_links(cls.L)
        cls.L.commit()
        cls.mod = cls.L.module

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def test_review_relations_paged(self):
        d = self.ok("/api/review/R-0003")
        self.assertEqual(len(d["relations"]), self.mod.REVIEW_RELATIONS)
        self.assertEqual(d["relation_total"], LINKS)
        page = self.ok("/api/review/R-0003/relations", offset=self.mod.REVIEW_RELATIONS, limit=500)
        self.assertEqual((page["total"], page["offset"], len(page["rows"])),
                         (LINKS, self.mod.REVIEW_RELATIONS, LINKS - self.mod.REVIEW_RELATIONS))
        every = self.ok("/api/review/R-0003/relations", limit=500)["rows"]
        self.assertEqual(every[:self.mod.REVIEW_RELATIONS], d["relations"])
        self.assertEqual(every[self.mod.REVIEW_RELATIONS:], page["rows"])
        self.assertEqual(self.call("/api/review/R-9999/relations")[0], 404)
        con = self.mod.connect(self.L.db, readonly=True)
        try:
            snap = self.mod.hub_snapshot(self.L.root, self.L.db, con)["responses"]
        finally:
            con.close()
        self.assertEqual(len(snap["review/R-0003/relations"]["rows"]), LINKS)
        self.assertEqual(len(snap["review/R-0003"]["relations"]), self.mod.REVIEW_RELATIONS)

    def test_page_timeout_sentence(self):
        mod = fixtures.load_ledger_module()
        real = mod.route

        def slow_route(root, db, con, path, qs, body):
            con.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                        "SELECT count(*) FROM c").fetchone()

        mod.ROUTE_SECONDS, mod.route = 0.2, slow_route
        try:
            res = mod.api(self.L.root, self.L.db, "/api/overview", {}, None)
        finally:
            mod.route, mod.ROUTE_SECONDS = real, 3
        self.assertEqual(res, ({"error": "This page took too long to load; try a narrower filter."}, 400))


class DoubleSlash(unittest.TestCase):
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

    def test_double_slash_path(self):
        page = self.srv.get("/", token=False)
        for path in ("//", "///"):
            r = self.srv.get(path, token=False)
            self.assertEqual((r.status, r.body), (200, page.body), path)
        r = self.srv.get("//api/overview")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["project"], self.L.root.name)


class LinksInBrowser(browser.ViewerTestCase):
    @classmethod
    def build_ledger(cls, ledger):
        ids = with_links(ledger)
        fixtures.finish_demo(ledger)
        ledger.commit()
        cls.hub = ledger.root / "hub.html"
        ledger.run_ok("progress-page", "-o", str(cls.hub))
        return ids

    SECTION = "h2:text('Links between issues made in this review') + div"

    def check_show_more(self, page):
        self.assertEqual(page.count(self.SECTION + " tbody tr"), 200)
        button = page.page.locator(self.SECTION + " button")
        self.assertEqual(button.inner_text(), f"Show more ({LINKS - 200} left)")
        page.after(button.click)
        self.assertEqual(page.count(self.SECTION + " tbody tr"), LINKS)
        self.assertEqual(page.count(self.SECTION + " button"), 0)

    def test_review_relations_show_more(self):
        self.check_show_more(self.open("#/review/R-0003"))
        hub = browser.ViewerPage(self.browser, self.hub.as_uri())
        self._pages.append(hub)
        hub.page.goto(self.hub.as_uri() + "#review-R-0003")
        hub.settle()
        self.check_show_more(hub)

    def test_double_slash_address_draws(self):
        page = self.new_page(token=None)
        page.page.goto(self.server.url + "/#k=" + self.server.token + "#/reviews")
        page.settle()
        self.assertEqual(page.text("h1"), "Reviews")
        self.assertEqual(page.js("location.pathname"), "/")


class Budget10x(unittest.TestCase):
    @slow
    def test_review_page_budget_10x(self):
        b = browser.require_browser()
        info = scale.build(fixtures.temp_dir("w5review-") / "x10", scale=10)
        con = sqlite3.connect(str(info["db"]))
        biggest = con.execute("""SELECT r.code FROM issue_relations x JOIN reviews r ON r.id=x.established_in_review_id
                                 GROUP BY r.id ORDER BY count(*) DESC LIMIT 1""").fetchone()[0]
        con.close()
        srv = ServerThread(root=info["root"], db=info["db"]).start()
        self.addCleanup(srv.stop)
        best = None
        for _ in range(3):
            page = browser.ViewerPage(b, srv.url, token=srv.token)
            self.addCleanup(page.close)
            t = time.perf_counter()
            page.goto("#/review/" + biggest)
            took = time.perf_counter() - t
            best = took if best is None else min(best, took)
        self.assertLess(best, 1.5)
        self.assertLess(page.js("document.querySelectorAll('*').length"), 10_000)
        self.assertEqual(page.page_errors, [])


if __name__ == "__main__":
    unittest.main()
