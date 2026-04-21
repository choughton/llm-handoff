from __future__ import annotations

from pathlib import Path

from llm_handoff import config


LEGACY_PROJECT = "cross" + "fire"
LEGACY_TOOL = "anti" + "gravity"


def test_public_defaults_do_not_reference_source_project() -> None:
    public_defaults = "\n".join(
        [
            config.GEMINI_PLANNER_MENTION,
            config.GEMINI_FRONTEND_MENTION,
            config.CODEX_SKILL_NAME,
            config.DISPATCH_WINDOW_TITLE,
        ]
    )

    assert LEGACY_PROJECT not in public_defaults.lower()


def test_detect_repo_root_uses_git_root_without_project_handoff(tmp_path: Path) -> None:
    nested = tmp_path / "pkg" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / ".git").mkdir()

    assert config.detect_repo_root(nested) == tmp_path.resolve()


def test_package_source_does_not_reference_source_project_terms() -> None:
    package_root = Path(__file__).resolve().parents[1] / "llm_handoff"
    source_text = "\n".join(
        path.read_text(encoding="utf-8") for path in package_root.rglob("*.py")
    )

    banned_terms = [
        LEGACY_PROJECT,
        f"llm {LEGACY_PROJECT}",
        f"llm-{LEGACY_PROJECT}",
        LEGACY_TOOL,
        "completed_work_ledger",
        "claude.md",
        "v" + "20",
    ]

    lowered_source = source_text.lower()
    for term in banned_terms:
        assert term not in lowered_source
