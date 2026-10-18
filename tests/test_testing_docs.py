from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTING_DOC = REPO_ROOT / "docs" / "TESTING.md"
LINEAR_HEADING = "## Linear Mode Acceptance Run"


def _linear_section(content: str) -> str:
    lines = content.splitlines()
    start = lines.index(LINEAR_HEADING)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def test_testing_doc_has_linear_mode_acceptance_run_heading() -> None:
    content = TESTING_DOC.read_text(encoding="utf-8")

    assert LINEAR_HEADING in content.splitlines()


def test_linear_mode_acceptance_run_names_required_steps() -> None:
    section = _linear_section(TESTING_DOC.read_text(encoding="utf-8"))

    for term in (
        "--probe-tracker",
        "--tracker linear",
        "lease_opened",
        "implementation_handoff",
        "audit_passed",
        "audit_failed",
        "success_state",
        "in_progress_state",
        "tracker_lease_rebuild",
        "LINEAR-EVIDENCE",
        "pending-dispatcher-commit",
        "sandbox",
    ):
        assert term in section, term


def test_testing_doc_is_ascii() -> None:
    TESTING_DOC.read_bytes().decode("ascii")
