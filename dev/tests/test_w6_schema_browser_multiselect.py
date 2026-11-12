"""Plan P-0001 step 217 in a real browser: a multi-select gate is drawn as checkboxes with the recommended options
ticked and Select all / Select none, in the viewer and in the published hub, and the chosen values are saved as a list.
Skipped cleanly when Playwright or a browser is missing (see dev/tests/browser.py)."""
import json
import unittest

from dev.tests import browser, fixtures
from dev.tests.test_browser_p5_tools_hub import FAKE_DB

OPTS = [{"value": "ruff", "label": "Ruff"}, {"value": "mypy", "label": "mypy"}, {"value": "vale", "label": "Vale"}]


def add_gates(L):
    L.gate(kind="decision", title="Which tools may run?", question="Pick the tools.", options=json.dumps(OPTS),
           multi=1, preselect=json.dumps(["ruff", "vale"]), blocking=0)
    return L.scalar("SELECT code FROM gates WHERE multi = 1")


class ViewerMultiSelect(browser.ViewerTestCase):
    @classmethod
    def build_ledger(cls, ledger):
        return {"gate": add_gates(ledger)}

    def test_page_multiselect_checkboxes(self):
        code = self.ids["gate"]
        p = self.open("#/inbox")
        card = p.page.locator(".card.gate", has=p.page.locator(f"input[name='opt-{code}']"))
        boxes = card.locator("input[type=checkbox]")
        self.assertEqual(boxes.count(), 3)
        self.assertEqual(card.locator("input[type=radio]").count(), 0)
        self.assertEqual([boxes.nth(i).is_checked() for i in range(3)], [True, False, True])
        card.get_by_role("button", name="Select none").click()
        self.assertEqual([boxes.nth(i).is_checked() for i in range(3)], [False, False, False])
        card.get_by_role("button", name="Select all").click()
        self.assertEqual([boxes.nth(i).is_checked() for i in range(3)], [True, True, True])
        boxes.nth(0).uncheck()
        p.after(lambda: card.locator("button", has_text="Send answer").click())
        self.assertEqual(json.loads(self.ledger.scalar("SELECT answer_value FROM gates WHERE code=?", (code,))),
                         ["mypy", "vale"])
        self.assertIn("mypy, Vale", p.text("#app"))


class HubMultiSelect(unittest.TestCase):
    def test_page_multiselect_checkboxes_in_hub(self):
        b = browser.require_browser()
        L = fixtures.Ledger()
        self.addCleanup(L.close)
        code = add_gates(L)
        L.commit()
        hub = L.root / "hub.html"
        L.run_ok("progress-page", "-o", str(hub))
        p = browser.ViewerPage(b, hub.as_uri())
        self.addCleanup(p.close)
        p.context.add_init_script(FAKE_DB)
        p.page.goto(hub.as_uri())
        p.settle()
        p.js('location.hash = "inbox"')
        p.settle(quiet_ms=60)
        card = p.page.locator(".card.gate", has=p.page.locator(f"input[name='opt-{code}']"))
        boxes = card.locator("input[type=checkbox]")
        self.assertEqual([boxes.nth(i).is_checked() for i in range(3)], [True, False, True])
        card.locator("button", has_text="Send answer").click()
        p.settle(quiet_ms=60)
        saved = p.js("window.__answers")
        self.assertEqual(saved[code]["answer_value"], ["ruff", "vale"])
        self.assertEqual((p.page_errors, p.console_errors, p.outside_requests), ([], [], []))
        f = L.root / "answers.json"
        f.write_text(json.dumps([{"id": k, "data": v} for k, v in saved.items()]))
        self.assertIn(f"{code}: recorded", L.run_ok("import-answers", str(f)).stdout)
        self.assertEqual(json.loads(L.scalar("SELECT answer_value FROM gates WHERE code=?", (code,))), ["ruff", "vale"])


if __name__ == "__main__":
    unittest.main()
