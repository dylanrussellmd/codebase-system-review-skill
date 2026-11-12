"""How the viewer page looks and is operated, in a real browser (plan P-0001 steps 178-182).

Step 181 (contrast, theme, cues): test_field_border_contrast, test_page_theme_cycle_persists,
test_theme_toggle_right_aligned, test_cited_line_marked, test_summary_has_chevron.
Step 178 (keyboard and focus): test_every_control_has_visible_focus, test_focus_after_actions,
test_focus_not_obscured, test_skip_link_first, test_page_is_not_one_live_region.
Step 179 (structure and names): test_heading_levels_do_not_skip, test_control_names_unique_per_view,
test_select_labels, test_no_empty_th, test_badge_accessible_name.
Step 180 (rows are links): test_page_table_keyboard_row, test_row_targets_are_links.
Step 182 (phone width): test_no_horizontal_overflow_390, test_all_tabs_visible_390, test_chart_has_data_table,
test_chart_label_size_390; HubLook runs the same checks on the page written by progress-page.
Skipped cleanly when Playwright or a browser is missing (see dev/tests/browser.py).
"""
import unittest

from dev.tests import browser, fixtures

# Contrast ratio (WCAG) of two computed colours, in the page.
CONTRAST = """([a, b]) => {
  const lum = c => { const [r, g, bl] = c.match(/[\\d.]+/g).slice(0, 3).map(v => { v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * r + 0.7152 * g + 0.0722 * bl; };
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); }"""

THROWING_STORAGE = """
for (const k of ["getItem", "setItem", "removeItem"]) Storage.prototype[k] = () => { throw new DOMException("denied", "SecurityError"); };
"""


class Look(browser.ViewerTestCase):
    """The demo, read only."""

    def test_field_border_contrast(self):
        for scheme in ("light", "dark"):
            page = self.open("#/issues", color_scheme=scheme)
            page.hash("#/inbox")
            page.hash("#/issues")
            got = page.js("""() => { const cs = (e, p) => getComputedStyle(e)[p];
              const f = document.querySelector("#app input.field"), s = document.querySelector("#app select.field");
              return {field: cs(f, "borderTopColor"), select: cs(s, "borderTopColor"), surface: cs(f, "backgroundColor"),
                      bg: cs(document.body, "backgroundColor")}; }""")
            page.hash("#/inbox")
            opt = page.js("""() => { const l = document.querySelector(".opts label"), r = l.querySelector("input");
              return [getComputedStyle(l).borderTopColor, getComputedStyle(r).accentColor,
                      getComputedStyle(document.documentElement).getPropertyValue("--accent").trim()]; }""")
            for name, colour in (("field", got["field"]), ("select", got["select"]), ("option card", opt[0])):
                for back in ("surface", "bg"):
                    with self.subTest(scheme=scheme, border=name, against=back):
                        self.assertGreaterEqual(page.js(CONTRAST, [colour, got[back]]), 3.0)
            # the radio buttons take the theme's accent colour
            want = page.js("c => { const e = document.createElement('i'); e.style.color = c; document.body.append(e);"
                           " const v = getComputedStyle(e).color; e.remove(); return v; }", opt[2])
            self.assertEqual(opt[1], want, scheme)

    def _theme(self, page):
        return page.js("[document.getElementById('theme').textContent, document.documentElement.dataset.theme,"
                       " document.getElementById('theme').getAttribute('aria-label')]")

    def test_page_theme_cycle_persists(self):
        page = self.open("#/", color_scheme="dark")
        self.assertEqual(self._theme(page), ["Theme: Automatic", "dark", "Theme: Automatic. Switch to Light."])
        btn = page.page.locator("#theme")
        btn.click()
        self.assertEqual(self._theme(page), ["Theme: Light", "light", "Theme: Light. Switch to Dark."])
        self.assertEqual(page.js("getComputedStyle(document.body).backgroundColor"), "rgb(247, 248, 246)")
        btn.click()
        self.assertEqual(self._theme(page), ["Theme: Dark", "dark", "Theme: Dark. Switch to Automatic."])
        page.page.reload()                                   # the choice is kept for the next visit
        page.settle()
        self.assertEqual(self._theme(page)[:2], ["Theme: Dark", "dark"])
        page.page.emulate_media(color_scheme="light")
        self.assertEqual(self._theme(page)[1], "dark")       # a chosen theme does not follow the system
        page.page.locator("#theme").click()
        self.assertEqual(self._theme(page)[:2], ["Theme: Automatic", "light"])
        page.page.emulate_media(color_scheme="dark")         # Automatic follows the system as it changes
        page.page.wait_for_function("t => document.documentElement.dataset.theme === t", arg="dark")
        self.assertEqual(self._theme(page)[:2], ["Theme: Automatic", "dark"])
        # a stored value that is not a theme is ignored
        page.js("localStorage.setItem('ledger-theme', 'blue')")
        page.page.reload()
        page.settle()
        self.assertEqual(self._theme(page)[:2], ["Theme: Automatic", "dark"])

    def test_page_theme_survives_blocked_storage(self):
        page = self.new_page()
        page.context.add_init_script(THROWING_STORAGE)
        page.goto("#/")
        self.assertEqual(self._theme(page)[:2], ["Theme: Automatic", "light"])
        page.page.locator("#theme").click()
        page.page.locator("#theme").click()
        self.assertEqual(self._theme(page)[:2], ["Theme: Dark", "dark"])
        self.assertEqual(page.text("#app h1"), "proj")

    def test_theme_toggle_right_aligned(self):
        for viewport in (browser.DESKTOP, browser.PHONE):
            page = self.open("#/", viewport=viewport)
            got = page.js("""() => { const b = document.getElementById("theme").getBoundingClientRect(),
              r = document.querySelector(".top .row").getBoundingClientRect(); return [b.right, r.right]; }""")
            self.assertLessEqual(abs(got[1] - got[0]), 8, viewport)

    def test_cited_line_marked(self):
        page = self.open("#/review/R-0001")
        item = page.page.locator("details.item", has_text="SEC-H1")
        item.locator("summary").click()
        item.locator(".refline button").first.click()
        item.locator(".code").wait_for()
        got = item.locator(".code tr").evaluate_all("""rs => rs.map(r => { const ln = r.querySelector("td.ln");
          return [r.className, getComputedStyle(ln).boxShadow !== "none",
                  [...ln.querySelectorAll(".visually-hidden")].map(e => e.textContent)]; })""")
        self.assertEqual(got[:3], [["hit", True, [" cited"]]] * 3)     # lines 1-3: a mark beside the number, and a word
        self.assertEqual(got[3:], [["", False, []]] * 2)

    def test_summary_has_chevron(self):
        page = self.open("#/review/R-0001")
        mark = "getComputedStyle(document.querySelector('details.item > summary .n'), '::before').content"
        closed = page.js(mark)
        page.page.locator("details.item > summary").first.click()
        opened = page.js(mark)
        self.assertIn("▸", closed)
        self.assertIn("▾", opened)
        page.hash("#/plan/P-0001")                                    # plan steps are the same kind of row
        self.assertIn("▸", page.js(mark))


LONG = "Averyverylongunbrokenidentifier" * 5          # 155 characters with nowhere to break
VIEWS = ("#/", "#/inbox", "#/history", "#/reviews", "#/review/R-0003", "#/goals", "#/issues", "#/issue/ISS-0002",
         "#/plans", "#/plan/P-0001", "#/trends", "#/explore", "#/explore/issues")


def build_stress(L):
    """The demo plus a third review of 130 points with long unbroken titles and paths, a gate with long unbroken
    text, an issue and a plan step named the same way, and metrics over ten reviews' worth of points."""
    ids = fixtures.seed_demo(L)
    fixtures.finish_demo(L)
    sha = L.repo.head()
    r3 = L.review("R-0003", commit_sha=sha, previous_review_id=ids["reviews"]["R-0002"])
    path = "src/" + "deeply_nested_directory_name/" * 6 + "module.py"
    for n in range(1, 131):
        f = L.finding(r3, f"TEST-T{n:03d}", severity="T", kind="test", domain_code="tests",
                      summary=f"{LONG} number {n}.")
        if n <= 3:
            L.location(f, path, 1, 2, excerpt="x = 1\ny = 2")
    L.gate("GATE-0003", "other", LONG, "Should " + LONG + " stay?", context="See " + path + " and " + LONG,
           allow_text=1, blocking=0)
    ids["long_issue"] = L.scalar("SELECT code FROM issues WHERE id=?", (L.issue(None, first_seen_review_id=r3, title=LONG),))
    L.step(ids["plan"], ids["phases"][1], 4, LONG, change="Change " + path, proof=LONG, section=LONG)
    for n in range(4, 13):
        r = L.review(f"R-{n:04d}", commit_sha=sha, status="handed-off")
        L.metric(r, "line_coverage", 40.0 + n, "measured", scope="all", unit="%")
    return ids


# The accessible name of a control, as far as the page uses the rules: aria-labelledby (an element may name itself
# by its aria-label), aria-label, then its text.
NAMES = """sel => { const name = e => { const by = e.getAttribute("aria-labelledby");
    if (by) return by.split(/\\s+/).map(id => { const t = document.getElementById(id);
      return t === e ? (e.getAttribute("aria-label") || e.textContent) : t ? t.textContent : "?"; }).join(" ").trim();
    return (e.getAttribute("aria-label") || e.textContent || "").trim(); };
  return [...document.querySelectorAll(sel)].filter(e => e.offsetParent !== null).map(name); }"""
CONTROLS = "button, select, textarea, input:not([type=radio]), [role=radiogroup]"


class Stress(browser.ViewerTestCase):
    """Keyboard, structure, links and phone width on the stress ledger (read only)."""
    build_ledger = classmethod(lambda cls, L: build_stress(L))

    FOCUSED = """() => { const e = document.activeElement, cs = getComputedStyle(e), r = e.getBoundingClientRect();
      return {tag: e.tagName, id: e.id, text: (e.getAttribute("aria-label") || e.textContent || "").trim().slice(0, 40),
              ring: cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) >= 2 || cs.boxShadow !== "none",
              top: r.top, bottom: r.bottom, header: document.querySelector("header.top").getBoundingClientRect().bottom,
              inApp: !!e.closest("#app")}; }"""

    def _tab_through(self, page, key="Tab", limit=400):
        seen = []
        for _ in range(limit):
            page.page.keyboard.press(key)
            f = page.js(self.FOCUSED)
            if f["tag"] == "BODY" or (seen and (f["tag"], f["id"], f["text"], round(f["top"])) == seen[0][0]):
                break
            seen.append(((f["tag"], f["id"], f["text"], round(f["top"])), f))
        return [f for _, f in seen]

    def test_every_control_has_visible_focus(self):
        for hash_ in VIEWS:
            page = self.open(hash_)                                  # a fresh load: Tab starts at the top
            stops = self._tab_through(page)
            self.assertGreater(len(stops), 9, hash_)                 # the header alone has ten
            for f in stops:
                with self.subTest(view=hash_, control=f["text"]):
                    self.assertTrue(f["ring"], f)

    def test_skip_link_first(self):
        page = self.open("#/review/R-0003")
        page.page.keyboard.press("Tab")
        first = page.js(self.FOCUSED)
        self.assertEqual((first["tag"], first["text"]), ("A", "Skip to content"))
        self.assertGreaterEqual(first["top"], 0)                     # shown while it has focus
        page.page.keyboard.press("Enter")
        page.settle()
        f = page.js(self.FOCUSED)
        self.assertEqual((f["tag"], f["inApp"]), ("H1", True))
        self.assertEqual(page.location_hash(), "#/review/R-0003")    # the address is left alone
        page.page.keyboard.press("Tab")                              # and Tab goes on from there, past the header
        self.assertTrue(page.js(self.FOCUSED)["inApp"])

    def test_focus_after_actions(self):
        page = self.open("#/reviews")
        self.assertEqual(page.js("document.activeElement.tagName"), "BODY")   # a fresh load leaves focus alone
        page.hash("#/plan/P-0001")                                   # a route change: the page heading
        f = page.js(self.FOCUSED)
        self.assertEqual((f["tag"], f["text"]), ("H1", "P-0001 · Fix plan for R-0001"))
        rail = page.page.locator("nav.rail button")
        rail.nth(1).focus()
        page.page.keyboard.press("Enter")                            # a phase: the button keeps focus
        page.settle()
        self.assertEqual(page.js("[document.activeElement.textContent, document.activeElement.getAttribute('aria-current')]"),
                         ["2Fix firststeps 2–31/2", "true"])
        self.assertEqual(rail.first.get_attribute("aria-current"), "false")
        page.hash("#/review/R-0003")
        item = page.page.locator("details.item").first
        item.locator("summary").click()
        btn = item.locator(".refline button").first
        btn.focus()
        page.page.keyboard.press("Enter")                            # Show code: the button keeps focus and works again
        item.locator(".code").wait_for()
        page.settle()
        self.assertEqual(page.js("document.activeElement.textContent"), "Hide code")
        self.assertTrue(btn.is_enabled())
        page.page.keyboard.press("Enter")
        self.assertEqual(page.js("document.activeElement.textContent"), "Show code at this commit")
        more = page.page.locator("button", has_text="Show more")
        more.focus()
        page.page.keyboard.press("Enter")                            # Show more: the first point it added
        page.settle()
        self.assertEqual(page.js("document.activeElement.closest('details').querySelector('.n').textContent"), "TEST-T101")
        page.hash("#/explore/findings")
        nxt = page.page.get_by_role("button", name="Next", exact=True)
        self.assertEqual(nxt.count(), 1)                             # the first 100 of its rows
        nxt.focus()
        page.after(lambda: page.page.keyboard.press("Enter"))        # Next: the last page has no Next; Previous has it
        self.assertEqual(page.js("document.activeElement.textContent"), "Previous")
        page.after(lambda: page.page.keyboard.press("Enter"))
        self.assertEqual(page.js("document.activeElement.textContent"), "Next")

    def test_focus_not_obscured(self):
        for viewport in (browser.DESKTOP, browser.PHONE):
            page = self.open("#/review/R-0003", viewport=viewport)
            page.js("window.scrollTo(0, document.body.scrollHeight)")
            page.page.locator("button", has_text="Show more").focus()
            stops = self._tab_through(page, "Shift+Tab", limit=60)
            self.assertGreater(len(stops), 50)
            for f in stops:
                if f["inApp"]:
                    self.assertGreaterEqual(f["top"], f["header"] - 0.5, (viewport["width"], f))

    def test_page_is_not_one_live_region(self):
        page = self.open("#/review/R-0003")
        self.assertIsNone(page.js("document.getElementById('app').getAttribute('aria-live')"))
        self.assertEqual(page.js("[...document.querySelectorAll('[role=status], [aria-live]')].map(e => e.id)"), ["live"])
        self.assertEqual(page.js("document.getElementById('live').className"), "visually-hidden")
        page.page.select_option("#fs", "T")
        page.settle()
        self.assertEqual(page.text("#live"), "130 of 130 points")
        page.allow_console_errors = True                             # Chrome logs the 404
        page.hash("#/issue/ISS-9999")                                # an error is said too
        self.assertEqual(page.text("#live"), "There is no issue ISS-9999 in this ledger.")   # step 174
        self.assertEqual(page.text("#live"), page.text("#app p.lede"))

    # ------------------------------------------------------------------ step 179: structure and names

    def test_heading_levels_do_not_skip(self):
        page = self.open("#/")
        for hash_ in VIEWS + ("#/plan/P-0001/phase/2",):
            page.hash(hash_)
            if hash_.startswith("#/review/"):
                page.page.locator("details.item > summary").first.click()
            got = page.js("""() => ({levels: [...document.querySelectorAll("h1, h2, h3, h4, h5, h6")]
              .filter(e => e.offsetParent !== null).map(e => +e.tagName[1]),
              mains: [...document.querySelectorAll("main, [role=main]")].map(e => e.id)})""")
            with self.subTest(hash_):
                self.assertEqual(got["mains"], ["app"])
                self.assertEqual(got["levels"][0], 1)
                self.assertEqual(got["levels"].count(1), 1)
                for a, b in zip(got["levels"], got["levels"][1:]):
                    self.assertLessEqual(b, a + 1, got["levels"])
        # an answered gate's title sits under the Answered heading
        gate = self.ledger.id_of("gates", "GATE-0002")
        self.write("UPDATE gates SET status='answered', answer_value='approve' WHERE id=?", (gate,))
        try:
            page.hash("#/reviews")
            page.hash("#/inbox")
            got = page.js("[...document.querySelectorAll('#app h1, #app h2, #app h3')].map(e => [e.tagName, e.textContent])")
            i = got.index(["H2", "Answered, waiting for the agent to act"])
            self.assertEqual(got[i + 1], ["H3", "Approve the domains for R-0002"])
            self.assertEqual({t for t, _ in got[1:i]}, {"H2"})
            # and looks like the open gates' titles
            self.assertEqual(page.js("[getComputedStyle(document.querySelector('#app .gate h2')).font,"
                                     " getComputedStyle(document.querySelector('#app .gate h2')).textTransform]"),
                             page.js("[getComputedStyle(document.querySelector('#app .gate h3')).font,"
                                     " getComputedStyle(document.querySelector('#app .gate h3')).textTransform]"))
        finally:
            self.write("UPDATE gates SET status='open', answer_value=NULL, answered_at=NULL WHERE id=?", (gate,))

    def test_control_names_unique_per_view(self):
        page = self.open("#/")
        for hash_ in VIEWS:
            page.hash(hash_)
            if hash_ == "#/review/R-0003":                 # open three points and their code
                for n in range(3):
                    item = page.page.locator("details.item").nth(n)
                    item.locator("summary").click()
                    item.locator(".refline button").first.click()
                    item.locator(".code").wait_for()
            names = page.js(NAMES, CONTROLS)
            with self.subTest(hash_):
                self.assertNotIn("", names)
                self.assertEqual(sorted(n for n in set(names) if names.count(n) > 1), [])
        page.hash("#/inbox")
        names = page.js(NAMES, "#app " + CONTROLS.replace(", ", ", #app "))
        for title in ("Default notice period", "Approve the domains for R-0002"):
            self.assertIn("Send answer " + title, names)
        self.assertIn("Approve the domains for R-0002", names)             # its choices
        self.assertIn("Your answer " + LONG, names)
        page.hash("#/review/R-0001")
        sha = self.ids["commits"][0][:10]
        self.assertIn("Copy commit " + sha, page.js(NAMES, "#app button"))
        self.assertEqual(page.texts("#app td button")[0], "Copy")

    def test_select_labels(self):
        page = self.open("#/review/R-0001")
        self.assertEqual(page.js(NAMES, "#app .tools select"), ["Area", "Severity", "Match", "Check"])
        self.assertEqual(page.js("[...document.querySelectorAll('#app .tools select')].map(s => s.options[0].text)"),
                         ["All areas", "All severities", "Any match", "Any check"])

    def test_no_empty_th(self):
        page = self.open("#/")
        seen = 0
        for hash_ in VIEWS:
            page.hash(hash_)
            ths = page.contents("#app th")
            seen += len(ths)
            self.assertNotIn("", ths, hash_)
        self.assertGreater(seen, 60)
        page.hash("#/review/R-0001")
        self.assertIn("Reviewed?", page.contents("#app th"))
        i2, i3 = self.ids["issues"]["ISS-0002"], self.ids["issues"]["ISS-0003"]
        self.ledger.relation(i3, i2, "blocks", "Same function")
        self.ledger.commit()
        page.hash("#/issue/ISS-0002")
        self.assertEqual(page.contents("#app th")[:2], ["Link", "Issue"])

    def test_badge_accessible_name(self):
        page = self.open("#/reviews")
        tabs = page.page.locator("#tabs").aria_snapshot()
        self.assertIn('link "Waiting on you, 3 waiting"', tabs)
        self.assertEqual(page.text(".tabs .badge"), "3")

    # ------------------------------------------------------------------ step 180: rows are links

    def test_page_table_keyboard_row(self):
        page = self.open("#/reviews")
        got = page.js("""() => { const cols = [{label: "A", key: "a"}, {label: "B", get: r => h("b", null, r.b)}];
          const t = table(cols, [{a: "one", b: "x"}, {a: "two", b: "y"}], r => r.a === "one" ? "#/issue/" + r.a : null);
          const plain = table(cols, [{a: "one", b: "x"}]);
          const second = table([cols[0], {...cols[1], link: true}], [{a: "one", b: "x"}], () => "#/plans");
          return {empty: [table(cols, []).outerHTML, table(cols, [], null, "No reviews yet.").textContent],
                  rows: [...t.querySelectorAll("tbody tr")].map(r => [r.className, r.hasAttribute("tabindex"), r.cells[0].innerHTML]),
                  plain: plain.querySelector("tbody tr").outerHTML, second: second.querySelector("tbody tr").innerHTML}; }""")
        self.assertEqual(got["empty"], ['<p class="empty">Nothing here yet.</p>', "No reviews yet."])
        self.assertEqual(got["rows"], [["link", False, '<a href="#/issue/one">one</a>'], ["", False, "two"]])
        self.assertEqual(got["plain"], "<tr><td>one</td><td><b>x</b></td></tr>")
        self.assertEqual(got["second"], '<td>one</td><td><a href="#/plans"><b>x</b></a></td>')
        # Tab reaches the row's link; Enter follows it
        link = page.page.locator("#app tbody tr", has_text="R-0002").locator("a")
        link.focus()
        self.assertEqual(page.js("[document.activeElement.tagName, document.activeElement.getAttribute('href')]"),
                         ["A", "#/review/R-0002"])
        page.after(lambda: page.page.keyboard.press("Enter"))
        self.assertEqual(page.location_hash(), "#/review/R-0002")
        # a click anywhere else on the row still opens it; selecting text in it does not
        page.hash("#/reviews")
        row = page.page.locator("#app tbody tr", has_text="R-0001")
        box = row.locator("td").nth(1).bounding_box()

        def drag():                                                   # select part of the title with the mouse
            page.page.mouse.move(box["x"] + 10, box["y"] + 16)
            page.page.mouse.down()
            page.page.mouse.move(box["x"] + 120, box["y"] + 16, steps=4)
            page.page.mouse.up()
        page.after(drag, timeout=300)
        self.assertNotEqual(page.js("String(getSelection())"), "")
        self.assertEqual(page.location_hash(), "#/reviews")
        page.js("getSelection().removeAllRanges()")
        page.after(lambda: row.locator("td").last.click())
        self.assertEqual(page.location_hash(), "#/review/R-0001")

    def test_row_targets_are_links(self):
        page = self.open("#/")
        rows = 0
        for hash_ in VIEWS:
            page.hash(hash_)
            got = page.js("""() => [...document.querySelectorAll("#app tr")].map(r =>
              [r.className, r.hasAttribute("tabindex"), [...r.querySelectorAll("a")].map(a => a.getAttribute("href"))])""")
            for cls, tab, hrefs in got:
                self.assertFalse(tab, hash_)                          # the link takes the focus, not the row
                if cls == "link":
                    rows += 1
                    self.assertEqual([h[:2] for h in hrefs if h.startswith("#")][:1], ["#/"], hash_)
        self.assertGreater(rows, 25)
        page.hash("#/")
        checks = page.page.locator(".card:has(h2:text('Housekeeping for the agent')) tbody tr")
        got = checks.evaluate_all("rs => rs.map(r => [r.cells[1].textContent, r.className, r.querySelectorAll('a').length])")
        self.assertEqual({(cls, links) for n, cls, links in got if n == "0"}, {("", 0)})      # nothing to open
        self.assertEqual({(cls, links) for n, cls, links in got if n != "0"}, {("link", 1)})
        # a related issue's row links from the issue's own cell
        i2, i3 = self.ids["issues"]["ISS-0002"], self.ids["issues"]["ISS-0003"]
        if not self.ledger.scalar("SELECT count(*) FROM issue_relations"):
            self.ledger.relation(i3, i2, "blocks", "Same function")
            self.ledger.commit()
        page.hash("#/issue/ISS-0002")
        row = page.page.locator("h2:text('Related issues') + div tbody tr").first
        self.assertEqual(row.locator("a").evaluate_all("as => as.map(a => [a.textContent, a.getAttribute('href')])"),
                         [["ISS-0003", "#/issue/ISS-0003"]])

    # ------------------------------------------------------------------ step 182: phone width

    def _phone_views(self):
        return VIEWS + ("#/issue/" + self.ids["long_issue"], "#/plan/P-0001/phase/1", "#/explore/findings")

    def _open_everything(self, page, hash_):
        """Open the first expandable rows of a view, and the code of the first reference."""
        if page.count("details.item"):
            for n in range(min(3, page.count("details.item"))):
                page.page.locator("details.item > summary").nth(n).click()
            btn = page.page.locator("details.item[open] .refline button")
            if btn.count():
                btn.first.click()
                page.page.locator(".code, .ref .status, .ref .note").first.wait_for()
                page.settle()

    def test_no_horizontal_overflow_390(self):
        page = self.open("#/", viewport=browser.PHONE)
        for hash_ in self._phone_views():
            page.hash(hash_)
            self._open_everything(page, hash_)
            with self.subTest(hash_):
                wide = page.js("""() => [...document.querySelectorAll("body *")].filter(e => {
                    if (e.closest(".scroll, .code, .tlist, .visually-hidden, nav.rail")) return false;   // these scroll inside themselves
                    const r = e.getBoundingClientRect(); return r.width && r.right > innerWidth + 0.5; })
                  .slice(0, 5).map(e => e.tagName + "." + e.className + ": " + e.textContent.slice(0, 40))""")
                self.assertEqual(wide, [])
                self.assertLessEqual(page.js("document.documentElement.scrollWidth"), 390)

    def test_all_tabs_visible_390(self):
        page = self.open("#/trends", viewport=browser.PHONE)
        boxes = page.js("[...document.querySelectorAll('#tabs a')].map(a => { const r = a.getBoundingClientRect();"
                        " return [a.textContent, r.left, r.right, r.top, r.bottom]; })")
        self.assertEqual(len(boxes), 10)   # How it works (step 197) and Ideas (step 145) are tabs too
        for text, left, right, top, bottom in boxes:
            self.assertTrue(0 <= left and right <= 390 and 0 <= top and bottom <= 844, (text, left, right, top, bottom))
        # between phone and desktop width the tabs scroll sideways, and the current one is brought into view
        mid = self.open("#/explore", viewport={"width": 640, "height": 800})
        r = mid.js("(r => [r.left, r.right])(document.querySelector('#tabs a[aria-current=page]').getBoundingClientRect())")
        self.assertTrue(0 <= r[0] and r[1] <= 640, r)

    def test_chart_has_data_table(self):
        page = self.open("#/trends")
        got = page.js("""() => [...document.querySelectorAll("#app .chart")].map(c => ({
            cls: c.querySelector("table").className, head: [...c.querySelectorAll("th")].map(t => t.textContent),
            rows: [...c.querySelectorAll("tbody tr")].map(r => [...r.cells].map(td => td.textContent)),
            points: c.querySelectorAll("circle").length}))""")
        self.assertEqual(len(got), 5)   # two measurements, and three per-review counts (step 168)
        for c in got:
            self.assertEqual(c["cls"], "visually-hidden")
            self.assertEqual(c["head"], ["Review", "Value"])
            self.assertEqual(len(c["rows"]), c["points"])
        cover = next(c for c in got if c["points"] == 9)
        self.assertEqual(cover["rows"][:2], [["R-0004", "44 %"], ["R-0005", "45 %"]])

    def test_chart_label_size_390(self):
        for viewport in (browser.PHONE, browser.DESKTOP):
            page = self.open("#/trends", viewport=viewport)
            got = page.js("""() => [...document.querySelectorAll("#app .chart svg")].map(svg => {
                const scale = svg.getBoundingClientRect().width / svg.viewBox.baseVal.width;
                const texts = [...svg.querySelectorAll("text")], boxes = texts.map(t => t.getBoundingClientRect());
                const clash = boxes.some((a, i) => boxes.some((b, j) => j > i && texts[i].getAttribute("y") === texts[j].getAttribute("y")
                  && a.left < b.right && b.left < a.right));
                return {px: texts.map(t => parseFloat(getComputedStyle(t).fontSize) * scale), clash,
                        last: texts[texts.length - 1].textContent, out: boxes.some(b => b.left < 0 || b.right > innerWidth)}; })""")
            self.assertEqual(len(got), 5)   # two measurements, and points, missing tests and confirmed per review
            for c in got:
                self.assertGreaterEqual(min(c["px"]), 11, viewport)
                self.assertFalse(c["clash"], viewport)                # labels that would overlap are left out
                self.assertFalse(c["out"], viewport)
            self.assertIn("R-0012", [c["last"] for c in got])         # the latest review keeps its label


class HubLook(unittest.TestCase):
    """The page written by progress-page from the stress ledger: the same structure and width rules hold there."""

    @classmethod
    def setUpClass(cls):
        cls.browser = browser.require_browser()
        cls.L = fixtures.Ledger()
        cls.ids = build_stress(cls.L)
        cls.L.commit()
        cls.hub = cls.L.root / "hub.html"
        cls.L.run_ok("progress-page", "-o", str(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()
        cls.L.repo.cleanup()

    def test_hub_structure_and_phone_width(self):
        p = browser.ViewerPage(self.browser, self.hub.as_uri(), viewport=browser.PHONE)
        self.addCleanup(p.close)
        p.page.goto(self.hub.as_uri())
        p.settle()
        for slug in ("home", "guide", "inbox", "history", "reviews", "review-R-0003", "goals", "issues",
                     "issue-" + self.ids["long_issue"], "plans", "plan-P-0001", "trends", "explore", "explore-findings"):
            p.js(f"location.hash = {slug!r}")
            p.settle(quiet_ms=60)
            if p.count("details.item"):
                p.page.locator("details.item > summary").first.click()
            got = p.js("""() => ({levels: [...document.querySelectorAll("h1, h2, h3, h4")].filter(e => e.offsetParent !== null).map(e => +e.tagName[1]),
              mains: document.querySelectorAll("main").length, width: document.documentElement.scrollWidth,
              h1: document.querySelector("#app h1").textContent, errs: document.querySelectorAll(".status.err").length,
              emptyTh: [...document.querySelectorAll("#app th")].filter(t => !t.textContent).length})""")
            with self.subTest(slug):
                self.assertNotEqual(got["h1"], "Could not load this view")
                self.assertEqual((got["mains"], got["emptyTh"]), (1, 0))
                self.assertEqual(got["errs"], 0)    # (step 188: the line about checks is a callout, not an error)
                self.assertEqual(got["levels"][0], 1)
                for a, b in zip(got["levels"], got["levels"][1:]):
                    self.assertLessEqual(b, a + 1, got["levels"])
                self.assertLessEqual(got["width"], 390)
        self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], []))
        self.assertEqual([u for u in p.requests if u != self.hub.as_uri()], [])
