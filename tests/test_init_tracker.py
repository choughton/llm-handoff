from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from llm_handoff import init_workflow
from llm_handoff.__main__ import app
from llm_handoff.config import load_dispatch_config
from llm_handoff.init_workflow import (
    InitConflictError,
    InitWorkflowError,
    InvalidTrackerOptionsError,
    TrackerRequiresFreshRepoError,
    TrackerUnsupportedMappingError,
    UnknownTrackerError,
    init_reference_workflow,
)
from llm_handoff.tracker_prompts import (
    LINEAR_PROMPT_ROLES,
    linear_agents_section,
    linear_evidence_contract,
    linear_issue_template,
    linear_role_section,
)

LINEAR_BEGIN = "<!-- llm-handoff:linear:begin -->"
LINEAR_END = "<!-- llm-handoff:linear:end -->"
TRACKER_BEGIN = "# llm-handoff:tracker:begin"
TRACKER_END = "# llm-handoff:tracker:end"
CONFIG = Path("dispatch_config.yaml")
AGENTS = Path("AGENTS.md")
HANDBOOK = Path("docs/handoff/HANDBOOK.md")


def _init(target: Path, **kwargs):
    kwargs.setdefault("available_providers", ())
    kwargs.setdefault("antigravity_available", False)
    return init_reference_workflow(target, **kwargs)


def _linear(target: Path, **kwargs):
    kwargs.setdefault("tracker", "linear")
    kwargs.setdefault("tracker_team", "ENG")
    if "tracker_label" not in kwargs:
        kwargs.setdefault("tracker_project", "Platform")
    return _init(target, **kwargs)


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _invoke(*args: str):
    return CliRunner().invoke(app, ["init", *args])


# Check 1: CLI options and exit codes.


def test_cli_linear_init_succeeds_and_prints_tracker_line(tmp_path: Path) -> None:
    result = _invoke(
        str(tmp_path),
        "--provider",
        "claude",
        "--tracker",
        "linear",
        "--tracker-team",
        "ENG",
        "--tracker-label",
        "repo-a",
        "--tracker-mcp-server",
        "linear-work",
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("Provider mapping:"))
    assert lines[index + 1] == "Tracker: linear"
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.provider == "linear"
    assert config.tracker.label == "repo-a"
    assert config.tracker.mcp_server == "linear-work"


@pytest.mark.parametrize("args", [(), ("--tracker", "none")])
def test_cli_vanilla_prints_tracker_none(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _invoke(str(tmp_path), "--provider", "codex", *args)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("Provider mapping:"))
    assert lines[index + 1] == "Tracker: none"


@pytest.mark.parametrize(
    ("args", "phrase"),
    [
        (("--tracker", "jira"), "Unknown tracker 'jira'"),
        (("--tracker", "linear", "--tracker-project", "P"), "--tracker-team"),
        (("--tracker", "linear", "--tracker-team", "ENG"), "exactly one of"),
        (
            (
                "--tracker",
                "linear",
                "--tracker-team",
                "ENG",
                "--tracker-project",
                "P",
                "--tracker-label",
                "L",
            ),
            "exactly one of",
        ),
        (
            (
                "--tracker",
                "linear",
                "--tracker-team",
                "ENG",
                "--tracker-project",
                "P",
                "--tracker-mcp-server",
                "a b",
            ),
            "--tracker-mcp-server",
        ),
        (
            ("--tracker", "linear", "--tracker-team", "  ", "--tracker-label", "L"),
            "--tracker-team",
        ),
        (("--tracker-team", "ENG"), "--tracker-team requires --tracker linear"),
        (
            ("--tracker", "none", "--tracker-mcp-server", "linear"),
            "--tracker-mcp-server requires --tracker linear",
        ),
        (
            ("--tracker", "none", "--tracker-project", "P", "--tracker-label", "L"),
            "--tracker-project, --tracker-label requires --tracker linear",
        ),
    ],
)
def test_cli_tracker_option_errors_exit_2_with_one_line(
    tmp_path: Path, args: tuple[str, ...], phrase: str
) -> None:
    for extra in ((), ("--dry-run",)):
        result = _invoke(str(tmp_path), "--provider", "claude", *args, *extra)
        assert result.exit_code == 2, result.output
        assert phrase in result.stderr
        assert len(result.stderr.strip().splitlines()) == 1
        assert not any(tmp_path.iterdir())


def test_cli_fresh_repo_refusal_exits_2(tmp_path: Path) -> None:
    (tmp_path / "dispatch_config.yaml").write_text("x: 1\n", encoding="utf-8")
    before = _snapshot(tmp_path)
    for extra in ((), ("--dry-run",)):
        result = _invoke(
            str(tmp_path),
            "--tracker",
            "linear",
            "--tracker-team",
            "ENG",
            "--tracker-project",
            "P",
            *extra,
        )
        assert result.exit_code == 2, result.output
        assert len(result.stderr.strip().splitlines()) == 1
        assert "no dispatch_config.yaml yet" in result.stderr
        assert "not supported yet" in result.stderr
    assert _snapshot(tmp_path) == before


def test_cli_help_lists_tracker_options() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for option in (
        "--tracker",
        "--tracker-team",
        "--tracker-project",
        "--tracker-label",
        "--tracker-mcp-server",
    ):
        assert option in result.output


# Check 2: tracker values are validated before anything is written.


@pytest.mark.parametrize(
    ("kwargs", "option"),
    [
        ({"tracker_project": "P", "tracker_label": "L"}, "--tracker-label"),
        ({"tracker_team": ""}, "--tracker-team"),
        ({"tracker_team": "   "}, "--tracker-team"),
        ({"tracker_project": "  "}, "--tracker-project"),
        ({"tracker_mcp_server": "a b"}, "--tracker-mcp-server"),
        ({"tracker_mcp_server": ""}, "--tracker-mcp-server"),
        ({"tracker_mcp_server": "mcp.server"}, "--tracker-mcp-server"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_tracker_values_raise_before_writing(
    tmp_path: Path, kwargs: dict[str, str], option: str, dry_run: bool
) -> None:
    target = tmp_path / "repo"
    with pytest.raises(InvalidTrackerOptionsError) as excinfo:
        _linear(target, dry_run=dry_run, **kwargs)
    assert isinstance(excinfo.value, InitWorkflowError)
    message = str(excinfo.value)
    assert option in message
    assert "\n" not in message
    assert not target.exists()


def test_unknown_tracker_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownTrackerError):
        _init(tmp_path, tracker="Linear")
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tracker_team": "ENG"},
        {"tracker_project": "P"},
        {"tracker_label": "L"},
        {"tracker_mcp_server": "linear"},
    ],
)
def test_tracker_options_without_linear_raise(
    tmp_path: Path, kwargs: dict[str, str]
) -> None:
    with pytest.raises(InvalidTrackerOptionsError, match="requires --tracker linear"):
        _init(tmp_path, tracker="none", **kwargs)
    assert not any(tmp_path.iterdir())


def test_tracker_values_are_stripped_in_the_block(tmp_path: Path) -> None:
    _linear(tmp_path, tracker_team=" ENG ", tracker_project=" Platform ")
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.team == "ENG"
    assert config.tracker.project == "Platform"
    text = (tmp_path / CONFIG).read_text(encoding="utf-8")
    assert "team: ENG\n" in text


# Check 3: fresh-repo refusal.


@pytest.mark.parametrize("kind", ["file", "directory", "link"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_config_refuses_linear_init(
    tmp_path: Path, kind: str, dry_run: bool
) -> None:
    config_path = tmp_path / CONFIG
    if kind == "file":
        config_path.write_text("x: 1\n", encoding="utf-8")
    elif kind == "directory":
        config_path.mkdir()
    else:
        try:
            config_path.symlink_to(tmp_path / "missing.yaml")
        except OSError:
            pytest.skip("symlinks are not available")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(TrackerRequiresFreshRepoError) as excinfo:
        _linear(tmp_path, dry_run=dry_run)
    message = str(excinfo.value)
    assert "--tracker linear only sets up a repository" in message
    assert "no dispatch_config.yaml yet" in message
    assert "switching an initialized repository is not supported yet" in message
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "antigravity"},
        {"available_providers": ("agy",)},
    ],
    ids=["explicit", "auto-detected"],
)
def test_linear_refuses_the_antigravity_mapping_before_writing(
    tmp_path: Path, kwargs: dict[str, object], dry_run: bool
) -> None:
    target = tmp_path / "repo"
    with pytest.raises(TrackerUnsupportedMappingError) as excinfo:
        _linear(target, dry_run=dry_run, **kwargs)
    assert isinstance(excinfo.value, InvalidTrackerOptionsError)
    assert isinstance(excinfo.value, InitWorkflowError)
    message = str(excinfo.value)
    assert "Linear mode does not support provider: antigravity" in message
    for option in ("--provider claude", "--provider codex", "--tracker none"):
        assert option in message
    assert "\n" not in message
    assert not target.exists()


def test_antigravity_mapping_without_linear_is_written(tmp_path: Path) -> None:
    result = _init(tmp_path, provider="antigravity", tracker="none")
    assert result.provider_mapping == "antigravity"
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.enabled is False


def test_cli_linear_with_auto_detected_agy_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        init_workflow.shutil,
        "which",
        lambda name, *args, **kwargs: "/usr/bin/agy" if name == "agy" else None,
    )
    result = _invoke(
        str(tmp_path),
        "--tracker",
        "linear",
        "--tracker-team",
        "ENG",
        "--tracker-project",
        "P",
    )
    assert result.exit_code == 2, result.output
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Linear mode does not support provider: antigravity" in result.stderr
    assert not any(tmp_path.iterdir())


def test_existing_config_does_not_affect_vanilla_init(tmp_path: Path) -> None:
    (tmp_path / CONFIG).write_text("x: 1\n", encoding="utf-8")
    result = _init(tmp_path, dry_run=True)
    assert CONFIG in result.conflicts


# Check 4: dispatch_config.yaml tracker block.


@pytest.mark.parametrize("provider", [None, "claude", "codex"])
@pytest.mark.parametrize("pool", ["project", "label"])
def test_config_block_round_trips_for_every_mapping(
    tmp_path: Path, provider: str | None, pool: str
) -> None:
    vanilla_root = tmp_path / "vanilla"
    linear_root = tmp_path / "linear"
    pool_kwargs = (
        {"tracker_project": "Platform"}
        if pool == "project"
        else {"tracker_label": "repo-a"}
    )
    _init(vanilla_root, provider=provider, verification_command="pytest -q")
    _linear(
        linear_root,
        provider=provider,
        verification_command="pytest -q",
        **pool_kwargs,
    )
    vanilla = (vanilla_root / CONFIG).read_text(encoding="utf-8")
    linear = (linear_root / CONFIG).read_text(encoding="utf-8")
    if provider is None:
        # The reference mapping moves its antigravity roles to claude.
        vanilla = init_workflow.render_linear_reference_agents(vanilla)
    assert linear.startswith(vanilla)
    appended = linear[len(vanilla) :]
    lines = appended.strip("\n").splitlines()
    assert lines[0] == TRACKER_BEGIN
    assert lines[-1] == TRACKER_END
    assert linear.endswith(TRACKER_END + "\n")
    expected = {
        "provider": "linear",
        "team": "ENG",
        **({"project": "Platform"} if pool == "project" else {"label": "repo-a"}),
        "eligible_state": "Todo",
        "in_progress_state": "In Progress",
        "success_state": "In Review",
        "mcp_server": "linear",
    }
    body = "\n".join(lines[1:-1]) + "\n"
    assert body == yaml.safe_dump(
        {"tracker": expected}, sort_keys=False, default_flow_style=False
    )
    assert list(yaml.safe_load(body)["tracker"]) == list(expected)
    assert "# tracker:\n" in vanilla
    for key in ("claude_mcp_config", "blocked_state", "user_attention_label"):
        assert f"  {key}:" not in appended

    config = load_dispatch_config(repo_root=linear_root)
    assert config.enforce_work_packet is True
    assert config.tracker.provider == "linear"
    assert config.tracker.team == "ENG"
    assert config.tracker.project == expected.get("project")
    assert config.tracker.label == expected.get("label")
    assert config.tracker.mcp_server == "linear"
    assert config.tracker.claude_mcp_config is None


def test_config_block_uses_template_crlf_line_ending() -> None:
    tracker = init_workflow.build_tracker_config(
        "linear", team="ENG", project="Platform"
    )
    assert tracker is not None
    template = "a: 1\r\n# tracker:\r\n"
    rendered = init_workflow.add_tracker_block(template, tracker)
    assert rendered.startswith(template)
    appended = rendered[len(template) :]
    assert "\n" not in appended.replace("\r\n", "")
    assert appended.endswith(TRACKER_END + "\r\n")
    assert yaml.safe_load(rendered)["tracker"]["team"] == "ENG"


def test_config_block_uses_lf_for_lf_template() -> None:
    tracker = init_workflow.build_tracker_config("linear", team="ENG", label="L")
    assert tracker is not None
    rendered = init_workflow.add_tracker_block("a: 1\n", tracker)
    assert "\r" not in rendered


# Check 5: AGENTS.md managed block.


def _linear_agents_block() -> str:
    return f"{LINEAR_BEGIN}\n{linear_agents_section().rstrip()}\n{LINEAR_END}"


def test_agents_block_appended_after_llm_handoff_block(tmp_path: Path) -> None:
    vanilla_root = tmp_path / "vanilla"
    linear_root = tmp_path / "linear"
    _init(vanilla_root)
    _linear(linear_root)
    vanilla = (vanilla_root / AGENTS).read_text(encoding="utf-8")
    linear = (linear_root / AGENTS).read_text(encoding="utf-8")
    assert linear == vanilla + _linear_agents_block() + "\n"
    assert linear.index("<!-- llm-handoff:end -->") < linear.index(LINEAR_BEGIN)


def test_agents_block_appended_to_existing_user_file(tmp_path: Path) -> None:
    user_text = b"# My rules\r\n\r\nKeep this.\r\n"
    (tmp_path / AGENTS).write_bytes(user_text)
    result = _linear(tmp_path)
    assert AGENTS in result.merged
    content = (tmp_path / AGENTS).read_bytes()
    assert content.startswith(user_text)
    assert content.endswith((_linear_agents_block() + "\n").encode("utf-8"))
    assert content.count(LINEAR_BEGIN.encode()) == 1


def test_agents_block_replaces_existing_linear_span(tmp_path: Path) -> None:
    prefix = b"before\n"
    suffix = b"\nafter: keep\r\n"
    old = (
        prefix
        + LINEAR_BEGIN.encode()
        + b"\nstale linear text\n"
        + LINEAR_END.encode()
        + suffix
    )
    (tmp_path / AGENTS).write_bytes(old)
    _linear(tmp_path)
    content = (tmp_path / AGENTS).read_bytes()
    assert b"stale linear text" not in content
    assert content.startswith(prefix)
    assert content.count(LINEAR_BEGIN.encode()) == 1
    block = _linear_agents_block().encode("utf-8")
    assert block in content
    start = content.index(block)
    tail = content[start + len(block) :]
    # The text after the Linear block is preserved byte for byte; the vanilla
    # llm-handoff block is appended after it.
    assert tail.startswith(suffix)
    assert b"<!-- llm-handoff:begin -->" in tail


def test_agents_rerun_on_linear_output_is_skipped(tmp_path: Path) -> None:
    _linear(tmp_path)
    agents = (tmp_path / AGENTS).read_bytes()
    (tmp_path / CONFIG).unlink()
    result = _linear(tmp_path, dry_run=True)
    assert AGENTS in result.skipped
    assert (tmp_path / AGENTS).read_bytes() == agents


@pytest.mark.parametrize(
    "content",
    [
        f"{LINEAR_BEGIN}\nx\n{LINEAR_END}\n{LINEAR_BEGIN}\ny\n{LINEAR_END}\n",
        f"{LINEAR_END}\nx\n{LINEAR_BEGIN}\n",
        f"text {LINEAR_BEGIN}\nx\n{LINEAR_END}\n",
        f"{LINEAR_BEGIN}\nx\n",
    ],
)
def test_malformed_linear_markers_are_a_conflict(tmp_path: Path, content: str) -> None:
    (tmp_path / AGENTS).write_text(content, encoding="utf-8", newline="")
    dry = _linear(tmp_path, dry_run=True)
    assert AGENTS in dry.conflicts
    with pytest.raises(InitConflictError) as excinfo:
        _linear(tmp_path)
    assert AGENTS in excinfo.value.conflicts
    assert (tmp_path / AGENTS).read_text(encoding="utf-8") == content
    assert not (tmp_path / CONFIG).exists()


def test_malformed_linear_markers_ignored_in_vanilla_mode(tmp_path: Path) -> None:
    content = f"{LINEAR_BEGIN}\nx\n{LINEAR_BEGIN}\n"
    (tmp_path / AGENTS).write_text(content, encoding="utf-8", newline="")
    result = _init(tmp_path)
    assert AGENTS in result.merged
    assert (tmp_path / AGENTS).read_text(encoding="utf-8").startswith(content)


# Check 6: docs/handoff/HANDBOOK.md managed block.


def test_handbook_block_content_and_order(tmp_path: Path) -> None:
    _linear(tmp_path)
    template = (init_workflow._TEMPLATE_ROOT / HANDBOOK).read_bytes()
    content = (tmp_path / HANDBOOK).read_bytes()
    assert content.startswith(template)
    block = content[len(template) :].decode("utf-8")
    sections = [
        linear_evidence_contract().strip("\n"),
        linear_issue_template().strip("\n"),
    ]
    sections.extend(linear_role_section(role).strip("\n") for role in LINEAR_PROMPT_ROLES)
    expected = f"{LINEAR_BEGIN}\n" + "\n\n".join(sections) + f"\n{LINEAR_END}\n"
    assert block == expected
    positions = [
        block.index("## Linear Evidence Comment\n"),
        block.index("## Linear Issue Template\n"),
    ]
    positions.extend(
        block.index(f"## Linear Mode: {role}\n") for role in LINEAR_PROMPT_ROLES
    )
    assert positions == sorted(positions)


def test_handbook_stays_non_managed_for_conflicts(tmp_path: Path) -> None:
    linear_root = tmp_path / "first"
    _linear(linear_root)
    linear_handbook = (linear_root / HANDBOOK).read_bytes()

    identical = tmp_path / "identical"
    (identical / HANDBOOK).parent.mkdir(parents=True)
    (identical / HANDBOOK).write_bytes(linear_handbook)
    assert HANDBOOK in _linear(identical, dry_run=True).skipped

    vanilla = tmp_path / "vanilla"
    (vanilla / HANDBOOK).parent.mkdir(parents=True)
    (vanilla / HANDBOOK).write_bytes(
        (init_workflow._TEMPLATE_ROOT / HANDBOOK).read_bytes()
    )
    assert HANDBOOK in _linear(vanilla, dry_run=True).conflicts
    with pytest.raises(InitConflictError):
        _linear(vanilla)
    result = _linear(vanilla, force=True)
    assert HANDBOOK in result.copied
    assert (vanilla / HANDBOOK).read_bytes() == linear_handbook


# Check 7: vanilla output is unchanged.


@pytest.mark.parametrize("provider", [None, "claude", "codex"])
def test_tracker_none_matches_no_tracker_option(
    tmp_path: Path, provider: str | None
) -> None:
    default_root = tmp_path / "default"
    none_root = tmp_path / "none"
    default_result = _init(default_root, provider=provider)
    none_result = _init(none_root, provider=provider, tracker="none")
    assert _snapshot(default_root) == _snapshot(none_root)
    assert default_result.copied == none_result.copied
    assert default_result.tracker == none_result.tracker == "none"
    assert b"llm-handoff:linear" not in (none_root / AGENTS).read_bytes()
    assert b"llm-handoff:linear" not in (none_root / HANDBOOK).read_bytes()
    assert b"llm-handoff:tracker" not in (none_root / CONFIG).read_bytes()
    assert (none_root / HANDBOOK).read_bytes() == (
        init_workflow._TEMPLATE_ROOT / HANDBOOK
    ).read_bytes()
    assert load_dispatch_config(repo_root=none_root).tracker.provider == "none"


def test_cli_tracker_none_matches_cli_without_option(tmp_path: Path) -> None:
    default_root = tmp_path / "default"
    none_root = tmp_path / "none"
    assert _invoke(str(default_root), "--provider", "claude").exit_code == 0
    assert (
        _invoke(str(none_root), "--provider", "claude", "--tracker", "none").exit_code
        == 0
    )
    assert _snapshot(default_root) == _snapshot(none_root)


def test_linear_changes_only_config_agents_handbook_and_subagent_tools(
    tmp_path: Path,
) -> None:
    vanilla_root = tmp_path / "vanilla"
    linear_root = tmp_path / "linear"
    _init(vanilla_root)
    _linear(linear_root)
    vanilla = _snapshot(vanilla_root)
    linear = _snapshot(linear_root)
    assert vanilla.keys() == linear.keys()
    changed = {path for path in vanilla if vanilla[path] != linear[path]}
    assert changed == {CONFIG, AGENTS, HANDBOOK, PLANNER_AGENT, FRONTEND_AGENT}


# Claude planner and frontend subagent tools lines gain their role's Linear
# MCP tools, since a subagent's `tools:` list is an allowlist.

PLANNER_AGENT = Path(".claude/agents/planner.md")
FRONTEND_AGENT = Path(".claude/agents/frontend.md")
_WRITER_TOOLS = b"tools: Read, Edit, Write, Grep, Glob, Bash"


def _single_changed_line(root: Path, relative_path: Path) -> bytes:
    rendered = (root / relative_path).read_bytes().splitlines(keepends=True)
    template = (
        (init_workflow._TEMPLATE_ROOT / relative_path)
        .read_bytes()
        .splitlines(keepends=True)
    )
    assert len(rendered) == len(template)
    differing = [i for i, (a, b) in enumerate(zip(rendered, template)) if a != b]
    assert len(differing) == 1
    assert template[differing[0]].rstrip(b"\r\n") == _WRITER_TOOLS
    return rendered[differing[0]].rstrip(b"\r\n")


@pytest.mark.parametrize("server", ["linear", "team-linear"])
def test_planner_and_frontend_tools_lines_add_linear_tools(
    tmp_path: Path, server: str
) -> None:
    _linear(tmp_path, tracker_mcp_server=server)
    prefix = f"mcp__{server}__".encode()
    assert _single_changed_line(tmp_path, PLANNER_AGENT) == _WRITER_TOOLS + b"".join(
        b", " + prefix + tool
        for tool in (
            b"get_issue",
            b"list_issues",
            b"list_comments",
            b"save_comment",
            b"save_issue",
        )
    )
    assert _single_changed_line(tmp_path, FRONTEND_AGENT) == _WRITER_TOOLS + b"".join(
        b", " + prefix + tool
        for tool in (b"get_issue", b"list_issues", b"list_comments", b"save_comment")
    )


def test_claude_provider_linear_init_adds_planner_linear_tools(
    tmp_path: Path,
) -> None:
    _linear(tmp_path, provider="claude")
    line = _single_changed_line(tmp_path, PLANNER_AGENT)
    assert line.endswith(b", mcp__linear__save_issue")


@pytest.mark.parametrize("relative_path", [PLANNER_AGENT, FRONTEND_AGENT])
def test_vanilla_init_keeps_planner_and_frontend_templates(
    tmp_path: Path, relative_path: Path
) -> None:
    _init(tmp_path)
    assert (tmp_path / relative_path).read_bytes() == (
        init_workflow._TEMPLATE_ROOT / relative_path
    ).read_bytes()


def test_planner_tools_rerun_on_linear_output_is_skipped(tmp_path: Path) -> None:
    _linear(tmp_path)
    agent = (tmp_path / PLANNER_AGENT).read_bytes()
    (tmp_path / CONFIG).unlink()
    result = _linear(tmp_path, dry_run=True)
    assert PLANNER_AGENT in result.skipped
    assert PLANNER_AGENT not in result.conflicts
    assert (tmp_path / PLANNER_AGENT).read_bytes() == agent


# Claude handoff-validator tools line follows tracker.mcp_server.

VALIDATOR_AGENT = Path(".claude/agents/handoff-validator.md")


def _validator_template() -> bytes:
    return (init_workflow._TEMPLATE_ROOT / VALIDATOR_AGENT).read_bytes()


def test_validator_tools_line_uses_custom_mcp_server(tmp_path: Path) -> None:
    _linear(tmp_path, tracker_mcp_server="team-linear")
    rendered = (tmp_path / VALIDATOR_AGENT).read_bytes().splitlines(keepends=True)
    template = _validator_template().splitlines(keepends=True)
    assert len(rendered) == len(template)
    differing = [i for i, (a, b) in enumerate(zip(rendered, template)) if a != b]
    assert len(differing) == 1
    line = rendered[differing[0]]
    assert line.rstrip(b"\r\n") == (
        b"tools: Read, Grep, Glob, Bash, mcp__team-linear__get_issue, "
        b"mcp__team-linear__list_issues, mcp__team-linear__list_comments"
    )
    assert template[differing[0]].startswith(b"tools: ")


def test_validator_default_linear_init_matches_template(tmp_path: Path) -> None:
    _linear(tmp_path)
    assert (tmp_path / VALIDATOR_AGENT).read_bytes() == _validator_template()


def test_validator_vanilla_init_matches_template(tmp_path: Path) -> None:
    _init(tmp_path)
    assert (tmp_path / VALIDATOR_AGENT).read_bytes() == _validator_template()


def test_validator_rerun_on_custom_server_output_is_skipped(tmp_path: Path) -> None:
    _linear(tmp_path, tracker_mcp_server="team-linear")
    agent = (tmp_path / VALIDATOR_AGENT).read_bytes()
    (tmp_path / CONFIG).unlink()
    result = _linear(tmp_path, tracker_mcp_server="team-linear", dry_run=True)
    assert VALIDATOR_AGENT in result.skipped
    assert VALIDATOR_AGENT not in result.conflicts
    assert (tmp_path / VALIDATOR_AGENT).read_bytes() == agent


def test_validator_render_preserves_crlf_line_ending() -> None:
    template = (
        b"---\r\nname: handoff-validator\r\n"
        b"tools: Read, Grep, Glob, Bash, mcp__linear__get_issue\r\n"
        b"model: haiku\r\n---\r\n\r\nbody tools: x\r\n"
    )
    rendered = init_workflow.render_validator_agent_tools(template, "team-linear")
    assert rendered == (
        b"---\r\nname: handoff-validator\r\n"
        b"tools: Read, Grep, Glob, Bash, mcp__team-linear__get_issue, "
        b"mcp__team-linear__list_issues, mcp__team-linear__list_comments\r\n"
        b"model: haiku\r\n---\r\n\r\nbody tools: x\r\n"
    )


@pytest.mark.parametrize(
    "template",
    [
        b"---\nname: x\n---\ntools: Read\n",
        b"---\ntools: Read\ntools: Grep\n---\n",
        b"tools: Read\n",
    ],
)
def test_validator_render_requires_one_frontmatter_tools_line(
    template: bytes,
) -> None:
    with pytest.raises(InitWorkflowError):
        init_workflow.render_validator_agent_tools(template, "linear")


# Check 8: dry run.


def test_linear_dry_run_writes_nothing_and_matches_real_run(tmp_path: Path) -> None:
    (tmp_path / AGENTS).write_text("custom\n", encoding="utf-8")
    before = _snapshot(tmp_path)
    dry = _linear(tmp_path, dry_run=True)
    assert _snapshot(tmp_path) == before
    assert dry.dry_run is True
    assert dry.tracker == "linear"
    real = _linear(tmp_path)
    assert real.copied == dry.copied
    assert real.merged == dry.merged
    assert AGENTS in real.merged
    assert CONFIG in real.copied
    assert HANDBOOK in real.copied


# Check 9: output.


def test_init_result_tracker_field(tmp_path: Path) -> None:
    assert _init(tmp_path / "a", dry_run=True).tracker == "none"
    assert _linear(tmp_path / "b", dry_run=True).tracker == "linear"


def test_cli_dry_run_prints_tracker_linear(tmp_path: Path) -> None:
    result = _invoke(
        str(tmp_path),
        "--dry-run",
        "--tracker",
        "linear",
        "--tracker-team",
        "ENG",
        "--tracker-project",
        "Platform",
    )
    assert result.exit_code == 0, result.output
    assert "Tracker: linear" in result.stdout.splitlines()
    assert not any(tmp_path.iterdir())
