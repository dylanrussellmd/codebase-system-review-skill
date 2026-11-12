"""fill-refs when git cannot answer: a commit the repository lacks is named as such (asked once per commit), a git
that does not respond stops the run, and a run with nothing left to fill says so."""
import subprocess
import sys
import time
import unittest
from unittest import mock

from dev.tests._core_helpers import NUMBERED, project, quiet, ref
from dev.tests.test_p5_tools_fill_refs import PopenCounter

MISSING = "f" * 40


class FillRefsFailures(unittest.TestCase):
    def fill(self, L, code=None):
        L.commit()
        return quiet(L.module.fill_refs, L.root, L.con, code)

    def test_fill_refs_commit_not_found(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED, "src/b.py": NUMBERED}, test=self)
        good = ref(L, rid, "src/a.py", 1, 1)
        lost = [ref(L, rid, p, 2, 2, commit_sha=MISSING) for p in ("src/a.py", "src/b.py")]
        asked = []
        real = L.module.BlobReader._object

        def asking(reader, commit, path):
            if path is None:
                asked.append(commit)
            return real(reader, commit, path)

        with mock.patch.object(L.module.BlobReader, "_object", asking), PopenCounter() as pc:
            failed, out = self.fill(L)
        self.assertEqual(failed, 2, out)
        for lid, path in zip(lost, ("src/a.py", "src/b.py")):
            self.assertIn(f"ref {lid} {path}@{MISSING[:10]}: commit {MISSING[:10]} not found", out)
        self.assertNotIn("is not in commit", out)
        self.assertIn("filled 1 references, 2 could not be read from git", out)
        self.assertIsNotNone(L.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (good,)))
        self.assertEqual(sorted(asked), sorted([sha, MISSING]), "each commit is checked once")
        self.assertLessEqual(len(pc.git_calls()), 2, pc.git_calls())

    def test_fill_refs_stops_after_git_timeout(self):
        L, sha, rid, _ = project({f"src/f{k}.py": NUMBERED for k in range(4)}, test=self)
        for k in range(4):
            ref(L, rid, f"src/f{k}.py", 1, 1)
        L.commit()
        started = []
        real = subprocess.Popen

        def hung_git(args, *a, **kw):  # a git that takes the question and never answers
            started.append(args)
            return real([sys.executable, "-c", "import time; time.sleep(60)"], *a, **kw)

        with mock.patch.object(L.module, "BLOB_SECONDS", 0.3), mock.patch.object(subprocess, "Popen", hung_git):
            t0 = time.perf_counter()
            failed, out = self.fill(L)
            took = time.perf_counter() - t0
        self.assertEqual(len(started), 1, "git is not started again after a timeout")
        self.assertEqual(out.count("git is not responding"), 1, out)
        self.assertEqual(failed, 4, out)
        self.assertIn("filled 0 references, 4 could not be read from git", out)
        self.assertLess(took, 5)
        self.assertEqual(L.scalar("SELECT count(*) FROM finding_locations WHERE excerpt IS NOT NULL"), 0)

    def test_fill_refs_second_run_noop(self):
        L, sha, rid, _ = project(test=self)
        ref(L, rid, "src/a.py", 1, 2)
        failed, out = self.fill(L)
        self.assertEqual((failed, "already complete" in out), (0, False), out)
        self.assertIn("filled 1 references", out)
        before = L.one("SELECT * FROM finding_locations")
        with PopenCounter() as pc:
            failed, out = self.fill(L)
        self.assertEqual(failed, 0)
        self.assertEqual(out.strip(), "already complete: filled 0 references, 0 could not be read from git")
        self.assertEqual(pc.git_calls(), [])
        self.assertEqual(L.one("SELECT * FROM finding_locations"), before)


if __name__ == "__main__":
    unittest.main()
