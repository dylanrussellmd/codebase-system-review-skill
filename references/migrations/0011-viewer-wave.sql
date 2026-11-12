-- gate answer rules in the trigger, owner queue context, event labels, the artifact check
-- Plan P-0001, the viewer wave. tr_gate_answer_rules refuses a note on a gate without free text and a note over
-- 10,000 characters, as answer_gate() does; v_owner_queue gains a context column; the new warning check
-- v_artifacts_outside_ledger lists review files recorded outside the ledger's folder. migrate re-creates the shipped
-- views and triggers; the rows below are what it does not. ix_findings_review_counts lets the per-review counts be
-- read from the index alone. enum 'event' gives the event names the triggers write a label.

CREATE INDEX ix_findings_review_counts ON findings(review_id, verification, severity, match_type);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','opened','Opened',1),
 ('event','seen','Raised by a review',2),
 ('event','recurred','Raised again after it was closed',3),
 ('event','checked','Looked for by a review',4),
 ('event','unlinked','A point was moved to another issue',5),
 ('event','status-changed','Status changed',6),
 ('event','severity-changed','Severity changed',7),
 ('event','title-changed','Title changed',8),
 ('event','before-state-changed','What it did before: text changed',9),
 ('event','current-state-changed','What it does now: text changed',10),
 ('event','after-state-changed','What it does after the fix: text changed',11),
 ('event','recommendation-changed','Agreed fix changed',12),
 ('event','resolution-changed','Resolution changed',13),
 ('event','created','Created',14),
 ('event','proof-changed','Proof changed',15),
 ('event','change-text-changed','Change text changed',16),
 ('event','answer-changed','Answer changed',17),
 ('event','answer-cleared','Answer cleared',18),
 ('event','verification-changed','Check by the manager changed',19),
 ('event','text-changed','Text changed',20),
 ('event','asked','Asked',21),
 ('event','linked','Linked',22),
 ('event','step-linked','Linked to a plan step',23),
 ('event','source-linked','Linked to the interview answers it comes from',24),
 ('event','dependency-added','A step it depends on was added',25),
 ('event','proposed','Proposed',26),
 ('event','reviewer-run-created','A reviewer was started',27),
 ('event','metric-recorded','A measurement was recorded',28),
 ('event','tag-pushed','The tag of the reviewed commit was pushed',29),
 ('event','redacted','A secret was removed from the text',30);

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_artifacts_outside_ledger','Review files recorded outside the ledger''s folder','warning',110);
