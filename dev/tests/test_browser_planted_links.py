"""Planted javascript: links in the viewer (plan P-0001 step 35: verification only; ISS-0080).

The viewer builds three links from ledger data without checking the scheme: finding_locations.permalink ("Open on
the code host" in a reference's code panel), reviews.summary_doc_url ("Open the review document") and
step_commits.pr_url ("pull request" in a step). The page's CSP has script-src 'unsafe-inline', which does not block
javascript: URLs. This test plants a harmless payload in all three, clicks each link, and pins what happens today in
Chrome: the link is drawn with the javascript: href, but because it opens with target=_blank rel=noopener, the click
opens a new about:blank tab in which the payload does not run, and nothing runs in the viewer page. Firefox and
WebKit (Safari) could not be checked here (Playwright could not download them), so this pins Chromium only.

Step 44 (only http/https for data-derived links) flips the "link drawn with the javascript: href" assertions.
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
        ledger.x("UPDATE finding_locations SET permalink=?", (PAYLOAD,))
        ledger.x("UPDATE reviews SET summary_doc_url=?", (PAYLOAD,))
        ledger.x("UPDATE step_commits SET pr_url=?", (PAYLOAD,))
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

    def test_control_same_tab_javascript_link_runs_under_viewer_csp(self):
        # Control for the probe: the viewer's CSP does not block javascript: URLs, so the same payload in a link
        # WITHOUT target=_blank runs in the viewer page. Only target=_blank keeps the planted links harmless in Chrome.
        page = self.open("#/")
        page.js("""p => { const a = document.createElement('a'); a.id = 'ctl'; a.href = p; a.textContent = 'ctl';
                          document.body.append(a); }""", PAYLOAD)
        page.page.click("#ctl")
        page.page.wait_for_timeout(300)
        self.assertTrue(page.js("window.__pwned === 1 && document.title === 'PWNED'"))

    def test_current_planted_javascript_links(self):
        for column, hash_, text, reveal in LINKS:
            with self.subTest(column=column):
                href, ran_here, ran_popup, urls = self.probe(hash_, text, reveal)
                # characterization: ISS-0080 - the link is drawn with the planted javascript: href (no scheme check)
                self.assertEqual(href, PAYLOAD)
                # Chrome: the click opens one blank tab (target=_blank, noopener) and the payload runs nowhere
                self.assertFalse(ran_here, f"{column}: payload ran in the viewer page")
                self.assertFalse(ran_popup, f"{column}: payload ran in the new tab {urls}")
                self.assertEqual(urls, ["about:blank"])


if __name__ == "__main__":
    unittest.main()
