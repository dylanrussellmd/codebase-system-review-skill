"""Seed a demo ledger with two reviews, matching, a plan and gates; exercises triggers."""
import json, sqlite3, subprocess, sys
root = sys.argv[1]
db = f"{root}/.code-review/ledger.sqlite"
c1, c2 = [subprocess.check_output(["git", "-C", root, "rev-parse", r]).decode().strip() for r in ("HEAD~1", "HEAD")]
con = sqlite3.connect(db); con.execute("PRAGMA foreign_keys=ON")
x = con.execute
x("INSERT INTO repositories(name,path,default_branch,permalink_template) VALUES ('demo','.','main','https://github.com/example/demo/blob/{sha}/{path}#L{start}-L{end}')")
x("INSERT INTO actors(kind,name) VALUES ('owner','Dylan')")                                   # 1
x("INSERT INTO actors(kind,name,model,harness) VALUES ('manager','Review manager','claude-opus-5-5','Claude Code')")  # 2
x("INSERT INTO actors(kind,name,model,parent_actor_id) VALUES ('reviewer','security reviewer','claude-opus-5-5',2)")  # 3
x("INSERT INTO actors(kind,name,model,parent_actor_id) VALUES ('reviewer','spec reviewer','claude-opus-5-5',2)")      # 4
x("INSERT INTO design_goals(code,text,source) VALUES ('G-01','Every rule and behavior is a setting.','owner'),('G-02','No internal names on screen.','owner')")

def review(code, sha, prev=None, status='handed-off'):
    x("""INSERT INTO reviews(code,title,scope,status,request_text,requested_by_actor_id,manager_actor_id,approved_by_actor_id,approved_at,previous_review_id,verdict,started_at)
         VALUES (?,?,?,'planning',?,1,2,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,?,?)""",
      (code, f"Comprehensive review {code}", 'full' if not prev else 'second', 'Run the full system review',
       prev, 'Solid core; sign-in codes and notice rules need work.', '2026-11-0%sT18:00:00Z' % (1 if not prev else 9)))
    rid = con.execute("SELECT id FROM reviews WHERE code=?", (code,)).fetchone()[0]
    x("UPDATE reviews SET status=? WHERE id=?", (status, rid))
    x("INSERT INTO review_repositories(review_id,repository_id,commit_sha,branch,commit_subject) VALUES (?,1,?,'main',?)",
      (rid, sha, 'Initial time off and sign-in' if not prev else 'Use secrets for sign-in codes'))
    x("INSERT INTO review_goals VALUES (?,1),(?,2)", (rid, rid))
    x("INSERT INTO review_domains VALUES (?,'security','included','Sign-in codes and sessions exist','none',1,NULL)", (rid,))
    x("INSERT INTO review_domains VALUES (?,'spec','included','Owner rules for time off are stated','none',1,NULL)", (rid,))
    x("INSERT INTO review_domains VALUES (?,'database','skipped','No database in this project','none',0,NULL)", (rid,))
    x("INSERT INTO reviewer_runs(review_id,domain_code,actor_id,label,scope,status,items_reviewed,high_count,medium_count,low_count) VALUES (?,'security',3,'security','sign-in','finished',4,1,0,0)", (rid,))
    x("INSERT INTO reviewer_runs(review_id,domain_code,actor_id,label,scope,status,items_reviewed,high_count,medium_count,low_count) VALUES (?,'spec',4,'spec','time-off rules','finished',6,0,1,1)", (rid,))
    return rid

def finding(rid, run, pid, sev, kind, dom, theme, sub, summ, before, rec, path, a, b, sym):
    x("""INSERT INTO findings(review_id,reviewer_run_id,point_id,severity,kind,domain_code,theme_code,sub,summary,before_state,recommendation,recommended_test,fingerprint,verification,verified_by_actor_id)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'confirmed', 2)""",
      (rid, run, pid, sev, kind, dom, theme, sub, summ, before, rec, f"test_{sub.replace('-','_')}", f"{dom}:{theme}:{sub}"))
    fid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    x("INSERT INTO finding_locations(finding_id,repository_id,path,line_start,line_end,symbol) VALUES (?,1,?,?,?,?)", (fid, path, a, b, sym))
    return fid

def new_issue(code, fid, rid, title, dom, theme, sub, sev, kind, before):
    x("""INSERT INTO issues(code,title,domain_code,theme_code,sub,kind,severity,fingerprint,before_state,first_seen_review_id,change_actor_id)
         VALUES (?,?,?,?,?,?,?,?,?,?,2)""", (code, title, dom, theme, sub, kind, sev, f"{dom}:{theme}:{sub}", before, rid))
    iid = con.execute("SELECT id FROM issues WHERE code=?", (code,)).fetchone()[0]
    x("UPDATE findings SET issue_id=?, match_type='new', match_confidence='certain' WHERE id=?", (iid, fid))
    return iid

r1 = review("R-0001", c1)
f1 = finding(r1, 1, "SEC-H1", "H", "security", "security", "sign-in", "code-randomness", "Sign-in codes come from a predictable random generator.",
             "make_code uses random.randint, which is not suitable for secrets.", "Use secrets.randbelow.", "src/auth.py", 1, 3, "make_code")
f2 = finding(r1, 2, "SPEC-M1", "M", "defect", "spec", "spec-mismatch", "notice-period", "Notice period is hardcoded to 14 days instead of a setting.",
             "can_request refuses anything under 14 days; the owner asked for it to be a setting.", "Read the notice period from program settings.", "src/timeoff.py", 2, 4, "can_request")
f3 = finding(r1, 2, "SPEC-L1", "L", "defect", "spec", "spec-mismatch", "admin-bypass", "Admins skip the weekend limit entirely.",
             "Admins bypass the weekend-off rule.", "Apply the same rule with an override that is recorded.", "src/timeoff.py", 5, 6, "can_request")
i1 = new_issue("ISS-0001", f1, r1, "Predictable sign-in codes", "security", "sign-in", "code-randomness", "H", "security", "Codes generated with random.randint.")
i2 = new_issue("ISS-0002", f2, r1, "Notice period hardcoded", "spec", "spec-mismatch", "notice-period", "M", "defect", "14 days in code.")
i3 = new_issue("ISS-0003", f3, r1, "Admins bypass the weekend limit", "spec", "spec-mismatch", "admin-bypass", "L", "defect", "Admin bypass.")

# Plan
x("INSERT INTO plans(code,title,status,approved_by_actor_id,approved_at) VALUES ('P-0001','Fix plan for R-0001','drafting',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
x("INSERT INTO plan_reviews VALUES (1,?)", (r1,))
x("INSERT INTO phases(plan_id,number,title,goal) VALUES (1,1,'Safety net','Every later step is checked by tests.'),(1,2,'Fix first','Confirmed defects fixed with failing tests first.')")
x("INSERT INTO steps(plan_id,phase_id,number,section,title,change,proof) VALUES (1,1,1,'CI','Run every suite in CI','Add a CI workflow.','CI fails on a broken test.')")
x("INSERT INTO steps(plan_id,phase_id,number,title,change,proof) VALUES (1,2,2,'Use secrets for sign-in codes','Replace random with secrets in make_code.','test_code_uses_secrets fails before, passes after.')")
x("INSERT INTO steps(plan_id,phase_id,number,title,change,proof,owner_need,owner_question,behavior_change,behavior_note) VALUES (1,2,3,'Make the notice period a setting','Add notice_days setting.','Tests with 7 and 21 days.','decision','Should the default stay 14 days?',1,'Programs can set their own notice.')")
x("INSERT INTO step_dependencies VALUES (2,1),(3,1)")
try:
    x("INSERT INTO step_dependencies VALUES (1,3)"); print("FAIL: backward dependency accepted")
except sqlite3.IntegrityError as e:
    print("ok: dependency order enforced:", e)
x("INSERT INTO step_findings VALUES (2,?,NULL),(3,?,NULL),(1,?,NULL)", (f1, f2, f3))
x("UPDATE plans SET status='in-progress' WHERE id=1")
x("UPDATE steps SET status='doing', change_actor_id=2 WHERE id=2")
x("UPDATE steps SET status='done', progress_note='Sign-in codes now come from a secure generator.', change_commit_sha=? WHERE id=2", (c2,))
x("INSERT INTO step_commits(step_id,repository_id,commit_sha,branch,summary) VALUES (2,1,?,'main','Use secrets for sign-in codes')", (c2,))
x("UPDATE issues SET status='fixed', after_state='Codes come from secrets.randbelow.', change_step_id=2, change_commit_sha=?, change_actor_id=2, change_note='Step 2 done' WHERE id=?", (c2, i1))
x("INSERT INTO guard_tests(issue_id,repository_id,test_path,test_name,step_id,added_commit_sha,failed_before_fix,passes_after_fix) VALUES (?,1,'tests/test_auth.py','test_code_uses_secrets',2,?,1,1)", (i1, c2))
x("UPDATE steps SET status='waiting-owner' WHERE id=3")

# Decision + gates
x("INSERT INTO decisions(code,question,status,review_id,step_id,asked_by_actor_id) VALUES ('D-0001','Should the default notice period stay 14 days?','open',?,3,2)", (r1,))
x("INSERT INTO gates(code,kind,title,question,context,options,review_id,plan_id,step_id,decision_id,issue_id,opened_by_actor_id) VALUES ('GATE-0001','decision','Default notice period','Should the default notice period stay 14 days when it becomes a setting?','Today the code refuses requests made less than 14 days ahead. Programs will be able to change it.',?,?,1,3,1,?,2)",
  (json.dumps([{"value": "keep-14", "label": "Keep 14 days", "description": "Current behaviour stays the default."}, {"value": "use-7", "label": "Change to 7 days"}]), r1, i2))

# Second review
r2 = review("R-0002", c2, prev=r1, status='awaiting-approval')
x("INSERT INTO review_issue_checks(review_id,issue_id,outcome,evidence,actor_id) VALUES (?,?,'fixed','make_code now uses secrets.randbelow (src/auth.py:3)',3)", (r2, i1))
f4 = finding(r2, 4, "SPEC-M1", "M", "defect", "spec", "spec-mismatch", "notice-period", "Notice period is still 14 days in code.",
             "Unchanged since R-0001.", "Read it from settings.", "src/timeoff.py", 2, 4, "can_request")
x("UPDATE findings SET issue_id=?, match_type='still-present', match_confidence='certain', match_rationale='Same function and lines as ISS-0002' WHERE id=?", (i2, f4))
x("INSERT INTO review_issue_checks(review_id,issue_id,outcome,finding_id,actor_id) VALUES (?,?,'still-present',?,4)", (r2, i2, f4))
f5 = finding(r2, 3, "SEC-M1", "M", "security", "security", "sign-in", "code-compare", "Code check uses == instead of a constant-time compare.",
             "check compares codes with ==.", "Use hmac.compare_digest.", "src/auth.py", 4, 5, "check")
i4 = new_issue("ISS-0004", f5, r2, "Sign-in code compare is not constant-time", "security", "sign-in", "code-compare", "M", "security", "== compare.")
x("INSERT INTO issue_relations(from_issue_id,to_issue_id,relation,confidence,rationale,established_in_review_id,established_by_actor_id) VALUES (?,?,'related-to','likely','Same sign-in code path as the earlier randomness issue',?,2)", (i4, i1, r2))
x("INSERT INTO review_issue_checks(review_id,issue_id,outcome,actor_id) VALUES (?,?,'not-checked',4)", (r2, i3))
x("INSERT INTO gates(code,kind,title,question,context,options,allow_text,review_id,opened_by_actor_id) VALUES ('GATE-0002','domain-approval','Approve the domains for R-0002','Run these 2 domains with 2 reviewers?','Included: Security, Spec conformance. Skipped: Database (no database).',?,1,?,2)",
  (json.dumps([{"value": "approve", "label": "Approve as listed"}, {"value": "add", "label": "Add skipped domains"}]), r2))
for rid, v in ((r1, 61.0), (r2, 74.5)):
    x("INSERT INTO metrics(review_id,domain_code,name,scope,value,unit,method) VALUES (?,'test-quality','mutation_score','src',?,'%','measured')", (rid, v))
con.commit()

# Regression: a later finding matches the fixed issue -> status regressed
x("INSERT INTO findings(review_id,point_id,severity,kind,domain_code,theme_code,sub,summary) VALUES (?, 'SEC-H9','H','security','security','sign-in','code-randomness','Test probe: randomness returned')", (r2,))
fid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
x("UPDATE findings SET issue_id=?, match_type='regression' WHERE id=?", (i1, fid))
print("regression ->", con.execute("SELECT status, times_seen FROM issues WHERE id=?", (i1,)).fetchone())
con.rollback()  # undo the probe
print("after rollback ->", con.execute("SELECT status, times_seen, resolved_at IS NOT NULL FROM issues WHERE id=?", (i1,)).fetchone())
print("times_seen ISS-0002:", con.execute("SELECT times_seen, last_seen_review_id FROM issues WHERE id=?", (i2,)).fetchone())
print("location commits:", con.execute("SELECT f.point_id, substr(fl.commit_sha,1,7) FROM finding_locations fl JOIN findings f ON f.id=fl.finding_id").fetchall())
print("events:", con.execute("SELECT entity_type, event, from_value, to_value FROM events ORDER BY id").fetchall())
