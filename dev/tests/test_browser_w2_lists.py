"""Lists in a real browser (plan P-0001 steps 160, 186, 187): issue filters kept in the address, the latest search
winning, lists a page at a time with their total, the gate history newest first, and review gate rows that open the
gate itself. The same views are opened in the published hub. Skipped cleanly without Playwright or a browser."""
import json
import unittest

from dev.tests import browser, fixtures

OPTS = [{"value": "yes", "label": "Yes, do it"}, {"value": "no", "label": "No, leave it"}]


def build(L):
    ids = fixtures.seed_demo(L)
    r2 = ids["reviews"]["R-0002"]
    for n in range(230):  # ISS-0005 .. ISS-0234, enough for a second page
        L.issue(None, first_seen_review_id=r2, severity="L", title=f"Bulk issue {n}")
    for n in range(210):  # GATE-0003 .. GATE-0212, applied, oldest first
        L.gate(None, "decision", f"Old question {n}", options=OPTS, status="applied", answer_value="no",
               answer_text="Later.", review_id=ids["reviews"]["R-0001"] if n == 0 else None,
               created_at=f"2026-10-01T10:{n // 60:02d}:{n % 60:02d}.000Z")
    fixtures.finish_demo(L)
    return ids


class Lists(browser.ViewerTestCase):
    build_ledger = classmethod(lambda cls, L: build(L))

    def codes(self, page):
        return page.texts("#app tbody td:first-child")

    def test_issue_filters_survive_reload(self):
        page = self.open("#/issues")
        self.assertEqual(page.location_hash(), "#/issues")
        page.after(lambda: page.page.select_option("select[aria-label=Severity]", "M"))
        self.assertEqual(page.location_hash(), "#/issues?severity=M")
        page.after(lambda: page.page.select_option("select[aria-label=Status]", ""))
        self.assertEqual(page.location_hash(), "#/issues?severity=M&status=all")
        page.after(lambda: page.page.select_option("select[aria-label=Area]", "spec"))
        page.page.locator("#app input[type=search]").fill("notice")
        page.page.wait_for_function("() => location.hash.includes('q=notice')")
        page.settle()
        self.assertEqual(page.location_hash(), "#/issues?severity=M&domain_code=spec&q=notice&status=all")
        before = self.codes(page)
        self.assertEqual(before, ["ISS-0002"])
        page.page.reload()
        page.settle()
        self.assertEqual(self.codes(page), before)
        self.assertEqual(page.js("[...document.querySelectorAll('#app .tools select')].map(s => s.value)"),
                         ["", "M", "spec"])
        self.assertEqual(page.js("document.querySelector('#app input[type=search]').value"), "notice")
        # the address is replaced, not pushed: Back leaves the list
        self.assertEqual(page.js("history.length"), 2)

    def test_issue_search_latest_wins(self):
        page = self.open("#/issues")
        state = {"held": None}

        def slow_first(route):
            if "q=sign" in route.request.url and state["held"] is None:
                state["held"] = route   # hold the first search until the second has been answered
            else:
                route.continue_()

        page.page.route("**/api/issues?**", slow_first)
        box = page.page.locator("#app input[type=search]")
        box.fill("sign")
        page.page.wait_for_timeout(400)
        self.assertIsNotNone(state["held"])
        box.fill("notice")
        page.page.wait_for_function("() => document.querySelector('#app p.note')?.textContent === '1 issue'")
        self.assertEqual(self.codes(page), ["ISS-0002"])
        state["held"].continue_()
        page.page.wait_for_timeout(300)
        page.settle()
        self.assertEqual(self.codes(page), ["ISS-0002"], "the late answer to the first search is dropped")
        self.assertEqual(page.text("#app p.note"), "1 issue")

    def test_issues_list_pages(self):
        page = self.open("#/issues")
        self.assertEqual(page.text("#app p.note"), "233 issues")
        self.assertEqual(len(self.codes(page)), 200)
        self.assertEqual(page.text(".pager .note"), "Showing 1–200 of 233")
        self.assertEqual(page.texts(".pager button"), ["Next"])
        page.after(lambda: page.page.click(".pager button"))
        self.assertEqual(page.text(".pager .note"), "Showing 201–233 of 233")
        self.assertEqual(page.texts(".pager button"), ["Previous"])
        self.assertEqual(len(self.codes(page)), 33)
        self.assertEqual(page.location_hash(), "#/issues?offset=200")
        self.assertEqual(page.js("document.activeElement.tagName"), "H1")
        page.page.reload()
        page.settle()
        self.assertEqual(page.text(".pager .note"), "Showing 201–233 of 233")
        # a filter change starts again at the first page
        page.after(lambda: page.page.select_option("select[aria-label=Severity]", "M"))
        self.assertEqual(page.location_hash(), "#/issues?severity=M")
        self.assertEqual(page.count(".pager"), 0)

    def test_page_hotspot_link_filters_issues(self):
        page = self.open("#/trends")
        link = page.page.locator("h2:text('Files that keep producing issues') + div tbody tr", has_text="src/auth.py")
        self.assertEqual(link.locator("a").get_attribute("href"), "#/issues?status=all&q=src%2Fauth.py")
        page.after(link.locator("a").click)
        self.assertEqual(page.js("document.querySelector('select[aria-label=Status]').value"), "")
        self.assertIn("ISS-0001", self.codes(page))  # the fixed and confirmed one is listed too

    def test_history_sorted_newest_first(self):
        page = self.open("#/history")
        self.assertEqual(page.text(".pager .note"), "Showing 1–200 of 212")
        titles = page.texts("#app tbody td:nth-child(3)")
        self.assertEqual(titles[2:5], ["Old question 209", "Old question 208", "Old question 207"])
        self.assertEqual(page.texts("#app tbody tr:nth-child(3) td:nth-child(5)"), ["No, leave it — Later."])
        page.after(lambda: page.page.click(".pager button"))
        self.assertEqual(page.texts("#app tbody td:nth-child(3)")[-1], "Old question 0")
        self.assertEqual(page.location_hash(), "#/history?offset=200")

    def test_review_gate_row_links_to_gate(self):
        page = self.open("#/review/R-0001")
        rows = page.page.locator(".card:has(h2:text('Questions asked during this review')) tbody tr")
        old = rows.filter(has_text="Old question 0")
        self.assertEqual(old.locator("a").get_attribute("href"), "#/history/GATE-0003")
        self.assertEqual(old.locator("td").nth(3).inner_text(), "No, leave it — Later.")
        page.after(old.locator("a").click)
        # the oldest gate is on the history's second page: that page is shown, at its row
        self.assertEqual(page.text(".pager .note"), "Showing 201–212 of 212")
        self.assertEqual(page.js("document.activeElement.id"), "gate-row-GATE-0003")
        self.assertTrue(page.js("(() => { const r = document.activeElement.getBoundingClientRect(); "
                                "return r.top >= 0 && r.bottom <= innerHeight; })()"))
        page.hash("#/review/R-0002")
        open_gate = page.page.locator(".card:has(h2:text('Questions asked during this review')) tbody tr a").first
        self.assertEqual(open_gate.get_attribute("href"), "#/inbox/GATE-0002")
        page.after(open_gate.click)
        self.assertEqual(page.text("h1"), "Waiting on you")
        self.assertEqual(page.js("document.activeElement.querySelector('h2').id"), "gate-GATE-0002")


class HubLists(unittest.TestCase):
    """The published hub answers the same lists from its snapshot."""

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

    def open(self, slug=""):
        p = browser.ViewerPage(self.browser, self.hub.as_uri())
        self.addCleanup(p.close)
        p.page.goto(self.hub.as_uri() + ("#" + slug if slug else ""))
        p.settle()
        self.addCleanup(lambda: self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], [])))
        return p

    def go(self, p, slug):
        p.js(f"location.hash = {json.dumps(slug)}")
        return p.settle(quiet_ms=60)

    def test_hub_lists_page_and_filter(self):
        p = self.open("issues")
        self.assertEqual(p.text("#app p.note"), "233 issues")
        self.assertEqual(p.text(".pager .note"), "Showing 1–200 of 233")
        p.page.click(".pager button")
        p.settle()
        self.assertEqual(p.text(".pager .note"), "Showing 201–233 of 233")
        p.page.select_option("select[aria-label=Status]", "")
        p.settle()
        self.assertEqual(p.text("#app p.note"), "234 issues")
        p.page.locator("#app input[type=search]").fill("Bulk issue 7")
        p.page.wait_for_function("() => document.querySelector('#app p.note').textContent === '11 issues'")
        self.go(p, "history")
        self.assertEqual(p.text(".pager .note"), "Showing 1–200 of 212")
        self.assertEqual(p.texts("#app tbody tr:nth-child(3) td:nth-child(5)"), ["No, leave it — Later."])
        self.go(p, "review-R-0001")
        link = p.page.locator(".card:has(h2:text('Questions asked during this review')) tbody tr a").first
        link.click()
        p.settle()
        self.assertEqual(p.location_hash(), "#history-GATE-0003")
        self.assertEqual(p.text(".pager .note"), "Showing 201–212 of 212")
        self.assertEqual(p.js("document.activeElement.id"), "gate-row-GATE-0003")


if __name__ == "__main__":
    unittest.main()
