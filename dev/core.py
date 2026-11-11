#!/usr/bin/env python3
"""Code review ledger: one SQLite file per project that records every review,
every point it raised, how points relate across reviews, the fix plan, and
what is waiting on the owner. Standard library only (Python 3.9+; tested on 3.9.0, 3.9.25 and 3.13).

    python3 ledger.py init               create .code-review/ledger.sqlite here
    python3 ledger.py serve [--port N]   open the local viewer (127.0.0.1 only)
    python3 ledger.py check              integrity + must-be-empty checks
    python3 ledger.py fill-refs [R-0001] copy excerpts, blob ids, permalinks from git
    python3 ledger.py tag-review R-0001  tag each reviewed commit so it stays reachable
    python3 ledger.py show <location id> print the cited lines at their commit
    python3 ledger.py gates              list gates that are open or answered
    python3 ledger.py dump               write .code-review/ledger.sql (text, for diffs)

Options: --root DIR (project root; default: the nearest folder upward with a ledger, else the
top of the current git repository, else the current directory), --db FILE.
"""
import argparse, hashlib, json, re, secrets, sqlite3, subprocess, sys, threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCHEMA_VERSION = 1
SCHEMA = r"""@@SCHEMA@@"""
PAGE = r"""@@PAGE@@"""

HEX = re.compile(r"^[0-9a-f]{7,64}$")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------- database

def connect(db, readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    else:
        con = sqlite3.connect(db, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def init(root, db):
    db.parent.mkdir(parents=True, exist_ok=True)
    fresh = not db.exists()
    con = connect(db)
    if fresh:
        con.executescript(SCHEMA)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('project_name',?)", (root.name,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('created_at',?)", (now(),))
        con.commit()
        print(f"created {db}")
    else:
        v = int(one(con, "SELECT value FROM meta WHERE key='schema_version'")["value"])
        if v > SCHEMA_VERSION:
            sys.exit(f"{db} is schema {v}; this ledger.py only knows {SCHEMA_VERSION}. Use a newer ledger.py.")
        if v < SCHEMA_VERSION:
            sys.exit(f"{db} is schema {v}; run the migrations for {v + 1}..{SCHEMA_VERSION} first.")
        print(f"{db} already exists (schema {v})")
    me = Path(__file__).resolve()
    home = db.parent / "ledger.py"
    if me != home.resolve() and (not home.exists() or home.read_bytes() != me.read_bytes()):
        import shutil
        if home.exists():
            print(f"note: replacing {home} with this copy of ledger.py")
        shutil.copyfile(me, home)
        print(f"copied ledger.py to {home}")
    gi = db.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("ledger.sqlite-journal\nledger.sqlite-wal\nledger.sqlite-shm\n")
    con.close()


def find_root(start, explicit=False):
    """The project this ledger belongs to: the nearest folder at or above `start` that already has
    .code-review/ledger.sqlite; otherwise the top of the git repository containing `start`; otherwise `start`.
    An explicit --root is used as given."""
    if explicit:
        return start
    for d in (start, *start.parents):
        if (d / ".code-review" / "ledger.sqlite").exists():
            return d
    try:
        top = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                             capture_output=True, timeout=10)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.decode().strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Twin of the must-be-empty list in SKILL.md (Rules for the ledger / check) and of the 'must be empty' comments in
# references/schema.sql; change all three together. dev/tests/test_check.py enforces that they agree.
MUST_BE_EMPTY = [
    ("v_unmatched_findings", "Findings not matched to an issue"),
    ("v_unchecked_known_issues", "Known issues in a domain that ran, with no check recorded"),
    ("v_untraceable_refs", "Code references that cannot be traced to exact lines"),
    ("v_uncovered_findings", "Findings no step of their plan closes"),
    ("v_coverage_problems", "Steps closing nothing, or findings closed twice without a reason"),
]


def check(con, quiet=False):
    bad = 0
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        print("integrity_check:", ic); bad += 1
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        print(f"foreign_key_check: {len(fk)} broken references"); bad += 1
    for view, label in MUST_BE_EMPTY:
        n = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        if n:
            bad += 1
            print(f"{n:5d}  {label}  ({view})")
            for r in rows(con, f"SELECT * FROM {view} LIMIT 5"):
                print("       ", json.dumps(r, ensure_ascii=False)[:200])
        elif not quiet:
            print(f"    0  {label}")
    return bad


# ---------------------------------------------------------------- git

def repo_dir(root, repo_path):
    return (root / (repo_path or ".")).resolve()


def git(cwd, *args, timeout=15):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return p.stdout


def file_at(root, repo_path, sha, path):
    if not HEX.match(sha or ""):
        raise RuntimeError("commit is not a hex hash")
    if path.startswith("-") or "\x00" in path:
        raise RuntimeError("bad path")
    return git(repo_dir(root, repo_path), "show", f"{sha}:{path}").decode("utf-8", "replace")


def location(con, loc_id):
    return one(con, """SELECT fl.*, rp.path AS repo_path, rp.name AS repo_name, rp.permalink_template
                       FROM finding_locations fl JOIN repositories rp ON rp.id = fl.repository_id
                       WHERE fl.id = ?""", (loc_id,))


def source(root, con, loc_id, context=8):
    loc = location(con, loc_id)
    if not loc:
        return {"error": "no such reference"}
    out = {k: loc[k] for k in ("path", "commit_sha", "line_start", "line_end", "symbol",
                               "excerpt", "permalink", "repo_name", "blob_sha")}
    a, b = loc["line_start"], loc["line_end"] or loc["line_start"]
    out["git_command"] = (f"git -C {loc['repo_path'] or '.'} show {loc['commit_sha']}:{loc['path']}"
                          + (f" | sed -n '{a},{b}p'" if a else ""))
    try:
        text = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"]).splitlines()
        lo = max(1, (a or 1) - context)
        hi = min(len(text), (b or a or min(len(text), 40)) + context)
        out["lines"] = [{"n": i, "text": text[i - 1]} for i in range(lo, hi + 1)]
        out["source"] = "git"
        if loc["excerpt"] is not None and a:
            out["excerpt_matches"] = "\n".join(text[a - 1:b]) == loc["excerpt"].rstrip("\n")
    except Exception as e:  # history rewritten, repo missing, or not a git checkout
        out["git_error"] = str(e)
        out["source"] = "excerpt" if loc["excerpt"] is not None else "none"
        if loc["excerpt"] is not None and a:
            out["lines"] = [{"n": a + i, "text": t} for i, t in enumerate(loc["excerpt"].split("\n"))]
    return out


def fill_refs(root, con, review_code=None):
    sql = """SELECT fl.id FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
             JOIN reviews r ON r.id = f.review_id
             WHERE fl.commit_sha IS NOT NULL AND (fl.excerpt IS NULL OR fl.blob_sha IS NULL
                   OR fl.permalink IS NULL OR (fl.line_start IS NULL AND fl.symbol IS NOT NULL))"""
    args = ()
    if review_code:
        sql += " AND r.code = ?"; args = (review_code,)
    done = failed = 0
    for (lid,) in con.execute(sql, args).fetchall():
        loc = location(con, lid)
        try:
            text = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"])
        except Exception as e:
            print(f"ref {lid} {loc['path']}@{loc['commit_sha'][:10]}: {e}"); failed += 1; continue
        lines = text.splitlines()
        a, b = loc["line_start"], loc["line_end"]
        if a is None and loc["symbol"]:
            for i, t in enumerate(lines, 1):
                if loc["symbol"] in t:
                    a = b = i; break
        upd = {}
        if a:
            b = b or a
            excerpt = "\n".join(lines[a - 1:b])
            upd.update(line_start=a, line_end=b)
            if loc["excerpt"] is None:
                upd.update(excerpt=excerpt, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest())
        if loc["blob_sha"] is None:
            try:
                upd["blob_sha"] = git(repo_dir(root, loc["repo_path"]), "rev-parse",
                                      f"{loc['commit_sha']}:{loc['path']}").decode().strip()
            except Exception:
                pass
        if loc["permalink"] is None and loc["permalink_template"]:
            upd["permalink"] = (loc["permalink_template"].replace("{sha}", loc["commit_sha"])
                                .replace("{path}", loc["path"]).replace("{start}", str(a or 1))
                                .replace("{end}", str(b or a or 1)))
        if upd:
            con.execute(f"UPDATE finding_locations SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                        (*upd.values(), lid))
            done += 1
    con.commit()
    print(f"filled {done} references, {failed} could not be read from git")
    return failed


def tag_review(root, con, code):
    rv = one(con, "SELECT id FROM reviews WHERE code=?", (code,))
    if not rv:
        sys.exit(f"no review {code}")
    tag = f"review/{code}"
    for r in rows(con, """SELECT rr.repository_id, rr.commit_sha, rp.path, rp.name FROM review_repositories rr
                          JOIN repositories rp ON rp.id = rr.repository_id WHERE rr.review_id=?""", (rv["id"],)):
        d = repo_dir(root, r["path"])
        try:
            existing = git(d, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}").decode().strip()
        except RuntimeError:
            existing = None
        if existing and existing != r["commit_sha"]:
            print(f"{r['name']}: tag {tag} already points at {existing[:10]}; left alone"); continue
        if not existing:
            git(d, "tag", "-a", tag, r["commit_sha"], "-m", f"Commit reviewed in {code}")
        con.execute("UPDATE review_repositories SET tag_name=? WHERE review_id=? AND repository_id=?",
                    (tag, rv["id"], r["repository_id"]))
        print(f"{r['name']}: {tag} -> {r['commit_sha'][:10]}  (push with: git -C {r['path'] or '.'} push origin {tag})")
    con.commit()


def dump(con, db):
    out = db.with_suffix(".sql")
    with open(out, "w", encoding="utf-8") as f:
        for line in con.iterdump():
            f.write(line + "\n")
    print(f"wrote {out}")


# ---------------------------------------------------------------- viewer API

LABEL_TABLES = ("domains", "themes")


def api(root, db, path, qs, body):
    con = connect(db)
    try:
        return route(root, db, con, path, qs, body)
    finally:
        con.close()


def route(root, db, con, path, qs, body):
    p = [x for x in path.split("/") if x][1:]  # drop 'api'
    arg = lambda k, d=None: qs.get(k, [d])[0]

    if p == ["overview"]:
        meta = {r["key"]: r["value"] for r in rows(con, "SELECT key, value FROM meta")}
        return {
            "project": meta.get("project_name", root.name),
            "schema_version": meta.get("schema_version"),
            "open_by_severity": rows(con, """SELECT severity, count(*) n FROM issues
                WHERE status IN ('open','needs-decision','planned','in-progress','regressed') GROUP BY severity"""),
            "issues_by_status": rows(con, "SELECT status, count(*) n FROM issues GROUP BY status"),
            "gates_open": one(con, "SELECT count(*) n FROM gates WHERE status='open'")["n"],
            "queue": one(con, "SELECT count(*) n FROM v_owner_queue")["n"],
            "reviews": rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
                (SELECT count(*) FROM findings f WHERE f.review_id=r.id) points,
                (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='H') high
                FROM reviews r ORDER BY r.started_at DESC LIMIT 5"""),
            "plans": rows(con, """SELECT p.code, p.title, p.status, count(s.id) steps,
                sum(s.status='done') done, sum(s.status='waiting-owner') waiting
                FROM plans p LEFT JOIN steps s ON s.plan_id=p.id GROUP BY p.id ORDER BY p.created_at DESC"""),
            "recurring": one(con, "SELECT count(*) n FROM v_recurring_issues")["n"],
            "checks": [{"label": label, "view": v, "n": con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]}
                       for v, label in MUST_BE_EMPTY],
            "labels": {t: {r["code"]: r["name"] for r in rows(con, f"SELECT code, name FROM {t}")} for t in LABEL_TABLES},
        }

    if p == ["gates"]:
        st = arg("status", "pending")
        where = {"pending": "g.status IN ('open','answered')", "all": "1"}.get(st, "g.status = ?")
        args = () if st in ("pending", "all") else (st,)
        gates = rows(con, f"""SELECT g.*, r.code review_code, pl.code plan_code, s.number step_number,
                i.code issue_code, d.code decision_code FROM gates g
                LEFT JOIN reviews r ON r.id=g.review_id LEFT JOIN plans pl ON pl.id=g.plan_id
                LEFT JOIN steps s ON s.id=g.step_id LEFT JOIN issues i ON i.id=g.issue_id
                LEFT JOIN decisions d ON d.id=g.decision_id
                WHERE {where} ORDER BY g.status='answered', g.blocking DESC, g.created_at""", args)
        for g in gates:
            g["options"] = json.loads(g["options"]) if g["options"] else []
        other = rows(con, "SELECT * FROM v_owner_queue WHERE kind <> 'gate'")
        return {"gates": gates, "other": other}

    if len(p) == 3 and p[0] == "gates" and p[2] == "answer" and body is not None:
        g = one(con, "SELECT * FROM gates WHERE code=?", (p[1],))
        if not g:
            return {"error": "no such gate"}, 404
        if g["status"] != "open":
            return {"error": "This question was already answered."}, 409
        val = (body.get("answer_value") or "").strip() or None
        txt = (body.get("answer_text") or "").strip() or None
        opts = [o.get("value") for o in json.loads(g["options"] or "[]")]
        if val is not None and val not in opts:
            return {"error": "That choice is not one of the options."}, 400
        if val is None and not txt:
            return {"error": "Choose an option or write an answer."}, 400
        if txt and not g["allow_text"] and val is None:
            return {"error": "Choose one of the options."}, 400
        con.execute("""UPDATE gates SET status='answered', answer_value=?, answer_text=?,
                       answered_by='owner', answered_via='viewer' WHERE id=? AND status='open'""",
                    (val, txt, g["id"]))
        con.commit()
        return {"ok": True}

    if p == ["reviews"]:
        return rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
            (SELECT group_concat(rp.name || ' ' || substr(rr.commit_sha,1,10) || COALESCE(' on ' || rr.branch, ''), ' · ')
               FROM review_repositories rr JOIN repositories rp ON rp.id=rr.repository_id WHERE rr.review_id=r.id) commits,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id) points,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='H') high,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='M') medium,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='L') low,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type='new') new_issues,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN ('recurrence','regression')) came_back
            FROM reviews r ORDER BY r.started_at DESC""")

    if len(p) == 2 and p[0] == "review":
        r = one(con, """SELECT r.*, m.name manager, m.model manager_model, a.name approved_by,
                        pr.code previous_code FROM reviews r
                        LEFT JOIN actors m ON m.id=r.manager_actor_id LEFT JOIN actors a ON a.id=r.approved_by_actor_id
                        LEFT JOIN reviews pr ON pr.id=r.previous_review_id WHERE r.code=?""", (p[1],))
        if not r:
            return {"error": "no such review"}, 404
        rid = r["id"]
        findings = rows(con, """SELECT f.*, i.code issue_code, i.status issue_status, rr.label run_label
            FROM findings f LEFT JOIN issues i ON i.id=f.issue_id LEFT JOIN reviewer_runs rr ON rr.id=f.reviewer_run_id
            WHERE f.review_id=? ORDER BY CASE f.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, f.point_id""", (rid,))
        locs = rows(con, """SELECT fl.id, fl.finding_id, fl.path, fl.line_start, fl.line_end, fl.symbol, fl.role,
            fl.commit_sha, fl.permalink, rp.name repo FROM finding_locations fl
            JOIN findings f ON f.id=fl.finding_id JOIN repositories rp ON rp.id=fl.repository_id WHERE f.review_id=?""", (rid,))
        by = {}
        for l in locs:
            by.setdefault(l["finding_id"], []).append(l)
        for f in findings:
            f["locations"] = by.get(f["id"], [])
        return {
            "review": r,
            "repos": rows(con, """SELECT rp.name, rr.* FROM review_repositories rr JOIN repositories rp
                                  ON rp.id=rr.repository_id WHERE rr.review_id=?""", (rid,)),
            "goals": rows(con, """SELECT g.code, g.text FROM review_goals rg JOIN design_goals g ON g.id=rg.goal_id
                                  WHERE rg.review_id=? ORDER BY g.code""", (rid,)),
            "domains": rows(con, """SELECT rd.*, d.name, d.category FROM review_domains rd JOIN domains d ON d.code=rd.domain_code
                                    WHERE rd.review_id=? ORDER BY rd.decision, d.category, d.name""", (rid,)),
            "runs": rows(con, """SELECT rr.*, a.name actor, a.model FROM reviewer_runs rr JOIN actors a ON a.id=rr.actor_id
                                 WHERE rr.review_id=? ORDER BY rr.domain_code, rr.label""", (rid,)),
            "checks": rows(con, """SELECT c.outcome, c.evidence, i.code issue_code, i.title, i.status
                                   FROM review_issue_checks c JOIN issues i ON i.id=c.issue_id WHERE c.review_id=?
                                   ORDER BY c.outcome, i.code""", (rid,)),
            "summary": rows(con, "SELECT * FROM v_review_summary WHERE review=?", (p[1],)),
            "metrics": rows(con, "SELECT name, scope, value, unit, method, note FROM metrics WHERE review_id=? ORDER BY name, scope", (rid,)),
            "artifacts": rows(con, "SELECT kind, path, url FROM artifacts WHERE review_id=? ORDER BY kind", (rid,)),
            "gates": rows(con, "SELECT code, kind, title, status, answer_value, answer_text FROM gates WHERE review_id=? ORDER BY created_at", (rid,)),
            "findings": findings,
        }

    if p == ["issues"]:
        where, args = ["1"], []
        for k in ("status", "severity", "domain_code"):
            v = arg(k)
            if v == "active" and k == "status":
                where.append("i.status IN ('open','needs-decision','planned','in-progress','regressed')")
            elif v:
                where.append(f"i.{k} = ?"); args.append(v)
        q = arg("q")
        if q:
            where.append("(i.code LIKE ? OR i.title LIKE ? OR i.sub LIKE ? OR EXISTS (SELECT 1 FROM findings f LEFT JOIN finding_locations fl ON fl.finding_id=f.id WHERE f.issue_id=i.id AND (f.point_id LIKE ? OR f.summary LIKE ? OR fl.path LIKE ?)))")
            args += [f"%{q}%"] * 6
        return rows(con, f"""SELECT i.code, i.title, i.severity, i.status, i.domain_code, i.theme_code, i.times_seen,
            i.opened_at, i.updated_at, fr.code first_seen, lr.code last_seen
            FROM issues i JOIN reviews fr ON fr.id=i.first_seen_review_id LEFT JOIN reviews lr ON lr.id=i.last_seen_review_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, i.code LIMIT 2000""", args)

    if len(p) == 2 and p[0] == "issue":
        i = one(con, """SELECT i.*, fr.code first_seen, lr.code last_seen, cr.code last_checked FROM issues i
                        JOIN reviews fr ON fr.id=i.first_seen_review_id LEFT JOIN reviews lr ON lr.id=i.last_seen_review_id
                        LEFT JOIN reviews cr ON cr.id=i.last_checked_review_id WHERE i.code=?""", (p[1],))
        if not i:
            return {"error": "no such issue"}, 404
        iid = i["id"]
        findings = rows(con, """SELECT f.*, r.code review_code, r.started_at review_at FROM findings f
                                JOIN reviews r ON r.id=f.review_id WHERE f.issue_id=? ORDER BY r.started_at""", (iid,))
        for f in findings:
            f["locations"] = rows(con, """SELECT fl.id, fl.path, fl.line_start, fl.line_end, fl.symbol, fl.role, fl.commit_sha,
                fl.permalink, rp.name repo FROM finding_locations fl JOIN repositories rp ON rp.id=fl.repository_id
                WHERE fl.finding_id=?""", (f["id"],))
        return {
            "issue": i,
            "findings": findings,
            "relations": rows(con, """SELECT 'out' dir, r.relation, b.code other, b.title other_title, b.status other_status,
                    r.confidence, r.rationale, rv.code review FROM issue_relations r JOIN issues b ON b.id=r.to_issue_id
                    LEFT JOIN reviews rv ON rv.id=r.established_in_review_id WHERE r.from_issue_id=?
                UNION ALL SELECT 'in', r.relation, a.code, a.title, a.status, r.confidence, r.rationale, rv.code
                    FROM issue_relations r JOIN issues a ON a.id=r.from_issue_id
                    LEFT JOIN reviews rv ON rv.id=r.established_in_review_id WHERE r.to_issue_id=?""", (iid, iid)),
            "checks": rows(con, """SELECT r.code review, r.started_at, c.outcome, c.evidence FROM review_issue_checks c
                                   JOIN reviews r ON r.id=c.review_id WHERE c.issue_id=? ORDER BY r.started_at""", (iid,)),
            "timeline": rows(con, "SELECT * FROM v_issue_timeline WHERE code=?", (p[1],)),
            "guard_tests": rows(con, "SELECT * FROM guard_tests WHERE issue_id=?", (iid,)),
            "decisions": rows(con, """SELECT d.code, d.question, d.answer, d.rationale, d.status, d.decided_at
                                      FROM decision_issues di JOIN decisions d ON d.id=di.decision_id WHERE di.issue_id=?""", (iid,)),
            "steps": rows(con, """SELECT DISTINCT p.code plan, s.number, s.title, s.status, s.progress_note,
                    (SELECT group_concat(substr(sc.commit_sha,1,10), ', ') FROM step_commits sc WHERE sc.step_id=s.id) commits
                    FROM step_findings sf JOIN steps s ON s.id=sf.step_id JOIN plans p ON p.id=s.plan_id
                    JOIN findings f ON f.id=sf.finding_id WHERE f.issue_id=? ORDER BY p.code, s.number""", (iid,)),
        }

    if p == ["plans"]:
        return rows(con, """SELECT p.code, p.title, p.status, p.created_at, p.approved_at, count(s.id) steps,
            sum(s.status='done') done, sum(s.status='doing') doing, sum(s.status='waiting-owner') waiting,
            (SELECT group_concat(r.code, ', ') FROM plan_reviews pr JOIN reviews r ON r.id=pr.review_id WHERE pr.plan_id=p.id) reviews
            FROM plans p LEFT JOIN steps s ON s.plan_id=p.id GROUP BY p.id ORDER BY p.created_at DESC""")

    if len(p) == 2 and p[0] == "plan":
        pl = one(con, "SELECT * FROM plans WHERE code=?", (p[1],))
        if not pl:
            return {"error": "no such plan"}, 404
        pid = pl["id"]
        steps = rows(con, """SELECT s.*, ph.number phase FROM steps s JOIN phases ph ON ph.id=s.phase_id
                             WHERE s.plan_id=? ORDER BY s.number""", (pid,))
        deps, closes, commits = {}, {}, {}
        for d in rows(con, """SELECT sd.step_id, s.number FROM step_dependencies sd JOIN steps s ON s.id=sd.depends_on_step_id
                              WHERE s.plan_id=?""", (pid,)):
            deps.setdefault(d["step_id"], []).append(d["number"])
        for c in rows(con, """SELECT sf.step_id, f.point_id, f.severity, f.summary, r.code review, i.code issue
                              FROM step_findings sf JOIN steps s ON s.id=sf.step_id JOIN findings f ON f.id=sf.finding_id
                              JOIN reviews r ON r.id=f.review_id LEFT JOIN issues i ON i.id=f.issue_id WHERE s.plan_id=?""", (pid,)):
            closes.setdefault(c["step_id"], []).append(c)
        for c in rows(con, """SELECT sc.step_id, sc.commit_sha, sc.branch, sc.pr_url, sc.summary FROM step_commits sc
                              JOIN steps s ON s.id=sc.step_id WHERE s.plan_id=?""", (pid,)):
            commits.setdefault(c["step_id"], []).append(c)
        for s in steps:
            s["depends"] = sorted(deps.get(s["id"], []))
            s["closes"] = closes.get(s["id"], [])
            s["commits"] = commits.get(s["id"], [])
        return {"plan": pl,
                "phases": rows(con, "SELECT * FROM v_plan_progress WHERE plan=?", (p[1],)),
                "phase_rows": rows(con, "SELECT number, title, goal, why FROM phases WHERE plan_id=? ORDER BY number", (pid,)),
                "steps": steps}

    if p == ["trends"]:
        return {"metrics": rows(con, "SELECT * FROM v_metric_trends"),
                "reviews": rows(con, """SELECT review, min(started_at) started_at, sum(points) points, sum(high) high,
                    sum(medium) medium, sum(low) low, sum(new_issues) new_issues, sum(carried_over) carried_over,
                    sum(came_back) came_back, sum(false_findings) false_findings
                    FROM v_review_summary GROUP BY review ORDER BY started_at"""),
                "recurring": rows(con, "SELECT * FROM v_recurring_issues LIMIT 50"),
                "hotspots": rows(con, "SELECT * FROM v_file_hotspots LIMIT 30")}

    if p == ["tables"]:
        out = []
        for t in rows(con, "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name"):
            cols = [c["name"] for c in rows(con, f"PRAGMA table_info({t['name']})")]
            n = con.execute(f'SELECT count(*) FROM "{t["name"]}"').fetchone()[0]
            out.append({**t, "columns": cols, "count": n})
        return out

    if len(p) == 2 and p[0] == "table":
        name = p[1]
        if not NAME.match(name) or not one(con, "SELECT 1 x FROM sqlite_master WHERE name=? AND type IN ('table','view')", (name,)):
            return {"error": "no such table"}, 404
        limit = max(1, min(500, int(arg("limit", "100"))))
        offset = max(0, int(arg("offset", "0")))
        cur = con.execute(f'SELECT * FROM "{name}" LIMIT ? OFFSET ?', (limit, offset))
        return {"columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()],
                "count": con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0], "offset": offset, "limit": limit}

    if p == ["sql"] and body is not None:
        return run_readonly(db, body.get("sql", ""))

    if len(p) == 2 and p[0] == "source":
        return source(root, con, int(p[1]))

    return {"error": "not found"}, 404


def run_readonly(db, sql):
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return {"error": "Write a query first."}, 400
    con = connect(db, readonly=True)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    con.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        data = cur.fetchmany(1001)
        return {"columns": cols, "rows": [list(r) for r in data[:1000]], "truncated": len(data) > 1000}
    except (sqlite3.Error, sqlite3.Warning) as e:  # before Python 3.11, several statements raise sqlite3.Warning
        return {"error": f"The query did not run: {e}"}, 400
    finally:
        con.close()


# ---------------------------------------------------------------- HTTP

def make_server(root, db, port=0):
    """Build the viewer's handler and bind it to 127.0.0.1:port (0 = any free port); returns (server, token).
    The caller runs server.serve_forever(). The Host allow-list uses the port actually bound."""
    token = secrets.token_urlsafe(24)
    page = PAGE.replace("@@TOKEN@@", token)
    hosts = set()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def guard(self):
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "wrong host"}); return False
            u = urlparse(self.path)
            if u.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Ledger-Token", ""), token):
                self.send(403, {"error": "missing token; reload the page"}); return False
            return True

        def handle_api(self, body):
            u = urlparse(self.path)
            try:
                res = api(root, db, u.path, parse_qs(u.query), body)
            except Exception as e:
                res = ({"error": f"Something went wrong reading the ledger: {e}"}, 500)
            code, res = (res[1], res[0]) if isinstance(res, tuple) else (200, res)
            self.send(code, res)

        def do_GET(self):
            if not self.guard():
                return
            u = urlparse(self.path)
            if u.path == "/":
                self.send(200, page.encode(), "text/html; charset=utf-8")
            elif u.path.startswith("/api/"):
                self.handle_api(None)
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.guard():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "JSON only"})
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 200_000:
                return self.send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad JSON"})
            self.handle_api(body if isinstance(body, dict) else {})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    bound = srv.server_address[1]
    hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    return srv, token


def serve(root, db, port, open_browser):
    srv, _ = make_server(root, db, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Ledger viewer for {db} at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765); s.add_argument("--no-open", action="store_true")
    sub.add_parser("check")
    s = sub.add_parser("fill-refs"); s.add_argument("review", nargs="?")
    s = sub.add_parser("tag-review"); s.add_argument("review")
    s = sub.add_parser("show"); s.add_argument("location", type=int)
    sub.add_parser("gates")
    sub.add_parser("dump")
    sub.add_parser("schema")
    a = ap.parse_args()
    root = find_root(Path(a.root).resolve(), explicit=a.root != ".")
    if a.cmd == "init":
        print(f"project root: {root}")
    db = Path(a.db).resolve() if a.db else root / ".code-review" / "ledger.sqlite"
    if a.cmd == "schema":
        print(SCHEMA); return
    if a.cmd == "init":
        init(root, db); return
    if not db.exists():
        sys.exit(f"no ledger at {db}; run: python3 ledger.py init")
    con = connect(db)
    if a.cmd == "serve":
        con.close(); serve(root, db, a.port, not a.no_open)
    elif a.cmd == "check":
        sys.exit(1 if check(con) else 0)
    elif a.cmd == "fill-refs":
        sys.exit(1 if fill_refs(root, con, a.review) else 0)
    elif a.cmd == "tag-review":
        tag_review(root, con, a.review)
    elif a.cmd == "show":
        s = source(root, con, a.location, context=0)
        if s.get("error"):
            sys.exit(s["error"])
        print(f"# {s['repo_name']} {s['path']} @ {s['commit_sha']} (from {s['source']})")
        if s.get("git_error"):
            print(f"# git: {s['git_error']}")
        for l in s.get("lines", []):
            print(f"{l['n']:6d}  {l['text']}")
    elif a.cmd == "gates":
        for g in rows(con, "SELECT * FROM gates WHERE status IN ('open','answered') ORDER BY created_at"):
            ans = f" -> {g['answer_value'] or ''} {g['answer_text'] or ''}".rstrip() if g["status"] == "answered" else ""
            print(f"{g['code']}  [{g['status']}] {g['kind']}: {g['title']}{ans}")
    elif a.cmd == "dump":
        dump(con, db)


if __name__ == "__main__":
    main()
