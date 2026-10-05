"""Harness guards: Section 13 of the review skill, applied to this repo.

The guard is a Claude Code PreToolUse hook on Bash that runs the test suite and
exits 2 (block) when a ``git commit`` or ``git push`` is attempted with a red
suite. These tests run the hook script directly with the test command replaced
by ``true`` / ``false``, so the suite does not recurse into itself.
"""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / ".claude" / "settings.json"
HOOK = ROOT / ".claude" / "hooks" / "guard-git-push.sh"


def _run_hook(command: str, test_cmd: str) -> subprocess.CompletedProcess[str]:
    """Feed the hook the stdin JSON Claude Code sends for a Bash call."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "CCA_GUARD_TEST_CMD": test_cmd, "CLAUDE_PROJECT_DIR": str(ROOT)}
    return subprocess.run(
        [str(HOOK)], input=payload, capture_output=True, text=True, env=env, check=False
    )


class TestPreToolUseGuardIsConfigured:
    """Structural: the hook is registered in the committed project settings."""

    def test_project_settings_register_hook_on_bash(self) -> None:
        settings = json.loads(SETTINGS.read_text())
        entries = [e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == "Bash"]
        assert entries, "no PreToolUse hook registered for Bash"
        commands = [h["command"] for e in entries for h in e["hooks"] if h["type"] == "command"]
        assert any("guard-git-push.sh" in c for c in commands)

    def test_hook_script_is_executable(self) -> None:
        assert HOOK.exists()
        assert os.access(HOOK, os.X_OK)


class TestPreToolUseGuardBehavior:
    """Behavioral: the actual runtime path, exit code 2 blocks the tool call."""

    def test_blocks_commit_when_tests_fail(self) -> None:
        result = _run_hook("git add -A && git commit -m 'x'", "false")
        assert result.returncode == 2
        assert "Blocked" in result.stderr

    def test_blocks_push_when_tests_fail(self) -> None:
        assert _run_hook("git push origin main", "false").returncode == 2

    def test_allows_commit_when_tests_pass(self) -> None:
        assert _run_hook("git commit -m 'x'", "true").returncode == 0

    def test_ignores_commands_that_are_not_commit_or_push(self) -> None:
        # The suite must not run for other commands, so a failing test command is irrelevant.
        assert _run_hook("git status && ls", "false").returncode == 0
