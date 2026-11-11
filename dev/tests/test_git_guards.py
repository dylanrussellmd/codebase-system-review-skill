"""file_at's option and hash guards, as they are today (plan P-0001 step 6; TEST-T44, SEC-T9).

These pin behaviour that is already right, so the path checks and quoting work in a later step cannot loosen it
without a test failing. Run from the repository root:  python3 -m unittest dev.tests.test_git_guards -v
"""
import os
import unittest

from dev.tests import fixtures

TEXT = "one\ntwo\nthree\n"


class FileAtGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = fixtures.load_ledger_module()
        cls.repo = fixtures.ScratchRepo()
        cls.repo.write("src/a.py", TEXT)
        cls.repo.write("-x", "a file whose name starts with a dash\n")
        cls.sha = cls.repo.commit("files")
        cls.root = cls.repo.path

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def err(self, sha, path):
        with self.assertRaises(RuntimeError) as cm:
            self.m.file_at(self.root, None, sha, path)
        return str(cm.exception)

    def test_file_at_rejects_option_like_path(self):
        # Non-hex commit names (refs, options, uppercase) never reach git.
        for bad in ("HEAD", "main", "--output=x", "-p", self.sha.upper(), self.sha[:6], self.sha + "0" * 30):
            with self.subTest(sha=bad):
                self.assertEqual(self.err(bad, "src/a.py"), "commit is not a hex hash")
        # Paths that git could read as an option, or that hold NUL, are refused before git runs.
        for bad in ("--output=x", "-x", "-", "--", "src/a.py\x00--output=x", "\x00"):
            with self.subTest(path=bad):
                self.assertEqual(self.err(self.sha, bad), "bad path")
        # A refused --output=x left no file behind.
        self.assertFalse(os.path.exists(self.root / "x"))
        # A valid sha:path reads the file at that commit (short and full sha).
        self.assertEqual(self.m.file_at(self.root, None, self.sha, "src/a.py"), TEXT)
        self.assertEqual(self.m.file_at(self.root, ".", self.sha[:7], "src/a.py"), TEXT)

    def test_file_at_rejects_option_paths(self):
        """The two cases named in SEC-T9."""
        self.assertEqual(self.err(self.sha, "--output=x"), "bad path")
        self.assertEqual(self.err("HEAD", "src/a.py"), "commit is not a hex hash")

    def test_current_dash_file_is_unreadable(self):
        # characterization: a tracked file whose name starts with '-' cannot be cited at all (the guard refuses the
        # path even though git show sha:-x would be safe, since the colon form is one argument). A later step may
        # choose to allow it with './' or '--' quoting; then change this test on purpose.
        self.assertEqual(self.err(self.sha, "-x"), "bad path")

    def test_current_parent_path_reaches_git(self):
        # characterization: '..' segments are not refused by file_at; git itself rejects a path outside the
        # repository ("outside repository"), so nothing is read. The path CHECKs step should refuse it up front.
        msg = self.err(self.sha, "../outside.txt")
        self.assertNotEqual(msg, "bad path")
        self.assertTrue(msg)


if __name__ == "__main__":
    unittest.main()
