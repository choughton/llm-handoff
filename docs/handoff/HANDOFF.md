---
next_agent: planner
reason: "W1.2 approved at story level; the planner should pick the next Phase 1 story."
epic_id: "SELFHOST-P1"
story_id: "W1.2"
story_title: "Isolate the supervised child from the target's working directory"
status: verified_pass
scope_sha: 7e40a7cf2a4d0068bba0f5a6a110d7f5b4323473
close_type: story
producer: auditor
---

# Audit Report - W1.2 (Isolate the supervised child from the target's working directory)

**Agent:** auditor
**Audit mode:** read-only fan-out (quality: claude, security: claude) on `20000b1eb5a82afa18658a7c0eb94813dcf930ec..7e40a7cf2a4d0068bba0f5a6a110d7f5b4323473`; the dispatcher wrote this handoff from the reviewers' structured verdicts.

## Verdict

APPROVED

## Merge Decision

- Quality reviewer verdict: pass
- Security reviewer verdict: pass
- Block severity threshold: high
- Blocking findings: 0
- Sub-threshold findings: 6

## Quality Review

W1.2 meets the Work Packet. The child is launched with -P before -m, cwd=repo_root is kept, a child-only env adds the source root to PYTHONPATH for source checkouts, and the required tests and CHANGELOG entry are present.

### Audit Report - W1.2 Isolate the supervised child from the target's working directory

#### Verdict
APPROVED WITH NITS

#### Checks Run
- `python -m pytest tests/test_supervisor.py -q`: NOT RUN. The Dispatcher Verification section shows Verdict PASS for 7e40a7c (exit 0), so I did not run it again.
- `git diff --stat 20000b1 7e40a7c`: PASS. Only 3 files changed (CHANGELOG.md, llm_handoff/supervisor.py, tests/test_supervisor.py), and all are in scope.
- `git rev-parse HEAD`: 7e40a7cf2a4d0068bba0f5a6a110d7f5b4323473, which matches scope_sha.

#### Phase 1 - Spec compliance
- Instruction 1 is met. `-P` comes right after sys.executable and before `-m` (llm_handoff/supervisor.py:615), and `cwd=config.repo_root` is kept (supervisor.py:626).
- Instruction 2 is met. `_child_dispatch_env` (supervisor.py:596-610) copies os.environ without changing it. Because the package is a namespace package, it falls back to the supervisor's own `__file__`. It puts the package parent first on PYTHONPATH unless that parent is site-packages or dist-packages, and keeps any existing PYTHONPATH after it.
- Instruction 3 is met. The tests cover:
  - the argv order, cwd and env (tests/test_supervisor.py:651-652);
  - the source-checkout env (test_supervisor.py:690);
  - the installed-location case (test_supervisor.py:714);
  - a real subprocess with a decoy package (test_supervisor.py:730).
- Instruction 4 is met: there is one ASCII line under Unreleased in CHANGELOG.md.
- Instruction 5 is met: `_child_dispatch_options` and `SupervisorConfig` are unchanged.
- There is no scope creep.

#### Phase 2 - Code quality
The change is small and readable. Nits only:
- The injected PYTHONPATH is passed on to the agent subprocesses the child starts (agent_process.py:251, 266).
- The source-env test assumes it runs from a source checkout (test_supervisor.py:718).
- The risk from llm_handoff being a namespace package was already there before this change.

#### Next Step
- **planner:** Record W1.2 as verified at story level and assign the next Phase 1 story.

### Quality Findings

- [low] llm_handoff/supervisor.py:596 - Injected PYTHONPATH leaks into agent subprocesses
  - Description: _child_dispatch_env sets PYTHONPATH on the dispatch child, and the agent processes it starts inherit it (agent_process.py:251, 266). Suppose the target repository is a different llm-handoff checkout and an agent there runs a console-script `pytest`. The supervisor's source root can then be imported ahead of the target's own llm_handoff.
  - Recommendation: Remove the injected entry from the env built for agent subprocesses, or document the behavior. A follow-up story is enough.
- [info] tests/test_supervisor.py:718 - Source-env test assumes a source checkout
  - Description: test_child_env_preserves_source_import_and_parent_environment expects the source root to be injected. If the suite runs against a non-editable site-packages install, nothing is injected and the test fails.
  - Recommendation: Monkeypatch llm_handoff.__file__ to a synthetic source path, or skip the test when the package parent is site-packages or dist-packages.
- [info] llm_handoff/supervisor.py:600 - Namespace-package precedence remains a residual hazard
  - Description: The source tree has no __init__.py, so it is a namespace package. A regular llm_handoff package anywhere later on sys.path still takes priority over it. -P closes the cwd route but not this one. The issue existed before this change.
  - Recommendation: Consider adding llm_handoff/__init__.py in a separate story.

## Security Review

The W1.2 change hardens the child launch: `-P` plus an explicit import root stop a target repo's `llm_handoff` from being imported. No exploitable defect was introduced; three low/info notes remain.

## Security Notes

- [low] llm_handoff/supervisor.py:605 - Inherited PYTHONPATH entries still resolve against the target cwd
  - Description: _child_dispatch_env adds the operator's existing PYTHONPATH after the source import root. In installed mode it passes PYTHONPATH through unchanged. The `-P` flag only stops Python from adding the script or cwd directory to sys.path. A relative entry such as '.' or 'src' in the inherited PYTHONPATH is still resolved against cwd=config.repo_root. In source mode the package root comes first, so `llm_handoff` itself resolves correctly, but other modules (yaml, stdlib-shadowing names, or llm_handoff in installed mode) could be loaded from the untrusted target repository if the operator's environment has such entries. This requires a misconfigured operator environment, so it is low severity.
  - Recommendation: Optionally drop or absolutize relative or empty PYTHONPATH entries (resolve them against the supervisor's own cwd) before passing them to the child, or document that PYTHONPATH must contain only absolute paths.
- [info] llm_handoff/supervisor.py:34 - Installed-vs-source detection depends on the parent directory name
  - Description: Source mode is detected when the package's parent directory is not named site-packages or dist-packages. Other layouts, such as zipapps, custom --target/--prefix directories, or vendored copies, are treated as source mode. In those cases the package's parent directory is put first on PYTHONPATH. That directory is the supervisor's own install location, not the target repo, so this is not a security issue. It only means unrelated sibling modules in that directory become importable by the child.
  - Recommendation: No action required. Consider using importlib.metadata or a check against sysconfig paths if the detection needs to be more precise.
- [info] llm_handoff/supervisor.py:616 - `-P` requires Python 3.11+; full parent environment (including API keys) is forwarded
  - Description: `-P` (safe path) only exists in Python 3.11+, which matches the documented minimum in INSTALL.md:12. Older interpreters fail closed: they reject the option instead of ignoring it. The child now gets an explicit copy of os.environ, which includes secrets such as OPENAI_API_KEY (the test checks this). This matches the earlier implicit inheritance and is needed by the child dispatcher, so nothing new is exposed.
  - Recommendation: No change needed. Consider declaring requires-python >= 3.11 in package metadata once a pyproject is added.

## Verification Evidence

- **Commands run:** cd /c/Users/hough/GitHub/llm-handoff && git rev-parse HEAD && ls llm_handoff | head -30 && grep -n "pythonpath\|testpaths\|addopts\|packages" pyproject.toml; grep -n "_load_supervisor_module" -A8 tests/test_supervisor.py | head -15; git diff --stat 20000b1 7e40a7c; cd /c/Users/hough/GitHub/llm-handoff && grep -n "_child_dispatch_env\|\"-P\"\|^import llm_handoff" llm_handoff/supervisor.py; grep -n "def test_child_\|popen_options\[0\]" tests/test_supervisor.py; grep -rn "PYTHONPATH\|env=" llm_handoff/agent_process.py | head; security reviewer: claude --permission-mode dontAsk --tools Read,Grep,Glob,Bash --strict-mcp-config --allowedTools Read Grep Glob Bash(git diff *) Bash(git show *) Bash(git log *) --disallowedTools Edit Write MultiEdit NotebookEdit WebFetch WebSearch --model claude-opus-5-5 --output-format json --json-schema <long-arg> --no-session-persistence -p <long-arg>
- **Output summary:** quality reviewer (claude) exit 0, verdict pass, 3 finding(s), 77.9s; security reviewer (claude) exit 0, verdict pass, 3 finding(s), 29.4s; Command 1: exit 0 overall. The grep on pyproject.toml failed because the file does not exist, but the chain continued and the final git diff --stat succeeded. HEAD is 7e40a7cf2a4d0068bba0f5a6a110d7f5b4323473. llm_handoff has no __init__.py, so it is a namespace package. The diff touches 3 files (+92). Command 2: exit 0. It found the cited lines: supervisor.py:24, 596, 615, 627; test_supervisor.py:651-652, 690, 714, 730; and the env pass-through in agent_process.py:251, 266.
- **Commit SHA verified:** 7e40a7cf2a4d0068bba0f5a6a110d7f5b4323473
- **Files changed or reviewed:** CHANGELOG.md, llm_handoff/supervisor.py, tests/test_supervisor.py
- **Unresolved concerns:** none

## Next Step

- **planner:** W1.2 approved at story level; the planner should pick the next Phase 1 story.
