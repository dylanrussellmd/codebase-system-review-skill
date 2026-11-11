"""Small builders shared by test_core_helpers, test_source, test_refs_tags and test_cli (not a test module)."""
import contextlib
import io

from dev.tests.fixtures import Ledger, ScratchRepo

# A 30-line file whose line n reads "line n"; easy to check slices against.
NUMBERED = "".join(f"line {n}\n" for n in range(1, 31))
PERMALINK = "https://example.invalid/blob/{sha}/{path}#L{start}-L{end}"


def numbered(n):
    return "".join(f"line {i}\n" for i in range(1, n + 1))


def quiet(fn, *args, **kw):
    """Call fn with stdout captured; returns (result, printed text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fn(*args, **kw)
    return res, buf.getvalue()


def project(files=None, *, permalink_template=PERMALINK, repo_path=".", test=None):
    """A Ledger on a ScratchRepo with `files` committed, one repository row and one review at that commit.

    Returns (ledger, sha, review_id, repository_id). With test given, cleanups are registered on it."""
    L = Ledger()
    if test is not None:
        test.addCleanup(L.close)
        test.addCleanup(L.repo.cleanup)
    for p, t in (files or {"src/a.py": NUMBERED}).items():
        L.repo.write(p, t)
    sha = L.repo.commit("files")
    rep = L.repository("demo", repo_path, permalink_template=permalink_template)
    rid = L.review("R-0001", commit_sha=sha, repository_id=rep)
    return L, sha, rid, rep


def ref(L, review_id, path="src/a.py", line_start=1, line_end=None, **kw):
    """A finding in the review with one location; returns the location id."""
    fid = L.finding(review_id)
    return L.location(fid, path, line_start, line_end, **kw)
