"""Create a demo project with a filled review ledger, to try the viewer or check the ledger's rules.

    python3 dev/tests/seed_demo.py [DIR] [--ledger PATH]

DIR (default: a new temporary folder) must be missing or empty. It becomes a small git repository with two commits
(src/auth.py and src/timeoff.py), a ledger made by `ledger.py init`, and the demo data from fixtures.seed_demo():
two reviews, matching across them, a fix plan, a decision and two open gates. Then fill-refs, tag-review for both
reviews and check run through the CLI. Every expectation is asserted; if one fails the script prints FAIL and the
reason and exits 1.

--ledger PATH uses that ledger.py instead of a fresh build of dev/core.py, dev/page.html and references/schema.sql.
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dev.tests import fixtures  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seed_demo.py", description="Create a demo project with a filled ledger.")
    ap.add_argument("dir", nargs="?", help="folder to create (missing or empty); default: a new temporary folder")
    ap.add_argument("--ledger", help="ledger.py to use (default: build one from the sources)")
    a = ap.parse_args(argv)
    if a.dir:
        target = Path(a.dir).resolve()
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            print(f"seed_demo.py: {target} is not empty; give a new or empty folder", file=sys.stderr)
            return 2
    else:
        target = Path(tempfile.mkdtemp(prefix="ledger-demo-")).resolve()
    try:
        repo = fixtures.ScratchRepo(target)
        ledger = fixtures.Ledger(repo, real_init=True, ledger_py=a.ledger)
        try:
            ids = fixtures.seed_demo(ledger)
            fixtures.finish_demo(ledger)
        finally:
            ledger.close()
    except fixtures.DemoExpectationError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as e:
        print(f"seed_demo.py: error: {e}", file=sys.stderr)
        return 2
    print(f"demo project: {target}")
    print(f"  {len(ids['reviews'])} reviews, {len(ids['issues'])} issues, {len(ids['steps'])} plan steps, "
          f"{len(ids['gates'])} open gates; check passed")
    print(f"open the viewer with: python3 {target / '.code-review' / 'ledger.py'} --root {target} serve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
