"""Secret-looking values in cited lines are masked in fill-refs excerpts (plan P-0001 step 51, SEC-M7).

The excerpt keeps the line's shape with the value replaced by [redacted]; excerpt_redacted is 1; excerpt_sha256 is
still the hash of the real lines, so show's excerpt check keeps working.
"""
import hashlib
import unittest

from dev.tests import fixtures
from dev.tests._core_helpers import project, quiet, ref

KEY = "abcd1234efgh5678ijkl9012"
FILES = {
    "src/settings.py": f'DEBUG = True\nAPI_KEY = "{KEY}"\nNAME = "demo"\n',
    "deploy/.env": f"export DB_PASSWORD={KEY}\nPORT=8080\n",
    "config.json": f'{{\n  "client_secret": "{KEY}",\n  "region": "eu"\n}}\n',
    "keys/id.pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "A" * 40 + "\nQ" * 3 + "\n-----END RSA PRIVATE KEY-----\n",
    "src/aws.py": 'ID = "AKIAABCDEFGHIJKLMNOP"\ngh = "ghp_' + "a" * 36 + '"\n',
    "src/plain.py": 'def login(user, password):\n    token = make_token(user)\n    return check(password)\n',
}


class MaskSecrets(unittest.TestCase):

    def test_fill_refs_masks_secrets(self):
        L, sha, rid, _ = project(FILES, test=self)
        cases = [("src/settings.py", 1, 3), ("deploy/.env", 1, 2), ("config.json", 1, 4), ("keys/id.pem", 1, 6),
                 ("src/aws.py", 1, 2)]
        ids = [ref(L, rid, p, a, b) for p, a, b in cases]
        failed, out = quiet(L.module.fill_refs, L.root, L.con, None)
        self.assertEqual(failed, 0, out)
        for (path, a, b), lid in zip(cases, ids):
            with self.subTest(path=path):
                r = L.one("SELECT excerpt, excerpt_sha256, excerpt_redacted FROM finding_locations WHERE id=?", (lid,))
                real = "\n".join(FILES[path].splitlines()[a - 1:b])
                self.assertIn("[redacted]", r["excerpt"])
                self.assertNotIn(KEY, r["excerpt"])
                self.assertNotIn("AKIAABCDEFGHIJKLMNOP", r["excerpt"])
                self.assertNotIn("ghp_aaaa", r["excerpt"])
                self.assertNotIn("MIIEow", r["excerpt"])
                self.assertEqual(r["excerpt_redacted"], 1)
                self.assertEqual(r["excerpt_sha256"], hashlib.sha256(real.encode()).hexdigest())
                self.assertEqual(len(r["excerpt"].split("\n")), b - a + 1, "masking must keep the line count")
        settings = L.scalar("SELECT excerpt FROM finding_locations WHERE id=?", (ids[0],))
        self.assertEqual(settings, 'DEBUG = True\nAPI_KEY = "[redacted]"\nNAME = "demo"')
        # show compares the real lines with the stored hash, so the masked excerpt still "matches".
        s, _ = quiet(L.module.source, L.root, L.con, ids[0])
        self.assertTrue(s["excerpt_matches"])
        self.assertTrue(s["excerpt_redacted"])

    def test_fill_refs_plain_line_unmasked(self):
        L, sha, rid, _ = project(FILES, test=self)
        lid = ref(L, rid, "src/plain.py", 1, 3)
        quiet(L.module.fill_refs, L.root, L.con, None)
        r = L.one("SELECT excerpt, excerpt_redacted FROM finding_locations WHERE id=?", (lid,))
        self.assertEqual(r["excerpt"], FILES["src/plain.py"].rstrip("\n"))
        self.assertEqual(r["excerpt_redacted"], 0)

    def test_mask_secrets_shapes(self):
        m = fixtures.load_ledger_module()
        for text in ('password: "hunter2hunter2"', "SLACK_TOKEN=xoxb-1234567890-abcdef", "key = 'sk-" + "x" * 30 + "'",
                     "stripe sk_live_" + "Z" * 24, "AIza" + "b" * 35):
            with self.subTest(text=text):
                masked, changed = m.mask_secrets(text)
                self.assertTrue(changed, masked)
                self.assertIn("[redacted]", masked)
        for text in ("token = get_token()", "password = request.form['password']", "x = 1"):
            with self.subTest(text=text):
                self.assertEqual(m.mask_secrets(text), (text, False))


if __name__ == "__main__":
    unittest.main()
