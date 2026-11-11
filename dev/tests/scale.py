"""Large-ledger generator for scale and performance tests, and the baseline measurements built on it.

    python3 dev/tests/scale.py build DIR [--scale N] [--reviews R] [--files F] [--reports DIR] [--json FILE]
    python3 dev/tests/scale.py baseline [--reports DIR] [--scales 1,10] [--out FILE] [--work DIR]

build     DIR (missing or empty) becomes a git project of F source files with R commits, and a ledger written the way
          an agent writes one (SKILL.md: one transaction per report, then matching, checks, a plan and its progress),
          then every review tagged so `check` passes. Prints the timing of each write phase.
          Scale 1 = 5 reviews x 2,000 findings (1-3 locations each), about 4,000 issues, a 600-step plan, 200 gates
          (the shape of review R-0005 and plan P-0001). Scale 10 is ten times each count.
baseline  Builds the scales asked for in a temporary folder (or --work) and measures: ledger and dump size and their
          gzip ratio, the size and gzip ratio of the viewer's /api/review and /api/plan payloads, and git object
          growth over five consecutive ledger commits at the first scale. With --reports, the text in the ledger is
          real report prose (sentences from the *.md files there) instead of generated words.

Library use (tests):  from dev.tests import scale;  info = scale.build(proj, scale=1)
Deterministic: the same arguments give the same rows (random.Random(seed)); git shas also match across runs.
Standard library only; Python 3.9+.
"""
import argparse
import gzip
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dev.tests import fixtures  # noqa: E402

KINDS = ["defect", "test", "validation", "refactor", "feature", "design", "docs", "logging", "perf", "security",
         "ops", "verify"]
WORDS = ("the server reads ledger finding review issue step plan gate token page route query index view trigger "
         "commit branch path symbol line excerpt owner agent reviewer status severity history event check match "
         "returns writes without with before after every each when only never always row column table json").split()
TEMPLATE = "https://github.com/example/proj/blob/{sha}/{path}#L{start}-L{end}"


class Prose:
    """Text for the ledger's prose columns: random words from a 46-word vocabulary (default), or consecutive
    sentences of real report prose (from_reports). text(n) returns about n words."""

    def __init__(self, rnd, sentences=None):
        self.rnd = rnd
        self.sentences = sentences

    @classmethod
    def from_reports(cls, rnd, folder):
        sentences = []
        for f in sorted(Path(folder).glob("*.md")):
            body = re.sub(r"\s+", " ", f.read_text(encoding="utf-8", errors="replace"))
            sentences += [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) >= 3]
        if not sentences:
            raise SystemExit(f"no report prose found in {folder}")
        return cls(rnd, sentences)

    @property
    def kind(self):
        return "report prose" if self.sentences else "generated words"

    def text(self, n_words):
        if not self.sentences:
            return " ".join(self.rnd.choice(WORDS) for _ in range(n_words)).capitalize() + "."
        i = self.rnd.randrange(len(self.sentences))
        out, n = [], 0
        while n < n_words:
            s = self.sentences[i % len(self.sentences)]
            out.append(s)
            n += len(s.split())
            i += 1
        return " ".join(out)


def _ts(day, sec=0):
    return (f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}T{(sec // 3600) % 24:02d}:{(sec // 60) % 60:02d}:"
            f"{sec % 60:02d}.000Z")


class _Timer:
    def __init__(self, verbose):
        self.t = {}
        self.verbose = verbose

    def add(self, name, seconds, n=None):
        e = self.t.setdefault(name, {"s": 0.0, "n": 0})
        e["s"] += seconds
        e["n"] += n or 0

    def report(self):
        out = {}
        for k, e in self.t.items():
            out[k] = {"s": round(e["s"], 4)}
            if e["n"]:
                out[k].update(n=e["n"], us_per=round(e["s"] / e["n"] * 1e6, 1))
        if self.verbose:
            for k, e in out.items():
                extra = f"  n={e['n']:<7d} {e['us_per']:9.1f} us each" if "n" in e else ""
                print(f"{k:34s} {e['s']:8.3f} s{extra}", flush=True)
        return out


def _git(proj, *args, env=None):
    p = subprocess.run(["git", "-C", str(proj), *args], capture_output=True, env=env or fixtures.git_env())
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace')}")
    return p.stdout.decode().strip()


def _dated(n):
    stamp = f"@{fixtures.ScratchRepo.EPOCH + 60 * n} +0000"
    return fixtures.git_env({"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})


def build(proj, scale=1, reviews=5, files=300, reports=None, ledger_py=None, seed=42, plan=True, tag=True,
          findings=None, steps=None, verbose=False):
    """Build a large ledger in proj (missing or empty). Returns {"timings", "counts", "ledger_bytes", "db", "prose",
    "per_review", "shas"}.

    scale    multiplies findings per review (2,000), the plan's steps (600), gates (40 per review) and decisions
    reviews  number of reviews (and of source commits)
    reports  a folder of *.md reports: use their sentences as the prose (default: generated words)
    findings findings per review (default 2,000 x scale); steps: plan steps (default 600 x scale). For quick tests.
    plan     False: stop after the reviews (no plan, steps or progress)
    tag      False: do not run tag-review (v_untraceable_refs is then not empty)
    """
    proj = Path(proj)
    if proj.exists() and any(proj.iterdir()):
        raise ValueError(f"{proj} is not empty")
    proj.mkdir(parents=True, exist_ok=True)
    ledger_py = Path(ledger_py) if ledger_py else fixtures.built_ledger_path()
    rnd = random.Random(seed)
    prose = Prose.from_reports(rnd, reports) if reports else Prose(rnd)
    text = prose.text
    S, R = scale, reviews
    per_review = findings or 2000 * S
    new_first, new_later = int(per_review * 0.8), int(per_review * 0.3)
    n_steps, gates_per_review = steps or 600 * S, 40 * S
    T = _Timer(verbose)

    # ------------------------------------------------ git project
    t0 = time.perf_counter()
    _git(proj, "init", "-q", "-b", "main")
    with open(proj / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[user]\n\tname = {fixtures.GIT_NAME}\n\temail = {fixtures.GIT_EMAIL}\n"
                "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n[gc]\n\tauto = 0\n")
    paths = [f"src/mod{i // 20:02d}/file{i:03d}.py" for i in range(files)]
    contents = {}
    for i, p in enumerate(paths):
        contents[p] = [f"def func_{i}_{ln // 25}(value):" if ln % 25 == 1
                       else f"    x_{ln} = compute_{ln % 17}(value, {ln})  # file {i} rev 0" for ln in range(1, 401)]
    snaps = []
    for k in range(R):
        if k:
            for p in rnd.sample(paths, max(1, files // 10)):
                ls = contents[p][:]
                for ln in rnd.sample(range(400), 20):
                    if not ls[ln].startswith("def "):
                        ls[ln] = ls[ln].rsplit("rev", 1)[0] + f"rev {k}"
                contents[p] = ls
        for p in paths:
            fp = proj / p
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("\n".join(contents[p]) + "\n", encoding="utf-8")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-q", "--no-verify", "-m", f"commit {k}", env=_dated(k + 1))
        snaps.append((_git(proj, "rev-parse", "HEAD"), dict(contents)))
    T.add("git_repo_build", time.perf_counter() - t0)

    # ------------------------------------------------ ledger
    p = fixtures.run_ledger("--root", proj, "init", ledger_py=ledger_py)
    if p.returncode != 0:
        raise RuntimeError(f"ledger init failed:\n{p.stdout}\n{p.stderr}")
    db = proj / ".code-review" / "ledger.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA foreign_keys=ON")
    con.isolation_level = None  # explicit BEGIN/COMMIT, as an agent writing one transaction per report would
    x = con.execute

    def lastid():
        return x("SELECT last_insert_rowid()").fetchone()[0]

    domains = [r[0] for r in x("SELECT code FROM domains ORDER BY rowid")]
    themes = [r[0] for r in x("SELECT code FROM themes ORDER BY rowid")]

    x("BEGIN")
    x("INSERT INTO repositories(name,path,default_branch,permalink_template) VALUES ('proj','.','main',?)",
      (TEMPLATE,))
    x("INSERT INTO actors(kind,name) VALUES ('owner','Owner')")
    owner = lastid()
    for j in range(8):
        x("INSERT INTO design_goals(code,text,source) VALUES (?,?,'owner')", (f"G-{j + 1:02d}", text(14)))
    for j in range(12):
        x("INSERT INTO requirements(code,text,source) VALUES (?,?,'owner chat')", (f"REQ-{j + 1:04d}", text(16)))
    x("COMMIT")

    next_iss = 1
    all_findings = []
    review_ids = []
    for k in range(R):
        sha, files_at = snaps[k]
        day = k * 40
        x("BEGIN")
        x("INSERT INTO actors(kind,name,model,harness) VALUES ('manager','Review manager','model-x','harness-x')")
        mgr = lastid()
        x("""INSERT INTO reviews(code,title,scope,status,request_text,requested_by_actor_id,manager_actor_id,
             approved_by_actor_id,previous_review_id,started_at) VALUES (?,?,?,'planning',?,?,?,?,?,?)""",
          (f"R-{k + 1:04d}", f"Full review {k + 1}", "full" if k == 0 else "second", text(30), owner, mgr, owner,
           review_ids[-1] if review_ids else None, _ts(day)))
        rid = lastid()
        review_ids.append(rid)
        for st in ("awaiting-approval", "approved", "running", "verifying", "indexing"):
            # schema 3: a review past awaiting-approval has approved_at
            x("UPDATE reviews SET status=?, approved_at=CASE WHEN ?='approved' THEN ? ELSE approved_at END WHERE id=?",
              (st, st, _ts(day, 60), rid))
        x("INSERT INTO review_repositories(review_id,repository_id,commit_sha,branch,commit_subject) "
          "VALUES (?,1,?,'main',?)", (rid, sha, f"commit {k}"))
        for j in range(1, 9):
            x("INSERT INTO review_goals(review_id,goal_id) VALUES (?,?)", (rid, j))
        inc = domains[:20]
        for d in domains:
            x("INSERT INTO review_domains(review_id,domain_code,decision,reason) VALUES (?,?,?,?)",
              (rid, d, "included" if d in inc else "skipped", text(10)))
        runs = []
        for j in range(12):
            x("INSERT INTO actors(kind,name,model,parent_actor_id) VALUES ('reviewer',?,'model-x',?)",
              (f"rev {j}", mgr))
            act = lastid()
            x("""INSERT INTO reviewer_runs(review_id,domain_code,actor_id,label,scope,prompt,report_path,status,
                 items_reviewed,high_count,medium_count,low_count,final_message)
                 VALUES (?,?,?,?,?,?,?,'finished',120,10,40,80,?)""",
              (rid, inc[j], act, f"{inc[j]}-{j}", text(20), text(200), f"reports/{inc[j]}.md", text(60)))
            runs.append((lastid(), inc[j], act))
        x("COMMIT")

        # findings, one transaction per report (SKILL.md 5c); the remainder goes to the first runs
        fids = []
        for ri, (run_id, dom, act) in enumerate(runs):
            n_run = per_review // len(runs) + (1 if ri < per_review % len(runs) else 0)
            t0 = time.perf_counter()
            x("BEGIN")
            for j in range(n_run):
                sev = rnd.choices("HMLT", (1, 3, 5, 2))[0]
                th = rnd.choice(themes)
                sub = f"sub-{rnd.randrange(2000 * S)}"
                pid = f"{dom[:4].upper()}-{sev}{j + 1}-{run_id}"
                x("""INSERT INTO findings(review_id,reviewer_run_id,point_id,severity,kind,domain_code,theme_code,sub,
                     summary,detail,before_state,recommendation,recommended_test,evidence,where_report,fingerprint,
                     verification,verified_by_actor_id,verification_note,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (rid, run_id, pid, sev, (lambda k: "test" if sev == "T" else k)(rnd.choice(KINDS)), dom, th, sub, text(22), text(100), text(45), text(45),
                   text(22), text(45), f"{rnd.randrange(900)}-{rnd.randrange(900, 1000)}", f"{dom}:{th}:{sub}",
                   rnd.choices(["confirmed", "unverified", "partly", "false"], (6, 2, 1, 1))[0], mgr, text(12),
                   _ts(day, 3600)))
                fid = lastid()
                for li in range(rnd.choice((1, 1, 2, 2, 3))):
                    p = rnd.choice(paths)
                    a_ = rnd.randrange(1, 385)
                    b_ = a_ + rnd.randrange(0, 15)
                    ex = "\n".join(files_at[p][a_ - 1:b_])
                    data = ("\n".join(files_at[p]) + "\n").encode()
                    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
                    perma = (TEMPLATE.replace("{sha}", sha).replace("{path}", p).replace("{start}", str(a_))
                             .replace("{end}", str(b_)))
                    x("""INSERT INTO finding_locations(finding_id,repository_id,path,line_start,line_end,symbol,role,
                         excerpt,excerpt_sha256,blob_sha,permalink) VALUES (?,1,?,?,?,?,?,?,?,?,?)""",
                      (fid, p, a_, b_, f"func_{paths.index(p)}_{a_ // 25}",
                       "primary" if li == 0 else rnd.choice(("also", "test", "doc")), ex,
                       hashlib.sha256(ex.encode()).hexdigest(), blob, perma))
                x("INSERT INTO finding_goals(finding_id,goal_id) VALUES (?,?)", (fid, rnd.randrange(1, 9)))
                if rnd.random() < 0.2:
                    x("INSERT INTO finding_requirements(finding_id,requirement_id,verdict) VALUES (?,?,?)",
                      (fid, rnd.randrange(1, 13), rnd.choice(("matches", "differs", "partly", "missing"))))
                fids.append((fid, dom))
            x("INSERT INTO metrics(review_id,domain_code,name,scope,value,unit,method) "
              "VALUES (?,?,?,?,?,?,'measured')", (rid, dom, f"items_{dom}", "all", n_run, "count"))
            x("COMMIT")
            T.add("insert_findings", time.perf_counter() - t0, n_run)
        all_findings += [f for f, _ in fids]

        # a check for every known issue in a domain that ran (SKILL.md 5d)
        t0 = time.perf_counter()
        x("BEGIN")
        issues = [tuple(r) for r in x("SELECT id, domain_code, status FROM issues")]
        # every known issue, duplicate and obsolete ones too: a recurrence matched below may reopen them (schema 3)
        known = [i for i in issues if i[1] in inc]
        for (iid, dom, st) in known:
            outc = rnd.choices(["still-present", "fixed", "not-checked", "changed", "regressed"], (6, 2, 1, 1, 0.3))[0]
            x("INSERT INTO review_issue_checks(review_id,issue_id,outcome,evidence,actor_id) VALUES (?,?,?,?,?)",
              (rid, iid, outc, text(20), runs[0][2]))
        x("COMMIT")
        if known:
            T.add("insert_checks", time.perf_counter() - t0, len(known))

        # matching: new issues, then matches to existing ones, in one transaction
        nnew = new_first if k == 0 else new_later
        rnd.shuffle(fids)
        x("BEGIN")
        t0 = time.perf_counter()
        created = []
        for (fid, dom) in fids[:nnew]:
            code = f"ISS-{next_iss:04d}"   # canonical and sequential (schema 3)
            next_iss += 1
            sev = x("SELECT severity FROM findings WHERE id=?", (fid,)).fetchone()[0]
            x("""INSERT INTO issues(code,title,domain_code,theme_code,sub,kind,severity,fingerprint,before_state,
                 recommendation,first_seen_review_id,change_actor_id,opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (code, text(8), dom, rnd.choice(themes), f"s{next_iss}", rnd.choice(KINDS), sev, f"{dom}:x:{next_iss}",
               text(45), text(45), rid, mgr, _ts(day, 7200)))
            iid = lastid()
            x("UPDATE findings SET issue_id=?, match_type='new', match_confidence='certain', match_rationale=? "
              "WHERE id=?", (iid, text(15), fid))
            created.append((iid, dom, "open"))
        T.add("match_new_issue", time.perf_counter() - t0, nnew)
        # schema 3: a review that recorded an issue fixed cannot also raise it, and 'regression' is only for an
        # issue that was fixed or verified
        fixed_here = {r[0] for r in x("SELECT issue_id FROM review_issue_checks WHERE review_id=? AND outcome='fixed'",
                                      (rid,))}
        pool = [i for i in issues + created if i[0] not in fixed_here]
        t0 = time.perf_counter()
        for (fid, dom) in fids[nnew:]:
            iid = rnd.choice(pool)[0]
            mt = rnd.choices(["still-present", "changed", "recurrence", "regression", "continuation"],
                             (6, 2, 1, 1, 1))[0]
            st, first = x("SELECT status, first_seen_review_id FROM issues WHERE id=?", (iid,)).fetchone()
            if mt == "regression" and st not in ("fixed", "verified", "regressed"):
                mt = "recurrence"
            # schema 7 (v_match_problems): the type agrees with the issue
            if first == rid:
                mt = "new"                      # an issue this review opened
            elif mt == "continuation":
                mt = "changed"                  # a continuation would need its own issue and relation
            if mt in ("still-present", "changed") and st in ("fixed", "verified"):
                mt = "regression"
            if mt == "recurrence" and not x("SELECT 1 FROM events WHERE entity_type='issue' AND entity_id=? AND "
                                            "event IN ('opened','status-changed') AND to_value IN ('fixed','verified',"
                                            "'accepted-risk','wont-fix','duplicate','obsolete')", (iid,)).fetchone():
                mt = "still-present"
            x("UPDATE findings SET issue_id=?, match_type=?, match_confidence='likely', match_rationale=? WHERE id=?",
              (iid, mt, text(15), fid))
        T.add("match_existing_issue", time.perf_counter() - t0, len(fids) - nnew)
        x("COMMIT")

        # relations, about one per new issue
        t0 = time.perf_counter()
        n = 0
        x("BEGIN")
        allids = [r[0] for r in x("SELECT id FROM issues")]
        for (iid, dom, st) in created:
            other = rnd.choice(allids)
            if other == iid:
                continue
            try:
                x("""INSERT INTO issue_relations(from_issue_id,to_issue_id,relation,confidence,rationale,
                     established_in_review_id,established_by_actor_id) VALUES (?,?,?,?,?,?,?)""",
                  (iid, other, rnd.choice(["related-to", "continuation-of", "caused-by", "blocks", "duplicate-of"]),
                   "likely", text(15), rid, mgr))
                n += 1
            except sqlite3.IntegrityError:
                pass
        x("COMMIT")
        T.add("insert_relations", time.perf_counter() - t0, n)

        # metrics, decisions, gates; then the review is handed off
        x("BEGIN")
        for m in range(60):
            x("INSERT INTO metrics(review_id,domain_code,name,scope,value,unit,method) "
              "VALUES (?,?,?,?,?,?,'measured')", (rid, "performance", f"metric_{m}", "all", rnd.random() * 100, "ms"))
        for j in range(10 * S):
            x("INSERT INTO decisions(code,question,status,review_id,asked_by_actor_id,answer) VALUES (?,?,?,?,?,?)",
              (f"D-{k * 10 * S + j + 1:04d}", text(20), "open", rid, mgr, text(10)))
            did = lastid()
            x("INSERT INTO decision_issues(decision_id,issue_id) VALUES (?,?)", (did, rnd.choice(allids)))
            if rnd.random() < 0.7:
                x("UPDATE decisions SET status='decided', decided_by_actor_id=? WHERE id=?", (owner, did))
        for j in range(gates_per_review):
            opts = json.dumps([{"value": f"o{v}", "label": text(4), "description": text(12)} for v in range(3)])
            x("""INSERT INTO gates(code,kind,title,question,context,options,review_id,issue_id,opened_by_actor_id,
                 created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
              (f"GATE-{k * gates_per_review + j + 1:04d}",
               rnd.choice(["decision", "accept-risk", "confirm-false-finding", "other", "domain-approval"]),
               text(6), text(25), text(80), opts, rid, rnd.choice(allids), mgr, _ts(day, j)))
            gid = lastid()
            if k < R - 1 or rnd.random() < 0.8:
                x("UPDATE gates SET status='answered', answer_value='o1', answered_via='chat' WHERE id=?", (gid,))
                if rnd.random() < 0.9:
                    x("UPDATE gates SET status='applied', applied_note=? WHERE id=?", (text(10), gid))
        for st in ("consolidating", "handed-off"):
            x("UPDATE reviews SET status=? WHERE id=?", (st, rid))
        x("COMMIT")
        if verbose:
            print(f"review {k + 1}: {len(fids)} findings, {next_iss - 1} issues", flush=True)

    if plan:
        _build_plan(con, rnd, text, review_ids, all_findings, n_steps, snaps[-1][0], T)
    con.close()

    if tag:  # tag every review so v_untraceable_refs is empty
        t0 = time.perf_counter()
        for k in range(R):
            p = fixtures.run_ledger("--root", proj, "tag-review", f"R-{k + 1:04d}", ledger_py=ledger_py)
            if p.returncode != 0:
                raise RuntimeError(f"tag-review failed:\n{p.stdout}\n{p.stderr}")
        T.add("tag_review_all", time.perf_counter() - t0, R)

    con = sqlite3.connect(str(db))
    counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
              for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                      "ORDER BY name").fetchall()}
    con.close()
    return {"timings": T.report(), "counts": counts, "ledger_bytes": db.stat().st_size, "db": db, "root": proj,
            "prose": prose.kind, "per_review": per_review, "shas": [s for s, _ in snaps]}


def _build_plan(con, rnd, text, review_ids, all_findings, n_steps, head_sha, T):
    x = con.execute

    def lastid():
        return x("SELECT last_insert_rowid()").fetchone()[0]

    t0 = time.perf_counter()
    x("BEGIN")
    owner = x("SELECT min(id) FROM actors WHERE kind='owner'").fetchone()[0]
    agent = x("SELECT min(id) FROM actors WHERE kind='manager'").fetchone()[0]
    x("INSERT INTO plans(code,title,status,approved_by_actor_id) VALUES ('P-0001','Fix plan','drafting',?)", (owner,))
    plan_id = lastid()
    for rid in review_ids:
        x("INSERT INTO plan_reviews(plan_id,review_id) VALUES (?,?)", (plan_id, rid))
    n_phases = 12
    for ph in range(1, n_phases + 1):
        x("INSERT INTO phases(plan_id,number,title,goal,why) VALUES (?,?,?,?,?)",
          (plan_id, ph, text(4), text(15), text(25)))
    ph_ids = [r[0] for r in x("SELECT id FROM phases WHERE plan_id=? ORDER BY number", (plan_id,))]
    step_ids = []
    for s in range(1, n_steps + 1):
        ph = ph_ids[min(n_phases - 1, (s - 1) * n_phases // n_steps)]
        x("""INSERT INTO steps(plan_id,phase_id,number,section,title,change,proof,owner_need,owner_question,
             behavior_change,behavior_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
          (plan_id, ph, s, f"Section {s % 5}", text(8), text(60), text(30),
           rnd.choices(["none", "decision", "confirm", "review", "production"], (10, 1, 1, 1, 0.3))[0], text(15),
           int(rnd.random() < 0.1), text(12)))
        step_ids.append(lastid())
    ndep = 0
    for i, sid in enumerate(step_ids[1:], 1):
        for d in sorted(set(rnd.sample(step_ids[:i], min(i, rnd.choice((1, 2, 3)))))):
            x("INSERT INTO step_dependencies(step_id,depends_on_step_id) VALUES (?,?)", (sid, d))
            ndep += 1
    x("COMMIT")
    T.add("plan_steps_and_deps", time.perf_counter() - t0, n_steps + ndep)

    t0 = time.perf_counter()
    x("BEGIN")
    fl = list(all_findings)
    rnd.shuffle(fl)
    nsf = 0
    for i, fid in enumerate(fl):
        s = step_ids[i % n_steps]
        if rnd.random() < 0.03:
            s2 = step_ids[(i + 7) % n_steps]
            x("INSERT INTO step_findings(step_id,finding_id,multi_reason) VALUES (?,?,?)", (s, fid, "split fix"))
            x("INSERT INTO step_findings(step_id,finding_id,multi_reason) VALUES (?,?,?)", (s2, fid, "split fix"))
            nsf += 2
        else:
            x("INSERT INTO step_findings(step_id,finding_id,multi_reason) VALUES (?,?,NULL)", (s, fid))
            nsf += 1
    x("COMMIT")
    T.add("insert_step_findings", time.perf_counter() - t0, nsf)

    # progress: about 40% of steps done, their issues fixed with guard tests
    t0 = time.perf_counter()
    nupd = 0
    x("BEGIN")
    x("UPDATE plans SET status='awaiting-approval' WHERE id=?", (plan_id,))
    x("UPDATE plans SET status='approved', approved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (plan_id,))
    x("UPDATE plans SET status='in-progress' WHERE id=?", (plan_id,))
    for sid in step_ids:
        r = rnd.random()
        if r < 0.4:
            x("UPDATE steps SET status='doing', change_actor_id=?, change_note='started' WHERE id=?", (agent, sid))
            x("UPDATE steps SET status='done', progress_note=?, change_commit_sha=?, change_note='done' WHERE id=?",
              (text(30), head_sha, sid))
            x("INSERT INTO step_commits(step_id,repository_id,commit_sha,branch,summary) VALUES (?,1,?,'main',?)",
              (sid, head_sha, text(8)))
            for (iid,) in x("SELECT DISTINCT f.issue_id FROM step_findings sf JOIN findings f ON f.id=sf.finding_id "
                            "WHERE sf.step_id=? ORDER BY 1", (sid,)).fetchall():
                x("""UPDATE issues SET status='fixed', after_state=?, change_step_id=?, change_commit_sha=?,
                     change_actor_id=?, change_review_id=NULL, change_note='step done'
                     WHERE id=? AND status NOT IN ('fixed','verified')""", (text(20), sid, head_sha, agent, iid))
                x("""INSERT OR IGNORE INTO guard_tests(issue_id,repository_id,test_path,test_name,step_id,
                     added_commit_sha,failed_before_fix,passes_after_fix) VALUES (?,1,?,?,?,?,1,1)""",
                  (iid, f"tests/test_{iid % 50}.py", f"test_issue_{iid}", sid, head_sha))
                nupd += 1
        elif r < 0.5:
            x("UPDATE steps SET status='doing', change_actor_id=?, change_note='started' WHERE id=?", (agent, sid))
        elif r < 0.55:
            x("UPDATE steps SET status='waiting-owner', owner_question=? WHERE id=?", (text(12), sid))
    x("COMMIT")
    T.add("plan_progress_updates", time.perf_counter() - t0, nupd or None)


def check(info, ledger_py=None):
    """Run `ledger.py check` on a built ledger; returns the CompletedProcess."""
    return fixtures.run_ledger("--root", info["root"], "check", ledger_py=ledger_py, timeout=600)


# ------------------------------------------------------------------ baseline measurements

def _gz(data):
    return len(gzip.compress(data, 6))


def _payload_sizes(info, ledger_py=None):
    """Bytes, gzip bytes and gzip ratio (raw/gzip) of the viewer payloads the review and plan pages load."""
    out = {}
    with fixtures.ServerThread(root=info["root"], db=info["db"], ledger_py=ledger_py) as srv:
        for name, path in (("api_review", "/api/review/R-0001"), ("api_plan", "/api/plan/P-0001")):
            t0 = time.perf_counter()
            r = srv.get(path, timeout=300)
            ms = (time.perf_counter() - t0) * 1000
            if r.status != 200:
                raise RuntimeError(f"GET {path} -> {r.status}")
            g = _gz(r.body)
            out[name] = {"bytes": len(r.body), "gzip_bytes": g, "gzip_ratio": round(len(r.body) / g, 2),
                         "ms": round(ms, 1)}
    return out


def _file_sizes(info, ledger_py=None):
    db = info["db"]
    p = fixtures.run_ledger("--root", info["root"], "dump", ledger_py=ledger_py, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"dump failed:\n{p.stdout}\n{p.stderr}")
    sql = db.with_suffix(".sql")
    out = {}
    for name, f in (("ledger", db), ("dump", sql)):
        data = f.read_bytes()
        g = _gz(data)
        out[name] = {"bytes": len(data), "gzip_bytes": g, "gzip_ratio": round(len(data) / g, 2)}
    return out


def _count_objects(proj):
    kv = dict(line.split(": ", 1) for line in _git(proj, "count-objects", "-v").split("\n") if ": " in line)
    return {k: int(v) for k, v in kv.items() if v.isdigit()}


def _git_growth(info, rnd, commits=5, ledger_py=None):
    """Commit the ledger binary and its dump `commits` times, with a writing session (30 steps finished with notes,
    20 issue checks' evidence rewritten) between commits. Returns the loose-object growth per commit (KiB, what git
    stores before packing) and the pack size after `git gc` (with deltas) for the history of ledger commits."""
    proj, db = info["root"], info["db"]
    prose = Prose(rnd)
    base = _count_objects(proj)
    _git(proj, "gc", "-q")
    packed0 = _count_objects(proj)["size-pack"]
    per_commit = []
    before = _count_objects(proj)["size"]
    steps = checks = None
    for c in range(commits):
        con = sqlite3.connect(str(db))
        if steps is None:
            steps = [r[0] for r in con.execute("SELECT id FROM steps WHERE status IN ('planned','doing') ORDER BY id")]
            rnd.shuffle(steps)
        with con:
            for sid in steps[c * 30:(c + 1) * 30]:
                con.execute("UPDATE steps SET status='doing', change_note='started' WHERE id=? AND status='planned'",
                            (sid,))
                con.execute("UPDATE steps SET status='done', progress_note=?, change_note='done' WHERE id=?",
                            (prose.text(30), sid))
            if checks is None:  # review checks are append-only (schema 3): edit issue text instead
                checks = [r[0] for r in con.execute("SELECT id FROM issues ORDER BY id")]
            for cid in rnd.sample(checks, min(20, len(checks))):
                con.execute("UPDATE issues SET recommendation=? WHERE id=?", (prose.text(20), cid))
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
        p = fixtures.run_ledger("--root", proj, "dump", ledger_py=ledger_py, timeout=600)
        if p.returncode != 0:
            raise RuntimeError(f"dump failed:\n{p.stdout}\n{p.stderr}")
        _git(proj, "add", "-f", ".code-review/ledger.sqlite", ".code-review/ledger.sql")
        _git(proj, "commit", "-q", "--no-verify", "-m", f"ledger session {c + 1}", env=_dated(100 + c))
        now = _count_objects(proj)["size"]
        per_commit.append(now - before)
        before = now
    _git(proj, "gc", "-q")
    packed = _count_objects(proj)["size-pack"]
    return {"commits": commits, "loose_kib_per_commit": per_commit,
            "loose_kib_mean": round(sum(per_commit) / len(per_commit), 1),
            "pack_kib_after_gc": packed, "pack_kib_before_ledger": packed0,
            "pack_kib_growth_per_commit": round((packed - packed0) / commits, 1), "objects_before": base}


def baseline(scales=(1, 10), reports=None, work=None, ledger_py=None, verbose=True):
    """Build each scale (generated words, plus real prose at the first scale when reports is given) and measure.
    Returns a list of (name, value, unit) rows and the raw measurements."""
    work = Path(work) if work else Path(tempfile.mkdtemp(prefix="ledger-scale-"))
    raw = {}
    rows = []

    def add(name, value, unit):
        rows.append((name, value, unit))
        if verbose:
            print(f"{name:52s} {value:>14} {unit}", flush=True)

    variants = [(s, None) for s in scales]
    if reports:
        variants.insert(1, (scales[0], reports))
    for s, rep in variants:
        tag = f"x{s}" + ("_prose" if rep else "")
        t0 = time.perf_counter()
        info = build(work / tag, scale=s, reports=rep, ledger_py=ledger_py)
        add(f"{tag}.build_seconds", round(time.perf_counter() - t0, 1), "s")
        c = check(info, ledger_py)
        add(f"{tag}.check_exit", c.returncode, "exit code")
        add(f"{tag}.findings", info["counts"]["findings"], "rows")
        add(f"{tag}.issues", info["counts"]["issues"], "rows")
        add(f"{tag}.steps", info["counts"]["steps"], "rows")
        tm = info["timings"]
        for k in ("insert_findings", "match_new_issue", "match_existing_issue", "insert_checks"):
            if k in tm:
                add(f"{tag}.{k}_us_per_row", tm[k]["us_per"], "us/row")
        fs = _file_sizes(info, ledger_py)
        for name, d in fs.items():
            add(f"{tag}.{name}_bytes", d["bytes"], "bytes")
            add(f"{tag}.{name}_gzip_ratio", d["gzip_ratio"], "raw/gzip")
        ps = _payload_sizes(info, ledger_py)
        for name, d in ps.items():
            add(f"{tag}.{name}_bytes", d["bytes"], "bytes")
            add(f"{tag}.{name}_gzip_ratio", d["gzip_ratio"], "raw/gzip")
            add(f"{tag}.{name}_ms", d["ms"], "ms")
        raw[tag] = {"timings": tm, "counts": info["counts"], "files": fs, "payloads": ps, "prose": info["prose"],
                    "check_output": c.stdout[-2000:]}
        if s == scales[0] and not rep:
            g = _git_growth(info, random.Random(7), ledger_py=ledger_py)
            raw[tag]["git_growth"] = g
            add(f"{tag}.git_loose_growth_per_ledger_commit_mean", g["loose_kib_mean"], "KiB")
            add(f"{tag}.git_loose_growth_per_ledger_commit", ",".join(map(str, g["loose_kib_per_commit"])), "KiB")
            add(f"{tag}.git_pack_growth_per_ledger_commit_after_gc", g["pack_kib_growth_per_commit"], "KiB")
    return rows, raw


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scale.py", description="Build large ledgers and measure them.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build one large ledger")
    b.add_argument("dir")
    b.add_argument("--scale", type=int, default=1)
    b.add_argument("--reviews", type=int, default=5)
    b.add_argument("--files", type=int, default=300)
    b.add_argument("--reports", help="folder of *.md reports to take prose from")
    b.add_argument("--ledger", help="ledger.py to use (default: build one from the sources)")
    b.add_argument("--json", help="write timings and counts to this file")
    m = sub.add_parser("baseline", help="build 1x and 10x ledgers and record the baseline measurements")
    m.add_argument("--scales", default="1,10")
    m.add_argument("--reports")
    m.add_argument("--work", help="folder for the built projects (default: a new temporary folder, kept)")
    m.add_argument("--ledger")
    m.add_argument("--out", help="write the measurements here (name, value, unit per line)")
    m.add_argument("--json", help="write the raw measurements here")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        info = build(a.dir, scale=a.scale, reviews=a.reviews, files=a.files, reports=a.reports, ledger_py=a.ledger,
                     verbose=True)
        print(json.dumps(info["counts"]))
        print("ledger bytes", info["ledger_bytes"])
        if a.json:
            Path(a.json).write_text(json.dumps({k: v for k, v in info.items() if k not in ("db", "root")}, indent=1))
        c = check(info, a.ledger)
        print(c.stdout, end="")
        return c.returncode
    rows, raw = baseline(tuple(int(s) for s in a.scales.split(",")), reports=a.reports, work=a.work,
                         ledger_py=a.ledger)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            for name, value, unit in rows:
                f.write(f"{name}\t{value}\t{unit}\n")
    if a.json:
        Path(a.json).write_text(json.dumps(raw, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
