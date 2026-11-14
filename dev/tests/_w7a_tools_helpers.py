"""Shared setup for the picked-tools tests (plan P-0001 steps 218-221). Not a test module.

    DATA                     dev/tests/data/tools/: recorded tool outputs (references/tools.md, "How a tool is wired
                             in", says which are real runs and which are written from the documented format)
    tool_project(test)       project() with src/app.py as the samples cite it, and src/util.js
    shared_project(test, f)  one such project per test module, for tests that only read what a skipped run prints
    shim(name, script)       a folder holding an executable `name` that runs the Python `script` (a stand-in tool)
    path_with(*dirs)         PATH with these folders first; path_without_tools() keeps only git and the shell
    leads(L)                 [(code, rule, path, line_start, level)] of every lead, by code
    RECORDED, RECORDED_FIRST the project most samples were recorded on, and the commit before it (gitleaks reads
                             the history)
    assert_matches_recording(test, tool, sample, ...)
                             the real tool's leads and metrics on a project equal the import of its recorded sample
"""
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, project

DATA = Path(__file__).resolve().parent / "data" / "tools"

APP_PY = (
    "import datetime\nimport logging\n\nimport requests\n\nlog = logging.getLogger(__name__)\n\n\n"
    "def fetch(url):\n    return requests.get(url)\n\n\ndef today():\n    return datetime.datetime.now()\n\n\n"
    "def is_admin(user):\n    if user.role == \"admin\":\n        return True\n    return False\n\n\n"
    "def risky():\n    try:\n        return 1 / 0\n    except Exception:\n        pass\n\n\n"
    "def area(w, h):\n    total = w * h\n    if total < 0:\n        raise ValueError(\"negative\")\n    return total\n")
UTIL_JS = "".join(f"// line {n}\n" for n in range(1, 31))


_skill_copy = None


def skill_ledger_path():
    """The fresh build placed as the skill places it, scripts/ledger.py beside references/ (a link to this
    repository's), so it finds references/tools.lock and references/rules/ as the shipped copy does."""
    global _skill_copy
    if _skill_copy is None:
        d = fixtures.temp_dir("w7a-skill-")
        (d / "scripts").mkdir()
        shutil.copy(str(fixtures.built_ledger_path()), str(d / "scripts" / "ledger.py"))
        os.symlink(str(fixtures.REPO_ROOT / "references"), str(d / "references"))
        _skill_copy = d / "scripts" / "ledger.py"
    return _skill_copy


def tool_project(test, files=None, earlier=()):
    """(repo, L, sha): a project whose R-0001 looked at a commit holding src/app.py and src/util.js (or `files`,
    after the commits of `earlier`); L runs the skill's copy of ledger.py (skill_ledger_path)."""
    repo, L, sha = project(test, files=files or {"src/app.py": APP_PY, "src/util.js": UTIL_JS,
                                                 "tests/test_app.py": "import unittest\n"}, earlier=earlier)
    L.ledger_py = skill_ledger_path()
    return repo, L, sha


# The project the samples of scc, jscpd, gitleaks, actionlint, zizmor, semgrep and osv-scanner come from: config.py
# gets an AWS key in one commit and a GitHub token in the next; the workflow has the expression injection, the
# dangerous trigger and the unpinned action; src/util.js holds one copy of nine lines. The key and the token are
# put together here so this file holds neither.
_COPY = ("  const out = [];\n  for (const item of items) {\n    if (item && item.enabled && item.visible) {\n"
         "      out.push({ id: item.id, name: item.name, size: item.size * 2 });\n    }\n  }\n  return out;\n}\n")
RECORDED_UTIL_JS = ("// Demo helpers.\nexport const VERSION = \"1.0\";\nexport const NAME = \"demo\";\n"
                    "export const EMPTY = [];\nexport function id(x) {\n  return x;\n}\nexport const LIMIT = 10;\n"
                    "export const STEP = 2;\n\nexport function copyA(items) {\n" + _COPY +
                    "\nexport function copyB(items) {\n" + _COPY +
                    "\n// Two copies of one loop, for the copy detector.\n")
RECORDED_TEST_PY = ("import unittest\n\nfrom src import app\n\n\nclass AppTest(unittest.TestCase):\n"
                    "    def test_area(self):\n        self.assertEqual(app.area(2, 3), 6)\n\n"
                    "    def test_admin(self):\n        user = type(\"U\", (), {\"role\": \"admin\"})()\n"
                    "        self.assertTrue(app.is_admin(user))\n        if app.is_admin(user):\n            pass\n"
                    "        self.assertFalse(False)\n")
RECORDED_WORKFLOW = ("on: [push, pull_request_target]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                     "      - uses: actions/checkout@v4\n      - run: echo \"${{ github.event.pull_request.title }}\"\n"
                     "      - run: python -m unittest\n")
_AWS_LINE = 'AWS_ACCESS_KEY_ID = "' + "AKIA" + "QX4NZ7KJ3PLM2RTW" + '"\n'
_TOKEN_LINE = 'GITHUB_TOKEN = "' + "ghp_" + "".join(chr(97 + n * 7 % 26) if n % 3 else str(n % 10)
                                                    for n in range(36)) + '"\n'
RECORDED_FIRST = {"config.py": _AWS_LINE}
RECORDED = {"config.py": _AWS_LINE + _TOKEN_LINE, "src/app.py": APP_PY + "a = 1\nb = 2\n",
            "tests/test_app.py": RECORDED_TEST_PY, "src/util.js": RECORDED_UTIL_JS, "README.md": "# Demo\n",
            "requirements.txt": "requests==2.19.0\n", ".github/workflows/ci.yml": RECORDED_WORKFLOW}


def _sample_at(sample, repo, folder="/project"):
    """A copy of a recorded sample with its scratch folder pointed at this repository."""
    f = fixtures.temp_dir("w7a-sample-") / Path(sample).name
    f.write_text(Path(sample).read_text(encoding="utf-8").replace(folder, str(repo.path)), encoding="utf-8")
    return str(f)


def _results(L):
    L.commit()
    leads = [tuple(r.values()) for r in L.all("SELECT rule, level, path, line_start, line_end, message "
                                               "FROM tool_results ORDER BY code")]
    metrics = [tuple(r.values()) for r in L.all("SELECT name, scope, value, unit FROM metrics ORDER BY name, scope")]
    return leads, metrics


def assert_matches_recording(test, tool, sample, files=None, *, earlier=None, args=(), env=None, same=None,
                             prepare=None):
    """Run `tool` for real on a project (RECORDED after RECORDED_FIRST, or `files`) and import `sample` into the
    ledger of a second project with the same commits; their leads and metrics must be equal and not empty.
    `same(message)` puts a message in the form both sides share (a commit id the two projects cannot share);
    `prepare(L)` runs before the real run. Leads are compared in sorted order. Skipped, as an environment skip, when
    the tool is absent or its version is not the pinned one. Returns the real run's leads, as stored, and its repo."""
    mod = fixtures.load_ledger_module()
    ad = mod.TOOL_ADAPTERS[tool]
    version = mod.tool_version(ad)
    pinned = mod.read_tools_lock(fixtures.REPO_ROOT / "references" / "tools.lock").get(ad.lock_key)
    if version is None:
        test.skipTest(f"environment: {tool} is not installed")
    if version != pinned:
        test.skipTest(f"environment: {tool} {version or '(version unknown)'} is installed; tools.lock pins {pinned}")
    if files is None:
        files, earlier = RECORDED, ((RECORDED_FIRST,) if earlier is None else earlier)
    got, raw = [], None
    for real in (True, False):
        repo, L, sha = tool_project(test, files=files, earlier=earlier or ())
        if real and prepare:
            prepare(L)
        how = list(args) if real else ["--from", _sample_at(sample, repo)]
        out = ok(L, "tool-run", tool, "--review", "R-0001", *how, "--actor", "A-0001", env=env)
        test.assertEqual(L.scalar("SELECT status FROM tool_runs"), "ran" if real else "imported", out)
        leads, metrics = _results(L)
        if real:
            raw = (leads, repo)
        shared = [(*lead[:-1], same(lead[-1]) if same else lead[-1]) for lead in leads]
        got.append((sorted(shared, key=repr), metrics))
    test.assertTrue(got[0][0] or got[0][1], f"{tool} gave no leads and no metrics")
    test.assertEqual(got[0], got[1])
    return raw


class _ModuleScope:
    """Stands in for a test in project(): its cleanups run when the test module ends."""
    addCleanup = staticmethod(unittest.addModuleCleanup)


_shared = {}


def shared_project(test, files=None):
    """tool_project() made once per test module for these files, for tests that only read what a command prints
    (a tool skipped for want of its input): each such test adds runs to the same ledger. It is closed when the
    module's tests end."""
    key = (type(test).__module__, tuple(sorted((files or {}).items())))
    if key not in _shared:
        _shared[key] = tool_project(_ModuleScope, files=files)
    return _shared[key]


def shim(name, script):
    """A temporary folder with an executable `name` that runs `script` with this Python."""
    d = fixtures.temp_dir("w7a-bin-")
    body = Path(d) / f"{name}.py"
    body.write_text(script, encoding="utf-8")
    exe = Path(d) / name
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {body} \"$@\"\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(d)


def bare_bin():
    """A folder holding only git and sh (links), for a PATH on which no picked tool is found."""
    d = fixtures.temp_dir("w7a-bare-")
    for name in ("git", "sh"):
        found = shutil.which(name)
        if found:
            os.symlink(found, Path(d) / name)
    return str(d)


def env_with(*dirs, bare=False):
    """fixtures.git_env() with these folders first on PATH (with bare, only them and bare_bin())."""
    base = bare_bin() if bare else os.environ.get("PATH", "")
    return fixtures.git_env({"PATH": os.pathsep.join([*dirs, base])})


def leads(L):
    L.commit()
    return [(r["code"], r["rule"], r["path"], r["line_start"], r["level"])
            for r in L.all("SELECT code, rule, path, line_start, level FROM tool_results ORDER BY code")]


__all__ = ["skill_ledger_path", "DATA", "RECORDED", "RECORDED_FIRST", "assert_matches_recording",
           "APP_PY", "UTIL_JS", "tool_project", "shared_project", "shim", "bare_bin", "env_with", "leads", "ok"]
