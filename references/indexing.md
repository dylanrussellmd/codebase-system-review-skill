# Indexing and matching

Read this in Step 5a, to brief the indexers, and in Step 5d, to match. The file formats are in
`references/formats/index.md`.

## The indexer brief

Each indexer reads ONE report in full and writes, in `.code-review/reviews/R-0001/index/`:

- `<report>.points.tsv`: one point per line, 16 columns (`references/formats/index.md#points-pointstsv-16-columns`),
  ids starting with the prefix you give it (`SEC-`), then the report's own id (`SEC-H3`), else section and row;
- `<report>.known.tsv`: the report's "Known issues" table (issue, outcome, evidence, the point id that raises it again);
- for the spec report, `<report>.trace.tsv`: one row per requirement (`REQ-` code, verdict, where, tested, point,
  evidence).

It does not re-review and changes nothing. It may read the ledger (`ledger.py query`, read-only) for `themes`,
`domains`, `design_goals`, `requirements` and the known issues' slugs. It replies only with its file paths, line
counts, counts by severity, and what it was unsure how to index.

What counts as a point (when in doubt, include it):
- every ranked gap (High, Medium, Low);
- every table row that states something wrong, missing, partial, untested, unvalidated, unreachable, duplicated,
  hardcoded, inconsistent, undocumented, unlogged or unverified (rows that say tested or solid are not points);
- every item in lists such as no-validation, validation-branches-with-no-test, missing tests, defects, nondeterminism,
  surviving mutants, stale comments, undiagnosable complaints, handover gaps;
- every recommendation, proposed refactor, test, budget, route, split or documentation fix, even in prose only;
- every not-verified item (kind `verify`).

A point stated twice in one report is indexed once, under the ranked-gap id, with the other place in `where_report`.
`sub` is a short slug naming the specific thing, so the same thing in other reports and other reviews gets the same
slug: reuse a known issue's slug when it is the same thing. The fingerprint `domain:theme:sub` is built from it. The
first location of a point is its `primary` location (`finding_locations.role`), the others `also`.

## Checking the indexes (5b)

`load-index` checks each file whole before it loads anything: column counts, unique ids, known severities, kinds,
themes, domains, goals and requirements, and that every location parses and exists at the reviewed commit. A refused
file prints the rows and reasons: send it back to its indexer, never guess. Before loading, also compare the counts
with the report's own totals and spot-check a few sections of the report against its index for dropped points.

## Matching

For each finding, find candidate issues: same fingerprint; same domain, theme and sub; an overlapping location (same
path and symbol, or overlapping lines after following renames with `git log --follow`); the issue the report named
(`known_issue`); the same `excerpt_sha256` (identical code). At scale give matching to read-only matcher subagents by
domain, each returning `matches.tsv` (`references/formats/index.md#matches-matchestsv-6-columns`); you check their
work and load it with `ledger.py match R-0001 --file matches.tsv --actor A-0002`.

- Same problem, still there: link it, `still-present`; `changed` if its shape or severity changed (then
  `ledger.py issue-status` for a new severity, with its reason). On a fixed or verified issue these mean it came back:
  use `regression`.
- A fixed or verified issue that is back: `regression` (the issue becomes `regressed`; refused for an issue that was
  not fixed or verified). Put the regression note, which guard test failed to catch it or that there was none, in
  `--rationale` (it is stored as `findings.match_rationale`).
- The same problem closed earlier as obsolete, won't-fix or duplicate, or back after a long gap: `recurrence` (the
  issue reopens; an accepted risk stays accepted, with a `recurred` event).
- A new problem that grows out of an earlier one (a fix that moved the bug, a follow-on): `NEW` with `continuation`
  and the relation `continuation-of:ISS-0007`.
- Otherwise: `NEW`, `new` (the issue takes the finding's title, domain, theme, sub, kind, severity, before state and
  fix). Add `related-to`, `caused-by`, `blocks` or `duplicate-of` relations wherever they hold, with a rationale.
- An issue with a standing decision (accepted risk, won't fix): link it and leave its status; open a `confirm` gate for
  the owner only if something material changed.
- False findings (verification `false`, recorded in Step 4) stay in the ledger and are still matched, so a later review
  that raises them again sees they were checked and why they were wrong. A false finding never regresses an issue,
  moves its `current_state` or counts as a sighting. One that matches no issue gets its own issue, closed as `obsolete`
  with resolution `false finding: <why>`.
- `duplicate-of` and `merged-into` close their active source issue as `duplicate`; the ledger refuses a relation stored
  both ways and any cycle of `blocks`, `duplicate-of` or `merged-into`.
- A wrong match is corrected, never deleted: `references/recipes.md#re-match-a-finding`.

Every known issue of a domain that ran gets a check (`not-checked`, honestly, when the reviewer did not look); a check
that names the finding raising the issue again carries its point id in `known.tsv` column 4
(`review_issue_checks.finding_id`). Then `ledger.py check`: `v_unmatched_findings`, `v_unchecked_known_issues` and
`v_untraceable_refs` must be empty.
