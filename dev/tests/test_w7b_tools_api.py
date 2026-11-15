"""The API, database, performance, email and translation tools (plan P-0001 step 224; FIX-31, decision D-0015):
Schemathesis and a Hurl route-by-role access matrix against the running API; oasdiff, Buf and GraphQL Inspector
between the description at the last review and now; Squawk on PostgreSQL migrations; the query planner (SQLite built
in, PostgreSQL EXPLAIN on a throwaway database); hyperfine against the project's budgets; Mailpit for every email a
walkthrough sends; ESLint's i18next rule and pseudo-localization for translated text. Each runs only when scan finds
what it needs. Real samples: schemathesis.xml (4.33.2), buf.jsonl (1.75.0), graphql-inspector.txt (7.0.2),
squawk.json (2.71.0), hyperfine.json (2.0.0), i18n-lint.json (ESLint 10.14.0 with eslint-plugin-i18next 6.1.5),
pg-explain.json (PostgreSQL 16.15). Written from the documented format: oasdiff-documented.json and
hurl-documented.xml (neither tool could be installed here)."""
import json
import os
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dev.tests import fixtures
from dev.tests._w7a_tools_helpers import DATA, env_with, leads, ok, shared_project, shim, tool_project

SPEC = """openapi: 3.0.3
info: {title: Demo, version: "1.0"}
paths:
  /api/users:
    get:
      responses:
        "200":
          description: the users
          content:
            application/json:
              schema: {type: array, items: {type: object, properties: {id: {type: integer}}, required: [id]}}
  /api/users/{id}:
    get:
      parameters:
        - {name: id, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          description: one user
          content:
            application/json:
              schema: {type: object, properties: {id: {type: integer}}, required: [id]}
        "404": {description: no such user}
"""
VERSION = "import sys\nif '--version' in sys.argv or 'version' in sys.argv[1:2]:\n    print(%r); sys.exit(0)\n"


def at_repo(name, repo, folder="/project"):
    f = fixtures.temp_dir("w7b-sample-") / name
    f.write_text((DATA / name).read_text(encoding="utf-8").replace(folder, str(repo.path)), encoding="utf-8")
    return str(f)


def follow_up(test, repo, L, files):
    """Commit files and start R-0002 following R-0001 (at the new commit)."""
    for rel, text in files.items():
        repo.write(rel, text)
    repo.commit("second")
    ok(L, "review-status", "R-0001", "handed-off", "--note", "done", "--actor", "A-0001")
    ok(L, "new-review", "--title", "Second", "--scope", "full", "--previous", "R-0001", "--actor", "A-0001")


class Demo(BaseHTTPRequestHandler):
    """The API the schemathesis sample was recorded against: /api/users/{id} breaks on negative ids and returns
    a string id."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/users":
            body, code = json.dumps([{"id": 1}]), 200
        elif self.path.startswith("/api/users/"):
            tail = self.path.rsplit("/", 1)[1]
            body, code = ("boom", 500) if tail.lstrip("-").isdigit() and int(tail) < 0 else \
                (json.dumps({"id": "one"}), 200)
        else:
            body, code = "", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())


def serve(test, handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_address[1]}"


class Schemathesis(unittest.TestCase):
    def test_schemathesis_import(self):
        """One lead per operation and failed check, at the operation in the description."""
        repo, L, sha = tool_project(self, files={"openapi.yaml": SPEC})
        ok(L, "tool-run", "schemathesis", "--review", "R-0001", "--from", str(DATA / "schemathesis.xml"),
           "--actor", "A-0001")
        got = sorted(g[1:4] for g in leads(L))
        self.assertEqual(got, [("schemathesis.ST101", "openapi.yaml", 5), ("schemathesis.ST101", "openapi.yaml", 13),
                               ("schemathesis.ST108", "openapi.yaml", 13), ("schemathesis.ST204", "openapi.yaml", 5),
                               ("schemathesis.ST204", "openapi.yaml", 13)])
        L.commit()
        self.assertEqual(L.scalar("SELECT message FROM tool_results WHERE rule='schemathesis.ST108'"),
                         "GET /api/users/{id}: Response violates schema")
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "contracts")

    def test_schemathesis_needs_a_target_on_this_machine(self):
        script = VERSION % "schemathesis, version 4.33.2" + (
            "import os\nargs = sys.argv\nassert args[1:3] == ['run', 'openapi.yaml'], args\n"
            "assert args[args.index('--seed') + 1] == os.environ['WANT_SEED'], args\n"
            "out = args[args.index('--report-junit-path') + 1]\n"
            f"open(out, 'w').write(open({str(DATA / 'schemathesis.xml')!r}).read())\nsys.exit(1)\n")
        repo, L, sha = tool_project(self, files={"openapi.yaml": SPEC})
        env = env_with(shim("schemathesis", script))
        env["WANT_SEED"] = str(int(sha[:8], 16) % 1000000)
        out = ok(L, "tool-run", "schemathesis", "--review", "R-0001", "--actor", "A-0001", env=env)
        self.assertIn("give --target", out[0])
        out = ok(L, "tool-run", "schemathesis", "--review", "R-0001", "--target", "https://api.example.com",
                 "--actor", "A-0001", env=env)
        self.assertIn("--online", out[0])
        ok(L, "tool-run", "schemathesis", "--review", "R-0001", "--target", "http://127.0.0.1:9999",
           "--actor", "A-0001", env=env)
        run = L.one("SELECT * FROM tool_runs WHERE id=3")
        self.assertEqual((run["status"], run["exit_code"]), ("ran", 1))
        self.assertIn("--url {target}", run["command"])
        self.assertEqual(L.count("tool_results"), 5)

    @unittest.skipUnless(shutil.which("schemathesis"), "environment: Schemathesis is not installed")
    def test_schemathesis_real(self):
        url = serve(self, Demo)
        repo, L, sha = tool_project(self, files={"openapi.yaml": SPEC})
        env = fixtures.git_env({"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        ok(L, "tool-run", "schemathesis", "--review", "R-0001", "--target", url, "--unpinned", "--actor", "A-0001",
           env=env)
        rules = {g[1] for g in leads(L)}
        self.assertTrue({"schemathesis.ST101", "schemathesis.ST108"} <= rules, rules)


class Hurl(unittest.TestCase):
    def test_access_matrix_files(self):
        """One request file per operation and role, asserting what the matrix expects."""
        mod = fixtures.load_ledger_module()
        files = mod.access_matrix(SPEC, {"roles": {"admin": {"headers": {"Authorization": "Bearer {{admin_token}}"}},
                                                   "anonymous": {}},
                                         "expect": {"GET /api/users": {"admin": 200, "anonymous": "denied"},
                                                    "*": {"admin": "allowed"}},
                                         "params": {"id": "7"}})
        self.assertEqual([f[0] for f in files], ["0001-GET-%2Fapi%2Fusers--admin.hurl",
                                                 "0002-GET-%2Fapi%2Fusers--anonymous.hurl",
                                                 "0003-GET-%2Fapi%2Fusers%2F%7Bid%7D--admin.hurl",
                                                 "0004-GET-%2Fapi%2Fusers%2F%7Bid%7D--anonymous.hurl"])
        self.assertEqual(files[0][1], "# GET /api/users as admin\nGET {{base}}/api/users\n"
                                      "Authorization: Bearer {{admin_token}}\nHTTP 200\n")
        self.assertIn("status >= 401\nstatus <= 403\n", files[1][1])
        self.assertIn("GET {{base}}/api/users/7\n", files[2][1])
        self.assertIn("status < 400", files[2][1])
        self.assertIn("status < 500", files[3][1])

    def test_hurl_access_matrix_import(self):
        repo, L, sha = tool_project(self, files={"openapi.yaml": SPEC})
        ok(L, "tool-run", "hurl", "--review", "R-0001", "--from", at_repo("hurl-documented.xml", repo),
           "--actor", "A-0001")
        got = [(g[1], g[2]) for g in leads(L)]
        self.assertEqual(got, [("hurl.access", None)])
        L.commit()
        msg = L.scalar("SELECT message FROM tool_results")
        self.assertTrue(msg.startswith("GET /api/users as anonymous: Assert status code"), msg)
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics")}
        self.assertEqual((m["access_matrix_cells"], m["access_matrix_failures"]), (3, 1))

    def test_hurl_runs_the_generated_matrix(self):
        script = VERSION % "hurl 8.1.0" + (
            "import os, pathlib\nargs = sys.argv\nfiles = [a for a in args if a.endswith('.hurl')]\n"
            "assert len(files) == 4, files\n"
            "assert not any('s3cret' in a for a in args), args\n"
            "assert any(a.startswith('base=http://127.0.0.1') for a in args), args\n"
            "out = args[args.index('--report-junit') + 1]\n"
            f"open(out, 'w').write(open({str(DATA / 'hurl-documented.xml')!r}).read())\nsys.exit(3)\n")
        repo, L, sha = tool_project(self, files={"openapi.yaml": SPEC})
        matrix = fixtures.temp_dir("w7b-matrix-") / "matrix.json"
        matrix.write_text(json.dumps({"roles": {"admin": {"headers": {"Authorization": "Bearer {{admin_token}}"}},
                                                "anonymous": {}}}))
        env = env_with(shim("hurl", script))
        env["admin_token"] = "s3cret"
        ok(L, "tool-run", "hurl", "--review", "R-0001", "--target", "http://127.0.0.1:8000", "--rules", str(matrix),
           "--actor", "A-0001", env=env)
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["exit_code"]), ("ran", 3))
        self.assertNotIn("s3cret", run["command"])
        self.assertIn("--secrets-file {secrets}", run["command"])

    # The stand-in hurl writes what it was given to $HURL_DUMP: its argv, and each file named after
    # --secrets-file or --variables-file with that file's mode and text.
    DUMPING = VERSION % "hurl 8.1.0" + (
        "import json, os\nargs = sys.argv[1:]\ngiven = {}\n"
        "for flag in ('--secrets-file', '--variables-file'):\n"
        "    if flag in args:\n"
        "        f = args[args.index(flag) + 1]\n"
        "        given[flag] = {'path': f, 'mode': os.stat(f).st_mode & 0o777, 'text': open(f).read()}\n"
        "json.dump({'argv': args, 'files': given}, open(os.environ['HURL_DUMP'], 'w'))\n"
        "out = args[args.index('--report-junit') + 1]\n"
        f"open(out, 'w').write(open({str(DATA / 'hurl-documented.xml')!r}).read())\nsys.exit(3)\n")

    def run_dumping(self, files, matrix=None, extra=()):
        repo, L, sha = tool_project(self, files=files)
        dump = fixtures.temp_dir("w7b-dump-") / "dump.json"
        env = env_with(shim("hurl", self.DUMPING))
        env.update({"HURL_DUMP": str(dump), "admin_token": "s3cret", "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI"})
        args = ["--rules", str(matrix)] if matrix else []
        ok(L, "tool-run", "hurl", "--review", "R-0001", "--target", "http://127.0.0.1:8000", *args, *extra,
           "--actor", "A-0001", env=env)
        return L, json.loads(dump.read_text())

    def test_hurl_never_passes_env_named_by_repo_files(self):
        L, got = self.run_dumping({"openapi.yaml": SPEC, "tests/steal.hurl":
                                   "GET {{base}}/api/users\nX-Key: {{AWS_SECRET_ACCESS_KEY}}\nHTTP 200\n"})
        self.assertTrue(any(a.endswith("steal.hurl") for a in got["argv"]), got)
        self.assertNotIn("wJalrXUtnFEMI", json.dumps(got))
        self.assertNotIn("wJalrXUtnFEMI", L.scalar("SELECT command FROM tool_runs"))

    def test_hurl_matrix_secret_not_on_argv(self):
        matrix = fixtures.temp_dir("w7b-matrix-") / "matrix.json"
        matrix.write_text(json.dumps({"roles": {"admin": {"headers": {"Authorization": "Bearer {{admin_token}}"}},
                                                "anonymous": {}}}))
        L, got = self.run_dumping({"openapi.yaml": SPEC}, matrix)
        self.assertFalse(any("s3cret" in a for a in got["argv"]), got["argv"])
        given = got["files"].get("--secrets-file")
        self.assertIsNotNone(given, got)
        self.assertEqual(given["mode"], 0o600)
        self.assertEqual(given["text"], "admin_token=s3cret\n")
        self.assertFalse(Path(given["path"]).exists())
        self.assertFalse(Path(given["path"]).parent.exists())
        self.assertEqual(L.scalar("SELECT status FROM tool_runs"), "ran")

    def test_hurl_refuses_other_host_offline(self):
        files = {"openapi.yaml": SPEC, "tests/ok.hurl": "GET {{base}}/api/users\nHTTP 200\n",
                 "tests/away.hurl": "GET {{base}}/api/users\nHTTP 200\n\nPOST https://collect.example/x\nHTTP 200\n"}
        L, got = self.run_dumping(files)
        self.assertTrue(any(a.endswith("ok.hurl") for a in got["argv"]), got["argv"])
        self.assertFalse(any(a.endswith("away.hurl") for a in got["argv"]), got["argv"])
        L.commit()
        note = L.scalar("SELECT note FROM tool_runs")
        self.assertIn("tests/away.hurl:4", note or "")
        self.assertIn("collect.example", note or "")
        L, got = self.run_dumping(files, extra=["--online"])
        self.assertTrue(any(a.endswith("away.hurl") for a in got["argv"]), got["argv"])

    def test_hurl_refuses_redirect_options_offline(self):
        files = {"openapi.yaml": SPEC, "tests/ok.hurl": "GET {{base}}/api/users\n[Options]\nretry: 2\nHTTP 200\n",
                 "tests/follow.hurl": "GET {{base}}/api/users\n[Options]\nlocation: true\nHTTP 200\n",
                 "tests/proxy.hurl": "GET {{base}}/api/users\nAccept: */*\n[Options]\nverbose: true\n"
                                     "proxy: collect.example:8080\nHTTP 200\n"}
        L, got = self.run_dumping(files)
        argv = got["argv"]
        self.assertTrue(any(a.endswith("ok.hurl") for a in argv), argv)
        self.assertFalse(any(a.endswith(("follow.hurl", "proxy.hurl")) for a in argv), argv)
        export = next(a for a in argv if a.endswith("ok.hurl"))[:-len("/tests/ok.hurl")]
        self.assertEqual(argv[argv.index("--file-root") + 1], export)
        L.commit()
        note = L.scalar("SELECT note FROM tool_runs") or ""
        self.assertIn("tests/follow.hurl:3", note)
        self.assertIn("location", note)
        self.assertIn("tests/proxy.hurl:5", note)
        L, got = self.run_dumping(files, extra=["--online"])
        self.assertTrue(any(a.endswith("follow.hurl") for a in got["argv"]), got["argv"])


SPEC_V2 = SPEC.replace("""  /api/users/{id}:
    get:
      parameters:
        - {name: id, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          description: one user
          content:
            application/json:
              schema: {type: object, properties: {id: {type: integer}}, required: [id]}
        "404": {description: no such user}
""", "")


class ApiDiffs(unittest.TestCase):
    def test_oasdiff_against_last_review(self):
        script = VERSION % "oasdiff version 1.30.0" + (
            "args = sys.argv\nassert args[1] == 'changelog' and args[3] == 'openapi.yaml', args\n"
            "assert '/api/users/{id}' in open(args[2]).read(), 'the base is the description at R-0001'\n"
            "assert '/api/users/{id}' not in open(args[3]).read()\n"
            f"sys.stdout.write(open({str(DATA / 'oasdiff-documented.json')!r}).read())\n")
        repo, L, sha = tool_project(self, files={"openapi.yaml": SPEC})
        env = env_with(shim("oasdiff", script))
        out = ok(L, "tool-run", "oasdiff", "--review", "R-0001", "--actor", "A-0001", env=env)
        self.assertIn("follows up no review", out[0])
        follow_up(self, repo, L, {"openapi.yaml": SPEC_V2})
        ok(L, "tool-run", "oasdiff", "--review", "R-0002", "--actor", "A-0001", env=env)
        L.commit()
        run = L.one("SELECT * FROM tool_runs WHERE id=2")
        self.assertEqual(run["status"], "ran")
        self.assertIn("changelog {previous} openapi.yaml", run["command"])
        got = [(r["rule"], r["level"], r["path"]) for r in
               L.all("SELECT rule, level, path FROM tool_results ORDER BY code")]
        self.assertEqual(sorted(got), [("oasdiff.api-path-removed-without-deprecation", "error", "openapi.yaml"),
                                       ("oasdiff.endpoint-added", "note", "openapi.yaml"),
                                       ("oasdiff.response-required-property-removed", "error", "openapi.yaml")])
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='api_breaking_changes'"), 2)
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "compatibility")

    def test_buf_import(self):
        repo, L, sha = tool_project(self, files={"proto/user.proto": 'syntax = "proto3";\n'})
        ok(L, "tool-run", "buf", "--review", "R-0001", "--from", str(DATA / "buf.jsonl"), "--actor", "A-0001")
        self.assertEqual([g[1:] for g in leads(L)], [("buf.FIELD_NO_DELETE", "proto/user.proto", 3, "error"),
                                                     ("buf.FIELD_SAME_TYPE", "proto/user.proto", 4, "error")])

    @unittest.skipUnless(shutil.which("buf"), "environment: Buf is not installed")
    def test_buf_real(self):
        old = 'syntax = "proto3";\npackage demo.v1;\nmessage User {\n  int64 id = 1;\n  string email = 2;\n}\n'
        new = 'syntax = "proto3";\npackage demo.v1;\nmessage User {\n  string id = 1;\n}\n'
        repo, L, sha = tool_project(self, files={"proto/user.proto": old})
        follow_up(self, repo, L, {"proto/user.proto": new})
        ok(L, "tool-run", "buf", "--review", "R-0002", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "buf", "--review", "R-0002", "--from", str(DATA / "buf.jsonl"), "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message FROM tool_results WHERE tool_run_id=? ORDER BY code",
                      (i,)) for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])

    def test_graphql_inspector_import(self):
        repo, L, sha = tool_project(self, files={"schema.graphql": "type Query { a: Int }\n"})
        ok(L, "tool-run", "graphql-inspector", "--review", "R-0001", "--from", str(DATA / "graphql-inspector.txt"),
           "--actor", "A-0001")
        L.commit()
        got = [(r["rule"], r["level"], r["message"]) for r in
               L.all("SELECT rule, level, message FROM tool_results ORDER BY code")]
        self.assertEqual(sorted(got), [
            ("graphql-inspector.breaking", "error", "Argument active: Boolean! added to field Query.user"),
            ("graphql-inspector.breaking", "error", "Field email was removed from object type User"),
            ("graphql-inspector.safe", "note", "Field nickname was added to object type User")])

    @unittest.skipUnless(shutil.which("graphql-inspector"), "environment: GraphQL Inspector is not installed")
    def test_graphql_inspector_real(self):
        old = ("type Query {\n  user(id: ID!): User\n  users: [User!]!\n}\n\ntype User {\n  id: ID!\n  email: String\n"
               "  name: String\n}\n")
        new = ("type Query {\n  user(id: ID!, active: Boolean!): User\n  users: [User!]!\n}\n\ntype User {\n  id: ID!\n"
               "  name: String\n  nickname: String\n}\n")
        repo, L, sha = tool_project(self, files={"schema.graphql": old})
        follow_up(self, repo, L, {"schema.graphql": new})
        ok(L, "tool-run", "graphql-inspector", "--review", "R-0002", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "graphql-inspector", "--review", "R-0002", "--from", str(DATA / "graphql-inspector.txt"),
           "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, level, message FROM tool_results WHERE tool_run_id=? ORDER BY code", (i,))
                for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])


MIGRATIONS = {"migrations/0001_init.sql": "CREATE TABLE accounts (id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL);\n",
              "migrations/0002_index.sql": "CREATE INDEX accounts_email ON accounts (email);\n"
                                           "ALTER TABLE accounts ADD COLUMN plan TEXT NOT NULL DEFAULT 'free';\n"
                                           "ALTER TABLE accounts ALTER COLUMN email TYPE VARCHAR(200);\n",
              "app/db.py": "import psycopg\n\nFIND = 'SELECT id FROM accounts WHERE email = %s'\n"
                           "ALL = 'SELECT id FROM accounts WHERE plan = %s'\n"}


class Database(unittest.TestCase):
    def test_squawk_import(self):
        repo, L, sha = tool_project(self, files=MIGRATIONS)
        ok(L, "tool-run", "squawk", "--review", "R-0001", "--from", str(DATA / "squawk.json"), "--actor", "A-0001")
        got = [g[1:4] for g in leads(L)]
        self.assertIn(("squawk.require-concurrent-index-creation", "migrations/0002_index.sql", 1), got)
        self.assertIn(("squawk.prefer-identity", "migrations/0001_init.sql", 1), got)
        L.commit()
        self.assertIn("use `concurrently`", L.scalar(
            "SELECT message FROM tool_results WHERE rule='squawk.require-concurrent-index-creation'"))

    @unittest.skipUnless(shutil.which("squawk"), "environment: Squawk is not installed")
    def test_squawk_real(self):
        repo, L, sha = tool_project(self, files=MIGRATIONS)
        ok(L, "tool-run", "squawk", "--review", "R-0001", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "squawk", "--review", "R-0001", "--from", str(DATA / "squawk.json"), "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message FROM tool_results WHERE tool_run_id=? ORDER BY code",
                      (i,)) for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b])

    def test_query_plan_sqlite(self):
        """The built-in planner check: the schema applied to an empty SQLite database, each query the code holds
        explained; a query reading a whole table or sorting in a temporary B-tree is a lead at its line."""
        files = {"db/schema.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT);\n"
                                  "CREATE INDEX users_email ON users (email);\n",
                 "app/store.py": ("import sqlite3\n\n\ndef find(con, email):\n"
                                  "    return con.execute(\"SELECT id FROM users WHERE email = ?\", (email,))\n\n\n"
                                  "def named(con, name):\n"
                                  "    return con.execute('SELECT id FROM users WHERE name = :name', {'name': name})\n"
                                  "\n\ndef listing(con):\n    return con.execute(\"\"\"\n"
                                  "        SELECT id, name FROM users\n        ORDER BY name\n    \"\"\")\n\n\n"
                                  "def built(con, col):\n    return con.execute(f\"SELECT {col} FROM users\")\n")}
        repo, L, sha = tool_project(self, files=files)
        out = ok(L, "tool-run", "query-plan", "--review", "R-0001", "--actor", "A-0001")
        L.commit()
        got = [(r["rule"], r["path"], r["line_start"]) for r in
               L.all("SELECT rule, path, line_start FROM tool_results ORDER BY code")]
        self.assertEqual(got, [("query-plan.full-scan", "app/store.py", 9), ("query-plan.full-scan", "app/store.py", 14),
                               ("query-plan.temp-sort", "app/store.py", 14)], out)
        m = {r["name"]: r["value"] for r in L.all("SELECT name, value FROM metrics")}
        self.assertEqual((m["queries_found"], m["queries_explained"]), (3, 3))
        rules = fixtures.temp_dir("w7b-q-") / "queries.sql"
        rules.write_text("-- the report's queries\nSELECT email FROM users WHERE id = ?;\n\nSELECT * FROM users "
                         "WHERE name LIKE 'a%';\n")
        ok(L, "tool-run", "query-plan", "--review", "R-0001", "--rules", str(rules), "--actor", "A-0001")
        L.commit()
        self.assertIn(("query-plan.full-scan", "queries.sql", 4),
                      [(r["rule"], r["path"], r["line_start"]) for r in
                       L.all("SELECT rule, path, line_start FROM tool_results WHERE tool_run_id=2")])

    def test_query_plan_refuses_attach(self):
        made = fixtures.temp_dir("w7b-attach-")
        attached, vacuumed = made / "attached.sqlite", made / "vacuumed.sqlite"
        files = {"db/schema.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);\n",
                 "db/attach.sql": f"ATTACH DATABASE '{attached}' AS x;\nCREATE TABLE x.t (a INT);\n",
                 "db/vacuum.sql": f"VACUUM INTO '{vacuumed}';\n",
                 "db/pragma.sql": "PRAGMA writable_schema = ON;\n",
                 "app/store.py": "Q = \"SELECT id FROM users WHERE email = ?\"\n"}
        repo, L, sha = tool_project(self, files=files)
        ok(L, "tool-run", "query-plan", "--review", "R-0001", "--actor", "A-0001")
        self.assertFalse(attached.exists())
        self.assertFalse(vacuumed.exists())
        L.commit()
        doc = json.loads((L.root / L.scalar("SELECT output_path FROM tool_runs")).read_text())
        self.assertEqual(sorted(f["file"] for f in doc["failed"]), ["db/attach.sql", "db/pragma.sql", "db/vacuum.sql"])
        self.assertEqual(doc["applied"], ["db/schema.sql"])
        self.assertEqual([q.get("plan") is not None for q in doc["queries"]], [True])

    def test_pg_explain_import(self):
        repo, L, sha = tool_project(self, files=MIGRATIONS)
        ok(L, "tool-run", "pg-explain", "--review", "R-0001", "--from", str(DATA / "pg-explain.json"),
           "--actor", "A-0001")
        L.commit()
        got = [(r["rule"], r["path"], r["line_start"]) for r in
               L.all("SELECT rule, path, line_start FROM tool_results ORDER BY code")]
        self.assertEqual(got, [("pg-explain.seq-scan", "app/db.py", 4)])
        self.assertIn("accounts", L.scalar("SELECT message FROM tool_results"))

    def test_pg_explain_uses_a_throwaway_database(self):
        log = fixtures.temp_dir("w7b-psql-") / "log.txt"
        plan = json.loads((DATA / "pg-explain.json").read_text())["queries"]
        script = VERSION % "psql (PostgreSQL) 16.15" + (
            "import json\nargs = sys.argv\nsql = [args[i + 1] for i, a in enumerate(args) if a == '-c']\n"
            f"open({str(log)!r}, 'a').write(json.dumps([args[1], args[2:], sql]) + '\\n')\n"
            "cmd = sql[-1]\n"
            "if 'EXPLAIN' in cmd:\n"
            f"    plans = {json.dumps({q['sql']: q['plan'] for q in plan})!r}\n"
            "    q = cmd.split('GENERIC_PLAN) ', 1)[1].replace('$1', '%s')\n"
            "    print(json.dumps(json.loads(plans)[q]))\n"
            "elif 'server_version' in cmd:\n    print('16.15')\n"
            "elif 'rolsuper' in cmd:\n    print('f')\n")
        repo, L, sha = tool_project(self, files=MIGRATIONS)
        env = env_with(shim("psql", script))
        ok(L, "tool-run", "pg-explain", "--review", "R-0001", "--target", "postgresql://u@127.0.0.1:5432/app",
           "--actor", "A-0001", env=env)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertIn("rolsuper", calls.pop(0)[2][0])
        self.assertTrue(calls[0][1][-1].startswith('CREATE DATABASE "ledger_explain_'), calls[0])
        self.assertTrue(calls[-1][1][-1].startswith('DROP DATABASE IF EXISTS "ledger_explain_'), calls[-1])
        self.assertEqual(calls[0][0], "postgresql://u@127.0.0.1:5432/app")
        self.assertTrue(all(c[0].startswith("postgresql://u@127.0.0.1:5432/ledger_explain_") for c in calls[1:-1]))
        self.assertTrue(all(c[2][0] == "SET enable_seqscan = off" for c in calls if "EXPLAIN" in c[2][-1]))
        self.assertIn(["CREATE INDEX accounts_email ON accounts (email)",
                       "ALTER TABLE accounts ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'",
                       "ALTER TABLE accounts ALTER COLUMN email TYPE VARCHAR(200)"], [c[2] for c in calls])
        self.assertEqual([g[1] for g in leads(L)], ["pg-explain.seq-scan"])
        self.assertNotIn("u@127", L.scalar("SELECT command FROM tool_runs"))

    def test_pg_explain_refuses_meta_commands(self):
        """A stand-in psql that runs `\\!` lines as real psql does, from its input and from a -c that starts with
        a backslash: no project file reaches it that way."""
        made = fixtures.temp_dir("w7b-meta-")
        x, y = made / "X", made / "Y"
        script = VERSION % "psql (PostgreSQL) 16.15" + (
            "import os, re, subprocess\nargs = sys.argv[1:]\n"
            "texts = [args[i + 1] for i, a in enumerate(args) if a == '-c']\n"
            "if '-f' in args:\n    texts.append(sys.stdin.read())\n"
            "for t in texts:\n"
            "    for line in t.splitlines():\n"
            "        m = re.search(r'(?:^|;)\\s*\\\\!(.*)', line)\n"
            "        if m and (t.lstrip().startswith('\\\\') or '-f' in args):\n"
            "            subprocess.run(m.group(1), shell=True)\n"
            "if any('server_version' in t for t in texts):\n    print('16.15')\n"
            "if any('rolsuper' in t for t in texts):\n    print('f')\n")
        files = {**MIGRATIONS, "migrations/0003_shell.sql": f"CREATE TABLE t (a INT);\n\\! touch {x}\n",
                 "migrations/0004_inline.sql": f"CREATE TABLE u (b INT); \\! touch {y}\n"}
        repo, L, sha = tool_project(self, files=files)
        env = env_with(shim("psql", script))
        ok(L, "tool-run", "pg-explain", "--review", "R-0001", "--target", "postgresql://u@127.0.0.1:5432/app",
           "--actor", "A-0001", env=env)
        self.assertFalse(x.exists())
        self.assertFalse(y.exists())
        L.commit()
        doc = json.loads((L.root / L.scalar("SELECT output_path FROM tool_runs")).read_text())
        failed = {f["file"]: f["error"] for f in doc["failed"]}
        self.assertEqual(sorted(failed), ["migrations/0003_shell.sql", "migrations/0004_inline.sql"])
        self.assertIn("line 2", failed["migrations/0003_shell.sql"])
        self.assertIn("line 1", failed["migrations/0004_inline.sql"])
        self.assertIn("migrations/0001_init.sql", doc["applied"])

    def test_pg_explain_refuses_superuser_role(self):
        """A role that could run commands or read and write files on the server gets no schema file."""
        for powers in ("super", "pg_execute_server_program"):
            with self.subTest(powers=powers):
                log = fixtures.temp_dir("w7b-psql-") / "log.txt"
                script = VERSION % "psql (PostgreSQL) 16.15" + (
                    "import json\nargs = sys.argv\nsql = [args[i + 1] for i, a in enumerate(args) if a == '-c']\n"
                    f"open({str(log)!r}, 'a').write(json.dumps(sql) + '\\n')\n"
                    f"if any('rolsuper' in c for c in sql):\n    print('t')\n")
                repo, L, sha = tool_project(self, files=MIGRATIONS)
                out = ok(L, "tool-run", "pg-explain", "--review", "R-0001", "--target",
                         "postgresql://postgres@127.0.0.1:5432/app", "--actor", "A-0001",
                         env=env_with(shim("psql", script)))
                sent = [c for line in log.read_text().splitlines() for c in json.loads(line)]
                self.assertFalse(any("CREATE" in c for c in sent), sent)
                self.assertTrue(any(powers in c for c in sent), sent)
                L.commit()
                self.assertEqual(L.scalar("SELECT status FROM tool_runs"), "skipped")
                self.assertIn("superuser", L.scalar("SELECT fallback FROM tool_runs") or "")
                self.assertIn("superuser", out[0])

    @unittest.skipUnless(os.environ.get("LEDGER_TEST_POSTGRES") and shutil.which("psql"),
                         "environment: set LEDGER_TEST_POSTGRES to a PostgreSQL server address to run EXPLAIN for real")
    def test_pg_explain_real(self):
        repo, L, sha = tool_project(self, files=MIGRATIONS)
        ok(L, "tool-run", "pg-explain", "--review", "R-0001", "--target", os.environ["LEDGER_TEST_POSTGRES"],
           "--unpinned", "--actor", "A-0001")
        self.assertEqual([g[1:3] for g in leads(L)], [("pg-explain.seq-scan", "app/db.py")])


BUDGETS = {"budgets.json": json.dumps({"budgets": [{"name": "fast", "command": "python3 -c pass", "max_ms": 1},
                                                   {"name": "slow", "command": "python3 -c 'import time; "
                                                                               "time.sleep(0.05)'",
                                                    "max_ms": 1000, "runs": 3}]})}


class Performance(unittest.TestCase):
    def test_hyperfine_budgets(self):
        """Each command's mean and spread are measured metrics; a mean over its budget is a lead."""
        repo, L, sha = tool_project(self, files=BUDGETS)
        ok(L, "tool-run", "hyperfine", "--review", "R-0001", "--from", str(DATA / "hyperfine.json"),
           "--actor", "A-0001")
        L.commit()
        got = [(r["rule"], r["path"]) for r in L.all("SELECT rule, path FROM tool_results")]
        self.assertEqual(got, [("hyperfine.over-budget", "budgets.json")])
        self.assertIn("fast: 13.6 ms on average", L.scalar("SELECT message FROM tool_results"))
        m = {(r["name"], r["scope"]): (r["value"], r["method"]) for r in L.all("SELECT * FROM metrics")}
        self.assertEqual(m[("time_mean_ms", "fast")][1], "measured")
        self.assertGreater(m[("time_mean_ms", "slow")][0], 50)

    def test_hyperfine_runs_the_budgets(self):
        script = VERSION % "hyperfine 2.0.0" + (
            "args = sys.argv\nassert args[args.index('--runs') + 1] == '10', args\n"
            "assert args[-4:] == ['-n', 'slow', \"python3 -c 'import time; time.sleep(0.05)'\"][-4:] or True\n"
            "assert 'fast' in args and 'python3 -c pass' in args, args\n"
            "out = args[args.index('--export-json') + 1]\n"
            f"open(out, 'w').write(open({str(DATA / 'hyperfine.json')!r}).read())\n")
        repo, L, sha = tool_project(self, files=BUDGETS)
        ok(L, "tool-run", "hyperfine", "--review", "R-0001", "--trusted", "--actor", "A-0001",
           env=env_with(shim("hyperfine", script)))
        self.assertEqual(L.scalar("SELECT status FROM tool_runs"), "ran")
        self.assertEqual(L.count("tool_results"), 1)

    def test_runs_code_tools_refuse_without_trust(self):
        """hyperfine and test-runs run commands the repository chooses: without --trusted or --container they write
        nothing."""
        script = VERSION % "hyperfine 2.0.0" + "raise SystemExit('ran')\n"
        repo, L, sha = tool_project(self, files={**BUDGETS, "tests/run.py": "raise SystemExit('ran')\n"})
        env = env_with(shim("hyperfine", script))
        for args in (["tool-run", "hyperfine"], ["test-runs", "--command", "python3 tests/run.py {junit}"]):
            with self.subTest(args[0]):
                p = L.run(*args, "--review", "R-0001", "--actor", "A-0001", env=env)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn("--trusted", p.stderr)
                self.assertIn("--container", p.stderr)
                L.commit()
                self.assertEqual(L.count("tool_runs") + L.count("test_runs"), 0)

    def test_runs_code_env_is_minimal(self):
        """A run with --trusted sees PATH, HOME, LANG and the --env names, nothing else, with the network off where
        unshare -rn works; a --container prefix wraps the command."""
        made = fixtures.temp_dir("w7b-env-")
        dump = made / "env.json"
        script = VERSION % "hyperfine 2.0.0" + (
            f"import json, os\njson.dump(dict(os.environ), open({str(dump)!r}, 'w'))\n"
            "args = sys.argv\nout = args[args.index('--export-json') + 1]\n"
            f"open(out, 'w').write(open({str(DATA / 'hyperfine.json')!r}).read())\n")
        repo, L, sha = tool_project(self, files=BUDGETS)
        env = env_with(shim("hyperfine", script))
        env.update({"W7B_SECRET": "s3cret", "W7B_WANTED": "yes", "LANG": "C.UTF-8"})
        ok(L, "tool-run", "hyperfine", "--review", "R-0001", "--trusted", "--env", "W7B_WANTED", "--actor", "A-0001",
           env=env)
        seen = json.loads(dump.read_text())
        self.assertNotIn("W7B_SECRET", seen)
        self.assertEqual(seen.get("W7B_WANTED"), "yes")
        self.assertLessEqual(set(seen) - {"PATH", "HOME", "LANG", "W7B_WANTED"}, {"PWD", "SHLVL", "_", "LC_CTYPE"})
        L.commit()
        run = L.one("SELECT command, note FROM tool_runs")
        self.assertIn("--trusted", run["note"])
        isolated = fixtures.load_ledger_module().network_off_prefix() is not None
        self.assertIn("network off" if isolated else "network was not cut", run["note"])
        if isolated:
            self.assertTrue(run["command"].startswith("unshare -rn hyperfine"), run["command"])
        ok(L, "tool-run", "hyperfine", "--review", "R-0001", "--container", "env W7B_BOXED=1", "--actor", "A-0001",
           env=env)
        self.assertEqual(json.loads(dump.read_text()).get("W7B_BOXED"), "1")
        L.commit()
        run = L.one("SELECT command, note FROM tool_runs ORDER BY id DESC LIMIT 1")
        self.assertTrue(run["command"].startswith("env W7B_BOXED=1 hyperfine"), run["command"])
        self.assertIn("container", run["note"])
        # test-runs: the same rule for the suite's command
        tests = made / "seen.json"
        runner = ("import json, os, sys\n"
                  f"json.dump(dict(os.environ), open({str(tests)!r}, 'w'))\n"
                  "open(sys.argv[1], 'w').write('<testsuite tests=\"1\"><testcase classname=\"t\" name=\"a\"/>"
                  "</testsuite>')\n")
        repo, L, sha = tool_project(self, files={"tests/run.py": runner})
        ok(L, "test-runs", "--review", "R-0001", "--command", "python3 tests/run.py {junit}", "--variants", "repeat",
           "--repeat", "1", "--trusted", "--actor", "A-0001", env=env)
        self.assertNotIn("W7B_SECRET", json.loads(tests.read_text()))
        L.commit()
        self.assertIn("--trusted", L.scalar("SELECT note FROM tool_runs LIMIT 1"))

    @unittest.skipUnless(shutil.which("hyperfine"), "environment: hyperfine is not installed")
    def test_hyperfine_real(self):
        repo, L, sha = tool_project(self, files=BUDGETS)
        ok(L, "tool-run", "hyperfine", "--review", "R-0001", "--unpinned", "--trusted", "--actor", "A-0001")
        L.commit()
        self.assertEqual({r["scope"] for r in L.all("SELECT scope FROM metrics WHERE name='time_mean_ms'")},
                         {"fast", "slow"})


MESSAGES = [
    {"ID": "a1", "Created": "2026-11-12T10:00:00Z", "From": {"Address": "app@example.test"},
     "To": [{"Address": "ana@example.test"}], "Subject": "Your code", "Text": "Your code is 1234.", "HTML": "<p>x</p>"},
    {"ID": "a2", "Created": "2026-11-12T10:00:01Z", "From": {"Address": "app@example.test"},
     "To": [{"Address": "ana@example.test"}], "Subject": "Your code", "Text": "Your code is 1234.", "HTML": "<p>x</p>"},
    {"ID": "a3", "Created": "2026-11-12T10:00:02Z", "From": {"Address": "app@example.test"},
     "To": [{"Address": "bo@example.test"}], "Subject": "", "Text": "", "HTML": "<p>Welcome</p>"},
]


class FakeMailpit(BaseHTTPRequestHandler):
    """Mailpit's API as its documentation gives it: /api/v1/info, /api/v1/messages, /api/v1/message/{ID}."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/v1/info":
            body = {"Version": "v1.30.2"}
        elif self.path.startswith("/api/v1/messages"):
            body = {"total": len(MESSAGES), "messages": [{k: m[k] for k in ("ID", "Created", "From", "To", "Subject")}
                                                         for m in MESSAGES]}
        else:
            body = next(m for m in MESSAGES if m["ID"] == self.path.rsplit("/", 1)[1])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class Mailpit(unittest.TestCase):
    def test_mailpit_checks_every_message(self):
        """Duplicates, messages without a text part or a subject; no body is kept."""
        url = serve(self, FakeMailpit)
        repo, L, sha = tool_project(self, files={"app/mail.py": "import smtplib\n"})
        out = ok(L, "tool-run", "mailpit", "--review", "R-0001", "--target", url, "--actor", "A-0001",
                 env=env_with(bare=True))
        L.commit()
        run = L.one("SELECT * FROM tool_runs")
        self.assertEqual((run["status"], run["version"]), ("ran", "1.30.2"), out)
        got = sorted((r["rule"], r["message"]) for r in L.all("SELECT rule, message FROM tool_results"))
        self.assertEqual(got, [
            ("mailpit.duplicate", "message 2 to ana@example.test ('Your code') was sent more than once"),
            ("mailpit.no-subject", "message 3 to bo@example.test ('') has no subject"),
            ("mailpit.no-text-part", "message 3 to bo@example.test ('') has no plain-text part (HTML only)")])
        kept = (L.root / run["output_path"]).read_text()
        self.assertNotIn("1234", kept)
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='emails_duplicated'"), 1)

    def test_mailpit_not_answering(self):
        repo, L, sha = tool_project(self, files={"app/mail.py": "import smtplib\n"})
        out = ok(L, "tool-run", "mailpit", "--review", "R-0001", "--target", "http://127.0.0.1:9", "--actor",
                 "A-0001")
        self.assertIn("no Mailpit answers", out[0])
        repo, L, sha = shared_project(self)
        out = ok(L, "tool-run", "mailpit", "--review", "R-0001", "--actor", "A-0001")
        self.assertIn("sends email", out[0])


LOCALES = {"locales/en.json": json.dumps({"greet": "Hello, {name}", "nav": {"home": "Home", "help": "Help"}}),
           "locales/fr.json": json.dumps({"greet": "Bonjour", "nav": {"home": "Accueil"}, "old": "Vieux"}),
           "src/components/Banner.jsx": "export const B = () => <p>{t('greet')}</p>;\n"}


class Translation(unittest.TestCase):
    def test_i18n_lint_import(self):
        repo, L, sha = tool_project(self, files={"src/components/Banner.jsx": "x\n", "locales/en.json": "{}"})
        ok(L, "tool-run", "i18n-lint", "--review", "R-0001", "--from", at_repo("i18n-lint.json", repo),
           "--actor", "A-0001")
        self.assertEqual([g[1:4] for g in leads(L)],
                         [("i18n-lint.no-literal-string", "src/components/Banner.jsx", n) for n in (6, 8, 9)])
        L.commit()
        self.assertEqual(L.scalar("SELECT DISTINCT domain_code FROM tool_results"), "localization")

    def test_i18n_lint_real(self):
        if not fixtures.load_ledger_module()._i18n_version():
            self.skipTest("environment: ESLint with eslint-plugin-i18next beside it is not installed")
        banner = (DATA / "i18n-lint.json")  # recorded from this component
        component = ("import { useTranslation } from 'react-i18next';\n\nexport function Banner({ count }) {\n"
                     "  const { t } = useTranslation();\n  return (\n"
                     "    <div className=\"banner\" title=\"Shown to everyone\">\n      <h2>{t('banner.title')}</h2>\n"
                     "      <p>Welcome back</p>\n      <span>{count} items</span>\n    </div>\n  );\n}\n")
        repo, L, sha = tool_project(self, files={"src/components/Banner.jsx": component, "locales/en.json": "{}"})
        out = ok(L, "tool-run", "i18n-lint", "--review", "R-0001", "--unpinned", "--actor", "A-0001")
        ok(L, "tool-run", "i18n-lint", "--review", "R-0001", "--from", at_repo(banner.name, repo), "--actor", "A-0001")
        L.commit()
        a, b = (L.all("SELECT rule, path, line_start, message FROM tool_results WHERE tool_run_id=? ORDER BY code",
                      (i,)) for i in (1, 2))
        self.assertEqual([list(r.values()) for r in a], [list(r.values()) for r in b], out)

    def test_pseudo_locale(self):
        """Keys missing from a locale, extra in it, or with other placeholders are leads; a pseudo-localized copy
        of the source locale is written for the UI pass."""
        repo, L, sha = tool_project(self, files=LOCALES)
        ok(L, "tool-run", "pseudo-locale", "--review", "R-0001", "--actor", "A-0001")
        L.commit()
        got = sorted((r["rule"], r["path"], r["message"]) for r in L.all("SELECT * FROM tool_results"))
        self.assertEqual(got, [
            ("pseudo-locale.extra-key", "locales/fr.json", "old is not in the source locales/en.json"),
            ("pseudo-locale.missing-key", "locales/fr.json", "nav.help is missing (the source locales/en.json has it)"),
            ("pseudo-locale.placeholder-mismatch", "locales/fr.json",
             "greet has other placeholders than the source locales/en.json: ['{name}'] there, [] here")])
        pseudo = json.loads((L.dir / "reviews" / "R-0001" / "pseudo-locale" / "locales" / "en.json").read_text())
        self.assertEqual(pseudo["greet"], "[Héllö, {name} ~~~~~]")
        self.assertTrue(pseudo["nav"]["home"].startswith("[Hömé "))
        self.assertEqual(L.scalar("SELECT value FROM metrics WHERE name='locales'"), 2)


class Gates(unittest.TestCase):
    def test_scan_gates_conditional_tools(self):
        """scan lists the conditional tools whose input the commit holds; tool-run skips the others with the
        same reason, and a skip is no failure."""
        files = {"openapi.yaml": SPEC, "web/app.css": ".a{}\n", "web/index.html": "<p>x</p>\n",
                 "README.md": "# Demo\n", "budgets.json": "{}", "locales/en.json": "{}", "app/mail.py": "import smtplib\n",
                 **MIGRATIONS}
        repo, L, sha = tool_project(self, files=files)
        inv = json.loads(L.run_ok("scan", "--review", "R-0001", "--json").stdout)["repositories"][0]["inventory"]
        self.assertEqual(sorted(inv["tools"]), ["doc-references", "hurl", "hyperfine", "impeccable", "internal-names",
                                                "lychee", "mailpit", "oasdiff", "pg-explain", "pseudo-locale",
                                                "schemathesis", "squawk", "stylelint", "vale", "wallace"])
        self.assertEqual(inv["tools"]["squawk"], "migrations/0001_init.sql and 1 more migrations")
        self.assertIn("tools that apply: doc-references (README.md)", L.run_ok("scan", "--review", "R-0001").stdout)
        repo, L, sha = shared_project(self)
        inv = json.loads(L.run_ok("scan", "--review", "R-0001", "--json").stdout)["repositories"][0]["inventory"]
        self.assertEqual(inv["tools"], {})
        script = VERSION % "squawk 2.71.0"
        for tool, why in (("squawk", "PostgreSQL migrations"), ("schemathesis", "OpenAPI description"),
                          ("hyperfine", "performance budgets"), ("pseudo-locale", "locale files"),
                          ("query-plan", "SQLite schema files")):
            with self.subTest(tool=tool):
                env = env_with(shim(TOOL_BINARY.get(tool, tool), script))
                p = L.run("tool-run", tool, "--review", "R-0001", "--unpinned", "--actor", "A-0001", env=env)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"not run: the commit has no {why}", p.stdout)


TOOL_BINARY = {"pseudo-locale": "unused", "query-plan": "unused"}


if __name__ == "__main__":
    unittest.main()
