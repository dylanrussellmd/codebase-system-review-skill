"""The review and plan pages in a real browser (plan P-0001 steps 148, 162, 164, 167, 168): points and steps that
load in full when opened, a search that waits for the typing to stop and leaves open rows open, the earlier issues
in parts, and the same pages in the published hub. Skipped cleanly without Playwright or a browser."""
import unittest

from dev.tests import browser, fixtures


def build(L):
    ids = fixtures.seed_demo(L)
    r3 = L.review("R-0003", commit_sha=ids["commits"][1], repository_id=ids["repository"], title="A larger review")
    for n in range(230):
        f = L.finding(r3, f"BULK-L{n}", severity="L", domain_code="spec" if n % 2 else "tests",
                      summary=f"Point number {n}.", detail="needle in the detail" if n == 150 else f"Detail {n}.")
        if n in (5, 150):
            L.location(f, "src/requests.py", 3, 4, repository_id=ids["repository"], symbol="only_here")
    for n in range(120):
        i = L.issue(None, first_seen_review_id=ids["reviews"]["R-0001"], title=f"Old issue {n}")
        L.check_row(r3, i, "still-present" if n % 3 else "fixed", evidence="Looked.")
    plan, phase = ids["plan"], ids["phases"][2]
    for n in range(4, 40):
        L.step(plan, phase, n, f"Extra step {n}", change=f"Change the thing called widget{n}.",
               proof=f"test_widget_{n} passes.")
    return ids


class Pages(browser.ViewerTestCase):
    build_ledger = classmethod(lambda cls, L: build(L))
    NOTE = ".tools ~ div p.note"

    def api_calls(self, page, part):
        return [u for u in page.requests if part in u]

    def test_review_point_loads_when_opened(self):
        page = self.open("#/review/R-0001")
        self.assertEqual(self.api_calls(page, "/api/finding/"), [])
        self.assertEqual(page.count("details.item .body *"), 0)          # no body is built before it is opened
        item = page.page.locator("details.item", has_text="SEC-H1")
        item.locator("summary").click()
        page.settle()
        self.assertEqual(len(self.api_calls(page, "/api/finding/")), 1)
        self.assertIn("Recommended fix", item.locator("dt").all_inner_texts())
        self.assertEqual(item.locator(".ref .refline code").first.inner_text(), "src/auth.py")
        item.locator("summary").click()                                   # closing and opening again asks nothing
        item.locator("summary").click()
        page.settle()
        self.assertEqual(len(self.api_calls(page, "/api/finding/")), 1)

    def test_review_search_debounced(self):
        page = self.open("#/review/R-0003")
        box = page.page.locator("input[type=search]")
        box.press_sequentially("needl", delay=20)                         # five keys, well inside 150 ms each
        self.assertEqual(self.api_calls(page, "?q="), [])                 # nothing while typing
        page.settle()
        calls = self.api_calls(page, "?q=")
        self.assertEqual(len(calls), 1)                                   # one search once the typing stops
        self.assertTrue(calls[0].endswith("/api/review/R-0003?q=needl"))
        self.assertEqual(page.text(self.NOTE), "1 of 230 points")         # found in a point's detail, by the server
        self.assertEqual(page.texts("details.item > summary .n"), ["BULK-L150"])
        box.fill("only_here")
        page.settle()
        self.assertEqual(page.texts("details.item > summary .n"), ["BULK-L5", "BULK-L150"])   # a cited symbol
        box.fill("src/requests")
        page.settle()
        self.assertEqual(page.text(self.NOTE), "2 of 230 points")                              # a cited file
        box.fill("number 22")
        page.settle()
        self.assertEqual(page.text(self.NOTE), "11 of 230 points")        # ids and summaries, matched in the page
        box.fill("")
        page.settle()
        self.assertEqual(page.text(self.NOTE), "230 of 230 points")
        self.assertEqual(page.count("details.item"), 100)

    def test_review_search_keeps_open_items(self):
        page = self.open("#/review/R-0003")
        first = page.page.locator("details.item", has_text="BULK-L0").first
        first.locator("summary").click()
        page.settle()
        handle = first.element_handle()
        box = page.page.locator("input[type=search]")
        box.fill("number 0.")
        page.settle()
        self.assertEqual(page.texts("details.item > summary .n"), ["BULK-L0"])
        self.assertTrue(page.page.evaluate("e => e.open && e.isConnected", handle))
        box.fill("number 1")                                              # the open point is filtered out...
        page.settle()
        self.assertFalse(page.page.evaluate("e => e.isConnected", handle))
        box.fill("")                                                      # ...and is the same row, still open, after
        page.settle()
        self.assertTrue(page.page.evaluate("e => e.open && e === document.querySelector('details.item')", handle))
        self.assertIn("Detail 0.", first.inner_text())
        page.page.select_option("#fd", "tests")                           # a filter keeps it open too
        page.settle()
        self.assertTrue(page.page.evaluate("e => e.open && e.isConnected", handle))

    def test_review_show_more_appends(self):
        page = self.open("#/review/R-0003")
        first = page.page.locator("details.item").first.element_handle()
        page.page.locator("details.item > summary").nth(3).click()
        page.settle()
        page.page.locator(".points + div button").click()
        page.settle()
        self.assertEqual(page.count("details.item"), 230)
        self.assertTrue(page.page.evaluate("e => e === document.querySelector('details.item')", first))
        self.assertEqual(page.count("details.item[open]"), 1)             # what was open stays open
        self.assertEqual(page.js("document.activeElement.textContent.includes('BULK-L')"), True)

    def test_review_earlier_issues_in_parts(self):
        page = self.open("#/review/R-0003")
        sec = "h2:text('Earlier issues this review looked for') + div"
        self.assertEqual(page.text(sec + " p.note"), "40 fixed · 80 still present")
        self.assertEqual(page.count(sec + " tbody tr"), 50)
        page.after(lambda: page.page.locator(sec + " button").click())
        self.assertEqual(page.count(sec + " tbody tr"), 120)
        self.assertEqual(page.count(sec + " button"), 0)
        self.assertLess(page.js("document.querySelectorAll('*').length"), 10_000)

    def test_reviews_list_shows_missing_tests(self):
        page = self.open("#/reviews")
        self.assertIn("Missing tests", page.contents("#app thead th"))
        col = page.contents("#app thead th").index("Missing tests") + 1
        self.assertEqual(page.texts(f"#app tbody td:nth-child({col})"),
                         [str(r["tests"]) for r in self.ledger.all("SELECT tests FROM v_review_totals ORDER BY started_at DESC")])

    def test_plan_step_loads_when_opened(self):
        page = self.open("#/plan/P-0001")                                 # phase 1 is loaded in full
        one = page.page.locator(".steps details.item").first
        one.locator("summary").click()
        page.settle()
        self.assertIn("Change", one.locator("dt").all_inner_texts())
        self.assertEqual(one.locator(".ref .note").inner_text(), "Closes 1 point")
        page.page.locator("nav.rail button").nth(1).click()               # phase 2: its steps come when opened
        page.settle()
        before = len(self.api_calls(page, "/api/step/"))
        late = page.page.locator(".steps details.item", has_text="Extra step 17")
        late.locator("summary").click()
        page.settle()
        self.assertEqual(len(self.api_calls(page, "/api/step/")), before + 1)
        self.assertIn("Change the thing called widget17.", late.inner_text())
        self.assertIn("test_widget_17 passes.", late.inner_text())
        # a deep link to a phase loads that phase in full
        deep = self.open("#/plan/P-0001/phase/2")
        step = deep.page.locator(".steps details.item", has_text="Extra step 20")
        step.locator("summary").click()
        self.assertIn("Change the thing called widget20.", step.inner_text())
        self.assertEqual(self.api_calls(deep, "/api/step/"), [])

    def test_plan_search_debounced(self):
        page = self.open("#/plan/P-0001")
        box = page.page.locator("#app .tools input")
        marks = page.js("""() => { window.__draws = 0;
            new MutationObserver(m => { if (m.some(x => [...x.addedNodes].some(n => n.tagName === "H2"))) window.__draws++; })
              .observe(document.querySelector(".steps"), {childList:true}); return 0; }""")
        box.press_sequentially("widge", delay=20)
        self.assertEqual(page.js("window.__draws"), marks)                # nothing drawn while typing
        page.settle()
        self.assertEqual(page.js("window.__draws"), 1)                    # one draw after the last key
        self.assertEqual(page.text("#app .steps h2"), "36 matching steps across all phases")
        box.fill("widget17")                                              # text of a step outside the loaded phase
        page.settle()
        self.assertEqual(page.texts(".steps details.item > summary .n"), ["17"])
        box.fill("test_widget_30")                                        # its proof too
        page.settle()
        self.assertEqual(page.texts(".steps details.item > summary .n"), ["30"])

    def test_trends_charts_tests_and_confirmed(self):
        page = self.open("#/trends")
        head = page.contents("h2:text('Points per review') + div thead th")
        self.assertEqual(head[6:8], ["Missing tests", "Confirmed"])
        want = self.ledger.all("SELECT review, sum(tests) t, sum(confirmed) c FROM v_review_summary GROUP BY review ORDER BY min(started_at)")
        rows = page.js("[...document.querySelectorAll('#app table')[0].querySelectorAll('tbody tr')].map(r => "
                       "[r.cells[0].textContent, +r.cells[6].textContent, +r.cells[7].textContent])")
        self.assertEqual(rows, [[w["review"], w["t"], w["c"]] for w in want])
        cards = page.page.locator("h2:text('Points per review') ~ .grid2").first.locator(".card b")
        self.assertEqual(cards.all_inner_texts(), ["Points", "Missing tests", "Confirmed"])


class HubPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = fixtures.Ledger()
        build(cls.L)
        cls.L.commit()
        cls.hub = cls.L.root / "hub.html"
        cls.L.run_ok("progress-page", "-o", str(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def open(self, slug):
        p = browser.ViewerPage(self.browser, self.hub.as_uri())
        self.addCleanup(p.close)
        p.page.goto(self.hub.as_uri() + "#" + slug)
        p.settle()
        self.addCleanup(lambda: self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], [])))
        return p

    def test_hub_review_and_plan(self):
        p = self.open("review-R-0003")
        box = p.page.locator("input[type=search]")
        box.fill("needle in the")
        p.settle()
        self.assertEqual(p.texts("details.item > summary .n"), ["BULK-L150"])
        item = p.page.locator("details.item").first
        item.locator("summary").click()
        p.settle()
        self.assertIn("needle in the detail", item.inner_text())
        self.assertEqual(item.locator(".ref .refline code").first.inner_text(), "src/requests.py")
        box.fill("")
        p.settle()
        sec = "h2:text('Earlier issues this review looked for') + div"
        self.assertEqual(p.count(sec + " tbody tr"), 50)
        p.page.locator(sec + " button").click()
        p.settle()
        self.assertEqual(p.count(sec + " tbody tr"), 120)
        p.js("location.hash = 'plan-P-0001-phase-2'")
        p.settle(quiet_ms=80)
        step = p.page.locator(".steps details.item", has_text="Make the notice period")
        step.locator("summary").click()
        p.settle()
        self.assertIn("Closes", step.locator(".ref .note").inner_text())
        self.assertIn("Change", step.locator("dt").all_inner_texts())
        self.assertEqual(p.count(".status.err"), 0)


if __name__ == "__main__":
    unittest.main()
