# Tools a review can use

Read this when choosing what to run for a domain, or when a project already has one of these tools installed.
`ledger.py` calls only the tools in the first table; every other row is a candidate, run by a reviewer through the
shell when it is installed, or wired into `ledger.py` by its own change to the skill. Each domain's table lists the
rows rated must-have or strong; `tools.jsonl` beside this file holds all 626 rows (one object per tool and
domain, with `domain`, `group`, `rating`, `link`, `adds`, `call`, `deterministic`, `install_licence`, `offline`,
`harness`, `fallback`, `maturity`, `verified`, `reason`, `sends_data`, `runs_project_code`).

Survey of 2026-11-11 by three researchers (correctness and architecture; user-facing and documentation; security,
operations and the review process). They read each tool's repository or registry entry that day and ran no tool.
Command lines are starting points: confirm the flags with `--help` at the pinned version before relying on one. A
cell that says "not verified" is unconfirmed.

- **Rating.** must-have: run it whenever the project is in that ecosystem. strong: run it when installed.
- **Deterministic.** yes: same commit, version and rules give the same output. yes-db: and the same advisory
  database, so record its date. noisy: a measurement; store the spread. no: a service, a search or a model.
- **Marks.** online: the tool sends something (package names, a domain, code, metrics) to an outside service.
  online in some modes: it has an offline mode, or needs a database, rules or images fetched first. runs code: it
  builds, tests or runs the project. Marks are read from the row's offline cell and command; check a tool before
  running it on code the owner does not fully trust.

## Tools the skill calls

| Tool | Called by | Fallback |
|---|---|---|
| git | every reference, `fill-refs`, `tag-review`, `scan`, `show` | none: stop and ask the owner to install git; the ledger cannot pin lines without it |
| Python 3.9+ with SQLite | `ledger.py` itself | none: nothing runs without it |
| Playwright (Python package) | `ui-capture`, `ui-audit` | the owner saves screenshots at phone and desktop width; review from the source and write "rendered result not seen" (environment.md, Rendered pages) |
| Chrome, Chromium or Edge | the browser Playwright drives (`LEDGER_CHROME` names one) | `python3 -m playwright install chromium`, or the Playwright fallback above |
| Lighthouse (npx --no-install lighthouse) | category scores in `ui-audit`, only when installed; never downloaded | `ui-audit`'s own contrast, names, weight and layout-shift numbers |
| Vale (optional, `tool-run vale`) | the writing standard on a project's docs and UI text (writing.md) | the reviewer reads for the phrases in `vale/styles/Plain/*.yml` |

`ledger.py env` reports which of these this machine has.

## Cross-domain shortlist

The tools that several researchers put in their top 10, then the rest of each top 10. C, U and S are the
correctness, user-facing and security researchers, with each one's rank.

| Tool | Ranked | Domains | What it buys |
|---|---|---|---|
| ast-grep or Semgrep CE / Opengrep, with rule packs shipped in the skill and pinned rules | C1, S1 | time-concurrency, resilience, decision-placement, server, client-api, abstraction, security, privacy, logging | each "inventory every route, sink, log call" checklist item becomes a query with a countable result; SARIF or JSON lines out |
| Importers: SARIF 2.1.0, JUnit XML, coverage (lcov, Cobertura, coverage.json), mutation-testing-report-schema | C2, C3, S2 | every static analyzer; tests, test-quality, server, hotspots | four formats turn about half the rows into ledger data; no machine result is dropped |
| Coverage tool per ecosystem | C2 | tests, server, test-quality, hotspots | "no test reaches this" becomes a join |
| Mutation testing per ecosystem | C3 | test-quality | the only measure of whether tests check anything |
| Pinned tool versions (mise or a version table) and scanning an export of the recorded commit | S3 | all | without these no tool is repeatable between machines |
| Playwright (probe script and walkthrough specs) | U1 | ui, walkthroughs, notifications, localization, docs-user, client-api, compatibility | the running UI as evidence: screenshots, status per role and route, traces of each task |
| Vale with the Plain styles | U2 | docs-developer, docs-user, ui | the writing standard, enforced the same way every run |
| axe-core | U3 | ui, walkthroughs, notifications | accessibility findings from the real DOM, with selectors |
| scc | C4 | abstraction, hotspots | size, complexity, hotspots, coupling and bus factor for 200+ languages in one binary |
| gitleaks | S4 | security | full-history secret scan, redacted |
| OSV-Scanner with offline databases, and Syft for an SBOM per review | S5 | dependencies | every lockfile in every ecosystem, and a dependency diff between reviews |
| oasdiff (Buf and GraphQL Inspector beside it) | C6 | compatibility, contracts, client-api | breaking changes and drift between two descriptions of an API, classified |
| lychee | U7 | docs-developer, docs-user | dead links and stale paths in every doc |
| jscpd | C5 | decision-placement, abstraction, contracts | copies of rules and types across languages, as pairs with locations |
| Schemathesis | C7 | tests, contracts, server | per-route cases and responses the contract does not mention; seeded |
| Hurl | C8 | contracts, server, resilience | stored, rerunnable HTTP probes and the route by role access matrix |
| Database planner capture (SQLite through the standard library, PostgreSQL auto_explain), Squawk, tbls | C9 | database, compatibility, decision-placement | every query with its plan, migration safety, the schema inventory |
| libfaketime, a TZ matrix and unshare -rn | C10 | test-quality, time-concurrency, resilience | three zero-cost run variants that measure clock, zone and network dependence |
| Project Wallace, with Stylelint for locations | U4 | ui | design consistency as counts that trend between reviews |
| Doc Detective | U5 | docs-user, walkthroughs | help pages tested against the product |
| Mailpit | U6 | notifications, walkthroughs, resilience | every message sent, with duplicates and missing text parts |
| Lighthouse CI | U8 | ui, performance | performance and layout numbers stable enough to compare |
| Impeccable (a design tool's file, when one exists, takes its place) | U9 | ui | a rule-based test for the generic generated look |
| eslint-plugin-i18next with pseudo-localization | U10 | localization | hardcoded strings as an exact list; text that will not fit, on screen |
| Trivy | S6 | security, dependencies | IaC, Dockerfile, container, licence and secret checks in one binary |
| zizmor and actionlint | S7 | operations | CI workflow security and correctness, exact and offline |
| Prompts rendered from the ledger, reviewer output validated against a schema | S8 | process | same ledger state, same prompt bytes; output shape checked |
| Agent, command and hook files shipped for each harness, and a read-only ledger connector | S9 | process | reviewer roles that cannot edit, pinned models, a guard on writes |
| A repeatability yardstick: two runs compared at one commit, and an eval suite on planted-defect repositories | S10 | process | how far two runs differ, measured |

Built into `ledger.py` at no install cost, by all three researchers' account: git-history hotspots and knowledge
concentration, the requirement-code scan, DST and boundary dates, the SQLite planner, the four importers, the
doc-reference check, the internal-name scan, the environment-variable inventory, lockfile facts and a log-sample
check. Each is regex, set arithmetic or standard-library parsing on files the review already has.

## One way to run every tool

The three researchers agree on one design:

- **One generic command, with presets per domain.** It looks the tool up in a table shipped with the skill (command
  line, version command, output format, parser, domain, fallback, whether it goes online, whether it runs project
  code), runs it on an export of the recorded commit (`git worktree add --detach` or `git archive` into a temporary
  folder, so results match the ledger's commit and no tool writes into the repository), and keeps the raw output in
  the review's folder.
- **Every run recorded.** Tool, version, command, exit code, duration, rules hash, database date and the output's
  sha256, as an artifact of the review. A missing tool is recorded as absent with its fallback, the brief tells the
  reviewer to do that part by reading, and the report says "not machine-checked" for it.
- **Results are leads, not findings.** Each imported result goes into a tool-results table, and a must-be-empty
  check fails until every result is cited by a finding or dismissed with a reason: a reviewer may judge a lead but
  cannot miss one. Across reviews the same tool, rule and fingerprint carries its earlier disposition forward. A
  result that is gone is evidence for a `fixed` check, never a status change on its own.
- **Counts as metrics.** `ledger.py add-metric R-0001 NAME VALUE --method counted` for counts, `--method measured`
  for timings and sizes, so the trends between reviews hold them.
- **Pinned and offline by default.** Versions pinned in a file shipped with the skill (with download hashes). A tool
  marked online runs in its offline mode where it has one; the rest wait for an owner decision recorded once. A tool
  marked runs code runs in a container on code the owner does not fully trust, or is skipped with a note.

`ledger.py tool-run` is that command, `tool_runs` and `tool_results` hold the runs and their leads, and
`v_unaddressed_tool_results` is the check; the next section is how a tool is added to it.

## How a tool is wired in

The tools the owner picked (decision D-0015) run through one command:

```
ledger.py tool-run <tool> --review R-0001 [--domain D] [--repo NAME] [--rules FILE] [--online] [--unpinned]
ledger.py tool-run <tool or format> --review R-0001 --from <file the tool wrote> [--tool-version V]
ledger.py dismiss-lead LD-0001 [LD-0002 ...] --reason '<why it is not a finding>'
ledger.py cite-lead LD-0001 R-0001:SEC-H3
```

- **Where it runs.** On an export of the review's commit (`git archive` into a temporary folder, regular files
  only), with that folder as the working directory and `.` as the target, so reported paths are already relative.
  History tools (gitleaks, git-history) read the repository itself, up to the reviewed commit. Nothing is written
  into the repository.
- **What is recorded.** One `tool_runs` row per run: tool, version printed, version pinned, the command with
  `{checkout}`, `{out}` and `{rules}` in place of temporary paths, exit code, duration, output format, the output
  kept as `.code-review/reviews/R-0001/tools/NNNN-<tool>.<ext>` and its sha256, the rule file's sha256, and whether
  it could go online. Status `ran`, `imported` (`--from`), `absent` (not on PATH: the fallback is recorded and the
  command exits 0), `skipped` (nothing to run on, or it needs the network: the reason and fallback are recorded) or
  `failed` (an exit code the adapter does not accept, or no output).
- **Pins.** `references/tools.lock` (mise's `[tools]` table) holds one version per tool. `env` lists each tool as
  `tool:<name>` with the version found and the pinned one; `tool-run` refuses another version unless `--unpinned`,
  and records both. The lock is read beside `scripts/ledger.py`; the project's copy in `.code-review/` has none, so
  run the skill's copy.
- **Offline.** No adapter goes online unless `--online` is given: Semgrep's registry rules and OSV-Scanner's advisory
  databases are fetched only then, into `.code-review/tools/`, and later runs read them from there. Tools run (and
  print their versions) with their update checks and telemetry off; zizmor runs with `--offline`. gitleaks redacts
  values itself and the kept report has every value field replaced by `REDACTED`. actionlint and zizmor run only when
  the commit has `.github/workflows/`, OSV-Scanner only when it has a lockfile or requirements file.
- **Leads.** Each result is a `tool_results` row with a code `LD-0001` (one sequence per ledger), the rule as the tool
  names it (the shipped ast-grep rules are `<pack>.<rule>`, built-in checks `<tool>.<check>`), a level (`error`,
  `warning`, `note`, `none`), path and lines at the reviewed commit, and a message of at most 400 characters. Leads
  are inserted sorted by path, line, rule and message, so the same output gives the same rows. The fingerprint hashes
  tool, rule, path, message and occurrence (no line): a lead dismissed in an earlier review is dismissed again in the
  next, its reason starting "carried from LD-0007".
- **Addressing a lead.** A finding of the same review with a location on the lead's lines covers it; otherwise
  `cite-lead` links it to a finding, or `dismiss-lead` records why it is not one. A reviewer names each lead of its
  domain in the report: its code in a finding, or a row of a `## Leads` table (`| LD-0001 | dismissed | why |`);
  `lint-report --review` refuses a report that leaves one out, and `--record` stores the dismissals. The reviewer's
  prompt lists the domain's open leads. `check` fails while `v_unaddressed_tool_results` has rows.
- **Rule packs.** `references/rules/ast-grep/` holds one rule per file under `rules/<domain>/` (decision
  placement, time, resilience, server entry points, client calls, logging), each with a message and a test under
  `tests/` that `ast-grep test -c references/rules/ast-grep/sgconfig.yml` runs: the rule matches its `invalid`
  samples and none of its `valid` ones.
- **Built in.** `git-history` needs no install: from `git log` up to the reviewed commit (at most 5,000 commits) it
  lists the most changed files (hotspots), pairs of files in different folders that change together in at least
  half their commits, and files one author changed five times or more (handover); per-file commits and authors,
  the history's commits, authors and bus factor are metrics.
- **Coverage.** `tool-run coverage --from <file>` reads the project's own LCOV or Cobertura file (Cobertura when it
  starts with `<`): line and branch coverage per file and overall, per function its calls (LCOV) or line coverage
  (Cobertura), and a lead for each file no test reaches.
- **Test runs.** `ledger.py test-runs --review R-0001 --command '<command writing JUnit XML to {junit}>'` runs the
  suite on a fresh export for each run: the baseline, `--repeat` more times (default 3), in random order (seed
  `--seed`, default from the commit, given as `{seed}` in `--random-command` and as `LEDGER_TEST_SEED` and
  `PYTHONHASHSEED`), with the network off (`unshare -rn` on Linux; elsewhere every proxy variable points at a closed
  port, recorded as the fallback) and at four clocks: the end of a leap day in Pacific/Kiritimati, the end of a year
  in America/St_Johns, and the minute before the 2027 daylight-saving changes in America/New_York and Europe/Berlin
  (from zoneinfo), each with `TZ` set and the time set by `faketime` when it is installed (not verified: faketime could not be
  installed where this was built). Each run is a `test_runs`
  row and a `tool_runs` row; a test that fails in a variant and passed in the baseline (or that flips between
  repeats) is a lead `test-runs.flaky`, `.order-dependent`, `.network` or `.clock`, once per test per invocation.
- **Documentation.** `vale` runs a copy of `references/vale/` without its `Packages` line (so nothing is fetched);
  a `PlainUI` alert goes to `ui`, one in a user guide to `docs-user`. `lychee` checks local links and anchors with
  `--offline --include-fragments` (web links only with `--online`); a broken link or anchor is a lead at its line.
  Two scans are built in. `doc-references` reads every Markdown, reStructuredText and AsciiDoc file (change logs and
  test fixtures left out): each path in a code span, link or shell block must be a file or folder at the commit,
  each `npm run` script, `make` target and justfile recipe must exist, and each environment variable in a code span (upper case with
  an underscore) must appear in a file other than the docs. `internal-names` lists the tables, columns and
  enumeration values the SQL files create and the routes the code defines (names with an underscore or in
  camelCase, and route paths: a single plain word could be ordinary text), then finds them in what users see: page
  and template text and labelling attributes, client-script strings that read as words, locale files.
- **Pages and styles.** `ui-audit` runs axe-core on every page when it finds `axe.min.js` (`LEDGER_AXE`, the
  project's `node_modules/axe-core/`, or npm's global folder): its `--json` holds each page's violations, and
  `tool-run axe --from` that file makes them leads (critical and serious are errors). `wallace` reads every
  stylesheet of the commit as one: the distinct colours, font sizes, families, line heights, z-indexes, shadows and
  radii are metrics, and a category over its limit is a lead. `impeccable detect --json` flags the generic
  generated look (its `slop` category is counted) and low contrast in the files; it honours the project's own
  config and `DESIGN.md`, and `DO_NOT_TRACK` keeps its usage data off. `odiff` compares each PNG screenshot recorded
  on the review with the one of the same name on the review it follows up (`new-review --previous`), with
  antialiasing ignored and a size change reported as such; the changed share per screenshot is a metric and the
  diff images go to `reviews/R-0002/ui/odiff/`. `stylelint` runs one rule, `declaration-property-value-allowed-list`,
  built from the project's design tokens: Style Dictionary JSON (`tokens/**/*.json`, `*.tokens.json`; a token's CSS
  name is its path in kebab case, as Style Dictionary's `css/variables` writes it), else the custom properties its
  stylesheets declare. A colour, spacing, font size, family, radius, shadow or z-index property may hold only
  `var()` of its category's tokens, `calc()` over them, or a keyword; a raw value is a lead naming the token that
  holds the same value. Style Dictionary itself is not run: its JSON is read directly. Plain `.css` only; Sass and
  Less are counted by Wallace.
- **The running system.** `--target` names what a tool talks to; anything not on this machine needs `--online`
  too. `schemathesis` runs the OpenAPI description against the API at `--target` (seeded from the commit, or
  `--seed`; deterministic generation), a lead per operation and failed check (ST101 a server error, ST108 a
  response outside the schema), at the operation's line in the description. `hurl` runs a route-by-role access
  matrix: one request file per operation of the description and role, from `--rules matrix.json`
  (`{"roles": {"admin": {"headers": {"Authorization": "Bearer {{admin_token}}"}}, "anonymous": {}},
  "expect": {"GET /api/users": {"anonymous": "denied", "admin": 200}, "*": {...}}, "params": {"id": "7"}}`;
  `allowed` is below 400, `denied` 401 to 403, no expectation means no 5xx), plus the project's own `.hurl` files.
  A `{{name}}` in a header is passed as `--secret` from the environment variable of that name and never recorded.
  `mailpit` reads every message Mailpit caught (`--target`, default `http://127.0.0.1:8025`) after the walkthroughs:
  a lead for each one sent twice, without a plain-text part, a subject or a recipient; only sizes and hashes of the
  bodies are kept. It needs no binary on PATH: Mailpit may run in a container.
- **Between two reviews.** `oasdiff changelog`, `buf breaking` and `graphql-inspector diff` compare the API
  description at the commit with the same file at the commit of the review this one follows up; a breaking change
  is an error lead. GraphQL Inspector prints text only, which `ledger.py` turns into JSON.
- **Databases.** `squawk` checks the migrations when the code or dependencies show PostgreSQL. `query-plan` is
  built in: the commit's `.sql` files (schema files first, then by path) applied to an empty in-memory SQLite
  database, then `EXPLAIN QUERY PLAN` of every query the code holds as a string literal (or of a `--rules` file of
  statements), placeholders bound to NULL; reading a whole table (`SCAN t` without an index) or sorting in a
  temporary B-tree is a lead at the query's line. `pg-explain` does the same on PostgreSQL 16 or later: it creates
  a throwaway database on the server at `--target`, applies the files, explains each query's generic plan with
  sequential scans off (a `Seq Scan` left means no index fits), and drops the database.
- **Budgets and translations.** `hyperfine` times the commands of the project's `budgets.json`
  (`{"budgets": [{"name": "build", "command": "...", "max_ms": 500, "runs": 10}]}`); the mean and spread of each are
  metrics measured, not counted, and a mean over its budget is a lead. `i18n-lint` runs ESLint with one rule,
  eslint-plugin-i18next's `no-literal-string` (JSX text and attributes), from the plugin installed beside `eslint`.
  `pseudo-locale` is built in: each locale file's keys and placeholders against the source locale (English, else
  the first), and a pseudo-localized copy of the source (accented, 40% longer, in brackets) written to
  `reviews/R-0001/pseudo-locale/` for the UI pass to load.
- **Metrics.** Counts and coverage go to `metrics` (method `counted`, the run named in the note), one row per name and
  scope: `scope` is `all`, a path, or `path#function`.

### The adapter

An adapter is one `tool_adapter(...)` call in `dev/core.py`; `TOOL_ADAPTERS` holds them all.

```python
tool_adapter("semgrep", fmt="sarif", parse=parse_sarif, domain="security", binary="semgrep", argv=_semgrep_argv,
             ok_exits=(0, 1), install="pipx install semgrep==<pinned>", fallback="what the reviewer does instead",
             online="optional")
```

| argument | what it is |
|---|---|
| `name` | `tool-run NAME`, the key in `tools.lock`, the prefix of the output file |
| `fmt` | `sarif`, `junit`, `lcov`, `cobertura`, `json` or `jsonl` |
| `parse(data, call)` | the output bytes to `{"leads": [...], "metrics": [(name, scope, value, unit)], "version": str or None}`; each lead from `_lead(rule, level, path, start, end, message, domain=None)`, paths through `_relative_lead_path(raw, call)`; raise `WriteError` for output it cannot read (nothing is then written) |
| `argv(call)` | the command as a list, run in `call.work` (the export); write to `call.out` or set `stdout=True`; register extra temporary paths in `call.placeholders`; raise `ToolSkip(reason)` when there is nothing to run on |
| `run(call)` | instead of `argv`: a built-in that returns `(exit code, output bytes)` |
| `binary`, `version_args` | the executable `env` and `tool-run` look for, and how it prints its version |
| `ok_exits` | the exit codes that still mean complete output (scanners exit 1 when they find something) |
| `store(data)` | the bytes kept and hashed, when the output holds what must not be kept (gitleaks: secret values) |
| `domain` | whose reviewer gets the leads when `--domain` is not given; a lead may name its own (`_lead(..., domain=)`) |
| `source` | `checkout` (the export) or `repo` (history tools) |
| `install`, `fallback`, `online` | shown by `env` and recorded on an absent or skipped run |

`call` (a `ToolCall`) carries `work`, `out`, `repo_dir`, `commit`, `rules`, `online`, `ledger_dir` (the ledger's
folder, for cached rules and databases) and the parsed arguments `a`.

Adding a tool takes four things: the adapter; its version in `tools.lock`; a recorded output in
`dev/tests/data/tools/<tool>.<ext>`; and a test that imports it with `--from` (it runs without the tool) and, where
the tool runs, one through a stand-in executable on `PATH` (`dev/tests/_w7a_tools_helpers.py`, `shim()`).

### The recorded samples

Real outputs, from the pinned versions run on a small scratch project on 2026-11-11: `semgrep.sarif` (one local
rule), `ast-grep.jsonl` (the shipped packs), `zizmor.sarif`, `actionlint.json`, `gitleaks.json` (run with `--redact`), `scc.json`, `jscpd.json` (its
`detectionDate` makes each run's bytes differ; the leads do not), `junit.xml` (pytest), `coverage.lcov` and
`coverage.xml` (coverage.py; the `<source>` folder replaced with `/project`), `osv-no-database.json` (OSV-Scanner
offline with no database). From 2026-11-12: `vale.json` (Vale 3.26.0 with the shipped rules on `vale-samples/`, where every rule raises its
alert), `lychee.json` (lychee 0.24.2 offline; the scratch folder replaced with `/project`), `axe.json` (axe-core
4.14.0 in Chrome, trimmed to its violations), `wallace.json` (wallace-cli 4.0.5), `impeccable.json` (Impeccable
4.8.2), `stylelint.json` (Stylelint 18.0.0 with the generated token rule), `schemathesis.xml` (4.33.2 against a
small local API), `buf.jsonl` (1.75.0), `graphql-inspector.txt` (7.0.2), `squawk.json` (2.71.0), `hyperfine.json`
(2.0.0), `i18n-lint.json` (ESLint 10.14.0 with eslint-plugin-i18next 6.1.5), `pg-explain.json` (PostgreSQL 16.15).
`oasdiff-documented.json` and `hurl-documented.xml` are written from the documented formats: neither oasdiff
(the Go module proxy refused it here) nor Hurl (it needs a newer Rust) could be installed, so both adapters and
the pins of oasdiff 1.30.0 and Mailpit 1.30.2 are not verified. Mailpit was not run either: its test serves the
API as Mailpit's documentation describes it. Written from the documented format: those whose names end in `-documented`
(`cobertura-methods-documented.xml`, since coverage.py writes no methods; `osv-documented.json`: the proxy here refused the advisory database download, so OSV-Scanner's `--online` path is
not verified).

### Harness features (optional)

Each has a fallback that works in any harness; none is required.

| harness | feature a researcher found | fallback |
|---|---|---|
| Claude Code | `claude -p --output-format json --json-schema` validates a reviewer's output at generation and reports `total_cost_usd` | validate after the fact with `lint-report`; record cost by hand |
| Claude Code | plugin packaging: agents with `disallowedTools`, a `PreToolUse` hook that refuses writes outside `.code-review/`, a workflow script, `claude plugin eval` | the manager briefs each reviewer as read-only and checks `git status` after each run |
| Claude Code | skill frontmatter `allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ledger.py *)` stops permission prompts for the ledger tool | the harness asks once per command |
| OpenCode | agent and command files under `~/.config/opencode/` with per-agent permissions (V2 configuration; whether a skill can carry them is not verified) | as above |
| both | MCP connectors (Playwright MCP, Semgrep, Serena, GitHub, Figma and others) | each has a CLI or script equivalent, which is better for repeatability because the command can be stored |

## Correctness and architecture

### Spec conformance (`spec`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [ripgrep](https://github.com/BurntSushi/ripgrep) | must-have | Exhaustive, ordered list of every REQ-/G- code or quoted phrase in code, tests and docs; an agent's grep is sampled, this is complete | rg --json -n 'REQ-\d{4}' ; parse JSON lines (path, line, match) | yes | single binary; MIT/Unlicense; often already bundled with the harness | yes | git grep -n (always present in a git repo) | - |
| [Test-runner requirement markers (JUnit XML)](https://github.com/testmoapp/junitxml) | must-have | A convention, not a product: tests carry the requirement code (pytest marker, JUnit @Tag, test name suffix); the ledger joins JUnit XML to requirements, so 'a test asserts it' is a lookup | pytest --junitxml=out.xml -o junit_family=xunit2 ; go test -json \| gotestsum --junitfile ; parse testcase name/classname/properties | yes | nothing to install | yes | rg REQ- in test files | runs code |
| [OpenFastTrace](https://github.com/itsallcode/openfasttrace) | strong | Computes the full trace: every requirement item covered / uncovered / orphaned / outdated-revision, from tags like [impl-&gt;dsn~name~1] | java -jar openfasttrace.jar trace -o aspec\|plain\|html &lt;dirs&gt; ; parse aspec XML or plain text; exit code non-zero on defects | yes | one jar, needs Java 11+ (version not verified); GPL-3.0 | yes | rg for REQ- codes plus ledger trace.tsv | - |
| [StrictDoc](https://github.com/strictdoc-project/strictdoc) | strong | Requirements in .sdoc text files, forward/backward links to source ranges and tests, coverage of requirements by code | strictdoc export --formats=json,html &lt;dir&gt; ; parse JSON; source links via @relation(REQ-1, scope=function) markers | yes | pip/pipx; Apache-2.0 | yes | ledger requirements table (add-requirement) stays the store | - |
| [reqstool](https://github.com/Luftfartsverket/reqstool-client) | strong | Joins requirements.yml, @Requirements/@SVCs annotations and JUnit results into one status: implemented? tested? passing? | reqstool status local -p &lt;dir&gt; ; reqstool report ... ; JSON export (flag names not verified) | yes | pip; MIT (not verified) | yes | ledger load-index --trace | - |
| [pandoc](https://github.com/jgm/pandoc) | strong | Turns requirement sources (Word, HTML, wiki exports) into plain text with stable line numbers so every quoted requirement has a location the ledger can cite | pandoc spec.docx -t plain --wrap=none -o spec.txt ; then number lines | yes | single binary ~150 MB; GPL-2.0+ | yes | agent reads the document directly; location is 'section heading' only | - |
| [MarkItDown](https://github.com/microsoft/markitdown) | strong | Same as pandoc but handles PDF, slides and spreadsheets; also ships an MCP server (markitdown-mcp) | markitdown spec.pdf &gt; spec.md ; or MCP tool convert_to_markdown | yes (text PDFs); OCR/LLM image modes are not | pip; MIT | yes | pdftotext (poppler) | - |
| [Issue-tracker connectors (GitHub MCP server, gh CLI, Atlassian remote MCP)](https://github.com/github/github-mcp-server) | strong | Requirements and decisions that live in issues, PR descriptions and Jira/Confluence, with stable ids and dates | gh issue list --json number,title,body,labels,closedAt --state all ; or MCP tools (list_issues, search) ; Atlassian remote MCP for Jira/Confluence (not verified this session) | yes for a fixed query at a fixed time | gh is one binary (MIT); MCP servers need a token; tracker itself may be paid | no | owner pastes or exports the issues | online |

Top 3 (each optional, with the fallback in its row): a ripgrep scan for requirement codes joined to JUnit XML test names; OpenFastTrace where the owner tags code; pandoc or MarkItDown to turn Word, PDF and wiki sources into text with line numbers.

All 18 rows, situational and skipped included: `tools.jsonl`, domain `spec`.

### Test coverage (`tests`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [coverage.py (+ pytest-cov)](https://github.com/nedbat/coveragepy) | must-have | Line and branch coverage per file and function, and with dynamic contexts which test executed each line; proves 'no test reaches this branch' | coverage run --branch -m pytest ; coverage json -o cov.json --show-contexts ; or pytest --cov --cov-branch --cov-context=test --cov-report=json | yes | pip; Apache-2.0 | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [c8 / Istanbul (nyc), Vitest and Jest coverage](https://github.com/bcoe/c8) | must-have | V8 or Istanbul line/branch/function coverage | c8 --reporter=json-summary --reporter=lcov npm test ; vitest run --coverage --coverage.reporter=json ; jest --coverage --coverageReporters=json-summary lcov | yes | npm dev dependency or npx; ISC/MIT | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [go test -cover](https://pkg.go.dev/cmd/go#hdr-Testing_flags) | must-have | Statement coverage per function; test list | go test ./... -coverprofile=c.out -covermode=atomic ; go tool cover -func=c.out ; go test -list . ; go test -json | yes | built into Go | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [cargo-llvm-cov](https://github.com/taiki-e/cargo-llvm-cov) | must-have | Source-based line/region/branch coverage | cargo llvm-cov --json --output-path cov.json (or --lcov) | yes | cargo install + llvm-tools component; Apache-2.0/MIT | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [JaCoCo / Kover](https://github.com/jacoco/jacoco) | must-have | Instruction, branch, line, method coverage | mvn test jacoco:report -&gt; target/site/jacoco/jacoco.xml ; gradle koverXmlReport ; parse XML | yes | build plugin; EPL-2.0 / Apache-2.0 | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [coverlet + ReportGenerator](https://github.com/coverlet-coverage/coverlet) | must-have | Line/branch/method coverage in Cobertura or OpenCover XML; ReportGenerator (danielpalme/ReportGenerator) merges and converts formats for any language | dotnet test --collect:'XPlat Code Coverage' -&gt; coverage.cobertura.xml ; reportgenerator -reporttypes:JsonSummary | yes | NuGet; MIT / Apache-2.0 | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [SimpleCov](https://github.com/simplecov-ruby/simplecov) | must-have | Line and branch coverage | COVERAGE=1 bundle exec rspec -&gt; coverage/.resultset.json | yes | gem; MIT | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [PHPUnit coverage (PCOV / Xdebug)](https://github.com/krakjoe/pcov) | must-have | Line/branch coverage and the CRAP score (complexity x lack of coverage) per method | php -d pcov.enabled=1 vendor/bin/phpunit --coverage-clover clover.xml --coverage-crap4j crap.xml | yes | PHP extension; PHP licence | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [gcovr / lcov / llvm-cov](https://github.com/gcovr/gcovr) | must-have | Line/branch/function coverage | gcovr --json-summary -o cov.json (or --cobertura) ; llvm-cov export -format=lcov | yes | pip; BSD-3 ; lcov is linux-test-project/lcov (GPL-2.0) | yes | agent reads tests and code; totals marked "estimated" | - |
| [Test listers (built into runners)](https://docs.pytest.org/) | must-have | The exact list of existing tests, parametrized ids included, instead of the agent counting by reading | pytest --collect-only -q ; jest --listTests ; vitest list --json ; go test -list . ; cargo test -- --list ; dotnet test --list-tests ; rspec --dry-run -f json ; phpunit --list-tests-xml | yes | nothing to install | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [Type checkers (pyright, mypy, tsc, and compilers)](https://github.com/microsoft/pyright) | must-have | Type vs schema vs validation mismatches, unreachable branches, optional values used unchecked | pyright --outputjson ; mypy --output json ; tsc --noEmit --pretty false ; phpstan analyse --error-format=json | yes | npm/pip; MIT | yes | agent reads tests and code; totals marked "estimated" | - |
| [diff-cover](https://github.com/Bachmann1234/diff_cover) | strong | Coverage of only the lines changed since a base commit; lists uncovered changed lines | diff-cover coverage.xml --compare-branch=&lt;last reviewed sha&gt; --json-report dc.json | yes | pip; Apache-2.0 | yes | ledger joins lcov with git diff itself | - |
| [Universal Ctags](https://github.com/universal-ctags/ctags) | strong | A flat inventory of every function, class, method and constant with file and line: the 'EVERY unit' list | ctags -R --output-format=json --fields=+neKS --extras=-F -f - . | yes | distro package; GPL-2.0 | yes | ast-grep or tree-sitter tags; else LSP documentSymbol | - |
| [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | strong | Generates boundary and adversarial inputs and shrinks failures to a minimal example; ghostwriter drafts property tests for a function | hypothesis write pkg.func &gt; test_prop.py ; pytest -p hypothesis --hypothesis-seed=0 --hypothesis-show-statistics | yes with derandomize=True or a fixed seed | pip; MPL-2.0 | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [fast-check](https://github.com/dubzzz/fast-check) | strong | Property-based testing with shrinking; model-based commands; async race scheduler | vitest/jest test using fc.assert(..., {seed: 42, numRuns: 1000}) | yes with seed | npm; MIT | yes | agent reads tests and code; totals marked "estimated" | runs code |
| [Schemathesis (see contracts)](https://github.com/schemathesis/schemathesis) | strong | Generates the per-route cases (valid, invalid, boundary) and checks responses against the schema | st run openapi.yaml --url http://localhost:8000 --report junit (flag spelling varies by version) | yes with --seed | pip/uvx; MIT | yes | agent reads tests and code; totals marked "estimated" | - |

Top 3 (each optional, with the fallback in its row): the ecosystem's coverage tool (with per-test contexts where it has them); test listers and Universal Ctags for the exact list of tests and units; a type checker in JSON mode.

All 25 rows, situational and skipped included: `tools.jsonl`, domain `tests`.

### Test suite quality (`test-quality`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [StrykerJS](https://github.com/stryker-mutator/stryker-js) | must-have | Mutation score per file and the list of surviving mutants with line and replacement: tests that run code without checking it | npx stryker run --reporters json --incremental -&gt; reports/mutation/mutation.json (mutation-testing-report-schema) | yes (same code, same mutants); timeouts can flip on a loaded machine | npm dev dependency, heavy run time; Apache-2.0 | yes | sample assertions by hand; marked "not measured" | runs code |
| [mutation-testing-report-schema](https://github.com/stryker-mutator/mutation-testing-elements) | must-have | One JSON shape for mutants (file, location, mutator, status, killedBy, coveredBy) | parse mutation.json | yes | nothing to install; Apache-2.0 | yes | sample assertions by hand; marked "not measured" | - |
| [Stryker.NET](https://github.com/stryker-mutator/stryker-net) | must-have | As StrykerJS | dotnet stryker --reporter json -&gt; StrykerOutput/**/mutation-report.json | yes | dotnet tool; Apache-2.0 | yes | sample assertions by hand; marked "not measured" | runs code |
| [mutmut](https://github.com/boxed/mutmut) | must-have | Mutation testing with per-function mutants; v3 runs in-process and is much faster than v2 | mutmut run ; mutmut results ; mutmut export-cicd-stats -&gt; mutants/mutmut-cicd-stats.json (v3; file name not verified) | yes | pip (v3 needs fork(): Linux/macOS/WSL only); BSD-3 | yes | sample assertions by hand; marked "not measured" | runs code |
| [cargo-mutants](https://github.com/sourcefrog/cargo-mutants) | must-have | Mutants that no test catches, with function and replacement | cargo mutants --json ... -&gt; mutants.out/outcomes.json, missed.txt, caught.txt ; --in-diff file.diff for changed code only | yes | cargo install; MIT | yes | sample assertions by hand; marked "not measured" | runs code |
| [PIT (pitest)](https://github.com/hcoles/pitest) | must-have | Mutation coverage and test strength per class; fast bytecode mutation | mvn org.pitest:pitest-maven:mutationCoverage -DoutputFormats=XML,CSV -&gt; target/pit-reports/mutations.xml | yes | build plugin; Apache-2.0 | yes | sample assertions by hand; marked "not measured" | runs code |
| [Infection](https://github.com/infection/infection) | must-have | Mutation Score Indicator, covered MSI, escaped mutants | vendor/bin/infection --logger-json=infection.json --min-msi=0 (logger option names: check --help) | yes | composer; BSD-3 | yes | sample assertions by hand; marked "not measured" | runs code |
| [Random-order and repeat runs (runner flags and plugins)](https://github.com/pytest-dev/pytest-randomly) | must-have | Order-dependent and flaky tests found by rerunning with recorded seeds | pytest -p randomly --randomly-seed=N (pytest-randomly) ; pytest --count=5 (pytest-repeat) ; go test -shuffle=N -count=5 ; jest --randomize --seed=N ; vitest --sequence.shuffle --sequence.seed=N ; rspec --order rand:N ; phpunit --order-by=random --random-order-seed=N ; cargo nextest run --retries 2 | yes for a given seed; the flake itself may not be | pytest plugins: pip, MIT; others built in | yes | sample assertions by hand; marked "not measured" | runs code |
| [libfaketime + TZ matrix](https://github.com/wolfcw/libfaketime) | must-have | Runs the unchanged suite at another date and zone; tests that then fail depend on the real clock or the machine's zone | TZ=Pacific/Kiritimati faketime '2028-02-29 23:59:30' &lt;test command&gt; ; repeat for TZ=America/St_Johns, a DST gap date, Dec 31 ; diff the failing-test sets | yes | distro package (apt install faketime); GPL-2.0 | yes | TZ= variable alone (works everywhere, zone only) | runs code |
| [Network-off run (unshare -n / bwrap / pytest-socket)](https://github.com/miketheman/pytest-socket) | must-have | Tests that fail with networking removed depend on the network | unshare -rn &lt;test command&gt; (or bwrap --unshare-net --dev-bind / / ...) ; pytest --disable-socket --allow-unix-socket | yes | util-linux is preinstalled on Linux; macOS has no equivalent without Docker (docker run --network none) | yes | docker run --network none ; else grep for http clients in tests | runs code |
| [Cosmic Ray](https://github.com/sixty-north/cosmic-ray) | strong | Mutation testing with a session database; works on Windows; distributed runs | cosmic-ray init cfg.toml s.sqlite ; cosmic-ray exec cfg.toml s.sqlite ; cr-report s.sqlite ; cr-rate s.sqlite | yes | pip; MIT | yes | sample assertions by hand; marked "not measured" | runs code |
| [Gremlins](https://github.com/go-gremlins/gremlins) | strong | Mutation testing with killed/lived/not-covered counts | gremlins unleash --output=gremlins.json | yes | single binary; Apache-2.0 | yes | sample assertions by hand; marked "not measured" | runs code |
| [cargo-nextest](https://github.com/nextest-rs/nextest) | strong | Per-test process isolation, retries with flaky marking, JUnit output, timings | cargo nextest run --profile ci -&gt; target/nextest/ci/junit.xml | yes | cargo install; Apache-2.0/MIT | yes | sample assertions by hand; marked "not measured" | runs code |
| [gotestsum](https://github.com/gotestyourself/gotestsum) | strong | JUnit XML and slowest-test list from go test -json; --rerun-fails | gotestsum --junitfile j.xml --jsonfile t.json -- -shuffle=on -count=3 ./... ; gotestsum tool slowest --jsonfile t.json | yes | single binary; Apache-2.0 | yes | sample assertions by hand; marked "not measured" | - |
| [Test-lint rules (eslint-plugin-jest / vitest, ruff PT, rubocop-rspec, testifylint)](https://github.com/jest-community/eslint-plugin-jest) | strong | Tests with no assertion, disabled or focused tests, conditional expects, duplicate titles | npx eslint -f json --rule 'jest/expect-expect: error' --rule 'jest/no-disabled-tests: error' tests/ ; ruff check --select PT --output-format json ; rubocop --only RSpec -f json ; golangci-lint run --enable testifylint --out-format json (flag names change between major versions) | yes | npm/pip/gem; MIT | yes | sample assertions by hand; marked "not measured" | runs code |
| [CI history via gh](https://github.com/cli/cli) | strong | Jobs that failed then passed on rerun without a code change: real-world flakiness evidence | gh run list --json databaseId,conclusion,headSha,attempt -L 500 ; gh run view &lt;id&gt; --json jobs | yes for a fixed window | one binary; MIT | no | sample assertions by hand; marked "not measured" | online |

Top 3 (each optional, with the fallback in its row): mutation testing by language (Stryker, mutmut, cargo-mutants, PIT, Infection, Gremlins); seeded random-order and repeated runs; libfaketime with a TZ matrix and a network-off run.

All 26 rows, situational and skipped included: `tools.jsonl`, domain `test-quality`.

### Time, dates and concurrency (`time-concurrency`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [ast-grep date/time rule pack](https://github.com/ast-grep/ast-grep) | must-have | The complete inventory of places that create, parse, compare or format a date (new Date(), Date.now(), datetime.now(), time.Now(), LocalDate.now(), strtotime ...) as file:line rows | ast-grep scan --rule &lt;skill&gt;/rules/time/*.yml --json=stream . | yes | single binary (npm, pip, cargo, brew); MIT | yes | ripgrep with the same API names (more false positives) | - |
| [ruff DTZ rules (flake8-datetimez)](https://github.com/astral-sh/ruff) | must-have | Every naive datetime call (now() without tz, utcnow, strptime without %z, fromtimestamp without tz) | ruff check --select DTZ --output-format json . | yes | single binary; MIT | yes | agent inventories by reading and grep; marked "not measured" | - |
| [zdump / tzdata (or Python zoneinfo)](https://www.iana.org/time-zones) | must-have | The actual DST transition instants for the project's zones and years: the 'concrete dates to test' come from data, not memory | zdump -v -c 2026,2029 America/Los_Angeles ; or python3 -c with zoneinfo (stdlib 3.9+) | yes (for a given tzdata version; record it) | preinstalled on Linux/macOS; public domain | yes | agent inventories by reading and grep; marked "not measured" | - |
| [libfaketime](https://github.com/wolfcw/libfaketime) | must-have | Run the app or suite at a named instant (DST gap, leap day, year rollover) without code changes | faketime -f '2027-03-14 01:59:50' &lt;cmd&gt; ; FAKETIME='@2027-11-07 01:30:00' LD_PRELOAD=... | yes | distro package; GPL-2.0 | yes | in-language clock fakes | runs code |
| [Go race detector](https://go.dev/doc/articles/race_detector) | must-have | Data races observed at run time with both stacks | go test -race -count=3 ./... 2&gt; race.txt ; parse 'WARNING: DATA RACE' blocks | no (finds only races that occur in that run); a reported race is real | built in (needs cgo) | yes | agent inventories by reading and grep; marked "not measured" | runs code |
| [Semgrep CE / Opengrep](https://github.com/semgrep/semgrep) | strong | Same inventory with taint-style patterns (e.g. a naive datetime flowing into a comparison); community rules for time and concurrency misuse | semgrep scan --config &lt;skill&gt;/rules/time --json -o out.json --metrics=off (opengrep: same flags) | yes | pip/brew, ~200 MB; engine LGPL-2.1; registry rules have their own licence; cross-file analysis is paid | yes | ast-grep rules | online in some modes |
| [Error Prone time checks](https://github.com/google/error-prone) | strong | JavaTimeDefaultTimeZone, MisusedWeekYear (YYYY), JodaTime misuse, FromTemporalAccessor, plus GuardedBy and other concurrency checks | javac plugin via Maven/Gradle; warnings on compiler output (-XepAllErrorsAsWarnings) | yes | build plugin; Apache-2.0 | yes | agent inventories by reading and grep; marked "not measured" | runs code |
| [SpotBugs](https://github.com/spotbugs/spotbugs) | strong | Multithreaded-correctness category (inconsistent synchronization, static Calendar/DateFormat, wait/notify misuse) | spotbugs -textui -sarif -output out.sarif build/classes (or Maven/Gradle plugin, XML) | yes | jar; LGPL-2.1 | yes | agent inventories by reading and grep; marked "not measured" | runs code |
| [rubocop-rails time cops](https://github.com/rubocop/rubocop-rails) | strong | Rails/TimeZone, Rails/Date, Rails/TimeZoneAssignment: Time.now vs Time.zone.now, Date.today | rubocop --only Rails/TimeZone,Rails/Date -f json | yes | gem; MIT | yes | agent inventories by reading and grep; marked "not measured" | - |
| [go vet / staticcheck / golangci-lint](https://github.com/golangci/golangci-lint) | strong | copylocks, lostcancel, unsynchronized patterns; staticcheck (dominikh/go-tools) SA-series for time.Tick leaks, defers in loops | go vet -json ./... ; staticcheck -f json ./... ; golangci-lint run --output.json.path out.json (v2 flag; v1 used --out-format json) | yes | binaries; BSD/MIT/GPL-3.0 (golangci-lint) | yes | agent inventories by reading and grep; marked "not measured" | - |
| [Clock fakes (time-machine, freezegun, @sinonjs/fake-timers, timecop, clockwork, FakeTimeProvider, testing/synctest)](https://github.com/adamchainz/time-machine) | strong | Deterministic control of 'now' inside a probe test | used inside a probe test the reviewer writes; record it with add-artifact probe-script | yes | library; MIT/Apache | yes | agent inventories by reading and grep; marked "not measured" | - |
| [ThreadSanitizer](https://clang.llvm.org/docs/ThreadSanitizer.html) | strong | Data races and lock-order inversions at run time | clang -fsanitize=thread -g ... ; run tests ; parse 'WARNING: ThreadSanitizer' reports | no (schedule-dependent) | part of clang/gcc | yes | agent inventories by reading and grep; marked "not measured" | runs code |
| [fast-check scheduler](https://github.com/dubzzz/fast-check) | strong | Reorders promise resolution to find async race conditions (double submit, stale response wins) and shrinks to the failing order | fc.assert(fc.asyncProperty(fc.scheduler(), async s =&gt; ...), {seed}) | yes with seed | npm; MIT | yes | agent inventories by reading and grep; marked "not measured" | - |
| [Concurrent request probes (hey, oha, k6, curl --parallel)](https://github.com/hatoo/oha) | strong | Fires N identical or conflicting saves at once against a local instance to show duplicates and lost updates | oha -n 50 -c 50 -m POST -d @body.json --output-format json http://localhost/x ; curl --parallel --parallel-immediate ... ; then count rows in the DB | no (timing) but a duplicate row is proof | single binaries; MIT (oha, hey Apache-2.0), k6 AGPL-3.0 | yes | xargs -P with curl | - |

Top 3 (each optional, with the fallback in its row): an ast-grep rule pack for date, time and shared-state sites (plus ruff DTZ and similar); tzdata transition dates (zdump, or zoneinfo inside ledger.py); libfaketime at those instants, with the language's race tool.

All 28 rows, situational and skipped included: `tools.jsonl`, domain `time-concurrency`.

### Failure handling and resilience (`resilience`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Timeout / retry / swallowed-error rule pack (ast-grep or Semgrep)](https://github.com/ast-grep/ast-grep) | must-have | Inventory of every outbound call (fetch, axios, requests, httpx, http.Client, HttpClient, DB and queue clients) with whether a timeout, AbortSignal, retry or idempotency key is present; every empty catch | ast-grep scan --rule &lt;skill&gt;/rules/resilience/*.yml --json=stream . ; rows: path, line, rule id (call-no-timeout, catch-empty, retry-no-backoff) | yes | single binary; MIT | yes | ripgrep for client API names | - |
| [Linter rules that already exist for this](https://github.com/astral-sh/ruff) | must-have | Named, maintained rules for missing timeouts, ignored errors and unawaited promises | ruff check --select S113,BLE,E722,S110,ASYNC --output-format json ; golangci-lint run --enable noctx,bodyclose,errcheck ; eslint -f json | yes | already present in most projects; MIT mostly | yes | agent reasons from code; scenario marked "not exercised" | - |
| [Toxiproxy](https://github.com/Shopify/toxiproxy) | must-have | Injects latency, timeouts, connection resets, bandwidth limits and slow closes between the app and a dependency, so the 'slow / down / partial' columns are observed | toxiproxy-server & ; toxiproxy-cli create -l :15432 -u localhost:5432 pg ; toxiproxy-cli toxic add -t latency -a latency=5000 pg ; run the scenario ; HTTP API on :8474 returns JSON | yes (fault is exact; app timing may vary) | two Go binaries; MIT | yes | iptables/tc netem, or stop the dependency process | - |
| [WireMock](https://github.com/wiremock/wiremock) | strong | Stub an external API with delays, faults (connection reset, empty or malformed response), stateful scenarios, and a request journal showing every retry the app made | java -jar wiremock-standalone.jar --port 8089 ; POST /__admin/mappings ; GET /__admin/requests (JSON) to count calls | yes | one jar (Java); Apache-2.0 | yes | agent reasons from code; scenario marked "not exercised" | - |
| [mitmproxy](https://github.com/mitmproxy/mitmproxy) | strong | Scriptable proxy: drop, delay, truncate or rewrite responses of real third-party calls; records flows | mitmdump -s faults.py -w flows.mitm ; mitmdump -nr flows.mitm --set hardump=out.har | yes for a script | pip/binary; MIT | yes | agent reasons from code; scenario marked "not exercised" | - |
| [Prism](https://github.com/stoplightio/prism) | strong | Mock server from the dependency's OpenAPI that can return each documented error (Prefer: code=503) and validate the app's requests | prism mock dep-openapi.yaml -p 4010 ; requests with header 'Prefer: code=429' | yes | npm; Apache-2.0 | yes | agent reasons from code; scenario marked "not exercised" | - |
| [strace fault injection](https://strace.io/) | strong | Makes chosen system calls fail or stall (write returns ENOSPC, connect ETIMEDOUT, fsync EIO) on the Nth call | strace -f -e trace=write -e inject=write:error=ENOSPC:when=3+ &lt;cmd&gt; | yes | preinstalled or distro package; LGPL-2.1+ | yes | kill -9 at a breakpoint or sleep | runs code |
| [Network-off run (unshare -n)](https://man7.org/linux/man-pages/man1/unshare.1.html) | strong | Starts the app or job with no network at all: shows what the user and the log see when every dependency is down | unshare -rn &lt;cmd&gt; 2&gt; err.txt ; record exit code and output | yes | preinstalled (util-linux) | yes | docker run --network none | runs code |
| [Mailpit](https://github.com/axllent/mailpit) | strong | Captures outgoing mail with an API, and has chaos triggers for SMTP errors; counts duplicate emails after a retry | mailpit & ; GET http://localhost:8025/api/v1/messages (JSON) | yes | single binary; MIT | yes | agent reasons from code; scenario marked "not exercised" | - |

Top 3 (each optional, with the fallback in its row): a rule pack plus existing linter rules for calls without timeout, unawaited promises, ignored errors and empty catch; Toxiproxy (WireMock or Prism for HTTP dependencies); strace fault injection and unshare -rn.

All 23 rows, situational and skipped included: `tools.jsonl`, domain `resilience`.

### Backward compatibility and migrations (`compatibility`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [oasdiff](https://github.com/oasdiff/oasdiff) | must-have | Classified breaking changes and a changelog between two versions of an API description (hundreds of checks) | git show &lt;old&gt;:openapi.yaml &gt; old.yaml ; oasdiff breaking old.yaml openapi.yaml -f json ; oasdiff changelog ... -f json | yes | single Go binary; Apache-2.0 | yes | agent diffs the files at the two commits by reading | - |
| [Buf](https://github.com/bufbuild/buf) | must-have | Wire- and source-compatibility breaking-change detection against any git ref, plus lint | buf breaking --against '.git#ref=&lt;old sha&gt;' --error-format=json ; buf lint --error-format=json | yes | single binary; Apache-2.0 | yes | agent diffs the files at the two commits by reading | - |
| [GraphQL Inspector](https://github.com/graphql-hive/graphql-inspector) | must-have | Schema diff classified breaking / dangerous / safe; also validate documents against a schema | graphql-inspector diff 'git:&lt;old sha&gt;:schema.graphql' schema.graphql ; exit code + text (JSON via programmatic API) | yes | npm; MIT | yes | agent diffs the files at the two commits by reading | - |
| [Squawk](https://github.com/sbdchd/squawk) | must-have | Migration statements that take long locks, rewrite tables, or break old code still running during deploy (rename, drop, NOT NULL without default, non-concurrent index) | squawk --reporter json migrations/*.sql (reporters: Tty, Gcc, Json; check --help) | yes | single binary (npm/pip/cargo); Apache-2.0/MIT | yes | agent diffs the files at the two commits by reading | - |
| [Old-version fixture replay (git worktree + project tests)](https://git-scm.com/docs/git-worktree) | must-have | Build data with the previous release, read it with the current one: the direct test of 'can new code read everything old code wrote' | git worktree add /tmp/old &lt;last release tag&gt; ; run old seed/migrate to produce a DB or settings file ; run current migrations and read path against it | yes | git only | yes | agent diffs the files at the two commits by reading | runs code |
| [cargo-semver-checks](https://github.com/obi1kenobi/cargo-semver-checks) | strong | Semver violations in a crate's public API against a baseline | cargo semver-checks --baseline-rev &lt;sha&gt; | yes | cargo install; Apache-2.0/MIT | yes | agent diffs the files at the two commits by reading | - |
| [apidiff / gorelease](https://github.com/golang/exp) | strong | Incompatible changes to exported API | apidiff -m old.export new.export ; gorelease -base=v1.2.3 | yes | go install golang.org/x/exp/cmd/apidiff; BSD-3 | yes | agent diffs the files at the two commits by reading | - |
| [japicmp / Revapi / Kotlin binary-compatibility-validator](https://github.com/siom79/japicmp) | strong | Binary and source compatibility between two jars; API dump files that diff in git | java -jar japicmp.jar -o old.jar -n new.jar --only-incompatible --xml-output-file r.xml ; gradle apiCheck | yes | jar / Gradle plugin; Apache-2.0 | yes | agent diffs the files at the two commits by reading | runs code |
| [Microsoft.DotNet.ApiCompat / PublicApiAnalyzers](https://github.com/dotnet/sdk) | strong | Breaking changes against a baseline package; PublicAPI.Shipped.txt diffs | dotnet pack -p:EnablePackageValidation=true -p:PackageValidationBaselineVersion=1.0.0 ; apicompat tool | yes | SDK built in; MIT | yes | agent diffs the files at the two commits by reading | runs code |
| [Griffe](https://github.com/mkdocstrings/griffe) | strong | API breakages (removed objects, changed parameters, changed defaults) between two git refs | griffe check pkg -a &lt;old ref&gt; -s src -f oneline\|verbose\|markdown\|github (no JSON format documented) | yes | pip/uvx; ISC | yes | agent diffs the files at the two commits by reading | - |
| [Atlas](https://github.com/ariga/atlas) | strong | 50+ migration analyzers (destructive, data-dependent, backward-incompatible, locks), schema inspect and schema diff between any two states | atlas migrate lint --dir file://migrations --dev-url 'docker://postgres/16/dev' --format '{{ json . }}' ; atlas schema diff --from ... --to ... | yes | single binary; Apache-2.0 source, default binary under Atlas EULA; which commands need a (free) login or paid plan has changed: not verified | yes | agent diffs the files at the two commits by reading | runs code |
| [strong_migrations / django-migration-linter / pytest-alembic / django-test-migrations](https://github.com/ankane/strong_migrations) | strong | Framework-aware unsafe-migration checks; tests that every migration applies up and down and that models match migrations | python manage.py lintmigrations --output-format json (flag not verified) ; pytest --test-alembic ; manage.py makemigrations --check --dry-run ; alembic check | yes | gem / pip; MIT | yes | agent diffs the files at the two commits by reading | runs code |
| [pg-schema-diff / migra (migradiff) / sqldef / schemadiff](https://github.com/stripe/pg-schema-diff) | strong | The SQL needed to go from schema A to schema B, with hazards flagged: detects drift between migrations and the declared or live schema | pg-schema-diff plan --dsn ... --schema-dir schema/ ; sqlite3def --dry-run db &lt; schema.sql | yes | single binaries; MIT | yes | agent diffs the files at the two commits by reading | - |

Top 3 (each optional, with the fallback in its row): a contract diff against the last reviewed commit (oasdiff, Buf, GraphQL Inspector, or the library-API checkers); Squawk or Atlas on every new migration; old-version fixture replay in a git worktree.

All 27 rows, situational and skipped included: `tools.jsonl`, domain `compatibility`.

### Decision placement (`decision-placement`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [ast-grep](https://github.com/ast-grep/ast-grep) | must-have | Structural inventory of decision sites: every permission call, every comparison against a named limit, every validation schema, every state assignment, by rule id | ast-grep scan --rule &lt;skill&gt;/rules/decisions/*.yml --json=stream . ; ast-grep run -p 'if ($U.role === $R) { $$$ }' --json | yes | single binary (npm i -g @ast-grep/cli, pip, cargo, brew); MIT | yes | ripgrep patterns | - |
| [jscpd](https://github.com/kucherenko/jscpd) | must-have | Token-level copy-paste detection across languages and layers: finds a rule pasted into client and server | npx jscpd --reporters json --min-tokens 40 --output out/ . -&gt; out/jscpd-report.json | yes | npm; MIT | yes | agent inventories by reading and grep | - |
| [Schema constraint dump (pg_dump -s, sqlite3 .schema, tbls)](https://github.com/k1LoW/tbls) | must-have | Every CHECK, UNIQUE, NOT NULL, FK and trigger: the decisions placed in the database, as a list | tbls out -t json ; pg_dump -s ; sqlite3 db .schema | yes | binary; MIT | yes | agent inventories by reading and grep | - |
| [Semgrep CE / Opengrep](https://github.com/semgrep/semgrep) | strong | Pattern + metavariable + intra-file taint rules: e.g. routes whose handler never reaches an authorization call; decisions checked in one path but not another | semgrep scan --config rules/ --json --metrics=off ; MCP: 'semgrep mcp' in the main binary (standalone semgrep/mcp repo is deprecated) | yes | pip/brew; LGPL-2.1 engine; cross-file (Pro) analysis is paid; Opengrep (opengrep/opengrep, LGPL-2.1) is the community fork | yes | agent inventories by reading and grep | online in some modes |
| [Language servers through the harness](https://microsoft.github.io/language-server-protocol/) | strong | Exact find-references and call hierarchy for a decision function: every caller, so 'written more than once' and 'checked in one path only' rest on the compiler's view | OpenCode: lsp tool (goToDefinition, findReferences, incomingCalls ...) behind OPENCODE_EXPERIMENTAL_LSP_TOOL=true per v1 docs; Claude Code: LSP servers come from plugins (.lsp.json) | yes for a given server version | language server per language | yes | ctags + ripgrep | - |
| [Serena](https://github.com/oraios/serena) | strong | MCP tools for symbol overview, find symbol, find referencing symbols, so reviewers ask for callers instead of grepping | MCP server: uvx --from git+https://github.com/oraios/serena serena start-mcp-server (see its README for the current command) | yes | uv + language servers; MIT (SolidLSP) and GPL-3.0-or-later (application) | yes | agent inventories by reading and grep | - |
| [PMD CPD](https://github.com/pmd/pmd) | strong | Duplicate detection that can ignore identifiers and literals (catches renamed copies) | pmd cpd --minimum-tokens 60 --language typescript --ignore-identifiers --ignore-literals --format xml --dir src | yes | zip + Java; BSD-style | yes | agent inventories by reading and grep | - |
| [CrossHair diffbehavior / differential property tests](https://github.com/pschanely/CrossHair) | strong | Searches for an input on which two copies of a decision give different answers | crosshair diffbehavior a.is_eligible b.is_eligible ; or a property test asserting f(x) == g(x) | yes with seed | pip; MIT | yes | agent inventories by reading and grep | - |
| [active_record_doctor / database_consistency](https://github.com/gregnavis/active_record_doctor) | strong | Model validations with no matching database constraint and the reverse (presence vs NOT NULL, uniqueness vs unique index, missing foreign keys) | bundle exec rake active_record_doctor ; bundle exec database_consistency -f json (flag not verified) | yes | gem; MIT | yes | agent inventories by reading and grep | runs code |
| [import-linter / Tach](https://github.com/seddonym/import-linter) | strong | Layer and forbidden-import contracts: proves rule code is or is not imported from the layer that should own it | lint-imports (exit code + text) ; tach check --output json (flag not verified) ; tach report | yes | pip; BSD-2 / MIT | yes | agent inventories by reading and grep | - |
| [dependency-cruiser / eslint-plugin-boundaries](https://github.com/sverweij/dependency-cruiser) | strong | Forbidden-dependency rules and the full module graph as JSON | npx depcruise src --config .dependency-cruiser.cjs --output-type json | yes | npm; MIT | yes | agent inventories by reading and grep | - |

Top 3 (each optional, with the fallback in its row): ast-grep with a decisions rule pack; jscpd (and PMD CPD) for copies of a rule; a schema constraint dump with language-server references.

All 24 rows, situational and skipped included: `tools.jsonl`, domain `decision-placement`.

### Internal contracts (`contracts`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Spec extraction from code (framework exporters)](https://github.com/tfranzel/drf-spectacular) | must-have | The contract the code implements, generated, to diff against the committed or documented one: 'docs accuracy' becomes a diff | python -c 'import json,app; print(json.dumps(app.app.openapi()))' &gt; impl.json ; manage.py spectacular --file impl.yaml ; then oasdiff diff docs/openapi.yaml impl.json -f json | yes | already in the project where used | yes | agent compares producer and consumer code by reading | runs code |
| [oasdiff (diff mode)](https://github.com/oasdiff/oasdiff) | must-have | Field-by-field drift between two descriptions of the same API (producer's vs consumer's copy, documented vs generated) | oasdiff diff a.yaml b.yaml -f json | yes | binary; Apache-2.0 | yes | agent compares producer and consumer code by reading | - |
| [Schemathesis](https://github.com/schemathesis/schemathesis) | must-have | Generated requests per endpoint; checks status codes, content types and response bodies against the schema, undocumented 5xx, and auth being ignored; stateful sequences via links | st run impl.json --url http://localhost:8000 --checks all --seed 1 --report junit,har (report flags differ between v3 and v4) | yes with --seed | pip/uvx/Docker; MIT | yes | agent compares producer and consumer code by reading | - |
| [Spectral / vacuum / Redocly CLI](https://github.com/stoplightio/spectral) | must-have | Lint the contract itself with rules: every operation has error responses, consistent error schema, no undocumented fields, naming rules | spectral lint api.yaml -f json ; vacuum lint -d api.yaml / vacuum report ; redocly lint --format json | yes | npm (Spectral, Redocly) / Go binary (vacuum); Apache-2.0 / MIT | yes | agent compares producer and consumer code by reading | - |
| [GraphQL Inspector (validate, coverage, similar)](https://github.com/graphql-hive/graphql-inspector) | must-have | validate: every client operation against the schema; coverage: which schema fields no client operation uses; similar: near-duplicate types | graphql-inspector validate './src/**/*.graphql' schema.graphql ; graphql-inspector coverage './src/**/*.graphql' schema.graphql --write cov.json | yes | npm; MIT | yes | agent compares producer and consumer code by reading | - |
| [Hurl](https://github.com/Orange-OpenSource/hurl) | must-have | Plain-text request files with assertions on status, headers, JSONPath, timing; reports in JSON, JUnit, TAP, HTML: a recorded probe of each response branch | hurl --test --report-json out/ --report-junit j.xml probes/*.hurl | yes | single binary (libcurl); Apache-2.0 | yes | agent compares producer and consumer code by reading | - |
| [Pact](https://github.com/pact-foundation/pact-reference) | strong | Consumer-driven contracts: the consumer's real expectations recorded as a pact file and verified against the provider | consumer tests write pacts/*.json ; pact_verifier_cli --dir pacts --hostname localhost --port 8000 --json out.json | yes | library + verifier binary; MIT | yes | agent compares producer and consumer code by reading | - |
| [Specmatic](https://github.com/specmatic/specmatic) | strong | Runs the spec as contract tests against the provider and as a stub for the consumer; backward-compatibility check between spec versions | specmatic test --host localhost --port 8000 spec.yaml ; specmatic stub spec.yaml ; specmatic backward-compatibility-check | yes | jar/Docker/npm; MIT licence file in repo (commercial add-ons exist) | yes | agent compares producer and consumer code by reading | - |
| [Prism (validation proxy)](https://github.com/stoplightio/prism) | strong | Sits between caller and callee and reports every request or response that violates the spec while the test suite runs | prism proxy api.yaml http://localhost:8000 --errors | yes | npm; Apache-2.0 | yes | agent compares producer and consumer code by reading | - |
| [openapi-typescript / openapi-generator + compiler](https://github.com/openapi-ts/openapi-typescript) | strong | Generate the consumer's types from the producer's contract and compile the consumer: every drifted field is a compiler error | npx openapi-typescript impl.json -o /tmp/api.d.ts ; tsc --noEmit with the generated types swapped in | yes | npm / jar; MIT / Apache-2.0 | yes | agent compares producer and consumer code by reading | - |
| [Buf (lint, breaking) + grpcurl](https://github.com/bufbuild/buf) | strong | Contract lint; live service description via reflection to compare with the repo's protos | buf lint --error-format=json ; grpcurl -plaintext localhost:50051 describe | yes | binaries; Apache-2.0 / MIT | yes | agent compares producer and consumer code by reading | - |
| [check-jsonschema / ajv](https://github.com/python-jsonschema/check-jsonschema) | strong | Validates fixtures, sample messages and settings files against the schema the other side publishes | check-jsonschema --schemafile s.json samples/*.json -o json | yes | pip; Apache-2.0 | yes | agent compares producer and consumer code by reading | - |
| [knip (unused exports) for in-repo contracts](https://github.com/webpro-nl/knip) | strong | Exports and types of a shared package that no other package imports; unlisted and unused dependencies | npx knip --reporter json | yes | npm; ISC | yes | agent compares producer and consumer code by reading | - |

Top 3 (each optional, with the fallback in its row): generate the producer's contract from code and diff it with oasdiff; Schemathesis against a local instance; Hurl probe files with Spectral rules.

All 24 rows, situational and skipped included: `tools.jsonl`, domain `contracts`.

### Backend / server (`server`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Framework route listers](https://github.com/django-extensions/django-extensions) | must-have | The framework's own list of every route with method, path, handler and middleware: the Part 2 inventory starts from data, and a route the agent missed cannot be missed | rails routes --expanded ; php artisan route:list --json ; bin/console debug:router --format=json ; manage.py show_urls --format json ; flask routes ; curl :8080/actuator/mappings ; FastAPI: app.openapi() | yes | built into the framework (django-extensions: pip, MIT) | yes | ast-grep rules for route declarations | runs code |
| [ast-grep entry-point rule pack](https://github.com/ast-grep/ast-grep) | must-have | Where no lister exists: every route declaration, cron registration, queue consumer, webhook handler, and email send call as file:line rows; plus string literals compared in conditionals (hardcoded tenant or customer names) | ast-grep scan --rule &lt;skill&gt;/rules/server/*.yml --json=stream . | yes | single binary; MIT | yes | agent inventories by reading the router and handlers | - |
| [Schemathesis](https://github.com/schemathesis/schemathesis) | must-have | For each route: validation cases, undocumented status codes, 5xx on malformed input, and the ignored_auth check (endpoint answers without or with invalid credentials) | st run spec.json --url http://localhost:8000 --checks all --seed 1 | yes with seed | pip/uvx; MIT | yes | agent inventories by reading the router and handlers | - |
| [Hurl access matrix](https://github.com/Orange-OpenSource/hurl) | must-have | One request file per route run once per identity (signed out, each role, other tenant): a route x role table of status codes from real responses | hurl --test --variables-file roles/viewer.env --report-json out/viewer routes.hurl (repeat per role) | yes | single binary; Apache-2.0 | yes | agent inventories by reading the router and handlers | - |
| [Settings-usage cross-check (ripgrep + jq/yq)](https://github.com/mikefarah/yq) | must-have | Every key of the settings schema or .env.example against where the code reads it: settings declared but never read, and reads of keys that are not declared | yq -o=json '.. \| select(tag != "!!map") \| path \| join(".")' settings.yaml ; rg -c -F '&lt;key&gt;' src/ for each | yes | single binaries; MIT | yes | agent inventories by reading the router and handlers | - |
| [Semgrep CE / Opengrep with framework rules](https://github.com/semgrep/semgrep) | strong | Handlers with no auth decorator or middleware, request data reaching sinks without validation (single-file taint in the free engine) | semgrep scan --config p/default --config rules/ --sarif -o out.sarif --metrics=off | yes for pinned rules (registry packs change; pin or vendor them) | pip; LGPL-2.1 engine; registry needs network on first fetch | yes with vendored rules | agent inventories by reading the router and handlers | online in some modes |
| [Per-test coverage contexts joined to handlers](https://github.com/nedbat/coveragepy) | strong | For every handler function: which tests execute it, and which branches (refusals, error paths) no test reaches | coverage json --show-contexts ; join function line ranges (from ctags JSON) to contexts | yes | pip; Apache-2.0 | yes | agent inventories by reading the router and handlers | - |
| [Static analyzers with framework knowledge (Brakeman, Larastan/PHPStan, Psalm, gosec, Bandit, django check)](https://github.com/presidentbeef/brakeman) | strong | Mass assignment, unscoped finds, unsafe redirects, raw SQL, unvalidated params, per framework idiom | brakeman -f json -o b.json ; phpstan analyse --error-format=json ; psalm --taint-analysis --output-format=json ; gosec -fmt sarif ./... ; bandit -r . -f json | yes | gem/composer/go/pip; Brakeman is under the non-commercial 'Brakeman Public Use License' (licence file confirmed), others MIT/Apache-2.0 | yes | agent inventories by reading the router and handlers | - |
| [Magic-value and hardcode linters](https://github.com/astral-sh/ruff) | strong | Literals in conditions and repeated string constants: candidates for Part 1 (hardcoded domain behavior) | ruff check --select PLR2004 --output-format json ; golangci-lint run --enable mnd,goconst | yes | already common; MIT/BSD | yes | agent inventories by reading the router and handlers | - |
| [actionlint + cron/job manifest readers](https://github.com/rhysd/actionlint) | strong | Scheduled-job inventory from the manifests that schedule them | actionlint -format '{{json .}}' ; yq '.on.schedule' .github/workflows/*.yml ; yq '.triggers.crons' wrangler.toml | yes | single binary; MIT | yes | agent inventories by reading the router and handlers | - |

Top 3 (each optional, with the fallback in its row): the framework's route lister (or the ast-grep entry-point pack); Schemathesis for validation and refusal cases; a Hurl access matrix, one run per identity.

All 16 rows, situational and skipped included: `tools.jsonl`, domain `server`.

### Database (`database`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [SQLite planner (EXPLAIN QUERY PLAN, .expert)](https://github.com/sqlite/sqlite) | must-have | The planner's verdict per query on a copy of the schema: SCAN vs SEARCH, which index, temp b-trees for ORDER BY; .expert proposes indexes | python3 -c sqlite3 (stdlib): conn.execute('EXPLAIN QUERY PLAN ' + sql) ; sqlite3 copy.db '.expert' '&lt;sql&gt;' ; D1: wrangler d1 execute DB --local --command 'EXPLAIN QUERY PLAN ...' | yes for a given schema, stats and SQLite version | Python standard library; public domain | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [PostgreSQL EXPLAIN + auto_explain](https://www.postgresql.org/docs/current/auto-explain.html) | must-have | Run the test suite against a throwaway Postgres with auto_explain logging every statement's plan: every distinct query with its plan, without finding queries by reading | postgres -c shared_preload_libraries=auto_explain -c auto_explain.log_min_duration=0 -c auto_explain.log_format=json -c auto_explain.log_nested_statements=on ; parse the log ; or psql -c 'EXPLAIN (FORMAT JSON) ...' | yes for given schema and statistics (plans change with row counts: load representative data or note it) | ships with PostgreSQL (contrib) | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [Ephemeral databases (PGlite, Docker, Testcontainers, embedded-postgres)](https://github.com/electric-sql/pglite) | must-have | A disposable copy of the schema to plan against, without touching the project's database; PGlite needs no Docker or server | PGlite from a short Node script (new PGlite(); db.exec(schema)) ; or docker run --rm -e POSTGRES_PASSWORD=x -p 5433:5432 postgres:17 ; psql -f schema.sql | yes | npm (Apache-2.0) or Docker | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [pg-index-health (and its SQL)](https://github.com/mfvanek/pg-index-health-sql) | must-have | Catalog checks: duplicate and intersecting indexes, foreign keys without an index, tables without primary key, nullable indexed columns, invalid indexes, bloat | psql -f sql/foreign_keys_without_index.sql (one file per check) ; CSV out | yes | SQL files; Apache-2.0 | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [sqlx prepare --check / Prisma validate / Drizzle check / Django makemigrations --check](https://github.com/launchbadge/sqlx) | must-have | Queries and models checked against the real schema; drift between models and migrations | cargo sqlx prepare --check ; prisma validate ; prisma migrate diff --from-migrations m --to-schema-datamodel s --exit-code ; drizzle-kit check ; manage.py makemigrations --check --dry-run ; alembic check | yes | already in the project | yes | agent reasons about indexes from the schema; marked "planner not run" | runs code |
| [Squawk](https://github.com/sbdchd/squawk) | must-have | Migration safety lint (see compatibility); newer versions also ship a language server | squawk --reporter json migrations/*.sql | yes | binary; Apache-2.0/MIT | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [tbls](https://github.com/k1LoW/tbls) | must-have | Schema document as JSON/Markdown with ER diagrams, lint (missing comments, unrelated tables, duplicate relations) and diff against a committed document | tbls out -t json -o schema.json ; tbls lint ; tbls diff | yes | single binary; MIT | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [pg_query / pglast / libpg_query](https://github.com/pganalyze/libpg_query) | strong | Parses with the real Postgres parser; normalizes and fingerprints statements so identical queries with different literals collapse to one row; lists tables and columns touched | python: pglast.parser.fingerprint(sql) ; pglast.parse_sql(sql) | yes | pip wheel; BSD-3 | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [sqlglot](https://github.com/tobymao/sqlglot) | strong | Dialect-aware parse of SQL strings found in code: tables, columns, missing WHERE or LIMIT, SELECT *; column lineage | python: sqlglot.parse_one(sql, read='sqlite').find_all(exp.Table) | yes | pip, pure Python, no dependencies; MIT | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [HypoPG](https://github.com/HypoPG/hypopg) | strong | Hypothetical indexes: EXPLAIN shows whether a proposed index would be used, without building it | SELECT hypopg_create_index('CREATE INDEX ON t (a,b)'); EXPLAIN (FORMAT JSON) ... | yes | extension; PostgreSQL licence | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [Dexter](https://github.com/ankane/dexter) | strong | Suggests indexes from a query log or pg_stat_statements using HypoPG | dexter -d db --pg-stat-statements ; dexter -d db queries.sql | yes | gem (needs HypoPG); MIT | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) | strong | MCP tools for explain plans incl. hypothetical indexes, workload index tuning, and health checks (unused/duplicate indexes, vacuum, sequences); restricted read-only mode | MCP: uvx postgres-mcp --access-mode=restricted with DATABASE_URI | yes for a given DB state | pip/Docker; MIT | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [SQLFluff / sqruff](https://github.com/sqlfluff/sqlfluff) | strong | Lint rules for SQL files: ambiguous joins, SELECT *, implicit column lists, aliasing, references | sqlfluff lint --dialect postgres --format json . ; sqruff lint --format json (flag not verified) | yes | pip / binary; MIT / Apache-2.0 | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [Atlas schema inspect / diff](https://github.com/ariga/atlas) | strong | Schema as JSON/HCL/Mermaid ERD; diff between migrations directory and a database | atlas schema inspect -u 'sqlite://file.db' --format '{{ json . }}' ; --format '{{ mermaid . }}' | yes | binary; licence caveat as in compatibility | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [N+1 detectors in tests](https://github.com/charkost/prosopite) | strong | Raises or logs when a request or test issues the same query per item | enable in the test environment, run the suite, collect raised errors or log lines | yes | gem/pip/composer; MIT/Apache-2.0 | yes | agent reasons about indexes from the schema; marked "planner not run" | runs code |
| [Query count per request (statement log + fingerprint)](https://github.com/darold/pgbadger) | strong | Counts queries per test or request and groups by fingerprint: round trips per action, unbounded result sets (rows returned) | log_statement=all + log_line_prefix with application_name ; pgbadger -x json -o q.json postgresql.log ; pt-query-digest --output json slow.log | yes | Perl scripts; PostgreSQL licence / GPL-2.0 | yes | agent reasons about indexes from the schema; marked "planner not run" | - |
| [active_record_doctor / database_consistency](https://github.com/gregnavis/active_record_doctor) | strong | Unindexed foreign keys, missing unique indexes behind uniqueness validations, missing FK constraints, incorrect dependent options, mismatched column types | bundle exec rake active_record_doctor | yes | gem; MIT | yes | agent reasons about indexes from the schema; marked "planner not run" | runs code |

Top 3 (each optional, with the fallback in its row): the planner on a copy of the schema (SQLite EXPLAIN QUERY PLAN from the standard library, PostgreSQL auto_explain); tbls and pg-index-health; Squawk with the ORM's own drift check.

All 31 rows, situational and skipped included: `tools.jsonl`, domain `database`.

### Backend-to-client API (`client-api`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Client call-site rule pack (ast-grep)](https://github.com/ast-grep/ast-grep) | must-have | Every place a screen calls the backend (fetch, axios, generated client methods, google.script.run.*, RPC stubs) with the endpoint or function name as a captured variable | ast-grep scan --rule &lt;skill&gt;/rules/client-api/*.yml --json=stream web/ ; join captured names to the server inventory | yes | single binary; MIT | yes | agent builds both inventories by reading | - |
| [Server inventory (route listers / generated OpenAPI)](https://spec.openapis.org/oas/latest.html) | must-have | The other side of the set difference | see server: framework route listers | yes | - | yes | agent builds both inventories by reading | - |
| [knip](https://github.com/webpro-nl/knip) | must-have | Unused files, exports and dependencies: API client functions nothing imports, server exports nothing calls (in a shared repo) | npx knip --reporter json | yes | npm; ISC | yes | agent builds both inventories by reading | - |
| [Spectral / vacuum with a consistency ruleset](https://github.com/stoplightio/spectral) | must-have | Coherence rules across the API: one error shape, consistent pagination and naming, ids not display names (pattern rules), duplicate operations | spectral lint impl.json -r &lt;skill&gt;/rules/api-coherence.yaml -f json | yes | npm / binary; Apache-2.0 / MIT | yes | agent builds both inventories by reading | - |
| [GraphQL Inspector coverage / similar](https://github.com/graphql-hive/graphql-inspector) | must-have | Fields no client query uses; near-duplicate types (duplicated capabilities) | graphql-inspector coverage ... ; graphql-inspector similar schema.graphql | yes | npm; MIT | yes | agent builds both inventories by reading | - |
| [TypeScript compiler over a generated client](https://github.com/hey-api/openapi-ts) | strong | Compiling the client against types generated from the server's real contract flags every call to something that does not exist or has a different shape | npx @hey-api/openapi-ts -i impl.json -o /tmp/client ; tsc --noEmit | yes | npm; MIT | yes | agent builds both inventories by reading | - |
| [Playwright (network capture)](https://github.com/microsoft/playwright) | strong | Drive each screen and record the network: which endpoints each screen calls, payload sizes, data returned and never rendered | npx playwright test --reporter=json with recordHar: { path: 'out.har' } ; parse HAR entries per test | yes for a scripted path with seeded data | npm + browsers (~300 MB); Apache-2.0 | yes | agent builds both inventories by reading | runs code |
| [Language server references](https://github.com/oraios/serena) | strong | For each API function: its callers on the client side (zero callers = no screen uses it) | LSP findReferences via harness tool or Serena find_referencing_symbols | yes | see decision-placement | yes | agent builds both inventories by reading | - |

Top 3 (each optional, with the fallback in its row): two inventories and a set difference (client call sites against the server's routes); knip; Spectral or vacuum with a coherence ruleset.

All 13 rows, situational and skipped included: `tools.jsonl`, domain `client-api`.

### Abstraction and shared code (`abstraction`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [jscpd](https://github.com/kucherenko/jscpd) | must-have | Clone pairs with file, lines and token count; duplication percentage per format | npx jscpd --reporters json --output out/ --min-tokens 50 . | yes | npm; MIT | yes | agent samples; counts marked "estimated" | - |
| [scc](https://github.com/boyter/scc) | must-have | Lines, cyclomatic and cognitive complexity per file, ULOC/DRYness, and (v4) git-based hotspots, change coupling and authors; also an MCP mode | scc --by-file --cognitive --format json . ; scc --hotspots ; scc --coupling ; scc --mcp | yes | single Go binary; MIT | yes | agent samples; counts marked "estimated" | - |
| [Lizard](https://github.com/terryyin/lizard) | must-have | Cyclomatic complexity, length, parameter count and nesting per function, without a build | lizard --csv . &gt; lizard.csv ; lizard --xml ; lizard -Eduplicate | yes | pip, pure Python; MIT | yes | agent samples; counts marked "estimated" | - |
| [knip](https://github.com/webpro-nl/knip) | must-have | Unused files, exports, types, enum members, dependencies | npx knip --reporter json | yes | npm; ISC | yes | agent samples; counts marked "estimated" | - |
| [dependency-cruiser](https://github.com/sverweij/dependency-cruiser) | must-have | Module graph JSON, cycles, orphans, rule violations, Mermaid/dot output, per-folder instability metrics | npx depcruise src --no-config --output-type json ; --output-type metrics | yes | npm; MIT | yes | agent samples; counts marked "estimated" | - |
| [ast-grep type-shape dump](https://github.com/ast-grep/ast-grep) | must-have | Every type declaration with its field names as JSON; a small script groups types whose field sets are equal or near-equal across packages: duplicated types found without reading | ast-grep run -p 'interface $N { $$$F }' --json=stream ; group by normalized field set in ledger.py | yes | single binary; MIT | yes | agent samples; counts marked "estimated" | - |
| [git co-change of a past addition](https://git-scm.com/docs/git-show) | must-have | The files touched by the commit(s) that last added an entity type, rule or setting: the real historical answer to 'list every file that must change' | git log --oneline -S'&lt;name of last added member&gt;' ; git show --stat --format= &lt;sha&gt; | yes | git | yes | agent samples; counts marked "estimated" | - |
| [SARIF tooling (sarif-tools)](https://github.com/microsoft/sarif-tools) | must-have | Summaries, CSV export, and diff between two SARIF runs (new vs fixed results since the last review); blame to attach authors | sarif summary out/*.sarif ; sarif csv ; sarif diff old/ new/ -o diff.json | yes | pip; MIT | yes | agent samples; counts marked "estimated" | - |
| [PMD CPD](https://github.com/pmd/pmd) | strong | Clones ignoring identifiers/literals | pmd cpd --minimum-tokens 60 --ignore-identifiers --format xml --dir . | yes | Java zip; BSD-style | yes | agent samples; counts marked "estimated" | - |
| [Per-language complexity linters](https://github.com/SonarSource/eslint-plugin-sonarjs) | strong | Oversized and complex units with the ecosystem's own thresholds; smells (reek, detekt, PMD design rules) | each has a JSON or SARIF formatter, e.g. eslint -f json ; detekt --report sarif:out.sarif ; pmd check -f sarif ; rubocop -f json ; shellcheck -f json | yes | per ecosystem | yes | agent samples; counts marked "estimated" | - |
| [vulture / deadcode](https://github.com/jendrikseipp/vulture) | strong | Unused functions, classes, variables with a confidence value | vulture . --min-confidence 80 | yes | pip; MIT | yes | agent samples; counts marked "estimated" | - |
| [Go deadcode / staticcheck U1000](https://github.com/golang/tools) | strong | Functions unreachable from main (whole-program) and unused identifiers | deadcode -json ./... ; staticcheck -checks U1000 -f json ./... | yes | go install golang.org/x/tools/cmd/deadcode@latest; BSD-3 | yes | agent samples; counts marked "estimated" | - |
| [madge / skott](https://github.com/pahen/madge) | strong | Circular dependencies and module graph | npx madge --circular --json --extensions ts,tsx src/ ; skott --showCircularDependencies --displayMode=raw | yes | npm; MIT | yes | agent samples; counts marked "estimated" | - |
| [pydeps / import-linter / Tach](https://github.com/thebjorn/pydeps) | strong | Import graph and cycles; layer contracts | pydeps pkg --show-cycles --no-output ; pydeps pkg --show-deps (JSON) ; lint-imports | yes | pip; BSD-2 | yes | agent samples; counts marked "estimated" | - |
| [Universal Ctags symbol dump](https://github.com/universal-ctags/ctags) | strong | All identifiers with kind and scope: input for the naming glossary (same concept under several names, one name for several concepts) and for module size counts | ctags -R --output-format=json --fields=+nKS -f - . | yes | distro package; GPL-2.0 | yes | agent samples; counts marked "estimated" | - |
| [Language servers (Serena / harness LSP)](https://github.com/oraios/serena) | strong | References to an enum, union or registry: the files that must change to add a new member (extensibility walk-through) | findReferences on the type; Serena find_referencing_symbols | yes | see decision-placement | yes | agent samples; counts marked "estimated" | - |
| [Compiler exhaustiveness probe](https://www.typescriptlang.org/tsconfig/#noEmit) | strong | In a scratch copy add one member to the entity/rule/notification enum and compile: each error is a file that must change; files that should change but raise no error are the dangerous ones | git worktree add /tmp/probe ; apply one-line change ; tsc --noEmit --pretty false \| cargo check --message-format=json | yes | nothing to install | yes | agent samples; counts marked "estimated" | - |
| [Baseline linters and formatters (ruff, Biome, ESLint, oxlint, clippy, golangci-lint)](https://github.com/biomejs/biome) | strong | The standing list of local smells: unused variables and imports, shadowing, unreachable code | ruff check --output-format sarif ; biome lint --reporter=json ; cargo clippy --message-format=json ; oxlint -f json | yes | single binaries; MIT | yes | agent samples; counts marked "estimated" | - |

Top 3 (each optional, with the fallback in its row): jscpd; scc (or Lizard) for size and complexity; the ecosystem's dead-code and cycle finder.

All 33 rows, situational and skipped included: `tools.jsonl`, domain `abstraction`.

### Hotspots from history (`hotspots`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [git log --numstat (computed in ledger.py)](https://git-scm.com/docs/git-log) | must-have | Churn per file, bug-fix and revert commits by message, co-change pairs, authors per file, last-change age; with a fixed --until commit the numbers are identical on every run | git log --numstat --no-renames --format='--%H%x09%aI%x09%aN%x09%s' &lt;sha&gt; ; parse in Python (standard library) | yes (pin the end commit and the window) | git only | yes | none needed | - |
| [scc --hotspots / --coupling](https://github.com/boyter/scc) | must-have | Files ranked by complexity x change frequency, change-coupling pairs (optionally complexity-weighted), blast radius for one file (--coupling-for), author roll-up with bus factor; honors .mailmap | scc --hotspots --depth 0 ; scc --coupling ; scc --coupling-for path ; scc --mcp (flags confirmed in README; whether --format json applies to these reports is not verified) | yes for a fixed commit and --depth | single Go binary; MIT; v4.1.0 tagged | yes | ledger's own git log + lizard join | - |
| [Coverage x churn join](https://github.com/Bachmann1234/diff_cover) | must-have | Areas with many changes and few tests: churn rank joined with line coverage per file | ledger joins cov.json/lcov with the churn table ; diff-cover for the last N commits | yes | none beyond the coverage tool | yes | plain git log / git shortlog read by the agent | - |
| [Lizard (complexity side of the join)](https://github.com/terryyin/lizard) | strong | Per-file complexity to multiply with churn | lizard --csv . | yes | pip; MIT | yes | plain git log / git shortlog read by the agent | - |
| [code-maat](https://github.com/adamtornhill/code-maat) | strong | The reference analyses: revisions, coupling, sum of coupling, authors, main-dev, entity-ownership, entity-effort, fragmentation, communication, age, churn; CSV | git log --all --numstat --date=short --pretty=format:'--%h--%ad--%aN' --no-renames &gt; log ; java -jar code-maat-1.0.4-standalone.jar -l log -c git2 -a coupling | yes | jar (Java 8+); GPL-3.0; README says it is in maintenance, last commit July 2025 | yes | plain git log / git shortlog read by the agent | - |
| [CodeLore](https://github.com/emrecdr/codelore) | strong | code-maat-compatible analyses plus hotspots with cognitive complexity, Fisher-significant coupling, knowledge islands (departed authors), ownership, modularity violations (imports vs co-change), a provenance file per run, and a local MCP server | codelore analyze --analysis hotspots --repo . --min-revs 5 --rows 50 (CSV on stdout) ; codelore mcp | yes (provenance sidecar records every option) | Rust binary (crates.io 0.30.0, Sept 2026); GPL-3.0 | yes | plain git log / git shortlog read by the agent | - |
| [PyDriller](https://github.com/ishepard/pydriller) | strong | Commit traversal with per-method changes and complexity (via lizard), process metrics (code churn, contributors count, hunks), and get_commits_last_modified_lines for SZZ (which commit introduced the lines a fix changed) | python script: Repository(path, to_commit=sha).traverse_commits() ; pydriller.metrics.process.* | yes | pip (GitPython, lizard); Apache-2.0 | yes | plain git log / git shortlog read by the agent | - |
| [git blame ownership (git-fame, git-who)](https://github.com/casperdcl/git-fame) | strong | Surviving lines per author per file or directory: single-author code and knowledge risk | git fame --format json --bytype ; git who table -n 0 -- path ; git who tree | yes at a fixed commit | pip (MPL-2.0) / Go binary (MIT) | yes | plain git log / git shortlog read by the agent | - |
| [Issue and PR history (gh CLI / GitHub MCP)](https://github.com/cli/cli) | strong | Bug-labelled issues linked to merged PRs and their files: a defect map that does not depend on commit-message wording | gh pr list --state merged --search 'label:bug' --json number,files,mergedAt -L 500 | yes for a fixed window | one binary; MIT | no | plain git log / git shortlog read by the agent | online |

Top 3 (each optional, with the fallback in its row): git log --numstat parsed inside ledger.py; scc --hotspots and --coupling; PyDriller and gh for a better defect signal than commit subjects.

All 20 rows, situational and skipped included: `tools.jsonl`, domain `hotspots`.

## User-facing and documentation

### Front end / UI (`ui`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Playwright (library and test runner)](https://playwright.dev/) | must-have | The rendered page: screenshots at fixed viewports, the accessibility tree, computed styles, overflow, tab order, console errors. The access audit becomes a script: one saved login per role, visit every route, record status and visible text. | npx playwright test --reporter=json or a probe script kept under .code-review/; page.screenshot, locator.ariaSnapshot(), expect(page).toHaveScreenshot(), storageState per role. | Yes with fixed viewport, animations off, seeded data, clock frozen. | Node (or pip install playwright), about 300 MB of browsers. Apache-2.0. | Yes | curl plus HTML parsing for status and text only; mark visual checks not-run. | runs code |
| [axe-core (@axe-core/cli, @axe-core/playwright)](https://github.com/dequelabs/axe-core) | must-have | WCAG violations on the rendered DOM: contrast with real computed colors, missing names, ARIA misuse, each with selector and rule id. | npx @axe-core/cli &lt;url&gt; --save axe.json --tags wcag2a,wcag2aa,wcag22aa or new AxeBuilder({page}).analyze() inside the Playwright probe (covers signed-in pages). | Yes for the same DOM. | Node, Chrome. MPL-2.0. | Yes | eslint a11y plugins and html-validate on source. | - |
| [Lighthouse](https://github.com/GoogleChrome/lighthouse) | must-have | Scores and audits for performance, accessibility, best practices, SEO; layout shift, tap-target size, image sizing. | npx lighthouse &lt;url&gt; --output=json --output-path=lh.json --chrome-flags=--headless --form-factor=mobile. | Audits yes; performance numbers vary, so run 3 and take the median. | Node, Chrome. Apache-2.0. | Yes | axe for a11y; no fallback for performance. | - |
| [Project Wallace (@projectwallace/css-analyzer, wallace-cli)](https://www.projectwallace.com/) | must-have | Counts of what the CSS contains: unique colors, font sizes, font families, spacing values, z-indexes, specificity, !important. 47 grays and 19 font sizes is a finding no reader counts by hand. | npx wallace-cli dist/app.css --json &gt; wallace.json; @projectwallace/css-code-quality for scores. | Yes | Node. MIT. | Yes | Ripgrep hex colors and font-size declarations and count unique. | - |
| [Figma MCP server (official)](https://developers.figma.com/docs/figma-mcp-server/) | strong | The intended design: frame screenshot, variables and styles, component names, Code Connect map. Lets the review compare what was built with what was designed. | MCP figma, remote https://mcp.figma.com/mcp (OAuth) or desktop app server http://127.0.0.1:3845/mcp (both URLs not verified today). Read tools verified in the docs: get_design_context, get_variable_defs, get_screenshot, get_metadata, search_design_system, get_libraries, get_code_connect_map. Save get_variable_defs and get_screenshot output as artifacts. | Reads are stable for one file version; which frames to read is the agent's choice unless the brief lists node ids. | No install for remote. Figma account; desktop server needs a Dev or Full seat on a paid plan (secondary source). Rate limits by seat, numbers not verified. | No | Figma REST API with a token; else ask the owner for exported frames (PNG) and a tokens file. | online |
| [Figma REST API](https://developers.figma.com/docs/rest-api/) | strong | Same design data without an LLM in the loop: file JSON, rendered frames, styles, comments. | curl -H 'X-Figma-Token: $FIGMA_TOKEN' https://api.figma.com/v1/files/&lt;key&gt; (JSON); /v1/images/&lt;key&gt;?ids=&lt;node&gt;&format=png. Variables endpoint is Enterprise-only (not verified). Types: @figma/rest-api-spec. | Yes for a file version. | curl only. Personal access token. Free within plan limits. | No | Exported PNGs and tokens JSON committed to the repo. | online |
| [Storybook MCP (@storybook/addon-mcp)](https://storybook.js.org/docs/ai/mcp/overview) | strong | The component inventory as the project defines it: every component, its props and stories. Shows which screens bypass the shared components. | npx storybook add @storybook/addon-mcp; server at http://localhost:6006/mcp while Storybook runs. Tools: docs-list, docs-show, docs-show-story, stories-find-by-component, test-run. Docs toolset needs features.componentsManifest. | Lists yes; marked experimental, API may change. | Node, Storybook 10. MIT. | Yes | Read storybook-static/index.json after storybook build; or glob *.stories.*. | - |
| [Storybook test runner / Vitest addon with a11y addon](https://storybook.js.org/docs/writing-tests/accessibility-testing) | strong | axe results and render errors for every component state, not only the states a page happens to show. | npx test-storybook --json --outputFile sb.json (@storybook/test-runner) or npx vitest --project=storybook --reporter=json (@storybook/addon-vitest); @storybook/addon-a11y adds axe. | Yes | Node, Playwright browser. MIT. | Yes | axe on the app's pages. | runs code |
| [Style Dictionary](https://styledictionary.com/) | strong | Resolves design tokens to one flat list of allowed values (colors, spacing, type). That list is what stylelint and the CSS analysis are checked against. | npx style-dictionary build --config sd.config.json with a json/flat format; read the output JSON. | Yes | Node. Apache-2.0. | Yes | Read CSS custom properties from :root with the Wallace analyzer. | - |
| [Playwright CLI (@playwright/cli)](https://playwright.dev/agent-cli/introduction) | strong | Lets the agent drive a browser by shell command and get the accessibility snapshot as a YAML file. | npm i -g @playwright/cli; playwright-cli open &lt;url&gt;, snapshot, screenshot, click e21, -s=&lt;session&gt;. | Commands yes; what the agent clicks is its choice. | Node, browser. Apache-2.0. | Yes | Playwright MCP. | - |
| [Playwright MCP (@playwright/mcp)](https://github.com/microsoft/playwright-mcp) | strong | Same as the CLI, as MCP tools. | MCP local: npx -y @playwright/mcp@latest --headless --isolated (flags not verified today). | Tools yes; exploration no. | Node, browser. Apache-2.0. | Yes | Playwright CLI. | - |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | strong | Performance trace with Core Web Vitals, network and console capture, CPU and network throttling. | MCP local: npx -y chrome-devtools-mcp@latest. Tool names not verified (the repo page could not be fetched in this session). | Traces vary run to run; take the median of 3. | Node, Chrome. Apache-2.0. | Yes | Lighthouse CLI. | - |
| [shot-scraper](https://shot-scraper.datasette.io/) | strong | Screenshots of many pages and selectors from one YAML file; can also run JavaScript on a page and print the JSON result. | shot-scraper multi shots.yml; shot-scraper javascript &lt;url&gt; &lt;js&gt;; shot-scraper accessibility &lt;url&gt;. | Yes | pip install shot-scraper plus Playwright browser. Apache-2.0. | Yes | A Playwright script. | - |
| [odiff / pixelmatch](https://github.com/dmtrKovalenko/odiff) | strong | A number for how much two screenshots differ, and a diff image. Use against the last review's screenshots or a Figma frame export. | npx odiff-bin a.png b.png diff.png --parsable-stdout (prints pixel count and percent; flag not verified) or pixelmatch in a script. | Yes | odiff-bin 4.5.0 MIT (native binary); pixelmatch 8.0.0 ISC (pure JS). | Yes | Playwright toHaveScreenshot. | - |
| [reg-cli / reg-suit](https://github.com/reg-viz/reg-suit) | strong | Compares two folders of screenshots and writes a JSON and HTML report of new, removed and changed images. | npx reg-cli actual/ expected/ diff/ -J reg.json -R report.html. | Yes | Node. MIT. | Yes | Loop odiff over file pairs. | - |
| [Pa11y / pa11y-ci](https://pa11y.org/) | strong | Runs HTML_CodeSniffer and axe over a URL list or sitemap, with scripted actions before testing. | npx pa11y-ci --sitemap &lt;url&gt; --json &gt; pa11y.json; .pa11yci lists URLs and actions. | Yes | Node, Puppeteer. LGPL-3.0. | Yes | axe CLI over a URL list. | - |
| [Lighthouse CI (@lhci/cli)](https://github.com/GoogleChrome/lighthouse-ci) | strong | Runs Lighthouse N times per URL, takes the median and checks it against budgets. | npx lhci collect --url=&lt;u&gt; --numberOfRuns=3 then lhci assert --preset=lighthouse:recommended; results in .lighthouseci/*.json. | Median of N: close to. | Node, Chrome. Apache-2.0. | Yes | Loop Lighthouse three times. | - |
| [IBM Equal Access (accessibility-checker)](https://github.com/IBMa/equal-access) | strong | A different rule engine from axe; reports 'needs review' items separately from violations. | npx achecker &lt;url-or-file&gt;; JSON per page in results/. | Yes | Node, Puppeteer. Apache-2.0. | Yes | axe only. | - |
| [Static a11y lint (eslint-plugin-jsx-a11y and siblings)](https://github.com/jsx-eslint/eslint-plugin-jsx-a11y) | strong | Accessibility faults in source, including screens the review cannot run. | npx eslint --no-config-lookup -c &lt;skill a11y config&gt; -f json src; siblings: eslint-plugin-vuejs-accessibility, @angular-eslint/eslint-plugin-template, eslint-plugin-lit-a11y, eslint-plugin-react-native-a11y, svelte-check. | Yes | Node. MIT. | Yes | Reviewer reads for the same rules. | - |
| [html-validate](https://html-validate.org/) | strong | Invalid HTML and a11y rules on templates and static files, offline. | npx html-validate --formatter json 'templates/**/*.html'. | Yes | Node. MIT. | Yes | Nu validator (not verified). | - |
| [Stylelint with value-restricting plugins](https://stylelint.io/) | strong | Every declaration that uses a raw value where a token is required, with file and line. | npx stylelint '**/*.{css,scss}' -f json --config &lt;skill config&gt; using stylelint-declaration-strict-value, stylelint-scales, stylelint-value-no-unknown-custom-properties, stylelint-use-logical. | Yes | Node. MIT. | Yes | Wallace counts. | - |
| [Impeccable (npx impeccable detect)](https://impeccable.style/) | strong | A rule-based detector for the generic AI look: gradient text, purple gradients, default fonts, nested cards, everything centered, uniform spacing, bounce easing, tiny text, long lines. | npx impeccable detect --json src/ (or a URL); exit 2 when patterns are found. | Yes (no LLM). | Node; jsdom included, Puppeteer only for URLs. Apache-2.0. | Yes | Reviewer applies the same list by eye. | - |
| [Knip](https://knip.dev/) | strong | Unused components, files and exports: screens and widgets nothing links to. | npx knip --reporter json. | Yes | Node. ISC. | Yes | Ripgrep for imports of each component. | - |
| [Vision review of screenshots (the agent's own model)](https://docs.claude.com/en/docs/build-with-claude/vision) | strong | A judgement of hierarchy, density, alignment and overall look from the real pixels. | The agent reads the PNGs the Playwright probe saved, with a fixed rubric and the reference screenshots beside them. Scores are recorded as estimated metrics. | No. Fix the screenshot set, rubric and order; record model and prompt. | Nothing extra. Needs a vision model in the harness. | No | Skip and say so. | online |
| [Maestro (CLI and maestro mcp)](https://docs.maestro.dev/) | strong | Drives iOS simulators and Android emulators from YAML flows; returns the view hierarchy and screenshots. | maestro test flows/ --format junit --output out.xml; MCP local maestro mcp with tools list_devices, inspect_screen, take_screenshot, run. | Flows yes. | Java, a simulator or emulator. Apache-2.0 CLI; cloud is paid. | Yes | xcrun simctl io booted screenshot, adb exec-out screencap -p. | - |

Top 3 (each optional, with the fallback in its row): Playwright, axe-core, Project Wallace (Lighthouse CI and Impeccable next).

All 59 rows, situational and skipped included: `tools.jsonl`, domain `ui`.

### Task walkthroughs (`walkthroughs`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Playwright Test with trace, video and HAR](https://playwright.dev/docs/trace-viewer) | must-have | Each task run for real, as each kind of user: every page, request, response status, console error and screenshot per step. Dead ends and broken hand-offs show up as failures, not opinions. | One spec per task under .code-review/R-0001/walkthroughs/; npx playwright test --reporter=json --trace=on; JSON report plus trace.zip, video and HAR per task. npx playwright codegen &lt;url&gt; records a first draft. | Yes with seeded data and a frozen clock. | Node, browsers. Apache-2.0. | Yes | Trace through code only, as now; mark the task untested. | runs code |
| [Route and page inventory from the framework](https://nextjs.org/docs/app/api-reference/cli/next) | strong | The full list of pages and endpoints, so 'the 10 to 20 most important tasks' is chosen from everything and nothing is missed. | php artisan route:list --json, rails routes, flask routes, python manage.py show_urls (django-extensions), Next.js build output, or the OpenAPI file. Not verified today; each is the framework's own command. | Yes | Whatever the project runs on. | Yes | Ripgrep for route declarations. | runs code |
| [Playwright CLI sessions](https://playwright.dev/agent-cli/introduction) | strong | The agent walks a task step by step before writing the spec, saving a snapshot at each step. | playwright-cli -s=task3 open &lt;url&gt;, snapshot, click, tracing-start. | Exploration no; the saved spec yes. | Node. Apache-2.0. | Yes | Playwright MCP. | - |
| [Mailpit in the loop](https://mailpit.axllent.org/) | strong | The emails a task sends, caught and read by the walkthrough: invite link, approval notice, reset code. | Point the app's SMTP at localhost:1025; the spec reads GET http://localhost:8025/api/v1/messages and follows the link. | Yes | Single Go binary or Docker. MIT. | Yes | Read the app's mail log or file backend. | - |
| [Maestro flows](https://docs.maestro.dev/) | strong | The same for mobile apps: a YAML flow per task. | maestro test flows/ --format junit. | Yes | Java, simulator. Apache-2.0. | Yes | Code trace. | - |
| [axe at every step](https://github.com/dequelabs/axe-core-npm) | strong | Accessibility of states only reached mid-task: dialogs, validation errors, confirmations. | new AxeBuilder({page}).analyze() after each step in the spec. | Yes | With Playwright. | Yes | Page-level axe. | - |
| [Doc Detective](https://doc-detective.com/) | strong | Runs the steps written in the help pages against the app, so the documented way to do a task is itself tested. | npx doc-detective runTests --input docs/ --output dd.json. | Yes | Node 18+, browser. AGPL-3.0. | Yes | Hand-map doc steps to walkthrough specs. | - |

Top 3 (each optional, with the fallback in its row): Playwright Test with traces, the framework's route list, Mailpit in the loop.

All 12 rows, situational and skipped included: `tools.jsonl`, domain `walkthroughs`.

### Email and notifications (`notifications`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Mailpit](https://mailpit.axllent.org/) | must-have | Every message the system sends in a test run: recipients, headers, both bodies, attachments. Built-in checks for client support of the HTML, dead links, and spam score. | mailpit (SMTP 1025, UI and API 8025). GET /api/v1/messages, /api/v1/message/{id}, /api/v1/message/{id}/html-check, /link-check, /sa-check (paths from memory; the feature list is verified). JSON. | Yes | One Go binary, Homebrew or Docker. MIT. | Yes (link check needs network) | App's file or console mail backend; parse .eml with Python email. | - |
| [Python email / mailparser](https://docs.python.org/3/library/email.html) | must-have | Structural facts per message: multipart/alternative with a text part, List-Unsubscribe and List-Unsubscribe-Post, Reply-To, Message-ID, charset, size against Gmail's 102 KB clip. | Standard library inside ledger.py; Node mailparser 3.14.3 as an alternative. | Yes | None. | Yes | n/a | - |
| [caniemail data (caniemail, @jsx-email/doiuse-email)](https://www.caniemail.com/) | strong | Which HTML and CSS in each template fails in which mail client (flexbox in Outlook, and so on). | doiuse-email API or CLI on rendered HTML with a client list; JSON. Mailpit's HTML check uses the same data. | Yes for a data version. | Node. MIT. | Yes | Mailpit HTML check. | - |
| [Playwright screenshots of emails](https://playwright.dev/) | strong | How each email looks at 375 px and 600 px, light and dark, images blocked. | Load the captured HTML with page.setContent; screenshot at each width; colorScheme: 'dark'; block image requests. | Yes | With Playwright. | Yes | Mailpit's own preview by hand. | - |
| [axe-core on email HTML](https://github.com/dequelabs/axe-core) | strong | Contrast, alt text, lang, layout tables without role=presentation. | Run axe in the same Playwright page with rules color-contrast,image-alt,html-has-lang,link-name. | Yes | With Playwright. | Yes | html-validate. | - |
| [checkdmarc](https://github.com/domainaware/checkdmarc) | strong | The live SPF, DMARC, MX and related DNS records of the sending domain, parsed and validated. | checkdmarc example.com prints JSON; -o out.json. | Yes for the DNS at that moment. | pip install checkdmarc. Apache-2.0. | No | dig TXT by hand; else 'not verified'. | online |
| [icalendar / node-ical](https://icalendar.readthedocs.io/) | strong | Parses calendar feeds and invites: valid RFC 5545, time zones present, stable UIDs, SEQUENCE raised on change. | Python icalendar.Calendar.from_ical(data); Node node-ical. | Yes | pip install icalendar. BSD-2-Clause. | Yes | Hand read the .ics. | - |

Top 3 (each optional, with the fallback in its row): Mailpit, the Python email parser, caniemail data (checkdmarc when the sending domain is known).

All 16 rows, situational and skipped included: `tools.jsonl`, domain `notifications`.

### Localization (`localization`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [eslint-plugin-i18next no-literal-string](https://github.com/edvardchen/eslint-plugin-i18next) | must-have | Every hardcoded user-facing string in JSX and TS, with file and line. Works on projects with no i18n library at all. | npx eslint --no-config-lookup -c &lt;skill i18n config&gt; -f json src with i18next/no-literal-string in jsx-only or all mode. | Yes | Node. ISC. | Yes | Ripgrep for text nodes; reviewer reads. | - |
| [Pseudo-localization with screenshots](https://github.com/tryggvigy/pseudo-localization) | must-have | Shows two faults at once on real screens: text that stays plain was hardcoded; text that is cut off will not fit German or Finnish. | Generate a pseudo locale (pseudo-localization, or 40% longer accented strings from ledger.py), run the Playwright probe in that locale, detect clipped elements (scrollWidth &gt; clientWidth) and un-accented text. | Yes | Node. MIT. | Yes | Length-ratio estimate from catalog strings. | - |
| [Vue, Angular, FormatJS, Lingui lint rules](https://formatjs.github.io/docs/tooling/linter/) | strong | The same for other stacks, plus message-quality rules. | @intlify/eslint-plugin-vue-i18n (no-raw-text, no-missing-keys, no-unused-keys); eslint-plugin-formatjs (no-literal-string-in-jsx, enforce-plural-rules, no-offset); eslint-plugin-lingui (no-unlocalized-strings); Angular @angular-eslint/template/i18n. | Yes | Node. MIT. | Yes | Reviewer reads. | - |
| [i18next-cli](https://github.com/i18next/i18next-cli) | strong | Extracts keys from code and compares with the catalogs: missing, unused, per-locale completion. | npx i18next-cli extract --ci, status, lint (subcommands not verified). | Yes | Node. MIT. | Yes | i18n-unused; script the key diff. | - |
| [FormatJS CLI / Lingui CLI](https://formatjs.github.io/docs/tooling/cli/) | strong | Extract and verify ICU messages. | npx formatjs extract 'src/**/*.tsx' --out-file en.json, formatjs verify (not verified); npx lingui extract --clean, lingui compile --strict. | Yes | Node. MIT. | Yes | messageformat-validator. | - |
| [@lingual/i18n-check](https://github.com/lingualdev/i18n-check) | strong | Across locale files: missing keys, unused keys, and translations whose ICU or i18next placeholders differ from the source. | npx i18n-check --locales locales --source en --format icu (or i18next); --reporter for output. | Yes | Node. MIT. | Yes | Key-set diff in ledger.py. | - |
| [messageformat-validator](https://github.com/bearfriend/messageformat-validator) | strong | ICU MessageFormat validity and source-to-target structure: plural categories each language needs, argument mismatches. | npx mfv -s en -l fr,de path/ (invocation not verified). | Yes | Node. MIT. | Yes | @formatjs/icu-messageformat-parser in a script. | - |
| [gettext tools, Babel, dennis, msgcheck, Translate Toolkit](https://www.gnu.org/software/gettext/) | strong | For gettext projects: unextracted strings, format-string mismatches, fuzzy and untranslated counts, punctuation and whitespace differences. | xgettext or pybabel extract, then msgfmt --check --statistics, msgcmp; dennis-cmd lint locale/; msgcheck *.po; pofilter (translate-toolkit). | Yes | System gettext; pip install babel dennis msgcheck translate-toolkit. | Yes | polib in a script. | - |
| [Playwright locale, time zone and direction contexts](https://playwright.dev/docs/emulation) | strong | The same pages under ar with dir=rtl, de-DE, en-US, ja-JP and far time zones; dates, numbers and currency that do not change with locale were hardcoded. | browser.newContext({locale, timezoneId}); diff the visible text between locales with regexes for dates and numbers. | Yes | With Playwright. | Yes | Ripgrep for format strings. | - |
| [Logical-property lint (stylelint-use-logical)](https://github.com/csstools/stylelint-use-logical) | strong | Every margin-left, text-align: left, float: right that breaks right-to-left. | npx stylelint -f json with csstools/use-logical or stylelint-plugin-logical-css. | Yes | Node. CC0 or MIT. | Yes | Ripgrep physical properties. | - |
| [ESLint no-restricted-syntax for format calls](https://eslint.org/docs/latest/rules/no-restricted-syntax) | strong | Hardcoded formats: toLocaleDateString() with a fixed locale, toFixed for money, string-built dates, moment().format('MM/DD/YYYY'), string concatenation inside translated messages. | Selectors shipped in the skill's ESLint config; -f json. | Yes | ESLint core. | Yes | Ripgrep patterns. | - |

Top 3 (each optional, with the fallback in its row): eslint-plugin-i18next no-literal-string (or the stack's equivalent), pseudo-localization with screenshots, @lingual/i18n-check with messageformat-validator.

All 16 rows, situational and skipped included: `tools.jsonl`, domain `localization`.

### Developer documentation (`docs-developer`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Vale](https://vale.sh/) | must-have | The owner's writing standard as rules: history phrases, filler, empty labels, sentence and paragraph length, term consistency. Lints Markdown, reStructuredText, AsciiDoc, HTML and the comments in source files. | vale --config &lt;skill&gt;/vale/.vale.ini --output=JSON --no-exit README.md docs src; JSON keyed by file with Check, Line, Span, Severity, Message, Match. vale sync fetches packages once. | Yes | One Go binary (Homebrew, pip install vale, or release download). MIT. Packages are downloaded by vale sync; vendor them in the skill for offline use. | Yes after sync | A regex pass in ledger.py over the same phrase lists (stdlib), without sentence scoping. | online in some modes |
| [lychee](https://lychee.cli.rs/) | must-have | Every dead link, missing local file and broken anchor in the docs. | lychee --format json --output lychee.json --include-fragments --no-progress '**/*.md'; --offline checks local paths only. | Local links yes; web links depend on the network. | One Rust binary (Homebrew, cargo, release). MIT or Apache-2.0. | --offline mode | remark-validate-links for local links; stdlib path check in ledger.py. | online in some modes |
| Doc-reference check (to build into ledger.py) | must-have | Every file path, command, npm or make script, environment variable and settings name mentioned in the docs, checked against the repo; and the reverse: every env var and setting in code with no mention in docs. | ledger.py doc-refs R-0001 (proposed): stdlib regex over Markdown code spans and fences, git ls-files, package.json scripts, Makefile targets, .env.example. | Yes | None: Python stdlib. | Yes | n/a | - |
| [Vale packages: write-good, proselint, alex, Microsoft, Google, Readability](https://vale.sh/hub/) | strong | Ready-made rules: passive voice, weasel words, cliches, insensitive terms, vendor style guides, readability grades. | Packages = write-good, proselint, Readability in .vale.ini; switch on single rules, not whole styles. | Yes | Downloaded by vale sync. | Yes after sync | Custom rules only. | online in some modes |
| [Vale packages: ai-tells, Voices, Comments, Std](https://vale.sh/explorer) | strong | tbhb/vale-ai-tells (134 rules for AI-sounding prose), jdkato/voices (hedging, puffery, throat-clearing), jdkato/comments (code-comment conventions, TODOs without an owner, commented-out code), vale-cli/Std. | Packages = tbhb/vale-ai-tells, jdkato/voices, jdkato/comments (values from the hub explorer; rule contents not inspected). | Yes | Downloaded by vale sync. Licences not verified. | Yes after sync | Custom rules only. | online in some modes |
| [markdownlint-cli2](https://github.com/DavidAnson/markdownlint-cli2) | strong | Structure faults: skipped heading levels, duplicate headings, bare URLs, code fences with no language, broken tables. | npx markdownlint-cli2 '**/*.md' with the JSON formatter (markdownlint-cli2-formatter-json, not verified). | Yes | Node. MIT. | Yes | pymarkdownlnt (Python); skip. | - |
| [typos](https://github.com/crate-ci/typos) | strong | Misspellings in code, comments and docs with few false alarms. | typos --format json. | Yes | One Rust binary. MIT or Apache-2.0. | Yes | codespell. | - |
| [Python docstring coverage and accuracy (interrogate, pydoclint, Ruff D rules)](https://github.com/jsh9/pydoclint) | strong | Coverage percent per module and every docstring whose arguments, returns or raises differ from the signature. | interrogate -v --fail-under 0 src; pydoclint --style=google src; ruff check --select D --output-format json. | Yes | pip. MIT. | Yes | ast walk in ledger.py for presence only. | - |
| [TypeScript and JavaScript doc checks (TypeDoc, eslint-plugin-jsdoc, API Extractor)](https://typedoc.org/) | strong | Undocumented exports, JSDoc params that do not match the function, and a diffable report of the public API. | npx typedoc --validation.notDocumented --json api.json; typedoc-plugin-coverage; npx eslint -f json with jsdoc/require-jsdoc, jsdoc/check-param-names, jsdoc/require-param; npx api-extractor run --local. | Yes | Node. Apache-2.0, BSD-3, MIT. | Yes | Reviewer reads. | - |
| [Docs-as-tests for code blocks (pytest-markdown-docs, Sybil, doctest, typescript-docs-verifier)](https://github.com/modal-labs/pytest-markdown-docs) | strong | Whether the examples in README and docs run and compile now. | pytest --markdown-docs README.md docs/; python -m doctest -v file.md; Sybil 10.1.0; pytest-codeblocks; mktestdocs; npx typescript-docs-verifier; @eslint/markdown lints JS fences. | Yes | pip or Node. MIT or Apache-2.0. | Yes | Run the commands by hand in a temp copy. | runs code |
| [Runme](https://runme.dev/) | strong | Runs the shell blocks of a Markdown file by name, in order. | runme list --json, runme run &lt;block&gt; or runme run --all in a throwaway clone or container. | Yes | Go binary or npx runme. Apache-2.0. | Yes | Extract fenced sh blocks with ledger.py and run them. | - |
| [OpenAPI checks (Redocly CLI, Spectral, Schemathesis)](https://redocly.com/docs/cli/) | strong | Spec lint (missing descriptions and examples) and, with Schemathesis, whether the running API matches its spec. | npx @redocly/cli lint openapi.yaml --format json; npx spectral lint -f json; schemathesis run openapi.yaml --url http://localhost:8000. | Lint yes; Schemathesis with a fixed seed. | Node or pip. MIT, Apache-2.0. | Yes | Compare routes to the spec by script. | - |

Top 3 (each optional, with the fallback in its row): Vale with the Plain style (references/vale/), lychee, a doc-reference check built into ledger.py; the language's doc-coverage tool fourth.

All 23 rows, situational and skipped included: `tools.jsonl`, domain `docs-developer`.

### End-user documentation (`docs-user`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Vale with a UI-text style](https://vale.sh/) | must-have | The writing standard applied to what users read: help pages, error messages, empty states, emails. | Extract strings first (locale catalogs, eslint-plugin-i18next output, or the rendered text from the Playwright probe) into ui-strings.md, one string per line with its source; then vale --output=JSON ui-strings.md. Vale can also read JSON and YAML values directly through its views feature (not verified). | Yes | As Vale. | Yes | Regex pass in ledger.py. | - |
| Internal-name scan (to build into ledger.py) | must-have | User-visible strings containing table, column, enum, class or route names, snake_case or camelCase tokens, stack traces, status codes. | ledger.py ui-strings R-0001 --identifiers (proposed): build the identifier list from migrations, models and enums; match against the extracted strings; output points TSV. | Yes | None: Python stdlib. | Yes | n/a | - |
| [Doc Detective](https://doc-detective.com/) | must-have | Runs the procedures in help pages against the app: follows 'click Save', checks the named button exists, retakes screenshots. | npx doc-detective runTests --input help/ --output dd.json; it detects steps in Markdown or reads inline test comments; JSON with PASS or FAIL per step. | Yes | Node 18+, browser. AGPL-3.0 (running it does not affect the reviewed code's licence). | Yes | Map each doc step to the UI by hand; mark untested. | - |
| [Rendered-text capture (Playwright aria snapshot)](https://playwright.dev/docs/aria-snapshots) | strong | All text a user can see on each page and state, whatever file it came from, plus DOM facts: placeholder equal to label, tooltip equal to button text, heading repeated in the breadcrumb. | In the UI probe: page.locator('body').ariaSnapshot() per route and role, saved as YAML; ledger.py derives the string list and the repeated-label checks. | Yes | With Playwright. | Yes | Catalog strings only. | - |
| [Doc screenshot freshness (shot-scraper or Playwright with odiff)](https://shot-scraper.datasette.io/) | strong | Which screenshots in the help pages differ from the app. | Retake each documented screen at the image's size; odiff old.png new.png gives percent changed. | Yes | As those tools. | Yes | Vision compare by the agent. | - |
| [lychee / linkinator on the help site](https://lychee.cli.rs/) | strong | Dead links in help pages and from the app into help. | lychee --format json &lt;site-url or build dir&gt;; npx linkinator &lt;url&gt; --recurse --format json. | Local yes. | As above. | Partly | Link list from the Playwright crawl. | online |
| [Error-message inventory by AST search](https://ast-grep.github.io/) | strong | Every message a user can be shown from a throw, flash, toast or validation rule, as a list to lint and to match against help pages. | ast-grep or ESLint no-restricted-syntax selectors, JSON output (ast-grep not verified here). | Yes | One binary or ESLint. | Yes | Ripgrep. | - |

Top 3 (each optional, with the fallback in its row): Vale on extracted UI strings, Doc Detective, an internal-name scan built into ledger.py.

All 11 rows, situational and skipped included: `tools.jsonl`, domain `docs-user`.

### Maintainability and handover (`handover`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Knowledge concentration from git (to build into ledger.py)](https://git-scm.com/docs/git-log) | must-have | Per directory: authors by share of changed lines over the last 12 months and all time, files with one author, files whose only author has no commit in a year, and a truck factor. | ledger.py knowledge R-0001 (proposed): git log --numstat --format=%H%x09%aN%x09%aI with .mailmap, computed in stdlib; records metrics truck_factor, single_author_files. | Yes for a commit. | git only. | Yes | n/a | - |
| Environment and secret inventory (to build into ledger.py) | must-have | Every environment variable and secret name the code reads, against .env.example, deploy config and docs: undocumented, unused, and names with no rotation note. | ledger.py env-vars R-0001 (proposed): regex for process.env.X, os.environ, getenv, ENV[...], ${{ secrets.X }}; compare sets. | Yes | None. | Yes | n/a | - |
| [codeowners-validator](https://github.com/mszostok/codeowners-validator) | strong | CODEOWNERS faults: owners who do not exist or lack access, patterns matching no file, files with no owner, duplicates. | REPOSITORY_PATH=. CHECKS=files,duppatterns,syntax EXPERIMENTAL_CHECKS=notowned codeowners-validator; owner checks need GITHUB_ACCESS_TOKEN (env names from memory). | Yes | Go binary or Docker. Apache-2.0. | File checks yes | gh api repos/{owner}/{repo}/codeowners/errors. | - |
| [GitHub CLI inventory](https://cli.github.com/) | strong | Who and what the repo depends on: collaborators and roles, branch protection, secret and variable names, environments, deploy keys, webhooks, apps, Actions that run on schedules. | gh api repos/{o}/{r}/collaborators, gh secret list --json name,updatedAt, gh variable list, gh api repos/{o}/{r}/branches/main/protection, gh api repos/{o}/{r}/hooks, gh workflow list --json name,state. | Yes at a moment. | gh and a token with admin read. | No | Ask the owner; record as an open gate. | online |
| [Clean-room setup run (Dev Containers CLI or Docker with Runme)](https://github.com/devcontainers/cli) | strong | Whether a new maintainer can clone, install, test and start using only the README: the README's commands run in a fresh container, and the step that fails is recorded. | devcontainer up --workspace-folder . then devcontainer exec ... runme run --all; or docker run --rm -v $PWD:/src:ro &lt;base image&gt; with the extracted commands. Dev Containers CLI not verified today. | Yes for pinned images. | Docker. MIT. | After image pull | Walk the steps by reading; mark untested. | online in some modes, runs code |
| [Expiry checks (certificates, domains, DNS)](https://github.com/domainaware/checkdmarc) | strong | Dates for the yearly chores: TLS certificate expiry, domain registration expiry, registrar, name servers, mail records. | openssl s_client -connect host:443 &lt;/dev/null 2&gt;/dev/null piped to openssl x509 -noout -enddate; RDAP curl https://rdap.org/domain/&lt;d&gt; (JSON); checkdmarc &lt;d&gt;. | Yes at a moment. | openssl, curl. | No | Ask the owner. | online |

Top 3 (each optional, with the fallback in its row): a git knowledge report and an environment inventory (both built into ledger.py, no install), then a clean-room setup run; a gh inventory where the repository is on GitHub.

All 13 rows, situational and skipped included: `tools.jsonl`, domain `handover`.

## Security and operations

### Security (`security`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Semgrep Community Edition](https://github.com/semgrep/semgrep) | must-have | Every match of a rule over every file, with rule id, CWE and exact lines; the skill can ship its own rules (routes without auth decorators, innerHTML, string-built SQL, log calls) so inventories stop depending on what a reviewer happened to read | semgrep scan --config &lt;pinned rules dir&gt; --sarif -o semgrep.sarif --metrics=off --disable-version-check &lt;export of the reviewed commit&gt; | yes (pinned version and pinned rules; --config auto or p/... fetches rules that change) | pipx/uv/brew/docker; ~150 MB; engine LGPL-2.1; registry rules under Semgrep Rules License (no redistribution in a competing product); cross-file taint is paid | yes with local rules | ast-grep or ripgrep patterns from the same rule list; say 'pattern scan not run' | online in some modes |
| [gitleaks](https://github.com/gitleaks/gitleaks) | must-have | Every secret-shaped string in every commit, including ones deleted from HEAD | gitleaks git --report-format sarif --report-path gitleaks.sarif --redact &lt;repo&gt;; gitleaks dir for the tree | yes | single binary ~15 MB; MIT | yes | ledger's own redaction patterns over git log -p; say 'history not scanned' | - |
| [Trivy](https://github.com/aquasecurity/trivy) | must-have | One binary for vulnerabilities, misconfiguration, secrets and licences; replaces tfsec | trivy fs --scanners vuln,misconfig,secret,license --format sarif -o trivy.sarif --skip-db-update . (after one trivy fs --download-db-only) | yes-db | single binary ~150 MB + DB ~700 MB; Apache-2.0 | yes with cached DB (--offline-scan) | checkov + osv-scanner + gitleaks | online in some modes |
| [Opengrep](https://github.com/opengrep/opengrep) | strong | Semgrep fork that keeps cross-function taint, fingerprints and Windows support in the open engine | opengrep scan --config &lt;rules&gt; --sarif-output=opengrep.sarif &lt;dir&gt; | yes | single binary; LGPL-2.1 | yes | Semgrep CE | - |
| [semgrep-rules / Trail of Bits rules](https://github.com/semgrep/semgrep-rules) | strong | Vetted rule sets to vendor at a commit hash, so the rule set is the same on every run | git clone at a pinned commit; --config &lt;dir&gt; | yes | ~30 MB; Semgrep Rules License v1.0 (semgrep-rules); AGPL-3.0 (trailofbits) | yes | skill's own small rule pack | - |
| [Bandit](https://github.com/PyCQA/bandit) | strong | AST checks for eval/exec, subprocess shell, weak crypto, yaml.load, assert, hardcoded binds | bandit -r . -f sarif -o bandit.sarif (extra bandit[sarif]) or -f json | yes | pip, small; Apache-2.0 | yes | ruff --select S (same checks reimplemented) | - |
| [gosec](https://github.com/securego/gosec) | strong | SSA-based checks incl. taint for SQL/command/path injection, weak TLS, unhandled errors | gosec -fmt sarif -out gosec.sarif ./... | yes | single binary; needs module to type-check; Apache-2.0 | yes (deps cached) | semgrep p/golang | online in some modes |
| [Brakeman](https://github.com/presidentbeef/brakeman) | strong | Rails-aware flow: mass assignment, SQLi, XSS in views, unsafe redirects, with confidence levels | brakeman -f sarif -o brakeman.sarif --no-exit-on-warn | yes | gem; Brakeman Public Use License (free to use, not OSI) | yes | semgrep ruby rules | - |
| [eslint-plugin-security / no-unsanitized / @microsoft/sdl](https://github.com/eslint-community/eslint-plugin-security) | strong | Flags eval, child_process with variables, non-literal fs/regexp/require, innerHTML and document.write sinks | npx eslint -c &lt;skill&gt;/eslint.security.config.mjs -f @microsoft/eslint-formatter-sarif -o eslint.sarif . | yes | npm; run with a config outside the repo; Apache-2.0 / MPL-2.0 / MIT | yes once installed | semgrep p/javascript | - |
| [cargo-audit / cargo-deny / cargo-geiger](https://github.com/rustsec/rustsec) | strong | Advisories, yanked crates, licence and source policy, unsafe usage counts per dependency | cargo audit --json; cargo deny --format json check; cargo geiger --output-format Json | yes-db | cargo install / binaries; MIT/Apache-2.0 | advisory DB cached | osv-scanner on Cargo.lock | online in some modes |
| [ast-grep](https://github.com/ast-grep/ast-grep) | strong | Structural search that returns every route, handler, query call or log call as JSON: the complete inventory a reviewer then fills in row by row | ast-grep scan -r rules.yml --json=stream . (or --format sarif) | yes | single binary ~40 MB; MIT | yes | ripgrep + ctags | - |
| [TruffleHog](https://github.com/trufflesecurity/trufflehog) | strong | 800+ detectors; can verify a secret is live against the provider | trufflehog git file://&lt;repo&gt; --json --no-verification (verification sends the candidate secret to the provider: owner decision) | yes without verification; no with it (live calls) | single binary ~80 MB; AGPL-3.0 | yes with --no-verification | gitleaks | online in some modes |
| [Checkov](https://github.com/bridgecrewio/checkov) | strong | 1,000+ policies incl. graph checks across resources | checkov -d . -o sarif --output-file-path out/ --skip-download | yes | pip, heavy (~300 MB); Apache-2.0 | yes with --skip-download | trivy config | online in some modes |
| [Hadolint](https://github.com/hadolint/hadolint) | strong | Dockerfile rules plus ShellCheck on RUN lines (root user, unpinned base, apt without cleanup) | hadolint -f sarif Dockerfile | yes | single binary; GPL-3.0 | yes | trivy config | - |
| [CSP Evaluator](https://github.com/google/csp-evaluator) | strong | Parses the policy and reports bypasses (unsafe-inline, missing object-src, allow-listed JSONP hosts) with severity | node -e using npm csp_evaluator on each policy string extracted from code/config; prints JSON | yes | npm, tiny; Apache-2.0 | yes | reviewer reads the policy against MDN's list | - |
| [MDN HTTP Observatory](https://github.com/mdn/mdn-http-observatory) | strong | Scores response headers: CSP, HSTS, cookies flags, CORS, referrer policy, X-Frame-Options, SRI | npx @mdn/mdn-http-observatory &lt;host&gt; (JSON to stdout; CLI name mdn-http-observatory-scan, not verified) | yes for a fixed response | npm; MPL-2.0 | yes against a local server | curl -sI + a header checklist in the brief | - |
| [OWASP ASVS 5.0 (machine-readable)](https://github.com/OWASP/ASVS) | strong | A fixed requirement list (CSV/JSON) to load as the security checklist, so every review answers the same ~350 items instead of what the model recalls | load 5.0/docs_en/*.json into the ledger as requirements (add-requirement --source ASVS-5.0); reviewers give a verdict per item | yes (the list); verdicts are judgement | a file; CC BY-SA 4.0 | yes | the bullet list in domains.md | - |

Top 3 (each optional, with the fallback in its row): Semgrep CE (or Opengrep) with a pinned rule set, gitleaks, Trivy.

All 46 rows, situational and skipped included: `tools.jsonl`, domain `security`.

### Privacy and data map (`privacy`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Bearer CLI](https://github.com/Bearer/bearer) | strong | Detects ~120 personal-data types in code and follows them to logs, third-party calls, cookies and storage; --report dataflow and --report privacy produce the data map itself | bearer scan . --report dataflow --format json --output dataflow.json; bearer scan . --format sarif --output bearer.sarif | yes | single binary ~100 MB; Elastic License 2.0 (free to use; owned by Cycode) | yes | semgrep rules for PII field names + grep of model/schema files; mark 'data map hand-built' | - |
| [Semgrep privacy rules (own pack)](https://github.com/semgrep/semgrep) | strong | Rules for 'PII-named field reaches logger/analytics/email', kept with the skill and extended from each review's findings | semgrep scan --config &lt;skill&gt;/rules/privacy --sarif | yes | with Semgrep; own rules | yes | grep | - |

Top 3 (each optional, with the fallback in its row): Bearer (data-flow and privacy reports), the skill's own Semgrep privacy rules, a personal-data pass over fixtures and log samples (Presidio, or a regex pass in ledger.py).

All 11 rows, situational and skipped included: `tools.jsonl`, domain `privacy`.

### Logging (`logging`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Log-statement inventory by pattern (Semgrep or ast-grep rule pack)](https://github.com/ast-grep/ast-grep) | must-have | The complete list of log calls with level, message template and arguments, and of catch blocks with no log or rethrow; the report's inventory is then counted, not sampled | ast-grep scan -r &lt;skill&gt;/rules/logging/*.yml --json=stream → ledger metric log_statements, swallowed_catches | yes | with ast-grep/Semgrep; own rules | yes | ripgrep for logger names per language | - |
| [Semgrep rules: secrets and PII in logs, log injection](https://github.com/semgrep/semgrep-rules) | strong | Logger calls whose arguments are named password/token/secret/email, or take request input unescaped (CWE-532, CWE-117) | semgrep scan --config &lt;pinned rules&gt;/…/logging --sarif | yes | with Semgrep; Semgrep Rules License | yes | grep logger lines for sensitive names | - |
| [Ruff logging rules (G, LOG, TRY400/401, BLE001, S110/S112)](https://github.com/astral-sh/ruff) | strong | f-strings in log calls, logging.exception outside handlers, root-logger use, blind except, try/except/pass | ruff check --select G,LOG,TRY,BLE,S110,S112 --output-format sarif --isolated . | yes | single binary; MIT | yes | pylint logging checker (W1201-W1203, W0702, W0718) | - |
| [ESLint no-console / no-empty / unused catch binding rules](https://eslint.org/docs/latest/rules/no-empty) | strong | console.* left in code, empty catch blocks, caught errors never used | npx eslint --rule 'no-empty: error' --rule 'no-console: warn' -f json . | yes | npm; MIT | yes | ast-grep rule | - |
| [Log-sample checks: jq schema check, Presidio/regex PII pass, gitleaks over log files](https://github.com/gitleaks/gitleaks) | strong | Captures real output from the test run and checks each line parses as JSON, has timestamp-with-zone/level/request id, and holds no secrets or personal data | run the suite with logs to a file; gitleaks dir logs/ --redact; ledger.py check-log-sample logs/app.jsonl --require ts,level,msg,request_id | yes for a fixed sample | none beyond gitleaks; - | yes | read a sample by eye | runs code |

Top 3 (each optional, with the fallback in its row): a log-statement inventory by pattern (ast-grep or Semgrep rules shipped with the skill), the language's own exception and logging lint rules, a check of a real log sample.

All 11 rows, situational and skipped included: `tools.jsonl`, domain `logging`.

### Operations, CI/CD and recovery (`operations`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [actionlint](https://github.com/rhysd/actionlint) | must-have | Type-checks expressions, contexts, matrix, needs, cron syntax, and runs ShellCheck on run: blocks | actionlint -format '{{json .}}' (SARIF via the documented template) | yes | single binary ~5 MB; MIT | yes | check-jsonschema --builtin-schema vendor.github-workflows | - |
| [zizmor](https://github.com/zizmorcore/zizmor) | must-have | Security audit: template injection, pull_request_target misuse, unpinned actions, excessive permissions, credential persistence, cache poisoning, known-vulnerable actions | zizmor --format sarif --offline . &gt; zizmor.sarif | yes offline; online audits query GitHub | pipx/cargo/binary; MIT | yes with --offline | poutine, or grep uses: for non-SHA refs | online in some modes |
| [OpenSSF Scorecard](https://github.com/ossf/scorecard) | strong | 18 scored checks: branch protection, code review, token permissions, pinned dependencies, dangerous workflows, signed releases, fuzzing, SAST present | scorecard --local . --format json (local subset) or --repo=github.com/o/r with a token (full; reads repo settings) | yes for a fixed repo state | single binary / docker; Apache-2.0 | --local yes; full needs GitHub API | gh api for branch protection + zizmor | online |
| [gh api settings snapshot (branch protection, rulesets, environments, secrets names, Actions permissions)](https://cli.github.com/manual/gh_api) | strong | The actual protections on main and production environments, which no file in the repo shows | gh api repos/{o}/{r}/branches/main/protection; .../rulesets; .../environments; .../actions/permissions → JSON saved as artifacts | yes for a point in time | gh CLI; MIT | no | ask the owner; mark 'not verified' | online |
| [check-jsonschema](https://github.com/python-jsonschema/check-jsonschema) | strong | Validates CI and bot config against their published schemas | check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml; --builtin-schema vendor.dependabot .github/dependabot.yml; --builtin-schema vendor.renovate renovate.json -o json | yes (schemas vendored in the package) | pip; Apache-2.0 | yes with builtin schemas | yamllint | - |
| [ShellCheck](https://github.com/koalaman/shellcheck) | strong | Quoting, unset variables, set -e pitfalls, unchecked cd: the failures that break a restore at 3 a.m. | shellcheck -f json1 $(git ls-files '*.sh') (SARIF via shellcheck-sarif from sarif-rs) | yes | single binary; GPL-3.0 | yes | bash -n | - |
| [Backup verification: restic check / pgBackRest verify / pg_restore --list / sqlite3 integrity_check / Litestream](https://github.com/restic/restic) | strong | Proof that a backup opens and holds data: structure check, restore to a scratch location, row counts against the source | restic check --read-data-subset=5% --json; pgbackrest --stanza=x verify; sqlite3 restored.db 'PRAGMA integrity_check; SELECT count(*) ...'; wrangler d1 export / time-travel info for D1 | yes for a given backup | existing tool; BSD / MIT / Apache | restore drill is local; fetching the backup is not | 'restore never tested' recorded as a finding | online |
| [Docker Compose / Terraform / Wrangler validate](https://developers.cloudflare.com/workers/wrangler/commands/) | strong | Config that does not parse or references missing bindings | docker compose config -q; terraform validate -json; wrangler deploy --dry-run --outdir /tmp/x; wrangler types --check | yes | the project's own toolchain; - | mostly | read the config | - |

Top 3 (each optional, with the fallback in its row): actionlint, zizmor, OpenSSF Scorecard (with a gh api snapshot of branch protection and environments).

All 17 rows, situational and skipped included: `tools.jsonl`, domain `operations`.

### Dependencies and supply chain (`dependencies`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [OSV-Scanner](https://github.com/google/osv-scanner) | must-have | Known-vulnerable versions for every lockfile in the tree, with fixed versions and (Go, Rust) call analysis; licenses check; guided remediation plan | osv-scanner scan source -r --format sarif --output osv.sarif --offline-vulnerabilities --download-offline-databases . (first run downloads; later --offline) | yes-db (record the DB date) | single binary ~50 MB + DBs; Apache-2.0 | yes with local DBs; online mode sends package names and versions to osv.dev | ecosystem audit command; else 'not checked online' | online in some modes |
| [Grype + Syft](https://github.com/anchore/grype) | strong | Syft writes the SBOM (the dependency inventory as data); Grype matches it against vulnerability data with EPSS and KEV flags | syft dir:. -o cyclonedx-json=sbom.cdx.json; grype sbom:sbom.cdx.json -o sarif &gt; grype.sarif (GRYPE_DB_AUTO_UPDATE=false after one grype db update) | yes-db | two binaries ~100 MB + DB; Apache-2.0 | yes with cached DB | osv-scanner; npm ls --all --json etc. as the inventory | online in some modes |
| [Trivy (fs mode)](https://github.com/aquasecurity/trivy) | strong | Same job as OSV-Scanner/Grype plus licences and misconfig | trivy fs --scanners vuln,license --format cyclonedx / sarif . | yes-db | see security; Apache-2.0 | yes with cached DB | osv-scanner | online in some modes |
| [Ecosystem audits: npm audit / pnpm audit / yarn npm audit / pip-audit / bundler-audit / govulncheck / cargo audit / dotnet list package --vulnerable / composer audit](https://github.com/pypa/pip-audit) | strong | The package manager's own advisory check; govulncheck reports only vulnerabilities whose code is called | npm audit --json; pip-audit -r requirements.txt --no-deps --disable-pip -f json (avoids running setup.py); bundle-audit check --format json; govulncheck -format sarif ./...; cargo audit --json; dotnet list package --vulnerable --format json; composer audit --format=json | yes-db | already present with the toolchain (pip-audit: pip); various OSS | no, except bundler-audit and cargo-audit with a cached DB | osv-scanner | online, runs code |
| [Outdated and age: npm outdated / pip list --outdated / cargo outdated / go list -u -m all / libyear](https://github.com/kbknapp/cargo-outdated) | strong | How far behind each dependency is, as numbers the ledger can trend (count outdated, majors behind, libyears) | npm outdated --json; pip list --outdated --format json; cargo outdated --format json; go list -u -m -json all; npx libyear --json | yes for a registry snapshot | built-in / npx; various | no | 'not checked online' | online |
| [deps.dev API](https://github.com/google/deps.dev) | strong | Per package version: licences, advisories, Scorecard of the source repo, dependents, latest release date: 'abandoned' becomes a date | GET https://api.deps.dev/v3/systems/{sys}/packages/{name}/versions/{v} (JSON; sends package names only) | yes for a snapshot | none (HTTP); free API, CC-BY data | no | registry JSON (pypi.org/pypi/x/json, registry.npmjs.org/x) for last-release dates | online |
| [endoflife.date API / xeol](https://github.com/endoflife-date/endoflife.date) | strong | Whether the pinned Node/Python/Postgres/framework version is past end of life, with the date | GET https://endoflife.date/api/v1/products/{product}/ (JSON); xeol dir:. -o json (xeol last commit 2025-03) | yes for a snapshot | none; MIT data | no (a dated copy can be vendored) | reviewer's knowledge, marked 'not verified' | online |
| [Licence checks: cargo-deny / license-checker-rseidelsohn / pip-licenses / go-licenses / licensee / ScanCode / ORT / REUSE](https://github.com/aboutcode-org/scancode-toolkit) | strong | Licence of every dependency against an allow-list; ScanCode finds licence text inside vendored files | npx license-checker-rseidelsohn --json --production; pip-licenses --format=json; go-licenses report ./...; cargo deny check licenses; osv-scanner --licenses=MIT,Apache-2.0,...; scancode -l --json-pp out.json vendor/ | yes | small CLIs; ScanCode and ORT heavy; various OSS | yes for installed packages | read licence fields in lockfiles/manifests | - |
| [Unused and undeclared dependencies: Knip / depcheck / deptry / cargo-machete / cargo-udeps / go mod tidy -diff](https://github.com/webpro-nl/knip) | strong | Dependencies declared but never imported, imported but not declared, and unused exports/files | npx knip --reporter json; deptry . --json-output deptry.json; cargo machete; go mod tidy -diff | yes | npm / pip / cargo; ISC / MIT | yes | grep imports against the manifest | - |
| [Install scripts and lockfile hygiene: can-i-ignore-scripts / npm query / lockfile-lint](https://github.com/lirantal/lockfile-lint) | strong | Packages with install scripts; lockfile entries resolving to non-registry hosts or http | npm query ':attr(scripts, [postinstall]), :attr(scripts, [install]), :attr(scripts, [preinstall])' ; npx can-i-ignore-scripts; npx lockfile-lint --path package-lock.json --type npm --allowed-hosts npm --validate-https | yes | npm; Apache-2.0 / MIT | yes | jq over the lockfile for hasInstallScript | - |
| [Renovate / Dependabot config checks](https://github.com/renovatebot/renovate) | strong | Whether update automation exists, parses, and covers every manifest and the Actions ecosystem | npx --package renovate renovate-config-validator; check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml; compare package-ecosystem entries with manifests found | yes | npx (large) / pip; AGPL-3.0 (Renovate); MIT | yes | read the config | - |

Top 3 (each optional, with the fallback in its row): OSV-Scanner with offline databases, Syft for an SBOM per review, and the lockfile facts that need no install (install scripts, non-registry sources, unpinned ranges, updater coverage).

All 18 rows, situational and skipped included: `tools.jsonl`, domain `dependencies`.

### Performance (`performance`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [hyperfine](https://github.com/sharkdp/hyperfine) | must-have | Wall-clock mean, deviation and outliers over warm runs for CLI commands, test suites, builds, batch jobs | hyperfine --warmup 3 --runs 10 --export-json bench.json 'cmd A' 'cmd B' | noisy (reports spread; compare with tolerance) | single binary ~1 MB; MIT/Apache-2.0 | yes | /usr/bin/time -v x5 in a loop | runs code |
| [Database planner: EXPLAIN QUERY PLAN (SQLite) / EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) (Postgres) / EXPLAIN FORMAT=JSON (MySQL)](https://www.sqlite.org/eqp.html) | must-have | For every distinct query: scan or index, estimated rows, sort and temp B-trees; exact on a schema copy | sqlite3 copy.db 'EXPLAIN QUERY PLAN &lt;query&gt;'; psql -c 'EXPLAIN (FORMAT JSON) ...' on a schema-only restore; D1: wrangler d1 execute --local | yes for a fixed schema and statistics | none (database CLI); - | yes | reason from the schema; mark 'planner not run' | - |
| [k6](https://github.com/grafana/k6) | strong | Latency percentiles and error rates under scripted load with pass/fail thresholds | k6 run --summary-export summary.json --out json=raw.json script.js (against localhost or staging only) | noisy | single binary ~60 MB; AGPL-3.0 | yes local | oha / autocannon | runs code |
| [Lighthouse / Lighthouse CI](https://github.com/GoogleChrome/lighthouse-ci) | strong | Page weight, render-blocking resources, LCP/TBT/CLS in a throttled lab run, with budgets and assertions; median of N runs | lhci collect --url=http://localhost:PORT --numberOfRuns=5 && lhci assert --preset=lighthouse:no-pwa ; or lighthouse URL --output json --output-path lh.json --chrome-flags=--headless | noisy (use median of 5; byte counts are exact) | npm + Chrome; Apache-2.0 | yes local | sum transfer sizes with curl; count blocking tags | - |
| [Bundle analysis: esbuild --metafile / webpack-bundle-analyzer / rollup-plugin-visualizer / source-map-explorer / size-limit](https://github.com/ai/size-limit) | strong | Bytes per module in the shipped bundle; duplicate packages; size budget as a test | esbuild --bundle --metafile=meta.json --analyze; webpack --profile --json &gt; stats.json; npx source-map-explorer dist/*.js --json; npx size-limit --json | yes | npm; MIT | yes | ls -l dist and gzip -c \| wc -c | - |
| [Python profilers: py-spy / Scalene / pyinstrument / cProfile / Memray / tracemalloc](https://github.com/benfred/py-spy) | strong | Where time and memory go, per line; py-spy attaches without code changes; Memray tracks every allocation | py-spy record -f speedscope -o prof.json -- python job.py; pyinstrument -r json -o p.json job.py; scalene --json --outfile s.json job.py; memray run -o m.bin job.py && memray stats --json m.bin | noisy (sampling); allocation counts near-exact | pip / single binary; MIT / Apache-2.0 | yes | python -X importtime; cProfile (stdlib) | runs code |

Top 3 (each optional, with the fallback in its row): hyperfine, the database's query planner over every extracted query, Lighthouse CI (web) or the language's sampling profiler for back-end jobs.

All 20 rows, situational and skipped included: `tools.jsonl`, domain `performance`.

### Cost (`cost`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [Infracost](https://github.com/infracost/infracost) | strong | Monthly cost per resource from the IaC, cost diff between commits, and usage-file scenarios (current vs 10x) | infracost breakdown --path . --format json --out-file infracost.json --usage-file usage-10x.yml; infracost diff --compare-to base.json | yes for a pricing snapshot | single binary + free API key (prices from Infracost Cloud Pricing API; self-hostable); Apache-2.0 CLI; paid cloud features | no (sends resource types/sizes, not code); pricing API can be self-hosted | vendor calculator by hand; 'not verified' | online |
| [Cloud billing exports (read-only): aws ce get-cost-and-usage / gcloud billing / az costmanagement / Cloudflare GraphQL analytics / vendor invoices](https://github.com/aws/aws-cli) | strong | Actual spend per service per month: the baseline every estimate should be checked against | aws ce get-cost-and-usage --time-period Start=..,End=.. --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE (owner runs it or grants a read-only profile) | yes for a closed month | vendor CLI + read-only credentials; - | no | ask the owner for last month's invoice per service | online |
| [Price lists as data: AWS Price List API / Azure Retail Prices API / GCP Cloud Billing Catalog API / ec2instances.info](https://github.com/vantage-sh/ec2instances.info) | strong | Current unit prices by SKU, fetched and dated, instead of prices remembered by a model | aws pricing get-products --service-code AmazonEC2 --filters ...; curl 'https://prices.azure.com/api/retail/prices?$filter=...' | yes for a snapshot (store the date) | none / vendor CLI; public | no | pricing page quoted with its date; 'not verified' | online |
| [Budget and limit guards as code checks (Semgrep/Conftest/Checkov rules)](https://github.com/bridgecrewio/checkov) | strong | Missing spending limits: no budget resource, no log retention, no S3 lifecycle, unbounded autoscaling, no max_tokens, retries without cap | checkov --check CKV_AWS_338,... ; semgrep rules for max_tokens, retry loops without backoff/limit; conftest policy 'every log group has retention' | yes | with the scanners; - | yes | reviewer checklist | - |
| [LLM price tables: LiteLLM model_prices_and_context_window.json / models.dev / llm-prices / OpenRouter models API](https://github.com/BerriAI/litellm) | strong | Per-model input/output/cache prices as dated JSON, for computing cost at current and 10x token volume | curl raw model_prices_and_context_window.json at a pinned commit; https://models.dev/api.json (OpenCode's own model catalogue); record the commit/date | yes for a pinned snapshot | none; MIT | no (vendor a dated copy) | provider pricing page quoted with date | online |
| [Token counters: Anthropic count_tokens endpoint / tiktoken / tokencost](https://github.com/openai/tiktoken) | strong | Exact tokens per prompt template and typical payload, to multiply by call counts | client.messages.count_tokens(...) (free endpoint, needs API key; not re-verified today); python -c 'import tiktoken; ...'; tokencost (last commit 2025-09) | yes | pip; MIT | tiktoken yes; Anthropic no (the old @anthropic-ai/tokenizer npm package dates from 2023 and does not match current models) | chars/4 estimate marked estimated | - |
| [Agent-usage accounting: ccusage / claude -p --output-format json total_cost_usd / OpenCode session stats](https://github.com/ryoppippi/ccusage) | strong | What the review itself cost, per run and per subagent | npx ccusage session --json; claude -p ... --output-format json \| jq .total_cost_usd; OpenCode: opencode stats (not confirmed in today's docs) | yes (reads logs) | npx; MIT | yes | none: record 'cost not captured' | - |

Top 3 (each optional, with the fallback in its row): Infracost (Terraform or CloudFormation), a dated price table as data, real billing exports through a read-only credential.

All 12 rows, situational and skipped included: `tools.jsonl`, domain `cost`.

## The review process

### Review process (`process`)

| Tool | Rating | What it adds | How it is called | Deterministic | Install and licence | Offline | Fallback | Marks |
|---|---|---|---|---|---|---|---|---|
| [SARIF 2.1.0](https://github.com/oasis-tcs/sarif-spec) | must-have | One schema for tool, version, rule id, level, file, region, snippet, fingerprints and code flows; one importer instead of one parser per tool | ledger.py import-sarif (stdlib json); validate with the OASIS schema | yes | a schema file; OASIS open standard | yes | per-tool JSON adapters | - |
| [mise / aqua (tool version pinning)](https://github.com/jdx/mise) | must-have | One file listing exact scanner versions; mise x runs them without touching the system; versions go in the ledger with each run | mise install (from &lt;skill&gt;/mise.toml); mise x -- semgrep ... ; aqua i -l | yes: this is what makes tool output comparable between runs and machines | single binary; tools downloaded once to ~/.local/share/mise; MIT | after first install | tools doctor prints versions found on PATH and warns on drift | - |
| [git worktree / git archive at the reviewed commit](https://git-scm.com/docs/git-worktree) | must-have | Scanners read exactly the commit the ledger recorded, not a dirty tree; their caches and reports land outside the repository | git worktree add --detach $TMP/R-0001 &lt;sha&gt; (or git archive &lt;sha&gt; \| tar -x -C $TMP) | yes | built-in; GPL-2.0 | yes | - | - |
| [Prompt templating: string.Template in ledger.py / Jinja2 / harness placeholders](https://github.com/pallets/jinja) | must-have | The reviewer prompt is rendered from the ledger (scope, goals, known issues, tool results) by code, byte-identical for identical ledger state, and its hash is stored with the run | ledger.py render-prompt R-0001 security-1 &gt; prompt.txt (stdlib); OpenCode commands: $ARGUMENTS, $1, !cmd; Claude skills: $ARGUMENTS, !cmd, ${CLAUDE_SKILL_DIR} | yes | none; BSD-3 (Jinja) | yes | the manager writes prompts by hand, as today | - |
| [Run-to-run comparison in the ledger (to write)](https://github.com/microsoft/sarif-tools) | must-have | Overlap of findings between runs by (domain, sub, primary location): the direct measure of repeatability | ledger.py compare R-0001 R-0002 --json → only-in-A, only-in-B, both, Jaccard; add-metric review_overlap | yes | none; - | yes | SQL by hand over findings | - |
| [sarif-tools](https://github.com/microsoft/sarif-tools) | strong | sarif diff between two runs (new, fixed, unchanged), summary, csv, html, trend, blame | sarif diff old/ new/ -o diff.json; sarif summary *.sarif; sarif html -o report.html | yes | pip (PyPI 3.0.5, 2025-07); MIT | yes | ledger.py's own diff on fingerprints | - |
| [GitHub code scanning, Dependabot and secret-scanning alerts (read)](https://github.com/github/codeql-action) | strong | What GitHub already found, with alert state and dismissal reasons: prior art the review should reconcile with | gh api repos/{o}/{r}/code-scanning/alerts --paginate; .../dependabot/alerts; .../secret-scanning/alerts → JSON artifacts | yes for a point in time | gh CLI + token with security_events scope; free on public repositories; private needs GitHub Code Security / Secret Protection | no | none | online |
| [pre-commit / prek / lefthook](https://github.com/pre-commit/pre-commit) | strong | Runs the project's own pinned checks exactly as contributors do | pre-commit run --all-files --show-diff-on-failure (on an export; hooks may modify files); prek run --all-files | yes (revisions pinned in the config) | pip / single binary (prek); MIT | after environments are built | run the linters named in the config directly | runs code |
| [Ledger MCP server (to write)](https://github.com/modelcontextprotocol/python-sdk) | strong | Typed read-only tools (ledger_query, goals, known_issues, show_location, gates) so subagents cannot build a wrong command line or reach a write command | python3 &lt;skill&gt;/scripts/ledger_mcp.py --root &lt;project&gt; (stdio; either ~150 lines of stdlib JSON-RPC, or FastMCP from the mcp package) | yes | none (stdlib) or pip mcp; MIT SDK | yes | ledger.py query through the shell, as today | - |
| [Schema-constrained output: claude -p --json-schema, workflow agent(prompt, {schema}), Agent SDK structured outputs](https://code.claude.com/docs/en/headless) | strong | The harness retries until the subagent's reply validates, so a reviewer cannot hand back a malformed index | claude -p '&lt;prompt&gt;' --bare --output-format json --json-schema "$(cat points.schema.json)" \| jq .structured_output | shape yes, content no | built into Claude Code; - | no | validate after the fact with ledger.py validate | online |
| [JSON Schema validators: fastjsonschema (generate a standalone validator) / jsonschema / check-jsonschema / sourcemeta jsonschema / ajv](https://github.com/horejsek/python-fastjsonschema) | strong | Same schema checks in both harnesses, with error paths; fastjsonschema can compile a schema to plain Python with no imports, which dev/build.py could inline so ledger.py stays stdlib-only | build time: fastjsonschema.compile_to_code(schema); run time: ledger.py validate steps.jsonl --schema plan-steps | yes | none at run time; BSD-3 / MIT / Apache-2.0 (sourcemeta CLI: AGPL, not verified) | yes | the hand-written checks load-index and import-steps already do | - |
| [claude plugin eval](https://code.claude.com/docs/en/plugin-evals) | strong | Runs cases N times, grades with regex / tool_used / tool_order / file_exists / llm / baseline graders, compares with a no-plugin arm, exits 1 under a threshold | claude plugin eval --runs 5 --threshold 0.8 --json results.json --model &lt;pinned&gt; --trust-plugin (cases in evals/&lt;case&gt;/prompt.md + graders/) | graders regex/tool/file: yes; llm graders: no | built into Claude Code; early access per the docs page | no | promptfoo | online |
| [promptfoo](https://github.com/promptfoo/promptfoo) | strong | Matrix of prompts x models x cases with deterministic assertions (contains, regex, is-json, JSON-schema, javascript/python) and --repeat for variance | promptfoo eval -c evals/promptfooconfig.yaml --repeat 5 -o out.json (exec provider wrapping opencode run or claude -p) | assertions yes | npm; MIT; company acquired by OpenAI in March 2026 (news reports), repo still active | with local models only | a shell loop + ledger.py diff of two reviews | - |
| [Planted-defect targets: OWASP Benchmark, Juice Shop, the skill's own seeded repos](https://github.com/OWASP-Benchmark/BenchmarkJava) | strong | Ground truth: known vulnerabilities with expected results, so recall and false-positive rate are numbers | run the skill on the fixture; compare findings with expectedresults CSV | yes (the answer key) | git clone; GPL-2.0 / MIT | yes | dev/tests planted cases | - |
| [Claude Code dynamic workflows](https://code.claude.com/docs/en/workflows) | strong | Orchestration as a saved script (agent, pipeline, parallel, per-agent schema); clock and randomness are blocked so a relaunch repeats the same calls; up to 16 agents at once | workflows/review.js in a plugin (runs as /&lt;plugin&gt;:review) or .claude/workflows/ | the orchestration yes; agent content no | built in (paid plans); - | no | SKILL.md Steps 3-5 driven by the manager, as today | online |

Top 3 (each optional, with the fallback in its row): prompts rendered from the ledger (ledger.py prompt), schema-validated reviewer output, and a repeatability measure (ledger.py compare-runs plus an eval suite on a planted-defect repository).

All 33 rows, situational and skipped included: `tools.jsonl`, domain `process`.

## Not verified

What the researchers could not confirm on 2026-11-11:

- Command flags and output formats marked "not verified" in their cells; no tool was run.
- Pages that could not be fetched: scc, the Semgrep MCP server, Postgres MCP Pro and CodeLore repositories' web pages
  (read from the repositories instead), a harness LSP page, the Atlas community-edition limits, the Chrome DevTools
  MCP repository page (its tool names are not listed), the Bullet repository.
- Go and Rust binaries without a registry entry (Mailpit, Maestro, codeowners-validator, Scorecard, Runme) carry no
  version. The rule set in `vale/` was run with Vale 3.26.0 on 2026-11-12 ("How a tool is wired in").
- Semgrep's MCP server: the separate repository's last commit is 2025-10; whether the main CLI ships it is
  unconfirmed. CodeQL is free on public repositories only.
- Licences to check before use: Brakeman (non-commercial public-use licence), mutant for Ruby (paid for private code),
  CodeQL CLI (private code needs a paid plan), Qlty CLI (BSL with an AI-service exclusion), Liquibase (FSL from v5),
  Atlas (EULA binary), LocalStack (account-based edition), Semgrep registry rules and Pro engine, MegaLinter (AGPL).
- Stale or folded: tfsec is part of Trivy; Terrascan, Security Code Scan, piicatcher, Webbkoll, nplusone and wrk2 have
  no recent commits. Betterleaks (from the author of gitleaks) is at release candidates.
- Harness details: headless flags, structured output and whether a skill can carry agents or commands were not in the
  harness pages read; harness configuration shapes differ between versions, so check the installed one.
