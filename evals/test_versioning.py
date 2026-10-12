#!/usr/bin/env python3
"""Tests for version consistency and patch-only bumps."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bump_version.py"


class VersioningTests(unittest.TestCase):
    def make_fixture(self, root, version="0.0.1"):
        (root / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, root / "scripts" / "bump_version.py")
        (root / "VERSION").write_text(version + "\n", encoding="utf-8")
        (root / "SKILL.md").write_text(
            "---\nname: idea-to-insight\nmetadata:\n  version: \"{}\"\n---\n".format(version),
            encoding="utf-8",
        )
        (root / "README.md").write_text(
            "# Idea to Insight\n\n**Current version:** `v{}`\n".format(version),
            encoding="utf-8",
        )

    def run_script(self, root, action):
        return subprocess.run(
            [sys.executable, str(root / "scripts" / "bump_version.py"), action],
            cwd=str(root), text=True, capture_output=True, check=False,
        )

    def test_current_version_copies_are_consistent(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "check"],
            cwd=str(ROOT), text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v0.0.1", result.stdout)

    def test_bump_increments_patch_and_synchronizes_all_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_fixture(root)
            result = self.run_script(root, "bump")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "0.0.2")
            self.assertIn('version: "0.0.2"', (root / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("Current version:** `v0.0.2`", (root / "README.md").read_text(encoding="utf-8"))
            checked = self.run_script(root, "check")
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_bump_refuses_to_overwrite_an_existing_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_fixture(root)
            (root / "README.md").write_text("# mismatched version\n", encoding="utf-8")
            result = self.run_script(root, "bump")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
