from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_supervised_dispatch_docs_describe_operator_controls() -> None:
    content = (REPO_ROOT / "docs" / "SUPERVISED_DISPATCH.md").read_text(
        encoding="utf-8"
    )
    normalized = content.lower()

    assert "dispatch-supervised.ps1" in content
    assert "--doctor" in content
    assert "300 seconds" in normalized
    assert "boundary" in normalized
    assert "current-run.json" in content
    assert "incident" in normalized
    assert "dispatch-supervised.lock" in content
    assert "notify-command" in content
    assert "shell" in normalized
    assert "stalled-output" in normalized
    assert "repair-role" in normalized
    assert "off by default" in normalized


def test_readme_and_configuration_link_supervised_dispatch_docs() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    configuration = (REPO_ROOT / "CONFIGURATION.md").read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "docs/SUPERVISED_DISPATCH.md" in readme
    assert "docs/SUPERVISED_DISPATCH.md" in configuration
    assert "llm_handoff.supervisor" in architecture
