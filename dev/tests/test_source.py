"""source(): the cited lines at their commit, with the git, excerpt and none fallbacks (SRV-T7's four branches)."""
import unittest

from dev.tests._core_helpers import numbered, project, ref

EXCERPT_10_12 = "line 10\nline 11\nline 12"


class Source(unittest.TestCase):
    def make(self, files=None, **kw):
        L, sha, rid, rep = project(files, test=self, **kw)
        self.L, self.sha, self.rid = L, sha, rid
        return L

    def src(self, lid, context=8):
        self.L.commit()
        return self.L.module.source(self.L.root, self.L.con, lid, context=context)

    def test_source_from_git(self):
        L = self.make()
        lid = ref(L, self.rid, "src/a.py", 10, 12)
        s = self.src(lid, context=3)
        self.assertEqual(s["source"], "git")
        self.assertNotIn("git_error", s)
        self.assertEqual([l["n"] for l in s["lines"]], list(range(7, 16)))
        self.assertEqual(s["lines"][0], {"n": 7, "text": "line 7"})
        self.assertEqual(s["git_command"], f"git -C . show {self.sha}:src/a.py | sed -n '10,12p'")
        for k in ("path", "commit_sha", "line_start", "line_end", "symbol", "excerpt", "permalink", "repo_name",
                  "blob_sha"):
            self.assertIn(k, s)
        self.assertEqual(s["repo_name"], "demo")
        # No excerpt saved: no excerpt_matches key at all.
        self.assertNotIn("excerpt_matches", s)
        # Context is clipped to the file at both ends.
        s = self.src(ref(L, self.rid, "src/a.py", 2, 29), context=8)
        self.assertEqual([l["n"] for l in s["lines"]], list(range(1, 31)))
        # context=0 is exactly the cited lines (what `show` prints); line_end NULL means one line.
        s = self.src(ref(L, self.rid, "src/a.py", 5, None), context=0)
        self.assertEqual(s["lines"], [{"n": 5, "text": "line 5"}])
        self.assertEqual(s["git_command"], f"git -C . show {self.sha}:src/a.py | sed -n '5,5p'")

    def test_source_excerpt_matches(self):
        L = self.make()
        same = ref(L, self.rid, "src/a.py", 10, 12, excerpt=EXCERPT_10_12)
        trailing = ref(L, self.rid, "src/a.py", 10, 12, excerpt=EXCERPT_10_12 + "\n")
        other = ref(L, self.rid, "src/a.py", 10, 12, excerpt="line 10\nedited\nline 12")
        self.assertIs(self.src(same)["excerpt_matches"], True)
        self.assertIs(self.src(trailing)["excerpt_matches"], True)  # one trailing newline is ignored
        self.assertIs(self.src(other)["excerpt_matches"], False)
        self.assertEqual(self.src(other)["source"], "git")

    def test_source_falls_back_to_excerpt(self):
        # History rewritten: the reviewed commit is gone, the saved excerpt is shown instead.
        L = self.make()
        lid = ref(L, self.rid, "src/a.py", 10, 12, excerpt=EXCERPT_10_12)
        L.commit()
        L.repo.rewrite_history()
        self.assertFalse(L.repo.has_object(self.sha))
        s = self.src(lid, context=8)
        self.assertEqual(s["source"], "excerpt")
        self.assertTrue(s["git_error"])
        self.assertEqual(s["lines"], [{"n": 10, "text": "line 10"}, {"n": 11, "text": "line 11"},
                                      {"n": 12, "text": "line 12"}])  # no context around an excerpt
        self.assertNotIn("excerpt_matches", s)
        # characterization: an excerpt saved with a trailing newline shows an extra empty numbered line in the
        # fallback (split, not splitlines), while excerpt_matches ignores that newline.
        s = self.src(ref(L, self.rid, "src/a.py", 10, 12, excerpt=EXCERPT_10_12 + "\n"))
        self.assertEqual(s["lines"][-1], {"n": 13, "text": ""})

    def test_source_falls_back_when_repository_removed(self):
        L, sha, rid, rep = project(test=self)
        sub = L.repo.subrepo("sub")
        sub.write("b.py", numbered(5))
        ssha = sub.commit("sub")
        srep = L.repository("sub", "sub")
        L.review_repository(rid, ssha, repository_id=srep)
        fid = L.finding(rid)
        lid = L.location(fid, "b.py", 2, 3, repository_id=srep, excerpt="line 2\nline 3")
        L.commit()
        self.assertEqual(L.module.source(L.root, L.con, lid)["source"], "git")
        sub.remove()
        s = L.module.source(L.root, L.con, lid)
        self.assertEqual(s["source"], "excerpt")
        self.assertTrue(s["git_error"])
        self.assertEqual([l["text"] for l in s["lines"]], ["line 2", "line 3"])
        self.assertTrue(s["git_command"].startswith(f"git -C sub show {ssha}:b.py"))

    def test_source_none(self):
        L = self.make()
        lid = ref(L, self.rid, "src/a.py", 10, 12)
        L.commit()
        L.repo.rewrite_history()
        s = self.src(lid)
        self.assertEqual(s["source"], "none")
        self.assertTrue(s["git_error"])
        self.assertNotIn("lines", s)
        # A path missing at the commit also lands here (git readable, file not).
        L = self.make()
        s = self.src(ref(L, self.rid, "src/missing.py", 1, 1))
        self.assertEqual(s["source"], "none")
        self.assertIn("src/missing.py", s["git_error"])

    def test_source_without_line_start(self):
        L = self.make({"src/a.py": numbered(60), "src/short.py": numbered(5)})
        lid = ref(L, self.rid, "src/a.py", None, None, symbol="something")
        s = self.src(lid, context=8)
        self.assertEqual(s["source"], "git")
        # First 40 lines plus the context.
        self.assertEqual([l["n"] for l in s["lines"]], list(range(1, 49)))
        self.assertEqual(s["git_command"], f"git -C . show {self.sha}:src/a.py")  # no sed without lines
        s = self.src(ref(L, self.rid, "src/short.py", None, None, symbol="x"), context=8)
        self.assertEqual([l["n"] for l in s["lines"]], [1, 2, 3, 4, 5])
        # With an excerpt but no lines, excerpt_matches is not computed.
        s = self.src(ref(L, self.rid, "src/a.py", None, None, symbol="x", excerpt="line 1"))
        self.assertNotIn("excerpt_matches", s)

    def test_source_unknown(self):
        L = self.make()
        self.assertEqual(self.src(424242), {"error": "no such reference"})

    def test_source_fallbacks(self):
        """SRV-T7: git, excerpt, none and mismatch in one place."""
        L = self.make()
        good = ref(L, self.rid, "src/a.py", 10, 12, excerpt=EXCERPT_10_12)
        bad = ref(L, self.rid, "src/a.py", 10, 12, excerpt="x")
        bare = ref(L, self.rid, "src/a.py", 10, 12)
        got = {k: self.src(v) for k, v in (("git", good), ("mismatch", bad))}
        self.assertEqual((got["git"]["source"], got["git"]["excerpt_matches"]), ("git", True))
        self.assertEqual((got["mismatch"]["source"], got["mismatch"]["excerpt_matches"]), ("git", False))
        L.repo.rewrite_history()
        self.assertEqual(self.src(good)["source"], "excerpt")
        self.assertEqual(self.src(bare)["source"], "none")


if __name__ == "__main__":
    unittest.main()
