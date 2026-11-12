"""Root detection (plan P-0001 step 40): which project, and so which ledger, a command works on.

Inside git: the nearest ledger upward, but not past the repository top; a git worktree uses the main checkout's
ledger; a ledger above the repository top only when its repositories table lists that repository; otherwise the
repository top. Outside git (or when git cannot run): the nearest ledger upward, and init without --root refuses
rather than creating a ledger in some subfolder. The project's own copy (.code-review/ledger.py) finds its ledger from
any folder. Any --root given, '.' included, is used as given. A ledger owned by another user is refused.
"""
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from dev.tests import fixtures
from dev.tests.fixtures import ScratchRepo, run_ledger

NOGIT_PATH = "/nonexistent-bin"  # PATH with no git at all (the tool runs from sys.executable's absolute path)


def make_ledger(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    p = run_ledger("--root", root, "init")
    assert p.returncode == 0, p.stdout + p.stderr
    return root / ".code-review" / "ledger.sqlite"


def add_repo(db, path):
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO repositories(name, path) VALUES (?, ?)", (f"r{path}", path))
    con.commit()
    con.close()


def root_of(cwd, *args, env=None):
    """The project root `init` reports when run in cwd, plus the process (init prints the root first)."""
    p = run_ledger(*args, "init", cwd=cwd, env=env)
    for line in p.stdout.splitlines():
        if line.startswith("project root: "):
            return Path(line[len("project root: "):]), p
    return None, p


class FindRoot(unittest.TestCase):

    def setUp(self):
        self.m = fixtures.load_ledger_module()
        self.base = fixtures.temp_dir("root-").resolve()

    def find(self, start, **kw):
        return self.m.find_root(Path(start).resolve(), **kw)

    def test_find_root_prefers_existing_ledger(self):
        repo = ScratchRepo(self.base / "repo")
        (repo.path / "a" / "b").mkdir(parents=True)
        make_ledger(repo.path)
        self.assertEqual(self.find(repo.path / "a" / "b"), repo.path.resolve())

    def test_find_root_git_toplevel(self):
        repo = ScratchRepo(self.base / "repo")
        (repo.path / "a" / "b").mkdir(parents=True)
        self.assertEqual(self.find(repo.path / "a" / "b"), repo.path.resolve())
        self.assertEqual(self.m.resolve_root((repo.path / "a").resolve())[1], "git")

    def test_find_root_nested_unlisted_repo_is_its_own_project(self):
        make_ledger(self.base / "parent")
        inner = ScratchRepo(self.base / "parent" / "work" / "inner")
        (inner.path / "src").mkdir()
        self.assertEqual(self.find(inner.path / "src"), inner.path.resolve())

    test_find_root_ignores_parent_ledger_outside_repo = test_find_root_nested_unlisted_repo_is_its_own_project

    def test_find_root_parent_ledger_listing_repo_is_used(self):
        db = make_ledger(self.base / "system")
        api = ScratchRepo(self.base / "system" / "api")
        (api.path / "src").mkdir()
        add_repo(db, "api")
        self.assertEqual(self.find(api.path / "src"), (self.base / "system").resolve())

    test_find_root_parent_ledger_listed_repo = test_find_root_parent_ledger_listing_repo_is_used

    def test_find_root_refuses_foreign_owned_ledger(self):
        repo = ScratchRepo(self.base / "repo")
        db = make_ledger(repo.path)
        real_stat = os.stat
        me = os.getuid()

        def fake_stat(p, *a, **kw):
            st = real_stat(p, *a, **kw)
            if str(p) == str(db):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, me + 1, st.st_gid,
                                       st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            return st

        with mock.patch.object(self.m.os, "stat", fake_stat), self.assertRaises(SystemExit) as cm:
            self.find(repo.path)
        self.assertIn("belongs to another user", str(cm.exception.code))
        self.assertIn("--root", str(cm.exception.code))
        os.chmod(str(db.parent), 0o777)
        try:
            with self.assertRaises(SystemExit) as cm:
                self.find(repo.path)
            self.assertIn("can be changed by every user", str(cm.exception.code))
        finally:
            os.chmod(str(db.parent), 0o700)
        # An explicit --root is the owner's own choice: no check.
        self.assertEqual(self.find(repo.path, explicit=True), repo.path.resolve())

    test_find_root_rejects_foreign_owner = test_find_root_refuses_foreign_owned_ledger

    def test_find_root_non_git(self):
        plain = self.base / "plain"
        make_ledger(plain)
        (plain / "sub" / "deeper").mkdir(parents=True)
        self.assertEqual(self.find(plain / "sub" / "deeper"), plain.resolve())
        # No ledger anywhere above and no git: init without --root refuses and creates nothing.
        lone = self.base / "lone" / "sub"
        lone.mkdir(parents=True)
        root, p = root_of(lone)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("is not in a git repository and no folder above it has a ledger; pass --root", p.stderr)
        self.assertFalse((lone / ".code-review").exists())
        self.assertEqual(root_of(lone, "--root", lone)[1].returncode, 0)

    def test_find_root_explicit(self):
        repo = ScratchRepo(self.base / "repo")
        sub = repo.path / "sub"
        sub.mkdir()
        self.assertEqual(self.find(sub, explicit=True), sub.resolve())
        make_ledger(repo.path)
        self.assertEqual(self.find(sub), repo.path.resolve())
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(self.find(plain), plain.resolve())
        with mock.patch.dict(os.environ, {"PATH": NOGIT_PATH}):
            self.assertEqual(self.find(plain), plain.resolve())

    def test_find_root_without_git_binary(self):
        repo = ScratchRepo(self.base / "repo")
        sub = repo.path / "sub"
        sub.mkdir()
        env = fixtures.git_env({"PATH": NOGIT_PATH})
        root, p = root_of(sub, env=env)
        self.assertEqual(root, sub.resolve())
        self.assertIn(f"note: git not available; using {sub.resolve()} as the project root", p.stderr)
        self.assertEqual(p.returncode, 1)  # and init will not create a ledger there without --root
        self.assertFalse((sub / ".code-review").exists())
        # A ledger above is still found without git.
        make_ledger(repo.path)
        root, p = root_of(sub, env=env)
        self.assertEqual(root, repo.path.resolve())
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_init_creates_ledger_at_git_top_from_subfolder(self):
        repo = ScratchRepo(self.base / "myrepo")
        deep = repo.path / "a" / "b"
        deep.mkdir(parents=True)
        root, p = root_of(deep)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        db = repo.path.resolve() / ".code-review" / "ledger.sqlite"
        self.assertEqual(root, repo.path.resolve())
        self.assertIn(f"ledger at {db} (created)", p.stdout)
        self.assertFalse((deep / ".code-review").exists())
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='project_name'").fetchone()[0], "myrepo")
        con.close()

    def test_root_explicit(self):
        plain = self.base / "nogit"
        plain.mkdir()
        root, p = root_of(self.base, "--root", plain)
        self.assertEqual((root, p.returncode), (plain.resolve(), 0), p.stderr)
        self.assertTrue((plain / ".code-review" / "ledger.sqlite").exists())

    def test_root_dot_is_explicit(self):
        repo = ScratchRepo(self.base / "repo")
        src = repo.path / "src"
        src.mkdir()
        root, p = root_of(src, "--root", ".")
        self.assertEqual(root, src.resolve())
        self.assertTrue((src / ".code-review" / "ledger.sqlite").exists())

    def test_root_dot_vs_dot_slash_consistent(self):
        repo = ScratchRepo(self.base / "repo")
        src = repo.path / "src"
        src.mkdir()
        self.assertEqual(self.m.find_root(src.resolve(), explicit=True), src.resolve())
        a, _ = root_of(src, "--root", ".")
        b, _ = root_of(src, "--root", "./")
        self.assertEqual(a, b)

    def test_project_copy_finds_its_own_ledger_from_any_cwd(self):
        repo = ScratchRepo(self.base / "repo")
        db = make_ledger(repo.path)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        p = run_ledger("check", cwd=elsewhere, ledger_py=db.parent / "ledger.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = run_ledger("init", cwd=elsewhere, ledger_py=db.parent / "ledger.py")
        self.assertIn(f"project root: {repo.path.resolve()}", p.stdout)
        self.assertFalse((elsewhere / ".code-review").exists())

    def test_worktree_uses_main_checkout_ledger(self):
        repo = ScratchRepo(self.base / "main")
        repo.write("a.py", "x\n")
        repo.commit("one")
        make_ledger(repo.path)
        wt = self.base / "wt"
        repo.git("worktree", "add", "-q", "-b", "side", str(wt))
        (wt / "deep").mkdir()
        self.assertEqual(self.find(wt / "deep"), repo.path.resolve())
        shutil.rmtree(str(wt))


if __name__ == "__main__":
    unittest.main()
