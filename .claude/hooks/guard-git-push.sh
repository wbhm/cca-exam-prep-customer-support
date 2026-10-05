#!/usr/bin/env bash
# PreToolUse hook (matcher: Bash) registered in .claude/settings.json.
#
# Blocks `git commit` and `git push` when the test suite fails. This is the
# harness-level form of the project's first rule: programmatic enforcement
# beats prompt guidance. A CLAUDE.md line saying "run tests before committing"
# is advice; this hook is a gate.
#
# Contract (Claude Code hooks):
#   stdin  - JSON with .tool_input.command (the Bash command about to run)
#   exit 0 - no opinion; the normal permission flow applies
#   exit 2 - block the tool call; stderr is fed back to the model as feedback
#
# CCA_GUARD_TEST_CMD overrides the test command so the hook's own tests can
# substitute `true` / `false` instead of recursing into the full suite.
set -u

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')

case "$COMMAND" in
  *"git commit"* | *"git push"*) ;;
  *) exit 0 ;;
esac

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 0

TEST_CMD="${CCA_GUARD_TEST_CMD:-poetry run pytest -q -x}"
if OUTPUT=$(bash -c "$TEST_CMD" 2>&1); then
  exit 0
fi

{
  echo "Blocked: the test suite fails. Fix the tests before git commit / git push."
  echo "Command: $TEST_CMD"
  printf '%s\n' "$OUTPUT" | tail -n 15
} >&2
exit 2
