"""The remaining write commands of a review (plan P-0001 step 131: FIX-15, FIX-18): add-requirement (and
propose-goal --source),
add-domain, review-domain, finish-run, verify, record-test-run, add-commit (and step-status --commit), add-guard
(and step-status done --guard), add-metric, add-artifact, tag-review --pushed."""
import hashlib
import unittest

from dev.tests._p5_writes2_helpers import ok, project, refused


class SetupWritesTest(unittest.TestCase):
    def test_add_requirement_and_goal_source(self):
        repo, L, sha = project(self, approved=False)
        # D-0013: no add-goal; a goal stated in a document is proposed with its --source, then the owner approves it
        self.assertEqual(ok(L, "propose-goal", "--source", "README.md, section 2", "One validator per input.",
                            "--actor", "A-0002"), ["G-01"])
        ok(L, "approve-goals", "G-01", "--actor", "A-0001")
        self.assertEqual(L.one("SELECT source, status, approved_by_actor_id FROM design_goals"),
                         {"source": "README.md, section 2", "status": "approved", "approved_by_actor_id": 1})
        self.assertIn("--source", refused(self, L, "add-requirement", "A visitor sees the sign-in page."))
        out = ok(L, "add-requirement", "A visitor sees the sign-in page.", "--source", "docs/spec.md", "--location",
                 "section 2.1", "--review", "R-0001", "--actor", "A-0002")
        self.assertEqual(out, ["REQ-0001"])
        self.assertEqual(L.one("SELECT code, source, source_location, first_review_id, status FROM requirements"),
                         {"code": "REQ-0001", "source": "docs/spec.md", "source_location": "section 2.1",
                          "first_review_id": 1, "status": "active"})

    def test_add_domain_and_review_domain(self):
        repo, L, sha = project(self, approved=False)
        self.assertIn("already", refused(self, L, "add-domain", "spec", "Spec", "--description", "x"))
        self.assertIn("--description", refused(self, L, "add-domain", "payments", "Payments flow"))
        self.assertIn("category", refused(self, L, "add-domain", "payments", "Payments", "--description", "x",
                                          "--category", "money"))
        out = ok(L, "add-domain", "payments", "Payments flow", "--description", "Applies when it takes payments.",
                 "--report-file", "payments.md", "--theme", "payments=Payments")
        self.assertEqual(out, ["domain payments (custom), theme payments"])
        self.assertEqual(L.one("SELECT name, category, report_file FROM domains WHERE code='payments'"),
                         {"name": "Payments flow", "category": "custom", "report_file": "payments.md"})
        self.assertEqual(L.scalar("SELECT name FROM themes WHERE code='payments'"), "Payments")

        self.assertIn("--reason", refused(self, L, "review-domain", "R-0001", "database", "include"))
        self.assertIn("include or skip", refused(self, L, "review-domain", "R-0001", "database", "maybe",
                                                 "--reason", "x"))
        self.assertIn("no domain", refused(self, L, "review-domain", "R-0001", "nope", "include", "--reason", "x"))
        self.assertIn("owner-change", refused(self, L, "review-domain", "R-0001", "database", "include", "--reason",
                                              "x", "--owner-change", "by-owner"))
        self.assertEqual(ok(L, "review-domain", "R-0001", "database", "include", "--reason", "the owner wants it",
                            "--owner-change", "added-by-owner", "--reviewers", "2"),
                         ["R-0001 database: included (added-by-owner)"])
        ok(L, "review-domain", "R-0001", "tests", "skip", "--reason", "the owner drops it", "--owner-change",
           "removed-by-owner")
        ok(L, "review-domain", "R-0001", "payments", "include", "--reason", "added by the owner", "--owner-change",
           "added-by-owner")
        got = {r["domain_code"]: (r["decision"], r["owner_change"], r["reviewers_planned"])
               for r in L.all("SELECT * FROM review_domains")}
        self.assertEqual(got, {"spec": ("included", "none", None), "tests": ("skipped", "removed-by-owner", None),
                               "database": ("included", "added-by-owner", 2),
                               "payments": ("included", "added-by-owner", None)})

    def test_skip_refused_once_a_run_exists(self):
        repo, L, sha = project(self)
        self.assertIn("reviewer run", refused(self, L, "review-domain", "R-0001", "spec", "skip", "--reason", "x"))


class RunWritesTest(unittest.TestCase):
    def test_finish_run_records_report_and_sha256(self):
        repo, L, sha = project(self)
        report = L.dir / "reviews" / "R-0001" / "spec.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Spec\n\n1 high.\n")
        outside = L.root / "notes.md"
        outside.write_text("x")
        self.assertIn("no run", refused(self, L, "finish-run", "R-0001", "nope", "--report", report))
        self.assertIn("--report", refused(self, L, "finish-run", "R-0001", "spec-A"))
        self.assertIn("inside", refused(self, L, "finish-run", "R-0001", "spec-A", "--report", outside))
        self.assertIn("not one of", refused(self, L, "finish-run", "R-0001", "spec-A", "--status", "done",
                                            "--report", report))
        out = ok(L, "finish-run", "R-0001", "spec-A", "--report", report, "--items", "4", "--high", "1",
                 "--medium", "0", "--low", "0", "--message", "Reviewed 4 paths; 1 high.", "--actor", "A-0002")
        self.assertIn("spec-A: running -> finished", out[0])
        r = L.one("""SELECT status, report_path, report_sha256, items_reviewed, high_count, medium_count, low_count,
                     final_message, finished_at IS NOT NULL AS done FROM reviewer_runs""")
        self.assertEqual(r, {"status": "finished", "report_path": ".code-review/reviews/R-0001/spec.md",
                             "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "items_reviewed": 4,
                             "high_count": 1, "medium_count": 0, "low_count": 0,
                             "final_message": "Reviewed 4 paths; 1 high.", "done": 1})
        self.assertEqual(L.one("SELECT kind, reviewer_run_id, path, sha256 FROM artifacts"),
                         {"kind": "report", "reviewer_run_id": 1, "path": r["report_path"],
                          "sha256": r["report_sha256"]})
        self.assertIn("already finished", refused(self, L, "finish-run", "R-0001", "spec-A", "--report", report))

    def test_verify_confirmed_and_false(self):
        repo, L, sha = project(self)
        L.finding(1, "SPEC-M1", reviewer_run_id=1)
        L.finding(1, "SPEC-M2", reviewer_run_id=1)
        L.commit()
        self.assertIn("--note", refused(self, L, "verify", "R-0001:SPEC-M1", "confirmed"))
        self.assertIn("not one of", refused(self, L, "verify", "R-0001:SPEC-M1", "true", "--note", "x"))
        self.assertIn("no point SPEC-X9", refused(self, L, "verify", "R-0001:SPEC-X9", "false", "--note", "x"))
        self.assertEqual(ok(L, "verify", "R-0001:SPEC-M1", "confirmed", "--note", "read src/app.py:3-5",
                            "--actor", "A-0002"), ["R-0001:SPEC-M1: unverified -> confirmed"])
        ok(L, "verify", "SPEC-M2", "false", "--review", "R-0001", "--note", "the check exists", "--actor", "A-0002")
        got = L.all("""SELECT point_id, verification, verification_note, verified_by_actor_id,
                       verified_at IS NOT NULL AS at FROM findings ORDER BY point_id""")
        self.assertEqual(got, [
            {"point_id": "SPEC-M1", "verification": "confirmed", "verification_note": "read src/app.py:3-5",
             "verified_by_actor_id": 2, "at": 1},
            {"point_id": "SPEC-M2", "verification": "false", "verification_note": "the check exists",
             "verified_by_actor_id": 2, "at": 1}])
        self.assertEqual(L.scalar("SELECT count(*) FROM events WHERE event='verification-changed' AND actor_id=2"), 2)

    def test_record_test_run(self):
        repo, L, sha = project(self)
        self.assertIn("purpose", refused(self, L, "record-test-run", "--purpose", "smoke", "--command", "x",
                                         "--review", "R-0001"))
        self.assertIn("--review or --step", refused(self, L, "record-test-run", "--purpose", "baseline",
                                                    "--command", "python3 -m unittest"))
        self.assertIn("not in the repository", refused(self, L, "record-test-run", "--purpose", "baseline",
                                                       "--command", "x", "--review", "R-0001", "--commit", "f" * 40))
        out = ok(L, "record-test-run", "--purpose", "baseline", "--command", "python3 -m unittest", "--review",
                 "R-0001", "--passed", "120", "--failed", "0", "--skipped", "2", "--exit-code", "0", "--duration-ms",
                 "5400", "--note", "two skipped: no network", "--actor", "A-0002")
        self.assertIn("baseline", out[0])
        self.assertEqual(L.one("""SELECT review_id, repository_id, commit_sha, purpose, passed, failed, skipped,
                                  exit_code, duration_ms, notes FROM test_runs"""),
                         {"review_id": 1, "repository_id": 1, "commit_sha": sha, "purpose": "baseline",
                          "passed": 120, "failed": 0, "skipped": 2, "exit_code": 0, "duration_ms": 5400,
                          "notes": "two skipped: no network"})

    def test_add_metric_and_artifact(self):
        repo, L, sha = project(self)
        self.assertIn("method", refused(self, L, "add-metric", "R-0001", "line_coverage", "81.5", "--method",
                                        "guessed"))
        self.assertIn("number", refused(self, L, "add-metric", "R-0001", "line_coverage", "lots", "--method",
                                        "measured"))
        ok(L, "add-metric", "R-0001", "test_cases_asserted", "7", "--method", "counted", "--domain", "spec",
           "--repo", "demo", "--scope", "src/app.py", "--unit", "cases", "--note", "table 2")
        self.assertEqual(L.one("SELECT name, scope, value, unit, method, repository_id, domain_code FROM metrics"),
                         {"name": "test_cases_asserted", "scope": "src/app.py", "value": 7.0, "unit": "cases",
                          "method": "counted", "repository_id": 1, "domain_code": "spec"})

        report = L.dir / "reviews" / "R-0001" / "spec.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Spec\n")
        self.assertIn("kind", refused(self, L, "add-artifact", "notes", "--path", report, "--review", "R-0001"))
        self.assertIn("--path or --url", refused(self, L, "add-artifact", "report", "--review", "R-0001"))
        self.assertIn("inside", refused(self, L, "add-artifact", "report", "--path", L.root / "src" / "app.py",
                                        "--review", "R-0001"))
        ok(L, "add-artifact", "report", "--path", report, "--review", "R-0001", "--run", "spec-A")
        self.assertEqual(L.one("SELECT kind, path, sha256, reviewer_run_id, review_id FROM artifacts"),
                         {"kind": "report", "path": ".code-review/reviews/R-0001/spec.md",
                          "sha256": hashlib.sha256(b"# Spec\n").hexdigest(), "reviewer_run_id": 1, "review_id": 1})
        L.plan("P-0001", reviews=(1,))
        L.commit()
        ok(L, "add-artifact", "progress-page", "--plan", "P-0001", "--url", "https://claude.ai/artifacts/example",
           "--path", L.dir / "hub.html")
        self.assertEqual(L.scalar("SELECT page_url FROM plans"), "https://claude.ai/artifacts/example")

    def test_set_verdict(self):
        repo, L, sha = project(self)
        self.assertIn("--verdict", refused(self, L, "set-verdict", "R-0001"))
        ok(L, "set-verdict", "R-0001", "--verdict", "Usable, one high to fix first.", "--summary", "Two domains.",
           "--actor", "A-0002")
        self.assertEqual(L.one("SELECT verdict, summary FROM reviews"),
                         {"verdict": "Usable, one high to fix first.", "summary": "Two domains."})

    def test_tag_review_pushed(self):
        repo, L, sha = project(self)
        self.assertIn("tag-review R-0001 first", refused(self, L, "tag-review", "R-0001", "--pushed"))
        ok(L, "tag-review", "R-0001")
        self.assertEqual(ok(L, "tag-review", "R-0001", "--pushed", "--actor", "A-0002"),
                         ["demo: review/R-0001 recorded as pushed"])
        self.assertEqual(L.scalar("SELECT tag_pushed FROM review_repositories"), 1)
        self.assertEqual(L.one("SELECT to_value, actor_id FROM events WHERE event='tag-pushed'"),
                         {"to_value": "review/R-0001", "actor_id": 2})


class FixWritesTest(unittest.TestCase):
    def plan(self):
        repo, L, sha = project(self)
        fid = L.finding(1, "SPEC-M1", reviewer_run_id=1)
        iid = L.issue("ISS-0001", first_seen_review_id=1)
        L.match(fid, iid)
        pid = L.plan("P-0001", reviews=(1,))
        L.step(pid, L.phase(pid, 1), 1, findings=(fid,))
        L.commit()
        return repo, L, sha

    def test_add_commit_and_step_status_commit(self):
        repo, L, sha = self.plan()
        self.assertIn("not in the repository", refused(self, L, "add-commit", "1", "e" * 40))
        self.assertIn("https", refused(self, L, "add-commit", "1", sha, "--pr", "ftp://x"))
        self.assertEqual(ok(L, "add-commit", "1", "HEAD", "--pr", "https://example.invalid/pr/1"),
                         [f"step 1: commit {sha[:10]} on main"])
        self.assertEqual(L.one("SELECT step_id, repository_id, commit_sha, branch, pr_url, summary FROM step_commits"),
                         {"step_id": 1, "repository_id": 1, "commit_sha": sha, "branch": "main",
                          "pr_url": "https://example.invalid/pr/1", "summary": "first"})
        repo.write("src/app.py", "fixed\n")
        fix = repo.commit("Fix the handler")
        self.assertIn("not in the repository", refused(self, L, "step-status", "1", "done", "--note", "done",
                                                       "--commit", "d" * 40))
        ok(L, "step-status", "1", "done", "--note", "done", "--commit", fix)
        self.assertEqual(L.all("SELECT commit_sha, summary FROM step_commits ORDER BY id"),
                         [{"commit_sha": sha, "summary": "first"}, {"commit_sha": fix, "summary": "Fix the handler"}])

    def test_add_guard_and_step_status_guard(self):
        repo, L, sha = self.plan()
        self.assertIn("no issue ISS-0009", refused(self, L, "add-guard", "ISS-0009", "tests/t.py", "test_x"))
        out = ok(L, "add-guard", "ISS-0001", "tests/test_app.py", "test_refuses_empty", "--failed-before",
                 "--step", "1", "--commit", sha)
        self.assertEqual(out, ["ISS-0001: guard test tests/test_app.py::test_refuses_empty"])
        g = L.one("""SELECT issue_id, repository_id, test_path, test_name, step_id, added_commit_sha, failed_before_fix,
                     passes_after_fix, still_exists FROM guard_tests""")
        self.assertEqual(g, {"issue_id": 1, "repository_id": 1, "test_path": "tests/test_app.py",
                             "test_name": "test_refuses_empty", "step_id": 1, "added_commit_sha": sha,
                             "failed_before_fix": 1, "passes_after_fix": 1, "still_exists": 1})
        self.assertIn("already", refused(self, L, "add-guard", "ISS-0001", "tests/test_app.py", "test_refuses_empty"))
        self.assertIn("PATH::NAME", refused(self, L, "step-status", "1", "done", "--note", "done", "--guard",
                                            "test_only_a_name"))
        ok(L, "step-status", "1", "done", "--note", "done", "--commit", sha, "--guard",
           "tests/test_app.py::test_keeps_quotes", "--failed-before")
        self.assertEqual(L.all("SELECT test_name, step_id, failed_before_fix, passes_after_fix FROM guard_tests "
                               "ORDER BY id"),
                         [{"test_name": "test_refuses_empty", "step_id": 1, "failed_before_fix": 1,
                           "passes_after_fix": 1},
                          {"test_name": "test_keeps_quotes", "step_id": 1, "failed_before_fix": 1,
                           "passes_after_fix": 1}])
        L.x("UPDATE issues SET status='fixed', after_state='ok', change_note='done' WHERE code='ISS-0001'")
        L.commit()
        self.assertEqual(L.count("v_unguarded_fixes"), 0)
        with self.subTest("a later review confirms them, or finds one gone"):
            self.assertIn("no guard test", refused(self, L, "confirm-guard", "R-0001", "ISS-0001", "--test",
                                                   "tests/other.py::test_y"))
            self.assertEqual(len(ok(L, "confirm-guard", "R-0001", "ISS-0001")), 2)
            ok(L, "confirm-guard", "R-0001", "ISS-0001", "--test", "tests/test_app.py::test_keeps_quotes", "--gone")
            self.assertEqual(L.all("SELECT still_exists, last_confirmed_review_id FROM guard_tests ORDER BY id"),
                             [{"still_exists": 1, "last_confirmed_review_id": 1},
                              {"still_exists": 0, "last_confirmed_review_id": 1}])


if __name__ == "__main__":
    unittest.main()
