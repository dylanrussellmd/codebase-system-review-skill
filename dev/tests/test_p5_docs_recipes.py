"""references/recipes.md runs (plan P-0001 steps 118, 121): every ```sql block, with the runner line the file gives,
and every `ledger.py` line of its ```sh blocks, in file order, against the demo ledger; `check` passes afterwards and
the recipes' results are in the ledger.

test_recipes_run          every block runs and check still exits 0
test_redaction_logged     the redaction recipe leaves a 'redacted' event naming no value
test_tag_pushed_recorded  the tag recipe sets tag_pushed and writes an event
"""
import re
import shlex
import subprocess
import sys
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, finish_demo, seed_demo

RECIPES = fixtures.REPO_ROOT / "references" / "recipes.md"


def blocks(text):
    """[(lang, body)] of the fenced blocks with a language, in order."""
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^```(\w+)\n(.*?)^```", text, re.M | re.S)]


def runner_line(text):
    """The indented `python3 -c ...` line the file says runs a block, split into argv."""
    m = re.search(r"^    (python3 -c .*< recipe\.sql)$", text, re.M)
    assert m, "recipes.md no longer gives the runner line"
    argv = shlex.split(m.group(1))
    assert argv[-2:] == ["<", "recipe.sql"] and argv[-3] == ".code-review/ledger.sqlite", argv
    return argv[:-3]


class Recipes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = RECIPES.read_text(encoding="utf-8")
        cls.L = Ledger()
        seed_demo(cls.L)
        finish_demo(cls.L)
        L = cls.L
        sha = L.scalar("SELECT commit_sha FROM review_repositories WHERE review_id = "
                       "(SELECT id FROM reviews WHERE code = 'R-0001')")
        (L.root / "steps.jsonl").write_text(
            '{"key": "A", "phase": 1, "title": "Use a constant-time compare", "change": "hmac.compare_digest",'
            ' "proof": "test_code_compare: timing-safe", "closes": ["R-0002:SEC-M1"]}\n'
            '{"key": "B", "phase": 1, "title": "Make the notice period a setting", "change": "a setting",'
            ' "proof": "test_notice: read from settings", "closes": ["R-0002:SPEC-M1"]}\n'
            '{"kind": "phase", "phase": 1, "title": "Fix first", "goal": "The confirmed defects."}\n',
            encoding="utf-8")
        subs = {"<full commit sha>": sha, "<sha256 of the report>": "a" * 64}
        runner = runner_line(cls.text)
        cls.ran = []
        L.commit()
        for n, (lang, body) in enumerate(blocks(cls.text), 1):
            for k, v in subs.items():
                body = body.replace(k, v)
            if lang == "sql":
                p = subprocess.run([sys.executable if a == "python3" else a for a in runner] + [str(L.db)],
                                   input=body, capture_output=True, text=True, cwd=str(L.root), timeout=60)
                cls.ran.append((n, lang, body, p))
            elif lang == "sh":
                for line in body.splitlines():
                    if line.startswith("ledger.py "):
                        p = L.run(*shlex.split(line)[1:], cwd=L.root)
                        cls.ran.append((n, lang, line, p))

    @classmethod
    def tearDownClass(cls):
        cls.L.close()

    def test_recipes_run(self):
        self.assertGreaterEqual(len([r for r in self.ran if r[1] == "sql"]), 15)
        for n, lang, body, p in self.ran:
            with self.subTest(block=n, lang=lang):
                self.assertEqual(p.returncode, 0, f"block {n} failed:\n{body}\n{p.stdout}\n{p.stderr}")
        p = self.L.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        L = self.L
        self.assertEqual(L.scalar("SELECT status FROM plans WHERE code = 'P-0002'"), "approved")
        self.assertEqual(L.scalar("SELECT category FROM domains WHERE code = 'payments'"), "custom")
        self.assertEqual(L.scalar("SELECT count(*) FROM requirements"), 1)
        self.assertEqual(L.scalar("SELECT status FROM gates WHERE code = 'GATE-0001'"), "withdrawn")
        self.assertEqual(L.scalar("SELECT count(*) FROM step_commits"), 2)  # the demo has one
        self.assertEqual(L.scalar("SELECT owner_change FROM review_domains WHERE domain_code = 'database' AND "
                                  "review_id = (SELECT id FROM reviews WHERE code = 'R-0002')"), "added-by-owner")
        self.assertTrue(L.scalar("SELECT count(*) FROM artifacts WHERE path LIKE '.code-review/%'") >= 2)

    def test_redaction_logged(self):
        ev = self.L.all("SELECT * FROM events WHERE event = 'redacted'")
        self.assertEqual(len(ev), 1)
        self.assertIn("[redacted]", ev[0]["note"])
        self.assertIsNone(ev[0]["from_value"])
        self.assertIsNone(ev[0]["to_value"])

    def test_tag_pushed_recorded(self):
        L = self.L
        self.assertEqual(L.scalar("SELECT min(tag_pushed) FROM review_repositories WHERE review_id = "
                                  "(SELECT id FROM reviews WHERE code = 'R-0001')"), 1)
        self.assertEqual(L.count("events", "event = 'tag-pushed'"), 1)


if __name__ == "__main__":
    unittest.main()
