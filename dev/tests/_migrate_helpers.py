"""Helpers for the version and migration tests (plan steps 31-34). Not a test module.

    V1_SCHEMA            dev/tests/data/schema-v1.sql: references/schema.sql exactly as it was at commit ea00196
                         (schema version 1, what every ledger made before migrations existed has)
    V1_DEMO              dev/tests/data/ledger-v1-demo.sql: a SQL dump of a v1 ledger made by the ea00196 ledger.py
                         and filled with the demo data (fixtures.seed_demo + finish_demo)
    restore_v1(dir)      a project folder whose .code-review/ledger.sqlite is the v1 demo ledger; returns its db path
    FIXTURE(n)           dev/tests/data/ledger-v<n>.sql: a ledger at released schema n with a row in every table
    RELEASED             the released schemas, 1 .. SHIPPED - 1: each has a fixture
    restore(n, dir)      a project folder holding the schema-n fixture; returns (project, db)
    V16_INIT             dev/tests/data/ledger-v16-init.sql: `init` by the ledger.py of c06a6e6 (schema 16, with its
                         triggers); restore_v16() puts it in a project folder
    MIGRATION_HASHES     dev/tests/data/migrations.sha256: the sha256 of every released migration file
    variant(...)         build a ledger.py from a temporary copy of the sources with a changed tool version, an extra
                         (test-only) migration and the matching schema edit; returns the path of the built file
    objects(db)          {(type, name): normalized sql} of a ledger's schema (sqlite_master, without sqlite_*)
    counts(db)           {table: row count} for every table

Regenerate the v1 demo dump (only if it is lost; it is meant to stay frozen) with:

    python3 -m dev.tests._migrate_helpers --write-v1-demo

The fixtures of the released schemas come from the v1 demo, with migrations 2..n applied as migrate applies them
(views and triggers dropped first) and the rows of FILL added to each table the demo leaves empty, in the schema that
creates it. They hold tables, indexes and rows; migrate drops every view and trigger before it upgrades, so they hold
none. A new release adds the fixture of the schema it ships and appends its migration's line to MIGRATION_HASHES:

    python3 -m dev.tests._migrate_helpers --write-fixture N
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

# Rows later migrations seed into tables a v1 ledger already has: 0019 adds the features domain.
SEEDED_LATER = {"domains": 1}


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
    new, n = re.subn(r"(?m)^  version: .*$", f"  version: {version}", text, count=1)
    assert n == 1, "SKILL.md has no '  version:' line under metadata:"
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


FIXTURE = lambda n: DATA / f"ledger-v{n}.sql"  # noqa: E731
RELEASED = range(1, SHIPPED)
V16_INIT = DATA / "ledger-v16-init.sql"
V16_COMMIT = "c06a6e6"
MIGRATION_HASHES = DATA / "migrations.sha256"

# One row for each table the demo leaves empty, inserted right after the migration that creates the table (0 for the
# tables of schema 1), in that schema's columns. Each row keeps every later schema's rules, so check passes after
# migrate. review_actors at 4 is the row 0006's rebuild copies.
FILL = {
    1: ["INSERT INTO requirements(code, text, source, first_review_id, created_at) VALUES ('REQ-0001', "
        "'Requests need 14 days of notice.', 'owner, chat 2026-11-01', 1, '2026-11-01T09:06:30.000Z')",
        "INSERT INTO finding_requirements(finding_id, requirement_id, verdict) VALUES (2, 1, 'differs')",
        "INSERT INTO finding_goals(finding_id, goal_id) VALUES (2, 1)",
        "INSERT INTO decision_issues(decision_id, issue_id) VALUES (1, 2)",
        "INSERT INTO artifacts(review_id, kind, url, created_at) VALUES (1, 'summary-doc', "
        "'https://example.com/demo/R-0001', '2026-11-01T09:08:10.000Z')",
        "INSERT INTO test_runs(review_id, repository_id, commit_sha, command, purpose, passed, failed, skipped, "
        "errored, exit_code, duration_ms, started_at) SELECT 1, 1, commit_sha, 'python3 -m unittest', 'baseline', 12, "
        "0, 0, 0, 0, 900, '2026-11-01T09:08:30.000Z' FROM review_repositories WHERE review_id = 1"],
    4: ["INSERT INTO review_actors(review_id, actor_id, role_label) VALUES (1, 3, 'security reviewer')",
        "INSERT INTO review_requirement_checks(review_id, requirement_id, verdict, where_implemented, tested, "
        "finding_id, evidence) VALUES (1, 1, 'differs', 'src/requests.py', 'no', 2, 'can_request hardcodes 14 days')"],
    9: ["INSERT INTO interview_questions(code, review_id, domain_code, question, why_asked, asked_by_actor_id, "
        "asked_at) VALUES ('IQ-0001', 1, NULL, 'Should every program set its own notice period?', "
        "'can_request hardcodes 14 days', 2, '2026-11-01T09:06:10.000Z')",
        "INSERT INTO interview_answers(question_id, answer, answered_via, answered_by_actor_id, answered_at) "
        "VALUES (1, 'Yes, each program sets it.', 'chat', 2, '2026-11-01T09:06:20.000Z')",
        "INSERT INTO goal_sources(goal_id, question_id) VALUES (1, 1)",
        "INSERT INTO step_goals(step_id, goal_id) VALUES (3, 1)"],
    12: ["INSERT INTO review_environment(review_id, capability, available, detail, how_to_enable, position) "
         "VALUES (1, 'git', 1, '2.43', 'install git', 1)"],
    14: ["INSERT INTO checklist_answers(review_id, check_code, report, answer, evidence) SELECT 1, code, 'spec.md', "
         "'yes', 'can_request read in full' FROM domain_checks WHERE domain_code = 'spec' ORDER BY code LIMIT 1"],
    16: ["INSERT INTO step_conflicts(step_a_id, step_b_id, kind, target, reason, resolution, note, "
          "resolved_by_actor_id, resolved_at) VALUES (2, 3, 'file', 'src/requests.py', 'both edit the file', "
          "'no-conflict', 'different functions', 2, '2026-11-01T09:18:30.000Z')"],
    19: ["INSERT INTO feature_proposals(code, review_id, goal_id, title, today, evidence, proposal, value, effort, "
         "done_test, proposed_by_actor_id, created_at) VALUES ('FP-0001', 1, 1, 'Notice period per program', "
         "'14 days for every program', 'src/requests.py:2', 'A notice_days setting per program', 'high', 'small', "
         "'test_notice_per_program', 2, '2026-11-01T09:08:40.000Z')",
         "INSERT INTO feature_status_changes(proposal_id, from_status, to_status, note, actor_id, at) "
         "VALUES (1, NULL, 'proposed', 'proposed by the features reviewer', 2, '2026-11-01T09:08:40.000Z')"],
    20: ["INSERT INTO tool_runs(review_id, repository_id, commit_sha, tool, domain_code, status, version, "
         "output_format, actor_id, created_at) SELECT 1, 1, commit_sha, 'ruff', 'security', 'imported', '0.6.0', "
         "'sarif', 2, '2026-11-01T09:08:50.000Z' FROM review_repositories WHERE review_id = 1",
         "INSERT INTO tool_results(code, tool_run_id, domain_code, rule, level, path, line_start, line_end, message, "
         "fingerprint, dismissed_reason, disposed_by_actor_id, disposed_at, created_at) VALUES ('LD-0001', 1, "
         "'security', 'S311', 'warning', 'src/auth.py', 1, 1, 'random is not for secrets', 'ruff:S311:src/auth.py:1', "
         "'SEC-H1 says the same', 2, '2026-11-01T09:09:00.000Z', '2026-11-01T09:08:50.000Z')"],
}


def build_fixtures(upto=SHIPPED - 1):
    """{n: SQL dump} of the released-schema fixtures 1..upto, built from the v1 demo (see the module docstring)."""
    mod = fixtures.load_ledger_module()
    _, db = restore_v1(fixtures.temp_dir("fixtures-") / "proj")
    con = sqlite3.connect(str(db))
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys = OFF")  # as migrate runs them (the table rebuilds need it); checked after each
    for t, n in con.execute("SELECT type, name FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchall():
        con.execute(f'DROP {t.upper()} "{n}"')
    out = {}
    for ver, slug, desc, sql in [(1, None, None, "")] + [m for m in mod.MIGRATIONS if 1 < m[0] <= upto]:
        con.execute("BEGIN")
        for stmt in mod.split_sql(sql):
            con.execute(stmt)
        if ver > 1:
            con.execute("INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
                        (ver, desc, f"2026-11-02T{ver:02d}:00:00.000Z"))
            con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(ver),))
        for stmt in FILL.get(ver, ()):
            con.execute(stmt)
        con.execute("COMMIT")
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        assert not bad, (ver, bad)
        empty = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%' ORDER BY name")
                 if con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == 0]
        assert not empty, f"schema {ver}: no row in {', '.join(empty)} (add one to FILL)"
        out[ver] = "\n".join(con.iterdump()) + "\n"
    con.close()
    return out


def write_fixture(n):
    """Write FIXTURE(n) (frozen once its schema is released)."""
    text = build_fixtures(n)[n]
    FIXTURE(n).write_text(f"-- A schema-{n} ledger with a row in every table: the v1 demo (ledger-v1-demo.sql), "
                          f"migrations 2..{n} and the rows of\n-- _migrate_helpers.FILL. Frozen: regenerate only with "
                          f"python3 -m dev.tests._migrate_helpers --write-fixture {n}\n" + text, encoding="utf-8")
    print(f"wrote {FIXTURE(n)}")


def restore(n, project=None):
    """A project folder whose ledger is the schema-n fixture; returns (project, db)."""
    project = Path(project) if project else fixtures.temp_dir(f"v{n}-") / "proj"
    (project / ".code-review").mkdir(parents=True, exist_ok=True)
    db = project / ".code-review" / "ledger.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(FIXTURE(n).read_text(encoding="utf-8"))
    con.close()
    return project, db


def write_v16_init():
    """Write V16_INIT: `init` by the ledger.py built from the sources at V16_COMMIT (needs the git history)."""
    root = fixtures.temp_dir("v16-src-")
    paths = ["SKILL.md", "dev/core.py", "dev/build.py", "dev/page.html", "references/schema.sql",
             "references/migrations"]
    tar = subprocess.run(["git", "-C", str(fixtures.REPO_ROOT), "archive", V16_COMMIT, *paths], capture_output=True,
                         check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(root)], input=tar, check=True, capture_output=True)
    import importlib.util
    spec = importlib.util.spec_from_file_location("ledger_build_v16", str(root / "dev" / "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    (root / "scripts").mkdir(exist_ok=True)
    old = root / "scripts" / "ledger.py"
    old.write_bytes(build.build(root).encode("utf-8"))
    project = fixtures.temp_dir("v16-init-") / "proj"
    project.mkdir()
    subprocess.run([sys.executable, str(old), "--root", str(project), "init"], check=True, capture_output=True)
    con = sqlite3.connect(str(project / ".code-review" / "ledger.sqlite"))
    con.execute("UPDATE meta SET value='demo-v16' WHERE key='project_name'")
    con.execute("UPDATE meta SET value='2026-11-11T00:00:00.000Z' WHERE key='created_at'")
    con.execute("DELETE FROM meta WHERE key='ledger_location'")
    con.commit()
    text = "\n".join(con.iterdump()) + "\n"
    con.close()
    V16_INIT.write_text(f"-- `ledger.py init` by the ledger.py of commit {V16_COMMIT} (schema 16), with its views and "
                        f"triggers.\n-- Frozen: regenerate only with python3 -m dev.tests._migrate_helpers "
                        f"--write-v16-init\n" + text, encoding="utf-8")
    print(f"wrote {V16_INIT}")


def restore_v16(project=None):
    """A project folder whose ledger is V16_INIT (no git needed); returns (project, db)."""
    project = Path(project) if project else fixtures.temp_dir("v16-") / "proj"
    (project / ".code-review").mkdir(parents=True, exist_ok=True)
    db = project / ".code-review" / "ledger.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(V16_INIT.read_text(encoding="utf-8"))
    con.close()
    return project, db


def migration_hashes():
    """[(file name, sha256)] of MIGRATION_HASHES, in order."""
    out = []
    for line in MIGRATION_HASHES.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            digest, name = line.split()
            out.append((name, digest))
    return out


if __name__ == "__main__" and "--write-v1-demo" in sys.argv:
    write_v1_demo()
if __name__ == "__main__" and "--write-fixture" in sys.argv:
    write_fixture(int(sys.argv[sys.argv.index("--write-fixture") + 1]))
if __name__ == "__main__" and "--write-v16-init" in sys.argv:
    write_v16_init()
