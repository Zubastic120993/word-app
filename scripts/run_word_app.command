#!/bin/bash
set -euo pipefail

# Word App local launcher (macOS .command).
# Double-click to start the server and open the browser.

PROJECT_ROOT="/Users/vladymyrzub/Desktop/word_app"
cd "$PROJECT_ROOT"

# Activate virtualenv if present (preferred: .venv, fallback: venv)
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
elif [ -f "venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "venv/bin/activate"
fi

echo "Starting Word App from: $PROJECT_ROOT"
echo "Opening: http://127.0.0.1:8000"

# Run server, open browser shortly after start, and keep logs visible.
python main.py &
APP_PID=$!

(
  sleep 1
  open "http://127.0.0.1:8000" >/dev/null 2>&1 || true
) &

wait "$APP_PID" || true
echo ""
echo "Word App stopped. Press Enter to close this window."
read -r _
