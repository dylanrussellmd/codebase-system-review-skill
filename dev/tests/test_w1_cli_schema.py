"""The comments of references/schema.sql: the first comment line above each table and view is one plain sentence
(the viewer shows it as the description), every trigger says what it does, and the comments that name facts (the
meta keys, the direction of v_issue_graph) match the tool."""
import re
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, temp_dir

SCHEMA = fixtures.REPO_ROOT / "references" / "schema.sql"
CREATE = re.compile(r"CREATE (TABLE|VIEW|TRIGGER) (\w+)")
# Triggers of one family share the comment above the family's first trigger.
TIME_FAMILY = "tr_time_"


def comment_blocks():
    """(kind, name) -> the comment lines directly above its CREATE, top line first ([] when there are none)."""
    lines = SCHEMA.read_text(encoding="utf-8").split("\n")
    out = {}
    for i, line in enumerate(lines):
        m = CREATE.match(line)
        if not m:
            continue
        j = i
        while j > 0 and lines[j - 1].startswith("--"):
            j -= 1
        out[(m.group(1), m.group(2))] = [l[2:].strip() for l in lines[j:i]]
    return out


class FirstLines(unittest.TestCase):
    def test_every_table_commented(self):
        """Each table and view opens with one sentence in plain words: no column or object names, no code
        spans, no schema or plan-step numbers."""
        blocks = comment_blocks()
        checked = 0
        for (kind, name), block in sorted(blocks.items()):
            if kind == "TRIGGER":
                continue
            checked += 1
            with self.subTest(kind=kind, name=name):
                self.assertTrue(block, "no comment above it")
                first = block[0]
                self.assertRegex(first, r"^[A-Z].*\.$")
                self.assertNotRegex(first, r"[_`]|\bschema \d|\bstep \d|\b[A-Z]+-\d|\bNULL\b|->")
                self.assertLessEqual(len(first), 116)
        self.assertGreater(checked, 70)

    def test_every_trigger_commented(self):
        blocks = comment_blocks()
        triggers = [(n, b) for (k, n), b in sorted(blocks.items()) if k == "TRIGGER"]
        self.assertGreater(len(triggers), 150)
        for name, block in triggers:
            if name.startswith(TIME_FAMILY):
                continue
            with self.subTest(trigger=name):
                self.assertTrue(block, "no comment above it")
        family = [b for n, b in triggers if n.startswith(TIME_FAMILY) and b]
        self.assertEqual(len(family), 1, "one comment covers the time-format triggers")

    def test_reviewer_label_example_is_neutral(self):
        text = SCHEMA.read_text(encoding="utf-8")
        self.assertFalse("engine-A" in text)
        self.assertTrue("'tests-api'" in text)


class DemoWording(unittest.TestCase):
    def test_demo_wording_is_neutral(self):
        """The demo project names no particular product: nothing left of the app the skill was first written for."""
        for name in ("fixtures.py", "seed_demo.py"):
            text = (fixtures.REPO_ROOT / "dev" / "tests" / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"engine-A|[Tt]ime[ -]off", text))


class Facts(unittest.TestCase):
    def test_meta_keys_documented(self):
        """The keys the meta comment lists are the keys init writes."""
        text = SCHEMA.read_text(encoding="utf-8")
        m = re.search(r"CREATE TABLE meta \(\n\s*key\s+TEXT PRIMARY KEY,\s*--([^\n]*(?:\n\s*--[^\n]*)*)", text)
        self.assertIsNotNone(m)
        listed = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", m.group(1)))
        L = Ledger(temp_dir("meta-"), real_init=True)
        self.addCleanup(L.close)
        self.assertEqual(listed, {r["key"] for r in L.all("SELECT key FROM meta")})

    def test_issue_graph_rows(self):
        """v_issue_graph lists each relation once, in its stored direction, as its comment says."""
        L = Ledger(temp_dir("graph-"))
        self.addCleanup(L.close)
        rid = L.review()
        a, b, c = (L.issue(first_seen_review_id=rid) for _ in range(3))
        L.relation(a, b, "duplicate-of")
        L.relation(b, c, "blocks")
        L.commit()
        codes = {i: L.scalar("SELECT code FROM issues WHERE id=?", (i,)) for i in (a, b, c)}
        got = sorted((r["from_issue"], r["relation"], r["to_issue"]) for r in L.all("SELECT * FROM v_issue_graph"))
        self.assertEqual(got, sorted([(codes[a], "duplicate-of", codes[b]), (codes[b], "blocks", codes[c])]))
        block = comment_blocks()[("VIEW", "v_issue_graph")]
        self.assertIn("once", " ".join(block))
        self.assertNotIn("both directions", " ".join(block))

    def test_fingerprint_comment_matches_the_tool(self):
        text = SCHEMA.read_text(encoding="utf-8")
        self.assertFalse("[:symbol]" in text)
        mod = fixtures.load_ledger_module()
        self.assertEqual(mod.fingerprint("security", "authorization", "admin-route").count(":"), 2)


if __name__ == "__main__":
    unittest.main()
