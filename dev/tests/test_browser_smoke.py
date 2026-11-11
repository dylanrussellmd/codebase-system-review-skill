"""Viewer smoke tests in a real browser (plan step 25; closes SPEC-T7, TEST-M7, UI-T1).

Every route of the viewer is drawn from the demo ledger at desktop (1280 px) and phone (390 px) widths, in light and
dark colour schemes; no page error, console error, error banner or request leaving 127.0.0.1 is allowed. Skipped
cleanly when Playwright or a browser is missing (see dev/tests/browser.py).
"""
import subprocess
import sys
import unittest

from dev.tests import browser, fixtures

# Every view, with and without arguments, plus an unknown hash (falls back to the overview).
ROUTES = [
    "#/", "#/inbox", "#/history", "#/reviews", "#/review/R-0001", "#/review/R-0002", "#/issues",
    "#/issues?status=&severity=M&q=auth", "#/issue/ISS-0001", "#/issue/ISS-0004", "#/plans", "#/plan/P-0001",
    "#/plan/P-0001/phase/2", "#/trends", "#/explore", "#/explore/issues", "#/no-such-view",
]


class ViewerSmoke(browser.ViewerTestCase):

    def _walk(self, viewport, scheme):
        page = self.new_page(viewport=viewport, color_scheme=scheme)
        page.goto(ROUTES[0])
        for hash_ in ROUTES:
            where = f"at {hash_} ({viewport['width']} px, {scheme})"
            page.hash(hash_) if hash_ != ROUTES[0] else None
            self.assertEqual(page.page_errors, [], where)
            self.assertEqual(page.count(".status.err"), 0, f"error banner {where}: {page.texts('.status.err')}")
            self.assertGreater(page.count("#app h1"), 0, f"no heading {where}")
            self.assertNotEqual(page.text("#app h1"), "Could not load this view", where)
            self.assertEqual(page.outside_requests, [], where)
        self.assertEqual(page.console_errors, [])
        return page

    def test_viewer_routes_render_without_error(self):
        # 17 hashes at desktop width in the light scheme.
        page = self._walk(browser.DESKTOP, "light")
        self.assertTrue(all(u.startswith(self.server.url) for u in page.requests), page.requests)

    def test_viewer_smoke_all_views(self):
        for viewport in (browser.DESKTOP, browser.PHONE):
            for scheme in ("light", "dark"):
                if (viewport, scheme) == (browser.DESKTOP, "light"):
                    continue  # covered by test_viewer_routes_render_without_error
                with self.subTest(width=viewport["width"], scheme=scheme):
                    page = self._walk(viewport, scheme)
                    bg = page.js("getComputedStyle(document.body).backgroundColor")
                    # The dark palette applies under prefers-color-scheme: dark (theme "auto").
                    self.assertEqual(bg, "rgb(18, 23, 21)" if scheme == "dark" else "rgb(247, 248, 246)")

    def test_unknown_hash_shows_overview(self):
        page = self.open("#/no-such-view")
        self.assertEqual(page.text("#app h1"), "proj")
        self.assertEqual(page.texts(".tabs a[aria-current=page]"), ["Overview"])


class GateFormSuccess(browser.ViewerTestCase):
    """Answering a gate in the browser saves it and redraws the inbox (own ledger: this writes)."""

    def test_page_gate_form_success(self):
        page = self.open("#/inbox")
        card = page.page.locator(".card.gate", has_text="Default notice period")
        card.locator("input[type=radio][value=keep-14]").check()
        card.locator("textarea").fill("Keep it for now.")
        card.locator("button", has_text="Send answer").click()
        page.page.wait_for_selector("text=Answer saved. The agent will act on it the next time it checks.")
        # route() runs again after 1.2 s; the gate is then listed as answered.
        page.page.wait_for_selector("h3:has-text('Answered, waiting for the agent to act')", timeout=5000)
        page.settle()
        answered = page.page.locator(".card.gate", has_text="Default notice period")
        self.assertIn("You answered: Keep 14 days — Keep it for now.", answered.inner_text())
        row = self.ledger.one("SELECT status, answer_value, answer_text, answered_via FROM gates WHERE code=?",
                              ("GATE-0001",))
        self.assertEqual(row, {"status": "answered", "answer_value": "keep-14", "answer_text": "Keep it for now.",
                               "answered_via": "viewer"})


class HarnessSkips(unittest.TestCase):
    """Without Playwright the harness reports a reason and raises SkipTest instead of failing (no browser needed)."""

    def test_browser_tests_skip_cleanly_without_playwright(self):
        code = ("import os; os.environ['LEDGER_BROWSER_TESTS'] = '1'\n"
                "import sys, unittest; sys.modules['playwright'] = None; sys.modules['playwright.sync_api'] = None\n"
                "from dev.tests import browser\n"
                "assert browser.get_browser() is None\n"
                "assert 'Playwright is not installed' in browser.skip_reason(), browser.skip_reason()\n"
                "try:\n    browser.require_browser()\nexcept unittest.SkipTest as e:\n    print('SKIP', e)\n")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(fixtures.REPO_ROOT), capture_output=True, text=True,
                           timeout=60, env=fixtures.git_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SKIP browser tests skipped: Playwright is not installed", p.stdout)

    def test_browser_tests_skip_cleanly_without_chrome(self):
        code = ("import sys, unittest\n"
                "import os; os.environ['LEDGER_CHROME'] = '/nonexistent/chrome'; os.environ['LEDGER_BROWSER_TESTS'] = '1'\n"
                "from dev.tests import browser\n"
                "browser.DEFAULT_CHROME = '/nonexistent/chrome'\n"
                "assert browser.chrome_path() is None\n"
                "class Chromium:\n"
                "    def launch(self, **kw):\n"
                "        assert 'executable_path' not in kw, kw\n"
                "        raise RuntimeError(\"Executable doesn't exist\")\n"
                "class PW:\n"
                "    chromium = Chromium()\n"
                "    def stop(self): pass\n"
                "class Start:\n"
                "    def start(self): return PW()\n"
                "browser.sync_playwright = lambda: Start()\n"
                "assert browser.get_browser() is None\n"
                "try:\n    browser.require_browser()\nexcept unittest.SkipTest as e:\n    print('SKIP', e)\n")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(fixtures.REPO_ROOT), capture_output=True, text=True,
                           timeout=60, env=fixtures.git_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SKIP browser tests skipped: no browser could be launched (Playwright Chromium)", p.stdout)

    def test_browser_tests_are_off_unless_switched_on(self):
        code = ("import os, unittest; os.environ.pop('LEDGER_BROWSER_TESTS', None)\n"
                "from dev.tests import browser\n"
                "try:\n    browser.require_browser()\nexcept unittest.SkipTest as e:\n    print('SKIP', e)\n"
                "assert browser._state['tried'] is False, 'no browser may be started when switched off'\n")
        p = subprocess.run([sys.executable, "-c", code], cwd=str(fixtures.REPO_ROOT), capture_output=True, text=True,
                           timeout=60, env=fixtures.git_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SKIP browser tests skipped: set LEDGER_BROWSER_TESTS=1 to run them", p.stdout)


if __name__ == "__main__":
    unittest.main()
