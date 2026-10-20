"""TPM-1: `tpm` is a recognized role name, not yet a launchable one."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, get_args

import pytest

from llm_handoff import config as config_module
from llm_handoff import rationalization_detector
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


def _dispatch_config(repo_root: Path):
    return config_module.DispatchConfig(
        repo_root=repo_root,
        pause_recovery="planner",
        poll_interval_seconds=0,
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the tpm pause only.",
        },
        enforce_work_packet=False,
        work_packet_opt_out_reason="Tests cover routing without the gate.",
    )


def test_run_loop_pauses_on_tpm_route_without_dispatching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    exit_code = orchestrator.run_loop(
        _dispatch_config(tmp_path),
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
