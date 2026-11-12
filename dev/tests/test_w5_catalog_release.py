"""The release bundle (plan P-0001 step 210): dev/release.py, LICENSE, and the README's canonical copy.

test_bundle_contents_and_frontmatter  the zip's top folder equals SKILL.md's name:, its entries are exactly SKILL.md,
                                      README.md, LICENSE, scripts/ledger.py and references/, and scripts/ledger.py
                                      inside equals a fresh build
test_bundle_is_reproducible           two bundles of the same sources are byte-identical
test_frontmatter_rules_refuse         a wrong name, a missing or overlong description, no version: exit 2, no zip
test_license_is_mit                   LICENSE is the MIT licence
test_readme_names_the_canonical_copy  README names the GitHub repository and the release command
"""
import importlib.util
import re
import subprocess
import sys
import unittest
import zipfile

from dev.tests import fixtures
from dev.tests.test_docs import copy_repo

REPO = fixtures.REPO_ROOT
NAME = "codebase-system-review"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def release(root, *args):
    return subprocess.run([sys.executable, str(root / "dev" / "release.py"), "--root", str(root), *args],
                          capture_output=True, text=True, timeout=120, env=fixtures.git_env())


class Bundle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = copy_repo(fixtures.temp_dir("release-") / NAME)

    def bundle(self, root=None):
        root = root or self.root
        p = release(root)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        zips = sorted((root / "dist").glob(f"{NAME}-*.zip"))
        self.assertEqual(len(zips), 1, zips)
        return zips[0], p

    def test_bundle_contents_and_frontmatter(self):
        path, p = self.bundle()
        version = load(self.root / "dev" / "build.py", "build_v").skill_version(self.root)
        self.assertEqual(path.name, f"{NAME}-{version}.zip")
        self.assertIn(path.name, p.stdout)
        refs = sorted(f.relative_to(self.root).as_posix() for f in (self.root / "references").rglob("*")
                      if f.is_file() and "__pycache__" not in f.parts)
        want = sorted(["SKILL.md", "README.md", "LICENSE", "scripts/ledger.py"] + refs)
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            self.assertEqual({n.split("/", 1)[0] for n in names}, {NAME}, "one top folder, named as SKILL.md's name:")
            self.assertEqual(sorted(n.split("/", 1)[1] for n in names), want)
            built = load(self.root / "dev" / "build.py", "build_b").build(self.root)
            self.assertEqual(z.read(f"{NAME}/scripts/ledger.py").decode("utf-8"), built)
            self.assertEqual(z.read(f"{NAME}/SKILL.md"), (self.root / "SKILL.md").read_bytes())
            skill = z.read(f"{NAME}/SKILL.md").decode("utf-8")
        front = skill.split("---\n", 2)[1]
        self.assertRegex(front, rf"(?m)^name: {NAME}$")
        desc = re.search(r"(?m)^description: (.+)$", front).group(1)
        self.assertLessEqual(len(desc), 1024)
        self.assertRegex(front, r"(?m)^version: \d+\.\d+\.\d+")

    def test_bundle_is_reproducible(self):
        first, _ = self.bundle()
        a = first.read_bytes()
        second, _ = self.bundle()
        self.assertEqual(a, second.read_bytes())

    def test_frontmatter_rules_refuse(self):
        cases = {
            "name: other-name\n": "name",
            "description: \n": "description",
            "description: " + "x" * 1100 + "\n": "description",
            "": "version",
        }
        for repl, word in cases.items():
            with self.subTest(change=repl[:30] or "no version"):
                root = copy_repo(fixtures.temp_dir("release-bad-") / NAME)
                skill = (root / "SKILL.md").read_text(encoding="utf-8")
                key = repl.split(":", 1)[0] if repl else "version"
                skill = re.sub(rf"(?m)^{key}:.*\n", repl, skill, count=1)
                (root / "SKILL.md").write_text(skill, encoding="utf-8")
                p = release(root)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(word, p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertFalse(list((root / "dist").glob("*.zip")) if (root / "dist").exists() else [])

    def test_folder_name_must_match(self):
        root = copy_repo(fixtures.temp_dir("release-dir-") / "some-clone")
        path, _ = self.bundle(root)   # the clone's folder name does not matter: the zip's top folder is name:
        with zipfile.ZipFile(path) as z:
            self.assertTrue(all(n.startswith(NAME + "/") for n in z.namelist()))


class Licence(unittest.TestCase):

    def test_license_is_mit(self):
        text = (REPO / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertRegex(text, r"Copyright \(c\) \d{4} \S")
        self.assertIn("Permission is hereby granted, free of charge, to any person obtaining a copy", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND', text)

    def test_readme_names_the_canonical_copy(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/dylanrussellmd/codebase-system-review-skill", readme)
        self.assertIn("python3 dev/release.py", readme)
        self.assertIn("dist/", (REPO / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
