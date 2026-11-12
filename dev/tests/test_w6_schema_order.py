"""Plan P-0001 step 216 (schema 17): a step may depend on any step of its plan, cycles refused, and v_step_order
gives the order to work in (a step waits behind every step it depends on; otherwise steps keep their numbers' order).

LATER_FIRST is the kind of ordering the deconfliction pass could not record before schema 17: a step added later (a
higher number) that must run before an earlier-numbered one.
"""
import shutil
import sqlite3
import subprocess
import unittest

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M

# (step, the step it waits for): each waits for a later-numbered step, as steps added after a plan was built do.
LATER_FIRST = ((2, 5), (3, 5), (4, 6), (1, 6))


def plan_with_steps(L, n=6):
    pid = L.plan()
    ph = L.phase(pid, 1)
    return pid, {k: L.step(pid, ph, k) for k in range(1, n + 1)}


def order(L, plan="P-0001"):
    return [r["step"] for r in L.all("SELECT step FROM v_step_order WHERE plan=? ORDER BY position", (plan,))]


class StepOrder(unittest.TestCase):
    def setUp(self):
        self.L = fixtures.Ledger(fixtures.temp_dir("w6-order-"))
        self.addCleanup(self.L.close)

    def test_dependency_on_later_step_allowed(self):
        L = self.L
        _, s = plan_with_steps(L)
        for a, b in LATER_FIRST:
            L.dependency(s[a], s[b])
        got = L.all("""SELECT a.number a, b.number b FROM step_dependencies d JOIN steps a ON a.id = d.step_id
                       JOIN steps b ON b.id = d.depends_on_step_id ORDER BY a.number, b.number""")
        self.assertEqual(sorted((r["a"], r["b"]) for r in got), sorted(LATER_FIRST))
        L.x("UPDATE steps SET number = 60 WHERE id = ?", (s[6],))   # renumbering keeps every dependency
        self.assertEqual(L.scalar("SELECT count(*) FROM step_dependencies"), len(LATER_FIRST))

    def test_dependency_cycle_refused(self):
        L = self.L
        _, s = plan_with_steps(L)
        L.dependency(s[2], s[5])
        L.dependency(s[5], s[4])
        for a, b, why in ((s[4], s[2], "cycle"), (s[5], s[2], "cycle"), (s[3], s[3], "same plan")):
            with self.subTest(a=a, b=b), self.assertRaises(sqlite3.IntegrityError) as cm:
                L.x("INSERT INTO step_dependencies VALUES (?, ?)", (a, b))
            self.assertIn(why, str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError) as cm:   # an edit that closes a loop
            L.x("UPDATE step_dependencies SET depends_on_step_id = ? WHERE step_id = ?", (s[2], s[5]))
        self.assertIn("cycle", str(cm.exception))
        L.x("UPDATE step_dependencies SET depends_on_step_id = ? WHERE step_id = ?", (s[1], s[5]))  # no loop
        other = L.plan()
        o1 = L.step(other, L.phase(other, 1), 1)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            L.x("INSERT INTO step_dependencies VALUES (?, ?)", (s[1], o1))
        self.assertIn("same plan", str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError) as cm:   # moving a step with dependencies to another plan
            L.x("UPDATE steps SET plan_id = ?, phase_id = (SELECT id FROM phases WHERE plan_id = ?) WHERE id = ?",
                (other, other, s[5]))
        self.assertIn("same plan", str(cm.exception))

    def test_step_order_respects_dependencies(self):
        L = self.L
        _, s = plan_with_steps(L)
        self.assertEqual(order(L), [1, 2, 3, 4, 5, 6])          # no dependency: the numbers' order
        for a, b in LATER_FIRST:
            L.dependency(s[a], s[b])
        got = order(L)
        self.assertEqual(got, [5, 2, 3, 6, 1, 4])
        position = {n: k for k, n in enumerate(got)}
        for a, b in LATER_FIRST:
            self.assertLess(position[b], position[a], f"step {a} waits for step {b}")
        self.assertEqual(L.one("SELECT depends_on FROM v_step_order WHERE step = 2")["depends_on"], "5")

    def test_import_steps_takes_later_dependency_and_refuses_cycle(self):
        L = self.L
        L.x("INSERT INTO plans(code, title) VALUES ('P-0001', 'Plan')")
        L.commit()
        f = fixtures.temp_dir("w6-steps-") / "steps.jsonl"
        lines = ['{"kind": "phase", "phase": 1, "title": "One", "goal": "Do it."}',
                 '{"key": "A", "phase": 1, "title": "A", "change": "C", "proof": "P", "no_findings_reason": "x", '
                 '"depends": ["B"]}',
                 '{"key": "B", "phase": 1, "title": "B", "change": "C", "proof": "P", "no_findings_reason": "x"}']
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        L.run_ok("import-steps", "P-0001", str(f))
        self.assertEqual(order(L), [2, 1])
        g = f.with_name("cycle.jsonl")
        g.write_text('{"key": "C", "phase": 1, "title": "C", "change": "C", "proof": "P", "no_findings_reason": "x", '
                     '"depends": ["D"]}\n{"key": "D", "phase": 1, "title": "D", "change": "C", "proof": "P", '
                     '"no_findings_reason": "x", "depends": ["C"]}\n', encoding="utf-8")
        p = L.run("import-steps", "P-0001", str(g))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("cycle", p.stderr)
        self.assertEqual(L.scalar("SELECT count(*) FROM steps"), 2)

    def test_progress_md_lists_steps_in_work_order(self):
        L = self.L
        _, s = plan_with_steps(L, 3)
        L.dependency(s[1], s[3])
        L.commit()
        md = L.run_ok("progress-md", "P-0001").stdout
        steps = md[md.index("| Step | Status |"):].splitlines()[2:]
        self.assertEqual([line.split("|")[1].strip() for line in steps], ["2", "3", "1"])


V16 = "c06a6e6"


def v16_ledger_py():
    """ledger.py built from the sources at commit c06a6e6 (schema 16), or None when git cannot give them."""
    root = fixtures.temp_dir("v16-")
    paths = ["SKILL.md", "dev/core.py", "dev/build.py", "dev/page.html", "references/schema.sql",
             "references/migrations"]
    try:
        tar = subprocess.run(["git", "-C", str(fixtures.REPO_ROOT), "archive", V16, *paths], capture_output=True,
                             check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(root)], input=tar, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("ledger_build_v16", str(root / "dev" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (root / "scripts").mkdir(exist_ok=True)
    out = root / "scripts" / "ledger.py"
    out.write_bytes(mod.build(root).encode("utf-8"))
    return out


class FromV16(unittest.TestCase):
    def test_migration_from_v16(self):
        old = v16_ledger_py()
        if old is None:
            self.skipTest(f"git cannot give the sources at {V16}")
        project = fixtures.temp_dir("v16-proj-") / "proj"
        project.mkdir()
        p = fixtures.run_ledger("--root", project, "init", ledger_py=old)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        db = project / ".code-review" / "ledger.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
        con.execute("INSERT INTO plans(code, title) VALUES ('P-0001', 'Plan')")
        con.execute("INSERT INTO phases(plan_id, number, title, goal) VALUES (1, 1, 'One', 'Do it.')")
        for n in (1, 2, 3):
            con.execute("INSERT INTO steps(plan_id, phase_id, number, title, change, proof) VALUES (1, 1, ?, ?, 'C', 'P')",
                        (n, f"Step {n}"))
        con.execute("INSERT INTO step_dependencies VALUES (2, 1)")
        with self.assertRaises(sqlite3.IntegrityError):   # schema 16: only an earlier step
            con.execute("INSERT INTO step_dependencies VALUES (1, 3)")
        con.commit()
        con.close()
        new = fixtures.built_ledger_path()
        p = fixtures.run_ledger("--root", project, "migrate", ledger_py=new)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0017-step-order", p.stdout)
        con = sqlite3.connect(str(db))
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(fixtures.load_ledger_module(new).verify_schema(con), [])
        con.execute("INSERT INTO step_dependencies VALUES (1, 3)")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO step_dependencies VALUES (3, 2)")
        self.assertEqual([r[0] for r in con.execute("SELECT step FROM v_step_order ORDER BY position")], [3, 1, 2])
        shutil.rmtree(str(project), ignore_errors=True)



class OrderByJudgement(unittest.TestCase):
    def test_resolve_conflict_order_first_names_the_later_step(self):
        from dev.tests.test_w4_conflicts import planned
        from dev.tests._p5_writes_helpers import ok, refused
        L = planned(self)
        L.run_ok("conflicts", "--plan", "P-0001", "--actor", "A-0001")
        self.assertIn("neither step 1 nor step 2", refused(self, L, "resolve-conflict", "P-0001", "1", "2",
                                                          "--resolution", "order", "--first", "3", "--note", "x",
                                                          "--actor", "A-0001"))
        ok(L, "resolve-conflict", "P-0001", "1", "2", "--resolution", "order", "--first", "2", "--note",
           "step 1 builds on step 2's column", "--actor", "A-0001")
        L.commit()
        dep = L.all("""SELECT a.number a, b.number b FROM step_dependencies d JOIN steps a ON a.id = d.step_id
                       JOIN steps b ON b.id = d.depends_on_step_id""")
        self.assertEqual([(r["a"], r["b"]) for r in dep], [(1, 2)])
        self.assertEqual(order(L), [2, 1, 3])


if __name__ == "__main__":
    unittest.main()
