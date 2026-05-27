#!/usr/bin/env bash
# Remove the make-interactive skill from Claude Code.
set -euo pipefail

SKILL_DIR="$HOME/.claude/commands/make-interactive"

if [[ -d "$SKILL_DIR" ]]; then
  rm -rf "$SKILL_DIR"
  echo "✓ Removed $SKILL_DIR"
else
  echo "ℹ Nothing to remove — $SKILL_DIR doesn't exist"
fi

echo ""
echo "Restart Claude Code so the skill list refreshes."
echo "Your .make-interactive-queue.json files (if any) are kept — they live next to your HTML."
