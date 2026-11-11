# Domain catalog

Read this in Step 2a to choose the domains, and give each reviewer its domain's entry in the brief (Step 2c). Each
entry names the domain, its default report file and, in italics, when it usually applies (the same sentence as
`domains.description` in the ledger; the code is in `ledger.py query "SELECT code, report_file FROM domains"`). Choose
from these; launch only what the owner approved. A domain the catalog lacks is added with
`references/recipes.md#add-a-custom-domain`.

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
expected outcomes; the existing tests and whether each case is actually asserted (Yes / Partly / No); verdict and the
missing tests by name. Also list: inputs read with no validation, validation branches with no test,
migrations/conversions with no test, and error paths with no test. End with totals (units, cases needed, asserted,
missing).

**Test suite quality, `test-quality.md`.** *Applies when there is a meaningful test suite.* Measure, don't judge:
run mutation testing on the core modules (Stryker for JS/TS, mutmut or cosmic-ray for Python, cargo-mutants for Rust,
PIT for Java; install outside the repo or in a temp copy) and report the mutation score per module with the surviving
mutants that matter most. Run the suite several times (and in random order where the runner allows) to find flaky and
order-dependent tests. Find tests that depend on the real clock, time zone, network, or shared state. Report test
suite speed and the slowest tests. Review test doubles: do fakes enforce the real limits and errors of what they
replace? Check that every guard test recorded in the ledger (listed in the brief) still exists and still fails without its fix
where cheap to try; the manager records the answer (`references/recipes.md#confirm-guard-tests`). If mutation tooling can't run, say so and fall back to sampling assertions by hand, marked "not measured".

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
public APIs, or deployed clients.* Can the new code read everything old code wrote (rows, saved settings, cached files,
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
errors, docs accuracy and tests. Can every setting or capability be reached through the boundary? Does the caller handle every response the callee can
give (each success shape, refusal, error and timeout), not only the common one? Is every field of
every response used by the caller? Does the caller populate every input? Field-by-field drift between the producer's
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

**Front end / UI (one to three reviewers, split by area), `ui-<area>.md`.** *Applies when there are screens.* Each
reads every line of its screens and the backend views feeding them, and reports violations of each requirement with
file:line, the exact text or code, and the fix: (1) generic, including what a second customer/tenant would see; (2) can
display and act on every configuration the backend allows (best through settings forms generated from the
settings schema, so a new setting needs no new screen code); (3) no internal names on screen; (4) select, don't type;
(5) context-sensitive by tenant and by user role; (6) usability, looks, small-screen width, accessibility (keyboard,
contrast, labels, focus, screen-reader names); (7) duplicated code that should be shared. One reviewer owns the
**ACCESS AUDIT**: a matrix of every kind of user (signed out, pending, no roles, each role, admin, super admin, service
account, user of another tenant) against every link, button and page. List every link shown to someone who is then
refused, and every page that, opened directly without access, shows anything other than the explanation (partial
render, endless loading, raw error, blank page, bare status).

**Task walkthroughs, `walkthroughs.md`.** *Applies when users complete multi-step tasks.* List the 10-20 most important
real tasks per kind of user (e.g. submit an expense and get it approved; invite a member; change a setting and see its
effect). Trace each end to end through the code: every page, call, check, email and state change. Report broken or
confusing hand-offs between pages, dead ends, steps that need information the user can't see, missing confirmations
or undo, and tasks the system can't complete at all. Distinct from per-page UI review: it finds problems that only show
across pages.

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
- Tests: which protections have a test that would fail if the protection were removed, and the missing ones by name.

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
  what it does, its inputs/outputs, units, invariants and why (not just what)? Flag comments that are wrong, stale
  (describe old behavior, reference removed code), contradict the code, or restate the obvious. Flag TODO/FIXME/HACK
  notes with no owner or issue. Magic numbers with no explanation.
- Repo docs: README, setup, architecture overview, data model, API reference, settings reference, contribution guide,
  ADRs/decision log. For each: does it exist, is it accurate to the code now (check commands by running them where
  safe, check every settings name and file path it mentions), and is anything important undocumented?
- Generated docs: are API/settings references generated from the source of truth, or hand-copied and drifting?
- Report per module: documented / partly / undocumented, with the specific missing or wrong items.

**End-user documentation, `docs-user.md`.** *Applies when the system has users other than its developers.* Inventory
every piece of help a user or admin gets: help pages, onboarding, in-page hints and empty states, error messages,
tooltips, emails explaining what happened, FAQ, admin guides for every setting. For each: is it accurate to current
behavior, written for the reader (no internal names or jargon), findable from where the user needs it, and does it
cover every role's main tasks (cross-check against the task walkthroughs if that domain runs)? List every task,
setting and error a user could meet with no explanation anywhere, and every doc describing behavior that no longer
exists.

**Maintainability and handover, `handover.md`.** *Applies whenever someone other than the current author may need to
run or change the system.* Could a new maintainer, using only the repo and its docs, clone, set up, run, test, deploy,
roll back and operate the system? Walk each step and say where they would get stuck. List every account, domain,
registrar, DNS, hosting, email provider, billing method, API key, secret and admin login the system depends on: who
owns it, whether it is tied to one person, how access is transferred, and whether secrets can be rotated without the
original author. Runbooks for the common incidents and the yearly/periodic chores (rollover, renewals, certificate or
key expiry). Single points of knowledge (from the hotspots report if available). End with a handover checklist.
