"""Viewer issues, issue, plans, plan, trends and Explore views in a real browser (plan step 28; closes TEST-T223,
T224, T225, T228, T229, T230, T232, T233, T234).

Characterization tests: they pin what the page does today. Where today's behaviour is a known defect, the assertion
carries a '# characterization: ISS-xxxx' comment, so the step that fixes it changes that assertion on purpose.
Skipped cleanly when Playwright or a browser is missing (see dev/tests/browser.py).
"""
import json
import unittest

from dev.tests import browser, fixtures


class IssuesPlansExplore(browser.ViewerTestCase):
    """The demo plus: ISS-0003 raised from Low to Medium, ISS-0003 blocks ISS-0002, a plan with no phases, whole-system
    and per-repository coverage metrics, and 150 extra design goals (for Explore paging)."""

    @classmethod
    def build_ledger(cls, L):
        ids = fixtures.seed_demo(L)
        i2, i3 = ids["issues"]["ISS-0002"], ids["issues"]["ISS-0003"]
        L.update("issues", i3, severity="M", change_note="Raised by the owner")
        L.relation(i3, i2, "blocks", "Same function")
        L.plan("P-0002", "Empty plan")
        r1, r2 = ids["reviews"]["R-0001"], ids["reviews"]["R-0002"]
        for rid, v in ((r1, 40.0), (r2, 55.0)):
            L.metric(rid, "line_coverage", v, "measured", scope="all", unit="%")
        L.metric(r2, "line_coverage", 70.0, "measured", scope="all", unit="%", repository_id=ids["repository"])
        for n in range(150):
            L.design_goal(f"G-{100 + n}", f"Extra goal {n}.")
        return ids

    # ------------------------------------------------------------------ issues

    def _codes(self, page):
        return page.texts("#app tbody td:first-child")

    def test_page_issues_defaults_and_params(self):
        page = self.open("#/issues")
        self.assertEqual(page.js("document.querySelector('select[aria-label=Status]').value"), "active")
        self.assertEqual(self._codes(page), ["ISS-0002", "ISS-0003", "ISS-0004"])  # the verified ISS-0001 is hidden
        self.assertEqual(page.text("#app p.note"), "3 issues")
        for hash_, want in (("#/issues?status=&severity=H", ["ISS-0001"]), ("#/issues?status=verified", ["ISS-0001"]),
                            ("#/issues?q=notice", ["ISS-0002"]), ("#/issues?domain=security", ["ISS-0004"])):
            with self.subTest(hash_):
                page.hash(hash_)
                self.assertEqual(self._codes(page), want)
        page.hash("#/issues?q=notice")
        self.assertEqual(page.js("document.querySelector('#app input[type=search]').value"), "notice")
        # characterization: ISS-0249 hand-written plurals: one issue is shown as "1 issues"
        self.assertEqual(page.text("#app p.note"), "1 issues")
        # characterization: ISS-0234 the page reads ?domain=, so a link with the API's own name domain_code is ignored
        page.hash("#/issues?domain_code=security")
        self.assertEqual(page.js("document.querySelector('select[aria-label=Area]').value"), "")
        self.assertEqual(self._codes(page), ["ISS-0002", "ISS-0003", "ISS-0004"])
        page.after(lambda: page.page.locator("tbody tr", has_text="ISS-0004").click())
        self.assertEqual(page.location_hash(), "#/issue/ISS-0004")

    def test_page_issues_debounce(self):
        page = self.open("#/issues")
        calls = lambda: [u for u in page.requests if "/api/issues?" in u]  # noqa: E731
        self.assertEqual(len(calls()), 1)
        page.page.locator("#app input[type=search]").press_sequentially("sign", delay=30)
        self.assertEqual(len(calls()), 1)                    # nothing sent while typing
        page.page.wait_for_function("n => performance.getEntriesByType('resource').filter(e => "
                                    "e.name.includes('/api/issues?')).length > n", arg=1, timeout=2000)
        page.settle()
        self.assertEqual(len(calls()), 2)                    # one request 250 ms after the last key
        self.assertTrue(calls()[-1].endswith("status=active&q=sign"), calls()[-1])
        self.assertEqual(self._codes(page), ["ISS-0004"])
        # A select change loads at once.
        page.after(lambda: page.page.select_option("select[aria-label=Status]", ""))
        self.assertEqual(len(calls()), 3)
        self.assertEqual(self._codes(page), ["ISS-0001", "ISS-0004"])
        # A failed load shows the server's message in the list area; the filters stay.
        page.page.route("**/api/issues?**", lambda r: r.fulfill(status=500, content_type="application/json",
                                                                body=json.dumps({"error": "The ledger is busy."})))
        page.allow_console_errors = True
        page.after(lambda: page.page.select_option("select[aria-label=Severity]", "H"))
        self.assertEqual(page.texts("#app .status.err"), ["The ledger is busy."])
        self.assertEqual(page.count("#app .tools select"), 3)

    def test_page_issue_timeline_labels(self):
        page = self.open("#/issue/ISS-0001")
        self.assertEqual([t.split(" — ", 1)[1].split(" · ")[0] for t in page.texts(".timeline > div")],
                         ["Opened", "Raised as a new issue", "Open → Fixed", "Fixed → Fixed and confirmed"])
        self.assertIn(" · step 2 · ", page.texts(".timeline > div")[2])
        self.assertEqual([r.split("\t")[:2] for r in page.texts("h3:text('Related issues') + div tbody tr")],
                         [["Related to", "ISS-0004"]])   # the incoming side of related-to
        self.assertEqual(page.texts("h3:text('Tests that guard the fix') + div tbody td:first-child"),
                         ["tests/test_auth.py › test_code_uses_secrets"])
        page.hash("#/issue/ISS-0002")
        self.assertIn("Raised again (Still present)", [t.split(" — ", 1)[1].split(" · ")[0]
                                                       for t in page.texts(".timeline > div")])
        self.assertEqual([r.split("\t")[:2] for r in page.texts("h3:text('Related issues') + div tbody tr")],
                         [["Blocked by", "ISS-0003"]])
        page.hash("#/issue/ISS-0003")
        lines = [t.split(" — ", 1)[1] for t in page.texts(".timeline > div")]
        self.assertIn("Severity Low → Medium", [x.split(" · ")[0] for x in lines])
        self.assertEqual([r.split("\t")[:2] for r in page.texts("h3:text('Related issues') + div tbody tr")],
                         [["Blocks", "ISS-0002"]])
        self.assertEqual(page.texts("#app > div:nth-of-type(1) .tag"), ["Medium", "Open", "Spec conformance",
                                                                        "Spec mismatch"])

    # ------------------------------------------------------------------ plans

    def test_page_plans_progress(self):
        page = self.open("#/plans")
        row = page.page.locator("tbody tr", has_text="P-0001")
        cells = row.locator("td").all_inner_texts()
        self.assertEqual(cells[:4], ["P-0001", "Fix plan for R-0001", "R-0001", "In progress"])
        self.assertEqual(row.locator(".note").inner_text(), "1 of 3 done · 1 waiting on you")
        self.assertEqual(row.locator(".bar i").evaluate_all("is => is.map(i => i.getAttribute('style'))"),
                         ["width:33.33%", "width:0.00%", "width:33.33%"])
        empty = page.page.locator("tbody tr", has_text="P-0002")
        self.assertEqual(empty.locator(".note").inner_text(), "0 of 0 done")
        page.after(row.click)
        self.assertEqual(page.location_hash(), "#/plan/P-0001")

    def _rail(self, page):
        return page.page.locator("nav.rail button").evaluate_all(
            "bs => bs.map(b => [b.textContent, b.getAttribute('aria-current')])")

    def test_page_plan_phase_routing(self):
        page = self.open("#/plan/P-0001")
        self.assertEqual(self._rail(page), [["1Safety net0/1", "true"], ["2Fix first1/2", "false"]])
        self.assertEqual(page.text("#app main h2"), "Phase 1 · Safety net")
        self.assertEqual(page.contents("#app main h3"), ["CI"])     # steps grouped by section
        self.assertEqual(page.texts("main details.item > summary .n"), ["1"])
        facts = dict(zip(page.texts(".facts span"), page.texts(".facts b")))
        self.assertEqual(facts, {"steps": "3", "phases": "2", "points closed": "3"})
        page.hash("#/plan/P-0001/phase/2")
        self.assertEqual(page.text("#app main h2"), "Phase 2 · Fix first")
        self.assertEqual(page.texts("main details.item > summary .n"), ["2", "3"])
        # A rail click switches phase in place, rewrites the URL and clears the filters.
        page.page.locator("#app .tools input").fill("secrets")
        page.page.locator("nav.rail button").first.click()
        page.settle()
        self.assertEqual(page.location_hash(), "#/plan/P-0001/phase/1")
        self.assertEqual(page.js("document.querySelector('#app .tools input').value"), "")
        self.assertEqual(page.text("#app main h2"), "Phase 1 · Safety net")
        # Step tags and body.
        page.hash("#/plan/P-0001/phase/2")
        tags = page.page.locator("main details.item", has_text="Make the notice period").locator(".tags .tag")
        self.assertEqual(tags.all_inner_texts(), ["Waiting on you", "Your decision", "Changes behaviour"])

    def test_page_plan_filters(self):
        page = self.open("#/plan/P-0001")
        cases = [("st-done", ["2"]), ("st-doing", []), ("st-waiting-owner", ["3"]), ("st-not-started", ["1"]),
                 ("any", ["3"]), ("decision", ["3"]), ("confirm", []), ("review", []), ("production", []),
                 ("behavior", ["3"]), ("", None)]
        for value, want in cases:
            with self.subTest(value):
                page.page.select_option("#app select[aria-label='Filter steps']", value)
                page.settle()
                if want is None:
                    self.assertEqual(page.text("#app main h2"), "Phase 1 · Safety net")
                    continue
                self.assertEqual(page.texts("main details.item > summary .n"), want)
                n = len(want)
                self.assertEqual(page.text("#app main h2"),
                                 f"{n} matching {'step' if n == 1 else 'steps'} across all phases")
                self.assertEqual([c for _, c in self._rail(page)], ["false", "false"])  # no phase is current
                if not want:
                    self.assertEqual(page.texts("main p.empty"), ["No step matches."])

    def test_page_plan_search_point_id(self):
        page = self.open("#/plan/P-0001")
        search = page.page.locator("#app .tools input")
        for term, want in (("SEC-H1", ["2"]), ("spec-m1", ["3"]), ("ISS-0003", ["1"]), ("secrets", ["2"]),
                           ("CI fails", ["1"]), ("notice_days", ["3"]), ("no such step", [])):
            with self.subTest(term):
                search.fill(term)
                page.settle()
                self.assertEqual(page.texts("main details.item > summary .n"), want)
        search.fill("SEC-H1")
        page.settle()
        item = page.page.locator("main details.item").first
        item.locator("summary").click()
        self.assertEqual(item.locator(".ref .note").inner_text(), "Closes 1 point")
        self.assertEqual(item.locator(".ref .refline").first.locator(":scope > *").all_text_contents(),
                         ["R-0001 · SEC-H1", "High", "ISS-0001", "Sign-in codes come from a predictable random "
                                                                 "generator."])

    def test_page_plan_without_phases(self):
        page = self.open("#/plan/P-0002")
        # characterization: ISS-0096 a plan with no phases fails to draw, with a raw JavaScript error
        self.assertEqual(page.text("#app h1"), "Could not load this view")
        self.assertIn("Cannot read properties of undefined", page.text("#app .status.err"))

    def test_page_rail_and_table_list_focus(self):
        page = self.open("#/plan/P-0001")
        page.page.locator("#app select[aria-label='Filter steps']").focus()
        page.page.keyboard.press("Tab")
        got = page.js("""() => { const e = document.activeElement;
          return [e.closest('nav.rail') !== null, e.matches(':focus-visible'), getComputedStyle(e).outlineStyle]; }""")
        # characterization: ISS-0028 a keyboard-focused phase rail button shows no focus outline
        self.assertEqual(got, [True, True, "none"])
        page.hash("#/explore")
        page.page.locator(".tlist button").first.focus()
        page.page.keyboard.press("Tab")
        got = page.js("""() => { const e = document.activeElement;
          return [e.closest('.tlist') !== null, e.matches(':focus-visible'), getComputedStyle(e).outlineStyle]; }""")
        # characterization: ISS-0028 a keyboard-focused Explore table button shows no focus outline
        self.assertEqual(got, [True, True, "none"])
        # Other controls do get the outline.
        page.page.locator("#app textarea").focus()
        page.page.keyboard.press("Tab")
        self.assertEqual(page.js("[document.activeElement.textContent, getComputedStyle(document.activeElement)"
                                 ".outlineStyle]"), ["Run query", "solid"])

    # ------------------------------------------------------------------ trends

    def test_page_line_chart_edge_cases(self):
        page = self.open("#/trends")
        got = page.js("""() => {
          const pts = vs => vs.map((v, i) => ({value: v, review: "R-" + i}));
          const shape = el => ({empty: el.matches("p.empty") ? el.textContent : null,
            circles: [...el.querySelectorAll("circle")].map(c => [+c.getAttribute("cx"), +c.getAttribute("cy")]),
            labels: [...el.querySelectorAll("text")].map(t => t.textContent),
            line: el.querySelector("polyline") && el.querySelector("polyline").getAttribute("points")});
          return [shape(lineChart([], "%")), shape(lineChart(pts([5]), "%")), shape(lineChart(pts([3, 3]))),
                  shape(lineChart(pts([1, 2, 1.23456])))];
        }""")
        self.assertEqual(got[0], {"empty": "No values.", "circles": [], "labels": [], "line": None})
        self.assertEqual(got[1]["circles"], [[260, 130]])     # one point: centred, on the baseline
        self.assertEqual(got[1]["labels"], ["5 %", "R-0"])
        self.assertEqual(got[2]["circles"], [[30, 130], [490, 130]])  # flat series: span 0 does not divide by zero
        self.assertEqual(got[2]["line"], "30,130 490,130")
        self.assertEqual([c[0] for c in got[3]["circles"]], [30, 260, 490])
        self.assertEqual([c[1] for c in got[3]["circles"]][:2], [130, 30])
        self.assertEqual(got[3]["labels"][::2], ["1", "2", "1.23"])

    def test_page_trends_grouping(self):
        page = self.open("#/trends")
        cards = page.page.locator("#app .grid2 .card")
        got = cards.evaluate_all("cs => cs.map(c => [c.querySelector('b').textContent, "
                                 "c.querySelector('.note').textContent, c.querySelectorAll('circle').length])")
        self.assertEqual(sorted(got), [["Line coverage", "Whole system", 2], ["Line coverage", "demo", 1],
                                       ["Mutation score", "src", 2]])
        self.assertEqual(page.texts("h3:text('Points per review') + div tbody td:first-child"), ["R-0001", "R-0002"])
        self.assertEqual(page.texts("h3:text('Issues that keep coming back') + div tbody td:first-child"),
                         ["ISS-0002"])
        hot = page.page.locator("h3:text('Files that keep producing issues') + div tbody tr", has_text="src/auth.py")
        page.after(hot.click)
        self.assertEqual(page.location_hash(), "#/issues?q=src%2Fauth.py")
        self.assertEqual(page.js("document.querySelector('#app input[type=search]').value"), "src/auth.py")
        # characterization: ISS-0258 the hotspot link keeps the default Still to fix filter, hiding the fixed ISS-0001
        self.assertEqual(self._codes(page), ["ISS-0004"])

    # ------------------------------------------------------------------ explore

    def test_page_explore_paging(self):
        page = self.open("#/explore")
        self.assertEqual(page.texts("#app .split > div:last-child p.empty"), ["Choose a table or view on the left."])
        self.assertEqual(page.contents(".tlist h3"), ["Saved views", "Tables"])
        page.after(lambda: page.page.locator(".tlist button[data-n=design_goals]").click())
        self.assertEqual(page.location_hash(), "#/explore/design_goals")
        self.assertEqual(page.js("document.querySelector('.tlist button[aria-current=true]').dataset.n"),
                         "design_goals")
        note = "#app .split > div:last-child > p.note"
        self.assertEqual(page.text(note), "152 rows · showing 1–100")
        self.assertEqual(page.count("#app .split .grid tbody tr"), 100)
        tools = "#app .split > div:last-child .tools button"
        self.assertEqual(page.texts(tools), ["Next", "Query this"])
        page.after(lambda: page.page.locator(tools, has_text="Next").click())
        self.assertEqual(page.text(note), "152 rows · showing 101–152")
        self.assertEqual(page.count("#app .split .grid tbody tr"), 52)
        self.assertEqual(page.texts(tools), ["Previous", "Query this"])
        page.after(lambda: page.page.locator(tools, has_text="Previous").click())
        self.assertEqual(page.text(note), "152 rows · showing 1–100")
        page.page.locator(tools, has_text="Query this").click()
        self.assertEqual(page.js("[document.activeElement.getAttribute('aria-label'), document.activeElement.value]"),
                         ["SQL query", "SELECT * FROM design_goals LIMIT 50"])
        # A small table has no paging; an unknown table in the URL shows the prompt.
        page.hash("#/explore/plans")
        self.assertEqual(page.text(note), "2 rows")
        self.assertEqual(page.texts(tools), ["Query this"])
        page.hash("#/explore/no_such_table")
        self.assertEqual(page.texts("#app .split > div:last-child p.empty"), ["Choose a table or view on the left."])

    def test_page_explore_sql_refused(self):
        page = self.open("#/explore")
        page.allow_console_errors = True  # the 400 answer is logged by Chrome
        before = self.ledger.count("issues")
        for sql, msg in (("DELETE FROM issues", "The query did not run: not authorized"),
                         ("UPDATE issues SET title='x'", "The query did not run: not authorized"),
                         ("SELECT 1; SELECT 2", "The query did not run: You can only execute one statement at a time."),
                         ("", "Write a query first.")):
            with self.subTest(sql):
                page.page.locator("#app textarea").fill(sql)
                page.after(lambda: page.page.locator("button", has_text="Run query").click())
                self.assertEqual(page.texts("#app .card .status.err"), [msg])
        self.assertEqual(self.ledger.count("issues"), before)

    def test_page_explore_ctrl_enter(self):
        page = self.open("#/explore")
        sql = page.page.locator("#app textarea")
        sql.fill("SELECT code, severity FROM issues ORDER BY code")
        page.after(lambda: sql.press("Control+Enter"))
        out = "#app .card > div:last-child"
        self.assertEqual(page.text(out + " p.note"), "4 rows")
        self.assertEqual(page.contents(out + " thead th"), ["code", "severity"])
        self.assertEqual(page.texts(out + " tbody td:first-child"), ["ISS-0001", "ISS-0002", "ISS-0003", "ISS-0004"])
        # Plain Enter only adds a new line.
        sql.press("Enter")
        page.settle()
        self.assertEqual(len([u for u in page.requests if u.endswith("/api/sql")]), 1)
        sql.fill("SELECT 1 AS one")
        page.after(lambda: sql.press("Control+Enter"))
        # characterization: ISS-0249 hand-written plurals: one row is shown as "1 rows"
        self.assertEqual(page.text(out + " p.note"), "1 rows")
        sql.fill("WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c LIMIT 1500) SELECT n FROM c")
        page.after(lambda: sql.press("Control+Enter"))
        self.assertEqual(page.text(out + " p.note"), "1000 rows (first 1,000 shown)")
        self.assertEqual(page.count(out + " tbody tr"), 1000)


if __name__ == "__main__":
    unittest.main()
