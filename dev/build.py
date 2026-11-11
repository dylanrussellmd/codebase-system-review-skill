"""Build scripts/ledger.py from dev/core.py, dev/page.html, references/schema.sql, references/migrations/ and the
version in SKILL.md's frontmatter.

    python3 dev/build.py           write scripts/ledger.py (through a temporary file, then a rename)
    python3 dev/build.py --check   exit 1, with a short diff, if scripts/ledger.py differs from a fresh build

Each payload placeholder below must occur exactly once in core.py, inside a raw triple-quoted string
(r\"\"\"@@X@@\"\"\"), and in none of the payloads. A payload may not contain three double quotes in a row and may not end
with a backslash or a double quote, since either would end or break the string it is pasted into. The built text must
parse and hold each payload verbatim; a violation stops the build and nothing is written.

The migrations payload is every references/migrations/NNNN-<slug>.sql in order, each preceded by a line
`-- migration: NNNN-<slug>.sql`. Files must be numbered 0001, 0002, ... without gaps, and each must start with a line
`-- <description>`. The highest number is the schema version (core.py derives SCHEMA_VERSION from it), and the build
refuses unless references/schema.sql agrees: its header line says that version, its meta seed sets schema_version to
it, and its schema_migrations seed holds exactly one (version, description) row per migration file.

The stamps: TOOL_VERSION ("@@TOOL_VERSION@@" in core.py) becomes `version:` from SKILL.md's frontmatter (semver), and
TOOL_HASH ("@@TOOL_HASH@@") becomes the sha256 of the built text in which TOOL_HASH's value is still the placeholder.
"""
import argparse
import ast
import difflib
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = "scripts/ledger.py"
CORE = "dev/core.py"
SKILL = "SKILL.md"
MIGRATIONS_DIR = "references/migrations"
PAYLOADS = (("@@SCHEMA@@", "references/schema.sql"), ("@@PAGE@@", "dev/page.html"),
            ("@@MIGRATIONS@@", MIGRATIONS_DIR))
STAMPS = ("@@TOOL_VERSION@@", "@@TOOL_HASH@@")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
MIGRATION_NAME = re.compile(r"^(\d{4})-([a-z0-9][a-z0-9-]*)\.sql$")
SEPARATOR = "-- migration: "


class BuildError(Exception):
    """The sources cannot be embedded safely."""


def _read(root, rel):
    return (root / rel).read_text(encoding="utf-8")


def skill_version(root):
    """`version:` from SKILL.md's frontmatter (the one source of the tool version)."""
    text = _read(root, SKILL)
    parts = text.split("---\n", 2)
    m = re.search(r"(?m)^version:[ \t]*(\S*)[ \t]*$", parts[1]) if text.startswith("---\n") and len(parts) == 3 else None
    if not m:
        raise BuildError(f"{SKILL} needs a 'version: X.Y.Z' line in its frontmatter")
    if not SEMVER.match(m.group(1)):
        raise BuildError(f"{SKILL} frontmatter version {m.group(1)!r} is not a semantic version (X.Y.Z)")
    return m.group(1)


def migrations(root):
    """[(number, file name, text)] of references/migrations, checked: names, numbering, descriptions."""
    folder = root / MIGRATIONS_DIR
    if not folder.is_dir():
        raise BuildError(f"{MIGRATIONS_DIR}/ is missing")
    out = []
    for f in sorted(folder.iterdir(), key=lambda f: f.name):
        m = MIGRATION_NAME.match(f.name)
        if not m:
            raise BuildError(f"{MIGRATIONS_DIR}/{f.name}: the name must be NNNN-<lowercase-slug>.sql")
        text = f.read_text(encoding="utf-8")
        first = text.split("\n", 1)[0]
        if not first.startswith("-- ") or not first[3:].strip():
            raise BuildError(f"{MIGRATIONS_DIR}/{f.name}: the first line must be '-- <description>'")
        if any(line.startswith(SEPARATOR) for line in text.split("\n")):
            raise BuildError(f"{MIGRATIONS_DIR}/{f.name}: no line may start with '{SEPARATOR}'")
        if not text.endswith("\n"):
            text += "\n"
        out.append((int(m.group(1)), f.name, text))
    if [n for n, _, _ in out] != list(range(1, len(out) + 1)):
        raise BuildError(f"{MIGRATIONS_DIR}/ must hold files numbered 0001, 0002, ... without gaps "
                         f"(found {', '.join(name for _, name, _ in out) or 'none'})")
    return out


def check_schema_versions(schema, migs):
    """The schema version is written in several places; all must equal the highest migration."""
    version = migs[-1][0]
    head = re.search(r"(?m)^-- Code review ledger .*schema version (\d+)$", schema)
    if not head or int(head.group(1)) != version:
        raise BuildError(f"references/schema.sql header must say schema version {version} (the highest migration), "
                         f"found {head.group(1) if head else 'no version'}")
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema)
        meta = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        seeded = con.execute("SELECT version, description FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.Error as e:
        raise BuildError(f"references/schema.sql does not run: {e}") from None
    finally:
        con.close()
    if not meta or meta[0] != str(version):
        raise BuildError(f"references/schema.sql meta seed must set schema_version to '{version}' (the highest "
                         f"migration), found {meta[0] if meta else 'none'!r}")
    want = [(n, text.split("\n", 1)[0][3:].strip()) for n, _, text in migs]
    if seeded != want:
        raise BuildError(f"references/schema.sql schema_migrations seed must be one row per migration file, "
                         f"{want}, found {seeded}")


def build(root=None):
    """Return the text of scripts/ledger.py built from the sources under `root` (default: this repository)."""
    root = Path(root) if root else ROOT
    core = _read(root, CORE)
    version = skill_version(root)
    migs = migrations(root)
    schema = _read(root, "references/schema.sql")
    check_schema_versions(schema, migs)
    bundle = "".join(f"{SEPARATOR}{name}\n{text}" for _, name, text in migs)
    payloads = [(ph, rel, bundle if rel == MIGRATIONS_DIR else _read(root, rel)) for ph, rel in PAYLOADS]
    for ph in [ph for ph, _ in PAYLOADS] + list(STAMPS):
        n = core.count(ph)
        if n != 1:
            raise BuildError(f"{CORE} must contain {ph} exactly once (found {n})")
    for _, rel, text in payloads:
        for ph in [ph for ph, _ in PAYLOADS] + list(STAMPS):
            if ph in text:
                raise BuildError(f"{rel} must not contain the placeholder {ph}")
        if '"""' in text:
            raise BuildError(f"{rel} must not contain triple quotes")
        if text.endswith(("\\", '"')):
            raise BuildError(f"{rel} must not end with a backslash or a double quote")
    out = core.replace("@@TOOL_VERSION@@", version, 1)
    for ph, _, text in payloads:
        out = out.replace(ph, text, 1)
    out = out.replace("@@TOOL_HASH@@", hashlib.sha256(out.encode("utf-8")).hexdigest(), 1)
    try:
        tree = ast.parse(out, OUTPUT)
    except SyntaxError as e:
        raise BuildError(f"the built {OUTPUT} does not parse: {e}") from None
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for _, rel, text in payloads:
        if text not in strings:
            raise BuildError(f"{rel} is not embedded verbatim (is its placeholder inside a raw triple-quoted string?)")
    return out


def write(root, text):
    """Write the output through a temporary file in the same folder and an atomic rename."""
    target = root / OUTPUT
    fd, tmp = tempfile.mkstemp(prefix=".ledger.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, str(target))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def check(root, text, limit=40):
    """0 if the shipped file equals `text`, else print a short diff and return 1."""
    target = root / OUTPUT
    current = target.read_bytes() if target.exists() else None
    if current == text.encode("utf-8"):
        print(f"{OUTPUT} is up to date")
        return 0
    if current is None:
        print(f"{OUTPUT} is missing; run: python3 dev/build.py")
        return 1
    old = current.decode("utf-8", "replace").splitlines()
    new = text.splitlines()
    diff = list(difflib.unified_diff(old, new, f"{OUTPUT} (shipped)", f"{OUTPUT} (fresh build)", n=1, lineterm=""))
    changed = sum(1 for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---")))
    print(f"{OUTPUT} is stale: {changed} changed lines. Run python3 dev/build.py and commit {OUTPUT}.")
    for line in diff[:limit]:
        print("    " + line[:160])
    if len(diff) > limit:
        print(f"    ... {len(diff) - limit} more diff lines")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="build.py", description="Build scripts/ledger.py from dev/core.py, "
                                 "dev/page.html, references/schema.sql, references/migrations/ and SKILL.md's version.")
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 with a short diff if scripts/ledger.py differs from a fresh build")
    a = ap.parse_args(argv)
    try:
        text = build(ROOT)
    except BuildError as e:
        print(f"build.py: error: {e}", file=sys.stderr)
        return 2
    if a.check:
        return check(ROOT, text)
    write(ROOT, text)
    print(OUTPUT, len(text.splitlines()), "lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
