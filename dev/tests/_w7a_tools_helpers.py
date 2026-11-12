"""Shared setup for the picked-tools tests (plan P-0001 steps 218-221). Not a test module.

    DATA                     dev/tests/data/tools/: recorded tool outputs (references/tools.md, "How a tool is wired
                             in", says which are real runs and which are written from the documented format)
    tool_project(test)       project() with src/app.py as the samples cite it, and src/util.js
    shim(name, script)       a folder holding an executable `name` that runs the Python `script` (a stand-in tool)
    path_with(*dirs)         PATH with these folders first; path_without_tools() keeps only git and the shell
    leads(L)                 [(code, rule, path, line_start, level)] of every lead, by code
"""
import os
import shutil
import stat
import sys
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


def tool_project(test, files=None):
    """(repo, L, sha): a project whose R-0001 looked at a commit holding src/app.py and src/util.js; L runs the
    skill's copy of ledger.py (skill_ledger_path)."""
    repo, L, sha = project(test, files=files or {"src/app.py": APP_PY, "src/util.js": UTIL_JS,
                                                 "tests/test_app.py": "import unittest\n"})
    L.ledger_py = skill_ledger_path()
    return repo, L, sha


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


__all__ = ["skill_ledger_path", "DATA", "APP_PY", "UTIL_JS", "tool_project", "shim", "bare_bin", "env_with", "leads", "ok"]
