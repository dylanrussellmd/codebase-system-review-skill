"""Coverage and the test-suite runs (plan P-0001 step 221; FIX-31, decision D-0015): `tool-run coverage --from FILE`
imports the project's own LCOV or Cobertura file into per-file and per-function metrics; `test-runs` runs the
project's suite on the reviewed commit as a baseline, then repeated, in random order with a recorded seed, with the
network off (unshare -rn on Linux; a stated fallback elsewhere) and at shifted clocks and time zones (faketime when
installed, TZ always), recording each run as a test run and each test that newly fails as a lead."""
import json
import os
import random
import sys
import unittest

from dev.tests import fixtures
from dev.tests._w7a_tools_helpers import APP_PY, DATA, env_with, ok, tool_project

# A tiny test runner in the project under review: it writes JUnit XML to argv[1]. test_flaky fails on every second
# run (a counter outside the export), test_c fails when test_d ran before it, test_zone fails in Kiritimati.
RUNNER = r'''import os, random, sys
from xml.sax.saxutils import quoteattr
order = ["test_a", "test_b", "test_c", "test_d", "test_flaky", "test_zone"]
seed = os.environ.get("LEDGER_TEST_SEED")
if seed is not None:
    random.Random(int(seed)).shuffle(order)
counter = os.environ.get("W7A_COUNTER")
n = 0
if counter:
    n = int(open(counter).read() or 0) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
ran, cases = [], []
for t in order:
    bad = None
    if t == "test_c" and "test_d" in ran:
        bad = "test_d left state behind"
    if t == "test_flaky" and n % 2 == 1:
        bad = "failed this time"
    if t == "test_zone" and os.environ.get("TZ") == "Pacific/Kiritimati":
        bad = "assumed the zone"
    ran.append(t)
    body = f"<failure message={quoteattr(bad)}>tests/test_suite.py:{order.index(t) + 3}: {bad}</failure>" if bad else ""
    cases.append(f'<testcase classname="tests.test_suite" name="{t}" file="tests/test_suite.py">{body}</testcase>')
fails = sum(1 for c in cases if "<failure" in c)
open(sys.argv[1], "w").write(f'<testsuite name="s" tests="{len(cases)}" failures="{fails}">' + "".join(cases) + "</testsuite>")
sys.exit(1 if fails else 0)
'''


def order_seed():
    """A seed whose shuffle puts test_d before test_c (the order-dependent pair)."""
    for s in range(1, 1000):
        order = ["test_a", "test_b", "test_c", "test_d", "test_flaky", "test_zone"]
        random.Random(s).shuffle(order)
        if order.index("test_d") < order.index("test_c"):
            return s


class Coverage(unittest.TestCase):
    def test_coverage_import(self):
        repo, L, sha = tool_project(self)
        out = ok(L, "tool-run", "coverage", "--review", "R-0001", "--from",
                 str(DATA / "cobertura-methods-documented.xml"), "--actor", "A-0001")
        self.assertIn("1 lead", out[0])
        L.commit()
        m = {(r["name"], r["scope"]): r["value"] for r in L.all("SELECT name, scope, value FROM metrics")}
        self.assertEqual(m[("line_coverage", "all")], 50.0)
        self.assertEqual(m[("line_coverage", "src/app.py")], 60.0)
        self.assertEqual(m[("branch_coverage", "src/app.py")], 50.0)
        self.assertEqual(m[("function_line_coverage", "src/app.py#fetch")], 0.0)
        self.assertEqual(m[("function_line_coverage", "src/app.py#area")], 100.0)
        self.assertEqual(L.scalar("SELECT output_format FROM tool_runs"), "cobertura")
        r = L.one("SELECT rule, path, domain_code FROM tool_results")
        self.assertEqual((r["rule"], r["path"], r["domain_code"]), ("coverage.untested-file", "src/util.js", "tests"))
        ok(L, "tool-run", "coverage", "--review", "R-0001", "--from", str(DATA / "coverage.lcov"), "--actor", "A-0001")
        L.commit()
        self.assertEqual(L.scalar("SELECT output_format FROM tool_runs WHERE id=2"), "lcov")
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='function_calls' AND scope='src/app.py#area'"),
                         1)
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='function_calls' AND scope='src/app.py#fetch'"),
                         0)


class TestRuns(unittest.TestCase):
    def suite_project(self):
        return tool_project(self, files={"src/app.py": APP_PY, "tests/run.py": RUNNER})

    def test_flaky_detection_on_seeded_suite(self):
        repo, L, sha = self.suite_project()
        counter = fixtures.temp_dir("w7a-count-") / "n"
        env = fixtures.git_env({"W7A_COUNTER": str(counter)})
        seed = order_seed()
        out = ok(L, "test-runs", "--review", "R-0001", "--command", f"{sys.executable} tests/run.py {{junit}}",
                 "--repeat", "2", "--seed", str(seed), "--variants", "repeat,random-order,clock", "--actor", "A-0001",
                 env=env)
        text = "\n".join(out)
        self.assertIn(f"seed {seed}", text)
        L.commit()
        runs = L.all("SELECT purpose, passed, failed, notes FROM test_runs ORDER BY id")
        self.assertEqual(runs[0]["purpose"], "baseline")
        self.assertEqual((runs[0]["passed"], runs[0]["failed"]), (6, 0))
        self.assertTrue(all(r["purpose"] == "flake-check" for r in runs[1:]))
        self.assertGreaterEqual(len(runs), 1 + 2 + 1 + 4)
        got = {(r["rule"], r["message"].split(" ")[0]) for r in L.all("SELECT rule, message FROM tool_results")}
        self.assertIn(("test-runs.flaky", "tests.test_suite.test_flaky"), got)
        self.assertIn(("test-runs.order-dependent", "tests.test_suite.test_c"), got)
        self.assertIn(("test-runs.clock", "tests.test_suite.test_zone"), got)
        self.assertEqual(len(got), 3, got)
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "test-quality")
        self.assertEqual(L.scalar("SELECT count(DISTINCT path) FROM tool_results"), 1)
        notes = " ".join(r["notes"] for r in runs)
        self.assertIn(f"seed {seed}", notes)
        self.assertIn("Pacific/Kiritimati", notes)
        # the same seed gives the same order: a rerun of the random-order variant finds the same lead
        ok(L, "test-runs", "--review", "R-0001", "--command", f"{sys.executable} tests/run.py {{junit}}",
           "--seed", str(seed), "--variants", "random-order", "--actor", "A-0001")
        L.commit()
        self.assertEqual(L.count("tool_results", "rule='test-runs.order-dependent'"), 2)

    def test_network_off_fallback_recorded(self):
        repo, L, sha = self.suite_project()
        env = env_with(bare=True)
        out = ok(L, "test-runs", "--review", "R-0001", "--command", f"{sys.executable} tests/run.py {{junit}}",
                 "--variants", "network-off", "--actor", "A-0001", env=env)
        self.assertIn("network off: 6 passed", "\n".join(out))
        self.assertIn("not cut", "\n".join(out))
        L.commit()
        run = L.one("SELECT * FROM tool_runs WHERE command LIKE 'network-off%'")
        self.assertIn("unshare", run["fallback"])
        self.assertIn("not cut", run["note"])
        self.assertEqual(L.scalar("SELECT notes FROM test_runs ORDER BY id DESC LIMIT 1").count("fallback"), 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "unshare is Linux only")
    def test_network_off_with_unshare(self):
        repo, L, sha = self.suite_project()
        mod = fixtures.load_ledger_module()
        if not mod.network_off_prefix():
            self.skipTest("unshare -rn is not allowed here")
        ok(L, "test-runs", "--review", "R-0001", "--command", f"{sys.executable} tests/run.py {{junit}}",
           "--variants", "network-off", "--actor", "A-0001")
        L.commit()
        run = L.one("SELECT * FROM tool_runs WHERE command LIKE 'network-off%'")
        self.assertIn("unshare -rn", run["command"])
        self.assertIsNone(run["fallback"])

    def test_command_needs_junit_placeholder(self):
        repo, L, sha = self.suite_project()
        p = L.run("test-runs", "--review", "R-0001", "--command", "python3 -m pytest", "--actor", "A-0001")
        self.assertEqual(p.returncode, 1)
        self.assertIn("{junit}", p.stderr)
        self.assertEqual(L.count("test_runs"), 0)

    def test_clock_instants_come_from_zoneinfo(self):
        mod = fixtures.load_ledger_module()
        instants = mod.clock_instants()
        names = [i[0] for i in instants]
        self.assertEqual(names, ["leap-day", "year-end", "dst-start", "dst-end"])
        dst = dict((i[0], i) for i in instants)["dst-start"]
        self.assertEqual(dst[1], "America/New_York")
        self.assertEqual(dst[2], "2027-03-14 01:59:30")
        self.assertEqual(dict((i[0], i) for i in instants)["dst-end"][2], "2027-10-31 02:59:30")


if __name__ == "__main__":
    unittest.main()
