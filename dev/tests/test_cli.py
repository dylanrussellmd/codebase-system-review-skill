"""The ledger.py command line, run from the fresh build by subprocess in temporary folders: init, schema, serve options,
the missing-ledger guard, subcommand handling, and show."""
import socket
import sqlite3
import sys
import time
import subprocess
import unittest
from unittest import mock

from dev.tests import fixtures
from dev.tests import _migrate_helpers as M
from dev.tests.fixtures import built_ledger_path, run_ledger, temp_dir
from dev.tests._core_helpers import project, ref

SCHEMA_SQL = fixtures.REPO_ROOT / "references" / "schema.sql"


def fresh_project(name="myproj"):
    d = temp_dir("cli-") / name
    d.mkdir()
    return d


def object_counts(db):
    con = sqlite3.connect(str(db))
    try:
        return dict(con.execute("SELECT type, count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                                "GROUP BY type").fetchall())
    finally:
        con.close()


class Init(unittest.TestCase):
    def test_init_adds_missing_gitignore_lines(self):
        # A .gitignore written by an older version (no backup lines) gains the new lines; custom lines are kept.
        d = fresh_project()
        (d / ".code-review").mkdir()
        gi = d / ".code-review" / ".gitignore"
        gi.write_text("ledger.sqlite-journal\nledger.sqlite-wal\nledger.sqlite-shm\nmy-notes.txt")
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        # Step 48: the owner's lines first, then the tool's lines in order (the file is rewritten, not appended to).
        self.assertEqual(gi.read_text(), "my-notes.txt\n" + fixtures.load_ledger_module().GITIGNORE)
        self.assertIn(f"repaired {gi}: added ledger.sqlite.tmp, ledger.sqlite.bak-v*, ledger.py.bak", p.stdout)
        p = run_ledger("--root", d, "init")
        self.assertNotIn("repaired", p.stdout)

    def test_init_copies_tool_and_gitignore(self):
        mod = fixtures.load_ledger_module()
        d = fresh_project()
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        db = d / ".code-review" / "ledger.sqlite"
        self.assertIn(f"project root: {d}", p.stdout)
        self.assertIn(f"created {db}", p.stdout)
        self.assertIn(f"copied ledger.py to {d / '.code-review' / 'ledger.py'}", p.stdout)
        counts = object_counts(db)
        # The counts at commit ea00196 were 35, 18, 16; schema 2 (migration 0002) added tr_gate_answer_final and
        # tr_gate_answer_cleared; schema 3 and later add many triggers (plan phase 4), so the counts are now those of
        # the shipped schema itself, and at least the ea00196 ones.
        want = {k: sum(1 for o in mod.shipped_objects() if o[0] == k) for k in ("table", "view", "trigger")}
        self.assertEqual((counts["table"], counts["view"], counts["trigger"]),
                         (want["table"], want["view"], want["trigger"]))
        self.assertGreaterEqual(want["trigger"], 18 + 140)  # schema 3 (0003-integrity) alone adds over 140
        # Same objects as the source schema.
        mem = sqlite3.connect(":memory:")
        mem.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        want = sorted(mem.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall())
        mem.close()
        con = sqlite3.connect(str(db))
        got = sorted(con.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall())
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        con.close()
        self.assertEqual(got, want)
        self.assertEqual(meta["schema_version"], str(mod.SCHEMA_VERSION))
        self.assertEqual(meta["project_name"], "myproj")
        self.assertEqual(meta["created_by_skill"], mod.TOOL_VERSION)
        self.assertRegex(meta["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        # ledger.py copied byte-identical; .gitignore written (local backups included).
        self.assertEqual((d / ".code-review" / "ledger.py").read_bytes(), built_ledger_path().read_bytes())
        self.assertEqual((d / ".code-review" / ".gitignore").read_text(), mod.GITIGNORE)
        self.assertIn("ledger.sqlite-wal\nledger.sqlite-shm\n", mod.GITIGNORE)
        self.assertIn("WAL", mod.GITIGNORE.splitlines()[0])  # test_init_gitignore_lists_wal_files_with_comment

        # Second run: reports the existing schema, leaves the data alone, does not copy again.
        con = sqlite3.connect(str(db))
        con.execute("INSERT INTO meta(key, value) VALUES ('probe', 'kept')")
        con.commit()
        con.close()
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"{db} already exists (schema {mod.SCHEMA_VERSION})", p.stdout)
        self.assertNotIn("copied", p.stdout)
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='probe'").fetchone()[0], "kept")
        con.close()

        # An older (here: unversioned) ledger.py in the folder is replaced, with a note and a .bak (step 31; at
        # ea00196 any differing copy was replaced, newer or not, and no backup was kept).
        (d / ".code-review" / "ledger.py").write_text("# old\n")
        p = run_ledger("--root", d, "init")
        self.assertIn("note: replacing", p.stdout)
        self.assertEqual((d / ".code-review" / "ledger.py").read_bytes(), built_ledger_path().read_bytes())
        self.assertEqual((d / ".code-review" / "ledger.py.bak").read_text(), "# old\n")

    def test_init_keeps_existing_gitignore(self):
        d = fresh_project()
        (d / ".code-review").mkdir()
        (d / ".code-review" / ".gitignore").write_text("custom\n")
        p = run_ledger("--root", d, "init")
        self.assertEqual(p.returncode, 0, p.stderr)
        # Custom lines are kept; the tool's own lines are added after them.
        self.assertEqual((d / ".code-review" / ".gitignore").read_text(),
                         "custom\n" + fixtures.load_ledger_module().GITIGNORE)

    def set_version(self, db, value):
        con = sqlite3.connect(str(db))
        if value is None:
            con.execute("DELETE FROM meta WHERE key='schema_version'")
        else:
            con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (value,))
        con.commit()
        con.close()

    def assertOneLineRefusal(self, p, words):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(words, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)

    def test_init_refuses_newer_schema(self):
        d = fresh_project()
        run_ledger("--root", d, "init")
        db = d / ".code-review" / "ledger.sqlite"
        self.set_version(db, "99")
        before = db.read_bytes()
        p = run_ledger("--root", d, "init")
        self.assertOneLineRefusal(p, f"{db} is schema 99; this ledger.py only knows {M.SHIPPED}. Use a newer "
                                     f"ledger.py.")
        self.assertEqual(db.read_bytes(), before)

    def test_init_refuses_older_schema(self):
        from dev.tests import _migrate_helpers as M
        project, db = M.restore_v1()
        p = run_ledger("--root", project, "init", ledger_py=M.v2_ledger_py())
        self.assertOneLineRefusal(p, f"{db} is schema 1; this ledger.py is schema {M.NEXT}. Run: python3 ")
        self.assertIn("ledger.py migrate", p.stderr)
        # Below the oldest schema this tool can migrate (step 32; at ea00196: "run the migrations for 1..1 first",
        # which did not exist).
        d = fresh_project()
        run_ledger("--root", d, "init")
        db = d / ".code-review" / "ledger.sqlite"
        self.set_version(db, "0")
        p = run_ledger("--root", d, "init")
        self.assertOneLineRefusal(p, f"{db} is schema 0, older than any schema this ledger.py can migrate (oldest 1)")

    def test_init_missing_version_row_clean_error(self):
        # Was test_current_init_tracebacks_on_bad_ledgers (TEST-T1, ISS-0282: a TypeError traceback); step 33.
        d = fresh_project()
        run_ledger("--root", d, "init")
        db = d / ".code-review" / "ledger.sqlite"
        self.set_version(db, None)
        p = run_ledger("--root", d, "init")
        self.assertOneLineRefusal(p, f"{db} has no schema_version in its meta table")

    def test_init_non_sqlite_clean_error(self):
        # Was test_current_init_tracebacks_on_bad_ledgers (TEST-T1, ISS-0282: a DatabaseError traceback); step 33.
        d = fresh_project()
        run_ledger("--root", d, "init")
        db = d / ".code-review" / "ledger.sqlite"
        db.write_bytes(b"this is not a database at all" * 100)
        p = run_ledger("--root", d, "init")
        self.assertOneLineRefusal(p, f"{db} is not a ledger")
        self.assertEqual(db.read_bytes(), b"this is not a database at all" * 100)


class SchemaAndGuards(unittest.TestCase):
    def test_cli_schema_prints_source_schema(self):
        d = fresh_project()
        p = run_ledger("--root", d, "schema")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, SCHEMA_SQL.read_text(encoding="utf-8") + "\n")
        self.assertFalse((d / ".code-review").exists(), "schema must not create a ledger")

    def test_commands_refuse_without_ledger(self):
        d = fresh_project()
        db = d / ".code-review" / "ledger.sqlite"
        for args in (["check"], ["gates"], ["show", "1"], ["dump"], ["fill-refs"], ["tag-review", "R-0001"],
                     ["serve", "--no-open"], ["migrate"]):
            with self.subTest(args=args):
                p = run_ledger("--root", d, *args, timeout=20)
                self.assertEqual(p.returncode, 1)
                self.assertIn(f"no ledger at {db}; run: python3 ledger.py init", p.stderr)
        self.assertFalse((d / ".code-review").exists())

    def test_cli_requires_subcommand(self):
        d = fresh_project()
        p = run_ledger("--root", d)
        self.assertEqual(p.returncode, 2)
        self.assertIn("required", p.stderr)
        p = run_ledger("--root", d, "frobnicate")
        self.assertEqual(p.returncode, 2)
        self.assertIn("invalid choice", p.stderr)


class Serve(unittest.TestCase):
    def test_cli_serve_port_and_no_open(self):
        # Argument mapping, in-process: default port 8765 and opening the browser; --port N and --no-open.
        L, _, _, _ = project(test=self)
        L.commit()
        m = fixtures.load_ledger_module(fresh=True)
        calls = []
        m.serve = lambda root, db, port, open_browser: calls.append((root, db, port, open_browser))
        for argv, want in ((["serve"], (8765, True)), (["serve", "--port", "9123", "--no-open"], (9123, False))):
            with mock.patch.object(sys, "argv", ["ledger.py", "--root", str(L.root), *argv]):
                m.main()
            self.assertEqual(calls[-1], (L.root, L.db, *want))

        # The real CLI: --port N binds N; --no-open never calls webbrowser.open (a sitecustomize shim records calls).
        shim = temp_dir("shim-")
        marker = shim / "opened.txt"
        (shim / "sitecustomize.py").write_text(
            "import webbrowser\n"
            f"def _open(url, *a, **k):\n    open({str(marker)!r}, 'a').write(url + '\\n'); return True\n"
            "webbrowser.open = _open\n")
        env = fixtures.git_env({"PYTHONPATH": str(shim), "BROWSER": "true"})

        def run_serve(*extra):
            port = fixtures.free_port()
            proc = subprocess.Popen([sys.executable, str(L.ledger_py), "--root", str(L.root), "serve",
                                     "--port", str(port), *extra], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.time() + 10
                while True:
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                        break
                    except OSError:
                        if proc.poll() is not None or time.time() > deadline:
                            self.fail(f"serve did not listen on {port}: {proc.communicate()}")
                        time.sleep(0.05)
                time.sleep(1.0)  # the browser would open after 0.5 s
            finally:
                proc.terminate()
                out, err = proc.communicate(timeout=5)
            return port, out

        port, out = run_serve("--no-open")
        self.assertIn(f"at http://127.0.0.1:{port}/", out)
        self.assertFalse(marker.exists(), "--no-open must not open a browser")
        port, out = run_serve()  # control: without --no-open the shim sees the call
        # step 42: the browser opens the launch URL, whose #k= fragment carries the token; serve printed the same URL
        opened = marker.read_text()
        self.assertRegex(opened, r"^http://127\.0\.0\.1:%d/#k=[A-Za-z0-9_\-]{32,}\n$" % port)
        self.assertIn(opened.strip(), out)


class Show(unittest.TestCase):
    def make(self):
        L, sha, rid, _ = project(test=self)
        self.sha = sha
        lid = ref(L, rid, "src/a.py", 10, 12, excerpt="line 10\nline 11\nline 12")
        L.commit()
        return L, lid

    def test_cli_show_lines(self):
        L, lid = self.make()
        p = L.run_ok("show", lid)
        self.assertEqual(p.stdout, f"# demo src/a.py @ {self.sha} (from git)\n"
                                   "    10  line 10\n    11  line 11\n    12  line 12\n")
        p = L.run("show", "abc")
        self.assertEqual(p.returncode, 2)
        self.assertIn("invalid int value", p.stderr)

    def test_cli_show_unknown(self):
        L, _ = self.make()
        p = L.run("show", "99999")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip(), "no such reference")
        self.assertEqual(p.stdout, "")

    def test_cli_show_falls_back_to_excerpt(self):
        """SPEC-T3: the commit is lost, show prints the saved excerpt."""
        L, lid = self.make()
        L.repo.rewrite_history()
        p = L.run_ok("show", lid)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], f"# demo src/a.py @ {self.sha} (from excerpt)")
        self.assertTrue(lines[1].startswith("# git: "), lines[1])
        self.assertEqual(lines[2:], ["    10  line 10", "    11  line 11", "    12  line 12"])



if __name__ == "__main__":
    unittest.main()
