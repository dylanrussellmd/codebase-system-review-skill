"""Shared builders for the test suite. Standard library only; Python 3.9+.

Import from any test module as::

    from dev.tests import fixtures
    from dev.tests.fixtures import Ledger, ScratchRepo, ServerThread, seed_demo

and run the suite from the repository root with::

    python3 -m unittest discover -s dev/tests -t . -v

What is here
------------
load_ledger_module()  Builds scripts/ledger.py from the sources (dev/build.py's build()) into a temporary file once
                      per test process and imports it, so tests exercise exactly the code that ships. built_ledger_path()
                      is that file, for subprocess tests; run_ledger() runs it as a CLI.
ScratchRepo           A throwaway git repository with deterministic commit shas (fixed identity and dates, isolated
                      HOME and git config): write / delete / commit / tag / subrepo / rewrite_history / remove.
Ledger                A ledger initialised in a project folder (by default a new ScratchRepo), with `con` (sqlite3,
                      foreign_keys ON, sqlite3.Row rows), insert helpers for the main tables that fill every
                      timestamp column from a deterministic Clock, and run() for the CLI.
seed_demo(ledger)     The demo data set (two reviews, matching, a plan, a decision and gates) with built-in
                      assertions; returns the ids it created. finish_demo(ledger) then runs fill-refs, tag-review x2
                      and check through the CLI.
ServerThread          Runs the viewer (serve()) in-process on a free port in a thread (mode="thread", default) or as a
                      `ledger.py serve` subprocess (mode="process"); get/post/raw helpers and the page token.

Everything temporary goes under one session folder (session_dir()) that is deleted when the test process exits, so
cleanup calls are optional. Nothing here writes inside the repository.
"""
import atexit
import importlib.util
import itertools
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import http.client
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "dev" / "build.py"
SHIPPED_LEDGER = REPO_ROOT / "scripts" / "ledger.py"

# Tests that take more than a second or two (scale, soak) should be decorated with @slow.
SLOW = os.environ.get("LEDGER_SLOW_TESTS") == "1"


def slow(test):
    """Skip a test unless LEDGER_SLOW_TESTS=1 (keeps the default suite under a minute)."""
    import unittest
    return unittest.skipUnless(SLOW, "slow test; set LEDGER_SLOW_TESTS=1 to run")(test)


# ---------------------------------------------------------------- session folder

_session = None


def session_dir():
    """One temporary folder per test process, removed at exit. Everything temporary goes under it."""
    global _session
    if _session is None:
        _session = Path(tempfile.mkdtemp(prefix="ledger-tests-")).resolve()
        atexit.register(shutil.rmtree, str(_session), True)
    return _session


def temp_dir(prefix="t-"):
    """A new empty folder inside session_dir()."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(session_dir())))


# ---------------------------------------------------------------- the built ledger module

def load_build_module():
    """Import dev/build.py as a module (it must not build on import)."""
    spec = importlib.util.spec_from_file_location("ledger_build_under_test", str(BUILD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_built_path = None


def built_ledger_path():
    """Path of a ledger.py freshly built from the sources (built once per test process)."""
    global _built_path
    if _built_path is None:
        text = load_build_module().build()
        d = session_dir() / "build"
        d.mkdir(exist_ok=True)
        p = d / "ledger.py"
        p.write_bytes(text.encode("utf-8"))
        _built_path = p
    return _built_path


_module_cache = {}
_module_counter = itertools.count(1)


def load_ledger_module(path=None, fresh=False):
    """Import the ledger tool.

    path   a ledger.py to import; default built_ledger_path() (the sources, built now).
    fresh  return a new, private module object instead of the cached one. Use it when a test patches module
           globals (ServerThread does), so other tests are not affected.
    """
    p = Path(path) if path else built_ledger_path()
    key = str(p.resolve())
    if not fresh and key in _module_cache:
        return _module_cache[key]
    name = f"ledger_under_test_{next(_module_counter)}"
    spec = importlib.util.spec_from_file_location(name, str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    if not fresh:
        _module_cache[key] = mod
    return mod


# ---------------------------------------------------------------- isolated environment

GIT_NAME = "Test Author"
GIT_EMAIL = "test@example.invalid"


def isolated_home():
    """A HOME for git and child processes with an empty global git config."""
    home = session_dir() / "home"
    if not home.exists():
        home.mkdir()
        (home / ".gitconfig").write_text("")
        (home / ".config").mkdir()
    return home


def git_env(extra=None):
    """os.environ without any GIT_* variable, with an isolated HOME, no system or user git config, a fixed identity,
    and UTF-8 output. Pass it to every git or ledger.py child process."""
    home = isolated_home()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_AUTHOR_NAME": GIT_NAME, "GIT_AUTHOR_EMAIL": GIT_EMAIL,
        "GIT_COMMITTER_NAME": GIT_NAME, "GIT_COMMITTER_EMAIL": GIT_EMAIL,
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if extra:
        env.update(extra)
    return env


def run_ledger(*args, cwd=None, ledger_py=None, env=None, input=None, timeout=60):
    """Run ledger.py (default: the fresh build) as a CLI. Returns subprocess.CompletedProcess with text stdout/stderr;
    never raises on a non-zero exit."""
    cmd = [sys.executable, str(ledger_py or built_ledger_path()), *[str(a) for a in args]]
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env or git_env(), input=input,
                          capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------- git repositories

class ScratchRepo:
    """A throwaway git repository with deterministic shas.

    Commits use a fixed author/committer and dates EPOCH + n minutes (n = commits made through this object), and git
    runs with git_env(), so the same writes and commits give the same shas on every machine and run. The repository's
    own .git/config also carries the identity, so code under test that runs git with the real environment (for
    example tag_review called in-process) still works.

    By default `.code-review/` is listed in .git/info/exclude, so a ledger inside the project never lands in a commit
    (that would make shas depend on the ledger). Pass exclude_ledger=False to test committing it.
    """
    EPOCH = 1767225600  # 2026-01-01T00:00:00Z

    def __init__(self, path=None, *, exclude_ledger=True, branch="main"):
        self._owned = None
        if path is None:
            self._owned = temp_dir("repo-")
            path = self._owned / "proj"
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.env = git_env()
        self.branch = branch
        self.commits = []
        self.git("init", "-q", "-b", branch)
        with open(self.path / ".git" / "config", "a", encoding="utf-8") as f:
            f.write(f"[user]\n\tname = {GIT_NAME}\n\temail = {GIT_EMAIL}\n"
                    "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
                    "[core]\n\tautocrlf = false\n[gc]\n\tauto = 0\n")
        if exclude_ledger:
            self.exclude(".code-review/")

    # -- plumbing
    def git(self, *args, check=True, env=None):
        """Run git in the repository; returns stdout without the trailing newline. Raises RuntimeError with git's
        stderr when check and the exit code is not 0."""
        p = subprocess.run(["git", *[str(a) for a in args]], cwd=str(self.path), env=env or self.env,
                           capture_output=True, text=True, timeout=60)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(map(str, args))} failed ({p.returncode}): {p.stderr.strip()}")
        return p.stdout.rstrip("\n")

    def exclude(self, pattern):
        """Add a pattern to .git/info/exclude (local ignore, not committed)."""
        ex = self.path / ".git" / "info" / "exclude"
        ex.parent.mkdir(parents=True, exist_ok=True)
        with open(ex, "a", encoding="utf-8") as f:
            f.write(pattern + "\n")

    # -- files
    def write(self, rel, text):
        """Write a file (str or bytes, '\\n' line endings kept as given), creating folders. Returns its Path."""
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return p

    def read(self, rel):
        return (self.path / rel).read_text(encoding="utf-8")

    def delete(self, rel):
        """Delete a file from the working tree (commit to record it)."""
        (self.path / rel).unlink()

    # -- history
    def _dated_env(self):
        n = len(self.commits) + 1
        stamp = f"@{self.EPOCH + 60 * n} +0000"
        return dict(self.env, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)

    def commit(self, message="commit", *, add_all=True):
        """Stage everything (unless add_all=False) and commit, even if nothing changed. Returns the full sha."""
        if add_all:
            self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "--no-verify", "-m", message, env=self._dated_env())
        sha = self.head()
        self.commits.append(sha)
        return sha

    def head(self):
        return self.git("rev-parse", "HEAD")

    def rev_parse(self, ref):
        return self.git("rev-parse", ref)

    def has_object(self, sha):
        """True if the object (commit, blob ...) still exists in the repository."""
        return subprocess.run(["git", "cat-file", "-e", sha], cwd=str(self.path), env=self.env,
                              capture_output=True).returncode == 0

    def tag(self, name, ref="HEAD", message=None):
        """Create a tag (annotated when message is given). Returns the commit sha it points at."""
        if message is None:
            self.git("tag", name, ref)
        else:
            self.git("tag", "-a", name, ref, "-m", message, env=self._dated_env())
        return self.rev_parse(f"{name}^{{commit}}")

    def tags(self):
        out = self.git("tag", "-l")
        return out.split("\n") if out else []

    def subrepo(self, name, **kw):
        """A separate repository in a sub-folder (not a submodule), ignored by this one. For multi-repo projects:
        record it in the ledger with repositories.path = name."""
        self.exclude(f"/{name}/")
        return ScratchRepo(self.path / name, **kw)

    def rewrite_history(self, message="rewritten history", drop_tags=True):
        """Replace the whole history with one new root commit holding the current tree, then expire reflogs and prune,
        so every earlier commit sha (and blobs only they held) is gone. Tags are deleted too unless drop_tags=False
        (a tag keeps its commit reachable). Returns the new head sha. Use it to test the excerpt fallback."""
        self.git("checkout", "-q", "--orphan", "rewritten-tmp")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "--no-verify", "-m", message, env=self._dated_env())
        self.git("branch", "-D", self.branch)
        self.git("branch", "-m", self.branch)
        if drop_tags:
            for t in self.tags():
                self.git("tag", "-d", t)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "-q", "--prune=now")
        sha = self.head()
        self.commits = [sha]
        return sha

    def remove(self):
        """Delete the repository folder entirely (to test the "repository gone" fallback)."""
        shutil.rmtree(self.path, ignore_errors=True)

    def cleanup(self):
        """Remove the temporary folder this object created (optional; the session folder goes at exit anyway)."""
        if self._owned:
            shutil.rmtree(self._owned, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


# ---------------------------------------------------------------- ledgers

def _fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Clock:
    """Deterministic timestamps in the ledger's own format (2026-11-01T09:00:00.000Z, the same shape as core.now()
    and the schema's strftime('%Y-%m-%dT%H:%M:%fZ')). tick() returns the current time, then advances one step."""

    def __init__(self, start="2026-11-01T09:00:00.000Z", step=timedelta(minutes=1)):
        self.t = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        self.step = step

    def peek(self):
        return _fmt(self.t)

    def tick(self):
        s = _fmt(self.t)
        self.t += self.step
        return s

    def advance(self, **delta):
        """Jump forward, e.g. clock.advance(days=8)."""
        self.t += timedelta(**delta)


# Columns that default to "now" in the schema; the insert helpers fill them from the Clock unless given.
DEFAULT_COMMIT = "a" * 40   # the commit Ledger.location records for a review that has none for the repository

TIMESTAMP_COLUMNS = ("created_at", "started_at", "updated_at", "opened_at", "checked_at", "asked_at",
                     "recorded_at", "adopted_at", "at")

_template = {}


def _template_ledger(ledger_py):
    """A ledger made once per process by the real `init` command; Ledger copies it (much faster than init each time)."""
    key = str(ledger_py)
    if key not in _template:
        proj = temp_dir("template-") / "template"
        proj.mkdir()
        p = run_ledger("--root", proj, "init", ledger_py=ledger_py)
        if p.returncode != 0:
            raise RuntimeError(f"ledger init failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        _template[key] = proj / ".code-review"
    return _template[key]


class Ledger:
    """A ledger in a project folder.

    Ledger()                         new ScratchRepo, ledger initialised in it
    Ledger(repo)                     in an existing ScratchRepo (or any folder Path)
    Ledger(repo, real_init=True)     run `ledger.py init` for this project instead of copying a template made by init
    Ledger(..., ledger_py=path)      use another built ledger.py (implies real_init)

    Attributes: repo (ScratchRepo or None), root, dir (.code-review), db, con, clock, module (imported tool),
    ledger_py. The connection has foreign_keys ON and sqlite3.Row rows; it is in Python's default transaction mode,
    so call commit() (run() does it for you) before another process reads.

    Insert helpers return the new row id and accept any extra column as a keyword; every timestamp column the table
    has (TIMESTAMP_COLUMNS) is filled from self.clock unless passed. Codes (R-0001, ISS-0001, ...) are numbered
    automatically when not given.
    """

    def __init__(self, repo=None, *, real_init=False, ledger_py=None, clock=None):
        if repo is None:
            repo = ScratchRepo()
        self.repo = repo if isinstance(repo, ScratchRepo) else None
        self.root = Path(repo.path if isinstance(repo, ScratchRepo) else repo).resolve()
        self.dir = self.root / ".code-review"
        self.db = self.dir / "ledger.sqlite"
        self.ledger_py = Path(ledger_py) if ledger_py else built_ledger_path()
        self.clock = clock or Clock()
        self._module = None
        self._cols = {}
        self._seq = {}
        if real_init or ledger_py:
            p = self.run("init")
            if p.returncode != 0:
                raise RuntimeError(f"ledger init failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        else:
            shutil.copytree(str(_template_ledger(self.ledger_py)), str(self.dir))
            con = sqlite3.connect(str(self.db))
            con.execute("UPDATE meta SET value=? WHERE key='project_name'", (self.root.name,))
            con.execute("UPDATE meta SET value=? WHERE key='ledger_location'", (str(self.dir),))
            con.commit()
            con.close()
        self.con = self.connect()

    # -- connections and the tool
    def connect(self):
        """A new connection like `con`: foreign_keys ON, sqlite3.Row rows."""
        con = sqlite3.connect(str(self.db), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    @property
    def module(self):
        """The imported ledger tool these tests run (load_ledger_module for this ledger_py)."""
        if self._module is None:
            self._module = load_ledger_module(self.ledger_py)
        return self._module

    def run(self, *args, **kw):
        """Commit, then run `ledger.py --root <project> <args>` as a CLI; returns CompletedProcess (text)."""
        if getattr(self, "con", None) is not None:
            self.con.commit()
        return run_ledger("--root", self.root, *args, ledger_py=self.ledger_py, **kw)

    def run_ok(self, *args, **kw):
        """run(), raising AssertionError with the output when the exit code is not 0."""
        p = self.run(*args, **kw)
        if p.returncode != 0:
            raise AssertionError(f"ledger.py {' '.join(map(str, args))} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
        return p

    def commit(self):
        self.con.commit()

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- queries
    def x(self, sql, args=()):
        return self.con.execute(sql, args)

    def scalar(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return None if r is None else r[0]

    def one(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return None if r is None else dict(r)

    def all(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def count(self, table_or_view, where="1", args=()):
        return self.scalar(f'SELECT count(*) FROM "{table_or_view}" WHERE {where}', args)

    def id_of(self, table, code):
        """Row id from a code column (reviews, issues, plans, gates, decisions)."""
        return self.scalar(f'SELECT id FROM "{table}" WHERE code=?', (code,))

    def columns(self, table):
        if table not in self._cols:
            self._cols[table] = [r[1] for r in self.con.execute(f'PRAGMA table_info("{table}")')]
        return self._cols[table]

    # -- generic writes
    def insert(self, table, **cols):
        """INSERT one row; fills missing timestamp columns from the clock (one tick per row). Returns lastrowid."""
        have = self.columns(table)
        stamp = None
        for c in TIMESTAMP_COLUMNS:
            if c in have and c not in cols:
                stamp = stamp or self.clock.tick()
                cols[c] = stamp
        for k, v in list(cols.items()):
            if isinstance(v, (list, dict)):
                cols[k] = json.dumps(v)
        names = ", ".join(f'"{k}"' for k in cols)
        marks = ", ".join("?" for _ in cols)
        cur = self.con.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', tuple(cols.values()))
        return cur.lastrowid

    def update(self, table, row_id, **cols):
        """UPDATE one row by id (triggers run as in real use). A status or severity change of an issue or step
        without change_note gets the note "test change" (schema 3 refuses such a change without one); to test that
        refusal, write the UPDATE yourself."""
        if table in ("issues", "steps") and ("status" in cols or "severity" in cols) and "change_note" not in cols:
            cols["change_note"] = "test change"
        if table == "issues" and cols.get("status") in ("accepted-risk", "wont-fix") and "resolution" not in cols \
                and not self.scalar("SELECT NULLIF(trim(resolution), '') FROM issues WHERE id=?", (row_id,)):
            cols["resolution"] = "test reason"   # schema 7 refuses these closings without a resolution
        sets = ", ".join(f'"{k}"=?' for k in cols)
        self.con.execute(f'UPDATE "{table}" SET {sets} WHERE id=?', (*cols.values(), row_id))

    CODE_TABLES = {"R-": "reviews", "ISS-": "issues", "P-": "plans", "D-": "decisions", "GATE-": "gates",
                   "REQ-": "requirements", "G-": "design_goals"}

    def _code(self, prefix, width=4):
        table = self.CODE_TABLES.get(prefix)
        if table:  # codes are sequential in the ledger (schema 3): the next number after the highest one there
            have = self.scalar(f'SELECT max(CAST(substr(code, {len(prefix) + 1}) AS INTEGER)) FROM "{table}" '
                               f'WHERE code LIKE ?', (prefix + "%",))
            return f"{prefix}{(have or 0) + 1:0{width}d}"
        n = self._seq.get(prefix, 0) + 1
        self._seq[prefix] = n
        return f"{prefix}{n:0{width}d}"

    # -- helpers, one per main table
    def repository(self, name="demo", path=".", **kw):
        kw.setdefault("default_branch", "main")
        return self.insert("repositories", name=name, path=path, **kw)

    def default_repository(self):
        """The first repository; created (name 'demo', path '.') if there is none."""
        rid = self.scalar("SELECT min(id) FROM repositories")
        return rid if rid is not None else self.repository()

    def actor(self, kind="manager", name=None, **kw):
        return self.insert("actors", kind=kind, name=name or f"{kind} {self._code(kind + '-', 1)}", **kw)

    def design_goal(self, code=None, text="Every rule is a setting.", **kw):
        return self.insert("design_goals", code=code or self._code("G-", 2), text=text, **kw)

    def review(self, code=None, *, commit_sha=None, repository_id=None, scope="full", status="running",
               title=None, **kw):
        """A review; with commit_sha also its review_repositories row (repository default_repository()). By default it
        is inserted approved and running (approved_at from the clock), since findings and reviewer runs need an
        approved review (schema 3)."""
        if status not in ("planning", "awaiting-approval", "abandoned"):
            kw.setdefault("approved_at", self.clock.tick())
        code = code or self._code("R-")
        rid = self.insert("reviews", code=code, title=title or f"Review {code}", scope=scope, status=status, **kw)
        if commit_sha:
            self.review_repository(rid, commit_sha, repository_id=repository_id)
        return rid

    def review_repository(self, review_id, commit_sha, *, repository_id=None, branch="main", **kw):
        return self.insert("review_repositories", review_id=review_id,
                           repository_id=repository_id or self.default_repository(),
                           commit_sha=commit_sha, branch=branch, **kw)

    def review_domain(self, review_id, domain_code, decision="included", reason="In scope.", **kw):
        return self.insert("review_domains", review_id=review_id, domain_code=domain_code, decision=decision,
                           reason=reason, **kw)

    def reviewer_run(self, review_id, domain_code, actor_id=None, label=None, scope="all", status="finished", **kw):
        """A reviewer run; the review_domains row including domain_code is made first if missing (schema 3 needs it)."""
        if not self.scalar("SELECT 1 FROM review_domains WHERE review_id=? AND domain_code=? AND decision='included'",
                           (review_id, domain_code)):
            self.review_domain(review_id, domain_code)
        actor_id = actor_id or self.actor("reviewer", f"{domain_code} reviewer")
        return self.insert("reviewer_runs", review_id=review_id, domain_code=domain_code, actor_id=actor_id,
                           label=label or self._code(f"{domain_code}-", 1), scope=scope, status=status, **kw)

    def finding(self, review_id, point_id=None, *, severity="M", kind="defect", domain_code="spec", sub=None,
                summary=None, issue_id=None, match_type="new", **kw):
        """A finding. issue_id is applied afterwards with match() (an UPDATE), the way matching is done for real, so
        the matching trigger runs. To insert with issue_id set directly, use insert('findings', ...)."""
        point_id = point_id or self._code(f"{domain_code.upper()[:4]}-{severity}", 1)
        fid = self.insert("findings", review_id=review_id, point_id=point_id, severity=severity, kind=kind,
                          domain_code=domain_code, sub=sub or point_id.lower(),
                          summary=summary or f"Finding {point_id}.", **kw)
        if issue_id is not None:
            self.match(fid, issue_id, match_type)
        return fid

    def location(self, finding_id, path="src/app.py", line_start=1, line_end=None, *, repository_id=None, **kw):
        """A finding_locations row; commit_sha left NULL takes the review's commit (trigger). The location's repository
        must be one its review looked at (schema 3): when the review has no review_repositories row for it, one is
        made with commit DEFAULT_COMMIT."""
        repository_id = repository_id or self.default_repository()
        if "role" not in kw and self.scalar("SELECT 1 FROM finding_locations WHERE finding_id=? AND role='primary'",
                                            (finding_id,)):
            kw["role"] = "also"   # one primary location per finding (schema 3); later ones default to 'also'
        rid = self.scalar("SELECT review_id FROM findings WHERE id=?", (finding_id,))
        if rid is not None and not self.scalar("SELECT 1 FROM review_repositories WHERE review_id=? AND repository_id=?",
                                               (rid, repository_id)):
            self.review_repository(rid, DEFAULT_COMMIT, repository_id=repository_id)
        return self.insert("finding_locations", finding_id=finding_id,
                           repository_id=repository_id, path=path,
                           line_start=line_start, line_end=line_end if line_end is not None else line_start, **kw)

    def issue(self, code=None, *, first_seen_review_id, title=None, severity="M", domain_code="spec", kind="defect",
              sub=None, theme_code=None, **kw):
        code = code or self._code("ISS-")
        sub = sub or code.lower()
        if kw.get("status") in ("accepted-risk", "wont-fix"):
            kw.setdefault("resolution", "test reason")   # schema 7 refuses these closings without a resolution
        kw.setdefault("fingerprint", f"{domain_code}:{theme_code or ''}:{sub}")
        return self.insert("issues", code=code, title=title or f"Issue {code}", severity=severity,
                           domain_code=domain_code, kind=kind, sub=sub, theme_code=theme_code,
                           first_seen_review_id=first_seen_review_id, **kw)

    def match(self, finding_id, issue_id, match_type="new", confidence="certain", rationale=None):
        """Match a finding to an issue with an UPDATE (fires tr_finding_matched)."""
        self.update("findings", finding_id, issue_id=issue_id, match_type=match_type, match_confidence=confidence,
                    match_rationale=rationale)

    def check_row(self, review_id, issue_id, outcome="still-present", **kw):
        """A review_issue_checks row (fires tr_issue_checked). A fixed, regressed or changed check without evidence
        gets "test evidence" (schema 7 requires it); pass evidence=None to test that refusal."""
        if outcome in ("fixed", "regressed", "changed"):
            kw.setdefault("evidence", "test evidence")
        return self.insert("review_issue_checks", review_id=review_id, issue_id=issue_id, outcome=outcome, **kw)

    def plan(self, code=None, title=None, status="drafting", *, reviews=(), **kw):
        code = code or self._code("P-")
        pid = self.insert("plans", code=code, title=title or f"Plan {code}", status=status, **kw)
        for r in reviews:
            self.plan_review(pid, r)
        return pid

    def plan_review(self, plan_id, review_id):
        return self.insert("plan_reviews", plan_id=plan_id, review_id=review_id)

    def phase(self, plan_id, number, title=None, goal="Do the phase.", **kw):
        return self.insert("phases", plan_id=plan_id, number=number, title=title or f"Phase {number}", goal=goal,
                           **kw)

    def step(self, plan_id, phase_id, number, title=None, change="Change it.", proof="A test proves it.", *,
             findings=(), depends_on=(), **kw):
        sid = self.insert("steps", plan_id=plan_id, phase_id=phase_id, number=number,
                          title=title or f"Step {number}", change=change, proof=proof, **kw)
        for f in findings:
            self.step_finding(sid, f)
        for d in depends_on:
            self.dependency(sid, d)
        return sid

    def step_finding(self, step_id, finding_id, multi_reason=None):
        """A coverage link; the finding's review is added to the step's plan (plan_reviews) first if missing."""
        self.x("""INSERT OR IGNORE INTO plan_reviews(plan_id, review_id)
                  SELECT s.plan_id, f.review_id FROM steps s, findings f WHERE s.id=? AND f.id=?""", (step_id, finding_id))
        return self.insert("step_findings", step_id=step_id, finding_id=finding_id, multi_reason=multi_reason)

    def dependency(self, step_id, depends_on_step_id):
        return self.insert("step_dependencies", step_id=step_id, depends_on_step_id=depends_on_step_id)

    def step_commit(self, step_id, commit_sha, *, repository_id=None, **kw):
        return self.insert("step_commits", step_id=step_id, commit_sha=commit_sha,
                           repository_id=repository_id or self.default_repository(), **kw)

    def guard_test(self, issue_id, test_path="tests/test_x.py", test_name="test_x", **kw):
        return self.insert("guard_tests", issue_id=issue_id, test_path=test_path, test_name=test_name, **kw)

    def decision(self, code=None, question="Which way?", **kw):
        return self.insert("decisions", code=code or self._code("D-"), question=question, **kw)

    def gate(self, code=None, kind="decision", title=None, question="Which way?", **kw):
        """A gate; options may be a list of {"value","label","description"} (stored as JSON). A gate inserted answered
        or applied without an answer gets answer_text "test answer", and applied without applied_note gets "applied
        in a test" (schema 3 requires both)."""
        if kw.get("status") in ("answered", "applied") and kw.get("answer_value") is None and kw.get("answer_text") is None:
            kw["answer_text"] = "test answer"
        if kw.get("status") == "applied":
            kw.setdefault("applied_note", "applied in a test")
        code = code or self._code("GATE-")
        return self.insert("gates", code=code, kind=kind, title=title or f"Gate {code}", question=question, **kw)

    def relation(self, from_issue_id, to_issue_id, relation="related-to", rationale="Related.", **kw):
        return self.insert("issue_relations", from_issue_id=from_issue_id, to_issue_id=to_issue_id,
                           relation=relation, rationale=rationale, **kw)

    def metric(self, review_id, name="mutation_score", value=50.0, method="measured", **kw):
        return self.insert("metrics", review_id=review_id, name=name, value=value, method=method, **kw)

    def events(self, entity_type=None, entity_id=None):
        """(entity_type, entity_id, event, from_value, to_value) tuples in order, optionally filtered."""
        sql, args = "SELECT entity_type, entity_id, event, from_value, to_value FROM events WHERE 1", []
        if entity_type:
            sql += " AND entity_type=?"; args.append(entity_type)
        if entity_id is not None:
            sql += " AND entity_id=?"; args.append(entity_id)
        return [tuple(r) for r in self.con.execute(sql + " ORDER BY id", args)]


# ---------------------------------------------------------------- the demo data set

class DemoExpectationError(AssertionError):
    """A seed_demo expectation did not hold (the ledger's rules behave differently than the demo expects)."""


def _expect(cond, message):
    if not cond:
        raise DemoExpectationError(message)


DEMO_OWNER = "Demo Owner"

DEMO_FILES_1 = {
    "src/auth.py": ("import random\n"
                    "def make_code():\n"
                    "    return f\"{random.randint(0, 999999):06d}\"\n"
                    "def check(code, expected):\n"
                    "    return code == expected\n"),
    "src/timeoff.py": ("NOTICE_DAYS = 14\n"
                       "def can_request(user, days_ahead, weekend):\n"
                       "    if days_ahead < NOTICE_DAYS:\n"
                       "        return False\n"
                       "    if user.is_admin:\n"
                       "        return True\n"
                       "    return not weekend\n"),
}
DEMO_FILES_2 = {
    "src/auth.py": ("import secrets\n"
                    "def make_code():\n"
                    "    return f\"{secrets.randbelow(1000000):06d}\"\n"
                    "def check(code, expected):\n"
                    "    return code == expected\n"),
}


def seed_demo(ledger):
    """Fill `ledger` with the demo: two reviews of a small sample app (sign-in codes and a rule for requests),
    matching across them, a plan with a done step and a step waiting on the owner, a decision, two open gates and
    metrics. Writes src/auth.py and src/timeoff.py into ledger.repo and commits twice (the commits the two reviews
    looked at), so every cited file exists. Every expectation is checked; a failed one raises DemoExpectationError. The work is committed.

    Returns a dict of the ids created:
      commits [c1, c2]; repository; actors {owner, manager, security, spec}; reviews {"R-0001", "R-0002"};
      runs {"R-0001/security", ...}; findings {"R-0001/SEC-H1", ...}; issues {"ISS-0001".."ISS-0004"};
      plan; phases {1, 2}; steps {1, 2, 3}; decisions {"D-0001"}; gates {"GATE-0001", "GATE-0002"}.
    """
    repo = ledger.repo
    if repo is None:
        raise ValueError("seed_demo needs a Ledger on a ScratchRepo (it commits the demo files)")
    L = ledger
    for p, t in DEMO_FILES_1.items():
        repo.write(p, t)
    c1 = repo.commit("Initial requests and sign-in")
    for p, t in DEMO_FILES_2.items():
        repo.write(p, t)
    c2 = repo.commit("Use secrets for sign-in codes")
    ids = {"commits": [c1, c2], "runs": {}, "findings": {}, "issues": {}, "reviews": {}}

    ids["repository"] = L.repository("demo", ".", permalink_template=
                                     "https://github.com/example/demo/blob/{sha}/{path}#L{start}-L{end}")
    owner = L.actor("owner", DEMO_OWNER)
    manager = L.actor("manager", "Review manager", model="claude-opus-5-5", harness="Claude Code")
    sec = L.actor("reviewer", "security reviewer", model="claude-opus-5-5", parent_actor_id=manager)
    spec = L.actor("reviewer", "spec reviewer", model="claude-opus-5-5", parent_actor_id=manager)
    ids["actors"] = {"owner": owner, "manager": manager, "security": sec, "spec": spec}
    g1 = L.design_goal("G-01", "Every rule and behavior is a setting.", source="owner")
    g2 = L.design_goal("G-02", "No internal names on screen.", source="owner")

    def review(code, sha, prev, status, subject):
        # Inserted planning, then moved through the approval to running (findings and runs need an approved review);
        # R-0001 moves on to handed-off once its points are in.
        rid = L.review(code, title=f"Comprehensive review {code}", scope="second" if prev else "full",
                       status="planning", request_text="Run the full system review", requested_by_actor_id=owner,
                       manager_actor_id=manager, approved_by_actor_id=owner, approved_at=L.clock.tick(),
                       previous_review_id=prev, verdict="Solid core; sign-in codes and notice rules need work.")
        for st in ("awaiting-approval", "approved", "running"):
            L.update("reviews", rid, status=st, change_actor_id=owner if st == "approved" else manager)
        L.review_repository(rid, sha, repository_id=ids["repository"], commit_subject=subject)
        L.insert("review_goals", review_id=rid, goal_id=g1)
        L.insert("review_goals", review_id=rid, goal_id=g2)
        L.review_domain(rid, "security", "included", "Sign-in codes and sessions exist", reviewers_planned=1)
        L.review_domain(rid, "spec", "included", "The owner stated the rules for requests", reviewers_planned=1)
        L.review_domain(rid, "database", "skipped", "No database in this project", reviewers_planned=0)
        ids["runs"][f"{code}/security"] = L.reviewer_run(rid, "security", sec, "security", "sign-in",
                                                         items_reviewed=4, high_count=1, medium_count=0, low_count=0)
        ids["runs"][f"{code}/spec"] = L.reviewer_run(rid, "spec", spec, "spec", "request rules", items_reviewed=6,
                                                     high_count=0, medium_count=1, low_count=1)
        ids["reviews"][code] = rid
        return rid

    def finding(rid, code, run, pid, sev, kind, dom, theme, sub, summ, before, rec, path, a, b, sym):
        fid = L.finding(rid, pid, severity=sev, kind=kind, domain_code=dom, theme_code=theme, sub=sub, summary=summ,
                        before_state=before, recommendation=rec, recommended_test=f"test_{sub.replace('-', '_')}",
                        fingerprint=f"{dom}:{theme}:{sub}", verification="confirmed",
                        verified_by_actor_id=manager, reviewer_run_id=ids["runs"][f"{code}/{run}"])
        L.location(fid, path, a, b, repository_id=ids["repository"], symbol=sym)
        ids["findings"][f"{code}/{pid}"] = fid
        return fid

    def new_issue(code, fid, rid, title, dom, theme, sub, sev, kind, before):
        iid = L.issue(code, first_seen_review_id=rid, title=title, domain_code=dom, theme_code=theme, sub=sub,
                      kind=kind, severity=sev, before_state=before, change_actor_id=manager)
        L.match(fid, iid, "new", "certain")
        ids["issues"][code] = iid
        return iid

    # ---- first review
    r1 = review("R-0001", c1, None, "handed-off", "Initial requests and sign-in")
    f1 = finding(r1, "R-0001", "security", "SEC-H1", "H", "security", "security", "sign-in", "code-randomness",
                 "Sign-in codes come from a predictable random generator.",
                 "make_code uses random.randint, which is not suitable for secrets.", "Use secrets.randbelow.",
                 "src/auth.py", 1, 3, "make_code")
    f2 = finding(r1, "R-0001", "spec", "SPEC-M1", "M", "defect", "spec", "spec-mismatch", "notice-period",
                 "Notice period is hardcoded to 14 days instead of a setting.",
                 "can_request refuses anything under 14 days; the owner asked for it to be a setting.",
                 "Read the notice period from program settings.", "src/timeoff.py", 2, 4, "can_request")
    f3 = finding(r1, "R-0001", "spec", "SPEC-L1", "L", "defect", "spec", "spec-mismatch", "admin-bypass",
                 "Admins skip the weekend limit entirely.", "Admins bypass the weekend-off rule.",
                 "Apply the same rule with an override that is recorded.", "src/timeoff.py", 5, 6, "can_request")
    i1 = new_issue("ISS-0001", f1, r1, "Predictable sign-in codes", "security", "sign-in", "code-randomness", "H",
                   "security", "Codes generated with random.randint.")
    i2 = new_issue("ISS-0002", f2, r1, "Notice period hardcoded", "spec", "spec-mismatch", "notice-period", "M",
                   "defect", "14 days in code.")
    i3 = new_issue("ISS-0003", f3, r1, "Admins bypass the weekend limit", "spec", "spec-mismatch", "admin-bypass",
                   "L", "defect", "Admin bypass.")
    for st in ("verifying", "indexing", "consolidating", "handed-off"):
        L.update("reviews", r1, status=st, change_actor_id=manager)

    # ---- plan
    pid = L.plan("P-0001", "Fix plan for R-0001", "drafting", reviews=(r1,), approved_by_actor_id=owner,
                 approved_at=L.clock.tick())
    ph1 = L.phase(pid, 1, "Safety net", "Every later step is checked by tests.")
    ph2 = L.phase(pid, 2, "Fix first", "Confirmed defects fixed with failing tests first.")
    s1 = L.step(pid, ph1, 1, "Run every suite in CI", "Add a CI workflow.", "CI fails on a broken test.",
                section="CI")
    s2 = L.step(pid, ph2, 2, "Use secrets for sign-in codes", "Replace random with secrets in make_code.",
                "test_code_uses_secrets fails before, passes after.")
    s3 = L.step(pid, ph2, 3, "Make the notice period a setting", "Add notice_days setting.",
                "Tests with 7 and 21 days.", owner_need="decision", owner_question="Should the default stay 14 days?",
                behavior_change=1, behavior_note="Programs can set their own notice.")
    ids.update(plan=pid, phases={1: ph1, 2: ph2}, steps={1: s1, 2: s2, 3: s3})
    L.dependency(s2, s1)
    L.dependency(s3, s1)
    try:
        L.con.execute("SAVEPOINT backward")
        L.dependency(s1, s3)
    except sqlite3.IntegrityError as e:
        _expect("earlier step" in str(e), f"backward dependency refused with an unexpected message: {e}")
    else:
        raise DemoExpectationError("a step was allowed to depend on a later step (tr_dep_order)")
    finally:
        L.con.execute("ROLLBACK TO backward")
        L.con.execute("RELEASE backward")
    L.step_finding(s2, f1)
    L.step_finding(s3, f2)
    L.step_finding(s1, f3)
    for st in ("awaiting-approval", "approved", "in-progress"):
        L.update("plans", pid, status=st, change_actor_id=owner if st == "approved" else manager)
    L.update("steps", s2, status="doing", change_actor_id=manager, change_note="Started the change.")
    L.update("steps", s2, status="done", progress_note="Sign-in codes now come from a secure generator.",
             change_commit_sha=c2, change_note="Sign-in codes now come from a secure generator.")
    L.step_commit(s2, c2, repository_id=ids["repository"], branch="main", summary="Use secrets for sign-in codes")
    L.update("issues", i1, status="fixed", after_state="Codes come from secrets.randbelow.", change_step_id=s2,
             change_commit_sha=c2, change_actor_id=manager, change_note="Step 2 done")
    L.guard_test(i1, "tests/test_auth.py", "test_code_uses_secrets", repository_id=ids["repository"], step_id=s2,
                 added_commit_sha=c2, failed_before_fix=1, passes_after_fix=1)
    L.update("steps", s3, status="waiting-owner", change_note="Waiting for the owner: Should the default stay 14 days?")
    _expect(L.scalar("SELECT status FROM steps WHERE id=?", (s2,)) == "done", "step 2 should be done")
    _expect(L.scalar("SELECT done_at IS NOT NULL FROM steps WHERE id=?", (s2,)) == 1, "step 2 should have done_at")

    # ---- decision and gates
    d1 = L.decision("D-0001", "Should the default notice period stay 14 days?", status="open", review_id=r1,
                    step_id=s3, asked_by_actor_id=manager)
    gate1 = L.gate("GATE-0001", "decision", "Default notice period",
                   "Should the default notice period stay 14 days when it becomes a setting?",
                   context="Today the code refuses requests made less than 14 days ahead. Programs will be able to "
                           "change it.",
                   options=[{"value": "keep-14", "label": "Keep 14 days",
                             "description": "Current behaviour stays the default."},
                            {"value": "use-7", "label": "Change to 7 days"}],
                   review_id=r1, plan_id=pid, step_id=s3, decision_id=d1, issue_id=i2, opened_by_actor_id=manager)
    ids["decisions"] = {"D-0001": d1}

    # ---- second review, after the issues were opened
    r2 = review("R-0002", c2, r1, "awaiting-approval", "Use secrets for sign-in codes")
    L.check_row(r2, i1, "fixed", evidence="make_code now uses secrets.randbelow (src/auth.py:3)", actor_id=sec)
    _expect(L.scalar("SELECT status FROM issues WHERE id=?", (i1,)) == "verified",
            "a 'fixed' check on a fixed issue should verify it")
    f4 = finding(r2, "R-0002", "spec", "SPEC-M1", "M", "defect", "spec", "spec-mismatch", "notice-period",
                 "Notice period is still 14 days in code.", "Unchanged since R-0001.", "Read it from settings.",
                 "src/timeoff.py", 2, 4, "can_request")
    L.match(f4, i2, "still-present", "certain", "Same function and lines as ISS-0002")
    L.check_row(r2, i2, "still-present", finding_id=f4, actor_id=spec)
    f5 = finding(r2, "R-0002", "security", "SEC-M1", "M", "security", "security", "sign-in", "code-compare",
                 "Code check uses == instead of a constant-time compare.", "check compares codes with ==.",
                 "Use hmac.compare_digest.", "src/auth.py", 4, 5, "check")
    i4 = new_issue("ISS-0004", f5, r2, "Sign-in code compare is not constant-time", "security", "sign-in",
                   "code-compare", "M", "security", "== compare.")
    L.relation(i4, i1, "related-to", "Same sign-in code path as the earlier randomness issue",
               confidence="likely", established_in_review_id=r2, established_by_actor_id=manager)
    L.check_row(r2, i3, "not-checked", actor_id=spec)
    gate2 = L.gate("GATE-0002", "domain-approval", "Approve the domains for R-0002",
                   "Run these 2 domains with 2 reviewers?",
                   context="Included: Security, Spec conformance. Skipped: Database (no database).",
                   options=[{"value": "approve", "label": "Approve as listed"},
                            {"value": "add", "label": "Add skipped domains"}],
                   allow_text=1, review_id=r2, opened_by_actor_id=manager)
    ids["gates"] = {"GATE-0001": gate1, "GATE-0002": gate2}
    for rid, v in ((r1, 61.0), (r2, 74.5)):
        L.metric(rid, "mutation_score", v, "measured", domain_code="test-quality", scope="src", unit="%")
    L.commit()

    # ---- expectations on the end state
    def issue(code):
        return L.one("SELECT * FROM issues WHERE code=?", (code,))

    _expect(issue("ISS-0001")["status"] == "verified", "ISS-0001 should be verified")
    _expect(issue("ISS-0002")["times_seen"] == 2, "ISS-0002 should have been seen by 2 reviews")
    _expect(issue("ISS-0002")["last_seen_review_id"] == r2, "ISS-0002 should be last seen in R-0002")
    _expect(issue("ISS-0004")["status"] == "open", "ISS-0004 should be open")
    for code in ("GATE-0001", "GATE-0002"):
        _expect(L.scalar("SELECT status FROM gates WHERE code=?", (code,)) == "open", f"{code} should be open")
    for loc in L.all("""SELECT r.code, fl.commit_sha FROM finding_locations fl JOIN findings f ON f.id=fl.finding_id
                        JOIN reviews r ON r.id=f.review_id"""):
        want = c1 if loc["code"] == "R-0001" else c2
        _expect(loc["commit_sha"] == want, f"a {loc['code']} location should take that review's commit")
    for view in ("v_unmatched_findings", "v_unchecked_known_issues", "v_uncovered_findings", "v_coverage_problems"):
        _expect(L.count(view) == 0, f"{view} should be empty after seeding: {L.all(f'SELECT * FROM {view}')}")
    seq = [e[2:] for e in L.events("issue", i1) if e[2] in ("opened", "seen", "status-changed", "checked")]
    _expect(seq == [("opened", None, "open"), ("seen", None, "new"), ("status-changed", "open", "fixed"),
                    ("status-changed", "fixed", "verified"), ("checked", None, "fixed")],
            f"ISS-0001 history is not as expected: {seq}")

    # ---- regression probe: a finding of a later review matched to the verified issue as a regression makes it
    # regressed; then undone (R-0002 itself reported it fixed, so it cannot also raise it again)
    r3 = L.review(None, title="Probe review", previous_review_id=r2)
    probe = L.finding(r3, "SEC-H9", severity="H", kind="security", domain_code="security", theme_code="sign-in",
                      sub="code-randomness", summary="Test probe: randomness returned")
    L.match(probe, i1, "regression")
    got = L.one("SELECT status, times_seen FROM issues WHERE id=?", (i1,))
    _expect((got["status"], got["times_seen"]) == ("regressed", 2),
            f"a regression match on a verified issue should regress it and count the review: {got}")
    L.con.rollback()
    got = L.one("SELECT status, times_seen, resolved_at IS NOT NULL AS resolved FROM issues WHERE id=?", (i1,))
    _expect((got["status"], got["times_seen"], got["resolved"]) == ("verified", 1, 1),
            f"rolling back the probe should restore ISS-0001: {got}")
    return ids


def finish_demo(ledger, reviews=("R-0001", "R-0002")):
    """After seed_demo: fill-refs, tag-review for each review, then check, all through the CLI. Raises
    DemoExpectationError unless each exits 0 and every reference was read from git. Returns check's output."""
    p = ledger.run("fill-refs")
    _expect(p.returncode == 0 and "0 could not be read" in p.stdout, f"fill-refs failed:\n{p.stdout}{p.stderr}")
    for code in reviews:
        p = ledger.run("tag-review", code)
        _expect(p.returncode == 0, f"tag-review {code} failed:\n{p.stdout}{p.stderr}")
    p = ledger.run("check")
    _expect(p.returncode == 0, f"check failed after seeding:\n{p.stdout}{p.stderr}")
    return p.stdout


# ---------------------------------------------------------------- the viewer server

def free_port():
    """A TCP port on 127.0.0.1 that was free a moment ago."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Response:
    """status (int), headers (http.client.HTTPMessage), body (bytes); text and json() for convenience."""

    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body or b"null")

    def __repr__(self):
        return f"<Response {self.status} {self.body[:80]!r}>"


class ServerThread:
    """The viewer, running against a ledger, on a free port.

    mode="thread" (default): calls serve(root, db, port, False) from a private copy of the built module in a daemon
    thread. serve() keeps its server object to itself, so the copy's ThreadingHTTPServer is replaced by a subclass
    that records the instance; stop() then calls shutdown(). The copy's print is silenced.
    mode="process": runs `ledger.py --root R --db D serve --port P --no-open` as a subprocess (the real CLI path).

    Use as a context manager or call start()/stop() (stop is safe to call twice):

        with ServerThread(ledger) as srv:
            r = srv.get("/api/...")          # sends the page token unless token=False
            r = srv.post("/api/...", {"a": 1})
            raw = srv.raw(b"GET / HTTP/1.0\\r\\n\\r\\n")

    srv.launch_url is the URL serve() printed (http://127.0.0.1:<port>/#k=<token>); srv.token is its fragment token
    (the page itself holds no token). Requests send Host 127.0.0.1:<port>.
    """

    def __init__(self, ledger=None, *, root=None, db=None, mode="thread", ledger_py=None, start_timeout=10):
        if ledger is not None:
            root, db = ledger.root, ledger.db
            ledger_py = ledger_py or ledger.ledger_py
            if getattr(ledger, "con", None) is not None:
                ledger.con.commit()
        if root is None:
            raise ValueError("ServerThread needs a Ledger or root=")
        self.root = Path(root)
        self.db = Path(db) if db else self.root / ".code-review" / "ledger.sqlite"
        self.mode = mode
        self.ledger_py = Path(ledger_py) if ledger_py else built_ledger_path()
        self.start_timeout = start_timeout
        self.port = None
        self._token = None
        self.launch_url = None
        self._srv = self._thread = self._proc = None
        self.error = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def start(self):
        last = None
        for _ in range(5):
            self.port = free_port()
            try:
                (self._start_thread if self.mode == "thread" else self._start_process)()
                return self
            except OSError as e:  # port taken between free_port() and bind: try another
                last = e
        raise RuntimeError(f"could not start the viewer: {last}")

    def _start_thread(self):
        mod = load_ledger_module(self.ledger_py, fresh=True)
        captured, ready = [], threading.Event()
        base = mod.ThreadingHTTPServer

        class Capturing(base):
            daemon_threads = True

            def __init__(s, *a, **k):
                super().__init__(*a, **k)
                captured.append(s)
                ready.set()

        mod.ThreadingHTTPServer = Capturing
        printed = []
        mod.print = lambda *a, **k: printed.append(" ".join(map(str, a)))
        self.error = None
        umask = os.umask(0o022)  # serve() sets 077 for the process; give the test process its own back
        os.umask(umask)

        def target():
            try:
                mod.serve(self.root, self.db, self.port, False)
            except BaseException as e:  # noqa: B902 - reported to the test via self.error
                self.error = e
                ready.set()

        t = threading.Thread(target=target, name=f"viewer-{self.port}", daemon=True)
        t.start()
        if not ready.wait(self.start_timeout):
            raise RuntimeError("viewer did not start in time")
        if not captured:
            if isinstance(self.error, OSError):
                raise self.error
            raise RuntimeError(f"serve() failed: {self.error!r}")
        self._srv, self._thread = captured[0], t
        deadline = time.time() + self.start_timeout
        while not printed and time.time() < deadline:
            time.sleep(0.01)
        os.umask(umask)
        self._take_launch_url(" ".join(printed))

    def _start_process(self):
        cmd = [sys.executable, str(self.ledger_py), "--root", str(self.root), "--db", str(self.db), "serve",
               "--port", str(self.port), "--no-open"]
        self._proc = subprocess.Popen(cmd, env=git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                out, err = self._proc.communicate()
                code, self._proc = self._proc.returncode, None
                if b"in use" in err or b"Errno 98" in err:
                    raise OSError(err.decode(errors="replace"))
                raise RuntimeError(f"serve exited {code}: {err.decode(errors='replace')}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                self._take_launch_url(self._proc.stdout.readline().decode(errors="replace"))
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("viewer process did not start listening in time")

    def stop(self):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._thread.join(5)
            self._srv = self._thread = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.communicate()
            self._proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _take_launch_url(self, printed):
        m = re.search(r"(http://127\.0\.0\.1:\d+/#k=([A-Za-z0-9_\-]{16,}))", printed or "")
        if not m:
            raise RuntimeError(f"serve() printed no launch URL with a #k= token: {printed!r}")
        self.launch_url, self._token = m.group(1), m.group(2)

    # -- requests
    @property
    def token(self):
        if self._token is None:
            raise AssertionError("the viewer printed no token")
        return self._token

    def request(self, method, path, body=None, headers=None, token=True, timeout=10):
        """One HTTP request. body: bytes, str, or a dict/list sent as JSON (Content-Type application/json unless
        headers sets it). token=True adds X-Ledger-Token."""
        h = dict(headers or {})
        if token and "X-Ledger-Token" not in h:
            h["X-Ledger-Token"] = self.token
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body = body.encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return Response(r.status, r.headers, r.read())
        finally:
            c.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.request("POST", path, body={} if data is None else data, **kw)

    def raw(self, data, timeout=5):
        """Send raw bytes and return everything the server answers until it closes the connection."""
        chunks = []
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            s.sendall(data)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            while True:
                try:
                    b = s.recv(65536)
                except socket.timeout:
                    break
                if not b:
                    break
                chunks.append(b)
        return b"".join(chunks)
