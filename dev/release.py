"""Build the skill bundle: dist/codebase-system-review-<version>.zip, the folder a harness's skills directory takes.

    python3 dev/release.py               rebuild scripts/ledger.py, check SKILL.md's frontmatter, write the zip
    python3 dev/release.py --out DIR     write the zip into DIR (default: dist/)

The zip holds one folder named as SKILL.md's `name:`, with exactly SKILL.md, README.md, LICENSE, scripts/ledger.py and
every file under references/. Entries are sorted and dated 1980-01-01, so the same sources give the same bytes.

Frontmatter rules: `name:` is codebase-system-review (1-64 lowercase letters, digits and single hyphens), the
folder name in the zip and in every skills folder;
`description:` is 1-1024 characters on one line; `version:` is X.Y.Z, indented under `metadata:` (dev/build.py reads
it). The top-level keys are only those every skill loader accepts (KEYS); some loaders refuse any other, so a
top-level `version:` is refused too. A broken rule exits 2 and writes nothing.
"""
import argparse
import importlib.util
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_NAME = "codebase-system-review"  # the folder the skill is installed as; README and SKILL.md use it
FILES = ("SKILL.md", "README.md", "LICENSE", "scripts/ledger.py")
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_MAX = 1024  # characters; the skill format's limit
NAME_MAX = 64
KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")  # portable top-level keys
EPOCH = (1980, 1, 1, 0, 0, 0)  # the earliest date a zip entry can carry


class ReleaseError(Exception):
    """The sources do not make a valid bundle."""


def _build_module(root):
    spec = importlib.util.spec_from_file_location("build", str(Path(root) / "dev" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frontmatter(root):
    """{key: value} of SKILL.md's frontmatter, checked against the rules above (version through dev/build.py)."""
    text = (Path(root) / "SKILL.md").read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if not text.startswith("---\n") or len(parts) < 3:
        raise ReleaseError("SKILL.md must start with a frontmatter block between two '---' lines")
    keys = dict(re.findall(r"(?m)^([^\s:#][^:]*):[ \t]*(.*?)[ \t]*$", parts[1]))
    other = [k for k in keys if k not in KEYS]
    if other:
        raise ReleaseError(f"SKILL.md frontmatter key {other[0]!r} is not one of {', '.join(KEYS)}"
                           + (" (version goes under metadata:)" if other[0] == "version" else ""))
    name = keys.get("name", "")
    if not NAME.match(name) or len(name) > NAME_MAX:
        raise ReleaseError(f"SKILL.md name {name!r} must be 1-{NAME_MAX} lowercase letters, digits and single hyphens")
    if name != SKILL_NAME:
        raise ReleaseError(f"SKILL.md name {name!r} must be {SKILL_NAME!r}, the folder the skill is installed as")
    desc = keys.get("description", "")
    if not desc or len(desc) > DESCRIPTION_MAX:
        raise ReleaseError(f"SKILL.md description must be 1-{DESCRIPTION_MAX} characters on one line "
                           f"(it is {len(desc)})")
    return keys


def entries(root):
    """[(path inside the folder, source file)] in sorted order."""
    root = Path(root)
    out = [(rel, root / rel) for rel in FILES]
    out += [(f.relative_to(root).as_posix(), f) for f in (root / "references").rglob("*")
            if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith(".pyc")]
    missing = [rel for rel, f in out if not f.is_file()]
    if missing:
        raise ReleaseError("missing: " + ", ".join(missing))
    return sorted(out)


def bundle(root, out_dir=None):
    """Rebuild scripts/ledger.py, check the frontmatter, write the zip; return its path."""
    root = Path(root)
    build = _build_module(root)
    keys = frontmatter(root)
    try:
        version = build.skill_version(root)
        text = build.build(root)
    except build.BuildError as e:
        raise ReleaseError(str(e)) from None
    build.write(root, text)
    out_dir = Path(out_dir) if out_dir else root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{keys['name']}-{version}.zip"
    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, src in entries(root):
            info = zipfile.ZipInfo(f"{keys['name']}/{rel}", EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if rel == "scripts/ledger.py" else 0o644) << 16
            z.writestr(info, src.read_bytes())
    tmp.replace(target)
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(prog="release.py", description="Build the skill bundle zip (rebuilds "
                                 "scripts/ledger.py and checks SKILL.md's frontmatter first).")
    ap.add_argument("--root", default=str(HERE.parent), help="the skill repository (default: this one)")
    ap.add_argument("--out", help="folder for the zip (default: dist/ in the repository)")
    a = ap.parse_args(argv)
    try:
        path = bundle(a.root, a.out)
    except ReleaseError as e:
        print(f"release.py: error: {e}", file=sys.stderr)
        return 2
    print(path.relative_to(Path(a.root).resolve()) if path.resolve().is_relative_to(Path(a.root).resolve()) else path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
