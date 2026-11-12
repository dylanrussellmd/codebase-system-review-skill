# Writing standard

For UI text and documentation: what a review judges (the `ui`, `docs-developer` and `docs-user` domains) and what
this skill writes (reports, briefs, notes, the hub).

Write for someone who already knows what they are looking at. Each sentence earns its place.

- **Show, don't tell.** Labels, layout and defaults make a screen clear. A control that needs a sentence to explain
  it gets fixed. No caption repeats what is visible.
- **Don't label the obvious.** No "Click here", no heading over a single item, no restating the page title.
- **Current state only.** Say what the system is and what running and maintaining it takes. No history:
  "previously", "used to", "was changed to", "no longer". History belongs in git log and the changelog.
- **Short and plain.** The point first, one idea per sentence. No filler ("note that", "it's important to",
  "simply", "in order to"), no hedging, no marketing words.
- **Only what the reader needs to act.** Link to the reference for the rest.
- **One word per thing, everywhere.** No internal names on screen; Explore is the one page that shows stored names.

## In a review

Each breach is a finding: the location, the text as it stands, and the shorter version. The docs reviewer also lists
every statement that is stale against the current code.

## Checking

- This skill's own text: `dev/tests/test_writing_lint.py` refuses the history, filler, wordy and announcing phrases
  in SKILL.md, README.md and `references/`. `CHANGELOG.md` is exempt.
- Any project: `ledger.py tool-run vale --review R-0001` runs these rules (`references/vale/`, checked with the
  Vale version `tools.lock` pins) on the reviewed commit, offline, and records each alert as a lead. Without Vale,
  the reviewer reads for the same phrases (`references/vale/styles/Plain/*.yml` lists them).
