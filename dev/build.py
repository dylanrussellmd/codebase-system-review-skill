"""Build scripts/ledger.py from dev/core.py, dev/page.html and references/schema.sql.

    python3 dev/build.py           write scripts/ledger.py (through a temporary file, then a rename)
    python3 dev/build.py --check   exit 1, with a short diff, if scripts/ledger.py differs from a fresh build

Each placeholder below must occur exactly once in core.py, inside a raw triple-quoted string (r\"\"\"@@X@@\"\"\"), and
in none of the payloads. A payload may not contain three double quotes in a row and may not end with a backslash or
a double quote, since either would end or break the string it is pasted into. The built text must parse and hold
each payload verbatim; a violation stops the build and nothing is written.
"""
import argparse
import ast
import difflib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = "scripts/ledger.py"
CORE = "dev/core.py"
PAYLOADS = (("@@SCHEMA@@", "references/schema.sql"), ("@@PAGE@@", "dev/page.html"))


class BuildError(Exception):
    """The sources cannot be embedded safely."""


def _read(root, rel):
    return (root / rel).read_text(encoding="utf-8")


def build(root=None):
    """Return the text of scripts/ledger.py built from the sources under `root` (default: this repository)."""
    root = Path(root) if root else ROOT
    core = _read(root, CORE)
    payloads = [(ph, rel, _read(root, rel)) for ph, rel in PAYLOADS]
    for ph, _, _ in payloads:
        n = core.count(ph)
        if n != 1:
            raise BuildError(f"{CORE} must contain {ph} exactly once (found {n})")
    for _, rel, text in payloads:
        for ph, _ in PAYLOADS:
            if ph in text:
                raise BuildError(f"{rel} must not contain the placeholder {ph}")
        if '"""' in text:
            raise BuildError(f"{rel} must not contain triple quotes")
        if text.endswith(("\\", '"')):
            raise BuildError(f"{rel} must not end with a backslash or a double quote")
    out = core
    for ph, _, text in payloads:
        out = out.replace(ph, text, 1)
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
                                 "dev/page.html and references/schema.sql.")
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
