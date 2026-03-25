# zo-hermes

`zo-hermes` is a localhost FastAPI bridge that lets Zo Computer integrations, such as [zo-discord](https://github.com/JackALaing/zo-discord) and [zo-dispatcher](https://github.com/JackALaing/zo-dispatcher), talk to Hermes Agent through a Zo-shaped `/ask` API.

## Scope

`zo-hermes` is not a native Zo feature and does not make Zo's native channels or agents work with Hermes Agent. It exists for explicit callers that want to keep their own UX and routing behavior while using Hermes Agent instead of Zo’s native agent.

## Comparisons

### Zo API vs Hermes Agent

Zo API only:
- Zo’s channels (SMS, email, Telegram) – use `zo-discord` instead, or Hermes’ native gateways
- Zo’s personas – use Hermes personalities instead
- Zo’s rules – use AGENTS.md instead
- Zo’s agents – use `zo-dispatcher` instead

Hermes Agent only:
- [plugins and hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#available-hooks)
- [memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory), [automatic skill creation](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills#agent-managed-skills-skill_manage-tool), and [Honcho integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/honcho)
- [code execution (programmatic tool calling)](https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution)
- clarify callbacks
- session control: interrupt, cancel, undo, retry, compression threshold, manual compression
- agent config: `reasoning_effort`, `max_iterations`, `enabled_toolsets`, `disabled_toolsets`, `skip_memory`, and `skip_context`

### Hermes Agent’s Discord gateway vs zo-discord

Hermes Agent (Discord gateway) only:
- DMs
- non-threaded channels
- mention-gated server channels
- per-user session isolation inside the same shared channel
- multi-user Honcho integration
- voice messages and voice channels
- background sessions
- invoke any installed skill with slash commands
- set Hermes personality
- gateway-level prompt caching for Anthropic-style models
- gateway event hooks

zo-discord only:
- multi-backend, with per-channel routing between Zo and Hermes
- per-channel config: model, reasoning, tools, max-iterations, skip-memory, skip-context
- model aliases to override per-channel model
- `zo-dispatcher` agents can use zo-discord as a notification channel
- thread management features like auto-archive override and archive-by-reaction
- quiet vs streaming toggle
- queue vs interrupt toggle
- message buffering and batching

### Hermes Agent’s crons vs zo-dispatcher

Hermes Agent only:
- explicitly attach skills to cron jobs
- explicitly deliver cron outputs to session of origin
- /cron slash command to manage crons

zo-dispatcher only:
- multi-backend, with per-agent routing between Zo and Hermes
- markdown files as agent definitions
- webhooks: triggers, transforms, dedupe, queuing, and batching
- use zo-discord as a notification channel
- business-hours queueing and notification levels

## zo-hermes API

Base URL: `http://127.0.0.1:8788`

### Endpoints

| Method | Path                     | Purpose                                           |
| ------ | ------------------------ | ------------------------------------------------- |
| `POST` | `/ask`                   | Main Zo-compatible request endpoint               |
| `POST` | `/cancel`                | Cancel an active session                          |
| `POST` | `/undo`                  | Remove the last user turn and everything after it |
| `GET`  | `/status?session_id=...` | Running or idle state with live metadata          |
| `GET`  | `/usage?session_id=...`  | Live token usage or estimate-only fallback        |
| `POST` | `/compress`              | Force Hermes context compression                  |
| `POST` | `/clarify-response`      | Resume a pending clarify question                 |
| `GET`  | `/health`                | Service health check                              |

### `/ask` request body

```json
{
  "input": "user message",
  "stream": true,
  "conversation_id": "optional existing session id",
  "model_name": "optional model override",
  "persona_id": "accepted for compatibility, ignored",
  "ephemeral_system_prompt": "optional request-time overlay",
  "max_iterations": 90,
  "reasoning_effort": "medium",
  "skip_memory": false,
  "skip_context": false,
  "enabled_toolsets": ["web", "file"],
  "disabled_toolsets": ["zo"]
}
```

`session_id` is also accepted directly as an alias for `conversation_id`. Do not send both in the same request.

### Request behavior

| Request field / behavior                | `zo-hermes` behavior                                                                                       |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `conversation_id`                       | Alias for `session_id`                                                                                     |
| `model_name`                            | Per-request model override                                                                                 |
| `max_iterations`                        | Per-request max iterations override                                                                        |
| `reasoning_effort`                      | Per-request reasoning override; `none` disables reasoning                                                  |
| `skip_memory`, `skip_context`           | Skip memory or context files                                                                               |
| `enabled_toolsets`, `disabled_toolsets` | Passed through as Hermes-specific tool controls; invalid or empty resolved toolsets return `400`           |
| bare `mcp` in `enabled_toolsets`        | Expands to configured MCP server aliases; if none are configured, it resolves to nothing and fails         |
| `byok:...` model IDs                    | If Zo-style BYOKs are passed (incompatible with Hermes), falls back to the configured Hermes default model |
| `persona_id`                            | Accepted for compatibility, ignored semantically, logs a warning, returns `X-Persona-Ignored: true`        |
| `ephemeral_system_prompt`               | Supported request-time overlay path, used to pass zo-discord instructions                                  |

### Response and stream behavior

| Response / stream behavior | `zo-hermes` behavior                                                                                                                                      |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Returned `conversation_id` | The effective Hermes session ID is returned to callers and may change after compression (Hermes rotates session IDs during compression)                   |
| Response headers           | `X-Conversation-Id` is always returned; `X-Model-Fallback` and `X-Persona-Ignored` are added when relevant                                                |
| Streaming event types      | Emits `PartStartEvent`, `PartDeltaEvent`, `PartEndEvent`, `End`, `SSEErrorEvent`, and `ClarifyEvent`                                                      |
| Tool-progress noise        | The bridge intentionally does not emit `ProgressEvent`; tool-call progress is filtered down to the commentary-style stream so behavior stays closer to Zo |

### Current gaps

- no request-time mapping from Zo `persona_id` values to Hermes personalities configured via `SOUL.md`, `agent.system_prompt`, or `~/.hermes/config.yaml`
- the bridge currently instantiates Hermes with `platform="discord"` for all callers, including `zo-dispatcher` and custom integrations

## Setup

For installation, Hermes model/provider auth, Zo environment setup, Zo MCP configuration, service registration, direct usage, verification, logs, and operational checks, read [`skill/SKILL.md`](./skill/SKILL.md).

## Project Structure

```text
zo-hermes/
├── server.py                 # FastAPI bridge and Zo-compatibility layer
├── start.sh                  # Service entrypoint
├── tests/
│   └── test_server.py        # Bridge behavior tests
├── skill/
│   ├── SKILL.md              # Setup and operations guide
│   └── assets/
│       └── hermes-auto-update-agent-template.md
├── LICENSE
└── README.md
```