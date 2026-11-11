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
