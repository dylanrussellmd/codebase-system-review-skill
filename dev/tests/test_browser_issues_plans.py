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
            L.design_goal(None, f"Extra goal {n}.")   # codes are sequential (schema 3): G-03, G-04 ...
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
        self.assertEqual(page.text("#app p.note"), "1 issue")  # ISS-0249 fixed
        # ISS-0234 fixed: the API's own name, domain_code, filters by area
        page.hash("#/issues?domain_code=security")
        self.assertEqual(page.js("document.querySelector('select[aria-label=Area]').value"), "security")
        self.assertEqual(self._codes(page), ["ISS-0004"])
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
        self.assertIn("status=active&q=sign&", calls()[-1])
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
                         # schema 3 logs text edits and every review check too
                         # step 184: event names come from the ledger's labels; a review's check says what it found
                         ["Opened", "What it does now: text changed", "Raised as a new issue",
                          "What it does after the fix: text changed", "Open → Fixed", "Fixed → Fixed and confirmed",
                          "Looked for by a review: fixed"])
        self.assertIn(" · step 2 · ", page.texts(".timeline > div")[4])
        self.assertEqual([r.split("\t")[:2] for r in page.texts("h2:text('Related issues') + div tbody tr")],
                         [["Related to", "ISS-0004"]])   # the incoming side of related-to
        self.assertEqual(page.texts("h2:text('Tests that guard the fix') + div tbody td:first-child"),
                         ["tests/test_auth.py › test_code_uses_secrets"])
        page.hash("#/issue/ISS-0002")
        self.assertIn("Raised again (Still present)", [t.split(" — ", 1)[1].split(" · ")[0]
                                                       for t in page.texts(".timeline > div")])
        self.assertEqual([r.split("\t")[:2] for r in page.texts("h2:text('Related issues') + div tbody tr")],
                         [["Blocked by", "ISS-0003"]])
        page.hash("#/issue/ISS-0003")
        lines = [t.split(" — ", 1)[1] for t in page.texts(".timeline > div")]
        # (schema 3: the event no longer carries a stale review from an earlier check, so the note follows directly)
        self.assertIn("Severity Low → Medium", [x.split(" · ")[0].split("\n")[0] for x in lines])
        self.assertEqual([r.split("\t")[:2] for r in page.texts("h2:text('Related issues') + div tbody tr")],
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
        self.assertEqual(self._rail(page), [["1Safety netstep 10/1", "true"], ["2Fix firststeps 2–31/2", "false"]])   # step 191: the steps of each phase
        self.assertEqual(page.text("#app .steps h2"), "Phase 1: Safety net")
        self.assertEqual(page.contents("#app .steps h3"), ["CI"])     # steps grouped by section
        self.assertEqual(page.texts(".steps details.item > summary .n"), ["1"])
        facts = dict(zip(page.texts(".facts span"), page.texts(".facts b")))
        self.assertEqual(facts, {"steps": "3", "phases": "2", "points closed": "3", "step needs you": "1"})
        page.hash("#/plan/P-0001/phase/2")
        self.assertEqual(page.text("#app .steps h2"), "Phase 2: Fix first")
        self.assertEqual(page.texts(".steps details.item > summary .n"), ["2", "3"])
        # A rail click switches phase in place, rewrites the URL and clears the filters.
        page.page.locator("#app .tools input").fill("secrets")
        page.page.locator("nav.rail button").first.click()
        page.settle()
        self.assertEqual(page.location_hash(), "#/plan/P-0001/phase/1")
        self.assertEqual(page.js("document.querySelector('#app .tools input').value"), "")
        self.assertEqual(page.text("#app .steps h2"), "Phase 1: Safety net")
        # Step tags and body.
        page.hash("#/plan/P-0001/phase/2")
        tags = page.page.locator(".steps details.item", has_text="Make the notice period").locator(".tags .tag")
        self.assertEqual(tags.all_inner_texts(), ["Waiting on you", "Your decision", "Changes behaviour", "1 point"])

    def test_page_plan_filters(self):
        page = self.open("#/plan/P-0001")
        # the choices are the ledger's labels: every step status, then every owner need
        self.assertEqual(page.js("[...document.querySelector(\"#app select[aria-label='Filter steps']\").options]"
                                 ".map(o => o.value + '=' + o.text)"),
                         ["=All steps", "st-not-started=Not started", "st-doing=In progress",
                          "st-waiting-owner=Waiting on you", "st-done=Done", "st-dropped=Dropped", "any=Needs you",
                          "decision=Needs your decision", "confirm=Needs your confirmation", "review=Needs your review",
                          "production=Needs production action", "behavior=Changes behaviour"])
        cases = [("st-done", ["2"]), ("st-doing", []), ("st-waiting-owner", ["3"]), ("st-not-started", ["1"]),
                 ("st-dropped", []),
                 ("any", ["3"]), ("decision", ["3"]), ("confirm", []), ("review", []), ("production", []),
                 ("behavior", ["3"]), ("", None)]
        for value, want in cases:
            with self.subTest(value):
                page.page.select_option("#app select[aria-label='Filter steps']", value)
                page.settle()
                if want is None:
                    self.assertEqual(page.text("#app .steps h2"), "Phase 1: Safety net")
                    continue
                self.assertEqual(page.texts(".steps details.item > summary .n"), want)
                n = len(want)
                self.assertEqual(page.text("#app .steps h2"),
                                 f"{n} matching {'step' if n == 1 else 'steps'} across all phases")
                self.assertEqual([c for _, c in self._rail(page)], ["false", "false"])  # no phase is current
                if not want:
                    self.assertEqual(page.texts(".steps p.empty"), ["No step matches."])

    def test_page_plan_search_point_id(self):
        page = self.open("#/plan/P-0001")
        search = page.page.locator("#app .tools input")
        for term, want in (("SEC-H1", ["2"]), ("spec-m1", ["3"]), ("ISS-0003", ["1"]), ("secrets", ["2"]),
                           ("CI fails", ["1"]), ("notice_days", ["3"]), ("no such step", [])):
            with self.subTest(term):
                search.fill(term)
                page.settle()
                self.assertEqual(page.texts(".steps details.item > summary .n"), want)
        search.fill("SEC-H1")
        page.settle()
        item = page.page.locator(".steps details.item").first
        item.locator("summary").click()
        page.settle()   # the points a step closes arrive when it is opened (step 162)
        self.assertEqual(item.locator(".ref .note").inner_text(), "Closes 1 point")
        self.assertEqual(item.locator(".ref .refline").first.locator(":scope > *").all_text_contents(),
                         ["R-0001 · SEC-H1", "High", "ISS-0001", "Sign-in codes come from a predictable random "
                                                                 "generator."])

    def test_plan_without_phases_renders(self):
        page = self.open("#/plan/P-0002")                 # ISS-0096: this used to fail with a raw JavaScript error
        self.assertEqual(page.text("#app h1"), "P-0002 · Empty plan")
        self.assertEqual(page.texts("#app p.empty"), ["This plan has no phases yet."])
        self.assertEqual(page.count(".status.err"), 0)
        self.assertEqual(page.count("nav.rail"), 0)
        self.assertEqual(page.count("#app .tools"), 0)    # nothing to search or filter
        facts = dict(zip(page.texts(".facts span"), page.texts(".facts b")))
        self.assertEqual(facts, {"steps": "0", "phases": "0", "points closed": "0", "steps need you": "0"})

    def test_page_rail_and_table_list_focus(self):
        page = self.open("#/plan/P-0001")
        page.page.locator("#app select[aria-label='Filter steps']").focus()
        page.page.keyboard.press("Tab")
        got = page.js("""() => { const e = document.activeElement;
          return [e.closest('nav.rail') !== null, e.matches(':focus-visible'), getComputedStyle(e).outlineStyle]; }""")
        self.assertEqual(got, [True, True, "solid"])     # ISS-0028: a phase button shows the focus outline
        page.hash("#/explore")
        page.page.locator(".tlist button").first.focus()
        page.page.keyboard.press("Tab")
        got = page.js("""() => { const e = document.activeElement;
          return [e.closest('.tlist') !== null, e.matches(':focus-visible'), getComputedStyle(e).outlineStyle]; }""")
        self.assertEqual(got, [True, True, "solid"])     # and so does a table button in Explore
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
        # step 168: points, missing tests and confirmed points are charted per review, above the measurements
        per = page.page.locator("h2:text('Points per review') ~ .grid2").first.locator(".card")
        self.assertEqual(per.evaluate_all("cs => cs.map(c => [c.querySelector('b').textContent, "
                                          "c.querySelectorAll('circle').length])"),
                         [["Points", 2], ["Missing tests", 2], ["Confirmed", 2]])
        self.assertEqual(page.contents("h2:text('Points per review') + div thead th")[6:8], ["Missing tests", "Confirmed"])
        cards = page.page.locator("h2:text('Measurements') + .grid2 .card")
        got = cards.evaluate_all("cs => cs.map(c => [c.querySelector('b').textContent, "
                                 "c.querySelector('.note').textContent, c.querySelectorAll('circle').length])")
        # plan step 94: the title is the metric_defs label and the note says which way is better
        self.assertEqual(sorted(got), [["Line coverage", "Whole system · higher is better", 2],
                                       ["Line coverage", "demo · higher is better", 1],
                                       ["Mutation score", "src · higher is better", 2]])
        self.assertEqual(page.texts("h2:text('Points per review') + div tbody td:first-child"), ["R-0001", "R-0002"])
        self.assertEqual(page.texts("h2:text('Issues that keep coming back') + div tbody td:first-child"),
                         ["ISS-0002"])
        hot = page.page.locator("h2:text('Files that keep producing issues') + div tbody tr", has_text="src/auth.py")
        page.after(hot.click)
        self.assertEqual(page.location_hash(), "#/issues?status=all&q=src%2Fauth.py")  # ISS-0235 fixed
        self.assertEqual(page.js("document.querySelector('#app input[type=search]').value"), "src/auth.py")
        # ISS-0258 fixed: the hotspot link asks for every status, so the fixed ISS-0001 is listed
        self.assertEqual(self._codes(page), ["ISS-0001", "ISS-0004"])

    # ------------------------------------------------------------------ explore

    def test_page_explore_paging(self):
        page = self.open("#/explore")
        self.assertEqual(page.texts("#app .split > div:last-child p.empty"), ["Choose a table or view on the left."])
        self.assertEqual(page.contents(".tlist h2"), ["Saved views", "Tables"])
        page.after(lambda: page.page.locator(".tlist button[data-n=design_goals]").click())
        self.assertEqual(page.location_hash(), "#/explore/design_goals")
        self.assertEqual(page.js("document.querySelector('.tlist button[aria-current=true]').dataset.n"),
                         "design_goals")
        note = "#app .split > div:last-child > p.note:nth-of-type(3)"
        # step 189: the name in words, what it holds, and the stored name
        self.assertEqual(page.text("#app .split h2:not(.sec)"), "Design goals")
        self.assertEqual(page.text("#app .split > div:last-child > p.lede"),
                         self.ledger.module.SCHEMA_NOTES["design_goals"])
        self.assertEqual(page.text("#app .split > div:last-child > p.note"), "Stored as design_goals")
        self.assertEqual(page.text(note), "152 rows · rows 1–100")
        self.assertEqual(page.count("#app .split .grid tbody tr"), 100)
        tools = "#app .split > div:last-child .tools button"
        self.assertEqual(page.texts(tools), ["Next", "Query this"])
        page.after(lambda: page.page.locator(tools, has_text="Next").click())
        self.assertEqual(page.text(note), "152 rows · rows 101–152")
        self.assertEqual(page.location_hash(), "#/explore/design_goals?offset=100")
        page.page.reload()                       # the position survives a reload (step 189)
        page.settle()
        self.assertEqual(page.text(note), "152 rows · rows 101–152")
        self.assertEqual(page.count("#app .split .grid tbody tr"), 52)
        self.assertEqual(page.texts(tools), ["Previous", "Query this"])
        page.after(lambda: page.page.locator(tools, has_text="Previous").click())
        self.assertEqual(page.text(note), "152 rows · rows 1–100")
        self.assertEqual(page.location_hash(), "#/explore/design_goals")
        page.page.locator(tools, has_text="Query this").click()
        self.assertEqual(page.js("[document.activeElement.getAttribute('aria-label'), document.activeElement.value]"),
                         ["SQL query", "SELECT * FROM design_goals LIMIT 50"])
        # A small table has no paging; an unknown table in the URL shows the prompt.
        page.hash("#/explore/plans")
        self.assertEqual(page.text(note), "2 rows")
        self.assertEqual(page.texts(tools), ["Query this"])
        # a view is not counted until asked (step 165), and says what it is (step 189)
        page.hash("#/explore/v_owner_queue")
        self.assertEqual(page.text("#app .split > div:last-child > p.lede"), "Everything waiting on the owner.")
        self.assertRegex(page.text(note), r"^rows 1–\d+$")
        self.assertEqual(page.texts(tools), ["Count the rows", "Query this"])
        page.after(lambda: page.page.locator(tools, has_text="Count the rows").click())
        self.assertRegex(page.text(note), r"^\d+ rows? · rows 1–\d+$")
        self.assertEqual(page.texts(tools), ["Query this"])
        self.assertEqual(page.js("document.querySelector('.tlist button[data-n=v_owner_queue]').textContent"),
                         "Owner queuev_owner_queue")
        self.assertEqual(page.js("document.querySelector('.tlist button[data-n=plans]').textContent"), "Plansplans2")
        page.hash("#/explore/v_code_refs")
        self.assertIn("The agent reads this view", page.text("#app .split > div:last-child > p.note"))
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
        self.assertEqual(page.text(out + " p.note"), "1 row")
        sql.fill("WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c LIMIT 1500) SELECT n FROM c")
        page.after(lambda: sql.press("Control+Enter"))
        self.assertEqual(page.text(out + " p.note"), "1000 rows (first 1,000 shown)")
        self.assertEqual(page.count(out + " tbody tr"), 1000)


class LargePlan(browser.ViewerTestCase):
    """A plan of one phase and 320 steps, every one matching the search 'step'."""

    @classmethod
    def build_ledger(cls, L):
        plan = L.plan("P-0001", "Large plan")
        phase = L.phase(plan, 1, "Everything")
        for n in range(1, 321):
            L.step(plan, phase, n, f"Step number {n}")
        return {}

    def test_plan_filter_truncation_notice(self):
        page = self.open("#/plan/P-0001")
        self.assertEqual(page.count(".steps details.item"), 320)          # a phase shows all its steps
        search = page.page.locator("#app .tools input")
        search.fill("step number")
        page.settle()
        self.assertEqual(page.text("#app .steps h2"), "320 matching steps across all phases")
        self.assertEqual(page.count(".steps details.item"), 300)
        self.assertEqual(page.text("#app .steps p.note"), "Showing the first 300.")
        more = page.page.locator("#app .steps button")
        self.assertEqual(more.inner_text(), "Show 20 more")
        more.focus()
        page.page.keyboard.press("Enter")
        page.settle()
        self.assertEqual(page.count(".steps details.item"), 320)
        self.assertEqual(page.count("#app .steps p.note"), 0)
        self.assertEqual(more.count(), 0)
        self.assertEqual(page.js("document.activeElement.querySelector('.n').textContent"), "301")
        search.fill("step number 31")                                     # 31 and 310-319: under the limit, no notice
        page.settle()
        self.assertEqual(page.text("#app .steps h2"), "11 matching steps across all phases")
        self.assertEqual(page.count("#app .steps p.note"), 0)
        search.fill("step")                                               # a new search starts again at 300
        page.settle()
        self.assertEqual(page.count(".steps details.item"), 300)


if __name__ == "__main__":
    unittest.main()
