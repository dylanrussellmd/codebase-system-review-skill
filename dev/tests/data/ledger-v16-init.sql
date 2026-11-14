-- `ledger.py init` by the ledger.py of commit c06a6e6 (schema 16), with its views and triggers.
-- Frozen: regenerate only with python3 -m dev.tests._migrate_helpers --write-v16-init
BEGIN TRANSACTION;
CREATE TABLE actors (
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('owner','human','manager','reviewer','verifier','indexer',
                     'matcher','drafter','fixer','tool')),
  name            TEXT NOT NULL,         -- person's name, or the agent's role label ("security reviewer")
  model           TEXT,                  -- model id for agents
  harness         TEXT,                  -- e.g. OpenCode, CI
  session_ref     TEXT,                  -- session id or URL
  parent_actor_id INTEGER REFERENCES actors(id),   -- subagent -> the agent that launched it
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE artifacts (
  id              INTEGER PRIMARY KEY,
  review_id       INTEGER REFERENCES reviews(id),
  plan_id         INTEGER REFERENCES plans(id),
  reviewer_run_id INTEGER REFERENCES reviewer_runs(id),
  kind            TEXT NOT NULL CHECK (kind IN
                    ('brief','report','index','summary-doc','plan-file','progress-page',
                     'probe-script','other')),
  path            TEXT,                    -- repo-relative if kept in the repo
  url             TEXT,
  sha256          TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (path IS NOT NULL OR url IS NOT NULL)
);
CREATE TABLE checklist_answers (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  check_code TEXT NOT NULL REFERENCES domain_checks(code),
  report     TEXT NOT NULL,                -- the report's file name
  answer     TEXT NOT NULL CHECK (answer IN ('yes','no','partly','not-applicable')),
  evidence   TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
  PRIMARY KEY (review_id, check_code, report)
);
CREATE TABLE decision_issues (
  decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  issue_id    INTEGER NOT NULL REFERENCES issues(id),
  PRIMARY KEY (decision_id, issue_id)
);
CREATE TABLE decisions (
  id                  INTEGER PRIMARY KEY,
  code                TEXT NOT NULL UNIQUE    -- D-0001
                        CHECK (code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'D-*[^0-9]*'),
  question            TEXT NOT NULL,
  options             TEXT CHECK (options IS NULL OR json_valid(options)),
  answer              TEXT,
  rationale           TEXT,
  status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','revisit'))
                        CHECK (status <> 'decided' OR answer IS NOT NULL),   -- a decided decision has its answer
  revisit_when        TEXT,                   -- condition or date that reopens it
  review_id           INTEGER REFERENCES reviews(id),
  step_id             INTEGER REFERENCES steps(id),
  asked_by_actor_id   INTEGER REFERENCES actors(id),
  decided_by_actor_id INTEGER REFERENCES actors(id),
  asked_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at          TEXT
);
CREATE TABLE design_goals (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE     -- G-01
                  CHECK (code GLOB 'G-[0-9][0-9]*' AND code NOT GLOB 'G-*[^0-9]*'),
  text          TEXT NOT NULL,           -- one statement of one goal, in words the owner approved
  source        TEXT,                    -- where it came from (doc path, "owner, 2026-11-10")
  adopted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),  -- when it was written down
  retired_at    TEXT,
  superseded_by INTEGER REFERENCES design_goals(id),
  domain_code   TEXT REFERENCES domains(code),           -- NULL = a goal of the whole project
  status        TEXT NOT NULL DEFAULT 'proposed' REFERENCES goal_statuses(value),
  approved_by_actor_id INTEGER REFERENCES actors(id),   -- the owner (an actor of kind owner or human)
  approved_at   TEXT,
  review_id     INTEGER REFERENCES reviews(id),          -- the review whose interview it came from
  gate_id       INTEGER REFERENCES gates(id),            -- the confirm gate that asks the owner to approve it
  note          TEXT,                    -- why it was rejected or retired
  change_actor_id INTEGER REFERENCES actors(id),        -- who makes the next status change; logged, then cleared
  CHECK (superseded_by IS NULL OR retired_at IS NOT NULL),   -- a superseded goal is retired
  CHECK (status <> 'approved' OR (approved_by_actor_id IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK (status <> 'rejected' OR length(trim(COALESCE(note, ''))) > 0)
);
CREATE TABLE domain_checks (
  code        TEXT PRIMARY KEY CHECK (length(trim(code)) > 0),        -- spec.1
  domain_code TEXT NOT NULL REFERENCES domains(code),
  position    INTEGER NOT NULL,
  severity    TEXT NOT NULL REFERENCES severities(value),   -- the default of a finding that answers it
  question    TEXT NOT NULL CHECK (length(trim(question)) > 0),
  evidence    TEXT NOT NULL CHECK (length(trim(evidence)) > 0),     -- what counts as an answer
  UNIQUE (domain_code, position)
);
INSERT INTO "domain_checks" VALUES('spec.1','spec',1,'H','Is every stated requirement listed, with its source quoted?','A row per requirement: its code or quote, source and location.');
INSERT INTO "domain_checks" VALUES('spec.2','spec',2,'H','Does the implementation of each requirement match it at its boundaries, priority orders and exceptions?','Per requirement: path:line of every copy and a verdict (matches, differs, partly, missing, untested).');
INSERT INTO "domain_checks" VALUES('spec.3','spec',3,'M','Does a test assert each requirement?','The test name per requirement, or untested.');
INSERT INTO "domain_checks" VALUES('spec.4','spec',4,'M','Does the code do anything no requirement asks for, or do requirements contradict each other?','Each such behaviour or contradiction with path:line.');
INSERT INTO "domain_checks" VALUES('tests.1','tests',1,'H','Is every unit of behaviour or configuration listed with the cases it needs?','A row per function, route, command, setting, rule or flag, with its cases and expected outcomes.');
INSERT INTO "domain_checks" VALUES('tests.2','tests',2,'H','Is each needed case asserted by a test that would fail if the behaviour broke?','Per case: the test name and Yes, Partly or No.');
INSERT INTO "domain_checks" VALUES('tests.3','tests',3,'M','Is every input validated, and every validation branch and error path tested?','Inputs read with no validation; branches and error paths with no test.');
INSERT INTO "domain_checks" VALUES('tests.4','tests',4,'M','Are migrations and conversions tested?','Each migration or conversion with its test, or no test.');
INSERT INTO "domain_checks" VALUES('tests.5','tests',5,'L','Do the totals add up?','Units, cases needed, asserted and missing.');
INSERT INTO "domain_checks" VALUES('test-quality.1','test-quality',1,'H','What is the mutation score of the core modules?','The score per module and the surviving mutants that matter, or not measured with the hand sample.');
INSERT INTO "domain_checks" VALUES('test-quality.2','test-quality',2,'M','Are any tests flaky or order-dependent?','The results of repeated and shuffled runs.');
INSERT INTO "domain_checks" VALUES('test-quality.3','test-quality',3,'M','Do tests depend on the real clock, time zone, network or shared state?','Each such test by name.');
INSERT INTO "domain_checks" VALUES('test-quality.4','test-quality',4,'L','How fast is the suite, and which tests are slowest?','The run time and the slowest tests.');
INSERT INTO "domain_checks" VALUES('test-quality.5','test-quality',5,'M','Does every recorded guard test still exist and still guard its issue?','Per guard test: exists, and fails without its fix where tried.');
INSERT INTO "domain_checks" VALUES('time-concurrency.1','time-concurrency',1,'H','Is every place that creates, parses, compares, formats or stores a date or time listed with its time zone handling?','A row per place: path:line, the zone stored and converted, whose today it is.');
INSERT INTO "domain_checks" VALUES('time-concurrency.2','time-concurrency',2,'M','Are daylight-saving changes, month, year and leap boundaries and inclusive or exclusive ends handled?','The concrete dates to test and what happens on each.');
INSERT INTO "domain_checks" VALUES('time-concurrency.3','time-concurrency',3,'H','What happens when two actors change the same thing at once?','Per shared write: the guard (version check, constraint, transaction, lock) or the lost update.');
INSERT INTO "domain_checks" VALUES('time-concurrency.4','time-concurrency',4,'M','Does a test exercise each boundary date and each interleaving?','Test names, or the missing tests by name.');
INSERT INTO "domain_checks" VALUES('resilience.1','resilience',1,'H','Does every call to another service, queue, database or file system have a timeout and a retry that is safe to repeat?','A row per call: timeout, retry policy, idempotency.');
INSERT INTO "domain_checks" VALUES('resilience.2','resilience',2,'H','Can any multi-step change stop halfway and leave inconsistent data?','Per change: the transaction or compensating step, or the inconsistent state.');
INSERT INTO "domain_checks" VALUES('resilience.3','resilience',3,'M','What does each job do on failure, on a rerun, on overlap with itself and on a missed run?','The four answers per job.');
INSERT INTO "domain_checks" VALUES('resilience.4','resilience',4,'M','What do the user and the operator see for each failure?','At least five failures followed end to end.');
INSERT INTO "domain_checks" VALUES('compatibility.1','compatibility',1,'H','Can today''s code read everything older code wrote?','Per stored thing (rows, settings, files, cookies, links): the read path and the oldest version handled.');
INSERT INTO "domain_checks" VALUES('compatibility.2','compatibility',2,'H','Is every schema or settings migration tested from each older version in use?','Per migration: the test and the versions it starts from.');
INSERT INTO "domain_checks" VALUES('compatibility.3','compatibility',3,'M','Can each deploy roll back without losing or corrupting data?','The rollback path per kind of change.');
INSERT INTO "domain_checks" VALUES('compatibility.4','compatibility',4,'M','Are API changes versioned or additive, and do old clients keep working across a deploy?','Each breaking change waiting to happen and the test that would catch it.');
INSERT INTO "domain_checks" VALUES('decision-placement.1','decision-placement',1,'H','Is every business decision listed with the layer that decides it today?','A row per decision: where it is decided, path:line.');
INSERT INTO "domain_checks" VALUES('decision-placement.2','decision-placement',2,'H','Is any decision written more than once, and do the copies agree?','Every copy with path:line and the result of probing them.');
INSERT INTO "domain_checks" VALUES('decision-placement.3','decision-placement',3,'M','Is any decision checked in one path and not in another?','Each path that skips the check.');
INSERT INTO "domain_checks" VALUES('decision-placement.4','decision-placement',4,'M','Which decisions should move, and in what order?','Move or keep with the reason, and the tests that must pass before and after.');
INSERT INTO "domain_checks" VALUES('contracts.1','contracts',1,'H','Is every endpoint or message between components listed with its request, response and errors?','An inventory per boundary.');
INSERT INTO "domain_checks" VALUES('contracts.2','contracts',2,'H','Does each caller handle every response the callee can give?','Per call: each success shape, refusal, error and timeout, and the ones not handled.');
INSERT INTO "domain_checks" VALUES('contracts.3','contracts',3,'M','Do the producer''s contract and the consumer''s copy agree field by field?','Each drifted or duplicated type.');
INSERT INTO "domain_checks" VALUES('contracts.4','contracts',4,'M','Is every endpoint and response branch tested, failures included?','Tests per branch: timeout, 4xx, 5xx, malformed, unavailable.');
INSERT INTO "domain_checks" VALUES('server.1','server',1,'M','Where does the server hardcode domain or customer-specific behaviour?','Each place with path:line, whether a setting exists, and the fix.');
INSERT INTO "domain_checks" VALUES('server.2','server',2,'H','Is every route, job, consumer, webhook and sign-in flow listed with who may call it?','The inventory with the permission per entry.');
INSERT INTO "domain_checks" VALUES('server.3','server',3,'H','Does each have tests for success, each refusal, validation, stale saves and a failing dependency?','Cases against tests; the missing tests by name.');
INSERT INTO "domain_checks" VALUES('server.4','server',4,'H','Is any client input unvalidated, or any permission check without a negative test?','Each with path:line.');
INSERT INTO "domain_checks" VALUES('database.1','database',1,'M','Is everything the system consumes or produces stored and queryable later?','Anything lost, lossy or only in logs.');
INSERT INTO "domain_checks" VALUES('database.2','database',2,'M','Does every distinct query use an index, bound its results and avoid a query per item?','The query plan per query: scans, repeated queries, unbounded results.');
INSERT INTO "domain_checks" VALUES('database.3','database',3,'H','Do constraints, uniqueness and transactions keep the data consistent under concurrent writes?','Each missing constraint or unguarded write.');
INSERT INTO "domain_checks" VALUES('database.4','database',4,'M','Are migrations reversible, retention defined, and no secret stored in clear?','Per migration and per sensitive column.');
INSERT INTO "domain_checks" VALUES('client-api.1','client-api',1,'M','Does every backend capability have an API and a screen that uses it?','The capability inventory with the gaps both ways.');
INSERT INTO "domain_checks" VALUES('client-api.2','client-api',2,'M','Are the API styles coherent: no duplicated capability, one shape for results and errors?','Each inconsistency.');
INSERT INTO "domain_checks" VALUES('client-api.3','client-api',3,'H','Do two paths to one action apply the same permission check?','Each pair of paths with its checks.');
INSERT INTO "domain_checks" VALUES('client-api.4','client-api',4,'L','Is any identifier a display name?','Each one.');
INSERT INTO "domain_checks" VALUES('abstraction.1','abstraction',1,'M','Which types, contracts or logic are duplicated between packages or layers?','Each duplicate with every location.');
INSERT INTO "domain_checks" VALUES('abstraction.2','abstraction',2,'M','How many files change to add a new entity, rule, setting group or notification type?','The file list per walk-through.');
INSERT INTO "domain_checks" VALUES('abstraction.3','abstraction',3,'L','Is there dead code, an oversized module or a circular dependency?','Each with its path.');
INSERT INTO "domain_checks" VALUES('abstraction.4','abstraction',4,'L','What is the refactoring order that keeps the system working?','Quick wins, medium, structural.');
INSERT INTO "domain_checks" VALUES('hotspots.1','hotspots',1,'M','Which files change most often and are most complex?','The ranked list from git log.');
INSERT INTO "domain_checks" VALUES('hotspots.2','hotspots',2,'M','Which files always change together but live apart?','Each cluster.');
INSERT INTO "domain_checks" VALUES('hotspots.3','hotspots',3,'M','Which code has a single author, and which busy areas have few tests?','Lists with counts.');
INSERT INTO "domain_checks" VALUES('hotspots.4','hotspots',4,'L','Which files keep producing issues across reviews?','The rows of the hotspot view.');
INSERT INTO "domain_checks" VALUES('ui.1','ui',1,'H','Does each page meet the design brief (type scale, palette, density)?','Per page: the goal broken and the screenshot, or rendered result not seen.');
INSERT INTO "domain_checks" VALUES('ui.2','ui',2,'H','Does every text meet the required contrast, and every image, field, button and link have a name?','The audit''s output per page, or the same computed from the stylesheets and marked not measured.');
INSERT INTO "domain_checks" VALUES('ui.3','ui',3,'M','How many colours, font sizes and spacing values are in use against the brief''s scale?','The inventory with counts and the values to merge.');
INSERT INTO "domain_checks" VALUES('ui.4','ui',4,'M','Does each page work at phone width and in both themes?','Screenshots at 390 and 1280 pixels, light and dark, with what breaks.');
INSERT INTO "domain_checks" VALUES('ui.5','ui',5,'M','Are there internal names on screen, typing where a choice would do, or text below the writing standard?','Each with file:line, the text and the shorter version.');
INSERT INTO "domain_checks" VALUES('ui.6','ui',6,'H','Is any link shown to someone who is then refused, or any page shown without access?','The access matrix: kind of user by link, button and page.');
INSERT INTO "domain_checks" VALUES('ui.7','ui',7,'M','Can every configuration the backend allows be displayed and changed?','Each setting with no screen.');
INSERT INTO "domain_checks" VALUES('walkthroughs.1','walkthroughs',1,'H','Can each kind of user complete each of its main tasks?','10 to 20 tasks per kind of user, traced page by page.');
INSERT INTO "domain_checks" VALUES('walkthroughs.2','walkthroughs',2,'M','Where does a hand-off between pages break or confuse?','Each dead end, missing confirmation or undo, and step that needs information the user cannot see.');
INSERT INTO "domain_checks" VALUES('walkthroughs.3','walkthroughs',3,'M','Can a first-time user finish the main task unaided, and is what they see first what the task needs?','Per main task: the first screen and where they would stop.');
INSERT INTO "domain_checks" VALUES('walkthroughs.4','walkthroughs',4,'L','What on each page could be deleted without loss?','A list per page.');
INSERT INTO "domain_checks" VALUES('notifications.1','notifications',1,'M','Is every message type listed with its trigger, recipients, content source and opt-out?','The inventory.');
INSERT INTO "domain_checks" VALUES('notifications.2','notifications',2,'H','Can a message be sent twice, missed, or sent to the wrong person after a role change?','Per message type: the guard or the failure.');
INSERT INTO "domain_checks" VALUES('notifications.3','notifications',3,'M','Are bounces handled, and is staging kept from mailing real people?','Configuration and code, or not verified.');
INSERT INTO "domain_checks" VALUES('notifications.4','notifications',4,'L','Does each message render on a phone and have a plain-text alternative?','Per message type.');
INSERT INTO "domain_checks" VALUES('localization.1','localization',1,'M','Which user-facing strings are hardcoded?','Each with file:line.');
INSERT INTO "domain_checks" VALUES('localization.2','localization',2,'M','Are dates, numbers, currency and week start formatted for the reader''s locale?','Each fixed format.');
INSERT INTO "domain_checks" VALUES('localization.3','localization',3,'L','Will translated text fit, and is right-to-left possible?','Each layout that will not.');
INSERT INTO "domain_checks" VALUES('security.1','security',1,'H','For every route and API function, what is the permission check, and can a lower-privileged, other-tenant or signed-out caller reach it?','A row per route: the check with file:line and the result of changing an id.');
INSERT INTO "domain_checks" VALUES('security.2','security',2,'H','Where does client input reach SQL, HTML, e-mail, file paths, redirects or deserialization unescaped?','Each sink with file:line.');
INSERT INTO "domain_checks" VALUES('security.3','security',3,'H','How are sign-in codes, tokens and sessions stored, expired and limited?','Per method: hashing, expiry, attempt limits, cookie flags.');
INSERT INTO "domain_checks" VALUES('security.4','security',4,'H','Is any secret in the repository, a workflow or a log, or stored in clear?','Locations only, never the value.');
INSERT INTO "domain_checks" VALUES('security.5','security',5,'M','Which browser protections are set?','Per page or response: content security policy, cross-site request checks, cross-origin rules, framing, caching of private responses.');
INSERT INTO "domain_checks" VALUES('security.6','security',6,'M','Who can deploy, and what could each kind of misuser do today?','Workflow permissions and a short threat model.');
INSERT INTO "domain_checks" VALUES('security.7','security',7,'M','Which protections have a test that fails without them?','Test names and the missing tests.');
INSERT INTO "domain_checks" VALUES('privacy.1','privacy',1,'H','Where does each kind of personal data enter, and where is it stored or copied?','A data map: database, backups, logs, e-mail, analytics, third parties.');
INSERT INTO "domain_checks" VALUES('privacy.2','privacy',2,'H','Who can read it, and how long is it kept?','Per kind of data.');
INSERT INTO "domain_checks" VALUES('privacy.3','privacy',3,'M','How would one person''s data be exported or deleted in full?','The path, or what would be left behind.');
INSERT INTO "domain_checks" VALUES('privacy.4','privacy',4,'M','Which obligations apply, and where are the gaps?','Each gap, marked not legal advice.');
INSERT INTO "domain_checks" VALUES('logging.1','logging',1,'M','Is every important event logged?','The events that leave no trace: sign-ins, refusals, state changes, external calls, jobs, e-mail, configuration changes.');
INSERT INTO "domain_checks" VALUES('logging.2','logging',2,'M','Can an operator answer five realistic complaints from the logs alone?','Each complaint walked, and where the trail breaks.');
INSERT INTO "domain_checks" VALUES('logging.3','logging',3,'H','Do logs hold secrets, tokens, codes or personal data, or accept injected lines?','Each statement with file:line.');
INSERT INTO "domain_checks" VALUES('logging.4','logging',4,'L','Are structure, levels, a request id and zoned timestamps consistent, with no swallowed exception?','Each exception to it.');
INSERT INTO "domain_checks" VALUES('logging.5','logging',5,'M','Do errors reach a person, and where do logs go and for how long?','Alerting, retention and access.');
INSERT INTO "domain_checks" VALUES('operations.1','operations',1,'M','Does CI run every suite, linter and type-check on every change and block merges on failure?','The workflow files.');
INSERT INTO "domain_checks" VALUES('operations.2','operations',2,'M','Are builds reproducible, and are the deploy and rollback steps written down?','The steps, or what is missing.');
INSERT INTO "domain_checks" VALUES('operations.3','operations',3,'H','If the database, hosting account, domain or e-mail provider were lost today, what is the path back?','Per loss: the path, the time, and whether a restore was ever tested.');
INSERT INTO "domain_checks" VALUES('operations.4','operations',4,'M','What is backed up, how often, where, and is it encrypted?','Per store.');
INSERT INTO "domain_checks" VALUES('operations.5','operations',5,'M','Are scheduled jobs monitored for failure and missed runs?','Per job.');
INSERT INTO "domain_checks" VALUES('dependencies.1','dependencies',1,'M','Which packages are outdated, abandoned or have known problems?','From the lock files; not checked online without network.');
INSERT INTO "domain_checks" VALUES('dependencies.2','dependencies',2,'M','Is every version pinned, and every CI action referenced by commit hash?','Each one that is not.');
INSERT INTO "domain_checks" VALUES('dependencies.3','dependencies',3,'L','Is there a licence problem, an install script or a dependency used for something trivial?','Each one.');
INSERT INTO "domain_checks" VALUES('performance.1','performance',1,'M','Where does the time go for each page load and action?','Server calls, queries per call and repeated work; measured or reasoned, saying which.');
INSERT INTO "domain_checks" VALUES('performance.2','performance',2,'M','What do the heavy components cost in time and memory at realistic and larger sizes?','Numbers per job type.');
INSERT INTO "domain_checks" VALUES('performance.3','performance',3,'M','What is each page''s weight and layout shift?','The audit''s numbers, or not measured.');
INSERT INTO "domain_checks" VALUES('performance.4','performance',4,'M','What breaks first at 10x users and 3x data?','The first limit and the three changes that buy the most.');
INSERT INTO "domain_checks" VALUES('performance.5','performance',5,'L','Which performance tests or budgets exist?','The existing ones and the ones to add.');
INSERT INTO "domain_checks" VALUES('cost.1','cost',1,'M','What does each service cost now and at 10x scale?','From configuration and pricing in the repository; not verified otherwise.');
INSERT INTO "domain_checks" VALUES('cost.2','cost',2,'M','What could run up a bill by accident?','Runaway retries, unbounded logs or storage, no spending limit.');
INSERT INTO "domain_checks" VALUES('cost.3','cost',3,'L','Which account and payment method is each service billed to?','Per service.');
INSERT INTO "domain_checks" VALUES('docs-developer.1','docs-developer',1,'M','Does every public function, type and module say what it does, its inputs and outputs, and why?','Per module: documented, partly, undocumented.');
INSERT INTO "domain_checks" VALUES('docs-developer.2','docs-developer',2,'M','Which comments or docs are stale against the current code?','Each statement with file:line and what the code does.');
INSERT INTO "domain_checks" VALUES('docs-developer.3','docs-developer',3,'M','Do the README, setup, architecture and settings docs exist, and do their commands run?','Per document: exists, accurate, what is missing; commands run where safe.');
INSERT INTO "domain_checks" VALUES('docs-developer.4','docs-developer',4,'L','Does every piece of text meet the writing standard?','Each breach with the shorter version.');
INSERT INTO "domain_checks" VALUES('docs-user.1','docs-user',1,'M','Which tasks, settings and errors can a user meet with no explanation anywhere?','Each one.');
INSERT INTO "domain_checks" VALUES('docs-user.2','docs-user',2,'M','Is each piece of help accurate to current behaviour and findable where it is needed?','The inventory with a verdict per piece.');
INSERT INTO "domain_checks" VALUES('docs-user.3','docs-user',3,'L','Is it written for the reader, to the writing standard?','Each breach with the shorter version.');
INSERT INTO "domain_checks" VALUES('handover.1','handover',1,'H','Could a new maintainer clone, set up, run, test, deploy and roll back from the repository and its docs alone?','Each step walked, and where they would get stuck.');
INSERT INTO "domain_checks" VALUES('handover.2','handover',2,'H','Who owns each account, domain, key and secret, and is any tied to one person?','The list with the owner and how access transfers.');
INSERT INTO "domain_checks" VALUES('handover.3','handover',3,'M','Are there runbooks for the common incidents and the periodic chores?','Each one, present or missing.');
INSERT INTO "domain_checks" VALUES('handover.4','handover',4,'M','Where is knowledge held by one person?','From the history or the hotspots report.');
CREATE TABLE domain_rules (
  domain_code TEXT PRIMARY KEY REFERENCES domains(code),
  rule        TEXT NOT NULL CHECK (length(trim(rule)) > 0)   -- 'db_files >= 1 or api_files >= 1'
);
INSERT INTO "domain_rules" VALUES('spec','requirements >= 1 or spec_files >= 1');
INSERT INTO "domain_rules" VALUES('tests','code_files >= 1');
INSERT INTO "domain_rules" VALUES('test-quality','test_files >= 5');
INSERT INTO "domain_rules" VALUES('time-concurrency','time_files >= 1 or concurrency_files >= 1');
INSERT INTO "domain_rules" VALUES('resilience','external_call_files >= 1 or job_files >= 1 or db_files >= 1');
INSERT INTO "domain_rules" VALUES('compatibility','db_files >= 1 or api_files >= 1 or config_files >= 1');
INSERT INTO "domain_rules" VALUES('decision-placement','code_files >= 10');
INSERT INTO "domain_rules" VALUES('contracts','components >= 2');
INSERT INTO "domain_rules" VALUES('server','server_files >= 1');
INSERT INTO "domain_rules" VALUES('database','db_files >= 1');
INSERT INTO "domain_rules" VALUES('client-api','ui_files >= 1 and server_files >= 1');
INSERT INTO "domain_rules" VALUES('abstraction','code_files >= 1');
INSERT INTO "domain_rules" VALUES('hotspots','history_months >= 3');
INSERT INTO "domain_rules" VALUES('ui','ui_files >= 1');
INSERT INTO "domain_rules" VALUES('walkthroughs','page_files >= 1');
INSERT INTO "domain_rules" VALUES('notifications','notification_files >= 1');
INSERT INTO "domain_rules" VALUES('localization','i18n_files >= 1');
INSERT INTO "domain_rules" VALUES('security','code_files >= 1');
INSERT INTO "domain_rules" VALUES('privacy','personal_data_files >= 1');
INSERT INTO "domain_rules" VALUES('logging','code_files >= 1');
INSERT INTO "domain_rules" VALUES('operations','deploy_files >= 1 or ci_files >= 1');
INSERT INTO "domain_rules" VALUES('dependencies','code_files >= 1');
INSERT INTO "domain_rules" VALUES('performance','server_files >= 1 or db_files >= 1 or page_files >= 1');
INSERT INTO "domain_rules" VALUES('cost','paid_service_files >= 1 or deploy_files >= 1');
INSERT INTO "domain_rules" VALUES('docs-developer','code_files >= 1');
INSERT INTO "domain_rules" VALUES('docs-user','user_doc_files >= 1 or page_files >= 1');
INSERT INTO "domain_rules" VALUES('handover','authors >= 2 or deploy_files >= 1 or history_months >= 6');
CREATE TABLE domains (
  code        TEXT PRIMARY KEY,          -- 'security', 'logging', 'docs-developer' ...
  name        TEXT NOT NULL,
  category    TEXT NOT NULL CHECK (category IN
                ('correctness','architecture','user-facing','security-operations',
                 'documentation','custom')),
  description TEXT,
  report_file TEXT,                      -- default report file name
  retired_at  TEXT
);
INSERT INTO "domains" VALUES('spec','Spec conformance','correctness','Applies whenever there are stated requirements anywhere.','spec.md',NULL);
INSERT INTO "domains" VALUES('tests','Test coverage','correctness','Almost always applies.','tests-<module>.md',NULL);
INSERT INTO "domains" VALUES('test-quality','Test suite quality','correctness','Applies when there is a meaningful test suite.','test-quality.md',NULL);
INSERT INTO "domains" VALUES('time-concurrency','Time, dates and concurrency','correctness','Applies to anything with dates, schedules, deadlines, expiries, or more than one writer.','time-concurrency.md',NULL);
INSERT INTO "domains" VALUES('resilience','Failure handling and resilience','correctness','Applies whenever there are external calls, jobs, or multi-step writes.','resilience.md',NULL);
INSERT INTO "domains" VALUES('compatibility','Backward compatibility and migrations','correctness','Applies whenever there is stored data, saved settings, public APIs, or deployed clients.','compatibility.md',NULL);
INSERT INTO "domains" VALUES('decision-placement','Decision placement','architecture','Applies when the system has business rules.','decision-placement.md',NULL);
INSERT INTO "domains" VALUES('contracts','Internal contracts','architecture','Applies when there is more than one component.','contracts.md',NULL);
INSERT INTO "domains" VALUES('server','Backend / server','architecture','Applies when there is a server.','server.md',NULL);
INSERT INTO "domains" VALUES('database','Database','architecture','Applies when there is a database.','database.md',NULL);
INSERT INTO "domains" VALUES('client-api','Backend-to-client API','architecture','Applies when a client talks to a backend.','client-api.md',NULL);
INSERT INTO "domains" VALUES('abstraction','Abstraction and shared code','architecture','Almost always applies.','abstraction.md',NULL);
INSERT INTO "domains" VALUES('hotspots','Hotspots from history','architecture','Applies when there is git history worth mining (a few months or more).','hotspots.md',NULL);
INSERT INTO "domains" VALUES('ui','Front end / UI','user-facing','Applies when there are screens.','ui-<area>.md',NULL);
INSERT INTO "domains" VALUES('walkthroughs','Task walkthroughs','user-facing','Applies when users complete multi-step tasks.','walkthroughs.md',NULL);
INSERT INTO "domains" VALUES('notifications','Email and notifications','user-facing','Applies when the system sends email, SMS, push or calendar feeds.','notifications.md',NULL);
INSERT INTO "domains" VALUES('localization','Localization','user-facing','Applies only if other languages, regions or conventions are plausible.','localization.md',NULL);
INSERT INTO "domains" VALUES('security','Security','security-operations','Almost always applies.','security.md',NULL);
INSERT INTO "domains" VALUES('privacy','Privacy and data map','security-operations','Applies whenever personal data is handled.','privacy.md',NULL);
INSERT INTO "domains" VALUES('logging','Logging','security-operations','Almost always applies.','logging.md',NULL);
INSERT INTO "domains" VALUES('operations','Operations, CI/CD and recovery','security-operations','Applies when the system is deployed.','operations.md',NULL);
INSERT INTO "domains" VALUES('dependencies','Dependencies and supply chain','security-operations','Almost always applies.','dependencies.md',NULL);
INSERT INTO "domains" VALUES('performance','Performance','security-operations','Applies when speed or scale matters to users or cost.','performance.md',NULL);
INSERT INTO "domains" VALUES('cost','Cost','security-operations','Applies when the system runs on paid infrastructure or services.','cost.md',NULL);
INSERT INTO "domains" VALUES('docs-developer','Developer documentation','documentation','Almost always applies.','docs-developer.md',NULL);
INSERT INTO "domains" VALUES('docs-user','End-user documentation','documentation','Applies when the system has users other than its developers.','docs-user.md',NULL);
INSERT INTO "domains" VALUES('handover','Maintainability and handover','documentation','Applies whenever someone other than the current author may need to run or change the system.','handover.md',NULL);
CREATE TABLE enum_labels (
  enum     TEXT NOT NULL,
  value    TEXT NOT NULL,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (enum, value),
  UNIQUE (enum, position)
);
INSERT INTO "enum_labels" VALUES('check_tier','error','Must be empty',1);
INSERT INTO "enum_labels" VALUES('check_tier','warning','Worth a look',2);
INSERT INTO "enum_labels" VALUES('actor_kind','owner','Owner',1);
INSERT INTO "enum_labels" VALUES('actor_kind','human','Person',2);
INSERT INTO "enum_labels" VALUES('actor_kind','manager','Manager agent',3);
INSERT INTO "enum_labels" VALUES('actor_kind','reviewer','Reviewer agent',4);
INSERT INTO "enum_labels" VALUES('actor_kind','verifier','Verifier agent',5);
INSERT INTO "enum_labels" VALUES('actor_kind','indexer','Indexer agent',6);
INSERT INTO "enum_labels" VALUES('actor_kind','matcher','Matcher agent',7);
INSERT INTO "enum_labels" VALUES('actor_kind','drafter','Plan drafter agent',8);
INSERT INTO "enum_labels" VALUES('actor_kind','fixer','Fixer agent',9);
INSERT INTO "enum_labels" VALUES('actor_kind','tool','Tool',10);
INSERT INTO "enum_labels" VALUES('domain_category','correctness','Correctness',1);
INSERT INTO "enum_labels" VALUES('domain_category','architecture','Architecture',2);
INSERT INTO "enum_labels" VALUES('domain_category','user-facing','User-facing',3);
INSERT INTO "enum_labels" VALUES('domain_category','security-operations','Security and operations',4);
INSERT INTO "enum_labels" VALUES('domain_category','documentation','Documentation',5);
INSERT INTO "enum_labels" VALUES('domain_category','custom','Custom',6);
INSERT INTO "enum_labels" VALUES('review_scope','full','Full review',1);
INSERT INTO "enum_labels" VALUES('review_scope','partial','Partial review',2);
INSERT INTO "enum_labels" VALUES('review_scope','targeted','Targeted review',3);
INSERT INTO "enum_labels" VALUES('review_scope','second','Second review',4);
INSERT INTO "enum_labels" VALUES('review_scope','fix-verification','Fix check',5);
INSERT INTO "enum_labels" VALUES('review_status','planning','Planning',1);
INSERT INTO "enum_labels" VALUES('review_status','awaiting-approval','Waiting for your approval',2);
INSERT INTO "enum_labels" VALUES('review_status','approved','Approved',3);
INSERT INTO "enum_labels" VALUES('review_status','running','Reviewers running',4);
INSERT INTO "enum_labels" VALUES('review_status','verifying','Verifying',5);
INSERT INTO "enum_labels" VALUES('review_status','indexing','Indexing points',6);
INSERT INTO "enum_labels" VALUES('review_status','consolidating','Writing it up',7);
INSERT INTO "enum_labels" VALUES('review_status','handed-off','Handed off',8);
INSERT INTO "enum_labels" VALUES('review_status','abandoned','Abandoned',9);
INSERT INTO "enum_labels" VALUES('domain_decision','included','Included',1);
INSERT INTO "enum_labels" VALUES('domain_decision','skipped','Skipped',2);
INSERT INTO "enum_labels" VALUES('owner_change','none','As proposed',1);
INSERT INTO "enum_labels" VALUES('owner_change','added-by-owner','Added by you',2);
INSERT INTO "enum_labels" VALUES('owner_change','removed-by-owner','Removed by you',3);
INSERT INTO "enum_labels" VALUES('owner_change','modified-by-owner','Changed by you',4);
INSERT INTO "enum_labels" VALUES('run_status','queued','Queued',1);
INSERT INTO "enum_labels" VALUES('run_status','running','Running',2);
INSERT INTO "enum_labels" VALUES('run_status','finished','Finished',3);
INSERT INTO "enum_labels" VALUES('run_status','failed','Failed',4);
INSERT INTO "enum_labels" VALUES('run_status','cancelled','Cancelled',5);
INSERT INTO "enum_labels" VALUES('requirement_status','active','In force',1);
INSERT INTO "enum_labels" VALUES('requirement_status','superseded','Replaced',2);
INSERT INTO "enum_labels" VALUES('requirement_status','withdrawn','Withdrawn',3);
INSERT INTO "enum_labels" VALUES('issue_kind','defect','Defect',1);
INSERT INTO "enum_labels" VALUES('issue_kind','test','Missing test',2);
INSERT INTO "enum_labels" VALUES('issue_kind','validation','Validation',3);
INSERT INTO "enum_labels" VALUES('issue_kind','refactor','Refactor',4);
INSERT INTO "enum_labels" VALUES('issue_kind','feature','Feature',5);
INSERT INTO "enum_labels" VALUES('issue_kind','design','Design',6);
INSERT INTO "enum_labels" VALUES('issue_kind','docs','Documentation',7);
INSERT INTO "enum_labels" VALUES('issue_kind','logging','Logging',8);
INSERT INTO "enum_labels" VALUES('issue_kind','perf','Performance',9);
INSERT INTO "enum_labels" VALUES('issue_kind','security','Security',10);
INSERT INTO "enum_labels" VALUES('issue_kind','ops','Operations',11);
INSERT INTO "enum_labels" VALUES('issue_kind','verify','To verify',12);
INSERT INTO "enum_labels" VALUES('confidence','certain','Certain',1);
INSERT INTO "enum_labels" VALUES('confidence','likely','Likely',2);
INSERT INTO "enum_labels" VALUES('confidence','possible','Possible',3);
INSERT INTO "enum_labels" VALUES('verification','unverified','Not checked yet',1);
INSERT INTO "enum_labels" VALUES('verification','confirmed','Confirmed',2);
INSERT INTO "enum_labels" VALUES('verification','partly','Partly confirmed',3);
INSERT INTO "enum_labels" VALUES('verification','false','False finding',4);
INSERT INTO "enum_labels" VALUES('verification','not-checkable','Could not be checked',5);
INSERT INTO "enum_labels" VALUES('location_role','primary','Main place',1);
INSERT INTO "enum_labels" VALUES('location_role','also','Also here',2);
INSERT INTO "enum_labels" VALUES('location_role','test','Test',3);
INSERT INTO "enum_labels" VALUES('location_role','doc','Documentation',4);
INSERT INTO "enum_labels" VALUES('location_role','config','Configuration',5);
INSERT INTO "enum_labels" VALUES('excerpt_status','truncated','First 200 lines only',1);
INSERT INTO "enum_labels" VALUES('excerpt_status','not-text','Not text',2);
INSERT INTO "enum_labels" VALUES('location_source','commit','Read from the commit',1);
INSERT INTO "enum_labels" VALUES('location_source','working-tree','Read from the reviewed working tree',2);
INSERT INTO "enum_labels" VALUES('verdict','matches','Matches',1);
INSERT INTO "enum_labels" VALUES('verdict','differs','Differs',2);
INSERT INTO "enum_labels" VALUES('verdict','partly','Partly matches',3);
INSERT INTO "enum_labels" VALUES('verdict','missing','Missing',4);
INSERT INTO "enum_labels" VALUES('verdict','untested','Not tested',5);
INSERT INTO "enum_labels" VALUES('tested','yes','Tested',1);
INSERT INTO "enum_labels" VALUES('tested','partly','Partly tested',2);
INSERT INTO "enum_labels" VALUES('tested','no','Not tested',3);
INSERT INTO "enum_labels" VALUES('check_outcome','still-present','Still present',1);
INSERT INTO "enum_labels" VALUES('check_outcome','changed','Changed',2);
INSERT INTO "enum_labels" VALUES('check_outcome','fixed','Fixed',3);
INSERT INTO "enum_labels" VALUES('check_outcome','regressed','Came back',4);
INSERT INTO "enum_labels" VALUES('check_outcome','obsolete','No longer applies',5);
INSERT INTO "enum_labels" VALUES('check_outcome','not-checked','Not checked',6);
INSERT INTO "enum_labels" VALUES('decision_status','open','Open',1);
INSERT INTO "enum_labels" VALUES('decision_status','decided','Decided',2);
INSERT INTO "enum_labels" VALUES('decision_status','revisit','To revisit',3);
INSERT INTO "enum_labels" VALUES('gate_status','open','Waiting on you',1);
INSERT INTO "enum_labels" VALUES('gate_status','answered','Answered, waiting for the agent',2);
INSERT INTO "enum_labels" VALUES('gate_status','applied','Done',3);
INSERT INTO "enum_labels" VALUES('gate_status','withdrawn','Withdrawn',4);
INSERT INTO "enum_labels" VALUES('answered_via','chat','In chat',1);
INSERT INTO "enum_labels" VALUES('answered_via','viewer','In the viewer',2);
INSERT INTO "enum_labels" VALUES('answered_via','other','Elsewhere',3);
INSERT INTO "enum_labels" VALUES('plan_status','drafting','Drafting',1);
INSERT INTO "enum_labels" VALUES('plan_status','awaiting-approval','Waiting for your approval',2);
INSERT INTO "enum_labels" VALUES('plan_status','approved','Approved',3);
INSERT INTO "enum_labels" VALUES('plan_status','in-progress','In progress',4);
INSERT INTO "enum_labels" VALUES('plan_status','complete','Complete',5);
INSERT INTO "enum_labels" VALUES('plan_status','superseded','Replaced',6);
INSERT INTO "enum_labels" VALUES('plan_status','abandoned','Abandoned',7);
INSERT INTO "enum_labels" VALUES('owner_need','none','',1);
INSERT INTO "enum_labels" VALUES('owner_need','decision','Your decision',2);
INSERT INTO "enum_labels" VALUES('owner_need','confirm','Your confirmation',3);
INSERT INTO "enum_labels" VALUES('owner_need','review','Your review',4);
INSERT INTO "enum_labels" VALUES('owner_need','production','Production action',5);
INSERT INTO "enum_labels" VALUES('step_status','not-started','Not started',1);
INSERT INTO "enum_labels" VALUES('step_status','doing','In progress',2);
INSERT INTO "enum_labels" VALUES('step_status','waiting-owner','Waiting on you',3);
INSERT INTO "enum_labels" VALUES('step_status','done','Done',4);
INSERT INTO "enum_labels" VALUES('step_status','dropped','Dropped',5);
INSERT INTO "enum_labels" VALUES('test_purpose','baseline','Baseline',1);
INSERT INTO "enum_labels" VALUES('test_purpose','targeted','Targeted',2);
INSERT INTO "enum_labels" VALUES('test_purpose','flake-check','Flakiness check',3);
INSERT INTO "enum_labels" VALUES('test_purpose','mutation','Mutation testing',4);
INSERT INTO "enum_labels" VALUES('test_purpose','before-fix','Before the fix',5);
INSERT INTO "enum_labels" VALUES('test_purpose','after-fix','After the fix',6);
INSERT INTO "enum_labels" VALUES('test_purpose','ci','Continuous integration',7);
INSERT INTO "enum_labels" VALUES('metric_better','higher','Higher is better',1);
INSERT INTO "enum_labels" VALUES('metric_better','lower','Lower is better',2);
INSERT INTO "enum_labels" VALUES('metric_better','none','Neither',3);
INSERT INTO "enum_labels" VALUES('metric_method','measured','Measured',1);
INSERT INTO "enum_labels" VALUES('metric_method','counted','Counted',2);
INSERT INTO "enum_labels" VALUES('metric_method','estimated','Estimated',3);
INSERT INTO "enum_labels" VALUES('artifact_kind','brief','Brief',1);
INSERT INTO "enum_labels" VALUES('artifact_kind','report','Report',2);
INSERT INTO "enum_labels" VALUES('artifact_kind','index','Index',3);
INSERT INTO "enum_labels" VALUES('artifact_kind','summary-doc','Summary document',4);
INSERT INTO "enum_labels" VALUES('artifact_kind','plan-file','Plan file',5);
INSERT INTO "enum_labels" VALUES('artifact_kind','progress-page','Progress page',6);
INSERT INTO "enum_labels" VALUES('artifact_kind','probe-script','Probe script',7);
INSERT INTO "enum_labels" VALUES('artifact_kind','other','Other',8);
INSERT INTO "enum_labels" VALUES('entity_type','review','Review',1);
INSERT INTO "enum_labels" VALUES('entity_type','issue','Issue',2);
INSERT INTO "enum_labels" VALUES('entity_type','step','Plan step',3);
INSERT INTO "enum_labels" VALUES('entity_type','plan','Plan',4);
INSERT INTO "enum_labels" VALUES('entity_type','decision','Decision',5);
INSERT INTO "enum_labels" VALUES('entity_type','finding','Finding',6);
INSERT INTO "enum_labels" VALUES('entity_type','gate','Question for you',7);
INSERT INTO "enum_labels" VALUES('entity_type','relation','Relation',8);
INSERT INTO "enum_labels" VALUES('entity_type','check','Review check',9);
INSERT INTO "enum_labels" VALUES('entity_type','step-finding','Step coverage',10);
INSERT INTO "enum_labels" VALUES('entity_type','goal','Design goal',11);
INSERT INTO "enum_labels" VALUES('entity_type','question','Interview question',12);
INSERT INTO "enum_labels" VALUES('event','opened','Opened',1);
INSERT INTO "enum_labels" VALUES('event','seen','Raised by a review',2);
INSERT INTO "enum_labels" VALUES('event','recurred','Raised again after it was closed',3);
INSERT INTO "enum_labels" VALUES('event','checked','Looked for by a review',4);
INSERT INTO "enum_labels" VALUES('event','unlinked','A point was moved to another issue',5);
INSERT INTO "enum_labels" VALUES('event','status-changed','Status changed',6);
INSERT INTO "enum_labels" VALUES('event','severity-changed','Severity changed',7);
INSERT INTO "enum_labels" VALUES('event','title-changed','Title changed',8);
INSERT INTO "enum_labels" VALUES('event','before-state-changed','What it did before: text changed',9);
INSERT INTO "enum_labels" VALUES('event','current-state-changed','What it does now: text changed',10);
INSERT INTO "enum_labels" VALUES('event','after-state-changed','What it does after the fix: text changed',11);
INSERT INTO "enum_labels" VALUES('event','recommendation-changed','Agreed fix changed',12);
INSERT INTO "enum_labels" VALUES('event','resolution-changed','Resolution changed',13);
INSERT INTO "enum_labels" VALUES('event','created','Created',14);
INSERT INTO "enum_labels" VALUES('event','proof-changed','Proof changed',15);
INSERT INTO "enum_labels" VALUES('event','change-text-changed','Change text changed',16);
INSERT INTO "enum_labels" VALUES('event','answer-changed','Answer changed',17);
INSERT INTO "enum_labels" VALUES('event','answer-cleared','Answer cleared',18);
INSERT INTO "enum_labels" VALUES('event','verification-changed','Check by the manager changed',19);
INSERT INTO "enum_labels" VALUES('event','text-changed','Text changed',20);
INSERT INTO "enum_labels" VALUES('event','asked','Asked',21);
INSERT INTO "enum_labels" VALUES('event','linked','Linked',22);
INSERT INTO "enum_labels" VALUES('event','step-linked','Linked to a plan step',23);
INSERT INTO "enum_labels" VALUES('event','source-linked','Linked to the interview answers it comes from',24);
INSERT INTO "enum_labels" VALUES('event','dependency-added','A step it depends on was added',25);
INSERT INTO "enum_labels" VALUES('event','proposed','Proposed',26);
INSERT INTO "enum_labels" VALUES('event','reviewer-run-created','A reviewer was started',27);
INSERT INTO "enum_labels" VALUES('event','metric-recorded','A measurement was recorded',28);
INSERT INTO "enum_labels" VALUES('event','tag-pushed','The tag of the reviewed commit was pushed',29);
INSERT INTO "enum_labels" VALUES('event','redacted','A secret was removed from the text',30);
INSERT INTO "enum_labels" VALUES('checklist_answer','yes','Yes',1);
INSERT INTO "enum_labels" VALUES('checklist_answer','no','No',2);
INSERT INTO "enum_labels" VALUES('checklist_answer','partly','Partly',3);
INSERT INTO "enum_labels" VALUES('checklist_answer','not-applicable','Does not apply',4);
INSERT INTO "enum_labels" VALUES('touch_kind','file','File',1);
INSERT INTO "enum_labels" VALUES('touch_kind','symbol','Function or name',2);
INSERT INTO "enum_labels" VALUES('touch_kind','table','Table',3);
INSERT INTO "enum_labels" VALUES('touch_kind','column','Column',4);
INSERT INTO "enum_labels" VALUES('touch_kind','setting','Setting',5);
INSERT INTO "enum_labels" VALUES('touch_kind','route','Route',6);
INSERT INTO "enum_labels" VALUES('touch_kind','behaviour','What a user sees',7);
INSERT INTO "enum_labels" VALUES('touch_direction','adds','Adds',1);
INSERT INTO "enum_labels" VALUES('touch_direction','removes','Removes',2);
INSERT INTO "enum_labels" VALUES('touch_direction','tightens','Tightens',3);
INSERT INTO "enum_labels" VALUES('touch_direction','loosens','Loosens',4);
INSERT INTO "enum_labels" VALUES('touch_direction','renames','Renames',5);
INSERT INTO "enum_labels" VALUES('touch_direction','changes','Changes',6);
INSERT INTO "enum_labels" VALUES('touch_source','declared','Declared by the step',1);
INSERT INTO "enum_labels" VALUES('touch_source','finding','Where its points are',2);
INSERT INTO "enum_labels" VALUES('conflict_resolution','none','Not judged yet',1);
INSERT INTO "enum_labels" VALUES('conflict_resolution','no-conflict','No conflict',2);
INSERT INTO "enum_labels" VALUES('conflict_resolution','order','Done in order',3);
INSERT INTO "enum_labels" VALUES('conflict_resolution','merge','Merged into one change',4);
INSERT INTO "enum_labels" VALUES('conflict_resolution','owner','Decided by you',5);
CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('review','issue','step','plan','decision','finding','gate',
                                                   'relation','check','step-finding','goal','question')),
  entity_id   INTEGER NOT NULL,
  event       TEXT NOT NULL,              -- status-changed, severity-changed, opened, seen, linked, note ...
  from_value  TEXT,
  to_value    TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  review_id   INTEGER REFERENCES reviews(id),
  step_id     INTEGER REFERENCES steps(id),
  commit_sha  TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64) AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  note        TEXT,
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE finding_goals (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);
CREATE TABLE finding_locations (
  id             INTEGER PRIMARY KEY,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  repository_id  INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                                    AND commit_sha NOT GLOB '*[^0-9a-f]*')),
                                            -- full hash, lowercase hex; filled from review_repositories if left
                                            -- NULL (the location's repository must be one the review looked at)
  path           TEXT NOT NULL,             -- repo-relative, forward slashes (see the CHECK below)
  line_start     INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end       INTEGER CHECK (line_end IS NULL OR line_start IS NOT NULL),   -- no end without a start
  symbol         TEXT,                      -- function, class, route, setting name
  blob_sha       TEXT,                      -- `git rev-parse <commit>:<path>`
  excerpt        TEXT,                      -- the cited lines, verbatim, at that commit
  excerpt_sha256 TEXT,                      -- hash of excerpt, to detect the same code later
  permalink      TEXT CHECK (permalink IS NULL OR permalink GLOB 'https://*' OR permalink GLOB 'http://*'),
                                            -- e.g. https://github.com/o/r/blob/<sha>/<path>#L10-L20
  role           TEXT NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','also','test','doc','config')),
  excerpt_redacted INTEGER NOT NULL DEFAULT 0 CHECK (excerpt_redacted IN (0,1)),
                                            -- 1 = fill-refs masked secret-looking values in excerpt as [redacted];
                                            -- excerpt_sha256 is still the hash of the real lines
  whole_file     INTEGER NOT NULL DEFAULT 0 CHECK (whole_file IN (0,1)),
                                            -- 1 = the point is about the whole file (location form repo:path, no
                                            -- lines, no symbol); fill-refs sets it with lines 1..n
  excerpt_status TEXT CHECK (excerpt_status IS NULL OR excerpt_status IN ('truncated','not-text')),
                                            -- truncated = a whole-file excerpt holds only its first 200 lines;
                                            -- not-text = binary or not UTF-8: no excerpt, excerpt_sha256 is of
                                            -- the file's bytes
  source         TEXT NOT NULL DEFAULT 'commit' CHECK (source IN ('commit','working-tree')),
                                            -- where the excerpt was read: the commit, or (schema 8) the working
                                            -- tree of a review that recorded uncommitted changes (is_dirty), since
                                            -- that is the text its reviewers saw (snapshot saves the patch)
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);
CREATE TABLE finding_requirements (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);
CREATE TABLE findings (
  id                   INTEGER PRIMARY KEY,
  review_id            INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  reviewer_run_id      INTEGER REFERENCES reviewer_runs(id),
  point_id             TEXT NOT NULL,       -- index id: prefix + the report's own id (SEC-H3)
  issue_id             INTEGER REFERENCES issues(id),   -- set during matching, by an UPDATE (never on INSERT)
  match_type           TEXT REFERENCES match_types(value)
                         CHECK (match_type IS NULL OR issue_id IS NOT NULL),   -- a match type needs the issue
  match_confidence     TEXT CHECK (match_confidence IN ('certain','likely','possible')),
  match_rationale      TEXT,
  severity             TEXT NOT NULL REFERENCES severities(value),
  kind                 TEXT NOT NULL CHECK (kind IN
                         ('defect','test','validation','refactor','feature','design','docs',
                          'logging','perf','security','ops','verify')),
  domain_code          TEXT NOT NULL REFERENCES domains(code),
  theme_code           TEXT REFERENCES themes(code),
  sub                  TEXT NOT NULL,
  summary              TEXT NOT NULL,       -- one self-contained sentence
  detail               TEXT,
  before_state         TEXT,                -- what the system does now, as observed
  recommendation       TEXT,                -- the fix
  recommended_test     TEXT,                -- test name and what it must assert
  evidence             TEXT,
  where_report         TEXT,                -- report line numbers
  fingerprint          TEXT,
  related_point_ids    TEXT CHECK (related_point_ids IS NULL OR json_valid(related_point_ids)),
  verification         TEXT NOT NULL DEFAULT 'unverified' CHECK (verification IN
                         ('unverified','confirmed','partly','false','not-checkable')),
  verified_by_actor_id INTEGER REFERENCES actors(id),
  verified_at          TEXT,
  verification_note    TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  discovered_in_step_id INTEGER REFERENCES steps(id),  -- set when the point was found during fix work on this
                                            -- plan step (not by a reviewer); v_unplanned_discoveries checks it
  UNIQUE (review_id, point_id)
);
CREATE TABLE gate_kinds (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE
);
INSERT INTO "gate_kinds" VALUES('domain-approval','Approve review domains',1);
INSERT INTO "gate_kinds" VALUES('plan-approval','Approve fix plan',2);
INSERT INTO "gate_kinds" VALUES('decision','Decision',3);
INSERT INTO "gate_kinds" VALUES('step-input','Input on a step',4);
INSERT INTO "gate_kinds" VALUES('confirm-false-finding','Confirm a false finding',5);
INSERT INTO "gate_kinds" VALUES('accept-risk','Accept a risk',6);
INSERT INTO "gate_kinds" VALUES('production-action','Production action',7);
INSERT INTO "gate_kinds" VALUES('review-handoff','Review hand-off',8);
INSERT INTO "gate_kinds" VALUES('confirm','Confirm',9);
INSERT INTO "gate_kinds" VALUES('other','Question',10);
CREATE TABLE gates (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE      -- GATE-0001
                    CHECK (code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'GATE-*[^0-9]*'),
  kind            TEXT NOT NULL REFERENCES gate_kinds(value),
  title           TEXT NOT NULL,
  question        TEXT NOT NULL,
  context         TEXT,                     -- plain text the owner needs to answer
  options         TEXT CHECK (options IS NULL OR json_valid(options))
                    CHECK (options IS NULL OR json_type(options) = 'array'),
                                            -- JSON array of {"value","label","description"} (value and label
                                            -- required: tr_gate_options_shape)
  allow_text      INTEGER NOT NULL DEFAULT 1 CHECK (allow_text IN (0,1))
                    -- someone must be able to answer: free text, or at least one option
                    CHECK (allow_text = 1 OR (options IS NOT NULL AND json_array_length(options) > 0)),
  blocking        INTEGER NOT NULL DEFAULT 1 CHECK (blocking IN (0,1)),  -- work stops until answered
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','applied','withdrawn'))
                    CHECK (status <> 'answered' OR answer_value IS NOT NULL OR answer_text IS NOT NULL)
                    CHECK (status <> 'applied' OR applied_note IS NOT NULL),
  answer_value    TEXT,                     -- the option chosen
  answer_text     TEXT,                     -- the owner's own words
  answered_by     TEXT,
  answered_via    TEXT CHECK (answered_via IS NULL OR answered_via IN ('chat','viewer','other')),
  answered_at     TEXT,
  applied_note    TEXT,                     -- what the agent did with the answer
  applied_at      TEXT,
  review_id       INTEGER REFERENCES reviews(id),
  plan_id         INTEGER REFERENCES plans(id),
  step_id         INTEGER REFERENCES steps(id),
  issue_id        INTEGER REFERENCES issues(id),
  decision_id     INTEGER REFERENCES decisions(id),
  opened_by_actor_id INTEGER REFERENCES actors(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  answered_by_actor_id INTEGER REFERENCES actors(id),  -- who answered (the viewer: the owner's actors row);
                                            -- answered_by keeps the text for older rows
  change_actor_id INTEGER REFERENCES actors(id)        -- who made the next status change (applied, withdrawn);
                                            -- copied into its event, then cleared
);
CREATE TABLE goal_sources (
  goal_id     INTEGER NOT NULL REFERENCES design_goals(id) ON DELETE RESTRICT,
  question_id INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  PRIMARY KEY (goal_id, question_id)
);
CREATE TABLE goal_statuses (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE,
  served   INTEGER NOT NULL CHECK (served IN (0,1))    -- given to reviewers, drafters and implementers (`goals`)
);
INSERT INTO "goal_statuses" VALUES('proposed','Waiting for your approval',1,0);
INSERT INTO "goal_statuses" VALUES('approved','Approved',2,1);
INSERT INTO "goal_statuses" VALUES('rejected','Not approved',3,0);
INSERT INTO "goal_statuses" VALUES('retired','Retired',4,0);
CREATE TABLE guard_tests (
  id                      INTEGER PRIMARY KEY,
  issue_id                INTEGER NOT NULL REFERENCES issues(id),
  repository_id           INTEGER REFERENCES repositories(id),
  test_path               TEXT NOT NULL,
  test_name               TEXT NOT NULL,
  step_id                 INTEGER REFERENCES steps(id),
  added_commit_sha        TEXT CHECK (added_commit_sha IS NULL OR ((length(added_commit_sha) = 40 OR length(added_commit_sha) = 64) AND added_commit_sha NOT GLOB '*[^0-9a-f]*')),
  failed_before_fix       INTEGER CHECK (failed_before_fix IN (0,1)),
  passes_after_fix        INTEGER CHECK (passes_after_fix IN (0,1)),
  still_exists            INTEGER NOT NULL DEFAULT 1 CHECK (still_exists IN (0,1)),
  last_confirmed_review_id INTEGER REFERENCES reviews(id),
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (issue_id, test_path, test_name)
);
CREATE TABLE interview_answers (
  id                   INTEGER PRIMARY KEY,
  question_id          INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  answer               TEXT NOT NULL CHECK (length(trim(answer)) > 0),   -- the owner's words, verbatim
  answered_via         TEXT NOT NULL DEFAULT 'chat' CHECK (answered_via IN ('chat','viewer')),
  answered_by_actor_id INTEGER REFERENCES actors(id),   -- who recorded it
  answered_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE interview_questions (
  id                INTEGER PRIMARY KEY,
  code              TEXT NOT NULL UNIQUE    -- IQ-0001
                      CHECK (code GLOB 'IQ-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'IQ-*[^0-9]*'),
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code       TEXT REFERENCES domains(code),   -- an approved domain of the review; NULL = the whole project
  question          TEXT NOT NULL CHECK (length(trim(question)) > 0),
  why_asked         TEXT NOT NULL CHECK (length(trim(why_asked)) > 0),  -- what in the code prompted it
  asked_by_actor_id INTEGER REFERENCES actors(id),
  gate_id           INTEGER REFERENCES gates(id),    -- asked in the hub too: the gate's answer is recorded here
  asked_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE issue_relations (
  id                       INTEGER PRIMARY KEY,
  from_issue_id            INTEGER NOT NULL REFERENCES issues(id),
  to_issue_id              INTEGER NOT NULL REFERENCES issues(id),
  relation                 TEXT NOT NULL REFERENCES relation_types(value),
  confidence               TEXT NOT NULL DEFAULT 'likely' CHECK (confidence IN ('certain','likely','possible')),
  rationale                TEXT NOT NULL,
  established_in_review_id INTEGER REFERENCES reviews(id),
  established_by_actor_id  INTEGER REFERENCES actors(id),
  created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (from_issue_id <> to_issue_id),
  UNIQUE (from_issue_id, to_issue_id, relation)
);
CREATE TABLE issue_statuses (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE,
  active   INTEGER NOT NULL CHECK (active IN (0,1)),    -- still to fix (v_open_issues, the "Still to fix" filter)
  resolved INTEGER NOT NULL CHECK (resolved IN (0,1)),  -- closed: entering it sets issues.resolved_at
  fixed    INTEGER NOT NULL CHECK (fixed IN (0,1)),     -- the code was fixed: a regression match or check regresses it
  reopens  INTEGER NOT NULL CHECK (reopens IN (0,1)),   -- closed without a fix: a recurrence reopens it
  briefed  INTEGER NOT NULL CHECK (briefed IN (0,1)),   -- given to reviewers as a known issue (not merged away)
  CHECK (active + resolved = 1),
  CHECK (fixed = 0 OR resolved = 1),
  CHECK (reopens = 0 OR (resolved = 1 AND fixed = 0))
);
INSERT INTO "issue_statuses" VALUES('open','Open',1,1,0,0,0,1);
INSERT INTO "issue_statuses" VALUES('needs-decision','Needs your decision',2,1,0,0,0,1);
INSERT INTO "issue_statuses" VALUES('planned','Planned',3,1,0,0,0,1);
INSERT INTO "issue_statuses" VALUES('in-progress','In progress',4,1,0,0,0,1);
INSERT INTO "issue_statuses" VALUES('fixed','Fixed',5,0,1,1,0,1);
INSERT INTO "issue_statuses" VALUES('verified','Fixed and confirmed',6,0,1,1,0,1);
INSERT INTO "issue_statuses" VALUES('regressed','Came back',7,1,0,0,0,1);
INSERT INTO "issue_statuses" VALUES('accepted-risk','Accepted risk',8,0,1,0,0,1);
INSERT INTO "issue_statuses" VALUES('wont-fix','Won''t fix',9,0,1,0,1,1);
INSERT INTO "issue_statuses" VALUES('duplicate','Duplicate',10,0,1,0,1,0);
INSERT INTO "issue_statuses" VALUES('obsolete','No longer applies',11,0,1,0,1,0);
CREATE TABLE issues (
  id                     INTEGER PRIMARY KEY,
  code                   TEXT NOT NULL UNIQUE    -- ISS-0001, never reused
                           CHECK (code GLOB 'ISS-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'ISS-*[^0-9]*'),
  title                  TEXT NOT NULL,
  domain_code            TEXT NOT NULL REFERENCES domains(code),
  theme_code             TEXT REFERENCES themes(code),
  sub                    TEXT,                   -- slug naming the specific thing
  kind                   TEXT NOT NULL CHECK (kind IN
                           ('defect','test','validation','refactor','feature','design','docs',
                            'logging','perf','security','ops','verify')),
  severity               TEXT NOT NULL REFERENCES severities(value),             -- current
  status                 TEXT NOT NULL DEFAULT 'open' REFERENCES issue_statuses(value),
  fingerprint            TEXT,                   -- domain:theme:sub, for matching
  before_state           TEXT,                   -- what the system did when first found
  current_state          TEXT,                   -- what the latest review observed
  after_state            TEXT,                   -- what it does once fixed
  recommendation         TEXT,                   -- current agreed fix
  resolution             TEXT,                   -- how it was closed, or why it was accepted
  first_seen_review_id   INTEGER NOT NULL REFERENCES reviews(id),
  last_seen_review_id    INTEGER REFERENCES reviews(id),   -- last review that raised it
  last_checked_review_id INTEGER REFERENCES reviews(id),   -- last review that looked for it
  times_seen             INTEGER NOT NULL DEFAULT 1 CHECK (times_seen >= 1),
  opened_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at            TEXT,
  verified_at            TEXT,
  updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status/severity change, set in the same UPDATE: change_note is required (the
  -- update is refused without it), the others where they apply. The triggers copy them into events, so
  -- history records who, why, in which review/step/commit, and then set all five back to NULL, so context
  -- is never reused by a later change.
  change_actor_id        INTEGER REFERENCES actors(id),
  change_review_id       INTEGER REFERENCES reviews(id),
  change_step_id         INTEGER REFERENCES steps(id),
  change_commit_sha      TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40
                                     OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note            TEXT
);
CREATE TABLE ledger_checks (
  view     TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  tier     TEXT NOT NULL CHECK (tier IN ('error','warning')),
  position INTEGER NOT NULL UNIQUE
);
INSERT INTO "ledger_checks" VALUES('v_unmatched_findings','Findings not matched to an issue','error',10);
INSERT INTO "ledger_checks" VALUES('v_unchecked_known_issues','Known issues in a domain that ran, with no check recorded','error',20);
INSERT INTO "ledger_checks" VALUES('v_untraceable_refs','Code references that cannot be traced to exact lines','error',30);
INSERT INTO "ledger_checks" VALUES('v_uncovered_findings','Findings no step of their plan closes','error',40);
INSERT INTO "ledger_checks" VALUES('v_coverage_problems','Steps closing nothing, or findings closed twice without a reason','error',50);
INSERT INTO "ledger_checks" VALUES('v_unguarded_fixes','Fixed issues with no guard test, or whose guard test is gone','warning',100);
INSERT INTO "ledger_checks" VALUES('v_unplanned_discoveries','Defects found during fix work that no plan step closes','error',60);
INSERT INTO "ledger_checks" VALUES('v_history_gaps','Issues and gates whose history is missing an event','error',70);
INSERT INTO "ledger_checks" VALUES('v_match_problems','Matches whose type disagrees with the issue','error',72);
INSERT INTO "ledger_checks" VALUES('v_off_plan_coverage','Steps closing a finding of a review their plan does not cover','error',74);
INSERT INTO "ledger_checks" VALUES('v_done_steps_open_issues','Done steps whose issues are still to do','error',76);
INSERT INTO "ledger_checks" VALUES('v_closed_without_reason','Issues accepted or declined with no reason recorded','error',78);
INSERT INTO "ledger_checks" VALUES('v_relation_problems','Related-to pairs stored in both directions','error',80);
INSERT INTO "ledger_checks" VALUES('v_check_finding_owner','Review checks citing a finding of another issue','error',82);
INSERT INTO "ledger_checks" VALUES('v_artifacts_outside_ledger','Review files recorded outside the ledger''s folder','warning',110);
INSERT INTO "ledger_checks" VALUES('v_unresolved_conflicts','Plan steps that touch the same thing, not judged yet','error',120);
INSERT INTO "ledger_checks" VALUES('v_late_step_conflicts','Steps added after approval that touch what other steps touch','warning',130);
CREATE TABLE match_types (
  value     TEXT PRIMARY KEY,
  label     TEXT NOT NULL,
  position  INTEGER NOT NULL UNIQUE,
  came_back INTEGER NOT NULL CHECK (came_back IN (0,1))  -- the issue was closed and was raised again
);
INSERT INTO "match_types" VALUES('new','New',1,0);
INSERT INTO "match_types" VALUES('still-present','Still present',2,0);
INSERT INTO "match_types" VALUES('changed','Changed',3,0);
INSERT INTO "match_types" VALUES('recurrence','Came back',4,1);
INSERT INTO "match_types" VALUES('regression','Came back after a fix',5,1);
INSERT INTO "match_types" VALUES('continuation','Continues an earlier issue',6,0);
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,                -- schema_version, project_name, created_at, ledger_location,
                                         -- created_by_skill, written_by_tool, schema_hash
  value TEXT NOT NULL
);
INSERT INTO "meta" VALUES('schema_version','16');
INSERT INTO "meta" VALUES('project_name','demo-v16');
INSERT INTO "meta" VALUES('created_at','2026-11-11T00:00:00.000Z');
INSERT INTO "meta" VALUES('written_by_tool','1.9.1');
INSERT INTO "meta" VALUES('schema_hash','64a86628293e2fc82f1d1559a5477ab8949f65cd583885db2b133edc713f5c49');
INSERT INTO "meta" VALUES('created_by_skill','1.9.1');
CREATE TABLE metric_defs (
  name   TEXT PRIMARY KEY,                 -- the stable key used in metrics.name
  label  TEXT NOT NULL,
  unit   TEXT,
  better TEXT NOT NULL DEFAULT 'none' CHECK (better IN ('higher','lower','none'))
);
INSERT INTO "metric_defs" VALUES('mutation_score','Mutation score','%','higher');
INSERT INTO "metric_defs" VALUES('line_coverage','Line coverage','%','higher');
INSERT INTO "metric_defs" VALUES('branch_coverage','Branch coverage','%','higher');
INSERT INTO "metric_defs" VALUES('test_cases_asserted','Test cases asserted','cases','higher');
INSERT INTO "metric_defs" VALUES('test_cases_needed','Test cases needed','cases','none');
INSERT INTO "metric_defs" VALUES('tests_missing_named','Missing tests named','tests','lower');
INSERT INTO "metric_defs" VALUES('flaky_tests','Flaky tests','tests','lower');
INSERT INTO "metric_defs" VALUES('test_suite_seconds','Test suite run time','s','lower');
INSERT INTO "metric_defs" VALUES('page_weight','Page weight','KB','lower');
INSERT INTO "metric_defs" VALUES('queries_per_page','Queries per page','queries','lower');
INSERT INTO "metric_defs" VALUES('p95_job_ms','Job time, 95th percentile','ms','lower');
INSERT INTO "metric_defs" VALUES('dependency_count','Dependencies','packages','lower');
INSERT INTO "metric_defs" VALUES('vulnerable_dependencies','Dependencies with known vulnerabilities','packages','lower');
INSERT INTO "metric_defs" VALUES('domain_agreement','Domains both runs chose','%','higher');
INSERT INTO "metric_defs" VALUES('finding_agreement','Issues both runs found','%','higher');
INSERT INTO "metric_defs" VALUES('severity_agreement','Shared issues given the same severity','%','higher');
CREATE TABLE metrics (
  id            INTEGER PRIMARY KEY,
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code   TEXT REFERENCES domains(code),
  repository_id INTEGER REFERENCES repositories(id),
  name          TEXT NOT NULL REFERENCES metric_defs(name),  -- stable key, e.g. 'mutation_score'
  scope         TEXT NOT NULL DEFAULT 'all',  -- module, page, job ...
  value         REAL NOT NULL,
  unit          TEXT,
  method        TEXT NOT NULL CHECK (method IN ('measured','counted','estimated')),
  note          TEXT,
  recorded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (review_id, name, scope, repository_id)
);
CREATE TABLE phases (
  id      INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  number  INTEGER NOT NULL,
  title   TEXT NOT NULL,
  goal    TEXT NOT NULL,                     -- one sentence
  why     TEXT,                              -- why it sits where it does
  UNIQUE (plan_id, number)
);
CREATE TABLE plan_reviews (
  plan_id   INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  PRIMARY KEY (plan_id, review_id)
);
CREATE TABLE plans (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE    -- P-0001
                         CHECK (code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'P-*[^0-9]*'),
  title                TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'drafting' CHECK (status IN
                         ('drafting','awaiting-approval','approved','in-progress','complete',
                          'superseded','abandoned'))
                         -- approved, in-progress and complete only once the owner approved (set approved_at with it)
                         CHECK (status IN ('drafting','awaiting-approval','superseded','abandoned')
                                OR approved_at IS NOT NULL),
  approved_by_actor_id INTEGER REFERENCES actors(id),
  approved_at          TEXT,
  superseded_by        INTEGER REFERENCES plans(id),
  doc_url              TEXT,
  page_url             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status change, copied into its event and then cleared; leaving superseded or
  -- abandoned needs change_note.
  change_actor_id      INTEGER REFERENCES actors(id),
  change_note          TEXT,
  narrative_md         TEXT                    -- the plan's narrative for the owner (Markdown: how to read it,
                                               -- why this order), shown in the viewer; `ledger.py set-narrative`
);
CREATE TABLE relation_types (
  value         TEXT PRIMARY KEY,
  label         TEXT NOT NULL,              -- read from the issue the relation starts at ("Duplicate of")
  inverse_label TEXT NOT NULL,              -- read from the issue it points to ("Has duplicate")
  position      INTEGER NOT NULL UNIQUE,
  closes        INTEGER NOT NULL CHECK (closes IN (0,1))  -- recording it closes the active issue as duplicate
);
INSERT INTO "relation_types" VALUES('duplicate-of','Duplicate of','Has duplicate',1,1);
INSERT INTO "relation_types" VALUES('continuation-of','Continues','Continued by',2,0);
INSERT INTO "relation_types" VALUES('regression-of','Regression of','Came back as',3,0);
INSERT INTO "relation_types" VALUES('related-to','Related to','Related to',4,0);
INSERT INTO "relation_types" VALUES('caused-by','Caused by','Causes',5,0);
INSERT INTO "relation_types" VALUES('blocks','Blocks','Blocked by',6,0);
INSERT INTO "relation_types" VALUES('supersedes','Supersedes','Superseded by',7,0);
INSERT INTO "relation_types" VALUES('split-from','Split from','Split into',8,0);
INSERT INTO "relation_types" VALUES('merged-into','Merged into','Absorbed',9,1);
CREATE TABLE repositories (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,   -- short name used in the brief
  path           TEXT,                   -- relative to the project root, '.' for the root repo
  remote_url     TEXT,
  default_branch TEXT,
  permalink_template TEXT CHECK (permalink_template IS NULL OR permalink_template GLOB 'https://*'
                                 OR permalink_template GLOB 'http://*'),
                                         -- e.g. https://github.com/o/r/blob/{sha}/{path}#L{start}-L{end}
  notes          TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  allow_outside_root INTEGER NOT NULL DEFAULT 0 CHECK (allow_outside_root IN (0,1)),
                                         -- 1 = the owner listed this repo explicitly as living outside the
                                         -- project root (a sibling checkout); its path may then be absolute or
                                         -- start with ../. Otherwise ledger.py refuses to read outside the root.
  -- path: no leading -, no control characters; and unless allow_outside_root, relative (no leading /, no
  -- backslash) with no .. segment
  CHECK (path IS NULL OR (path NOT GLOB '-*' AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*')
         AND (allow_outside_root = 1 OR (path NOT GLOB '/*' AND path NOT GLOB '*\*' AND path <> '..'
              AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'))))
);
CREATE TABLE requirements (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE    -- REQ-0001
                    CHECK (code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'REQ-*[^0-9]*'),
  text            TEXT NOT NULL,
  source          TEXT NOT NULL,          -- doc path, issue URL, "owner, chat 2026-11-10"
  source_location TEXT,
  stated_at       TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','withdrawn')),
  superseded_by   INTEGER REFERENCES requirements(id)   -- set exactly when status is superseded
                    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
  first_review_id INTEGER REFERENCES reviews(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE review_actors (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);
CREATE TABLE review_domains (
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code       TEXT NOT NULL REFERENCES domains(code),
  decision          TEXT NOT NULL CHECK (decision IN ('included','skipped')),
  reason            TEXT NOT NULL,
  owner_change      TEXT NOT NULL DEFAULT 'none' CHECK (owner_change IN
                      ('none','added-by-owner','removed-by-owner','modified-by-owner')),
  reviewers_planned INTEGER,
  split_note        TEXT,                 -- how the domain was split between reviewers
  proposed_by_rule  TEXT,                 -- the domain_rules rule (or 'open_issues >= 1') that proposed it
  PRIMARY KEY (review_id, domain_code)
);
CREATE TABLE review_environment (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  capability    TEXT NOT NULL CHECK (length(trim(capability)) > 0),   -- git, playwright, connector:figma ...
  available     INTEGER NOT NULL CHECK (available IN (0,1)),
  detail        TEXT,                    -- a version, a name, or where a connector is configured
  how_to_enable TEXT NOT NULL,           -- what to install or do to have it
  position      INTEGER NOT NULL,        -- the order `env` lists them in
  PRIMARY KEY (review_id, capability)
);
CREATE TABLE review_goals (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);
CREATE TABLE review_issue_checks (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id),
  outcome    TEXT NOT NULL CHECK (outcome IN
               ('still-present','changed','fixed','regressed','obsolete','not-checked')),
  finding_id INTEGER REFERENCES findings(id),
  evidence   TEXT,
  actor_id   INTEGER REFERENCES actors(id),
  checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (review_id, issue_id)
);
CREATE TABLE review_repositories (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                   AND commit_sha NOT GLOB '*[^0-9a-f]*'),  -- full hash, lowercase hex
  branch        TEXT,
  commit_time   TEXT,                     -- the commit's committer time in UTC (…Z, like every time here; a git
                                          -- time with an offset is converted); tag-review fills it when NULL
  commit_subject TEXT,
  tag_name      TEXT,                     -- e.g. review/R-0001, keeps the commit reachable
  tag_pushed    INTEGER NOT NULL DEFAULT 0 CHECK (tag_pushed IN (0,1)),
  is_dirty      INTEGER NOT NULL DEFAULT 0 CHECK (is_dirty IN (0,1)),
  dirty_note    TEXT,                     -- what was uncommitted, if anything
  dirty_patch_path TEXT,                  -- saved `git diff` of uncommitted changes, if any
  PRIMARY KEY (review_id, repository_id)
);
CREATE TABLE review_requirement_checks (
  id                INTEGER PRIMARY KEY,
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  requirement_id    INTEGER NOT NULL REFERENCES requirements(id),
  verdict           TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  where_implemented TEXT,                  -- file, function or page that implements it
  tested            TEXT CHECK (tested IS NULL OR tested IN ('yes','partly','no')),
  finding_id        INTEGER REFERENCES findings(id),   -- the finding raised for it, if any
  evidence          TEXT,
  UNIQUE (review_id, requirement_id)
);
CREATE TABLE reviewer_runs (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code    TEXT NOT NULL REFERENCES domains(code),
  actor_id       INTEGER NOT NULL REFERENCES actors(id),
  label          TEXT NOT NULL,           -- names the run within its review: 'tests-api'
  scope          TEXT NOT NULL,
  prompt         TEXT,
  report_path    TEXT,
  report_sha256  TEXT,
  status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                   ('queued','running','finished','failed','cancelled')),
  items_reviewed INTEGER CHECK (items_reviewed IS NULL OR items_reviewed >= 0),
  high_count     INTEGER CHECK (high_count IS NULL OR high_count >= 0),
  medium_count   INTEGER CHECK (medium_count IS NULL OR medium_count >= 0),
  low_count      INTEGER CHECK (low_count IS NULL OR low_count >= 0),
  test_count     INTEGER CHECK (test_count IS NULL OR test_count >= 0),
  final_message  TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  UNIQUE (review_id, label)
);
CREATE TABLE reviews (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE    -- R-0001
                          CHECK (code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'R-*[^0-9]*'),
  title                 TEXT NOT NULL,
  scope                 TEXT NOT NULL CHECK (scope IN
                          ('full','partial','targeted','second','fix-verification')),
  status                TEXT NOT NULL DEFAULT 'planning' CHECK (status IN
                          ('planning','awaiting-approval','approved','running','verifying',
                           'indexing','consolidating','handed-off','abandoned'))
                          -- past awaiting-approval only once the owner approved (set approved_at with it)
                          CHECK (status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NOT NULL),
  request_text          TEXT,                   -- what the owner asked for, in their words
  requested_by_actor_id INTEGER REFERENCES actors(id),
  manager_actor_id      INTEGER REFERENCES actors(id),
  approved_by_actor_id  INTEGER REFERENCES actors(id),
  approved_at           TEXT,
  previous_review_id    INTEGER REFERENCES reviews(id),  -- the review this one follows up
  skill_version         TEXT,
  review_folder         TEXT,                   -- scratch folder used (may not persist)
  summary_doc_url       TEXT CHECK (summary_doc_url IS NULL OR summary_doc_url GLOB 'https://*'
                                    OR summary_doc_url GLOB 'http://*'),
  verdict               TEXT,
  summary               TEXT,
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at          TEXT CHECK (completed_at IS NULL OR julianday(started_at) <= julianday(completed_at)),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status change, copied into its event and then cleared (like issues.change_*);
  -- leaving handed-off or abandoned needs change_note.
  change_actor_id       INTEGER REFERENCES actors(id),
  change_note           TEXT,
  report_md             TEXT                    -- the review document for the owner (Markdown), shown in the
                                                -- viewer; written in Step 6 with `ledger.py set-report`
);
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "schema_migrations" VALUES(1,'initial ledger schema','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(2,'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(3,'integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(4,'check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(5,'one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(6,'lookup tables for the ledger''s words and labels; RESTRICT keys under reviews and findings; metrics.name catalogued','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(7,'must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(8,'finding_locations.source (the reviewed text of a dirty working tree); v_next_codes for the write commands','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(9,'owner interview (interview_questions, interview_answers) and per-domain design goals the owner approves','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(10,'v_plan_totals, v_review_totals and v_pending_gates: the counts the viewer''s lists show','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(11,'gate answer rules in the trigger, owner queue context, event labels, the artifact check','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(12,'review_environment: what each review''s environment offered (tools, the harness, its connectors)','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(13,'domain_rules: the rule by which scan proposes each domain; review_domains.proposed_by_rule','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(14,'domain_checks and checklist_answers: each domain''s checklist, and the answers each report gave','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(15,'the agreement metrics compare-runs records: domain_agreement, finding_agreement, severity_agreement','2026-11-14T16:28:36.018Z');
INSERT INTO "schema_migrations" VALUES(16,'step_touches and step_conflicts: what each plan step touches, and the pairs of steps that touch the same thing','2026-11-14T16:28:36.018Z');
CREATE TABLE severities (
  value    TEXT PRIMARY KEY,                 -- H, M, L, T
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE           -- most severe first
);
INSERT INTO "severities" VALUES('H','High',1);
INSERT INTO "severities" VALUES('M','Medium',2);
INSERT INTO "severities" VALUES('L','Low',3);
INSERT INTO "severities" VALUES('T','Missing test',4);
ANALYZE "sqlite_master";
INSERT INTO "sqlite_stat1" VALUES('metric_defs','sqlite_autoindex_metric_defs_1','16 1');
INSERT INTO "sqlite_stat1" VALUES('ledger_checks','sqlite_autoindex_ledger_checks_2','17 1');
INSERT INTO "sqlite_stat1" VALUES('ledger_checks','sqlite_autoindex_ledger_checks_1','17 1');
INSERT INTO "sqlite_stat1" VALUES('issue_statuses','sqlite_autoindex_issue_statuses_2','11 1');
INSERT INTO "sqlite_stat1" VALUES('issue_statuses','sqlite_autoindex_issue_statuses_1','11 1');
INSERT INTO "sqlite_stat1" VALUES('goal_statuses','sqlite_autoindex_goal_statuses_2','4 1');
INSERT INTO "sqlite_stat1" VALUES('goal_statuses','sqlite_autoindex_goal_statuses_1','4 1');
INSERT INTO "sqlite_stat1" VALUES('meta','sqlite_autoindex_meta_1','7 1');
INSERT INTO "sqlite_stat1" VALUES('severities','sqlite_autoindex_severities_2','4 1');
INSERT INTO "sqlite_stat1" VALUES('severities','sqlite_autoindex_severities_1','4 1');
INSERT INTO "sqlite_stat1" VALUES('match_types','sqlite_autoindex_match_types_2','6 1');
INSERT INTO "sqlite_stat1" VALUES('match_types','sqlite_autoindex_match_types_1','6 1');
INSERT INTO "sqlite_stat1" VALUES('domains','sqlite_autoindex_domains_1','27 1');
INSERT INTO "sqlite_stat1" VALUES('gate_kinds','sqlite_autoindex_gate_kinds_2','10 1');
INSERT INTO "sqlite_stat1" VALUES('gate_kinds','sqlite_autoindex_gate_kinds_1','10 1');
INSERT INTO "sqlite_stat1" VALUES('enum_labels','sqlite_autoindex_enum_labels_2','203 6 1');
INSERT INTO "sqlite_stat1" VALUES('enum_labels','sqlite_autoindex_enum_labels_1','203 6 1');
INSERT INTO "sqlite_stat1" VALUES('domain_rules','sqlite_autoindex_domain_rules_1','27 1');
INSERT INTO "sqlite_stat1" VALUES('relation_types','sqlite_autoindex_relation_types_2','9 1');
INSERT INTO "sqlite_stat1" VALUES('relation_types','sqlite_autoindex_relation_types_1','9 1');
INSERT INTO "sqlite_stat1" VALUES('domain_checks','ix_fk_domain_checks_severity','115 39');
INSERT INTO "sqlite_stat1" VALUES('domain_checks','ix_fk_domain_checks_domain_code','115 5');
INSERT INTO "sqlite_stat1" VALUES('domain_checks','sqlite_autoindex_domain_checks_2','115 5 1');
INSERT INTO "sqlite_stat1" VALUES('domain_checks','sqlite_autoindex_domain_checks_1','115 1');
INSERT INTO "sqlite_stat1" VALUES('themes','sqlite_autoindex_themes_1','44 1');
CREATE TABLE step_commits (
  id            INTEGER PRIMARY KEY,
  step_id       INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  repository_id INTEGER REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                   AND commit_sha NOT GLOB '*[^0-9a-f]*'),  -- full hash, lowercase hex
  branch        TEXT,
  pr_url        TEXT CHECK (pr_url IS NULL OR pr_url GLOB 'https://*' OR pr_url GLOB 'http://*'),
  summary       TEXT,
  committed_at  TEXT,
  UNIQUE (step_id, commit_sha)
);
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
CREATE TABLE step_dependencies (
  step_id            INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  depends_on_step_id INTEGER NOT NULL REFERENCES steps(id),
  PRIMARY KEY (step_id, depends_on_step_id),
  CHECK (step_id <> depends_on_step_id)
);
CREATE TABLE step_findings (
  step_id      INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  finding_id   INTEGER NOT NULL REFERENCES findings(id),
  multi_reason TEXT,          -- required when a finding is closed by more than one step
  PRIMARY KEY (step_id, finding_id)
);
CREATE TABLE step_goals (
  step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  goal_id INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (step_id, goal_id)
);
CREATE TABLE step_touches (
  step_id   INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('file','symbol','table','column','setting','route','behaviour')),
  target    TEXT NOT NULL CHECK (length(trim(target)) > 0),   -- demo:src/app.py, findings.severity, GET /api/x
  direction TEXT NOT NULL DEFAULT 'changes' CHECK (direction IN
              ('adds','removes','tightens','loosens','renames','changes')),
  source    TEXT NOT NULL CHECK (source IN ('declared','finding')),   -- steps.jsonl, or a closed finding's location
  PRIMARY KEY (step_id, kind, target, direction, source)
);
CREATE TABLE steps (
  id                INTEGER PRIMARY KEY,
  plan_id           INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  phase_id          INTEGER NOT NULL REFERENCES phases(id),
  number            INTEGER NOT NULL,        -- global within the plan
  section           TEXT,
  title             TEXT NOT NULL,           -- imperative
  change            TEXT NOT NULL,           -- what to change
  proof             TEXT NOT NULL,           -- test or check that proves it
  owner_need        TEXT NOT NULL DEFAULT 'none' CHECK (owner_need IN
                      ('none','decision','confirm','review','production')),
  owner_question    TEXT,
  behavior_change   INTEGER NOT NULL DEFAULT 0 CHECK (behavior_change IN (0,1)),
  behavior_note     TEXT,
  status            TEXT NOT NULL DEFAULT 'not-started' CHECK (status IN
                      ('not-started','doing','waiting-owner','done','dropped')),
  progress_note     TEXT,                    -- written for the owner, plain sentences
  drafter_key       TEXT,
  started_at        TEXT,
  done_at           TEXT,
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- Context for the next status change: change_note is required (on waiting-owner, owner_question will do);
  -- copied into the event, then cleared.
  change_actor_id   INTEGER REFERENCES actors(id),
  change_commit_sha TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40 OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note       TEXT,
  no_findings_reason TEXT,                  -- why this step closes no finding by design (e.g. the follow-up
                                            -- review that verifies the plan); v_coverage_problems skips it
  UNIQUE (plan_id, number)
);
CREATE TABLE test_runs (
  id            INTEGER PRIMARY KEY,
  review_id     INTEGER REFERENCES reviews(id),
  step_id       INTEGER REFERENCES steps(id),
  repository_id INTEGER REFERENCES repositories(id),
  commit_sha    TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64) AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  command       TEXT NOT NULL,
  purpose       TEXT NOT NULL CHECK (purpose IN
                  ('baseline','targeted','flake-check','mutation','before-fix','after-fix','ci')),
  passed        INTEGER,
  failed        INTEGER,
  skipped       INTEGER,
  errored       INTEGER,
  exit_code     INTEGER,
  duration_ms   INTEGER,
  output_path   TEXT,
  notes         TEXT,
  started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE themes (
  code       TEXT PRIMARY KEY,           -- 'authorization', 'logging-audit' ...
  name       TEXT NOT NULL,
  retired_at TEXT
);
INSERT INTO "themes" VALUES('spec-mismatch','Spec mismatch',NULL);
INSERT INTO "themes" VALUES('sign-in','Sign-in and sessions',NULL);
INSERT INTO "themes" VALUES('authorization','Authorization',NULL);
INSERT INTO "themes" VALUES('ops-security','Operations security',NULL);
INSERT INTO "themes" VALUES('privacy','Privacy and retention',NULL);
INSERT INTO "themes" VALUES('access','Access to links and pages',NULL);
INSERT INTO "themes" VALUES('validation','Input validation',NULL);
INSERT INTO "themes" VALUES('test-gaps','Test gaps',NULL);
INSERT INTO "themes" VALUES('test-quality','Test suite quality',NULL);
INSERT INTO "themes" VALUES('time','Time and dates',NULL);
INSERT INTO "themes" VALUES('concurrency','Concurrency',NULL);
INSERT INTO "themes" VALUES('failure-handling','Failure handling and idempotency',NULL);
INSERT INTO "themes" VALUES('compatibility','Compatibility and migrations',NULL);
INSERT INTO "themes" VALUES('defect','Defects',NULL);
INSERT INTO "themes" VALUES('decision-placement','Decision placement',NULL);
INSERT INTO "themes" VALUES('contracts','Inter-component contracts',NULL);
INSERT INTO "themes" VALUES('workflow','Approvals and workflow flows',NULL);
INSERT INTO "themes" VALUES('db-integrity','Database integrity, history, operations',NULL);
INSERT INTO "themes" VALUES('client-api','Client API',NULL);
INSERT INTO "themes" VALUES('generic','Generic text and logic',NULL);
INSERT INTO "themes" VALUES('config-display','Display of configuration',NULL);
INSERT INTO "themes" VALUES('internal-names','Internal names on screen',NULL);
INSERT INTO "themes" VALUES('select-not-type','Select, not type',NULL);
INSERT INTO "themes" VALUES('usability','Usability and accessibility',NULL);
INSERT INTO "themes" VALUES('task-flows','Task flows',NULL);
INSERT INTO "themes" VALUES('shared-frontend','Shared front end',NULL);
INSERT INTO "themes" VALUES('shared-backend','Shared backend code',NULL);
INSERT INTO "themes" VALUES('perf-backend','Performance: backend',NULL);
INSERT INTO "themes" VALUES('perf-db','Performance: database',NULL);
INSERT INTO "themes" VALUES('perf-frontend','Performance: front end',NULL);
INSERT INTO "themes" VALUES('cost','Cost',NULL);
INSERT INTO "themes" VALUES('email-jobs-feeds','Email, jobs and feeds',NULL);
INSERT INTO "themes" VALUES('logging','Logging and audit',NULL);
INSERT INTO "themes" VALUES('monitoring','Alerting and monitoring',NULL);
INSERT INTO "themes" VALUES('ci-release','CI and release',NULL);
INSERT INTO "themes" VALUES('backup-recovery','Backup and recovery',NULL);
INSERT INTO "themes" VALUES('dependencies','Dependencies',NULL);
INSERT INTO "themes" VALUES('docs-inline','Inline developer docs',NULL);
INSERT INTO "themes" VALUES('docs-repo','Repo developer docs',NULL);
INSERT INTO "themes" VALUES('docs-user','End-user docs',NULL);
INSERT INTO "themes" VALUES('handover','Handover and ownership',NULL);
INSERT INTO "themes" VALUES('localization','Localization',NULL);
INSERT INTO "themes" VALUES('verify','Verify',NULL);
INSERT INTO "themes" VALUES('other','Other',NULL);
CREATE TRIGGER tr_metric_defined AFTER INSERT ON metrics BEGIN
  INSERT OR IGNORE INTO metric_defs(name, label, unit)
    VALUES (NEW.name, upper(substr(NEW.name, 1, 1)) || replace(substr(NEW.name, 2), '_', ' '), NEW.unit);
END;
CREATE TRIGGER tr_metric_renamed AFTER UPDATE OF name ON metrics BEGIN
  INSERT OR IGNORE INTO metric_defs(name, label, unit)
    VALUES (NEW.name, upper(substr(NEW.name, 1, 1)) || replace(substr(NEW.name, 2), '_', ' '), NEW.unit);
END;
CREATE INDEX ix_findings_review      ON findings(review_id);
CREATE INDEX ix_findings_review_counts ON findings(review_id, verification, severity, match_type);
CREATE INDEX ix_findings_issue       ON findings(issue_id);
CREATE INDEX ix_findings_fingerprint ON findings(fingerprint);
CREATE INDEX ix_findings_slug        ON findings(domain_code, theme_code, sub);
CREATE INDEX ix_locations_finding    ON finding_locations(finding_id);
CREATE INDEX ix_locations_path       ON finding_locations(path, symbol);
CREATE INDEX ix_issues_status        ON issues(status);
CREATE INDEX ix_issues_fingerprint   ON issues(fingerprint);
CREATE INDEX ix_issues_slug          ON issues(domain_code, theme_code, sub);
CREATE INDEX ix_relations_to         ON issue_relations(to_issue_id);
CREATE INDEX ix_checks_issue         ON review_issue_checks(issue_id);
CREATE INDEX ix_steps_phase          ON steps(phase_id);
CREATE INDEX ix_steps_status         ON steps(plan_id, status);
CREATE INDEX ix_step_findings_find   ON step_findings(finding_id);
CREATE INDEX ix_deps_on              ON step_dependencies(depends_on_step_id);
CREATE INDEX ix_guard_issue          ON guard_tests(issue_id);
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);
CREATE INDEX ix_metrics_name         ON metrics(name, scope);
CREATE INDEX ix_runs_review          ON reviewer_runs(review_id);
CREATE INDEX ix_gates_status         ON gates(status);
CREATE UNIQUE INDEX ux_metrics_key            ON metrics(review_id, name, scope, COALESCE(repository_id, 0));
CREATE UNIQUE INDEX ux_locations_one_primary  ON finding_locations(finding_id) WHERE role = 'primary';
CREATE UNIQUE INDEX ux_repositories_path      ON repositories(path);
CREATE INDEX ix_fk_reviews_change_actor_id    ON reviews(change_actor_id);
CREATE INDEX ix_fk_plans_change_actor_id      ON plans(change_actor_id);
CREATE INDEX ix_fk_gates_change_actor_id      ON gates(change_actor_id);
CREATE INDEX ix_fk_gates_answered_by_actor_id ON gates(answered_by_actor_id);
CREATE INDEX ix_fk_actors_parent_actor_id            ON actors(parent_actor_id);
CREATE INDEX ix_fk_design_goals_superseded_by        ON design_goals(superseded_by);
CREATE INDEX ix_fk_design_goals_domain_code          ON design_goals(domain_code);
CREATE INDEX ix_fk_design_goals_status               ON design_goals(status);
CREATE INDEX ix_fk_design_goals_approved_by_actor_id ON design_goals(approved_by_actor_id);
CREATE INDEX ix_fk_design_goals_review_id            ON design_goals(review_id);
CREATE INDEX ix_fk_design_goals_gate_id              ON design_goals(gate_id);
CREATE INDEX ix_fk_design_goals_change_actor_id      ON design_goals(change_actor_id);
CREATE INDEX ix_fk_interview_questions_review_id         ON interview_questions(review_id);
CREATE INDEX ix_fk_interview_questions_domain_code       ON interview_questions(domain_code);
CREATE INDEX ix_fk_interview_questions_asked_by_actor_id ON interview_questions(asked_by_actor_id);
CREATE INDEX ix_fk_interview_questions_gate_id           ON interview_questions(gate_id);
CREATE INDEX ix_fk_interview_answers_question_id          ON interview_answers(question_id);
CREATE INDEX ix_fk_interview_answers_answered_by_actor_id ON interview_answers(answered_by_actor_id);
CREATE INDEX ix_fk_goal_sources_question_id ON goal_sources(question_id);
CREATE INDEX ix_fk_step_goals_goal_id ON step_goals(goal_id);
CREATE INDEX ix_fk_reviews_approved_by_actor_id      ON reviews(approved_by_actor_id);
CREATE INDEX ix_fk_reviews_manager_actor_id          ON reviews(manager_actor_id);
CREATE INDEX ix_fk_reviews_previous_review_id        ON reviews(previous_review_id);
CREATE INDEX ix_fk_reviews_requested_by_actor_id     ON reviews(requested_by_actor_id);
CREATE INDEX ix_fk_review_repositories_repository_id ON review_repositories(repository_id);
CREATE INDEX ix_fk_review_goals_goal_id              ON review_goals(goal_id);
CREATE INDEX ix_fk_domain_checks_domain_code       ON domain_checks(domain_code);
CREATE INDEX ix_fk_domain_checks_severity          ON domain_checks(severity);
CREATE INDEX ix_fk_checklist_answers_check_code    ON checklist_answers(check_code);
CREATE INDEX ix_step_touches_target                  ON step_touches(kind, target);
CREATE INDEX ix_fk_step_conflicts_step_b_id           ON step_conflicts(step_b_id);
CREATE INDEX ix_fk_step_conflicts_late_step_id        ON step_conflicts(late_step_id);
CREATE INDEX ix_fk_step_conflicts_gate_id             ON step_conflicts(gate_id);
CREATE INDEX ix_fk_step_conflicts_resolved_by_actor_id ON step_conflicts(resolved_by_actor_id);
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
CREATE INDEX ix_fk_findings_discovered_in_step_id    ON findings(discovered_in_step_id);
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
CREATE INDEX ix_fk_issues_severity                   ON issues(severity);
CREATE INDEX ix_fk_issue_relations_relation          ON issue_relations(relation);
CREATE INDEX ix_fk_findings_severity                 ON findings(severity);
CREATE INDEX ix_fk_findings_match_type               ON findings(match_type);
CREATE INDEX ix_fk_gates_kind                        ON gates(kind);
CREATE TRIGGER tr_issue_code BEFORE INSERT ON issues
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'ISS-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'ISS-*[^0-9]*' AND (
     NEW.code IS NOT 'ISS-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER))
  OR CAST(substr(NEW.code, 5) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 5) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM issues
        WHERE code = 'ISS-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'issue codes are sequential: ISS- and the next number (ISS-0001, ISS-0002 ...)');
END;
CREATE TRIGGER tr_issue_code_fixed BEFORE UPDATE OF code ON issues WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_issue_domain_active BEFORE INSERT ON issues
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;
CREATE TRIGGER tr_issue_opened AFTER INSERT ON issues BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'opened', NEW.status, NEW.change_actor_id,
          NEW.first_seen_review_id, NEW.change_step_id, NEW.change_commit_sha, NEW.change_note);
END;
CREATE TRIGGER tr_issue_change_needs_note BEFORE UPDATE OF status, severity ON issues
WHEN (OLD.status IS NOT NEW.status OR OLD.severity IS NOT NEW.severity) AND NEW.change_note IS NULL BEGIN
  SELECT RAISE(ABORT, 'give change_note with a status or severity change');
END;
CREATE TRIGGER tr_issue_fixed_needs_after BEFORE UPDATE OF status ON issues
WHEN NEW.status = 'fixed' AND OLD.status IS NOT 'fixed' AND NEW.after_state IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue set fixed needs after_state (what the system does now)');
END;
CREATE TRIGGER tr_issue_closed_needs_reason BEFORE UPDATE OF status, resolution ON issues
WHEN NEW.status IN ('accepted-risk','wont-fix') AND NULLIF(trim(NEW.resolution), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue closed as accepted-risk or wont-fix needs a resolution (the reason)');
END;
CREATE TRIGGER tr_issue_closed_needs_reason_ins BEFORE INSERT ON issues
WHEN NEW.status IN ('accepted-risk','wont-fix') AND NULLIF(trim(NEW.resolution), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'an issue closed as accepted-risk or wont-fix needs a resolution (the reason)');
END;
CREATE TRIGGER tr_issue_verified_needs_check BEFORE UPDATE OF status ON issues
WHEN NEW.status = 'verified' AND OLD.status IS NOT 'verified'
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c JOIN reviews r ON r.id = c.review_id
                  WHERE c.issue_id = NEW.id AND c.review_id = NEW.change_review_id AND c.outcome = 'fixed'
                    AND julianday(r.started_at) > (SELECT max(julianday(x.started_at)) FROM reviews x
                                                    WHERE x.id IN (OLD.first_seen_review_id,
                                                                   OLD.last_seen_review_id))) BEGIN
  SELECT RAISE(ABORT, 'an issue is verified only by a later review''s fixed check (record it in review_issue_checks)');
END;
CREATE TRIGGER tr_issue_status AFTER UPDATE OF status ON issues
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.change_actor_id, NEW.change_review_id, NEW.change_step_id, NEW.change_commit_sha,
          CASE WHEN NEW.status = 'regressed' AND NEW.after_state IS NOT NULL
               THEN COALESCE(NEW.change_note || ' ', '') || '(after_state was: ' || NEW.after_state || ')'
               ELSE NEW.change_note END);
  UPDATE issues SET
    resolved_at = CASE WHEN NEW.status = 'verified' THEN COALESCE(resolved_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                       WHEN NEW.status IN (SELECT value FROM issue_statuses WHERE resolved = 1)
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       ELSE NULL END,
    verified_at = CASE WHEN NEW.status = 'verified' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
    after_state = CASE WHEN NEW.status = 'regressed' THEN NULL ELSE after_state END
  WHERE id = NEW.id;
END;
CREATE TRIGGER tr_issue_severity AFTER UPDATE OF severity ON issues
WHEN OLD.severity IS NOT NEW.severity BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, commit_sha, note)
  VALUES ('issue', NEW.id, 'severity-changed', OLD.severity, NEW.severity,
          NEW.change_actor_id, NEW.change_review_id, NEW.change_step_id,
          NEW.change_commit_sha, NEW.change_note);
END;
CREATE TRIGGER tr_issue_title_changed AFTER UPDATE OF title ON issues WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.change_review_id,
          NEW.change_step_id, NEW.change_note);
END;
CREATE TRIGGER tr_issue_before_changed AFTER UPDATE OF before_state ON issues
WHEN OLD.before_state IS NOT NEW.before_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'before-state-changed', OLD.before_state, NEW.before_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;
CREATE TRIGGER tr_issue_current_changed AFTER UPDATE OF current_state ON issues
WHEN OLD.current_state IS NOT NEW.current_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'current-state-changed', OLD.current_state, NEW.current_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;
CREATE TRIGGER tr_issue_after_changed AFTER UPDATE OF after_state ON issues
WHEN OLD.after_state IS NOT NEW.after_state BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'after-state-changed', OLD.after_state, NEW.after_state, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;
CREATE TRIGGER tr_issue_recommendation_changed AFTER UPDATE OF recommendation ON issues
WHEN OLD.recommendation IS NOT NEW.recommendation BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'recommendation-changed', OLD.recommendation, NEW.recommendation, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;
CREATE TRIGGER tr_issue_resolution_changed AFTER UPDATE OF resolution ON issues
WHEN OLD.resolution IS NOT NEW.resolution BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('issue', NEW.id, 'resolution-changed', OLD.resolution, NEW.resolution, NEW.change_actor_id,
          NEW.change_review_id, NEW.change_step_id, NEW.change_note);
END;
CREATE TRIGGER tr_issue_touch AFTER UPDATE ON issues
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE issues SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
CREATE TRIGGER tr_issue_context_cleared AFTER UPDATE ON issues
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_review_id IS NOT NULL OR NEW.change_step_id IS NOT NULL
  OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE issues SET change_actor_id = NULL, change_review_id = NULL, change_step_id = NULL,
                    change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_issue_context_cleared_ins AFTER INSERT ON issues
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_review_id IS NOT NULL OR NEW.change_step_id IS NOT NULL
  OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE issues SET change_actor_id = NULL, change_review_id = NULL, change_step_id = NULL,
                    change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_issue_no_delete BEFORE DELETE ON issues BEGIN
  SELECT RAISE(ABORT, 'history is append-only: issues cannot be deleted; close one with a status instead');
END;
CREATE TRIGGER tr_finding_insert_unmatched BEFORE INSERT ON findings WHEN NEW.issue_id IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'insert the finding first, then set issue_id (matching runs on that UPDATE)');
END;
CREATE TRIGGER tr_finding_review_approved BEFORE INSERT ON findings
WHEN (SELECT status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NULL
        FROM reviews WHERE id = NEW.review_id) BEGIN
  SELECT RAISE(ABORT, 'findings need an approved review (approved_at set, status past awaiting-approval)');
END;
CREATE TRIGGER tr_finding_run_same_review BEFORE INSERT ON findings
WHEN NEW.reviewer_run_id IS NOT NULL
 AND (SELECT review_id FROM reviewer_runs WHERE id = NEW.reviewer_run_id) IS NOT NEW.review_id BEGIN
  SELECT RAISE(ABORT, 'the reviewer run belongs to another review');
END;
CREATE TRIGGER tr_finding_domain_active BEFORE INSERT ON findings
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;
CREATE TRIGGER tr_finding_test_severity BEFORE INSERT ON findings
WHEN NEW.severity = 'T' AND NEW.kind IS NOT 'test' BEGIN
  SELECT RAISE(ABORT, 'severity T is for a missing test: give the finding kind test, or another severity');
END;
CREATE TRIGGER tr_finding_created AFTER INSERT ON findings BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('finding', NEW.id, 'created', NEW.point_id, NULL, NEW.review_id, NEW.summary);
END;
CREATE TRIGGER tr_finding_immutable BEFORE UPDATE ON findings
WHEN OLD.review_id IS NOT NEW.review_id OR OLD.reviewer_run_id IS NOT NEW.reviewer_run_id
  OR OLD.point_id IS NOT NEW.point_id OR OLD.severity IS NOT NEW.severity OR OLD.kind IS NOT NEW.kind
  OR OLD.domain_code IS NOT NEW.domain_code OR OLD.theme_code IS NOT NEW.theme_code OR OLD.sub IS NOT NEW.sub
  OR OLD.summary IS NOT NEW.summary OR OLD.detail IS NOT NEW.detail OR OLD.before_state IS NOT NEW.before_state
  OR OLD.recommendation IS NOT NEW.recommendation OR OLD.recommended_test IS NOT NEW.recommended_test
  OR OLD.evidence IS NOT NEW.evidence OR OLD.where_report IS NOT NEW.where_report
  OR OLD.fingerprint IS NOT NEW.fingerprint OR OLD.related_point_ids IS NOT NEW.related_point_ids
  OR OLD.created_at IS NOT NEW.created_at BEGIN
  SELECT RAISE(ABORT, 'history is append-only: only issue_id, match_* and verification* of a finding can change');
END;
CREATE TRIGGER tr_finding_no_delete BEFORE DELETE ON findings BEGIN
  SELECT RAISE(ABORT, 'history is append-only: findings cannot be deleted; mark a wrong one verification = false');
END;
CREATE TRIGGER tr_finding_regression_guard BEFORE UPDATE OF issue_id, match_type ON findings
WHEN NEW.match_type = 'regression' AND NEW.issue_id IS NOT NULL
 AND (SELECT status FROM issues WHERE id = NEW.issue_id) NOT IN (SELECT value FROM issue_statuses WHERE fixed = 1 OR value = 'regressed') BEGIN
  SELECT RAISE(ABORT, 'match_type regression needs a fixed or verified issue; use recurrence, still-present or changed');
END;
CREATE TRIGGER tr_finding_match_vs_fixed_check BEFORE UPDATE OF issue_id ON findings
WHEN NEW.issue_id IS NOT NULL AND OLD.issue_id IS NOT NEW.issue_id AND NEW.verification <> 'false'
 AND EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = NEW.review_id AND c.issue_id = NEW.issue_id
               AND c.outcome = 'fixed') BEGIN
  SELECT RAISE(ABORT, 'this review recorded the issue fixed; a finding of the same review cannot be matched to it');
END;
CREATE TRIGGER tr_finding_matched AFTER UPDATE OF issue_id ON findings
WHEN OLD.issue_id IS NOT NEW.issue_id BEGIN
  UPDATE issues SET
    times_seen = max(1, (SELECT count(DISTINCT f.review_id) FROM findings f
                          WHERE f.issue_id = OLD.issue_id AND f.verification <> 'false')),
    last_seen_review_id = (SELECT f.review_id FROM findings f JOIN reviews r ON r.id = f.review_id
                            WHERE f.issue_id = OLD.issue_id AND f.verification <> 'false'
                            ORDER BY r.started_at DESC, r.id DESC LIMIT 1),
    last_checked_review_id = (SELECT x.rid FROM (
                                SELECT f.review_id AS rid FROM findings f
                                 WHERE f.issue_id = OLD.issue_id AND f.verification <> 'false'
                                UNION SELECT c.review_id FROM review_issue_checks c WHERE c.issue_id = OLD.issue_id) x
                              JOIN reviews r ON r.id = x.rid ORDER BY r.started_at DESC, r.id DESC LIMIT 1)
  WHERE OLD.issue_id IS NOT NULL AND id = OLD.issue_id;
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, note)
  SELECT 'issue', OLD.issue_id, 'unlinked', NEW.point_id, (SELECT code FROM issues WHERE id = NEW.issue_id),
         NEW.review_id, 'finding ' || NEW.point_id || ' no longer matched to this issue'
  WHERE OLD.issue_id IS NOT NULL;
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, note)
  SELECT 'issue', NEW.issue_id, 'recurred', status, NEW.match_type, NEW.review_id,
         'raised again by finding ' || NEW.point_id || ' while ' || status
    FROM issues WHERE NEW.issue_id IS NOT NULL AND id = NEW.issue_id AND status = 'accepted-risk'
     AND NEW.verification <> 'false' AND NEW.match_type IN (SELECT value FROM match_types WHERE came_back = 1);
  UPDATE issues SET
    times_seen = max(1, (SELECT count(DISTINCT f.review_id) FROM findings f
                          WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false')),
    last_seen_review_id = COALESCE((SELECT f.review_id FROM findings f JOIN reviews r ON r.id = f.review_id
                                     WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false'
                                     ORDER BY r.started_at DESC, r.id DESC LIMIT 1), last_seen_review_id),
    last_checked_review_id = COALESCE((SELECT x.rid FROM (
                                SELECT f.review_id AS rid FROM findings f
                                 WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false'
                                UNION SELECT c.review_id FROM review_issue_checks c WHERE c.issue_id = NEW.issue_id) x
                              JOIN reviews r ON r.id = x.rid ORDER BY r.started_at DESC, r.id DESC LIMIT 1),
                              last_checked_review_id),
    current_state = CASE WHEN NEW.verification = 'false' THEN current_state
                         ELSE COALESCE(NEW.before_state, current_state) END,
    change_review_id = NEW.review_id,
    change_actor_id = NULL,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = 'matched by finding ' || NEW.point_id,
    status = CASE
      WHEN NEW.verification = 'false' THEN status
      WHEN NEW.match_type = 'regression' AND status IN (SELECT value FROM issue_statuses WHERE fixed = 1) THEN 'regressed'
      WHEN NEW.match_type IN (SELECT value FROM match_types WHERE came_back = 1)
       AND status IN (SELECT value FROM issue_statuses WHERE reopens = 1) THEN 'open'
      ELSE status END
  WHERE NEW.issue_id IS NOT NULL AND id = NEW.issue_id;
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  SELECT 'issue', NEW.issue_id, 'seen', CASE WHEN NEW.verification = 'false' THEN 'false' ELSE NEW.match_type END,
         NEW.review_id, NEW.point_id
  WHERE NEW.issue_id IS NOT NULL;
END;
CREATE TRIGGER tr_finding_verification_changed AFTER UPDATE OF verification ON findings
WHEN OLD.verification IS NOT NEW.verification BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('finding', NEW.id, 'verification-changed', OLD.verification, NEW.verification, NEW.verified_by_actor_id,
          NEW.review_id, NEW.verification_note);
  UPDATE issues SET
    times_seen = max(1, (SELECT count(DISTINCT f.review_id) FROM findings f
                          WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false')),
    last_seen_review_id = COALESCE((SELECT f.review_id FROM findings f JOIN reviews r ON r.id = f.review_id
                                     WHERE f.issue_id = NEW.issue_id AND f.verification <> 'false'
                                     ORDER BY r.started_at DESC, r.id DESC LIMIT 1), last_seen_review_id)
  WHERE NEW.issue_id IS NOT NULL AND id = NEW.issue_id
    AND (OLD.verification = 'false') <> (NEW.verification = 'false');
END;
CREATE TRIGGER tr_location_repo_in_review BEFORE INSERT ON finding_locations
WHEN NOT EXISTS (SELECT 1 FROM findings f
                 JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = NEW.repository_id
                 WHERE f.id = NEW.finding_id) BEGIN
  SELECT RAISE(ABORT, 'the location''s repository is not among its review''s review_repositories');
END;
CREATE TRIGGER tr_location_commit AFTER INSERT ON finding_locations
WHEN NEW.commit_sha IS NULL BEGIN
  UPDATE finding_locations SET commit_sha = (
    SELECT rr.commit_sha FROM findings f
    JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = NEW.repository_id
    WHERE f.id = NEW.finding_id)
  WHERE id = NEW.id;
END;
CREATE TRIGGER tr_location_immutable BEFORE UPDATE ON finding_locations
WHEN OLD.finding_id IS NOT NEW.finding_id OR OLD.repository_id IS NOT NEW.repository_id
  OR OLD.path IS NOT NEW.path OR OLD.symbol IS NOT NEW.symbol OR OLD.role IS NOT NEW.role
  OR (OLD.commit_sha IS NOT NULL AND OLD.commit_sha IS NOT NEW.commit_sha) BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a location''s finding, repository, commit, path, symbol and role cannot change');
END;
CREATE TRIGGER tr_location_no_delete BEFORE DELETE ON finding_locations BEGIN
  SELECT RAISE(ABORT, 'history is append-only: finding locations cannot be deleted');
END;
CREATE TRIGGER tr_relation_noreverse_dup BEFORE INSERT ON issue_relations
WHEN NEW.relation IN ('duplicate-of','continuation-of','regression-of','supersedes','merged-into','split-from',
                      'caused-by','related-to')
BEGIN
  SELECT RAISE(ABORT, 'the reverse relation already exists')
  WHERE EXISTS (SELECT 1 FROM issue_relations
                WHERE from_issue_id = NEW.to_issue_id AND to_issue_id = NEW.from_issue_id
                  AND relation = NEW.relation);
END;
CREATE TRIGGER tr_relation_noreverse_dup_upd BEFORE UPDATE OF from_issue_id, to_issue_id, relation ON issue_relations
WHEN NEW.relation IN ('duplicate-of','continuation-of','regression-of','supersedes','merged-into','split-from',
                      'caused-by','related-to')
BEGIN
  SELECT RAISE(ABORT, 'the reverse relation already exists')
  WHERE EXISTS (SELECT 1 FROM issue_relations
                WHERE from_issue_id = NEW.to_issue_id AND to_issue_id = NEW.from_issue_id
                  AND relation = NEW.relation AND id <> NEW.id);
END;
CREATE TRIGGER tr_relation_no_cycle BEFORE INSERT ON issue_relations
WHEN NEW.relation IN ('blocks','duplicate-of','merged-into') BEGIN
  SELECT RAISE(ABORT, 'that relation would make a cycle')
  WHERE EXISTS (WITH RECURSIVE reach(n) AS (
                  SELECT NEW.to_issue_id
                  UNION SELECT r.to_issue_id FROM issue_relations r JOIN reach ON r.from_issue_id = reach.n
                   WHERE r.relation = NEW.relation
                      OR (NEW.relation <> 'blocks' AND r.relation IN (SELECT value FROM relation_types WHERE closes = 1)))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;
CREATE TRIGGER tr_relation_no_cycle_upd BEFORE UPDATE OF from_issue_id, to_issue_id, relation ON issue_relations
WHEN NEW.relation IN ('blocks','duplicate-of','merged-into') BEGIN
  SELECT RAISE(ABORT, 'that relation would make a cycle')
  WHERE EXISTS (WITH RECURSIVE reach(n) AS (
                  SELECT NEW.to_issue_id
                  UNION SELECT r.to_issue_id FROM issue_relations r JOIN reach ON r.from_issue_id = reach.n
                   WHERE r.id <> NEW.id AND (r.relation = NEW.relation
                      OR (NEW.relation <> 'blocks' AND r.relation IN (SELECT value FROM relation_types WHERE closes = 1))))
                SELECT 1 FROM reach WHERE n = NEW.from_issue_id);
END;
CREATE TRIGGER tr_relation_created AFTER INSERT ON issue_relations BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('relation', NEW.id, 'created', (SELECT code FROM issues WHERE id = NEW.from_issue_id),
          NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id),
          NEW.established_by_actor_id, NEW.established_in_review_id, NEW.rationale);
END;
CREATE TRIGGER tr_relation_closes_duplicate AFTER INSERT ON issue_relations
WHEN NEW.relation IN (SELECT value FROM relation_types WHERE closes = 1) BEGIN
  UPDATE issues SET
    status = 'duplicate',
    resolution = COALESCE(resolution, NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id)),
    change_actor_id = NEW.established_by_actor_id,
    change_review_id = NEW.established_in_review_id,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = NEW.relation || ' ' || (SELECT code FROM issues WHERE id = NEW.to_issue_id) || ': ' || NEW.rationale
  WHERE id = NEW.from_issue_id AND status IN (SELECT value FROM issue_statuses WHERE active = 1);
END;
CREATE TRIGGER tr_relation_no_delete BEFORE DELETE ON issue_relations BEGIN
  SELECT RAISE(ABORT, 'history is append-only: relations cannot be deleted');
END;
CREATE TRIGGER tr_check_needs_evidence BEFORE INSERT ON review_issue_checks
WHEN NEW.outcome IN ('fixed','regressed','changed') AND NULLIF(trim(NEW.evidence), '') IS NULL BEGIN
  SELECT RAISE(ABORT, 'a fixed, regressed or changed check needs evidence (what the review saw)');
END;
CREATE TRIGGER tr_check_guard BEFORE INSERT ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'checks are history: this review already recorded a check of this issue')
  WHERE EXISTS (SELECT 1 FROM review_issue_checks WHERE review_id = NEW.review_id AND issue_id = NEW.issue_id);
  SELECT RAISE(ABORT, 'a review cannot check an issue it first found')
  WHERE (SELECT first_seen_review_id FROM issues WHERE id = NEW.issue_id) = NEW.review_id;
  SELECT RAISE(ABORT, 'the check''s finding belongs to another review')
  WHERE NEW.finding_id IS NOT NULL
    AND (SELECT review_id FROM findings WHERE id = NEW.finding_id) IS NOT NEW.review_id;
  SELECT RAISE(ABORT, 'this review raised the issue again in a finding; it cannot also record it fixed')
  WHERE NEW.outcome = 'fixed'
    AND EXISTS (SELECT 1 FROM findings f WHERE f.review_id = NEW.review_id AND f.issue_id = NEW.issue_id
                  AND f.verification <> 'false');
END;
CREATE TRIGGER tr_check_no_update BEFORE UPDATE ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'history is append-only: review checks cannot be changed');
END;
CREATE TRIGGER tr_check_no_delete BEFORE DELETE ON review_issue_checks BEGIN
  SELECT RAISE(ABORT, 'history is append-only: review checks cannot be deleted');
END;
CREATE TRIGGER tr_issue_checked AFTER INSERT ON review_issue_checks BEGIN
  UPDATE issues SET
    last_checked_review_id = CASE
      WHEN last_checked_review_id IS NULL
        OR (SELECT started_at FROM reviews WHERE id = NEW.review_id)
           >= (SELECT started_at FROM reviews WHERE id = last_checked_review_id)
      THEN NEW.review_id ELSE last_checked_review_id END,
    change_review_id = NEW.review_id,
    change_actor_id = NEW.actor_id,
    change_step_id = NULL,
    change_commit_sha = NULL,
    change_note = 'review check: ' || NEW.outcome || COALESCE(' — ' || NEW.evidence, ''),
    status = CASE
      WHEN NEW.outcome = 'fixed' AND status = 'fixed' THEN 'verified'
      WHEN NEW.outcome = 'fixed' AND status IN (SELECT value FROM issue_statuses WHERE active = 1) THEN 'fixed'
      WHEN NEW.outcome IN ('regressed','still-present','changed') AND status IN (SELECT value FROM issue_statuses WHERE fixed = 1) THEN 'regressed'
      WHEN NEW.outcome = 'obsolete' AND status <> 'duplicate' THEN 'obsolete'
      ELSE status END,
    after_state = CASE WHEN NEW.outcome = 'fixed' AND status IN (SELECT value FROM issue_statuses WHERE active = 1)
                       THEN COALESCE(after_state, NEW.evidence,
                                     'Found fixed by review ' || (SELECT code FROM reviews WHERE id = NEW.review_id) || '.')
                       ELSE after_state END,
    resolution = CASE
      WHEN NEW.outcome = 'fixed' AND status IN (SELECT value FROM issue_statuses WHERE active = 1)
      THEN COALESCE(resolution,
             'Found fixed by a later review while plan step ' || (
               SELECT p.code || ' #' || s.number FROM step_findings sf
               JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
               JOIN findings f ON f.id = sf.finding_id
               WHERE f.issue_id = NEW.issue_id AND s.status NOT IN ('done','dropped')
               ORDER BY s.number LIMIT 1) || ' was not done.',
             'Found fixed by a later review, with no plan step recorded.')
      WHEN NEW.outcome = 'obsolete' AND status <> 'duplicate'
      THEN COALESCE(resolution, 'Found obsolete by review ' || (SELECT code FROM reviews WHERE id = NEW.review_id)
                                || COALESCE(': ' || NEW.evidence, '.'))
      ELSE resolution END
  WHERE id = NEW.issue_id;
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('issue', NEW.issue_id, 'checked', NEW.outcome, NEW.actor_id, NEW.review_id, NEW.evidence);
END;
CREATE TRIGGER tr_review_code BEFORE INSERT ON reviews
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'R-*[^0-9]*' AND (
     NEW.code IS NOT 'R-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM reviews
        WHERE code = 'R-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'review codes are sequential: R- and the next number (R-0001, R-0002 ...)');
END;
CREATE TRIGGER tr_review_code_fixed BEFORE UPDATE OF code ON reviews WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_review_manager_kind BEFORE INSERT ON reviews
WHEN NEW.manager_actor_id IS NOT NULL
 AND (SELECT kind FROM actors WHERE id = NEW.manager_actor_id) IS NOT 'manager' BEGIN
  SELECT RAISE(ABORT, 'manager_actor_id must be an actor of kind manager');
END;
CREATE TRIGGER tr_review_manager_kind_upd BEFORE UPDATE OF manager_actor_id ON reviews
WHEN NEW.manager_actor_id IS NOT NULL
 AND (SELECT kind FROM actors WHERE id = NEW.manager_actor_id) IS NOT 'manager' BEGIN
  SELECT RAISE(ABORT, 'manager_actor_id must be an actor of kind manager');
END;
CREATE TRIGGER tr_review_transition BEFORE UPDATE OF status ON reviews
WHEN OLD.status IS NOT NEW.status BEGIN
  SELECT RAISE(ABORT, 'a review leaves handed-off or abandoned only with change_note')
  WHERE OLD.status IN ('handed-off','abandoned') AND NEW.change_note IS NULL;
  SELECT RAISE(ABORT, 'a review moves planning, awaiting-approval, approved, running, verifying, indexing, consolidating, handed-off (or abandoned)')
  WHERE OLD.status NOT IN ('handed-off','abandoned') AND NEW.status <> 'abandoned' AND NOT (
       (OLD.status = 'planning' AND NEW.status = 'awaiting-approval')
    OR (OLD.status = 'awaiting-approval' AND NEW.status IN ('planning','approved'))
    OR (instr(',approved,running,verifying,indexing,consolidating,', ',' || OLD.status || ',') > 0
        AND instr(',approved,running,verifying,indexing,consolidating,handed-off,', ',' || NEW.status || ',')
            > instr(',approved,running,verifying,indexing,consolidating,handed-off,', ',' || OLD.status || ','))
    OR (instr(',running,verifying,indexing,consolidating,', ',' || OLD.status || ',') > 0
        AND instr(',running,verifying,indexing,consolidating,', ',' || NEW.status || ',') > 0));
  SELECT RAISE(ABORT, 'a domain-approval gate of this review is not applied yet')
  WHERE OLD.status = 'awaiting-approval' AND NEW.status <> 'abandoned'
    AND EXISTS (SELECT 1 FROM gates g WHERE g.review_id = NEW.id AND g.kind = 'domain-approval'
                  AND g.status IN ('open','answered'));
END;
CREATE TRIGGER tr_review_status AFTER UPDATE OF status ON reviews
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('review', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.change_actor_id, NEW.id, NEW.change_note);
  UPDATE reviews SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    completed_at = CASE WHEN NEW.status IN ('handed-off','abandoned')
                        THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END
  WHERE id = NEW.id;
END;
CREATE TRIGGER tr_review_context_cleared AFTER UPDATE ON reviews
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE reviews SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_review_context_cleared_ins AFTER INSERT ON reviews
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE reviews SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_review_no_delete BEFORE DELETE ON reviews BEGIN
  SELECT RAISE(ABORT, 'history is append-only: reviews cannot be deleted; set status abandoned instead');
END;
CREATE TRIGGER tr_run_review_approved BEFORE INSERT ON reviewer_runs BEGIN
  SELECT RAISE(ABORT, 'reviewer runs need an approved review (approved_at set, status past awaiting-approval)')
  WHERE (SELECT status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NULL
           FROM reviews WHERE id = NEW.review_id);
  SELECT RAISE(ABORT, 'reviewer runs need the domain included in review_domains')
  WHERE NOT EXISTS (SELECT 1 FROM review_domains WHERE review_id = NEW.review_id
                      AND domain_code = NEW.domain_code AND decision = 'included');
  SELECT RAISE(ABORT, 'that domain is retired; use a current one')
  WHERE EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL);
END;
CREATE TRIGGER tr_run_created AFTER INSERT ON reviewer_runs BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('review', NEW.review_id, 'reviewer-run-created', NEW.label, NEW.actor_id, NEW.review_id,
          NEW.domain_code || ': ' || NEW.scope);
END;
CREATE TRIGGER tr_review_domain_active BEFORE INSERT ON review_domains
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;
CREATE TRIGGER tr_metric_domain_active BEFORE INSERT ON metrics
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;
CREATE TRIGGER tr_metric_created AFTER INSERT ON metrics BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  VALUES ('review', NEW.review_id, 'metric-recorded', CAST(NEW.value AS TEXT), NEW.review_id,
          NEW.name || ' (' || NEW.scope || ')');
END;
CREATE TRIGGER tr_actor_parent_self BEFORE INSERT ON actors
WHEN NEW.parent_actor_id IS NOT NULL AND NEW.parent_actor_id = NEW.id BEGIN
  SELECT RAISE(ABORT, 'an actor cannot be its own parent');
END;
CREATE TRIGGER tr_actor_parent_cycle BEFORE UPDATE OF parent_actor_id ON actors
WHEN NEW.parent_actor_id IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'that parent would make a cycle of actors')
  WHERE NEW.parent_actor_id = NEW.id
     OR EXISTS (WITH RECURSIVE up(n) AS (
                  SELECT NEW.parent_actor_id
                  UNION SELECT a.parent_actor_id FROM actors a JOIN up ON a.id = up.n WHERE a.parent_actor_id IS NOT NULL)
                SELECT 1 FROM up WHERE n = NEW.id);
END;
CREATE TRIGGER tr_design_goal_code BEFORE INSERT ON design_goals
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'G-[0-9][0-9]*' AND NEW.code NOT GLOB 'G-*[^0-9]*' AND (
     NEW.code IS NOT 'G-' || printf('%02d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM design_goals
        WHERE code = 'G-' || printf('%02d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'design goal codes are sequential: G- and the next number (G-01, G-02 ...)');
END;
CREATE TRIGGER tr_design_goal_code_fixed BEFORE UPDATE OF code ON design_goals WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_requirement_code BEFORE INSERT ON requirements
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'REQ-*[^0-9]*' AND (
     NEW.code IS NOT 'REQ-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER))
  OR CAST(substr(NEW.code, 5) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 5) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM requirements
        WHERE code = 'REQ-' || printf('%04d', CAST(substr(NEW.code, 5) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'requirement codes are sequential: REQ- and the next number (REQ-0001, REQ-0002 ...)');
END;
CREATE TRIGGER tr_requirement_code_fixed BEFORE UPDATE OF code ON requirements WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_goal_insert BEFORE INSERT ON design_goals
WHEN NEW.status NOT IN ('proposed','approved') BEGIN
  SELECT RAISE(ABORT, 'a design goal starts proposed (or approved, with its approver)');
END;
CREATE TRIGGER tr_goal_domain_active BEFORE INSERT ON design_goals
WHEN EXISTS (SELECT 1 FROM domains WHERE code = NEW.domain_code AND retired_at IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'that domain is retired; use a current one');
END;
CREATE TRIGGER tr_goal_approver BEFORE INSERT ON design_goals
WHEN NEW.status = 'approved'
 AND NOT EXISTS (SELECT 1 FROM actors WHERE id = NEW.approved_by_actor_id AND kind IN ('owner','human')) BEGIN
  SELECT RAISE(ABORT, 'a design goal is approved by the owner: its approver (approved_by_actor_id) must be an actor of kind owner or human');
END;
CREATE TRIGGER tr_goal_approver_upd BEFORE UPDATE OF status, approved_by_actor_id ON design_goals
WHEN NEW.status = 'approved'
 AND NOT EXISTS (SELECT 1 FROM actors WHERE id = NEW.approved_by_actor_id AND kind IN ('owner','human')) BEGIN
  SELECT RAISE(ABORT, 'a design goal is approved by the owner: its approver (approved_by_actor_id) must be an actor of kind owner or human');
END;
CREATE TRIGGER tr_goal_transition BEFORE UPDATE OF status ON design_goals
WHEN OLD.status IS NOT NEW.status
 AND NOT ((OLD.status = 'proposed' AND NEW.status IN ('approved','rejected','retired'))
       OR (OLD.status = 'approved' AND NEW.status = 'retired')) BEGIN
  SELECT RAISE(ABORT, 'a design goal moves proposed -> approved, rejected or retired, and approved -> retired; propose a new goal instead');
END;
CREATE TRIGGER tr_goal_fixed BEFORE UPDATE OF text, domain_code, review_id, approved_by_actor_id, approved_at ON design_goals
WHEN OLD.status <> 'proposed'
 AND (OLD.text IS NOT NEW.text OR OLD.domain_code IS NOT NEW.domain_code OR OLD.review_id IS NOT NEW.review_id
      OR OLD.approved_by_actor_id IS NOT NEW.approved_by_actor_id OR OLD.approved_at IS NOT NEW.approved_at) BEGIN
  SELECT RAISE(ABORT, 'an approved, rejected or retired design goal is not edited: retire it and propose a new one');
END;
CREATE TRIGGER tr_goal_created AFTER INSERT ON design_goals BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('goal', NEW.id, CASE NEW.status WHEN 'proposed' THEN 'proposed' ELSE 'recorded' END, NEW.status,
          COALESCE(NEW.change_actor_id, NEW.approved_by_actor_id), NEW.review_id,
          COALESCE(NEW.domain_code || ': ', 'whole project: ') || NEW.text);
  INSERT OR IGNORE INTO review_goals(review_id, goal_id)
    SELECT NEW.review_id, NEW.id WHERE NEW.status = 'approved' AND NEW.review_id IS NOT NULL;
END;
CREATE TRIGGER tr_goal_status AFTER UPDATE OF status ON design_goals
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, note)
  VALUES ('goal', NEW.id, 'status-changed', OLD.status, NEW.status,
          CASE WHEN NEW.status = 'approved' THEN COALESCE(NEW.change_actor_id, NEW.approved_by_actor_id)
               ELSE NEW.change_actor_id END,
          NEW.review_id, CASE WHEN NEW.status IN ('rejected','retired') THEN NEW.note END);
  UPDATE design_goals SET retired_at = COALESCE(retired_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  WHERE id = NEW.id AND NEW.status = 'retired';
  INSERT OR IGNORE INTO review_goals(review_id, goal_id)
    SELECT NEW.review_id, NEW.id WHERE NEW.status = 'approved' AND NEW.review_id IS NOT NULL;
END;
CREATE TRIGGER tr_goal_retired_at AFTER UPDATE OF retired_at ON design_goals
WHEN NEW.retired_at IS NOT NULL AND NEW.status <> 'retired' BEGIN
  UPDATE design_goals SET status = 'retired' WHERE id = NEW.id;
END;
CREATE TRIGGER tr_goal_text_changed AFTER UPDATE OF text ON design_goals WHEN OLD.text IS NOT NEW.text BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id)
  VALUES ('goal', NEW.id, 'text-changed', OLD.text, NEW.text, NEW.change_actor_id, NEW.review_id);
END;
CREATE TRIGGER tr_goal_context_cleared AFTER UPDATE ON design_goals WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE design_goals SET change_actor_id = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_goal_context_cleared_ins AFTER INSERT ON design_goals
WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE design_goals SET change_actor_id = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_goal_no_delete BEFORE DELETE ON design_goals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: design goals cannot be deleted; retire it instead');
END;
CREATE TRIGGER tr_gate_approves_goals AFTER UPDATE OF status ON gates
WHEN OLD.status = 'open' AND NEW.status = 'answered' AND NEW.answer_value = 'approve' BEGIN
  UPDATE design_goals SET status = 'approved',
    approved_by_actor_id = COALESCE(NEW.answered_by_actor_id, (SELECT min(id) FROM actors WHERE kind = 'owner')),
    approved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    change_actor_id = COALESCE(NEW.answered_by_actor_id, (SELECT min(id) FROM actors WHERE kind = 'owner'))
  WHERE gate_id = NEW.id AND status = 'proposed';
END;
CREATE TRIGGER tr_gate_answers_question AFTER UPDATE OF status ON gates
WHEN OLD.status = 'open' AND NEW.status = 'answered' AND length(trim(COALESCE(NEW.answer_text, ''))) > 0 BEGIN
  INSERT INTO interview_answers(question_id, answer, answered_via, answered_by_actor_id)
    SELECT q.id, NEW.answer_text, CASE NEW.answered_via WHEN 'viewer' THEN 'viewer' ELSE 'chat' END,
           NEW.answered_by_actor_id
    FROM interview_questions q WHERE q.gate_id = NEW.id;
END;
CREATE TRIGGER tr_question_code BEFORE INSERT ON interview_questions
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'IQ-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'IQ-*[^0-9]*' AND (
     NEW.code IS NOT 'IQ-' || printf('%04d', CAST(substr(NEW.code, 4) AS INTEGER))
  OR CAST(substr(NEW.code, 4) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 4) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM interview_questions
        WHERE code = 'IQ-' || printf('%04d', CAST(substr(NEW.code, 4) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'interview question codes are sequential: IQ- and the next number (IQ-0001, IQ-0002 ...)');
END;
CREATE TRIGGER tr_question_review_approved BEFORE INSERT ON interview_questions
WHEN (SELECT approved_at FROM reviews WHERE id = NEW.review_id) IS NULL BEGIN
  SELECT RAISE(ABORT, 'the interview comes after the owner approves the review''s domains (review-status <review> approved)');
END;
CREATE TRIGGER tr_question_domain_in_review BEFORE INSERT ON interview_questions
WHEN NEW.domain_code IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM review_domains WHERE review_id = NEW.review_id AND domain_code = NEW.domain_code
                   AND decision = 'included') BEGIN
  SELECT RAISE(ABORT, 'the review did not include that domain; ask about an approved domain, or leave the domain out for a whole-project question');
END;
CREATE TRIGGER tr_question_asked AFTER INSERT ON interview_questions BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('question', NEW.id, 'asked', NEW.code, NEW.asked_by_actor_id, NEW.review_id, NEW.question);
END;
CREATE TRIGGER tr_question_no_update BEFORE UPDATE ON interview_questions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: an interview question is kept as asked; ask a new one');
END;
CREATE TRIGGER tr_question_no_delete BEFORE DELETE ON interview_questions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: interview questions cannot be deleted');
END;
CREATE TRIGGER tr_answer_recorded AFTER INSERT ON interview_answers BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, note)
  VALUES ('question', NEW.question_id, 'answered', NEW.answered_via, NEW.answered_by_actor_id,
          (SELECT review_id FROM interview_questions WHERE id = NEW.question_id), NEW.answer);
END;
CREATE TRIGGER tr_answer_no_update BEFORE UPDATE ON interview_answers BEGIN
  SELECT RAISE(ABORT, 'history is append-only: an answer is kept as given; record a new answer instead');
END;
CREATE TRIGGER tr_answer_no_delete BEFORE DELETE ON interview_answers BEGIN
  SELECT RAISE(ABORT, 'history is append-only: interview answers cannot be deleted');
END;
CREATE TRIGGER tr_goal_source_rules BEFORE INSERT ON goal_sources BEGIN
  SELECT RAISE(ABORT, 'a design goal names its sources when it is proposed')
  WHERE (SELECT status FROM design_goals WHERE id = NEW.goal_id) IS NOT 'proposed';
  SELECT RAISE(ABORT, 'a design goal cites questions of its own domain or whole-project ones')
  WHERE EXISTS (SELECT 1 FROM design_goals g JOIN interview_questions q ON q.id = NEW.question_id
                WHERE g.id = NEW.goal_id AND g.domain_code IS NOT NULL AND q.domain_code IS NOT NULL
                  AND q.domain_code <> g.domain_code);
END;
CREATE TRIGGER tr_goal_source_linked AFTER INSERT ON goal_sources BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, review_id, note)
  SELECT 'goal', NEW.goal_id, 'source-linked', q.code, q.review_id, q.question
  FROM interview_questions q WHERE q.id = NEW.question_id;
END;
CREATE TRIGGER tr_goal_source_no_update BEFORE UPDATE ON goal_sources BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a goal''s sources are kept as recorded');
END;
CREATE TRIGGER tr_goal_source_no_delete BEFORE DELETE ON goal_sources BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a goal''s sources cannot be deleted');
END;
CREATE TRIGGER tr_step_goal_served BEFORE INSERT ON step_goals
WHEN NOT EXISTS (SELECT 1 FROM design_goals g JOIN goal_statuses s ON s.value = g.status
                 WHERE g.id = NEW.goal_id AND s.served = 1) BEGIN
  SELECT RAISE(ABORT, 'a plan step serves approved design goals only; ask the owner to approve the goal first');
END;
CREATE TRIGGER tr_step_goal_linked AFTER INSERT ON step_goals BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id)
  SELECT 'goal', NEW.goal_id, 'step-linked', p.code || ' #' || s.number, NEW.step_id
  FROM steps s JOIN plans p ON p.id = s.plan_id WHERE s.id = NEW.step_id;
END;
CREATE TRIGGER tr_step_goal_no_update BEFORE UPDATE ON step_goals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: link the step to another goal instead');
END;
CREATE TRIGGER tr_step_goal_no_delete BEFORE DELETE ON step_goals BEGIN
  SELECT RAISE(ABORT, 'history is append-only: a step''s goals cannot be unlinked');
END;
CREATE TRIGGER tr_decision_code BEFORE INSERT ON decisions
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'D-*[^0-9]*' AND (
     NEW.code IS NOT 'D-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM decisions
        WHERE code = 'D-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'decision codes are sequential: D- and the next number (D-0001, D-0002 ...)');
END;
CREATE TRIGGER tr_decision_code_fixed BEFORE UPDATE OF code ON decisions WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_decision_created AFTER INSERT ON decisions BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'created', NEW.status,
          CASE WHEN NEW.status = 'decided' THEN COALESCE(NEW.decided_by_actor_id, NEW.asked_by_actor_id)
               ELSE NEW.asked_by_actor_id END,
          NEW.review_id, NEW.step_id, CASE WHEN NEW.status = 'decided' THEN NEW.answer ELSE NEW.question END);
  UPDATE decisions SET decided_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
  WHERE id = NEW.id AND NEW.status = 'decided' AND NEW.decided_at IS NULL;
END;
CREATE TRIGGER tr_decision_status AFTER UPDATE OF status ON decisions
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.decided_by_actor_id, NEW.review_id, NEW.step_id, NEW.answer);
  UPDATE decisions SET decided_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
  WHERE id = NEW.id AND NEW.status = 'decided' AND (NEW.decided_at IS NULL OR NEW.decided_at IS OLD.decided_at);
END;
CREATE TRIGGER tr_decision_answer AFTER UPDATE OF answer ON decisions
WHEN OLD.answer IS NOT NULL AND OLD.answer IS NOT NEW.answer BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('decision', NEW.id, 'answer-changed', OLD.answer, NEW.answer, NEW.decided_by_actor_id, NEW.review_id,
          NEW.step_id, NEW.rationale);
END;
CREATE TRIGGER tr_decision_no_delete BEFORE DELETE ON decisions BEGIN
  SELECT RAISE(ABORT, 'history is append-only: decisions cannot be deleted; set status revisit instead');
END;
CREATE TRIGGER tr_gate_code BEFORE INSERT ON gates
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'GATE-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'GATE-*[^0-9]*' AND (
     NEW.code IS NOT 'GATE-' || printf('%04d', CAST(substr(NEW.code, 6) AS INTEGER))
  OR CAST(substr(NEW.code, 6) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 6) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM gates
        WHERE code = 'GATE-' || printf('%04d', CAST(substr(NEW.code, 6) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'gate codes are sequential: GATE- and the next number (GATE-0001, GATE-0002 ...)');
END;
CREATE TRIGGER tr_gate_code_fixed BEFORE UPDATE OF code ON gates WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_gate_options_shape BEFORE INSERT ON gates
WHEN NEW.options IS NOT NULL AND json_valid(NEW.options) AND json_type(NEW.options) = 'array'
 AND EXISTS (SELECT 1 FROM json_each(NEW.options) j
             WHERE j.type <> 'object' OR json_type(j.value, '$.value') IS NOT 'text'
                OR json_type(j.value, '$.label') IS NOT 'text'
                OR json_type(j.value, '$.description') NOT IN ('text','null')) BEGIN
  SELECT RAISE(ABORT, 'gate options must be objects with a text value and label (and an optional description)');
END;
CREATE TRIGGER tr_gate_options_shape_upd BEFORE UPDATE OF options ON gates
WHEN NEW.options IS NOT NULL AND json_valid(NEW.options) AND json_type(NEW.options) = 'array'
 AND EXISTS (SELECT 1 FROM json_each(NEW.options) j
             WHERE j.type <> 'object' OR json_type(j.value, '$.value') IS NOT 'text'
                OR json_type(j.value, '$.label') IS NOT 'text'
                OR json_type(j.value, '$.description') NOT IN ('text','null')) BEGIN
  SELECT RAISE(ABORT, 'gate options must be objects with a text value and label (and an optional description)');
END;
CREATE TRIGGER tr_gate_answer_rules BEFORE UPDATE OF status, answer_value, answer_text ON gates
WHEN NEW.status = 'answered' AND (OLD.status IS NOT 'answered' OR OLD.answer_value IS NOT NEW.answer_value
                                  OR OLD.answer_text IS NOT NEW.answer_text) BEGIN
  SELECT RAISE(ABORT, 'answer_value must be one of the gate''s option values')
  WHERE NEW.answer_value IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM json_each(COALESCE(NEW.options, '[]')) j
                    WHERE json_extract(j.value, '$.value') = NEW.answer_value);
  SELECT RAISE(ABORT, 'this gate takes no free text: choose one of the options')
  WHERE NEW.allow_text = 0 AND NEW.answer_value IS NULL;
  SELECT RAISE(ABORT, 'this gate takes no free text: answer with the option alone')
  WHERE NEW.allow_text = 0 AND NEW.answer_text IS NOT NULL;
  SELECT RAISE(ABORT, 'answer_text is too long (over 10,000 characters)')
  WHERE length(NEW.answer_text) > 10000;
END;
CREATE TRIGGER tr_gate_transition BEFORE UPDATE OF status ON gates
WHEN OLD.status IS NOT NEW.status AND NEW.status <> 'open'
 AND NOT ((OLD.status = 'open' AND NEW.status IN ('answered','withdrawn'))
       OR (OLD.status = 'answered' AND NEW.status IN ('applied','withdrawn'))) BEGIN
  SELECT RAISE(ABORT, 'a gate moves open -> answered -> applied (or withdrawn); answer it before applying it');
END;
CREATE TRIGGER tr_gate_created AFTER INSERT ON gates BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'opened', NEW.status, NEW.opened_by_actor_id, NEW.review_id, NEW.step_id, NEW.title);
END;
CREATE TRIGGER tr_gate_status AFTER UPDATE OF status ON gates
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'status-changed', OLD.status, NEW.status,
          CASE WHEN NEW.status = 'answered' THEN COALESCE(NEW.answered_by_actor_id, NEW.change_actor_id)
               ELSE NEW.change_actor_id END,
          NEW.review_id, NEW.step_id,
          CASE WHEN NEW.status = 'applied' THEN NEW.applied_note
               ELSE COALESCE(NEW.answer_value || ': ', '') || COALESCE(NEW.answer_text, '') END);
  UPDATE gates SET
    answered_at = CASE WHEN NEW.status = 'answered' AND answered_at IS NULL
                       THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE answered_at END,
    applied_at  = CASE WHEN NEW.status = 'applied' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE applied_at END
  WHERE id = NEW.id;
END;
CREATE TRIGGER tr_gate_answer_final BEFORE UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status <> 'open'
 AND (OLD.answer_value IS NOT NEW.answer_value OR OLD.answer_text IS NOT NEW.answer_text)
 AND NOT (NEW.status = 'open' AND NEW.answer_value IS NULL AND NEW.answer_text IS NULL) BEGIN
  SELECT RAISE(ABORT, 'the answer of a gate that is no longer open cannot be changed; open a new gate');
END;
CREATE TRIGGER tr_gate_answered_by_final BEFORE UPDATE OF answered_by, answered_via, answered_by_actor_id, answered_at ON gates
WHEN OLD.status <> 'open' AND NEW.status <> 'open'
 AND (OLD.answered_by IS NOT NEW.answered_by OR OLD.answered_via IS NOT NEW.answered_via
      OR OLD.answered_by_actor_id IS NOT NEW.answered_by_actor_id
      OR (OLD.answered_at IS NOT NULL AND OLD.answered_at IS NOT NEW.answered_at)) BEGIN
  SELECT RAISE(ABORT, 'who answered a gate, how and when cannot be changed once it is answered');
END;
CREATE TRIGGER tr_gate_answer_cleared AFTER UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status <> 'open' AND NEW.status = 'open'
 AND (OLD.answer_value IS NOT NULL OR OLD.answer_text IS NOT NULL) BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'answer-cleared', OLD.answer_value, NULL, NEW.review_id, NEW.step_id,
          'reopened; the answer was: ' || COALESCE(OLD.answer_value || ': ', '') || COALESCE(OLD.answer_text, ''));
END;
CREATE TRIGGER tr_gate_answer_changed AFTER UPDATE OF answer_value, answer_text ON gates
WHEN OLD.status = 'open' AND (OLD.answer_value IS NOT NULL OR OLD.answer_text IS NOT NULL)
 AND (OLD.answer_value IS NOT NEW.answer_value OR OLD.answer_text IS NOT NEW.answer_text) BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, note)
  VALUES ('gate', NEW.id, 'answer-changed', OLD.answer_value, NEW.answer_value, NEW.change_actor_id, NEW.review_id,
          NEW.step_id, 'was: ' || COALESCE(OLD.answer_text, '') || '; now: ' || COALESCE(NEW.answer_text, ''));
END;
CREATE TRIGGER tr_gate_context_cleared AFTER UPDATE ON gates WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE gates SET change_actor_id = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_gate_context_cleared_ins AFTER INSERT ON gates
WHEN NEW.change_actor_id IS NOT NULL BEGIN
  UPDATE gates SET change_actor_id = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_gate_no_delete BEFORE DELETE ON gates BEGIN
  SELECT RAISE(ABORT, 'history is append-only: gates cannot be deleted; set status withdrawn instead');
END;
CREATE TRIGGER tr_plan_code BEFORE INSERT ON plans
-- (a malformed code is left to the code CHECK, which names it)
WHEN NEW.code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND NEW.code NOT GLOB 'P-*[^0-9]*' AND (
     NEW.code IS NOT 'P-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER))
  OR CAST(substr(NEW.code, 3) AS INTEGER) < 1
  OR (CAST(substr(NEW.code, 3) AS INTEGER) > 1 AND NOT EXISTS (SELECT 1 FROM plans
        WHERE code = 'P-' || printf('%04d', CAST(substr(NEW.code, 3) AS INTEGER) - 1)))) BEGIN
  SELECT RAISE(ABORT, 'plan codes are sequential: P- and the next number (P-0001, P-0002 ...)');
END;
CREATE TRIGGER tr_plan_code_fixed BEFORE UPDATE OF code ON plans WHEN OLD.code IS NOT NEW.code BEGIN
  SELECT RAISE(ABORT, 'codes never change');
END;
CREATE TRIGGER tr_plan_created AFTER INSERT ON plans BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'created', NEW.status, NEW.change_actor_id, NEW.title);
END;
CREATE TRIGGER tr_plan_transition BEFORE UPDATE OF status ON plans
WHEN OLD.status IS NOT NEW.status BEGIN
  SELECT RAISE(ABORT, 'a plan leaves superseded or abandoned only with change_note')
  WHERE OLD.status IN ('superseded','abandoned') AND NEW.change_note IS NULL;
  SELECT RAISE(ABORT, 'a plan moves drafting, awaiting-approval, approved, in-progress, complete (or superseded, abandoned)')
  WHERE OLD.status NOT IN ('superseded','abandoned') AND NOT (
       (OLD.status = 'drafting' AND NEW.status IN ('awaiting-approval','superseded','abandoned'))
    OR (OLD.status = 'awaiting-approval' AND NEW.status IN ('drafting','approved','superseded','abandoned'))
    OR (OLD.status = 'approved' AND NEW.status IN ('in-progress','drafting','superseded','abandoned'))
    OR (OLD.status = 'in-progress' AND NEW.status IN ('complete','superseded','abandoned'))
    OR (OLD.status = 'complete' AND NEW.status IN ('in-progress','superseded')));
  SELECT RAISE(ABORT, 'a plan-approval gate of this plan is not applied yet')
  WHERE OLD.status = 'awaiting-approval' AND NEW.status = 'approved'
    AND EXISTS (SELECT 1 FROM gates g WHERE g.plan_id = NEW.id AND g.kind = 'plan-approval'
                  AND g.status IN ('open','answered'));
END;
CREATE TRIGGER tr_plan_status AFTER UPDATE OF status ON plans
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'status-changed', OLD.status, NEW.status, NEW.change_actor_id, NEW.change_note);
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
CREATE TRIGGER tr_plan_title_changed AFTER UPDATE OF title ON plans WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, note)
  VALUES ('plan', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.change_note);
END;
CREATE TRIGGER tr_plan_touch AFTER UPDATE ON plans
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE plans SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
CREATE TRIGGER tr_plan_context_cleared AFTER UPDATE ON plans
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE plans SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_plan_context_cleared_ins AFTER INSERT ON plans
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE plans SET change_actor_id = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_dep_order BEFORE INSERT ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
END;
CREATE TRIGGER tr_dep_order_upd BEFORE UPDATE ON step_dependencies BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE NOT EXISTS (
    SELECT 1 FROM steps a JOIN steps b ON a.plan_id = b.plan_id
    WHERE a.id = NEW.step_id AND b.id = NEW.depends_on_step_id AND b.number < a.number);
END;
CREATE TRIGGER tr_dep_created AFTER INSERT ON step_dependencies BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id, note)
  VALUES ('step', NEW.step_id, 'dependency-added',
          CAST((SELECT number FROM steps WHERE id = NEW.depends_on_step_id) AS TEXT), NEW.step_id,
          'depends on step ' || (SELECT number FROM steps WHERE id = NEW.depends_on_step_id));
END;
CREATE TRIGGER tr_step_phase_plan BEFORE INSERT ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;
CREATE TRIGGER tr_step_phase_plan_upd BEFORE UPDATE OF phase_id, plan_id ON steps BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE (SELECT plan_id FROM phases WHERE id = NEW.phase_id) IS NOT NEW.plan_id;
END;
CREATE TRIGGER tr_phase_plan_upd BEFORE UPDATE OF plan_id ON phases
WHEN OLD.plan_id IS NOT NEW.plan_id BEGIN
  SELECT RAISE(ABORT, 'step phase belongs to another plan')
  WHERE EXISTS (SELECT 1 FROM steps WHERE phase_id = NEW.id AND plan_id IS NOT NEW.plan_id);
END;
CREATE TRIGGER tr_step_renumber BEFORE UPDATE OF number, plan_id ON steps
WHEN OLD.number IS NOT NEW.number OR OLD.plan_id IS NOT NEW.plan_id BEGIN
  SELECT RAISE(ABORT, 'a step may only depend on an earlier step of the same plan')
  WHERE EXISTS (SELECT 1 FROM step_dependencies d JOIN steps b ON b.id = d.depends_on_step_id
                WHERE d.step_id = NEW.id AND (b.plan_id IS NOT NEW.plan_id OR b.number >= NEW.number))
     OR EXISTS (SELECT 1 FROM step_dependencies d JOIN steps a ON a.id = d.step_id
                WHERE d.depends_on_step_id = NEW.id AND (a.plan_id IS NOT NEW.plan_id OR a.number <= NEW.number));
END;
CREATE TRIGGER tr_step_change_needs_note BEFORE UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status AND NEW.change_note IS NULL
 AND NOT (NEW.status = 'waiting-owner' AND NEW.owner_question IS NOT NULL) BEGIN
  SELECT RAISE(ABORT, 'give change_note with a step status change');
END;
CREATE TRIGGER tr_step_gate_blocks BEFORE UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status AND NEW.status IN ('doing','done')
 AND EXISTS (SELECT 1 FROM gates g WHERE g.step_id = NEW.id AND g.blocking = 1 AND g.status IN ('open','answered')) BEGIN
  SELECT RAISE(ABORT, 'a blocking gate on this step is not applied yet');
END;
CREATE TRIGGER tr_step_created AFTER INSERT ON steps BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'created', NEW.status, NEW.change_actor_id, NEW.id, NEW.title);
END;
CREATE TRIGGER tr_step_status AFTER UPDATE OF status ON steps
WHEN OLD.status IS NOT NEW.status BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value,
                     actor_id, step_id, commit_sha, note)
  VALUES ('step', NEW.id, 'status-changed', OLD.status, NEW.status,
          NEW.change_actor_id, NEW.id, NEW.change_commit_sha,
          COALESCE(NEW.change_note, CASE WHEN NEW.status = 'waiting-owner' THEN NEW.owner_question END));
  UPDATE steps SET
    started_at = CASE WHEN NEW.status IN ('doing','done') AND started_at IS NULL
                      THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE started_at END,
    done_at    = CASE WHEN NEW.status = 'done' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END
  WHERE id = NEW.id;
END;
CREATE TRIGGER tr_step_title_changed AFTER UPDATE OF title ON steps WHEN OLD.title IS NOT NEW.title BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'title-changed', OLD.title, NEW.title, NEW.change_actor_id, NEW.id, NEW.change_note);
END;
CREATE TRIGGER tr_step_change_changed AFTER UPDATE OF change ON steps WHEN OLD.change IS NOT NEW.change BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'change-text-changed', OLD.change, NEW.change, NEW.change_actor_id, NEW.id,
          NEW.change_note);
END;
CREATE TRIGGER tr_step_proof_changed AFTER UPDATE OF proof ON steps WHEN OLD.proof IS NOT NEW.proof BEGIN
  INSERT INTO events(entity_type, entity_id, event, from_value, to_value, actor_id, step_id, note)
  VALUES ('step', NEW.id, 'proof-changed', OLD.proof, NEW.proof, NEW.change_actor_id, NEW.id, NEW.change_note);
END;
CREATE TRIGGER tr_step_touch AFTER UPDATE ON steps
WHEN NEW.updated_at IS OLD.updated_at BEGIN
  UPDATE steps SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
CREATE TRIGGER tr_step_context_cleared AFTER UPDATE ON steps
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE steps SET change_actor_id = NULL, change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_step_context_cleared_ins AFTER INSERT ON steps
WHEN NEW.change_actor_id IS NOT NULL OR NEW.change_commit_sha IS NOT NULL OR NEW.change_note IS NOT NULL BEGIN
  UPDATE steps SET change_actor_id = NULL, change_commit_sha = NULL, change_note = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER tr_step_no_delete BEFORE DELETE ON steps BEGIN
  SELECT RAISE(ABORT, 'history is append-only: steps cannot be deleted; set status dropped instead');
END;
CREATE TRIGGER tr_step_finding_in_plan BEFORE INSERT ON step_findings
WHEN NOT EXISTS (SELECT 1 FROM steps s JOIN findings f ON f.id = NEW.finding_id
                 JOIN plan_reviews pr ON pr.plan_id = s.plan_id AND pr.review_id = f.review_id
                 WHERE s.id = NEW.step_id) BEGIN
  SELECT RAISE(ABORT, 'the finding''s review is not one of the step''s plan reviews (plan_reviews)');
END;
CREATE TRIGGER tr_step_finding_created AFTER INSERT ON step_findings BEGIN
  INSERT INTO events(entity_type, entity_id, event, to_value, step_id, review_id, note)
  VALUES ('step-finding', NEW.finding_id, 'linked', (SELECT point_id FROM findings WHERE id = NEW.finding_id),
          NEW.step_id, (SELECT review_id FROM findings WHERE id = NEW.finding_id),
          'closed by step ' || (SELECT number FROM steps WHERE id = NEW.step_id) || COALESCE(': ' || NEW.multi_reason, ''));
END;
CREATE TRIGGER tr_events_no_update BEFORE UPDATE ON events BEGIN
  SELECT RAISE(ABORT, 'history is append-only: events cannot be changed');
END;
CREATE TRIGGER tr_events_no_delete BEFORE DELETE ON events BEGIN
  SELECT RAISE(ABORT, 'history is append-only: events cannot be deleted');
END;
CREATE TRIGGER tr_time_schema_migrations_ins BEFORE INSERT ON schema_migrations
WHEN (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at)) BEGIN
  SELECT RAISE(ABORT, 'schema_migrations: applied_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_schema_migrations_upd BEFORE UPDATE OF applied_at ON schema_migrations
WHEN (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at)) BEGIN
  SELECT RAISE(ABORT, 'schema_migrations: applied_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_repositories_ins BEFORE INSERT ON repositories
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'repositories: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_repositories_upd BEFORE UPDATE OF created_at ON repositories
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'repositories: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_actors_ins BEFORE INSERT ON actors
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'actors: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_actors_upd BEFORE UPDATE OF created_at ON actors
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'actors: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_domains_ins BEFORE INSERT ON domains
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'domains: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_domains_upd BEFORE UPDATE OF retired_at ON domains
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'domains: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_themes_ins BEFORE INSERT ON themes
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'themes: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_themes_upd BEFORE UPDATE OF retired_at ON themes
WHEN (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at)) BEGIN
  SELECT RAISE(ABORT, 'themes: retired_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_design_goals_ins BEFORE INSERT ON design_goals
WHEN (NEW.adopted_at IS NOT NULL AND (NEW.adopted_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.adopted_at) IS NOT NEW.adopted_at))
     OR (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at))
     OR (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at)) BEGIN
  SELECT RAISE(ABORT, 'design_goals: adopted_at, retired_at, approved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_design_goals_upd BEFORE UPDATE OF adopted_at, retired_at, approved_at ON design_goals
WHEN (NEW.adopted_at IS NOT NULL AND (NEW.adopted_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.adopted_at) IS NOT NEW.adopted_at))
     OR (NEW.retired_at IS NOT NULL AND (NEW.retired_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.retired_at) IS NOT NEW.retired_at))
     OR (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at)) BEGIN
  SELECT RAISE(ABORT, 'design_goals: adopted_at, retired_at, approved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_interview_questions_ins BEFORE INSERT ON interview_questions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_questions: asked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_interview_questions_upd BEFORE UPDATE OF asked_at ON interview_questions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_questions: asked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_interview_answers_ins BEFORE INSERT ON interview_answers
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_answers: answered_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_interview_answers_upd BEFORE UPDATE OF answered_at ON interview_answers
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at)) BEGIN
  SELECT RAISE(ABORT, 'interview_answers: answered_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_reviews_ins BEFORE INSERT ON reviews
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.completed_at IS NOT NULL AND (NEW.completed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.completed_at) IS NOT NEW.completed_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'reviews: approved_at, started_at, completed_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_reviews_upd BEFORE UPDATE OF approved_at, started_at, completed_at, updated_at ON reviews
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.completed_at IS NOT NULL AND (NEW.completed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.completed_at) IS NOT NEW.completed_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'reviews: approved_at, started_at, completed_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_review_repositories_ins BEFORE INSERT ON review_repositories
WHEN (NEW.commit_time IS NOT NULL AND (NEW.commit_time NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.commit_time) IS NOT NEW.commit_time)) BEGIN
  SELECT RAISE(ABORT, 'review_repositories: commit_time must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_review_repositories_upd BEFORE UPDATE OF commit_time ON review_repositories
WHEN (NEW.commit_time IS NOT NULL AND (NEW.commit_time NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.commit_time) IS NOT NEW.commit_time)) BEGIN
  SELECT RAISE(ABORT, 'review_repositories: commit_time must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_reviewer_runs_ins BEFORE INSERT ON reviewer_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.finished_at IS NOT NULL AND (NEW.finished_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.finished_at) IS NOT NEW.finished_at)) BEGIN
  SELECT RAISE(ABORT, 'reviewer_runs: started_at, finished_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_reviewer_runs_upd BEFORE UPDATE OF started_at, finished_at ON reviewer_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.finished_at IS NOT NULL AND (NEW.finished_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.finished_at) IS NOT NEW.finished_at)) BEGIN
  SELECT RAISE(ABORT, 'reviewer_runs: started_at, finished_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_requirements_ins BEFORE INSERT ON requirements
WHEN (NEW.stated_at IS NOT NULL AND (NEW.stated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.stated_at) IS NOT NEW.stated_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'requirements: stated_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_requirements_upd BEFORE UPDATE OF stated_at, created_at ON requirements
WHEN (NEW.stated_at IS NOT NULL AND (NEW.stated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.stated_at) IS NOT NEW.stated_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'requirements: stated_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_issues_ins BEFORE INSERT ON issues
WHEN (NEW.opened_at IS NOT NULL AND (NEW.opened_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.opened_at) IS NOT NEW.opened_at))
     OR (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at))
     OR (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'issues: opened_at, resolved_at, verified_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_issues_upd BEFORE UPDATE OF opened_at, resolved_at, verified_at, updated_at ON issues
WHEN (NEW.opened_at IS NOT NULL AND (NEW.opened_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.opened_at) IS NOT NEW.opened_at))
     OR (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at))
     OR (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'issues: opened_at, resolved_at, verified_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_issue_relations_ins BEFORE INSERT ON issue_relations
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'issue_relations: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_issue_relations_upd BEFORE UPDATE OF created_at ON issue_relations
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'issue_relations: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_findings_ins BEFORE INSERT ON findings
WHEN (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'findings: verified_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_findings_upd BEFORE UPDATE OF verified_at, created_at ON findings
WHEN (NEW.verified_at IS NOT NULL AND (NEW.verified_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.verified_at) IS NOT NEW.verified_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'findings: verified_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_review_issue_checks_ins BEFORE INSERT ON review_issue_checks
WHEN (NEW.checked_at IS NOT NULL AND (NEW.checked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.checked_at) IS NOT NEW.checked_at)) BEGIN
  SELECT RAISE(ABORT, 'review_issue_checks: checked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_review_issue_checks_upd BEFORE UPDATE OF checked_at ON review_issue_checks
WHEN (NEW.checked_at IS NOT NULL AND (NEW.checked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.checked_at) IS NOT NEW.checked_at)) BEGIN
  SELECT RAISE(ABORT, 'review_issue_checks: checked_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_decisions_ins BEFORE INSERT ON decisions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at))
     OR (NEW.decided_at IS NOT NULL AND (NEW.decided_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.decided_at) IS NOT NEW.decided_at)) BEGIN
  SELECT RAISE(ABORT, 'decisions: asked_at, decided_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_decisions_upd BEFORE UPDATE OF asked_at, decided_at ON decisions
WHEN (NEW.asked_at IS NOT NULL AND (NEW.asked_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.asked_at) IS NOT NEW.asked_at))
     OR (NEW.decided_at IS NOT NULL AND (NEW.decided_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.decided_at) IS NOT NEW.decided_at)) BEGIN
  SELECT RAISE(ABORT, 'decisions: asked_at, decided_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_gates_ins BEFORE INSERT ON gates
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at))
     OR (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'gates: answered_at, applied_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_gates_upd BEFORE UPDATE OF answered_at, applied_at, created_at ON gates
WHEN (NEW.answered_at IS NOT NULL AND (NEW.answered_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.answered_at) IS NOT NEW.answered_at))
     OR (NEW.applied_at IS NOT NULL AND (NEW.applied_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.applied_at) IS NOT NEW.applied_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'gates: answered_at, applied_at, created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_plans_ins BEFORE INSERT ON plans
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'plans: approved_at, created_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_plans_upd BEFORE UPDATE OF approved_at, created_at, updated_at ON plans
WHEN (NEW.approved_at IS NOT NULL AND (NEW.approved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.approved_at) IS NOT NEW.approved_at))
     OR (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'plans: approved_at, created_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_steps_ins BEFORE INSERT ON steps
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.done_at IS NOT NULL AND (NEW.done_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.done_at) IS NOT NEW.done_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'steps: started_at, done_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_steps_upd BEFORE UPDATE OF started_at, done_at, updated_at ON steps
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at))
     OR (NEW.done_at IS NOT NULL AND (NEW.done_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.done_at) IS NOT NEW.done_at))
     OR (NEW.updated_at IS NOT NULL AND (NEW.updated_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at)) BEGIN
  SELECT RAISE(ABORT, 'steps: started_at, done_at, updated_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_step_commits_ins BEFORE INSERT ON step_commits
WHEN (NEW.committed_at IS NOT NULL AND (NEW.committed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.committed_at) IS NOT NEW.committed_at)) BEGIN
  SELECT RAISE(ABORT, 'step_commits: committed_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_step_commits_upd BEFORE UPDATE OF committed_at ON step_commits
WHEN (NEW.committed_at IS NOT NULL AND (NEW.committed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.committed_at) IS NOT NEW.committed_at)) BEGIN
  SELECT RAISE(ABORT, 'step_commits: committed_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_guard_tests_ins BEFORE INSERT ON guard_tests
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'guard_tests: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_guard_tests_upd BEFORE UPDATE OF created_at ON guard_tests
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'guard_tests: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_test_runs_ins BEFORE INSERT ON test_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at)) BEGIN
  SELECT RAISE(ABORT, 'test_runs: started_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_test_runs_upd BEFORE UPDATE OF started_at ON test_runs
WHEN (NEW.started_at IS NOT NULL AND (NEW.started_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.started_at) IS NOT NEW.started_at)) BEGIN
  SELECT RAISE(ABORT, 'test_runs: started_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_metrics_ins BEFORE INSERT ON metrics
WHEN (NEW.recorded_at IS NOT NULL AND (NEW.recorded_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.recorded_at) IS NOT NEW.recorded_at)) BEGIN
  SELECT RAISE(ABORT, 'metrics: recorded_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_metrics_upd BEFORE UPDATE OF recorded_at ON metrics
WHEN (NEW.recorded_at IS NOT NULL AND (NEW.recorded_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.recorded_at) IS NOT NEW.recorded_at)) BEGIN
  SELECT RAISE(ABORT, 'metrics: recorded_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_step_conflicts_ins BEFORE INSERT ON step_conflicts
WHEN (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at)) BEGIN
  SELECT RAISE(ABORT, 'step_conflicts: resolved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_step_conflicts_upd BEFORE UPDATE OF resolved_at ON step_conflicts
WHEN (NEW.resolved_at IS NOT NULL AND (NEW.resolved_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.resolved_at) IS NOT NEW.resolved_at)) BEGIN
  SELECT RAISE(ABORT, 'step_conflicts: resolved_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_artifacts_ins BEFORE INSERT ON artifacts
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'artifacts: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_artifacts_upd BEFORE UPDATE OF created_at ON artifacts
WHEN (NEW.created_at IS NOT NULL AND (NEW.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.created_at) IS NOT NEW.created_at)) BEGIN
  SELECT RAISE(ABORT, 'artifacts: created_at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_events_ins BEFORE INSERT ON events
WHEN (NEW.at IS NOT NULL AND (NEW.at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.at) IS NOT NEW.at)) BEGIN
  SELECT RAISE(ABORT, 'events: at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_time_events_upd BEFORE UPDATE OF at ON events
WHEN (NEW.at IS NOT NULL AND (NEW.at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.at) IS NOT NEW.at)) BEGIN
  SELECT RAISE(ABORT, 'events: at must be UTC times like 2026-11-10T09:30:00.000Z');
END;
CREATE TRIGGER tr_plan_approval_conflicts BEFORE UPDATE OF status ON plans
WHEN NEW.status = 'approved' AND OLD.status <> 'approved' AND EXISTS (
  SELECT 1 FROM step_conflicts c JOIN steps s ON s.id = c.step_a_id
  WHERE s.plan_id = NEW.id AND c.resolution = 'none' AND c.late_step_id IS NULL) BEGIN
  SELECT RAISE(ABORT, 'the plan has unresolved conflicts between steps (ledger.py conflicts lists them; judge each with resolve-conflict)');
END;
CREATE TRIGGER tr_step_start_conflicts BEFORE UPDATE OF status ON steps
WHEN NEW.status = 'doing' AND OLD.status <> 'doing' AND EXISTS (
  SELECT 1 FROM step_conflicts c WHERE c.late_step_id = NEW.id AND c.resolution = 'none') BEGIN
  SELECT RAISE(ABORT, 'this step has unresolved conflicts with other steps (ledger.py conflicts lists them; judge each with resolve-conflict)');
END;
CREATE VIEW v_open_issues AS
SELECT i.code, i.title, i.severity, i.status, i.domain_code, i.theme_code, i.sub,
       i.times_seen, fr.code AS first_seen, fr.started_at AS first_seen_at,
       lr.code AS last_seen, cr.code AS last_checked, i.current_state, i.recommendation,
       (SELECT group_concat(DISTINCT CASE WHEN (SELECT count(*) FROM repositories) > 1 THEN rp.name || ':' ELSE '' END
                                     || fl.path || COALESCE('#' || fl.symbol, ''))
          FROM v_real_findings f JOIN finding_locations fl ON fl.finding_id = f.id
          JOIN repositories rp ON rp.id = fl.repository_id
         WHERE f.issue_id = i.id AND fl.role = 'primary') AS locations
FROM issues i
JOIN reviews fr ON fr.id = i.first_seen_review_id
LEFT JOIN reviews lr ON lr.id = i.last_seen_review_id
LEFT JOIN reviews cr ON cr.id = i.last_checked_review_id
WHERE i.status IN (SELECT value FROM issue_statuses WHERE active = 1)
ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END,
         fr.started_at;
CREATE VIEW v_known_issues_for_brief AS
SELECT i.domain_code, i.code, i.title, i.severity, i.status, i.current_state,
       i.recommendation, i.resolution,
       (SELECT group_concat(test_path || '::' || test_name, '; ')
          FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1) AS guard_tests,
       (SELECT group_concat(DISTINCT fl.path || COALESCE('#' || fl.symbol, ''))
          FROM findings f JOIN finding_locations fl ON fl.finding_id = f.id
         WHERE f.issue_id = i.id AND fl.role = 'primary') AS locations,
       (SELECT group_concat(x.line, '; ') FROM
          (SELECT d.code || ' (' || d.status || '): ' || d.question || COALESCE(' -> ' || d.answer, '') AS line
             FROM decision_issues di JOIN decisions d ON d.id = di.decision_id
            WHERE di.issue_id = i.id AND d.status IN ('decided','revisit') ORDER BY d.code) x) AS decisions
FROM issues i
WHERE i.status IN (SELECT value FROM issue_statuses WHERE briefed = 1)
ORDER BY i.domain_code, i.id;
CREATE VIEW v_recurring_issues AS
SELECT i.code, i.title, i.severity, i.status, i.times_seen,
       (SELECT count(*) FROM events e WHERE e.entity_type = 'issue' AND e.entity_id = i.id
          AND e.event = 'status-changed' AND e.to_value = 'regressed') AS regressions,
       (SELECT group_concat(r.code, ', ') FROM
          (SELECT DISTINCT f.review_id FROM v_real_findings f WHERE f.issue_id = i.id) x
          JOIN reviews r ON r.id = x.review_id) AS reviews
FROM issues i
WHERE i.times_seen > 1 OR EXISTS (SELECT 1 FROM events e WHERE e.entity_type = 'issue'
        AND e.entity_id = i.id AND e.to_value = 'regressed')
ORDER BY regressions DESC, i.times_seen DESC;
CREATE VIEW v_issue_timeline AS
SELECT i.code, e.at, e.event, e.from_value, e.to_value, r.code AS review,
       s.number AS step, e.commit_sha, a.name AS actor, e.note
FROM events e
JOIN issues i ON e.entity_type = 'issue' AND e.entity_id = i.id
LEFT JOIN reviews r ON r.id = e.review_id
LEFT JOIN steps s ON s.id = e.step_id
LEFT JOIN actors a ON a.id = e.actor_id
ORDER BY i.code, e.at, e.id;
CREATE VIEW v_issue_graph AS
SELECT a.code AS from_issue, r.relation, b.code AS to_issue, r.confidence, r.rationale,
       rv.code AS established_in
FROM issue_relations r
JOIN issues a ON a.id = r.from_issue_id
JOIN issues b ON b.id = r.to_issue_id
LEFT JOIN reviews rv ON rv.id = r.established_in_review_id;
CREATE VIEW v_review_summary AS
SELECT r.code AS review, r.started_at, f.domain_code,
       sum(f.verification <> 'false') AS points,
       sum(f.verification <> 'false' AND f.severity = 'H') AS high,
       sum(f.verification <> 'false' AND f.severity = 'M') AS medium,
       sum(f.verification <> 'false' AND f.severity = 'L') AS low,
       sum(f.verification <> 'false' AND f.severity = 'T') AS tests,
       sum(f.verification = 'confirmed') AS confirmed,
       sum(f.verification = 'false') AS false_findings,
       sum(f.verification <> 'false' AND f.match_type IN ('new','continuation')) AS new_issues,  -- a continuation opens a new issue
       sum(f.verification <> 'false' AND f.match_type IN ('still-present','changed')) AS carried_over,
       (SELECT count(DISTINCT e.entity_id) FROM events e
         WHERE e.entity_type = 'issue' AND e.review_id = r.id
           AND ((e.event = 'status-changed' AND (e.to_value = 'regressed'
                 OR (e.to_value = 'open' AND e.from_value IN (SELECT value FROM issue_statuses WHERE reopens = 1))))
                OR e.event = 'recurred')
           AND EXISTS (SELECT 1 FROM v_real_findings f2 WHERE f2.review_id = r.id AND f2.issue_id = e.entity_id
                         AND f2.domain_code = f.domain_code)) AS came_back
FROM findings f JOIN reviews r ON r.id = f.review_id
GROUP BY r.id, r.code, f.domain_code
ORDER BY r.started_at, f.domain_code;
CREATE VIEW v_review_totals AS
SELECT r.code AS review, r.started_at, count(f.id) AS points,
       COALESCE(sum(f.severity = 'H'), 0) AS high, COALESCE(sum(f.severity = 'M'), 0) AS medium,
       COALESCE(sum(f.severity = 'L'), 0) AS low, COALESCE(sum(f.severity = 'T'), 0) AS tests,
       COALESCE(sum(f.match_type IN ('new','continuation')), 0) AS new_issues,
       COALESCE(sum(f.match_type IN (SELECT value FROM match_types WHERE came_back = 1)), 0) AS came_back
FROM reviews r LEFT JOIN v_real_findings f ON f.review_id = r.id
GROUP BY r.id, r.code;
CREATE VIEW v_unmatched_findings AS
SELECT r.code AS review, f.point_id, f.severity, f.summary
FROM findings f JOIN reviews r ON r.id = f.review_id
WHERE f.issue_id IS NULL AND r.status <> 'abandoned';
CREATE VIEW v_real_findings AS
SELECT * FROM findings WHERE verification <> 'false';
CREATE VIEW v_unchecked_known_issues AS
SELECT r.code AS review, i.code AS issue, i.domain_code, i.title
FROM reviews r
JOIN review_domains rd ON rd.review_id = r.id AND rd.decision = 'included'
JOIN issues i ON i.domain_code = rd.domain_code
WHERE r.status <> 'abandoned'
  AND i.first_seen_review_id <> r.id
  AND julianday(i.opened_at) < julianday(r.started_at)
  AND i.status IN (SELECT value FROM issue_statuses WHERE briefed = 1)
  AND NOT EXISTS (SELECT 1 FROM review_issue_checks c WHERE c.review_id = r.id AND c.issue_id = i.id);
CREATE VIEW v_uncovered_findings AS
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
CREATE VIEW v_coverage_problems AS
SELECT p.code AS plan, 'step closes nothing' AS problem, CAST(s.number AS TEXT) AS item
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
  AND NULLIF(trim(s.no_findings_reason), '') IS NULL
  AND NOT EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
UNION ALL
SELECT p.code, 'finding closed by several steps without a reason', f.point_id
FROM step_findings sf JOIN steps s ON s.id = sf.step_id JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
WHERE p.status NOT IN ('superseded','abandoned') AND s.status <> 'dropped'
GROUP BY p.id, sf.finding_id
HAVING count(*) > 1 AND sum(sf.multi_reason IS NULL) > 0;
CREATE VIEW v_plan_progress AS
SELECT p.code AS plan, ph.number AS phase, ph.title,
       min(s.number) AS first_step, max(s.number) AS last_step, count(s.id) AS steps,
       COALESCE(sum(s.status = 'done'), 0) AS done, COALESCE(sum(s.status = 'doing'), 0) AS doing,
       COALESCE(sum(s.status = 'waiting-owner'), 0) AS waiting_owner,
       COALESCE(sum(s.status = 'not-started'), 0) AS not_started,
       COALESCE(sum(s.status = 'dropped'), 0) AS dropped   -- 0, not NULL, for a phase with no steps
FROM phases ph JOIN plans p ON p.id = ph.plan_id
LEFT JOIN steps s ON s.phase_id = ph.id
GROUP BY ph.id ORDER BY p.code, ph.number;
CREATE VIEW v_plan_totals AS
SELECT p.code AS plan, p.title, p.status, p.created_at, p.approved_at, count(s.id) AS steps,
       COALESCE(sum(s.status = 'done'), 0) AS done, COALESCE(sum(s.status = 'doing'), 0) AS doing,
       COALESCE(sum(s.status = 'waiting-owner'), 0) AS waiting_owner,
       COALESCE(sum(s.status = 'not-started'), 0) AS not_started,
       COALESCE(sum(s.status = 'dropped'), 0) AS dropped,
       (SELECT group_concat(r.code, ', ') FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id
         WHERE pr.plan_id = p.id) AS reviews
FROM plans p LEFT JOIN steps s ON s.plan_id = p.id
GROUP BY p.id, p.code;
CREATE VIEW v_pending_gates AS
SELECT g.id, g.code, g.kind, g.title, g.question, g.status, g.blocking, g.created_at,
       r.code AS review_code, pl.code AS plan_code, s.number AS step_number, i.code AS issue_code,
       d.code AS decision_code
FROM gates g
LEFT JOIN reviews r ON r.id = g.review_id LEFT JOIN plans pl ON pl.id = g.plan_id
LEFT JOIN steps s ON s.id = g.step_id LEFT JOIN issues i ON i.id = g.issue_id
LEFT JOIN decisions d ON d.id = g.decision_id
WHERE g.status IN ('open','answered')
ORDER BY g.status = 'answered', g.blocking DESC, g.created_at;
CREATE VIEW v_owner_queue AS
SELECT 'gate' AS kind, g.code AS ref, g.kind AS need, g.title, g.question,
       g.status, g.blocking, g.created_at, g.context
FROM gates g WHERE g.status IN ('open','answered')
UNION ALL
SELECT 'step', p.code || ' #' || s.number, s.owner_need, s.title,
       COALESCE(NULLIF(trim(s.owner_question), ''), NULLIF(trim(s.progress_note), ''), s.change), 'open',
       EXISTS (SELECT 1 FROM step_dependencies sd JOIN steps ds ON ds.id = sd.step_id
               WHERE sd.depends_on_step_id = s.id AND ds.status NOT IN ('done','dropped')), s.updated_at, NULL
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE s.status = 'waiting-owner'
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = s.id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'decision', d.code, 'decision',
       CASE d.status WHEN 'revisit' THEN 'A decision to look at again' ELSE 'A decision to make' END,
       d.question, d.status, 0, d.asked_at, d.revisit_when
FROM decisions d WHERE d.status IN ('open','revisit')
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.decision_id = d.id AND g.status IN ('open','answered'))
  AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.step_id = d.step_id AND g.status IN ('open','answered'))
UNION ALL
SELECT 'goal', g.code, 'confirm', 'Approve a design goal', g.text, g.status, 1, g.adopted_at, NULL
FROM design_goals g WHERE g.status = 'proposed' AND g.review_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM gates x WHERE x.id = g.gate_id AND x.status IN ('open','answered'));
CREATE VIEW v_code_refs AS
SELECT r.code AS review, f.point_id, i.code AS issue, fl.id AS location_id, fl.role,
       rp.name AS repository, rp.path AS repo_path, COALESCE(fl.commit_sha, rr.commit_sha) AS commit_sha,
       CASE WHEN fl.commit_sha IS NOT NULL THEN 'location' WHEN rr.commit_sha IS NOT NULL THEN 'review' END
         AS commit_from,
       rr.tag_name,
       fl.path, fl.line_start, fl.line_end, fl.symbol, fl.blob_sha, fl.permalink,
       -- every argument single-quoted for a POSIX shell (an embedded quote is closed, escaped and reopened);
       -- ledger.py show, /api/source and the viewer's Copy button all use this one command
       'git -C ''' || replace(COALESCE(NULLIF(rp.path, ''), '.'), '''', '''\''''') || ''' show '''
         || replace(COALESCE(fl.commit_sha, rr.commit_sha) || ':' || fl.path, '''', '''\''''') || ''''
         || CASE WHEN fl.line_start IS NOT NULL
                 THEN ' | sed -n ''' || fl.line_start || ',' || COALESCE(fl.line_end, fl.line_start) || 'p'''
                 ELSE '' END AS git_command,
       fl.excerpt IS NOT NULL AS has_excerpt
FROM finding_locations fl
JOIN findings f ON f.id = fl.finding_id
JOIN reviews r ON r.id = f.review_id
JOIN repositories rp ON rp.id = fl.repository_id
LEFT JOIN review_repositories rr ON rr.review_id = f.review_id AND rr.repository_id = fl.repository_id
LEFT JOIN issues i ON i.id = f.issue_id;
CREATE VIEW v_untraceable_refs AS
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
CREATE VIEW v_unplanned_discoveries AS
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
CREATE VIEW v_artifacts_outside_ledger AS
SELECT r.code AS review, a.kind, a.path
FROM artifacts a LEFT JOIN reviews r ON r.id = a.review_id
WHERE a.path IS NOT NULL AND a.kind IN ('brief','report','index')
  AND replace(a.path, char(92), '/') NOT LIKE '.code-review/%'
  AND replace(a.path, char(92), '/') NOT LIKE
      replace(COALESCE((SELECT value FROM meta WHERE key = 'ledger_location'), '.code-review'), char(92), '/') || '/%';
CREATE VIEW v_unguarded_fixes AS
SELECT i.code, i.title, i.status,
       CASE WHEN NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id)
            THEN 'no guard test' ELSE 'guard test removed' END AS problem
FROM issues i
WHERE i.status IN (SELECT value FROM issue_statuses WHERE fixed = 1) AND COALESCE(i.kind, '') NOT IN ('docs','design')
  AND NOT EXISTS (SELECT 1 FROM guard_tests g WHERE g.issue_id = i.id AND g.still_exists = 1);
CREATE VIEW v_metric_trends AS
SELECT m.name, m.scope, COALESCE(rp.name, 'all') AS repository, r.code AS review,
       r.started_at, m.value, COALESCE(m.unit, md.unit) AS unit, m.method,
       COALESCE(md.label, upper(substr(m.name, 1, 1)) || replace(substr(m.name, 2), '_', ' ')) AS label,
       COALESCE(md.better, 'none') AS better
FROM metrics m JOIN reviews r ON r.id = m.review_id
LEFT JOIN repositories rp ON rp.id = m.repository_id
LEFT JOIN metric_defs md ON md.name = m.name
ORDER BY m.name, m.scope, r.started_at;
CREATE VIEW v_file_hotspots AS
SELECT rp.name AS repository, fl.path, count(DISTINCT f.issue_id) AS issues,
       count(DISTINCT f.review_id) AS reviews, sum(f.severity = 'H') AS high_points
FROM finding_locations fl JOIN v_real_findings f ON f.id = fl.finding_id
JOIN repositories rp ON rp.id = fl.repository_id
GROUP BY fl.repository_id, fl.path ORDER BY issues DESC, high_points DESC, rp.name, fl.path;
CREATE VIEW v_standing_decisions AS
SELECT d.code, d.question, d.answer, d.rationale, d.revisit_when, d.decided_at,
       (SELECT group_concat(i.code, ', ') FROM decision_issues di
          JOIN issues i ON i.id = di.issue_id WHERE di.decision_id = d.id) AS issues
FROM decisions d WHERE d.status = 'decided';
CREATE VIEW v_step_issues AS
SELECT p.code AS plan, s.number AS step, s.title AS step_title, s.status AS step_status,
       r.code AS review, f.point_id, i.code AS issue, i.title AS issue_title, i.status AS issue_status
FROM steps s
JOIN plans p ON p.id = s.plan_id
JOIN step_findings sf ON sf.step_id = s.id
JOIN findings f ON f.id = sf.finding_id
JOIN reviews r ON r.id = f.review_id
LEFT JOIN issues i ON i.id = f.issue_id
ORDER BY p.code, s.number, r.code, f.point_id;
CREATE VIEW v_next_codes AS
SELECT 'ISS' AS prefix, 'ISS-' || printf('%04d', COALESCE(max(CAST(substr(code, 5) AS INTEGER)), 0) + 1) AS next_code
FROM issues
UNION ALL SELECT 'R', 'R-' || printf('%04d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM reviews
UNION ALL SELECT 'P', 'P-' || printf('%04d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM plans
UNION ALL SELECT 'D', 'D-' || printf('%04d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM decisions
UNION ALL SELECT 'GATE', 'GATE-' || printf('%04d', COALESCE(max(CAST(substr(code, 6) AS INTEGER)), 0) + 1) FROM gates
UNION ALL SELECT 'REQ', 'REQ-' || printf('%04d', COALESCE(max(CAST(substr(code, 5) AS INTEGER)), 0) + 1)
FROM requirements
UNION ALL SELECT 'G', 'G-' || printf('%02d', COALESCE(max(CAST(substr(code, 3) AS INTEGER)), 0) + 1) FROM design_goals
UNION ALL SELECT 'IQ', 'IQ-' || printf('%04d', COALESCE(max(CAST(substr(code, 4) AS INTEGER)), 0) + 1)
FROM interview_questions;
CREATE VIEW v_stale_steps AS
SELECT p.code AS plan, s.number AS step, s.title, s.status,
       (SELECT group_concat(x.issue, ', ') FROM
          (SELECT DISTINCT i.code || ' ' || i.status AS issue FROM step_findings sf
             JOIN findings f ON f.id = sf.finding_id JOIN issues i ON i.id = f.issue_id
            WHERE sf.step_id = s.id ORDER BY 1) x) AS issues
FROM steps s JOIN plans p ON p.id = s.plan_id
WHERE s.status NOT IN ('done','dropped')
  AND p.status NOT IN ('superseded','abandoned')
  AND EXISTS (SELECT 1 FROM step_findings sf WHERE sf.step_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM step_findings sf JOIN findings f ON f.id = sf.finding_id
                  LEFT JOIN issues i ON i.id = f.issue_id
                  WHERE sf.step_id = s.id
                    AND (i.id IS NULL OR i.status IN
                         (SELECT value FROM issue_statuses WHERE active = 1)));
CREATE VIEW v_last_reviewed_commit AS
SELECT rp.name AS repository, rp.path AS repo_path, r.code AS review, rr.commit_sha, rr.branch, rr.tag_name,
       r.completed_at
FROM repositories rp
JOIN review_repositories rr ON rr.repository_id = rp.id
JOIN reviews r ON r.id = rr.review_id
WHERE r.id = (SELECT r2.id FROM review_repositories rr2 JOIN reviews r2 ON r2.id = rr2.review_id
              WHERE rr2.repository_id = rp.id AND r2.status = 'handed-off'
              ORDER BY COALESCE(r2.completed_at, r2.started_at) DESC, r2.id DESC LIMIT 1)
ORDER BY rp.name;
CREATE VIEW v_requirement_trace AS
SELECT r.code AS review, q.code AS requirement, q.text, c.verdict, c.where_implemented, c.tested,
       f.point_id, c.evidence,
       (SELECT count(*) FROM review_requirement_checks c2
         WHERE c2.review_id = c.review_id AND c2.verdict = c.verdict) AS verdict_count
FROM review_requirement_checks c
JOIN reviews r ON r.id = c.review_id
JOIN requirements q ON q.id = c.requirement_id
LEFT JOIN findings f ON f.id = c.finding_id
ORDER BY r.code, q.code;
CREATE VIEW v_review_agents AS
SELECT r.code AS review, x.role, a.id AS actor_id, a.kind, a.name, a.model, a.harness, a.session_ref,
       pa.name AS parent
FROM (SELECT id AS review_id, manager_actor_id AS actor_id, 'manager' AS role FROM reviews
       WHERE manager_actor_id IS NOT NULL
      UNION SELECT review_id, actor_id, label FROM reviewer_runs
      UNION SELECT review_id, actor_id, role_label FROM review_actors) x
JOIN reviews r ON r.id = x.review_id
JOIN actors a ON a.id = x.actor_id
LEFT JOIN actors pa ON pa.id = a.parent_actor_id
ORDER BY r.code, CASE a.kind WHEN 'manager' THEN 0 WHEN 'reviewer' THEN 1 ELSE 2 END, x.role, a.id;
CREATE VIEW v_history_gaps AS
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
CREATE VIEW v_match_problems AS
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
CREATE VIEW v_off_plan_coverage AS
SELECT p.code AS plan, s.number AS step, r.code AS review, f.point_id
FROM step_findings sf
JOIN steps s ON s.id = sf.step_id
JOIN plans p ON p.id = s.plan_id
JOIN findings f ON f.id = sf.finding_id
JOIN reviews r ON r.id = f.review_id
WHERE p.status NOT IN ('superseded','abandoned')
  AND NOT EXISTS (SELECT 1 FROM plan_reviews pr WHERE pr.plan_id = p.id AND pr.review_id = f.review_id);
CREATE VIEW v_done_steps_open_issues AS
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
CREATE VIEW v_closed_without_reason AS
SELECT code, title, status FROM issues
WHERE status IN ('accepted-risk','wont-fix') AND NULLIF(trim(resolution), '') IS NULL;
CREATE VIEW v_relation_problems AS
SELECT a.code AS from_issue, b.code AS to_issue, ir.relation, 'related-to stored in both directions' AS problem
FROM issue_relations ir
JOIN issues a ON a.id = ir.from_issue_id
JOIN issues b ON b.id = ir.to_issue_id
WHERE ir.relation = 'related-to'
  AND EXISTS (SELECT 1 FROM issue_relations rv WHERE rv.relation = 'related-to'
                AND rv.from_issue_id = ir.to_issue_id AND rv.to_issue_id = ir.from_issue_id AND rv.id < ir.id);
CREATE VIEW v_check_finding_owner AS
SELECT r.code AS review, i.code AS issue, f.point_id, fi.code AS finding_issue
FROM review_issue_checks c
JOIN reviews r ON r.id = c.review_id
JOIN issues i ON i.id = c.issue_id
JOIN findings f ON f.id = c.finding_id
JOIN issues fi ON fi.id = f.issue_id
WHERE f.issue_id <> c.issue_id;
CREATE VIEW v_goals_for_brief AS
SELECT g.domain_code, g.code, g.text,
       (SELECT group_concat(x.code, ', ') FROM
          (SELECT q.code FROM goal_sources gs JOIN interview_questions q ON q.id = gs.question_id
            WHERE gs.goal_id = g.id ORDER BY q.id) x) AS sources
FROM design_goals g
WHERE g.status IN (SELECT value FROM goal_statuses WHERE served = 1)
ORDER BY g.domain_code IS NOT NULL, g.domain_code, g.id;
CREATE VIEW v_unresolved_conflicts AS
SELECT p.code AS plan, a.number AS step_a, b.number AS step_b, c.kind, c.target, c.reason, c.opposite
FROM step_conflicts c JOIN steps a ON a.id = c.step_a_id JOIN steps b ON b.id = c.step_b_id
JOIN plans p ON p.id = a.plan_id
WHERE c.resolution = 'none' AND c.late_step_id IS NULL
ORDER BY p.code, a.number, b.number, c.kind, c.target;
CREATE VIEW v_late_step_conflicts AS
SELECT p.code AS plan, l.number AS step, CASE WHEN l.id = c.step_a_id THEN b.number ELSE a.number END AS other_step,
       c.kind, c.target, c.reason
FROM step_conflicts c JOIN steps a ON a.id = c.step_a_id JOIN steps b ON b.id = c.step_b_id
JOIN steps l ON l.id = c.late_step_id JOIN plans p ON p.id = a.plan_id
WHERE c.resolution = 'none'
ORDER BY p.code, l.number, other_step, c.kind, c.target;
COMMIT;
