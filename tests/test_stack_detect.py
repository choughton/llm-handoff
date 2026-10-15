from __future__ import annotations

import json
from pathlib import Path

from llm_handoff.stack_detect import Candidate, detect_candidates


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _package_json(scripts: dict[str, str]) -> str:
    return json.dumps({"name": "demo", "scripts": scripts})


def test_node_repo_lists_package_scripts(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", _package_json({"test": "jest", "build": "tsc"}))

    assert detect_candidates(tmp_path) == (
        Candidate("npm run test", "package.json"),
        Candidate("npm run build", "package.json"),
    )


def test_go_repo_lists_build_and_test(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/demo\n")

    assert detect_candidates(tmp_path) == (
        Candidate("go build ./...", "go.mod"),
        Candidate("go test ./...", "go.mod"),
    )


def test_ci_and_package_json_each_keep_their_source(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "on: push\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: npm run test:ci\n",
    )
    _write(tmp_path, "package.json", _package_json({"test": "jest"}))

    assert detect_candidates(tmp_path) == (
        Candidate("npm run test:ci", ".github/workflows/ci.yml"),
        Candidate("npm run test", "package.json"),
    )


def test_multi_line_run_block_yields_one_candidate_per_command(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/build.yaml",
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - name: Build and test\n"
        "        run: |\n"
        "          # install first\n"
        "          pip install -e .\n"
        "\n"
        "          python -m pytest -q\n"
        "      - name: No run\n"
        "        uses: actions/setup-python@v5\n",
    )

    assert detect_candidates(tmp_path) == (
        Candidate("pip install -e .", ".github/workflows/build.yaml"),
        Candidate("python -m pytest -q", ".github/workflows/build.yaml"),
    )


def test_workflow_files_are_read_in_name_order(tmp_path: Path) -> None:
    for name, command in (("b.yml", "make b"), ("a.yaml", "make a")):
        _write(
            tmp_path,
            f".github/workflows/{name}",
            f"jobs:\n  j:\n    steps:\n      - run: {command}\n",
        )

    assert [c.command for c in detect_candidates(tmp_path)] == ["make a", "make b"]


def test_makefile_lists_only_verification_targets(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Makefile",
        "CC := gcc\n"
        "VERSION = 1:2\n"
        ".PHONY: test lint install\n"
        "\n"
        "test:\n"
        "\t$(CC) -o tests tests.c\n"
        "install:\n"
        "\tcp demo /usr/local/bin\n"
        "lint: test\n"
        "\techo lint\n",
    )

    assert detect_candidates(tmp_path) == (
        Candidate("make test", "Makefile"),
        Candidate("make lint", "Makefile"),
    )


def test_makefile_accepts_test_prefixed_targets(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Makefile",
        "test-unit:\n\techo unit\ntest_e2e:\n\techo e2e\ncheck build:\n\techo\n",
    )

    assert [c.command for c in detect_candidates(tmp_path)] == [
        "make test-unit",
        "make test_e2e",
        "make check",
        "make build",
    ]


def test_pyproject_pytest_and_cargo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "demo"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    )
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\n')

    assert detect_candidates(tmp_path) == (
        Candidate("python -m pytest", "pyproject.toml"),
        Candidate("cargo build", "Cargo.toml"),
        Candidate("cargo test", "Cargo.toml"),
    )


def test_pyproject_without_pytest_table_has_no_candidate(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[tool.ruff]\nline-length = 88\n')

    assert detect_candidates(tmp_path) == ()


def test_malformed_sources_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, ".github/workflows/bad.yml", "jobs: [unclosed\n  - : :\n")
    _write(tmp_path, ".github/workflows/shape.yml", "jobs:\n  - just a list\n")
    _write(tmp_path, "package.json", "{not json")
    _write(tmp_path, "pyproject.toml", "[tool.pytest\n")

    assert detect_candidates(tmp_path) == ()


def test_non_object_scripts_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"scripts": ["test"]}))

    assert detect_candidates(tmp_path) == ()


def test_exact_duplicates_are_dropped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n"
        "  a:\n    steps:\n      - run: make test\n"
        "  b:\n    steps:\n      - run: make test\n",
    )

    assert detect_candidates(tmp_path) == (
        Candidate("make test", ".github/workflows/ci.yml"),
    )


def test_empty_directory_has_no_candidates(tmp_path: Path) -> None:
    assert detect_candidates(tmp_path) == ()


def test_missing_directory_has_no_candidates(tmp_path: Path) -> None:
    assert detect_candidates(tmp_path / "missing") == ()
