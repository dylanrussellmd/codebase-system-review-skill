"""Write the versions references/tools.lock pins as manifests OSV-Scanner reads, so CI's tools job can look up
advisories for the tools themselves (their top-level packages, not what each one bundles).

    python3 dev/pin_manifests.py FOLDER    writes package-lock.json (npm), requirements.txt (PyPI), go.mod (Go)
                                           and Cargo.lock (crates.io) into FOLDER

Every pin needs a place in PACKAGES or NO_PACKAGE; a pin in neither stops the script.
"""
import json
import re
import sys
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent / "references" / "tools.lock"

# The package of each pin whose key names no npm or PyPI package: (ecosystem, package name).
PACKAGES = {
    "actionlint": ("go", "github.com/rhysd/actionlint"),
    "ast-grep": ("cargo", "ast-grep"),
    "gitleaks": ("go", "github.com/zricethezav/gitleaks/v8"),
    "hurl": ("cargo", "hurl"),
    "hyperfine": ("cargo", "hyperfine"),
    "lychee": ("cargo", "lychee"),
    "mailpit": ("go", "github.com/axllent/mailpit"),
    "github:oasdiff/oasdiff": ("go", "github.com/oasdiff/oasdiff"),
    "osv-scanner": ("go", "github.com/google/osv-scanner/v2"),
    "aqua:boyter/scc": ("go", "github.com/boyter/scc/v3"),
}
NO_PACKAGE = {"postgres"}   # a server with no package in an OSV ecosystem


def pins(text):
    """[(key as written, version)] of the [tools] table."""
    out, table = [], None
    for line in text.split("\n"):
        line = line.split("#", 1)[0].strip()
        if line.startswith("["):
            table = line.strip("[] ")
            continue
        m = re.match(r'^"?([^"=\s]+)"?\s*=\s*"([^"]+)"$', line)
        if table == "tools" and m:
            out.append((m.group(1), m.group(2)))
    return out


def manifests(text):
    """{file name: content} for the pins in `text`."""
    npm, pypi, go, cargo, unknown = {}, [], [], [], []
    for key, version in pins(text):
        backend, _, name = key.partition(":") if key.startswith(("npm:", "pipx:")) else ("", "", key)
        if backend == "npm":
            npm[f"node_modules/{name}"] = {"version": version}
        elif backend == "pipx":
            pypi.append(f"{name}=={version}")
        elif key in PACKAGES:
            eco, pkg = PACKAGES[key]
            (go if eco == "go" else cargo).append((pkg, version))
        elif key not in NO_PACKAGE:
            unknown.append(key)
    if unknown:
        raise SystemExit(f"pin_manifests: no package known for {', '.join(unknown)} (add it to PACKAGES)")
    lock = {"name": "pins", "lockfileVersion": 3, "requires": True, "packages": {"": {"name": "pins"}, **npm}}
    return {
        "package-lock.json": json.dumps(lock, indent=2, sort_keys=True) + "\n",
        "requirements.txt": "".join(f"{p}\n" for p in sorted(pypi)),
        # No go directive: it would add the Go standard library of that version to the scan.
        "go.mod": "module pins\n\nrequire (\n" + "".join(f"\t{p} v{v}\n" for p, v in sorted(go)) + ")\n",
        "Cargo.lock": "version = 3\n" + "".join(
            f'\n[[package]]\nname = "{p}"\nversion = "{v}"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n' for p, v in sorted(cargo)),
    }


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__.strip().split("\n\n")[1])
    folder = Path(argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in manifests(LOCK.read_text(encoding="utf-8")).items():
        (folder / name).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
