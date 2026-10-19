from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any

import pytest
import yaml

from llm_handoff import audit_review
from llm_handoff import config as config_module
from llm_handoff import ledger
from llm_handoff import orchestrator
from llm_handoff.agent_providers.codex import _CodexCommandResult
from llm_handoff.agent_types import DispatchResult, SubagentResult, _ProcessResult
from llm_handoff.audit_review import (
    AuditFanOutContext,
    ReviewFinding,
    ReviewScope,
    ReviewVerdict,
    ReviewerRun,
)
from llm_handoff.config import DispatchConfig, load_dispatch_config
from llm_handoff.router import parse_handoff_frontmatter_text, route
from llm_handoff.validator import validate_handoff_text


STORY_ID = "AUDIT-S1"
NL = chr(10)
# Captured at import, before the conftest fixture stubs the lookup per test.
REAL_REVIEWER_CLI_AVAILABLE = audit_review._reviewer_cli_available
ORIGINAL_AUDIT_PROMPT_TEMPLATE = (
    "Use the auditor agent to audit the work described in "
    "{handoff_path}. Read the repository instructions and relevant "
    "state files before judging the diff. Run the configured verification "
    "commands when available. Return a concise audit report, then update "
    "{handoff_path} with findings and a routing instruction for the "
    "next agent."
)
ORIGINAL_AUDIT_PROMPT = ORIGINAL_AUDIT_PROMPT_TEMPLATE.format(
    handoff_path="docs/handoff/HANDOFF.md"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_file(repo: Path, relative: str, content: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    base = _commit_file(repo, "AGENTS.md", "fixture\n", "base")
    return repo, base


def _backend_handoff(scope_sha: str) -> str:
    return f"""---
next_agent: auditor
reason: "Backend work complete; audit requested."
story_id: {STORY_ID}
story_title: Example story
status: ready_for_review
scope_sha: {scope_sha}
producer: backend
---

# Backend Handback

**Agent:** backend

## Completed Work

Implemented the assigned change at `{scope_sha}`.

## Verification Evidence

- **Commands run:** python -m pytest -q
- **Output summary:** exit 0
- **Commit SHA verified:** {scope_sha}
- **Files changed or reviewed:** src/app.py
- **Unresolved concerns:** none
"""


def _write_handoff(repo: Path, content: str) -> Path:
    path = repo / "docs" / "handoff" / "HANDOFF.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _fan_out_config(
    repo: Path,
    *,
    security_provider: str | None = "claude",
    block_severity: str = "high",
    agents: dict[str, Any] | None = None,
    **overrides: Any,
) -> DispatchConfig:
    audit: dict[str, Any] = {
        "reviewers": ["quality", "security"],
        "block_severity": block_severity,
    }
    if security_provider is not None:
        audit["security_provider"] = security_provider
    kwargs: dict[str, Any] = {
        "repo_root": repo,
        "poll_interval_seconds": 0,
        # Pauses exit here; pause recovery is covered in test_pause_recovery.py.
        "pause_recovery": "off",
        "audit": audit,
    }
    if agents is not None:
        kwargs["agents"] = agents
    kwargs.update(overrides)
    return DispatchConfig(**kwargs)


def _finding(severity: str, title: str = "Finding") -> dict[str, Any]:
    return {
        "severity": severity,
        "file": "src/app.py",
        "line": 3,
        "title": title,
        "description": f"{title} description.",
        "recommendation": f"Fix {title.lower()}.",
    }


def _quality_payload(
    *,
    verdict: str = "pass",
    next_agent: str = "planner",
    close_type: str | None = "story",
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": "Spec compliance and code quality reviewed.",
        "next_agent": next_agent,
        "reason": "Story approved; scope the next story.",
        "close_type": close_type,
        "commands_run": ["python -m pytest -q"],
        "output_summary": "pytest exit 0",
        "files_reviewed": ["src/app.py"],
        "unresolved_concerns": "none",
        "report": "## Checks Run\n- `python -m pytest -q`: PASS",
        "findings": findings or [],
    }


def _security_payload(
    *,
    verdict: str = "pass",
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": "Security review of the committed range.",
        "findings": findings or [],
    }


def _claude_envelope(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Security review complete.",
            "structured_output": payload,
        }
    )


def _subagent(
    stdout: str,
    exit_code: int = 0,
    usage: dict[str, object] | None = None,
    stderr: str = "",
) -> SubagentResult:
    return SubagentResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        elapsed_seconds=0.01,
        usage=usage,
    )


def _process(stdout: str, exit_code: int = 0, stderr: str = "") -> _ProcessResult:
    return _ProcessResult(stdout=stdout, stderr=stderr, exit_code=exit_code)


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def __call__(self, level: str, message: str) -> None:
        self.messages.append((level, message))

    def text(self) -> str:
        return "\n".join(message for _level, message in self.messages)


def _run_fan_out(
    config: DispatchConfig,
    handoff_content: str,
    *,
    quality_stdout: str,
    base_sha: str | None = None,
    quality_side_effect=None,
    quality_exit_code: int = 0,
    quality_usage: dict[str, object] | None = None,
) -> tuple[DispatchResult, _Recorder]:
    log = _Recorder()

    def quality_invoker(prompt: str, agent_config) -> SubagentResult:
        if quality_side_effect is not None:
            quality_side_effect(prompt, agent_config)
        return _subagent(quality_stdout, quality_exit_code, quality_usage)

    result = audit_review.run_audit_fan_out(
        config,
        context=AuditFanOutContext(
            cycle_number=2,
            handoff_content=handoff_content,
            implementer_base_sha=base_sha,
        ),
        frontmatter=parse_handoff_frontmatter_text(handoff_content),
        quality_invoker=quality_invoker,
        log=log,
    )
    return result, log


@pytest.fixture(autouse=True)
def _reset_bounces():
    ledger.reset_bounce(STORY_ID)
    yield
    ledger.reset_bounce(STORY_ID)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


OPT_OUT_REASON = "Throwaway prototype; no secrets or user data."
QUALITY_ONLY_AUDIT: dict[str, Any] = {
    "reviewers": ["quality"],
    "security_opt_out_reason": OPT_OUT_REASON,
}


def test_audit_config_defaults_run_quality_and_security(tmp_path: Path) -> None:
    loaded = load_dispatch_config(repo_root=tmp_path)

    assert config_module.DEFAULT_AUDIT_REVIEWERS == ("quality", "security")
    assert loaded.audit.reviewers == ("quality", "security")
    assert loaded.audit.security_provider is None
    assert loaded.audit.security_opt_out_reason is None
    assert loaded.audit.block_severity == "high"
    assert loaded.audit.review_timeout_ms == 900_000
    assert loaded.audit.security_enabled is True


@pytest.mark.parametrize(
    "audit_yaml",
    [
        "reviewers: [quality]",
        "reviewers: [quality]\n  security_opt_out_reason: ''",
        "reviewers: [quality]\n  security_opt_out_reason: '   '",
    ],
)
def test_audit_config_rejects_security_opt_out_without_a_reason(
    tmp_path: Path,
    audit_yaml: str,
) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        f"audit:\n  {audit_yaml}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="security_opt_out_reason"):
        load_dispatch_config(repo_root=tmp_path)


def test_audit_config_rejects_opt_out_reason_while_security_is_on(
    tmp_path: Path,
) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        "audit:\n  reviewers: [quality, security]\n"
        "  security_opt_out_reason: not needed\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="security_opt_out_reason"):
        load_dispatch_config(repo_root=tmp_path)


def test_audit_config_loads_security_opt_out_with_a_reason(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        f'audit:\n  reviewers: [quality]\n  security_opt_out_reason: "  {OPT_OUT_REASON} "\n',
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.audit.reviewers == ("quality",)
    assert loaded.audit.security_enabled is False
    assert loaded.audit.security_opt_out_reason == OPT_OUT_REASON


def test_audit_config_loads_from_yaml(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        """audit:
  reviewers: [quality, security]
  security_provider: codex
  block_severity: medium
  review_timeout_ms: 1234
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.audit.reviewers == ("quality", "security")
    assert loaded.audit.security_provider == "codex"
    assert loaded.audit.block_severity == "medium"
    assert loaded.audit.review_timeout_ms == 1234
    assert loaded.audit.security_enabled is True


@pytest.mark.parametrize(
    ("audit_yaml", "message"),
    [
        ("reviewers: [security]", "must include `quality`"),
        ("reviewers: [quality, style]", "reviewers"),
        ("reviewers: [quality, quality]", "must not repeat"),
        ("reviewers: []", "at least one reviewer"),
        ("security_provider: openai", "security_provider"),
        ("block_severity: urgent", "block_severity"),
        ("review_timeout_ms: 0", "at least 1"),
        ("unexpected: true", "unexpected"),
    ],
)
def test_audit_config_rejects_invalid_values(
    tmp_path: Path,
    audit_yaml: str,
    message: str,
) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        f"audit:\n  {audit_yaml}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_dispatch_config(repo_root=tmp_path)


@pytest.mark.parametrize(
    ("backend_provider", "expected"),
    [("codex", "claude"), ("claude", "codex"), ("antigravity", "claude")],
)
def test_security_provider_defaults_to_a_different_vendor(
    tmp_path: Path,
    backend_provider: str,
    expected: str,
) -> None:
    config = DispatchConfig(
        repo_root=tmp_path,
        agents={
            **{
                role: agent.model_dump(exclude_none=True)
                for role, agent in config_module._default_agent_configs().items()
            },
            "backend": {"provider": backend_provider},
        },
        audit={"reviewers": ["quality", "security"]},
    )

    provider, origin = audit_review.resolve_security_provider(
        config,
        implementer_role="backend",
    )

    assert (provider, origin) == (expected, "auto")


def test_configured_security_provider_wins(tmp_path: Path) -> None:
    config = _fan_out_config(tmp_path, security_provider="codex")

    assert audit_review.resolve_security_provider(
        config, implementer_role="backend"
    ) == (
        "codex",
        "configured",
    )


def _installed_clis(monkeypatch: pytest.MonkeyPatch, *providers: str) -> None:
    """Use the real reviewer PATH lookup with only ``providers`` on PATH."""
    monkeypatch.setattr(
        audit_review, "_reviewer_cli_available", REAL_REVIEWER_CLI_AVAILABLE
    )

    def fake_which(name: str, *_args: Any, **_kwargs: Any) -> str | None:
        stem = Path(name).name.split(".", 1)[0]
        return f"/usr/bin/{name}" if stem in providers else None

    monkeypatch.setattr(audit_review.shutil, "which", fake_which)


def _auto_config(tmp_path: Path, **overrides: dict[str, Any]) -> DispatchConfig:
    return _fan_out_config(
        tmp_path,
        security_provider=None,
        agents=_default_agents(**overrides),
    )


def test_auto_security_provider_skips_a_vendor_whose_cli_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # claude is first in the preference order but not installed; codex is
    # the implementer's vendor, so antigravity (agy) is the independent
    # reviewer.
    _installed_clis(monkeypatch, "codex", "agy")
    config = _auto_config(tmp_path)
    log = _Recorder()

    provider = audit_review.resolve_security_provider(
        config, implementer_role="backend", log=log
    )

    assert provider == ("antigravity", "auto")
    assert not [message for level, message in log.messages if level == "WARN"]


def test_auto_security_provider_order_is_claude_codex_antigravity() -> None:
    assert config_module.SECURITY_REVIEW_PROVIDER_PREFERENCE == (
        "claude",
        "codex",
        "antigravity",
    )


def test_auto_security_provider_never_picks_gemini(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gemini left the automatic order: a gemini-only PATH has no reviewer.
    _installed_clis(monkeypatch, "gemini")
    config = _auto_config(tmp_path)

    with pytest.raises(audit_review.SecurityReviewerUnavailable) as excinfo:
        audit_review.resolve_security_provider(config, implementer_role="backend")

    assert "gemini" not in str(excinfo.value).split("(looked for", 1)[1]


def test_auto_security_provider_skips_gemini_for_codex_and_agy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed_clis(monkeypatch, "codex", "gemini", "agy")
    config = _auto_config(tmp_path)

    assert audit_review.resolve_security_provider(
        config, implementer_role="backend"
    ) == ("antigravity", "auto")


def test_auto_security_provider_falls_back_to_the_implementer_vendor_with_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed_clis(monkeypatch, "codex")
    config = _auto_config(tmp_path)
    log = _Recorder()

    provider = audit_review.resolve_security_provider(
        config, implementer_role="backend", log=log
    )

    assert provider == ("codex", "auto, same vendor")
    warnings = [message for level, message in log.messages if level == "WARN"]
    assert len(warnings) == 1
    assert "not independent" in warnings[0]


def test_auto_security_provider_raises_when_no_reviewer_cli_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed_clis(monkeypatch)
    config = _auto_config(tmp_path)

    with pytest.raises(audit_review.SecurityReviewerUnavailable) as excinfo:
        audit_review.resolve_security_provider(config, implementer_role="backend")

    assert str(excinfo.value).startswith("security_reviewer_failed:")
    assert "security_opt_out_reason" in str(excinfo.value)


def test_startup_fails_before_any_agent_when_no_reviewer_cli_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _init_repo(tmp_path)
    _write_handoff(repo, _backend_handoff("a" * 40))
    _installed_clis(monkeypatch)
    config = DispatchConfig(repo_root=repo, poll_interval_seconds=0)

    def fail_launch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no agent may launch when the reviewer cannot")

    for name in (
        "invoke_support_role",
        "invoke_backend_role",
        "invoke_frontend_role",
        "invoke_planner_role",
    ):
        monkeypatch.setattr(orchestrator, name, fail_launch)
    log = _Recorder()

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=log)

    assert exit_code == 1
    errors = [message for level, message in log.messages if level == "ERROR"]
    assert len(errors) == 1
    assert errors[0].startswith("security_reviewer_failed:")
    assert "--- Cycle" not in log.text()


def test_startup_check_covers_each_implementer_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # backend (codex) gets claude; frontend (claude) gets codex. Only claude
    # is installed, so the frontend's reviewer falls back to claude with a
    # warning and the check passes.
    _installed_clis(monkeypatch, "claude")
    config = _auto_config(tmp_path, frontend={"provider": "claude"})
    log = _Recorder()

    assert audit_review.security_reviewer_startup_error(config, log=log) is None
    warnings = [message for level, message in log.messages if level == "WARN"]
    assert len(warnings) == 1
    assert "frontend implementer" in warnings[0]
    assert "not independent" in warnings[0]


_PROVIDER_CLI_STEMS = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "agy",
}


@pytest.mark.parametrize("security_provider", ["claude", "codex", "antigravity"])
def test_startup_check_fails_when_the_configured_reviewer_cli_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    security_provider: str,
) -> None:
    others = [
        stem
        for name, stem in _PROVIDER_CLI_STEMS.items()
        if name != security_provider
    ]
    _installed_clis(monkeypatch, *others)
    config = _fan_out_config(tmp_path, security_provider=security_provider)

    error = audit_review.security_reviewer_startup_error(config)

    assert error is not None
    assert error.startswith("security_reviewer_failed:")
    assert f"the {security_provider} security reviewer CLI" in error


def test_fan_out_fails_closed_when_no_reviewer_cli_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "print('hi')\n", "feature")
    handoff = _backend_handoff(head)
    _write_handoff(repo, handoff)
    _installed_clis(monkeypatch)
    config = _fan_out_config(repo, security_provider=None)

    result, _log = _run_fan_out(
        config, handoff, quality_stdout=json.dumps(_quality_payload())
    )

    assert result.exit_code == 1
    assert result.stderr.startswith("security_reviewer_failed:")
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_text(
        encoding="utf-8"
    ) == handoff


# --------------------------------------------------------------------------
# default runs both reviewers; the opt-out path stays byte-for-byte unchanged
# --------------------------------------------------------------------------


def test_default_config_dispatches_quality_and_security_fan_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DispatchConfig(repo_root=tmp_path)
    fan_out_calls: list[DispatchConfig] = []

    def fake_fan_out(config_arg: DispatchConfig, **_kwargs: Any) -> DispatchResult:
        fan_out_calls.append(config_arg)
        return DispatchResult(
            stdout="audit fan-out passed", stderr="", exit_code=0, elapsed_seconds=0.0
        )

    def fail_support(*_args: Any, **_kwargs: Any) -> SubagentResult:
        raise AssertionError("the default config must not run the single auditor")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fail_support)
    monkeypatch.setattr(orchestrator, "run_audit_fan_out", fake_fan_out)
    log = _Recorder()

    result, label = orchestrator._dispatch_route("auditor", config, log)

    assert config.audit.reviewers == ("quality", "security")
    assert label == "auditor (audit)"
    assert result.stdout == "audit fan-out passed"
    assert fan_out_calls == [config]
    assert (
        "DISPATCH",
        "Dispatching audit fan-out (quality + security reviewers).",
    ) in log.messages


def test_opted_out_config_dispatches_original_single_auditor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DispatchConfig(repo_root=tmp_path, audit=QUALITY_ONLY_AUDIT)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_support(*args: Any, **kwargs: Any) -> SubagentResult:
        calls.append((args, kwargs))
        return _subagent("audit report")

    def fail_fan_out(*_args: Any, **_kwargs: Any) -> DispatchResult:
        raise AssertionError("fan-out must not run when security is opted out")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "run_audit_fan_out", fail_fan_out)
    log = _Recorder()

    result, label = orchestrator._dispatch_route("auditor", config, log)

    assert orchestrator.AUDIT_PROMPT_TEMPLATE == ORIGINAL_AUDIT_PROMPT_TEMPLATE
    assert label == "auditor (audit)"
    assert result.stdout == "audit report"
    assert calls == [
        (
            ("auditor", ORIGINAL_AUDIT_PROMPT),
            {
                "role": "auditor",
                "handoff_path": config.handoff_full_path,
                "agent_config": config.agents["auditor"],
                "log": log,
                "repo_root": config.repo_root,
            },
        )
    ]
    assert log.messages == [("DISPATCH", "Dispatching auditor for audit.")]


def test_opted_out_run_loop_skips_fan_out_bookkeeping_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "print('hi')\n", "feature")
    _write_handoff(repo, _backend_handoff(head))
    config = DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        pause_recovery="off",
        audit=QUALITY_ONLY_AUDIT,
    )
    prompts: list[str] = []

    def fake_support(name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        prompts.append(prompt)
        return _subagent("")

    def fail_git_head(_repo_root: Path) -> str:
        raise AssertionError("opted-out config must not capture the implementer base")

    def fail_startup_check(*_args: Any, **_kwargs: Any) -> str | None:
        raise AssertionError("opted-out config must not check a security reviewer")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "_git_head", fail_git_head)
    monkeypatch.setattr(audit_review, "_reviewer_cli_available", fail_startup_check)
    log = _Recorder()

    orchestrator.run_loop(config, max_cycles=1, log=log)

    assert prompts[0] == ORIGINAL_AUDIT_PROMPT
    assert "quality + security" not in log.text()
    warnings = [message for level, message in log.messages if level == "WARN"]
    assert any(
        "security reviewer is OFF" in message and OPT_OUT_REASON in message
        for message in warnings
    ), warnings
    assert not (repo / "logs" / "dispatch" / "audit").exists()


def test_opted_out_dry_run_start_logs_the_reason(tmp_path: Path) -> None:
    config = DispatchConfig(repo_root=tmp_path, dry_run=True, audit=QUALITY_ONLY_AUDIT)
    log = _Recorder()

    orchestrator._log_startup_banner(config, log)

    assert any(
        level == "WARN" and OPT_OUT_REASON in message
        for level, message in log.messages
    )


# --------------------------------------------------------------------------
# review scope
# --------------------------------------------------------------------------


def test_review_scope_uses_pre_dispatch_base(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    _commit_file(repo, "src/a.py", "a = 1\n", "one")
    head = _commit_file(repo, "src/b.py", "b = 2\n", "two")
    log_dir = tmp_path / "audit-log"
    log_dir.mkdir()

    scope, error = audit_review.compute_review_scope(
        repo,
        head_sha=head,
        base_sha=base,
        log_dir=log_dir,
    )

    assert error is None
    assert scope is not None
    assert (scope.base_sha, scope.head_sha) == (base, head)
    assert scope.files == ("src/a.py", "src/b.py")
    assert "+a = 1" in scope.diff_path.read_text(encoding="utf-8")


def test_review_scope_falls_back_to_scope_sha_parent(tmp_path: Path) -> None:
    repo, _base = _init_repo(tmp_path)
    parent = _commit_file(repo, "src/a.py", "a = 1\n", "one")
    head = _commit_file(repo, "src/b.py", "b = 2\n", "two")
    log_dir = tmp_path / "audit-log"
    log_dir.mkdir()

    scope, error = audit_review.compute_review_scope(
        repo,
        head_sha=head[:12],
        base_sha=None,
        log_dir=log_dir,
    )

    assert error is None
    assert scope is not None
    assert (scope.base_sha, scope.head_sha) == (parent, head)
    assert scope.files == ("src/b.py",)


def test_review_scope_rejects_unknown_scope_sha(tmp_path: Path) -> None:
    repo, _base = _init_repo(tmp_path)

    scope, error = audit_review.compute_review_scope(
        repo,
        head_sha="deadbeefdeadbeef",
        base_sha=None,
        log_dir=tmp_path,
    )

    assert scope is None
    assert error is not None and error.startswith("review_scope_unavailable:")


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------


def _scope(tmp_path: Path, base: str = "a" * 40, head: str = "b" * 40) -> ReviewScope:
    return ReviewScope(
        base_sha=base,
        head_sha=head,
        files=("src/app.py",),
        diff_path=tmp_path / "scope.diff",
    )


def test_claude_security_command_is_read_only_structured_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    config = _fan_out_config(tmp_path, security_provider="claude")
    scope = _scope(tmp_path)

    plan = audit_review.build_security_reviewer_plan(
        config,
        provider="claude",
        scope=scope,
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )

    command = list(plan.command)
    assert plan.kind == "claude"
    assert command[0] == config_module.CLAUDE_BINARY
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "--dangerously-skip-permissions" not in command
    allowed = command[
        command.index("--allowedTools") + 1 : command.index("--disallowedTools")
    ]
    assert all(not tool.startswith(("Edit", "Write")) for tool in allowed)
    assert {"Edit", "Write"} <= set(command)
    assert command[command.index("--output-format") + 1] == "json"
    assert json.loads(command[command.index("--json-schema") + 1]) == (
        audit_review.SECURITY_VERDICT_JSON_SCHEMA
    )
    # Claude Code rejects a draft 2020-12 "$schema" reference in --json-schema.
    assert "$schema" not in json.loads(command[command.index("--json-schema") + 1])
    assert command[-2] == "-p"
    prompt = command[-1]
    assert "security review" in prompt
    assert scope.range_text in prompt
    assert "Do not scan the whole repository" in prompt
    assert "do not write docs/handoff/HANDOFF.md" in prompt
    assert plan.env is not None and "ANTHROPIC_API_KEY" not in plan.env


def _claude_plan_model(config: DispatchConfig, tmp_path: Path) -> str:
    plan = audit_review.build_security_reviewer_plan(
        config,
        provider="claude",
        scope=_scope(tmp_path),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )
    command = list(plan.command)
    return command[command.index("--model") + 1]


def test_claude_security_command_uses_audit_security_model_when_set(
    tmp_path: Path,
) -> None:
    agents = _default_agents(auditor={"provider": "claude", "model": "role-model"})
    unset = _fan_out_config(tmp_path, agents=agents)
    configured = DispatchConfig(
        repo_root=tmp_path,
        agents=agents,
        audit={
            "reviewers": ["quality", "security"],
            "security_provider": "claude",
            "security_model": "security-model",
        },
    )

    assert _claude_plan_model(unset, tmp_path) == "role-model"
    assert _claude_plan_model(configured, tmp_path) == "security-model"
    unset_plan = audit_review.build_security_reviewer_plan(
        unset,
        provider="claude",
        scope=_scope(tmp_path),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )
    configured_plan = audit_review.build_security_reviewer_plan(
        configured,
        provider="claude",
        scope=_scope(tmp_path),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )
    # Only the --model value differs between the two commands.
    unset_command = list(unset_plan.command)
    configured_command = list(configured_plan.command)
    model_index = unset_command.index("--model") + 1
    unset_command[model_index] = configured_command[model_index]
    assert unset_command == configured_command


def _security_model_config(
    tmp_path: Path,
    *,
    security_provider: str | None = None,
    security_model: str | None = None,
    agents: dict[str, Any] | None = None,
) -> DispatchConfig:
    audit: dict[str, Any] = {"reviewers": ["quality", "security"]}
    if security_provider is not None:
        audit["security_provider"] = security_provider
    if security_model is not None:
        audit["security_model"] = security_model
    return DispatchConfig(
        repo_root=tmp_path,
        agents=agents if agents is not None else _default_agents(),
        audit=audit,
    )


def test_describe_security_reviewer_model_names_the_configured_model(
    tmp_path: Path,
) -> None:
    config = _security_model_config(
        tmp_path, security_provider="claude", security_model="security-model"
    )

    assert audit_review.describe_security_reviewer_model(config) == (
        "security-model (audit.security_model)"
    )


def test_describe_security_reviewer_model_names_the_borrowed_role(
    tmp_path: Path,
) -> None:
    config = _security_model_config(
        tmp_path,
        security_provider="claude",
        agents=_default_agents(
            auditor={"provider": "codex"},
            validator={"provider": "claude", "model": "validator-model"},
        ),
    )

    assert audit_review.describe_security_reviewer_model(config) == (
        "validator-model (borrowed from the validator role)"
    )


def test_describe_security_reviewer_model_falls_back_to_the_default(
    tmp_path: Path,
) -> None:
    agents = {
        role: {"provider": "codex"}
        for role in config_module._default_agent_configs()
    }
    config = _security_model_config(
        tmp_path, security_provider="claude", agents=agents
    )

    assert audit_review.describe_security_reviewer_model(config) == (
        f"{config_module.CLAUDE_MODEL} (default)"
    )
    assert _claude_plan_model(config, tmp_path) == config_module.CLAUDE_MODEL


@pytest.mark.parametrize("provider", ["codex"])
def test_describe_security_reviewer_model_for_non_claude_provider(
    tmp_path: Path,
    provider: str,
) -> None:
    config = _security_model_config(tmp_path, security_provider=provider)

    text = audit_review.describe_security_reviewer_model(config)

    assert text.startswith(f"{provider} CLI default model")
    assert text.isascii()


def test_describe_security_reviewer_model_for_antigravity_default(
    tmp_path: Path,
) -> None:
    config = _security_model_config(tmp_path, security_provider="antigravity")

    text = audit_review.describe_security_reviewer_model(config)

    assert text.startswith("antigravity CLI default model")
    assert text.isascii()


def test_describe_security_reviewer_model_for_antigravity_borrows_role_model(
    tmp_path: Path,
) -> None:
    config = _security_model_config(
        tmp_path,
        security_provider="antigravity",
        agents=_default_agents(
            planner={"provider": "antigravity", "model": "agy-model"}
        ),
    )

    assert audit_review.describe_security_reviewer_model(config) == (
        "agy-model (borrowed from the planner role)"
    )


def test_describe_security_reviewer_model_in_auto_mode(tmp_path: Path) -> None:
    config = _security_model_config(tmp_path, security_model="security-model")

    text = audit_review.describe_security_reviewer_model(config)

    assert text.startswith("security-model (audit.security_model)")
    assert "applies when the claude reviewer is chosen" in text
    assert text.isascii()


def test_describe_security_reviewer_model_does_no_path_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_which(*_args: Any, **_kwargs: Any) -> str | None:
        raise AssertionError("describe_security_reviewer_model looked up PATH")

    def fail_available(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("describe_security_reviewer_model looked up PATH")

    monkeypatch.setattr(audit_review.shutil, "which", fail_which)
    monkeypatch.setattr(audit_review, "_reviewer_cli_available", fail_available)

    audit_review.describe_security_reviewer_model(_security_model_config(tmp_path))


def test_codex_prefers_codex_security_cli_when_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "x = 1\n", "feature")
    config = _fan_out_config(repo, security_provider="codex")
    monkeypatch.setattr(
        audit_review.shutil,
        "which",
        lambda name: (
            "/opt/bin/codex-security"
            if name == audit_review.CODEX_SECURITY_BINARY
            else None
        ),
    )

    plan = audit_review.build_security_reviewer_plan(
        config,
        provider="codex",
        scope=_scope(tmp_path, base=base, head=head),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )

    assert plan.kind == "codex-security"
    assert plan.command == (
        "/opt/bin/codex-security",
        "scan",
        ".",
        "--diff",
        base,
        "--json",
    )


def test_codex_falls_back_to_exec_with_output_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _fan_out_config(tmp_path, security_provider="codex")
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)
    scope = _scope(tmp_path)

    plan = audit_review.build_security_reviewer_plan(
        config,
        provider="codex",
        scope=scope,
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )

    command = list(plan.command)
    assert plan.kind == "codex-exec"
    assert command[:5] == [
        config_module.CODEX_BINARY,
        "exec",
        "--sandbox",
        "read-only",
        "--json",
    ]
    assert "--yolo" not in command
    schema_path = Path(command[command.index("--output-schema") + 1])
    assert json.loads(schema_path.read_text(encoding="utf-8")) == (
        audit_review.SECURITY_VERDICT_JSON_SCHEMA
    )
    assert (
        Path(command[command.index("--output-last-message") + 1])
        == plan.last_message_path
    )
    assert scope.range_text in command[-1]


def test_codex_security_cli_is_skipped_when_scope_is_not_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _init_repo(tmp_path)
    scoped = _commit_file(repo, "src/app.py", "x = 1\n", "feature")
    _commit_file(repo, "src/other.py", "y = 2\n", "later")
    config = _fan_out_config(repo, security_provider="codex")
    monkeypatch.setattr(
        audit_review.shutil, "which", lambda _name: "/opt/bin/codex-security"
    )

    plan = audit_review.build_security_reviewer_plan(
        config,
        provider="codex",
        scope=_scope(tmp_path, base=base, head=scoped),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )

    assert plan.kind == "codex-exec"


# --------------------------------------------------------------------------
# structured output parsing
# --------------------------------------------------------------------------


def test_parse_claude_structured_output() -> None:
    verdict = audit_review.parse_claude_structured_verdict(
        _claude_envelope(
            _security_payload(findings=[_finding("High", "SQL injection")])
        )
    )

    assert verdict.verdict == "pass"
    assert verdict.findings[0].severity == "high"
    assert verdict.findings[0].location == "src/app.py:3"


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "I reviewed the code and it is fine.",
        json.dumps({"type": "result", "is_error": True, "result": "API error"}),
        _claude_envelope({"summary": "missing verdict", "findings": []}),
        _claude_envelope({"verdict": "maybe", "findings": []}),
        _claude_envelope({"verdict": "pass", "findings": [_finding("urgent")]}),
        _claude_envelope({"verdict": "pass", "findings": [{"severity": "high"}]}),
        _claude_envelope({"verdict": "pass", "findings": "none"}),
    ],
)
def test_parse_claude_structured_output_rejects_malformed(stdout: str) -> None:
    with pytest.raises(ValueError):
        audit_review.parse_claude_structured_verdict(stdout)


def test_parse_security_verdict_accepts_fenced_json_in_prose() -> None:
    text = (
        "Analysis complete.\n```json\n"
        + json.dumps(_security_payload(verdict="fail", findings=[_finding("critical")]))
        + "\n```\n"
    )

    verdict = audit_review.parse_security_verdict(text)

    assert verdict.verdict == "fail"
    assert verdict.findings[0].severity == "critical"


def test_parse_quality_verdict_reads_codex_envelope() -> None:
    envelope = json.dumps({"status": "pass", "summary": json.dumps(_quality_payload())})

    verdict = audit_review.parse_quality_verdict(envelope)

    assert verdict.next_agent == "planner"
    assert verdict.commands_run == ["python -m pytest -q"]


def _with_raw_control_characters(payload: dict[str, Any]) -> str:
    # A Codex quality reviewer prints its verdict object with real newlines
    # and tabs inside string values instead of `\n` and `\t` escapes.
    text = json.dumps(payload).replace("\\n", "\n").replace("\\t", "\t")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    return text


@pytest.mark.parametrize("prefix", ["", "Review complete.\n\n"])
def test_parse_quality_verdict_accepts_raw_newlines_in_strings(prefix: str) -> None:
    report = (
        "## Phase 1 - Spec Compliance\nAll 19 files match the Work Packet.\n\n"
        "## Findings\nNone.\n\n## Next Step\n**planner:**\tClose the story."
    )
    payload = {**_quality_payload(), "report": report}

    verdict = audit_review.parse_quality_verdict(
        prefix + _with_raw_control_characters(payload)
    )

    assert verdict.verdict == "pass"
    assert verdict.next_agent == "planner"
    assert verdict.report == report


def test_parse_security_verdict_accepts_raw_newlines_in_strings() -> None:
    payload = _security_payload(
        findings=[
            {
                "severity": "medium",
                "file": "src/app.py",
                "line": 4,
                "title": "Shell access",
                "description": "First line.\nSecond line.",
                "recommendation": "Remove it.",
            }
        ]
    )

    verdict = audit_review.parse_security_verdict(
        _with_raw_control_characters(payload)
    )

    assert verdict.findings[0].description == "First line.\nSecond line."


@pytest.mark.parametrize(
    "overrides",
    [
        {"next_agent": "auditor"},
        {"next_agent": "finalizer", "close_type": "story"},
        {"reason": ""},
        {"verdict": "approved"},
    ],
)
def test_parse_quality_verdict_rejects_unroutable_output(
    overrides: dict[str, Any],
) -> None:
    payload = {**_quality_payload(), **overrides}

    with pytest.raises(ValueError):
        audit_review.parse_quality_verdict(json.dumps(payload))


def test_parse_codex_security_output_fails_closed_on_unknown_severity() -> None:
    stdout = json.dumps(
        {
            "manifest": {"scan_id": "s1"},
            "findings": [
                {
                    "severity": "moderate",
                    "path": "a.py",
                    "line": 4,
                    "title": "Weak hash",
                },
                {
                    "severity": "spicy",
                    "location": {"file": "b.py", "line": 9},
                    "title": "Odd",
                },
            ],
        }
    )

    verdict = audit_review.parse_codex_security_output(stdout)

    assert [finding.severity for finding in verdict.findings] == ["medium", "high"]
    assert verdict.findings[1].location == "b.py:9"
    assert "unrecognized severity" in verdict.findings[1].description


# --------------------------------------------------------------------------
# merge rule
# --------------------------------------------------------------------------


def _run_with(name: str, verdict: ReviewVerdict) -> ReviewerRun:
    return ReviewerRun(
        name=name,
        provider="claude",
        command=(),
        exit_code=0,
        elapsed_seconds=0.0,
        stdout="",
        stderr="",
        verdict=verdict,
    )


@pytest.mark.parametrize(
    ("block_severity", "finding_severity", "blocked"),
    [
        (
            threshold,
            severity,
            audit_review.SEVERITY_RANK[severity]
            >= audit_review.SEVERITY_RANK[threshold],
        )
        for threshold in ("critical", "high", "medium", "low")
        for severity in ("critical", "high", "medium", "low", "info")
    ],
)
def test_merge_blocks_at_or_above_threshold(
    block_severity: str,
    finding_severity: str,
    blocked: bool,
) -> None:
    runs = {
        "quality": _run_with("quality", ReviewVerdict(verdict="pass")),
        "security": _run_with(
            "security",
            ReviewVerdict(
                verdict="pass",
                findings=[ReviewFinding(severity=finding_severity, title="Issue")],
            ),
        ),
    }

    decision = audit_review.merge_review_verdicts(runs, block_severity=block_severity)

    assert decision.blocked is blocked
    assert len(decision.blocking) == (1 if blocked else 0)
    assert len(decision.notes) == (0 if blocked else 1)


@pytest.mark.parametrize("failing", ["quality", "security"])
def test_merge_blocks_when_either_reviewer_fails(failing: str) -> None:
    runs = {
        name: _run_with(
            name, ReviewVerdict(verdict="fail" if name == failing else "pass")
        )
        for name in ("quality", "security")
    }

    decision = audit_review.merge_review_verdicts(runs, block_severity="critical")

    assert decision.blocked is True
    assert decision.reasons == (f"{failing} reviewer returned fail",)


# --------------------------------------------------------------------------
# fan-out execution
# --------------------------------------------------------------------------


def _fan_out_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo, base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "query = 'SELECT ' + user\n", "feature")
    handoff = _backend_handoff(head)
    _write_handoff(repo, handoff)
    return repo, base, head, handoff


def test_fan_out_runs_reviewers_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    barrier = threading.Barrier(2, timeout=10)
    threads: set[str] = set()

    def fake_security(_name: str, command: list[str], **kwargs: Any) -> _ProcessResult:
        threads.add(threading.current_thread().name)
        assert kwargs["cwd"] == repo.resolve()
        barrier.wait()
        return _process(_claude_envelope(_security_payload()))

    def quality_side_effect(_prompt: str, _agent_config) -> None:
        threads.add(threading.current_thread().name)
        barrier.wait()

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_security)

    result, log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_side_effect=quality_side_effect,
    )

    assert result.exit_code == 0, result.stderr
    assert len(threads) == 2
    assert "Quality reviewer (claude) exited with code 0" in log.text()
    assert "Security reviewer (claude) exited with code 0" in log.text()


def test_fan_out_blocks_and_routes_back_with_security_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, head, handoff = _fan_out_repo(tmp_path)
    prompts: list[str] = []
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(
                _security_payload(findings=[_finding("high", "SQL injection")])
            )
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_side_effect=lambda prompt, _config: prompts.append(prompt),
    )

    assert result.exit_code == 0, result.stderr
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    frontmatter = parse_handoff_frontmatter_text(written)
    assert frontmatter is not None
    assert frontmatter.next_agent == "backend"
    assert frontmatter.status == "verified_fail"
    assert frontmatter.producer == "auditor"
    assert frontmatter.scope_sha == head
    assert frontmatter.story_id == STORY_ID
    assert frontmatter.reason and "blocked" in frontmatter.reason
    assert '\nreason: "' in written
    assert "## Security Findings" in written
    assert "[high] src/app.py:3 - SQL injection" in written
    assert "## Verification Evidence" in written
    assert f"- **Commit SHA verified:** {head}" in written
    assert "do NOT edit docs/handoff/HANDOFF.md" in prompts[0]
    assert f"{base}..{head}" in prompts[0]


def test_fan_out_passes_and_attaches_sub_threshold_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(
                _security_payload(findings=[_finding("low", "Verbose error")])
            )
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(
            _quality_payload(next_agent="planner", close_type="story")
        ),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    frontmatter = parse_handoff_frontmatter_text(written)
    assert frontmatter is not None
    assert frontmatter.next_agent == "planner"
    assert frontmatter.status == "verified_pass"
    assert frontmatter.close_type == "story"
    assert frontmatter.scope_sha == head
    assert "## Security Notes" in written
    assert "[low] src/app.py:3 - Verbose error" in written
    assert "## Security Findings" not in written


@pytest.mark.parametrize(
    ("requested", "close_type", "expected"),
    [
        ("backend", "story", "planner"),
        ("frontend", "story", "planner"),
        ("validator", "story", "planner"),
        ("user", None, "planner"),
        ("finalizer", "epic", "finalizer"),
        ("planner", "epic", "finalizer"),
    ],
)
def test_passing_fan_out_routes_to_the_verified_pass_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    close_type: str | None,
    expected: str,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(_security_payload())
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(
            _quality_payload(next_agent=requested, close_type=close_type)
        ),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    frontmatter = parse_handoff_frontmatter_text(written)
    assert frontmatter is not None
    assert frontmatter.next_agent == expected
    assert frontmatter.status == "verified_pass"
    decision = route(written)
    assert decision.route == expected
    assert decision.confidence == "HIGH"


def test_fan_out_records_raw_outputs_and_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(_security_payload()), stderr="security stderr"
        ),
    )

    _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    cycle_dirs = list((repo / "logs" / "dispatch" / "audit").iterdir())
    assert len(cycle_dirs) == 1
    cycle_dir = cycle_dirs[0]
    assert cycle_dir.name.endswith("-cycle2")
    assert json.loads((cycle_dir / "quality.stdout.txt").read_text(encoding="utf-8"))
    assert (cycle_dir / "security.stderr.txt").read_text(
        encoding="utf-8"
    ) == "security stderr"
    summary = json.loads((cycle_dir / "review.json").read_text(encoding="utf-8"))
    assert summary["scope"] == {
        "base_sha": base,
        "head_sha": head,
        "files": ["src/app.py"],
    }
    for name in ("quality", "security"):
        assert summary["reviewers"][name]["exit_code"] == 0
        assert "elapsed_seconds" in summary["reviewers"][name]


CLAUDE_SECURITY_USAGE_ENVELOPE_FIELDS: dict[str, Any] = {
    "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    },
    "total_cost_usd": 0.25,
    "num_turns": 3,
    "duration_ms": 1200,
}


def _claude_envelope_with_usage(payload: dict[str, Any]) -> str:
    envelope = json.loads(_claude_envelope(payload))
    envelope.update(CLAUDE_SECURITY_USAGE_ENVELOPE_FIELDS)
    return json.dumps(envelope)


def _usage_json_text(repo: Path) -> str:
    cycle_dirs = list((repo / "logs" / "dispatch" / "audit").iterdir())
    assert len(cycle_dirs) == 1
    return (cycle_dirs[0] / "usage.json").read_text(encoding="utf-8")


def _expected_usage_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2) + "\n"


def test_fan_out_writes_usage_json_with_reviewer_usage_and_total_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    config = _fan_out_config(repo)
    security_model = _claude_plan_model(config, tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope_with_usage(_security_payload())
        ),
    )

    result, log = _run_fan_out(
        config,
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_usage={"model": "m", "input_tokens": 10, "cost_usd": 0.5},
    )

    assert result.exit_code == 0, result.stderr
    assert _usage_json_text(repo) == _expected_usage_text(
        {
            "reviewers": {
                "quality": {
                    "provider": "claude",
                    "usage": {"model": "m", "input_tokens": 10, "cost_usd": 0.5},
                },
                "security": {
                    "provider": "claude",
                    "usage": {
                        "model": security_model,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 5,
                        "cache_creation_input_tokens": 0,
                        "cost_usd": 0.25,
                        "num_turns": 3,
                        "duration_ms": 1200,
                    },
                },
            },
            "total_cost_usd": 0.75,
        }
    )
    debug_lines = [
        message
        for level, message in log.messages
        if level == "DEBUG" and message.startswith("usage:")
    ]
    assert debug_lines == [
        "usage: Security reviewer (claude) provider=claude "
        f"model={security_model} input_tokens=100 output_tokens=20 "
        "cache_read_input_tokens=5 cache_creation_input_tokens=0 "
        "cost_usd=0.25 num_turns=3 duration_ms=1200"
    ]


def test_fan_out_usage_json_records_null_when_no_usage_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(_security_payload())
        ),
    )

    result, log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_usage=None,
    )

    assert result.exit_code == 0, result.stderr
    assert _usage_json_text(repo) == _expected_usage_text(
        {
            "reviewers": {
                "quality": {"provider": "claude", "usage": None},
                "security": {"provider": "claude", "usage": None},
            },
            "total_cost_usd": None,
        }
    )
    assert not any(message.startswith("usage:") for _level, message in log.messages)


def test_clean_usage_drops_bad_values_without_mutating_the_source() -> None:
    source: dict[str, object] = {
        "model": None,
        "input_tokens": True,
        "output_tokens": -1,
        "total_tokens": "12",
        "unknown_key": 5,
        "cached_input_tokens": 4,
        "cost_usd": 0.0,
    }
    snapshot = dict(source)

    cleaned = audit_review._clean_usage(source)

    assert cleaned == {"cached_input_tokens": 4, "cost_usd": 0.0}
    assert source == snapshot
    assert audit_review._clean_usage({"model": "", "input_tokens": False}) is None
    assert audit_review._clean_usage({"model": None}) is None
    assert audit_review._clean_usage(None) is None
    assert audit_review._clean_usage({"model": "m"}) == {"model": "m"}


def test_usage_json_drops_model_none_and_keeps_runs_unmutated(
    tmp_path: Path,
) -> None:
    usage: dict[str, object] = {"model": None, "input_tokens": 7, "flag": True}
    run = ReviewerRun(
        name="security",
        provider="claude",
        command=(),
        exit_code=0,
        elapsed_seconds=0.0,
        stdout="",
        stderr="",
        usage=usage,
    )

    audit_review._write_usage_log(tmp_path, {"security": run})

    assert (tmp_path / "usage.json").read_text(encoding="utf-8") == (
        _expected_usage_text(
            {
                "reviewers": {
                    "security": {"provider": "claude", "usage": {"input_tokens": 7}}
                },
                "total_cost_usd": None,
            }
        )
    )
    assert usage == {"model": None, "input_tokens": 7, "flag": True}


def test_claude_json_usage_is_returned_without_a_log_function() -> None:
    envelope = _claude_envelope_with_usage(_security_payload())

    returned = audit_review._log_claude_json_usage(
        "Security reviewer (claude)", envelope, ["claude", "-p", "x"], None
    )

    assert returned == {
        "model": None,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.25,
        "num_turns": 3,
        "duration_ms": 1200,
    }
    assert (
        audit_review._log_claude_json_usage("s", "not json", ["claude"], None) is None
    )
    assert audit_review._log_claude_json_usage("s", "[1]", ["claude"], None) is None
    assert (
        audit_review._log_claude_json_usage(
            "s", _claude_envelope(_security_payload()), ["claude"], None
        )
        is None
    )


def test_fan_out_usage_json_records_codex_exec_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)

    def fake_codex(
        _name: str, _command: list[str], **_kwargs: Any
    ) -> _CodexCommandResult:
        return _CodexCommandResult(
            result=_process('{"type":"turn.completed"}\n'),
            thread_id="t1",
            final_message=json.dumps(_security_payload()),
            usage={
                "model": "codex-model",
                "input_tokens": 300,
                "cached_input_tokens": 40,
                "output_tokens": 60,
            },
        )

    monkeypatch.setattr(audit_review, "_run_codex_json_command", fake_codex)

    result, _log = _run_fan_out(
        _fan_out_config(repo, security_provider="codex"),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_usage={"cost_usd": 0.1},
    )

    assert result.exit_code == 0, result.stderr
    assert _usage_json_text(repo) == _expected_usage_text(
        {
            "reviewers": {
                "quality": {"provider": "claude", "usage": {"cost_usd": 0.1}},
                "security": {
                    "provider": "codex",
                    "usage": {
                        "model": "codex-model",
                        "input_tokens": 300,
                        "output_tokens": 60,
                        "cached_input_tokens": 40,
                    },
                },
            },
            "total_cost_usd": 0.1,
        }
    )


def test_fan_out_writes_usage_json_when_a_reviewer_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope_with_usage(_security_payload())
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout="",
        base_sha=base,
        quality_exit_code=1,
        quality_usage={"input_tokens": 9, "cost_usd": 0.05},
    )

    assert result.exit_code == 1
    assert "quality_reviewer_failed" in result.stderr
    payload = json.loads(_usage_json_text(repo))
    assert payload["reviewers"]["quality"] == {
        "provider": "claude",
        "usage": {"input_tokens": 9, "cost_usd": 0.05},
    }
    assert payload["reviewers"]["security"]["usage"]["cost_usd"] == 0.25
    assert payload["total_cost_usd"] == pytest.approx(0.3)


def test_fan_out_quotes_yaml_special_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "x = 1\n", "feature")
    prior = _commit_file(repo, "src/app.py", "x = 2\n", "fix")
    handoff = (
        _backend_handoff(prior)
        .replace(f"story_id: {STORY_ID}", "story_id: '001'\nepic_id: 'on'")
        .replace(
            "producer: backend",
            f"prior_sha: '{head[:7]}'\nremaining_stories:\n  - 'no'\n  - '1e3'\n"
            "producer: backend",
        )
    )
    _write_handoff(repo, handoff)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(_security_payload())
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    loaded = yaml.safe_load(written.split("---")[1])
    assert loaded["story_id"] == "001"
    assert loaded["epic_id"] == "on"
    assert loaded["prior_sha"] == head[:7]
    assert loaded["remaining_stories"] == ["no", "1e3"]
    frontmatter = parse_handoff_frontmatter_text(written)
    assert frontmatter is not None
    assert (frontmatter.story_id, frontmatter.epic_id) == ("001", "on")


def test_audit_log_directory_is_unique_within_the_same_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_review.time, "strftime", lambda *_args: "20260101T000000")

    first = audit_review._create_audit_log_directory(tmp_path, 1)
    (first / "review.json").write_text("first run", encoding="utf-8")
    second = audit_review._create_audit_log_directory(tmp_path, 1)
    third = audit_review._create_audit_log_directory(tmp_path, 1)

    assert first.name == "20260101T000000-cycle1"
    assert second.name == "20260101T000000-cycle1-2"
    assert third.name == "20260101T000000-cycle1-3"
    assert (first / "review.json").read_text(encoding="utf-8") == "first run"


WORK_PACKET = (
    "## Work Packet"
    + NL
    + NL
    + "- **Objective:** Add the app entry point."
    + NL
    + "- **Files in scope:** src/app.py"
    + NL
    + "- **Files out of bounds:** none"
    + NL
    + "- **Context:** AGENTS.md"
    + NL
    + "- **Verification command:** python -m pytest -q"
    + NL
    + "- **Expected next route:** auditor"
    + NL
)


@pytest.mark.parametrize("security_findings", [["high"], ["low"]])
def test_blocked_fan_out_carries_work_packet_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    security_findings: list[str],
) -> None:
    repo, base, head, _handoff = _fan_out_repo(tmp_path)
    handoff = _backend_handoff(head) + NL + WORK_PACKET
    _write_handoff(repo, handoff)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(
                _security_payload(findings=[_finding(sev) for sev in security_findings])
            )
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    blocked = security_findings == ["high"]
    assert (WORK_PACKET in written) is blocked
    if blocked:
        validation = validate_handoff_text(
            written,
            "auditor (audit)",
            git_cwd=repo,
            require_work_packet=True,
        )
        assert not [e for e in validation.errors if e.startswith("work_packet")], (
            validation.errors
        )


@pytest.mark.parametrize("writer", ["quality", "security"])
def test_fan_out_rejects_reviewer_that_writes_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"

    def scribble() -> None:
        handoff_path.write_text("---\nnext_agent: finalizer\n---\n", encoding="utf-8")

    def fake_security(
        _name: str, _command: list[str], **_kwargs: Any
    ) -> _ProcessResult:
        if writer == "security":
            scribble()
        return _process(_claude_envelope(_security_payload()))

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_security)

    result, log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_side_effect=(lambda *_args: scribble())
        if writer == "quality"
        else None,
    )

    assert result.exit_code == 1
    assert f"reviewer_wrote_handoff: the {writer} reviewer" in result.stderr
    assert handoff_path.read_text(encoding="utf-8") == handoff
    assert any(
        level == "ERROR" and "reviewer_wrote_handoff:" in msg
        for level, msg in log.messages
    )


def test_fan_out_fails_closed_on_malformed_security_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process("Looks fine to me."),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 1
    assert "security_verdict_malformed:" in result.stderr
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_text(
        encoding="utf-8"
    ) == handoff


def test_fan_out_fails_closed_on_malformed_quality_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(_security_payload())
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout="Audit complete. Approved.",
        base_sha=base,
    )

    assert result.exit_code == 1
    assert "quality_verdict_malformed:" in result.stderr


def test_fan_out_reads_codex_exec_last_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)
    calls: list[dict[str, Any]] = []

    def fake_codex(
        _name: str, command: list[str], **kwargs: Any
    ) -> _CodexCommandResult:
        calls.append({"command": command, **kwargs})
        return _CodexCommandResult(
            result=_process('{"type":"turn.completed"}\n'),
            thread_id="t1",
            final_message=json.dumps(_security_payload()),
        )

    monkeypatch.setattr(audit_review, "_run_codex_json_command", fake_codex)

    result, _log = _run_fan_out(
        _fan_out_config(repo, security_provider="codex"),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    assert calls[0]["cwd"] == repo.resolve()
    assert calls[0]["timeout_ms"] == 900_000
    assert "status: verified_pass" in (
        repo / "docs" / "handoff" / "HANDOFF.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("security_model", ["security-model", None])
def test_fan_out_warns_once_when_security_model_is_not_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    security_model: str | None,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(audit_review.shutil, "which", lambda _name: None)
    codex_commands: list[list[str]] = []

    def fake_codex(
        _name: str, command: list[str], **_kwargs: Any
    ) -> _CodexCommandResult:
        codex_commands.append(command)
        return _CodexCommandResult(
            result=_process('{"type":"turn.completed"}\n'),
            thread_id="t1",
            final_message=json.dumps(_security_payload()),
        )

    monkeypatch.setattr(audit_review, "_run_codex_json_command", fake_codex)
    audit: dict[str, Any] = {"reviewers": ["quality", "security"]}
    if security_model is not None:
        audit["security_model"] = security_model
    # The implementer is claude, so auto mode picks codex as the reviewer.
    config = DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        agents=_default_agents(backend={"provider": "claude"}),
        audit=audit,
    )

    result, log = _run_fan_out(
        config,
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    assert "security reviewer: codex (auto)" in log.text()
    warnings = [
        message
        for level, message in log.messages
        if level == "WARN" and "audit.security_model" in message
    ]
    if security_model is None:
        assert warnings == []
    else:
        assert len(warnings) == 1
        assert "`codex`" in warnings[0]
        assert "was not applied" in warnings[0]
    assert all("--model" not in command for command in codex_commands)


def test_fan_out_does_not_warn_when_claude_uses_security_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    commands: list[list[str]] = []

    def fake_security(_name: str, command: list[str], **_kwargs: Any) -> _ProcessResult:
        commands.append(command)
        return _process(_claude_envelope(_security_payload()))

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_security)
    config = DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        audit={
            "reviewers": ["quality", "security"],
            "security_provider": "claude",
            "security_model": "security-model",
        },
    )

    result, log = _run_fan_out(
        config,
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    assert not [
        message
        for level, message in log.messages
        if level == "WARN" and "audit.security_model" in message
    ]
    assert commands[0][commands[0].index("--model") + 1] == "security-model"


def test_fan_out_reports_reviewer_timeout_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    seen_timeouts: list[int] = []

    def fake_runner(_name: str, _command: list[str], **kwargs: Any) -> _ProcessResult:
        seen_timeouts.append(kwargs["timeout_ms"])
        return _process("", exit_code=1, stderr="Process timed out after 1 seconds.")

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_runner)
    config = DispatchConfig(
        repo_root=repo,
        audit={
            "reviewers": ["quality", "security"],
            "security_provider": "claude",
            "review_timeout_ms": 1000,
        },
    )
    timeouts: list[int | None] = []

    result, _log = _run_fan_out(
        config,
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
        quality_side_effect=lambda _prompt, agent_config: timeouts.append(
            agent_config.timeout_ms
        ),
    )

    assert result.exit_code == 1
    assert "security_reviewer_failed:" in result.stderr
    assert seen_timeouts == [1000]
    assert timeouts == [1000]


# --------------------------------------------------------------------------
# run_loop integration
# --------------------------------------------------------------------------


def _run_backend_then_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    security_payload: dict[str, Any],
) -> tuple[Path, str, str, _Recorder]:
    repo, _base = _init_repo(tmp_path)
    _write_handoff(
        repo,
        """---
next_agent: backend
reason: "Implement the story."
story_id: AUDIT-S1
producer: planner
---

## Task Assignment

**Agent:** backend
""",
    )
    pre_dispatch_head = _git(repo, "rev-parse", "HEAD")
    # The assignment carries no Work Packet; these tests cover the audit
    # fan-out, and the work-claim gate is covered in tests/test_work_claim.py.
    config = _fan_out_config(
        repo,
        enforce_work_packet=False,
        work_packet_opt_out_reason="Tests cover the audit fan-out, not the gate.",
    )

    def fake_backend(path: Path, **_kwargs: Any) -> DispatchResult:
        _commit_file(repo, "src/one.py", "one = 1\n", "first")
        head = _commit_file(repo, "src/two.py", "two = 2\n", "second")
        path.write_text(_backend_handoff(head), encoding="utf-8")
        return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    def fake_support(name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        assert name == "auditor"
        assert "read-only fan-out" in prompt
        return _subagent(json.dumps(_quality_payload()))

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(_claude_envelope(security_payload)),
    )
    log = _Recorder()

    exit_code = orchestrator.run_loop(config, max_cycles=2, log=log)

    assert exit_code == 0, log.text()
    return repo, pre_dispatch_head, _git(repo, "rev-parse", "HEAD"), log


def test_run_loop_fan_out_blocks_and_increments_bounce_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, pre_dispatch_head, head, log = _run_backend_then_audit(
        tmp_path,
        monkeypatch,
        security_payload=_security_payload(findings=[_finding("critical", "RCE")]),
    )

    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    assert "next_agent: backend" in written
    assert "status: verified_fail" in written
    assert ledger.bounce_count(STORY_ID) == 1
    assert "Post-dispatch gate PASSED for auditor (audit)." in log.text()
    assert "Audit reviewers:    quality + security" in log.text()
    startup_lines = [
        message
        for _level, message in log.messages
        if message.startswith("Audit reviewers:")
    ]
    assert len(startup_lines) == 1
    assert (
        f"security model: {config_module.CLAUDE_MODEL} "
        "(borrowed from the auditor role)" in startup_lines[0]
    )
    summary_path = (
        next((repo / "logs" / "dispatch" / "audit").iterdir()) / "review.json"
    )
    scope = json.loads(summary_path.read_text(encoding="utf-8"))["scope"]
    assert scope["base_sha"] == pre_dispatch_head
    assert scope["head_sha"] == head
    assert scope["files"] == ["src/one.py", "src/two.py"]


def test_run_loop_fan_out_pass_resets_bounce_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger.increment_bounce(STORY_ID)

    repo, _pre, _head, log = _run_backend_then_audit(
        tmp_path,
        monkeypatch,
        security_payload=_security_payload(findings=[_finding("medium", "Weak hash")]),
    )

    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    assert "next_agent: planner" in written
    assert "status: verified_pass" in written
    assert "## Security Notes" in written
    assert ledger.bounce_count(STORY_ID) == 0
    assert "Post-dispatch gate PASSED for auditor (audit)." in log.text()


def test_run_loop_fan_out_reviewer_failure_counts_as_dispatch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "x = 1\n", "feature")
    handoff = _backend_handoff(head)
    _write_handoff(repo, handoff)
    config = _fan_out_config(repo).model_copy(update={"max_consecutive_failures": 1})
    monkeypatch.setattr(
        orchestrator,
        "invoke_support_role",
        lambda *_args, **_kwargs: _subagent(json.dumps(_quality_payload())),
    )
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process("not json"),
    )
    log = _Recorder()

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=log)

    assert exit_code == 1
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_text(
        encoding="utf-8"
    ) == handoff
    assert "security_verdict_malformed:" in log.text()


# --------------------------------------------------------------------------
# reviewer start failures
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "stdout", "stderr", "expected"),
    [
        (0, "", "error: unknown option '--bogus'", None),
        (1, "", "[WinError 2] The system cannot find the file specified", "launch"),
        (2, "", "error: unknown option '--bogus'", "argument"),
        (2, "", "error: unexpected argument '--bogus' found", "argument"),
        (1, "", "Not logged in. Please run codex login.", "auth"),
        (1, "", "Process timed out after 5 seconds.", None),
        (2, "partial review output", "unknown option", None),
    ],
)
def test_reviewer_start_failure_classifies_only_start_errors(
    exit_code: int,
    stdout: str,
    stderr: str,
    expected: str | None,
) -> None:
    assert audit_review._reviewer_start_failure(exit_code, stdout, stderr) == expected


def _run_loop_with_reviewers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    quality_result: SubagentResult,
    security_result: _ProcessResult,
) -> tuple[int, Path, bytes, _Recorder, dict[str, int]]:
    repo, _base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "x = 1\n", "feature")
    handoff_bytes = _write_handoff(repo, _backend_handoff(head)).read_bytes()
    config = _fan_out_config(repo).model_copy(update={"max_consecutive_failures": 3})
    calls = {"quality": 0, "security": 0}

    def fake_support(*_args: Any, **_kwargs: Any) -> SubagentResult:
        calls["quality"] += 1
        return quality_result

    def fake_security(_name: str, _command: list[str], **_kwargs: Any) -> _ProcessResult:
        calls["security"] += 1
        return security_result

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_security)
    log = _Recorder()

    exit_code = orchestrator.run_loop(config, max_cycles=3, log=log)
    return exit_code, repo, handoff_bytes, log, calls


def test_run_loop_stops_after_one_attempt_when_security_reviewer_cannot_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, repo, handoff, log, calls = _run_loop_with_reviewers(
        tmp_path,
        monkeypatch,
        quality_result=_subagent(json.dumps(_quality_payload())),
        security_result=_process(
            "", exit_code=2, stderr="error: unknown option '--bogus'"
        ),
    )

    assert exit_code == 1
    assert calls["security"] == 1
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_bytes() == handoff
    text = log.text()
    assert "audit_reviewer_start_failed: the security reviewer" in text
    assert "could not start (argument); exit code 2." in text
    assert "unknown option '--bogus'" in text
    assert "An audit reviewer failed to start; stopping the dispatch loop." in text
    assert "Failure count:" not in text


def test_run_loop_stops_after_one_attempt_when_quality_reviewer_cannot_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, repo, handoff, log, calls = _run_loop_with_reviewers(
        tmp_path,
        monkeypatch,
        quality_result=_subagent(
            "",
            exit_code=1,
            stderr="[WinError 2] The system cannot find the file specified",
        ),
        security_result=_process(_claude_envelope(_security_payload())),
    )

    assert exit_code == 1
    assert calls["quality"] == 1
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_bytes() == handoff
    text = log.text()
    assert "audit_reviewer_start_failed: the quality reviewer" in text
    assert "could not start (launch)" in text
    assert "WinError 2" in text
    assert "Failure count:" not in text


def test_start_failure_message_truncates_stderr_and_marks_empty() -> None:
    long_run = ReviewerRun(
        name="security",
        provider="claude",
        command=(),
        exit_code=2,
        elapsed_seconds=0.0,
        stdout="",
        stderr="x" * 2500 + "TAIL",
        error="security_reviewer_failed: claude exited with code 2.",
        start_failure="argument",
    )
    message = audit_review._start_failure_message(long_run)
    assert message.endswith("stderr: ..." + ("x" * 2500 + "TAIL")[-297:])
    assert len(message.split("stderr: ", 1)[1]) == 300

    empty_run = ReviewerRun(
        name="quality",
        provider="codex",
        command=(),
        exit_code=1,
        elapsed_seconds=0.0,
        stdout="",
        stderr="  ",
        error="quality_reviewer_failed: exit code 1.",
        start_failure="launch",
    )
    assert audit_review._start_failure_message(empty_run) == (
        "audit_reviewer_start_failed: the quality reviewer (codex) could not "
        "start (launch); exit code 1. stderr: (empty)"
    )


def test_start_failure_message_redacts_planted_key() -> None:
    planted = "sk-ant-" + "Rk4Lm" * 9
    run = ReviewerRun(
        name="security",
        provider="claude",
        command=(),
        exit_code=2,
        elapsed_seconds=0.0,
        stdout="",
        stderr=f"error: unknown option\nANTHROPIC_API_KEY={planted}\n",
        error="security_reviewer_failed: claude exited with code 2.",
        start_failure="argument",
    )

    message = audit_review._start_failure_message(run)

    assert planted not in message
    assert "Rk4Lm" not in message
    assert message.endswith(
        "stderr: error: unknown option ANTHROPIC_API_KEY=[REDACTED]"
    )


def test_security_reviewer_failed_error_redacts_planted_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    planted = "sk-proj-" + "Vb8Nc" * 8
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            "",
            exit_code=1,
            stderr=f"upstream said: Authorization: Bearer {planted}\n",
        ),
    )
    config = DispatchConfig(
        repo_root=repo,
        audit={"reviewers": ["quality", "security"], "security_provider": "claude"},
    )

    result, log = _run_fan_out(
        config,
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 1
    assert "security_reviewer_failed:" in result.stderr
    assert "Authorization: Bearer [REDACTED]" in result.stderr
    assert planted not in result.stderr
    assert "Vb8Nc" not in result.stderr
    assert "Vb8Nc" not in log.text()


def test_security_reviewer_timeout_does_not_stop_the_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            "", exit_code=1, stderr="Process timed out after 5 seconds."
        ),
    )

    result, log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 1
    assert result.stop_loop is False
    assert "audit_reviewer_start_failed" not in log.text()
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_text(
        encoding="utf-8"
    ) == handoff


# --------------------------------------------------------------------------
# reviewer subprocess environment (end to end)
# --------------------------------------------------------------------------


REVIEWER_UNRELATED_SECRETS = {
    "GITHUB_TOKEN": "ghp-secret",
    "GH_TOKEN": "gh-secret",
    "AWS_ACCESS_KEY_ID": "aws-id",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "AWS_SESSION_TOKEN": "aws-session",
    "NPM_TOKEN": "npm-secret",
}
REVIEWER_VENDOR_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
    "OPENAI_API_KEY": "openai-secret",
    "GEMINI_API_KEY": "gemini-secret",
    "GOOGLE_API_KEY": "google-secret",
}


@pytest.fixture
def reviewer_secret_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    # PATH stays real: the fan-out runs git for the review scope.
    for name, value in {**REVIEWER_UNRELATED_SECRETS, **REVIEWER_VENDOR_KEYS}.items():
        monkeypatch.setenv(name, value)


class _ProviderPopenRecorder:
    """Record the environment each provider CLI launch actually receives.

    Git runs for real. Every other command is replaced by a Python child that
    prints a placeholder line, so the reviewer launch is recorded without
    running a real provider CLI.
    """

    def __init__(self) -> None:
        self.real_popen = subprocess.Popen
        self.launches: list[tuple[list[str], dict[str, str]]] = []
        self.lock = threading.Lock()

    def __call__(self, args, *popen_args, **kwargs):
        command = [str(arg) for arg in args]
        if Path(command[0]).stem.lower() == "git":
            return self.real_popen(args, *popen_args, **kwargs)
        env = kwargs.get("env")
        with self.lock:
            self.launches.append(
                (command, dict(os.environ) if env is None else dict(env))
            )
        return self.real_popen(
            [sys.executable, "-c", "print('reviewer-stub')"],
            *popen_args,
            **kwargs,
        )

    def stems(self) -> set[str]:
        return {_launched_cli_stem(command) for command, _env in self.launches}


def _launched_cli_stem(command: list[str]) -> str:
    """Name the CLI a recorded launch ran, e.g. ``codex`` or ``claude``.

    On Windows an npm-installed CLI is launched as ``node <entry>.js ...``
    (see ``agent_process._resolve_command_argv``), so the CLI name is the
    script's stem rather than ``argv[0]``'s.
    """
    program = Path(command[0]).stem.lower()
    if program == "node" and len(command) > 1:
        return Path(command[1]).stem.lower()
    return program


def _with_codex_security_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    present: bool,
) -> None:
    real_which = audit_review.shutil.which

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        if name == audit_review.CODEX_SECURITY_BINARY:
            return "/opt/bin/codex-security" if present else None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(audit_review.shutil, "which", which)


def _default_agents(**overrides: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            role: agent.model_dump(exclude_none=True)
            for role, agent in config_module._default_agent_configs().items()
        },
        **overrides,
    }


def _binary_stem(provider: str) -> str:
    binary = getattr(config_module, f"{provider.upper()}_BINARY")
    return Path(binary).stem.lower()


@pytest.mark.parametrize(
    ("quality_provider", "security_provider", "codex_security_cli"),
    [
        ("claude", "codex", False),
        ("claude", "codex", True),
        ("codex", "claude", False),
        ("claude", "antigravity", False),
    ],
)
def test_no_reviewer_subprocess_receives_unrelated_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_secret_environ: None,
    quality_provider: str,
    security_provider: str,
    codex_security_cli: bool,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    config = _fan_out_config(
        repo,
        security_provider=security_provider,
        agents=_default_agents(auditor={"provider": quality_provider}),
    )
    _with_codex_security_cli(monkeypatch, present=codex_security_cli)
    recorder = _ProviderPopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    orchestrator._dispatch_audit_fan_out(
        config,
        _Recorder(),
        audit_context=AuditFanOutContext(
            cycle_number=1,
            handoff_content=handoff,
            implementer_base_sha=base,
        ),
    )

    security_stem = (
        "codex-security" if codex_security_cli else _binary_stem(security_provider)
    )
    assert {_binary_stem(quality_provider), security_stem} <= recorder.stems()
    for command, env in recorder.launches:
        upper_names = {name.upper() for name in env}
        leaked = sorted(
            name
            for name in upper_names
            if name in REVIEWER_UNRELATED_SECRETS
            or name in REVIEWER_VENDOR_KEYS
            or name.startswith("AWS_")
        )
        assert not leaked, f"{command[0]} received {leaked}"
        assert "PATH" in upper_names


# --------------------------------------------------------------------------
# Security reviewer startup check
# --------------------------------------------------------------------------


@pytest.mark.parametrize("security_provider", [None, "claude", "codex"])
def test_startup_check_passes_for_available_security_reviewers(
    tmp_path: Path,
    security_provider: str | None,
) -> None:
    config = _fan_out_config(tmp_path, security_provider=security_provider)

    assert audit_review.security_reviewer_startup_error(config) is None


def test_startup_check_is_off_when_security_review_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed_clis(monkeypatch)
    config = DispatchConfig(
        repo_root=tmp_path,
        audit={**QUALITY_ONLY_AUDIT, "security_provider": "codex"},
    )

    assert audit_review.security_reviewer_startup_error(config) is None


def test_quality_prompt_skips_a_verification_the_dispatcher_already_passed() -> None:
    template = audit_review.QUALITY_FAN_OUT_PROMPT_TEMPLATE

    assert "## Dispatcher Verification" in template
    assert "Verdict: PASS" in template
    assert "do not run it again" in template
    assert "Run the configured verification commands when available" not in template


def test_quality_prompt_routes_the_last_story_of_an_epic_to_the_finalizer() -> None:
    # The fan-out writes the handoff from the reviewer's verdict, so the
    # reviewer, not the planner, must turn an epic's last approved story into
    # the epic close: the validator rejects a planner route to finalizer.
    template = audit_review.QUALITY_FAN_OUT_PROMPT_TEMPLATE

    assert "`remaining_stories: []`" in template
    assert "not an omitted field" in template
    assert "instead of sending it to planner to decide" in template


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("remaining_stories: []\n", []), ("", None)],
    ids=["explicit-empty", "omitted"],
)
def test_blocked_fan_out_keeps_an_explicit_empty_remaining_stories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    expected: list[str] | None,
) -> None:
    # The next audit routes the epic's last story to the finalizer only when
    # it sees an explicit `remaining_stories: []`, so a blocked audit must
    # carry that marker back to the implementer instead of dropping it.
    repo, base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "query = 'SELECT ' + user\n", "feature")
    handoff = _backend_handoff(head).replace(
        "producer: backend", f"{declared}producer: backend"
    )
    _write_handoff(repo, handoff)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(
                _security_payload(findings=[_finding("high", "SQL injection")])
            )
        ),
    )

    result, _log = _run_fan_out(
        _fan_out_config(repo),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    loaded = yaml.safe_load(written.split("---")[1])
    assert loaded["status"] == "verified_fail"
    assert loaded.get("remaining_stories") == expected


# --------------------------------------------------------------------------
# Linear mode: evidence step wiring and quality reviewer tools (LIN-9e)
# --------------------------------------------------------------------------


_LINEAR_TRACKER: dict[str, Any] = {
    "provider": "linear",
    "team": "ENG",
    "project": "Platform",
    "eligible_state": "Todo",
    "in_progress_state": "In Progress",
    "success_state": "In Review",
    "mcp_server": "linear",
}
_ALL_ROLES = ("planner", "backend", "frontend", "auditor", "validator", "finalizer")


def _normalize_run_text(text: str) -> str:
    text = re.sub(r"\d+\.\ds\b", "<elapsed>", text)
    return re.sub(r"\S*scope\.diff", "<diff>", text)


def test_vanilla_fan_out_never_calls_the_evidence_invoker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    monkeypatch.setattr(
        audit_review,
        "_run_logged_agent_command",
        lambda _name, _command, **_kwargs: _process(
            _claude_envelope(_security_payload())
        ),
    )
    config = _fan_out_config(repo)
    assert config.tracker.provider == "none"
    handoff_path = repo / "docs" / "handoff" / "HANDOFF.md"
    evidence_calls: list[str] = []
    outputs: list[tuple[str, str, Any]] = []

    for pass_invoker in (True, False):
        handoff_path.write_text(handoff, encoding="utf-8")
        prompts: list[str] = []
        agent_configs: list[Any] = []

        def quality_invoker(prompt: str, agent_config) -> SubagentResult:
            prompts.append(prompt)
            agent_configs.append(agent_config)
            return _subagent(json.dumps(_quality_payload()))

        kwargs: dict[str, Any] = {}
        if pass_invoker:
            kwargs["linear_evidence_invoker"] = (
                lambda prompt, _config: evidence_calls.append(prompt)
            )
        result = audit_review.run_audit_fan_out(
            config,
            context=AuditFanOutContext(
                cycle_number=2, handoff_content=handoff, implementer_base_sha=base
            ),
            frontmatter=parse_handoff_frontmatter_text(handoff),
            quality_invoker=quality_invoker,
            log=_Recorder(),
            **kwargs,
        )
        assert result.exit_code == 0, result.stderr
        outputs.append(
            (
                handoff_path.read_text(encoding="utf-8"),
                prompts[0],
                agent_configs[0],
            )
        )

    assert evidence_calls == []
    (with_handoff, with_prompt, with_config), (plain_handoff, plain_prompt, _c) = (
        outputs
    )
    assert _normalize_run_text(with_handoff) == _normalize_run_text(plain_handoff)
    assert with_prompt == with_prompt.replace(
        audit_review.QUALITY_LINEAR_MODE_SUFFIX, ""
    )
    assert _normalize_run_text(with_prompt) == _normalize_run_text(plain_prompt)
    diff_path = re.search(r"saved at (\S*scope\.diff)", with_prompt)
    assert diff_path is not None
    head = parse_handoff_frontmatter_text(handoff).scope_sha  # type: ignore[union-attr]
    assert with_prompt == audit_review.QUALITY_FAN_OUT_PROMPT_TEMPLATE.format(
        handoff_path="docs/handoff/HANDOFF.md",
        range_text=f"{base}..{head}",
        base_sha=base,
        head_sha=head,
        diff_path=diff_path.group(1),
        shape_hint=audit_review.QUALITY_VERDICT_SHAPE_HINT,
    )
    assert with_config.allowed_tools is None


def test_linear_quality_reviewer_loses_the_linear_write_tools(
    tmp_path: Path,
) -> None:
    agents: dict[str, Any] = {role: {"provider": "claude"} for role in _ALL_ROLES}
    agents["auditor"] = {
        "provider": "claude",
        "fallback": {"provider": "claude", "model": "fallback-model"},
    }
    config = DispatchConfig(
        repo_root=tmp_path, agents=agents, tracker=_LINEAR_TRACKER
    )
    assert "mcp__linear__save_comment" in (config.agents["auditor"].allowed_tools or ())

    quality = audit_review._quality_reviewer_agent_config(config)

    for agent in (quality, quality.fallback):
        assert agent is not None
        tools = agent.allowed_tools or ()
        assert "mcp__linear__save_comment" not in tools
        assert "mcp__linear__save_issue" not in tools
        for kept in (
            "mcp__linear__get_issue",
            "mcp__linear__list_issues",
            "mcp__linear__list_comments",
            "Read",
        ):
            assert kept in tools, kept
        assert agent.timeout_ms == config.audit.review_timeout_ms


def test_linear_quality_prompt_adds_the_no_linear_posting_sentence(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    vanilla = audit_review.build_quality_fan_out_prompt(
        scope, handoff_display="docs/handoff/HANDOFF.md", provider="claude"
    )
    linear = audit_review.build_quality_fan_out_prompt(
        scope,
        handoff_display="docs/handoff/HANDOFF.md",
        provider="claude",
        linear_mode=True,
    )
    codex = audit_review.build_quality_fan_out_prompt(
        scope,
        handoff_display="docs/handoff/HANDOFF.md",
        provider="codex",
        linear_mode=True,
    )

    assert linear == vanilla + audit_review.QUALITY_LINEAR_MODE_SUFFIX
    assert "Do not post Linear comments or change Linear issue state" in linear
    assert audit_review.QUALITY_LINEAR_MODE_SUFFIX not in vanilla
    assert codex.endswith(
        audit_review.QUALITY_LINEAR_MODE_SUFFIX
        + audit_review.CODEX_QUALITY_ENVELOPE_SUFFIX
    )


def test_run_loop_logs_the_stop_reason_when_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "x = 1\n", "feature")
    handoff = _backend_handoff(head)
    _write_handoff(repo, handoff)
    reason = "linear_evidence_failed: the evidence dispatch exited with code 2"

    def fake_fan_out(*_args: Any, **_kwargs: Any) -> DispatchResult:
        return DispatchResult(
            stdout="",
            stderr=reason,
            exit_code=1,
            elapsed_seconds=0.01,
            stop_loop=True,
            stop_reason=reason,
        )

    monkeypatch.setattr(orchestrator, "_dispatch_audit_fan_out", fake_fan_out)
    log = _Recorder()

    exit_code = orchestrator.run_loop(_fan_out_config(repo), max_cycles=3, log=log)

    assert exit_code == 1
    assert ("ERROR", f"Stopping the dispatch loop: {reason}") in log.messages
    assert "An audit reviewer failed to start" not in log.text()
    assert "Failure count:" not in log.text()
