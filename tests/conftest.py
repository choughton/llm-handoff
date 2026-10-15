from __future__ import annotations

import pytest

import llm_handoff.agents as agents_facade
from llm_handoff.agent_providers import gemini as gemini_provider


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


@pytest.fixture(autouse=True)
def _gemini_sandbox_runtime_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gemini launches fail closed when no Docker/Podman is on PATH. Tests run
    # on machines without a container runtime, so pretend one exists; tests of
    # the fail-closed path override this.
    monkeypatch.setattr(
        gemini_provider,
        "detect_gemini_sandbox_runtime",
        lambda: "docker",
    )
