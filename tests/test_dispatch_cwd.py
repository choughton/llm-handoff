from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

import pytest

from llm_handoff import agent_process
from llm_handoff import config as config_module
from llm_handoff import orchestrator
from llm_handoff import supervisor
from llm_handoff.agent_providers import manual as manual_provider
from llm_handoff.agent_types import SubagentResult
from llm_handoff.normalizer_providers import claude as claude_normalizer


# The documented two-repo workflow runs ``python -m llm_handoff --repo-root
# <target>`` from the dispatcher's own source checkout. Every provider CLI must
# launch inside the target repo, never inside the checkout that happens to be
# the process cwd.


class _FakeStream:
    def readline(self) -> str:
        return ""

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode = 0
        self.pid = 4242

    def poll(self) -> int:
        return 0

    def wait(self) -> int:
        return 0

    def kill(self) -> None:
        return None


def _two_repos(tmp_path: Path) -> tuple[Path, Path]:
    dispatcher_checkout = tmp_path / "llm-handoff-source"
    target_repo = tmp_path / "target-repo"
    dispatcher_checkout.mkdir()
    target_repo.mkdir()
    handoff_path = target_repo / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text(
        """---
next_agent: backend
reason: Implement the next story.
producer: planner
---

## Work Packet
""",
        encoding="utf-8",
    )
    return dispatcher_checkout, target_repo


def _capture_popen(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    popen_calls: list[dict[str, Any]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_calls.append({"command": list(command), **kwargs})
        return _FakeProcess()

    monkeypatch.setattr(agent_process.subprocess, "Popen", fake_popen)
    return popen_calls


@pytest.mark.parametrize(
    ("route", "expected_provider"),
    [
        ("backend", "codex"),
        ("planner", "antigravity"),
        ("frontend", "antigravity"),
        ("auditor", "claude"),
        ("validator", "claude"),
    ],
)
def test_dispatch_route_launches_provider_cli_in_config_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_provider: str,
) -> None:
    dispatcher_checkout, target_repo = _two_repos(tmp_path)
    monkeypatch.chdir(dispatcher_checkout)
    popen_calls = _capture_popen(monkeypatch)
    config = config_module.DispatchConfig(
        repo_root=target_repo,
        # The auditor route here is the single auditor launch; the security
        # reviewer fan-out is covered in tests/test_audit_review.py.
        audit={
            "reviewers": ["quality"],
            "security_opt_out_reason": "Tests cover the single-auditor route.",
        },
    )
    assert config.agents[route].provider == expected_provider
    assert Path.cwd().resolve() != config.repo_root

    orchestrator._dispatch_route(route, config, lambda level, message: None)

    assert popen_calls, "the provider CLI was not launched"
    assert [call["cwd"] for call in popen_calls] == [config.repo_root] * len(
        popen_calls
    )


def test_dispatch_route_manual_frontend_names_handoff_in_target_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher_checkout, target_repo = _two_repos(tmp_path)
    monkeypatch.chdir(dispatcher_checkout)
    monkeypatch.setattr(manual_provider, "_wait_for_manual_continue", lambda: None)
    config = config_module.DispatchConfig(
        repo_root=target_repo,
        use_manual_frontend=True,
    )
    log_messages: list[tuple[str, str]] = []

    orchestrator._dispatch_route(
        "frontend",
        config,
        lambda level, message: log_messages.append((level, message)),
    )

    pause_messages = [message for level, message in log_messages if level == "PAUSE"]
    assert len(pause_messages) == 1
    assert str(config.handoff_full_path) in pause_messages[0]
    assert str(dispatcher_checkout) not in pause_messages[0]


def test_manual_frontend_resolves_relative_handoff_against_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher_checkout, target_repo = _two_repos(tmp_path)
    monkeypatch.chdir(dispatcher_checkout)
    monkeypatch.setattr(manual_provider, "_wait_for_manual_continue", lambda: None)
    messages: list[str] = []

    manual_provider.invoke_manual_frontend(
        Path("docs/handoff/HANDOFF.md"),
        log=lambda level, message: messages.append(message),
        repo_root=target_repo,
    )

    expected = (target_repo / "docs" / "handoff" / "HANDOFF.md").resolve()
    assert str(expected) in messages[0]
    assert str(dispatcher_checkout) not in messages[0]


def test_claude_cli_normalizer_runs_in_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher_checkout, target_repo = _two_repos(tmp_path)
    monkeypatch.chdir(dispatcher_checkout)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run_calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append(kwargs)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"structured_output": {"normalized": "backend"}}',
            stderr="",
        )

    monkeypatch.setattr(claude_normalizer.subprocess, "run", fake_run)

    normalized = claude_normalizer.normalize_next_agent_with_claude(
        "implementer",
        model="claude-haiku-test",
        timeout_ms=1000,
        repo_root=target_repo,
    )

    assert normalized == "backend"
    assert len(run_calls) == 1
    assert run_calls[0]["cwd"] == str(target_repo)


def test_orchestrator_passes_repo_root_to_next_agent_normalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher_checkout, target_repo = _two_repos(tmp_path)
    monkeypatch.chdir(dispatcher_checkout)
    config = config_module.DispatchConfig(repo_root=target_repo)
    handoff_content = """---
next_agent: implementer
reason: Implement the next story.
producer: planner
---
"""
    config.handoff_full_path.write_text(handoff_content, encoding="utf-8")
    normalizer_calls: list[dict[str, Any]] = []

    def fake_normalizer(freeform: str, **kwargs: Any) -> str:
        normalizer_calls.append({"freeform": freeform, **kwargs})
        return "backend"

    monkeypatch.setattr(orchestrator, "normalize_next_agent", fake_normalizer)

    rewritten = orchestrator._normalize_handoff_next_agent_file(
        config.handoff_full_path,
        handoff_content,
        config.normalizer,
        lambda level, message: None,
        repo_root=config.repo_root,
    )

    assert "next_agent: backend" in rewritten
    assert normalizer_calls == [
        {
            "freeform": "implementer",
            "provider": config.normalizer.provider,
            "model": config.normalizer.model,
            "timeout_ms": config.normalizer.timeout_ms,
            "repo_root": config.repo_root,
        }
    ]


def test_supervisor_repair_role_launches_in_target_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher_checkout, target_repo = _two_repos(tmp_path)
    (target_repo / ".git").mkdir()
    monkeypatch.chdir(dispatcher_checkout)
    captured: dict[str, Any] = {}

    def fake_invoke(subagent_name: str, prompt: str, **kwargs: Any) -> SubagentResult:
        captured["subagent_name"] = subagent_name
        captured.update(kwargs)
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.0)

    monkeypatch.setattr(supervisor, "invoke_support_role", fake_invoke)
    supervisor_config = supervisor.SupervisorConfig(
        repo_root=target_repo.resolve(),
        repair_role="validator",
        max_repair_attempts=1,
    )

    supervisor._run_repair_role(
        supervisor_config,
        target_repo / "failure-packet.json",
        lambda level, message: None,
    )

    assert captured["subagent_name"] == "supervisor-repair"
    assert captured["repo_root"] == target_repo.resolve()
