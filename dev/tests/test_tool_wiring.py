"""Every tool adapter's wiring runs without the tool installed: a stand-in (shim) on PATH prints a recorded output, and
`tool-run <tool> --unpinned` must start it, read what it prints and record the same leads as `--from` the recording.
The tests named *_real or *_matches_the_recording run the real tool and skip without it; these never skip.

test_every_adapter_has_a_shim_test   every adapter with a binary is started through a shim in some test here
test_<tool>_wiring                   buf, squawk, graphql-inspector, jscpd and i18n-lint (ESLint) through shims
"""
import json
import re
import sys
import unittest
from pathlib import Path

from dev.tests import fixtures
from dev.tests._w7a_tools_helpers import DATA, env_with, ok, shim, tool_project

HERE = Path(__file__).resolve().parent

# A shim that prints a version and otherwise writes a recording: %(version)r, then the body.
SHIM = """import os, sys
if '--version' in sys.argv or 'version' in sys.argv[1:2]:
    print(%(version)r); sys.exit(0)
data = open(%(data)r, encoding='utf-8').read().replace('/project', os.getcwd())
%(body)s
"""


def make_shim(name, data, body="sys.stdout.write(data)", version="1.0.0"):
    return shim(name, SHIM % {"version": version, "data": str(data), "body": body})


def npm_shim(name, mods, data, body, version):
    """make_shim's stand-in installed as npm installs a CLI: node_modules/<name>/bin/<name>. Returns the bin folder."""
    bin_dir = mods / name / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / f"{name}.py"
    script.write_text(SHIM % {"version": version, "data": str(data), "body": body}, encoding="utf-8")
    (bin_dir / name).write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n', encoding="utf-8")
    (bin_dir / name).chmod(0o755)
    return str(bin_dir)


def rows(L, run):
    L.commit()
    return [list(r.values()) for r in L.all("SELECT rule, path, line_start, line_end, level, message FROM tool_results "
                                            "WHERE tool_run_id=? ORDER BY code", (run,))]


def follow_up(repo, L, files):
    for rel, text in files.items():
        repo.write(rel, text)
    repo.commit("second")
    ok(L, "dump")
    ok(L, "review-status", "R-0001", "handed-off", "--note", "done", "--actor", "A-0001")
    ok(L, "new-review", "--title", "Second", "--scope", "full", "--previous", "R-0001", "--actor", "A-0001")


class Wiring(unittest.TestCase):

    def same_as_recording(self, L, tool, review, data, env, at=None):
        """tool-run through the shim, then --from the recording: the same leads, and the run is recorded as ran."""
        ok(L, "tool-run", tool, "--review", review, "--unpinned", "--actor", "A-0001", env=env)
        ok(L, "tool-run", tool, "--review", review, "--from", str(at or data), "--actor", "A-0001")
        a, b = rows(L, 1), rows(L, 2)
        self.assertTrue(a, f"{tool}: the shim's output gave no leads")
        self.assertEqual(a, b)
        self.assertEqual(L.scalar("SELECT status FROM tool_runs WHERE id=1"), "ran")

    def test_every_adapter_has_a_shim_test(self):
        mod = fixtures.load_ledger_module()
        binaries = {a.binary for a in mod.TOOL_ADAPTERS.values() if a.binary}
        self.assertGreater(len(binaries), 15)
        text = "\n".join(f.read_text(encoding="utf-8") for f in HERE.glob("test_*.py"))
        shimmed = set(re.findall(r"""(?:make_|npm_)?shim\(\s*["']([a-z0-9-]+)["']""", text))
        self.assertEqual(sorted(binaries - shimmed), [], "adapters whose wiring no test runs without the tool")

    def test_buf_wiring(self):
        old = 'syntax = "proto3";\npackage demo.v1;\nmessage User {\n  int64 id = 1;\n  string email = 2;\n}\n'
        new = 'syntax = "proto3";\npackage demo.v1;\nmessage User {\n  string id = 1;\n}\n'
        repo, L, sha = tool_project(self, files={"proto/user.proto": old})
        follow_up(repo, L, {"proto/user.proto": new})
        body = ("assert sys.argv[1:3] == ['breaking', '.'] and '--against' in sys.argv, sys.argv\n"
                "sys.stdout.write(data); sys.exit(100)")
        self.same_as_recording(L, "buf", "R-0002", DATA / "buf.jsonl",
                               env_with(make_shim("buf", DATA / "buf.jsonl", body, "1.47.2")))

    def test_squawk_wiring(self):
        from dev.tests.test_w7b_tools_api import MIGRATIONS
        repo, L, sha = tool_project(self, files=MIGRATIONS)
        body = ("assert sys.argv[1:3] == ['--reporter', 'json'], sys.argv\n"
                "assert any(a.endswith('0001_init.sql') for a in sys.argv), sys.argv\n"
                "sys.stdout.write(data); sys.exit(1)")
        self.same_as_recording(L, "squawk", "R-0001", DATA / "squawk.json",
                               env_with(make_shim("squawk", DATA / "squawk.json", body, "squawk 2.71.0")))

    def test_graphql_inspector_wiring(self):
        old = "type Query {\n  user(id: ID!): User\n}\n\ntype User {\n  id: ID!\n  email: String\n}\n"
        new = "type Query {\n  user(id: ID!, active: Boolean!): User\n}\n\ntype User {\n  id: ID!\n  nickname: String\n}\n"
        repo, L, sha = tool_project(self, files={"schema.graphql": old})
        follow_up(repo, L, {"schema.graphql": new})
        body = ("assert sys.argv[1] == 'diff' and sys.argv[-1] == 'schema.graphql', sys.argv\n"
                "sys.stdout.write(data); sys.exit(1)")
        self.same_as_recording(L, "graphql-inspector", "R-0002", DATA / "graphql-inspector.txt",
                               env_with(make_shim("graphql-inspector", DATA / "graphql-inspector.txt", body)))

    def test_jscpd_wiring(self):
        repo, L, sha = tool_project(self)
        body = ("out = sys.argv[sys.argv.index('--output') + 1]\n"
                "os.makedirs(out, exist_ok=True)\n"
                "open(os.path.join(out, 'jscpd-report.json'), 'w', encoding='utf-8').write(data)")
        self.same_as_recording(L, "jscpd", "R-0001", DATA / "jscpd.json",
                               env_with(make_shim("jscpd", DATA / "jscpd.json", body, "4.0.5")))

    def test_i18n_lint_wiring(self):
        """ESLint is found with eslint-plugin-i18next beside it in node_modules, as npm installs them."""
        repo, L, sha = tool_project(self, files={"src/components/Banner.jsx": "x\n", "locales/en.json": "{}"})
        mods = fixtures.temp_dir("w7b-npm-") / "node_modules"
        (mods / "eslint-plugin-i18next").mkdir(parents=True)
        (mods / "eslint-plugin-i18next" / "package.json").write_text(
            json.dumps({"name": "eslint-plugin-i18next", "version": "6.1.1"}), encoding="utf-8")
        body = ("out = sys.argv[sys.argv.index('--output-file') + 1]\n"
                "assert '--config' in sys.argv and sys.argv[-1] == '.', sys.argv\n"
                "open(out, 'w', encoding='utf-8').write(data)")
        bin_dir = npm_shim("eslint", mods, DATA / "i18n-lint.json", body, "v9.14.0")
        recording = fixtures.temp_dir("w7b-sample-") / "i18n-lint.json"
        recording.write_text((DATA / "i18n-lint.json").read_text(encoding="utf-8").replace("/project", str(repo.path)),
                             encoding="utf-8")
        ok(L, "tool-run", "i18n-lint", "--review", "R-0001", "--unpinned", "--actor", "A-0001",
           env=env_with(str(bin_dir)))
        ok(L, "tool-run", "i18n-lint", "--review", "R-0001", "--from", str(recording), "--actor", "A-0001")
        a, b = rows(L, 1), rows(L, 2)
        self.assertEqual(len(a), 3)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
