-- multi-select gates: gates.multi and gates.preselect; a multi-select answer is a JSON list of option values
-- Plan P-0001 step 217 (OWN-14). A gate opened with --multi takes any number of its options; --preselect names the
-- recommended ones the page ticks first. Its answer_value is a JSON list of the chosen values ('[]' for none),
-- checked against the options by answer_gate() and tr_gate_answer_rules. Gates already in the ledger stay
-- single-select. migrate creates the triggers with the shipped schema.

ALTER TABLE gates ADD COLUMN multi INTEGER NOT NULL DEFAULT 0 CHECK (multi IN (0,1));
ALTER TABLE gates ADD COLUMN preselect TEXT CHECK (preselect IS NULL OR (json_valid(preselect) AND json_type(preselect) = 'array'));
