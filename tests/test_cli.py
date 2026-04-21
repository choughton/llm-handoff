from __future__ import annotations

from llm_handoff import __main__ as main_module


LEGACY_PROJECT_NAME = "Cross" + "fire"
LEGACY_TOOL_NAME = "anti" + "gravity"


def test_help_text_names_public_dispatcher(capsys) -> None:
    exit_code = main_module.main(["--help"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "llm-handoff dispatcher" in output
    assert "--manual-frontend" in output
    assert "--config" in output
    assert LEGACY_PROJECT_NAME not in output
    assert LEGACY_TOOL_NAME not in output.lower()
