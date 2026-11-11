#!/usr/bin/env python3
"""Code review ledger: one SQLite file per project that records every review,
every point it raised, how points relate across reviews, the fix plan, and
what is waiting on the owner. Standard library only (Python 3.9+; tested on 3.9.0, 3.9.25 and 3.13).

    python3 ledger.py init [--force]     create .code-review/ledger.sqlite here and copy this ledger.py beside it
                                         (never over a newer copy unless --force)
    python3 ledger.py migrate            upgrade an older ledger to this schema (keeps a .bak-v<N> copy)
    python3 ledger.py serve [--port N]   open the local viewer (127.0.0.1 only)
    python3 ledger.py check              integrity + must-be-empty checks
    python3 ledger.py fill-refs [R-0001] copy excerpts, blob ids, permalinks from git
    python3 ledger.py tag-review R-0001  tag each reviewed commit so it stays reachable
    python3 ledger.py show <location id> print the cited lines at their commit
    python3 ledger.py gates              list gates that are open or answered
    python3 ledger.py dump               write .code-review/ledger.sql (text, for diffs)

Options: --root DIR (project root; default: the nearest folder upward with a ledger, else the
top of the current git repository, else the current directory), --db FILE, --version.
"""
import argparse, hashlib, json, os, re, secrets, shlex, sqlite3, subprocess, sys, threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

TOOL_VERSION = "@@TOOL_VERSION@@"  # stamped by dev/build.py from `version:` in SKILL.md's frontmatter
TOOL_HASH = "@@TOOL_HASH@@"  # stamped by dev/build.py: sha256 of the built file with this value as the placeholder
SCHEMA = r"""@@SCHEMA@@"""
PAGE = r"""@@PAGE@@"""
MIGRATION_FILES = r"""@@MIGRATIONS@@"""


def _parse_migrations(text):
    """[(version, slug, description, sql)] from the embedded references/migrations/NNNN-<slug>.sql files."""
    out = []
    for m in re.finditer(r"^-- migration: (\d{4})-([a-z0-9][a-z0-9-]*)\.sql\n(.*?)(?=^-- migration: |\Z)", text,
                         re.M | re.S):
        body = m.group(3)
        out.append((int(m.group(1)), m.group(2), body.split("\n", 1)[0][3:].strip(), body))
    return out


# The schema version is the highest migration; references/schema.sql makes a ledger of exactly that version, and
# dev/build.py refuses to build unless its header, meta seed and schema_migrations seed agree.
MIGRATIONS = _parse_migrations(MIGRATION_FILES)
SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0
BASELINE_VERSION = MIGRATIONS[0][0] if MIGRATIONS else 0  # the oldest schema migrate starts from

HEX = re.compile(r"^[0-9a-f]{7,64}$")
FULL_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REVIEW_CODE = re.compile(r"R-[0-9]{4,}")
SAFE_URL = re.compile(r"^https?://")  # the only links the ledger stores or the viewer draws (schema CHECKs agree)
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------- database

def connect(db, readonly=False, create=False):
    """A connection with foreign keys on, a 5 s busy timeout and sqlite3.Row rows. The file is opened through a
    percent-encoded file: URI, so ?, # or % in the path cannot cut the name or drop the mode. readonly: mode=ro plus
    PRAGMA query_only. Otherwise mode=rw, which refuses a missing file; only init and backups pass create=True."""
    mode = "ro" if readonly else ("rwc" if create else "rw")
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=" + mode, uri=True, timeout=5,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    if readonly:
        con.execute("PRAGMA query_only = 1")
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


class LedgerError(Exception):
    """A ledger that cannot be used as it is; the message is one line for the person running the command."""


def ledger_version(con, db):
    """The ledger's meta schema_version as an int; LedgerError for anything that is not a ledger."""
    try:
        r = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.DatabaseError as e:  # not SQLite at all, or SQLite without the ledger's tables
        raise LedgerError(f"{db} is not a ledger ({e})") from None
    if r is None:
        raise LedgerError(f"{db} has no schema_version in its meta table; it was not made by ledger.py init")
    try:
        return int(r[0])
    except (TypeError, ValueError):
        raise LedgerError(f"{db} has schema_version {r[0]!r} in its meta table, which is not a whole number") from None


def version_problem(db, v, write):
    """None when schema v can be used as asked, else (fatal, message)."""
    me = Path(__file__).resolve()
    if v > SCHEMA_VERSION:
        return True, f"{db} is schema {v}; this ledger.py only knows {SCHEMA_VERSION}. Use a newer ledger.py."
    if v < BASELINE_VERSION:
        return write, (f"{db} is schema {v}, older than any schema this ledger.py can migrate "
                       f"(oldest {BASELINE_VERSION}).")
    if v < SCHEMA_VERSION:
        return write, f"{db} is schema {v}; this ledger.py is schema {SCHEMA_VERSION}. Run: python3 {me} migrate"
    return None


def open_ledger(db, write=False, verify=False):
    """Connect to an existing ledger for a command, or exit 1 with one line: no ledger, not a ledger, no
    schema_version, a newer schema, or (for writes) an older one. Reads of an older ledger print a warning. With
    verify, a ledger of this schema version whose stored schema differs from the shipped one is refused too."""
    if not db.exists():
        sys.exit(f"no ledger at {db}; run: python3 ledger.py init")
    con = None
    try:
        con = connect(db)
        v = ledger_version(con, db)
    except (LedgerError, sqlite3.DatabaseError) as e:
        if con is not None:
            con.close()  # never left open on the way out (FIX-4)
        sys.exit(str(e) if isinstance(e, LedgerError) else f"{db} is not a ledger ({e})")
    problem = version_problem(db, v, write)
    if problem:
        fatal, msg = problem
        if fatal:
            con.close()
            sys.exit(msg)
        print(f"warning: {msg} (reading it as it is)", file=sys.stderr)
    elif verify:
        problems = verify_schema(con)
        if problems:
            con.close()
            sys.exit(drift_message(db, problems))
    return con


def drift_message(db, problems):
    shown = "\n".join(f"  {p}" for p in problems[:20])
    more = f"\n  ... {len(problems) - 20} more" if len(problems) > 20 else ""
    return (f"{db} does not match the schema this ledger.py ships ({len(problems)} differences); refusing to use it. "
            f"init with the skill's ledger.py restores the shipped views and triggers; check lists every "
            f"difference.\n{shown}{more}")


# ---------------------------------------------------------------- schema comparison and migrations

def _sql_segments(sql):
    """Split SQL into (kind, text) runs: 'lit' for '...' string literals, 'id' for simple quoted identifiers (given
    unquoted), 'code' for the rest, with -- and /* */ comments replaced by a space."""
    out, i, n, code = [], 0, len(sql), []
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k < 0:
                    k = n - 1
                    break
                if sql[k + 1:k + 2] == "'":
                    j = k + 2
                    continue
                break
            out.append(("code", "".join(code))); code = []
            out.append(("lit", sql[i:k + 1])); i = k + 1
        elif c in '"`':
            k = sql.find(c, i + 1)
            inner = sql[i + 1:k] if k > 0 else ""
            if k > 0 and NAME.match(inner):
                code.append(inner)
            else:
                code.append(sql[i:(k + 1 if k > 0 else n)])
            i = k + 1 if k > 0 else n
        elif sql.startswith("--", i):
            k = sql.find("\n", i)
            code.append(" "); i = n if k < 0 else k
        elif sql.startswith("/*", i):
            k = sql.find("*/", i + 2)
            code.append(" "); i = n if k < 0 else k + 2
        else:
            code.append(c); i += 1
    out.append(("code", "".join(code)))
    return out


def normalize_sql(sql):
    """SQL text compared for schema drift: comments dropped, whitespace collapsed (none around ( ) , ;), simple
    quoted names unquoted; string literals kept exactly."""
    parts = []
    for kind, text in _sql_segments(sql or ""):
        if kind == "code":
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r" ?([(),;]) ?", r"\1", text)
        parts.append(text)
    return "".join(parts).strip()


def split_sql(sql):
    """The statements of a script, in order, without comment-only pieces (for running inside one transaction:
    executescript would commit first)."""
    out, buf = [], ""
    pieces = sql.split(";")
    for i, piece in enumerate(pieces):
        buf += piece + (";" if i < len(pieces) - 1 else "")
        if sqlite3.complete_statement(buf):
            if normalize_sql(buf).strip(";"):
                out.append(buf.strip())
            buf = ""
    if normalize_sql(buf).strip(";"):
        raise ValueError(f"incomplete SQL statement at the end: {buf.strip()[:80]}")
    return out


_shipped = {}
CREATE_OBJECT = re.compile(r"^CREATE (?:UNIQUE |TEMP |TEMPORARY )?(TABLE|INDEX|VIEW|TRIGGER) (?:IF NOT EXISTS )?"
                           r"([A-Za-z_][A-Za-z0-9_]*)", re.I)

LEADING_COMMENTS = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)


def shipped_objects():
    """[(type, name, tbl_name, sql)] of the shipped schema in creation order: each CREATE statement of SCHEMA, read
    from the text (tbl_name is None). dev/tests/test_schema_drift.py checks it equals what SQLite stores when SCHEMA
    runs."""
    if SCHEMA not in _shipped:
        out = []
        for stmt in split_sql(SCHEMA):
            m = CREATE_OBJECT.match(normalize_sql(stmt))
            if m:
                lead = LEADING_COMMENTS.match(stmt)  # skip comments above the statement, which may say "create"
                sql = stmt[lead.end():].rstrip().rstrip(";").rstrip()
                out.append((m.group(1).lower(), m.group(2), None, sql))
        _shipped.clear()
        _shipped[SCHEMA] = out
    return _shipped[SCHEMA]


def ledger_objects(con):
    return {(r[0], r[1]): r[2] for r in con.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}


def shipped_schema_hash():
    """sha256 of the shipped schema's objects (type, name, normalized sql), stored in meta schema_hash."""
    items = sorted((t, n, normalize_sql(s)) for t, n, _, s in shipped_objects())
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()


TYPE_ORDER = {"table": 0, "index": 1, "view": 2, "trigger": 3}


def verify_schema(con):
    """Every difference between the ledger's stored schema and the shipped one, as lines naming the object: missing,
    extra (not in the shipped schema) or changed tables, indexes, views and triggers. Empty when they match."""
    want = {(t, n): normalize_sql(s) for t, n, _, s in shipped_objects()}
    have = {k: normalize_sql(s) for k, s in ledger_objects(con).items()}
    out = []
    for key in sorted(set(want) | set(have), key=lambda k: (TYPE_ORDER.get(k[0], 9), k[1])):
        t, n = key
        if key not in have:
            out.append(f"missing {t} {n}")
        elif key not in want:
            out.append(f"extra {t} {n} (not in the shipped schema)")
        elif have[key] != want[key]:
            out.append(f"changed {t} {n} (its definition differs from the shipped schema)")
    return out


def _drop_views_and_triggers(con):
    for t, n in [(t, n) for (t, n) in ledger_objects(con) if t in ("view", "trigger")]:
        con.execute(f'DROP {t.upper()} IF EXISTS "{n}"')


def _create_views_and_triggers(con):
    for t, n, _, sql in shipped_objects():
        if t in ("view", "trigger"):
            con.execute(sql)


def sync_views_and_triggers(con):
    """Make the ledger's views and triggers exactly the shipped ones (they hold no data). Runs in the caller's
    transaction. Returns notes naming each one that was removed or restored; empty when nothing differed."""
    want = {(t, n): normalize_sql(s) for t, n, _, s in shipped_objects() if t in ("view", "trigger")}
    have = {k: normalize_sql(s) for k, s in ledger_objects(con).items() if k[0] in ("view", "trigger")}
    notes = [f"removed {t} {n} (not in the shipped schema)" for (t, n) in sorted(set(have) - set(want))]
    notes += [f"restored {t} {n} (the shipped definition)" for (t, n) in sorted(want)
              if have.get((t, n)) != want[(t, n)]]
    if notes:
        _drop_views_and_triggers(con)
        _create_views_and_triggers(con)
    return notes


def stamp_meta(con, created_by=None):
    """Record which tool last wrote the ledger, the shipped schema's hash and PRAGMA user_version (only what
    changed). created_by fills created_by_skill when the ledger has none."""
    meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
    want = {"written_by_tool": TOOL_VERSION, "schema_hash": shipped_schema_hash()}
    if created_by and not meta.get("created_by_skill"):
        want["created_by_skill"] = created_by
    for k, v in want.items():
        if meta.get(k) != v:
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (k, v))
    if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


UNKNOWN_CREATOR = "unknown (made before ledger.py had versions)"


class MigrationError(Exception):
    pass


def free_backup_name(db, v):
    base = db.with_name(f"{db.name}.bak-v{v}")
    n, cand = 1, base
    while cand.exists():
        n += 1
        cand = base.with_name(f"{base.name}.{n}")
    return cand


def _checks_of(sql):
    """The CHECK expressions of one CREATE TABLE statement (normalized text), in order."""
    text, out, i = normalize_sql(sql), [], 0
    while True:
        m = re.compile(r"\bCHECK\(", re.I).search(text, i)
        if not m:
            return out
        depth, j, quote = 1, m.end(), False
        while j < len(text) and depth:
            c = text[j]
            if c == "'":
                quote = not quote
            elif not quote:
                depth += {"(": 1, ")": -1}.get(c, 0)
            j += 1
        out.append(text[m.end():j - 1])
        i = j


def rows_refused_by(con, stmt, limit=50):
    """For a migration's `CREATE TABLE <table>_new (...)` statement: a line for each row of <table> that one of the
    new table's CHECKs would refuse, '<table> <code, id or rowid>: <rule> (<column>=<value> ...)', at most `limit`.
    Empty for any other statement. A rule naming a column the old table lacks (a column the rebuild adds) is
    skipped: that column holds its default after the rebuild."""
    m = re.match(r"CREATE TABLE ([A-Za-z_][A-Za-z0-9_]*)_new\s*\(", normalize_sql(stmt), re.I)
    if not m:
        return []
    table, out = m.group(1), []
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({quote_name(table)})")]
    key = "code" if "code" in cols else ("id" if "id" in cols else "rowid")
    for rule in _checks_of(stmt) if cols else []:
        used = [c for c in cols if re.search(rf"\b{re.escape(c)}\b", rule)]
        try:
            bad = con.execute(f"SELECT {key}, {', '.join(used) or 'NULL'} FROM {quote_name(table)} "
                              f"WHERE NOT ({rule}) LIMIT {int(limit)}").fetchall()
        except sqlite3.OperationalError:
            continue  # names a column this table does not have yet
        for r in bad:
            vals = " ".join(f"{c}={r[n + 1]!r}" for n, c in enumerate(used))
            out.append(f"{table} {r[0]}: {rule if len(rule) <= 140 else rule[:137] + '...'} ({vals})")
    return out[:limit]


def migrate(db):
    """Upgrade the ledger to SCHEMA_VERSION: back it up (SQLite backup API) to ledger.sqlite.bak-v<N>, then in one
    BEGIN IMMEDIATE transaction drop every view and trigger, apply each pending migration and record it in
    schema_migrations, re-create the shipped views and triggers, run foreign_key_check and compare the result with
    the shipped schema; anything wrong rolls it all back. Refuses a ledger newer than this tool."""
    if not db.exists():
        sys.exit(f"no ledger at {db}; run: python3 ledger.py init")
    try:
        con = connect(db)
        v = ledger_version(con, db)
    except (LedgerError, sqlite3.DatabaseError) as e:
        sys.exit(str(e) if isinstance(e, LedgerError) else f"{db} is not a ledger ({e})")
    problem = version_problem(db, v, True)
    if problem and v >= BASELINE_VERSION and v < SCHEMA_VERSION:
        problem = None  # older but migratable: that is what migrate is for
    if problem:
        con.close()
        sys.exit(problem[1])
    pending = [m for m in MIGRATIONS if m[0] > v]
    backup = None
    if pending:
        backup = free_backup_name(db, v)
        dest = connect(backup, create=True)
        try:
            con.backup(dest)
        finally:
            dest.close()
        make_private(backup)  # a full copy of the ledger: as private as the ledger itself
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys = OFF")  # the table-rebuild recipe needs it off; a no-op inside a transaction
    step = "the update" if pending else "the check of the current schema"
    try:
        con.execute("BEGIN IMMEDIATE")
        if pending:
            _drop_views_and_triggers(con)
            for ver, slug, desc, sql in pending:
                step = f"migration {ver:04d}-{slug}"
                for stmt in split_sql(sql):
                    # before a table is rebuilt with new rules, list every row they refuse (step 61)
                    refused = rows_refused_by(con, stmt)
                    if refused:
                        raise MigrationError(
                            f"CHECK constraint failed for {len(refused)} existing row"
                            f"{'s' if len(refused) != 1 else ''} (fix each, then run migrate again):\n  "
                            + "\n  ".join(refused) + "\n")
                    con.execute(stmt)
                con.execute("INSERT INTO schema_migrations(version, description) VALUES (?, ?)", (ver, desc))
            step = "the checks after " + ", ".join(f"migration {m[0]:04d}-{m[1]}" for m in pending)
            con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            _create_views_and_triggers(con)
            notes = []
        else:
            notes = sync_views_and_triggers(con)
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise MigrationError(f"foreign_key_check found {len(fk)} broken references after it "
                                 f"(first: {tuple(fk[0])})")
        problems = verify_schema(con)
        if problems:
            raise MigrationError("the result differs from the shipped schema: " + "; ".join(problems[:10]))
        stamp_meta(con, created_by=UNKNOWN_CREATOR)
        con.execute("COMMIT")
    except (sqlite3.Error, ValueError, MigrationError) as e:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
        if backup is not None and backup.exists():
            backup.unlink()  # the ledger is unchanged, so the copy is not needed
        sys.exit(f"migrate: {step} failed: {e}; nothing was changed (the ledger is still schema {v})")
    finally:
        try:
            con.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error:
            pass
    con.close()
    if pending:
        set_wal(db)  # existing ledgers switch to WAL once, with their upgrade (D-0006)
        print(f"migrated {db} from schema {v} to {SCHEMA_VERSION} (a copy of the schema-{v} ledger is at {backup})")
        for ver, slug, desc, _ in pending:
            print(f"  {ver:04d}-{slug}: {desc}")
    else:
        print(f"{db} is already schema {v}; nothing to migrate")
        for note in notes:
            print(f"note: {note}")
    return 0


# ---------------------------------------------------------------- init and the project's copy of ledger.py

VERSION_LINE = re.compile(r'^TOOL_VERSION = "([^"\n]*)"', re.M)


def parse_version(v):
    """A sortable key for a semantic version (a pre-release sorts before its release), or None."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1, m.group(4) or "")


def tool_version_of(path):
    """TOOL_VERSION of a ledger.py read as text (it is never run): '' when it has none (made before versions)."""
    m = VERSION_LINE.search(path.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else ""


def plan_tool_copy(home, force):
    """What init does with the project's .code-review/ledger.py: None (leave it), or (note, keep_backup) to replace
    it with this file. Exits 1, before anything is written, when the project copy is newer (or of unknown version)
    and --force was not given."""
    me = Path(__file__).resolve()
    if not home.exists():
        return ("", False)
    if me == home.resolve() or home.read_bytes() == me.read_bytes():
        return None
    theirs = tool_version_of(home)
    label = theirs or "no version"
    mine_k, theirs_k = parse_version(TOOL_VERSION), (parse_version(theirs) if theirs else ())
    bak = home.with_name("ledger.py.bak")
    if mine_k is not None and theirs_k is not None and theirs_k < mine_k:
        return (f"note: replacing {home} ({label}) with this copy ({TOOL_VERSION}); the old copy is kept as {bak}", True)
    if mine_k is not None and theirs_k == mine_k:
        if force:
            return (f"note: --force: replacing {home} (also {label}, other contents); the old copy is kept as {bak}",
                    True)
        print(f"warning: {home} says version {label} like this copy, but its contents differ; left it unchanged "
              f"(init --force replaces it)", file=sys.stderr)
        return None
    if force:
        return (f"note: --force: replacing {home} ({label}) with this older copy ({TOOL_VERSION}); the old copy is "
                f"kept as {bak}", True)
    newer = "newer than" if theirs_k is not None and mine_k is not None else "of a version not comparable with"
    sys.exit(f"{home} is ledger.py {label}, {newer} this copy ({TOOL_VERSION}); left it unchanged. Run that copy, "
             f"update the skill, or pass init --force to replace it.")


GITIGNORE = ("# The ledger runs in WAL mode: ledger.sqlite-wal and -shm exist while it is open, and dump checkpoints\n"
             "# them into ledger.sqlite. The rest are local backups and temporary files of ledger.py.\n"
             "ledger.sqlite-journal\nledger.sqlite-wal\nledger.sqlite-shm\nledger.sqlite.tmp\nledger.sqlite.bak-v*\n"
             "ledger.py.bak\nledger.py.tmp\nledger.sql.tmp\n")
TOOL_SIGNATURE = '"""Code review ledger:'  # the second line of every ledger.py this tool builds


def looks_like_tool(path):
    """True when path starts like a ledger.py built by this tool (its docstring within the first three lines)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = [f.readline() for _ in range(3)]
    except OSError:
        return False
    return any(l.startswith(TOOL_SIGNATURE) for l in head)


def write_atomic(path, data, mode=0o600):
    """Write bytes to path through path.tmp + fsync + os.replace, so path is either the old or the new content."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def make_private(path, mode=0o600):
    try:
        if path.exists():
            os.chmod(str(path), mode)
    except OSError:
        pass


def repaired_gitignore(text):
    """(new text, lines added) for an existing .gitignore: the owner's own lines first, then the tool's lines in
    order; a line that is only the start of one of the tool's lines (a truncated write) is dropped."""
    want = GITIGNORE.splitlines()
    have = text.splitlines()
    own = [l for l in have if l not in want and not any(w.startswith(l) for w in want)]
    added = [l for l in want if l not in have and not l.startswith("#")]
    return "".join(l + "\n" for l in own + want), added


def require_ledger(db):
    """Exit 1 with one line unless db is SQLite with a meta schema_version row."""
    try:
        con = connect(db)
        try:
            ledger_version(con, db)
        finally:
            con.close()
    except (LedgerError, sqlite3.DatabaseError) as e:
        sys.exit(f"incomplete or foreign ledger at {db} ({e}); move it aside and run init again")


def build_ledger(root, db):
    """Create db atomically: the whole schema and the meta rows are built in db.tmp inside one transaction, then
    renamed into place. On any failure nothing is left behind and init can simply run again."""
    tmp = db.with_name(db.name + ".tmp")
    side = [tmp.with_name(tmp.name + s) for s in ("", "-journal", "-wal", "-shm")]
    for p in side:
        if p.exists():
            p.unlink()
    con = None
    try:
        con = connect(tmp, create=True)  # created owner-only: init runs under umask 077
        con.isolation_level = None
        con.execute("BEGIN")
        for stmt in split_sql(SCHEMA):
            con.execute(stmt)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('project_name',?)", (root.name,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('created_at',?)", (now(),))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('ledger_location',?)", (str(db.parent),))
        stamp_meta(con, created_by=TOOL_VERSION)
        con.execute("COMMIT")
        con.close()
        con = None
        os.replace(str(tmp), str(db))
    except (sqlite3.Error, OSError, ValueError) as e:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
        for p in side:
            try:
                p.unlink()
            except OSError:
                pass
        sys.exit(f"init: could not create {db} ({e}); nothing was left behind, run init again")


def set_wal(db):
    """Switch the ledger to WAL (decision D-0006: readers never block the agent's writes). Kept in the file."""
    try:
        con = connect(db)
        try:
            con.execute("PRAGMA journal_mode = WAL")
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"note: could not switch {db} to WAL ({e}); it keeps its journal mode", file=sys.stderr)


def init(root, db, force=False, copy_tool=True, gitignore=True):
    """Create the ledger (or check and refresh an existing one), copy this ledger.py beside it and write its
    .gitignore. copy_tool / gitignore are off for a ledger file that is not in a ledger folder (--db elsewhere).
    Files are created owner-only (umask 077; the folder 0700, the ledger 0600)."""
    old = os.umask(0o077)
    try:
        _init(root, db, force, copy_tool, gitignore)
    finally:
        os.umask(old)


def _init(root, db, force, copy_tool, gitignore):
    home = db.parent / "ledger.py"
    if copy_tool and home.exists() and db.parent.name != ".code-review" and not looks_like_tool(home):
        sys.exit(f"{home} is not a ledger.py made by this tool; left it unchanged. Choose another folder for the "
                 f"ledger.")
    copy = plan_tool_copy(home, force) if copy_tool else None  # may exit, before anything is written
    fresh = not db.exists()
    if not fresh:
        require_ledger(db)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"init: could not create {db.parent} ({e})")
    if db.parent.name == ".code-review":
        make_private(db.parent, 0o700)
    if copy is not None:  # first, so a ledger never exists without the project's copy of the tool
        note, keep = copy
        try:
            if keep:
                write_atomic(home.with_name("ledger.py.bak"), home.read_bytes())
            write_atomic(home, Path(__file__).resolve().read_bytes())
        except OSError as e:
            sys.exit(f"init: could not copy ledger.py to {home} ({e}); nothing else was changed")
        if note:
            print(note)
        print(f"copied ledger.py to {home}")
    if fresh:
        build_ledger(root, db)
        set_wal(db)
        optimize_db(db)
        print(f"created {db}")
        print(f"ledger at {db} (created)")
    else:
        con = open_ledger(db, write=True)
        v = ledger_version(con, db)
        con.isolation_level = None
        moved = None
        try:
            con.execute("BEGIN IMMEDIATE")
            notes = sync_views_and_triggers(con)
            problems = verify_schema(con)
            if problems:
                con.execute("ROLLBACK")
                con.close()
                sys.exit(drift_message(db, problems))
            stamp_meta(con, created_by=UNKNOWN_CREATOR)
            was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
            if was is None or was[0] != str(db.parent):
                moved = was[0] if was else None
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('ledger_location',?)", (str(db.parent),))
            con.execute("COMMIT")
        except sqlite3.Error as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()
            sys.exit(f"init: could not update {db}: {e}")
        con.close()
        set_wal(db)
        optimize_db(db)
        for note in notes:
            print(f"note: {note}")
        if moved:
            print(f"note: the ledger was last set up at {moved}; it now records {db.parent}")
        print(f"{db} already exists (schema {v})")
        print(f"ledger at {db} (attached to existing)")
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), db.with_suffix(".sql"),
              *db.parent.glob(db.name + ".bak-v*")):
        make_private(p)
    if db.parent.name == ".code-review" and (db.parent / "patches").is_dir():
        make_private(db.parent / "patches", 0o700)
        for p in (db.parent / "patches").iterdir():
            make_private(p)
    if gitignore:
        gi = db.parent / ".gitignore"
        try:
            if not gi.exists():
                write_atomic(gi, GITIGNORE.encode(), 0o600)
            else:
                text = gi.read_text(encoding="utf-8", errors="replace")
                new, added = repaired_gitignore(text)
                if new != text:
                    write_atomic(gi, new.encode(), 0o600)
                    print(f"repaired {gi}" + (f": added {', '.join(added)}" if added else ""))
        except OSError as e:
            sys.exit(f"init: could not write {gi} ({e})")
        # If the ledger is ever committed, git must treat it as binary (no text diff or line merge); harmless
        # while it is not. The owner's own lines are kept; the tool's are added once.
        ga = db.parent / ".gitattributes"
        try:
            have = ga.read_text(encoding="utf-8", errors="replace") if ga.exists() else ""
            missing = [l for l in GITATTRIBUTES.splitlines() if l and not l.startswith("#") and l not in have.splitlines()]
            if missing:
                body = have + ("" if not have or have.endswith("\n") else "\n") + (GITATTRIBUTES if not have else
                                                                                   "".join(l + "\n" for l in missing))
                write_atomic(ga, body.encode(), 0o600)
        except OSError as e:
            sys.exit(f"init: could not write {ga} ({e})")


def _owner_problem(path):
    """Why a found ledger (file or folder) must not be used without --root: another user's, or writable by
    everyone. None when it is fine (or the platform has no user ids)."""
    if not hasattr(os, "getuid"):
        return None
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    if st.st_uid != os.getuid():
        return f"{path} belongs to another user (uid {st.st_uid})"
    if st.st_mode & 0o002:
        return f"{path} can be changed by every user"
    return None


def _check_owner(ledger_dir):
    for p in (ledger_dir / "ledger.sqlite", ledger_dir):
        why = _owner_problem(p)
        if why:
            sys.exit(f"{why}; refusing to use that ledger. Pass --root DIR (and --db FILE) to choose the project "
                     f"and ledger explicitly.")


def _git_tops(start):
    """(worktree top, main checkout top, note) for start. Outside git both tops are None; note says why git could
    not be asked (missing, timed out, failed), else None."""
    try:
        p = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel", "--git-common-dir"],
                           capture_output=True, timeout=10)
    except FileNotFoundError:
        return None, None, "git not available"
    except subprocess.TimeoutExpired:
        return None, None, "git timed out"
    except OSError as e:
        return None, None, f"git could not run ({e})"
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        if "not a git repository" in err:
            return None, None, None
        return None, None, f"git failed ({err.splitlines()[0] if err else p.returncode})"
    out = p.stdout.decode("utf-8", "replace").splitlines()
    if not out:
        return None, None, "git gave no top folder"
    top = Path(out[0]).resolve()
    main = top
    if len(out) > 1:
        common = Path(out[1])
        common = (common if common.is_absolute() else start / common).resolve()
        if common.name == ".git":
            main = common.parent
    return top, main, None


def _lists_repo(ledger_root, tops):
    """True when the ledger at ledger_root/.code-review lists one of tops in repositories.path."""
    db = ledger_root / ".code-review" / "ledger.sqlite"
    try:
        con = connect(db, readonly=True)
        try:
            paths = [r[0] for r in con.execute("SELECT path FROM repositories")]
        finally:
            con.close()
    except sqlite3.Error:
        return False
    for p in paths:
        try:
            if (ledger_root / (p or ".")).resolve() in tops:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def resolve_root(start, explicit=False, script=None):
    """(project root, how it was found). how is 'explicit' (--root given), 'script' (this ledger.py is the copy in a
    project's .code-review/), 'ledger' (an existing ledger found upward), 'git' (the top of the git repository) or
    'start' (no git and no ledger: the start folder; init then requires --root).

    Inside git the walk upward for an existing .code-review/ledger.sqlite stops at the repository top; for a git
    worktree the main checkout's ledger is used; a ledger above the repository top is used only when its
    repositories table lists that top. Outside git (or when git cannot run) the nearest ledger upward is used.
    A found ledger owned by another user, or writable by everyone, is refused."""
    start = Path(start)
    if explicit:
        return start, "explicit"
    if script is not None:
        here = Path(script).resolve().parent
        if here.name == ".code-review" and (here / "ledger.sqlite").exists():
            _check_owner(here)
            return here.parent, "script"

    def has(d):
        return (d / ".code-review" / "ledger.sqlite").exists()

    top, main, note = _git_tops(start)
    if top is None:
        for d in (start, *start.parents):
            if has(d):
                _check_owner(d / ".code-review")
                return d, "ledger"
        if note:
            print(f"note: {note}; using {start} as the project root", file=sys.stderr)
        return start, "start"
    for d in (start, *start.parents):
        if has(d):
            _check_owner(d / ".code-review")
            return d, "ledger"
        if d == top or top not in d.parents:
            break
    if main != top and has(main):
        _check_owner(main / ".code-review")
        return main, "ledger"
    for d in main.parents:
        if has(d):
            if _lists_repo(d, {top, main}):
                _check_owner(d / ".code-review")
                return d, "ledger"
            break
    return main, "git"


def find_root(start, explicit=False, script=None):
    """The project root for start (see resolve_root)."""
    return resolve_root(start, explicit, script)[0]


TIME_FORMAT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")  # what the tr_time_* triggers accept
_ISO_TIME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d+))?)?\s*(Z|z|[+-]\d\d:?\d\d)?$")


def now():
    """The current time as the ledger stores every time: UTC with milliseconds, the same text as SQLite's
    strftime('%Y-%m-%dT%H:%M:%fZ','now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_time(text):
    """An ISO-8601 time with any offset (git's %cI: 2026-11-10T10:00:00+02:00, or ...Z) in the ledger's format,
    converted to UTC (2026-11-10T08:00:00.000Z). A time without an offset is taken as UTC. ValueError if unreadable."""
    m = _ISO_TIME.match((text or "").strip())
    if not m:
        raise ValueError(f"not an ISO-8601 time: {text!r}")
    y, mo, d, h, mi, sec, frac, zone = m.groups()
    t = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0),
                 int(((frac or "") + "000000")[:6]), tzinfo=timezone.utc)
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        hh, mm = zone[1:3], zone[-2:]
        t -= sign * timedelta(hours=int(hh), minutes=int(mm))
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


# The checks live in the ledger's ledger_checks table (view, label, tier, position): tier 'error' views must be empty
# (check exits 1 otherwise), tier 'warning' views are listed but do not fail. The error tier is the twin of the
# must-be-empty list in SKILL.md (Rules for the ledger / check) and of the 'must be empty' comments in
# references/schema.sql; dev/tests/test_check.py enforces that they agree.
def ledger_checks(con, tier=None):
    """[(view, label, tier)] of the ledger's ledger_checks rows in position order (only `tier` if given); None when
    the ledger has no ledger_checks table (an older schema, which check_schema reports)."""
    try:
        got = [tuple(r) for r in con.execute("SELECT view, label, tier FROM ledger_checks ORDER BY position, view")]
    except sqlite3.Error:
        return None
    return [c for c in got if tier is None or c[2] == tier]


def count_view(con, view):
    return con.execute(f"SELECT count(*) FROM {quote_name(view)}").fetchone()[0]


def check(con, quiet=False, full=False):
    """Print what check finds; return the number of failed checks (integrity, foreign keys, schema, each non-empty
    error-tier view). A non-empty warning-tier view is printed with 'warning' and does not count. The integrity
    pass is PRAGMA quick_check (no index-content comparison, so fast on a large ledger) unless full, which runs the
    full PRAGMA integrity_check (`check --full`); the line printed names the one that ran."""
    bad = 0
    pragma = "integrity_check" if full else "quick_check"
    ic = con.execute(f"PRAGMA {pragma}").fetchone()[0]
    if ic != "ok":
        print(f"{pragma}:", ic); bad += 1
    elif not quiet:
        print(f"{pragma}: ok" + ("" if full else " (check --full runs the full integrity_check)"))
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        print(f"foreign_key_check: {len(fk)} broken references"); bad += 1
    bad += check_schema(con)
    checks = ledger_checks(con)
    if checks is None:
        print("ledger_checks: the ledger has no ledger_checks table, so its views were not checked "
              "(run: python3 ledger.py migrate)")
        return bad + 1
    for view, label, tier in checks:
        warn = "  warning" if tier == "warning" else ""
        try:
            n = count_view(con, view)
        except sqlite3.Error as e:
            bad += tier == "error"
            print(f"  ERR  {label}  ({view}){warn}: {e}")
            continue
        if n:
            bad += tier == "error"
            print(f"{n:5d}  {label}  ({view}){warn}")
            for r in rows(con, f"SELECT * FROM {quote_name(view)} LIMIT 5"):
                print("       ", json.dumps(r, ensure_ascii=False)[:200])
        elif not quiet:
            print(f"    0  {label}")
    optimize(con)
    return bad


def optimize_db(db):
    """PRAGMA optimize on the ledger file (after init)."""
    try:
        con = connect(db)
    except sqlite3.Error:
        return
    try:
        optimize(con)
    finally:
        con.close()


def optimize(con):
    """PRAGMA optimize: lets SQLite gather the statistics its query planner uses (cheap; skipped on a read-only
    connection or a busy ledger)."""
    try:
        con.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass


def check_schema(con):
    """Part of check: the ledger's schema_version must be SCHEMA_VERSION and its stored schema must match the shipped
    one object by object. Prints each problem; returns the number of failed checks (0 or 1). Silent when clean."""
    try:
        v = ledger_version(con, "the ledger")
    except LedgerError as e:
        print(f"schema_version: {e}")
        return 1
    if v != SCHEMA_VERSION:
        how = "run: python3 ledger.py migrate" if BASELINE_VERSION <= v < SCHEMA_VERSION else "use a matching ledger.py"
        print(f"schema_version: the ledger is schema {v}; this ledger.py is schema {SCHEMA_VERSION} ({how})")
        return 1
    problems = verify_schema(con)
    if problems:
        print(f"schema: {len(problems)} differences from the shipped schema (init with the skill's ledger.py "
              f"restores views and triggers)")
        for line in problems:
            print("       ", line)
        return 1
    return 0


# ---------------------------------------------------------------- git

def repo_dir(root, repo_path, allow_outside=False):
    """The folder of a repositories row: root/path, resolved (symlinks too). It must be the project root or below it
    unless the repository is listed explicitly as outside the root (repositories.allow_outside_root, D-0007)."""
    top = Path(root).resolve()
    d = (top / (repo_path or ".")).resolve()
    if not allow_outside and d != top and top not in d.parents:
        raise RuntimeError("repository path outside the project root")
    return d


def bad_location_path(path):
    """True for a finding_locations.path that must not reach git: empty, absolute, a backslash, a leading -, a ..
    segment, or a control character (the schema CHECK refuses the same on insert)."""
    return (not path or path.startswith(("/", "-")) or "\\" in path or ".." in path.split("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in path))


def git(cwd, *args, timeout=15):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return p.stdout


MAX_BLOB_BYTES = 20 * 1024 * 1024  # a file larger than this is not read (fill-refs, show, /api/source)
BLOB_SECONDS = 15  # how long one blob read may take before the reader is stopped


class BlobReader:
    """Files at commits of one repository folder, read through one `git cat-file --batch` process (started on the
    first read, stopped by close()), with a cache per (commit, path): fill-refs reads each file once however many
    references cite it. read() returns (blob sha, bytes) or raises RuntimeError with a one-line reason; a file over
    MAX_BLOB_BYTES is skipped unread (its bytes are discarded as they arrive, never held)."""

    def __init__(self, d):
        self.d = d
        self.p = None
        self.cache = {}

    def read(self, sha, path):
        key = (sha, path)
        if key not in self.cache:
            try:
                self.cache[key] = self._fetch(sha, path)
            except RuntimeError as e:
                self.cache[key] = e
        got = self.cache[key]
        if isinstance(got, RuntimeError):
            raise got
        return got

    def _start(self):
        if self.p is None:
            try:
                self.p = subprocess.Popen(["git", "-C", str(self.d), "cat-file", "--batch"], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                raise RuntimeError(f"git could not start: {e}")

    def _dead(self):
        err = b""
        try:
            self.p.kill()
            err = self.p.communicate(timeout=5)[1] or b""
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self.p = None
        return RuntimeError(err.decode("utf-8", "replace").strip().splitlines()[-1:][0] if err.strip()
                            else "git cat-file stopped")

    def _exact(self, n, keep=True):
        out, left = [], n
        while left:
            chunk = self.p.stdout.read(min(left, 1 << 20))
            if not chunk:
                raise self._dead()
            if keep:
                out.append(chunk)
            left -= len(chunk)
        return b"".join(out)

    def _fetch(self, sha, path):
        self._start()
        guard = threading.Timer(BLOB_SECONDS, lambda p=self.p: p.kill())
        guard.start()
        try:
            try:
                self.p.stdin.write(f"{sha}:{path}\n".encode("utf-8"))
                self.p.stdin.flush()
            except OSError:
                raise self._dead()
            head = self.p.stdout.readline()
            if not head:
                raise self._dead()
            parts = head.decode("utf-8", "replace").rstrip("\n").rsplit(" ", 2)
            if len(parts) != 3 or not parts[2].isdigit():  # "<name> missing" / "<name> ambiguous"
                raise RuntimeError(f"{path} is not in commit {sha[:10]}")
            blob, kind, size = parts[0], parts[1], int(parts[2])
            too_big = size > MAX_BLOB_BYTES
            data = self._exact(size, keep=kind == "blob" and not too_big)
            self._exact(1, keep=False)  # the newline after the content
            if kind != "blob":
                raise RuntimeError(f"{path} is a {kind} at {sha[:10]}, not a file")
            if too_big:
                raise RuntimeError(f"the file is {size:,} bytes, over the {MAX_BLOB_BYTES:,}-byte limit; not read")
            return blob, data
        finally:
            guard.cancel()

    def close(self):
        if self.p is not None:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                self.p.kill()
            for f in (self.p.stdout, self.p.stderr):
                try:
                    f.close()
                except OSError:
                    pass
            self.p = None


def file_at(root, repo_path, sha, path, allow_outside=False, raw=False):
    """The file's text at commit sha (raw=True: its bytes). A refused path gives the fixed error 'bad path' before
    git runs, so no message says whether some file outside the repository exists. A file over MAX_BLOB_BYTES is
    refused (RuntimeError)."""
    if not HEX.match(sha or ""):
        raise RuntimeError("commit is not a hex hash")
    if bad_location_path(path or ""):
        raise RuntimeError("bad path")
    reader = BlobReader(repo_dir(root, repo_path, allow_outside))
    try:
        data = reader.read(sha, path)[1]
    finally:
        reader.close()
    return data if raw else data.decode("utf-8", "replace")


def location(con, loc_id):
    loc = one(con, """SELECT fl.*, rp.path AS repo_path, rp.name AS repo_name, rp.permalink_template
                      FROM finding_locations fl JOIN repositories rp ON rp.id = fl.repository_id
                      WHERE fl.id = ?""", (loc_id,))
    if loc is not None:  # allow_outside_root is schema 2; an older ledger read as it is has none (so: not allowed)
        try:
            loc["allow_outside_root"] = con.execute("SELECT allow_outside_root FROM repositories WHERE id=?",
                                                    (loc["repository_id"],)).fetchone()[0]
        except sqlite3.OperationalError:
            loc["allow_outside_root"] = 0
    return loc


# Common secret shapes masked in excerpts by fill-refs (SEC-M7). Each match keeps its name and quotes and has the
# value replaced by REDACTED; line breaks are kept so the excerpt still lines up with line_start..line_end.
REDACTED = "[redacted]"
_SECRET_NAME = r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?key|credentials?)[A-Za-z0-9_.-]*"
SECRET_PATTERNS = [
    # PEM blocks (private keys and the like): the lines between BEGIN and END (or the excerpt's end)
    re.compile(r"(-----BEGIN [A-Z0-9 ]+-----)(.*?)(-----END [A-Z0-9 ]+-----|\Z)", re.S),
    # name = "value" / name: 'value' / "name": "value" (any case), a quoted literal of 4+ characters
    re.compile(r"(?i)(\b" + _SECRET_NAME + r"[\"']?\s*(?:=|:|=>)\s*)([\"'])([^\"'\n]{4,})(\2)"),
    # NAME=value lines of env files and shell (upper-case names), unquoted
    re.compile(r"(?m)^(\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
               r"CREDENTIALS?)[A-Z0-9_]*\s*=\s*)([^\s'\"#][^\s#]{3,})(\s*)$"),
    # well-known key shapes
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                     # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),                 # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                    # Slack tokens
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),      # Stripe
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),                           # API keys of the sk-... shape
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                         # Google API keys
]


def mask_secrets(text):
    """(text with secret-looking values replaced by [redacted], whether anything was masked)."""
    out = text

    def pem(m):
        inner = m.group(2)
        body = "\n".join(REDACTED if l.strip() else l for l in inner.split("\n"))
        return m.group(1) + body + m.group(3)

    out = SECRET_PATTERNS[0].sub(pem, out)
    out = SECRET_PATTERNS[1].sub(lambda m: m.group(1) + m.group(2) + REDACTED + m.group(4), out)
    out = SECRET_PATTERNS[2].sub(lambda m: m.group(1) + REDACTED + m.group(3), out)
    for p in SECRET_PATTERNS[3:]:
        out = p.sub(REDACTED, out)
    return out, out != text


def source(root, con, loc_id, context=8):
    loc = location(con, loc_id)
    if not loc:
        return {"error": "no such reference"}
    out = {k: loc[k] for k in ("path", "commit_sha", "line_start", "line_end", "symbol",
                               "excerpt", "permalink", "repo_name", "blob_sha")}
    a, b = loc["line_start"], loc["line_end"] or loc["line_start"]
    # One command for show, /api/source and the viewer: v_code_refs builds it with every argument shell-quoted.
    r = con.execute("SELECT git_command FROM v_code_refs WHERE location_id=?", (loc_id,)).fetchone()
    out["git_command"] = r[0] if r else None
    out["excerpt_redacted"] = bool(loc.get("excerpt_redacted"))
    try:
        text = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"],
                       bool(loc.get("allow_outside_root"))).splitlines()
        lo = max(1, (a or 1) - context)
        hi = min(len(text), (b or a or min(len(text), 40)) + context)
        out["lines"] = [{"n": i, "text": text[i - 1]} for i in range(lo, hi + 1)]
        out["source"] = "git"
        if loc["excerpt"] is not None and a:
            real = "\n".join(text[a - 1:b])
            if loc.get("excerpt_redacted"):  # the excerpt is masked; its hash is of the real lines
                out["excerpt_matches"] = hashlib.sha256(real.encode()).hexdigest() == loc["excerpt_sha256"]
            else:
                out["excerpt_matches"] = real == loc["excerpt"].rstrip("\n")
    except Exception as e:  # history rewritten, repo missing, or not a git checkout
        out["git_error"] = str(e)
        out["source"] = "excerpt" if loc["excerpt"] is not None else "none"
        if loc["excerpt"] is not None and a:
            out["lines"] = [{"n": a + i, "text": t} for i, t in enumerate(loc["excerpt"].split("\n"))]
    return out


WHOLE_FILE_EXCERPT_LINES = 200  # a whole-file reference keeps at most this many lines (excerpt_status 'truncated')
_DEFINITION = (r"(?i:\b(?:def|class|function|interface|struct|enum|fn|func|"
               r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|VIEW|TRIGGER|INDEX)(?:\s+IF\s+NOT\s+EXISTS)?))\s+")


def symbol_line(lines, symbol):
    """(line number, None) for the one line that defines symbol, else (None, why). A definition is a def, class,
    function, interface, struct, enum, fn, func or SQL CREATE TABLE/VIEW/TRIGGER/INDEX followed by the symbol as a
    whole word; with no definition, the one line holding the symbol as a whole word. Several candidates, or none,
    leave the reference unlined (the caller counts it as failed)."""
    word = r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])"
    hits = [i for i, t in enumerate(lines, 1) if re.search(_DEFINITION + word, t)]
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, (f"symbol {symbol!r} is defined on {len(hits)} lines "
                      f"({', '.join(map(str, hits[:5]))}); give the lines")
    hits = [i for i, t in enumerate(lines, 1) if re.search(word, t)]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"symbol {symbol!r} not found"
    return None, (f"symbol {symbol!r} is on {len(hits)} lines ({', '.join(map(str, hits[:5]))}) and defined on "
                  f"none; give the lines")


def permalink_for(template, sha, path, a, b):
    """The permalink from repositories.permalink_template, or None. With no lines (a whole file), the #fragment that
    holds {start} or {end} is dropped and any other {start}/{end} is left out of the link."""
    link = template.replace("{sha}", sha).replace("{path}", path)
    if a is None:
        head, sep, frag = link.partition("#")
        if "{start}" in frag or "{end}" in frag:
            link = head
        if "{start}" in link or "{end}" in link:
            return None
        return link
    return link.replace("{start}", str(a)).replace("{end}", str(b or a))


def fill_refs(root, con, review_code=None):
    """Fill excerpt, excerpt_sha256, blob_sha and permalink (and lines from a symbol) of each reference from git.
    Never overwrites an excerpt. A failure (file unreadable or over MAX_BLOB_BYTES, lines past the end, a symbol not
    found or ambiguous, an empty file) writes nothing for that reference and is counted; returns the count.
    Idempotent: a reference is selected only when something can still be filled (a missing permalink only when its
    repository has a permalink template), only columns whose value changes are written, and 'filled N' counts the
    references that changed. Files are read through one `git cat-file --batch` per repository (BlobReader), each
    (commit, path) once."""
    sql = """SELECT fl.id FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
             JOIN reviews r ON r.id = f.review_id JOIN repositories rp ON rp.id = fl.repository_id
             WHERE fl.commit_sha IS NOT NULL
               AND ((fl.excerpt IS NULL AND fl.excerpt_status IS NOT 'not-text') OR fl.blob_sha IS NULL
                    OR (fl.permalink IS NULL AND rp.permalink_template IS NOT NULL)
                    OR (fl.line_start IS NULL AND fl.whole_file = 0))"""
    args = ()
    if review_code:
        sql += " AND r.code = ?"; args = (review_code,)
    failed = 0
    updates = []  # every git read happens first; the writes then go in one short transaction (DB-M8)
    readers = {}  # one BlobReader per repository folder
    try:
        for (lid,) in con.execute(sql, args).fetchall():
            loc = location(con, lid)
            where = f"ref {lid} {loc['path']}@{loc['commit_sha'][:10]}"
            outside = bool(loc["allow_outside_root"])
            try:
                if not HEX.match(loc["commit_sha"] or ""):
                    raise RuntimeError("commit is not a hex hash")
                if bad_location_path(loc["path"] or ""):
                    raise RuntimeError("bad path")
                d = repo_dir(root, loc["repo_path"], outside)
                reader = readers.get(d) or readers.setdefault(d, BlobReader(d))
                blob, data = reader.read(loc["commit_sha"], loc["path"])
            except Exception as e:
                print(f"{where}: {e}"); failed += 1; continue
            seen = working_tree_bytes(root, con, loc, data)  # a dirty review: the text its reviewers saw (D-14)
            if seen is not None and loc["excerpt"] is None:
                data = seen
            try:
                text = None if b"\0" in data else data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            a, b = loc["line_start"], loc["line_end"]
            if a is None and b is not None:
                print(f"{where}: line_end {b} without line_start"); failed += 1; continue
            whole = bool(loc["whole_file"]) or (a is None and not loc["symbol"])
            upd = {"whole_file": 1} if whole and not loc["whole_file"] else {}
            if text is None:  # binary or not UTF-8: no lossy excerpt; the hash is of the file's bytes
                if loc["excerpt"] is None and loc["excerpt_status"] != "not-text":
                    upd.update(excerpt_status="not-text", excerpt_sha256=hashlib.sha256(data).hexdigest())
                if a is None and not whole:
                    print(f"{where}: not a text file; symbol {loc['symbol']!r} cannot be looked up"); failed += 1
                    continue
            else:
                lines = text.splitlines()
                if whole and a is None:
                    if not lines:
                        print(f"{where}: the file is empty; nothing to cite"); failed += 1; continue
                    a, b = 1, len(lines)
                elif a is None:
                    a, why = symbol_line(lines, loc["symbol"])
                    if a is None:
                        print(f"{where}: {why}"); failed += 1; continue
                    b = a
                b = b or a
                if b > len(lines):
                    print(f"{where}: lines {a}-{b} are past the end of the file ({len(lines)} lines)")
                    failed += 1; continue
                upd.update(line_start=a, line_end=b)
                if loc["excerpt"] is None:
                    cut = whole and b - a + 1 > WHOLE_FILE_EXCERPT_LINES
                    excerpt = "\n".join(lines[a - 1:(a - 1 + WHOLE_FILE_EXCERPT_LINES) if cut else b])
                    shown, masked = mask_secrets(excerpt)
                    upd.update(excerpt=shown, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
                               excerpt_redacted=1 if masked else 0)
                    if cut:
                        upd["excerpt_status"] = "truncated"
            if loc["blob_sha"] is None:
                upd["blob_sha"] = blob
            if loc["permalink"] is None and loc["permalink_template"]:
                link = permalink_for(loc["permalink_template"], loc["commit_sha"], loc["path"],
                                     None if whole else a, None if whole else b)
                if link and SAFE_URL.match(link):
                    upd["permalink"] = link
                else:
                    print(f"{where}: the permalink template does not give an http(s) link; no permalink written")
            if seen is not None and loc["excerpt"] is None and "excerpt" in upd:
                upd["source"] = "working-tree"
            upd = {k: v for k, v in upd.items() if loc.get(k) != v}  # only what changes is written and counted
            if upd:
                updates.append((lid, upd))
    finally:
        for r in readers.values():
            r.close()
    if updates:
        if not con.in_transaction:  # a caller's own open transaction is simply continued
            con.execute("BEGIN IMMEDIATE")
        try:
            for lid, upd in updates:
                con.execute(f"UPDATE finding_locations SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                            (*upd.values(), lid))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    print(f"filled {len(updates)} references, {failed} could not be read from git")
    return failed


def existing_tag(d, tag):
    """The commit a tag points at in repository d, or None when there is no such tag. Any other git problem (not a
    repository, git missing) raises RuntimeError."""
    if not d.is_dir():
        raise RuntimeError(f"no folder {d}")
    git(d, "rev-parse", "--git-dir")  # raises when d is not a git repository
    p = subprocess.run(["git", "-C", str(d), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                       capture_output=True, timeout=15)
    if p.returncode == 0:
        return p.stdout.decode().strip()
    if p.returncode == 1 and not p.stdout.strip():
        return None
    raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "git rev-parse failed")


def tag_review(root, con, code):
    """Create the local annotated tag review/<code> on each reviewed commit and record it. Tags stay local
    (D-0002): nothing is pushed. Returns 1 when a repository is left untagged, else 0."""
    if not REVIEW_CODE.fullmatch(code or ""):
        sys.exit(f"tag-review: {code!r} is not a review code like R-0001")
    rv = one(con, "SELECT id FROM reviews WHERE code=?", (code,))
    if not rv:
        sys.exit(f"no review {code}")
    tag = f"review/{code}"
    repos = rows(con, """SELECT rr.repository_id, rr.commit_sha, rp.path, rp.name, rp.allow_outside_root
                         FROM review_repositories rr JOIN repositories rp ON rp.id = rr.repository_id
                         WHERE rr.review_id=? ORDER BY rp.id""", (rv["id"],))
    if not repos:
        sys.exit(f"review {code} has no repositories; nothing to tag (record them in review_repositories)")
    left = 0
    for r in repos:
        sha = r["commit_sha"] or ""
        if not FULL_HEX.fullmatch(sha):
            print(f"{r['name']}: commit {sha!r} is not a full commit hash; not tagged", file=sys.stderr)
            left += 1; continue
        try:
            d = repo_dir(root, r["path"], bool(r["allow_outside_root"]))
            existing = existing_tag(d, tag)
            if existing and existing != sha:
                print(f"{r['name']}: tag {tag} already points at {existing[:10]}; left alone")
                left += 1; continue
            if not existing:
                git(d, "tag", "-a", "-m", f"Commit reviewed in {code}", "--", tag, sha)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"{r['name']}: not tagged: {e}", file=sys.stderr)
            left += 1; continue
        # recorded right away, in its own short transaction, so a later repository's failure cannot lose it; an
        # empty commit_time is filled from git, in UTC like every time in the ledger
        try:
            when = utc_time(git(d, "show", "-s", "--format=%cI", sha).decode().strip())
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            when = None
        con.execute("UPDATE review_repositories SET tag_name=?, commit_time=COALESCE(commit_time, ?) "
                    "WHERE review_id=? AND repository_id=?", (tag, when, rv["id"], r["repository_id"]))
        con.commit()
        hint = f"git -C {shlex.quote(r['path'] or '.')} push origin {shlex.quote(tag)}"
        print(f"{r['name']}: {tag} -> {sha[:10]}  (push with: {hint})")
    if left:
        print(f"tag-review {code}: {left} of {len(repos)} repositories not tagged", file=sys.stderr)
    return 1 if left else 0


def check_git_refs(root, con, quiet=False):
    """Part of `check`: each reviewed commit must exist in its repository (git cat-file -e) and each recorded review
    tag must point at it (refs/tags/<tag>). A repository folder that is not here (the ledger was copied elsewhere)
    is noted and not counted. Returns the number of problems."""
    bad = skipped = 0
    try:
        found = rows(con, """SELECT rv.code, rp.name, rp.path, rp.allow_outside_root, rr.commit_sha, rr.tag_name
                             FROM review_repositories rr JOIN reviews rv ON rv.id = rr.review_id
                             JOIN repositories rp ON rp.id = rr.repository_id ORDER BY rv.id, rp.id""")
    except sqlite3.Error as e:  # an older schema (check_schema has already reported it)
        print(f"    note: reviewed commits and tags were not checked ({e})")
        return 0
    for r in found:
        try:
            d = repo_dir(root, r["path"], bool(r["allow_outside_root"]))
            if not d.is_dir():
                raise FileNotFoundError(d)
            git(d, "rev-parse", "--git-dir")
        except (RuntimeError, OSError, subprocess.SubprocessError):
            skipped += 1
            continue
        what = f"{r['code']} {r['name']} {r['commit_sha'][:10]}"
        try:
            git(d, "cat-file", "-e", f"{r['commit_sha']}^{{commit}}")
        except (RuntimeError, subprocess.SubprocessError):
            print(f"git: {what}: the reviewed commit is not in the repository"); bad += 1
            continue
        if r["tag_name"]:
            try:
                at = existing_tag(d, r["tag_name"])
            except (RuntimeError, subprocess.SubprocessError) as e:
                at = f"unreadable ({e})"
            if at != r["commit_sha"]:
                print(f"git: {what}: tag {r['tag_name']} " + ("is missing" if at is None else f"points at {at[:10]}"))
                bad += 1
    if skipped and not quiet:
        print(f"    note: {skipped} reviewed repositories are not here; their commits and tags were not checked")
    elif not bad and not quiet:
        print("    0  Reviewed commits missing or review tags moved")
    return bad


TEXT_TARGETS = {"set-report": ("reviews", "report_md", "review", REVIEW_CODE),
                "set-narrative": ("plans", "narrative_md", "plan", re.compile(r"P-[0-9]{4,}"))}


def set_text(con, cmd, code, source):
    """set-report R-0007 FILE / set-narrative P-0001 FILE: store the review document or the plan's narrative
    (Markdown, UTF-8; FILE '-' reads standard input) in the ledger, where the viewer shows it. Returns 0."""
    table, column, what, pattern = TEXT_TARGETS[cmd]
    if not pattern.fullmatch(code or ""):
        sys.exit(f"{cmd}: {code!r} is not a {what} code")
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"{cmd}: cannot read {source}: {e}")
    cur = con.execute(f"UPDATE {table} SET {column}=? WHERE code=?", (text, code))
    if cur.rowcount != 1:
        con.rollback()
        sys.exit(f"{cmd}: no {what} {code}")
    con.commit()
    print(f"{code}: stored {len(text):,} characters in {table}.{column}")
    return 0


DUMP_HEADER = "-- ledger.py dump: a text copy of the ledger for diffs; rebuild with: sqlite3 new.sqlite < ledger.sql"


def dump(con, db):
    """Write <db>.sql: one consistent snapshot (a read transaction around iterdump), written to <db>.sql.tmp,
    fsynced and renamed over the old dump, so a failed dump leaves the previous one intact. A WAL ledger is
    checkpointed first so the .sqlite file itself holds every commit. Refuses to overwrite a .sql file that this
    command did not write (its first line is not DUMP_HEADER, nor iterdump's own first line, which older dumps had)."""
    out = db.with_suffix(".sql")
    if out.exists():
        with open(out, encoding="utf-8", errors="replace") as f:
            first = f.readline().rstrip("\n")
        if first not in (DUMP_HEADER, "BEGIN TRANSACTION;"):
            sys.exit(f"{out} was not written by ledger.py dump (its first line is not the dump header); left it "
                     f"unchanged. Move it aside, or pass --db for a ledger whose .sql name is free.")
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass  # a reader elsewhere: the dump below is still complete
    tmp = out.with_name(out.name + ".tmp")
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN")
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()  # the snapshot starts here
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(DUMP_HEADER + "\n")
            for line in con.iterdump():
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(out))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        sys.exit(f"dump: could not write {out} ({e}); the previous dump is unchanged")
    finally:
        con.rollback()
    print(f"wrote {out}")


# ---------------------------------------------------------------- viewer API

LABEL_TABLES = ("domains", "themes")
# The lookup tables of the ledger's words (schema 6), by the name their labels have in /api/overview labels.enums.
LOOKUP_TABLES = (("issue_status", "issue_statuses"), ("severity", "severities"), ("match_type", "match_types"),
                 ("relation_type", "relation_types"), ("gate_kind", "gate_kinds"))
ACTIVE = "(SELECT value FROM issue_statuses WHERE active = 1)"  # issue statuses still to fix


def enum_labels(con):
    """{enum: {value: label}} in display order, from the lookup tables and enum_labels (plus relation_type_inverse,
    the labels read from the issue a relation points to). Empty for a ledger older than schema 6: the page then
    falls back to its own wording."""
    out = {}
    try:
        for name, table in LOOKUP_TABLES:
            out[name] = {r[0]: r[1] for r in con.execute(f"SELECT value, label FROM {table} ORDER BY position, value")}
        out["relation_type_inverse"] = {r[0]: r[1] for r in con.execute(
            "SELECT value, inverse_label FROM relation_types ORDER BY position, value")}
        for enum, value, label in con.execute("SELECT enum, value, label FROM enum_labels ORDER BY enum, position"):
            out.setdefault(enum, {})[value] = label
    except sqlite3.Error:
        return {}
    return out


ROUTE_SECONDS = 3  # a viewer page read that runs longer is stopped (the SQL box has its own SQL_SECONDS)
ANSWER_BUSY_MS = 3000  # how long a gate answer waits for another writer before it reports the ledger busy
TOO_LONG = "The query took too long (over {} s); add a LIMIT or a WHERE."


def with_deadline(con, seconds):
    """Stop any statement on con that runs past `seconds` from now (it raises OperationalError 'interrupted').
    Returns a function that tells whether the deadline was what stopped it."""
    from time import monotonic
    end = monotonic() + seconds
    con.set_progress_handler(lambda: 1 if monotonic() > end else 0, 10_000)
    return lambda: monotonic() > end


def api(root, db, path, qs, body):
    """One viewer API request. Each request opens its own connection and closes it when the answer is built:
    sqlite3 connections are not shared between the server's threads, and a fresh one always sees the agent's latest
    commit. That connection is read-only (mode=ro, query_only) for every route and stops after ROUTE_SECONDS; only
    the gate answer route opens a second, writable connection of its own. A missing ledger is a 503 and is never
    created."""
    if not Path(db).exists():
        return {"error": "ledger not found"}, 503
    con = connect(db, readonly=True)
    timed_out = with_deadline(con, ROUTE_SECONDS)
    try:
        return route(root, db, con, path, qs, body)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e) and timed_out():
            return {"error": TOO_LONG.format(ROUTE_SECONDS)}, 400
        raise
    finally:
        con.close()


def gate_answer_request(db, code, body, via="viewer"):
    """The owner's answer to an open gate from the viewer (POST /api/gates/<code>/answer): answer_gate() on a
    writable connection of its own, so the viewer and `ledger.py answer-gate` apply exactly the same rules. Two
    answers cannot both be reported saved: the loser, or an answer to a gate answered meanwhile (in chat, say), gets
    409. If another writer holds the ledger past ANSWER_BUSY_MS the answer is not saved and the owner is told to try
    again (503); the page keeps what they typed. Returns {"ok": True} or (error, status)."""
    busy = {"error": "The ledger is busy; your answer was not saved, try again."}, 503
    try:
        w = connect(db)
    except sqlite3.OperationalError:
        return {"error": "ledger not found"}, 503
    w.isolation_level = None
    try:
        w.execute(f"PRAGMA busy_timeout = {int(ANSWER_BUSY_MS)}")
        return answer_gate(w, code, body.get("answer_value"), body.get("answer_text"), via)
    except GateRefused as e:
        return {"error": str(e)}, e.status
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            return busy
        raise
    finally:
        w.close()


def quote_name(name):
    return '"' + str(name).replace('"', '""') + '"'


def route(root, db, con, path, qs, body):
    parts = [x for x in path.split("/") if x]
    if not parts or parts[0] != "api":
        return {"error": "not found"}, 404
    p = parts[1:]
    arg = lambda k, d=None: qs.get(k, [d])[0]

    if p == ["overview"]:
        meta = {r["key"]: r["value"] for r in rows(con, "SELECT key, value FROM meta")}
        return {
            "project": meta.get("project_name", root.name),
            "schema_version": meta.get("schema_version"),
            "open_by_severity": rows(con, """SELECT severity, count(*) n FROM issues
                WHERE status IN {ACTIVE} GROUP BY severity""".format(ACTIVE=ACTIVE)),
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
            "checks": [{"label": label, "view": v, "tier": tier, "n": count_view(con, v)}
                       for v, label, tier in (ledger_checks(con) or [])],
            "labels": {**{t: {r["code"]: r["name"] for r in rows(con, f"SELECT code, name FROM {t}")} for t in LABEL_TABLES},
                       "enums": enum_labels(con)},
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
        return gate_answer_request(db, p[1], body)

    if p == ["reviews"]:
        return rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
            (SELECT group_concat(rp.name || ' ' || substr(rr.commit_sha,1,10) || COALESCE(' on ' || rr.branch, ''), ' · ')
               FROM review_repositories rr JOIN repositories rp ON rp.id=rr.repository_id WHERE rr.review_id=r.id) commits,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id) points,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='H') high,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='M') medium,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='L') low,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN ('new','continuation')) new_issues,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN (SELECT value FROM match_types WHERE came_back = 1)) came_back
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
            WHERE f.review_id=? ORDER BY CASE f.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, length(f.point_id), f.point_id""", (rid,))
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
            "agents": rows(con, "SELECT * FROM v_review_agents WHERE review=?", (p[1],)),
            "requirements": rows(con, "SELECT * FROM v_requirement_trace WHERE review=?", (p[1],)),
            "metrics": rows(con, """SELECT m.name, m.scope, m.value, COALESCE(m.unit, md.unit) unit, m.method, m.note,
                COALESCE(md.label, upper(substr(m.name, 1, 1)) || replace(substr(m.name, 2), '_', ' ')) label,
                COALESCE(md.better, 'none') better
                FROM metrics m LEFT JOIN metric_defs md ON md.name=m.name WHERE m.review_id=? ORDER BY m.name, m.scope""", (rid,)),
            "artifacts": rows(con, "SELECT kind, path, url FROM artifacts WHERE review_id=? ORDER BY kind", (rid,)),
            "gates": rows(con, "SELECT code, kind, title, status, answer_value, answer_text FROM gates WHERE review_id=? ORDER BY created_at", (rid,)),
            "findings": findings,
        }

    if p == ["issues"]:
        where, args = ["1"], []
        for k in ("status", "severity", "domain_code"):
            v = arg(k)
            if v == "active" and k == "status":
                where.append(f"i.status IN {ACTIVE}")
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
            ORDER BY CASE i.severity WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END, i.id LIMIT 2000""", args)

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
        for t in rows(con, """SELECT name, type FROM sqlite_master WHERE type IN ('table','view')
                              AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name"""):
            cols = [c["name"] for c in rows(con, "SELECT name FROM pragma_table_info(?)", (t["name"],))]
            n = con.execute(f"SELECT count(*) FROM {quote_name(t['name'])}").fetchone()[0]
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


SQL_SECONDS = 4  # the SQL box stops a query after this long
SQL_MAX_TEXT = 20_000  # characters of SQL text accepted
SQL_MAX_CELL = 10_000  # characters of one text cell sent back; longer text is cut with a note
SQL_MAX_RESPONSE = 5_000_000  # rows stop being added once the JSON passes about this many bytes (truncated=true)
SQL_MAX_ROWS = 1000


def _sql_cell(v):
    if isinstance(v, bytes):
        return {"blob_bytes": len(v)}
    if isinstance(v, str) and len(v) > SQL_MAX_CELL:
        return v[:SQL_MAX_CELL] + f" … [cut: {len(v):,} characters in all]"
    return v


def run_readonly(db, sql):
    """The viewer's SQL box: one SELECT on a read-only connection whose authorizer allows reads only. Bounded by
    SQL_SECONDS of run time, SQL_MAX_TEXT of SQL, SQL_MAX_CELL per text cell (blobs come back as {"blob_bytes": N}),
    SQL_MAX_ROWS rows and about SQL_MAX_RESPONSE bytes of JSON; truncated says rows were left out."""
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return {"error": "Write a query first."}, 400
    if len(sql) > SQL_MAX_TEXT:
        return {"error": f"The query is too long (over {SQL_MAX_TEXT:,} characters)."}, 400
    if not Path(db).exists():
        return {"error": "ledger not found"}, 503
    con = connect(db, readonly=True)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    con.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    timed_out = with_deadline(con, SQL_SECONDS)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        out, size, truncated = [], 2, False
        for r in cur:
            if len(out) >= SQL_MAX_ROWS:
                truncated = True
                break
            row = [_sql_cell(v) for v in r]
            size += len(json.dumps(row, ensure_ascii=False, default=str).encode()) + 1
            if size > SQL_MAX_RESPONSE:
                truncated = True
                break
            out.append(row)
        return {"columns": cols, "rows": out, "truncated": truncated}
    except (sqlite3.Error, sqlite3.Warning) as e:  # before Python 3.11, several statements raise sqlite3.Warning
        if isinstance(e, sqlite3.OperationalError) and "interrupted" in str(e) and timed_out():
            return {"error": TOO_LONG.format(SQL_SECONDS)}, 400
        return {"error": f"The query did not run: {e}"}, 400
    finally:
        con.close()


# ---------------------------------------------------------------- HTTP

REQUEST_TIMEOUT = 10  # seconds a viewer connection may sit idle or half-sent before it is dropped
MAX_BODY = 200_000  # bytes of POST body accepted


def page_csp(page):
    """The viewer page's Content-Security-Policy: its one inline script is allowed by its sha256 (the page is static
    now that the token travels in the URL fragment), not by 'unsafe-inline'."""
    import base64
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(x.encode("utf-8")).digest()).decode()
                      for x in scripts) or "'none'"
    return (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_server(root, db, port=0):
    """Build the viewer's handler and bind it to 127.0.0.1:port (0 = any free port); returns (server, token).
    The caller runs server.serve_forever(). The Host allow-list uses the port actually bound. The page itself holds
    no secret: the token reaches the browser only in the launch URL's #k= fragment (see serve), and every /api/
    request must send it back as X-Ledger-Token."""
    token = secrets.token_urlsafe(32)
    token_bytes = token.encode()
    page = PAGE.encode("utf-8")
    csp = page_csp(PAGE)
    hosts = set()

    class H(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

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
                self.send_header("Content-Security-Policy", csp)
            if code >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "wrong host"}); return False
            return True

        def token_ok(self):
            try:  # headers are decoded as latin-1, so this gives back the bytes that were sent
                sent = self.headers.get("X-Ledger-Token", "").encode("latin-1")
                good = secrets.compare_digest(sent, token_bytes)
            except (UnicodeError, TypeError):
                good = False
            if not good:
                self.send(403, {"error": "missing or wrong token; open the viewer through the link that "
                                         "ledger.py serve printed (it ends in #k=...)"})
            return good

        def handle_api(self, u, body):
            try:
                res = api(root, db, u.path, parse_qs(u.query), body)
            except Exception as e:
                res = ({"error": f"Something went wrong reading the ledger: {e}"}, 500)
            code, res = (res[1], res[0]) if isinstance(res, tuple) else (200, res)
            self.send(code, res)

        def do_GET(self):
            if not self.host_ok():
                return
            u = urlparse(self.path)
            if u.path == "/":
                self.send(200, page, "text/html; charset=utf-8")
            elif u.path.startswith("/api/"):
                if self.token_ok():
                    self.handle_api(u, None)
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.host_ok():
                return
            u = urlparse(self.path)  # the guard below and the dispatch use this one parsed path
            if not u.path.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            if not self.token_ok():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "JSON only"})
            raw_n = self.headers.get("Content-Length")
            if raw_n is None:
                n = 0
            elif not re.fullmatch(r"[0-9]{1,12}", raw_n):
                return self.send(400, {"error": "bad Content-Length"})
            else:
                n = int(raw_n)
            if n > MAX_BODY:
                return self.send(413, {"error": "too large"})
            try:
                data = self.rfile.read(n) if n else b""
            except OSError:  # the client stopped sending (REQUEST_TIMEOUT): drop the connection
                self.close_connection = True
                return
            if len(data) < n:
                return self.send(400, {"error": "the body is shorter than its Content-Length"})
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad JSON"})
            if not isinstance(body, dict):
                return self.send(400, {"error": "Send a JSON object"})
            self.handle_api(u, body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    bound = srv.server_address[1]
    hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    return srv, token


def serve(root, db, port, open_browser):
    """Run the viewer until Ctrl+C. The launch URL carries the token in its #k= fragment (never sent to the server,
    read by the page into sessionStorage and removed from the address bar). The umask is set to 077 first, so any
    file the viewer's connections create beside the ledger (-journal, -wal, -shm) is owner-only."""
    import os
    if hasattr(os, "umask"):
        os.umask(0o077)
    srv, token = make_server(root, db, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/#k={token}"
    print(f"Ledger viewer for {db} at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- owner queue, query, progress, hub, answers

def print_gates(con):
    """`ledger.py gates`: the owner's whole queue (v_owner_queue), the start-of-session check. Open and answered gates
    first, in the order they were asked ('no open gates' when there are none), then steps waiting on the owner,
    open decisions, and stale steps (their issues are all closed)."""
    gates = rows(con, "SELECT * FROM gates WHERE status IN ('open','answered') ORDER BY created_at, id")
    if not gates:
        print("no open gates")
    for g in gates:
        ans = f" -> {g['answer_value'] or ''} {g['answer_text'] or ''}".rstrip() if g["status"] == "answered" else ""
        print(f"{g['code']}  [{g['status']}] {g['kind']}: {g['title']}{ans}")
    try:
        other = rows(con, "SELECT * FROM v_owner_queue WHERE kind <> 'gate' ORDER BY kind DESC, created_at, ref")
    except sqlite3.Error:
        other = []  # an older ledger (gates warned already): migrate adds the view
    for o in other:
        if o["kind"] == "step":
            print(f"step {o['ref']} [{o['need']}] {o['title']}: {o['question']}")
        else:
            print(f"decision {o['ref']} [{o['status']}] {o['title']}"
                  + (f" (revisit when: {o['question']})" if o["question"] else ""))
    try:
        stale = rows(con, "SELECT * FROM v_stale_steps ORDER BY plan, step")
    except sqlite3.Error:
        stale = []
    for s in stale:
        print(f"stale step {s['plan']} #{s['step']} [{s['status']}] {s['title']}: its issues are all closed "
              f"({s['issues']}); mark it done or dropped")


_QUERY_START = re.compile(r"(?is)^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*(select|with)\b")


def _tsv(v):
    if v is None:
        return ""
    if isinstance(v, dict) and "blob_bytes" in v:
        return f"<blob {v['blob_bytes']} bytes>"
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def query_command(db, sql, as_json=False):
    """`ledger.py query [--readonly] [--json] "SQL"`: one SELECT (or WITH ... SELECT) through run_readonly, the
    viewer's SQL box: a read-only connection whose authorizer allows reads only, with its time, row and size limits.
    Prints TSV with a header row (tabs, new lines and backslashes in values written \\t, \\n, \\\\; NULL as nothing)
    or, with --json, {"columns", "rows", "truncated"}. Anything else is refused on stderr with exit 1; the ledger is
    never changed."""
    if not _QUERY_START.match(sql or ""):
        print("query: only one SELECT (or WITH ... SELECT) statement is accepted", file=sys.stderr)
        return 1
    res = run_readonly(db, sql)
    if isinstance(res, tuple):
        print(f"query: {res[0]['error']}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(res, ensure_ascii=False, default=str))
    else:
        print("\t".join(_tsv(c) for c in res["columns"]))
        for r in res["rows"]:
            print("\t".join(_tsv(v) for v in r))
    if res["truncated"]:
        print(f"query: only the first {len(res['rows'])} rows are shown; add a LIMIT or a WHERE", file=sys.stderr)
    return 0


def _md_cell(v):
    return str("" if v is None else v).replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", " ")


def progress_md(con, plan_code):
    """The Markdown progress report of one plan (`ledger.py progress-md P-0001`): the plan's totals, one row per
    phase from v_plan_progress, then each phase's steps (number, status, title, how many review points and issues it
    closes, its progress note). The format is fixed so every agent's PROGRESS.md reads the same. None when there is
    no such plan."""
    pl = one(con, "SELECT * FROM plans WHERE code=?", (plan_code,))
    if not pl:
        return None
    phases = rows(con, "SELECT * FROM v_plan_progress WHERE plan=? ORDER BY phase", (plan_code,))
    steps = rows(con, """SELECT s.number, s.title, s.status, s.progress_note, ph.number phase,
            (SELECT count(*) FROM step_findings sf WHERE sf.step_id=s.id) points,
            (SELECT count(DISTINCT f.issue_id) FROM step_findings sf JOIN findings f ON f.id=sf.finding_id
              WHERE sf.step_id=s.id) issues
            FROM steps s JOIN phases ph ON ph.id=s.phase_id WHERE s.plan_id=? ORDER BY s.number""", (pl["id"],))
    keys = ("steps", "done", "doing", "waiting_owner", "not_started", "dropped")
    tot = {k: sum(p[k] or 0 for p in phases) for k in keys}
    out = [f"# Progress of {pl['code']}: {pl['title']}", "",
           f"Plan status: {pl['status']}. {tot['done']} of {tot['steps']} steps done, {tot['doing']} in progress, "
           f"{tot['waiting_owner']} waiting on the owner, {tot['not_started']} not started, {tot['dropped']} dropped.",
           "", f"Written by ledger.py progress-md from the ledger at {now()}.", "",
           "| Phase | Title | Steps | Done | Doing | Waiting on the owner | Not started | Dropped |",
           "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for p in phases:
        out.append(f"| {p['phase']} | {_md_cell(p['title'])} | " + " | ".join(str(p[k] or 0) for k in keys) + " |")
    out.append(f"| | **All phases** | " + " | ".join(str(tot[k]) for k in keys) + " |")
    by_phase = {}
    for s in steps:
        by_phase.setdefault(s["phase"], []).append(s)
    for p in phases:
        out += ["", f"## Phase {p['phase']}: {_md_cell(p['title'])}", ""]
        if not by_phase.get(p["phase"]):
            out.append("No steps.")
            continue
        out += ["| Step | Status | Title | Closes | Note |", "| ---: | --- | --- | --- | --- |"]
        for s in by_phase[p["phase"]]:
            closes = f"{s['points']} points, {s['issues']} issues" if s["points"] else ""
            out.append(f"| {s['number']} | {s['status']} | {_md_cell(s['title'])} | {closes} | "
                       f"{_md_cell(s['progress_note'])} |")
    return "\n".join(out) + "\n"


# The published hub: the viewer page (PAGE, the same page.html `serve` sends) made into one self-contained file.
# Every API response the page can ask for is computed with route() and embedded as JSON; the page answers from that
# data, routes on artifact-safe hash names (#issue-ISS-0019), shows a How it works guide, and, when it is published as
# a Claude artifact with a db capability, saves the owner's gate answers in that artifact's database (collection
# "answers", one document per gate code). The agent exports those answers and runs `ledger.py import-answers`.
# Nothing in the page makes an outside request; publishing it is the agent's job, outside this tool.
HUB_JS = r'''
const STATIC = window.LEDGER_SNAPSHOT || null;
let CUR_PATH = null;
const ACTIVE = STATIC ? STATIC.active : [];
const slugOf = p => (p || "").split("?")[0].replace(/^#\/?/, "").replace(/[\/_]/g, "-").replace(/[^A-Za-z0-9-]/g, "") || "home";
function pathFromSlug(s){
  s = (s || "").replace(/^#/, "");
  if (!s || s === "home") return STATIC && STATIC.start && CUR_PATH === null ? STATIC.start : "";
  const m = s.match(/^([a-z]+)(?:-(.*))?$/); if (!m) return "";
  const [, view, rest] = m;
  if (!rest) return view;
  if (view === "plan") { const p = rest.match(/^(P-\d+)(?:-phase-(\d+))?$/); return p ? `plan/${p[1]}` + (p[2] ? `/phase/${p[2]}` : "") : "plans"; }
  if (view === "explore") { const t = (STATIC.responses.tables || []).find(t => t.name.replace(/_/g, "-") === rest); return t ? `explore/${t.name}` : "explore"; }
  return `${view}/${rest}`;
}
function nav(hash){ CUR_PATH = hash.replace(/^#\/?/, ""); const s = slugOf(CUR_PATH); if (location.hash.slice(1) === s) route(); else location.hash = s; }
function setPath(hash){ if (!STATIC) { history.replaceState(null, "", hash); return; } CUR_PATH = hash.replace(/^#\/?/, ""); try { history.replaceState(null, "", "#" + slugOf(CUR_PATH)); } catch {} }
function currentPath(){
  if (!STATIC) return location.hash.replace(/^#\/?/, "");
  const s = location.hash.slice(1);
  if (CUR_PATH === null || slugOf(CUR_PATH) !== (s || "home")) CUR_PATH = pathFromSlug(s);
  return CUR_PATH;
}
function staticApi(path, body){
  if (body !== undefined) return Promise.reject(new Error("This shared copy is read-only. Answer in the chat with Claude, or run the local viewer."));
  const S = STATIC.responses, [p, q] = path.split("?"), qs = new URLSearchParams(q || "");
  const copy = v => JSON.parse(JSON.stringify(v));
  if (p === "issues") {
    const st = qs.get("status"), sv = qs.get("severity"), dm = qs.get("domain_code"), t = (qs.get("q") || "").toLowerCase();
    return Promise.resolve(copy(S.issues.filter(i => (!st || (st === "active" ? ACTIVE.includes(i.status) : i.status === st))
      && (!sv || i.severity === sv) && (!dm || i.domain_code === dm) && (!t || i.search.toLowerCase().includes(t)))));
  }
  if (p === "gates") return Promise.resolve(copy(S["gates?status=" + (qs.get("status") || "pending")] || S["gates?status=all"]));
  if (p.startsWith("table/")) {
    const t = S[p]; if (!t) return Promise.reject(new Error("That table is not in this snapshot."));
    const off = Number(qs.get("offset")) || 0, lim = Number(qs.get("limit")) || 100;
    return Promise.resolve({columns: t.columns, rows: copy(t.rows.slice(off, off + lim)), count: t.count, offset: off, limit: lim});
  }
  if (S[p] === undefined) return Promise.reject(new Error("This view is not in the snapshot."));
  return Promise.resolve(copy(S[p]));
}

const HUB = {db: null, answers: {}, error: null};
const OPEN_GATES = () => STATIC ? (STATIC.responses["gates?status=pending"].gates || []).filter(g => g.status === "open").map(g => g.code) : [];
const hubAnsweredOpen = () => OPEN_GATES().filter(c => HUB.answers[c]).length;
async function hubInit(){
  if (!STATIC || !window.claude || typeof window.claude.use !== "function") return;
  try {
    const db = await window.claude.use("db"); if (!db) return;
    HUB.db = db;
    db.collection("answers").onSnapshot(snap => {
      HUB.answers = Object.fromEntries(snap.docs.map(d => [d.id, d.data()]));
      const cur = document.querySelector('#tabs a[aria-current="page"]');
      if (OVERVIEW) drawTabs(cur ? (cur.dataset.k || "") : "");
    }, e => { HUB.error = e && e.code; });
  } catch (e) { HUB.error = String(e); }
}
function md(text){
  const lines = String(text || "").split("\n"), out = []; let i = 0;
  const inline = str => { const parts = []; const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g; let last = 0, m;
    while ((m = re.exec(str))) { if (m.index > last) parts.push(str.slice(last, m.index)); const t = m[0];
      if (t.startsWith("**")) parts.push(h("b", null, t.slice(2, -2)));
      else if (t.startsWith("`")) parts.push(h("code", null, t.slice(1, -1)));
      else { const mm = t.match(/^\[([^\]]+)\]\((.+)\)$/); parts.push(h("a", {href: mm[2], target: "_blank", rel: "noopener noreferrer"}, mm[1])); }
      last = re.lastIndex; }
    if (last < str.length) parts.push(str.slice(last)); return parts; };
  const cells = r => r.replace(/^\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
  while (i < lines.length) {
    const l = lines[i]; let m;
    if (!l.trim()) { i++; continue; }
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { out.push(h("h" + Math.min(m[1].length + 1, 4), null, inline(m[2]))); i++; continue; }
    if (l.startsWith("|")) { const rows = []; while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      out.push(h("div", {class: "scroll"}, h("table", null, h("thead", null, h("tr", null, cells(rows[0]).map(c => h("th", null, inline(c))))),
        h("tbody", null, rows.slice(2).map(r => h("tr", null, cells(r).map(c => h("td", null, inline(c))))))))); continue; }
    if (/^(\d+\.|-)\s/.test(l)) { const ordered = /^\d+\./.test(l), items = []; while (i < lines.length && /^(\d+\.|-)\s/.test(lines[i])) items.push(lines[i++].replace(/^(\d+\.|-)\s+/, ""));
      out.push(h(ordered ? "ol" : "ul", null, items.map(t => h("li", null, inline(t))))); continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\||\d+\.\s|-\s)/.test(lines[i])) para.push(lines[i++]);
    out.push(h("p", null, inline(para.join(" "))));
  }
  return h("div", {class: "md"}, out);
}
function hubGateBody(card, g){
  const saved = HUB.answers[g.code];
  const optLab = v => ((g.options || []).find(o => o.value === v) || {}).label || v;
  if (saved) { add(card, h("p", {class: "status"}, "You answered: ", saved.answer_value ? h("b", null, optLab(saved.answer_value)) : null,
      saved.answer_text ? (saved.answer_value ? " — " : "") + saved.answer_text : "", ". Claude records it the next time it checks (or tell Claude in the chat to check now).")); return card; }
  if (!HUB.db) { add(card, h("p", {class: "note"}, "Answer in the chat with Claude; answering here needs you signed in to claude.ai as this page's owner.")); return card; }
  const nm = "opt-" + g.code;
  const opts = (g.options || []).length ? h("div", {class: "opts", role: "radiogroup", "aria-label": "Choices"},
    g.options.map(o => h("label", null, h("input", {type: "radio", name: nm, value: o.value}), h("span", null, o.label, o.description ? h("small", null, o.description) : null)))) : null;
  const ta = g.allow_text ? h("textarea", {placeholder: opts ? "Add a note or a different answer (optional)" : "Your answer", "aria-label": "Your answer"}) : null;
  const msg = h("div"), form = h("div", {class: "gate"});
  const send = h("button", {class: "btn", type: "button", onclick: async () => {
    const picked = card.querySelector(`input[name="${nm}"]:checked`), val = picked ? picked.value : null, txt = ta ? ta.value.trim() : "";
    if (!val && !txt) { put(msg, h("p", {class: "status err"}, "Choose an option or write an answer.")); return; }
    send.disabled = true;
    try {
      await HUB.db.doc("answers/" + g.code).set({gate: g.code, answer_value: val, answer_text: txt || null, answered_at: new Date().toISOString()});
      HUB.answers[g.code] = {answer_value: val, answer_text: txt || null};
      put(form, h("p", {class: "status"}, "Saved. Claude records it the next time it checks (or tell Claude in the chat to check now)."));
      drawTabs("inbox");
    } catch (e) { put(msg, h("p", {class: "status err"}, "Your answer could not be saved here" + (e && e.code === "invalid_argument" ? ": only this page's owner can answer." : ". Try again, or answer in the chat."))); send.disabled = false; }
  }}, "Send answer");
  add(form, opts, ta, h("div", null, send), msg); add(card, form); return card;
}

if (STATIC) document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest('a[href^="#/"]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey) return;
  e.preventDefault(); nav(a.getAttribute("href"));
});
'''

HUB_GUIDE_VIEW = r'''
VIEWS.guide = async () => {
  const o = OVERVIEW || await api("overview");
  const plans = await api("plans"), revs = await api("reviews");
  const r = revs[0], p = plans[0], waiting = Math.max(0, o.queue - (STATIC ? hubAnsweredOpen() : 0));
  setView(
    h("h1", null, "How it works"),
    h("p", {class:"lede"}, "What this site is, how a review runs, what the words mean, and where things stand right now."),
    h("div", {class:"card"}, h("h2", null, "Where things stand"),
      kv([["Latest review", r ? h("a", {href:"#/review/" + r.code}, `${r.code} · ${lab("review", r.status)} · ${r.points} points`) : "None yet"],
          ["Fix plan", p ? h("a", {href:"#/plan/" + p.code}, `${p.code} · ${lab("plan", p.status)} · ${p.done || 0} of ${p.steps || 0} steps done`) : "None yet"],
          ["Waiting on you", waiting ? h("a", {href:"#/inbox"}, `${waiting} ${waiting === 1 ? "question" : "questions"}`) : "Nothing right now"],
          ["Updated", when(STATIC ? STATIC.taken : null)]])),
    md(STATIC ? STATIC.guide : ""));
};
'''

HUB_GUIDE_MD = """## What this site is

This is the review ledger for this project, shown as a website. Every review, every point it raised, the issues those points describe, the fix plan, and every question waiting on you live here. Claude updates the site after each step of a review and after each batch of fixes; the date at the top says when.

You can answer every question here, under **Waiting on you**. Your answer is saved with this page; Claude records it the next time it checks, or right away if you tell it in the chat.

## How a review runs

1. **Goals and areas.** Claude reads the project, proposes the design goals the code is judged against and the areas to review (security, tests, the viewer, and so on), and asks you to approve them. Nothing runs before you do.
2. **Reviewers.** One reviewer per approved area reads its part of the code, read-only, and writes a report.
3. **Checking.** Claude checks every serious point against the code itself and records which were confirmed and which were wrong.
4. **Points into issues.** Every point of every report is recorded here, pinned to the exact lines at the reviewed commit, and grouped into issues: one issue per underlying problem, however many reviewers described it. Issues found by earlier reviews are matched, so a problem that comes back is marked as having come back.
5. **Report.** The review's report (on the review's page) gives the verdict, the root causes, what to fix first, and how each of your requirements is met.
6. **Fix plan.** Every point is placed in a step; steps are grouped into phases that build on each other. You approve the plan and any choices it needs.
7. **Fixing.** Claude works through the steps in order. Each fix comes with a test that fails before it and passes after; each step's note says in plain words what changed.
8. **Second review.** The skill runs again on the fixed code and confirms each fix; only then is an issue marked fixed and confirmed.

## Words used here

| Word | Meaning |
| --- | --- |
| Point | One thing one reviewer raised in one review |
| Issue | The underlying problem, followed across reviews; several points can describe one issue |
| Severity | High, Medium, Low, or Missing test |
| Question (gate) | Something Claude cannot get past without you; it waits until you answer |
| Decision | Your answer, kept so later reviews don't ask again |
| Plan, phase, step | The fix plan; its stages, in order; one change with its proof |
| Commit | The exact version of the code a review looked at |

## Where to look

**Overview** shows the totals and the ledger's own checks. **Reviews** lists every review; open one for its report, the areas reviewed and skipped, the reviewers, and every point with the code it cites. **Issues** lets you search and filter every issue and see its full history. **Fix plans** shows every step by phase, with what it changes, how it is proven, and which points it closes. **Over time** shows how the numbers move between reviews. **Explore** shows the raw tables behind everything.
"""

# (text in page.html, its replacement): each must occur exactly once; hub_page refuses to build otherwise, so an edit
# of page.html that moves one of these fails test_hub_patches_apply instead of shipping a broken hub.
HUB_PATCHES = [
    ('takeToken();\nlet LABELS', 'takeToken();\n' + HUB_JS + '\nlet LABELS'),
    ('async function api(path, body){', 'async function api(path, body){\n  if (STATIC) return staticApi(path, body);'),
    ('function go(hash){ location.hash = hash; }', 'function go(hash){ if (STATIC) return nav(hash); location.hash = hash; }'),
    ('history.replaceState(null, "", `#/plan/${code}/phase/${phase}`)', 'setPath(`#/plan/${code}/phase/${phase}`)'),
    ('history.replaceState(null, "", "#/explore/" + t);', 'setPath("#/explore/" + t);'),
    ('const raw = location.hash.replace(/^#\\/?/, "");', 'const raw = currentPath();'),
    ('"Make sure the viewer is still running in your terminal, then reload the page."',
     'STATIC ? "This is a read-only snapshot; reload the page to start again." : "Make sure the viewer is still running in your terminal, then reload the page."'),
    ('"Questions and approvals the agent cannot get past without you. Answer here or in the chat; the agent picks answers up the next time it checks the ledger."',
     'STATIC ? "Questions and approvals the agent cannot get past without you. Answer here: your answer is saved with this page, and Claude records it the next time it checks." : "Questions and approvals the agent cannot get past without you. Answer here or in the chat; the agent picks answers up the next time it checks the ledger."'),
    ('  const name = "opt-" + g.code;', '  if (STATIC) return hubGateBody(card, g);\n  const name = "opt-" + g.code;'),
    ('    h("div", {class:"card", style:"display:flex;flex-direction:column;gap:8px"}, h("b", null, "Query"),',
     '    STATIC ? h("p", {class:"note"}, "The query box needs the local viewer (python3 ledger.py serve). Here each table and view shows up to 500 rows, and long values are shortened.") : h("div", {class:"card", style:"display:flex;flex-direction:column;gap:8px"}, h("b", null, "Query"),'),
    ('"Browse every table and saved view, or run your own read-only query. Nothing here can change the ledger."',
     'STATIC ? "Browse every table and saved view as they were when this snapshot was taken." : "Browse every table and saved view, or run your own read-only query. Nothing here can change the ledger."'),
    ('h("small", null, "review ledger")', 'h("small", null, STATIC ? "review ledger · updated " + when(STATIC.taken) : "review ledger")'),
    ('h("div", {class:"mdtext"}, r.report_md)', '(STATIC ? md(r.report_md) : h("div", {class:"mdtext"}, r.report_md))'),
    ('h("div", {class:"mdtext"}, pl.narrative_md)', '(STATIC ? md(pl.narrative_md) : h("div", {class:"mdtext"}, pl.narrative_md))'),
    ('const TABS = [["home","#/","Overview"],', 'const TABS = [["home","#/","Overview"],["guide","#/guide","How it works"],'),
    ('  const n = OVERVIEW ? OVERVIEW.queue : 0;', '  const n = Math.max(0, (OVERVIEW ? OVERVIEW.queue : 0) - (STATIC ? hubAnsweredOpen() : 0));'),
    ('h("a", {href, "aria-current": active === k ? "page" : null}, t,', 'h("a", {href, "data-k": k, "aria-current": active === k ? "page" : null}, t,'),
    ('// ---------- routing', HUB_GUIDE_VIEW + '\n// ---------- routing'),
    ('route();\n</script>', 'route();\nhubInit();\n</script>'),
]
HUB_MAX_ROWS = 500   # rows of each table and view embedded (Explore)
HUB_MAX_CELL = 400   # characters of one embedded table cell
HUB_MAX_LINES = 400  # lines of one embedded code excerpt


def hub_snapshot(root, db, con, start=None):
    """Every viewer API response the page can ask for, from route() on a read-only connection, as one dict."""
    def call(path):
        p, _, q = path.partition("?")
        res = route(root, db, con, "/api/" + p, parse_qs(q), None)
        if isinstance(res, tuple):
            raise LedgerError(f"progress-page: /api/{path} failed: {res[0].get('error')}")
        return res

    R = {"overview": call("overview"), "gates?status=pending": call("gates?status=pending"),
         "gates?status=all": call("gates?status=all"), "reviews": call("reviews"), "plans": call("plans"),
         "trends": call("trends")}
    for r in R["reviews"]:
        R["review/" + r["code"]] = call("review/" + r["code"])
    for p in R["plans"]:
        R["plan/" + p["code"]] = call("plan/" + p["code"])
    issues = call("issues?status=")
    search = {}
    for code, point, path in con.execute("""SELECT i.code, f.point_id, COALESCE(fl.path, '') FROM issues i
                                            JOIN findings f ON f.issue_id=i.id
                                            LEFT JOIN finding_locations fl ON fl.finding_id=f.id"""):
        search.setdefault(code, set()).update([point, path])
    for i in issues:
        i["search"] = " ".join([i["code"], i["title"] or "", i.get("sub") or ""] + sorted(search.get(i["code"], ())))
        R["issue/" + i["code"]] = call("issue/" + i["code"])
    R["issues"] = issues
    R["tables"] = call("tables")
    for t in R["tables"]:
        d = call(f"table/{t['name']}?limit={HUB_MAX_ROWS}&offset=0")
        cut = [[(v[:HUB_MAX_CELL] + "…") if isinstance(v, str) and len(v) > HUB_MAX_CELL else
                ({"blob_bytes": len(v)} if isinstance(v, bytes) else v) for v in r] for r in d["rows"]]
        R["table/" + t["name"]] = {"columns": d["columns"], "rows": cut, "count": len(cut), "total": d["count"]}
    cmds = {r[0]: r[1] for r in con.execute("SELECT location_id, git_command FROM v_code_refs")}
    for (lid,) in con.execute("SELECT id FROM finding_locations ORDER BY id"):
        loc = location(con, lid)
        a, b = loc["line_start"], loc["line_end"] or loc["line_start"]
        lines = loc["excerpt"].split("\n") if loc["excerpt"] is not None else []
        R[f"source/{lid}"] = {
            "path": loc["path"], "commit_sha": loc["commit_sha"], "line_start": a, "line_end": b,
            "symbol": loc["symbol"], "permalink": loc["permalink"], "repo_name": loc["repo_name"],
            "blob_sha": loc["blob_sha"], "excerpt_redacted": bool(loc.get("excerpt_redacted")),
            "source": "excerpt" if lines else "none", "git_command": cmds.get(lid),
            "git_error": (f"Showing the first {HUB_MAX_LINES} of {len(lines)} lines." if len(lines) > HUB_MAX_LINES
                          else "This copy shows the excerpt saved in the ledger."),
            "lines": [{"n": (a or 1) + k, "text": t} for k, t in enumerate(lines[:HUB_MAX_LINES])]}
    try:
        active = [r[0] for r in con.execute("SELECT value FROM issue_statuses WHERE active = 1 ORDER BY position")]
    except sqlite3.Error:
        active = ["open", "needs-decision", "planned", "in-progress", "regressed"]
    return {"taken": now(), "start": start, "active": active, "guide": HUB_GUIDE_MD, "responses": R}


def hub_page(root, db, start=None, page=None):
    """The hub: the viewer page with the snapshot embedded (see HUB_PATCHES). start is the route it opens on when the
    address has no #route (e.g. 'plan/P-0001'). Returns (html, number of responses)."""
    page = PAGE if page is None else page
    for old, new in HUB_PATCHES:
        if page.count(old) != 1:
            raise LedgerError(f"progress-page: page.html changed; this text is not in it exactly once: {old[:70]!r}")
        page = page.replace(old, new)
    con = connect(db, readonly=True)
    try:
        snap = hub_snapshot(root, db, con, start)
    finally:
        con.close()
    # In JSON, '<' only appears inside strings, where \u003c means the same: no '</script>' or '<!--' can end or
    # bend the data script.
    data = json.dumps(snap, ensure_ascii=False, default=str, separators=(",", ":")).replace("<", "\\u003c")
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    marker = '<script>\n"use strict";'
    if page.count(marker) != 1:
        raise LedgerError("progress-page: page.html changed; its main <script> was not found")
    page = page.replace(marker, f"<script>window.LEDGER_SNAPSHOT = {data};</script>\n{marker}", 1)
    name = snap["responses"]["overview"].get("project") or root.name
    esc = str(name).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    page = page.replace("<title>Review ledger</title>", f"<title>Review ledger: {esc}</title>", 1)
    return page, len(snap["responses"])


GATE_CODE = re.compile(r"GATE-[0-9]{4,}")


def _answer_items(doc):
    """The answers in an exported answers file, as [(gate code, {answer_value, answer_text, answered_at})].
    Accepted shapes: a list of answers, {"answers": [...]}, {gate code: answer}, and database documents
    ({"id": code, "data": {...}}); an answer names its gate as gate, code or the document id."""
    if isinstance(doc, dict) and isinstance(doc.get("answers"), (list, dict)):
        doc = doc["answers"]
    if isinstance(doc, dict):
        doc = [dict(v, gate=v.get("gate") or k) if isinstance(v, dict) else {"gate": k, "bad": True}
               for k, v in doc.items()]
    if not isinstance(doc, list):
        raise ValueError("the file holds no list of answers")
    out = []
    for item in doc:
        if not isinstance(item, dict):
            out.append((None, None)); continue
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        code = data.get("gate") or data.get("code") or item.get("id") or ""
        code = str(code).rsplit("/", 1)[-1]
        out.append((code, data))
    return out


def import_answers(db, source):
    """`ledger.py import-answers FILE`: record the owner's answers given in the published hub (exported from its
    artifact database, collection answers/<gate code>) into the ledger. Each goes through answer_gate(), the one set
    of answer rules the viewer and answer-gate use, so it is checked against the gate's options and recorded
    answered_via='viewer' by the owner. A gate that is no longer open is left as it is and said so. The time the
    owner answered in the hub is not kept: the gate records when it was imported. Returns 1 when any answer was
    refused, else 0."""
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        items = _answer_items(json.loads(raw))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"import-answers: cannot read {source}: {e}", file=sys.stderr)
        return 1
    recorded = skipped = refused = 0
    con = connect(db)
    try:
        for code, data in items:
            if not code or not GATE_CODE.fullmatch(code) or data is None or data.get("bad"):
                print(f"{code or '?'}: not an answer to a gate (skipped)"); refused += 1; continue
            try:
                answer_gate(con, code, data.get("answer_value"), data.get("answer_text"), via="viewer")
            except GateRefused as e:
                if e.status == 409:
                    print(f"{code}: already answered or closed; left as it is"); skipped += 1
                else:
                    print(f"{code}: not recorded: {e}"); refused += 1
                continue
            print(f"{code}: recorded")
            recorded += 1
    finally:
        con.close()
    print(f"import-answers: {recorded} recorded, {skipped} already answered, {refused} refused")
    return 1 if refused else 0


# ---------------------------------------------------------------- restore and merge

GITATTRIBUTES = ("# ledger.sqlite is a SQLite file: git must not diff or line-merge it. After a merge conflict keep one\n"
                 "# side and bring the other in with: python3 .code-review/ledger.py merge OTHER.sqlite\n"
                 "ledger.sqlite binary -diff -merge\nledger.sql -merge\n")


def _restore_authorizer(action, arg1, *rest):
    """A dump may only build tables and rows: no ATTACH (writing another file) and only the foreign_keys pragma."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() != "foreign_keys":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def restore(source, target):
    """`ledger.py restore FILE.sql --to NEW.sqlite`: rebuild a ledger from a dump into a new file (never over an
    existing one), then run check on it. The dump runs with ATTACH and pragmas other than foreign_keys refused.
    Returns check's failure count (0 = restored and clean); exits on a refused target or an unreadable dump."""
    target = Path(target).resolve()
    for p in (target, target.with_name(target.name + "-wal"), target.with_name(target.name + "-journal")):
        if p.exists():
            sys.exit(f"restore: {p} exists; restore writes a new file only. Give another name with --to NEW.sqlite")
    try:
        sql = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"restore: cannot read {source}: {e}")
    if not sql.startswith((DUMP_HEADER, "BEGIN TRANSACTION;")):
        sys.exit(f"restore: {source} is not a ledger.py dump (its first line is not the dump header)")
    tmp = target.with_name(target.name + ".restore-tmp")
    for p in (tmp, tmp.with_name(tmp.name + "-journal")):
        if p.exists():
            p.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    con = connect(tmp, create=True)
    try:
        con.execute("PRAGMA foreign_keys = OFF")  # the dump makes tables in name order, children before parents
        con.set_authorizer(_restore_authorizer)
        con.executescript(sql)
        con.set_authorizer(None)
        con.commit()
    except sqlite3.Error as e:
        con.close()
        tmp.unlink()
        sys.exit(f"restore: the dump did not load ({e}); nothing was written")
    con.close()
    os.replace(str(tmp), str(target))
    make_private(target)
    set_wal(target)
    print(f"restored {source} into {target}")
    con = connect(target)
    try:
        bad = check(con, quiet=True)
    finally:
        con.close()
    print(f"check: {'clean' if not bad else f'{bad} failed checks (listed above)'}")
    return bad


# events.entity_id names a row of these tables (by its rowid), by events.entity_type
EVENT_ENTITY_TABLES = {"review": "reviews", "issue": "issues", "step": "steps", "plan": "plans",
                       "decision": "decisions", "finding": "findings", "gate": "gates", "relation": "issue_relations",
                       "check": "review_issue_checks", "step-finding": "step_findings"}
_CREATED_COLUMNS = ("created_at", "started_at", "opened_at", "asked_at")  # when a row with a code was first made
_KEEP_OURS_TABLES = ("meta", "schema_migrations")  # this ledger's own identity: never taken from the other one


class _MergeTable:
    def __init__(self, con, name):
        self.name = name
        info = con.execute(f"PRAGMA table_info({quote_name(name)})").fetchall()
        self.cols = [r[1] for r in info]
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        self.id_col = "id" if pk == ["id"] else None
        self.fks = {}  # column -> parent table (single-column foreign keys)
        for f in con.execute(f"PRAGMA foreign_key_list({quote_name(name)})").fetchall():
            self.fks[f[3]] = f[2]
        self.self_fks = [c for c, t in self.fks.items() if t == name]
        keys = [] if not pk or pk == ["id"] else [pk]
        for il in con.execute(f"PRAGMA index_list({quote_name(name)})").fetchall():
            if il[2] and not il[4]:  # unique, not partial
                k = [r[2] for r in con.execute(f"PRAGMA index_info({quote_name(il[1])})").fetchall()]
                if None not in k and k not in keys and not set(k) & set(self.self_fks):
                    keys.append(k)
        self.keys = keys
        try:
            con.execute(f"SELECT rowid FROM {quote_name(name)} LIMIT 0")
            self.rowid = True
        except sqlite3.Error:
            self.rowid = False
        self.created = next((c for c in _CREATED_COLUMNS if c in self.cols), None)
        self.content = [c for c in self.cols if c != self.id_col and c not in self.self_fks]


def _merge_order(con, tables):
    """Tables with each parent before its children (self references aside); events last (its entity_id needs the
    other tables' id maps)."""
    deps = {t: {p for p in tables[t].fks.values() if p != t and p in tables} for t in tables}
    order, seen = [], set()

    def visit(t, stack=()):
        if t in seen or t in stack:
            return
        for p in sorted(deps[t]):
            visit(p, stack + (t,))
        seen.add(t)
        order.append(t)
    for t in sorted(tables):
        if t != "events":
            visit(t)
    if "events" in tables:
        order.append("events")
    return order


def _next_code(con, table, code, taken):
    prefix, _, digits = code.rpartition("-")
    width = max(4, len(digits))
    nums = [int(r[0].rpartition("-")[2]) for r in con.execute(
        f"SELECT code FROM {quote_name(table)} WHERE code LIKE ?", (prefix + "-%",)) if r[0].rpartition("-")[2].isdigit()]
    nums += [int(c.rpartition("-")[2]) for c in taken.get(table, ()) if c.startswith(prefix + "-")]
    new = f"{prefix}-{(max(nums) if nums else 0) + 1:0{width}d}"
    taken.setdefault(table, set()).add(new)
    return new


def merge_ledgers(db, other):
    """`ledger.py merge OTHER.sqlite`: bring the rows of another copy of this ledger (the other side of a git merge
    conflict) into this one. Rows are matched by their natural keys (codes, unique columns; rows with no such key by
    their whole content), so rows both copies share are kept once and events are not duplicated. A code both sides
    made independently (same code, different creation time) is renumbered on the other side's row, and the mapping
    is printed. A row both sides changed keeps the newer version by updated_at, else this ledger's. Ids are
    remapped; rows go in with triggers and views dropped (so no history is written twice), and both are recreated
    from the shipped schema afterwards. The result is built in a temporary copy and checked; only a clean result is
    written into this ledger, after a backup (ledger.sqlite.bak-merge[.N]). Returns check's failure count."""
    db, other = Path(db).resolve(), Path(other).resolve()
    if db == other:
        sys.exit("merge: OTHER is this ledger")
    try:
        oc = connect(other, readonly=True)
    except sqlite3.Error as e:
        sys.exit(f"merge: cannot open {other}: {e}")
    try:
        ov = ledger_version(oc, other)
    except LedgerError as e:
        sys.exit(f"merge: {e}")
    if ov != SCHEMA_VERSION:
        sys.exit(f"merge: {other} is schema {ov}; this ledger.py is schema {SCHEMA_VERSION} (migrate it first)")
    tmp = db.with_name(db.name + ".merge-tmp")
    for p in (tmp, tmp.with_name(tmp.name + "-journal")):
        if p.exists():
            p.unlink()
    os.close(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    src = connect(db)
    con = connect(tmp, create=True)
    try:
        src.backup(con)
    finally:
        src.close()
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA journal_mode = DELETE")
    report, problems, taken = [], [], {}
    try:
        con.execute("BEGIN")
        _drop_views_and_triggers(con)
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        theirs_names = {r[0] for r in oc.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {n: _MergeTable(con, n) for n in names if n in theirs_names}
        maps = {n: {} for n in tables}  # table -> {their rowid: our rowid}
        stats = {}
        for name in _merge_order(con, tables):
            t = tables[name]
            st = stats[name] = {"same": 0, "added": 0, "renumbered": 0, "took_theirs": 0, "kept_ours": 0}
            sel = ("rowid AS __rowid__, " if t.rowid else "") + ", ".join(quote_name(c) for c in t.cols)
            order = " ORDER BY rowid" if t.rowid else ""
            theirs = [dict(r) for r in oc.execute(f"SELECT {sel} FROM {quote_name(name)}{order}")]
            if name in _KEEP_OURS_TABLES:
                continue
            ours_by_content = None
            pending = []  # (their row, mapped values) to insert after the table's map is complete
            next_id = (con.execute(f"SELECT COALESCE(max(rowid), 0) FROM {quote_name(name)}").fetchone()[0]
                       if t.rowid else 0)
            for r in theirs:
                v = {c: r[c] for c in t.cols}
                for c, parent in t.fks.items():
                    if parent != name and v.get(c) is not None and parent in maps and tables[parent].id_col:
                        if v[c] not in maps[parent]:
                            problems.append(f"{name}: a row points at {parent} {v[c]}, which the other ledger "
                                            f"does not have")
                        v[c] = maps[parent].get(v[c], v[c])
                if name == "events":
                    et = EVENT_ENTITY_TABLES.get(v["entity_type"])
                    if et in maps:
                        v["entity_id"] = maps[et].get(v["entity_id"], v["entity_id"])
                match = None
                for k in t.keys:
                    got = con.execute(f"SELECT {'rowid AS __rowid__, ' if t.rowid else ''}* FROM {quote_name(name)} "
                                      f"WHERE " + " AND ".join(f"{quote_name(c)} IS ?" for c in k),
                                      [v[c] for c in k]).fetchone()
                    if got is not None:
                        match = (k, dict(got))
                        break
                if match is None and not t.keys:
                    if ours_by_content is None:
                        ours_by_content = {}
                        for o in con.execute(f"SELECT {sel} FROM {quote_name(name)}"):
                            ours_by_content.setdefault(tuple(o[c] for c in t.content), dict(o))
                    o = ours_by_content.get(tuple(v[c] for c in t.content))
                    if o is not None:
                        match = (None, o)
                if match is not None:
                    k, o = match
                    if (k == ["code"] and t.created and o.get(t.created) != v.get(t.created)):
                        # brought in by an earlier merge under another code: the same row but for its code
                        same = [c for c in t.content if c != "code"]
                        for cand in con.execute(f"SELECT {'rowid AS __rowid__, ' if t.rowid else ''}* FROM "
                                                f"{quote_name(name)} WHERE {quote_name(t.created)} IS ?",
                                                (v.get(t.created),)):
                            if all(cand[c] == v[c] for c in same):
                                if t.rowid:
                                    maps[name][r["__rowid__"]] = cand["__rowid__"]
                                st["same"] += 1
                                break
                        else:
                            cand = None
                        if cand is not None:
                            continue
                        old = v["code"]
                        v["code"] = _next_code(con, name, old, taken)
                        report.append(f"{name}: the other ledger's {old} is now {v['code']} (both sides made "
                                      f"{old})")
                        st["renumbered"] += 1
                        match = None
                    else:
                        if t.rowid:
                            maps[name][r["__rowid__"]] = o["__rowid__"]
                        diff = [c for c in t.content if o.get(c) != v[c]]
                        if not diff:
                            st["same"] += 1
                        elif ("updated_at" in t.cols and (v.get("updated_at") or "") > (o.get("updated_at") or "")):
                            pending.append(("update", r, v, o))
                            st["took_theirs"] += 1
                        else:
                            st["kept_ours"] += 1
                        continue
                if t.rowid:
                    next_id += 1
                    maps[name][r["__rowid__"]] = next_id
                    if t.id_col:
                        v[t.id_col] = next_id
                pending.append(("insert", r, v, None))
                st["added"] += 1
            for kind, r, v, o in pending:
                for c in t.self_fks:
                    if v.get(c) is not None:
                        v[c] = maps[name].get(v[c], v[c])
                try:
                    if kind == "insert":
                        cols = list(t.cols)
                        extra = ["rowid"] if t.rowid and not t.id_col else []
                        vals = [v[c] for c in cols] + ([maps[name][r["__rowid__"]]] if extra else [])
                        con.execute(f"INSERT INTO {quote_name(name)}({', '.join(quote_name(c) for c in cols + extra)}) "
                                    f"VALUES ({', '.join('?' * len(vals))})", vals)
                    else:
                        cols = [c for c in t.content if not any(c in k for k in t.keys)]
                        con.execute(f"UPDATE {quote_name(name)} SET {', '.join(quote_name(c) + '=?' for c in cols)} "
                                    f"WHERE rowid=?", [v[c] for c in cols] + [o["__rowid__"]])
                except sqlite3.Error as e:
                    problems.append(f"{name}: a row of the other ledger could not be merged ({e})")
        _create_views_and_triggers(con)
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"{len(fk)} broken references after the merge")
        con.execute("COMMIT")
    except sqlite3.Error as e:
        problems.append(f"the merge stopped: {e}")
        if con.in_transaction:
            con.execute("ROLLBACK")
    finally:
        oc.close()
    for name, st in sorted(stats.items()):
        if st["added"] or st["renumbered"] or st["took_theirs"] or st["kept_ours"]:
            print(f"{name}: {st['added']} added, {st['same']} already here, {st['took_theirs']} updated from the "
                  f"other (newer), {st['kept_ours']} differ and kept as here" +
                  (f", {st['renumbered']} renumbered" if st["renumbered"] else ""))
    if not any(st["added"] or st["took_theirs"] or st["kept_ours"] for st in stats.values()):
        print("the other ledger holds nothing this one does not")
    for line in report:
        print(line)
    if report:
        print("note: text that mentions a renumbered code (notes, reports, review tags) still says the old one")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    bad = 0 if problems else check(con, quiet=True)
    con.close()
    if problems or bad:
        for p in problems:
            print(f"merge: {p}")
        print(f"merge: not applied; {db} is unchanged (the merged copy is {tmp})")
        return len(problems) + bad
    bak = db.with_name(db.name + ".bak-merge")
    n = 1
    while bak.exists():
        n += 1
        bak = db.with_name(f"{db.name}.bak-merge.{n}")
    live = connect(db)
    try:
        b = connect(bak, create=True)
        live.backup(b)
        b.close()
        make_private(bak)
        m = connect(tmp)
        m.backup(live)
        m.close()
    finally:
        live.close()
    tmp.unlink()
    print(f"merged {other} into {db} (the ledger before the merge is kept as {bak.name}); check: clean")
    return 0


# ---------------------------------------------------------------- the commands above, for main()

TOOL_COMMANDS = ("query", "progress-md", "progress-page", "import-answers", "restore", "merge")


def add_tool_parsers(sub):
    s = sub.add_parser("query", help="run one read-only SELECT on the ledger and print TSV (or --json)",
                       description='Run one SELECT (or WITH ... SELECT) through the viewer\'s read-only path and print '
                                   'the rows as TSV with a header line, or JSON. Writes, ATTACH and PRAGMAs are '
                                   'refused. Example: python3 .code-review/ledger.py query "SELECT code, title FROM '
                                   'issues WHERE status=\'open\'". Without ledger.py: python3 -c "import sqlite3; '
                                   'print(sqlite3.connect(\'file:.code-review/ledger.sqlite?mode=ro\', uri=True)'
                                   '.execute(\'SELECT ...\').fetchall())"')
    s.add_argument("sql")
    s.add_argument("--readonly", action="store_true", help="accepted for clarity; query is always read-only")
    s.add_argument("--json", action="store_true", help='print {"columns", "rows", "truncated"}')
    s = sub.add_parser("progress-md", help="write a plan's progress as Markdown (PROGRESS.md)",
                       description="The plan's progress in a fixed Markdown format: totals, one row per phase "
                                   "(v_plan_progress), then every step with its status, what it closes and its note. "
                                   "Printed, or written to -o FILE (e.g. -o docs/fix-plan/PROGRESS.md).")
    s.add_argument("plan"); s.add_argument("-o", "--out", help="file to write (default: print)")
    s = sub.add_parser("progress-page", help="write the hub: the viewer as one self-contained HTML page",
                       description="Write the whole viewer (every review, issue, plan, question and table) as one "
                                   "HTML file that makes no outside request: the page `serve` shows, with the data "
                                   "embedded. Published as a Claude artifact with a database, it also takes the "
                                   "owner's answers to open questions (collection answers/<gate code>); export them "
                                   "and run import-answers. Give a plan to open the page on it. Default file: "
                                   "hub.html beside the ledger.")
    s.add_argument("plan", nargs="?"); s.add_argument("-o", "--out", help="file to write (default: hub.html beside "
                                                                         "the ledger)")
    s = sub.add_parser("import-answers", help="record gate answers exported from the published hub",
                       description="Read answers given in the published hub (a JSON list of {gate, answer_value, "
                                   "answer_text, answered_at}, or the hub's database documents) and record each in "
                                   "its gate as the viewer does (answered_via 'viewer'). A gate no longer open is "
                                   "left as it is. FILE - reads standard input.")
    s.add_argument("file")
    s = sub.add_parser("restore", help="rebuild a ledger from a dump into a new file, then check it",
                       description="Rebuild a ledger from ledger.sql (written by dump) into a new file and run check "
                                   "on it. Never writes over an existing file. Example: python3 ledger.py restore "
                                   ".code-review/ledger.sql --to /tmp/restored.sqlite")
    s.add_argument("file"); s.add_argument("--to", required=True, help="the new ledger file to create")
    s = sub.add_parser("merge", help="bring another copy of this ledger (the other side of a merge) into this one",
                       description="After a git merge conflict on ledger.sqlite: keep one side as the ledger, save "
                                   "the other (git show MERGE_HEAD:.code-review/ledger.sqlite > /tmp/theirs.sqlite) "
                                   "and run merge /tmp/theirs.sqlite. Shared rows are kept once, codes both sides "
                                   "made are renumbered (the mapping is printed), and the result is checked before "
                                   "it is written; the ledger before the merge is kept as ledger.sqlite.bak-merge.")
    s.add_argument("other")


def tool_command(a, root, db):
    """Run one of TOOL_COMMANDS; returns the exit code."""
    if a.cmd == "query":
        return query_command(db, a.sql, a.json)
    if a.cmd == "restore":
        return 1 if restore(a.file, a.to) else 0
    if a.cmd == "merge":
        con = open_ledger(db, write=True, verify=True)
        con.close()
        return 1 if merge_ledgers(db, a.other) else 0
    if a.cmd == "import-answers":
        con = open_ledger(db, write=True, verify=True)
        con.close()
        return import_answers(db, a.file)
    con = open_ledger(db, verify=True)
    try:
        if a.cmd == "progress-md":
            text = progress_md(con, a.plan)
            if text is None:
                print(f"progress-md: no plan {a.plan}", file=sys.stderr)
                return 1
            if a.out:
                out = Path(a.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(out, text.encode("utf-8"), 0o644)
                print(f"wrote {out}")
            else:
                sys.stdout.write(text)
            return 0
        if a.plan and not one(con, "SELECT 1 x FROM plans WHERE code=?", (a.plan,)):
            print(f"progress-page: no plan {a.plan}", file=sys.stderr)
            return 1
    finally:
        con.close()
    try:
        page, n = hub_page(root, db, start=f"plan/{a.plan}" if a.plan else None)
    except LedgerError as e:
        print(e, file=sys.stderr)
        return 1
    out = Path(a.out) if a.out else db.parent / "hub.html"
    write_atomic(out, page.encode("utf-8"))
    print(f"wrote {out} ({len(page.encode('utf-8')) / 1e6:.2f} MB, {n} views, no outside requests)")
    return 0



# ---------------------------------------------------------------- write commands (plan P-0001 steps 98-106, 127)
#
# Every command that writes runs in one BEGIN IMMEDIATE transaction on a connection with foreign keys on and a busy
# timeout (open_ledger), with change_actor_id taken from --actor. The ledger's own triggers are the rules; a command
# checks what it can first so the person gets a plain reason, and turns any refusal by a trigger or constraint into
# one line on stderr and exit 1, with nothing written. Codes come from v_next_codes (next-code), never computed by
# hand. The file formats the import commands read are in references/formats/.

class WriteError(Exception):
    """A write command refused: one line for the person running it; nothing was written."""


class GateRefused(Exception):
    """answer_gate() refused an answer: (HTTP status for the viewer, the message both the viewer and the CLI show)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


CODE_PREFIXES = ("ISS", "R", "P", "D", "GATE", "REQ", "G")


def next_code(con, prefix):
    """The next code for a prefix (ISS, R, P, D, GATE, REQ or G), from v_next_codes: the highest number + 1 in the
    canonical format (ISS-0001, G-01). Codes are never reused, and the code triggers refuse anything else."""
    p = (prefix or "").upper().rstrip("-")
    if p not in CODE_PREFIXES:
        raise WriteError(f"unknown code prefix {prefix!r}; use one of {', '.join(CODE_PREFIXES)}")
    r = con.execute("SELECT next_code FROM v_next_codes WHERE prefix=?", (p,)).fetchone()
    return r[0]


def actor_code(actor_id):
    return f"A-{int(actor_id):04d}"


def resolve_actor(con, ref, what="--actor"):
    """An actors.id from A-0007, 7, or a name that is unique among actors; None for None."""
    if ref is None or str(ref).strip() == "":
        return None
    ref = str(ref).strip()
    m = re.fullmatch(r"(?:A-)?0*([0-9]+)", ref)
    if m:
        r = con.execute("SELECT id FROM actors WHERE id=?", (int(m.group(1)),)).fetchone()
        if not r:
            raise WriteError(f"{what}: no actor {ref}")
        return r[0]
    found = [r[0] for r in con.execute("SELECT id FROM actors WHERE name=?", (ref,))]
    if len(found) != 1:
        raise WriteError(f"{what}: {'no actor' if not found else str(len(found)) + ' actors'} named {ref!r}"
                         + ("; give its A- code" if found else ""))
    return found[0]


def owner_actor(con):
    """The project's owner as an actor (the lowest kind='owner' row), made on first use."""
    owner = con.execute("SELECT min(id) FROM actors WHERE kind='owner'").fetchone()[0]
    if owner is None:
        owner = con.execute("INSERT INTO actors(kind, name) VALUES ('owner', 'owner')").lastrowid
    return owner


def review_row(con, code):
    if not REVIEW_CODE.fullmatch(code or ""):
        raise WriteError(f"{code!r} is not a review code like R-0001")
    r = one(con, "SELECT * FROM reviews WHERE code=?", (code,))
    if not r:
        raise WriteError(f"no review {code}")
    return r


def plan_row(con, code=None):
    """The plan named by code, or the one plan that is not superseded, abandoned or complete."""
    if code:
        r = one(con, "SELECT * FROM plans WHERE code=?", (code,))
        if not r:
            raise WriteError(f"no plan {code}")
        return r
    live = rows(con, "SELECT * FROM plans WHERE status NOT IN ('superseded','abandoned','complete') ORDER BY id")
    if len(live) != 1:
        raise WriteError(f"{len(live)} plans are in progress; name one with --plan P-0001")
    return live[0]


def step_row(con, key, plan_code=None):
    """A step from its number (in --plan, or the one live plan), P-0001#12, or its drafter key."""
    key = str(key).strip()
    m = re.fullmatch(r"(P-[0-9]{4,})[#:]([0-9]+)", key)
    if m:
        plan_code, key = m.group(1), m.group(2)
    if key.isdigit():
        pl = plan_row(con, plan_code)
        s = one(con, "SELECT * FROM steps WHERE plan_id=? AND number=?", (pl["id"], int(key)))
        if not s:
            raise WriteError(f"plan {pl['code']} has no step {key}")
        return s
    args = [key]
    sql = "SELECT s.* FROM steps s JOIN plans p ON p.id = s.plan_id WHERE s.drafter_key=?"
    if plan_code:
        sql += " AND p.code=?"; args.append(plan_code)
    found = rows(con, sql, args)
    if len(found) != 1:
        raise WriteError(f"{'no step' if not found else str(len(found)) + ' steps'} with key {key!r}")
    return found[0]


def issue_row(con, code):
    r = one(con, "SELECT * FROM issues WHERE code=?", (code,))
    if not r:
        raise WriteError(f"no issue {code}")
    return r


def full_commit(d, ref):
    """The full hash of a commit that is in repository d, else WriteError (an uncommitted state has no commit)."""
    try:
        sha = git(d, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except (RuntimeError, OSError, subprocess.SubprocessError):
        sha = ""
    if not FULL_HEX.fullmatch(sha):
        raise WriteError(f"commit {ref!r} is not in the repository at {d}")
    return sha


def _one_line(e):
    return " ".join(str(e).split())


class WriteContext:
    def __init__(self, con, root, db, a):
        self.con, self.root, self.db, self.a = con, root, db, a
        self.actor_id = None
        self.files = []  # files written by the command, removed again if it fails

    def x(self, sql, args=()):
        return self.con.execute(sql, args)


WRITE_COMMANDS = {}  # name -> handler(ctx) returning the lines to print once committed
READ_COMMANDS = {}   # name -> handler(con, a) for commands that only read (next-code)


def run_write_command(con, root, db, a):
    """Run the write command a.cmd in one BEGIN IMMEDIATE transaction. Prints its lines after COMMIT and returns 0;
    on any refusal rolls back, removes the files it wrote, prints one line and returns 1."""
    if a.cmd in READ_COMMANDS:
        try:
            for line in READ_COMMANDS[a.cmd](con, a):
                print(line)
        except (WriteError, sqlite3.Error) as e:
            print(f"{a.cmd}: {_one_line(e)}", file=sys.stderr)
            return 1
        return 0
    handler = WRITE_COMMANDS[a.cmd]
    ctx = WriteContext(con, root, db, a)
    if con.in_transaction:
        con.commit()
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        print(f"{a.cmd}: the ledger is busy ({_one_line(e)}); nothing was written, try again", file=sys.stderr)
        return 1
    ok = False
    try:
        ctx.actor_id = resolve_actor(con, getattr(a, "actor", None))
        lines = handler(ctx) or []
        con.execute("COMMIT")
        ok = True
    except WriteError as e:
        msg = _one_line(e)
    except GateRefused as e:
        msg = _one_line(e)
    except sqlite3.Error as e:
        msg = f"refused by the ledger: {_one_line(e)}"
    except RuntimeError as e:  # git
        msg = _one_line(e)
    finally:
        if not ok:
            if con.in_transaction:
                con.execute("ROLLBACK")
            for f in ctx.files:
                try:
                    Path(f).unlink()
                except OSError:
                    pass
    if not ok:
        print(f"{a.cmd}: {msg}; nothing was written", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def _command(name, help, usage, read=False):
    def wrap(f):
        (READ_COMMANDS if read else WRITE_COMMANDS)[name] = f
        f.cli = (help, usage)
        return f
    return wrap


def _text_arg(value, what):
    """Text from an argument, or from a file when it is @path (or @- for standard input)."""
    if value is None:
        return None
    if value.startswith("@"):
        src = value[1:]
        try:
            return sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise WriteError(f"{what}: cannot read {src}: {e}")
    return value


def _need(value, what):
    if value is None or not str(value).strip():
        raise WriteError(f"give {what}")
    return str(value).strip()


# ----- next-code, add-repo, add-actor

@_command("next-code", "print the next code for a prefix (ISS, R, P, D, GATE, REQ, G)",
          "ledger.py next-code ISS", read=True)
def cmd_next_code(con, a):
    return [next_code(con, a.prefix)]


@_command("add-repo", "record a repository of the project",
          "ledger.py add-repo NAME PATH [--remote URL] [--default-branch main] [--permalink-template URL] "
          "[--outside-root]")
def cmd_add_repo(ctx):
    a = ctx.a
    if not a.outside_root:
        repo_dir(ctx.root, a.path)  # refuses a path outside the project root (D-0007)
    rid = ctx.x("""INSERT INTO repositories(name, path, remote_url, default_branch, permalink_template,
                   allow_outside_root) VALUES (?, ?, ?, ?, ?, ?)""",
                (_need(a.name, "the repository name"), a.path, a.remote, a.default_branch, a.permalink_template,
                 1 if a.outside_root else 0)).lastrowid
    return [f"repository {a.name} (id {rid}) at {a.path}"]


ACTOR_KINDS = ('owner', 'human', 'manager', 'reviewer', 'verifier', 'indexer', 'matcher', 'drafter', 'fixer', 'tool')


@_command("add-actor", "record a person or agent (one row per agent instance); prints its A- code",
          "ledger.py add-actor KIND NAME [--model M] [--harness H] [--session REF] [--parent A-0001] "
          "[--review R-0001 --role 'indexer for security.md']")
def cmd_add_actor(ctx):
    a = ctx.a
    if a.kind not in ACTOR_KINDS:
        raise WriteError(f"kind {a.kind!r} is not one of {', '.join(ACTOR_KINDS)}")
    name = _need(a.name, "the actor's name")
    dup = ctx.x("""SELECT id FROM actors WHERE kind=? AND name=? AND model IS ? AND session_ref IS ?""",
                (a.kind, name, a.model, a.session)).fetchone()
    if dup:
        raise WriteError(f"{actor_code(dup[0])} is already {a.kind} {name!r}"
                         f"{' (' + a.model + ')' if a.model else ''}; reuse it, or give --session for a new instance")
    parent = resolve_actor(ctx.con, a.parent, "--parent") if a.parent else ctx.actor_id
    aid = ctx.x("INSERT INTO actors(kind, name, model, harness, session_ref, parent_actor_id) VALUES (?,?,?,?,?,?)",
                (a.kind, name, a.model, a.harness, a.session, parent)).lastrowid
    out = [actor_code(aid)]
    if a.review:
        rv = review_row(ctx.con, a.review)
        ctx.x("INSERT INTO review_actors(review_id, actor_id, role_label) VALUES (?,?,?)",
              (rv["id"], aid, _need(a.role, "--role (what it did in the review)")))
        out.append(f"{actor_code(aid)} works on {a.review} as {a.role}")
    return out


# ----- reviews: new-review, review-status, snapshot, add-run

def _exclude_spec(d, db):
    """git pathspec for the whole repository d except the ledger's folder when it is inside d."""
    try:
        rel = Path(db).resolve().parent.relative_to(Path(d).resolve())
    except ValueError:
        return ["."]
    return [".", f":(exclude){rel.as_posix()}"] if str(rel) != "." else ["."]


def snapshot_repository(root, db, review_code, repo, commit=None):
    """What review_repositories records for one repository: the full commit (HEAD unless commit is given), branch,
    commit time (UTC) and subject; for HEAD also whether the working tree differs from it, ignoring the ledger's own
    folder, and the patch of every difference (git diff HEAD, staged and unstaged, plus each untracked file not
    ignored). Returns (fields, patch bytes or None)."""
    d = repo_dir(root, repo["path"], bool(repo["allow_outside_root"]))
    try:
        git(d, "rev-parse", "--git-dir")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        raise WriteError(f"repository {repo['name']}: {d} is not a git checkout")
    head = full_commit(d, "HEAD")
    sha = full_commit(d, commit) if commit else head
    try:
        branch = git(d, "symbolic-ref", "--short", "-q", "HEAD").decode().strip() or None
    except RuntimeError:
        branch = None  # detached
    info = git(d, "show", "-s", "--format=%cI%n%s", sha).decode("utf-8", "replace").split("\n", 1)
    fields = {"commit_sha": sha, "branch": branch if sha == head else None,
              "commit_time": utc_time(info[0].strip()), "commit_subject": (info[1].strip() if len(info) > 1 else None),
              "is_dirty": 0, "dirty_note": None, "dirty_patch_path": None}
    if sha != head:
        return fields, None
    spec = _exclude_spec(d, db)
    status = git(d, "status", "--porcelain", "--untracked-files=all", "--", *spec).decode("utf-8", "replace")
    if not status.strip():
        return fields, None
    patch = git(d, "diff", "HEAD", "--binary", "--", *spec)
    untracked = [p for p in git(d, "ls-files", "--others", "--exclude-standard", "-z", "--", *spec)
                 .decode("utf-8", "replace").split("\0") if p]
    for p in untracked:
        r = subprocess.run(["git", "-C", str(d), "diff", "--no-index", "--binary", "--", "/dev/null", p],
                           capture_output=True, timeout=15)
        if r.returncode not in (0, 1):
            raise WriteError(f"repository {repo['name']}: git diff of untracked {p} failed: "
                             f"{r.stderr.decode('utf-8', 'replace').strip()}")
        patch += r.stdout
    changed = len([l for l in status.splitlines() if not l.startswith("??")])
    fields.update(is_dirty=1, dirty_note=f"{changed} changed and {len(untracked)} untracked files differ from "
                                         f"{sha[:10]}")
    return fields, patch


def _write_patch(ctx, review_code, repo, patch):
    folder = Path(ctx.db).parent / "patches"
    folder.mkdir(mode=0o700, exist_ok=True)
    make_private(folder, 0o700)
    path = folder / f"{review_code}-{re.sub(r'[^A-Za-z0-9._-]', '_', repo['name'])}.patch"
    write_atomic(path, patch, 0o600)
    ctx.files.append(path)
    try:
        return path.resolve().relative_to(Path(ctx.root).resolve()).as_posix()
    except ValueError:
        return str(path)


def record_snapshot(ctx, rv, repo, commit=None):
    """Insert or refresh the review_repositories row of one repository from git (see snapshot_repository)."""
    fields, patch = snapshot_repository(ctx.root, ctx.db, rv["code"], repo, commit)
    if patch is not None:
        fields["dirty_patch_path"] = _write_patch(ctx, rv["code"], repo, patch)
    old = one(ctx.con, "SELECT commit_sha FROM review_repositories WHERE review_id=? AND repository_id=?",
              (rv["id"], repo["id"]))
    if old and old["commit_sha"] != fields["commit_sha"] and ctx.x(
            """SELECT 1 FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
               WHERE f.review_id=? AND fl.repository_id=? LIMIT 1""", (rv["id"], repo["id"])).fetchone():
        raise WriteError(f"{rv['code']} already has findings at {old['commit_sha'][:10]} in {repo['name']}; "
                         f"its reviewed commit cannot change")
    cols = list(fields)
    ctx.x(f"""INSERT INTO review_repositories(review_id, repository_id, {', '.join(cols)})
              VALUES (?, ?, {', '.join('?' for _ in cols)})
              ON CONFLICT(review_id, repository_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}""",
          (rv["id"], repo["id"], *fields.values()))
    dirty = (f", working tree differs ({fields['dirty_note']}; patch {fields['dirty_patch_path']})"
             if fields["is_dirty"] else ", clean")
    return f"{repo['name']}: {fields['commit_sha']}" + (f" on {fields['branch']}" if fields["branch"] else "") + dirty


def _repo_args(con, specs):
    """[(repository row, commit or None)] from NAME or NAME@COMMIT arguments; every repository when none given."""
    if not specs:
        found = rows(con, "SELECT * FROM repositories ORDER BY id")
        if not found:
            raise WriteError("the ledger has no repositories; add one with: ledger.py add-repo NAME PATH")
        return [(r, None) for r in found]
    out = []
    for s in specs:
        name, _, commit = s.partition("@")
        r = one(con, "SELECT * FROM repositories WHERE name=?", (name,))
        if not r:
            raise WriteError(f"no repository named {name!r} (add it with: ledger.py add-repo {name} PATH)")
        out.append((r, commit or None))
    return out


REVIEW_SCOPES = ('full', 'partial', 'targeted', 'second', 'fix-verification')


@_command("new-review", "start a review: its code, repositories at their commits (from git), domains and goals",
          "ledger.py new-review --title T --scope full|partial|targeted|second|fix-verification "
          "[--repo NAME[@COMMIT]]... [--domain CODE[=reason]]... [--skip CODE=reason]... [--goal G-01]... "
          "[--request TEXT|@file] [--previous R-0001] [--manager A-0001] [--actor A-0001]")
def cmd_new_review(ctx):
    a, con = ctx.a, ctx.con
    if a.scope not in REVIEW_SCOPES:
        raise WriteError(f"scope {a.scope!r} is not one of {', '.join(REVIEW_SCOPES)}")
    code = next_code(con, "R")
    prev = review_row(con, a.previous)["id"] if a.previous else None
    manager = resolve_actor(con, a.manager, "--manager") if a.manager else None
    rid = ctx.x("""INSERT INTO reviews(code, title, scope, status, request_text, requested_by_actor_id,
                   manager_actor_id, previous_review_id, skill_version, change_actor_id)
                   VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?)""",
                (code, _need(a.title, "--title"), a.scope, _text_arg(a.request, "--request"), None, manager, prev,
                 TOOL_VERSION, ctx.actor_id)).lastrowid
    rv = one(con, "SELECT * FROM reviews WHERE id=?", (rid,))
    out = [code]
    for repo, commit in _repo_args(con, a.repo):
        out.append(record_snapshot(ctx, rv, repo, commit))
    for spec, decision in [(s, "included") for s in a.domain or []] + [(s, "skipped") for s in a.skip or []]:
        dom, _, reason = spec.partition("=")
        if not one(con, "SELECT 1 x FROM domains WHERE code=?", (dom,)):
            raise WriteError(f"no domain {dom!r} in the catalog")
        if decision == "skipped" and not reason.strip():
            raise WriteError(f"--skip {dom}: give the reason (--skip {dom}='why')")
        ctx.x("INSERT INTO review_domains(review_id, domain_code, decision, reason) VALUES (?,?,?,?)",
              (rid, dom, decision, reason.strip() or "proposed for this review"))
    for g in a.goal or []:
        gid = con.execute("SELECT id FROM design_goals WHERE code=?", (g,)).fetchone()
        if not gid:
            raise WriteError(f"no design goal {g}")
        ctx.x("INSERT INTO review_goals(review_id, goal_id) VALUES (?, ?)", (rid, gid[0]))
    n_in, n_out = len(a.domain or []), len(a.skip or [])
    out.append(f"{code}: planning, {n_in} domains proposed, {n_out} skipped")
    return out


REVIEW_STATUSES = ('planning', 'awaiting-approval', 'approved', 'running', 'verifying', 'indexing', 'consolidating',
                   'handed-off', 'abandoned')


@_command("review-status", "move a review to its next status (approved records who approved it and when)",
          "ledger.py review-status R-0001 approved|running|verifying|indexing|consolidating|handed-off|... "
          "--note TEXT [--approved-by A-0001] [--actor A-0001]")
def cmd_review_status(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    if a.status not in REVIEW_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(REVIEW_STATUSES)}")
    sets, args = {"status": a.status, "change_actor_id": ctx.actor_id, "change_note": a.note}, []
    if a.status not in ('planning', 'awaiting-approval', 'abandoned') and rv["approved_at"] is None:
        sets["approved_at"] = now()
        sets["approved_by_actor_id"] = (resolve_actor(con, a.approved_by, "--approved-by") if a.approved_by
                                        else owner_actor(con))
    ctx.x(f"UPDATE reviews SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), rv["id"]))
    return [f"{rv['code']}: {rv['status']} -> {a.status}"]


@_command("snapshot", "record each repository's commit, branch and uncommitted changes for a review (from git)",
          "ledger.py snapshot R-0001 [--repo NAME[@COMMIT]]...")
def cmd_snapshot(ctx):
    rv = review_row(ctx.con, ctx.a.review)
    specs = ctx.a.repo
    if not specs:
        have = [r["name"] for r in rows(ctx.con, """SELECT rp.name FROM review_repositories rr
                 JOIN repositories rp ON rp.id = rr.repository_id WHERE rr.review_id=? ORDER BY rp.id""", (rv["id"],))]
        specs = have or None
    return [record_snapshot(ctx, rv, repo, commit) for repo, commit in _repo_args(ctx.con, specs)]


RUN_STATUSES = ('queued', 'running', 'finished', 'failed', 'cancelled')


@_command("add-run", "record a reviewer run (an approved review and a domain it included)",
          "ledger.py add-run R-0001 DOMAIN LABEL --scope TEXT [--reviewer A-0003 | --model M] "
          "[--prompt TEXT|@file] [--status queued] [--actor A-0001]")
def cmd_add_run(ctx):
    a, con = ctx.a, ctx.con
    rv = review_row(con, a.review)
    if rv["status"] in ('planning', 'awaiting-approval', 'abandoned') or rv["approved_at"] is None:
        raise WriteError(f"{rv['code']} is {rv['status']}; reviewer runs need the owner's approval first "
                         f"(review-status {rv['code']} approved)")
    if not one(con, "SELECT 1 x FROM review_domains WHERE review_id=? AND domain_code=? AND decision='included'",
               (rv["id"], a.domain)):
        raise WriteError(f"{rv['code']} did not include domain {a.domain!r} (review_domains)")
    if a.status not in RUN_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(RUN_STATUSES)}")
    reviewer = resolve_actor(con, a.reviewer, "--reviewer") if a.reviewer else ctx.x(
        "INSERT INTO actors(kind, name, model, parent_actor_id) VALUES ('reviewer', ?, ?, ?)",
        (f"{a.domain} reviewer {a.label}", a.model, ctx.actor_id)).lastrowid
    run = ctx.x("""INSERT INTO reviewer_runs(review_id, domain_code, actor_id, label, scope, prompt, status, started_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (rv["id"], a.domain, reviewer, _need(a.label, "the run's label"), _need(a.scope, "--scope"),
                 _text_arg(a.prompt, "--prompt"), a.status, now() if a.status == "running" else None)).lastrowid
    return [f"run {a.label} (id {run}) of {rv['code']} in {a.domain}, reviewer {actor_code(reviewer)}"]


# ----- issue-status, step-status, import-steps

@_command("issue-status", "change an issue's status with its reason and context, in one transaction",
          "ledger.py issue-status ISS-0001 fixed --note TEXT --after 'what it does now' [--review R-0002] "
          "[--step 12 [--plan P-0001]] [--commit FULLSHA] [--resolution TEXT] [--actor A-0001]")
def cmd_issue_status(ctx):
    a, con = ctx.a, ctx.con
    iss = issue_row(con, a.issue)
    known = [r[0] for r in con.execute("SELECT value FROM issue_statuses ORDER BY position")]
    if a.status not in known:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(known)}")
    note = _need(a.note, "--note (why the status changes)")
    after = _text_arg(a.after, "--after")
    if a.status == "fixed" and not (after or iss["after_state"]):
        raise WriteError("an issue set fixed needs --after (what the system does now)")
    if a.status in ("accepted-risk", "wont-fix") and not ((a.resolution or "").strip() or
                                                          (iss["resolution"] or "").strip()):
        raise WriteError(f"an issue closed as {a.status} needs --resolution (the reason)")
    if a.commit and not FULL_HEX.fullmatch(a.commit):
        raise WriteError("--commit needs the full commit hash")
    sets = {"status": a.status, "change_note": note, "change_actor_id": ctx.actor_id,
            "change_review_id": review_row(con, a.review)["id"] if a.review else None,
            "change_step_id": step_row(con, a.step, a.plan)["id"] if a.step else None,
            "change_commit_sha": a.commit}
    if after:
        sets["after_state"] = after
    if a.resolution:
        sets["resolution"] = a.resolution
    ctx.x(f"UPDATE issues SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), iss["id"]))
    return [f"{iss['code']}: {iss['status']} -> {a.status}"]


STEP_STATUSES = ('not-started', 'doing', 'waiting-owner', 'done', 'dropped')


@_command("step-status", "change a plan step's status with its note (waiting-owner needs the owner's question)",
          "ledger.py step-status 12|P-0001#12|KEY doing|waiting-owner|done|dropped|not-started --note TEXT "
          "[--question TEXT] [--progress-note TEXT] [--commit FULLSHA] [--plan P-0001] [--actor A-0001]")
def cmd_step_status(ctx):
    a, con = ctx.a, ctx.con
    s = step_row(con, a.step, a.plan)
    if a.status not in STEP_STATUSES:
        raise WriteError(f"status {a.status!r} is not one of {', '.join(STEP_STATUSES)}")
    if a.status == "waiting-owner" and not (a.question or "").strip():
        raise WriteError("a step waiting on the owner needs --question (what the owner must answer)")
    if a.status != "waiting-owner" and not (a.note or "").strip():
        raise WriteError("give --note (why the status changes)")
    if a.commit and not FULL_HEX.fullmatch(a.commit):
        raise WriteError("--commit needs the full commit hash")
    sets = {"status": a.status, "change_note": (a.note or "").strip() or None, "change_actor_id": ctx.actor_id,
            "change_commit_sha": a.commit}
    if a.question:
        sets["owner_question"] = a.question
    progress = _text_arg(a.progress_note, "--progress-note")
    if progress or a.status == "done":
        sets["progress_note"] = progress or s["progress_note"] or a.note
    ctx.x(f"UPDATE steps SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), s["id"]))
    return [f"step {s['number']}: {s['status']} -> {a.status}"]


OWNER_NEEDS = ('none', 'decision', 'confirm', 'review', 'production')


def _read_jsonl(path):
    try:
        text = sys.stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WriteError(f"cannot read {path}: {e}")
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise WriteError(f"{path} line {n}: not JSON ({e})")
        if not isinstance(obj, dict):
            raise WriteError(f"{path} line {n}: not a JSON object")
        out.append((n, obj))
    return out


def plan_findings(con, plan_id):
    """{(review code, point id): finding id} of the plan's reviews, and {point id: [review code ...]}."""
    by, points = {}, {}
    for r in con.execute("""SELECT r.code, f.point_id, f.id FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id
                            JOIN findings f ON f.review_id = r.id WHERE pr.plan_id=?""", (plan_id,)):
        by[(r[0], r[1])] = r[2]
        points.setdefault(r[1], []).append(r[0])
    return by, points


def _finding_ref(ref, by, points):
    """The finding id of R-0001:SEC-H1 or a bare SEC-H1 that is unique among the plan's reviews; else None, why."""
    m = re.fullmatch(r"(R-[0-9]{4,})[:/](.+)", ref)
    if m:
        fid = by.get((m.group(1), m.group(2)))
        return (fid, None) if fid else (None, f"no point {ref} among the plan's reviews")
    revs = points.get(ref, [])
    if len(revs) == 1:
        return by[(revs[0], ref)], None
    if not revs:
        return None, f"no point {ref} among the plan's reviews"
    return None, f"point {ref} is in {', '.join(revs)}; write it as {revs[0]}:{ref}"


@_command("import-steps", "load drafted plan steps (JSON lines) into a plan: phases, steps, closes, depends",
          "ledger.py import-steps P-0001 steps.jsonl [--actor A-0001]   (format: references/formats/plan-steps.md)")
def cmd_import_steps(ctx):
    con = ctx.con
    pl = plan_row(con, ctx.a.plan)
    if pl["status"] in ("superseded", "abandoned", "complete"):
        raise WriteError(f"plan {pl['code']} is {pl['status']}; steps go into a live plan")
    items = _read_jsonl(ctx.a.file)
    problems = []
    phases = {r["number"]: r["id"] for r in rows(con, "SELECT id, number FROM phases WHERE plan_id=?", (pl["id"],))}
    new_phases, steps = [], []
    for n, o in items:
        if o.get("kind") == "phase":
            num = o.get("phase")
            if not isinstance(num, int) or num in phases or any(p[1]["phase"] == num for p in new_phases):
                problems.append(f"line {n}: phase {num!r} is not a new phase number")
            for k in ("title", "goal"):
                if not str(o.get(k) or "").strip():
                    problems.append(f"line {n}: phase {num} needs {k}")
            new_phases.append((n, o))
        elif o.get("kind", "step") == "step":
            steps.append((n, o))
        else:
            problems.append(f"line {n}: kind {o.get('kind')!r} is not phase or step")
    known_phases = set(phases) | {o["phase"] for _, o in new_phases if isinstance(o.get("phase"), int)}
    by, points = plan_findings(con, pl["id"])
    keys = {}
    existing_keys = {r["drafter_key"]: r for r in rows(con, "SELECT id, number, drafter_key FROM steps WHERE plan_id=? "
                                                            "AND drafter_key IS NOT NULL", (pl["id"],))}
    for n, o in steps:
        k = str(o.get("key") or "").strip()
        if not k:
            problems.append(f"line {n}: a step needs its key")
        elif k in keys or k in existing_keys:
            problems.append(f"line {n}: key {k} is used twice")
        keys[k] = (n, o)
        for f in ("title", "change", "proof"):
            if not str(o.get(f) or "").strip():
                problems.append(f"line {n} ({k}): needs {f}")
        if o.get("phase") not in known_phases:
            problems.append(f"line {n} ({k}): phase {o.get('phase')!r} is not a phase of {pl['code']}")
        if not isinstance(o.get("order", 0), int):
            problems.append(f"line {n} ({k}): order must be a whole number")
        need = o.get("owner_need") or "none"
        if need not in OWNER_NEEDS:
            problems.append(f"line {n} ({k}): owner_need {need!r} is not one of {', '.join(OWNER_NEEDS)}")
        elif need != "none" and not str(o.get("owner_question") or "").strip():
            problems.append(f"line {n} ({k}): owner_need {need} needs owner_question")
        if not isinstance(o.get("closes", []), list) or not isinstance(o.get("depends", []), list):
            problems.append(f"line {n} ({k}): closes and depends are lists")
            continue
        if not o.get("closes") and not str(o.get("no_findings_reason") or "").strip():
            problems.append(f"line {n} ({k}): closes no point; give closes or no_findings_reason")
        for ref in o.get("closes") or []:
            if _finding_ref(str(ref), by, points)[1]:
                problems.append(f"line {n} ({k}): {_finding_ref(str(ref), by, points)[1]}")
        mr = o.get("multi_reason") or {}
        if not isinstance(mr, dict):
            problems.append(f"line {n} ({k}): multi_reason is an object of point -> reason")
    if problems:
        raise WriteError(f"{len(problems)} problems, nothing imported: " + "; ".join(problems[:12])
                         + (f"; ... {len(problems) - 12} more" if len(problems) > 12 else ""))
    # numbers: after the plan's last step, in (phase, order, line) order
    start = (con.execute("SELECT max(number) FROM steps WHERE plan_id=?", (pl["id"],)).fetchone()[0] or 0) + 1
    ordered = sorted(steps, key=lambda s: (s[1]["phase"], s[1].get("order", 0), s[0]))
    number = {str(o["key"]).strip(): start + i for i, (_, o) in enumerate(ordered)}
    for n, o in ordered:
        k = str(o["key"]).strip()
        for d in o.get("depends") or []:
            dn = number.get(str(d)) or (existing_keys[str(d)]["number"] if str(d) in existing_keys else None)
            if dn is None and isinstance(d, int):
                dn = d if con.execute("SELECT 1 FROM steps WHERE plan_id=? AND number=?", (pl["id"], d)).fetchone() \
                    else None
            if dn is None:
                problems.append(f"line {n} ({k}): depends on unknown step {d!r}")
            elif dn >= number[k]:
                problems.append(f"line {n} ({k}): depends on {d} (step {dn}), which is not earlier (step {number[k]})")
    closers = {}
    for r in con.execute("""SELECT sf.finding_id, sf.multi_reason FROM step_findings sf JOIN steps s ON s.id = sf.step_id
                            WHERE s.plan_id=? AND s.status <> 'dropped'""", (pl["id"],)):
        closers.setdefault(r[0], []).append(r[1])
    for n, o in ordered:
        for ref in o.get("closes") or []:
            closers.setdefault(_finding_ref(str(ref), by, points)[0], []).append(
                (o.get("multi_reason") or {}).get(str(ref)))
    for fid, reasons in closers.items():
        if len(reasons) > 1 and any(not r for r in reasons):
            pid = con.execute("SELECT point_id FROM findings WHERE id=?", (fid,)).fetchone()[0]
            problems.append(f"point {pid} is closed by {len(reasons)} steps; give multi_reason for it on each")
    if problems:
        raise WriteError(f"{len(problems)} problems, nothing imported: " + "; ".join(problems[:12])
                         + (f"; ... {len(problems) - 12} more" if len(problems) > 12 else ""))
    for _, o in new_phases:
        phases[o["phase"]] = ctx.x("INSERT INTO phases(plan_id, number, title, goal, why) VALUES (?,?,?,?,?)",
                                   (pl["id"], o["phase"], o["title"], o["goal"], o.get("why"))).lastrowid
    ids = {}
    for n, o in ordered:
        k = str(o["key"]).strip()
        ids[k] = ctx.x("""INSERT INTO steps(plan_id, phase_id, number, section, title, change, proof, owner_need,
                          owner_question, behavior_change, behavior_note, drafter_key, no_findings_reason,
                          change_actor_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (pl["id"], phases[o["phase"]], number[k], o.get("section"), o["title"], o["change"],
                        o["proof"], o.get("owner_need") or "none", o.get("owner_question"),
                        1 if o.get("behavior_change") else 0, o.get("behavior_note"), k,
                        o.get("no_findings_reason"), ctx.actor_id)).lastrowid
    for n, o in ordered:
        k = str(o["key"]).strip()
        for ref in o.get("closes") or []:
            ctx.x("INSERT INTO step_findings(step_id, finding_id, multi_reason) VALUES (?,?,?)",
                  (ids[k], _finding_ref(str(ref), by, points)[0], (o.get("multi_reason") or {}).get(str(ref))))
        for d in o.get("depends") or []:
            dep = ids.get(str(d)) or (existing_keys[str(d)]["id"] if str(d) in existing_keys else
                                      con.execute("SELECT id FROM steps WHERE plan_id=? AND number=?",
                                                  (pl["id"], d)).fetchone()[0])
            ctx.x("INSERT OR IGNORE INTO step_dependencies(step_id, depends_on_step_id) VALUES (?,?)", (ids[k], dep))
    out = [f"{pl['code']}: imported {len(new_phases)} phases and {len(ordered)} steps "
           f"({start}-{start + len(ordered) - 1})" if ordered else f"{pl['code']}: imported {len(new_phases)} phases"]
    uncovered = con.execute("SELECT count(*) FROM v_uncovered_findings WHERE plan=?", (pl["code"],)).fetchone()[0]
    problems = con.execute("SELECT count(*) FROM v_coverage_problems WHERE plan=?", (pl["code"],)).fetchone()[0]
    out.append(f"coverage: {uncovered} points of the plan's reviews closed by no step, {problems} coverage problems")
    return out


# ----- gates: open-gate, answer-gate (shared with the viewer), apply-gate

GATE_KINDS_HELP = ("decision|confirm|domain-approval|plan-approval|step-input|confirm-false-finding|accept-risk|"
                   "production-action|review-handoff|other")


def _gate_options(specs, raw_json):
    """Options as a list of {"value","label"[, "description"]} from --option value=label[:description] arguments
    or --options-json; values must be unique."""
    opts = []
    if raw_json:
        try:
            opts = json.loads(_text_arg(raw_json, "--options-json"))
        except ValueError as e:
            raise WriteError(f"--options-json is not JSON ({e})")
        if not isinstance(opts, list) or not all(isinstance(o, dict) for o in opts):
            raise WriteError("--options-json must be a JSON array of objects")
    for s in specs or []:
        value, sep, rest = s.partition("=")
        label, _, desc = rest.partition(":")
        if not sep or not value.strip() or not label.strip():
            raise WriteError(f"--option {s!r}: write it as value=label or value=label:description")
        opts.append({"value": value.strip(), "label": label.strip(), **({"description": desc.strip()} if desc else {})})
    for o in opts:
        if not isinstance(o.get("value"), str) or not o["value"].strip() or not isinstance(o.get("label"), str) \
                or not o["label"].strip():
            raise WriteError("every option needs a text value and label")
        if o.get("description") is not None and not isinstance(o["description"], str):
            raise WriteError(f"option {o['value']}: description must be text")
    values = [o["value"] for o in opts]
    dup = sorted({v for v in values if values.count(v) > 1})
    if dup:
        raise WriteError(f"option values must be unique: {', '.join(dup)}")
    return opts


@_command("open-gate", "ask the owner something (prints the gate's code)",
          f"ledger.py open-gate {GATE_KINDS_HELP} --title T --question Q [--context TEXT|@file] "
          "[--option value=label[:description]]... [--options-json JSON|@file] [--no-text] [--non-blocking] "
          "[--review R-0001] [--plan P-0001] [--step 12] [--issue ISS-0001] [--decision D-0001] [--actor A-0001]")
def cmd_open_gate(ctx):
    a, con = ctx.a, ctx.con
    kinds = [r[0] for r in con.execute("SELECT value FROM gate_kinds ORDER BY position")]
    if a.kind not in kinds:
        raise WriteError(f"kind {a.kind!r} is not one of {', '.join(kinds)}")
    opts = _gate_options(a.option, a.options_json)
    if a.no_text and not opts:
        raise WriteError("a gate without free text needs at least one --option")
    code = next_code(con, "GATE")
    plan = plan_row(con, a.plan) if a.plan or a.step else None
    dec = one(con, "SELECT id FROM decisions WHERE code=?", (a.decision,)) if a.decision else None
    if a.decision and not dec:
        raise WriteError(f"no decision {a.decision}")
    ctx.x("""INSERT INTO gates(code, kind, title, question, context, options, allow_text, blocking, review_id, plan_id,
             step_id, issue_id, decision_id, opened_by_actor_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (code, a.kind, _need(a.title, "--title"), _need(a.question, "--question"), _text_arg(a.context, "--context"),
           json.dumps(opts) if opts else None, 0 if a.no_text else 1, 0 if a.non_blocking else 1,
           review_row(con, a.review)["id"] if a.review else None, plan["id"] if plan else None,
           step_row(con, a.step, plan["code"])["id"] if a.step else None,
           issue_row(con, a.issue)["id"] if a.issue else None, dec["id"] if dec else None, ctx.actor_id))
    return [code]


def answer_gate(con, code, value=None, text=None, via="chat", actor_id=None):
    """Record the owner's answer to an open gate: the one set of answer rules for the viewer (via 'viewer') and
    `ledger.py answer-gate` (via 'chat' or 'other'). On the caller's open transaction, or else in a BEGIN IMMEDIATE
    of its own (committed here): the gate is read and updated in that one transaction and the UPDATE must change
    exactly one open row, so two answers cannot both be saved. actor_id is who answered (default: the project's
    owner actor, made on first use). Raises GateRefused(status, message) for 404 no such gate, 409 answered
    already, 400 an answer the gate does not take; returns {"ok": True}."""
    if not isinstance(con, sqlite3.Connection):  # answer_gate(db, code, body): the viewer's request form
        return gate_answer_request(con, code, value if isinstance(value, dict) else {})
    if (value is not None and not isinstance(value, str)) or (text is not None and not isinstance(text, str)):
        raise GateRefused(400, "An answer is text.")
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        g = one(con, "SELECT * FROM gates WHERE code=?", (code,))
        if not g:
            raise GateRefused(404, "no such gate")
        if g["status"] != "open":
            raise GateRefused(409, "This question was already answered.")
        val = (value or "").strip() or None
        txt = (text or "").strip() or None
        opts = [o.get("value") for o in json.loads(g["options"] or "[]")]
        if val is not None and val not in opts:
            raise GateRefused(400, "That choice is not one of the options.")
        if val is None and not txt:
            raise GateRefused(400, "Choose an option or write an answer.")
        if txt and not g["allow_text"] and val is None:
            raise GateRefused(400, "Choose one of the options.")
        if via not in ("chat", "viewer", "other"):
            raise GateRefused(400, "answered via must be chat, viewer or other")
        who = actor_id if actor_id is not None else owner_actor(con)
        cur = con.execute("""UPDATE gates SET status='answered', answer_value=?, answer_text=?,
                             answered_by=(SELECT CASE kind WHEN 'owner' THEN 'owner' ELSE name END FROM actors
                                          WHERE id=?), answered_via=?, answered_by_actor_id=?
                             WHERE id=? AND status='open'""", (val, txt, who, via, who, g["id"]))
        if cur.rowcount != 1:
            raise GateRefused(409, "This question was already answered.")
        if own:
            con.execute("COMMIT")
        return {"ok": True}
    finally:
        if own and con.in_transaction:
            con.execute("ROLLBACK")


@_command("answer-gate", "record the owner's answer given in chat (the viewer's rules)",
          "ledger.py answer-gate GATE-0001 [--value OPTION] [--text 'their words'] [--via chat|other] "
          "[--by A-0001 (default: the owner)]")
def cmd_answer_gate(ctx):
    a = ctx.a
    who = resolve_actor(ctx.con, a.by, "--by") if a.by else None
    answer_gate(ctx.con, a.gate, a.value, _text_arg(a.text, "--text"), a.via, who)
    g = one(ctx.con, "SELECT answer_value, answer_text FROM gates WHERE code=?", (a.gate,))
    return [f"{a.gate}: answered ({', '.join(x for x in (g['answer_value'], g['answer_text']) if x)})"]


@_command("apply-gate", "record what was done with a gate's answer (a decision gate also records the decision)",
          "ledger.py apply-gate GATE-0001 --note 'what was done with the answer' [--revisit-when TEXT] "
          "[--actor A-0001]")
def cmd_apply_gate(ctx):
    a, con = ctx.a, ctx.con
    note = _need(_text_arg(a.note, "--note"), "--note (what was done with the answer)")
    g = one(con, "SELECT * FROM gates WHERE code=?", (a.gate,))
    if not g:
        raise WriteError(f"no gate {a.gate}")
    if g["status"] != "answered":
        raise WriteError(f"{a.gate} is {g['status']}; only an answered gate can be applied")
    out = []
    if g["kind"] == "decision":
        answer = " - ".join(x for x in (g["answer_value"], g["answer_text"]) if x)
        if g["decision_id"]:
            ctx.x("""UPDATE decisions SET status='decided', answer=?, rationale=?, decided_by_actor_id=?,
                     revisit_when=COALESCE(?, revisit_when) WHERE id=?""",
                  (answer, note, g["answered_by_actor_id"], a.revisit_when, g["decision_id"]))
            dcode = con.execute("SELECT code FROM decisions WHERE id=?", (g["decision_id"],)).fetchone()[0]
        else:
            dcode = next_code(con, "D")
            did = ctx.x("""INSERT INTO decisions(code, question, options, answer, rationale, status, revisit_when,
                           review_id, step_id, asked_by_actor_id, decided_by_actor_id)
                           VALUES (?,?,?,?,?, 'decided', ?,?,?,?,?)""",
                        (dcode, g["question"], g["options"], answer, note, a.revisit_when, g["review_id"],
                         g["step_id"], g["opened_by_actor_id"], g["answered_by_actor_id"])).lastrowid
            ctx.x("UPDATE gates SET decision_id=? WHERE id=?", (did, g["id"]))
            if g["issue_id"]:
                ctx.x("INSERT OR IGNORE INTO decision_issues(decision_id, issue_id) VALUES (?,?)", (did, g["issue_id"]))
        out.append(f"decision {dcode}: {answer}")
    ctx.x("UPDATE gates SET status='applied', applied_note=?, change_actor_id=? WHERE id=?",
          (note, ctx.actor_id, g["id"]))
    return [f"{a.gate}: applied"] + out


# ----- findings: import-findings, import-checks, load-index, match

FINDING_KINDS = ('defect', 'test', 'validation', 'refactor', 'feature', 'design', 'docs', 'logging', 'perf',
                 'security', 'ops', 'verify')
INDEX_COLUMNS = ("id", "sev", "kind", "theme", "sub", "locations", "where_report", "summary", "before", "fix", "test",
                 "related", "known_issue", "domain", "goals", "requirements")
CHECK_COLUMNS = ("issue", "outcome", "evidence", "finding")
MATCH_COLUMNS = ("point", "issue", "match_type", "confidence", "rationale", "relations")
TRACE_COLUMNS = ("requirement", "verdict", "where_implemented", "tested", "finding", "evidence")
VERDICTS = ('matches', 'differs', 'partly', 'missing', 'untested')
LOCATION = re.compile(r"^(?P<path>[^:#]+?)(?::(?P<a>[0-9]+)(?:-(?P<b>[0-9]+))?)?(?:#(?P<sym>[^#]+))?$")


def fingerprint(domain, theme, sub):
    """What findings and issues are matched on: domain:theme:sub. The symbol is not part of it (WALK-L17), so the
    same problem cited with and without a symbol, or at another function, gets one fingerprint."""
    return f"{domain}:{theme or ''}:{sub}"


def parse_location(text, repo_names, default_repo=None):
    """(repository name, path, start, end, symbol) from 'repo:path[:start[-end]][#symbol]'; the repository may be left
    out when the review has one. 'repo:path' alone is the whole file. ValueError with the reason."""
    t = text.strip()
    repo, rest = None, t
    head, sep, tail = t.partition(":")
    if sep and head in repo_names:
        repo, rest = head, tail
    elif default_repo is not None:
        repo = default_repo
    else:
        raise ValueError(f"location {text!r} does not start with one of the review's repositories "
                         f"({', '.join(sorted(repo_names))})")
    m = LOCATION.match(rest)
    if not m:
        raise ValueError(f"location {text!r} is not repo:path[:start[-end]][#symbol]")
    path = m.group("path").strip()
    if bad_location_path(path):
        raise ValueError(f"location {text!r}: the path must be relative, without .. or a leading -")
    a = int(m.group("a")) if m.group("a") else None
    b = int(m.group("b")) if m.group("b") else a
    if a is not None and (a < 1 or b < a):
        raise ValueError(f"location {text!r}: lines must be start-end with 1 <= start <= end")
    return repo, path, a, b, (m.group("sym") or "").strip() or None


def _split_list(text):
    return [x for x in re.split(r"[,\s]+", text or "") if x]


def _review_repos(con, review_id):
    return {r["name"]: r for r in rows(con, """SELECT rp.*, rr.commit_sha FROM review_repositories rr
            JOIN repositories rp ON rp.id = rr.repository_id WHERE rr.review_id=?""", (review_id,))}


def _read_tsv(path, columns, what):
    """[(line number, [cells])] of a tab-separated file without header; every row must have len(columns) cells."""
    try:
        text = sys.stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise WriteError(f"cannot read {path}: {e}")
    out, bad = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) != len(columns):
            bad.append(f"{what} line {n}: {len(cells)} columns, expected {len(columns)} ({', '.join(columns)})")
            continue
        out.append((n, cells))
    if bad:
        raise WriteError(f"{len(bad)} bad rows: " + "; ".join(bad[:8]))
    return out


def import_findings(ctx, rv, run_label, path):
    """Validate a findings index TSV (references/formats/index.md) completely, then insert every point unmatched with
    its locations (the first one primary), goals and requirement links. All or nothing. Returns lines to print."""
    con = ctx.con
    run = one(con, "SELECT * FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], run_label))
    if not run:
        raise WriteError(f"{rv['code']} has no reviewer run {run_label!r} (add-run)")
    repos = _review_repos(con, rv["id"])
    if not repos:
        raise WriteError(f"{rv['code']} has no repositories (snapshot)")
    default_repo = next(iter(repos)) if len(repos) == 1 else None
    themes = {r[0] for r in con.execute("SELECT code FROM themes WHERE retired_at IS NULL")}
    domains = {r[0] for r in con.execute("SELECT code FROM domains WHERE retired_at IS NULL")}
    sevs = {r[0] for r in con.execute("SELECT value FROM severities")}
    taken = {r[0] for r in con.execute("SELECT point_id FROM findings WHERE review_id=?", (rv["id"],))}
    problems, points, seen, exists_cache = [], [], {}, {}
    for n, c in _read_tsv(path, INDEX_COLUMNS, "index"):
        row = dict(zip(INDEX_COLUMNS, c))
        pid, where = row["id"], f"line {n} ({row['id'] or 'no id'})"
        if not pid:
            problems.append(f"line {n}: no id")
        elif pid in seen:
            problems.append(f"{where}: id used twice (also line {seen[pid]})")
        elif pid in taken:
            problems.append(f"{where}: {rv['code']} already has a point {pid}")
        seen.setdefault(pid, n)
        if row["sev"] not in sevs:
            problems.append(f"{where}: severity {row['sev']!r} is not one of {', '.join(sorted(sevs))}")
        if row["kind"] not in FINDING_KINDS:
            problems.append(f"{where}: kind {row['kind']!r} is not one of {', '.join(FINDING_KINDS)}")
        if row["theme"] not in themes:
            problems.append(f"{where}: unknown theme {row['theme']!r}")
        dom = row["domain"] or run["domain_code"]
        if dom not in domains:
            problems.append(f"{where}: unknown domain {dom!r}")
        for k in ("sub", "summary"):
            if not row[k]:
                problems.append(f"{where}: needs {k}")
        locs = []
        for t in [x for x in row["locations"].split(";") if x.strip()]:
            try:
                rname, p, a, b, sym = parse_location(t, set(repos), default_repo)
            except ValueError as e:
                problems.append(f"{where}: {e}"); continue
            key = (rname, p)
            if key not in exists_cache:
                r = repos[rname]
                try:
                    git(repo_dir(ctx.root, r["path"], bool(r["allow_outside_root"])), "cat-file", "-e",
                        f"{r['commit_sha']}:{p}")
                    exists_cache[key] = True
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    exists_cache[key] = False
            if not exists_cache[key]:
                problems.append(f"{where}: {rname}:{p} does not exist at the reviewed commit "
                                f"{repos[rname]['commit_sha'][:10]}")
            locs.append((rname, p, a, b, sym))
        if not locs:
            problems.append(f"{where}: needs at least one location")
        goals = []
        for g in _split_list(row["goals"]):
            gid = con.execute("SELECT id FROM design_goals WHERE code=?", (g,)).fetchone()
            if not gid:
                problems.append(f"{where}: unknown design goal {g}")
            else:
                goals.append(gid[0])
        reqs = []
        for spec in _split_list(row["requirements"]):
            code, _, verdict = spec.partition("=")
            rid = con.execute("SELECT id FROM requirements WHERE code=?", (code,)).fetchone()
            if not rid:
                problems.append(f"{where}: unknown requirement {code}")
            elif verdict not in VERDICTS:
                problems.append(f"{where}: requirement {code} needs =verdict ({', '.join(VERDICTS)})")
            else:
                reqs.append((rid[0], verdict))
        known = row["known_issue"]
        if known and not con.execute("SELECT 1 FROM issues WHERE code=?", (known,)).fetchone():
            problems.append(f"{where}: unknown known issue {known}")
        points.append((row, dom, locs, goals, reqs))
    for row, *_ in points:
        for r in _split_list(row["related"]):
            if r not in seen:
                problems.append(f"{row['id']}: related id {r} is not in this file")
    if problems:
        raise WriteError(f"{len(problems)} problems in {path}, nothing loaded: " + "; ".join(problems[:10])
                         + (f"; ... {len(problems) - 10} more" if len(problems) > 10 else ""))
    counts = {}
    for row, dom, locs, goals, reqs in points:
        related = _split_list(row["related"])
        fid = ctx.x("""INSERT INTO findings(review_id, reviewer_run_id, point_id, severity, kind, domain_code,
                       theme_code, sub, summary, detail, before_state, recommendation, recommended_test, where_report,
                       fingerprint, related_point_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rv["id"], run["id"], row["id"], row["sev"], row["kind"], dom, row["theme"], row["sub"],
                     row["summary"], f"The report names {row['known_issue']}." if row["known_issue"] else None,
                     row["before"] or None, row["fix"] or None, row["test"] or None, row["where_report"] or None,
                     fingerprint(dom, row["theme"], row["sub"]), json.dumps(related) if related else None)).lastrowid
        for i, (rname, p, a, b, sym) in enumerate(locs):
            ctx.x("""INSERT INTO finding_locations(finding_id, repository_id, path, line_start, line_end, symbol, role,
                     whole_file) VALUES (?,?,?,?,?,?,?,?)""",
                  (fid, repos[rname]["id"], p, a, b, sym, "primary" if i == 0 else "also",
                   1 if a is None and sym is None else 0))
        for gid in goals:
            ctx.x("INSERT INTO finding_goals(finding_id, goal_id) VALUES (?,?)", (fid, gid))
        for rid, verdict in reqs:
            ctx.x("INSERT INTO finding_requirements(finding_id, requirement_id, verdict) VALUES (?,?,?)",
                  (fid, rid, verdict))
        counts[row["sev"]] = counts.get(row["sev"], 0) + 1
    return [f"{rv['code']} {run_label}: loaded {len(points)} points ("
            + ", ".join(f"{k} {counts[k]}" for k in sorted(counts)) + ") unmatched; next: fill-refs, then match"]


CHECK_OUTCOMES = ('still-present', 'changed', 'fixed', 'regressed', 'obsolete', 'not-checked')


def import_checks(ctx, rv, path, actor_id):
    """Validate a known-issues TSV (issue, outcome, evidence, finding) and insert review_issue_checks. All or
    nothing."""
    con = ctx.con
    problems, out, seen = [], [], set()
    points = {r[0]: r[1] for r in con.execute("SELECT point_id, id FROM findings WHERE review_id=?", (rv["id"],))}
    for n, c in _read_tsv(path, CHECK_COLUMNS, "known issues"):
        code, outcome, evidence, point = c
        where = f"line {n} ({code})"
        iss = one(con, "SELECT id FROM issues WHERE code=?", (code,))
        if not iss:
            problems.append(f"{where}: no issue {code}"); continue
        if code in seen:
            problems.append(f"{where}: {code} is checked twice")
        seen.add(code)
        if outcome not in CHECK_OUTCOMES:
            problems.append(f"{where}: outcome {outcome!r} is not one of {', '.join(CHECK_OUTCOMES)}")
        if outcome in ("fixed", "regressed", "changed", "still-present", "obsolete") and not evidence:
            problems.append(f"{where}: a {outcome} check needs evidence")
        if point and point not in points:
            problems.append(f"{where}: {rv['code']} has no point {point}")
        out.append((iss["id"], outcome, evidence or None, points.get(point) if point else None))
    if problems:
        raise WriteError(f"{len(problems)} problems in {path}, nothing loaded: " + "; ".join(problems[:10]))
    for iid, outcome, evidence, fid in out:
        ctx.x("""INSERT INTO review_issue_checks(review_id, issue_id, outcome, finding_id, evidence, actor_id)
                 VALUES (?,?,?,?,?,?)""", (rv["id"], iid, outcome, fid, evidence, actor_id))
    return [f"{rv['code']}: recorded {len(out)} checks of known issues"]


def import_trace(ctx, rv, path):
    """A requirement trace TSV (requirement, verdict, where_implemented, tested, finding, evidence) into
    review_requirement_checks."""
    con = ctx.con
    problems, out = [], []
    points = {r[0]: r[1] for r in con.execute("SELECT point_id, id FROM findings WHERE review_id=?", (rv["id"],))}
    for n, c in _read_tsv(path, TRACE_COLUMNS, "requirement trace"):
        code, verdict, where_impl, tested, point, evidence = c
        req = con.execute("SELECT id FROM requirements WHERE code=?", (code,)).fetchone()
        if not req:
            problems.append(f"line {n}: no requirement {code}"); continue
        if verdict not in VERDICTS:
            problems.append(f"line {n}: verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
        if tested and tested not in ("yes", "partly", "no"):
            problems.append(f"line {n}: tested must be yes, partly or no")
        if point and point not in points:
            problems.append(f"line {n}: {rv['code']} has no point {point}")
        out.append((req[0], verdict, where_impl or None, tested or None, points.get(point), evidence or None))
    if problems:
        raise WriteError(f"{len(problems)} problems in {path}, nothing loaded: " + "; ".join(problems[:10]))
    for r in out:
        ctx.x("""INSERT INTO review_requirement_checks(review_id, requirement_id, verdict, where_implemented, tested,
                 finding_id, evidence) VALUES (?,?,?,?,?,?,?)""", (rv["id"], *r))
    return [f"{rv['code']}: recorded {len(out)} requirement verdicts"]


@_command("import-findings", "load an index TSV of one report into the ledger (all or nothing, unmatched)",
          "ledger.py import-findings R-0001 RUN_LABEL points.tsv   (format: references/formats/index.md)")
def cmd_import_findings(ctx):
    return import_findings(ctx, review_row(ctx.con, ctx.a.review), ctx.a.run, ctx.a.file)


@_command("import-checks", "load a report's known-issues TSV into review_issue_checks (all or nothing)",
          "ledger.py import-checks R-0001 known.tsv [--run RUN_LABEL (the checks' actor)]   "
          "(format: references/formats/index.md)")
def cmd_import_checks(ctx):
    rv = review_row(ctx.con, ctx.a.review)
    actor = ctx.actor_id
    if ctx.a.run:
        run = one(ctx.con, "SELECT actor_id FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], ctx.a.run))
        if not run:
            raise WriteError(f"{rv['code']} has no reviewer run {ctx.a.run!r}")
        actor = run["actor_id"]
    return import_checks(ctx, rv, ctx.a.file, actor)


@_command("load-index", "load one report's points, known-issue checks and requirement trace in one transaction",
          "ledger.py load-index R-0001 RUN_LABEL points.tsv [known.tsv] [--trace trace.tsv]   "
          "(formats: references/formats/index.md)")
def cmd_load_index(ctx):
    rv = review_row(ctx.con, ctx.a.review)
    out = import_findings(ctx, rv, ctx.a.run, ctx.a.points)
    run = one(ctx.con, "SELECT actor_id FROM reviewer_runs WHERE review_id=? AND label=?", (rv["id"], ctx.a.run))
    if ctx.a.known:
        out += import_checks(ctx, rv, ctx.a.known, run["actor_id"])
    if ctx.a.trace:
        out += import_trace(ctx, rv, ctx.a.trace)
    return out


MATCH_TYPES_HELP = "new|still-present|changed|regression|recurrence|continuation"
CONFIDENCES = ('certain', 'likely', 'possible')


def parse_relations(text):
    """[(relation, issue code)] from 'relation:ISS-0001;relation:ISS-0002' (the matcher TSV's extra relations)."""
    out = []
    for part in [p.strip() for p in (text or "").split(";") if p.strip()]:
        rel, sep, code = part.partition(":")
        if not sep or not re.fullmatch(r"ISS-[0-9]{4,}", code.strip()) or not rel.strip():
            raise WriteError(f"relation {part!r} is not relation:ISS-0001")
        out.append((rel.strip(), code.strip()))
    return out


def open_issue_for(ctx, f, rv, title=None):
    """A new issue (next ISS- code) from a finding: its title, domain, theme, sub, kind, severity, fingerprint, before
    state and recommendation; first seen in the finding's review."""
    code = next_code(ctx.con, "ISS")
    iid = ctx.x("""INSERT INTO issues(code, title, domain_code, theme_code, sub, kind, severity, fingerprint,
                   before_state, recommendation, first_seen_review_id, change_actor_id, change_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, title or f["summary"], f["domain_code"], f["theme_code"], f["sub"], f["kind"], f["severity"],
                 f["fingerprint"] or fingerprint(f["domain_code"], f["theme_code"], f["sub"]), f["before_state"],
                 f["recommendation"], rv["id"], ctx.actor_id, f"opened from finding {f['point_id']}")).lastrowid
    return iid, code


def match_finding(ctx, rv, point, target, match_type=None, confidence="likely", rationale=None, relations=""):
    """Match one finding by UPDATE (the insert-then-update rule: tr_finding_matched does the bookkeeping). target is
    an ISS- code or 'new' (the next ISS- code). A false finding with target new gets its own issue, closed as
    obsolete with resolution 'false finding: <why>' (C-11). Returns the line to print."""
    con = ctx.con
    rels = parse_relations(relations)
    f = one(con, "SELECT * FROM findings WHERE review_id=? AND point_id=?", (rv["id"], point))
    if not f:
        raise WriteError(f"{rv['code']} has no point {point}")
    if f["issue_id"] is not None:
        have = con.execute("SELECT code FROM issues WHERE id=?", (f["issue_id"],)).fetchone()[0]
        if have == target or str(target).lower() == "new":
            raise WriteError(f"{point} is already matched to {have}"
                             + ("; give an ISS- code to re-point it" if have != target else ""))
    if confidence not in CONFIDENCES:
        raise WriteError(f"confidence {confidence!r} is not one of {', '.join(CONFIDENCES)}")
    types = [r[0] for r in con.execute("SELECT value FROM match_types ORDER BY position")]
    known_rel = {r[0] for r in con.execute("SELECT value FROM relation_types")}
    for rel, code in rels:
        if rel not in known_rel:
            raise WriteError(f"relation {rel!r} is not one of {', '.join(sorted(known_rel))}")
        issue_row(con, code)
    false = f["verification"] == "false"
    if str(target).lower() == "new":
        match_type = match_type or "new"
        if match_type not in ("new", "continuation"):
            raise WriteError(f"a new issue is matched as new or continuation, not {match_type}")
        if match_type == "continuation" and not any(r == "continuation-of" for r, _ in rels):
            raise WriteError("a continuation needs its relation: --relations continuation-of:ISS-0001")
        iid, code = open_issue_for(ctx, f, rv)
    else:
        iss = issue_row(con, target)
        iid, code = iss["id"], iss["code"]
        if not match_type:
            raise WriteError(f"give --type for a match to an existing issue ({MATCH_TYPES_HELP})")
    if match_type not in types:
        raise WriteError(f"match type {match_type!r} is not one of {', '.join(types)}")
    ctx.x("""UPDATE findings SET issue_id=?, match_type=?, match_confidence=?, match_rationale=? WHERE id=?""",
          (iid, match_type, confidence, rationale, f["id"]))
    for rel, other in rels:
        ctx.x("""INSERT INTO issue_relations(from_issue_id, to_issue_id, relation, confidence, rationale,
                 established_in_review_id, established_by_actor_id)
                 VALUES (?, (SELECT id FROM issues WHERE code=?), ?, ?, ?, ?, ?)""",
              (iid, other, rel, confidence, rationale or f"from the match of {point}", rv["id"], ctx.actor_id))
    note = ""
    if false and str(target).lower() == "new":
        why = (f["verification_note"] or rationale or "").strip()
        if not why:
            raise WriteError(f"{point} is a false finding: give --rationale (why it is wrong) for its obsolete issue")
        ctx.x("""UPDATE issues SET status='obsolete', resolution=?, change_note=?, change_review_id=?,
                 change_actor_id=? WHERE id=?""",
              (f"false finding: {why}", f"false finding {point}", rv["id"], ctx.actor_id, iid))
        note = " (false finding: closed as obsolete)"
    return f"{point} -> {code} {match_type}{note}"


@_command("match", "match a finding to an issue (an ISS- code, or new for the next code); or a matcher TSV",
          f"ledger.py match R-0001 POINT ISS-0001|new --type {MATCH_TYPES_HELP} [--confidence certain|likely|possible] "
          "[--rationale TEXT] [--relations 'related-to:ISS-0002;caused-by:ISS-0003']   |   "
          "ledger.py match R-0001 --file matches.tsv   (format: references/formats/index.md)")
def cmd_match(ctx):
    a = ctx.a
    rv = review_row(ctx.con, a.review)
    if a.file:
        if a.point or a.target:
            raise WriteError("give either --file or POINT and ISSUE, not both")
        out = []
        for n, c in _read_tsv(a.file, MATCH_COLUMNS, "matches"):
            point, target, mtype, conf, why, rels = c
            try:
                out.append(match_finding(ctx, rv, point, target, mtype or None, conf or "likely", why or None, rels))
            except WriteError as e:
                raise WriteError(f"line {n} ({point}): {e}")
        return out + [f"{rv['code']}: matched {len(out)} points"]
    if not a.point or not a.target:
        raise WriteError("give POINT and ISSUE (an ISS- code or new), or --file")
    return [match_finding(ctx, rv, a.point, a.target, a.type, a.confidence, _text_arg(a.rationale, "--rationale"),
                          a.relations or "")]


# ----- report-finding: a defect found during fix work (step 127)

def cited_location(root, repo, commit, spec_path, a, b, sym):
    """Read path at commit with git and check the citation: the lines exist, and a symbol given with lines is on one
    of them (given alone, it is looked up). Returns the location's fields with the excerpt filled from that commit."""
    d = repo_dir(root, repo["path"], bool(repo["allow_outside_root"]))
    try:
        data = git(d, "show", f"{commit}:{spec_path}")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        raise WriteError(f"{spec_path} does not exist at commit {commit[:10]} (cite lines with "
                         f"git show {commit[:10]}:{spec_path}; code added later is cited at the commit that added it)")
    try:
        text = None if b"\0" in data else data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    out = {"commit_sha": commit, "path": spec_path, "line_start": a, "line_end": b, "symbol": sym,
           "whole_file": 0, "excerpt": None, "excerpt_sha256": None, "excerpt_redacted": 0, "excerpt_status": None}
    try:
        out["blob_sha"] = git(d, "rev-parse", f"{commit}:{spec_path}").decode().strip()
    except RuntimeError:
        out["blob_sha"] = None
    if text is None:
        if a is not None or sym:
            raise WriteError(f"{spec_path} is not a text file at {commit[:10]}; cite it whole (repo:path)")
        out.update(whole_file=1, excerpt_status="not-text", excerpt_sha256=hashlib.sha256(data).hexdigest())
        return out
    lines = text.splitlines()
    if a is None and sym is None:
        if not lines:
            raise WriteError(f"{spec_path} is empty at {commit[:10]}; nothing to cite")
        a, b = 1, len(lines)
        out.update(whole_file=1, line_start=a, line_end=b)
    elif a is None:
        a, why = symbol_line(lines, sym)
        if a is None:
            raise WriteError(f"{spec_path} at {commit[:10]}: {why}")
        b = a
        out.update(line_start=a, line_end=b)
    if b > len(lines):
        raise WriteError(f"{spec_path} has {len(lines)} lines at {commit[:10]}; lines {a}-{b} are past its end "
                         f"(cite the lines of git show {commit[:10]}:{spec_path}, not of an edited file)")
    if sym and out["line_start"] == a and not any(
            re.search(r"(?<![\w$])" + re.escape(sym) + r"(?![\w$])", t) for t in lines[a - 1:b]):
        raise WriteError(f"{sym!r} is not in {spec_path} lines {a}-{b} at {commit[:10]}")
    cut = out["whole_file"] and b - a + 1 > WHOLE_FILE_EXCERPT_LINES
    excerpt = "\n".join(lines[a - 1:(a - 1 + WHOLE_FILE_EXCERPT_LINES) if cut else b])
    shown, masked = mask_secrets(excerpt)
    out.update(excerpt=shown, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
               excerpt_redacted=1 if masked else 0, excerpt_status="truncated" if cut else None)
    return out


def discovery_candidates(con, fp, locs, exclude_review_point=None):
    """Issues that may be the same problem: the same fingerprint (domain:theme:sub), a location at the same path and
    symbol, or (without a symbol) at lines of the same path that overlap at the same commit. [(code, status, title,
    why)]."""
    found = {}
    for r in con.execute("SELECT code, status, title FROM issues WHERE fingerprint=?", (fp,)):
        found.setdefault(r[0], (r[0], r[1], r[2], "same fingerprint"))
    for loc in locs:
        sql = """SELECT DISTINCT i.code, i.status, i.title FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
                 JOIN issues i ON i.id = f.issue_id WHERE fl.path=?"""
        args = [loc["path"]]
        if loc["symbol"]:
            sql += " AND fl.symbol=?"; args.append(loc["symbol"])
            why = f"same path and symbol {loc['path']}#{loc['symbol']}"
        else:  # lines overlapping at the same commit
            sql += " AND fl.commit_sha=? AND fl.line_start <= ? AND COALESCE(fl.line_end, fl.line_start) >= ?"
            args += [loc["commit_sha"], loc["line_end"], loc["line_start"]]
            why = f"overlapping lines of {loc['path']}"
        for r in con.execute(sql, args):
            found.setdefault(r[0], (r[0], r[1], r[2], why))
    return sorted(found.values())


@_command("report-finding", "record a defect found during fix work, match it and plan it (or ask the owner)",
          "ledger.py report-finding --step 12 --severity H|M|L|T --domain CODE --where 'path:start-end#symbol' "
          "[--where ...] --summary TEXT --before TEXT --fix TEXT --test 'test_name: what it asserts' "
          "[--kind defect] [--theme CODE] [--sub SLUG] [--commit SHA (default: the review's commit)] "
          "[--review R-0001] [--plan P-0001] [--issue ISS-0001 | --new-issue] "
          "[--into-step 30 | --new-step 'Title' [--change TEXT] [--proof TEXT] [--phase N] | --gate] [--actor A-0001]")
def cmd_report_finding(ctx):
    a, con = ctx.a, ctx.con
    for flag, value in (("--where", a.where), ("--before", a.before), ("--fix", a.fix), ("--summary", a.summary),
                        ("--severity", a.severity), ("--domain", a.domain), ("--step", a.step)):
        if not value or (isinstance(value, str) and not value.strip()):
            raise WriteError(f"a discovery needs {flag}")
    if a.into_step and (a.new_step or a.gate) or (a.new_step and a.gate):
        raise WriteError("say where it is planned once: --into-step, --new-step or --gate")
    if a.issue and a.new_issue:
        raise WriteError("give --issue or --new-issue, not both")
    step = step_row(con, a.step, a.plan)
    pl = one(con, "SELECT * FROM plans WHERE id=?", (step["plan_id"],))
    if a.review:
        rv = review_row(con, a.review)
        if not con.execute("SELECT 1 FROM plan_reviews WHERE plan_id=? AND review_id=?", (pl["id"], rv["id"])).fetchone():
            raise WriteError(f"{rv['code']} is not one of plan {pl['code']}'s reviews")
    else:
        rv = one(con, """SELECT r.* FROM plan_reviews pr JOIN reviews r ON r.id = pr.review_id WHERE pr.plan_id=?
                         ORDER BY r.started_at DESC, r.id DESC LIMIT 1""", (pl["id"],))
        if not rv:
            raise WriteError(f"plan {pl['code']} covers no review")
    if a.domain not in {r[0] for r in con.execute("SELECT code FROM domains WHERE retired_at IS NULL")}:
        raise WriteError(f"unknown domain {a.domain!r}")
    kind = a.kind or "defect"
    if kind not in FINDING_KINDS:
        raise WriteError(f"kind {kind!r} is not one of {', '.join(FINDING_KINDS)}")
    theme = a.theme
    if theme and not con.execute("SELECT 1 FROM themes WHERE code=?", (theme,)).fetchone():
        raise WriteError(f"unknown theme {theme!r}")
    repos = _review_repos(con, rv["id"])
    if not repos:
        raise WriteError(f"{rv['code']} has no repositories")
    default_repo = next(iter(repos)) if len(repos) == 1 else None
    locs = []
    for spec in [s for w in a.where for s in w.split(";") if s.strip()]:
        try:
            rname, p, la, lb, sym = parse_location(spec, set(repos), default_repo)
        except ValueError as e:
            raise WriteError(str(e))
        repo = repos[rname]
        d = repo_dir(ctx.root, repo["path"], bool(repo["allow_outside_root"]))
        commit = full_commit(d, a.commit) if a.commit else repo["commit_sha"]
        loc = cited_location(ctx.root, repo, commit, p, la, lb, sym)
        loc["repository_id"] = repo["id"]
        loc["permalink"] = None
        if repo["permalink_template"]:
            link = permalink_for(repo["permalink_template"], commit, p, None if loc["whole_file"] else loc["line_start"],
                                 None if loc["whole_file"] else loc["line_end"])
            loc["permalink"] = link if link and SAFE_URL.match(link) else None
        locs.append(loc)
    sub = a.sub or re.sub(r"[^a-z0-9]+", "-", a.summary.lower()).strip("-")[:48]
    fp = fingerprint(a.domain, theme, sub)
    candidates = discovery_candidates(con, fp, locs)
    if not a.issue and not a.new_issue and candidates:
        raise WriteError("it may be a known issue; look and give --issue CODE or --new-issue. Candidates: "
                         + "; ".join(f"{c} [{s}] {t[:70]} ({why})" for c, s, t, why in candidates[:8]))
    n = con.execute("""SELECT max(CAST(substr(point_id, 5) AS INTEGER)) FROM findings
                       WHERE review_id=? AND point_id GLOB 'FIX-[0-9]*'""", (rv["id"],)).fetchone()[0] or 0
    point = f"FIX-{n + 1}"
    fid = ctx.x("""INSERT INTO findings(review_id, point_id, severity, kind, domain_code, theme_code, sub, summary,
                   before_state, recommendation, recommended_test, fingerprint, verification, verified_by_actor_id,
                   verified_at, verification_note, discovered_in_step_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'unverified', NULL, NULL, NULL, ?)""",
                (rv["id"], point, a.severity, kind, a.domain, theme, sub, a.summary.strip(), a.before.strip(),
                 a.fix.strip(), (a.test or "").strip() or None, fp, step["id"])).lastrowid
    for i, loc in enumerate(locs):
        cols = {k: loc[k] for k in ("repository_id", "commit_sha", "path", "line_start", "line_end", "symbol",
                                    "blob_sha", "excerpt", "excerpt_sha256", "permalink", "excerpt_redacted",
                                    "whole_file", "excerpt_status")}
        cols.update(finding_id=fid, role="primary" if i == 0 else "also")
        ctx.x(f"INSERT INTO finding_locations({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
              tuple(cols.values()))
    out = [f"{rv['code']} {point}: recorded (found in step {step['number']}, {len(locs)} location"
           f"{'s' if len(locs) != 1 else ''} at {locs[0]['commit_sha'][:10]})"]
    if a.issue:
        iss = issue_row(con, a.issue)
        fixed = con.execute("SELECT fixed FROM issue_statuses WHERE value=?", (iss["status"],)).fetchone()[0]
        mtype = "new" if iss["first_seen_review_id"] == rv["id"] else ("regression" if fixed else "still-present")
        out.append(match_finding(ctx, rv, point, iss["code"], mtype, "likely",
                                 f"reported during step {step['number']}"))
        icode = iss["code"]
    else:
        out.append(match_finding(ctx, rv, point, "new", "new", "certain", f"found during step {step['number']}"))
        icode = con.execute("SELECT i.code FROM findings f JOIN issues i ON i.id = f.issue_id WHERE f.id=?",
                            (fid,)).fetchone()[0]
    for c, s, t, why in candidates[:8]:
        out.append(f"  candidate {c} [{s}] {t[:80]} ({why})")
    if len(candidates) > 8:
        out.append(f"  ... {len(candidates) - 8} more candidates")
    if a.into_step:
        target = step_row(con, a.into_step, pl["code"])
        if target["status"] in ("done", "dropped"):
            raise WriteError(f"step {target['number']} is {target['status']}; plan it into an open step, a new step "
                             f"or --gate")
        ctx.x("INSERT INTO step_findings(step_id, finding_id) VALUES (?,?)", (target["id"], fid))
        out.append(f"{point} planned in step {target['number']} ({target['title']})")
    elif a.new_step:
        ph = one(con, "SELECT * FROM phases WHERE plan_id=? AND number=?", (pl["id"], a.phase)) if a.phase else \
            one(con, "SELECT * FROM phases WHERE id=?", (step["phase_id"],))
        if not ph:
            raise WriteError(f"plan {pl['code']} has no phase {a.phase}")
        num = con.execute("SELECT max(number) FROM steps WHERE plan_id=?", (pl["id"],)).fetchone()[0] + 1
        sid = ctx.x("""INSERT INTO steps(plan_id, phase_id, number, section, title, change, proof, drafter_key,
                       change_actor_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (pl["id"], ph["id"], num, "Fix-time discoveries", a.new_step.strip(), (a.change or a.fix).strip(),
                     (a.proof or a.test or "a test that fails before the fix").strip(), f"{rv['code']}:{point}",
                     ctx.actor_id)).lastrowid
        ctx.x("INSERT INTO step_findings(step_id, finding_id) VALUES (?,?)", (sid, fid))
        out.append(f"{point} planned in new step {num} (phase {ph['number']}: {a.new_step.strip()})")
    else:
        gcode = next_code(con, "GATE")
        ctx.x("""INSERT INTO gates(code, kind, title, question, context, options, allow_text, blocking, review_id,
                 plan_id, issue_id, opened_by_actor_id) VALUES (?, 'decision', ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)""",
              (gcode, f"Plan the discovery {point} ({icode})",
               f"Where should {point} be fixed: in an existing step, a new step, or not at all?",
               f"{a.summary.strip()}\nNow: {a.before.strip()}\nFix: {a.fix.strip()}",
               json.dumps([{"value": "existing-step", "label": "In an existing step (say which)"},
                           {"value": "new-step", "label": "In a new step"},
                           {"value": "no-fix", "label": "Do not fix it (say why)"}]),
               rv["id"], pl["id"], issue_row(con, icode)["id"], ctx.actor_id))
        out.append(f"{point} is not planned yet: asked the owner in {gcode} (v_unplanned_discoveries lists it "
                   f"until a step closes it)")
    return out


def add_write_commands(sub):
    """The write commands' subparsers (each one's --help gives its exact usage line)."""
    def p(name, *args, **kw):
        f = WRITE_COMMANDS.get(name) or READ_COMMANDS[name]
        s = sub.add_parser(name, help=f.cli[0], description=f.cli[0], usage=f.cli[1])
        if name not in READ_COMMANDS:
            s.add_argument("--actor", help="who makes this change (A-0001, an actor id, or a unique actor name)")
        return s

    s = p("next-code"); s.add_argument("prefix", help="ISS, R, P, D, GATE, REQ or G")
    s = p("add-repo"); s.add_argument("name"); s.add_argument("path", help="relative to the project root ('.')")
    s.add_argument("--remote"); s.add_argument("--default-branch"); s.add_argument("--permalink-template")
    s.add_argument("--outside-root", action="store_true", help="the owner listed it as living outside the root")
    s = p("add-actor"); s.add_argument("kind", help=", ".join(ACTOR_KINDS)); s.add_argument("name")
    for o in ("--model", "--harness", "--session", "--parent", "--review", "--role"):
        s.add_argument(o)
    s = p("new-review"); s.add_argument("--title"); s.add_argument("--scope", default="full")
    s.add_argument("--repo", action="append", help="NAME or NAME@COMMIT (default: every repository at HEAD)")
    s.add_argument("--domain", action="append", help="CODE or CODE=reason (proposed, included)")
    s.add_argument("--skip", action="append", help="CODE=reason (skipped)")
    s.add_argument("--goal", action="append"); s.add_argument("--request"); s.add_argument("--previous")
    s.add_argument("--manager")
    s = p("review-status"); s.add_argument("review"); s.add_argument("status")
    s.add_argument("--note"); s.add_argument("--approved-by")
    s = p("snapshot"); s.add_argument("review"); s.add_argument("--repo", action="append")
    s = p("add-run"); s.add_argument("review"); s.add_argument("domain"); s.add_argument("label")
    s.add_argument("--scope"); s.add_argument("--reviewer"); s.add_argument("--model"); s.add_argument("--prompt")
    s.add_argument("--status", default="queued")
    s = p("issue-status"); s.add_argument("issue"); s.add_argument("status")
    for o in ("--note", "--review", "--step", "--plan", "--commit", "--after", "--resolution"):
        s.add_argument(o)
    s = p("step-status"); s.add_argument("step"); s.add_argument("status")
    for o in ("--note", "--question", "--progress-note", "--commit", "--plan"):
        s.add_argument(o)
    s = p("import-steps"); s.add_argument("plan"); s.add_argument("file", help="JSON lines, or - for standard input")
    s = p("open-gate"); s.add_argument("kind")
    for o in ("--title", "--question", "--context", "--options-json", "--review", "--plan", "--step", "--issue",
              "--decision"):
        s.add_argument(o)
    s.add_argument("--option", action="append", help="value=label or value=label:description")
    s.add_argument("--no-text", action="store_true", help="the owner must choose an option")
    s.add_argument("--non-blocking", action="store_true")
    s = p("answer-gate"); s.add_argument("gate"); s.add_argument("--value"); s.add_argument("--text")
    s.add_argument("--via", default="chat", choices=("chat", "other")); s.add_argument("--by")
    s = p("apply-gate"); s.add_argument("gate"); s.add_argument("--note"); s.add_argument("--revisit-when")
    s = p("import-findings"); s.add_argument("review"); s.add_argument("run"); s.add_argument("file")
    s = p("import-checks"); s.add_argument("review"); s.add_argument("file"); s.add_argument("--run")
    s = p("load-index"); s.add_argument("review"); s.add_argument("run"); s.add_argument("points")
    s.add_argument("known", nargs="?"); s.add_argument("--trace")
    s = p("match"); s.add_argument("review"); s.add_argument("point", nargs="?"); s.add_argument("target", nargs="?")
    s.add_argument("--type"); s.add_argument("--confidence", default="likely"); s.add_argument("--rationale")
    s.add_argument("--relations"); s.add_argument("--file")
    s = p("report-finding")
    for o in ("--step", "--severity", "--domain", "--summary", "--before", "--fix", "--test", "--kind", "--theme",
              "--sub", "--commit", "--review", "--plan", "--issue", "--into-step", "--new-step", "--change",
              "--proof"):
        s.add_argument(o)
    s.add_argument("--where", action="append", default=[], help="[repo:]path[:start[-end]][#symbol], at --commit")
    s.add_argument("--phase", type=int); s.add_argument("--new-issue", action="store_true")
    s.add_argument("--gate", action="store_true", help="ask the owner where to plan it (the default)")


# ----- the reviewed text of a dirty working tree (D-14, used by fill_refs)

def working_tree_bytes(root, con, loc, committed):
    """The file's bytes in the working tree when the location's review recorded that repository dirty (is_dirty) at
    the location's commit and the file there differs from the commit; else None (read the commit). The reviewers
    saw the working tree, so its text is what the excerpt should hold (finding_locations.source 'working-tree')."""
    try:
        rr = con.execute("""SELECT rr.is_dirty, rr.commit_sha FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
                            JOIN review_repositories rr ON rr.review_id = f.review_id
                             AND rr.repository_id = fl.repository_id WHERE fl.id=?""", (loc["id"],)).fetchone()
    except sqlite3.Error:
        return None
    if not rr or not rr[0] or rr[1] != loc["commit_sha"]:
        return None
    try:
        d = repo_dir(root, loc["repo_path"], bool(loc.get("allow_outside_root")))
        f = (d / loc["path"]).resolve()
        if d.resolve() not in f.parents or not f.is_file():
            return None
        data = f.read_bytes()
    except (OSError, RuntimeError):
        return None
    return data if data != committed else None


# ---------------------------------------------------------------- CLI

_MAIN_CONNECTIONS = []  # the ledger connection main() opened, closed on every way out (FIX-4)


def main():
    """The CLI. Whatever way a command ends (return, sys.exit, an exception), the ledger connection it opened is
    closed before main() returns or raises."""
    try:
        return _main()
    finally:
        while _MAIN_CONNECTIONS:
            try:
                _MAIN_CONNECTIONS.pop().close()
            except sqlite3.Error:
                pass


def _main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None)
    ap.add_argument("--db")
    ap.add_argument("--version", action="version", version=f"ledger.py {TOOL_VERSION} (schema {SCHEMA_VERSION})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--force", action="store_true",
                   help="replace the project's .code-review/ledger.py even when it is newer than this copy")
    sub.add_parser("migrate", help="upgrade an older ledger to this schema (keeps a .bak-v<N> copy)")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765); s.add_argument("--no-open", action="store_true")
    s = sub.add_parser("check")
    s.add_argument("--full", action="store_true",
                   help="run the full PRAGMA integrity_check instead of quick_check")
    s.add_argument("--quiet", action="store_true", help="print only failures and warnings (no 'ok' or zero lines)")
    s = sub.add_parser("fill-refs"); s.add_argument("review", nargs="?")
    s = sub.add_parser("tag-review"); s.add_argument("review")
    s = sub.add_parser("set-report", help="store the review document (Markdown) shown in the viewer")
    s.add_argument("review"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("set-narrative", help="store the plan's narrative (Markdown) shown in the viewer")
    s.add_argument("plan"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("show"); s.add_argument("location", type=int)
    sub.add_parser("gates", help="the owner's queue: open and answered gates, waiting steps, open decisions, "
                                 "stale steps ('no open gates' when there are none)")
    add_tool_parsers(sub)
    add_write_commands(sub)
    sub.add_parser("dump")
    sub.add_parser("schema")
    a = ap.parse_args()
    if a.cmd == "schema":
        print(SCHEMA); return
    explicit = a.root is not None  # any --root given, '.' included, is used as given
    root, how = resolve_root(Path(a.root if explicit else ".").resolve(), explicit, script=__file__)
    # The ledger: --db FILE, else $CODE_REVIEW_DIR/ledger.sqlite (a ledger kept outside the repository), else
    # <root>/.code-review/ledger.sqlite.
    env_dir = os.environ.get("CODE_REVIEW_DIR", "").strip()
    if a.db:
        db, where = Path(a.db).resolve(), "db"
    elif env_dir:
        db, where = Path(env_dir).expanduser().resolve() / "ledger.sqlite", "env"
    else:
        db, where = root / ".code-review" / "ledger.sqlite", "default"
    if a.cmd == "init":
        print(f"project root: {root}")
        if how == "start" and where == "default":
            sys.exit(f"init: {root} is not in a git repository and no folder above it has a ledger; pass --root DIR "
                     f"to say which folder is the project root (init then creates DIR/.code-review/ledger.sqlite)")
        in_ledger_folder = db.parent.name == ".code-review"
        inside_root = db.parent == root or root in db.parent.parents
        init(root, db, force=a.force, copy_tool=in_ledger_folder or where == "env",
             gitignore=in_ledger_folder and inside_root)
        return
    if a.cmd == "migrate":
        migrate(db); return
    if a.cmd in TOOL_COMMANDS:
        sys.exit(tool_command(a, root, db))
    # Writes refuse an older ledger; serve, gates and the writes also refuse a schema that differs from the shipped
    # one (a forged trigger could answer gates). show and dump still read it, for diagnosis; check reports it.
    writes = a.cmd in WRITE_COMMANDS or a.cmd in READ_COMMANDS  # the write commands (and next-code)
    con = open_ledger(db, write=writes or a.cmd in ("serve", "fill-refs", "tag-review", "set-report", "set-narrative"),
                      verify=writes or a.cmd in ("serve", "gates", "fill-refs", "tag-review", "set-report",
                                                 "set-narrative"))
    _MAIN_CONNECTIONS.append(con)
    if writes:
        sys.exit(run_write_command(con, root, db, a))
    if a.cmd == "serve":
        con.close(); serve(root, db, a.port, not a.no_open)
    elif a.cmd == "check":
        was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
        if was and was[0] != str(db.parent):
            print(f"warning: this ledger was set up at {was[0]} and is now used from {db.parent} (fine after a move; "
                  f"init records the new place)", file=sys.stderr)
        bad = check(con, quiet=a.quiet, full=a.full)
        bad += check_git_refs(root, con, quiet=a.quiet)
        sys.exit(1 if bad else 0)
    elif a.cmd == "fill-refs":
        sys.exit(1 if fill_refs(root, con, a.review) else 0)
    elif a.cmd == "tag-review":
        sys.exit(tag_review(root, con, a.review))
    elif a.cmd in ("set-report", "set-narrative"):
        sys.exit(set_text(con, a.cmd, a.review if a.cmd == "set-report" else a.plan, a.file))
    elif a.cmd == "show":
        s = source(root, con, a.location, context=0)
        if s.get("error"):
            sys.exit(s["error"])
        print(f"# {s['repo_name']} {s['path']} @ {s['commit_sha']} (from {s['source']})")
        if s.get("git_error"):
            print(f"# git: {s['git_error']}")
        for l in s.get("lines", []):
            print(f"{l['n']:6d}  {l['text']}")
        if s.get("source") == "none":
            sys.exit(f"show: nothing to show for reference {a.location} (git could not read it and it has no "
                     f"excerpt)")
    elif a.cmd == "gates":
        print_gates(con)
    elif a.cmd == "dump":
        dump(con, db)


if __name__ == "__main__":
    main()
