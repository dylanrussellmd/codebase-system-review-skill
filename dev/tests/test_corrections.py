"""The ledger's correction commands (plan P-0002 step 8: WALK-M9, LOG-M3, DU-checklist-1, OWN-R3-11): rematch,
correct-location, supersede-check, correct-issue, withdraw-gate and reopen-gate. Each writes one event with its actor
and reason, and refuses a no-op and a missing reason."""
import unittest

from dev.tests import fixtures
from dev.tests._p5_writes_helpers import counts, index_row, ok, project, refused

ACT = ("--actor", "A-0001")
FILES = {"src/app.py": "a\nb\nc\ndef go():\n  pass\n", "src/other.py": "x = 1\ny = 2\nz = 3\n", "README.md": "# hi\n"}


def tsv(name, *rows):
    f = fixtures.temp_dir("tsv-") / name
    f.write_text("".join(r + "\n" for r in rows))
    return f


def last_event(L, event):
    return L.one("""SELECT e.*, a.name AS actor FROM events e LEFT JOIN actors a ON a.id = e.actor_id
                    WHERE e.event=? ORDER BY e.id DESC LIMIT 1""", (event,))


class Points(unittest.TestCase):
    """R-0001 with three points: SPEC-M1 and SPEC-M2 matched to new issues ISS-0001 and ISS-0002, SPEC-L1 to
    ISS-0003."""

    def setUp(self):
        self.repo, self.L, self.sha = project(self, files=FILES)
        ok(self.L, "load-index", "R-0001", "spec-A", tsv(
            "points.tsv", index_row("SPEC-M1", before="It accepts junk."),
            index_row("SPEC-M2", locations="demo:src/app.py:4#go", before="go() hangs."),
            index_row("SPEC-L1", sev="L", locations="demo:src")))
        for p in ("SPEC-M1", "SPEC-M2", "SPEC-L1"):
            ok(self.L, "match", "R-0001", p, "new")


class Rematch(Points):
    def test_rematch_command_writes_event(self):
        L = self.L
        before = L.scalar("SELECT count(*) FROM events WHERE actor_id IS NOT NULL AND event='rematched'")
        self.assertEqual(before, 0)
        out = ok(L, "rematch", "R-0001:SPEC-M2", "ISS-0001", "--type", "still-present", "--confidence", "certain",
                 "--reason", "the same parser defect", *ACT)
        self.assertEqual(out[0], "R-0001:SPEC-M2: ISS-0002 -> ISS-0001 still-present")
        f = L.one("SELECT i.code, f.match_type, f.match_confidence, f.match_rationale FROM findings f "
                  "JOIN issues i ON i.id = f.issue_id WHERE f.point_id='SPEC-M2'")
        self.assertEqual(f, {"code": "ISS-0001", "match_type": "still-present", "match_confidence": "certain",
                             "match_rationale": "the same parser defect"})
        e = last_event(L, "rematched")
        self.assertEqual((e["entity_type"], e["from_value"], e["to_value"], e["actor"], e["note"]),
                         ("finding", "ISS-0002", "ISS-0001", "review manager", "the same parser defect"))
        self.assertEqual(L.scalar("SELECT count(*) FROM events WHERE event='rematched'"), 1)
        # the issue it left has no points now; the command says so
        self.assertTrue(any("ISS-0002 has no points left" in line for line in out), out)

    def test_rematch_refuses_without_reason_or_as_a_no_op(self):
        L = self.L
        before = counts(L, "events", "issues")
        self.assertIn("--reason", refused(self, L, "rematch", "R-0001:SPEC-M2", "ISS-0001", "--type", "still-present",
                                          *ACT))
        self.assertIn("--actor", refused(self, L, "rematch", "R-0001:SPEC-M2", "ISS-0001", "--type", "still-present",
                                         "--reason", "x"))
        self.assertIn("already matched to ISS-0002", refused(self, L, "rematch", "R-0001:SPEC-M2", "ISS-0002",
                                                             "--type", "new", "--reason", "x", *ACT))
        self.assertIn("no point SPEC-Q9", refused(self, L, "rematch", "R-0001:SPEC-Q9", "ISS-0001", "--type",
                                                  "still-present", "--reason", "x", *ACT))
        self.assertIn("REVIEW:POINT", refused(self, L, "rematch", "SPEC-M2", "ISS-0001", "--reason", "x", *ACT))
        self.assertEqual(counts(L, "events", "issues"), before)

    def test_rematch_several_points_to_one_new_issue(self):
        L = self.L
        out = ok(L, "rematch", "R-0001:SPEC-M2", "R-0001:SPEC-L1", "new", "--title", "Two points, one new problem",
                 "--relations", "related-to:ISS-0001", "--reason", "neither is ISS-0002's problem", *ACT)
        self.assertEqual(out[:2], ["R-0001:SPEC-M2: ISS-0002 -> ISS-0004 new",
                                   "R-0001:SPEC-L1: ISS-0003 -> ISS-0004 new"])
        self.assertEqual(L.scalar("SELECT title FROM issues WHERE code='ISS-0004'"), "Two points, one new problem")
        self.assertEqual(L.scalar("SELECT count(*) FROM findings WHERE issue_id=(SELECT id FROM issues "
                                  "WHERE code='ISS-0004')"), 2)
        self.assertEqual(L.scalar("SELECT count(*) FROM issue_relations WHERE relation='related-to'"), 1)
        self.assertEqual(L.scalar("SELECT count(*) FROM events WHERE event='rematched'"), 2)
        self.assertIn("--title", refused(self, L, "rematch", "R-0001:SPEC-M1", "new", "--reason", "x", *ACT))


class CorrectLocation(Points):
    def test_correct_location_writes_event(self):
        L = self.L
        out = ok(L, "correct-location", "R-0001:SPEC-L1", "--from", "demo:src", "--to", "demo:src/other.py:2-3",
                 "--reason", "the point is about other.py, not the folder", *ACT)
        self.assertEqual(out, ["R-0001:SPEC-L1: src -> src/other.py:2-3"])
        loc = L.one("""SELECT path, line_start, line_end, whole_file, excerpt, commit_sha, blob_sha FROM finding_locations
                       WHERE finding_id=(SELECT id FROM findings WHERE point_id='SPEC-L1')""")
        self.assertEqual((loc["path"], loc["line_start"], loc["line_end"], loc["whole_file"], loc["excerpt"],
                          loc["commit_sha"]), ("src/other.py", 2, 3, 0, "y = 2\nz = 3", self.sha))
        self.assertIsNotNone(loc["blob_sha"])
        e = last_event(L, "location-corrected")
        self.assertEqual((e["entity_type"], e["from_value"], e["to_value"], e["actor"], e["note"]),
                         ("finding", "src", "src/other.py:2-3", "review manager",
                          "the point is about other.py, not the folder"))
        self.assertEqual(L.scalar("SELECT count(*) FROM events WHERE event='location-corrected'"), 1)

    def test_correct_location_refusals(self):
        L = self.L
        before = counts(L, "events", "finding_locations")
        self.assertIn("--reason", refused(self, L, "correct-location", "R-0001:SPEC-L1", "--from", "demo:src",
                                          "--to", "demo:src/other.py:2-3", *ACT))
        self.assertIn("--actor", refused(self, L, "correct-location", "R-0001:SPEC-L1", "--from", "demo:src",
                                         "--to", "demo:src/other.py:2-3", "--reason", "x"))
        self.assertIn("no location src/nope.py", refused(self, L, "correct-location", "R-0001:SPEC-L1", "--from",
                                                         "demo:src/nope.py", "--to", "demo:src/other.py:2",
                                                         "--reason", "x", *ACT))
        self.assertIn("past its end", refused(self, L, "correct-location", "R-0001:SPEC-L1", "--from", "demo:src",
                                              "--to", "demo:src/other.py:2-9", "--reason", "x", *ACT))
        self.assertIn("already", refused(self, L, "correct-location", "R-0001:SPEC-M1", "--from",
                                         "demo:src/app.py:2-3", "--to", "demo:src/app.py:2-3", "--reason", "x", *ACT))
        self.assertEqual(counts(L, "events", "finding_locations"), before)

    def test_location_change_by_sql_needs_actor_and_note(self):
        L = self.L
        with self.assertRaisesRegex(Exception, "append-only"):
            L.x("UPDATE finding_locations SET path='src/other.py' WHERE path='src'")


class SupersedeCheck(unittest.TestCase):
    def setUp(self):
        self.repo, self.L, self.sha = project(self, files=FILES)
        L = self.L
        prior = L.review("R-0002", status="handed-off", commit_sha=self.sha)
        L.x("UPDATE reviews SET started_at='2026-01-01T00:00:00.000Z' WHERE id=?", (prior,))
        L.issue("ISS-0001", first_seen_review_id=prior, opened_at="2026-01-01T00:00:00.000Z")
        L.commit()
        ok(L, "load-index", "R-0001", "spec-A", tsv("points.tsv", index_row("SPEC-M1"), index_row("SPEC-M2")),
           tsv("known.tsv", "ISS-0001\tchanged\tline 2 moved\tSPEC-M2"))
        ok(L, "match", "R-0001", "SPEC-M1", "ISS-0001", "--type", "still-present")
        ok(L, "match", "R-0001", "SPEC-M2", "new")

    def test_supersede_check_writes_event(self):
        L = self.L
        status = L.scalar("SELECT status FROM issues WHERE code='ISS-0001'")
        out = ok(L, "supersede-check", "R-0001:ISS-0001", "--finding", "SPEC-M1", "--reason",
                 "SPEC-M2 is ISS-0002's point; SPEC-M1 is this issue's", *ACT)
        self.assertEqual(out, ["R-0001:ISS-0001: check corrected (finding SPEC-M2 -> SPEC-M1)"])
        c = L.one("SELECT outcome, evidence, finding_id, actor_id FROM review_issue_checks")
        self.assertEqual((c["outcome"], c["evidence"], c["finding_id"]),
                         ("changed", "line 2 moved", L.scalar("SELECT id FROM findings WHERE point_id='SPEC-M1'")))
        e = last_event(L, "check-corrected")
        self.assertEqual((e["entity_type"], e["actor"], e["note"]),
                         ("issue", "review manager", "SPEC-M2 is ISS-0002's point; SPEC-M1 is this issue's"))
        self.assertEqual((e["from_value"], e["to_value"]), ("changed: line 2 moved (SPEC-M2)",
                                                            "changed: line 2 moved (SPEC-M1)"))
        self.assertEqual(L.scalar("SELECT status FROM issues WHERE code='ISS-0001'"), status)
        self.assertEqual(L.scalar("SELECT count(*) FROM events WHERE event='check-corrected'"), 1)
        ok(L, "supersede-check", "R-0001:ISS-0001", "--finding", "none", "--evidence", "line 2 moved (SPEC-M1)",
           "--reason", "evidence only", *ACT)
        self.assertIsNone(L.scalar("SELECT finding_id FROM review_issue_checks"))

    def test_supersede_check_refusals(self):
        L = self.L
        before = counts(L, "events", "review_issue_checks")
        self.assertIn("--reason", refused(self, L, "supersede-check", "R-0001:ISS-0001", "--finding", "SPEC-M1",
                                          *ACT))
        self.assertIn("--actor", refused(self, L, "supersede-check", "R-0001:ISS-0001", "--finding", "SPEC-M1",
                                         "--reason", "x"))
        self.assertIn("changes nothing", refused(self, L, "supersede-check", "R-0001:ISS-0001", "--finding",
                                                 "SPEC-M2", "--reason", "x", *ACT))
        self.assertIn("no check of ISS-0002", refused(self, L, "supersede-check", "R-0001:ISS-0002", "--outcome",
                                                      "fixed", "--reason", "x", *ACT))
        self.assertIn("needs evidence", refused(self, L, "supersede-check", "R-0001:ISS-0001", "--evidence", "",
                                                "--reason", "x", *ACT))
        self.assertEqual(counts(L, "events", "review_issue_checks"), before)
        with self.assertRaisesRegex(Exception, "append-only"):
            L.x("UPDATE review_issue_checks SET finding_id=NULL")


class CorrectIssue(Points):
    def test_correct_issue_restores_current_state(self):
        L = self.L
        ok(L, "rematch", "R-0001:SPEC-M2", "ISS-0001", "--type", "still-present", "--reason", "same defect", *ACT)
        self.assertEqual(L.scalar("SELECT current_state FROM issues WHERE code='ISS-0001'"), "go() hangs.")
        out = ok(L, "rematch", "R-0001:SPEC-M2", "ISS-0002", "--type", "new", "--reason", "it was right", *ACT)
        self.assertTrue(any("ISS-0001's current state" in line and "correct-issue ISS-0001" in line for line in out),
                        out)
        out = ok(L, "correct-issue", "ISS-0001", "--current", "It accepts junk.", "--reason",
                 "SPEC-M2's text overwrote it", *ACT)
        self.assertEqual(out, ["ISS-0001: current state corrected"])
        e = last_event(L, "current-state-changed")
        self.assertEqual((e["from_value"], e["to_value"], e["actor"], e["note"]),
                         ("go() hangs.", "It accepts junk.", "review manager", "SPEC-M2's text overwrote it"))
        self.assertIn("changes nothing", refused(self, L, "correct-issue", "ISS-0001", "--current",
                                                 "It accepts junk.", "--reason", "x", *ACT))
        self.assertIn("--reason", refused(self, L, "correct-issue", "ISS-0001", "--current", "y", *ACT))
        self.assertIn("--actor", refused(self, L, "correct-issue", "ISS-0001", "--current", "y", "--reason", "x"))


class Gates(unittest.TestCase):
    def setUp(self):
        self.repo, self.L, _ = project(self, approved=False)
        ok(self.L, "add-actor", "owner", "the owner")
        ok(self.L, "open-gate", "decision", "--title", "Keep?", "--question", "Keep the column?", "--option",
           "keep=Keep it", "--option", "drop=Drop it", "--actor", "A-0001")

    def test_withdraw_gate_needs_note_and_actor(self):
        L = self.L
        before = counts(L, "events")
        self.assertIn("--note", refused(self, L, "withdraw-gate", "GATE-0001", "--actor", "A-0001"))
        self.assertIn("--actor", refused(self, L, "withdraw-gate", "GATE-0001", "--note", "not needed"))
        self.assertEqual(counts(L, "events"), before)
        self.assertEqual(L.scalar("SELECT status FROM gates"), "open")
        self.assertEqual(ok(L, "withdraw-gate", "GATE-0001", "--note", "asked by mistake", "--actor", "A-0001"),
                         ["GATE-0001: open -> withdrawn"])
        self.assertEqual(L.scalar("SELECT status FROM gates"), "withdrawn")
        e = last_event(L, "status-changed")
        self.assertEqual((e["entity_type"], e["from_value"], e["to_value"], e["actor"], e["note"]),
                         ("gate", "open", "withdrawn", "review manager", "asked by mistake"))
        self.assertIn("is withdrawn", refused(self, L, "withdraw-gate", "GATE-0001", "--note", "again",
                                              "--actor", "A-0001"))
        # by SQL: refused without an actor and a note
        ok(L, "open-gate", "confirm", "--title", "T", "--question", "Q")
        for sets in ("status='withdrawn'", "status='withdrawn', change_actor_id=1",
                     "status='withdrawn', change_note='why'"):
            with self.assertRaisesRegex(Exception, "withdraw"):
                L.x(f"UPDATE gates SET {sets} WHERE code='GATE-0002'")
            L.con.rollback()
        L.x("UPDATE gates SET status='withdrawn', change_actor_id=1, change_note='why' WHERE code='GATE-0002'")
        L.commit()
        self.assertEqual(L.one("SELECT status, change_actor_id, change_note FROM gates WHERE code='GATE-0002'"),
                         {"status": "withdrawn", "change_actor_id": None, "change_note": None})

    def test_withdraw_gate_command(self):
        L = self.L
        ok(L, "answer-gate", "GATE-0001", "--value", "keep")
        self.assertEqual(ok(L, "withdraw-gate", "GATE-0001", "--note", "overtaken", "--actor", "A-0001"),
                         ["GATE-0001: answered -> withdrawn"])
        ok(L, "open-gate", "confirm", "--title", "T", "--question", "Q")
        ok(L, "answer-gate", "GATE-0002", "--text", "yes")
        ok(L, "apply-gate", "GATE-0002", "--note", "done")
        self.assertIn("applied", refused(self, L, "withdraw-gate", "GATE-0002", "--note", "x", "--actor", "A-0001"))

    def test_reopen_keeps_old_answer_in_events(self):
        L = self.L
        self.assertIn("is open", refused(self, L, "reopen-gate", "GATE-0001", "--note", "x", "--actor", "A-0001"))
        ok(L, "answer-gate", "GATE-0001", "--value", "keep", "--text", "for now")
        before = L.scalar("SELECT count(*) FROM events")
        self.assertIn("--note", refused(self, L, "reopen-gate", "GATE-0001", "--actor", "A-0001"))
        self.assertIn("--actor", refused(self, L, "reopen-gate", "GATE-0001", "--note", "x"))
        self.assertEqual(ok(L, "reopen-gate", "GATE-0001", "--note", "the owner chose the wrong option",
                            "--actor", "A-0001"), ["GATE-0001: answered -> open (the answer was keep: for now)"])
        g = L.one("SELECT status, answer_value, answer_text, answered_by, answered_via, answered_at FROM gates")
        self.assertEqual(g, {"status": "open", "answer_value": None, "answer_text": None, "answered_by": None,
                             "answered_via": None, "answered_at": None})
        self.assertEqual(L.scalar("SELECT count(*) FROM events") - before, 1)
        e = last_event(L, "answer-cleared")
        self.assertEqual((e["entity_type"], e["from_value"], e["actor"]), ("gate", "keep", "review manager"))
        self.assertIn("keep: for now", e["note"])
        self.assertIn("the owner chose the wrong option", e["note"])
        ok(L, "answer-gate", "GATE-0001", "--value", "drop")
        self.assertEqual(L.scalar("SELECT answer_value FROM gates"), "drop")
        self.assertEqual(L.scalar("SELECT count(*) FROM v_history_gaps"), 0)


class Upgrade(unittest.TestCase):
    def test_schema_upgrade_from_21(self):
        """A schema-21 ledger gains the correction columns: after migrate a gate is withdrawn only with who and why,
        and withdraw-gate does it."""
        import sqlite3
        from dev.tests import _migrate_helpers as M
        project, db = M.restore(21)
        p = fixtures.run_ledger("--root", project, "migrate")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0022-corrections", p.stdout)
        con = sqlite3.connect(str(db))
        self.addCleanup(con.close)
        for table in ("gates", "finding_locations", "review_issue_checks"):
            self.assertIn("change_note", [r[1] for r in con.execute(f"PRAGMA table_info({table})")])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "withdraw"):
            con.execute("UPDATE gates SET status='withdrawn' WHERE code='GATE-0001'")
        con.rollback()
        p = fixtures.run_ledger("--root", project, "withdraw-gate", "GATE-0001", "--note", "settled", "--actor",
                                "A-0002")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "GATE-0001: open -> withdrawn"), p.stderr)
        self.assertEqual(con.execute("SELECT label FROM enum_labels WHERE enum='event' AND value='rematched'")
                         .fetchone()[0], "A point was moved to the right issue")


if __name__ == "__main__":
    unittest.main()
