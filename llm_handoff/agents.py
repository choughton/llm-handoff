from __future__ import annotations

# Compatibility facade. Runtime code should prefer the layered modules below:
# agent_types -> agent_process/agent_streams -> agent_providers -> agent_roles.

import shutil
import subprocess
import time

from llm_handoff import agent_process as _process
from llm_handoff import agent_streams as _streams
from llm_handoff import agent_types as _types
from llm_handoff import agent_roles as _roles
from llm_handoff.agent_providers import claude as _claude
from llm_handoff.agent_providers import codex as _codex
from llm_handoff.agent_providers import gemini as _gemini
from llm_handoff.agent_providers import manual as _manual

_EXPORTED_MODULES = (shutil, subprocess, time)

DispatchResult = _types.DispatchResult
LogFn = _types.LogFn
SubagentResult = _types.SubagentResult
_ProcessResult = _types._ProcessResult

StderrMode = _streams.StderrMode
_LiveAgentStreamState = _streams._LiveAgentStreamState
_LiveAgentOutputMonitor = _streams._LiveAgentOutputMonitor
_filter_live_agent_stdout_line = _streams._filter_live_agent_stdout_line
_filter_codex_stderr_line = _streams._filter_codex_stderr_line
_filter_codex_json_stderr_line = _streams._filter_codex_json_stderr_line
_trim_trailing_diff_text = _streams._trim_trailing_diff_text
_looks_like_diff_line = _streams._looks_like_diff_line
_should_emit_live_agent_stdout_line = _streams._should_emit_live_agent_stdout_line

_append_agent_additional_instruction = _process._append_agent_additional_instruction
_coerce_output = _process._coerce_output
_drain_stream_queue = _process._drain_stream_queue
_first_nonempty_line = _process._first_nonempty_line
_first_nonempty_value = _process._first_nonempty_value
_format_process_failure_detail = _process._format_process_failure_detail
_read_process_stream = _process._read_process_stream
_resolve_command_binary = _process._resolve_command_binary
_resolve_handoff_path = _process._resolve_handoff_path
_run_command = _process._run_command
_run_command_streaming = _process._run_command_streaming
_wait_for_manual_continue = _process._wait_for_manual_continue

GeminiRole = _gemini.GeminiRole
RateLimitLastBlockPolicy = _gemini.RateLimitLastBlockPolicy
_GeminiRateLimitEvent = _gemini._GeminiRateLimitEvent
_GeminiRateLimitBlock = _gemini._GeminiRateLimitBlock
_GeminiRateLimitStreamState = _gemini._GeminiRateLimitStreamState
_GeminiLiveRateLimitMonitor = _gemini._GeminiLiveRateLimitMonitor
_GeminiStreamJsonMonitor = _gemini._GeminiStreamJsonMonitor
_build_gemini_command = _gemini._build_gemini_command
_build_gemini_prompt = _gemini._build_gemini_prompt
_run_gemini_with_retries = _gemini._run_gemini_with_retries
_run_tenacity_with_real_clock = _gemini._run_tenacity_with_real_clock
_should_retry_gemini_result = _gemini._should_retry_gemini_result
_log_gemini_retry_wait = _gemini._log_gemini_retry_wait
_return_last_gemini_retry_result = _gemini._return_last_gemini_retry_result
_is_gemini_resume_recoverable_failure = _gemini._is_gemini_resume_recoverable_failure
_gemini_stream_json_unsupported = _gemini._gemini_stream_json_unsupported
_parse_gemini_stream_json_line = _gemini._parse_gemini_stream_json_line
_extract_gemini_stream_text = _gemini._extract_gemini_stream_text
_format_gemini_tool_use_label = _gemini._format_gemini_tool_use_label
_format_gemini_result_tokens = _gemini._format_gemini_result_tokens
_build_gemini_env = _gemini._build_gemini_env
_log_gemini_failure = _gemini._log_gemini_failure
_extract_gemini_rate_limit_events = _gemini._extract_gemini_rate_limit_events
_extract_ready_gemini_rate_limit_blocks = (
    _gemini._extract_ready_gemini_rate_limit_blocks
)
_extract_completed_gemini_rate_limit_blocks = (
    _gemini._extract_completed_gemini_rate_limit_blocks
)
_extract_gemini_rate_limit_blocks = _gemini._extract_gemini_rate_limit_blocks
_build_gemini_rate_limit_event = _gemini._build_gemini_rate_limit_event
_event_has_full_metadata = _gemini._event_has_full_metadata
_log_live_gemini_rate_limit_event = _gemini._log_live_gemini_rate_limit_event
_log_gemini_rate_limit_block_detail = _gemini._log_gemini_rate_limit_block_detail
_log_gemini_rate_limit_events = _gemini._log_gemini_rate_limit_events
_format_gemini_rate_limit_metadata = _gemini._format_gemini_rate_limit_metadata
_format_gemini_rate_limit_detail = _gemini._format_gemini_rate_limit_detail
_gemini_failure_text = _gemini._gemini_failure_text
_last_match = _gemini._last_match
_text_has_rate_limit_markers = _gemini._text_has_rate_limit_markers
_command_requests_gemini_stream_json = _gemini._command_requests_gemini_stream_json
_gemini_agent_name_from_command = _gemini._gemini_agent_name_from_command
_strip_gemini_stream_json_args = _gemini._strip_gemini_stream_json_args

_CodexCommandResult = _codex._CodexCommandResult
_CodexArtifactPaths = _codex._CodexArtifactPaths
_CodexJsonMonitor = _codex._CodexJsonMonitor
_build_codex_exec_command = _codex._build_codex_exec_command
_build_codex_resume_command = _codex._build_codex_resume_command
_build_codex_stateless_prompt = _codex._build_codex_stateless_prompt
_build_codex_managed_bootstrap_prompt = _codex._build_codex_managed_bootstrap_prompt
_build_codex_resume_prompt = _codex._build_codex_resume_prompt
_codex_artifact_paths = _codex._codex_artifact_paths
_cleanup_codex_output_artifacts = _codex._cleanup_codex_output_artifacts
_read_codex_session_state = _codex._read_codex_session_state
_write_codex_session_state = _codex._write_codex_session_state
_clear_codex_session_state = _codex._clear_codex_session_state
_is_codex_resume_recoverable_failure = _codex._is_codex_resume_recoverable_failure
_read_codex_output_last_message = _codex._read_codex_output_last_message
_parse_codex_structured_message = _codex._parse_codex_structured_message

_ClaudeStreamJsonMonitor = _claude._ClaudeStreamJsonMonitor
_claude_stream_json_unsupported = _claude._claude_stream_json_unsupported
_extract_claude_text_fragment = _claude._extract_claude_text_fragment
_extract_claude_tool_result_text = _claude._extract_claude_tool_result_text
_format_claude_tool_use_label = _claude._format_claude_tool_use_label
_coerce_claude_usage_value = _claude._coerce_claude_usage_value
_build_claude_env = _claude._build_claude_env

invoke_backend_role = _roles.invoke_backend_role
invoke_planner_role = _roles.invoke_planner_role
invoke_frontend_role = _roles.invoke_frontend_role
invoke_support_role = _roles.invoke_support_role

_ORIGINAL_GEMINI_RUN_COMMAND = _gemini._run_gemini_command
_ORIGINAL_GEMINI_RUN_STREAM_JSON_COMMAND = _gemini._run_gemini_stream_json_command
_ORIGINAL_GEMINI_RUN_PLAIN_TEXT_COMMAND = _gemini._run_gemini_plain_text_command
_ORIGINAL_CODEX_RUN_JSON_COMMAND = _codex._run_codex_json_command
_ORIGINAL_CLAUDE_RUN_STREAM_JSON_COMMAND = _claude._run_claude_stream_json_command
_ORIGINAL_PROCESS_RUN_LOGGED_AGENT_COMMAND = _process._run_logged_agent_command


def _selected(global_name: str, facade_func: object, original_func: object) -> object:
    value = globals()[global_name]
    return original_func if value is facade_func else value


def _sync_process() -> None:
    _process._run_command_streaming = globals()["_run_command_streaming"]


def _sync_gemini() -> None:
    _sync_process()
    _gemini._run_command_streaming = globals()["_run_command_streaming"]
    _gemini._run_gemini_command = _selected(
        "_run_gemini_command",
        _facade_run_gemini_command,
        _ORIGINAL_GEMINI_RUN_COMMAND,
    )
    _gemini._run_gemini_stream_json_command = _selected(
        "_run_gemini_stream_json_command",
        _facade_run_gemini_stream_json_command,
        _ORIGINAL_GEMINI_RUN_STREAM_JSON_COMMAND,
    )
    _gemini._run_gemini_plain_text_command = _selected(
        "_run_gemini_plain_text_command",
        _facade_run_gemini_plain_text_command,
        _ORIGINAL_GEMINI_RUN_PLAIN_TEXT_COMMAND,
    )


def _sync_codex() -> None:
    _sync_process()
    _codex._run_command_streaming = globals()["_run_command_streaming"]
    _codex._run_codex_json_command = _selected(
        "_run_codex_json_command",
        _facade_run_codex_json_command,
        _ORIGINAL_CODEX_RUN_JSON_COMMAND,
    )
    _codex._cleanup_codex_output_artifacts = globals()[
        "_cleanup_codex_output_artifacts"
    ]
    _codex._read_codex_session_state = globals()["_read_codex_session_state"]
    _codex._write_codex_session_state = globals()["_write_codex_session_state"]
    _codex._clear_codex_session_state = globals()["_clear_codex_session_state"]


def _sync_claude() -> None:
    _sync_process()
    _claude._run_command_streaming = globals()["_run_command_streaming"]
    _claude._run_logged_agent_command = globals()["_run_logged_agent_command"]
    _claude._run_claude_stream_json_command = _selected(
        "_run_claude_stream_json_command",
        _facade_run_claude_stream_json_command,
        _ORIGINAL_CLAUDE_RUN_STREAM_JSON_COMMAND,
    )


def _sync_manual() -> None:
    _manual._wait_for_manual_continue = globals()["_wait_for_manual_continue"]


def invoke_gemini(*args, **kwargs):
    _sync_gemini()
    return _gemini.invoke_gemini(*args, **kwargs)


def _facade_run_gemini_command(*args, **kwargs):
    _sync_gemini()
    return _ORIGINAL_GEMINI_RUN_COMMAND(*args, **kwargs)


def _facade_run_gemini_stream_json_command(*args, **kwargs):
    _sync_gemini()
    return _ORIGINAL_GEMINI_RUN_STREAM_JSON_COMMAND(*args, **kwargs)


def _facade_run_gemini_plain_text_command(*args, **kwargs):
    _sync_gemini()
    return _ORIGINAL_GEMINI_RUN_PLAIN_TEXT_COMMAND(*args, **kwargs)


def invoke_codex(*args, **kwargs):
    _sync_codex()
    return _codex.invoke_codex(*args, **kwargs)


def _facade_run_codex_json_command(*args, **kwargs):
    _sync_codex()
    return _ORIGINAL_CODEX_RUN_JSON_COMMAND(*args, **kwargs)


def invoke_manual_frontend(*args, **kwargs):
    _sync_manual()
    return _manual.invoke_manual_frontend(*args, **kwargs)


def invoke_claude_subagent(*args, **kwargs):
    _sync_claude()
    return _claude.invoke_claude_subagent(*args, **kwargs)


def _facade_run_claude_stream_json_command(*args, **kwargs):
    _sync_claude()
    return _ORIGINAL_CLAUDE_RUN_STREAM_JSON_COMMAND(*args, **kwargs)


def _facade_run_logged_agent_command(*args, **kwargs):
    _sync_process()
    return _ORIGINAL_PROCESS_RUN_LOGGED_AGENT_COMMAND(*args, **kwargs)


_run_gemini_command = _facade_run_gemini_command
_run_gemini_stream_json_command = _facade_run_gemini_stream_json_command
_run_gemini_plain_text_command = _facade_run_gemini_plain_text_command
_run_codex_json_command = _facade_run_codex_json_command
_run_claude_stream_json_command = _facade_run_claude_stream_json_command
_run_logged_agent_command = _facade_run_logged_agent_command
