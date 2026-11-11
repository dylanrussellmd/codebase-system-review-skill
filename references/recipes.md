# Recipes

Read this when a step points here or something goes wrong. Each recipe is tested against a demo ledger
(`dev/tests/test_p5_docs_recipes.py`), in this order.

Most writes have a `ledger.py` command (see SKILL.md and `ledger.py <command> --help`); prefer it. The writes below
have none yet, so they are SQL. Replace the example codes (`R-0002`, `ISS-0003`, `P-0001`, step 2, repository `demo`)
and the `<...>` values with yours, keep the `BEGIN IMMEDIATE` / `COMMIT` lines, and run a block with:

    python3 -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1], timeout=30); c.execute('PRAGMA foreign_keys = ON'); c.executescript(sys.stdin.read())" .code-review/ledger.sqlite < recipe.sql

(with `CODE_REVIEW_DIR` set, the ledger is `$CODE_REVIEW_DIR/ledger.sqlite`). A refused statement prints the
ledger's reason and nothing of the block is written. Only the manager runs recipes, one block at a time, and runs
`ledger.py check` afterwards (after creating a plan, once `import-steps` has loaded its steps: a plan with no steps
leaves its reviews' findings uncovered). `(SELECT id FROM actors WHERE kind='manager' ORDER BY id DESC LIMIT 1)` is the current
manager; with several managers in one ledger, write the manager's actor id instead. In the `sh` blocks,
`ledger.py X` is the skill's `python3 <skill folder>/scripts/ledger.py --root <project root> X`.

## Setting up a review

### Add a design goal

Goals come from the owner interview (SKILL.md Step 2c): `ledger.py propose-goal --domain <D> --from IQ-0001
'<statement>'`, approved with `ledger.py approve-goals G-03 --actor <owner>`. Only where those commands are not in your
copy of the tool yet, add one by hand:

```sql
BEGIN IMMEDIATE;
INSERT INTO design_goals(code, text, source)
  VALUES ((SELECT next_code FROM v_next_codes WHERE prefix = 'G'),
          'One validator per input, shared by the server and the client.', 'owner, chat 2026-11-11');
COMMIT;
```

and link it to a review that exists:

```sql
BEGIN IMMEDIATE;
INSERT INTO review_goals(review_id, goal_id)
  VALUES ((SELECT id FROM reviews WHERE code = 'R-0002'), (SELECT id FROM design_goals WHERE code = 'G-03'));
COMMIT;
```

A goal the owner drops is retired, never deleted: `UPDATE design_goals SET retired_at =
strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE code = 'G-01';` (a replaced one also sets `superseded_by`).

### Add a requirement

`source` is required: a document path, an issue URL, or `owner, chat <date>`; `source_location` says where in it.

```sql
BEGIN IMMEDIATE;
INSERT INTO requirements(code, text, source, source_location, first_review_id)
  VALUES ((SELECT next_code FROM v_next_codes WHERE prefix = 'REQ'),
          'A signed-out visitor sees the sign-in page, never a blank page.', 'docs/spec.md', 'section 2.1',
          (SELECT id FROM reviews WHERE code = 'R-0002'));
COMMIT;
```

### Add a custom domain

A domain the catalog lacks has category `custom` (and a theme, when the seeded themes do not fit):

```sql
BEGIN IMMEDIATE;
INSERT INTO domains(code, name, category, description, report_file)
  VALUES ('payments', 'Payments flow', 'custom', 'Applies when the system takes or refunds payments.', 'payments.md');
INSERT INTO themes(code, name) VALUES ('payments', 'Payments');
COMMIT;
```

### Change the domain list

After the owner's answer to the `domain-approval` gate (before you apply it), record each change with its
`owner_change`: `added-by-owner`, `removed-by-owner` or `modified-by-owner`.

```sql
BEGIN IMMEDIATE;
UPDATE review_domains SET decision = 'included', reason = 'the owner wants the data model reviewed',
       owner_change = 'added-by-owner'
 WHERE review_id = (SELECT id FROM reviews WHERE code = 'R-0002') AND domain_code = 'database';
INSERT INTO review_domains(review_id, domain_code, decision, reason, owner_change, reviewers_planned)
  VALUES ((SELECT id FROM reviews WHERE code = 'R-0002'), 'payments', 'included', 'added by the owner',
          'added-by-owner', 1);
COMMIT;
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

`purpose` is one of `baseline`, `targeted`, `flake-check`, `mutation`, `before-fix`, `after-fix`, `ci`. A run for a
plan step sets `step_id` instead of (or with) `review_id`.

```sql
BEGIN IMMEDIATE;
INSERT INTO test_runs(review_id, repository_id, commit_sha, command, purpose, passed, failed, skipped, exit_code,
                      duration_ms, notes)
  VALUES ((SELECT id FROM reviews WHERE code = 'R-0002'), (SELECT id FROM repositories WHERE name = 'demo'),
          '<full commit sha>', 'python3 -m unittest', 'baseline', 120, 0, 2, 0, 5400, 'two tests skipped: no network');
COMMIT;
```

### Finish a reviewer run

When a report arrives: its path inside the review folder, its sha256 (`sha256sum <file>`), the counts it states, and
the status (`finished`, `failed` or `cancelled`).

```sql
BEGIN IMMEDIATE;
UPDATE reviewer_runs SET status = 'finished', report_path = '.code-review/reviews/R-0002/security.md',
       report_sha256 = '<sha256 of the report>', items_reviewed = 4, high_count = 1, medium_count = 0, low_count = 0,
       final_message = 'Reviewed 4 sign-in paths; 1 high.', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
 WHERE review_id = (SELECT id FROM reviews WHERE code = 'R-0002') AND label = 'security';
COMMIT;
```

### Record a verification

After loading (Step 5c), for each finding you checked: `verification` is `confirmed`, `partly`, `false` or
`not-checkable` (it starts `unverified`), with a note saying what you saw. A `false` finding is still matched in 5d.

```sql
BEGIN IMMEDIATE;
UPDATE findings SET verification = 'confirmed', verification_note = 'read src/auth.py:3-5 at the reviewed commit',
       verified_by_actor_id = (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1),
       verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
 WHERE review_id = (SELECT id FROM reviews WHERE code = 'R-0002') AND point_id = 'SEC-M1';
COMMIT;
```

### Record a metric

Keep metric names stable between reviews (`mutation_score`, `line_coverage`, `test_cases_asserted`, `page_weight`,
`queries_per_page`, ...: `ledger.py query "SELECT * FROM metric_defs"`); a new name is added to `metric_defs`
automatically. `method` is `measured`, `counted` or `estimated`.

```sql
BEGIN IMMEDIATE;
INSERT INTO metrics(review_id, domain_code, repository_id, name, scope, value, unit, method, note)
  VALUES ((SELECT id FROM reviews WHERE code = 'R-0002'), 'security', (SELECT id FROM repositories WHERE name = 'demo'),
          'test_cases_asserted', 'src/auth.py', 7, 'cases', 'counted', 'from the security report, table 2');
COMMIT;
```

### Record an artifact

Briefs, reports, indexes and the review report are kept inside the ledger's folder, in `.code-review/reviews/R-0002/`
(copy them there first; never record a path in a scratch folder or elsewhere in the repository). `kind` is `brief`,
`report`, `index`, `summary-doc`, `plan-file`, `progress-page`, `probe-script` or `other`; a report also names its
run.

```sql
BEGIN IMMEDIATE;
INSERT INTO artifacts(review_id, reviewer_run_id, kind, path, sha256)
  VALUES ((SELECT id FROM reviews WHERE code = 'R-0002'),
          (SELECT id FROM reviewer_runs WHERE label = 'security'
              AND review_id = (SELECT id FROM reviews WHERE code = 'R-0002')),
          'report', '.code-review/reviews/R-0002/security.md', '<sha256 of the report>');
COMMIT;
```

### Set a review's verdict

```sql
BEGIN IMMEDIATE;
UPDATE reviews SET verdict = 'Usable, with one high-severity sign-in weakness to fix first.',
       summary = 'Two domains, five points, one confirmed high.',
       change_actor_id = (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1)
 WHERE code = 'R-0002';
COMMIT;
```

### Confirm guard tests

Each review checks the guard tests of fixed issues (the test-quality reviewer, or you): still there and still
guarding, or gone.

```sql
BEGIN IMMEDIATE;
UPDATE guard_tests SET still_exists = 1, last_confirmed_review_id = (SELECT id FROM reviews WHERE code = 'R-0002')
 WHERE issue_id IN (SELECT id FROM issues WHERE code IN ('ISS-0001'));
COMMIT;
```

A test that is gone: `still_exists = 0` (then `v_unguarded_fixes` lists its issue).

### Record that tags were pushed

Only when the ledger-location decision lets tags be pushed, after `git push origin review/R-0001`:

```sql
BEGIN IMMEDIATE;
UPDATE review_repositories SET tag_pushed = 1
 WHERE review_id = (SELECT id FROM reviews WHERE code = 'R-0001') AND tag_name IS NOT NULL;
INSERT INTO events(entity_type, entity_id, event, to_value, note, actor_id, review_id)
  VALUES ('review', (SELECT id FROM reviews WHERE code = 'R-0001'), 'tag-pushed', 'review/R-0001',
          'git push origin review/R-0001', (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1),
          (SELECT id FROM reviews WHERE code = 'R-0001'));
COMMIT;
```

## The plan

### Create a plan

```sql
BEGIN IMMEDIATE;
INSERT INTO plans(code, title, change_actor_id)
  VALUES ((SELECT next_code FROM v_next_codes WHERE prefix = 'P'), 'Fix plan for R-0002',
          (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1));
INSERT INTO plan_reviews(plan_id, review_id)
  VALUES ((SELECT max(id) FROM plans), (SELECT id FROM reviews WHERE code = 'R-0002'));
COMMIT;
```

Add one `plan_reviews` row per review the plan covers. Then load the drafted steps (`references/fix-plan.md`):

```sh
ledger.py import-steps P-0002 steps.jsonl
```

### Move a plan's status

`drafting`, `awaiting-approval`, `approved` (with `approved_at` and the approver, only once its `plan-approval` gate
is applied), `in-progress` (at the first step), `complete` (every step done or dropped), or `superseded` /
`abandoned` (with a note; a superseded plan names its successor). Each change takes `change_actor_id` and
`change_note`.

```sql
BEGIN IMMEDIATE;
UPDATE plans SET status = 'awaiting-approval', change_note = 'the steps are ready for the owner',
       change_actor_id = (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1)
 WHERE code = 'P-0002';
COMMIT;
```

Approval goes through the owner's gate first:

```sh
ledger.py open-gate plan-approval --plan P-0002 --title 'Approve the fix plan' --question 'Start work in this order?' --option approve='Approve the plan' --option change='Change it first'
ledger.py answer-gate GATE-0003 --value approve --text 'Go ahead.'
ledger.py apply-gate GATE-0003 --note 'plan approved; work starts with phase 1'
```

```sql
BEGIN IMMEDIATE;
UPDATE plans SET status = 'approved', approved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
       approved_by_actor_id = (SELECT id FROM actors WHERE kind = 'owner' ORDER BY id LIMIT 1),
       change_note = 'the owner approved the plan (GATE-0003)',
       change_actor_id = (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1)
 WHERE code = 'P-0002';
COMMIT;
```

The plan's open issues then go to `planned` (`ledger.py issue-status ISS-0004 planned --note 'in P-0002 step 1'`), and
at the first step the plan goes `in-progress` the same way (`status = 'in-progress'`), at the end `complete`.

### Record a step's commit

```sql
BEGIN IMMEDIATE;
INSERT INTO step_commits(step_id, repository_id, commit_sha, branch, summary, committed_at)
  VALUES ((SELECT s.id FROM steps s JOIN plans p ON p.id = s.plan_id WHERE p.code = 'P-0001' AND s.number = 2),
          (SELECT id FROM repositories WHERE name = 'demo'), '<full commit sha>', 'fix/sign-in-codes',
          'Sign-in codes from secrets.token_hex', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
COMMIT;
```

`pr_url` takes the pull request's `https://` address when there is one. If a step's own commit cannot name itself,
record it with the next commit.

### Record a guard test

The test that would fail if the fixed issue came back, with whether it failed before the fix and passes after:

```sql
BEGIN IMMEDIATE;
INSERT INTO guard_tests(issue_id, repository_id, test_path, test_name, step_id, added_commit_sha, failed_before_fix,
                        passes_after_fix)
  VALUES ((SELECT id FROM issues WHERE code = 'ISS-0004'), (SELECT id FROM repositories WHERE name = 'demo'),
          'tests/test_auth.py', 'test_sign_in_code_is_unpredictable',
          (SELECT s.id FROM steps s JOIN plans p ON p.id = s.plan_id WHERE p.code = 'P-0001' AND s.number = 2),
          '<full commit sha>', 1, 1);
COMMIT;
```

### Record the hub address

After publishing the hub page (`progress-page`) somewhere, keep its address on the plan and as an artifact:

```sql
BEGIN IMMEDIATE;
UPDATE plans SET page_url = 'https://example.com/hub' WHERE code = 'P-0001';
INSERT INTO artifacts(plan_id, kind, path, url)
  VALUES ((SELECT id FROM plans WHERE code = 'P-0001'), 'progress-page', '.code-review/hub.html',
          'https://example.com/hub');
COMMIT;
```

`plans.doc_url` is only for a document the owner keeps elsewhere and asks to link.

## Correcting the ledger

History is never deleted; every correction is a new row or a change with its reason.

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

Set its verification (as in "Record a verification") to `false` with the reason in `verification_note`, then match it
as usual. With no existing issue, `match R-0002 SEC-M1 new --type new` creates one closed as `obsolete` with
resolution `false finding: <why>`. A false finding marked fixed by mistake is corrected the same way: its issue
`issue-status ISS-0005 obsolete --note 'false finding: <why>'`.

### Withdraw or change a gate

A gate that is no longer needed is withdrawn (only an open or answered one). To ask again, open a new gate; or reopen
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

### Remove a test review

There is no command to delete a review (`drop-review` is planned). Try the skill on a throwaway ledger instead:
`ledger.py --db /tmp/trial.sqlite init`, then pass `--db /tmp/trial.sqlite` to every command. A review started by
mistake in the real ledger is abandoned: `ledger.py review-status R-0003 abandoned --note 'started by mistake'`.

### Redact a secret

Finding text and events cannot be edited in place, so a secret that reached the ledger is removed through the dump:

    ledger.py dump
    python3 -c "import sys; p = sys.argv[1]; t = open(p, encoding='utf-8').read(); open('/tmp/redacted.sql', 'w', encoding='utf-8').write(t.replace(sys.argv[2], '[redacted]'))" .code-review/ledger.sql '<the secret value>'
    ledger.py restore /tmp/redacted.sql --to /tmp/redacted.sqlite
    cp .code-review/ledger.sqlite .code-review/ledger.sqlite.bak-redact
    cp /tmp/redacted.sqlite .code-review/ledger.sqlite
    ledger.py check

Choose the replaced text so it occurs only where the secret is (the whole value, never a common word); `restore`
refuses a dump that does not load and `check` must pass. Then record what was redacted and why (never the value),
delete the backup and `/tmp/redacted.*`, and run `dump` again:

```sql
BEGIN IMMEDIATE;
INSERT INTO events(entity_type, entity_id, event, note, actor_id)
  VALUES ('finding', (SELECT id FROM findings WHERE point_id = 'SEC-H1' ORDER BY id LIMIT 1), 'redacted',
          'an API key value in the summary and its excerpt replaced by [redacted]: a secret',
          (SELECT id FROM actors WHERE kind = 'manager' ORDER BY id DESC LIMIT 1));
COMMIT;
```

Tell the owner: a secret already committed stays in git history until the history is rewritten, and must be rotated.

## When something goes wrong

### A failing check

`ledger.py check` names each failing view with its rows; read them with `ledger.py query "SELECT * FROM <view>"`.

- `v_unmatched_findings`: findings with no issue: match them (Step 5d), false ones included.
- `v_unchecked_known_issues`: a known issue of a domain that ran has no check: add it to the report's `known.tsv`
  (`not-checked` if nobody looked) and run `import-checks`.
- `v_untraceable_refs`: a location without its commit, tag or excerpt: run `tag-review` and `fill-refs`; give lines to
  a symbol it could not place (a new point with the right location, or report-finding for a discovery).
- `v_uncovered_findings`: a finding of the plan's reviews that no step closes: add it to a step's `closes`.
- `v_coverage_problems`: a step that closes nothing (give it `no_findings_reason`) or a finding closed twice without a
  `multi_reason`.
- `v_unplanned_discoveries`: a discovery whose issue is open and no step closes: `report-finding --into-step` or a new
  step, or the owner's gate.
- `v_history_gaps`: an issue or gate change without its event: it was written around the triggers; tell the owner.
- `v_match_problems`: a match type that disagrees with its issue: re-match the finding.
- `v_off_plan_coverage`: a step closing a finding of a review the plan does not cover: add the review to
  `plan_reviews` or move the point.
- `v_done_steps_open_issues`: a done step whose issues are still open: `issue-status ... fixed`, or the step's note
  says why not.
- `v_closed_without_reason`: an `accepted-risk` or `wont-fix` issue without a resolution: `issue-status` again with
  `--resolution`.
- `v_relation_problems`: a relation that contradicts its issues: add the right relation and close or reopen with a
  note.
- `v_check_finding_owner`: a check's finding belongs to another review: re-record the check in its own review.
- `v_unguarded_fixes` (warning): a fixed issue with no guard test: record one.
- `quick_check` / `integrity_check` or `foreign_key_check` failures: stop writing; `ledger.py restore
  .code-review/ledger.sql --to /tmp/check.sqlite` from the last dump, compare, and ask the owner before replacing.
- A schema difference (a table, view or trigger that is not the shipped one): someone changed the ledger's schema;
  open a gate, and do not run the project copy of `ledger.py`.

### Merge conflicts and backups

After a git merge conflict on `ledger.sqlite` (only when the owner decided to commit it): keep this side, save the
other and merge it in; then dump.

    git checkout --ours .code-review/ledger.sqlite
    git show MERGE_HEAD:.code-review/ledger.sqlite > /tmp/theirs.sqlite
    ledger.py merge /tmp/theirs.sqlite
    ledger.py dump

`merge` prints codes it renumbered; text that mentions them (notes, tags) is not rewritten. Back up by running `dump`
after each writing session, and copy `ledger.sqlite` before `migrate`, `merge` or a redaction (`migrate` and `merge`
also keep their own backups). Expect about 7 MB of ledger and 5 MB of dump per 1,000 findings (measured at 10,000
findings: 72 MB and 50 MB).
