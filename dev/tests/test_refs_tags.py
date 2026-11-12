"""fill-refs (excerpt, sha256, blob, permalink, symbol lookup, review scoping) and tag-review (annotated tags, sub-repos,
and durability of the reviewed lines after squash-merge, branch deletion and gc). Review tags stay local (D-0002)."""
import hashlib
import unittest

from dev.tests._core_helpers import NUMBERED, PERMALINK, numbered, project, quiet, ref

SRC = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"


def loc_row(L, lid):
    return L.one("SELECT * FROM finding_locations WHERE id=?", (lid,))


class FillRefs(unittest.TestCase):
    def fill(self, L, code=None):
        L.commit()
        failed, out = quiet(L.module.fill_refs, L.root, L.con, code)
        return failed, out

    def test_fill_refs_fills_all_fields(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED, "src/b.py": SRC}, test=self)
        lined = ref(L, rid, "src/a.py", 3, 5)
        single = ref(L, rid, "src/a.py", 7, None)
        by_symbol = ref(L, rid, "src/b.py", None, None, symbol="def beta")
        keep = ref(L, rid, "src/a.py", 1, 2, excerpt="saved by hand")
        failed, out = self.fill(L)
        self.assertEqual(failed, 0)
        self.assertIn("filled 4 references, 0 could not be read from git", out)

        r = loc_row(L, lined)
        self.assertEqual(r["excerpt"], "line 3\nline 4\nline 5")
        self.assertEqual(r["excerpt_sha256"], hashlib.sha256(r["excerpt"].encode()).hexdigest())
        self.assertEqual(r["blob_sha"], L.repo.rev_parse(f"{sha}:src/a.py"))
        self.assertEqual(r["permalink"], f"https://example.invalid/blob/{sha}/src/a.py#L3-L5")
        r = loc_row(L, single)
        self.assertEqual((r["line_start"], r["line_end"], r["excerpt"]), (7, 7, "line 7"))
        # A symbol-only reference gets the first line containing the symbol.
        r = loc_row(L, by_symbol)
        self.assertEqual((r["line_start"], r["line_end"], r["excerpt"]), (4, 4, "def beta():"))
        self.assertTrue(r["permalink"].endswith("src/b.py#L4-L4"))
        # An existing excerpt is never overwritten (and its hash is not filled either).
        r = loc_row(L, keep)
        self.assertEqual(r["excerpt"], "saved by hand")
        self.assertIsNone(r["excerpt_sha256"])
        self.assertIsNotNone(r["blob_sha"])
        # Second run fills nothing.
        failed, out = self.fill(L)
        self.assertIn("filled 0 references, 0 could not be read", out)

    def test_fill_refs_edge_cases(self):
        L, sha, rid, _ = project({"src/a.py": numbered(5), "src/b.py": SRC}, test=self)
        missing_symbol = ref(L, rid, "src/b.py", None, None, symbol="def gamma")
        past_end = ref(L, rid, "src/a.py", 8, 9)
        unreadable = ref(L, rid, "src/gone.py", 1, 1)
        failed, out = self.fill(L)
        # TEST-T53 (ISS-0283) fixed with RES-M2 (step 47): a symbol that is not found and lines past the end of
        # the file are failures; nothing is written for them (no permalink to #L1-L1, no empty excerpt).
        self.assertEqual(failed, 3)
        self.assertIn("filled 0 references, 3 could not be read from git", out)
        self.assertIn(f"ref {unreadable} src/gone.py@{sha[:10]}:", out)
        self.assertIn(f"ref {missing_symbol} src/b.py@{sha[:10]}: symbol 'def gamma' not found", out)
        self.assertIn(f"ref {past_end} src/a.py@{sha[:10]}: lines 8-9 are past the end of the file (5 lines)", out)
        for lid in (unreadable, missing_symbol, past_end):
            r = loc_row(L, lid)
            self.assertEqual((r["excerpt"], r["permalink"], r["blob_sha"]), (None, None, None))
        L.x("UPDATE review_repositories SET tag_name='review/R-0001'")
        problems = L.all("SELECT problem FROM v_untraceable_refs WHERE path='src/a.py'")
        self.assertEqual(problems, [{"problem": "no excerpt"}])

        # They are selected again each run, and fail again.
        failed, out = self.fill(L)
        self.assertIn("filled 0 references, 3 could not be read", out)

    def test_fill_refs_permalink_template(self):
        tmpl = "https://h.invalid/{sha}/{path}?a={start}&b={end}&again={sha}"
        L, sha, rid, rep = project(permalink_template=tmpl, test=self)
        lid = ref(L, rid, "src/a.py", 2, 4)
        self.fill(L)
        # Every placeholder is substituted, each occurrence of it.
        self.assertEqual(loc_row(L, lid)["permalink"], f"https://h.invalid/{sha}/src/a.py?a=2&b=4&again={sha}")
        # No template: no permalink, other fields still filled.
        L2, sha2, rid2, _ = project(permalink_template=None, test=self)
        lid2 = ref(L2, rid2, "src/a.py", 2, 2)
        self.fill(L2)
        r = loc_row(L2, lid2)
        self.assertIsNone(r["permalink"])
        self.assertEqual(r["excerpt"], "line 2")
        # An existing permalink is kept.
        lid3 = ref(L, rid, "src/a.py", 1, 1, permalink="https://kept.invalid/")
        self.fill(L)
        self.assertEqual(loc_row(L, lid3)["permalink"], "https://kept.invalid/")

    def test_fill_refs_scoped_to_review(self):
        L, sha, r1, rep = project(test=self)
        r2 = L.review("R-0002", commit_sha=sha, repository_id=rep)
        a = ref(L, r1, "src/a.py", 1, 1)
        b = ref(L, r2, "src/a.py", 2, 2)
        p = L.run_ok("fill-refs", "R-9999")  # unknown code: nothing filled, exit 0
        self.assertIn("filled 0 references, 0 could not be read", p.stdout)
        p = L.run_ok("fill-refs", "R-0001")
        self.assertIn("filled 1 references", p.stdout)
        self.assertEqual(loc_row(L, a)["excerpt"], "line 1")
        rb = loc_row(L, b)
        self.assertEqual((rb["excerpt"], rb["blob_sha"], rb["permalink"]), (None, None, None))
        p = L.run_ok("fill-refs")
        self.assertIn("filled 1 references", p.stdout)
        self.assertEqual(loc_row(L, b)["excerpt"], "line 2")

    def test_cli_fill_refs_exit_code(self):
        L, sha, rid, _ = project(test=self)
        ref(L, rid, "src/gone.py", 1, 1)
        p = L.run("fill-refs")
        self.assertEqual(p.returncode, 1)
        self.assertIn("1 could not be read from git", p.stdout)


class TagReview(unittest.TestCase):
    def test_tag_review_creates_annotated_tag(self):
        L, sha, rid, _ = project(test=self)
        p = L.run_ok("tag-review", "R-0001")
        self.assertIn(f"demo: review/R-0001 -> {sha[:10]}", p.stdout)
        self.assertIn("push with: git -C . push origin review/R-0001", p.stdout)
        self.assertEqual(L.repo.git("cat-file", "-t", "review/R-0001"), "tag")  # annotated
        self.assertEqual(L.repo.rev_parse("review/R-0001^{commit}"), sha)
        self.assertEqual(L.repo.git("tag", "-l", "--format=%(contents:subject)", "review/R-0001"),
                         "Commit reviewed in R-0001")
        self.assertEqual(L.scalar("SELECT tag_name FROM review_repositories WHERE review_id=?", (rid,)),
                         "review/R-0001")
        # Tags stay local (D-0002): nothing is pushed; there is no remote at all here.
        self.assertEqual(L.repo.git("remote"), "")
        # Run again: same tag object, same output.
        tag_obj = L.repo.rev_parse("review/R-0001")
        p2 = L.run_ok("tag-review", "R-0001")
        self.assertEqual(p2.stdout, p.stdout)
        self.assertEqual(L.repo.rev_parse("review/R-0001"), tag_obj)
        self.assertEqual(L.repo.tags(), ["review/R-0001"])

    def test_tag_review_leaves_foreign_tag_alone(self):
        L, sha, rid, _ = project(test=self)
        other = L.repo.commit("later")
        L.repo.tag("review/R-0001", other, message="someone else's")
        p = L.run("tag-review", "R-0001")
        self.assertEqual(p.returncode, 1)  # step 47: a repository left untagged is a failure
        self.assertIn(f"demo: tag review/R-0001 already points at {other[:10]}; left alone", p.stdout)
        self.assertEqual(L.repo.rev_parse("review/R-0001^{commit}"), other)
        self.assertIsNone(L.scalar("SELECT tag_name FROM review_repositories WHERE review_id=?", (rid,)))

    def test_tag_review_unknown_and_empty(self):
        L, sha, rid, _ = project(test=self)
        p = L.run("tag-review", "R-9999")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no review R-9999", p.stderr)
        L.review("R-0002")  # no review_repositories rows
        p = L.run("tag-review", "R-0002")
        # TEST-T11 (ISS-0284) fixed by step 47: a review with no repositories says so and exits 1.
        self.assertEqual(p.returncode, 1)
        self.assertIn("review R-0002 has no repositories; nothing to tag", p.stderr)

    def test_tag_review_subrepo(self):
        L, sha, rid, _ = project(test=self)
        sub = L.repo.subrepo("sub")
        sub.write("b.py", numbered(3))
        ssha = sub.commit("sub")
        srep = L.repository("lib", "sub")
        L.review_repository(rid, ssha, repository_id=srep)
        p = L.run_ok("tag-review", "R-0001")
        self.assertIn(f"lib: review/R-0001 -> {ssha[:10]}  (push with: git -C sub push origin review/R-0001)",
                      p.stdout)
        self.assertEqual(sub.rev_parse("review/R-0001^{commit}"), ssha)
        self.assertEqual(L.repo.rev_parse("review/R-0001^{commit}"), sha)
        self.assertEqual(L.all("SELECT tag_name FROM review_repositories WHERE review_id=? ORDER BY repository_id",
                               (rid,)), [{"tag_name": "review/R-0001"}] * 2)

    def test_tag_keeps_commit_after_squash_and_branch_delete(self):
        L, base, _, rep = project(test=self)
        repo = L.repo
        repo.git("checkout", "-q", "-b", "feature")
        repo.write("src/a.py", NUMBERED.replace("line 10\n", "line 10 feature\n"))
        reviewed = repo.commit("feature work")
        repo.write("src/c.py", "untagged\n")
        untagged = repo.commit("more feature work")  # control: not tagged, should be pruned
        rid = L.review("R-0002", commit_sha=reviewed, repository_id=rep)
        lid = ref(L, rid, "src/a.py", 10, 10)
        L.run_ok("fill-refs", "R-0002")
        L.run_ok("tag-review", "R-0002")
        repo.git("checkout", "-q", "main")
        repo.git("merge", "-q", "--squash", "feature")
        repo.commit("squash feature")
        repo.git("branch", "-D", "feature")
        repo.git("reflog", "expire", "--expire=now", "--all")
        repo.git("gc", "-q", "--prune=now")
        self.assertFalse(repo.has_object(untagged), "control: an untagged branch commit should be gone after gc")
        self.assertTrue(repo.has_object(reviewed))
        L.commit()
        s = L.module.source(L.root, L.con, lid, context=0)
        self.assertEqual(s["source"], "git")
        self.assertEqual(s["lines"], [{"n": 10, "text": "line 10 feature"}])
        p = L.run_ok("show", lid)
        self.assertIn("(from git)", p.stdout)
        self.assertIn("    10  line 10 feature", p.stdout)


if __name__ == "__main__":
    unittest.main()
