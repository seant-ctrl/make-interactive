#!/usr/bin/env bash
# make-interactive installer
# Works both ways:
#   1. ./install.sh from a cloned repo  → copies adjacent files
#   2. curl ... | bash                  → fetches files from GitHub raw
set -euo pipefail

SKILL_NAME="make-interactive"
SKILL_DIR="$HOME/.claude/commands/$SKILL_NAME"
FILES=(SKILL.md server.py overlay.js overlay.css)
REPO_RAW="https://raw.githubusercontent.com/seant-ctrl/make-interactive/main"

# --- preflight ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 is required (3.7+). Install it first." >&2
  exit 1
fi

PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "→ python3 $PYV detected"

mkdir -p "$SKILL_DIR"

# --- detect mode: local clone vs curl pipe ---
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/SKILL.md" && -f "$SCRIPT_DIR/server.py" ]]; then
  echo "→ Installing from local directory: $SCRIPT_DIR"
  for f in "${FILES[@]}"; do
    cp "$SCRIPT_DIR/$f" "$SKILL_DIR/$f"
    echo "  ✓ $f"
  done
else
  echo "→ Downloading from $REPO_RAW"
  for f in "${FILES[@]}"; do
    if curl -fsSL "$REPO_RAW/$f" -o "$SKILL_DIR/$f"; then
      echo "  ✓ $f"
    else
      echo "  ✗ failed to fetch $f from $REPO_RAW" >&2
      exit 1
    fi
  done
fi

# --- done ---
echo ""
echo "✓ make-interactive installed to $SKILL_DIR"
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code so it picks up the new skill"
echo "  2. Run:  /make-interactive path/to/your-file.html"
echo ""
echo "Triggers (natural phrases that invoke it):"
echo "  • \"make-interactive\""
echo "  • \"rend cette page interactive\""
echo "  • \"comment on this page\""
echo "  • \"iterate on this HTML\""
echo ""
echo "Uninstall later: curl -fsSL $REPO_RAW/uninstall.sh | bash"
