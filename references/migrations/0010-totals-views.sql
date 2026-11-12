-- v_plan_totals, v_review_totals and v_pending_gates: the counts the viewer's lists show
-- Plan P-0001 (ABS-M7, DOC-X8). The viewer reads its list counts from three views instead of counting in its own
-- SQL: v_plan_totals (steps per plan by status), v_review_totals (real findings per review by severity, new and
-- come back) and v_pending_gates (open and answered gates in inbox order). migrate creates them with the shipped
-- views; no table and no row changes.

SELECT 1;
