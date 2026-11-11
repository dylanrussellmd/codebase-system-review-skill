"""Viewer inbox, gate answer form, reviews list, review page, point cards and code viewer in a real browser (plan step
27; closes TEST-T208, T209, T210, T214, T216, T217, T219, T220, T221, T222).

Characterization tests: they pin what the page does today. Where today's behaviour is a known defect, the assertion
carries a '# characterization: ISS-xxxx' comment, so the step that fixes it changes that assertion on purpose.
Skipped cleanly when Playwright or a browser is missing (see dev/tests/browser.py).
"""
import unittest

from dev.tests import browser, fixtures

SAVED = "Answer saved. The agent will act on it the next time it checks."
EMPTY_ANSWER = "Choose an option or write an answer."


def _reopen(test, code):
    test.write("""UPDATE gates SET status='open', answer_value=NULL, answer_text=NULL, answered_by=NULL,
                  answered_via=NULL, answered_at=NULL WHERE code=?""", (code,))


class Inbox(browser.ViewerTestCase):
    """The demo plus an answered gate, a text-only gate, an unanswerable gate, and an ungated step and decision."""

    @classmethod
    def build_ledger(cls, L):
        ids = fixtures.seed_demo(L)
        L.gate("GATE-0003", "plan-approval", "Approve the fix plan", "Approve P-0001?", plan_id=ids["plan"],
               options=[{"value": "approve", "label": "Approve the plan"}, {"value": "change", "label": "Change it"}])
        L.update("gates", L.id_of("gates", "GATE-0003"), status="answered", answer_value="approve",
                 answer_text="Go ahead.", answered_by="owner", answered_via="chat")
        L.gate("GATE-0004", "other", "Anything else?", "Is there anything the review should look at?", allow_text=1,
               blocking=0)
        # schema 3 refuses a gate nobody can answer; this one is planted as an older ledger could hold it
        L.x("PRAGMA ignore_check_constraints = ON")
        L.gate("GATE-0005", "other", "No way to answer", "This gate has no options and no text box.", allow_text=0,
               blocking=0)
        L.x("PRAGMA ignore_check_constraints = OFF")
        L.update("steps", ids["steps"][1], status="waiting-owner")
        L.decision("D-0002", "Keep the weekend rule?", status="open")
        return ids

    def card(self, page, title):
        return page.page.locator(".card.gate", has=page.page.locator("h2, h3", has_text=title))

    def test_page_inbox_sections(self):
        page = self.open("#/inbox")
        self.assertEqual(page.text("#app h1"), "Waiting on you")
        # Open gates first (blocking first, then oldest), then the answered ones under their own heading.
        self.assertEqual(page.texts(".card.gate h2, .card.gate h3"), ["Default notice period", "Approve the domains for R-0002",
                                                       "Anything else?", "No way to answer", "Approve the fix plan"])
        self.assertEqual(page.contents("#app h2.sec"), ["Answered, waiting for the agent to act",
                                                 "Also waiting on you (answer in the chat)"])
        answered = self.card(page, "Approve the fix plan")
        self.assertIn("You answered: Approve the plan — Go ahead.", answered.inner_text())
        self.assertEqual(answered.locator("button").count(), 0)
        self.assertEqual(answered.locator(".tag").first.inner_text(), "Approve fix plan")
        first = self.card(page, "Default notice period")
        self.assertEqual(first.locator(".tag").all_inner_texts(), ["Decision", "Work is paused until you answer"])
        self.assertEqual(first.locator(".refline a").all_inner_texts(), ["Review R-0001", "Plan P-0001 step 3",
                                                                         "Issue ISS-0002"])
        self.assertEqual(self.card(page, "Anything else?").locator(".tag").all_inner_texts(), ["Question"])
        # Owner-queue items with no gate: answered in the chat, listed in a table.
        rows = page.js("[...document.querySelectorAll('#app table tbody tr')].map(r => "
                       "[...r.cells].map(c => c.textContent))")
        # The step has no owner question or progress note: Question falls back to its change text (was blank before
        # plan step 66, ISS-0305). The decision has no revisit note: its Question is blank.
        self.assertEqual(rows, [["Plan step P-0001 #1", "Your input", "Run every suite in CI", "Add a CI workflow."],
                                ["Decision D-0002", "Your decision", "Keep the weekend rule?", ""]])
        self.assertEqual(page.page.locator("a", has_text="See every question and answer so far").get_attribute("href"),
                         "#/history")

    def test_page_gate_form_shape(self):
        page = self.open("#/inbox")
        c1 = self.card(page, "Default notice period")
        self.assertEqual(c1.locator(".opts label").all_inner_texts(),
                         ["Keep 14 days\nCurrent behaviour stays the default.", "Change to 7 days"])
        self.assertEqual(c1.locator("input[type=radio]").evaluate_all("rs => rs.map(r => r.value)"),
                         ["keep-14", "use-7"])
        self.assertEqual(c1.locator("textarea").get_attribute("placeholder"),
                         "Add a note or a different answer (optional)")
        c4 = self.card(page, "Anything else?")
        self.assertEqual(c4.locator("input[type=radio]").count(), 0)
        self.assertEqual(c4.locator("textarea").get_attribute("placeholder"), "Your answer")
        # characterization: ISS-0042 allow_text=0 with no options: only a Send button, so the gate cannot be answered
        c5 = self.card(page, "No way to answer")
        self.assertEqual((c5.locator("input").count(), c5.locator("textarea").count()), (0, 0))
        self.assertEqual(c5.locator("button").all_inner_texts(), ["Send answer"])
        page.allow_console_errors = True
        c5.locator("button").click()
        page.page.wait_for_selector(".card.gate .status.err")
        self.assertEqual(c5.locator(".status.err").inner_text(), EMPTY_ANSWER)

    def test_page_gate_form_empty_error(self):
        page = self.open("#/inbox")
        page.allow_console_errors = True  # the 400 answer is logged by Chrome
        c1 = self.card(page, "Default notice period")
        send = c1.locator("button", has_text="Send answer")
        send.click()
        c1.locator(".status.err").wait_for()
        self.assertEqual(c1.locator(".status.err").inner_text(), EMPTY_ANSWER)
        self.assertTrue(send.is_enabled())                 # an error re-enables Send
        c1.locator("textarea").fill("   ")                 # whitespace only is empty too
        send.click()
        page.settle()
        self.assertEqual(c1.locator(".status.err").inner_text(), EMPTY_ANSWER)
        self.assertEqual(self.ledger.scalar("SELECT status FROM gates WHERE code='GATE-0001'"), "open")

    def test_page_gate_form_text_only(self):
        page = self.open("#/inbox")
        c4 = self.card(page, "Anything else?")
        try:
            c4.locator("textarea").fill("Look at the export too.")
            c4.locator("button", has_text="Send answer").click()
            page.page.wait_for_selector(f"text={SAVED}")
            row = self.ledger.one("SELECT status, answer_value, answer_text, answered_via FROM gates WHERE code=?",
                                  ("GATE-0004",))
            self.assertEqual(row, {"status": "answered", "answer_value": None,
                                   "answer_text": "Look at the export too.", "answered_via": "viewer"})
            # After 1.2 s the inbox is drawn again and the gate is listed as answered.
            page.page.wait_for_function("() => [...document.querySelectorAll('.card.gate')].some(c => "
                                        "c.textContent.includes('You answered: Look at the export too.'))",
                                        timeout=5000)
            page.settle()
        finally:
            _reopen(self, "GATE-0004")

    def test_page_inbox_redraw_erases_other_drafts(self):
        page = self.open("#/inbox")
        try:
            self.card(page, "Approve the domains for R-0002").locator("textarea").fill("Draft I have not sent")
            c1 = self.card(page, "Default notice period")
            c1.locator("input[value=use-7]").check()
            c1.locator("button", has_text="Send answer").click()
            page.page.wait_for_selector(f"text={SAVED}")
            draft = self.card(page, "Approve the domains for R-0002").locator("textarea")
            self.assertEqual(draft.input_value(), "Draft I have not sent")   # still there until the redraw
            page.page.wait_for_function("t => !document.getElementById('app').textContent.includes(t)", arg=SAVED,
                                        timeout=5000)
            page.settle()
            # characterization: ISS-0029 the redraw after an answer wipes unsent text typed into another gate
            self.assertEqual(self.card(page, "Approve the domains for R-0002").locator("textarea").input_value(), "")
        finally:
            _reopen(self, "GATE-0001")

    def test_page_history_uses_option_labels(self):
        page = self.open("#/history")
        row = page.page.locator("tbody tr", has_text="Approve the fix plan")
        self.assertEqual(row.locator("td").nth(4).inner_text(), "Approve the plan Go ahead.")
        self.assertEqual(row.locator("td").nth(3).inner_text(), "Answered, waiting for the agent")


class Reviews(browser.ViewerTestCase):
    """The reviews list, the review page and the code viewer on the demo (read only)."""

    def test_page_reviews_row_nav(self):
        page = self.open("#/reviews")
        self.assertEqual(page.texts("tbody td:first-child"), ["R-0002", "R-0001"])
        page.after(lambda: page.page.locator("tbody tr", has_text="R-0001").click())
        self.assertEqual(page.location_hash(), "#/review/R-0001")
        self.assertEqual(page.text("#app h1"), "R-0001 · Comprehensive review R-0001")
        page.hash("#/reviews")
        page.page.locator("tbody tr", has_text="R-0002").locator("a").focus()
        page.after(lambda: page.page.keyboard.press("Enter"))
        self.assertEqual(page.location_hash(), "#/review/R-0002")

    def _points(self, page):
        return page.texts("details.item > summary .n")

    def test_page_review_filters(self):
        page = self.open("#/review/R-0001")
        self.assertEqual(page.text(".tools ~ div p.note"), "3 of 3 points")
        self.assertEqual(self._points(page), ["SEC-H1", "SPEC-M1", "SPEC-L1"])
        cases = [("#fd", "security", ["SEC-H1"]), ("#fd", "spec", ["SPEC-M1", "SPEC-L1"]),
                 ("#fs", "L", ["SPEC-L1"]), ("#fm", "new", ["SEC-H1", "SPEC-M1", "SPEC-L1"]),
                 ("#fv", "confirmed", ["SEC-H1", "SPEC-M1", "SPEC-L1"])]
        for sel, value, want in cases:
            with self.subTest(select=sel, value=value):
                page.page.select_option(sel, value)
                page.settle()
                self.assertEqual(self._points(page), want)
                self.assertEqual(page.text(".tools ~ div p.note"), f"{len(want)} of 3 points")
                page.page.select_option(sel, "")
        self.assertEqual(page.page.locator("#fs option").all_inner_texts(), ["All severities", "High", "Medium", "Low"])
        self.assertEqual(page.page.locator("#fd option").all_inner_texts(),
                         ["All areas", "Security", "Spec conformance"])
        page.hash("#/review/R-0002")
        self.assertEqual(page.page.locator("#fm option").all_inner_texts(), ["Any match", "New", "Still present"])
        page.page.select_option("#fm", "still-present")
        page.settle()
        self.assertEqual(self._points(page), ["SPEC-M1"])

    def test_page_review_search_paths(self):
        page = self.open("#/review/R-0001")
        search = page.page.locator("input[type=search]")
        for term, want in (("timeoff.py", ["SPEC-M1", "SPEC-L1"]), ("src/auth", ["SEC-H1"]), ("sec-h1", ["SEC-H1"]),
                           ("ISS-0003", ["SPEC-L1"]), ("predictable", ["SEC-H1"]), ("  WEEKEND ", ["SPEC-L1"]),
                           ("nothing like this", []), ("", ["SEC-H1", "SPEC-M1", "SPEC-L1"])):
            with self.subTest(term):
                search.fill(term)
                page.settle()
                self.assertEqual(self._points(page), want)
                self.assertEqual(page.text(".tools ~ div p.note"), f"{len(want)} of 3 points")

    def test_page_review_gate_answers_raw(self):
        self.write("UPDATE gates SET status='answered', answer_value='approve' WHERE code='GATE-0002'")
        try:
            page = self.open("#/review/R-0002")
            row = page.page.locator(".card:has(h2:text('Questions asked during this review')) tbody tr")
            # characterization: ISS-0103 the review page shows the raw answer value, not the option label
            self.assertEqual(row.locator("td").nth(3).inner_text(), "approve")
            page.after(row.click)
            # characterization: ISS-0256 a gate row on the review page links to the inbox, not to the gate
            self.assertEqual(page.location_hash(), "#/inbox")
        finally:
            _reopen(self, "GATE-0002")

    def test_page_code_viewer_toggle_and_highlight(self):
        page = self.open("#/review/R-0001")
        item = page.page.locator("details.item", has_text="SEC-H1")
        item.locator("summary").click()
        ref = item.locator(".ref")
        self.assertEqual(ref.locator(".refline").first.locator(":scope > *").all_text_contents(),
                         ["Primary", "demo", "src/auth.py", "lines 1–3", "make_code",
                          f"at commit {self.ids['commits'][0][:10]}", "Show code at this commit"])
        btn = ref.locator(".refline").first.locator("button")
        self.assertEqual(btn.inner_text(), "Show code at this commit")
        btn.click()
        ref.locator(".code").wait_for()
        page.settle()
        self.assertEqual(btn.inner_text(), "Hide code")
        rows = ref.locator(".code tr")
        self.assertEqual(rows.count(), 5)                   # the whole 5-line file (8 lines of context around 1-3)
        self.assertEqual(ref.locator(".code tr.hit td.ln").all_text_contents(), ["1 cited", "2 cited", "3 cited"])
        self.assertEqual(ref.locator(".code tr td:not(.ln)").all_inner_texts(),
                         fixtures.DEMO_FILES_1["src/auth.py"].rstrip("\n").split("\n"))
        self.assertEqual(ref.locator("p.note").count(), 0)  # read from git and the saved excerpt matches
        sha = self.ids["commits"][0]
        self.assertEqual(ref.locator(".refline code").last.inner_text(),
                         f"git -C '.' show '{sha}:src/auth.py' | sed -n '1,3p'")
        link = ref.locator("a", has_text="Open on the code host")
        self.assertEqual(link.get_attribute("href"), f"https://github.com/example/demo/blob/{sha}/src/auth.py#L1-L3")
        self.assertEqual((link.get_attribute("target"), link.get_attribute("rel")), ("_blank", "noopener noreferrer"))
        btn.click()
        self.assertEqual(btn.inner_text(), "Show code at this commit")
        self.assertEqual(ref.locator(".code").count(), 0)


class PointsAndCode(browser.ViewerTestCase):
    """A ledger made for point cards, code-viewer fallbacks and long point lists (own ledger)."""

    GONE = "deadbeef" * 5   # a commit that is not in the repository

    @classmethod
    def build_ledger(cls, L):
        L.repo.write("src/a.py", "one = 1\ntwo = 2\nthree = 3\n")
        sha = L.repo.commit("a")
        r1 = L.review("R-0001", commit_sha=sha, status="handed-off")
        r2 = L.review("R-0002", commit_sha=sha, previous_review_id=r1)
        old = L.finding(r1, "SPEC-M1", summary="Old point.")
        # fixed once, so R-0002 can raise it as a regression (schema 3 allows that match type only then)
        iss = L.issue("ISS-0001", first_seen_review_id=r1, title="The issue", status="fixed", after_state="Fixed once.")
        L.match(old, iss, "new")
        made = {}
        for pid, match, verif in (("SPEC-M2", "still-present", "confirmed"), ("SPEC-M3", "recurrence", "false"),
                                  ("SPEC-M4", "regression", "unverified"), ("SPEC-M5", "changed", "partly")):
            made[pid] = L.finding(r2, pid, summary=f"Point {pid}.", issue_id=iss, match_type=match,
                                  verification=verif)
        exc = L.finding(r2, "SPEC-L1", severity="L", summary="Excerpt only.")
        L.location(exc, "src/a.py", 5, 6, commit_sha=cls.GONE, excerpt="five = 5\nsix = 6")
        none = L.finding(r2, "SPEC-L2", severity="L", summary="Nothing saved.")
        L.location(none, "src/a.py", 3, 3, commit_sha=cls.GONE)
        mis = L.finding(r2, "SPEC-L3", severity="L", summary="Wrong excerpt.")
        L.location(mis, "src/a.py", 2, 2, excerpt="not what is there")
        r3 = L.review("R-0003", commit_sha=sha, previous_review_id=r2)
        for n in range(1, 231):
            L.finding(r3, f"TEST-T{n:03d}", severity="T", kind="test", domain_code="tests" if n % 2 else "spec",
                      summary=f"Missing test number {n}.")
        return {"sha": sha}

    def item(self, page, pid):
        return page.page.locator("details.item", has=page.page.locator("summary .n", has_text=pid))

    def test_page_finding_item_tags(self):
        page = self.open("#/review/R-0002")
        tags = {pid: self.item(page, pid).locator("summary .tag").evaluate_all(
                    "ts => ts.map(t => [t.className, t.textContent])")
                for pid in ("SPEC-M2", "SPEC-M3", "SPEC-M4", "SPEC-M5", "SPEC-L1")}
        self.assertEqual(tags["SPEC-M2"], [["tag M", "Medium"], ["tag", "Still present"], ["tag ok", "Confirmed"]])
        self.assertEqual(tags["SPEC-M3"], [["tag M", "Medium"], ["tag crit", "Came back"], ["tag", "False finding"]])
        self.assertEqual(tags["SPEC-M4"], [["tag M", "Medium"], ["tag crit", "Came back after a fix"]])
        self.assertEqual(tags["SPEC-M5"], [["tag M", "Medium"], ["tag", "Changed"]])  # partly: no tag
        self.assertEqual(tags["SPEC-L1"], [["tag L", "Low"]])                       # unmatched: no match tag
        it = self.item(page, "SPEC-M2")
        it.locator("summary").click()
        self.assertEqual(it.locator("dd a").get_attribute("href"), "#/issue/ISS-0001")
        self.assertEqual(it.locator("dt").all_inner_texts(), ["Area", "Kind", "Issue", "Checked by the manager"])
        facts = dict(zip(page.texts(".facts span"), page.texts(".facts b")))
        self.assertEqual((facts["points"], facts["new issues"], facts["came back"]), ("7", "0", "2"))
        # On the issue page the cards hide the issue link and show the review code.
        page.hash("#/issue/ISS-0001")
        self.assertEqual(page.texts("details.item > summary .n"),
                         ["R-0001 · SPEC-M1", "R-0002 · SPEC-M2", "R-0002 · SPEC-M3", "R-0002 · SPEC-M4",
                          "R-0002 · SPEC-M5"])
        page.page.locator("details.item summary").first.click()
        self.assertNotIn("Issue", page.page.locator("details.item[open] dt").all_inner_texts())

    def _show(self, page, pid):
        it = self.item(page, pid)
        it.locator("summary").click()
        it.locator("button", has_text="Show code at this commit").click()
        page.page.wait_for_function("b => b.textContent === 'Hide code'",
                                    arg=it.locator(".ref button").first.element_handle())
        page.settle()
        return it

    def test_page_code_viewer_fallback_notes(self):
        page = self.open("#/review/R-0002")
        exc = self._show(page, "SPEC-L1")
        note = exc.locator("p.note").inner_text()
        self.assertTrue(note.startswith("The commit could not be read from git here, so this is the copy of the "
                                        "lines saved at review time. Git said: "), note)
        self.assertEqual(exc.locator(".code td.ln").all_text_contents(), ["5 cited", "6 cited"])
        self.assertEqual(exc.locator(".code tr.hit td:not(.ln)").all_inner_texts(), ["five = 5", "six = 6"])
        self.assertIn("lines 5–6", exc.locator(".refline").first.inner_text())
        none = self._show(page, "SPEC-L2")
        self.assertTrue(none.locator("p.note").inner_text().startswith(
            "The commit could not be read from git and no copy of the lines was saved. "))
        self.assertEqual(none.locator(".code").count(), 0)
        self.assertIn("line 3", none.locator(".refline").first.inner_text())
        self.assertEqual(none.locator(".refline code").last.inner_text(),
                         f"git -C '.' show '{self.GONE}:src/a.py' | sed -n '3,3p'")
        self.assertEqual(none.locator("a", has_text="Open on the code host").count(), 0)  # no permalink template
        mis = self._show(page, "SPEC-L3")
        self.assertEqual(mis.locator("p.note").inner_text(),
                         "The saved copy of these lines differs from the file at this commit; the line numbers may "
                         "have been recorded wrong.")
        self.assertEqual(mis.locator(".code tr.hit td:not(.ln)").all_inner_texts(), ["two = 2"])

    def test_page_review_show_more(self):
        page = self.open("#/review/R-0003")
        note = ".tools ~ div p.note"
        self.assertEqual(page.text(note), "230 of 230 points")
        self.assertEqual(page.count("details.item"), 100)
        more = page.page.locator("button", has_text="Show more")
        self.assertEqual(more.inner_text(), "Show more (130 left)")
        more.click()
        page.settle()
        self.assertEqual(page.count("details.item"), 230)   # each click adds 200
        self.assertEqual(more.count(), 0)
        # Changing a filter starts again at 100.
        page.page.select_option("#fd", "tests")
        page.settle()
        self.assertEqual(page.text(note), "115 of 230 points")
        self.assertEqual(page.count("details.item"), 100)
        self.assertEqual(more.inner_text(), "Show more (15 left)")
        page.page.locator("input[type=search]").fill("number 1")  # 1, 10-19, 100-199 among the odd numbers
        page.settle()
        self.assertEqual(page.text(note), "56 of 230 points")
        self.assertEqual(more.count(), 0)


if __name__ == "__main__":
    unittest.main()
