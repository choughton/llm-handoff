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
  provider: codex
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no next-agent normalizer adapter"):
        load_dispatch_config(repo_root=tmp_path, config_path=config_path)


@pytest.mark.parametrize(
    ("provider", "expected_model"),
    [
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
