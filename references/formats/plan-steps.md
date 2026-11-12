# Plan steps (`steps.jsonl`)

What drafters write and `ledger.py import-steps P-0001 steps.jsonl` loads. One JSON object per line; blank lines are
skipped. The whole file is checked first; any problem refuses it (exit 1, the problems listed) and nothing is loaded.

## Phase lines

    {"kind": "phase", "phase": 3, "title": "Contracts and validation", "goal": "One sentence.", "why": "Why here."}

A phase the plan does not have yet. `phase` (a whole number), `title` and `goal` are required. Steps may name a
phase the plan already has without a phase line.

## Step lines

    {"key": "C-4", "phase": 3, "order": 2, "section": "Validation", "title": "Refuse empty requests",
     "change": "What to change.", "proof": "test_x: what it asserts", "owner_need": "none",
     "owner_question": null, "behavior_change": true, "behavior_note": "Empty requests now get 400.",
     "depends": ["C-1"], "closes": ["SEC-H3", "R-0002:TEST-M7"], "multi_reason": {"SEC-H3": "the server half"},
     "no_findings_reason": null, "touches": [{"kind": "column", "target": "findings.severity", "direction": "tightens"}]}

| field | required | meaning |
|-------|----------|---------|
| key | yes | the drafter's key, unique in the plan (stored as `steps.drafter_key`; `step-status` accepts it) |
| phase | yes | a phase number of the plan (or a phase line of the file) |
| order | no | order inside the phase (a whole number; default 0, then the line order) |
| title, change, proof | yes | the step's imperative title, what to change, and the test or check that proves it |
| section | no | a heading inside the phase |
| owner_need | no | none (default), decision, confirm, review, production; anything but none needs owner_question |
| behavior_change, behavior_note | no | true when the step changes behaviour on purpose, and what changes |
| depends | no | keys of earlier steps (this file or already loaded), or numbers of steps already in the plan |
| closes | yes, unless no_findings_reason | point ids of the plan's reviews; `R-0001:SEC-H3` when the same id is in two reviews |
| multi_reason | no | point id -> why it is closed by more than one step (required for each such point) |
| no_findings_reason | no | why the step closes no point by design (the follow-up review that verifies the plan) |
| touches | no | what the step changes beyond its points' locations: a list of `{"kind", "target", "direction"}`; kind file, symbol, table, column, setting, route or behaviour; direction adds, removes, tightens, loosens, renames or changes (the default) |

Numbers: the new steps are numbered after the plan's last step, in (phase, order, line) order, so a dependency must
point at a step that comes earlier in that order. The command then reports how many points of the plan's reviews no
step closes (`v_uncovered_findings`) and the coverage problems (`v_coverage_problems`); both must reach zero before
the plan is shown to the owner.
