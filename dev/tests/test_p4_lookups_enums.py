"""Plan P-0001 step 95 (decision D-0009, schema 6): the ledger's words live in seeded lookup tables. issue_statuses
(with active / resolved / fixed / reopens / briefed flags), severities, match_types (came_back), relation_types (closes,
inverse labels) and gate_kinds are referenced by foreign key; every other word list keeps its CHECK and has its labels
in enum_labels. The viewer's labels come from /api/overview, and views, triggers and the API pick statuses by flag.
"""
import re
import sqlite3
import unittest

from dev.tests import fixtures
from dev.tests.fixtures import Ledger, seed_demo, temp_dir

SCHEMA_SQL = (fixtures.REPO_ROOT / "references" / "schema.sql").read_text(encoding="utf-8")
PAGE_HTML = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")

# Lookup table -> the name its labels have in /api/overview labels.enums.
LOOKUPS = {"issue_statuses": "issue_status", "severities": "severity", "match_types": "match_type",
           "relation_types": "relation_type", "gate_kinds": "gate_kind"}

# Every column that keeps a CHECK list of words -> its enum in enum_labels. A new word column must be added here
# (and its words to enum_labels), or test_every_enum_value_has_a_label fails.
COLUMN_ENUMS = {
    ("ledger_checks", "tier"): "check_tier",
    ("actors", "kind"): "actor_kind",
    ("domains", "category"): "domain_category",
    ("reviews", "scope"): "review_scope",
    ("reviews", "status"): "review_status",
    ("review_domains", "decision"): "domain_decision",
    ("review_domains", "owner_change"): "owner_change",
    ("reviewer_runs", "status"): "run_status",
    ("requirements", "status"): "requirement_status",
    ("issues", "kind"): "issue_kind",
    ("findings", "kind"): "issue_kind",
    ("issue_relations", "confidence"): "confidence",
    ("findings", "match_confidence"): "confidence",
    ("findings", "verification"): "verification",
    ("finding_locations", "role"): "location_role",
    ("finding_locations", "excerpt_status"): "excerpt_status",
    ("finding_locations", "source"): "location_source",   # schema 8 (step 98)
    ("finding_requirements", "verdict"): "verdict",
    ("review_requirement_checks", "verdict"): "verdict",
    ("review_requirement_checks", "tested"): "tested",
    ("review_issue_checks", "outcome"): "check_outcome",
    ("decisions", "status"): "decision_status",
    ("gates", "status"): "gate_status",
    ("gates", "answered_via"): "answered_via",
    ("interview_answers", "answered_via"): "answered_via",   # schema 9 (step 129)
    ("plans", "status"): "plan_status",
    ("steps", "owner_need"): "owner_need",
    ("steps", "status"): "step_status",
    ("test_runs", "purpose"): "test_purpose",
    ("metric_defs", "better"): "metric_better",
    ("metrics", "method"): "metric_method",
    ("artifacts", "kind"): "artifact_kind",
    ("checklist_answers", "answer"): "checklist_answer",   # schema 14 (step 139)
    ("step_touches", "kind"): "touch_kind",                 # schema 16 (step 143)
    ("step_touches", "direction"): "touch_direction",
    ("step_touches", "source"): "touch_source",
    ("step_conflicts", "resolution"): "conflict_resolution",
    ("events", "entity_type"): "entity_type",
}
# Enums whose words no CHECK lists: events.event is free text, and its labels cover the names the triggers write.
FREE_TEXT_ENUMS = {"event"}
EMPTY_LABEL_OK = {("owner_need", "none")}  # the page shows nothing when a step needs nothing from the owner

ACTIVE_LIST = "('open','needs-decision','planned','in-progress','regressed')"
LIST_OF_WORDS = re.compile(r"\b([a-z_]+)\s+IN\s*\(\s*('(?:[^']*)'(?:\s*,\s*'[^']*')*)\s*\)", re.S)


def check_lists(con):
    """{(table, column): [values]} of every `column IN ('a','b',...)` inside a CHECK of a table's stored SQL."""
    out = {}
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
        for m in re.finditer(r"CHECK\s*\(", sql):
            depth, i = 1, m.end()
            while depth and i < len(sql):
                depth += {"(": 1, ")": -1}.get(sql[i], 0)
                i += 1
            for w in LIST_OF_WORDS.finditer(sql[m.end():i]):
                vals = re.findall(r"'([^']*)'", w.group(2))
                out.setdefault((name, w.group(1)), [])
                out[(name, w.group(1))] += [v for v in vals if v not in out[(name, w.group(1))]]
    return out


def route(L, path, qs=None):
    con = L.module.connect(L.db)
    try:
        return L.module.route(L.root, L.db, con, path, qs or {}, None)
    finally:
        con.close()


class Labels(unittest.TestCase):

    def test_every_enum_value_has_a_label(self):
        with Ledger(temp_dir("p4l-")) as L:
            enums = route(L, "/api/overview")["labels"]["enums"]
            lists = check_lists(L.con)
            with self.subTest("every CHECK word list is mapped to an enum"):
                self.assertEqual(sorted(set(lists) - set(COLUMN_ENUMS)), [])
                self.assertEqual(sorted(set(COLUMN_ENUMS) - set(lists)), [])
            for (table, col), values in sorted(lists.items()):
                enum = COLUMN_ENUMS[(table, col)]
                with self.subTest(column=f"{table}.{col}", enum=enum):
                    self.assertIn(enum, enums)
                    for v in values:
                        self.assertIn(v, enums[enum], f"{table}.{col} value {v!r} has no label")
                        if (enum, v) not in EMPTY_LABEL_OK:
                            self.assertTrue(enums[enum][v].strip(), f"{enum} {v!r} has an empty label")
            with self.subTest("enum_labels holds no word a CHECK does not allow"):
                allowed = {}
                for key, values in lists.items():
                    allowed.setdefault(COLUMN_ENUMS[key], set()).update(values)
                for enum, value in L.con.execute("SELECT enum, value FROM enum_labels"):
                    if enum in FREE_TEXT_ENUMS:
                        continue
                    self.assertIn(value, allowed.get(enum, set()), f"enum_labels {enum} {value!r} is not used")
            with self.subTest("every event name a trigger writes has a label"):
                schema = (fixtures.REPO_ROOT / "references" / "schema.sql").read_text(encoding="utf-8")
                written = set(re.findall(r"VALUES \('[a-z-]+', NEW\.id, '([a-z-]+)'", schema)) \
                    | set(re.findall(r"'issue', (?:NEW|OLD)\.issue_id, '([a-z-]+)'", schema))
                self.assertGreater(len(written), 15)
                self.assertEqual(written - set(enums["event"]), set())
            for table, enum in LOOKUPS.items():
                with self.subTest(lookup=table):
                    got = {r[0]: r[1] for r in L.con.execute(f"SELECT value, label FROM {table} ORDER BY position")}
                    self.assertTrue(got)
                    self.assertEqual(enums[enum], got)
                    self.assertTrue(all(lbl.strip() for lbl in got.values()))
            inverse = {r[0]: r[1] for r in L.con.execute("SELECT value, inverse_label FROM relation_types")}
            self.assertEqual(enums["relation_type_inverse"], inverse)
            self.assertTrue(all(lbl.strip() for lbl in inverse.values()))

    def test_lookup_columns_use_foreign_keys_not_checks(self):
        """The five lookup-backed columns have no CHECK list left (one source) and refuse unknown words by FK."""
        want = {("issues", "status"): "issue_statuses", ("issues", "severity"): "severities",
                ("findings", "severity"): "severities", ("findings", "match_type"): "match_types",
                ("issue_relations", "relation"): "relation_types", ("gates", "kind"): "gate_kinds"}
        with Ledger(temp_dir("p4l-")) as L:
            lists = check_lists(L.con)
            for (table, col), parent in want.items():
                with self.subTest(column=f"{table}.{col}"):
                    self.assertNotIn((table, col), lists)
                    fks = {(r[3], r[2], r[4]) for r in L.con.execute(f"PRAGMA foreign_key_list({table})")}
                    self.assertIn((col, parent, "value"), fks)
            r = L.review("R-0001")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                L.issue("ISS-0001", first_seen_review_id=r, status="closed")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                L.finding(r, "SEC-X1", severity="X")

    def test_page_labels_come_from_the_api(self):
        """page.html has no copy of the label words: L is built from OVERVIEW.labels.enums (fallback: severity
        letters and words())."""
        self.assertIn("labels.enums", PAGE_HTML)
        for word in ("Fixed and confirmed", "Approve fix plan",
                     "Waiting for your approval", "Absorbed"):
            self.assertNotIn(word, PAGE_HTML)
        self.assertIn('H:"High"', PAGE_HTML)  # the minimal fallback: a severity letter is never shown raw

    def test_labels_on_an_older_ledger_are_empty_not_an_error(self):
        """A read of a schema-5 ledger (no lookup tables) still answers /api/overview; the page falls back."""
        with Ledger(temp_dir("p4l-")) as L:
            fn = L.module.enum_labels
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            self.assertEqual(fn(con), {})


class Flags(unittest.TestCase):

    def test_active_statuses_from_flags(self):
        with Ledger(temp_dir("p4l-")) as L:
            flags = {r["value"]: dict(r) for r in L.all("SELECT * FROM issue_statuses")}
            self.assertEqual({v for v, f in flags.items() if f["active"]},
                             {"open", "needs-decision", "planned", "in-progress", "regressed"})
            self.assertEqual({v for v, f in flags.items() if f["resolved"]},
                             {"fixed", "verified", "accepted-risk", "wont-fix", "duplicate", "obsolete"})
            self.assertEqual({v for v, f in flags.items() if f["fixed"]}, {"fixed", "verified"})
            self.assertEqual({v for v, f in flags.items() if f["reopens"]}, {"obsolete", "wont-fix", "duplicate"})
            self.assertEqual({v for v, f in flags.items() if not f["briefed"]}, {"duplicate", "obsolete"})
            self.assertEqual({r[0] for r in L.x("SELECT value FROM match_types WHERE came_back = 1")},
                             {"recurrence", "regression"})
            self.assertEqual({r[0] for r in L.x("SELECT value FROM relation_types WHERE closes = 1")},
                             {"duplicate-of", "merged-into"})
            # A new active status, added as one row, is picked up everywhere the active set is used.
            L.insert("issue_statuses", value="triaged", label="Triaged", position=99, active=1, resolved=0, fixed=0,
                     reopens=0, briefed=1)
            L.insert("issue_statuses", value="parked", label="Parked", position=98, active=0, resolved=1, fixed=0,
                     reopens=1, briefed=1)
            r = L.review("R-0001")
            iid = L.issue("ISS-0001", first_seen_review_id=r, status="triaged", severity="H")
            L.issue("ISS-0002", first_seen_review_id=r, status="parked", severity="H")
            L.commit()
            self.assertEqual([x["code"] for x in L.all("SELECT code FROM v_open_issues")], ["ISS-0001"])
            self.assertEqual([x["code"] for x in route(L, "/api/issues", {"status": ["active"]})["rows"]], ["ISS-0001"])
            self.assertEqual(route(L, "/api/home")["open_by_severity"], [{"severity": "H", "n": 1}])
            # tr_issue_status sets resolved_at on entering a status flagged resolved
            L.update("issues", iid, status="parked", change_note="parked for now")
            L.commit()
            self.assertIsNotNone(L.scalar("SELECT resolved_at FROM issues WHERE id=?", (iid,)))
            # a recurrence reopens a status flagged reopens
            f = L.finding(L.review("R-0002"), "SPEC-M9")
            L.update("findings", f, issue_id=iid, match_type="recurrence")
            L.commit()
            self.assertEqual(L.scalar("SELECT status FROM issues WHERE id=?", (iid,)), "open")

    def test_no_literal_status_lists_left(self):
        """Views, triggers and the API use the flags, not copies of the status lists."""
        core = (fixtures.REPO_ROOT / "dev" / "core.py").read_text(encoding="utf-8")
        for name, text in (("schema.sql", SCHEMA_SQL), ("core.py", core)):
            flat = re.sub(r"\s+", "", text)
            with self.subTest(name):
                self.assertNotIn(ACTIVE_LIST, flat)
                self.assertNotIn("('fixed','accepted-risk','wont-fix','duplicate','obsolete')", flat)
                self.assertNotIn("('obsolete','wont-fix','duplicate')", flat)
                self.assertNotIn("match_typeIN('recurrence','regression')", flat)

    def test_demo_views_unchanged_by_flags(self):
        """The demo ledger's flag-driven views list what the literal lists listed."""
        with Ledger() as L:
            seed_demo(L)
            active = ("open", "needs-decision", "planned", "in-progress", "regressed")
            want = sorted(r[0] for r in L.x(
                f"SELECT code FROM issues WHERE status IN ({','.join('?' * len(active))})", active))
            self.assertEqual(sorted(r["code"] for r in L.all("SELECT code FROM v_open_issues")), want)


if __name__ == "__main__":
    unittest.main()
