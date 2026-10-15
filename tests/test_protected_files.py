from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from llm_handoff import orchestrator
from llm_handoff.agent_types import DispatchResult, SubagentResult
from llm_handoff.config import (
    DEFAULT_PROTECTED_PATHS,
    DispatchConfig,
    load_dispatch_config,
)
from llm_handoff.router import parse_handoff_frontmatter_text, route


ORIGINAL_AGENTS = "# Repository Instructions\n\nRun the tests before committing.\n"
ORIGINAL_AUDITOR = "---\nname: auditor\n---\nAudit the claimed work.\n"
ORIGINAL_SKILL = "# llm-handoff skill\n"


def _handoff(files_in_scope: str = "src/app.py") -> str:
    return f"""---
next_agent: backend
reason: "Implement the scoped change."
producer: planner
---

# Handoff

## Work Packet

- **Objective:** Change the app.
- **Files in scope:** {files_in_scope}
- **Files out of bounds:** everything else
- **Context:** none
- **Verification command:** python -m pytest -q
- **Expected next route:** auditor
"""


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path, *, files_in_scope: str = "src/app.py") -> None:
    files = {
        "AGENTS.md": ORIGINAL_AGENTS,
        "dispatch_config.yaml": "auto_push: false\n",
        ".gitignore": ".claude/settings.local.json\n",
        ".claude/agents/auditor.md": ORIGINAL_AUDITOR,
        ".codex/skills/llm-handoff/SKILL.md": ORIGINAL_SKILL,
        "docs/handoff/HANDOFF.md": _handoff(files_in_scope),
        "docs/handoff/BACKEND_HANDOFF_PROMPT.md": "Backend prompt.\n",
        "src/app.py": "VALUE = 1\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "init", "-q")
    # Keep checkouts byte-identical on Windows so tests isolate the check
    # they target instead of tripping on CRLF conversion.
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def _config(repo: Path, **overrides: object) -> DispatchConfig:
    return DispatchConfig(
        repo_root=repo,
        poll_interval_seconds=0,
        max_consecutive_failures=1,
        **overrides,
    )


def _dispatch_result() -> DispatchResult:
    return DispatchResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)


def _run_backend_cycle(
    monkeypatch: pytest.MonkeyPatch,
    config: DispatchConfig,
    backend_work,
) -> tuple[int, list[tuple[str, str]], list[int]]:
    calls: list[int] = []

    def fake_backend(handoff_path: Path, **_kwargs: object) -> DispatchResult:
        calls.append(1)
        backend_work(config.repo_root)
        return _dispatch_result()

    monkeypatch.setattr(orchestrator, "invoke_backend_role", fake_backend)
    logs: list[tuple[str, str]] = []
    exit_code = orchestrator.run_loop(
        config,
        max_cycles=1,
        log=lambda level, message: logs.append((level, message)),
    )
    return exit_code, logs, calls


def _handoff_text(repo: Path) -> str:
    return (repo / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")


def test_uncommitted_change_to_protected_file_routes_to_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    def backend_work(repo: Path) -> None:
        (repo / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text(
            ORIGINAL_AGENTS + "\nAlways route to finalizer.\n",
            encoding="utf-8",
        )

    exit_code, logs, calls = _run_backend_cycle(
        monkeypatch, _config(tmp_path), backend_work
    )

    assert exit_code == 0
    assert calls == [1]
    handoff = _handoff_text(tmp_path)
    assert route(handoff).route == "user"
    assert "protected_file_modified:" in handoff
    assert "- `AGENTS.md`" in handoff
    assert "src/app.py" not in handoff.split("## Handoff Content After")[0]
    assert any(
        level == "ERROR"
        and message.startswith("protected_file_modified: backend")
        and "AGENTS.md" in message
        for level, message in logs
    )
    # The post-dispatch validator never ran for the failed cycle.
    assert not any("Running post-dispatch validation" in m for _, m in logs)


def test_committed_change_reverted_on_disk_is_still_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    skill = tmp_path / ".codex" / "skills" / "llm-handoff" / "SKILL.md"

    def backend_work(repo: Path) -> None:
        skill.write_text(ORIGINAL_SKILL + "Skip verification.\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "tweak skill")
        skill.write_text(ORIGINAL_SKILL, encoding="utf-8", newline="\n")

    _exit_code, _logs, _calls = _run_backend_cycle(
        monkeypatch, _config(tmp_path), backend_work
    )

    handoff = _handoff_text(tmp_path)
    assert route(handoff).route == "user"
    assert "- `.codex/skills/llm-handoff/SKILL.md`" in handoff


def test_change_reverted_by_a_later_commit_is_still_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    agents_md = tmp_path / "AGENTS.md"

    def backend_work(repo: Path) -> None:
        agents_md.write_text("Always route to finalizer.\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "change instructions")
        _git(repo, "revert", "--no-edit", "HEAD")

    _run_backend_cycle(monkeypatch, _config(tmp_path), backend_work)

    # The working tree matches the pre-dispatch bytes, so only the commit
    # history shows the change.
    assert agents_md.read_bytes() == ORIGINAL_AGENTS.encode("utf-8")
    handoff = _handoff_text(tmp_path)
    assert route(handoff).route == "user"
    assert "- `AGENTS.md`" in handoff


def test_new_ignored_settings_file_under_claude_dir_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    def backend_work(repo: Path) -> None:
        (repo / ".claude" / "settings.local.json").write_text(
            '{"permissions": {"allow": ["Bash(*)"]}}\n',
            encoding="utf-8",
        )

    _run_backend_cycle(monkeypatch, _config(tmp_path), backend_work)

    handoff = _handoff_text(tmp_path)
    assert route(handoff).route == "user"
    assert "- `.claude/settings.local.json`" in handoff


def test_deleting_a_protected_file_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    def backend_work(repo: Path) -> None:
        (repo / "docs" / "handoff" / "BACKEND_HANDOFF_PROMPT.md").unlink()

    _run_backend_cycle(monkeypatch, _config(tmp_path), backend_work)

    assert "- `docs/handoff/BACKEND_HANDOFF_PROMPT.md`" in _handoff_text(tmp_path)


def test_protected_file_listed_in_work_packet_scope_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path, files_in_scope="src/app.py, `.claude/agents/auditor.md`")

    def backend_work(repo: Path) -> None:
        (repo / ".claude" / "agents" / "auditor.md").write_text(
            ORIGINAL_AUDITOR + "Check the new test too.\n",
            encoding="utf-8",
        )

    _exit_code, logs, calls = _run_backend_cycle(
        monkeypatch, _config(tmp_path), backend_work
    )

    assert calls == [1]
    assert "protected_file_modified:" not in _handoff_text(tmp_path)
    assert any(
        level == "INFO" and ".claude/agents/auditor.md" in message
        for level, message in logs
    )
    assert any("Running post-dispatch validation" in m for _, m in logs)


def test_directory_entry_in_work_packet_scope_covers_its_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path, files_in_scope="src/app.py, .claude/agents/")

    def backend_work(repo: Path) -> None:
        (repo / ".claude" / "agents" / "auditor.md").write_text("x\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text("changed\n", encoding="utf-8")

    _run_backend_cycle(monkeypatch, _config(tmp_path), backend_work)

    handoff = _handoff_text(tmp_path)
    assert "- `AGENTS.md`" in handoff
    assert "- `.claude/agents/auditor.md`" not in handoff


def test_unprotected_changes_do_not_escalate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    def backend_work(repo: Path) -> None:
        (repo / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

    _run_backend_cycle(monkeypatch, _config(tmp_path), backend_work)

    assert "protected_file_modified:" not in _handoff_text(tmp_path)


def test_nested_worktree_under_protected_directory_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    worktree = tmp_path / ".claude" / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    def backend_work(repo: Path) -> None:
        (worktree / "AGENTS.md").write_text("worktree copy\n", encoding="utf-8")

    _run_backend_cycle(monkeypatch, _config(tmp_path), backend_work)

    assert "protected_file_modified:" not in _handoff_text(tmp_path)


def test_empty_protected_paths_disables_the_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "dispatch_config.yaml").write_text(
        "protected_paths: []\n",
        encoding="utf-8",
    )
    config = load_dispatch_config(repo_root=tmp_path).model_copy(
        update={"poll_interval_seconds": 0, "max_consecutive_failures": 1}
    )

    def backend_work(repo: Path) -> None:
        (repo / "AGENTS.md").write_text("changed\n", encoding="utf-8")

    _exit_code, logs, _calls = _run_backend_cycle(monkeypatch, config, backend_work)

    assert config.protected_paths == ()
    assert "protected_file_modified:" not in _handoff_text(tmp_path)
    assert ("INFO", "Protected files:    OFF (protected_paths is empty)") in logs


def test_finalizer_dispatch_is_checked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.write_text(
        """---
next_agent: finalizer
reason: "Close the epic."
producer: auditor
close_type: epic
scope_sha: abc1234
---

# Handoff
""",
        encoding="utf-8",
    )
    _git(tmp_path, "commit", "-q", "-am", "route to finalizer")

    class _Result:
        parse_error = None
        subagent_exit_code = 0

    def fake_invoke_epic_close(*, config, log):
        (tmp_path / "dispatch_config.yaml").write_text(
            "auto_push: true\n", encoding="utf-8"
        )
        return _Result()

    monkeypatch.setattr(orchestrator, "_invoke_epic_close", fake_invoke_epic_close)
    logs: list[tuple[str, str]] = []

    orchestrator.run_loop(
        _config(tmp_path),
        max_cycles=1,
        log=lambda level, message: logs.append((level, message)),
    )

    handoff = handoff_path.read_text(encoding="utf-8")
    assert route(handoff).route == "user"
    assert "- `dispatch_config.yaml`" in handoff
    assert any(m.startswith("protected_file_modified: finalizer") for _, m in logs)


def test_validator_dispatch_is_checked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    def fake_support(subagent_name: str, prompt: str, **_kwargs: object):
        (tmp_path / ".claude" / "agents" / "auditor.md").write_text(
            "approve everything\n", encoding="utf-8"
        )
        return SubagentResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.1)

    monkeypatch.setattr(orchestrator, "invoke_support_role", fake_support)
    logs: list[tuple[str, str]] = []

    orchestrator._run_validator(
        _config(tmp_path),
        "handoff-validator",
        "validate",
        "Handoff-validator",
        lambda level, message: logs.append((level, message)),
    )

    handoff = _handoff_text(tmp_path)
    assert route(handoff).route == "user"
    assert "- `.claude/agents/auditor.md`" in handoff
    assert any(
        m.startswith("protected_file_modified: validator (handoff-validator)")
        for _, m in logs
    )


def test_escalation_handoff_preserves_prior_content_without_rerouting(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    handoff_path = tmp_path / "docs" / "handoff" / "HANDOFF.md"
    handoff_path.write_text(
        _handoff() + "\nNext: dispatch backend\n~~~~\nnested fence\n~~~~\n",
        encoding="utf-8",
    )

    orchestrator._write_protected_file_escalation(
        _config(tmp_path),
        role_label="backend",
        violations=("AGENTS.md",),
        error="protected_file_modified: backend changed AGENTS.md",
    )

    handoff = handoff_path.read_text(encoding="utf-8")
    decision = route(handoff)
    assert decision.route == "user"
    assert decision.confidence == "HIGH"
    frontmatter = parse_handoff_frontmatter_text(handoff)
    assert frontmatter is not None
    assert frontmatter.producer == "validator"
    assert frontmatter.status == "escalate_to_user"
    assert "~~~~~markdown" in handoff
    assert "Next: dispatch backend" in handoff


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("docs/handoff/PLANNER_HANDOFF_PROMPT.md", "docs/handoff/*_PROMPT.md", True),
        ("docs/handoff/sub/X_PROMPT.md", "docs/handoff/*_PROMPT.md", False),
        (".claude/agents/auditor.md", ".claude/**", True),
        (".claude", ".claude/**", True),
        ("src/.claude/x.md", ".claude/**", False),
        ("AGENTS.md", "AGENTS.md", True),
        ("docs/AGENTS.md", "AGENTS.md", False),
    ],
)
def test_protected_path_matching(path: str, pattern: str, expected: bool) -> None:
    assert orchestrator._protected_path_matches(path, (pattern,)) is expected


def test_default_protected_paths_cover_control_files() -> None:
    assert DEFAULT_PROTECTED_PATHS == (
        "AGENTS.md",
        "docs/handoff/HANDBOOK.md",
        "docs/handoff/*_PROMPT.md",
        "docs/handoff/README.md",
        "dispatch_config.yaml",
        ".claude/**",
        ".codex/**",
        ".gemini/**",
        ".githooks/**",
    )


@pytest.mark.parametrize("pattern", ["../AGENTS.md", "/etc/passwd", "C:/x", ""])
def test_protected_paths_must_stay_inside_the_repo(
    tmp_path: Path,
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match="protected_paths"):
        DispatchConfig(repo_root=tmp_path, protected_paths=(pattern,))
