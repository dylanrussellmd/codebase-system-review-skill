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
