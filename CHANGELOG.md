# Changelog

The tool version is `version:` in SKILL.md's frontmatter; `ledger.py --version` prints it with the schema version.

## 1.2.1 (schema 2)

- `migrate` writes its `ledger.sqlite.bak-v<N>` backup owner-only (0600), and `init` makes existing backups private.

## 1.2.0 (schema 2)

- Schema 2 (migration 0002): ledger links are http(s) only (`permalink`, `summary_doc_url`, `pr_url`,
  `permalink_template`; the migration sets other values to NULL); location and repository paths are relative with no
  `..` (a repository outside the project root needs `repositories.allow_outside_root = 1`); commit hashes are full
  lowercase hex; review codes are `R-NNNN`; `finding_locations.excerpt_redacted`; trigger `tr_gate_answer_final` keeps
  a gate's answer once the gate is no longer open.
- Root detection stops at the git top (a parent ledger is used only if it lists the repository), uses the main
  checkout for worktrees and the ledger beside the project's own ledger.py, refuses ledgers of other users, and treats
  any `--root` as explicit; `init` outside git with no ledger above needs `--root`.
- `CODE_REVIEW_DIR` keeps the ledger outside the repository; `meta.ledger_location`; `check` warns after a move.
- init is atomic (copy first, schema built in `ledger.sqlite.tmp`), repairs `.gitignore`, makes files owner-only and
  switches the ledger to WAL (`migrate` too); a `--db` outside `.code-review/` gets no ledger.py or `.gitignore`.
- dump checkpoints, reads one snapshot, writes through a temp file and refuses to overwrite a foreign `.sql` file.
- Generated git commands are shell-quoted and built in one place (`v_code_refs.git_command`); `file_at` refuses bad
  paths with one fixed message; repositories outside the root are refused.
- tag-review validates the code and hashes, records each tag at once and exits 1 when a repository is left untagged
  or none is listed; fill-refs exits 1 for lines past the end or a symbol not found and masks secret-looking values
  in excerpts; show exits 1 when there is nothing to show.

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
