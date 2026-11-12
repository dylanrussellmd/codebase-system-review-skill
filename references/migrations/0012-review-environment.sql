-- review_environment: what each review's environment offered (tools, the harness, its connectors)
-- Plan P-0001 step 135 (OWN-6, OWN-8). `ledger.py env --review R-0001` records one row per capability: git, Python,
-- SQLite, Playwright and a browser, node and Lighthouse, the harness, each MCP connector configured for it
-- (capability 'connector:<name>': the name only, never a value from a config file), and the harness tools the agent
-- states (subagents, questions, images, publishing). how_to_enable is what to install or do to have the capability.

CREATE TABLE review_environment (
  review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE RESTRICT,
  capability    TEXT NOT NULL CHECK (length(trim(capability)) > 0),   -- git, playwright, connector:figma ...
  available     INTEGER NOT NULL CHECK (available IN (0,1)),
  detail        TEXT,                    -- a version, a name, or where a connector is configured
  how_to_enable TEXT NOT NULL,           -- what to install or do to have it
  position      INTEGER NOT NULL,        -- the order `env` lists them in
  PRIMARY KEY (review_id, capability)
);
