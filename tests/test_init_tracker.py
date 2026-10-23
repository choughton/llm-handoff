from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from llm_handoff import init_workflow
from llm_handoff.__main__ import app
from llm_handoff.config import load_dispatch_config
from llm_handoff.init_workflow import (
    InitConflictError,
    InitializedTargetError,
    InitWorkflowError,
    InvalidTrackerOptionsError,
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


def test_cli_switches_a_safe_initialized_repo_to_linear(tmp_path: Path) -> None:
    _init(tmp_path, provider="claude")
    before = _snapshot(tmp_path)
    args = ("--tracker", "linear", "--tracker-team", "ENG", "--tracker-project", "P")
    dry = _invoke(str(tmp_path), *args, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert dry.stdout.splitlines()[0].startswith("DRY RUN: tracker none -> linear")
    assert _snapshot(tmp_path) == before
    result = _invoke(str(tmp_path), *args)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("Switched: tracker none -> linear")
    index = next(i for i, line in enumerate(lines) if line.startswith("Provider mapping:"))
    assert lines[index] == "Provider mapping: kept from dispatch_config.yaml"
    assert lines[index + 1] == "Tracker: linear"
    # The applied switch adds the Linear onboarding section after the summary
    # the dry run prints (LIN-12).
    summary, onboarding = result.stdout.split("\n\nLinear setup", 1)
    assert summary.replace("Switched", "DRY RUN", 1) + "\n" == dry.stdout
    assert "--probe-tracker ENG-123" in onboarding
    assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "linear"


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


# Check 3: an initialized repository is switched, not regenerated.


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_valid_config_is_switched_to_linear(
    tmp_path: Path, dry_run: bool, force: bool
) -> None:
    _init(tmp_path, provider="claude")
    before = _snapshot(tmp_path)
    result = _linear(tmp_path, dry_run=dry_run, force=force)
    assert result.switched_from == "none"
    assert result.tracker == "linear"
    assert result.conflicts == ()
    assert result.forced == ()
    assert result.copied == ()
    if dry_run:
        assert _snapshot(tmp_path) == before
    else:
        assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "linear"


@pytest.mark.parametrize("kind", ["directory", "link"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_unusable_config_refuses_linear_init(
    tmp_path: Path, kind: str, dry_run: bool
) -> None:
    # The initialized-target preflight runs before any switch is planned, so
    # a config that is not a usable file gets the preflight's refusal.
    config_path = tmp_path / CONFIG
    if kind == "directory":
        config_path.mkdir()
    else:
        try:
            config_path.symlink_to(tmp_path / "missing.yaml")
        except OSError:
            pytest.skip("symlinks are not available")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(InitializedTargetError) as excinfo:
        _linear(tmp_path, dry_run=dry_run)
    message = str(excinfo.value)
    assert "dispatch_config.yaml" in message
    assert "\n" not in message
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


@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_invalid_config_refuses_vanilla_init(
    tmp_path: Path, dry_run: bool
) -> None:
    # An invalid existing config is no longer a vanilla conflict: the
    # initialized-target preflight refuses it before anything is planned.
    (tmp_path / CONFIG).write_text("x: 1\n", encoding="utf-8")
    before = _snapshot(tmp_path)
    with pytest.raises(InitializedTargetError, match="fails config validation"):
        _init(tmp_path, dry_run=dry_run)
    assert _snapshot(tmp_path) == before


def test_existing_valid_config_with_local_edits_is_a_vanilla_conflict(
    tmp_path: Path,
) -> None:
    _init(tmp_path, provider="claude")
    config_path = tmp_path / CONFIG
    config_path.write_bytes(config_path.read_bytes() + b"# local edit\n")
    result = _init(tmp_path, provider="claude", dry_run=True)
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
    tpm_absent_roles = [role for role in LINEAR_PROMPT_ROLES if role != "tpm"]
    sections.extend(linear_role_section(role).strip("\n") for role in tpm_absent_roles)
    expected = f"{LINEAR_BEGIN}\n" + "\n\n".join(sections) + f"\n{LINEAR_END}\n"
    assert block == expected
    assert "## Linear Mode: tpm\n" not in block
    positions = [
        block.index("## Linear Evidence Comment\n"),
        block.index("## Linear Issue Template\n"),
    ]
    positions.extend(
        block.index(f"## Linear Mode: {role}\n") for role in tpm_absent_roles
    )
    assert positions == sorted(positions)


def test_default_handbook_text_pins_the_tpm_absent_block() -> None:
    text = init_workflow._linear_handbook_text()
    planner = linear_role_section("planner").strip("\r\n")
    assert planner in text
    assert "## Linear Mode: tpm" not in text
    assert linear_role_section("planner", tpm_configured=True).strip(
        "\r\n"
    ) not in text


# SHA-256 of the tpm-absent Linear handbook text. GROOMING changes only the
# tpm-configured templates, so this rendering must stay byte-identical.
# Re-pinned by TIDY-14, which adds the `epic_closed` event and changes the
# finalizer and validator sections (shared by both renderings), and by
# TIDY-15, which adds the `handoff_repaired` event and lease-repair text to
# the shared contract, planner, and validator sections.
TPM_ABSENT_HANDBOOK_SHA256 = (
    "b163141a256563e256bb28c4b2d6a379b9e8ea72fc3d7261e068274da44f2913"
)


def test_tpm_absent_handbook_text_is_byte_identical() -> None:
    text = init_workflow._linear_handbook_text()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == TPM_ABSENT_HANDBOOK_SHA256
    for phrase in ("Grooming.", "Epic direction.", "grooming rules"):
        assert phrase not in text, phrase


def test_handbook_text_with_tpm_carries_grooming_rules() -> None:
    text = init_workflow._linear_handbook_text(tpm_configured=True)
    planner_section = text[
        text.index("## Linear Mode: planner\n") : text.index("## Linear Mode: tpm\n")
    ]
    tpm_section = text[
        text.index("## Linear Mode: tpm\n") : text.index("## Linear Mode: backend\n")
    ]
    assert "Epic direction." in planner_section
    assert "Grooming." not in planner_section
    assert "Grooming." in tpm_section
    assert "Grooming never changes an epic." in tpm_section


def test_handbook_text_with_tpm_orders_sections() -> None:
    text = init_workflow._linear_handbook_text(tpm_configured=True)
    headings = [
        "## Linear Evidence Comment\n",
        "## Linear Issue Template\n",
        "## Linear Mode: planner\n",
        "## Linear Mode: tpm\n",
        "## Linear Mode: backend\n",
        "## Linear Mode: frontend\n",
        "## Linear Mode: auditor\n",
        "## Linear Mode: validator\n",
        "## Linear Mode: finalizer\n",
    ]
    for heading in headings:
        assert text.count(heading) == 1, heading
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    planner_section = text[
        text.index("## Linear Mode: planner\n") : text.index("## Linear Mode: tpm\n")
    ]
    assert "selection_basis: priority" not in planner_section
    assert linear_role_section("tpm").strip("\r\n") in text


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
    assert changed == {
        CONFIG,
        AGENTS,
        HANDBOOK,
        PLANNER_AGENT,
        TPM_AGENT,
        FRONTEND_AGENT,
        BACKEND_AGENT,
        AUDITOR_AGENT,
        FINALIZER_AGENT,
        # TIDY-15: Linear init appends the validator's `save_comment`.
        VALIDATOR_AGENT,
        # TIDY-15: handoff-router runs `next_agent: validator` handoffs.
        ROUTER_AGENT,
    }


# Claude planner, tpm, frontend, backend, auditor, finalizer, and router
# subagent tools lines gain their role's Linear MCP tools, since a subagent's `tools:` list
# is an allowlist.

PLANNER_AGENT = Path(".claude/agents/planner.md")
TPM_AGENT = Path(".claude/agents/tpm.md")
FRONTEND_AGENT = Path(".claude/agents/frontend.md")
BACKEND_AGENT = Path(".claude/agents/backend.md")
AUDITOR_AGENT = Path(".claude/agents/auditor.md")
FINALIZER_AGENT = Path(".claude/agents/ledger-updater.md")
# The dispatcher runs handoff-router for `next_agent: validator`, so it gets
# the validator's Linear tools.
ROUTER_AGENT = Path(".claude/agents/handoff-router.md")
_WRITER_TOOLS = b"tools: Read, Edit, Write, Grep, Glob, Bash"
_AUDITOR_TOOLS = b"tools: Read, Grep, Glob, Bash"
_FINALIZER_TOOLS = b"tools: Read, Edit, Write, Grep, Glob, Bash"


def _single_changed_line(
    root: Path, relative_path: Path, template_line: bytes = _WRITER_TOOLS
) -> bytes:
    rendered = (root / relative_path).read_bytes().splitlines(keepends=True)
    template = (
        (init_workflow._TEMPLATE_ROOT / relative_path)
        .read_bytes()
        .splitlines(keepends=True)
    )
    assert len(rendered) == len(template)
    differing = [i for i, (a, b) in enumerate(zip(rendered, template)) if a != b]
    assert len(differing) == 1
    assert template[differing[0]].rstrip(b"\r\n") == template_line
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


@pytest.mark.parametrize("server", ["linear", "team-linear"])
def test_tpm_tools_line_adds_linear_tools(tmp_path: Path, server: str) -> None:
    _linear(tmp_path, tracker_mcp_server=server)
    prefix = f"mcp__{server}__".encode()
    assert _single_changed_line(tmp_path, TPM_AGENT) == _WRITER_TOOLS + b"".join(
        b", " + prefix + tool
        for tool in (
            b"get_issue",
            b"list_issues",
            b"list_comments",
            b"save_comment",
            b"save_issue",
        )
    )


@pytest.mark.parametrize("server", ["linear", "team-linear"])
def test_backend_auditor_and_finalizer_tools_lines_add_linear_tools(
    tmp_path: Path, server: str
) -> None:
    _linear(tmp_path, tracker_mcp_server=server)
    prefix = f"mcp__{server}__".encode()
    expected = {
        BACKEND_AGENT: (
            _WRITER_TOOLS,
            (b"get_issue", b"list_issues", b"list_comments", b"save_comment"),
        ),
        AUDITOR_AGENT: (
            _AUDITOR_TOOLS,
            (
                b"get_issue",
                b"list_issues",
                b"list_comments",
                b"save_comment",
                b"save_issue",
            ),
        ),
        FINALIZER_AGENT: (
            _FINALIZER_TOOLS,
            (b"get_issue", b"list_issues", b"list_comments", b"save_comment"),
        ),
    }
    for relative_path, (base, tools) in expected.items():
        line = _single_changed_line(tmp_path, relative_path, base)
        assert line == base + b"".join(b", " + prefix + tool for tool in tools)


def test_linear_agent_tools_bases_match_template_tools_lines() -> None:
    for relative_path, (_role, base_tools) in init_workflow._LINEAR_AGENT_TOOLS.items():
        lines = (init_workflow._TEMPLATE_ROOT / relative_path).read_text(
            encoding="utf-8"
        ).splitlines()
        assert lines[0] == "---"
        frontmatter = lines[1 : lines.index("---", 1)]
        tools_lines = [line for line in frontmatter if line.startswith("tools: ")]
        assert len(tools_lines) == 1, relative_path
        # The handoff-validator template already lists the default-server
        # Linear tools; every other template lists built-in tools only.
        built_in = [
            tool
            for tool in tools_lines[0][len("tools: ") :].split(", ")
            if not tool.startswith("mcp__")
        ]
        assert f"tools: {', '.join(built_in)}" == f"tools: {base_tools}", (
            relative_path
        )


def test_claude_provider_linear_init_adds_planner_linear_tools(
    tmp_path: Path,
) -> None:
    _linear(tmp_path, provider="claude")
    line = _single_changed_line(tmp_path, PLANNER_AGENT)
    assert line.endswith(b", mcp__linear__save_issue")


@pytest.mark.parametrize(
    "relative_path",
    [
        PLANNER_AGENT,
        TPM_AGENT,
        FRONTEND_AGENT,
        BACKEND_AGENT,
        AUDITOR_AGENT,
        FINALIZER_AGENT,
        ROUTER_AGENT,
    ],
)
def test_vanilla_init_keeps_linear_subagent_templates(
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
        b"mcp__team-linear__list_issues, mcp__team-linear__list_comments, "
        b"mcp__team-linear__save_comment"
    )
    assert template[differing[0]].startswith(b"tools: ")
    assert b"save_issue" not in line


def test_validator_default_linear_init_appends_save_comment_only(
    tmp_path: Path,
) -> None:
    # The template already lists the default-server read tools; Linear init
    # appends only the comment-only `save_comment` grant (TIDY-15).
    _linear(tmp_path)
    rendered = (tmp_path / VALIDATOR_AGENT).read_bytes().splitlines(keepends=True)
    template = _validator_template().splitlines(keepends=True)
    assert len(rendered) == len(template)
    differing = [i for i, (a, b) in enumerate(zip(rendered, template)) if a != b]
    assert len(differing) == 1
    template_line = template[differing[0]].rstrip(b"\r\n")
    assert rendered[differing[0]].rstrip(b"\r\n") == (
        template_line + b", mcp__linear__save_comment"
    )
    assert b"save_issue" not in rendered[differing[0]]


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


@pytest.mark.parametrize("server", ["linear", "team-linear"])
def test_router_tools_line_adds_validator_linear_tools(
    tmp_path: Path, server: str
) -> None:
    _linear(tmp_path, tracker_mcp_server=server)
    prefix = f"mcp__{server}__".encode()
    line = _single_changed_line(tmp_path, ROUTER_AGENT, _AUDITOR_TOOLS)
    assert line == _AUDITOR_TOOLS + b"".join(
        b", " + prefix + tool
        for tool in (b"get_issue", b"list_issues", b"list_comments", b"save_comment")
    )
    assert b"save_issue" not in line
    # handoff-router and handoff-validator carry the same Linear grants.
    validator_tools = [
        raw
        for raw in (tmp_path / VALIDATOR_AGENT).read_bytes().splitlines()
        if raw.startswith(b"tools: ")
    ]
    assert validator_tools == [line]


@pytest.mark.parametrize("server", ["linear", "team-linear"])
def test_router_rerun_on_linear_output_is_skipped(
    tmp_path: Path, server: str
) -> None:
    _linear(tmp_path, tracker_mcp_server=server)
    agent = (tmp_path / ROUTER_AGENT).read_bytes()
    (tmp_path / CONFIG).unlink()
    result = _linear(tmp_path, tracker_mcp_server=server, dry_run=True)
    assert ROUTER_AGENT in result.skipped
    assert ROUTER_AGENT not in result.conflicts
    assert (tmp_path / ROUTER_AGENT).read_bytes() == agent


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
        b"mcp__team-linear__list_issues, mcp__team-linear__list_comments, "
        b"mcp__team-linear__save_comment\r\n"
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
    assert "Linear setup" not in result.output


# LIN-12: Linear onboarding after an applied fresh init.

ONBOARDING = "Linear setup (full guide: docs/LINEAR.md"


def _onboarding(stdout: str) -> list[str]:
    lines = stdout.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(ONBOARDING)]
    assert len(starts) == 1, stdout
    return lines[starts[0] :]


def _refuse_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError(f"init launched a process: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("server", [None, "team-linear"])
def test_cli_fresh_linear_init_prints_onboarding_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, server: str | None
) -> None:
    _refuse_subprocess(monkeypatch)
    extra = () if server is None else ("--tracker-mcp-server", server)
    result = _invoke(
        str(tmp_path),
        "--provider",
        provider,
        "--tracker",
        "linear",
        "--tracker-team",
        "ENG",
        "--tracker-project",
        "Platform",
        *extra,
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    section = _onboarding(result.stdout)
    start = len(lines) - len(section)
    # After the whole result summary, including the candidate commands.
    assert lines.index("Tracker: linear") < start
    assert any(line.startswith("Candidate commands") for line in lines[:start])
    assert lines[start - 1] == ""
    text = "\n".join(section)
    name = server or "linear"
    assert f'configure and authenticate a Linear MCP server named "{name}"' in text
    assert "every provider CLI that runs a role or fallback" in text
    assert "init did not configure or authenticate any connector" in text
    assert f"Change to the initialized repository root first: {tmp_path.resolve()}" in text
    assert "  3. Run: llm-handoff doctor" in section
    assert "does not prove that the connector authenticates" in text
    assert "  4. Optional: llm-handoff doctor --probe-tracker ENG-123" in section
    assert "Replace ENG-123 with a Linear issue you can access" in text
    assert "at least one Claude role or fallback" in text
    assert "a Codex-only setup cannot run it" in text
    assert "does not check any other provider's connector" in text
    assert all(line.isascii() for line in section)


def test_init_result_names_the_mcp_server(tmp_path: Path) -> None:
    assert _init(tmp_path / "none", dry_run=True).tracker_mcp_server is None
    assert _linear(tmp_path / "default", dry_run=True).tracker_mcp_server == "linear"
    custom = _linear(tmp_path / "custom", dry_run=True, tracker_mcp_server="lin-2")
    assert custom.tracker_mcp_server == "lin-2"


@pytest.mark.parametrize("args", [(), ("--tracker", "none")])
def test_cli_file_mode_init_prints_no_onboarding(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    result = _invoke(str(tmp_path), "--provider", "claude", *args)
    assert result.exit_code == 0, result.output
    assert "Linear setup" not in result.output
    assert "--probe-tracker" not in result.output


def test_cli_failed_linear_init_prints_no_onboarding(tmp_path: Path) -> None:
    (tmp_path / AGENTS).mkdir()
    args = ("--tracker", "linear", "--tracker-team", "ENG", "--tracker-project", "P")
    result = _invoke(str(tmp_path), "--provider", "claude", *args)
    assert result.exit_code == 1, result.output
    assert "Initialization aborted: target files already exist." in result.stderr
    assert "Linear setup" not in result.output

    bad = _invoke(str(tmp_path / "other"), "--provider", "claude", "--tracker", "linear")
    assert bad.exit_code == 2, bad.output
    assert "Linear setup" not in bad.output


# Check 10: initialized-target safety preflight (LIN-13).

HANDOFF = Path("docs/handoff/HANDOFF.md")
LOCK = Path(".dispatch.lock")
_MODE_KWARGS: dict[str, dict[str, str]] = {
    "none": {"tracker": "none"},
    "linear": {"tracker": "linear", "tracker_team": "ENG", "tracker_project": "P"},
}
_MODE_ARGS: dict[str, tuple[str, ...]] = {
    "none": ("--tracker", "none"),
    "linear": ("--tracker", "linear", "--tracker-team", "ENG", "--tracker-project", "P"),
}
_FLAGS = [(False, False), (True, False), (False, True), (True, True)]
_FLAG_IDS = ["plain", "dry-run", "force", "dry-run-force"]


def _tree(root: Path) -> dict[Path, bytes | None]:
    """Every entry under root: file bytes, or None for directories and links."""
    tree: dict[Path, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink() or not path.is_file():
            tree[relative] = None
        else:
            tree[relative] = path.read_bytes()
    return tree


def _handoff_text(
    next_agent: str | None,
    tracker_issue: str | None = None,
    *,
    extra: tuple[str, ...] = (),
    reason: bool = True,
) -> str:
    lines = ["---"]
    if next_agent is not None:
        lines.append(f"next_agent: {next_agent}")
    if reason:
        lines.append('reason: "Test handoff."')
    if tracker_issue is not None:
        lines.append(f"tracker_issue: {tracker_issue}")
    lines.extend(extra)
    lines.extend(["producer: planner", "---", "", "# Handoff", ""])
    return "\n".join(lines)


def _write(root: Path, relative: Path, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _initialized(root: Path) -> Path:
    _init(root, provider="claude")
    return root


def _assert_refused(
    root: Path, mode: str, dry_run: bool, force: bool, phrase: str
) -> InitializedTargetError:
    before = _tree(root)
    with pytest.raises(InitializedTargetError) as excinfo:
        _init(
            root, provider="claude", dry_run=dry_run, force=force, **_MODE_KWARGS[mode]
        )
    error = excinfo.value
    assert isinstance(error, InitWorkflowError)
    message = str(error)
    assert "\n" not in message
    assert phrase in message
    assert _tree(root) == before
    return error


def _make_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


def _make_junction(link: Path, target: Path) -> None:
    try:
        import _winapi
    except ImportError:
        pytest.skip("junctions are Windows-only")
    _winapi.CreateJunction(str(target), str(link))


# Acceptance 1: dispatch_config.yaml must be a readable, valid config.


_BAD_CONFIGS: dict[str, tuple[bytes, str]] = {
    "invalid-yaml": (b"agents: [\n", "is not valid YAML"),
    "not-a-mapping": (b"- one\n- two\n", "is not a YAML mapping"),
    "unknown-key": (b"x: 1\n", "fails config validation"),
    "bad-value": (b"poll_interval_seconds: soon\n", "fails config validation"),
    "bad-handoff-path": (b"handoff_path: ../outside.md\n", "fails config validation"),
    "not-utf8": (b"handoff_path: \xff\xfe\n", "cannot be read"),
}


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("case", sorted(_BAD_CONFIGS))
def test_preflight_refuses_invalid_config_before_writing(
    tmp_path: Path, case: str, mode: str, dry_run: bool, force: bool
) -> None:
    content, phrase = _BAD_CONFIGS[case]
    (tmp_path / CONFIG).write_bytes(content)
    _assert_refused(tmp_path, mode, dry_run, force, phrase)


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_refuses_config_directory(
    tmp_path: Path, mode: str, dry_run: bool, force: bool
) -> None:
    (tmp_path / CONFIG).mkdir()
    _assert_refused(tmp_path, mode, dry_run, force, "is a directory")


@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("dangling", [False, True], ids=["live", "dangling"])
def test_preflight_refuses_config_symlink(
    tmp_path: Path, mode: str, dangling: bool
) -> None:
    target = tmp_path / "real.yaml"
    if not dangling:
        target.write_text("handoff_path: docs/handoff/HANDOFF.md\n", encoding="utf-8")
    _make_symlink(tmp_path / CONFIG, target)
    for dry_run, force in _FLAGS:
        _assert_refused(tmp_path, mode, dry_run, force, "symbolic link or junction")


@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("dangling", [False, True], ids=["live", "dangling"])
def test_preflight_refuses_config_junction(
    tmp_path: Path, mode: str, dangling: bool
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    _make_junction(tmp_path / CONFIG, target)
    if dangling:
        target.rmdir()
    for dry_run, force in _FLAGS:
        _assert_refused(tmp_path, mode, dry_run, force, "symbolic link or junction")


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_refuses_unreadable_config(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _initialized(tmp_path)
    before = _tree(tmp_path)
    original = Path.read_text

    def refuse(self: Path, *args, **kwargs):
        if self.name == CONFIG.name:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    for dry_run, force in _FLAGS:
        with pytest.raises(InitializedTargetError) as excinfo:
            _init(
                tmp_path,
                provider="claude",
                dry_run=dry_run,
                force=force,
                **_MODE_KWARGS[mode],
            )
        message = str(excinfo.value)
        assert "cannot be read" in message
        assert "\n" not in message
    monkeypatch.undo()
    assert _tree(tmp_path) == before


# Acceptance 2: a dispatcher lock or run-state file blocks any change.


_RUN_STATE = [
    LOCK,
    Path("logs/dispatch/in-flight.json"),
    Path("logs/dispatch/story.json"),
    Path("logs/dispatch/publish/story.json"),
]


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("state", _RUN_STATE, ids=[p.as_posix() for p in _RUN_STATE])
def test_preflight_refuses_lock_and_run_state(
    tmp_path: Path, state: Path, mode: str, dry_run: bool, force: bool
) -> None:
    _initialized(tmp_path)
    _write(tmp_path, state, "{}\n")
    _assert_refused(tmp_path, mode, dry_run, force, state.as_posix())


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_refuses_lock_directory(tmp_path: Path, mode: str) -> None:
    _initialized(tmp_path)
    (tmp_path / LOCK).mkdir()
    for dry_run, force in _FLAGS:
        _assert_refused(tmp_path, mode, dry_run, force, ".dispatch.lock")


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_refuses_dangling_lock_symlink(tmp_path: Path, mode: str) -> None:
    _initialized(tmp_path)
    _make_symlink(tmp_path / LOCK, tmp_path / "gone")
    for dry_run, force in _FLAGS:
        _assert_refused(tmp_path, mode, dry_run, force, ".dispatch.lock")


def test_lock_without_config_is_not_an_initialized_target(tmp_path: Path) -> None:
    (tmp_path / LOCK).write_text("", encoding="utf-8")
    assert init_workflow.preflight_initialized_target(tmp_path) is None


# Acceptance 3: the configured handoff must be between assignments.


_ACTIVE_HANDOFFS: dict[str, tuple[str, str]] = {
    **{
        f"next-{role}": (_handoff_text(role), f"next_agent: {role}")
        for role in ("backend", "frontend", "auditor", "validator", "finalizer")
    },
    "next-Backend-label": (_handoff_text("Backend"), "next_agent: backend"),
    "planner-lease": (_handoff_text("planner", "ENG-12"), "tracker_issue: ENG-12"),
    "tpm-lease": (_handoff_text("tpm", "ENG-13"), "tracker_issue: ENG-13"),
    "user-lease": (_handoff_text("user", "ENG-14"), "tracker_issue: ENG-14"),
}
_UNKNOWN_HANDOFFS: dict[str, tuple[str, str]] = {
    "no-frontmatter": ("I made the edits.\n", "no YAML routing frontmatter"),
    "unclosed": ("---\nnext_agent: planner\n", "does not show a between-assignments"),
    "bad-yaml": ("---\nnext_agent: [\n---\n", "does not show a between-assignments"),
    "no-next-agent": (_handoff_text(None), "next_agent is None"),
    "unknown-next-agent": (_handoff_text("reviewer"), "next_agent is 'reviewer'"),
    # A status that conflicts with an idle-looking next_agent, or one the
    # router does not recognize, leaves the dispatcher with no route.
    "planner-ready-for-review": (
        _handoff_text("planner", extra=("status: ready_for_review",)),
        "status `ready_for_review` routes to auditor",
    ),
    "user-blocked-implementation": (
        _handoff_text("user", extra=("status: blocked_implementation_failure",)),
        "status `blocked_implementation_failure` routes to planner",
    ),
    "planner-unknown-status": (
        _handoff_text("planner", extra=("status: almost_done",)),
        "status `almost_done` is not recognized",
    ),
    "planner-no-reason": (
        _handoff_text("planner", reason=False),
        "reason is required",
    ),
}


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("case", sorted(_ACTIVE_HANDOFFS))
def test_preflight_refuses_active_handoff(
    tmp_path: Path, case: str, mode: str, dry_run: bool, force: bool
) -> None:
    _initialized(tmp_path)
    text, phrase = _ACTIVE_HANDOFFS[case]
    _write(tmp_path, HANDOFF, text)
    _assert_refused(tmp_path, mode, dry_run, force, phrase)


@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("case", sorted(_UNKNOWN_HANDOFFS))
def test_preflight_refuses_handoff_without_between_assignments_state(
    tmp_path: Path, case: str, mode: str
) -> None:
    _initialized(tmp_path)
    text, phrase = _UNKNOWN_HANDOFFS[case]
    _write(tmp_path, HANDOFF, text)
    for dry_run, force in _FLAGS:
        error = _assert_refused(tmp_path, mode, dry_run, force, phrase)
        assert "between assignments" in str(error)


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_refuses_unreadable_handoff(tmp_path: Path, mode: str) -> None:
    _initialized(tmp_path)
    handoff = tmp_path / HANDOFF
    handoff.unlink()
    handoff.mkdir()
    for dry_run, force in _FLAGS:
        _assert_refused(tmp_path, mode, dry_run, force, "cannot be read")


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_refuses_non_utf8_handoff(tmp_path: Path, mode: str) -> None:
    _initialized(tmp_path)
    (tmp_path / HANDOFF).write_bytes(b"---\nnext_agent: planner\xff\n---\n")
    for dry_run, force in _FLAGS:
        _assert_refused(tmp_path, mode, dry_run, force, "is not UTF-8 text")


def _use_custom_handoff_path(root: Path, relative: str) -> None:
    config = root / CONFIG
    text = config.read_text(encoding="utf-8")
    old = "handoff_path: docs/handoff/HANDOFF.md\n"
    assert old in text
    config.write_text(text.replace(old, f"handoff_path: {relative}\n"), encoding="utf-8")


@pytest.mark.parametrize("mode", ["none", "linear"])
def test_preflight_reads_the_configured_handoff_path(tmp_path: Path, mode: str) -> None:
    _initialized(tmp_path)
    _use_custom_handoff_path(tmp_path, "work/LIVE.md")
    # The default path looks idle; the configured one holds a lease on a
    # planner route.
    _write(tmp_path, Path("work/LIVE.md"), _handoff_text("planner", "ENG-7"))
    for dry_run, force in _FLAGS:
        error = _assert_refused(tmp_path, mode, dry_run, force, "tracker_issue: ENG-7")
        assert "work/LIVE.md" in str(error)


def test_preflight_ignores_an_active_handoff_outside_the_configured_path(
    tmp_path: Path,
) -> None:
    _initialized(tmp_path)
    _use_custom_handoff_path(tmp_path, "work/LIVE.md")
    _write(tmp_path, HANDOFF, _handoff_text("backend"))
    _write(tmp_path, Path("work/LIVE.md"), _handoff_text("user"))
    config = init_workflow.preflight_initialized_target(tmp_path)
    assert config is not None
    assert config.handoff_path == Path("work/LIVE.md")


@pytest.mark.parametrize(
    "state",
    ["missing", "starter", "planner", "tpm", "user", "Planner-label"],
)
def test_preflight_allows_between_assignments_states(
    tmp_path: Path, state: str
) -> None:
    _initialized(tmp_path)
    handoff = tmp_path / HANDOFF
    if state == "missing":
        handoff.unlink()
    elif state != "starter":
        _write(tmp_path, HANDOFF, _handoff_text(state.removesuffix("-label")))
    before = _tree(tmp_path)
    config = init_workflow.preflight_initialized_target(tmp_path)
    assert config is not None
    assert config.handoff_path == HANDOFF
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("next_agent", "status"),
    [
        ("planner", "verified_pass"),
        ("tpm", "verified_pass"),
        ("planner", "blocked_implementation_failure"),
        ("user", "blocked_missing_context"),
        ("user", "escalate_to_user"),
    ],
)
def test_preflight_allows_status_routes_that_stay_between_assignments(
    tmp_path: Path, next_agent: str, status: str
) -> None:
    _initialized(tmp_path)
    _write(tmp_path, HANDOFF, _handoff_text(next_agent, extra=(f"status: {status}",)))
    before = _tree(tmp_path)
    assert init_workflow.preflight_initialized_target(tmp_path) is not None
    assert _tree(tmp_path) == before


def test_preflight_allows_a_custom_missing_handoff(tmp_path: Path) -> None:
    _initialized(tmp_path)
    _use_custom_handoff_path(tmp_path, "work/LIVE.md")
    assert init_workflow.preflight_initialized_target(tmp_path) is not None


def test_preflight_returns_none_for_a_fresh_target(tmp_path: Path) -> None:
    assert init_workflow.preflight_initialized_target(tmp_path / "missing") is None
    assert init_workflow.preflight_initialized_target(tmp_path) is None
    assert not any(tmp_path.iterdir())


# Acceptance 4: API and CLI behavior for safe targets and refusals.


@pytest.mark.parametrize(("dry_run", "force"), _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("state", ["missing", "starter", "planner", "tpm", "user"])
def test_safe_target_switches_to_linear_and_keeps_the_handoff(
    tmp_path: Path, state: str, dry_run: bool, force: bool
) -> None:
    _initialized(tmp_path)
    if state == "missing":
        (tmp_path / HANDOFF).unlink()
    elif state != "starter":
        _write(tmp_path, HANDOFF, _handoff_text(state))
    before = _tree(tmp_path)
    result = _linear(tmp_path, dry_run=dry_run, force=force)
    assert result.switched_from == "none"
    assert result.conflicts == ()
    after = _tree(tmp_path)
    assert after.get(HANDOFF) == before.get(HANDOFF)
    if dry_run:
        assert after == before
    else:
        assert set(after) == set(before)
        assert load_dispatch_config(repo_root=tmp_path).tracker.provider == "linear"


@pytest.mark.parametrize("dry_run", [False, True])
def test_safe_target_rerun_with_tracker_none_is_unchanged(
    tmp_path: Path, dry_run: bool
) -> None:
    _initialized(tmp_path)
    before = _tree(tmp_path)
    result = _init(tmp_path, tracker="none", dry_run=dry_run)
    assert result.conflicts == ()
    assert result.copied == ()
    assert result.merged == ()
    assert _tree(tmp_path) == before


def test_fresh_init_output_is_unchanged_by_the_preflight(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _init(first, provider="claude")
    _init(second, provider="claude")
    tree = _tree(first)
    assert tree == _tree(second)
    files = {path for path, content in tree.items() if content is not None}
    assert files == set(init_workflow.template_files())
    assert (first / HANDOFF).read_bytes() == (
        init_workflow._TEMPLATE_ROOT / HANDOFF
    ).read_bytes()


_CLI_REFUSALS = {
    "invalid-config": "fails config validation",
    "lock": ".dispatch.lock",
    "active-handoff": "next_agent: auditor",
    "lease": "tracker_issue: ENG-9",
    "status-conflict": "status `ready_for_review` routes to auditor",
}


def _set_up_cli_refusal(root: Path, case: str) -> None:
    if case == "invalid-config":
        (root / CONFIG).write_text("x: 1\n", encoding="utf-8")
        return
    _initialized(root)
    if case == "lock":
        _write(root, LOCK, "")
    elif case == "active-handoff":
        _write(root, HANDOFF, _handoff_text("auditor"))
    elif case == "status-conflict":
        _write(
            root, HANDOFF, _handoff_text("planner", extra=("status: ready_for_review",))
        )
    else:
        _write(root, HANDOFF, _handoff_text("user", "ENG-9"))


@pytest.mark.parametrize("mode", ["none", "linear"])
@pytest.mark.parametrize("case", sorted(_CLI_REFUSALS))
def test_cli_preflight_refusal_exits_2_with_one_line(
    tmp_path: Path, case: str, mode: str
) -> None:
    _set_up_cli_refusal(tmp_path, case)
    before = _tree(tmp_path)
    for extra in ((), ("--dry-run",), ("--force",), ("--dry-run", "--force")):
        result = _invoke(str(tmp_path), "--provider", "claude", *_MODE_ARGS[mode], *extra)
        assert result.exit_code == 2, result.output
        assert result.stdout == ""
        assert len(result.stderr.strip().splitlines()) == 1
        assert "Traceback" not in result.output
        assert "Refusing to change the initialized repository" in result.stderr
        assert _CLI_REFUSALS[case] in result.stderr
        assert _tree(tmp_path) == before


def test_cli_conflict_still_exits_1(tmp_path: Path) -> None:
    _initialized(tmp_path)
    config = tmp_path / CONFIG
    config.write_bytes(config.read_bytes() + b"# local edit\n")
    result = _invoke(str(tmp_path), "--provider", "claude")
    assert result.exit_code == 1, result.output
    assert "Initialization aborted: target files already exist." in result.stderr
    assert "dispatch_config.yaml" in result.stderr
