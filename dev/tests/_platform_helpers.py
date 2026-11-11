"""Helpers for test_platform.py and test_git_guards.py. Standard library only; Python 3.9+."""
import ast
import sqlite3
from pathlib import Path

from dev.tests import fixtures

REPO_ROOT = fixtures.REPO_ROOT
MIN_PYTHON = (3, 9)

# Python sources that must stay valid Python 3.9 (the tool, its build, and the tests that run on 3.9 in CI).
PY39_SOURCES = ("dev/core.py", "dev/build.py", "dev/tests")


def python_sources():
    """(label, text) for every source that must parse as Python 3.9, including the freshly built ledger.py."""
    out = []
    for rel in PY39_SOURCES:
        p = REPO_ROOT / rel
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            out.append((str(f.relative_to(REPO_ROOT)), f.read_text(encoding="utf-8")))
    out.append(("built scripts/ledger.py", fixtures.built_ledger_path().read_text(encoding="utf-8")))
    return out


def parse_as(text, label, version=MIN_PYTHON):
    """ast.parse with the grammar of `version`; raises SyntaxError for newer syntax (match, except*, type params,
    parenthesized context managers are best-effort in ast's feature_version)."""
    return ast.parse(text, label, feature_version=version)


def schema_text():
    return (REPO_ROOT / "references" / "schema.sql").read_text(encoding="utf-8")


def fresh_schema_connection():
    """An in-memory database with the schema loaded, foreign keys on."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(schema_text())
    return con


def fk_pragma(con):
    return con.execute("PRAGMA foreign_keys").fetchone()[0]


ORPHAN_SQL = "INSERT INTO plan_reviews(plan_id, review_id) VALUES (999999, 999999)"
"""An insert whose two foreign keys point at rows that do not exist (plan_reviews has no other required column)."""


def is_fk_error(exc):
    return isinstance(exc, sqlite3.IntegrityError) and "FOREIGN KEY" in str(exc).upper()


def tree_files(base):
    """Every path below `base`, relative, as strings (to spot stray files a probe created)."""
    base = Path(base)
    return sorted(str(p.relative_to(base)) for p in base.rglob("*"))
