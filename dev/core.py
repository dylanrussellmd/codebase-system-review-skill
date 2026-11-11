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
    try:
        con = connect(db)
        v = ledger_version(con, db)
    except (LedgerError, sqlite3.DatabaseError) as e:
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


def check(con, quiet=False):
    """Print what check finds; return the number of failed checks (integrity, foreign keys, schema, each non-empty
    error-tier view). A non-empty warning-tier view is printed with 'warning' and does not count."""
    bad = 0
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        print("integrity_check:", ic); bad += 1
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


def file_at(root, repo_path, sha, path, allow_outside=False, raw=False):
    """The file's text at commit sha (raw=True: its bytes). A refused path gives the fixed error 'bad path' before
    git runs, so no message says whether some file outside the repository exists."""
    if not HEX.match(sha or ""):
        raise RuntimeError("commit is not a hex hash")
    if bad_location_path(path or ""):
        raise RuntimeError("bad path")
    data = git(repo_dir(root, repo_path, allow_outside), "show", f"{sha}:{path}")
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
    Never overwrites an excerpt. A failure (file unreadable, lines past the end, a symbol not found or ambiguous, an
    empty file) writes nothing for that reference and is counted; returns the count."""
    sql = """SELECT fl.id FROM finding_locations fl JOIN findings f ON f.id = fl.finding_id
             JOIN reviews r ON r.id = f.review_id
             WHERE fl.commit_sha IS NOT NULL
               AND ((fl.excerpt IS NULL AND fl.excerpt_status IS NOT 'not-text') OR fl.blob_sha IS NULL
                    OR fl.permalink IS NULL OR (fl.line_start IS NULL AND fl.whole_file = 0))"""
    args = ()
    if review_code:
        sql += " AND r.code = ?"; args = (review_code,)
    failed = 0
    updates = []  # every git call happens first; the writes then go in one short transaction (DB-M8)
    for (lid,) in con.execute(sql, args).fetchall():
        loc = location(con, lid)
        where = f"ref {lid} {loc['path']}@{loc['commit_sha'][:10]}"
        outside = bool(loc["allow_outside_root"])
        try:
            data = file_at(root, loc["repo_path"], loc["commit_sha"], loc["path"], outside, raw=True)
        except Exception as e:
            print(f"{where}: {e}"); failed += 1; continue
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
                print(f"{where}: not a text file; symbol {loc['symbol']!r} cannot be looked up"); failed += 1; continue
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
            try:
                upd["blob_sha"] = git(repo_dir(root, loc["repo_path"], outside), "rev-parse",
                                      f"{loc['commit_sha']}:{loc['path']}").decode().strip()
            except Exception:
                pass
        if loc["permalink"] is None and loc["permalink_template"]:
            link = permalink_for(loc["permalink_template"], loc["commit_sha"], loc["path"],
                                 None if whole else a, None if whole else b)
            if link and SAFE_URL.match(link):
                upd["permalink"] = link
            else:
                print(f"{where}: the permalink template does not give an http(s) link; no permalink written")
        if upd:
            updates.append((lid, upd))
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


def answer_gate(db, code, body):
    """The owner's answer to an open gate, from the viewer. Read and write happen in one BEGIN IMMEDIATE transaction
    on a writable connection of its own, so two answers cannot both be reported saved: the loser, or an answer to a
    gate answered meanwhile (in chat, say), gets 409. If another writer holds the ledger past ANSWER_BUSY_MS the
    answer is not saved and the owner is told to try again (503); the page keeps what they typed."""
    busy = {"error": "The ledger is busy; your answer was not saved, try again."}, 503
    try:
        w = connect(db)
    except sqlite3.OperationalError:
        return {"error": "ledger not found"}, 503
    w.isolation_level = None
    try:
        w.execute(f"PRAGMA busy_timeout = {int(ANSWER_BUSY_MS)}")
        try:
            w.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                return busy
            raise
        try:
            g = one(w, "SELECT * FROM gates WHERE code=?", (code,))
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
            # who answered, as an actor (schema 3): the project's owner, made on first use if the ledger has none
            owner = w.execute("SELECT min(id) FROM actors WHERE kind='owner'").fetchone()[0]
            if owner is None:
                owner = w.execute("INSERT INTO actors(kind, name) VALUES ('owner', 'owner')").lastrowid
            cur = w.execute("""UPDATE gates SET status='answered', answer_value=?, answer_text=?,
                               answered_by='owner', answered_via='viewer', answered_by_actor_id=?
                               WHERE id=? AND status='open'""",
                            (val, txt, owner, g["id"]))
            if cur.rowcount != 1:
                return {"error": "This question was already answered."}, 409
            w.execute("COMMIT")
            return {"ok": True}
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                return busy
            raise
        finally:
            if w.in_transaction:
                w.execute("ROLLBACK")
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
            "checks": [{"label": label, "view": v, "tier": tier, "n": count_view(con, v)}
                       for v, label, tier in (ledger_checks(con) or [])],
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
        return answer_gate(db, p[1], body)

    if p == ["reviews"]:
        return rows(con, """SELECT r.code, r.title, r.scope, r.status, r.started_at, r.completed_at, r.verdict,
            (SELECT group_concat(rp.name || ' ' || substr(rr.commit_sha,1,10) || COALESCE(' on ' || rr.branch, ''), ' · ')
               FROM review_repositories rr JOIN repositories rp ON rp.id=rr.repository_id WHERE rr.review_id=r.id) commits,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id) points,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='H') high,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='M') medium,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.severity='L') low,
            (SELECT count(*) FROM findings f WHERE f.review_id=r.id AND f.match_type IN ('new','continuation')) new_issues,
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


# ---------------------------------------------------------------- CLI

def main():
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
    sub.add_parser("check")
    s = sub.add_parser("fill-refs"); s.add_argument("review", nargs="?")
    s = sub.add_parser("tag-review"); s.add_argument("review")
    s = sub.add_parser("set-report", help="store the review document (Markdown) shown in the viewer")
    s.add_argument("review"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("set-narrative", help="store the plan's narrative (Markdown) shown in the viewer")
    s.add_argument("plan"); s.add_argument("file", help="Markdown file, or - for standard input")
    s = sub.add_parser("show"); s.add_argument("location", type=int)
    sub.add_parser("gates")
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
    # Writes refuse an older ledger; serve, gates and the writes also refuse a schema that differs from the shipped
    # one (a forged trigger could answer gates). show and dump still read it, for diagnosis; check reports it.
    con = open_ledger(db, write=a.cmd in ("serve", "fill-refs", "tag-review", "set-report", "set-narrative"),
                      verify=a.cmd in ("serve", "gates", "fill-refs", "tag-review", "set-report", "set-narrative"))
    if a.cmd == "serve":
        con.close(); serve(root, db, a.port, not a.no_open)
    elif a.cmd == "check":
        was = con.execute("SELECT value FROM meta WHERE key='ledger_location'").fetchone()
        if was and was[0] != str(db.parent):
            print(f"warning: this ledger was set up at {was[0]} and is now used from {db.parent} (fine after a move; "
                  f"init records the new place)", file=sys.stderr)
        bad = check(con)
        bad += check_git_refs(root, con)
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
        for g in rows(con, "SELECT * FROM gates WHERE status IN ('open','answered') ORDER BY created_at"):
            ans = f" -> {g['answer_value'] or ''} {g['answer_text'] or ''}".rstrip() if g["status"] == "answered" else ""
            print(f"{g['code']}  [{g['status']}] {g['kind']}: {g['title']}{ans}")
        try:
            stale = rows(con, "SELECT * FROM v_stale_steps ORDER BY plan, step")
        except sqlite3.Error:
            stale = []  # an older ledger (gates warned already): migrate adds the view
        for s in stale:
            print(f"stale step {s['plan']} #{s['step']} [{s['status']}] {s['title']}: its issues are all closed "
                  f"({s['issues']}); mark it done or dropped")
    elif a.cmd == "dump":
        dump(con, db)


if __name__ == "__main__":
    main()
