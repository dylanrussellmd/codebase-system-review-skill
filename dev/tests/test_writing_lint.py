"""The writing standard (references/writing.md, plan P-0001 step 134) applied to the skill's own text: SKILL.md,
README.md and every Markdown file under references/ contain none of the standard's history, filler, wordy or
announcing phrases.

The phrases are the tokens of the optional Vale rules shipped in references/vale/styles/Plain/, so the two cannot
drift. CHANGELOG.md is exempt (history belongs there); in writing.md, text inside double quotes is an example.
"""
import re
import unittest

from dev.tests import fixtures

REPO = fixtures.REPO_ROOT
PLAIN = REPO / "references" / "vale" / "styles" / "Plain"
RULES = ("History", "Filler", "Wordy", "Announce")


def tokens(rule):
    """The patterns of one Vale rule file: the items under `tokens:` or the left-hand sides under `swap:`."""
    out, inside = [], None
    for line in (PLAIN / f"{rule}.yml").read_text(encoding="utf-8").splitlines():
        if line in ("tokens:", "swap:"):
            inside = line[:-1]
        elif inside == "tokens" and line.startswith("  - "):
            out.append(line[4:].strip())
        elif inside == "swap" and line.startswith("  "):
            out.append(line.strip().rsplit(": ", 1)[0])
        elif line and not line.startswith(" "):
            inside = None
    return out


def patterns():
    return [(rule, re.compile(r"\b(?:%s)\b" % t, re.I)) for rule in RULES for t in tokens(rule)]


def linted_files():
    return [REPO / "SKILL.md", REPO / "README.md"] + sorted((REPO / "references").rglob("*.md"))


def breaches(text, pats=None):
    """(line number, rule, phrase) for every banned phrase in text."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        for rule, pat in pats or patterns():
            for m in pat.finditer(line):
                out.append((n, rule, m.group(0)))
    return out


class WritingLint(unittest.TestCase):

    def test_writing_lint(self):
        pats = patterns()
        bad = []
        for f in linted_files():
            text = f.read_text(encoding="utf-8")
            if f.name == "writing.md":
                text = re.sub(r'"[^"\n]*"', '""', text)
            bad += [f"{f.relative_to(REPO)}:{n}: {rule}: {phrase}" for n, rule, phrase in breaches(text, pats)]
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_lint_reads_the_shipped_rules(self):
        for rule in RULES:
            self.assertGreater(len(tokens(rule)), 5, rule)
        found = {rule for _, rule, _ in breaches(
            "It was previously renamed.\nNote that it simply works.\nIn order to run it.\nThis section describes it.")}
        self.assertEqual(found, set(RULES))

    def test_writing_md_names_its_banned_phrases(self):
        text = (REPO / "references" / "writing.md").read_text(encoding="utf-8")
        for phrase in ("previously", "used to", "was changed to", "no longer", "note that", "it's important to",
                       "simply", "in order to"):
            self.assertIn(f'"{phrase}"', text)

    def test_changelog_is_not_linted(self):
        self.assertNotIn(REPO / "CHANGELOG.md", linted_files())
        self.assertIn(REPO / "references" / "domains.md", linted_files())

    def test_vale_config_ships_and_says_untested(self):
        ini = (REPO / "references" / "vale" / ".vale.ini").read_text(encoding="utf-8")
        self.assertIn("Untested", ini.splitlines()[0])
        for style in re.findall(r"^BasedOnStyles = (.*)$", ini, re.M):
            for name in (s.strip() for s in style.split(",")):
                if name != "Vale":
                    self.assertTrue((PLAIN.parent / name).is_dir(), name)


if __name__ == "__main__":
    unittest.main()
