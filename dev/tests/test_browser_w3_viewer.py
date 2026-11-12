"""Every view on a stress ledger, an empty ledger and a ledger with no plan (plan P-0001 step 152).

test_viewer_stress_ledger               150 points with long text, a gate with six options, a text-only gate, twelve
                                        links between issues: every view draws, nothing is wider than a 390 px phone
test_viewer_empty_and_noplan_ledgers    an empty ledger and one with a review but no plan: every view draws its
                                        empty state and no page error is raised
"""
import unittest

from dev.tests import browser, fixtures

LONG = ("A sentence long enough to wrap several times on a phone, with an unbroken path like "
        "src/services/scheduling/requests/validation/limits/notice_period_rules_and_overrides.py in it. ") * 6
# The address of every view, and of one record of each kind the stress ledger has.
VIEWS = ["#/", "#/guide", "#/inbox", "#/decisions", "#/history", "#/reviews", "#/review/R-0001", "#/goals",
         "#/issues", "#/issue/ISS-0002", "#/plans", "#/plan/P-0001", "#/trends", "#/explore", "#/explore/findings"]
EMPTY_VIEWS = ["#/", "#/guide", "#/inbox", "#/decisions", "#/history", "#/reviews", "#/goals", "#/issues", "#/plans",
               "#/trends", "#/explore"]
PHONE = {"width": 390, "height": 844}


def stress(ledger):
    ids = fixtures.seed_demo(ledger)
    L, r1, repo = ledger, ids["reviews"]["R-0001"], ids["repository"]
    issues = []
    for n in range(150):
        sev = "MHLT"[n % 4]
        f = L.finding(r1, f"STRESS-M{n}", severity=sev, kind="test" if sev == "T" else "defect", domain_code="spec",
                      summary=f"Point {n}: " + LONG[:200], detail=LONG, recommendation=LONG)
        L.location(f, "src/requests.py", 1, 2, repository_id=repo)
        i = L.issue(None, first_seen_review_id=r1, title=f"Stress issue {n} " + LONG[:120], domain_code="spec")
        L.match(f, i, "new")
        issues.append(i)
    for n in range(12):
        L.relation(issues[n], issues[n + 1], "related-to", "Same rule. " + LONG[:150], established_in_review_id=r1)
    L.gate("GATE-0003", "decision", "Six ways to go " + LONG[:80], question=LONG[:300],
           options=[{"value": f"v{n}", "label": f"Option {n} " + LONG[:60], "description": LONG[:120]}
                    for n in range(6)], review_id=r1)
    L.gate("GATE-0004", "other", "No options at all", question="Say it in your words.", review_id=r1)   # text only
    L.commit()   # no finish_demo: these points are in no plan step, so check would list them
    return ids


def no_plan(ledger):
    L = ledger
    repo = L.default_repository()
    r = L.review("R-0001", status="running")
    L.finding(r, "SPEC-M1", summary="One point, no plan yet.")
    L.commit()
    return {"review": r, "repository": repo}


class Views(browser.ViewerTestCase):
    """Each view at phone width: it draws a heading, raises no page error and is no wider than the screen."""
    views = VIEWS

    def check_views(self):
        for hash_ in self.views:
            with self.subTest(view=hash_):
                page = self.new_page(viewport=PHONE).goto(hash_)
                self.addCleanup(page.close)
                self.assertTrue(page.text("#app h1"), "no heading")
                self.assertNotIn("could not be loaded", page.text("#app"))
                wide = page.js("document.documentElement.scrollWidth - window.innerWidth")
                self.assertLessEqual(wide, 0, f"{wide} px wider than the screen")
                self.assertClean(page, hash_)


class Stress(Views):
    @classmethod
    def build_ledger(cls, ledger):
        return stress(ledger)

    def test_viewer_stress_ledger(self):
        self.check_views()
        page = self.new_page(viewport=PHONE).goto("#/inbox")
        self.addCleanup(page.close)
        six = page.page.locator(".card.gate", has_text="Six ways to go")
        self.assertEqual(six.locator(".opts label").count(), 6)
        none = page.page.locator(".card.gate", has_text="No options at all")
        self.assertEqual(none.locator(".opts").count(), 0)
        self.assertEqual(none.locator("textarea").get_attribute("placeholder"), "Your answer")
        review = self.new_page(viewport=PHONE).goto("#/review/R-0001")
        self.addCleanup(review.close)
        self.assertEqual(review.count("h2:text('Links between issues made in this review') + div tbody tr"), 12)


class Empty(Views):
    views = EMPTY_VIEWS

    @classmethod
    def build_ledger(cls, ledger):
        return {}

    def test_viewer_empty_and_noplan_ledgers(self):
        self.check_views()
        page = self.new_page(viewport=PHONE).goto("#/")
        self.addCleanup(page.close)
        self.assertIn("No reviews yet.", page.text("#app"))
        self.assertIn("No fix plan yet.", page.text("#app"))
        self.assertEqual(self.new_page().goto("#/inbox").text("#app .empty"), "Nothing is waiting on you right now.")


class NoPlan(Views):
    views = EMPTY_VIEWS + ["#/review/R-0001"]

    @classmethod
    def build_ledger(cls, ledger):
        return no_plan(ledger)

    def test_viewer_empty_and_noplan_ledgers(self):
        self.check_views()
        page = self.new_page(viewport=PHONE).goto("#/plan/P-0001")
        self.addCleanup(page.close)
        self.assertEqual(page.text("#app h1"), "Not found")
        # the 404 the page was asked to show is the one console line it logs
        self.assertEqual(len(page.console_errors), 1)
        self.assertIn("404", page.console_errors[0])
        page.console_errors.clear()
        plans = self.new_page().goto("#/plans")
        self.addCleanup(plans.close)
        self.assertEqual(plans.text("#app .empty"), "Nothing here yet.")


if __name__ == "__main__":
    unittest.main()
