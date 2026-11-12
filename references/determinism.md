# Who decides what

Read this when a step leaves you a choice. Two runs on the same commit agree only when every decision code can make is
made by `ledger.py` or read from the ledger; the rest are judgements, each held to a fixed template, checklist or rule.
`ledger.py compare-runs` measures what is left.

- **tool**: a command decides it; the same inputs give the same output, with no time in the text.
- **data**: a table in the ledger holds the answer; a project changes it there, never in a prompt.
- **judgement**: the agent or the owner decides, inside the constraint named.

| step | decision | by | how |
|------|----------|----|-----|
| Step 0 | Which goals to draft for the interview | judgement | because the project's documents and the owner's words are prose no rule can read; constrained by the sources and themes Step 0 lists, and only approved goals are used (`ledger.py goals`) |
| Step 1 | Which tools and connectors the review can use | tool | `ledger.py env` |
| Step 1 | Which repositories make up the system | judgement | because a system can span folders git does not link; constrained by the owner's answer and by `ledger.py add-repo`, which refuses paths outside the project unless listed |
| Step 1 | What the history says: open, recurring and unguarded issues, the last reviewed commit | data | `v_open_issues`, `v_recurring_issues`, `v_unguarded_fixes`, `v_last_reviewed_commit` |
| 2a | The commits reviewed and whether the tree is dirty | tool | `ledger.py new-review` |
| 2a | Which domains are proposed, and why | tool | `ledger.py scan`, by the rules in `domain_rules` |
| 2a | Dropping or adding a proposed domain | judgement | because the owner's request can narrow or widen the review; constrained by `ledger.py review-domain`, which needs a written reason, and by the owner's approval in 2b |
| 2a | How many reviewers per domain, and the split | judgement | because it depends on how much one reviewer can read; constrained by the split by module and `review_domains.reviewers_planned` |
| 2b | Whether the domains are approved | judgement | because only the owner can say what the review is for; constrained by the `domain-approval` gate, which the ledger requires before any run |
| 2c | Which interview questions to ask | judgement | because good questions come from reading this code; constrained by the catalog's questions per domain (`references/domains.md#interview-questions`), each question saying what prompted it |
| 2c | The wording of each goal | judgement | because a goal summarizes the owner's words; constrained by the owner's approval (`approve-goals`) and the questions it cites |
| 2d | The brief's contents | tool | `ledger.py brief` |
| Step 3 | Each reviewer's prompt | tool | `ledger.py prompt` |
| Step 3 | What each reviewer examines and asks | data | `domain_checks`, the domain's checklist, plus its approved goals in `design_goals` |
| Step 3 | Which pages are screenshotted and what is measured on them | tool | `ledger.py ui-capture`, `ledger.py ui-audit` |
| Step 4 | Whether a report has the required shape and answers its checklist | tool | `ledger.py lint-report` |
| Step 4 | Which findings to verify against the code, and the verdict | judgement | because whether code does what a report claims needs reading it; constrained by the rule to verify the most serious findings of every domain and every `fixed` verdict, and by `ledger.py verify` |
| 5a | How a report's points are indexed | judgement | because reports are prose; constrained by the 16-column format (`references/formats/index.md`) and the indexer brief |
| 5b | Whether an index is complete | judgement | because the report's own totals are prose; constrained by `ledger.py load-index`, which refuses a file with any bad row |
| 5c | Each location's lines and excerpt at the commit | tool | `ledger.py load-index`, `ledger.py fill-refs` |
| 5d | Matching a finding the data decides | tool | `ledger.py match --auto` (one active issue by fingerprint, domain and sub, or symbol) |
| 5d | Matching the rest, and the match type | judgement | because a changed or recurring problem looks different in code; constrained by the match table in SKILL.md and the triggers that refuse a type that does not fit the issue's status |
| 5d | What a match does to the issue's status | data | `match_types`, `issue_statuses` and the triggers on `findings` |
| Step 4 | The severity of a finding that answers a checklist item without one | tool | `ledger.py lint-report`, from `domain_checks.severity` |
| Step 6 | The review report's sections | judgement | because it explains root causes to the owner; constrained by the sections in `references/brief.md#the-review-report` and every number checked against the ledger |
| Step 6 | How far a rerun agrees with an earlier run | tool | `ledger.py compare-runs` |
| Step 7 | Whether the ledger is complete and consistent | tool | `ledger.py check` |
| 8a | Which reviews a plan covers | judgement | because the owner asks for the plan; constrained by `ledger.py new-plan --review` |
| 8b | The plan's phases and steps | judgement | because grouping fixes needs design; constrained by the ordering rules in `references/fix-plan.md` and the drafter brief |
| 8c | Whether every point is covered | tool | `ledger.py check` (`v_uncovered_findings`, `v_coverage_problems`) |
| 8c | Which steps touch the same thing | tool | `ledger.py conflicts` |
| 8c | How each pair of touching steps is resolved | judgement | because whether two changes clash needs reading both; constrained by the four resolutions of `resolve-conflict`, a note for each, and the owner for opposite goals |
| 8d | Whether the plan is approved | judgement | because only the owner can approve it; constrained by the `plan-approval` gate and the refusal while conflicts are open |
| Step 9 | What each fix changes | judgement | because the fix is code; constrained by the step's change and proof, a failing test first, and `report-finding` for anything else found |
| Step 9 | Which steps are stale and which discoveries are unplanned | data | `v_stale_steps`, `v_unplanned_discoveries` |
