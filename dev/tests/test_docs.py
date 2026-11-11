"""The docs contract: the commands the README and SKILL.md tell people (and agents) to run must exist and work.

test_readme_commands_run       every command in README.md's "Developing" section exits 0 in a fresh copy of the repo
test_skill_md_commands_exist   every `ledger.py <command>` SKILL.md names is a command of the built CLI
"""
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from dev.tests import fixtures

REPO = fixtures.REPO_ROOT

# The placeholder the README uses for "a new folder of your choice"; the test puts a fresh temporary path there.
DEMO_PLACEHOLDER = "DEMO"


def readme_section(title):
    """The text of README.md's '## <title>' section, up to the next '## ' heading."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise AssertionError(f"README.md has no '## {title}' section")
    return m.group(1)


def section_commands(section):
    """Shell commands in a section's indented blocks: lines starting with 'python3'. A command ends at the first run
    of two or more spaces (what follows is its description)."""
    out = []
    for line in section.split("\n"):
        if not line.startswith("    "):
            continue
        s = line.strip()
        if s.startswith("python3 "):
            out.append(re.split(r"\s{2,}", s, maxsplit=1)[0])
    return out


def copy_repo(dest):
    """A fresh copy of the repository's files (what a clone of the working tree would hold: tracked and untracked
    files, minus anything ignored such as .code-review/ and __pycache__/)."""
    p = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=str(REPO),
                       capture_output=True, env=fixtures.git_env())
    if p.returncode == 0:
        names = [n for n in p.stdout.decode("utf-8").split("\0") if n]
    else:  # not a git checkout: copy everything except the ledger and caches
        names = [str(f.relative_to(REPO)) for f in REPO.rglob("*")
                 if f.is_file() and not ({".git", ".code-review", "__pycache__"} & set(f.relative_to(REPO).parts))]
    for n in names:
        src = REPO / n
        if not src.is_file():  # listed but deleted in the working tree
            continue
        d = dest / n
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(d))
    return dest


def skill_md_commands():
    """(line number, command) for every `ledger.py <command>` inside code in SKILL.md: fenced blocks and inline
    backtick spans. Prose such as 'ledger.py works out ...' is not a command."""
    text = (REPO / "SKILL.md").read_text(encoding="utf-8")
    found = []
    in_fence = False
    for no, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        spans = [line] if in_fence else re.findall(r"`([^`]+)`", line)
        for span in spans:
            span = re.split(r"(?:^|\s)#", span, maxsplit=1)[0]  # drop a shell comment
            for m in re.finditer(r"ledger\.py((?:\s+--\S+(?:\s+(?!-)\S+)?)*)\s+([a-z][a-z0-9-]*)", span):
                found.append((no, m.group(2)))
    return found


class ReadmeDeveloping(unittest.TestCase):

    def test_readme_developing_lists_the_commands(self):
        cmds = section_commands(readme_section("Developing"))
        joined = "\n".join(cmds)
        self.assertIn("python3 dev/build.py", cmds)
        self.assertIn("python3 dev/build.py --check", cmds)
        self.assertRegex(joined, r"python3 dev/tests/seed_demo\.py " + DEMO_PLACEHOLDER)
        for c in cmds:
            self.assertNotRegex(c, r"<[^>]+>", f"README command has a placeholder the reader cannot run: {c}")

    def test_readme_commands_run(self):
        cmds = section_commands(readme_section("Developing"))
        self.assertTrue(cmds, "README Developing section lists no commands")
        work = fixtures.temp_dir("readme-")
        clone = copy_repo(work / "clone")
        demo = work / "demo"  # must not exist yet: seed_demo.py wants a new or empty folder
        for c in cmds:
            argv = [a.replace(DEMO_PLACEHOLDER, str(demo)) if a == DEMO_PLACEHOLDER or a.startswith(DEMO_PLACEHOLDER + "/")
                    else a for a in shlex.split(c)]
            self.assertEqual(argv[0], "python3")
            if "serve" in argv:  # serve blocks until Ctrl+C; its argument parsing is checked with --help instead
                argv = argv[:argv.index("serve") + 1] + ["--help"]
            p = subprocess.run([sys.executable, *argv[1:]], cwd=str(clone), env=fixtures.git_env(),
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, f"README command failed: {c}\n{p.stdout}\n{p.stderr}")


class SkillMdCommands(unittest.TestCase):

    def test_skill_md_commands_exist(self):
        found = skill_md_commands()
        names = {c for _, c in found}
        # SKILL.md's command list (lines 43-51 at ea00196) must at least name these.
        for need in ("init", "schema", "serve", "check", "fill-refs", "tag-review", "show", "gates", "dump"):
            self.assertIn(need, names, f"SKILL.md no longer names `ledger.py {need}`")
        ledger_py = fixtures.built_ledger_path()
        for name in sorted(names):
            p = fixtures.run_ledger(name, "--help", ledger_py=ledger_py, cwd=fixtures.temp_dir("help-"))
            where = ", ".join(str(n) for n, c in found if c == name)
            self.assertEqual(p.returncode, 0,
                             f"SKILL.md line {where} names `ledger.py {name}`, which the CLI does not have:\n{p.stderr}")

    def test_skill_md_parser_finds_only_code(self):
        # 'ledger.py works out ...' in prose is not taken for a command; a fenced line is.
        self.assertNotIn("works", {c for _, c in skill_md_commands()})


if __name__ == "__main__":
    unittest.main()
