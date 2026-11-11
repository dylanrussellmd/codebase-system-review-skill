"""Helpers for the version and migration tests (plan steps 31-34). Not a test module.

    V1_SCHEMA            dev/tests/data/schema-v1.sql: references/schema.sql exactly as it was at commit ea00196
                         (schema version 1, what every ledger made before migrations existed has)
    V1_DEMO              dev/tests/data/ledger-v1-demo.sql: a SQL dump of a v1 ledger made by the ea00196 ledger.py
                         and filled with the demo data (fixtures.seed_demo + finish_demo)
    restore_v1(dir)      a project folder whose .code-review/ledger.sqlite is the v1 demo ledger; returns its db path
    variant(...)         build a ledger.py from a temporary copy of the sources with a changed tool version, an extra
                         (test-only) migration and the matching schema edit; returns the path of the built file
    objects(db)          {(type, name): normalized sql} of a ledger's schema (sqlite_master, without sqlite_*)
    counts(db)           {table: row count} for every table

Regenerate the v1 demo dump (only if it is lost; it is meant to stay frozen) with:

    python3 -m dev.tests._migrate_helpers --write-v1-demo
"""
import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from dev.tests import fixtures

DATA = Path(__file__).resolve().parent / "data"
V1_SCHEMA = DATA / "schema-v1.sql"
V1_DEMO = DATA / "ledger-v1-demo.sql"
V1_SCHEMA_SHA256 = "fbbcd4a812bf50766b9df9caf06fe365e9c23e0cb91efc006c954c7964132f15"  # ea00196:references/schema.sql
REVIEWED_COMMIT = "ea00196"

SOURCES = ("dev/core.py", "dev/build.py", "dev/page.html", "references/schema.sql", "SKILL.md")

def shipped_version():
    """The highest migration number in references/migrations (the shipped schema version)."""
    return max(int(f.name[:4]) for f in (fixtures.REPO_ROOT / "references" / "migrations").glob("[0-9][0-9][0-9][0-9]-*.sql"))


SHIPPED = shipped_version()
NEXT = SHIPPED + 1          # the test-only schema: one past the shipped one
TEST_MIGRATION = f"{NEXT:04d}-test-only"

# The test-only schema NEXT: themes gains a column through the SQLite table-rebuild recipe (themes is referenced by
# findings and issues, so the rebuild has to keep foreign keys intact).
THEMES_V1 = """CREATE TABLE themes (
  code       TEXT PRIMARY KEY,           -- 'authorization', 'logging-audit' ...
  name       TEXT NOT NULL,
  retired_at TEXT
);"""
THEMES_V2 = """CREATE TABLE themes (
  code        TEXT PRIMARY KEY,          -- 'authorization', 'logging-audit' ...
  name        TEXT NOT NULL,
  retired_at  TEXT,
  description TEXT                       -- test-only column of the test-only schema
);"""
REBUILD_THEMES = """-- test only: themes gains a description column
CREATE TABLE themes_new (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  retired_at  TEXT,
  description TEXT
);
INSERT INTO themes_new(code, name, retired_at) SELECT code, name, retired_at FROM themes;
DROP TABLE themes;
ALTER TABLE themes_new RENAME TO themes;
UPDATE themes SET description = 'Theme ' || name;
"""


def schema_v2(text):
    """references/schema.sql turned into the test-only schema NEXT that REBUILD_THEMES produces (the name is from
    when the shipped schema was 1)."""
    assert text.count(THEMES_V1) == 1, "the themes table changed; update dev/tests/_migrate_helpers.py"
    text = text.replace(THEMES_V1, THEMES_V2)
    text = re.sub(r"(?m)^(-- Code review ledger — schema version )\d+$", rf"\g<1>{NEXT}", text, count=1)
    old = f"VALUES ('schema_version','{SHIPPED}');"
    assert text.count(old) == 1, "the meta seed changed; update dev/tests/_migrate_helpers.py"
    text = text.replace(old, f"VALUES ('schema_version','{NEXT}');")
    m = list(re.finditer(r"(?m)^INSERT INTO schema_migrations\(version, description\) VALUES .*?\);$", text, re.S))
    assert len(m) == 1, "the schema_migrations seed changed; update dev/tests/_migrate_helpers.py"
    seed = m[0]
    text = (text[:seed.end() - 1] + f",\n  ({NEXT}, 'test only: themes gains a description column');"
            + text[seed.end():])
    return text


def copy_sources(root=None):
    """A temporary copy of everything dev/build.py reads; returns its root."""
    root = Path(root) if root else fixtures.temp_dir("variant-")
    for rel in SOURCES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(fixtures.REPO_ROOT / rel), str(root / rel))
    shutil.copytree(str(fixtures.REPO_ROOT / "references" / "migrations"), str(root / "references" / "migrations"))
    (root / "scripts").mkdir(exist_ok=True)
    return root


def set_version(root, version):
    p = root / "SKILL.md"
    text = p.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^version: .*$", f"version: {version}", text, count=1)
    assert n == 1, "SKILL.md has no version: line"
    p.write_text(new, encoding="utf-8")


def variant(version=None, migration=None, schema_edit=None, body_edit=None, name=TEST_MIGRATION + ".sql"):
    """Build a ledger.py from a copy of the sources. version: the tool version written into SKILL.md; migration: SQL
    of an extra migration file `name`; schema_edit: function applied to references/schema.sql; body_edit: function
    applied to dev/core.py. Returns the built file's path."""
    root = copy_sources()
    if version:
        set_version(root, version)
    if migration is not None:
        (root / "references" / "migrations" / name).write_text(migration, encoding="utf-8")
    if schema_edit:
        p = root / "references" / "schema.sql"
        p.write_text(schema_edit(p.read_text(encoding="utf-8")), encoding="utf-8")
    if body_edit:
        p = root / "dev" / "core.py"
        p.write_text(body_edit(p.read_text(encoding="utf-8")), encoding="utf-8")
    text = fixtures.load_build_module().build(root)
    out = root / "scripts" / "ledger.py"
    out.write_bytes(text.encode("utf-8"))
    return out


_v2 = None


def v2_ledger_py():
    """A tool whose schema is the test-only schema 2 (built once per process)."""
    global _v2
    if _v2 is None:
        _v2 = variant(migration=REBUILD_THEMES, schema_edit=schema_v2)
    return _v2


def restore_v1(project=None):
    """A project folder with the frozen v1 demo ledger; returns (project, db)."""
    project = Path(project) if project else fixtures.temp_dir("v1-") / "proj"
    (project / ".code-review").mkdir(parents=True, exist_ok=True)
    db = project / ".code-review" / "ledger.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(V1_DEMO.read_text(encoding="utf-8"))
    con.close()
    return project, db


def normalized(con, sql):
    return fixtures.load_ledger_module().normalize_sql(sql)


def objects(db):
    mod = fixtures.load_ledger_module()
    con = sqlite3.connect(str(db))
    try:
        return {(t, n): (tb, mod.normalize_sql(s)) for t, n, tb, s in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}
    finally:
        con.close()


def objects_of_sql(text):
    mod = fixtures.load_ledger_module()
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
        return {(t, n): (tb, mod.normalize_sql(s)) for t, n, tb, s in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}
    finally:
        con.close()


def counts(db):
    con = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def meta(db):
    con = sqlite3.connect(str(db))
    try:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_v1_demo():
    """Make V1_DEMO with the ledger.py of commit ea00196 (needs the git history of this repository)."""
    old = fixtures.temp_dir("ea00196-") / "ledger.py"
    old.write_bytes(subprocess.run(["git", "-C", str(fixtures.REPO_ROOT), "show", f"{REVIEWED_COMMIT}:scripts/ledger.py"],
                                   check=True, capture_output=True).stdout)
    repo = fixtures.ScratchRepo(fixtures.temp_dir("v1demo-") / "demo")
    L = fixtures.Ledger(repo, ledger_py=old)
    fixtures.seed_demo(L)
    fixtures.finish_demo(L)
    L.x("UPDATE meta SET value='demo-v1' WHERE key='project_name'")
    L.x("UPDATE meta SET value='2026-11-01T09:00:00.000Z' WHERE key='created_at'")
    L.commit()
    L.close()
    con = sqlite3.connect(str(L.db))
    text = "\n".join(con.iterdump()) + "\n"
    con.close()
    V1_DEMO.write_text("-- A schema-1 ledger made by ledger.py at commit ea00196 and filled with the demo data.\n"
                       "-- Frozen: regenerate only with python3 -m dev.tests._migrate_helpers --write-v1-demo\n"
                       + text, encoding="utf-8")
    print(f"wrote {V1_DEMO}")


if __name__ == "__main__" and "--write-v1-demo" in sys.argv:
    write_v1_demo()
