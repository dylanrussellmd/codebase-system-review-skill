-- measured guard tests: guard_tests.measured_at and measured_commit_sha; v_unproven_steps; the guard-dropped and guard-renamed events
-- Plan P-0002 steps 16-18 (OWN-R3-7, TQ-H1, DEC-L4, HO-other-4). step-status done --guard, add-guard and
-- measure-guards run each guard test with the fix undone and at the fix, and store the outcomes with the commit and
-- time; tr_guard_measured refuses outcomes without measured_at. Rows from older schemas keep their values and stay
-- unmeasured (measured_at NULL) until measure-guards runs on them. migrate re-creates the triggers and views
-- (v_unproven_steps among them) with the shipped schema; the warning-tier check and the event label are the rows
-- added.

ALTER TABLE guard_tests ADD COLUMN measured_at TEXT;
ALTER TABLE guard_tests ADD COLUMN measured_commit_sha TEXT CHECK (measured_commit_sha IS NULL OR ((length(measured_commit_sha) = 40 OR length(measured_commit_sha) = 64) AND measured_commit_sha NOT GLOB '*[^0-9a-f]*'));

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unproven_steps','Done steps with no test seen failing before the fix','warning',105);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','guard-dropped','A guard test was dropped',35),
 ('event','guard-renamed','A guard test was renamed',36);
