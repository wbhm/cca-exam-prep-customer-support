#!/usr/bin/env bash
# PostToolUse hook (matcher: Edit|Write) registered in .claude/settings.json.
#
# Lints the Python file that was just written with the project's ruff settings.
# Report-only by design: the hook never rewrites the file. An autofix here would
# leave the Edit tool's view of the file stale for the next edit. Instead,
# exit 2 feeds ruff's output back to the model as feedback, and the model makes
# the fix through a normal edit.
#
# Contract (Claude Code hooks, PostToolUse):
#   stdin  - JSON with .tool_input.file_path (and .tool_response.filePath)
#   exit 0 - nothing to report
#   exit 2 - the tool already ran; stderr is fed back to the model
set -u

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_response.filePath // empty')

case "$FILE" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$FILE" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 0

if OUTPUT=$({ poetry run ruff check "$FILE" && poetry run ruff format --check "$FILE"; } 2>&1); then
  exit 0
fi

{
  echo "ruff reported problems in $FILE. Fix them before moving on."
  printf '%s\n' "$OUTPUT" | tail -n 30
} >&2
exit 2
