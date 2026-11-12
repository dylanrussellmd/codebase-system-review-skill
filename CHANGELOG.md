# Changelog

The tool version is `version:` in SKILL.md's frontmatter; `ledger.py --version` prints it with the schema version.

## 1.9.1 (schema 16)

- The hub works with no answer store: answers download as `answers.json`.
- `references/tools.md` and `references/tools.jsonl`: the tool catalog. `dev/release.py` builds the bundle. Review
  pages page their issue links. Model-agnostic lint.

## 1.9.0 (schema 16)

- Deterministic review commands: `env` (what the machine and harness offer, with fallbacks), `scan` (a file-based
  inventory that proposes domains by rules), `brief`, `prompt` (generated from templates, approved goals, known issues
  and 115 checklist questions), `lint-report`, `match --auto`, `compare-runs`, `conflicts` and `resolve-conflict`
  (plan approval refused while recorded conflicts are unjudged). Optional `ui-capture` and `ui-audit` (Playwright).
  Same inputs, same output.
- `new-review` no longer takes domain flags: `scan` proposes them and `review-domain` changes one.
- Migrations 0012-0016. `references/environment.md`, `references/determinism.md`, `references/writing.md`.
- Viewer: shared helpers, only the fields each page reads, text to the writing standard, docstrings throughout.

## 1.8.0 (schema 11)

- Viewer: input checked with clear 400s; answers keep drafts and show 409 and 503 in place; paged issue and gate
  lists; slimmer review and plan payloads with server search; gzip; a request log; `/api/home`, `/api/checks`,
  `/api/decisions`, `/api/guide` and per-item routes; decisions, recorded data and goal lifecycle pages.
- Commands that fail print one line and exit 2. Schema 11 (migration 0011): answer rules, queue context, a check for
  artifacts outside the ledger folder, event labels.

## 1.7.0 (schema 10)

- Viewer: one JSON error shape with accurate statuses; a route table with method routing; totals views (migration
  0010); the page carries its own static-snapshot branch, so `progress-page` only embeds data; focus, headings,
  contrast, phone width and row links.
- `references/writing.md` (the writing standard) with a lint test and an optional Vale config.
- `set-doc-url`, `plan-add-review`; a note on SQLite older than 3.34; one plain sentence above every table and view.

## 1.6.0 (schema 9)

- Owner interview and design goals by domain: `interview-ask`, `interview-answer`, `propose-goal`, `approve-goals`,
  `reject-goal`, `link-step-goals`, and read-only `goals` and `interview`. Schema 9 (migration 0009).
- Every write has a command: `new-plan`, `plan-status`, `add-requirement`, `add-domain`, `review-domain`,
  `finish-run`, `verify`, `record-test-run`, `add-commit`, `add-guard`, `confirm-guard`, `set-verdict`, `add-metric`,
  `add-artifact`, `tag-review --pushed`, `redact`.
- SKILL.md runs on commands; detail lives in `references/`. Printed ledger text is stripped of terminal controls.

## 1.5.0 (schema 8)

- Commands that write the ledger, each in one transaction with `--actor`, refusing with one line and exit 1:
  `next-code`, `add-repo`, `add-actor`, `new-review`, `review-status`, `snapshot`, `add-run`, `issue-status`,
  `step-status`, `import-steps`, `open-gate`, `answer-gate`, `apply-gate`, `import-findings`, `import-checks`,
  `load-index`, `match` and `report-finding` (records a discovery at an explicit commit, refuses lines or symbols not
  at that commit, fills the excerpt and amends the plan). The viewer's answers go through the same `answer_gate()`.
  Formats in `references/formats/`; implementer brief template in `references/implementer-brief.md`.
- Other commands: `query [--readonly] [--json]`, `progress-md`, `progress-page` (one self-contained owner page),
  `import-answers`, `restore`, `merge`; `check --quiet`; `gates` lists waiting steps, open decisions and stale steps.
- fill-refs reads through one `git cat-file --batch` per repository, is idempotent and refuses files over 20 MB.
- Schema 8 (migration 0008): `finding_locations.source` and `v_next_codes`. init writes `.code-review/.gitattributes`.

## 1.4.0 (schema 7)

- Schema 6 (migration 0006, lookups): issue statuses, severities, match types, relation types and gate kinds are
  lookup tables with flags (active, resolved, reopens, came back...) that views and triggers use instead of literal
  lists; every other word list has a label in `enum_labels`, and the viewer takes its labels from the API. Review and
  finding foreign keys are RESTRICT; `metrics.name` references `metric_defs`. `migrate` lists every row a new rule
  refuses.
- Schema 7 (migration 0007, views): seven new must-be-empty checks (`v_history_gaps`, `v_match_problems`,
  `v_off_plan_coverage`, `v_done_steps_open_issues`, `v_closed_without_reason`, `v_relation_problems`,
  `v_check_finding_owner`) and `v_real_findings`; `verified` only through a later review's `fixed` check; evidence on
  fixed, regressed and changed checks; a resolution on accepted-risk and wont-fix; `steps.no_findings_reason`.
- `check` runs quick_check; `check --full` runs integrity_check. Review points are listed in natural order.

## 1.3.0 (schema 5)

- Schema 3 (migration 0003, integrity): history is append-only; findings are inserted unmatched and matched by
  UPDATE; status and severity changes need a `change_note`; a fixed issue needs `after_state`; only a `regression`
  match regresses an issue and false findings count for nothing; the review-check outcome matrix is enforced and checks
  are immutable; codes are sequential and canonical and commit columns hold full hashes; review, plan, gate and step
  transitions, approvals and blocking gates are enforced; gate answers must be one of the gate's options; every tracked
  change writes an event; gate kind `confirm`.
- Schema 4 (migration 0004, views): `ledger_checks` drives `check` (error and warning tiers) and the overview; an index
  on every foreign-key column; requirement verdicts per review (`v_requirement_trace`); agents per review
  (`review_actors`); plan helper views; a metric catalog; faster coverage and summary views; `PRAGMA optimize`.
- Schema 5 (migration 0005, records): every stored time is UTC with milliseconds; fill-refs resolves symbols by
  definition, refuses out-of-range lines and supports whole-file references; continuations count as new issues;
  `reviews.report_md` and `plans.narrative_md` with `set-report` and `set-narrative`; `findings.discovered_in_step_id`
  and the `v_unplanned_discoveries` check.

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
