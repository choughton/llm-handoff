from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

REFERENCE_TEMPLATE = "reference-workflow"
_PACKAGE_NAME = "llm_handoff"
# Wheels carry the template under llm_handoff/templates/ (pyproject.toml maps
# examples/reference-workflow/ there at build time).
_PACKAGED_TEMPLATES_DIR = "templates"
_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent


def _packaged_template_root(template: str = REFERENCE_TEMPLATE) -> Path | None:
    """Return the template bundled with the installed package, if present.

    Only a template that sits beside this module counts, so a source checkout
    never picks up a template from a different installed copy of the package.
    """

    try:
        candidate = resources.files(_PACKAGE_NAME) / _PACKAGED_TEMPLATES_DIR / template
        path = Path(os.fspath(candidate)).resolve()
    except (ModuleNotFoundError, TypeError, ValueError, OSError):
        return None
    if not path.is_dir():
        return None
    if path.parent.parent != _PACKAGE_DIR:
        return None
    return path


def resolve_template_root(template: str = REFERENCE_TEMPLATE) -> Path:
    """Locate a template: installed package data first, then the source layout."""

    packaged = _packaged_template_root(template)
    if packaged is not None:
        return packaged
    return _REPO_ROOT / "examples" / template


_TEMPLATE_ROOT = resolve_template_root()
_EXCLUDED_TEMPLATE_FILES = frozenset({Path("README.md")})

# Provider mappings `init` can write into dispatch_config.yaml. `reference`
# keeps the template's mixed mapping; `claude` puts every role on Claude Code.
REFERENCE_MAPPING = "reference"
CLAUDE_MAPPING = "claude"
PROVIDER_MAPPINGS = (CLAUDE_MAPPING,)
PROVIDER_CLIS = ("claude", "codex", "gemini")
_CONFIG_PATH = Path("dispatch_config.yaml")
_TEXT_MARKERS = {
    Path("AGENTS.md"): (b"<!-- llm-handoff:begin -->", b"<!-- llm-handoff:end -->"),
    Path(".gitignore"): (b"# llm-handoff:begin", b"# llm-handoff:end"),
    Path(".geminiignore"): (b"# llm-handoff:begin", b"# llm-handoff:end"),
}
_SETTINGS_PATH = Path(".claude/settings.json")
_GEMINIIGNORE_BLOCK = b""".git
node_modules
venv
.venv
__pycache__
dist
build
out
.next
logs
.pytest_cache
htmlcov
.ruff_cache
.tox
coverage
.vscode
.env
.env.*
*.db
*.db-shm
*.db-wal
*.db-journal
package-lock.json
"""

_CLAUDE_AGENTS_BLOCK = """agents:
  # All-Claude mapping written by `llm-handoff init` (Claude Code only).
  # Each role runs the matching subagent in .claude/agents/.
  planner:
    provider: claude
    binary: claude
    resume: false
    timeout_ms: 1200000

  backend:
    provider: claude
    binary: claude
    resume: false
    timeout_ms: 1200000

  frontend:
    provider: claude
    binary: claude
    resume: false
    timeout_ms: 1200000

  auditor:
    provider: claude
    binary: claude
    model: claude-opus-5-5
    resume: false
    timeout_ms: 900000

  validator:
    provider: claude
    binary: claude
    model: claude-haiku-5-5
    resume: false
    timeout_ms: 900000

  finalizer:
    provider: claude
    binary: claude
    model: claude-opus-5-5
    resume: false
    timeout_ms: 900000

"""
_REFERENCE_SECURITY_PROVIDER_LINE = (
    "  # security_provider: claude   # claude | gemini | codex; unset = auto\n"
)
_CLAUDE_SECURITY_PROVIDER_LINE = (
    "  # Same vendor as the implementer: the only CLI available.\n"
    "  security_provider: claude\n"
)


@dataclass(frozen=True)
class InitResult:
    template: str
    target_root: Path
    copied: tuple[Path, ...]
    skipped: tuple[Path, ...]
    conflicts: tuple[Path, ...]
    dry_run: bool
    force: bool
    provider_mapping: str = REFERENCE_MAPPING
    detected_providers: tuple[str, ...] = ()
    merged: tuple[Path, ...] = ()


class InitWorkflowError(RuntimeError):
    """Base error for target-repo initialization failures."""


class UnknownTemplateError(InitWorkflowError):
    def __init__(self, template: str) -> None:
        super().__init__(
            f"Unknown template {template!r}. Available templates: {REFERENCE_TEMPLATE}."
        )
        self.template = template


class UnknownProviderError(InitWorkflowError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Unknown provider mapping {provider!r}. "
            f"Available mappings: {', '.join(PROVIDER_MAPPINGS)}."
        )
        self.provider = provider


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
        # The template ships a Python hook script; never copy bytecode caches
        # that a local run may have left beside it.
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        files.append(relative_path)
    return tuple(sorted(files))


def detect_provider_clis() -> tuple[str, ...]:
    """Return the provider CLIs found on PATH, in a stable order."""

    return tuple(name for name in PROVIDER_CLIS if shutil.which(name))


def resolve_provider_mapping(
    provider: str | None,
    available_providers: Iterable[str],
) -> str:
    """Pick the mapping to write: an explicit choice, else auto-detect.

    Auto-detection only switches to the all-Claude mapping when Claude Code
    is the only provider CLI installed; any other combination keeps the
    reference mapping.
    """

    if provider is not None:
        if provider not in PROVIDER_MAPPINGS:
            raise UnknownProviderError(provider)
        return provider
    if set(available_providers) == {"claude"}:
        return CLAUDE_MAPPING
    return REFERENCE_MAPPING


def render_config(template_text: str, mapping: str) -> str:
    """Return dispatch_config.yaml text for a provider mapping."""

    if mapping == REFERENCE_MAPPING:
        return template_text
    if mapping != CLAUDE_MAPPING:
        raise UnknownProviderError(mapping)
    start = template_text.index("agents:\n")
    end = template_text.index("normalizer:\n", start)
    rendered = template_text[:start] + _CLAUDE_AGENTS_BLOCK + template_text[end:]
    if rendered.count(_REFERENCE_SECURITY_PROVIDER_LINE) != 1:
        raise InitWorkflowError(
            "Template dispatch_config.yaml no longer has the expected "
            "audit.security_provider line."
        )
    return rendered.replace(
        _REFERENCE_SECURITY_PROVIDER_LINE,
        _CLAUDE_SECURITY_PROVIDER_LINE,
    )


def _source_bytes(relative_path: Path, mapping: str) -> bytes:
    source_path = _TEMPLATE_ROOT / relative_path
    if relative_path == _CONFIG_PATH and mapping != REFERENCE_MAPPING:
        text = source_path.read_text(encoding="utf-8")
        return render_config(text, mapping).encode("utf-8")
    return source_path.read_bytes()


def _marker_span(content: bytes, begin: bytes, end: bytes) -> tuple[int, int] | None:
    if begin not in content and end not in content:
        return None
    if content.count(begin) != 1 or content.count(end) != 1:
        raise ValueError("Expected exactly one managed marker pair")
    start = content.index(begin)
    stop = content.index(end) + len(end)
    if start >= stop - len(end):
        raise ValueError("Reversed managed markers")
    for offset, marker in ((start, begin), (stop - len(end), end)):
        tail = content[offset + len(marker) :]
        if (offset and content[offset - 1 : offset] != b"\n") or (
            tail and not tail.startswith((b"\n", b"\r\n"))
        ):
            raise ValueError("Managed markers must occupy separate lines")
    return start, stop


def _merge_text(path: Path, existing: bytes, source: bytes) -> bytes:
    begin, end = _TEXT_MARKERS[path]
    unmarked_template = existing == source
    if path == Path(".geminiignore"):
        source = _GEMINIIGNORE_BLOCK
    source_span = _marker_span(source, begin, end)
    block = (
        source[source_span[0] : source_span[1]]
        if source_span
        else begin + b"\n" + source.rstrip(b"\r\n") + b"\n" + end
    )
    span = _marker_span(existing, begin, end)
    if span:
        return existing[: span[0]] + block + existing[span[1] :]
    if unmarked_template:
        return block + b"\n"
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    return existing + separator + block + b"\n"


def _merge_settings(existing: bytes, source: bytes) -> bytes:
    target = json.loads(existing)
    template = json.loads(source)
    if not isinstance(target, dict):
        raise TypeError("Settings must be an object")
    permissions = target.setdefault("permissions", {})
    hooks = target.setdefault("hooks", {})
    if not isinstance(permissions, dict) or not isinstance(hooks, dict):
        raise TypeError("Permissions and hooks must be objects")
    deny = permissions.setdefault("deny", [])
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(deny, list) or not all(isinstance(item, str) for item in deny):
        raise TypeError("Permission deny must be a list of strings")
    if not isinstance(entries, list):
        raise TypeError("PreToolUse must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
            raise TypeError("Invalid PreToolUse entry")
        if not all(isinstance(hook, dict) for hook in entry.get("hooks", [])):
            raise TypeError("Invalid PreToolUse hook")
    permissions["deny"] = list(dict.fromkeys(deny + template["permissions"]["deny"]))
    for entry in template["hooks"]["PreToolUse"]:
        missing = [
            hook
            for hook in entry["hooks"]
            if not any(
                candidate.get("matcher") == entry.get("matcher")
                and any(
                    item.get("command") == hook["command"]
                    for item in candidate.get("hooks", [])
                )
                for candidate in entries
            )
        ]
        if missing:
            entries.append({**entry, "hooks": missing})
    return (json.dumps(target, indent=2) + "\n").encode("utf-8")


def _has_linked_destination(target_root: Path, relative_path: Path) -> bool:
    """Reject linked destinations and paths with non-directory parents."""
    current = target_root
    for part in relative_path.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            return True
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            return True
        if current != target_root / relative_path and not stat.S_ISDIR(metadata.st_mode):
            return True
        # Windows exposes reparse attributes on Python 3.11 too; pathlib's
        # is_junction() only exists from 3.12. Do not follow links to inspect them.
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return True
    return False


def init_reference_workflow(
    target_root: Path,
    *,
    template: str = REFERENCE_TEMPLATE,
    dry_run: bool = False,
    force: bool = False,
    provider: str | None = None,
    available_providers: Iterable[str] | None = None,
) -> InitResult:
    if template != REFERENCE_TEMPLATE:
        raise UnknownTemplateError(template)
    if not _TEMPLATE_ROOT.is_dir():
        raise InitWorkflowError(f"Template directory not found: {_TEMPLATE_ROOT}")

    detected = tuple(
        detect_provider_clis() if available_providers is None else available_providers
    )
    mapping = resolve_provider_mapping(provider, detected)

    target_root = target_root.expanduser().resolve()
    copied: list[Path] = []
    skipped: list[Path] = []
    conflicts: list[Path] = []
    merged: list[Path] = []
    planned: dict[Path, bytes] = {}

    for relative_path in template_files():
        target_path = target_root / relative_path
        source = _source_bytes(relative_path, mapping)
        managed = relative_path in _TEXT_MARKERS or relative_path == _SETTINGS_PATH
        if _has_linked_destination(target_root, relative_path) or target_path.is_dir():
            conflicts.append(relative_path)
            continue
        if managed:
            exists = target_path.exists()
            existing = target_path.read_bytes() if exists else b""
            try:
                if relative_path in _TEXT_MARKERS:
                    rendered = _merge_text(relative_path, existing, source)
                else:
                    rendered = _merge_settings(existing if exists else b"{}", source)
            except (ValueError, TypeError):
                conflicts.append(relative_path)
                continue
            if exists and rendered == existing:
                skipped.append(relative_path)
            else:
                (merged if exists else copied).append(relative_path)
                planned[relative_path] = rendered
            continue
        if not target_path.exists():
            copied.append(relative_path)
            continue
        if target_path.is_dir():
            conflicts.append(relative_path)
            continue
        if force:
            copied.append(relative_path)
            continue
        if target_path.read_bytes() == _source_bytes(relative_path, mapping):
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
        provider_mapping=mapping,
        detected_providers=detected,
        merged=tuple(merged),
    )

    if conflicts and not dry_run:
        raise InitConflictError(result.conflicts)
    if dry_run:
        return result

    target_root.mkdir(parents=True, exist_ok=True)
    for relative_path in copied + merged:
        source_path = _TEMPLATE_ROOT / relative_path
        target_path = target_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if relative_path in planned:
            target_path.write_bytes(planned[relative_path])
        elif relative_path == _CONFIG_PATH and mapping != REFERENCE_MAPPING:
            target_path.write_bytes(_source_bytes(relative_path, mapping))
        else:
            shutil.copy2(source_path, target_path)

    return result
