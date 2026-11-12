"""What the pages show, in a real browser (plan P-0001 steps 184, 188, 191, 192, 193, 196, 197, 198): labels in
place of stored words, no status line on a page that has just loaded, the recorded data of a review and a plan, the
goals' lifecycle, the How it works page and Markdown drawn as a document. Skipped cleanly without Playwright."""
import json
import re
import unittest

from dev.tests import _w2_shown_helpers as H
from dev.tests import browser, fixtures

VIEWS = ("#/", "#/guide", "#/inbox", "#/history", "#/decisions", "#/reviews", "#/review/R-0001", "#/review/R-0002",
         "#/goals", "#/issues?status=all", "#/issue/ISS-0001", "#/issue/ISS-0002", "#/plans", "#/plan/P-0001",
         "#/plan/P-0001/phase/2", "#/trends")
# Stored words that must never reach the screen as they are (each has a label).
RAW = re.compile(r"\b(still-present|not-checked|waiting-owner|needs-decision|in-progress|not-started|status-changed|"
                 r"current-state-changed|added-by-owner|plan-approval|domain-approval|fix-verification|use-7|keep-14|"
                 r"review_scope|owner_change)\b|(?<![\w/.-])(primary|revisit|unverified|measured|baseline)(?![\w/.-])")


class Shown(browser.ViewerTestCase):
    build_ledger = classmethod(lambda cls, L: H.build(L))

    def open_all(self, page):
        """Open every row that opens in place, so its text is on the page too."""
        page.js("""() => document.querySelectorAll("details:not([open]) > summary").forEach(s => s.click())""")
        page.settle()

    def test_no_enum_values_in_text(self):
        self.write("UPDATE gates SET status='answered', answer_value='use-7' WHERE code='GATE-0001'")
        try:
            page = self.open("#/")
            for hash_ in VIEWS:
                page.hash(hash_)
                self.open_all(page)
                text = page.js("document.getElementById('app').innerText")
                found = [m.group(0) for m in RAW.finditer(text)]
                self.assertEqual(found, [], hash_)
                self.assertNotRegex(text, r"(?m)^all$|\tall\t", hash_)          # the stored scope "all"
            self.assertEqual(page.js("[scopeName('all'), scopeName(null), scopeName('src')]"),
                             ["Whole system", "Whole system", "src"])
            page.hash("#/inbox")
            self.assertIn("You answered: Change to 7 days", page.text("#gates-answered"))
        finally:
            self.write("""UPDATE gates SET status='open', answer_value=NULL, answered_by=NULL, answered_via=NULL,
                          answered_at=NULL WHERE code='GATE-0001'""")

    def test_no_status_on_load(self):
        self.write("UPDATE gates SET status='answered', answer_value='approve' WHERE code='GATE-0002'")
        try:
            page = self.open("#/")
            for hash_ in VIEWS + ("#/explore", "#/explore/issues"):
                page.hash(hash_)
                self.assertEqual(page.count(".status"), 0, hash_)      # a status line follows an action, never a load
            page.hash("#/inbox")
            self.assertEqual(page.count("#gates-answered .callout.answer"), 1)   # a standing fact is a callout
        finally:
            self.write("""UPDATE gates SET status='open', answer_value=NULL, answered_by=NULL, answered_via=NULL,
                          answered_at=NULL WHERE code='GATE-0002'""")

    def test_review_page_lists_artifacts(self):
        page = self.open("#/review/R-0001")
        rows = page.page.locator("h2:text('Files and documents') + div tbody tr")
        self.assertEqual(rows.count(), 2)
        report = rows.filter(has_text="Report")
        link = report.locator("a")
        self.assertEqual((link.inner_text(), link.get_attribute("href"), link.get_attribute("rel")),
                         ("Open report", "https://example.com/reports/security", "noopener noreferrer"))
        self.assertEqual(report.locator("code").inner_text(), ".code-review/reviews/R-0001/security.md")
        self.assertEqual(rows.filter(has_text="Brief").locator("a").count(), 0)      # a file with no web address
        # a reviewer's row opens to what it was asked and what it answered
        run = page.page.locator("details.run").first
        self.assertEqual(page.count("details.run .body *"), 0)
        run.locator("summary").first.click()
        page.settle()
        self.assertEqual(run.locator("details summary").all_inner_texts(), ["What it was asked", "What it answered"])
        self.assertIn("Review the sign-in code for weak randomness.", run.locator(".mdtext").first.text_content())
        self.assertIn("session-42", run.inner_text())
        self.assertIn("a test harness", run.inner_text())
        # test runs, requirements and the review's history
        self.assertEqual(page.texts("h2:text('Test runs') + div tbody td:first-child"), ["python -m unittest"])
        self.assertEqual(page.page.locator("h2:text('Test runs') + div tbody td").nth(1).inner_text(), "Baseline")
        self.assertIn("Differs", page.text("h2:text('Requirements') ~ div tbody"))
        self.assertGreater(page.count("h2:text('History') + .timeline > div"), 0)

    def test_review_shows_points_by_area(self):
        """Step 172: the per-area counts the review route returns are on the review's page."""
        page = self.open("#/review/R-0001")
        d = self.server.get("/api/review/R-0001").json()
        self.assertEqual(page.contents("h2:text('Points by area') + div thead th"),
                         ["Area", "Points", "High", "Medium", "Low", "Missing tests", "Confirmed", "New", "Carried over",
                          "Came back", "False"])
        rows = [r.split("\t") for r in page.texts("h2:text('Points by area') + div tbody tr")]
        labels = self.server.get("/api/overview").json()["labels"]["domains"]
        self.assertEqual([[c.strip() for c in r[:3]] for r in rows],
                         [[labels[x["domain_code"]], str(x["points"]), str(x["high"])] for x in d["summary"]])

    def test_review_header_shows_skill_version(self):
        page = self.open("#/review/R-0001")
        kv = dict(zip(page.texts("#app > .card dl.kv dt"), page.texts("#app > .card dl.kv dd")))
        self.assertEqual(kv["Skill version"], "2.4.0")
        self.assertEqual(kv["Working folder"], ".code-review/reviews/R-0001")
        row = page.page.locator("h2:text('Code reviewed') + div tbody tr").first
        self.assertIn("The only repository.", row.inner_text())
        self.assertEqual(row.locator("a").get_attribute("href"), "https://example.com/demo.git")
        # a review with nothing to say about itself has no empty card (R-0002 has no verdict in the demo: check both)
        self.assertEqual(page.count("#app > .card:empty"), 0)

    def test_report_is_drawn_as_a_document(self):
        page = self.open("#/review/R-0001")
        doc = page.page.locator(".card:has(h2:text('The review document')) .md")
        self.assertEqual(doc.locator("h2, h3").all_inner_texts(), ["Verdict", "What to fix first"])
        self.assertEqual(doc.locator("ol li").all_inner_texts(), ["Sign-in codes", "The notice period"])
        self.assertEqual(doc.locator("table th").all_inner_texts(), ["AREA", "POINTS"])
        self.assertEqual(doc.locator("b").inner_text(), "sound")
        self.assertEqual(doc.locator("code").inner_text(), "src/auth.py")
        text = doc.inner_text()
        for raw in ("**", "##", "| ---", "`"):
            self.assertNotIn(raw, text)
        page.hash("#/plan/P-0001")
        about = page.page.locator(".card:has(h2:text('About this plan')) .md")
        self.assertEqual(about.locator("li").all_inner_texts(), ["CI", "Sign-in"])
        self.assertNotIn("**", about.inner_text())

    def test_plan_header_links(self):
        page = self.open("#/plan/P-0001")
        links = {a.inner_text(): a.get_attribute("href") for a in page.page.locator("#app > dl.kv a").all()}
        self.assertEqual(links["Open the plan document"], "https://example.com/plan-doc")
        self.assertEqual(links["Open the shared page"], "https://example.com/plan-page")
        self.assertEqual(links["R-0001"], "#/review/R-0001")
        self.assertIn("last updated", page.text("#app p.lede"))
        gate = page.page.locator(".card:has(h2:text('Questions about this plan')) tbody tr a")
        self.assertEqual(gate.get_attribute("href"), "#/inbox/GATE-0001")
        self.assertGreater(page.count("h2:text('History') + .timeline > div"), 0)

    def test_plan_rail_shows_step_range(self):
        page = self.open("#/plan/P-0001")
        self.assertEqual(page.contents("nav.rail button small"), ["step 1", "steps 2–3"])

    def test_step_card_shows_done_at(self):
        page = self.open("#/plan/P-0001/phase/2")
        step = page.page.locator(".steps details.item").first
        step.locator("summary").click()
        page.settle()
        kv = dict(zip(step.locator("dt").all_inner_texts(), step.locator("dd").all_inner_texts()))
        self.assertIn("Started", kv)
        self.assertRegex(kv["Done"], r"Nov 2, 2026")
        self.assertRegex(step.locator("dd", has_text="on main").inner_text(), r"^[0-9a-f]{10} on main")

    def test_discovery_on_plan_issue_and_home(self):
        page = self.open("#/plan/P-0001")
        box = page.page.locator("details.card", has_text="found during fixes")
        self.assertEqual(box.locator("summary").inner_text(), "1 defect found during fixes")
        box.locator("summary").click()
        row = box.locator("tbody tr td")
        self.assertEqual([row.nth(i).inner_text() for i in (0, 3, 4, 5)],
                         ["R-0001 · FOUND-1", "2", self.ids["found_issue"], "3"])
        page.hash("#/issue/" + self.ids["found_issue"])
        item = page.page.locator("details.item").first
        item.locator("summary").click()
        self.assertEqual(item.locator("dd a", has_text="Fix work on step 2 of P-0001").get_attribute("href"),
                         "#/plan/P-0001")
        page.hash("#/")
        self.assertEqual(dict(zip(page.texts(".facts span"), page.texts(".facts b")))["found during fixes"], "1")

    def test_goals_page_shows_lifecycle(self):
        page = self.open("#/review/R-0001")
        link = page.page.locator("h2:text('Design goals used') + ul a").first
        self.assertEqual(link.get_attribute("href"), "#/goals/G-01")
        page.after(link.click)
        self.assertEqual(page.text("#app h1"), "Design goals")
        self.assertEqual(page.js("document.activeElement.id"), "goal-G-01")
        head = page.contents("#app thead tr >> nth=0 >> th")
        self.assertEqual(head[-3:], ["Written down", "Retired", "Used by"])
        old = page.page.locator("tbody tr", has_text="Reports are sent weekly.")
        cells = old.locator("td").all_inner_texts()
        self.assertEqual((cells[3], cells[-3], cells[-2]), ("Retired", "Jan 5, 2026", "Jun 1, 2026"))
        self.assertEqual(page.page.locator("#goal-G-01 td").last.inner_text(), "R-0001, R-0002")

    def test_how_it_works_in_the_viewer(self):
        page = self.open("#/guide")
        self.assertEqual(page.text("#app h1"), "How it works")
        self.assertEqual(page.js("document.querySelector('.tabs a[aria-current=page]').textContent"), "How it works")
        kv = dict(zip(page.texts(".card dl.kv dt"), page.texts(".card dl.kv dd")))
        self.assertEqual(kv["Waiting on you"], "2 questions")                 # the live count
        self.assertRegex(kv["Fix plan"], r"^P-0001 · In progress · 1 of 3 steps done$")
        self.assertRegex(kv["Latest review"], r"^R-0002 · ")
        self.assertNotIn("This copy was made", kv)
        self.assertEqual(page.texts(".md h3")[:2], ["What this site is", "How a review runs"])
        self.assertEqual(page.count(".md ol li"), 8)
        self.assertIn("Point", page.texts(".md table td:first-child"))
        text = page.text("#app")
        for raw in ("**", "##", "| ---", "Claude"):
            self.assertNotIn(raw, text)


class HubShown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = fixtures.Ledger()
        H.build(cls.L)
        cls.hub = cls.L.root / "hub.html"
        cls.L.run_ok("progress-page", "-o", str(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_hub_shows_the_same(self):
        p = browser.ViewerPage(self.browser, self.hub.as_uri())
        self.addCleanup(p.close)
        p.page.goto(self.hub.as_uri() + "#guide")
        p.settle()
        self.assertEqual(p.texts(".md h3")[:2], ["What this site is", "How a review runs"])
        self.assertIn("This copy was made", p.texts(".card dl.kv dt"))
        go = lambda slug: (p.js(f"location.hash = {json.dumps(slug)}"), p.settle(quiet_ms=80))  # noqa: E731
        go("review-R-0001")
        self.assertEqual(p.page.locator(".card:has(h2:text('The review document')) .md h2").inner_text(), "Verdict")
        run = p.page.locator("details.run").first
        run.locator("summary").first.click()
        p.settle()
        self.assertIn("Review the sign-in code", run.text_content())
        self.assertEqual(p.count("h2:text('Files and documents') + div tbody tr"), 2)
        go("plan-P-0001")
        self.assertEqual(p.contents("nav.rail button small"), ["step 1", "steps 2–3"])
        self.assertEqual(p.page.locator("details.card", has_text="found during fixes").count(), 1)
        go("goals")
        self.assertIn("Retired", p.contents("#app thead tr >> nth=0 >> th"))
        go("home")
        self.assertEqual(p.count(".status"), 0)
        self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], []))


if __name__ == "__main__":
    unittest.main()
