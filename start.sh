#!/bin/bash
# zo-hermes start script — registered as Zo user service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_VENV="/opt/hermes-agent/venv"

# Load secrets (API keys, tokens)
if [ -f /root/.zo_secrets ]; then
    source /root/.zo_secrets
fi

# Activate Hermes venv (has all deps including AIAgent)
source "$HERMES_VENV/bin/activate"

# Ensure FastAPI deps are available
pip install --quiet --break-system-packages fastapi uvicorn 2>/dev/null || true

# Run from workspace dir (Hermes CWD)
cd /home/workspace

exec python "$SCRIPT_DIR/launch_server.py"
