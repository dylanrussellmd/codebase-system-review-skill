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
