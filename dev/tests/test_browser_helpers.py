"""Viewer page helpers, overview, router and text-not-HTML rendering in a real browser (plan step 26; closes
TEST-T200, T201, T204, T206, T211, T213, T236 and SEC-T8).

Characterization tests: they pin what the page does today. Where today's behaviour is a known defect, the assertion
carries a '# characterization: ISS-xxxx' comment, so the step that fixes it changes that assertion on purpose.
Skipped cleanly when Playwright or a browser is missing (see dev/tests/browser.py).
"""
import unittest

from dev.tests import browser, fixtures

XSS = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'


class Helpers(browser.ViewerTestCase):
    """Page globals (h, put, add, kv, copyBtn, progressBar) called directly in the loaded page."""

    def test_page_h_text_not_html(self):
        page = self.open("#/")
        got = page.js("""() => {
          let clicks = 0;
          const e = h("div", {onclick: () => clicks++, class: "a b", hidden: true, title: null, "data-x": false,
                              "data-n": 0}, "<b>bold</b>", null, false, 0, "", ["x", ["y"]], h("i", null, "z"));
          e.click(); e.dispatchEvent(new Event("click"));
          return {clicks, cls: e.className, hidden: e.getAttribute("hidden"), title: e.hasAttribute("title"),
                  dataX: e.hasAttribute("data-x"), dataN: e.getAttribute("data-n"), html: e.innerHTML,
                  nodes: e.childNodes.length, b: e.querySelectorAll("b").length,
                  onclickAttr: e.hasAttribute("onclick")};
        }""")
        self.assertEqual(got["clicks"], 2)               # on* becomes a listener, not an attribute
        self.assertFalse(got["onclickAttr"])
        self.assertEqual(got["cls"], "a b")
        self.assertEqual(got["hidden"], "")               # true -> empty attribute
        self.assertFalse(got["title"])                    # null skipped
        self.assertFalse(got["dataX"])                    # false skipped
        self.assertEqual(got["dataN"], "0")               # 0 is kept
        self.assertEqual(got["b"], 0)                     # strings are text, never HTML
        # "" and 0 children are kept by h (only null and false are dropped); nested arrays are flattened.
        self.assertEqual(got["html"], "&lt;b&gt;bold&lt;/b&gt;0xy<i>z</i>")
        self.assertEqual(got["nodes"], 6)

    def test_page_put_add_filter(self):
        page = self.open("#/")
        got = page.js("""() => {
          const el = h("div", null, "old");
          put(el, null, false, "", "a", 0, [h("b", null, "B"), [undefined, "c"]]);
          const afterPut = el.innerHTML, putNodes = el.childNodes.length;
          add(el, null, false, "", "<i>", [0]);
          return {afterPut, putNodes, afterAdd: el.innerHTML, ret: put(el) === el, cleared: el.childNodes.length};
        }""")
        self.assertEqual(got["afterPut"], "a0<b>B</b>c")   # null, false, "" and undefined dropped; old content replaced
        self.assertEqual(got["putNodes"], 4)
        self.assertEqual(got["afterAdd"], "a0<b>B</b>c&lt;i&gt;0")
        self.assertTrue(got["ret"])
        self.assertEqual(got["cleared"], 0)

    def test_page_kv_drops_empty(self):
        page = self.open("#/")
        got = page.js("""() => {
          const d = kv([["A", null], ["B", ""], ["C", []], ["D", "v"], ["E", 0], ["F", undefined], ["G", ["x"]],
                        ["H", h("a", {href: "#/"}, "link")]]);
          return {tag: d.tagName, cls: d.className, dt: [...d.querySelectorAll("dt")].map(x => x.textContent),
                  dd: [...d.querySelectorAll("dd")].map(x => x.innerHTML)};
        }""")
        self.assertEqual((got["tag"], got["cls"]), ("DL", "kv"))
        self.assertEqual(got["dt"], ["D", "E", "G", "H"])  # null, "", [] and undefined dropped; 0 kept
        self.assertEqual(got["dd"], ["v", "0", "x", '<a href="#/">link</a>'])

    def test_page_copy_fallback(self):
        page = self.open("#/", permissions=["clipboard-read", "clipboard-write"])
        got = page.js("""async () => {
          const b = copyBtn("git show abc:src/a.py", "Copy git command");
          document.getElementById("app").append(b);
          const label = b.textContent; b.click();
          for (let i = 0; i < 50 && b.textContent === label; i++) await new Promise(r => setTimeout(r, 20));
          return {label, after: b.textContent, clip: await navigator.clipboard.readText(), type: b.type, cls: b.className};
        }""")
        self.assertEqual(got, {"label": "Copy git command", "after": "Copied", "clip": "git show abc:src/a.py",
                               "type": "button", "cls": "btn ghost"})
        # Clipboard refused (permission denied or not a secure context): the button says to copy by hand.
        denied = page.js("""async () => {
          Object.defineProperty(navigator, "clipboard", {configurable: true,
            value: {writeText: () => Promise.reject(new DOMException("denied", "NotAllowedError"))}});
          const b = copyBtn("x"); document.getElementById("app").append(b); b.click();
          for (let i = 0; i < 50 && b.textContent === "Copy"; i++) await new Promise(r => setTimeout(r, 20));
          return b.textContent;
        }""")
        self.assertEqual(denied, "Select and copy by hand")

    def test_page_progress_bar_widths(self):
        page = self.open("#/")
        got = page.js("""() => [[0,0,0,0],[1,1,1,4],[4,0,0,4],[1,0,2,3]].map(a => {
          const b = progressBar(...a);
          return {widths: [...b.querySelectorAll(".bar i")].map(i => i.getAttribute("style")),
                  label: b.querySelector(".bar").getAttribute("aria-label"), note: b.querySelector(".note").textContent};
        })""")
        self.assertEqual(got[0], {"widths": ["width:0"] * 3, "label": "0 of 0 done", "note": "0 of 0 done"})
        self.assertEqual(got[1]["widths"], ["width:25.00%"] * 3)
        self.assertEqual(got[1]["note"], "1 of 4 done · 1 in progress · 1 waiting on you")
        self.assertEqual(got[2]["widths"], ["width:100.00%", "width:0.00%", "width:0.00%"])
        self.assertEqual(got[2]["note"], "4 of 4 done")
        self.assertEqual(got[3]["widths"], ["width:33.33%", "width:0.00%", "width:66.67%"])
        self.assertEqual(got[3]["note"], "1 of 3 done · 2 waiting on you")

    def test_page_home_facts(self):
        page = self.open("#/")
        facts = dict(zip(page.texts(".facts span"), page.texts(".facts b")))
        self.assertEqual(facts, {"waiting on you": "2", "open issues": "3", "open and high": "0",
                                 "came back or seen again": "1"})
        link = page.page.locator("p.status a")
        self.assertEqual(link.inner_text(), "2 things need your input or approval →")
        self.assertEqual(link.get_attribute("href"), "#/inbox")
        self.assertEqual(page.texts(".card h2"), ["Latest reviews", "Fix plans", "Ledger checks"])
        self.assertEqual(page.texts(".card:has(h2:text('Latest reviews')) tbody td:first-child"), ["R-0002", "R-0001"])
        self.assertEqual(page.text(".card:has(h2:text('Fix plans')) a"), "P-0001 · Fix plan for R-0001")
        self.assertEqual(page.count(".status.err"), 0)          # every check is 0: no failing-checks line
        # A check row with n = 0 does not navigate.
        page.after(page.page.locator(".card:has(h2:text('Ledger checks')) tbody tr").first.click, timeout=300)
        self.assertEqual(page.location_hash(), "#/")
        # A review row does.
        page.after(page.page.locator(".card:has(h2:text('Latest reviews')) tbody tr").first.click)
        self.assertEqual(page.location_hash(), "#/review/R-0002")
        self.assertEqual(page.text("#app h1"), "R-0002 · Comprehensive review R-0002")

    def test_page_home_failing_checks_link(self):
        # One unmatched finding makes a check fail: the line appears and that row links to Explore.
        rid = self.ids["reviews"]["R-0002"]
        fid = self.ledger.finding(rid, "SPEC-L9", severity="L", summary="Unmatched probe.")
        self.ledger.commit()
        try:
            page = self.open("#/")
            # two: the finding is unmatched, and (step 76, schema 7) a finding with no location is untraceable
            self.assertEqual(page.texts(".status.err"), ["2 checks need attention."])
            row = page.page.locator(".card:has(h2:text('Ledger checks')) tbody tr", has_text="not matched")
            self.assertEqual(row.locator("td").nth(1).inner_text(), "1")
            page.after(row.click)
            self.assertEqual(page.location_hash(), "#/explore/v_unmatched_findings")
            self.assertEqual(page.text("#app h2"), "v_unmatched_findings")
        finally:
            # findings are append-only (schema 3): lift the guard for this cleanup only, then put it back as it was
            con = self.ledger.con
            guard = con.execute("SELECT sql FROM sqlite_master WHERE name='tr_finding_no_delete'").fetchone()[0]
            con.execute("DROP TRIGGER tr_finding_no_delete")
            con.execute("DELETE FROM findings WHERE id=?", (fid,))
            con.execute(guard)
            self.ledger.commit()

    def test_page_router_tabs_and_unknown(self):
        page = self.open("#/")
        self.assertEqual(page.texts(".tabs a"), ["Overview", "Waiting on you2", "Reviews", "Design goals", "Issues", "Fix plans",
                                                 "Over time", "Explore"])
        cases = [("#/inbox", "Waiting on you", "Waiting on you"), ("#/history", "Waiting on you", "Questions and answers"),
                 ("#/reviews", "Reviews", "Reviews"), ("#/review/R-0001", "Reviews", "R-0001 · Comprehensive review R-0001"),
                 ("#/issues", "Issues", "Issues"), ("#/issue/ISS-0002", "Issues", "ISS-0002 · Notice period hardcoded"),
                 ("#/plans", "Fix plans", "Fix plans"), ("#/plan/P-0001", "Fix plans", "P-0001 · Fix plan for R-0001"),
                 ("#/trends", "Over time", "Over time"), ("#/explore", "Explore", "Explore the ledger"),
                 ("#/nope/1", "Overview", "proj"), ("#", "Overview", "proj"),
                 # Path parts are decoded: %2D is "-".
                 ("#/review/R%2D0002", "Reviews", "R-0002 · Comprehensive review R-0002")]
        for hash_, tab, h1 in cases:
            with self.subTest(hash_):
                page.hash(hash_)
                current = page.texts(".tabs a[aria-current=page]")
                self.assertEqual([c.rstrip("0123456789") for c in current], [tab])
                self.assertEqual(page.text("#app h1"), h1)
        # The query part goes to the view as URL parameters.
        page.hash("#/issues?q=notice&status=")
        self.assertEqual(page.js("document.querySelector('input[type=search]').value"), "notice")
        self.assertEqual(page.texts("#app tbody td:first-child"), ["ISS-0002"])
        self.assertEqual(page.js("document.title"), "proj · review ledger")
        self.assertEqual(page.js("document.getElementById('brand').firstChild.textContent"), "proj")

    def test_page_router_badge_refresh(self):
        # The tab badge counts the owner queue; the router reloads the overview on home and inbox only.
        page = self.open("#/reviews")
        self.assertEqual(page.text(".tabs .badge"), "2")
        gate = self.ids["gates"]["GATE-0002"]
        self.write("UPDATE gates SET status='withdrawn' WHERE id=?", (gate,))
        try:
            page.hash("#/issues")
            # characterization: ISS-0259 the badge is stale outside home and inbox (still 2 after a gate went away)
            self.assertEqual(page.text(".tabs .badge"), "2")
            page.hash("#/inbox")
            self.assertEqual(page.text(".tabs .badge"), "1")
        finally:
            self.write("UPDATE gates SET status='open' WHERE id=?", (gate,))

    def test_page_badge_counts_answered_gates(self):
        gate = self.ids["gates"]["GATE-0002"]
        self.write("UPDATE gates SET status='answered', answer_value='approve' WHERE id=?", (gate,))
        try:
            page = self.open("#/inbox")
            # characterization: ISS-0107 an answered gate (no longer waiting on the owner) still counts in the badge
            self.assertEqual(page.text(".tabs .badge"), "2")
            self.assertEqual(page.count(".card.gate"), 2)
        finally:
            self.write("UPDATE gates SET status='open', answer_value=NULL, answered_at=NULL WHERE id=?", (gate,))


class HomeWithStepInProgress(browser.ViewerTestCase):
    """The demo with plan step 1 in progress (own ledger)."""

    @classmethod
    def build_ledger(cls, ledger):
        ids = fixtures.seed_demo(ledger)
        ledger.update("steps", ids["steps"][1], status="doing")
        return ids

    def test_page_home_bar_ignores_in_progress(self):
        page = self.open("#/")
        bar = page.page.locator(".card:has(h2:text('Fix plans'))")
        # characterization: ISS-0237 the overview always passes doing=0, so the in-progress step is not shown
        self.assertEqual(bar.locator(".note").inner_text(), "1 of 3 done · 1 waiting on you")
        self.assertEqual(bar.locator(".bar i.g").get_attribute("style"), "width:0.00%")
        # The plans list does show it.
        page.hash("#/plans")
        self.assertEqual(page.text("#app td .note"), "1 of 3 done · 1 in progress · 1 waiting on you")


class EmptyLedger(browser.ViewerTestCase):
    """A ledger with nothing in it (own ledger)."""

    @classmethod
    def build_ledger(cls, ledger):
        return {}

    def test_page_home_empty(self):
        page = self.open("#/")
        facts = dict(zip(page.texts(".facts span"), page.texts(".facts b")))
        self.assertEqual(facts, {"waiting on you": "0", "open issues": "0", "open and high": "0",
                                 "came back or seen again": "0"})
        self.assertEqual(page.count("p.status"), 0)               # no queue link when the queue is 0
        self.assertEqual(page.count(".tabs .badge"), 0)
        self.assertEqual(page.texts("p.empty"), ["Nothing here yet.", "No fix plan yet."])

    def test_page_empty_views(self):
        page = self.open("#/inbox")
        self.assertEqual(page.texts("p.empty"), ["Nothing is waiting on you right now."])
        self.assertEqual(page.count("#app h3"), 0)                 # no answered or other section
        for hash_, want in (("#/reviews", ["Nothing here yet."]), ("#/plans", ["Nothing here yet."]),
                            ("#/history", ["Nothing here yet."])):
            with self.subTest(hash_):
                page.hash(hash_)
                self.assertEqual(page.texts("p.empty"), want)
        page.hash("#/trends")
        self.assertIn("No measurements recorded yet.", page.texts("p.empty"))


class TextNotHtml(browser.ViewerTestCase):
    """Markup in ledger text is shown as text everywhere (own ledger full of <img onerror> payloads)."""

    @classmethod
    def build_ledger(cls, L):
        repo = L.repo
        repo.write("src/x.py", f"a = 1\nb = '{XSS}'\nc = 3\n")
        sha = repo.commit("payload")
        L.x("PRAGMA ignore_check_constraints = ON")  # schema 2 refuses a javascript: URL; plant one anyway
        rid = L.review("R-0001", commit_sha=sha, title=XSS, summary=XSS, verdict=XSS, request_text=XSS,
                       summary_doc_url="javascript:window.__xss=100")
        L.x("PRAGMA ignore_check_constraints = OFF")
        L.review_domain(rid, "security", reason=XSS)
        fid = L.finding(rid, "SEC-H1", severity="H", domain_code="security", summary=XSS, detail=XSS,
                        before_state=XSS, recommendation=XSS, evidence=XSS)
        L.location(fid, "src/x.py", 2, 2, symbol=XSS, excerpt=XSS)
        iid = L.issue("ISS-0001", first_seen_review_id=rid, title=XSS, domain_code="security", before_state=XSS)
        L.match(fid, iid, "new")
        pid = L.plan("P-0001", XSS, reviews=(rid,))
        ph = L.phase(pid, 1, XSS, XSS)
        sid = L.step(pid, ph, 1, XSS, XSS, XSS, findings=(fid,), section=XSS, owner_need="decision",
                     owner_question=XSS)
        L.gate("GATE-0001", "decision", XSS, XSS, context=XSS, review_id=rid, plan_id=pid, step_id=sid, issue_id=iid,
               options=[{"value": "a", "label": XSS, "description": XSS}], allow_text=1)
        L.metric(rid, XSS, 1.0, scope=XSS, unit=XSS)
        return {"review": rid}

    def test_viewer_renders_text_not_html(self):
        page = self.open("#/")
        for hash_ in ("#/", "#/inbox", "#/history", "#/reviews", "#/review/R-0001", "#/issues", "#/issue/ISS-0001",
                      "#/plans", "#/plan/P-0001", "#/trends", "#/explore/findings"):
            with self.subTest(hash_):
                page.hash(hash_)
                if hash_ in ("#/review/R-0001", "#/issue/ISS-0001"):
                    page.page.locator("details.item summary").first.click()
                    page.page.locator("button", has_text="Show code at this commit").first.click()
                    page.page.wait_for_selector(".code tr.hit")
                    page.settle()
                if hash_ == "#/plan/P-0001":
                    page.page.locator("details.item summary").first.click()
                self.assertEqual(page.count("img"), 0)
                self.assertIn(XSS, page.text("#app"))
        page.settle(quiet_ms=150)
        self.assertIsNone(page.js("window.__xss"))
        self.assertEqual(page.requests and [u for u in page.requests if u.endswith("/x")], [])
        # ISS-0080 fixed (step 44): a non-http(s) ledger URL is shown as text, never as a link
        page.hash("#/review/R-0001")
        self.assertEqual(page.page.locator("a", has_text="Open the review document").count(), 0)
        self.assertIn("Open the review document: javascript:window.__xss=100", page.text("#app"))


if __name__ == "__main__":
    unittest.main()
