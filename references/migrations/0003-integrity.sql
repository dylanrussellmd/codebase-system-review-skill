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
