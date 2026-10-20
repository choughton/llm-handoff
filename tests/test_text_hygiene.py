from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DOCTOR_TESTS = REPO_ROOT / "tests" / "test_doctor.py"
MAX_CHANGELOG_LINE = 100


def test_changelog_lines_are_at_most_100_characters() -> None:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()

    too_long = [
        number
        for number, line in enumerate(lines, start=1)
        if len(line) > MAX_CHANGELOG_LINE
    ]

    assert not too_long, (
        f"CHANGELOG.md lines longer than {MAX_CHANGELOG_LINE} characters: "
        f"{', '.join(str(number) for number in too_long)}"
    )


def test_doctor_tests_are_ascii() -> None:
    DOCTOR_TESTS.read_bytes().decode("ascii")
