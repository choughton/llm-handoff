from __future__ import annotations

from pathlib import Path, PurePath
import shutil
import subprocess
import tempfile

import pytest

import llm_handoff.agents as agents_facade
from llm_handoff import audit_review


# `gemini` stays listed so a real Gemini CLI on PATH cannot leak into tests.
_PROVIDER_CLI_NAMES = frozenset({"claude", "codex", "gemini", "agy", "codex-security"})
_REAL_POPEN_INIT = subprocess.Popen.__init__


def _launched_executable(args: object) -> str | None:
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    if isinstance(args, str) and args.strip():
        return args.strip().split()[0].strip('"')
    return None


@pytest.fixture(autouse=True)
def _no_real_provider_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # A test that reaches a real provider CLI is slow (seconds per call),
    # spends the developer's tokens, and depends on their sign-in. Fake
    # binaries that tests create under the temp directory are still allowed.
    temp_root = Path(tempfile.gettempdir()).resolve()

    def guarded_init(self, args, *init_args, **init_kwargs):
        executable = _launched_executable(args)
        if executable is not None:
            stem = PurePath(executable).stem.lower()
            if stem in _PROVIDER_CLI_NAMES:
                resolved = shutil.which(executable) or executable
                try:
                    under_temp = Path(resolved).resolve().is_relative_to(temp_root)
                except OSError:
                    under_temp = False
                if not under_temp:
                    raise RuntimeError(
                        f"test tried to launch the real `{stem}` CLI ({resolved}); "
                        "stub the agent call instead."
                    )
        _REAL_POPEN_INIT(self, args, *init_args, **init_kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)


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
    agents_facade._sync_codex()
    agents_facade._sync_claude()
    agents_facade._sync_manual()


@pytest.fixture(autouse=True)
def _security_reviewer_cli_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # The security reviewer is on by default, and the dispatcher stops at
    # startup when no reviewer CLI is on PATH. Tests run on machines without
    # the provider CLIs, so pretend each one is installed; tests of the
    # PATH lookup and the startup failure override this.
    monkeypatch.setattr(
        audit_review,
        "_reviewer_cli_available",
        lambda _config, _provider: True,
    )
