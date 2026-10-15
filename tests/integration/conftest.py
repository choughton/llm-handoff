"""Session fixture that installs the built wheel for the integration tests.

This mirrors the install pattern in ``tests/test_packaging.py``: build the
wheel offline and install it with ``pip install --target``. The helpers are
copied here on purpose so the integration tests do not import from or depend
on the packaging test module.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {command}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture(scope="session")
def installed_site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel offline, install it into a temp dir, return that dir."""
    # hatchling is a dev requirement, so a missing backend is a broken dev
    # environment, not a reason to skip the end-to-end install check.
    if importlib.util.find_spec("hatchling") is None:
        pytest.fail(
            "hatchling is not importable. It is listed in requirements-dev.txt; "
            "run `python -m pip install -r requirements-dev.txt`. The wheel "
            "build runs offline with --no-build-isolation and needs the build "
            "backend installed."
        )

    root = tmp_path_factory.mktemp("installed")
    wheel_dir = root / "wheelhouse"
    site_dir = root / "site"

    # Each pytest-xdist worker builds its own wheel from the same source, so
    # concurrent builds would race to rename the same file in pip's shared
    # wheel cache (WinError 5 on Windows). --no-cache-dir keeps every build in
    # its own temp directory.
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-index",
            "--no-build-isolation",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--wheel-dir",
            str(wheel_dir),
            str(REPO_ROOT),
        ]
    )
    wheels = sorted(wheel_dir.glob("llm_handoff-*.whl"))
    assert len(wheels) == 1, wheels

    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--target",
            str(site_dir),
            str(wheels[0]),
        ]
    )
    return site_dir
