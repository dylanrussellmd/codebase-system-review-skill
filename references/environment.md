# The environment and its fallbacks

Read this in Step 1 with the output of `ledger.py env`, and whenever a step names a tool this harness lacks.

`ledger.py env` lists what the review can use here, each line `yes` or `no` with how to enable it. It reads tool
versions, the harness's variables and the names of the MCP connectors in its config files; it stores no value from a
config file and makes no network request. After `new-review`, record the list on the review:

```
ledger.py env --review R-0001 --has subagents --has questions --actor A-0002
```

`--has` states a tool of the harness that a command cannot see: `subagents`, `questions`, `images`, `publishing`.
State only what you have used in this session. `ledger.py query "SELECT capability, available, detail FROM
review_environment"` shows what was recorded; every later step reads it there, never from memory.

## Capabilities

Each step uses the capability when `env` says `yes` and the last column when it says `no`. The review is complete
either way; say in the report which fallbacks were used.

| capability | used for | without it |
|------------|----------|------------|
| `git` | every reference is read at a commit; `tag-review`, `fill-refs`, `scan` | stop and ask the owner to install git: the ledger cannot pin lines without it |
| `python` | runs `ledger.py` (3.9 or later) | nothing runs: `env` itself needs it |
| `sqlite` | the ledger file | comes with Python; an old SQLite is named by `check` |
| `playwright` | `ui-capture` and `ui-audit` (the `ui`, `walkthroughs` and `performance` domains) | both commands exit 2 with the install line; follow "Rendered pages" below |
| `browser` | the Chrome, Chromium or Edge that Playwright drives | set `LEDGER_CHROME` to one, or follow "Rendered pages" below |
| `node` | only Lighthouse needs it | `ui-audit` still measures contrast, names, weight and layout shift |
| `lighthouse` | category scores added to `ui-audit` | `ui-audit` reports it as not installed and never downloads it; give its own numbers |
| `harness` | recorded with the review and on each actor (`add-actor --harness`) | pass `--harness NAME`; nothing else depends on which harness it is |
| `design-tool` | UI proposals and a check of the code against the owner's designs | write each proposal as an HTML file in `.code-review/reviews/R-0001/ui/`, capture it with `ui-capture`, and ask the owner for exported designs |
| `subagents` | reviewers, verifiers, indexers, matchers, drafters and implementers in parallel | the manager runs each scope itself, one after the other, from the same generated prompt, and records each run as usual |
| `questions` | putting a gate's question to the owner in chat | write the question in chat as plain text; the gate in the hub is the record either way |
| `images` | judging screenshots | judge from `ui-audit`'s numbers and the source, and write "screenshots not looked at" in the report |
| `publishing` | a copy of the hub at an address | give the owner `.code-review/hub.html` or the viewer (`ledger.py serve`); answers arrive in the viewer or in chat |
| `connector:<name>` | whatever the connector offers (an issue tracker, a design tool, documentation) | work from the repository and the owner's answers; ask the owner for an export of what the connector holds |

## Harnesses

| harness | how `env` recognises it | connector files it reads (names only) |
|---------|-------------------------|----------------------------------------|
| OpenCode | a variable `OPENCODE` or `OPENCODE_...` | `opencode.json`, `opencode.jsonc` in the project, its `.opencode/` folder and the user's config folder |
| Claude Code | a variable `CLAUDECODE` or `CLAUDE_CODE_...` | `.mcp.json` in the project; `~/.claude.json` (user servers and this project's) |
| others | `--harness NAME`, or the variable `CODE_REVIEW_HARNESS` | `.cursor/mcp.json`, `.vscode/mcp.json` in the project |

A connector switched off in its file is listed with `no`. A connector whose name suggests a design tool (Figma,
Penpot and similar) makes `design-tool` `yes`.

## Rendered pages

Playwright is optional (`python3 -m pip install playwright`; it drives the Chrome or Chromium already installed).
With it:

```
ledger.py ui-capture http://127.0.0.1:3000/ http://127.0.0.1:3000/settings --review R-0001 --actor A-0002
ledger.py ui-audit http://127.0.0.1:3000/ http://127.0.0.1:3000/settings
```

- `ui-capture` screenshots each page at 390 and 1280 pixels wide, light and dark (`--widths`, `--themes`), into
  `.code-review/reviews/R-0001/ui/` and records the files on the review. A page is an http(s) address, a local HTML
  file, or a route of the ledger's own viewer (`'#/issues'`, served by `ledger.py serve`; `--base` for another port).
- `ui-audit` reports, per page: texts below the required contrast (4.5:1, or 3:1 for large text), images, fields,
  buttons and links with no name, every colour, font size and spacing value in use with its count, page weight and
  layout shift (`--json` for the same as data). Lighthouse scores are added only when Lighthouse is installed.
- Both load only the pages given: requests to any other host are stopped. The owner starts the system under review;
  never point either command at a production address without the owner's word.

Without Playwright: ask the owner to open each page at phone and desktop width in both themes and save the
screenshots into `.code-review/reviews/R-0001/ui/` (record each with `ledger.py add-artifact other --path`), or
review from the source alone. Compute contrast from the stylesheet's colour pairs, list the colours, font sizes and
spacing values with a search of the stylesheets, and write "rendered result not seen" beside every UI finding that
depends on it.
