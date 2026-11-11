#!/usr/bin/env python3
"""Code review ledger: one SQLite file per project that records every review,
every point it raised, how points relate across reviews, the fix plan, and
what is waiting on the owner. Standard library only (Python 3.9+; tested on 3.9.0, 3.9.25 and 3.13).

    python3 ledger.py init [--force]     create .code-review/ledger.sqlite here and copy this ledger.py beside it
                                         (never over a newer copy unless --force)
    python3 ledger.py migrate            upgrade an older ledger to this schema (keeps a .bak-v<N> copy)
    python3 ledger.py serve [--port N]   open the local viewer (127.0.0.1 only)
    python3 ledger.py check              integrity + must-be-empty checks
    python3 ledger.py fill-refs [R-0001] copy excerpts, blob ids, permalinks from git
    python3 ledger.py tag-review R-0001  tag each reviewed commit so it stays reachable
    python3 ledger.py show <location id> print the cited lines at their commit
    python3 ledger.py gates              list gates that are open or answered
    python3 ledger.py dump               write .code-review/ledger.sql (text, for diffs)

Options: --root DIR (project root; default: the nearest folder upward with a ledger, else the
top of the current git repository, else the current directory), --db FILE, --version.
"""
import argparse, hashlib, json, os, re, secrets, shlex, sqlite3, subprocess, sys, threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

TOOL_VERSION = "1.4.0"  # stamped by dev/build.py from `version:` in SKILL.md's frontmatter
TOOL_HASH = "dc572dee48a000118388a31e05c33f7b797e9e5cb59748fab2e48ee8b64053a0"  # stamped by dev/build.py: sha256 of the built file with this value as the placeholder
SCHEMA = r"""-- =====================================================================
-- Code review ledger — schema version 7
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

-- ---------- Words: the single source of the ledger's status words (D-0009) ----
-- Each lookup table lists the words a column may hold (referenced by foreign key), the plain label the viewer
-- shows the owner, and the order the viewer lists them in. Views, triggers and the viewer pick words by the
-- flags here, never by copies of the lists: a new issue status is one row in issue_statuses. Every other word
-- column keeps its CHECK list, with its labels in enum_labels (a test keeps the two equal). Seeded at the end of
-- this file.

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

CREATE TABLE severities (
  value    TEXT PRIMARY KEY,                 -- H, M, L, T
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE           -- most severe first
);

CREATE TABLE match_types (
  value     TEXT PRIMARY KEY,
  label     TEXT NOT NULL,
  position  INTEGER NOT NULL UNIQUE,
  came_back INTEGER NOT NULL CHECK (came_back IN (0,1))  -- the issue was closed and was raised again
);

CREATE TABLE relation_types (
  value         TEXT PRIMARY KEY,
  label         TEXT NOT NULL,              -- read from the issue the relation starts at ("Duplicate of")
  inverse_label TEXT NOT NULL,              -- read from the issue it points to ("Has duplicate")
  position      INTEGER NOT NULL UNIQUE,
  closes        INTEGER NOT NULL CHECK (closes IN (0,1))  -- recording it closes the active issue as duplicate
);

CREATE TABLE gate_kinds (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE
);

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

-- History is append-only: reviews, findings, steps and decisions refuse DELETE (tr_*_no_delete). The keys from
-- the children of reviews (review_repositories, review_goals, review_domains, reviewer_runs, review_actors,
-- findings, review_requirement_checks, review_issue_checks, metrics) and of findings (finding_locations,
-- finding_goals, finding_requirements) are ON DELETE RESTRICT (schema 6), so even with the triggers gone a
-- review or finding with rows under it cannot be deleted. The ON DELETE CASCADE keys left (phases, steps,
-- plan_reviews of plans; step_dependencies, step_findings, step_commits of steps; decision_issues of decisions)
-- fire only when their parent row is deleted, which the triggers refuse for steps and decisions; so they reach
-- only parents nothing protects (a plan with no steps, for instance). Abandon a review (status 'abandoned')
-- instead of deleting it.

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

CREATE TABLE review_goals (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);

-- Which domains were proposed, approved, skipped, and why.
CREATE TABLE review_domains (
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
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
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
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
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
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
  severity               TEXT NOT NULL REFERENCES severities(value),             -- current
  status                 TEXT NOT NULL DEFAULT 'open' REFERENCES issue_statuses(value),
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
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);

CREATE TABLE finding_goals (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);

CREATE TABLE finding_requirements (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);

-- The spec reviewer's verdict on every requirement a review traced, findings or not (a 'matches' or 'untested'
-- row has no finding). One row per review and requirement; v_requirement_trace lists them with counts.
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

-- What each review concluded about each issue already known when it started.
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
-- Which tables write events (plan step 73; a test keeps these two lists equal to the triggers and the tables):
-- Logged: reviews, reviewer_runs, issues, issue_relations, findings, review_issue_checks, decisions, gates, plans,
-- steps, step_dependencies, step_findings, metrics.
-- Not logged: finding_locations, finding_goals, finding_requirements, review_requirement_checks, test_runs,
-- artifacts, step_commits, guard_tests, decision_issues, plan_reviews, phases, review_repositories, review_goals,
-- review_domains, review_actors, actors, repositories, domains, themes, design_goals, requirements, metric_defs,
-- meta, schema_migrations, ledger_checks, issue_statuses, severities, match_types, relation_types, gate_kinds,
-- enum_labels.
-- Why not: finding_locations, finding_goals and finding_requirements belong to their finding (logged when it is
-- created) and cannot change but for fill-refs; review_requirement_checks, test_runs and artifacts are themselves
-- timestamped records written once; step_commits, guard_tests, decision_issues, plan_reviews and phases are plan
-- and evidence links whose rows are their own record; the rest are setup and configuration (who, which
-- repositories, the catalogs and word lists, design goals and requirements, the schema's own bookkeeping), whose
-- changes do not drive an issue's history.
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
-- The keys to the lookup tables (schema 6), so a change to a lookup row finds its users without a scan.
CREATE INDEX ix_fk_issues_severity                   ON issues(severity);
CREATE INDEX ix_fk_issue_relations_relation          ON issue_relations(relation);
CREATE INDEX ix_fk_findings_severity                 ON findings(severity);
CREATE INDEX ix_fk_findings_match_type               ON findings(match_type);
CREATE INDEX ix_fk_gates_kind                        ON gates(kind);

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

-- Accepting a risk or declining a fix is a decision: its reason goes in resolution (v_closed_without_reason lists
-- older rows without one).
CREATE TRIGGER tr_issue_closed_needs_reason BEFORE UPDATE OF status, resolution ON issues
WHEN NEW.status IN ('accepted-risk','wont-fix') AND NULLIF(trim(NEW.resolution), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue closed as accepted-risk or wont-fix needs a resolution (the reason)');
END;

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

-- Severity T means a missing test, nothing else (a finding's severity and kind never change after insert).
CREATE TRIGGER tr_finding_test_severity BEFORE INSERT ON findings
WHEN NEW.severity = 'T' AND NEW.kind IS NOT 'test' BEGIN
  SELECT RAISE(ABORT, 'severity T is for a missing test: give the finding kind test, or another severity');
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
                      OR (NEW.relation <> 'blocks' AND r.relation IN (SELECT value FROM relation_types WHERE closes = 1)))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;

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

-- Issues not yet closed, oldest and most severe first. locations: the primary locations of the issue's real
-- findings (v_real_findings), each prefixed with its repository name when the ledger has more than one, so the
-- same path in two repositories stays two places.
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
WHERE i.status IN (SELECT value FROM issue_statuses WHERE briefed = 1)
ORDER BY i.domain_code, i.id;   -- by id, so ISS-9999 comes before ISS-10000

-- Issues raised by more than one review, or that came back after a fix. Only real findings count (v_real_findings;
-- times_seen counts them too): a false re-raise does not make an issue recurring.
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

-- Per review and domain: counts by severity, verified and false. Every count but false_findings is of real
-- findings (the v_real_findings rule, verification <> 'false'); false_findings counts the rest. came_back counts
-- the issues of this review's real findings that this review sent back, from their events (the trigger's own
-- record): regressed, reopened from a closed status, or raised again while accepted. r.code is a GROUP BY term so
-- that a WHERE review = ? filter is pushed into the findings scan (one review's findings, not every review's).
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

-- Findings not yet matched to an issue (must be empty before consolidating). False findings too: a false finding
-- is matched to an issue closed as obsolete, so the ledger keeps what was claimed and why it was wrong. An
-- abandoned review's findings are not required to be matched.
CREATE VIEW v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL AND r.status <> 'abandoned';

-- Every finding but the false ones: the one definition of a real finding. Counts (times_seen, v_recurring_issues,
-- v_review_summary, v_file_hotspots, v_open_issues' locations) use it; the must-be-empty checks do not, since a
-- false finding must still be matched (to an issue closed as obsolete) and closed by a plan step.
CREATE VIEW v_real_findings AS
SELECT * FROM findings WHERE verification <> 'false';

-- Known issues in domains that ran, with no check recorded (must be empty). Known = first seen in another review
-- and opened before this one started, compared as julianday() values (an instant, not text). An abandoned review
-- needs no checks.
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

-- Coverage: findings of a plan's reviews closed by no step (must be empty). A finding is covered by a step of
-- this plan, or by a done step of a plan this plan supersedes (work already finished under the old plan). The
-- probe goes from the finding to its steps (ix_step_findings_find), never over a whole plan's steps per finding.
-- False findings count too (the plan's 'record false findings' step closes them). A dropped step covers nothing
-- unless a decided decision is linked to it (the owner chose not to do it). Superseded and abandoned plans, and
-- abandoned reviews, are not checked.
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

-- Coverage (must be empty): steps that close nothing, and findings closed by several steps without a reason. A
-- dropped step, or one whose no_findings_reason says why it closes nothing by design (the follow-up review that
-- verifies the plan), is not a problem; dropped steps do not count towards closing a finding twice. Superseded
-- and abandoned plans are not checked.
CREATE VIEW v_coverage_problems AS
SELECT p.code AS plan, 'step closes nothing' AS problem, CAST(s.number AS TEXT) AS item
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
  AND NULLIF(trim(s.no_findings_reason), '') IS NULL
  AND NOT EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
UNION ALL
SELECT p.code, 'finding closed by several steps without a reason', f.point_id
FROM step_findings sf JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
GROUP BY p.id, sf.finding_id
HAVING count(*) > 1 AND sum(sf.multi_reason IS NULL) > 0;

-- Progress per phase.
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

-- Every code reference with what is needed to get back to the exact lines. commit_sha is the location's own
-- commit, else its review's commit for that repository (commit_from says which: 'location', 'review', or NULL
-- when neither is known; git_command is then NULL and v_untraceable_refs lists the row as 'no commit').
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

-- References that could not be traced back (must be empty after indexing). A whole-file reference (whole_file = 1)
-- needs no symbol; a not-text one (binary or not UTF-8) needs no excerpt. An excerpt whose line count differs from
-- its line range (lines past the end of the file, or a hand-written excerpt) is a problem, except a whole-file
-- excerpt cut at 200 lines (excerpt_status 'truncated'). An empty excerpt is no excerpt; a location whose commit
-- differs from the commit its review looked at is a problem (unless the finding was discovered during fix work,
-- at a later commit: discovered_in_step_id); a finding with no location at all is one too. An
-- abandoned review's findings are not checked.
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
  UNION ALL
  SELECT r.code, f.point_id, NULL, 'no location'
  FROM findings f JOIN reviews r ON r.id = f.review_id
  WHERE r.status <> 'abandoned'
    AND NOT EXISTS (SELECT 1 FROM finding_locations fl WHERE fl.finding_id = f.id)
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
  AND (i.id IS NULL OR i.status IN (SELECT value FROM issue_statuses WHERE active = 1))
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
WHERE i.status IN (SELECT value FROM issue_statuses WHERE fixed = 1) AND COALESCE(i.kind, '') NOT IN ('docs','design')
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

-- Hotspot memory: files that keep producing issues, per repository (the same path in two repositories is two
-- files), from real findings only (v_real_findings).
CREATE VIEW v_file_hotspots AS
SELECT rp.name AS repository, fl.path, count(DISTINCT f.issue_id) AS issues,
       count(DISTINCT f.review_id) AS reviews, sum(f.severity = 'H') AS high_points
FROM finding_locations fl JOIN v_real_findings f ON f.id = fl.finding_id
JOIN repositories rp ON rp.id = fl.repository_id
GROUP BY fl.repository_id, fl.path ORDER BY issues DESC, high_points DESC, rp.name, fl.path;

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
                    AND (i.id IS NULL OR i.status IN
                         (SELECT value FROM issue_statuses WHERE active = 1)));

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

-- History gaps (must be empty): an issue with no 'opened' event, an issue whose status is not the last status
-- its events logged, and a gate answered or applied with no event reaching that status. The triggers write
-- these events, so a gap means a row was written with the triggers bypassed.
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

-- Matches whose type disagrees with the issue (must be empty): a continuation needs a continuation-of relation
-- from its new issue; 'new' (or 'continuation') is for an issue this review opened; 'still-present', 'changed',
-- 'recurrence' and 'regression' are for an issue an earlier review opened; a recurrence is of an issue that was
-- closed at some point; a real finding still present on an issue fixed before this review started is a
-- regression (match it so: it then regresses the issue). Abandoned reviews are not checked.
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

-- Steps closing a finding of a review their plan does not cover (must be empty): add the review back to
-- plan_reviews or close the finding in its own plan. tr_step_finding_in_plan refuses such a link when it is made;
-- this lists links made before it, or left behind when a plan_reviews row was removed.
CREATE VIEW v_off_plan_coverage AS
SELECT p.code AS plan, s.number AS step, r.code AS review, f.point_id
FROM step_findings sf
JOIN steps s ON s.id = sf.step_id
JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
JOIN reviews r ON r.id = f.review_id
WHERE p.status NOT IN ('superseded','abandoned')
  AND NOT EXISTS (SELECT 1 FROM plan_reviews pr WHERE pr.plan_id = p.id AND pr.review_id = f.review_id);

-- Done steps whose issues are still to do (must be empty): when a step is done, its issues move on (fixed with
-- after_state, or another closing status), unless another unfinished step still works on them. Regressed issues
-- are left to the next plan. Superseded and abandoned plans are not checked.
CREATE VIEW v_done_steps_open_issues AS
SELECT DISTINCT p.code AS plan, s.number AS step, i.code AS issue, i.status AS issue_status
FROM steps s
JOIN plans p ON p.id = s.plan_id
JOIN step_findings sf ON sf.step_id = s.id
JOIN findings f ON f.id = sf.finding_id
JOIN issues i ON i.id = f.issue_id
WHERE s.status = 'done' AND p.status NOT IN ('superseded','abandoned') AND f.verification <> 'false'
  AND i.status IN ('open','needs-decision','planned','in-progress')
  AND NOT EXISTS (SELECT 1 FROM findings f2 JOIN step_findings sf2 ON sf2.finding_id = f2.id
                  JOIN steps s2 ON s2.id = sf2.step_id JOIN plans p2 ON p2.id = s2.plan_id
                  WHERE f2.issue_id = i.id AND s2.status NOT IN ('done','dropped')
                    AND p2.status NOT IN ('superseded','abandoned'))
ORDER BY p.code, s.number, i.id;

-- Issues closed as accepted-risk or wont-fix with no resolution (must be empty): the reason is the record. New
-- closings are refused by tr_issue_closed_needs_reason; this lists rows written before it.
CREATE VIEW v_closed_without_reason AS
SELECT code, title, status FROM issues
WHERE status IN ('accepted-risk','wont-fix') AND NULLIF(trim(resolution), '') IS NULL;

-- Relation problems (must be empty): a related-to pair stored in both directions (related-to is symmetric, so one
-- row says it; tr_relation_noreverse_dup refuses the second for new rows). The later row is listed.
CREATE VIEW v_relation_problems AS
SELECT a.code AS from_issue, b.code AS to_issue, ir.relation, 'related-to stored in both directions' AS problem
FROM issue_relations ir
JOIN issues a ON a.id = ir.from_issue_id
JOIN issues b ON b.id = ir.to_issue_id
WHERE ir.relation = 'related-to'
  AND EXISTS (SELECT 1 FROM issue_relations rv WHERE rv.relation = 'related-to'
                AND rv.from_issue_id = ir.to_issue_id AND rv.to_issue_id = ir.from_issue_id AND rv.id < ir.id);

-- Review checks citing a finding matched to another issue (must be empty): a check's finding is the evidence for
-- that issue.
CREATE VIEW v_check_finding_owner AS
SELECT r.code AS review, i.code AS issue, f.point_id, fi.code AS finding_issue
FROM review_issue_checks c
JOIN reviews r ON r.id = c.review_id
JOIN issues i ON i.id = c.issue_id
JOIN findings f ON f.id = c.finding_id
JOIN issues fi ON fi.id = f.issue_id
WHERE f.issue_id <> c.issue_id;

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
 ('other','Question',10);
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
 ('entity_type','step-finding','Step coverage',10);

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
INSERT INTO meta(key, value) VALUES ('schema_version','7');
INSERT INTO schema_migrations(version, description) VALUES (1, 'initial ledger schema'),
  (2, 'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted'),
  (3, 'integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers'),
  (4, 'check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes'),
  (5, 'one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries'),
  (6, 'lookup tables for the ledger''s words and labels; RESTRICT keys under reviews and findings; metrics.name catalogued'),
  (7, 'must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason');
"""
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review ledger</title>
<style>
:root{--bg:#f7f8f6;--surface:#fff;--fg:#1b2420;--muted:#5d6b64;--line:#dfe4e0;--accent:#0f6b57;--accent-soft:#e2f1ec;--warn:#9a5b00;--warn-soft:#fbefd9;--crit:#a3271f;--crit-soft:#fae6e3;--info:#2b5c99;--info-soft:#e5eefa;
 --display:ui-rounded,"SF Pro Rounded","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;--body:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121715;--surface:#1a211e;--fg:#e6ece8;--muted:#9aa8a1;--line:#2c3632;--accent:#5fc9ae;--accent-soft:#17352d;--warn:#e7b25a;--warn-soft:#3a2c12;--crit:#f08d84;--crit-soft:#3d1c19;--info:#8db6ee;--info-soft:#1a2a40;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121715;--surface:#1a211e;--fg:#e6ece8;--muted:#9aa8a1;--line:#2c3632;--accent:#5fc9ae;--accent-soft:#17352d;--warn:#e7b25a;--warn-soft:#3a2c12;--crit:#f08d84;--crit-soft:#3d1c19;--info:#8db6ee;--info-soft:#1a2a40;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 var(--body);padding:0 max(16px,env(safe-area-inset-right)) 64px max(16px,env(safe-area-inset-left))}
a{color:var(--accent)}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 0 0;margin:0 -4px}
.top .row{max-width:1180px;margin:0 auto;display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:0 4px}
.brand{font:600 17px var(--display)}
.brand small{color:var(--muted);font:400 13px var(--body);margin-left:8px}
.tabs{display:flex;gap:2px;overflow-x:auto;max-width:1180px;margin:8px auto 0;padding:0 4px}
.tabs a{text-decoration:none;color:var(--muted);padding:8px 12px;border-bottom:2px solid transparent;white-space:nowrap;font-weight:500}
.tabs a[aria-current="page"]{color:var(--fg);border-color:var(--accent)}
.badge{display:inline-block;min-width:20px;padding:0 6px;border-radius:999px;background:var(--warn);color:var(--bg);font:600 12px/20px var(--mono);text-align:center;margin-left:6px}
.theme{margin-left:auto;all:unset;cursor:pointer;color:var(--muted);font-size:13px;padding:4px 8px;border:1px solid var(--line);border-radius:6px}
h1{font:600 26px/1.15 var(--display);margin:18px 0 0;text-wrap:balance}
h2{font:600 19px/1.2 var(--display);margin:8px 0 0}
h3{font:600 12px var(--body);letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:14px 0 4px}
.lede{color:var(--muted);max-width:72ch;margin:4px 0 0}
.facts{display:flex;flex-wrap:wrap;gap:10px 28px;font-variant-numeric:tabular-nums}
.facts div b{font:600 22px var(--display);display:block}
.facts div span{color:var(--muted);font-size:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input,select,textarea,button{font:inherit;color:var(--fg)}
.tools input,.tools select,textarea{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 10px;min-width:0}
.tools input{flex:1 1 240px}
textarea{width:100%;min-height:72px;resize:vertical}
button.btn{cursor:pointer;background:var(--accent);color:var(--bg);border:0;border-radius:6px;padding:8px 14px;font-weight:600}
button.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
button.btn:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font:600 12px var(--body);color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:break-word;min-width:4ch}
.grid td{white-space:nowrap;max-width:360px;overflow:hidden;text-overflow:ellipsis}
td:first-child{white-space:nowrap}tr.link{cursor:pointer}tr.link:hover td{background:var(--accent-soft)}
.scroll{overflow-x:auto}
.num{font-variant-numeric:tabular-nums;text-align:right}
.mono,code{font:500 12.5px var(--mono)}
.muted{color:var(--muted)}
.tag{display:inline-block;font:500 11px/1.6 var(--mono);padding:0 7px;border-radius:999px;background:var(--line);color:var(--fg);white-space:nowrap;margin:0 4px 2px 0}
.tag.H,.tag.crit{background:var(--crit-soft);color:var(--crit)}.tag.M,.tag.warn{background:var(--warn-soft);color:var(--warn)}
.tag.info{background:var(--info-soft);color:var(--info)}.tag.ok{background:var(--accent-soft);color:var(--accent)}.tag.solid{background:var(--accent);color:var(--bg)}.tag.you{background:var(--warn);color:var(--bg)}
details.item{background:var(--surface);border:1px solid var(--line);border-radius:8px}
details.item>summary{cursor:pointer;list-style:none;display:grid;grid-template-columns:minmax(90px,auto) minmax(0,1fr) auto;gap:10px;padding:10px 12px;align-items:baseline}
details.item>summary::-webkit-details-marker{display:none}
details.item>summary .n{font:500 13px var(--mono);color:var(--muted)}
details.item>summary .t{font-weight:500}
details.item>summary .tags{display:flex;flex-wrap:wrap;justify-content:flex-end}
.body{padding:2px 14px 14px;display:flex;flex-direction:column;gap:10px}
dl.kv{margin:0;display:grid;grid-template-columns:150px minmax(0,1fr);gap:6px 14px}
dl.kv dt{color:var(--muted);font-size:13px}dl.kv dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.ref{border-top:1px solid var(--line);padding-top:8px;display:flex;flex-direction:column;gap:6px}
.refline{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;font-size:13px}
.code{background:var(--bg);border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:420px}
.code table{font:12.5px/1.45 var(--mono)}
.code td{border:0;padding:0 10px;white-space:pre;overflow-wrap:normal}
.code td.ln{color:var(--muted);text-align:right;user-select:none;width:1%}
.code tr.hit td{background:var(--warn-soft)}
.note{font-size:13px;color:var(--muted)}
.mdtext{white-space:pre-wrap;overflow-wrap:anywhere;max-height:480px;overflow:auto;font-size:14px}
.status{padding:10px 12px;border-radius:6px;background:var(--accent-soft);color:var(--accent)}
.status.err{background:var(--crit-soft);color:var(--crit)}
.cols{display:grid;grid-template-columns:270px minmax(0,1fr);gap:24px;align-items:start}
nav.rail{display:flex;flex-direction:column;gap:2px;position:sticky;top:110px}
nav.rail button{all:unset;cursor:pointer;display:grid;grid-template-columns:24px 1fr auto;gap:8px;padding:7px 10px;border-radius:6px;font-size:14px;align-items:baseline}
nav.rail button:hover{background:var(--accent-soft)}
nav.rail button[aria-current="true"]{background:var(--accent);color:var(--bg)}
nav.rail .i,nav.rail .c{font:500 12px var(--mono);opacity:.8}
.bar{height:8px;border-radius:999px;background:var(--line);overflow:hidden;display:flex;max-width:520px}.bar i{display:block;height:100%}
.bar .d{background:var(--accent)}.bar .y{background:var(--warn)}.bar .g{background:var(--info)}
.gate{display:flex;flex-direction:column;gap:10px}
.gate .q{font-weight:500;white-space:pre-wrap}
.gate .ctx{white-space:pre-wrap;color:var(--muted);font-size:14px;max-height:260px;overflow:auto}
.opts{display:flex;flex-direction:column;gap:6px}
.opts label{display:grid;grid-template-columns:20px 1fr;gap:6px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;cursor:pointer}
.opts label:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.opts small{display:block;color:var(--muted)}
.timeline{display:flex;flex-direction:column;gap:0;border-left:2px solid var(--line);margin-left:6px}
.timeline>div{padding:4px 0 8px 14px;position:relative;font-size:14px}
.timeline>div::before{content:"";position:absolute;left:-6px;top:10px;width:10px;height:10px;border-radius:50%;background:var(--accent)}
.empty{color:var(--muted);padding:18px 0}
.chart svg{width:100%;height:auto;display:block}
.split{display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px;align-items:start}
.tlist{display:flex;flex-direction:column;gap:1px;max-height:70vh;overflow:auto}
.tlist button{all:unset;cursor:pointer;display:flex;justify-content:space-between;gap:8px;padding:5px 8px;border-radius:5px;font:13px var(--mono)}
.tlist button:hover{background:var(--accent-soft)}.tlist button[aria-current="true"]{background:var(--accent);color:var(--bg)}
@media (max-width:820px){.cols,.split{grid-template-columns:minmax(0,1fr)}nav.rail{position:static;flex-direction:row;overflow-x:auto}nav.rail button{grid-template-columns:auto auto auto;white-space:nowrap;border:1px solid var(--line)}
 details.item>summary{grid-template-columns:auto minmax(0,1fr)}details.item>summary .tags{grid-column:1/-1;justify-content:flex-start}dl.kv{grid-template-columns:minmax(0,1fr)}dl.kv dt{margin-top:6px}.tlist{max-height:220px}}
</style>
</head>
<body>
<header class="top">
  <div class="row"><span class="brand" id="brand">Review ledger</span><button class="theme" id="theme" type="button">Theme: auto</button></div>
  <nav class="tabs" id="tabs" aria-label="Sections"></nav>
</header>
<div class="wrap" id="app" aria-live="polite"></div>
<script>
"use strict";
// The token arrives once, in the launch URL's fragment (#k=<token>, optionally followed by #/route); it is kept in
// sessionStorage for this tab and removed from the address bar. The page itself carries no secret.
let TOKEN = "";
function takeToken(){
  const m = /^#k=([A-Za-z0-9_-]+)(#.*)?$/.exec(location.hash);
  if (m) {
    TOKEN = m[1];
    try { sessionStorage.setItem("ledger-token", TOKEN); } catch {}
    history.replaceState(null, "", location.pathname + location.search + (m[2] || "#/"));
  } else if (!TOKEN) {
    try { TOKEN = sessionStorage.getItem("ledger-token") || ""; } catch {}
  }
}
takeToken();
let LABELS = {domains:{}, themes:{}}, OVERVIEW = null;

// ---------- helpers
function h(tag, attrs, ...kids){
  const e = document.createElement(tag);
  if (attrs) for (const [k,v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) { if (c == null || c === false) continue; e.append(c instanceof Node ? c : String(c)); }
  return e;
}
// Links built from ledger data: only http(s) URLs become links (the schema refuses others; an older or edited
// ledger may still hold one). Anything else is shown as plain text, never as an href.
function safeHref(u){ return typeof u === "string" && /^https?:\/\//.test(u) ? u : null; }
function extLink(u, text){
  const href = safeHref(u);
  return href ? h("a", {href, target:"_blank", rel:"noopener noreferrer"}, text)
              : h("span", {class:"muted", title:"not an http(s) link, so not shown as a link"}, `${text}: ${u}`);
}
function put(el, ...kids){ el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false && k !== "").map(k => k instanceof Node ? k : document.createTextNode(String(k)))); return el; }
function add(el, ...kids){ for (const k of kids.flat(Infinity)) if (k != null && k !== false && k !== "") el.append(k instanceof Node ? k : String(k)); return el; }
async function api(path, body){
  const opt = {headers:{"X-Ledger-Token":TOKEN}};
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api/" + path, opt);
  const j = await r.json().catch(() => ({error:"The viewer sent back something unreadable."}));
  if (!r.ok || (j && j.error && !Array.isArray(j))) throw new Error(j.error || "Request failed");
  return j;
}
const words = s => s == null ? "" : String(s).replace(/[-_]/g," ").replace(/^./, c => c.toUpperCase());
// The words' labels come from the ledger (GET /api/overview labels.enums, from its lookup tables); L maps the page's
// short names to them. Without them (an older ledger) a word is shown through words(), and a severity letter
// through the fallback below, so the page never shows a raw value.
const ENUM_OF = {sev:"severity", issue:"issue_status", match:"match_type", verif:"verification", review:"review_status",
  scope:"review_scope", step:"step_status", need:"owner_need", gate:"gate_kind", gstat:"gate_status", rel:"relation_type",
  relIn:"relation_type_inverse", check:"check_outcome", kind:"issue_kind", run:"run_status", plan:"plan_status",
  decision:"decision_status"};
const FALLBACK = {sev:{H:"High",M:"Medium",L:"Low",T:"Missing test"}, need:{none:""}};
let L = {};
const setLabels = enums => { L = Object.fromEntries(Object.entries(ENUM_OF).map(([k, e]) =>
  [k, Object.assign({}, FALLBACK[k] || {}, (enums || {})[e] || {})])); };
setLabels({});
const lab = (map, v) => (L[map] && L[map][v] !== undefined) ? L[map][v] : words(v);
const domainName = c => c ? (LABELS.domains[c] || words(c)) : "";
const themeName = c => c ? (LABELS.themes[c] || words(c)) : "";
const sevTag = s => h("span", {class:"tag " + s, title:"Severity"}, lab("sev", s));
const issueTone = s => ({open:"warn","needs-decision":"you",regressed:"crit",planned:"info","in-progress":"info",fixed:"ok",verified:"solid"})[s] || "";
const issueTag = s => h("span", {class:"tag " + issueTone(s)}, lab("issue", s));
function when(iso, withTime=true){
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, withTime ? {dateStyle:"medium", timeStyle:"short"} : {dateStyle:"medium"});
}
const short = sha => sha ? sha.slice(0, 10) : "";
const kv = pairs => h("dl", {class:"kv"}, pairs.filter(p => p[1] != null && p[1] !== "" && !(Array.isArray(p[1]) && !p[1].length)).map(([k,v]) => [h("dt", null, k), h("dd", null, v)]));
const empty = t => h("p", {class:"empty"}, t);
function table(cols, data, onRow){
  if (!data.length) return empty("Nothing here yet.");
  return h("div", {class:"scroll"}, h("table", null,
    h("thead", null, h("tr", null, cols.map(c => h("th", {class:c.num ? "num" : null}, c.label)))),
    h("tbody", null, data.map(r => h("tr", onRow ? {class:"link", tabindex:"0", onclick:() => onRow(r), onkeydown:e => { if (e.key === "Enter") onRow(r); }} : null,
      cols.map(c => h("td", {class:c.num ? "num" : null}, c.get ? c.get(r) : (r[c.key] ?? ""))))))));
}
function copyBtn(text, label="Copy"){
  return h("button", {class:"btn ghost", type:"button", onclick:async e => {
    try { await navigator.clipboard.writeText(text); e.target.textContent = "Copied"; } catch { e.target.textContent = "Select and copy by hand"; }
  }}, label);
}
function go(hash){ location.hash = hash; }
function setView(...kids){ put(document.getElementById("app"), kids); window.scrollTo(0,0); }
function failed(err){ setView(h("h1", null, "Could not load this view"), h("p", {class:"status err"}, err.message), h("p", {class:"lede"}, "Make sure the viewer is still running in your terminal, then reload the page.")); }

// ---------- code at a commit
function codeRef(loc){
  const box = h("div");
  const range = loc.line_start ? (loc.line_end && loc.line_end !== loc.line_start ? `lines ${loc.line_start}–${loc.line_end}` : `line ${loc.line_start}`) : "";
  const btn = h("button", {class:"btn ghost", type:"button", onclick:async () => {
    if (box.childElementCount) { put(box, ); btn.textContent = "Show code at this commit"; return; }
    btn.disabled = true;
    try {
      const s = await api("source/" + loc.id);
      const hi = [s.line_start, s.line_end || s.line_start];
      const notes = [];
      if (s.source === "excerpt") notes.push("The commit could not be read from git here, so this is the copy of the lines saved at review time. " + (s.git_error ? "Git said: " + s.git_error : ""));
      if (s.source === "none") notes.push("The commit could not be read from git and no copy of the lines was saved. " + (s.git_error || ""));
      if (s.excerpt_matches === false) notes.push("The saved copy of these lines differs from the file at this commit; the line numbers may have been recorded wrong.");
      put(box, 
        notes.length ? h("p", {class:"note"}, notes.join(" ")) : null,
        s.lines && s.lines.length ? h("div", {class:"code"}, h("table", null, h("tbody", null, s.lines.map(l =>
          h("tr", {class: hi[0] && l.n >= hi[0] && l.n <= hi[1] ? "hit" : null}, h("td", {class:"ln"}, l.n), h("td", null, l.text)))))) : null,
        h("div", {class:"refline"}, h("code", null, s.git_command), copyBtn(s.git_command, "Copy git command"),
          s.permalink ? extLink(s.permalink, "Open on the code host") : null));
      btn.textContent = "Hide code";
    } catch (e) { put(box, h("p", {class:"status err"}, e.message)); }
    btn.disabled = false;
  }}, "Show code at this commit");
  return h("div", {class:"ref"},
    h("div", {class:"refline"},
      h("span", {class:"tag"}, words(loc.role)), h("b", null, loc.repo), h("code", null, loc.path), range ? h("span", null, range) : null,
      loc.symbol ? h("code", {class:"muted"}, loc.symbol) : null,
      h("span", {class:"muted"}, "at commit ", h("code", null, short(loc.commit_sha))), btn),
    box);
}

function findingItem(f, opts={}){
  return h("details", {class:"item"},
    h("summary", null, h("span", {class:"n"}, opts.showReview ? `${f.review_code} · ${f.point_id}` : f.point_id), h("span", {class:"t"}, f.summary),
      h("span", {class:"tags"}, sevTag(f.severity), f.match_type ? h("span", {class:"tag " + (["recurrence","regression"].includes(f.match_type) ? "crit" : f.match_type === "new" ? "info" : "")}, lab("match", f.match_type)) : null,
        f.verification === "false" ? h("span", {class:"tag"}, "False finding") : f.verification === "confirmed" ? h("span", {class:"tag ok"}, "Confirmed") : null)),
    h("div", {class:"body"},
      kv([["Area", domainName(f.domain_code)], ["Theme", themeName(f.theme_code)], ["Kind", lab("kind", f.kind)],
          ["Issue", f.issue_code && !opts.hideIssue ? h("a", {href:"#/issue/" + f.issue_code}, f.issue_code) : null],
          ["How it was matched", f.match_rationale], ["What it does now", f.before_state], ["Detail", f.detail],
          ["Recommended fix", f.recommendation], ["Test to add", f.recommended_test], ["Evidence", f.evidence],
          ["Checked by the manager", f.verification ? lab("verif", f.verification) + (f.verification_note ? " — " + f.verification_note : "") : null],
          ["Reviewer", f.run_label], ["In the report", f.where_report]]),
      (f.locations || []).map(codeRef)));
}

// ---------- views
const VIEWS = {};

VIEWS.home = async () => {
  const o = OVERVIEW = await api("overview");
  const sev = Object.fromEntries(o.open_by_severity.map(r => [r.severity, r.n]));
  const openTotal = o.open_by_severity.reduce((a,r) => a + r.n, 0);
  const failing = o.checks.filter(c => c.n);
  setView(
    h("h1", null, o.project),
    h("p", {class:"lede"}, "Every review of this project, every point it raised, how those points carried over between reviews, the fix plans, and anything waiting on you."),
    h("div", {class:"facts"},
      h("div", null, h("b", null, o.queue), h("span", null, "waiting on you")),
      h("div", null, h("b", null, openTotal), h("span", null, "open issues")),
      h("div", null, h("b", null, sev.H || 0), h("span", null, "open and high")),
      h("div", null, h("b", null, o.recurring), h("span", null, "came back or seen again"))),
    o.queue ? h("p", {class:"status"}, h("a", {href:"#/inbox"}, `${o.queue} ${o.queue === 1 ? "thing needs" : "things need"} your input or approval →`)) : null,
    h("div", {class:"grid2"},
      h("div", {class:"card"}, h("h2", null, "Latest reviews"),
        table([{label:"Review", get:r => r.code}, {label:"Title", key:"title"}, {label:"Status", get:r => lab("review", r.status)},
               {label:"Started", get:r => when(r.started_at, false)}, {label:"High", key:"high", num:1}, {label:"Points", key:"points", num:1}],
              o.reviews, r => go("#/review/" + r.code))),
      h("div", {class:"card"}, h("h2", null, "Fix plans"),
        o.plans.length ? o.plans.map(p => h("div", {style:"margin:10px 0"},
          h("a", {href:"#/plan/" + p.code}, `${p.code} · ${p.title}`), h("span", {class:"muted"}, " — " + lab("plan", p.status)),
          progressBar(p.done || 0, 0, p.waiting || 0, p.steps || 0))) : empty("No fix plan yet."))),
    h("div", {class:"card"}, h("h2", null, "Ledger checks"),
      h("p", {class:"note"}, "These should all be zero once a review has been indexed and matched."),
      table([{label:"Check", key:"label"}, {label:"Count", key:"n", num:1}], o.checks, c => c.n && go("#/explore/" + c.view)),
      failing.length ? h("p", {class:"status err"}, `${failing.length} ${failing.length === 1 ? "check needs" : "checks need"} attention.`) : null));
};

function progressBar(done, doing, waiting, total){
  const pct = n => total ? (100 * n / total).toFixed(2) + "%" : "0";
  return h("div", {style:"display:flex;flex-direction:column;gap:4px;margin-top:4px"},
    h("div", {class:"bar", role:"img", "aria-label":`${done} of ${total} done`},
      h("i", {class:"d", style:`width:${pct(done)}`}), h("i", {class:"g", style:`width:${pct(doing)}`}), h("i", {class:"y", style:`width:${pct(waiting)}`})),
    h("span", {class:"note"}, `${done} of ${total} done` + (doing ? ` · ${doing} in progress` : "") + (waiting ? ` · ${waiting} waiting on you` : "")));
}

VIEWS.inbox = async () => {
  const d = await api("gates?status=pending");
  const open = d.gates.filter(g => g.status === "open"), answered = d.gates.filter(g => g.status === "answered");
  setView(
    h("h1", null, "Waiting on you"),
    h("p", {class:"lede"}, "Questions and approvals the agent cannot get past without you. Answer here or in the chat; the agent picks answers up the next time it checks the ledger."),
    open.length ? open.map(gateCard) : empty("Nothing is waiting on you right now."),
    answered.length ? [h("h3", null, "Answered, waiting for the agent to act"), answered.map(gateCard)] : null,
    d.other.length ? [h("h3", null, "Also waiting on you (answer in the chat)"),
      table([{label:"What", get:r => r.kind === "step" ? "Plan step " + r.ref : "Decision " + r.ref}, {label:"Needs", get:r => lab("need", r.need) || "Your input"},
             {label:"Title", key:"title"}, {label:"Question", key:"question"}], d.other)] : null,
    h("p", null, h("a", {href:"#/history"}, "See every question and answer so far")));
};

VIEWS.history = async () => {
  const d = await api("gates?status=all");
  setView(h("h1", null, "Questions and answers"),
    table([{label:"Asked", get:g => when(g.created_at)}, {label:"About", get:g => lab("gate", g.kind)}, {label:"Title", key:"title"},
           {label:"Status", get:g => lab("gstat", g.status)}, {label:"Answer", get:g => [g.answer_value ? optLabel(g) : "", g.answer_text ? " " + g.answer_text : ""]},
           {label:"What the agent did", key:"applied_note"}], d.gates));
};
const optLabel = g => ((g.options || []).find(o => o.value === g.answer_value) || {}).label || words(g.answer_value);

function gateCard(g){
  const refs = [g.review_code && h("a", {href:"#/review/" + g.review_code}, "Review " + g.review_code),
                g.plan_code && h("a", {href:"#/plan/" + g.plan_code}, "Plan " + g.plan_code + (g.step_number ? " step " + g.step_number : "")),
                g.issue_code && h("a", {href:"#/issue/" + g.issue_code}, "Issue " + g.issue_code)].filter(Boolean);
  const card = h("div", {class:"card gate"},
    h("div", null, h("span", {class:"tag " + (g.kind === "production-action" ? "crit" : g.status === "open" ? "you" : "info")}, lab("gate", g.kind)),
      g.blocking && g.status === "open" ? h("span", {class:"tag warn"}, "Work is paused until you answer") : null,
      h("span", {class:"note"}, " Asked " + when(g.created_at))),
    h("h2", null, g.title), h("div", {class:"q"}, g.question),
    g.context ? h("div", {class:"ctx"}, g.context) : null,
    refs.length ? h("div", {class:"refline"}, refs) : null);
  if (g.status === "answered") {
    add(card, h("p", {class:"status"}, "You answered: ", g.answer_value ? h("b", null, optLabel(g)) : null, g.answer_text ? (g.answer_value ? " — " : "") + g.answer_text : "",
      ` (${when(g.answered_at)})`));
    return card;
  }
  const name = "opt-" + g.code;
  const opts = (g.options || []).length ? h("div", {class:"opts", role:"radiogroup", "aria-label":"Choices"},
    g.options.map(o => h("label", null, h("input", {type:"radio", name, value:o.value}), h("span", null, o.label, o.description ? h("small", null, o.description) : null)))) : null;
  const ta = g.allow_text ? h("textarea", {placeholder: opts ? "Add a note or a different answer (optional)" : "Your answer", "aria-label":"Your answer"}) : null;
  const msg = h("div");
  const send = h("button", {class:"btn", type:"button", onclick:async () => {
    const picked = card.querySelector(`input[name="${name}"]:checked`);
    send.disabled = true;
    try {
      await api(`gates/${encodeURIComponent(g.code)}/answer`, {answer_value: picked ? picked.value : null, answer_text: ta ? ta.value : null});
      put(msg, h("p", {class:"status"}, "Answer saved. The agent will act on it the next time it checks."));
      setTimeout(() => route(), 1200);
    } catch (e) { put(msg, h("p", {class:"status err"}, e.message)); send.disabled = false; }
  }}, "Send answer");
  add(card, opts, ta, h("div", null, send), msg);
  return card;
}

VIEWS.reviews = async () => {
  const list = await api("reviews");
  setView(h("h1", null, "Reviews"), h("p", {class:"lede"}, "Every review run on this project, newest first."),
    table([{label:"Review", key:"code"}, {label:"Title", key:"title"}, {label:"Kind", get:r => lab("scope", r.scope)},
           {label:"Status", get:r => lab("review", r.status)}, {label:"Started", get:r => when(r.started_at)}, {label:"Code reviewed", get:r => h("span", {class:"mono"}, r.commits || "")},
           {label:"High", key:"high", num:1}, {label:"Medium", key:"medium", num:1}, {label:"Low", key:"low", num:1}, {label:"New", key:"new_issues", num:1}, {label:"Came back", key:"came_back", num:1}],
          list, r => go("#/review/" + r.code)));
};

const REQ_VERDICT = {matches:"matches", differs:"differs", partly:"partly matches", missing:"missing", untested:"not tested"};
VIEWS.review = async ([code]) => {
  const d = await api("review/" + encodeURIComponent(code)); const r = d.review;
  const fs = d.findings;
  const sel = (id, label, values, fmt) => h("select", {id, "aria-label":label}, h("option", {value:""}, label), [...new Set(values)].filter(v => v != null).map(v => h("option", {value:v}, fmt(v))));
  const q = h("input", {type:"search", placeholder:"Search points, ids, files", "aria-label":"Search points"});
  const fd = sel("fd", "All areas", fs.map(f => f.domain_code), domainName), fsv = sel("fs", "All severities", ["H","M","L","T"].filter(s => fs.some(f => f.severity === s)), v => lab("sev", v));
  const fm = sel("fm", "Any match", fs.map(f => f.match_type), v => lab("match", v)), fv = sel("fv", "Any check", fs.map(f => f.verification), v => lab("verif", v));
  const listBox = h("div", {style:"display:flex;flex-direction:column;gap:8px"});
  let shown = 100;
  const draw = () => {
    const t = q.value.trim().toLowerCase();
    const hit = fs.filter(f => (!fd.value || f.domain_code === fd.value) && (!fsv.value || f.severity === fsv.value) && (!fm.value || f.match_type === fm.value) && (!fv.value || f.verification === fv.value)
      && (!t || [f.point_id, f.summary, f.detail, f.issue_code, ...(f.locations||[]).map(l => l.path)].join(" ").toLowerCase().includes(t)));
    put(listBox, h("p", {class:"note"}, `${hit.length} of ${fs.length} points`), hit.slice(0, shown).map(f => findingItem(f)),
      hit.length > shown ? h("button", {class:"btn ghost", type:"button", onclick:() => { shown += 200; draw(); }}, `Show more (${hit.length - shown} left)`) : null);
  };
  [q, fd, fsv, fm, fv].forEach(e => e.addEventListener("input", () => { shown = 100; draw(); }));
  const sumBy = k => fs.filter(f => f.severity === k).length;
  setView(
    h("h1", null, `${r.code} · ${r.title}`),
    h("p", {class:"lede"}, `${lab("scope", r.scope)} · ${lab("review", r.status)} · started ${when(r.started_at)}` + (r.completed_at ? ` · finished ${when(r.completed_at)}` : "")),
    h("div", {class:"facts"}, h("div", null, h("b", null, fs.length), h("span", null, "points")), ["H","M","L","T"].map(s => h("div", null, h("b", null, sumBy(s)), h("span", null, lab("sev", s)))),
      h("div", null, h("b", null, fs.filter(f => ["new","continuation"].includes(f.match_type)).length), h("span", null, "new issues")),
      h("div", null, h("b", null, fs.filter(f => ["recurrence","regression"].includes(f.match_type)).length), h("span", null, "came back"))),
    d.gates.length ? h("div", {class:"card"}, h("h2", null, "Questions asked during this review"), table([{label:"About", get:g => lab("gate", g.kind)}, {label:"Title", key:"title"}, {label:"Status", get:g => lab("gstat", g.status)}, {label:"Answer", get:g => [g.answer_value || "", " ", g.answer_text || ""]}], d.gates, () => go("#/inbox"))) : null,
    h("div", {class:"card"}, kv([["Verdict", r.verdict], ["Summary", r.summary], ["What was asked", r.request_text], ["Run by", r.manager ? `${r.manager}${r.manager_model ? " (" + r.manager_model + ")" : ""}` : null],
      ["Domains approved by", r.approved_by ? `${r.approved_by}, ${when(r.approved_at)}` : null], ["Follows", r.previous_code ? h("a", {href:"#/review/" + r.previous_code}, r.previous_code) : null],
      ["Write-up", r.summary_doc_url ? extLink(r.summary_doc_url, "Open the review document") : null]])),
    r.report_md ? h("div", {class:"card"}, h("h2", null, "The review document"), h("div", {class:"mdtext"}, r.report_md)) : null,
    h("h3", null, "Code reviewed"),
    table([{label:"Repository", key:"name"}, {label:"Branch", key:"branch"}, {label:"Commit", get:x => [h("code", null, short(x.commit_sha)), " ", copyBtn(x.commit_sha, "Copy")]},
           {label:"Commit message", key:"commit_subject"}, {label:"Kept by tag", get:x => x.tag_name ? x.tag_name + (x.tag_pushed ? "" : " (not pushed)") : "Not tagged"},
           {label:"Uncommitted changes", get:x => x.is_dirty ? (x.dirty_note || "Yes") : "None"}], d.repos),
    d.goals.length ? [h("h3", null, "Design goals used"), h("ul", null, d.goals.map(g => h("li", null, g.text)))] : null,
    h("h3", null, "Areas reviewed and skipped"),
    table([{label:"Area", get:x => x.name}, {label:"", get:x => x.decision === "included" ? h("span", {class:"tag ok"}, "Reviewed") : h("span", {class:"tag"}, "Skipped")},
           {label:"Why", key:"reason"}, {label:"Your change", get:x => x.owner_change === "none" ? "" : words(x.owner_change)}, {label:"Reviewers", key:"reviewers_planned", num:1}], d.domains),
    h("h3", null, "Reviewers"),
    table([{label:"Reviewer", key:"label"}, {label:"Area", get:x => domainName(x.domain_code)}, {label:"Agent", get:x => x.model ? `${x.actor} (${x.model})` : x.actor},
           {label:"Status", get:x => lab("run", x.status)}, {label:"Items", key:"items_reviewed", num:1}, {label:"High", key:"high_count", num:1}, {label:"Medium", key:"medium_count", num:1}, {label:"Low", key:"low_count", num:1}], d.runs),
    d.agents.length ? [h("h3", null, "Agents"),
      table([{label:"Agent", key:"name"}, {label:"Did", key:"role"}, {label:"Kind", get:x => words(x.kind)}, {label:"Model", key:"model"}, {label:"Run in", key:"harness"}, {label:"Started by", key:"parent"}], d.agents)] : null,
    d.requirements.length ? [h("h3", null, "Requirements"),
      h("p", {class:"note"}, [...new Map(d.requirements.map(x => [x.verdict, x.verdict_count]))].map(([v, n]) => `${n} ${REQ_VERDICT[v] || words(v)}`).join(" · ")),
      table([{label:"Requirement", key:"requirement"}, {label:"Says", key:"text"}, {label:"Found", get:x => REQ_VERDICT[x.verdict] || words(x.verdict)}, {label:"Where", key:"where_implemented"},
             {label:"Tested", get:x => words(x.tested)}, {label:"Point", key:"point_id"}, {label:"Evidence", key:"evidence"}], d.requirements)] : null,
    d.checks.length ? [h("h3", null, "Earlier issues this review looked for"),
      table([{label:"Issue", key:"issue_code"}, {label:"Title", key:"title"}, {label:"Found", get:x => lab("check", x.outcome)}, {label:"Evidence", key:"evidence"}, {label:"Now", get:x => issueTag(x.status)}],
            d.checks, x => go("#/issue/" + x.issue_code))] : null,
    d.metrics.length ? [h("h3", null, "Measurements"), table([{label:"Measure", get:m => m.label || words(m.name)}, {label:"Of", key:"scope"}, {label:"Value", get:m => `${m.value}${m.unit ? " " + m.unit : ""}`, num:1}, {label:"How", get:m => words(m.method)}, {label:"Note", key:"note"}], d.metrics)] : null,
    h("h3", null, "Points raised"),
    h("div", {class:"tools"}, q, fd, fsv, fm, fv), listBox);
  draw();
};

VIEWS.issues = async (_, params) => {
  const o = OVERVIEW || await api("overview");
  const q = h("input", {type:"search", placeholder:"Search titles, ids, point ids", "aria-label":"Search issues", value:params.get("q") || ""});
  const st = h("select", {"aria-label":"Status"}, [["active","Still to fix"],["","Any status"], ...Object.entries(L.issue)].map(([v,t]) => h("option", {value:v}, t)));
  const sv = h("select", {"aria-label":"Severity"}, [["","Any severity"], ...Object.entries(L.sev)].map(([v,t]) => h("option", {value:v}, t)));
  const dm = h("select", {"aria-label":"Area"}, [["","All areas"], ...Object.entries(o.labels.domains)].map(([v,t]) => h("option", {value:v}, t)));
  st.value = params.has("status") ? params.get("status") : "active"; sv.value = params.get("severity") || ""; dm.value = params.get("domain") || "";
  const box = h("div");
  let timer;
  const load = async () => {
    const p = new URLSearchParams(); if (st.value) p.set("status", st.value); if (sv.value) p.set("severity", sv.value); if (dm.value) p.set("domain_code", dm.value); if (q.value.trim()) p.set("q", q.value.trim());
    try {
      const list = await api("issues?" + p);
      put(box, h("p", {class:"note"}, `${list.length} issues`), table([{label:"Issue", key:"code"}, {label:"Title", key:"title"}, {label:"Severity", get:i => sevTag(i.severity)},
        {label:"Status", get:i => issueTag(i.status)}, {label:"Area", get:i => domainName(i.domain_code)}, {label:"First seen", key:"first_seen"}, {label:"Last seen", key:"last_seen"}, {label:"Reviews", key:"times_seen", num:1}],
        list, i => go("#/issue/" + i.code)));
    } catch (e) { put(box, h("p", {class:"status err"}, e.message)); }
  };
  [st, sv, dm].forEach(e => e.addEventListener("change", load)); q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  setView(h("h1", null, "Issues"), h("p", {class:"lede"}, "An issue is one underlying problem, followed across every review that raised it or looked for it."), h("div", {class:"tools"}, q, st, sv, dm), box);
  load();
};

VIEWS.issue = async ([code]) => {
  const d = await api("issue/" + encodeURIComponent(code)); const i = d.issue;
  const cont = d.relations.find(x => x.dir === "out" && x.relation === "continuation-of");
  const continues = "Raised as a new issue that continues " + (cont ? cont.other : "an earlier issue");
  setView(
    h("h1", null, `${i.code} · ${i.title}`),
    h("div", null, sevTag(i.severity), issueTag(i.status), h("span", {class:"tag"}, domainName(i.domain_code)), i.theme_code ? h("span", {class:"tag"}, themeName(i.theme_code)) : null),
    h("div", {class:"card"}, kv([["First found", h("span", null, h("a", {href:"#/review/" + i.first_seen}, i.first_seen), " · " + when(i.opened_at))],
      ["Last raised", i.last_seen ? h("a", {href:"#/review/" + i.last_seen}, i.last_seen) : null], ["Last looked for", i.last_checked ? h("a", {href:"#/review/" + i.last_checked}, i.last_checked) : null],
      ["Raised in", `${i.times_seen} ${i.times_seen === 1 ? "review" : "reviews"}`],
      ["Before", i.before_state], ["Now", i.current_state], ["After the fix", i.after_state], ["Agreed fix", i.recommendation], ["Resolution", i.resolution],
      ["Fixed", when(i.resolved_at)], ["Fix confirmed", when(i.verified_at)]])),
    d.relations.length ? [h("h3", null, "Related issues"), table([{label:"", get:x => x.dir === "out" ? lab("rel", x.relation) : lab("relIn", x.relation)},
      {label:"Issue", key:"other"}, {label:"Title", key:"other_title"}, {label:"Status", get:x => issueTag(x.other_status)}, {label:"How sure", get:x => words(x.confidence)}, {label:"Why", key:"rationale"}, {label:"Linked in", key:"review"}],
      d.relations, x => go("#/issue/" + x.other))] : null,
    d.decisions.length ? [h("h3", null, "Your decisions about it"), table([{label:"Decision", key:"code"}, {label:"Question", key:"question"}, {label:"Answer", key:"answer"}, {label:"Why", key:"rationale"}, {label:"When", get:x => when(x.decided_at)}], d.decisions)] : null,
    d.steps.length ? [h("h3", null, "Fix plan steps"), table([{label:"Plan", key:"plan"}, {label:"Step", key:"number", num:1}, {label:"Title", key:"title"}, {label:"Status", get:x => lab("step", x.status)},
      {label:"Commits", get:x => h("span", {class:"mono"}, x.commits || "")}, {label:"Note", key:"progress_note"}], d.steps, x => go(`#/plan/${x.plan}`))] : null,
    d.guard_tests.length ? [h("h3", null, "Tests that guard the fix"), table([{label:"Test", get:t => h("code", null, `${t.test_path} › ${t.test_name}`)},
      {label:"Failed before the fix", get:t => t.failed_before_fix == null ? "Not recorded" : t.failed_before_fix ? "Yes" : "No"}, {label:"Still in the code", get:t => t.still_exists ? "Yes" : "Removed"},
      {label:"Added in", get:t => h("code", null, short(t.added_commit_sha))}], d.guard_tests)] : null,
    d.checks.length ? [h("h3", null, "What each review found"), table([{label:"Review", key:"review"}, {label:"When", get:x => when(x.started_at, false)}, {label:"Found", get:x => lab("check", x.outcome)}, {label:"Evidence", key:"evidence"}], d.checks, x => go("#/review/" + x.review))] : null,
    h("h3", null, "Every time it was raised, with the code at that commit"),
    d.findings.map(f => findingItem(f, {showReview:true, hideIssue:true})),
    h("h3", null, "History"),
    d.timeline.length ? h("div", {class:"timeline"}, d.timeline.map(e => h("div", null, h("b", null, when(e.at)), " — ",
      e.event === "status-changed" ? `${lab("issue", e.from_value)} → ${lab("issue", e.to_value)}` : e.event === "severity-changed" ? `Severity ${lab("sev", e.from_value)} → ${lab("sev", e.to_value)}`
      : e.event === "seen" ? (e.to_value === "new" ? "Raised as a new issue" : e.to_value === "continuation" ? continues : `Raised again (${lab("match", e.to_value)})`) : e.event === "opened" ? "Opened" : words(e.event),
      e.review ? ` · ${e.review}` : "", e.step ? ` · step ${e.step}` : "", e.commit_sha ? [" · ", h("code", null, short(e.commit_sha))] : "", e.actor ? ` · ${e.actor}` : "",
      e.note ? h("div", {class:"note", style:"padding:0;border:0"}, e.note) : null))) : empty("No history recorded."));
};

VIEWS.plans = async () => {
  const list = await api("plans");
  setView(h("h1", null, "Fix plans"),
    table([{label:"Plan", key:"code"}, {label:"Title", key:"title"}, {label:"From reviews", key:"reviews"}, {label:"Status", get:p => lab("plan", p.status)},
           {label:"Progress", get:p => progressBar(p.done || 0, p.doing || 0, p.waiting || 0, p.steps || 0)}], list, p => go("#/plan/" + p.code)));
};

VIEWS.plan = async ([code, , phaseArg]) => {
  const d = await api("plan/" + encodeURIComponent(code));
  const steps = d.steps, phases = d.phase_rows, prog = Object.fromEntries(d.phases.map(p => [p.phase, p]));
  let phase = Number(phaseArg) || (phases[0] && phases[0].number) || 1;
  const count = s => steps.filter(x => x.status === s).length;
  const q = h("input", {type:"search", placeholder:"Search steps, or a point id", "aria-label":"Search steps or point ids"});
  const f = h("select", {"aria-label":"Filter steps"}, [["","All steps"],["st-done","Done"],["st-doing","In progress"],["st-waiting-owner","Waiting on you"],["st-not-started","Not started"],
    ["any","Needs you (any)"],["decision","Needs your decision"],["confirm","Needs you to confirm"],["review","Needs your review"],["production","Production action"],["behavior","Changes behaviour on purpose"]].map(([v,t]) => h("option", {value:v}, t)));
  const nav = h("nav", {class:"rail", "aria-label":"Phases"}), main = h("main", {style:"min-width:0;display:flex;flex-direction:column;gap:8px"});
  const match = s => {
    const t = q.value.trim().toLowerCase(), v = f.value;
    if (v.startsWith("st-") && s.status !== v.slice(3)) return false;
    if (v === "any" && s.owner_need === "none") return false;
    if (["decision","confirm","review","production"].includes(v) && s.owner_need !== v) return false;
    if (v === "behavior" && !s.behavior_change) return false;
    return !t || [s.number, s.title, s.change, s.proof, ...s.closes.map(c => c.point_id + " " + (c.issue || ""))].join(" ").toLowerCase().includes(t);
  };
  const stepItem = s => h("details", {class:"item"},
    h("summary", null, h("span", {class:"n"}, s.number), h("span", {class:"t"}, s.title),
      h("span", {class:"tags"}, s.status !== "not-started" ? h("span", {class:"tag " + ({done:"solid",doing:"ok","waiting-owner":"you"}[s.status] || "")}, lab("step", s.status)) : null,
        s.owner_need !== "none" ? h("span", {class:"tag " + (s.owner_need === "production" ? "crit" : s.owner_need === "decision" ? "warn" : "info")}, lab("need", s.owner_need)) : null,
        s.behavior_change ? h("span", {class:"tag"}, "Changes behaviour") : null)),
    h("div", {class:"body"}, kv([["Change", s.change], ["Proof", s.proof], ["Depends on", s.depends.length ? (s.depends.length === 1 ? "Step " : "Steps ") + s.depends.join(", ") : null], ["Behaviour change", s.behavior_note],
      ["Needs from you", s.owner_question], ["Progress", s.progress_note], ["Commits", s.commits.length ? s.commits.map(c => h("div", null, h("code", null, short(c.commit_sha)), c.summary ? " " + c.summary : "", c.pr_url ? [" ", extLink(c.pr_url, "pull request")] : "")) : null]]),
      s.closes.length ? h("div", {class:"ref"}, h("span", {class:"note"}, `Closes ${s.closes.length} ${s.closes.length === 1 ? "point" : "points"}`),
        s.closes.map(c => h("div", {class:"refline"}, h("code", null, `${c.review} · ${c.point_id}`), sevTag(c.severity), c.issue ? h("a", {href:"#/issue/" + c.issue}, c.issue) : null, h("span", null, c.summary)))) : null));
  const draw = () => {
    const filtering = q.value.trim() || f.value;
    put(nav, ...phases.map(p => { const pr = prog[p.number] || {}; return h("button", {type:"button", "aria-current": String(!filtering && p.number === phase),
      onclick:() => { phase = p.number; q.value = ""; f.value = ""; history.replaceState(null, "", `#/plan/${code}/phase/${phase}`); draw(); }},
      h("span", {class:"i"}, p.number), h("span", null, p.title), h("span", {class:"c"}, `${pr.done || 0}/${pr.steps || 0}`)); }));
    if (filtering) {
      const hit = steps.filter(match);
      put(main, h("h2", null, `${hit.length} matching ${hit.length === 1 ? "step" : "steps"} across all phases`), hit.length ? hit.slice(0, 300).map(stepItem) : empty("No step matches."));
    } else {
      const p = phases.find(x => x.number === phase) || phases[0];
      const ps = steps.filter(s => s.phase === p.number);
      const sections = [...new Set(ps.map(s => s.section || ""))];
      put(main, h("h2", null, `Phase ${p.number} · ${p.title}`), h("p", {class:"lede"}, p.goal), p.why ? h("p", {class:"note"}, p.why) : null,
        sections.map(sec => [sec ? h("h3", null, sec) : null, ps.filter(s => (s.section || "") === sec).map(stepItem)]));
    }
  };
  q.addEventListener("input", draw); f.addEventListener("change", draw);
  const pl = d.plan;
  setView(h("h1", null, `${pl.code} · ${pl.title}`),
    h("p", {class:"lede"}, lab("plan", pl.status) + (pl.approved_at ? ` · approved ${when(pl.approved_at)}` : "")),
    h("div", {class:"facts"}, h("div", null, h("b", null, steps.length), h("span", null, "steps")), h("div", null, h("b", null, phases.length), h("span", null, "phases")),
      h("div", null, h("b", null, new Set(steps.flatMap(s => s.closes.map(c => c.review + c.point_id))).size), h("span", null, "points closed"))),
    progressBar(count("done"), count("doing"), count("waiting-owner"), steps.length),
    pl.narrative_md ? h("div", {class:"card"}, h("h2", null, "About this plan"), h("div", {class:"mdtext"}, pl.narrative_md)) : null,
    h("div", {class:"tools"}, q, f), h("div", {class:"cols"}, nav, main));
  draw();
};

function lineChart(points, unit){
  const W = 520, H = 160, P = 30;
  if (!points.length) return empty("No values.");
  const vs = points.map(p => p.value), lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1;
  const x = i => P + (points.length === 1 ? (W - 2*P) / 2 : i * (W - 2*P) / (points.length - 1)), y = v => H - P - (v - lo) * (H - 2*P) / span;
  const ns = "http://www.w3.org/2000/svg", el = (t, a) => { const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, role:"img", "aria-label":"Values over reviews"});
  svg.append(el("line", {x1:P, x2:W-P, y1:H-P, y2:H-P, stroke:"var(--line)"}));
  svg.append(el("polyline", {points:points.map((p,i) => `${x(i)},${y(p.value)}`).join(" "), fill:"none", stroke:"var(--accent)", "stroke-width":2}));
  points.forEach((p,i) => {
    svg.append(el("circle", {cx:x(i), cy:y(p.value), r:3.5, fill:"var(--accent)"}));
    const t1 = el("text", {x:x(i), y:y(p.value) - 8, "text-anchor":"middle", "font-size":11, fill:"var(--fg)"}); t1.textContent = `${+p.value.toFixed(2)}${unit ? " " + unit : ""}`; svg.append(t1);
    const t2 = el("text", {x:x(i), y:H - 10, "text-anchor":"middle", "font-size":11, fill:"var(--muted)"}); t2.textContent = p.review; svg.append(t2);
  });
  return h("div", {class:"chart"}, svg);
}

VIEWS.trends = async () => {
  const d = await api("trends");
  const groups = {};
  for (const m of d.metrics) (groups[`${m.name}|${m.scope}|${m.repository}`] ||= []).push(m);
  setView(h("h1", null, "Over time"), h("p", {class:"lede"}, "How the project has moved between reviews."),
    h("h3", null, "Points per review"),
    table([{label:"Review", key:"review"}, {label:"Started", get:r => when(r.started_at, false)}, {label:"Points", key:"points", num:1}, {label:"High", key:"high", num:1}, {label:"Medium", key:"medium", num:1}, {label:"Low", key:"low", num:1},
           {label:"New", key:"new_issues", num:1}, {label:"Carried over", key:"carried_over", num:1}, {label:"Came back", key:"came_back", num:1}, {label:"False", key:"false_findings", num:1}], d.reviews, r => go("#/review/" + r.review)),
    h("h3", null, "Measurements"),
    Object.keys(groups).length ? h("div", {class:"grid2"}, Object.entries(groups).map(([k, pts]) => { const [n, sc, repo] = k.split("|");
      const better = {higher:"higher is better", lower:"lower is better"}[pts[0].better];
      return h("div", {class:"card"}, h("b", null, pts[0].label || words(n)), h("div", {class:"note"}, [[sc !== "all" ? sc : "", repo !== "all" ? repo : ""].filter(Boolean).join(" · ") || "Whole system", better].filter(Boolean).join(" · ")), lineChart(pts, pts[0].unit)); }))
      : empty("No measurements recorded yet."),
    h("h3", null, "Issues that keep coming back"),
    table([{label:"Issue", key:"code"}, {label:"Title", key:"title"}, {label:"Severity", get:i => sevTag(i.severity)}, {label:"Status", get:i => issueTag(i.status)}, {label:"Reviews", key:"times_seen", num:1}, {label:"Came back after a fix", key:"regressions", num:1}, {label:"Raised in", key:"reviews"}],
          d.recurring, i => go("#/issue/" + i.code)),
    h("h3", null, "Files that keep producing issues"),
    table([{label:"File", get:r => h("code", null, r.path)}, {label:"Issues", key:"issues", num:1}, {label:"Reviews", key:"reviews", num:1}, {label:"High points", key:"high_points", num:1}], d.hotspots,
          r => go("#/issues?q=" + encodeURIComponent(r.path))));
};

const cellText = v => v && typeof v === "object" && "blob_bytes" in v ? `<${v.blob_bytes} bytes>` : String(v);
VIEWS.explore = async ([name]) => {
  const tables = await api("tables");
  const list = h("div", {class:"tlist"}), right = h("div", {style:"min-width:0;display:flex;flex-direction:column;gap:10px"});
  const grid = (cols, data) => data.length ? h("div", {class:"scroll card grid", style:"padding:0"}, h("table", null, h("thead", null, h("tr", null, cols.map(c => h("th", null, c)))),
    h("tbody", null, data.map(r => h("tr", null, r.map(v => h("td", {class: typeof v === "number" ? "num mono" : null, title: v == null ? null : cellText(v).slice(0, 2000)}, v == null ? h("span", {class:"muted"}, "—") : cellText(v)))))))) : empty("No rows.");
  const showTable = async (t, offset=0) => {
    list.querySelectorAll("button").forEach(b => b.setAttribute("aria-current", String(b.dataset.n === t)));
    history.replaceState(null, "", "#/explore/" + t);
    try {
      const d = await api(`table/${t}?limit=100&offset=${offset}`);
      put(right, h("h2", null, t), h("p", {class:"note"}, `${d.count} rows` + (d.count > d.limit ? ` · showing ${offset + 1}–${Math.min(d.count, offset + d.limit)}` : "")),
        h("div", {class:"tools"}, offset > 0 ? h("button", {class:"btn ghost", type:"button", onclick:() => showTable(t, Math.max(0, offset - 100))}, "Previous") : null,
          offset + d.limit < d.count ? h("button", {class:"btn ghost", type:"button", onclick:() => showTable(t, offset + 100)}, "Next") : null,
          h("button", {class:"btn ghost", type:"button", onclick:() => { sql.value = `SELECT * FROM ${t} LIMIT 50`; sql.focus(); }}, "Query this")),
        grid(d.columns, d.rows));
    } catch (e) { put(right, h("p", {class:"status err"}, e.message)); }
  };
  const sec = (title, type) => [h("h3", null, title), tables.filter(t => t.type === type).map(t => h("button", {type:"button", "data-n":t.name, onclick:() => showTable(t.name)}, h("span", null, t.name), h("span", {class:"muted"}, t.count)))];
  add(list, sec("Saved views", "view"), sec("Tables", "table"));
  const sql = h("textarea", {class:"mono", "aria-label":"SQL query", placeholder:"SELECT code, title, status FROM issues WHERE severity = 'H'"});
  const out = h("div");
  const run = async () => {
    try { const d = await api("sql", {sql: sql.value}); put(out, h("p", {class:"note"}, `${d.rows.length} rows${d.truncated ? ` (first ${d.rows.length.toLocaleString("en")} shown)` : ""}`), grid(d.columns, d.rows)); }
    catch (e) { put(out, h("p", {class:"status err"}, e.message)); }
  };
  sql.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run(); });
  setView(h("h1", null, "Explore the ledger"),
    h("p", {class:"lede"}, "Browse every table and saved view, or run your own read-only query. Nothing here can change the ledger."),
    h("div", {class:"card", style:"display:flex;flex-direction:column;gap:8px"}, h("b", null, "Query"), sql, h("div", {class:"tools"}, h("button", {class:"btn", type:"button", onclick:run}, "Run query"), h("span", {class:"note"}, "Ctrl or ⌘ + Enter runs it")), out),
    h("div", {class:"split"}, list, right));
  if (name && tables.some(t => t.name === name)) showTable(name); else add(right, empty("Choose a table or view on the left."));
};

// ---------- routing
const TABS = [["home","#/","Overview"],["inbox","#/inbox","Waiting on you"],["reviews","#/reviews","Reviews"],["issues","#/issues","Issues"],["plans","#/plans","Fix plans"],["trends","#/trends","Over time"],["explore","#/explore","Explore"]];
function drawTabs(active){
  const n = OVERVIEW ? OVERVIEW.queue : 0;
  put(document.getElementById("tabs"), ...TABS.map(([k, href, t]) => h("a", {href, "aria-current": active === k ? "page" : null}, t, k === "inbox" && n ? h("span", {class:"badge"}, n) : null)));
}
async function route(){
  takeToken();
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, query] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const name = parts[0] || "home";
  const view = VIEWS[name] || VIEWS.home;
  const tab = {review:"reviews", issue:"issues", plan:"plans", history:"inbox"}[name] || (VIEWS[name] ? name : "home");
  try {
    if (!OVERVIEW || name === "home" || name === "inbox") { OVERVIEW = await api("overview"); LABELS = OVERVIEW.labels; setLabels(LABELS.enums);
      put(document.getElementById("brand"), OVERVIEW.project, h("small", null, "review ledger")); document.title = OVERVIEW.project + " · review ledger"; }
    drawTabs(tab);
    await view(parts.slice(1), new URLSearchParams(query || ""));
  } catch (e) { drawTabs(tab); failed(e); }
}
window.addEventListener("hashchange", route);

// ---------- theme
(() => {
  const btn = document.getElementById("theme"), order = ["auto","light","dark"];
  let t = "auto"; try { t = localStorage.getItem("ledger-theme") || "auto"; } catch {}
  const apply = () => { if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t); btn.textContent = "Theme: " + t; };
  btn.addEventListener("click", () => { t = order[(order.indexOf(t) + 1) % 3]; try { localStorage.setItem("ledger-theme", t); } catch {} apply(); });
  apply();
})();
route();
</script>
</body>
</html>
"""
MIGRATION_FILES = r"""-- migration: 0001-initial.sql
-- initial ledger schema
-- Baseline. Schema 1 is references/schema.sql as it was at commit ea00196 (frozen in dev/tests/data/schema-v1.sql):
-- every ledger made before migrations existed is schema 1, and a new ledger is made from references/schema.sql
-- directly, so this file is never applied. Each later file NNNN-<slug>.sql takes a ledger from schema NNNN-1 to
-- NNNN; see "Changing the schema" in README.md.
-- migration: 0002-checks-and-columns.sql
-- url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted
-- Plan P-0001 steps 44-47 and 51. Ledger links are http(s) only (a javascript: or other link is not data worth
-- keeping: it is set to NULL here, and fill-refs writes permalinks again from permalink_template); repository and
-- location paths, commit hashes and review codes get CHECKs, so a row that breaks one makes this migration fail
-- and change nothing (fix the row, then run migrate again). Five tables are rebuilt with SQLite's recipe: create
-- <table>_new, copy the rows, drop the old table, rename the new one, re-create its indexes.

UPDATE repositories SET permalink_template = NULL
  WHERE permalink_template IS NOT NULL AND permalink_template NOT GLOB 'https://*' AND permalink_template NOT GLOB 'http://*';
UPDATE reviews SET summary_doc_url = NULL
  WHERE summary_doc_url IS NOT NULL AND summary_doc_url NOT GLOB 'https://*' AND summary_doc_url NOT GLOB 'http://*';
UPDATE finding_locations SET permalink = NULL
  WHERE permalink IS NOT NULL AND permalink NOT GLOB 'https://*' AND permalink NOT GLOB 'http://*';
UPDATE step_commits SET pr_url = NULL
  WHERE pr_url IS NOT NULL AND pr_url NOT GLOB 'https://*' AND pr_url NOT GLOB 'http://*';

CREATE TABLE repositories_new (
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
INSERT INTO repositories_new(id, name, path, remote_url, default_branch, permalink_template, notes, created_at)
  SELECT id, name, path, remote_url, default_branch, permalink_template, notes, created_at FROM repositories;
DROP TABLE repositories;
ALTER TABLE repositories_new RENAME TO repositories;

CREATE TABLE reviews_new (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE    -- R-0001
                          CHECK (code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'R-*[^0-9]*'),
  title                 TEXT NOT NULL,
  scope                 TEXT NOT NULL CHECK (scope IN
                          ('full','partial','targeted','second','fix-verification')),
  status                TEXT NOT NULL DEFAULT 'planning' CHECK (status IN
                          ('planning','awaiting-approval','approved','running','verifying',
                           'indexing','consolidating','handed-off','abandoned')),
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
  completed_at          TEXT,
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO reviews_new(id, code, title, scope, status, request_text, requested_by_actor_id, manager_actor_id, approved_by_actor_id, approved_at, previous_review_id, skill_version, review_folder, summary_doc_url, verdict, summary, started_at, completed_at, updated_at)
  SELECT id, code, title, scope, status, request_text, requested_by_actor_id, manager_actor_id, approved_by_actor_id, approved_at, previous_review_id, skill_version, review_folder, summary_doc_url, verdict, summary, started_at, completed_at, updated_at FROM reviews;
DROP TABLE reviews;
ALTER TABLE reviews_new RENAME TO reviews;

CREATE TABLE review_repositories_new (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                   AND commit_sha NOT GLOB '*[^0-9a-f]*'),  -- full hash, lowercase hex
  branch        TEXT,
  commit_time   TEXT,
  commit_subject TEXT,
  tag_name      TEXT,                     -- e.g. review/R-0001, keeps the commit reachable
  tag_pushed    INTEGER NOT NULL DEFAULT 0 CHECK (tag_pushed IN (0,1)),
  is_dirty      INTEGER NOT NULL DEFAULT 0 CHECK (is_dirty IN (0,1)),
  dirty_note    TEXT,                     -- what was uncommitted, if anything
  dirty_patch_path TEXT,                  -- saved `git diff` of uncommitted changes, if any
  PRIMARY KEY (review_id, repository_id)
);
INSERT INTO review_repositories_new(review_id, repository_id, commit_sha, branch, commit_time, commit_subject, tag_name, tag_pushed, is_dirty, dirty_note, dirty_patch_path)
  SELECT review_id, repository_id, commit_sha, branch, commit_time, commit_subject, tag_name, tag_pushed, is_dirty, dirty_note, dirty_patch_path FROM review_repositories;
DROP TABLE review_repositories;
ALTER TABLE review_repositories_new RENAME TO review_repositories;

CREATE TABLE finding_locations_new (
  id             INTEGER PRIMARY KEY,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  repository_id  INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR (length(commit_sha) >= 7 AND commit_sha NOT GLOB '*[^0-9a-f]*')),
                                            -- filled from review_repositories if left NULL
  path           TEXT NOT NULL,             -- repo-relative, forward slashes (see the CHECK below)
  line_start     INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end       INTEGER,
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
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);
INSERT INTO finding_locations_new(id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role)
  SELECT id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role FROM finding_locations;
DROP TABLE finding_locations;
ALTER TABLE finding_locations_new RENAME TO finding_locations;
CREATE INDEX ix_locations_finding    ON finding_locations(finding_id);
CREATE INDEX ix_locations_path       ON finding_locations(path, symbol);

CREATE TABLE step_commits_new (
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
INSERT INTO step_commits_new(id, step_id, repository_id, commit_sha, branch, pr_url, summary, committed_at)
  SELECT id, step_id, repository_id, commit_sha, branch, pr_url, summary, committed_at FROM step_commits;
DROP TABLE step_commits;
ALTER TABLE step_commits_new RENAME TO step_commits;
-- migration: 0003-integrity.sql
-- integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers
-- Plan P-0001 steps 54-64, 70-74, 79-83. Fourteen tables are rebuilt with SQLite's recipe (create <table>_new,
-- copy the rows, drop the old table, rename the new one, re-create its indexes) to add CHECKs and NOT NULLs:
-- code formats, full lowercase commit hashes on every commit column, issue domain and kind required,
-- times_seen >= 1, non-negative run counts, a match type only with its issue, no line_end without line_start,
-- answerable gates with an array of options, answered gates with an answer and applied gates with a note,
-- decided decisions with an answer, consistent supersede columns, approved reviews and plans with approved_at.
-- New columns: reviews/plans change_actor_id and change_note, gates answered_by_actor_id and change_actor_id,
-- steps change_note. A row that breaks a new rule makes this migration fail and change nothing; the error
-- names the rule (fix the row, then run migrate again). The rules that triggers enforce (sequence of codes,
-- transitions, append-only history, events) are in references/schema.sql: migrate re-creates every trigger.

CREATE TABLE design_goals_new (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE
                  CHECK (code GLOB 'G-[0-9][0-9]*' AND code NOT GLOB 'G-*[^0-9]*'),
  text          TEXT NOT NULL,
  source        TEXT,
  adopted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  retired_at    TEXT,
  superseded_by INTEGER REFERENCES design_goals(id),
  CHECK (superseded_by IS NULL OR retired_at IS NOT NULL)
);
INSERT INTO design_goals_new(id, code, text, source, adopted_at, retired_at, superseded_by)
  SELECT id, code, text, source, adopted_at, retired_at, superseded_by FROM design_goals;
DROP TABLE design_goals;
ALTER TABLE design_goals_new RENAME TO design_goals;

CREATE TABLE reviews_new (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE
                          CHECK (code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'R-*[^0-9]*'),
  title                 TEXT NOT NULL,
  scope                 TEXT NOT NULL CHECK (scope IN
                          ('full','partial','targeted','second','fix-verification')),
  status                TEXT NOT NULL DEFAULT 'planning' CHECK (status IN
                          ('planning','awaiting-approval','approved','running','verifying',
                           'indexing','consolidating','handed-off','abandoned'))
                          CHECK (status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NOT NULL),
  request_text          TEXT,
  requested_by_actor_id INTEGER REFERENCES actors(id),
  manager_actor_id      INTEGER REFERENCES actors(id),
  approved_by_actor_id  INTEGER REFERENCES actors(id),
  approved_at           TEXT,
  previous_review_id    INTEGER REFERENCES reviews(id),
  skill_version         TEXT,
  review_folder         TEXT,
  summary_doc_url       TEXT CHECK (summary_doc_url IS NULL OR summary_doc_url GLOB 'https://*'
                                    OR summary_doc_url GLOB 'http://*'),
  verdict               TEXT,
  summary               TEXT,
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at          TEXT CHECK (completed_at IS NULL OR julianday(started_at) <= julianday(completed_at)),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id       INTEGER REFERENCES actors(id),
  change_note           TEXT
);
INSERT INTO reviews_new(id, code, title, scope, status, request_text, requested_by_actor_id, manager_actor_id, approved_by_actor_id, approved_at, previous_review_id, skill_version, review_folder, summary_doc_url, verdict, summary, started_at, completed_at, updated_at)
  SELECT id, code, title, scope, status, request_text, requested_by_actor_id, manager_actor_id, approved_by_actor_id, approved_at, previous_review_id, skill_version, review_folder, summary_doc_url, verdict, summary, started_at, completed_at, updated_at FROM reviews;
DROP TABLE reviews;
ALTER TABLE reviews_new RENAME TO reviews;

CREATE TABLE reviewer_runs_new (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  domain_code    TEXT NOT NULL REFERENCES domains(code),
  actor_id       INTEGER NOT NULL REFERENCES actors(id),
  label          TEXT NOT NULL,
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
INSERT INTO reviewer_runs_new(id, review_id, domain_code, actor_id, label, scope, prompt, report_path, report_sha256, status, items_reviewed, high_count, medium_count, low_count, test_count, final_message, started_at, finished_at)
  SELECT id, review_id, domain_code, actor_id, label, scope, prompt, report_path, report_sha256, status, items_reviewed, high_count, medium_count, low_count, test_count, final_message, started_at, finished_at FROM reviewer_runs;
DROP TABLE reviewer_runs;
ALTER TABLE reviewer_runs_new RENAME TO reviewer_runs;
CREATE INDEX ix_runs_review          ON reviewer_runs(review_id);

CREATE TABLE requirements_new (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE
                    CHECK (code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'REQ-*[^0-9]*'),
  text            TEXT NOT NULL,
  source          TEXT NOT NULL,
  source_location TEXT,
  stated_at       TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','withdrawn')),
  superseded_by   INTEGER REFERENCES requirements(id)
                    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
  first_review_id INTEGER REFERENCES reviews(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO requirements_new(id, code, text, source, source_location, stated_at, status, superseded_by, first_review_id, created_at)
  SELECT id, code, text, source, source_location, stated_at, status, superseded_by, first_review_id, created_at FROM requirements;
DROP TABLE requirements;
ALTER TABLE requirements_new RENAME TO requirements;

CREATE TABLE issues_new (
  id                     INTEGER PRIMARY KEY,
  code                   TEXT NOT NULL UNIQUE
                           CHECK (code GLOB 'ISS-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'ISS-*[^0-9]*'),
  title                  TEXT NOT NULL,
  domain_code            TEXT NOT NULL REFERENCES domains(code),
  theme_code             TEXT REFERENCES themes(code),
  sub                    TEXT,
  kind                   TEXT NOT NULL CHECK (kind IN
                           ('defect','test','validation','refactor','feature','design','docs',
                            'logging','perf','security','ops','verify')),
  severity               TEXT NOT NULL CHECK (severity IN ('H','M','L','T')),
  status                 TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                           ('open','needs-decision','planned','in-progress','fixed','verified',
                            'regressed','accepted-risk','wont-fix','duplicate','obsolete')),
  fingerprint            TEXT,
  before_state           TEXT,
  current_state          TEXT,
  after_state            TEXT,
  recommendation         TEXT,
  resolution             TEXT,
  first_seen_review_id   INTEGER NOT NULL REFERENCES reviews(id),
  last_seen_review_id    INTEGER REFERENCES reviews(id),
  last_checked_review_id INTEGER REFERENCES reviews(id),
  times_seen             INTEGER NOT NULL DEFAULT 1 CHECK (times_seen >= 1),
  opened_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at            TEXT,
  verified_at            TEXT,
  updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id        INTEGER REFERENCES actors(id),
  change_review_id       INTEGER REFERENCES reviews(id),
  change_step_id         INTEGER REFERENCES steps(id),
  change_commit_sha      TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40
                                     OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note            TEXT
);
INSERT INTO issues_new(id, code, title, domain_code, theme_code, sub, kind, severity, status, fingerprint, before_state, current_state, after_state, recommendation, resolution, first_seen_review_id, last_seen_review_id, last_checked_review_id, times_seen, opened_at, resolved_at, verified_at, updated_at, change_actor_id, change_review_id, change_step_id, change_commit_sha, change_note)
  SELECT id, code, title, domain_code, theme_code, sub, kind, severity, status, fingerprint, before_state, current_state, after_state, recommendation, resolution, first_seen_review_id, last_seen_review_id, last_checked_review_id, times_seen, opened_at, resolved_at, verified_at, updated_at, change_actor_id, change_review_id, change_step_id, change_commit_sha, change_note FROM issues;
DROP TABLE issues;
ALTER TABLE issues_new RENAME TO issues;
CREATE INDEX ix_issues_fingerprint   ON issues(fingerprint);
CREATE INDEX ix_issues_slug          ON issues(domain_code, theme_code, sub);
CREATE INDEX ix_issues_status        ON issues(status);

CREATE TABLE findings_new (
  id                   INTEGER PRIMARY KEY,
  review_id            INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  reviewer_run_id      INTEGER REFERENCES reviewer_runs(id),
  point_id             TEXT NOT NULL,
  issue_id             INTEGER REFERENCES issues(id),
  match_type           TEXT CHECK (match_type IN
                         ('new','still-present','changed','recurrence','regression','continuation'))
                         CHECK (match_type IS NULL OR issue_id IS NOT NULL),
  match_confidence     TEXT CHECK (match_confidence IN ('certain','likely','possible')),
  match_rationale      TEXT,
  severity             TEXT NOT NULL CHECK (severity IN ('H','M','L','T')),
  kind                 TEXT NOT NULL CHECK (kind IN
                         ('defect','test','validation','refactor','feature','design','docs',
                          'logging','perf','security','ops','verify')),
  domain_code          TEXT NOT NULL REFERENCES domains(code),
  theme_code           TEXT REFERENCES themes(code),
  sub                  TEXT NOT NULL,
  summary              TEXT NOT NULL,
  detail               TEXT,
  before_state         TEXT,
  recommendation       TEXT,
  recommended_test     TEXT,
  evidence             TEXT,
  where_report         TEXT,
  fingerprint          TEXT,
  related_point_ids    TEXT CHECK (related_point_ids IS NULL OR json_valid(related_point_ids)),
  verification         TEXT NOT NULL DEFAULT 'unverified' CHECK (verification IN
                         ('unverified','confirmed','partly','false','not-checkable')),
  verified_by_actor_id INTEGER REFERENCES actors(id),
  verified_at          TEXT,
  verification_note    TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (review_id, point_id)
);
INSERT INTO findings_new(id, review_id, reviewer_run_id, point_id, issue_id, match_type, match_confidence, match_rationale, severity, kind, domain_code, theme_code, sub, summary, detail, before_state, recommendation, recommended_test, evidence, where_report, fingerprint, related_point_ids, verification, verified_by_actor_id, verified_at, verification_note, created_at)
  SELECT id, review_id, reviewer_run_id, point_id, issue_id, match_type, match_confidence, match_rationale, severity, kind, domain_code, theme_code, sub, summary, detail, before_state, recommendation, recommended_test, evidence, where_report, fingerprint, related_point_ids, verification, verified_by_actor_id, verified_at, verification_note, created_at FROM findings;
DROP TABLE findings;
ALTER TABLE findings_new RENAME TO findings;
CREATE INDEX ix_findings_fingerprint ON findings(fingerprint);
CREATE INDEX ix_findings_issue       ON findings(issue_id);
CREATE INDEX ix_findings_review      ON findings(review_id);
CREATE INDEX ix_findings_slug        ON findings(domain_code, theme_code, sub);

CREATE TABLE finding_locations_new (
  id             INTEGER PRIMARY KEY,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  repository_id  INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                                    AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  path           TEXT NOT NULL,
  line_start     INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end       INTEGER CHECK (line_end IS NULL OR line_start IS NOT NULL),
  symbol         TEXT,
  blob_sha       TEXT,
  excerpt        TEXT,
  excerpt_sha256 TEXT,
  permalink      TEXT CHECK (permalink IS NULL OR permalink GLOB 'https://*' OR permalink GLOB 'http://*'),
  role           TEXT NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','also','test','doc','config')),
  excerpt_redacted INTEGER NOT NULL DEFAULT 0 CHECK (excerpt_redacted IN (0,1)),
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);
INSERT INTO finding_locations_new(id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role, excerpt_redacted)
  SELECT id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role, excerpt_redacted FROM finding_locations;
DROP TABLE finding_locations;
ALTER TABLE finding_locations_new RENAME TO finding_locations;
CREATE INDEX ix_locations_finding    ON finding_locations(finding_id);
CREATE INDEX ix_locations_path       ON finding_locations(path, symbol);

CREATE TABLE decisions_new (
  id                  INTEGER PRIMARY KEY,
  code                TEXT NOT NULL UNIQUE
                        CHECK (code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'D-*[^0-9]*'),
  question            TEXT NOT NULL,
  options             TEXT CHECK (options IS NULL OR json_valid(options)),
  answer              TEXT,
  rationale           TEXT,
  status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','revisit'))
                        CHECK (status <> 'decided' OR answer IS NOT NULL),
  revisit_when        TEXT,
  review_id           INTEGER REFERENCES reviews(id),
  step_id             INTEGER REFERENCES steps(id),
  asked_by_actor_id   INTEGER REFERENCES actors(id),
  decided_by_actor_id INTEGER REFERENCES actors(id),
  asked_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at          TEXT
);
INSERT INTO decisions_new(id, code, question, options, answer, rationale, status, revisit_when, review_id, step_id, asked_by_actor_id, decided_by_actor_id, asked_at, decided_at)
  SELECT id, code, question, options, answer, rationale, status, revisit_when, review_id, step_id, asked_by_actor_id, decided_by_actor_id, asked_at, decided_at FROM decisions;
DROP TABLE decisions;
ALTER TABLE decisions_new RENAME TO decisions;

CREATE TABLE gates_new (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE
                    CHECK (code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'GATE-*[^0-9]*'),
  kind            TEXT NOT NULL CHECK (kind IN
                    ('domain-approval','plan-approval','decision','step-input',
                     'confirm-false-finding','accept-risk','production-action',
                     'review-handoff','confirm','other')),
  title           TEXT NOT NULL,
  question        TEXT NOT NULL,
  context         TEXT,
  options         TEXT CHECK (options IS NULL OR json_valid(options))
                    CHECK (options IS NULL OR json_type(options) = 'array'),
  allow_text      INTEGER NOT NULL DEFAULT 1 CHECK (allow_text IN (0,1))
                    CHECK (allow_text = 1 OR (options IS NOT NULL AND json_array_length(options) > 0)),
  blocking        INTEGER NOT NULL DEFAULT 1 CHECK (blocking IN (0,1)),
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','applied','withdrawn'))
                    CHECK (status <> 'answered' OR answer_value IS NOT NULL OR answer_text IS NOT NULL)
                    CHECK (status <> 'applied' OR applied_note IS NOT NULL),
  answer_value    TEXT,
  answer_text     TEXT,
  answered_by     TEXT,
  answered_via    TEXT CHECK (answered_via IS NULL OR answered_via IN ('chat','viewer','other')),
  answered_at     TEXT,
  applied_note    TEXT,
  applied_at      TEXT,
  review_id       INTEGER REFERENCES reviews(id),
  plan_id         INTEGER REFERENCES plans(id),
  step_id         INTEGER REFERENCES steps(id),
  issue_id        INTEGER REFERENCES issues(id),
  decision_id     INTEGER REFERENCES decisions(id),
  opened_by_actor_id INTEGER REFERENCES actors(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  answered_by_actor_id INTEGER REFERENCES actors(id),
  change_actor_id INTEGER REFERENCES actors(id)
);
INSERT INTO gates_new(id, code, kind, title, question, context, options, allow_text, blocking, status, answer_value, answer_text, answered_by, answered_via, answered_at, applied_note, applied_at, review_id, plan_id, step_id, issue_id, decision_id, opened_by_actor_id, created_at)
  SELECT id, code, kind, title, question, context, options, allow_text, blocking, status, answer_value, answer_text, answered_by, answered_via, answered_at, applied_note, applied_at, review_id, plan_id, step_id, issue_id, decision_id, opened_by_actor_id, created_at FROM gates;
DROP TABLE gates;
ALTER TABLE gates_new RENAME TO gates;
CREATE INDEX ix_gates_status         ON gates(status);

CREATE TABLE plans_new (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE
                         CHECK (code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'P-*[^0-9]*'),
  title                TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'drafting' CHECK (status IN
                         ('drafting','awaiting-approval','approved','in-progress','complete',
                          'superseded','abandoned'))
                         CHECK (status IN ('drafting','awaiting-approval','superseded','abandoned')
                                OR approved_at IS NOT NULL),
  approved_by_actor_id INTEGER REFERENCES actors(id),
  approved_at          TEXT,
  superseded_by        INTEGER REFERENCES plans(id),
  doc_url              TEXT,
  page_url             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id      INTEGER REFERENCES actors(id),
  change_note          TEXT
);
INSERT INTO plans_new(id, code, title, status, approved_by_actor_id, approved_at, superseded_by, doc_url, page_url, created_at, updated_at)
  SELECT id, code, title, status, approved_by_actor_id, approved_at, superseded_by, doc_url, page_url, created_at, updated_at FROM plans;
DROP TABLE plans;
ALTER TABLE plans_new RENAME TO plans;

CREATE TABLE steps_new (
  id                INTEGER PRIMARY KEY,
  plan_id           INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  phase_id          INTEGER NOT NULL REFERENCES phases(id),
  number            INTEGER NOT NULL,
  section           TEXT,
  title             TEXT NOT NULL,
  change            TEXT NOT NULL,
  proof             TEXT NOT NULL,
  owner_need        TEXT NOT NULL DEFAULT 'none' CHECK (owner_need IN
                      ('none','decision','confirm','review','production')),
  owner_question    TEXT,
  behavior_change   INTEGER NOT NULL DEFAULT 0 CHECK (behavior_change IN (0,1)),
  behavior_note     TEXT,
  status            TEXT NOT NULL DEFAULT 'not-started' CHECK (status IN
                      ('not-started','doing','waiting-owner','done','dropped')),
  progress_note     TEXT,
  drafter_key       TEXT,
  started_at        TEXT,
  done_at           TEXT,
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id   INTEGER REFERENCES actors(id),
  change_commit_sha TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40 OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note       TEXT,
  UNIQUE (plan_id, number)
);
INSERT INTO steps_new(id, plan_id, phase_id, number, section, title, change, proof, owner_need, owner_question, behavior_change, behavior_note, status, progress_note, drafter_key, started_at, done_at, updated_at, change_actor_id, change_commit_sha)
  SELECT id, plan_id, phase_id, number, section, title, change, proof, owner_need, owner_question, behavior_change, behavior_note, status, progress_note, drafter_key, started_at, done_at, updated_at, change_actor_id, change_commit_sha FROM steps;
DROP TABLE steps;
ALTER TABLE steps_new RENAME TO steps;
CREATE INDEX ix_steps_phase          ON steps(phase_id);
CREATE INDEX ix_steps_status         ON steps(plan_id, status);

CREATE TABLE guard_tests_new (
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
INSERT INTO guard_tests_new(id, issue_id, repository_id, test_path, test_name, step_id, added_commit_sha, failed_before_fix, passes_after_fix, still_exists, last_confirmed_review_id, created_at)
  SELECT id, issue_id, repository_id, test_path, test_name, step_id, added_commit_sha, failed_before_fix, passes_after_fix, still_exists, last_confirmed_review_id, created_at FROM guard_tests;
DROP TABLE guard_tests;
ALTER TABLE guard_tests_new RENAME TO guard_tests;
CREATE INDEX ix_guard_issue          ON guard_tests(issue_id);

CREATE TABLE test_runs_new (
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
INSERT INTO test_runs_new(id, review_id, step_id, repository_id, commit_sha, command, purpose, passed, failed, skipped, errored, exit_code, duration_ms, output_path, notes, started_at)
  SELECT id, review_id, step_id, repository_id, commit_sha, command, purpose, passed, failed, skipped, errored, exit_code, duration_ms, output_path, notes, started_at FROM test_runs;
DROP TABLE test_runs;
ALTER TABLE test_runs_new RENAME TO test_runs;

CREATE TABLE events_new (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('review','issue','step','plan','decision','finding','gate',
                                                   'relation','check','step-finding')),
  entity_id   INTEGER NOT NULL,
  event       TEXT NOT NULL,
  from_value  TEXT,
  to_value    TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  review_id   INTEGER REFERENCES reviews(id),
  step_id     INTEGER REFERENCES steps(id),
  commit_sha  TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64) AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  note        TEXT,
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO events_new(id, entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, commit_sha, note, at)
  SELECT id, entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, commit_sha, note, at FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);

-- Who answered: viewer answers were stored as the text 'owner'; point them at the project's owner actor.
UPDATE gates SET answered_by_actor_id = (SELECT min(id) FROM actors WHERE kind = 'owner')
  WHERE answered_by = 'owner' AND answered_by_actor_id IS NULL;

CREATE UNIQUE INDEX ux_metrics_key            ON metrics(review_id, name, scope, COALESCE(repository_id, 0));
CREATE UNIQUE INDEX ux_locations_one_primary  ON finding_locations(finding_id) WHERE role = 'primary';
CREATE UNIQUE INDEX ux_repositories_path      ON repositories(path);
CREATE INDEX ix_fk_reviews_change_actor_id    ON reviews(change_actor_id);
CREATE INDEX ix_fk_plans_change_actor_id      ON plans(change_actor_id);
CREATE INDEX ix_fk_gates_change_actor_id      ON gates(change_actor_id);
CREATE INDEX ix_fk_gates_answered_by_actor_id ON gates(answered_by_actor_id);
-- migration: 0004-views.sql
-- check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes
-- Plan P-0001 steps 66, 69, 85-91, 93 and 94. New tables: ledger_checks (the checks `ledger.py check` runs, with an
-- error or warning tier), review_actors (every non-reviewer agent of a review), review_requirement_checks (the
-- requirement trace) and metric_defs (metric labels, units, direction; rows added for names already in metrics).
-- domains.description is filled from SKILL.md's "applies when" lines where it is empty, and every foreign-key
-- column gets a leading index. Views and triggers need nothing here: migrate drops them all and re-creates the
-- shipped ones after the last migration.

CREATE TABLE ledger_checks (
  view     TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  tier     TEXT NOT NULL CHECK (tier IN ('error','warning')),
  position INTEGER NOT NULL UNIQUE
);

CREATE TABLE review_actors (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);

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

CREATE TABLE metric_defs (
  name   TEXT PRIMARY KEY,                 -- the stable key used in metrics.name
  label  TEXT NOT NULL,
  unit   TEXT,
  better TEXT NOT NULL DEFAULT 'none' CHECK (better IN ('higher','lower','none'))
);

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

-- Metric names already recorded get a catalog row too (label = the name with spaces, capitalized; a unit seen).
INSERT OR IGNORE INTO metric_defs(name, label, unit)
  SELECT name, upper(substr(name, 1, 1)) || replace(substr(name, 2), '_', ' '), min(unit)
    FROM metrics GROUP BY name;
-- migration: 0005-records.sql
-- one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries
-- Plan P-0001 steps 75, 81, 96 and 97 (step 84's view change and the time triggers come with the shipped views and
-- triggers, which migrate re-creates). Every stored time becomes strftime('%Y-%m-%dT%H:%M:%fZ', value): UTC with
-- milliseconds, so a time with an offset (a git commit time like 2026-11-10T10:00:00+02:00) is converted to UTC
-- and a time without milliseconds or with a space gains them. A stored time that is not a date at all makes this
-- migration fail and change nothing (the error names the constraint below; fix the row, then run migrate again).
-- From now on the tr_time_* triggers refuse any other format. No table is rebuilt: the new columns are added at
-- the end of their tables.

UPDATE schema_migrations SET applied_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', applied_at), applied_at);
UPDATE repositories SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE actors SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE domains SET retired_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', retired_at), retired_at);
UPDATE themes SET retired_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', retired_at), retired_at);
UPDATE design_goals SET adopted_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', adopted_at), adopted_at), retired_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', retired_at), retired_at);
UPDATE reviews SET approved_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', approved_at), approved_at), started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at), completed_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', completed_at), completed_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE review_repositories SET commit_time = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', commit_time), commit_time);
UPDATE reviewer_runs SET started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at), finished_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', finished_at), finished_at);
UPDATE requirements SET stated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', stated_at), stated_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE issues SET opened_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', opened_at), opened_at), resolved_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at), resolved_at), verified_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', verified_at), verified_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE issue_relations SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE findings SET verified_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', verified_at), verified_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE review_issue_checks SET checked_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', checked_at), checked_at);
UPDATE decisions SET asked_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', asked_at), asked_at), decided_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', decided_at), decided_at);
UPDATE gates SET answered_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', answered_at), answered_at), applied_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', applied_at), applied_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE plans SET approved_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', approved_at), approved_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE steps SET started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at), done_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', done_at), done_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE step_commits SET committed_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', committed_at), committed_at);
UPDATE guard_tests SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE test_runs SET started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at);
UPDATE metrics SET recorded_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', recorded_at), recorded_at);
UPDATE artifacts SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE events SET at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', at), at);

CREATE TEMP TABLE p0005_unreadable_times (what TEXT CONSTRAINT a_stored_time_is_not_a_date_fix_the_row_then_migrate_again CHECK (what IS NULL));
INSERT INTO p0005_unreadable_times(what)
  SELECT 'schema_migrations.applied_at: ' || applied_at FROM schema_migrations WHERE applied_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', applied_at) IS NOT applied_at
  UNION ALL SELECT 'repositories.created_at: ' || created_at FROM repositories WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'actors.created_at: ' || created_at FROM actors WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'domains.retired_at: ' || retired_at FROM domains WHERE retired_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', retired_at) IS NOT retired_at
  UNION ALL SELECT 'themes.retired_at: ' || retired_at FROM themes WHERE retired_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', retired_at) IS NOT retired_at
  UNION ALL SELECT 'design_goals.adopted_at: ' || adopted_at FROM design_goals WHERE adopted_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', adopted_at) IS NOT adopted_at
  UNION ALL SELECT 'design_goals.retired_at: ' || retired_at FROM design_goals WHERE retired_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', retired_at) IS NOT retired_at
  UNION ALL SELECT 'reviews.approved_at: ' || approved_at FROM reviews WHERE approved_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', approved_at) IS NOT approved_at
  UNION ALL SELECT 'reviews.started_at: ' || started_at FROM reviews WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'reviews.completed_at: ' || completed_at FROM reviews WHERE completed_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', completed_at) IS NOT completed_at
  UNION ALL SELECT 'reviews.updated_at: ' || updated_at FROM reviews WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'review_repositories.commit_time: ' || commit_time FROM review_repositories WHERE commit_time IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', commit_time) IS NOT commit_time
  UNION ALL SELECT 'reviewer_runs.started_at: ' || started_at FROM reviewer_runs WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'reviewer_runs.finished_at: ' || finished_at FROM reviewer_runs WHERE finished_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', finished_at) IS NOT finished_at
  UNION ALL SELECT 'requirements.stated_at: ' || stated_at FROM requirements WHERE stated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', stated_at) IS NOT stated_at
  UNION ALL SELECT 'requirements.created_at: ' || created_at FROM requirements WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'issues.opened_at: ' || opened_at FROM issues WHERE opened_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', opened_at) IS NOT opened_at
  UNION ALL SELECT 'issues.resolved_at: ' || resolved_at FROM issues WHERE resolved_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at) IS NOT resolved_at
  UNION ALL SELECT 'issues.verified_at: ' || verified_at FROM issues WHERE verified_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', verified_at) IS NOT verified_at
  UNION ALL SELECT 'issues.updated_at: ' || updated_at FROM issues WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'issue_relations.created_at: ' || created_at FROM issue_relations WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'findings.verified_at: ' || verified_at FROM findings WHERE verified_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', verified_at) IS NOT verified_at
  UNION ALL SELECT 'findings.created_at: ' || created_at FROM findings WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'review_issue_checks.checked_at: ' || checked_at FROM review_issue_checks WHERE checked_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', checked_at) IS NOT checked_at
  UNION ALL SELECT 'decisions.asked_at: ' || asked_at FROM decisions WHERE asked_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', asked_at) IS NOT asked_at
  UNION ALL SELECT 'decisions.decided_at: ' || decided_at FROM decisions WHERE decided_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', decided_at) IS NOT decided_at
  UNION ALL SELECT 'gates.answered_at: ' || answered_at FROM gates WHERE answered_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', answered_at) IS NOT answered_at
  UNION ALL SELECT 'gates.applied_at: ' || applied_at FROM gates WHERE applied_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', applied_at) IS NOT applied_at
  UNION ALL SELECT 'gates.created_at: ' || created_at FROM gates WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'plans.approved_at: ' || approved_at FROM plans WHERE approved_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', approved_at) IS NOT approved_at
  UNION ALL SELECT 'plans.created_at: ' || created_at FROM plans WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'plans.updated_at: ' || updated_at FROM plans WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'steps.started_at: ' || started_at FROM steps WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'steps.done_at: ' || done_at FROM steps WHERE done_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', done_at) IS NOT done_at
  UNION ALL SELECT 'steps.updated_at: ' || updated_at FROM steps WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'step_commits.committed_at: ' || committed_at FROM step_commits WHERE committed_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) IS NOT committed_at
  UNION ALL SELECT 'guard_tests.created_at: ' || created_at FROM guard_tests WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'test_runs.started_at: ' || started_at FROM test_runs WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'metrics.recorded_at: ' || recorded_at FROM metrics WHERE recorded_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', recorded_at) IS NOT recorded_at
  UNION ALL SELECT 'artifacts.created_at: ' || created_at FROM artifacts WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'events.at: ' || at FROM events WHERE at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', at) IS NOT at;
DROP TABLE p0005_unreadable_times;
UPDATE meta SET value = strftime('%Y-%m-%dT%H:%M:%fZ', value)
  WHERE key = 'created_at' AND strftime('%Y-%m-%dT%H:%M:%fZ', value) IS NOT NULL;

ALTER TABLE reviews ADD COLUMN report_md TEXT;
ALTER TABLE plans ADD COLUMN narrative_md TEXT;
ALTER TABLE findings ADD COLUMN discovered_in_step_id INTEGER REFERENCES steps(id);
CREATE INDEX ix_fk_findings_discovered_in_step_id    ON findings(discovered_in_step_id);
ALTER TABLE finding_locations ADD COLUMN whole_file INTEGER NOT NULL DEFAULT 0 CHECK (whole_file IN (0,1));
ALTER TABLE finding_locations ADD COLUMN excerpt_status TEXT
  CHECK (excerpt_status IS NULL OR excerpt_status IN ('truncated','not-text'));

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unplanned_discoveries','Defects found during fix work that no plan step closes','error',60);
-- migration: 0006-lookups.sql
-- lookup tables for the ledger's words and labels; RESTRICT keys under reviews and findings; metrics.name catalogued
-- Plan P-0001 steps 95 (D-0009), 94, 70 and 61. The ledger's words get lookup tables: issue_statuses (with the
-- active, resolved, fixed, reopens and briefed flags that views and triggers now read), severities, match_types
-- (came_back), relation_types (inverse labels; closes) and gate_kinds, referenced by foreign key instead of the
-- CHECK lists on issues.status, issues/findings.severity, findings.match_type, issue_relations.relation and
-- gates.kind; every other word list keeps its CHECK and has its labels in enum_labels. metrics.name now
-- references metric_defs(name), and the keys from the children of reviews and findings become ON DELETE RESTRICT
-- (the append-only triggers already refused those deletes, so nothing behaves differently). Fifteen tables are
-- rebuilt with SQLite's recipe (create <table>_new, copy the rows, drop the old table, rename the new one,
-- re-create its indexes); every CHECK, trigger and column of schemas 3-5 is kept. migrate first lists any row a
-- rule of this migration would refuse.

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

CREATE TABLE severities (
  value    TEXT PRIMARY KEY,                 -- H, M, L, T
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE           -- most severe first
);

CREATE TABLE match_types (
  value     TEXT PRIMARY KEY,
  label     TEXT NOT NULL,
  position  INTEGER NOT NULL UNIQUE,
  came_back INTEGER NOT NULL CHECK (came_back IN (0,1))  -- the issue was closed and was raised again
);

CREATE TABLE relation_types (
  value         TEXT PRIMARY KEY,
  label         TEXT NOT NULL,              -- read from the issue the relation starts at ("Duplicate of")
  inverse_label TEXT NOT NULL,              -- read from the issue it points to ("Has duplicate")
  position      INTEGER NOT NULL UNIQUE,
  closes        INTEGER NOT NULL CHECK (closes IN (0,1))  -- recording it closes the active issue as duplicate
);

CREATE TABLE gate_kinds (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE
);

CREATE TABLE enum_labels (
  enum     TEXT NOT NULL,
  value    TEXT NOT NULL,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (enum, value),
  UNIQUE (enum, position)
);

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
 ('other','Question',10);

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
 ('entity_type','step-finding','Step coverage',10);

-- Every metric name in use has a catalog row (0004 added them; again here, before the key exists).
INSERT OR IGNORE INTO metric_defs(name, label, unit)
  SELECT DISTINCT name, upper(substr(name, 1, 1)) || replace(substr(name, 2), '_', ' '), NULL FROM metrics;

CREATE TABLE review_repositories_new (
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
INSERT INTO review_repositories_new(review_id, repository_id, commit_sha, branch, commit_time, commit_subject, tag_name, tag_pushed, is_dirty, dirty_note, dirty_patch_path)
  SELECT review_id, repository_id, commit_sha, branch, commit_time, commit_subject, tag_name, tag_pushed, is_dirty, dirty_note, dirty_patch_path FROM review_repositories;
DROP TABLE review_repositories;
ALTER TABLE review_repositories_new RENAME TO review_repositories;

CREATE TABLE review_goals_new (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);
INSERT INTO review_goals_new(review_id, goal_id)
  SELECT review_id, goal_id FROM review_goals;
DROP TABLE review_goals;
ALTER TABLE review_goals_new RENAME TO review_goals;

CREATE TABLE review_domains_new (
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code       TEXT NOT NULL REFERENCES domains(code),
  decision          TEXT NOT NULL CHECK (decision IN ('included','skipped')),
  reason            TEXT NOT NULL,
  owner_change      TEXT NOT NULL DEFAULT 'none' CHECK (owner_change IN
                      ('none','added-by-owner','removed-by-owner','modified-by-owner')),
  reviewers_planned INTEGER,
  split_note        TEXT,                 -- how the domain was split between reviewers
  PRIMARY KEY (review_id, domain_code)
);
INSERT INTO review_domains_new(review_id, domain_code, decision, reason, owner_change, reviewers_planned, split_note)
  SELECT review_id, domain_code, decision, reason, owner_change, reviewers_planned, split_note FROM review_domains;
DROP TABLE review_domains;
ALTER TABLE review_domains_new RENAME TO review_domains;

CREATE TABLE reviewer_runs_new (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
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
INSERT INTO reviewer_runs_new(id, review_id, domain_code, actor_id, label, scope, prompt, report_path, report_sha256, status, items_reviewed, high_count, medium_count, low_count, test_count, final_message, started_at, finished_at)
  SELECT id, review_id, domain_code, actor_id, label, scope, prompt, report_path, report_sha256, status, items_reviewed, high_count, medium_count, low_count, test_count, final_message, started_at, finished_at FROM reviewer_runs;
DROP TABLE reviewer_runs;
ALTER TABLE reviewer_runs_new RENAME TO reviewer_runs;

CREATE TABLE review_actors_new (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);
INSERT INTO review_actors_new(review_id, actor_id, role_label)
  SELECT review_id, actor_id, role_label FROM review_actors;
DROP TABLE review_actors;
ALTER TABLE review_actors_new RENAME TO review_actors;

CREATE TABLE issues_new (
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
INSERT INTO issues_new(id, code, title, domain_code, theme_code, sub, kind, severity, status, fingerprint, before_state, current_state, after_state, recommendation, resolution, first_seen_review_id, last_seen_review_id, last_checked_review_id, times_seen, opened_at, resolved_at, verified_at, updated_at, change_actor_id, change_review_id, change_step_id, change_commit_sha, change_note)
  SELECT id, code, title, domain_code, theme_code, sub, kind, severity, status, fingerprint, before_state, current_state, after_state, recommendation, resolution, first_seen_review_id, last_seen_review_id, last_checked_review_id, times_seen, opened_at, resolved_at, verified_at, updated_at, change_actor_id, change_review_id, change_step_id, change_commit_sha, change_note FROM issues;
DROP TABLE issues;
ALTER TABLE issues_new RENAME TO issues;

CREATE TABLE issue_relations_new (
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
INSERT INTO issue_relations_new(id, from_issue_id, to_issue_id, relation, confidence, rationale, established_in_review_id, established_by_actor_id, created_at)
  SELECT id, from_issue_id, to_issue_id, relation, confidence, rationale, established_in_review_id, established_by_actor_id, created_at FROM issue_relations;
DROP TABLE issue_relations;
ALTER TABLE issue_relations_new RENAME TO issue_relations;

CREATE TABLE findings_new (
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
INSERT INTO findings_new(id, review_id, reviewer_run_id, point_id, issue_id, match_type, match_confidence, match_rationale, severity, kind, domain_code, theme_code, sub, summary, detail, before_state, recommendation, recommended_test, evidence, where_report, fingerprint, related_point_ids, verification, verified_by_actor_id, verified_at, verification_note, created_at, discovered_in_step_id)
  SELECT id, review_id, reviewer_run_id, point_id, issue_id, match_type, match_confidence, match_rationale, severity, kind, domain_code, theme_code, sub, summary, detail, before_state, recommendation, recommended_test, evidence, where_report, fingerprint, related_point_ids, verification, verified_by_actor_id, verified_at, verification_note, created_at, discovered_in_step_id FROM findings;
DROP TABLE findings;
ALTER TABLE findings_new RENAME TO findings;

CREATE TABLE finding_locations_new (
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
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);
INSERT INTO finding_locations_new(id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role, excerpt_redacted, whole_file, excerpt_status)
  SELECT id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role, excerpt_redacted, whole_file, excerpt_status FROM finding_locations;
DROP TABLE finding_locations;
ALTER TABLE finding_locations_new RENAME TO finding_locations;

CREATE TABLE finding_goals_new (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);
INSERT INTO finding_goals_new(finding_id, goal_id)
  SELECT finding_id, goal_id FROM finding_goals;
DROP TABLE finding_goals;
ALTER TABLE finding_goals_new RENAME TO finding_goals;

CREATE TABLE finding_requirements_new (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);
INSERT INTO finding_requirements_new(finding_id, requirement_id, verdict)
  SELECT finding_id, requirement_id, verdict FROM finding_requirements;
DROP TABLE finding_requirements;
ALTER TABLE finding_requirements_new RENAME TO finding_requirements;

CREATE TABLE review_requirement_checks_new (
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
INSERT INTO review_requirement_checks_new(id, review_id, requirement_id, verdict, where_implemented, tested, finding_id, evidence)
  SELECT id, review_id, requirement_id, verdict, where_implemented, tested, finding_id, evidence FROM review_requirement_checks;
DROP TABLE review_requirement_checks;
ALTER TABLE review_requirement_checks_new RENAME TO review_requirement_checks;

CREATE TABLE review_issue_checks_new (
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
INSERT INTO review_issue_checks_new(review_id, issue_id, outcome, finding_id, evidence, actor_id, checked_at)
  SELECT review_id, issue_id, outcome, finding_id, evidence, actor_id, checked_at FROM review_issue_checks;
DROP TABLE review_issue_checks;
ALTER TABLE review_issue_checks_new RENAME TO review_issue_checks;

CREATE TABLE gates_new (
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
INSERT INTO gates_new(id, code, kind, title, question, context, options, allow_text, blocking, status, answer_value, answer_text, answered_by, answered_via, answered_at, applied_note, applied_at, review_id, plan_id, step_id, issue_id, decision_id, opened_by_actor_id, created_at, answered_by_actor_id, change_actor_id)
  SELECT id, code, kind, title, question, context, options, allow_text, blocking, status, answer_value, answer_text, answered_by, answered_via, answered_at, applied_note, applied_at, review_id, plan_id, step_id, issue_id, decision_id, opened_by_actor_id, created_at, answered_by_actor_id, change_actor_id FROM gates;
DROP TABLE gates;
ALTER TABLE gates_new RENAME TO gates;

CREATE TABLE metrics_new (
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
INSERT INTO metrics_new(id, review_id, domain_code, repository_id, name, scope, value, unit, method, note, recorded_at)
  SELECT id, review_id, domain_code, repository_id, name, scope, value, unit, method, note, recorded_at FROM metrics;
DROP TABLE metrics;
ALTER TABLE metrics_new RENAME TO metrics;

-- The indexes of the rebuilt tables (DROP TABLE removed them).
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
CREATE INDEX ix_metrics_name         ON metrics(name, scope);
CREATE INDEX ix_runs_review          ON reviewer_runs(review_id);
CREATE INDEX ix_gates_status         ON gates(status);
CREATE UNIQUE INDEX ux_metrics_key            ON metrics(review_id, name, scope, COALESCE(repository_id, 0));
CREATE UNIQUE INDEX ux_locations_one_primary  ON finding_locations(finding_id) WHERE role = 'primary';
CREATE INDEX ix_fk_gates_change_actor_id      ON gates(change_actor_id);
CREATE INDEX ix_fk_gates_answered_by_actor_id ON gates(answered_by_actor_id);
CREATE INDEX ix_fk_review_repositories_repository_id ON review_repositories(repository_id);
CREATE INDEX ix_fk_review_goals_goal_id              ON review_goals(goal_id);
CREATE INDEX ix_fk_review_domains_domain_code        ON review_domains(domain_code);
CREATE INDEX ix_fk_reviewer_runs_actor_id            ON reviewer_runs(actor_id);
CREATE INDEX ix_fk_reviewer_runs_domain_code         ON reviewer_runs(domain_code);
CREATE INDEX ix_fk_review_actors_actor_id            ON review_actors(actor_id);
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
CREATE INDEX ix_gates_decision                       ON gates(decision_id);
CREATE INDEX ix_fk_gates_issue_id                    ON gates(issue_id);
CREATE INDEX ix_fk_gates_opened_by_actor_id          ON gates(opened_by_actor_id);
CREATE INDEX ix_fk_gates_plan_id                     ON gates(plan_id);
CREATE INDEX ix_fk_gates_review_id                   ON gates(review_id);
CREATE INDEX ix_gates_step                           ON gates(step_id);
CREATE INDEX ix_fk_metrics_domain_code               ON metrics(domain_code);
CREATE INDEX ix_fk_metrics_repository_id             ON metrics(repository_id);
CREATE INDEX ix_fk_issues_severity                   ON issues(severity);
CREATE INDEX ix_fk_issue_relations_relation          ON issue_relations(relation);
CREATE INDEX ix_fk_findings_severity                 ON findings(severity);
CREATE INDEX ix_fk_findings_match_type               ON findings(match_type);
CREATE INDEX ix_fk_gates_kind                        ON gates(kind);
-- migration: 0007-views.sql
-- must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason
-- Plan P-0001 steps 58, 65, 67, 68, 70, 73, 76, 77, 78, 83 and 93 (views and triggers). The views and triggers come
-- with the shipped schema, which migrate re-creates after the last migration: the reporting views count real
-- findings only (v_real_findings), the must-be-empty views forgive abandoned reviews, superseded and abandoned
-- plans and dropped steps, references fall back to their review's commit, and new triggers refuse accepted-risk or
-- wont-fix without a resolution, 'verified' without a later review's fixed check, a fixed/regressed/changed check
-- without evidence and severity T on a finding that is not a test. Rows already in the ledger are not changed:
-- the new checks below list the ones that break these rules. No table is rebuilt; steps gains one column at the
-- end (the reason a step closes no finding by design, such as the follow-up review that verifies the plan).

ALTER TABLE steps ADD COLUMN no_findings_reason TEXT;

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_history_gaps','Issues and gates whose history is missing an event','error',70),
 ('v_match_problems','Matches whose type disagrees with the issue','error',72),
 ('v_off_plan_coverage','Steps closing a finding of a review their plan does not cover','error',74),
 ('v_done_steps_open_issues','Done steps whose issues are still to do','error',76),
 ('v_closed_without_reason','Issues accepted or declined with no reason recorded','error',78),
 ('v_relation_problems','Related-to pairs stored in both directions','error',80),
 ('v_check_finding_owner','Review checks citing a finding of another issue','error',82);
"""


def _parse_migrations(text):
    """[(version, slug, description, sql)] from the embedded references/migrations/NNNN-<slug>.sql files."""
    out = []
    for m in re.finditer(r"^-- migration: (\d{4})-([a-z0-9][a-z0-9-]*)\.sql\n(.*?)(?=^-- migration: |\Z)", text,
                         re.M | re.S):
        body = m.group(3)
        out.append((int(m.group(1)), m.group(2), body.split("\n", 1)[0][3:].strip(), body))
    return out


# The schema version is the highest migration; references/schema.sql makes a ledger of exactly that version, and
# dev/build.py refuses to build unless its header, meta seed and schema_migrations seed agree.
MIGRATIONS = _parse_migrations(MIGRATION_FILES)
SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0
BASELINE_VERSION = MIGRATIONS[0][0] if MIGRATIONS else 0  # the oldest schema migrate starts from

HEX = re.compile(r"^[0-9a-f]{7,64}$")
FULL_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REVIEW_CODE = re.compile(r"R-[0-9]{4,}")
SAFE_URL = re.compile(r"^https?://")  # the only links the ledger stores or the viewer draws (schema CHECKs agree)
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------- database

def connect(db, readonly=False, create=False):
    """A connection with foreign keys on, a 5 s busy timeout and sqlite3.Row rows. The file is opened through a
    percent-encoded file: URI, so ?, # or % in the path cannot cut the name or drop the mode. readonly: mode=ro plus
    PRAGMA query_only. Otherwise mode=rw, which refuses a missing file; only init and backups pass create=True."""
    mode = "ro" if readonly else ("rwc" if create else "rw")
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=" + mode, uri=True, timeout=5,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    if readonly:
        con.execute("PRAGMA query_only = 1")
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


class LedgerError(Exception):
    """A ledger that cannot be used as it is; the message is one line for the person running the command."""


def ledger_version(con, db):
    """The ledger's meta schema_version as an int; LedgerError for anything that is not a ledger."""
    try:
        r = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.DatabaseError as e:  # not SQLite at all, or SQLite without the ledger's tables
        raise LedgerError(f"{db} is not a ledger ({e})") from None
    if r is None:
        raise LedgerError(f"{db} has no schema_version in its meta table; it was not made by ledger.py init")
    try:
        return int(r[0])
    except (TypeError, ValueError):
        raise LedgerError(f"{db} has schema_version {r[0]!r} in its meta table, which is not a whole number") from None


def version_problem(db, v, write):
    """None when schema v can be used as asked, else (fatal, message)."""
    me = Path(__file__).resolve()
    if v > SCHEMA_VERSION:
        return True, f"{db} is schema {v}; this ledger.py only knows {SCHEMA_VERSION}. Use a newer ledger.py."
    if v < BASELINE_VERSION:
        return write, (f"{db} is schema {v}, older than any schema this ledger.py can migrate "
                       f"(oldest {BASELINE_VERSION}).")
    if v < SCHEMA_VERSION:
        return write, f"{db} is schema {v}; this ledger.py is schema {SCHEMA_VERSION}. Run: python3 {me} migrate"
    return None


def open_ledger(db, write=False, verify=False):
    """Connect to an existing ledger for a command, or exit 1 with one line: no ledger, not a ledger, no
    schema_version, a newer schema, or (for writes) an older one. Reads of an older ledger print a warning. With
    verify, a ledger of this schema version whose stored schema differs from the shipped one is refused too."""
    if not db.exists():
        sys.exit(f"no ledger at {db}; run: python3 ledger.py init")
    try:
        con = connect(db)
        v = ledger_version(con, db)
    except (LedgerError, sqlite3.DatabaseError) as e:
        sys.exit(str(e) if isinstance(e, LedgerError) else f"{db} is not a ledger ({e})")
    problem = version_problem(db, v, write)
    if problem:
        fatal, msg = problem
        if fatal:
            con.close()
            sys.exit(msg)
        print(f"warning: {msg} (reading it as it is)", file=sys.stderr)
    elif verify:
        problems = verify_schema(con)
        if problems:
            con.close()
            sys.exit(drift_message(db, problems))
    return con


def drift_message(db, problems):
    shown = "\n".join(f"  {p}" for p in problems[:20])
    more = f"\n  ... {len(problems) - 20} more" if len(problems) > 20 else ""
    return (f"{db} does not match the schema this ledger.py ships ({len(problems)} differences); refusing to use it. "
            f"init with the skill's ledger.py restores the shipped views and triggers; check lists every "
            f"difference.\n{shown}{more}")


# ---------------------------------------------------------------- schema comparison and migrations

def _sql_segments(sql):
    """Split SQL into (kind, text) runs: 'lit' for '...' string literals, 'id' for simple quoted identifiers (given
    unquoted), 'code' for the rest, with -- and /* */ comments replaced by a space."""
    out, i, n, code = [], 0, len(sql), []
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k < 0:
                    k = n - 1
                    break
                if sql[k + 1:k + 2] == "'":
                    j = k + 2
                    continue
                break
            out.append(("code", "".join(code))); code = []
            out.append(("lit", sql[i:k + 1])); i = k + 1
        elif c in '"`':
            k = sql.find(c, i + 1)
            inner = sql[i + 1:k] if k > 0 else ""
            if k > 0 and NAME.match(inner):
                code.append(inner)
            else:
                code.append(sql[i:(k + 1 if k > 0 else n)])
            i = k + 1 if k > 0 else n
        elif sql.startswith("--", i):
            k = sql.find("\n", i)
            code.append(" "); i = n if k < 0 else k
        elif sql.startswith("/*", i):
            k = sql.find("*/", i + 2)
            code.append(" "); i = n if k < 0 else k + 2
        else:
            code.append(c); i += 1
    out.append(("code", "".join(code)))
    return out


def normalize_sql(sql):
    """SQL text compared for schema drift: comments dropped, whitespace collapsed (none around ( ) , ;), simple
    quoted names unquoted; string literals kept exactly."""
    parts = []
    for kind, text in _sql_segments(sql or ""):
        if kind == "code":
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r" ?([(),;]) ?", r"\1", text)
        parts.append(text)
    return "".join(parts).strip()


def split_sql(sql):
    """The statements of a script, in order, without comment-only pieces (for running inside one transaction:
    executescript would commit first)."""
    out, buf = [], ""
    pieces = sql.split(";")
    for i, piece in enumerate(pieces):
        buf += piece + (";" if i < len(pieces) - 1 else "")
        if sqlite3.complete_statement(buf):
            if normalize_sql(buf).strip(";"):
                out.append(buf.strip())
            buf = ""
    if normalize_sql(buf).strip(";"):
        raise ValueError(f"incomplete SQL statement at the end: {buf.strip()[:80]}")
    return out


_shipped = {}
CREATE_OBJECT = re.compile(r"^CREATE (?:UNIQUE |TEMP |TEMPORARY )?(TABLE|INDEX|VIEW|TRIGGER) (?:IF NOT EXISTS )?"
                           r"([A-Za-z_][A-Za-z0-9_]*)", re.I)

LEADING_COMMENTS = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)


def shipped_objects():
    """[(type, name, tbl_name, sql)] of the shipped schema in creation order: each CREATE statement of SCHEMA, read
    from the text (tbl_name is None). dev/tests/test_schema_drift.py checks it equals what SQLite stores when SCHEMA
    runs."""
    if SCHEMA not in _shipped:
        out = []
        for stmt in split_sql(SCHEMA):
            m = CREATE_OBJECT.match(normalize_sql(stmt))
            if m:
                lead = LEADING_COMMENTS.match(stmt)  # skip comments above the statement, which may say "create"
                sql = stmt[lead.end():].rstrip().rstrip(";").rstrip()
                out.append((m.group(1).lower(), m.group(2), None, sql))
        _shipped.clear()
        _shipped[SCHEMA] = out
    return _shipped[SCHEMA]


def ledger_objects(con):
    return {(r[0], r[1]): r[2] for r in con.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}


def shipped_schema_hash():
    """sha256 of the shipped schema's objects (type, name, normalized sql), stored in meta schema_hash."""
    items = sorted((t, n, normalize_sql(s)) for t, n, _, s in shipped_objects())
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()


TYPE_ORDER = {"table": 0, "index": 1, "view": 2, "trigger": 3}


def verify_schema(con):
    """Every difference between the ledger's stored schema and the shipped one, as lines naming the object: missing,
    extra (not in the shipped schema) or changed tables, indexes, views and triggers. Empty when they match."""
    want = {(t, n): normalize_sql(s) for t, n, _, s in shipped_objects()}
    have = {k: normalize_sql(s) for k, s in ledger_objects(con).items()}
    out = []
    for key in sorted(set(want) | set(have), key=lambda k: (TYPE_ORDER.get(k[0], 9), k[1])):
        t, n = key
        if key not in have:
            out.append(f"missing {t} {n}")
        elif key not in want:
            out.append(f"extra {t} {n} (not in the shipped schema)")
        elif have[key] != want[key]:
            out.append(f"changed {t} {n} (its definition differs from the shipped schema)")
    return out


def _drop_views_and_triggers(con):
    for t, n in [(t, n) for (t, n) in ledger_objects(con) if t in ("view", "trigger")]:
        con.execute(f'DROP {t.upper()} IF EXISTS "{n}"')


def _create_views_and_triggers(con):
    for t, n, _, sql in shipped_objects():
        if t in ("view", "trigger"):
            con.execute(sql)


def sync_views_and_triggers(con):
    """Make the ledger's views and triggers exactly the shipped ones (they hold no data). Runs in the caller's
    transaction. Returns notes naming each one that was removed or restored; empty when nothing differed."""
    want = {(t, n): normalize_sql(s) for t, n, _, s in shipped_objects() if t in ("view", "trigger")}
    have = {k: normalize_sql(s) for k, s in ledger_objects(con).items() if k[0] in ("view", "trigger")}
    notes = [f"removed {t} {n} (not in the shipped schema)" for (t, n) in sorted(set(have) - set(want))]
    notes += [f"restored {t} {n} (the shipped definition)" for (t, n) in sorted(want)
              if have.get((t, n)) != want[(t, n)]]
    if notes:
        _drop_views_and_triggers(con)
        _create_views_and_triggers(con)
    return notes


def stamp_meta(con, created_by=None):
    """Record which tool last wrote the ledger, the shipped schema's hash and PRAGMA user_version (only what
    changed). created_by fills created_by_skill when the ledger has none."""
    meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
    want = {"written_by_tool": TOOL_VERSION, "schema_hash": shipped_schema_hash()}
    if created_by and not meta.get("created_by_skill"):
        want["created_by_skill"] = created_by
    for k, v in want.items():
        if meta.get(k) != v:
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (k, v))
    if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


UNKNOWN_CREATOR = "unknown (made before ledger.py had versions)"


class MigrationError(Exception):
    pass


def free_backup_name(db, v):
    base = db.with_name(f"{db.name}.bak-v{v}")
    n, cand = 1, base
    while cand.exists():
        n += 1
        cand = base.with_name(f"{base.name}.{n}")
    return cand


def _checks_of(sql):
    """The CHECK expressions of one CREATE TABLE statement (normalized text), in order."""
    text, out, i = normalize_sql(sql), [], 0
    while True:
        m = re.compile(r"\bCHECK\(", re.I).search(text, i)
        if not m:
            return out
        depth, j, quote = 1, m.end(), False
        while j < len(text) and depth:
            c = text[j]
            if c == "'":
                quote = not quote
            elif not quote:
                depth += {"(": 1, ")": -1}.get(c, 0)
            j += 1
        out.append(text[m.end():j - 1])
        i = j


def rows_refused_by(con, stmt, limit=50):
    """For a migration's `CREATE TABLE <table>_new (...)` statement: a line for each row of <table> that one of the
    new table's CHECKs would refuse, '<table> <code, id or rowid>: <rule> (<column>=<value> ...)', at most `limit`.
    Empty for any other statement. A rule naming a column the old table lacks (a column the rebuild adds) is
    skipped: that column holds its default after the rebuild."""
    m = re.match(r"CREATE TABLE ([A-Za-z_][A-Za-z0-9_]*)_new\s*\(", normalize_sql(stmt), re.I)
    if not m:
        return []
    table, out = m.group(1), []
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({quote_name(table)})")]
    key = "code" if "code" in cols else ("id" if "id" in cols else "rowid")
    for rule in _checks_of(stmt) if cols else []:
        used = [c for c in cols if re.search(rf"\b{re.escape(c)}\b", rule)]
        try:
            bad = con.execute(f"SELECT {key}, {', '.join(used) or 'NULL'} FROM {quote_name(table)} "
                              f"WHERE NOT ({rule}) LIMIT {int(limit)}").fetchall()
        except sqlite3.OperationalError:
            continue  # names a column this table does not have yet
        for r in bad:
            vals = " ".join(f"{c}={r[n + 1]!r}" for n, c in enumerate(used))
            out.append(f"{table} {r[0]}: {rule if len(rule) <= 140 else rule[:137] + '...'} ({vals})")
    return out[:limit]


def migrate(db):
    """Upgrade the ledger to SCHEMA_VERSION: back it up (SQLite backup API) to ledger.sqlite.bak-v<N>, then in one
    BEGIN IMMEDIATE transaction drop every view and trigger, apply each pending migration and record it in
    schema_migrations, re-create the shipped views and triggers, run foreign_key_check and compare the result with
    the shipped schema; anything wrong rolls it all back. Refuses a ledger newer than this tool."""
    if not db.exists():
        sys.exit(f"no ledger at {db}; run: python3 ledger.py init")
    try:
        con = connect(db)
        v = ledger_version(con, db)
    except (LedgerError, sqlite3.DatabaseError) as e:
        sys.exit(str(e) if isinstance(e, LedgerError) else f"{db} is not a ledger ({e})")
    problem = version_problem(db, v, True)
    if problem and v >= BASELINE_VERSION and v < SCHEMA_VERSION:
        problem = None  # older but migratable: that is what migrate is for
    if problem:
        con.close()
        sys.exit(problem[1])
    pending = [m for m in MIGRATIONS if m[0] > v]
    backup = None
    if pending:
        backup = free_backup_name(db, v)
        dest = connect(backup, create=True)
        try:
            con.backup(dest)
        finally:
            dest.close()
        make_private(backup)  # a full copy of the ledger: as private as the ledger itself
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys = OFF")  # the table-rebuild recipe needs it off; a no-op inside a transaction
    step = "the update" if pending else "the check of the current schema"
    try:
        con.execute("BEGIN IMMEDIATE")
        if pending:
            _drop_views_and_triggers(con)
            for ver, slug, desc, sql in pending:
                step = f"migration {ver:04d}-{slug}"
                for stmt in split_sql(sql):
                    # before a table is rebuilt with new rules, list every row they refuse (step 61)
                    refused = rows_refused_by(con, stmt)
                    if refused:
                        raise MigrationError(
                            f"CHECK constraint failed for {len(refused)} existing row"
                            f"{'s' if len(refused) != 1 else ''} (fix each, then run migrate again):\n  "
                            + "\n  ".join(refused) + "\n")
                    con.execute(stmt)
                con.execute("INSERT INTO schema_migrations(version, description) VALUES (?, ?)", (ver, desc))
            step = "the checks after " + ", ".join(f"migration {m[0]:04d}-{m[1]}" for m in pending)
            con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            _create_views_and_triggers(con)
            notes = []
        else:
            notes = sync_views_and_triggers(con)
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise MigrationError(f"foreign_key_check found {len(fk)} broken references after it "
                                 f"(first: {tuple(fk[0])})")
        problems = verify_schema(con)
        if problems:
            raise MigrationError("the result differs from the shipped schema: " + "; ".join(problems[:10]))
        stamp_meta(con, created_by=UNKNOWN_CREATOR)
        con.execute("COMMIT")
    except (sqlite3.Error, ValueError, MigrationError) as e:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
        if backup is not None and backup.exists():
            backup.unlink()  # the ledger is unchanged, so the copy is not needed
        sys.exit(f"migrate: {step} failed: {e}; nothing was changed (the ledger is still schema {v})")
    finally:
        try:
            con.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error:
            pass
    con.close()
    if pending:
        set_wal(db)  # existing ledgers switch to WAL once, with their upgrade (D-0006)
        print(f"migrated {db} from schema {v} to {SCHEMA_VERSION} (a copy of the schema-{v} ledger is at {backup})")
        for ver, slug, desc, _ in pending:
            print(f"  {ver:04d}-{slug}: {desc}")
    else:
        print(f"{db} is already schema {v}; nothing to migrate")
        for note in notes:
            print(f"note: {note}")
    return 0


# ---------------------------------------------------------------- init and the project's copy of ledger.py

VERSION_LINE = re.compile(r'^TOOL_VERSION = "([^"\n]*)"', re.M)


def parse_version(v):
    """A sortable key for a semantic version (a pre-release sorts before its release), or None."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1, m.group(4) or "")


def tool_version_of(path):
    """TOOL_VERSION of a ledger.py read as text (it is never run): '' when it has none (made before versions)."""
    m = VERSION_LINE.search(path.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else ""


def plan_tool_copy(home, force):
    """What init does with the project's .code-review/ledger.py: None (leave it), or (note, keep_backup) to replace
    it with this file. Exits 1, before anything is written, when the project copy is newer (or of unknown version)
    and --force was not given."""
    me = Path(__file__).resolve()
    if not home.exists():
        return ("", False)
    if me == home.resolve() or home.read_bytes() == me.read_bytes():
        return None
    theirs = tool_version_of(home)
    label = theirs or "no version"
    mine_k, theirs_k = parse_version(TOOL_VERSION), (parse_version(theirs) if theirs else ())
    bak = home.with_name("ledger.py.bak")
    if mine_k is not None and theirs_k is not None and theirs_k < mine_k:
        return (f"note: replacing {home} ({label}) with this copy ({TOOL_VERSION}); the old copy is kept as {bak}", True)
    if mine_k is not None and theirs_k == mine_k:
        if force:
            return (f"note: --force: replacing {home} (also {label}, other contents); the old copy is kept as {bak}",
                    True)
        print(f"warning: {home} says version {label} like this copy, but its contents differ; left it unchanged "
              f"(init --force replaces it)", file=sys.stderr)
        return None
    if force:
        return (f"note: --force: replacing {home} ({label}) with this older copy ({TOOL_VERSION}); the old copy is "
                f"kept as {bak}", True)
    newer = "newer than" if theirs_k is not None and mine_k is not None else "of a version not comparable with"
    sys.exit(f"{home} is ledger.py {label}, {newer} this copy ({TOOL_VERSION}); left it unchanged. Run that copy, "
             f"update the skill, or pass init --force to replace it.")


GITIGNORE = ("# The ledger runs in WAL mode: ledger.sqlite-wal and -shm exist while it is open, and dump checkpoints\n"
             "# them into ledger.sqlite. The rest are local backups and temporary files of ledger.py.\n"
             "ledger.sqlite-journal\nledger.sqlite-wal\nledger.sqlite-shm\nledger.sqlite.tmp\nledger.sqlite.bak-v*\n"
             "ledger.py.bak\nledger.py.tmp\nledger.sql.tmp\n")
TOOL_SIGNATURE = '"""Code review ledger:'  # the second line of every ledger.py this tool builds


def looks_like_tool(path):
    """True when path starts like a ledger.py built by this tool (its docstring within the first three lines)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = [f.readline() for _ in range(3)]
    except OSError:
        return False
    return any(l.startswith(TOOL_SIGNATURE) for l in head)


def write_atomic(path, data, mode=0o600):
    """Write bytes to path through path.tmp + fsync + os.replace, so path is either the old or the new content."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def make_private(path, mode=0o600):
    try:
        if path.exists():
            os.chmod(str(path), mode)
    except OSError:
        pass


def repaired_gitignore(text):
    """(new text, lines added) for an existing .gitignore: the owner's own lines first, then the tool's lines in
    order; a line that is only the start of one of the tool's lines (a truncated write) is dropped."""
    want = GITIGNORE.splitlines()
    have = text.splitlines()
    own = [l for l in have if l not in want and not any(w.startswith(l) for w in want)]
    added = [l for l in want if l not in have and not l.startswith("#")]
    return "".join(l + "\n" for l in own + want), added


def require_ledger(db):
    """Exit 1 with one line unless db is SQLite with a meta schema_version row."""
    try:
        con = connect(db)
        try:
            ledger_version(con, db)
        finally:
            con.close()
    except (LedgerError, sqlite3.DatabaseError) as e:
        sys.exit(f"incomplete or foreign ledger at {db} ({e}); move it aside and run init again")


def build_ledger(root, db):
    """Create db atomically: the whole schema and the meta rows are built in db.tmp inside one transaction, then
    renamed into place. On any failure nothing is left behind and init can simply run again."""
    tmp = db.with_name(db.name + ".tmp")
    side = [tmp.with_name(tmp.name + s) for s in ("", "-journal", "-wal", "-shm")]
    for p in side:
        if p.exists():
            p.unlink()
    con = None
    try:
        con = connect(tmp, create=True)  # created owner-only: init runs under umask 077
        con.isolation_level = None
        con.execute("BEGIN")
        for stmt in split_sql(SCHEMA):
            con.execute(stmt)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('project_name',?)", (root.name,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('created_at',?)", (now(),))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('ledger_location',?)", (str(db.parent),))
        stamp_meta(con, created_by=TOOL_VERSION)
        con.execute("COMMIT")
        con.close()
        con = None
        os.replace(str(tmp), str(db))
    except (sqlite3.Error, OSError, ValueError) as e:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
        for p in side:
            try:
                p.unlink()
            except OSError:
                pass
        sys.exit(f"init: could not create {db} ({e}); nothing was left behind, run init again")


def set_wal(db):
    """Switch the ledger to WAL (decision D-0006: readers never block the agent's writes). Kept in the file."""
    try:
        con = connect(db)
        try:
            con.execute("PRAGMA journal_mode = WAL")
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"note: could not switch {db} to WAL ({e}); it keeps its journal mode", file=sys.stderr)


def init(root, db, force=False, copy_tool=True, gitignore=True):
    """Create the ledger (or check and refresh an existing one), copy this ledger.py beside it and write its
    .gitignore. copy_tool / gitignore are off for a ledger file that is not in a ledger folder (--db elsewhere).
    Files are created owner-only (umask 077; the folder 0700, the ledger 0600)."""
    old = os.umask(0o077)
    try:
        _init(root, db, force, copy_tool, gitignore)
    finally:
        os.umask(old)


def _init(root, db, force, copy_tool, gitignore):
    home = db.parent / "ledger.py"
    if copy_tool and home.exists() and db.parent.name != ".code-review" and not looks_like_tool(home):
        sys.exit(f"{home} is not a ledger.py made by this tool; left it unchanged. Choose another folder for the "
                 f"ledger.")
    copy = plan_tool_copy(home, force) if copy_tool else None  # may exit, before anything is written
    fresh = not db.exists()
    if not fresh:
        require_ledger(db)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"init: could not create {db.parent} ({e})")
    if db.parent.name == ".code-review":
        make_private(db.parent, 0o700)
    if copy is not None:  # first, so a ledger never exists without the project's copy of the tool
        note, keep = copy
        try:
            if keep:
                write_atomic(home.with_name("ledger.py.bak"), home.read_bytes())
            write_atomic(home, Path(__file__).resolve().read_bytes())
        except OSError as e:
            sys.exit(f"init: could not copy ledger.py to {home} ({e}); nothing else was changed")
        if note:
            print(note)
        print(f"copied ledger.py to {home}")
    if fresh:
        build_ledger(root, db)
        set_wal(db)
        optimize_db(db)
        print(f"created {db}")
        print(f"ledger at {db} (created)")
    else:
        con = open_ledger(db, write=True)
        v = ledger_version(con, db)
        con.isolation_level = None
        moved = None
        try:
            con.execute("BEGIN IMMEDIATE")
            notes = sync_views_and_triggers(con)
            problems = verify_schema(con)
            if problems:
                con.execute("ROLLBACK")
                con.close()
                sys.exit(drift_message(db, problems))
            stamp_meta(con, created_by=UNKNOWN_CREATOR)
            was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
            if was is None or was[0] != str(db.parent):
                moved = was[0] if was else None
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('ledger_location',?)", (str(db.parent),))
            con.execute("COMMIT")
        except sqlite3.Error as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()
            sys.exit(f"init: could not update {db}: {e}")
        con.close()
        set_wal(db)
        optimize_db(db)
        for note in notes:
            print(f"note: {note}")
        if moved:
            print(f"note: the ledger was last set up at {moved}; it now records {db.parent}")
        print(f"{db} already exists (schema {v})")
        print(f"ledger at {db} (attached to existing)")
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), db.with_suffix(".sql"),
              *db.parent.glob(db.name + ".bak-v*")):
        make_private(p)
    if db.parent.name == ".code-review" and (db.parent / "patches").is_dir():
        make_private(db.parent / "patches", 0o700)
        for p in (db.parent / "patches").iterdir():
            make_private(p)
    if gitignore:
        gi = db.parent / ".gitignore"
        try:
            if not gi.exists():
                write_atomic(gi, GITIGNORE.encode(), 0o600)
            else:
                text = gi.read_text(encoding="utf-8", errors="replace")
                new, added = repaired_gitignore(text)
                if new != text:
                    write_atomic(gi, new.encode(), 0o600)
                    print(f"repaired {gi}" + (f": added {', '.join(added)}" if added else ""))
        except OSError as e:
            sys.exit(f"init: could not write {gi} ({e})")


def _owner_problem(path):
    """Why a found ledger (file or folder) must not be used without --root: another user's, or writable by
    everyone. None when it is fine (or the platform has no user ids)."""
    if not hasattr(os, "getuid"):
        return None
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    if st.st_uid != os.getuid():
        return f"{path} belongs to another user (uid {st.st_uid})"
    if st.st_mode & 0o002:
        return f"{path} can be changed by every user"
    return None


def _check_owner(ledger_dir):
    for p in (ledger_dir / "ledger.sqlite", ledger_dir):
        why = _owner_problem(p)
        if why:
            sys.exit(f"{why}; refusing to use that ledger. Pass --root DIR (and --db FILE) to choose the project "
                     f"and ledger explicitly.")


def _git_tops(start):
    """(worktree top, main checkout top, note) for start. Outside git both tops are None; note says why git could
    not be asked (missing, timed out, failed), else None."""
    try:
        p = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel", "--git-common-dir"],
                           capture_output=True, timeout=10)
    except FileNotFoundError:
        return None, None, "git not available"
    except subprocess.TimeoutExpired:
        return None, None, "git timed out"
    except OSError as e:
        return None, None, f"git could not run ({e})"
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        if "not a git repository" in err:
            return None, None, None
        return None, None, f"git failed ({err.splitlines()[0] if err else p.returncode})"
    out = p.stdout.decode("utf-8", "replace").splitlines()
    if not out:
        return None, None, "git gave no top folder"
    top = Path(out[0]).resolve()
    main = top
    if len(out) > 1:
        common = Path(out[1])
        common = (common if common.is_absolute() else start / common).resolve()
        if common.name == ".git":
            main = common.parent
    return top, main, None


def _lists_repo(ledger_root, tops):
    """True when the ledger at ledger_root/.code-review lists one of tops in repositories.path."""
    db = ledger_root / ".code-review" / "ledger.sqlite"
    try:
        con = connect(db, readonly=True)
        try:
            paths = [r[0] for r in con.execute("SELECT path FROM repositories")]
        finally:
            con.close()
    except sqlite3.Error:
        return False
    for p in paths:
        try:
            if (ledger_root / (p or ".")).resolve() in tops:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def resolve_root(start, explicit=False, script=None):
    """(project root, how it was found). how is 'explicit' (--root given), 'script' (this ledger.py is the copy in a
    project's .code-review/), 'ledger' (an existing ledger found upward), 'git' (the top of the git repository) or
    'start' (no git and no ledger: the start folder; init then requires --root).

    Inside git the walk upward for an existing .code-review/ledger.sqlite stops at the repository top; for a git
    worktree the main checkout's ledger is used; a ledger above the repository top is used only when its
    repositories table lists that top. Outside git (or when git cannot run) the nearest ledger upward is used.
    A found ledger owned by another user, or writable by everyone, is refused."""
    start = Path(start)
    if explicit:
        return start, "explicit"
    if script is not None:
        here = Path(script).resolve().parent
        if here.name == ".code-review" and (here / "ledger.sqlite").exists():
            _check_owner(here)
            return here.parent, "script"

    def has(d):
        return (d / ".code-review" / "ledger.sqlite").exists()

    top, main, note = _git_tops(start)
    if top is None:
        for d in (start, *start.parents):
            if has(d):
                _check_owner(d / ".code-review")
                return d, "ledger"
        if note:
            print(f"note: {note}; using {start} as the project root", file=sys.stderr)
        return start, "start"
    for d in (start, *start.parents):
        if has(d):
            _check_owner(d / ".code-review")
            return d, "ledger"
        if d == top or top not in d.parents:
            break
    if main != top and has(main):
        _check_owner(main / ".code-review")
        return main, "ledger"
    for d in main.parents:
        if has(d):
            if _lists_repo(d, {top, main}):
                _check_owner(d / ".code-review")
                return d, "ledger"
            break
    return main, "git"


def find_root(start, explicit=False, script=None):
    """The project root for start (see resolve_root)."""
    return resolve_root(start, explicit, script)[0]


TIME_FORMAT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")  # what the tr_time_* triggers accept
_ISO_TIME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d+))?)?\s*(Z|z|[+-]\d\d:?\d\d)?$")


def now():
    """The current time as the ledger stores every time: UTC with milliseconds, the same text as SQLite's
    strftime('%Y-%m-%dT%H:%M:%fZ','now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_time(text):
    """An ISO-8601 time with any offset (git's %cI: 2026-11-10T10:00:00+02:00, or ...Z) in the ledger's format,
    converted to UTC (2026-11-10T08:00:00.000Z). A time without an offset is taken as UTC. ValueError if unreadable."""
    m = _ISO_TIME.match((text or "").strip())
    if not m:
        raise ValueError(f"not an ISO-8601 time: {text!r}")
    y, mo, d, h, mi, sec, frac, zone = m.groups()
    t = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0),
                 int(((frac or "") + "000000")[:6]), tzinfo=timezone.utc)
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        hh, mm = zone[1:3], zone[-2:]
        t -= sign * timedelta(hours=int(hh), minutes=int(mm))
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


# The checks live in the ledger's ledger_checks table (view, label, tier, position): tier 'error' views must be empty
# (check exits 1 otherwise), tier 'warning' views are listed but do not fail. The error tier is the twin of the
# must-be-empty list in SKILL.md (Rules for the ledger / check) and of the 'must be empty' comments in
# references/schema.sql; dev/tests/test_check.py enforces that they agree.
def ledger_checks(con, tier=None):
    """[(view, label, tier)] of the ledger's ledger_checks rows in position order (only `tier` if given); None when
    the ledger has no ledger_checks table (an older schema, which check_schema reports)."""
    try:
        got = [tuple(r) for r in con.execute("SELECT view, label, tier FROM ledger_checks ORDER BY position, view")]
    except sqlite3.Error:
        return None
    return [c for c in got if tier is None or c[2] == tier]


def count_view(con, view):
    return con.execute(f"SELECT count(*) FROM {quote_name(view)}").fetchone()[0]


def check(con, quiet=False, full=False):
    """Print what check finds; return the number of failed checks (integrity, foreign keys, schema, each non-empty
    error-tier view). A non-empty warning-tier view is printed with 'warning' and does not count. The integrity
    pass is PRAGMA quick_check (no index-content comparison, so fast on a large ledger) unless full, which runs the
    full PRAGMA integrity_check (`check --full`); the line printed names the one that ran."""
    bad = 0
    pragma = "integrity_check" if full else "quick_check"
    ic = con.execute(f"PRAGMA {pragma}").fetchone()[0]
    if ic != "ok":
        print(f"{pragma}:", ic); bad += 1
    elif not quiet:
        print(f"{pragma}: ok" + ("" if full else " (check --full runs the full integrity_check)"))
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        print(f"foreign_key_check: {len(fk)} broken references"); bad += 1
    bad += check_schema(con)
    checks = ledger_checks(con)
    if checks is None:
        print("ledger_checks: the ledger has no ledger_checks table, so its views were not checked "
              "(run: python3 ledger.py migrate)")
        return bad + 1
    for view, label, tier in checks:
        warn = "  warning" if tier == "warning" else ""
        try:
            n = count_view(con, view)
        except sqlite3.Error as e:
            bad += tier == "error"
            print(f"  ERR  {label}  ({view}){warn}: {e}")
            continue
        if n:
            bad += tier == "error"
            print(f"{n:5d}  {label}  ({view}){warn}")
            for r in rows(con, f"SELECT * FROM {quote_name(view)} LIMIT 5"):
                print("       ", json.dumps(r, ensure_ascii=False)[:200])
        elif not quiet:
            print(f"    0  {label}")
    optimize(con)
    return bad


def optimize_db(db):
    """PRAGMA optimize on the ledger file (after init)."""
    try:
        con = connect(db)
    except sqlite3.Error:
        return
    try:
        optimize(con)
    finally:
        con.close()


def optimize(con):
    """PRAGMA optimize: lets SQLite gather the statistics its query planner uses (cheap; skipped on a read-only
    connection or a busy ledger)."""
    try:
        con.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass


def check_schema(con):
    """Part of check: the ledger's schema_version must be SCHEMA_VERSION and its stored schema must match the shipped
    one object by object. Prints each problem; returns the number of failed checks (0 or 1). Silent when clean."""
    try:
        v = ledger_version(con, "the ledger")
    except LedgerError as e:
        print(f"schema_version: {e}")
        return 1
    if v != SCHEMA_VERSION:
        how = "run: python3 ledger.py migrate" if BASELINE_VERSION <= v < SCHEMA_VERSION else "use a matching ledger.py"
        print(f"schema_version: the ledger is schema {v}; this ledger.py is schema {SCHEMA_VERSION} ({how})")
        return 1
    problems = verify_schema(con)
    if problems:
        print(f"schema: {len(problems)} differences from the shipped schema (init with the skill's ledger.py "
              f"restores views and triggers)")
        for line in problems:
            print("       ", line)
        return 1
    return 0


# ---------------------------------------------------------------- git

def repo_dir(root, repo_path, allow_outside=False):
    """The folder of a repositories row: root/path, resolved (symlinks too). It must be the project root or below it
    unless the repository is listed explicitly as outside the root (repositories.allow_outside_root, D-0007)."""
    top = Path(root).resolve()
    d = (top / (repo_path or ".")).resolve()
    if not allow_outside and d != top and top not in d.parents:
        raise RuntimeError("repository path outside the project root")
    return d


def bad_location_path(path):
    """True for a finding_locations.path that must not reach git: empty, absolute, a backslash, a leading -, a ..
    segment, or a control character (the schema CHECK refuses the same on insert)."""
    return (not path or path.startswith(("/", "-")) or "\\" in path or ".." in path.split("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in path))


def git(cwd, *args, timeout=15):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return p.stdout


def file_at(root, repo_path, sha, path, allow_outside=False, raw=False):
    """The file's text at commit sha (raw=True: its bytes). A refused path gives the fixed error 'bad path' before
    git runs, so no message says whether some file outside the repository exists."""
    if not HEX.match(sha or ""):
        raise RuntimeError("commit is not a hex hash")
    if bad_location_path(path or ""):
        raise RuntimeError("bad path")
    data = git(repo_dir(root, repo_path, allow_outside), "show", f"{sha}:{path}")
    return data if raw else data.decode("utf-8", "replace")


def location(con, loc_id):
    loc = one(con, """SELECT fl.*, rp.path AS repo_path, rp.name AS repo_name, rp.permalink_template
                      FROM finding_locations fl JOIN repositories rp ON rp.id = fl.repository_id
                      WHERE fl.id = ?""", (loc_id,))
    if loc is not None:  # allow_outside_root is schema 2; an older ledger read as it is has none (so: not allowed)
        try:
            loc["allow_outside_root"] = con.execute("SELECT allow_outside_root FROM repositories WHERE id=?",
                                                    (loc["repository_id"],)).fetchone()[0]
        except sqlite3.OperationalError:
            loc["allow_outside_root"] = 0
    return loc


# Common secret shapes masked in excerpts by fill-refs (SEC-M7). Each match keeps its name and quotes and has the
# value replaced by REDACTED; line breaks are kept so the excerpt still lines up with line_start..line_end.
REDACTED = "[redacted]"
_SECRET_NAME = r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?key|credentials?)[A-Za-z0-9_.-]*"
SECRET_PATTERNS = [
    # PEM blocks (private keys and the like): the lines between BEGIN and END (or the excerpt's end)
    re.compile(r"(-----BEGIN [A-Z0-9 ]+-----)(.*?)(-----END [A-Z0-9 ]+-----|\Z)", re.S),
    # name = "value" / name: 'value' / "name": "value" (any case), a quoted literal of 4+ characters
    re.compile(r"(?i)(\b" + _SECRET_NAME + r"[\"']?\s*(?:=|:|=>)\s*)([\"'])([^\"'\n]{4,})(\2)"),
    # NAME=value lines of env files and shell (upper-case names), unquoted
    re.compile(r"(?m)^(\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
               r"CREDENTIALS?)[A-Z0-9_]*\s*=\s*)([^\s'\"#][^\s#]{3,})(\s*)$"),
    # well-known key shapes
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                     # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),                 # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                    # Slack tokens
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),      # Stripe
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),                           # API keys of the sk-... shape
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                         # Google API keys
]


def mask_secrets(text):
    """(text with secret-looking values replaced by [redacted], whether anything was masked)."""
    out = text

    def pem(m):
        inner = m.group(2)
        body = "\n".join(REDACTED if l.strip() else l for l in inner.split("\n"))
        return m.group(1) + body + m.group(3)

    out = SECRET_PATTERNS[0].sub(pem, out)
    out = SECRET_PATTERNS[1].sub(lambda m: m.group(1) + m.group(2) + REDACTED + m.group(4), out)
    out = SECRET_PATTERNS[2].sub(lambda m: m.group(1) + REDACTED + m.group(3), out)
    for p in SECRET_PATTERNS[3:]:
        out = p.sub(REDACTED, out)
    return out, out != text


def source(root, con, loc_id, context=8):
    loc = location(con, loc_id)
    if not loc:
        return {"error": "no such reference"}
    out = {k: loc[k] for k in ("path", "commit_sha", "line_start", "line_end", "symbol",
                               "excerpt", "permalink", "repo_name", "blob_sha")}
    a, b = loc["line_start"], loc["line_end"] or loc["line_start"]
    # One command for show, /api/source and the viewer: v_code_refs builds it with every argument shell-quoted.
    r = con.execute("SELECT git_command FROM v_code_refs WHERE location_id=?", (loc_id,)).fetchone()
    out["git_command"] = r[0] if r else None
    out["excerpt_redacted"] = bool(loc.get("excerpt_redacted"))
    try:
        text = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"],
                       bool(loc.get("allow_outside_root"))).splitlines()
        lo = max(1, (a or 1) - context)
        hi = min(len(text), (b or a or min(len(text), 40)) + context)
        out["lines"] = [{"n": i, "text": text[i - 1]} for i in range(lo, hi + 1)]
        out["source"] = "git"
        if loc["excerpt"] is not None and a:
            real = "\n".join(text[a - 1:b])
            if loc.get("excerpt_redacted"):  # the excerpt is masked; its hash is of the real lines
                out["excerpt_matches"] = hashlib.sha256(real.encode()).hexdigest() == loc["excerpt_sha256"]
            else:
                out["excerpt_matches"] = real == loc["excerpt"].rstrip("\n")
    except Exception as e:  # history rewritten, repo missing, or not a git checkout
        out["git_error"] = str(e)
        out["source"] = "excerpt" if loc["excerpt"] is not None else "none"
        if loc["excerpt"] is not None and a:
            out["lines"] = [{"n": a + i, "text": t} for i, t in enumerate(loc["excerpt"].split("\n"))]
    return out


WHOLE_FILE_EXCERPT_LINES = 200  # a whole-file reference keeps at most this many lines (excerpt_status 'truncated')
_DEFINITION = (r"(?i:\b(?:def|class|function|interface|struct|enum|fn|func|"
               r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|VIEW|TRIGGER|INDEX)(?:\s+IF\s+NOT\s+EXISTS)?))\s+")


def symbol_line(lines, symbol):
    """(line number, None) for the one line that defines symbol, else (None, why). A definition is a def, class,
    function, interface, struct, enum, fn, func or SQL CREATE TABLE/VIEW/TRIGGER/INDEX followed by the symbol as a
    whole word; with no definition, the one line holding the symbol as a whole word. Several candidates, or none,
    leave the reference unlined (the caller counts it as failed)."""
    word = r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])"
    hits = [i for i, t in enumerate(lines, 1) if re.search(_DEFINITION + word, t)]
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, (f"symbol {symbol!r} is defined on {len(hits)} lines "
                      f"({', '.join(map(str, hits[:5]))}); give the lines")
    hits = [i for i, t in enumerate(lines, 1) if re.search(word, t)]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"symbol {symbol!r} not found"
    return None, (f"symbol {symbol!r} is on {len(hits)} lines ({', '.join(map(str, hits[:5]))}) and defined on "
                  f"none; give the lines")


def permalink_for(template, sha, path, a, b):
    """The permalink from repositories.permalink_template, or None. With no lines (a whole file), the #fragment that
    holds {start} or {end} is dropped and any other {start}/{end} is left out of the link."""
    link = template.replace("{sha}", sha).replace("{path}", path)
    if a is None:
        head, sep, frag = link.partition("#")
        if "{start}" in frag or "{end}" in frag:
            link = head
        if "{start}" in link or "{end}" in link:
            return None
        return link
    return link.replace("{start}", str(a)).replace("{end}", str(b or a))


def fill_refs(root, con, review_code=None):
    """Fill excerpt, excerpt_sha256, blob_sha and permalink (and lines from a symbol) of each reference from git.
    Never overwrites an excerpt. A failure (file unreadable, lines past the end, a symbol not found or ambiguous, an
    empty file) writes nothing for that reference and is counted; returns the count."""
    sql = """SELECT fl.id FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
             JOIN reviews r ON r.id = f.review_id
             WHERE fl.commit_sha IS NOT NULL
               AND ((fl.excerpt IS NULL AND fl.excerpt_status IS NOT 'not-text') OR fl.blob_sha IS NULL
                    OR fl.permalink IS NULL OR (fl.line_start IS NULL AND fl.whole_file = 0))"""
    args = ()
    if review_code:
        sql += " AND r.code = ?"; args = (review_code,)
    failed = 0
    updates = []  # every git call happens first; the writes then go in one short transaction (DB-M8)
    for (lid,) in con.execute(sql, args).fetchall():
        loc = location(con, lid)
        where = f"ref {lid} {loc['path']}@{loc['commit_sha'][:10]}"
        outside = bool(loc["allow_outside_root"])
        try:
            data = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"], outside, raw=True)
        except Exception as e:
            print(f"{where}: {e}"); failed += 1; continue
        try:
            text = None if b"\0" in data else data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        a, b = loc["line_start"], loc["line_end"]
        if a is None and b is not None:
            print(f"{where}: line_end {b} without line_start"); failed += 1; continue
        whole = bool(loc["whole_file"]) or (a is None and not loc["symbol"])
        upd = {"whole_file": 1} if whole and not loc["whole_file"] else {}
        if text is None:  # binary or not UTF-8: no lossy excerpt; the hash is of the file's bytes
            if loc["excerpt"] is None and loc["excerpt_status"] != "not-text":
                upd.update(excerpt_status="not-text", excerpt_sha256=hashlib.sha256(data).hexdigest())
            if a is None and not whole:
                print(f"{where}: not a text file; symbol {loc['symbol']!r} cannot be looked up"); failed += 1; continue
        else:
            lines = text.splitlines()
            if whole and a is None:
                if not lines:
                    print(f"{where}: the file is empty; nothing to cite"); failed += 1; continue
                a, b = 1, len(lines)
            elif a is None:
                a, why = symbol_line(lines, loc["symbol"])
                if a is None:
                    print(f"{where}: {why}"); failed += 1; continue
                b = a
            b = b or a
            if b > len(lines):
                print(f"{where}: lines {a}-{b} are past the end of the file ({len(lines)} lines)")
                failed += 1; continue
            upd.update(line_start=a, line_end=b)
            if loc["excerpt"] is None:
                cut = whole and b - a + 1 > WHOLE_FILE_EXCERPT_LINES
                excerpt = "\n".join(lines[a - 1:(a - 1 + WHOLE_FILE_EXCERPT_LINES) if cut else b])
                shown, masked = mask_secrets(excerpt)
                upd.update(excerpt=shown, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
                           excerpt_redacted=1 if masked else 0)
                if cut:
                    upd["excerpt_status"] = "truncated"
        if loc["blob_sha"] is None:
            try:
                upd["blob_sha"] = git(repo_dir(root, loc["repo_path"], outside), "rev-parse",
                                      f"{loc['commit_sha']}:{loc['path']}").decode().strip()
            except Exception:
                pass
        if loc["permalink"] is None and loc["permalink_template"]:
            link = permalink_for(loc["permalink_template"], loc["commit_sha"], loc["path"],
                                 None if whole else a, None if whole else b)
            if link and SAFE_URL.match(link):
                upd["permalink"] = link
            else:
                print(f"{where}: the permalink template does not give an http(s) link; no permalink written")
        if upd:
            updates.append((lid, upd))
    if updates:
        if not con.in_transaction:  # a caller's own open transaction is simply continued
            con.execute("BEGIN IMMEDIATE")
        try:
            for lid, upd in updates:
                con.execute(f"UPDATE finding_locations SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                            (*upd.values(), lid))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    print(f"filled {len(updates)} references, {failed} could not be read from git")
    return failed


def existing_tag(d, tag):
    """The commit a tag points at in repository d, or None when there is no such tag. Any other git problem (not a
    repository, git missing) raises RuntimeError."""
    if not d.is_dir():
        raise RuntimeError(f"no folder {d}")
    git(d, "rev-parse", "--git-dir")  # raises when d is not a git repository
    p = subprocess.run(["git", "-C", str(d), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                       capture_output=True, timeout=15)
    if p.returncode == 0:
        return p.stdout.decode().strip()
    if p.returncode == 1 and not p.stdout.strip():
        return None
    raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "git rev-parse failed")


def tag_review(root, con, code):
    """Create the local annotated tag review/<code> on each reviewed commit and record it. Tags stay local
    (D-0002): nothing is pushed. Returns 1 when a repository is left untagged, else 0."""
    if not REVIEW_CODE.fullmatch(code or ""):
        sys.exit(f"tag-review: {code!r} is not a review code like R-0001")
    rv = one(con, "SELECT id FROM reviews WHERE code=?", (code,))
    if not rv:
        sys.exit(f"no review {code}")
    tag = f"review/{code}"
    repos = rows(con, """SELECT rr.repository_id, rr.commit_sha, rp.path, rp.name, rp.allow_outside_root
                         FROM review_repositories rr JOIN repositories rp ON rp.id = rr.repository_id
                         WHERE rr.review_id=? ORDER BY rp.id""", (rv["id"],))
    if not repos:
        sys.exit(f"review {code} has no repositories; nothing to tag (record them in review_repositories)")
    left = 0
    for r in repos:
        sha = r["commit_sha"] or ""
        if not FULL_HEX.fullmatch(sha):
            print(f"{r['name']}: commit {sha!r} is not a full commit hash; not tagged", file=sys.stderr)
            left += 1; continue
        try:
            d = repo_dir(root, r["path"], bool(r["allow_outside_root"]))
            existing = existing_tag(d, tag)
            if existing and existing != sha:
                print(f"{r['name']}: tag {tag} already points at {existing[:10]}; left alone")
                left += 1; continue
            if not existing:
                git(d, "tag", "-a", "-m", f"Commit reviewed in {code}", "--", tag, sha)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"{r['name']}: not tagged: {e}", file=sys.stderr)
            left += 1; continue
        # recorded right away, in its own short transaction, so a later repository's failure cannot lose it; an
        # empty commit_time is filled from git, in UTC like every time in the ledger
        try:
            when = utc_time(git(d, "show", "-s", "--format=%cI", sha).decode().strip())
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            when = None
        con.execute("UPDATE review_repositories SET tag_name=?, commit_time=COALESCE(commit_time, ?) "
                    "WHERE review_id=? AND repository_id=?", (tag, when, rv["id"], r["repository_id"]))
        con.commit()
        hint = f"git -C {shlex.quote(r['path'] or '.')} push origin {shlex.quote(tag)}"
        print(f"{r['name']}: {tag} -> {sha[:10]}  (push with: {hint})")
    if left:
        print(f"tag-review {code}: {left} of {len(repos)} repositories not tagged", file=sys.stderr)
    return 1 if left else 0


def check_git_refs(root, con, quiet=False):
    """Part of `check`: each reviewed commit must exist in its repository (git cat-file -e) and each recorded review
    tag must point at it (refs/tags/<tag>). A repository folder that is not here (the ledger was copied elsewhere)
    is noted and not counted. Returns the number of problems."""
    bad = skipped = 0
    try:
        found = rows(con, """SELECT rv.code, rp.name, rp.path, rp.allow_outside_root, rr.commit_sha, rr.tag_name
                             FROM review_repositories rr JOIN reviews rv ON rv.id = rr.review_id
                             JOIN repositories rp ON rp.id = rr.repository_id ORDER BY rv.id, rp.id""")
    except sqlite3.Error as e:  # an older schema (check_schema has already reported it)
        print(f"    note: reviewed commits and tags were not checked ({e})")
        return 0
    for r in found:
        try:
            d = repo_dir(root, r["path"], bool(r["allow_outside_root"]))
            if not d.is_dir():
                raise FileNotFoundError(d)
            git(d, "rev-parse", "--git-dir")
        except (RuntimeError, OSError, subprocess.SubprocessError):
            skipped += 1
            continue
        what = f"{r['code']} {r['name']} {r['commit_sha'][:10]}"
        try:
            git(d, "cat-file", "-e", f"{r['commit_sha']}^{{commit}}")
        except (RuntimeError, subprocess.SubprocessError):
            print(f"git: {what}: the reviewed commit is not in the repository"); bad += 1
            continue
        if r["tag_name"]:
            try:
                at = existing_tag(d, r["tag_name"])
            except (RuntimeError, subprocess.SubprocessError) as e:
                at = f"unreadable ({e})"
            if at != r["commit_sha"]:
                print(f"git: {what}: tag {r['tag_name']} " + ("is missing" if at is None else f"points at {at[:10]}"))
                bad += 1
    if skipped and not quiet:
        print(f"    note: {skipped} reviewed repositories are not here; their commits and tags were not checked")
    elif not bad and not quiet:
        print("    0  Reviewed commits missing or review tags moved")
    return bad


TEXT_TARGETS = {"set-report": ("reviews", "report_md", "review", REVIEW_CODE),
                "set-narrative": ("plans", "narrative_md", "plan", re.compile(r"P-[0-9]{4,}"))}


def set_text(con, cmd, code, source):
    """set-report R-0007 FILE / set-narrative P-0001 FILE: store the review document or the plan's narrative
    (Markdown, UTF-8; FILE '-' reads standard input) in the ledger, where the viewer shows it. Returns 0."""
    table, column, what, pattern = TEXT_TARGETS[cmd]
    if not pattern.fullmatch(code or ""):
        sys.exit(f"{cmd}: {code!r} is not a {what} code")
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"{cmd}: cannot read {source}: {e}")
    cur = con.execute(f"UPDATE {table} SET {column}=? WHERE code=?", (text, code))
    if cur.rowcount != 1:
        con.rollback()
        sys.exit(f"{cmd}: no {what} {code}")
    con.commit()
    print(f"{code}: stored {len(text):,} characters in {table}.{column}")
    return 0


DUMP_HEADER = "-- ledger.py dump: a text copy of the ledger for diffs; rebuild with: sqlite3 new.sqlite < ledger.sql"


def dump(con, db):
    """Write <db>.sql: one consistent snapshot (a read transaction around iterdump), written to <db>.sql.tmp,
    fsynced and renamed over the old dump, so a failed dump leaves the previous one intact. A WAL ledger is
    checkpointed first so the .sqlite file itself holds every commit. Refuses to overwrite a .sql file that this
    command did not write (its first line is not DUMP_HEADER, nor iterdump's own first line, which older dumps had)."""
    out = db.with_suffix(".sql")
    if out.exists():
        with open(out, encoding="utf-8", errors="replace") as f:
            first = f.readline().rstrip("\n")
        if first not in (DUMP_HEADER, "BEGIN TRANSACTION;"):
            sys.exit(f"{out} was not written by ledger.py dump (its first line is not the dump header); left it "
                     f"unchanged. Move it aside, or pass --db for a ledger whose .sql name is free.")
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass  # a reader elsewhere: the dump below is still complete
    tmp = out.with_name(out.name + ".tmp")
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN")
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()  # the snapshot starts here
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(DUMP_HEADER + "\n")
            for line in con.iterdump():
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(out))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        sys.exit(f"dump: could not write {out} ({e}); the previous dump is unchanged")
    finally:
        con.rollback()
    print(f"wrote {out}")


# ---------------------------------------------------------------- viewer API

LABEL_TABLES = ("domains", "themes")
# The lookup tables of the ledger's words (schema 6), by the name their labels have in /api/overview labels.enums.
LOOKUP_TABLES = (("issue_status", "issue_statuses"), ("severity", "severities"), ("match_type", "match_types"),
                 ("relation_type", "relation_types"), ("gate_kind", "gate_kinds"))
ACTIVE = "(SELECT value FROM issue_statuses WHERE active = 1)"  # issue statuses still to fix


def enum_labels(con):
    """{enum: {value: label}} in display order, from the lookup tables and enum_labels (plus relation_type_inverse,
    the labels read from the issue a relation points to). Empty for a ledger older than schema 6: the page then
    falls back to its own wording."""
    out = {}
    try:
        for name, table in LOOKUP_TABLES:
            out[name] = {r[0]: r[1] for r in con.execute(f"SELECT value, label FROM {table} ORDER BY position, value")}
        out["relation_type_inverse"] = {r[0]: r[1] for r in con.execute(
            "SELECT value, inverse_label FROM relation_types ORDER BY position, value")}
        for enum, value, label in con.execute("SELECT enum, value, label FROM enum_labels ORDER BY enum, position"):
            out.setdefault(enum, {})[value] = label
    except sqlite3.Error:
        return {}
    return out


ROUTE_SECONDS = 3  # a viewer page read that runs longer is stopped (the SQL box has its own SQL_SECONDS)
ANSWER_BUSY_MS = 3000  # how long a gate answer waits for another writer before it reports the ledger busy
TOO_LONG = "The query took too long (over {} s); add a LIMIT or a WHERE."


def with_deadline(con, seconds):
    """Stop any statement on con that runs past `seconds` from now (it raises OperationalError 'interrupted').
    Returns a function that tells whether the deadline was what stopped it."""
    from time import monotonic
    end = monotonic() + seconds
    con.set_progress_handler(lambda: 1 if monotonic() > end else 0, 10_000)
    return lambda: monotonic() > end


def api(root, db, path, qs, body):
    """One viewer API request. Each request opens its own connection and closes it when the answer is built:
    sqlite3 connections are not shared between the server's threads, and a fresh one always sees the agent's latest
    commit. That connection is read-only (mode=ro, query_only) for every route and stops after ROUTE_SECONDS; only
    the gate answer route opens a second, writable connection of its own. A missing ledger is a 503 and is never
    created."""
    if not Path(db).exists():
        return {"error": "ledger not found"}, 503
    con = connect(db, readonly=True)
    timed_out = with_deadline(con, ROUTE_SECONDS)
    try:
        return route(root, db, con, path, qs, body)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e) and timed_out():
            return {"error": TOO_LONG.format(ROUTE_SECONDS)}, 400
        raise
    finally:
        con.close()


def answer_gate(db, code, body):
    """The owner's answer to an open gate, from the viewer. Read and write happen in one BEGIN IMMEDIATE transaction
    on a writable connection of its own, so two answers cannot both be reported saved: the loser, or an answer to a
    gate answered meanwhile (in chat, say), gets 409. If another writer holds the ledger past ANSWER_BUSY_MS the
    answer is not saved and the owner is told to try again (503); the page keeps what they typed."""
    busy = {"error": "The ledger is busy; your answer was not saved, try again."}, 503
    try:
        w = connect(db)
    except sqlite3.OperationalError:
        return {"error": "ledger not found"}, 503
    w.isolation_level = None
    try:
        w.execute(f"PRAGMA busy_timeout = {int(ANSWER_BUSY_MS)}")
        try:
            w.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                return busy
            raise
        try:
            g = one(w, "SELECT * FROM gates WHERE code=?", (code,))
            if not g:
                return {"error": "no such gate"}, 404
            if g["status"] != "open":
                return {"error": "This question was already answered."}, 409
            val = (body.get("answer_value") or "").strip() or None
            txt = (body.get("answer_text") or "").strip() or None
            opts = [o.get("value") for o in json.loads(g["options"] or "[]")]
            if val is not None and val not in opts:
                return {"error": "That choice is not one of the options."}, 400
            if val is None and not txt:
                return {"error": "Choose an option or write an answer."}, 400
            if txt and not g["allow_text"] and val is None:
                return {"error": "Choose one of the options."}, 400
            # who answered, as an actor (schema 3): the project's owner, made on first use if the ledger has none
            owner = w.execute("SELECT min(id) FROM actors WHERE kind='owner'").fetchone()[0]
            if owner is None:
                owner = w.execute("INSERT INTO actors(kind, name) VALUES ('owner', 'owner')").lastrowid
            cur = w.execute("""UPDATE gates SET status='answered', answer_value=?, answer_text=?,
                               answered_by='owner', answered_via='viewer', answered_by_actor_id=?
                               WHERE id=? AND status='open'""",
                            (val, txt, owner, g["id"]))
            if cur.rowcount != 1:
                return {"error": "This question was already answered."}, 409
            w.execute("COMMIT")
            return {"ok": True}
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                return busy
            raise
        finally:
            if w.in_transaction:
                w.execute("ROLLBACK")
    finally:
        w.close()


def quote_name(name):
    return '"' + str(name).replace('"', '""') + '"'


def route(root, db, con, path, qs, body):
    parts = [x for x in path.split("/") if x]
    if not parts or parts[0] != "api":
        return {"error": "not found"}, 404
    p = parts[1:]
    arg = lambda k, d=None: qs.get(k, [d])[0]

    if p == ["overview"]:
        meta = {r["key"]: r["value"] for r in rows(con, "SELECT key, value FROM meta")}
        return {
            "project": meta.get("project_name", root.name),
            "schema_version": meta.get("schema_version"),
            "open_by_severity": rows(con, """SELECT severity, count(*) n FROM issues
                WHERE status IN {ACTIVE} GROUP BY severity""".format(ACTIVE=ACTIVE)),
            "issues_by_status": rows(con, "SELECT status, count(*) n FROM issues GROUP BY status"),
            "gates_open": one(con, "SELECT count(*) n FROM gates WHERE status='open'")["n"],
            "queue": one(con, "SELECT count(*) n FROM v_owner_queue")["n"],
            "reviews": rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
                (SELECT count(*) FROM findings f WHERE f.review_id=r.id) points,
                (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='H') high
                FROM reviews r ORDER BY r.started_at DESC LIMIT 5"""),
            "plans": rows(con, """SELECT p.code, p.title, p.status, count(s.id) steps,
                sum(s.status='done') done, sum(s.status='waiting-owner') waiting
                FROM plans p LEFT JOIN steps s ON s.plan_id=p.id GROUP BY p.id ORDER BY p.created_at DESC"""),
            "recurring": one(con, "SELECT count(*) n FROM v_recurring_issues")["n"],
            "checks": [{"label": label, "view": v, "tier": tier, "n": count_view(con, v)}
                       for v, label, tier in (ledger_checks(con) or [])],
            "labels": {**{t: {r["code"]: r["name"] for r in rows(con, f"SELECT code, name FROM {t}")} for t in LABEL_TABLES},
                       "enums": enum_labels(con)},
        }

    if p == ["gates"]:
        st = arg("status", "pending")
        where = {"pending": "g.status IN ('open','answered')", "all": "1"}.get(st, "g.status = ?")
        args = () if st in ("pending", "all") else (st,)
        gates = rows(con, f"""SELECT g.*, r.code review_code, pl.code plan_code, s.number step_number,
                i.code issue_code, d.code decision_code FROM gates g
                LEFT JOIN reviews r ON r.id=g.review_id LEFT JOIN plans pl ON pl.id=g.plan_id
                LEFT JOIN steps s ON s.id=g.step_id LEFT JOIN issues i ON i.id=g.issue_id
                LEFT JOIN decisions d ON d.id=g.decision_id
                WHERE {where} ORDER BY g.status='answered', g.blocking DESC, g.created_at""", args)
        for g in gates:
            g["options"] = json.loads(g["options"]) if g["options"] else []
        other = rows(con, "SELECT * FROM v_owner_queue WHERE kind <> 'gate'")
        return {"gates": gates, "other": other}

    if len(p) == 3 and p[0] == "gates" and p[2] == "answer" and body is not None:
        return answer_gate(db, p[1], body)

    if p == ["reviews"]:
        return rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
            (SELECT group_concat(rp.name || ' ' || substr(rr.commit_sha,1,10) || COALESCE(' on ' || rr.branch, ''), ' · ')
               FROM review_repositories rr JOIN repositories rp ON rp.id=rr.repository_id WHERE rr.review_id=r.id) commits,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id) points,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='H') high,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='M') medium,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='L') low,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN ('new','continuation')) new_issues,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN (SELECT value FROM match_types WHERE came_back = 1)) came_back
            FROM reviews r ORDER BY r.started_at DESC""")

    if len(p) == 2 and p[0] == "review":
        r = one(con, """SELECT r.*, m.name manager, m.model manager_model, a.name approved_by,
                        pr.code previous_code FROM reviews r
                        LEFT JOIN actors m ON m.id=r.manager_actor_id LEFT JOIN actors a ON a.id=r.approved_by_actor_id
                        LEFT JOIN reviews pr ON pr.id=r.previous_review_id WHERE r.code=?""", (p[1],))
        if not r:
            return {"error": "no such review"}, 404
        rid = r["id"]
        findings = rows(con, """SELECT f.*, i.code issue_code, i.status issue_status, rr.label run_label
            FROM findings f LEFT JOIN issues i ON i.id=f.issue_id LEFT JOIN reviewer_runs rr ON rr.id=f.reviewer_run_id
            WHERE f.review_id=? ORDER BY CASE f.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, length(f.point_id), f.point_id""", (rid,))
        locs = rows(con, """SELECT fl.id, fl.finding_id, fl.path, fl.line_start, fl.line_end, fl.symbol, fl.role,
            fl.commit_sha, fl.permalink, rp.name repo FROM finding_locations fl
            JOIN findings f ON f.id=fl.finding_id JOIN repositories rp ON rp.id=fl.repository_id WHERE f.review_id=?""", (rid,))
        by = {}
        for l in locs:
            by.setdefault(l["finding_id"], []).append(l)
        for f in findings:
            f["locations"] = by.get(f["id"], [])
        return {
            "review": r,
            "repos": rows(con, """SELECT rp.name, rr.* FROM review_repositories rr JOIN repositories rp
                                  ON rp.id=rr.repository_id WHERE rr.review_id=?""", (rid,)),
            "goals": rows(con, """SELECT g.code, g.text FROM review_goals rg JOIN design_goals g ON g.id=rg.goal_id
                                  WHERE rg.review_id=? ORDER BY g.code""", (rid,)),
            "domains": rows(con, """SELECT rd.*, d.name, d.category FROM review_domains rd JOIN domains d ON d.code=rd.domain_code
                                    WHERE rd.review_id=? ORDER BY rd.decision, d.category, d.name""", (rid,)),
            "runs": rows(con, """SELECT rr.*, a.name actor, a.model FROM reviewer_runs rr JOIN actors a ON a.id=rr.actor_id
                                 WHERE rr.review_id=? ORDER BY rr.domain_code, rr.label""", (rid,)),
            "checks": rows(con, """SELECT c.outcome, c.evidence, i.code issue_code, i.title, i.status
                                   FROM review_issue_checks c JOIN issues i ON i.id=c.issue_id WHERE c.review_id=?
                                   ORDER BY c.outcome, i.code""", (rid,)),
            "summary": rows(con, "SELECT * FROM v_review_summary WHERE review=?", (p[1],)),
            "agents": rows(con, "SELECT * FROM v_review_agents WHERE review=?", (p[1],)),
            "requirements": rows(con, "SELECT * FROM v_requirement_trace WHERE review=?", (p[1],)),
            "metrics": rows(con, """SELECT m.name, m.scope, m.value, COALESCE(m.unit, md.unit) unit, m.method, m.note,
                COALESCE(md.label, upper(substr(m.name, 1, 1)) || replace(substr(m.name, 2), '_', ' ')) label,
                COALESCE(md.better, 'none') better
                FROM metrics m LEFT JOIN metric_defs md ON md.name=m.name WHERE m.review_id=? ORDER BY m.name, m.scope""", (rid,)),
            "artifacts": rows(con, "SELECT kind, path, url FROM artifacts WHERE review_id=? ORDER BY kind", (rid,)),
            "gates": rows(con, "SELECT code, kind, title, status, answer_value, answer_text FROM gates WHERE review_id=? ORDER BY created_at", (rid,)),
            "findings": findings,
        }

    if p == ["issues"]:
        where, args = ["1"], []
        for k in ("status", "severity", "domain_code"):
            v = arg(k)
            if v == "active" and k == "status":
                where.append(f"i.status IN {ACTIVE}")
            elif v:
                where.append(f"i.{k} = ?"); args.append(v)
        q = arg("q")
        if q:
            where.append("(i.code LIKE ? OR i.title LIKE ? OR i.sub LIKE ? OR EXISTS (SELECT 1 FROM findings f LEFT JOIN finding_locations fl ON fl.finding_id=f.id WHERE f.issue_id=i.id AND (f.point_id LIKE ? OR f.summary LIKE ? OR fl.path LIKE ?)))")
            args += [f"%{q}%"] * 6
        return rows(con, f"""SELECT i.code, i.title, i.severity, i.status, i.domain_code, i.theme_code, i.times_seen,
            i.opened_at, i.updated_at, fr.code first_seen, lr.code last_seen
            FROM issues i JOIN reviews fr ON fr.id=i.first_seen_review_id LEFT JOIN reviews lr ON lr.id=i.last_seen_review_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, i.id LIMIT 2000""", args)

    if len(p) == 2 and p[0] == "issue":
        i = one(con, """SELECT i.*, fr.code first_seen, lr.code last_seen, cr.code last_checked FROM issues i
                        JOIN reviews fr ON fr.id=i.first_seen_review_id LEFT JOIN reviews lr ON lr.id=i.last_seen_review_id
                        LEFT JOIN reviews cr ON cr.id=i.last_checked_review_id WHERE i.code=?""", (p[1],))
        if not i:
            return {"error": "no such issue"}, 404
        iid = i["id"]
        findings = rows(con, """SELECT f.*, r.code review_code, r.started_at review_at FROM findings f
                                JOIN reviews r ON r.id=f.review_id WHERE f.issue_id=? ORDER BY r.started_at""", (iid,))
        for f in findings:
            f["locations"] = rows(con, """SELECT fl.id, fl.path, fl.line_start, fl.line_end, fl.symbol, fl.role, fl.commit_sha,
                fl.permalink, rp.name repo FROM finding_locations fl JOIN repositories rp ON rp.id=fl.repository_id
                WHERE fl.finding_id=?""", (f["id"],))
        return {
            "issue": i,
            "findings": findings,
            "relations": rows(con, """SELECT 'out' dir, r.relation, b.code other, b.title other_title, b.status other_status,
                    r.confidence, r.rationale, rv.code review FROM issue_relations r JOIN issues b ON b.id=r.to_issue_id
                    LEFT JOIN reviews rv ON rv.id=r.established_in_review_id WHERE r.from_issue_id=?
                UNION ALL SELECT 'in', r.relation, a.code, a.title, a.status, r.confidence, r.rationale, rv.code
                    FROM issue_relations r JOIN issues a ON a.id=r.from_issue_id
                    LEFT JOIN reviews rv ON rv.id=r.established_in_review_id WHERE r.to_issue_id=?""", (iid, iid)),
            "checks": rows(con, """SELECT r.code review, r.started_at, c.outcome, c.evidence FROM review_issue_checks c
                                   JOIN reviews r ON r.id=c.review_id WHERE c.issue_id=? ORDER BY r.started_at""", (iid,)),
            "timeline": rows(con, "SELECT * FROM v_issue_timeline WHERE code=?", (p[1],)),
            "guard_tests": rows(con, "SELECT * FROM guard_tests WHERE issue_id=?", (iid,)),
            "decisions": rows(con, """SELECT d.code, d.question, d.answer, d.rationale, d.status, d.decided_at
                                      FROM decision_issues di JOIN decisions d ON d.id=di.decision_id WHERE di.issue_id=?""", (iid,)),
            "steps": rows(con, """SELECT DISTINCT p.code plan, s.number, s.title, s.status, s.progress_note,
                    (SELECT group_concat(substr(sc.commit_sha,1,10), ', ') FROM step_commits sc WHERE sc.step_id=s.id) commits
                    FROM step_findings sf JOIN steps s ON s.id=sf.step_id JOIN plans p ON p.id=s.plan_id
                    JOIN findings f ON f.id=sf.finding_id WHERE f.issue_id=? ORDER BY p.code, s.number""", (iid,)),
        }

    if p == ["plans"]:
        return rows(con, """SELECT p.code, p.title, p.status, p.created_at, p.approved_at, count(s.id) steps,
            sum(s.status='done') done, sum(s.status='doing') doing, sum(s.status='waiting-owner') waiting,
            (SELECT group_concat(r.code, ', ') FROM plan_reviews pr JOIN reviews r ON r.id=pr.review_id WHERE pr.plan_id=p.id) reviews
            FROM plans p LEFT JOIN steps s ON s.plan_id=p.id GROUP BY p.id ORDER BY p.created_at DESC""")

    if len(p) == 2 and p[0] == "plan":
        pl = one(con, "SELECT * FROM plans WHERE code=?", (p[1],))
        if not pl:
            return {"error": "no such plan"}, 404
        pid = pl["id"]
        steps = rows(con, """SELECT s.*, ph.number phase FROM steps s JOIN phases ph ON ph.id=s.phase_id
                             WHERE s.plan_id=? ORDER BY s.number""", (pid,))
        deps, closes, commits = {}, {}, {}
        for d in rows(con, """SELECT sd.step_id, s.number FROM step_dependencies sd JOIN steps s ON s.id=sd.depends_on_step_id
                              WHERE s.plan_id=?""", (pid,)):
            deps.setdefault(d["step_id"], []).append(d["number"])
        for c in rows(con, """SELECT sf.step_id, f.point_id, f.severity, f.summary, r.code review, i.code issue
                              FROM step_findings sf JOIN steps s ON s.id=sf.step_id JOIN findings f ON f.id=sf.finding_id
                              JOIN reviews r ON r.id=f.review_id LEFT JOIN issues i ON i.id=f.issue_id WHERE s.plan_id=?""", (pid,)):
            closes.setdefault(c["step_id"], []).append(c)
        for c in rows(con, """SELECT sc.step_id, sc.commit_sha, sc.branch, sc.pr_url, sc.summary FROM step_commits sc
                              JOIN steps s ON s.id=sc.step_id WHERE s.plan_id=?""", (pid,)):
            commits.setdefault(c["step_id"], []).append(c)
        for s in steps:
            s["depends"] = sorted(deps.get(s["id"], []))
            s["closes"] = closes.get(s["id"], [])
            s["commits"] = commits.get(s["id"], [])
        return {"plan": pl,
                "phases": rows(con, "SELECT * FROM v_plan_progress WHERE plan=?", (p[1],)),
                "phase_rows": rows(con, "SELECT number, title, goal, why FROM phases WHERE plan_id=? ORDER BY number", (pid,)),
                "steps": steps}

    if p == ["trends"]:
        return {"metrics": rows(con, "SELECT * FROM v_metric_trends"),
                "reviews": rows(con, """SELECT review, min(started_at) started_at, sum(points) points, sum(high) high,
                    sum(medium) medium, sum(low) low, sum(new_issues) new_issues, sum(carried_over) carried_over,
                    sum(came_back) came_back, sum(false_findings) false_findings
                    FROM v_review_summary GROUP BY review ORDER BY started_at"""),
                "recurring": rows(con, "SELECT * FROM v_recurring_issues LIMIT 50"),
                "hotspots": rows(con, "SELECT * FROM v_file_hotspots LIMIT 30")}

    if p == ["tables"]:
        out = []
        for t in rows(con, """SELECT name, type FROM sqlite_master WHERE type IN ('table','view')
                              AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name"""):
            cols = [c["name"] for c in rows(con, "SELECT name FROM pragma_table_info(?)", (t["name"],))]
            n = con.execute(f"SELECT count(*) FROM {quote_name(t['name'])}").fetchone()[0]
            out.append({**t, "columns": cols, "count": n})
        return out

    if len(p) == 2 and p[0] == "table":
        name = p[1]
        if not NAME.match(name) or not one(con, "SELECT 1 x FROM sqlite_master WHERE name=? AND type IN ('table','view')", (name,)):
            return {"error": "no such table"}, 404
        limit = max(1, min(500, int(arg("limit", "100"))))
        offset = max(0, int(arg("offset", "0")))
        cur = con.execute(f'SELECT * FROM "{name}" LIMIT ? OFFSET ?', (limit, offset))
        return {"columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()],
                "count": con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0], "offset": offset, "limit": limit}

    if p == ["sql"] and body is not None:
        return run_readonly(db, body.get("sql", ""))

    if len(p) == 2 and p[0] == "source":
        return source(root, con, int(p[1]))

    return {"error": "not found"}, 404


SQL_SECONDS = 4  # the SQL box stops a query after this long
SQL_MAX_TEXT = 20_000  # characters of SQL text accepted
SQL_MAX_CELL = 10_000  # characters of one text cell sent back; longer text is cut with a note
SQL_MAX_RESPONSE = 5_000_000  # rows stop being added once the JSON passes about this many bytes (truncated=true)
SQL_MAX_ROWS = 1000


def _sql_cell(v):
    if isinstance(v, bytes):
        return {"blob_bytes": len(v)}
    if isinstance(v, str) and len(v) > SQL_MAX_CELL:
        return v[:SQL_MAX_CELL] + f" … [cut: {len(v):,} characters in all]"
    return v


def run_readonly(db, sql):
    """The viewer's SQL box: one SELECT on a read-only connection whose authorizer allows reads only. Bounded by
    SQL_SECONDS of run time, SQL_MAX_TEXT of SQL, SQL_MAX_CELL per text cell (blobs come back as {"blob_bytes": N}),
    SQL_MAX_ROWS rows and about SQL_MAX_RESPONSE bytes of JSON; truncated says rows were left out."""
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return {"error": "Write a query first."}, 400
    if len(sql) > SQL_MAX_TEXT:
        return {"error": f"The query is too long (over {SQL_MAX_TEXT:,} characters)."}, 400
    if not Path(db).exists():
        return {"error": "ledger not found"}, 503
    con = connect(db, readonly=True)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    con.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    timed_out = with_deadline(con, SQL_SECONDS)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        out, size, truncated = [], 2, False
        for r in cur:
            if len(out) >= SQL_MAX_ROWS:
                truncated = True
                break
            row = [_sql_cell(v) for v in r]
            size += len(json.dumps(row, ensure_ascii=False, default=str).encode()) + 1
            if size > SQL_MAX_RESPONSE:
                truncated = True
                break
            out.append(row)
        return {"columns": cols, "rows": out, "truncated": truncated}
    except (sqlite3.Error, sqlite3.Warning) as e:  # before Python 3.11, several statements raise sqlite3.Warning
        if isinstance(e, sqlite3.OperationalError) and "interrupted" in str(e) and timed_out():
            return {"error": TOO_LONG.format(SQL_SECONDS)}, 400
        return {"error": f"The query did not run: {e}"}, 400
    finally:
        con.close()


# ---------------------------------------------------------------- HTTP

REQUEST_TIMEOUT = 10  # seconds a viewer connection may sit idle or half-sent before it is dropped
MAX_BODY = 200_000  # bytes of POST body accepted


def page_csp(page):
    """The viewer page's Content-Security-Policy: its one inline script is allowed by its sha256 (the page is static
    now that the token travels in the URL fragment), not by 'unsafe-inline'."""
    import base64
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(x.encode("utf-8")).digest()).decode()
                      for x in scripts) or "'none'"
    return (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_server(root, db, port=0):
    """Build the viewer's handler and bind it to 127.0.0.1:port (0 = any free port); returns (server, token).
    The caller runs server.serve_forever(). The Host allow-list uses the port actually bound. The page itself holds
    no secret: the token reaches the browser only in the launch URL's #k= fragment (see serve), and every /api/
    request must send it back as X-Ledger-Token."""
    token = secrets.token_urlsafe(32)
    token_bytes = token.encode()
    page = PAGE.encode("utf-8")
    csp = page_csp(PAGE)
    hosts = set()

    class H(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", csp)
            if code >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "wrong host"}); return False
            return True

        def token_ok(self):
            try:  # headers are decoded as latin-1, so this gives back the bytes that were sent
                sent = self.headers.get("X-Ledger-Token", "").encode("latin-1")
                good = secrets.compare_digest(sent, token_bytes)
            except (UnicodeError, TypeError):
                good = False
            if not good:
                self.send(403, {"error": "missing or wrong token; open the viewer through the link that "
                                         "ledger.py serve printed (it ends in #k=...)"})
            return good

        def handle_api(self, u, body):
            try:
                res = api(root, db, u.path, parse_qs(u.query), body)
            except Exception as e:
                res = ({"error": f"Something went wrong reading the ledger: {e}"}, 500)
            code, res = (res[1], res[0]) if isinstance(res, tuple) else (200, res)
            self.send(code, res)

        def do_GET(self):
            if not self.host_ok():
                return
            u = urlparse(self.path)
            if u.path == "/":
                self.send(200, page, "text/html; charset=utf-8")
            elif u.path.startswith("/api/"):
                if self.token_ok():
                    self.handle_api(u, None)
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.host_ok():
                return
            u = urlparse(self.path)  # the guard below and the dispatch use this one parsed path
            if not u.path.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            if not self.token_ok():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "JSON only"})
            raw_n = self.headers.get("Content-Length")
            if raw_n is None:
                n = 0
            elif not re.fullmatch(r"[0-9]{1,12}", raw_n):
                return self.send(400, {"error": "bad Content-Length"})
            else:
                n = int(raw_n)
            if n > MAX_BODY:
                return self.send(413, {"error": "too large"})
            try:
                data = self.rfile.read(n) if n else b""
            except OSError:  # the client stopped sending (REQUEST_TIMEOUT): drop the connection
                self.close_connection = True
                return
            if len(data) < n:
                return self.send(400, {"error": "the body is shorter than its Content-Length"})
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad JSON"})
            if not isinstance(body, dict):
                return self.send(400, {"error": "Send a JSON object"})
            self.handle_api(u, body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    bound = srv.server_address[1]
    hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    return srv, token


def serve(root, db, port, open_browser):
    """Run the viewer until Ctrl+C. The launch URL carries the token in its #k= fragment (never sent to the server,
    read by the page into sessionStorage and removed from the address bar). The umask is set to 077 first, so any
    file the viewer's connections create beside the ledger (-journal, -wal, -shm) is owner-only."""
    import os
    if hasattr(os, "umask"):
        os.umask(0o077)
    srv, token = make_server(root, db, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/#k={token}"
    print(f"Ledger viewer for {db} at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None)
    ap.add_argument("--db")
    ap.add_argument("--version", action="version", version=f"ledger.py {TOOL_VERSION} (schema {SCHEMA_VERSION})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--force", action="store_true",
                   help="replace the project's .code-review/ledger.py even when it is newer than this copy")
    sub.add_parser("migrate", help="upgrade an older ledger to this schema (keeps a .bak-v<N> copy)")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765); s.add_argument("--no-open", action="store_true")
    s = sub.add_parser("check")
    s.add_argument("--full", action="store_true",
                   help="run the full PRAGMA integrity_check instead of quick_check")
    s = sub.add_parser("fill-refs"); s.add_argument("review", nargs="?")
    s = sub.add_parser("tag-review"); s.add_argument("review")
    s = sub.add_parser("set-report", help="store the review document (Markdown) shown in the viewer")
    s.add_argument("review"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("set-narrative", help="store the plan's narrative (Markdown) shown in the viewer")
    s.add_argument("plan"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("show"); s.add_argument("location", type=int)
    sub.add_parser("gates")
    sub.add_parser("dump")
    sub.add_parser("schema")
    a = ap.parse_args()
    if a.cmd == "schema":
        print(SCHEMA); return
    explicit = a.root is not None  # any --root given, '.' included, is used as given
    root, how = resolve_root(Path(a.root if explicit else ".").resolve(), explicit, script=__file__)
    # The ledger: --db FILE, else $CODE_REVIEW_DIR/ledger.sqlite (a ledger kept outside the repository), else
    # <root>/.code-review/ledger.sqlite.
    env_dir = os.environ.get("CODE_REVIEW_DIR", "").strip()
    if a.db:
        db, where = Path(a.db).resolve(), "db"
    elif env_dir:
        db, where = Path(env_dir).expanduser().resolve() / "ledger.sqlite", "env"
    else:
        db, where = root / ".code-review" / "ledger.sqlite", "default"
    if a.cmd == "init":
        print(f"project root: {root}")
        if how == "start" and where == "default":
            sys.exit(f"init: {root} is not in a git repository and no folder above it has a ledger; pass --root DIR "
                     f"to say which folder is the project root (init then creates DIR/.code-review/ledger.sqlite)")
        in_ledger_folder = db.parent.name == ".code-review"
        inside_root = db.parent == root or root in db.parent.parents
        init(root, db, force=a.force, copy_tool=in_ledger_folder or where == "env",
             gitignore=in_ledger_folder and inside_root)
        return
    if a.cmd == "migrate":
        migrate(db); return
    # Writes refuse an older ledger; serve, gates and the writes also refuse a schema that differs from the shipped
    # one (a forged trigger could answer gates). show and dump still read it, for diagnosis; check reports it.
    con = open_ledger(db, write=a.cmd in ("serve", "fill-refs", "tag-review", "set-report", "set-narrative"),
                      verify=a.cmd in ("serve", "gates", "fill-refs", "tag-review", "set-report", "set-narrative"))
    if a.cmd == "serve":
        con.close(); serve(root, db, a.port, not a.no_open)
    elif a.cmd == "check":
        was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
        if was and was[0] != str(db.parent):
            print(f"warning: this ledger was set up at {was[0]} and is now used from {db.parent} (fine after a move; "
                  f"init records the new place)", file=sys.stderr)
        bad = check(con, full=a.full)
        bad += check_git_refs(root, con)
        sys.exit(1 if bad else 0)
    elif a.cmd == "fill-refs":
        sys.exit(1 if fill_refs(root, con, a.review) else 0)
    elif a.cmd == "tag-review":
        sys.exit(tag_review(root, con, a.review))
    elif a.cmd in ("set-report", "set-narrative"):
        sys.exit(set_text(con, a.cmd, a.review if a.cmd == "set-report" else a.plan, a.file))
    elif a.cmd == "show":
        s = source(root, con, a.location, context=0)
        if s.get("error"):
            sys.exit(s["error"])
        print(f"# {s['repo_name']} {s['path']} @ {s['commit_sha']} (from {s['source']})")
        if s.get("git_error"):
            print(f"# git: {s['git_error']}")
        for l in s.get("lines", []):
            print(f"{l['n']:6d}  {l['text']}")
        if s.get("source") == "none":
            sys.exit(f"show: nothing to show for reference {a.location} (git could not read it and it has no "
                     f"excerpt)")
    elif a.cmd == "gates":
        for g in rows(con, "SELECT * FROM gates WHERE status IN ('open','answered') ORDER BY created_at"):
            ans = f" -> {g['answer_value'] or ''} {g['answer_text'] or ''}".rstrip() if g["status"] == "answered" else ""
            print(f"{g['code']}  [{g['status']}] {g['kind']}: {g['title']}{ans}")
        try:
            stale = rows(con, "SELECT * FROM v_stale_steps ORDER BY plan, step")
        except sqlite3.Error:
            stale = []  # an older ledger (gates warned already): migrate adds the view
        for s in stale:
            print(f"stale step {s['plan']} #{s['step']} [{s['status']}] {s['title']}: its issues are all closed "
                  f"({s['issues']}); mark it done or dropped")
    elif a.cmd == "dump":
        dump(con, db)


if __name__ == "__main__":
    main()
