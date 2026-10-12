#!/usr/bin/env python3
"""Check or increment the idea-to-insight patch version."""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SKILL_VERSION_RE = re.compile(r'(?m)^  version: "([0-9]+\.[0-9]+\.[0-9]+)"$')
README_VERSION_RE = re.compile(r"(?m)^\*\*Current version:\*\* `v([0-9]+\.[0-9]+\.[0-9]+)`[ \t]*$")


class VersionError(Exception):
    pass


def _read_state(root: Path) -> tuple[str, str, str, str, str]:
    version_path = root / "VERSION"
    skill_path = root / "SKILL.md"
    readme_path = root / "README.md"
    version = version_path.read_text(encoding="utf-8").strip()
    if VERSION_RE.fullmatch(version) is None:
        raise VersionError("VERSION must contain exactly MAJOR.MINOR.PATCH, without a v prefix.")
    skill_text = skill_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    skill_matches = list(SKILL_VERSION_RE.finditer(skill_text))
    readme_matches = list(README_VERSION_RE.finditer(readme_text))
    if len(skill_matches) != 1:
        raise VersionError("SKILL.md must contain exactly one metadata version field.")
    if len(readme_matches) != 1:
        raise VersionError("README.md must contain exactly one Current version line.")
    skill_version = skill_matches[0].group(1)
    readme_version = readme_matches[0].group(1)
    if skill_version != version or readme_version != version:
        raise VersionError(
            "Version mismatch: VERSION={}, SKILL.md={}, README.md={}. "
            "Resolve the mismatch before bumping.".format(version, skill_version, readme_version)
        )
    return version, skill_text, readme_text, skill_matches[0].group(0), readme_matches[0].group(0)


def _atomic_write(path: Path, content: str) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=str(path.parent), delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(content)
        os.replace(temp_name, str(path))
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def check(root: Path) -> str:
    version, _skill_text, _readme_text, _skill_line, _readme_line = _read_state(root)
    return version


def bump(root: Path) -> tuple[str, str]:
    version, skill_text, readme_text, skill_line, readme_line = _read_state(root)
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise VersionError("Invalid version.")
    major, minor, patch = (int(part) for part in match.groups())
    next_version = "{}.{}.{}".format(major, minor, patch + 1)
    skill_next = '  version: "{}"'.format(next_version)
    readme_next = "**Current version:** `v{}`".format(next_version)
    _atomic_write(root / "SKILL.md", skill_text.replace(skill_line, skill_next, 1))
    _atomic_write(root / "README.md", readme_text.replace(readme_line, readme_next, 1))
    _atomic_write(root / "VERSION", next_version + "\n")
    return version, next_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "bump"), help="check copies or increment PATCH by one")
    args = parser.parse_args()
    try:
        if args.action == "check":
            print("Version check passed: v{}".format(check(ROOT)))
        else:
            previous, current = bump(ROOT)
            print("Version bumped: v{} -> v{}".format(previous, current))
    except (OSError, VersionError) as exc:
        print("Version error: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
