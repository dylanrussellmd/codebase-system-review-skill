"""The writing standard (references/writing.md, plan P-0001 step 134) applied to the skill's own text: SKILL.md,
README.md and every Markdown file under references/ contain none of the standard's history, filler, wordy or
announcing phrases. The same rules hold for every string dev/page.html can show and for the hub's text in dev/core.py
(test_page_text_lint).

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


def page_strings(page=None):
    """What dev/page.html can put on screen: the text of its HTML body and each string of its script that reads as
    words (two words together, or one capitalised word). Comments and code are not text."""
    page = (REPO / "dev" / "page.html").read_text(encoding="utf-8") if page is None else page
    at = page.index('"use strict";')
    body = re.sub(r"<style>.*?</style>", "", page[:page.rindex("<script>", 0, at)], flags=re.S)
    out = [t.strip() for t in re.findall(r">([^<>]+)<", body) if t.strip()]
    script = re.sub(r"^\s*//.*$|\s//\s.*$", "", page[at + len('"use strict";'):page.rindex("</script>")], flags=re.M)
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`', script):
        t = m.group(1) if m.group(1) is not None else m.group(2)
        if re.search(r"[A-Za-z']{2,} [A-Za-z']{2,}", t) or re.match(r"[A-Z][a-z]+$", t):
            out.append(t)
    return out


def hub_strings():
    """The words dev/core.py sends to the page: the How it works text and what the agent does about each check."""
    core = (REPO / "dev" / "core.py").read_text(encoding="utf-8")
    guide = core[core.index('HUB_GUIDE_MD = """'):]
    guide = guide[:guide.index('"""', 20)]
    actions = core[core.index("CHECK_ACTIONS = {"):core.index("RECIPES_FILE =")]
    return guide.split("\n") + re.findall(r'": "([^"]+)"', actions)


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

    def test_page_text_lint(self):
        """The viewer's and the hub's own words meet the same rules: every sentence the page can show (its HTML
        text and the strings of its script), the How it works text and what the agent does about each check."""
        pats = patterns()
        texts = page_strings() + hub_strings()
        self.assertGreater(len(texts), 150, "the scan found too few strings; its pattern is stale")
        bad = [f"{rule}: {phrase!r} in {t!r}" for t in texts for _, rule, phrase in breaches(t, pats)]
        # words for the tool's insides: the reader sees a copy, the agent and a question
        bad += [f"internal name: {t!r}" for t in texts if re.search(r"\bsnapshot\b|\bthe manager\b|\bskill runs\b", t, re.I)]
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_page_scan_finds_a_banned_phrase(self):
        found = [s for s in page_strings('<p>Fine</p><script>"use strict";\nh("p", null, "Note that it was changed to X.");</script>')]
        self.assertEqual(found, ["Fine", "Note that it was changed to X."])
        self.assertEqual({rule for _, rule, _ in breaches(found[1])}, {"Filler", "History"})

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
