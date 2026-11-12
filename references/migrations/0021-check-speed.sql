-- indexes for check's views, which read them instead of every row; the answers-imported event
-- Plan P-0001 step 92 (decision D-0016). ix_events_history, ix_issues_domain_known, ix_findings_discovered and
-- ix_locations_untraceable are new; ix_findings_review_counts gains issue_id and ix_step_findings_find gains
-- step_id, so v_match_problems and v_uncovered_findings are read from them. migrate re-creates the views (four of
-- them rewritten to read in a cheaper order, with the same rows) with the shipped schema. Plan step 225 (FIX-32):
-- import-answers records each run as an answers-imported event on the plan; its label is the one row added.

DROP INDEX ix_findings_review_counts;
CREATE INDEX ix_findings_review_counts ON findings(review_id, verification, severity, match_type, issue_id);
DROP INDEX ix_step_findings_find;
CREATE INDEX ix_step_findings_find   ON step_findings(finding_id, step_id);
CREATE INDEX ix_events_history       ON events(entity_type, entity_id, id, event, to_value)
  WHERE event IN ('opened','status-changed');
CREATE INDEX ix_issues_domain_known  ON issues(domain_code, status, first_seen_review_id, opened_at);
CREATE INDEX ix_findings_discovered  ON findings(discovered_in_step_id) WHERE discovered_in_step_id IS NOT NULL;
CREATE INDEX ix_locations_untraceable ON finding_locations(finding_id)
  WHERE commit_sha IS NULL OR (line_start IS NULL AND whole_file = 0)
     OR ((excerpt IS NULL OR excerpt = '') AND excerpt_status IS NOT 'not-text')
     OR (excerpt_status IS NULL AND line_start IS NOT NULL
         AND length(excerpt) - length(replace(excerpt, char(10), '')) + 1
             <> COALESCE(line_end, line_start) - line_start + 1
         AND NOT (substr(excerpt, -1) = char(10)
                  AND length(excerpt) - length(replace(excerpt, char(10), ''))
                      = COALESCE(line_end, line_start) - line_start + 1));

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('event','answers-imported','Answers brought in from the published page',31);
