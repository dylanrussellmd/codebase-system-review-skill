-- The ledger_checks views as they were at schema 20, before migration 0021 rewrote the slow ones (plan step 92).
-- test_w8_check_speed creates each as a temporary view and compares its rows with the shipped view's.

CREATE TEMP VIEW before_v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL AND r.status <> 'abandoned';

CREATE TEMP VIEW before_v_unchecked_known_issues AS
SELECT r.code AS review, i.code AS issue, i.domain_code, i.title
FROM reviews r
JOIN review_domains rd ON rd.review_id = r.id AND rd.decision = 'included'
JOIN issues i ON i.domain_code = rd.domain_code
WHERE r.status <> 'abandoned'
  AND i.first_seen_review_id <> r.id
  AND julianday(i.opened_at) < julianday(r.started_at)
  AND i.status IN (SELECT value FROM issue_statuses WHERE briefed = 1)
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = r.id AND c.issue_id = i.id);

CREATE TEMP VIEW before_v_untraceable_refs AS
SELECT review, point_id, path, problem FROM (
  SELECT r.code AS review, f.point_id, fl.path,
         CASE WHEN fl.commit_sha IS NULL THEN 'no commit'
              WHEN rr.commit_sha IS NOT NULL AND fl.commit_sha <> rr.commit_sha AND f.discovered_in_step_id IS NULL
                THEN 'commit differs from the review commit'
              WHEN fl.line_start IS NULL AND fl.whole_file = 0 AND fl.symbol IS NULL THEN 'no lines or symbol'
              WHEN fl.line_start IS NULL AND fl.whole_file = 0 THEN 'symbol not resolved to lines'
              WHEN (fl.excerpt IS NULL OR fl.excerpt = '') AND fl.excerpt_status IS NOT 'not-text' THEN 'no excerpt'
              -- line count = newlines + 1 (an excerpt may also end with one extra newline)
              WHEN fl.excerpt_status IS NULL AND fl.line_start IS NOT NULL
                   AND length(fl.excerpt) - length(replace(fl.excerpt, char(10), '')) + 1
                       <> COALESCE(fl.line_end, fl.line_start) - fl.line_start + 1
                   AND NOT (substr(fl.excerpt, -1) = char(10)
                            AND length(fl.excerpt) - length(replace(fl.excerpt, char(10), ''))
                                = COALESCE(fl.line_end, fl.line_start) - fl.line_start + 1)
                THEN 'excerpt does not cover its lines (past the end of the file?)'
              WHEN rr.tag_name IS NULL THEN 'commit not tagged' END AS problem
  FROM finding_locations fl
  JOIN findings f ON f.id = fl.finding_id
  JOIN reviews r ON r.id = f.review_id
  LEFT JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = fl.repository_id
  WHERE r.status <> 'abandoned'
  UNION ALL
  SELECT r.code, f.point_id, NULL, 'no location'
  FROM findings f JOIN reviews r ON r.id = f.review_id
  WHERE r.status <> 'abandoned'
    AND NOT EXISTS (SELECT 1 FROM finding_locations fl WHERE fl.finding_id = f.id)
) WHERE problem IS NOT NULL;

CREATE TEMP VIEW before_v_uncovered_findings AS
SELECT p.code AS plan, r.code AS review, f.point_id, f.severity, f.summary
FROM plans p
JOIN plan_reviews pr ON pr.plan_id = p.id
JOIN reviews r ON r.id = pr.review_id
JOIN findings f ON f.review_id = r.id
WHERE p.status NOT IN ('superseded','abandoned') AND r.status <> 'abandoned'
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                    JOIN plans sp ON sp.id = +s.plan_id
                  WHERE sf.finding_id = f.id
                    AND (sp.id = p.id OR (sp.superseded_by = p.id AND s.status = 'done'))
                    AND (s.status <> 'dropped'
                         OR EXISTS (SELECT 1 FROM decisions d WHERE d.step_id = s.id AND d.status = 'decided')));

CREATE TEMP VIEW before_v_coverage_problems AS
SELECT p.code AS plan, 'step closes nothing' AS problem, CAST(s.number AS TEXT) AS item
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
  AND NULLIF(trim(s.no_findings_reason), '') IS NULL
  AND NOT EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM feature_proposals fp WHERE fp.step_id = s.id)
UNION ALL
SELECT p.code, 'finding closed by several steps without a reason', f.point_id
FROM step_findings sf JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
GROUP BY p.id, sf.finding_id
HAVING count(*) > 1 AND sum(sf.multi_reason IS NULL) > 0;

CREATE TEMP VIEW before_v_unplanned_discoveries AS
SELECT p.code AS plan, s.number AS found_in_step, r.code AS review, f.point_id, i.code AS issue,
       i.status AS issue_status, f.summary
FROM findings f
JOIN steps s ON s.id = f.discovered_in_step_id
JOIN plans p ON p.id = s.plan_id
JOIN reviews r ON r.id = f.review_id
LEFT JOIN issues i ON i.id = f.issue_id
WHERE f.verification <> 'false'
  AND (i.id IS NULL OR i.status IN (SELECT value FROM issue_statuses WHERE active = 1))
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN steps cs ON cs.id = sf.step_id
                  WHERE sf.finding_id = f.id AND cs.status <> 'dropped')
  AND NOT EXISTS (SELECT 1 FROM findings f2 JOIN step_findings sf ON sf.finding_id = f2.id
                  JOIN steps cs ON cs.id = sf.step_id
                  WHERE f.issue_id IS NOT NULL AND f2.issue_id = f.issue_id AND cs.status <> 'dropped');

CREATE TEMP VIEW before_v_history_gaps AS
SELECT 'issue' AS kind, i.code AS ref, 'no opened event' AS problem
FROM issues i
WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id AND e.event = 'opened')
UNION ALL
SELECT 'issue', x.code, 'status ' || x.status || ' not in its history (last logged: ' || x.last || ')'
FROM (SELECT i.code, i.status,
             (SELECT e.to_value FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
                AND e.event IN ('opened','status-changed') ORDER BY e.id DESC LIMIT 1) AS last
        FROM issues i) x
WHERE x.last IS NOT NULL AND x.last IS NOT x.status
UNION ALL
SELECT 'gate', g.code, g.status || ' with no event'
FROM gates g
WHERE g.status IN ('answered','applied')
  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'gate' AND e.entity_id = g.id
                    AND e.to_value = g.status);

CREATE TEMP VIEW before_v_match_problems AS
SELECT review, point_id, issue, match_type, problem FROM (
  SELECT r.code AS review, f.point_id, i.code AS issue, f.match_type,
         CASE WHEN f.match_type = 'continuation'
                   AND NOT EXISTS (SELECT 1 FROM issue_relations ir WHERE ir.from_issue_id = i.id
                                     AND ir.relation = 'continuation-of')
                THEN 'continuation with no continuation-of relation'
              WHEN f.match_type IN ('new','continuation') AND i.first_seen_review_id <> f.review_id
                THEN 'matched as new to an issue first seen in another review'
              WHEN f.match_type IN ('still-present','changed','recurrence','regression')
                   AND i.first_seen_review_id = f.review_id
                THEN 'matched as seen before to an issue this review opened'
              WHEN f.match_type = 'recurrence'
                   AND NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
                                     AND e.event IN ('opened','status-changed')
                                     AND e.to_value IN ('fixed','verified','accepted-risk','wont-fix',
                                                        'duplicate','obsolete'))
                THEN 'recurrence of an issue that was never closed'
              WHEN f.match_type IN ('still-present','changed') AND f.verification <> 'false'
                   AND i.status IN ('fixed','verified') AND i.resolved_at IS NOT NULL
                   AND julianday(i.resolved_at) < julianday(r.started_at)
                THEN 'still present on a fixed issue: match it as a regression' END AS problem
  FROM findings f
  JOIN reviews r ON r.id = f.review_id
  JOIN issues i ON i.id = f.issue_id
  WHERE r.status <> 'abandoned'
) WHERE problem IS NOT NULL;

CREATE TEMP VIEW before_v_off_plan_coverage AS
SELECT p.code AS plan, s.number AS step, r.code AS review, f.point_id
FROM step_findings sf
JOIN steps s ON s.id = sf.step_id
JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
JOIN reviews r ON r.id = f.review_id
WHERE p.status NOT IN ('superseded','abandoned')
  AND NOT EXISTS (SELECT 1 FROM plan_reviews pr WHERE pr.plan_id = p.id AND pr.review_id = f.review_id);

CREATE TEMP VIEW before_v_done_steps_open_issues AS
SELECT DISTINCT p.code AS plan, s.number AS step, i.code AS issue, i.status AS issue_status
FROM steps s
JOIN plans p ON p.id = s.plan_id
JOIN step_findings sf ON sf.step_id = s.id
JOIN findings f ON f.id = sf.finding_id
JOIN issues i ON i.id = f.issue_id
WHERE s.status = 'done' AND p.status NOT IN ('superseded','abandoned') AND f.verification <> 'false'
  AND i.status IN ('open','needs-decision','planned','in-progress')
  AND NOT EXISTS (SELECT 1 FROM findings f2 JOIN step_findings sf2 ON sf2.finding_id = f2.id
                  JOIN steps s2 ON s2.id = sf2.step_id JOIN plans p2 ON p2.id = s2.plan_id
                  WHERE f2.issue_id = i.id AND s2.status NOT IN ('done','dropped')
                    AND p2.status NOT IN ('superseded','abandoned'))
ORDER BY p.code, s.number, i.id;

CREATE TEMP VIEW before_v_closed_without_reason AS
SELECT code, title, status FROM issues
WHERE status IN ('accepted-risk','wont-fix') AND NULLIF(trim(resolution), '') IS NULL;

CREATE TEMP VIEW before_v_relation_problems AS
SELECT a.code AS from_issue, b.code AS to_issue, ir.relation, 'related-to stored in both directions' AS problem
FROM issue_relations ir
JOIN issues a ON a.id = ir.from_issue_id
JOIN issues b ON b.id = ir.to_issue_id
WHERE ir.relation = 'related-to'
  AND EXISTS (SELECT 1 FROM issue_relations rv WHERE rv.relation = 'related-to'
                AND rv.from_issue_id = ir.to_issue_id AND rv.to_issue_id = ir.from_issue_id AND rv.id < ir.id);

CREATE TEMP VIEW before_v_check_finding_owner AS
SELECT r.code AS review, i.code AS issue, f.point_id, fi.code AS finding_issue
FROM review_issue_checks c
JOIN reviews r ON r.id = c.review_id
JOIN issues i ON i.id = c.issue_id
JOIN findings f ON f.id = c.finding_id
JOIN issues fi ON fi.id = f.issue_id
WHERE f.issue_id <> c.issue_id;

CREATE TEMP VIEW before_v_unaddressed_tool_results AS
SELECT v.review, v.lead, v.tool, v.domain, v.rule, v.level,
       COALESCE(v.path, '') || CASE WHEN v.line_start IS NULL THEN '' ELSE ':' || v.line_start END AS location,
       v.message, v.tool_run_id
FROM v_tool_results v JOIN reviews r ON r.code = v.review
WHERE v.disposition = 'open' AND r.status <> 'abandoned'
ORDER BY v.review, v.lead;

CREATE TEMP VIEW before_v_unguarded_fixes AS
SELECT i.code, i.title, i.status,
       CASE WHEN NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id)
            THEN 'no guard test' ELSE 'guard test removed' END AS problem
FROM issues i
WHERE i.status IN (SELECT value FROM issue_statuses WHERE fixed = 1) AND COALESCE(i.kind, '') NOT IN ('docs','design')
  AND NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1);

CREATE TEMP VIEW before_v_artifacts_outside_ledger AS
SELECT r.code AS review, a.kind, a.path
FROM artifacts a LEFT JOIN reviews r ON r.id = a.review_id
WHERE a.path IS NOT NULL AND a.kind IN ('brief','report','index')
  AND replace(a.path, char(92), '/') NOT LIKE '.code-review/%'
  AND replace(a.path, char(92), '/') NOT LIKE
      replace(COALESCE((SELECT value FROM meta WHERE key = 'ledger_location'), '.code-review'), char(92), '/') || '/%';

CREATE TEMP VIEW before_v_unresolved_conflicts AS
SELECT p.code AS plan, a.number AS step_a, b.number AS step_b, c.kind, c.target, c.reason, c.opposite
FROM step_conflicts c JOIN steps a ON a.id = c.step_a_id JOIN steps b ON b.id = c.step_b_id
JOIN plans p ON p.id = a.plan_id
WHERE c.resolution = 'none' AND c.late_step_id IS NULL
ORDER BY p.code, a.number, b.number, c.kind, c.target;

CREATE TEMP VIEW before_v_late_step_conflicts AS
SELECT p.code AS plan, l.number AS step, CASE WHEN l.id = c.step_a_id THEN b.number ELSE a.number END AS other_step,
       c.kind, c.target, c.reason
FROM step_conflicts c JOIN steps a ON a.id = c.step_a_id JOIN steps b ON b.id = c.step_b_id
JOIN steps l ON l.id = c.late_step_id JOIN plans p ON p.id = a.plan_id
WHERE c.resolution = 'none'
ORDER BY p.code, l.number, other_step, c.kind, c.target;
