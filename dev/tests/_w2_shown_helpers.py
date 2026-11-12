"""A demo ledger with one of everything the review and plan pages show since plan P-0001 steps 191 to 198: test
runs, files, requirements, links between issues, a retired goal, a plan document, a defect found during fixes, and a
report and plan narrative written in Markdown."""
from dev.tests import fixtures

REPORT = """# Verdict

The code is **sound** with two gaps. See `src/auth.py`.

## What to fix first

1. Sign-in codes
2. The notice period

| Area | Points |
| --- | --- |
| Security | 1 |
| Spec | 2 |
"""
NARRATIVE = "## How this plan is ordered\n\nSafety first, then the **fixes**.\n\n- CI\n- Sign-in"


def build(L):
    ids = fixtures.seed_demo(L)
    r1, r2 = ids["reviews"]["R-0001"], ids["reviews"]["R-0002"]
    plan, repo = ids["plan"], ids["repository"]
    L.update("reviews", r1, report_md=REPORT, skill_version="2.4.0", review_folder=".code-review/reviews/R-0001")
    L.update("plans", plan, narrative_md=NARRATIVE, doc_url="https://example.com/plan-doc",
             page_url="https://example.com/plan-page")
    L.update("repositories", repo, remote_url="https://example.com/demo.git", default_branch="main",
             notes="The only repository.")
    run = L.scalar("SELECT id FROM reviewer_runs WHERE review_id=? ORDER BY id LIMIT 1", (r1,))
    L.update("reviewer_runs", run, prompt="Review the sign-in code for weak randomness.",
             final_message="One high point: codes are predictable.", report_path=".code-review/reviews/R-0001/security.md",
             started_at="2026-11-01T09:00:00.000Z", finished_at="2026-11-01T09:20:00.000Z")
    actor = L.scalar("SELECT actor_id FROM reviewer_runs WHERE id=?", (run,))
    L.update("actors", actor, harness="a test harness", session_ref="session-42")
    L.insert("test_runs", review_id=r1, repository_id=repo, commit_sha=ids["commits"][0],
             command="python -m unittest", purpose="baseline", passed=41, failed=2, skipped=1, errored=0, exit_code=1,
             duration_ms=12300, notes="Two known failures.")
    L.insert("artifacts", review_id=r1, reviewer_run_id=run, kind="report",
             path=".code-review/reviews/R-0001/security.md", url="https://example.com/reports/security")
    L.insert("artifacts", review_id=r1, kind="brief", path=".code-review/reviews/R-0001/BRIEF.md")
    L.insert("artifacts", plan_id=plan, kind="plan-file", path=".code-review/plans/P-0001.md")
    L.relation(ids["issues"]["ISS-0002"], ids["issues"]["ISS-0003"], "blocks", "Same function.",
               established_in_review_id=r2)
    req = L.insert("requirements", code="REQ-0001", text="A request needs 14 days of notice.", source="spec.md")
    L.insert("review_requirement_checks", review_id=r1, requirement_id=req, verdict="differs", tested="no",
             where_implemented="src/requests.py", evidence="The limit is hardcoded.")
    f1 = ids["findings"]["R-0001/SPEC-M1"]
    L.insert("finding_requirements", finding_id=f1, requirement_id=req, verdict="differs")
    L.insert("finding_goals", finding_id=f1, goal_id=L.id_of("design_goals", "G-01"))
    old = L.design_goal(None, "Reports are sent weekly.", adopted_at="2026-01-05T10:00:00.000Z")
    L.update("design_goals", old, status="retired", retired_at="2026-06-01T10:00:00.000Z")
    L.update("steps", ids["steps"][2], started_at="2026-11-02T08:00:00.000Z", done_at="2026-11-02T09:30:00.000Z")
    # a defect found while step 2 was being fixed, matched to a new issue, closed by step 3
    found = L.finding(r1, "FOUND-1", severity="M", domain_code="security", summary="The retry loop never stops.",
                      discovered_in_step_id=ids["steps"][2])
    iss = L.issue(None, first_seen_review_id=r1, title="Endless retry", domain_code="security")
    L.match(found, iss, "new")
    L.step_finding(ids["steps"][3], found)
    L.commit()
    ids.update(run=run, found=found, found_issue=L.scalar("SELECT code FROM issues WHERE id=?", (iss,)), retired=old)
    return ids
