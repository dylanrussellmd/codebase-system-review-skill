"""scan (plan P-0001 step 138; OWN-9, G-09): a repository inventory read from the files at the reviewed commit, and
the domains its rules propose. The rules are data (domain_rules, seeded from the table in references/domains.md), the
same commit gives the same bytes, and each review_domains row scan writes carries the rule that proposed it."""
import json
import re
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import new_ledger, ok, refused

REFS = fixtures.REPO_ROOT / "references"

PLAIN = {
    "README.md": "# Demo\n\nThe handler must refuse an empty request.\n",
    "src/app.py": "import datetime\n\n\ndef handler(request):\n    return request or datetime.date.today()\n",
    "src/util.py": "def helper():\n    return 1\n",
    "tests/test_app.py": "import unittest\n",
    "requirements.txt": "flask==3.0\nrequests>=2\n",
}
PAGES = dict(PLAIN, **{
    "web/index.html": "<!doctype html><title>Demo</title><form><input name=q></form>\n",
    "web/settings.html": "<!doctype html><title>Settings</title>\n",
    "web/site.css": "body { color: #111; }\n",
    "db/migrations/0001-init.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n",
    ".github/workflows/ci.yml": "name: ci\non: push\n",
    "package.json": json.dumps({"dependencies": {"react": "18"}, "devDependencies": {"jest": "29"}}),
    "package-lock.json": "{}\n",
})


def scanned(test, files, working_tree=None):
    """A project with the files committed and review R-0001 in planning. Returns (L, repo)."""
    repo = fixtures.ScratchRepo()
    for rel, text in files.items():
        repo.write(rel, text)
    repo.commit("first")
    for rel, text in (working_tree or {}).items():
        repo.write(rel, text)
    L = new_ledger(repo)
    test.addCleanup(L.close)
    ok(L, "add-repo", "demo", ".")
    ok(L, "add-actor", "manager", "review manager")
    ok(L, "new-review", "--title", "First review", "--scope", "full", "--actor", "A-0001")
    return L, repo


def domains(L):
    L.commit()
    return {r["domain_code"]: r for r in L.all("SELECT * FROM review_domains")}


class Scan(unittest.TestCase):
    def test_scan_is_deterministic(self):
        L, repo = scanned(self, PAGES)
        first = L.run_ok("scan", "--review", "R-0001", "--json").stdout
        again = L.run_ok("scan", "--review", "R-0001", "--json").stdout
        self.assertEqual(first, again)
        self.assertEqual(L.run_ok("scan", "--review", "R-0001").stdout, L.run_ok("scan", "--review", "R-0001").stdout)
        out = json.loads(first)
        self.assertNotRegex(first, r"20\d\d-\d\d-\d\dT")       # no time in the text
        self.assertNotIn(str(L.root), first)                    # no path of this machine
        inv = out["repositories"][0]["inventory"]
        self.assertEqual(out["repositories"][0]["commit"], repo.head())
        self.assertEqual(inv["languages"]["python"]["files"], 3)
        self.assertEqual(inv["manifests"], ["package.json", "requirements.txt"])
        self.assertEqual(inv["frameworks"], ["flask", "jest", "react"])
        self.assertEqual(inv["tests"]["files"], 1)
        self.assertEqual(inv["ci"], [".github/workflows/ci.yml"])
        self.assertEqual(inv["ui"]["pages"], ["web/index.html", "web/settings.html"])
        self.assertEqual(inv["database"]["migrations"], 1)
        self.assertEqual(inv["dependencies"], {"count": 4, "lock_files": ["package-lock.json"]})
        self.assertEqual(sorted(inv["areas"]), ["(root)", ".github", "db", "src", "tests", "web"])
        with self.subTest("files changed after the commit are not read"):
            repo.write("web/extra.html", "<p>new</p>\n")
            repo.write("src/app.py", "changed\n")
            self.assertEqual(L.run_ok("scan", "--review", "R-0001", "--json").stdout, first)
        with self.subTest("another clone of the same commit gives the same bytes"):
            L2, _ = scanned(self, PAGES)
            self.assertEqual(L2.run_ok("scan", "--review", "R-0001", "--json").stdout, first)

    def test_scan_rules(self):
        with_pages, _ = scanned(self, PAGES)
        plain, _ = scanned(self, PLAIN)
        a = {d["code"]: d for d in json.loads(with_pages.run_ok("scan", "--review", "R-0001", "--json").stdout)["domains"]}
        b = {d["code"]: d for d in json.loads(plain.run_ok("scan", "--review", "R-0001", "--json").stdout)["domains"]}
        for code in ("ui", "walkthroughs", "database", "operations"):
            self.assertEqual((a[code]["decision"], b[code]["decision"]), ("included", "skipped"), code)
        for code in ("tests", "security", "logging", "dependencies", "docs-developer", "abstraction", "spec",
                     "time-concurrency"):
            self.assertEqual((a[code]["decision"], b[code]["decision"]), ("included", "included"), code)
        self.assertEqual(a["ui"]["rule"], "ui_files >= 1")
        self.assertIn("ui_files = 3", a["ui"]["reason"])
        self.assertIn("not met", b["ui"]["reason"])
        self.assertIn("ui_files = 0", b["ui"]["reason"])
        self.assertEqual(len(a), 27)
        text = plain.run_ok("scan", "--review", "R-0001").stdout
        self.assertRegex(text, r"(?m)^  ui +skipped +ui_files >= 1 not met: ui_files = 0$")

    def test_scan_proposals_recorded(self):
        L, _ = scanned(self, PAGES)
        out = L.run_ok("scan", "--review", "R-0001", "--actor", "A-0001").stdout
        got = domains(L)
        self.assertEqual(len(got), 27)
        self.assertEqual((got["ui"]["decision"], got["ui"]["proposed_by_rule"]), ("included", "ui_files >= 1"))
        self.assertIn("ui_files = 3", got["ui"]["reason"])
        self.assertEqual((got["localization"]["decision"], got["localization"]["proposed_by_rule"]), ("skipped", None))
        self.assertIn("not met", got["localization"]["reason"])
        self.assertIn("recorded on R-0001", out)
        with self.subTest("the agent changes a proposal only with a reason; a rerun keeps the change"):
            self.assertIn("give --reason", refused(self, L, "review-domain", "R-0001", "ui", "skip"))
            ok(L, "review-domain", "R-0001", "ui", "skip", "--reason", "the pages are generated samples")
            ok(L, "review-domain", "R-0001", "localization", "include", "--reason", "the owner sells abroad",
               "--owner-change", "added-by-owner")
            L.run_ok("scan", "--review", "R-0001")
            got = domains(L)
            self.assertEqual((got["ui"]["decision"], got["ui"]["reason"], got["ui"]["proposed_by_rule"]),
                             ("skipped", "the pages are generated samples", "ui_files >= 1"))
            self.assertEqual((got["localization"]["decision"], got["localization"]["proposed_by_rule"]),
                             ("included", None))
    def test_an_open_issue_proposes_its_domain(self):
        L, _ = scanned(self, PLAIN)
        rid = L.one("SELECT id FROM reviews WHERE code='R-0001'")["id"]
        L.issue(first_seen_review_id=rid, domain_code="notifications")
        L.run_ok("scan", "--review", "R-0001")
        row = domains(L)["notifications"]
        self.assertEqual((row["decision"], row["proposed_by_rule"]), ("included", "open_issues >= 1"))
        self.assertIn("ISS-0001", row["reason"])

    def test_scan_after_approval_writes_nothing(self):
        L, _ = scanned(self, PLAIN)
        ok(L, "review-domain", "R-0001", "spec", "include", "--reason", "the rules")
        ok(L, "review-status", "R-0001", "awaiting-approval", "--note", "proposed")
        ok(L, "review-status", "R-0001", "approved", "--note", "approved")
        out = L.run_ok("scan", "--review", "R-0001").stdout
        self.assertIn("R-0001 is approved: its domains are settled, nothing was recorded", out)
        self.assertEqual(set(domains(L)), {"spec"})

    def test_scan_refusals(self):
        L, repo = scanned(self, PLAIN)
        self.assertIn("no review R-0007", refused(self, L, "scan", "--review", "R-0007"))
        p = L.run("scan")
        self.assertNotEqual(p.returncode, 0)

    def test_new_review_takes_no_domain_flags(self):
        L, _ = scanned(self, PLAIN)
        for flag in ("--domain", "--skip"):
            p = L.run("new-review", "--title", "X", "--scope", "full", flag, "spec=why")
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("unrecognized arguments", p.stderr)
        self.assertEqual(ok(L, "next-code", "R"), ["R-0002"])


class RulesAreData(unittest.TestCase):
    def test_rules_equal_the_catalog_table(self):
        """references/domains.md's 'Scan rules' table and the seeded domain_rules rows are the same."""
        doc = (REFS / "domains.md").read_text(encoding="utf-8")
        start = doc.index("## Scan rules")
        table = {}
        for line in doc[start:].split("\n## ")[0].split("\n"):
            m = re.match(r"\| `([a-z-]+)` \| `([^`]+)` \|$", line)
            if m:
                table[m.group(1)] = m.group(2)
        with fixtures.Ledger(fixtures.temp_dir("rules-")) as L:
            seeded = {r["domain_code"]: r["rule"] for r in L.all("SELECT * FROM domain_rules")}
            catalog = {r["code"] for r in L.all("SELECT code FROM domains")}
        self.assertEqual(table, seeded)
        self.assertEqual(set(seeded), catalog, "every catalog domain has a rule")

    def test_every_rule_uses_known_facts(self):
        L, _ = scanned(self, PAGES)
        facts = json.loads(L.run_ok("scan", "--review", "R-0001", "--json").stdout)["facts"]
        for r in L.all("SELECT * FROM domain_rules"):
            for name in re.findall(r"[a-z][a-z0-9_]*(?= >=)", r["rule"]):
                self.assertIn(name, facts, f"{r['domain_code']}: {r['rule']}")
        doc = (REFS / "domains.md").read_text(encoding="utf-8")
        for name in facts:
            self.assertIn(f"`{name}`", doc, f"references/domains.md does not say what the fact {name} counts")

    def test_a_project_can_change_a_rule(self):
        L, _ = scanned(self, PLAIN)
        L.con.execute("UPDATE domain_rules SET rule='test_files >= 1' WHERE domain_code='test-quality'")
        out = {d["code"]: d for d in json.loads(L.run_ok("scan", "--review", "R-0001", "--json").stdout)["domains"]}
        self.assertEqual(out["test-quality"]["decision"], "included")
        L.con.execute("UPDATE domain_rules SET rule='nonsense' WHERE domain_code='test-quality'")
        self.assertIn("test-quality", refused(self, L, "scan", "--review", "R-0001"))


if __name__ == "__main__":
    unittest.main()
