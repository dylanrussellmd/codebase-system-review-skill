"""Viewer: a step opens with the problem it fixes (plan P-0002 step 329), an issue shows Now only when it differs
from Before (step 331), and stored text renders `code` and **bold** (step 333).
Skipped unless LEDGER_BROWSER_TESTS=1 and a browser is available (see dev/tests/browser.py)."""
from dev.tests import browser


class TextFields(browser.ViewerTestCase):
    @classmethod
    def build_ledger(cls, L):
        r1 = L.review("R-0001")
        same = L.issue("ISS-0001", first_seen_review_id=r1, title="init overwrites a newer `ledger.py`",
                       before_state="init copies `ledger.py` over a **newer** one",
                       current_state="init copies `ledger.py` over a **newer** one")
        moved = L.issue("ISS-0002", first_seen_review_id=r1, title="Second issue",
                        before_state="It fails every time.", current_state="It fails on Mondays only.")
        f1 = L.finding(r1, issue_id=same, point_id="SEC-H1", summary="The point's own summary.")
        f2 = L.finding(r1, issue_id=moved, point_id="SEC-H2")
        plan = L.plan("P-0001", reviews=[r1])
        ph = L.phase(plan, 1)
        L.step(plan, ph, 1, "Keep the newer copy", change="Compare versions in `init`.", findings=[f1, f2])
        L.commit()

    def test_now_only_when_it_differs(self):
        page = self.open("#/issue/ISS-0001")
        self.assertIn("Before", page.texts("dl.kv dt"))
        self.assertNotIn("Now", page.texts("dl.kv dt"))
        page = self.open("#/issue/ISS-0002")
        dts = page.texts("dl.kv dt")
        self.assertIn("Now", dts)
        self.assertEqual(page.texts("dl.kv dd")[dts.index("Now")], "It fails on Mondays only.")

    def test_inline_code_and_bold(self):
        page = self.open("#/issue/ISS-0001")
        before = page.page.locator("dl.kv dt", has_text="Before").locator("xpath=following-sibling::dd[1]")
        self.assertEqual(before.inner_text(), "init copies ledger.py over a newer one")
        self.assertEqual(before.locator("code").all_text_contents(), ["ledger.py"])
        self.assertEqual(before.locator("b").all_text_contents(), ["newer"])

    def test_step_opens_with_its_problem(self):
        page = self.open("#/plan/P-0001")
        item = page.page.locator(".steps details.item").first
        item.locator("summary").click()
        page.settle()
        self.assertEqual(item.locator("dl.kv dt").first.inner_text(), "Problem")
        problem = item.locator("dl.kv dd").first
        self.assertEqual(problem.locator(":scope > div").all_text_contents(),
                         ["ISS-0001 init overwrites a newer ledger.py", "ISS-0002 Second issue"])
        self.assertEqual(problem.locator("code").all_text_contents(), ["ledger.py"])
        change = item.locator("dl.kv dt", has_text="Change").locator("xpath=following-sibling::dd[1]")
        self.assertEqual(change.locator("code").all_text_contents(), ["init"])
