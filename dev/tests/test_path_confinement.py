"""Path confinement of git reads (plan P-0001 step 36: verification only).

Pins what the tool does today with hostile paths, so the fix steps change these tests on purpose:
- finding_locations.path ('../../etc/passwd', 'a/../../x', an absolute path) passed to file_at -> `git show sha:path`
  (ISS-0220, flipped by step 45): git itself refuses all three and nothing outside the repository comes back, but the
  absolute path's error says whether that file exists on disk.
- repositories.path ('../other', an absolute folder) joined by repo_dir without a containment check (ISS-0077, flipped
  by step 46): git runs in the outside folder, and file_at, fill-refs and show return its content.

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

    def test_current_file_at_parent_path_refused_by_git(self):
        # characterization: ISS-0220 - no path check in the tool; git refuses (exit 128, "outside repository")
        self.assertIn("outside repository", self.file_at_error("../../etc/passwd"))

    def test_current_file_at_inner_parent_path_not_found(self):
        # characterization: ISS-0220 - git treats 'a/../../x' as a tree path that does not exist (exit 128)
        self.assertIn("does not exist", self.file_at_error("a/../../x"))

    def test_current_file_at_absolute_path_reveals_existence(self):
        # characterization: ISS-0220 - git refuses (exit 128) and returns no content, but its message tells whether
        # the absolute path exists on disk, and that message reaches the viewer as source()'s git_error
        self.assertIn("exists on disk", self.file_at_error(str(self.absfile)))
        self.assertNotIn("exists on disk", self.file_at_error(str(self.base / "missing.txt")))

    # ------------------------------------------------ repositories.path (ISS-0077)

    def test_current_repo_dir_leaves_project_root(self):
        # characterization: ISS-0077 - repo_dir resolves '../other' and absolute paths to folders outside the root
        for rp, target in (("../other", self.other.path), (str(self.absrepo.path), self.absrepo.path)):
            d = self.m.repo_dir(self.root, rp)
            self.assertEqual(d, target.resolve())
            self.assertFalse(str(d).startswith(str(self.root) + "/"))

    def test_current_file_at_reads_outside_repository(self):
        # characterization: ISS-0077 - git exits 0 in the outside repository and its content comes back
        for rp, sha in (("../other", self.osha), (str(self.absrepo.path), self.asha)):
            self.assertIn(SENT, self.m.file_at(self.root, rp, sha, "secret.txt"))

    def _ledger_with(self, entries):
        """A ledger in the scratch project with one location per (repo_path, sha, path); returns (ledger, [ids])."""
        led = fixtures.Ledger(self.root)
        rid = led.review()
        ids = []
        for n, (rp, sha, path) in enumerate(entries):
            repo_id = led.repository(name=f"r{n}", path=rp)
            led.review_repository(rid, sha, repository_id=repo_id)
            fid = led.finding(rid)
            ids.append(led.location(fid, path=path, line_start=1, repository_id=repo_id, commit_sha=sha))
        led.commit()
        return led, ids

    def test_current_fill_refs_and_show(self):
        entries = [(".", self.sha, "../../etc/passwd"), (".", self.sha, "a/../../x"),
                   (".", self.sha, str(self.absfile)),
                   ("../other", self.osha, "secret.txt"), (str(self.absrepo.path), self.asha, "secret.txt")]
        led, ids = self._ledger_with(entries)
        try:
            p = led.run("fill-refs")
            # the three bad location paths fail in git; the two outside repositories are read
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("filled 2 references, 3 could not be read from git", p.stdout)
            self.assertNotIn(SENT, p.stdout + p.stderr)
            ex = {i: led.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (i,)) for i in ids}
            for i in ids[:3]:  # characterization: ISS-0220 - git confines location paths; nothing copied
                self.assertIsNone(ex[i])
            for i in ids[3:]:  # characterization: ISS-0077 - outside content copied into the ledger
                self.assertIn(SENT, ex[i])
            for i in ids[:3]:
                s = led.run("show", i)
                self.assertNotIn(SENT, s.stdout + s.stderr)
                self.assertIn("# git:", s.stdout)
            for i in ids[3:]:  # characterization: ISS-0077 - show prints the outside repository's file
                s = led.run("show", i)
                self.assertEqual(s.returncode, 0, s.stderr)
                self.assertIn("(from git)", s.stdout)
                self.assertIn(SENT, s.stdout)
        finally:
            led.close()


if __name__ == "__main__":
    unittest.main()
