-- approval gates keep their fixed options; meta last_write_at; only the owner answers, decides and rejects goals
-- Plan P-0002 step 342 (ISS-0715). tr_gate_fixed_options and its UPDATE twin hold a domain-approval or plan-approval
-- gate to the option values of gate_kinds.options; gates already stored keep theirs. migrate re-creates the triggers
-- with the shipped schema.

SELECT 1;

-- Step 343 (ISS-0716). meta last_write_at holds the time of the last write to any table; the tr_written_* triggers
-- of the shipped schema stamp it. It starts as '' (no write yet), and migrate stamps it once it is done, so a dump
-- made before the upgrade shows as stale.
INSERT OR IGNORE INTO meta(key, value) VALUES ('last_write_at', '');

-- Step 45 (ISS-0427). Only an actor of kind owner or human answers a gate (tr_gate_answerer), decides a decision
-- (tr_decision_decider) or rejects a design goal (tr_goal_rejecter); each has its UPDATE twin. Rows already stored
-- keep their actors.

-- Step 46 (ISS-0427). A production-action or accept-risk answer given in the viewer is applied only after the owner
-- confirms it in chat: answer-gate --confirm fills confirmed_at, confirmed_by_actor_id and confirmed_text, and
-- tr_gate_viewer_confirmed refuses apply until then. The confirmation is logged as an answer-confirmed event.
ALTER TABLE gates ADD COLUMN confirmed_at TEXT;
ALTER TABLE gates ADD COLUMN confirmed_by_actor_id INTEGER REFERENCES actors(id);
ALTER TABLE gates ADD COLUMN confirmed_text TEXT;
CREATE INDEX ix_fk_gates_confirmed_by_actor_id ON gates(confirmed_by_actor_id);
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','answer-confirmed','The owner confirmed an answer in chat',37);

-- Step 47 (ISS-0427). meta ledger_id is a random id made once per ledger. The hub embeds it, every answer the hub
-- saves carries it with the gate's created_at, and import-answers refuses an answer from another ledger's hub or to
-- another gate of the same code.
INSERT OR IGNORE INTO meta(key, value) VALUES ('ledger_id', lower(hex(randomblob(16))));

-- Step 48 (ISS-0014). Every table but meta refuses DELETE; the record tables whose rows never change refuse UPDATE;
-- a review's commit, a guard test's identity and a location's filled lines are fixed (all triggers of the shipped
-- schema). checklist_answers keeps every answer lint-report --record writes: it is rebuilt with an id and
-- recorded_at, its rows kept, and v_checklist_answers holds the latest of each.
CREATE TABLE checklist_answers_new (
  id          INTEGER PRIMARY KEY,
  review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  check_code  TEXT NOT NULL REFERENCES domain_checks(code),
  report      TEXT NOT NULL,                -- the report's file name
  answer      TEXT NOT NULL CHECK (answer IN ('yes','no','partly','not-applicable')),
  evidence    TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
  recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO checklist_answers_new(review_id, check_code, report, answer, evidence)
  SELECT review_id, check_code, report, answer, evidence FROM checklist_answers ORDER BY rowid;
DROP TABLE checklist_answers;
ALTER TABLE checklist_answers_new RENAME TO checklist_answers;
CREATE INDEX ix_fk_checklist_answers_check_code    ON checklist_answers(check_code);
CREATE INDEX ix_checklist_answers_latest ON checklist_answers(review_id, check_code, report, id);

-- Step 49 (ISS-0010). v_review_gaps lists why a review is not complete; tr_review_transition refuses consolidating
-- while an included domain has no finished run and handed-off while the review has any gap. v_incomplete_reviews
-- (must be empty) lists reviews already in those states with the gaps they need closed. check records its result
-- as a check-run event on the review in progress.
INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','check-run','The ledger was checked',38);
INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_incomplete_reviews','Reviews handed off incomplete, or consolidating with a domain not run','error',86);
