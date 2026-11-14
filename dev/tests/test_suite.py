"""The suite itself: discovery works with the documented command, and the shared fixtures behave as documented."""
import ast
import re
import socket
import sqlite3
import subprocess
import sys
import unittest

from pathlib import Path

from dev.tests import fixtures
from dev.tests.fixtures import Clock, Ledger, ScratchRepo, ServerThread

ROOT = fixtures.REPO_ROOT
COMMAND = "python3 -m unittest discover -s dev/tests -t . -v"


# What switches a gated test on: the marker names a test or the helpers it calls use, and the variable each needs.
GATES = {"require_browser": "LEDGER_BROWSER_TESTS", "ViewerTestCase": "LEDGER_BROWSER_TESTS",
         "LEDGER_BROWSER_TESTS": "LEDGER_BROWSER_TESTS", "slow": "LEDGER_SLOW_TESTS", "SLOW": "LEDGER_SLOW_TESTS",
         "LEDGER_SLOW_TESTS": "LEDGER_SLOW_TESTS"}
DISCOVER_ALL = re.compile(r"python3 -m unittest discover -s dev/tests -t \.(?: -v)?\s*$")


def _names(node):
    """Every name, attribute name and LEDGER_* string used inside node; 'slow' only as a decorator, since tests use
    the word for other things too."""
    decorators = {id(d) for n in ast.walk(node) for d in getattr(n, "decorator_list", ())}
    out = set()

    def walk(n, in_decorator):
        in_decorator = in_decorator or id(n) in decorators
        name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else \
            n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("LEDGER_") \
            else None
        if name and (name != "slow" or in_decorator):
            out.add(name)
        for child in ast.iter_child_nodes(n):
            walk(child, in_decorator)
    walk(node, False)
    return out


def gated_tests(folder):
    """{"module.Class.test_name": {variable, ...}} for every test that a LEDGER_*_TESTS variable switches on: by its
    own decorators or body, its class's, a base class's (in any module here), or a module-level helper it calls."""
    mods = {f.stem: ast.parse(f.read_text(encoding="utf-8")) for f in sorted(Path(folder).glob("*.py"))
            if f.stem not in ("browser", "fixtures", "test_suite")}
    classes = {}   # name -> (needs of its own body, base names)
    helpers = {}   # module -> {function name: needs}

    def needs_of(names, funcs):
        got = {GATES[n] for n in names if n in GATES}
        for name in names & set(funcs):
            got |= funcs[name]
        return got

    for mod, tree in mods.items():
        funcs = {n.name: _names(n) for n in tree.body if isinstance(n, ast.FunctionDef)}
        resolved = {k: set() for k in funcs}
        for _ in range(len(funcs) + 1):   # helpers that call helpers
            resolved = {k: needs_of(v, resolved) for k, v in funcs.items()}
        helpers[mod] = resolved
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                own = set().union(*(_names(d) for d in n.decorator_list)) if n.decorator_list else set()
                for item in n.body:   # class-level code and fixtures, not the test methods
                    if not (isinstance(item, ast.FunctionDef) and item.name.startswith("test")):
                        own |= _names(item)
                classes[n.name] = (needs_of(own, resolved), [_names(b) for b in n.bases])

    def class_needs(name, seen=()):
        if name not in classes or name in seen:
            return {GATES[name]} if name in GATES else set()
        own, bases = classes[name]
        got = set(own)
        for b in bases:
            for part in b:
                got |= class_needs(part, seen + (name,))
        return got

    out = {}
    for mod, tree in mods.items():
        if not mod.startswith("test_"):
            continue
        for n in tree.body:
            if not isinstance(n, ast.ClassDef):
                continue
            base = class_needs(n.name)
            for item in n.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test"):
                    need = base | needs_of(_names(item), helpers[mod])
                    if need:
                        out[f"{mod}.{n.name}.{item.name}"] = need
    return out


def ci_steps(text):
    """[(job, variables set to "1", the step's command, whether the job installs Playwright)] from ci.yml: a small
    reader of the layout ci.yml uses (jobs at two spaces, steps as '- ' items, env: maps, run: lines or | blocks)."""
    jobs, job, in_jobs, skip_to = {}, None, False, 0
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i < skip_to:   # inside a run: | block already read
            continue
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            job = jobs.setdefault(m.group(1), {"env": {}, "steps": []})
            continue
        if job is None:
            continue
        s = line.strip()
        item = re.match(r"^      - ", line)
        if item:
            job["steps"].append({"env": {}, "run": ""})
            s = s[2:]
        target = job["steps"][-1] if job["steps"] and (item or len(line) - len(line.lstrip()) >= 8) else job
        m = re.match(r"^([A-Z][A-Z0-9_]*):\s*\"?([^\"]*)\"?\s*$", s)
        if m and target.get("_in_env"):
            target["env"][m.group(1)] = m.group(2)
            continue
        target.pop("_in_env", None)
        if s == "env:":
            target["_in_env"] = True
        elif s.startswith("run:"):
            body = s[4:].strip()
            if body == "|":
                indent = len(line) - len(line.lstrip())
                block = []
                for nxt in lines[i + 1:]:
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                        break
                    block.append(nxt.strip())
                skip_to = i + 1 + len(block)
                body = "\n".join(block)
            target["run"] = body
    out = []
    for name, j in jobs.items():
        installs = any("playwright install" in st["run"] for st in j["steps"])
        for st in j["steps"]:
            for cmd in st["run"].split("\n"):
                inline = dict(re.findall(r"\b(LEDGER_[A-Z_]+)=(\S+)\s", cmd))
                env = {**j["env"], **st["env"], **{k: v.strip("'\"") for k, v in inline.items()}}
                cmd = re.sub(r"^(?:[A-Z_]+=\S+\s+)+", "", cmd.strip())
                if cmd:
                    out.append((name, {k for k, v in env.items() if v == "1"}, cmd, installs))
    return out


class Discovery(unittest.TestCase):
    def test_suite_discovers_and_runs(self):
        loader = unittest.TestLoader()
        suite = loader.discover(start_dir=str(ROOT / "dev" / "tests"), top_level_dir=str(ROOT))
        self.assertEqual(loader.errors, [], "test modules failed to import")
        self.assertGreater(suite.countTestCases(), 0)
        # The documented command runs (one small module, so this stays fast and does not recurse).
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "dev/tests", "-t", ".",
                            "-p", "test_static.py"], cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                           env=fixtures.git_env())
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        m = re.search(r"Ran (\d+) tests?", p.stderr)
        self.assertTrue(m and int(m.group(1)) > 0, p.stderr[-500:])

    def test_suite_prints_environment_skips(self):
        """A run on a machine without the tools says how many tests it skipped for the machine, and which."""
        from dev.tests._w7a_tools_helpers import bare_bin
        env = dict(fixtures.git_env(), PATH=bare_bin())
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "dev/tests", "-t", ".",
                            "-p", "test_w7b_tools_docs.py"], cwd=str(ROOT), capture_output=True, text=True,
                           timeout=300, env=env)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        result = re.search(r"^OK \(skipped=(\d+)\)$", p.stderr, re.M)
        self.assertTrue(result, p.stderr[-1000:])
        tail = p.stderr[result.end():]
        m = re.search(r"^Skipped for this machine: (\d+) tests? \(the run is partial\)$", tail, re.M)
        self.assertTrue(m, tail[-1000:])
        self.assertGreaterEqual(int(m.group(1)), 2)
        self.assertIn("test_vale_matches_the_recording (test_w7b_tools_docs.Vale): Vale is not installed", tail)
        self.assertIn("test_lychee_matches_the_recording", tail)

    def test_environment_skips_carry_the_prefix(self):
        """Every skip that depends on the machine (a tool not on PATH, a platform, an address) starts its reason
        with fixtures.ENVIRONMENT, so the summary can count it."""
        machine = re.compile(r"not installed|which\(|POSIX|Linux only|IPv6|loopback|git checkout|git cannot|"
                             r"unshare|affinity|heap size|stderr\.strip\(\)")
        bad = []
        for f in sorted((ROOT / "dev" / "tests").glob("*.py")):
            lines = f.read_text(encoding="utf-8").split("\n")
            for no, line in enumerate(lines, 1):
                call = line + " " + (lines[no] if no < len(lines) else "")   # a reason may be on the next line
                if re.search(r"skip(?:Test|Unless|If)\(|SkipTest\(", line) and machine.search(call) \
                        and "environment: " not in call and f.name != "test_suite.py":
                    bad.append(f"{f.name}:{no}: {line.strip()}")
        self.assertEqual(bad, [])

    def test_readme_names_the_command(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Testing", readme)
        self.assertIn(COMMAND, readme)
        self.assertIn("python3 dev/build.py --check", readme)

    def test_ci_runs_build_check_and_suite(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python3 dev/build.py --check", ci)
        self.assertIn("python3 -m unittest discover -s dev/tests -t .", ci)
        for v in ("3.9", "3.12"):
            self.assertIn(v, ci)


    def reaching(self, need):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        return [(job, cmd) for job, env, cmd, installs in ci_steps(ci) if DISCOVER_ALL.match(cmd) and need <= env
                and ("LEDGER_BROWSER_TESTS" not in need or installs)]

    def test_every_gated_test_runs_in_some_ci_job(self):
        tests = gated_tests(ROOT / "dev" / "tests")
        self.assertGreater(len(tests), 30)
        for known in ("test_w2_perf.RouteBudgets.test_plan_route_budget_1x", "test_perf.ScaleGenerator"
                      ".test_perf_ledger_builds", "test_browser_smoke.ViewerSmoke.test_viewer_smoke_all_views"):
            self.assertIn(known, tests)
        self.assertEqual(tests["test_w2_perf.PageBudgets.test_plan_page_budget_1x"],
                         {"LEDGER_SLOW_TESTS", "LEDGER_BROWSER_TESTS"})
        missed = sorted(f"{t} (needs {', '.join(sorted(need))})" for t, need in tests.items()
                        if not self.reaching(need))
        self.assertEqual(missed, [], "run by no CI job")

    def test_ci_sets_every_test_gate(self):
        read = set()
        for f in (ROOT / "dev" / "tests").glob("*.py"):
            read |= set(re.findall(r"\bLEDGER_[A-Z_]*TESTS\b", f.read_text(encoding="utf-8")))
        self.assertIn("LEDGER_SLOW_TESTS", read)
        self.assertEqual(sorted(v for v in read if not self.reaching({v})), [])

    def test_ci_reader(self):
        text = ("jobs:\n  a:\n    steps:\n      - name: x\n        env:\n          LEDGER_SLOW_TESTS: \"1\"\n"
                "        run: python3 -m unittest discover -s dev/tests -t . -v\n  b:\n    steps:\n"
                "      - run: |\n          python3 -m pip install playwright\n          python3 -m playwright install"
                " chromium\n      - run: LEDGER_BROWSER_TESTS=1 python3 -m unittest discover -s dev/tests -t .\n")
        got = ci_steps(text)
        self.assertIn(("a", {"LEDGER_SLOW_TESTS"}, "python3 -m unittest discover -s dev/tests -t . -v", False), got)
        self.assertIn(("b", {"LEDGER_BROWSER_TESTS"}, "python3 -m unittest discover -s dev/tests -t .", True), got)


class ScratchRepoFixture(unittest.TestCase):
    def make(self):
        r = ScratchRepo()
        self.addCleanup(r.cleanup)
        r.write("a.txt", "one\n")
        c1 = r.commit("first")
        r.write("dir/b.txt", "two\n")
        c2 = r.commit("second")
        return r, c1, c2

    def test_shas_are_deterministic(self):
        a, b = self.make(), self.make()
        self.assertEqual(a[1:], b[1:])
        self.assertNotEqual(a[1], a[2])
        self.assertRegex(a[1], r"^[0-9a-f]{40}$")

    def test_identity_and_config_are_isolated(self):
        r, _, c2 = self.make()
        # git 2.56 and later print UTC as "Z" in iso-strict dates; earlier versions print "+00:00".
        self.assertIn(r.git("log", "-1", "--format=%an <%ae> %ad", "--date=iso-strict", c2),
                      ("Test Author <test@example.invalid> 2026-01-01T00:02:00+00:00",
                       "Test Author <test@example.invalid> 2026-01-01T00:02:00Z"))
        self.assertEqual(r.git("config", "--global", "--list", check=False), "")
        self.assertEqual(r.git("rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_ledger_folder_is_not_committed(self):
        r, _, _ = self.make()
        r.write(".code-review/ledger.sqlite", "x")
        r.commit("third")
        self.assertNotIn(".code-review", r.git("ls-files"))

    def test_subrepo_is_separate_and_ignored(self):
        r, _, _ = self.make()
        sub = r.subrepo("lib")
        sub.write("x.py", "x = 1\n")
        s1 = sub.commit("lib first")
        r.commit("parent after subrepo")
        self.assertNotIn("lib", r.git("ls-files"))
        self.assertEqual(sub.git("rev-parse", "--show-toplevel"), str(sub.path.resolve()))
        self.assertEqual(sub.head(), s1)

    def test_rewrite_history_drops_old_commits(self):
        r, c1, c2 = self.make()
        r.tag("v1", c1, message="tag")
        new = r.rewrite_history()
        self.assertNotIn(new, (c1, c2))
        self.assertFalse(r.has_object(c1))
        self.assertFalse(r.has_object(c2))
        self.assertEqual(r.tags(), [])
        self.assertEqual(r.read("dir/b.txt"), "two\n")

    def test_delete_and_remove(self):
        r, _, _ = self.make()
        r.delete("a.txt")
        r.commit("delete a")
        self.assertEqual(r.git("ls-files"), "dir/b.txt")
        r.remove()
        self.assertFalse(r.path.exists())


class LedgerFixture(unittest.TestCase):
    def test_clock_matches_the_ledger_format(self):
        mod = fixtures.load_ledger_module()
        shape = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertRegex(mod.now(), shape)
        c = Clock()
        a, b = c.tick(), c.tick()
        self.assertEqual(a, "2026-11-01T09:00:00.000Z")
        self.assertRegex(b, shape)
        self.assertLess(a, b)

    def test_ledger_is_initialised_with_foreign_keys_on(self):
        L = Ledger()
        self.addCleanup(L.close)
        self.assertTrue(L.db.exists())
        self.assertEqual(L.scalar("PRAGMA foreign_keys"), 1)
        self.assertEqual(L.scalar("SELECT value FROM meta WHERE key='project_name'"), L.root.name)
        self.assertEqual(int(L.scalar("SELECT value FROM meta WHERE key='schema_version'")),
                         L.module.SCHEMA_VERSION)
        with self.assertRaises(sqlite3.IntegrityError):
            L.finding(9999, "X-1")

    def test_real_init_matches_template(self):
        a, b = Ledger(), Ledger(real_init=True)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        q = "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        self.assertEqual(a.all(q), b.all(q))
        self.assertTrue((b.dir / "ledger.py").exists())

    def test_insert_helpers_fill_timestamps_and_codes(self):
        L = Ledger()
        self.addCleanup(L.close)
        sha = L.repo.commit("c")
        r = L.review(commit_sha=sha)
        f = L.finding(r, "SPEC-M1")
        i = L.issue(first_seen_review_id=r)
        L.match(f, i)
        L.location(f, "src/x.py", 3, 4)
        L.check_row(L.review(), i, "still-present")   # a later review checks it (not the one that found it)
        p = L.plan(reviews=(r,))
        ph = L.phase(p, 1)
        s = L.step(p, ph, 1, findings=(f,))
        L.gate(options=[{"value": "a", "label": "A"}], step_id=s)
        L.decision()
        self.assertEqual(L.scalar("SELECT min(code) FROM reviews"), "R-0001")
        self.assertEqual(L.scalar("SELECT code FROM issues"), "ISS-0001")
        self.assertEqual(L.scalar("SELECT code FROM gates"), "GATE-0001")
        self.assertEqual(L.scalar("SELECT commit_sha FROM finding_locations"), sha)
        stamps = [L.scalar("SELECT started_at FROM reviews"), L.scalar("SELECT created_at FROM findings"),
                  L.scalar("SELECT opened_at FROM issues")]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(s.startswith("2026-11-01T09:") for s in stamps), stamps)
        self.assertEqual(L.scalar("SELECT times_seen FROM issues"), 1)
        # the match's seen event, then the later review's check (schema 3 logs every check)
        self.assertEqual([e[2:] for e in L.events("issue") if e[2] in ("seen", "checked")],
                         [("seen", None, "new"), ("checked", None, "still-present")])

    def test_run_commits_first_and_reports_exit(self):
        L = Ledger()
        self.addCleanup(L.close)
        L.review()
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with self.assertRaises(AssertionError):
            L.run_ok("show", "999")


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.L.review("R-0001")
        cls.L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def check_server(self, srv):
        page = srv.get("/", token=False)
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.headers["Content-Type"])
        self.assertRegex(srv.token, r"^[A-Za-z0-9_\-]{16,}$")
        self.assertEqual(srv.get("/api/nothing-here", token=False).status, 403)
        self.assertNotEqual(srv.get("/api/nothing-here").status, 403)
        self.assertEqual(srv.get("/", headers={"Host": "evil.example"}, token=False).status, 403)
        raw = srv.raw(b"GET / HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % srv.port)
        self.assertTrue(raw.startswith(b"HTTP/1."), raw[:40])

    def assert_closed(self, port):
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()

    def test_server_thread_starts_and_stops(self):
        with ServerThread(self.L) as srv:
            self.check_server(srv)
            port = srv.port
        self.assert_closed(port)
        srv.stop()  # a second stop is harmless

    def test_server_process_mode(self):
        with ServerThread(self.L, mode="process") as srv:
            self.check_server(srv)
            port = srv.port
        self.assert_closed(port)


if __name__ == "__main__":
    unittest.main()
