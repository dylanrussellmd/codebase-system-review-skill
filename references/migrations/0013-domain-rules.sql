-- domain_rules: the rule by which scan proposes each domain; review_domains.proposed_by_rule
-- Plan P-0001 step 138 (OWN-9, G-09). `ledger.py scan --review R-0001` inventories each repository at its reviewed
-- commit and evaluates one rule per domain against the facts it counts ('ui_files >= 1', terms joined by ' and ' and
-- ' or '); a domain whose rule is met is proposed. review_domains.proposed_by_rule holds the rule that proposed a
-- domain (NULL when scan skipped it or a person added it). The rules are the table in references/domains.md.

CREATE TABLE domain_rules (
  domain_code TEXT PRIMARY KEY REFERENCES domains(code),
  rule        TEXT NOT NULL CHECK (length(trim(rule)) > 0)   -- 'db_files >= 1 or api_files >= 1'
);
INSERT INTO domain_rules(domain_code, rule) VALUES
 ('spec','requirements >= 1 or spec_files >= 1'),
 ('tests','code_files >= 1'),
 ('test-quality','test_files >= 5'),
 ('time-concurrency','time_files >= 1 or concurrency_files >= 1'),
 ('resilience','external_call_files >= 1 or job_files >= 1 or db_files >= 1'),
 ('compatibility','db_files >= 1 or api_files >= 1 or config_files >= 1'),
 ('decision-placement','code_files >= 10'),
 ('contracts','components >= 2'),
 ('server','server_files >= 1'),
 ('database','db_files >= 1'),
 ('client-api','ui_files >= 1 and server_files >= 1'),
 ('abstraction','code_files >= 1'),
 ('hotspots','history_months >= 3'),
 ('ui','ui_files >= 1'),
 ('walkthroughs','page_files >= 1'),
 ('notifications','notification_files >= 1'),
 ('localization','i18n_files >= 1'),
 ('security','code_files >= 1'),
 ('privacy','personal_data_files >= 1'),
 ('logging','code_files >= 1'),
 ('operations','deploy_files >= 1 or ci_files >= 1'),
 ('dependencies','code_files >= 1'),
 ('performance','server_files >= 1 or db_files >= 1 or page_files >= 1'),
 ('cost','paid_service_files >= 1 or deploy_files >= 1'),
 ('docs-developer','code_files >= 1'),
 ('docs-user','user_doc_files >= 1 or page_files >= 1'),
 ('handover','authors >= 2 or deploy_files >= 1 or history_months >= 6');

ALTER TABLE review_domains ADD COLUMN proposed_by_rule TEXT;
