"""README.md (plan P-0001 step 211): every command and path it names exists.

test_readme_commands_exist   every `ledger.py <command>` in README.md is a command of the built CLI, and every dev/,
                             references/ or scripts/ path it names exists
test_readme_sections         install from a bundle or a clone, what the ledger stores, maintenance, uninstalling and
                             contributing each have their place
"""
import re
import unittest

from dev.tests import fixtures

REPO = fixtures.REPO_ROOT


def readme():
    return (REPO / "README.md").read_text(encoding="utf-8")


def code_text(text):
    """Inline code spans and indented block lines (a block line up to its description) that start a command."""
    spans = re.findall(r"`([^`\n]+)`", text)
    blocks = [re.split(r"\s{2,}", line.strip(), maxsplit=1)[0] for line in text.split("\n") if line.startswith("    ")]
    return [c for c in spans + blocks if c.startswith(("ledger.py", "python3"))]


class Readme(unittest.TestCase):

    def test_readme_commands_exist(self):
        text = readme()
        names = set()
        for code in code_text(text):
            for m in re.finditer(r"ledger\.py(?:\s+--\S+(?:\s+(?!-)\S+)?)*\s+([a-z][a-z0-9-]*)", code):
                names.add(m.group(1))
        self.assertTrue({"dump", "restore", "migrate", "merge", "check", "serve"} <= names, names)
        ledger_py = fixtures.built_ledger_path()
        for name in sorted(names):
            with self.subTest(command=name):
                p = fixtures.run_ledger(name, "--help", ledger_py=ledger_py, cwd=fixtures.temp_dir("readme-help-"))
                self.assertEqual(p.returncode, 0, f"README names ledger.py {name}, which the CLI does not have")
        paths = set(re.findall(r"(?<![\w./-])((?:dev|references|scripts)/[\w./-]*[\w/])", text))
        self.assertIn("dev/release.py", paths)
        for rel in sorted(p for p in paths if "NNNN" not in p):   # NNNN: a placeholder for the next number
            with self.subTest(path=rel):
                self.assertTrue((REPO / rel).exists(), f"README names {rel}, which does not exist")

    def test_readme_sections(self):
        text = readme()
        for heading in ("## Install", "## What the ledger stores", "## Where the ledger lives", "## Maintaining a ledger",
                        "## Developing", "## Testing", "## Contributing"):
            self.assertIn(heading, text)
        self.assertIn("dist/codebase-system-review-<version>.zip", text)
        self.assertIn("copy or symlink `SKILL.md`, `scripts/` and `references/`", text)
        self.assertRegex(text, r"To uninstall, delete")
        self.assertRegex(text, r"To remove a project's ledger")
        self.assertIn("**Privacy.**", text)
        self.assertIn("dev/                             in the repository only, not in the bundle", text)
        self.assertIn("schema.sql (the schema's source of truth", text)
        self.assertTrue((REPO / "dev" / "viewer.png").is_file())


if __name__ == "__main__":
    unittest.main()
