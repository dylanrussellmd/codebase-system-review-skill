-- the features domain and feature_proposals: additions to the reviewed codebase that would serve the owner's goals
-- Plan P-0001 step 145 (OWN-13). A proposal names the approved goal or active requirement it serves, what the code
-- does toward it today with evidence, the addition, its value and effort, risks, what it depends on and a test that
-- shows it is built. It is not a defect: it has no severity and no issue, and no count of issues, defects,
-- regressions or coverage reads it. The owner picks or declines each (one non-blocking feature-pick gate per
-- review); a picked one becomes a plan step (feature_proposals.step_id), and a declined one is not proposed again for
-- the same goal (tr_feature_not_again). feature_status_changes is each proposal's history. migrate creates the
-- triggers and views with the shipped schema.

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
CREATE INDEX ix_fk_feature_proposals_review_id            ON feature_proposals(review_id);
CREATE INDEX ix_fk_feature_proposals_goal_id              ON feature_proposals(goal_id);
CREATE INDEX ix_fk_feature_proposals_requirement_id       ON feature_proposals(requirement_id);
CREATE INDEX ix_fk_feature_proposals_decided_by_actor_id  ON feature_proposals(decided_by_actor_id);
CREATE INDEX ix_fk_feature_proposals_gate_id              ON feature_proposals(gate_id);
CREATE INDEX ix_fk_feature_proposals_step_id              ON feature_proposals(step_id);
CREATE INDEX ix_fk_feature_proposals_proposed_by_actor_id ON feature_proposals(proposed_by_actor_id);

CREATE TABLE feature_status_changes (
  id          INTEGER PRIMARY KEY,
  proposal_id INTEGER NOT NULL REFERENCES feature_proposals(id) ON DELETE RESTRICT,
  from_status TEXT,                      -- NULL when it was proposed
  to_status   TEXT NOT NULL,
  note        TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX ix_fk_feature_status_changes_proposal_id ON feature_status_changes(proposal_id);
CREATE INDEX ix_fk_feature_status_changes_actor_id    ON feature_status_changes(actor_id);
CREATE UNIQUE INDEX ux_gates_one_feature_pick ON gates(review_id) WHERE kind = 'feature-pick' AND status <> 'withdrawn';

INSERT INTO gate_kinds(value, label, position) VALUES ('feature-pick','Pick ideas to build',11);

INSERT INTO domains(code, name, category, report_file, description) VALUES
 ('features','Features toward the goals','architecture','features.md',
  'Applies when the review has approved goals or stated requirements.');
INSERT INTO domain_rules(domain_code, rule) VALUES ('features','goals >= 1 or requirements >= 1');
INSERT INTO domain_checks(code, domain_code, position, severity, question, evidence) VALUES
 ('features.1','features',1,'L','Is every approved goal and active requirement listed with what the code does toward it today?','A row per goal and requirement: path:line of what serves it, or absent.'),
 ('features.2','features',2,'L','Does each proposal name the goal or requirement it serves, its value, effort, risks and dependencies?','Each proposal with its goal code and the four answers.'),
 ('features.3','features',3,'L','Does each proposal have a test that would show it is built?','The done-test per proposal: its name and what it asserts.'),
 ('features.4','features',4,'L','Were the proposals already on record checked again against the goals and the code?','Per earlier proposal: still wanted, built, or serving no current goal.');

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('feature_status','proposed','Proposed',1), ('feature_status','picked','Picked',2),
 ('feature_status','declined','Declined',3), ('feature_status','built','Built',4),
 ('feature_value','high','High',1), ('feature_value','medium','Medium',2), ('feature_value','low','Low',3),
 ('feature_effort','small','Small',1), ('feature_effort','medium','Medium',2), ('feature_effort','large','Large',3);
