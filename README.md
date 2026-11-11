# codebase-system-review

A skill for a managed, multi-reviewer code review, with a per-project SQLite ledger and a local viewer.

    codebase-system-review/
    ├── SKILL.md               the instructions the agent follows
    ├── scripts/ledger.py      ledger tool + viewer (Python 3.9+, no dependencies)
    └── references/schema.sql  the ledger schema, for reading

## Install

OpenCode, for every project:   unzip into ~/.config/opencode/skills/
OpenCode, one project only:    unzip into <project>/.opencode/skills/
Claude Code and OpenCode both: unzip into ~/.claude/skills/   (OpenCode reads this too)

The folder name must stay `codebase-system-review` (it must match `name:` in SKILL.md).
Restart OpenCode, then ask it to "run the comprehensive system review".

Each project keeps a copy of the tool at `.code-review/ledger.py`. Treat a change to that copy, or to the ledger's
schema, in someone else's pull request like any other code change you must review: `ledger.py check` fails when the
ledger's tables, views or triggers differ from the shipped schema, and the skill tells agents to prefer the skill's own
`scripts/ledger.py`.

## Use the viewer

From the project root, after a review has started:

    python3 .code-review/ledger.py serve

## Where the ledger lives

By default `<project root>/.code-review/` (owner-only: the folder 0700, the files 0600). To keep review data out of
the repository, set `CODE_REVIEW_DIR=/some/folder` for every command (or pass `--db FILE`): nothing is then written
inside the repository.

The ledger runs in SQLite's WAL mode (`init` sets it; `migrate` switches an existing ledger once), so the viewer's
reads never block the agent's writes. While it is open, `ledger.sqlite-wal` and `ledger.sqlite-shm` sit beside it
(`.code-review/.gitignore` lists them). That needs:

- a folder where SQLite can create the `-shm` file beside the database (also for read-only use);
- a local disk: WAL does not work on network file systems (NFS, SMB);
- a checkpoint before the `.sqlite` file is copied or committed: `ledger.py dump` runs
  `PRAGMA wal_checkpoint(TRUNCATE)` first, so after `dump` the file holds every commit.

## Developing

`scripts/ledger.py` is generated; edit the sources, never the generated file:

    dev/core.py              CLI, checks, git helpers, viewer API and server
    dev/page.html            the viewer page
    references/schema.sql    the schema (also embedded in ledger.py)

From the repository root, rebuild, confirm the build is current, and try it on a demo project (`DEMO` is a new or
empty folder of your choice; leave it out to get a temporary one):

    python3 dev/build.py                          regenerate scripts/ledger.py
    python3 dev/build.py --check                  exit 1 if scripts/ledger.py is stale
    python3 dev/tests/seed_demo.py DEMO           a small git project with a filled ledger (reviews, a plan, gates)
    python3 scripts/ledger.py --version           print the tool version (from SKILL.md) and the schema version
    python3 scripts/ledger.py --root DEMO check   the demo ledger passes every check
    python3 scripts/ledger.py --root DEMO serve   open the viewer on it (Ctrl+C to stop)

Scale tests build large generated ledgers (`dev/tests/scale.py`; run it with `--help` for a standalone build) and are
skipped unless `LEDGER_SLOW_TESTS=1`. The README's Developing commands and every `ledger.py` command SKILL.md names
are checked by `dev/tests/test_docs.py`.

## Testing

From the repository root (Python 3.9+, standard library only; tested on Python 3.9.0, 3.9.25 and 3.13, and on
SQLite 3.31 to 3.53):

    python3 -m unittest discover -s dev/tests -t . -v

The tests build `scripts/ledger.py` from the sources into a temporary folder and test that build, so they always run
the code that ships. Everything they create (scratch git repositories, ledgers, viewer servers on free ports) lives in
a temporary folder that is removed when the run ends. Slow scale tests are skipped unless `LEDGER_SLOW_TESTS=1`,
and browser tests unless `LEDGER_BROWSER_TESTS=1` with Playwright and Chrome or Chromium installed
(`pip install playwright && python3 -m playwright install chromium`). CI runs both in their own jobs. Measuring helpers
for performance work are in `dev/tests/helpers.py`; baseline numbers are in `dev/tests/perf_baseline.json`.

Before committing a change to `dev/core.py`, `dev/page.html` or `references/schema.sql`, rebuild and commit
`scripts/ledger.py` with it:

    python3 dev/build.py           regenerate scripts/ledger.py
    python3 dev/build.py --check   exit 1, with a short diff, if scripts/ledger.py is stale (CI runs this)

The build pastes the schema and the page into raw triple-quoted strings in core.py, so neither may contain three
double quotes in a row or end with a backslash or a double quote, and each placeholder (`@@SCHEMA@@`, `@@PAGE@@`)
must appear exactly once in core.py and nowhere else; the build refuses otherwise and writes nothing.

`python3 dev/tests/seed_demo.py [DIR]` creates a demo project (a small git repository with a filled ledger, refs
filled and reviews tagged) to try the viewer; it exits 1 if any of the ledger behaviour it relies on has changed.
Shared test builders (scratch repositories, ledgers, the demo data, a viewer server) are in `dev/tests/fixtures.py`.

CI (`.github/workflows/ci.yml`) runs the build check and the suite on Python 3.9 and 3.12 for every push and pull
request.

## Changing the schema

Every ledger already in use is schema 1 (`references/schema.sql` as it was at commit ea00196, frozen in
`dev/tests/data/schema-v1.sql`). A schema change ships as a numbered migration, never as an edit alone:

1. Add `references/migrations/NNNN-<slug>.sql` (the next number; lowercase slug). Its first line is
   `-- <description>`. It holds only what changes tables, indexes and data, from schema NNNN-1 to NNNN. Views and
   triggers are not migrated by hand: `migrate` drops every one before the steps run and re-creates them from
   `references/schema.sql` afterwards. To change a table's columns or constraints use SQLite's table-rebuild recipe
   (create `<table>_new`, copy the rows, drop the old table, rename the new one, re-create its indexes); foreign keys
   are off while the steps run and `PRAGMA foreign_key_check` must be clean at the end.
2. Edit `references/schema.sql` to the new end state, and in the same change set its header line to `schema version
   NNNN`, the meta seed to `'NNNN'`, and add `(NNNN, '<description>')` to the `schema_migrations` seed. The build
   refuses if any of these disagrees with the highest migration (`SCHEMA_VERSION` in ledger.py is that number).
3. Add a test that restores the frozen v1 ledger (`dev/tests/_migrate_helpers.py`), runs `migrate`, and checks the
   result against a fresh `init` (`dev/tests/test_migrate.py` shows how); then rebuild with `python3 dev/build.py`.
4. Bump `version:` in SKILL.md's frontmatter and add a line to CHANGELOG.md.

`migrate` refuses a ledger newer than the tool, backs the ledger up with SQLite's backup API to
`ledger.sqlite.bak-v<old version>` (never overwriting an earlier backup), applies every pending step in one
`BEGIN IMMEDIATE` transaction, records each in `schema_migrations`, sets `meta.schema_version` and
`PRAGMA user_version`, and rolls everything back (removing the backup, the ledger byte-identical) if a step fails, a
foreign key breaks, or the result differs from a fresh build of `references/schema.sql`. `check` reports any table,
index, view or trigger that differs from the shipped schema; `serve`, `gates`, `fill-refs` and `tag-review` refuse to
run on such a ledger, and `init` restores the shipped views and triggers. The version, the build and the ledger.py
copy that `init` makes:

    ledger.py --version     the tool version (`version:` in SKILL.md, stamped in by the build with a content hash)
    ledger.py init          replaces .code-review/ledger.py only with a newer copy, keeping ledger.py.bak
    ledger.py init --force  also replaces a newer or same-version copy
    ledger.py migrate       upgrades an older ledger
