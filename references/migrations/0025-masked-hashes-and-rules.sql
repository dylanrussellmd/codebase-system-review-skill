-- masked excerpts hash the masked lines; rule triggers hold on UPDATE; approval follows the owner's applied answer
-- Plan P-0002 step 341 (ISS-0714). A masked excerpt's excerpt_sha256 is the sha256 of the masked text, so it never
-- confirms a guess of the secret. migrate gives this connection sha256_hex(text) for this update.

UPDATE finding_locations SET excerpt_sha256 = sha256_hex(excerpt)
  WHERE excerpt_redacted = 1 AND excerpt IS NOT NULL AND excerpt_sha256 IS NOT sha256_hex(excerpt);

-- Step 43 (ISS-0005, ISS-0169, ISS-0170). The rule triggers' UPDATE twins and the theme triggers come with the
-- shipped schema, which migrate re-creates. A repository's path is required ('.' where it was empty), and a plan is
-- superseded exactly when it names its successor: both tables are rebuilt with their rows kept.

UPDATE repositories SET path = '.' WHERE path IS NULL;

CREATE TABLE repositories_new (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,   -- short name used in the brief
  path           TEXT NOT NULL DEFAULT '.',  -- relative to the project root, '.' for the root repo
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
  CHECK ((path NOT GLOB '-*' AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*')
         AND (allow_outside_root = 1 OR (path NOT GLOB '/*' AND path NOT GLOB '*\*' AND path <> '..'
              AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'))))
);
INSERT INTO repositories_new(id, name, path, remote_url, default_branch, permalink_template, notes, created_at, allow_outside_root)
  SELECT id, name, path, remote_url, default_branch, permalink_template, notes, created_at, allow_outside_root FROM repositories;
DROP TABLE repositories;
ALTER TABLE repositories_new RENAME TO repositories;
CREATE UNIQUE INDEX ux_repositories_path      ON repositories(path);

CREATE TABLE plans_new (
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
  superseded_by        INTEGER REFERENCES plans(id)   -- set exactly when status is superseded
                         CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
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
INSERT INTO plans_new(id, code, title, status, approved_by_actor_id, approved_at, superseded_by, doc_url, page_url, created_at, updated_at, change_actor_id, change_note, narrative_md)
  SELECT id, code, title, status, approved_by_actor_id, approved_at, superseded_by, doc_url, page_url, created_at, updated_at, change_actor_id, change_note, narrative_md FROM plans;
DROP TABLE plans;
ALTER TABLE plans_new RENAME TO plans;
CREATE INDEX ix_fk_plans_change_actor_id      ON plans(change_actor_id);
CREATE INDEX ix_fk_plans_approved_by_actor_id        ON plans(approved_by_actor_id);
CREATE INDEX ix_fk_plans_superseded_by               ON plans(superseded_by);

-- Step 44 (ISS-0010). Domain-approval and plan-approval gates have the fixed options approve and change
-- (gate_kinds.options); a review or plan is approved only from its latest applied approval gate answered approve,
-- by the person who answered it, and a plan is complete only when every step is done or dropped (the shipped
-- tr_review_transition and tr_plan_transition).

CREATE TABLE gate_kinds_new (
  value    TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE,
  options  TEXT CHECK (options IS NULL OR (json_valid(options) AND json_type(options) = 'array'))
                                  -- the fixed options every gate of this kind has (open-gate uses them); NULL: any
);
INSERT INTO gate_kinds_new(value, label, position) SELECT value, label, position FROM gate_kinds;
DROP TABLE gate_kinds;
ALTER TABLE gate_kinds_new RENAME TO gate_kinds;
UPDATE gate_kinds SET options = '[{"value":"approve","label":"Approve"},{"value":"change","label":"Change it first"}]'
  WHERE value IN ('domain-approval','plan-approval');
