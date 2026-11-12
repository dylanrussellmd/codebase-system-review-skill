"""Shell-safe git commands and path confinement (plan P-0001 steps 45 and 46).

- v_code_refs.git_command (also what ledger.py show, /api/source and the viewer's Copy button use) quotes every
  argument for a POSIX shell, so a cited path like a$(touch X)'b;c is just a file name.
- file_at refuses absolute paths, backslashes, leading - and .. segments itself, with one fixed "bad path" message,
  so nothing tells whether a file outside the repository exists.
- repo_dir keeps repositories under the project root unless the repository is listed explicitly as outside it
  (repositories.allow_outside_root = 1, D-0007).
"""
import os
import subprocess
import unittest

from dev.tests import fixtures
from dev.tests._core_helpers import quiet
from dev.tests.fixtures import Ledger, ScratchRepo

EVIL = "a$(touch PWNED)'b;c"


class GitCommand(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo = ScratchRepo(fixtures.temp_dir("shell-") / "proj")
        cls.repo.write(EVIL, "one\ntwo\nthree\n")
        cls.repo.write("src/a.py", "x = 1\ny = 2\n")
        cls.sha = cls.repo.commit("evil name")
        cls.sub = cls.repo.subrepo("sub dir")
        cls.sub.write("b.py", "b1\nb2\n")
        cls.ssha = cls.sub.commit("sub")
        cls.L = Ledger(cls.repo)
        L = cls.L
        cls.rep_empty = L.repository("root-empty", "")
        cls.rep_sub = L.repository("lib", "sub dir")
        rid = L.review("R-0001", commit_sha=cls.sha, repository_id=cls.rep_empty)
        L.review_repository(rid, cls.ssha, repository_id=cls.rep_sub)
        fid = L.finding(rid)
        cls.evil = L.location(fid, EVIL, 2, 3, repository_id=cls.rep_empty)
        cls.plain = L.location(fid, "src/a.py", 1, 1, repository_id=cls.rep_empty)
        cls.subloc = L.location(fid, "b.py", 2, None, repository_id=cls.rep_sub)
        L.commit()

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def view_command(self, lid):
        return self.L.scalar("SELECT git_command FROM v_code_refs WHERE location_id=?", (lid,))

    def api_command(self, lid):
        return self.L.module.source(self.L.root, self.L.con, lid)["git_command"]

    def run_sh(self, cmd):
        return subprocess.run(["sh", "-c", cmd], cwd=str(self.L.root), capture_output=True, text=True,
                              env=fixtures.git_env(), timeout=30)

    def test_git_command_is_shell_safe(self):
        for label, cmd in (("view", self.view_command(self.evil)), ("api", self.api_command(self.evil))):
            with self.subTest(label):
                p = self.run_sh(cmd)
                self.assertFalse((self.L.root / "PWNED").exists(), f"{label}: the command ran $(touch PWNED)")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stdout, "two\nthree\n")

    def test_git_command_same_in_view_and_api(self):
        for lid in (self.evil, self.plain, self.subloc):
            with self.subTest(lid=lid):
                self.assertEqual(self.api_command(lid), self.view_command(lid))
        # An empty repository path means the root: '.' (the view and source() used to differ here).
        self.assertEqual(self.view_command(self.plain), f"git -C '.' show '{self.sha}:src/a.py' | sed -n '1,1p'")
        cmd = self.view_command(self.subloc)
        self.assertEqual(cmd, f"git -C 'sub dir' show '{self.ssha}:b.py' | sed -n '2,2p'")
        self.assertEqual(self.run_sh(cmd).stdout, "b2\n")

    def test_show_prints_the_evil_file(self):
        p = self.L.run("show", self.evil)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("(from git)", p.stdout)
        self.assertIn("two", p.stdout)


class FileAtPaths(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = fixtures.load_ledger_module()
        base = cls.base = fixtures.temp_dir("fileat-")
        cls.repo = ScratchRepo(base / "proj")
        cls.repo.write("src/a.py", "a\n")
        cls.sha = cls.repo.commit("one")
        cls.outside = base / "outside.txt"
        cls.outside.write_text("outside\n")

    def err(self, path):
        with self.assertRaises(RuntimeError) as cm:
            self.m.file_at(self.repo.path, ".", self.sha, path)
        return str(cm.exception)

    def test_file_at_refuses_bad_paths_with_one_message(self):
        for bad in (str(self.outside), str(self.base / "missing.txt"), "../outside.txt", "src/../../outside.txt",
                    "..", "src/..", "a\\b", "-p", "a\x00b", "a\x01b", ""):
            with self.subTest(path=bad):
                self.assertEqual(self.err(bad), "bad path")
        self.assertEqual(self.m.file_at(self.repo.path, ".", self.sha, "src/a.py"), "a\n")
        self.assertEqual(self.m.file_at(self.repo.path, ".", self.sha, "./src/a.py"), "a\n")


class RepoDirConfinement(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = fixtures.load_ledger_module()
        cls.base = fixtures.temp_dir("repodir-")
        cls.root = cls.base / "proj"
        (cls.root / "sub" / "repo").mkdir(parents=True)
        (cls.base / "x").mkdir()
        os.symlink(str(cls.base / "x"), str(cls.root / "link"))

    def test_repo_dir(self):
        root = self.root.resolve()
        for path, want in ((None, root), ("", root), (".", root), ("sub/repo", root / "sub" / "repo"),
                           ("sub/../sub", root / "sub")):
            with self.subTest(path=path):
                self.assertEqual(self.m.repo_dir(self.root, path), want)

    def test_repo_dir_stays_under_root(self):
        for path in ("/tmp/x", "../x", str(self.base / "x"), "sub/../../x", "link"):
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError) as cm:
                    self.m.repo_dir(self.root, path)
                self.assertEqual(str(cm.exception), "repository path outside the project root")
        # Listed explicitly as outside the root (D-0007): used as given.
        self.assertEqual(self.m.repo_dir(self.root, "../x", allow_outside=True), (self.base / "x").resolve())

    def test_fill_refs_and_show_refuse_outside_repository(self):
        base = fixtures.temp_dir("outside-")
        proj = ScratchRepo(base / "proj")
        proj.write("a.py", "inside\n")
        proj.commit("p")
        other = ScratchRepo(base / "other")
        other.write("s.txt", "SECRET-OUTSIDE\n")
        osha = other.commit("o")
        L = Ledger(proj)
        self.addCleanup(L.close)
        L.x("PRAGMA ignore_check_constraints = ON")  # a ledger written before the path CHECK, or by hand
        rep = L.repository("other", "../other")
        L.x("PRAGMA ignore_check_constraints = OFF")
        # the same checkout listed explicitly (one repository per path in schema 3, so spelled with a trailing /)
        listed = L.repository("sibling", "../other/", allow_outside_root=1)
        rid = L.review("R-0001", commit_sha=osha, repository_id=rep)
        L.review_repository(rid, osha, repository_id=listed)
        fid = L.finding(rid)
        refused = L.location(fid, "s.txt", 1, 1, repository_id=rep)
        allowed = L.location(fid, "s.txt", 1, 1, repository_id=listed)
        p = L.run("fill-refs")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("filled 1 references, 1 could not be read from git", p.stdout)
        self.assertIn(f"ref {refused} s.txt@{osha[:10]}: repository path outside the project root", p.stdout)
        row = L.one("SELECT excerpt, blob_sha, permalink FROM finding_locations WHERE id=?", (refused,))
        self.assertEqual(dict(row), {"excerpt": None, "blob_sha": None, "permalink": None})
        self.assertEqual(L.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (allowed,)), "SECRET-OUTSIDE")
        s = L.run("show", refused)
        self.assertNotIn("SECRET-OUTSIDE", s.stdout + s.stderr)
        self.assertIn("# git: repository path outside the project root", s.stdout)
        src, _ = quiet(L.module.source, L.root, L.con, refused)
        self.assertEqual(src["git_error"], "repository path outside the project root")
        self.assertNotIn("lines", src)


if __name__ == "__main__":
    unittest.main()
