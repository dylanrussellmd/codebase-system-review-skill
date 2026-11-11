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

TOOL_VERSION = "1.7.0"  # stamped by dev/build.py from `version:` in SKILL.md's frontmatter
TOOL_HASH = "861ff85fb6f52b8049867c604361f7ceac6311c4438ac5c65d29d07e92a79878"  # stamped by dev/build.py: sha256 of the built file with this value as the placeholder
SCHEMA = r"""-- =====================================================================
-- Code review ledger — schema version 10
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
  harness         TEXT,                  -- e.g. Claude Code, CI
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
-- reason says why; owner_change records what the owner changed in the proposal.
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

-- The earlier steps each step has to wait for.
-- A step depends only on earlier steps of its own plan (tr_dep_order).
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
-- review_domains, review_actors, actors, repositories, domains, themes, requirements, metric_defs,
-- meta, schema_migrations, ledger_checks, issue_statuses, severities, match_types, relation_types, gate_kinds,
-- goal_statuses, enum_labels.
-- Why not: finding_locations, finding_goals and finding_requirements belong to their finding (logged when it is
-- created) and cannot change but for fill-refs; review_requirement_checks, test_runs and artifacts are themselves
-- timestamped records written once; step_commits, guard_tests, decision_issues, plan_reviews and phases are plan
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

-- The owner's answer to a goal-approval gate: 'approve' approves every goal still proposed on it, by whoever answered
-- (the viewer and import-answers: the owner). Any other answer leaves them proposed for the manager to reword.
CREATE TRIGGER tr_gate_approves_goals AFTER UPDATE OF status ON gates
WHEN OLD.status = 'open' AND NEW.status = 'answered' AND NEW.answer_value = 'approve' BEGIN
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

-- Plan rules enforced by the database, on INSERT and on UPDATE.
CREATE TRIGGER tr_dep_order BEFORE INSERT ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
END;

-- The same when a dependency is edited.
CREATE TRIGGER tr_dep_order_upd BEFORE UPDATE ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
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
-- Must be empty. A dropped step, or one whose no_findings_reason says why it closes nothing by design (the
-- follow-up review that verifies the plan), is not a problem; dropped steps do not count towards closing a finding
-- twice. Superseded and abandoned plans are not checked.
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
-- when an unfinished step depends on it.
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
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = d.step_id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'goal', g.code, 'confirm', 'Approve a design goal', g.text, g.status, 1, g.adopted_at
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
FROM interview_questions;

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
SELECT p.code AS plan, s.number AS step, r.code AS review, f.point_id
FROM step_findings sf
JOIN steps s ON s.id = sf.step_id
JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
JOIN reviews r ON r.id = f.review_id
WHERE p.status NOT IN ('superseded','abandoned')
  AND NOT EXISTS (SELECT 1 FROM plan_reviews pr WHERE pr.plan_id = p.id AND pr.review_id = f.review_id);

-- Finished plan steps whose issues are still to fix.
-- Must be empty: when a step is done, its issues move on (fixed with after_state, or another closing status),
-- unless another unfinished step still works on them. Regressed issues are left to the next plan. Superseded and
-- abandoned plans are not checked.
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
INSERT INTO meta(key, value) VALUES ('schema_version','10');
INSERT INTO schema_migrations(version, description) VALUES (1, 'initial ledger schema'),
  (2, 'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted'),
  (3, 'integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers'),
  (4, 'check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes'),
  (5, 'one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries'),
  (6, 'lookup tables for the ledger''s words and labels; RESTRICT keys under reviews and findings; metrics.name catalogued'),
  (7, 'must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason'),
  (8, 'finding_locations.source (the reviewed text of a dirty working tree); v_next_codes for the write commands'),
  (9, 'owner interview (interview_questions, interview_answers) and per-domain design goals the owner approves'),
  (10, 'v_plan_totals, v_review_totals and v_pending_gates: the counts the viewer''s lists show');
"""
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review ledger</title>
<style>
:root{--bg:#f7f8f6;--surface:#fff;--fg:#1b2420;--muted:#5d6b64;--line:#dfe4e0;--field:#828f88;--accent:#0f6b57;--accent-soft:#e2f1ec;--warn:#9a5b00;--warn-soft:#fbefd9;--crit:#a3271f;--crit-soft:#fae6e3;--info:#2b5c99;--info-soft:#e5eefa;
 --display:ui-rounded,"SF Pro Rounded","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;--body:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color-scheme:light}
:root[data-theme="dark"]{--bg:#121715;--surface:#1a211e;--fg:#e6ece8;--muted:#9aa8a1;--line:#2c3632;--field:#6b7a73;--accent:#5fc9ae;--accent-soft:#17352d;--warn:#e7b25a;--warn-soft:#3a2c12;--crit:#f08d84;--crit-soft:#3d1c19;--info:#8db6ee;--info-soft:#1a2a40;color-scheme:dark}
*{box-sizing:border-box}
html{scroll-padding-top:var(--top,110px)}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 var(--body);padding:0 max(16px,env(safe-area-inset-right)) 64px max(16px,env(safe-area-inset-left))}
a{color:var(--accent)}
.skip{position:absolute;left:8px;top:-80px;z-index:10;background:var(--surface);padding:8px 12px;border:1px solid var(--field);border-radius:6px}.skip:focus{top:8px}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 0 0;margin:0 -4px}
.top .row{max-width:1180px;margin:0 auto;display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:0 4px}
.brand{font:600 17px var(--display)}
.brand small{color:var(--muted);font:400 13px var(--body);margin-left:8px}
.tabs{display:flex;gap:2px;overflow-x:auto;max-width:1180px;margin:8px auto 0;padding:0 4px}
.tabs a{text-decoration:none;color:var(--muted);padding:8px 12px;border-bottom:2px solid transparent;white-space:nowrap;font-weight:500}
.tabs a[aria-current="page"]{color:var(--fg);border-color:var(--accent)}
.badge{display:inline-block;min-width:20px;padding:0 6px;border-radius:999px;background:var(--warn);color:var(--bg);font:600 12px/20px var(--mono);text-align:center;margin-left:6px}
.theme{all:unset;margin-left:auto;cursor:pointer;color:var(--muted);font-size:13px;padding:4px 8px;border:1px solid var(--line);border-radius:6px}
h1{font:600 26px/1.15 var(--display);margin:18px 0 0;text-wrap:balance}
h1,h2,h3,.lede,.status,.note,.gate .q,.gate .ctx,.refline,.refline code,.opts label,.timeline>div,li{overflow-wrap:anywhere}
h2,h3.title{font:600 19px/1.2 var(--display);margin:8px 0 0;letter-spacing:normal;text-transform:none;color:inherit}
h3,h2.sec{font:600 12px var(--body);letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:14px 0 4px}
.lede{color:var(--muted);max-width:72ch;margin:4px 0 0}
.facts{display:flex;flex-wrap:wrap;gap:10px 28px;font-variant-numeric:tabular-nums}
.facts div b{font:600 22px var(--display);display:block}
.facts div span{color:var(--muted);font-size:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input,select,textarea,button{font:inherit;color:var(--fg)}
.field{background:var(--surface);border:1px solid var(--field);border-radius:6px;padding:8px 10px;min-width:0}
.tools input{flex:1 1 240px}
textarea{width:100%;min-height:72px;resize:vertical}
input[type=radio]{accent-color:var(--accent)}
button.btn{cursor:pointer;background:var(--accent);color:var(--bg);border:0;border-radius:6px;padding:8px 14px;font-weight:600}
button.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
button.btn:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
h1:focus{outline:none}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font:600 12px var(--body);color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:break-word;min-width:4ch}
.grid td{white-space:nowrap;max-width:360px;overflow:hidden;text-overflow:ellipsis}
td:first-child{white-space:nowrap}tr.link{cursor:pointer}tr.link:hover td{background:var(--accent-soft)}tr.link a{text-decoration:none}tr.link a:hover,tr.link a:focus-visible{text-decoration:underline}
.scroll{overflow-x:auto}
.num{font-variant-numeric:tabular-nums;text-align:right}
.mono,code{font:500 12.5px var(--mono)}
.muted{color:var(--muted)}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}
.stack{display:flex;flex-direction:column;gap:8px;min-width:0}.stack.loose{gap:10px}
.card-link{margin:10px 0}
.progress{display:flex;flex-direction:column;gap:4px;margin-top:4px}
.card.grid{padding:0}
.tag{display:inline-block;font:500 11px/1.6 var(--mono);padding:0 7px;border-radius:999px;background:var(--line);color:var(--fg);white-space:nowrap;margin:0 4px 2px 0}
.tag.H,.tag.crit{background:var(--crit-soft);color:var(--crit)}.tag.M,.tag.warn{background:var(--warn-soft);color:var(--warn)}
.tag.info{background:var(--info-soft);color:var(--info)}.tag.ok{background:var(--accent-soft);color:var(--accent)}.tag.solid{background:var(--accent);color:var(--bg)}.tag.you{background:var(--warn);color:var(--bg)}
details.item{background:var(--surface);border:1px solid var(--line);border-radius:8px}
details.item>summary{cursor:pointer;list-style:none;display:grid;grid-template-columns:minmax(90px,auto) minmax(0,1fr) auto;gap:10px;padding:10px 12px;align-items:baseline}
details.item>summary::-webkit-details-marker{display:none}
details.item>summary .n{font:500 13px var(--mono);color:var(--muted)}
details.item>summary .n::before{content:"▸ ";content:"▸ " / ""}details.item[open]>summary .n::before{content:"▾ ";content:"▾ " / ""}
details.item>summary .t{font-weight:500;overflow-wrap:anywhere}
details.item>summary .tags{display:flex;flex-wrap:wrap;justify-content:flex-end}
.body{padding:2px 14px 14px;display:flex;flex-direction:column;gap:10px}
dl.kv{margin:0;display:grid;grid-template-columns:150px minmax(0,1fr);gap:6px 14px}
dl.kv dt{color:var(--muted);font-size:13px}dl.kv dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.ref{border-top:1px solid var(--line);padding-top:8px;display:flex;flex-direction:column;gap:6px}
.refline{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;font-size:13px}.refline>*{min-width:0}
.code{background:var(--bg);border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:420px}
.code table{font:12.5px/1.45 var(--mono)}
.code td{border:0;padding:0 10px;white-space:pre;overflow-wrap:normal}
.code td.ln{color:var(--muted);text-align:right;user-select:none;width:1%}
.code tr.hit td{background:var(--warn-soft)}.code tr.hit td.ln{box-shadow:inset 3px 0 var(--warn);color:var(--fg)}
.note{font-size:13px;color:var(--muted)}
.mdtext{white-space:pre-wrap;overflow-wrap:anywhere;max-height:480px;overflow:auto;font-size:14px}
.status{padding:10px 12px;border-radius:6px;background:var(--accent-soft);color:var(--accent)}
.status.err{background:var(--crit-soft);color:var(--crit)}
.cols{display:grid;grid-template-columns:270px minmax(0,1fr);gap:24px;align-items:start}
nav.rail{display:flex;flex-direction:column;gap:2px;position:sticky;top:var(--top,110px)}
nav.rail button{all:unset;cursor:pointer;display:grid;grid-template-columns:24px 1fr auto;gap:8px;padding:7px 10px;border-radius:6px;font-size:14px;align-items:baseline}
nav.rail button:hover{background:var(--accent-soft)}
nav.rail button:focus-visible,.tlist button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
nav.rail button[aria-current="true"]{background:var(--accent);color:var(--bg)}
nav.rail .i,nav.rail .c{font:500 12px var(--mono);opacity:.8}
.bar{height:8px;border-radius:999px;background:var(--line);overflow:hidden;display:flex;max-width:520px}.bar i{display:block;height:100%}
.bar .d{background:var(--accent)}.bar .y{background:var(--warn)}.bar .g{background:var(--info)}
.gate{display:flex;flex-direction:column;gap:10px}
.gate .q{font-weight:500;white-space:pre-wrap}
.gate .ctx{white-space:pre-wrap;color:var(--muted);font-size:14px;max-height:260px;overflow:auto}
.opts{display:flex;flex-direction:column;gap:6px}
.opts label{display:grid;grid-template-columns:20px 1fr;gap:6px;padding:8px 10px;border:1px solid var(--field);border-radius:6px;cursor:pointer}
.opts label:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.opts small{display:block;color:var(--muted)}
.timeline{display:flex;flex-direction:column;gap:0;border-left:2px solid var(--line);margin-left:6px}
.timeline>div{padding:4px 0 8px 14px;position:relative;font-size:14px}
.timeline>div::before{content:"";position:absolute;left:-6px;top:10px;width:10px;height:10px;border-radius:50%;background:var(--accent)}
.empty{color:var(--muted);padding:18px 0}
.chart svg{width:100%;height:auto;display:block}
.split{display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px;align-items:start}
.tlist{display:flex;flex-direction:column;gap:1px;max-height:70vh;overflow:auto;padding:4px}
.tlist button{all:unset;cursor:pointer;display:flex;justify-content:space-between;gap:8px;padding:5px 8px;border-radius:5px;font:13px var(--mono)}
.tlist button:hover{background:var(--accent-soft)}.tlist button[aria-current="true"]{background:var(--accent);color:var(--bg)}
@media (max-width:600px){.tabs{flex-wrap:wrap;overflow-x:visible}.top{position:static}}
@media (max-width:820px){.cols,.split{grid-template-columns:minmax(0,1fr)}nav.rail{position:static;flex-direction:row;overflow-x:auto;padding:4px}nav.rail button{grid-template-columns:auto auto auto;white-space:nowrap;border:1px solid var(--line)}
 details.item>summary{grid-template-columns:auto minmax(0,1fr)}details.item>summary .tags{grid-column:1/-1;justify-content:flex-start}dl.kv{grid-template-columns:minmax(0,1fr)}dl.kv dt{margin-top:6px}.tlist{max-height:220px}}
</style>
</head>
<body>
<a class="skip" id="skip" href="#app">Skip to content</a>
<header class="top">
  <div class="row"><span class="brand" id="brand">Review ledger</span><button class="theme" id="theme" type="button">Theme: Automatic</button></div>
  <nav class="tabs" id="tabs" aria-label="Sections"></nav>
</header>
<main class="wrap" id="app"></main>
<div class="visually-hidden" id="live" role="status"></div>
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

// ---------- the shared copy
// `ledger.py progress-page` writes this page as one file with every API answer embedded (window.LEDGER_SNAPSHOT).
// STATIC is that data; in the viewer it is null. With it the page answers api() from the data, routes on hash names
// an artifact allows (#issue-ISS-0019), shows the How it works tab, and saves gate answers in the artifact's
// database (collection "answers", one document per gate code) for `ledger.py import-answers`.
const STATIC = (s => s && typeof s === "object" && s.responses ? s : null)(window.LEDGER_SNAPSHOT);
let CUR_PATH = null;
const ACTIVE = STATIC ? STATIC.active : [];
const slugOf = p => (p || "").split("?")[0].replace(/^#\/?/, "").replace(/[\/_]/g, "-").replace(/[^A-Za-z0-9-]/g, "") || "home";
function pathFromSlug(s){
  s = (s || "").replace(/^#/, "");
  if (!s || s === "home") return STATIC && STATIC.start && CUR_PATH === null ? STATIC.start : "";
  const m = s.match(/^([a-z]+)(?:-(.*))?$/); if (!m) return "";
  const [, view, rest] = m;
  if (!rest) return view;
  if (view === "plan") { const p = rest.match(/^(P-\d+)(?:-phase-(\d+))?$/); return p ? `plan/${p[1]}` + (p[2] ? `/phase/${p[2]}` : "") : "plans"; }
  if (view === "explore") { const t = (STATIC.responses.tables || []).find(t => t.name.replace(/_/g, "-") === rest); return t ? `explore/${t.name}` : "explore"; }
  return `${view}/${rest}`;
}
function nav(hash){ CUR_PATH = hash.replace(/^#\/?/, ""); const s = slugOf(CUR_PATH); if (location.hash.slice(1) === s) route(); else location.hash = s; }
function setPath(hash){ if (!STATIC) { history.replaceState(null, "", hash); return; } CUR_PATH = hash.replace(/^#\/?/, ""); try { history.replaceState(null, "", "#" + slugOf(CUR_PATH)); } catch {} }
function currentPath(){
  if (!STATIC) return location.hash.replace(/^#\/?/, "");
  const s = location.hash.slice(1);
  if (CUR_PATH === null || slugOf(CUR_PATH) !== (s || "home")) CUR_PATH = pathFromSlug(s);
  return CUR_PATH;
}
function staticApi(path, body){
  if (body !== undefined) return Promise.reject(new Error("This shared copy is read-only. Answer in the chat with Claude, or run the local viewer."));
  const S = STATIC.responses, [p, q] = path.split("?"), qs = new URLSearchParams(q || "");
  const copy = v => JSON.parse(JSON.stringify(v));
  if (p === "issues") {
    const st = qs.get("status"), sv = qs.get("severity"), dm = qs.get("domain_code"), t = (qs.get("q") || "").toLowerCase();
    return Promise.resolve(copy(S.issues.filter(i => (!st || (st === "active" ? ACTIVE.includes(i.status) : i.status === st))
      && (!sv || i.severity === sv) && (!dm || i.domain_code === dm) && (!t || i.search.toLowerCase().includes(t)))));
  }
  if (p === "gates") return Promise.resolve(copy(S["gates?status=" + (qs.get("status") || "pending")] || S["gates?status=all"]));
  if (p.startsWith("table/")) {
    const t = S[p]; if (!t) return Promise.reject(new Error("That table is not in this snapshot."));
    const off = Number(qs.get("offset")) || 0, lim = Number(qs.get("limit")) || 100;
    return Promise.resolve({columns: t.columns, rows: copy(t.rows.slice(off, off + lim)), count: t.count, offset: off, limit: lim});
  }
  if (S[p] === undefined) return Promise.reject(new Error("This view is not in the snapshot."));
  return Promise.resolve(copy(S[p]));
}

const HUB = {db: null, answers: {}, error: null};
const OPEN_GATES = () => STATIC ? (STATIC.responses["gates?status=pending"].gates || []).filter(g => g.status === "open").map(g => g.code) : [];
const hubAnsweredOpen = () => OPEN_GATES().filter(c => HUB.answers[c]).length;
async function hubInit(){
  if (!STATIC || !window.claude || typeof window.claude.use !== "function") return;
  try {
    const db = await window.claude.use("db"); if (!db) return;
    HUB.db = db;
    db.collection("answers").onSnapshot(snap => {
      HUB.answers = Object.fromEntries(snap.docs.map(d => [d.id, d.data()]));
      const cur = document.querySelector('#tabs a[aria-current="page"]');
      if (OVERVIEW) drawTabs(cur ? (cur.dataset.k || "") : "");
    }, e => { HUB.error = e && e.code; });
  } catch (e) { HUB.error = String(e); }
}
function md(text){
  const lines = String(text || "").split("\n"), out = []; let i = 0;
  const inline = str => { const parts = []; const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g; let last = 0, m;
    while ((m = re.exec(str))) { if (m.index > last) parts.push(str.slice(last, m.index)); const t = m[0];
      if (t.startsWith("**")) parts.push(h("b", null, t.slice(2, -2)));
      else if (t.startsWith("`")) parts.push(h("code", null, t.slice(1, -1)));
      else { const mm = t.match(/^\[([^\]]+)\]\((.+)\)$/); parts.push(h("a", {href: mm[2], target: "_blank", rel: "noopener noreferrer"}, mm[1])); }
      last = re.lastIndex; }
    if (last < str.length) parts.push(str.slice(last)); return parts; };
  const cells = r => r.replace(/^\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
  while (i < lines.length) {
    const l = lines[i]; let m;
    if (!l.trim()) { i++; continue; }
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { out.push(h("h" + Math.min(m[1].length + 1, 4), null, inline(m[2]))); i++; continue; }
    if (l.startsWith("|")) { const rows = []; while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      out.push(h("div", {class: "scroll"}, h("table", null, h("thead", null, h("tr", null, cells(rows[0]).map(c => h("th", null, inline(c))))),
        h("tbody", null, rows.slice(2).map(r => h("tr", null, cells(r).map(c => h("td", null, inline(c))))))))); continue; }
    if (/^(\d+\.|-)\s/.test(l)) { const ordered = /^\d+\./.test(l), items = []; while (i < lines.length && /^(\d+\.|-)\s/.test(lines[i])) items.push(lines[i++].replace(/^(\d+\.|-)\s+/, ""));
      out.push(h(ordered ? "ol" : "ul", null, items.map(t => h("li", null, inline(t))))); continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\||\d+\.\s|-\s)/.test(lines[i])) para.push(lines[i++]);
    out.push(h("p", null, inline(para.join(" "))));
  }
  return h("div", {class: "md"}, out);
}
function hubGateBody(card, g, hid){
  const saved = HUB.answers[g.code];
  if (saved) { add(card, h("p", {class: "status"}, "You answered: ", answerText({options: g.options, ...saved}, true), ". Claude records it the next time it checks (or tell Claude in the chat to check now).")); return card; }
  if (!HUB.db) { add(card, h("p", {class: "note"}, "Answer in the chat with Claude; answering here needs you signed in to claude.ai as this page's owner.")); return card; }
  const nm = "opt-" + g.code;
  const opts = (g.options || []).length ? h("div", {class: "opts", role: "radiogroup", "aria-labelledby": hid},
    g.options.map(o => h("label", null, h("input", {type: "radio", name: nm, value: o.value}), h("span", null, o.label, o.description ? h("small", null, o.description) : null)))) : null;
  const ta = g.allow_text ? h("textarea", {class: "field", placeholder: opts ? "Add a note or a different answer (optional)" : "Your answer", id: hid + "-text", "aria-label": "Your answer", "aria-labelledby": `${hid}-text ${hid}`}) : null;
  const msg = h("div"), form = h("div", {class: "gate"});
  const send = h("button", {class: "btn", type: "button", id: hid + "-send", "aria-labelledby": `${hid}-send ${hid}`, onclick: async () => {
    const picked = card.querySelector(`input[name="${nm}"]:checked`), val = picked ? picked.value : null, txt = ta ? ta.value.trim() : "";
    if (!val && !txt) { put(msg, errorBox("Choose an option or write an answer.")); return; }
    send.disabled = true;
    try {
      await HUB.db.doc("answers/" + g.code).set({gate: g.code, answer_value: val, answer_text: txt || null, answered_at: new Date().toISOString()});
      HUB.answers[g.code] = {answer_value: val, answer_text: txt || null};
      put(form, h("p", {class: "status"}, "Saved. Claude records it the next time it checks (or tell Claude in the chat to check now)."));
      say("Answer saved.");
      drawTabs("inbox");
    } catch (e) { put(msg, errorBox("Your answer could not be saved here" + (e && e.code === "invalid_argument" ? ": only this page's owner can answer." : ". Try again, or answer in the chat."))); send.disabled = false; }
  }}, "Send answer");
  add(form, opts, ta, h("div", null, send), msg); add(card, form); return card;
}

if (STATIC) document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest('a[href^="#/"]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey) return;
  e.preventDefault(); nav(a.getAttribute("href"));
});

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
  if (STATIC) return staticApi(path, body);
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
  decision:"decision_status", goal:"goal_status", via:"answered_via"};
const FALLBACK = {sev:{H:"High",M:"Medium",L:"Low",T:"Missing test"}, need:{none:""}};
let L = {};
const setLabels = enums => { L = Object.fromEntries(Object.entries(ENUM_OF).map(([k, e]) =>
  [k, Object.assign({}, FALLBACK[k] || {}, (enums || {})[e] || {})])); };
setLabels({});
const lab = (map, v) => (L[map] && L[map][v] !== undefined) ? L[map][v] : words(v);
const domainName = c => c ? (LABELS.domains[c] || words(c)) : "";
const themeName = c => c ? (LABELS.themes[c] || words(c)) : "";
// The colour of a tag, by label map and stored value: the one place a value gets its tone.
const TONE = {
  issue:{open:"warn", "needs-decision":"you", regressed:"crit", planned:"info", "in-progress":"info", fixed:"ok", verified:"solid"},
  match:{recurrence:"crit", regression:"crit", new:"info"},
  verif:{confirmed:"ok"},
  gate:{"production-action":"crit"}, gstat:{open:"you"},
  goal:{approved:"ok", proposed:"you"},
  step:{done:"solid", doing:"ok", "waiting-owner":"you"},
  need:{production:"crit", decision:"warn", confirm:"info", review:"info"},
  area:{included:"ok"},
};
const tone = (map, v) => (TONE[map] && TONE[map][v]) || "";
const tag = (text, cls, title) => h("span", {class:"tag" + (cls ? " " + cls : ""), title}, text);
const labTag = (map, v) => tag(lab(map, v), tone(map, v));
const sevTag = s => tag(lab("sev", s), s, "Severity");
const issueTag = s => labTag("issue", s);
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
// A stored time in the reader's own words and time zone. Stored times are UTC: "2026-01-02T03:04:05.000Z", also
// with a space for the T, without the Z, or with an offset. A date alone is shown as that day. Nothing gives "",
// and text that is not a time gives "date unknown".
const NO_DATE = "date unknown";
function when(stored, withTime=true){
  if (stored == null || stored === "") return "";
  const text = String(stored).trim(), valid = d => !isNaN(d) && d.toISOString().slice(0, 10);
  let m = /^(\d{4})-(\d\d)-(\d\d)$/.exec(text);
  if (m) {
    const d = new Date(Date.UTC(+m[1], m[2] - 1, +m[3]));
    return valid(d) === text ? d.toLocaleDateString(undefined, {dateStyle:"medium", timeZone:"UTC"}) : NO_DATE;
  }
  m = /^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d)(:\d\d)?(\.\d{1,3})?\d*\s*(Z|[+-]\d\d:?\d\d)?$/i.exec(text);
  if (!m) return NO_DATE;
  const zone = (m[5] || "Z").replace(/^([+-]\d\d)(\d\d)$/, "$1:$2").toUpperCase();
  const d = new Date(`${m[1]}T${m[2]}${m[3] || ":00"}${m[4] || ""}${zone}`);
  if (!valid(d) || valid(new Date(m[1] + "T00:00:00Z")) !== m[1]) return NO_DATE;
  return d.toLocaleString(undefined, withTime ? {dateStyle:"medium", timeStyle:"short"} : {dateStyle:"medium"});
}
// The same, for a table cell or a value shown on its own: text that is not a time keeps what was stored as a tooltip.
function whenCell(stored, withTime){
  const t = when(stored, withTime);
  return t === NO_DATE ? h("span", {title:String(stored)}, t) : t;
}
const short = sha => sha ? sha.slice(0, 10) : "";
const kv = pairs => h("dl", {class:"kv"}, pairs.filter(p => p[1] != null && p[1] !== "" && !(Array.isArray(p[1]) && !p[1].length)).map(([k,v]) => [h("dt", null, k), h("dd", null, v)]));
const empty = t => h("p", {class:"empty"}, t);
const sect = title => h("h2", {class:"sec"}, title);   // a section of a page: second level, in the small style
// Words for a screen reader about something that just happened (a count after filtering, an answer saved, an error).
const say = text => { document.getElementById("live").textContent = text || ""; };
function errorBox(e, quiet){
  const text = e && e.message ? e.message : String(e);
  if (!quiet) say(text);
  return h("p", {class:"status err"}, text);
}
const fact = (n, label) => h("div", null, h("b", null, n), h("span", null, label));
// One expandable row: number, title, tags, and the body shown when it is opened.
function item(n, title, tags, body){
  return h("details", {class:"item"},
    h("summary", null, h("span", {class:"n"}, n), h("span", {class:"t"}, title), h("span", {class:"tags"}, tags)),
    h("div", {class:"body"}, body));
}
const select = (label, options, attrs) => h("select", {class:"field", "aria-label":label, ...attrs}, options.map(([v, t]) => h("option", {value:v}, t)));
// A row of filters. onChange(true) runs when a select changes and as a search box is typed in (after `wait` ms of quiet).
function filterBar(controls, onChange, wait=0){
  let timer;
  const now = () => onChange(true), typed = wait ? () => { clearTimeout(timer); timer = setTimeout(now, wait); } : now;
  for (const c of controls) c.addEventListener(c.tagName === "SELECT" ? "change" : "input", c.tagName === "SELECT" ? now : typed);
  return h("div", {class:"tools"}, controls);
}
// A gate's answer as the owner gave it: the chosen option's label, then the note.
function answerText(g, strong){
  const label = g.answer_value ? optLabel(g) : "", note = g.answer_text || "";
  if (!strong) return [label, note].filter(Boolean).join(" — ");
  return [label ? h("b", null, label) : null, note ? (label ? " — " : "") + note : null];
}
// A table. href(row) gives the page a row opens ("#/issue/ISS-0001"), or nothing for a row that opens none: the row's
// first cell (or the column marked link) is then a real link, and a click anywhere on the row follows it.
function table(cols, data, href, none="Nothing here yet."){
  if (!data.length) return empty(none);
  const at = Math.max(0, cols.findIndex(c => c.link));
  const row = r => {
    const to = href && href(r);
    const cell = (c, i) => { const v = c.get ? c.get(r) : (r[c.key] ?? ""); return h("td", {class:c.num ? "num" : null}, to && i === at ? h("a", {href:to}, v) : v); };
    const open = e => { if (!e.target.closest("a, button") && !String(getSelection())) go(to); };
    return h("tr", to ? {class:"link", onclick:open} : null, cols.map(cell));
  };
  return h("div", {class:"scroll"}, h("table", null,
    h("thead", null, h("tr", null, cols.map(c => h("th", {class:c.num ? "num" : null}, c.label)))),
    h("tbody", null, data.map(row))));
}
// name: what a screen reader calls the button when the page has several with the same label.
function copyBtn(text, label="Copy", name){
  return h("button", {class:"btn ghost", type:"button", "aria-label":name, onclick:async e => {
    try { await navigator.clipboard.writeText(text); e.target.textContent = "Copied"; } catch { e.target.textContent = "Select and copy by hand"; }
    say(e.target.textContent);
  }}, label);
}
function go(hash){ if (STATIC) return nav(hash); location.hash = hash; }
// Draws a view. After the first one, focus moves to the new page's heading, so the keyboard and a screen reader
// start where the page starts.
let DRAWN = false;
function focusHeading(){ const t = document.querySelector("#app h1") || document.getElementById("app"); t.tabIndex = -1; t.focus({preventScroll:true}); }
function setView(...kids){
  put(document.getElementById("app"), kids); window.scrollTo(0,0);
  if (DRAWN) focusHeading();
  DRAWN = true;
}
function failed(err){ setView(h("h1", null, "Could not load this view"), errorBox(err), h("p", {class:"lede"}, STATIC ? "This is a read-only snapshot; reload the page to start again." : "Make sure the viewer is still running in your terminal, then reload the page.")); }

// ---------- code at a commit
// point: the id of the point citing these lines (it tells this reference's buttons from another's).
function codeRef(loc, point){
  const box = h("div");
  const range = loc.line_start ? (loc.line_end && loc.line_end !== loc.line_start ? `lines ${loc.line_start}–${loc.line_end}` : `line ${loc.line_start}`) : "";
  const where = `${point}, ${loc.path}${range ? ", " + range : ""}, commit ${short(loc.commit_sha)}`;
  const label = t => { btn.textContent = t; btn.setAttribute("aria-label", `${t}: ${where}`); };
  let busy = false;   // not `disabled`: a disabled button drops the keyboard focus
  const btn = h("button", {class:"btn ghost", type:"button", onclick:async () => {
    if (busy) return;
    if (box.childElementCount) { put(box, ); label("Show code at this commit"); return; }
    busy = true; btn.setAttribute("aria-busy", "true");
    try {
      const s = await api("source/" + loc.id);
      const hi = [s.line_start, s.line_end || s.line_start];
      const notes = [];
      if (s.source === "excerpt") notes.push("The commit could not be read from git here, so this is the copy of the lines saved at review time. " + (s.git_error ? "Git said: " + s.git_error : ""));
      if (s.source === "none") notes.push("The commit could not be read from git and no copy of the lines was saved. " + (s.git_error || ""));
      if (s.excerpt_matches === false) notes.push("The saved copy of these lines differs from the file at this commit; the line numbers may have been recorded wrong.");
      put(box, 
        notes.length ? h("p", {class:"note"}, notes.join(" ")) : null,
        s.lines && s.lines.length ? h("div", {class:"code"}, h("table", null, h("tbody", null, s.lines.map(l => {
            const cited = hi[0] && l.n >= hi[0] && l.n <= hi[1];
            return h("tr", {class: cited ? "hit" : null}, h("td", {class:"ln"}, l.n, cited ? h("span", {class:"visually-hidden"}, " cited") : null), h("td", null, l.text)); })))) : null,
        h("div", {class:"refline"}, h("code", null, s.git_command), copyBtn(s.git_command, "Copy git command", "Copy git command: " + where),
          s.permalink ? extLink(s.permalink, "Open on the code host") : null));
      label("Hide code");
    } catch (e) { put(box, errorBox(e)); }
    busy = false; btn.removeAttribute("aria-busy");
  }});
  label("Show code at this commit");
  return h("div", {class:"ref"},
    h("div", {class:"refline"},
      tag(words(loc.role)), h("b", null, loc.repo), h("code", null, loc.path), range ? h("span", null, range) : null,
      loc.symbol ? h("code", {class:"muted"}, loc.symbol) : null,
      h("span", {class:"muted"}, "at commit ", h("code", null, short(loc.commit_sha))), btn),
    box);
}

function findingItem(f, opts={}){
  const id = opts.showReview ? `${f.review_code} · ${f.point_id}` : f.point_id;
  return item(id, f.summary,
    [sevTag(f.severity), f.match_type ? labTag("match", f.match_type) : null,
     f.verification === "false" ? tag("False finding") : f.verification === "confirmed" ? tag("Confirmed", tone("verif", "confirmed")) : null],
    [ kv([["Area", domainName(f.domain_code)], ["Theme", themeName(f.theme_code)], ["Kind", lab("kind", f.kind)],
          ["Issue", f.issue_code && !opts.hideIssue ? h("a", {href:"#/issue/" + f.issue_code}, f.issue_code) : null],
          ["How it was matched", f.match_rationale], ["What it does now", f.before_state], ["Detail", f.detail],
          ["Recommended fix", f.recommendation], ["Test to add", f.recommended_test], ["Evidence", f.evidence],
          ["Checked by the manager", f.verification ? lab("verif", f.verification) + (f.verification_note ? " — " + f.verification_note : "") : null],
          ["Reviewer", f.run_label], ["In the report", f.where_report]]),
      (f.locations || []).map(loc => codeRef(loc, id))]);
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
      fact(o.queue, "waiting on you"), fact(openTotal, "open issues"), fact(sev.H || 0, "open and high"), fact(o.recurring, "came back or seen again")),
    o.queue ? h("p", {class:"status"}, h("a", {href:"#/inbox"}, plural(o.queue, "thing needs", "things need") + " your input or approval →")) : null,
    h("div", {class:"grid2"},
      h("div", {class:"card"}, h("h2", null, "Latest reviews"),
        table([{label:"Review", get:r => r.code}, {label:"Title", key:"title"}, {label:"Status", get:r => lab("review", r.status)},
               {label:"Started", get:r => whenCell(r.started_at, false)}, {label:"High", key:"high", num:1}, {label:"Points", key:"points", num:1}],
              o.reviews, r => "#/review/" + r.code)),
      h("div", {class:"card"}, h("h2", null, "Fix plans"),
        o.plans.length ? o.plans.map(p => h("div", {class:"card-link"},
          h("a", {href:"#/plan/" + p.code}, `${p.code} · ${p.title}`), h("span", {class:"muted"}, " — " + lab("plan", p.status)),
          progressBar(p.done || 0, 0, p.waiting || 0, p.steps || 0))) : empty("No fix plan yet."))),
    h("div", {class:"card"}, h("h2", null, "Ledger checks"),
      h("p", {class:"note"}, "These should all be zero once a review has been indexed and matched."),
      table([{label:"Check", key:"label"}, {label:"Count", key:"n", num:1}], o.checks, c => c.n ? "#/explore/" + c.view : null),
      failing.length ? errorBox(plural(failing.length, "check needs", "checks need") + " attention.", true) : null));
};

function progressBar(done, doing, waiting, total){
  const pct = n => total ? (100 * n / total).toFixed(2) + "%" : "0";
  return h("div", {class:"progress"},
    h("div", {class:"bar", role:"img", "aria-label":`${done} of ${total} done`},
      h("i", {class:"d", style:`width:${pct(done)}`}), h("i", {class:"g", style:`width:${pct(doing)}`}), h("i", {class:"y", style:`width:${pct(waiting)}`})),
    h("span", {class:"note"}, `${done} of ${total} done` + (doing ? ` · ${doing} in progress` : "") + (waiting ? ` · ${waiting} waiting on you` : "")));
}

VIEWS.inbox = async () => {
  const d = await api("gates?status=pending");
  const open = d.gates.filter(g => g.status === "open"), answered = d.gates.filter(g => g.status === "answered");
  setView(
    h("h1", null, "Waiting on you"),
    h("p", {class:"lede"}, STATIC ? "Questions and approvals the agent cannot get past without you. Answer here: your answer is saved with this page, and Claude records it the next time it checks."
      : "Questions and approvals the agent cannot get past without you. Answer here or in the chat; the agent picks answers up the next time it checks the ledger."),
    open.length ? open.map(g => gateCard(g)) : empty("Nothing is waiting on you right now."),
    answered.length ? [sect("Answered, waiting for the agent to act"), answered.map(g => gateCard(g, "h3"))] : null,
    d.other.length ? [sect("Also waiting on you (answer in the chat)"),
      table([{label:"What", get:r => (r.kind === "step" ? "Plan step " : r.kind === "goal" ? "Design goal " : "Decision ") + r.ref}, {label:"Needs", get:r => lab("need", r.need) || "Your input"},
             {label:"Title", key:"title"}, {label:"Question", key:"question"}], d.other)] : null,
    h("p", null, h("a", {href:"#/history"}, "See every question and answer so far")));
};

VIEWS.history = async () => {
  const d = await api("gates?status=all");
  setView(h("h1", null, "Questions and answers"),
    table([{label:"Asked", get:g => whenCell(g.created_at)}, {label:"About", get:g => lab("gate", g.kind)}, {label:"Title", key:"title"},
           {label:"Status", get:g => lab("gstat", g.status)}, {label:"Answer", get:g => [g.answer_value ? optLabel(g) : "", g.answer_text ? " " + g.answer_text : ""]},
           {label:"What the agent did", key:"applied_note"}], d.gates));
};
const optLabel = g => ((g.options || []).find(o => o.value === g.answer_value) || {}).label || words(g.answer_value);

// level: "h3" for a gate listed under a section heading.
function gateCard(g, level){
  const hid = "gate-" + g.code;   // the title's id: it names this gate's choices, text box and button
  const refs = [g.review_code && h("a", {href:"#/review/" + g.review_code}, "Review " + g.review_code),
                g.plan_code && h("a", {href:"#/plan/" + g.plan_code}, "Plan " + g.plan_code + (g.step_number ? " step " + g.step_number : "")),
                g.issue_code && h("a", {href:"#/issue/" + g.issue_code}, "Issue " + g.issue_code)].filter(Boolean);
  const card = h("div", {class:"card gate"},
    h("div", null, tag(lab("gate", g.kind), tone("gate", g.kind) || tone("gstat", g.status) || "info"),
      g.blocking && g.status === "open" ? tag("Work is paused until you answer", "warn") : null,
      h("span", {class:"note"}, " Asked " + when(g.created_at))),
    h(level || "h2", {id:hid, class: level ? "title" : null}, g.title), h("div", {class:"q"}, g.question),
    g.context ? h("div", {class:"ctx"}, g.context) : null,
    refs.length ? h("div", {class:"refline"}, refs) : null);
  if (g.status === "answered") {
    add(card, h("p", {class:"status"}, "You answered: ", answerText(g, true), ` (${when(g.answered_at)})`));
    return card;
  }
  if (STATIC) return hubGateBody(card, g, hid);
  const name = "opt-" + g.code;
  const opts = (g.options || []).length ? h("div", {class:"opts", role:"radiogroup", "aria-labelledby":hid},
    g.options.map(o => h("label", null, h("input", {type:"radio", name, value:o.value}), h("span", null, o.label, o.description ? h("small", null, o.description) : null)))) : null;
  const ta = g.allow_text ? h("textarea", {class:"field", placeholder: opts ? "Add a note or a different answer (optional)" : "Your answer", id:hid + "-text", "aria-label":"Your answer", "aria-labelledby":`${hid}-text ${hid}`}) : null;
  const msg = h("div");
  const send = h("button", {class:"btn", type:"button", id:hid + "-send", "aria-labelledby":`${hid}-send ${hid}`, onclick:async () => {
    const picked = card.querySelector(`input[name="${name}"]:checked`);
    send.disabled = true;
    try {
      await api(`gates/${encodeURIComponent(g.code)}/answer`, {answer_value: picked ? picked.value : null, answer_text: ta ? ta.value : null});
      put(msg, h("p", {class:"status"}, "Answer saved. The agent will act on it the next time it checks."));
      say("Answer saved.");
      setTimeout(() => route(), 1200);
    } catch (e) { put(msg, errorBox(e)); send.disabled = false; }
  }}, "Send answer");
  add(card, opts, ta, h("div", null, send), msg);
  return card;
}

VIEWS.reviews = async () => {
  const list = await api("reviews");
  setView(h("h1", null, "Reviews"), h("p", {class:"lede"}, "Every review run on this project, newest first."),
    table([{label:"Review", key:"code"}, {label:"Title", key:"title"}, {label:"Kind", get:r => lab("scope", r.scope)},
           {label:"Status", get:r => lab("review", r.status)}, {label:"Started", get:r => whenCell(r.started_at)}, {label:"Code reviewed", get:r => h("span", {class:"mono"}, r.commits || "")},
           {label:"High", key:"high", num:1}, {label:"Medium", key:"medium", num:1}, {label:"Low", key:"low", num:1}, {label:"New", key:"new_issues", num:1}, {label:"Came back", key:"came_back", num:1}],
          list, r => "#/review/" + r.code));
};

const REQ_VERDICT = {matches:"matches", differs:"differs", partly:"partly matches", missing:"missing", untested:"not tested"};
VIEWS.review = async ([code]) => {
  const d = await api("review/" + encodeURIComponent(code)); const r = d.review;
  const fs = d.findings;
  const sel = (id, label, all, values, fmt) => select(label, [["", all], ...[...new Set(values)].filter(v => v != null).map(v => [v, fmt(v)])], {id});
  const q = h("input", {class:"field", type:"search", placeholder:"Search points, ids, files", "aria-label":"Search points"});
  const fd = sel("fd", "Area", "All areas", fs.map(f => f.domain_code), domainName), fsv = sel("fs", "Severity", "All severities", ["H","M","L","T"].filter(s => fs.some(f => f.severity === s)), v => lab("sev", v));
  const fm = sel("fm", "Match", "Any match", fs.map(f => f.match_type), v => lab("match", v)), fv = sel("fv", "Check", "Any check", fs.map(f => f.verification), v => lab("verif", v));
  const listBox = h("div", {class:"stack"});
  let shown = 100;
  const draw = () => {
    const t = q.value.trim().toLowerCase();
    const hit = fs.filter(f => (!fd.value || f.domain_code === fd.value) && (!fsv.value || f.severity === fsv.value) && (!fm.value || f.match_type === fm.value) && (!fv.value || f.verification === fv.value)
      && (!t || [f.point_id, f.summary, f.detail, f.issue_code, ...(f.locations||[]).map(l => l.path)].join(" ").toLowerCase().includes(t)));
    const count = `${hit.length} of ${fs.length} points`;
    const more = () => { const first = shown; shown += 200; draw(); listBox.querySelectorAll("details.item > summary")[first].focus(); };
    put(listBox, h("p", {class:"note"}, count), hit.slice(0, shown).map(f => findingItem(f)),
      hit.length > shown ? h("button", {class:"btn ghost", type:"button", onclick:more}, `Show more (${hit.length - shown} left)`) : null);
    return count;
  };
  const filters = filterBar([q, fd, fsv, fm, fv], () => { shown = 100; say(draw()); });
  const sumBy = k => fs.filter(f => f.severity === k).length;
  setView(
    h("h1", null, `${r.code} · ${r.title}`),
    h("p", {class:"lede"}, `${lab("scope", r.scope)} · ${lab("review", r.status)} · started ${when(r.started_at)}` + (r.completed_at ? ` · finished ${when(r.completed_at)}` : "")),
    h("div", {class:"facts"}, fact(fs.length, "points"), ["H","M","L","T"].map(s => fact(sumBy(s), lab("sev", s))),
      fact(fs.filter(f => ["new","continuation"].includes(f.match_type)).length, "new issues"),
      fact(fs.filter(f => ["recurrence","regression"].includes(f.match_type)).length, "came back")),
    d.gates.length ? h("div", {class:"card"}, h("h2", null, "Questions asked during this review"), table([{label:"About", get:g => lab("gate", g.kind)}, {label:"Title", key:"title"}, {label:"Status", get:g => lab("gstat", g.status)}, {label:"Answer", get:g => [g.answer_value || "", " ", g.answer_text || ""]}], d.gates, () => "#/inbox")) : null,
    h("div", {class:"card"}, kv([["Verdict", r.verdict], ["Summary", r.summary], ["What was asked", r.request_text], ["Run by", r.manager ? `${r.manager}${r.manager_model ? " (" + r.manager_model + ")" : ""}` : null],
      ["Domains approved by", r.approved_by ? `${r.approved_by}, ${when(r.approved_at)}` : null], ["Follows", r.previous_code ? h("a", {href:"#/review/" + r.previous_code}, r.previous_code) : null],
      ["Write-up", r.summary_doc_url ? extLink(r.summary_doc_url, "Open the review document") : null]])),
    r.report_md ? h("div", {class:"card"}, h("h2", null, "The review document"), (STATIC ? md(r.report_md) : h("div", {class:"mdtext"}, r.report_md))) : null,
    sect("Code reviewed"),
    table([{label:"Repository", key:"name"}, {label:"Branch", key:"branch"}, {label:"Commit", get:x => [h("code", null, short(x.commit_sha)), " ", copyBtn(x.commit_sha, "Copy", "Copy commit " + short(x.commit_sha))]},
           {label:"Commit message", key:"commit_subject"}, {label:"Kept by tag", get:x => x.tag_name ? x.tag_name + (x.tag_pushed ? "" : " (not pushed)") : "Not tagged"},
           {label:"Uncommitted changes", get:x => x.is_dirty ? (x.dirty_note || "Yes") : "None"}], d.repos),
    d.goals.length ? [sect("Design goals used"), h("ul", null, d.goals.map(g => h("li", null, g.text)))] : null,
    (d.interview || []).length ? interviewSection(d.interview) : null,
    (d.review_goals || []).length ? [sect("Design goals from this interview"), goalsTable(d.review_goals)] : null,
    sect("Areas reviewed and skipped"),
    table([{label:"Area", get:x => x.name}, {label:"Reviewed?", get:x => tag(x.decision === "included" ? "Reviewed" : "Skipped", tone("area", x.decision))},
           {label:"Why", key:"reason"}, {label:"Your change", get:x => x.owner_change === "none" ? "" : words(x.owner_change)}, {label:"Reviewers", key:"reviewers_planned", num:1}], d.domains),
    sect("Reviewers"),
    table([{label:"Reviewer", key:"label"}, {label:"Area", get:x => domainName(x.domain_code)}, {label:"Agent", get:x => x.model ? `${x.actor} (${x.model})` : x.actor},
           {label:"Status", get:x => lab("run", x.status)}, {label:"Items", key:"items_reviewed", num:1}, {label:"High", key:"high_count", num:1}, {label:"Medium", key:"medium_count", num:1}, {label:"Low", key:"low_count", num:1}], d.runs),
    d.agents.length ? [sect("Agents"),
      table([{label:"Agent", key:"name"}, {label:"Did", key:"role"}, {label:"Kind", get:x => words(x.kind)}, {label:"Model", key:"model"}, {label:"Run in", key:"harness"}, {label:"Started by", key:"parent"}], d.agents)] : null,
    d.requirements.length ? [sect("Requirements"),
      h("p", {class:"note"}, [...new Map(d.requirements.map(x => [x.verdict, x.verdict_count]))].map(([v, n]) => `${n} ${REQ_VERDICT[v] || words(v)}`).join(" · ")),
      table([{label:"Requirement", key:"requirement"}, {label:"Says", key:"text"}, {label:"Found", get:x => REQ_VERDICT[x.verdict] || words(x.verdict)}, {label:"Where", key:"where_implemented"},
             {label:"Tested", get:x => words(x.tested)}, {label:"Point", key:"point_id"}, {label:"Evidence", key:"evidence"}], d.requirements)] : null,
    d.checks.length ? [sect("Earlier issues this review looked for"),
      table([{label:"Issue", key:"issue_code"}, {label:"Title", key:"title"}, {label:"Found", get:x => lab("check", x.outcome)}, {label:"Evidence", key:"evidence"}, {label:"Now", get:x => issueTag(x.status)}],
            d.checks, x => "#/issue/" + x.issue_code)] : null,
    d.metrics.length ? [sect("Measurements"), table([{label:"Measure", get:m => m.label || words(m.name)}, {label:"Of", key:"scope"}, {label:"Value", get:m => `${m.value}${m.unit ? " " + m.unit : ""}`, num:1}, {label:"How", get:m => words(m.method)}, {label:"Note", key:"note"}], d.metrics)] : null,
    sect("Points raised"),
    filters, listBox);
  draw();
};

// ---------- the owner interview and design goals (schema 9)
function byDomain(list){
  const groups = new Map();
  for (const x of list) { const k = x.domain || ""; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(x); }
  return [...groups].sort((a, b) => (a[0] === "" ? -1 : b[0] === "" ? 1 : domainName(a[0]).localeCompare(domainName(b[0]))));
}
const areaName = c => c ? domainName(c) : "Whole project";
function interviewSection(qs){
  return h("div", {class:"card interview"}, h("h2", null, "Interview"),
    h("p", {class:"note"}, "What the agent asked you about the code and your plans for it, by area, and what you said."),
    byDomain(qs).map(([dom, list]) => [h("h3", null, areaName(dom)),
      list.map(q => h("div", {class:"iq", "data-code":q.code},
        h("p", null, h("b", null, q.question)),
        h("p", {class:"note"}, "Asked because: " + q.why_asked),
        q.answers.length ? q.answers.map(a => h("p", {class:"answer"}, a.answer, h("span", {class:"note"}, ` (${lab("via", a.answered_via)}, ${when(a.answered_at)})`)))
          : h("p", {class:"note"}, q.gate ? "Not answered yet: answer it under Waiting on you." : "Not answered yet.")))]));
}
function goalsTable(goals){
  return table([{label:"Goal", key:"code"}, {label:"Area", get:g => areaName(g.domain)}, {label:"Statement", key:"text"},
    {label:"Status", get:g => labTag("goal", g.status)},
    {label:"From your answers to", get:g => g.sources.map(q => q.code + ": " + q.question).join("; ")},
    {label:"Note", get:g => g.note || (g.status === "approved" && g.approved_at ? "Approved " + when(g.approved_at) : "")}], goals);
}
VIEWS.goals = async () => {
  const d = await api("goals");
  setView(h("h1", null, "Design goals"),
    h("p", {class:"lede"}, "What you want the code to be, by area, written from your interview answers. Reviewers, fix plans and every agent work to the approved ones."),
    d.goals.length ? byDomain(d.goals).map(([dom, list]) => [sect(areaName(dom)), goalsTable(list)])
      : empty("No design goals yet."),
    d.goals.some(g => g.status === "proposed") ? h("p", {class:"status"}, h("a", {href:"#/inbox"}, "Goals are waiting for your approval →")) : null);
};

VIEWS.issues = async (_, params) => {
  const o = OVERVIEW || await api("overview");
  const q = h("input", {class:"field", type:"search", placeholder:"Search titles, ids, point ids", "aria-label":"Search issues", value:params.get("q") || ""});
  const st = select("Status", [["active","Still to fix"],["","Any status"], ...Object.entries(L.issue)]);
  const sv = select("Severity", [["","Any severity"], ...Object.entries(L.sev)]);
  const dm = select("Area", [["","All areas"], ...Object.entries(o.labels.domains)]);
  st.value = params.has("status") ? params.get("status") : "active"; sv.value = params.get("severity") || ""; dm.value = params.get("domain") || "";
  const box = h("div");
  const load = async announce => {
    const p = new URLSearchParams(); if (st.value) p.set("status", st.value); if (sv.value) p.set("severity", sv.value); if (dm.value) p.set("domain_code", dm.value); if (q.value.trim()) p.set("q", q.value.trim());
    try {
      const list = await api("issues?" + p);
      if (announce) say(`${list.length} issues`);
      put(box, h("p", {class:"note"}, `${list.length} issues`), table([{label:"Issue", key:"code"}, {label:"Title", key:"title"}, {label:"Severity", get:i => sevTag(i.severity)},
        {label:"Status", get:i => issueTag(i.status)}, {label:"Area", get:i => domainName(i.domain_code)}, {label:"First seen", key:"first_seen"}, {label:"Last seen", key:"last_seen"}, {label:"Reviews", key:"times_seen", num:1}],
        list, i => "#/issue/" + i.code));
    } catch (e) { put(box, errorBox(e)); }
  };
  setView(h("h1", null, "Issues"), h("p", {class:"lede"}, "An issue is one underlying problem, followed across every review that raised it or looked for it."), filterBar([q, st, sv, dm], load, 250), box);
  load();
};

VIEWS.issue = async ([code]) => {
  const d = await api("issue/" + encodeURIComponent(code)); const i = d.issue;
  const cont = d.relations.find(x => x.dir === "out" && x.relation === "continuation-of");
  const continues = "Raised as a new issue that continues " + (cont ? cont.other : "an earlier issue");
  setView(
    h("h1", null, `${i.code} · ${i.title}`),
    h("div", null, sevTag(i.severity), issueTag(i.status), tag(domainName(i.domain_code)), i.theme_code ? tag(themeName(i.theme_code)) : null),
    h("div", {class:"card"}, kv([["First found", h("span", null, h("a", {href:"#/review/" + i.first_seen}, i.first_seen), " · " + when(i.opened_at))],
      ["Last raised", i.last_seen ? h("a", {href:"#/review/" + i.last_seen}, i.last_seen) : null], ["Last looked for", i.last_checked ? h("a", {href:"#/review/" + i.last_checked}, i.last_checked) : null],
      ["Raised in", plural(i.times_seen, "review")],
      ["Before", i.before_state], ["Now", i.current_state], ["After the fix", i.after_state], ["Agreed fix", i.recommendation], ["Resolution", i.resolution],
      ["Fixed", whenCell(i.resolved_at)], ["Fix confirmed", whenCell(i.verified_at)]])),
    d.relations.length ? [sect("Related issues"), table([{label:"Link", get:x => x.dir === "out" ? lab("rel", x.relation) : lab("relIn", x.relation)},
      {label:"Issue", key:"other", link:true}, {label:"Title", key:"other_title"}, {label:"Status", get:x => issueTag(x.other_status)}, {label:"How sure", get:x => words(x.confidence)}, {label:"Why", key:"rationale"}, {label:"Linked in", key:"review"}],
      d.relations, x => "#/issue/" + x.other)] : null,
    d.decisions.length ? [sect("Your decisions about it"), table([{label:"Decision", key:"code"}, {label:"Question", key:"question"}, {label:"Answer", key:"answer"}, {label:"Why", key:"rationale"}, {label:"When", get:x => whenCell(x.decided_at)}], d.decisions)] : null,
    d.steps.length ? [sect("Fix plan steps"), table([{label:"Plan", key:"plan"}, {label:"Step", key:"number", num:1}, {label:"Title", key:"title"}, {label:"Status", get:x => lab("step", x.status)},
      {label:"Commits", get:x => h("span", {class:"mono"}, x.commits || "")}, {label:"Note", key:"progress_note"}], d.steps, x => `#/plan/${x.plan}`)] : null,
    d.guard_tests.length ? [sect("Tests that guard the fix"), table([{label:"Test", get:t => h("code", null, `${t.test_path} › ${t.test_name}`)},
      {label:"Failed before the fix", get:t => t.failed_before_fix == null ? "Not recorded" : t.failed_before_fix ? "Yes" : "No"}, {label:"Still in the code", get:t => t.still_exists ? "Yes" : "Removed"},
      {label:"Added in", get:t => h("code", null, short(t.added_commit_sha))}], d.guard_tests)] : null,
    d.checks.length ? [sect("What each review found"), table([{label:"Review", key:"review"}, {label:"When", get:x => whenCell(x.started_at, false)}, {label:"Found", get:x => lab("check", x.outcome)}, {label:"Evidence", key:"evidence"}], d.checks, x => "#/review/" + x.review)] : null,
    sect("Every time it was raised, with the code at that commit"),
    d.findings.map(f => findingItem(f, {showReview:true, hideIssue:true})),
    sect("History"),
    d.timeline.length ? h("div", {class:"timeline"}, d.timeline.map(e => h("div", null, h("b", null, whenCell(e.at)), " — ",
      e.event === "status-changed" ? `${lab("issue", e.from_value)} → ${lab("issue", e.to_value)}` : e.event === "severity-changed" ? `Severity ${lab("sev", e.from_value)} → ${lab("sev", e.to_value)}`
      : e.event === "seen" ? (e.to_value === "new" ? "Raised as a new issue" : e.to_value === "continuation" ? continues : `Raised again (${lab("match", e.to_value)})`) : e.event === "opened" ? "Opened" : words(e.event),
      e.review ? ` · ${e.review}` : "", e.step ? ` · step ${e.step}` : "", e.commit_sha ? [" · ", h("code", null, short(e.commit_sha))] : "", e.actor ? ` · ${e.actor}` : "",
      e.note ? h("div", {class:"note"}, e.note) : null))) : empty("No history recorded."));
};

VIEWS.plans = async () => {
  const list = await api("plans");
  setView(h("h1", null, "Fix plans"),
    table([{label:"Plan", key:"code"}, {label:"Title", key:"title"}, {label:"From reviews", key:"reviews"}, {label:"Status", get:p => lab("plan", p.status)},
           {label:"Progress", get:p => progressBar(p.done || 0, p.doing || 0, p.waiting || 0, p.steps || 0)}], list, p => "#/plan/" + p.code));
};

VIEWS.plan = async ([code, , phaseArg]) => {
  const d = await api("plan/" + encodeURIComponent(code));
  const steps = d.steps, phases = d.phase_rows, prog = Object.fromEntries(d.phases.map(p => [p.phase, p]));
  let phase = Number(phaseArg) || (phases[0] && phases[0].number) || 1;
  const count = s => steps.filter(x => x.status === s).length;
  const q = h("input", {class:"field", type:"search", placeholder:"Search steps, or a point id", "aria-label":"Search steps or point ids"});
  const f = select("Filter steps", [["","All steps"],["st-done","Done"],["st-doing","In progress"],["st-waiting-owner","Waiting on you"],["st-not-started","Not started"],
    ["any","Needs you (any)"],["decision","Needs your decision"],["confirm","Needs you to confirm"],["review","Needs your review"],["production","Production action"],["behavior","Changes behaviour on purpose"]]);
  const nav = h("nav", {class:"rail", "aria-label":"Phases"}), main = h("div", {class:"stack steps"});
  const match = s => {
    const t = q.value.trim().toLowerCase(), v = f.value;
    if (v.startsWith("st-") && s.status !== v.slice(3)) return false;
    if (v === "any" && s.owner_need === "none") return false;
    if (["decision","confirm","review","production"].includes(v) && s.owner_need !== v) return false;
    if (v === "behavior" && !s.behavior_change) return false;
    return !t || [s.number, s.title, s.change, s.proof, ...s.closes.map(c => c.point_id + " " + (c.issue || ""))].join(" ").toLowerCase().includes(t);
  };
  const stepItem = s => item(s.number, s.title,
    [s.status !== "not-started" ? labTag("step", s.status) : null,
     s.owner_need !== "none" ? tag(lab("need", s.owner_need), tone("need", s.owner_need) || "info") : null,
     s.behavior_change ? tag("Changes behaviour") : null],
    [ kv([["Change", s.change], ["Proof", s.proof], ["Depends on", s.depends.length ? (s.depends.length === 1 ? "Step " : "Steps ") + s.depends.join(", ") : null], ["Behaviour change", s.behavior_note],
      ["Needs from you", s.owner_question], ["Progress", s.progress_note], ["Commits", s.commits.length ? s.commits.map(c => h("div", null, h("code", null, short(c.commit_sha)), c.summary ? " " + c.summary : "", c.pr_url ? [" ", extLink(c.pr_url, "pull request")] : "")) : null]]),
      s.closes.length ? h("div", {class:"ref"}, h("span", {class:"note"}, "Closes " + plural(s.closes.length, "point")),
        s.closes.map(c => h("div", {class:"refline"}, h("code", null, `${c.review} · ${c.point_id}`), sevTag(c.severity), c.issue ? h("a", {href:"#/issue/" + c.issue}, c.issue) : null, h("span", null, c.summary)))) : null]);
  // The phase buttons are built once and marked in place, so the one just pressed keeps the keyboard focus.
  const rail = phases.map(p => { const pr = prog[p.number] || {}; return h("button", {type:"button",
    onclick:() => { phase = p.number; q.value = ""; f.value = ""; shown = PAGE_OF; setPath(`#/plan/${code}/phase/${phase}`); draw(); }},
    h("span", {class:"i"}, p.number), h("span", null, p.title), h("span", {class:"c"}, `${pr.done || 0}/${pr.steps || 0}`)); });
  add(nav, rail);
  const PAGE_OF = 300;   // matching steps drawn at a time
  let shown = PAGE_OF;
  const draw = () => {
    const filtering = q.value.trim() || f.value;
    rail.forEach((b, i) => b.setAttribute("aria-current", String(!filtering && phases[i].number === phase)));
    if (filtering) {
      const hit = steps.filter(match), count = plural(hit.length, "matching step") + " across all phases";
      const more = () => { const first = shown; shown += PAGE_OF; draw(); main.querySelectorAll("details.item > summary")[first].focus(); };
      put(main, h("h2", null, count), hit.length > shown ? h("p", {class:"note"}, `Showing the first ${shown}.`) : null,
        hit.length ? hit.slice(0, shown).map(stepItem) : empty("No step matches."),
        hit.length > shown ? h("button", {class:"btn ghost", type:"button", onclick:more}, `Show more (${hit.length - shown} left)`) : null);
      return count;
    } else {
      const p = phases.find(x => x.number === phase) || phases[0];
      const ps = steps.filter(s => s.phase === p.number);
      const sections = [...new Set(ps.map(s => s.section || ""))];
      put(main, h("h2", null, `Phase ${p.number} · ${p.title}`), h("p", {class:"lede"}, p.goal), p.why ? h("p", {class:"note"}, p.why) : null,
        sections.map(sec => [sec ? h("h3", null, sec) : null, ps.filter(s => (s.section || "") === sec).map(stepItem)]));
    }
  };
  const filters = filterBar([q, f], () => { shown = PAGE_OF; say(draw()); });
  const pl = d.plan;
  const head = [h("h1", null, `${pl.code} · ${pl.title}`),
    h("p", {class:"lede"}, lab("plan", pl.status) + (pl.approved_at ? ` · approved ${when(pl.approved_at)}` : "")),
    h("div", {class:"facts"}, fact(steps.length, "steps"), fact(phases.length, "phases"),
      fact(new Set(steps.flatMap(s => s.closes.map(c => c.review + c.point_id))).size, "points closed")),
    progressBar(count("done"), count("doing"), count("waiting-owner"), steps.length),
    pl.narrative_md ? h("div", {class:"card"}, h("h2", null, "About this plan"), (STATIC ? md(pl.narrative_md) : h("div", {class:"mdtext"}, pl.narrative_md))) : null];
  if (!phases.length) return setView(head, empty("This plan has no phases yet."), add(main, steps.map(stepItem)));
  setView(head, filters, h("div", {class:"cols"}, nav, main));
  draw();
};

// A line of values over reviews. The drawing is as wide as its box (one unit is one pixel, so the 11 px labels stay
// readable on a phone); labels that would run into each other are left out, the latest always shown. The same
// values are in a table for screen readers.
function lineChart(points, unit){
  const H = 160, P = 30;
  if (!points.length) return empty("No values.");
  const vs = points.map(p => p.value), lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1, last = points.length - 1;
  const value = p => `${+p.value.toFixed(2)}${unit ? " " + unit : ""}`;
  const ns = "http://www.w3.org/2000/svg", el = (t, a) => { const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  const svg = el("svg", {role:"img", "aria-label":"Values over reviews"});
  let width = 0;
  const draw = W => {
    width = W;
    const x = i => P + (points.length === 1 ? (W - 2*P) / 2 : i * (W - 2*P) / last), y = v => H - P - (v - lo) * (H - 2*P) / span;
    const every = Math.max(1, Math.ceil(points.length * 56 / (W - 2*P)));   // a label needs about 56 px
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.replaceChildren(el("line", {x1:P, x2:W-P, y1:H-P, y2:H-P, stroke:"var(--line)"}),
      el("polyline", {points:points.map((p,i) => `${x(i)},${y(p.value)}`).join(" "), fill:"none", stroke:"var(--accent)", "stroke-width":2}));
    points.forEach((p,i) => {
      svg.append(el("circle", {cx:x(i), cy:y(p.value), r:3.5, fill:"var(--accent)"}));
      if ((last - i) % every) return;
      const t1 = el("text", {x:x(i), y:y(p.value) - 8, "text-anchor":"middle", "font-size":11, fill:"var(--fg)"}); t1.textContent = value(p); svg.append(t1);
      const t2 = el("text", {x:x(i), y:H - 10, "text-anchor":"middle", "font-size":11, fill:"var(--muted)"}); t2.textContent = p.review; svg.append(t2);
    });
  };
  draw(520);
  const box = h("div", {class:"chart"}, svg, h("table", {class:"visually-hidden"},
    h("thead", null, h("tr", null, h("th", null, "Review"), h("th", null, "Value"))),
    h("tbody", null, points.map(p => h("tr", null, h("td", null, p.review), h("td", null, value(p)))))));
  new ResizeObserver(() => { const w = Math.max(200, Math.round(box.clientWidth)); if (box.clientWidth && w !== width) draw(w); }).observe(box);
  return box;
}

VIEWS.trends = async () => {
  const d = await api("trends");
  const groups = {};
  for (const m of d.metrics) (groups[`${m.name}|${m.scope}|${m.repository}`] ||= []).push(m);
  setView(h("h1", null, "Over time"), h("p", {class:"lede"}, "How the project has moved between reviews."),
    sect("Points per review"),
    table([{label:"Review", key:"review"}, {label:"Started", get:r => whenCell(r.started_at, false)}, {label:"Points", key:"points", num:1}, {label:"High", key:"high", num:1}, {label:"Medium", key:"medium", num:1}, {label:"Low", key:"low", num:1},
           {label:"New", key:"new_issues", num:1}, {label:"Carried over", key:"carried_over", num:1}, {label:"Came back", key:"came_back", num:1}, {label:"False", key:"false_findings", num:1}], d.reviews, r => "#/review/" + r.review),
    sect("Measurements"),
    Object.keys(groups).length ? h("div", {class:"grid2"}, Object.entries(groups).map(([k, pts]) => { const [n, sc, repo] = k.split("|");
      const better = {higher:"higher is better", lower:"lower is better"}[pts[0].better];
      return h("div", {class:"card"}, h("b", null, pts[0].label || words(n)), h("div", {class:"note"}, [[sc !== "all" ? sc : "", repo !== "all" ? repo : ""].filter(Boolean).join(" · ") || "Whole system", better].filter(Boolean).join(" · ")), lineChart(pts, pts[0].unit)); }))
      : empty("No measurements recorded yet."),
    sect("Issues that keep coming back"),
    table([{label:"Issue", key:"code"}, {label:"Title", key:"title"}, {label:"Severity", get:i => sevTag(i.severity)}, {label:"Status", get:i => issueTag(i.status)}, {label:"Reviews", key:"times_seen", num:1}, {label:"Came back after a fix", key:"regressions", num:1}, {label:"Raised in", key:"reviews"}],
          d.recurring, i => "#/issue/" + i.code),
    sect("Files that keep producing issues"),
    table([{label:"File", get:r => h("code", null, r.path)}, {label:"Issues", key:"issues", num:1}, {label:"Reviews", key:"reviews", num:1}, {label:"High points", key:"high_points", num:1}], d.hotspots,
          r => "#/issues?q=" + encodeURIComponent(r.path)));
};

const cellText = v => v && typeof v === "object" && "blob_bytes" in v ? `<${v.blob_bytes} bytes>` : String(v);
VIEWS.explore = async ([name]) => {
  const tables = await api("tables");
  const list = h("div", {class:"tlist"}), right = h("div", {class:"stack loose"});
  const grid = (cols, data) => data.length ? h("div", {class:"scroll card grid"}, h("table", null, h("thead", null, h("tr", null, cols.map(c => h("th", null, c)))),
    h("tbody", null, data.map(r => h("tr", null, r.map(v => h("td", {class: typeof v === "number" ? "num mono" : null, title: v == null ? null : cellText(v).slice(0, 2000)}, v == null ? h("span", {class:"muted"}, "—") : cellText(v)))))))) : empty("No rows.");
  const showTable = async (t, offset=0, paged=false) => {
    list.querySelectorAll("button").forEach(b => b.setAttribute("aria-current", String(b.dataset.n === t)));
    setPath("#/explore/" + t);
    try {
      const d = await api(`table/${t}?limit=100&offset=${offset}`);
      put(right, h("h2", null, t), h("p", {class:"note"}, `${d.count} rows` + (d.count > d.limit ? ` · showing ${offset + 1}–${Math.min(d.count, offset + d.limit)}` : "")),
        h("div", {class:"tools"}, offset > 0 ? h("button", {class:"btn ghost", type:"button", onclick:() => showTable(t, Math.max(0, offset - 100), true)}, "Previous") : null,
          offset + d.limit < d.count ? h("button", {class:"btn ghost", type:"button", onclick:() => showTable(t, offset + 100, true)}, "Next") : null,
          h("button", {class:"btn ghost", type:"button", onclick:() => { sql.value = `SELECT * FROM ${t} LIMIT 50`; sql.focus(); }}, "Query this")),
        grid(d.columns, d.rows));
      if (paged) right.querySelector("button").focus();   // the pressed button was redrawn: Previous, or Next on the first page
    } catch (e) { put(right, errorBox(e)); }
  };
  const sec = (title, type) => [sect(title), tables.filter(t => t.type === type).map(t => h("button", {type:"button", "data-n":t.name, onclick:() => showTable(t.name)}, h("span", null, t.name), h("span", {class:"muted"}, t.count)))];
  add(list, sec("Saved views", "view"), sec("Tables", "table"));
  const sql = h("textarea", {class:"field mono", "aria-label":"SQL query", placeholder:"SELECT code, title, status FROM issues WHERE severity = 'H'"});
  const out = h("div");
  const run = async () => {
    try { const d = await api("sql", {sql: sql.value}); put(out, h("p", {class:"note"}, `${d.rows.length} rows${d.truncated ? ` (first ${d.rows.length.toLocaleString("en")} shown)` : ""}`), grid(d.columns, d.rows)); }
    catch (e) { put(out, errorBox(e)); }
  };
  sql.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run(); });
  setView(h("h1", null, "Explore the ledger"),
    h("p", {class:"lede"}, STATIC ? "Browse every table and saved view as they were when this snapshot was taken."
      : "Browse every table and saved view, or run your own read-only query. Nothing here can change the ledger."),
    STATIC ? h("p", {class:"note"}, "The query box needs the local viewer (python3 ledger.py serve). Here each table and view shows up to 500 rows, and long values are shortened.")
    : h("div", {class:"card stack"}, h("b", null, "Query"), sql, h("div", {class:"tools"}, h("button", {class:"btn", type:"button", onclick:run}, "Run query"), h("span", {class:"note"}, "Ctrl or ⌘ + Enter runs it")), out),
    h("div", {class:"split"}, list, right));
  if (name && tables.some(t => t.name === name)) showTable(name); else add(right, empty("Choose a table or view on the left."));
};

// ---------- How it works (the shared copy)
if (STATIC) VIEWS.guide = async () => {
  const o = OVERVIEW || await api("overview");
  const plans = await api("plans"), revs = await api("reviews");
  const r = revs[0], p = plans[0], waiting = Math.max(0, o.queue - (STATIC ? hubAnsweredOpen() : 0));
  setView(
    h("h1", null, "How it works"),
    h("p", {class:"lede"}, "What this site is, how a review runs, what the words mean, and where things stand right now."),
    h("div", {class:"card"}, h("h2", null, "Where things stand"),
      kv([["Latest review", r ? h("a", {href:"#/review/" + r.code}, `${r.code} · ${lab("review", r.status)} · ${r.points} points`) : "None yet"],
          ["Fix plan", p ? h("a", {href:"#/plan/" + p.code}, `${p.code} · ${lab("plan", p.status)} · ${p.done || 0} of ${p.steps || 0} steps done`) : "None yet"],
          ["Waiting on you", waiting ? h("a", {href:"#/inbox"}, plural(waiting, "question")) : "Nothing right now"],
          ["Updated", whenCell(STATIC ? STATIC.taken : null)]])),
    md(STATIC ? STATIC.guide : ""));
};

// ---------- routing
const GUIDE_TAB = ["guide","#/guide","How it works"];
const TABS = [["home","#/","Overview"], ...(STATIC ? [GUIDE_TAB] : []),["inbox","#/inbox","Waiting on you"],["reviews","#/reviews","Reviews"],["goals","#/goals","Design goals"],["issues","#/issues","Issues"],["plans","#/plans","Fix plans"],["trends","#/trends","Over time"],["explore","#/explore","Explore"]];
function drawTabs(active){
  const n = Math.max(0, (OVERVIEW ? OVERVIEW.queue : 0) - (STATIC ? hubAnsweredOpen() : 0));
  put(document.getElementById("tabs"), ...TABS.map(([k, href, t]) => {
    const badge = k === "inbox" && n;
    return h("a", {href, "data-k": k, "aria-current": active === k ? "page" : null, "aria-label": badge ? `${t}, ${n} waiting` : null}, t, badge ? h("span", {class:"badge"}, n) : null); }));
  // Where the tabs scroll sideways, bring the current one into view (not scrollIntoView: Chrome then starts Tab from it).
  const tabs = document.getElementById("tabs"), cur = tabs.querySelector('a[aria-current="page"]');
  if (cur && tabs.scrollWidth > tabs.clientWidth) tabs.scrollLeft = cur.offsetLeft - tabs.offsetLeft - (tabs.clientWidth - cur.offsetWidth) / 2;
}
async function route(){
  takeToken();
  const raw = currentPath();
  const [pathPart, query] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const name = parts[0] || "home";
  const view = VIEWS[name] || VIEWS.home;
  const tab = {review:"reviews", issue:"issues", plan:"plans", history:"inbox"}[name] || (VIEWS[name] ? name : "home");
  try {
    if (!OVERVIEW || name === "home" || name === "inbox") { OVERVIEW = await api("overview"); LABELS = OVERVIEW.labels; setLabels(LABELS.enums);
      put(document.getElementById("brand"), OVERVIEW.project, h("small", null, STATIC ? "review ledger · updated " + when(STATIC.taken) : "review ledger")); document.title = OVERVIEW.project + " · review ledger"; }
    drawTabs(tab);
    await view(parts.slice(1), new URLSearchParams(query || ""));
  } catch (e) { drawTabs(tab); failed(e); }
}
window.addEventListener("hashchange", route);
document.getElementById("skip").addEventListener("click", e => { e.preventDefault(); focusHeading(); });
// --top: the sticky header's height, which scrolling to a focused control and the plan's phase list stay clear of.
(() => {
  const top = document.querySelector("header.top");
  const set = () => document.documentElement.style.setProperty("--top", (getComputedStyle(top).position === "sticky" ? top.offsetHeight : 0) + 8 + "px");
  new ResizeObserver(set).observe(top); set();
})();

// ---------- theme
(() => {
  const btn = document.getElementById("theme"), order = ["auto","light","dark"], dark = matchMedia("(prefers-color-scheme:dark)");
  const name = {auto:"Automatic", light:"Light", dark:"Dark"}, next = () => order[(order.indexOf(t) + 1) % 3];
  let t = "auto"; try { t = localStorage.getItem("ledger-theme") || "auto"; } catch {}
  if (!order.includes(t)) t = "auto";
  // data-theme is always the palette in use; "auto" follows the system setting, also when it changes.
  const apply = () => { document.documentElement.setAttribute("data-theme", t === "auto" ? (dark.matches ? "dark" : "light") : t);
    btn.textContent = "Theme: " + name[t]; btn.setAttribute("aria-label", `Theme: ${name[t]}. Switch to ${name[next()]}.`); };
  dark.addEventListener("change", apply);
  btn.addEventListener("click", () => { t = next(); try { localStorage.setItem("ledger-theme", t); } catch {} apply(); });
  apply();
})();
route();
hubInit();
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
-- migration: 0008-write-commands.sql
-- finding_locations.source (the reviewed text of a dirty working tree); v_next_codes for the write commands
-- Plan P-0001 step 98 (SPEC-M5, WALK-M4). The write commands of ledger.py take every code from the new view
-- v_next_codes, which migrate creates with the shipped views. finding_locations gains one column at the end: where
-- its excerpt was read. Every existing reference was read from its commit, so they all keep the default 'commit';
-- fill-refs writes 'working-tree' for a reference of a review whose snapshot recorded uncommitted changes, when the
-- file in the working tree differs from the commit (that is the text the reviewers saw).

ALTER TABLE finding_locations ADD COLUMN source TEXT NOT NULL DEFAULT 'commit' CHECK (source IN ('commit','working-tree'));

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('location_source','commit','Read from the commit',1),
 ('location_source','working-tree','Read from the reviewed working tree',2);
-- migration: 0009-interview-goals.sql
-- owner interview (interview_questions, interview_answers) and per-domain design goals the owner approves
-- Plan P-0001 step 129 (OWN-5). After the owner approves a review's domains, the manager interviews the owner;
-- every question (with what in the code prompted it) and every answer is kept, append-only. Design goals gain a
-- domain (NULL = the whole project), a status from the new lookup table goal_statuses (proposed, approved, rejected,
-- retired), who approved them and when, the review and the confirm gate that asked for the approval, and a note;
-- goal_sources links a goal to the interview questions it summarizes and step_goals a plan step to the goals it
-- serves. events takes the entity types 'goal' and 'question'. Every goal recorded before schema 9 was the owner's
-- already: it becomes an approved whole-project goal, approved by the project's owner actor (made here if the ledger
-- has none) at the time it was adopted; a retired one keeps its retirement as status 'retired'.

CREATE TABLE goal_statuses (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE,
  served   INTEGER NOT NULL CHECK (served IN (0,1))    -- given to reviewers, drafters and implementers (`goals`)
);
INSERT INTO goal_statuses(value, label, position, served) VALUES
 ('proposed','Waiting for your approval',1,0),
 ('approved','Approved',2,1),
 ('rejected','Not approved',3,0),
 ('retired','Retired',4,0);

INSERT INTO actors(kind, name)
  SELECT 'owner', 'owner' WHERE EXISTS (SELECT 1 FROM design_goals)
                            AND NOT EXISTS (SELECT 1 FROM actors WHERE kind = 'owner');

CREATE TABLE design_goals_new (
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
INSERT INTO design_goals_new(id, code, text, source, adopted_at, retired_at, superseded_by, domain_code, status,
                             approved_by_actor_id, approved_at)
  SELECT id, code, text, source, adopted_at, retired_at, superseded_by, NULL,
         CASE WHEN retired_at IS NULL THEN 'approved' ELSE 'retired' END,
         (SELECT min(id) FROM actors WHERE kind = 'owner'), adopted_at
  FROM design_goals;
DROP TABLE design_goals;
ALTER TABLE design_goals_new RENAME TO design_goals;
CREATE INDEX ix_fk_design_goals_superseded_by        ON design_goals(superseded_by);
CREATE INDEX ix_fk_design_goals_domain_code          ON design_goals(domain_code);
CREATE INDEX ix_fk_design_goals_status               ON design_goals(status);
CREATE INDEX ix_fk_design_goals_approved_by_actor_id ON design_goals(approved_by_actor_id);
CREATE INDEX ix_fk_design_goals_review_id            ON design_goals(review_id);
CREATE INDEX ix_fk_design_goals_gate_id              ON design_goals(gate_id);
CREATE INDEX ix_fk_design_goals_change_actor_id      ON design_goals(change_actor_id);

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
CREATE INDEX ix_fk_interview_questions_review_id         ON interview_questions(review_id);
CREATE INDEX ix_fk_interview_questions_domain_code       ON interview_questions(domain_code);
CREATE INDEX ix_fk_interview_questions_asked_by_actor_id ON interview_questions(asked_by_actor_id);
CREATE INDEX ix_fk_interview_questions_gate_id           ON interview_questions(gate_id);

CREATE TABLE interview_answers (
  id                   INTEGER PRIMARY KEY,
  question_id          INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  answer               TEXT NOT NULL CHECK (length(trim(answer)) > 0),   -- the owner's words, verbatim
  answered_via         TEXT NOT NULL DEFAULT 'chat' CHECK (answered_via IN ('chat','viewer')),
  answered_by_actor_id INTEGER REFERENCES actors(id),   -- who recorded it
  answered_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX ix_fk_interview_answers_question_id          ON interview_answers(question_id);
CREATE INDEX ix_fk_interview_answers_answered_by_actor_id ON interview_answers(answered_by_actor_id);

CREATE TABLE goal_sources (
  goal_id     INTEGER NOT NULL REFERENCES design_goals(id) ON DELETE RESTRICT,
  question_id INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  PRIMARY KEY (goal_id, question_id)
);
CREATE INDEX ix_fk_goal_sources_question_id ON goal_sources(question_id);

CREATE TABLE step_goals (
  step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  goal_id INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (step_id, goal_id)
);
CREATE INDEX ix_fk_step_goals_goal_id ON step_goals(goal_id);

CREATE TABLE events_new (
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
INSERT INTO events_new(id, entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, commit_sha, note, at)
  SELECT id, entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, commit_sha, note, at FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);
CREATE INDEX ix_fk_events_actor_id                   ON events(actor_id);
CREATE INDEX ix_fk_events_review_id                  ON events(review_id);
CREATE INDEX ix_fk_events_step_id                    ON events(step_id);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('entity_type','goal','Design goal',11), ('entity_type','question','Interview question',12);
-- migration: 0010-totals-views.sql
-- v_plan_totals, v_review_totals and v_pending_gates: the counts the viewer's lists show
-- Plan P-0001 (ABS-M7, DOC-X8). The viewer reads its list counts from three views instead of counting in its own
-- SQL: v_plan_totals (steps per plan by status), v_review_totals (real findings per review by severity, new and
-- come back) and v_pending_gates (open and answered gates in inbox order). migrate creates them with the shipped
-- views; no table and no row changes.

SELECT 1;
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
    con = None
    try:
        con = connect(db)
        v = ledger_version(con, db)
    except (LedgerError, sqlite3.DatabaseError) as e:
        if con is not None:
            con.close()  # never left open on the way out (FIX-4)
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
    shipped = {(t, n): s for t, n, _, s in shipped_objects()}
    stored = ledger_objects(con)
    # text SQLite stored exactly as shipped needs no normalizing (most objects; it is what makes this check slow)
    want = {k: s if stored.get(k) == s else normalize_sql(s) for k, s in shipped.items()}
    have = {k: s if shipped.get(k) == s else normalize_sql(s) for k, s in stored.items()}
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


MIN_SQLITE = (3, 31, 0)  # the oldest SQLite the tests run on
NAMED_CHECK_SQLITE = (3, 34, 0)  # from here a refused CHECK quotes the rule; older ones name only the table


def _dotted(version):
    return ".".join(str(n) for n in version[:2])


def sqlite_problem():
    """Why this Python's SQLite cannot hold a ledger (too old, or built without the JSON functions the schema's
    CHECKs use), in one line; None when it can."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        return (f"this Python's SQLite is {sqlite3.sqlite_version}; the ledger needs {_dotted(MIN_SQLITE)} or newer "
                f"(run ledger.py with a newer Python)")
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("SELECT json_valid('[]')")
        finally:
            con.close()
    except sqlite3.Error:
        return (f"this Python's SQLite ({sqlite3.sqlite_version}) has no JSON functions, which the ledger needs "
                f"(run ledger.py with a Python whose SQLite is 3.38 or newer)")
    return None


def old_sqlite_note():
    """The one line init and check print, once they have done their work, on a SQLite older than
    NAMED_CHECK_SQLITE; None on a newer one."""
    if sqlite3.sqlite_version_info >= NAMED_CHECK_SQLITE:
        return None
    return (f"note: SQLite {sqlite3.sqlite_version} is older than {_dotted(NAMED_CHECK_SQLITE)}: when the ledger "
            f"refuses a value, the message names the table but not the rule (the schema's CHECK on that table has it)")


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
    problem = sqlite_problem()
    if problem:  # before anything is written
        sys.exit(f"init: {problem}")
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
            tool = Path(__file__).resolve().read_bytes()
            write_atomic(home, tool)
        except OSError as e:
            sys.exit(f"init: could not copy ledger.py to {home} ({e}); nothing else was changed")
        if note:
            print(note)
        # the hash lets the owner compare the project's copy with the skill's (sha256sum scripts/ledger.py)
        print(f"copied ledger.py to {home} (sha256 {hashlib.sha256(tool).hexdigest()})")
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
        # If the ledger is ever committed, git must treat it as binary (no text diff or line merge); harmless
        # while it is not. The owner's own lines are kept; the tool's are added once.
        ga = db.parent / ".gitattributes"
        try:
            have = ga.read_text(encoding="utf-8", errors="replace") if ga.exists() else ""
            missing = [l for l in GITATTRIBUTES.splitlines() if l and not l.startswith("#") and l not in have.splitlines()]
            if missing:
                body = have + ("" if not have or have.endswith("\n") else "\n") + (GITATTRIBUTES if not have else
                                                                                   "".join(l + "\n" for l in missing))
                write_atomic(ga, body.encode(), 0o600)
        except OSError as e:
            sys.exit(f"init: could not write {ga} ({e})")


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


MAX_BLOB_BYTES = 20 * 1024 * 1024  # a file larger than this is not read (fill-refs, show, /api/source)
BLOB_SECONDS = 15  # how long one blob read may take before the reader is stopped


class BlobReader:
    """Files at commits of one repository folder, read through one `git cat-file --batch` process (started on the
    first read, stopped by close()), with a cache per (commit, path): fill-refs reads each file once however many
    references cite it. read() returns (blob sha, bytes) or raises RuntimeError with a one-line reason; a file over
    MAX_BLOB_BYTES is skipped unread (its bytes are discarded as they arrive, never held). has_commit() says whether
    the repository has a commit, asked once per commit. hung is True once git gave no answer within BLOB_SECONDS."""

    def __init__(self, d):
        self.d = d
        self.p = None
        self.cache = {}
        self.hung = False

    def has_commit(self, sha):
        key = (sha, None)
        if key not in self.cache:
            self.cache[key] = self._object(sha, None)
        return self.cache[key]

    def _expire(self, p):
        self.hung = True
        p.kill()

    def read(self, sha, path):
        key = (sha, path)
        if key not in self.cache:
            try:
                self.cache[key] = self._fetch(sha, path)
            except RuntimeError as e:
                self.cache[key] = e
        got = self.cache[key]
        if isinstance(got, RuntimeError):
            raise got
        return got

    def _start(self):
        if self.p is None:
            try:
                self.p = subprocess.Popen(["git", "-C", str(self.d), "cat-file", "--batch"], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                raise RuntimeError(f"git could not start: {e}")

    def _dead(self):
        err = b""
        try:
            self.p.kill()
            err = self.p.communicate(timeout=5)[1] or b""
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self.p = None
        return RuntimeError(err.decode("utf-8", "replace").strip().splitlines()[-1:][0] if err.strip()
                            else "git cat-file stopped")

    def _exact(self, n, keep=True):
        out, left = [], n
        while left:
            chunk = self.p.stdout.read(min(left, 1 << 20))
            if not chunk:
                raise self._dead()
            if keep:
                out.append(chunk)
            left -= len(chunk)
        return b"".join(out)

    def _fetch(self, sha, path):
        return self._object(sha, path)

    def _object(self, sha, path):
        """One question to git: the file at sha:path as (blob sha, bytes), or with path None whether commit sha
        exists."""
        self._start()
        guard = threading.Timer(BLOB_SECONDS, self._expire, (self.p,))
        guard.start()
        try:
            try:
                self.p.stdin.write((f"{sha}^{{commit}}\n" if path is None else f"{sha}:{path}\n").encode("utf-8"))
                self.p.stdin.flush()
            except OSError:
                raise self._dead()
            head = self.p.stdout.readline()
            if not head:
                raise self._dead()
            parts = head.decode("utf-8", "replace").rstrip("\n").rsplit(" ", 2)
            if len(parts) != 3 or not parts[2].isdigit():  # "<name> missing" / "<name> ambiguous"
                if path is None:
                    return False
                raise RuntimeError(f"{path} is not in commit {sha[:10]}")
            blob, kind, size = parts[0], parts[1], int(parts[2])
            if path is None:
                self._exact(size + 1, keep=False)  # the commit's text and the newline after it
                return True
            too_big = size > MAX_BLOB_BYTES
            data = self._exact(size, keep=kind == "blob" and not too_big)
            self._exact(1, keep=False)  # the newline after the content
            if kind != "blob":
                raise RuntimeError(f"{path} is a {kind} at {sha[:10]}, not a file")
            if too_big:
                raise RuntimeError(f"the file is {size:,} bytes, over the {MAX_BLOB_BYTES:,}-byte limit; not read")
            return blob, data
        finally:
            guard.cancel()

    def close(self):
        if self.p is not None:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                self.p.kill()
            for f in (self.p.stdout, self.p.stderr):
                try:
                    f.close()
                except OSError:
                    pass
            self.p = None


def file_at(root, repo_path, sha, path, allow_outside=False, raw=False):
    """The file's text at commit sha (raw=True: its bytes). A refused path gives the fixed error 'bad path' before
    git runs, so no message says whether some file outside the repository exists. A file over MAX_BLOB_BYTES is
    refused (RuntimeError)."""
    if not HEX.match(sha or ""):
        raise RuntimeError("commit is not a hex hash")
    if bad_location_path(path or ""):
        raise RuntimeError("bad path")
    reader = BlobReader(repo_dir(root, repo_path, allow_outside))
    try:
        data = reader.read(sha, path)[1]
    finally:
        reader.close()
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
    Never overwrites an excerpt. A failure (file unreadable or over MAX_BLOB_BYTES, lines past the end, a symbol not
    found or ambiguous, an empty file) writes nothing for that reference and is counted; returns the count.
    Idempotent: a reference is selected only when something can still be filled (a missing permalink only when its
    repository has a permalink template), only columns whose value changes are written, and 'filled N' counts the
    references that changed. Files are read through one `git cat-file --batch` per repository (BlobReader), each
    (commit, path) once; a commit the repository lacks is reported as 'commit ... not found' for each reference that
    cites it. When git gives no answer within BLOB_SECONDS the run stops: the references not yet read count as failed
    and what was read is still written. With nothing to fill it prints 'already complete'."""
    sql = """SELECT fl.id FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
             JOIN reviews r ON r.id = f.review_id JOIN repositories rp ON rp.id = fl.repository_id
             WHERE fl.commit_sha IS NOT NULL
               AND ((fl.excerpt IS NULL AND fl.excerpt_status IS NOT 'not-text') OR fl.blob_sha IS NULL
                    OR (fl.permalink IS NULL AND rp.permalink_template IS NOT NULL)
                    OR (fl.line_start IS NULL AND fl.whole_file = 0))"""
    args = ()
    if review_code:
        sql += " AND r.code = ?"; args = (review_code,)
    failed = 0
    updates = []  # every git read happens first; the writes then go in one short transaction (DB-M8)
    readers = {}  # one BlobReader per repository folder
    todo = [r[0] for r in con.execute(sql, args).fetchall()]
    if not todo:
        print("already complete: filled 0 references, 0 could not be read from git")
        return 0
    try:
        for n, lid in enumerate(todo):
            loc = location(con, lid)
            where = f"ref {lid} {loc['path']}@{loc['commit_sha'][:10]}"
            outside = bool(loc["allow_outside_root"])
            reader = None
            try:
                if not HEX.match(loc["commit_sha"] or ""):
                    raise RuntimeError("commit is not a hex hash")
                if bad_location_path(loc["path"] or ""):
                    raise RuntimeError("bad path")
                d = repo_dir(root, loc["repo_path"], outside)
                reader = readers.get(d) or readers.setdefault(d, BlobReader(d))
                if not reader.has_commit(loc["commit_sha"]):
                    raise RuntimeError(f"commit {loc['commit_sha'][:10]} not found")
                blob, data = reader.read(loc["commit_sha"], loc["path"])
            except Exception as e:
                if reader is not None and reader.hung:
                    left = len(todo) - n
                    print(f"{where}: git is not responding (no answer in {BLOB_SECONDS:g} s); stopped, {left} "
                          f"reference{'s' if left != 1 else ''} not read")
                    failed += left
                    break
                print(f"{where}: {e}"); failed += 1; continue
            seen = working_tree_bytes(root, con, loc, data)  # a dirty review: the text its reviewers saw (D-14)
            if seen is not None and loc["excerpt"] is None:
                data = seen
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
                    print(f"{where}: not a text file; symbol {loc['symbol']!r} cannot be looked up"); failed += 1
                    continue
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
                upd["blob_sha"] = blob
            if loc["permalink"] is None and loc["permalink_template"]:
                link = permalink_for(loc["permalink_template"], loc["commit_sha"], loc["path"],
                                     None if whole else a, None if whole else b)
                if link and SAFE_URL.match(link):
                    upd["permalink"] = link
                else:
                    print(f"{where}: the permalink template does not give an http(s) link; no permalink written")
            if seen is not None and loc["excerpt"] is None and "excerpt" in upd:
                upd["source"] = "working-tree"
            upd = {k: v for k, v in upd.items() if loc.get(k) != v}  # only what changes is written and counted
            if upd:
                updates.append((lid, upd))
    finally:
        for r in readers.values():
            r.close()
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
                 ("relation_type", "relation_types"), ("gate_kind", "gate_kinds"), ("goal_status", "goal_statuses"))
ACTIVE = "(SELECT value FROM issue_statuses WHERE active = 1)"  # issue statuses still to fix


def enum_labels(con):
    """{enum: {value: label}} in display order, from the lookup tables and enum_labels (plus relation_type_inverse,
    the labels read from the issue a relation points to). Empty for a ledger older than schema 6: the page then
    falls back to its own wording."""
    out = {}
    try:
        for name, table in LOOKUP_TABLES:
            if table == "goal_statuses" and not con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_statuses'").fetchone():
                continue  # schema 9
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


BUSY = "The ledger is busy (the agent is writing to it); try again in a moment."
FAILED = "Something went wrong; see the terminal where ledger.py serve is running."


class ApiError(Exception):
    """A viewer request that is refused: the HTTP status, the sentence the page shows and any headers to send."""

    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers


def error(status, message, headers=None):
    """The one shape of a viewer API failure: ({"error": sentence}, status), with the headers third when any."""
    return ({"error": message}, status, headers) if headers else ({"error": message}, status)


def is_busy(e):
    return isinstance(e, sqlite3.OperationalError) and ("locked" in str(e) or "busy" in str(e))


def api_failure(e, what):
    """The answer to a request that raised e: a refusal's own status and sentence, 503 while another writer holds
    the ledger, else 500 with a fixed sentence. The request, the exception and its traceback go to stderr (the
    terminal `ledger.py serve` runs in), never to the page."""
    if isinstance(e, ApiError):
        return error(e.status, e.message, e.headers)
    if is_busy(e):
        return error(503, BUSY)
    import traceback
    sys.stderr.write(f"viewer: {what} failed:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
    sys.stderr.flush()
    return error(500, FAILED)


def whole_number(text, what):
    if not isinstance(text, str) or not re.fullmatch(r"-?[0-9]{1,18}", text):
        raise ApiError(400, f"{what} must be a whole number.")
    return int(text)


def api(root, db, path, qs, body):
    """One viewer API request. Each request opens its own connection and closes it when the answer is built:
    sqlite3 connections are not shared between the server's threads, and a fresh one always sees the agent's latest
    commit. That connection is read-only (mode=ro, query_only) for every route and stops after ROUTE_SECONDS; only
    the gate answer route opens a second, writable connection of its own. A missing ledger is a 503 and is never
    created."""
    if not Path(db).exists():
        return error(503, "ledger not found")
    con = connect(db, readonly=True)
    timed_out = with_deadline(con, ROUTE_SECONDS)
    try:
        return route(root, db, con, path, qs, body)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e) and timed_out():
            return error(400, TOO_LONG.format(ROUTE_SECONDS))
        raise
    finally:
        con.close()


def gate_answer_request(db, code, body, via="viewer"):
    """The owner's answer to an open gate from the viewer (POST /api/gates/<code>/answer): answer_gate() on a
    writable connection of its own, so the viewer and `ledger.py answer-gate` apply exactly the same rules. Two
    answers cannot both be reported saved: the loser, or an answer to a gate answered meanwhile (in chat, say), gets
    409. If another writer holds the ledger past ANSWER_BUSY_MS the answer is not saved and the owner is told to try
    again (503); the page keeps what they typed. Returns {"ok": True} or (error, status)."""
    try:
        w = connect(db)
    except sqlite3.OperationalError:
        return error(503, "ledger not found")
    w.isolation_level = None
    try:
        w.execute(f"PRAGMA busy_timeout = {int(ANSWER_BUSY_MS)}")
        return answer_gate(w, code, body.get("answer_value"), body.get("answer_text"), via)
    except GateRefused as e:
        return error(e.status, str(e))
    except sqlite3.OperationalError as e:
        if is_busy(e):
            return error(503, "The ledger is busy; your answer was not saved, try again.")
        raise
    finally:
        w.close()


def has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def quote_name(name):
    return '"' + str(name).replace('"', '""') + '"'


class Request:
    """What a route handler is given: the project root, the ledger's path, this request's read-only connection, the
    path's '*' segments in order (args), the query string and, for a POST, the JSON body."""

    def __init__(self, root, db, con, args, qs, body):
        self.root, self.db, self.con, self.args, self.qs, self.body = root, db, con, args, qs, body

    def arg(self, name, default=None):
        return self.qs.get(name, [default])[0]


class Route:
    """One row of ROUTES: an HTTP method, a path under /api/ ('*' is one segment of any text, handed to the handler
    in Request.args) and its handler. hub, for a GET: the paths of this route the published hub embeds for this
    ledger (hub(con) -> ["review/R-0001", ...]); the hub is read-only, so a POST has none."""

    def __init__(self, method, path, handler, hub=None):
        self.method, self.pattern, self.handler, self.hub = method, tuple(path.split("/")), handler, hub


def get_overview(rq):
    con = rq.con
    meta = {r["key"]: r["value"] for r in rows(con, "SELECT key, value FROM meta")}
    return {
        "project": meta.get("project_name", rq.root.name),
        "schema_version": meta.get("schema_version"),
        "open_by_severity": rows(con, f"SELECT severity, count(*) n FROM issues WHERE status IN {ACTIVE} GROUP BY severity"),
        "issues_by_status": rows(con, "SELECT status, count(*) n FROM issues GROUP BY status"),
        "gates_open": one(con, "SELECT count(*) n FROM v_pending_gates WHERE status = 'open'")["n"],
        "queue": one(con, "SELECT count(*) n FROM v_owner_queue")["n"],
        "reviews": rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
            t.points, t.high FROM v_review_totals t JOIN reviews r ON r.code = t.review
            WHERE t.review IN (SELECT code FROM reviews ORDER BY started_at DESC LIMIT 5)
            ORDER BY r.started_at DESC"""),
        "plans": rows(con, """SELECT plan code, title, status, steps, done, waiting_owner waiting
            FROM v_plan_totals ORDER BY created_at DESC"""),
        "recurring": one(con, "SELECT count(*) n FROM v_recurring_issues")["n"],
        "checks": [{"label": label, "view": v, "tier": tier, "n": count_view(con, v)}
                   for v, label, tier in (ledger_checks(con) or [])],
        "labels": {**{t: {r["code"]: r["name"] for r in rows(con, f"SELECT code, name FROM {t}")} for t in LABEL_TABLES},
                   "enums": enum_labels(con)},
    }


def get_gates(rq):
    """?status=pending (the default: v_pending_gates), all, or one gate status."""
    con, st = rq.con, rq.arg("status", "pending")
    where = {"pending": "g.id IN (SELECT id FROM v_pending_gates)", "all": "1"}.get(st, "g.status = ?")
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


def post_gate_answer(rq):
    return gate_answer_request(rq.db, rq.args[0], rq.body)


def get_reviews(rq):
    return rows(rq.con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
        (SELECT group_concat(rp.name || ' ' || substr(rr.commit_sha,1,10) || COALESCE(' on ' || rr.branch, ''), ' · ')
           FROM review_repositories rr JOIN repositories rp ON rp.id=rr.repository_id WHERE rr.review_id=r.id) commits,
        t.points, t.high, t.medium, t.low, t.new_issues, t.came_back
        FROM v_review_totals t JOIN reviews r ON r.code = t.review ORDER BY r.started_at DESC""")


def get_review(rq):
    con, code = rq.con, rq.args[0]
    r = one(con, """SELECT r.*, m.name manager, m.model manager_model, a.name approved_by,
                    pr.code previous_code FROM reviews r
                    LEFT JOIN actors m ON m.id=r.manager_actor_id LEFT JOIN actors a ON a.id=r.approved_by_actor_id
                    LEFT JOIN reviews pr ON pr.id=r.previous_review_id WHERE r.code=?""", (code,))
    if not r:
        return error(404, "no such review")
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
        "summary": rows(con, "SELECT * FROM v_review_summary WHERE review=?", (code,)),
        "agents": rows(con, "SELECT * FROM v_review_agents WHERE review=?", (code,)),
        "requirements": rows(con, "SELECT * FROM v_requirement_trace WHERE review=?", (code,)),
        "metrics": rows(con, """SELECT m.name, m.scope, m.value, COALESCE(m.unit, md.unit) unit, m.method, m.note,
            COALESCE(md.label, upper(substr(m.name, 1, 1)) || replace(substr(m.name, 2), '_', ' ')) label,
            COALESCE(md.better, 'none') better
            FROM metrics m LEFT JOIN metric_defs md ON md.name=m.name WHERE m.review_id=? ORDER BY m.name, m.scope""", (rid,)),
        "artifacts": rows(con, "SELECT kind, path, url FROM artifacts WHERE review_id=? ORDER BY kind", (rid,)),
        "gates": rows(con, "SELECT code, kind, title, status, answer_value, answer_text FROM gates WHERE review_id=? ORDER BY created_at", (rid,)),
        "interview": interview_list(con, code) if has_table(con, "interview_questions") else [],
        "review_goals": goals_list(con, include_all=True, review=code) if has_table(con, "goal_statuses") else [],
        "findings": findings,
    }


def get_issues(rq):
    """?status= (active: the statuses still to fix; else one status), severity=, domain_code= (exact) and q= (text in
    the code, title, sub, a finding's point id or summary, or a location's path), all ANDed."""
    where, args = ["1"], []
    for k in ("status", "severity", "domain_code"):
        v = rq.arg(k)
        if v == "active" and k == "status":
            where.append(f"i.status IN {ACTIVE}")
        elif v:
            where.append(f"i.{k} = ?"); args.append(v)
    q = rq.arg("q")
    if q:
        where.append("(i.code LIKE ? OR i.title LIKE ? OR i.sub LIKE ? OR EXISTS (SELECT 1 FROM findings f LEFT JOIN finding_locations fl ON fl.finding_id=f.id WHERE f.issue_id=i.id AND (f.point_id LIKE ? OR f.summary LIKE ? OR fl.path LIKE ?)))")
        args += [f"%{q}%"] * 6
    return rows(rq.con, f"""SELECT i.code, i.title, i.severity, i.status, i.domain_code, i.theme_code, i.times_seen,
        i.opened_at, i.updated_at, fr.code first_seen, lr.code last_seen
        FROM issues i JOIN reviews fr ON fr.id=i.first_seen_review_id LEFT JOIN reviews lr ON lr.id=i.last_seen_review_id
        WHERE {' AND '.join(where)}
        ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, i.id LIMIT 2000""", args)


def get_issue(rq):
    con, code = rq.con, rq.args[0]
    i = one(con, """SELECT i.*, fr.code first_seen, lr.code last_seen, cr.code last_checked FROM issues i
                    JOIN reviews fr ON fr.id=i.first_seen_review_id LEFT JOIN reviews lr ON lr.id=i.last_seen_review_id
                    LEFT JOIN reviews cr ON cr.id=i.last_checked_review_id WHERE i.code=?""", (code,))
    if not i:
        return error(404, "no such issue")
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
        "timeline": rows(con, "SELECT * FROM v_issue_timeline WHERE code=?", (code,)),
        "guard_tests": rows(con, "SELECT * FROM guard_tests WHERE issue_id=?", (iid,)),
        "decisions": rows(con, """SELECT d.code, d.question, d.answer, d.rationale, d.status, d.decided_at
                                  FROM decision_issues di JOIN decisions d ON d.id=di.decision_id WHERE di.issue_id=?""", (iid,)),
        "steps": rows(con, """SELECT DISTINCT p.code plan, s.number, s.title, s.status, s.progress_note,
                (SELECT group_concat(substr(sc.commit_sha,1,10), ', ') FROM step_commits sc WHERE sc.step_id=s.id) commits
                FROM step_findings sf JOIN steps s ON s.id=sf.step_id JOIN plans p ON p.id=s.plan_id
                JOIN findings f ON f.id=sf.finding_id WHERE f.issue_id=? ORDER BY p.code, s.number""", (iid,)),
    }


def get_plans(rq):
    return rows(rq.con, """SELECT plan code, title, status, created_at, approved_at, steps, done, doing,
        waiting_owner waiting, reviews FROM v_plan_totals ORDER BY created_at DESC""")


def get_plan(rq):
    con, code = rq.con, rq.args[0]
    pl = one(con, "SELECT * FROM plans WHERE code=?", (code,))
    if not pl:
        return error(404, "no such plan")
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
            "phases": rows(con, "SELECT * FROM v_plan_progress WHERE plan=?", (code,)),
            "phase_rows": rows(con, "SELECT number, title, goal, why FROM phases WHERE plan_id=? ORDER BY number", (pid,)),
            "steps": steps}


def get_goals(rq):
    """Every design goal by domain, with its status and sources (schema 9)."""
    if not has_table(rq.con, "goal_statuses"):
        return {"goals": []}
    return {"goals": goals_list(rq.con, include_all=True)}


def get_trends(rq):
    con = rq.con
    return {"metrics": rows(con, "SELECT * FROM v_metric_trends"),
            "reviews": rows(con, """SELECT review, min(started_at) started_at, sum(points) points, sum(high) high,
                sum(medium) medium, sum(low) low, sum(new_issues) new_issues, sum(carried_over) carried_over,
                sum(came_back) came_back, sum(false_findings) false_findings
                FROM v_review_summary GROUP BY review ORDER BY started_at"""),
            "recurring": rows(con, "SELECT * FROM v_recurring_issues LIMIT 50"),
            "hotspots": rows(con, "SELECT * FROM v_file_hotspots LIMIT 30")}


def tables_and_views(con):
    """The ledger's tables and views, by type then name."""
    return rows(con, """SELECT name, type FROM sqlite_master WHERE type IN ('table','view')
                        AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name""")


def get_tables(rq):
    con, out = rq.con, []
    for t in tables_and_views(con):
        cols = [c["name"] for c in rows(con, "SELECT name FROM pragma_table_info(?)", (t["name"],))]
        n = con.execute(f"SELECT count(*) FROM {quote_name(t['name'])}").fetchone()[0]
        out.append({**t, "columns": cols, "count": n})
    return out


def get_table(rq):
    """?limit= (1 to 500, default 100) and offset= rows of one table or view."""
    con, name = rq.con, rq.args[0]
    if not NAME.match(name) or not one(con, "SELECT 1 x FROM sqlite_master WHERE name=? AND type IN ('table','view')", (name,)):
        return error(404, "no such table")
    limit = max(1, min(500, whole_number(rq.arg("limit", "100"), "limit")))
    offset = max(0, whole_number(rq.arg("offset", "0"), "offset"))
    cur = con.execute(f'SELECT * FROM "{name}" LIMIT ? OFFSET ?', (limit, offset))
    return {"columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()],
            "count": con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0], "offset": offset, "limit": limit}


def post_sql(rq):
    sql = rq.body.get("sql", "")
    if not isinstance(sql, str):
        return error(400, "sql must be text.")
    return run_readonly(rq.db, sql)


def get_source(rq):
    res = source(rq.root, rq.con, whole_number(rq.args[0], "The reference id"))
    if "error" in res and "path" not in res:
        return error(404, res["error"])
    return res


def _hub(*paths):
    return lambda con: list(paths)


def _hub_each(prefix, sql):
    return lambda con: [f"{prefix}/{r[0]}" for r in con.execute(sql)]


# Every address under /api/. A path no row matches is 404; a path that matches under another method only is 405
# with Allow. The published hub embeds the answers of every GET row (hub_snapshot).
ROUTES = (
    Route("GET", "overview", get_overview, _hub("overview")),
    Route("GET", "gates", get_gates, _hub("gates?status=pending", "gates?status=all")),
    Route("POST", "gates/*/answer", post_gate_answer),
    Route("GET", "reviews", get_reviews, _hub("reviews")),
    Route("GET", "review/*", get_review, _hub_each("review", "SELECT code FROM reviews ORDER BY code")),
    Route("GET", "issues", get_issues, _hub("issues")),
    Route("GET", "issue/*", get_issue, _hub_each("issue", "SELECT code FROM issues ORDER BY code")),
    Route("GET", "plans", get_plans, _hub("plans")),
    Route("GET", "plan/*", get_plan, _hub_each("plan", "SELECT code FROM plans ORDER BY code")),
    Route("GET", "goals", get_goals, _hub("goals")),
    Route("GET", "trends", get_trends, _hub("trends")),
    Route("GET", "tables", get_tables, _hub("tables")),
    Route("GET", "table/*", get_table,
          lambda con: [f"table/{t['name']}?limit={HUB_MAX_ROWS}&offset=0" for t in tables_and_views(con)
                       if NAME.match(t["name"])]),
    Route("POST", "sql", post_sql),
    Route("GET", "source/*", get_source, _hub_each("source", "SELECT id FROM finding_locations ORDER BY id")),
)


def api_segments(path):
    """The segments after /api of a URL path, or None for a path that is not under /api."""
    parts = [x for x in path.split("/") if x]
    return parts[1:] if parts and parts[0] == "api" else None


def _matches(r, parts):
    return len(r.pattern) == len(parts) and all(a == "*" or a == b for a, b in zip(r.pattern, parts))


def allowed_methods(path):
    """The methods a URL path answers: GET for the page, its routes' methods under /api/, none for anything else."""
    if path == "/":
        return ["GET"]
    parts = api_segments(path)
    return [] if parts is None else [r.method for r in ROUTES if _matches(r, parts)]


def wrong_method(method, allowed):
    return error(405, f"This address takes {' or '.join(allowed)}, not {method}.", {"Allow": ", ".join(allowed)})


def route(root, db, con, path, qs, body):
    """Answer one API request from ROUTES: the handler's result (a dict or list: 200) or error(...). body is None
    for a GET and the JSON object for a POST."""
    method = "GET" if body is None else "POST"
    parts = api_segments(path)
    allowed = [] if parts is None else allowed_methods(path)
    if not allowed:
        return error(404, "not found")
    if method not in allowed:
        return wrong_method(method, allowed)
    r = next(r for r in ROUTES if r.method == method and _matches(r, parts))
    try:
        return r.handler(Request(root, db, con, [b for a, b in zip(r.pattern, parts) if a == "*"], qs, body))
    except ApiError as e:
        return error(e.status, e.message, e.headers)


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

        def send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
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
            if self.command != "HEAD":  # a HEAD answer is headers only
                self.wfile.write(data)

        def send_error(self, code, message=None, explain=None):
            """Errors the HTTP library raises itself (a malformed request line, an unknown method, an address or
            header that is too long) in the viewer's own shape: JSON with the security headers."""
            self.send(code, {"error": message or self.responses.get(code, ("error",))[0]})

        def refuse_method(self):
            """Any method but GET and POST: 405 naming the methods the address takes, 404 for an unknown address."""
            if not self.host_ok():
                return
            allowed = allowed_methods(urlparse(self.path).path)
            if not allowed:
                return self.send(404, {"error": "not found"})
            res = wrong_method(self.command, allowed)
            self.send(res[1], res[0], headers=res[2])

        do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = refuse_method

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
                res = api_failure(e, f"{self.command} {u.path}")
            res = res if isinstance(res, tuple) else (res, 200)
            self.send(res[1], res[0], headers=res[2] if len(res) > 2 else None)

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
            if u.path == "/":
                return self.refuse_method()
            if not u.path.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            if not self.token_ok():
                return
            allowed = allowed_methods(u.path)
            if allowed and "POST" not in allowed:  # before the body is read
                res = wrong_method("POST", allowed)
                return self.send(res[1], res[0], headers=res[2])
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


# ---------------------------------------------------------------- owner queue, query, progress, hub, answers

def print_gates(con):
    """`ledger.py gates`: the owner's whole queue (v_owner_queue), the start-of-session check. Open and answered gates
    first, in the order they were asked ('no open gates' when there are none), then steps waiting on the owner,
    open decisions, and stale steps (their issues are all closed)."""
    gates = rows(con, "SELECT * FROM gates WHERE status IN ('open','answered') ORDER BY created_at, id")
    if not gates:
        print("no open gates")
    for g in gates:
        ans = f" -> {g['answer_value'] or ''} {g['answer_text'] or ''}".rstrip() if g["status"] == "answered" else ""
        print(f"{g['code']}  [{g['status']}] {g['kind']}: {g['title']}{ans}")
    try:
        other = rows(con, "SELECT * FROM v_owner_queue WHERE kind <> 'gate' ORDER BY kind DESC, created_at, ref")
    except sqlite3.Error:
        other = []  # an older ledger (gates warned already): migrate adds the view
    for o in other:
        if o["kind"] == "step":
            print(f"step {o['ref']} [{o['need']}] {o['title']}: {o['question']}")
        else:
            print(f"decision {o['ref']} [{o['status']}] {o['title']}"
                  + (f" (revisit when: {o['question']})" if o["question"] else ""))
    try:
        stale = rows(con, "SELECT * FROM v_stale_steps ORDER BY plan, step")
    except sqlite3.Error:
        stale = []
    for s in stale:
        print(f"stale step {s['plan']} #{s['step']} [{s['status']}] {s['title']}: its issues are all closed "
              f"({s['issues']}); mark it done or dropped")


_QUERY_START = re.compile(r"(?is)^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*(select|with)\b")


def _tsv(v):
    if v is None:
        return ""
    if isinstance(v, dict) and "blob_bytes" in v:
        return f"<blob {v['blob_bytes']} bytes>"
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def query_command(db, sql, as_json=False):
    """`ledger.py query [--readonly] [--json] "SQL"`: one SELECT (or WITH ... SELECT) through run_readonly, the
    viewer's SQL box: a read-only connection whose authorizer allows reads only, with its time, row and size limits.
    Prints TSV with a header row (tabs, new lines and backslashes in values written \\t, \\n, \\\\; NULL as nothing)
    or, with --json, {"columns", "rows", "truncated"}. Anything else is refused on stderr with exit 1; the ledger is
    never changed."""
    if not _QUERY_START.match(sql or ""):
        print("query: only one SELECT (or WITH ... SELECT) statement is accepted", file=sys.stderr)
        return 1
    res = run_readonly(db, sql)
    if isinstance(res, tuple):
        print(f"query: {res[0]['error']}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(res, ensure_ascii=False, default=str))
    else:
        print("\t".join(_tsv(c) for c in res["columns"]))
        for r in res["rows"]:
            print("\t".join(_tsv(v) for v in r))
    if res["truncated"]:
        print(f"query: only the first {len(res['rows'])} rows are shown; add a LIMIT or a WHERE", file=sys.stderr)
    return 0


def _md_cell(v):
    return str("" if v is None else v).replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", " ")


def progress_md(con, plan_code):
    """The Markdown progress report of one plan (`ledger.py progress-md P-0001`): the plan's totals, one row per
    phase from v_plan_progress, then each phase's steps (number, status, title, how many review points and issues it
    closes, its progress note). The format is fixed so every agent's PROGRESS.md reads the same. None when there is
    no such plan."""
    pl = one(con, "SELECT * FROM plans WHERE code=?", (plan_code,))
    if not pl:
        return None
    phases = rows(con, "SELECT * FROM v_plan_progress WHERE plan=? ORDER BY phase", (plan_code,))
    steps = rows(con, """SELECT s.number, s.title, s.status, s.progress_note, ph.number phase,
            (SELECT count(*) FROM step_findings sf WHERE sf.step_id=s.id) points,
            (SELECT count(DISTINCT f.issue_id) FROM step_findings sf JOIN findings f ON f.id=sf.finding_id
              WHERE sf.step_id=s.id) issues
            FROM steps s JOIN phases ph ON ph.id=s.phase_id WHERE s.plan_id=? ORDER BY s.number""", (pl["id"],))
    keys = ("steps", "done", "doing", "waiting_owner", "not_started", "dropped")
    tot = {k: sum(p[k] or 0 for p in phases) for k in keys}
    out = [f"# Progress of {pl['code']}: {pl['title']}", "",
           f"Plan status: {pl['status']}. {tot['done']} of {tot['steps']} steps done, {tot['doing']} in progress, "
           f"{tot['waiting_owner']} waiting on the owner, {tot['not_started']} not started, {tot['dropped']} dropped.",
           "", f"Written by ledger.py progress-md from the ledger at {now()}.", "",
           "| Phase | Title | Steps | Done | Doing | Waiting on the owner | Not started | Dropped |",
           "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for p in phases:
        out.append(f"| {p['phase']} | {_md_cell(p['title'])} | " + " | ".join(str(p[k] or 0) for k in keys) + " |")
    out.append(f"| | **All phases** | " + " | ".join(str(tot[k]) for k in keys) + " |")
    by_phase = {}
    for s in steps:
        by_phase.setdefault(s["phase"], []).append(s)
    for p in phases:
        out += ["", f"## Phase {p['phase']}: {_md_cell(p['title'])}", ""]
        if not by_phase.get(p["phase"]):
            out.append("No steps.")
            continue
        out += ["| Step | Status | Title | Closes | Note |", "| ---: | --- | --- | --- | --- |"]
        for s in by_phase[p["phase"]]:
            closes = f"{s['points']} points, {s['issues']} issues" if s["points"] else ""
            out.append(f"| {s['number']} | {s['status']} | {_md_cell(s['title'])} | {closes} | "
                       f"{_md_cell(s['progress_note'])} |")
    return "\n".join(out) + "\n"


# The published hub: the viewer page (PAGE, the same page.html `serve` sends) made into one self-contained file.
# Every API response the page can ask for is computed with route() and embedded as JSON; the page answers from that
# data, routes on artifact-safe hash names (#issue-ISS-0019), shows a How it works guide, and, when it is published as
# a Claude artifact with a db capability, saves the owner's gate answers in that artifact's database (collection
# "answers", one document per gate code). The agent exports those answers and runs `ledger.py import-answers`.
# Nothing in the page makes an outside request; publishing it is the agent's job, outside this tool.
HUB_GUIDE_MD = """## What this site is

This is the review ledger for this project, shown as a website. Every review, every point it raised, the issues those points describe, the fix plan, and every question waiting on you live here. Claude updates the site after each step of a review and after each batch of fixes; the date at the top says when.

You can answer every question here, under **Waiting on you**. Your answer is saved with this page; Claude records it the next time it checks, or right away if you tell it in the chat.

## How a review runs

1. **Goals and areas.** Claude reads the project, proposes the design goals the code is judged against and the areas to review (security, tests, the viewer, and so on), and asks you to approve them. Nothing runs before you do.
2. **Reviewers.** One reviewer per approved area reads its part of the code, read-only, and writes a report.
3. **Checking.** Claude checks every serious point against the code itself and records which were confirmed and which were wrong.
4. **Points into issues.** Every point of every report is recorded here, pinned to the exact lines at the reviewed commit, and grouped into issues: one issue per underlying problem, however many reviewers described it. Issues found by earlier reviews are matched, so a problem that comes back is marked as having come back.
5. **Report.** The review's report (on the review's page) gives the verdict, the root causes, what to fix first, and how each of your requirements is met.
6. **Fix plan.** Every point is placed in a step; steps are grouped into phases that build on each other. You approve the plan and any choices it needs.
7. **Fixing.** Claude works through the steps in order. Each fix comes with a test that fails before it and passes after; each step's note says in plain words what changed.
8. **Second review.** The skill runs again on the fixed code and confirms each fix; only then is an issue marked fixed and confirmed.

## Words used here

| Word | Meaning |
| --- | --- |
| Point | One thing one reviewer raised in one review |
| Issue | The underlying problem, followed across reviews; several points can describe one issue |
| Severity | High, Medium, Low, or Missing test |
| Question (gate) | Something Claude cannot get past without you; it waits until you answer |
| Decision | Your answer, kept so later reviews don't ask again |
| Plan, phase, step | The fix plan; its stages, in order; one change with its proof |
| Commit | The exact version of the code a review looked at |

## Where to look

**Overview** shows the totals and the ledger's own checks. **Reviews** lists every review; open one for its report, the areas reviewed and skipped, the reviewers, and every point with the code it cites. **Issues** lets you search and filter every issue and see its full history. **Fix plans** shows every step by phase, with what it changes, how it is proven, and which points it closes. **Over time** shows how the numbers move between reviews. **Explore** shows the raw tables behind everything.
"""

# The one change progress-page makes to page.html: the data script (%s is the JSON), put in front of the page's own
# script. Everything the hub does with the data is in the page (STATIC). hub_page refuses to build when the first text
# is not in the page exactly once.
HUB_PATCHES = [
    ('<script>\n"use strict";', '<script>window.LEDGER_SNAPSHOT = %s;</script>\n<script>\n"use strict";'),
]
HUB_MAX_ROWS = 500   # rows of each table and view embedded (Explore)
HUB_MAX_CELL = 400   # characters of one embedded table cell
HUB_MAX_LINES = 400  # lines of one embedded code excerpt


def hub_snapshot(root, db, con, start=None):
    """Every viewer API response the page can ask for, as one dict keyed by path: each GET row of ROUTES, over the
    paths its hub() lists for this ledger, answered by route() on a read-only connection. Three are reshaped: a
    table keeps HUB_MAX_ROWS rows of cut cells under table/<name>; a code reference is the excerpt saved in the
    ledger (the hub cannot read git); each issue of the list gains the text its search box matches."""
    def call(path):
        p, _, q = path.partition("?")
        res = route(root, db, con, "/api/" + p, parse_qs(q), None)
        if isinstance(res, tuple):
            raise LedgerError(f"progress-page: /api/{path} failed: {res[0].get('error')}")
        return res

    def table(path):
        d = call(path)
        cut = [[(v[:HUB_MAX_CELL] + "…") if isinstance(v, str) and len(v) > HUB_MAX_CELL else
                ({"blob_bytes": len(v)} if isinstance(v, bytes) else v) for v in r] for r in d["rows"]]
        return {"columns": d["columns"], "rows": cut, "count": len(cut), "total": d["count"]}

    cmds = {r[0]: r[1] for r in con.execute("SELECT location_id, git_command FROM v_code_refs")}

    def saved_excerpt(lid):
        loc = location(con, lid)
        a, b = loc["line_start"], loc["line_end"] or loc["line_start"]
        lines = loc["excerpt"].split("\n") if loc["excerpt"] is not None else []
        return {
            "path": loc["path"], "commit_sha": loc["commit_sha"], "line_start": a, "line_end": b,
            "symbol": loc["symbol"], "permalink": loc["permalink"], "repo_name": loc["repo_name"],
            "blob_sha": loc["blob_sha"], "excerpt_redacted": bool(loc.get("excerpt_redacted")),
            "source": "excerpt" if lines else "none", "git_command": cmds.get(lid),
            "git_error": (f"Showing the first {HUB_MAX_LINES} of {len(lines)} lines." if len(lines) > HUB_MAX_LINES
                          else "This copy shows the excerpt saved in the ledger."),
            "lines": [{"n": (a or 1) + k, "text": t} for k, t in enumerate(lines[:HUB_MAX_LINES])]}

    R = {}
    for r in ROUTES:
        for path in (r.hub(con) if r.hub else ()):
            if r.handler is get_source:
                R[path] = saved_excerpt(int(path.split("/")[1]))
            elif r.handler is get_table:
                R[path.split("?")[0]] = table(path)
            else:
                R[path] = call(path)
    search = {}
    for code, point, path in con.execute("""SELECT i.code, f.point_id, COALESCE(fl.path, '') FROM issues i
                                            JOIN findings f ON f.issue_id=i.id
                                            LEFT JOIN finding_locations fl ON fl.finding_id=f.id"""):
        search.setdefault(code, set()).update([point, path])
    for i in R["issues"]:
        i["search"] = " ".join([i["code"], i["title"] or "", i.get("sub") or ""] + sorted(search.get(i["code"], ())))
    try:
        active = [r[0] for r in con.execute("SELECT value FROM issue_statuses WHERE active = 1 ORDER BY position")]
    except sqlite3.Error:
        active = ["open", "needs-decision", "planned", "in-progress", "regressed"]
    return {"taken": now(), "start": start, "active": active, "guide": HUB_GUIDE_MD, "responses": R}


def hub_page(root, db, start=None, page=None):
    """The hub: the viewer page with the snapshot embedded (see HUB_PATCHES). start is the route it opens on when the
    address has no #route (e.g. 'plan/P-0001'). Returns (html, number of responses)."""
    page = PAGE if page is None else page
    (marker, script), = HUB_PATCHES
    if page.count(marker) != 1:
        raise LedgerError("progress-page: page.html changed; its main <script> was not found")
    con = connect(db, readonly=True)
    try:
        snap = hub_snapshot(root, db, con, start)
    finally:
        con.close()
    # In JSON, '<' only appears inside strings, where \u003c means the same: no '</script>' or '<!--' can end or
    # bend the data script.
    data = json.dumps(snap, ensure_ascii=False, default=str, separators=(",", ":")).replace("<", "\\u003c")
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = page.replace(marker, script % data, 1)
    name = snap["responses"]["overview"].get("project") or root.name
    esc = str(name).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    page = page.replace("<title>Review ledger</title>", f"<title>Review ledger: {esc}</title>", 1)
    return page, len(snap["responses"])


GATE_CODE = re.compile(r"GATE-[0-9]{4,}")


def _answer_items(doc):
    """The answers in an exported answers file, as [(gate code, {answer_value, answer_text, answered_at})].
    Accepted shapes: a list of answers, {"answers": [...]}, {gate code: answer}, and database documents
    ({"id": code, "data": {...}}); an answer names its gate as gate, code or the document id."""
    if isinstance(doc, dict) and isinstance(doc.get("answers"), (list, dict)):
        doc = doc["answers"]
    if isinstance(doc, dict):
        doc = [dict(v, gate=v.get("gate") or k) if isinstance(v, dict) else {"gate": k, "bad": True}
               for k, v in doc.items()]
    if not isinstance(doc, list):
        raise ValueError("the file holds no list of answers")
    out = []
    for item in doc:
        if not isinstance(item, dict):
            out.append((None, None)); continue
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        code = data.get("gate") or data.get("code") or item.get("id") or ""
        code = str(code).rsplit("/", 1)[-1]
        out.append((code, data))
    return out


def import_answers(db, source):
    """`ledger.py import-answers FILE`: record the owner's answers given in the published hub (exported from its
    artifact database, collection answers/<gate code>) into the ledger. Each goes through answer_gate(), the one set
    of answer rules the viewer and answer-gate use, so it is checked against the gate's options and recorded
    answered_via='viewer' by the owner. A gate that is no longer open is left as it is and said so. The time the
    owner answered in the hub is not kept: the gate records when it was imported. Returns 1 when any answer was
    refused, else 0."""
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        items = _answer_items(json.loads(raw))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"import-answers: cannot read {source}: {e}", file=sys.stderr)
        return 1
    recorded = skipped = refused = 0
    con = connect(db)
    try:
        for code, data in items:
            if not code or not GATE_CODE.fullmatch(code) or data is None or data.get("bad"):
                print(f"{code or '?'}: not an answer to a gate (skipped)"); refused += 1; continue
            try:
                answer_gate(con, code, data.get("answer_value"), data.get("answer_text"), via="viewer")
            except GateRefused as e:
                if e.status == 409:
                    print(f"{code}: already answered or closed; left as it is"); skipped += 1
                else:
                    print(f"{code}: not recorded: {e}"); refused += 1
                continue
            print(f"{code}: recorded")
            recorded += 1
    finally:
        con.close()
    print(f"import-answers: {recorded} recorded, {skipped} already answered, {refused} refused")
    return 1 if refused else 0


# ---------------------------------------------------------------- restore and merge

GITATTRIBUTES = ("# ledger.sqlite is a SQLite file: git must not diff or line-merge it. After a merge conflict keep one\n"
                 "# side and bring the other in with: python3 .code-review/ledger.py merge OTHER.sqlite\n"
                 "ledger.sqlite binary -diff -merge\nledger.sql -merge\n")


def _restore_authorizer(action, arg1, *rest):
    """A dump may only build tables and rows: no ATTACH (writing another file) and only the foreign_keys pragma."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() != "foreign_keys":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def restore(source, target):
    """`ledger.py restore FILE.sql --to NEW.sqlite`: rebuild a ledger from a dump into a new file (never over an
    existing one), then run check on it. The dump runs with ATTACH and pragmas other than foreign_keys refused.
    Returns check's failure count (0 = restored and clean); exits on a refused target or an unreadable dump."""
    target = Path(target).resolve()
    for p in (target, target.with_name(target.name + "-wal"), target.with_name(target.name + "-journal")):
        if p.exists():
            sys.exit(f"restore: {p} exists; restore writes a new file only. Give another name with --to NEW.sqlite")
    try:
        sql = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"restore: cannot read {source}: {e}")
    if not sql.startswith((DUMP_HEADER, "BEGIN TRANSACTION;")):
        sys.exit(f"restore: {source} is not a ledger.py dump (its first line is not the dump header)")
    tmp = target.with_name(target.name + ".restore-tmp")
    for p in (tmp, tmp.with_name(tmp.name + "-journal")):
        if p.exists():
            p.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    con = connect(tmp, create=True)
    try:
        con.execute("PRAGMA foreign_keys = OFF")  # the dump makes tables in name order, children before parents
        con.set_authorizer(_restore_authorizer)
        con.executescript(sql)
        con.set_authorizer(None)
        con.commit()
    except sqlite3.Error as e:
        con.close()
        tmp.unlink()
        sys.exit(f"restore: the dump did not load ({e}); nothing was written")
    con.close()
    os.replace(str(tmp), str(target))
    make_private(target)
    set_wal(target)
    print(f"restored {source} into {target}")
    con = connect(target)
    try:
        bad = check(con, quiet=True)
    finally:
        con.close()
    print(f"check: {'clean' if not bad else f'{bad} failed checks (listed above)'}")
    return bad


# events.entity_id names a row of these tables (by its rowid), by events.entity_type
EVENT_ENTITY_TABLES = {"review": "reviews", "issue": "issues", "step": "steps", "plan": "plans",
                       "decision": "decisions", "finding": "findings", "gate": "gates", "relation": "issue_relations",
                       "check": "review_issue_checks", "step-finding": "step_findings", "goal": "design_goals",
                       "question": "interview_questions"}
_CREATED_COLUMNS = ("created_at", "started_at", "opened_at", "asked_at")  # when a row with a code was first made
_KEEP_OURS_TABLES = ("meta", "schema_migrations")  # this ledger's own identity: never taken from the other one


class _MergeTable:
    def __init__(self, con, name):
        self.name = name
        info = con.execute(f"PRAGMA table_info({quote_name(name)})").fetchall()
        self.cols = [r[1] for r in info]
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        self.id_col = "id" if pk == ["id"] else None
        self.fks = {}  # column -> parent table (single-column foreign keys)
        for f in con.execute(f"PRAGMA foreign_key_list({quote_name(name)})").fetchall():
            self.fks[f[3]] = f[2]
        self.self_fks = [c for c, t in self.fks.items() if t == name]
        keys = [] if not pk or pk == ["id"] else [pk]
        for il in con.execute(f"PRAGMA index_list({quote_name(name)})").fetchall():
            if il[2] and not il[4]:  # unique, not partial
                k = [r[2] for r in con.execute(f"PRAGMA index_info({quote_name(il[1])})").fetchall()]
                if None not in k and k not in keys and not set(k) & set(self.self_fks):
                    keys.append(k)
        self.keys = keys
        try:
            con.execute(f"SELECT rowid FROM {quote_name(name)} LIMIT 0")
            self.rowid = True
        except sqlite3.Error:
            self.rowid = False
        self.created = next((c for c in _CREATED_COLUMNS if c in self.cols), None)
        self.content = [c for c in self.cols if c != self.id_col and c not in self.self_fks]


def _merge_order(con, tables):
    """Tables with each parent before its children (self references aside); events last (its entity_id needs the
    other tables' id maps)."""
    deps = {t: {p for p in tables[t].fks.values() if p != t and p in tables} for t in tables}
    order, seen = [], set()

    def visit(t, stack=()):
        if t in seen or t in stack:
            return
        for p in sorted(deps[t]):
            visit(p, stack + (t,))
        seen.add(t)
        order.append(t)
    for t in sorted(tables):
        if t != "events":
            visit(t)
    if "events" in tables:
        order.append("events")
    return order


def _next_code(con, table, code, taken):
    prefix, _, digits = code.rpartition("-")
    width = max(4, len(digits))
    nums = [int(r[0].rpartition("-")[2]) for r in con.execute(
        f"SELECT code FROM {quote_name(table)} WHERE code LIKE ?", (prefix + "-%",)) if r[0].rpartition("-")[2].isdigit()]
    nums += [int(c.rpartition("-")[2]) for c in taken.get(table, ()) if c.startswith(prefix + "-")]
    new = f"{prefix}-{(max(nums) if nums else 0) + 1:0{width}d}"
    taken.setdefault(table, set()).add(new)
    return new


def merge_ledgers(db, other):
    """`ledger.py merge OTHER.sqlite`: bring the rows of another copy of this ledger (the other side of a git merge
    conflict) into this one. Rows are matched by their natural keys (codes, unique columns; rows with no such key by
    their whole content), so rows both copies share are kept once and events are not duplicated. A code both sides
    made independently (same code, different creation time) is renumbered on the other side's row, and the mapping
    is printed. A row both sides changed keeps the newer version by updated_at, else this ledger's. Ids are
    remapped; rows go in with triggers and views dropped (so no history is written twice), and both are recreated
    from the shipped schema afterwards. The result is built in a temporary copy and checked; only a clean result is
    written into this ledger, after a backup (ledger.sqlite.bak-merge[.N]). Returns check's failure count."""
    db, other = Path(db).resolve(), Path(other).resolve()
    if db == other:
        sys.exit("merge: OTHER is this ledger")
    try:
        oc = connect(other, readonly=True)
    except sqlite3.Error as e:
        sys.exit(f"merge: cannot open {other}: {e}")
    try:
        ov = ledger_version(oc, other)
    except LedgerError as e:
        sys.exit(f"merge: {e}")
    if ov != SCHEMA_VERSION:
        sys.exit(f"merge: {other} is schema {ov}; this ledger.py is schema {SCHEMA_VERSION} (migrate it first)")
    tmp = db.with_name(db.name + ".merge-tmp")
    for p in (tmp, tmp.with_name(tmp.name + "-journal")):
        if p.exists():
            p.unlink()
    os.close(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    src = connect(db)
    con = connect(tmp, create=True)
    try:
        src.backup(con)
    finally:
        src.close()
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA journal_mode = DELETE")
    report, problems, taken = [], [], {}
    try:
        con.execute("BEGIN")
        _drop_views_and_triggers(con)
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        theirs_names = {r[0] for r in oc.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {n: _MergeTable(con, n) for n in names if n in theirs_names}
        maps = {n: {} for n in tables}  # table -> {their rowid: our rowid}
        stats = {}
        for name in _merge_order(con, tables):
            t = tables[name]
            st = stats[name] = {"same": 0, "added": 0, "renumbered": 0, "took_theirs": 0, "kept_ours": 0}
            sel = ("rowid AS __rowid__, " if t.rowid else "") + ", ".join(quote_name(c) for c in t.cols)
            order = " ORDER BY rowid" if t.rowid else ""
            theirs = [dict(r) for r in oc.execute(f"SELECT {sel} FROM {quote_name(name)}{order}")]
            if name in _KEEP_OURS_TABLES:
                continue
            ours_by_content = None
            pending = []  # (their row, mapped values) to insert after the table's map is complete
            next_id = (con.execute(f"SELECT COALESCE(max(rowid), 0) FROM {quote_name(name)}").fetchone()[0]
                       if t.rowid else 0)
            for r in theirs:
                v = {c: r[c] for c in t.cols}
                for c, parent in t.fks.items():
                    if parent != name and v.get(c) is not None and parent in maps and tables[parent].id_col:
                        if v[c] not in maps[parent]:
                            problems.append(f"{name}: a row points at {parent} {v[c]}, which the other ledger "
                                            f"does not have")
                        v[c] = maps[parent].get(v[c], v[c])
                if name == "events":
                    et = EVENT_ENTITY_TABLES.get(v["entity_type"])
                    if et in maps:
                        v["entity_id"] = maps[et].get(v["entity_id"], v["entity_id"])
                match = None
                for k in t.keys:
                    got = con.execute(f"SELECT {'rowid AS __rowid__, ' if t.rowid else ''}* FROM {quote_name(name)} "
                                      f"WHERE " + " AND ".join(f"{quote_name(c)} IS ?" for c in k),
                                      [v[c] for c in k]).fetchone()
                    if got is not None:
                        match = (k, dict(got))
                        break
                if match is None and not t.keys:
                    if ours_by_content is None:
                        ours_by_content = {}
                        for o in con.execute(f"SELECT {sel} FROM {quote_name(name)}"):
                            ours_by_content.setdefault(tuple(o[c] for c in t.content), dict(o))
                    o = ours_by_content.get(tuple(v[c] for c in t.content))
                    if o is not None:
                        match = (None, o)
                if match is not None:
                    k, o = match
                    if (k == ["code"] and t.created and o.get(t.created) != v.get(t.created)):
                        # brought in by an earlier merge under another code: the same row but for its code
                        same = [c for c in t.content if c != "code"]
                        for cand in con.execute(f"SELECT {'rowid AS __rowid__, ' if t.rowid else ''}* FROM "
                                                f"{quote_name(name)} WHERE {quote_name(t.created)} IS ?",
                                                (v.get(t.created),)):
                            if all(cand[c] == v[c] for c in same):
                                if t.rowid:
                                    maps[name][r["__rowid__"]] = cand["__rowid__"]
                                st["same"] += 1
                                break
                        else:
                            cand = None
                        if cand is not None:
                            continue
                        old = v["code"]
                        v["code"] = _next_code(con, name, old, taken)
                        report.append(f"{name}: the other ledger's {old} is now {v['code']} (both sides made "
                                      f"{old})")
                        st["renumbered"] += 1
                        match = None
                    else:
                        if t.rowid:
                            maps[name][r["__rowid__"]] = o["__rowid__"]
                        diff = [c for c in t.content if o.get(c) != v[c]]
                        if not diff:
                            st["same"] += 1
                        elif ("updated_at" in t.cols and (v.get("updated_at") or "") > (o.get("updated_at") or "")):
                            pending.append(("update", r, v, o))
                            st["took_theirs"] += 1
                        else:
                            st["kept_ours"] += 1
                        continue
                if t.rowid:
                    next_id += 1
                    maps[name][r["__rowid__"]] = next_id
                    if t.id_col:
                        v[t.id_col] = next_id
                pending.append(("insert", r, v, None))
                st["added"] += 1
            for kind, r, v, o in pending:
                for c in t.self_fks:
                    if v.get(c) is not None:
                        v[c] = maps[name].get(v[c], v[c])
                try:
                    if kind == "insert":
                        cols = list(t.cols)
                        extra = ["rowid"] if t.rowid and not t.id_col else []
                        vals = [v[c] for c in cols] + ([maps[name][r["__rowid__"]]] if extra else [])
                        con.execute(f"INSERT INTO {quote_name(name)}({', '.join(quote_name(c) for c in cols + extra)}) "
                                    f"VALUES ({', '.join('?' * len(vals))})", vals)
                    else:
                        cols = [c for c in t.content if not any(c in k for k in t.keys)]
                        con.execute(f"UPDATE {quote_name(name)} SET {', '.join(quote_name(c) + '=?' for c in cols)} "
                                    f"WHERE rowid=?", [v[c] for c in cols] + [o["__rowid__"]])
                except sqlite3.Error as e:
                    problems.append(f"{name}: a row of the other ledger could not be merged ({e})")
        _create_views_and_triggers(con)
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"{len(fk)} broken references after the merge")
        con.execute("COMMIT")
    except sqlite3.Error as e:
        problems.append(f"the merge stopped: {e}")
        if con.in_transaction:
            con.execute("ROLLBACK")
    finally:
        oc.close()
    for name, st in sorted(stats.items()):
        if st["added"] or st["renumbered"] or st["took_theirs"] or st["kept_ours"]:
            print(f"{name}: {st['added']} added, {st['same']} already here, {st['took_theirs']} updated from the "
                  f"other (newer), {st['kept_ours']} differ and kept as here" +
                  (f", {st['renumbered']} renumbered" if st["renumbered"] else ""))
    if not any(st["added"] or st["took_theirs"] or st["kept_ours"] for st in stats.values()):
        print("the other ledger holds nothing this one does not")
    for line in report:
        print(line)
    if report:
        print("note: text that mentions a renumbered code (notes, reports, review tags) still says the old one")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    bad = 0 if problems else check(con, quiet=True)
    con.close()
    if problems or bad:
        for p in problems:
            print(f"merge: {p}")
        print(f"merge: not applied; {db} is unchanged (the merged copy is {tmp})")
        return len(problems) + bad
    bak = db.with_name(db.name + ".bak-merge")
    n = 1
    while bak.exists():
        n += 1
        bak = db.with_name(f"{db.name}.bak-merge.{n}")
    live = connect(db)
    try:
        b = connect(bak, create=True)
        live.backup(b)
        b.close()
        make_private(bak)
        m = connect(tmp)
        m.backup(live)
        m.close()
    finally:
        live.close()
    tmp.unlink()
    print(f"merged {other} into {db} (the ledger before the merge is kept as {bak.name}); check: clean")
    return 0


# ---------------------------------------------------------------- the commands above, for main()

TOOL_COMMANDS = ("query", "progress-md", "progress-page", "import-answers", "restore", "merge")


def add_tool_parsers(sub):
    s = sub.add_parser("query", help="run one read-only SELECT on the ledger and print TSV (or --json)",
                       description='Run one SELECT (or WITH ... SELECT) through the viewer\'s read-only path and print '
                                   'the rows as TSV with a header line, or JSON. Writes, ATTACH and PRAGMAs are '
                                   'refused. Example: python3 .code-review/ledger.py query "SELECT code, title FROM '
                                   'issues WHERE status=\'open\'". Without ledger.py: python3 -c "import sqlite3; '
                                   'print(sqlite3.connect(\'file:.code-review/ledger.sqlite?mode=ro\', uri=True)'
                                   '.execute(\'SELECT ...\').fetchall())"')
    s.add_argument("sql")
    s.add_argument("--readonly", action="store_true", help="accepted for clarity; query is always read-only")
    s.add_argument("--json", action="store_true", help='print {"columns", "rows", "truncated"}')
    s = sub.add_parser("progress-md", help="write a plan's progress as Markdown (PROGRESS.md)",
                       description="The plan's progress in a fixed Markdown format: totals, one row per phase "
                                   "(v_plan_progress), then every step with its status, what it closes and its note. "
                                   "Printed, or written to -o FILE (e.g. -o docs/fix-plan/PROGRESS.md).")
    s.add_argument("plan"); s.add_argument("-o", "--out", help="file to write (default: print)")
    s = sub.add_parser("progress-page", help="write the hub: the viewer as one self-contained HTML page",
                       description="Write the whole viewer (every review, issue, plan, question and table) as one "
                                   "HTML file that makes no outside request: the page `serve` shows, with the data "
                                   "embedded. Published as a Claude artifact with a database, it also takes the "
                                   "owner's answers to open questions (collection answers/<gate code>); export them "
                                   "and run import-answers. Give a plan to open the page on it. Default file: "
                                   "hub.html beside the ledger.")
    s.add_argument("plan", nargs="?"); s.add_argument("-o", "--out", help="file to write (default: hub.html beside "
                                                                         "the ledger)")
    s = sub.add_parser("import-answers", help="record gate answers exported from the published hub",
                       description="Read answers given in the published hub (a JSON list of {gate, answer_value, "
                                   "answer_text, answered_at}, or the hub's database documents) and record each in "
                                   "its gate as the viewer does (answered_via 'viewer'). A gate no longer open is "
                                   "left as it is. FILE - reads standard input.")
    s.add_argument("file")
    s = sub.add_parser("restore", help="rebuild a ledger from a dump into a new file, then check it",
                       description="Rebuild a ledger from ledger.sql (written by dump) into a new file and run check "
                                   "on it. Never writes over an existing file. Example: python3 ledger.py restore "
                                   ".code-review/ledger.sql --to /tmp/restored.sqlite")
    s.add_argument("file"); s.add_argument("--to", required=True, help="the new ledger file to create")
    s = sub.add_parser("merge", help="bring another copy of this ledger (the other side of a merge) into this one",
                       description="After a git merge conflict on ledger.sqlite: keep one side as the ledger, save "
                                   "the other (git show MERGE_HEAD:.code-review/ledger.sqlite > /tmp/theirs.sqlite) "
                                   "and run merge /tmp/theirs.sqlite. Shared rows are kept once, codes both sides "
                                   "made are renumbered (the mapping is printed), and the result is checked before "
                                   "it is written; the ledger before the merge is kept as ledger.sqlite.bak-merge.")
    s.add_argument("other")


def tool_command(a, root, db):
    """Run one of TOOL_COMMANDS; returns the exit code."""
    if a.cmd == "query":
        return query_command(db, a.sql, a.json)
    if a.cmd == "restore":
        return 1 if restore(a.file, a.to) else 0
    if a.cmd == "merge":
        con = open_ledger(db, write=True, verify=True)
        con.close()
        return 1 if merge_ledgers(db, a.other) else 0
    if a.cmd == "import-answers":
        con = open_ledger(db, write=True, verify=True)
        con.close()
        return import_answers(db, a.file)
    con = open_ledger(db, verify=True)
    try:
        if a.cmd == "progress-md":
            text = progress_md(con, a.plan)
            if text is None:
                print(f"progress-md: no plan {a.plan}", file=sys.stderr)
                return 1
            if a.out:
                out = Path(a.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(out, terminal_safe(text).encode("utf-8"), 0o644)  # read with cat as often as viewed
                print(f"wrote {out}")
            else:
                sys.stdout.write(text)
            return 0
        if a.plan and not one(con, "SELECT 1 x FROM plans WHERE code=?", (a.plan,)):
            print(f"progress-page: no plan {a.plan}", file=sys.stderr)
            return 1
    finally:
        con.close()
    try:
        page, n = hub_page(root, db, start=f"plan/{a.plan}" if a.plan else None)
    except LedgerError as e:
        print(e, file=sys.stderr)
        return 1
    out = Path(a.out) if a.out else db.parent / "hub.html"
    write_atomic(out, page.encode("utf-8"))
    print(f"wrote {out} ({len(page.encode('utf-8')) / 1e6:.2f} MB, {n} views, no outside requests)")
    return 0



# ---------------------------------------------------------------- write commands (plan P-0001 steps 98-106, 127)
#
# Every command that writes runs in one BEGIN IMMEDIATE transaction on a connection with foreign keys on and a busy
# timeout (open_ledger), with change_actor_id taken from --actor. The ledger's own triggers are the rules; a command
# checks what it can first so the person gets a plain reason, and turns any refusal by a trigger or constraint into
# one line on stderr and exit 1, with nothing written. Codes come from v_next_codes (next-code), never computed by
# hand. The file formats the import commands read are in references/formats/.

class WriteError(Exception):
    """A write command refused: one line for the person running it; nothing was written."""


class GateRefused(Exception):
    """answer_gate() refused an answer: (HTTP status for the viewer, the message both the viewer and the CLI show)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


CODE_PREFIXES = ("ISS", "R", "P", "D", "GATE", "REQ", "G", "IQ")


def next_code(con, prefix):
    """The next code for a prefix (ISS, R, P, D, GATE, REQ, G or IQ), from v_next_codes: the highest number + 1 in the
    canonical format (ISS-0001, G-01). Codes are never reused, and the code triggers refuse anything else."""
    p = (prefix or "").upper().rstrip("-")
    if p not in CODE_PREFIXES:
        raise WriteError(f"unknown code prefix {prefix!r}; use one of {', '.join(CODE_PREFIXES)}")
    r = con.execute("SELECT next_code FROM v_next_codes WHERE prefix=?", (p,)).fetchone()
    return r[0]


def actor_code(actor_id):
    return f"A-{int(actor_id):04d}"


def resolve_actor(con, ref, what="--actor"):
    """An actors.id from A-0007, 7, or a name that is unique among actors; None for None."""
    if ref is None or str(ref).strip() == "":
        return None
    ref = str(ref).strip()
    m = re.fullmatch(r"(?:A-)?0*([0-9]+)", ref)
    if m:
        r = con.execute("SELECT id FROM actors WHERE id=?", (int(m.group(1)),)).fetchone()
        if not r:
            raise WriteError(f"{what}: no actor {ref}")
        return r[0]
    found = [r[0] for r in con.execute("SELECT id FROM actors WHERE name=?", (ref,))]
    if len(found) != 1:
        raise WriteError(f"{what}: {'no actor' if not found else str(len(found)) + ' actors'} named {ref!r}"
                         + ("; give its A- code" if found else ""))
    return found[0]


def owner_actor(con):
    """The project's owner as an actor (the lowest kind='owner' row), made on first use."""
    owner = con.execute("SELECT min(id) FROM actors WHERE kind='owner'").fetchone()[0]
    if owner is None:
        owner = con.execute("INSERT INTO actors(kind, name) VALUES ('owner', 'owner')").lastrowid
    return owner


def review_row(con, code):
    if not REVIEW_CODE.fullmatch(code or ""):
        raise WriteError(f"{code!r} is not a review code like R-0001")
    r = one(con, "SELECT * FROM reviews WHERE code=?", (code,))
    if not r:
        raise WriteError(f"no review {code}")
    return r


def plan_row(con, code=None):
    """The plan named by code, or the one plan that is not superseded, abandoned or complete."""
    if code:
        r = one(con, "SELECT * FROM plans WHERE code=?", (code,))
        if not r:
            raise WriteError(f"no plan {code}")
        return r
    live = rows(con, "SELECT * FROM plans WHERE status NOT IN ('superseded','abandoned','complete') ORDER BY id")
    if len(live) != 1:
        raise WriteError(f"{len(live)} plans are in progress; name one with --plan P-0001")
    return live[0]


def step_row(con, key, plan_code=None):
    """A step from its number (in --plan, or the one live plan), P-0001#12, or its drafter key."""
    key = str(key).strip()
    m = re.fullmatch(r"(P-[0-9]{4,})[#:]([0-9]+)", key)
    if m:
        plan_code, key = m.group(1), m.group(2)
    if key.isdigit():
        pl = plan_row(con, plan_code)
        s = one(con, "SELECT * FROM steps WHERE plan_id=? AND number=?", (pl["id"], int(key)))
        if not s:
            raise WriteError(f"plan {pl['code']} has no step {key}")
        return s
    args = [key]
    sql = "SELECT s.* FROM steps s JOIN plans p ON p.id = s.plan_id WHERE s.drafter_key=?"
    if plan_code:
        sql += " AND p.code=?"; args.append(plan_code)
    found = rows(con, sql, args)
    if len(found) != 1:
        raise WriteError(f"{'no step' if not found else str(len(found)) + ' steps'} with key {key!r}")
    return found[0]


def issue_row(con, code):
    r = one(con, "SELECT * FROM issues WHERE code=?", (code,))
    if not r:
        raise WriteError(f"no issue {code}")
    return r


def full_commit(d, ref):
    """The full hash of a commit that is in repository d, else WriteError (an uncommitted state has no commit)."""
    try:
        sha = git(d, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except (RuntimeError, OSError, subprocess.SubprocessError):
        sha = ""
    if not FULL_HEX.fullmatch(sha):
        raise WriteError(f"commit {ref!r} is not in the repository at {d}")
    return sha


def _one_line(e):
    return " ".join(str(e).split())


class WriteContext:
    def __init__(self, con, root, db, a):
        self.con, self.root, self.db, self.a = con, root, db, a
        self.actor_id = None
        self.files = []  # files written by the command, removed again if it fails
        self.after = []  # callables run once the transaction is committed (redact: checkpoint the WAL)

    def x(self, sql, args=()):
        return self.con.execute(sql, args)


WRITE_COMMANDS = {}  # name -> handler(ctx) returning the lines to print once committed
READ_COMMANDS = {}   # name -> handler(con, a) for commands that only read (next-code)


def run_write_command(con, root, db, a, handler=None):
    """Run the write command a.cmd (or handler, for a write behind another command's flag: tag-review --pushed) in
    one BEGIN IMMEDIATE transaction. Prints its lines after COMMIT and returns 0; on any refusal rolls back, removes
    the files it wrote, prints one line and returns 1."""
    if a.cmd in READ_COMMANDS:
        try:
            for line in READ_COMMANDS[a.cmd](con, a):
                print(line)
        except (WriteError, sqlite3.Error) as e:
            print(f"{a.cmd}: {_one_line(e)}", file=sys.stderr)
            return 1
        return 0
    handler = handler or WRITE_COMMANDS[a.cmd]
    ctx = WriteContext(con, root, db, a)
    if con.in_transaction:
        con.commit()
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        print(f"{a.cmd}: the ledger is busy ({_one_line(e)}); nothing was written, try again", file=sys.stderr)
        return 1
    ok = False
    try:
        ctx.actor_id = resolve_actor(con, getattr(a, "actor", None))
        lines = handler(ctx) or []
        con.execute("COMMIT")
        ok = True
    except WriteError as e:
        msg = _one_line(e)
    except GateRefused as e:
        msg = _one_line(e)
    except sqlite3.Error as e:
        msg = f"refused by the ledger: {_one_line(e)}"
    except RuntimeError as e:  # git
        msg = _one_line(e)
    finally:
        if not ok:
            if con.in_transaction:
                con.execute("ROLLBACK")
            for f in ctx.files:
                try:
                    Path(f).unlink()
                except OSError:
                    pass
    if not ok:
        print(f"{a.cmd}: {msg}; nothing was written", file=sys.stderr)
        return 1
    for f in ctx.after:
        try:
            f()
        except sqlite3.Error:
            pass
    for line in lines:
        print(line)
    return 0


def _command(name, help, usage, read=False):
    def wrap(f):
        (READ_COMMANDS if read else WRITE_COMMANDS)[name] = f
        f.cli = (help, usage)
        return f
    return wrap


def _text_arg(value, what):
    """Text from an argument, or from a file when it is @path (or @- for standard input)."""
    if value is None:
        return None
    if value.startswith("@"):
        src = value[1:]
        try:
            return sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise WriteError(f"{what}: cannot read {src}: {e}")
    return value


def _need(value, what):
    if value is None or not str(value).strip():
        raise WriteError(f"give {what}")
    return str(value).strip()


# ----- next-code, add-repo, add-actor

@_command("next-code", "print the next code for a prefix (ISS, R, P, D, GATE, REQ, G, IQ)",
          "ledger.py next-code ISS", read=True)
def cmd_next_code(con, a):
    return [next_code(con, a.prefix)]


@_command("add-repo", "record a repository of the project",
          "ledger.py add-repo NAME PATH [--remote URL] [--default-branch main] [--permalink-template URL] "
          "[--outside-root]")
def cmd_add_repo(ctx):
    a = ctx.a
    if not a.outside_root:
        repo_dir(ctx.root, a.path)  # refuses a path outside the project root (D-0007)
    rid = ctx.x("""INSERT INTO repositories(name, path, remote_url, default_branch, permalink_template,
                   allow_outside_root) VALUES (?, ?, ?, ?, ?, ?)""",
                (_need(a.name, "the repository name"), a.path, a.remote, a.default_branch, a.permalink_template,
                 1 if a.outside_root else 0)).lastrowid
    return [f"repository {a.name} (id {rid}) at {a.path}"]


ACTOR_KINDS = ('owner', 'human', 'manager', 'reviewer', 'verifier', 'indexer', 'matcher', 'drafter', 'fixer', 'tool')


@_command("add-actor", "record a person or agent (one row per agent instance); prints its A- code",
          "ledger.py add-actor KIND NAME [--model M] [--harness H] [--session REF] [--parent A-0001] "
          "[--review R-0001 --role 'indexer for security.md']")
def cmd_add_actor(ctx):
    a = ctx.a
    if a.kind not in ACTOR_KINDS:
        raise WriteError(f"kind {a.kind!r} is not one of {', '.join(ACTOR_KINDS)}")
    name = _need(a.name, "the actor's name")
    dup = ctx.x("""SELECT id FROM actors WHERE kind=? AND name=? AND model IS ? AND session_ref IS ?""",
                (a.kind, name, a.model, a.session)).fetchone()
    if dup:
        raise WriteError(f"{actor_code(dup[0])} is already {a.kind} {name!r}"
                         f"{' (' + a.model + ')' if a.model else ''}; reuse it, or give --session for a new instance")
    parent = resolve_actor(ctx.con, a.parent, "--parent") if a.parent else ctx.actor_id
    aid = ctx.x("INSERT INTO actors(kind, name, model, harness, session_ref, parent_actor_id) VALUES (?,?,?,?,?,?)",
                (a.kind, name, a.model, a.harness, a.session, parent)).lastrowid
    out = [actor_code(aid)]
    if a.review:
        rv = review_row(ctx.con, a.review)
        ctx.x("INSERT INTO review_actors(review_id, actor_id, role_label) VALUES (?,?,?)",
              (rv["id"], aid, _need(a.role, "--role (what it did in the review)")))
        out.append(f"{actor_code(aid)} works on {a.review} as {a.role}")
    return out


# ----- reviews: new-review, review-status, snapshot, add-run

def _exclude_spec(d, db):
    """git pathspec for the whole repository d except the ledger's folder when it is inside d."""
    try:
        rel = Path(db).resolve().parent.relative_to(Path(d).resolve())
    except ValueError:
        return ["."]
    return [".", f":(exclude){rel.as_posix()}"] if str(rel) != "." else ["."]


def snapshot_repository(root, db, review_code, repo, commit=None):
    """What review_repositories records for one repository: the full commit (HEAD unless commit is given), branch,
    commit time (UTC) and subject; for HEAD also whether the working tree differs from it, ignoring the ledger's own
    folder, and the patch of every difference (git diff HEAD, staged and unstaged, plus each untracked file not
    ignored). Returns (fields, patch bytes or None)."""
    d = repo_dir(root, repo["path"], bool(repo["allow_outside_root"]))
    try:
        git(d, "rev-parse", "--git-dir")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        raise WriteError(f"repository {repo['name']}: {d} is not a git checkout")
    head = full_commit(d, "HEAD")
    sha = full_commit(d, commit) if commit else head
    try:
        branch = git(d, "symbolic-ref", "--short", "-q", "HEAD").decode().strip() or None
    except RuntimeError:
        branch = None  # detached
    info = git(d, "show", "-s", "--format=%cI%n%s", sha).decode("utf-8", "replace").split("\n", 1)
    fields = {"commit_sha": sha, "branch": branch if sha == head else None,
              "commit_time": utc_time(info[0].strip()), "commit_subject": (info[1].strip() if len(info) > 1 else None),
              "is_dirty": 0, "dirty_note": None, "dirty_patch_path": None}
    if sha != head:
        return fields, None
    spec = _exclude_spec(d, db)
    status = git(d, "status", "--porcelain", "--untracked-files=all", "--", *spec).decode("utf-8", "replace")
    if not status.strip():
        return fields, None
    patch = git(d, "diff", "HEAD", "--binary", "--", *spec)
    untracked = [p for p in git(d, "ls-files", "--others", "--exclude-standard", "-z", "--", *spec)
                 .decode("utf-8", "replace").split("\0") if p]
    for p in untracked:
        r = subprocess.run(["git", "-C", str(d), "diff", "--no-index", "--binary", "--", "/dev/null", p],
                           capture_output=True, timeout=15)
        if r.returncode not in (0, 1):
            raise WriteError(f"repository {repo['name']}: git diff of untracked {p} failed: "
                             f"{r.stderr.decode('utf-8', 'replace').strip()}")
        patch += r.stdout
    changed = len([l for l in status.splitlines() if not l.startswith("??")])
    fields.update(is_dirty=1, dirty_note=f"{changed} changed and {len(untracked)} untracked files differ from "
                                         f"{sha[:10]}")
    return fields, patch


def _write_patch(ctx, review_code, repo, patch):
    folder = Path(ctx.db).parent / "patches"
    folder.mkdir(mode=0o700, exist_ok=True)
    make_private(folder, 0o700)
    path = folder / f"{review_code}-{re.sub(r'[^A-Za-z0-9._-]', '_', repo['name'])}.patch"
    write_atomic(path, patch, 0o600)
    ctx.files.append(path)
    try:
        return path.resolve().relative_to(Path(ctx.root).resolve()).as_posix()
    except ValueError:
        return str(path)


def record_snapshot(ctx, rv, repo, commit=None):
    """Insert or refresh the review_repositories row of one repository from git (see snapshot_repository)."""
    fields, patch = snapshot_repository(ctx.root, ctx.db, rv["code"], repo, commit)
    if patch is not None:
        fields["dirty_patch_path"] = _write_patch(ctx, rv["code"], repo, patch)
    old = one(ctx.con, "SELECT commit_sha FROM review_repositories WHERE review_id=? AND repository_id=?",
              (rv["id"], repo["id"]))
    if old and old["commit_sha"] != fields["commit_sha"] and ctx.x(
            """SELECT 1 FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
               WHERE f.review_id=? AND fl.repository_id=? LIMIT 1""", (rv["id"], repo["id"])).fetchone():
        raise WriteError(f"{rv['code']} already has findings at {old['commit_sha'][:10]} in {repo['name']}; "
                         f"its reviewed commit cannot change")
    cols = list(fields)
    ctx.x(f"""INSERT INTO review_repositories(review_id, repository_id, {', '.join(cols)})
              VALUES (?, ?, {', '.join('?' for _ in cols)})
              ON CONFLICT(review_id, repository_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}""",
          (rv["id"], repo["id"], *fields.values()))
    dirty = (f", working tree differs ({fields['dirty_note']}; patch {fields['dirty_patch_path']})"
             if fields["is_dirty"] else ", clean")
    return f"{repo['name']}: {fields['commit_sha']}" + (f" on {fields['branch']}" if fields["branch"] else "") + dirty


def _repo_args(con, specs):
    """[(repository row, commit or None)] from NAME or NAME@COMMIT arguments; every repository when none given."""
    if not specs:
        found = rows(con, "SELECT * FROM repositories ORDER BY id")
        if not found:
            raise WriteError("the ledger has no repositories; add one with: ledger.py add-repo NAME PATH")
        return [(r, None) for r in found]
    out = []
    for s in specs:
        name, _, commit = s.partition("@")
        r = one(con, "SELECT * FROM repositories WHERE name=?", (name,))
        if not r:
            raise WriteError(f"no repository named {name!r} (add it with: ledger.py add-repo {name} PATH)")
        out.append((r, commit or None))
    return out


REVIEW_SCOPES = ('full', 'partial', 'targeted', 'second', 'fix-verification')


@_command("new-review", "start a review: its code, repositories at their commits (from git), domains and goals",
          "ledger.py new-review --title T --scope full|partial|targeted|second|fix-verification "
          "[--repo NAME[@COMMIT]]... [--domain CODE[=reason]]... [--skip CODE=reason]... [--goal G-01]... "
          "[--request TEXT|@file] [--previous R-0001] [--manager A-0001] [--actor A-0001]")
def cmd_new_review(ctx):
    a, con = ctx.a, ctx.con
    if a.scope not in REVIEW_SCOPES:
        raise WriteError(f"scope {a.scope!r} is not one of {', '.join(REVIEW_SCOPES)}")
    code = next_code(con, "R")
    prev = review_row(con, a.previous)["id"] if a.previous else None
    manager = resolve_actor(con, a.manager, "--manager") if a.manager else None
    rid = ctx.x("""INSERT INTO reviews(code, title, scope, status, request_text, requested_by_actor_id,
                   manager_actor_id, previous_review_id, skill_version, change_actor_id)
                   VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?)""",
                (code, _need(a.title, "--title"), a.scope, _text_arg(a.request, "--request"), None, manager, prev,
                 TOOL_VERSION, ctx.actor_id)).lastrowid
    rv = one(con, "SELECT * FROM reviews WHERE id=?", (rid,))
    out = [code]
    for repo, commit in _repo_args(con, a.repo):
        out.append(record_snapshot(ctx, rv, repo, commit))
    for spec, decision in [(s, "included") for s in a.domain or []] + [(s, "skipped") for s in a.skip or []]:
        dom, _, reason = spec.partition("=")
        if not one(con, "SELECT 1 x FROM domains WHERE code=?", (dom,)):
            raise WriteError(f"no domain {dom!r} in the catalog")
        if decision == "skipped" and not reason.strip():
            raise WriteError(f"--skip {dom}: give the reason (--skip {dom}='why')")
        ctx.x("INSERT INTO review_domains(review_id, domain_code, decision, reason) VALUES (?,?,?,?)",
              (rid, dom, decision, reason.strip() or "proposed for this review"))
    for g in a.goal or []:
        gid = con.execute("SELECT id FROM design_goals WHERE code=?", (g,)).fetchone()
        if not gid:
            raise WriteError(f"no design goal {g}")
        ctx.x("INSERT INTO review_goals(review_id, goal_id) VALUES (?, ?)", (rid, gid[0]))
    n_in, n_out = len(a.domain or []), len(a.skip or [])
    out.append(f"{code}: planning, {n_in} domains proposed, {n_out} skipped")
    return out


REVIEW_STATUSES = ('planning', 'awaiting-approval', 'approved', 'running', 'verifying', 'indexing', 'consolidating',
                   'handed-off', 'abandoned')


@_command("review-status", "move a review to its next status (approved records who approved it and when)",
          "ledger.py review-status R-0001 approved|running|verifying|indexing|consolidating|handed-off|... "
          "--note TEXT [--approved-by A-0001] [--actor A-0001]")
def cmd_review_status(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    if a.status not in REVIEW_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(REVIEW_STATUSES)}")
    sets, args = {"status": a.status, "change_actor_id": ctx.actor_id, "change_note": a.note}, []
    if a.status not in ('planning', 'awaiting-approval', 'abandoned') and rv["approved_at"] is None:
        sets["approved_at"] = now()
        sets["approved_by_actor_id"] = (resolve_actor(con, a.approved_by, "--approved-by") if a.approved_by
                                        else owner_actor(con))
    ctx.x(f"UPDATE reviews SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), rv["id"]))
    return [f"{rv['code']}: {rv['status']} -> {a.status}"]


@_command("snapshot", "record each repository's commit, branch and uncommitted changes for a review (from git)",
          "ledger.py snapshot R-0001 [--repo NAME[@COMMIT]]...")
def cmd_snapshot(ctx):
    rv = review_row(ctx.con, ctx.a.review)
    specs = ctx.a.repo
    if not specs:
        have = [r["name"] for r in rows(ctx.con, """SELECT rp.name FROM review_repositories rr
                 JOIN repositories rp ON rp.id = rr.repository_id WHERE rr.review_id=? ORDER BY rp.id""", (rv["id"],))]
        specs = have or None
    return [record_snapshot(ctx, rv, repo, commit) for repo, commit in _repo_args(ctx.con, specs)]


RUN_STATUSES = ('queued', 'running', 'finished', 'failed', 'cancelled')


@_command("add-run", "record a reviewer run (an approved review and a domain it included)",
          "ledger.py add-run R-0001 DOMAIN LABEL --scope TEXT [--reviewer A-0003 | --model M] "
          "[--prompt TEXT|@file] [--status queued] [--actor A-0001]")
def cmd_add_run(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    if rv["status"] in ('planning', 'awaiting-approval', 'abandoned') or rv["approved_at"] is None:
        raise WriteError(f"{rv['code']} is {rv['status']}; reviewer runs need the owner's approval first "
                         f"(review-status {rv['code']} approved)")
    if not one(con, "SELECT 1 x FROM review_domains WHERE review_id=? AND domain_code=? AND decision='included'",
               (rv["id"], a.domain)):
        raise WriteError(f"{rv['code']} did not include domain {a.domain!r} (review_domains)")
    if a.status not in RUN_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(RUN_STATUSES)}")
    reviewer = resolve_actor(con, a.reviewer, "--reviewer") if a.reviewer else ctx.x(
        "INSERT INTO actors(kind, name, model, parent_actor_id) VALUES ('reviewer', ?, ?, ?)",
        (f"{a.domain} reviewer {a.label}", a.model, ctx.actor_id)).lastrowid
    run = ctx.x("""INSERT INTO reviewer_runs(review_id, domain_code, actor_id, label, scope, prompt, status, started_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (rv["id"], a.domain, reviewer, _need(a.label, "the run's label"), _need(a.scope, "--scope"),
                 _text_arg(a.prompt, "--prompt"), a.status, now() if a.status == "running" else None)).lastrowid
    return [f"run {a.label} (id {run}) of {rv['code']} in {a.domain}, reviewer {actor_code(reviewer)}"]


# ----- issue-status, step-status, import-steps

@_command("issue-status", "change an issue's status with its reason and context, in one transaction",
          "ledger.py issue-status ISS-0001 fixed --note TEXT --after 'what it does now' [--review R-0002] "
          "[--step 12 [--plan P-0001]] [--commit FULLSHA] [--resolution TEXT] [--actor A-0001]")
def cmd_issue_status(ctx):
    a, con = ctx.a, ctx.con
    iss = issue_row(con, a.issue)
    known = [r[0] for r in con.execute("SELECT value FROM issue_statuses ORDER BY position")]
    if a.status not in known:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(known)}")
    note = _need(a.note, "--note (why the status changes)")
    after = _text_arg(a.after, "--after")
    if a.status == "fixed" and not (after or iss["after_state"]):
        raise WriteError("an issue set fixed needs --after (what the system does now)")
    if a.status in ("accepted-risk", "wont-fix") and not ((a.resolution or "").strip() or
                                                          (iss["resolution"] or "").strip()):
        raise WriteError(f"an issue closed as {a.status} needs --resolution (the reason)")
    if a.commit and not FULL_HEX.fullmatch(a.commit):
        raise WriteError("--commit needs the full commit hash")
    sets = {"status": a.status, "change_note": note, "change_actor_id": ctx.actor_id,
            "change_review_id": review_row(con, a.review)["id"] if a.review else None,
            "change_step_id": step_row(con, a.step, a.plan)["id"] if a.step else None,
            "change_commit_sha": a.commit}
    if after:
        sets["after_state"] = after
    if a.resolution:
        sets["resolution"] = a.resolution
    ctx.x(f"UPDATE issues SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), iss["id"]))
    return [f"{iss['code']}: {iss['status']} -> {a.status}"]


STEP_STATUSES = ('not-started', 'doing', 'waiting-owner', 'done', 'dropped')


@_command("step-status", "change a plan step's status with its note (waiting-owner needs the owner's question); "
                         "--commit also records the step's commit, done --guard its guard tests",
          "ledger.py step-status 12|P-0001#12|KEY doing|waiting-owner|done|dropped|not-started --note TEXT "
          "[--question TEXT] [--progress-note TEXT] [--commit REF [--repo NAME]] "
          "[--guard PATH::NAME|ISS-0001=PATH::NAME]... [--failed-before] [--plan P-0001] [--actor A-0001]")
def cmd_step_status(ctx):
    a, con = ctx.a, ctx.con
    s = step_row(con, a.step, a.plan)
    if a.status not in STEP_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(STEP_STATUSES)}")
    if a.status == "waiting-owner" and not (a.question or "").strip():
        raise WriteError("a step waiting on the owner needs --question (what the owner must answer)")
    if a.status != "waiting-owner" and not (a.note or "").strip():
        raise WriteError("give --note (why the status changes)")
    guards = [_guard_spec(g) for g in getattr(a, "guard", None) or []]
    if guards and a.status != "done":
        raise WriteError("--guard goes with done (the guard tests the finished step added)")
    out = []
    sha = None
    if a.commit:  # recorded in step_commits too (add-commit)
        sha, line = record_step_commit(ctx, s, a.commit, getattr(a, "repo", None))
        out.append(line)
    sets = {"status": a.status, "change_note": (a.note or "").strip() or None, "change_actor_id": ctx.actor_id,
            "change_commit_sha": sha}
    if a.question:
        sets["owner_question"] = a.question
    progress = _text_arg(a.progress_note, "--progress-note")
    if progress or a.status == "done":
        sets["progress_note"] = progress or s["progress_note"] or a.note
    ctx.x(f"UPDATE steps SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), s["id"]))
    if guards:
        issues = rows(con, """SELECT DISTINCT i.* FROM step_findings sf JOIN findings f ON f.id = sf.finding_id
                              JOIN issues i ON i.id = f.issue_id WHERE sf.step_id=? ORDER BY i.code""", (s["id"],))
        repo = _one_repo(con, getattr(a, "repo", None))
        for code, path, name in guards:
            targets = [issue_row(con, code)] if code else issues
            if not targets:
                raise WriteError(f"step {s['number']} closes no matched issue; name it: --guard ISS-0001=PATH::NAME")
            for iss in targets:
                out.append(record_guard(ctx, iss, path, name, step=s, commit=sha,
                                        failed_before=True if a.failed_before else None, repo=repo))
    return [f"step {s['number']}: {s['status']} -> {a.status}"] + out


OWNER_NEEDS = ('none', 'decision', 'confirm', 'review', 'production')


def _read_jsonl(path):
    try:
        text = sys.stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WriteError(f"cannot read {path}: {e}")
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise WriteError(f"{path} line {n}: not JSON ({e})")
        if not isinstance(obj, dict):
            raise WriteError(f"{path} line {n}: not a JSON object")
        out.append((n, obj))
    return out


def plan_findings(con, plan_id):
    """{(review code, point id): finding id} of the plan's reviews, and {point id: [review code ...]}."""
    by, points = {}, {}
    for r in con.execute("""SELECT r.code, f.point_id, f.id FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id
                            JOIN findings f ON f.review_id = r.id WHERE pr.plan_id=?""", (plan_id,)):
        by[(r[0], r[1])] = r[2]
        points.setdefault(r[1], []).append(r[0])
    return by, points


def _finding_ref(ref, by, points):
    """The finding id of R-0001:SEC-H1 or a bare SEC-H1 that is unique among the plan's reviews; else None, why."""
    m = re.fullmatch(r"(R-[0-9]{4,})[:/](.+)", ref)
    if m:
        fid = by.get((m.group(1), m.group(2)))
        return (fid, None) if fid else (None, f"no point {ref} among the plan's reviews")
    revs = points.get(ref, [])
    if len(revs) == 1:
        return by[(revs[0], ref)], None
    if not revs:
        return None, f"no point {ref} among the plan's reviews"
    return None, f"point {ref} is in {', '.join(revs)}; write it as {revs[0]}:{ref}"


@_command("import-steps", "load drafted plan steps (JSON lines) into a plan: phases, steps, closes, depends",
          "ledger.py import-steps P-0001 steps.jsonl [--actor A-0001]   (format: references/formats/plan-steps.md)")
def cmd_import_steps(ctx):
    con = ctx.con
    pl = plan_row(con, ctx.a.plan)
    if pl["status"] in ("superseded", "abandoned", "complete"):
        raise WriteError(f"plan {pl['code']} is {pl['status']}; steps go into a live plan")
    items = _read_jsonl(ctx.a.file)
    problems = []
    phases = {r["number"]: r["id"] for r in rows(con, "SELECT id, number FROM phases WHERE plan_id=?", (pl["id"],))}
    new_phases, steps = [], []
    for n, o in items:
        if o.get("kind") == "phase":
            num = o.get("phase")
            if not isinstance(num, int) or num in phases or any(p[1]["phase"] == num for p in new_phases):
                problems.append(f"line {n}: phase {num!r} is not a new phase number")
            for k in ("title", "goal"):
                if not str(o.get(k) or "").strip():
                    problems.append(f"line {n}: phase {num} needs {k}")
            new_phases.append((n, o))
        elif o.get("kind", "step") == "step":
            steps.append((n, o))
        else:
            problems.append(f"line {n}: kind {o.get('kind')!r} is not phase or step")
    known_phases = set(phases) | {o["phase"] for _, o in new_phases if isinstance(o.get("phase"), int)}
    by, points = plan_findings(con, pl["id"])
    keys = {}
    existing_keys = {r["drafter_key"]: r for r in rows(con, "SELECT id, number, drafter_key FROM steps WHERE plan_id=? "
                                                            "AND drafter_key IS NOT NULL", (pl["id"],))}
    for n, o in steps:
        k = str(o.get("key") or "").strip()
        if not k:
            problems.append(f"line {n}: a step needs its key")
        elif k in keys or k in existing_keys:
            problems.append(f"line {n}: key {k} is used twice")
        keys[k] = (n, o)
        for f in ("title", "change", "proof"):
            if not str(o.get(f) or "").strip():
                problems.append(f"line {n} ({k}): needs {f}")
        if o.get("phase") not in known_phases:
            problems.append(f"line {n} ({k}): phase {o.get('phase')!r} is not a phase of {pl['code']}")
        if not isinstance(o.get("order", 0), int):
            problems.append(f"line {n} ({k}): order must be a whole number")
        need = o.get("owner_need") or "none"
        if need not in OWNER_NEEDS:
            problems.append(f"line {n} ({k}): owner_need {need!r} is not one of {', '.join(OWNER_NEEDS)}")
        elif need != "none" and not str(o.get("owner_question") or "").strip():
            problems.append(f"line {n} ({k}): owner_need {need} needs owner_question")
        if not isinstance(o.get("closes", []), list) or not isinstance(o.get("depends", []), list):
            problems.append(f"line {n} ({k}): closes and depends are lists")
            continue
        if not o.get("closes") and not str(o.get("no_findings_reason") or "").strip():
            problems.append(f"line {n} ({k}): closes no point; give closes or no_findings_reason")
        for ref in o.get("closes") or []:
            if _finding_ref(str(ref), by, points)[1]:
                problems.append(f"line {n} ({k}): {_finding_ref(str(ref), by, points)[1]}")
        mr = o.get("multi_reason") or {}
        if not isinstance(mr, dict):
            problems.append(f"line {n} ({k}): multi_reason is an object of point -> reason")
    if problems:
        raise WriteError(f"{len(problems)} problems, nothing imported: " + "; ".join(problems[:12])
                         + (f"; ... {len(problems) - 12} more" if len(problems) > 12 else ""))
    # numbers: after the plan's last step, in (phase, order, line) order
    start = (con.execute("SELECT max(number) FROM steps WHERE plan_id=?", (pl["id"],)).fetchone()[0] or 0) + 1
    ordered = sorted(steps, key=lambda s: (s[1]["phase"], s[1].get("order", 0), s[0]))
    number = {str(o["key"]).strip(): start + i for i, (_, o) in enumerate(ordered)}
    for n, o in ordered:
        k = str(o["key"]).strip()
        for d in o.get("depends") or []:
            dn = number.get(str(d)) or (existing_keys[str(d)]["number"] if str(d) in existing_keys else None)
            if dn is None and isinstance(d, int):
                dn = d if con.execute("SELECT 1 FROM steps WHERE plan_id=? AND number=?", (pl["id"], d)).fetchone() \
                    else None
            if dn is None:
                problems.append(f"line {n} ({k}): depends on unknown step {d!r}")
            elif dn >= number[k]:
                problems.append(f"line {n} ({k}): depends on {d} (step {dn}), which is not earlier (step {number[k]})")
    closers = {}
    for r in con.execute("""SELECT sf.finding_id, sf.multi_reason FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                            WHERE s.plan_id=? AND s.status <> 'dropped'""", (pl["id"],)):
        closers.setdefault(r[0], []).append(r[1])
    for n, o in ordered:
        for ref in o.get("closes") or []:
            closers.setdefault(_finding_ref(str(ref), by, points)[0], []).append(
                (o.get("multi_reason") or {}).get(str(ref)))
    for fid, reasons in closers.items():
        if len(reasons) > 1 and any(not r for r in reasons):
            pid = con.execute("SELECT point_id FROM findings WHERE id=?", (fid,)).fetchone()[0]
            problems.append(f"point {pid} is closed by {len(reasons)} steps; give multi_reason for it on each")
    if problems:
        raise WriteError(f"{len(problems)} problems, nothing imported: " + "; ".join(problems[:12])
                         + (f"; ... {len(problems) - 12} more" if len(problems) > 12 else ""))
    for _, o in new_phases:
        phases[o["phase"]] = ctx.x("INSERT INTO phases(plan_id, number, title, goal, why) VALUES (?,?,?,?,?)",
                                   (pl["id"], o["phase"], o["title"], o["goal"], o.get("why"))).lastrowid
    ids = {}
    for n, o in ordered:
        k = str(o["key"]).strip()
        ids[k] = ctx.x("""INSERT INTO steps(plan_id, phase_id, number, section, title, change, proof, owner_need,
                          owner_question, behavior_change, behavior_note, drafter_key, no_findings_reason,
                          change_actor_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (pl["id"], phases[o["phase"]], number[k], o.get("section"), o["title"], o["change"],
                        o["proof"], o.get("owner_need") or "none", o.get("owner_question"),
                        1 if o.get("behavior_change") else 0, o.get("behavior_note"), k,
                        o.get("no_findings_reason"), ctx.actor_id)).lastrowid
    for n, o in ordered:
        k = str(o["key"]).strip()
        for ref in o.get("closes") or []:
            ctx.x("INSERT INTO step_findings(step_id, finding_id, multi_reason) VALUES (?,?,?)",
                  (ids[k], _finding_ref(str(ref), by, points)[0], (o.get("multi_reason") or {}).get(str(ref))))
        for d in o.get("depends") or []:
            dep = ids.get(str(d)) or (existing_keys[str(d)]["id"] if str(d) in existing_keys else
                                      con.execute("SELECT id FROM steps WHERE plan_id=? AND number=?",
                                                  (pl["id"], d)).fetchone()[0])
            ctx.x("INSERT OR IGNORE INTO step_dependencies(step_id, depends_on_step_id) VALUES (?,?)", (ids[k], dep))
    out = [f"{pl['code']}: imported {len(new_phases)} phases and {len(ordered)} steps "
           f"({start}-{start + len(ordered) - 1})" if ordered else f"{pl['code']}: imported {len(new_phases)} phases"]
    uncovered = con.execute("SELECT count(*) FROM v_uncovered_findings WHERE plan=?", (pl["code"],)).fetchone()[0]
    problems = con.execute("SELECT count(*) FROM v_coverage_problems WHERE plan=?", (pl["code"],)).fetchone()[0]
    out.append(f"coverage: {uncovered} points of the plan's reviews closed by no step, {problems} coverage problems")
    return out


# ----- gates: open-gate, answer-gate (shared with the viewer), apply-gate

GATE_KINDS_HELP = ("decision|confirm|domain-approval|plan-approval|step-input|confirm-false-finding|accept-risk|"
                   "production-action|review-handoff|other")


def _gate_options(specs, raw_json):
    """Options as a list of {"value","label"[, "description"]} from --option value=label[:description] arguments
    or --options-json; values must be unique."""
    opts = []
    if raw_json:
        try:
            opts = json.loads(_text_arg(raw_json, "--options-json"))
        except ValueError as e:
            raise WriteError(f"--options-json is not JSON ({e})")
        if not isinstance(opts, list) or not all(isinstance(o, dict) for o in opts):
            raise WriteError("--options-json must be a JSON array of objects")
    for s in specs or []:
        value, sep, rest = s.partition("=")
        label, _, desc = rest.partition(":")
        if not sep or not value.strip() or not label.strip():
            raise WriteError(f"--option {s!r}: write it as value=label or value=label:description")
        opts.append({"value": value.strip(), "label": label.strip(), **({"description": desc.strip()} if desc else {})})
    for o in opts:
        if not isinstance(o.get("value"), str) or not o["value"].strip() or not isinstance(o.get("label"), str) \
                or not o["label"].strip():
            raise WriteError("every option needs a text value and label")
        if o.get("description") is not None and not isinstance(o["description"], str):
            raise WriteError(f"option {o['value']}: description must be text")
    values = [o["value"] for o in opts]
    dup = sorted({v for v in values if values.count(v) > 1})
    if dup:
        raise WriteError(f"option values must be unique: {', '.join(dup)}")
    return opts


@_command("open-gate", "ask the owner something (prints the gate's code)",
          f"ledger.py open-gate {GATE_KINDS_HELP} --title T --question Q [--context TEXT|@file] "
          "[--option value=label[:description]]... [--options-json JSON|@file] [--no-text] [--non-blocking] "
          "[--review R-0001] [--plan P-0001] [--step 12] [--issue ISS-0001] [--decision D-0001] [--actor A-0001]")
def cmd_open_gate(ctx):
    a, con = ctx.a, ctx.con
    kinds = [r[0] for r in con.execute("SELECT value FROM gate_kinds ORDER BY position")]
    if a.kind not in kinds:
        raise WriteError(f"kind {a.kind!r} is not one of {', '.join(kinds)}")
    opts = _gate_options(a.option, a.options_json)
    if a.no_text and not opts:
        raise WriteError("a gate without free text needs at least one --option")
    code = next_code(con, "GATE")
    plan = plan_row(con, a.plan) if a.plan or a.step else None
    dec = one(con, "SELECT id FROM decisions WHERE code=?", (a.decision,)) if a.decision else None
    if a.decision and not dec:
        raise WriteError(f"no decision {a.decision}")
    ctx.x("""INSERT INTO gates(code, kind, title, question, context, options, allow_text, blocking, review_id, plan_id,
             step_id, issue_id, decision_id, opened_by_actor_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (code, a.kind, _need(a.title, "--title"), _need(a.question, "--question"), _text_arg(a.context, "--context"),
           json.dumps(opts) if opts else None, 0 if a.no_text else 1, 0 if a.non_blocking else 1,
           review_row(con, a.review)["id"] if a.review else None, plan["id"] if plan else None,
           step_row(con, a.step, plan["code"])["id"] if a.step else None,
           issue_row(con, a.issue)["id"] if a.issue else None, dec["id"] if dec else None, ctx.actor_id))
    return [code]


def answer_gate(con, code, value=None, text=None, via="chat", actor_id=None):
    """Record the owner's answer to an open gate: the one set of answer rules for the viewer (via 'viewer') and
    `ledger.py answer-gate` (via 'chat' or 'other'). On the caller's open transaction, or else in a BEGIN IMMEDIATE
    of its own (committed here): the gate is read and updated in that one transaction and the UPDATE must change
    exactly one open row, so two answers cannot both be saved. actor_id is who answered (default: the project's
    owner actor, made on first use). Raises GateRefused(status, message) for 404 no such gate, 409 answered
    already, 400 an answer the gate does not take; returns {"ok": True}."""
    if not isinstance(con, sqlite3.Connection):  # answer_gate(db, code, body): the viewer's request form
        return gate_answer_request(con, code, value if isinstance(value, dict) else {})
    if (value is not None and not isinstance(value, str)) or (text is not None and not isinstance(text, str)):
        raise GateRefused(400, "An answer is text.")
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        g = one(con, "SELECT * FROM gates WHERE code=?", (code,))
        if not g:
            raise GateRefused(404, "no such gate")
        if g["status"] != "open":
            raise GateRefused(409, "This question was already answered.")
        val = (value or "").strip() or None
        txt = (text or "").strip() or None
        opts = [o.get("value") for o in json.loads(g["options"] or "[]")]
        if val is not None and val not in opts:
            raise GateRefused(400, "That choice is not one of the options.")
        if val is None and not txt:
            raise GateRefused(400, "Choose an option or write an answer.")
        if txt and not g["allow_text"] and val is None:
            raise GateRefused(400, "Choose one of the options.")
        if via not in ("chat", "viewer", "other"):
            raise GateRefused(400, "answered via must be chat, viewer or other")
        who = actor_id if actor_id is not None else owner_actor(con)
        cur = con.execute("""UPDATE gates SET status='answered', answer_value=?, answer_text=?,
                             answered_by=(SELECT CASE kind WHEN 'owner' THEN 'owner' ELSE name END FROM actors
                                          WHERE id=?), answered_via=?, answered_by_actor_id=?
                             WHERE id=? AND status='open'""", (val, txt, who, via, who, g["id"]))
        if cur.rowcount != 1:
            raise GateRefused(409, "This question was already answered.")
        if own:
            con.execute("COMMIT")
        return {"ok": True}
    finally:
        if own and con.in_transaction:
            con.execute("ROLLBACK")


@_command("answer-gate", "record the owner's answer given in chat (the viewer's rules)",
          "ledger.py answer-gate GATE-0001 [--value OPTION] [--text 'their words'] [--via chat|other] "
          "[--by A-0001 (default: the owner)]")
def cmd_answer_gate(ctx):
    a = ctx.a
    who = resolve_actor(ctx.con, a.by, "--by") if a.by else None
    answer_gate(ctx.con, a.gate, a.value, _text_arg(a.text, "--text"), a.via, who)
    g = one(ctx.con, "SELECT answer_value, answer_text FROM gates WHERE code=?", (a.gate,))
    return [f"{a.gate}: answered ({', '.join(x for x in (g['answer_value'], g['answer_text']) if x)})"]


@_command("apply-gate", "record what was done with a gate's answer (a decision gate also records the decision)",
          "ledger.py apply-gate GATE-0001 --note 'what was done with the answer' [--revisit-when TEXT] "
          "[--actor A-0001]")
def cmd_apply_gate(ctx):
    a, con = ctx.a, ctx.con
    note = _need(_text_arg(a.note, "--note"), "--note (what was done with the answer)")
    g = one(con, "SELECT * FROM gates WHERE code=?", (a.gate,))
    if not g:
        raise WriteError(f"no gate {a.gate}")
    if g["status"] != "answered":
        raise WriteError(f"{a.gate} is {g['status']}; only an answered gate can be applied")
    out = []
    if g["kind"] == "decision":
        answer = " - ".join(x for x in (g["answer_value"], g["answer_text"]) if x)
        if g["decision_id"]:
            ctx.x("""UPDATE decisions SET status='decided', answer=?, rationale=?, decided_by_actor_id=?,
                     revisit_when=COALESCE(?, revisit_when) WHERE id=?""",
                  (answer, note, g["answered_by_actor_id"], a.revisit_when, g["decision_id"]))
            dcode = con.execute("SELECT code FROM decisions WHERE id=?", (g["decision_id"],)).fetchone()[0]
        else:
            dcode = next_code(con, "D")
            did = ctx.x("""INSERT INTO decisions(code, question, options, answer, rationale, status, revisit_when,
                           review_id, step_id, asked_by_actor_id, decided_by_actor_id)
                           VALUES (?,?,?,?,?, 'decided', ?,?,?,?,?)""",
                        (dcode, g["question"], g["options"], answer, note, a.revisit_when, g["review_id"],
                         g["step_id"], g["opened_by_actor_id"], g["answered_by_actor_id"])).lastrowid
            ctx.x("UPDATE gates SET decision_id=? WHERE id=?", (did, g["id"]))
            if g["issue_id"]:
                ctx.x("INSERT OR IGNORE INTO decision_issues(decision_id, issue_id) VALUES (?,?)", (did, g["issue_id"]))
        out.append(f"decision {dcode}: {answer}")
    ctx.x("UPDATE gates SET status='applied', applied_note=?, change_actor_id=? WHERE id=?",
          (note, ctx.actor_id, g["id"]))
    return [f"{a.gate}: applied"] + out


# ----- the owner interview and design goals (step 129): interview-ask, interview-answer, propose-goal, approve-goals,
# reject-goal, link-step-goals, and the read-only goals and interview any agent uses to look them up

IQ_CODE = re.compile(r"IQ-[0-9]{4,}")
GOAL_CODE = re.compile(r"G-[0-9]{2,}")


def question_row(con, code):
    if not IQ_CODE.fullmatch(code or ""):
        raise WriteError(f"{code!r} is not an interview question code like IQ-0001")
    r = one(con, "SELECT * FROM interview_questions WHERE code=?", (code,))
    if not r:
        raise WriteError(f"no question {code}")
    return r


def goal_row(con, code):
    if not GOAL_CODE.fullmatch(code or ""):
        raise WriteError(f"{code!r} is not a design goal code like G-01")
    r = one(con, "SELECT * FROM design_goals WHERE code=?", (code,))
    if not r:
        raise WriteError(f"no design goal {code}")
    return r


def _goal_domain(con, rv, domain):
    """None (the whole project) when no domain is given; else a catalog domain that review rv (if any) included."""
    if domain is None or not str(domain).strip():
        return None
    d = str(domain).strip()
    if not one(con, "SELECT 1 x FROM domains WHERE code=?", (d,)):
        raise WriteError(f"no domain {d!r} in the catalog")
    if rv is not None and not one(con, """SELECT 1 x FROM review_domains WHERE review_id=? AND domain_code=?
                                          AND decision='included'""", (rv["id"], d)):
        raise WriteError(f"{rv['code']} did not include domain {d!r}; use one of its approved domains, or leave "
                         f"--domain out for the whole project")
    return d


def _domain_label(con, code):
    if code is None:
        return "Whole project"
    r = one(con, "SELECT name FROM domains WHERE code=?", (code,))
    return f"{code} ({r['name']})" if r else code


def _served(con):
    return [r[0] for r in con.execute("SELECT value FROM goal_statuses WHERE served = 1 ORDER BY position")]


@_command("interview-ask", "record a question of the owner interview, about an approved domain or the whole project "
                           "(prints its code)",
          "ledger.py interview-ask --review R-0001 [--domain CODE] --why 'what in the code prompted it' 'QUESTION' "
          "[--gate] [--actor A-0002]")
def cmd_interview_ask(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, _need(a.review, "--review"))
    if rv["approved_at"] is None:
        raise WriteError(f"the interview comes after the owner approves the review's domains "
                         f"(review-status {rv['code']} approved)")
    dom = _goal_domain(con, rv, a.domain)
    why = _need(_text_arg(a.why, "--why"), "--why (what in the code prompted the question)")
    question = _need(_text_arg(a.question, "the question"), "the question")
    gate_id, out = None, []
    if a.gate:  # asked in the hub too: tr_gate_answers_question records the owner's answer there
        gcode = next_code(con, "GATE")
        gate_id = ctx.x("""INSERT INTO gates(code, kind, title, question, context, allow_text, blocking, review_id,
                           opened_by_actor_id) VALUES (?, 'other', ?, ?, ?, 1, 1, ?, ?)""",
                        (gcode, f"Interview: {_domain_label(con, dom)}", question, f"Why I ask: {why}", rv["id"],
                         ctx.actor_id)).lastrowid
        out.append(f"asked in the hub as {gcode}")
    code = next_code(con, "IQ")
    ctx.x("""INSERT INTO interview_questions(code, review_id, domain_code, question, why_asked, asked_by_actor_id,
             gate_id) VALUES (?,?,?,?,?,?,?)""", (code, rv["id"], dom, question, why, ctx.actor_id, gate_id))
    return [code] + out


@_command("interview-answer", "record the owner's answer to an interview question, verbatim (append-only)",
          "ledger.py interview-answer IQ-0001 'THE OWNER'S ANSWER' [--via chat|viewer] [--actor A-0002]")
def cmd_interview_answer(ctx):
    a, con = ctx.a, ctx.con
    q = question_row(con, a.question)
    answer = _need(_text_arg(a.answer, "the answer"), "the answer (the owner's words)")
    if a.via not in ("chat", "viewer"):
        raise WriteError(f"--via {a.via!r}: use chat or viewer")
    ctx.x("INSERT INTO interview_answers(question_id, answer, answered_via, answered_by_actor_id) VALUES (?,?,?,?)",
          (q["id"], answer, a.via, ctx.actor_id))
    return [f"{q['code']}: answered"]


def _goal_gate(ctx, rv, goal_id):
    """Put the goal on the review's open goal-approval gate (one confirm gate listing them), opening one if there is
    none, and rewrite the gate's context to list every goal on it. Returns the gate's code."""
    con = ctx.con
    g = one(con, """SELECT * FROM gates g WHERE g.review_id=? AND g.status='open'
                    AND EXISTS (SELECT 1 FROM design_goals d WHERE d.gate_id=g.id) ORDER BY g.id DESC LIMIT 1""",
            (rv["id"],))
    if g is None:
        code = next_code(con, "GATE")
        opts = [{"value": "approve", "label": "Approve these goals"},
                {"value": "change", "label": "Change some", "description": "say which, and how, in your note"}]
        gid = ctx.x("""INSERT INTO gates(code, kind, title, question, options, allow_text, blocking, review_id,
                       opened_by_actor_id) VALUES (?, 'confirm', ?, ?, ?, 1, 1, ?, ?)""",
                    (code, f"Approve the design goals of {rv['code']}",
                     "Do these statements say what you want the code to be? The reviewers, the plan and every agent "
                     "after them will work to the goals you approve.", json.dumps(opts), rv["id"],
                     ctx.actor_id)).lastrowid
    else:
        code, gid = g["code"], g["id"]
    ctx.x("UPDATE design_goals SET gate_id=? WHERE id=?", (gid, goal_id))
    lines = []
    for d in rows(con, """SELECT d.code, d.domain_code, d.text, (SELECT group_concat(q.code, ', ') FROM goal_sources gs
                          JOIN interview_questions q ON q.id=gs.question_id WHERE gs.goal_id=d.id) sources
                          FROM design_goals d WHERE d.gate_id=? ORDER BY d.domain_code IS NOT NULL, d.domain_code, d.id""",
                  (gid,)):
        lines.append(f"{d['code']} ({_domain_label(con, d['domain_code'])}): {d['text']}"
                     + (f" [from {d['sources']}]" if d["sources"] else ""))
    ctx.x("UPDATE gates SET context=? WHERE id=?", ("\n".join(lines), gid))
    return code


@_command("propose-goal", "write one statement of one of the owner's design goals, for a domain or the whole "
                          "project, citing the interview questions it summarizes (prints the goal's code)",
          "ledger.py propose-goal [--domain CODE] [--from IQ-0001,IQ-0003] [--source 'README.md, section 2'] "
          "'STATEMENT' [--review R-0001] [--gate] [--actor A-0002]")
def cmd_propose_goal(ctx):
    a, con = ctx.a, ctx.con
    text = _need(_text_arg(a.statement, "the statement"), "the goal's statement")
    qs = []
    for c in (a.sources or "").split(","):
        if c.strip() and c.strip() not in [q["code"] for q in qs]:
            qs.append(question_row(con, c.strip()))
    reviews = {q["review_id"] for q in qs}
    if a.review:
        reviews.add(review_row(con, a.review)["id"])
    if len(reviews) > 1:
        raise WriteError("the goal's questions and --review are from more than one review; propose it from one "
                         "review's interview")
    rv = one(con, "SELECT * FROM reviews WHERE id=?", (next(iter(reviews)),)) if reviews else None
    if a.gate and rv is None:
        raise WriteError("--gate asks for the approval in the review's hub: give --review R-0001 or --from a question")
    dom = _goal_domain(con, rv, a.domain)
    for q in qs:
        if dom is not None and q["domain_code"] is not None and q["domain_code"] != dom:
            raise WriteError(f"{q['code']} is about {q['domain_code']}, not {dom}; a goal cites questions of its own "
                             f"domain or whole-project ones")
    code = next_code(con, "G")
    gid = ctx.x("""INSERT INTO design_goals(code, text, source, domain_code, status, review_id, change_actor_id)
                   VALUES (?, ?, ?, ?, 'proposed', ?, ?)""",
                (code, text, "; ".join(x for x in ((getattr(a, "source", None) or "").strip() or None,
                                                   f"owner interview ({', '.join(q['code'] for q in qs)})" if qs
                                                   else None) if x) or None, dom,
                 rv["id"] if rv else None, ctx.actor_id)).lastrowid
    for q in qs:
        ctx.x("INSERT INTO goal_sources(goal_id, question_id) VALUES (?, ?)", (gid, q["id"]))
    if a.gate:
        return [code, f"waiting for the owner's approval in {_goal_gate(ctx, rv, gid)}"]
    return [code]


def _close_goal_gates(ctx, gate_ids, actor_id):
    """Answer each open goal-approval gate none of whose goals is still proposed, as the owner decided in chat:
    'approve' when all were approved, else 'change' with what was decided (through answer_gate(), its one set of
    rules)."""
    for gid in sorted(gate_ids):
        g = one(ctx.con, "SELECT code, status FROM gates WHERE id=?", (gid,))
        goals = rows(ctx.con, "SELECT code, status FROM design_goals WHERE gate_id=? ORDER BY id", (gid,))
        if g["status"] != "open" or any(x["status"] == "proposed" for x in goals):
            continue
        all_ok = all(x["status"] == "approved" for x in goals)
        answer_gate(ctx.con, g["code"], "approve" if all_ok else "change",
                    "decided in chat: " + ", ".join(f"{x['code']} {x['status']}" for x in goals), "chat", actor_id)


def _owner_decides(ctx, what):
    if ctx.actor_id is None:
        raise WriteError(f"give --actor: the owner who {what} (their A- code)")
    kind = ctx.con.execute("SELECT kind FROM actors WHERE id=?", (ctx.actor_id,)).fetchone()[0]
    if kind not in ("owner", "human"):
        raise WriteError(f"{actor_code(ctx.actor_id)} is a {kind}; design goals are decided by the owner: give the "
                         f"owner's code as --actor")
    return ctx.actor_id


@_command("approve-goals", "record the owner's approval of proposed design goals (answers their approval gate)",
          "ledger.py approve-goals G-01 [G-02 ...] --actor A-0001 (the owner)")
def cmd_approve_goals(ctx):
    owner = _owner_decides(ctx, "approved them")
    out, gates = [], set()
    for code in dict.fromkeys(ctx.a.goals):
        g = goal_row(ctx.con, code)
        if g["status"] != "proposed":
            raise WriteError(f"{code} is {g['status']}; only a proposed goal can be approved")
        ctx.x("""UPDATE design_goals SET status='approved', approved_by_actor_id=?, approved_at=?, change_actor_id=?
                 WHERE id=?""", (owner, now(), owner, g["id"]))
        if g["gate_id"]:
            gates.add(g["gate_id"])
        out.append(f"{code}: approved")
    _close_goal_gates(ctx, gates, owner)
    return out


@_command("reject-goal", "record that the owner did not approve a proposed design goal, with their reason",
          "ledger.py reject-goal G-01 --note 'the owner's reason' [--actor A-0001]")
def cmd_reject_goal(ctx):
    a = ctx.a
    note = _need(_text_arg(a.note, "--note"), "--note (the owner's reason)")
    g = goal_row(ctx.con, a.goal)
    if g["status"] != "proposed":
        raise WriteError(f"{a.goal} is {g['status']}; only a proposed goal can be rejected")
    ctx.x("UPDATE design_goals SET status='rejected', note=?, change_actor_id=? WHERE id=?", (note, ctx.actor_id, g["id"]))
    if g["gate_id"]:
        _close_goal_gates(ctx, {g["gate_id"]}, ctx.actor_id if ctx.actor_id is not None else owner_actor(ctx.con))
    return [f"{a.goal}: rejected"]


@_command("link-step-goals", "record which approved design goals a plan step serves",
          "ledger.py link-step-goals 12|P-0001#12|KEY G-01 [G-02 ...] [--plan P-0001] [--actor A-0002]")
def cmd_link_step_goals(ctx):
    a, con = ctx.a, ctx.con
    s = step_row(con, a.step, a.plan)
    served, out = _served(con), []
    for code in dict.fromkeys(a.goals):
        g = goal_row(con, code)
        if g["status"] not in served:
            raise WriteError(f"{code} is {g['status']}; a plan step serves approved goals only")
        cur = ctx.x("INSERT OR IGNORE INTO step_goals(step_id, goal_id) VALUES (?, ?)", (s["id"], g["id"]))
        out.append(f"step {s['number']} serves {code}" + ("" if cur.rowcount else " already"))
    return out


def _goal_details(con, goals):
    for g in goals:
        g["sources"] = []
        for q in rows(con, """SELECT q.id, q.code, q.domain_code domain, q.question FROM goal_sources gs
                              JOIN interview_questions q ON q.id=gs.question_id WHERE gs.goal_id=? ORDER BY q.id""",
                      (g["id"],)):
            q["answers"] = [r[0] for r in con.execute(
                "SELECT answer FROM interview_answers WHERE question_id=? ORDER BY id", (q.pop("id"),))]
            g["sources"].append(q)
        g["steps"] = [r[0] for r in con.execute("""SELECT p.code || ' #' || s.number FROM step_goals sg
                      JOIN steps s ON s.id=sg.step_id JOIN plans p ON p.id=s.plan_id WHERE sg.goal_id=?
                      ORDER BY p.code, s.number""", (g["id"],))]
        g["approved_by"] = actor_code(g["approved_by"]) if g["approved_by"] is not None else None
        g.pop("id")
    return goals


def goals_list(con, domain=None, include_all=False, review=None):
    """The design goals (approved only, unless include_all), whole-project ones first, each with its sources (the
    interview questions and their answers) and the plan steps that serve it. domain: that domain's goals and the
    whole-project ones."""
    where, args = [], []
    if not include_all:
        where.append("g.status IN (SELECT value FROM goal_statuses WHERE served = 1)")
    if domain:
        where.append("(g.domain_code = ? OR g.domain_code IS NULL)"); args.append(domain)
    if review:
        where.append("r.code = ?"); args.append(review)
    goals = rows(con, f"""SELECT g.id, g.code, g.domain_code domain, g.text, g.status, gs.label status_label,
        g.approved_by_actor_id approved_by, g.approved_at, r.code review, g.note, g.source, gt.code gate
        FROM design_goals g JOIN goal_statuses gs ON gs.value=g.status LEFT JOIN reviews r ON r.id=g.review_id
        LEFT JOIN gates gt ON gt.id=g.gate_id
        WHERE {' AND '.join(where) or '1'} ORDER BY g.domain_code IS NOT NULL, g.domain_code, g.id""", args)
    return _goal_details(con, goals)


@_command("goals", "list the owner's approved design goals with their sources (read-only; --all adds proposed, "
                   "rejected and retired ones)",
          "ledger.py goals [--domain CODE] [--all] [--json]", read=True)
def cmd_goals(con, a):
    if a.domain and not one(con, "SELECT 1 x FROM domains WHERE code=?", (a.domain,)):
        raise WriteError(f"no domain {a.domain!r} in the catalog")
    goals = goals_list(con, a.domain, a.all)
    if a.json:
        return [json.dumps({"goals": goals}, ensure_ascii=False, indent=1)]
    if not goals:
        return ["no approved design goals" + ("" if a.all else " (goals --all lists proposed ones too)")]
    out, last = [], object()
    for g in goals:
        if g["domain"] != last:
            last = g["domain"]
            out.append(("" if not out else "\n") + _domain_label(con, last))
        out.append(f"  {g['code']}  {g['text']}" + (f"  [{g['status']}]" if a.all else ""))
        for q in g["sources"]:
            out.append(f"      from {q['code']}: {q['question']}")
        if g["steps"]:
            out.append(f"      served by {', '.join(g['steps'])}")
    return out


def interview_list(con, review=None, domain=None):
    where, args = [], []
    if review:
        where.append("r.code = ?"); args.append(review)
    if domain:
        where.append("q.domain_code = ?"); args.append(domain)
    qs = rows(con, f"""SELECT q.id, q.code, r.code review, q.domain_code domain, q.question, q.why_asked, q.asked_at,
        q.asked_by_actor_id asked_by, gt.code gate FROM interview_questions q JOIN reviews r ON r.id=q.review_id
        LEFT JOIN gates gt ON gt.id=q.gate_id WHERE {' AND '.join(where) or '1'}
        ORDER BY q.id""", args)
    for q in qs:
        q["answers"] = rows(con, """SELECT answer, answered_via, answered_at FROM interview_answers WHERE question_id=?
                                    ORDER BY id""", (q.pop("id"),))
        q["asked_by"] = actor_code(q["asked_by"]) if q["asked_by"] is not None else None
    return qs


@_command("interview", "list the owner interview: each question (by review and domain, with what prompted it) and "
                       "its answers (read-only)",
          "ledger.py interview [--review R-0001] [--domain CODE] [--json]", read=True)
def cmd_interview(con, a):
    if a.review:
        review_row(con, a.review)
    qs = interview_list(con, a.review, a.domain)
    if a.json:
        return [json.dumps({"questions": qs}, ensure_ascii=False, indent=1)]
    if not qs:
        return ["no interview questions" + (" match" if a.review or a.domain else " yet")]
    out, last = [], None
    by_group = {}
    for q in qs:  # grouped by review and domain (whole project first), each group in the order asked
        by_group.setdefault((q["review"], q["domain"]), []).append(q)
    for q in [q for k in sorted(by_group, key=lambda k: (k[0], k[1] is not None, k[1] or "")) for q in by_group[k]]:
        if (q["review"], q["domain"]) != last:
            last = (q["review"], q["domain"])
            out.append(("" if not out else "\n") + f"{q['review']} · {_domain_label(con, q['domain'])}")
        out.append(f"  {q['code']}  {q['question']}")
        out.append(f"      why: {q['why_asked']}")
        for ans in q["answers"] or [None]:
            out.append(f"      answer ({ans['answered_via']}, {ans['answered_at']}): {ans['answer']}" if ans
                       else "      not answered yet")
    return out


# ----- findings: import-findings, import-checks, load-index, match

FINDING_KINDS = ('defect', 'test', 'validation', 'refactor', 'feature', 'design', 'docs', 'logging', 'perf',
                 'security', 'ops', 'verify')
INDEX_COLUMNS = ("id", "sev", "kind", "theme", "sub", "locations", "where_report", "summary", "before", "fix", "test",
                 "related", "known_issue", "domain", "goals", "requirements")
CHECK_COLUMNS = ("issue", "outcome", "evidence", "finding")
MATCH_COLUMNS = ("point", "issue", "match_type", "confidence", "rationale", "relations")
TRACE_COLUMNS = ("requirement", "verdict", "where_implemented", "tested", "finding", "evidence")
VERDICTS = ('matches', 'differs', 'partly', 'missing', 'untested')
LOCATION = re.compile(r"^(?P<path>[^:#]+?)(?::(?P<a>[0-9]+)(?:-(?P<b>[0-9]+))?)?(?:#(?P<sym>[^#]+))?$")


def fingerprint(domain, theme, sub):
    """What findings and issues are matched on: domain:theme:sub. The symbol is not part of it (WALK-L17), so the
    same problem cited with and without a symbol, or at another function, gets one fingerprint."""
    return f"{domain}:{theme or ''}:{sub}"


def parse_location(text, repo_names, default_repo=None):
    """(repository name, path, start, end, symbol) from 'repo:path[:start[-end]][#symbol]'; the repository may be left
    out when the review has one. 'repo:path' alone is the whole file. ValueError with the reason."""
    t = text.strip()
    repo, rest = None, t
    head, sep, tail = t.partition(":")
    if sep and head in repo_names:
        repo, rest = head, tail
    elif default_repo is not None:
        repo = default_repo
    else:
        raise ValueError(f"location {text!r} does not start with one of the review's repositories "
                         f"({', '.join(sorted(repo_names))})")
    m = LOCATION.match(rest)
    if not m:
        raise ValueError(f"location {text!r} is not repo:path[:start[-end]][#symbol]")
    path = m.group("path").strip()
    if bad_location_path(path):
        raise ValueError(f"location {text!r}: the path must be relative, without .. or a leading -")
    a = int(m.group("a")) if m.group("a") else None
    b = int(m.group("b")) if m.group("b") else a
    if a is not None and (a < 1 or b < a):
        raise ValueError(f"location {text!r}: lines must be start-end with 1 <= start <= end")
    return repo, path, a, b, (m.group("sym") or "").strip() or None


def _split_list(text):
    return [x for x in re.split(r"[,\s]+", text or "") if x]


def _review_repos(con, review_id):
    return {r["name"]: r for r in rows(con, """SELECT rp.*, rr.commit_sha FROM review_repositories rr
            JOIN repositories rp ON rp.id = rr.repository_id WHERE rr.review_id=?""", (review_id,))}


def _read_tsv(path, columns, what):
    """[(line number, [cells])] of a tab-separated file without header; every row must have len(columns) cells."""
    try:
        text = sys.stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WriteError(f"cannot read {path}: {e}")
    out, bad = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) != len(columns):
            bad.append(f"{what} line {n}: {len(cells)} columns, expected {len(columns)} ({', '.join(columns)})")
            continue
        out.append((n, cells))
    if bad:
        raise WriteError(f"{len(bad)} bad rows: " + "; ".join(bad[:8]))
    return out


def import_findings(ctx, rv, run_label, path):
    """Validate a findings index TSV (references/formats/index.md) completely, then insert every point unmatched with
    its locations (the first one primary), goals and requirement links. All or nothing. Returns lines to print."""
    con = ctx.con
    run = one(con, "SELECT * FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], run_label))
    if not run:
        raise WriteError(f"{rv['code']} has no reviewer run {run_label!r} (add-run)")
    repos = _review_repos(con, rv["id"])
    if not repos:
        raise WriteError(f"{rv['code']} has no repositories (snapshot)")
    default_repo = next(iter(repos)) if len(repos) == 1 else None
    themes = {r[0] for r in con.execute("SELECT code FROM themes WHERE retired_at IS NULL")}
    domains = {r[0] for r in con.execute("SELECT code FROM domains WHERE retired_at IS NULL")}
    sevs = {r[0] for r in con.execute("SELECT value FROM severities")}
    taken = {r[0] for r in con.execute("SELECT point_id FROM findings WHERE review_id=?", (rv["id"],))}
    problems, points, seen, exists_cache = [], [], {}, {}
    for n, c in _read_tsv(path, INDEX_COLUMNS, "index"):
        row = dict(zip(INDEX_COLUMNS, c))
        pid, where = row["id"], f"line {n} ({row['id'] or 'no id'})"
        if not pid:
            problems.append(f"line {n}: no id")
        elif pid in seen:
            problems.append(f"{where}: id used twice (also line {seen[pid]})")
        elif pid in taken:
            problems.append(f"{where}: {rv['code']} already has a point {pid}")
        seen.setdefault(pid, n)
        if row["sev"] not in sevs:
            problems.append(f"{where}: severity {row['sev']!r} is not one of {', '.join(sorted(sevs))}")
        if row["kind"] not in FINDING_KINDS:
            problems.append(f"{where}: kind {row['kind']!r} is not one of {', '.join(FINDING_KINDS)}")
        if row["theme"] not in themes:
            problems.append(f"{where}: unknown theme {row['theme']!r}")
        dom = row["domain"] or run["domain_code"]
        if dom not in domains:
            problems.append(f"{where}: unknown domain {dom!r}")
        for k in ("sub", "summary"):
            if not row[k]:
                problems.append(f"{where}: needs {k}")
        locs = []
        for t in [x for x in row["locations"].split(";") if x.strip()]:
            try:
                rname, p, a, b, sym = parse_location(t, set(repos), default_repo)
            except ValueError as e:
                problems.append(f"{where}: {e}"); continue
            key = (rname, p)
            if key not in exists_cache:
                r = repos[rname]
                try:
                    git(repo_dir(ctx.root, r["path"], bool(r["allow_outside_root"])), "cat-file", "-e",
                        f"{r['commit_sha']}:{p}")
                    exists_cache[key] = True
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    exists_cache[key] = False
            if not exists_cache[key]:
                problems.append(f"{where}: {rname}:{p} does not exist at the reviewed commit "
                                f"{repos[rname]['commit_sha'][:10]}")
            locs.append((rname, p, a, b, sym))
        if not locs:
            problems.append(f"{where}: needs at least one location")
        goals = []
        for g in _split_list(row["goals"]):
            gid = con.execute("SELECT id FROM design_goals WHERE code=?", (g,)).fetchone()
            if not gid:
                problems.append(f"{where}: unknown design goal {g}")
            else:
                goals.append(gid[0])
        reqs = []
        for spec in _split_list(row["requirements"]):
            code, _, verdict = spec.partition("=")
            rid = con.execute("SELECT id FROM requirements WHERE code=?", (code,)).fetchone()
            if not rid:
                problems.append(f"{where}: unknown requirement {code}")
            elif verdict not in VERDICTS:
                problems.append(f"{where}: requirement {code} needs =verdict ({', '.join(VERDICTS)})")
            else:
                reqs.append((rid[0], verdict))
        known = row["known_issue"]
        if known and not con.execute("SELECT 1 FROM issues WHERE code=?", (known,)).fetchone():
            problems.append(f"{where}: unknown known issue {known}")
        points.append((row, dom, locs, goals, reqs))
    for row, *_ in points:
        for r in _split_list(row["related"]):
            if r not in seen:
                problems.append(f"{row['id']}: related id {r} is not in this file")
    if problems:
        raise WriteError(f"{len(problems)} problems in {path}, nothing loaded: " + "; ".join(problems[:10])
                         + (f"; ... {len(problems) - 10} more" if len(problems) > 10 else ""))
    counts = {}
    for row, dom, locs, goals, reqs in points:
        related = _split_list(row["related"])
        fid = ctx.x("""INSERT INTO findings(review_id, reviewer_run_id, point_id, severity, kind, domain_code,
                       theme_code, sub, summary, detail, before_state, recommendation, recommended_test, where_report,
                       fingerprint, related_point_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rv["id"], run["id"], row["id"], row["sev"], row["kind"], dom, row["theme"], row["sub"],
                     row["summary"], f"The report names {row['known_issue']}." if row["known_issue"] else None,
                     row["before"] or None, row["fix"] or None, row["test"] or None, row["where_report"] or None,
                     fingerprint(dom, row["theme"], row["sub"]), json.dumps(related) if related else None)).lastrowid
        for i, (rname, p, a, b, sym) in enumerate(locs):
            ctx.x("""INSERT INTO finding_locations(finding_id, repository_id, path, line_start, line_end, symbol, role,
                     whole_file) VALUES (?,?,?,?,?,?,?,?)""",
                  (fid, repos[rname]["id"], p, a, b, sym, "primary" if i == 0 else "also",
                   1 if a is None and sym is None else 0))
        for gid in goals:
            ctx.x("INSERT INTO finding_goals(finding_id, goal_id) VALUES (?,?)", (fid, gid))
        for rid, verdict in reqs:
            ctx.x("INSERT INTO finding_requirements(finding_id, requirement_id, verdict) VALUES (?,?,?)",
                  (fid, rid, verdict))
        counts[row["sev"]] = counts.get(row["sev"], 0) + 1
    return [f"{rv['code']} {run_label}: loaded {len(points)} points ("
            + ", ".join(f"{k} {counts[k]}" for k in sorted(counts)) + ") unmatched; next: fill-refs, then match"]


CHECK_OUTCOMES = ('still-present', 'changed', 'fixed', 'regressed', 'obsolete', 'not-checked')


def import_checks(ctx, rv, path, actor_id):
    """Validate a known-issues TSV (issue, outcome, evidence, finding) and insert review_issue_checks. All or
    nothing."""
    con = ctx.con
    problems, out, seen = [], [], set()
    points = {r[0]: r[1] for r in con.execute("SELECT point_id, id FROM findings WHERE review_id=?", (rv["id"],))}
    for n, c in _read_tsv(path, CHECK_COLUMNS, "known issues"):
        code, outcome, evidence, point = c
        where = f"line {n} ({code})"
        iss = one(con, "SELECT id FROM issues WHERE code=?", (code,))
        if not iss:
            problems.append(f"{where}: no issue {code}"); continue
        if code in seen:
            problems.append(f"{where}: {code} is checked twice")
        seen.add(code)
        if outcome not in CHECK_OUTCOMES:
            problems.append(f"{where}: outcome {outcome!r} is not one of {', '.join(CHECK_OUTCOMES)}")
        if outcome in ("fixed", "regressed", "changed", "still-present", "obsolete") and not evidence:
            problems.append(f"{where}: a {outcome} check needs evidence")
        if point and point not in points:
            problems.append(f"{where}: {rv['code']} has no point {point}")
        out.append((iss["id"], outcome, evidence or None, points.get(point) if point else None))
    if problems:
        raise WriteError(f"{len(problems)} problems in {path}, nothing loaded: " + "; ".join(problems[:10]))
    for iid, outcome, evidence, fid in out:
        ctx.x("""INSERT INTO review_issue_checks(review_id, issue_id, outcome, finding_id, evidence, actor_id)
                 VALUES (?,?,?,?,?,?)""", (rv["id"], iid, outcome, fid, evidence, actor_id))
    return [f"{rv['code']}: recorded {len(out)} checks of known issues"]


def import_trace(ctx, rv, path):
    """A requirement trace TSV (requirement, verdict, where_implemented, tested, finding, evidence) into
    review_requirement_checks."""
    con = ctx.con
    problems, out = [], []
    points = {r[0]: r[1] for r in con.execute("SELECT point_id, id FROM findings WHERE review_id=?", (rv["id"],))}
    for n, c in _read_tsv(path, TRACE_COLUMNS, "requirement trace"):
        code, verdict, where_impl, tested, point, evidence = c
        req = con.execute("SELECT id FROM requirements WHERE code=?", (code,)).fetchone()
        if not req:
            problems.append(f"line {n}: no requirement {code}"); continue
        if verdict not in VERDICTS:
            problems.append(f"line {n}: verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
        if tested and tested not in ("yes", "partly", "no"):
            problems.append(f"line {n}: tested must be yes, partly or no")
        if point and point not in points:
            problems.append(f"line {n}: {rv['code']} has no point {point}")
        out.append((req[0], verdict, where_impl or None, tested or None, points.get(point), evidence or None))
    if problems:
        raise WriteError(f"{len(problems)} problems in {path}, nothing loaded: " + "; ".join(problems[:10]))
    for r in out:
        ctx.x("""INSERT INTO review_requirement_checks(review_id, requirement_id, verdict, where_implemented, tested,
                 finding_id, evidence) VALUES (?,?,?,?,?,?,?)""", (rv["id"], *r))
    return [f"{rv['code']}: recorded {len(out)} requirement verdicts"]


@_command("import-findings", "load an index TSV of one report into the ledger (all or nothing, unmatched)",
          "ledger.py import-findings R-0001 RUN_LABEL points.tsv   (format: references/formats/index.md)")
def cmd_import_findings(ctx):
    return import_findings(ctx, review_row(ctx.con, ctx.a.review), ctx.a.run, ctx.a.file)


@_command("import-checks", "load a report's known-issues TSV into review_issue_checks (all or nothing)",
          "ledger.py import-checks R-0001 known.tsv [--run RUN_LABEL (the checks' actor)]   "
          "(format: references/formats/index.md)")
def cmd_import_checks(ctx):
    rv = review_row(ctx.con, ctx.a.review)
    actor = ctx.actor_id
    if ctx.a.run:
        run = one(ctx.con, "SELECT actor_id FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], ctx.a.run))
        if not run:
            raise WriteError(f"{rv['code']} has no reviewer run {ctx.a.run!r}")
        actor = run["actor_id"]
    return import_checks(ctx, rv, ctx.a.file, actor)


@_command("load-index", "load one report's points, known-issue checks and requirement trace in one transaction",
          "ledger.py load-index R-0001 RUN_LABEL points.tsv [known.tsv] [--trace trace.tsv]   "
          "(formats: references/formats/index.md)")
def cmd_load_index(ctx):
    rv = review_row(ctx.con, ctx.a.review)
    out = import_findings(ctx, rv, ctx.a.run, ctx.a.points)
    run = one(ctx.con, "SELECT actor_id FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], ctx.a.run))
    if ctx.a.known:
        out += import_checks(ctx, rv, ctx.a.known, run["actor_id"])
    if ctx.a.trace:
        out += import_trace(ctx, rv, ctx.a.trace)
    return out


MATCH_TYPES_HELP = "new|still-present|changed|regression|recurrence|continuation"
CONFIDENCES = ('certain', 'likely', 'possible')


def parse_relations(text):
    """[(relation, issue code)] from 'relation:ISS-0001;relation:ISS-0002' (the matcher TSV's extra relations)."""
    out = []
    for part in [p.strip() for p in (text or "").split(";") if p.strip()]:
        rel, sep, code = part.partition(":")
        if not sep or not re.fullmatch(r"ISS-[0-9]{4,}", code.strip()) or not rel.strip():
            raise WriteError(f"relation {part!r} is not relation:ISS-0001")
        out.append((rel.strip(), code.strip()))
    return out


def open_issue_for(ctx, f, rv, title=None):
    """A new issue (next ISS- code) from a finding: its title, domain, theme, sub, kind, severity, fingerprint, before
    state and recommendation; first seen in the finding's review."""
    code = next_code(ctx.con, "ISS")
    iid = ctx.x("""INSERT INTO issues(code, title, domain_code, theme_code, sub, kind, severity, fingerprint,
                   before_state, recommendation, first_seen_review_id, change_actor_id, change_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, title or f["summary"], f["domain_code"], f["theme_code"], f["sub"], f["kind"], f["severity"],
                 f["fingerprint"] or fingerprint(f["domain_code"], f["theme_code"], f["sub"]), f["before_state"],
                 f["recommendation"], rv["id"], ctx.actor_id, f"opened from finding {f['point_id']}")).lastrowid
    return iid, code


def match_finding(ctx, rv, point, target, match_type=None, confidence="likely", rationale=None, relations=""):
    """Match one finding by UPDATE (the insert-then-update rule: tr_finding_matched does the bookkeeping). target is
    an ISS- code or 'new' (the next ISS- code). A false finding with target new gets its own issue, closed as
    obsolete with resolution 'false finding: <why>' (C-11). Returns the line to print."""
    con = ctx.con
    rels = parse_relations(relations)
    f = one(con, "SELECT * FROM findings WHERE review_id=? AND point_id=?", (rv["id"], point))
    if not f:
        raise WriteError(f"{rv['code']} has no point {point}")
    if f["issue_id"] is not None:
        have = con.execute("SELECT code FROM issues WHERE id=?", (f["issue_id"],)).fetchone()[0]
        if have == target or str(target).lower() == "new":
            raise WriteError(f"{point} is already matched to {have}"
                             + ("; give an ISS- code to re-point it" if have != target else ""))
    if confidence not in CONFIDENCES:
        raise WriteError(f"confidence {confidence!r} is not one of {', '.join(CONFIDENCES)}")
    types = [r[0] for r in con.execute("SELECT value FROM match_types ORDER BY position")]
    known_rel = {r[0] for r in con.execute("SELECT value FROM relation_types")}
    for rel, code in rels:
        if rel not in known_rel:
            raise WriteError(f"relation {rel!r} is not one of {', '.join(sorted(known_rel))}")
        issue_row(con, code)
    false = f["verification"] == "false"
    if str(target).lower() == "new":
        match_type = match_type or "new"
        if match_type not in ("new", "continuation"):
            raise WriteError(f"a new issue is matched as new or continuation, not {match_type}")
        if match_type == "continuation" and not any(r == "continuation-of" for r, _ in rels):
            raise WriteError("a continuation needs its relation: --relations continuation-of:ISS-0001")
        iid, code = open_issue_for(ctx, f, rv)
    else:
        iss = issue_row(con, target)
        iid, code = iss["id"], iss["code"]
        if not match_type:
            raise WriteError(f"give --type for a match to an existing issue ({MATCH_TYPES_HELP})")
    if match_type not in types:
        raise WriteError(f"match type {match_type!r} is not one of {', '.join(types)}")
    ctx.x("""UPDATE findings SET issue_id=?, match_type=?, match_confidence=?, match_rationale=? WHERE id=?""",
          (iid, match_type, confidence, rationale, f["id"]))
    for rel, other in rels:
        ctx.x("""INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, confidence, rationale,
                 established_in_review_id, established_by_actor_id)
                 VALUES (?, (SELECT id FROM issues WHERE code=?), ?, ?, ?, ?, ?)""",
              (iid, other, rel, confidence, rationale or f"from the match of {point}", rv["id"], ctx.actor_id))
    note = ""
    if false and str(target).lower() == "new":
        why = (f["verification_note"] or rationale or "").strip()
        if not why:
            raise WriteError(f"{point} is a false finding: give --rationale (why it is wrong) for its obsolete issue")
        ctx.x("""UPDATE issues SET status='obsolete', resolution=?, change_note=?, change_review_id=?,
                 change_actor_id=? WHERE id=?""",
              (f"false finding: {why}", f"false finding {point}", rv["id"], ctx.actor_id, iid))
        note = " (false finding: closed as obsolete)"
    return f"{point} -> {code} {match_type}{note}"


@_command("match", "match a finding to an issue (an ISS- code, or new for the next code); or a matcher TSV",
          f"ledger.py match R-0001 POINT ISS-0001|new --type {MATCH_TYPES_HELP} [--confidence certain|likely|possible] "
          "[--rationale TEXT] [--relations 'related-to:ISS-0002;caused-by:ISS-0003']   |   "
          "ledger.py match R-0001 --file matches.tsv   (format: references/formats/index.md)")
def cmd_match(ctx):
    a = ctx.a
    rv = review_row(ctx.con, a.review)
    if a.file:
        if a.point or a.target:
            raise WriteError("give either --file or POINT and ISSUE, not both")
        out = []
        for n, c in _read_tsv(a.file, MATCH_COLUMNS, "matches"):
            point, target, mtype, conf, why, rels = c
            try:
                out.append(match_finding(ctx, rv, point, target, mtype or None, conf or "likely", why or None, rels))
            except WriteError as e:
                raise WriteError(f"line {n} ({point}): {e}")
        return out + [f"{rv['code']}: matched {len(out)} points"]
    if not a.point or not a.target:
        raise WriteError("give POINT and ISSUE (an ISS- code or new), or --file")
    return [match_finding(ctx, rv, a.point, a.target, a.type, a.confidence, _text_arg(a.rationale, "--rationale"),
                          a.relations or "")]


# ----- report-finding: a defect found during fix work (step 127)

def cited_location(root, repo, commit, spec_path, a, b, sym):
    """Read path at commit with git and check the citation: the lines exist, and a symbol given with lines is on one
    of them (given alone, it is looked up). Returns the location's fields with the excerpt filled from that commit."""
    d = repo_dir(root, repo["path"], bool(repo["allow_outside_root"]))
    try:
        data = git(d, "show", f"{commit}:{spec_path}")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        raise WriteError(f"{spec_path} does not exist at commit {commit[:10]} (cite lines with "
                         f"git show {commit[:10]}:{spec_path}; code added later is cited at the commit that added it)")
    try:
        text = None if b"\0" in data else data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    out = {"commit_sha": commit, "path": spec_path, "line_start": a, "line_end": b, "symbol": sym,
           "whole_file": 0, "excerpt": None, "excerpt_sha256": None, "excerpt_redacted": 0, "excerpt_status": None}
    try:
        out["blob_sha"] = git(d, "rev-parse", f"{commit}:{spec_path}").decode().strip()
    except RuntimeError:
        out["blob_sha"] = None
    if text is None:
        if a is not None or sym:
            raise WriteError(f"{spec_path} is not a text file at {commit[:10]}; cite it whole (repo:path)")
        out.update(whole_file=1, excerpt_status="not-text", excerpt_sha256=hashlib.sha256(data).hexdigest())
        return out
    lines = text.splitlines()
    if a is None and sym is None:
        if not lines:
            raise WriteError(f"{spec_path} is empty at {commit[:10]}; nothing to cite")
        a, b = 1, len(lines)
        out.update(whole_file=1, line_start=a, line_end=b)
    elif a is None:
        a, why = symbol_line(lines, sym)
        if a is None:
            raise WriteError(f"{spec_path} at {commit[:10]}: {why}")
        b = a
        out.update(line_start=a, line_end=b)
    if b > len(lines):
        raise WriteError(f"{spec_path} has {len(lines)} lines at {commit[:10]}; lines {a}-{b} are past its end "
                         f"(cite the lines of git show {commit[:10]}:{spec_path}, not of an edited file)")
    if sym and out["line_start"] == a and not any(
            re.search(r"(?<![\w$])" + re.escape(sym) + r"(?![\w$])", t) for t in lines[a - 1:b]):
        raise WriteError(f"{sym!r} is not in {spec_path} lines {a}-{b} at {commit[:10]}")
    cut = out["whole_file"] and b - a + 1 > WHOLE_FILE_EXCERPT_LINES
    excerpt = "\n".join(lines[a - 1:(a - 1 + WHOLE_FILE_EXCERPT_LINES) if cut else b])
    shown, masked = mask_secrets(excerpt)
    out.update(excerpt=shown, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
               excerpt_redacted=1 if masked else 0, excerpt_status="truncated" if cut else None)
    return out


def discovery_candidates(con, fp, locs, exclude_review_point=None):
    """Issues that may be the same problem: the same fingerprint (domain:theme:sub), a location at the same path and
    symbol, or (without a symbol) at lines of the same path that overlap at the same commit. [(code, status, title,
    why)]."""
    found = {}
    for r in con.execute("SELECT code, status, title FROM issues WHERE fingerprint=?", (fp,)):
        found.setdefault(r[0], (r[0], r[1], r[2], "same fingerprint"))
    for loc in locs:
        sql = """SELECT DISTINCT i.code, i.status, i.title FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
                 JOIN issues i ON i.id = f.issue_id WHERE fl.path=?"""
        args = [loc["path"]]
        if loc["symbol"]:
            sql += " AND fl.symbol=?"; args.append(loc["symbol"])
            why = f"same path and symbol {loc['path']}#{loc['symbol']}"
        else:  # lines overlapping at the same commit
            sql += " AND fl.commit_sha=? AND fl.line_start <= ? AND COALESCE(fl.line_end, fl.line_start) >= ?"
            args += [loc["commit_sha"], loc["line_end"], loc["line_start"]]
            why = f"overlapping lines of {loc['path']}"
        for r in con.execute(sql, args):
            found.setdefault(r[0], (r[0], r[1], r[2], why))
    return sorted(found.values())


@_command("report-finding", "record a defect found during fix work, match it and plan it (or ask the owner)",
          "ledger.py report-finding --step 12 --severity H|M|L|T --domain CODE --where 'path:start-end#symbol' "
          "[--where ...] --summary TEXT --before TEXT --fix TEXT --test 'test_name: what it asserts' "
          "[--kind defect] [--theme CODE] [--sub SLUG] [--commit SHA (default: the review's commit)] "
          "[--review R-0001] [--plan P-0001] [--issue ISS-0001 | --new-issue] "
          "[--into-step 30 | --new-step 'Title' [--change TEXT] [--proof TEXT] [--phase N] | --gate] [--actor A-0001]")
def cmd_report_finding(ctx):
    a, con = ctx.a, ctx.con
    for flag, value in (("--where", a.where), ("--before", a.before), ("--fix", a.fix), ("--summary", a.summary),
                        ("--severity", a.severity), ("--domain", a.domain), ("--step", a.step)):
        if not value or (isinstance(value, str) and not value.strip()):
            raise WriteError(f"a discovery needs {flag}")
    if a.into_step and (a.new_step or a.gate) or (a.new_step and a.gate):
        raise WriteError("say where it is planned once: --into-step, --new-step or --gate")
    if a.issue and a.new_issue:
        raise WriteError("give --issue or --new-issue, not both")
    step = step_row(con, a.step, a.plan)
    pl = one(con, "SELECT * FROM plans WHERE id=?", (step["plan_id"],))
    if a.review:
        rv = review_row(con, a.review)
        if not con.execute("SELECT 1 FROM plan_reviews WHERE plan_id=? AND review_id=?", (pl["id"], rv["id"])).fetchone():
            raise WriteError(f"{rv['code']} is not one of plan {pl['code']}'s reviews")
    else:
        rv = one(con, """SELECT r.* FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id WHERE pr.plan_id=?
                         ORDER BY r.started_at DESC, r.id DESC LIMIT 1""", (pl["id"],))
        if not rv:
            raise WriteError(f"plan {pl['code']} covers no review")
    if a.domain not in {r[0] for r in con.execute("SELECT code FROM domains WHERE retired_at IS NULL")}:
        raise WriteError(f"unknown domain {a.domain!r}")
    kind = a.kind or "defect"
    if kind not in FINDING_KINDS:
        raise WriteError(f"kind {kind!r} is not one of {', '.join(FINDING_KINDS)}")
    theme = a.theme
    if theme and not con.execute("SELECT 1 FROM themes WHERE code=?", (theme,)).fetchone():
        raise WriteError(f"unknown theme {theme!r}")
    repos = _review_repos(con, rv["id"])
    if not repos:
        raise WriteError(f"{rv['code']} has no repositories")
    default_repo = next(iter(repos)) if len(repos) == 1 else None
    locs = []
    for spec in [s for w in a.where for s in w.split(";") if s.strip()]:
        try:
            rname, p, la, lb, sym = parse_location(spec, set(repos), default_repo)
        except ValueError as e:
            raise WriteError(str(e))
        repo = repos[rname]
        d = repo_dir(ctx.root, repo["path"], bool(repo["allow_outside_root"]))
        commit = full_commit(d, a.commit) if a.commit else repo["commit_sha"]
        loc = cited_location(ctx.root, repo, commit, p, la, lb, sym)
        loc["repository_id"] = repo["id"]
        loc["permalink"] = None
        if repo["permalink_template"]:
            link = permalink_for(repo["permalink_template"], commit, p, None if loc["whole_file"] else loc["line_start"],
                                 None if loc["whole_file"] else loc["line_end"])
            loc["permalink"] = link if link and SAFE_URL.match(link) else None
        locs.append(loc)
    sub = a.sub or re.sub(r"[^a-z0-9]+", "-", a.summary.lower()).strip("-")[:48]
    fp = fingerprint(a.domain, theme, sub)
    candidates = discovery_candidates(con, fp, locs)
    if not a.issue and not a.new_issue and candidates:
        raise WriteError("it may be a known issue; look and give --issue CODE or --new-issue. Candidates: "
                         + "; ".join(f"{c} [{s}] {t[:70]} ({why})" for c, s, t, why in candidates[:8]))
    n = con.execute("""SELECT max(CAST(substr(point_id, 5) AS INTEGER)) FROM findings
                       WHERE review_id=? AND point_id GLOB 'FIX-[0-9]*'""", (rv["id"],)).fetchone()[0] or 0
    point = f"FIX-{n + 1}"
    fid = ctx.x("""INSERT INTO findings(review_id, point_id, severity, kind, domain_code, theme_code, sub, summary,
                   before_state, recommendation, recommended_test, fingerprint, verification, verified_by_actor_id,
                   verified_at, verification_note, discovered_in_step_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'unverified', NULL, NULL, NULL, ?)""",
                (rv["id"], point, a.severity, kind, a.domain, theme, sub, a.summary.strip(), a.before.strip(),
                 a.fix.strip(), (a.test or "").strip() or None, fp, step["id"])).lastrowid
    for i, loc in enumerate(locs):
        cols = {k: loc[k] for k in ("repository_id", "commit_sha", "path", "line_start", "line_end", "symbol",
                                    "blob_sha", "excerpt", "excerpt_sha256", "permalink", "excerpt_redacted",
                                    "whole_file", "excerpt_status")}
        cols.update(finding_id=fid, role="primary" if i == 0 else "also")
        ctx.x(f"INSERT INTO finding_locations({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
              tuple(cols.values()))
    out = [f"{rv['code']} {point}: recorded (found in step {step['number']}, {len(locs)} location"
           f"{'s' if len(locs) != 1 else ''} at {locs[0]['commit_sha'][:10]})"]
    if a.issue:
        iss = issue_row(con, a.issue)
        fixed = con.execute("SELECT fixed FROM issue_statuses WHERE value=?", (iss["status"],)).fetchone()[0]
        mtype = "new" if iss["first_seen_review_id"] == rv["id"] else ("regression" if fixed else "still-present")
        out.append(match_finding(ctx, rv, point, iss["code"], mtype, "likely",
                                 f"reported during step {step['number']}"))
        icode = iss["code"]
    else:
        out.append(match_finding(ctx, rv, point, "new", "new", "certain", f"found during step {step['number']}"))
        icode = con.execute("SELECT i.code FROM findings f JOIN issues i ON i.id = f.issue_id WHERE f.id=?",
                            (fid,)).fetchone()[0]
    for c, s, t, why in candidates[:8]:
        out.append(f"  candidate {c} [{s}] {t[:80]} ({why})")
    if len(candidates) > 8:
        out.append(f"  ... {len(candidates) - 8} more candidates")
    if a.into_step:
        target = step_row(con, a.into_step, pl["code"])
        if target["status"] in ("done", "dropped"):
            raise WriteError(f"step {target['number']} is {target['status']}; plan it into an open step, a new step "
                             f"or --gate")
        ctx.x("INSERT INTO step_findings(step_id, finding_id) VALUES (?,?)", (target["id"], fid))
        out.append(f"{point} planned in step {target['number']} ({target['title']})")
    elif a.new_step:
        ph = one(con, "SELECT * FROM phases WHERE plan_id=? AND number=?", (pl["id"], a.phase)) if a.phase else \
            one(con, "SELECT * FROM phases WHERE id=?", (step["phase_id"],))
        if not ph:
            raise WriteError(f"plan {pl['code']} has no phase {a.phase}")
        num = con.execute("SELECT max(number) FROM steps WHERE plan_id=?", (pl["id"],)).fetchone()[0] + 1
        sid = ctx.x("""INSERT INTO steps(plan_id, phase_id, number, section, title, change, proof, drafter_key,
                       change_actor_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (pl["id"], ph["id"], num, "Fix-time discoveries", a.new_step.strip(), (a.change or a.fix).strip(),
                     (a.proof or a.test or "a test that fails before the fix").strip(), f"{rv['code']}:{point}",
                     ctx.actor_id)).lastrowid
        ctx.x("INSERT INTO step_findings(step_id, finding_id) VALUES (?,?)", (sid, fid))
        out.append(f"{point} planned in new step {num} (phase {ph['number']}: {a.new_step.strip()})")
    else:
        gcode = next_code(con, "GATE")
        ctx.x("""INSERT INTO gates(code, kind, title, question, context, options, allow_text, blocking, review_id,
                 plan_id, issue_id, opened_by_actor_id) VALUES (?, 'decision', ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)""",
              (gcode, f"Plan the discovery {point} ({icode})",
               f"Where should {point} be fixed: in an existing step, a new step, or not at all?",
               f"{a.summary.strip()}\nNow: {a.before.strip()}\nFix: {a.fix.strip()}",
               json.dumps([{"value": "existing-step", "label": "In an existing step (say which)"},
                           {"value": "new-step", "label": "In a new step"},
                           {"value": "no-fix", "label": "Do not fix it (say why)"}]),
               rv["id"], pl["id"], issue_row(con, icode)["id"], ctx.actor_id))
        out.append(f"{point} is not planned yet: asked the owner in {gcode} (v_unplanned_discoveries lists it "
                   f"until a step closes it)")
    return out


# ----- the remaining writes of a review (step 131): plans, goals, requirements, domains, runs, verification, test
# runs, step commits, guard tests, metrics, artifacts, pushed tags; and redact (step 132)

PLAN_STATUSES = ('drafting', 'awaiting-approval', 'approved', 'in-progress', 'complete', 'superseded', 'abandoned')


@_command("new-plan", "start a fix plan over one or more reviews (prints its P- code)",
          "ledger.py new-plan --title T --review R-0001 [--review R-0002]... [--actor A-0001]")
def cmd_new_plan(ctx):
    a, con = ctx.a, ctx.con
    title = _need(a.title, "--title")
    if not a.review:
        raise WriteError("give --review R-0001 for each review the plan covers")
    reviews = [review_row(con, r) for r in dict.fromkeys(a.review)]
    code = next_code(con, "P")
    pid = ctx.x("INSERT INTO plans(code, title, change_actor_id) VALUES (?, ?, ?)",
                (code, title, ctx.actor_id)).lastrowid
    for rv in reviews:
        ctx.x("INSERT INTO plan_reviews(plan_id, review_id) VALUES (?, ?)", (pid, rv["id"]))
    return [code, f"{code}: drafting, covers {', '.join(r['code'] for r in reviews)}"]


@_command("plan-status", "move a plan to its next status (approved only once its plan-approval gate is applied)",
          "ledger.py plan-status P-0001 awaiting-approval|approved|in-progress|complete|drafting|superseded|abandoned "
          "--note TEXT [--approved-by A-0001] [--superseded-by P-0002] [--actor A-0001]")
def cmd_plan_status(ctx):
    a, con = ctx.a, ctx.con
    pl = plan_row(con, _need(a.plan, "the plan's code"))
    if a.status not in PLAN_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(PLAN_STATUSES)}")
    note = _need(_text_arg(a.note, "--note"), "--note (why the status changes)")
    sets = {"status": a.status, "change_note": note, "change_actor_id": ctx.actor_id}
    if a.status == "approved" and pl["status"] != "approved":
        gates = rows(con, "SELECT * FROM gates WHERE plan_id=? AND kind='plan-approval' ORDER BY id", (pl["id"],))
        pending = [g for g in gates if g["status"] in ("open", "answered")]
        applied = [g for g in gates if g["status"] == "applied"]
        if pending:
            raise WriteError(f"the plan-approval gate {pending[0]['code']} of {pl['code']} is {pending[0]['status']}; "
                             f"record the owner's answer and apply it first (apply-gate {pending[0]['code']})")
        if not applied:
            raise WriteError(f"{pl['code']} has no applied plan-approval gate; ask the owner first (open-gate "
                             f"plan-approval --plan {pl['code']}, answer-gate, apply-gate)")
        sets["approved_at"] = now()
        sets["approved_by_actor_id"] = (resolve_actor(con, a.approved_by, "--approved-by") if a.approved_by
                                        else applied[-1]["answered_by_actor_id"] or owner_actor(con))
    if a.status == "complete":
        left = rows(con, "SELECT number, status FROM steps WHERE plan_id=? AND status NOT IN ('done','dropped') "
                         "ORDER BY number", (pl["id"],))
        if left:
            raise WriteError(f"step {left[0]['number']} of {pl['code']} is {left[0]['status']}"
                             + (f" (and {len(left) - 1} more)" if len(left) > 1 else "")
                             + "; a plan is complete when every step is done or dropped")
    if a.superseded_by:
        if a.status != "superseded":
            raise WriteError("--superseded-by goes with status superseded")
        nxt = plan_row(con, a.superseded_by)
        if nxt["id"] == pl["id"]:
            raise WriteError(f"{pl['code']} cannot supersede itself")
        sets["superseded_by"] = nxt["id"]
    ctx.x(f"UPDATE plans SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), pl["id"]))
    return [f"{pl['code']}: {pl['status']} -> {a.status}"]


def _table_columns(con, table):
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


@_command("add-requirement", "record a requirement with its source (prints its REQ- code)",
          "ledger.py add-requirement TEXT --source docs/spec.md|URL|'owner, chat <date>' [--location 'section 2.1'] "
          "[--stated-at TIME] [--review R-0001] [--actor A-0001]")
def cmd_add_requirement(ctx):
    a, con = ctx.a, ctx.con
    text = _need(_text_arg(a.text, "the requirement"), "the requirement's text")
    source = _need(a.source, "--source (a document path, an issue URL, or 'owner, chat <date>')")
    rv = review_row(con, a.review) if a.review else None
    code = next_code(con, "REQ")
    ctx.x("""INSERT INTO requirements(code, text, source, source_location, stated_at, first_review_id)
             VALUES (?,?,?,?,?,?)""", (code, text, source, a.location, a.stated_at, rv["id"] if rv else None))
    return [code]


DOMAIN_CATEGORIES = ('correctness', 'architecture', 'user-facing', 'security-operations', 'documentation', 'custom')


@_command("add-domain", "add a domain the catalog lacks (category custom by default), with its theme",
          "ledger.py add-domain CODE NAME --description 'Applies when ...' [--report-file F.md] "
          "[--category custom] [--theme CODE=Name] [--actor A-0001]")
def cmd_add_domain(ctx):
    a, con = ctx.a, ctx.con
    code = _need(a.code, "the domain's code")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", code):
        raise WriteError(f"domain code {code!r}: use lowercase letters, digits and dashes (payments, data-export)")
    if one(con, "SELECT 1 x FROM domains WHERE code=?", (code,)):
        raise WriteError(f"domain {code} is already in the catalog")
    desc = _need(a.description, "--description (when the domain applies)")
    if a.category not in DOMAIN_CATEGORIES:
        raise WriteError(f"category {a.category!r} is not one of {', '.join(DOMAIN_CATEGORIES)}")
    ctx.x("INSERT INTO domains(code, name, category, description, report_file) VALUES (?,?,?,?,?)",
          (code, _need(a.name, "the domain's name"), a.category, desc, a.report_file or f"{code}.md"))
    out = f"domain {code} ({a.category})"
    if a.theme:
        tcode, sep, tname = a.theme.partition("=")
        if not sep or not tcode.strip() or not tname.strip():
            raise WriteError(f"--theme {a.theme!r}: write it as CODE=Name")
        if not one(con, "SELECT 1 x FROM themes WHERE code=?", (tcode.strip(),)):
            ctx.x("INSERT INTO themes(code, name) VALUES (?, ?)", (tcode.strip(), tname.strip()))
        out += f", theme {tcode.strip()}"
    return [out]


OWNER_CHANGES = ('none', 'added-by-owner', 'removed-by-owner', 'modified-by-owner')


@_command("review-domain", "include or skip a domain in a review, with the reason and the owner's change",
          "ledger.py review-domain R-0001 DOMAIN include|skip --reason TEXT "
          "[--owner-change added-by-owner|removed-by-owner|modified-by-owner] [--reviewers N] [--split-note TEXT] "
          "[--actor A-0001]")
def cmd_review_domain(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    reason = _need(_text_arg(a.reason, "--reason"), "--reason (why the domain is in or out)")
    decision = {"include": "included", "included": "included", "skip": "skipped", "skipped": "skipped"}.get(a.decision)
    if not decision:
        raise WriteError(f"{a.decision!r}: say include or skip")
    if not one(con, "SELECT 1 x FROM domains WHERE code=?", (a.domain,)):
        raise WriteError(f"no domain {a.domain!r} in the catalog (add it with: ledger.py add-domain)")
    if a.owner_change is not None and a.owner_change not in OWNER_CHANGES:
        raise WriteError(f"--owner-change {a.owner_change!r} is not one of {', '.join(OWNER_CHANGES)}")
    if rv["status"] in ("handed-off", "abandoned"):
        raise WriteError(f"{rv['code']} is {rv['status']}; its domains are settled")
    run = one(con, "SELECT label FROM reviewer_runs WHERE review_id=? AND domain_code=? ORDER BY id LIMIT 1",
              (rv["id"], a.domain))
    if decision == "skipped" and run:
        raise WriteError(f"{rv['code']} has a reviewer run in {a.domain} ({run['label']}); a domain with reviewer "
                         f"runs cannot be skipped (cancel the run with finish-run --status cancelled instead)")
    old = one(con, "SELECT * FROM review_domains WHERE review_id=? AND domain_code=?", (rv["id"], a.domain))
    change = a.owner_change or (old["owner_change"] if old else "none")
    if a.reviewers is not None and a.reviewers < 0:
        raise WriteError("--reviewers is a count (0 or more)")
    if old:
        ctx.x("""UPDATE review_domains SET decision=?, reason=?, owner_change=?,
                 reviewers_planned=COALESCE(?, reviewers_planned), split_note=COALESCE(?, split_note)
                 WHERE review_id=? AND domain_code=?""",
              (decision, reason, change, a.reviewers, a.split_note, rv["id"], a.domain))
    else:
        ctx.x("""INSERT INTO review_domains(review_id, domain_code, decision, reason, owner_change, reviewers_planned,
                 split_note) VALUES (?,?,?,?,?,?,?)""",
              (rv["id"], a.domain, decision, reason, change, a.reviewers, a.split_note))
    return [f"{rv['code']} {a.domain}: {decision}" + (f" ({change})" if change != "none" else "")]


def _ledger_file(ctx, path, what, must_exist=True):
    """(stored path, sha256 or None) of a file that must be inside the ledger's folder: relative to the project root
    when it is inside it, else absolute."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p) if (Path.cwd() / p).exists() else (Path(ctx.root) / p)
    p = p.resolve()
    folder = Path(ctx.db).parent.resolve()
    if folder not in p.parents:
        raise WriteError(f"{what} {path} is not inside the ledger's folder {folder}; copy it there first "
                         f"(reviews/R-0001/...)")
    if not p.is_file():
        if must_exist:
            raise WriteError(f"{what} {path}: no such file")
        sha = None
    else:
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
    try:
        stored = p.relative_to(Path(ctx.root).resolve()).as_posix()
    except ValueError:
        stored = str(p)
    return stored, sha


@_command("finish-run", "record a reviewer run's result: status, counts, report path and its sha256",
          "ledger.py finish-run R-0001 LABEL [--status finished|failed|cancelled] --report FILE [--items N] "
          "[--high N] [--medium N] [--low N] [--tests N] [--message TEXT|@file] [--actor A-0001]")
def cmd_finish_run(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    run = one(con, "SELECT * FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], a.label))
    if not run:
        raise WriteError(f"no run {a.label!r} in {rv['code']} (add-run records it)")
    if a.status not in ("finished", "failed", "cancelled"):
        raise WriteError(f"status {a.status!r} is not one of finished, failed, cancelled")
    if run["status"] in ("finished", "failed", "cancelled"):
        raise WriteError(f"run {a.label} is already {run['status']}")
    if a.status == "finished" and not a.report:
        raise WriteError("a finished run needs --report (its report file, inside the ledger's folder)")
    path, sha = _ledger_file(ctx, a.report, "the report") if a.report else (None, None)
    for n in ("items", "high", "medium", "low", "tests"):
        if getattr(a, n) is not None and getattr(a, n) < 0:
            raise WriteError(f"--{n} is a count (0 or more)")
    ctx.x("""UPDATE reviewer_runs SET status=?, report_path=COALESCE(?, report_path),
             report_sha256=COALESCE(?, report_sha256), items_reviewed=?, high_count=?, medium_count=?, low_count=?,
             test_count=?, final_message=?, finished_at=? WHERE id=?""",
          (a.status, path, sha, a.items, a.high, a.medium, a.low, a.tests, _text_arg(a.message, "--message"), now(),
           run["id"]))
    out = [f"{a.label}: {run['status']} -> {a.status}" + (f", report {path} (sha256 {sha[:12]})" if path else "")]
    if path:
        ctx.x("INSERT INTO artifacts(review_id, reviewer_run_id, kind, path, sha256) VALUES (?,?,?,?,?)",
              (rv["id"], run["id"], "report", path, sha))
    return out


VERIFICATIONS = ('confirmed', 'partly', 'false', 'not-checkable')


def _live_review(con, what):
    live = rows(con, "SELECT * FROM reviews WHERE status NOT IN ('handed-off','abandoned') ORDER BY id")
    if len(live) != 1:
        raise WriteError(f"{len(live)} reviews are in progress; {what}")
    return live[0]


@_command("verify", "record the manager's verification of a finding: confirmed, partly, false or not-checkable",
          "ledger.py verify R-0001:SEC-M1|SEC-M1 confirmed|partly|false|not-checkable --note 'what you saw' "
          "[--review R-0001] [--actor A-0001]")
def cmd_verify(ctx):
    a, con = ctx.a, ctx.con
    m = re.fullmatch(r"(R-[0-9]{4,})[:/](.+)", a.point.strip())
    rv = review_row(con, m.group(1)) if m else (review_row(con, a.review) if a.review else
                                                 _live_review(con, "write the point as R-0001:SEC-M1"))
    pid = m.group(2) if m else a.point.strip()
    note = _need(_text_arg(a.note, "--note"), "--note (what you saw)")
    if a.verdict not in VERIFICATIONS:
        raise WriteError(f"verdict {a.verdict!r} is not one of {', '.join(VERIFICATIONS)}")
    f = one(con, "SELECT id, verification FROM findings WHERE review_id=? AND point_id=?", (rv["id"], pid))
    if not f:
        raise WriteError(f"no point {pid} in {rv['code']}")
    ctx.x("""UPDATE findings SET verification=?, verification_note=?, verified_by_actor_id=?, verified_at=?
             WHERE id=?""", (a.verdict, note, ctx.actor_id, now(), f["id"]))
    return [f"{rv['code']}:{pid}: {f['verification']} -> {a.verdict}"]


TEST_PURPOSES = ('baseline', 'targeted', 'flake-check', 'mutation', 'before-fix', 'after-fix', 'ci')


def _one_repo(con, name):
    """The repository named, or the only one; None when the ledger has none and no name is given."""
    if name:
        r = one(con, "SELECT * FROM repositories WHERE name=?", (name,))
        if not r:
            raise WriteError(f"no repository named {name!r}")
        return r
    found = rows(con, "SELECT * FROM repositories ORDER BY id")
    if len(found) > 1:
        raise WriteError(f"the ledger has {len(found)} repositories; name one with --repo")
    return found[0] if found else None


def _repo_commit(ctx, repo, ref):
    """(repository dir, full commit hash) of ref (default HEAD) in repo, from git."""
    if repo is None:
        raise WriteError("the ledger has no repositories; add one with: ledger.py add-repo NAME PATH")
    d = repo_dir(ctx.root, repo["path"], bool(repo["allow_outside_root"]))
    return d, full_commit(d, ref or "HEAD")


@_command("record-test-run", "record a test run (baseline, before-fix, after-fix, ...) at its commit",
          "ledger.py record-test-run --purpose baseline|targeted|flake-check|mutation|before-fix|after-fix|ci "
          "--command 'python3 -m unittest' (--review R-0001 | --step 12 [--plan P-0001]) [--repo NAME] "
          "[--commit REF (default HEAD)] [--passed N] [--failed N] [--skipped N] [--errored N] [--exit-code N] "
          "[--duration-ms N] [--output FILE] [--note TEXT] [--actor A-0001]")
def cmd_record_test_run(ctx):
    a, con = ctx.a, ctx.con
    if a.purpose not in TEST_PURPOSES:
        raise WriteError(f"purpose {a.purpose!r} is not one of {', '.join(TEST_PURPOSES)}")
    command = _need(a.command, "--command (what was run)")
    if not a.review and not a.step:
        raise WriteError("give --review or --step (what the test run belongs to)")
    rv = review_row(con, a.review) if a.review else None
    st = step_row(con, a.step, a.plan) if a.step else None
    repo = _one_repo(con, a.repo)
    sha = _repo_commit(ctx, repo, a.commit)[1] if repo is not None or a.commit else None
    for n in ("passed", "failed", "skipped", "errored", "duration_ms"):
        if getattr(a, n) is not None and getattr(a, n) < 0:
            raise WriteError(f"--{n.replace('_', '-')} is a count (0 or more)")
    tid = ctx.x("""INSERT INTO test_runs(review_id, step_id, repository_id, commit_sha, command, purpose, passed, failed,
                   skipped, errored, exit_code, duration_ms, output_path, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rv["id"] if rv else None, st["id"] if st else None, repo["id"] if repo else None, sha, command,
                 a.purpose, a.passed, a.failed, a.skipped, a.errored, a.exit_code, a.duration_ms, a.output,
                 _text_arg(a.note, "--note"))).lastrowid
    counts = ", ".join(f"{getattr(a, n)} {n}" for n in ("passed", "failed", "skipped", "errored")
                       if getattr(a, n) is not None)
    return [f"test run {tid}: {a.purpose}" + (f" at {sha[:10]}" if sha else "") + (f": {counts}" if counts else "")]


def record_step_commit(ctx, step, ref, repo_name=None, pr=None, summary=None):
    """Record (or refresh) the step_commits row of a step's commit, from git: the full hash, the current branch when
    it contains the commit, the subject and commit time. Returns (full hash, the line to print)."""
    if pr is not None and not SAFE_URL.match(pr):
        raise WriteError("--pr must be the pull request's https:// address")
    repo = _one_repo(ctx.con, repo_name)
    d, sha = _repo_commit(ctx, repo, ref)
    branch = None
    try:
        head = git(d, "symbolic-ref", "--short", "-q", "HEAD").decode().strip() or None
        if head and subprocess.run(["git", "-C", str(d), "merge-base", "--is-ancestor", sha, "HEAD"],
                                   capture_output=True, timeout=15).returncode == 0:
            branch = head
    except (RuntimeError, OSError, subprocess.SubprocessError):
        pass
    info = git(d, "show", "-s", "--format=%cI%n%s", sha).decode("utf-8", "replace").split("\n", 1)
    subject = info[1].strip() if len(info) > 1 else None
    ctx.x("""INSERT INTO step_commits(step_id, repository_id, commit_sha, branch, pr_url, summary, committed_at)
             VALUES (?,?,?,?,?,?,?)
             ON CONFLICT(step_id, commit_sha) DO UPDATE SET pr_url=COALESCE(excluded.pr_url, pr_url),
               summary=COALESCE(?, summary), branch=COALESCE(branch, excluded.branch)""",
          (step["id"], repo["id"], sha, branch, pr, summary or subject, utc_time(info[0].strip()), summary))
    return sha, f"step {step['number']}: commit {sha[:10]}" + (f" on {branch}" if branch else "")


@_command("add-commit", "record a plan step's commit (and its pull request) from git",
          "ledger.py add-commit 12|P-0001#12|KEY COMMIT [--plan P-0001] [--repo NAME] [--pr URL] [--summary TEXT] "
          "[--actor A-0001]")
def cmd_add_commit(ctx):
    a = ctx.a
    s = step_row(ctx.con, a.step, a.plan)
    return [record_step_commit(ctx, s, a.commit, a.repo, a.pr, a.summary)[1]]


def record_guard(ctx, iss, path, name, *, step=None, commit=None, failed_before=None, repo=None):
    if one(ctx.con, "SELECT 1 x FROM guard_tests WHERE issue_id=? AND test_path=? AND test_name=?",
           (iss["id"], path, name)):
        raise WriteError(f"{iss['code']} already has guard test {path}::{name}")
    ctx.x("""INSERT INTO guard_tests(issue_id, repository_id, test_path, test_name, step_id, added_commit_sha,
             failed_before_fix, passes_after_fix) VALUES (?,?,?,?,?,?,?,1)""",
          (iss["id"], repo["id"] if repo else None, path, name, step["id"] if step else None, commit,
           None if failed_before is None else int(bool(failed_before))))
    return f"{iss['code']}: guard test {path}::{name}"


def _guard_spec(spec):
    """(issue code or None, test path, test name) from PATH::NAME or ISS-0001=PATH::NAME."""
    m = re.fullmatch(r"(?:(ISS-[0-9]{4,})=)?(.+?)::(.+)", spec.strip())
    if not m or not m.group(2).strip() or not m.group(3).strip():
        raise WriteError(f"--guard {spec!r}: write it as PATH::NAME or ISS-0001=PATH::NAME")
    return m.group(1), m.group(2).strip(), m.group(3).strip()


@_command("add-guard", "record the test that guards a fixed issue (it passes now; --failed-before if it failed "
                       "before the fix)",
          "ledger.py add-guard ISS-0001 TEST_PATH TEST_NAME [--failed-before] [--step 12 [--plan P-0001]] "
          "[--commit REF] [--repo NAME] [--actor A-0001]")
def cmd_add_guard(ctx):
    a, con = ctx.a, ctx.con
    iss = issue_row(con, a.issue)
    path, name = _need(a.test_path, "the test's path"), _need(a.test_name, "the test's name")
    step = step_row(con, a.step, a.plan) if a.step else None
    repo = _one_repo(con, a.repo)
    commit = _repo_commit(ctx, repo, a.commit)[1] if a.commit else None
    return [record_guard(ctx, iss, path, name, step=step, commit=commit,
                         failed_before=True if a.failed_before else None, repo=repo)]


@_command("confirm-guard", "record that a review checked fixed issues' guard tests: still there, or --gone",
          "ledger.py confirm-guard R-0002 ISS-0001 [ISS-0002]... [--test PATH::NAME] [--gone] [--actor A-0001]")
def cmd_confirm_guard(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    test = _guard_spec(a.test)[1:] if a.test else None
    out = []
    for code in dict.fromkeys(a.issues):
        iss = issue_row(con, code)
        sql, args = "SELECT id, test_path, test_name FROM guard_tests WHERE issue_id=?", [iss["id"]]
        if test:
            sql += " AND test_path=? AND test_name=?"; args += list(test)
        found = rows(con, sql, args)
        if not found:
            raise WriteError(f"{iss['code']} has no guard test{' ' + '::'.join(test) if test else ''} "
                             f"(record one with add-guard)")
        for g in found:
            ctx.x("UPDATE guard_tests SET still_exists=?, last_confirmed_review_id=? WHERE id=?",
                  (0 if a.gone else 1, rv["id"], g["id"]))
            out.append(f"{iss['code']}: {g['test_path']}::{g['test_name']} "
                       f"{'is gone' if a.gone else 'still guards it'} ({rv['code']})")
    return out


@_command("set-verdict", "store a review's one-line verdict and short summary (shown in the hub)",
          "ledger.py set-verdict R-0001 --verdict TEXT [--summary TEXT|@file] [--actor A-0001]")
def cmd_set_verdict(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    verdict = _need(_text_arg(a.verdict, "--verdict"), "--verdict (the review's verdict in one sentence)")
    ctx.x("UPDATE reviews SET verdict=?, summary=COALESCE(?, summary), change_actor_id=? WHERE id=?",
          (verdict, _text_arg(a.summary, "--summary"), ctx.actor_id, rv["id"]))
    return [f"{rv['code']}: verdict recorded"]


@_command("set-doc-url", "record the address of the document the owner keeps for a review or a plan",
          "ledger.py set-doc-url R-0001|P-0001 URL [--actor A-0001]")
def cmd_set_doc_url(ctx):
    a, con = ctx.a, ctx.con
    code = _need(a.code, "the review's or plan's code")
    if not re.match(r"^[RP]-[0-9]+$", code):
        raise WriteError(f"{code!r} is not a review or a plan; give R-0001 or P-0001")
    url = _need(a.url, "the address").strip()
    if not SAFE_URL.match(url):
        raise WriteError(f"{url!r} is not an http(s) address")
    if code.startswith("R-"):
        ctx.x("UPDATE reviews SET summary_doc_url=? WHERE id=?", (url, review_row(con, code)["id"]))
    else:
        ctx.x("UPDATE plans SET doc_url=? WHERE id=?", (url, plan_row(con, code)["id"]))
    return [f"{code}: document {url}"]


@_command("plan-add-review", "add a review to the reviews a plan covers",
          "ledger.py plan-add-review P-0001 R-0002 [--actor A-0001]")
def cmd_plan_add_review(ctx):
    a, con = ctx.a, ctx.con
    pl = plan_row(con, _need(a.plan, "the plan's code"))
    rv = review_row(con, _need(a.review, "the review's code"))
    if pl["status"] in ("superseded", "abandoned"):
        raise WriteError(f"{pl['code']} is {pl['status']}; add the review to the plan that replaced it")
    if one(con, "SELECT 1 x FROM plan_reviews WHERE plan_id=? AND review_id=?", (pl["id"], rv["id"])):
        raise WriteError(f"{pl['code']} already covers {rv['code']}")
    ctx.x("INSERT INTO plan_reviews(plan_id, review_id) VALUES (?, ?)", (pl["id"], rv["id"]))
    covered = rows(con, "SELECT r.code FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id "
                        "WHERE pr.plan_id=? ORDER BY r.code", (pl["id"],))
    return [f"{pl['code']}: covers {', '.join(r['code'] for r in covered)}"]


@_command("add-metric", "record a measured, counted or estimated metric of a review",
          "ledger.py add-metric R-0001 NAME VALUE --method measured|counted|estimated [--domain D] [--repo NAME] "
          "[--scope all] [--unit U] [--note TEXT] [--actor A-0001]")
def cmd_add_metric(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    if a.method not in ("measured", "counted", "estimated"):
        raise WriteError(f"method {a.method!r} is not one of measured, counted, estimated")
    try:
        value = float(a.value)
    except (TypeError, ValueError):
        raise WriteError(f"VALUE {a.value!r} is not a number")
    if a.domain and not one(con, "SELECT 1 x FROM domains WHERE code=?", (a.domain,)):
        raise WriteError(f"no domain {a.domain!r} in the catalog")
    repo = _one_repo(con, a.repo) if a.repo else None
    name = _need(a.name, "the metric's name")
    ctx.x("""INSERT INTO metrics(review_id, domain_code, repository_id, name, scope, value, unit, method, note)
             VALUES (?,?,?,?,?,?,?,?,?)""",
          (rv["id"], a.domain, repo["id"] if repo else None, name, a.scope or "all", value, a.unit, a.method,
           _text_arg(a.note, "--note")))
    return [f"{rv['code']} {name} = {value:g}{' ' + a.unit if a.unit else ''} ({a.scope or 'all'})"]


ARTIFACT_KINDS = ('brief', 'report', 'index', 'summary-doc', 'plan-file', 'progress-page', 'probe-script', 'other')


@_command("add-artifact", "record a file of the review or plan (kept in the ledger's folder) or a published address",
          "ledger.py add-artifact brief|report|index|summary-doc|plan-file|progress-page|probe-script|other "
          "(--path FILE | --url URL) (--review R-0001 [--run LABEL] | --plan P-0001) [--actor A-0001]   "
          "(progress-page with --plan and --url also sets the plan's page address)")
def cmd_add_artifact(ctx):
    a, con = ctx.a, ctx.con
    if a.kind not in ARTIFACT_KINDS:
        raise WriteError(f"kind {a.kind!r} is not one of {', '.join(ARTIFACT_KINDS)}")
    if not a.path and not a.url:
        raise WriteError("give --path or --url (where the artifact is)")
    if a.url and not SAFE_URL.match(a.url):
        raise WriteError("--url must be an https:// address")
    if not a.review and not a.plan:
        raise WriteError("give --review or --plan (what the artifact belongs to)")
    rv = review_row(con, a.review) if a.review else None
    pl = plan_row(con, a.plan) if a.plan else None
    run = None
    if a.run:
        if not rv:
            raise WriteError("--run needs --review (run labels are per review)")
        run = one(con, "SELECT id FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], a.run))
        if not run:
            raise WriteError(f"no run {a.run!r} in {rv['code']}")
    path, sha = _ledger_file(ctx, a.path, "the artifact", must_exist=not a.url) if a.path else (None, None)
    aid = ctx.x("INSERT INTO artifacts(review_id, plan_id, reviewer_run_id, kind, path, url, sha256) "
                "VALUES (?,?,?,?,?,?,?)", (rv["id"] if rv else None, pl["id"] if pl else None,
                                           run["id"] if run else None, a.kind, path, a.url, sha)).lastrowid
    out = [f"artifact {aid}: {a.kind} {path or a.url}"]
    if a.kind == "progress-page" and pl and a.url:
        ctx.x("UPDATE plans SET page_url=? WHERE id=?", (a.url, pl["id"]))
        out.append(f"{pl['code']}: page {a.url}")
    return out


def cmd_tag_review_pushed(ctx):
    """tag-review R-0001 --pushed: record that the review's tags were pushed (after git push origin review/R-0001)."""
    con = ctx.con
    rv = review_row(con, ctx.a.review)
    tagged = rows(con, """SELECT rr.repository_id, rr.tag_name, rr.tag_pushed, rp.name FROM review_repositories rr
                          JOIN repositories rp ON rp.id = rr.repository_id
                          WHERE rr.review_id=? AND rr.tag_name IS NOT NULL ORDER BY rp.id""", (rv["id"],))
    if not tagged:
        raise WriteError(f"{rv['code']} has no recorded tag; run tag-review {rv['code']} first, then push it")
    out = []
    for t in tagged:
        if t["tag_pushed"]:
            out.append(f"{t['name']}: {t['tag_name']} was already recorded as pushed")
            continue
        ctx.x("UPDATE review_repositories SET tag_pushed=1 WHERE review_id=? AND repository_id=?",
              (rv["id"], t["repository_id"]))
        ctx.x("""INSERT INTO events(entity_type, entity_id, event, to_value, note, actor_id, review_id)
                 VALUES ('review', ?, 'tag-pushed', ?, ?, ?, ?)""",
              (rv["id"], t["tag_name"], f"{t['name']}: git push origin {t['tag_name']}", ctx.actor_id, rv["id"]))
        out.append(f"{t['name']}: {t['tag_name']} recorded as pushed")
    return out


cmd_tag_review_pushed.cli = ("", "")


# ----- redact (step 132)

REDACTED = "[redacted]"
# The history entity a redaction is logged against, by table (else the row's own review, plan, step or issue).
REDACT_ENTITIES = {"findings": "finding", "issues": "issue", "reviews": "review", "steps": "step", "plans": "plan",
                   "decisions": "decision", "gates": "gate", "issue_relations": "relation",
                   "review_issue_checks": "check", "step_findings": "step-finding", "design_goals": "goal",
                   "interview_questions": "question"}


def _redact_row(con, table, cols, ref):
    """The rowid of the row named by ref: a row id, its code (ISS-0001, GATE-0001), or R-0001:POINT for a finding."""
    ref = str(ref).strip()
    if ref.isdigit():
        r = con.execute(f'SELECT rowid FROM "{table}" WHERE rowid=?', (int(ref),)).fetchone()
    else:
        m = re.fullmatch(r"(R-[0-9]{4,})[:/](.+)", ref)
        if m and table == "findings":
            r = con.execute("SELECT f.id FROM findings f JOIN reviews r ON r.id = f.review_id "
                            "WHERE r.code=? AND f.point_id=?", m.groups()).fetchone()
        elif "code" in cols:
            r = con.execute(f'SELECT rowid FROM "{table}" WHERE code=?', (ref,)).fetchone()
        else:
            r = None
    if not r:
        raise WriteError(f"no row {ref} in {table}")
    return r[0]


def _redactable(con, table, column, info):
    """True for a free-text column: declared TEXT, not a key, not a reference, not unique, not a word from a list."""
    col = info[column]
    if "TEXT" not in (col["type"] or "").upper() or col["pk"]:
        return False
    if column in {r[3] for r in con.execute(f'PRAGMA foreign_key_list("{table}")')}:
        return False
    for ix in con.execute(f'PRAGMA index_list("{table}")'):
        if ix[2] and column in [r[2] for r in con.execute(f'PRAGMA index_info("{ix[1]}")')]:
            return False
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] or ""
    return not re.search(rf"\b{re.escape(column)}\s+IN\s*\(", sql)


@_command("redact", "replace a secret in the ledger's text with [redacted], everywhere it was copied, and log why",
          "ledger.py redact TABLE.COLUMN ROW --reason TEXT [--only 'the secret part'] [--actor A-0001]   "
          "(ROW: an id, a code like ISS-0001, or R-0001:SEC-H1 for a finding)")
def cmd_redact(ctx):
    a, con = ctx.a, ctx.con
    table, sep, column = (a.field or "").partition(".")
    if not sep or not table or not column:
        raise WriteError(f"{a.field!r}: name the field as TABLE.COLUMN (findings.summary)")
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    if table not in tables:
        raise WriteError(f"no table {table!r} in the ledger")
    info = {r[1]: {"type": r[2], "pk": r[5]} for r in con.execute(f'PRAGMA table_info("{table}")')}
    if column not in info:
        raise WriteError(f"no column {column!r} in {table}")
    if not _redactable(con, table, column, info):
        raise WriteError(f"{table}.{column} is not a text column that can be redacted (only free text: summaries, "
                         f"notes, excerpts, answers)")
    reason = _need(_text_arg(a.reason, "--reason"), "--reason (why the text is redacted; never the value)")
    rowid = _redact_row(con, table, info, a.row)
    value = con.execute(f'SELECT "{column}" FROM "{table}" WHERE rowid=?', (rowid,)).fetchone()[0]
    if not isinstance(value, str) or not value.strip():
        raise WriteError(f"{table}.{column} of {a.row} is empty; nothing to redact")
    secret = value if a.only is None else a.only
    if a.only is not None and a.only not in value:
        raise WriteError(f"the text given with --only is not in {table}.{column} of {a.row}")
    if len(secret.strip()) < 6:
        raise WriteError("the text to redact is shorter than 6 characters: too short to find its copies safely")
    if secret in reason:
        raise WriteError("the --reason holds the redacted text; say why it is redacted, never the value")
    # every copy: any text cell of any table holding the secret (events' values and notes, issue titles, ...)
    hits = {}
    for t in tables:
        for c in _table_columns(con, t):
            for (rid,) in con.execute(f'SELECT rowid FROM "{t}" WHERE typeof("{c}")=\'text\' AND instr("{c}", ?) > 0',
                                      (secret,)):
                hits.setdefault(t, []).append((c, rid))
    # the append-only guards (and every other trigger) of those tables are lifted for these updates only: dropped
    # and recreated from their own text in this one transaction, so the shipped schema is back before COMMIT
    triggers = rows(con, "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN (%s) ORDER BY name"
                    % ",".join("?" for _ in hits), list(hits))
    for tr in triggers:
        ctx.x(f'DROP TRIGGER "{tr["name"]}"')
    for t, cells in hits.items():
        for c, rid in cells:
            ctx.x(f'UPDATE "{t}" SET "{c}" = replace("{c}", ?, ?) WHERE rowid=?', (secret, REDACTED, rid))
    for tr in triggers:
        ctx.x(tr["sql"])
    entity = REDACT_ENTITIES.get(table)
    row = one(con, f'SELECT * FROM "{table}" WHERE rowid=?', (rowid,))
    review_id = row.get("review_id")
    if table == "events":
        entity, eid = row["entity_type"], row["entity_id"]
    elif table == "finding_locations":
        entity, eid = "finding", row["finding_id"]
    elif entity:
        eid = rowid
    else:
        for col, ent in (("finding_id", "finding"), ("issue_id", "issue"), ("step_id", "step"),
                         ("review_id", "review"), ("first_review_id", "review"), ("plan_id", "plan")):
            if row.get(col) is not None:
                entity, eid = ent, row[col]
                break
        else:
            raise WriteError(f"{table} rows have no history entry to log a redaction against")
    copies = sum(len(v) for v in hits.values()) - 1
    where = ", ".join(f"{t} ({len(v)})" for t, v in sorted(hits.items()))
    ctx.x("""INSERT INTO events(entity_type, entity_id, event, to_value, note, actor_id, review_id)
             VALUES (?, ?, 'redacted', ?, ?, ?, ?)""",
          (entity, eid, f"{table}.{column}",
           f"{table}.{column} of {a.row} redacted{' in part' if a.only is not None else ''}, with {copies} copies "
           f"(cells by table: {where}): {reason}", ctx.actor_id, review_id))
    con.execute("PRAGMA secure_delete = ON")
    ctx.after.append(lambda: con.execute("PRAGMA wal_checkpoint(TRUNCATE)"))
    out = [f"redacted {table}.{column} of {a.row} and {copies} copies: {where}"]
    if Path(ctx.db).with_suffix(".sql").exists():
        out.append("run `ledger.py dump` now: the old ledger.sql still holds the text")
    out.append("a secret that reached git (a commit, a pushed dump) stays in its history: rotate it")
    return out


def add_write_commands(sub):
    """The write commands' subparsers (each one's --help gives its exact usage line)."""
    def p(name, *args, **kw):
        f = WRITE_COMMANDS.get(name) or READ_COMMANDS[name]
        s = sub.add_parser(name, help=f.cli[0], description=f.cli[0], usage=f.cli[1])
        if name not in READ_COMMANDS:
            s.add_argument("--actor", help="who makes this change (A-0001, an actor id, or a unique actor name)")
        return s

    s = p("next-code"); s.add_argument("prefix", help="ISS, R, P, D, GATE, REQ, G or IQ")
    s = p("add-repo"); s.add_argument("name"); s.add_argument("path", help="relative to the project root ('.')")
    s.add_argument("--remote"); s.add_argument("--default-branch"); s.add_argument("--permalink-template")
    s.add_argument("--outside-root", action="store_true", help="the owner listed it as living outside the root")
    s = p("add-actor"); s.add_argument("kind", help=", ".join(ACTOR_KINDS)); s.add_argument("name")
    for o in ("--model", "--harness", "--session", "--parent", "--review", "--role"):
        s.add_argument(o)
    s = p("new-review"); s.add_argument("--title"); s.add_argument("--scope", default="full")
    s.add_argument("--repo", action="append", help="NAME or NAME@COMMIT (default: every repository at HEAD)")
    s.add_argument("--domain", action="append", help="CODE or CODE=reason (proposed, included)")
    s.add_argument("--skip", action="append", help="CODE=reason (skipped)")
    s.add_argument("--goal", action="append"); s.add_argument("--request"); s.add_argument("--previous")
    s.add_argument("--manager")
    s = p("review-status"); s.add_argument("review"); s.add_argument("status")
    s.add_argument("--note"); s.add_argument("--approved-by")
    s = p("snapshot"); s.add_argument("review"); s.add_argument("--repo", action="append")
    s = p("add-run"); s.add_argument("review"); s.add_argument("domain"); s.add_argument("label")
    s.add_argument("--scope"); s.add_argument("--reviewer"); s.add_argument("--model"); s.add_argument("--prompt")
    s.add_argument("--status", default="queued")
    s = p("issue-status"); s.add_argument("issue"); s.add_argument("status")
    for o in ("--note", "--review", "--step", "--plan", "--commit", "--after", "--resolution"):
        s.add_argument(o)
    s = p("step-status"); s.add_argument("step"); s.add_argument("status")
    for o in ("--note", "--question", "--progress-note", "--commit", "--plan", "--repo"):
        s.add_argument(o)
    s.add_argument("--guard", action="append", help="with done: PATH::NAME (each issue of the step) or "
                                                    "ISS-0001=PATH::NAME, a guard test the step added")
    s.add_argument("--failed-before", action="store_true", help="the --guard tests failed before the fix")
    s = p("import-steps"); s.add_argument("plan"); s.add_argument("file", help="JSON lines, or - for standard input")
    s = p("open-gate"); s.add_argument("kind")
    for o in ("--title", "--question", "--context", "--options-json", "--review", "--plan", "--step", "--issue",
              "--decision"):
        s.add_argument(o)
    s.add_argument("--option", action="append", help="value=label or value=label:description")
    s.add_argument("--no-text", action="store_true", help="the owner must choose an option")
    s.add_argument("--non-blocking", action="store_true")
    s = p("answer-gate"); s.add_argument("gate"); s.add_argument("--value"); s.add_argument("--text")
    s.add_argument("--via", default="chat", choices=("chat", "other")); s.add_argument("--by")
    s = p("apply-gate"); s.add_argument("gate"); s.add_argument("--note"); s.add_argument("--revisit-when")
    s = p("interview-ask"); s.add_argument("question", nargs="?"); s.add_argument("--review"); s.add_argument("--domain")
    s.add_argument("--why", help="what in the code prompted the question")
    s.add_argument("--gate", action="store_true", help="ask it in the hub too (a gate whose answer is recorded)")
    s = p("interview-answer"); s.add_argument("question"); s.add_argument("answer", nargs="?")
    s.add_argument("--via", default="chat", help="chat or viewer")
    s = p("propose-goal"); s.add_argument("statement", nargs="?"); s.add_argument("--domain", help="omit: whole project")
    s.add_argument("--from", dest="sources", help="the interview questions it summarizes: IQ-0001,IQ-0003")
    s.add_argument("--source", help="where a goal stated in a document comes from (a path, 'owner, chat <date>')")
    s.add_argument("--review"); s.add_argument("--gate", action="store_true",
                                               help="ask for the owner's approval in the hub (one confirm gate)")
    s = p("approve-goals"); s.add_argument("goals", nargs="+")
    s = p("reject-goal"); s.add_argument("goal"); s.add_argument("--note")
    s = p("link-step-goals"); s.add_argument("step"); s.add_argument("goals", nargs="+"); s.add_argument("--plan")
    s = p("goals"); s.add_argument("--domain"); s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    s = p("interview"); s.add_argument("--review"); s.add_argument("--domain"); s.add_argument("--json", action="store_true")
    s = p("import-findings"); s.add_argument("review"); s.add_argument("run"); s.add_argument("file")
    s = p("import-checks"); s.add_argument("review"); s.add_argument("file"); s.add_argument("--run")
    s = p("load-index"); s.add_argument("review"); s.add_argument("run"); s.add_argument("points")
    s.add_argument("known", nargs="?"); s.add_argument("--trace")
    s = p("match"); s.add_argument("review"); s.add_argument("point", nargs="?"); s.add_argument("target", nargs="?")
    s.add_argument("--type"); s.add_argument("--confidence", default="likely"); s.add_argument("--rationale")
    s.add_argument("--relations"); s.add_argument("--file")
    s = p("report-finding")
    for o in ("--step", "--severity", "--domain", "--summary", "--before", "--fix", "--test", "--kind", "--theme",
              "--sub", "--commit", "--review", "--plan", "--issue", "--into-step", "--new-step", "--change",
              "--proof"):
        s.add_argument(o)
    s.add_argument("--where", action="append", default=[], help="[repo:]path[:start[-end]][#symbol], at --commit")
    s.add_argument("--phase", type=int); s.add_argument("--new-issue", action="store_true")
    s.add_argument("--gate", action="store_true", help="ask the owner where to plan it (the default)")
    add_write_commands_131(p)


def add_write_commands_131(p):
    """The subparsers of the step 131-132 writes (new-plan ... redact); p is add_write_commands' maker."""
    s = p("new-plan"); s.add_argument("--title"); s.add_argument("--review", action="append")
    s = p("plan-status"); s.add_argument("plan"); s.add_argument("status"); s.add_argument("--note")
    s.add_argument("--approved-by"); s.add_argument("--superseded-by")
    s = p("add-requirement"); s.add_argument("text", nargs="?"); s.add_argument("--source")
    s.add_argument("--location"); s.add_argument("--stated-at"); s.add_argument("--review")
    s = p("add-domain"); s.add_argument("code"); s.add_argument("name"); s.add_argument("--description")
    s.add_argument("--report-file"); s.add_argument("--category", default="custom")
    s.add_argument("--theme", help="CODE=Name (added when the catalog lacks it)")
    s = p("review-domain"); s.add_argument("review"); s.add_argument("domain")
    s.add_argument("decision", help="include or skip"); s.add_argument("--reason")
    s.add_argument("--owner-change", help=", ".join(OWNER_CHANGES)); s.add_argument("--reviewers", type=int)
    s.add_argument("--split-note")
    s = p("finish-run"); s.add_argument("review"); s.add_argument("label")
    s.add_argument("--status", default="finished"); s.add_argument("--report"); s.add_argument("--message")
    for o in ("--items", "--high", "--medium", "--low", "--tests"):
        s.add_argument(o, type=int)
    s = p("verify"); s.add_argument("point", help="R-0001:SEC-M1, or SEC-M1 with --review")
    s.add_argument("verdict", help=", ".join(VERIFICATIONS)); s.add_argument("--note"); s.add_argument("--review")
    s = p("record-test-run")
    for o in ("--purpose", "--command", "--review", "--step", "--plan", "--repo", "--commit", "--output", "--note"):
        s.add_argument(o)
    for o in ("--passed", "--failed", "--skipped", "--errored", "--exit-code", "--duration-ms"):
        s.add_argument(o, type=int)
    s = p("add-commit"); s.add_argument("step"); s.add_argument("commit", help="a full hash, or any git ref")
    for o in ("--plan", "--repo", "--pr", "--summary"):
        s.add_argument(o)
    s = p("add-guard"); s.add_argument("issue"); s.add_argument("test_path"); s.add_argument("test_name")
    s.add_argument("--failed-before", action="store_true", help="it failed before the fix")
    for o in ("--step", "--plan", "--commit", "--repo"):
        s.add_argument(o)
    s = p("confirm-guard"); s.add_argument("review"); s.add_argument("issues", nargs="+")
    s.add_argument("--test", help="PATH::NAME (default: every guard test of the issue)")
    s.add_argument("--gone", action="store_true", help="the test no longer exists or no longer guards the issue")
    s = p("set-verdict"); s.add_argument("review"); s.add_argument("--verdict"); s.add_argument("--summary")
    s = p("set-doc-url"); s.add_argument("code", help="R-0001 or P-0001"); s.add_argument("url")
    s = p("plan-add-review"); s.add_argument("plan"); s.add_argument("review")
    s = p("add-metric"); s.add_argument("review"); s.add_argument("name"); s.add_argument("value")
    for o in ("--method", "--domain", "--repo", "--scope", "--unit", "--note"):
        s.add_argument(o)
    s = p("add-artifact"); s.add_argument("kind", help=", ".join(ARTIFACT_KINDS))
    for o in ("--path", "--url", "--review", "--plan", "--run"):
        s.add_argument(o)
    s = p("redact"); s.add_argument("field", help="TABLE.COLUMN, e.g. findings.summary")
    s.add_argument("row", help="a row id, a code (ISS-0001), or R-0001:SEC-H1 for a finding")
    s.add_argument("--reason"); s.add_argument("--only", help="redact only this part of the text (the secret)")


# ----- the reviewed text of a dirty working tree (D-14, used by fill_refs)

def working_tree_bytes(root, con, loc, committed):
    """The file's bytes in the working tree when the location's review recorded that repository dirty (is_dirty) at
    the location's commit and the file there differs from the commit; else None (read the commit). The reviewers
    saw the working tree, so its text is what the excerpt should hold (finding_locations.source 'working-tree')."""
    try:
        rr = con.execute("""SELECT rr.is_dirty, rr.commit_sha FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
                            JOIN review_repositories rr ON rr.review_id = f.review_id
                             AND rr.repository_id = fl.repository_id WHERE fl.id=?""", (loc["id"],)).fetchone()
    except sqlite3.Error:
        return None
    if not rr or not rr[0] or rr[1] != loc["commit_sha"]:
        return None
    try:
        d = repo_dir(root, loc["repo_path"], bool(loc.get("allow_outside_root")))
        f = (d / loc["path"]).resolve()
        if d.resolve() not in f.parents or not f.is_file():
            return None
        data = f.read_bytes()
    except (OSError, RuntimeError):
        return None
    return data if data != committed else None


# ---------------------------------------------------------------- CLI

_MAIN_CONNECTIONS = []  # the ledger connection main() opened, closed on every way out (FIX-4)

# C0 controls except tab and newline, DEL, and the C1 controls (U+0080-U+009F, among them the one-character CSI U+009B).
_TERMINAL_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def terminal_safe(text):
    """The one sanitizer for text the CLI prints (step 118). The ledger may have been written by someone else, so a
    stored title, summary, answer or cited line could carry terminal escapes that clear the screen, retitle the
    window or hide text. Every control character but newline and tab becomes a visible \\u00XX escape (valid inside
    the JSON `query --json` prints, unambiguous in its TSV, where a literal backslash is doubled)."""
    return _TERMINAL_CONTROLS.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


class _TerminalSafeStream:
    """A text stream that writes through terminal_safe(): main() puts one over stdout and stderr, so every command's
    output (show, gates, check, query, progress-md, the write commands' messages and refusals) is sanitized in one
    place, whatever prints it."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(terminal_safe(text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _discard_stdout(out):
    """Point the process's standard output at the null device, so nothing written or flushed after the reader
    closed raises again."""
    try:
        fd = out.fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)
    except (OSError, ValueError, AttributeError):  # not a real file (a test's StringIO)
        pass


def main():
    """The CLI. Whatever way a command ends (return, sys.exit, an exception), the ledger connection it opened is
    closed before main() returns or raises. Everything it prints goes through terminal_safe(), the message of a
    sys.exit("...") included (the interpreter prints that one after main() has returned). When the reader of its
    output closes early (`| head`), it ends quietly with exit 0."""
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _TerminalSafeStream(out), _TerminalSafeStream(err)
    try:
        try:
            result = _main()
            out.flush()  # here, so a closed reader is noticed now and not when the interpreter exits
            return result
        except SystemExit:
            out.flush()
            raise
    except BrokenPipeError:
        _discard_stdout(out)  # the reader closed early (`ledger.py gates | head`): end quietly
        return None
    except SystemExit as e:
        if isinstance(e.code, str):
            raise SystemExit(terminal_safe(e.code)) from None
        raise
    finally:
        sys.stdout, sys.stderr = out, err
        while _MAIN_CONNECTIONS:
            try:
                _MAIN_CONNECTIONS.pop().close()
            except sqlite3.Error:
                pass


def _main():
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
    s.add_argument("--quiet", action="store_true", help="print only failures and warnings (no 'ok' or zero lines)")
    s = sub.add_parser("fill-refs"); s.add_argument("review", nargs="?")
    s = sub.add_parser("tag-review"); s.add_argument("review")
    s.add_argument("--pushed", action="store_true",
                   help="record that the review's tags were pushed (after git push origin review/R-0001)")
    s.add_argument("--actor", help="who records the push (A-0001)")
    s = sub.add_parser("set-report", help="store the review document (Markdown) shown in the viewer")
    s.add_argument("review"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("set-narrative", help="store the plan's narrative (Markdown) shown in the viewer")
    s.add_argument("plan"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("show"); s.add_argument("location", type=int)
    sub.add_parser("gates", help="the owner's queue: open and answered gates, waiting steps, open decisions, "
                                 "stale steps ('no open gates' when there are none)")
    add_tool_parsers(sub)
    add_write_commands(sub)
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
        if old_sqlite_note():
            print(old_sqlite_note(), file=sys.stderr)
        return
    if a.cmd == "migrate":
        migrate(db); return
    if a.cmd in TOOL_COMMANDS:
        sys.exit(tool_command(a, root, db))
    # Writes refuse an older ledger; serve, gates and the writes also refuse a schema that differs from the shipped
    # one (a forged trigger could answer gates). show and dump still read it, for diagnosis; check reports it.
    writes = a.cmd in WRITE_COMMANDS or a.cmd in READ_COMMANDS  # the write commands (and next-code)
    con = open_ledger(db, write=writes or a.cmd in ("serve", "fill-refs", "tag-review", "set-report", "set-narrative"),
                      verify=writes or a.cmd in ("serve", "gates", "fill-refs", "tag-review", "set-report",
                                                 "set-narrative"))
    _MAIN_CONNECTIONS.append(con)
    if writes:
        sys.exit(run_write_command(con, root, db, a))
    if a.cmd == "serve":
        con.close(); serve(root, db, a.port, not a.no_open)
    elif a.cmd == "check":
        was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
        if was and was[0] != str(db.parent):
            print(f"warning: this ledger was set up at {was[0]} and is now used from {db.parent} (fine after a move; "
                  f"init records the new place)", file=sys.stderr)
        bad = check(con, quiet=a.quiet, full=a.full)
        bad += check_git_refs(root, con, quiet=a.quiet)
        if old_sqlite_note():
            print(old_sqlite_note(), file=sys.stderr)
        sys.exit(1 if bad else 0)
    elif a.cmd == "fill-refs":
        sys.exit(1 if fill_refs(root, con, a.review) else 0)
    elif a.cmd == "tag-review" and a.pushed:
        sys.exit(run_write_command(con, root, db, a, cmd_tag_review_pushed))
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
        print_gates(con)
    elif a.cmd == "dump":
        dump(con, db)


if __name__ == "__main__":
    main()
