-- A schema-1 ledger made by ledger.py at commit ea00196 and filled with the demo data.
-- Frozen: regenerate only with python3 -m dev.tests._migrate_helpers --write-v1-demo
BEGIN TRANSACTION;
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
INSERT INTO "actors" VALUES(1,'owner','Demo Owner',NULL,NULL,NULL,NULL,'2026-11-01T09:01:00.000Z');
INSERT INTO "actors" VALUES(2,'manager','Review manager','model-a','Harness A',NULL,NULL,'2026-11-01T09:02:00.000Z');
INSERT INTO "actors" VALUES(3,'reviewer','security reviewer','model-a',NULL,NULL,2,'2026-11-01T09:03:00.000Z');
INSERT INTO "actors" VALUES(4,'reviewer','spec reviewer','model-a',NULL,NULL,2,'2026-11-01T09:04:00.000Z');
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
CREATE TABLE decision_issues (
  decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  issue_id    INTEGER NOT NULL REFERENCES issues(id),
  PRIMARY KEY (decision_id, issue_id)
);
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
INSERT INTO "decisions" VALUES(1,'D-0001','Should the default notice period stay 14 days?',NULL,NULL,NULL,'open',NULL,1,3,2,NULL,'2026-11-01T09:23:00.000Z',NULL);
CREATE TABLE design_goals (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE,    -- G-01
  text          TEXT NOT NULL,
  source        TEXT,                    -- where it came from (doc path, "owner, 2026-11-10")
  adopted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  retired_at    TEXT,
  superseded_by INTEGER REFERENCES design_goals(id)
);
INSERT INTO "design_goals" VALUES(1,'G-01','Every rule and behavior is a setting.','owner','2026-11-01T09:05:00.000Z',NULL,NULL);
INSERT INTO "design_goals" VALUES(2,'G-02','No internal names on screen.','owner','2026-11-01T09:06:00.000Z',NULL,NULL);
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
INSERT INTO "domains" VALUES('spec','Spec conformance','correctness',NULL,'spec.md',NULL);
INSERT INTO "domains" VALUES('tests','Test coverage','correctness',NULL,'tests-<module>.md',NULL);
INSERT INTO "domains" VALUES('test-quality','Test suite quality','correctness',NULL,'test-quality.md',NULL);
INSERT INTO "domains" VALUES('time-concurrency','Time, dates and concurrency','correctness',NULL,'time-concurrency.md',NULL);
INSERT INTO "domains" VALUES('resilience','Failure handling and resilience','correctness',NULL,'resilience.md',NULL);
INSERT INTO "domains" VALUES('compatibility','Backward compatibility and migrations','correctness',NULL,'compatibility.md',NULL);
INSERT INTO "domains" VALUES('decision-placement','Decision placement','architecture',NULL,'decision-placement.md',NULL);
INSERT INTO "domains" VALUES('contracts','Internal contracts','architecture',NULL,'contracts.md',NULL);
INSERT INTO "domains" VALUES('server','Backend / server','architecture',NULL,'server.md',NULL);
INSERT INTO "domains" VALUES('database','Database','architecture',NULL,'database.md',NULL);
INSERT INTO "domains" VALUES('client-api','Backend-to-client API','architecture',NULL,'client-api.md',NULL);
INSERT INTO "domains" VALUES('abstraction','Abstraction and shared code','architecture',NULL,'abstraction.md',NULL);
INSERT INTO "domains" VALUES('hotspots','Hotspots from history','architecture',NULL,'hotspots.md',NULL);
INSERT INTO "domains" VALUES('ui','Front end / UI','user-facing',NULL,'ui-<area>.md',NULL);
INSERT INTO "domains" VALUES('walkthroughs','Task walkthroughs','user-facing',NULL,'walkthroughs.md',NULL);
INSERT INTO "domains" VALUES('notifications','Email and notifications','user-facing',NULL,'notifications.md',NULL);
INSERT INTO "domains" VALUES('localization','Localization','user-facing',NULL,'localization.md',NULL);
INSERT INTO "domains" VALUES('security','Security','security-operations',NULL,'security.md',NULL);
INSERT INTO "domains" VALUES('privacy','Privacy and data map','security-operations',NULL,'privacy.md',NULL);
INSERT INTO "domains" VALUES('logging','Logging','security-operations',NULL,'logging.md',NULL);
INSERT INTO "domains" VALUES('operations','Operations, CI/CD and recovery','security-operations',NULL,'operations.md',NULL);
INSERT INTO "domains" VALUES('dependencies','Dependencies and supply chain','security-operations',NULL,'dependencies.md',NULL);
INSERT INTO "domains" VALUES('performance','Performance','security-operations',NULL,'performance.md',NULL);
INSERT INTO "domains" VALUES('cost','Cost','security-operations',NULL,'cost.md',NULL);
INSERT INTO "domains" VALUES('docs-developer','Developer documentation','documentation',NULL,'docs-developer.md',NULL);
INSERT INTO "domains" VALUES('docs-user','End-user documentation','documentation',NULL,'docs-user.md',NULL);
INSERT INTO "domains" VALUES('handover','Maintainability and handover','documentation',NULL,'handover.md',NULL);
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
INSERT INTO "events" VALUES(1,'review',1,'status-changed','planning','handed-off',NULL,1,NULL,NULL,NULL,'2026-11-11T05:25:14.075Z');
INSERT INTO "events" VALUES(2,'issue',1,'opened',NULL,'open',2,1,NULL,NULL,NULL,'2026-11-11T05:25:14.076Z');
INSERT INTO "events" VALUES(3,'issue',1,'seen',NULL,'new',NULL,1,NULL,NULL,'SEC-H1','2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(4,'issue',2,'opened',NULL,'open',2,1,NULL,NULL,NULL,'2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(5,'issue',2,'seen',NULL,'new',NULL,1,NULL,NULL,'SPEC-M1','2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(6,'issue',3,'opened',NULL,'open',2,1,NULL,NULL,NULL,'2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(7,'issue',3,'seen',NULL,'new',NULL,1,NULL,NULL,'SPEC-L1','2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(8,'plan',1,'status-changed','drafting','in-progress',NULL,NULL,NULL,NULL,NULL,'2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(9,'step',2,'status-changed','not-started','doing',2,NULL,2,NULL,NULL,'2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(10,'step',2,'status-changed','doing','done',2,NULL,2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','Sign-in codes now come from a secure generator.','2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(11,'issue',1,'status-changed','open','fixed',2,1,2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','Step 2 done','2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(12,'step',3,'status-changed','not-started','waiting-owner',NULL,NULL,3,NULL,NULL,'2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(13,'review',2,'status-changed','planning','awaiting-approval',NULL,2,NULL,NULL,NULL,'2026-11-11T05:25:14.079Z');
INSERT INTO "events" VALUES(14,'issue',1,'status-changed','fixed','verified',3,2,NULL,NULL,'review check: fixed — make_code now uses secrets.randbelow (src/auth.py:3)','2026-11-11T05:25:14.079Z');
INSERT INTO "events" VALUES(15,'issue',2,'seen',NULL,'still-present',NULL,2,NULL,NULL,'SPEC-M1','2026-11-11T05:25:14.079Z');
INSERT INTO "events" VALUES(16,'issue',4,'opened',NULL,'open',2,2,NULL,NULL,NULL,'2026-11-11T05:25:14.084Z');
INSERT INTO "events" VALUES(17,'issue',4,'seen',NULL,'new',NULL,2,NULL,NULL,'SEC-M1','2026-11-11T05:25:14.084Z');
CREATE TABLE finding_goals (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);
CREATE TABLE finding_locations (
  id             INTEGER PRIMARY KEY,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  repository_id  INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR (length(commit_sha) >= 7 AND commit_sha NOT GLOB '*[^0-9a-f]*')),
                                            -- filled from review_repositories if left NULL
  path           TEXT NOT NULL,             -- repo-relative, forward slashes
  line_start     INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end       INTEGER,
  symbol         TEXT,                      -- function, class, route, setting name
  blob_sha       TEXT,                      -- `git rev-parse <commit>:<path>`
  excerpt        TEXT,                      -- the cited lines, verbatim, at that commit
  excerpt_sha256 TEXT,                      -- hash of excerpt, to detect the same code later
  permalink      TEXT,                      -- e.g. https://github.com/o/r/blob/<sha>/<path>#L10-L20
  role           TEXT NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','also','test','doc','config')),
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start)
);
INSERT INTO "finding_locations" VALUES(1,1,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','src/auth.py',1,3,'make_code','1baf0b7838848c9d10acbd97f658d4216ef3cb33','import random
def make_code():
    return f"{random.randint(0, 999999):06d}"','60ac807b60625602459422c3fc375fe38d986b39458dd1780bdd9e4608b4aecb','https://github.com/example/demo/blob/16822abc0b87e79b664c98f386b3354147e0f1bc/src/auth.py#L1-L3','primary');
INSERT INTO "finding_locations" VALUES(2,2,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','src/timeoff.py',2,4,'can_request','168c600dc4f8f17a3edb63936f393a3420163fc8','def can_request(user, days_ahead, weekend):
    if days_ahead < NOTICE_DAYS:
        return False','e744180489f92abed87806426451d36a4e699caef1a83537e1424b902378d881','https://github.com/example/demo/blob/16822abc0b87e79b664c98f386b3354147e0f1bc/src/timeoff.py#L2-L4','primary');
INSERT INTO "finding_locations" VALUES(3,3,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','src/timeoff.py',5,6,'can_request','168c600dc4f8f17a3edb63936f393a3420163fc8','    if user.is_admin:
        return True','8d85823055eb5d195db34524855c0e47d1f803c5f2b1b1ebc416bd5ae8dbb9b9','https://github.com/example/demo/blob/16822abc0b87e79b664c98f386b3354147e0f1bc/src/timeoff.py#L5-L6','primary');
INSERT INTO "finding_locations" VALUES(4,4,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','src/timeoff.py',2,4,'can_request','168c600dc4f8f17a3edb63936f393a3420163fc8','def can_request(user, days_ahead, weekend):
    if days_ahead < NOTICE_DAYS:
        return False','e744180489f92abed87806426451d36a4e699caef1a83537e1424b902378d881','https://github.com/example/demo/blob/4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee/src/timeoff.py#L2-L4','primary');
INSERT INTO "finding_locations" VALUES(5,5,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','src/auth.py',4,5,'check','a03d48405198b5b43ac074bb87df9678b441c06e','def check(code, expected):
    return code == expected','124fc58f514b796cceae4dbae31ce6e721561c0f4bc9238e3fa2cf77fc4a03d8','https://github.com/example/demo/blob/4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee/src/auth.py#L4-L5','primary');
CREATE TABLE finding_requirements (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);
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
INSERT INTO "findings" VALUES(1,1,1,'SEC-H1',1,'new','certain',NULL,'H','security','security','sign-in','code-randomness','Sign-in codes come from a predictable random generator.',NULL,'make_code uses random.randint, which is not suitable for secrets.','Use secrets.randbelow.','test_code_randomness',NULL,NULL,'security:sign-in:code-randomness',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:11:00.000Z');
INSERT INTO "findings" VALUES(2,1,2,'SPEC-M1',2,'new','certain',NULL,'M','defect','spec','spec-mismatch','notice-period','Notice period is hardcoded to 14 days instead of a setting.',NULL,'can_request refuses anything under 14 days; the owner asked for it to be a setting.','Read the notice period from program settings.','test_notice_period',NULL,NULL,'spec:spec-mismatch:notice-period',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:12:00.000Z');
INSERT INTO "findings" VALUES(3,1,2,'SPEC-L1',3,'new','certain',NULL,'L','defect','spec','spec-mismatch','admin-bypass','Admins skip the weekend limit entirely.',NULL,'Admins bypass the weekend-off rule.','Apply the same rule with an override that is recorded.','test_admin_bypass',NULL,NULL,'spec:spec-mismatch:admin-bypass',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:13:00.000Z');
INSERT INTO "findings" VALUES(4,2,4,'SPEC-M1',2,'still-present','certain','Same function and lines as ISS-0002','M','defect','spec','spec-mismatch','notice-period','Notice period is still 14 days in code.',NULL,'Unchanged since R-0001.','Read it from settings.','test_notice_period',NULL,NULL,'spec:spec-mismatch:notice-period',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:30:00.000Z');
INSERT INTO "findings" VALUES(5,2,3,'SEC-M1',4,'new','certain',NULL,'M','security','security','sign-in','code-compare','Code check uses == instead of a constant-time compare.',NULL,'check compares codes with ==.','Use hmac.compare_digest.','test_code_compare',NULL,NULL,'security:sign-in:code-compare',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:32:00.000Z');
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
INSERT INTO "gates" VALUES(1,'GATE-0001','decision','Default notice period','Should the default notice period stay 14 days when it becomes a setting?','Today the code refuses requests made less than 14 days ahead. Programs will be able to change it.','[{"value": "keep-14", "label": "Keep 14 days", "description": "Current behaviour stays the default."}, {"value": "use-7", "label": "Change to 7 days"}]',1,1,'open',NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,1,3,2,1,2,'2026-11-01T09:24:00.000Z');
INSERT INTO "gates" VALUES(2,'GATE-0002','domain-approval','Approve the domains for R-0002','Run these 2 domains with 2 reviewers?','Included: Security, Spec conformance. Skipped: Database (no database).','[{"value": "approve", "label": "Approve as listed"}, {"value": "add", "label": "Add skipped domains"}]',1,1,'open',NULL,NULL,NULL,NULL,NULL,NULL,NULL,2,NULL,NULL,NULL,NULL,2,'2026-11-01T09:36:00.000Z');
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
INSERT INTO "guard_tests" VALUES(1,1,1,'tests/test_auth.py','test_code_uses_secrets',2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee',1,1,1,NULL,'2026-11-01T09:22:00.000Z');
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
INSERT INTO "issue_relations" VALUES(1,4,1,'related-to','likely','Same sign-in code path as the earlier randomness issue',2,2,'2026-11-01T09:34:00.000Z');
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
INSERT INTO "issues" VALUES(1,'ISS-0001','Predictable sign-in codes','security','sign-in','code-randomness','security','H','verified','security:sign-in:code-randomness','Codes generated with random.randint.','make_code uses random.randint, which is not suitable for secrets.','Codes come from secrets.randbelow.',NULL,NULL,1,1,2,1,'2026-11-01T09:14:00.000Z','2026-11-11T05:25:14.078Z','2026-11-11T05:25:14.079Z','2026-11-11T05:25:14.079Z',3,2,NULL,NULL,'review check: fixed — make_code now uses secrets.randbelow (src/auth.py:3)');
INSERT INTO "issues" VALUES(2,'ISS-0002','Notice period hardcoded','spec','spec-mismatch','notice-period','defect','M','open','spec:spec-mismatch:notice-period','14 days in code.','Unchanged since R-0001.',NULL,NULL,NULL,1,2,2,2,'2026-11-01T09:15:00.000Z',NULL,NULL,'2026-11-11T05:25:14.079Z',4,2,NULL,NULL,'review check: still-present');
INSERT INTO "issues" VALUES(3,'ISS-0003','Admins bypass the weekend limit','spec','spec-mismatch','admin-bypass','defect','L','open','spec:spec-mismatch:admin-bypass','Admin bypass.','Admins bypass the weekend-off rule.',NULL,NULL,NULL,1,1,2,1,'2026-11-01T09:16:00.000Z',NULL,NULL,'2026-11-11T05:25:14.084Z',4,2,NULL,NULL,'review check: not-checked');
INSERT INTO "issues" VALUES(4,'ISS-0004','Sign-in code compare is not constant-time','security','sign-in','code-compare','security','M','open','security:sign-in:code-compare','== compare.','check compares codes with ==.',NULL,NULL,NULL,2,2,2,1,'2026-11-01T09:33:00.000Z',NULL,NULL,'2026-11-11T05:25:14.084Z',NULL,2,NULL,NULL,'matched by finding SEC-M1');
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,                -- schema_version, project_name, created_at, created_by_skill
  value TEXT NOT NULL
);
INSERT INTO "meta" VALUES('schema_version','1');
INSERT INTO "meta" VALUES('project_name','demo-v1');
INSERT INTO "meta" VALUES('created_at','2026-11-01T09:00:00.000Z');
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
INSERT INTO "metrics" VALUES(1,1,'test-quality',NULL,'mutation_score','src',61.0,'%','measured',NULL,'2026-11-01T09:37:00.000Z');
INSERT INTO "metrics" VALUES(2,2,'test-quality',NULL,'mutation_score','src',74.5,'%','measured',NULL,'2026-11-01T09:38:00.000Z');
CREATE TABLE phases (
  id      INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  number  INTEGER NOT NULL,
  title   TEXT NOT NULL,
  goal    TEXT NOT NULL,                     -- one sentence
  why     TEXT,                              -- why it sits where it does
  UNIQUE (plan_id, number)
);
INSERT INTO "phases" VALUES(1,1,1,'Safety net','Every later step is checked by tests.',NULL);
INSERT INTO "phases" VALUES(2,1,2,'Fix first','Confirmed defects fixed with failing tests first.',NULL);
CREATE TABLE plan_reviews (
  plan_id   INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  PRIMARY KEY (plan_id, review_id)
);
INSERT INTO "plan_reviews" VALUES(1,1);
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
INSERT INTO "plans" VALUES(1,'P-0001','Fix plan for R-0001','in-progress',1,'2026-11-01T09:17:00.000Z',NULL,NULL,NULL,'2026-11-01T09:18:00.000Z','2026-11-11T05:25:14.078Z');
CREATE TABLE repositories (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,   -- short name used in the brief
  path           TEXT,                   -- relative to the project root, '.' for the root repo
  remote_url     TEXT,
  default_branch TEXT,
  permalink_template TEXT,               -- e.g. https://github.com/o/r/blob/{sha}/{path}#L{start}-L{end}
  notes          TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "repositories" VALUES(1,'demo','.',NULL,'main','https://github.com/example/demo/blob/{sha}/{path}#L{start}-L{end}',NULL,'2026-11-01T09:00:00.000Z');
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
INSERT INTO "review_domains" VALUES(1,'security','included','Sign-in codes and sessions exist','none',1,NULL);
INSERT INTO "review_domains" VALUES(1,'spec','included','Owner rules for time off are stated','none',1,NULL);
INSERT INTO "review_domains" VALUES(1,'database','skipped','No database in this project','none',0,NULL);
INSERT INTO "review_domains" VALUES(2,'security','included','Sign-in codes and sessions exist','none',1,NULL);
INSERT INTO "review_domains" VALUES(2,'spec','included','Owner rules for time off are stated','none',1,NULL);
INSERT INTO "review_domains" VALUES(2,'database','skipped','No database in this project','none',0,NULL);
CREATE TABLE review_goals (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);
INSERT INTO "review_goals" VALUES(1,1);
INSERT INTO "review_goals" VALUES(1,2);
INSERT INTO "review_goals" VALUES(2,1);
INSERT INTO "review_goals" VALUES(2,2);
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
INSERT INTO "review_issue_checks" VALUES(2,1,'fixed',NULL,'make_code now uses secrets.randbelow (src/auth.py:3)',3,'2026-11-01T09:29:00.000Z');
INSERT INTO "review_issue_checks" VALUES(2,2,'still-present',4,NULL,4,'2026-11-01T09:31:00.000Z');
INSERT INTO "review_issue_checks" VALUES(2,3,'not-checked',NULL,NULL,4,'2026-11-01T09:35:00.000Z');
CREATE TABLE review_repositories (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK (length(commit_sha) = 40 OR length(commit_sha) = 64),  -- full hash
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
INSERT INTO "review_repositories" VALUES(1,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','main',NULL,'Initial time off and sign-in','review/R-0001',0,0,NULL,NULL);
INSERT INTO "review_repositories" VALUES(2,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','main',NULL,'Use secrets for sign-in codes','review/R-0002',0,0,NULL,NULL);
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
INSERT INTO "reviewer_runs" VALUES(1,1,'security',3,'security','sign-in',NULL,NULL,NULL,'finished',4,1,0,0,NULL,NULL,'2026-11-01T09:09:00.000Z',NULL);
INSERT INTO "reviewer_runs" VALUES(2,1,'spec',4,'spec','time-off rules',NULL,NULL,NULL,'finished',6,0,1,1,NULL,NULL,'2026-11-01T09:10:00.000Z',NULL);
INSERT INTO "reviewer_runs" VALUES(3,2,'security',3,'security','sign-in',NULL,NULL,NULL,'finished',4,1,0,0,NULL,NULL,'2026-11-01T09:27:00.000Z',NULL);
INSERT INTO "reviewer_runs" VALUES(4,2,'spec',4,'spec','time-off rules',NULL,NULL,NULL,'finished',6,0,1,1,NULL,NULL,'2026-11-01T09:28:00.000Z',NULL);
CREATE TABLE reviews (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE,   -- R-0001
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
  summary_doc_url       TEXT,
  verdict               TEXT,
  summary               TEXT,
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at          TEXT,
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "reviews" VALUES(1,'R-0001','Comprehensive review R-0001','full','handed-off','Run the full system review',1,2,1,'2026-11-01T09:07:00.000Z',NULL,NULL,NULL,NULL,'Solid core; sign-in codes and notice rules need work.',NULL,'2026-11-01T09:08:00.000Z','2026-11-11T05:25:14.075Z','2026-11-11T05:25:14.075Z');
INSERT INTO "reviews" VALUES(2,'R-0002','Comprehensive review R-0002','second','awaiting-approval','Run the full system review',1,2,1,'2026-11-01T09:25:00.000Z',1,NULL,NULL,NULL,'Solid core; sign-in codes and notice rules need work.',NULL,'2026-11-01T09:26:00.000Z',NULL,'2026-11-11T05:25:14.079Z');
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "schema_migrations" VALUES(1,'initial ledger schema','2026-11-11T05:25:14.017Z');
CREATE TABLE step_commits (
  id            INTEGER PRIMARY KEY,
  step_id       INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  repository_id INTEGER REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL,
  branch        TEXT,
  pr_url        TEXT,
  summary       TEXT,
  committed_at  TEXT,
  UNIQUE (step_id, commit_sha)
);
INSERT INTO "step_commits" VALUES(1,2,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','main',NULL,'Use secrets for sign-in codes',NULL);
CREATE TABLE step_dependencies (
  step_id            INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  depends_on_step_id INTEGER NOT NULL REFERENCES steps(id),
  PRIMARY KEY (step_id, depends_on_step_id),
  CHECK (step_id <> depends_on_step_id)
);
INSERT INTO "step_dependencies" VALUES(2,1);
INSERT INTO "step_dependencies" VALUES(3,1);
CREATE TABLE step_findings (
  step_id      INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  finding_id   INTEGER NOT NULL REFERENCES findings(id),
  multi_reason TEXT,          -- required when a finding is closed by more than one step
  PRIMARY KEY (step_id, finding_id)
);
INSERT INTO "step_findings" VALUES(2,1,NULL);
INSERT INTO "step_findings" VALUES(3,2,NULL);
INSERT INTO "step_findings" VALUES(1,3,NULL);
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
INSERT INTO "steps" VALUES(1,1,1,1,'CI','Run every suite in CI','Add a CI workflow.','CI fails on a broken test.','none',NULL,0,NULL,'not-started',NULL,NULL,'2026-11-01T09:19:00.000Z',NULL,'2026-11-01T09:19:00.000Z',NULL,NULL);
INSERT INTO "steps" VALUES(2,1,2,2,NULL,'Use secrets for sign-in codes','Replace random with secrets in make_code.','test_code_uses_secrets fails before, passes after.','none',NULL,0,NULL,'done','Sign-in codes now come from a secure generator.',NULL,'2026-11-01T09:20:00.000Z','2026-11-11T05:25:14.078Z','2026-11-11T05:25:14.078Z',2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee');
INSERT INTO "steps" VALUES(3,1,2,3,NULL,'Make the notice period a setting','Add notice_days setting.','Tests with 7 and 21 days.','decision','Should the default stay 14 days?',1,'Programs can set their own notice.','waiting-owner',NULL,NULL,'2026-11-01T09:21:00.000Z',NULL,'2026-11-11T05:25:14.078Z',NULL,NULL);
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
CREATE TABLE themes (
  code       TEXT PRIMARY KEY,           -- 'authorization', 'logging-audit' ...
  name       TEXT NOT NULL,
  retired_at TEXT
);
INSERT INTO "themes" VALUES('spec-mismatch','Spec mismatch',NULL);
INSERT INTO "themes" VALUES('sign-in','Sign-in and sessions',NULL);
INSERT INTO "themes" VALUES('authorization','Authorization',NULL);
INSERT INTO "themes" VALUES('ops-security','Operations security',NULL);
INSERT INTO "themes" VALUES('privacy','Privacy and retention',NULL);
INSERT INTO "themes" VALUES('access','Access to links and pages',NULL);
INSERT INTO "themes" VALUES('validation','Input validation',NULL);
INSERT INTO "themes" VALUES('test-gaps','Test gaps',NULL);
INSERT INTO "themes" VALUES('test-quality','Test suite quality',NULL);
INSERT INTO "themes" VALUES('time','Time and dates',NULL);
INSERT INTO "themes" VALUES('concurrency','Concurrency',NULL);
INSERT INTO "themes" VALUES('failure-handling','Failure handling and idempotency',NULL);
INSERT INTO "themes" VALUES('compatibility','Compatibility and migrations',NULL);
INSERT INTO "themes" VALUES('defect','Defects',NULL);
INSERT INTO "themes" VALUES('decision-placement','Decision placement',NULL);
INSERT INTO "themes" VALUES('contracts','Inter-component contracts',NULL);
INSERT INTO "themes" VALUES('workflow','Approvals and workflow flows',NULL);
INSERT INTO "themes" VALUES('db-integrity','Database integrity, history, operations',NULL);
INSERT INTO "themes" VALUES('client-api','Client API',NULL);
INSERT INTO "themes" VALUES('generic','Generic text and logic',NULL);
INSERT INTO "themes" VALUES('config-display','Display of configuration',NULL);
INSERT INTO "themes" VALUES('internal-names','Internal names on screen',NULL);
INSERT INTO "themes" VALUES('select-not-type','Select, not type',NULL);
INSERT INTO "themes" VALUES('usability','Usability and accessibility',NULL);
INSERT INTO "themes" VALUES('task-flows','Task flows',NULL);
INSERT INTO "themes" VALUES('shared-frontend','Shared front end',NULL);
INSERT INTO "themes" VALUES('shared-backend','Shared backend code',NULL);
INSERT INTO "themes" VALUES('perf-backend','Performance: backend',NULL);
INSERT INTO "themes" VALUES('perf-db','Performance: database',NULL);
INSERT INTO "themes" VALUES('perf-frontend','Performance: front end',NULL);
INSERT INTO "themes" VALUES('cost','Cost',NULL);
INSERT INTO "themes" VALUES('email-jobs-feeds','Email, jobs and feeds',NULL);
INSERT INTO "themes" VALUES('logging','Logging and audit',NULL);
INSERT INTO "themes" VALUES('monitoring','Alerting and monitoring',NULL);
INSERT INTO "themes" VALUES('ci-release','CI and release',NULL);
INSERT INTO "themes" VALUES('backup-recovery','Backup and recovery',NULL);
INSERT INTO "themes" VALUES('dependencies','Dependencies',NULL);
INSERT INTO "themes" VALUES('docs-inline','Inline developer docs',NULL);
INSERT INTO "themes" VALUES('docs-repo','Repo developer docs',NULL);
INSERT INTO "themes" VALUES('docs-user','End-user docs',NULL);
INSERT INTO "themes" VALUES('handover','Handover and ownership',NULL);
INSERT INTO "themes" VALUES('localization','Localization',NULL);
INSERT INTO "themes" VALUES('verify','Verify',NULL);
INSERT INTO "themes" VALUES('other','Other',NULL);
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
CREATE VIEW v_known_issues_for_brief AS
SELECT i.domain_code, i.code, i.title, i.severity, i.status, i.current_state,
       i.recommendation, i.resolution,
       (SELECT group_concat(test_path || '::' || test_name, '; ')
          FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1) AS guard_tests
FROM issues i
WHERE i.status NOT IN ('duplicate','obsolete')
ORDER BY i.domain_code, i.code;
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
CREATE VIEW v_issue_timeline AS
SELECT i.code, e.at, e.event, e.from_value, e.to_value, r.code AS review,
       s.number AS step, e.commit_sha, a.name AS actor, e.note
FROM events e
JOIN issues i ON e.entity_type = 'issue' AND e.entity_id = i.id
LEFT JOIN reviews r ON r.id = e.review_id
LEFT JOIN steps s ON s.id = e.step_id
LEFT JOIN actors a ON a.id = e.actor_id
ORDER BY i.code, e.at, e.id;
CREATE VIEW v_issue_graph AS
SELECT a.code AS from_issue, r.relation, b.code AS to_issue, r.confidence, r.rationale,
       rv.code AS established_in
FROM issue_relations r
JOIN issues a ON a.id = r.from_issue_id
JOIN issues b ON b.id = r.to_issue_id
LEFT JOIN reviews rv ON rv.id = r.established_in_review_id;
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
CREATE VIEW v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL AND f.verification <> 'false';
CREATE VIEW v_unchecked_known_issues AS
SELECT r.code AS review, i.code AS issue, i.domain_code, i.title
FROM reviews r
JOIN review_domains rd ON rd.review_id = r.id AND rd.decision = 'included'
JOIN issues i ON i.domain_code = rd.domain_code
WHERE i.first_seen_review_id <> r.id
  AND i.opened_at < r.started_at
  AND i.status NOT IN ('duplicate','obsolete')
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = r.id AND c.issue_id = i.id);
CREATE VIEW v_uncovered_findings AS
SELECT p.code AS plan, r.code AS review, f.point_id, f.severity, f.summary
FROM plans p
JOIN plan_reviews pr ON pr.plan_id = p.id
JOIN reviews r ON r.id = pr.review_id
JOIN findings f ON f.review_id = r.id
WHERE f.verification <> 'false'
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                  WHERE sf.finding_id = f.id AND s.plan_id = p.id);
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
CREATE VIEW v_plan_progress AS
SELECT p.code AS plan, ph.number AS phase, ph.title,
       min(s.number) AS first_step, max(s.number) AS last_step, count(s.id) AS steps,
       sum(s.status = 'done') AS done, sum(s.status = 'doing') AS doing,
       sum(s.status = 'waiting-owner') AS waiting_owner,
       sum(s.status = 'not-started') AS not_started, sum(s.status = 'dropped') AS dropped
FROM phases ph JOIN plans p ON p.id = ph.plan_id
LEFT JOIN steps s ON s.phase_id = ph.id
GROUP BY ph.id ORDER BY p.code, ph.number;
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
CREATE VIEW v_code_refs AS
SELECT r.code AS review, f.point_id, i.code AS issue, fl.id AS location_id, fl.role,
       rp.name AS repository, rp.path AS repo_path, fl.commit_sha, rr.tag_name,
       fl.path, fl.line_start, fl.line_end, fl.symbol, fl.blob_sha, fl.permalink,
       'git -C ' || COALESCE(rp.path, '.') || ' show ' || fl.commit_sha || ':' || fl.path
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
CREATE VIEW v_unguarded_fixes AS
SELECT i.code, i.title, i.status,
       CASE WHEN NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id)
            THEN 'no guard test' ELSE 'guard test removed' END AS problem
FROM issues i
WHERE i.status IN ('fixed','verified') AND i.kind NOT IN ('docs','design')
  AND NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1);
CREATE VIEW v_metric_trends AS
SELECT m.name, m.scope, COALESCE(rp.name, 'all') AS repository, r.code AS review,
       r.started_at, m.value, m.unit, m.method
FROM metrics m JOIN reviews r ON r.id = m.review_id
LEFT JOIN repositories rp ON rp.id = m.repository_id
ORDER BY m.name, m.scope, r.started_at;
CREATE VIEW v_file_hotspots AS
SELECT fl.path, count(DISTINCT f.issue_id) AS issues, count(DISTINCT f.review_id) AS reviews,
       sum(f.severity = 'H') AS high_points
FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
WHERE f.verification <> 'false'
GROUP BY fl.path ORDER BY issues DESC, high_points DESC;
CREATE VIEW v_standing_decisions AS
SELECT d.code, d.question, d.answer, d.rationale, d.revisit_when, d.decided_at,
       (SELECT group_concat(i.code, ', ') FROM decision_issues di
          JOIN issues i ON i.id = di.issue_id WHERE di.decision_id = d.id) AS issues
FROM decisions d WHERE d.status = 'decided';
COMMIT;
