# Recipes

Read this when a step points here or something goes wrong. Every block is tested against a demo ledger
(`dev/tests/test_p5_docs_recipes.py`), in this order.

Every write a review or a plan needs is a `ledger.py` command: `ledger.py <command> --help` lists its options, and
`ledger.py X` stands for `python3 <skill folder>/scripts/ledger.py --root <project root> X`. Replace the example codes
(`R-0002`, `ISS-0004`, `P-0002`, step 2, repository `demo`) and the `<...>` values with yours.

Raw SQL is only for the corrections under "Correcting the ledger" that no command covers. Only the manager runs it,
one block at a time, keeping the `BEGIN IMMEDIATE` / `COMMIT` lines, with:

    python3 -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1], timeout=30); c.execute('PRAGMA foreign_keys = ON'); c.executescript(sys.stdin.read())" .code-review/ledger.sqlite < recipe.sql

(with `CODE_REVIEW_DIR` set, the ledger is `$CODE_REVIEW_DIR/ledger.sqlite`). A refused statement prints the ledger's
reason and nothing of the block is written. Run `ledger.py check` afterwards.
`(SELECT id FROM actors WHERE kind='manager' ORDER BY id DESC LIMIT 1)` is the current manager; with several managers
in one ledger, write the manager's actor id instead.

## Setting up a review

### Add a goal a document states

Goals come from the owner interview (SKILL.md Step 2c). One that a document or the owner already states takes
`--source` in place of `--from`, and the owner approves it like any other:

```sh
ledger.py propose-goal --source 'README.md, section 2' 'One validator per input, shared by the server and the client.'
ledger.py approve-goals G-03 --actor A-0001
```

### Add a requirement

`--source` is required: a document path, an issue URL, or `owner, chat <date>`; `--location` says where in it.

```sh
ledger.py add-requirement 'A signed-out visitor sees the sign-in page, never a blank page.' --source docs/spec.md --location 'section 2.1' --review R-0002
```

### Add a custom domain

A domain the catalog lacks gets `category` `custom`, and a theme when the seeded themes do not fit:

```sh
ledger.py add-domain payments 'Payments flow' --description 'Applies when the system takes or refunds payments.' --report-file payments.md --theme payments=Payments
```

### Change the domain list

After the owner's answer to the `domain-approval` gate (before you apply it), record each change with its
`owner_change`: `added-by-owner`, `removed-by-owner` or `modified-by-owner`. A domain that has reviewer runs cannot
be skipped.

```sh
ledger.py review-domain R-0002 database include --reason 'the owner wants the data model reviewed' --owner-change added-by-owner
ledger.py review-domain R-0002 payments include --reason 'added by the owner' --owner-change added-by-owner --reviewers 1
```

### Several repositories

One ledger per system, in the primary repository or in a parent folder holding them all (`ledger.py --root <parent>
init`). Add each repository with its path relative to that root:

    ledger.py add-repo web web --remote https://github.com/o/web --permalink-template 'https://github.com/o/web/blob/{sha}/{path}#L{start}-L{end}'
    ledger.py add-repo api services/api

`ledger.py` reads only repositories at or below the root. A sibling checkout outside it is read only when the owner
lists it explicitly: `add-repo shared ../shared --outside-root`, after a `decision` gate saying so.

## During a review

### Record a test run

`--purpose` is one of `baseline`, `targeted`, `flake-check`, `mutation`, `before-fix`, `after-fix`, `ci`. The commit
is the repository's `HEAD` unless `--commit` names another. A run for a plan step takes `--step 12` in place of
`--review`.

```sh
ledger.py record-test-run --purpose baseline --command 'python3 -m unittest' --review R-0002 --passed 120 --skipped 2 --exit-code 0 --duration-ms 5400 --note 'two tests skipped: no network'
```

### Finish a reviewer run

When a report arrives, give its path inside the review folder and the counts it states; `--status` is `finished`
(the default), `failed` or `cancelled`. The command computes the file's sha256 for `report_sha256`, stores the path as
`report_path` and records the report as an artifact.

```sh
ledger.py finish-run R-0002 security-1 --report .code-review/reviews/R-0002/security.md --items 4 --high 1 --message 'Reviewed 4 sign-in paths; 1 high.'
```

### Record a verification

After loading (Step 5c), for each finding you checked: `confirmed`, `partly`, `false` or `not-checkable` (it starts
`unverified`), with a note saying what you saw. A `false` finding is still matched in 5d.

```sh
ledger.py verify R-0002:SEC-M1 confirmed --note 'read src/auth.py:3-5 at the reviewed commit'
```

### Record a metric

Keep metric names stable between reviews (`mutation_score`, `line_coverage`, `test_cases_asserted`, `page_weight`,
`queries_per_page`, ...: `ledger.py query "SELECT * FROM metric_defs"`); a new name is added to `metric_defs`
automatically. `--method` is `measured`, `counted` or `estimated`.

```sh
ledger.py add-metric R-0002 test_cases_asserted 7 --method counted --domain security --repo demo --scope src/auth.py --unit cases --note 'from the security report, table 2'
```

### Record an artifact

Briefs, reports, indexes and the review report are kept inside the ledger's folder, in `.code-review/reviews/R-0002/`
(copy them there first; never record a path in a scratch folder or elsewhere in the repository). The kind is `brief`,
`report`, `index`, `summary-doc`, `plan-file`, `progress-page`, `probe-script` or `other`; `--run security-1` names the
reviewer run a file belongs to. `finish-run` has already recorded each report.

```sh
ledger.py add-artifact brief --path .code-review/reviews/R-0002/BRIEF.md --review R-0002
```

### Set a review's verdict

```sh
ledger.py set-verdict R-0002 --verdict 'Usable, with one high-severity sign-in weakness to fix first.' --summary 'Two domains, five points, one confirmed high.'
```

### Confirm guard tests

Each review checks the guard tests of fixed issues (the test-quality reviewer, or you). One that is still there and
still guards its issue (`still_exists`, `last_confirmed_review_id`):

```sh
ledger.py confirm-guard R-0002 ISS-0001
```

One that is gone takes `--gone` (and `--test PATH::NAME` when the issue has several); `v_unguarded_fixes` then lists
its issue.

### Record that tags were pushed

Only when the ledger-location decision lets tags be pushed, after `git push origin review/R-0001` (sets `tag_pushed`):

```sh
ledger.py tag-review R-0001 --pushed
```

## The plan

### Create a plan

One `--review` per review the plan covers. Then load the drafted steps (`references/fix-plan.md`); until they are
loaded, `check` lists the reviews' findings as uncovered.

```sh
ledger.py new-plan --title 'Fix plan for R-0002' --review R-0002
ledger.py import-steps P-0002 steps.jsonl
```

### Move a plan's status

`drafting`, `awaiting-approval`, `approved` (only once its `plan-approval` gate is applied), `in-progress` (at the
first step), `complete` (every step done or dropped), `superseded` (with `--superseded-by P-0003`) or `abandoned`.
Every change takes `--note`.

```sh
ledger.py plan-status P-0002 awaiting-approval --note 'the steps are ready for the owner'
ledger.py open-gate plan-approval --plan P-0002 --title 'Approve the fix plan' --question 'Start work in this order?' --option approve='Approve the plan' --option change='Change it first'
ledger.py answer-gate GATE-0003 --value approve --text 'Go ahead.'
ledger.py apply-gate GATE-0003 --note 'plan approved; work starts with phase 1'
ledger.py plan-status P-0002 approved --note 'the owner approved the plan (GATE-0003)' --approved-by A-0001
ledger.py issue-status ISS-0004 planned --note 'in P-0002 step 1'
```

### Record a step's commit

`step-status 2 done --commit <sha>` records the commit with the status. For a further commit, or a pull request:

```sh
ledger.py add-commit P-0001#2 <full commit sha> --pr https://example.com/o/demo/pull/7
```

The branch and the summary come from git. If a step's own commit cannot name itself, record it with the next commit.

### Record a guard test

The test that would fail if the fixed issue came back; `--failed-before` when you saw it fail without the fix.
`step-status 2 done --guard PATH::NAME` records one for each issue of the step.

```sh
ledger.py add-guard ISS-0004 tests/test_auth.py test_sign_in_code_is_unpredictable --failed-before --step 2 --plan P-0001
```

### Record the hub address

After publishing the hub page (`progress-page`) somewhere, record its address; this also sets the plan's `page_url`.

```sh
ledger.py add-artifact progress-page --plan P-0001 --url https://example.com/hub
```

A document the owner keeps elsewhere and asks to link is an artifact too:
`ledger.py add-artifact plan-file --plan P-0001 --url <address>`.

## Correcting the ledger

History is never deleted; every correction is a new row or a change with its reason. The skill uses hand-written SQL
only in these blocks.

### Re-match a finding

Point it at the right issue; the ledger recounts both issues and logs an `unlinked` event on the old one.

```sql
BEGIN IMMEDIATE;
UPDATE findings SET issue_id = (SELECT id FROM issues WHERE code = 'ISS-0003'), match_type = 'still-present',
       match_confidence = 'likely', match_rationale = 'same hardcoded limit, matched to ISS-0004 by mistake'
 WHERE review_id = (SELECT id FROM reviews WHERE code = 'R-0002') AND point_id = 'SEC-M1';
COMMIT;
```

### Undo an automatic regression

A wrong `regression` match left an issue `regressed`. Re-match the finding as above, then put the issue back with its
reason:

```sh
ledger.py issue-status ISS-0001 fixed --note 'the regression match was wrong: SEC-M1 is another problem' --after 'Sign-in codes come from secrets.token_hex.'
```

(An issue goes back to `verified` only through a later review's `fixed` check.)

### Mark a false finding

`ledger.py verify R-0002:SEC-M1 false --note '<why it is false>'`, then match it as usual. With no existing issue,
`match R-0002 SEC-M1 new --type new` creates one closed as `obsolete` with resolution `false finding: <why>`. The
issue of a false finding marked fixed by mistake: `issue-status ISS-0005 obsolete --note 'false finding: <why>'`.

### Retire a goal

A goal the owner drops is retired, never deleted (one that another replaces also sets `superseded_by`):

```sql
BEGIN IMMEDIATE;
UPDATE design_goals SET retired_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE code = 'G-02';
COMMIT;
```

### Withdraw or change a gate

A gate nobody needs is withdrawn (only an open or answered one). To ask again, open a new gate; or reopen
an answered one by clearing both answer columns (the cleared answer stays in `events`).

```sql
BEGIN IMMEDIATE;
UPDATE gates SET status = 'withdrawn',
       change_actor_id = (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1)
 WHERE code = 'GATE-0001' AND status IN ('open', 'answered');
COMMIT;
```

Check that it changed one row (`ledger.py gates`). To reopen instead:
`UPDATE gates SET status = 'open', answer_value = NULL, answer_text = NULL WHERE code = 'GATE-0002' AND status = 'answered';`

### A review started by mistake

It is abandoned and stays in history: `ledger.py review-status R-0003 abandoned --note 'started by mistake'`. Try the
skill on a throwaway ledger: `ledger.py --db /tmp/trial.sqlite init`, then `--db /tmp/trial.sqlite` on every command.

### Redact a secret

`redact` replaces the secret with `[redacted]` in the named field and in every copy of it elsewhere in the ledger, and
logs an event with your reason (never the value). `--only` limits it to the secret inside a longer text; without it
the whole field is replaced. Then write the dump again:

```sh
ledger.py redact findings.summary R-0001:SEC-H1 --only '<the secret value>' --reason 'an API key in the summary'
ledger.py dump
```

Delete every older copy that still holds it (`ledger.sql` copies, `ledger.sqlite.bak*`). Tell the owner to rotate
the secret: one that reached git stays in its history until the history is rewritten.

## When something goes wrong

### A failing check

`ledger.py check` names each failing check with its rows; read a view with `ledger.py query "SELECT * FROM <view>"`.

#### `v_unmatched_findings`

Match each finding (Step 5d), false ones included.

#### `v_unchecked_known_issues`

Add the known issue to its report's `known.tsv` (`not-checked` if nobody looked) and run `import-checks`.

#### `v_untraceable_refs`

Run `tag-review` and `fill-refs`. A symbol they could not place needs lines: a new point with the right location,
or `report-finding` for a discovery.

#### `v_uncovered_findings`

Add the finding to the `closes` of a step in its plan.

#### `v_coverage_problems`

Give a step that closes nothing a `no_findings_reason`; give a finding closed by several steps a `multi_reason`.

#### `v_unplanned_discoveries`

Plan the discovery: `report-finding --into-step`, a new step, or the owner's gate.

#### `v_history_gaps`

An issue or gate changed without its event, so something wrote around the triggers: tell the owner.

#### `v_match_problems`

Re-match the finding: its match type disagrees with its issue.

#### `v_off_plan_coverage`

Add the finding's review to `plan_reviews`, or move the point to a step of a plan that covers it.

#### `v_done_steps_open_issues`

Run `issue-status ... fixed` for each issue of the done step, or say in the step's note why it stays open.

#### `v_closed_without_reason`

Run `issue-status` again with `--resolution` on the `accepted-risk` or `wont-fix` issue.

#### `v_relation_problems`

Add the right relation, and close or reopen the issue with a note.

#### `v_check_finding_owner`

Record the check again in the review its finding belongs to.

#### `v_unguarded_fixes`

A warning: record one with `add-guard` for each fixed issue listed.

#### `v_artifacts_outside_ledger`

A warning: copy the brief, report or index into `.code-review/reviews/R-0002/` and record it there with
`add-artifact`. `check` also names a recorded file that is missing from disk.

#### `quick_check`

The file is damaged. Stop writing. `ledger.py restore .code-review/ledger.sql --to /tmp/check.sqlite` rebuilds the last
dump; compare the two, and ask the owner before replacing the ledger.

#### `integrity_check`

`check --full` runs it. Act as for `quick_check`.

#### `foreign_key_check`

A row points at a row that is missing. Act as for `quick_check`.

#### `check_schema`

A table, view or trigger differs from the shipped schema. Open a gate, and do not run the project copy of `ledger.py`.

### Merge conflicts and backups

After a git merge conflict on `ledger.sqlite` (only when the owner decided to commit it): keep this side, save the
other and merge it in; then dump.

    git checkout --ours .code-review/ledger.sqlite
    git show MERGE_HEAD:.code-review/ledger.sqlite > /tmp/theirs.sqlite
    ledger.py merge /tmp/theirs.sqlite
    ledger.py dump

`merge` prints codes it renumbered; text that mentions them (notes, tags) is not rewritten. Back up by running `dump`
after each writing session, and copy `ledger.sqlite` before `migrate` or `merge` (both
also keep their own backups). Expect about 9 MB of ledger and 7 MB of dump per 1,000 findings (measured at 10,000
findings: 94 MB and 66 MB).
