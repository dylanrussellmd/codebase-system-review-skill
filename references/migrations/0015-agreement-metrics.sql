-- the agreement metrics compare-runs records: domain_agreement, finding_agreement, severity_agreement
-- Plan P-0001 step 141 (OWN-9, K55). `ledger.py compare-runs R-A R-B --record` stores how far two runs of a review
-- agree, in %, on the second review, so drift is followed over time like any other metric.

INSERT OR IGNORE INTO metric_defs(name, label, unit, better) VALUES
 ('domain_agreement','Domains both runs chose','%','higher'),
 ('finding_agreement','Issues both runs found','%','higher'),
 ('severity_agreement','Shared issues given the same severity','%','higher');
