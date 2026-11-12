-- one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries
-- Plan P-0001 steps 75, 81, 96 and 97 (step 84's view change and the time triggers come with the shipped views and
-- triggers, which migrate re-creates). Every stored time becomes strftime('%Y-%m-%dT%H:%M:%fZ', value): UTC with
-- milliseconds, so a time with an offset (a git commit time like 2026-11-10T10:00:00+02:00) is converted to UTC
-- and a time without milliseconds or with a space gains them. A stored time that is not a date at all makes this
-- migration fail and change nothing (the error names the constraint below; fix the row, then run migrate again).
-- From now on the tr_time_* triggers refuse any other format. No table is rebuilt: the new columns are added at
-- the end of their tables.

UPDATE schema_migrations SET applied_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', applied_at), applied_at);
UPDATE repositories SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE actors SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE domains SET retired_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', retired_at), retired_at);
UPDATE themes SET retired_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', retired_at), retired_at);
UPDATE design_goals SET adopted_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', adopted_at), adopted_at), retired_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', retired_at), retired_at);
UPDATE reviews SET approved_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', approved_at), approved_at), started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at), completed_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', completed_at), completed_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE review_repositories SET commit_time = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', commit_time), commit_time);
UPDATE reviewer_runs SET started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at), finished_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', finished_at), finished_at);
UPDATE requirements SET stated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', stated_at), stated_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE issues SET opened_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', opened_at), opened_at), resolved_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at), resolved_at), verified_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', verified_at), verified_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE issue_relations SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE findings SET verified_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', verified_at), verified_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE review_issue_checks SET checked_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', checked_at), checked_at);
UPDATE decisions SET asked_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', asked_at), asked_at), decided_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', decided_at), decided_at);
UPDATE gates SET answered_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', answered_at), answered_at), applied_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', applied_at), applied_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE plans SET approved_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', approved_at), approved_at), created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE steps SET started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at), done_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', done_at), done_at), updated_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at), updated_at);
UPDATE step_commits SET committed_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', committed_at), committed_at);
UPDATE guard_tests SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE test_runs SET started_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', started_at), started_at);
UPDATE metrics SET recorded_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', recorded_at), recorded_at);
UPDATE artifacts SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at), created_at);
UPDATE events SET at = COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', at), at);

CREATE TEMP TABLE p0005_unreadable_times (what TEXT CONSTRAINT a_stored_time_is_not_a_date_fix_the_row_then_migrate_again CHECK (what IS NULL));
INSERT INTO p0005_unreadable_times(what)
  SELECT 'schema_migrations.applied_at: ' || applied_at FROM schema_migrations WHERE applied_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', applied_at) IS NOT applied_at
  UNION ALL SELECT 'repositories.created_at: ' || created_at FROM repositories WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'actors.created_at: ' || created_at FROM actors WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'domains.retired_at: ' || retired_at FROM domains WHERE retired_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', retired_at) IS NOT retired_at
  UNION ALL SELECT 'themes.retired_at: ' || retired_at FROM themes WHERE retired_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', retired_at) IS NOT retired_at
  UNION ALL SELECT 'design_goals.adopted_at: ' || adopted_at FROM design_goals WHERE adopted_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', adopted_at) IS NOT adopted_at
  UNION ALL SELECT 'design_goals.retired_at: ' || retired_at FROM design_goals WHERE retired_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', retired_at) IS NOT retired_at
  UNION ALL SELECT 'reviews.approved_at: ' || approved_at FROM reviews WHERE approved_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', approved_at) IS NOT approved_at
  UNION ALL SELECT 'reviews.started_at: ' || started_at FROM reviews WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'reviews.completed_at: ' || completed_at FROM reviews WHERE completed_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', completed_at) IS NOT completed_at
  UNION ALL SELECT 'reviews.updated_at: ' || updated_at FROM reviews WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'review_repositories.commit_time: ' || commit_time FROM review_repositories WHERE commit_time IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', commit_time) IS NOT commit_time
  UNION ALL SELECT 'reviewer_runs.started_at: ' || started_at FROM reviewer_runs WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'reviewer_runs.finished_at: ' || finished_at FROM reviewer_runs WHERE finished_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', finished_at) IS NOT finished_at
  UNION ALL SELECT 'requirements.stated_at: ' || stated_at FROM requirements WHERE stated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', stated_at) IS NOT stated_at
  UNION ALL SELECT 'requirements.created_at: ' || created_at FROM requirements WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'issues.opened_at: ' || opened_at FROM issues WHERE opened_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', opened_at) IS NOT opened_at
  UNION ALL SELECT 'issues.resolved_at: ' || resolved_at FROM issues WHERE resolved_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at) IS NOT resolved_at
  UNION ALL SELECT 'issues.verified_at: ' || verified_at FROM issues WHERE verified_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', verified_at) IS NOT verified_at
  UNION ALL SELECT 'issues.updated_at: ' || updated_at FROM issues WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'issue_relations.created_at: ' || created_at FROM issue_relations WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'findings.verified_at: ' || verified_at FROM findings WHERE verified_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', verified_at) IS NOT verified_at
  UNION ALL SELECT 'findings.created_at: ' || created_at FROM findings WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'review_issue_checks.checked_at: ' || checked_at FROM review_issue_checks WHERE checked_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', checked_at) IS NOT checked_at
  UNION ALL SELECT 'decisions.asked_at: ' || asked_at FROM decisions WHERE asked_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', asked_at) IS NOT asked_at
  UNION ALL SELECT 'decisions.decided_at: ' || decided_at FROM decisions WHERE decided_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', decided_at) IS NOT decided_at
  UNION ALL SELECT 'gates.answered_at: ' || answered_at FROM gates WHERE answered_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', answered_at) IS NOT answered_at
  UNION ALL SELECT 'gates.applied_at: ' || applied_at FROM gates WHERE applied_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', applied_at) IS NOT applied_at
  UNION ALL SELECT 'gates.created_at: ' || created_at FROM gates WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'plans.approved_at: ' || approved_at FROM plans WHERE approved_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', approved_at) IS NOT approved_at
  UNION ALL SELECT 'plans.created_at: ' || created_at FROM plans WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'plans.updated_at: ' || updated_at FROM plans WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'steps.started_at: ' || started_at FROM steps WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'steps.done_at: ' || done_at FROM steps WHERE done_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', done_at) IS NOT done_at
  UNION ALL SELECT 'steps.updated_at: ' || updated_at FROM steps WHERE updated_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT updated_at
  UNION ALL SELECT 'step_commits.committed_at: ' || committed_at FROM step_commits WHERE committed_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) IS NOT committed_at
  UNION ALL SELECT 'guard_tests.created_at: ' || created_at FROM guard_tests WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'test_runs.started_at: ' || started_at FROM test_runs WHERE started_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT started_at
  UNION ALL SELECT 'metrics.recorded_at: ' || recorded_at FROM metrics WHERE recorded_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', recorded_at) IS NOT recorded_at
  UNION ALL SELECT 'artifacts.created_at: ' || created_at FROM artifacts WHERE created_at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT created_at
  UNION ALL SELECT 'events.at: ' || at FROM events WHERE at IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', at) IS NOT at;
DROP TABLE p0005_unreadable_times;
UPDATE meta SET value = strftime('%Y-%m-%dT%H:%M:%fZ', value)
  WHERE key = 'created_at' AND strftime('%Y-%m-%dT%H:%M:%fZ', value) IS NOT NULL;

ALTER TABLE reviews ADD COLUMN report_md TEXT;
ALTER TABLE plans ADD COLUMN narrative_md TEXT;
ALTER TABLE findings ADD COLUMN discovered_in_step_id INTEGER REFERENCES steps(id);
CREATE INDEX ix_fk_findings_discovered_in_step_id    ON findings(discovered_in_step_id);
ALTER TABLE finding_locations ADD COLUMN whole_file INTEGER NOT NULL DEFAULT 0 CHECK (whole_file IN (0,1));
ALTER TABLE finding_locations ADD COLUMN excerpt_status TEXT
  CHECK (excerpt_status IS NULL OR excerpt_status IN ('truncated','not-text'));

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unplanned_discoveries','Defects found during fix work that no plan step closes','error',60);
