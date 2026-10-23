"""LIN-14: `init --tracker` switches an initialized repository between modes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llm_handoff import init_workflow
from llm_handoff.__main__ import app
from llm_handoff.config import load_dispatch_config
from llm_handoff.init_workflow import (
    InitConflictError,
    InitializedTargetError,
    InitWorkflowError,
    TrackerSwitchConflictError,
    TrackerSwitchError,
    init_reference_workflow,
)
from llm_handoff.tracker_prompts import linear_agents_section

CONFIG = Path("dispatch_config.yaml")
AGENTS = Path("AGENTS.md")
HANDBOOK = Path("docs/handoff/HANDBOOK.md")
HANDOFF = Path("docs/handoff/HANDOFF.md")
PLANNER_AGENT = Path(".claude/agents/planner.md")
BACKEND_AGENT = Path(".claude/agents/backend.md")
VALIDATOR_AGENT = Path(".claude/agents/handoff-validator.md")
AGENT_FILES = tuple(init_workflow._LINEAR_AGENT_TOOLS)
SWITCHED_FILES = {CONFIG, AGENTS, HANDBOOK, *AGENT_FILES}
LINEAR_BEGIN = "<!-- llm-handoff:linear:begin -->"
LINEAR_END = "<!-- llm-handoff:linear:end -->"
TRACKER_BEGIN = "# llm-handoff:tracker:begin"
TRACKER_END = "# llm-handoff:tracker:end"
USER_AGENTS_TEXT = "\n## Local rules\n\nKeep this paragraph.\n"
CUSTOM_CONFIG_TEXT = "# local note: keep me\npoll_interval_seconds: 7\n"
_FLAGS = [(False, False), (True, False), (False, True), (True, True)]
_FLAG_IDS = ["plain", "dry-run", "force", "dry-run-force"]


def _fresh(root: Path, **kwargs) -> Path:
    kwargs.setdefault("available_providers", ())
    kwargs.setdefault("antigravity_available", False)
    init_reference_workflow(root, **kwargs)
    return root


def _switch(root: Path, mode: str, **kwargs):
    kwargs.setdefault("available_providers", ())
    kwargs.setdefault("antigravity_available", False)
    if mode == "linear":
        kwargs.setdefault("tracker_team", "ENG")
        if "tracker_label" not in kwargs:
            kwargs.setdefault("tracker_project", "Platform")
    return init_reference_workflow(root, tracker=mode, **kwargs)


def _tree(root: Path) -> dict[Path, bytes | None]:
    tree: dict[Path, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink() or not path.is_file():
            tree[relative] = None
        else:
            tree[relative] = path.read_bytes()
    return tree


def _invoke(*args: str):
    return CliRunner().invoke(app, ["init", *args])


def _append(root: Path, relative: Path, text: str) -> None:
    path = root / relative
    path.write_bytes(path.read_bytes() + text.encode("utf-8"))


def _customize(root: Path) -> None:
    """User content outside the managed spans and a non-tracker config edit."""
    _append(root, AGENTS, USER_AGENTS_TEXT)
    _append(root, HANDBOOK, "\n## Team notes\n\nLocal handbook text.\n")
    _append(root, CONFIG, CUSTOM_CONFIG_TEXT)
    _append(root, PLANNER_AGENT, "\nLocal planner note.\n")


def _to_crlf(root: Path) -> None:
    for relative in SWITCHED_FILES:
        path = root / relative
        data = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(data.replace(b"\n", b"\r\n"))


def _add_tpm(root: Path) -> None:
    path = root / CONFIG
    text = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    anchor = f"  planner:{newline}"
    assert text.count(anchor) == 1
    stanza = newline.join(
        ["  tpm:", "    provider: claude", "    binary: claude", "", "  planner:"]
    )
    path.write_bytes(text.replace(anchor, stanza + newline).encode("utf-8"))


def _tools_line(root: Path, relative: Path) -> bytes:
    lines = [
        line
        for line in (root / relative).read_bytes().splitlines()
        if line.startswith(b"tools: ")
    ]
    assert len(lines) == 1
    return lines[0]


def _result_view(result) -> tuple:
    return (
        result.switched_from,
        result.tracker,
        result.merged,
        result.skipped,
        result.conflicts,
        result.conflict_details,
        result.forced,
    )


# Acceptance 1: both directions, library and CLI.


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("server", [None, "team-linear"])
@pytest.mark.parametrize("tpm", [False, True], ids=["no-tpm", "tpm"])
def test_library_switch_both_directions(
    tmp_path: Path, provider: str, server: str | None, tpm: bool
) -> None:
    _fresh(tmp_path, provider=provider)
    _customize(tmp_path)
    if tpm:
        _add_tpm(tmp_path)
    vanilla = _tree(tmp_path)
    vanilla_config = load_dispatch_config(repo_root=tmp_path)

    to_linear = _switch(
        tmp_path, "linear", tracker_label="repo-a", tracker_mcp_server=server
    )
    assert to_linear.switched_from == "none"
    assert to_linear.tracker == "linear"
    assert to_linear.provider_mapping == init_workflow.KEPT_MAPPING
    assert to_linear.conflicts == ()
    assert set(to_linear.merged) == SWITCHED_FILES
    linear = _tree(tmp_path)
    assert set(linear) == set(vanilla)
    assert {path for path in vanilla if vanilla[path] != linear[path]} == SWITCHED_FILES

    config = load_dispatch_config(repo_root=tmp_path)
    expected_server = server or "linear"
    assert config.tracker.provider == "linear"
    assert config.tracker.team == "ENG"
    assert config.tracker.label == "repo-a"
    assert config.tracker.mcp_server == expected_server
    assert config.poll_interval_seconds == 7
    assert ("tpm" in config.agents) is tpm
    for role, agent in vanilla_config.agents.items():
        assert config.agents[role].provider == agent.provider
    assert config.commit_mode == vanilla_config.commit_mode

    handbook = (tmp_path / HANDBOOK).read_text(encoding="utf-8")
    assert ("## Linear Mode: tpm\n" in handbook) is tpm
    assert (tmp_path / HANDBOOK).read_text(encoding="utf-8").endswith(
        LINEAR_END + "\n"
    )
    assert (tmp_path / AGENTS).read_text(encoding="utf-8").endswith(
        USER_AGENTS_TEXT + f"{LINEAR_BEGIN}\n{linear_agents_section().rstrip()}"
        f"\n{LINEAR_END}\n"
    )
    assert _tools_line(tmp_path, PLANNER_AGENT).endswith(
        f", mcp__{expected_server}__save_issue".encode()
    )

    to_none = _switch(tmp_path, "none")
    assert to_none.switched_from == "linear"
    assert to_none.tracker == "none"
    assert set(to_none.merged) == SWITCHED_FILES
    assert _tree(tmp_path) == vanilla
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.provider == "none"
    assert config.poll_interval_seconds == 7


def test_cli_switch_both_directions(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    vanilla = _tree(tmp_path)
    linear_args = (
        "--tracker",
        "linear",
        "--tracker-team",
        "ENG",
        "--tracker-project",
        "Platform",
        "--tracker-mcp-server",
        "team-linear",
    )
    result = _invoke(str(tmp_path), *linear_args)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        f"Switched: tracker none -> linear in {tmp_path.resolve()}"
    )
    assert f"Change: {len(SWITCHED_FILES)} file(s)" in result.stdout
    assert "  - dispatch_config.yaml" in result.stdout
    assert "Tracker: linear" in result.stdout.splitlines()
    assert "Candidate commands" not in result.stdout
    assert load_dispatch_config(repo_root=tmp_path).tracker.mcp_server == "team-linear"

    rerun = _invoke(str(tmp_path), *linear_args)
    assert rerun.exit_code == 0, rerun.output
    assert "Change: 0 file(s)" in rerun.stdout
    assert f"Skip identical: {len(SWITCHED_FILES)} file(s)" in rerun.stdout

    back = _invoke(str(tmp_path), "--tracker", "none")
    assert back.exit_code == 0, back.output
    assert back.stdout.splitlines()[0].startswith("Switched: tracker linear -> none")
    assert "Tracker: none" in back.stdout.splitlines()
    assert _tree(tmp_path) == vanilla


def test_switch_from_a_fresh_linear_init_matches_its_files(tmp_path: Path) -> None:
    switched = _fresh(tmp_path / "switched", provider="claude")
    fresh = _fresh(
        tmp_path / "fresh",
        provider="claude",
        tracker="linear",
        tracker_team="ENG",
        tracker_project="Platform",
    )
    _switch(switched, "linear")
    switched_tree = _tree(switched)
    fresh_tree = _tree(fresh)
    for relative in SWITCHED_FILES:
        assert switched_tree[relative] == fresh_tree[relative], relative


# Acceptance 2: byte-identical round trips and idempotent reruns.


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
@pytest.mark.parametrize("server", [None, "team-linear"])
def test_none_linear_none_round_trip_is_byte_identical(
    tmp_path: Path, crlf: bool, server: str | None
) -> None:
    _fresh(tmp_path, provider="claude")
    _customize(tmp_path)
    if crlf:
        _to_crlf(tmp_path)
    before = _tree(tmp_path)
    _switch(tmp_path, "linear", tracker_mcp_server=server)
    middle = _tree(tmp_path)
    assert set(middle) == set(before)
    if crlf:
        for relative in SWITCHED_FILES:
            content = middle[relative] or b""
            assert b"\n" not in content.replace(b"\r\n", b""), relative
    _switch(tmp_path, "none")
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    "variant", ["unterminated", "empty-user-agents", "block-not-at-end"]
)
def test_round_trip_keeps_separators(tmp_path: Path, variant: str) -> None:
    _fresh(tmp_path, provider="claude")
    if variant == "unterminated":
        for relative in (CONFIG, AGENTS, HANDBOOK):
            path = tmp_path / relative
            path.write_bytes(path.read_bytes().rstrip(b"\n"))
    elif variant == "empty-user-agents":
        _append(tmp_path, AGENTS, "\n\n\n")
    else:
        _append(tmp_path, CONFIG, "\n\n")
    before = _tree(tmp_path)
    _switch(tmp_path, "linear")
    assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "linear"
    _switch(tmp_path, "none")
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
@pytest.mark.parametrize("tpm", [False, True], ids=["no-tpm", "tpm"])
def test_linear_none_linear_round_trip_is_byte_identical(
    tmp_path: Path, crlf: bool, tpm: bool
) -> None:
    _fresh(tmp_path, provider="claude")
    _customize(tmp_path)
    if tpm:
        _add_tpm(tmp_path)
    if crlf:
        _to_crlf(tmp_path)
    options = {"tracker_label": "repo-a", "tracker_mcp_server": "team-linear"}
    _switch(tmp_path, "linear", **options)
    linear = _tree(tmp_path)
    _switch(tmp_path, "none")
    _switch(tmp_path, "linear", **options)
    assert _tree(tmp_path) == linear


def test_fresh_linear_init_round_trips_through_none(tmp_path: Path) -> None:
    _fresh(
        tmp_path,
        provider="codex",
        tracker="linear",
        tracker_team="ENG",
        tracker_project="Platform",
    )
    linear = _tree(tmp_path)
    _switch(tmp_path, "none")
    assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "none"
    _switch(tmp_path, "linear")
    assert _tree(tmp_path) == linear


@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_same_mode_rerun_is_idempotent(tmp_path: Path, mode: str, dry_run: bool) -> None:
    _fresh(tmp_path, provider="claude")
    _customize(tmp_path)
    if mode == "linear":
        _switch(tmp_path, "linear")
    before = _tree(tmp_path)
    for force in (False, True):
        result = _switch(tmp_path, mode, dry_run=dry_run, force=force)
        assert result.switched_from == mode
        assert result.merged == ()
        assert result.conflicts == ()
        assert result.forced == ()
        assert set(result.skipped) == SWITCHED_FILES
        assert _tree(tmp_path) == before


def test_linear_rerun_with_new_options_rewrites_the_managed_parts(
    tmp_path: Path,
) -> None:
    _fresh(tmp_path, provider="claude")
    _switch(tmp_path, "linear")
    agents = (tmp_path / AGENTS).read_bytes()
    result = _switch(
        tmp_path, "linear", tracker_label="repo-b", tracker_mcp_server="team-linear"
    )
    assert result.conflicts == ()
    assert AGENTS in result.skipped
    assert (tmp_path / AGENTS).read_bytes() == agents
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.label == "repo-b"
    assert config.tracker.project is None
    assert config.tracker.mcp_server == "team-linear"
    assert b"mcp__team-linear__save_issue" in _tools_line(tmp_path, PLANNER_AGENT)
    assert b"mcp__linear__" not in _tools_line(tmp_path, PLANNER_AGENT)


def test_adding_a_tpm_rerenders_the_handbook_block(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    _switch(tmp_path, "linear")
    assert "## Linear Mode: tpm\n" not in (tmp_path / HANDBOOK).read_text("utf-8")
    _add_tpm(tmp_path)
    result = _switch(tmp_path, "linear")
    assert result.merged == (HANDBOOK,)
    assert "## Linear Mode: tpm\n" in (tmp_path / HANDBOOK).read_text("utf-8")
    _switch(tmp_path, "none")
    assert LINEAR_BEGIN not in (tmp_path / HANDBOOK).read_text("utf-8")


# Acceptance 3: dry run, conflicts, and --force.


def _edit_agents_block(root: Path) -> None:
    path = root / AGENTS
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(LINEAR_END, f"Local Linear note.\n{LINEAR_END}"), encoding="utf-8"
    )


def _edit_tools_line(root: Path) -> None:
    path = root / BACKEND_AGENT
    line = _tools_line(root, BACKEND_AGENT)
    path.write_bytes(path.read_bytes().replace(line, line + b", WebFetch"))


def _malformed_agents(root: Path) -> None:
    _append(root, AGENTS, f"{LINEAR_BEGIN}\nstray\n")


def _duplicate_handbook(root: Path) -> None:
    _append(root, HANDBOOK, f"{LINEAR_BEGIN}\nx\n{LINEAR_END}\n{LINEAR_BEGIN}\ny\n{LINEAR_END}\n")


def _edit_tracker_block(root: Path) -> None:
    path = root / CONFIG
    text = path.read_text(encoding="utf-8")
    assert "  eligible_state: Todo\n" in text
    path.write_text(
        text.replace("  eligible_state: Todo\n", "  eligible_state: Backlog\n"),
        encoding="utf-8",
    )


def _remove_agents(root: Path) -> None:
    (root / AGENTS).unlink()


NOTES_AFTER_BLOCK = "\n## Local notes\n\nKeep this after the managed section.\n"


def _notes_after_agents_block(root: Path) -> None:
    _append(root, AGENTS, NOTES_AFTER_BLOCK)


def _notes_after_handbook_block(root: Path) -> None:
    _append(root, HANDBOOK, NOTES_AFTER_BLOCK)


# case -> (start mode, target mode, edit, conflicting path, forced-file bytes
# change under --force)
_SOFT_CASES = {
    "edited-agents-block-to-none": ("linear", "none", _edit_agents_block, AGENTS, False),
    "edited-agents-block-rerun": ("linear", "linear", _edit_agents_block, AGENTS, False),
    "edited-tools-line-to-linear": ("none", "linear", _edit_tools_line, BACKEND_AGENT, False),
    "edited-tools-line-to-none": ("linear", "none", _edit_tools_line, BACKEND_AGENT, False),
    "malformed-agents-markers": ("none", "linear", _malformed_agents, AGENTS, False),
    "duplicate-handbook-markers": ("none", "linear", _duplicate_handbook, HANDBOOK, False),
    "missing-agents": ("none", "linear", _remove_agents, AGENTS, False),
    "notes-after-agents-block-to-none": (
        "linear",
        "none",
        _notes_after_agents_block,
        AGENTS,
        False,
    ),
    "notes-after-handbook-block-to-none": (
        "linear",
        "none",
        _notes_after_handbook_block,
        HANDBOOK,
        False,
    ),
    "edited-tracker-block-to-none": ("linear", "none", _edit_tracker_block, CONFIG, True),
    "edited-tracker-block-rerun": ("linear", "linear", _edit_tracker_block, CONFIG, True),
}


@pytest.mark.parametrize("case", sorted(_SOFT_CASES))
def test_local_edits_conflict_and_force_reports_them(tmp_path: Path, case: str) -> None:
    start, target, edit, path, rewritten = _SOFT_CASES[case]
    _fresh(tmp_path, provider="claude")
    _customize(tmp_path)
    if start == "linear":
        _switch(tmp_path, "linear")
    edit(tmp_path)
    before = _tree(tmp_path)

    dry = _switch(tmp_path, target, dry_run=True)
    assert dry.conflicts == (path,)
    assert len(dry.conflict_details) == 1
    assert dry.conflict_details[0].startswith(f"{path.as_posix()}: ")
    assert _tree(tmp_path) == before
    with pytest.raises(TrackerSwitchConflictError) as excinfo:
        _switch(tmp_path, target)
    assert isinstance(excinfo.value, InitConflictError)
    assert excinfo.value.conflicts == dry.conflicts
    assert excinfo.value.details == dry.conflict_details
    assert _tree(tmp_path) == before

    dry_force = _switch(tmp_path, target, dry_run=True, force=True)
    assert _tree(tmp_path) == before
    assert dry_force.conflicts == ()
    assert len(dry_force.forced) == 1
    assert dry_force.forced[0].startswith(f"{path.as_posix()}: ")
    real = _switch(tmp_path, target, force=True)
    assert _result_view(real)[1:] == _result_view(dry_force)[1:]
    after = _tree(tmp_path)
    if rewritten:
        assert path in real.merged
        assert "not kept" in real.forced[0]
    else:
        assert path not in real.merged
        assert after.get(path) == before.get(path)
        assert "left" in real.forced[0]
    # --force always leaves the requested mode selected and keeps unrelated
    # content.
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.provider == target
    assert config.poll_interval_seconds == 7
    if path != AGENTS:
        assert USER_AGENTS_TEXT in (tmp_path / AGENTS).read_text(encoding="utf-8")
    assert set(after) == set(before)


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
@pytest.mark.parametrize("fresh_linear", [False, True], ids=["switched", "fresh"])
def test_user_text_after_linear_blocks_keeps_the_round_trip(
    tmp_path: Path, crlf: bool, fresh_linear: bool
) -> None:
    if fresh_linear:
        _fresh(
            tmp_path,
            provider="claude",
            tracker="linear",
            tracker_team="ENG",
            tracker_project="Platform",
        )
    else:
        _fresh(tmp_path, provider="claude")
        _switch(tmp_path, "linear")
    _notes_after_agents_block(tmp_path)
    _notes_after_handbook_block(tmp_path)
    if crlf:
        _to_crlf(tmp_path)
    linear = _tree(tmp_path)

    # Removing a block that user text follows could not be undone in place,
    # so it is a conflict and nothing is written.
    dry = _switch(tmp_path, "none", dry_run=True)
    assert dry.conflicts == (AGENTS, HANDBOOK)
    assert all("follows its Linear section" in detail for detail in dry.conflict_details)
    with pytest.raises(TrackerSwitchConflictError):
        _switch(tmp_path, "none")
    assert _tree(tmp_path) == linear

    # --force leaves both sections in place, so the trip back is identical.
    forced = _switch(tmp_path, "none", force=True)
    assert {item.split(":")[0] for item in forced.forced} == {
        AGENTS.as_posix(),
        HANDBOOK.as_posix(),
    }
    assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "none"
    for relative in (AGENTS, HANDBOOK):
        assert (tmp_path / relative).read_bytes() == linear[relative]
    _switch(tmp_path, "linear")
    assert _tree(tmp_path) == linear


def test_config_text_after_the_tracker_block_conflicts_on_to_none(
    tmp_path: Path,
) -> None:
    _fresh(tmp_path, provider="claude")
    _switch(tmp_path, "linear")
    _append(tmp_path, CONFIG, CUSTOM_CONFIG_TEXT)
    before = _tree(tmp_path)

    dry = _switch(tmp_path, "none", dry_run=True)
    assert dry.conflicts == (CONFIG,)
    assert "follows its managed tracker block" in dry.conflict_details[0]
    with pytest.raises(TrackerSwitchConflictError):
        _switch(tmp_path, "none")
    assert _tree(tmp_path) == before

    # --force removes the block, keeps the text after it, and says where a
    # switch back puts the block.
    forced = _switch(tmp_path, "none", force=True)
    assert CONFIG in forced.merged
    assert "at the end of the file" in forced.forced[0]
    config = load_dispatch_config(repo_root=tmp_path)
    assert config.tracker.provider == "none"
    assert config.poll_interval_seconds == 7
    assert TRACKER_BEGIN not in (tmp_path / CONFIG).read_text(encoding="utf-8")


def _unmanaged_tracker(root: Path) -> None:
    _append(root, CONFIG, "tracker:\n  provider: none\n")


def _malformed_tracker_markers(root: Path) -> None:
    _append(root, CONFIG, f"{TRACKER_BEGIN}\n")


def _foreign_key_in_tracker_block(root: Path) -> None:
    path = root / CONFIG
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(TRACKER_END, f"max_consecutive_failures: 4\n{TRACKER_END}"),
        encoding="utf-8",
    )


def _linked_agents(root: Path) -> None:
    real = root / "AGENTS.real.md"
    (root / AGENTS).replace(real)
    try:
        (root / AGENTS).symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


def _hard_linked_handbook(root: Path) -> None:
    try:
        os.link(root / HANDBOOK, root / "HANDBOOK.copy.md")
    except OSError:
        pytest.skip("hard links are not available")


def _junction_agents_dir(root: Path) -> None:
    try:
        import _winapi
    except ImportError:
        pytest.skip("junctions are Windows-only")
    agents_dir = root / ".claude" / "agents"
    real = root / "agents-real"
    agents_dir.replace(real)
    _winapi.CreateJunction(str(real), str(agents_dir))


def _directory_tools_file(root: Path) -> None:
    (root / BACKEND_AGENT).unlink()
    (root / BACKEND_AGENT).mkdir()


# case -> (start mode, target mode, edit, conflicting path)
_HARD_CASES = {
    "unmanaged-tracker-key": ("none", "linear", _unmanaged_tracker, CONFIG),
    "malformed-tracker-markers": ("none", "linear", _malformed_tracker_markers, CONFIG),
    "malformed-tracker-markers-to-none": (
        "linear",
        "none",
        _malformed_tracker_markers,
        CONFIG,
    ),
    "foreign-key-in-tracker-block": (
        "linear",
        "none",
        _foreign_key_in_tracker_block,
        CONFIG,
    ),
    "linked-agents": ("none", "linear", _linked_agents, AGENTS),
    "junction-agents-dir": ("none", "linear", _junction_agents_dir, PLANNER_AGENT),
    "hard-linked-handbook": ("linear", "none", _hard_linked_handbook, HANDBOOK),
    "directory-tools-file": ("none", "linear", _directory_tools_file, BACKEND_AGENT),
}


@pytest.mark.parametrize("case", sorted(_HARD_CASES))
def test_unsafe_conflicts_stop_the_switch_even_with_force(
    tmp_path: Path, case: str
) -> None:
    start, target, edit, path = _HARD_CASES[case]
    _fresh(tmp_path, provider="claude")
    if start == "linear":
        _switch(tmp_path, "linear")
    edit(tmp_path)
    before = _tree(tmp_path)
    for force in (False, True):
        dry = _switch(tmp_path, target, dry_run=True, force=force)
        assert path in dry.conflicts
        assert _tree(tmp_path) == before
        with pytest.raises(TrackerSwitchConflictError) as excinfo:
            _switch(tmp_path, target, force=force)
        assert excinfo.value.conflicts == dry.conflicts
        assert excinfo.value.details == dry.conflict_details
        assert _tree(tmp_path) == before


def test_unmanaged_tracker_none_key_is_kept_on_a_none_rerun(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    _unmanaged_tracker(tmp_path)
    before = _tree(tmp_path)
    result = _switch(tmp_path, "none")
    assert result.conflicts == ()
    assert result.merged == ()
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
def test_dry_run_reports_the_same_plan_as_the_real_switch(
    tmp_path: Path, dry_run: bool, force: bool
) -> None:
    root = _fresh(tmp_path / "a", provider="claude")
    twin = _fresh(tmp_path / "b", provider="claude")
    for target in (root, twin):
        _customize(target)
        _edit_tools_line(target)
    planned = _switch(root, "linear", dry_run=True, force=force)
    if force:
        real = _switch(twin, "linear", force=True)
        assert _result_view(real) == _result_view(planned)
    else:
        with pytest.raises(TrackerSwitchConflictError) as excinfo:
            _switch(twin, "linear")
        assert excinfo.value.conflicts == planned.conflicts
        assert excinfo.value.details == planned.conflict_details


def test_cli_conflict_and_force_output(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    _edit_tools_line(tmp_path)
    before = _tree(tmp_path)
    args = ("--tracker", "linear", "--tracker-team", "ENG", "--tracker-project", "P")

    dry = _invoke(str(tmp_path), *args, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "Conflict: 1 file(s)" in dry.stdout
    assert "  - .claude/agents/backend.md: its tools line has local edits" in dry.stdout

    blocked = _invoke(str(tmp_path), *args)
    assert blocked.exit_code == 1, blocked.output
    assert "Tracker switch aborted: nothing was changed." in blocked.stderr
    assert ".claude/agents/backend.md: its tools line has local edits" in blocked.stderr
    assert "--force" in blocked.stderr
    assert _tree(tmp_path) == before

    forced = _invoke(str(tmp_path), *args, "--force")
    assert forced.exit_code == 0, forced.output
    assert "Left behind or replaced by --force: 1 file(s)" in forced.stdout
    assert "  - .claude/agents/backend.md: left as it is;" in forced.stdout
    assert (tmp_path / BACKEND_AGENT).read_bytes() == before[BACKEND_AGENT]
    assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "linear"


def test_cli_hard_conflict_exits_1_even_with_force(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    _unmanaged_tracker(tmp_path)
    before = _tree(tmp_path)
    for extra in ((), ("--force",)):
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
        assert result.exit_code == 1, result.output
        assert "dispatch_config.yaml: it has a tracker key outside" in result.stderr
    assert _tree(tmp_path) == before


# Config validation, options, and the preflight.


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
def test_reference_mapping_with_antigravity_roles_is_refused(
    tmp_path: Path, dry_run: bool, force: bool
) -> None:
    # The switch keeps the providers; config validation rejects antigravity
    # roles in Linear mode, so nothing is written.
    _fresh(tmp_path)
    before = _tree(tmp_path)
    with pytest.raises(TrackerSwitchError) as excinfo:
        _switch(tmp_path, "linear", dry_run=dry_run, force=force)
    message = str(excinfo.value)
    assert "would fail config validation in linear mode" in message
    assert "antigravity" in message
    assert "\n" not in message
    assert _tree(tmp_path) == before


def test_disabled_work_claim_gate_is_refused_for_linear(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    path = tmp_path / CONFIG
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "enforce_work_packet: true\n",
            "enforce_work_packet: false\n"
            'work_packet_opt_out_reason: "Local experiment."\n',
        ),
        encoding="utf-8",
    )
    before = _tree(tmp_path)
    with pytest.raises(TrackerSwitchError, match="enforce_work_packet"):
        _switch(tmp_path, "linear")
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "claude"},
        {"verification_command": "pytest -q"},
        {"provider": "codex", "verification_command": "make test"},
    ],
)
@pytest.mark.parametrize("mode", ["none", "linear"])
def test_switch_refuses_provider_and_verification_options(
    tmp_path: Path, kwargs: dict[str, str], mode: str
) -> None:
    _fresh(tmp_path, provider="claude")
    before = _tree(tmp_path)
    for dry_run, force in _FLAGS:
        with pytest.raises(TrackerSwitchError) as excinfo:
            _switch(tmp_path, mode, dry_run=dry_run, force=force, **kwargs)
        message = str(excinfo.value)
        assert "cannot be combined with --tracker" in message
        assert "\n" not in message
    assert _tree(tmp_path) == before


def test_cli_switch_refuses_provider_with_one_line(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    before = _tree(tmp_path)
    result = _invoke(str(tmp_path), "--provider", "claude", "--tracker", "none")
    assert result.exit_code == 2, result.output
    assert len(result.stderr.strip().splitlines()) == 1
    assert "--provider cannot be combined with --tracker" in result.stderr
    assert _tree(tmp_path) == before


def test_init_without_tracker_keeps_the_template_update(tmp_path: Path) -> None:
    # No --tracker option: an initialized target gets the ordinary template
    # update, not a switch.
    _fresh(tmp_path, provider="claude")
    rerun = init_reference_workflow(
        tmp_path, provider="claude", available_providers=(), antigravity_available=False
    )
    assert rerun.switched_from is None
    assert rerun.provider_mapping == "claude"


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_still_runs_before_a_switch(tmp_path: Path, mode: str) -> None:
    _fresh(tmp_path, provider="claude")
    if mode == "none":
        _switch(tmp_path, "linear")
    handoff = tmp_path / HANDOFF
    handoff.write_text(
        "---\nnext_agent: backend\nreason: \"Busy.\"\nproducer: planner\n---\n",
        encoding="utf-8",
    )
    before = _tree(tmp_path)
    for dry_run, force in _FLAGS:
        with pytest.raises(InitializedTargetError):
            _switch(tmp_path, mode, dry_run=dry_run, force=force)
    assert _tree(tmp_path) == before


def test_switch_does_not_touch_the_handoff_or_add_files(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    handoff = tmp_path / HANDOFF
    handoff.write_text(
        "---\nnext_agent: planner\nreason: \"Next epic.\"\nproducer: auditor\n---\n",
        encoding="utf-8",
    )
    before = _tree(tmp_path)
    _switch(tmp_path, "linear")
    after = _tree(tmp_path)
    assert after[HANDOFF] == before[HANDOFF]
    assert set(after) == set(before)


def test_switch_errors_are_init_workflow_errors() -> None:
    assert issubclass(TrackerSwitchError, InitWorkflowError)
    assert issubclass(TrackerSwitchConflictError, InitConflictError)


def test_validator_tools_line_round_trips_on_the_default_server(tmp_path: Path) -> None:
    _fresh(tmp_path, provider="claude")
    template_line = _tools_line(tmp_path, VALIDATOR_AGENT)
    _switch(tmp_path, "linear")
    assert _tools_line(tmp_path, VALIDATOR_AGENT) == (
        template_line + b", mcp__linear__save_comment"
    )
    _switch(tmp_path, "none")
    assert _tools_line(tmp_path, VALIDATOR_AGENT) == template_line
