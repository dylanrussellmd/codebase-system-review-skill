"""The tool documents itself (plan P-0001 steps 205, 206 and 211).

test_every_function_has_docstring   every function and method of dev/core.py says what it does (a write command's
                                    @_command help counts; __init__ and other dunders are the class's)
test_no_bare_magic_numbers          timeouts, row limits and sample sizes in dev/core.py are named constants
test_route_table_documented         route()'s docstring lists every address of ROUTES
test_help_lists_every_command       --help and the module docstring name every command; every command and every
                                    argument has help text
test_routes_documented              dev/page.html's header comment lists every view's address
test_page_constants_named           the page's list sizes and short commit length are named constants
"""
import argparse
import ast
import io
import re
import sys
import unittest
from contextlib import redirect_stdout

from dev.tests import fixtures

CORE = (fixtures.REPO_ROOT / "dev" / "core.py").read_text(encoding="utf-8")
PAGE = (fixtures.REPO_ROOT / "dev" / "page.html").read_text(encoding="utf-8")


def parser():
    """main()'s argument parser, as built for a run (captured where it would parse)."""
    mod = fixtures.load_ledger_module()
    got = {}

    def grab(self, *a, **k):
        got["p"] = self
        raise SystemExit(0)

    real, argv = argparse.ArgumentParser.parse_args, sys.argv
    argparse.ArgumentParser.parse_args, sys.argv = grab, ["ledger.py"]
    try:
        mod._main()
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args, sys.argv = real, argv
    return got["p"], mod


def subcommands(ap):
    return next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))


class CoreDocs(unittest.TestCase):
    def test_every_function_has_docstring(self):
        missing = []

        def walk(node, in_class):
            for n in ast.iter_child_nodes(node):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    command = any(isinstance(d, ast.Call) and getattr(d.func, "id", "") == "_command"
                                  for d in n.decorator_list)
                    dunder = in_class and n.name.startswith("__") and n.name.endswith("__")
                    if not (ast.get_docstring(n) or command or dunder):
                        missing.append(f"{n.lineno} {n.name}")
                    if isinstance(n, ast.ClassDef):
                        walk(n, True)   # methods; functions inside a function are its own business
                elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(n, in_class)

        walk(ast.parse(CORE), False)
        self.assertEqual(missing, [])

    def test_no_bare_magic_numbers(self):
        code = "\n".join(l for l in CORE.splitlines() if not l.lstrip().startswith("#"))
        found = re.findall(r"timeout=\d+(?:\.\d+)?|LIMIT (?:[2-9]|\d{2,})\b|fetchmany\(\d+\)|Timer\(\d", code)
        self.assertEqual(found, [], "name the number as a module constant with a one-line reason")

    def test_route_table_documented(self):
        mod = fixtures.load_ledger_module()
        doc = mod.route.__doc__
        for r in mod.ROUTES:
            shown = "/".join("<" if p == "*" else p for p in r.pattern)
            line = re.sub(r"<[^>]*>", "<", doc)
            self.assertRegex(line, r"\n\s+%s\s+%s(?!\S)" % (r.method, re.escape(shown)), f"{r.method} {shown}")


class Help(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ap, cls.mod = parser()
        cls.sub = subcommands(cls.ap)

    def test_help_lists_every_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.ap.print_help()
        helps = {c.dest: c.help for c in self.sub._choices_actions}
        doc = self.mod.__doc__
        for name, p in self.sub.choices.items():
            with self.subTest(command=name):
                self.assertIn(name, out.getvalue())
                self.assertRegex(doc, r"(?<![\w-])%s(?![\w-])" % re.escape(name), "the module docstring")
                self.assertTrue(helps.get(name), "no help for the command")
                bare = [a.dest for a in p._actions if not a.help and a.dest != "help"]
                self.assertEqual(bare, [], "arguments without help")
        bare = [a.dest for a in self.ap._actions if not a.help and a.dest not in ("help", "cmd")]
        self.assertEqual(bare, [])


class PageDocs(unittest.TestCase):
    HEADER = PAGE[PAGE.index('"use strict";'):PAGE.index("let TOKEN")]

    def test_routes_documented(self):
        views = re.findall(r"^VIEWS\.(\w+) =", PAGE, re.M)
        self.assertGreater(len(views), 10)
        for v in views:
            self.assertRegex(self.HEADER, r"#/%s\b" % ("" if v == "home" else v), v)

    def test_page_constants_named(self):
        script = PAGE[PAGE.index('"use strict";'):]
        for name in ("SHORT_SHA", "REVIEW_FIRST", "REVIEW_MORE", "PLAN_PAGE", "EXPLORE_ROWS", "PAGE_ROWS"):
            self.assertRegex(script, r"const %s = \d+;\s+// \S" % name, name)
        self.assertNotRegex(script, r"sha\.slice\(0, 10\)|const FIRST = 100|const PAGE_OF = 300|limit=100&")


if __name__ == "__main__":
    unittest.main()
