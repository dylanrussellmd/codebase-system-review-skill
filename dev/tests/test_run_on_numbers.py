"""Stored text that runs a word into a number is flagged (plan P-0002 step 332): "Steps1,2,3 and38" reads wrongly on
the page, and some writers drop those spaces."""
import unittest

from dev.tests.fixtures import Ledger, load_ledger_module


class RunOnNumbers(unittest.TestCase):
    def test_spots(self):
        m = load_ledger_module()
        cases = {
            "Steps1,2,3 and38 are complete": ["Steps1,2,3", "and38"],
            "passed in the snapshot:73Python tests, 1,388/1,388 GUT tests": ["snapshot:73Python"],
            "The manager verified63work": ["verified63work"],
            # names, codes, hashes, versions, units, paths and code spans are left alone
            "ISS-0001 R-0003 GATE-0035 H1 PG14 sha256 utf8 ipv6 base64 v3 x86 e2e 9ff70bc 10ms 2nd 3x 64bit": [],
            "see dev/core.py:1723, `step1`, https://example.com/a1b and 1.30.6 on 2027-01-15": [],
        }
        for text, spots in cases.items():
            with self.subTest(text=text):
                self.assertEqual(m.run_on_numbers(text), spots)

    def test_writes_warn_and_still_store(self):
        L = Ledger()
        self.addCleanup(L.close)
        L.plan("P-0001")
        p = L.run_ok("set-narrative", "P-0001", "-", input="Steps1,2,3 and38 are complete.\n")
        self.assertIn("warning: set-narrative P-0001: a word runs into a number at 'Steps1,2,3', 'and38'", p.stderr)
        self.assertEqual(L.scalar("SELECT narrative_md FROM plans WHERE code='P-0001'"),
                         "Steps1,2,3 and38 are complete.\n")
        p = L.run_ok("set-narrative", "P-0001", "-", input="Steps 1, 2, 3 and 38 are complete.\n")
        self.assertNotIn("warning", p.stderr)
        p = L.run("plan-status", "P-0001", "drafting", "--note", "verified63work")
        self.assertIn("warning: plan-status --note: a word runs into a number at 'verified63work'", p.stderr)
