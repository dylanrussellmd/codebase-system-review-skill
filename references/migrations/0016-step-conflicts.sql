-- step_touches and step_conflicts: what each plan step touches, and the pairs of steps that touch the same thing
-- Plan P-0001 step 143 (OWN-11, K33). import-steps records each step's declared touches and the files and symbols of
-- the points it closes; `ledger.py conflicts` records every pair of steps touching the same target, and
-- resolve-conflict the judgement. The new check v_unresolved_conflicts blocks plan approval; v_late_step_conflicts
-- (a warning) lists conflicts of steps added after approval, each of which blocks only that step's start.

CREATE TABLE step_touches (
  step_id   INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('file','symbol','table','column','setting','route','behaviour')),
  target    TEXT NOT NULL CHECK (length(trim(target)) > 0),   -- demo:src/app.py, findings.severity, GET /api/x
  direction TEXT NOT NULL DEFAULT 'changes' CHECK (direction IN
              ('adds','removes','tightens','loosens','renames','changes')),
  source    TEXT NOT NULL CHECK (source IN ('declared','finding')),   -- steps.jsonl, or a closed finding's location
  PRIMARY KEY (step_id, kind, target, direction, source)
);
CREATE INDEX ix_step_touches_target ON step_touches(kind, target);

CREATE TABLE step_conflicts (
  id           INTEGER PRIMARY KEY,
  step_a_id    INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,   -- the lower-numbered step
  step_b_id    INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  target       TEXT NOT NULL,
  reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  opposite     INTEGER NOT NULL DEFAULT 0 CHECK (opposite IN (0,1)),   -- they change it in different directions
  late_step_id INTEGER REFERENCES steps(id),   -- the step added after the plan was approved, when one was
  resolution   TEXT NOT NULL DEFAULT 'none' CHECK (resolution IN ('none','no-conflict','order','merge','owner')),
  note         TEXT,
  gate_id      INTEGER REFERENCES gates(id),   -- the owner's gate, for resolution owner
  resolved_by_actor_id INTEGER REFERENCES actors(id),
  resolved_at  TEXT,
  CHECK (step_a_id <> step_b_id),
  CHECK (late_step_id IS NULL OR late_step_id IN (step_a_id, step_b_id)),
  CHECK (resolution = 'none' OR (length(trim(COALESCE(note, ''))) > 0 AND resolved_at IS NOT NULL)),
  CHECK (resolution <> 'owner' OR gate_id IS NOT NULL),
  UNIQUE (step_a_id, step_b_id, kind, target)
);
CREATE INDEX ix_fk_step_conflicts_step_b_id           ON step_conflicts(step_b_id);
CREATE INDEX ix_fk_step_conflicts_late_step_id        ON step_conflicts(late_step_id);
CREATE INDEX ix_fk_step_conflicts_gate_id             ON step_conflicts(gate_id);
CREATE INDEX ix_fk_step_conflicts_resolved_by_actor_id ON step_conflicts(resolved_by_actor_id);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('touch_kind','file','File',1), ('touch_kind','symbol','Function or name',2), ('touch_kind','table','Table',3),
 ('touch_kind','column','Column',4), ('touch_kind','setting','Setting',5), ('touch_kind','route','Route',6),
 ('touch_kind','behaviour','What a user sees',7),
 ('touch_direction','adds','Adds',1), ('touch_direction','removes','Removes',2),
 ('touch_direction','tightens','Tightens',3), ('touch_direction','loosens','Loosens',4),
 ('touch_direction','renames','Renames',5), ('touch_direction','changes','Changes',6),
 ('touch_source','declared','Declared by the step',1), ('touch_source','finding','Where its points are',2),
 ('conflict_resolution','none','Not judged yet',1), ('conflict_resolution','no-conflict','No conflict',2),
 ('conflict_resolution','order','Done in order',3), ('conflict_resolution','merge','Merged into one change',4),
 ('conflict_resolution','owner','Decided by you',5);

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unresolved_conflicts','Plan steps that touch the same thing, not judged yet','error',120),
 ('v_late_step_conflicts','Steps added after approval that touch what other steps touch','warning',130);

INSERT OR IGNORE INTO step_touches(step_id, kind, target, direction, source)
  SELECT DISTINCT sf.step_id, 'file', r.name || ':' || fl.path, 'changes', 'finding'
  FROM step_findings sf JOIN finding_locations fl ON fl.finding_id = sf.finding_id
  JOIN repositories r ON r.id = fl.repository_id;
INSERT OR IGNORE INTO step_touches(step_id, kind, target, direction, source)
  SELECT DISTINCT sf.step_id, 'symbol', r.name || ':' || fl.path || '#' || fl.symbol, 'changes', 'finding'
  FROM step_findings sf JOIN finding_locations fl ON fl.finding_id = sf.finding_id
  JOIN repositories r ON r.id = fl.repository_id WHERE fl.symbol IS NOT NULL;
