from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


REFERENCE_TEMPLATE = "reference-workflow"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_ROOT = _REPO_ROOT / "examples" / REFERENCE_TEMPLATE
_EXCLUDED_TEMPLATE_FILES = frozenset({Path("README.md")})


@dataclass(frozen=True)
class InitResult:
    template: str
    target_root: Path
    copied: tuple[Path, ...]
    skipped: tuple[Path, ...]
    conflicts: tuple[Path, ...]
    dry_run: bool
    force: bool


class InitWorkflowError(RuntimeError):
    """Base error for target-repo initialization failures."""


class UnknownTemplateError(InitWorkflowError):
    def __init__(self, template: str) -> None:
        super().__init__(
            f"Unknown template {template!r}. Available templates: {REFERENCE_TEMPLATE}."
        )
        self.template = template


class InitConflictError(InitWorkflowError):
    def __init__(self, conflicts: tuple[Path, ...]) -> None:
        super().__init__("Target files already exist with different content.")
        self.conflicts = conflicts


def template_files(template_root: Path = _TEMPLATE_ROOT) -> tuple[Path, ...]:
    files = []
    for path in template_root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(template_root)
        if relative_path in _EXCLUDED_TEMPLATE_FILES:
            continue
        files.append(relative_path)
    return tuple(sorted(files))


def init_reference_workflow(
    target_root: Path,
    *,
    template: str = REFERENCE_TEMPLATE,
    dry_run: bool = False,
    force: bool = False,
) -> InitResult:
    if template != REFERENCE_TEMPLATE:
        raise UnknownTemplateError(template)
    if not _TEMPLATE_ROOT.is_dir():
        raise InitWorkflowError(f"Template directory not found: {_TEMPLATE_ROOT}")

    target_root = target_root.expanduser().resolve()
    copied: list[Path] = []
    skipped: list[Path] = []
    conflicts: list[Path] = []

    for relative_path in template_files():
        source_path = _TEMPLATE_ROOT / relative_path
        target_path = target_root / relative_path
        if not target_path.exists():
            copied.append(relative_path)
            continue
        if target_path.is_dir():
            conflicts.append(relative_path)
            continue
        if force:
            copied.append(relative_path)
            continue
        if target_path.read_bytes() == source_path.read_bytes():
            skipped.append(relative_path)
            continue
        conflicts.append(relative_path)

    result = InitResult(
        template=template,
        target_root=target_root,
        copied=tuple(copied),
        skipped=tuple(skipped),
        conflicts=tuple(conflicts),
        dry_run=dry_run,
        force=force,
    )

    if conflicts and not dry_run:
        raise InitConflictError(result.conflicts)
    if dry_run:
        return result

    target_root.mkdir(parents=True, exist_ok=True)
    for relative_path in copied:
        source_path = _TEMPLATE_ROOT / relative_path
        target_path = target_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    return result
