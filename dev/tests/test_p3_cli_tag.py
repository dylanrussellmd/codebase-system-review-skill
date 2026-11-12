"""tag-review hardening (plan P-0001 step 47) and the other exit codes of RES-M2 (show with nothing shown, fill-refs
with lines past the end or a symbol not found).

tag-review checks the review code and each commit hash before calling git, runs `git tag -a -m <msg> -- <tag> <sha>`,
tells "no such tag" apart from other git errors, records each repository's tag right after creating it, and exits 1
when a repository is left untagged or the review lists none. Tags stay local (D-0002): nothing is pushed.
"""
import unittest

from dev.tests._core_helpers import NUMBERED, numbered, project, ref

PUSH_HINT = "(push with: git -C . push origin review/R-0001)"


def tag_names(L, rid):
    return [r["tag_name"] for r in L.all("SELECT tag_name FROM review_repositories WHERE review_id=? "
                                         "ORDER BY repository_id", (rid,))]


class TagReview(unittest.TestCase):

    def test_tag_review_idempotent(self):
        L, sha, rid, _ = project(test=self)
        p1 = L.run("tag-review", "R-0001")
        p2 = L.run("tag-review", "R-0001")
        self.assertEqual((p1.returncode, p2.returncode), (0, 0), p1.stderr + p2.stderr)
        self.assertEqual(p1.stdout, p2.stdout)
        self.assertIn(f"demo: review/R-0001 -> {sha[:10]}  {PUSH_HINT}", p1.stdout)
        self.assertEqual(L.repo.tags(), ["review/R-0001"])
        self.assertEqual(L.repo.rev_parse("review/R-0001^{commit}"), sha)
        self.assertEqual(tag_names(L, rid), ["review/R-0001"])
        self.assertEqual(L.repo.git("remote"), "")  # local only (D-0002)

    test_tag_review_rerun_is_idempotent = test_tag_review_idempotent

    def test_tag_review_conflicting_tag_left_alone(self):
        L, sha, rid, _ = project(test=self)
        other = L.repo.commit("later")
        L.repo.tag("review/R-0001", other, message="someone else's")
        p = L.run("tag-review", "R-0001")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"demo: tag review/R-0001 already points at {other[:10]}; left alone", p.stdout)
        self.assertIn("1 of 1 repositories not tagged", p.stderr)
        self.assertEqual(L.repo.rev_parse("review/R-0001^{commit}"), other)
        self.assertEqual(tag_names(L, rid), [None])

    def test_tag_review_unknown_exit1(self):
        L, *_ = project(test=self)
        p = L.run("tag-review", "R-9999")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no review R-9999", p.stderr)
        for bad in ("../x", "--message=x", "R-1"):
            with self.subTest(code=bad):
                p = L.run("tag-review", "--", bad) if bad.startswith("-") else L.run("tag-review", bad)
                self.assertEqual(p.returncode, 1)
                self.assertIn("is not a review code like R-0001", p.stderr)
                self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(L.repo.tags(), [])

    def test_tag_review_no_repos_reports(self):
        L, *_ = project(test=self)
        L.review("R-0002")
        p = L.run("tag-review", "R-0002")
        self.assertEqual(p.returncode, 1)
        self.assertIn("review R-0002 has no repositories; nothing to tag", p.stderr)

    def test_tag_review_rejects_non_hex_sha(self):
        L, sha, rid, rep = project(test=self)
        L.x("PRAGMA ignore_check_constraints = ON")  # a ledger from before the hex CHECK, or edited by hand
        L.x("UPDATE review_repositories SET commit_sha=? WHERE review_id=?", ("--message=" + "0" * 30, rid))
        L.x("PRAGMA ignore_check_constraints = OFF")
        p = L.run("tag-review", "R-0001")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("is not a full commit hash; not tagged", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(L.repo.tags(), [])
        self.assertEqual(tag_names(L, rid), [None])

    def test_tag_review_partial_failure_records_first_repo(self):
        L, sha, rid, rep = project(test=self)
        sub = L.repo.subrepo("gone")
        sub.write("b.py", numbered(2))
        ssha = sub.commit("sub")
        srep = L.repository("lib", "gone")
        L.review_repository(rid, ssha, repository_id=srep)
        sub.remove()
        p = L.run("tag-review", "R-0001")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("lib: not tagged:", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(L.repo.rev_parse("review/R-0001^{commit}"), sha)
        self.assertEqual(tag_names(L, rid), ["review/R-0001", None])

    def test_multi_repo_parent_tags_and_reads_each_repo(self):
        """A parent folder (not itself the code) with two repos below it: one ledger tags and reads each."""
        from dev.tests import fixtures
        parent = fixtures.temp_dir("multi-") / "system"
        parent.mkdir()
        api = fixtures.ScratchRepo(parent / "api")
        api.write("x.py", "api line\n")
        asha = api.commit("api")
        web = fixtures.ScratchRepo(parent / "web")
        web.write("x.py", "web line\n")
        wsha = web.commit("web")
        L = fixtures.Ledger(parent)
        self.addCleanup(L.close)
        ra, rw = L.repository("api", "api"), L.repository("web", "web")
        rid = L.review("R-0001", commit_sha=asha, repository_id=ra)
        L.review_repository(rid, wsha, repository_id=rw)
        fid = L.finding(rid)
        la = L.location(fid, "x.py", 1, 1, repository_id=ra)
        lw = L.location(fid, "x.py", 1, 1, repository_id=rw)
        p = L.run("tag-review", "R-0001")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(api.rev_parse("review/R-0001^{commit}"), asha)
        self.assertEqual(web.rev_parse("review/R-0001^{commit}"), wsha)
        self.assertEqual(tag_names(L, rid), ["review/R-0001"] * 2)
        self.assertIn("api line", L.run("show", la).stdout)
        self.assertIn("web line", L.run("show", lw).stdout)


class OtherExitCodes(unittest.TestCase):
    """test_failures_exit_nonzero (RES-M2): show with nothing to show, fill-refs past the end or with a symbol that is
    not found exit 1 (tag-review's cases are above; check's are in test_schema_drift)."""

    def test_show_nothing_exits_1(self):
        L, sha, rid, _ = project(test=self)
        lid = ref(L, rid, "src/gone.py", 1, 1)
        p = L.run("show", lid)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("(from none)", p.stdout)
        self.assertIn("nothing to show", p.stderr)

    def test_fill_refs_past_end_and_missing_symbol_fail(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED, "src/b.py": "def alpha():\n    pass\n"}, test=self)
        past = ref(L, rid, "src/a.py", 29, 35)
        nosym = ref(L, rid, "src/b.py", None, None, symbol="def nowhere")
        good = ref(L, rid, "src/a.py", 2, 2)
        p = L.run("fill-refs")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("filled 1 references, 2 could not be read from git", p.stdout)
        self.assertIn(f"ref {past} src/a.py@{sha[:10]}: lines 29-35 are past the end of the file (30 lines)", p.stdout)
        self.assertIn(f"ref {nosym} src/b.py@{sha[:10]}: symbol 'def nowhere' not found", p.stdout)
        for lid in (past, nosym):
            r = L.one("SELECT excerpt, permalink, line_start FROM finding_locations WHERE id=?", (lid,))
            self.assertIsNone(r["excerpt"])
            self.assertIsNone(r["permalink"])
        self.assertEqual(L.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (good,)), "line 2")


if __name__ == "__main__":
    unittest.main()
