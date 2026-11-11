"""Shared setup for the step 131-132 write-command tests (test_p5_writes2_*.py). Not a test module.

Commands run in this process through the built tool's main() (the same parsing and code as the CLI, without a
Python start per command), so these tests stay fast; LEDGER_SLOW_TESTS=1 runs each as its own process instead.
"""
import contextlib
import io
import os
import subprocess
import sys
from unittest import mock

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import APP, new_ledger


def run(L, *args):
    """`ledger.py --root <project> <args>` for Ledger L; returns CompletedProcess(returncode, stdout, stderr)."""
    L.commit()
    argv = ["--root", str(L.root), *map(str, args)]
    if fixtures.SLOW:
        return fixtures.run_ledger(*argv, ledger_py=L.ledger_py)
    mod = L.module
    out, err, rc = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", [str(L.ledger_py), *argv]), \
            mock.patch.dict(os.environ, fixtures.git_env(), clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            mod.main()
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
                rc = 1
            else:
                rc = e.code or 0
    return subprocess.CompletedProcess(argv, rc, out.getvalue(), err.getvalue())


def ok(L, *args):
    p = run(L, *args)
    if p.returncode != 0:
        raise AssertionError(f"ledger.py {' '.join(map(str, args))} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
    return p.stdout.splitlines()


def refused(test, L, *args):
    """A command that must be refused with one line on stderr and exit 1, writing nothing; returns that line."""
    before = changes(L)
    p = run(L, *args)
    test.assertEqual(p.returncode, 1, f"{args} should be refused:\n{p.stdout}\n{p.stderr}")
    test.assertNotIn("Traceback", p.stderr)
    err = p.stderr.strip().splitlines()
    test.assertEqual(len(err), 1, p.stderr)
    test.assertIn("nothing was written", err[0])
    test.assertEqual(changes(L), before, f"{args} wrote something although it was refused")
    return err[0]


def changes(L):
    """A cheap fingerprint of every table's rows (count and max rowid), to see that a refusal wrote nothing."""
    L.commit()
    names = [r[0] for r in L.con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: tuple(L.con.execute(f'SELECT count(*), total(rowid) FROM "{n}"').fetchone()) for n in names}


def project(test, *, approved=True):
    """A git project with src/app.py committed, a ledger with repository 'demo', the owner A-0001, a manager A-0002
    and review R-0001 (domains spec and tests included; approved and running with run spec-A unless
    approved=False)."""
    repo = fixtures.ScratchRepo()
    repo.write("src/app.py", APP)
    sha = repo.commit("first")
    L = new_ledger(repo)
    test.addCleanup(L.close)
    ok(L, "add-repo", "demo", ".")
    ok(L, "add-actor", "owner", "Project Owner")
    ok(L, "add-actor", "manager", "review manager")
    ok(L, "new-review", "--title", "First review", "--scope", "full", "--domain", "spec=the spec", "--domain",
       "tests", "--actor", "A-0002")
    if approved:
        ok(L, "review-status", "R-0001", "awaiting-approval", "--note", "domains proposed", "--actor", "A-0002")
        ok(L, "review-status", "R-0001", "approved", "--note", "the owner approved the domains", "--approved-by",
           "A-0001", "--actor", "A-0002")
        ok(L, "review-status", "R-0001", "running", "--note", "reviewers started", "--actor", "A-0002")
        ok(L, "add-run", "R-0001", "spec", "spec-A", "--scope", "all", "--model", "m1", "--status", "running",
           "--actor", "A-0002")
    return repo, L, sha
