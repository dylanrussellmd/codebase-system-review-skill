"""SKILL.md's steps write only through commands (plan P-0001 step 131: FIX-15)."""
import re
import unittest

from dev.tests import fixtures

SKILL = (fixtures.REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
RECIPES = (fixtures.REPO_ROOT / "references" / "recipes.md").read_text(encoding="utf-8")


def subcommands():
    out = fixtures.run_ledger("--help").stdout
    return set(out.split("{", 1)[1].split("}", 1)[0].split(","))


class SkillWritesTest(unittest.TestCase):
    def test_every_named_command_exists(self):
        known = subcommands()
        for name, text in (("SKILL.md", SKILL), ("recipes.md", RECIPES)):
            text = re.sub(r"--root <[^>]*> ", "", text)
            named = set(re.findall(r"ledger\.py (?:--[a-z-]+ \S+ )*([a-z][a-z-]+)", text))
            self.assertEqual(sorted(named - known - {"py"}), [], f"{name} names commands the tool lacks")

    # Until SKILL.md and references/recipes.md use the step 131 commands (the docs agent applies the changes listed in
    # the step 131 report): then this passes, and the decorator must go.
    @unittest.expectedFailure
    def test_skill_steps_need_no_sql(self):
        steps = SKILL[SKILL.index("## Step 0"):SKILL.index("## Reference files")]
        self.assertNotIn("recipes.md", steps, "a SKILL.md step still sends a write to a recipe")
        rules = SKILL[:SKILL.index("## Step 0")]
        self.assertNotRegex(rules, r"Writes with no command yet|commands are planned for them")
        # recipes.md keeps SQL only for corrections (and what goes wrong after them)
        before = RECIPES[:RECIPES.index("## Correcting the ledger")]
        self.assertNotIn("```sql", before, "recipes.md still has SQL for a write that has a command")
        self.assertNotIn("#### Redact a secret", RECIPES)
        self.assertNotRegex(RECIPES[RECIPES.index("### Redact a secret"):], r"^```sql", "redaction is `redact` now")


if __name__ == "__main__":
    unittest.main()
