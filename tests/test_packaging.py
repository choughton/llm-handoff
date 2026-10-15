from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from llm_handoff import init_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
TEMPLATE_PREFIX = "llm_handoff/templates/reference-workflow/"
HIDDEN_TEMPLATE_FILES = (
    ".claude/settings.json",
    ".geminiignore",
    ".gitignore",
)
HIDDEN_TEMPLATE_DIRS = (".codex/", ".gemini/")
TEST_AND_LINT_TOOLS = {"pytest", "pytest-xdist", "pre-commit", "ruff"}
BUILD_TOOLS = {"hatchling"}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _requirement_name(requirement: str) -> str:
    for separator in ("<", ">", "=", "!", "~", ";", "[", " "):
        requirement = requirement.split(separator, 1)[0]
    return requirement.strip().lower()


def test_pyproject_declares_console_script_for_main_entry_point() -> None:
    project = _pyproject()["project"]

    assert project["name"] == "llm-handoff"
    assert project["scripts"] == {"llm-handoff": "llm_handoff.__main__:main"}
    assert project["requires-python"] == ">=3.11"
    assert project["license"] == "Apache-2.0"


def test_pyproject_runtime_dependencies_drop_test_and_lint_tools() -> None:
    dev_requirements = {
        _requirement_name(line)
        for line in (REPO_ROOT / "requirements-dev.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    runtime = {
        _requirement_name(item) for item in _pyproject()["project"]["dependencies"]
    }

    assert BUILD_TOOLS <= dev_requirements
    assert not runtime & BUILD_TOOLS
    assert runtime == dev_requirements - TEST_AND_LINT_TOOLS - BUILD_TOOLS


def test_pyproject_maps_reference_workflow_into_the_wheel() -> None:
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel["packages"] == ["llm_handoff"]
    assert wheel["force-include"] == {
        "examples/reference-workflow": TEMPLATE_PREFIX.rstrip("/"),
    }


def test_source_checkout_falls_back_to_examples_template() -> None:
    expected = REPO_ROOT / "examples" / init_workflow.REFERENCE_TEMPLATE

    assert init_workflow.resolve_template_root() == expected
    assert init_workflow._TEMPLATE_ROOT == expected


def test_packaged_template_is_preferred_over_source_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "llm_handoff"
    packaged = package_dir / "templates" / init_workflow.REFERENCE_TEMPLATE
    packaged.mkdir(parents=True)
    monkeypatch.setattr(init_workflow, "_PACKAGE_DIR", package_dir.resolve())
    monkeypatch.setattr(init_workflow.resources, "files", lambda name: package_dir)

    assert init_workflow.resolve_template_root() == packaged.resolve()


def test_packaged_template_from_another_install_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_install = tmp_path / "other" / "llm_handoff"
    (other_install / "templates" / init_workflow.REFERENCE_TEMPLATE).mkdir(parents=True)
    monkeypatch.setattr(init_workflow.resources, "files", lambda name: other_install)

    assert init_workflow.resolve_template_root() == (
        REPO_ROOT / "examples" / init_workflow.REFERENCE_TEMPLATE
    )


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        **kwargs,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {command}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _installed_env(site_dir: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
    }
    env["PYTHONPATH"] = str(site_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def test_installed_wheel_bundles_template_and_init_writes_hidden_files(
    tmp_path: Path,
) -> None:
    # hatchling is a dev requirement, so a missing backend is a broken dev
    # environment, not a reason to skip the only end-to-end install check.
    if importlib.util.find_spec("hatchling") is None:
        pytest.fail(
            "hatchling is not importable. It is listed in requirements-dev.txt; "
            "run `python -m pip install -r requirements-dev.txt`. The wheel "
            "build runs offline with --no-build-isolation and needs the build "
            "backend installed."
        )

    wheel_dir = tmp_path / "wheelhouse"
    site_dir = tmp_path / "site"
    work_dir = tmp_path / "work"
    target = tmp_path / "target"
    work_dir.mkdir()

    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-index",
            "--no-build-isolation",
            "--disable-pip-version-check",
            "--wheel-dir",
            str(wheel_dir),
            str(REPO_ROOT),
        ]
    )
    wheels = sorted(wheel_dir.glob("llm_handoff-*.whl"))
    assert len(wheels) == 1, wheels
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points = next(
            archive.read(name).decode("utf-8")
            for name in names
            if name.endswith(".dist-info/entry_points.txt")
        )
    for relative in HIDDEN_TEMPLATE_FILES:
        assert TEMPLATE_PREFIX + relative in names
    for prefix in HIDDEN_TEMPLATE_DIRS:
        assert any(name.startswith(TEMPLATE_PREFIX + prefix) for name in names)
    assert "llm_handoff/schemas/codex_final_response.schema.json" in names
    assert "llm-handoff = llm_handoff.__main__:main" in entry_points

    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--disable-pip-version-check",
            "--target",
            str(site_dir),
            str(wheel),
        ]
    )
    env = _installed_env(site_dir)

    # -P keeps the working directory off sys.path, so only the installed copy
    # under site_dir can satisfy the imports.
    probe = _run(
        [
            sys.executable,
            "-P",
            "-c",
            (
                "import llm_handoff.init_workflow as w, llm_handoff.config as c; "
                "print(w.__file__); print(w._TEMPLATE_ROOT); "
                "print(c.CODEX_OUTPUT_SCHEMA_PATH)"
            ),
        ],
        cwd=work_dir,
        env=env,
    )
    module_file, template_root, schema_path = (
        Path(line) for line in probe.stdout.strip().splitlines()
    )
    site_root = site_dir.resolve()
    assert module_file.resolve().is_relative_to(site_root)
    assert template_root.resolve().is_relative_to(site_root)
    assert schema_path.resolve().is_relative_to(site_root)
    assert schema_path.is_file()

    _run(
        [sys.executable, "-P", "-m", "llm_handoff", "init", str(target)],
        cwd=work_dir,
        env=env,
    )

    for relative in HIDDEN_TEMPLATE_FILES:
        assert (target / relative).is_file(), relative
    for prefix in HIDDEN_TEMPLATE_DIRS:
        directory = target / prefix.rstrip("/")
        assert any(path.is_file() for path in directory.rglob("*")), prefix
    assert (target / "dispatch_config.yaml").is_file()
    assert not (target / "README.md").exists()

    # The `llm-handoff` console script pip generated for the wheel.
    scripts = sorted(
        path
        for scripts_dir in (site_dir / "bin", site_dir / "Scripts")
        for path in scripts_dir.glob("llm-handoff*")
        if path.suffix.lower() in {"", ".exe"}
    )
    assert scripts, sorted(str(path) for path in site_dir.iterdir())
    script_target = tmp_path / "script-target"
    _run([str(scripts[0]), "init", str(script_target)], cwd=work_dir, env=env)
    for relative in HIDDEN_TEMPLATE_FILES:
        assert (script_target / relative).is_file(), relative
