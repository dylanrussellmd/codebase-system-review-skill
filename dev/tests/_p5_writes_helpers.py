"""Shared setup for the write-command tests (plan P-0001 steps 98, 99, 105, 106, 127). Not a test module.

LEDGER_PY_UNDER_TEST=/path/to/ledger.py runs these tests against another build (to see them fail on a build from
before the write commands existed).
"""
import os
import sqlite3

from dev.tests import fixtures

LEDGER_PY = os.environ.get("LEDGER_PY_UNDER_TEST") or None

APP = "".join(f"line {n}\n" for n in range(1, 11)).replace("line 5\n", "def handler(request):\n")


def new_ledger(repo=None, **kw):
    if LEDGER_PY:
        kw["ledger_py"] = LEDGER_PY
    return fixtures.Ledger(repo, **kw)


def ok(L, *args, **kw):
    """Run a command that must succeed; returns its stdout lines."""
    return L.run_ok(*args, **kw).stdout.splitlines()


def refused(test, L, *args, **kw):
    """Run a command that must be refused with one line and exit 1 (never a traceback); returns that line."""
    p = L.run(*args, **kw)
    test.assertEqual(p.returncode, 1, f"{args} should be refused:\n{p.stdout}\n{p.stderr}")
    test.assertNotIn("Traceback", p.stderr)
    err = p.stderr.strip().splitlines()
    test.assertEqual(len(err), 1, p.stderr)
    return err[0]


def project(test, *, approved=True, files=None):
    """A git project with src/app.py committed, a ledger with repository 'demo', a manager A-0001 and review R-0001
    (approved and running, domain spec included, reviewer run spec-A) unless approved=False (planning)."""
    repo = fixtures.ScratchRepo()
    for rel, text in (files or {"src/app.py": APP}).items():
        repo.write(rel, text)
    sha = repo.commit("first")
    L = new_ledger(repo)
    test.addCleanup(L.close)
    ok(L, "add-repo", "demo", ".")
    ok(L, "add-actor", "manager", "review manager")
    ok(L, "new-review", "--title", "First review", "--scope", "full", "--actor", "A-0001")
    ok(L, "review-domain", "R-0001", "spec", "include", "--reason", "the spec")
    ok(L, "review-domain", "R-0001", "tests", "include", "--reason", "proposed for this review")
    if approved:
        ok(L, "review-status", "R-0001", "awaiting-approval", "--note", "domains proposed")
        ok(L, "review-status", "R-0001", "approved", "--note", "the owner approved the domains")
        ok(L, "review-status", "R-0001", "running", "--note", "reviewers started")
        ok(L, "add-run", "R-0001", "spec", "spec-A", "--scope", "all", "--model", "m1")
    return repo, L, sha


def counts(L, *tables):
    L.commit()
    con = sqlite3.connect(str(L.db))
    try:
        return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()


def index_row(pid, *, sev="M", kind="defect", theme="validation", sub=None, locations="demo:src/app.py:2-3",
              where="L10", summary=None, before="It accepts anything.", fix="Validate it.", test="test_x: refuses",
              related="", known="", domain="", goals="", requirements=""):
    cells = [pid, sev, kind, theme, sub or pid.lower(), locations, where, summary or f"Point {pid} is wrong.", before,
             fix, test, related, known, domain, goals, requirements]
    return "\t".join(cells)
