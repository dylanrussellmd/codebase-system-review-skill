-- correction commands: who corrects a gate, a location or a check, and why
-- Plan P-0002 step 8 (WALK-M9, LOG-M3, OWN-R3-11). withdraw-gate and reopen-gate set gates.change_note with
-- change_actor_id, and tr_gate_transition refuses a withdrawal without both; correct-location and supersede-check
-- set change_actor_id and change_note on finding_locations and review_issue_checks, which their triggers log and
-- clear. migrate re-creates the triggers with the shipped schema. The three events the commands write get labels.

ALTER TABLE gates ADD COLUMN change_note TEXT;
ALTER TABLE finding_locations ADD COLUMN change_actor_id INTEGER REFERENCES actors(id);
ALTER TABLE finding_locations ADD COLUMN change_note TEXT;
ALTER TABLE review_issue_checks ADD COLUMN change_actor_id INTEGER REFERENCES actors(id);
ALTER TABLE review_issue_checks ADD COLUMN change_note TEXT;
CREATE INDEX ix_fk_review_issue_checks_change_actor_id ON review_issue_checks(change_actor_id);
CREATE INDEX ix_fk_finding_locations_change_actor_id ON finding_locations(change_actor_id);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','rematched','A point was moved to the right issue',32),
 ('event','location-corrected','Where a point is was corrected',33),
 ('event','check-corrected','A review check was corrected',34);
