from __future__ import annotations

import json
import os
from pathlib import Path
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
from llm_handoff.router import parse_handoff_frontmatter_text
from llm_handoff.validator import validate_handoff_text


STORY_ID = "AUDIT-S1"
NL = chr(10)
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
        "audit": audit,
    }
    if agents is not None:
        kwargs["agents"] = agents
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


def _subagent(stdout: str, exit_code: int = 0) -> SubagentResult:
    return SubagentResult(
        stdout=stdout, stderr="", exit_code=exit_code, elapsed_seconds=0.01
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
) -> tuple[DispatchResult, _Recorder]:
    log = _Recorder()

    def quality_invoker(prompt: str, agent_config) -> SubagentResult:
        if quality_side_effect is not None:
            quality_side_effect(prompt, agent_config)
        return _subagent(quality_stdout)

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


def test_audit_config_defaults_preserve_single_auditor(tmp_path: Path) -> None:
    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.audit.reviewers == ("quality",)
    assert loaded.audit.security_provider is None
    assert loaded.audit.block_severity == "high"
    assert loaded.audit.review_timeout_ms == 900_000
    assert loaded.audit.security_enabled is False


def test_audit_config_loads_from_yaml(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        """audit:
  reviewers: [quality, security]
  security_provider: gemini
  block_severity: medium
  review_timeout_ms: 1234
""",
        encoding="utf-8",
    )

    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.audit.reviewers == ("quality", "security")
    assert loaded.audit.security_provider == "gemini"
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
    [("codex", "claude"), ("claude", "codex"), ("gemini", "claude")],
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


# --------------------------------------------------------------------------
# default path stays byte-for-byte unchanged
# --------------------------------------------------------------------------


def test_default_config_dispatches_original_single_auditor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DispatchConfig(repo_root=tmp_path)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_support(*args: Any, **kwargs: Any) -> SubagentResult:
        calls.append((args, kwargs))
        return _subagent("audit report")

    def fail_fan_out(*_args: Any, **_kwargs: Any) -> DispatchResult:
        raise AssertionError("fan-out must not run with the default config")

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


def test_default_config_run_loop_skips_fan_out_bookkeeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _init_repo(tmp_path)
    head = _commit_file(repo, "src/app.py", "print('hi')\n", "feature")
    _write_handoff(repo, _backend_handoff(head))
    config = DispatchConfig(repo_root=repo, poll_interval_seconds=0)
    prompts: list[str] = []

    def fake_support(name: str, prompt: str, **_kwargs: Any) -> SubagentResult:
        prompts.append(prompt)
        return _subagent("")

    def fail_git_head(_repo_root: Path) -> str:
        raise AssertionError("default config must not capture the implementer base")

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    monkeypatch.setattr(orchestrator, "_git_head", fail_git_head)
    log = _Recorder()

    orchestrator.run_loop(config, max_cycles=1, log=log)

    assert prompts[0] == ORIGINAL_AUDIT_PROMPT
    assert "Audit reviewers:" not in log.text()
    assert not (repo / "logs" / "dispatch" / "audit").exists()


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
    assert command[-2] == "-p"
    prompt = command[-1]
    assert "security review" in prompt
    assert scope.range_text in prompt
    assert "Do not scan the whole repository" in prompt
    assert "do not write docs/handoff/HANDOFF.md" in prompt
    assert plan.env is not None and "ANTHROPIC_API_KEY" not in plan.env


def test_gemini_security_command_uses_extension_in_plan_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "secret")
    config = _fan_out_config(tmp_path, security_provider="gemini")
    scope = _scope(tmp_path)

    plan = audit_review.build_security_reviewer_plan(
        config,
        provider="gemini",
        scope=scope,
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path,
    )

    command = list(plan.command)
    assert plan.kind == "gemini"
    assert command[0] == config_module.GEMINI_BINARY
    assert command[command.index("--approval-mode") + 1] == "plan"
    assert "--yolo" not in command
    assert command[-2] == "-p"
    assert command[-1].startswith("/security:analyze ")
    assert scope.base_sha in command[-1] and scope.head_sha in command[-1]
    assert plan.precheck_command == (config_module.GEMINI_BINARY, "extensions", "list")
    assert plan.env is not None and "GOOGLE_API_KEY" not in plan.env


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


def test_fan_out_fails_closed_when_gemini_extension_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    commands: list[list[str]] = []

    def fake_runner(_name: str, command: list[str], **_kwargs: Any) -> _ProcessResult:
        commands.append(command)
        return _process("No extensions installed.")

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_runner)

    result, _log = _run_fan_out(
        _fan_out_config(repo, security_provider="gemini"),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 1
    assert "security_extension_missing:" in result.stderr
    assert (
        "gemini extensions install https://github.com/gemini-cli-extensions/security"
        in (result.stderr)
    )
    assert commands == [[config_module.GEMINI_BINARY, "extensions", "list"]]
    assert (repo / "docs" / "handoff" / "HANDOFF.md").read_text(
        encoding="utf-8"
    ) == handoff


def test_fan_out_runs_gemini_review_when_extension_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, _head, handoff = _fan_out_repo(tmp_path)
    commands: list[list[str]] = []

    def fake_runner(_name: str, command: list[str], **_kwargs: Any) -> _ProcessResult:
        commands.append(command)
        if command[1:] == ["extensions", "list"]:
            return _process("gemini-cli-security (0.5.0)\n")
        return _process(
            "Report follows.\n"
            + json.dumps(_security_payload(findings=[_finding("medium")]))
        )

    monkeypatch.setattr(audit_review, "_run_logged_agent_command", fake_runner)

    result, _log = _run_fan_out(
        _fan_out_config(repo, security_provider="gemini", block_severity="medium"),
        handoff,
        quality_stdout=json.dumps(_quality_payload()),
        base_sha=base,
    )

    assert result.exit_code == 0, result.stderr
    assert len(commands) == 2
    written = (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    assert "status: verified_fail" in written


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
    config = _fan_out_config(repo)

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
    prints the Gemini security extension name, so the Gemini precheck passes
    and the reviewer launch itself is reached.
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
            [sys.executable, "-c", "print('gemini-cli-security')"],
            *popen_args,
            **kwargs,
        )

    def stems(self) -> set[str]:
        return {Path(command[0]).stem.lower() for command, _env in self.launches}


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
        ("gemini", "gemini", False),
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
# Gemini reviewer startup check
# --------------------------------------------------------------------------


def _no_gemini_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit_review.gemini_provider,
        "detect_gemini_sandbox_runtime",
        lambda: None,
    )


def _agents_without_gemini() -> dict[str, Any]:
    return _default_agents(
        planner={"provider": "codex"},
        frontend={"provider": "codex"},
    )


def test_run_loop_refuses_to_start_when_gemini_reviewer_has_no_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_gemini_sandbox(monkeypatch)
    config = _fan_out_config(
        tmp_path,
        security_provider="gemini",
        agents=_agents_without_gemini(),
    )
    log = _Recorder()

    exit_code = orchestrator.run_loop(config, max_cycles=1, log=log)

    assert exit_code == 1
    errors = [message for level, message in log.messages if level == "ERROR"]
    assert len(errors) == 1
    assert errors[0].startswith("security_reviewer_failed:")
    assert "audit.security_provider" in errors[0]
    assert "unsafe_bypass" not in errors[0]


def test_startup_check_follows_gemini_role_unsafe_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_gemini_sandbox(monkeypatch)
    config = _fan_out_config(
        tmp_path,
        security_provider="gemini",
        agents=_default_agents(
            planner={"provider": "gemini", "unsafe_bypass": True},
            frontend={"provider": "codex"},
        ),
    )

    assert audit_review.security_reviewer_startup_error(config) is None


def test_startup_check_names_gemini_role_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_gemini_sandbox(monkeypatch)
    config = _fan_out_config(tmp_path, security_provider="gemini")

    error = audit_review.security_reviewer_startup_error(config)

    assert error is not None
    assert "agents.planner.unsafe_bypass: true" in error


@pytest.mark.parametrize("security_provider", [None, "claude", "codex"])
def test_startup_check_skips_non_gemini_security_reviewers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    security_provider: str | None,
) -> None:
    _no_gemini_sandbox(monkeypatch)
    config = _fan_out_config(tmp_path, security_provider=security_provider)

    assert audit_review.security_reviewer_startup_error(config) is None


def test_startup_check_is_off_when_security_review_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_gemini_sandbox(monkeypatch)
    config = DispatchConfig(
        repo_root=tmp_path,
        audit={"reviewers": ["quality"], "security_provider": "gemini"},
    )

    assert audit_review.security_reviewer_startup_error(config) is None
