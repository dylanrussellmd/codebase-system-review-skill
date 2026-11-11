"""Path confinement of git reads (plan P-0001 step 36 probed it; steps 45 and 46 fixed it and flipped these tests).

- finding_locations.path ('../../etc/passwd', 'a/../../x', an absolute path) (ISS-0220, step 45): file_at refuses all
  three before git runs, with the one fixed message "bad path", so nothing says whether an outside file exists; the
  schema CHECK refuses such paths on insert (the rows here are planted with ignore_check_constraints, as in a ledger
  edited by hand).
- repositories.path ('../other', an absolute folder) (ISS-0077, step 46): repo_dir refuses a folder outside the
  project root unless the repository is listed explicitly (allow_outside_root, D-0007); file_at, fill-refs and show
  return nothing from it.

Nothing here reads real files outside the scratch folders: the "outside" files are sentinels in a temp folder.
"""
import unittest

from dev.tests import fixtures

SENT = "SENTINEL-OUTSIDE-7f3a"


class PathConfinementTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.m = fixtures.load_ledger_module()
        base = cls.base = fixtures.temp_dir("confine-")
        # proj is base/x/proj, so proj/../../etc/passwd = base/etc/passwd and proj/a/../../x = base/x/x
        (base / "etc").mkdir()
        (base / "etc" / "passwd").write_text(SENT + " etc\n")
        (base / "x").mkdir()
        (base / "x" / "x").write_text(SENT + " ax\n")
        cls.absfile = base / "abs.txt"
        cls.absfile.write_text(SENT + " abs\n")
        cls.repo = fixtures.ScratchRepo(base / "x" / "proj")
        cls.repo.write("src/app.py", "print('hi')\n")
        cls.sha = cls.repo.commit("one")
        cls.other = fixtures.ScratchRepo(base / "x" / "other")  # outside the project root, reached by '../other'
        cls.other.write("secret.txt", SENT + " other-repo\n")
        cls.osha = cls.other.commit("o")
        cls.absrepo = fixtures.ScratchRepo(base / "absrepo")    # outside, reached by an absolute path
        cls.absrepo.write("secret.txt", SENT + " abs-repo\n")
        cls.asha = cls.absrepo.commit("a")
        cls.root = cls.repo.path.resolve()

    def file_at_error(self, path, repo_path=".", sha=None):
        with self.assertRaises(RuntimeError) as cm:
            self.m.file_at(self.root, repo_path, sha or self.sha, path)
        msg = str(cm.exception)
        self.assertNotIn(SENT, msg)
        return msg

    # ------------------------------------------------ finding_locations.path (ISS-0220)

    def test_file_at_parent_path_refused(self):
        # ISS-0220 fixed (step 45): refused by the tool before git runs
        self.assertEqual(self.file_at_error("../../etc/passwd"), "bad path")

    def test_file_at_inner_parent_path_refused(self):
        self.assertEqual(self.file_at_error("a/../../x"), "bad path")

    def test_file_at_absolute_path_reveals_nothing(self):
        # ISS-0220 / FIX-7 fixed (step 45): an existing and a missing outside file give the same fixed error
        self.assertEqual(self.file_at_error(str(self.absfile)), "bad path")
        self.assertEqual(self.file_at_error(str(self.base / "missing.txt")), "bad path")

    # ------------------------------------------------ repositories.path (ISS-0077)

    def test_repo_dir_refuses_outside_project_root(self):
        # ISS-0077 fixed (step 46): '../other' and absolute folders are refused unless listed explicitly
        for rp, target in (("../other", self.other.path), (str(self.absrepo.path), self.absrepo.path)):
            with self.assertRaises(RuntimeError) as cm:
                self.m.repo_dir(self.root, rp)
            self.assertEqual(str(cm.exception), "repository path outside the project root")
            self.assertEqual(self.m.repo_dir(self.root, rp, allow_outside=True), target.resolve())

    def test_file_at_reads_nothing_outside(self):
        for rp, sha in (("../other", self.osha), (str(self.absrepo.path), self.asha)):
            with self.assertRaises(RuntimeError) as cm:
                self.m.file_at(self.root, rp, sha, "secret.txt")
            self.assertNotIn(SENT, str(cm.exception))

    def _ledger_with(self, entries):
        """A ledger in the scratch project with one location per (repo_path, sha, path); returns (ledger, [ids])."""
        led = fixtures.Ledger(self.root)
        led.x("PRAGMA ignore_check_constraints = ON")  # plant rows the schema 2 CHECKs refuse
        rid = led.review()
        ids, repos = [], {}
        for n, (rp, sha, path) in enumerate(entries):
            if rp not in repos:  # one repository per path (schema 3)
                repos[rp] = led.repository(name=f"r{n}", path=rp)
                led.review_repository(rid, sha, repository_id=repos[rp])
            repo_id = repos[rp]
            fid = led.finding(rid)
            ids.append(led.location(fid, path=path, line_start=1, repository_id=repo_id, commit_sha=sha))
        led.commit()
        return led, ids

    def test_fill_refs_and_show(self):
        entries = [(".", self.sha, "../../etc/passwd"), (".", self.sha, "a/../../x"),
                   (".", self.sha, str(self.absfile)),
                   ("../other", self.osha, "secret.txt"), (str(self.absrepo.path), self.asha, "secret.txt")]
        led, ids = self._ledger_with(entries)
        try:
            p = led.run("fill-refs")
            # all five refused: three bad location paths, two repositories outside the root
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("filled 0 references, 5 could not be read from git", p.stdout)
            self.assertNotIn(SENT, p.stdout + p.stderr)
            self.assertEqual(p.stdout.count(": bad path"), 3, p.stdout)
            self.assertEqual(p.stdout.count(": repository path outside the project root"), 2, p.stdout)
            for i in ids:
                self.assertIsNone(led.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (i,)))
                s = led.run("show", i)
                self.assertNotIn(SENT, s.stdout + s.stderr)
                self.assertIn("# git:", s.stdout)
                self.assertEqual(s.returncode, 1)  # nothing to show
        finally:
            led.close()


if __name__ == "__main__":
    unittest.main()
