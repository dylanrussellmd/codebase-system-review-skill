-- must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason
-- Plan P-0001 steps 58, 65, 67, 68, 70, 73, 76, 77, 78, 83 and 93 (views and triggers). The views and triggers come
-- with the shipped schema, which migrate re-creates after the last migration: the reporting views count real
-- findings only (v_real_findings), the must-be-empty views forgive abandoned reviews, superseded and abandoned
-- plans and dropped steps, references fall back to their review's commit, and new triggers refuse accepted-risk or
-- wont-fix without a resolution, 'verified' without a later review's fixed check, a fixed/regressed/changed check
-- without evidence and severity T on a finding that is not a test. Rows already in the ledger are not changed:
-- the new checks below list the ones that break these rules. No table is rebuilt; steps gains one column at the
-- end (the reason a step closes no finding by design, such as the follow-up review that verifies the plan).

ALTER TABLE steps ADD COLUMN no_findings_reason TEXT;

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_history_gaps','Issues and gates whose history is missing an event','error',70),
 ('v_match_problems','Matches whose type disagrees with the issue','error',72),
 ('v_off_plan_coverage','Steps closing a finding of a review their plan does not cover','error',74),
 ('v_done_steps_open_issues','Done steps whose issues are still to do','error',76),
 ('v_closed_without_reason','Issues accepted or declined with no reason recorded','error',78),
 ('v_relation_problems','Related-to pairs stored in both directions','error',80),
 ('v_check_finding_owner','Review checks citing a finding of another issue','error',82);
