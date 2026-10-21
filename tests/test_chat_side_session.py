from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_handoff import chat_side_session, config
from llm_handoff.agent_providers.claude import claude_permission_args
from llm_handoff.agent_types import _ProcessResult
from llm_handoff.chat_side_session import (
    SUPPORTED_CHAT_PROVIDERS,
    ChatSessionResult,
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
    assert SUPPORTED_CHAT_PROVIDERS == ("claude", "codex")


def test_result_is_frozen_dataclass() -> None:
    result = ChatSessionResult(
        text="", session_id=None, exit_code=0, error=None, edited_paths=()
    )
    with pytest.raises(Exception):
        result.text = "x"  # type: ignore[misc]


def test_unsupported_provider_raises_before_launch(monkeypatch, tmp_path) -> None:
    fake = _install(monkeypatch, _FakeRun([]))
    with pytest.raises(ValueError) as excinfo:
        _run("antigravity", tmp_path)
    assert "antigravity" in str(excinfo.value)
    assert "only claude and codex" in str(excinfo.value)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("provider", "lines"),
    [
        ("claude", _lines(_claude_init(), _claude_result())),
        ("codex", _lines(_codex_thread(), _codex_message())),
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


@pytest.mark.parametrize("provider", ["claude", "codex"])
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
