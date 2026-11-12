"""The published hub page in a real browser (plan P-0001 steps 113 and 125; WALK-L15, OWN-2).

test_hub_loads_without_errors_or_outside_requests  the page written by progress-page opens from a file, draws every
                                                   tab, a review, an issue, a plan and the guide on artifact-safe
                                                   hash names, with no page or console error and no request at all
test_hub_answer_saved_in_artifact_db               with an artifact database (window.claude.use("db")), answering an
                                                   open gate writes answers/<gate code>; import-answers records it
Skipped cleanly when Playwright or a browser is missing (see dev/tests/browser.py).
"""
import json
import unittest

from dev.tests import browser, fixtures

FAKE_DB = r"""
(() => {
  const docs = {}; const subs = [];
  const snap = () => ({docs: Object.entries(docs).map(([id, d]) => ({id, data: () => d}))});
  window.__answers = docs;
  window.claude = {use: async name => name !== "db" ? null : {
    collection: c => ({onSnapshot: (ok, bad) => { subs.push(ok); ok(snap()); return () => {}; }}),
    doc: path => ({set: async d => { docs[path.split("/").pop()] = d; subs.forEach(f => f(snap())); }}),
  }};
})();
"""


class HubPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = fixtures.Ledger()
        fixtures.seed_demo(cls.L)
        fixtures.finish_demo(cls.L)
        cls.L.commit()
        cls.hub = cls.L.root / "hub.html"
        cls.L.run_ok("progress-page", "-o", str(cls.hub))
        cls.url = cls.hub.as_uri()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def setUp(self):
        self.pages = []

    def tearDown(self):
        for p in self.pages:
            p.close()

    def open(self, init=None):
        p = browser.ViewerPage(self.browser, self.url)
        self.pages.append(p)
        if init:
            p.context.add_init_script(init)
        p.page.goto(self.url)
        return p.settle()

    def go(self, p, slug):
        p.js(f"location.hash = {json.dumps(slug)}")
        return p.settle(quiet_ms=60)

    def assertClean(self, p, where):
        self.assertEqual(p.page_errors, [], where)
        self.assertEqual(p.console_errors, [], where)
        self.assertEqual(p.outside_requests, [], where)
        self.assertEqual(p.count(".status.err"), 0, f"{where}: {p.texts('.status.err')}")
        self.assertNotEqual(p.text("#app h1"), "Could not load this view", where)

    def test_hub_loads_without_errors_or_outside_requests(self):
        p = self.open()
        self.assertClean(p, "home")
        self.assertIn("How it works", p.texts(".tabs a"))
        for slug, h1 in (("inbox", "Waiting on you"), ("history", "Questions and answers"), ("reviews", "Reviews"),
                         ("review-R-0001", None), ("issues", "Issues"), ("issue-ISS-0001", None),
                         ("plans", "Fix plans"), ("plan-P-0001", None), ("plan-P-0001-phase-2", None),
                         ("trends", None), ("explore", "Explore the ledger"), ("explore-finding-locations", None),
                         ("guide", "How it works")):
            self.go(p, slug)
            self.assertClean(p, slug)
            if h1:
                self.assertEqual(p.text("#app h1"), h1, slug)
        # a link inside the page lands on an artifact-safe hash name, not on #/...
        self.go(p, "home")
        p.page.locator('#app a[href="#/plan/P-0001"]').first.click()
        p.settle(quiet_ms=60)
        self.assertEqual(p.location_hash(), "#plan-P-0001")
        self.assertClean(p, "after a link click")
        self.go(p, "reviews")  # a table row opens through go()
        p.page.locator("#app tbody tr").last.click()
        p.settle(quiet_ms=60)
        self.assertEqual(p.location_hash(), "#review-R-0001")
        self.assertClean(p, "after a row click")
        self.assertEqual([u for u in p.requests if u != self.url], [])

    def test_hub_answer_saved_in_artifact_db(self):
        L = self.L
        p = self.open(FAKE_DB)
        self.go(p, "inbox")
        self.assertClean(p, "inbox")
        code = L.scalar("SELECT code FROM gates WHERE status='open' AND options IS NOT NULL ORDER BY id")
        card = p.page.locator(".card.gate", has=p.page.locator(f"input[name='opt-{code}']"))
        card.locator("input[type=radio]").first.check()
        card.locator("button").click()
        p.settle(quiet_ms=60)
        saved = p.js("window.__answers")
        self.assertIn(code, saved)
        self.assertClean(p, "after answering")
        f = L.root / "answers.json"
        f.write_text(json.dumps([{"id": k, "data": v} for k, v in saved.items()]))
        out = L.run_ok("import-answers", str(f)).stdout
        self.assertIn(f"{code}: recorded", out)
        self.assertEqual(L.scalar("SELECT answered_via FROM gates WHERE code=?", (code,)), "viewer")


if __name__ == "__main__":
    unittest.main()
