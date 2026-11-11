"""The owner interview and design goals in the viewer and the published hub (plan P-0001 step 129, OWN-5): the review
page shows the interview by area with the owner's answers and the goals proposed from it, the Design goals view lists
every goal by area with its status and sources, and the goal-approval gate is under Waiting on you, answerable there
(the answer approves the goals). Skipped unless LEDGER_BROWSER_TESTS=1 and a browser is available
(dev/tests/browser.py)."""
import json
import unittest

from dev.tests import browser, fixtures


def build_interview(L):
    """R-0001 (approved, spec included) with two questions (one answered), a proposed goal on an approval gate and an
    approved whole-project goal; the owner is A-0002."""
    rid = L.review("R-0001")
    L.review_domain(rid, "spec")
    L.commit()
    L.run_ok("add-actor", "manager", "review manager")
    L.run_ok("add-actor", "owner", "the owner")
    L.run_ok("interview-ask", "--review", "R-0001", "--domain", "spec", "--why", "the handler parses any body",
             "Which request bodies must the handler accept?", "--actor", "A-0001")
    L.run_ok("interview-ask", "--review", "R-0001", "--why", "no README names a maintainer",
             "Who will look after this next year?", "--actor", "A-0001")
    L.run_ok("interview-answer", "IQ-0001", "JSON only, under one megabyte.", "--actor", "A-0001")
    L.run_ok("propose-goal", "--from", "IQ-0002", "Anyone can run it from the README alone.", "--actor", "A-0001")
    L.run_ok("approve-goals", "G-01", "--actor", "A-0002")
    L.run_ok("propose-goal", "--domain", "spec", "--from", "IQ-0001", "The handler refuses anything but JSON.",
             "--gate", "--actor", "A-0001")


class InterviewInViewer(browser.ViewerTestCase):
    @classmethod
    def build_ledger(cls, L):
        build_interview(L)

    def test_review_page_shows_interview_and_goals(self):
        page = self.open("#/review/R-0001")
        card = page.contents(".interview")[0]
        self.assertIn("Which request bodies must the handler accept?", card)
        self.assertIn("Asked because: the handler parses any body", card)
        self.assertIn("JSON only, under one megabyte.", card)
        self.assertIn("Whole project", card)
        self.assertIn("Not answered yet", card)
        body = " ".join(page.contents("#app"))
        self.assertIn("Design goals from this interview", body)
        self.assertIn("The handler refuses anything but JSON.", body)
        self.assertIn("IQ-0001: Which request bodies must the handler accept?", body)
        # the status label comes from goal_statuses (the approval test may have run first)
        self.assertTrue("Waiting for your approval" in body or "Approved" in body)

    def test_goals_view_by_area(self):
        page = self.open("#/goals")
        self.assertEqual(page.text("#app h1"), "Design goals")
        body = page.text("#app")
        self.assertIn("Anyone can run it from the README alone.", body)
        self.assertIn("Approved", body)
        self.assertIn("IQ-0002: Who will look after this next year?", body)
        self.assertEqual(page.contents("#app h3")[0], "Whole project")

    def test_approval_under_waiting_on_you(self):
        page = self.open("#/inbox")
        cards = [c for c in page.texts(".card.gate") if "Approve the design goals" in c]
        self.assertEqual(len(cards), 1)
        self.assertIn("G-02", cards[0])
        self.assertIn("The handler refuses anything but JSON.", cards[0])
        gate = page.page.locator(".card.gate", has_text="Approve the design goals")
        gate.locator("input[value=approve]").check()
        page.after(lambda: gate.locator("button", has_text="Send answer").click())
        self.assertIn("Answer saved", page.text("#app"))
        self.ledger.con.rollback()
        self.assertEqual(self.ledger.scalar("SELECT status FROM design_goals WHERE code='G-02'"), "approved")
        self.assertEqual(self.ledger.scalar("SELECT a.kind FROM design_goals g JOIN actors a "
                                            "ON a.id=g.approved_by_actor_id WHERE g.code='G-02'"), "owner")


class InterviewInHub(unittest.TestCase):
    """The published hub (progress-page) draws the same views from its snapshot."""

    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = fixtures.Ledger()
        build_interview(cls.L)
        cls.hub = cls.L.root / "hub.html"
        cls.L.run_ok("progress-page", "-o", str(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_hub_shows_interview_goals_and_approval(self):
        p = browser.ViewerPage(self.browser, self.hub.as_uri())
        try:
            p.page.goto(self.hub.as_uri())
            p.settle()
            for slug, want in (("review-R-0001", "Which request bodies must the handler accept?"),
                               ("goals", "Anyone can run it from the README alone."),
                               ("inbox", "Approve the design goals")):
                p.js(f"location.hash = {json.dumps(slug)}")
                p.settle(quiet_ms=60)
                self.assertIn(want, p.text("#app"), slug)
            self.assertEqual(p.page_errors, [])
            self.assertEqual(p.outside_requests, [])
        finally:
            p.close()
