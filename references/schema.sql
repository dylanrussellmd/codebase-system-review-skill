-- =====================================================================
-- Code review ledger — schema version 21
-- One SQLite file per project: <project root>/.code-review/ledger.sqlite
-- Every connection must run:  PRAGMA foreign_keys = ON;
-- Times are UTC ISO-8601 text with milliseconds (strftime('%Y-%m-%dT%H:%M:%fZ','now')); the tr_time_*
-- triggers refuse any other format.
-- JSON columns are TEXT checked with json_valid().
--
-- Core idea: a FINDING is one point raised by one review; an ISSUE is the
-- underlying problem tracked across reviews. Every finding belongs to one
-- issue (new or existing); issues are related to each other through
-- issue_relations, and their whole history is in events.
-- =====================================================================

-- ---------- Housekeeping ----------------------------------------------

-- Facts about this ledger file, one per row.
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,                -- schema_version, project_name, created_at, ledger_location,
                                         -- created_by_skill, written_by_tool, schema_hash
  value TEXT NOT NULL
);

-- The schema upgrades this ledger has been through.
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The checks that say whether the ledger is complete and consistent.
-- The checks `ledger.py check` and the viewer's home page run, in position order: each view must be empty (tier
-- 'error': check exits 1 when it is not) or should be (tier 'warning': listed, check still exits 0). Seeded at the
-- end of this file; a schema that adds a check inserts its row in its migration.
CREATE TABLE ledger_checks (
  view     TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  tier     TEXT NOT NULL CHECK (tier IN ('error','warning')),
  position INTEGER NOT NULL UNIQUE
);

-- ---------- Words: the single source of the ledger's status words (D-0009) ----
-- Each lookup table lists the words a column may hold (referenced by foreign key), the plain label the viewer
-- shows the owner, and the order the viewer lists them in. Views, triggers and the viewer pick words by the
-- flags here, never by copies of the lists: a new issue status is one row in issue_statuses. Every other word
-- column keeps its CHECK list, with its labels in enum_labels (a test keeps the two equal). Seeded at the end of
-- this file.

-- The statuses an issue can have, with the label shown for each.
CREATE TABLE issue_statuses (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE,
  active   INTEGER NOT NULL CHECK (active IN (0,1)),    -- still to fix (v_open_issues, the "Still to fix" filter)
  resolved INTEGER NOT NULL CHECK (resolved IN (0,1)),  -- closed: entering it sets issues.resolved_at
  fixed    INTEGER NOT NULL CHECK (fixed IN (0,1)),     -- the code was fixed: a regression match or check regresses it
  reopens  INTEGER NOT NULL CHECK (reopens IN (0,1)),   -- closed without a fix: a recurrence reopens it
  briefed  INTEGER NOT NULL CHECK (briefed IN (0,1)),   -- given to reviewers as a known issue (not merged away)
  CHECK (active + resolved = 1),
  CHECK (fixed = 0 OR resolved = 1),
  CHECK (reopens = 0 OR (resolved = 1 AND fixed = 0))
);

-- How serious a point is: high, medium, low, or a missing test.
CREATE TABLE severities (
  value    TEXT PRIMARY KEY,                 -- H, M, L, T
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE           -- most severe first
);

-- How a point relates to the issue it was matched to.
-- new: this review opened the issue. still-present, changed: an open issue seen again, as before or differently.
-- recurrence: an issue closed without a fix was raised again. regression: a fixed issue came back. continuation: a
-- new issue that continues an earlier one (with a continuation-of relation). references/indexing.md says how to
-- choose.
CREATE TABLE match_types (
  value     TEXT PRIMARY KEY,
  label     TEXT NOT NULL,
  position  INTEGER NOT NULL UNIQUE,
  came_back INTEGER NOT NULL CHECK (came_back IN (0,1))  -- the issue was closed and was raised again
);

-- The ways two issues can be related.
CREATE TABLE relation_types (
  value         TEXT PRIMARY KEY,
  label         TEXT NOT NULL,              -- read from the issue the relation starts at ("Duplicate of")
  inverse_label TEXT NOT NULL,              -- read from the issue it points to ("Has duplicate")
  position      INTEGER NOT NULL UNIQUE,
  closes        INTEGER NOT NULL CHECK (closes IN (0,1))  -- recording it closes the active issue as duplicate
);

-- The kinds of question the owner is asked.
CREATE TABLE gate_kinds (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE
);

-- The statuses a design goal can have.
-- Proposed from the owner interview, approved or rejected by the owner, retired once it no longer applies. served =
-- given to reviewers, drafters and implementers (`ledger.py goals`, v_goals_for_brief, the goals a plan step may
-- serve).
CREATE TABLE goal_statuses (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE,
  served   INTEGER NOT NULL CHECK (served IN (0,1))    -- given to reviewers, drafters and implementers (`goals`)
);

-- The labels shown for the words of every other fixed list.
-- Labels of the words of every other word column (whose CHECK lists the allowed words). enum names the list:
-- issue_kind is issues.kind and findings.kind, confidence is issue_relations.confidence and
-- findings.match_confidence, verdict is the verdict of finding_requirements and review_requirement_checks.
CREATE TABLE enum_labels (
  enum     TEXT NOT NULL,
  value    TEXT NOT NULL,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (enum, value),
  UNIQUE (enum, position)
);

-- ---------- Who and what ----------------------------------------------

-- The git repositories the project is made of.
-- permalink_template builds the web link to cited lines: {sha} is the commit, {path} the file, {start} and {end}
-- the first and last line. A whole-file reference drops the #fragment that holds {start} or {end}.
CREATE TABLE repositories (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,   -- short name used in the brief
  path           TEXT,                   -- relative to the project root, '.' for the root repo
  remote_url     TEXT,
  default_branch TEXT,
  permalink_template TEXT CHECK (permalink_template IS NULL OR permalink_template GLOB 'https://*'
                                 OR permalink_template GLOB 'http://*'),
                                         -- e.g. https://github.com/o/r/blob/{sha}/{path}#L{start}-L{end}
  notes          TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  allow_outside_root INTEGER NOT NULL DEFAULT 0 CHECK (allow_outside_root IN (0,1)),
                                         -- 1 = the owner listed this repo explicitly as living outside the
                                         -- project root (a sibling checkout); its path may then be absolute or
                                         -- start with ../. Otherwise ledger.py refuses to read outside the root.
  -- path: no leading -, no control characters; and unless allow_outside_root, relative (no leading /, no
  -- backslash) with no .. segment
  CHECK (path IS NULL OR (path NOT GLOB '-*' AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*')
         AND (allow_outside_root = 1 OR (path NOT GLOB '/*' AND path NOT GLOB '*\*' AND path <> '..'
              AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'))))
);

-- The people and agents who did or decided something.
-- One row per agent instance (each subagent run gets its own row), so "who said this" is always answerable.
CREATE TABLE actors (
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('owner','human','manager','reviewer','verifier','indexer',
                     'matcher','drafter','fixer','tool')),
  name            TEXT NOT NULL,         -- person's name, or the agent's role label ("security reviewer")
  model           TEXT,                  -- model id for agents
  harness         TEXT,                  -- e.g. OpenCode, CI
  session_ref     TEXT,                  -- session id or URL
  parent_actor_id INTEGER REFERENCES actors(id),   -- subagent -> the agent that launched it
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Catalogs ----------------------------------------------------

-- The review domains: the angles a codebase is reviewed from.
-- report_file is the default report name; <module> in it stands for the part of the code one reviewer of a split
-- domain covers (tests-<module>.md gives tests-api.md). description is optional and empty for the built-in domains,
-- which references/domains.md describes. A domain with retired_at set takes no new reviews, findings, issues,
-- metrics or goals (the tr_*_domain_active triggers); rows that already use it stay.
CREATE TABLE domains (
  code        TEXT PRIMARY KEY,          -- 'security', 'logging', 'docs-developer' ...
  name        TEXT NOT NULL,
  category    TEXT NOT NULL CHECK (category IN
                ('correctness','architecture','user-facing','security-operations',
                 'documentation','custom')),
  description TEXT,
  report_file TEXT,                      -- default report file name
  retired_at  TEXT
);

-- The rule by which the scan proposes each domain for a review.
-- `ledger.py scan` evaluates it.
-- rule is 'fact >= N' terms joined by ' and ' and ' or ' ('ui_files >= 1 and server_files >= 1'); the facts are the
-- counts scan takes from the repositories at the reviewed commit (references/domains.md lists them). A domain with
-- no row is never proposed by scan. Seeded at the end of this file from the table in references/domains.md.
CREATE TABLE domain_rules (
  domain_code TEXT PRIMARY KEY REFERENCES domains(code),
  rule        TEXT NOT NULL CHECK (length(trim(rule)) > 0)   -- 'db_files >= 1 or api_files >= 1'
);

-- The questions every reviewer of a domain answers, in order.
-- brief and prompt list them; lint-report checks a report answers each with its evidence; severity is the default
-- of a finding that names the item. Seeded at the end of this file from the table in references/domains.md.
CREATE TABLE domain_checks (
  code        TEXT PRIMARY KEY CHECK (length(trim(code)) > 0),        -- spec.1
  domain_code TEXT NOT NULL REFERENCES domains(code),
  position    INTEGER NOT NULL,
  severity    TEXT NOT NULL REFERENCES severities(value),   -- the default of a finding that answers it
  question    TEXT NOT NULL CHECK (length(trim(question)) > 0),
  evidence    TEXT NOT NULL CHECK (length(trim(evidence)) > 0),     -- what counts as an answer
  UNIQUE (domain_code, position)
);

-- The topics a point can be filed under, whatever its domain.
-- A theme with retired_at set is not accepted for new findings; rows that already use it stay.
CREATE TABLE themes (
  code       TEXT PRIMARY KEY,           -- 'authorization', 'logging-audit' ...
  name       TEXT NOT NULL,
  retired_at TEXT
);

-- ---------- Standards the code is judged against ----------------------

-- What the owner wants the code to be, one statement per goal.
-- Each goal belongs to an approved domain (domain_code; NULL = the whole project). Written from the owner interview
-- as 'proposed' (`ledger.py propose-goal`, citing the interview questions it summarizes in goal_sources) and given
-- to agents only once the owner approved it (`approve-goals`, or the confirm gate gate_id answered 'approve' in the
-- viewer or hub; approved_by_actor_id and approved_at say who and when). A rejected goal keeps the owner's reason
-- in note; an approved goal is never edited: it is retired (setting status 'retired' or retired_at sets the other)
-- and a new one proposed (superseded_by). Goals recorded before schema 9 became approved whole-project goals.
-- retired_at alone means the goal no longer applies; with superseded_by, that another goal replaced it.
CREATE TABLE design_goals (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE     -- G-01
                  CHECK (code GLOB 'G-[0-9][0-9]*' AND code NOT GLOB 'G-*[^0-9]*'),
  text          TEXT NOT NULL,           -- one statement of one goal, in words the owner approved
  source        TEXT,                    -- where it came from (doc path, "owner, 2026-11-10")
  adopted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),  -- when it was written down
  retired_at    TEXT,
  superseded_by INTEGER REFERENCES design_goals(id),
  domain_code   TEXT REFERENCES domains(code),           -- NULL = a goal of the whole project
  status        TEXT NOT NULL DEFAULT 'proposed' REFERENCES goal_statuses(value),
  approved_by_actor_id INTEGER REFERENCES actors(id),   -- the owner (an actor of kind owner or human)
  approved_at   TEXT,
  review_id     INTEGER REFERENCES reviews(id),          -- the review whose interview it came from
  gate_id       INTEGER REFERENCES gates(id),            -- the confirm gate that asks the owner to approve it
  note          TEXT,                    -- why it was rejected or retired
  change_actor_id INTEGER REFERENCES actors(id),        -- who makes the next status change; logged, then cleared
  CHECK (superseded_by IS NULL OR retired_at IS NOT NULL),   -- a superseded goal is retired
  CHECK (status <> 'approved' OR (approved_by_actor_id IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK (status <> 'rejected' OR length(trim(COALESCE(note, ''))) > 0)
);

-- ---------- Reviews -----------------------------------------------------

-- History is append-only: reviews, findings, steps and decisions refuse DELETE (tr_*_no_delete). The keys from
-- the children of reviews (review_repositories, review_goals, review_domains, reviewer_runs, review_actors,
-- findings, review_requirement_checks, review_issue_checks, metrics) and of findings (finding_locations,
-- finding_goals, finding_requirements) are ON DELETE RESTRICT (schema 6), so even with the triggers gone a
-- review or finding with rows under it cannot be deleted. The ON DELETE CASCADE keys left (phases, steps,
-- plan_reviews of plans; step_dependencies, step_findings, step_commits of steps; decision_issues of decisions)
-- fire only when their parent row is deleted, which the triggers refuse for steps and decisions; so they reach
-- only parents nothing protects (a plan with no steps, for instance). Abandon a review (status 'abandoned')
-- instead of deleting it.

-- One row for each review of the project.
-- status runs planning <-> awaiting-approval -> approved -> running -> verifying -> indexing -> consolidating ->
-- handed-off, or abandoned from any status (tr_review_transition has the exact moves). skill_version is the version
-- of the skill that started the review, as `ledger.py --version` prints it.
CREATE TABLE reviews (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE    -- R-0001
                          CHECK (code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'R-*[^0-9]*'),
  title                 TEXT NOT NULL,
  scope                 TEXT NOT NULL CHECK (scope IN
                          ('full','partial','targeted','second','fix-verification')),
  status                TEXT NOT NULL DEFAULT 'planning' CHECK (status IN
                          ('planning','awaiting-approval','approved','running','verifying',
                           'indexing','consolidating','handed-off','abandoned'))
                          -- past awaiting-approval only once the owner approved (set approved_at with it)
                          CHECK (status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NOT NULL),
  request_text          TEXT,                   -- what the owner asked for, in their words
  requested_by_actor_id INTEGER REFERENCES actors(id),
  manager_actor_id      INTEGER REFERENCES actors(id),
  approved_by_actor_id  INTEGER REFERENCES actors(id),
  approved_at           TEXT,
  previous_review_id    INTEGER REFERENCES reviews(id),  -- the review this one follows up
  skill_version         TEXT,
  review_folder         TEXT,                   -- scratch folder used (may not persist)
  summary_doc_url       TEXT CHECK (summary_doc_url IS NULL OR summary_doc_url GLOB 'https://*'
                                    OR summary_doc_url GLOB 'http://*'),
  verdict               TEXT,
  summary               TEXT,
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at          TEXT CHECK (completed_at IS NULL OR julianday(started_at) <= julianday(completed_at)),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status change, copied into its event and then cleared (like issues.change_*);
  -- leaving handed-off or abandoned needs change_note.
  change_actor_id       INTEGER REFERENCES actors(id),
  change_note           TEXT,
  report_md             TEXT                    -- the review document for the owner (Markdown), shown in the
                                                -- viewer; written in Step 6 with `ledger.py set-report`
);

-- The exact commit of each repository that a review looked at.
CREATE TABLE review_repositories (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                   AND commit_sha NOT GLOB '*[^0-9a-f]*'),  -- full hash, lowercase hex
  branch        TEXT,
  commit_time   TEXT,                     -- the commit's committer time in UTC (…Z, like every time here; a git
                                          -- time with an offset is converted); tag-review fills it when NULL
  commit_subject TEXT,
  tag_name      TEXT,                     -- e.g. review/R-0001, keeps the commit reachable
  tag_pushed    INTEGER NOT NULL DEFAULT 0 CHECK (tag_pushed IN (0,1)),
  is_dirty      INTEGER NOT NULL DEFAULT 0 CHECK (is_dirty IN (0,1)),
  dirty_note    TEXT,                     -- what was uncommitted, if anything
  dirty_patch_path TEXT,                  -- saved `git diff` of uncommitted changes, if any
  PRIMARY KEY (review_id, repository_id)
);

-- The design goals each review judged the code against.
CREATE TABLE review_goals (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);

-- The domains proposed for each review, and whether each was reviewed or skipped.
-- reason says why; owner_change records what the owner changed in the proposal; proposed_by_rule is the rule by
-- which `ledger.py scan` proposed the domain (NULL when scan skipped it or a person added it).
CREATE TABLE review_domains (
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code       TEXT NOT NULL REFERENCES domains(code),
  decision          TEXT NOT NULL CHECK (decision IN ('included','skipped')),
  reason            TEXT NOT NULL,
  owner_change      TEXT NOT NULL DEFAULT 'none' CHECK (owner_change IN
                      ('none','added-by-owner','removed-by-owner','modified-by-owner')),
  reviewers_planned INTEGER,
  split_note        TEXT,                 -- how the domain was split between reviewers
  proposed_by_rule  TEXT,                 -- the domain_rules rule (or 'open_issues >= 1') that proposed it
  PRIMARY KEY (review_id, domain_code)
);

-- What each review's environment offered: tools, the harness and its connectors.
-- `ledger.py env --review R-0001` writes one row per capability: git, python, sqlite, playwright, browser, node,
-- lighthouse, harness, design-tool, one 'connector:<name>' per MCP connector configured for the harness (the name
-- only, never a value from a config file), and the harness tools the agent states (subagents, questions, images,
-- publishing). A capability recorded earlier and no longer found stays, with available 0.
CREATE TABLE review_environment (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  capability    TEXT NOT NULL CHECK (length(trim(capability)) > 0),   -- git, playwright, connector:figma ...
  available     INTEGER NOT NULL CHECK (available IN (0,1)),
  detail        TEXT,                    -- a version, a name, or where a connector is configured
  how_to_enable TEXT NOT NULL,           -- what to install or do to have it
  position      INTEGER NOT NULL,        -- the order `env` lists them in
  PRIMARY KEY (review_id, capability)
);

-- The answer each report gave to each item of its domain's checklist.
-- Written by `ledger.py lint-report --record` for a report that passes; a corrected report replaces its answers.
CREATE TABLE checklist_answers (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  check_code TEXT NOT NULL REFERENCES domain_checks(code),
  report     TEXT NOT NULL,                -- the report's file name
  answer     TEXT NOT NULL CHECK (answer IN ('yes','no','partly','not-applicable')),
  evidence   TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
  PRIMARY KEY (review_id, check_code, report)
);

-- One row for each reviewer that ran in a review.
-- label names the run within its review. items_reviewed and the high, medium, low and test counts are what the
-- reviewer reported in its final message; v_review_summary counts the findings that were indexed.
CREATE TABLE reviewer_runs (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code    TEXT NOT NULL REFERENCES domains(code),
  actor_id       INTEGER NOT NULL REFERENCES actors(id),
  label          TEXT NOT NULL,           -- names the run within its review: 'tests-api'
  scope          TEXT NOT NULL,
  prompt         TEXT,
  report_path    TEXT,
  report_sha256  TEXT,
  status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                   ('queued','running','finished','failed','cancelled')),
  items_reviewed INTEGER CHECK (items_reviewed IS NULL OR items_reviewed >= 0),
  high_count     INTEGER CHECK (high_count IS NULL OR high_count >= 0),
  medium_count   INTEGER CHECK (medium_count IS NULL OR medium_count >= 0),
  low_count      INTEGER CHECK (low_count IS NULL OR low_count >= 0),
  test_count     INTEGER CHECK (test_count IS NULL OR test_count >= 0),
  final_message  TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  UNIQUE (review_id, label)
);

-- The other agents that worked on each review.
-- Verifiers, indexers, matchers, drafters and fixers: reviewers are tied to it by reviewer_runs and the manager by
-- reviews.manager_actor_id. v_review_agents lists all of them.
CREATE TABLE review_actors (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);

-- ---------- Requirements (for spec conformance over time) -------------

-- What the code is required to do, and where each requirement was stated.
-- source is the document, issue or conversation, source_location the place in it, stated_at when it was stated. A
-- requirement replaced by another is superseded (superseded_by names the new one); one that no longer applies is
-- withdrawn. first_review_id is the review that recorded it.
CREATE TABLE requirements (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE    -- REQ-0001
                    CHECK (code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'REQ-*[^0-9]*'),
  text            TEXT NOT NULL,
  source          TEXT NOT NULL,          -- doc path, issue URL, "owner, chat 2026-11-10"
  source_location TEXT,
  stated_at       TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','withdrawn')),
  superseded_by   INTEGER REFERENCES requirements(id)   -- set exactly when status is superseded
                    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
  first_review_id INTEGER REFERENCES reviews(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Issues: the longitudinal record ----------------------------

-- A problem in the code, followed from review to review until it is closed.
-- severity is H, M or L (high, medium, low), or T for a missing test and nothing else. fingerprint is
-- domain:theme:sub, what findings are matched on.
CREATE TABLE issues (
  id                     INTEGER PRIMARY KEY,
  code                   TEXT NOT NULL UNIQUE    -- ISS-0001, never reused
                           CHECK (code GLOB 'ISS-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'ISS-*[^0-9]*'),
  title                  TEXT NOT NULL,
  domain_code            TEXT NOT NULL REFERENCES domains(code),
  theme_code             TEXT REFERENCES themes(code),
  sub                    TEXT,                   -- slug naming the specific thing
  kind                   TEXT NOT NULL CHECK (kind IN
                           ('defect','test','validation','refactor','feature','design','docs',
                            'logging','perf','security','ops','verify')),
  severity               TEXT NOT NULL REFERENCES severities(value),             -- current
  status                 TEXT NOT NULL DEFAULT 'open' REFERENCES issue_statuses(value),
  fingerprint            TEXT,                   -- domain:theme:sub, for matching
  before_state           TEXT,                   -- what the system did when first found
  current_state          TEXT,                   -- what the latest review observed
  after_state            TEXT,                   -- what it does once fixed
  recommendation         TEXT,                   -- current agreed fix
  resolution             TEXT,                   -- how it was closed, or why it was accepted
  first_seen_review_id   INTEGER NOT NULL REFERENCES reviews(id),
  last_seen_review_id    INTEGER REFERENCES reviews(id),   -- last review that raised it
  last_checked_review_id INTEGER REFERENCES reviews(id),   -- last review that looked for it
  times_seen             INTEGER NOT NULL DEFAULT 1 CHECK (times_seen >= 1),
  opened_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at            TEXT,
  verified_at            TEXT,
  updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status/severity change, set in the same UPDATE: change_note is required (the
  -- update is refused without it), the others where they apply. The triggers copy them into events, so
  -- history records who, why, in which review/step/commit, and then set all five back to NULL, so context
  -- is never reused by a later change.
  change_actor_id        INTEGER REFERENCES actors(id),
  change_review_id       INTEGER REFERENCES reviews(id),
  change_step_id         INTEGER REFERENCES steps(id),
  change_commit_sha      TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40
                                     OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note            TEXT
);

-- How one issue is related to another.
-- Read it from left to right: from_issue_id is a duplicate of, continues, is caused by, blocks ... to_issue_id
-- (relation_types has the label for each reading). A relation is stored once, in that direction.
CREATE TABLE issue_relations (
  id                       INTEGER PRIMARY KEY,
  from_issue_id            INTEGER NOT NULL REFERENCES issues(id),
  to_issue_id              INTEGER NOT NULL REFERENCES issues(id),
  relation                 TEXT NOT NULL REFERENCES relation_types(value),
  confidence               TEXT NOT NULL DEFAULT 'likely' CHECK (confidence IN ('certain','likely','possible')),
  rationale                TEXT NOT NULL,
  established_in_review_id INTEGER REFERENCES reviews(id),
  established_by_actor_id  INTEGER REFERENCES actors(id),
  created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (from_issue_id <> to_issue_id),
  UNIQUE (from_issue_id, to_issue_id, relation)
);

-- ---------- Findings: every point from every review ---------------------

-- Every point a review raised, as the reviewer reported it.
-- match_type says how the point relates to its issue (match_types lists the values and what each means).
CREATE TABLE findings (
  id                   INTEGER PRIMARY KEY,
  review_id            INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  reviewer_run_id      INTEGER REFERENCES reviewer_runs(id),
  point_id             TEXT NOT NULL,       -- index id: prefix + the report's own id (SEC-H3)
  issue_id             INTEGER REFERENCES issues(id),   -- set during matching, by an UPDATE (never on INSERT)
  match_type           TEXT REFERENCES match_types(value)
                         CHECK (match_type IS NULL OR issue_id IS NOT NULL),   -- a match type needs the issue
  match_confidence     TEXT CHECK (match_confidence IN ('certain','likely','possible')),
  match_rationale      TEXT,
  severity             TEXT NOT NULL REFERENCES severities(value),
  kind                 TEXT NOT NULL CHECK (kind IN
                         ('defect','test','validation','refactor','feature','design','docs',
                          'logging','perf','security','ops','verify')),
  domain_code          TEXT NOT NULL REFERENCES domains(code),
  theme_code           TEXT REFERENCES themes(code),
  sub                  TEXT NOT NULL,
  summary              TEXT NOT NULL,       -- one self-contained sentence
  detail               TEXT,
  before_state         TEXT,                -- what the system does now, as observed
  recommendation       TEXT,                -- the fix
  recommended_test     TEXT,                -- test name and what it must assert
  evidence             TEXT,
  where_report         TEXT,                -- report line numbers
  fingerprint          TEXT,
  related_point_ids    TEXT CHECK (related_point_ids IS NULL OR json_valid(related_point_ids)),
  verification         TEXT NOT NULL DEFAULT 'unverified' CHECK (verification IN
                         ('unverified','confirmed','partly','false','not-checkable')),
  verified_by_actor_id INTEGER REFERENCES actors(id),
  verified_at          TEXT,
  verification_note    TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  discovered_in_step_id INTEGER REFERENCES steps(id),  -- set when the point was found during fix work on this
                                            -- plan step (not by a reviewer); v_unplanned_discoveries checks it
  UNIQUE (review_id, point_id)
);

-- Where in the code each point is, pinned to the commit that was reviewed.
-- Three ways back to the lines, strongest first:
--   1. git: `git -C <repo path> show <commit_sha>:<path>`, lines line_start..line_end
--      (the review's commit is protected by the tag in review_repositories.tag_name,
--      so it survives rebases, squashes and branch deletion);
--   2. permalink: a web link to the same lines at the same commit, if the repo has a remote;
--   3. excerpt: the cited lines copied verbatim at review time, so the text survives
--      even if the history is rewritten or the repo is lost.
-- symbol survives line drift when looking for the same code in a later commit.
CREATE TABLE finding_locations (
  id             INTEGER PRIMARY KEY,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  repository_id  INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                                    AND commit_sha NOT GLOB '*[^0-9a-f]*')),
                                            -- full hash, lowercase hex; filled from review_repositories if left
                                            -- NULL (the location's repository must be one the review looked at)
  path           TEXT NOT NULL,             -- repo-relative, forward slashes (see the CHECK below)
  line_start     INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end       INTEGER CHECK (line_end IS NULL OR line_start IS NOT NULL),   -- no end without a start
  symbol         TEXT,                      -- function, class, route, setting name
  blob_sha       TEXT,                      -- `git rev-parse <commit>:<path>`
  excerpt        TEXT,                      -- the cited lines, verbatim, at that commit
  excerpt_sha256 TEXT,                      -- hash of excerpt, to detect the same code later
  permalink      TEXT CHECK (permalink IS NULL OR permalink GLOB 'https://*' OR permalink GLOB 'http://*'),
                                            -- e.g. https://github.com/o/r/blob/<sha>/<path>#L10-L20
  role           TEXT NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','also','test','doc','config')),
  excerpt_redacted INTEGER NOT NULL DEFAULT 0 CHECK (excerpt_redacted IN (0,1)),
                                            -- 1 = fill-refs masked secret-looking values in excerpt as [redacted];
                                            -- excerpt_sha256 is still the hash of the real lines
  whole_file     INTEGER NOT NULL DEFAULT 0 CHECK (whole_file IN (0,1)),
                                            -- 1 = the point is about the whole file (location form repo:path, no
                                            -- lines, no symbol); fill-refs sets it with lines 1..n
  excerpt_status TEXT CHECK (excerpt_status IS NULL OR excerpt_status IN ('truncated','not-text')),
                                            -- truncated = a whole-file excerpt holds only its first 200 lines;
                                            -- not-text = binary or not UTF-8: no excerpt, excerpt_sha256 is of
                                            -- the file's bytes
  source         TEXT NOT NULL DEFAULT 'commit' CHECK (source IN ('commit','working-tree')),
                                            -- where the excerpt was read: the commit, or (schema 8) the working
                                            -- tree of a review that recorded uncommitted changes (is_dirty), since
                                            -- that is the text its reviewers saw (snapshot saves the patch)
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);

-- The design goals each point goes against.
CREATE TABLE finding_goals (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);

-- The requirements each point is about, and how the code compares.
-- verdict: matches (the code does what is required), differs (it does something else), partly, missing (not
-- implemented), untested (implemented, but no test asserts it).
CREATE TABLE finding_requirements (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);

-- A review's verdict on each requirement it traced.
-- Recorded by the spec reviewer whether or not a finding was raised (a 'matches' or 'untested' row has no finding).
-- One row per review and requirement; v_requirement_trace lists them with counts.
CREATE TABLE review_requirement_checks (
  id                INTEGER PRIMARY KEY,
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  requirement_id    INTEGER NOT NULL REFERENCES requirements(id),
  verdict           TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  where_implemented TEXT,                  -- file, function or page that implements it
  tested            TEXT CHECK (tested IS NULL OR tested IN ('yes','partly','no')),
  finding_id        INTEGER REFERENCES findings(id),   -- the finding raised for it, if any
  evidence          TEXT,
  UNIQUE (review_id, requirement_id)
);

-- What each review concluded about each issue that was already known.
-- Absence of a finding is NOT evidence of a fix; only a row here is.
CREATE TABLE review_issue_checks (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id),
  outcome    TEXT NOT NULL CHECK (outcome IN
               ('still-present','changed','fixed','regressed','obsolete','not-checked')),
  finding_id INTEGER REFERENCES findings(id),
  evidence   TEXT,
  actor_id   INTEGER REFERENCES actors(id),
  checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (review_id, issue_id)
);

-- ---------- Owner decisions -------------------------------------------

-- The owner's rulings: each question, the answer and the reason.
-- status: open (asked), decided (the answer is set and decided_at filled), revisit (to be asked again; revisit_when
-- says what reopens it). options is a JSON array of the choices offered. review_id and step_id say where the
-- question came up; decision_issues lists the issues it settles.
CREATE TABLE decisions (
  id                  INTEGER PRIMARY KEY,
  code                TEXT NOT NULL UNIQUE    -- D-0001
                        CHECK (code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'D-*[^0-9]*'),
  question            TEXT NOT NULL,
  options             TEXT CHECK (options IS NULL OR json_valid(options)),
  answer              TEXT,
  rationale           TEXT,
  status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','revisit'))
                        CHECK (status <> 'decided' OR answer IS NOT NULL),   -- a decided decision has its answer
  revisit_when        TEXT,                   -- condition or date that reopens it
  review_id           INTEGER REFERENCES reviews(id),
  step_id             INTEGER REFERENCES steps(id),
  asked_by_actor_id   INTEGER REFERENCES actors(id),
  decided_by_actor_id INTEGER REFERENCES actors(id),
  asked_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at          TEXT
);

-- The issues each decision settles.
CREATE TABLE decision_issues (
  decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  issue_id    INTEGER NOT NULL REFERENCES issues(id),
  PRIMARY KEY (decision_id, issue_id)
);

-- Questions waiting for the owner's answer.
-- The agent opens a gate and does not pass it until it is answered (in chat or in the viewer). When the agent acts
-- on the answer it sets status = 'applied'. Decisions are the durable rulings; a gate of kind 'decision' records
-- its answer in decisions too.
CREATE TABLE gates (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE      -- GATE-0001
                    CHECK (code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'GATE-*[^0-9]*'),
  kind            TEXT NOT NULL REFERENCES gate_kinds(value),
  title           TEXT NOT NULL,
  question        TEXT NOT NULL,
  context         TEXT,                     -- plain text the owner needs to answer
  options         TEXT CHECK (options IS NULL OR json_valid(options))
                    CHECK (options IS NULL OR json_type(options) = 'array'),
                                            -- JSON array of {"value","label","description"} (value and label
                                            -- required: tr_gate_options_shape)
  allow_text      INTEGER NOT NULL DEFAULT 1 CHECK (allow_text IN (0,1))
                    -- someone must be able to answer: free text, or at least one option
                    CHECK (allow_text = 1 OR (options IS NOT NULL AND json_array_length(options) > 0)),
  blocking        INTEGER NOT NULL DEFAULT 1 CHECK (blocking IN (0,1)),  -- work stops until answered
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','applied','withdrawn'))
                    CHECK (status <> 'answered' OR answer_value IS NOT NULL OR answer_text IS NOT NULL)
                    CHECK (status <> 'applied' OR applied_note IS NOT NULL),
  answer_value    TEXT,                     -- the option chosen; a multi-select gate's: a JSON list of them
  answer_text     TEXT,                     -- the owner's own words
  answered_by     TEXT,
  answered_via    TEXT CHECK (answered_via IS NULL OR answered_via IN ('chat','viewer','other')),
  answered_at     TEXT,
  applied_note    TEXT,                     -- what the agent did with the answer
  applied_at      TEXT,
  review_id       INTEGER REFERENCES reviews(id),
  plan_id         INTEGER REFERENCES plans(id),
  step_id         INTEGER REFERENCES steps(id),
  issue_id        INTEGER REFERENCES issues(id),
  decision_id     INTEGER REFERENCES decisions(id),
  opened_by_actor_id INTEGER REFERENCES actors(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  answered_by_actor_id INTEGER REFERENCES actors(id),  -- who answered (the viewer: the owner's actors row);
                                            -- answered_by keeps the text for older rows
  change_actor_id INTEGER REFERENCES actors(id),       -- who made the next status change (applied, withdrawn);
                                            -- copied into its event, then cleared
  multi INTEGER NOT NULL DEFAULT 0 CHECK (multi IN (0,1)),   -- takes any number of its options (schema 18)
  preselect TEXT CHECK (preselect IS NULL OR (json_valid(preselect) AND json_type(preselect) = 'array'))
                                            -- the option values a multi-select gate shows ticked first
);

-- ---------- The owner interview (schema 9, plan step 129) --------------
-- After the owner approves a review's domains the manager interviews them about the codebase and their vision:
-- each question names the approved domain it is about (NULL = the whole project) and what in the code prompted it.
-- Questions and answers are append-only; a question may get several answers (a later one adds to or corrects an
-- earlier one). A question asked in the hub as well has gate_id: the owner's answer to that gate is recorded here
-- (via 'viewer') by tr_gate_answers_question. `ledger.py interview` lists them.

-- The questions the owner was asked about the codebase and what they want from it.
CREATE TABLE interview_questions (
  id                INTEGER PRIMARY KEY,
  code              TEXT NOT NULL UNIQUE    -- IQ-0001
                      CHECK (code GLOB 'IQ-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'IQ-*[^0-9]*'),
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code       TEXT REFERENCES domains(code),   -- an approved domain of the review; NULL = the whole project
  question          TEXT NOT NULL CHECK (length(trim(question)) > 0),
  why_asked         TEXT NOT NULL CHECK (length(trim(why_asked)) > 0),  -- what in the code prompted it
  asked_by_actor_id INTEGER REFERENCES actors(id),
  gate_id           INTEGER REFERENCES gates(id),    -- asked in the hub too: the gate's answer is recorded here
  asked_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The owner's answers to the interview questions, in their own words.
CREATE TABLE interview_answers (
  id                   INTEGER PRIMARY KEY,
  question_id          INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  answer               TEXT NOT NULL CHECK (length(trim(answer)) > 0),   -- the owner's words, verbatim
  answered_via         TEXT NOT NULL DEFAULT 'chat' CHECK (answered_via IN ('chat','viewer')),
  answered_by_actor_id INTEGER REFERENCES actors(id),   -- who recorded it
  answered_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The interview questions each design goal was drawn from.
CREATE TABLE goal_sources (
  goal_id     INTEGER NOT NULL REFERENCES design_goals(id) ON DELETE RESTRICT,
  question_id INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  PRIMARY KEY (goal_id, question_id)
);

-- ---------- Fix plans -----------------------------------------------------

-- A fix plan: the ordered work that closes the points of one or more reviews.
-- page_url is the address of the plan's hub page (set when `add-artifact` records a progress-page with a URL);
-- doc_url is a document the owner keeps elsewhere, if any. superseded_by names the plan that replaced this one.
CREATE TABLE plans (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE    -- P-0001
                         CHECK (code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'P-*[^0-9]*'),
  title                TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'drafting' CHECK (status IN
                         ('drafting','awaiting-approval','approved','in-progress','complete',
                          'superseded','abandoned'))
                         -- approved, in-progress and complete only once the owner approved (set approved_at with it)
                         CHECK (status IN ('drafting','awaiting-approval','superseded','abandoned')
                                OR approved_at IS NOT NULL),
  approved_by_actor_id INTEGER REFERENCES actors(id),
  approved_at          TEXT,
  superseded_by        INTEGER REFERENCES plans(id),
  doc_url              TEXT,
  page_url             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status change, copied into its event and then cleared; leaving superseded or
  -- abandoned needs change_note.
  change_actor_id      INTEGER REFERENCES actors(id),
  change_note          TEXT,
  narrative_md         TEXT                    -- the plan's narrative for the owner (Markdown: how to read it,
                                               -- why this order), shown in the viewer; `ledger.py set-narrative`
);

-- The reviews each fix plan covers.
CREATE TABLE plan_reviews (
  plan_id   INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  PRIMARY KEY (plan_id, review_id)
);

-- The phases of a fix plan, in order.
-- goal is one sentence saying what the phase achieves; why says why it sits where it does.
CREATE TABLE phases (
  id      INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  number  INTEGER NOT NULL,
  title   TEXT NOT NULL,
  goal    TEXT NOT NULL,                     -- one sentence
  why     TEXT,                              -- why it sits where it does
  UNIQUE (plan_id, number)
);

-- One change of a fix plan, with the test that proves it.
-- number is the step's place in the whole plan, not in its phase. drafter_key is the key the plan's drafter gave
-- the step, unique in the plan; `step-status` accepts it in place of the number. A status change sets change_note,
-- and change_actor_id and change_commit_sha where they apply, in the same UPDATE, as for issues.
-- A step waiting on the owner asks through a gate of the kind its owner_need maps to (a test checks the map):
--   owner_need decision -> gate kind decision
--   owner_need confirm -> gate kind confirm
--   owner_need review -> gate kind other
--   owner_need production -> gate kind production-action
CREATE TABLE steps (
  id                INTEGER PRIMARY KEY,
  plan_id           INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  phase_id          INTEGER NOT NULL REFERENCES phases(id),
  number            INTEGER NOT NULL,        -- global within the plan
  section           TEXT,
  title             TEXT NOT NULL,           -- imperative
  change            TEXT NOT NULL,           -- what to change
  proof             TEXT NOT NULL,           -- test or check that proves it
  owner_need        TEXT NOT NULL DEFAULT 'none' CHECK (owner_need IN
                      ('none','decision','confirm','review','production')),
  owner_question    TEXT,
  behavior_change   INTEGER NOT NULL DEFAULT 0 CHECK (behavior_change IN (0,1)),
  behavior_note     TEXT,
  status            TEXT NOT NULL DEFAULT 'not-started' CHECK (status IN
                      ('not-started','doing','waiting-owner','done','dropped')),
  progress_note     TEXT,                    -- written for the owner, plain sentences
  drafter_key       TEXT,
  started_at        TEXT,
  done_at           TEXT,
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status change: change_note is required (on waiting-owner, owner_question will do);
  -- copied into the event, then cleared.
  change_actor_id   INTEGER REFERENCES actors(id),
  change_commit_sha TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40 OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note       TEXT,
  no_findings_reason TEXT,                  -- why this step closes no finding by design (e.g. the follow-up
                                            -- review that verifies the plan); v_coverage_problems skips it
  UNIQUE (plan_id, number)
);

-- The steps each step has to wait for.
-- Any other step of its own plan, earlier or later, as long as no cycle forms (tr_dep_order); v_step_order is the
-- order to work in.
CREATE TABLE step_dependencies (
  step_id            INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  depends_on_step_id INTEGER NOT NULL REFERENCES steps(id),
  PRIMARY KEY (step_id, depends_on_step_id),
  CHECK (step_id <> depends_on_step_id)
);

-- The points each plan step closes.
CREATE TABLE step_findings (
  step_id      INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  finding_id   INTEGER NOT NULL REFERENCES findings(id),
  multi_reason TEXT,          -- required when a finding is closed by more than one step
  PRIMARY KEY (step_id, finding_id)
);

-- The approved design goals each plan step serves.
CREATE TABLE step_goals (
  step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  goal_id INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (step_id, goal_id)
);

-- What each plan step changes: files, functions, tables, columns, settings, routes, what a user sees.
-- import-steps writes the touches a step declares (source declared) and the file and symbol of every location of
-- the points it closes (source finding); report-finding does the same for a step it adds. direction says how the
-- step changes the target; 'changes' when it does not say.
CREATE TABLE step_touches (
  step_id   INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('file','symbol','table','column','setting','route','behaviour')),
  target    TEXT NOT NULL CHECK (length(trim(target)) > 0),   -- demo:src/app.py, findings.severity, GET /api/x
  direction TEXT NOT NULL DEFAULT 'changes' CHECK (direction IN
              ('adds','removes','tightens','loosens','renames','changes')),
  source    TEXT NOT NULL CHECK (source IN ('declared','finding')),   -- steps.jsonl, or a closed finding's location
  PRIMARY KEY (step_id, kind, target, direction, source)
);

-- Pairs of plan steps that touch the same thing, and how each pair was judged.
-- `ledger.py conflicts` writes one row per pair and target (opposite = 1 when they change it in different
-- directions); resolve-conflict records the resolution and its note. late_step_id is the step that was added after
-- the plan was approved: its conflicts block only its own start (tr_step_start_conflicts), the others block the
-- plan's approval (tr_plan_approval_conflicts).
CREATE TABLE step_conflicts (
  id           INTEGER PRIMARY KEY,
  step_a_id    INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,   -- the lower-numbered step
  step_b_id    INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  target       TEXT NOT NULL,
  reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  opposite     INTEGER NOT NULL DEFAULT 0 CHECK (opposite IN (0,1)),   -- they change it in different directions
  late_step_id INTEGER REFERENCES steps(id),   -- the step added after the plan was approved, when one was
  resolution   TEXT NOT NULL DEFAULT 'none' CHECK (resolution IN ('none','no-conflict','order','merge','owner')),
  note         TEXT,
  gate_id      INTEGER REFERENCES gates(id),   -- the owner's gate, for resolution owner
  resolved_by_actor_id INTEGER REFERENCES actors(id),
  resolved_at  TEXT,
  CHECK (step_a_id <> step_b_id),
  CHECK (late_step_id IS NULL OR late_step_id IN (step_a_id, step_b_id)),
  CHECK (resolution = 'none' OR (length(trim(COALESCE(note, ''))) > 0 AND resolved_at IS NOT NULL)),
  CHECK (resolution <> 'owner' OR gate_id IS NOT NULL),
  UNIQUE (step_a_id, step_b_id, kind, target)
);

-- The commits that carried out each plan step.
-- One row per step and commit (`ledger.py add-commit`); pr_url when it went through a pull request.
CREATE TABLE step_commits (
  id            INTEGER PRIMARY KEY,
  step_id       INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  repository_id INTEGER REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                   AND commit_sha NOT GLOB '*[^0-9a-f]*'),  -- full hash, lowercase hex
  branch        TEXT,
  pr_url        TEXT CHECK (pr_url IS NULL OR pr_url GLOB 'https://*' OR pr_url GLOB 'http://*'),
  summary       TEXT,
  committed_at  TEXT,
  UNIQUE (step_id, commit_sha)
);

-- The tests that keep a fixed issue fixed.
-- failed_before_fix and passes_after_fix record the proof. A later review checks that each test is still there:
-- `confirm-guard` sets still_exists and last_confirmed_review_id. A fixed issue whose guard tests are all gone is
-- listed by v_unguarded_fixes.
CREATE TABLE guard_tests (
  id                      INTEGER PRIMARY KEY,
  issue_id                INTEGER NOT NULL REFERENCES issues(id),
  repository_id           INTEGER REFERENCES repositories(id),
  test_path               TEXT NOT NULL,
  test_name               TEXT NOT NULL,
  step_id                 INTEGER REFERENCES steps(id),
  added_commit_sha        TEXT CHECK (added_commit_sha IS NULL OR ((length(added_commit_sha) = 40 OR length(added_commit_sha) = 64) AND added_commit_sha NOT GLOB '*[^0-9a-f]*')),
  failed_before_fix       INTEGER CHECK (failed_before_fix IN (0,1)),
  passes_after_fix        INTEGER CHECK (passes_after_fix IN (0,1)),
  still_exists            INTEGER NOT NULL DEFAULT 1 CHECK (still_exists IN (0,1)),
  last_confirmed_review_id INTEGER REFERENCES reviews(id),
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (issue_id, test_path, test_name)
);

-- ---------- Feature proposals (schema 19, plan step 145) ---------------
-- What the features domain proposes the codebase could add to meet the owner's goals. A proposal is not a defect: no
-- severity, no issue, never counted in open issues, defect totals, regressions, coverage or the must-be-empty
-- checks. The owner picks or declines each (`feature-status`, or the review's one non-blocking feature-pick gate);
-- a picked one is built by a plan step (step_id), and a declined one is not proposed again for the same goal or
-- requirement (tr_feature_not_again). A goal changes by being replaced (a new code), so a replaced goal takes the
-- idea again.

-- One addition the codebase could make to serve a goal.
-- It names the goal or requirement it serves, what the code does toward it today and the evidence
-- (path:line, or 'absent'), the addition, its value and effort, risks, what it needs first, and the test that shows
-- it is built. decided_by_actor_id and decided_at are the last status change; feature_status_changes keeps them all.
CREATE TABLE feature_proposals (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE      -- FP-0001
                         CHECK (code GLOB 'FP-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'FP-*[^0-9]*'),
  review_id            INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id              INTEGER REFERENCES design_goals(id),     -- the approved goal it serves, or
  requirement_id       INTEGER REFERENCES requirements(id),     -- the active requirement it serves
  title                TEXT NOT NULL CHECK (length(trim(title)) > 0),
  today                TEXT NOT NULL CHECK (length(trim(today)) > 0),      -- what the code does toward it now
  evidence             TEXT NOT NULL CHECK (length(trim(evidence)) > 0),   -- path:line, or 'absent'
  proposal             TEXT NOT NULL CHECK (length(trim(proposal)) > 0),   -- the addition
  value                TEXT NOT NULL CHECK (value IN ('high','medium','low')),     -- how far it advances the goal
  effort               TEXT NOT NULL CHECK (effort IN ('small','medium','large')),
  risks                TEXT,
  depends              TEXT,                     -- what it needs first
  done_test            TEXT NOT NULL CHECK (length(trim(done_test)) > 0),  -- the test that shows it is built
  status               TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','picked','declined','built')),
  status_note          TEXT,                     -- why it was picked, declined or called built
  decided_by_actor_id  INTEGER REFERENCES actors(id),   -- who made the last status change
  decided_at           TEXT,
  gate_id              INTEGER REFERENCES gates(id),    -- the feature-pick gate the owner answered
  step_id              INTEGER REFERENCES steps(id),    -- the plan step that builds it
  proposed_by_actor_id INTEGER REFERENCES actors(id),
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK ((goal_id IS NULL) <> (requirement_id IS NULL)),
  CHECK (status = 'proposed' OR (decided_by_actor_id IS NOT NULL AND decided_at IS NOT NULL
                                 AND length(trim(COALESCE(status_note, ''))) > 0)),
  CHECK (step_id IS NULL OR status IN ('picked','built'))
);

-- Each status a feature proposal has had, with who set it, when and why.
-- Written by the feature triggers, never by hand.
CREATE TABLE feature_status_changes (
  id          INTEGER PRIMARY KEY,
  proposal_id INTEGER NOT NULL REFERENCES feature_proposals(id) ON DELETE RESTRICT,
  from_status TEXT,                      -- NULL when it was proposed
  to_status   TEXT NOT NULL,
  note        TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Tool runs and leads (schema 20, plan step 218) -----------

-- Each run of a picked tool on a review's commit, or its import from a file the tool wrote.
-- ledger.py tool-run runs it on an export of the reviewed commit (never the working tree) and records what ran, the
-- version printed and the one references/tools.lock pins, the exit code, how long it took and the output's sha256;
-- the output itself is kept in the review's folder (output_path). status: ran, imported (--from FILE), absent (not
-- installed: fallback says what the reviewer does instead), skipped (nothing to run on, or it would need the network:
-- fallback says why) or failed (it exited with an error and wrote nothing usable). A run is never changed.
CREATE TABLE tool_runs (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  repository_id  INTEGER REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                                   AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  tool           TEXT NOT NULL CHECK (length(trim(tool)) > 0),    -- the adapter's name (references/tools.md)
  domain_code    TEXT REFERENCES domains(code),                   -- the domain whose reviewer gets its leads
  status         TEXT NOT NULL CHECK (status IN ('ran','imported','absent','skipped','failed')),
  version        TEXT,                    -- the version the tool printed
  pinned_version TEXT,                    -- the version references/tools.lock pins
  command        TEXT,                    -- what ran, with {checkout} and {out} for the temporary paths
  exit_code      INTEGER,
  duration_ms    INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  output_format  TEXT CHECK (output_format IS NULL OR output_format IN
                   ('sarif','junit','lcov','cobertura','json','jsonl')),
  output_path    TEXT,                    -- the output as imported, in the review's folder
  output_sha256  TEXT CHECK (output_sha256 IS NULL OR (length(output_sha256) = 64
                                                       AND output_sha256 NOT GLOB '*[^0-9a-f]*')),
  rules_sha256   TEXT,                    -- the rule set's hash, for a tool run with rules
  online         INTEGER NOT NULL DEFAULT 0 CHECK (online IN (0,1)),   -- 1 = the run was allowed to go online
  fallback       TEXT,                    -- what the reviewer does instead, when the tool did not run
  note           TEXT,
  actor_id       INTEGER REFERENCES actors(id),
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (status NOT IN ('absent','skipped','failed') OR length(trim(COALESCE(fallback, ''))) > 0),
  CHECK (status <> 'ran' OR (command IS NOT NULL AND exit_code IS NOT NULL))
);
CREATE INDEX ix_fk_tool_runs_review_id     ON tool_runs(review_id);
CREATE INDEX ix_fk_tool_runs_repository_id ON tool_runs(repository_id);
CREATE INDEX ix_fk_tool_runs_domain_code   ON tool_runs(domain_code);
CREATE INDEX ix_fk_tool_runs_actor_id      ON tool_runs(actor_id);

-- Each result a tool run produced, kept as a lead for the domain's reviewer, never as a finding.
-- A lead (LD-0001) is addressed when a finding of the same review cites it (cite-lead, or a location of a finding on
-- the same lines), or a reviewer dismisses it with a reason (dismiss-lead); v_unaddressed_tool_results lists the rest.
-- fingerprint hashes tool, rule, path, message and the occurrence, not the line, so the same lead in a later review
-- is recognised: its dismissal is carried forward (carried_from_id). Only the disposition columns ever change.
CREATE TABLE tool_results (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE     -- LD-0001
                         CHECK (code GLOB 'LD-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'LD-*[^0-9]*'),
  tool_run_id          INTEGER NOT NULL REFERENCES tool_runs(id) ON DELETE RESTRICT,
  domain_code          TEXT REFERENCES domains(code),
  rule                 TEXT NOT NULL CHECK (length(trim(rule)) > 0),
  level                TEXT NOT NULL CHECK (level IN ('error','warning','note','none')),
  path                 TEXT,             -- repository-relative, forward slashes; none for a whole-run result
  line_start           INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end             INTEGER CHECK (line_end IS NULL OR (line_start IS NOT NULL AND line_end >= line_start)),
  message              TEXT NOT NULL CHECK (length(trim(message)) > 0),
  fingerprint          TEXT NOT NULL,    -- tool, rule, path, message and occurrence, hashed: the same lead later
  finding_id           INTEGER REFERENCES findings(id),     -- the finding that cites it (cite-lead)
  dismissed_reason     TEXT,             -- why it is not a finding (dismiss-lead)
  disposed_by_actor_id INTEGER REFERENCES actors(id),
  disposed_at          TEXT,
  carried_from_id      INTEGER REFERENCES tool_results(id),  -- the earlier lead whose dismissal it carries
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (tool_run_id, fingerprint),
  CHECK (dismissed_reason IS NULL OR length(trim(dismissed_reason)) > 0),
  CHECK ((finding_id IS NULL AND dismissed_reason IS NULL) = (disposed_at IS NULL))
);
CREATE INDEX ix_fk_tool_results_tool_run_id          ON tool_results(tool_run_id);
CREATE INDEX ix_fk_tool_results_domain_code          ON tool_results(domain_code);
CREATE INDEX ix_fk_tool_results_finding_id           ON tool_results(finding_id);
CREATE INDEX ix_fk_tool_results_disposed_by_actor_id ON tool_results(disposed_by_actor_id);
CREATE INDEX ix_fk_tool_results_carried_from_id      ON tool_results(carried_from_id);
CREATE INDEX ix_tool_results_fingerprint             ON tool_results(fingerprint);

-- ---------- Measurements over time ------------------------------------

-- Test runs recorded as evidence, with their results.
-- purpose: baseline (the suite before a review or a plan starts), targeted (chosen tests), flake-check (repeated
-- runs to find unstable tests), mutation (a mutation run), before-fix (the proof test failing), after-fix (the same
-- test passing), ci (a run by CI).
CREATE TABLE test_runs (
  id            INTEGER PRIMARY KEY,
  review_id     INTEGER REFERENCES reviews(id),
  step_id       INTEGER REFERENCES steps(id),
  repository_id INTEGER REFERENCES repositories(id),
  commit_sha    TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64) AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  command       TEXT NOT NULL,
  purpose       TEXT NOT NULL CHECK (purpose IN
                  ('baseline','targeted','flake-check','mutation','before-fix','after-fix','ci')),
  passed        INTEGER,
  failed        INTEGER,
  skipped       INTEGER,
  errored       INTEGER,
  exit_code     INTEGER,
  duration_ms   INTEGER,
  output_path   TEXT,
  notes         TEXT,
  started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The names of the numbers a review measures, with the label shown for each.
-- unit is the default unit; better says which way is an improvement. Seeded with the names the domains ask for; a
-- metric recorded under a new name gets a row here from tr_metric_defined (label = the name with spaces,
-- capitalized), which can then be given a better label.
CREATE TABLE metric_defs (
  name   TEXT PRIMARY KEY,                 -- the stable key used in metrics.name
  label  TEXT NOT NULL,
  unit   TEXT,
  better TEXT NOT NULL DEFAULT 'none' CHECK (better IN ('higher','lower','none'))
);

-- Numbers measured in a review, kept to follow them over time.
-- For example mutation score, coverage %, cases asserted, page weight, queries per page, p95 job time, dependency
-- count ...
CREATE TABLE metrics (
  id            INTEGER PRIMARY KEY,
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code   TEXT REFERENCES domains(code),
  repository_id INTEGER REFERENCES repositories(id),
  name          TEXT NOT NULL REFERENCES metric_defs(name),  -- stable key, e.g. 'mutation_score'
  scope         TEXT NOT NULL DEFAULT 'all',  -- module, page, job ...
  value         REAL NOT NULL,
  unit          TEXT,
  method        TEXT NOT NULL CHECK (method IN ('measured','counted','estimated')),
  note          TEXT,
  recorded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (review_id, name, scope, repository_id)
);

-- A metric recorded under a name the catalog lacks gets a metric_defs row (so every metric has a label).
CREATE TRIGGER tr_metric_defined AFTER INSERT ON metrics BEGIN
  INSERT OR IGNORE INTO metric_defs(name, label, unit)
    VALUES (NEW.name, upper(substr(NEW.name, 1, 1)) || replace(substr(NEW.name, 2), '_', ' '), NEW.unit);
END;

-- The same when a metric is renamed.
CREATE TRIGGER tr_metric_renamed AFTER UPDATE OF name ON metrics BEGIN
  INSERT OR IGNORE INTO metric_defs(name, label, unit)
    VALUES (NEW.name, upper(substr(NEW.name, 1, 1)) || replace(substr(NEW.name, 2), '_', ' '), NEW.unit);
END;

-- ---------- Files and links produced ---------------------------------

-- Files and links a review or plan produced.
-- kind: brief, report (one reviewer's), index, summary-doc, plan-file, progress-page (the hub; its url is copied to
-- plans.page_url), probe-script or other. sha256 is the file's hash when it was recorded. At least one of path and
-- url is set.
CREATE TABLE artifacts (
  id              INTEGER PRIMARY KEY,
  review_id       INTEGER REFERENCES reviews(id),
  plan_id         INTEGER REFERENCES plans(id),
  reviewer_run_id INTEGER REFERENCES reviewer_runs(id),
  kind            TEXT NOT NULL CHECK (kind IN
                    ('brief','report','index','summary-doc','plan-file','progress-page',
                     'probe-script','other')),
  path            TEXT,                    -- repo-relative if kept in the repo
  url             TEXT,
  sha256          TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (path IS NOT NULL OR url IS NOT NULL)
);

-- ---------- History of everything that changes state ------------------

-- The history: every status change and every other tracked change, in order.
-- Append-only (G-04): triggers refuse every UPDATE and DELETE on events (tr_events_no_update/no_delete).
-- Which tables write events (plan step 73; a test keeps these two lists equal to the triggers and the tables):
-- Logged: reviews, reviewer_runs, issues, issue_relations, findings, review_issue_checks, decisions, gates, plans,
-- steps, step_dependencies, step_findings, metrics, design_goals, interview_questions, interview_answers,
-- goal_sources, step_goals.
-- Not logged: finding_locations, finding_goals, finding_requirements, review_requirement_checks, test_runs,
-- artifacts, step_commits, guard_tests, decision_issues, plan_reviews, phases, review_repositories, review_goals,
-- review_domains, review_environment, review_actors, actors, repositories, domains, domain_rules,
-- domain_checks, checklist_answers, step_touches, step_conflicts, themes, feature_proposals,
-- feature_status_changes, requirements, metric_defs, tool_runs, tool_results,
-- meta, schema_migrations, ledger_checks, issue_statuses, severities, match_types, relation_types, gate_kinds,
-- goal_statuses, enum_labels.
-- Why not: feature_proposals keep their own history in feature_status_changes; finding_locations, finding_goals and
-- finding_requirements belong to their finding (logged when it is
-- created) and cannot change but for fill-refs; review_requirement_checks, test_runs, artifacts and tool_runs are
-- themselves timestamped records written once, and a tool result's disposition carries its own actor and time; step_commits, guard_tests, decision_issues, plan_reviews and phases are plan
-- and evidence links whose rows are their own record; the rest are setup and configuration (who, which
-- repositories, the catalogs and word lists, requirements, the schema's own bookkeeping), whose
-- changes do not drive an issue's history.
CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('review','issue','step','plan','decision','finding','gate',
                                                   'relation','check','step-finding','goal','question')),
  entity_id   INTEGER NOT NULL,
  event       TEXT NOT NULL,              -- status-changed, severity-changed, opened, seen, linked, note ...
  from_value  TEXT,
  to_value    TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  review_id   INTEGER REFERENCES reviews(id),
  step_id     INTEGER REFERENCES steps(id),
  commit_sha  TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64) AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  note        TEXT,
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Indexes -----------------------------------------------------

CREATE INDEX ix_findings_review      ON findings(review_id);
-- The columns the per-review counts read (v_review_totals), so a list of reviews never reads the findings themselves;
-- issue_id too, so v_match_problems is counted from the index (schema 21).
CREATE INDEX ix_findings_review_counts ON findings(review_id, verification, severity, match_type, issue_id);
CREATE INDEX ix_findings_issue       ON findings(issue_id);
CREATE INDEX ix_findings_fingerprint ON findings(fingerprint);
CREATE INDEX ix_findings_slug        ON findings(domain_code, theme_code, sub);
CREATE INDEX ix_locations_finding    ON finding_locations(finding_id);
CREATE INDEX ix_locations_path       ON finding_locations(path, symbol);
CREATE INDEX ix_issues_status        ON issues(status);
CREATE INDEX ix_issues_fingerprint   ON issues(fingerprint);
CREATE INDEX ix_issues_slug          ON issues(domain_code, theme_code, sub);
CREATE INDEX ix_relations_to         ON issue_relations(to_issue_id);
CREATE INDEX ix_checks_issue         ON review_issue_checks(issue_id);
CREATE INDEX ix_steps_phase          ON steps(phase_id);
CREATE INDEX ix_steps_status         ON steps(plan_id, status);
CREATE INDEX ix_step_findings_find   ON step_findings(finding_id, step_id);
CREATE INDEX ix_deps_on              ON step_dependencies(depends_on_step_id);
CREATE INDEX ix_guard_issue          ON guard_tests(issue_id);
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);
CREATE INDEX ix_metrics_name         ON metrics(name, scope);
CREATE INDEX ix_runs_review          ON reviewer_runs(review_id);
CREATE INDEX ix_gates_status         ON gates(status);
-- For check's views (schema 21, plan step 92): each lets a view read an index instead of every row.
-- The status events of each issue and gate in order (v_history_gaps, v_match_problems).
CREATE INDEX ix_events_history       ON events(entity_type, entity_id, id, event, to_value)
  WHERE event IN ('opened','status-changed');
-- The issues of a domain by status (v_unchecked_known_issues).
CREATE INDEX ix_issues_domain_known  ON issues(domain_code, status, first_seen_review_id, opened_at);
-- The few points found during fix work (v_unplanned_discoveries).
CREATE INDEX ix_findings_discovered  ON findings(discovered_in_step_id) WHERE discovered_in_step_id IS NOT NULL;
-- Locations with a problem of their own (v_untraceable_refs repeats this WHERE word for word, so the planner uses
-- the index; edit both together).
CREATE INDEX ix_locations_untraceable ON finding_locations(finding_id)
  WHERE commit_sha IS NULL OR (line_start IS NULL AND whole_file = 0)
     OR ((excerpt IS NULL OR excerpt = '') AND excerpt_status IS NOT 'not-text')
     OR (excerpt_status IS NULL AND line_start IS NOT NULL
         AND length(excerpt) - length(replace(excerpt, char(10), '')) + 1
             <> COALESCE(line_end, line_start) - line_start + 1
         AND NOT (substr(excerpt, -1) = char(10)
                  AND length(excerpt) - length(replace(excerpt, char(10), ''))
                      = COALESCE(line_end, line_start) - line_start + 1));
-- Uniqueness the table constraints cannot express (schema 3): one metric per review, name, scope and repository
-- even when repository_id is NULL; at most one primary location per finding; one repository per path.
CREATE UNIQUE INDEX ux_metrics_key            ON metrics(review_id, name, scope, COALESCE(repository_id, 0));
CREATE UNIQUE INDEX ux_locations_one_primary  ON finding_locations(finding_id) WHERE role = 'primary';
CREATE UNIQUE INDEX ux_repositories_path      ON repositories(path);
CREATE INDEX ix_fk_reviews_change_actor_id    ON reviews(change_actor_id);
CREATE INDEX ix_fk_plans_change_actor_id      ON plans(change_actor_id);
CREATE INDEX ix_fk_gates_change_actor_id      ON gates(change_actor_id);
CREATE INDEX ix_fk_gates_answered_by_actor_id ON gates(answered_by_actor_id);
-- A leading index for every foreign-key column (a parent delete or update checks its children through it, and
-- views look gates up by step and decision); dev/tests/test_p4_views_indexes.py keeps the list complete.
CREATE INDEX ix_fk_actors_parent_actor_id            ON actors(parent_actor_id);
CREATE INDEX ix_fk_design_goals_superseded_by        ON design_goals(superseded_by);
CREATE INDEX ix_fk_design_goals_domain_code          ON design_goals(domain_code);
CREATE INDEX ix_fk_design_goals_status               ON design_goals(status);
CREATE INDEX ix_fk_design_goals_approved_by_actor_id ON design_goals(approved_by_actor_id);
CREATE INDEX ix_fk_design_goals_review_id            ON design_goals(review_id);
CREATE INDEX ix_fk_design_goals_gate_id              ON design_goals(gate_id);
CREATE INDEX ix_fk_design_goals_change_actor_id      ON design_goals(change_actor_id);
CREATE INDEX ix_fk_interview_questions_review_id         ON interview_questions(review_id);
CREATE INDEX ix_fk_interview_questions_domain_code       ON interview_questions(domain_code);
CREATE INDEX ix_fk_interview_questions_asked_by_actor_id ON interview_questions(asked_by_actor_id);
CREATE INDEX ix_fk_interview_questions_gate_id           ON interview_questions(gate_id);
CREATE INDEX ix_fk_interview_answers_question_id          ON interview_answers(question_id);
CREATE INDEX ix_fk_interview_answers_answered_by_actor_id ON interview_answers(answered_by_actor_id);
CREATE INDEX ix_fk_goal_sources_question_id ON goal_sources(question_id);
CREATE INDEX ix_fk_step_goals_goal_id ON step_goals(goal_id);
CREATE INDEX ix_fk_reviews_approved_by_actor_id      ON reviews(approved_by_actor_id);
CREATE INDEX ix_fk_reviews_manager_actor_id          ON reviews(manager_actor_id);
CREATE INDEX ix_fk_reviews_previous_review_id        ON reviews(previous_review_id);
CREATE INDEX ix_fk_reviews_requested_by_actor_id     ON reviews(requested_by_actor_id);
CREATE INDEX ix_fk_review_repositories_repository_id ON review_repositories(repository_id);
CREATE INDEX ix_fk_review_goals_goal_id              ON review_goals(goal_id);
CREATE INDEX ix_fk_domain_checks_domain_code       ON domain_checks(domain_code);
CREATE INDEX ix_fk_domain_checks_severity          ON domain_checks(severity);
CREATE INDEX ix_fk_checklist_answers_check_code    ON checklist_answers(check_code);
CREATE INDEX ix_step_touches_target                  ON step_touches(kind, target);
CREATE INDEX ix_fk_step_conflicts_step_b_id           ON step_conflicts(step_b_id);
CREATE INDEX ix_fk_step_conflicts_late_step_id        ON step_conflicts(late_step_id);
CREATE INDEX ix_fk_step_conflicts_gate_id             ON step_conflicts(gate_id);
CREATE INDEX ix_fk_step_conflicts_resolved_by_actor_id ON step_conflicts(resolved_by_actor_id);
CREATE INDEX ix_fk_review_domains_domain_code        ON review_domains(domain_code);
CREATE INDEX ix_fk_reviewer_runs_actor_id            ON reviewer_runs(actor_id);
CREATE INDEX ix_fk_reviewer_runs_domain_code         ON reviewer_runs(domain_code);
CREATE INDEX ix_fk_review_actors_actor_id            ON review_actors(actor_id);
CREATE INDEX ix_fk_requirements_first_review_id      ON requirements(first_review_id);
CREATE INDEX ix_fk_requirements_superseded_by        ON requirements(superseded_by);
CREATE INDEX ix_fk_issues_change_actor_id            ON issues(change_actor_id);
CREATE INDEX ix_fk_issues_change_review_id           ON issues(change_review_id);
CREATE INDEX ix_fk_issues_change_step_id             ON issues(change_step_id);
CREATE INDEX ix_fk_issues_first_seen_review_id       ON issues(first_seen_review_id);
CREATE INDEX ix_fk_issues_last_checked_review_id     ON issues(last_checked_review_id);
CREATE INDEX ix_fk_issues_last_seen_review_id        ON issues(last_seen_review_id);
CREATE INDEX ix_fk_issues_theme_code                 ON issues(theme_code);
CREATE INDEX ix_fk_issue_relations_established_by_actor_id ON issue_relations(established_by_actor_id);
CREATE INDEX ix_fk_issue_relations_established_in_review_id ON issue_relations(established_in_review_id);
CREATE INDEX ix_findings_run                         ON findings(reviewer_run_id);
CREATE INDEX ix_fk_findings_theme_code               ON findings(theme_code);
CREATE INDEX ix_fk_findings_verified_by_actor_id     ON findings(verified_by_actor_id);
CREATE INDEX ix_fk_findings_discovered_in_step_id    ON findings(discovered_in_step_id);
CREATE INDEX ix_fk_finding_locations_repository_id   ON finding_locations(repository_id);
CREATE INDEX ix_fk_finding_goals_goal_id             ON finding_goals(goal_id);
CREATE INDEX ix_fk_finding_requirements_requirement_id ON finding_requirements(requirement_id);
CREATE INDEX ix_fk_review_requirement_checks_finding_id ON review_requirement_checks(finding_id);
CREATE INDEX ix_fk_review_requirement_checks_requirement_id ON review_requirement_checks(requirement_id);
CREATE INDEX ix_fk_review_issue_checks_actor_id      ON review_issue_checks(actor_id);
CREATE INDEX ix_ric_finding                          ON review_issue_checks(finding_id);
CREATE INDEX ix_fk_decisions_asked_by_actor_id       ON decisions(asked_by_actor_id);
CREATE INDEX ix_fk_decisions_decided_by_actor_id     ON decisions(decided_by_actor_id);
CREATE INDEX ix_fk_decisions_review_id               ON decisions(review_id);
CREATE INDEX ix_fk_decisions_step_id                 ON decisions(step_id);
CREATE INDEX ix_fk_decision_issues_issue_id          ON decision_issues(issue_id);
CREATE INDEX ix_gates_decision                       ON gates(decision_id);
CREATE INDEX ix_fk_gates_issue_id                    ON gates(issue_id);
CREATE INDEX ix_fk_gates_opened_by_actor_id          ON gates(opened_by_actor_id);
CREATE INDEX ix_fk_gates_plan_id                     ON gates(plan_id);
CREATE INDEX ix_fk_gates_review_id                   ON gates(review_id);
CREATE INDEX ix_gates_step                           ON gates(step_id);
CREATE INDEX ix_fk_plans_approved_by_actor_id        ON plans(approved_by_actor_id);
CREATE INDEX ix_fk_plans_superseded_by               ON plans(superseded_by);
CREATE INDEX ix_fk_plan_reviews_review_id            ON plan_reviews(review_id);
CREATE INDEX ix_fk_steps_change_actor_id             ON steps(change_actor_id);
CREATE INDEX ix_fk_step_commits_repository_id        ON step_commits(repository_id);
CREATE INDEX ix_fk_guard_tests_last_confirmed_review_id ON guard_tests(last_confirmed_review_id);
CREATE INDEX ix_fk_guard_tests_repository_id         ON guard_tests(repository_id);
CREATE INDEX ix_fk_guard_tests_step_id               ON guard_tests(step_id);
CREATE INDEX ix_fk_test_runs_repository_id           ON test_runs(repository_id);
CREATE INDEX ix_fk_test_runs_review_id               ON test_runs(review_id);
CREATE INDEX ix_fk_test_runs_step_id                 ON test_runs(step_id);
CREATE INDEX ix_fk_metrics_domain_code               ON metrics(domain_code);
CREATE INDEX ix_fk_metrics_repository_id             ON metrics(repository_id);
CREATE INDEX ix_fk_artifacts_plan_id                 ON artifacts(plan_id);
CREATE INDEX ix_fk_artifacts_review_id               ON artifacts(review_id);
CREATE INDEX ix_fk_artifacts_reviewer_run_id         ON artifacts(reviewer_run_id);
CREATE INDEX ix_fk_events_actor_id                   ON events(actor_id);
CREATE INDEX ix_fk_events_review_id                  ON events(review_id);
CREATE INDEX ix_fk_events_step_id                    ON events(step_id);
-- The keys to the lookup tables (schema 6), so a change to a lookup row finds its users without a scan.
CREATE INDEX ix_fk_issues_severity                   ON issues(severity);
CREATE INDEX ix_fk_issue_relations_relation          ON issue_relations(relation);
CREATE INDEX ix_fk_findings_severity                 ON findings(severity);
CREATE INDEX ix_fk_findings_match_type               ON findings(match_type);
CREATE INDEX ix_fk_gates_kind                        ON gates(kind);
CREATE INDEX ix_fk_feature_proposals_review_id            ON feature_proposals(review_id);
CREATE INDEX ix_fk_feature_proposals_goal_id              ON feature_proposals(goal_id);
CREATE INDEX ix_fk_feature_proposals_requirement_id       ON feature_proposals(requirement_id);
CREATE INDEX ix_fk_feature_proposals_decided_by_actor_id  ON feature_proposals(decided_by_actor_id);
CREATE INDEX ix_fk_feature_proposals_gate_id              ON feature_proposals(gate_id);
CREATE INDEX ix_fk_feature_proposals_step_id              ON feature_proposals(step_id);
CREATE INDEX ix_fk_feature_proposals_proposed_by_actor_id ON feature_proposals(proposed_by_actor_id);
CREATE INDEX ix_fk_feature_status_changes_proposal_id ON feature_status_changes(proposal_id);
CREATE INDEX ix_fk_feature_status_changes_actor_id    ON feature_status_changes(actor_id);
CREATE UNIQUE INDEX ux_gates_one_feature_pick ON gates(review_id) WHERE kind = 'feature-pick' AND status <> 'withdrawn';

-- ---------- Triggers ----------------------------------------------------
-- (schema 3, plan steps 54-83) History is append-only, every tracked change writes an event, status and
-- severity changes need fresh context (change_note) and clear it after logging, and the issue lifecycle,
-- plan structure, review and plan approvals and gate answers are enforced here, not only in SKILL.md.

-- ----- Issues

-- Codes: the prefix and the next number, zero-padded to 4 digits (ISS-0001, ISS-0002 ...); never changed or reused.
CREATE TRIGGER tr_issue_code BEFORE INSERT ON issues
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'ISS-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'ISS-*[^0-9]*' AND (
     NEW.code IS NOT 'ISS-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER))
  OR CAST(substr(NEW.code, 5) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 5) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM issues
        WHERE code = 'ISS-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'issue codes are sequential: ISS- and the next number (ISS-0001, ISS-0002 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_issue_code_fixed BEFORE UPDATE OF code ON issues WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- A retired domain takes no new issues.
CREATE TRIGGER tr_issue_domain_active BEFORE INSERT ON issues
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

-- Logs 'opened'. The INSERT may carry change_actor_id, change_step_id, change_commit_sha and change_note: they go
-- into the event and are then cleared (tr_issue_context_cleared_ins).
CREATE TRIGGER tr_issue_opened AFTER INSERT ON issues BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'opened', NEW.status, NEW.change_actor_id,
          NEW.first_seen_review_id, NEW.change_step_id, NEW.change_commit_sha, NEW.change_note);
END;

-- A status or severity change needs its reason in the same UPDATE (change_note), so history never logs a
-- stale or empty context.
CREATE TRIGGER tr_issue_change_needs_note BEFORE UPDATE OF status, severity ON issues
WHEN (OLD.status IS NOT NEW.status OR OLD.severity IS NOT NEW.severity) AND NEW.change_note IS NULL BEGIN
  SELECT RAISE(ABORT, 'give change_note with a status or severity change');
END;

-- Fixed means the after state is known.
CREATE TRIGGER tr_issue_fixed_needs_after BEFORE UPDATE OF status ON issues
WHEN NEW.status = 'fixed' AND OLD.status IS NOT 'fixed' AND NEW.after_state IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue set fixed needs after_state (what the system does now)');
END;

-- Accepting a risk or declining a fix is a decision: its reason goes in resolution (v_closed_without_reason lists
-- older rows without one).
CREATE TRIGGER tr_issue_closed_needs_reason BEFORE UPDATE OF status, resolution ON issues
WHEN NEW.status IN ('accepted-risk','wont-fix') AND NULLIF(trim(NEW.resolution), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue closed as accepted-risk or wont-fix needs a resolution (the reason)');
END;

-- The same for an issue inserted already closed.
CREATE TRIGGER tr_issue_closed_needs_reason_ins BEFORE INSERT ON issues
WHEN NEW.status IN ('accepted-risk','wont-fix') AND NULLIF(trim(NEW.resolution), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue closed as accepted-risk or wont-fix needs a resolution (the reason)');
END;

-- Only a later review verifies a fix: entering 'verified' needs a 'fixed' check by the review named in
-- change_review_id (tr_issue_checked sets both when it verifies), and that review started after every review that
-- raised the issue (its first and last sightings). Reviews are compared with each other, not with resolved_at,
-- since review times may be recorded rather than taken from the clock.
CREATE TRIGGER tr_issue_verified_needs_check BEFORE UPDATE OF status ON issues
WHEN NEW.status = 'verified' AND OLD.status IS NOT 'verified'
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c JOIN reviews r ON r.id = c.review_id
                  WHERE c.issue_id = NEW.id AND c.review_id = NEW.change_review_id AND c.outcome = 'fixed'
                    AND julianday(r.started_at) > (SELECT max(julianday(x.started_at)) FROM reviews x
                                                    WHERE x.id IN (OLD.first_seen_review_id,
                                                                   OLD.last_seen_review_id))) BEGIN
  SELECT RAISE(ABORT, 'an issue is verified only by a later review''s fixed check (record it in review_issue_checks)');
END;

-- resolved_at is set on entering a resolved status (kept from fixed to verified) and cleared on reopening;
-- verified_at is set on verified and cleared on leaving it. A regression moves the old after_state into the
-- event note and clears it.
CREATE TRIGGER tr_issue_status AFTER UPDATE OF status ON issues
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.change_actor_id, NEW.change_review_id, NEW.change_step_id, NEW.change_commit_sha,
          CASE WHEN NEW.status = 'regressed' AND NEW.after_state IS NOT NULL
               THEN COALESCE(NEW.change_note || ' ', '') || '(after_state was: ' || NEW.after_state || ')'
               ELSE NEW.change_note END);
  UPDATE issues SET
    resolved_at = CASE WHEN NEW.status = 'verified' THEN COALESCE(resolved_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                       WHEN NEW.status IN (SELECT value FROM issue_statuses WHERE resolved = 1)
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       ELSE NULL END,
    verified_at = CASE WHEN NEW.status = 'verified' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
    after_state = CASE WHEN NEW.status = 'regressed' THEN NULL ELSE after_state END
  WHERE id = NEW.id;
END;

-- Logs 'severity-changed' with the change context (change_note is required: tr_issue_change_needs_note).
CREATE TRIGGER tr_issue_severity AFTER UPDATE OF severity ON issues
WHEN OLD.severity IS NOT NEW.severity BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'severity-changed', OLD.severity, NEW.severity,
          NEW.change_actor_id, NEW.change_review_id, NEW.change_step_id,
          NEW.change_commit_sha, NEW.change_note);
END;

-- Text edits are history too: one '<field>-changed' event with the old and new text.
CREATE TRIGGER tr_issue_title_changed AFTER UPDATE OF title ON issues WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.change_review_id,
          NEW.change_step_id, NEW.change_note);
END;

-- Logs 'before-state-changed'.
CREATE TRIGGER tr_issue_before_changed AFTER UPDATE OF before_state ON issues
WHEN OLD.before_state IS NOT NEW.before_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'before-state-changed', OLD.before_state, NEW.before_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

-- Logs 'current-state-changed'.
CREATE TRIGGER tr_issue_current_changed AFTER UPDATE OF current_state ON issues
WHEN OLD.current_state IS NOT NEW.current_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'current-state-changed', OLD.current_state, NEW.current_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

-- Logs 'after-state-changed'.
CREATE TRIGGER tr_issue_after_changed AFTER UPDATE OF after_state ON issues
WHEN OLD.after_state IS NOT NEW.after_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'after-state-changed', OLD.after_state, NEW.after_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

-- Logs 'recommendation-changed'.
CREATE TRIGGER tr_issue_recommendation_changed AFTER UPDATE OF recommendation ON issues
WHEN OLD.recommendation IS NOT NEW.recommendation BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'recommendation-changed', OLD.recommendation, NEW.recommendation, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

-- Logs 'resolution-changed'.
CREATE TRIGGER tr_issue_resolution_changed AFTER UPDATE OF resolution ON issues
WHEN OLD.resolution IS NOT NEW.resolution BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'resolution-changed', OLD.resolution, NEW.resolution, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

-- An update that leaves updated_at alone sets it to now. The trigger's own UPDATE does not fire it again only
-- because recursive triggers are off (SQLite's default; ledger.py never turns them on).
CREATE TRIGGER tr_issue_touch AFTER UPDATE ON issues
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE issues SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- After the events above are written (each reads NEW, so the order of triggers does not matter), the change
-- context is cleared: a later change must give its own.
CREATE TRIGGER tr_issue_context_cleared AFTER UPDATE ON issues
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_review_id IS NOT NULL OR NEW.change_step_id IS NOT NULL
  OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE issues SET change_actor_id = NULL, change_review_id = NULL, change_step_id = NULL,
                    change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- The same after an INSERT that carried context (tr_issue_opened has logged it).
CREATE TRIGGER tr_issue_context_cleared_ins AFTER INSERT ON issues
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_review_id IS NOT NULL OR NEW.change_step_id IS NOT NULL
  OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE issues SET change_actor_id = NULL, change_review_id = NULL, change_step_id = NULL,
                    change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- Refuses DELETE: an issue is closed with a status.
CREATE TRIGGER tr_issue_no_delete BEFORE DELETE ON issues BEGIN
  SELECT RAISE(ABORT, 'history is append-only: issues cannot be deleted; close one with a status instead');
END;

-- ----- Findings and matching

-- A finding is inserted unmatched and matched by an UPDATE of issue_id, so tr_finding_matched always runs.
CREATE TRIGGER tr_finding_insert_unmatched BEFORE INSERT ON findings WHEN NEW.issue_id IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'insert the finding first, then set issue_id (matching runs on that UPDATE)');
END;

-- Points are raised only once the owner approved the review.
CREATE TRIGGER tr_finding_review_approved BEFORE INSERT ON findings
WHEN (SELECT status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NULL
        FROM reviews WHERE id = NEW.review_id) BEGIN
  SELECT RAISE(ABORT, 'findings need an approved review (approved_at set, status past awaiting-approval)');
END;

-- A finding's reviewer run is a run of the finding's own review.
CREATE TRIGGER tr_finding_run_same_review BEFORE INSERT ON findings
WHEN NEW.reviewer_run_id IS NOT NULL
 AND (SELECT review_id FROM reviewer_runs WHERE id = NEW.reviewer_run_id) IS NOT NEW.review_id BEGIN
  SELECT RAISE(ABORT, 'the reviewer run belongs to another review');
END;

-- A retired domain takes no new findings.
CREATE TRIGGER tr_finding_domain_active BEFORE INSERT ON findings
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

-- Severity T means a missing test, nothing else (a finding's severity and kind never change after insert).
CREATE TRIGGER tr_finding_test_severity BEFORE INSERT ON findings
WHEN NEW.severity = 'T' AND NEW.kind IS NOT 'test' BEGIN
  SELECT RAISE(ABORT, 'severity T is for a missing test: give the finding kind test, or another severity');
END;

-- Logs 'created' with the point id and the summary.
CREATE TRIGGER tr_finding_created AFTER INSERT ON findings BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('finding', NEW.id, 'created', NEW.point_id, NULL, NEW.review_id, NEW.summary);
END;

-- What a review saw stays as it saw it: only the matching (issue_id, match_*) and verification (verification*)
-- columns of a finding change after it is written.
CREATE TRIGGER tr_finding_immutable BEFORE UPDATE ON findings
WHEN OLD.review_id IS NOT NEW.review_id OR OLD.reviewer_run_id IS NOT NEW.reviewer_run_id
  OR OLD.point_id IS NOT NEW.point_id OR OLD.severity IS NOT NEW.severity OR OLD.kind IS NOT NEW.kind
  OR OLD.domain_code IS NOT NEW.domain_code OR OLD.theme_code IS NOT NEW.theme_code OR OLD.sub IS NOT NEW.sub
  OR OLD.summary IS NOT NEW.summary OR OLD.detail IS NOT NEW.detail OR OLD.before_state IS NOT NEW.before_state
  OR OLD.recommendation IS NOT NEW.recommendation OR OLD.recommended_test IS NOT NEW.recommended_test
  OR OLD.evidence IS NOT NEW.evidence OR OLD.where_report IS NOT NEW.where_report
  OR OLD.fingerprint IS NOT NEW.fingerprint OR OLD.related_point_ids IS NOT NEW.related_point_ids
  OR OLD.created_at IS NOT NEW.created_at BEGIN
  SELECT RAISE(ABORT, 'history is append-only: only issue_id, match_* and verification* of a finding can change');
END;

-- Refuses DELETE: a wrong finding is marked verification = 'false'.
CREATE TRIGGER tr_finding_no_delete BEFORE DELETE ON findings BEGIN
  SELECT RAISE(ABORT, 'history is append-only: findings cannot be deleted; mark a wrong one verification = false');
END;

-- match_type 'regression' is for an issue that was fixed or verified (or already regressed in this review);
-- a closed issue raised again is a 'recurrence'.
CREATE TRIGGER tr_finding_regression_guard BEFORE UPDATE OF issue_id, match_type ON findings
WHEN NEW.match_type = 'regression' AND NEW.issue_id IS NOT NULL
 AND (SELECT status FROM issues WHERE id = NEW.issue_id) NOT IN (SELECT value FROM issue_statuses WHERE fixed = 1 OR value = 'regressed') BEGIN
  SELECT RAISE(ABORT, 'match_type regression needs a fixed or verified issue; use recurrence, still-present or changed');
END;

-- One review cannot both report an issue fixed (a check) and raise it again (a real finding): the result
-- would depend on which was written first.
CREATE TRIGGER tr_finding_match_vs_fixed_check BEFORE UPDATE OF issue_id ON findings
WHEN NEW.issue_id IS NOT NULL AND OLD.issue_id IS NOT NEW.issue_id AND NEW.verification <> 'false'
 AND EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = NEW.review_id AND c.issue_id = NEW.issue_id
               AND c.outcome = 'fixed') BEGIN
  SELECT RAISE(ABORT, 'this review recorded the issue fixed; a finding of the same review cannot be matched to it');
END;

-- Matching (issue_id set or changed by an UPDATE). Both the issue it leaves and the issue it joins get
-- times_seen, last_seen and last_checked recomputed from their real (not false) findings and checks, by review
-- start time, so they never move backwards. On the joined issue: current_state follows the finding; a
-- 'regression' match regresses a fixed or verified issue; a 'recurrence' or 'regression' match reopens an
-- obsolete, wont-fix or duplicate issue (an accepted-risk one stays, with a 'recurred' event); a false finding
-- changes nothing but its 'seen' event (to_value 'false'). The issue left behind gets an 'unlinked' event.
CREATE TRIGGER tr_finding_matched AFTER UPDATE OF issue_id ON findings
WHEN OLD.issue_id IS NOT NEW.issue_id BEGIN
  UPDATE issues SET
    times_seen = max(1, (SELECT count(DISTINCT f.review_id) FROM findings f
                          WHERE f.issue_id = OLD.issue_id AND f.verification <> 'false')),
    last_seen_review_id = (SELECT f.review_id FROM findings f JOIN reviews r ON r.id = f.review_id
                            WHERE f.issue_id = OLD.issue_id AND f.verification <> 'false'
                            ORDER BY r.started_at DESC, r.id DESC LIMIT 1),
    last_checked_review_id = (SELECT x.rid FROM (
                                SELECT f.review_id AS rid FROM findings f
                                 WHERE f.issue_id = OLD.issue_id AND f.verification <> 'false'
                                UNION SELECT c.review_id FROM review_issue_checks c WHERE c.issue_id = OLD.issue_id) x
                              JOIN reviews r ON r.id = x.rid ORDER BY r.started_at DESC, r.id DESC LIMIT 1)
  WHERE OLD.issue_id IS NOT NULL AND id = OLD.issue_id;
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, note)
  SELECT 'issue', OLD.issue_id, 'unlinked', NEW.point_id, (SELECT code FROM issues WHERE id = NEW.issue_id),
         NEW.review_id, 'finding ' || NEW.point_id || ' no longer matched to this issue'
  WHERE OLD.issue_id IS NOT NULL;
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, note)
  SELECT 'issue', NEW.issue_id, 'recurred', status, NEW.match_type, NEW.review_id,
         'raised again by finding ' || NEW.point_id || ' while ' || status
    FROM issues WHERE NEW.issue_id IS NOT NULL AND id = NEW.issue_id AND status = 'accepted-risk'
     AND NEW.verification <> 'false' AND NEW.match_type IN (SELECT value FROM match_types WHERE came_back = 1);
  UPDATE issues SET
    times_seen = max(1, (SELECT count(DISTINCT f.review_id) FROM findings f
                          WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false')),
    last_seen_review_id = COALESCE((SELECT f.review_id FROM findings f JOIN reviews r ON r.id = f.review_id
                                     WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false'
                                     ORDER BY r.started_at DESC, r.id DESC LIMIT 1), last_seen_review_id),
    last_checked_review_id = COALESCE((SELECT x.rid FROM (
                                SELECT f.review_id AS rid FROM findings f
                                 WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false'
                                UNION SELECT c.review_id FROM review_issue_checks c WHERE c.issue_id = NEW.issue_id) x
                              JOIN reviews r ON r.id = x.rid ORDER BY r.started_at DESC, r.id DESC LIMIT 1),
                              last_checked_review_id),
    current_state = CASE WHEN NEW.verification = 'false' THEN current_state
                         ELSE COALESCE(NEW.before_state, current_state) END,
    change_review_id = NEW.review_id,
    change_actor_id = NULL,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = 'matched by finding ' || NEW.point_id,
    status = CASE
      WHEN NEW.verification = 'false' THEN status
      WHEN NEW.match_type = 'regression' AND status IN (SELECT value FROM issue_statuses WHERE fixed = 1) THEN 'regressed'
      WHEN NEW.match_type IN (SELECT value FROM match_types WHERE came_back = 1)
       AND status IN (SELECT value FROM issue_statuses WHERE reopens = 1) THEN 'open'
      ELSE status END
  WHERE NEW.issue_id IS NOT NULL AND id = NEW.issue_id;
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  SELECT 'issue', NEW.issue_id, 'seen', CASE WHEN NEW.verification = 'false' THEN 'false' ELSE NEW.match_type END,
         NEW.review_id, NEW.point_id
  WHERE NEW.issue_id IS NOT NULL;
END;

-- A verification change is logged; turning a matched finding false (or back) recounts its issue.
CREATE TRIGGER tr_finding_verification_changed AFTER UPDATE OF verification ON findings
WHEN OLD.verification IS NOT NEW.verification BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('finding', NEW.id, 'verification-changed', OLD.verification, NEW.verification, NEW.verified_by_actor_id,
          NEW.review_id, NEW.verification_note);
  UPDATE issues SET
    times_seen = max(1, (SELECT count(DISTINCT f.review_id) FROM findings f
                          WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false')),
    last_seen_review_id = COALESCE((SELECT f.review_id FROM findings f JOIN reviews r ON r.id = f.review_id
                                     WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false'
                                     ORDER BY r.started_at DESC, r.id DESC LIMIT 1), last_seen_review_id)
  WHERE NEW.issue_id IS NOT NULL AND id = NEW.issue_id
    AND (OLD.verification = 'false') <> (NEW.verification = 'false');
END;

-- ----- Locations

-- A location's repository must be one its review looked at, so tr_location_commit always finds the commit.
CREATE TRIGGER tr_location_repo_in_review BEFORE INSERT ON finding_locations
WHEN NOT EXISTS (SELECT 1 FROM findings f
                 JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = NEW.repository_id
                 WHERE f.id = NEW.finding_id) BEGIN
  SELECT RAISE(ABORT, 'the location''s repository is not among its review''s review_repositories');
END;

-- A location with no commit takes the commit its review looked at.
CREATE TRIGGER tr_location_commit AFTER INSERT ON finding_locations
WHEN NEW.commit_sha IS NULL BEGIN
  UPDATE finding_locations SET commit_sha = (
    SELECT rr.commit_sha FROM findings f
    JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = NEW.repository_id
    WHERE f.id = NEW.finding_id)
  WHERE id = NEW.id;
END;

-- Where a point was found does not change; fill-refs may still fill lines, excerpt, blob and permalink.
CREATE TRIGGER tr_location_immutable BEFORE UPDATE ON finding_locations
WHEN OLD.finding_id IS NOT NEW.finding_id OR OLD.repository_id IS NOT NEW.repository_id
  OR OLD.path IS NOT NEW.path OR OLD.symbol IS NOT NEW.symbol OR OLD.role IS NOT NEW.role
  OR (OLD.commit_sha IS NOT NULL AND OLD.commit_sha IS NOT NEW.commit_sha) BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a location''s finding, repository, commit, path, symbol and role cannot change');
END;

-- Refuses DELETE: where a point was found is history.
CREATE TRIGGER tr_location_no_delete BEFORE DELETE ON finding_locations BEGIN
  SELECT RAISE(ABORT, 'history is append-only: finding locations cannot be deleted');
END;

-- ----- Relations

-- No relation is stored both ways: with A duplicate-of B recorded, B duplicate-of A is refused, and related-to is
-- symmetric, so one row says it. blocks is not in the list because a blocks pair both ways is a cycle, which
-- tr_relation_no_cycle refuses.
CREATE TRIGGER tr_relation_noreverse_dup BEFORE INSERT ON issue_relations
WHEN NEW.relation IN ('duplicate-of','continuation-of','regression-of','supersedes','merged-into','split-from',
                      'caused-by','related-to')
BEGIN
  SELECT RAISE(ABORT, 'the reverse relation already exists')
  WHERE EXISTS (SELECT 1 FROM issue_relations
                WHERE from_issue_id = NEW.to_issue_id AND to_issue_id = NEW.from_issue_id
                  AND relation = NEW.relation);
END;

-- The same when a relation is edited.
CREATE TRIGGER tr_relation_noreverse_dup_upd BEFORE UPDATE OF from_issue_id, to_issue_id, relation ON issue_relations
WHEN NEW.relation IN ('duplicate-of','continuation-of','regression-of','supersedes','merged-into','split-from',
                      'caused-by','related-to')
BEGIN
  SELECT RAISE(ABORT, 'the reverse relation already exists')
  WHERE EXISTS (SELECT 1 FROM issue_relations
                WHERE from_issue_id = NEW.to_issue_id AND to_issue_id = NEW.from_issue_id
                  AND relation = NEW.relation AND id <> NEW.id);
END;

-- No cycles of blocks, or of duplicate-of / merged-into (an issue cannot end up a duplicate of itself).
CREATE TRIGGER tr_relation_no_cycle BEFORE INSERT ON issue_relations
WHEN NEW.relation IN ('blocks','duplicate-of','merged-into') BEGIN
  SELECT RAISE(ABORT, 'that relation would make a cycle')
  WHERE EXISTS (WITH RECURSIVE reach(n) AS (
                  SELECT NEW.to_issue_id
                  UNION SELECT r.to_issue_id FROM issue_relations r JOIN reach ON r.from_issue_id = reach.n
                   WHERE r.relation = NEW.relation
                      OR (NEW.relation <> 'blocks' AND r.relation IN (SELECT value FROM relation_types WHERE closes = 1)))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;

-- The same when a relation is edited.
CREATE TRIGGER tr_relation_no_cycle_upd BEFORE UPDATE OF from_issue_id, to_issue_id, relation ON issue_relations
WHEN NEW.relation IN ('blocks','duplicate-of','merged-into') BEGIN
  SELECT RAISE(ABORT, 'that relation would make a cycle')
  WHERE EXISTS (WITH RECURSIVE reach(n) AS (
                  SELECT NEW.to_issue_id
                  UNION SELECT r.to_issue_id FROM issue_relations r JOIN reach ON r.from_issue_id = reach.n
                   WHERE r.id <> NEW.id AND (r.relation = NEW.relation
                      OR (NEW.relation <> 'blocks' AND r.relation IN (SELECT value FROM relation_types WHERE closes = 1))))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;

-- Logs 'created' on the relation: the two issue codes, the relation and the rationale.
CREATE TRIGGER tr_relation_created AFTER INSERT ON issue_relations BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('relation', NEW.id, 'created', (SELECT code FROM issues WHERE id = NEW.from_issue_id),
          NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id),
          NEW.established_by_actor_id, NEW.established_in_review_id, NEW.rationale);
END;

-- Recording that an active issue is a duplicate of (or merged into) another closes it as duplicate.
CREATE TRIGGER tr_relation_closes_duplicate AFTER INSERT ON issue_relations
WHEN NEW.relation IN (SELECT value FROM relation_types WHERE closes = 1) BEGIN
  UPDATE issues SET
    status = 'duplicate',
    resolution = COALESCE(resolution, NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id)),
    change_actor_id = NEW.established_by_actor_id,
    change_review_id = NEW.established_in_review_id,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id) || ': ' || NEW.rationale
  WHERE id = NEW.from_issue_id AND status IN (SELECT value FROM issue_statuses WHERE active = 1);
END;

-- Refuses DELETE: relations are history.
CREATE TRIGGER tr_relation_no_delete BEFORE DELETE ON issue_relations BEGIN
  SELECT RAISE(ABORT, 'history is append-only: relations cannot be deleted');
END;

-- ----- Review checks of known issues

-- One check per review and issue, never replaced (INSERT OR REPLACE is refused too); not by the review that
-- first found the issue; its finding must be from the same review; and 'fixed' cannot be recorded by a review
-- that also raised the issue again. (A check that says the issue moved carries evidence: tr_check_needs_evidence.)
CREATE TRIGGER tr_check_needs_evidence BEFORE INSERT ON review_issue_checks
WHEN NEW.outcome IN ('fixed','regressed','changed') AND NULLIF(trim(NEW.evidence), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'a fixed, regressed or changed check needs evidence (what the review saw)');
END;

-- The rules above, each with its own message.
CREATE TRIGGER tr_check_guard BEFORE INSERT ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'checks are history: this review already recorded a check of this issue')
  WHERE EXISTS (SELECT 1 FROM review_issue_checks WHERE review_id = NEW.review_id AND issue_id = NEW.issue_id);
  SELECT RAISE(ABORT, 'a review cannot check an issue it first found')
  WHERE (SELECT first_seen_review_id FROM issues WHERE id = NEW.issue_id) = NEW.review_id;
  SELECT RAISE(ABORT, 'the check''s finding belongs to another review')
  WHERE NEW.finding_id IS NOT NULL
    AND (SELECT review_id FROM findings WHERE id = NEW.finding_id) IS NOT NEW.review_id;
  SELECT RAISE(ABORT, 'this review raised the issue again in a finding; it cannot also record it fixed')
  WHERE NEW.outcome = 'fixed'
    AND EXISTS (SELECT 1 FROM findings f WHERE f.review_id = NEW.review_id AND f.issue_id = NEW.issue_id
                  AND f.verification <> 'false');
END;

-- A recorded check is never changed.
CREATE TRIGGER tr_check_no_update BEFORE UPDATE ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'history is append-only: review checks cannot be changed');
END;

-- Refuses DELETE: checks are history.
CREATE TRIGGER tr_check_no_delete BEFORE DELETE ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'history is append-only: review checks cannot be deleted');
END;

-- A review's verdict on a known issue moves the issue along:
--   fixed on a fixed issue -> verified; fixed on an active issue -> fixed (after_state from the evidence, and a
--   resolution naming the plan step that is not done yet, if any);
--   regressed, still-present or changed on a fixed or verified issue -> regressed;
--   obsolete -> obsolete; not-checked, and anything else, leaves the status alone.
-- Every check sets last_checked (never backwards in time) and writes a 'checked' event with the outcome.
CREATE TRIGGER tr_issue_checked AFTER INSERT ON review_issue_checks BEGIN
  UPDATE issues SET
    last_checked_review_id = CASE
      WHEN last_checked_review_id IS NULL
        OR (SELECT started_at FROM reviews WHERE id = NEW.review_id)
           >= (SELECT started_at FROM reviews WHERE id = last_checked_review_id)
      THEN NEW.review_id ELSE last_checked_review_id END,
    change_review_id = NEW.review_id,
    change_actor_id = NEW.actor_id,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = 'review check: ' || NEW.outcome || COALESCE(' — ' || NEW.evidence, ''),
    status = CASE
      WHEN NEW.outcome = 'fixed' AND status = 'fixed' THEN 'verified'
      WHEN NEW.outcome = 'fixed' AND status IN (SELECT value FROM issue_statuses WHERE active = 1) THEN 'fixed'
      WHEN NEW.outcome IN ('regressed','still-present','changed') AND status IN (SELECT value FROM issue_statuses WHERE fixed = 1) THEN 'regressed'
      WHEN NEW.outcome = 'obsolete' AND status <> 'duplicate' THEN 'obsolete'
      ELSE status END,
    after_state = CASE WHEN NEW.outcome = 'fixed' AND status IN (SELECT value FROM issue_statuses WHERE active = 1)
                       THEN COALESCE(after_state, NEW.evidence,
                                     'Found fixed by review ' || (SELECT code FROM reviews WHERE id = NEW.review_id) || '.')
                       ELSE after_state END,
    resolution = CASE
      WHEN NEW.outcome = 'fixed' AND status IN (SELECT value FROM issue_statuses WHERE active = 1)
      THEN COALESCE(resolution,
             'Found fixed by a later review while plan step ' || (
               SELECT p.code || ' #' || s.number FROM step_findings sf
               JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
               JOIN findings f ON f.id = sf.finding_id
               WHERE f.issue_id = NEW.issue_id AND s.status NOT IN ('done','dropped')
               ORDER BY s.number LIMIT 1) || ' was not done.',
             'Found fixed by a later review, with no plan step recorded.')
      WHEN NEW.outcome = 'obsolete' AND status <> 'duplicate'
      THEN COALESCE(resolution, 'Found obsolete by review ' || (SELECT code FROM reviews WHERE id = NEW.review_id)
                                || COALESCE(': ' || NEW.evidence, '.'))
      ELSE resolution END
  WHERE id = NEW.issue_id;
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('issue', NEW.issue_id, 'checked', NEW.outcome, NEW.actor_id, NEW.review_id, NEW.evidence);
END;

-- ----- Reviews

-- Codes: R- and the next number, zero-padded to 4 digits (R-0001 ...); never reused.
CREATE TRIGGER tr_review_code BEFORE INSERT ON reviews
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'R-*[^0-9]*' AND (
     NEW.code IS NOT 'R-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM reviews
        WHERE code = 'R-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'review codes are sequential: R- and the next number (R-0001, R-0002 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_review_code_fixed BEFORE UPDATE OF code ON reviews WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- A review's manager is an actor of kind manager.
CREATE TRIGGER tr_review_manager_kind BEFORE INSERT ON reviews
WHEN NEW.manager_actor_id IS NOT NULL
 AND (SELECT kind FROM actors WHERE id = NEW.manager_actor_id) IS NOT 'manager' BEGIN
  SELECT RAISE(ABORT, 'manager_actor_id must be an actor of kind manager');
END;

-- The same when the manager is changed.
CREATE TRIGGER tr_review_manager_kind_upd BEFORE UPDATE OF manager_actor_id ON reviews
WHEN NEW.manager_actor_id IS NOT NULL
 AND (SELECT kind FROM actors WHERE id = NEW.manager_actor_id) IS NOT 'manager' BEGIN
  SELECT RAISE(ABORT, 'manager_actor_id must be an actor of kind manager');
END;

-- A review moves planning <-> awaiting-approval -> approved -> running -> verifying -> indexing ->
-- consolidating -> handed-off. After approval it may skip ahead, and between running and consolidating it may
-- also go back (to run or verify again). Any status may move to abandoned. Leaving handed-off or abandoned needs
-- change_note. It leaves awaiting-approval only when no domain-approval gate of it is open or answered but not
-- applied.
CREATE TRIGGER tr_review_transition BEFORE UPDATE OF status ON reviews
WHEN OLD.status IS NOT NEW.status BEGIN
  SELECT RAISE(ABORT, 'a review leaves handed-off or abandoned only with change_note')
  WHERE OLD.status IN ('handed-off','abandoned') AND NEW.change_note IS NULL;
  SELECT RAISE(ABORT, 'a review moves planning, awaiting-approval, approved, running, verifying, indexing, consolidating, handed-off (or abandoned)')
  WHERE OLD.status NOT IN ('handed-off','abandoned') AND NEW.status <> 'abandoned' AND NOT (
       (OLD.status = 'planning' AND NEW.status = 'awaiting-approval')
    OR (OLD.status = 'awaiting-approval' AND NEW.status IN ('planning','approved'))
    OR (instr(',approved,running,verifying,indexing,consolidating,', ',' || OLD.status || ',') > 0
        AND instr(',approved,running,verifying,indexing,consolidating,handed-off,', ',' || NEW.status || ',')
            > instr(',approved,running,verifying,indexing,consolidating,handed-off,', ',' || OLD.status || ','))
    OR (instr(',running,verifying,indexing,consolidating,', ',' || OLD.status || ',') > 0
        AND instr(',running,verifying,indexing,consolidating,', ',' || NEW.status || ',') > 0));
  SELECT RAISE(ABORT, 'a domain-approval gate of this review is not applied yet')
  WHERE OLD.status = 'awaiting-approval' AND NEW.status <> 'abandoned'
    AND EXISTS (SELECT 1 FROM gates g WHERE g.review_id = NEW.id AND g.kind = 'domain-approval'
                  AND g.status IN ('open','answered'));
END;

-- Logs 'status-changed' with change_actor_id and change_note, sets updated_at, and sets completed_at on handed-off
-- or abandoned (cleared when the review leaves them).
CREATE TRIGGER tr_review_status AFTER UPDATE OF status ON reviews
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('review', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.change_actor_id, NEW.id, NEW.change_note);
  UPDATE reviews SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    completed_at = CASE WHEN NEW.status IN ('handed-off','abandoned')
                        THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END
  WHERE id = NEW.id;
END;

-- Once logged, the change context is cleared: a later change gives its own.
CREATE TRIGGER tr_review_context_cleared AFTER UPDATE ON reviews
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE reviews SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- The same after an INSERT that carried context.
CREATE TRIGGER tr_review_context_cleared_ins AFTER INSERT ON reviews
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE reviews SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- Refuses DELETE: a review is abandoned with a status.
CREATE TRIGGER tr_review_no_delete BEFORE DELETE ON reviews BEGIN
  SELECT RAISE(ABORT, 'history is append-only: reviews cannot be deleted; set status abandoned instead');
END;

-- Reviewers run only for an approved review, in a domain the review included.
CREATE TRIGGER tr_run_review_approved BEFORE INSERT ON reviewer_runs BEGIN
  SELECT RAISE(ABORT, 'reviewer runs need an approved review (approved_at set, status past awaiting-approval)')
  WHERE (SELECT status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NULL
           FROM reviews WHERE id = NEW.review_id);
  SELECT RAISE(ABORT, 'reviewer runs need the domain included in review_domains')
  WHERE NOT EXISTS (SELECT 1 FROM review_domains WHERE review_id = NEW.review_id
                      AND domain_code = NEW.domain_code AND decision = 'included');
  SELECT RAISE(ABORT, 'that domain is retired; use a current one')
  WHERE EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL);
END;

-- Logs 'reviewer-run-created' on the review, with the run's label, domain and scope.
CREATE TRIGGER tr_run_created AFTER INSERT ON reviewer_runs BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('review', NEW.review_id, 'reviewer-run-created', NEW.label, NEW.actor_id, NEW.review_id,
          NEW.domain_code || ': ' || NEW.scope);
END;

-- A retired domain takes no new reviews.
CREATE TRIGGER tr_review_domain_active BEFORE INSERT ON review_domains
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

-- A retired domain takes no new metrics.
CREATE TRIGGER tr_metric_domain_active BEFORE INSERT ON metrics
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

-- Logs 'metric-recorded' on the review, with the value, name and scope.
CREATE TRIGGER tr_metric_created AFTER INSERT ON metrics BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  VALUES ('review', NEW.review_id, 'metric-recorded', CAST(NEW.value AS TEXT), NEW.review_id,
          NEW.name || ' (' || NEW.scope || ')');
END;

-- ----- Actors, catalogs and standards

-- An actor is not its own parent.
CREATE TRIGGER tr_actor_parent_self BEFORE INSERT ON actors
WHEN NEW.parent_actor_id IS NOT NULL AND NEW.parent_actor_id = NEW.id BEGIN
  SELECT RAISE(ABORT, 'an actor cannot be its own parent');
END;

-- Nor its own ancestor: changing a parent may not close a loop.
CREATE TRIGGER tr_actor_parent_cycle BEFORE UPDATE OF parent_actor_id ON actors
WHEN NEW.parent_actor_id IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'that parent would make a cycle of actors')
  WHERE NEW.parent_actor_id = NEW.id
     OR EXISTS (WITH RECURSIVE up(n) AS (
                  SELECT NEW.parent_actor_id
                  UNION SELECT a.parent_actor_id FROM actors a JOIN up ON a.id = up.n WHERE a.parent_actor_id IS NOT NULL)
                SELECT 1 FROM up WHERE n = NEW.id);
END;

-- Codes: G- and the next number, zero-padded to 2 digits (G-01 ...); never reused.
CREATE TRIGGER tr_design_goal_code BEFORE INSERT ON design_goals
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'G-[0-9][0-9]*' AND NEW.code NOT GLOB 'G-*[^0-9]*' AND (
     NEW.code IS NOT 'G-' || printf('%02d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM design_goals
        WHERE code = 'G-' || printf('%02d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'design goal codes are sequential: G- and the next number (G-01, G-02 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_design_goal_code_fixed BEFORE UPDATE OF code ON design_goals WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- Codes: REQ- and the next number, zero-padded to 4 digits (REQ-0001 ...); never reused.
CREATE TRIGGER tr_requirement_code BEFORE INSERT ON requirements
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'REQ-*[^0-9]*' AND (
     NEW.code IS NOT 'REQ-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER))
  OR CAST(substr(NEW.code, 5) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 5) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM requirements
        WHERE code = 'REQ-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'requirement codes are sequential: REQ- and the next number (REQ-0001, REQ-0002 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_requirement_code_fixed BEFORE UPDATE OF code ON requirements WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- ----- Design goals and the owner interview (schema 9, plan step 129)

-- A goal starts proposed (or approved, with its approver); it moves proposed -> approved, rejected or retired, and
-- approved -> retired. An approved goal has an approver of kind owner or human (who approved it and when are kept);
-- a rejected one keeps the owner's reason (note). An approved, rejected or retired goal is never edited: retire it
-- and propose a new one. Every change writes an event ('goal').
CREATE TRIGGER tr_goal_insert BEFORE INSERT ON design_goals
WHEN NEW.status NOT IN ('proposed','approved') BEGIN
  SELECT RAISE(ABORT, 'a design goal starts proposed (or approved, with its approver)');
END;

-- A retired domain takes no new design goals.
CREATE TRIGGER tr_goal_domain_active BEFORE INSERT ON design_goals
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

-- An approved goal's approver is an actor of kind owner or human.
CREATE TRIGGER tr_goal_approver BEFORE INSERT ON design_goals
WHEN NEW.status = 'approved'
 AND NOT EXISTS (SELECT 1 FROM actors WHERE id = NEW.approved_by_actor_id AND kind IN ('owner','human')) BEGIN
  SELECT RAISE(ABORT, 'a design goal is approved by the owner: its approver (approved_by_actor_id) must be an actor of kind owner or human');
END;

-- The same when a goal is approved later, or its approver changed.
CREATE TRIGGER tr_goal_approver_upd BEFORE UPDATE OF status, approved_by_actor_id ON design_goals
WHEN NEW.status = 'approved'
 AND NOT EXISTS (SELECT 1 FROM actors WHERE id = NEW.approved_by_actor_id AND kind IN ('owner','human')) BEGIN
  SELECT RAISE(ABORT, 'a design goal is approved by the owner: its approver (approved_by_actor_id) must be an actor of kind owner or human');
END;

-- The moves a goal's status may make.
CREATE TRIGGER tr_goal_transition BEFORE UPDATE OF status ON design_goals
WHEN OLD.status IS NOT NEW.status
 AND NOT ((OLD.status = 'proposed' AND NEW.status IN ('approved','rejected','retired'))
       OR (OLD.status = 'approved' AND NEW.status = 'retired')) BEGIN
  SELECT RAISE(ABORT, 'a design goal moves proposed -> approved, rejected or retired, and approved -> retired; propose a new goal instead');
END;

-- Once a goal is no longer proposed, its text, domain, review and approval are fixed.
CREATE TRIGGER tr_goal_fixed BEFORE UPDATE OF text, domain_code, review_id, approved_by_actor_id, approved_at ON design_goals
WHEN OLD.status <> 'proposed'
 AND (OLD.text IS NOT NEW.text OR OLD.domain_code IS NOT NEW.domain_code OR OLD.review_id IS NOT NEW.review_id
      OR OLD.approved_by_actor_id IS NOT NEW.approved_by_actor_id OR OLD.approved_at IS NOT NEW.approved_at) BEGIN
  SELECT RAISE(ABORT, 'an approved, rejected or retired design goal is not edited: retire it and propose a new one');
END;

-- Logs 'proposed' (or 'recorded' for a goal inserted approved); a goal inserted approved for a review is also
-- listed in review_goals.
CREATE TRIGGER tr_goal_created AFTER INSERT ON design_goals BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('goal', NEW.id, CASE NEW.status WHEN 'proposed' THEN 'proposed' ELSE 'recorded' END, NEW.status,
          COALESCE(NEW.change_actor_id, NEW.approved_by_actor_id), NEW.review_id,
          COALESCE(NEW.domain_code || ': ', 'whole project: ') || NEW.text);
  INSERT OR IGNORE INTO review_goals(review_id, goal_id)
    SELECT NEW.review_id, NEW.id WHERE NEW.status = 'approved' AND NEW.review_id IS NOT NULL;
END;

-- Logs 'status-changed' (who approved, or the note of a rejection or retirement) and keeps retired_at in step with
-- the status.
CREATE TRIGGER tr_goal_status AFTER UPDATE OF status ON design_goals
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('goal', NEW.id, 'status-changed', OLD.status, NEW.status,
          CASE WHEN NEW.status = 'approved' THEN COALESCE(NEW.change_actor_id, NEW.approved_by_actor_id)
               ELSE NEW.change_actor_id END,
          NEW.review_id, CASE WHEN NEW.status IN ('rejected','retired') THEN NEW.note END);
  UPDATE design_goals SET retired_at = COALESCE(retired_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  WHERE id = NEW.id AND NEW.status = 'retired';
  INSERT OR IGNORE INTO review_goals(review_id, goal_id)
    SELECT NEW.review_id, NEW.id WHERE NEW.status = 'approved' AND NEW.review_id IS NOT NULL;
END;

-- Retiring by retired_at alone (the older way) sets the status too.
CREATE TRIGGER tr_goal_retired_at AFTER UPDATE OF retired_at ON design_goals
WHEN NEW.retired_at IS NOT NULL AND NEW.status <> 'retired' BEGIN
  UPDATE design_goals SET status = 'retired' WHERE id = NEW.id;
END;

-- Logs 'text-changed' with the old and new text (possible only while the goal is proposed).
CREATE TRIGGER tr_goal_text_changed AFTER UPDATE OF text ON design_goals WHEN OLD.text IS NOT NEW.text BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id)
  VALUES ('goal', NEW.id, 'text-changed', OLD.text, NEW.text, NEW.change_actor_id, NEW.review_id);
END;

-- Once logged, change_actor_id is cleared.
CREATE TRIGGER tr_goal_context_cleared AFTER UPDATE ON design_goals WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE design_goals SET change_actor_id = NULL WHERE id = NEW.id;
END;

-- The same after an INSERT that carried it.
CREATE TRIGGER tr_goal_context_cleared_ins AFTER INSERT ON design_goals
WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE design_goals SET change_actor_id = NULL WHERE id = NEW.id;
END;

-- Refuses DELETE: a goal is retired.
CREATE TRIGGER tr_goal_no_delete BEFORE DELETE ON design_goals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: design goals cannot be deleted; retire it instead');
END;

-- The owner's answer to a goal-approval gate: 'approve' (alone, or among the choices of a multi-select gate)
-- approves every goal still proposed on it, by whoever answered (the viewer and import-answers: the owner). Any other answer leaves them proposed for the manager to reword.
CREATE TRIGGER tr_gate_approves_goals AFTER UPDATE OF status ON gates
WHEN OLD.status = 'open' AND NEW.status = 'answered'
 AND (NEW.answer_value = 'approve' OR (NEW.multi = 1 AND json_valid(NEW.answer_value) AND EXISTS (
        SELECT 1 FROM json_each(NEW.answer_value) WHERE value = 'approve'))) BEGIN
  UPDATE design_goals SET status = 'approved',
    approved_by_actor_id = COALESCE(NEW.answered_by_actor_id, (SELECT min(id) FROM actors WHERE kind = 'owner')),
    approved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    change_actor_id = COALESCE(NEW.answered_by_actor_id, (SELECT min(id) FROM actors WHERE kind = 'owner'))
  WHERE gate_id = NEW.id AND status = 'proposed';
END;

-- The owner's answer to an interview question asked in the hub is the question's answer.
CREATE TRIGGER tr_gate_answers_question AFTER UPDATE OF status ON gates
WHEN OLD.status = 'open' AND NEW.status = 'answered' AND length(trim(COALESCE(NEW.answer_text, ''))) > 0 BEGIN
  INSERT INTO interview_answers(question_id, answer, answered_via, answered_by_actor_id)
    SELECT q.id, NEW.answer_text, CASE NEW.answered_via WHEN 'viewer' THEN 'viewer' ELSE 'chat' END,
           NEW.answered_by_actor_id
    FROM interview_questions q WHERE q.gate_id = NEW.id;
END;

-- Codes: IQ- and the next number, zero-padded to 4 digits (IQ-0001 ...); never changed or reused.
CREATE TRIGGER tr_question_code BEFORE INSERT ON interview_questions
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'IQ-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'IQ-*[^0-9]*' AND (
     NEW.code IS NOT 'IQ-' || printf('%04d', CAST(substr(NEW.code, 4) AS INTEGER))
  OR CAST(substr(NEW.code, 4) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 4) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM interview_questions
        WHERE code = 'IQ-' || printf('%04d', CAST(substr(NEW.code, 4) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'interview question codes are sequential: IQ- and the next number (IQ-0001, IQ-0002 ...)');
END;

-- The interview comes after the owner approved the review's domains, and asks about an approved domain (or the
-- whole project).
CREATE TRIGGER tr_question_review_approved BEFORE INSERT ON interview_questions
WHEN (SELECT approved_at FROM reviews WHERE id = NEW.review_id) IS NULL BEGIN
  SELECT RAISE(ABORT, 'the interview comes after the owner approves the review''s domains (review-status <review> approved)');
END;

-- A question about a domain names a domain the review included.
CREATE TRIGGER tr_question_domain_in_review BEFORE INSERT ON interview_questions
WHEN NEW.domain_code IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM review_domains WHERE review_id = NEW.review_id AND domain_code = NEW.domain_code
                   AND decision = 'included') BEGIN
  SELECT RAISE(ABORT, 'the review did not include that domain; ask about an approved domain, or leave the domain out for a whole-project question');
END;

-- Logs 'asked' with the question.
CREATE TRIGGER tr_question_asked AFTER INSERT ON interview_questions BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('question', NEW.id, 'asked', NEW.code, NEW.asked_by_actor_id, NEW.review_id, NEW.question);
END;

-- A question is kept as asked.
CREATE TRIGGER tr_question_no_update BEFORE UPDATE ON interview_questions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: an interview question is kept as asked; ask a new one');
END;

-- Refuses DELETE: questions are history.
CREATE TRIGGER tr_question_no_delete BEFORE DELETE ON interview_questions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: interview questions cannot be deleted');
END;

-- Logs 'answered' on the question, with the answer and how it was given.
CREATE TRIGGER tr_answer_recorded AFTER INSERT ON interview_answers BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('question', NEW.question_id, 'answered', NEW.answered_via, NEW.answered_by_actor_id,
          (SELECT review_id FROM interview_questions WHERE id = NEW.question_id), NEW.answer);
END;

-- An answer is kept as given; a correction is a new answer.
CREATE TRIGGER tr_answer_no_update BEFORE UPDATE ON interview_answers BEGIN
  SELECT RAISE(ABORT, 'history is append-only: an answer is kept as given; record a new answer instead');
END;

-- Refuses DELETE: answers are history.
CREATE TRIGGER tr_answer_no_delete BEFORE DELETE ON interview_answers BEGIN
  SELECT RAISE(ABORT, 'history is append-only: interview answers cannot be deleted');
END;

-- A goal cites the questions it summarizes while it is proposed: questions of its own domain or whole-project ones
-- (a whole-project goal may cite any).
CREATE TRIGGER tr_goal_source_rules BEFORE INSERT ON goal_sources BEGIN
  SELECT RAISE(ABORT, 'a design goal names its sources when it is proposed')
  WHERE (SELECT status FROM design_goals WHERE id = NEW.goal_id) IS NOT 'proposed';
  SELECT RAISE(ABORT, 'a design goal cites questions of its own domain or whole-project ones')
  WHERE EXISTS (SELECT 1 FROM design_goals g JOIN interview_questions q ON q.id = NEW.question_id
                WHERE g.id = NEW.goal_id AND g.domain_code IS NOT NULL AND q.domain_code IS NOT NULL
                  AND q.domain_code <> g.domain_code);
END;

-- Logs 'source-linked' on the goal, with the question.
CREATE TRIGGER tr_goal_source_linked AFTER INSERT ON goal_sources BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  SELECT 'goal', NEW.goal_id, 'source-linked', q.code, q.review_id, q.question
  FROM interview_questions q WHERE q.id = NEW.question_id;
END;

-- A goal's sources are kept as recorded.
CREATE TRIGGER tr_goal_source_no_update BEFORE UPDATE ON goal_sources BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a goal''s sources are kept as recorded');
END;

-- Refuses DELETE: a goal's sources are history.
CREATE TRIGGER tr_goal_source_no_delete BEFORE DELETE ON goal_sources BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a goal''s sources cannot be deleted');
END;

-- A plan step serves approved goals only (goal_statuses.served).
CREATE TRIGGER tr_step_goal_served BEFORE INSERT ON step_goals
WHEN NOT EXISTS (SELECT 1 FROM design_goals g JOIN goal_statuses s ON s.value = g.status
                 WHERE g.id = NEW.goal_id AND s.served = 1) BEGIN
  SELECT RAISE(ABORT, 'a plan step serves approved design goals only; ask the owner to approve the goal first');
END;

-- Logs 'step-linked' on the goal, with the plan and step number.
CREATE TRIGGER tr_step_goal_linked AFTER INSERT ON step_goals BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id)
  SELECT 'goal', NEW.goal_id, 'step-linked', p.code || ' #' || s.number, NEW.step_id
  FROM steps s JOIN plans p ON p.id = s.plan_id WHERE s.id = NEW.step_id;
END;

-- A link is not moved to another goal: add the other link.
CREATE TRIGGER tr_step_goal_no_update BEFORE UPDATE ON step_goals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: link the step to another goal instead');
END;

-- Refuses DELETE: a step's goals stay linked.
CREATE TRIGGER tr_step_goal_no_delete BEFORE DELETE ON step_goals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a step''s goals cannot be unlinked');
END;

-- ----- Decisions

-- Codes: D- and the next number, zero-padded to 4 digits (D-0001 ...); never reused.
CREATE TRIGGER tr_decision_code BEFORE INSERT ON decisions
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'D-*[^0-9]*' AND (
     NEW.code IS NOT 'D-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM decisions
        WHERE code = 'D-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'decision codes are sequential: D- and the next number (D-0001, D-0002 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_decision_code_fixed BEFORE UPDATE OF code ON decisions WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- A new decision is logged, also one inserted already decided; decided_at is filled when a decision is decided.
CREATE TRIGGER tr_decision_created AFTER INSERT ON decisions BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'created', NEW.status,
          CASE WHEN NEW.status = 'decided' THEN COALESCE(NEW.decided_by_actor_id, NEW.asked_by_actor_id)
               ELSE NEW.asked_by_actor_id END,
          NEW.review_id, NEW.step_id, CASE WHEN NEW.status = 'decided' THEN NEW.answer ELSE NEW.question END);
  UPDATE decisions SET decided_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
  WHERE id = NEW.id AND NEW.status = 'decided' AND NEW.decided_at IS NULL;
END;

-- Logs 'status-changed' with decided_by_actor_id and the answer as the note, and fills decided_at when the decision
-- is decided.
CREATE TRIGGER tr_decision_status AFTER UPDATE OF status ON decisions
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.decided_by_actor_id, NEW.review_id, NEW.step_id, NEW.answer);
  UPDATE decisions SET decided_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
  WHERE id = NEW.id AND NEW.status = 'decided' AND (NEW.decided_at IS NULL OR NEW.decided_at IS OLD.decided_at);
END;

-- A recorded answer that is replaced keeps the old one in history.
CREATE TRIGGER tr_decision_answer AFTER UPDATE OF answer ON decisions
WHEN OLD.answer IS NOT NULL AND OLD.answer IS NOT NEW.answer BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'answer-changed', OLD.answer, NEW.answer, NEW.decided_by_actor_id, NEW.review_id,
          NEW.step_id, NEW.rationale);
END;

-- Refuses DELETE: a decision to reconsider is set to revisit.
CREATE TRIGGER tr_decision_no_delete BEFORE DELETE ON decisions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: decisions cannot be deleted; set status revisit instead');
END;

-- ----- Gates

-- Codes: GATE- and the next number, zero-padded to 4 digits (GATE-0001 ...); never reused.
CREATE TRIGGER tr_gate_code BEFORE INSERT ON gates
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'GATE-*[^0-9]*' AND (
     NEW.code IS NOT 'GATE-' || printf('%04d', CAST(substr(NEW.code, 6) AS INTEGER))
  OR CAST(substr(NEW.code, 6) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 6) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM gates
        WHERE code = 'GATE-' || printf('%04d', CAST(substr(NEW.code, 6) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'gate codes are sequential: GATE- and the next number (GATE-0001, GATE-0002 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_gate_code_fixed BEFORE UPDATE OF code ON gates WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- Options are objects with a text value and label (description optional).
CREATE TRIGGER tr_gate_options_shape BEFORE INSERT ON gates
WHEN NEW.options IS NOT NULL AND json_valid(NEW.options) AND json_type(NEW.options) = 'array'
 AND EXISTS (SELECT 1 FROM json_each(NEW.options) j
             WHERE j.type <> 'object' OR json_type(j.value, '$.value') IS NOT 'text'
                OR json_type(j.value, '$.label') IS NOT 'text'
                OR json_type(j.value, '$.description') NOT IN ('text','null')) BEGIN
  SELECT RAISE(ABORT, 'gate options must be objects with a text value and label (and an optional description)');
END;

-- The same when the options are changed.
CREATE TRIGGER tr_gate_options_shape_upd BEFORE UPDATE OF options ON gates
WHEN NEW.options IS NOT NULL AND json_valid(NEW.options) AND json_type(NEW.options) = 'array'
 AND EXISTS (SELECT 1 FROM json_each(NEW.options) j
             WHERE j.type <> 'object' OR json_type(j.value, '$.value') IS NOT 'text'
                OR json_type(j.value, '$.label') IS NOT 'text'
                OR json_type(j.value, '$.description') NOT IN ('text','null')) BEGIN
  SELECT RAISE(ABORT, 'gate options must be objects with a text value and label (and an optional description)');
END;

-- The answer rules of answer_gate() (the viewer, answer-gate, import-answers), for answers written as SQL:
-- answer_value is one of the option values, or on a multi-select gate a JSON list of them ('[]': none); a gate
-- without free text needs an option chosen and takes no note; a note is at most 10,000 characters.
CREATE TRIGGER tr_gate_answer_rules BEFORE UPDATE OF status, answer_value, answer_text ON gates
WHEN NEW.status = 'answered' AND (OLD.status IS NOT 'answered' OR OLD.answer_value IS NOT NEW.answer_value
                                  OR OLD.answer_text IS NOT NEW.answer_text) BEGIN
  SELECT RAISE(ABORT, 'answer_value must be one of the gate''s option values')
  WHERE NEW.multi = 0 AND NEW.answer_value IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM json_each(COALESCE(NEW.options, '[]')) j
                    WHERE json_extract(j.value, '$.value') = NEW.answer_value);
  SELECT RAISE(ABORT, 'a multi-select answer is a JSON list of the gate''s option values')
  WHERE NEW.multi = 1 AND NEW.answer_value IS NOT NULL
    AND (CASE WHEN json_valid(NEW.answer_value) THEN json_type(NEW.answer_value) IS NOT 'array' ELSE 1 END
         OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(NEW.answer_value) AND json_type(NEW.answer_value) = 'array'
                                                 THEN NEW.answer_value ELSE '[]' END) a
                    WHERE a.type <> 'text' OR NOT EXISTS (
                      SELECT 1 FROM json_each(COALESCE(NEW.options, '[]')) j
                      WHERE json_extract(j.value, '$.value') = a.value)));
  SELECT RAISE(ABORT, 'this gate takes no free text: choose one of the options')
  WHERE NEW.allow_text = 0 AND NEW.answer_value IS NULL;
  SELECT RAISE(ABORT, 'this gate takes no free text: answer with the option alone')
  WHERE NEW.allow_text = 0 AND NEW.answer_text IS NOT NULL;
  SELECT RAISE(ABORT, 'answer_text is too long (over 10,000 characters)')
  WHERE length(NEW.answer_text) > 10000;
END;

-- A multi-select gate has options, and its preselected values are among them; only a multi-select gate preselects.
CREATE TRIGGER tr_gate_multi_shape BEFORE INSERT ON gates
WHEN NEW.multi = 1 OR NEW.preselect IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'a multi-select gate needs options')
  WHERE NEW.multi = 1 AND (NEW.options IS NULL OR json_array_length(NEW.options) = 0);
  SELECT RAISE(ABORT, 'preselect goes with a multi-select gate, and names only its option values')
  WHERE NEW.preselect IS NOT NULL AND (NEW.multi = 0 OR EXISTS (
    SELECT 1 FROM json_each(NEW.preselect) p WHERE p.type <> 'text' OR NOT EXISTS (
      SELECT 1 FROM json_each(COALESCE(NEW.options, '[]')) j WHERE json_extract(j.value, '$.value') = p.value)));
END;

-- The same when the options, the shape or the preselection change.
CREATE TRIGGER tr_gate_multi_shape_upd BEFORE UPDATE OF multi, preselect, options ON gates
WHEN NEW.multi = 1 OR NEW.preselect IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'a multi-select gate needs options')
  WHERE NEW.multi = 1 AND (NEW.options IS NULL OR json_array_length(NEW.options) = 0);
  SELECT RAISE(ABORT, 'preselect goes with a multi-select gate, and names only its option values')
  WHERE NEW.preselect IS NOT NULL AND (NEW.multi = 0 OR EXISTS (
    SELECT 1 FROM json_each(NEW.preselect) p WHERE p.type <> 'text' OR NOT EXISTS (
      SELECT 1 FROM json_each(COALESCE(NEW.options, '[]')) j WHERE json_extract(j.value, '$.value') = p.value)));
END;

-- A gate moves open -> answered -> applied (or withdrawn); it cannot be applied before it is answered. Any
-- status may go back to open (reopening, with the answer cleared: tr_gate_answer_final, tr_gate_answer_cleared).
CREATE TRIGGER tr_gate_transition BEFORE UPDATE OF status ON gates
WHEN OLD.status IS NOT NEW.status AND NEW.status <> 'open'
 AND NOT ((OLD.status = 'open' AND NEW.status IN ('answered','withdrawn'))
       OR (OLD.status = 'answered' AND NEW.status IN ('applied','withdrawn'))) BEGIN
  SELECT RAISE(ABORT, 'a gate moves open -> answered -> applied (or withdrawn); answer it before applying it');
END;

-- Logs 'opened' with the gate's title and who opened it.
CREATE TRIGGER tr_gate_created AFTER INSERT ON gates BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'opened', NEW.status, NEW.opened_by_actor_id, NEW.review_id, NEW.step_id, NEW.title);
END;

-- Answered: the event carries who answered and 'value: text'; applied: who applied it and applied_note.
CREATE TRIGGER tr_gate_status AFTER UPDATE OF status ON gates
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'status-changed', OLD.status, NEW.status,
          CASE WHEN NEW.status = 'answered' THEN COALESCE(NEW.answered_by_actor_id, NEW.change_actor_id)
               ELSE NEW.change_actor_id END,
          NEW.review_id, NEW.step_id,
          CASE WHEN NEW.status = 'applied' THEN NEW.applied_note
               ELSE COALESCE(CASE WHEN NEW.multi = 1 AND json_valid(NEW.answer_value)
                                   THEN (SELECT COALESCE(group_concat(value, ', '), 'none') FROM json_each(NEW.answer_value))
                                   ELSE NEW.answer_value END || ': ', '') || COALESCE(NEW.answer_text, '') END);
  UPDATE gates SET
    answered_at = CASE WHEN NEW.status = 'answered' AND answered_at IS NULL
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE answered_at END,
    applied_at  = CASE WHEN NEW.status = 'applied' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE applied_at END
  WHERE id = NEW.id;
END;

-- An answer is final once the gate is no longer open (answered, applied or withdrawn): a later write, by the
-- viewer or an agent, cannot silently replace what the owner said. The one way back is to reopen the gate
-- (status 'open', both answer columns NULL), which tr_gate_answer_cleared records with the answer it removed.
CREATE TRIGGER tr_gate_answer_final BEFORE UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status <> 'open'
 AND (OLD.answer_value IS NOT NEW.answer_value OR OLD.answer_text IS NOT NEW.answer_text)
 AND NOT (NEW.status = 'open' AND NEW.answer_value IS NULL AND NEW.answer_text IS NULL) BEGIN
  SELECT RAISE(ABORT, 'the answer of a gate that is no longer open cannot be changed; open a new gate');
END;

-- Likewise who answered, how and when (answered_at may still be filled once, by tr_gate_status).
CREATE TRIGGER tr_gate_answered_by_final BEFORE UPDATE OF answered_by, answered_via, answered_by_actor_id, answered_at ON gates
WHEN OLD.status <> 'open' AND NEW.status <> 'open'
 AND (OLD.answered_by IS NOT NEW.answered_by OR OLD.answered_via IS NOT NEW.answered_via
      OR OLD.answered_by_actor_id IS NOT NEW.answered_by_actor_id
      OR (OLD.answered_at IS NOT NULL AND OLD.answered_at IS NOT NEW.answered_at)) BEGIN
  SELECT RAISE(ABORT, 'who answered a gate, how and when cannot be changed once it is answered');
END;

-- Logs 'answer-cleared', with the answer it held, when a gate is reopened.
CREATE TRIGGER tr_gate_answer_cleared AFTER UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status <> 'open' AND NEW.status = 'open'
 AND (OLD.answer_value IS NOT NULL OR OLD.answer_text IS NOT NULL) BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'answer-cleared', OLD.answer_value, NULL, NEW.review_id, NEW.step_id,
          'reopened; the answer was: ' || COALESCE(OLD.answer_value || ': ', '') || COALESCE(OLD.answer_text, ''));
END;

-- An answer replaced while the gate is still open is logged with the old and new answer.
CREATE TRIGGER tr_gate_answer_changed AFTER UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status = 'open' AND (OLD.answer_value IS NOT NULL OR OLD.answer_text IS NOT NULL)
 AND (OLD.answer_value IS NOT NEW.answer_value OR OLD.answer_text IS NOT NEW.answer_text) BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'answer-changed', OLD.answer_value, NEW.answer_value, NEW.change_actor_id, NEW.review_id,
          NEW.step_id, 'was: ' || COALESCE(OLD.answer_text, '') || '; now: ' || COALESCE(NEW.answer_text, ''));
END;

-- Once logged, change_actor_id is cleared.
CREATE TRIGGER tr_gate_context_cleared AFTER UPDATE ON gates WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE gates SET change_actor_id = NULL WHERE id = NEW.id;
END;

-- The same after an INSERT that carried it.
CREATE TRIGGER tr_gate_context_cleared_ins AFTER INSERT ON gates
WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE gates SET change_actor_id = NULL WHERE id = NEW.id;
END;

-- Refuses DELETE: a gate no longer needed is withdrawn.
CREATE TRIGGER tr_gate_no_delete BEFORE DELETE ON gates BEGIN
  SELECT RAISE(ABORT, 'history is append-only: gates cannot be deleted; set status withdrawn instead');
END;

-- ----- Plans

-- Codes: P- and the next number, zero-padded to 4 digits (P-0001 ...); never reused.
CREATE TRIGGER tr_plan_code BEFORE INSERT ON plans
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'P-*[^0-9]*' AND (
     NEW.code IS NOT 'P-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM plans
        WHERE code = 'P-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'plan codes are sequential: P- and the next number (P-0001, P-0002 ...)');
END;

-- A code never changes.
CREATE TRIGGER tr_plan_code_fixed BEFORE UPDATE OF code ON plans WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- Logs 'created' with the plan's title and change_actor_id.
CREATE TRIGGER tr_plan_created AFTER INSERT ON plans BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'created', NEW.status, NEW.change_actor_id, NEW.title);
END;

-- A plan moves drafting <-> awaiting-approval -> approved -> in-progress <-> complete; any status but complete may
-- move to superseded or abandoned (complete to superseded only); leaving superseded or abandoned needs
-- change_note. It leaves awaiting-approval for approved only when no plan-approval gate of it is open or
-- answered but not applied.
CREATE TRIGGER tr_plan_transition BEFORE UPDATE OF status ON plans
WHEN OLD.status IS NOT NEW.status BEGIN
  SELECT RAISE(ABORT, 'a plan leaves superseded or abandoned only with change_note')
  WHERE OLD.status IN ('superseded','abandoned') AND NEW.change_note IS NULL;
  SELECT RAISE(ABORT, 'a plan moves drafting, awaiting-approval, approved, in-progress, complete (or superseded, abandoned)')
  WHERE OLD.status NOT IN ('superseded','abandoned') AND NOT (
       (OLD.status = 'drafting' AND NEW.status IN ('awaiting-approval','superseded','abandoned'))
    OR (OLD.status = 'awaiting-approval' AND NEW.status IN ('drafting','approved','superseded','abandoned'))
    OR (OLD.status = 'approved' AND NEW.status IN ('in-progress','drafting','superseded','abandoned'))
    OR (OLD.status = 'in-progress' AND NEW.status IN ('complete','superseded','abandoned'))
    OR (OLD.status = 'complete' AND NEW.status IN ('in-progress','superseded')));
  SELECT RAISE(ABORT, 'a plan-approval gate of this plan is not applied yet')
  WHERE OLD.status = 'awaiting-approval' AND NEW.status = 'approved'
    AND EXISTS (SELECT 1 FROM gates g WHERE g.plan_id = NEW.id AND g.kind = 'plan-approval'
                  AND g.status IN ('open','answered'));
END;

-- Logs 'status-changed' with change_actor_id and change_note, and sets updated_at.
CREATE TRIGGER tr_plan_status AFTER UPDATE OF status ON plans
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.change_actor_id, NEW.change_note);
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- Logs 'title-changed' with the old and new title.
CREATE TRIGGER tr_plan_title_changed AFTER UPDATE OF title ON plans WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.change_note);
END;

-- An update that leaves updated_at alone sets it to now (relies on recursive triggers being off, like
-- tr_issue_touch).
CREATE TRIGGER tr_plan_touch AFTER UPDATE ON plans
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- Once logged, the change context is cleared: a later change gives its own.
CREATE TRIGGER tr_plan_context_cleared AFTER UPDATE ON plans
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE plans SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- The same after an INSERT that carried context.
CREATE TRIGGER tr_plan_context_cleared_ins AFTER INSERT ON plans
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE plans SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- ----- Plan structure: phases, steps, dependencies, coverage links

-- Plan rules enforced by the database, on INSERT and on UPDATE: a step depends on another step of its own plan,
-- of any number, and no chain of dependencies leads back to the step.
CREATE TRIGGER tr_dep_order BEFORE INSERT ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on another step of the same plan')
  WHERE NEW.step_id = NEW.depends_on_step_id OR NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id);
  SELECT RAISE(ABORT, 'that dependency would make a cycle: the step it depends on already waits for it')
  WHERE EXISTS (
    WITH RECURSIVE up(id) AS (
      SELECT NEW.depends_on_step_id
      UNION SELECT d.depends_on_step_id FROM step_dependencies d JOIN up ON d.step_id = up.id)
    SELECT 1 FROM up WHERE up.id = NEW.step_id);
END;

-- The same when a dependency is edited.
CREATE TRIGGER tr_dep_order_upd BEFORE UPDATE ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on another step of the same plan')
  WHERE NEW.step_id = NEW.depends_on_step_id OR NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id);
  SELECT RAISE(ABORT, 'that dependency would make a cycle: the step it depends on already waits for it')
  WHERE EXISTS (
    WITH RECURSIVE up(id) AS (
      SELECT NEW.depends_on_step_id
      UNION SELECT d.depends_on_step_id FROM step_dependencies d JOIN up ON d.step_id = up.id
            WHERE NOT (d.step_id = OLD.step_id AND d.depends_on_step_id = OLD.depends_on_step_id))
    SELECT 1 FROM up WHERE up.id = NEW.step_id);
END;

-- Logs 'dependency-added' on the step, with the number of the step it depends on.
CREATE TRIGGER tr_dep_created AFTER INSERT ON step_dependencies BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id, note)
  VALUES ('step', NEW.step_id, 'dependency-added',
          CAST((SELECT number FROM steps WHERE id = NEW.depends_on_step_id) AS TEXT), NEW.step_id,
          'depends on step ' || (SELECT number FROM steps WHERE id = NEW.depends_on_step_id));
END;

-- A step's phase is a phase of the step's own plan. The foreign keys cannot say this: phase_id and plan_id each
-- point at a valid row on their own.
CREATE TRIGGER tr_step_phase_plan BEFORE INSERT ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;

-- The same when a step is moved to another phase or plan.
CREATE TRIGGER tr_step_phase_plan_upd BEFORE UPDATE OF phase_id, plan_id ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;

-- And when a phase with steps is moved to another plan.
CREATE TRIGGER tr_phase_plan_upd BEFORE UPDATE OF plan_id ON phases
WHEN OLD.plan_id IS NOT NEW.plan_id BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE EXISTS (SELECT 1 FROM steps WHERE phase_id = NEW.id AND plan_id IS NOT NEW.plan_id);
END;

-- Moving a step to another plan keeps every dependency of it and on it inside one plan; renumbering is free.
CREATE TRIGGER tr_step_renumber BEFORE UPDATE OF plan_id ON steps
WHEN OLD.plan_id IS NOT NEW.plan_id BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on another step of the same plan')
  WHERE EXISTS (SELECT 1 FROM step_dependencies d JOIN steps b ON b.id = d.depends_on_step_id
                WHERE d.step_id = NEW.id AND b.plan_id IS NOT NEW.plan_id)
     OR EXISTS (SELECT 1 FROM step_dependencies d JOIN steps a ON a.id = d.step_id
                WHERE d.depends_on_step_id = NEW.id AND a.plan_id IS NOT NEW.plan_id);
END;

-- A status change needs change_note (moving to waiting-owner, owner_question will do); a step cannot start or
-- finish while a blocking gate on it is open or answered but not applied.
CREATE TRIGGER tr_step_change_needs_note BEFORE UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status AND NEW.change_note IS NULL
 AND NOT (NEW.status = 'waiting-owner' AND NEW.owner_question IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'give change_note with a step status change');
END;

-- The second rule above: a blocking gate that is open or answered holds the step.
CREATE TRIGGER tr_step_gate_blocks BEFORE UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status AND NEW.status IN ('doing','done')
 AND EXISTS (SELECT 1 FROM gates g WHERE g.step_id = NEW.id AND g.blocking = 1 AND g.status IN ('open','answered')) BEGIN
  SELECT RAISE(ABORT, 'a blocking gate on this step is not applied yet');
END;

-- Logs 'created' with the step's title and change_actor_id.
CREATE TRIGGER tr_step_created AFTER INSERT ON steps BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'created', NEW.status, NEW.change_actor_id, NEW.id, NEW.title);
END;

-- started_at is set once, on doing or on a jump straight to done; done_at follows done.
CREATE TRIGGER tr_step_status AFTER UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, step_id, commit_sha, note)
  VALUES ('step', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.change_actor_id, NEW.id, NEW.change_commit_sha,
          COALESCE(NEW.change_note, CASE WHEN NEW.status = 'waiting-owner' THEN NEW.owner_question END));
  UPDATE steps SET
    started_at = CASE WHEN NEW.status IN ('doing','done') AND started_at IS NULL
                      THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE started_at END,
    done_at    = CASE WHEN NEW.status = 'done' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END
  WHERE id = NEW.id;
END;

-- Logs 'title-changed' with the old and new title.
CREATE TRIGGER tr_step_title_changed AFTER UPDATE OF title ON steps WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.id, NEW.change_note);
END;

-- Logs 'change-text-changed' with the old and new text.
CREATE TRIGGER tr_step_change_changed AFTER UPDATE OF change ON steps WHEN OLD.change IS NOT NEW.change BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'change-text-changed', OLD.change, NEW.change, NEW.change_actor_id, NEW.id,
          NEW.change_note);
END;

-- Logs 'proof-changed' with the old and new text.
CREATE TRIGGER tr_step_proof_changed AFTER UPDATE OF proof ON steps WHEN OLD.proof IS NOT NEW.proof BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'proof-changed', OLD.proof, NEW.proof, NEW.change_actor_id, NEW.id, NEW.change_note);
END;

-- An update that leaves updated_at alone sets it to now (relies on recursive triggers being off, like
-- tr_issue_touch).
CREATE TRIGGER tr_step_touch AFTER UPDATE ON steps
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE steps SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- Once logged, the change context is cleared: a later change gives its own.
CREATE TRIGGER tr_step_context_cleared AFTER UPDATE ON steps
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE steps SET change_actor_id = NULL, change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- The same after an INSERT that carried context.
CREATE TRIGGER tr_step_context_cleared_ins AFTER INSERT ON steps
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE steps SET change_actor_id = NULL, change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- Refuses DELETE: a step not to be done is dropped.
CREATE TRIGGER tr_step_no_delete BEFORE DELETE ON steps BEGIN
  SELECT RAISE(ABORT, 'history is append-only: steps cannot be deleted; set status dropped instead');
END;

-- A step closes only findings of the reviews its plan covers.
CREATE TRIGGER tr_step_finding_in_plan BEFORE INSERT ON step_findings
WHEN NOT EXISTS (SELECT 1 FROM steps s JOIN findings f ON f.id = NEW.finding_id
                 JOIN plan_reviews pr ON pr.plan_id = s.plan_id AND pr.review_id = f.review_id
                 WHERE s.id = NEW.step_id) BEGIN
  SELECT RAISE(ABORT, 'the finding''s review is not one of the step''s plan reviews (plan_reviews)');
END;

-- Logs 'linked' on the finding, with the step that closes it.
CREATE TRIGGER tr_step_finding_created AFTER INSERT ON step_findings BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id, review_id, note)
  VALUES ('step-finding', NEW.finding_id, 'linked', (SELECT point_id FROM findings WHERE id = NEW.finding_id),
          NEW.step_id, (SELECT review_id FROM findings WHERE id = NEW.finding_id),
          'closed by step ' || (SELECT number FROM steps WHERE id = NEW.step_id) || COALESCE(': ' || NEW.multi_reason, ''));
END;

-- ----- Events

-- An event is never changed.
CREATE TRIGGER tr_events_no_update BEFORE UPDATE ON events BEGIN
  SELECT RAISE(ABORT, 'history is append-only: events cannot be changed');
END;

-- Refuses DELETE: events are the history.
CREATE TRIGGER tr_events_no_delete BEFORE DELETE ON events BEGIN
  SELECT RAISE(ABORT, 'history is append-only: events cannot be deleted');
END;

-- One time format (plan step 75): every time column holds UTC text with milliseconds, exactly what
-- strftime('%Y-%m-%dT%H:%M:%fZ', ...) writes (2026-11-10T09:30:00.000Z), so times compare correctly as text
-- and as julianday(). Any other form (no milliseconds, a space, an offset like +02:00) is refused; convert
-- with strftime('%Y-%m-%dT%H:%M:%fZ', value) first.
CREATE TRIGGER tr_time_schema_migrations_ins BEFORE INSERT ON schema_migrations
WHEN (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at)) BEGIN
  SELECT RAISE(ABORT, 'schema_migrations: applied_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_schema_migrations_upd BEFORE UPDATE OF applied_at ON schema_migrations
WHEN (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at)) BEGIN
  SELECT RAISE(ABORT, 'schema_migrations: applied_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_repositories_ins BEFORE INSERT ON repositories
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'repositories: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_repositories_upd BEFORE UPDATE OF created_at ON repositories
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'repositories: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_actors_ins BEFORE INSERT ON actors
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'actors: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_actors_upd BEFORE UPDATE OF created_at ON actors
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'actors: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_domains_ins BEFORE INSERT ON domains
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'domains: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_domains_upd BEFORE UPDATE OF retired_at ON domains
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'domains: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_themes_ins BEFORE INSERT ON themes
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'themes: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_themes_upd BEFORE UPDATE OF retired_at ON themes
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'themes: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_design_goals_ins BEFORE INSERT ON design_goals
WHEN (NEW.adopted_at IS NOT NULL AND (NEW.adopted_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.adopted_at) IS NOT NEW.adopted_at))
     OR (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at))
     OR (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at)) BEGIN
  SELECT RAISE(ABORT, 'design_goals: adopted_at, retired_at, approved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_design_goals_upd BEFORE UPDATE OF adopted_at, retired_at, approved_at ON design_goals
WHEN (NEW.adopted_at IS NOT NULL AND (NEW.adopted_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.adopted_at) IS NOT NEW.adopted_at))
     OR (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at))
     OR (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at)) BEGIN
  SELECT RAISE(ABORT, 'design_goals: adopted_at, retired_at, approved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_interview_questions_ins BEFORE INSERT ON interview_questions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_questions: asked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_interview_questions_upd BEFORE UPDATE OF asked_at ON interview_questions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_questions: asked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_interview_answers_ins BEFORE INSERT ON interview_answers
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_answers: answered_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_interview_answers_upd BEFORE UPDATE OF answered_at ON interview_answers
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_answers: answered_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_reviews_ins BEFORE INSERT ON reviews
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.completed_at IS NOT NULL AND (NEW.completed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.completed_at) IS NOT NEW.completed_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'reviews: approved_at, started_at, completed_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_reviews_upd BEFORE UPDATE OF approved_at, started_at, completed_at, updated_at ON reviews
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.completed_at IS NOT NULL AND (NEW.completed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.completed_at) IS NOT NEW.completed_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'reviews: approved_at, started_at, completed_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_review_repositories_ins BEFORE INSERT ON review_repositories
WHEN (NEW.commit_time IS NOT NULL AND (NEW.commit_time NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.commit_time) IS NOT NEW.commit_time)) BEGIN
  SELECT RAISE(ABORT, 'review_repositories: commit_time must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_review_repositories_upd BEFORE UPDATE OF commit_time ON review_repositories
WHEN (NEW.commit_time IS NOT NULL AND (NEW.commit_time NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.commit_time) IS NOT NEW.commit_time)) BEGIN
  SELECT RAISE(ABORT, 'review_repositories: commit_time must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_reviewer_runs_ins BEFORE INSERT ON reviewer_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.finished_at IS NOT NULL AND (NEW.finished_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.finished_at) IS NOT NEW.finished_at)) BEGIN
  SELECT RAISE(ABORT, 'reviewer_runs: started_at, finished_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_reviewer_runs_upd BEFORE UPDATE OF started_at, finished_at ON reviewer_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.finished_at IS NOT NULL AND (NEW.finished_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.finished_at) IS NOT NEW.finished_at)) BEGIN
  SELECT RAISE(ABORT, 'reviewer_runs: started_at, finished_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_requirements_ins BEFORE INSERT ON requirements
WHEN (NEW.stated_at IS NOT NULL AND (NEW.stated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.stated_at) IS NOT NEW.stated_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'requirements: stated_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_requirements_upd BEFORE UPDATE OF stated_at, created_at ON requirements
WHEN (NEW.stated_at IS NOT NULL AND (NEW.stated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.stated_at) IS NOT NEW.stated_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'requirements: stated_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_issues_ins BEFORE INSERT ON issues
WHEN (NEW.opened_at IS NOT NULL AND (NEW.opened_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.opened_at) IS NOT NEW.opened_at))
     OR (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at))
     OR (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'issues: opened_at, resolved_at, verified_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_issues_upd BEFORE UPDATE OF opened_at, resolved_at, verified_at, updated_at ON issues
WHEN (NEW.opened_at IS NOT NULL AND (NEW.opened_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.opened_at) IS NOT NEW.opened_at))
     OR (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at))
     OR (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'issues: opened_at, resolved_at, verified_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_issue_relations_ins BEFORE INSERT ON issue_relations
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'issue_relations: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_issue_relations_upd BEFORE UPDATE OF created_at ON issue_relations
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'issue_relations: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_findings_ins BEFORE INSERT ON findings
WHEN (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'findings: verified_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_findings_upd BEFORE UPDATE OF verified_at, created_at ON findings
WHEN (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'findings: verified_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_review_issue_checks_ins BEFORE INSERT ON review_issue_checks
WHEN (NEW.checked_at IS NOT NULL AND (NEW.checked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.checked_at) IS NOT NEW.checked_at)) BEGIN
  SELECT RAISE(ABORT, 'review_issue_checks: checked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_review_issue_checks_upd BEFORE UPDATE OF checked_at ON review_issue_checks
WHEN (NEW.checked_at IS NOT NULL AND (NEW.checked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.checked_at) IS NOT NEW.checked_at)) BEGIN
  SELECT RAISE(ABORT, 'review_issue_checks: checked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_decisions_ins BEFORE INSERT ON decisions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at))
     OR (NEW.decided_at IS NOT NULL AND (NEW.decided_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.decided_at) IS NOT NEW.decided_at)) BEGIN
  SELECT RAISE(ABORT, 'decisions: asked_at, decided_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_decisions_upd BEFORE UPDATE OF asked_at, decided_at ON decisions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at))
     OR (NEW.decided_at IS NOT NULL AND (NEW.decided_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.decided_at) IS NOT NEW.decided_at)) BEGIN
  SELECT RAISE(ABORT, 'decisions: asked_at, decided_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_gates_ins BEFORE INSERT ON gates
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at))
     OR (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'gates: answered_at, applied_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_gates_upd BEFORE UPDATE OF answered_at, applied_at, created_at ON gates
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at))
     OR (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'gates: answered_at, applied_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_plans_ins BEFORE INSERT ON plans
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'plans: approved_at, created_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_plans_upd BEFORE UPDATE OF approved_at, created_at, updated_at ON plans
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'plans: approved_at, created_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_steps_ins BEFORE INSERT ON steps
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.done_at IS NOT NULL AND (NEW.done_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.done_at) IS NOT NEW.done_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'steps: started_at, done_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_steps_upd BEFORE UPDATE OF started_at, done_at, updated_at ON steps
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.done_at IS NOT NULL AND (NEW.done_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.done_at) IS NOT NEW.done_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'steps: started_at, done_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_step_commits_ins BEFORE INSERT ON step_commits
WHEN (NEW.committed_at IS NOT NULL AND (NEW.committed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.committed_at) IS NOT NEW.committed_at)) BEGIN
  SELECT RAISE(ABORT, 'step_commits: committed_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_step_commits_upd BEFORE UPDATE OF committed_at ON step_commits
WHEN (NEW.committed_at IS NOT NULL AND (NEW.committed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.committed_at) IS NOT NEW.committed_at)) BEGIN
  SELECT RAISE(ABORT, 'step_commits: committed_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_guard_tests_ins BEFORE INSERT ON guard_tests
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'guard_tests: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_guard_tests_upd BEFORE UPDATE OF created_at ON guard_tests
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'guard_tests: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_test_runs_ins BEFORE INSERT ON test_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at)) BEGIN
  SELECT RAISE(ABORT, 'test_runs: started_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_test_runs_upd BEFORE UPDATE OF started_at ON test_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at)) BEGIN
  SELECT RAISE(ABORT, 'test_runs: started_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_metrics_ins BEFORE INSERT ON metrics
WHEN (NEW.recorded_at IS NOT NULL AND (NEW.recorded_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.recorded_at) IS NOT NEW.recorded_at)) BEGIN
  SELECT RAISE(ABORT, 'metrics: recorded_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_metrics_upd BEFORE UPDATE OF recorded_at ON metrics
WHEN (NEW.recorded_at IS NOT NULL AND (NEW.recorded_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.recorded_at) IS NOT NEW.recorded_at)) BEGIN
  SELECT RAISE(ABORT, 'metrics: recorded_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_step_conflicts_ins BEFORE INSERT ON step_conflicts
WHEN (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at)) BEGIN
  SELECT RAISE(ABORT, 'step_conflicts: resolved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_step_conflicts_upd BEFORE UPDATE OF resolved_at ON step_conflicts
WHEN (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at)) BEGIN
  SELECT RAISE(ABORT, 'step_conflicts: resolved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_artifacts_ins BEFORE INSERT ON artifacts
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'artifacts: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_artifacts_upd BEFORE UPDATE OF created_at ON artifacts
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'artifacts: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_events_ins BEFORE INSERT ON events
WHEN (NEW.at IS NOT NULL AND (NEW.at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.at) IS NOT NEW.at)) BEGIN
  SELECT RAISE(ABORT, 'events: at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_events_upd BEFORE UPDATE OF at ON events
WHEN (NEW.at IS NOT NULL AND (NEW.at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.at) IS NOT NEW.at)) BEGIN
  SELECT RAISE(ABORT, 'events: at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

-- ----- Plan step conflicts (plan step 143)

-- A plan is not approved while two of its steps touch the same thing unjudged (v_unresolved_conflicts).
CREATE TRIGGER tr_plan_approval_conflicts BEFORE UPDATE OF status ON plans
WHEN NEW.status = 'approved' AND OLD.status <> 'approved' AND EXISTS (
  SELECT 1 FROM step_conflicts c JOIN steps s ON s.id = c.step_a_id
  WHERE s.plan_id = NEW.id AND c.resolution = 'none' AND c.late_step_id IS NULL) BEGIN
  SELECT RAISE(ABORT, 'the plan has unresolved conflicts between steps (ledger.py conflicts lists them; judge each with resolve-conflict)');
END;

-- A step added after approval does not start while one of its conflicts is unjudged; other steps go on.
CREATE TRIGGER tr_step_start_conflicts BEFORE UPDATE OF status ON steps
WHEN NEW.status = 'doing' AND OLD.status <> 'doing' AND EXISTS (
  SELECT 1 FROM step_conflicts c WHERE c.late_step_id = NEW.id AND c.resolution = 'none') BEGIN
  SELECT RAISE(ABORT, 'this step has unresolved conflicts with other steps (ledger.py conflicts lists them; judge each with resolve-conflict)');
END;


-- ----- Feature proposals (schema 19, plan step 145)

-- A proposal starts as proposed, serving an approved design goal or an active requirement.
CREATE TRIGGER tr_feature_serves BEFORE INSERT ON feature_proposals BEGIN
  SELECT RAISE(ABORT, 'a feature proposal starts as proposed')
  WHERE NEW.status IS NOT 'proposed' OR NEW.step_id IS NOT NULL OR NEW.decided_at IS NOT NULL;
  SELECT RAISE(ABORT, 'a feature proposal serves an approved design goal or an active requirement')
  WHERE (NEW.goal_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM design_goals g JOIN goal_statuses s ON s.value = g.status
           WHERE g.id = NEW.goal_id AND s.served = 1))
     OR (NEW.requirement_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM requirements WHERE id = NEW.requirement_id AND status = 'active'));
END;

-- An idea the owner declined is not proposed again for the same goal or requirement.
CREATE TRIGGER tr_feature_not_again BEFORE INSERT ON feature_proposals BEGIN
  SELECT RAISE(ABORT, 'the owner declined this idea for the same goal; propose it again only for a changed goal')
  WHERE EXISTS (SELECT 1 FROM feature_proposals f
                WHERE f.status = 'declined' AND f.goal_id IS NEW.goal_id AND f.requirement_id IS NEW.requirement_id
                  AND lower(trim(f.title)) = lower(trim(NEW.title)));
END;

-- Logs the proposal as the first row of its history.
CREATE TRIGGER tr_feature_proposed AFTER INSERT ON feature_proposals BEGIN
  INSERT INTO feature_status_changes(proposal_id, from_status, to_status, note, actor_id, at)
  VALUES (NEW.id, NULL, NEW.status, NULL, NEW.proposed_by_actor_id, NEW.created_at);
END;

-- The moves: proposed to picked or declined, picked to built or declined, declined to picked; the gate, when
-- given, is a feature-pick gate of the proposal's review.
CREATE TRIGGER tr_feature_transition BEFORE UPDATE OF status, gate_id ON feature_proposals BEGIN
  SELECT RAISE(ABORT, 'a feature proposal moves proposed -> picked or declined, picked -> built or declined, declined -> picked')
  WHERE OLD.status IS NOT NEW.status
    AND NOT ((OLD.status = 'proposed' AND NEW.status IN ('picked','declined'))
          OR (OLD.status = 'picked' AND NEW.status IN ('built','declined'))
          OR (OLD.status = 'declined' AND NEW.status = 'picked'));
  SELECT RAISE(ABORT, 'the gate is not the feature-pick gate of the proposal''s review')
  WHERE NEW.gate_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM gates g WHERE g.id = NEW.gate_id AND g.kind = 'feature-pick' AND g.review_id = NEW.review_id);
END;

-- Logs each status change with its note, who made it and when.
CREATE TRIGGER tr_feature_status_logged AFTER UPDATE OF status ON feature_proposals
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO feature_status_changes(proposal_id, from_status, to_status, note, actor_id, at)
  VALUES (NEW.id, OLD.status, NEW.status, NEW.status_note, NEW.decided_by_actor_id, NEW.decided_at);
END;

-- What a proposal says stays as written: a changed idea is a new proposal.
CREATE TRIGGER tr_feature_fixed BEFORE UPDATE ON feature_proposals
WHEN NEW.code IS NOT OLD.code OR NEW.review_id IS NOT OLD.review_id OR NEW.goal_id IS NOT OLD.goal_id
  OR NEW.requirement_id IS NOT OLD.requirement_id OR NEW.title IS NOT OLD.title OR NEW.today IS NOT OLD.today
  OR NEW.evidence IS NOT OLD.evidence OR NEW.proposal IS NOT OLD.proposal OR NEW.value IS NOT OLD.value
  OR NEW.effort IS NOT OLD.effort OR NEW.risks IS NOT OLD.risks OR NEW.depends IS NOT OLD.depends
  OR NEW.done_test IS NOT OLD.done_test OR NEW.proposed_by_actor_id IS NOT OLD.proposed_by_actor_id
  OR NEW.created_at IS NOT OLD.created_at BEGIN
  SELECT RAISE(ABORT, 'a feature proposal is not edited; propose the changed idea as a new one');
END;

-- History is append-only.
CREATE TRIGGER tr_feature_no_delete BEFORE DELETE ON feature_proposals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: feature proposals cannot be deleted; decline one instead');
END;

-- A feature proposal's history is not changed.
CREATE TRIGGER tr_feature_history_no_update BEFORE UPDATE ON feature_status_changes BEGIN
  SELECT RAISE(ABORT, 'history is append-only: feature_status_changes cannot be changed');
END;

-- Nor deleted.
CREATE TRIGGER tr_feature_history_no_delete BEFORE DELETE ON feature_status_changes BEGIN
  SELECT RAISE(ABORT, 'history is append-only: feature_status_changes cannot be deleted');
END;

CREATE TRIGGER tr_time_feature_proposals_ins BEFORE INSERT ON feature_proposals
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) OR (NEW.decided_at IS NOT NULL AND (NEW.decided_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.decided_at) IS NOT NEW.decided_at)) BEGIN
  SELECT RAISE(ABORT, 'feature_proposals: created_at and decided_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_feature_proposals_upd BEFORE UPDATE OF created_at, decided_at ON feature_proposals
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) OR (NEW.decided_at IS NOT NULL AND (NEW.decided_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.decided_at) IS NOT NEW.decided_at)) BEGIN
  SELECT RAISE(ABORT, 'feature_proposals: created_at and decided_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_feature_status_changes_ins BEFORE INSERT ON feature_status_changes
WHEN (NEW.at IS NOT NULL AND (NEW.at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.at) IS NOT NEW.at)) BEGIN
  SELECT RAISE(ABORT, 'feature_status_changes: at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_feature_status_changes_upd BEFORE UPDATE OF at ON feature_status_changes
WHEN (NEW.at IS NOT NULL AND (NEW.at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.at) IS NOT NEW.at)) BEGIN
  SELECT RAISE(ABORT, 'feature_status_changes: at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

-- ----- Tool runs and leads

-- A tool run is a record: it is never changed or deleted.
CREATE TRIGGER tr_tool_run_fixed BEFORE UPDATE ON tool_runs BEGIN
  SELECT RAISE(ABORT, 'a tool run is a record: it cannot be changed (run the tool again)');
END;

-- Nor deleted.
CREATE TRIGGER tr_tool_run_no_delete BEFORE DELETE ON tool_runs BEGIN
  SELECT RAISE(ABORT, 'history is append-only: tool_runs cannot be deleted');
END;

-- A lead keeps what the tool said; only its disposition (cited, dismissed) is written later.
CREATE TRIGGER tr_tool_result_fixed BEFORE UPDATE ON tool_results
WHEN NEW.code IS NOT OLD.code OR NEW.tool_run_id IS NOT OLD.tool_run_id OR NEW.domain_code IS NOT OLD.domain_code
  OR NEW.rule IS NOT OLD.rule OR NEW.level IS NOT OLD.level OR NEW.path IS NOT OLD.path
  OR NEW.line_start IS NOT OLD.line_start OR NEW.line_end IS NOT OLD.line_end OR NEW.message IS NOT OLD.message
  OR NEW.fingerprint IS NOT OLD.fingerprint OR NEW.created_at IS NOT OLD.created_at BEGIN
  SELECT RAISE(ABORT, 'a lead keeps what the tool said: only cite-lead and dismiss-lead change it');
END;

-- A lead is addressed once: a cited or dismissed lead keeps that disposition.
CREATE TRIGGER tr_tool_result_disposed_once BEFORE UPDATE OF finding_id, dismissed_reason ON tool_results
WHEN OLD.finding_id IS NOT NULL OR OLD.dismissed_reason IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'the lead is already addressed (cited by a finding or dismissed)');
END;

-- The finding that cites a lead belongs to the review the tool ran for.
CREATE TRIGGER tr_tool_result_finding_review BEFORE UPDATE OF finding_id ON tool_results
WHEN NEW.finding_id IS NOT NULL AND (SELECT review_id FROM findings WHERE id = NEW.finding_id)
     IS NOT (SELECT review_id FROM tool_runs WHERE id = NEW.tool_run_id) BEGIN
  SELECT RAISE(ABORT, 'a lead is cited by a finding of the review its tool ran for');
END;

-- Leads are never deleted.
CREATE TRIGGER tr_tool_result_no_delete BEFORE DELETE ON tool_results BEGIN
  SELECT RAISE(ABORT, 'history is append-only: tool_results cannot be deleted');
END;

CREATE TRIGGER tr_time_tool_runs_ins BEFORE INSERT ON tool_runs
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'tool_runs: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_tool_runs_upd BEFORE UPDATE OF created_at ON tool_runs
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'tool_runs: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_tool_results_ins BEFORE INSERT ON tool_results
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) OR (NEW.disposed_at IS NOT NULL AND (NEW.disposed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.disposed_at) IS NOT NEW.disposed_at)) BEGIN
  SELECT RAISE(ABORT, 'tool_results: created_at and disposed_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_tool_results_upd BEFORE UPDATE OF created_at, disposed_at ON tool_results
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) OR (NEW.disposed_at IS NOT NULL AND (NEW.disposed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.disposed_at) IS NOT NEW.disposed_at)) BEGIN
  SELECT RAISE(ABORT, 'tool_results: created_at and disposed_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

-- ---------- Views ---------------------------------------------------------

-- Issues still to fix, most severe first, then oldest.
-- locations: the primary locations of the issue's real findings (v_real_findings), each prefixed with its
-- repository name when the ledger has more than one, so the same path in two repositories stays two places.
CREATE VIEW v_open_issues AS
SELECT i.code, i.title, i.severity, i.status, i.domain_code, i.theme_code, i.sub,
       i.times_seen, fr.code AS first_seen, fr.started_at AS first_seen_at,
       lr.code AS last_seen, cr.code AS last_checked, i.current_state, i.recommendation,
       (SELECT group_concat(DISTINCT CASE WHEN (SELECT count(*) FROM repositories) > 1 THEN rp.name || ':' ELSE '' END
                                     || fl.path || COALESCE('#' || fl.symbol, ''))
          FROM v_real_findings f JOIN finding_locations fl ON fl.finding_id = f.id
          JOIN repositories rp ON rp.id = fl.repository_id
         WHERE f.issue_id = i.id AND fl.role = 'primary') AS locations
FROM issues i
JOIN reviews fr ON fr.id = i.first_seen_review_id
LEFT JOIN reviews lr ON lr.id = i.last_seen_review_id
LEFT JOIN reviews cr ON cr.id = i.last_checked_review_id
WHERE i.status IN (SELECT value FROM issue_statuses WHERE active = 1)
ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END,
         fr.started_at;

-- The known issues each reviewer is told about before it starts, by domain.
-- Each comes with where it was seen (primary locations, as in v_open_issues) and the owner's standing decisions on
-- it (decided or to revisit).
CREATE VIEW v_known_issues_for_brief AS
SELECT i.domain_code, i.code, i.title, i.severity, i.status, i.current_state,
       i.recommendation, i.resolution,
       (SELECT group_concat(test_path || '::' || test_name, '; ')
          FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1) AS guard_tests,
       (SELECT group_concat(DISTINCT fl.path || COALESCE('#' || fl.symbol, ''))
          FROM findings f JOIN finding_locations fl ON fl.finding_id = f.id
         WHERE f.issue_id = i.id AND fl.role = 'primary') AS locations,
       (SELECT group_concat(x.line, '; ') FROM
          (SELECT d.code || ' (' || d.status || '): ' || d.question || COALESCE(' -> ' || d.answer, '') AS line
             FROM decision_issues di JOIN decisions d ON d.id = di.decision_id
            WHERE di.issue_id = i.id AND d.status IN ('decided','revisit') ORDER BY d.code) x) AS decisions
FROM issues i
WHERE i.status IN (SELECT value FROM issue_statuses WHERE briefed = 1)
ORDER BY i.domain_code, i.id;   -- by id, so ISS-9999 comes before ISS-10000

-- Issues raised by more than one review, or that came back after a fix.
-- Only real findings count (v_real_findings; times_seen counts them too): a false re-raise does not make an issue
-- recurring.
CREATE VIEW v_recurring_issues AS
SELECT i.code, i.title, i.severity, i.status, i.times_seen,
       (SELECT count(*) FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
          AND e.event = 'status-changed' AND e.to_value = 'regressed') AS regressions,
       (SELECT group_concat(r.code, ', ') FROM
          (SELECT DISTINCT f.review_id FROM v_real_findings f WHERE f.issue_id = i.id) x
          JOIN reviews r ON r.id = x.review_id) AS reviews
FROM issues i
WHERE i.times_seen > 1 OR EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue'
        AND e.entity_id = i.id AND e.to_value = 'regressed')
ORDER BY regressions DESC, i.times_seen DESC;

-- The full history of each issue, oldest event first.
-- Filter by code.
CREATE VIEW v_issue_timeline AS
SELECT i.code, e.at, e.event, e.from_value, e.to_value, r.code AS review,
       s.number AS step, e.commit_sha, a.name AS actor, e.note
FROM events e
JOIN issues i ON e.entity_type = 'issue' AND e.entity_id = i.id
LEFT JOIN reviews r ON r.id = e.review_id
LEFT JOIN steps s ON s.id = e.step_id
LEFT JOIN actors a ON a.id = e.actor_id
ORDER BY i.code, e.at, e.id;

-- How issues are related to each other.
-- Each relation once, in its stored direction (from_issue is a duplicate of, blocks ... to_issue), with codes.
CREATE VIEW v_issue_graph AS
SELECT a.code AS from_issue, r.relation, b.code AS to_issue, r.confidence, r.rationale,
       rv.code AS established_in
FROM issue_relations r
JOIN issues a ON a.id = r.from_issue_id
JOIN issues b ON b.id = r.to_issue_id
LEFT JOIN reviews rv ON rv.id = r.established_in_review_id;

-- The number of points each review raised, by domain and severity.
-- Also counts the verified and the false ones. Every count but false_findings is of real findings (the
-- v_real_findings rule, verification <> 'false'); false_findings counts the rest. came_back counts the issues of
-- this review's real findings that this review sent back, from their events (the trigger's own record): regressed,
-- reopened from a closed status, or raised again while accepted. r.code is a GROUP BY term so that a WHERE review =
-- ? filter is pushed into the findings scan (one review's findings, not every review's).
CREATE VIEW v_review_summary AS
SELECT r.code AS review, r.started_at, f.domain_code,
       sum(f.verification <> 'false') AS points,
       sum(f.verification <> 'false' AND f.severity = 'H') AS high,
       sum(f.verification <> 'false' AND f.severity = 'M') AS medium,
       sum(f.verification <> 'false' AND f.severity = 'L') AS low,
       sum(f.verification <> 'false' AND f.severity = 'T') AS tests,
       sum(f.verification = 'confirmed') AS confirmed,
       sum(f.verification = 'false') AS false_findings,
       sum(f.verification <> 'false' AND f.match_type IN ('new','continuation')) AS new_issues,  -- a continuation opens a new issue
       sum(f.verification <> 'false' AND f.match_type IN ('still-present','changed')) AS carried_over,
       (SELECT count(DISTINCT e.entity_id) FROM events e
         WHERE e.entity_type = 'issue' AND e.review_id = r.id
           AND ((e.event = 'status-changed' AND (e.to_value = 'regressed'
                 OR (e.to_value = 'open' AND e.from_value IN (SELECT value FROM issue_statuses WHERE reopens = 1))))
                OR e.event = 'recurred')
           AND EXISTS (SELECT 1 FROM v_real_findings f2 WHERE f2.review_id = r.id AND f2.issue_id = e.entity_id
                         AND f2.domain_code = f.domain_code)) AS came_back
FROM findings f JOIN reviews r ON r.id = f.review_id
GROUP BY r.id, r.code, f.domain_code
ORDER BY r.started_at, f.domain_code;

-- Each review with its point counts.
-- One row per review (0, not NULL, for a review with no findings): its real findings (v_real_findings) by
-- severity, those that opened an issue, and those whose match type says the issue came back
-- (match_types.came_back). The viewer's review lists read this view. r.code is a GROUP BY term so that a
-- WHERE review = ? filter is pushed into the findings scan.
CREATE VIEW v_review_totals AS
SELECT r.code AS review, r.started_at, count(f.id) AS points,
       COALESCE(sum(f.severity = 'H'), 0) AS high, COALESCE(sum(f.severity = 'M'), 0) AS medium,
       COALESCE(sum(f.severity = 'L'), 0) AS low, COALESCE(sum(f.severity = 'T'), 0) AS tests,
       COALESCE(sum(f.match_type IN ('new','continuation')), 0) AS new_issues,
       COALESCE(sum(f.match_type IN (SELECT value FROM match_types WHERE came_back = 1)), 0) AS came_back
FROM reviews r LEFT JOIN v_real_findings f ON f.review_id = r.id
GROUP BY r.id, r.code;

-- Points not yet matched to an issue.
-- Must be empty before consolidating. False findings too: a false finding is matched to an issue closed as
-- obsolete, so the ledger keeps what was claimed and why it was wrong. An abandoned review's findings are not
-- required to be matched.
CREATE VIEW v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL AND r.status <> 'abandoned';

-- Every point except those found to be false.
-- The one definition of a real finding. Counts (times_seen, v_recurring_issues, v_review_summary, v_file_hotspots,
-- v_open_issues' locations) use it; the must-be-empty checks do not, since a false finding must still be matched
-- (to an issue closed as obsolete) and closed by a plan step.
CREATE VIEW v_real_findings AS
SELECT * FROM findings WHERE verification <> 'false';

-- Known issues a review should have looked for and did not.
-- Must be empty. Counted in the domains that ran. Known = first seen in another review and opened before this one
-- started, compared as julianday() values (an instant, not text). An abandoned review needs no checks.
CREATE VIEW v_unchecked_known_issues AS
SELECT r.code AS review, i.code AS issue, i.domain_code, i.title
FROM reviews r
JOIN review_domains rd ON rd.review_id = r.id AND rd.decision = 'included'
JOIN issues i ON i.domain_code = rd.domain_code
WHERE r.status <> 'abandoned'
  AND i.first_seen_review_id <> r.id
  AND julianday(i.opened_at) < julianday(r.started_at)
  AND i.status IN (SELECT value FROM issue_statuses WHERE briefed = 1)
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = r.id AND c.issue_id = i.id);

-- Points that no step of the fix plan closes.
-- Must be empty. Counted over the reviews a plan covers. A finding is covered by a step of this plan, or by a done
-- step of a plan this plan supersedes (work already finished under the old plan). The probe goes from the finding
-- to its steps (ix_step_findings_find), never over a whole plan's steps per finding. False findings count too (the
-- plan's 'record false findings' step closes them). A dropped step covers nothing unless a decided decision is
-- linked to it (the owner chose not to do it). Superseded and abandoned plans, and abandoned reviews, are not
-- checked.
CREATE VIEW v_uncovered_findings AS
SELECT p.code AS plan, r.code AS review, f.point_id, f.severity, f.summary
FROM plans p
JOIN plan_reviews pr ON pr.plan_id = p.id
JOIN reviews r ON r.id = pr.review_id
JOIN findings f ON f.review_id = r.id
WHERE p.status NOT IN ('superseded','abandoned') AND r.status <> 'abandoned'
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                    JOIN plans sp ON sp.id = +s.plan_id
                  WHERE sf.finding_id = f.id
                    AND (sp.id = p.id OR (sp.superseded_by = p.id AND s.status = 'done'))
                    AND (s.status <> 'dropped'
                         OR EXISTS (SELECT 1 FROM decisions d WHERE d.step_id = s.id AND d.status = 'decided')));

-- Plan steps that close nothing, and points closed by several steps without a reason.
-- Must be empty. A dropped step, one that builds a picked feature proposal, or one whose no_findings_reason says
-- why it closes nothing by design (the follow-up review that verifies the plan), is not a problem; dropped steps do
-- not count towards closing a finding twice. Superseded and abandoned plans are not checked.
CREATE VIEW v_coverage_problems AS
SELECT p.code AS plan, 'step closes nothing' AS problem, CAST(s.number AS TEXT) AS item
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
  AND NULLIF(trim(s.no_findings_reason), '') IS NULL
  AND NOT EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM feature_proposals fp WHERE fp.step_id = s.id)
UNION ALL
SELECT p.code, 'finding closed by several steps without a reason', f.point_id
FROM step_findings sf JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
  -- only points linked to more than one step can be closed twice (read from ix_step_findings_find alone)
  AND sf.finding_id IN (SELECT finding_id FROM step_findings GROUP BY finding_id HAVING count(*) > 1)
GROUP BY p.id, sf.finding_id
HAVING count(*) > 1 AND sum(sf.multi_reason IS NULL) > 0;

-- How far each phase of a fix plan has come.
CREATE VIEW v_plan_progress AS
SELECT p.code AS plan, ph.number AS phase, ph.title,
       min(s.number) AS first_step, max(s.number) AS last_step, count(s.id) AS steps,
       COALESCE(sum(s.status = 'done'), 0) AS done, COALESCE(sum(s.status = 'doing'), 0) AS doing,
       COALESCE(sum(s.status = 'waiting-owner'), 0) AS waiting_owner,
       COALESCE(sum(s.status = 'not-started'), 0) AS not_started,
       COALESCE(sum(s.status = 'dropped'), 0) AS dropped   -- 0, not NULL, for a phase with no steps
FROM phases ph JOIN plans p ON p.id = ph.plan_id
LEFT JOIN steps s ON s.phase_id = ph.id
GROUP BY ph.id ORDER BY p.code, ph.number;

-- Each fix plan with its step counts.
-- One row per plan (0, not NULL, for a plan with no steps), with the reviews it covers. The viewer's plan lists
-- read this view.
CREATE VIEW v_plan_totals AS
SELECT p.code AS plan, p.title, p.status, p.created_at, p.approved_at, count(s.id) AS steps,
       COALESCE(sum(s.status = 'done'), 0) AS done, COALESCE(sum(s.status = 'doing'), 0) AS doing,
       COALESCE(sum(s.status = 'waiting-owner'), 0) AS waiting_owner,
       COALESCE(sum(s.status = 'not-started'), 0) AS not_started,
       COALESCE(sum(s.status = 'dropped'), 0) AS dropped,
       (SELECT group_concat(r.code, ', ') FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id
         WHERE pr.plan_id = p.id) AS reviews
FROM plans p LEFT JOIN steps s ON s.plan_id = p.id
GROUP BY p.id, p.code;

-- Gates still on the owner's list.
-- Open gates (to answer) and answered ones the agent has not acted on yet: the ones to answer first, blocking
-- before the rest, then oldest, each with the codes of what it is about. The viewer's inbox and its count of
-- questions read this view.
CREATE VIEW v_pending_gates AS
SELECT g.id, g.code, g.kind, g.title, g.question, g.status, g.blocking, g.created_at,
       r.code AS review_code, pl.code AS plan_code, s.number AS step_number, i.code AS issue_code,
       d.code AS decision_code
FROM gates g
LEFT JOIN reviews r ON r.id = g.review_id LEFT JOIN plans pl ON pl.id = g.plan_id
LEFT JOIN steps s ON s.id = g.step_id LEFT JOIN issues i ON i.id = g.issue_id
LEFT JOIN decisions d ON d.id = g.decision_id
WHERE g.status IN ('open','answered')
ORDER BY g.status = 'answered', g.blocking DESC, g.created_at;

-- Everything waiting on the owner.
-- Open gates, answered gates the agent has not acted on yet, and anything else waiting on the owner without a gate
-- (steps, decisions, and design goals proposed for a review and not on an open approval gate). Each thing once: a
-- step or decision with an open or answered gate (on it, or for a decision on its step) is listed as that gate. A
-- step's question falls back to its progress note, then to its change text, so it is never blank; it is blocking
-- when an unfinished step depends on it. question is what the owner is asked; context is what helps answer it
-- (a gate's context, what reopens a decision).
CREATE VIEW v_owner_queue AS
SELECT 'gate' AS kind, g.code AS ref, g.kind AS need, g.title, g.question,
       g.status, g.blocking, g.created_at, g.context
FROM gates g WHERE g.status IN ('open','answered')
UNION ALL
SELECT 'step', p.code || ' #' || s.number, s.owner_need, s.title,
       COALESCE(NULLIF(trim(s.owner_question), ''), NULLIF(trim(s.progress_note), ''), s.change), 'open',
       EXISTS (SELECT 1 FROM step_dependencies sd JOIN steps ds ON ds.id = sd.step_id
               WHERE sd.depends_on_step_id = s.id AND ds.status NOT IN ('done','dropped')), s.updated_at, NULL
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE s.status = 'waiting-owner'
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = s.id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'decision', d.code, 'decision',
       CASE d.status WHEN 'revisit' THEN 'A decision to look at again' ELSE 'A decision to make' END,
       d.question, d.status, 0, d.asked_at, d.revisit_when
FROM decisions d WHERE d.status IN ('open','revisit')
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.decision_id = d.id AND g.status IN ('open','answered'))
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = d.step_id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'goal', g.code, 'confirm', 'Approve a design goal', g.text, g.status, 1, g.adopted_at, NULL
FROM design_goals g WHERE g.status = 'proposed' AND g.review_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM gates x WHERE x.id = g.gate_id AND x.status IN ('open','answered'));

-- Every place in the code a point cites, with how to get back to the exact lines.
-- commit_sha is the location's own commit, else its review's commit for that repository (commit_from says which:
-- 'location', 'review', or NULL when neither is known; git_command is then NULL and v_untraceable_refs lists the
-- row as 'no commit').
CREATE VIEW v_code_refs AS
SELECT r.code AS review, f.point_id, i.code AS issue, fl.id AS location_id, fl.role,
       rp.name AS repository, rp.path AS repo_path, COALESCE(fl.commit_sha, rr.commit_sha) AS commit_sha,
       CASE WHEN fl.commit_sha IS NOT NULL THEN 'location' WHEN rr.commit_sha IS NOT NULL THEN 'review' END
         AS commit_from,
       rr.tag_name,
       fl.path, fl.line_start, fl.line_end, fl.symbol, fl.blob_sha, fl.permalink,
       -- every argument single-quoted for a POSIX shell (an embedded quote is closed, escaped and reopened);
       -- ledger.py show, /api/source and the viewer's Copy button all use this one command
       'git -C ''' || replace(COALESCE(NULLIF(rp.path, ''), '.'), '''', '''\''''') || ''' show '''
         || replace(COALESCE(fl.commit_sha, rr.commit_sha) || ':' || fl.path, '''', '''\''''') || ''''
         || CASE WHEN fl.line_start IS NOT NULL
                 THEN ' | sed -n ''' || fl.line_start || ',' || COALESCE(fl.line_end, fl.line_start) || 'p'''
                 ELSE '' END AS git_command,
       fl.excerpt IS NOT NULL AS has_excerpt
FROM finding_locations fl
JOIN findings f ON f.id = fl.finding_id
JOIN reviews r ON r.id = f.review_id
JOIN repositories rp ON rp.id = fl.repository_id
LEFT JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = fl.repository_id
LEFT JOIN issues i ON i.id = f.issue_id;

-- Cited code that cannot be traced back to its lines.
-- Must be empty after indexing. A whole-file reference (whole_file = 1) needs no symbol; a not-text one (binary or
-- not UTF-8) needs no excerpt. An excerpt whose line count differs from its line range (lines past the end of the
-- file, or a hand-written excerpt) is a problem, except a whole-file excerpt cut at 200 lines (excerpt_status
-- 'truncated'). An empty excerpt is no excerpt; a location whose commit differs from the commit its review looked
-- at is a problem (unless the finding was discovered during fix work, at a later commit: discovered_in_step_id); a
-- finding with no location at all is one too. An abandoned review's findings are not checked.
CREATE VIEW v_untraceable_refs AS
SELECT review, point_id, path, problem FROM (
  SELECT r.code AS review, f.point_id, fl.path,
         CASE WHEN fl.commit_sha IS NULL THEN 'no commit'
              WHEN rr.commit_sha IS NOT NULL AND fl.commit_sha <> rr.commit_sha AND f.discovered_in_step_id IS NULL
                THEN 'commit differs from the review commit'
              WHEN fl.line_start IS NULL AND fl.whole_file = 0 AND fl.symbol IS NULL THEN 'no lines or symbol'
              WHEN fl.line_start IS NULL AND fl.whole_file = 0 THEN 'symbol not resolved to lines'
              WHEN (fl.excerpt IS NULL OR fl.excerpt = '') AND fl.excerpt_status IS NOT 'not-text' THEN 'no excerpt'
              -- line count = newlines + 1 (an excerpt may also end with one extra newline)
              WHEN fl.excerpt_status IS NULL AND fl.line_start IS NOT NULL
                   AND length(fl.excerpt) - length(replace(fl.excerpt, char(10), '')) + 1
                       <> COALESCE(fl.line_end, fl.line_start) - fl.line_start + 1
                   AND NOT (substr(fl.excerpt, -1) = char(10)
                            AND length(fl.excerpt) - length(replace(fl.excerpt, char(10), ''))
                                = COALESCE(fl.line_end, fl.line_start) - fl.line_start + 1)
                THEN 'excerpt does not cover its lines (past the end of the file?)'
              WHEN rr.tag_name IS NULL THEN 'commit not tagged' END AS problem
  FROM finding_locations fl
  JOIN findings f ON f.id = fl.finding_id
  JOIN reviews r ON r.id = f.review_id
  LEFT JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = fl.repository_id
  WHERE r.status <> 'abandoned'
    -- the rows the CASE can name: the review's own terms here, the location's own from ix_locations_untraceable
    -- (its WHERE, repeated word for word, so the excerpts are read only when a location is written)
    AND (rr.tag_name IS NULL
         OR (rr.commit_sha IS NOT NULL AND fl.commit_sha <> rr.commit_sha AND f.discovered_in_step_id IS NULL)
         OR fl.id IN (SELECT id FROM finding_locations
                      WHERE commit_sha IS NULL OR (line_start IS NULL AND whole_file = 0)
                         OR ((excerpt IS NULL OR excerpt = '') AND excerpt_status IS NOT 'not-text')
                         OR (excerpt_status IS NULL AND line_start IS NOT NULL
                             AND length(excerpt) - length(replace(excerpt, char(10), '')) + 1
                                 <> COALESCE(line_end, line_start) - line_start + 1
                             AND NOT (substr(excerpt, -1) = char(10)
                                      AND length(excerpt) - length(replace(excerpt, char(10), ''))
                                          = COALESCE(line_end, line_start) - line_start + 1))))
  UNION ALL
  SELECT r.code, f.point_id, NULL, 'no location'
  FROM findings f JOIN reviews r ON r.id = f.review_id
  WHERE r.status <> 'abandoned'
    AND NOT EXISTS (SELECT 1 FROM finding_locations fl WHERE fl.finding_id = f.id)
) WHERE problem IS NOT NULL;

-- Defects found during fix work that no plan step closes yet.
-- Must be empty while the issue is still open (findings.discovered_in_step_id marks a discovery): a discovery goes
-- through the same process as a review finding. A step closes it when it is linked (step_findings, step not
-- dropped) to the finding itself or to another finding of the same issue.
CREATE VIEW v_unplanned_discoveries AS
SELECT p.code AS plan, s.number AS found_in_step, r.code AS review, f.point_id, i.code AS issue,
       i.status AS issue_status, f.summary
FROM findings f
JOIN steps s ON s.id = f.discovered_in_step_id
JOIN plans p ON p.id = s.plan_id
JOIN reviews r ON r.id = f.review_id
LEFT JOIN issues i ON i.id = f.issue_id
WHERE f.verification <> 'false'
  AND (i.id IS NULL OR i.status IN (SELECT value FROM issue_statuses WHERE active = 1))
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps cs ON cs.id = sf.step_id
                  WHERE sf.finding_id = f.id AND cs.status <> 'dropped')
  AND NOT EXISTS (SELECT 1 FROM findings f2 JOIN step_findings sf ON sf.finding_id = f2.id
                  JOIN steps cs ON cs.id = sf.step_id
                  WHERE f.issue_id IS NOT NULL AND f2.issue_id = f.issue_id AND cs.status <> 'dropped');

-- Review files recorded outside the ledger's folder.
-- A brief, report or index whose path is not under .code-review/ (or under the folder meta.ledger_location names,
-- for a ledger kept elsewhere): it was left in a scratch folder and will not travel with the ledger. A warning;
-- check also says when a recorded file is missing from disk.
CREATE VIEW v_artifacts_outside_ledger AS
SELECT r.code AS review, a.kind, a.path
FROM artifacts a LEFT JOIN reviews r ON r.id = a.review_id
WHERE a.path IS NOT NULL AND a.kind IN ('brief','report','index')
  AND replace(a.path, char(92), '/') NOT LIKE '.code-review/%'
  AND replace(a.path, char(92), '/') NOT LIKE
      replace(COALESCE((SELECT value FROM meta WHERE key = 'ledger_location'), '.code-review'), char(92), '/') || '/%';

-- Fixed issues with no test guarding the fix, or whose guard test is gone.
-- Issues of kind docs or design need no guard test.
CREATE VIEW v_unguarded_fixes AS
SELECT i.code, i.title, i.status,
       CASE WHEN NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id)
            THEN 'no guard test' ELSE 'guard test removed' END AS problem
FROM issues i
WHERE i.status IN (SELECT value FROM issue_statuses WHERE fixed = 1) AND COALESCE(i.kind, '') NOT IN ('docs','design')
  AND NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1);

-- How each measured number changed from review to review.
-- Label and direction come from metric_defs; a metric without a unit takes the catalog's.
CREATE VIEW v_metric_trends AS
SELECT m.name, m.scope, COALESCE(rp.name, 'all') AS repository, r.code AS review,
       r.started_at, m.value, COALESCE(m.unit, md.unit) AS unit, m.method,
       COALESCE(md.label, upper(substr(m.name, 1, 1)) || replace(substr(m.name, 2), '_', ' ')) AS label,
       COALESCE(md.better, 'none') AS better
FROM metrics m JOIN reviews r ON r.id = m.review_id
LEFT JOIN repositories rp ON rp.id = m.repository_id
LEFT JOIN metric_defs md ON md.name = m.name
ORDER BY m.name, m.scope, r.started_at;

-- Files that keep producing issues.
-- Per repository (the same path in two repositories is two files), from real findings only (v_real_findings).
CREATE VIEW v_file_hotspots AS
SELECT rp.name AS repository, fl.path, count(DISTINCT f.issue_id) AS issues,
       count(DISTINCT f.review_id) AS reviews, sum(f.severity = 'H') AS high_points
FROM finding_locations fl JOIN v_real_findings f ON f.id = fl.finding_id
JOIN repositories rp ON rp.id = fl.repository_id
GROUP BY fl.repository_id, fl.path ORDER BY issues DESC, high_points DESC, rp.name, fl.path;

-- Decisions the owner has already made, so a later review does not raise them again.
CREATE VIEW v_standing_decisions AS
SELECT d.code, d.question, d.answer, d.rationale, d.revisit_when, d.decided_at,
       (SELECT group_concat(i.code, ', ') FROM decision_issues di
          JOIN issues i ON i.id = di.issue_id WHERE di.decision_id = d.id) AS issues
FROM decisions d WHERE d.status = 'decided';

-- What each plan step closes: its points and their issues.
-- Each finding the step closes and that finding's issue with its status.
CREATE VIEW v_step_issues AS
SELECT p.code AS plan, s.number AS step, s.title AS step_title, s.status AS step_status,
       r.code AS review, f.point_id, i.code AS issue, i.title AS issue_title, i.status AS issue_status
FROM steps s
JOIN plans p ON p.id = s.plan_id
JOIN step_findings sf ON sf.step_id = s.id
JOIN findings f ON f.id = sf.finding_id
JOIN reviews r ON r.id = f.review_id
LEFT JOIN issues i ON i.id = f.issue_id
ORDER BY p.code, s.number, r.code, f.point_id;

-- The next free code for each kind of record.
-- The highest number + 1, zero-padded as the code triggers require (ISS-0001 ..., G-01 ..., IQ-0001 ...). Write
-- commands take codes from here (`ledger.py next-code ISS`), never by hand.
CREATE VIEW v_next_codes AS
SELECT 'ISS' AS prefix, 'ISS-' || printf('%04d', COALESCE(max(CAST(substr(code, 5) AS INTEGER)), 0) + 1) AS next_code
FROM issues
UNION ALL SELECT 'R', 'R-' || printf('%04d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM reviews
UNION ALL SELECT 'P', 'P-' || printf('%04d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM plans
UNION ALL SELECT 'D', 'D-' || printf('%04d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM decisions
UNION ALL SELECT 'GATE', 'GATE-' || printf('%04d', COALESCE(max(CAST(substr(code, 6) AS INTEGER)), 0) + 1) FROM gates
UNION ALL SELECT 'REQ', 'REQ-' || printf('%04d', COALESCE(max(CAST(substr(code, 5) AS INTEGER)), 0) + 1)
FROM requirements
UNION ALL SELECT 'G', 'G-' || printf('%02d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM design_goals
UNION ALL SELECT 'IQ', 'IQ-' || printf('%04d', COALESCE(max(CAST(substr(code, 4) AS INTEGER)), 0) + 1)
FROM interview_questions
UNION ALL SELECT 'FP', 'FP-' || printf('%04d', COALESCE(max(CAST(substr(code, 4) AS INTEGER)), 0) + 1)
FROM feature_proposals
UNION ALL SELECT 'LD', 'LD-' || printf('%04d', COALESCE(max(CAST(substr(code, 4) AS INTEGER)), 0) + 1)
FROM tool_results;

-- Plan steps still to do whose issues are already closed.
-- Closed for example by a review's check of 'fixed': mark the step done or dropped. Superseded and abandoned plans
-- are left out. Listed by `ledger.py gates`; not one of the checks.
CREATE VIEW v_stale_steps AS
SELECT p.code AS plan, s.number AS step, s.title, s.status,
       (SELECT group_concat(x.issue, ', ') FROM
          (SELECT DISTINCT i.code || ' ' || i.status AS issue FROM step_findings sf
             JOIN findings f ON f.id = sf.finding_id JOIN issues i ON i.id = f.issue_id
            WHERE sf.step_id = s.id ORDER BY 1) x) AS issues
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE s.status NOT IN ('done','dropped')
  AND p.status NOT IN ('superseded','abandoned')
  AND EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN findings f ON f.id = sf.finding_id
                  LEFT JOIN issues i ON i.id = f.issue_id
                  WHERE sf.step_id = s.id
                    AND (i.id IS NULL OR i.status IN
                         (SELECT value FROM issue_statuses WHERE active = 1)));

-- The commit the latest finished review looked at, per repository.
-- Finished means handed-off. It is the start of the next review's `git log <commit>..HEAD`.
CREATE VIEW v_last_reviewed_commit AS
SELECT rp.name AS repository, rp.path AS repo_path, r.code AS review, rr.commit_sha, rr.branch, rr.tag_name,
       r.completed_at
FROM repositories rp
JOIN review_repositories rr ON rr.repository_id = rp.id
JOIN reviews r ON r.id = rr.review_id
WHERE r.id = (SELECT r2.id FROM review_repositories rr2 JOIN reviews r2 ON r2.id = rr2.review_id
              WHERE rr2.repository_id = rp.id AND r2.status = 'handed-off'
              ORDER BY COALESCE(r2.completed_at, r2.started_at) DESC, r2.id DESC LIMIT 1)
ORDER BY rp.name;

-- Whether the code does what each requirement says, per review.
-- Every verdict, with the count of that verdict in that review.
CREATE VIEW v_requirement_trace AS
SELECT r.code AS review, q.code AS requirement, q.text, c.verdict, c.where_implemented, c.tested,
       f.point_id, c.evidence,
       (SELECT count(*) FROM review_requirement_checks c2
         WHERE c2.review_id = c.review_id AND c2.verdict = c.verdict) AS verdict_count
FROM review_requirement_checks c
JOIN reviews r ON r.id = c.review_id
JOIN requirements q ON q.id = c.requirement_id
LEFT JOIN findings f ON f.id = c.finding_id
ORDER BY r.code, q.code;

-- Every agent that worked on a review.
-- Its manager, each reviewer run's agent and the agents in review_actors.
CREATE VIEW v_review_agents AS
SELECT r.code AS review, x.role, a.id AS actor_id, a.kind, a.name, a.model, a.harness, a.session_ref,
       pa.name AS parent
FROM (SELECT id AS review_id, manager_actor_id AS actor_id, 'manager' AS role FROM reviews
       WHERE manager_actor_id IS NOT NULL
      UNION SELECT review_id, actor_id, label FROM reviewer_runs
      UNION SELECT review_id, actor_id, role_label FROM review_actors) x
JOIN reviews r ON r.id = x.review_id
JOIN actors a ON a.id = x.actor_id
LEFT JOIN actors pa ON pa.id = a.parent_actor_id
ORDER BY r.code, CASE a.kind WHEN 'manager' THEN 0 WHEN 'reviewer' THEN 1 ELSE 2 END, x.role, a.id;

-- Records whose history is missing an entry.
-- Must be empty. A gap is an issue with no 'opened' event, an issue whose status is not the last status its events
-- logged, and a gate answered or applied with no event reaching that status. The triggers write these events, so a
-- gap means a row was written with the triggers bypassed.
CREATE VIEW v_history_gaps AS
SELECT 'issue' AS kind, i.code AS ref, 'no opened event' AS problem
FROM issues i
WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id AND e.event = 'opened')
UNION ALL
SELECT 'issue', x.code, 'status ' || x.status || ' not in its history (last logged: ' || x.last || ')'
FROM (SELECT i.code, i.status,
             (SELECT e.to_value FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
                AND e.event IN ('opened','status-changed') ORDER BY e.id DESC LIMIT 1) AS last
        FROM issues i) x
WHERE x.last IS NOT NULL AND x.last IS NOT x.status
UNION ALL
SELECT 'gate', g.code, g.status || ' with no event'
FROM gates g
WHERE g.status IN ('answered','applied')
  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'gate' AND e.entity_id = g.id
                    AND e.to_value = g.status);

-- Points matched to an issue in a way the issue's history contradicts.
-- Must be empty. The rules: a continuation needs a continuation-of relation from its new issue; 'new' (or
-- 'continuation') is for an issue this review opened; 'still-present', 'changed', 'recurrence' and 'regression' are
-- for an issue an earlier review opened; a recurrence is of an issue that was closed at some point; a real finding
-- still present on an issue fixed before this review started is a regression (match it so: it then regresses the
-- issue). Abandoned reviews are not checked.
CREATE VIEW v_match_problems AS
SELECT review, point_id, issue, match_type, problem FROM (
  SELECT r.code AS review, f.point_id, i.code AS issue, f.match_type,
         CASE WHEN f.match_type = 'continuation'
                   AND NOT EXISTS (SELECT 1 FROM issue_relations ir WHERE ir.from_issue_id = i.id
                                     AND ir.relation = 'continuation-of')
                THEN 'continuation with no continuation-of relation'
              WHEN f.match_type IN ('new','continuation') AND i.first_seen_review_id <> f.review_id
                THEN 'matched as new to an issue first seen in another review'
              WHEN f.match_type IN ('still-present','changed','recurrence','regression')
                   AND i.first_seen_review_id = f.review_id
                THEN 'matched as seen before to an issue this review opened'
              WHEN f.match_type = 'recurrence'
                   AND NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
                                     AND e.event IN ('opened','status-changed')
                                     AND e.to_value IN ('fixed','verified','accepted-risk','wont-fix',
                                                        'duplicate','obsolete'))
                THEN 'recurrence of an issue that was never closed'
              WHEN f.match_type IN ('still-present','changed') AND f.verification <> 'false'
                   AND i.status IN ('fixed','verified') AND i.resolved_at IS NOT NULL
                   AND julianday(i.resolved_at) < julianday(r.started_at)
                THEN 'still present on a fixed issue: match it as a regression' END AS problem
  FROM findings f
  JOIN reviews r ON r.id = f.review_id
  JOIN issues i ON i.id = f.issue_id
  WHERE r.status <> 'abandoned'
) WHERE problem IS NOT NULL;

-- Plan steps that close a point from a review the plan does not cover.
-- Must be empty: add the review back to plan_reviews or close the finding in its own plan. tr_step_finding_in_plan
-- refuses such a link when it is made; this lists links made before it, or left behind when a plan_reviews row was
-- removed.
CREATE VIEW v_off_plan_coverage AS
-- Read from the plans and the reviews they do not cover (CROSS JOIN keeps that order), so a plan that covers every
-- review reads no point at all.
SELECT p.code AS plan, s.number AS step, r.code AS review, f.point_id
FROM plans p
CROSS JOIN reviews r
CROSS JOIN findings f ON f.review_id = r.id
CROSS JOIN step_findings sf ON sf.finding_id = f.id
CROSS JOIN steps s ON s.id = sf.step_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.plan_id = p.id
  AND NOT EXISTS (SELECT 1 FROM plan_reviews pr WHERE pr.plan_id = p.id AND pr.review_id = r.id);

-- Finished plan steps whose issues are still to fix.
-- Must be empty: when a step is done, its issues move on (fixed with after_state, or another closing status),
-- unless another unfinished step still works on them. Regressed issues are left to the next plan. Superseded and
-- abandoned plans are not checked.
CREATE VIEW v_done_steps_open_issues AS
-- Read from the issues still to fix (ix_issues_status; CROSS JOIN keeps that order), fewer than the done steps' points.
SELECT DISTINCT p.code AS plan, s.number AS step, i.code AS issue, i.status AS issue_status
FROM issues i
CROSS JOIN findings f ON f.issue_id = i.id
CROSS JOIN step_findings sf ON sf.finding_id = f.id
CROSS JOIN steps s ON s.id = sf.step_id
JOIN plans p ON p.id = s.plan_id
WHERE s.status = 'done' AND p.status NOT IN ('superseded','abandoned') AND f.verification <> 'false'
  AND i.status IN ('open','needs-decision','planned','in-progress')
  AND NOT EXISTS (SELECT 1 FROM findings f2 JOIN step_findings sf2 ON sf2.finding_id = f2.id
                  JOIN steps s2 ON s2.id = sf2.step_id JOIN plans p2 ON p2.id = s2.plan_id
                  WHERE f2.issue_id = i.id AND s2.status NOT IN ('done','dropped')
                    AND p2.status NOT IN ('superseded','abandoned'))
ORDER BY p.code, s.number, i.id;

-- Issues closed as an accepted risk or as not to be fixed, with no reason given.
-- Must be empty: the reason is the record. New closings are refused by tr_issue_closed_needs_reason; this lists
-- rows written before it.
CREATE VIEW v_closed_without_reason AS
SELECT code, title, status FROM issues
WHERE status IN ('accepted-risk','wont-fix') AND NULLIF(trim(resolution), '') IS NULL;

-- Pairs of issues recorded as related twice, once in each direction.
-- Must be empty. A related-to pair stored in both directions (related-to is symmetric, so one row says it;
-- tr_relation_noreverse_dup refuses the second for new rows). The later row is listed.
CREATE VIEW v_relation_problems AS
SELECT a.code AS from_issue, b.code AS to_issue, ir.relation, 'related-to stored in both directions' AS problem
FROM issue_relations ir
JOIN issues a ON a.id = ir.from_issue_id
JOIN issues b ON b.id = ir.to_issue_id
WHERE ir.relation = 'related-to'
  AND EXISTS (SELECT 1 FROM issue_relations rv WHERE rv.relation = 'related-to'
                AND rv.from_issue_id = ir.to_issue_id AND rv.to_issue_id = ir.from_issue_id AND rv.id < ir.id);

-- Checks of a known issue that cite a point belonging to another issue.
-- Must be empty: a check's finding is the evidence for that issue.
CREATE VIEW v_check_finding_owner AS
SELECT r.code AS review, i.code AS issue, f.point_id, fi.code AS finding_issue
FROM review_issue_checks c
JOIN reviews r ON r.id = c.review_id
JOIN issues i ON i.id = c.issue_id
JOIN findings f ON f.id = c.finding_id
JOIN issues fi ON fi.id = f.issue_id
WHERE f.issue_id <> c.issue_id;

-- The owner's approved design goals, as given to reviewers.
-- Whole-project goals (domain_code NULL) first, then by domain: a reviewer of domain D gets the rows of D and the
-- whole-project ones (`ledger.py goals --domain D`). sources: the interview questions each goal summarizes.
CREATE VIEW v_goals_for_brief AS
SELECT g.domain_code, g.code, g.text,
       (SELECT group_concat(x.code, ', ') FROM
          (SELECT q.code FROM goal_sources gs JOIN interview_questions q ON q.id = gs.question_id
            WHERE gs.goal_id = g.id ORDER BY q.id) x) AS sources
FROM design_goals g
WHERE g.status IN (SELECT value FROM goal_statuses WHERE served = 1)
ORDER BY g.domain_code IS NOT NULL, g.domain_code, g.id;

-- Pairs of plan steps that touch the same thing and have not been judged.
-- Must be empty before a plan is approved (`ledger.py conflicts`, then resolve-conflict for each pair).
CREATE VIEW v_unresolved_conflicts AS
SELECT p.code AS plan, a.number AS step_a, b.number AS step_b, c.kind, c.target, c.reason, c.opposite
FROM step_conflicts c JOIN steps a ON a.id = c.step_a_id JOIN steps b ON b.id = c.step_b_id
JOIN plans p ON p.id = a.plan_id
WHERE c.resolution = 'none' AND c.late_step_id IS NULL
ORDER BY p.code, a.number, b.number, c.kind, c.target;

-- Steps added after their plan was approved that touch what another step touches, not judged yet.
-- Each blocks only the start of the step that was added.
CREATE VIEW v_late_step_conflicts AS
SELECT p.code AS plan, l.number AS step, CASE WHEN l.id = c.step_a_id THEN b.number ELSE a.number END AS other_step,
       c.kind, c.target, c.reason
FROM step_conflicts c JOIN steps a ON a.id = c.step_a_id JOIN steps b ON b.id = c.step_b_id
JOIN steps l ON l.id = c.late_step_id JOIN plans p ON p.id = a.plan_id
WHERE c.resolution = 'none'
ORDER BY p.code, l.number, other_step, c.kind, c.target;

-- The order to work each plan's steps in.
-- Each step comes after every step it depends on, directly or not, and otherwise keeps its number's place. position counts from 1 per plan; depth is the longest chain of steps it waits
-- for; depends_on lists the steps it depends on directly. A step whose dependencies all have lower numbers keeps its
-- place; one that waits for a later-numbered step moves to just after it.
CREATE VIEW v_step_order AS
WITH RECURSIVE n(c) AS (SELECT count(*) FROM steps),
walk(step_id, ancestor_id, depth) AS (
  SELECT id, id, 0 FROM steps
  UNION
  SELECT w.step_id, d.depends_on_step_id, w.depth + 1
  FROM walk w JOIN step_dependencies d ON d.step_id = w.ancestor_id JOIN n ON w.depth < n.c),
reach(step_id, plan_id, number, after_number, depth) AS (
  SELECT w.step_id, s.plan_id, s.number, max(a.number), max(w.depth)
  FROM walk w JOIN steps a ON a.id = w.ancestor_id JOIN steps s ON s.id = w.step_id GROUP BY w.step_id)
SELECT p.code AS plan, s.number AS step, s.title, s.status,
       (SELECT count(*) FROM reach o WHERE o.plan_id = r.plan_id
          AND (o.after_number < r.after_number OR (o.after_number = r.after_number AND (o.depth < r.depth
               OR (o.depth = r.depth AND o.number <= r.number))))) AS position,
       r.depth,
       (SELECT group_concat(x.number, ',') FROM (SELECT b.number FROM step_dependencies d
          JOIN steps b ON b.id = d.depends_on_step_id WHERE d.step_id = s.id ORDER BY b.number) x) AS depends_on
FROM reach r JOIN steps s ON s.id = r.step_id JOIN plans p ON p.id = s.plan_id;

-- Every feature proposal with what it serves and where it stands, ranked.
-- rank orders them best first: by how far each advances its goal (value), then by how little it costs (effort); ties
-- go by code. serves is the goal or requirement code, step the plan step that builds it (P-0001#12).
CREATE VIEW v_feature_proposals AS
SELECT f.code, r.code AS review, COALESCE(g.code, q.code) AS serves, COALESCE(g.text, q.text) AS serves_text,
       g.domain_code AS domain, f.title, f.today, f.evidence, f.proposal, f.value, f.effort, f.risks, f.depends,
       f.done_test, f.status, f.status_note, 'A-' || printf('%04d', a.id) AS decided_by, f.decided_at, gt.code AS gate,
       CASE WHEN s.id IS NULL THEN NULL ELSE p.code || '#' || s.number END AS step,
       vl.position * 10 + el.position AS rank, f.created_at
FROM feature_proposals f JOIN reviews r ON r.id = f.review_id
LEFT JOIN design_goals g ON g.id = f.goal_id LEFT JOIN requirements q ON q.id = f.requirement_id
LEFT JOIN actors a ON a.id = f.decided_by_actor_id LEFT JOIN gates gt ON gt.id = f.gate_id
LEFT JOIN steps s ON s.id = f.step_id LEFT JOIN plans p ON p.id = s.plan_id
LEFT JOIN enum_labels vl ON vl.enum = 'feature_value' AND vl.value = f.value
LEFT JOIN enum_labels el ON el.enum = 'feature_effort' AND el.value = f.effort;

-- Every lead with its disposition: cited by a finding, dismissed with a reason, or still open.
-- cited_by is the finding that cites it (R-0001:SEC-H3), through cite-lead or a location on the same lines.
CREATE VIEW v_tool_results AS
SELECT t.code AS lead, r.code AS review, tr.tool, t.domain_code AS domain, t.rule, t.level, t.path, t.line_start,
       t.line_end, t.message,
       CASE WHEN t.finding_id IS NOT NULL OR c.id IS NOT NULL THEN 'cited'
            WHEN t.dismissed_reason IS NOT NULL THEN 'dismissed' ELSE 'open' END AS disposition,
       r.code || ':' || COALESCE(f.point_id, c.point_id) AS cited_by, t.dismissed_reason,
       'A-' || printf('%04d', t.disposed_by_actor_id) AS disposed_by, t.disposed_at, cf.code AS carried_from,
       t.fingerprint, t.tool_run_id
FROM tool_results t JOIN tool_runs tr ON tr.id = t.tool_run_id JOIN reviews r ON r.id = tr.review_id
LEFT JOIN findings f ON f.id = t.finding_id
LEFT JOIN tool_results cf ON cf.id = t.carried_from_id
LEFT JOIN findings c ON c.id = (
  SELECT fl.finding_id FROM finding_locations fl JOIN findings ff ON ff.id = fl.finding_id
  WHERE t.finding_id IS NULL AND ff.review_id = tr.review_id AND t.path IS NOT NULL AND fl.path = t.path
    AND (tr.repository_id IS NULL OR fl.repository_id = tr.repository_id)
    AND (t.line_start IS NULL OR fl.line_start IS NULL
         OR (fl.line_start <= COALESCE(t.line_end, t.line_start)
             AND COALESCE(fl.line_end, fl.line_start) >= t.line_start))
  ORDER BY ff.point_id LIMIT 1);

-- Tool results no finding cites and no reviewer dismissed: must be empty.
-- A lead is addressed by a finding of the same review whose location covers its lines, by cite-lead, or by
-- dismiss-lead with a reason (lint-report also records the dismissals a report lists under its Leads section).
-- Leads of an abandoned review are left out.
CREATE VIEW v_unaddressed_tool_results AS
SELECT v.review, v.lead, v.tool, v.domain, v.rule, v.level,
       COALESCE(v.path, '') || CASE WHEN v.line_start IS NULL THEN '' ELSE ':' || v.line_start END AS location,
       v.message, v.tool_run_id
FROM v_tool_results v JOIN reviews r ON r.code = v.review
WHERE v.disposition = 'open' AND r.status <> 'abandoned'
ORDER BY v.review, v.lead;

-- Every tool run of every review, newest last, with how many leads it gave and how many are still unaddressed.
-- The tools page and `ledger.py env` read it; absent and skipped runs carry the fallback the reviewer follows.
CREATE VIEW v_tool_runs AS
SELECT t.id, r.code AS review, t.tool, t.domain_code AS domain, t.status, t.version, t.pinned_version,
       CASE WHEN t.version IS NULL OR t.pinned_version IS NULL THEN NULL
            ELSE t.version = t.pinned_version END AS pinned_match,
       rp.name AS repo, t.commit_sha, t.command, t.exit_code, t.duration_ms, t.output_format, t.output_path,
       t.output_sha256, t.rules_sha256, t.online, t.fallback, t.note,
       (SELECT count(*) FROM tool_results x WHERE x.tool_run_id = t.id) AS leads,
       (SELECT count(*) FROM v_unaddressed_tool_results u WHERE u.tool_run_id = t.id) AS unaddressed,
       t.created_at
FROM tool_runs t JOIN reviews r ON r.id = t.review_id LEFT JOIN repositories rp ON rp.id = t.repository_id;

-- ---------- Catalog seed (extend with custom domains/themes as needed) ---

INSERT INTO domains(code, name, category, report_file) VALUES
 ('spec','Spec conformance','correctness','spec.md'),
 ('tests','Test coverage','correctness','tests-<module>.md'),
 ('test-quality','Test suite quality','correctness','test-quality.md'),
 ('time-concurrency','Time, dates and concurrency','correctness','time-concurrency.md'),
 ('resilience','Failure handling and resilience','correctness','resilience.md'),
 ('compatibility','Backward compatibility and migrations','correctness','compatibility.md'),
 ('decision-placement','Decision placement','architecture','decision-placement.md'),
 ('contracts','Internal contracts','architecture','contracts.md'),
 ('server','Backend / server','architecture','server.md'),
 ('database','Database','architecture','database.md'),
 ('client-api','Backend-to-client API','architecture','client-api.md'),
 ('abstraction','Abstraction and shared code','architecture','abstraction.md'),
 ('hotspots','Hotspots from history','architecture','hotspots.md'),
 ('ui','Front end / UI','user-facing','ui-<area>.md'),
 ('walkthroughs','Task walkthroughs','user-facing','walkthroughs.md'),
 ('notifications','Email and notifications','user-facing','notifications.md'),
 ('localization','Localization','user-facing','localization.md'),
 ('security','Security','security-operations','security.md'),
 ('privacy','Privacy and data map','security-operations','privacy.md'),
 ('logging','Logging','security-operations','logging.md'),
 ('operations','Operations, CI/CD and recovery','security-operations','operations.md'),
 ('dependencies','Dependencies and supply chain','security-operations','dependencies.md'),
 ('performance','Performance','security-operations','performance.md'),
 ('cost','Cost','security-operations','cost.md'),
 ('docs-developer','Developer documentation','documentation','docs-developer.md'),
 ('docs-user','End-user documentation','documentation','docs-user.md'),
 ('handover','Maintainability and handover','documentation','handover.md'),
 ('features','Features toward the goals','architecture','features.md');

INSERT INTO themes(code, name) VALUES
 ('spec-mismatch','Spec mismatch'),('sign-in','Sign-in and sessions'),('authorization','Authorization'),
 ('ops-security','Operations security'),('privacy','Privacy and retention'),('access','Access to links and pages'),
 ('validation','Input validation'),('test-gaps','Test gaps'),('test-quality','Test suite quality'),
 ('time','Time and dates'),('concurrency','Concurrency'),('failure-handling','Failure handling and idempotency'),
 ('compatibility','Compatibility and migrations'),('defect','Defects'),('decision-placement','Decision placement'),
 ('contracts','Inter-component contracts'),('workflow','Approvals and workflow flows'),
 ('db-integrity','Database integrity, history, operations'),('client-api','Client API'),
 ('generic','Generic text and logic'),('config-display','Display of configuration'),
 ('internal-names','Internal names on screen'),('select-not-type','Select, not type'),
 ('usability','Usability and accessibility'),('task-flows','Task flows'),('shared-frontend','Shared front end'),
 ('shared-backend','Shared backend code'),('perf-backend','Performance: backend'),('perf-db','Performance: database'),
 ('perf-frontend','Performance: front end'),('cost','Cost'),('email-jobs-feeds','Email, jobs and feeds'),
 ('logging','Logging and audit'),('monitoring','Alerting and monitoring'),('ci-release','CI and release'),
 ('backup-recovery','Backup and recovery'),('dependencies','Dependencies'),('docs-inline','Inline developer docs'),
 ('docs-repo','Repo developer docs'),('docs-user','End-user docs'),('handover','Handover and ownership'),
 ('localization','Localization'),('verify','Verify'),('other','Other');

-- Each domain's one-line "applies when" sentence from SKILL.md's domain catalog.
WITH applies(code, text) AS (VALUES
 ('spec','Applies whenever there are stated requirements anywhere.'),
 ('tests','Almost always applies.'),
 ('test-quality','Applies when there is a meaningful test suite.'),
 ('time-concurrency','Applies to anything with dates, schedules, deadlines, expiries, or more than one writer.'),
 ('resilience','Applies whenever there are external calls, jobs, or multi-step writes.'),
 ('compatibility','Applies whenever there is stored data, saved settings, public APIs, or deployed clients.'),
 ('decision-placement','Applies when the system has business rules.'),
 ('contracts','Applies when there is more than one component.'),
 ('server','Applies when there is a server.'),
 ('database','Applies when there is a database.'),
 ('client-api','Applies when a client talks to a backend.'),
 ('abstraction','Almost always applies.'),
 ('hotspots','Applies when there is git history worth mining (a few months or more).'),
 ('ui','Applies when there are screens.'),
 ('walkthroughs','Applies when users complete multi-step tasks.'),
 ('notifications','Applies when the system sends email, SMS, push or calendar feeds.'),
 ('localization','Applies only if other languages, regions or conventions are plausible.'),
 ('security','Almost always applies.'),
 ('privacy','Applies whenever personal data is handled.'),
 ('logging','Almost always applies.'),
 ('operations','Applies when the system is deployed.'),
 ('dependencies','Almost always applies.'),
 ('performance','Applies when speed or scale matters to users or cost.'),
 ('cost','Applies when the system runs on paid infrastructure or services.'),
 ('docs-developer','Almost always applies.'),
 ('docs-user','Applies when the system has users other than its developers.'),
 ('handover','Applies whenever someone other than the current author may need to run or change the system.'),
 ('features','Applies when the review has approved goals or stated requirements.'))
UPDATE domains SET description = (SELECT text FROM applies WHERE applies.code = domains.code)
 WHERE description IS NULL AND code IN (SELECT code FROM applies);

-- The rule by which scan proposes each domain (the table "Scan rules" in references/domains.md).
INSERT INTO domain_rules(domain_code, rule) VALUES
 ('spec','requirements >= 1 or spec_files >= 1'),
 ('tests','code_files >= 1'),
 ('test-quality','test_files >= 5'),
 ('time-concurrency','time_files >= 1 or concurrency_files >= 1'),
 ('resilience','external_call_files >= 1 or job_files >= 1 or db_files >= 1'),
 ('compatibility','db_files >= 1 or api_files >= 1 or config_files >= 1'),
 ('decision-placement','code_files >= 10'),
 ('contracts','components >= 2'),
 ('server','server_files >= 1'),
 ('database','db_files >= 1'),
 ('client-api','ui_files >= 1 and server_files >= 1'),
 ('abstraction','code_files >= 1'),
 ('hotspots','history_months >= 3'),
 ('ui','ui_files >= 1'),
 ('walkthroughs','page_files >= 1'),
 ('notifications','notification_files >= 1'),
 ('localization','i18n_files >= 1'),
 ('security','code_files >= 1'),
 ('privacy','personal_data_files >= 1'),
 ('logging','code_files >= 1'),
 ('operations','deploy_files >= 1 or ci_files >= 1'),
 ('dependencies','code_files >= 1'),
 ('performance','server_files >= 1 or db_files >= 1 or page_files >= 1'),
 ('cost','paid_service_files >= 1 or deploy_files >= 1'),
 ('docs-developer','code_files >= 1'),
 ('docs-user','user_doc_files >= 1 or page_files >= 1'),
 ('handover','authors >= 2 or deploy_files >= 1 or history_months >= 6'),
 ('features','goals >= 1 or requirements >= 1');


-- The metric names the domains ask for (keep names stable between reviews so they trend).
INSERT OR IGNORE INTO metric_defs(name, label, unit, better) VALUES
 ('mutation_score','Mutation score','%','higher'),
 ('line_coverage','Line coverage','%','higher'),
 ('branch_coverage','Branch coverage','%','higher'),
 ('test_cases_asserted','Test cases asserted','cases','higher'),
 ('test_cases_needed','Test cases needed','cases','none'),
 ('tests_missing_named','Missing tests named','tests','lower'),
 ('flaky_tests','Flaky tests','tests','lower'),
 ('test_suite_seconds','Test suite run time','s','lower'),
 ('page_weight','Page weight','KB','lower'),
 ('queries_per_page','Queries per page','queries','lower'),
 ('p95_job_ms','Job time, 95th percentile','ms','lower'),
 ('dependency_count','Dependencies','packages','lower'),
 ('vulnerable_dependencies','Dependencies with known vulnerabilities','packages','lower');

-- How far two runs of a review agree (`ledger.py compare-runs --record`).
INSERT OR IGNORE INTO metric_defs(name, label, unit, better) VALUES
 ('domain_agreement','Domains both runs chose','%','higher'),
 ('finding_agreement','Issues both runs found','%','higher'),
 ('severity_agreement','Shared issues given the same severity','%','higher');

-- The ledger's words and their labels (see issue_statuses ... enum_labels). Migration 0006 seeds the same rows.
INSERT INTO issue_statuses(value, label, position, active, resolved, fixed, reopens, briefed) VALUES
 ('open','Open',1,1,0,0,0,1),
 ('needs-decision','Needs your decision',2,1,0,0,0,1),
 ('planned','Planned',3,1,0,0,0,1),
 ('in-progress','In progress',4,1,0,0,0,1),
 ('fixed','Fixed',5,0,1,1,0,1),
 ('verified','Fixed and confirmed',6,0,1,1,0,1),
 ('regressed','Came back',7,1,0,0,0,1),
 ('accepted-risk','Accepted risk',8,0,1,0,0,1),
 ('wont-fix','Won''t fix',9,0,1,0,1,1),
 ('duplicate','Duplicate',10,0,1,0,1,0),
 ('obsolete','No longer applies',11,0,1,0,1,0);
INSERT INTO severities(value, label, position) VALUES
 ('H','High',1), ('M','Medium',2), ('L','Low',3), ('T','Missing test',4);
INSERT INTO match_types(value, label, position, came_back) VALUES
 ('new','New',1,0),
 ('still-present','Still present',2,0),
 ('changed','Changed',3,0),
 ('recurrence','Came back',4,1),
 ('regression','Came back after a fix',5,1),
 ('continuation','Continues an earlier issue',6,0);
INSERT INTO relation_types(value, label, inverse_label, position, closes) VALUES
 ('duplicate-of','Duplicate of','Has duplicate',1,1),
 ('continuation-of','Continues','Continued by',2,0),
 ('regression-of','Regression of','Came back as',3,0),
 ('related-to','Related to','Related to',4,0),
 ('caused-by','Caused by','Causes',5,0),
 ('blocks','Blocks','Blocked by',6,0),
 ('supersedes','Supersedes','Superseded by',7,0),
 ('split-from','Split from','Split into',8,0),
 ('merged-into','Merged into','Absorbed',9,1);
INSERT INTO gate_kinds(value, label, position) VALUES
 ('domain-approval','Approve review domains',1),
 ('plan-approval','Approve fix plan',2),
 ('decision','Decision',3),
 ('step-input','Input on a step',4),
 ('confirm-false-finding','Confirm a false finding',5),
 ('accept-risk','Accept a risk',6),
 ('production-action','Production action',7),
 ('review-handoff','Review hand-off',8),
 ('confirm','Confirm',9),
 ('other','Question',10),
 ('feature-pick','Pick ideas to build',11);
INSERT INTO goal_statuses(value, label, position, served) VALUES
 ('proposed','Waiting for your approval',1,0),
 ('approved','Approved',2,1),
 ('rejected','Not approved',3,0),
 ('retired','Retired',4,0);
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('check_tier','error','Must be empty',1), ('check_tier','warning','Worth a look',2),
 ('actor_kind','owner','Owner',1), ('actor_kind','human','Person',2), ('actor_kind','manager','Manager agent',3),
 ('actor_kind','reviewer','Reviewer agent',4), ('actor_kind','verifier','Verifier agent',5),
 ('actor_kind','indexer','Indexer agent',6), ('actor_kind','matcher','Matcher agent',7),
 ('actor_kind','drafter','Plan drafter agent',8), ('actor_kind','fixer','Fixer agent',9), ('actor_kind','tool','Tool',10),
 ('domain_category','correctness','Correctness',1), ('domain_category','architecture','Architecture',2),
 ('domain_category','user-facing','User-facing',3), ('domain_category','security-operations','Security and operations',4),
 ('domain_category','documentation','Documentation',5), ('domain_category','custom','Custom',6),
 ('review_scope','full','Full review',1), ('review_scope','partial','Partial review',2),
 ('review_scope','targeted','Targeted review',3), ('review_scope','second','Second review',4),
 ('review_scope','fix-verification','Fix check',5),
 ('review_status','planning','Planning',1), ('review_status','awaiting-approval','Waiting for your approval',2),
 ('review_status','approved','Approved',3), ('review_status','running','Reviewers running',4),
 ('review_status','verifying','Verifying',5), ('review_status','indexing','Indexing points',6),
 ('review_status','consolidating','Writing it up',7), ('review_status','handed-off','Handed off',8),
 ('review_status','abandoned','Abandoned',9),
 ('domain_decision','included','Included',1), ('domain_decision','skipped','Skipped',2),
 ('owner_change','none','As proposed',1), ('owner_change','added-by-owner','Added by you',2),
 ('owner_change','removed-by-owner','Removed by you',3), ('owner_change','modified-by-owner','Changed by you',4),
 ('run_status','queued','Queued',1), ('run_status','running','Running',2), ('run_status','finished','Finished',3),
 ('run_status','failed','Failed',4), ('run_status','cancelled','Cancelled',5),
 ('requirement_status','active','In force',1), ('requirement_status','superseded','Replaced',2),
 ('requirement_status','withdrawn','Withdrawn',3),
 ('issue_kind','defect','Defect',1), ('issue_kind','test','Missing test',2), ('issue_kind','validation','Validation',3),
 ('issue_kind','refactor','Refactor',4), ('issue_kind','feature','Feature',5), ('issue_kind','design','Design',6),
 ('issue_kind','docs','Documentation',7), ('issue_kind','logging','Logging',8), ('issue_kind','perf','Performance',9),
 ('issue_kind','security','Security',10), ('issue_kind','ops','Operations',11), ('issue_kind','verify','To verify',12),
 ('confidence','certain','Certain',1), ('confidence','likely','Likely',2), ('confidence','possible','Possible',3),
 ('verification','unverified','Not checked yet',1), ('verification','confirmed','Confirmed',2),
 ('verification','partly','Partly confirmed',3), ('verification','false','False finding',4),
 ('verification','not-checkable','Could not be checked',5),
 ('location_role','primary','Main place',1), ('location_role','also','Also here',2), ('location_role','test','Test',3),
 ('location_role','doc','Documentation',4), ('location_role','config','Configuration',5),
 ('excerpt_status','truncated','First 200 lines only',1), ('excerpt_status','not-text','Not text',2),
 ('location_source','commit','Read from the commit',1),
 ('location_source','working-tree','Read from the reviewed working tree',2),
 ('verdict','matches','Matches',1), ('verdict','differs','Differs',2), ('verdict','partly','Partly matches',3),
 ('verdict','missing','Missing',4), ('verdict','untested','Not tested',5),
 ('tested','yes','Tested',1), ('tested','partly','Partly tested',2), ('tested','no','Not tested',3),
 ('check_outcome','still-present','Still present',1), ('check_outcome','changed','Changed',2),
 ('check_outcome','fixed','Fixed',3), ('check_outcome','regressed','Came back',4),
 ('check_outcome','obsolete','No longer applies',5), ('check_outcome','not-checked','Not checked',6),
 ('decision_status','open','Open',1), ('decision_status','decided','Decided',2), ('decision_status','revisit','To revisit',3),
 ('gate_status','open','Waiting on you',1), ('gate_status','answered','Answered, waiting for the agent',2),
 ('gate_status','applied','Done',3), ('gate_status','withdrawn','Withdrawn',4),
 ('answered_via','chat','In chat',1), ('answered_via','viewer','In the viewer',2), ('answered_via','other','Elsewhere',3),
 ('plan_status','drafting','Drafting',1), ('plan_status','awaiting-approval','Waiting for your approval',2),
 ('plan_status','approved','Approved',3), ('plan_status','in-progress','In progress',4),
 ('plan_status','complete','Complete',5), ('plan_status','superseded','Replaced',6), ('plan_status','abandoned','Abandoned',7),
 ('owner_need','none','',1), ('owner_need','decision','Your decision',2), ('owner_need','confirm','Your confirmation',3),
 ('owner_need','review','Your review',4), ('owner_need','production','Production action',5),
 ('step_status','not-started','Not started',1), ('step_status','doing','In progress',2),
 ('step_status','waiting-owner','Waiting on you',3), ('step_status','done','Done',4), ('step_status','dropped','Dropped',5),
 ('test_purpose','baseline','Baseline',1), ('test_purpose','targeted','Targeted',2), ('test_purpose','flake-check','Flakiness check',3),
 ('test_purpose','mutation','Mutation testing',4), ('test_purpose','before-fix','Before the fix',5),
 ('test_purpose','after-fix','After the fix',6), ('test_purpose','ci','Continuous integration',7),
 ('metric_better','higher','Higher is better',1), ('metric_better','lower','Lower is better',2),
 ('metric_better','none','Neither',3),
 ('metric_method','measured','Measured',1), ('metric_method','counted','Counted',2), ('metric_method','estimated','Estimated',3),
 ('artifact_kind','brief','Brief',1), ('artifact_kind','report','Report',2), ('artifact_kind','index','Index',3),
 ('artifact_kind','summary-doc','Summary document',4), ('artifact_kind','plan-file','Plan file',5),
 ('artifact_kind','progress-page','Progress page',6), ('artifact_kind','probe-script','Probe script',7),
 ('artifact_kind','other','Other',8),
 ('entity_type','review','Review',1), ('entity_type','issue','Issue',2), ('entity_type','step','Plan step',3),
 ('entity_type','plan','Plan',4), ('entity_type','decision','Decision',5), ('entity_type','finding','Finding',6),
 ('entity_type','gate','Question for you',7), ('entity_type','relation','Relation',8), ('entity_type','check','Review check',9),
 ('entity_type','step-finding','Step coverage',10),
 ('entity_type','goal','Design goal',11), ('entity_type','question','Interview question',12);

-- The checks `ledger.py check` runs (see ledger_checks).
INSERT OR IGNORE INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unmatched_findings','Findings not matched to an issue','error',10),
 ('v_unchecked_known_issues','Known issues in a domain that ran, with no check recorded','error',20),
 ('v_untraceable_refs','Code references that cannot be traced to exact lines','error',30),
 ('v_uncovered_findings','Findings no step of their plan closes','error',40),
 ('v_coverage_problems','Steps closing nothing, or findings closed twice without a reason','error',50),
 ('v_unguarded_fixes','Fixed issues with no guard test, or whose guard test is gone','warning',100);

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unplanned_discoveries','Defects found during fix work that no plan step closes','error',60);

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_history_gaps','Issues and gates whose history is missing an event','error',70),
 ('v_match_problems','Matches whose type disagrees with the issue','error',72),
 ('v_off_plan_coverage','Steps closing a finding of a review their plan does not cover','error',74),
 ('v_done_steps_open_issues','Done steps whose issues are still to do','error',76),
 ('v_closed_without_reason','Issues accepted or declined with no reason recorded','error',78),
 ('v_relation_problems','Related-to pairs stored in both directions','error',80),
 ('v_check_finding_owner','Review checks citing a finding of another issue','error',82);
-- The names the triggers give to events (events.event is free text: a name without a label is shown in words).
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','opened','Opened',1),
 ('event','seen','Raised by a review',2),
 ('event','recurred','Raised again after it was closed',3),
 ('event','checked','Looked for by a review',4),
 ('event','unlinked','A point was moved to another issue',5),
 ('event','status-changed','Status changed',6),
 ('event','severity-changed','Severity changed',7),
 ('event','title-changed','Title changed',8),
 ('event','before-state-changed','What it did before: text changed',9),
 ('event','current-state-changed','What it does now: text changed',10),
 ('event','after-state-changed','What it does after the fix: text changed',11),
 ('event','recommendation-changed','Agreed fix changed',12),
 ('event','resolution-changed','Resolution changed',13),
 ('event','created','Created',14),
 ('event','proof-changed','Proof changed',15),
 ('event','change-text-changed','Change text changed',16),
 ('event','answer-changed','Answer changed',17),
 ('event','answer-cleared','Answer cleared',18),
 ('event','verification-changed','Check by the manager changed',19),
 ('event','text-changed','Text changed',20),
 ('event','asked','Asked',21),
 ('event','linked','Linked',22),
 ('event','step-linked','Linked to a plan step',23),
 ('event','source-linked','Linked to the interview answers it comes from',24),
 ('event','dependency-added','A step it depends on was added',25),
 ('event','proposed','Proposed',26),
 ('event','reviewer-run-created','A reviewer was started',27),
 ('event','metric-recorded','A measurement was recorded',28),
 ('event','tag-pushed','The tag of the reviewed commit was pushed',29),
 ('event','redacted','A secret was removed from the text',30);
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','answers-imported','Answers brought in from the published page',31);

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_artifacts_outside_ledger','Review files recorded outside the ledger''s folder','warning',110);
-- Each domain's checklist (the table "Checklists" in references/domains.md).
INSERT INTO domain_checks(code, domain_code, position, severity, question, evidence) VALUES
 ('spec.1','spec',1,'H','Is every stated requirement listed, with its source quoted?','A row per requirement: its code or quote, source and location.'),
 ('spec.2','spec',2,'H','Does the implementation of each requirement match it at its boundaries, priority orders and exceptions?','Per requirement: path:line of every copy and a verdict (matches, differs, partly, missing, untested).'),
 ('spec.3','spec',3,'M','Does a test assert each requirement?','The test name per requirement, or untested.'),
 ('spec.4','spec',4,'M','Does the code do anything no requirement asks for, or do requirements contradict each other?','Each such behaviour or contradiction with path:line.'),
 ('tests.1','tests',1,'H','Is every unit of behaviour or configuration listed with the cases it needs?','A row per function, route, command, setting, rule or flag, with its cases and expected outcomes.'),
 ('tests.2','tests',2,'H','Is each needed case asserted by a test that would fail if the behaviour broke?','Per case: the test name and Yes, Partly or No.'),
 ('tests.3','tests',3,'M','Is every input validated, and every validation branch and error path tested?','Inputs read with no validation; branches and error paths with no test.'),
 ('tests.4','tests',4,'M','Are migrations and conversions tested?','Each migration or conversion with its test, or no test.'),
 ('tests.5','tests',5,'L','Do the totals add up?','Units, cases needed, asserted and missing.'),
 ('test-quality.1','test-quality',1,'H','What is the mutation score of the core modules?','The score per module and the surviving mutants that matter, or not measured with the hand sample.'),
 ('test-quality.2','test-quality',2,'M','Are any tests flaky or order-dependent?','The results of repeated and shuffled runs.'),
 ('test-quality.3','test-quality',3,'M','Do tests depend on the real clock, time zone, network or shared state?','Each such test by name.'),
 ('test-quality.4','test-quality',4,'L','How fast is the suite, and which tests are slowest?','The run time and the slowest tests.'),
 ('test-quality.5','test-quality',5,'M','Does every recorded guard test still exist and still guard its issue?','Per guard test: exists, and fails without its fix where tried.'),
 ('time-concurrency.1','time-concurrency',1,'H','Is every place that creates, parses, compares, formats or stores a date or time listed with its time zone handling?','A row per place: path:line, the zone stored and converted, whose today it is.'),
 ('time-concurrency.2','time-concurrency',2,'M','Are daylight-saving changes, month, year and leap boundaries and inclusive or exclusive ends handled?','The concrete dates to test and what happens on each.'),
 ('time-concurrency.3','time-concurrency',3,'H','What happens when two actors change the same thing at once?','Per shared write: the guard (version check, constraint, transaction, lock) or the lost update.'),
 ('time-concurrency.4','time-concurrency',4,'M','Does a test exercise each boundary date and each interleaving?','Test names, or the missing tests by name.'),
 ('resilience.1','resilience',1,'H','Does every call to another service, queue, database or file system have a timeout and a retry that is safe to repeat?','A row per call: timeout, retry policy, idempotency.'),
 ('resilience.2','resilience',2,'H','Can any multi-step change stop halfway and leave inconsistent data?','Per change: the transaction or compensating step, or the inconsistent state.'),
 ('resilience.3','resilience',3,'M','What does each job do on failure, on a rerun, on overlap with itself and on a missed run?','The four answers per job.'),
 ('resilience.4','resilience',4,'M','What do the user and the operator see for each failure?','At least five failures followed end to end.'),
 ('compatibility.1','compatibility',1,'H','Can today''s code read everything older code wrote?','Per stored thing (rows, settings, files, cookies, links): the read path and the oldest version handled.'),
 ('compatibility.2','compatibility',2,'H','Is every schema or settings migration tested from each older version in use?','Per migration: the test and the versions it starts from.'),
 ('compatibility.3','compatibility',3,'M','Can each deploy roll back without losing or corrupting data?','The rollback path per kind of change.'),
 ('compatibility.4','compatibility',4,'M','Are API changes versioned or additive, and do old clients keep working across a deploy?','Each breaking change waiting to happen and the test that would catch it.'),
 ('decision-placement.1','decision-placement',1,'H','Is every business decision listed with the layer that decides it today?','A row per decision: where it is decided, path:line.'),
 ('decision-placement.2','decision-placement',2,'H','Is any decision written more than once, and do the copies agree?','Every copy with path:line and the result of probing them.'),
 ('decision-placement.3','decision-placement',3,'M','Is any decision checked in one path and not in another?','Each path that skips the check.'),
 ('decision-placement.4','decision-placement',4,'M','Which decisions should move, and in what order?','Move or keep with the reason, and the tests that must pass before and after.'),
 ('contracts.1','contracts',1,'H','Is every endpoint or message between components listed with its request, response and errors?','An inventory per boundary.'),
 ('contracts.2','contracts',2,'H','Does each caller handle every response the callee can give?','Per call: each success shape, refusal, error and timeout, and the ones not handled.'),
 ('contracts.3','contracts',3,'M','Do the producer''s contract and the consumer''s copy agree field by field?','Each drifted or duplicated type.'),
 ('contracts.4','contracts',4,'M','Is every endpoint and response branch tested, failures included?','Tests per branch: timeout, 4xx, 5xx, malformed, unavailable.'),
 ('server.1','server',1,'M','Where does the server hardcode domain or customer-specific behaviour?','Each place with path:line, whether a setting exists, and the fix.'),
 ('server.2','server',2,'H','Is every route, job, consumer, webhook and sign-in flow listed with who may call it?','The inventory with the permission per entry.'),
 ('server.3','server',3,'H','Does each have tests for success, each refusal, validation, stale saves and a failing dependency?','Cases against tests; the missing tests by name.'),
 ('server.4','server',4,'H','Is any client input unvalidated, or any permission check without a negative test?','Each with path:line.'),
 ('database.1','database',1,'M','Is everything the system consumes or produces stored and queryable later?','Anything lost, lossy or only in logs.'),
 ('database.2','database',2,'M','Does every distinct query use an index, bound its results and avoid a query per item?','The query plan per query: scans, repeated queries, unbounded results.'),
 ('database.3','database',3,'H','Do constraints, uniqueness and transactions keep the data consistent under concurrent writes?','Each missing constraint or unguarded write.'),
 ('database.4','database',4,'M','Are migrations reversible, retention defined, and no secret stored in clear?','Per migration and per sensitive column.'),
 ('client-api.1','client-api',1,'M','Does every backend capability have an API and a screen that uses it?','The capability inventory with the gaps both ways.'),
 ('client-api.2','client-api',2,'M','Are the API styles coherent: no duplicated capability, one shape for results and errors?','Each inconsistency.'),
 ('client-api.3','client-api',3,'H','Do two paths to one action apply the same permission check?','Each pair of paths with its checks.'),
 ('client-api.4','client-api',4,'L','Is any identifier a display name?','Each one.'),
 ('abstraction.1','abstraction',1,'M','Which types, contracts or logic are duplicated between packages or layers?','Each duplicate with every location.'),
 ('abstraction.2','abstraction',2,'M','How many files change to add a new entity, rule, setting group or notification type?','The file list per walk-through.'),
 ('abstraction.3','abstraction',3,'L','Is there dead code, an oversized module or a circular dependency?','Each with its path.'),
 ('abstraction.4','abstraction',4,'L','What is the refactoring order that keeps the system working?','Quick wins, medium, structural.'),
 ('hotspots.1','hotspots',1,'M','Which files change most often and are most complex?','The ranked list from git log.'),
 ('hotspots.2','hotspots',2,'M','Which files always change together but live apart?','Each cluster.'),
 ('hotspots.3','hotspots',3,'M','Which code has a single author, and which busy areas have few tests?','Lists with counts.'),
 ('hotspots.4','hotspots',4,'L','Which files keep producing issues across reviews?','The rows of the hotspot view.'),
 ('ui.1','ui',1,'H','Does each page meet the design brief (type scale, palette, density)?','Per page: the goal broken and the screenshot, or rendered result not seen.'),
 ('ui.2','ui',2,'H','Does every text meet the required contrast, and every image, field, button and link have a name?','The audit''s output per page, or the same computed from the stylesheets and marked not measured.'),
 ('ui.3','ui',3,'M','How many colours, font sizes and spacing values are in use against the brief''s scale?','The inventory with counts and the values to merge.'),
 ('ui.4','ui',4,'M','Does each page work at phone width and in both themes?','Screenshots at 390 and 1280 pixels, light and dark, with what breaks.'),
 ('ui.5','ui',5,'M','Are there internal names on screen, typing where a choice would do, or text below the writing standard?','Each with file:line, the text and the shorter version.'),
 ('ui.6','ui',6,'H','Is any link shown to someone who is then refused, or any page shown without access?','The access matrix: kind of user by link, button and page.'),
 ('ui.7','ui',7,'M','Can every configuration the backend allows be displayed and changed?','Each setting with no screen.'),
 ('walkthroughs.1','walkthroughs',1,'H','Can each kind of user complete each of its main tasks?','10 to 20 tasks per kind of user, traced page by page.'),
 ('walkthroughs.2','walkthroughs',2,'M','Where does a hand-off between pages break or confuse?','Each dead end, missing confirmation or undo, and step that needs information the user cannot see.'),
 ('walkthroughs.3','walkthroughs',3,'M','Can a first-time user finish the main task unaided, and is what they see first what the task needs?','Per main task: the first screen and where they would stop.'),
 ('walkthroughs.4','walkthroughs',4,'L','What on each page could be deleted without loss?','A list per page.'),
 ('notifications.1','notifications',1,'M','Is every message type listed with its trigger, recipients, content source and opt-out?','The inventory.'),
 ('notifications.2','notifications',2,'H','Can a message be sent twice, missed, or sent to the wrong person after a role change?','Per message type: the guard or the failure.'),
 ('notifications.3','notifications',3,'M','Are bounces handled, and is staging kept from mailing real people?','Configuration and code, or not verified.'),
 ('notifications.4','notifications',4,'L','Does each message render on a phone and have a plain-text alternative?','Per message type.'),
 ('localization.1','localization',1,'M','Which user-facing strings are hardcoded?','Each with file:line.'),
 ('localization.2','localization',2,'M','Are dates, numbers, currency and week start formatted for the reader''s locale?','Each fixed format.'),
 ('localization.3','localization',3,'L','Will translated text fit, and is right-to-left possible?','Each layout that will not.'),
 ('security.1','security',1,'H','For every route and API function, what is the permission check, and can a lower-privileged, other-tenant or signed-out caller reach it?','A row per route: the check with file:line and the result of changing an id.'),
 ('security.2','security',2,'H','Where does client input reach SQL, HTML, e-mail, file paths, redirects or deserialization unescaped?','Each sink with file:line.'),
 ('security.3','security',3,'H','How are sign-in codes, tokens and sessions stored, expired and limited?','Per method: hashing, expiry, attempt limits, cookie flags.'),
 ('security.4','security',4,'H','Is any secret in the repository, a workflow or a log, or stored in clear?','Locations only, never the value.'),
 ('security.5','security',5,'M','Which browser protections are set?','Per page or response: content security policy, cross-site request checks, cross-origin rules, framing, caching of private responses.'),
 ('security.6','security',6,'M','Who can deploy, and what could each kind of misuser do today?','Workflow permissions and a short threat model.'),
 ('security.7','security',7,'M','Which protections have a test that fails without them?','Test names and the missing tests.'),
 ('privacy.1','privacy',1,'H','Where does each kind of personal data enter, and where is it stored or copied?','A data map: database, backups, logs, e-mail, analytics, third parties.'),
 ('privacy.2','privacy',2,'H','Who can read it, and how long is it kept?','Per kind of data.'),
 ('privacy.3','privacy',3,'M','How would one person''s data be exported or deleted in full?','The path, or what would be left behind.'),
 ('privacy.4','privacy',4,'M','Which obligations apply, and where are the gaps?','Each gap, marked not legal advice.'),
 ('logging.1','logging',1,'M','Is every important event logged?','The events that leave no trace: sign-ins, refusals, state changes, external calls, jobs, e-mail, configuration changes.'),
 ('logging.2','logging',2,'M','Can an operator answer five realistic complaints from the logs alone?','Each complaint walked, and where the trail breaks.'),
 ('logging.3','logging',3,'H','Do logs hold secrets, tokens, codes or personal data, or accept injected lines?','Each statement with file:line.'),
 ('logging.4','logging',4,'L','Are structure, levels, a request id and zoned timestamps consistent, with no swallowed exception?','Each exception to it.'),
 ('logging.5','logging',5,'M','Do errors reach a person, and where do logs go and for how long?','Alerting, retention and access.'),
 ('operations.1','operations',1,'M','Does CI run every suite, linter and type-check on every change and block merges on failure?','The workflow files.'),
 ('operations.2','operations',2,'M','Are builds reproducible, and are the deploy and rollback steps written down?','The steps, or what is missing.'),
 ('operations.3','operations',3,'H','If the database, hosting account, domain or e-mail provider were lost today, what is the path back?','Per loss: the path, the time, and whether a restore was ever tested.'),
 ('operations.4','operations',4,'M','What is backed up, how often, where, and is it encrypted?','Per store.'),
 ('operations.5','operations',5,'M','Are scheduled jobs monitored for failure and missed runs?','Per job.'),
 ('dependencies.1','dependencies',1,'M','Which packages are outdated, abandoned or have known problems?','From the lock files; not checked online without network.'),
 ('dependencies.2','dependencies',2,'M','Is every version pinned, and every CI action referenced by commit hash?','Each one that is not.'),
 ('dependencies.3','dependencies',3,'L','Is there a licence problem, an install script or a dependency used for something trivial?','Each one.'),
 ('performance.1','performance',1,'M','Where does the time go for each page load and action?','Server calls, queries per call and repeated work; measured or reasoned, saying which.'),
 ('performance.2','performance',2,'M','What do the heavy components cost in time and memory at realistic and larger sizes?','Numbers per job type.'),
 ('performance.3','performance',3,'M','What is each page''s weight and layout shift?','The audit''s numbers, or not measured.'),
 ('performance.4','performance',4,'M','What breaks first at 10x users and 3x data?','The first limit and the three changes that buy the most.'),
 ('performance.5','performance',5,'L','Which performance tests or budgets exist?','The existing ones and the ones to add.'),
 ('cost.1','cost',1,'M','What does each service cost now and at 10x scale?','From configuration and pricing in the repository; not verified otherwise.'),
 ('cost.2','cost',2,'M','What could run up a bill by accident?','Runaway retries, unbounded logs or storage, no spending limit.'),
 ('cost.3','cost',3,'L','Which account and payment method is each service billed to?','Per service.'),
 ('docs-developer.1','docs-developer',1,'M','Does every public function, type and module say what it does, its inputs and outputs, and why?','Per module: documented, partly, undocumented.'),
 ('docs-developer.2','docs-developer',2,'M','Which comments or docs are stale against the current code?','Each statement with file:line and what the code does.'),
 ('docs-developer.3','docs-developer',3,'M','Do the README, setup, architecture and settings docs exist, and do their commands run?','Per document: exists, accurate, what is missing; commands run where safe.'),
 ('docs-developer.4','docs-developer',4,'L','Does every piece of text meet the writing standard?','Each breach with the shorter version.'),
 ('docs-user.1','docs-user',1,'M','Which tasks, settings and errors can a user meet with no explanation anywhere?','Each one.'),
 ('docs-user.2','docs-user',2,'M','Is each piece of help accurate to current behaviour and findable where it is needed?','The inventory with a verdict per piece.'),
 ('docs-user.3','docs-user',3,'L','Is it written for the reader, to the writing standard?','Each breach with the shorter version.'),
 ('handover.1','handover',1,'H','Could a new maintainer clone, set up, run, test, deploy and roll back from the repository and its docs alone?','Each step walked, and where they would get stuck.'),
 ('handover.2','handover',2,'H','Who owns each account, domain, key and secret, and is any tied to one person?','The list with the owner and how access transfers.'),
 ('handover.3','handover',3,'M','Are there runbooks for the common incidents and the periodic chores?','Each one, present or missing.'),
 ('handover.4','handover',4,'M','Where is knowledge held by one person?','From the history or the hotspots report.');
INSERT INTO domain_checks(code, domain_code, position, severity, question, evidence) VALUES
 ('features.1','features',1,'L','Is every approved goal and active requirement listed with what the code does toward it today?','A row per goal and requirement: path:line of what serves it, or absent.'),
 ('features.2','features',2,'L','Does each proposal name the goal or requirement it serves, its value, effort, risks and dependencies?','Each proposal with its goal code and the four answers.'),
 ('features.3','features',3,'L','Does each proposal have a test that would show it is built?','The done-test per proposal: its name and what it asserts.'),
 ('features.4','features',4,'L','Were the proposals already on record checked again against the goals and the code?','Per earlier proposal: still wanted, built, or serving no current goal.');

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('checklist_answer','yes','Yes',1), ('checklist_answer','no','No',2), ('checklist_answer','partly','Partly',3),
 ('checklist_answer','not-applicable','Does not apply',4);
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('touch_kind','file','File',1), ('touch_kind','symbol','Function or name',2), ('touch_kind','table','Table',3),
 ('touch_kind','column','Column',4), ('touch_kind','setting','Setting',5), ('touch_kind','route','Route',6),
 ('touch_kind','behaviour','What a user sees',7),
 ('touch_direction','adds','Adds',1), ('touch_direction','removes','Removes',2),
 ('touch_direction','tightens','Tightens',3), ('touch_direction','loosens','Loosens',4),
 ('touch_direction','renames','Renames',5), ('touch_direction','changes','Changes',6),
 ('touch_source','declared','Declared by the step',1), ('touch_source','finding','Where its points are',2),
 ('conflict_resolution','none','Not judged yet',1), ('conflict_resolution','no-conflict','No conflict',2),
 ('conflict_resolution','order','Done in order',3), ('conflict_resolution','merge','Merged into one change',4),
 ('conflict_resolution','owner','Decided by you',5);
INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unresolved_conflicts','Plan steps that touch the same thing, not judged yet','error',120),
 ('v_late_step_conflicts','Steps added after approval that touch what other steps touch','warning',130);
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('feature_status','proposed','Proposed',1), ('feature_status','picked','Picked',2),
 ('feature_status','declined','Declined',3), ('feature_status','built','Built',4),
 ('feature_value','high','High',1), ('feature_value','medium','Medium',2), ('feature_value','low','Low',3),
 ('feature_effort','small','Small',1), ('feature_effort','medium','Medium',2), ('feature_effort','large','Large',3);
INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unaddressed_tool_results','Tool results no finding cites and no reviewer dismissed','error',84);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('tool_run_status','ran','Ran',1), ('tool_run_status','imported','Imported from a file',2),
 ('tool_run_status','absent','Not installed',3), ('tool_run_status','skipped','Not run',4),
 ('tool_run_status','failed','Failed',5),
 ('tool_output_format','sarif','SARIF',1), ('tool_output_format','junit','JUnit XML',2),
 ('tool_output_format','lcov','LCOV',3), ('tool_output_format','cobertura','Cobertura XML',4),
 ('tool_output_format','json','JSON',5), ('tool_output_format','jsonl','JSON lines',6),
 ('tool_result_level','error','Error',1), ('tool_result_level','warning','Warning',2),
 ('tool_result_level','note','Note',3), ('tool_result_level','none','No level',4);
INSERT INTO meta(key, value) VALUES ('schema_version','21');
INSERT INTO schema_migrations(version, description) VALUES (1, 'initial ledger schema'),
  (2, 'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted'),
  (3, 'integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers'),
  (4, 'check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes'),
  (5, 'one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries'),
  (6, 'lookup tables for the ledger''s words and labels; RESTRICT keys under reviews and findings; metrics.name catalogued'),
  (7, 'must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason'),
  (8, 'finding_locations.source (the reviewed text of a dirty working tree); v_next_codes for the write commands'),
  (9, 'owner interview (interview_questions, interview_answers) and per-domain design goals the owner approves'),
  (10, 'v_plan_totals, v_review_totals and v_pending_gates: the counts the viewer''s lists show'),
  (11, 'gate answer rules in the trigger, owner queue context, event labels, the artifact check'),
  (12, 'review_environment: what each review''s environment offered (tools, the harness, its connectors)'),
  (13, 'domain_rules: the rule by which scan proposes each domain; review_domains.proposed_by_rule'),
  (14, 'domain_checks and checklist_answers: each domain''s checklist, and the answers each report gave'),
  (15, 'the agreement metrics compare-runs records: domain_agreement, finding_agreement, severity_agreement'),
  (16, 'step_touches and step_conflicts: what each plan step touches, and the pairs of steps that touch the same thing'),
  (17, 'a step may depend on any step of its plan, cycles refused; v_step_order: the order to work in'),
  (18, 'multi-select gates: gates.multi and gates.preselect; a multi-select answer is a JSON list of option values'),
  (19, 'the features domain and feature_proposals: additions to the reviewed codebase that would serve the owner''s goals'),
  (20, 'tool_runs and tool_results: the picked tools'' runs at the reviewed commit, and their results as leads'),
  (21, 'indexes for check''s views, which read them instead of every row; the answers-imported event');
