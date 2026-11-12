-- check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes
-- Plan P-0001 steps 66, 69, 85-91, 93 and 94. New tables: ledger_checks (the checks `ledger.py check` runs, with an
-- error or warning tier), review_actors (every non-reviewer agent of a review), review_requirement_checks (the
-- requirement trace) and metric_defs (metric labels, units, direction; rows added for names already in metrics).
-- domains.description is filled from SKILL.md's "applies when" lines where it is empty, and every foreign-key
-- column gets a leading index. Views and triggers need nothing here: migrate drops them all and re-creates the
-- shipped ones after the last migration.

CREATE TABLE ledger_checks (
  view     TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  tier     TEXT NOT NULL CHECK (tier IN ('error','warning')),
  position INTEGER NOT NULL UNIQUE
);

CREATE TABLE review_actors (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);

CREATE TABLE review_requirement_checks (
  id                INTEGER PRIMARY KEY,
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  requirement_id    INTEGER NOT NULL REFERENCES requirements(id),
  verdict           TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  where_implemented TEXT,                  -- file, function or page that implements it
  tested            TEXT CHECK (tested IS NULL OR tested IN ('yes','partly','no')),
  finding_id        INTEGER REFERENCES findings(id),   -- the finding raised for it, if any
  evidence          TEXT,
  UNIQUE (review_id, requirement_id)
);

CREATE TABLE metric_defs (
  name   TEXT PRIMARY KEY,                 -- the stable key used in metrics.name
  label  TEXT NOT NULL,
  unit   TEXT,
  better TEXT NOT NULL DEFAULT 'none' CHECK (better IN ('higher','lower','none'))
);

CREATE INDEX ix_fk_actors_parent_actor_id            ON actors(parent_actor_id);
CREATE INDEX ix_fk_design_goals_superseded_by        ON design_goals(superseded_by);
CREATE INDEX ix_fk_reviews_approved_by_actor_id      ON reviews(approved_by_actor_id);
CREATE INDEX ix_fk_reviews_manager_actor_id          ON reviews(manager_actor_id);
CREATE INDEX ix_fk_reviews_previous_review_id        ON reviews(previous_review_id);
CREATE INDEX ix_fk_reviews_requested_by_actor_id     ON reviews(requested_by_actor_id);
CREATE INDEX ix_fk_review_repositories_repository_id ON review_repositories(repository_id);
CREATE INDEX ix_fk_review_goals_goal_id              ON review_goals(goal_id);
CREATE INDEX ix_fk_review_domains_domain_code        ON review_domains(domain_code);
CREATE INDEX ix_fk_reviewer_runs_actor_id            ON reviewer_runs(actor_id);
CREATE INDEX ix_fk_reviewer_runs_domain_code         ON reviewer_runs(domain_code);
CREATE INDEX ix_fk_review_actors_actor_id            ON review_actors(actor_id);
CREATE INDEX ix_fk_requirements_first_review_id      ON requirements(first_review_id);
CREATE INDEX ix_fk_requirements_superseded_by        ON requirements(superseded_by);
CREATE INDEX ix_fk_issues_change_actor_id            ON issues(change_actor_id);
CREATE INDEX ix_fk_issues_change_review_id           ON issues(change_review_id);
CREATE INDEX ix_fk_issues_change_step_id             ON issues(change_step_id);
CREATE INDEX ix_fk_issues_first_seen_review_id       ON issues(first_seen_review_id);
CREATE INDEX ix_fk_issues_last_checked_review_id     ON issues(last_checked_review_id);
CREATE INDEX ix_fk_issues_last_seen_review_id        ON issues(last_seen_review_id);
CREATE INDEX ix_fk_issues_theme_code                 ON issues(theme_code);
CREATE INDEX ix_fk_issue_relations_established_by_actor_id ON issue_relations(established_by_actor_id);
CREATE INDEX ix_fk_issue_relations_established_in_review_id ON issue_relations(established_in_review_id);
CREATE INDEX ix_findings_run                         ON findings(reviewer_run_id);
CREATE INDEX ix_fk_findings_theme_code               ON findings(theme_code);
CREATE INDEX ix_fk_findings_verified_by_actor_id     ON findings(verified_by_actor_id);
CREATE INDEX ix_fk_finding_locations_repository_id   ON finding_locations(repository_id);
CREATE INDEX ix_fk_finding_goals_goal_id             ON finding_goals(goal_id);
CREATE INDEX ix_fk_finding_requirements_requirement_id ON finding_requirements(requirement_id);
CREATE INDEX ix_fk_review_requirement_checks_finding_id ON review_requirement_checks(finding_id);
CREATE INDEX ix_fk_review_requirement_checks_requirement_id ON review_requirement_checks(requirement_id);
CREATE INDEX ix_fk_review_issue_checks_actor_id      ON review_issue_checks(actor_id);
CREATE INDEX ix_ric_finding                          ON review_issue_checks(finding_id);
CREATE INDEX ix_fk_decisions_asked_by_actor_id       ON decisions(asked_by_actor_id);
CREATE INDEX ix_fk_decisions_decided_by_actor_id     ON decisions(decided_by_actor_id);
CREATE INDEX ix_fk_decisions_review_id               ON decisions(review_id);
CREATE INDEX ix_fk_decisions_step_id                 ON decisions(step_id);
CREATE INDEX ix_fk_decision_issues_issue_id          ON decision_issues(issue_id);
CREATE INDEX ix_gates_decision                       ON gates(decision_id);
CREATE INDEX ix_fk_gates_issue_id                    ON gates(issue_id);
CREATE INDEX ix_fk_gates_opened_by_actor_id          ON gates(opened_by_actor_id);
CREATE INDEX ix_fk_gates_plan_id                     ON gates(plan_id);
CREATE INDEX ix_fk_gates_review_id                   ON gates(review_id);
CREATE INDEX ix_gates_step                           ON gates(step_id);
CREATE INDEX ix_fk_plans_approved_by_actor_id        ON plans(approved_by_actor_id);
CREATE INDEX ix_fk_plans_superseded_by               ON plans(superseded_by);
CREATE INDEX ix_fk_plan_reviews_review_id            ON plan_reviews(review_id);
CREATE INDEX ix_fk_steps_change_actor_id             ON steps(change_actor_id);
CREATE INDEX ix_fk_step_commits_repository_id        ON step_commits(repository_id);
CREATE INDEX ix_fk_guard_tests_last_confirmed_review_id ON guard_tests(last_confirmed_review_id);
CREATE INDEX ix_fk_guard_tests_repository_id         ON guard_tests(repository_id);
CREATE INDEX ix_fk_guard_tests_step_id               ON guard_tests(step_id);
CREATE INDEX ix_fk_test_runs_repository_id           ON test_runs(repository_id);
CREATE INDEX ix_fk_test_runs_review_id               ON test_runs(review_id);
CREATE INDEX ix_fk_test_runs_step_id                 ON test_runs(step_id);
CREATE INDEX ix_fk_metrics_domain_code               ON metrics(domain_code);
CREATE INDEX ix_fk_metrics_repository_id             ON metrics(repository_id);
CREATE INDEX ix_fk_artifacts_plan_id                 ON artifacts(plan_id);
CREATE INDEX ix_fk_artifacts_review_id               ON artifacts(review_id);
CREATE INDEX ix_fk_artifacts_reviewer_run_id         ON artifacts(reviewer_run_id);
CREATE INDEX ix_fk_events_actor_id                   ON events(actor_id);
CREATE INDEX ix_fk_events_review_id                  ON events(review_id);
CREATE INDEX ix_fk_events_step_id                    ON events(step_id);

-- Each domain's one-line "applies when" sentence from SKILL.md's domain catalog.
WITH applies(code, text) AS (VALUES
 ('spec','Applies whenever there are stated requirements anywhere.'),
 ('tests','Almost always applies.'),
 ('test-quality','Applies when there is a meaningful test suite.'),
 ('time-concurrency','Applies to anything with dates, schedules, deadlines, expiries, or more than one writer.'),
 ('resilience','Applies whenever there are external calls, jobs, or multi-step writes.'),
 ('compatibility','Applies whenever there is stored data, saved settings, public APIs, or deployed clients.'),
 ('decision-placement','Applies when the system has business rules.'),
 ('contracts','Applies when there is more than one component.'),
 ('server','Applies when there is a server.'),
 ('database','Applies when there is a database.'),
 ('client-api','Applies when a client talks to a backend.'),
 ('abstraction','Almost always applies.'),
 ('hotspots','Applies when there is git history worth mining (a few months or more).'),
 ('ui','Applies when there are screens.'),
 ('walkthroughs','Applies when users complete multi-step tasks.'),
 ('notifications','Applies when the system sends email, SMS, push or calendar feeds.'),
 ('localization','Applies only if other languages, regions or conventions are plausible.'),
 ('security','Almost always applies.'),
 ('privacy','Applies whenever personal data is handled.'),
 ('logging','Almost always applies.'),
 ('operations','Applies when the system is deployed.'),
 ('dependencies','Almost always applies.'),
 ('performance','Applies when speed or scale matters to users or cost.'),
 ('cost','Applies when the system runs on paid infrastructure or services.'),
 ('docs-developer','Almost always applies.'),
 ('docs-user','Applies when the system has users other than its developers.'),
 ('handover','Applies whenever someone other than the current author may need to run or change the system.'))
UPDATE domains SET description = (SELECT text FROM applies WHERE applies.code = domains.code)
 WHERE description IS NULL AND code IN (SELECT code FROM applies);

-- The metric names the domains ask for (keep names stable between reviews so they trend).
INSERT OR IGNORE INTO metric_defs(name, label, unit, better) VALUES
 ('mutation_score','Mutation score','%','higher'),
 ('line_coverage','Line coverage','%','higher'),
 ('branch_coverage','Branch coverage','%','higher'),
 ('test_cases_asserted','Test cases asserted','cases','higher'),
 ('test_cases_needed','Test cases needed','cases','none'),
 ('tests_missing_named','Missing tests named','tests','lower'),
 ('flaky_tests','Flaky tests','tests','lower'),
 ('test_suite_seconds','Test suite run time','s','lower'),
 ('page_weight','Page weight','KB','lower'),
 ('queries_per_page','Queries per page','queries','lower'),
 ('p95_job_ms','Job time, 95th percentile','ms','lower'),
 ('dependency_count','Dependencies','packages','lower'),
 ('vulnerable_dependencies','Dependencies with known vulnerabilities','packages','lower');

-- The checks `ledger.py check` runs (see ledger_checks).
INSERT OR IGNORE INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unmatched_findings','Findings not matched to an issue','error',10),
 ('v_unchecked_known_issues','Known issues in a domain that ran, with no check recorded','error',20),
 ('v_untraceable_refs','Code references that cannot be traced to exact lines','error',30),
 ('v_uncovered_findings','Findings no step of their plan closes','error',40),
 ('v_coverage_problems','Steps closing nothing, or findings closed twice without a reason','error',50),
 ('v_unguarded_fixes','Fixed issues with no guard test, or whose guard test is gone','warning',100);

-- Metric names already recorded get a catalog row too (label = the name with spaces, capitalized; a unit seen).
INSERT OR IGNORE INTO metric_defs(name, label, unit)
  SELECT name, upper(substr(name, 1, 1)) || replace(substr(name, 2), '_', ' '), min(unit)
    FROM metrics GROUP BY name;
