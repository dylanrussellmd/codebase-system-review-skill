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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

TOOL_VERSION = "1.2.1"  # stamped by dev/build.py from `version:` in SKILL.md's frontmatter
TOOL_HASH = "4ab5de9b9f468ac394088e26013eb5431e6da3c482e9fbcefa9ac93634855b30"  # stamped by dev/build.py: sha256 of the built file with this value as the placeholder
SCHEMA = r"""-- =====================================================================
-- Code review ledger — schema version 2
-- One SQLite file per project: <project root>/.code-review/ledger.sqlite
-- Every connection must run:  PRAGMA foreign_keys = ON;
-- Times are UTC ISO-8601 text (strftime('%Y-%m-%dT%H:%M:%fZ','now')).
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
  code          TEXT NOT NULL UNIQUE,    -- G-01
  text          TEXT NOT NULL,
  source        TEXT,                    -- where it came from (doc path, "owner, 2026-11-10")
  adopted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  retired_at    TEXT,
  superseded_by INTEGER REFERENCES design_goals(id)
);

-- ---------- Reviews -----------------------------------------------------

CREATE TABLE reviews (
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

-- The exact code state each review looked at.
CREATE TABLE review_repositories (
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
  items_reviewed INTEGER,
  high_count     INTEGER,
  medium_count   INTEGER,
  low_count      INTEGER,
  test_count     INTEGER,
  final_message  TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  UNIQUE (review_id, label)
);

-- ---------- Requirements (for spec conformance over time) -------------

CREATE TABLE requirements (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE,   -- REQ-0001
  text            TEXT NOT NULL,
  source          TEXT NOT NULL,          -- doc path, issue URL, "owner, chat 2026-11-10"
  source_location TEXT,
  stated_at       TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','withdrawn')),
  superseded_by   INTEGER REFERENCES requirements(id),
  first_review_id INTEGER REFERENCES reviews(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Issues: the longitudinal record ----------------------------

CREATE TABLE issues (
  id                     INTEGER PRIMARY KEY,
  code                   TEXT NOT NULL UNIQUE,   -- ISS-0001, never reused
  title                  TEXT NOT NULL,
  domain_code            TEXT REFERENCES domains(code),
  theme_code             TEXT REFERENCES themes(code),
  sub                    TEXT,                   -- slug naming the specific thing
  kind                   TEXT CHECK (kind IN
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
  times_seen             INTEGER NOT NULL DEFAULT 1,
  opened_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at            TEXT,
  verified_at            TEXT,
  updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status/severity change. Set ALL FIVE in the same
  -- UPDATE that changes status or severity (NULL the ones that don't apply,
  -- or stale context from an earlier change is logged); the trigger copies
  -- them into events, so history records who, why, in which review/step/commit.
  change_actor_id        INTEGER REFERENCES actors(id),
  change_review_id       INTEGER REFERENCES reviews(id),
  change_step_id         INTEGER REFERENCES steps(id),
  change_commit_sha      TEXT,
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
  issue_id             INTEGER REFERENCES issues(id),   -- set during matching
  match_type           TEXT CHECK (match_type IN
                         ('new','still-present','changed','recurrence','regression','continuation')),
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
  code                TEXT NOT NULL UNIQUE,   -- D-0001
  question            TEXT NOT NULL,
  options             TEXT CHECK (options IS NULL OR json_valid(options)),
  answer              TEXT,
  rationale           TEXT,
  status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','revisit')),
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
  code            TEXT NOT NULL UNIQUE,     -- GATE-0001
  kind            TEXT NOT NULL CHECK (kind IN
                    ('domain-approval','plan-approval','decision','step-input',
                     'confirm-false-finding','accept-risk','production-action',
                     'review-handoff','other')),
  title           TEXT NOT NULL,
  question        TEXT NOT NULL,
  context         TEXT,                     -- plain text the owner needs to answer
  options         TEXT CHECK (options IS NULL OR json_valid(options)),
                                            -- JSON array of {"value","label","description"}
  allow_text      INTEGER NOT NULL DEFAULT 1 CHECK (allow_text IN (0,1)),
  blocking        INTEGER NOT NULL DEFAULT 1 CHECK (blocking IN (0,1)),  -- work stops until answered
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','applied','withdrawn')),
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
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Fix plans -----------------------------------------------------

CREATE TABLE plans (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE,   -- P-0001
  title                TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'drafting' CHECK (status IN
                         ('drafting','awaiting-approval','approved','in-progress','complete',
                          'superseded','abandoned')),
  approved_by_actor_id INTEGER REFERENCES actors(id),
  approved_at          TEXT,
  superseded_by        INTEGER REFERENCES plans(id),
  doc_url              TEXT,
  page_url             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
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
  change_actor_id   INTEGER REFERENCES actors(id),   -- context for the status trigger
  change_commit_sha TEXT,
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
  added_commit_sha        TEXT,
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
  commit_sha    TEXT,
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

CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('review','issue','step','plan','decision','finding','gate')),
  entity_id   INTEGER NOT NULL,
  event       TEXT NOT NULL,              -- status-changed, severity-changed, opened, seen, linked, note ...
  from_value  TEXT,
  to_value    TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  review_id   INTEGER REFERENCES reviews(id),
  step_id     INTEGER REFERENCES steps(id),
  commit_sha  TEXT,
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

-- ---------- Triggers ----------------------------------------------------

CREATE TRIGGER tr_issue_opened AFTER INSERT ON issues BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('issue', NEW.id, 'opened', NEW.status, NEW.change_actor_id,
          NEW.first_seen_review_id, NEW.change_note);
END;

CREATE TRIGGER tr_issue_status AFTER UPDATE OF status ON issues
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.change_actor_id, NEW.change_review_id, NEW.change_step_id,
          NEW.change_commit_sha, NEW.change_note);
  UPDATE issues SET
    resolved_at = CASE WHEN NEW.status IN ('fixed','accepted-risk','wont-fix','duplicate','obsolete')
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       WHEN NEW.status IN ('open','regressed','needs-decision') THEN NULL
                       ELSE resolved_at END,
    verified_at = CASE WHEN NEW.status = 'verified' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       WHEN NEW.status IN ('open','regressed') THEN NULL
                       ELSE verified_at END
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

CREATE TRIGGER tr_issue_touch AFTER UPDATE ON issues
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE issues SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- A fixed or verified issue that a new finding matches is a regression.
CREATE TRIGGER tr_finding_matched AFTER UPDATE OF issue_id ON findings
WHEN NEW.issue_id IS NOT NULL AND OLD.issue_id IS NOT NEW.issue_id BEGIN
  UPDATE issues SET
    last_seen_review_id = NEW.review_id,
    last_checked_review_id = NEW.review_id,
    times_seen = (SELECT count(DISTINCT f.review_id) FROM findings f WHERE f.issue_id = NEW.issue_id),
    current_state = COALESCE(NEW.before_state, current_state),
    change_review_id = NEW.review_id,
    change_actor_id = NULL,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = 'matched by finding ' || NEW.point_id,
    status = CASE WHEN status IN ('fixed','verified') THEN 'regressed' ELSE status END
  WHERE id = NEW.issue_id;
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  VALUES ('issue', NEW.issue_id, 'seen', NEW.match_type, NEW.review_id, NEW.point_id);
END;

CREATE TRIGGER tr_step_status AFTER UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, step_id, commit_sha, note)
  VALUES ('step', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.change_actor_id, NEW.id, NEW.change_commit_sha, NEW.progress_note);
  UPDATE steps SET
    started_at = CASE WHEN NEW.status = 'doing' AND started_at IS NULL
                      THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE started_at END,
    done_at    = CASE WHEN NEW.status = 'done' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END
  WHERE id = NEW.id;
END;

CREATE TRIGGER tr_step_touch AFTER UPDATE ON steps
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE steps SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER tr_review_status AFTER UPDATE OF status ON reviews
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id)
  VALUES ('review', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.id);
  UPDATE reviews SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    completed_at = CASE WHEN NEW.status IN ('handed-off','abandoned')
                        THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE completed_at END
  WHERE id = NEW.id;
END;

CREATE TRIGGER tr_plan_status AFTER UPDATE OF status ON plans
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value)
  VALUES ('plan', NEW.id, 'status-changed', OLD.status, NEW.status);
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER tr_decision_status AFTER UPDATE OF status ON decisions
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.decided_by_actor_id, NEW.review_id, NEW.step_id, NEW.answer);
END;

-- A review's verdict on a known issue moves the issue along:
-- reported fixed after a fix -> verified; reported fixed with no fix recorded -> fixed;
-- reported back -> regressed. Every check updates last_checked.
CREATE TRIGGER tr_issue_checked AFTER INSERT ON review_issue_checks BEGIN
  UPDATE issues SET
    last_checked_review_id = NEW.review_id,
    change_review_id = NEW.review_id,
    change_actor_id = NEW.actor_id,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = 'review check: ' || NEW.outcome || COALESCE(' — ' || NEW.evidence, ''),
    status = CASE
      WHEN NEW.outcome = 'fixed' AND status = 'fixed' THEN 'verified'
      WHEN NEW.outcome = 'fixed' AND status IN ('open','needs-decision','planned','in-progress','regressed') THEN 'fixed'
      WHEN NEW.outcome = 'regressed' AND status IN ('fixed','verified') THEN 'regressed'
      ELSE status END,
    resolution = CASE WHEN NEW.outcome = 'fixed' AND status IN ('open','needs-decision','planned','in-progress','regressed')
                      THEN COALESCE(resolution, 'Found fixed by a later review, with no plan step recorded.')
                      ELSE resolution END
  WHERE id = NEW.issue_id;
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

CREATE TRIGGER tr_gate_status AFTER UPDATE OF status ON gates
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.review_id, NEW.step_id,
          COALESCE(NEW.answer_value || ': ', '') || COALESCE(NEW.answer_text, NEW.applied_note, ''));
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

CREATE TRIGGER tr_gate_answer_cleared AFTER UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status <> 'open' AND NEW.status = 'open'
 AND (OLD.answer_value IS NOT NULL OR OLD.answer_text IS NOT NULL) BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'answer-cleared', OLD.answer_value, NULL, NEW.review_id, NEW.step_id,
          'reopened; the answer was: ' || COALESCE(OLD.answer_value || ': ', '') || COALESCE(OLD.answer_text, ''));
END;

-- Plan rules enforced by the database.
CREATE TRIGGER tr_dep_order BEFORE INSERT ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
END;

CREATE TRIGGER tr_step_phase_plan BEFORE INSERT ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;

CREATE TRIGGER tr_relation_noreverse_dup BEFORE INSERT ON issue_relations
WHEN NEW.relation IN ('duplicate-of','continuation-of','regression-of','supersedes','merged-into','split-from','caused-by')
BEGIN
  SELECT RAISE(ABORT, 'the reverse relation already exists')
  WHERE EXISTS (SELECT 1 FROM issue_relations
                WHERE from_issue_id = NEW.to_issue_id AND to_issue_id = NEW.from_issue_id
                  AND relation = NEW.relation);
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

-- Known issues to hand each reviewer in the brief, by domain.
CREATE VIEW v_known_issues_for_brief AS
SELECT i.domain_code, i.code, i.title, i.severity, i.status, i.current_state,
       i.recommendation, i.resolution,
       (SELECT group_concat(test_path || '::' || test_name, '; ')
          FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1) AS guard_tests
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

-- Per review and domain: counts by severity, verified and false.
CREATE VIEW v_review_summary AS
SELECT r.code AS review, r.started_at, f.domain_code,
       count(*) AS points,
       sum(f.severity = 'H') AS high, sum(f.severity = 'M') AS medium,
       sum(f.severity = 'L') AS low,  sum(f.severity = 'T') AS tests,
       sum(f.verification = 'confirmed') AS confirmed,
       sum(f.verification = 'false') AS false_findings,
       sum(f.match_type = 'new') AS new_issues,
       sum(f.match_type IN ('still-present','changed','continuation')) AS carried_over,
       sum(f.match_type IN ('recurrence','regression')) AS came_back
FROM findings f JOIN reviews r ON r.id = f.review_id
GROUP BY r.id, f.domain_code
ORDER BY r.started_at, f.domain_code;

-- Findings not yet matched to an issue (must be empty before consolidating).
CREATE VIEW v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL AND f.verification <> 'false';

-- Known issues in domains that ran, with no check recorded (must be empty).
CREATE VIEW v_unchecked_known_issues AS
SELECT r.code AS review, i.code AS issue, i.domain_code, i.title
FROM reviews r
JOIN review_domains rd ON rd.review_id = r.id AND rd.decision = 'included'
JOIN issues i ON i.domain_code = rd.domain_code
WHERE i.first_seen_review_id <> r.id
  AND i.opened_at < r.started_at
  AND i.status NOT IN ('duplicate','obsolete')
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = r.id AND c.issue_id = i.id);

-- Coverage: findings of a plan's reviews closed by no step (must be empty).
CREATE VIEW v_uncovered_findings AS
SELECT p.code AS plan, r.code AS review, f.point_id, f.severity, f.summary
FROM plans p
JOIN plan_reviews pr ON pr.plan_id = p.id
JOIN reviews r ON r.id = pr.review_id
JOIN findings f ON f.review_id = r.id
WHERE f.verification <> 'false'
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                  WHERE sf.finding_id = f.id AND s.plan_id = p.id);

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
CREATE VIEW v_owner_queue AS
SELECT 'gate' AS kind, g.code AS ref, g.kind AS need, g.title, g.question,
       g.status, g.blocking, g.created_at
FROM gates g WHERE g.status IN ('open','answered')
UNION ALL
SELECT 'step', p.code || ' #' || s.number, s.owner_need, s.title,
       COALESCE(s.owner_question, s.progress_note), 'open', 1, s.updated_at
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE s.status = 'waiting-owner'
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = s.id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'decision', d.code, 'decision', d.question, d.revisit_when, d.status, 0, d.asked_at
FROM decisions d WHERE d.status IN ('open','revisit')
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.decision_id = d.id AND g.status IN ('open','answered'));

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

-- References that could not be traced back (must be empty after indexing).
CREATE VIEW v_untraceable_refs AS
SELECT r.code AS review, f.point_id, fl.path,
       CASE WHEN fl.commit_sha IS NULL THEN 'no commit'
            WHEN fl.line_start IS NULL AND fl.symbol IS NULL THEN 'no lines or symbol'
            WHEN fl.excerpt IS NULL THEN 'no excerpt'
            WHEN rr.tag_name IS NULL THEN 'commit not tagged' END AS problem
FROM finding_locations fl
JOIN findings f ON f.id = fl.finding_id
JOIN reviews r ON r.id = f.review_id
LEFT JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = fl.repository_id
WHERE fl.commit_sha IS NULL OR (fl.line_start IS NULL AND fl.symbol IS NULL)
   OR fl.excerpt IS NULL OR rr.tag_name IS NULL;

-- Fixed issues with no guard test, or whose guard test has disappeared.
CREATE VIEW v_unguarded_fixes AS
SELECT i.code, i.title, i.status,
       CASE WHEN NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id)
            THEN 'no guard test' ELSE 'guard test removed' END AS problem
FROM issues i
WHERE i.status IN ('fixed','verified') AND i.kind NOT IN ('docs','design')
  AND NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1);

-- Trend of each metric across reviews.
CREATE VIEW v_metric_trends AS
SELECT m.name, m.scope, COALESCE(rp.name, 'all') AS repository, r.code AS review,
       r.started_at, m.value, m.unit, m.method
FROM metrics m JOIN reviews r ON r.id = m.review_id
LEFT JOIN repositories rp ON rp.id = m.repository_id
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

INSERT INTO meta(key, value) VALUES ('schema_version','2');
INSERT INTO schema_migrations(version, description) VALUES (1, 'initial ledger schema'),
  (2, 'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted');
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
const L = {
  sev:{H:"High",M:"Medium",L:"Low",T:"Missing test"},
  issue:{open:"Open","needs-decision":"Needs your decision",planned:"Planned","in-progress":"In progress",fixed:"Fixed",verified:"Fixed and confirmed",regressed:"Came back","accepted-risk":"Accepted risk","wont-fix":"Won't fix",duplicate:"Duplicate",obsolete:"No longer applies"},
  match:{new:"New","still-present":"Still present",changed:"Changed",recurrence:"Came back",regression:"Came back after a fix",continuation:"Continues an earlier issue"},
  verif:{unverified:"Not checked yet",confirmed:"Confirmed",partly:"Partly confirmed",false:"False finding","not-checkable":"Could not be checked"},
  review:{planning:"Planning","awaiting-approval":"Waiting for your approval",approved:"Approved",running:"Reviewers running",verifying:"Verifying",indexing:"Indexing points",consolidating:"Writing it up","handed-off":"Handed off",abandoned:"Abandoned"},
  scope:{full:"Full review",partial:"Partial review",targeted:"Targeted review",second:"Second review","fix-verification":"Fix check"},
  step:{"not-started":"Not started",doing:"In progress","waiting-owner":"Waiting on you",done:"Done",dropped:"Dropped"},
  need:{none:"",decision:"Your decision",confirm:"Your confirmation",review:"Your review",production:"Production action"},
  gate:{"domain-approval":"Approve review domains","plan-approval":"Approve fix plan",decision:"Decision","step-input":"Input on a step","confirm-false-finding":"Confirm a false finding","accept-risk":"Accept a risk","production-action":"Production action","review-handoff":"Review hand-off",other:"Question"},
  gstat:{open:"Waiting on you",answered:"Answered, waiting for the agent",applied:"Done",withdrawn:"Withdrawn"},
  rel:{"duplicate-of":"Duplicate of","continuation-of":"Continues","regression-of":"Regression of","related-to":"Related to","caused-by":"Caused by",blocks:"Blocks",supersedes:"Supersedes","split-from":"Split from","merged-into":"Merged into"},
  relIn:{"duplicate-of":"Has duplicate","continuation-of":"Continued by","regression-of":"Came back as","related-to":"Related to","caused-by":"Causes",blocks:"Blocked by",supersedes:"Superseded by","split-from":"Split into","merged-into":"Absorbed"},
  check:{"still-present":"Still present",changed:"Changed",fixed:"Fixed",regressed:"Came back",obsolete:"No longer applies","not-checked":"Not checked"},
  kind:{defect:"Defect",test:"Missing test",validation:"Validation",refactor:"Refactor",feature:"Feature",design:"Design",docs:"Documentation",logging:"Logging",perf:"Performance",security:"Security",ops:"Operations",verify:"To verify"},
  run:{queued:"Queued",running:"Running",finished:"Finished",failed:"Failed",cancelled:"Cancelled"},
  plan:{drafting:"Drafting","awaiting-approval":"Waiting for your approval",approved:"Approved","in-progress":"In progress",complete:"Complete",superseded:"Replaced",abandoned:"Abandoned"},
  decision:{open:"Open",decided:"Decided",revisit:"To revisit"},
};
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
      h("div", null, h("b", null, fs.filter(f => f.match_type === "new").length), h("span", null, "new issues")),
      h("div", null, h("b", null, fs.filter(f => ["recurrence","regression"].includes(f.match_type)).length), h("span", null, "came back"))),
    d.gates.length ? h("div", {class:"card"}, h("h2", null, "Questions asked during this review"), table([{label:"About", get:g => lab("gate", g.kind)}, {label:"Title", key:"title"}, {label:"Status", get:g => lab("gstat", g.status)}, {label:"Answer", get:g => [g.answer_value || "", " ", g.answer_text || ""]}], d.gates, () => go("#/inbox"))) : null,
    h("div", {class:"card"}, kv([["Verdict", r.verdict], ["Summary", r.summary], ["What was asked", r.request_text], ["Run by", r.manager ? `${r.manager}${r.manager_model ? " (" + r.manager_model + ")" : ""}` : null],
      ["Domains approved by", r.approved_by ? `${r.approved_by}, ${when(r.approved_at)}` : null], ["Follows", r.previous_code ? h("a", {href:"#/review/" + r.previous_code}, r.previous_code) : null],
      ["Write-up", r.summary_doc_url ? extLink(r.summary_doc_url, "Open the review document") : null]])),
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
    d.checks.length ? [h("h3", null, "Earlier issues this review looked for"),
      table([{label:"Issue", key:"issue_code"}, {label:"Title", key:"title"}, {label:"Found", get:x => lab("check", x.outcome)}, {label:"Evidence", key:"evidence"}, {label:"Now", get:x => issueTag(x.status)}],
            d.checks, x => go("#/issue/" + x.issue_code))] : null,
    d.metrics.length ? [h("h3", null, "Measurements"), table([{label:"Measure", get:m => words(m.name)}, {label:"Of", key:"scope"}, {label:"Value", get:m => `${m.value}${m.unit ? " " + m.unit : ""}`, num:1}, {label:"How", get:m => words(m.method)}, {label:"Note", key:"note"}], d.metrics)] : null,
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
      : e.event === "seen" ? (e.to_value === "new" ? "Raised as a new issue" : `Raised again (${lab("match", e.to_value)})`) : e.event === "opened" ? "Opened" : words(e.event),
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
      return h("div", {class:"card"}, h("b", null, words(n)), h("div", {class:"note"}, [sc !== "all" ? sc : "", repo !== "all" ? repo : ""].filter(Boolean).join(" · ") || "Whole system"), lineChart(pts, pts[0].unit)); }))
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
    if (!OVERVIEW || name === "home" || name === "inbox") { OVERVIEW = await api("overview"); LABELS = OVERVIEW.labels;
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


def shipped_objects():
    """[(type, name, tbl_name, sql)] of the shipped schema in creation order: each CREATE statement of SCHEMA, read
    from the text (tbl_name is None). dev/tests/test_schema_drift.py checks it equals what SQLite stores when SCHEMA
    runs."""
    if SCHEMA not in _shipped:
        out = []
        for stmt in split_sql(SCHEMA):
            m = CREATE_OBJECT.match(normalize_sql(stmt))
            if m:
                sql = stmt[stmt.upper().index("CREATE"):].rstrip().rstrip(";").rstrip()
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


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Twin of the must-be-empty list in SKILL.md (Rules for the ledger / check) and of the 'must be empty' comments in
# references/schema.sql; change all three together. dev/tests/test_check.py enforces that they agree.
MUST_BE_EMPTY = [
    ("v_unmatched_findings", "Findings not matched to an issue"),
    ("v_unchecked_known_issues", "Known issues in a domain that ran, with no check recorded"),
    ("v_untraceable_refs", "Code references that cannot be traced to exact lines"),
    ("v_uncovered_findings", "Findings no step of their plan closes"),
    ("v_coverage_problems", "Steps closing nothing, or findings closed twice without a reason"),
]


def check(con, quiet=False):
    bad = 0
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        print("integrity_check:", ic); bad += 1
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        print(f"foreign_key_check: {len(fk)} broken references"); bad += 1
    bad += check_schema(con)
    for view, label in MUST_BE_EMPTY:
        try:
            n = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        except sqlite3.Error as e:
            bad += 1
            print(f"  ERR  {label}  ({view}): {e}")
            continue
        if n:
            bad += 1
            print(f"{n:5d}  {label}  ({view})")
            for r in rows(con, f"SELECT * FROM {view} LIMIT 5"):
                print("       ", json.dumps(r, ensure_ascii=False)[:200])
        elif not quiet:
            print(f"    0  {label}")
    return bad


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


def file_at(root, repo_path, sha, path, allow_outside=False):
    """The file's text at commit sha. A refused path gives the fixed error 'bad path' before git runs, so no message
    says whether some file outside the repository exists."""
    if not HEX.match(sha or ""):
        raise RuntimeError("commit is not a hex hash")
    if bad_location_path(path or ""):
        raise RuntimeError("bad path")
    return git(repo_dir(root, repo_path, allow_outside), "show", f"{sha}:{path}").decode("utf-8", "replace")


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


def fill_refs(root, con, review_code=None):
    sql = """SELECT fl.id FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
             JOIN reviews r ON r.id = f.review_id
             WHERE fl.commit_sha IS NOT NULL AND (fl.excerpt IS NULL OR fl.blob_sha IS NULL
                   OR fl.permalink IS NULL OR (fl.line_start IS NULL AND fl.symbol IS NOT NULL))"""
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
            text = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"], outside)
        except Exception as e:
            print(f"{where}: {e}"); failed += 1; continue
        lines = text.splitlines()
        a, b = loc["line_start"], loc["line_end"]
        if a is None and loc["symbol"]:
            for i, t in enumerate(lines, 1):
                if loc["symbol"] in t:
                    a = b = i; break
            else:
                print(f"{where}: symbol {loc['symbol']!r} not found"); failed += 1; continue
        if a and (b or a) > len(lines):
            print(f"{where}: lines {a}-{b or a} are past the end of the file ({len(lines)} lines)")
            failed += 1; continue
        upd = {}
        if a:
            b = b or a
            excerpt = "\n".join(lines[a - 1:b])
            upd.update(line_start=a, line_end=b)
            if loc["excerpt"] is None:
                shown, masked = mask_secrets(excerpt)
                upd.update(excerpt=shown, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
                           excerpt_redacted=1 if masked else 0)
        if loc["blob_sha"] is None:
            try:
                upd["blob_sha"] = git(repo_dir(root, loc["repo_path"], outside), "rev-parse",
                                      f"{loc['commit_sha']}:{loc['path']}").decode().strip()
            except Exception:
                pass
        if loc["permalink"] is None and loc["permalink_template"]:
            link = (loc["permalink_template"].replace("{sha}", loc["commit_sha"])
                    .replace("{path}", loc["path"]).replace("{start}", str(a or 1))
                    .replace("{end}", str(b or a or 1)))
            if SAFE_URL.match(link):
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
        # recorded right away, in its own short transaction, so a later repository's failure cannot lose it
        con.execute("UPDATE review_repositories SET tag_name=? WHERE review_id=? AND repository_id=?",
                    (tag, rv["id"], r["repository_id"]))
        con.commit()
        hint = f"git -C {shlex.quote(r['path'] or '.')} push origin {shlex.quote(tag)}"
        print(f"{r['name']}: {tag} -> {sha[:10]}  (push with: {hint})")
    if left:
        print(f"tag-review {code}: {left} of {len(repos)} repositories not tagged", file=sys.stderr)
    return 1 if left else 0


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
            cur = w.execute("""UPDATE gates SET status='answered', answer_value=?, answer_text=?,
                               answered_by='owner', answered_via='viewer' WHERE id=? AND status='open'""",
                            (val, txt, g["id"]))
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
                WHERE status IN ('open','needs-decision','planned','in-progress','regressed') GROUP BY severity"""),
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
            "checks": [{"label": label, "view": v, "n": con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]}
                       for v, label in MUST_BE_EMPTY],
            "labels": {t: {r["code"]: r["name"] for r in rows(con, f"SELECT code, name FROM {t}")} for t in LABEL_TABLES},
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
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type='new') new_issues,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN ('recurrence','regression')) came_back
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
            WHERE f.review_id=? ORDER BY CASE f.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, f.point_id""", (rid,))
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
            "metrics": rows(con, "SELECT name, scope, value, unit, method, note FROM metrics WHERE review_id=? ORDER BY name, scope", (rid,)),
            "artifacts": rows(con, "SELECT kind, path, url FROM artifacts WHERE review_id=? ORDER BY kind", (rid,)),
            "gates": rows(con, "SELECT code, kind, title, status, answer_value, answer_text FROM gates WHERE review_id=? ORDER BY created_at", (rid,)),
            "findings": findings,
        }

    if p == ["issues"]:
        where, args = ["1"], []
        for k in ("status", "severity", "domain_code"):
            v = arg(k)
            if v == "active" and k == "status":
                where.append("i.status IN ('open','needs-decision','planned','in-progress','regressed')")
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
            ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, i.code LIMIT 2000""", args)

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
        for t in rows(con, "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name"):
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
    sub.add_parser("check")
    s = sub.add_parser("fill-refs"); s.add_argument("review", nargs="?")
    s = sub.add_parser("tag-review"); s.add_argument("review")
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
    con = open_ledger(db, write=a.cmd in ("serve", "fill-refs", "tag-review"),
                      verify=a.cmd in ("serve", "gates", "fill-refs", "tag-review"))
    if a.cmd == "serve":
        con.close(); serve(root, db, a.port, not a.no_open)
    elif a.cmd == "check":
        was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
        if was and was[0] != str(db.parent):
            print(f"warning: this ledger was set up at {was[0]} and is now used from {db.parent} (fine after a move; "
                  f"init records the new place)", file=sys.stderr)
        sys.exit(1 if check(con) else 0)
    elif a.cmd == "fill-refs":
        sys.exit(1 if fill_refs(root, con, a.review) else 0)
    elif a.cmd == "tag-review":
        sys.exit(tag_review(root, con, a.review))
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
    elif a.cmd == "dump":
        dump(con, db)


if __name__ == "__main__":
    main()
