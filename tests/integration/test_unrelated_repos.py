"""Run the installed `llm-handoff` command on repositories unrelated to it.

Each case builds a throwaway Node or Go repository, puts exactly one fake
provider CLI (`claude` or `codex`) on a controlled PATH, and runs the
installed package's `init`, `doctor`, and `--dry-run` from a directory
outside the source checkout. Nothing here contacts the network or launches a
real provider CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.integration

PROVIDER_NAMES = ("claude", "codex", "gemini")
ROLES = ("planner", "backend", "frontend", "auditor", "validator", "finalizer")
# Files `init` merges into rather than copies over; every other file that
# existed before `init` must come out byte-identical.
MERGED_PATHS = frozenset(
    {"AGENTS.md", ".claude/settings.json", ".gitignore", ".geminiignore"}
)
AGENTS_BEGIN = b"<!-- llm-handoff:begin -->"
AGENTS_END = b"<!-- llm-handoff:end -->"
CUSTOM_AGENTS_TEXT = (
    b"# Fixture Project Agents\n"
    b"\n"
    b"Custom instructions for this Node project. `init` must keep this text.\n"
    b"- Run `npm test` before every commit.\n"
)
CUSTOM_DENY_RULE = "Read(./secrets/**)"
# The dispatcher's runtime directory; a dry run writes its log file here.
RUNTIME_LOG_PREFIX = "logs/dispatch/"
# Keys a normalizer could use to reach a provider API. The child processes
# run offline, so none of them is passed through.
API_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)
# A doctor WARN line about the repository's commit count (the one-commit
# audit-base warning). Handoff
# checks are excluded: the template handoff's `sha_missing` warning also says
# "commit" but is about the handoff, not the repository history.
_COMMIT_WARNING = re.compile(
    r"^WARN (?!handoff)[^:]*:.*\bcommits?\b", re.IGNORECASE
)


# --- process helpers -------------------------------------------------------


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        **kwargs,
    )


def _describe(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"command exited {result.returncode}: {result.args}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git with a per-command identity; the global config is never written."""
    result = _run(
        [
            "git",
            "-c",
            "user.name=Integration Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=repo,
    )
    assert result.returncode == 0, _describe(result)
    return result


# --- fixture repositories --------------------------------------------------


def _write(repo: Path, relative: str, content: bytes) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    # Repo-local settings: a developer's global line-ending conversion must
    # not make files look modified after `init` writes them.
    _git(repo, "config", "--local", "core.autocrlf", "false")
    _git(repo, "config", "--local", "core.safecrlf", "false")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)


def _make_node_repo(repo: Path) -> None:
    """Node repository with existing AGENTS.md and .claude/settings.json."""
    _init_repo(repo)
    package = {
        "name": "fixture-node-app",
        "version": "1.0.0",
        "private": True,
        "scripts": {"test": "node --test"},
    }
    _write(repo, "package.json", (json.dumps(package, indent=2) + "\n").encode())
    _write(repo, "index.js", b"module.exports = (a, b) => a + b;\n")
    _commit_all(repo, "Add the Node package")

    _write(repo, "AGENTS.md", CUSTOM_AGENTS_TEXT)
    settings = {"permissions": {"deny": [CUSTOM_DENY_RULE]}}
    _write(
        repo,
        ".claude/settings.json",
        (json.dumps(settings, indent=2) + "\n").encode(),
    )
    _write(repo, "test/add.test.js", b"// placeholder test file\n")
    _commit_all(repo, "Add agent instructions and Claude settings")


def _make_go_repo(repo: Path) -> None:
    """Go repository with exactly one commit."""
    _init_repo(repo)
    _write(repo, "go.mod", b"module example.com/fixture\n\ngo 1.22\n")
    _write(
        repo,
        "main.go",
        b'package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("hi")\n}\n',
    )
    _commit_all(repo, "Initial Go module")


FIXTURES = {"node": _make_node_repo, "go": _make_go_repo}


# --- fake providers and PATH -----------------------------------------------


def _write_fake_provider(bin_dir: Path, name: str, log_path: Path) -> None:
    """Fake provider CLI: print a version, append argv to a log, exit 0."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(
            "@echo off\r\n"
            f"echo {name} 0.0.0-fake\r\n"
            f'echo({name} %*>>"{log_path}"\r\n'
            "exit /b 0\r\n",
            encoding="ascii",
        )
        return
    quoted_log = "'" + str(log_path).replace("'", "'\\''") + "'"
    script = bin_dir / name
    script.write_text(
        "#!/bin/sh\n"
        f'echo "{name} 0.0.0-fake"\n'
        f'echo "{name} $*" >> {quoted_log}\n'
        "exit 0\n",
        encoding="ascii",
    )
    script.chmod(0o755)


def _child_path(provider_bin_dir: Path) -> str:
    """PATH holding only the run's fake provider, git, and Python."""
    git = shutil.which("git")
    assert git is not None, "git must be on PATH for the integration tests"
    entries: list[str] = []
    for directory in (
        provider_bin_dir,
        Path(git).resolve().parent,
        Path(sys.executable).resolve().parent,
    ):
        if str(directory) not in entries:
            entries.append(str(directory))
    return os.pathsep.join(entries)


def _child_env(site_dir: Path, path: str) -> dict[str, str]:
    dropped = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PATH", *API_KEY_NAMES}
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in dropped
    }
    env["PATH"] = path
    env["PYTHONPATH"] = str(site_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


# --- tree inspection -------------------------------------------------------


def _snapshot(repo: Path) -> dict[str, bytes]:
    """Map every file under ``repo`` (excluding .git/) to its bytes."""
    files: dict[str, bytes] = {}
    for current, dirs, names in os.walk(repo):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in names:
            path = Path(current) / name
            files[path.relative_to(repo).as_posix()] = path.read_bytes()
    return files


def _status_paths(repo: Path) -> set[str]:
    result = _git(
        repo,
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=all",
    )
    return {entry[3:] for entry in result.stdout.split("\0") if entry}


def _commit_warnings(output: str) -> list[str]:
    return [line for line in output.splitlines() if _COMMIT_WARNING.match(line)]


# --- the test ----------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", sorted(FIXTURES))
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_installed_command_runs_on_unrelated_repository(
    tmp_path: Path,
    installed_site_dir: Path,
    fixture_name: str,
    provider: str,
) -> None:
    repo = tmp_path / "repos" / f"{fixture_name}-project"
    FIXTURES[fixture_name](repo)

    fake_root = tmp_path / "fake-bin"
    call_log = tmp_path / "provider-calls.log"
    for name in PROVIDER_NAMES:
        _write_fake_provider(fake_root / name, name, call_log)
    path = _child_path(fake_root / provider)

    # Real provider installs on the developer's machine must stay out of the
    # child run: only this run's fake resolves.
    for name in PROVIDER_NAMES:
        resolved = shutil.which(name, path=path)
        if name == provider:
            assert resolved is not None, path
            assert Path(resolved).resolve().is_relative_to(tmp_path.resolve())
        else:
            assert resolved is None, f"{name} resolves to {resolved} on {path}"

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    env = _child_env(installed_site_dir, path)

    def llm_handoff(*args: str) -> subprocess.CompletedProcess[str]:
        # -P keeps the working directory off sys.path, so only the installed
        # copy under installed_site_dir can satisfy the import.
        return _run(
            [sys.executable, "-P", "-m", "llm_handoff", *args],
            cwd=work_dir,
            env=env,
        )

    # llm_handoff is a namespace package, so check every entry of its
    # __path__ plus the entry-point module's file.
    probe = _run(
        [
            sys.executable,
            "-P",
            "-c",
            (
                "import llm_handoff, llm_handoff.__main__ as m; "
                "print(m.__file__); [print(p) for p in llm_handoff.__path__]"
            ),
        ],
        cwd=work_dir,
        env=env,
    )
    assert probe.returncode == 0, _describe(probe)
    probed = [Path(line) for line in probe.stdout.splitlines() if line.strip()]
    assert len(probed) >= 2, _describe(probe)
    for location in probed:
        assert location.resolve().is_relative_to(installed_site_dir.resolve()), (
            _describe(probe)
        )

    before_init = _snapshot(repo)

    # --- init ---------------------------------------------------------------
    init = llm_handoff("init", str(repo))
    assert init.returncode == 0, _describe(init)
    after_init = _snapshot(repo)
    init_paths = {
        relative
        for relative, content in after_init.items()
        if before_init.get(relative) != content
    }
    for relative, content in before_init.items():
        if relative in MERGED_PATHS:
            continue
        assert after_init.get(relative) == content, f"init changed {relative}"
    init_status = _status_paths(repo)
    stray = init_status - init_paths
    assert not stray, f"init changed paths it did not write: {sorted(stray)}"

    config = yaml.safe_load(after_init["dispatch_config.yaml"])
    for role in ROLES:
        assert config["agents"][role]["provider"] == provider, role
    assert config["audit"]["security_provider"] == provider

    if fixture_name == "node":
        agents = after_init["AGENTS.md"]
        assert agents.count(AGENTS_BEGIN) == 1
        assert agents.count(AGENTS_END) == 1
        start = agents.index(AGENTS_BEGIN)
        stop = agents.index(AGENTS_END) + len(AGENTS_END)
        assert agents[:start] == CUSTOM_AGENTS_TEXT
        assert agents[stop:] in (b"", b"\n", b"\r\n")
        settings = json.loads(after_init[".claude/settings.json"])
        assert CUSTOM_DENY_RULE in settings["permissions"]["deny"]

    second_init = llm_handoff("init", str(repo))
    assert second_init.returncode == 0, _describe(second_init)
    assert _snapshot(repo) == after_init

    # --- doctor -------------------------------------------------------------
    doctor = llm_handoff("doctor", "--repo-root", str(repo))
    assert doctor.returncode == 0, _describe(doctor)

    # --- dry run ------------------------------------------------------------
    dry_run = llm_handoff("--dry-run", "--repo-root", str(repo))
    assert dry_run.returncode == 0, _describe(dry_run)
    assert "[DRY RUN] Would dispatch planner" in dry_run.stdout + dry_run.stderr

    # --- no stray changes ---------------------------------------------------
    # doctor and --dry-run must leave every file that existed after init,
    # including the files init generated, byte-identical. The only new files
    # allowed are the dispatcher's own run logs under its runtime directory,
    # and the git status check below shows init's .gitignore covers them.
    final = _snapshot(repo)
    changed = sorted(
        relative
        for relative in final.keys() | after_init.keys()
        if final.get(relative) != after_init.get(relative)
        and not (
            relative not in after_init
            and relative.startswith(RUNTIME_LOG_PREFIX)
        )
    )
    assert not changed, f"doctor or --dry-run changed files: {changed}"
    assert _status_paths(repo) == init_status

    # --- doctor one-commit audit-base warning --------------------------------
    commit_warnings = _commit_warnings(doctor.stdout)
    if fixture_name == "go":
        assert commit_warnings, (
            "expected a WARN line about the commit count for a one-commit "
            f"repository\n{doctor.stdout}"
        )
    else:
        assert not commit_warnings, doctor.stdout
