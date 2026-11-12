"""Viewer: a continuation's first-seen event is labelled as a new issue that continues the earlier one (plan P-0001
step 84, ISS-0228), and the review document and plan narrative stored in the ledger are shown (step 96, ISS-0299).
Skipped unless LEDGER_BROWSER_TESTS=1 and a browser is available (see dev/tests/browser.py)."""
from dev.tests import browser


class RecordsInViewer(browser.ViewerTestCase):
    @classmethod
    def build_ledger(cls, L):
        r1, r2 = L.review("R-0001"), L.review("R-0002")
        old = L.issue("ISS-0001", first_seen_review_id=r1)
        L.finding(r1, issue_id=old)
        new = L.issue("ISS-0002", first_seen_review_id=r2)
        L.finding(r2, issue_id=new, match_type="continuation")
        L.relation(new, old, "continuation-of", "The fix moved the bug.")
        L.x("UPDATE reviews SET report_md=? WHERE id=?", ("# Review R-0002\n\n<b>not bold</b> stays text.", r2))
        plan = L.plan("P-0001", reviews=[r1], narrative_md="Read the phases in order.")
        L.phase(plan, 1)  # a plan with no phases does not draw yet (ISS-0096)
        L.commit()

    def test_continuation_label_on_issue_timeline(self):
        page = self.open("#/issue/ISS-0002")
        self.assertIn("Raised as a new issue that continues ISS-0001", page.text(".timeline"))
        page = self.open("#/review/R-0002")
        facts = page.text(".facts")
        self.assertIn("1newissue0", facts.replace("\n", "").replace(" ", ""))

    def test_report_and_narrative_shown(self):
        page = self.open("#/review/R-0002")
        body = page.text("#app")
        self.assertIn("The review document", body)
        self.assertIn("<b>not bold</b> stays text.", body)  # shown as text, never as HTML
        page = self.open("#/plan/P-0001")
        self.assertIn("Read the phases in order.", page.text("#app"))
