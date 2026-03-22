---
name: zo-hermes
description: Operate and maintain the local zo-hermes bridge that exposes Hermes through a Zo-compatible API. Use when setting up, debugging, or maintaining zo-hermes, or when creating the Hermes auto-update agent.
compatibility: Created for Zo Computer
metadata:
  author: jackal.zo.computer
---

# zo-hermes

Use this skill when working on the local `zo-hermes` bridge service that sits in front of Hermes and provides a Zo-compatible `/ask` API for `zo-discord` and `zo-dispatcher`.

## Read first

- `README.md` for architecture, endpoint contract, environment variables, and operational notes
- `server.py` for the actual bridge behavior

## Key paths

- Service code: `Services/zo-hermes/server.py`
- Entrypoint: `Services/zo-hermes/start.sh`
- Tests: `Services/zo-hermes/tests/test_server.py`
- Auto-update agent template: `Services/zo-hermes/skill/assets/hermes-auto-update-agent-template.md`

## Auto-update agent

The template at `Services/zo-hermes/skill/assets/hermes-auto-update-agent-template.md` is the source artifact for a disabled `zo-dispatcher` schedule that keeps a local Hermes install current without restarting the bridge during live traffic.

Before using it:

1. Fill in the local Hermes repo path.
2. Fill in the Zo service ID for the bridge service.
3. Fill in the bridge port and notification channel.
4. Leave the agent disabled until the values are reviewed.

## Guardrails

- Do not modify `/opt/hermes-agent/` directly unless the task explicitly requires upstream Hermes changes.
- Prefer verifying with the local test suite before declaring bridge changes done.
- Treat `127.0.0.1` access and service restarts as operational details of the bridge, not user-facing setup.
