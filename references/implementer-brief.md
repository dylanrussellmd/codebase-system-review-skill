# Implementer brief (template)

Copy this into the brief of every agent that implements plan steps, and fill in the parts in angle brackets.

## Read first
- The owner's approved design goals: `python3 .code-review/ledger.py goals` (or `goals --domain <D>` for your steps'
  domains). Work to them, and read them from the ledger rather than from memory or this brief.
- Your steps: <how to print each step's change, proof, dependencies and the points it closes, e.g.
  `python3 .code-review/ledger.py query --readonly "SELECT ... FROM v_step_issues WHERE step = N"`>.
- The files you own: <files>. Other agents are editing other files at the same time; change only yours.

## Rules
- <the owner's rules for commits, branches and pushes in this project>.
- Proof first: write each step's test, run it and see it fail, then make the change and see it pass.
- After a source change: <the build or check command>. Before you report: <the whole test suite>; it must pass.

## Record each step
`python3 .code-review/ledger.py step-status <step> doing --note "<what you start>" --actor <your A- code>`, and when it
is finished `step-status <step> done --note "<for the owner, in plain sentences: what a person would now see or what is
now refused, anything that turned out different from the plan>"`. A step blocked on the owner: `step-status <step>
waiting-owner --question "<the question>"`.

## Defects you discover that the plan does not cover (mandatory)
Never just mention a new defect in a note, a comment or your final reply. Record it the moment you find it:

    python3 .code-review/ledger.py report-finding --step <the step you were on> --severity H|M|L|T \
        --domain <domain code> --where '<path>:<start>-<end>#<symbol>' --summary '<one sentence>' \
        --before '<what the code does now>' --fix '<the concrete fix>' --test '<test name: what it asserts>' \
        [--commit <commit>] [--issue ISS-0001 | --new-issue] [--into-step N | --new-step '<title>' | --gate] \
        --actor <your A- code>

- Cite lines as they are at a commit, never in your edited working file: read them with
  `git show <commit>:<path>`. The default commit is the one the review looked at; code that did not exist then is
  cited at the commit that added it (`--commit`). The command reads the file at that commit and refuses lines past its
  end, a symbol that is not in the cited lines, and a commit the repository does not have, so an uncommitted edit can
  never be cited. It stores the cited lines as the excerpt at once.
- It lists issues that may already be the same problem (same fingerprint, same path and symbol, or overlapping lines);
  when it finds any it asks you to choose `--issue <code>` or `--new-issue` before it records anything.
- Plan it: `--into-step N` adds it to an open step that will fix it, `--new-step '<title>'` appends a step to the phase
  of the step you were on, and with neither it opens a decision gate asking the owner where it goes.
  `ledger.py check` lists every discovery that no step closes yet (v_unplanned_discoveries).
- Report only the defect itself. Do not fix it unless it is inside the step you are on; if you pin it with a
  characterization test, say so in `--test`.

## Final reply
At most <N> lines: steps done, partial or blocked; test count and result; anything the next agent must know.
