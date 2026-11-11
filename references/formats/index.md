# Index, known-issues, matcher and requirement-trace files

The files indexers and matchers write, and the commands that load them. Every file is UTF-8 text, one row per line,
cells separated by one tab, no header line; blank lines are skipped and each cell is trimmed. A file is checked whole
before anything is written: one bad row refuses the whole file (exit 1, one line naming the rows), and nothing is
loaded.

    ledger.py import-findings R-0001 RUN_LABEL points.tsv
    ledger.py import-checks   R-0001 known.tsv [--run RUN_LABEL]
    ledger.py load-index      R-0001 RUN_LABEL points.tsv [known.tsv] [--trace trace.tsv]
    ledger.py match           R-0001 --file matches.tsv

`load-index` does the first two (and the trace) in one transaction.

## Points (`points.tsv`): 16 columns

| # | column | what it holds |
|---|--------|---------------|
| 1 | id | the point id: the prefix you gave the indexer plus the report's own id (`SEC-H3`); unique in the file and in the review |
| 2 | sev | `H`, `M`, `L`, or `T` (a missing test; kind must then be `test`) |
| 3 | kind | defect, test, validation, refactor, feature, design, docs, logging, perf, security, ops, verify |
| 4 | theme | one code of the ledger's `themes` table |
| 5 | sub | a short slug naming the specific thing, reused for the same thing in any report or review |
| 6 | locations | one or more locations separated by `;` (below); the first is the primary one |
| 7 | where_report | the report's line numbers (`L120-131`) |
| 8 | summary | one self-contained sentence (it becomes the issue's title when the point opens an issue) |
| 9 | before | what the code does now |
| 10 | fix | the recommended fix |
| 11 | test | the test that proves it: `test_name: what it asserts` |
| 12 | related | ids of other points in the same file, separated by `,` or spaces (stored as a JSON list) |
| 13 | known_issue | an `ISS-` code the report named, or empty (kept in the finding's detail as a matching hint) |
| 14 | domain | a domain code, or empty for the reviewer run's domain |
| 15 | goals | design goal codes separated by `,` (`G-01,G-04`), or empty |
| 16 | requirements | `REQ-code=verdict` separated by `,` (`REQ-0003=differs`); verdicts: matches, differs, partly, missing, untested |

A location is `repo:path[:start[-end]][#symbol]`:

- `demo:src/app.py:10-20#parse` lines 10 to 20, in `parse`; `demo:src/app.py:10` one line;
- `demo:src/app.py#parse` the symbol only (fill-refs finds its line);
- `demo:src/app.py` the whole file (stored with `whole_file` = 1).

`repo` is a repository name of the review (`review_repositories`); it may be left out when the review has only one
repository. The path is relative to the repository, with forward slashes, no `..` and no leading `-` or `/`, and it
must exist at the reviewed commit (`git cat-file -e <commit>:<path>`). Lines and commit are those of the reviewed
commit; the commit is filled from the review.

The fingerprint stored with each point is `domain:theme:sub` (no symbol, so the same problem cited at another symbol,
or without one, still has one fingerprint). Points are loaded unmatched (`issue_id` NULL), with verification
`unverified`; match them with `match`.

## Known issues (`known.tsv`): 4 columns

| # | column | what it holds |
|---|--------|---------------|
| 1 | issue | the `ISS-` code from the brief's known issues |
| 2 | outcome | still-present, changed, fixed, regressed, obsolete, not-checked |
| 3 | evidence | what the reviewer saw (required for every outcome but not-checked) |
| 4 | finding | the point id of this review that raises it again, or empty (`review_issue_checks.finding_id`) |

Each issue once per file. The checks' actor is the reviewer run's (`--run`, or the run given to `load-index`).

## Matches (`matches.tsv`): 6 columns

| # | column | what it holds |
|---|--------|---------------|
| 1 | point | the point id |
| 2 | issue | an `ISS-` code, or `NEW` for a new issue (the next code) |
| 3 | match_type | new, still-present, changed, regression, recurrence, continuation (empty = new for `NEW`) |
| 4 | confidence | certain, likely, possible (empty = likely) |
| 5 | rationale | why this match |
| 6 | relations | extra relations from the point's issue, `relation:ISS-code` separated by `;` (`related-to:ISS-0004;caused-by:ISS-0007`), or empty |

A `continuation` needs a `continuation-of:ISS-code` relation. A false finding (verification `false`) matched to `NEW`
gets its own issue closed as `obsolete`, resolution `false finding: <why>` (the verification note, else the
rationale).

## Requirement trace (`trace.tsv`): 6 columns

| # | column | what it holds |
|---|--------|---------------|
| 1 | requirement | a `REQ-` code |
| 2 | verdict | matches, differs, partly, missing, untested |
| 3 | where_implemented | file, function or page, or empty |
| 4 | tested | yes, partly, no, or empty |
| 5 | finding | the point id raised for it, or empty |
| 6 | evidence | what the reviewer saw |
