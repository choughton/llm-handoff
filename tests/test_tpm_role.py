"""TPM-1 and TPM-2: `tpm` is a recognized role name, and a configured tpm
has a launch path, but the dispatch loop still pauses on a tpm route."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, get_args
from unittest.mock import Mock

import pytest

from llm_handoff import agent_roles
from llm_handoff import config as config_module
from llm_handoff import doctor
from llm_handoff import rationalization_detector
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.normalizer_models import ValidAgent, normalizer_prompt
from llm_handoff.roles import (
    CANONICAL_DISPATCH_ROLES,
    CANONICAL_NEXT_AGENT_SET,
    DispatchRole,
    ExecutableRole,
    normalize_agent_label,
    normalize_next_agent_value,
    role_display_name,
)
from llm_handoff.router import NextAgent, route as route_handoff


SIX_ROLES = frozenset(
    {"planner", "backend", "frontend", "auditor", "validator", "finalizer"}
)


# Check 1: roles.py


@pytest.mark.parametrize("value", ["tpm", "TPM", " tpm "])
def test_normalize_next_agent_value_accepts_tpm(value: str) -> None:
    assert normalize_next_agent_value(value) == "tpm"


def test_normalize_agent_label_maps_tpm_prose_to_tpm() -> None:
    assert normalize_agent_label("tpm picks next story") == ("tpm", ())


def test_normalize_agent_label_still_drops_provider_labelled_tpm() -> None:
    assert normalize_agent_label("codex-tpm") == (None, ())


def test_tpm_is_a_dispatch_role_but_not_executable_yet() -> None:
    assert "tpm" in get_args(DispatchRole)
    assert "tpm" not in get_args(ExecutableRole)
    assert "tpm" in CANONICAL_NEXT_AGENT_SET
    assert CANONICAL_DISPATCH_ROLES.index("tpm") < CANONICAL_DISPATCH_ROLES.index(
        "user"
    )


def test_tpm_display_name() -> None:
    assert role_display_name("tpm") == "tpm"


# Check 2: config.py


def test_agent_role_includes_tpm_without_changing_required_roles() -> None:
    assert "tpm" in get_args(config_module.AgentRole)
    assert "tpm" not in config_module.DEFAULT_AGENT_PROVIDERS
    assert config_module.REQUIRED_AGENT_ROLES == SIX_ROLES
    assert set(config_module._default_agent_configs()) == SIX_ROLES
    assert "tpm" not in config_module.LINEAR_ROLE_TOOLS


def test_config_without_agents_tpm_loads_six_default_agents(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text("handoff_path: HANDOFF.md\n", encoding="utf-8")

    loaded = config_module.load_dispatch_config(
        repo_root=tmp_path, config_path=config_path
    )

    assert set(loaded.agents) == SIX_ROLES
    assert loaded.agents == config_module._default_agent_configs()


# Check 3: router.py


def test_router_routes_frontmatter_next_agent_tpm() -> None:
    assert "tpm" in get_args(NextAgent)
    decision = route_handoff(
        "---\n"
        "next_agent: tpm\n"
        'reason: "Pick the next story."\n'
        "producer: planner\n"
        "---\n\n"
        "# Handoff\n",
        project_state_content="# PROJECT STATE\n",
    )

    assert decision.route == "tpm"
    assert decision.source.startswith("frontmatter")
    assert decision.source != "unknown"


# Check 4: normalizer_models.py and handoff_normalizer.py


def test_valid_agent_and_normalizer_prompt_include_tpm() -> None:
    assert "tpm" in get_args(ValidAgent)
    assert "tpm" in normalizer_prompt("the tpm")


def test_handoff_normalizer_canonical_set_includes_tpm() -> None:
    handoff_normalizer = importlib.import_module("llm_handoff.handoff_normalizer")

    assert "tpm" in handoff_normalizer.CANONICAL_NEXT_AGENT_SET


# Check 5: orchestrator interim guard


def _tpm_agent(**overrides: Any) -> config_module.AgentConfig:
    fields: dict[str, Any] = {"provider": "codex", "skill_name": "llm-handoff-tpm"}
    fields.update(overrides)
    return config_module.AgentConfig(**fields)


def _agents_with_tpm(tpm: config_module.AgentConfig | None = None):
    return {
        **config_module._default_agent_configs(),
        "tpm": tpm or _tpm_agent(),
    }


def _dispatch_config(repo_root: Path, **overrides: Any):
    fields: dict[str, Any] = {
        "repo_root": repo_root,
        "pause_recovery": "planner",
        "poll_interval_seconds": 0,
        "audit": {
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the tpm pause only.",
        },
        "enforce_work_packet": False,
        "work_packet_opt_out_reason": "Tests cover routing without the gate.",
    }
    fields.update(overrides)
    return config_module.DispatchConfig(**fields)


@pytest.mark.parametrize("configured", [False, True])
def test_run_loop_pauses_on_tpm_route_without_dispatching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text(
        "---\n"
        "next_agent: tpm\n"
        'reason: "Pick the next story."\n'
        "producer: planner\n"
        "---\n\n"
        "# Handoff\n",
        encoding="utf-8",
    )
    (tmp_path / "PROJECT_STATE.md").write_text("# PROJECT STATE\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("dispatch test fixture\n", encoding="utf-8")

    orchestrator = importlib.import_module("llm_handoff.orchestrator")
    dispatch_calls: list[Any] = []
    recovery_calls: list[Any] = []
    pause_reasons: list[str] = []
    real_pause = orchestrator._pause_at_dispatcher_pause

    def fake_dispatch(*args: Any, **kwargs: Any) -> Any:
        dispatch_calls.append((args, kwargs))
        raise AssertionError("_dispatch_route must not run for a tpm route")

    def fake_schedule(*args: Any, **kwargs: Any) -> Any:
        recovery_calls.append((args, kwargs))
        raise AssertionError("pause recovery must not be scheduled for tpm")

    def spy_pause(*args: Any, **kwargs: Any) -> Any:
        pause_reasons.append(kwargs["reason"])
        return real_pause(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_dispatch_route", fake_dispatch)
    monkeypatch.setattr(orchestrator, "_schedule_pause_recovery", fake_schedule)
    monkeypatch.setattr(orchestrator, "_pause_at_dispatcher_pause", spy_pause)
    log_messages: list[tuple[str, str]] = []

    config = (
        _dispatch_config(tmp_path, agents=_agents_with_tpm())
        if configured
        else _dispatch_config(tmp_path)
    )
    assert ("tpm" in config.agents) is configured
    exit_code = orchestrator.run_loop(
        config,
        log=lambda level, message: log_messages.append((level, message)),
    )

    assert exit_code == 0
    assert dispatch_calls == []
    assert recovery_calls == []
    assert pause_reasons == [orchestrator.TPM_ROUTE_NOT_LAUNCHED_REASON]
    assert "tpm" in pause_reasons[0]
    assert pause_reasons[0] == (
        "HANDOFF.md routes to tpm, which this dispatcher does not launch yet. "
        "Route to planner or an implementer; dispatch will resume after the "
        "file changes."
    )
    assert any("routes to tpm" in message for _, message in log_messages)


# Check 6: rationalization patterns


def test_tpm_rationalization_patterns_load() -> None:
    patterns = rationalization_detector._PATTERNS["tpm"]

    assert len(patterns) >= 3
    assert all(entry["failure_mode"] for entry in patterns)
    assert rationalization_detector._role_key("tpm") == "tpm"


@pytest.mark.parametrize(
    "line",
    [
        "Add validation as needed.",
        "No need to check the scope; assume the story is ready.",
        "Run relevant tests before handing back.",
    ],
)
def test_tpm_rationalization_patterns_match_routine_story_pick_gaps(
    line: str,
) -> None:
    matches = rationalization_detector.detect("tpm", line, "ready_for_review")

    assert matches
    assert all(match.role == "tpm" for match in matches)


# TPM-2 (a), (b): config load


def test_config_with_agents_tpm_merges_codex_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "agents:\n"
        "  tpm:\n"
        "    provider: codex\n"
        "    skill_name: llm-handoff-tpm\n",
        encoding="utf-8",
    )

    loaded = config_module.load_dispatch_config(
        repo_root=tmp_path, config_path=config_path
    )

    assert set(loaded.agents) == SIX_ROLES | {"tpm"}
    tpm = loaded.agents["tpm"]
    assert tpm.provider == "codex"
    assert tpm.skill_name == "llm-handoff-tpm"
    assert tpm.binary == config_module.CODEX_BINARY
    assert tpm.timeout_ms == config_module.AGENT_TIMEOUT_MS
    defaults = config_module._default_agent_configs()
    for role in SIX_ROLES:
        assert loaded.agents[role] == defaults[role]


def test_config_without_agents_tpm_has_no_tpm_key(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(
        "agents:\n  backend:\n    provider: codex\n", encoding="utf-8"
    )

    loaded = config_module.load_dispatch_config(
        repo_root=tmp_path, config_path=config_path
    )

    assert "tpm" not in loaded.agents


# TPM-2 (c), (d): invoke_tpm_role


def _handoff(tmp_path: Path) -> Path:
    path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nnext_agent: tpm\n---\n", encoding="utf-8")
    return path


def test_invoke_tpm_role_launches_role_tpm_without_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = _handoff(tmp_path)
    result = DispatchResult(stdout="ok", stderr="", exit_code=0, elapsed_seconds=1.0)
    codex = Mock(return_value=result)
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)
    agent_config = _tpm_agent(resume=True)
    assert agent_config.resume is True

    returned = agent_roles.invoke_tpm_role(
        handoff,
        additional_instruction="extra",
        agent_config=agent_config,
        repo_root=tmp_path,
    )

    assert returned is result
    codex.assert_called_once()
    kwargs = codex.call_args.kwargs
    assert kwargs["role_name"] == "tpm"
    assert kwargs["use_resume"] is False
    assert kwargs["skill_name"] == "llm-handoff-tpm"
    assert kwargs["additional_instruction"] == "extra"
    assert kwargs["repo_root"] == tmp_path


def test_invoke_tpm_role_requires_agent_config(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        agent_roles.invoke_tpm_role(_handoff(tmp_path))  # type: ignore[call-arg]


def test_invoke_tpm_role_fails_over_to_tpm_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = _handoff(tmp_path)
    codex = Mock(
        return_value=DispatchResult(
            stdout="",
            stderr="Process timed out after 1200 seconds.",
            exit_code=1,
            elapsed_seconds=10.0,
        )
    )
    claude = Mock(
        return_value=SubagentResult(
            stdout="done", stderr="", exit_code=0, elapsed_seconds=2.0
        )
    )
    monkeypatch.setattr(agent_roles, "invoke_codex", codex)
    monkeypatch.setattr(agent_roles, "invoke_claude_subagent", claude)
    logs: list[tuple[str, str]] = []

    result = agent_roles.invoke_tpm_role(
        handoff,
        agent_config=_tpm_agent(
            fallback=config_module.AgentConfig(
                provider="claude", model="claude-sonnet-4-6"
            )
        ),
        log=lambda level, message: logs.append((level, message)),
        repo_root=tmp_path,
    )

    assert codex.call_count == 1
    assert claude.call_count == 1
    assert claude.call_args.args[0] == "tpm"
    assert claude.call_args.kwargs["model"] == "claude-sonnet-4-6"
    assert result.exit_code == 0
    assert result.stdout == "done"
    warnings = [message for level, message in logs if level == "WARN"]
    assert len(warnings) == 1
    assert warnings[0].startswith("tpm (codex) failed:")


# TPM-2 (e), (f): _dispatch_route


@pytest.mark.parametrize("commit_mode", ["agent", "dispatcher"])
def test_dispatch_route_tpm_invokes_tpm_role_with_configured_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_mode: str
) -> None:
    orchestrator = importlib.import_module("llm_handoff.orchestrator")
    from llm_handoff.dispatcher_commit import PLANNER_COMMIT_MODE_INSTRUCTION

    overrides: dict[str, Any] = {"agents": _agents_with_tpm()}
    if commit_mode == "dispatcher":
        overrides.update(
            commit_mode="dispatcher",
            enforce_work_packet=True,
            work_packet_opt_out_reason=None,
        )
    config = _dispatch_config(tmp_path, **overrides)
    result = DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.1)
    calls: list[tuple[Path, dict[str, Any]]] = []

    def fake_tpm(handoff_path: Path, **kwargs: Any) -> DispatchResult:
        calls.append((handoff_path, kwargs))
        return result

    monkeypatch.setattr(orchestrator, "invoke_tpm_role", fake_tpm)
    for name in ("invoke_planner_role", "invoke_backend_role", "invoke_frontend_role"):
        monkeypatch.setattr(
            orchestrator,
            name,
            Mock(side_effect=AssertionError(f"{name} must not run")),
        )
    logs: list[tuple[str, str]] = []

    returned, label = orchestrator._dispatch_route(
        "tpm",
        config,
        lambda level, message: logs.append((level, message)),
    )

    assert returned is result
    assert label == "tpm"
    assert ("DISPATCH", "Dispatching tpm.") in logs
    assert len(calls) == 1
    handoff_path, kwargs = calls[0]
    assert handoff_path == config.handoff_full_path
    assert kwargs["agent_config"] is config.agents["tpm"]
    assert kwargs["repo_root"] == config.repo_root
    if commit_mode == "dispatcher":
        assert kwargs["additional_instruction"] == PLANNER_COMMIT_MODE_INSTRUCTION
    else:
        assert kwargs["additional_instruction"] is None


def test_dispatch_route_tpm_without_agents_tpm_raises_and_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = importlib.import_module("llm_handoff.orchestrator")
    config = _dispatch_config(tmp_path)
    assert "tpm" not in config.agents
    tpm = Mock(side_effect=AssertionError("invoke_tpm_role must not run"))
    monkeypatch.setattr(orchestrator, "invoke_tpm_role", tpm)
    logs: list[tuple[str, str]] = []

    with pytest.raises(ValueError, match=r"tpm is not configured \(agents\.tpm is absent\)\."):
        orchestrator._dispatch_route(
            "tpm",
            config,
            lambda level, message: logs.append((level, message)),
        )

    tpm.assert_not_called()
    assert logs == []


# TPM-2 (h): Linear mode rejects an antigravity tpm


_LINEAR_YAML = """tracker:
  provider: linear
  team: ENG
  project: Platform
  eligible_state: Todo
  in_progress_state: In Progress
  success_state: In Review
  mcp_server: linear
agents:
  planner:
    provider: claude
  frontend:
    provider: claude
"""


@pytest.mark.parametrize(
    ("tpm_yaml", "location"),
    [
        ("  tpm:\n    provider: antigravity\n", "agents.tpm"),
        (
            "  tpm:\n    provider: codex\n    fallback:\n"
            "      provider: antigravity\n",
            "agents.tpm.fallback",
        ),
    ],
)
def test_linear_mode_rejects_antigravity_tpm(
    tmp_path: Path, tpm_yaml: str, location: str
) -> None:
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(_LINEAR_YAML + tpm_yaml, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        config_module.load_dispatch_config(
            repo_root=tmp_path, config_path=config_path
        )

    message = str(excinfo.value)
    assert "does not support provider: antigravity" in message
    assert f"({location})" in message
    assert "agents.planner" not in message


# TPM-2 (i): doctor


def _doctor_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agents_yaml: str
) -> list[str]:
    (tmp_path / ".git").mkdir()
    config_path = tmp_path / "dispatch_config.yaml"
    config_path.write_text(agents_yaml, encoding="utf-8")
    monkeypatch.setattr(doctor, "_check_git", lambda repo_root, checks: False)
    monkeypatch.setattr(doctor, "_role_binary_available", lambda binary, root: True)

    report = doctor.run_doctor(repo_root=tmp_path, config_path=config_path)

    return [check.format() for check in report.checks]


def test_doctor_reports_tpm_not_configured_without_agents_tpm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = _doctor_checks(tmp_path, monkeypatch, "handoff_path: HANDOFF.md\n")

    assert "PASS role tpm: not configured (planner picks stories)" in lines
    assert [line for line in lines if line.startswith("PASS role tpm")] == [
        "PASS role tpm: not configured (planner picks stories)"
    ]


def test_doctor_checks_tpm_binary_when_agents_tpm_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = _doctor_checks(
        tmp_path,
        monkeypatch,
        "agents:\n"
        "  tpm:\n"
        "    provider: codex\n"
        "    skill_name: llm-handoff-tpm\n"
        "    fallback:\n"
        "      provider: claude\n",
    )

    assert "PASS role tpm: not configured (planner picks stories)" not in lines
    assert any(
        line.startswith("PASS role tpm: provider codex binary") for line in lines
    )
    assert any(
        line.startswith("PASS role tpm fallback: provider claude binary")
        for line in lines
    )


# TPM-2 (j): example config


def test_example_config_loads_exactly_six_default_agents(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "dispatch_config.example.yaml"
    text = example.read_text(encoding="utf-8")
    assert "# tpm:" in text
    assert "#   skill_name: llm-handoff-tpm" in text

    loaded = config_module.load_dispatch_config(
        repo_root=tmp_path, config_path=example
    )

    assert set(loaded.agents) == SIX_ROLES
