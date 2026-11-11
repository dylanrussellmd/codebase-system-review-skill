-- =====================================================================
-- Code review ledger — schema version 5
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

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,                -- schema_version, project_name, created_at, created_by_skill
  value TEXT NOT NULL
);

CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The checks `ledger.py check` and the viewer's home page run, in position order: each view must be empty (tier
-- 'error': check exits 1 when it is not) or should be (tier 'warning': listed, check still exits 0). Seeded at the
-- end of this file; a schema that adds a check inserts its row in its migration.
CREATE TABLE ledger_checks (
  view     TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  tier     TEXT NOT NULL CHECK (tier IN ('error','warning')),
  position INTEGER NOT NULL UNIQUE
);

-- ---------- Who and what ----------------------------------------------

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

-- People and agents. One row per agent instance (each subagent run gets
-- its own row), so "who said this" is always answerable.
CREATE TABLE actors (
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('owner','human','manager','reviewer','verifier','indexer',
                     'matcher','drafter','fixer','tool')),
  name            TEXT NOT NULL,         -- person's name, or the agent's role label ("security reviewer")
  model           TEXT,                  -- model id for agents
  harness         TEXT,                  -- e.g. Claude Code, CI
  session_ref     TEXT,                  -- session id or URL
  parent_actor_id INTEGER REFERENCES actors(id),   -- subagent -> the agent that launched it
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Catalogs ----------------------------------------------------

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

CREATE TABLE themes (
  code       TEXT PRIMARY KEY,           -- 'authorization', 'logging-audit' ...
  name       TEXT NOT NULL,
  retired_at TEXT
);

-- ---------- Standards the code is judged against ----------------------

CREATE TABLE design_goals (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE     -- G-01
                  CHECK (code GLOB 'G-[0-9][0-9]*' AND code NOT GLOB 'G-*[^0-9]*'),
  text          TEXT NOT NULL,
  source        TEXT,                    -- where it came from (doc path, "owner, 2026-11-10")
  adopted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  retired_at    TEXT,
  superseded_by INTEGER REFERENCES design_goals(id),
  CHECK (superseded_by IS NULL OR retired_at IS NOT NULL)   -- a superseded goal is retired
);

-- ---------- Reviews -----------------------------------------------------

-- ON DELETE CASCADE keys (review_repositories, review_goals, review_domains, reviewer_runs, findings, metrics and
-- the other children of reviews; finding_locations, finding_goals, finding_requirements of findings; phases,
-- steps, plan_reviews of plans; step_dependencies, step_findings, step_commits of steps; decision_issues of
-- decisions) fire only when their parent row is deleted. History is append-only: reviews, findings, steps and
-- decisions refuse DELETE (tr_*_no_delete), and a review that others still reference is refused by the plain
-- foreign keys too (issues.first_seen_review_id, plan_reviews ...). So the cascades reach only parents nothing
-- protects (a plan with no steps, for instance). Abandon a review (status 'abandoned') instead of deleting it.

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

-- The exact code state each review looked at.
CREATE TABLE review_repositories (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
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

CREATE TABLE review_goals (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);

-- Which domains were proposed, approved, skipped, and why.
CREATE TABLE review_domains (
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  domain_code       TEXT NOT NULL REFERENCES domains(code),
  decision          TEXT NOT NULL CHECK (decision IN ('included','skipped')),
  reason            TEXT NOT NULL,
  owner_change      TEXT NOT NULL DEFAULT 'none' CHECK (owner_change IN
                      ('none','added-by-owner','removed-by-owner','modified-by-owner')),
  reviewers_planned INTEGER,
  split_note        TEXT,                 -- how the domain was split between reviewers
  PRIMARY KEY (review_id, domain_code)
);

-- One row per reviewer (subagent) run.
CREATE TABLE reviewer_runs (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  domain_code    TEXT NOT NULL REFERENCES domains(code),
  actor_id       INTEGER NOT NULL REFERENCES actors(id),
  label          TEXT NOT NULL,           -- 'tests-engine-A'
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

-- Every other agent that worked on a review (verifiers, indexers, matchers, drafters, fixers ...): reviewers are
-- tied to it by reviewer_runs and the manager by reviews.manager_actor_id. v_review_agents lists all of them.
CREATE TABLE review_actors (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);

-- ---------- Requirements (for spec conformance over time) -------------

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
  severity               TEXT NOT NULL CHECK (severity IN ('H','M','L','T')),  -- current
  status                 TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                           ('open','needs-decision','planned','in-progress','fixed','verified',
                            'regressed','accepted-risk','wont-fix','duplicate','obsolete')),
  fingerprint            TEXT,                   -- domain:theme:sub[:symbol], for matching
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

-- How issues relate across time and across reviews.
CREATE TABLE issue_relations (
  id                       INTEGER PRIMARY KEY,
  from_issue_id            INTEGER NOT NULL REFERENCES issues(id),
  to_issue_id              INTEGER NOT NULL REFERENCES issues(id),
  relation                 TEXT NOT NULL CHECK (relation IN
                             ('duplicate-of','continuation-of','regression-of','related-to',
                              'caused-by','blocks','supersedes','split-from','merged-into')),
  confidence               TEXT NOT NULL DEFAULT 'likely' CHECK (confidence IN ('certain','likely','possible')),
  rationale                TEXT NOT NULL,
  established_in_review_id INTEGER REFERENCES reviews(id),
  established_by_actor_id  INTEGER REFERENCES actors(id),
  created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (from_issue_id <> to_issue_id),
  UNIQUE (from_issue_id, to_issue_id, relation)
);

-- ---------- Findings: every point from every review ---------------------

CREATE TABLE findings (
  id                   INTEGER PRIMARY KEY,
  review_id            INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  reviewer_run_id      INTEGER REFERENCES reviewer_runs(id),
  point_id             TEXT NOT NULL,       -- index id: prefix + the report's own id (SEC-H3)
  issue_id             INTEGER REFERENCES issues(id),   -- set during matching, by an UPDATE (never on INSERT)
  match_type           TEXT CHECK (match_type IN
                         ('new','still-present','changed','recurrence','regression','continuation'))
                         CHECK (match_type IS NULL OR issue_id IS NOT NULL),   -- a match type needs the issue
  match_confidence     TEXT CHECK (match_confidence IN ('certain','likely','possible')),
  match_rationale      TEXT,
  severity             TEXT NOT NULL CHECK (severity IN ('H','M','L','T')),
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

-- Where in the code a finding points, pinned to the exact commit reviewed.
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
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
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
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);

CREATE TABLE finding_goals (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);

CREATE TABLE finding_requirements (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);

-- The spec reviewer's verdict on every requirement a review traced, findings or not (a 'matches' or 'untested'
-- row has no finding). One row per review and requirement; v_requirement_trace lists them with counts.
CREATE TABLE review_requirement_checks (
  id                INTEGER PRIMARY KEY,
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  requirement_id    INTEGER NOT NULL REFERENCES requirements(id),
  verdict           TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  where_implemented TEXT,                  -- file, function or page that implements it
  tested            TEXT CHECK (tested IS NULL OR tested IN ('yes','partly','no')),
  finding_id        INTEGER REFERENCES findings(id),   -- the finding raised for it, if any
  evidence          TEXT,
  UNIQUE (review_id, requirement_id)
);

-- What each review concluded about each issue already known when it started.
-- Absence of a finding is NOT evidence of a fix; only a row here is.
CREATE TABLE review_issue_checks (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
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

CREATE TABLE decision_issues (
  decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  issue_id    INTEGER NOT NULL REFERENCES issues(id),
  PRIMARY KEY (decision_id, issue_id)
);

-- Gates: anything waiting on the owner. The agent opens a gate and does not
-- pass it until it is answered (in chat or in the viewer). When the agent
-- acts on the answer it sets status = 'applied'. Decisions are the durable
-- rulings; a gate of kind 'decision' records its answer in decisions too.
CREATE TABLE gates (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE      -- GATE-0001
                    CHECK (code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'GATE-*[^0-9]*'),
  kind            TEXT NOT NULL CHECK (kind IN
                    ('domain-approval','plan-approval','decision','step-input',
                     'confirm-false-finding','accept-risk','production-action',
                     'review-handoff','confirm','other')),
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
  answer_value    TEXT,                     -- the option chosen
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
  change_actor_id INTEGER REFERENCES actors(id)        -- who made the next status change (applied, withdrawn);
                                            -- copied into its event, then cleared
);

-- ---------- Fix plans -----------------------------------------------------

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

CREATE TABLE plan_reviews (
  plan_id   INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  PRIMARY KEY (plan_id, review_id)
);

CREATE TABLE phases (
  id      INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  number  INTEGER NOT NULL,
  title   TEXT NOT NULL,
  goal    TEXT NOT NULL,                     -- one sentence
  why     TEXT,                              -- why it sits where it does
  UNIQUE (plan_id, number)
);

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
  UNIQUE (plan_id, number)
);

CREATE TABLE step_dependencies (
  step_id            INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  depends_on_step_id INTEGER NOT NULL REFERENCES steps(id),
  PRIMARY KEY (step_id, depends_on_step_id),
  CHECK (step_id <> depends_on_step_id)
);

-- The coverage link: which step closes which finding.
CREATE TABLE step_findings (
  step_id      INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  finding_id   INTEGER NOT NULL REFERENCES findings(id),
  multi_reason TEXT,          -- required when a finding is closed by more than one step
  PRIMARY KEY (step_id, finding_id)
);

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

-- Tests that guard a fixed issue. Future reviews check they still exist.
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

-- ---------- Measurements over time ------------------------------------

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

-- The catalog of metric names: what the viewer shows for each (label, default unit) and which way is better.
-- Seeded with the names the domains ask for; a metric recorded under a new name gets a row here from
-- tr_metric_defined (label = the name with spaces, capitalized), which can then be given a better label.
CREATE TABLE metric_defs (
  name   TEXT PRIMARY KEY,                 -- the stable key used in metrics.name
  label  TEXT NOT NULL,
  unit   TEXT,
  better TEXT NOT NULL DEFAULT 'none' CHECK (better IN ('higher','lower','none'))
);

-- Numbers worth trending: mutation score, coverage %, cases asserted,
-- page weight, queries per page, p95 job time, dependency count ...
CREATE TABLE metrics (
  id            INTEGER PRIMARY KEY,
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  domain_code   TEXT REFERENCES domains(code),
  repository_id INTEGER REFERENCES repositories(id),
  name          TEXT NOT NULL,             -- stable key, e.g. 'mutation_score'
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

CREATE TRIGGER tr_metric_renamed AFTER UPDATE OF name ON metrics BEGIN
  INSERT OR IGNORE INTO metric_defs(name, label, unit)
    VALUES (NEW.name, upper(substr(NEW.name, 1, 1)) || replace(substr(NEW.name, 2), '_', ' '), NEW.unit);
END;

-- ---------- Files and links produced ---------------------------------

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

-- Append-only (G-04): triggers refuse every UPDATE and DELETE on events (tr_events_no_update/no_delete).
CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('review','issue','step','plan','decision','finding','gate',
                                                   'relation','check','step-finding')),
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
CREATE INDEX ix_step_findings_find   ON step_findings(finding_id);
CREATE INDEX ix_deps_on              ON step_dependencies(depends_on_step_id);
CREATE INDEX ix_guard_issue          ON guard_tests(issue_id);
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);
CREATE INDEX ix_metrics_name         ON metrics(name, scope);
CREATE INDEX ix_runs_review          ON reviewer_runs(review_id);
CREATE INDEX ix_gates_status         ON gates(status);
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
CREATE INDEX ix_fk_reviews_approved_by_actor_id      ON reviews(approved_by_actor_id);
CREATE INDEX ix_fk_reviews_manager_actor_id          ON reviews(manager_actor_id);
CREATE INDEX ix_fk_reviews_previous_review_id        ON reviews(previous_review_id);
CREATE INDEX ix_fk_reviews_requested_by_actor_id     ON reviews(requested_by_actor_id);
CREATE INDEX ix_fk_review_repositories_repository_id ON review_repositories(repository_id);
CREATE INDEX ix_fk_review_goals_goal_id              ON review_goals(goal_id);
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

CREATE TRIGGER tr_issue_code_fixed BEFORE UPDATE OF code ON issues WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

CREATE TRIGGER tr_issue_domain_active BEFORE INSERT ON issues
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

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
    resolved_at = CASE WHEN NEW.status IN ('fixed','accepted-risk','wont-fix','duplicate','obsolete')
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       WHEN NEW.status = 'verified' THEN COALESCE(resolved_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                       ELSE NULL END,
    verified_at = CASE WHEN NEW.status = 'verified' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
    after_state = CASE WHEN NEW.status = 'regressed' THEN NULL ELSE after_state END
  WHERE id = NEW.id;
END;

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

CREATE TRIGGER tr_issue_before_changed AFTER UPDATE OF before_state ON issues
WHEN OLD.before_state IS NOT NEW.before_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'before-state-changed', OLD.before_state, NEW.before_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

CREATE TRIGGER tr_issue_current_changed AFTER UPDATE OF current_state ON issues
WHEN OLD.current_state IS NOT NEW.current_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'current-state-changed', OLD.current_state, NEW.current_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

CREATE TRIGGER tr_issue_after_changed AFTER UPDATE OF after_state ON issues
WHEN OLD.after_state IS NOT NEW.after_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'after-state-changed', OLD.after_state, NEW.after_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

CREATE TRIGGER tr_issue_recommendation_changed AFTER UPDATE OF recommendation ON issues
WHEN OLD.recommendation IS NOT NEW.recommendation BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'recommendation-changed', OLD.recommendation, NEW.recommendation, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

CREATE TRIGGER tr_issue_resolution_changed AFTER UPDATE OF resolution ON issues
WHEN OLD.resolution IS NOT NEW.resolution BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'resolution-changed', OLD.resolution, NEW.resolution, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;

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

CREATE TRIGGER tr_issue_context_cleared_ins AFTER INSERT ON issues
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_review_id IS NOT NULL OR NEW.change_step_id IS NOT NULL
  OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE issues SET change_actor_id = NULL, change_review_id = NULL, change_step_id = NULL,
                    change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

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

CREATE TRIGGER tr_finding_run_same_review BEFORE INSERT ON findings
WHEN NEW.reviewer_run_id IS NOT NULL
 AND (SELECT review_id FROM reviewer_runs WHERE id = NEW.reviewer_run_id) IS NOT NEW.review_id BEGIN
  SELECT RAISE(ABORT, 'the reviewer run belongs to another review');
END;

CREATE TRIGGER tr_finding_domain_active BEFORE INSERT ON findings
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

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

CREATE TRIGGER tr_finding_no_delete BEFORE DELETE ON findings BEGIN
  SELECT RAISE(ABORT, 'history is append-only: findings cannot be deleted; mark a wrong one verification = false');
END;

-- match_type 'regression' is for an issue that was fixed or verified (or already regressed in this review);
-- a closed issue raised again is a 'recurrence'.
CREATE TRIGGER tr_finding_regression_guard BEFORE UPDATE OF issue_id, match_type ON findings
WHEN NEW.match_type = 'regression' AND NEW.issue_id IS NOT NULL
 AND (SELECT status FROM issues WHERE id = NEW.issue_id) NOT IN ('fixed','verified','regressed') BEGIN
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
     AND NEW.verification <> 'false' AND NEW.match_type IN ('recurrence','regression');
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
      WHEN NEW.match_type = 'regression' AND status IN ('fixed','verified') THEN 'regressed'
      WHEN NEW.match_type IN ('recurrence','regression') AND status IN ('obsolete','wont-fix','duplicate') THEN 'open'
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

CREATE TRIGGER tr_location_no_delete BEFORE DELETE ON finding_locations BEGIN
  SELECT RAISE(ABORT, 'history is append-only: finding locations cannot be deleted');
END;

-- ----- Relations

-- No relation stored both ways: directional ones and related-to (which is symmetric).
CREATE TRIGGER tr_relation_noreverse_dup BEFORE INSERT ON issue_relations
WHEN NEW.relation IN ('duplicate-of','continuation-of','regression-of','supersedes','merged-into','split-from',
                      'caused-by','related-to')
BEGIN
  SELECT RAISE(ABORT, 'the reverse relation already exists')
  WHERE EXISTS (SELECT 1 FROM issue_relations
                WHERE from_issue_id = NEW.to_issue_id AND to_issue_id = NEW.from_issue_id
                  AND relation = NEW.relation);
END;

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
                      OR (NEW.relation <> 'blocks' AND r.relation IN ('duplicate-of','merged-into')))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;

CREATE TRIGGER tr_relation_no_cycle_upd BEFORE UPDATE OF from_issue_id, to_issue_id, relation ON issue_relations
WHEN NEW.relation IN ('blocks','duplicate-of','merged-into') BEGIN
  SELECT RAISE(ABORT, 'that relation would make a cycle')
  WHERE EXISTS (WITH RECURSIVE reach(n) AS (
                  SELECT NEW.to_issue_id
                  UNION SELECT r.to_issue_id FROM issue_relations r JOIN reach ON r.from_issue_id = reach.n
                   WHERE r.id <> NEW.id AND (r.relation = NEW.relation
                      OR (NEW.relation <> 'blocks' AND r.relation IN ('duplicate-of','merged-into'))))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;

CREATE TRIGGER tr_relation_created AFTER INSERT ON issue_relations BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('relation', NEW.id, 'created', (SELECT code FROM issues WHERE id = NEW.from_issue_id),
          NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id),
          NEW.established_by_actor_id, NEW.established_in_review_id, NEW.rationale);
END;

-- Recording that an active issue is a duplicate of (or merged into) another closes it as duplicate.
CREATE TRIGGER tr_relation_closes_duplicate AFTER INSERT ON issue_relations
WHEN NEW.relation IN ('duplicate-of','merged-into') BEGIN
  UPDATE issues SET
    status = 'duplicate',
    resolution = COALESCE(resolution, NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id)),
    change_actor_id = NEW.established_by_actor_id,
    change_review_id = NEW.established_in_review_id,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id) || ': ' || NEW.rationale
  WHERE id = NEW.from_issue_id AND status IN ('open','needs-decision','planned','in-progress','regressed');
END;

CREATE TRIGGER tr_relation_no_delete BEFORE DELETE ON issue_relations BEGIN
  SELECT RAISE(ABORT, 'history is append-only: relations cannot be deleted');
END;

-- ----- Review checks of known issues

-- One check per review and issue, never replaced (INSERT OR REPLACE is refused too); not by the review that
-- first found the issue; its finding must be from the same review; and 'fixed' cannot be recorded by a review
-- that also raised the issue again.
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

CREATE TRIGGER tr_check_no_update BEFORE UPDATE ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'history is append-only: review checks cannot be changed');
END;

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
      WHEN NEW.outcome = 'fixed' AND status IN ('open','needs-decision','planned','in-progress','regressed') THEN 'fixed'
      WHEN NEW.outcome IN ('regressed','still-present','changed') AND status IN ('fixed','verified') THEN 'regressed'
      WHEN NEW.outcome = 'obsolete' AND status <> 'duplicate' THEN 'obsolete'
      ELSE status END,
    after_state = CASE WHEN NEW.outcome = 'fixed' AND status IN ('open','needs-decision','planned','in-progress','regressed')
                       THEN COALESCE(after_state, NEW.evidence,
                                     'Found fixed by review ' || (SELECT code FROM reviews WHERE id = NEW.review_id) || '.')
                       ELSE after_state END,
    resolution = CASE
      WHEN NEW.outcome = 'fixed' AND status IN ('open','needs-decision','planned','in-progress','regressed')
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

CREATE TRIGGER tr_review_code BEFORE INSERT ON reviews
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'R-*[^0-9]*' AND (
     NEW.code IS NOT 'R-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM reviews
        WHERE code = 'R-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'review codes are sequential: R- and the next number (R-0001, R-0002 ...)');
END;

CREATE TRIGGER tr_review_code_fixed BEFORE UPDATE OF code ON reviews WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

CREATE TRIGGER tr_review_manager_kind BEFORE INSERT ON reviews
WHEN NEW.manager_actor_id IS NOT NULL
 AND (SELECT kind FROM actors WHERE id = NEW.manager_actor_id) IS NOT 'manager' BEGIN
  SELECT RAISE(ABORT, 'manager_actor_id must be an actor of kind manager');
END;

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

CREATE TRIGGER tr_review_status AFTER UPDATE OF status ON reviews
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('review', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.change_actor_id, NEW.id, NEW.change_note);
  UPDATE reviews SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    completed_at = CASE WHEN NEW.status IN ('handed-off','abandoned')
                        THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END
  WHERE id = NEW.id;
END;

CREATE TRIGGER tr_review_context_cleared AFTER UPDATE ON reviews
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE reviews SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER tr_review_context_cleared_ins AFTER INSERT ON reviews
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE reviews SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

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

CREATE TRIGGER tr_run_created AFTER INSERT ON reviewer_runs BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('review', NEW.review_id, 'reviewer-run-created', NEW.label, NEW.actor_id, NEW.review_id,
          NEW.domain_code || ': ' || NEW.scope);
END;

CREATE TRIGGER tr_review_domain_active BEFORE INSERT ON review_domains
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

CREATE TRIGGER tr_metric_domain_active BEFORE INSERT ON metrics
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;

CREATE TRIGGER tr_metric_created AFTER INSERT ON metrics BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  VALUES ('review', NEW.review_id, 'metric-recorded', CAST(NEW.value AS TEXT), NEW.review_id,
          NEW.name || ' (' || NEW.scope || ')');
END;

-- ----- Actors, catalogs and standards

CREATE TRIGGER tr_actor_parent_self BEFORE INSERT ON actors
WHEN NEW.parent_actor_id IS NOT NULL AND NEW.parent_actor_id = NEW.id BEGIN
  SELECT RAISE(ABORT, 'an actor cannot be its own parent');
END;

CREATE TRIGGER tr_actor_parent_cycle BEFORE UPDATE OF parent_actor_id ON actors
WHEN NEW.parent_actor_id IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'that parent would make a cycle of actors')
  WHERE NEW.parent_actor_id = NEW.id
     OR EXISTS (WITH RECURSIVE up(n) AS (
                  SELECT NEW.parent_actor_id
                  UNION SELECT a.parent_actor_id FROM actors a JOIN up ON a.id = up.n WHERE a.parent_actor_id IS NOT NULL)
                SELECT 1 FROM up WHERE n = NEW.id);
END;

CREATE TRIGGER tr_design_goal_code BEFORE INSERT ON design_goals
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'G-[0-9][0-9]*' AND NEW.code NOT GLOB 'G-*[^0-9]*' AND (
     NEW.code IS NOT 'G-' || printf('%02d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM design_goals
        WHERE code = 'G-' || printf('%02d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'design goal codes are sequential: G- and the next number (G-01, G-02 ...)');
END;

CREATE TRIGGER tr_design_goal_code_fixed BEFORE UPDATE OF code ON design_goals WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

CREATE TRIGGER tr_requirement_code BEFORE INSERT ON requirements
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'REQ-*[^0-9]*' AND (
     NEW.code IS NOT 'REQ-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER))
  OR CAST(substr(NEW.code, 5) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 5) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM requirements
        WHERE code = 'REQ-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'requirement codes are sequential: REQ- and the next number (REQ-0001, REQ-0002 ...)');
END;

CREATE TRIGGER tr_requirement_code_fixed BEFORE UPDATE OF code ON requirements WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

-- ----- Decisions

CREATE TRIGGER tr_decision_code BEFORE INSERT ON decisions
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'D-*[^0-9]*' AND (
     NEW.code IS NOT 'D-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM decisions
        WHERE code = 'D-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'decision codes are sequential: D- and the next number (D-0001, D-0002 ...)');
END;

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

CREATE TRIGGER tr_decision_no_delete BEFORE DELETE ON decisions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: decisions cannot be deleted; set status revisit instead');
END;

-- ----- Gates

CREATE TRIGGER tr_gate_code BEFORE INSERT ON gates
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'GATE-*[^0-9]*' AND (
     NEW.code IS NOT 'GATE-' || printf('%04d', CAST(substr(NEW.code, 6) AS INTEGER))
  OR CAST(substr(NEW.code, 6) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 6) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM gates
        WHERE code = 'GATE-' || printf('%04d', CAST(substr(NEW.code, 6) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'gate codes are sequential: GATE- and the next number (GATE-0001, GATE-0002 ...)');
END;

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

CREATE TRIGGER tr_gate_options_shape_upd BEFORE UPDATE OF options ON gates
WHEN NEW.options IS NOT NULL AND json_valid(NEW.options) AND json_type(NEW.options) = 'array'
 AND EXISTS (SELECT 1 FROM json_each(NEW.options) j
             WHERE j.type <> 'object' OR json_type(j.value, '$.value') IS NOT 'text'
                OR json_type(j.value, '$.label') IS NOT 'text'
                OR json_type(j.value, '$.description') NOT IN ('text','null')) BEGIN
  SELECT RAISE(ABORT, 'gate options must be objects with a text value and label (and an optional description)');
END;

-- The same answer rules as the viewer, for answers written as SQL: answer_value is one of the option values,
-- and a gate without free text needs an option chosen.
CREATE TRIGGER tr_gate_answer_rules BEFORE UPDATE OF status, answer_value, answer_text ON gates
WHEN NEW.status = 'answered' AND (OLD.status IS NOT 'answered' OR OLD.answer_value IS NOT NEW.answer_value
                                  OR OLD.answer_text IS NOT NEW.answer_text) BEGIN
  SELECT RAISE(ABORT, 'answer_value must be one of the gate''s option values')
  WHERE NEW.answer_value IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM json_each(COALESCE(NEW.options, '[]')) j
                    WHERE json_extract(j.value, '$.value') = NEW.answer_value);
  SELECT RAISE(ABORT, 'this gate takes no free text: choose one of the options')
  WHERE NEW.allow_text = 0 AND NEW.answer_value IS NULL;
END;

-- A gate moves open -> answered -> applied (or withdrawn); it cannot be applied before it is answered. Any
-- status may go back to open (reopening, with the answer cleared: tr_gate_answer_final, tr_gate_answer_cleared).
CREATE TRIGGER tr_gate_transition BEFORE UPDATE OF status ON gates
WHEN OLD.status IS NOT NEW.status AND NEW.status <> 'open'
 AND NOT ((OLD.status = 'open' AND NEW.status IN ('answered','withdrawn'))
       OR (OLD.status = 'answered' AND NEW.status IN ('applied','withdrawn'))) BEGIN
  SELECT RAISE(ABORT, 'a gate moves open -> answered -> applied (or withdrawn); answer it before applying it');
END;

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
               ELSE COALESCE(NEW.answer_value || ': ', '') || COALESCE(NEW.answer_text, '') END);
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

CREATE TRIGGER tr_gate_context_cleared AFTER UPDATE ON gates WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE gates SET change_actor_id = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER tr_gate_context_cleared_ins AFTER INSERT ON gates
WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE gates SET change_actor_id = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER tr_gate_no_delete BEFORE DELETE ON gates BEGIN
  SELECT RAISE(ABORT, 'history is append-only: gates cannot be deleted; set status withdrawn instead');
END;

-- ----- Plans

CREATE TRIGGER tr_plan_code BEFORE INSERT ON plans
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'P-*[^0-9]*' AND (
     NEW.code IS NOT 'P-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM plans
        WHERE code = 'P-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'plan codes are sequential: P- and the next number (P-0001, P-0002 ...)');
END;

CREATE TRIGGER tr_plan_code_fixed BEFORE UPDATE OF code ON plans WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;

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

CREATE TRIGGER tr_plan_status AFTER UPDATE OF status ON plans
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.change_actor_id, NEW.change_note);
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER tr_plan_title_changed AFTER UPDATE OF title ON plans WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.change_note);
END;

CREATE TRIGGER tr_plan_touch AFTER UPDATE ON plans
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER tr_plan_context_cleared AFTER UPDATE ON plans
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE plans SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER tr_plan_context_cleared_ins AFTER INSERT ON plans
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE plans SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;

-- ----- Plan structure: phases, steps, dependencies, coverage links

-- Plan rules enforced by the database, on INSERT and on UPDATE.
CREATE TRIGGER tr_dep_order BEFORE INSERT ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
END;

CREATE TRIGGER tr_dep_order_upd BEFORE UPDATE ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
END;

CREATE TRIGGER tr_dep_created AFTER INSERT ON step_dependencies BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id, note)
  VALUES ('step', NEW.step_id, 'dependency-added',
          CAST((SELECT number FROM steps WHERE id = NEW.depends_on_step_id) AS TEXT), NEW.step_id,
          'depends on step ' || (SELECT number FROM steps WHERE id = NEW.depends_on_step_id));
END;

CREATE TRIGGER tr_step_phase_plan BEFORE INSERT ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;

CREATE TRIGGER tr_step_phase_plan_upd BEFORE UPDATE OF phase_id, plan_id ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;

CREATE TRIGGER tr_phase_plan_upd BEFORE UPDATE OF plan_id ON phases
WHEN OLD.plan_id IS NOT NEW.plan_id BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE EXISTS (SELECT 1 FROM steps WHERE phase_id = NEW.id AND plan_id IS NOT NEW.plan_id);
END;

-- Renumbering or moving a step keeps every dependency of it and on it pointing at an earlier step of the same
-- plan (renumber before loading dependencies, or renumber in an order that keeps them valid).
CREATE TRIGGER tr_step_renumber BEFORE UPDATE OF number, plan_id ON steps
WHEN OLD.number IS NOT NEW.number OR OLD.plan_id IS NOT NEW.plan_id BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE EXISTS (SELECT 1 FROM step_dependencies d JOIN steps b ON b.id = d.depends_on_step_id
                WHERE d.step_id = NEW.id AND (b.plan_id IS NOT NEW.plan_id OR b.number >= NEW.number))
     OR EXISTS (SELECT 1 FROM step_dependencies d JOIN steps a ON a.id = d.step_id
                WHERE d.depends_on_step_id = NEW.id AND (a.plan_id IS NOT NEW.plan_id OR a.number <= NEW.number));
END;

-- A status change needs change_note (moving to waiting-owner, owner_question will do); a step cannot start or
-- finish while a blocking gate on it is open or answered but not applied.
CREATE TRIGGER tr_step_change_needs_note BEFORE UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status AND NEW.change_note IS NULL
 AND NOT (NEW.status = 'waiting-owner' AND NEW.owner_question IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'give change_note with a step status change');
END;

CREATE TRIGGER tr_step_gate_blocks BEFORE UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status AND NEW.status IN ('doing','done')
 AND EXISTS (SELECT 1 FROM gates g WHERE g.step_id = NEW.id AND g.blocking = 1 AND g.status IN ('open','answered')) BEGIN
  SELECT RAISE(ABORT, 'a blocking gate on this step is not applied yet');
END;

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

CREATE TRIGGER tr_step_title_changed AFTER UPDATE OF title ON steps WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.id, NEW.change_note);
END;

CREATE TRIGGER tr_step_change_changed AFTER UPDATE OF change ON steps WHEN OLD.change IS NOT NEW.change BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'change-text-changed', OLD.change, NEW.change, NEW.change_actor_id, NEW.id,
          NEW.change_note);
END;

CREATE TRIGGER tr_step_proof_changed AFTER UPDATE OF proof ON steps WHEN OLD.proof IS NOT NEW.proof BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'proof-changed', OLD.proof, NEW.proof, NEW.change_actor_id, NEW.id, NEW.change_note);
END;

CREATE TRIGGER tr_step_touch AFTER UPDATE ON steps
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE steps SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER tr_step_context_cleared AFTER UPDATE ON steps
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE steps SET change_actor_id = NULL, change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER tr_step_context_cleared_ins AFTER INSERT ON steps
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE steps SET change_actor_id = NULL, change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;

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

CREATE TRIGGER tr_step_finding_created AFTER INSERT ON step_findings BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id, review_id, note)
  VALUES ('step-finding', NEW.finding_id, 'linked', (SELECT point_id FROM findings WHERE id = NEW.finding_id),
          NEW.step_id, (SELECT review_id FROM findings WHERE id = NEW.finding_id),
          'closed by step ' || (SELECT number FROM steps WHERE id = NEW.step_id) || COALESCE(': ' || NEW.multi_reason, ''));
END;

-- ----- Events

CREATE TRIGGER tr_events_no_update BEFORE UPDATE ON events BEGIN
  SELECT RAISE(ABORT, 'history is append-only: events cannot be changed');
END;

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
     OR (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'design_goals: adopted_at, retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;

CREATE TRIGGER tr_time_design_goals_upd BEFORE UPDATE OF adopted_at, retired_at ON design_goals
WHEN (NEW.adopted_at IS NOT NULL AND (NEW.adopted_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.adopted_at) IS NOT NEW.adopted_at))
     OR (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'design_goals: adopted_at, retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
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

-- ---------- Views ---------------------------------------------------------

-- Issues not yet closed, oldest and most severe first.
CREATE VIEW v_open_issues AS
SELECT i.code, i.title, i.severity, i.status, i.domain_code, i.theme_code, i.sub,
       i.times_seen, fr.code AS first_seen, fr.started_at AS first_seen_at,
       lr.code AS last_seen, cr.code AS last_checked, i.current_state, i.recommendation,
       (SELECT group_concat(DISTINCT fl.path || COALESCE('#' || fl.symbol, ''))
          FROM findings f JOIN finding_locations fl ON fl.finding_id = f.id
         WHERE f.issue_id = i.id AND fl.role = 'primary') AS locations
FROM issues i
JOIN reviews fr ON fr.id = i.first_seen_review_id
LEFT JOIN reviews lr ON lr.id = i.last_seen_review_id
LEFT JOIN reviews cr ON cr.id = i.last_checked_review_id
WHERE i.status IN ('open','needs-decision','planned','in-progress','regressed')
ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END,
         fr.started_at;

-- Known issues to hand each reviewer in the brief, by domain: with where they were seen (primary locations, as in
-- v_open_issues) and the owner's standing decisions on them (decided or to revisit).
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
WHERE i.status NOT IN ('duplicate','obsolete')
ORDER BY i.domain_code, i.code;

-- Issues raised by more than one review, or that came back after a fix.
CREATE VIEW v_recurring_issues AS
SELECT i.code, i.title, i.severity, i.status, i.times_seen,
       (SELECT count(*) FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
          AND e.event = 'status-changed' AND e.to_value = 'regressed') AS regressions,
       (SELECT group_concat(r.code, ', ') FROM
          (SELECT DISTINCT f.review_id FROM findings f WHERE f.issue_id = i.id) x
          JOIN reviews r ON r.id = x.review_id) AS reviews
FROM issues i
WHERE i.times_seen > 1 OR EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue'
        AND e.entity_id = i.id AND e.to_value = 'regressed')
ORDER BY regressions DESC, i.times_seen DESC;

-- Full life of one issue: filter by code.
CREATE VIEW v_issue_timeline AS
SELECT i.code, e.at, e.event, e.from_value, e.to_value, r.code AS review,
       s.number AS step, e.commit_sha, a.name AS actor, e.note
FROM events e
JOIN issues i ON e.entity_type = 'issue' AND e.entity_id = i.id
LEFT JOIN reviews r ON r.id = e.review_id
LEFT JOIN steps s ON s.id = e.step_id
LEFT JOIN actors a ON a.id = e.actor_id
ORDER BY i.code, e.at, e.id;

-- Every relation in both directions, with codes.
CREATE VIEW v_issue_graph AS
SELECT a.code AS from_issue, r.relation, b.code AS to_issue, r.confidence, r.rationale,
       rv.code AS established_in
FROM issue_relations r
JOIN issues a ON a.id = r.from_issue_id
JOIN issues b ON b.id = r.to_issue_id
LEFT JOIN reviews rv ON rv.id = r.established_in_review_id;

-- Per review and domain: counts by severity, verified and false. r.code is a GROUP BY term so that a WHERE
-- review = ? filter is pushed into the findings scan (one review's findings, not every review's).
CREATE VIEW v_review_summary AS
SELECT r.code AS review, r.started_at, f.domain_code,
       count(*) AS points,
       sum(f.severity = 'H') AS high, sum(f.severity = 'M') AS medium,
       sum(f.severity = 'L') AS low,  sum(f.severity = 'T') AS tests,
       sum(f.verification = 'confirmed') AS confirmed,
       sum(f.verification = 'false') AS false_findings,
       sum(f.match_type IN ('new','continuation')) AS new_issues,  -- a continuation opens a new issue
       sum(f.match_type IN ('still-present','changed')) AS carried_over,
       sum(f.match_type IN ('recurrence','regression')) AS came_back
FROM findings f JOIN reviews r ON r.id = f.review_id
GROUP BY r.id, r.code, f.domain_code
ORDER BY r.started_at, f.domain_code;

-- Findings not yet matched to an issue (must be empty before consolidating). False findings too: a false finding
-- is matched to an issue closed as obsolete, so the ledger keeps what was claimed and why it was wrong.
CREATE VIEW v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL;

-- Known issues in domains that ran, with no check recorded (must be empty). Known = first seen in another review
-- and opened before this one started, compared as julianday() values (an instant, not text).
CREATE VIEW v_unchecked_known_issues AS
SELECT r.code AS review, i.code AS issue, i.domain_code, i.title
FROM reviews r
JOIN review_domains rd ON rd.review_id = r.id AND rd.decision = 'included'
JOIN issues i ON i.domain_code = rd.domain_code
WHERE i.first_seen_review_id <> r.id
  AND julianday(i.opened_at) < julianday(r.started_at)
  AND i.status NOT IN ('duplicate','obsolete')
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = r.id AND c.issue_id = i.id);

-- Coverage: findings of a plan's reviews closed by no step (must be empty). A finding is covered by a step of
-- this plan, or by a done step of a plan this plan supersedes (work already finished under the old plan). The
-- probe goes from the finding to its steps (ix_step_findings_find), never over a whole plan's steps per finding.
CREATE VIEW v_uncovered_findings AS
SELECT p.code AS plan, r.code AS review, f.point_id, f.severity, f.summary
FROM plans p
JOIN plan_reviews pr ON pr.plan_id = p.id
JOIN reviews r ON r.id = pr.review_id
JOIN findings f ON f.review_id = r.id
WHERE f.verification <> 'false'
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                    JOIN plans sp ON sp.id = +s.plan_id
                  WHERE sf.finding_id = f.id
                    AND (sp.id = p.id OR (sp.superseded_by = p.id AND s.status = 'done')));

-- Coverage: steps that close nothing, and multi-closed findings with no reason.
CREATE VIEW v_coverage_problems AS
SELECT p.code AS plan, 'step closes nothing' AS problem, CAST(s.number AS TEXT) AS item
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE NOT EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
UNION ALL
SELECT p.code, 'finding closed by several steps without a reason', f.point_id
FROM step_findings sf JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
GROUP BY p.id, sf.finding_id
HAVING count(*) > 1 AND sum(sf.multi_reason IS NULL) > 0;

-- Progress per phase.
CREATE VIEW v_plan_progress AS
SELECT p.code AS plan, ph.number AS phase, ph.title,
       min(s.number) AS first_step, max(s.number) AS last_step, count(s.id) AS steps,
       sum(s.status = 'done') AS done, sum(s.status = 'doing') AS doing,
       sum(s.status = 'waiting-owner') AS waiting_owner,
       sum(s.status = 'not-started') AS not_started, sum(s.status = 'dropped') AS dropped
FROM phases ph JOIN plans p ON p.id = ph.plan_id
LEFT JOIN steps s ON s.phase_id = ph.id
GROUP BY ph.id ORDER BY p.code, ph.number;

-- The owner's to-do list: open gates, answered gates the agent has not
-- acted on yet, and anything else waiting on the owner without a gate.
-- Each thing once: a step or decision with an open or answered gate (on it, or for a decision on its step) is
-- listed as that gate. A step's question falls back to its progress note, then to its change text, so it is never
-- blank; it is blocking when an unfinished step depends on it.
CREATE VIEW v_owner_queue AS
SELECT 'gate' AS kind, g.code AS ref, g.kind AS need, g.title, g.question,
       g.status, g.blocking, g.created_at
FROM gates g WHERE g.status IN ('open','answered')
UNION ALL
SELECT 'step', p.code || ' #' || s.number, s.owner_need, s.title,
       COALESCE(NULLIF(trim(s.owner_question), ''), NULLIF(trim(s.progress_note), ''), s.change), 'open',
       EXISTS (SELECT 1 FROM step_dependencies sd JOIN steps ds ON ds.id = sd.step_id
               WHERE sd.depends_on_step_id = s.id AND ds.status NOT IN ('done','dropped')), s.updated_at
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE s.status = 'waiting-owner'
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = s.id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'decision', d.code, 'decision', d.question, d.revisit_when, d.status, 0, d.asked_at
FROM decisions d WHERE d.status IN ('open','revisit')
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.decision_id = d.id AND g.status IN ('open','answered'))
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = d.step_id AND g.status IN ('open','answered'));

-- Every code reference with what is needed to get back to the exact lines.
CREATE VIEW v_code_refs AS
SELECT r.code AS review, f.point_id, i.code AS issue, fl.id AS location_id, fl.role,
       rp.name AS repository, rp.path AS repo_path, fl.commit_sha, rr.tag_name,
       fl.path, fl.line_start, fl.line_end, fl.symbol, fl.blob_sha, fl.permalink,
       -- every argument single-quoted for a POSIX shell (an embedded quote is closed, escaped and reopened);
       -- ledger.py show, /api/source and the viewer's Copy button all use this one command
       'git -C ''' || replace(COALESCE(NULLIF(rp.path, ''), '.'), '''', '''\''''') || ''' show '''
         || replace(fl.commit_sha || ':' || fl.path, '''', '''\''''') || ''''
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

-- References that could not be traced back (must be empty after indexing). A whole-file reference (whole_file = 1)
-- needs no symbol; a not-text one (binary or not UTF-8) needs no excerpt. An excerpt whose line count differs from
-- its line range (lines past the end of the file, or a hand-written excerpt) is a problem, except a whole-file
-- excerpt cut at 200 lines (excerpt_status 'truncated').
CREATE VIEW v_untraceable_refs AS
SELECT review, point_id, path, problem FROM (
  SELECT r.code AS review, f.point_id, fl.path,
         CASE WHEN fl.commit_sha IS NULL THEN 'no commit'
              WHEN fl.line_start IS NULL AND fl.whole_file = 0 AND fl.symbol IS NULL THEN 'no lines or symbol'
              WHEN fl.line_start IS NULL AND fl.whole_file = 0 THEN 'symbol not resolved to lines'
              WHEN fl.excerpt IS NULL AND fl.excerpt_status IS NOT 'not-text' THEN 'no excerpt'
              WHEN fl.excerpt = '' THEN 'empty excerpt'
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
) WHERE problem IS NOT NULL;

-- Defects found during fix work (findings.discovered_in_step_id) that no plan step closes yet while their issue is
-- still open (must be empty): a discovery goes through the same process as a review finding. A step closes it when
-- it is linked (step_findings, step not dropped) to the finding itself or to another finding of the same issue.
CREATE VIEW v_unplanned_discoveries AS
SELECT p.code AS plan, s.number AS found_in_step, r.code AS review, f.point_id, i.code AS issue,
       i.status AS issue_status, f.summary
FROM findings f
JOIN steps s ON s.id = f.discovered_in_step_id
JOIN plans p ON p.id = s.plan_id
JOIN reviews r ON r.id = f.review_id
LEFT JOIN issues i ON i.id = f.issue_id
WHERE f.verification <> 'false'
  AND (i.id IS NULL OR i.status IN ('open','needs-decision','planned','in-progress','regressed'))
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps cs ON cs.id = sf.step_id
                  WHERE sf.finding_id = f.id AND cs.status <> 'dropped')
  AND NOT EXISTS (SELECT 1 FROM findings f2 JOIN step_findings sf ON sf.finding_id = f2.id
                  JOIN steps cs ON cs.id = sf.step_id
                  WHERE f.issue_id IS NOT NULL AND f2.issue_id = f.issue_id AND cs.status <> 'dropped');

-- Fixed issues with no guard test, or whose guard test has disappeared.
CREATE VIEW v_unguarded_fixes AS
SELECT i.code, i.title, i.status,
       CASE WHEN NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id)
            THEN 'no guard test' ELSE 'guard test removed' END AS problem
FROM issues i
WHERE i.status IN ('fixed','verified') AND i.kind NOT IN ('docs','design')
  AND NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1);

-- Trend of each metric across reviews, with its label and direction from metric_defs (the unit falls back to the
-- catalog's).
CREATE VIEW v_metric_trends AS
SELECT m.name, m.scope, COALESCE(rp.name, 'all') AS repository, r.code AS review,
       r.started_at, m.value, COALESCE(m.unit, md.unit) AS unit, m.method,
       COALESCE(md.label, upper(substr(m.name, 1, 1)) || replace(substr(m.name, 2), '_', ' ')) AS label,
       COALESCE(md.better, 'none') AS better
FROM metrics m JOIN reviews r ON r.id = m.review_id
LEFT JOIN repositories rp ON rp.id = m.repository_id
LEFT JOIN metric_defs md ON md.name = m.name
ORDER BY m.name, m.scope, r.started_at;

-- Hotspot memory: files that keep producing issues.
CREATE VIEW v_file_hotspots AS
SELECT fl.path, count(DISTINCT f.issue_id) AS issues, count(DISTINCT f.review_id) AS reviews,
       sum(f.severity = 'H') AS high_points
FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
WHERE f.verification <> 'false'
GROUP BY fl.path ORDER BY issues DESC, high_points DESC;

-- Decisions the owner has already made, so future reviews don't re-raise them blindly.
CREATE VIEW v_standing_decisions AS
SELECT d.code, d.question, d.answer, d.rationale, d.revisit_when, d.decided_at,
       (SELECT group_concat(i.code, ', ') FROM decision_issues di
          JOIN issues i ON i.id = di.issue_id WHERE di.decision_id = d.id) AS issues
FROM decisions d WHERE d.status = 'decided';

-- What a plan step closes: each finding it closes and that finding's issue with its status (Step 9 "its issues").
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

-- Steps still to do (in a plan that is not superseded or abandoned) whose issues are all closed already, e.g. by a
-- review's check of 'fixed': mark the step done or dropped. Listed by `ledger.py gates`; not a must-be-empty check.
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
                    AND (i.id IS NULL OR i.status NOT IN
                         ('fixed','verified','accepted-risk','wont-fix','duplicate','obsolete')));

-- Per repository, the commit the latest finished (handed-off) review looked at: the start of the next review's
-- `git log <commit>..HEAD`.
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

-- The requirement trace of each review: every verdict, with the count of that verdict in that review.
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

-- Every agent that worked on a review: its manager, each reviewer run's agent and the agents in review_actors.
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
 ('handover','Maintainability and handover','documentation','handover.md');

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
 ('handover','Applies whenever someone other than the current author may need to run or change the system.'))
UPDATE domains SET description = (SELECT text FROM applies WHERE applies.code = domains.code)
 WHERE description IS NULL AND code IN (SELECT code FROM applies);

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
INSERT INTO meta(key, value) VALUES ('schema_version','5');
INSERT INTO schema_migrations(version, description) VALUES (1, 'initial ledger schema'),
  (2, 'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted'),
  (3, 'integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers'),
  (4, 'check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes'),
  (5, 'one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries');
