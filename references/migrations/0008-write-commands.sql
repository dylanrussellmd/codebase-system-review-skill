-- finding_locations.source (the reviewed text of a dirty working tree); v_next_codes for the write commands
-- Plan P-0001 step 98 (SPEC-M5, WALK-M4). The write commands of ledger.py take every code from the new view
-- v_next_codes, which migrate creates with the shipped views. finding_locations gains one column at the end: where
-- its excerpt was read. Every existing reference was read from its commit, so they all keep the default 'commit';
-- fill-refs writes 'working-tree' for a reference of a review whose snapshot recorded uncommitted changes, when the
-- file in the working tree differs from the commit (that is the text the reviewers saw).

ALTER TABLE finding_locations ADD COLUMN source TEXT NOT NULL DEFAULT 'commit' CHECK (source IN ('commit','working-tree'));

INSERT INTO enum_labels(enum, value, label, position) VALUES
 ('location_source','commit','Read from the commit',1),
 ('location_source','working-tree','Read from the reviewed working tree',2);
