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
    assert not hasattr(loaded, "auto_push")


def test_load_dispatch_config_rejects_auto_push_true(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("auto_push: true\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    message = str(excinfo.value)
    assert "auto_push" in message
    assert "publish.mode" in message


def test_dispatch_config_accepts_auto_push_false_without_attribute(
    tmp_path: Path,
) -> None:
    loaded = DispatchConfig.model_validate({"repo_root": tmp_path, "auto_push": False})

    assert not hasattr(loaded, "auto_push")


def test_dispatch_config_rejects_auto_push_true(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="auto_push"):
        DispatchConfig.model_validate({"repo_root": tmp_path, "auto_push": True})


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


def test_antigravity_is_a_runtime_and_normalizer_adapter() -> None:
    assert "antigravity" in config_module.RUNTIME_PROVIDER_ADAPTERS
    assert "antigravity" in config_module.NORMALIZER_PROVIDER_ADAPTERS
    assert (
        config_module.NORMALIZER_PROVIDER_MODELS["antigravity"]
        == config_module.ANTIGRAVITY_NORMALIZER_MODEL
        == "gemini-3.8-flash-low"
    )
    # The default normalizer stays claude.
    assert config_module.NORMALIZER_PROVIDER == "claude"
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


def test_load_dispatch_config_accepts_antigravity_normalizer(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("normalizer:\n  provider: antigravity\n", encoding="utf-8")

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.normalizer.provider == "antigravity"
    assert loaded.normalizer.model == "gemini-3.8-flash-low"


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
        ("antigravity", config_module.ANTIGRAVITY_NORMALIZER_MODEL),
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


@pytest.mark.parametrize(
    "entry",
    [
        "python  -m pytest",
        "python -m\tpytest",
        "  python -m   pytest  ",
        "python\x0b-m pytest",
        "python -m pytest",
    ],
)
def test_verification_allowed_prefixes_rejects_irregular_whitespace(
    tmp_path: Path,
    entry: str,
) -> None:
    with pytest.raises(ValueError, match="verification_allowed_prefixes") as error:
        DispatchConfig(repo_root=tmp_path, verification_allowed_prefixes=(entry,))
    message = str(error.value)
    assert repr(entry) in message
    assert "single spaces between words" in message


def test_load_dispatch_config_rejects_a_prefix_with_a_doubled_space(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        'verification_allowed_prefixes:\n  - "python  -m pytest"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="single spaces between words"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


# --- tracker block -----------------------------------------------------------

_DROP = object()


def _linear_tracker(**overrides: object) -> dict[str, object]:
    tracker: dict[str, object] = {
        "provider": "linear",
        "team": "ENG",
        "project": "Platform",
        "eligible_state": "Todo",
        "in_progress_state": "In Progress",
        "success_state": "In Review",
        "mcp_server": "linear",
    }
    for key, value in overrides.items():
        if value is _DROP:
            tracker.pop(key, None)
        else:
            tracker[key] = value
    return tracker


def _write_config(tmp_path: Path, text: str) -> Path:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def test_tracker_config_model_is_frozen_and_forbids_extra() -> None:
    assert config_module.TrackerConfig.model_config["frozen"] is True
    assert config_module.TrackerConfig.model_config["extra"] == "forbid"
    tracker = config_module.TrackerConfig()
    assert tracker.provider == "none"
    for key in config_module._TRACKER_SETTING_KEYS:
        assert getattr(tracker, key) is None
    with pytest.raises(ValueError):
        tracker.provider = "linear"  # type: ignore[misc]


def test_tracker_defaults_to_none_when_absent(tmp_path: Path) -> None:
    assert DispatchConfig(repo_root=tmp_path).tracker.provider == "none"
    config_path = _write_config(tmp_path, "pause_recovery: planner\n")
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    assert loaded.tracker.provider == "none"
    assert loaded.tracker.enabled is False
    assert loaded.tracker_claude_mcp_config_path is None


def test_tracker_provider_none_loads(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "tracker:\n  provider: none\n")
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    assert loaded.tracker.provider == "none"
    assert loaded.tracker == config_module.TrackerConfig()


def test_tracker_provider_none_allows_explicit_nulls(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, "tracker:\n  provider: none\n  project: null\n  label: null\n"
    )
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    assert loaded.tracker.provider == "none"


@pytest.mark.parametrize(
    "key",
    [
        "team",
        "project",
        "label",
        "eligible_state",
        "in_progress_state",
        "success_state",
        "blocked_state",
        "user_attention_label",
        "mcp_server",
        "claude_mcp_config",
    ],
)
def test_tracker_provider_none_rejects_other_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError) as error:
        DispatchConfig(repo_root=tmp_path, tracker={"provider": "none", key: "x"})
    message = str(error.value)
    assert f"tracker.{key}" in message
    assert "remove those keys or set tracker.provider: linear" in message


def test_tracker_provider_none_rejects_blank_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tracker.team"):
        DispatchConfig(repo_root=tmp_path, tracker={"team": "  "})


def test_tracker_linear_loads_and_strips_strings(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """tracker:
  provider: linear
  team: " ENG "
  label: " repo-a "
  eligible_state: " Todo "
  in_progress_state: " In Progress "
  success_state: " In Review "
  blocked_state: " Blocked "
  user_attention_label: " needs-operator "
  mcp_server: " linear "
""",
    )
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    tracker = loaded.tracker
    assert tracker.provider == "linear"
    assert tracker.enabled is True
    assert tracker.team == "ENG"
    assert tracker.project is None
    assert tracker.label == "repo-a"
    assert tracker.eligible_state == "Todo"
    assert tracker.in_progress_state == "In Progress"
    assert tracker.success_state == "In Review"
    assert tracker.blocked_state == "Blocked"
    assert tracker.user_attention_label == "needs-operator"
    assert tracker.mcp_server == "linear"
    assert tracker.claude_mcp_config is None


@pytest.mark.parametrize(
    "key",
    ["team", "eligible_state", "in_progress_state", "success_state", "mcp_server"],
)
@pytest.mark.parametrize("value", [_DROP, "", "   "])
def test_tracker_linear_requires_keys(
    tmp_path: Path, key: str, value: object
) -> None:
    with pytest.raises(ValueError) as error:
        DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker(**{key: value}))
    message = str(error.value)
    assert f"tracker.{key}" in message
    assert "requires non-empty" in message


def test_tracker_linear_rejects_project_and_label(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as error:
        DispatchConfig(
            repo_root=tmp_path, tracker=_linear_tracker(label="repo-a")
        )
    message = str(error.value)
    assert "exactly one of tracker.project and tracker.label" in message
    assert "both are set" in message


def test_tracker_linear_rejects_neither_project_nor_label(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as error:
        DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker(project=_DROP))
    message = str(error.value)
    assert "exactly one of tracker.project and tracker.label" in message
    assert "set one" in message


def test_tracker_linear_accepts_label_instead_of_project(tmp_path: Path) -> None:
    config = DispatchConfig(
        repo_root=tmp_path, tracker=_linear_tracker(project=_DROP, label="repo-a")
    )
    assert config.tracker.label == "repo-a"
    assert config.tracker.project is None


@pytest.mark.parametrize(
    "key", ["project", "label", "blocked_state", "user_attention_label"]
)
@pytest.mark.parametrize("value", ["", "   "])
def test_tracker_linear_rejects_blank_optional_values(
    tmp_path: Path, key: str, value: str
) -> None:
    overrides: dict[str, object] = {key: value}
    if key == "label":
        overrides["project"] = _DROP
    with pytest.raises(ValueError, match=f"tracker.{key} must not be empty"):
        DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker(**overrides))


def test_tracker_linear_optional_keys_may_be_omitted(tmp_path: Path) -> None:
    config = DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker())
    assert config.tracker.blocked_state is None
    assert config.tracker.user_attention_label is None


@pytest.mark.parametrize("server", ["linear", "Linear_2", "my-linear", "a"])
def test_tracker_mcp_server_accepts_simple_names(tmp_path: Path, server: str) -> None:
    config = DispatchConfig(
        repo_root=tmp_path, tracker=_linear_tracker(mcp_server=server)
    )
    assert config.tracker.mcp_server == server


@pytest.mark.parametrize(
    "server", ["my linear", "linear.app", "linear/v1", "linear:1", "lin*"]
)
def test_tracker_mcp_server_rejects_other_characters(
    tmp_path: Path, server: str
) -> None:
    with pytest.raises(ValueError, match="tracker.mcp_server") as error:
        DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker(mcp_server=server))
    assert f"`{server}`" in str(error.value)


def test_tracker_claude_mcp_config_resolves_relative_to_repo_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    mcp_file = tmp_path / "config" / "linear-mcp.json"
    mcp_file.write_text("{}", encoding="utf-8")
    config_path = _write_config(
        tmp_path,
        """tracker:
  provider: linear
  team: ENG
  project: Platform
  eligible_state: Todo
  in_progress_state: In Progress
  success_state: In Review
  mcp_server: linear
  claude_mcp_config: config/linear-mcp.json
""",
    )
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    assert loaded.tracker.claude_mcp_config == Path("config/linear-mcp.json")
    assert loaded.tracker_claude_mcp_config_path == mcp_file.resolve()


def test_tracker_claude_mcp_config_accepts_absolute_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    mcp_file = tmp_path / "outside" / "linear-mcp.json"
    mcp_file.parent.mkdir()
    mcp_file.write_text("{}", encoding="utf-8")
    config = DispatchConfig(
        repo_root=repo_root, tracker=_linear_tracker(claude_mcp_config=mcp_file)
    )
    assert config.tracker_claude_mcp_config_path == mcp_file


def test_tracker_claude_mcp_config_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tracker.claude_mcp_config") as error:
        DispatchConfig(
            repo_root=tmp_path,
            tracker=_linear_tracker(claude_mcp_config="missing/linear-mcp.json"),
        )
    expected = (tmp_path / "missing" / "linear-mcp.json").resolve()
    assert str(expected) in str(error.value)


def test_tracker_claude_mcp_config_rejects_directory(tmp_path: Path) -> None:
    (tmp_path / "mcp").mkdir()
    with pytest.raises(ValueError, match="tracker.claude_mcp_config"):
        DispatchConfig(
            repo_root=tmp_path, tracker=_linear_tracker(claude_mcp_config="mcp")
        )


def test_tracker_linear_requires_work_claim_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as error:
        DispatchConfig(
            repo_root=tmp_path,
            enforce_work_packet=False,
            work_packet_opt_out_reason="testing without the gate",
            tracker=_linear_tracker(),
        )
    message = str(error.value)
    assert "enforce_work_packet: true" in message
    assert "Linear mode requires the work-claim gate" in message


def test_tracker_none_allows_work_claim_gate_off(tmp_path: Path) -> None:
    config = DispatchConfig(
        repo_root=tmp_path,
        enforce_work_packet=False,
        work_packet_opt_out_reason="testing without the gate",
        tracker={"provider": "none"},
    )
    assert config.tracker.provider == "none"


def test_tracker_linear_rejects_antigravity_role(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as error:
        DispatchConfig(
            repo_root=tmp_path,
            agents={
                **config_module._default_agent_configs(),
                "planner": {"provider": "antigravity"},
            },
            tracker=_linear_tracker(),
        )
    message = str(error.value)
    assert (
        "tracker.provider: linear does not support provider: antigravity "
        "(agents.planner):"
    ) in message
    assert "no Linear MCP access" in message
    assert "Map these roles to claude or codex, or set tracker.provider: none." in (
        message
    )


def test_tracker_linear_rejects_antigravity_fallback(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        _LINEAR_YAML
        + """agents:
  backend:
    provider: claude
    fallback:
      provider: antigravity
""",
    )
    with pytest.raises(ValueError) as error:
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    message = str(error.value)
    assert "(agents.backend.fallback)" in message
    assert "agents.backend," not in message


def test_tracker_linear_names_every_antigravity_location_in_role_order(
    tmp_path: Path,
) -> None:
    # The YAML lists backend before planner; the message uses role order.
    config_path = _write_config(
        tmp_path,
        _LINEAR_YAML
        + """agents:
  backend:
    provider: claude
    fallback:
      provider: antigravity
  planner:
    provider: antigravity
""",
    )
    with pytest.raises(ValueError) as error:
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    assert "(agents.planner, agents.backend.fallback)" in str(error.value)


def test_tracker_linear_rejects_antigravity_role_with_unsafe_bypass(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        _LINEAR_YAML
        + """agents:
  frontend:
    provider: antigravity
    unsafe_bypass: true
""",
    )
    with pytest.raises(ValueError, match=r"\(agents\.frontend\)"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


@pytest.mark.parametrize("tracker_yaml", ["", "tracker:\n  provider: none\n"])
def test_vanilla_mode_loads_antigravity_roles_and_fallbacks(
    tmp_path: Path, tracker_yaml: str
) -> None:
    config_path = _write_config(
        tmp_path,
        tracker_yaml
        + """agents:
  planner:
    provider: antigravity
  backend:
    provider: claude
    fallback:
      provider: antigravity
""",
    )
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)
    assert loaded.tracker.enabled is False
    assert loaded.agents["planner"].provider == "antigravity"
    fallback = loaded.agents["backend"].fallback
    assert fallback is not None
    assert fallback.provider == "antigravity"


def test_tracker_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="api_key"):
        DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker(api_key="x"))
    config_path = _write_config(tmp_path, "tracker:\n  provider: none\n  teams: ENG\n")
    with pytest.raises(ValueError, match="teams"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_tracker_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider"):
        DispatchConfig(repo_root=tmp_path, tracker={"provider": "jira"})


def test_linear_role_tool_table() -> None:
    read = ("get_issue", "list_issues", "list_comments")
    assert config_module.LINEAR_ROLE_TOOLS == {
        "planner": (*read, "save_comment", "save_issue"),
        "backend": (*read, "save_comment"),
        "frontend": (*read, "save_comment"),
        "auditor": (*read, "save_comment", "save_issue"),
        "validator": read,
        "finalizer": read,
    }
    assert set(config_module.LINEAR_ROLE_TOOLS) == config_module.REQUIRED_AGENT_ROLES


def test_linear_tools_for_role_prefixes_in_table_order() -> None:
    assert config_module.linear_tools_for_role("auditor", "linear") == (
        "mcp__linear__get_issue",
        "mcp__linear__list_issues",
        "mcp__linear__list_comments",
        "mcp__linear__save_comment",
        "mcp__linear__save_issue",
    )
    assert config_module.linear_tools_for_role("finalizer", "my-linear") == (
        "mcp__my-linear__get_issue",
        "mcp__my-linear__list_issues",
        "mcp__my-linear__list_comments",
    )


def test_interpreter_grants_defaults_to_false(tmp_path: Path) -> None:
    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.interpreter_grants is False
    for agent in loaded.agents.values():
        assert agent.interpreter_grants is False
        if agent.provider == "claude":
            assert agent.claude_allowed_tools == (
                config_module.CLAUDE_DEFAULT_ALLOWED_TOOLS
            )
            assert "Bash(python:*)" not in agent.claude_allowed_tools


@pytest.mark.parametrize("value", [True, False])
def test_interpreter_grants_loads_from_yaml(tmp_path: Path, value: bool) -> None:
    config_path = _write_config(
        tmp_path,
        f"interpreter_grants: {str(value).lower()}\n"
        "agents:\n"
        "  backend:\n"
        "    provider: codex\n"
        "    fallback:\n"
        "      provider: claude\n"
        "  auditor:\n"
        "    provider: claude\n"
        "    allowed_tools: [Read]\n",
    )

    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.interpreter_grants is value
    expected = config_module.claude_default_allowed_tools(value)
    assert loaded.agents["validator"].claude_allowed_tools == expected
    fallback = loaded.agents["backend"].fallback
    assert fallback is not None
    assert fallback.interpreter_grants is value
    assert fallback.claude_allowed_tools == expected
    # An explicit list is kept as written whatever the flag says.
    assert loaded.agents["auditor"].claude_allowed_tools == ("Read",)


def test_interpreter_grants_is_not_a_role_key(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        "agents:\n  auditor:\n    provider: claude\n    interpreter_grants: true\n",
    )

    with pytest.raises(ValueError, match="interpreter_grants"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


def test_interpreter_grants_survive_revalidation(tmp_path: Path) -> None:
    loaded = DispatchConfig(repo_root=tmp_path, interpreter_grants=True)
    revalidated = DispatchConfig.model_validate(loaded.model_dump())

    assert revalidated.interpreter_grants is True
    assert revalidated.agents["auditor"].claude_allowed_tools == (
        config_module.claude_default_allowed_tools(True)
    )


def test_interpreter_grants_do_not_mutate_the_callers_agent_config(
    tmp_path: Path,
) -> None:
    agent = config_module.AgentConfig(provider="claude")
    agents = {**config_module._default_agent_configs(), "auditor": agent}

    loaded = DispatchConfig(repo_root=tmp_path, agents=agents, interpreter_grants=True)

    assert loaded.agents["auditor"].interpreter_grants is True
    assert agent.interpreter_grants is False


def test_uses_default_allowed_tools_tracks_the_list_source(tmp_path: Path) -> None:
    explicit = config_module.AgentConfig(provider="claude", allowed_tools=("Read",))
    fallback_default = config_module.AgentConfig(provider="claude", model="other")
    agents = {
        **config_module._default_agent_configs(),
        "planner": config_module.AgentConfig(provider="claude"),
        "auditor": explicit,
        "validator": config_module.AgentConfig(
            provider="claude", allowed_tools=("Read",), fallback=fallback_default
        ),
    }

    for tracker in (None, _linear_tracker()):
        kwargs = {} if tracker is None else {"tracker": tracker}
        loaded = DispatchConfig(
            repo_root=tmp_path, agents=agents, interpreter_grants=True, **kwargs
        )
        planner = loaded.agents["planner"]
        assert planner.uses_default_allowed_tools is True
        assert loaded.agents["auditor"].uses_default_allowed_tools is False
        validator = loaded.agents["validator"]
        assert validator.uses_default_allowed_tools is False
        assert validator.fallback is not None
        # The Linear derivation sets allowed_tools on default-list launches
        # but keeps the record that the list built on the default.
        assert validator.fallback.uses_default_allowed_tools is True
        if tracker is not None:
            assert planner.allowed_tools is not None
            assert validator.fallback.allowed_tools is not None


_LINEAR_YAML = """tracker:
  provider: linear
  team: ENG
  project: Platform
  eligible_state: Todo
  in_progress_state: In Progress
  success_state: In Review
  mcp_server: linear
"""


def test_linear_mode_load_derives_claude_allowed_tools(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        _LINEAR_YAML
        + """agents:
  planner:
    provider: claude
  backend:
    provider: claude
    allowed_tools: [Read, "Bash(pytest:*)"]
""",
    )
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=config_path)

    assert loaded.agents["planner"].allowed_tools == (
        *config_module.CLAUDE_DEFAULT_ALLOWED_TOOLS,
        *config_module.linear_tools_for_role("planner", "linear"),
    )
    assert loaded.agents["backend"].allowed_tools == (
        "Read",
        "Bash(pytest:*)",
        *config_module.linear_tools_for_role("backend", "linear"),
    )
    for role in ("auditor", "validator", "finalizer"):
        assert loaded.agents[role].allowed_tools == (
            *config_module.CLAUDE_DEFAULT_ALLOWED_TOOLS,
            *config_module.linear_tools_for_role(role, "linear"),
        )
    # Default frontend is Gemini, which has no per-role tool list.
    assert loaded.agents["frontend"].provider == "gemini"
    assert loaded.agents["frontend"].allowed_tools is None
    # The config file itself is never rewritten.
    assert "mcp__" not in config_path.read_text(encoding="utf-8")


def test_linear_mode_model_validate_derives_claude_allowed_tools(
    tmp_path: Path,
) -> None:
    loaded = DispatchConfig.model_validate(
        {"repo_root": tmp_path, "tracker": _linear_tracker()}
    )

    assert loaded.agents["auditor"].allowed_tools == (
        *config_module.CLAUDE_DEFAULT_ALLOWED_TOOLS,
        *config_module.linear_tools_for_role("auditor", "linear"),
    )
    assert loaded.agents["backend"].provider == "codex"
    assert loaded.agents["backend"].allowed_tools is None


def test_linear_mode_derivation_is_idempotent_on_revalidation(
    tmp_path: Path,
) -> None:
    loaded = DispatchConfig(repo_root=tmp_path, tracker=_linear_tracker())
    revalidated = DispatchConfig.model_validate(loaded.model_dump())

    assert revalidated.agents["validator"].allowed_tools == (
        loaded.agents["validator"].allowed_tools
    )


def test_linear_mode_mcp_config_path_lives_on_derived_agent(tmp_path: Path) -> None:
    mcp_file = tmp_path / "linear-mcp.json"
    mcp_file.write_text("{}", encoding="utf-8")
    loaded = DispatchConfig(
        repo_root=tmp_path,
        tracker=_linear_tracker(claude_mcp_config="linear-mcp.json"),
    )

    assert loaded.agents["auditor"].claude_mcp_config_path == mcp_file.resolve()
    assert loaded.agents["backend"].claude_mcp_config_path is None
    assert "claude_mcp_config" not in loaded.agents["auditor"].model_dump()


def test_vanilla_mode_leaves_allowed_tools_unset(tmp_path: Path) -> None:
    loaded = DispatchConfig(repo_root=tmp_path)
    assert all(agent.allowed_tools is None for agent in loaded.agents.values())
    assert all(
        agent.claude_mcp_config_path is None for agent in loaded.agents.values()
    )


@pytest.mark.parametrize(
    "key", ["claude_mcp_config", "_claude_mcp_config", "claude_mcp_config_path"]
)
def test_agent_yaml_rejects_new_mcp_config_key(tmp_path: Path, key: str) -> None:
    (tmp_path / "linear-mcp.json").write_text("{}", encoding="utf-8")
    config_path = _write_config(
        tmp_path,
        _LINEAR_YAML
        + f"""agents:
  auditor:
    provider: claude
    {key}: linear-mcp.json
""",
    )
    with pytest.raises(ValueError, match=key):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


@pytest.mark.parametrize(
    "relative_path",
    ["dispatch_config.example.yaml", "examples/reference-workflow/dispatch_config.yaml"],
)
def test_shipped_configs_leave_tracker_off_and_document_it(
    tmp_path: Path, relative_path: str
) -> None:
    source = Path(__file__).resolve().parents[1] / relative_path
    text = source.read_text(encoding="utf-8")
    assert "# tracker:" in text
    for key in ("provider", *config_module._TRACKER_SETTING_KEYS):
        assert f"#   {key}:" in text, key
    loaded = load_dispatch_config(repo_root=tmp_path, config_path=source)
    assert loaded.tracker.provider == "none"
