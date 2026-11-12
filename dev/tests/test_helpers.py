"""The measuring helpers in dev/tests/helpers.py work (plan step 29)."""
import json
import sqlite3
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests.helpers import eqp, time_route, trace_statements


class MeasuringHelpers(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        self.con.execute("CREATE INDEX t_b ON t (b)")

    def tearDown(self):
        self.con.close()

    def test_eqp_reports_index_use_and_scans(self):
        self.assertTrue(any("USING" in d and "t_b" in d for d in eqp(self.con, "SELECT a FROM t WHERE b = ?", ("x",))))
        self.assertTrue(any(d.startswith("SCAN") for d in eqp(self.con, "SELECT a FROM t WHERE b LIKE '%x'")))

    def test_trace_statements_counts_and_stops(self):
        with trace_statements(self.con) as seen:
            for i in range(3):
                self.con.execute("INSERT INTO t (b) VALUES (?)", (str(i),))
        self.assertEqual(sum("INSERT" in s for s in seen), 3)
        self.con.execute("SELECT 1")
        self.assertFalse(any("SELECT 1" in s for s in seen))

    def test_time_route_times_a_viewer_request(self):
        with fixtures.Ledger() as ledger, fixtures.ServerThread(ledger) as srv:
            secs, r = time_route(srv, "/api/overview")
        self.assertEqual(r.status, 200)
        self.assertGreater(secs, 0)

    def test_baseline_file_answers_the_two_open_measurements(self):
        doc = json.loads((Path(__file__).parent / "perf_baseline.json").read_text())
        m = doc["measurements"]
        for name in ("x1_prose.api_review_gzip_ratio", "x1_prose.api_plan_gzip_ratio", "x1_prose.dump_gzip_ratio",
                     "x1.git_loose_growth_per_ledger_commit_mean", "x1.git_pack_growth_per_ledger_commit_after_gc"):
            self.assertIn(name, m)
            self.assertGreater(m[name]["value"], 0)


if __name__ == "__main__":
    unittest.main()
