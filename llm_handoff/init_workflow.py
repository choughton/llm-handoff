from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import shutil


REFERENCE_TEMPLATE = "reference-workflow"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_ROOT = _REPO_ROOT / "examples" / REFERENCE_TEMPLATE
_EXCLUDED_TEMPLATE_FILES = frozenset({Path("README.md")})

# Provider mappings `init` can write into dispatch_config.yaml. `reference`
# keeps the template's mixed mapping; `claude` puts every role on Claude Code.
REFERENCE_MAPPING = "reference"
CLAUDE_MAPPING = "claude"
PROVIDER_MAPPINGS = (CLAUDE_MAPPING,)
PROVIDER_CLIS = ("claude", "codex", "gemini")
_CONFIG_PATH = Path("dispatch_config.yaml")

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
    model: claude-opus-4-7
    resume: false
    timeout_ms: 900000

  validator:
    provider: claude
    binary: claude
    model: claude-haiku-4-5
    resume: false
    timeout_ms: 900000

  finalizer:
    provider: claude
    binary: claude
    model: claude-opus-4-7
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

    for relative_path in template_files():
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
        if relative_path == _CONFIG_PATH and mapping != REFERENCE_MAPPING:
            target_path.write_bytes(_source_bytes(relative_path, mapping))
        else:
            shutil.copy2(source_path, target_path)

    return result
