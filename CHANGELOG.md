# Changelog

The tool version is `version:` in SKILL.md's frontmatter; `ledger.py --version` prints it with the schema version.

## 1.1.0

- `ledger.py --version`; ledger.py carries TOOL_VERSION and TOOL_HASH, stamped by `dev/build.py`.
- `init` never downgrades the project's `.code-review/ledger.py`: it replaces it only with a newer copy (keeping
  `ledger.py.bak`) and refuses an older one unless `--force`. It records `created_by_skill`, `written_by_tool` and
  `schema_hash` in meta and sets `PRAGMA user_version`, and restores the shipped views and triggers.
- New `migrate` command and numbered migrations in `references/migrations/` (0001 is the schema-1 baseline).
- Every command checks the ledger first: no traceback for a file that is not a ledger or has no schema version; a
  newer schema is refused; an older one is refused for writes and read with a warning.
- `check` reports schema drift (missing, extra or changed tables, indexes, views, triggers) and a schema_version other
  than the tool's; `serve`, `gates`, `fill-refs` and `tag-review` refuse a drifted ledger.

## 1.0.0 (commit ea00196)

- First version (unversioned at the time): the skill, the ledger schema 1 and the viewer.
