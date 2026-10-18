from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from llm_handoff import antigravity_security_review as review
from llm_handoff import audit_review
from llm_handoff import config as config_module
from llm_handoff.agent_providers.antigravity import ANTIGRAVITY_UNSAFE_BYPASS_ARGS
from llm_handoff.audit_review import ReviewScope
from llm_handoff.config import AgentConfig, DispatchConfig, load_dispatch_config


SECRET = "planted-secret-7f3a9c"
DIFF_TEXT = f"diff --git a/src/app.py b/src/app.py\n+token = '{SECRET}-diff'\n"
VERDICT = {
    "verdict": "pass",
    "summary": "No security defects in the range.",
    "findings": [
        {
            "severity": "low",
            "file": "src/app.py",
            "line": 1,
            "title": "Hard-coded token",
            "description": "A token literal appears in the diff.",
            "recommendation": "Load it from the environment.",
        }
    ],
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _event(name: str, **payload: Any) -> str:
    return json.dumps({"event": name, name: payload})


def _step(step_type: str | None, **fields: Any) -> str:
    payload: dict[str, Any] = dict(fields)
    if step_type is not None:
        payload["step_type"] = step_type
    return _event("step_update", **payload)


def _result(
    structured_output: object = VERDICT,
    *,
    status: str = "SUCCESS",
) -> str:
    return _event(
        "result",
        status=status,
        structured_output=structured_output,
        response=json.dumps({"verdict": "fail", "summary": SECRET, "findings": []}),
    )


def _clean_stream(*extra_steps: str) -> list[str]:
    return [
        _event("init", permission_mode="default"),
        _step("user_input", text_delta=SECRET),
        _step("tool", tool_name="view_file", step_index=1),
        _step("tool", tool_name="grep_search", step_index=2),
        _step("tool", tool_name="list_dir", step_index=3),
        _step("tool", tool_name="find_by_name", step_index=4),
        *extra_steps,
        _step("agent_response", text_delta="Reviewing."),
        _step("finish", step_index=5),
        _result(),
    ]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _config(
    repo: Path,
    *,
    role_overrides: dict[str, Any] | None = None,
) -> DispatchConfig:
    agents = {
        role: agent.model_dump(exclude_none=True)
        for role, agent in config_module._default_agent_configs().items()
    }
    if role_overrides is not None:
        agents["planner"] = {"provider": "antigravity", **role_overrides}
    return DispatchConfig(
        repo_root=repo,
        agents=agents,
        audit={
            "reviewers": ["quality", "security"],
            "security_provider": "antigravity",
            "review_timeout_ms": 90_500,
        },
    )


def _scope(tmp_path: Path) -> ReviewScope:
    log_dir = tmp_path / "log"
    log_dir.mkdir(exist_ok=True)
    diff_path = log_dir / "scope.diff"
    diff_path.write_text(DIFF_TEXT, encoding="utf-8")
    return ReviewScope(
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=("src/app.py",),
        diff_path=diff_path,
    )


def _plan(config: DispatchConfig, tmp_path: Path) -> audit_review.SecurityReviewerPlan:
    return audit_review.build_security_reviewer_plan(
        config,
        provider="antigravity",
        scope=_scope(tmp_path),
        handoff_display="docs/handoff/HANDOFF.md",
        log_dir=tmp_path / "log",
    )


class _FakeAgy:
    """Stands in for `_run_antigravity_process` and records each call."""

    def __init__(
        self,
        *,
        stdout: str | list[str] = "",
        stderr: str = "",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.stdout = "\n".join(stdout) + "\n" if isinstance(stdout, list) else stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        cwd_path = Path(cwd)
        agent_files = list((cwd_path / ".agents" / "agents").glob("*.md"))
        self.calls.append(
            {
                "command": list(command),
                "cwd": cwd_path,
                "env": env,
                "timeout": timeout,
                "agent_files": agent_files,
                "agent_text": [
                    path.read_text(encoding="utf-8") for path in agent_files
                ],
                "schema": json.loads(
                    (cwd_path / "security.schema.json").read_text(encoding="utf-8")
                ),
                "diff": (cwd_path / "scope.diff").read_text(encoding="utf-8"),
            }
        )
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            command, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: _FakeAgy,
    *,
    config: DispatchConfig | None = None,
) -> audit_review.ReviewerRun:
    monkeypatch.setattr(review, "_run_antigravity_process", fake)
    repo = _repo(tmp_path) if config is None else config.repo_root
    config = config or _config(repo)
    plan = _plan(config, tmp_path)
    handoff = repo / "docs" / "handoff" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("---\nnext_agent: auditor\n---\n", encoding="utf-8")
    outcome = audit_review._run_security_reviewer(
        plan,
        timeout_ms=config.audit.review_timeout_ms,
        repo_root=repo,
        handoff_path=handoff,
        log=None,
    )
    return outcome.run


# --------------------------------------------------------------------------
# command shape
# --------------------------------------------------------------------------


def test_command_shape_has_fixed_instruction_and_no_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(stdout=_clean_stream())
    config = _config(_repo(tmp_path), role_overrides={"unsafe_bypass": True})

    run = _run(monkeypatch, tmp_path, fake, config=config)

    command = fake.calls[0]["command"]
    assert run.error is None
    assert command[1:3] == ["-p", review.ANTIGRAVITY_SECURITY_INSTRUCTION]
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert command[command.index("--print-timeout") + 1] == "90s"
    schema_arg = Path(command[command.index("--json-schema") + 1])
    assert schema_arg.parent == fake.calls[0]["cwd"]
    assert schema_arg.name == "security.schema.json"
    assert "--model" not in command
    for flag in ANTIGRAVITY_UNSAFE_BYPASS_ARGS:
        assert flag not in command
    assert not any(SECRET in token for token in command)
    assert not any("diff --git" in token for token in command)
    assert run.command == tuple(command)


def test_model_flag_only_when_the_antigravity_role_sets_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(stdout=_clean_stream())
    config = _config(_repo(tmp_path), role_overrides={"model": "agy-model"})

    _run(monkeypatch, tmp_path, fake, config=config)

    command = fake.calls[0]["command"]
    assert command[command.index("--model") + 1] == "agy-model"


def test_print_timeout_has_a_one_second_floor() -> None:
    command = review.build_antigravity_security_command(
        binary="agy",
        agent_name="llm-handoff-security-x",
        schema_path="schema.json",
        timeout_ms=200,
        model=None,
    )

    assert command[command.index("--print-timeout") + 1] == "1s"


def test_environment_follows_the_antigravity_role_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    fake = _FakeAgy(stdout=_clean_stream())
    config = _config(
        _repo(tmp_path), role_overrides={"env_passthrough": ["HTTPS_PROXY"]}
    )

    _run(monkeypatch, tmp_path, fake, config=config)

    env = fake.calls[0]["env"]
    assert env["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert "GITHUB_TOKEN" not in env
    assert "GEMINI_API_KEY" not in env


def test_environment_defaults_to_a_plain_antigravity_agent_config() -> None:
    assert review.antigravity_security_env(None) == review.antigravity_security_env(
        AgentConfig(provider="antigravity")
    )


# --------------------------------------------------------------------------
# temp directory and agent file
# --------------------------------------------------------------------------


def test_agent_file_has_the_exact_tool_list_and_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(stdout=_clean_stream())

    _run(monkeypatch, tmp_path, fake)

    call = fake.calls[0]
    assert len(call["agent_files"]) == 1
    agent_path = call["agent_files"][0]
    text = call["agent_text"][0]
    frontmatter, body = text.split("---\n", 2)[1:]
    lines = frontmatter.splitlines()
    assert f"name: {agent_path.stem}" in lines
    assert agent_path.stem.startswith("llm-handoff-security-")
    assert len(agent_path.stem) == len("llm-handoff-security-") + 32
    assert (
        "tools: [view_file, list_dir, grep_search, find_by_name, finish]" in lines
    )
    assert "commandExecutionPolicy: off" in lines
    command = call["command"]
    assert command[command.index("--agent") + 1] == agent_path.stem
    # The body is the review prompt plus the antigravity suffix; it names
    # the temp diff copy and the repo root, never the diff contents.
    temp_diff = (call["cwd"] / "scope.diff").as_posix()
    assert temp_diff in body
    assert (tmp_path / "repo").resolve().as_posix() in body
    assert "cannot run `git`" in body
    assert "b" * 40 in body
    assert SECRET not in text
    assert call["diff"] == DIFF_TEXT
    assert call["schema"] == audit_review.SECURITY_VERDICT_JSON_SCHEMA


def test_tool_list_is_read_only() -> None:
    assert review.ANTIGRAVITY_SECURITY_TOOLS == (
        "view_file",
        "list_dir",
        "grep_search",
        "find_by_name",
        "finish",
    )
    assert "run_command" not in review.ANTIGRAVITY_SECURITY_TOOLS


def test_temp_directory_is_outside_the_repo_and_removed_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(stdout=_clean_stream())

    run = _run(monkeypatch, tmp_path, fake)

    cwd = fake.calls[0]["cwd"]
    assert run.error is None
    assert not cwd.resolve().is_relative_to((tmp_path / "repo").resolve())
    assert not cwd.exists()


@pytest.mark.parametrize(
    "raises",
    [
        subprocess.TimeoutExpired(["agy"], 1),
        OSError(2, "No such file or directory"),
        None,
    ],
)
def test_temp_directory_is_removed_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raises: BaseException | None,
) -> None:
    fake = _FakeAgy(stdout="", returncode=3, raises=raises)

    run = _run(monkeypatch, tmp_path, fake)

    assert run.error is not None
    assert not fake.calls[0]["cwd"].exists()


def test_temp_directory_is_removed_when_the_runner_raises_unexpectedly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(raises=RuntimeError("boom"))
    monkeypatch.setattr(review, "_run_antigravity_process", fake)
    plan = _plan(_config(_repo(tmp_path)), tmp_path)

    with pytest.raises(RuntimeError):
        audit_review._run_security_reviewer(
            plan,
            timeout_ms=1000,
            repo_root=tmp_path / "repo",
            handoff_path=tmp_path / "repo" / "HANDOFF.md",
            log=None,
        )

    assert not fake.calls[0]["cwd"].exists()


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_clean_stream_with_read_tool_steps_gives_the_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(stdout=_clean_stream())

    run = _run(monkeypatch, tmp_path, fake)

    assert run.error is None
    assert run.verdict is not None
    # Read from structured_output, never from `response`.
    assert run.verdict.verdict == "pass"
    assert run.verdict.summary == VERDICT["summary"]
    assert run.verdict.findings[0].title == "Hard-coded token"
    assert run.start_failure is None


def test_flat_type_events_are_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = [
        json.dumps({"type": "init"}),
        json.dumps({"type": "step_update", "step_type": "tool", "tool_name": "view_file"}),
        json.dumps({"type": "result", "status": "SUCCESS", "structured_output": VERDICT}),
    ]

    run = _run(monkeypatch, tmp_path, _FakeAgy(stdout=stdout))

    assert run.error is None
    assert run.verdict is not None


_SUCCESS_STREAM_PREFIX = [_event("init"), _step("user_input")]

FAILURE_CASES: dict[str, dict[str, Any]] = {
    "run_command tool step": {
        "stdout": _clean_stream(
            _step("tool", tool_name="run_command", command_line=f"echo {SECRET}")
        ),
        "prefix": "security_reviewer_failed:",
    },
    "write_to_file tool step": {
        "stdout": _clean_stream(
            _step("tool", tool_name="write_to_file", path=f"/tmp/{SECRET}")
        ),
        "prefix": "security_reviewer_failed:",
    },
    "tool step without tool_name": {
        "stdout": _clean_stream(_step("tool", detail=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "checkpoint step": {
        "stdout": _clean_stream(_step("checkpoint", text_delta=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "step without step_type": {
        "stdout": _clean_stream(_step(None, text_delta=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "array step_type": {
        "stdout": _clean_stream(_event("step_update", step_type=[], note=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "object step_type": {
        "stdout": _clean_stream(
            _event("step_update", step_type={"tool": SECRET}, note=SECRET)
        ),
        "prefix": "security_reviewer_failed:",
    },
    "array tool_name": {
        "stdout": _clean_stream(_step("tool", tool_name=["view_file"], note=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "object tool_name": {
        "stdout": _clean_stream(_step("tool", tool_name={}, note=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "numeric tool_name": {
        "stdout": _clean_stream(_step("tool", tool_name=7, note=SECRET)),
        "prefix": "security_reviewer_failed:",
    },
    "unknown event": {
        "stdout": _clean_stream(_event("telemetry", note=SECRET)),
        "prefix": "security_verdict_malformed:",
    },
    "non-JSON line": {
        "stdout": _clean_stream(f"plain text {SECRET}"),
        "prefix": "security_verdict_malformed:",
    },
    "line after result": {
        "stdout": [*_clean_stream(), _step("agent_response", text_delta=SECRET)],
        "prefix": "security_verdict_malformed:",
    },
    "two result events": {
        "stdout": [*_clean_stream(), _result()],
        "prefix": "security_verdict_malformed:",
    },
    "no result event": {
        "stdout": [*_SUCCESS_STREAM_PREFIX, _step("finish", text_delta=SECRET)],
        "prefix": "security_verdict_malformed:",
    },
    "non-SUCCESS status": {
        "stdout": [*_SUCCESS_STREAM_PREFIX, _result(status=f"ERROR {SECRET}")],
        "prefix": "security_reviewer_failed:",
    },
    "extra structured_output keys": {
        "stdout": [
            *_SUCCESS_STREAM_PREFIX,
            _result({**VERDICT, "note": SECRET}),
        ],
        "prefix": "security_verdict_malformed:",
    },
    "missing structured_output key": {
        "stdout": [
            *_SUCCESS_STREAM_PREFIX,
            _result({"verdict": "pass", "summary": SECRET}),
        ],
        "prefix": "security_verdict_malformed:",
    },
    "structured_output not an object": {
        "stdout": [*_SUCCESS_STREAM_PREFIX, _result(f"pass {SECRET}")],
        "prefix": "security_verdict_malformed:",
    },
    "structured_output missing": {
        "stdout": [*_SUCCESS_STREAM_PREFIX, _event("result", status="SUCCESS")],
        "prefix": "security_verdict_malformed:",
    },
    "structured_output fails validation": {
        "stdout": [
            *_SUCCESS_STREAM_PREFIX,
            _result({"verdict": SECRET, "summary": "", "findings": []}),
        ],
        "prefix": "security_verdict_malformed:",
    },
    "nonzero exit": {
        "stdout": _clean_stream(),
        "returncode": 2,
        "prefix": "security_reviewer_failed:",
    },
    "timeout": {
        "raises": subprocess.TimeoutExpired(
            ["agy"], 1, output=SECRET, stderr=SECRET
        ),
        "prefix": "security_reviewer_failed:",
    },
    "sign-in failure": {
        "stderr": f"Error: authentication required ({SECRET})",
        "returncode": 1,
        "prefix": "security_reviewer_failed:",
        "start_failure": "auth",
    },
    "soft deny": {
        "stderr": (
            f'auto-denied the "command" permission for `cat {SECRET}`: cannot '
            "prompt for permission in print mode"
        ),
        "prefix": "security_reviewer_failed:",
        "contains": "allows no shell tools",
    },
}


@pytest.mark.parametrize("case", sorted(FAILURE_CASES))
def test_failures_close_without_quoting_agy_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    spec = FAILURE_CASES[case]
    stdout = spec.get("stdout", [_event("init", note=SECRET)])
    stderr = spec.get("stderr", f"warning: {SECRET}")
    fake = _FakeAgy(
        stdout=stdout,
        stderr=stderr,
        returncode=spec.get("returncode", 0),
        raises=spec.get("raises"),
    )

    run = _run(monkeypatch, tmp_path, fake)

    assert run.verdict is None
    assert run.error is not None
    assert run.error.startswith(spec["prefix"])
    assert SECRET not in run.error
    assert run.start_failure == spec.get("start_failure")
    if "contains" in spec:
        assert spec["contains"] in run.error
    if "raises" not in spec:
        # The raw output still reaches the audit review log files.
        assert run.stderr == stderr


@pytest.mark.parametrize(
    "event",
    [
        {"type": "step_update", "step_type": []},
        {"type": "step_update", "step_type": {}},
        {"type": "step_update", "step_type": "tool", "tool_name": {}},
        {"type": "step_update", "step_type": "tool", "tool_name": []},
    ],
)
def test_parser_rejects_unhashable_step_fields_without_raising(
    event: dict[str, object],
) -> None:
    structured_output, error, start_failure = review.parse_antigravity_security_output(
        returncode=0, stdout=json.dumps(event), stderr=""
    )

    assert structured_output is None
    assert error is not None
    assert error.startswith("security_reviewer_failed:")
    assert start_failure is None


def test_raw_output_reaches_the_audit_review_logs(tmp_path: Path) -> None:
    run = audit_review.ReviewerRun(
        name="security",
        provider="antigravity",
        command=("agy",),
        exit_code=0,
        elapsed_seconds=0.0,
        stdout=f"out {SECRET}",
        stderr=f"err {SECRET}",
        error="security_reviewer_failed: fixed text.",
    )
    log_dir = tmp_path / "log"
    log_dir.mkdir()

    audit_review._write_review_logs(log_dir, _scope(tmp_path), {"security": run})

    assert SECRET in (log_dir / "security.stdout.txt").read_text(encoding="utf-8")
    assert SECRET in (log_dir / "security.stderr.txt").read_text(encoding="utf-8")


def test_launch_failure_is_a_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgy(raises=FileNotFoundError(2, "No such file or directory"))

    run = _run(monkeypatch, tmp_path, fake)

    assert run.error is not None
    assert run.error.startswith("security_reviewer_failed:")
    assert run.start_failure == "launch"


# --------------------------------------------------------------------------
# config and startup
# --------------------------------------------------------------------------


def test_security_provider_antigravity_loads(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        "audit:\n  security_provider: antigravity\n", encoding="utf-8"
    )

    loaded = load_dispatch_config(repo_root=tmp_path)

    assert loaded.audit.security_provider == "antigravity"


def test_security_model_with_antigravity_fails_to_load(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text(
        "audit:\n"
        "  security_provider: antigravity\n"
        "  security_model: some-model\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="applies only to the claude security"):
        load_dispatch_config(repo_root=tmp_path)


def test_reviewer_binary_defaults_to_agy(tmp_path: Path) -> None:
    config = _config(_repo(tmp_path))

    assert (
        audit_review.security_reviewer_binary(config, "antigravity")
        == config_module.ANTIGRAVITY_BINARY
    )


def test_startup_check_needs_only_the_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(_repo(tmp_path))
    launched: list[object] = []
    monkeypatch.setattr(
        audit_review, "_run_logged_agent_command", lambda *a, **k: launched.append(a)
    )

    assert audit_review.security_reviewer_startup_error(config) is None
    assert launched == []

    monkeypatch.setattr(
        audit_review, "_reviewer_cli_available", lambda _config, _provider: False
    )
    error = audit_review.security_reviewer_startup_error(config)
    assert error is not None
    assert error.startswith("security_reviewer_failed:")
    assert "the antigravity security reviewer CLI" in error


def test_reviewer_writing_the_handoff_fails_the_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    handoff = repo / "docs" / "handoff" / "HANDOFF.md"

    class _WritingAgy(_FakeAgy):
        def __call__(self, command, *, cwd, env, timeout):
            handoff.write_text("tampered\n", encoding="utf-8")
            return super().__call__(command, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(review, "_run_antigravity_process", _WritingAgy(stdout=_clean_stream()))
    config = _config(repo)
    plan = _plan(config, tmp_path)
    handoff.parent.mkdir(parents=True)
    handoff.write_text("original\n", encoding="utf-8")
    before = audit_review._handoff_sha(handoff)

    outcome = audit_review._run_security_reviewer(
        plan,
        timeout_ms=1000,
        repo_root=repo,
        handoff_path=handoff,
        log=None,
    )

    # run_audit_fan_out turns this hash mismatch into reviewer_wrote_handoff.
    assert outcome.handoff_sha_after != before
