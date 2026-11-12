"""references/determinism.md (plan P-0001 step 137; OWN-9, G-09): every decision SKILL.md asks for is listed, marked
tool, data or judgement. A tool decision names the command that makes it, the command exists, and SKILL.md names it
in that step; a judgement says why code cannot make it and what constrains it."""
import re
import unittest

from dev.tests import fixtures
from dev.tests.test_skill_contract import Tool, section, skill_text

DOC = fixtures.REPO_ROOT / "references" / "determinism.md"
STEPS = ("Step 0", "Step 1", "2a", "2b", "2c", "2d", "Step 3", "Step 4", "5a", "5b", "5c", "5d", "Step 6", "Step 7",
         "8a", "8b", "8c", "8d", "Step 9")


def inventory():
    """[(step, decision, by, how)] from the table."""
    out = []
    for line in DOC.read_text(encoding="utf-8").split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 4 and cells[2] in ("tool", "data", "judgement"):
            out.append(tuple(cells))
    return out


def skill_section(step):
    text = skill_text()
    title = {"Step 0": "Step 0:", "Step 1": "Step 1:", "Step 3": "Step 3:", "Step 4": "Step 4:", "Step 6": "Step 6:",
             "Step 7": "Step 7:", "Step 9": "Step 9"}.get(step)
    if title:
        return section(text, title)
    if step.startswith(("5", "8")):
        whole = section(text, "Step 5:" if step.startswith("5") else "Step 8:")
        m = re.search(rf"- \*\*{step}[.*]", whole)
        return whole[m.start():] if m else ""
    return section(text, step + ".")


class Determinism(unittest.TestCase):
    def test_determinism_inventory(self):
        rows = inventory()
        self.assertGreater(len(rows), 25)
        names, _ = Tool.get()
        for step in STEPS:
            with self.subTest(step=step):
                self.assertIn(step, {r[0] for r in rows}, f"references/determinism.md lists no decision of {step}")
        for step, decision, by, how in rows:
            with self.subTest(decision=decision):
                self.assertIn(step, STEPS)
                if by == "tool":
                    cmds = re.findall(r"`ledger\.py ([a-z][a-z0-9-]*)", how)
                    self.assertTrue(cmds, f"{decision}: a tool decision names its command")
                    for c in cmds:
                        self.assertIn(c, names, f"{decision}: ledger.py {c} does not exist")
                        self.assertRegex(" ".join(skill_section(step).split()), rf"ledger\.py {c}\b|`{c}\b",
                                         f"SKILL.md {step} does not name {c}")
                elif by == "judgement":
                    self.assertRegex(how, r"(?i)\bbecause\b", f"{decision}: say why code cannot decide it")
                    self.assertRegex(how, r"(?i)constrained by", f"{decision}: say what constrains it")
                else:
                    self.assertRegex(how, r"`[a-z_]+`", f"{decision}: name the table it is read from")

    def test_skill_points_to_it(self):
        self.assertIn("references/determinism.md", skill_text())


if __name__ == "__main__":
    unittest.main()
