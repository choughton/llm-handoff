from __future__ import annotations

from pathlib import Path

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


def test_load_dispatch_config_uses_defaults_when_file_is_absent(
    tmp_path: Path,
) -> None:
    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.handoff_path == Path("docs/handoff/HANDOFF.md")
    assert loaded.project_state_path == Path("PROJECT_STATE.md")
    assert loaded.normalizer.provider == "claude"
    assert loaded.agents["backend"].skill_name == "llm-handoff"


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
