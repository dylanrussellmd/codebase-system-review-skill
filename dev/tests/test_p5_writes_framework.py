"""Write-command framework, new-review, add-actor, add-run, next-code and snapshot (plan P-0001 step 98: ABS-H3,
WALK-M4, SPEC-M5, SPEC-X7, WALK-M16, FIX-4)."""
import gc
import io
import sqlite3
import sys
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import APP, counts, new_ledger, ok, project, refused


class WriteFrameworkTest(unittest.TestCase):
    def test_new_review_allocates_next_code(self):
        repo, L, sha = project(self)
        self.assertEqual(L.scalar("SELECT code FROM reviews"), "R-0001")
        self.assertEqual(L.one("SELECT commit_sha, branch, is_dirty FROM review_repositories"),
                         {"commit_sha": sha, "branch": "main", "is_dirty": 0})
        self.assertEqual(L.all("SELECT domain_code, decision FROM review_domains ORDER BY domain_code"),
                         [{"domain_code": "spec", "decision": "included"},
                          {"domain_code": "tests", "decision": "included"}])
        out = ok(L, "new-review", "--title", "Second", "--scope", "second", "--previous", "R-0001",
                 "--skip", "ui=no pages")
        self.assertEqual(out[0], "R-0002")
        self.assertEqual(L.scalar("SELECT previous_review_id FROM reviews WHERE code='R-0002'"),
                         L.id_of("reviews", "R-0001"))
        self.assertEqual(L.scalar("SELECT reason FROM review_domains WHERE domain_code='ui'"), "no pages")
        # the review event carries the manager given with --actor
        self.assertEqual(L.scalar("SELECT status FROM reviews WHERE code='R-0001'"), "running")

    def test_add_actor_rejects_duplicate(self):
        repo, L, _ = project(self, approved=False)
        out = ok(L, "add-actor", "verifier", "checker", "--model", "m2", "--review", "R-0001", "--role", "verifier")
        self.assertEqual(out[0], "A-0002")
        before = counts(L, "actors", "review_actors")
        err = refused(self, L, "add-actor", "verifier", "checker", "--model", "m2")
        self.assertIn("A-0002 is already verifier 'checker'", err)
        self.assertEqual(counts(L, "actors", "review_actors"), before)
        ok(L, "add-actor", "verifier", "checker", "--model", "m2", "--session", "run-2")  # another instance
        self.assertEqual(L.scalar("SELECT parent_actor_id FROM actors WHERE id=2"), None)

    def test_add_run_requires_approved_domain(self):
        repo, L, _ = project(self, approved=False)
        before = counts(L, "reviewer_runs", "actors", "events")
        err = refused(self, L, "add-run", "R-0001", "spec", "spec-A", "--scope", "all")
        self.assertIn("need the owner's approval", err)
        ok(L, "review-status", "R-0001", "awaiting-approval", "--note", "proposed")
        ok(L, "review-status", "R-0001", "approved", "--note", "approved")
        err = refused(self, L, "add-run", "R-0001", "security", "sec-A", "--scope", "all")
        self.assertIn("did not include domain 'security'", err)
        after = counts(L, "reviewer_runs", "actors")
        self.assertEqual(after["reviewer_runs"], before["reviewer_runs"])
        self.assertEqual(after["actors"], before["actors"] + 1)  # only the owner who approved
        ok(L, "add-run", "R-0001", "spec", "spec-A", "--scope", "all")
        self.assertEqual(L.scalar("SELECT approved_by_actor_id IS NOT NULL FROM reviews"), 1)

    def test_write_command_is_one_transaction(self):
        """new-review fails on its last part (an unknown design goal) after inserting the review, its repositories
        and domains: nothing is left, and the code is not used up."""
        repo, L, _ = project(self, approved=False)
        before = counts(L, "reviews", "review_repositories", "review_domains", "events")
        err = refused(self, L, "new-review", "--title", "X", "--scope", "full", "--domain", "spec", "--goal", "G-07")
        self.assertIn("no design goal G-07", err)
        self.assertIn("nothing was written", err)
        self.assertEqual(counts(L, "reviews", "review_repositories", "review_domains", "events"), before)
        self.assertEqual(ok(L, "next-code", "R"), ["R-0002"])

    def test_trigger_refusal_is_one_line(self):
        repo, L, _ = project(self, approved=False)
        err = refused(self, L, "review-status", "R-0001", "handed-off", "--note", "skip ahead")
        self.assertIn("refused by the ledger", err)
        self.assertIn("a review moves planning", err)

    def test_next_code_and_format(self):
        repo, L, _ = project(self, approved=False)
        for prefix, want in (("ISS", "ISS-0001"), ("R", "R-0002"), ("P", "P-0001"), ("D", "D-0001"),
                             ("GATE", "GATE-0001"), ("REQ", "REQ-0001"), ("G", "G-01"), ("iss-", "ISS-0001")):
            self.assertEqual(ok(L, "next-code", prefix), [want], prefix)
        L.issue("ISS-0001", first_seen_review_id=1)
        L.issue("ISS-0002", first_seen_review_id=1)
        self.assertEqual(ok(L, "next-code", "ISS"), ["ISS-0003"])
        self.assertEqual(L.scalar("SELECT next_code FROM v_next_codes WHERE prefix='ISS'"), "ISS-0003")
        self.assertIn("unknown code prefix", refused(self, L, "next-code", "X"))
        with self.assertRaises(sqlite3.IntegrityError):
            L.issue("ISS-3", first_seen_review_id=1)   # malformed: the code CHECK (B-15) refuses it
        # write commands produce sequential codes
        self.assertEqual(ok(L, "new-review", "--title", "B", "--scope", "full")[0], "R-0002")
        self.assertEqual(ok(L, "new-review", "--title", "C", "--scope", "full")[0], "R-0003")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_fills_review_repositories(self):
        repo, L, sha = project(self, approved=False)
        L.x("UPDATE review_repositories SET branch=NULL, commit_subject=NULL")
        L.commit()
        repo.write("src/app.py", APP + "line 11\n")
        sha2 = repo.commit("second")
        out = ok(L, "snapshot", "R-0001")
        self.assertIn(f"demo: {sha2} on main, clean", out)
        row = L.one("SELECT commit_sha, branch, commit_subject, commit_time, is_dirty FROM review_repositories")
        self.assertEqual(row["commit_sha"], sha2)
        self.assertEqual((row["branch"], row["commit_subject"], row["is_dirty"]), ("main", "second", 0))
        self.assertRegex(row["commit_time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        ok(L, "snapshot", "R-0001", "--repo", f"demo@{sha[:8]}")   # an explicit commit, given short
        self.assertEqual(L.scalar("SELECT commit_sha FROM review_repositories"), sha)
        self.assertIn("is not in the repository", refused(self, L, "snapshot", "R-0001", "--repo", "demo@0123abc"))

    def test_snapshot_clean_after_init(self):
        """A fresh init inside a repository that does not ignore .code-review/: the ledger's own files do not make
        the tree dirty."""
        repo = fixtures.ScratchRepo(exclude_ledger=False)
        repo.write("a.txt", "a\n")
        repo.commit("a")
        L = new_ledger(repo, real_init=True)
        self.addCleanup(L.close)
        self.assertIn("??", repo.git("status", "--porcelain"))  # git itself sees .code-review/ as untracked
        ok(L, "add-repo", "demo", ".")
        ok(L, "new-review", "--title", "T", "--scope", "full")
        self.assertEqual(L.one("SELECT is_dirty, dirty_patch_path FROM review_repositories"),
                         {"is_dirty": 0, "dirty_patch_path": None})

    def test_snapshot_patch_includes_staged_and_untracked(self):
        repo, L, sha = project(self, approved=False)
        repo.write("src/app.py", APP.replace("line 2\n", "B2-STAGED\n"))
        repo.git("add", "src/app.py")
        repo.write("src/app.py", APP.replace("line 2\n", "B2-STAGED\n").replace("line 3\n", "B3-UNSTAGED\n"))
        repo.write("src/new.py", "B-UNTRACKED\n")
        out = ok(L, "snapshot", "R-0001")
        self.assertIn("working tree differs", out[0])
        row = L.one("SELECT is_dirty, dirty_note, dirty_patch_path FROM review_repositories")
        self.assertEqual(row["is_dirty"], 1)
        self.assertEqual(row["dirty_patch_path"], ".code-review/patches/R-0001-demo.patch")
        self.assertIn("1 changed and 1 untracked", row["dirty_note"])
        patch = (L.root / row["dirty_patch_path"]).read_text()
        for text in ("+B2-STAGED", "+B3-UNSTAGED", "+B-UNTRACKED", "src/new.py"):
            self.assertIn(text, patch)
        self.assertNotIn("ledger.sqlite", patch)
        self.assertEqual((L.root / row["dirty_patch_path"]).stat().st_mode & 0o777, 0o600)
        # the patch applies to the reviewed commit and gives the reviewed tree
        check = fixtures.ScratchRepo()
        check.git("fetch", "-q", str(repo.path), "main")
        check.git("checkout", "-q", "FETCH_HEAD")
        check.git("apply", str(L.root / row["dirty_patch_path"]))
        self.assertEqual(check.read("src/new.py"), "B-UNTRACKED\n")

    def test_fill_refs_dirty_repo_uses_reviewed_text(self):
        repo, L, sha = project(self)
        repo.write("src/app.py", APP.replace("line 2\n", "B2-UNCOMMITTED\n"))
        ok(L, "snapshot", "R-0001")
        rid, run = L.id_of("reviews", "R-0001"), L.scalar("SELECT id FROM reviewer_runs")
        fid = L.finding(rid, "SPEC-M1", reviewer_run_id=run)
        L.location(fid, "src/app.py", 2, 2)
        ok(L, "fill-refs", "R-0001")
        row = L.one("SELECT excerpt, source, commit_sha FROM finding_locations")
        self.assertEqual(row, {"excerpt": "B2-UNCOMMITTED", "source": "working-tree", "commit_sha": sha})
        self.assertTrue((L.dir / "patches" / "R-0001-demo.patch").exists())
        # a file the working tree did not change is read from the commit
        fid2 = L.finding(rid, "SPEC-M2", reviewer_run_id=run)
        L.location(fid2, "src/app.py", 1, 1)
        repo.write("src/app.py", APP)
        ok(L, "fill-refs", "R-0001")
        self.assertEqual(L.one("SELECT excerpt, source FROM finding_locations WHERE finding_id=?", (fid2,)),
                         {"excerpt": "line 1", "source": "commit"})

    def test_snapshot_refuses_moving_a_review_with_findings(self):
        repo, L, sha = project(self)
        fid = L.finding(1, "SPEC-M1", reviewer_run_id=1)
        L.location(fid, "src/app.py", 1, 1)
        repo.write("b.txt", "b\n")
        repo.commit("b")
        self.assertIn("cannot change", refused(self, L, "snapshot", "R-0001"))


class MainTest(unittest.TestCase):
    def test_main_closes_connection(self):
        """main() run in-process for check (which ends in sys.exit) leaves no unclosed ledger connection."""
        L = new_ledger()
        self.addCleanup(L.close)
        mod = fixtures.load_ledger_module(L.ledger_py)
        argv = sys.argv
        sys.argv = ["ledger.py", "--root", str(L.root), "check"]
        gc.collect()   # connections other tests left behind warn now, not inside the check below
        try:
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        mod.main()
                gc.collect()
        finally:
            sys.argv = argv
        self.assertEqual([str(w.message) for w in seen if issubclass(w.category, ResourceWarning)], [])
        self.assertEqual(mod._MAIN_CONNECTIONS, [])

    def test_help_gives_usage_lines(self):
        L = new_ledger()
        self.addCleanup(L.close)
        for cmd, text in (("new-review", "ledger.py new-review --title T"), ("report-finding", "--where"),
                          ("match", "ledger.py match R-0001 POINT"), ("import-steps", "plan-steps.md")):
            p = L.run(cmd, "--help")
            self.assertEqual(p.returncode, 0)
            self.assertIn(text, p.stdout.replace("\n", " ").replace("  ", " "))


class MigrationTest(unittest.TestCase):
    def test_migrate_7_to_8_adds_source_and_next_codes(self):
        """The demo ledger of the oldest schema migrates through 7 to 8: every existing reference gets source
        'commit', v_next_codes is there, and the result matches the shipped schema."""
        from dev.tests import _migrate_helpers as M
        project_dir, db = M.restore_v1()
        refs = sqlite3.connect(str(db)).execute("SELECT count(*) FROM finding_locations").fetchone()[0]
        self.assertGreater(refs, 0)
        ledger_py = new_ledger().ledger_py
        p = fixtures.run_ledger("--root", project_dir, "migrate", ledger_py=ledger_py)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0008-write-commands", p.stdout)
        mod = fixtures.load_ledger_module(ledger_py)
        c = sqlite3.connect(str(db))
        try:
            self.assertEqual(c.execute("SELECT source, count(*) FROM finding_locations GROUP BY source").fetchall(),
                             [("commit", refs)])
            self.assertEqual(c.execute("SELECT next_code FROM v_next_codes WHERE prefix='ISS'").fetchone()[0],
                             "ISS-0005")
            self.assertEqual(mod.verify_schema(c), [])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
