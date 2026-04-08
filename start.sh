#!/bin/bash
# zo-hermes start script — registered as Zo user service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_ROOT="${HERMES_ROOT:-/opt/hermes-agent}"
HERMES_VENV="${HERMES_VENV:-$HERMES_ROOT/venv}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_CWD="${HERMES_CWD:-$HOME}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HERMES_HOME/.env}"

export HERMES_ROOT HERMES_HOME HERMES_CWD HERMES_ENV_FILE

# Load secrets (API keys, tokens)
if [ -f "$HERMES_ENV_FILE" ]; then
    set -a
    source "$HERMES_ENV_FILE"
    set +a
fi

# Activate Hermes venv (has all deps including AIAgent)
source "$HERMES_VENV/bin/activate"

# Ensure FastAPI deps are available
pip install --quiet --break-system-packages fastapi uvicorn 2>/dev/null || true

# Run from configured Hermes working dir
mkdir -p "$HERMES_HOME"
cd "$HERMES_CWD"

exec python "$SCRIPT_DIR/launch_server.py"
