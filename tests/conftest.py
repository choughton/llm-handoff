from __future__ import annotations

import pytest

import llm_handoff.agents as agents_facade


@pytest.fixture(autouse=True)
def _resync_agent_facade() -> None:
    # ``llm_handoff.agents`` re-binds provider-module globals (for example
    # ``agent_providers.codex._run_command_streaming``) to whatever the facade
    # module currently holds every time a facade entry point runs. A test that
    # monkeypatches a facade global and then calls through the facade leaves
    # the provider modules pointing at the fake after monkeypatch undo, which
    # silently changes later tests that exercise the provider modules
    # directly. Re-sync before each test so every test starts from the real
    # implementations.
    agents_facade._sync_gemini()
    agents_facade._sync_codex()
    agents_facade._sync_claude()
    agents_facade._sync_manual()
