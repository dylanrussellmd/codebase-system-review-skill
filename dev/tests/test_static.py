"""Static checks on the sources: no unused imports, and this repository keeps its review data out of git."""
import ast
import shutil
import subprocess
import unittest

from dev.tests import fixtures

ROOT = fixtures.REPO_ROOT
CHECKED = ("dev/core.py", "dev/build.py", "dev/tests/fixtures.py", "dev/tests/seed_demo.py")


def unused_imports(source, filename="<src>"):
    """Names bound by import statements at any level that are never read anywhere in the module."""
    tree = ast.parse(source, filename)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported[a.asname or a.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    exported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            exported |= {e.value for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)}
    return sorted((line, name) for name, line in imported.items() if name not in used | exported)


class Imports(unittest.TestCase):
    def test_no_unused_imports(self):
        for rel in CHECKED:
            with self.subTest(file=rel):
                path = ROOT / rel
                self.assertEqual(unused_imports(path.read_text(encoding="utf-8"), rel), [],
                                 f"{rel}: unused imports (line, name)")

    def test_unused_import_detector(self):
        self.assertEqual(unused_imports("import os, sys\nimport html\nprint(sys.argv)\n"), [(1, "os"), (2, "html")])
        self.assertEqual(unused_imports("import os.path\nos.path.join('a')\n"), [])
        self.assertEqual(unused_imports("from x import y as z\n__all__ = ['z']\n"), [])


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout of this repository")
class RepoHygiene(unittest.TestCase):
    def test_repo_ignores_review_dir(self):
        # The committed .gitignore must ignore the ledger folder by itself (this checkout's .git/info/exclude may
        # also list it, so test the rule in a clean repository).
        repo = fixtures.ScratchRepo(exclude_ledger=False)
        shutil.copyfile(str(ROOT / ".gitignore"), str(repo.path / ".gitignore"))
        for rel in (".code-review/ledger.sqlite", ".code-review/ledger.py", ".code-review/ledger.sql",
                    ".code-review/ledger.sqlite-wal", ".code-review/patches/R-0001.diff"):
            with self.subTest(path=rel):
                p = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(repo.path), env=repo.env)
                self.assertEqual(p.returncode, 0, f"{rel} is not ignored by .gitignore")
        tracked = subprocess.run(["git", "ls-files", ".code-review"], cwd=str(ROOT), capture_output=True,
                                 text=True, env=fixtures.git_env())
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        self.assertEqual(tracked.stdout, "", "files under .code-review/ are tracked; git rm --cached them")


if __name__ == "__main__":
    unittest.main()
