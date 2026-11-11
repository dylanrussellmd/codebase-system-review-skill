"""Ledger text printed by the CLI cannot drive the terminal (plan P-0001 step 118: SEC-L7).

A ledger can be written by someone else (a contributor's branch, an earlier review, a merged copy), so every command
prints stored text through one sanitizer, terminal_safe(): C0 controls other than newline and tab, DEL and the C1
controls (U+0080-U+009F, which include the one-byte CSI U+009B) are written as \\u00XX escapes. Newlines and tabs stay.
"""
import json
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import ok, project

# Clear the screen, set the window title, and a C1 CSI (U+009B) that starts a colour sequence on its own.
EVIL = "evil \x1b[2J\x1b]0;pwned\x07 \x9b31m \x7f\r end"
SAFE = "evil \\u001b[2J\\u001b]0;pwned\\u0007 \\u009b31m \\u007f\\u000d end"
# What must appear escaped in every output (query writes \r as \\r, Markdown cells drop it, show splits lines at it).
MARKS = ("\\u001b[2J", "\\u001b]0;pwned\\u0007", "\\u009b31m", "\\u007f")


def has_controls(text):
    return any((ord(c) < 32 and c not in "\n\t") or 0x7f <= ord(c) <= 0x9f for c in text)


class TerminalSafeTest(unittest.TestCase):
    def test_terminal_safe_escapes_controls_keeps_newline_and_tab(self):
        ts = fixtures.load_ledger_module().terminal_safe
        self.assertEqual(ts(EVIL), SAFE)
        self.assertEqual(ts("a\tb\nc é ✓"), "a\tb\nc é ✓")
        self.assertEqual(ts(""), "")


class CliOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One project for the class (each test adds its own rows; none depends on another's).
        class _Cleanup:
            addCleanup = cls.addClassCleanup
        cls.repo, cls.L, cls.sha = project(_Cleanup, files={"src/app.py": f"line 1\nprint('{EVIL}')\nline 3\n"})

    def assert_safe(self, p, expect_in="stdout"):
        for name in ("stdout", "stderr"):
            text = getattr(p, name)
            self.assertFalse(has_controls(text), f"raw control character in {name}: {text!r}")
        for m in MARKS:
            self.assertIn(m, getattr(p, expect_in))

    def test_cli_gates_strips_control_chars(self):
        ok(self.L, "open-gate", "confirm", "--title", EVIL, "--question", "q?", "--actor", "A-0001")
        self.assert_safe(self.L.run_ok("gates"))

    def test_show_escapes_cited_code(self):
        fid = self.L.finding(self.L.id_of("reviews", "R-0001"))
        loc = self.L.location(fid, "src/app.py", 2)
        self.assert_safe(self.L.run_ok("show", loc))

    def test_check_escapes_listed_rows(self):
        self.L.finding(self.L.id_of("reviews", "R-0001"), summary=EVIL)
        p = self.L.run("check", "--quiet")
        self.assertEqual(p.returncode, 1, p.stdout)  # the finding is unmatched
        self.assertFalse(has_controls(p.stdout), p.stdout)
        self.assertIn("\\u009b31m", p.stdout)  # json.dumps(ensure_ascii=False) alone lets C1 through

    def test_query_tsv_and_json_are_safe(self):
        ok(self.L, "open-gate", "confirm", "--title", EVIL, "--question", "q?", "--actor", "A-0001")
        self.assert_safe(self.L.run_ok("query", "SELECT title FROM gates"))
        p = self.L.run_ok("query", "--json", "SELECT title FROM gates")
        self.assertFalse(has_controls(p.stdout), p.stdout)
        self.assertEqual(json.loads(p.stdout)["rows"][0][0], EVIL)  # still valid JSON with the same value

    def test_progress_md_printed_and_written_are_safe(self):
        pid = self.L.plan(reviews=(1,))
        self.L.step(pid, self.L.phase(pid, 1), 1, title=EVIL, no_findings_reason="test")
        code = self.L.scalar("SELECT code FROM plans WHERE id=?", (pid,))
        self.assert_safe(self.L.run_ok("progress-md", code))
        out = self.L.root / "PROGRESS.md"
        self.L.run_ok("progress-md", code, "-o", out)
        text = out.read_text(encoding="utf-8")
        self.assertFalse(has_controls(text), text)
        for m in MARKS:
            self.assertIn(m, text)

    def test_refusal_on_stderr_is_safe(self):
        p = self.L.run("issue-status", "ISS-" + EVIL, "fixed", "--note", "x")
        self.assertEqual(p.returncode, 1)
        self.assert_safe(p, "stderr")


class ExitMessageTest(unittest.TestCase):
    def test_exit_message_is_safe(self):
        # sys.exit("message") is printed by the interpreter after main() returns: it is sanitized too.
        folder = fixtures.temp_dir() / ("not-git " + EVIL.replace("/", ""))
        folder.mkdir()
        p = fixtures.run_ledger("init", cwd=folder)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("is not in a git repository", p.stderr)
        for name in ("stdout", "stderr"):
            self.assertFalse(has_controls(getattr(p, name)), getattr(p, name))
        self.assertIn("\\u001b[2J", p.stderr)


if __name__ == "__main__":
    unittest.main()
