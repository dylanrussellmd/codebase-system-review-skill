"""Errors, gate answers, the badge and decisions in a real browser (plan P-0001 steps 154, 155, 161, 174, 176, 177,
190). Skipped cleanly without Playwright or a browser."""
import json
import unittest

from dev.tests import browser, fixtures

BUSY = "The ledger is busy; your answer was not saved, try again."
CALL = """async path => { try { return ["ok", await api(path)]; } catch (e) { return [e.constructor.name, e.message, e.status]; } }"""


def _reopen(test, code):
    test.write("""UPDATE gates SET status='open', answer_value=NULL, answer_text=NULL, answered_by=NULL,
                  answered_via=NULL, answered_at=NULL WHERE code=?""", (code,))


class Errors(browser.ViewerTestCase):
    def test_page_api_error_message(self):
        page = self.open("#/reviews")
        sent = []
        page.page.on("request", lambda r: sent.append((r.method, r.url, r.headers)))
        self.assertEqual(page.js(CALL, "plans")[0], "ok")
        method, url, headers = [s for s in sent if s[1].endswith("/api/plans")][-1]
        self.assertEqual((method, headers.get("x-ledger-token")), ("GET", self.server.token))   # sends the token
        page.allow_console_errors = True
        page.js(CALL, "nothing")
        res = page.js("""async () => { try { await api("sql", {sql: "SELECT 1 AS one"}); return "ok"; } catch (e) { return e.message; } }""")
        self.assertEqual(res, "ok")
        post = [s for s in sent if s[0] == "POST"][-1]
        self.assertEqual(post[2].get("content-type"), "application/json")                       # a POST is JSON
        # not ok: the server's own sentence, with its status
        self.assertEqual(page.js(CALL, "review/R-9999"), ["ApiError", "no such review", 404])
        self.assertEqual(page.js(CALL, "table/issues?limit=abc"), ["ApiError", "limit must be a whole number.", 400])
        self.assertEqual(page.js(CALL, "sql"), ["ApiError", "This address takes POST, not GET.", 405])
        # not JSON at all
        page.page.route("**/api/plans", lambda r: r.fulfill(status=200, content_type="text/html", body="<p>hi"))
        self.assertEqual(page.js(CALL, "plans"), ["ApiError", "The viewer sent back something unreadable.", 200])
        # a busy ledger: the server's sentence is shown as it is
        page.page.route("**/api/trends", lambda r: r.fulfill(status=503, content_type="application/json", body=json.dumps(
            {"error": "The ledger is busy (the agent is writing to it); try again in a moment."})))
        page.hash("#/trends")
        self.assertEqual(page.text("#app .status.err"),
                         "The ledger is busy (the agent is writing to it); try again in a moment.")
        self.assertEqual(page.text("#app p.lede"), "Reload the page to try again.")

    def test_missing_review_message(self):
        page = self.new_page()
        page.allow_console_errors = True   # Chrome logs each 404
        page.goto("#/review/R-9999")
        self.assertEqual(page.text("#app h1"), "Not found")
        self.assertEqual(page.text("#app p.lede"), "There is no review R-9999 in this ledger.")
        self.assertEqual(page.page.locator("#app a").get_attribute("href"), "#/reviews")
        self.assertNotIn("still running", page.text("#app"))
        self.assertEqual(page.text("#live"), "There is no review R-9999 in this ledger.")
        for hash_, text, link in (("#/issue/ISS-9999", "There is no issue ISS-9999 in this ledger.", "#/issues?status=all"),
                                  ("#/plan/P-9999", "There is no fix plan P-9999 in this ledger.", "#/plans")):
            page.hash(hash_)
            self.assertEqual((page.text("#app p.lede"), page.page.locator("#app a").get_attribute("href")), (text, link))
        page.after(lambda: page.page.click("#app a"))
        self.assertEqual(page.text("#app h1"), "Fix plans")

    test_page_unknown_review_shows_error = test_missing_review_message

    def test_error_messages_are_sentences(self):
        page = self.open("#/reviews")
        page.allow_console_errors = True
        seen = []

        def shown():
            text = page.text("#app")
            seen.append(text)
            return text

        page.hash("#/review/R-9999")                                    # 404
        self.assertIn("There is no review R-9999 in this ledger.", shown())
        page.js("() => { TOKEN = 'a-token-from-before-the-restart'; }")  # the viewer was restarted: the old token
        page.hash("#/plans")
        self.assertIn("The viewer was restarted. Copy anything you typed, then open the new link that ledger.py "
                      "serve printed.", shown())
        self.assertEqual(page.text("#app h1"), "This page could not be loaded")
        bare = self.new_page(token=None)                               # opened without the link's key
        bare.allow_console_errors = True
        bare.goto("#/issues")
        self.assertIn("This address is missing the viewer's key. Open the link that ledger.py serve printed.",
                      bare.text("#app"))
        seen.append(bare.text("#app"))
        page.page.route("**/api/**", lambda r: r.abort())              # the server is gone
        page.hash("#/trends")
        self.assertIn("The viewer is not running. Start it again with ledger.py serve and open the link it prints.",
                      shown())
        for text in seen:
            for raw in ("fatal:", "no such", "Failed to fetch", "TypeError", "wrong token", "undefined"):
                self.assertNotIn(raw, text)

    def test_stale_token_keeps_what_was_typed(self):
        """A 403 on an answer leaves the form as it is, so the owner can copy what they typed."""
        page = self.open("#/inbox")
        page.allow_console_errors = True
        card = page.page.locator(".card.gate", has_text="Approve the domains for R-0002")
        card.locator("textarea").fill("Add the docs too.")
        page.js("() => { TOKEN = 'a-token-from-before-the-restart'; }")
        card.locator("button", has_text="Send answer").click()
        card.locator(".status.err").wait_for()
        self.assertIn("The viewer was restarted. Copy anything you typed", card.locator(".status.err").inner_text())
        self.assertEqual(card.locator("textarea").input_value(), "Add the docs too.")
        self.assertEqual(self.ledger.scalar("SELECT status FROM gates WHERE code='GATE-0002'"), "open")


class Gates(browser.ViewerTestCase):
    def card(self, page, title):
        return page.page.locator(".card.gate", has=page.page.locator("h2, h3", has_text=title))

    def test_gate_card_keeps_text_on_503(self):
        page = self.open("#/inbox")
        page.allow_console_errors = True
        page.page.route("**/api/gates/*/answer", lambda r: r.fulfill(
            status=503, content_type="application/json", body=json.dumps({"error": BUSY})))
        card = self.card(page, "Default notice period")
        card.locator("input[value=use-7]").check()
        card.locator("textarea").fill("Seven is enough.")
        send = card.locator("button", has_text="Send answer")
        send.click()
        card.locator(".status.err").wait_for()
        self.assertEqual(card.locator(".status.err").inner_text(), BUSY)
        self.assertEqual(page.text("#live"), BUSY)
        self.assertTrue(card.locator("input[value=use-7]").is_checked())
        self.assertEqual(card.locator("textarea").input_value(), "Seven is enough.")
        self.assertTrue(send.is_enabled())
        self.assertTrue(page.js("!!document.querySelector('#gates-open #gate-GATE-0001')"))
        # the ledger is free again: the same form sends the same answer
        page.page.unroute("**/api/gates/*/answer")
        try:
            send.click()
            page.page.wait_for_selector("#gates-answered #gate-GATE-0001")
            page.settle()
            self.ledger.con.rollback()
            self.assertEqual(self.ledger.one("SELECT answer_value, answer_text FROM gates WHERE code='GATE-0001'"),
                             {"answer_value": "use-7", "answer_text": "Seven is enough."})
        finally:
            _reopen(self, "GATE-0001")

    def test_gate_flow_two_tabs(self):
        a, b = self.open("#/inbox"), self.open("#/inbox")
        b.allow_console_errors = True   # Chrome logs the 409
        try:
            self.assertEqual((a.text(".tabs .badge"), b.text(".tabs .badge")), ("2", "2"))
            ca = self.card(a, "Default notice period")
            ca.locator("input[value=keep-14]").check()
            ca.locator("textarea").fill("Fourteen stays.")
            ca.locator("button", has_text="Send answer").click()
            a.page.wait_for_selector("#gates-answered #gate-GATE-0001")
            a.settle()
            self.assertEqual(a.text(".tabs .badge"), "1")                                  # the badge drops
            cb = self.card(b, "Default notice period")
            cb.locator("input[value=use-7]").check()
            cb.locator("button", has_text="Send answer").click()
            b.page.wait_for_selector("#gates-answered #gate-GATE-0001")
            b.settle()
            moved = self.card(b, "Default notice period")
            self.assertIn("You answered: Keep 14 days — Fourteen stays.", moved.inner_text())   # A's answer, by label
            self.assertIn("Already answered in another tab or in the chat", moved.inner_text())
            self.assertEqual(moved.locator("button").count(), 0)
            self.assertEqual(b.text(".tabs .badge"), "1")
            self.assertEqual(b.count("#gates-open .card.gate"), 1)
            self.ledger.con.rollback()
            self.assertEqual(self.ledger.scalar("SELECT answer_value FROM gates WHERE code='GATE-0001'"), "keep-14")
            b.hash("#/history")
            row = b.page.locator("tbody tr", has_text="Default notice period")
            self.assertEqual(row.locator("td").nth(4).inner_text(), "Keep 14 days — Fourteen stays.")
        finally:
            _reopen(self, "GATE-0001")

    def test_new_gate_shows_on_focus(self):
        page = self.open("#/inbox")
        draft = self.card(page, "Approve the domains for R-0002").locator("textarea")
        draft.fill("Half a thought")
        self.assertEqual(page.text(".tabs .badge"), "2")
        self.write("""INSERT INTO gates(code, kind, title, question, allow_text) VALUES
                      ('GATE-0003', 'other', 'A late question', 'Anything to add?', 1)""")
        try:
            page.page.wait_for_timeout(2100)   # a refresh is not repeated within two seconds of the last
            page.after(lambda: page.js("() => { window.dispatchEvent(new Event('focus')); }"))
            page.page.wait_for_selector("#gates-open #gate-GATE-0003")
            page.settle()
            self.assertEqual(page.text(".tabs .badge"), "3")
            self.assertEqual(page.text("#live"), "A new question is waiting on you.")
            self.assertEqual(draft.input_value(), "Half a thought")        # nothing else was redrawn
            self.assertEqual(page.count(".card.gate"), 3)
            # the badge follows on any page, and a hidden window does not ask
            page.hash("#/plans")
            self.write("UPDATE gates SET status='withdrawn' WHERE code='GATE-0003'")
            page.page.wait_for_timeout(2100)
            page.after(lambda: page.js("() => { document.dispatchEvent(new Event('visibilitychange')); }"))
            self.assertEqual(page.text(".tabs .badge"), "2")
        finally:
            self.write("UPDATE gates SET status='withdrawn' WHERE code='GATE-0003' AND status='open'")

    def test_page_tabs_badge(self):
        page = self.open("#/issues")
        self.assertEqual(page.text(".tabs .badge"), "2")
        self.assertEqual(page.js("document.querySelector('.tabs a[aria-current=page]').textContent"), "Issues")
        self.assertEqual(page.page.locator(".tabs a", has_text="Waiting on you").get_attribute("aria-label"),
                         "Waiting on you, 2 waiting")
        self.write("UPDATE gates SET status='answered', answer_value='approve' WHERE code='GATE-0002'")
        self.write("UPDATE gates SET status='answered', answer_value='use-7' WHERE code='GATE-0001'")
        try:
            page.hash("#/plan/P-0001")
            self.assertEqual(page.count(".tabs .badge"), 0)                 # nothing left to answer: no badge
            self.assertIsNone(page.page.locator(".tabs a", has_text="Waiting on you").get_attribute("aria-label"))
            self.assertEqual(page.js("document.querySelector('.tabs a[aria-current=page]').textContent"), "Fix plans")
            page.hash("#/")
            self.assertEqual(page.text(".facts div:first-child b"), "0")
            self.assertEqual(page.count("#app p.status"), 0)
            self.assertEqual(page.text("#app > p.note"), "2 answers of yours waiting for the agent to act on.")
        finally:
            _reopen(self, "GATE-0001")
            _reopen(self, "GATE-0002")

    def test_home_fetches_overview_once(self):
        page = self.open("#/")
        api = [u.split("/api/")[1] for u in page.requests if "/api/" in u]
        self.assertEqual(sorted(api), ["checks", "home", "overview"])
        self.assertEqual(page.text(".facts div:nth-child(4) b"), "1")       # filled in once the checks arrive
        self.assertEqual(page.count(".card:has(h2:text('Housekeeping for the agent')) tbody tr"),
                         self.ledger.count("ledger_checks"))
        n = len(page.requests)
        page.hash("#/reviews")
        self.assertEqual([u.split("/api/")[1] for u in page.requests[n:]], ["overview", "reviews"])

    def test_decisions_page(self):
        page = self.open("#/inbox")
        link = self.card(page, "Default notice period").locator(".refline a", has_text="Decision D-0001")
        page.after(link.click)
        self.assertEqual(page.location_hash(), "#/decisions/D-0001")
        self.assertEqual(page.text("#app h1"), "Decisions")
        self.assertEqual(page.js("document.activeElement.id"), "decision-D-0001")
        self.assertEqual(page.js("document.querySelector('.tabs a[aria-current=page]').dataset.k"), "inbox")
        row = page.page.locator("#decision-D-0001 td")
        self.assertEqual(row.nth(2).inner_text(), page.js("L.decision[%s]" % json.dumps(
            self.ledger.scalar("SELECT status FROM decisions WHERE code='D-0001'"))))
        self.assertNotIn(row.nth(2).inner_text(), ("open", "decided", "revisit"))   # the label, not the stored word


class HubGates(unittest.TestCase):
    """The published hub has the same inbox: decisions, the chat hint and the note beside Send."""

    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = L = fixtures.Ledger()
        fixtures.seed_demo(L)
        fixtures.finish_demo(L)
        L.x("PRAGMA ignore_check_constraints = ON")   # as an older ledger could hold it
        L.gate("GATE-0003", "other", "No way to answer", "This gate has no options and no text box.", allow_text=0,
               blocking=0)
        L.x("PRAGMA ignore_check_constraints = OFF")
        L.commit()
        cls.hub = L.root / "hub.html"
        L.run_ok("progress-page", "-o", str(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_hub_inbox(self):
        p = browser.ViewerPage(self.browser, self.hub.as_uri())
        self.addCleanup(p.close)
        p.page.goto(self.hub.as_uri() + "#inbox")
        p.settle()
        self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], []))
        self.assertEqual(p.text(".tabs .badge"), "3")
        hint = p.page.locator(".card.gate", has_text="No way to answer")
        self.assertEqual(hint.locator("p.note").inner_text(), "Answer this one in the chat with the agent.")
        self.assertIn("Decisions", p.contents("#app h2.sec"))
        p.page.locator(".card.gate .refline a", has_text="Decision D-0001").click()
        p.settle()
        self.assertEqual(p.location_hash(), "#decisions-D-0001")
        self.assertEqual(p.js("document.activeElement.id"), "decision-D-0001")
        p.js("location.hash = 'home'")
        p.settle(quiet_ms=80)
        self.assertEqual(p.count(".card:has(h2:text('Housekeeping for the agent')) tbody tr"), self.L.count("ledger_checks"))
        self.assertEqual(p.count(".status.err"), 0)
        self.assertNotIn("Claude", p.text("body"))


if __name__ == "__main__":
    unittest.main()
