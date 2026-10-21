from __future__ import annotations

import json
from pathlib import Path, PurePath, PurePosixPath
import re

import pytest

from llm_handoff import chat_side_session, config
from llm_handoff.agent_providers.claude import claude_permission_args
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.chat_side_session import (
    SUPPORTED_CHAT_PROVIDERS,
    ChatSessionResult,
    _AntigravityEditTracker,
    build_chat_prompt,
    normalize_edited_paths,
    run_chat_session,
)


def _lines(*events: dict[str, object]) -> list[str]:
    return [json.dumps(event) + "\n" for event in events]


class _FakeRun:
    def __init__(
        self,
        lines: list[str],
        *,
        exit_code: int = 0,
        stderr: str = "",
    ) -> None:
        self.lines = lines
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls: list[dict[str, object]] = []

    def __call__(self, command, *, cwd, timeout_ms, idle_timeout_ms=None, env=None,
                 on_stdout=None, on_stderr=None, stop_on_chat_takeover="unset"):
        self.calls.append(
            {
                "command": list(command),
                "cwd": cwd,
                "timeout_ms": timeout_ms,
                "idle_timeout_ms": idle_timeout_ms,
                "env": env,
                "stop_on_chat_takeover": stop_on_chat_takeover,
            }
        )
        if on_stdout is not None:
            for line in self.lines:
                on_stdout(line)
        return _ProcessResult(
            stdout="".join(self.lines), stderr=self.stderr, exit_code=self.exit_code
        )

    @property
    def command(self) -> list[str]:
        return self.calls[-1]["command"]  # type: ignore[return-value]


def _install(monkeypatch, fake: _FakeRun) -> _FakeRun:
    monkeypatch.setattr(chat_side_session, "_run_command_streaming", fake)
    return fake


def _claude_init(session_id: str = "claude-sess-1") -> dict[str, object]:
    return {"type": "system", "subtype": "init", "session_id": session_id}


def _claude_result(text: str = "Done.") -> dict[str, object]:
    return {"type": "result", "subtype": "success", "result": text}


def _tool_use(tool_id: str, name: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": payload}
            ]
        },
    }


def _tool_result(tool_id: str, *, is_error: bool = False, text: str = "ok") -> dict[str, object]:
    item: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": text,
    }
    if is_error:
        item["is_error"] = True
    return {"type": "user", "message": {"content": [item]}}


def _codex_thread(thread_id: str = "codex-thread-1") -> dict[str, object]:
    return {"type": "thread.started", "thread_id": thread_id}


def _codex_message(text: str = "Answer.") -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {"id": "m1", "type": "agent_message", "text": text},
    }


def _codex_file_change(paths: list[str], status: str = "completed") -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "id": "fc",
            "type": "file_change",
            "status": status,
            "changes": [{"path": path, "kind": "update"} for path in paths],
        },
    }


def _run(provider: str, tmp_path: Path, **kwargs) -> ChatSessionResult:
    params: dict[str, object] = {
        "repo_root": tmp_path,
        "role": "backend",
        "story_id": "S-1",
        "handoff_path": tmp_path / "docs" / "handoff" / "HANDOFF.md",
    }
    params.update(kwargs)
    message = params.pop("message", "Please fix the typo.")
    return run_chat_session(provider, message, **params)  # type: ignore[arg-type]


# --- API -------------------------------------------------------------------


def test_supported_providers_constant() -> None:
    assert SUPPORTED_CHAT_PROVIDERS == ("claude", "codex", "antigravity")


def test_result_is_frozen_dataclass() -> None:
    result = ChatSessionResult(
        text="", session_id=None, exit_code=0, error=None, edited_paths=()
    )
    with pytest.raises(Exception):
        result.text = "x"  # type: ignore[misc]


@pytest.mark.parametrize("provider", ["gemini", "agy", "Antigravity", "unknown"])
def test_unsupported_provider_raises_before_launch(
    monkeypatch, tmp_path, provider
) -> None:
    fake = _install(monkeypatch, _FakeRun([]))
    with pytest.raises(ValueError) as excinfo:
        _run(provider, tmp_path)
    assert f"`{provider}`" in str(excinfo.value)
    assert "only claude, codex, and antigravity" in str(excinfo.value)
    assert fake.calls == []


def _agy(name: str, **fields: object) -> dict[str, object]:
    """One agy stream-json event in its nested ``event`` envelope."""

    return {"event": name, name: fields}


def _agy_result(
    response: str = "Done.", conversation_id: str = "agy-conv-1", status: str = "SUCCESS"
) -> dict[str, object]:
    return _agy(
        "result", conversation_id=conversation_id, status=status, response=response
    )


@pytest.mark.parametrize(
    ("provider", "lines"),
    [
        ("claude", _lines(_claude_init(), _claude_result())),
        ("codex", _lines(_codex_thread(), _codex_message())),
        ("antigravity", _lines(_agy_result())),
    ],
)
def test_chat_launch_is_never_stopped_by_a_chat_takeover(
    monkeypatch, tmp_path, provider, lines
) -> None:
    # A chat answer must not be stopped by the takeover its predecessor
    # requested, so both launches opt out explicitly.
    fake = _install(monkeypatch, _FakeRun(lines))
    _run(provider, tmp_path)
    [call] = fake.calls
    assert call["stop_on_chat_takeover"] is False


@pytest.mark.parametrize("message", ["", "   \n\t"])
def test_blank_message_raises_before_launch(monkeypatch, tmp_path, message) -> None:
    fake = _install(monkeypatch, _FakeRun([]))
    with pytest.raises(ValueError):
        _run("claude", tmp_path, message=message)
    assert fake.calls == []


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
def test_blank_session_id_raises_before_launch(monkeypatch, tmp_path, provider) -> None:
    fake = _install(monkeypatch, _FakeRun([]))
    with pytest.raises(ValueError):
        _run(provider, tmp_path, session_id="  ")
    assert fake.calls == []


# --- Prompt ----------------------------------------------------------------


def test_prompt_names_context_and_ends_with_message() -> None:
    prompt = build_chat_prompt(
        "  What changed?  \n",
        role="auditor",
        story_id="CHAT-17",
        handoff_path=Path("docs/handoff/HANDOFF.md"),
        log_path=Path("logs/dispatch/auditor.log"),
    )
    assert "auditor" in prompt
    assert "CHAT-17" in prompt
    assert str(Path("docs/handoff/HANDOFF.md")) in prompt
    assert str(Path("logs/dispatch/auditor.log")) in prompt
    assert "side session" in prompt
    assert "keeps running" in prompt
    assert "may edit files and HANDOFF.md" in prompt
    assert "Do not push" in prompt
    assert prompt.endswith("What changed?")


def test_prompt_without_story_or_log() -> None:
    prompt = build_chat_prompt(
        "hi",
        role="backend",
        story_id=None,
        handoff_path="HANDOFF.md",
        log_path=None,
    )
    assert "Story id: none" in prompt
    assert "No log path is known" in prompt
    assert prompt.endswith("hi")


def test_default_prompt_is_unchanged_byte_for_byte() -> None:
    prompt = build_chat_prompt(
        " hi there \n",
        role="backend",
        story_id="S-1",
        handoff_path="docs/handoff/HANDOFF.md",
        log_path="logs/dispatch/run.log",
    )
    assert prompt == (
        "The operator is talking to you in a side session while the `backend` "
        "role keeps running in the dispatch loop.\n"
        "Active role: backend\n"
        "Story id: S-1\n"
        "HANDOFF.md path: docs/handoff/HANDOFF.md\n"
        "The `backend` role's log: logs/dispatch/run.log\n\n"
        "You may edit files and HANDOFF.md to do what the operator asks. "
        "Do not push to any remote. Answer the operator in plain text.\n\n"
        "Operator message:\nhi there"
    )
    assert prompt == build_chat_prompt(
        " hi there \n",
        role="backend",
        story_id="S-1",
        handoff_path="docs/handoff/HANDOFF.md",
        log_path="logs/dispatch/run.log",
        resumed_role=False,
    )


def test_resumed_role_prompt() -> None:
    prompt = build_chat_prompt(
        "  Why did you skip the test?  \n",
        role="auditor",
        story_id="CHAT-33",
        handoff_path="docs/handoff/HANDOFF.md",
        log_path="logs/dispatch/run.log",
        resumed_role=True,
    )
    assert prompt.startswith(
        "You are the `auditor` role, resumed in your own session from the "
        "finished dispatch for the story below. The dispatch loop has moved on, "
        "and another role may be running now.\n"
    )
    assert "side session" not in prompt
    assert "Story id: CHAT-33\n" in prompt
    assert "HANDOFF.md path: docs/handoff/HANDOFF.md\n" in prompt
    assert "The `auditor` role's log: logs/dispatch/run.log\n" in prompt
    assert (
        "You may edit files and HANDOFF.md to do what the operator asks. "
        "Do not push to any remote. Answer the operator in plain text."
    ) in prompt
    assert prompt.endswith("\n\nOperator message:\nWhy did you skip the test?")


def test_resumed_role_prompt_without_story_or_log() -> None:
    prompt = build_chat_prompt(
        "hi",
        role="planner",
        story_id=None,
        handoff_path="HANDOFF.md",
        log_path=None,
        resumed_role=True,
    )
    assert "Story id: none\n" in prompt
    assert "No log path is known for that role.\n" in prompt
    assert prompt.endswith("Operator message:\nhi")


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
def test_resumed_role_without_session_id_raises_before_launch(
    monkeypatch, tmp_path, provider
) -> None:
    fake = _install(monkeypatch, _FakeRun([]))
    with pytest.raises(ValueError):
        _run(provider, tmp_path, resumed_role=True)
    assert fake.calls == []


def test_resumed_role_claude_command_keeps_role_arguments(
    monkeypatch, tmp_path
) -> None:
    agent_config = config.AgentConfig(
        provider="claude", model="role-model", allowed_tools=("Read", "Edit")
    )
    fake = _install(monkeypatch, _FakeRun(_lines(_claude_result())))
    _run(
        "claude",
        tmp_path,
        session_id="role-sess",
        agent_config=agent_config,
        resumed_role=True,
    )
    plain = _install(monkeypatch, _FakeRun(_lines(_claude_result())))
    _run("claude", tmp_path, session_id="role-sess", agent_config=agent_config)

    command = fake.command
    assert command[:-1] == plain.command[:-1]
    assert command[:3] == [config.CLAUDE_BINARY, "--resume", "role-sess"]
    expected_permissions = claude_permission_args(agent_config)
    assert command[3 : 3 + len(expected_permissions)] == expected_permissions
    assert command[command.index("--model") + 1] == "role-model"
    assert command[-1].startswith("You are the `backend` role, resumed")
    assert command[-1].endswith("Please fix the typo.")


def test_resumed_role_codex_command_keeps_role_arguments(
    monkeypatch, tmp_path
) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_codex_message("Sure."))))
    _run("codex", tmp_path, session_id="thread-7", resumed_role=True)

    command = fake.command
    sandbox = list(config.CODEX_SANDBOX_ARGS)
    assert command[:-1] == [
        config.CODEX_BINARY,
        *sandbox,
        "exec",
        "resume",
        "--json",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "thread-7",
    ]
    assert "--output-schema" not in command
    assert "--output-last-message" not in command
    assert command[-1].startswith("You are the `backend` role, resumed")


def test_module_docstring_names_the_wiring() -> None:
    doc = chat_side_session.__doc__ or ""
    assert "Nothing in the loop calls this module yet" not in doc
    assert "__main__._attach_console_chat" in doc
    assert "CHAT-32" in doc
    assert "CHAT-34" in doc
    assert "role_session" in doc


# --- Claude launch -----------------------------------------------------------


def test_claude_default_command_uses_dont_ask_permissions(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_claude_init(), _claude_result())))
    result = _run("claude", tmp_path)

    expected_permissions = claude_permission_args(config.AgentConfig(provider="claude"))
    assert "dontAsk" in expected_permissions
    command = fake.command
    assert command[0] == config.CLAUDE_BINARY
    assert command[1 : 1 + len(expected_permissions)] == expected_permissions
    tail = command[1 + len(expected_permissions) :]
    assert tail[:6] == [
        "--model",
        config.CLAUDE_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
    ]
    assert len(tail) == 7
    assert tail[6].endswith("Please fix the typo.")
    assert "--resume" not in command
    assert fake.calls[0]["cwd"] == tmp_path
    assert fake.calls[0]["timeout_ms"] == config.AGENT_TIMEOUT_MS
    assert isinstance(fake.calls[0]["env"], dict)
    assert result.text == "Done."
    assert result.session_id == "claude-sess-1"
    assert result.exit_code == 0
    assert result.error is None


def test_claude_role_allowed_tools_passed_unchanged(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_claude_result())))
    agent_config = config.AgentConfig(
        provider="claude",
        binary="my-claude",
        model="my-model",
        allowed_tools=("Read", "Edit", "Bash(git status:*)"),
        timeout_ms=1234,
    )
    _run("claude", tmp_path, agent_config=agent_config)

    command = fake.command
    expected_permissions = claude_permission_args(agent_config)
    assert command[0] == "my-claude"
    assert command[1 : 1 + len(expected_permissions)] == expected_permissions
    assert "Read,Edit,Bash(git status:*)" in command
    assert command.count("--disallowedTools") == expected_permissions.count(
        "--disallowedTools"
    )
    assert command.count("--strict-mcp-config") == expected_permissions.count(
        "--strict-mcp-config"
    )
    assert command[command.index("--model") + 1] == "my-model"
    assert fake.calls[0]["timeout_ms"] == 1234


def test_claude_unsafe_bypass_uses_role_flag(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_claude_result())))
    agent_config = config.AgentConfig(
        provider="claude",
        unsafe_bypass=True,
        permissions_flag="--custom-bypass",
    )
    _run("claude", tmp_path, agent_config=agent_config)

    command = fake.command
    assert command[1] == "--custom-bypass"
    assert "--permission-mode" not in command
    assert "--disallowedTools" not in command
    assert "--strict-mcp-config" not in command
    assert "--tools" not in command


def test_claude_unsafe_bypass_default_flag(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_claude_result())))
    _run(
        "claude",
        tmp_path,
        agent_config=config.AgentConfig(provider="claude", unsafe_bypass=True),
    )
    assert fake.command[1] == config.CLAUDE_PERMISSIONS_FLAG


def test_claude_resume_adds_resume_after_binary(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_claude_result("Again."))))
    result = _run("claude", tmp_path, session_id=" prior-sess ")

    assert fake.command[:3] == [config.CLAUDE_BINARY, "--resume", "prior-sess"]
    # No init event: falls back to the resumed id.
    assert result.session_id == "prior-sess"
    assert result.text == "Again."


def test_claude_session_id_from_first_init_event(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(_claude_init("first"), _claude_init("second"), _claude_result())
        ),
    )
    result = _run("claude", tmp_path, session_id="prior")
    assert result.session_id == "first"


# --- Claude edited paths -----------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "key"),
    [
        ("Edit", "file_path"),
        ("Write", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ],
)
def test_claude_edit_tools_counted(monkeypatch, tmp_path, tool, key) -> None:
    target = tmp_path / "src" / "app.py"
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _tool_use("t1", tool, {key: str(target)}),
                _tool_result("t1"),
                _claude_result(),
            )
        ),
    )
    result = _run("claude", tmp_path)
    assert result.edited_paths == ("src/app.py",)


def test_claude_refused_edit_bash_and_missing_result_not_counted(
    monkeypatch, tmp_path
) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _tool_use("t1", "Edit", {"file_path": str(tmp_path / "refused.py")}),
                _tool_result(
                    "t1",
                    is_error=True,
                    text="Permission to use Edit has been denied.",
                ),
                _tool_use("t2", "Bash", {"command": "echo hi > bash.txt"}),
                _tool_result("t2"),
                _tool_use("t3", "Write", {"file_path": str(tmp_path / "pending.py")}),
                _tool_use("t4", "Write", {"file_path": str(tmp_path / "ok.py")}),
                _tool_result("t4"),
                _claude_result(),
            )
        ),
    )
    result = _run("claude", tmp_path)
    assert result.edited_paths == ("ok.py",)


# --- Codex launch ------------------------------------------------------------


def test_codex_new_session_command(monkeypatch, tmp_path) -> None:
    fake = _install(
        monkeypatch, _FakeRun(_lines(_codex_thread(), _codex_message("Fixed it.")))
    )
    result = _run("codex", tmp_path)

    command = fake.command
    sandbox = list(config.CODEX_SANDBOX_ARGS)
    assert "workspace-write" in sandbox
    assert command[: 1 + len(sandbox)] == [config.CODEX_BINARY, *sandbox]
    assert command[1 + len(sandbox) : -1] == [
        "exec",
        "--json",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
    ]
    assert command[-1].endswith("Please fix the typo.")
    assert "--output-schema" not in command
    assert "--output-last-message" not in command
    assert fake.calls[0]["cwd"] == tmp_path
    assert result.text == "Fixed it."
    assert result.session_id == "codex-thread-1"
    assert result.error is None


def test_codex_resume_command_with_unsafe_bypass(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_codex_message("Sure."))))
    agent_config = config.AgentConfig(
        provider="codex", unsafe_bypass=True, binary="my-codex"
    )
    result = _run("codex", tmp_path, session_id="thread-9", agent_config=agent_config)

    command = fake.command
    assert command[:-1] == [
        "my-codex",
        "--yolo",
        "exec",
        "resume",
        "--json",
        "-c",
        f'web_search="{config.CODEX_WEB_SEARCH_MODE}"',
        "thread-9",
    ]
    assert "--output-schema" not in command
    assert "--output-last-message" not in command
    assert result.session_id == "thread-9"
    assert result.text == "Sure."


def test_codex_unwraps_structured_message(monkeypatch, tmp_path) -> None:
    envelope = json.dumps({"status": "ok", "summary": "Inner answer."})
    _install(monkeypatch, _FakeRun(_lines(_codex_message(envelope))))
    result = _run("codex", tmp_path)
    assert result.text == "Inner answer."


def test_codex_never_touches_saved_session_artifacts(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _FakeRun(_lines(_codex_thread(), _codex_message())))
    _run("codex", tmp_path)
    assert not (tmp_path / "logs").exists()


# --- Codex edited paths ------------------------------------------------------


def test_codex_file_change_paths(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _codex_thread(),
                {
                    "type": "item.started",
                    "item": {
                        "type": "file_change",
                        "status": "in_progress",
                        "changes": [{"path": "started.py"}],
                    },
                },
                _codex_file_change(["a.py", str(tmp_path / "b" / "c.py")]),
                _codex_file_change(["failed.py"], status="failed"),
                _codex_message(),
            )
        ),
    )
    result = _run("codex", tmp_path)
    assert result.edited_paths == ("a.py", "b/c.py")


# --- Path normalization ------------------------------------------------------


def test_normalize_paths(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = [
        str(repo / "pkg" / "mod.py"),
        "docs/readme.md",
        "../outside.txt",
        "pkg/../pkg/mod.py",
        str(tmp_path / "elsewhere.py"),
        "docs/readme.md",
    ]
    assert normalize_edited_paths(paths, repo_root=repo) == (
        "pkg/mod.py",
        "docs/readme.md",
    )


def test_normalize_drops_unresolvable_path(tmp_path) -> None:
    assert normalize_edited_paths(["bad\x00name", "ok.py"], repo_root=tmp_path) == (
        "ok.py",
    )


def test_claude_paths_normalized_with_duplicates_and_escape(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _tool_use("t1", "Edit", {"file_path": "a.py"}),
                _tool_result("t1"),
                _tool_use("t2", "Edit", {"file_path": str(tmp_path / "a.py")}),
                _tool_result("t2"),
                _tool_use("t3", "Write", {"file_path": "../escape.py"}),
                _tool_result("t3"),
                _claude_result(),
            )
        ),
    )
    result = _run("claude", tmp_path)
    assert result.edited_paths == ("a.py",)


# --- Errors ------------------------------------------------------------------


def test_claude_nonzero_exit_error_keeps_edited_paths(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _claude_init(),
                _tool_use("t1", "Write", {"file_path": "new.py"}),
                _tool_result("t1"),
            ),
            exit_code=2,
            stderr="boom\nsomething broke",
        ),
    )
    result = _run("claude", tmp_path)
    assert result.exit_code == 2
    assert result.text == ""
    assert result.error == "Claude exited with code 2. boom something broke"
    assert result.edited_paths == ("new.py",)


def test_codex_nonzero_exit_without_stderr(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(_lines(_codex_message("partial")), exit_code=1, stderr="  "),
    )
    result = _run("codex", tmp_path)
    assert result.error == "Codex exited with code 1."
    assert result.text == "partial"


@pytest.mark.parametrize(
    ("provider", "lines", "label"),
    [
        ("claude", _lines(_claude_init(), _claude_result("   ")), "Claude"),
        ("codex", _lines(_codex_thread()), "Codex"),
    ],
)
def test_no_answer_text_error(monkeypatch, tmp_path, provider, lines, label) -> None:
    _install(monkeypatch, _FakeRun(lines))
    result = _run(provider, tmp_path)
    assert result.exit_code == 0
    assert result.text == ""
    assert result.error == f"{label} produced no answer text."


def test_error_detail_is_redacted_and_capped(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun([], exit_code=3, stderr="x" * 500),
    )
    result = _run("codex", tmp_path)
    assert result.error is not None
    prefix = "Codex exited with code 3. "
    assert result.error.startswith(prefix)
    assert len(result.error) - len(prefix) <= 200


# --- Antigravity launch ------------------------------------------------------


def _agy_step(**fields: object) -> dict[str, object]:
    fields.setdefault("conversation_id", "agy-conv-1")
    return _agy("step_update", **fields)


def _agy_tool(
    step_index: int | None,
    state: str,
    tool: str = "write_to_file",
    *,
    target: str | None = None,
    error: str | None = None,
    tool_name: str | None = None,
) -> dict[str, object]:
    tool_info: dict[str, object] = {"name": tool}
    if target is not None:
        tool_info["parameters"] = {"TargetFile": target, "CodeContent": "x"}
    if error is not None:
        tool_info["error"] = {"type": "TOOL_ERROR", "message": error}
    fields: dict[str, object] = {
        "state": state,
        "step_type": "tool",
        "tool_name": tool if tool_name is None else tool_name,
        "tool_info": tool_info,
    }
    if step_index is not None:
        fields["step_index"] = step_index
    return _agy_step(**fields)


def _chunks(lines: list[str], size: int) -> list[str]:
    """Re-split a stream into fixed-size chunks that cut through events."""

    text = "".join(lines)
    return [text[i : i + size] for i in range(0, len(text), size)]


def test_antigravity_fresh_command_uses_role_contract(monkeypatch, tmp_path) -> None:
    fake = _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _agy("init", cwd=str(tmp_path), permission_mode="default"),
                _agy_result("Fixed it.", conversation_id="agy-new"),
            )
        ),
    )
    result = _run("antigravity", tmp_path)

    command = fake.command
    assert command[0] == config.ANTIGRAVITY_BINARY
    assert command[1] == "-p"
    assert command[3:] == [
        "--agent",
        "backend",
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{config.AGENT_TIMEOUT_MS // 1000}s",
        "--mode",
        "accept-edits",
    ]
    prompt = command[2]
    assert prompt == build_chat_prompt(
        "Please fix the typo.",
        role="backend",
        story_id="S-1",
        handoff_path=tmp_path / "docs" / "handoff" / "HANDOFF.md",
        log_path=None,
    )
    assert "Act as the" not in prompt
    assert "--conversation" not in command
    call = fake.calls[0]
    assert call["cwd"] == tmp_path
    assert call["timeout_ms"] == config.AGENT_TIMEOUT_MS
    assert call["idle_timeout_ms"] is None
    assert isinstance(call["env"], dict)
    assert call["stop_on_chat_takeover"] is False
    assert result == ChatSessionResult(
        text="Fixed it.",
        session_id="agy-new",
        exit_code=0,
        error=None,
        edited_paths=(),
    )


def test_antigravity_custom_config_resume_and_bypass(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_agy_result("Sure.", "agy-next"))))
    agent_config = config.AgentConfig(
        provider="antigravity",
        binary="agy-beta",
        model="pro",
        agent_name="backend-agy",
        unsafe_bypass=True,
        timeout_ms=90_500,
        idle_timeout_ms=5000,
        env_passthrough=("AGY_TEST_VAR",),
    )
    monkeypatch.setenv("AGY_TEST_VAR", "from-env")
    result = _run(
        "antigravity", tmp_path, session_id=" agy-prior ", agent_config=agent_config
    )

    command = fake.command
    assert command[0] == "agy-beta"
    assert command[3:] == [
        "--agent",
        "backend-agy",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "90s",
        "--model",
        "pro",
        "--conversation",
        "agy-prior",
        "--dangerously-skip-permissions",
    ]
    assert "--mode" not in command
    call = fake.calls[0]
    assert call["timeout_ms"] == 90_500
    assert call["idle_timeout_ms"] == 5000
    assert call["env"]["AGY_TEST_VAR"] == "from-env"  # type: ignore[index]
    assert result.session_id == "agy-next"
    assert result.text == "Sure."


def test_antigravity_safe_permissions_on_resume(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun(_lines(_agy_result())))
    _run(
        "antigravity",
        tmp_path,
        session_id="agy-prior",
        agent_config=config.AgentConfig(provider="antigravity", model="flash"),
    )
    assert fake.command[-6:] == [
        "--model",
        "flash",
        "--conversation",
        "agy-prior",
        "--mode",
        "accept-edits",
    ]
    assert "--dangerously-skip-permissions" not in fake.command


def test_antigravity_resume_without_new_id_keeps_the_session(
    monkeypatch, tmp_path
) -> None:
    fake = _install(
        monkeypatch,
        _FakeRun(_lines(_agy("result", status="SUCCESS", response="Again."))),
    )
    result = _run("antigravity", tmp_path, session_id="agy-prior")
    assert fake.command[fake.command.index("--conversation") + 1] == "agy-prior"
    assert result.session_id == "agy-prior"
    assert result.text == "Again."


def test_antigravity_resumed_role_keeps_role_arguments(monkeypatch, tmp_path) -> None:
    agent_config = config.AgentConfig(
        provider="antigravity", agent_name="auditor-agy", model="pro"
    )
    fake = _install(monkeypatch, _FakeRun(_lines(_agy_result())))
    _run(
        "antigravity",
        tmp_path,
        role="auditor",
        session_id="role-conv",
        agent_config=agent_config,
        resumed_role=True,
    )
    plain = _install(monkeypatch, _FakeRun(_lines(_agy_result())))
    _run(
        "antigravity",
        tmp_path,
        role="auditor",
        session_id="role-conv",
        agent_config=agent_config,
    )

    command = fake.command
    assert command[:2] == plain.command[:2]
    assert command[3:] == plain.command[3:]
    assert command[command.index("--agent") + 1] == "auditor-agy"
    assert command[command.index("--conversation") + 1] == "role-conv"
    assert command[2].startswith("You are the `auditor` role, resumed")
    assert command[2].endswith("Please fix the typo.")
    assert "Continue your assigned task" not in command[2]


def test_antigravity_text_from_deltas_and_chunked_stream(monkeypatch, tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    lines = _lines(
        _agy_step(step_type="user_input", text_delta="prompt echo "),
        _agy_tool(3, "ACTIVE", target=str(target)),
        _agy_tool(3, "DONE"),
        _agy_step(step_type="agent_response", text_delta="Hello "),
        _agy_step(
            conversation_id="agy-conv-2", step_type="agent_response", text_delta="world"
        ),
        _agy("result", status="SUCCESS", response=""),
    )
    # The last event has no trailing newline, as a stream cut at exit may.
    chunks = _chunks(lines, 7)
    chunks[-1] = chunks[-1].rstrip("\n")
    _install(monkeypatch, _FakeRun(chunks))
    result = _run("antigravity", tmp_path)

    assert result.exit_code == 0
    assert result.error is None
    assert result.text == "Hello world"
    assert result.session_id == "agy-conv-2"
    assert result.edited_paths == ("src/app.py",)


def test_antigravity_flat_events_are_accepted(monkeypatch, tmp_path) -> None:
    flat_tool = {
        "type": "step_update",
        "step_index": 1,
        "state": "DONE",
        "step_type": "tool",
        "tool_name": "replace_file_content",
        "tool_info": {"parameters": {"TargetFile": str(tmp_path / "flat.py")}},
    }
    flat_result = {
        "type": "result",
        "conversation_id": "flat-1",
        "status": "SUCCESS",
        "response": "Flat.",
    }
    _install(monkeypatch, _FakeRun(_lines(flat_tool, flat_result)))
    result = _run("antigravity", tmp_path)
    assert result.text == "Flat."
    assert result.session_id == "flat-1"
    assert result.edited_paths == ("flat.py",)


class _CapturedOnlyRun(_FakeRun):
    """A runner that returns captured stdout without live callbacks."""

    def __call__(self, command, **kwargs):
        kwargs["on_stdout"] = None
        return super().__call__(command, **kwargs)


def test_antigravity_parses_captured_stdout_without_streaming(
    monkeypatch, tmp_path
) -> None:
    _install(
        monkeypatch,
        _CapturedOnlyRun(
            _lines(
                _agy_tool(1, "DONE", target="captured.py"),
                _agy_result("Captured.", "agy-cap"),
            )
        ),
    )
    result = _run("antigravity", tmp_path)
    assert result.text == "Captured."
    assert result.session_id == "agy-cap"
    assert result.edited_paths == ("captured.py",)


def test_antigravity_never_touches_role_session_artifacts(
    monkeypatch, tmp_path
) -> None:
    _install(monkeypatch, _FakeRun(_lines(_agy_result())))
    _run("antigravity", tmp_path, session_id="agy-prior")
    assert not (tmp_path / "logs").exists()


# --- Antigravity edited paths ------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["write_to_file", "replace_file_content", "multi_replace_file_content"]
)
def test_antigravity_edit_tools_counted(monkeypatch, tmp_path, tool) -> None:
    target = tmp_path / "src" / "app.py"
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _agy_tool(2, "ACTIVE", tool, target=str(target)),
                _agy_tool(2, "DONE", tool, target=str(target)),
                _agy_result(),
            )
        ),
    )
    result = _run("antigravity", tmp_path)
    assert result.edited_paths == ("src/app.py",)


def test_antigravity_tool_name_from_tool_info(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _agy_tool(2, "DONE", "write_to_file", target="named.py", tool_name=""),
                _agy_result(),
            )
        ),
    )
    assert _run("antigravity", tmp_path).edited_paths == ("named.py",)


def test_antigravity_refused_failed_incomplete_and_other_steps_not_counted(
    monkeypatch, tmp_path
) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                # Refused: the step ends in ERROR.
                _agy_tool(1, "ACTIVE", target="refused.py"),
                _agy_tool(
                    1,
                    "ERROR",
                    target="refused.py",
                    error="permission check failed: user denied permission",
                ),
                # A later DONE for the refused step does not revive it.
                _agy_tool(1, "DONE", target="refused.py"),
                # Failed: DONE that still carries a tool error.
                _agy_tool(2, "DONE", target="failed.py", error="write failed"),
                # Incomplete: never reaches DONE.
                _agy_tool(3, "ACTIVE", target="pending.py"),
                # Not an edit tool.
                _agy_tool(4, "DONE", "run_command", target="shell.py"),
                _agy_tool(5, "DONE", "view_file", target="read.py"),
                # DONE without a TargetFile anywhere.
                _agy_tool(6, "ACTIVE"),
                _agy_tool(6, "DONE"),
                # Unknown state.
                _agy_tool(7, "WAITING", target="waiting.py"),
                # Counted.
                _agy_tool(8, "DONE", target="ok.py"),
                _agy_result(),
            )
        ),
    )
    result = _run("antigravity", tmp_path)
    assert result.edited_paths == ("ok.py",)


def test_antigravity_paths_duplicates_and_escape_dropped(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _agy_tool(1, "DONE", target=str(tmp_path / "a.py")),
                _agy_tool(1, "DONE", target=str(tmp_path / "a.py")),
                _agy_tool(2, "DONE", "replace_file_content", target="a.py"),
                _agy_tool(None, "DONE", target="no_index.py"),
                _agy_tool(3, "DONE", target=str(tmp_path.parent / "outside.py")),
                _agy_tool(4, "DONE", target="../escape.py"),
                _agy_result(),
            )
        ),
    )
    result = _run("antigravity", tmp_path)
    assert result.edited_paths == ("a.py", "no_index.py")


# Live agy 1.4.2 capture (``agy -p ... --agent backend --output-format
# stream-json --print-timeout 90s --mode accept-edits``, 2026-10-21) of one
# ``write_to_file`` step, three ``replace_file_content`` steps, and one failed
# ``replace_file_content`` step. The lines are verbatim except that the
# capture's temporary directory is replaced by ``{root}``.
AGY_142_EDIT_CAPTURE: tuple[str, ...] = (
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"write_to_file","tool_info":{"name":"write_to_file","parameters":{"TargetFile":"{root}\\created.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":2,"state":"DONE","step_type":"tool","tool_name":"write_to_file","duration_seconds":0.9608998,"tool_info":{"name":"write_to_file","parameters":{"TargetFile":"{root}\\created.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":4,"state":"ACTIVE","step_type":"tool","tool_name":"replace_file_content","tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":4,"state":"DONE","step_type":"tool","tool_name":"replace_file_content","duration_seconds":1.0240573,"tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":6,"state":"ACTIVE","step_type":"tool","tool_name":"replace_file_content","tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":6,"state":"DONE","step_type":"tool","tool_name":"replace_file_content","duration_seconds":1.1206883,"tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":8,"state":"ACTIVE","step_type":"tool","tool_name":"replace_file_content","tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":8,"state":"DONE","step_type":"tool","tool_name":"replace_file_content","duration_seconds":1.932653,"tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":10,"state":"ACTIVE","step_type":"tool","tool_name":"replace_file_content","tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"}}}}',
    r'{"event":"step_update","step_update":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","step_index":10,"state":"ERROR","step_type":"tool","tool_name":"replace_file_content","duration_seconds":1.2514718,"tool_info":{"name":"replace_file_content","parameters":{"TargetFile":"{root}\\sample.txt"},"error":{"type":"TOOL_ERROR","message":"Could not successfully apply any edits. Please review the file and try smaller edits that you are more confident about.: chunk 0: target content not found in file"}}}}',
    r'{"event":"result","result":{"conversation_id":"a9d52ef1-f742-47bc-a4cc-6b2305f0611a","status":"SUCCESS","response":"done\n","duration_seconds":43.9724056,"num_turns":1,"usage":{"input_tokens":20184,"output_tokens":1155,"thinking_tokens":553,"cache_read_tokens":34795,"total_tokens":21339}}}',
)


_AGY_142_ROOT_CHILD = re.compile(r"\{root\}\\\\([A-Za-z0-9_.-]+)")


def _agy_142_capture(
    root: PurePath, lines: tuple[str, ...] = AGY_142_EDIT_CAPTURE
) -> list[str]:
    """Return capture lines with each ``{root}\\\\<name>`` set to ``root / name``.

    The capture was taken on Windows, so its paths join with a backslash. The
    whole child path is rebuilt with ``root``'s own separator and JSON-escaped,
    so ``TargetFile`` is a child of ``root`` on Windows and POSIX alike.
    """

    replaced = [
        _AGY_142_ROOT_CHILD.sub(
            lambda match: json.dumps(str(root / match.group(1)))[1:-1], line
        )
        for line in lines
    ]
    assert not any("{root}" in line for line in replaced)
    return [line + "\n" for line in replaced]


def _agy_142_done_targets(lines: list[str]) -> list[str]:
    return [
        json.loads(line)["step_update"]["tool_info"]["parameters"]["TargetFile"]
        for line in lines[:-1]
        if json.loads(line)["step_update"]["state"] == "DONE"
    ]


def test_agy_142_capture_replays_with_posix_root() -> None:
    # A POSIX root must not leave the capture's Windows backslash in TargetFile.
    root = PurePosixPath("/srv/repo")
    lines = _agy_142_capture(root)
    targets = _agy_142_done_targets(lines)
    assert targets == [
        "/srv/repo/created.txt",
        "/srv/repo/sample.txt",
        "/srv/repo/sample.txt",
        "/srv/repo/sample.txt",
    ]
    assert not any("\\" in target for target in targets)

    tracker = _AntigravityEditTracker()
    for line in lines:
        tracker.consume_stdout(line)
    tracker.finalize()
    assert tracker.paths == targets
    assert [PurePosixPath(path).relative_to(root).as_posix() for path in tracker.paths] == [
        "created.txt",
        "sample.txt",
        "sample.txt",
        "sample.txt",
    ]


def test_agy_142_capture_replays_with_native_root(tmp_path) -> None:
    # The host's own root: on POSIX this is the POSIX-root replay end to end.
    lines = _agy_142_capture(tmp_path)
    targets = _agy_142_done_targets(lines)
    assert targets == [
        str(tmp_path / "created.txt"),
        str(tmp_path / "sample.txt"),
        str(tmp_path / "sample.txt"),
        str(tmp_path / "sample.txt"),
    ]
    if isinstance(tmp_path, PurePosixPath):
        assert not any("\\" in target for target in targets)

    tracker = _AntigravityEditTracker()
    for line in lines:
        tracker.consume_stdout(line)
    tracker.finalize()
    assert tracker.paths == targets
    assert normalize_edited_paths(tracker.paths, repo_root=tmp_path) == (
        "created.txt",
        "sample.txt",
    )


def test_antigravity_live_142_capture_records_done_edits_in_order(
    monkeypatch, tmp_path
) -> None:
    lines = _agy_142_capture(tmp_path)
    # The capture parses as the documented contract before the tracker sees it.
    events = [json.loads(line) for line in lines]
    done = [e["step_update"] for e in events[:-1] if e["step_update"]["state"] == "DONE"]
    assert [step["tool_name"] for step in done] == [
        "write_to_file",
        "replace_file_content",
        "replace_file_content",
        "replace_file_content",
    ]
    assert all("error" not in step["tool_info"] for step in done)
    failed = events[-2]["step_update"]
    assert failed["state"] == "ERROR"
    assert failed["tool_info"]["error"]["type"] == "TOOL_ERROR"

    _install(monkeypatch, _FakeRun(lines))
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 0
    assert result.error is None
    assert result.text == "done"
    assert result.session_id == "a9d52ef1-f742-47bc-a4cc-6b2305f0611a"
    assert result.edited_paths == ("created.txt", "sample.txt")


def test_antigravity_live_142_capture_chunked_stream(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _FakeRun(_chunks(_agy_142_capture(tmp_path), 53)))
    result = _run("antigravity", tmp_path)
    assert result.edited_paths == ("created.txt", "sample.txt")


def test_antigravity_live_142_capture_error_only_file_not_counted(
    monkeypatch, tmp_path
) -> None:
    # The step 10 ACTIVE + ERROR pair names a file no successful step touched.
    lines = list(AGY_142_EDIT_CAPTURE)
    for index in (8, 9):
        assert '"step_index":10' in lines[index]
        lines[index] = lines[index].replace(r"{root}\\sample.txt", r"{root}\\rejected.txt")
    _install(monkeypatch, _FakeRun(_agy_142_capture(tmp_path, tuple(lines))))
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 0
    assert result.edited_paths == ("created.txt", "sample.txt")


def test_antigravity_live_142_capture_error_step_alone_records_nothing(
    monkeypatch, tmp_path
) -> None:
    # Only the failed step 10 pair and the result: no edit is recorded.
    lines = AGY_142_EDIT_CAPTURE[8:]
    _install(monkeypatch, _FakeRun(_agy_142_capture(tmp_path, lines)))
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 0
    assert result.text == "done"
    assert result.edited_paths == ()


def test_antigravity_edits_before_failure_are_kept(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _agy_tool(1, "ACTIVE", target="new.py"),
                _agy_tool(1, "DONE"),
                _agy_tool(2, "ACTIVE", target="cut.py"),
            ),
            exit_code=2,
            stderr="agy: crashed",
        ),
    )
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 2
    assert result.text == ""
    assert result.error == "Antigravity exited with code 2. agy: crashed"
    assert result.edited_paths == ("new.py",)


# --- Antigravity errors ------------------------------------------------------


def test_antigravity_non_success_result_is_an_error(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(
            _lines(
                _agy_tool(1, "DONE", target="kept.py"),
                _agy("result", status="ERROR", error="quota exceeded", response=""),
            )
        ),
    )
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 1
    assert result.error == (
        "Antigravity exited with code 1. "
        "Antigravity result status=ERROR: quota exceeded"
    )
    assert result.edited_paths == ("kept.py",)


def test_antigravity_missing_result_is_an_error(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(_lines(_agy_step(step_type="agent_response", text_delta="partial"))),
    )
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 1
    assert result.text == "partial"
    assert result.error is not None
    assert result.error.startswith("Antigravity exited with code 1. ")
    assert "no result event" in result.error


def test_antigravity_no_answer_text_error(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _FakeRun(_lines(_agy_result("   "))))
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 0
    assert result.text == ""
    assert result.error == "Antigravity produced no answer text."


AGY_SOFT_DENY = (
    'jetski: no output produced - a tool required the "command" permission '
    "that headless mode cannot prompt for, so it was auto-denied. Add an "
    "allow-rule under permissions.allow in settings.json (e.g. "
    "command(<target>))."
)


def test_antigravity_soft_deny_is_an_error(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun(_lines(_agy_result("I could not run it.")), stderr=AGY_SOFT_DENY),
    )
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 1
    assert result.text == "I could not run it."
    assert result.error is not None
    assert result.error.startswith("Antigravity exited with code 1. ")
    assert "permissions.allow" in result.error


def test_antigravity_deny_rule_is_an_error_without_command_text(
    monkeypatch, tmp_path
) -> None:
    secret_command = "git push https://user:s3cr3t-T0KEN@example.com/repo.git"
    deny = _agy_step(
        step_index=2,
        state="ERROR",
        step_type="tool",
        tool_name="run_command",
        tool_info={
            "name": "run_command",
            "parameters": {"CommandLine": secret_command},
            "error": {
                "type": "TOOL_ERROR",
                "message": f'permission check failed for command "{secret_command}": '
                "Matches user-configured deny rule.",
            },
        },
    )
    _install(monkeypatch, _FakeRun(_lines(deny, _agy_result("Push refused."))))
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 1
    assert result.error is not None
    assert "Antigravity deny rule refused a command at step 2" in result.error
    assert "s3cr3t-T0KEN" not in result.error
    assert "git push" not in result.error


def test_antigravity_sign_in_failure_is_an_error(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        _FakeRun([], exit_code=1, stderr="Error: authentication required"),
    )
    result = _run("antigravity", tmp_path)
    assert result.exit_code == 1
    assert result.error is not None
    assert "agy is not authenticated" in result.error


def test_antigravity_error_detail_is_redacted_and_capped(
    monkeypatch, tmp_path
) -> None:
    # Built at runtime so secret scanners do not flag a literal token.
    planted = "ghp_" + "Ab3dE" * 7 + "F"
    _install(
        monkeypatch,
        _FakeRun(
            [],
            exit_code=3,
            stderr="token=" + planted + " " + "x" * 500,
        ),
    )
    result = _run("antigravity", tmp_path)
    prefix = "Antigravity exited with code 3. "
    assert result.error is not None
    assert result.error.startswith(prefix)
    assert len(result.error) - len(prefix) <= 200
    assert planted not in result.error
    assert planted[:14] not in result.error


def test_antigravity_failed_resume_is_not_retried_fresh(monkeypatch, tmp_path) -> None:
    fake = _install(
        monkeypatch, _FakeRun([], exit_code=1, stderr="conversation not found")
    )
    result = _run("antigravity", tmp_path, session_id="agy-gone")
    assert len(fake.calls) == 1
    assert result.exit_code == 1
    assert result.session_id == "agy-gone"
