# zo-hermes

Thin FastAPI bridge that wraps the [Hermes AIAgent](file:///opt/hermes-agent/) and exposes it as a Zo-compatible API. This lets zo-discord (and anything else that speaks the Zo `/zo/ask` protocol) talk to a self-hosted Hermes agent instead of the Zo API.

## Why it exists

Zo's native `/zo/ask` endpoint runs on Zo's infrastructure. zo-hermes runs the same Hermes agent locally on the Zo server, giving full control over model selection, iteration limits, toolsets, and reasoning config — while emitting SSE events in the exact same format so zo-discord's parsing works unchanged.

## Architecture

```
Discord user
    ↓
zo-discord (bot.py)
    ↓  POST /ask?stream=true
zo-hermes (server.py:8788)
    ↓  runs in threadpool
Hermes AIAgent (/opt/hermes-agent/run_agent.py)
    ↓  Anthropic API calls
Codex/Hermes runtime (model specified in config)
```

- **zo-hermes** is a FastAPI app running on `localhost:8788` (configurable via `HERMES_API_PORT`)
- **Hermes agent code** lives at `/opt/hermes-agent/` — this is read-only upstream code, not to be modified
- **Session state** is stored in Hermes's SQLite DB via `SessionDB` (from `hermes_state`)
- **Provider resolution** uses `hermes_cli.runtime_provider.resolve_runtime_provider()` to get Claude Code OAuth tokens

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ask` | Main endpoint. Supports `stream=true` (SSE) and non-streaming (JSON). |
| `POST` | `/cancel` | Cancel an in-flight session by `session_id`. |
| `GET` | `/health` | Health check. Returns `{"status": "ok"}`. |
| `GET` | `/status?session_id=X` | Session state: running/idle, iteration count, model, token usage. |
| `GET` | `/usage?session_id=X` | Detailed token usage, cost estimate, context window utilization. |
| `POST` | `/undo` | Remove last user+assistant exchange from a session transcript. |
| `POST` | `/compress` | Force context compression on a session. |
| `POST` | `/clarify-response` | Provide user's response to a pending clarify question. Body: `{session_id, response}`. |

### `/ask` request body

```json
{
  "input": "user message",
  "stream": true,
  "conversation_id": "optional session ID to continue",
  "model_name": "gpt-5.4",
  "max_iterations": 90,
  "reasoning_effort": "medium",
  "skip_memory": false,
  "skip_context": false,
  "enabled_toolsets": ["web", "terminal"],
  "disabled_toolsets": ["dangerous"]
}
```

- `conversation_id` and `session_id` are aliases (both accepted)
- BYOK model IDs (`byok:xxx`) are ignored — defaults to `HERMES_DEFAULT_MODEL`
- MCP toolsets are server-specific. For Zo MCP, use `["zo"]` or `["mcp-zo"]`. Bare `["mcp"]` is treated as an alias for all configured MCP servers.
- `zo-hermes` applies a default filter to the `zo` MCP server when no explicit `mcp_servers.zo.tools` policy exists yet, so Hermes only sees a narrow Zo-specific admin surface by default.
- Streaming returns SSE events matching Zo's format: `PartStartEvent`, `PartDeltaEvent`, `PartEndEvent`, `End`

## Default Zo MCP policy

When `~/.hermes/config.yaml` contains a `zo` MCP server with no explicit `tools` policy, `zo-hermes` injects this default:

```yaml
mcp_servers:
  zo:
    url: "https://api.zo.computer/mcp"
    headers:
      Authorization: "Bearer ${HERMES_ZO_ACCESS_TOKEN}"
    tools:
      include:
        - change_hardware
        - list_user_services
        - register_user_service
        - update_user_service
        - delete_user_service
        - service_doctor
        - proxy_local_service
        - create_website
        - list_space_routes
        - get_space_route
        - update_space_route
        - delete_space_route
        - list_space_assets
        - update_space_asset
        - delete_space_asset
        - get_space_errors
        - update_user_settings
      resources: false
      prompts: false
```

This keeps Zo MCP focused on the Zo-specific operations Hermes does not already provide natively, and trims overlapping shell, file, browser, search, media, messaging, and built-in Zo agent-management tools from the default tool surface.

## Overriding the default

If you want a different Zo MCP surface, set `mcp_servers.zo.tools` yourself in `~/.hermes/config.yaml`. `zo-hermes` only applies the default when that key is absent.

Example override:

```yaml
mcp_servers:
  zo:
    url: "https://api.zo.computer/mcp"
    headers:
      Authorization: "Bearer ${HERMES_ZO_ACCESS_TOKEN}"
    tools:
      include:
        - update_user_service
        - service_doctor
        - get_space_errors
      resources: false
      prompts: false
```

## SSE event format (streaming mode)

Matches Zo-native SSE so zo-discord can consume both backends without changes:

- **Thinking**: `PartStartEvent {part: {part_kind: "thinking", content: "full block"}}` → `PartEndEvent {}`
- **Text deltas**: `PartStartEvent {part: {part_kind: "text"}}` → `PartDeltaEvent {delta: {content_delta: "..."}}` (repeated) → `PartEndEvent {}`
- **Clarify**: `ClarifyEvent {question: "...", choices: [...], session_id: "..."}` — emitted when agent calls the clarify tool. The agent thread blocks until a response is posted to `POST /clarify-response`.
- **End**: `End {data: {output: "full response", conversation_id: "session_id"}}`

### Thinking dedup

Hermes's `AIAgent` calls the `reasoning_callback` from **two code paths** per reasoning block:
1. `_fire_reasoning_delta` — per-token streaming deltas (small fragments)
2. `_build_assistant_message` — full block text after each API call completes

zo-hermes deduplicates by accumulating deltas silently and only forwarding the full-text delivery (detected by prefix match against accumulated content). This means zo-discord receives one clean thinking message per reasoning block rather than duplicates.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_API_PORT` | `8788` | Port to listen on |
| `HERMES_DEFAULT_MODEL` | `gpt-5.4` | Model when none specified or BYOK ID received |
| `HERMES_MAX_ITERATIONS` | `90` | Max agent iterations per request |
| `HERMES_CWD` | `/home/workspace` | Working directory for the agent |

Secrets (API keys, OAuth tokens) are loaded from `/root/.zo_secrets` at startup via `start.sh`.

## Session IDs

Format: `YYYYMMDD_HHMMSS_hex8` (e.g., `20260318_195614_48202b37`).

Sessions persist across requests via `conversation_id`. After context compression, the session ID may change — the new ID is returned in the `X-Conversation-Id` header and the `End` SSE event.

The `pass_session_id=True` flag is set so the agent can see its own session ID in the system prompt.

## Running

Registered as a Zo user service (service ID: `svc_bInt4_9RgFI`). Managed via:

```bash
# The service runs start.sh, which:
# 1. Sources /root/.zo_secrets
# 2. Activates /opt/hermes-agent/venv
# 3. Installs fastapi/uvicorn if missing
# 4. Runs server.py
```

Logs: `/dev/shm/zo-hermes.log` (stdout), `/dev/shm/zo-hermes_err.log` (stderr).

## Files

- `server.py` — The FastAPI application (all endpoints, SSE streaming, thinking dedup)
- `start.sh` — Service entrypoint (venv activation, dependency check, secrets loading)

## Key design decisions

1. **Localhost only** — No auth needed. Only zo-discord and local services access it.
2. **Threadpool execution** — `AIAgent.run_conversation()` is synchronous, so it runs in `loop.run_in_executor()` while the async event generator yields SSE events from queues.
3. **Zo-compatible SSE** — Event format matches Zo's native `/zo/ask` streaming exactly, so zo-discord handles both backends with the same parsing code.
4. **Don't modify upstream Hermes** — `/opt/hermes-agent/` is read-only. All customization happens in `server.py` (callbacks, dedup, event shaping).
