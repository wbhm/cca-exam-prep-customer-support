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
PRE_COMMIT_CONFIG = ROOT / ".pre-commit-config.yaml"
README = ROOT / "README.md"
HOOK = ROOT / ".claude" / "hooks" / "guard-git-push.sh"
LINT_HOOK = ROOT / ".claude" / "hooks" / "lint-on-edit.sh"


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

    def test_ignores_heredoc_text_that_mentions_git_commit(self) -> None:
        # The guard matches a git invocation, not the substring. Writing a README line
        # through a heredoc must not trigger the suite, or a red suite blocks its own fix.
        cmd = "cat > notes.md <<'EOF'\nRun the suite before every `git commit`.\nEOF"
        assert _run_hook(cmd, "false").returncode == 0

    def test_blocks_commit_with_git_options_before_the_verb(self) -> None:
        assert _run_hook("cd src && git -c user.name=x commit -m 'x'", "false").returncode == 2


def _run_lint_hook(file_path: Path) -> subprocess.CompletedProcess[str]:
    """Feed the lint hook the stdin JSON Claude Code sends after an Edit."""
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(file_path)}})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}
    return subprocess.run(
        [str(LINT_HOOK)], input=payload, capture_output=True, text=True, env=env, check=False
    )


class TestPostToolUseLintHook:
    """Report-only lint after every Edit/Write: exit 2 feeds ruff's output back to the model."""

    def test_project_settings_register_hook_on_edit_and_write(self) -> None:
        settings = json.loads(SETTINGS.read_text())
        entries = [e for e in settings["hooks"]["PostToolUse"] if e["matcher"] == "Edit|Write"]
        assert entries, "no PostToolUse hook registered for Edit|Write"
        commands = [h["command"] for e in entries for h in e["hooks"] if h["type"] == "command"]
        assert any("lint-on-edit.sh" in c for c in commands)

    def test_reports_lint_violation_with_exit_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.py"
        bad.write_text("import os\n")
        result = _run_lint_hook(bad)
        assert result.returncode == 2
        assert "F401" in result.stderr

    def test_clean_file_passes(self, tmp_path: Path) -> None:
        good = tmp_path / "good.py"
        good.write_text('"""Clean module."""\n\nX = 1\n')
        assert _run_lint_hook(good).returncode == 0

    def test_never_rewrites_the_file(self, tmp_path: Path) -> None:
        # Report-only by design: an autofix would leave the Edit tool's view of the file stale.
        bad = tmp_path / "bad.py"
        bad.write_text("import os\n")
        _run_lint_hook(bad)
        assert bad.read_text() == "import os\n"

    def test_ignores_non_python_files(self, tmp_path: Path) -> None:
        notes = tmp_path / "notes.md"
        notes.write_text("import os\n")
        assert _run_lint_hook(notes).returncode == 0


class TestGitPreCommitGuard:
    """The git-level layer: fires on any `git commit`, not only ones Claude Code issues."""

    def test_pre_commit_config_runs_the_test_suite(self) -> None:
        import yaml

        config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
        local_hooks = [h for r in config["repos"] if r["repo"] == "local" for h in r["hooks"]]
        pytest_hooks = [h for h in local_hooks if "pytest" in h["entry"]]
        assert pytest_hooks, "no local pre-commit hook runs pytest (lint-only config)"
        hook = pytest_hooks[0]
        assert hook.get("always_run") is True, "must run even when no .py file is staged"
        assert hook.get("pass_filenames") is False, "pytest must not receive staged filenames"

    def test_readme_tells_contributors_to_install_the_hook(self) -> None:
        text = README.read_text()
        assert "pre-commit install" in text
        assert "enforced on every commit" not in text, "a config is not enforcement until installed"
