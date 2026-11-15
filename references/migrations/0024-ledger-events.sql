-- events take the entity type 'ledger': a redaction of a row that belongs to no other entity
-- Plan P-0002 step 35 (SECL-M7). redact logs every redaction as an event; a row of a table such as repositories or
-- requirements, which belongs to no review, issue or other entity, is logged against the ledger itself
-- (entity_type 'ledger', entity_id 0). The events table is rebuilt with the wider rule and its rows kept; migrate
-- re-creates its triggers with the shipped schema.

CREATE TABLE events_new (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('review','issue','step','plan','decision','finding','gate',
                                                   'relation','check','step-finding','goal','question','ledger')),
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
INSERT INTO events_new(id, entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, commit_sha, note, at)
  SELECT id, entity_type, entity_id, event, from_value, to_value, actor_id, review_id, step_id, commit_sha, note, at FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX ix_events_entity        ON events(entity_type, entity_id, at);
CREATE INDEX ix_events_history       ON events(entity_type, entity_id, id, event, to_value)
  WHERE event IN ('opened','status-changed');
CREATE INDEX ix_fk_events_actor_id                   ON events(actor_id);
CREATE INDEX ix_fk_events_review_id                  ON events(review_id);
CREATE INDEX ix_fk_events_step_id                    ON events(step_id);

INSERT INTO enum_labels(enum, value, label, position) VALUES ('entity_type','ledger','Ledger',13);
