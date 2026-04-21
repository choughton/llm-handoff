from __future__ import annotations

from llm_handoff import __main__ as main_module


def test_help_text_names_public_dispatcher(capsys) -> None:
    exit_code = main_module.main(["--help"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "llm-handoff dispatcher" in output
    assert "--manual-frontend" in output
    assert "Crossfire" not in output
    assert "antigravity" not in output.lower()
