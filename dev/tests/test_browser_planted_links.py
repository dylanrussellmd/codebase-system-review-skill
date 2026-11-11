"""Planted javascript: links in the viewer (plan P-0001 step 35: verification only; ISS-0080).

The viewer builds three links from ledger data without checking the scheme: finding_locations.permalink ("Open on
the code host" in a reference's code panel), reviews.summary_doc_url ("Open the review document") and
step_commits.pr_url ("pull request" in a step). The page's CSP has script-src 'unsafe-inline', which does not block
javascript: URLs. This test plants a harmless payload in all three, clicks each link, and pins what happens today in
Chrome: the link is drawn with the javascript: href, but because it opens with target=_blank rel=noopener, the click
opens a new about:blank tab in which the payload does not run, and nothing runs in the viewer page. Firefox and
WebKit (Safari) could not be checked here (Playwright could not download them), so this pins Chromium only.

Step 44 flipped the "link drawn with the javascript: href" assertions: the schema now refuses non-http(s) values in
these columns (and in repositories.permalink_template), so the test plants them with PRAGMA ignore_check_constraints,
as in a ledger made before schema 2 or edited by hand; the page's safeHref() shows such a value as plain text and
never as an href. test_viewer_drops_non_http_links checks that on the review and plan pages.
"""
import unittest

from dev.tests import browser

PAYLOAD = "javascript:window.__pwned=1;document.title='PWNED';void(0)"

# (column, view hash, link text, how to reveal the link)
LINKS = [
    ("finding_locations.permalink", "#/review/R-0001", "Open on the code host", "code"),
    ("reviews.summary_doc_url", "#/review/R-0001", "Open the review document", None),
    ("step_commits.pr_url", "#/plan/P-0001/phase/2", "pull request", None),
]


class PlantedLinks(browser.ViewerTestCase):

    @classmethod
    def build_ledger(cls, ledger):
        ids = super().build_ledger(ledger)
        ledger.x("PRAGMA ignore_check_constraints = ON")  # the schema CHECKs refuse these values (step 44)
        ledger.x("UPDATE finding_locations SET permalink=?", (PAYLOAD,))
        ledger.x("UPDATE reviews SET summary_doc_url=?", (PAYLOAD,))
        ledger.x("UPDATE step_commits SET pr_url=?", (PAYLOAD,))
        ledger.x("PRAGMA ignore_check_constraints = OFF")
        return ids

    def probe(self, hash_, text, reveal):
        """Open the view, reveal and click the link; returns (href or None, payload ran in the viewer,
        payload ran in any new tab, urls of new tabs)."""
        page = self.open(hash_)
        page.js("document.querySelectorAll('details').forEach(d => d.open = true)")
        if reveal == "code":
            btn = page.page.get_by_role("button", name="Show code at this commit").first
            page.after(btn.click)
        link = page.page.locator("a", has_text=text).first
        if not link.count():
            return None, False, False, []
        href = link.get_attribute("href")
        popups = []
        page.context.on("page", lambda p: popups.append(p))
        link.click()
        page.page.wait_for_timeout(500)
        ran_here = bool(page.js("window.__pwned === 1 || document.title === 'PWNED'"))
        ran_popup, urls = False, []
        for p in popups:
            try:
                p.wait_for_load_state(timeout=2000)
            except browser.PlaywrightError:
                pass
            urls.append(p.url)
            ran_popup = ran_popup or bool(p.evaluate("window.__pwned === 1 || document.title === 'PWNED'"))
        return href, ran_here, ran_popup, urls

    def test_control_same_tab_javascript_link_blocked_by_viewer_csp(self):
        # Control, changed on purpose: when step 35 probed, the CSP had script-src 'unsafe-inline' and a javascript:
        # link without target=_blank ran in the viewer page. Step 43 (p3-server) removed 'unsafe-inline', so the CSP
        # now blocks it too (Chrome logs a CSP violation); step 44's safeHref() means no such link is drawn at all.
        page = self.open("#/")
        page.allow_console_errors = True
        page.js("""p => { const a = document.createElement('a'); a.id = 'ctl'; a.href = p; a.textContent = 'ctl';
                          document.body.append(a); }""", PAYLOAD)
        page.page.click("#ctl")
        page.page.wait_for_timeout(300)
        self.assertFalse(page.js("window.__pwned === 1 || document.title === 'PWNED'"))
        self.assertTrue(any("Content Security Policy" in m or "violates" in m for m in page.console_errors),
                        page.console_errors)

    def test_planted_javascript_links_are_not_links(self):
        for column, hash_, text, reveal in LINKS:
            with self.subTest(column=column):
                href, ran_here, ran_popup, urls = self.probe(hash_, text, reveal)
                # ISS-0080 fixed (step 44): no link is drawn, so there is nothing to click
                self.assertIsNone(href)
                self.assertFalse(ran_here, f"{column}: payload ran in the viewer page")
                self.assertFalse(ran_popup, f"{column}: payload ran in the new tab {urls}")
                self.assertEqual(urls, [])

    def test_viewer_drops_non_http_links(self):
        for hash_ in ("#/review/R-0001", "#/plan/P-0001", "#/plan/P-0001/phase/2"):
            with self.subTest(hash_):
                page = self.open(hash_)
                page.js("document.querySelectorAll('details').forEach(d => d.open = true)")
                btns = page.page.get_by_role("button", name="Show code at this commit")
                if btns.count():
                    page.after(btns.first.click)
                self.assertEqual(page.js("document.querySelectorAll('a[href^=\"javascript\" i]').length"), 0)
                self.assertEqual(page.js("[...document.querySelectorAll('a[href]')].filter(a => !/^(#|https?:)/"
                                         ".test(a.getAttribute('href'))).length"), 0)
        # The value is still visible, as text, so the owner can see what was stored.
        page = self.open("#/review/R-0001")
        self.assertIn("Open the review document: " + PAYLOAD, page.js("document.body.innerText"))


if __name__ == "__main__":
    unittest.main()
