-- A schema-23 ledger with a row in every table: the v1 demo (ledger-v1-demo.sql), migrations 2..23 and the rows of
-- _migrate_helpers.FILL. Frozen: regenerate only with python3 -m dev.tests._migrate_helpers --write-fixture 23
BEGIN TRANSACTION;
CREATE TABLE actors (
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('owner','human','manager','reviewer','verifier','indexer',
                     'matcher','drafter','fixer','tool')),
  name            TEXT NOT NULL,         -- person's name, or the agent's role label ("security reviewer")
  model           TEXT,                  -- model id for agents
  harness         TEXT,                  -- e.g. Claude Code, CI
  session_ref     TEXT,                  -- session id or URL
  parent_actor_id INTEGER REFERENCES actors(id),   -- subagent -> the agent that launched it
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "actors" VALUES(1,'owner','Demo Owner',NULL,NULL,NULL,NULL,'2026-11-01T09:01:00.000Z');
INSERT INTO "actors" VALUES(2,'manager','Review manager','model-a','Harness A',NULL,NULL,'2026-11-01T09:02:00.000Z');
INSERT INTO "actors" VALUES(3,'reviewer','security reviewer','model-a',NULL,NULL,2,'2026-11-01T09:03:00.000Z');
INSERT INTO "actors" VALUES(4,'reviewer','spec reviewer','model-a',NULL,NULL,2,'2026-11-01T09:04:00.000Z');
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
INSERT INTO "artifacts" VALUES(1,1,NULL,NULL,'summary-doc',NULL,'https://example.com/demo/R-0001',NULL,'2026-11-01T09:08:10.000Z');
CREATE TABLE checklist_answers (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  check_code TEXT NOT NULL REFERENCES domain_checks(code),
  report     TEXT NOT NULL,                -- the report's file name
  answer     TEXT NOT NULL CHECK (answer IN ('yes','no','partly','not-applicable')),
  evidence   TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
  PRIMARY KEY (review_id, check_code, report)
);
INSERT INTO "checklist_answers" VALUES(1,'spec.1','spec.md','yes','can_request read in full');
CREATE TABLE decision_issues (
  decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  issue_id    INTEGER NOT NULL REFERENCES issues(id),
  PRIMARY KEY (decision_id, issue_id)
);
INSERT INTO "decision_issues" VALUES(1,2);
CREATE TABLE "decisions" (
  id                  INTEGER PRIMARY KEY,
  code                TEXT NOT NULL UNIQUE
                        CHECK (code GLOB 'D-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'D-*[^0-9]*'),
  question            TEXT NOT NULL,
  options             TEXT CHECK (options IS NULL OR json_valid(options)),
  answer              TEXT,
  rationale           TEXT,
  status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','revisit'))
                        CHECK (status <> 'decided' OR answer IS NOT NULL),
  revisit_when        TEXT,
  review_id           INTEGER REFERENCES reviews(id),
  step_id             INTEGER REFERENCES steps(id),
  asked_by_actor_id   INTEGER REFERENCES actors(id),
  decided_by_actor_id INTEGER REFERENCES actors(id),
  asked_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at          TEXT
);
INSERT INTO "decisions" VALUES(1,'D-0001','Should the default notice period stay 14 days?',NULL,NULL,NULL,'open',NULL,1,3,2,NULL,'2026-11-01T09:23:00.000Z',NULL);
CREATE TABLE "design_goals" (
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
INSERT INTO "design_goals" VALUES(1,'G-01','Every rule and behavior is a setting.','owner','2026-11-01T09:05:00.000Z',NULL,NULL,NULL,'approved',1,'2026-11-01T09:05:00.000Z',NULL,NULL,NULL,NULL);
INSERT INTO "design_goals" VALUES(2,'G-02','No internal names on screen.','owner','2026-11-01T09:06:00.000Z',NULL,NULL,NULL,'approved',1,'2026-11-01T09:06:00.000Z',NULL,NULL,NULL,NULL);
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
INSERT INTO "domain_checks" VALUES('features.1','features',1,'L','Is every approved goal and active requirement listed with what the code does toward it today?','A row per goal and requirement: path:line of what serves it, or absent.');
INSERT INTO "domain_checks" VALUES('features.2','features',2,'L','Does each proposal name the goal or requirement it serves, its value, effort, risks and dependencies?','Each proposal with its goal code and the four answers.');
INSERT INTO "domain_checks" VALUES('features.3','features',3,'L','Does each proposal have a test that would show it is built?','The done-test per proposal: its name and what it asserts.');
INSERT INTO "domain_checks" VALUES('features.4','features',4,'L','Were the proposals already on record checked again against the goals and the code?','Per earlier proposal: still wanted, built, or serving no current goal.');
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
INSERT INTO "domain_rules" VALUES('features','goals >= 1 or requirements >= 1');
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
INSERT INTO "domains" VALUES('features','Features toward the goals','architecture','Applies when the review has approved goals or stated requirements.','features.md',NULL);
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
INSERT INTO "enum_labels" VALUES('location_source','commit','Read from the commit',1);
INSERT INTO "enum_labels" VALUES('location_source','working-tree','Read from the reviewed working tree',2);
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
INSERT INTO "enum_labels" VALUES('feature_status','proposed','Proposed',1);
INSERT INTO "enum_labels" VALUES('feature_status','picked','Picked',2);
INSERT INTO "enum_labels" VALUES('feature_status','declined','Declined',3);
INSERT INTO "enum_labels" VALUES('feature_status','built','Built',4);
INSERT INTO "enum_labels" VALUES('feature_value','high','High',1);
INSERT INTO "enum_labels" VALUES('feature_value','medium','Medium',2);
INSERT INTO "enum_labels" VALUES('feature_value','low','Low',3);
INSERT INTO "enum_labels" VALUES('feature_effort','small','Small',1);
INSERT INTO "enum_labels" VALUES('feature_effort','medium','Medium',2);
INSERT INTO "enum_labels" VALUES('feature_effort','large','Large',3);
INSERT INTO "enum_labels" VALUES('tool_run_status','ran','Ran',1);
INSERT INTO "enum_labels" VALUES('tool_run_status','imported','Imported from a file',2);
INSERT INTO "enum_labels" VALUES('tool_run_status','absent','Not installed',3);
INSERT INTO "enum_labels" VALUES('tool_run_status','skipped','Not run',4);
INSERT INTO "enum_labels" VALUES('tool_run_status','failed','Failed',5);
INSERT INTO "enum_labels" VALUES('tool_output_format','sarif','SARIF',1);
INSERT INTO "enum_labels" VALUES('tool_output_format','junit','JUnit XML',2);
INSERT INTO "enum_labels" VALUES('tool_output_format','lcov','LCOV',3);
INSERT INTO "enum_labels" VALUES('tool_output_format','cobertura','Cobertura XML',4);
INSERT INTO "enum_labels" VALUES('tool_output_format','json','JSON',5);
INSERT INTO "enum_labels" VALUES('tool_output_format','jsonl','JSON lines',6);
INSERT INTO "enum_labels" VALUES('tool_result_level','error','Error',1);
INSERT INTO "enum_labels" VALUES('tool_result_level','warning','Warning',2);
INSERT INTO "enum_labels" VALUES('tool_result_level','note','Note',3);
INSERT INTO "enum_labels" VALUES('tool_result_level','none','No level',4);
INSERT INTO "enum_labels" VALUES('event','answers-imported','Answers brought in from the published page',31);
INSERT INTO "enum_labels" VALUES('event','rematched','A point was moved to the right issue',32);
INSERT INTO "enum_labels" VALUES('event','location-corrected','Where a point is was corrected',33);
INSERT INTO "enum_labels" VALUES('event','check-corrected','A review check was corrected',34);
INSERT INTO "enum_labels" VALUES('event','guard-dropped','A guard test was dropped',35);
INSERT INTO "enum_labels" VALUES('event','guard-renamed','A guard test was renamed',36);
CREATE TABLE "events" (
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
INSERT INTO "events" VALUES(1,'review',1,'status-changed','planning','handed-off',NULL,1,NULL,NULL,NULL,'2026-11-11T05:25:14.075Z');
INSERT INTO "events" VALUES(2,'issue',1,'opened',NULL,'open',2,1,NULL,NULL,NULL,'2026-11-11T05:25:14.076Z');
INSERT INTO "events" VALUES(3,'issue',1,'seen',NULL,'new',NULL,1,NULL,NULL,'SEC-H1','2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(4,'issue',2,'opened',NULL,'open',2,1,NULL,NULL,NULL,'2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(5,'issue',2,'seen',NULL,'new',NULL,1,NULL,NULL,'SPEC-M1','2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(6,'issue',3,'opened',NULL,'open',2,1,NULL,NULL,NULL,'2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(7,'issue',3,'seen',NULL,'new',NULL,1,NULL,NULL,'SPEC-L1','2026-11-11T05:25:14.077Z');
INSERT INTO "events" VALUES(8,'plan',1,'status-changed','drafting','in-progress',NULL,NULL,NULL,NULL,NULL,'2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(9,'step',2,'status-changed','not-started','doing',2,NULL,2,NULL,NULL,'2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(10,'step',2,'status-changed','doing','done',2,NULL,2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','Sign-in codes now come from a secure generator.','2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(11,'issue',1,'status-changed','open','fixed',2,1,2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','Step 2 done','2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(12,'step',3,'status-changed','not-started','waiting-owner',NULL,NULL,3,NULL,NULL,'2026-11-11T05:25:14.078Z');
INSERT INTO "events" VALUES(13,'review',2,'status-changed','planning','awaiting-approval',NULL,2,NULL,NULL,NULL,'2026-11-11T05:25:14.079Z');
INSERT INTO "events" VALUES(14,'issue',1,'status-changed','fixed','verified',3,2,NULL,NULL,'review check: fixed — make_code now uses secrets.randbelow (src/auth.py:3)','2026-11-11T05:25:14.079Z');
INSERT INTO "events" VALUES(15,'issue',2,'seen',NULL,'still-present',NULL,2,NULL,NULL,'SPEC-M1','2026-11-11T05:25:14.079Z');
INSERT INTO "events" VALUES(16,'issue',4,'opened',NULL,'open',2,2,NULL,NULL,NULL,'2026-11-11T05:25:14.084Z');
INSERT INTO "events" VALUES(17,'issue',4,'seen',NULL,'new',NULL,2,NULL,NULL,'SEC-M1','2026-11-11T05:25:14.084Z');
CREATE TABLE feature_proposals (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE      -- FP-0001
                         CHECK (code GLOB 'FP-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'FP-*[^0-9]*'),
  review_id            INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id              INTEGER REFERENCES design_goals(id),     -- the approved goal it serves, or
  requirement_id       INTEGER REFERENCES requirements(id),     -- the active requirement it serves
  title                TEXT NOT NULL CHECK (length(trim(title)) > 0),
  today                TEXT NOT NULL CHECK (length(trim(today)) > 0),      -- what the code does toward it now
  evidence             TEXT NOT NULL CHECK (length(trim(evidence)) > 0),   -- path:line, or 'absent'
  proposal             TEXT NOT NULL CHECK (length(trim(proposal)) > 0),   -- the addition
  value                TEXT NOT NULL CHECK (value IN ('high','medium','low')),     -- how far it advances the goal
  effort               TEXT NOT NULL CHECK (effort IN ('small','medium','large')),
  risks                TEXT,
  depends              TEXT,                     -- what it needs first
  done_test            TEXT NOT NULL CHECK (length(trim(done_test)) > 0),  -- the test that shows it is built
  status               TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','picked','declined','built')),
  status_note          TEXT,                     -- why it was picked, declined or called built
  decided_by_actor_id  INTEGER REFERENCES actors(id),   -- who made the last status change
  decided_at           TEXT,
  gate_id              INTEGER REFERENCES gates(id),    -- the feature-pick gate the owner answered
  step_id              INTEGER REFERENCES steps(id),    -- the plan step that builds it
  proposed_by_actor_id INTEGER REFERENCES actors(id),
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK ((goal_id IS NULL) <> (requirement_id IS NULL)),
  CHECK (status = 'proposed' OR (decided_by_actor_id IS NOT NULL AND decided_at IS NOT NULL
                                 AND length(trim(COALESCE(status_note, ''))) > 0)),
  CHECK (step_id IS NULL OR status IN ('picked','built'))
);
INSERT INTO "feature_proposals" VALUES(1,'FP-0001',1,1,NULL,'Notice period per program','14 days for every program','src/requests.py:2','A notice_days setting per program','high','small',NULL,NULL,'test_notice_per_program','proposed',NULL,NULL,NULL,NULL,NULL,2,'2026-11-01T09:08:40.000Z');
CREATE TABLE feature_status_changes (
  id          INTEGER PRIMARY KEY,
  proposal_id INTEGER NOT NULL REFERENCES feature_proposals(id) ON DELETE RESTRICT,
  from_status TEXT,                      -- NULL when it was proposed
  to_status   TEXT NOT NULL,
  note        TEXT,
  actor_id    INTEGER REFERENCES actors(id),
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "feature_status_changes" VALUES(1,1,NULL,'proposed','proposed by the features reviewer',2,'2026-11-01T09:08:40.000Z');
CREATE TABLE "finding_goals" (
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  goal_id    INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (finding_id, goal_id)
);
INSERT INTO "finding_goals" VALUES(2,1);
CREATE TABLE "finding_locations" (
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
  excerpt_status TEXT CHECK (excerpt_status IS NULL OR excerpt_status IN ('truncated','not-text')), source TEXT NOT NULL DEFAULT 'commit' CHECK (source IN ('commit','working-tree')), change_actor_id INTEGER REFERENCES actors(id), change_note TEXT,
                                            -- truncated = a whole-file excerpt holds only its first 200 lines;
                                            -- not-text = binary or not UTF-8: no excerpt, excerpt_sha256 is of
                                            -- the file's bytes
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);
INSERT INTO "finding_locations" VALUES(1,1,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','src/auth.py',1,3,'make_code','1baf0b7838848c9d10acbd97f658d4216ef3cb33','import random
def make_code():
    return f"{random.randint(0, 999999):06d}"','60ac807b60625602459422c3fc375fe38d986b39458dd1780bdd9e4608b4aecb','https://github.com/example/demo/blob/16822abc0b87e79b664c98f386b3354147e0f1bc/src/auth.py#L1-L3','primary',0,0,NULL,'commit',NULL,NULL);
INSERT INTO "finding_locations" VALUES(2,2,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','src/timeoff.py',2,4,'can_request','168c600dc4f8f17a3edb63936f393a3420163fc8','def can_request(user, days_ahead, weekend):
    if days_ahead < NOTICE_DAYS:
        return False','e744180489f92abed87806426451d36a4e699caef1a83537e1424b902378d881','https://github.com/example/demo/blob/16822abc0b87e79b664c98f386b3354147e0f1bc/src/timeoff.py#L2-L4','primary',0,0,NULL,'commit',NULL,NULL);
INSERT INTO "finding_locations" VALUES(3,3,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','src/timeoff.py',5,6,'can_request','168c600dc4f8f17a3edb63936f393a3420163fc8','    if user.is_admin:
        return True','8d85823055eb5d195db34524855c0e47d1f803c5f2b1b1ebc416bd5ae8dbb9b9','https://github.com/example/demo/blob/16822abc0b87e79b664c98f386b3354147e0f1bc/src/timeoff.py#L5-L6','primary',0,0,NULL,'commit',NULL,NULL);
INSERT INTO "finding_locations" VALUES(4,4,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','src/timeoff.py',2,4,'can_request','168c600dc4f8f17a3edb63936f393a3420163fc8','def can_request(user, days_ahead, weekend):
    if days_ahead < NOTICE_DAYS:
        return False','e744180489f92abed87806426451d36a4e699caef1a83537e1424b902378d881','https://github.com/example/demo/blob/4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee/src/timeoff.py#L2-L4','primary',0,0,NULL,'commit',NULL,NULL);
INSERT INTO "finding_locations" VALUES(5,5,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','src/auth.py',4,5,'check','a03d48405198b5b43ac074bb87df9678b441c06e','def check(code, expected):
    return code == expected','124fc58f514b796cceae4dbae31ce6e721561c0f4bc9238e3fa2cf77fc4a03d8','https://github.com/example/demo/blob/4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee/src/auth.py#L4-L5','primary',0,0,NULL,'commit',NULL,NULL);
CREATE TABLE "finding_requirements" (
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  verdict        TEXT NOT NULL CHECK (verdict IN ('matches','differs','partly','missing','untested')),
  PRIMARY KEY (finding_id, requirement_id)
);
INSERT INTO "finding_requirements" VALUES(2,1,'differs');
CREATE TABLE "findings" (
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
INSERT INTO "findings" VALUES(1,1,1,'SEC-H1',1,'new','certain',NULL,'H','security','security','sign-in','code-randomness','Sign-in codes come from a predictable random generator.',NULL,'make_code uses random.randint, which is not suitable for secrets.','Use secrets.randbelow.','test_code_randomness',NULL,NULL,'security:sign-in:code-randomness',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:11:00.000Z',NULL);
INSERT INTO "findings" VALUES(2,1,2,'SPEC-M1',2,'new','certain',NULL,'M','defect','spec','spec-mismatch','notice-period','Notice period is hardcoded to 14 days instead of a setting.',NULL,'can_request refuses anything under 14 days; the owner asked for it to be a setting.','Read the notice period from program settings.','test_notice_period',NULL,NULL,'spec:spec-mismatch:notice-period',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:12:00.000Z',NULL);
INSERT INTO "findings" VALUES(3,1,2,'SPEC-L1',3,'new','certain',NULL,'L','defect','spec','spec-mismatch','admin-bypass','Admins skip the weekend limit entirely.',NULL,'Admins bypass the weekend-off rule.','Apply the same rule with an override that is recorded.','test_admin_bypass',NULL,NULL,'spec:spec-mismatch:admin-bypass',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:13:00.000Z',NULL);
INSERT INTO "findings" VALUES(4,2,4,'SPEC-M1',2,'still-present','certain','Same function and lines as ISS-0002','M','defect','spec','spec-mismatch','notice-period','Notice period is still 14 days in code.',NULL,'Unchanged since R-0001.','Read it from settings.','test_notice_period',NULL,NULL,'spec:spec-mismatch:notice-period',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:30:00.000Z',NULL);
INSERT INTO "findings" VALUES(5,2,3,'SEC-M1',4,'new','certain',NULL,'M','security','security','sign-in','code-compare','Code check uses == instead of a constant-time compare.',NULL,'check compares codes with ==.','Use hmac.compare_digest.','test_code_compare',NULL,NULL,'security:sign-in:code-compare',NULL,'confirmed',2,NULL,NULL,'2026-11-01T09:32:00.000Z',NULL);
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
INSERT INTO "gate_kinds" VALUES('feature-pick','Pick ideas to build',11);
CREATE TABLE "gates" (
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
, multi INTEGER NOT NULL DEFAULT 0 CHECK (multi IN (0,1)), preselect TEXT CHECK (preselect IS NULL OR (json_valid(preselect) AND json_type(preselect) = 'array')), change_note TEXT);
INSERT INTO "gates" VALUES(1,'GATE-0001','decision','Default notice period','Should the default notice period stay 14 days when it becomes a setting?','Today the code refuses requests made less than 14 days ahead. Programs will be able to change it.','[{"value": "keep-14", "label": "Keep 14 days", "description": "Current behaviour stays the default."}, {"value": "use-7", "label": "Change to 7 days"}]',1,1,'open',NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,1,3,2,1,2,'2026-11-01T09:24:00.000Z',NULL,NULL,0,NULL,NULL);
INSERT INTO "gates" VALUES(2,'GATE-0002','domain-approval','Approve the domains for R-0002','Run these 2 domains with 2 reviewers?','Included: Security, Spec conformance. Skipped: Database (no database).','[{"value": "approve", "label": "Approve as listed"}, {"value": "add", "label": "Add skipped domains"}]',1,1,'open',NULL,NULL,NULL,NULL,NULL,NULL,NULL,2,NULL,NULL,NULL,NULL,2,'2026-11-01T09:36:00.000Z',NULL,NULL,0,NULL,NULL);
CREATE TABLE goal_sources (
  goal_id     INTEGER NOT NULL REFERENCES design_goals(id) ON DELETE RESTRICT,
  question_id INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  PRIMARY KEY (goal_id, question_id)
);
INSERT INTO "goal_sources" VALUES(1,1);
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
CREATE TABLE "guard_tests" (
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
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), measured_at TEXT, measured_commit_sha TEXT CHECK (measured_commit_sha IS NULL OR ((length(measured_commit_sha) = 40 OR length(measured_commit_sha) = 64) AND measured_commit_sha NOT GLOB '*[^0-9a-f]*')),
  UNIQUE (issue_id, test_path, test_name)
);
INSERT INTO "guard_tests" VALUES(1,1,1,'tests/test_auth.py','test_code_uses_secrets',2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee',1,1,1,NULL,'2026-11-01T09:22:00.000Z',NULL,NULL);
CREATE TABLE interview_answers (
  id                   INTEGER PRIMARY KEY,
  question_id          INTEGER NOT NULL REFERENCES interview_questions(id) ON DELETE RESTRICT,
  answer               TEXT NOT NULL CHECK (length(trim(answer)) > 0),   -- the owner's words, verbatim
  answered_via         TEXT NOT NULL DEFAULT 'chat' CHECK (answered_via IN ('chat','viewer')),
  answered_by_actor_id INTEGER REFERENCES actors(id),   -- who recorded it
  answered_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "interview_answers" VALUES(1,1,'Yes, each program sets it.','chat',2,'2026-11-01T09:06:20.000Z');
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
INSERT INTO "interview_questions" VALUES(1,'IQ-0001',1,NULL,'Should every program set its own notice period?','can_request hardcodes 14 days',2,NULL,'2026-11-01T09:06:10.000Z');
CREATE TABLE "issue_relations" (
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
INSERT INTO "issue_relations" VALUES(1,4,1,'related-to','likely','Same sign-in code path as the earlier randomness issue',2,2,'2026-11-01T09:34:00.000Z');
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
CREATE TABLE "issues" (
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
  fingerprint            TEXT,                   -- domain:theme:sub[:symbol], for matching
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
INSERT INTO "issues" VALUES(1,'ISS-0001','Predictable sign-in codes','security','sign-in','code-randomness','security','H','verified','security:sign-in:code-randomness','Codes generated with random.randint.','make_code uses random.randint, which is not suitable for secrets.','Codes come from secrets.randbelow.',NULL,NULL,1,1,2,1,'2026-11-01T09:14:00.000Z','2026-11-11T05:25:14.078Z','2026-11-11T05:25:14.079Z','2026-11-11T05:25:14.079Z',3,2,NULL,NULL,'review check: fixed — make_code now uses secrets.randbelow (src/auth.py:3)');
INSERT INTO "issues" VALUES(2,'ISS-0002','Notice period hardcoded','spec','spec-mismatch','notice-period','defect','M','open','spec:spec-mismatch:notice-period','14 days in code.','Unchanged since R-0001.',NULL,NULL,NULL,1,2,2,2,'2026-11-01T09:15:00.000Z',NULL,NULL,'2026-11-11T05:25:14.079Z',4,2,NULL,NULL,'review check: still-present');
INSERT INTO "issues" VALUES(3,'ISS-0003','Admins bypass the weekend limit','spec','spec-mismatch','admin-bypass','defect','L','open','spec:spec-mismatch:admin-bypass','Admin bypass.','Admins bypass the weekend-off rule.',NULL,NULL,NULL,1,1,2,1,'2026-11-01T09:16:00.000Z',NULL,NULL,'2026-11-11T05:25:14.084Z',4,2,NULL,NULL,'review check: not-checked');
INSERT INTO "issues" VALUES(4,'ISS-0004','Sign-in code compare is not constant-time','security','sign-in','code-compare','security','M','open','security:sign-in:code-compare','== compare.','check compares codes with ==.',NULL,NULL,NULL,2,2,2,1,'2026-11-01T09:33:00.000Z',NULL,NULL,'2026-11-11T05:25:14.084Z',NULL,2,NULL,NULL,'matched by finding SEC-M1');
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
INSERT INTO "ledger_checks" VALUES('v_unaddressed_tool_results','Tool results no finding cites and no reviewer dismissed','error',84);
INSERT INTO "ledger_checks" VALUES('v_unproven_steps','Done steps with no test seen failing before the fix','warning',105);
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
  key   TEXT PRIMARY KEY,                -- schema_version, project_name, created_at, created_by_skill
  value TEXT NOT NULL
);
INSERT INTO "meta" VALUES('schema_version','23');
INSERT INTO "meta" VALUES('project_name','demo-v1');
INSERT INTO "meta" VALUES('created_at','2026-11-01T09:00:00.000Z');
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
CREATE TABLE "metrics" (
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
INSERT INTO "metrics" VALUES(1,1,'test-quality',NULL,'mutation_score','src',61.0,'%','measured',NULL,'2026-11-01T09:37:00.000Z');
INSERT INTO "metrics" VALUES(2,2,'test-quality',NULL,'mutation_score','src',74.5,'%','measured',NULL,'2026-11-01T09:38:00.000Z');
CREATE TABLE phases (
  id      INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  number  INTEGER NOT NULL,
  title   TEXT NOT NULL,
  goal    TEXT NOT NULL,                     -- one sentence
  why     TEXT,                              -- why it sits where it does
  UNIQUE (plan_id, number)
);
INSERT INTO "phases" VALUES(1,1,1,'Safety net','Every later step is checked by tests.',NULL);
INSERT INTO "phases" VALUES(2,1,2,'Fix first','Confirmed defects fixed with failing tests first.',NULL);
CREATE TABLE plan_reviews (
  plan_id   INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  PRIMARY KEY (plan_id, review_id)
);
INSERT INTO "plan_reviews" VALUES(1,1);
CREATE TABLE "plans" (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE
                         CHECK (code GLOB 'P-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'P-*[^0-9]*'),
  title                TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'drafting' CHECK (status IN
                         ('drafting','awaiting-approval','approved','in-progress','complete',
                          'superseded','abandoned'))
                         CHECK (status IN ('drafting','awaiting-approval','superseded','abandoned')
                                OR approved_at IS NOT NULL),
  approved_by_actor_id INTEGER REFERENCES actors(id),
  approved_at          TEXT,
  superseded_by        INTEGER REFERENCES plans(id),
  doc_url              TEXT,
  page_url             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id      INTEGER REFERENCES actors(id),
  change_note          TEXT
, narrative_md TEXT);
INSERT INTO "plans" VALUES(1,'P-0001','Fix plan for R-0001','in-progress',1,'2026-11-01T09:17:00.000Z',NULL,NULL,NULL,'2026-11-01T09:18:00.000Z','2026-11-11T05:25:14.078Z',NULL,NULL,NULL);
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
CREATE TABLE "repositories" (
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
INSERT INTO "repositories" VALUES(1,'demo','.',NULL,'main','https://github.com/example/demo/blob/{sha}/{path}#L{start}-L{end}',NULL,'2026-11-01T09:00:00.000Z',0);
CREATE TABLE "requirements" (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE
                    CHECK (code GLOB 'REQ-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'REQ-*[^0-9]*'),
  text            TEXT NOT NULL,
  source          TEXT NOT NULL,
  source_location TEXT,
  stated_at       TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','withdrawn')),
  superseded_by   INTEGER REFERENCES requirements(id)
                    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
  first_review_id INTEGER REFERENCES reviews(id),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "requirements" VALUES(1,'REQ-0001','Requests need 14 days of notice.','owner, chat 2026-11-01',NULL,NULL,'active',NULL,1,'2026-11-01T09:06:30.000Z');
CREATE TABLE "review_actors" (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  actor_id   INTEGER NOT NULL REFERENCES actors(id),
  role_label TEXT NOT NULL,                -- what it did here: 'indexer for security.md', 'matcher'
  PRIMARY KEY (review_id, actor_id)
);
INSERT INTO "review_actors" VALUES(1,3,'security reviewer');
CREATE TABLE "review_domains" (
  review_id         INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code       TEXT NOT NULL REFERENCES domains(code),
  decision          TEXT NOT NULL CHECK (decision IN ('included','skipped')),
  reason            TEXT NOT NULL,
  owner_change      TEXT NOT NULL DEFAULT 'none' CHECK (owner_change IN
                      ('none','added-by-owner','removed-by-owner','modified-by-owner')),
  reviewers_planned INTEGER,
  split_note        TEXT, proposed_by_rule TEXT,                 -- how the domain was split between reviewers
  PRIMARY KEY (review_id, domain_code)
);
INSERT INTO "review_domains" VALUES(1,'security','included','Sign-in codes and sessions exist','none',1,NULL,NULL);
INSERT INTO "review_domains" VALUES(1,'spec','included','Owner rules for time off are stated','none',1,NULL,NULL);
INSERT INTO "review_domains" VALUES(1,'database','skipped','No database in this project','none',0,NULL,NULL);
INSERT INTO "review_domains" VALUES(2,'security','included','Sign-in codes and sessions exist','none',1,NULL,NULL);
INSERT INTO "review_domains" VALUES(2,'spec','included','Owner rules for time off are stated','none',1,NULL,NULL);
INSERT INTO "review_domains" VALUES(2,'database','skipped','No database in this project','none',0,NULL,NULL);
CREATE TABLE review_environment (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  capability    TEXT NOT NULL CHECK (length(trim(capability)) > 0),   -- git, playwright, connector:figma ...
  available     INTEGER NOT NULL CHECK (available IN (0,1)),
  detail        TEXT,                    -- a version, a name, or where a connector is configured
  how_to_enable TEXT NOT NULL,           -- what to install or do to have it
  position      INTEGER NOT NULL,        -- the order `env` lists them in
  PRIMARY KEY (review_id, capability)
);
INSERT INTO "review_environment" VALUES(1,'git',1,'2.43','install git',1);
CREATE TABLE "review_goals" (
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  goal_id   INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (review_id, goal_id)
);
INSERT INTO "review_goals" VALUES(1,1);
INSERT INTO "review_goals" VALUES(1,2);
INSERT INTO "review_goals" VALUES(2,1);
INSERT INTO "review_goals" VALUES(2,2);
CREATE TABLE "review_issue_checks" (
  review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id),
  outcome    TEXT NOT NULL CHECK (outcome IN
               ('still-present','changed','fixed','regressed','obsolete','not-checked')),
  finding_id INTEGER REFERENCES findings(id),
  evidence   TEXT,
  actor_id   INTEGER REFERENCES actors(id),
  checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), change_actor_id INTEGER REFERENCES actors(id), change_note TEXT,
  PRIMARY KEY (review_id, issue_id)
);
INSERT INTO "review_issue_checks" VALUES(2,1,'fixed',NULL,'make_code now uses secrets.randbelow (src/auth.py:3)',3,'2026-11-01T09:29:00.000Z',NULL,NULL);
INSERT INTO "review_issue_checks" VALUES(2,2,'still-present',4,NULL,4,'2026-11-01T09:31:00.000Z',NULL,NULL);
INSERT INTO "review_issue_checks" VALUES(2,3,'not-checked',NULL,NULL,4,'2026-11-01T09:35:00.000Z',NULL,NULL);
CREATE TABLE "review_repositories" (
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
INSERT INTO "review_repositories" VALUES(1,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','main',NULL,'Initial time off and sign-in','review/R-0001',0,0,NULL,NULL);
INSERT INTO "review_repositories" VALUES(2,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','main',NULL,'Use secrets for sign-in codes','review/R-0002',0,0,NULL,NULL);
CREATE TABLE "review_requirement_checks" (
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
INSERT INTO "review_requirement_checks" VALUES(1,1,1,'differs','src/requests.py','no',2,'can_request hardcodes 14 days');
CREATE TABLE "reviewer_runs" (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  domain_code    TEXT NOT NULL REFERENCES domains(code),
  actor_id       INTEGER NOT NULL REFERENCES actors(id),
  label          TEXT NOT NULL,           -- 'tests-engine-A'
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
INSERT INTO "reviewer_runs" VALUES(1,1,'security',3,'security','sign-in',NULL,NULL,NULL,'finished',4,1,0,0,NULL,NULL,'2026-11-01T09:09:00.000Z',NULL);
INSERT INTO "reviewer_runs" VALUES(2,1,'spec',4,'spec','time-off rules',NULL,NULL,NULL,'finished',6,0,1,1,NULL,NULL,'2026-11-01T09:10:00.000Z',NULL);
INSERT INTO "reviewer_runs" VALUES(3,2,'security',3,'security','sign-in',NULL,NULL,NULL,'finished',4,1,0,0,NULL,NULL,'2026-11-01T09:27:00.000Z',NULL);
INSERT INTO "reviewer_runs" VALUES(4,2,'spec',4,'spec','time-off rules',NULL,NULL,NULL,'finished',6,0,1,1,NULL,NULL,'2026-11-01T09:28:00.000Z',NULL);
CREATE TABLE "reviews" (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE
                          CHECK (code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'R-*[^0-9]*'),
  title                 TEXT NOT NULL,
  scope                 TEXT NOT NULL CHECK (scope IN
                          ('full','partial','targeted','second','fix-verification')),
  status                TEXT NOT NULL DEFAULT 'planning' CHECK (status IN
                          ('planning','awaiting-approval','approved','running','verifying',
                           'indexing','consolidating','handed-off','abandoned'))
                          CHECK (status IN ('planning','awaiting-approval','abandoned') OR approved_at IS NOT NULL),
  request_text          TEXT,
  requested_by_actor_id INTEGER REFERENCES actors(id),
  manager_actor_id      INTEGER REFERENCES actors(id),
  approved_by_actor_id  INTEGER REFERENCES actors(id),
  approved_at           TEXT,
  previous_review_id    INTEGER REFERENCES reviews(id),
  skill_version         TEXT,
  review_folder         TEXT,
  summary_doc_url       TEXT CHECK (summary_doc_url IS NULL OR summary_doc_url GLOB 'https://*'
                                    OR summary_doc_url GLOB 'http://*'),
  verdict               TEXT,
  summary               TEXT,
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at          TEXT CHECK (completed_at IS NULL OR julianday(started_at) <= julianday(completed_at)),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id       INTEGER REFERENCES actors(id),
  change_note           TEXT
, report_md TEXT);
INSERT INTO "reviews" VALUES(1,'R-0001','Comprehensive review R-0001','full','handed-off','Run the full system review',1,2,1,'2026-11-01T09:07:00.000Z',NULL,NULL,NULL,NULL,'Solid core; sign-in codes and notice rules need work.',NULL,'2026-11-01T09:08:00.000Z','2026-11-11T05:25:14.075Z','2026-11-11T05:25:14.075Z',NULL,NULL,NULL);
INSERT INTO "reviews" VALUES(2,'R-0002','Comprehensive review R-0002','second','awaiting-approval','Run the full system review',1,2,1,'2026-11-01T09:25:00.000Z',1,NULL,NULL,NULL,'Solid core; sign-in codes and notice rules need work.',NULL,'2026-11-01T09:26:00.000Z',NULL,'2026-11-11T05:25:14.079Z',NULL,NULL,NULL);
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  description TEXT NOT NULL,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO "schema_migrations" VALUES(1,'initial ledger schema','2026-11-11T05:25:14.017Z');
INSERT INTO "schema_migrations" VALUES(2,'url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted','2026-11-02T02:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(3,'integrity rules: codes, hashes, checks, append-only history, owner actor on gate answers','2026-11-02T03:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(4,'check registry, requirement trace, review agents, metric catalog, domain descriptions, foreign-key indexes','2026-11-02T04:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(5,'one UTC time format; whole-file and not-text references; report, plan narrative and fix-time discoveries','2026-11-02T05:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(6,'lookup tables for the ledger''s words and labels; RESTRICT keys under reviews and findings; metrics.name catalogued','2026-11-02T06:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(7,'must-be-empty checks for history gaps, matches, off-plan and done-step coverage, closings, relations and checks; steps.no_findings_reason','2026-11-02T07:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(8,'finding_locations.source (the reviewed text of a dirty working tree); v_next_codes for the write commands','2026-11-02T08:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(9,'owner interview (interview_questions, interview_answers) and per-domain design goals the owner approves','2026-11-02T09:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(10,'v_plan_totals, v_review_totals and v_pending_gates: the counts the viewer''s lists show','2026-11-02T10:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(11,'gate answer rules in the trigger, owner queue context, event labels, the artifact check','2026-11-02T11:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(12,'review_environment: what each review''s environment offered (tools, the harness, its connectors)','2026-11-02T12:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(13,'domain_rules: the rule by which scan proposes each domain; review_domains.proposed_by_rule','2026-11-02T13:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(14,'domain_checks and checklist_answers: each domain''s checklist, and the answers each report gave','2026-11-02T14:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(15,'the agreement metrics compare-runs records: domain_agreement, finding_agreement, severity_agreement','2026-11-02T15:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(16,'step_touches and step_conflicts: what each plan step touches, and the pairs of steps that touch the same thing','2026-11-02T16:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(17,'a step may depend on any step of its plan, cycles refused; v_step_order: the order to work in','2026-11-02T17:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(18,'multi-select gates: gates.multi and gates.preselect; a multi-select answer is a JSON list of option values','2026-11-02T18:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(19,'the features domain and feature_proposals: additions to the reviewed codebase that would serve the owner''s goals','2026-11-02T19:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(20,'tool_runs and tool_results: the picked tools'' runs at the reviewed commit, and their results as leads','2026-11-02T20:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(21,'indexes for check''s views, which read them instead of every row; the answers-imported event','2026-11-02T21:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(22,'correction commands: who corrects a gate, a location or a check, and why','2026-11-02T22:00:00.000Z');
INSERT INTO "schema_migrations" VALUES(23,'measured guard tests: guard_tests.measured_at and measured_commit_sha; v_unproven_steps; the guard-dropped and guard-renamed events','2026-11-02T23:00:00.000Z');
CREATE TABLE severities (
  value    TEXT PRIMARY KEY,                 -- H, M, L, T
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE           -- most severe first
);
INSERT INTO "severities" VALUES('H','High',1);
INSERT INTO "severities" VALUES('M','Medium',2);
INSERT INTO "severities" VALUES('L','Low',3);
INSERT INTO "severities" VALUES('T','Missing test',4);
CREATE TABLE "step_commits" (
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
INSERT INTO "step_commits" VALUES(1,2,1,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee','main',NULL,'Use secrets for sign-in codes',NULL);
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
INSERT INTO "step_conflicts" VALUES(1,2,3,'file','src/requests.py','both edit the file',0,NULL,'no-conflict','different functions',NULL,2,'2026-11-01T09:18:30.000Z');
CREATE TABLE step_dependencies (
  step_id            INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  depends_on_step_id INTEGER NOT NULL REFERENCES steps(id),
  PRIMARY KEY (step_id, depends_on_step_id),
  CHECK (step_id <> depends_on_step_id)
);
INSERT INTO "step_dependencies" VALUES(2,1);
INSERT INTO "step_dependencies" VALUES(3,1);
CREATE TABLE step_findings (
  step_id      INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  finding_id   INTEGER NOT NULL REFERENCES findings(id),
  multi_reason TEXT,          -- required when a finding is closed by more than one step
  PRIMARY KEY (step_id, finding_id)
);
INSERT INTO "step_findings" VALUES(2,1,NULL);
INSERT INTO "step_findings" VALUES(3,2,NULL);
INSERT INTO "step_findings" VALUES(1,3,NULL);
CREATE TABLE step_goals (
  step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  goal_id INTEGER NOT NULL REFERENCES design_goals(id),
  PRIMARY KEY (step_id, goal_id)
);
INSERT INTO "step_goals" VALUES(3,1);
CREATE TABLE step_touches (
  step_id   INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('file','symbol','table','column','setting','route','behaviour')),
  target    TEXT NOT NULL CHECK (length(trim(target)) > 0),   -- demo:src/app.py, findings.severity, GET /api/x
  direction TEXT NOT NULL DEFAULT 'changes' CHECK (direction IN
              ('adds','removes','tightens','loosens','renames','changes')),
  source    TEXT NOT NULL CHECK (source IN ('declared','finding')),   -- steps.jsonl, or a closed finding's location
  PRIMARY KEY (step_id, kind, target, direction, source)
);
INSERT INTO "step_touches" VALUES(2,'file','demo:src/auth.py','changes','finding');
INSERT INTO "step_touches" VALUES(3,'file','demo:src/timeoff.py','changes','finding');
INSERT INTO "step_touches" VALUES(1,'file','demo:src/timeoff.py','changes','finding');
INSERT INTO "step_touches" VALUES(2,'symbol','demo:src/auth.py#make_code','changes','finding');
INSERT INTO "step_touches" VALUES(3,'symbol','demo:src/timeoff.py#can_request','changes','finding');
INSERT INTO "step_touches" VALUES(1,'symbol','demo:src/timeoff.py#can_request','changes','finding');
CREATE TABLE "steps" (
  id                INTEGER PRIMARY KEY,
  plan_id           INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  phase_id          INTEGER NOT NULL REFERENCES phases(id),
  number            INTEGER NOT NULL,
  section           TEXT,
  title             TEXT NOT NULL,
  change            TEXT NOT NULL,
  proof             TEXT NOT NULL,
  owner_need        TEXT NOT NULL DEFAULT 'none' CHECK (owner_need IN
                      ('none','decision','confirm','review','production')),
  owner_question    TEXT,
  behavior_change   INTEGER NOT NULL DEFAULT 0 CHECK (behavior_change IN (0,1)),
  behavior_note     TEXT,
  status            TEXT NOT NULL DEFAULT 'not-started' CHECK (status IN
                      ('not-started','doing','waiting-owner','done','dropped')),
  progress_note     TEXT,
  drafter_key       TEXT,
  started_at        TEXT,
  done_at           TEXT,
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_actor_id   INTEGER REFERENCES actors(id),
  change_commit_sha TEXT CHECK (change_commit_sha IS NULL OR ((length(change_commit_sha) = 40 OR length(change_commit_sha) = 64) AND change_commit_sha NOT GLOB '*[^0-9a-f]*')),
  change_note       TEXT, no_findings_reason TEXT,
  UNIQUE (plan_id, number)
);
INSERT INTO "steps" VALUES(1,1,1,1,'CI','Run every suite in CI','Add a CI workflow.','CI fails on a broken test.','none',NULL,0,NULL,'not-started',NULL,NULL,'2026-11-01T09:19:00.000Z',NULL,'2026-11-01T09:19:00.000Z',NULL,NULL,NULL,NULL);
INSERT INTO "steps" VALUES(2,1,2,2,NULL,'Use secrets for sign-in codes','Replace random with secrets in make_code.','test_code_uses_secrets fails before, passes after.','none',NULL,0,NULL,'done','Sign-in codes now come from a secure generator.',NULL,'2026-11-01T09:20:00.000Z','2026-11-11T05:25:14.078Z','2026-11-11T05:25:14.078Z',2,'4de736a28a4bc6e37b58c1b1014cb6f7c68f71ee',NULL,NULL);
INSERT INTO "steps" VALUES(3,1,2,3,NULL,'Make the notice period a setting','Add notice_days setting.','Tests with 7 and 21 days.','decision','Should the default stay 14 days?',1,'Programs can set their own notice.','waiting-owner',NULL,NULL,'2026-11-01T09:21:00.000Z',NULL,'2026-11-11T05:25:14.078Z',NULL,NULL,NULL,NULL);
CREATE TABLE "test_runs" (
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
INSERT INTO "test_runs" VALUES(1,1,NULL,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','python3 -m unittest','baseline',12,0,0,0,0,900,NULL,NULL,'2026-11-01T09:08:30.000Z');
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
CREATE TABLE tool_results (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE     -- LD-0001
                         CHECK (code GLOB 'LD-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'LD-*[^0-9]*'),
  tool_run_id          INTEGER NOT NULL REFERENCES tool_runs(id) ON DELETE RESTRICT,
  domain_code          TEXT REFERENCES domains(code),
  rule                 TEXT NOT NULL CHECK (length(trim(rule)) > 0),
  level                TEXT NOT NULL CHECK (level IN ('error','warning','note','none')),
  path                 TEXT,             -- repository-relative, forward slashes; none for a whole-run result
  line_start           INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end             INTEGER CHECK (line_end IS NULL OR (line_start IS NOT NULL AND line_end >= line_start)),
  message              TEXT NOT NULL CHECK (length(trim(message)) > 0),
  fingerprint          TEXT NOT NULL,    -- tool, rule, path, message and occurrence, hashed: the same lead later
  finding_id           INTEGER REFERENCES findings(id),     -- the finding that cites it (cite-lead)
  dismissed_reason     TEXT,             -- why it is not a finding (dismiss-lead)
  disposed_by_actor_id INTEGER REFERENCES actors(id),
  disposed_at          TEXT,
  carried_from_id      INTEGER REFERENCES tool_results(id),  -- the earlier lead whose dismissal it carries
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (tool_run_id, fingerprint),
  CHECK (dismissed_reason IS NULL OR length(trim(dismissed_reason)) > 0),
  CHECK ((finding_id IS NULL AND dismissed_reason IS NULL) = (disposed_at IS NULL))
);
INSERT INTO "tool_results" VALUES(1,'LD-0001',1,'security','S311','warning','src/auth.py',1,1,'random is not for secrets','ruff:S311:src/auth.py:1',NULL,'SEC-H1 says the same',2,'2026-11-01T09:09:00.000Z',NULL,'2026-11-01T09:08:50.000Z');
CREATE TABLE tool_runs (
  id             INTEGER PRIMARY KEY,
  review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  repository_id  INTEGER REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                                   AND commit_sha NOT GLOB '*[^0-9a-f]*')),
  tool           TEXT NOT NULL CHECK (length(trim(tool)) > 0),    -- the adapter's name (references/tools.md)
  domain_code    TEXT REFERENCES domains(code),                   -- the domain whose reviewer gets its leads
  status         TEXT NOT NULL CHECK (status IN ('ran','imported','absent','skipped','failed')),
  version        TEXT,                    -- the version the tool printed
  pinned_version TEXT,                    -- the version references/tools.lock pins
  command        TEXT,                    -- what ran, with {checkout} and {out} for the temporary paths
  exit_code      INTEGER,
  duration_ms    INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  output_format  TEXT CHECK (output_format IS NULL OR output_format IN
                   ('sarif','junit','lcov','cobertura','json','jsonl')),
  output_path    TEXT,                    -- the output as imported, in the review's folder
  output_sha256  TEXT CHECK (output_sha256 IS NULL OR (length(output_sha256) = 64
                                                       AND output_sha256 NOT GLOB '*[^0-9a-f]*')),
  rules_sha256   TEXT,                    -- the rule set's hash, for a tool run with rules
  online         INTEGER NOT NULL DEFAULT 0 CHECK (online IN (0,1)),   -- 1 = the run was allowed to go online
  fallback       TEXT,                    -- what the reviewer does instead, when the tool did not run
  note           TEXT,
  actor_id       INTEGER REFERENCES actors(id),
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (status NOT IN ('absent','skipped','failed') OR length(trim(COALESCE(fallback, ''))) > 0),
  CHECK (status <> 'ran' OR (command IS NOT NULL AND exit_code IS NOT NULL))
);
INSERT INTO "tool_runs" VALUES(1,1,1,'16822abc0b87e79b664c98f386b3354147e0f1bc','ruff','security','imported','0.6.0',NULL,NULL,NULL,NULL,'sarif',NULL,NULL,NULL,0,NULL,NULL,2,'2026-11-01T09:08:50.000Z');
CREATE INDEX ix_deps_on              ON step_dependencies(depends_on_step_id);
CREATE INDEX ix_steps_phase          ON steps(phase_id);
CREATE INDEX ix_steps_status         ON steps(plan_id, status);
CREATE INDEX ix_guard_issue          ON guard_tests(issue_id);
CREATE UNIQUE INDEX ux_repositories_path      ON repositories(path);
CREATE INDEX ix_fk_reviews_change_actor_id    ON reviews(change_actor_id);
CREATE INDEX ix_fk_plans_change_actor_id      ON plans(change_actor_id);
CREATE INDEX ix_fk_actors_parent_actor_id            ON actors(parent_actor_id);
CREATE INDEX ix_fk_reviews_approved_by_actor_id      ON reviews(approved_by_actor_id);
CREATE INDEX ix_fk_reviews_manager_actor_id          ON reviews(manager_actor_id);
CREATE INDEX ix_fk_reviews_previous_review_id        ON reviews(previous_review_id);
CREATE INDEX ix_fk_reviews_requested_by_actor_id     ON reviews(requested_by_actor_id);
CREATE INDEX ix_fk_requirements_first_review_id      ON requirements(first_review_id);
CREATE INDEX ix_fk_requirements_superseded_by        ON requirements(superseded_by);
CREATE INDEX ix_fk_decisions_asked_by_actor_id       ON decisions(asked_by_actor_id);
CREATE INDEX ix_fk_decisions_decided_by_actor_id     ON decisions(decided_by_actor_id);
CREATE INDEX ix_fk_decisions_review_id               ON decisions(review_id);
CREATE INDEX ix_fk_decisions_step_id                 ON decisions(step_id);
CREATE INDEX ix_fk_decision_issues_issue_id          ON decision_issues(issue_id);
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
CREATE INDEX ix_fk_artifacts_plan_id                 ON artifacts(plan_id);
CREATE INDEX ix_fk_artifacts_review_id               ON artifacts(review_id);
CREATE INDEX ix_fk_artifacts_reviewer_run_id         ON artifacts(reviewer_run_id);
CREATE INDEX ix_findings_review      ON findings(review_id);
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
CREATE INDEX ix_metrics_name         ON metrics(name, scope);
CREATE INDEX ix_runs_review          ON reviewer_runs(review_id);
CREATE INDEX ix_gates_status         ON gates(status);
CREATE UNIQUE INDEX ux_metrics_key            ON metrics(review_id, name, scope, COALESCE(repository_id, 0));
CREATE UNIQUE INDEX ux_locations_one_primary  ON finding_locations(finding_id) WHERE role = 'primary';
CREATE INDEX ix_fk_gates_change_actor_id      ON gates(change_actor_id);
CREATE INDEX ix_fk_gates_answered_by_actor_id ON gates(answered_by_actor_id);
CREATE INDEX ix_fk_review_repositories_repository_id ON review_repositories(repository_id);
CREATE INDEX ix_fk_review_goals_goal_id              ON review_goals(goal_id);
CREATE INDEX ix_fk_review_domains_domain_code        ON review_domains(domain_code);
CREATE INDEX ix_fk_reviewer_runs_actor_id            ON reviewer_runs(actor_id);
CREATE INDEX ix_fk_reviewer_runs_domain_code         ON reviewer_runs(domain_code);
CREATE INDEX ix_fk_review_actors_actor_id            ON review_actors(actor_id);
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
CREATE INDEX ix_gates_decision                       ON gates(decision_id);
CREATE INDEX ix_fk_gates_issue_id                    ON gates(issue_id);
CREATE INDEX ix_fk_gates_opened_by_actor_id          ON gates(opened_by_actor_id);
CREATE INDEX ix_fk_gates_plan_id                     ON gates(plan_id);
CREATE INDEX ix_fk_gates_review_id                   ON gates(review_id);
CREATE INDEX ix_gates_step                           ON gates(step_id);
CREATE INDEX ix_fk_metrics_domain_code               ON metrics(domain_code);
CREATE INDEX ix_fk_metrics_repository_id             ON metrics(repository_id);
CREATE INDEX ix_fk_issues_severity                   ON issues(severity);
CREATE INDEX ix_fk_issue_relations_relation          ON issue_relations(relation);
CREATE INDEX ix_fk_findings_severity                 ON findings(severity);
CREATE INDEX ix_fk_findings_match_type               ON findings(match_type);
CREATE INDEX ix_fk_gates_kind                        ON gates(kind);
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
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);
CREATE INDEX ix_fk_events_actor_id                   ON events(actor_id);
CREATE INDEX ix_fk_events_review_id                  ON events(review_id);
CREATE INDEX ix_fk_events_step_id                    ON events(step_id);
CREATE INDEX ix_fk_domain_checks_domain_code ON domain_checks(domain_code);
CREATE INDEX ix_fk_domain_checks_severity ON domain_checks(severity);
CREATE INDEX ix_fk_checklist_answers_check_code ON checklist_answers(check_code);
CREATE INDEX ix_step_touches_target ON step_touches(kind, target);
CREATE INDEX ix_fk_step_conflicts_step_b_id           ON step_conflicts(step_b_id);
CREATE INDEX ix_fk_step_conflicts_late_step_id        ON step_conflicts(late_step_id);
CREATE INDEX ix_fk_step_conflicts_gate_id             ON step_conflicts(gate_id);
CREATE INDEX ix_fk_step_conflicts_resolved_by_actor_id ON step_conflicts(resolved_by_actor_id);
CREATE INDEX ix_fk_feature_proposals_review_id            ON feature_proposals(review_id);
CREATE INDEX ix_fk_feature_proposals_goal_id              ON feature_proposals(goal_id);
CREATE INDEX ix_fk_feature_proposals_requirement_id       ON feature_proposals(requirement_id);
CREATE INDEX ix_fk_feature_proposals_decided_by_actor_id  ON feature_proposals(decided_by_actor_id);
CREATE INDEX ix_fk_feature_proposals_gate_id              ON feature_proposals(gate_id);
CREATE INDEX ix_fk_feature_proposals_step_id              ON feature_proposals(step_id);
CREATE INDEX ix_fk_feature_proposals_proposed_by_actor_id ON feature_proposals(proposed_by_actor_id);
CREATE INDEX ix_fk_feature_status_changes_proposal_id ON feature_status_changes(proposal_id);
CREATE INDEX ix_fk_feature_status_changes_actor_id    ON feature_status_changes(actor_id);
CREATE UNIQUE INDEX ux_gates_one_feature_pick ON gates(review_id) WHERE kind = 'feature-pick' AND status <> 'withdrawn';
CREATE INDEX ix_fk_tool_runs_review_id     ON tool_runs(review_id);
CREATE INDEX ix_fk_tool_runs_repository_id ON tool_runs(repository_id);
CREATE INDEX ix_fk_tool_runs_domain_code   ON tool_runs(domain_code);
CREATE INDEX ix_fk_tool_runs_actor_id      ON tool_runs(actor_id);
CREATE INDEX ix_fk_tool_results_tool_run_id          ON tool_results(tool_run_id);
CREATE INDEX ix_fk_tool_results_domain_code          ON tool_results(domain_code);
CREATE INDEX ix_fk_tool_results_finding_id           ON tool_results(finding_id);
CREATE INDEX ix_fk_tool_results_disposed_by_actor_id ON tool_results(disposed_by_actor_id);
CREATE INDEX ix_fk_tool_results_carried_from_id      ON tool_results(carried_from_id);
CREATE INDEX ix_tool_results_fingerprint             ON tool_results(fingerprint);
CREATE INDEX ix_findings_review_counts ON findings(review_id, verification, severity, match_type, issue_id);
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
CREATE INDEX ix_fk_review_issue_checks_change_actor_id ON review_issue_checks(change_actor_id);
CREATE INDEX ix_fk_finding_locations_change_actor_id ON finding_locations(change_actor_id);
COMMIT;
