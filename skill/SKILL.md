---
name: zo-hermes
description: Install, configure, operate, and debug zo-hermes on Zo Computer so zo-discord, zo-dispatcher, and other explicit callers can run on Hermes through a Zo-compatible API.
compatibility: Created for Zo Computer
metadata:
  author: jackal.zo.computer
---

# zo-hermes

Use this skill when setting up or operating `zo-hermes` on Zo Computer.

## Setup

### Prerequisites

- a Zo Computer account
- Python 3.10+
- Hermes Agent installed and working
- provider access for the model you want Hermes Agent to use
- a local checkout of the `zo-hermes` repo

### Configure Hermes model/provider auth

Before starting `zo-hermes`, configure Hermes Agent itself in the Zo terminal.

Use:

```bash
hermes model
```

That flow persists model/provider settings to `~/.hermes/config.yaml`. Use it for provider auth and endpoint selection, including flows like Codex OAuth device login.

After setup, verify that Hermes has a usable default model in `~/.hermes/config.yaml`.

### Zo environment parity

Run `zo-hermes` with:

- `HERMES_CWD=/home/workspace`

This matters because it gives Hermes the same working tree and file access that the Zo agent has. In practice it means Hermes can see the same project files, `AGENTS.md`, and workspace state that Zo can.

`zo-hermes` also aligns `TERMINAL_CWD` to `HERMES_CWD` at startup so Hermes context-file discovery resolves against the workspace instead of the hermes-agent install directory.

No `AGENTS.md` symlink is needed. When Hermes runs with `HERMES_CWD=/home/workspace`, it will pick up `/home/workspace/AGENTS.md` automatically.

### Symlinks for Zo parity

Recommended symlinks:

```bash
ln -sfn /root/.zo_secrets ~/.hermes/.env
ln -sfn /home/workspace/SOUL.md ~/.hermes/SOUL.md
ln -sfn /home/workspace/Skills ~/.hermes/skills
```

Why:

- `~/.hermes/.env -> /root/.zo_secrets` lets Hermes read the same secrets as Zo services
- `~/.hermes/SOUL.md` should point to whichever `SOUL.md` file you want Hermes to use. `/home/workspace/SOUL.md` is the default assumption, but use your real SOUL path if it lives elsewhere.
- `~/.hermes/skills -> /home/workspace/Skills` makes Zo skills appear in Hermes Agent's system prompt `available_skills` list, so Hermes is aware they exist

If `~/.hermes/skills` already contains standalone Hermes skills, back it up or merge carefully before replacing it with a symlink.

### Important Zo-specific limits

- Zo personas are not stored locally, so they cannot be mirrored into `~/.hermes/personalities/`. Zo `persona_id` values are accepted by `zo-hermes` for compatibility but are ignored semantically.
- Zo Rules are not stored locally. They are instructions provided to the Zo agent through its system prompt, so there is nothing to symlink into Hermes. If you want equivalent behavior in Hermes, include the important rules in `AGENTS.md`.

## Config Shape

`zo-hermes` has no separate config file. It uses:

- upstream Hermes config in `~/.hermes/config.yaml`
- upstream Hermes secrets in `~/.hermes/.env`, which on Zo should usually symlink to `/root/.zo_secrets`
- a few bridge process env vars

Required `config.yaml` values:

- `model.default`
- `agent.max_turns`

`zo-hermes` fails fast at startup if either of these is missing or invalid.

Bridge env vars:

- `HERMES_API_PORT` — local listen port, default `8788`
- `HERMES_CWD` — working directory, should be `/home/workspace`
- `HERMES_HOME` — Hermes home, default `~/.hermes`

Default resolution order:

1. request field on `/ask`
2. upstream Hermes config in `~/.hermes/config.yaml`

If `/ask` omits `model_name` or `max_iterations`, zo-hermes will take the values from `config.yaml`. If `config.yaml` does not define them, zo-hermes will not start.

Known compatibility gaps:

- `persona_id` is accepted but ignored; there is no request-time mapping from Zo personas to Hermes personalities
- the bridge intentionally does not emit `ProgressEvent`; tool noise is filtered down to commentary-style output
- the bridge currently instantiates Hermes with `platform="discord"` for all callers
- `zo-hermes` does not enable native Zo channels or native Zo agents to use Hermes automatically

## Zo MCP

### Auth token

To let Hermes use Zo Computer admin tools through MCP:

1. Create an access token in **Zo Settings → Advanced → Access Tokens**
2. Save it as a secret named:
   - `HERMES_ZO_ACCESS_TOKEN`
3. Reference it from `~/.hermes/config.yaml`

Use:

- MCP endpoint: `https://api.zo.computer/mcp`
- auth header: `Authorization: Bearer ${HERMES_ZO_ACCESS_TOKEN}`

Do **not** use `ZO_CLIENT_IDENTITY_TOKEN`. That is a per-conversation session token, not the right token for a persistent service.

### Minimal config

```yaml
mcp_servers:
  zo:
    url: https://api.zo.computer/mcp
    headers:
      Authorization: Bearer ${HERMES_ZO_ACCESS_TOKEN}
```

### Default Zo MCP tool policy

If `mcp_servers.zo.tools` is omitted, `zo-hermes` injects a narrowed default include list at runtime. The goal is to give Hermes the Zo Computer account, service, and space management actions it needs without overlapping with tools Hermes already has for normal coding, files, and browser work.

Current default include groups:

- **Service admin**: `change_hardware`, `list_user_services`, `register_user_service`, `update_user_service`, `delete_user_service`, `service_doctor`, `proxy_local_service`
- **Space admin**: `create_website`, `list_space_routes`, `get_space_route`, `update_space_route`, `delete_space_route`, `list_space_assets`, `update_space_asset`, `delete_space_asset`, `get_space_errors`
- **Account settings**: `update_user_settings`

If you define `mcp_servers.zo.tools` explicitly, `zo-hermes` leaves it alone.

## Service Registration

Register `zo-hermes` as a Zo user service, not as a raw supervisord process, using `register_user_service`. This ensures that the process survives Zo server restarts and has proper log files in `/dev/shm/`.

### Registration shape

Register `zo-hermes` with:

- label: `zo-hermes`
- protocol: `http`
- local port: `8788`
- entrypoint: the repo's `start.sh`
- workdir: the repo checkout directory

Example shape:

```text
Register zo-hermes as a user service with:
- label: zo-hermes
- protocol: http
- local_port: 8788
- entrypoint: /absolute/path/to/zo-hermes/start.sh
- workdir: /absolute/path/to/zo-hermes
```

`start.sh` should:

- source `/root/.zo_secrets`
- activate `/opt/hermes-agent/venv`
- run `server.py`

For config or code changes, use `update_user_service`. Hosted services restart automatically when updated, so that applies the change and restarts the service in one step.

If the service appears to still be running old code after an update, run `service_doctor`. It can detect stale-code situations and is the first check before assuming the update failed.

Do not kill the process manually with `kill` or `pkill`; Zo will auto-restart it and you can end up with duplicate processes.

## Direct Usage

Non-streaming:

```bash
curl -sS http://127.0.0.1:8788/ask \
  -H 'Content-Type: application/json' \
  -d '{"input":"Say hello","stream":false}'
```

Streaming:

```bash
curl -N http://127.0.0.1:8788/ask \
  -H 'Content-Type: application/json' \
  -d '{"input":"Say hello","stream":true}'
```

Cancel:

```bash
curl -sS http://127.0.0.1:8788/cancel \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"20260318_195614_48202b37"}'
```

## Verification

Health check:

```bash
curl -sS http://127.0.0.1:8788/health
```

Targeted test file:

```bash
pytest tests/test_server.py
```

Good end-to-end smoke test:

1. send a simple `/ask` request
2. confirm you get a response body
3. confirm `conversation_id` or `X-Conversation-Id` comes back
4. send a follow-up using that session ID
5. confirm continuity works

## Operational Checks

### Health and logs

Health:

```bash
curl -sS http://127.0.0.1:8788/health
```

Logs:

```bash
tail -n 200 /dev/shm/zo-hermes.log
tail -n 200 /dev/shm/zo-hermes_err.log
```

### Tests

```bash
pytest tests/test_server.py
```

### Service behavior to check after edits

- service still starts cleanly
- `/health` returns `ok`
- `/ask` still streams and non-streams correctly
- `conversation_id` continuity still works
- `persona_id` warning behavior still works
- Zo MCP defaults still match the intended narrowed policy

### Auto-Update Agent

If this repo includes `assets/hermes-auto-update-agent-template.md`, treat it as a disabled template for a `zo-dispatcher` schedule that updates Hermes only when the bridge is idle, then restarts and health-checks the service.

Before enabling it:

1. fill in the Hermes repo path
2. fill in the Zo service ID for `zo-hermes`
3. fill in the bridge port and notification channel
4. keep it disabled until those values are reviewed

## Guardrails

- Do not describe `zo-hermes` as a native Zo feature. It is a custom bridge.
- Do not imply native Zo channels or native Zo agents will automatically start using Hermes.
- Do not imply Zo `persona_id` values work on Hermes today. They do not.
- Do not use `ZO_CLIENT_IDENTITY_TOKEN` for Zo MCP auth.
- Do not run the bridge outside `/home/workspace` if you want file access parity with Zo.
- Do not edit `/opt/hermes-agent/` unless the task explicitly requires an upstream Hermes patch.
- If you change the Zo MCP default include list, update the docs to explain both the included tools and the reason for narrowing.
- If you change stream behavior, preserve the intentional choice to filter tool-call noise instead of reintroducing raw `ProgressEvent` spam.
