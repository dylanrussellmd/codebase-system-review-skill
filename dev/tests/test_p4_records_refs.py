"""fill-refs finds a symbol by its definition, refuses ambiguous symbols and past-the-end lines, supports whole-file
references and marks binary files (plan P-0001 step 81; ISS-0022, ISS-0040, ISS-0074, ISS-0213); `check` verifies
the reviewed commits and review tags in git."""
import hashlib
import unittest

from dev.tests._core_helpers import NUMBERED, numbered, project, quiet, ref

DEFS = "x = 1\ntab = 'a'  # mentions a\n\ndef a():\n    return tab\n"


def loc_row(L, lid):
    return L.one("SELECT * FROM finding_locations WHERE id=?", (lid,))


def untraceable(L, path):
    L.x("UPDATE review_repositories SET tag_name='review/R-0001'")
    return [r["problem"] for r in L.all("SELECT problem FROM v_untraceable_refs WHERE path=?", (path,))]


class FillRefsP4(unittest.TestCase):
    def fill(self, L, code=None):
        L.commit()
        return quiet(L.module.fill_refs, L.root, L.con, code)

    def test_fill_refs_symbol_matches_definition_not_substring(self):
        L, sha, rid, _ = project({"src/d.py": DEFS}, test=self)
        lid = ref(L, rid, "src/d.py", None, None, symbol="a")
        failed, out = self.fill(L)
        self.assertEqual(failed, 0, out)
        r = loc_row(L, lid)
        self.assertEqual((r["line_start"], r["line_end"], r["excerpt"]), (4, 4, "def a():"))

    def test_fill_refs_symbol_prefers_definition(self):
        src = "# beta is called from main\ndef alpha():\n    beta()\n\ndef beta():\n    pass\n"
        L, sha, rid, _ = project({"src/b.py": src}, test=self)
        lid = ref(L, rid, "src/b.py", None, None, symbol="beta")
        alpha = ref(L, rid, "src/b.py", None, None, symbol="alpha")
        self.fill(L)
        self.assertEqual(loc_row(L, lid)["line_start"], 5)
        self.assertEqual(loc_row(L, alpha)["line_start"], 2)

    def test_fill_refs_symbol_resolution(self):
        src = "-- v_x lists things\nCREATE VIEW v_x AS SELECT 1;\nSELECT * FROM v_x;\n"
        L, sha, rid, _ = project({"s.sql": src}, test=self)
        lid = ref(L, rid, "s.sql", None, None, symbol="v_x")
        self.fill(L)
        self.assertEqual(loc_row(L, lid)["line_start"], 2)

    def test_fill_refs_ambiguous_symbol_left_null(self):
        src = "def go():\n    pass\n\nclass K:\n    def go(self):\n        pass\n"
        L, sha, rid, _ = project({"src/g.py": src, "src/m.py": "use(z)\nuse(z)\n"}, test=self)
        two_defs = ref(L, rid, "src/g.py", None, None, symbol="go")
        two_uses = ref(L, rid, "src/m.py", None, None, symbol="z")
        failed, out = self.fill(L)
        self.assertEqual(failed, 2, out)
        self.assertIn("symbol 'go' is defined on 2 lines (1, 5)", out)
        self.assertIn("symbol 'z' is on 2 lines (1, 2) and defined on none", out)
        for lid in (two_defs, two_uses):
            r = loc_row(L, lid)
            self.assertEqual((r["line_start"], r["excerpt"], r["permalink"], r["blob_sha"]), (None, None, None, None))
        self.assertEqual(untraceable(L, "src/g.py"), ["symbol not resolved to lines"])

    def test_fill_refs_symbol_not_found_no_permalink(self):
        L, sha, rid, _ = project({"src/d.py": DEFS}, test=self)
        lid = ref(L, rid, "src/d.py", None, None, symbol="gamma")
        failed, out = self.fill(L)
        self.assertEqual(failed, 1)
        self.assertIn("symbol 'gamma' not found", out)
        self.assertIsNone(loc_row(L, lid)["permalink"])

    def test_fill_refs_out_of_range_lines_not_traceable(self):
        L, sha, rid, _ = project({"src/a.py": numbered(5)}, test=self)
        lid = ref(L, rid, "src/a.py", 4, 9)
        failed, out = self.fill(L)
        self.assertGreaterEqual(failed, 1)
        self.assertIsNone(loc_row(L, lid)["excerpt"])
        self.assertEqual(untraceable(L, "src/a.py"), ["no excerpt"])

    def test_fill_refs_rejects_out_of_range_lines(self):
        L, sha, rid, _ = project({"src/a.py": numbered(5)}, test=self)
        lid = ref(L, rid, "src/a.py", 6, 6)
        p = L.run("fill-refs")
        self.assertEqual(p.returncode, 1)
        self.assertIsNone(loc_row(L, lid)["excerpt"])
        p = L.run("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("v_untraceable_refs", p.stdout)
        # an excerpt written by hand that is shorter than its lines (past the end) is caught by the view too
        L.x("UPDATE finding_locations SET excerpt='line 5', line_start=5, line_end=7 WHERE id=?", (lid,))
        self.assertEqual(untraceable(L, "src/a.py"), ["excerpt does not cover its lines (past the end of the file?)"])
        L.x("UPDATE finding_locations SET excerpt='' WHERE id=?", (lid,))
        self.assertEqual(untraceable(L, "src/a.py"), ["empty excerpt"])

    def test_fill_refs_whole_file(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED, "big.txt": numbered(250)}, test=self)
        whole = ref(L, rid, "src/a.py", None, None)
        big = ref(L, rid, "big.txt", None, None)
        failed, out = self.fill(L)
        self.assertEqual(failed, 0, out)
        r = loc_row(L, whole)
        self.assertEqual((r["whole_file"], r["line_start"], r["line_end"], r["excerpt_status"]), (1, 1, 30, None))
        self.assertEqual(r["excerpt"], NUMBERED.rstrip("\n"))
        self.assertEqual(r["permalink"], f"https://example.invalid/blob/{sha}/src/a.py")  # no #L anchor
        r = loc_row(L, big)
        self.assertEqual((r["line_start"], r["line_end"], r["excerpt_status"]), (1, 250, "truncated"))
        self.assertEqual(r["excerpt"].count("\n") + 1, 200)
        self.assertEqual(untraceable(L, "src/a.py"), [])
        self.assertEqual(untraceable(L, "big.txt"), [])

    def test_fill_refs_binary_file_marker(self):
        L, sha, rid, _ = project({"src/a.py": NUMBERED}, test=self)
        (L.repo.path / "logo.bin").write_bytes(b"\x89PNG\x00\x01\xff")
        (L.repo.path / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
        sha = L.repo.commit("binary")
        L.x("UPDATE review_repositories SET commit_sha=?", (sha,))
        logo = ref(L, rid, "logo.bin", None, None)
        latin = ref(L, rid, "latin.txt", 1, 1)
        failed, out = self.fill(L)
        self.assertEqual(failed, 0, out)
        r = loc_row(L, logo)
        self.assertEqual((r["excerpt"], r["excerpt_status"], r["whole_file"]), (None, "not-text", 1))
        self.assertEqual(r["excerpt_sha256"], hashlib.sha256(b"\x89PNG\x00\x01\xff").hexdigest())
        self.assertIsNotNone(r["blob_sha"])
        r = loc_row(L, latin)
        self.assertEqual((r["excerpt"], r["excerpt_status"]), (None, "not-text"))
        self.assertEqual(untraceable(L, "logo.bin") + untraceable(L, "latin.txt"), [])
        failed, out = self.fill(L)
        self.assertIn("filled 0 references", out)  # not selected again

    def test_fill_refs_keeps_existing_excerpt(self):
        tmpl = "https://h.invalid/{sha}/{path}?a={start}&b={end}&again={sha}"
        L, sha, rid, rep = project({"src/a.py": NUMBERED, "src/d.py": DEFS}, permalink_template=tmpl, test=self)
        lined = ref(L, rid, "src/a.py", 2, 4)
        by_symbol = ref(L, rid, "src/d.py", None, None, symbol="a")
        kept = ref(L, rid, "src/a.py", 1, 1, excerpt="line 1 (by hand)")
        gone = ref(L, rid, "src/gone.py", 1, 1)
        r2 = L.review("R-0002", commit_sha=sha, repository_id=rep)
        other = ref(L, r2, "src/a.py", 3, 3)
        failed, out = self.fill(L, "R-0001")
        self.assertEqual(failed, 1)  # the unreadable path
        r = loc_row(L, lined)
        self.assertEqual(r["excerpt"], "line 2\nline 3\nline 4")
        self.assertEqual(r["excerpt_sha256"], hashlib.sha256(r["excerpt"].encode()).hexdigest())
        self.assertEqual(r["blob_sha"], L.repo.rev_parse(f"{sha}:src/a.py"))
        self.assertEqual(r["permalink"], f"https://h.invalid/{sha}/src/a.py?a=2&b=4&again={sha}")
        self.assertEqual(loc_row(L, by_symbol)["line_start"], 4)
        self.assertEqual(loc_row(L, kept)["excerpt"], "line 1 (by hand)")
        self.assertIsNone(loc_row(L, gone)["excerpt"])
        self.assertIsNone(loc_row(L, other)["excerpt"])  # review scoping
        failed, out = self.fill(L, "R-0001")
        self.assertIn("filled 0 references", out)


class CheckGit(unittest.TestCase):
    def test_check_reports_missing_tag_and_commit(self):
        L, sha, rid, _ = project(test=self)
        lid = ref(L, rid, "src/a.py", 1, 1)
        L.match(L.scalar("SELECT finding_id FROM finding_locations WHERE id=?", (lid,)),
                L.issue(first_seen_review_id=rid))
        L.run_ok("fill-refs")
        L.run_ok("tag-review", "R-0001")
        p = L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout)
        L.repo.git("tag", "-d", "review/R-0001")
        p = L.run("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("tag review/R-0001 is missing", p.stdout)
        L.x("UPDATE review_repositories SET commit_sha=?", ("0" * 40,))
        p = L.run("check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("the reviewed commit is not in the repository", p.stdout)


if __name__ == "__main__":
    unittest.main()
