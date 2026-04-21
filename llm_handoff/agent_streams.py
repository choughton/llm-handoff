from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

from llm_handoff.agent_types import LogFn


StderrMode = Literal["warn", "codex"]
_LIVE_AGENT_TOOL_LABELS = frozenset(
    {
        "apply_patch",
        "bash",
        "cat",
        "claude",
        "codex",
        "edit",
        "exec",
        "find",
        "git",
        "grep",
        "ls",
        "open",
        "pwsh",
        "python",
        "read",
        "rg",
        "search",
        "view",
        "write",
    }
)
_DIFF_BLOCK_START_RE = re.compile(r"^diff --git\b")
_DIFF_BLOCK_LINE_RE = re.compile(
    r"^(?:diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|@@ |--- |\+\+\+ |"
    r"new file mode |deleted file mode |similarity index |rename from |"
    r"rename to |old mode |new mode |Binary files )"
)
_COMMAND_RESULT_RE = re.compile(r"\b(?:succeeded|failed|timed out|completed) in \d")
_COMMAND_LINE_RE = re.compile(r'^(?:"[^"]+"|[A-Za-z]:\\|/).+\bin\s+\S')
_STRUCTURED_STATUS_RE = re.compile(
    r"^(?:VALID|ROUTE|CONFIDENCE|SOURCE|REASONING|WARNINGS|SUMMARY|CHECKS|"
    r"AUDIT SHA|COMMIT SHA|PUSH|VERDICT|STATUS|ERROR|FAILED|PASS)\b"
)
_MARKDOWN_STATUS_RE = re.compile(r"^(?:- (?:\*\*|[A-Z0-9])|\d+\.\s)")
_PROSE_LINE_RE = re.compile(r"^(?:\*\*|[A-Z0-9][^\n]{2,})$")
_CODEX_STDERR_METADATA_RE = re.compile(
    r"^(?:--------$|workdir:|model:|provider:|approval:|sandbox:|reasoning effort:|"
    r"reasoning summaries:|session id:|deprecated:)"
)
_CODEX_STDERR_ERROR_RE = re.compile(
    r"^(?:error:|fatal:|panic:|traceback\b|exception\b)",
    re.IGNORECASE,
)


@dataclass
class _LiveAgentStreamState:
    in_diff_block: bool = False
    awaiting_tool_descriptor: bool = False
    suppress_tool_output: bool = False


@dataclass
class _LiveAgentOutputMonitor:
    agent_name: str
    log: LogFn | None
    stream_all_stdout: bool = False
    stderr_mode: StderrMode = "warn"
    _stdout_state: _LiveAgentStreamState = field(default_factory=_LiveAgentStreamState)
    _stderr_state: _LiveAgentStreamState = field(default_factory=_LiveAgentStreamState)

    def consume(self, stream_name: str, chunk: str) -> None:
        if self.log is None or not chunk:
            return

        for raw_line in chunk.splitlines():
            self._consume_line(stream_name, raw_line.rstrip("\r"))

    def _consume_line(self, stream_name: str, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        if stream_name == "stderr":
            if self.stderr_mode == "codex":
                filtered = _filter_codex_stderr_line(
                    stripped,
                    state=self._stderr_state,
                )
                if filtered is None:
                    return

                level, message = filtered
                if level == "AGENT":
                    self.log("AGENT", f"{self.agent_name}: {message}")
                else:
                    self.log("WARN", f"{self.agent_name} stderr: {message}")
                return

            self.log("WARN", f"{self.agent_name} stderr: {stripped}")
            return

        filtered_line = _filter_live_agent_stdout_line(
            stripped,
            state=self._stdout_state,
            stream_all_stdout=self.stream_all_stdout,
        )
        if filtered_line is not None:
            self.log("AGENT", f"{self.agent_name}: {filtered_line}")


def _filter_live_agent_stdout_line(
    line: str,
    *,
    state: _LiveAgentStreamState,
    stream_all_stdout: bool = False,
) -> str | None:
    normalized_line = _trim_trailing_diff_text(line.strip())
    if not normalized_line:
        return None

    if state.in_diff_block:
        if _should_emit_live_agent_stdout_line(normalized_line):
            state.in_diff_block = False
            return normalized_line
        if not _looks_like_diff_line(normalized_line):
            state.in_diff_block = False
        return None

    if _looks_like_diff_line(normalized_line):
        state.in_diff_block = True
        return None

    if stream_all_stdout:
        return normalized_line

    if _should_emit_live_agent_stdout_line(normalized_line):
        return normalized_line
    return None


def _filter_codex_stderr_line(
    line: str,
    *,
    state: _LiveAgentStreamState,
) -> tuple[Literal["AGENT", "WARN"], str] | None:
    stripped = _trim_trailing_diff_text(line.strip())
    if not stripped:
        return None

    if stripped == "user" or stripped in _LIVE_AGENT_TOOL_LABELS:
        state.awaiting_tool_descriptor = stripped not in {"codex", "user"}
        state.suppress_tool_output = False
        return ("AGENT", stripped)

    if state.awaiting_tool_descriptor:
        state.awaiting_tool_descriptor = False
        state.suppress_tool_output = True
        return ("AGENT", stripped)

    if state.suppress_tool_output:
        if _COMMAND_RESULT_RE.search(stripped):
            state.suppress_tool_output = False
            return ("AGENT", stripped)
        return None

    if _CODEX_STDERR_METADATA_RE.match(stripped):
        return ("AGENT", stripped)

    if _CODEX_STDERR_ERROR_RE.match(stripped):
        return ("WARN", stripped)

    filtered_line = _filter_live_agent_stdout_line(
        stripped,
        state=state,
        stream_all_stdout=False,
    )
    if filtered_line is not None:
        return ("AGENT", filtered_line)

    return None


def _filter_codex_json_stderr_line(
    line: str,
    *,
    state: _LiveAgentStreamState,
) -> tuple[Literal["AGENT", "WARN"], str] | None:
    stripped = _trim_trailing_diff_text(line.strip())
    if not stripped:
        return None

    filtered_line = _filter_live_agent_stdout_line(
        stripped,
        state=state,
        stream_all_stdout=True,
    )
    if filtered_line is None:
        return None

    if _CODEX_STDERR_ERROR_RE.match(filtered_line):
        return ("WARN", filtered_line)
    return ("AGENT", filtered_line)


def _trim_trailing_diff_text(line: str) -> str:
    diff_index = line.find(" diff --git ")
    if diff_index == -1:
        return line
    return line[:diff_index].rstrip()


def _looks_like_diff_line(line: str) -> bool:
    stripped = line.strip()
    if _DIFF_BLOCK_START_RE.match(stripped):
        return True
    return _DIFF_BLOCK_LINE_RE.match(stripped) is not None


def _should_emit_live_agent_stdout_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.lower() in _LIVE_AGENT_TOOL_LABELS:
        return True
    if _COMMAND_LINE_RE.match(stripped):
        return True
    if _COMMAND_RESULT_RE.search(stripped):
        return True
    if _STRUCTURED_STATUS_RE.match(stripped):
        return True
    if _MARKDOWN_STATUS_RE.match(stripped):
        return True
    return _PROSE_LINE_RE.match(stripped) is not None
