from __future__ import annotations

from pathlib import Path

import pytest

from llm_handoff import config as config_module
from llm_handoff import __main__ as main_module
from llm_handoff.config import DispatchConfig, load_dispatch_config


def test_load_dispatch_config_reads_public_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """handoff_path: HANDOFF.md
project_state_path: STATE.md
auto_push: false
normalizer:
  provider: claude
  model: claude-haiku-test
  timeout_ms: 12345
  on_unknown: fail_closed
agents:
  backend:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
    timeout_ms: 1200000
  planner:
    provider: gemini
    binary: gemini
    mention: "@planner"
    resume: true
    timeout_ms: 1200000
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(
        repo_root=tmp_path,
        config_path=config_path,
        dry_run=True,
        backend_resume=False,
    )

    assert loaded.repo_root == tmp_path.resolve()
    assert loaded.handoff_path == Path("HANDOFF.md")
    assert loaded.project_state_path == Path("STATE.md")
    assert loaded.dry_run is True
    assert loaded.backend_resume is False
    assert loaded.normalizer.model == "claude-haiku-test"
    assert loaded.normalizer.timeout_ms == 12345
    assert loaded.agents["backend"].provider == "codex"
    assert loaded.agents["planner"].mention == "@planner"
    assert loaded.agents["frontend"].provider == "gemini"
    assert loaded.agents["validator"].provider == "claude"
    assert loaded.agents["finalizer"].provider == "claude"


def test_load_dispatch_config_uses_defaults_when_file_is_absent(
    tmp_path: Path,
) -> None:
    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.handoff_path == Path("docs/handoff/HANDOFF.md")
    assert loaded.project_state_path == Path("PROJECT_STATE.md")
    assert loaded.normalizer.provider == "claude"
    assert loaded.agents["backend"].skill_name == "llm-handoff"


def test_load_dispatch_config_accepts_non_default_role_provider_mapping(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """agents:
  backend:
    provider: claude
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.agents["backend"].provider == "claude"
    assert loaded.agents["backend"].binary == config_module.CLAUDE_BINARY
    assert loaded.agents["backend"].model == config_module.CLAUDE_MODEL
    assert loaded.agents["backend"].skill_name is None
    assert loaded.agents["planner"].provider == "gemini"


def test_load_dispatch_config_uses_provider_defaults_when_role_provider_changes(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """agents:
  auditor:
    provider: codex
  backend:
    provider: gemini
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.agents["auditor"].provider == "codex"
    assert loaded.agents["auditor"].binary == config_module.CODEX_BINARY
    assert loaded.agents["auditor"].skill_name == config_module.CODEX_SKILL_NAME
    assert loaded.agents["auditor"].model is None
    assert loaded.agents["backend"].provider == "gemini"
    assert loaded.agents["backend"].binary == config_module.GEMINI_BINARY
    assert loaded.agents["backend"].mention == "@backend"
    assert loaded.agents["backend"].skill_name is None


def test_load_dispatch_config_rejects_provider_without_runtime_adapter(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """agents:
  backend:
    provider: openai
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no runtime adapter is registered"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_antigravity_is_a_runtime_adapter_only() -> None:
    assert "antigravity" in config_module.RUNTIME_PROVIDER_ADAPTERS
    assert "antigravity" not in config_module.NORMALIZER_PROVIDER_ADAPTERS
    assert config_module.ANTIGRAVITY_BINARY == config_module._binary_name("agy")
    assert config_module.DEFAULT_AGENT_PROVIDERS["planner"] == "gemini"
    assert ".agents/**" in config_module.DEFAULT_PROTECTED_PATHS


@pytest.mark.parametrize(
    ("role", "resume"),
    [("planner", True), ("backend", False), ("frontend", False), ("validator", False)],
)
def test_antigravity_agent_defaults(role: str, resume: bool) -> None:
    assert config_module._agent_defaults_for_provider(role, "antigravity") == {
        "provider": "antigravity",
        "binary": config_module.ANTIGRAVITY_BINARY,
        "resume": resume,
        "timeout_ms": config_module.AGENT_TIMEOUT_MS,
    }


def test_load_dispatch_config_fills_antigravity_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """agents:
  planner:
    provider: antigravity
  frontend:
    provider: antigravity
    model: pro
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    planner = loaded.agents["planner"]
    assert planner.provider == "antigravity"
    assert planner.binary == config_module.ANTIGRAVITY_BINARY
    assert planner.resume is True
    assert planner.mention is None
    assert planner.retries is None
    assert loaded.agents["frontend"].resume is False
    assert loaded.agents["frontend"].model == "pro"


def test_load_dispatch_config_rejects_antigravity_normalizer(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("normalizer:\n  provider: antigravity\n", encoding="utf-8")

    with pytest.raises(ValueError, match="normalizer"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_load_dispatch_config_rejects_antigravity_security_provider(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "audit:\n  security_provider: antigravity\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="security_provider"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_load_dispatch_config_accepts_and_strips_audit_security_model(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "audit:\n  security_model: '  claude-sonnet-test  '\n", encoding="utf-8"
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.audit.security_model == "claude-sonnet-test"


def test_load_dispatch_config_defaults_audit_security_model_to_unset(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("audit:\n  block_severity: high\n", encoding="utf-8")

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.audit.security_model is None


@pytest.mark.parametrize("value", ["''", "'   '"])
def test_load_dispatch_config_rejects_empty_audit_security_model(
    tmp_path: Path,
    value: str,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        f"audit:\n  security_model: {value}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="audit.security_model"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_load_dispatch_config_rejects_security_model_without_security_reviewer(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "audit:\n"
        "  reviewers: [quality]\n"
        "  security_opt_out_reason: covered by an external review\n"
        "  security_model: claude-sonnet-test\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="audit.security_model only applies"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


@pytest.mark.parametrize("provider", ["gemini", "codex"])
def test_load_dispatch_config_rejects_security_model_for_non_claude_provider(
    tmp_path: Path,
    provider: str,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "audit:\n"
        f"  security_provider: {provider}\n"
        "  security_model: claude-sonnet-test\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="applies only to the claude security"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_load_dispatch_config_accepts_security_model_with_claude_provider(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "audit:\n"
        "  security_provider: claude\n"
        "  security_model: claude-sonnet-test\n",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.audit.security_provider == "claude"
    assert loaded.audit.security_model == "claude-sonnet-test"


def test_antigravity_fallback_without_binary_matches_default_agy_binary() -> None:
    with pytest.raises(ValueError, match="fallback must launch a different"):
        config_module.AgentConfig(
            provider="antigravity",
            binary=config_module.ANTIGRAVITY_BINARY,
            fallback={"provider": "antigravity"},
        )

    agent = config_module.AgentConfig(
        provider="antigravity",
        binary="agy-beta",
        fallback={"provider": "antigravity"},
    )
    assert agent.fallback is not None


def test_antigravity_fallback_with_different_model_is_a_distinct_launch() -> None:
    agent = config_module.AgentConfig(
        provider="antigravity",
        model="pro",
        fallback={"provider": "antigravity", "model": "flash"},
    )
    assert agent.fallback is not None
    assert agent.fallback.model == "flash"

    with pytest.raises(ValueError, match="fallback must launch a different"):
        config_module.AgentConfig(
            provider="antigravity",
            model="pro",
            fallback={"provider": "antigravity", "model": "pro"},
        )


def test_dispatch_config_rejects_missing_required_agent_roles(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="required reference roles"):
        DispatchConfig.model_validate(
            {
                "repo_root": tmp_path,
                "agents": {
                    "backend": {
                        "provider": "codex",
                    },
                },
            }
        )


def test_load_dispatch_config_rejects_unsupported_normalizer_provider(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """normalizer:
  provider: mistral
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="normalizer"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_normalizer_adapters_and_default_models_cover_the_same_providers() -> None:
    assert config_module.NORMALIZER_PROVIDER_ADAPTERS == frozenset(
        config_module.NORMALIZER_PROVIDER_MODELS
    )
    assert "codex" in config_module.NORMALIZER_PROVIDER_ADAPTERS


@pytest.mark.parametrize(
    ("provider", "expected_model"),
    [
        ("codex", config_module.CODEX_NORMALIZER_MODEL),
        ("gemini", config_module.GEMINI_NORMALIZER_MODEL),
        ("openai", config_module.OPENAI_NORMALIZER_MODEL),
    ],
)
def test_load_dispatch_config_accepts_non_claude_normalizer_provider(
    tmp_path: Path,
    provider: str,
    expected_model: str,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        f"""normalizer:
  provider: {provider}
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.normalizer.provider == provider
    assert loaded.normalizer.model == expected_model


def test_run_dispatch_loads_config_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "dispatch_config.yaml").write_text(
        """handoff_path: HANDOFF.md
project_state_path: STATE.md
normalizer:
  provider: claude
  model: claude-haiku-test
  timeout_ms: 12345
  on_unknown: fail_closed
""",
        encoding="utf-8",
    )
    seen: list[DispatchConfig] = []

    def fake_run_loop(config: DispatchConfig, *, log) -> int:
        seen.append(config)
        return 0

    monkeypatch.setattr(main_module, "run_loop", fake_run_loop)

    exit_code = main_module._run_dispatch(
        dry_run=True,
        use_manual_frontend=False,
        planner_api_key_env=False,
        backend_resume=True,
        planner_resume=True,
        repo_root=tmp_path,
        config_path=Path("dispatch_config.yaml"),
    )

    assert exit_code == 0
    assert seen
    assert seen[0].handoff_path == Path("HANDOFF.md")
    assert seen[0].project_state_path == Path("STATE.md")
    assert seen[0].normalizer.model == "claude-haiku-test"


def test_pause_recovery_defaults_to_planner(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).pause_recovery == "planner"
    assert load_dispatch_config(repo_root=tmp_path).pause_recovery == "planner"


def test_pause_recovery_accepts_off(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path, pause_recovery="off").pause_recovery == (
        "off"
    )


def test_pause_recovery_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pause_recovery"):
        DispatchConfig(repo_root=tmp_path, pause_recovery="auto")


def test_load_dispatch_config_reads_pause_recovery(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text('pause_recovery: "off"\n', encoding="utf-8")

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.pause_recovery == "off"


def test_load_dispatch_config_rejects_unquoted_yaml_off(tmp_path: Path) -> None:
    # YAML 1.1 reads a bare `off` as the boolean false.
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("pause_recovery: off\n", encoding="utf-8")

    with pytest.raises(ValueError, match='pause_recovery: "off"'):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_interactive_pause_defaults_to_true(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).interactive_pause is True
    assert load_dispatch_config(repo_root=tmp_path).interactive_pause is True


def test_load_dispatch_config_reads_interactive_pause(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("interactive_pause: false\n", encoding="utf-8")

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.interactive_pause is False


def test_load_dispatch_config_interactive_pause_argument_overrides_file(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("interactive_pause: true\n", encoding="utf-8")

    overridden = load_dispatch_config(
        repo_root=tmp_path,
        config_path=config_path,
        interactive_pause=False,
    )
    kept = load_dispatch_config(
        repo_root=tmp_path,
        config_path=config_path,
        interactive_pause=None,
    )

    assert overridden.interactive_pause is False
    assert kept.interactive_pause is True


class _StopDispatch(Exception):
    pass


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["--no-interactive-pause"], False), ([], None)],
)
def test_no_interactive_pause_flag_reaches_load_dispatch_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: bool | None,
) -> None:
    captured: dict[str, object] = {}

    def fake_load(**kwargs: object) -> DispatchConfig:
        captured.update(kwargs)
        raise _StopDispatch

    monkeypatch.setattr(main_module, "load_dispatch_config", fake_load)

    with pytest.raises(_StopDispatch):
        main_module.main([*argv, "--repo-root", str(tmp_path)])

    assert captured["interactive_pause"] is expected


def test_work_claim_env_passthrough_defaults_to_empty(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).work_claim_env_passthrough == ()
    assert load_dispatch_config(repo_root=tmp_path).work_claim_env_passthrough == ()


def test_load_dispatch_config_reads_work_claim_env_passthrough(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """work_claim_env_passthrough:
  - " TOOL_CACHE_DIR "
  - Pip_Index_Url
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.work_claim_env_passthrough == ("TOOL_CACHE_DIR", "Pip_Index_Url")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("", "non-empty"),
        ("   ", "non-empty"),
        ("BAD=NAME", "not a valid environment variable name"),
        ("BAD NAME", "not a valid environment variable name"),
        ("OPENAI_API_KEY", "vendor API key"),
        ("anthropic_api_key", "vendor API key"),
    ],
)
def test_work_claim_env_passthrough_rejects_invalid_entries(
    tmp_path: Path,
    entry: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DispatchConfig(repo_root=tmp_path, work_claim_env_passthrough=(entry,))


def test_verification_forbidden_chars_is_exactly_the_shell_control_set() -> None:
    assert config_module.VERIFICATION_FORBIDDEN_CHARS == frozenset(
        {"&", "|", ";", "<", ">", "$", "`", "(", ")", "%", "!", "\r", "\n"}
    )
    assert isinstance(config_module.VERIFICATION_FORBIDDEN_CHARS, frozenset)


def test_verification_allowed_prefixes_defaults_to_empty(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).verification_allowed_prefixes == ()
    assert (
        load_dispatch_config(repo_root=tmp_path).verification_allowed_prefixes == ()
    )


def test_load_dispatch_config_reads_verification_allowed_prefixes(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        """verification_allowed_prefixes:
  - "  python -m pytest  "
  - npm test
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.verification_allowed_prefixes == ("python -m pytest", "npm test")


@pytest.mark.parametrize("entry", ["", "   ", "\t"])
def test_verification_allowed_prefixes_rejects_empty_entries(
    tmp_path: Path,
    entry: str,
) -> None:
    with pytest.raises(ValueError, match="verification_allowed_prefixes") as error:
        DispatchConfig(repo_root=tmp_path, verification_allowed_prefixes=(entry,))
    assert "non-empty" in str(error.value)
    assert repr(entry) in str(error.value)


@pytest.mark.parametrize(
    "char", sorted(config_module.VERIFICATION_FORBIDDEN_CHARS)
)
def test_verification_allowed_prefixes_rejects_each_forbidden_char(
    tmp_path: Path,
    char: str,
) -> None:
    entry = f"python -m pytest {char} x"
    with pytest.raises(ValueError, match="verification_allowed_prefixes") as error:
        DispatchConfig(repo_root=tmp_path, verification_allowed_prefixes=(entry,))
    message = str(error.value)
    assert repr(entry) in message
    assert repr(char) in message
