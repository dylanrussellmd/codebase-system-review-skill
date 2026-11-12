# The fix plan

Read this before Step 8. Every point of the plan's reviews is closed by a step: plan from the ledger and prove
coverage with its views. Under ~150 points, skip the parallel drafting and write `steps.jsonl` yourself.

## Create the plan (8a)

Create the plan over its reviews with `ledger.py new-plan --title '...' --review R-0001` (status `drafting`). A plan
may cover several reviews (one `--review` each) and may take over still-open steps of an older plan; that plan then
becomes `superseded` (`ledger.py plan-status P-0001 superseded --superseded-by P-0002 --note '...'`). Write the
plan's narrative (how to read it, why this order, what needs the owner, how it was built, what was not checked) to
`references/writing.md`, in `.code-review/plans/P-0001.md`, and store it with
`ledger.py set-narrative P-0001 .code-review/plans/P-0001.md`, again whenever it changes. The hub shows it on the plan
page; there is no separate plan document.

## Draft in parallel and merge (8b)

At full-review scale (about 2,000 points) one pass is too large:

- Fix the global phases first, each with a one-sentence goal and why it sits there, and write them into a drafting
  brief: safety net; fix first; spec mismatches; contracts and validation; tests; decision placement; backend
  foundations, resilience and compatibility; one client API and access; shared front end; logging and monitoring;
  performance; generic UI; usability and task flows; operations and recovery; documentation and handover; the last
  phase reserved for a second review.
- Split the points into about a dozen clusters of related themes (so the same fix described by several reports lands
  with one drafter) and give each cluster to one read-only drafter as an export:
  `ledger.py query --json "SELECT r.code AS review, f.point_id, f.severity, f.domain_code, f.theme_code, f.summary,
  f.recommendation, f.recommended_test, i.code AS issue FROM findings f JOIN reviews r ON r.id = f.review_id LEFT JOIN
  issues i ON i.id = f.issue_id WHERE f.theme_code IN ('validation', 'contracts')"`.
- Record each drafter: `ledger.py add-actor drafter 'drafter 1' --model <id> --parent A-0002 --review R-0001 --role
  'drafter for validation and contracts'`.
- A drafter writes JSON lines in the format of `references/formats/plan-steps.md`:
  - `phase` is the phase number (it becomes the step's phase, `steps.phase_id`), assigned by what the step IS, not by
    its cluster; `order` is the order inside the phase (steps are numbered globally after loading, in phase, order and
    line order: `steps.number`);
  - `closes` lists the points the step closes, **always review-qualified** as `R-0001:SEC-H3` when the plan covers more
    than one review (a point id is unique only within its review; `import-steps` refuses an id found in two reviews);
  - `owner_need` is one of `none`, `decision`, `confirm`, `review`, `production`, with `owner_question` for anything
    but `none`; such a step asks through a gate of kind `decision`, `confirm`, `other` or `production-action`;
  - the step's `change` names the approved design goals it serves (`G-01`), read with `ledger.py goals` (drafters
    run it themselves); a point that conflicts with an approved goal is not planned around: list it for the manager,
    who asks the owner (a `decision` gate) before the step is written;
  - `behavior_change` true marks a step that changes behavior on purpose, with `behavior_note`;
  - the drafter shows every point of its cluster closed before handing back.
- Merge: read every step title phase by phase; fold steps that are the same fix into one (union of points, both texts
  kept); move steps whose dependency sits in a later phase; put verification and pinning tests first inside a phase.
  Then load: `ledger.py import-steps P-0001 steps.jsonl --actor A-0002` (the whole file is checked first; a dependency
  may point at any step of the plan, never in a cycle). It prints the points still uncovered and the coverage problems.
- Count the steps that need the owner by kind and the steps that change behavior on purpose, for the narrative.

## Ordering rules

1. Test infrastructure and safety nets first (CI runs every suite, date-independent tests, test doubles that enforce
   real limits, enough logging to see what later changes do), so every later step is checked.
2. Confirmed defects, regressions, spec mismatches and security weaknesses next, each with a test that fails before the
   fix. A spec mismatch where it is unclear whether the code or the requirement is right is an owner decision first.
3. Foundations before the things built on them: validation and the shared contract before tests that rely on them;
   one source of truth (access, approvals, decisions in their owning layer) before the UI that shows it;
   characterization tests before any structural move.
4. A step that must wait for another names it in `depends`, whatever the numbers; work goes in `v_step_order`'s order.
   Steps that change behavior on purpose are marked for the owner's eye.
5. Not-verified items become explicit verification steps, placed before the work that depends on the answer.
6. Tests for untested units go group by group, the owner's most important or most used areas first.
7. Recurring issues and regressions get a guard test in the same step as their fix.
8. Documentation steps follow the code steps they describe, so docs are written once for the final behavior; but a
   doc that is wrong today in a way that could mislead is corrected early.
9. The last phase is a second full review (Steps 0-7, with the domain list approved again) and then the owner's own
   review; its step has `no_findings_reason`.

## Prove coverage (8c)

`ledger.py check` must pass: `v_uncovered_findings` empty (every finding of the plan's reviews closed by a step) and
`v_coverage_problems` empty (no step closing nothing without `no_findings_reason`; a finding closed by several steps
only with a `multi_reason`). Points the plan deliberately does not fix (the report itself calls it acceptable) still
get a step that records the decision and its reason, with a `confirm` gate. False findings are closed by one step
"Record false findings" that lists them, with a `confirm-false-finding` gate. Report totals in the narrative: points per
review and domain, steps per phase, zero uncovered.

## Deconflict the steps (8c)

Before approval, check that no two steps undo, contradict or trip over each other:

    ledger.py conflicts --plan P-0001 --actor A-0002

It records each step's touches (the `touches` a drafter declared, and the file and symbol of every point the step
closes) and lists every pair of steps that touch the same target, saying when they change it in different directions
(one tightens a rule the other loosens). Judge each pair, or give the list to a read-only reviewer who returns a
verdict per pair, then record it:

    ledger.py resolve-conflict P-0001 12 14 --resolution order --note 'step 14 builds on the check step 12 adds' --actor A-0002

- `no-conflict`: they touch the same file but not the same thing; say what each changes.
- `order`: one must land first; the later step now depends on the earlier.
- `merge`: one change does both; rewrite the steps so one carries it, and say which.
- `owner`: the steps want opposite things; open a `decision` gate, apply the owner's answer, and pass `--gate`.

`check` fails while `v_unresolved_conflicts` has rows, and `plan-status ... approved` is refused. A step added after
approval (`report-finding --new-step`) is checked at once: its conflicts are a warning in `check` and a line in
`ledger.py gates`, and only that step cannot start until each pair is judged.

## Approval (8d)

Check every number and step range in the narrative against the ledger, then:

    ledger.py open-gate plan-approval --plan P-0001 --title 'Approve the fix plan' --question 'Start work in this order?' \
        --option approve='Approve the plan' --option change='Change it first' --actor A-0002

Move the plan to `awaiting-approval` (`ledger.py plan-status P-0001 awaiting-approval --note '...'`), publish the hub,
and tell the owner the steps are in the hub's Fix plans. Start nothing until the owner approves; then `apply-gate`,
`plan-status P-0001 approved --approved-by A-0001 --note '...'`, and each of its issues `issue-status ISS-0001 planned`.
