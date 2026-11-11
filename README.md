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

`scripts/ledger.py` is generated; edit the sources and rebuild:

    dev/core.py              CLI, checks, git helpers, viewer API and server
    dev/page.html            the viewer page
    references/schema.sql    the schema (also embedded in ledger.py)
    python3 dev/build.py     regenerate scripts/ledger.py

`dev/tests/seed_demo.py <project>` fills a ledger in a git project with sample reviews, a plan and gates.

## Testing

From the repository root (Python 3.9+, standard library only):

    python3 -m unittest discover -s dev/tests -t . -v

The tests build `scripts/ledger.py` from the sources into a temporary folder and test that build, so they always run
the code that ships. Everything they create (scratch git repositories, ledgers, viewer servers on free ports) lives in
a temporary folder that is removed when the run ends. Slow scale tests are skipped unless `LEDGER_SLOW_TESTS=1`;
browser tests are skipped unless Playwright and Chrome are installed.

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
