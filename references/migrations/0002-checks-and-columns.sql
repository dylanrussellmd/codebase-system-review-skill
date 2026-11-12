-- url, path, hash and code checks; repositories.allow_outside_root; finding_locations.excerpt_redacted
-- Plan P-0001 steps 44-47 and 51. Ledger links are http(s) only (a javascript: or other link is not data worth
-- keeping: it is set to NULL here, and fill-refs writes permalinks again from permalink_template); repository and
-- location paths, commit hashes and review codes get CHECKs, so a row that breaks one makes this migration fail
-- and change nothing (fix the row, then run migrate again). Five tables are rebuilt with SQLite's recipe: create
-- <table>_new, copy the rows, drop the old table, rename the new one, re-create its indexes.

UPDATE repositories SET permalink_template = NULL
  WHERE permalink_template IS NOT NULL AND permalink_template NOT GLOB 'https://*' AND permalink_template NOT GLOB 'http://*';
UPDATE reviews SET summary_doc_url = NULL
  WHERE summary_doc_url IS NOT NULL AND summary_doc_url NOT GLOB 'https://*' AND summary_doc_url NOT GLOB 'http://*';
UPDATE finding_locations SET permalink = NULL
  WHERE permalink IS NOT NULL AND permalink NOT GLOB 'https://*' AND permalink NOT GLOB 'http://*';
UPDATE step_commits SET pr_url = NULL
  WHERE pr_url IS NOT NULL AND pr_url NOT GLOB 'https://*' AND pr_url NOT GLOB 'http://*';

CREATE TABLE repositories_new (
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
INSERT INTO repositories_new(id, name, path, remote_url, default_branch, permalink_template, notes, created_at)
  SELECT id, name, path, remote_url, default_branch, permalink_template, notes, created_at FROM repositories;
DROP TABLE repositories;
ALTER TABLE repositories_new RENAME TO repositories;

CREATE TABLE reviews_new (
  id                    INTEGER PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE    -- R-0001
                          CHECK (code GLOB 'R-[0-9][0-9][0-9][0-9]*' AND code NOT GLOB 'R-*[^0-9]*'),
  title                 TEXT NOT NULL,
  scope                 TEXT NOT NULL CHECK (scope IN
                          ('full','partial','targeted','second','fix-verification')),
  status                TEXT NOT NULL DEFAULT 'planning' CHECK (status IN
                          ('planning','awaiting-approval','approved','running','verifying',
                           'indexing','consolidating','handed-off','abandoned')),
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
  completed_at          TEXT,
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO reviews_new(id, code, title, scope, status, request_text, requested_by_actor_id, manager_actor_id, approved_by_actor_id, approved_at, previous_review_id, skill_version, review_folder, summary_doc_url, verdict, summary, started_at, completed_at, updated_at)
  SELECT id, code, title, scope, status, request_text, requested_by_actor_id, manager_actor_id, approved_by_actor_id, approved_at, previous_review_id, skill_version, review_folder, summary_doc_url, verdict, summary, started_at, completed_at, updated_at FROM reviews;
DROP TABLE reviews;
ALTER TABLE reviews_new RENAME TO reviews;

CREATE TABLE review_repositories_new (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha    TEXT NOT NULL CHECK ((length(commit_sha) = 40 OR length(commit_sha) = 64)
                                   AND commit_sha NOT GLOB '*[^0-9a-f]*'),  -- full hash, lowercase hex
  branch        TEXT,
  commit_time   TEXT,
  commit_subject TEXT,
  tag_name      TEXT,                     -- e.g. review/R-0001, keeps the commit reachable
  tag_pushed    INTEGER NOT NULL DEFAULT 0 CHECK (tag_pushed IN (0,1)),
  is_dirty      INTEGER NOT NULL DEFAULT 0 CHECK (is_dirty IN (0,1)),
  dirty_note    TEXT,                     -- what was uncommitted, if anything
  dirty_patch_path TEXT,                  -- saved `git diff` of uncommitted changes, if any
  PRIMARY KEY (review_id, repository_id)
);
INSERT INTO review_repositories_new(review_id, repository_id, commit_sha, branch, commit_time, commit_subject, tag_name, tag_pushed, is_dirty, dirty_note, dirty_patch_path)
  SELECT review_id, repository_id, commit_sha, branch, commit_time, commit_subject, tag_name, tag_pushed, is_dirty, dirty_note, dirty_patch_path FROM review_repositories;
DROP TABLE review_repositories;
ALTER TABLE review_repositories_new RENAME TO review_repositories;

CREATE TABLE finding_locations_new (
  id             INTEGER PRIMARY KEY,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  repository_id  INTEGER NOT NULL REFERENCES repositories(id),
  commit_sha     TEXT CHECK (commit_sha IS NULL OR (length(commit_sha) >= 7 AND commit_sha NOT GLOB '*[^0-9a-f]*')),
                                            -- filled from review_repositories if left NULL
  path           TEXT NOT NULL,             -- repo-relative, forward slashes (see the CHECK below)
  line_start     INTEGER CHECK (line_start IS NULL OR line_start >= 1),
  line_end       INTEGER,
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
  CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start),
  -- path: relative (no leading / or -), no backslash, no .. segment, no control characters
  CHECK (length(path) > 0 AND path NOT GLOB '/*' AND path NOT GLOB '-*' AND path NOT GLOB '*\*'
         AND path <> '..' AND path NOT GLOB '../*' AND path NOT GLOB '*/../*' AND path NOT GLOB '*/..'
         AND (path NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'))
);
INSERT INTO finding_locations_new(id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role)
  SELECT id, finding_id, repository_id, commit_sha, path, line_start, line_end, symbol, blob_sha, excerpt, excerpt_sha256, permalink, role FROM finding_locations;
DROP TABLE finding_locations;
ALTER TABLE finding_locations_new RENAME TO finding_locations;
CREATE INDEX ix_locations_finding    ON finding_locations(finding_id);
CREATE INDEX ix_locations_path       ON finding_locations(path, symbol);

CREATE TABLE step_commits_new (
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
INSERT INTO step_commits_new(id, step_id, repository_id, commit_sha, branch, pr_url, summary, committed_at)
  SELECT id, step_id, repository_id, commit_sha, branch, pr_url, summary, committed_at FROM step_commits;
DROP TABLE step_commits;
ALTER TABLE step_commits_new RENAME TO step_commits;
