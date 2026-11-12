"""Plan P-0001 step 152: the plan page against the owner's original fix-plan page (header numbers, progress line,
phase heading and its line of steps and points, step tags and the order of an open step, a point's report on
hover, the paging button, the display face). Skipped cleanly when Playwright or a browser is missing.

The comparison, section by section (kept, fixed, or different on purpose):
  header        kept: h1, the big numbers, the bar's three segments. fixed: a "need you" number; the line under the
                bar reads "N done, N in progress, N waiting on you, N not started. Updated <time>.". different: the
                lede is the plan's status and approval (the ledger has them; the original had a written sentence).
  filter        kept: one search box (words or a point id) and one select by state or by what the owner must do.
  phase rail    kept: sticky, number, name, the accent background on the current phase, a scrolling strip under
                820 px. different: the count is done/total with the step range (more than the original's count).
                fixed: the heading reads "Phase N: name" and the goal is followed by "Steps a to b, closing N review
                points.". different: the route is #/plan/P-0001/phase/N (one router for every page).
  step cards    kept: open in place, number in monospace, state and owner-need tags, the point's id and coloured
                severity chip. fixed: an "N points" tag; the open card starts Progress, Commit, Change, Proof,
                Depends on, Changes behaviour, Needs you; a point shows its report on hover; "Show N more" paging.
  palette       kept: the light and dark palettes are the original's. fixed: headings use a grotesk stack
                (system fonts only; the page loads none).
"""
import re
import unittest

from dev.tests import browser, fixtures

ORDER = ["Progress", "Commit", "Commits", "Change", "Proof", "Depends on", "Changes behaviour", "Needs you"]


class PlanLook(browser.ViewerTestCase):
    @classmethod
    def build_ledger(cls, L):
        ids = fixtures.seed_demo(L)
        fixtures.finish_demo(L)
        pid = L.plan("P-0002", "Long plan")
        ph = L.phase(pid, 1, "Everything")
        for n in range(1, 321):
            L.step(pid, ph, n, f"Long step {n}", no_findings_reason="A long list for paging.")
        L.commit()
        return ids

    def test_header_numbers_and_progress_line(self):
        page = self.open("#/plan/P-0001")
        facts = page.contents("#app .facts > div span")
        self.assertRegex(" ".join(facts), r"steps? needs? you")
        line = page.text("#app .progress .note")
        self.assertRegex(line, r"^\d+ done, \d+ in progress, \d+ waiting on you, \d+ not started\. Updated .+\.$")
        self.assertClean(page)

    def test_phase_heading_and_line(self):
        page = self.open("#/plan/P-0001")
        self.assertRegex(page.text("#app .steps h2"), r"^Phase 1: \S")
        self.assertRegex(page.text("#app .steps p.phase-span"),
                         r"^Steps? \d+( to \d+)?, closing \d+ review points?\.$")

    def test_step_tags_order_and_report(self):
        page = self.open("#/plan/P-0001")
        tags = page.contents("#app .steps details.item .tags .tag")
        self.assertTrue(any(re.fullmatch(r"\d+ points?", t) for t in tags), tags)
        page.after(lambda: page.page.locator("#app .steps details.item > summary").first.click())
        labels = page.contents("#app .steps details.item[open] dt")
        known = [x for x in labels if x in ORDER]
        self.assertEqual(known, sorted(known, key=ORDER.index), labels)
        self.assertNotIn("Behaviour change", labels)
        self.assertNotIn("Needs from you", labels)
        titles = page.js("[...document.querySelectorAll('#app details.item[open] .refline')].map(e => e.title)")
        self.assertTrue(titles and all(t.startswith("Report: ") for t in titles), titles)

    def test_show_more_paging(self):
        page = self.open("#/plan/P-0002")
        page.after(lambda: page.page.fill("#app input[type=search]", "long step"))
        page.page.wait_for_selector("#app .steps button.btn.ghost")
        self.assertEqual(page.text("#app .steps button.btn.ghost"), "Show 20 more")

    def test_display_face_is_grotesk(self):
        page = self.open("#/plan/P-0001")
        face = page.js("getComputedStyle(document.querySelector('h1')).fontFamily")
        self.assertNotIn("rounded", face.lower())
        self.assertIn("Helvetica", face)


if __name__ == "__main__":
    unittest.main()
