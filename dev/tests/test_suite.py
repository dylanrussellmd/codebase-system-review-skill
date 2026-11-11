"""The suite itself: discovery works with the documented command, and the shared fixtures behave as documented."""
import re
import socket
import sqlite3
import subprocess
import sys
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Clock, Ledger, ScratchRepo, ServerThread

ROOT = fixtures.REPO_ROOT
COMMAND = "python3 -m unittest discover -s dev/tests -t . -v"


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
        self.assertEqual(r.git("log", "-1", "--format=%an <%ae> %ad", "--date=iso-strict", c2),
                         "Test Author <test@example.invalid> 2026-01-01T00:02:00+00:00")
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
        L.check_row(r, i, "still-present")
        p = L.plan(reviews=(r,))
        ph = L.phase(p, 1)
        s = L.step(p, ph, 1, findings=(f,))
        L.gate(options=[{"value": "a", "label": "A"}], step_id=s)
        L.decision()
        self.assertEqual(L.scalar("SELECT code FROM reviews"), "R-0001")
        self.assertEqual(L.scalar("SELECT code FROM issues"), "ISS-0001")
        self.assertEqual(L.scalar("SELECT code FROM gates"), "GATE-0001")
        self.assertEqual(L.scalar("SELECT commit_sha FROM finding_locations"), sha)
        stamps = [L.scalar("SELECT started_at FROM reviews"), L.scalar("SELECT created_at FROM findings"),
                  L.scalar("SELECT opened_at FROM issues")]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(s.startswith("2026-11-01T09:") for s in stamps), stamps)
        self.assertEqual(L.scalar("SELECT times_seen FROM issues"), 1)
        self.assertEqual(L.events("issue")[-1][2:], ("seen", None, "new"))

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
