# Domain catalog

Read this in Step 2a with the proposal `ledger.py scan` prints; each reviewer gets its domain's entry in the brief
(Step 2d). An entry gives the domain, its report file and, in italics, when it applies (`domains.description` in the
ledger; codes: `ledger.py query "SELECT code, report_file FROM domains"`). "Scan rules" below is how `scan` decides
that from the files. Launch only what the owner approved. Add a domain the catalog lacks with `ledger.py add-domain`.

#### Correctness

**Spec conformance (requirements tracing), `spec.md`.** *Applies whenever there are stated requirements anywhere.*
List EVERY stated requirement, rule and decision from the sources collected in Step 1 (quote the source and location).
For each, one row: where it is implemented (`path:line`, every copy); whether the implementation matches the
requirement exactly (probe edge cases: the boundaries, priority orders, exceptions, "unless" clauses); whether a test
asserts it; verdict (matches / differs / partly / missing / untested). Also list behaviors in the code that no
requirement asks for (possible leftovers or undocumented decisions), requirements that contradict each other, and
requirements changed recently whose older version still survives somewhere in code, tests, docs or copy.

**Test coverage (one or more reviewers, split by module), `tests-<module>.md`.** *Almost always applies.* For EVERY
unit of behavior or configuration (each public function, route, command, setting, rule, feature flag), one row: what it
does and where the code reads it (flag things declared but never used); its options and valid range (type vs schema vs
validation mismatches); how it interacts with other units; the cases needed for thorough coverage with concrete
expected outcomes; the existing tests and whether each case is asserted (Yes / Partly / No); verdict and the
missing tests by name. Also list: inputs read with no validation, validation branches with no test,
migrations/conversions with no test, and error paths with no test. End with totals (units, cases needed, asserted,
missing).

**Test suite quality, `test-quality.md`.** *Applies when there is a meaningful test suite.* Measure, don't judge:
run mutation testing on the core modules (Stryker for JS/TS, mutmut or cosmic-ray for Python, cargo-mutants for Rust,
PIT for Java; install outside the repo or in a temp copy) and report the mutation score per module with the surviving
mutants that matter most. Run the suite several times (and in random order where the runner allows) to find flaky and
order-dependent tests. Find tests that depend on the real clock, time zone, network, or shared state. Report test
suite speed and the slowest tests. Review test doubles: do fakes enforce the real limits and errors of what they
replace? Check that every guard test recorded in the ledger (listed in the brief) still exists and, where cheap to
try, still fails without its fix; the manager records the answer (`ledger.py confirm-guard`). If mutation tooling
can't run, say so and sample assertions by hand, marked "not measured".

**Time, dates and concurrency, `time-concurrency.md`.** *Applies to anything with dates, schedules, deadlines,
expiries, or more than one writer.* Inventory every place the code creates, parses, compares, formats or stores a date
or time. For each: time zone handling (stored in UTC? converted where? whose zone is "today"?), daylight-saving
transitions (missing and repeated hours, 23- and 25-hour days), month, year and leap-year boundaries, week start day,
inclusive vs exclusive end dates, academic/fiscal year rollovers, and dependence on the server clock. Then every place
two actors can change the same thing: what happens if both save at once (lost update, duplicate, half-applied),
which guard prevents it (version check, unique constraint, transaction, lock), and whether a test exercises it. Name the
concrete dates and interleavings to test.

**Failure handling and resilience, `resilience.md`.** *Applies whenever there are external calls, jobs, or multi-step
writes.* For every call to another service, queue, database or file system: timeout, retry policy, whether retries are
safe to repeat (idempotency keys; no double emails, double bookings, double charges), what happens on slow / down /
malformed / partial responses, and what the user sees. For every multi-step change: can it stop halfway, and does it
leave consistent data (transactions, compensating steps, outbox)? For every scheduled job and consumer: what happens on
failure, on a rerun, on overlap with itself, and on a missed run. Follow at least five failures end to end from cause to
what the user and the operator see. Missing tests by name.

**Backward compatibility and migrations, `compatibility.md`.** *Applies whenever there is stored data, saved settings,
public APIs, or deployed clients.* Can today's code read everything older code wrote (rows, saved settings, cached files,
cookies, URLs people bookmarked, calendar subscriptions)? Is every schema/settings migration tested from each older
version still in the wild? Can each deploy roll back without losing or corrupting data? Do open browser tabs or old
clients keep working across a deploy? Are API changes versioned or additive? List every breaking change waiting to
happen and the test that would catch it.

#### Architecture

**Decision placement, `decision-placement.md`.** *Applies when the system has business rules.* Inventory EVERY
business decision (permission checks, validation, limits, eligibility, ordering/tie-breaking, pricing, scheduling,
deadlines, state transitions), wherever it lives. For each, one row: where it is decided today (core library / server /
client / database constraint / external service); whether the same decision is written more than once and whether the
copies agree (probe them); whether it belongs in a different layer or a shared mechanism (a rules engine, a solver, a
policy module, a schema constraint) and what moving it would take; what is gained (consistency, one source of truth,
explainability) and what it costs (complexity, performance, determinism); and a verdict: move / keep with reason. Also
list decisions checked in one path but not another (e.g. at request time but not in the batch job), and propose a
migration order with the tests that must pass before and after each move (same inputs, same answers).

**Internal contracts, `contracts.md`.** *Applies when there is more than one component.* For every boundary between
components (service to service, app to library, worker to queue): endpoint/message inventory with request, response,
errors, docs accuracy and tests. Can every setting or capability be reached through the boundary? Does the caller
handle every response the callee can give (each success shape, refusal, error and timeout), not only the common one?
Is every field of every response used by the caller? Does the caller populate every input? Field-by-field drift
between the producer's
contract and the consumer's copy (flag duplicated types). Test coverage per endpoint and response branch, including
timeout, 4xx, 5xx, malformed response and unavailable dependency.

**Backend / server, `server.md`.** *Applies when there is a server.* Part 1: every place the server hardcodes domain or
customer-specific behavior instead of reading a setting or asking the layer that owns it (file:line, whether a setting
exists, the fix). Part 2: an inventory of every API function, route, scheduled job, queue consumer, webhook, sign-in
flow and email/notification path: who may call it, the cases needed (success, each refusal, validation,
stale/concurrent saves, dependency refusing, dependency erroring), which tests assert each, and the missing tests. Flag
functions with no test, permission checks with no negative test, and unvalidated client input.

**Database, `database.md`.** *Applies when there is a database.* Schema inventory. Is everything the system consumes or
produces stored and queryable later (flag anything lost, lossy, or only in logs)? Every distinct query against the
indexes (use the query planner on a copy of the schema): scans, N+1 queries, unbounded results. Integrity and
concurrency (constraints, version bumps, uniqueness, transactions). Operations (migration reversibility, retention,
secrets stored in clear). Concrete index statements and round-trip reductions.

**Backend-to-client API, `client-api.md`.** *Applies when a client talks to a backend.* Capability inventory of
everything the backend can do or knows, against the API function that exposes it and the screen that uses it. Gaps:
capabilities with no API, data computed but not returned, functions no screen calls, screens calling things that don't
exist. Coherence between API styles: duplicated capabilities, inconsistent shapes and errors, different permission
checks on two paths to one action, identifiers that are display names. A target shape and migration order.

**Abstraction and shared code, `abstraction.md`.** *Almost always applies.* Duplicated types and contracts between
packages; duplicated logic across layers; the same concept implemented several times; extensibility walk-throughs (add
a new entity type, a rule, a menu section, a tenant, an approval-requiring action, a notification type, a settings
group: list every file that must change); code health (dead code, oversized modules, circular dependencies, complexity);
a naming glossary; a prioritized refactoring plan (quick wins, medium, structural) ordered to keep the live system
working.

**Hotspots from history, `hotspots.md`.** *Applies when there is git history worth mining (a few months or more).*
From `git log`: files ranked by change frequency x complexity; clusters of bug-fix commits (by message or by revert);
files that always change together but live apart (hidden coupling); code with a single author (knowledge risk); areas
with many changes and few tests. Combine with the ledger's `v_file_hotspots` (files that keep producing issues across
reviews). Run early if possible and share the ranked list with the other reviewers as where to dig deepest.

#### User-facing

**Front end / UI (one to three reviewers, split by area), `ui-<area>.md`.** *Applies when there are screens.* Judge
the rendered pages against the owner's design brief, then read every line of the screens and the backend views
feeding them. Report each finding with file:line, the exact text or code, and the fix.
- **Design brief.** The approved `ui` goals (`ledger.py goals --domain ui`) are the brief: the direction the owner
  gave in the interview and the type scale, palette and density agreed from it. A finding about looks cites the goal
  it breaks. With no approved `ui` goal, say so first and report only what can be measured.
- **Look at the pages.** The manager captures every page at phone (390) and desktop (1280) width, light and dark
  (`ledger.py ui-capture`; files in `.code-review/reviews/R-0001/ui/`). Open each screenshot and judge what is
  there: what is seen first, hierarchy, alignment, spacing rhythm, overflow and cut-off text at phone width, the dark
  theme. Without screenshots, or without a model that can look at an image, write "rendered result not seen" beside
  each finding that depends on it and review from the source (`references/environment.md#rendered-pages`).
- **Measure.** `ledger.py ui-audit` reports texts below the required contrast, images, fields, buttons and links with
  no name, page weight, layout shift, and every colour, font size and spacing value in use with its count. Each
  failure is a finding; so is an inventory longer than the brief's scale (14 font sizes against a 6-step type
  scale), with the values to merge. Without Playwright, compute the same from the stylesheets and mark each number
  "not measured".
- **Redesign proposals.** A change to the look is proposed as three deliberately different options, each a named
  direction with its type scale, palette and density, shown on the same page; name the generic default (what a
  tool produces unprompted) so the owner sees what to avoid, and never offer it as an option; the owner picks (a
  `decision` gate). With a design connector (`design-tool` in `ledger.py env`), build the options there and check
  the code against the owner's designs; without one, write each option as an HTML file in the review's `ui/`
  folder and capture it.
- **Requirements,** each breach a finding: (1) generic, including what a second customer/tenant would see; (2) can
  display and act on every configuration the backend allows (best through settings forms generated from the settings
  schema, so a new setting needs no new screen code); (3) no internal names on screen; (4) select, don't type;
  (5) context-sensitive by tenant and by user role; (6) usability, small-screen width, accessibility (keyboard,
  focus, screen-reader names); (7) duplicated code that should be shared; (8) every piece of text meets
  `references/writing.md`, each breach a finding with the shorter version.
- One reviewer owns the **ACCESS AUDIT**: a matrix of every kind of user (signed out, pending, no roles, each role,
  admin, super admin, service account, user of another tenant) against every link, button and page. List every link
  shown to someone who is then refused, and every page that, opened directly without access, shows anything other
  than the explanation (partial render, endless loading, raw error, blank page, bare status).

**Task walkthroughs, `walkthroughs.md`.** *Applies when users complete multi-step tasks.* List the 10-20 most important
real tasks per kind of user (e.g. submit an expense and get it approved; invite a member; change a setting and see its
effect). Trace each end to end through the code: every page, call, check, email and state change. Report broken or
confusing hand-offs between pages, dead ends, steps that need information the user can't see, missing confirmations
or undo, and tasks the system can't complete at all. Then take each main task as a first-time user of that kind: can
they finish the main task unaided? What is seen first on each page, and is it what the task needs? What on each page
could be deleted without loss? Use the screenshots when the manager captured them (`ledger.py ui-capture`; without
them, trace from the source and say so). Distinct from per-page UI review: it finds problems that only show across
pages.

**Email and notifications, `notifications.md`.** *Applies when the system sends email, SMS, push or calendar feeds.*
Inventory every message type: trigger, recipients and how they're chosen, content source, opt-out, and test. Check
duplicate sends (retries, overlapping jobs), missed sends, wrong recipients after role changes, bounce and complaint
handling, deliverability configuration visible in the repo or DNS docs (SPF, DKIM, DMARC; say "not verified" if not
visible), staging guards against emailing real people, rendering on phones, and plain-text alternatives.

**Localization, `localization.md`.** *Applies only if other languages, regions or conventions are plausible.*
Hardcoded user-facing strings, date/number/currency formats, week start day, name and address assumptions, text that
won't fit when translated, right-to-left readiness.

#### Security, privacy and operations

**Security, `security.md`.** *Almost always applies.* Defensive review of code and configuration only. Report each
weakness with file:line, how it could be misused in plain terms, severity, and the fix and test.
- Sign-in and sessions: each method, code/token handling (hashed? expiry, attempt and rate limits), session length and
  renewal, cookie flags and domain, step-up auth for sensitive actions, sign-out everywhere, account switching and
  email changes, invitations and sign-ups.
- Authorization: for EVERY API function and route, the permission check, and whether a lower-privileged user, a user of
  another tenant/year, or a signed-out caller could reach it or reach another person's data by changing an id or name.
  Compare API layers for the same action. What each role can read.
- Input handling: every place client input reaches SQL (parameter binding), HTML (escaping; `innerHTML` and similar),
  emails (header and HTML injection), feeds, file names and paths, redirects (open redirects), deserialization/JSON
  parsing, SSRF, and admin-saved settings (can a value break or script a page?).
- Browser protections: Content-Security-Policy per page, CSRF/same-site checks on state-changing requests, CORS,
  framing, referrer policy, caching of private responses, third-party scripts.
- Secrets: in the repo, workflows or logs; what is stored in clear; secret URLs as passwords (entropy, reset, leakage).
- Multi-tenant/template risks: what a tenant's config or an admin's input can inject; whether one deployment's or
  tenant's data could reach another's.
- Operations: deploy workflow permissions and token scope, who can deploy to production, staging safeguards, error
  messages that reveal internals.
- Threat model: a short list of who might misuse the system (outsider, ordinary user, admin, compromised account) and
  the worst thing each could do today.
- Tests: which protections have a test that would fail without the protection, and the missing ones by name.

**Privacy and data map, `privacy.md`.** *Applies whenever personal data is handled.* Every kind of personal data: where
it enters, every place it is stored or copied (database, backups, logs, emails, feeds, analytics, third parties), who
can read it, how long it is kept, and how one person's data would be fully exported or deleted. Free-text fields where
regulated data (patient, financial, children's) could slip in. Applicable obligations (HIPAA, GDPR, FERPA, etc.) and
the gaps against them, marked "not legal advice".

**Logging, `logging.md`.** *Almost always applies.* Inventory every log statement and logging configuration. Report:
- Coverage: is every important event logged (sign-ins and failures, permission refusals, every state-changing
  action, every external call and its outcome, job start/finish/failure, email sent/failed, config changes)? List the
  events that leave no trace.
- Diagnosability: could an operator answer five realistic complaints from logs alone ("I never got the email", "my
  request vanished", "the page says error", "someone changed my setting", "the nightly job didn't run")? Walk each and
  say where the trail breaks.
- Quality: consistent structure (structured/JSON vs free text), levels used correctly, a correlation/request id
  carried across components, timestamps with zone, no noisy logs that hide real ones, no swallowed exceptions
  (`catch` with no log or rethrow).
- Safety: no secrets, tokens, codes, full emails or personal/regulated data in logs (cite each); log injection
  (user input with newlines or control characters).
- Retention and access: where logs go, how long they are kept, who can read them, cost at volume, and whether audit
  trails (who did what) are separate from debug logs and tamper-resistant.
- Observability around the logs: errors reported somewhere a person will see them (alerts, error tracker), health
  checks, metrics for the slowest and most failure-prone paths.
- Tests: which log/audit entries are asserted by tests.

**Operations, CI/CD and recovery, `operations.md`.** *Applies when the system is deployed.* CI: does it run every suite,
linter and type-check on every change, and block merges on failure? Release: reproducible builds, staging that matches
production, deploy and rollback steps, feature flags. Recovery, on paper: if the database, hosting account, domain or
email provider were lost today, what is the path back, how long would it take, and has a restore ever been tested?
Backups: what, how often, where, encrypted, retention. Scheduled jobs: monitoring and missed-run detection.

**Dependencies and supply chain, `dependencies.md`.** *Almost always applies.* From the lock files: outdated and
abandoned packages, versions with known problems (say "not checked online" if no network), license problems, unpinned
versions, CI actions referenced by tag instead of commit hash, packages with install scripts, and dependencies used for
trivial things that could be dropped.

**Performance, `performance.md`.** *Applies when speed or scale matters to users or cost.* Where time goes for a user
and for background jobs, with measurements where they can be taken locally and reasoning from code where they can't
(say which).
- Per page load and per action: server calls made; per call the DB queries (sequential or parallel), calls to other
  services, repeated or recomputed work, loops that call out per item.
- Heavy components (compute engines, solvers, report generators): time and memory per job type at realistic and larger
  sizes; what scales worse than linearly; timeouts and what happens when hit; cold starts; payload sizes against
  platform limits.
- Platform limits the code could hit (CPU time, subrequests, batch size, payload size, queue/cron behavior), each marked
  "not verified" unless confirmed in code or docs in the repo.
- Front end: page weight, render-blocking resources, caching, work on every DOM change, repeated full re-renders,
  slow-phone behavior.
- Perceived speed: what the user sees while waiting; could stale data show first; could a result show before a
  follow-up call finishes.
- Scale test: what breaks first at 10x users and 3x data; the three changes that buy the most.
- Every recommendation states expected saving (measured or estimated) and risk. List existing performance tests/budgets
  and propose new ones (e.g. a test that a list view makes at most N queries). Report measured numbers under stable
  metric names so the ledger can trend them.

**Cost, `cost.md`.** *Applies when the system runs on paid infrastructure or services.* What each service costs at
current and 10x scale (from config and documented pricing in the repo; "not verified" otherwise), which account and
payment method each is billed to, and anything that could run up a bill by accident (runaway retries, unbounded logs or
storage, no spending limits).

#### Documentation and handover

**Developer documentation (inline and repo), `docs-developer.md`.** *Almost always applies.*
- Inline: every public/exported function, type, module and non-obvious block: is there a comment or docstring saying
  what it does, its inputs/outputs, units, invariants and why, not only what? Flag comments that are wrong, stale
  (describe old behavior, reference removed code), contradict the code, or restate the obvious. Flag TODO/FIXME/HACK
  notes with no owner or issue. Magic numbers with no explanation.
- Repo docs: README, setup, architecture overview, data model, API reference, settings reference, contribution guide,
  ADRs/decision log. For each: does it exist, is it accurate to the code now (check commands by running them where
  safe, check every settings name and file path it mentions), and is anything important undocumented?
- Generated docs: are API/settings references generated from the source of truth, or hand-copied and drifting?
- Writing: judge comments and docs by `references/writing.md`; each breach is a finding with the shorter version.
  List every statement that is stale against the current code.
- Report per module: documented / partly / undocumented, with the specific missing or wrong items.

**End-user documentation, `docs-user.md`.** *Applies when the system has users other than its developers.* Inventory
every piece of help a user or admin gets: help pages, onboarding, in-page hints and empty states, error messages,
tooltips, emails explaining what happened, FAQ, admin guides for every setting. For each: is it accurate to current
behavior, written for the reader (no internal names or jargon), findable from where the user needs it, and does it
cover every role's main tasks (cross-check against the task walkthroughs if that domain runs)? List every task,
setting and error a user could meet with no explanation anywhere. Judge the text by `references/writing.md`: each
breach is a finding with the shorter version. List every statement that is stale against the current code.

**Maintainability and handover, `handover.md`.** *Applies whenever someone other than the current author may need to
run or change the system.* Could a new maintainer, using only the repo and its docs, clone, set up, run, test, deploy,
roll back and operate the system? Walk each step and say where they would get stuck. List every account, domain,
registrar, DNS, hosting, email provider, billing method, API key, secret and admin login the system depends on: who
owns it, whether it is tied to one person, how access is transferred, and whether secrets can be rotated without the
original author. Runbooks for the common incidents and the yearly/periodic chores (rollover, renewals, certificate or
key expiry). Single points of knowledge (from the hotspots report if available). End with a handover checklist.

## Scan rules

`ledger.py scan --review R-0001` reads each repository at the reviewed commit and evaluates one rule per domain
(`domain_rules` in the ledger, seeded from the table below; a test keeps the two equal). A rule is `fact >= N` terms
joined by `and` and `or`. A domain whose rule is met is proposed, with the facts that met it as the reason. A domain
with open issues from earlier reviews is proposed whatever its rule says. The same commit gives the same proposal.
Change one only with `ledger.py review-domain R-0001 <code> include --reason '...'` (or `skip`); the owner approves
the result in Step 2b.

| domain | rule |
|--------|------|
| `spec` | `requirements >= 1 or spec_files >= 1` |
| `tests` | `code_files >= 1` |
| `test-quality` | `test_files >= 5` |
| `time-concurrency` | `time_files >= 1 or concurrency_files >= 1` |
| `resilience` | `external_call_files >= 1 or job_files >= 1 or db_files >= 1` |
| `compatibility` | `db_files >= 1 or api_files >= 1 or config_files >= 1` |
| `decision-placement` | `code_files >= 10` |
| `contracts` | `components >= 2` |
| `server` | `server_files >= 1` |
| `database` | `db_files >= 1` |
| `client-api` | `ui_files >= 1 and server_files >= 1` |
| `abstraction` | `code_files >= 1` |
| `hotspots` | `history_months >= 3` |
| `ui` | `ui_files >= 1` |
| `walkthroughs` | `page_files >= 1` |
| `notifications` | `notification_files >= 1` |
| `localization` | `i18n_files >= 1` |
| `security` | `code_files >= 1` |
| `privacy` | `personal_data_files >= 1` |
| `logging` | `code_files >= 1` |
| `operations` | `deploy_files >= 1 or ci_files >= 1` |
| `dependencies` | `code_files >= 1` |
| `performance` | `server_files >= 1 or db_files >= 1 or page_files >= 1` |
| `cost` | `paid_service_files >= 1 or deploy_files >= 1` |
| `docs-developer` | `code_files >= 1` |
| `docs-user` | `user_doc_files >= 1 or page_files >= 1` |
| `handover` | `authors >= 2 or deploy_files >= 1 or history_months >= 6` |

The facts, counted over the files tracked at the commit (vendored and build folders left out):

- `files`, `code_files` (source files), `languages` (how many), `test_files`, `ci_files`, `doc_files`,
  `user_doc_files` (help, guides, manuals), `spec_files` (README, specs, ADRs, contribution rules), `manifests`,
  `lock_files`, `dependencies` (declared in the manifests), `components` (folders holding a manifest; 1 when there is
  code and no manifest), `config_files`, `deploy_files` (container, platform and infrastructure files).
- `ui_files` (markup, styles, components), `page_files` (pages and templates), `db_files` (SQL, migrations, schema
  and model files), `migration_files`, `api_files` (API descriptions, route and controller files), `job_files`
  (jobs, workers, schedulers).
- From the text of the code, one search each: `server_files` (starts or serves HTTP), `time_files` (dates, times,
  expiries), `concurrency_files` (threads, tasks, locks), `external_call_files` (HTTP clients, subprocesses, mail,
  cloud SDKs), `notification_files` (mail, SMS, push, calendar feeds), `i18n_files` (translation and locale calls),
  `personal_data_files` (names, e-mail, passwords, phone, birth dates), `paid_service_files` (payment, cloud and
  hosted-service SDKs).
- From git: `history_commits`, `history_months` (first to last commit), `authors`. From the ledger: `requirements`
  (recorded with `add-requirement`).

## Checklists

Every reviewer of a domain answers its checklist (`domain_checks` in the ledger, seeded from this table; a test keeps
the two equal). `ledger.py brief` and `ledger.py prompt` list it, `ledger.py lint-report` checks each item is
answered with the evidence named, and the severity is the default of a finding that names the item (`- Check:`).
Project-specific questions come from the owner's approved goals, not from new rows.

| check | severity | question | evidence |
|-------|----------|----------|----------|
| `spec.1` | H | Is every stated requirement listed, with its source quoted? | A row per requirement: its code or quote, source and location. |
| `spec.2` | H | Does the implementation of each requirement match it at its boundaries, priority orders and exceptions? | Per requirement: path:line of every copy and a verdict (matches, differs, partly, missing, untested). |
| `spec.3` | M | Does a test assert each requirement? | The test name per requirement, or untested. |
| `spec.4` | M | Does the code do anything no requirement asks for, or do requirements contradict each other? | Each such behaviour or contradiction with path:line. |
| `tests.1` | H | Is every unit of behaviour or configuration listed with the cases it needs? | A row per function, route, command, setting, rule or flag, with its cases and expected outcomes. |
| `tests.2` | H | Is each needed case asserted by a test that would fail if the behaviour broke? | Per case: the test name and Yes, Partly or No. |
| `tests.3` | M | Is every input validated, and every validation branch and error path tested? | Inputs read with no validation; branches and error paths with no test. |
| `tests.4` | M | Are migrations and conversions tested? | Each migration or conversion with its test, or no test. |
| `tests.5` | L | Do the totals add up? | Units, cases needed, asserted and missing. |
| `test-quality.1` | H | What is the mutation score of the core modules? | The score per module and the surviving mutants that matter, or not measured with the hand sample. |
| `test-quality.2` | M | Are any tests flaky or order-dependent? | The results of repeated and shuffled runs. |
| `test-quality.3` | M | Do tests depend on the real clock, time zone, network or shared state? | Each such test by name. |
| `test-quality.4` | L | How fast is the suite, and which tests are slowest? | The run time and the slowest tests. |
| `test-quality.5` | M | Does every recorded guard test still exist and still guard its issue? | Per guard test: exists, and fails without its fix where tried. |
| `time-concurrency.1` | H | Is every place that creates, parses, compares, formats or stores a date or time listed with its time zone handling? | A row per place: path:line, the zone stored and converted, whose today it is. |
| `time-concurrency.2` | M | Are daylight-saving changes, month, year and leap boundaries and inclusive or exclusive ends handled? | The concrete dates to test and what happens on each. |
| `time-concurrency.3` | H | What happens when two actors change the same thing at once? | Per shared write: the guard (version check, constraint, transaction, lock) or the lost update. |
| `time-concurrency.4` | M | Does a test exercise each boundary date and each interleaving? | Test names, or the missing tests by name. |
| `resilience.1` | H | Does every call to another service, queue, database or file system have a timeout and a retry that is safe to repeat? | A row per call: timeout, retry policy, idempotency. |
| `resilience.2` | H | Can any multi-step change stop halfway and leave inconsistent data? | Per change: the transaction or compensating step, or the inconsistent state. |
| `resilience.3` | M | What does each job do on failure, on a rerun, on overlap with itself and on a missed run? | The four answers per job. |
| `resilience.4` | M | What do the user and the operator see for each failure? | At least five failures followed end to end. |
| `compatibility.1` | H | Can today's code read everything older code wrote? | Per stored thing (rows, settings, files, cookies, links): the read path and the oldest version handled. |
| `compatibility.2` | H | Is every schema or settings migration tested from each older version in use? | Per migration: the test and the versions it starts from. |
| `compatibility.3` | M | Can each deploy roll back without losing or corrupting data? | The rollback path per kind of change. |
| `compatibility.4` | M | Are API changes versioned or additive, and do old clients keep working across a deploy? | Each breaking change waiting to happen and the test that would catch it. |
| `decision-placement.1` | H | Is every business decision listed with the layer that decides it today? | A row per decision: where it is decided, path:line. |
| `decision-placement.2` | H | Is any decision written more than once, and do the copies agree? | Every copy with path:line and the result of probing them. |
| `decision-placement.3` | M | Is any decision checked in one path and not in another? | Each path that skips the check. |
| `decision-placement.4` | M | Which decisions should move, and in what order? | Move or keep with the reason, and the tests that must pass before and after. |
| `contracts.1` | H | Is every endpoint or message between components listed with its request, response and errors? | An inventory per boundary. |
| `contracts.2` | H | Does each caller handle every response the callee can give? | Per call: each success shape, refusal, error and timeout, and the ones not handled. |
| `contracts.3` | M | Do the producer's contract and the consumer's copy agree field by field? | Each drifted or duplicated type. |
| `contracts.4` | M | Is every endpoint and response branch tested, failures included? | Tests per branch: timeout, 4xx, 5xx, malformed, unavailable. |
| `server.1` | M | Where does the server hardcode domain or customer-specific behaviour? | Each place with path:line, whether a setting exists, and the fix. |
| `server.2` | H | Is every route, job, consumer, webhook and sign-in flow listed with who may call it? | The inventory with the permission per entry. |
| `server.3` | H | Does each have tests for success, each refusal, validation, stale saves and a failing dependency? | Cases against tests; the missing tests by name. |
| `server.4` | H | Is any client input unvalidated, or any permission check without a negative test? | Each with path:line. |
| `database.1` | M | Is everything the system consumes or produces stored and queryable later? | Anything lost, lossy or only in logs. |
| `database.2` | M | Does every distinct query use an index, bound its results and avoid a query per item? | The query plan per query: scans, repeated queries, unbounded results. |
| `database.3` | H | Do constraints, uniqueness and transactions keep the data consistent under concurrent writes? | Each missing constraint or unguarded write. |
| `database.4` | M | Are migrations reversible, retention defined, and no secret stored in clear? | Per migration and per sensitive column. |
| `client-api.1` | M | Does every backend capability have an API and a screen that uses it? | The capability inventory with the gaps both ways. |
| `client-api.2` | M | Are the API styles coherent: no duplicated capability, one shape for results and errors? | Each inconsistency. |
| `client-api.3` | H | Do two paths to one action apply the same permission check? | Each pair of paths with its checks. |
| `client-api.4` | L | Is any identifier a display name? | Each one. |
| `abstraction.1` | M | Which types, contracts or logic are duplicated between packages or layers? | Each duplicate with every location. |
| `abstraction.2` | M | How many files change to add a new entity, rule, setting group or notification type? | The file list per walk-through. |
| `abstraction.3` | L | Is there dead code, an oversized module or a circular dependency? | Each with its path. |
| `abstraction.4` | L | What is the refactoring order that keeps the system working? | Quick wins, medium, structural. |
| `hotspots.1` | M | Which files change most often and are most complex? | The ranked list from git log. |
| `hotspots.2` | M | Which files always change together but live apart? | Each cluster. |
| `hotspots.3` | M | Which code has a single author, and which busy areas have few tests? | Lists with counts. |
| `hotspots.4` | L | Which files keep producing issues across reviews? | The rows of the hotspot view. |
| `ui.1` | H | Does each page meet the design brief (type scale, palette, density)? | Per page: the goal broken and the screenshot, or rendered result not seen. |
| `ui.2` | H | Does every text meet the required contrast, and every image, field, button and link have a name? | The audit's output per page, or the same computed from the stylesheets and marked not measured. |
| `ui.3` | M | How many colours, font sizes and spacing values are in use against the brief's scale? | The inventory with counts and the values to merge. |
| `ui.4` | M | Does each page work at phone width and in both themes? | Screenshots at 390 and 1280 pixels, light and dark, with what breaks. |
| `ui.5` | M | Are there internal names on screen, typing where a choice would do, or text below the writing standard? | Each with file:line, the text and the shorter version. |
| `ui.6` | H | Is any link shown to someone who is then refused, or any page shown without access? | The access matrix: kind of user by link, button and page. |
| `ui.7` | M | Can every configuration the backend allows be displayed and changed? | Each setting with no screen. |
| `walkthroughs.1` | H | Can each kind of user complete each of its main tasks? | 10 to 20 tasks per kind of user, traced page by page. |
| `walkthroughs.2` | M | Where does a hand-off between pages break or confuse? | Each dead end, missing confirmation or undo, and step that needs information the user cannot see. |
| `walkthroughs.3` | M | Can a first-time user finish the main task unaided, and is what they see first what the task needs? | Per main task: the first screen and where they would stop. |
| `walkthroughs.4` | L | What on each page could be deleted without loss? | A list per page. |
| `notifications.1` | M | Is every message type listed with its trigger, recipients, content source and opt-out? | The inventory. |
| `notifications.2` | H | Can a message be sent twice, missed, or sent to the wrong person after a role change? | Per message type: the guard or the failure. |
| `notifications.3` | M | Are bounces handled, and is staging kept from mailing real people? | Configuration and code, or not verified. |
| `notifications.4` | L | Does each message render on a phone and have a plain-text alternative? | Per message type. |
| `localization.1` | M | Which user-facing strings are hardcoded? | Each with file:line. |
| `localization.2` | M | Are dates, numbers, currency and week start formatted for the reader's locale? | Each fixed format. |
| `localization.3` | L | Will translated text fit, and is right-to-left possible? | Each layout that will not. |
| `security.1` | H | For every route and API function, what is the permission check, and can a lower-privileged, other-tenant or signed-out caller reach it? | A row per route: the check with file:line and the result of changing an id. |
| `security.2` | H | Where does client input reach SQL, HTML, e-mail, file paths, redirects or deserialization unescaped? | Each sink with file:line. |
| `security.3` | H | How are sign-in codes, tokens and sessions stored, expired and limited? | Per method: hashing, expiry, attempt limits, cookie flags. |
| `security.4` | H | Is any secret in the repository, a workflow or a log, or stored in clear? | Locations only, never the value. |
| `security.5` | M | Which browser protections are set? | Per page or response: content security policy, cross-site request checks, cross-origin rules, framing, caching of private responses. |
| `security.6` | M | Who can deploy, and what could each kind of misuser do today? | Workflow permissions and a short threat model. |
| `security.7` | M | Which protections have a test that fails without them? | Test names and the missing tests. |
| `privacy.1` | H | Where does each kind of personal data enter, and where is it stored or copied? | A data map: database, backups, logs, e-mail, analytics, third parties. |
| `privacy.2` | H | Who can read it, and how long is it kept? | Per kind of data. |
| `privacy.3` | M | How would one person's data be exported or deleted in full? | The path, or what would be left behind. |
| `privacy.4` | M | Which obligations apply, and where are the gaps? | Each gap, marked not legal advice. |
| `logging.1` | M | Is every important event logged? | The events that leave no trace: sign-ins, refusals, state changes, external calls, jobs, e-mail, configuration changes. |
| `logging.2` | M | Can an operator answer five realistic complaints from the logs alone? | Each complaint walked, and where the trail breaks. |
| `logging.3` | H | Do logs hold secrets, tokens, codes or personal data, or accept injected lines? | Each statement with file:line. |
| `logging.4` | L | Are structure, levels, a request id and zoned timestamps consistent, with no swallowed exception? | Each exception to it. |
| `logging.5` | M | Do errors reach a person, and where do logs go and for how long? | Alerting, retention and access. |
| `operations.1` | M | Does CI run every suite, linter and type-check on every change and block merges on failure? | The workflow files. |
| `operations.2` | M | Are builds reproducible, and are the deploy and rollback steps written down? | The steps, or what is missing. |
| `operations.3` | H | If the database, hosting account, domain or e-mail provider were lost today, what is the path back? | Per loss: the path, the time, and whether a restore was ever tested. |
| `operations.4` | M | What is backed up, how often, where, and is it encrypted? | Per store. |
| `operations.5` | M | Are scheduled jobs monitored for failure and missed runs? | Per job. |
| `dependencies.1` | M | Which packages are outdated, abandoned or have known problems? | From the lock files; not checked online without network. |
| `dependencies.2` | M | Is every version pinned, and every CI action referenced by commit hash? | Each one that is not. |
| `dependencies.3` | L | Is there a licence problem, an install script or a dependency used for something trivial? | Each one. |
| `performance.1` | M | Where does the time go for each page load and action? | Server calls, queries per call and repeated work; measured or reasoned, saying which. |
| `performance.2` | M | What do the heavy components cost in time and memory at realistic and larger sizes? | Numbers per job type. |
| `performance.3` | M | What is each page's weight and layout shift? | The audit's numbers, or not measured. |
| `performance.4` | M | What breaks first at 10x users and 3x data? | The first limit and the three changes that buy the most. |
| `performance.5` | L | Which performance tests or budgets exist? | The existing ones and the ones to add. |
| `cost.1` | M | What does each service cost now and at 10x scale? | From configuration and pricing in the repository; not verified otherwise. |
| `cost.2` | M | What could run up a bill by accident? | Runaway retries, unbounded logs or storage, no spending limit. |
| `cost.3` | L | Which account and payment method is each service billed to? | Per service. |
| `docs-developer.1` | M | Does every public function, type and module say what it does, its inputs and outputs, and why? | Per module: documented, partly, undocumented. |
| `docs-developer.2` | M | Which comments or docs are stale against the current code? | Each statement with file:line and what the code does. |
| `docs-developer.3` | M | Do the README, setup, architecture and settings docs exist, and do their commands run? | Per document: exists, accurate, what is missing; commands run where safe. |
| `docs-developer.4` | L | Does every piece of text meet the writing standard? | Each breach with the shorter version. |
| `docs-user.1` | M | Which tasks, settings and errors can a user meet with no explanation anywhere? | Each one. |
| `docs-user.2` | M | Is each piece of help accurate to current behaviour and findable where it is needed? | The inventory with a verdict per piece. |
| `docs-user.3` | L | Is it written for the reader, to the writing standard? | Each breach with the shorter version. |
| `handover.1` | H | Could a new maintainer clone, set up, run, test, deploy and roll back from the repository and its docs alone? | Each step walked, and where they would get stuck. |
| `handover.2` | H | Who owns each account, domain, key and secret, and is any tied to one person? | The list with the owner and how access transfers. |
| `handover.3` | M | Are there runbooks for the common incidents and the periodic chores? | Each one, present or missing. |
| `handover.4` | M | Where is knowledge held by one person? | From the history or the hotspots report. |

## Interview questions

Step 2c asks these for each approved domain named here, besides the questions the code prompts. Record each with
`interview-ask --domain <code>`; `--why` says the catalog asks it of every project with that domain.

- `ui`, the design direction: Which sites, apps or screens do you like the look of, and what about each? Which three
  words should describe how this one feels? What must it never look like? Then propose the design brief as `ui`
  goals (`propose-goal --domain ui`), one each for type scale, palette and density, and one per "never"; the owner
  approves them like any goal, and reviewers judge against them.
- `walkthroughs`: Who uses it for the first time, and what is the one task they must finish unaided?
