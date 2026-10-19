from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import agent_roles
from llm_handoff.agent_process import build_agent_env
from llm_handoff.agent_providers import antigravity, claude, codex
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.config import AgentConfig, load_dispatch_config
from llm_handoff.normalizer_providers import claude as claude_normalizer


UNRELATED_SECRETS = {
    "GITHUB_TOKEN": "ghp-secret",
    "GH_TOKEN": "gh-secret",
    "AWS_ACCESS_KEY_ID": "aws-id",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "AWS_SESSION_TOKEN": "aws-session",
    "DATABASE_PASSWORD": "db-secret",
    "NPM_TOKEN": "npm-secret",
}
VENDOR_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
    "OPENAI_API_KEY": "openai-secret",
    "GEMINI_API_KEY": "gemini-secret",
    "GOOGLE_API_KEY": "google-secret",
}
PROVIDER_VARS = {
    "CLAUDE_CONFIG_DIR": "claude-config",
    "CODEX_HOME": "codex-home",
    "GEMINI_SANDBOX": "docker",
}


@pytest.fixture
def secret_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {**UNRELATED_SECRETS, **VENDOR_KEYS, **PROVIDER_VARS}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")


class _EnvRecorder:
    def __init__(self) -> None:
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, command, *, cwd, timeout_ms, env=None, **_kwargs):
        self.envs.append(env)
        return _ProcessResult(stdout="", stderr="", exit_code=0)


def _assert_no_unrelated_secrets(env: dict[str, str] | None) -> dict[str, str]:
    assert env is not None, "adapter must pass an explicit env, not inherit"
    upper_names = {name.upper() for name in env}
    for name in UNRELATED_SECRETS:
        assert name not in upper_names
    assert not any(name.startswith("AWS_") for name in upper_names)
    assert "PATH" in upper_names
    return env


def _upper(env: dict[str, str]) -> set[str]:
    return {name.upper() for name in env}


def test_claude_adapter_subprocess_gets_allowlisted_env_only(
    monkeypatch: pytest.MonkeyPatch,
    secret_environ: None,
) -> None:
    recorder = _EnvRecorder()
    monkeypatch.setattr(claude, "_run_command_streaming", recorder)

    claude.invoke_claude_subagent("auditor", "audit", repo_root=Path.cwd())

    env = _assert_no_unrelated_secrets(recorder.envs[0])
    assert "CLAUDE_CONFIG_DIR" in _upper(env)
    assert _upper(env).isdisjoint(VENDOR_KEYS)
    assert "CODEX_HOME" not in _upper(env)
    assert "GEMINI_SANDBOX" not in _upper(env)


def test_codex_adapter_subprocess_gets_allowlisted_env_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_environ: None,
) -> None:
    recorder = _EnvRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)

    codex.invoke_codex(tmp_path / "HANDOFF.md", repo_root=tmp_path)

    env = _assert_no_unrelated_secrets(recorder.envs[0])
    assert "CODEX_HOME" in _upper(env)
    assert _upper(env).isdisjoint(VENDOR_KEYS)
    assert "CLAUDE_CONFIG_DIR" not in _upper(env)


def test_role_dispatch_passes_role_env_passthrough_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_environ: None,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    recorder = _EnvRecorder()
    monkeypatch.setattr(codex, "_run_command_streaming", recorder)

    agent_roles.invoke_role(
        "backend",
        tmp_path / "HANDOFF.md",
        agent_config=AgentConfig(provider="codex", env_passthrough=("https_proxy",)),
        repo_root=tmp_path,
    )

    env = _assert_no_unrelated_secrets(recorder.envs[0])
    assert "HTTPS_PROXY" in _upper(env)


def test_codex_api_key_is_passed_only_when_role_opts_in(
    secret_environ: None,
) -> None:
    default_env = build_agent_env(AgentConfig(provider="codex"))
    opted_in_env = build_agent_env(AgentConfig(provider="codex", use_api_key_env=True))

    assert "OPENAI_API_KEY" not in _upper(default_env)
    assert opted_in_env["OPENAI_API_KEY"] == "openai-secret"
    assert _upper(opted_in_env).isdisjoint(VENDOR_KEYS.keys() - {"OPENAI_API_KEY"})


@pytest.mark.parametrize("env_mode", ["allowlist", "inherit"])
@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
def test_google_api_keys_are_stripped_for_every_provider_even_when_opted_in(
    provider: str,
    env_mode: str,
    secret_environ: None,
) -> None:
    # The gemini provider was removed, so no launch receives a Google key.
    agent_config = AgentConfig(
        provider=provider,
        use_api_key_env=True,
        env_mode=env_mode,
    )
    configured_env = build_agent_env(agent_config)
    forced_env = build_agent_env(agent_config, use_api_key_env=True)

    for env in (configured_env, forced_env):
        assert "GEMINI_API_KEY" not in _upper(env)
        assert "GOOGLE_API_KEY" not in _upper(env)


def test_claude_never_gets_an_api_key_even_when_opted_in(
    secret_environ: None,
) -> None:
    env = build_agent_env(AgentConfig(provider="claude", use_api_key_env=True))

    assert _upper(env).isdisjoint(VENDOR_KEYS)


def test_antigravity_never_gets_an_api_key_even_when_opted_in(
    secret_environ: None,
) -> None:
    env = build_agent_env(AgentConfig(provider="antigravity", use_api_key_env=True))
    forced_env = build_agent_env(
        AgentConfig(provider="antigravity"),
        use_api_key_env=True,
    )

    assert _upper(env).isdisjoint(VENDOR_KEYS)
    assert _upper(forced_env).isdisjoint(VENDOR_KEYS)


def test_antigravity_allowlist_keeps_profile_dirs_and_drops_provider_vars() -> None:
    profile = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "USERPROFILE": "C:/Users/u",
        "APPDATA": "C:/Users/u/AppData/Roaming",
        "LOCALAPPDATA": "C:/Users/u/AppData/Local",
    }
    env = build_agent_env(
        AgentConfig(provider="antigravity"),
        environ={
            **profile,
            **VENDOR_KEYS,
            **UNRELATED_SECRETS,
            "GEMINI_SANDBOX": "docker",
            "GEMINI_CLI_HOME": "x",
            "CLAUDE_CONFIG_DIR": "x",
            "CODEX_HOME": "x",
            "GOOGLE_CLOUD_PROJECT": "p",
        },
    )

    assert env == profile


def test_antigravity_adapter_subprocess_gets_allowlisted_env_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_environ: None,
) -> None:
    recorder = _EnvRecorder()
    monkeypatch.setattr(antigravity, "_run_command_streaming", recorder)

    antigravity.invoke_antigravity(
        "planner", tmp_path / "HANDOFF.md", repo_root=tmp_path
    )

    env = _assert_no_unrelated_secrets(recorder.envs[0])
    assert _upper(env).isdisjoint(VENDOR_KEYS)
    assert _upper(env).isdisjoint(PROVIDER_VARS)


def test_inherit_mode_restores_environment_copy_but_keeps_vendor_key_rule(
    secret_environ: None,
) -> None:
    env = build_agent_env(AgentConfig(provider="codex", env_mode="inherit"))

    assert env["GITHUB_TOKEN"] == "ghp-secret"
    assert env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
    assert _upper(env).isdisjoint(VENDOR_KEYS)


def test_allowlist_matches_names_case_insensitively() -> None:
    env = build_agent_env(
        AgentConfig(provider="claude"),
        environ={"Path": "C:/bin", "SystemRoot": "C:/Windows", "GitHub_Token": "x"},
    )

    assert env == {"Path": "C:/bin", "SystemRoot": "C:/Windows"}


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
def test_allowlist_keeps_the_account_name(provider: str) -> None:
    env = build_agent_env(
        AgentConfig(provider=provider),
        environ={"USERNAME": "hough", "USER": "hough", "LOGNAME": "hough"},
    )

    assert env == {"USERNAME": "hough", "USER": "hough", "LOGNAME": "hough"}


def test_env_passthrough_rejects_vendor_api_keys() -> None:
    with pytest.raises(ValueError, match="use_api_key_env"):
        AgentConfig(provider="antigravity", env_passthrough=("OPENAI_API_KEY",))


def test_env_passthrough_rejects_invalid_names() -> None:
    with pytest.raises(ValueError, match="not a valid environment variable name"):
        AgentConfig(provider="codex", env_passthrough=("AWS_*",))


def test_dispatch_config_reads_env_mode_and_passthrough(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """agents:
  backend:
    provider: codex
    env_passthrough: [HTTPS_PROXY, SSL_CERT_FILE]
  auditor:
    provider: claude
    env_mode: inherit
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.agents["backend"].env_passthrough == ("HTTPS_PROXY", "SSL_CERT_FILE")
    assert loaded.agents["backend"].env_mode == "allowlist"
    assert loaded.agents["auditor"].env_mode == "inherit"
    assert loaded.agents["planner"].env_mode == "allowlist"


def test_claude_normalizer_cli_fallback_uses_allowlist(secret_environ: None) -> None:
    env = claude_normalizer._claude_cli_oauth_env()

    _assert_no_unrelated_secrets(env)
    assert _upper(env).isdisjoint(VENDOR_KEYS)
    assert "CLAUDE_CONFIG_DIR" in _upper(env)
