"""TRANS-5: the handbook's transition table matches the validator.

`docs/handoff/HANDBOOK.md#transition-table` documents what each `producer`
permits on its own. These tests parse that table and probe
`validate_handoff_frontmatter()` for every canonical producer, route, status,
and close type, in both validator provenance modes, so the table and the
validator cannot drift apart. The validator itself never reads the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import pytest

from llm_handoff import validator as validator_module
from llm_handoff.agent_types import HandoffStatus
from llm_handoff.roles import CANONICAL_NEXT_AGENT_ROLES
from llm_handoff.router import HandoffRouting
from llm_handoff.validator import validate_handoff_frontmatter, validate_handoff_text


REPO_ROOT = Path(__file__).resolve().parents[1]
HANDBOOK = REPO_ROOT / "docs" / "handoff" / "HANDBOOK.md"
# `llm-handoff init` distributes this copy, so it must carry the same contract.
REFERENCE_HANDBOOK = (
    REPO_ROOT / "examples" / "reference-workflow" / "docs" / "handoff" / "HANDBOOK.md"
)
TABLE_ANCHOR = "docs/handoff/HANDBOOK.md#transition-table"

OMITTED = "(omitted)"
STATUSES = tuple(status.value for status in HandoffStatus)
CLOSE_TYPES = ("story", "epic")
AGENT = "agent"
DISPATCHER_AUTHORED = "dispatcher_authored"
PROVENANCES = (AGENT, DISPATCHER_AUTHORED)
COLUMNS = ("Producer", "Provenance", "next_agent", "status", "close_type")
# Every producer has an agent row; only the validator has a second row,
# because `dispatcher_authored=True` widens no other producer.
REQUIRED_ROWS = frozenset(
    [(role, AGENT) for role in CANONICAL_NEXT_AGENT_ROLES]
    + [("validator", DISPATCHER_AUTHORED)]
)

STATUS_RULE = "frontmatter_status_producer_rule:"
ROUTING_RULE = "frontmatter_routing_rule:"
# Structural epic-close rules that apply whoever the producer is. They are the
# table's "Epic close" check, not a producer permission.
_GENERIC_EPIC_RULES = (
    re.compile(r"^frontmatter_routing_rule: close_type `epic` requires next_agent "),
    re.compile(r"^frontmatter_routing_rule: next_agent `[^`]+` requires close_type "),
)
SCOPE_SHA = "0a040ac"

_SECTION_RE = re.compile(r"(?ms)^## Transition Table[ \t]*\n(.*?)(?=^## |\Z)")
_TOKEN_RE = re.compile(r"^`([^`]+)`$")


class TransitionTableError(ValueError):
    """The handbook table is missing, malformed, or names an unknown token."""


@dataclass(frozen=True)
class Permissions:
    routes: frozenset[str]
    statuses: frozenset[str | None]
    close_types: frozenset[str | None]


Table = dict[tuple[str, str], Permissions]


# Parsing


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        raise TransitionTableError(f"table line is not a `| ... |` row: {line!r}")
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _tokens(cell: str, *, allowed: tuple[str, ...], kind: str, row: str) -> list[str | None]:
    values: list[str | None] = []
    for raw in cell.split(","):
        token = raw.strip()
        match = _TOKEN_RE.fullmatch(token)
        if match is None:
            raise TransitionTableError(
                f"row {row}: {kind} token {token!r} is not a backticked value"
            )
        value = match.group(1)
        if value == OMITTED:
            if kind == "next_agent":
                raise TransitionTableError(
                    f"row {row}: next_agent cannot be {OMITTED}; it is required"
                )
            parsed: str | None = None
        elif value in allowed:
            parsed = value
        else:
            raise TransitionTableError(f"row {row}: unknown {kind} token `{value}`")
        if parsed in values:
            raise TransitionTableError(f"row {row}: duplicate {kind} token `{value}`")
        values.append(parsed)
    return values


def parse_transition_table(text: str) -> Table:
    section = _SECTION_RE.search(text)
    if section is None:
        raise TransitionTableError("no `## Transition Table` section")
    lines = [
        line for line in section.group(1).splitlines() if line.lstrip().startswith("|")
    ]
    if len(lines) < 2:
        raise TransitionTableError("transition table has no header and separator")
    header = tuple(_cells(lines[0]))
    if header != COLUMNS:
        raise TransitionTableError(f"table columns are {header}, expected {COLUMNS}")
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in _cells(lines[1])):
        raise TransitionTableError("second table line is not a separator row")

    table: Table = {}
    for line in lines[2:]:
        cells = _cells(line)
        if len(cells) != len(COLUMNS):
            raise TransitionTableError(f"row has {len(cells)} cells: {line!r}")
        producer_cell, provenance, routes, statuses, close_types = cells
        match = _TOKEN_RE.fullmatch(producer_cell)
        producer = match.group(1) if match else producer_cell
        if producer not in CANONICAL_NEXT_AGENT_ROLES:
            raise TransitionTableError(f"unknown producer role `{producer}`")
        if provenance not in PROVENANCES:
            raise TransitionTableError(
                f"row {producer}: unknown provenance `{provenance}`"
            )
        key = (producer, provenance)
        row = f"{producer}/{provenance}"
        if key in table:
            raise TransitionTableError(f"duplicate row {row}")
        if key not in REQUIRED_ROWS:
            raise TransitionTableError(
                f"unexpected row {row}; only `validator` has a "
                f"{DISPATCHER_AUTHORED} row"
            )
        table[key] = Permissions(
            routes=frozenset(
                _tokens(routes, allowed=CANONICAL_NEXT_AGENT_ROLES, kind="next_agent", row=row)
            ),
            statuses=frozenset(
                _tokens(statuses, allowed=STATUSES, kind="status", row=row)
            ),
            close_types=frozenset(
                _tokens(close_types, allowed=CLOSE_TYPES, kind="close_type", row=row)
            ),
        )

    missing = sorted(REQUIRED_ROWS - set(table))
    if missing:
        raise TransitionTableError(
            "missing rows: " + ", ".join(f"{p}/{prov}" for p, prov in missing)
        )
    return table


# Probing the validator


def _producer_rule_errors(errors: list[str]) -> list[str]:
    return [
        error
        for error in errors
        if error.startswith((STATUS_RULE, ROUTING_RULE))
        and not any(rule.match(error) for rule in _GENERIC_EPIC_RULES)
    ]


def _permits(producer: str, *, dispatcher_authored: bool, **fields: str | None) -> bool:
    parsed = HandoffRouting(
        reason="Transition table probe.",
        producer=producer,
        **{name: value for name, value in fields.items() if value is not None},
    )
    result = validate_handoff_frontmatter(
        parsed, dispatcher_authored=dispatcher_authored
    )
    return not _producer_rule_errors(result.errors)


def observe_validator(producer: str, provenance: str) -> Permissions:
    """What the validator permits `producer` on its own, one field at a time."""

    flag = provenance == DISPATCHER_AUTHORED
    return Permissions(
        routes=frozenset(
            route
            for route in CANONICAL_NEXT_AGENT_ROLES
            if _permits(producer, dispatcher_authored=flag, next_agent=route)
        ),
        statuses=frozenset(
            status
            for status in (None, *STATUSES)
            if _permits(producer, dispatcher_authored=flag, status=status)
        ),
        close_types=frozenset(
            close_type
            for close_type in (None, *CLOSE_TYPES)
            if _permits(
                producer,
                dispatcher_authored=flag,
                close_type=close_type,
                scope_sha=SCOPE_SHA if close_type else None,
            )
        ),
    )


def _label(value: str | None) -> str:
    return OMITTED if value is None else value


def transition_table_mismatches(table: Table) -> list[str]:
    mismatches: list[str] = []
    for producer, provenance in sorted(table):
        documented = table[(producer, provenance)]
        observed = observe_validator(producer, provenance)
        for field_name in ("routes", "statuses", "close_types"):
            doc_values = getattr(documented, field_name)
            seen_values = getattr(observed, field_name)
            for value in sorted(doc_values - seen_values, key=_label):
                mismatches.append(
                    f"{producer}/{provenance} {field_name}: table permits "
                    f"`{_label(value)}`, validator rejects it"
                )
            for value in sorted(seen_values - doc_values, key=_label):
                mismatches.append(
                    f"{producer}/{provenance} {field_name}: validator permits "
                    f"`{_label(value)}`, table omits it"
                )
    return mismatches


def _handbook_text() -> str:
    return HANDBOOK.read_text(encoding="utf-8")


def _replace_in_row(text: str, row_prefix: str, old: str, new: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(row_prefix):
            assert old in line, (row_prefix, old)
            lines[index] = line.replace(old, new, 1)
            return "".join(lines)
    raise AssertionError(f"no row starting {row_prefix!r}")


# Parity


@pytest.mark.parametrize(
    "handbook", [HANDBOOK, REFERENCE_HANDBOOK], ids=["root", "reference"]
)
def test_transition_table_matches_the_validator(handbook: Path) -> None:
    table = parse_transition_table(handbook.read_text(encoding="utf-8"))

    assert set(table) == REQUIRED_ROWS
    assert transition_table_mismatches(table) == []


def test_the_distributed_handbook_carries_the_same_transition_table() -> None:
    root = _SECTION_RE.search(_handbook_text())
    reference = _SECTION_RE.search(REFERENCE_HANDBOOK.read_text(encoding="utf-8"))

    assert root is not None and reference is not None
    assert reference.group(1) == root.group(1)


@pytest.mark.parametrize(
    "producer", [role for role in CANONICAL_NEXT_AGENT_ROLES if role != "validator"]
)
def test_dispatcher_authored_widens_no_producer_but_the_validator(
    producer: str,
) -> None:
    assert observe_validator(producer, DISPATCHER_AUTHORED) == observe_validator(
        producer, AGENT
    )


def test_dispatcher_authored_adds_only_the_validator_verdicts() -> None:
    table = parse_transition_table(_handbook_text())
    agent_row = table[("validator", AGENT)]
    dispatcher_row = table[("validator", DISPATCHER_AUTHORED)]

    assert dispatcher_row.routes == agent_row.routes
    assert dispatcher_row.close_types == agent_row.close_types
    assert dispatcher_row.statuses - agent_row.statuses == {
        "verified_pass",
        "verified_fail",
    }
    assert agent_row.statuses <= dispatcher_row.statuses


def test_a_changed_table_permission_fails_parity() -> None:
    narrowed = _replace_in_row(
        _handbook_text(), "| `tpm` | agent |", "`auditor`, ", ""
    )
    widened = _replace_in_row(
        _handbook_text(), "| `finalizer` | agent |", "`(omitted)` |\n", "`(omitted)`, `story` |\n"
    )

    assert transition_table_mismatches(parse_transition_table(narrowed)) == [
        "tpm/agent routes: validator permits `auditor`, table omits it"
    ]
    assert transition_table_mismatches(parse_transition_table(widened)) == [
        "finalizer/agent close_types: table permits `story`, validator rejects it"
    ]


def test_a_changed_validator_rule_fails_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = parse_transition_table(_handbook_text())
    monkeypatch.setattr(
        validator_module,
        "_TPM_ALLOWED_NEXT_AGENTS",
        (*validator_module._TPM_ALLOWED_NEXT_AGENTS, "validator"),
    )
    monkeypatch.setattr(
        validator_module,
        "_FINALIZER_REJECTED_STATUSES",
        (
            *validator_module._FINALIZER_REJECTED_STATUSES,
            HandoffStatus.ESCALATE_TO_USER.value,
        ),
    )

    assert transition_table_mismatches(table) == [
        "finalizer/agent statuses: table permits `escalate_to_user`, "
        "validator rejects it",
        "tpm/agent routes: validator permits `validator`, table omits it",
    ]


def test_a_changed_validator_provenance_rule_fails_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dropping the verdict gate would let an agent-written validator handoff
    # (and planner or user handoffs) carry a verdict without the flag.
    table = parse_transition_table(_handbook_text())
    monkeypatch.setattr(validator_module, "_VERIFIED_STATUSES", ())

    assert transition_table_mismatches(table) == [
        f"{producer}/agent statuses: validator permits `{status}`, table omits it"
        for producer in ("planner", "user", "validator")
        for status in ("verified_fail", "verified_pass")
    ]


# Malformed tables fail clearly


def test_a_missing_row_fails_clearly() -> None:
    text = "".join(
        line
        for line in _handbook_text().splitlines(keepends=True)
        if not line.startswith("| `user` | agent |")
    )

    with pytest.raises(TransitionTableError, match="missing rows: user/agent"):
        parse_transition_table(text)


def test_a_duplicate_row_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(),
        "| `tpm` | agent |",
        "| `tpm` | agent |",
        "| `backend` | agent |",
    )

    with pytest.raises(TransitionTableError, match="duplicate row backend/agent"):
        parse_transition_table(text)


def test_an_unknown_producer_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(), "| `tpm` | agent |", "| `tpm` |", "| `reviewer` |"
    )

    with pytest.raises(TransitionTableError, match="unknown producer role `reviewer`"):
        parse_transition_table(text)


def test_an_unknown_route_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(), "| `backend` | agent |", "`planner`, ", "`reviewer`, "
    )

    with pytest.raises(
        TransitionTableError, match="row backend/agent: unknown next_agent token `reviewer`"
    ):
        parse_transition_table(text)


def test_an_unknown_status_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(), "| `finalizer` | agent |", "`escalate_to_user`", "`escalated`"
    )

    with pytest.raises(
        TransitionTableError, match="row finalizer/agent: unknown status token `escalated`"
    ):
        parse_transition_table(text)


def test_an_unknown_close_type_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(), "| `finalizer` | agent |", "`(omitted)` |\n", "`final` |\n"
    )

    with pytest.raises(
        TransitionTableError, match="row finalizer/agent: unknown close_type token `final`"
    ):
        parse_transition_table(text)


def test_an_unknown_provenance_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(), "| `validator` | dispatcher_authored |", "dispatcher_authored", "trusted"
    )

    with pytest.raises(TransitionTableError, match="unknown provenance `trusted`"):
        parse_transition_table(text)


def test_an_extra_provenance_row_fails_clearly() -> None:
    text = _replace_in_row(
        _handbook_text(), "| `validator` | dispatcher_authored |", "`validator`", "`auditor`"
    )

    with pytest.raises(TransitionTableError, match="unexpected row auditor/dispatcher_authored"):
        parse_transition_table(text)


def test_a_missing_section_fails_clearly() -> None:
    text = _handbook_text().replace("## Transition Table", "## Transition Notes")

    with pytest.raises(TransitionTableError, match="no `## Transition Table` section"):
        parse_transition_table(text)


# Additional checks the table names: a permitted route and a permitted status
# do not make their combination legal.


def _errors(**fields: object) -> list[str]:
    flag = bool(fields.pop("dispatcher_authored", False))
    parsed = HandoffRouting(reason="Transition check.", **fields)  # type: ignore[arg-type]
    return validate_handoff_frontmatter(parsed, dispatcher_authored=flag).errors


@pytest.mark.parametrize(
    ("producer", "status", "next_agent"),
    [
        ("backend", "ready_for_review", "planner"),
        ("auditor", "verified_pass", "backend"),
        ("finalizer", "verified_pass", "user"),
        ("tpm", "blocked_implementation_failure", "backend"),
    ],
)
def test_a_permitted_route_and_status_can_still_conflict(
    producer: str, status: str, next_agent: str
) -> None:
    table = parse_transition_table(_handbook_text())
    row = table[(producer, AGENT)]
    assert next_agent in row.routes and status in row.statuses

    errors = _errors(producer=producer, status=status, next_agent=next_agent)

    assert _producer_rule_errors(errors) == []
    assert any(error.startswith("frontmatter_status_route_conflict:") for error in errors)


@pytest.mark.parametrize("producer", ["planner", "user"])
def test_a_non_auditor_epic_close_routes_only_to_the_auditor(producer: str) -> None:
    to_auditor = _errors(
        producer=producer, next_agent="auditor", close_type="epic", scope_sha=SCOPE_SHA
    )
    to_backend = _errors(
        producer=producer, next_agent="backend", close_type="epic", scope_sha=SCOPE_SHA
    )
    to_finalizer = _errors(
        producer=producer, next_agent="finalizer", close_type="epic", scope_sha=SCOPE_SHA
    )

    assert to_auditor == []
    assert (
        "frontmatter_routing_rule: close_type `epic` requires next_agent "
        "`auditor` or `finalizer`."
    ) in to_backend
    assert (
        "frontmatter_routing_rule: next_agent `finalizer` requires producer `auditor`."
    ) in to_finalizer


def test_an_auditor_finalizer_route_needs_an_epic_close() -> None:
    assert _errors(producer="auditor", next_agent="finalizer") == [
        "frontmatter_routing_rule: next_agent `finalizer` requires close_type `epic`."
    ]
    assert (
        _errors(
            producer="auditor",
            next_agent="finalizer",
            status="verified_pass",
            close_type="epic",
            scope_sha=SCOPE_SHA,
        )
        == []
    )


@pytest.mark.parametrize("flag", [False, True])
def test_no_provenance_lets_the_validator_close_an_epic(flag: bool) -> None:
    errors = _errors(
        producer="validator",
        next_agent="finalizer",
        status="verified_pass",
        close_type="epic",
        scope_sha=SCOPE_SHA,
        dispatcher_authored=flag,
    )

    assert _producer_rule_errors(errors)


def _handoff_text(**fields: str) -> str:
    frontmatter = "\n".join(f"{name}: {value}" for name, value in fields.items())
    return f'---\nreason: "Transition check."\n{frontmatter}\n---\n\n# Handoff\n'


def _text_errors(previous_agent: str, **kwargs: object) -> list[str]:
    fields = {name: str(value) for name, value in kwargs.pop("fields").items()}  # type: ignore[union-attr]
    return validate_handoff_text(
        _handoff_text(**fields), previous_agent, **kwargs  # type: ignore[arg-type]
    ).errors


def _mismatch(errors: list[str]) -> bool:
    return any(error.startswith("frontmatter_producer_mismatch:") for error in errors)


def test_the_producer_must_name_the_dispatched_role() -> None:
    forged = _text_errors(
        "planner", fields={"producer": "auditor", "next_agent": "backend"}
    )
    honest = _text_errors(
        "planner", fields={"producer": "planner", "next_agent": "backend"}
    )

    assert _mismatch(forged)
    assert not _mismatch(honest)


@pytest.mark.parametrize(("producer", "expected"), [("planner", False), ("user", False), ("auditor", True), ("backend", True)])
def test_a_chat_takeover_names_only_planner_or_user(producer: str, expected: bool) -> None:
    errors = _text_errors(
        "auditor",
        fields={"producer": producer, "next_agent": "planner"},
        chat_takeover=True,
    )

    assert _mismatch(errors) is expected


def test_a_dispatcher_authored_handoff_skips_only_the_identity_check() -> None:
    errors = _text_errors(
        "auditor",
        fields={"producer": "validator", "next_agent": "planner"},
        dispatcher_authored=True,
    )
    forged_route = _text_errors(
        "auditor",
        fields={
            "producer": "validator",
            "next_agent": "finalizer",
            "close_type": "epic",
            "scope_sha": SCOPE_SHA,
        },
        dispatcher_authored=True,
    )

    assert not _mismatch(errors)
    assert _producer_rule_errors(forged_route)


def test_a_handoff_cannot_route_back_to_the_dispatched_role() -> None:
    errors = _text_errors(
        "validator", fields={"producer": "validator", "next_agent": "validator"}
    )

    assert any(error.startswith("agent_self_loop:") for error in errors)


def test_a_finalizer_verdict_needs_a_finalizer_dispatch() -> None:
    fields = {"producer": "finalizer", "next_agent": "planner", "status": "verified_pass"}
    borrowed = _text_errors("auditor", fields=fields)
    carried = _text_errors("finalizer", fields=fields)

    assert any(
        error.startswith(STATUS_RULE) and "the dispatched role is `auditor`" in error
        for error in borrowed
    )
    assert any(error.startswith("EVIDENCE_MISSING:") for error in borrowed)
    assert not any(error.startswith(STATUS_RULE) for error in carried)
    assert not any(error.startswith("EVIDENCE_MISSING:") for error in carried)


# The table is the one source the helpers cite.


def test_runtime_validation_does_not_read_the_handbook() -> None:
    source = (REPO_ROOT / "llm_handoff" / "validator.py").read_text(encoding="utf-8")

    assert "HANDBOOK" not in source
    assert "Transition Table" not in source


NON_CLAUDE_VALIDATOR_HELPERS = (
    ".agents/agents/handoff-validator.md",
    ".codex/skills/llm-handoff-validator/SKILL.md",
)


@pytest.mark.parametrize("relative_path", NON_CLAUDE_VALIDATOR_HELPERS)
@pytest.mark.parametrize(
    "root", [REPO_ROOT, REPO_ROOT / "examples" / "reference-workflow"], ids=["root", "reference"]
)
def test_non_claude_validator_helpers_cite_the_table(root: Path, relative_path: str) -> None:
    content = (root / relative_path).read_text(encoding="utf-8")

    assert TABLE_ANCHOR in content
    assert "is one of `planner`, `backend`" not in content
    assert "routing is used only with `close_type: epic`" not in content
    assert "goes to `finalizer`" not in content
    assert "The public `next_agent` enum is:" not in content
    for line in content.splitlines():
        if line.startswith(("next_agent:", "close_type:")):
            assert "finalizer" not in line, line
            assert "epic" not in line, line
