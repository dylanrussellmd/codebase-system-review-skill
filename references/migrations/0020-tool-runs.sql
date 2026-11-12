-- tool_runs and tool_results: the picked tools' runs at the reviewed commit, and their results as leads
-- Plan P-0001 step 218 (FIX-31, decision D-0015). `ledger.py tool-run` runs one tool of references/tools.md on a
-- clean export of a review's commit (or imports a file it wrote) and records the run: tool, version and the pinned
-- one, command, exit code, duration, the output's sha256, or that the tool is absent and what to do instead. Each
-- result is a lead (LD-0001), never a finding: v_unaddressed_tool_results lists the leads no finding cites and no
-- reviewer dismissed with a reason, and must be empty. migrate creates the triggers and views with the shipped schema.

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
CREATE INDEX ix_fk_tool_runs_review_id     ON tool_runs(review_id);
CREATE INDEX ix_fk_tool_runs_repository_id ON tool_runs(repository_id);
CREATE INDEX ix_fk_tool_runs_domain_code   ON tool_runs(domain_code);
CREATE INDEX ix_fk_tool_runs_actor_id      ON tool_runs(actor_id);

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
CREATE INDEX ix_fk_tool_results_tool_run_id          ON tool_results(tool_run_id);
CREATE INDEX ix_fk_tool_results_domain_code          ON tool_results(domain_code);
CREATE INDEX ix_fk_tool_results_finding_id           ON tool_results(finding_id);
CREATE INDEX ix_fk_tool_results_disposed_by_actor_id ON tool_results(disposed_by_actor_id);
CREATE INDEX ix_fk_tool_results_carried_from_id      ON tool_results(carried_from_id);
CREATE INDEX ix_tool_results_fingerprint             ON tool_results(fingerprint);

INSERT INTO ledger_checks(view, label, tier, position) VALUES
 ('v_unaddressed_tool_results','Tool results no finding cites and no reviewer dismissed','error',84);

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('tool_run_status','ran','Ran',1), ('tool_run_status','imported','Imported from a file',2),
 ('tool_run_status','absent','Not installed',3), ('tool_run_status','skipped','Not run',4),
 ('tool_run_status','failed','Failed',5),
 ('tool_output_format','sarif','SARIF',1), ('tool_output_format','junit','JUnit XML',2),
 ('tool_output_format','lcov','LCOV',3), ('tool_output_format','cobertura','Cobertura XML',4),
 ('tool_output_format','json','JSON',5), ('tool_output_format','jsonl','JSON lines',6),
 ('tool_result_level','error','Error',1), ('tool_result_level','warning','Warning',2),
 ('tool_result_level','note','Note',3), ('tool_result_level','none','No level',4);
