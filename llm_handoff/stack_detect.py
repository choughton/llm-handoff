"""Detect candidate build and verification commands in a target repository.

Detection only reads files. It never runs a subprocess, never ranks the
candidates, and never picks one; `llm-handoff init` prints them and the
operator confirms a command with `--verification-command`.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

_MAKE_TARGET_NAMES = frozenset({"test", "check", "build", "lint"})
_MAKE_TARGET_PREFIXES = ("test-", "test_")


@dataclass(frozen=True)
class Candidate:
    """One detected command and the file it came from (POSIX, root-relative)."""

    command: str
    source: str


def detect_candidates(root: Path) -> tuple[Candidate, ...]:
    """Return candidate commands found under ``root``, in source order."""

    root = Path(root)
    if not root.is_dir():
        return ()
    found: list[Candidate] = []
    found.extend(_workflow_candidates(root))
    found.extend(_package_json_candidates(root))
    found.extend(_pyproject_candidates(root))
    found.extend(_makefile_candidates(root))
    found.extend(_marker_candidates(root, "Cargo.toml", ("cargo build", "cargo test")))
    found.extend(_marker_candidates(root, "go.mod", ("go build ./...", "go test ./...")))
    return tuple(dict.fromkeys(found))


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _source(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _workflow_candidates(root: Path) -> Iterator[Candidate]:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return
    files = sorted(
        (
            path
            for pattern in ("*.yml", "*.yaml")
            for path in workflows.glob(pattern)
            if path.is_file()
        ),
        key=lambda path: path.name,
    )
    for path in files:
        text = _read_text(path)
        if text is None:
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        source = _source(root, path)
        for command in _workflow_run_lines(data):
            yield Candidate(command=command, source=source)


def _workflow_run_lines(data: object) -> Iterator[str]:
    if not isinstance(data, dict):
        return
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for line in run.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    yield stripped


def _package_json_candidates(root: Path) -> Iterator[Candidate]:
    path = root / "package.json"
    text = _read_text(path)
    if text is None:
        return
    try:
        data = json.loads(text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return
    source = _source(root, path)
    for name in scripts:
        yield Candidate(command=f"npm run {name}", source=source)


def _pyproject_candidates(root: Path) -> Iterator[Candidate]:
    path = root / "pyproject.toml"
    text = _read_text(path)
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return
    pytest_table = tool.get("pytest")
    # `[tool.pytest.ini_options]` also creates the `tool.pytest` table.
    if isinstance(pytest_table, dict):
        yield Candidate(command="python -m pytest", source=_source(root, path))


def _makefile_candidates(root: Path) -> Iterator[Candidate]:
    path = root / "Makefile"
    text = _read_text(path)
    if text is None:
        return
    source = _source(root, path)
    for target in _make_targets(text.splitlines()):
        yield Candidate(command=f"make {target}", source=source)


def _make_targets(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        if not line or line[0] in " \t#":
            continue
        colon = line.find(":")
        if colon <= 0:
            continue
        head, rest = line[:colon], line[colon:]
        # `X := y` and `X ::= y` are assignments; `X = a:b` has `=` in the head.
        if rest.startswith((":=", "::=")) or "=" in head:
            continue
        for name in head.split():
            if name in _MAKE_TARGET_NAMES or name.startswith(_MAKE_TARGET_PREFIXES):
                yield name


def _marker_candidates(
    root: Path, filename: str, commands: tuple[str, ...]
) -> Iterator[Candidate]:
    path = root / filename
    if not path.is_file():
        return
    source = _source(root, path)
    for command in commands:
        yield Candidate(command=command, source=source)
