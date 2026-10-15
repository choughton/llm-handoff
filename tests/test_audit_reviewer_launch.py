from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from llm_handoff import audit_review, config as config_module
from llm_handoff.agent_providers import codex as codex_provider
from llm_handoff.agent_providers import gemini as gemini_provider
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.audit_review import ReviewScope
from llm_handoff.config import AgentConfig, DispatchConfig


SECRETS = {
    "GITHUB_TOKEN": "ghp-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "DATABASE_PASSWORD": "db-secret",
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "OPENAI_API_KEY": "openai-secret",
    "GEMINI_API_KEY": "gemini-secret",
    "GOOGLE_API_KEY": "google-secret",
}


@pytest.fixture(autouse=True)
def _secret_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")


def _config(
    repo: Path,
    provider: str,
    **role_overrides: object,
) -> DispatchConfig:
    agents = config_module._default_agent_configs()
    role = {"claude": "auditor", "gemini": "planner", "codex": "backend"}[provider]
    agents[role] = AgentConfig(
        **{
            **agents[role].model_dump(exclude_none=True),
            "env_passthrough": ("HTTPS_PROXY",),
            **role_overrides,
        }
    )
    return DispatchConfig(
        repo_root=repo,
        agents=agents,
        audit={"reviewers": ["quality", "security"], "security_provider": provider},
    )


def _scope(tmp_path: Path, base: str = "a" * 40, head: str = "b" * 40) -> ReviewScope:
    return ReviewScope(
        base_sha=base,
        head_sha=head,
        files=("src/app.py",),
        diff_path=tmp_path / "scope.diff",
    )


def _plan(config: DispatchConfig, provider: str, tmp_path: Path, **scope_kwargs):
    return audit_review.build_security_reviewer_plan(
        config,
        provider=provider,
        scope=_scope(tmp_path, **scope_kwargs),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )


def _assert_allowlisted(env: dict[str, str] | None) -> None:
    assert env is not None, "reviewer must not inherit the dispatcher environment"
    upper = {name.upper() for name in env}
    assert upper.isdisjoint(SECRETS.keys() - {"OPENAI_API_KEY", "GEMINI_API_KEY"})
    assert "ANTHROPIC_API_KEY" not in upper
    assert "GOOGLE_API_KEY" not in upper
    assert "HTTPS_PROXY" in upper
    assert "PATH" in upper


@pytest.mark.parametrize("provider", ["claude", "gemini", "codex"])
def test_security_reviewer_env_uses_the_role_allowlist(
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)

    plan = _plan(_config(tmp_path, provider), provider, tmp_path)

    _assert_allowlisted(plan.env)
    assert "OPENAI_API_KEY" not in plan.env
    assert "GEMINI_API_KEY" not in plan.env


def test_codex_security_cli_env_uses_the_role_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        [
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        audit_review.shutil, "which", lambda _name: "/opt/bin/codex-security"
    )

    plan = _plan(_config(repo, "codex"), "codex", tmp_path, head=head)

    assert plan.kind == "codex-security"
    _assert_allowlisted(plan.env)


def test_claude_reviewer_stays_read_only_and_denies_web_tools(tmp_path: Path) -> None:
    plan = _plan(_config(tmp_path, "claude", unsafe_bypass=True), "claude", tmp_path)

    command = list(plan.command)
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    disallowed = set(command[command.index("--disallowedTools") + 1 :])
    assert {"Edit", "Write", "WebFetch", "WebSearch"} <= disallowed


def test_codex_reviewer_stays_read_only_even_with_unsafe_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)

    plan = _plan(_config(tmp_path, "codex", unsafe_bypass=True), "codex", tmp_path)

    command = list(plan.command)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--yolo" not in command


def test_gemini_reviewer_uses_container_sandbox(tmp_path: Path) -> None:
    plan = _plan(_config(tmp_path, "gemini"), "gemini", tmp_path)

    command = list(plan.command)
    assert command[command.index("--approval-mode") + 1] == "plan"
    assert "--sandbox" in command
    assert "--sandbox=false" not in command
    assert plan.preflight_error is None


def test_gemini_reviewer_unsafe_bypass_only_lifts_the_sandbox(tmp_path: Path) -> None:
    plan = _plan(_config(tmp_path, "gemini", unsafe_bypass=True), "gemini", tmp_path)

    command = list(plan.command)
    assert "--sandbox=false" in command
    assert command[command.index("--approval-mode") + 1] == "plan"
    assert "--yolo" not in command


def test_gemini_reviewer_fails_closed_without_sandbox_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_provider, "detect_gemini_sandbox_runtime", lambda: None)
    launched: list[list[str]] = []

    def fake_runner(agent_name, command, **_kwargs):
        launched.append(list(command))
        return _ProcessResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_runner)
    plan = _plan(_config(tmp_path, "gemini"), "gemini", tmp_path)

    outcome = audit_review._run_security_reviewer(
        plan,
        timeout_ms=1000,
        repo_root=tmp_path,
        handoff_path=tmp_path / "HANDOFF.md",
        log=None,
    )

    assert launched == []
    assert outcome.run.error is not None
    assert outcome.run.error.startswith("security_reviewer_failed:")
    assert "agents.planner.unsafe_bypass: true" in outcome.run.error
    assert "audit.security_provider" in outcome.run.error


def test_codex_exec_reviewer_run_passes_its_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)
    recorded: dict[str, object] = {}

    def fake_codex(agent_name, command, **kwargs):
        recorded.update(kwargs)
        return codex_provider._CodexCommandResult(
            result=_ProcessResult(stdout="", stderr="", exit_code=1),
            thread_id=None,
            final_message="",
        )

    monkeypatch.setattr(audit_review, "_run_codex_json_command", fake_codex)
    plan = _plan(_config(tmp_path, "codex"), "codex", tmp_path)

    audit_review._run_security_reviewer(
        plan,
        timeout_ms=1000,
        repo_root=tmp_path,
        handoff_path=tmp_path / "HANDOFF.md",
        log=None,
    )

    assert recorded["env"] is plan.env
    _assert_allowlisted(recorded["env"])
