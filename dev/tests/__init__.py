"""Test suite for the codebase-system-review skill (stdlib unittest only).

Run from the repository root:

    python3 -m unittest discover -s dev/tests -t . -v

Shared builders live in dev/tests/fixtures.py; import them as `from dev.tests import fixtures`.
"""
import atexit
import os
import sys
import unittest

_results = []


class _Suite(unittest.TestSuite):
    """The discovered suite; it keeps the result it ran into, for the summary of environment skips."""

    def run(self, result, debug=False):
        _results.append(result)
        return super().run(result, debug)


def _environment_skips():
    from dev.tests.fixtures import ENVIRONMENT
    seen = {}
    for result in _results:
        for test, reason in result.skipped:
            if reason.startswith(ENVIRONMENT):
                name = test.id().replace("dev.tests.", "", 1)
                module_class, _, method = name.rpartition(".")
                seen[f"{method} ({module_class})"] = reason[len(ENVIRONMENT):]
    return seen


def _print_environment_skips():
    seen = _environment_skips()
    if not seen:
        return
    n = len(seen)
    lines = [f"Skipped for this machine: {n} test{'s' if n != 1 else ''} (the run is partial)"]
    lines += [f"  {name}: {reason}" for name, reason in sorted(seen.items())]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


def load_tests(loader, standard_tests, pattern):
    """`unittest discover -s dev/tests` comes here: discover as usual, and after the result line print how many
    tests were skipped for want of something on this machine (a tool, a platform, an address), and which."""
    here = os.path.dirname(os.path.abspath(__file__))
    top = os.path.dirname(os.path.dirname(here))
    suite = _Suite([standard_tests, loader.discover(start_dir=here, pattern=pattern or "test*.py", top_level_dir=top)])
    if not _results:
        atexit.register(_print_environment_skips)
    return suite
