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

## Use the viewer

From the project root, after a review has started:

    python3 .code-review/ledger.py serve

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
