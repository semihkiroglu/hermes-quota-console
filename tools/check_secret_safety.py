#!/usr/bin/env python3
"""Fail CI when source contains credentials or forbidden auth-state files."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
from typing import Iterable


_FORBIDDEN_FILENAMES = frozenset(
    {
        ".env",
        "auth.json",
        "config.yaml",
        "credentials.json",
        "codex_usage_state.json",
    }
)
_FORBIDDEN_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
_EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "build", "dist"}
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~-]{20,}\b"),
    re.compile(
        r"""(?ix)
        ["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)["']?
        \s*[:=]\s*
        (?:[rubf]{0,2})?["'][A-Za-z0-9._~-]{24,}["']
        """
    ),
)


def _tracked_files(root: Path) -> list[Path] | None:
    """Use Git source files, including non-ignored local additions, when available."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return [root / Path(item.decode("utf-8")) for item in completed.stdout.split(b"\0") if item]


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if any(part in _EXCLUDED_DIRECTORIES for part in relative_parts):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


def _is_forbidden(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in _FORBIDDEN_FILENAMES
        or name.startswith(".env.")
        or path.suffix.lower() in _FORBIDDEN_SUFFIXES
    )


def _has_credential_shaped_literal(path: Path) -> bool:
    try:
        contents = path.read_bytes()
    except OSError:
        return True
    if b"\0" in contents:
        return False
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)


def _source_files(root: Path) -> Iterable[Path]:
    tracked = _tracked_files(root)
    return tracked if tracked is not None else _walk_files(root)


def check(root: Path) -> list[str]:
    """Return safe diagnostics that never include source content or matches."""
    diagnostics: list[str] = []
    for path in sorted(_source_files(root)):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _is_forbidden(path):
            diagnostics.append(f"forbidden file: {relative}")
            continue
        if _has_credential_shaped_literal(path):
            diagnostics.append(f"credential-shaped literal: {relative}")
    return diagnostics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("--root must be a directory")
    diagnostics = check(root)
    for diagnostic in diagnostics:
        print(f"secret safety: {diagnostic}")
    return 1 if diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
