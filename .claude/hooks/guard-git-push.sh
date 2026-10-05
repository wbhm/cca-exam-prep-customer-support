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
#
# Limitation: the hook sees only the command text. A commit issued from inside
# another program (a Python subprocess, a Makefile) is invisible to it. The git
# pre-commit hook in .pre-commit-config.yaml covers that path.
set -u

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')

# Match a git invocation at the start of a line or after a command separator,
# with any options between `git` and the verb. A mention of "git commit" inside
# a heredoc body or a quoted string is not an invocation and must not match.
GIT_VERB='(^|[;&|(]|\$\()[[:space:]]*(command[[:space:]]+)?git([[:space:]]+[^[:space:]]+)*[[:space:]]+(commit|push)([[:space:]]|$)'
if ! printf '%s\n' "$COMMAND" | grep -Eq "$GIT_VERB"; then
  exit 0
fi

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
