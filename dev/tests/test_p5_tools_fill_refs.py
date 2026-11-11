"""fill-refs is idempotent and reads blobs through one `git cat-file --batch` per repository (plan P-0001 steps 101
and 102; RES-L1, PERF-M1, PERF-V3, ISS-0067).

test_fill_refs_idempotent_without_template  with no permalink template a second run prints 'filled 0' and starts no git
test_fill_refs_process_count                1,000 references over 50 files start at most 2 git processes, < 0.5 s
                                            (10,000 references, the 10x check, with LEDGER_SLOW_TESTS=1)
test_fill_refs_reads_each_blob_once         many references to one file read the blob once
test_fill_refs_refuses_huge_blob            a blob over MAX_BLOB_BYTES is refused with a clear failure line
test_fill_refs_counts_only_changed_rows     the count is of references whose values changed
"""
import subprocess
import time
import unittest
from unittest import mock

from dev.tests._core_helpers import NUMBERED, numbered, project, quiet, ref
from dev.tests.fixtures import slow


class PopenCounter:
    """Counts the processes the tool starts (subprocess.run goes through subprocess.Popen)."""

    def __init__(self):
        self.calls = []
        self.real = subprocess.Popen

    def __call__(self, args, *a, **kw):
        self.calls.append(list(args) if isinstance(args, (list, tuple)) else [args])
        return self.real(args, *a, **kw)

    def __enter__(self):
        self.patch = mock.patch.object(subprocess, "Popen", self)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def git_calls(self):
        return [c for c in self.calls if c and str(c[0]).endswith("git")]


def many_refs(L, rid, n, files):
    """n references spread over `files` files (each NUMBERED, 30 lines); one finding per reference."""
    rows = []
    for k in range(n):
        f = L.finding(rid, point_id=f"SPEC-M{k + 1}")
        line = k % 30 + 1
        rows.append(L.location(f, f"src/f{k % files:02d}.py", line, line))
    return rows


class FillRefsBatch(unittest.TestCase):
    def fill(self, L, code=None):
        L.commit()
        return quiet(L.module.fill_refs, L.root, L.con, code)

    def test_fill_refs_idempotent_without_template(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED}, permalink_template=None, test=self)
        for a in (1, 3, 5):
            ref(L, rid, "src/a.py", a, a)
        failed, out = self.fill(L)
        self.assertEqual(failed, 0, out)
        self.assertIn("filled 3 references", out)
        with PopenCounter() as pc:
            failed, out = self.fill(L)
        self.assertIn("filled 0 references, 0 could not be read", out)
        self.assertEqual(pc.git_calls(), [])

    def run_scale(self, n, files=50, budget=0.5):
        tree = {f"src/f{k:02d}.py": NUMBERED for k in range(files)}
        L, sha, rid, _ = project(tree, test=self)
        many_refs(L, rid, n, files)
        L.commit()
        with PopenCounter() as pc:
            t0 = time.perf_counter()
            failed, out = quiet(L.module.fill_refs, L.root, L.con)
            took = time.perf_counter() - t0
        self.assertEqual(failed, 0, out[-500:])
        self.assertIn(f"filled {n} references", out)
        self.assertLessEqual(len(pc.git_calls()), 2, pc.git_calls()[:5])
        self.assertLess(took, budget, f"fill-refs of {n} references took {took:.2f} s")
        self.assertEqual(L.scalar("SELECT count(*) FROM finding_locations WHERE excerpt IS NULL OR blob_sha IS NULL "
                                  "OR permalink IS NULL"), 0)
        return took

    def test_fill_refs_process_count(self):
        self.run_scale(1000)

    @slow
    def test_fill_refs_process_count_10x(self):
        self.run_scale(10000, budget=5.0)

    def test_fill_refs_reads_each_blob_once(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED, "src/b.py": numbered(5)}, test=self)
        for a in range(1, 21):
            ref(L, rid, "src/a.py", a, a)
        ref(L, rid, "src/b.py", 2, 3)
        L.commit()
        reads = []
        real = L.module.BlobReader._fetch

        def counting(self, sha_, path):
            reads.append((sha_, path))
            return real(self, sha_, path)

        with mock.patch.object(L.module.BlobReader, "_fetch", counting):
            failed, out = quiet(L.module.fill_refs, L.root, L.con)
        self.assertEqual(failed, 0, out)
        self.assertEqual(sorted(reads), sorted([(sha, "src/a.py"), (sha, "src/b.py")]))
        r = L.one("SELECT blob_sha, excerpt FROM finding_locations WHERE path='src/b.py'")
        self.assertEqual(r["blob_sha"], L.repo.rev_parse(f"{sha}:src/b.py"))
        self.assertEqual(r["excerpt"], "line 2\nline 3")

    def test_fill_refs_refuses_huge_blob(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED, "big.txt": "x" * 5000 + "\n"}, test=self)
        big = ref(L, rid, "big.txt", 1, 1)
        small = ref(L, rid, "src/a.py", 2, 2)
        L.commit()
        with mock.patch.object(L.module, "MAX_BLOB_BYTES", 4096):
            failed, out = quiet(L.module.fill_refs, L.root, L.con)
        self.assertEqual(failed, 1, out)
        self.assertIn(f"ref {big} big.txt@{sha[:10]}: the file is 5,001 bytes, over the 4,096-byte limit; "
                      f"not read", out)
        self.assertIn("filled 1 references, 1 could not be read", out)
        self.assertIsNone(L.one("SELECT excerpt FROM finding_locations WHERE id=?", (big,))["excerpt"])
        self.assertEqual(L.one("SELECT excerpt FROM finding_locations WHERE id=?", (small,))["excerpt"], "line 2")
        # file_at (show, /api/source) has the same limit.
        with mock.patch.object(L.module, "MAX_BLOB_BYTES", 4096):
            with self.assertRaises(RuntimeError) as cm:
                L.module.file_at(L.root, ".", sha, "big.txt")
        self.assertIn("over the 4,096-byte limit", str(cm.exception))

    def test_fill_refs_counts_only_changed_rows(self):
        L, sha, rid, _ = project(test=self)
        lid = ref(L, rid, "src/a.py", 2, 2)
        self.fill(L)
        # Only the permalink is missing now: the row is selected, and only that column changes.
        L.x("UPDATE finding_locations SET permalink=NULL WHERE id=?", (lid,))
        failed, out = self.fill(L)
        self.assertIn("filled 1 references", out)
        failed, out = self.fill(L)
        self.assertIn("filled 0 references", out)

    def test_fill_refs_missing_file_and_not_a_repo(self):
        L, sha, rid, rep = project(test=self)
        gone = ref(L, rid, "src/gone.py", 1, 1)
        tree = ref(L, rid, "src", 1, 1)  # a directory, not a file
        failed, out = self.fill(L)
        self.assertEqual(failed, 2, out)
        self.assertIn(f"ref {gone} src/gone.py@{sha[:10]}: src/gone.py is not in commit {sha[:10]}", out)
        self.assertIn(f"ref {tree} src@{sha[:10]}: src is a tree at {sha[:10]}, not a file", out)


if __name__ == "__main__":
    unittest.main()
