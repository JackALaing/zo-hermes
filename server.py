"""
zo-hermes — Thin FastAPI bridge wrapping Hermes AIAgent.

Exposes POST /ask (streaming SSE + non-streaming JSON) and POST /cancel.
Emits Zo-compatible SSE events so zo-discord parsing stays unchanged.
Localhost only — no auth needed.
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

DEFAULT_ZO_MCP_INCLUDE_TOOLS = [
    "change_hardware",
    "list_user_services",
    "register_user_service",
    "update_user_service",
    "delete_user_service",
    "service_doctor",
    "proxy_local_service",
    "create_website",
    "list_space_routes",
    "get_space_route",
    "update_space_route",
    "delete_space_route",
    "list_space_assets",
    "update_space_asset",
    "delete_space_asset",
    "get_space_errors",
    "update_user_settings",
]


def _expand_config_env(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_config_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_config_env(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# Hermes imports — add repo to path
# ---------------------------------------------------------------------------
HERMES_ROOT = Path("/opt/hermes-agent")
sys.path.insert(0, str(HERMES_ROOT))

from hermes_cli import config as hermes_config  # noqa: E402

_original_load_config = hermes_config.load_config


def _load_config_with_expanded_env():
    cfg = _expand_config_env(_original_load_config())
    return _apply_default_zo_mcp_policy(cfg)


def _apply_default_zo_mcp_policy(cfg):
    if not isinstance(cfg, dict):
        return cfg

    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        return cfg

    zo_cfg = servers.get("zo")
    if not isinstance(zo_cfg, dict):
        return cfg

    tools_cfg = zo_cfg.get("tools")
    if tools_cfg is not None:
        return cfg

    zo_cfg["tools"] = {
        "include": list(DEFAULT_ZO_MCP_INCLUDE_TOOLS),
        "resources": False,
        "prompts": False,
    }
    return cfg


hermes_config.load_config = _load_config_with_expanded_env

from model_tools import get_tool_definitions as _get_tool_definitions  # noqa: E402
import run_agent as _run_agent_module  # noqa: E402
from run_agent import AIAgent  # noqa: E402
from hermes_state import SessionDB  # noqa: E402
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402
from agent import prompt_builder as _prompt_builder  # noqa: E402
from agent.context_compressor import LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("zo-hermes")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.getenv("HERMES_API_PORT", "8788"))
DEFAULT_MODEL = os.getenv("HERMES_DEFAULT_MODEL", "gpt-5.4")
DEFAULT_MAX_ITERATIONS = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
HERMES_CWD = os.getenv("HERMES_CWD", "/home/workspace")
SESSION_FILES_DIR = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "sessions"


# ---------------------------------------------------------------------------
# Session tracking — active sessions that can be cancelled
# ---------------------------------------------------------------------------
@dataclass
class ActiveSession:
    cancel_event: threading.Event
    root_session_id: str
    current_session_id: str
    aliases: set[str] = field(default_factory=set)


_session_tracking_lock = threading.Lock()
_active_sessions: dict[str, ActiveSession] = {}  # any known session_id -> active session
_session_agents: dict[str, "AIAgent"] = {}  # session_id -> last AIAgent instance
_session_db: Optional[SessionDB] = None

# Pending clarify requests: session_id -> {event, response, question, choices}
_pending_clarify: dict[str, dict] = {}


def _get_session_db() -> SessionDB:
    global _session_db
    if _session_db is None:
        _session_db = SessionDB()
    return _session_db


def _generate_session_id() -> str:
    """Generate a Hermes-style session ID: YYYYMMDD_HHMMSS_hex8."""
    now = time.strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"{now}_{suffix}"


def _register_active_session(session_id: str, cancel_event: threading.Event) -> ActiveSession:
    active = ActiveSession(
        cancel_event=cancel_event,
        root_session_id=session_id,
        current_session_id=session_id,
        aliases={session_id},
    )
    with _session_tracking_lock:
        _active_sessions[session_id] = active
    return active


def _register_session_alias(active: ActiveSession, session_id: str) -> None:
    if not session_id:
        return
    with _session_tracking_lock:
        active.current_session_id = session_id
        active.aliases.add(session_id)
        for alias in active.aliases:
            _active_sessions[alias] = active


def _resolve_active_session(session_id: str) -> Optional[ActiveSession]:
    with _session_tracking_lock:
        active = _active_sessions.get(session_id)
    if active and not isinstance(active, ActiveSession):
        wrapped = ActiveSession(
            cancel_event=active,
            root_session_id=session_id,
            current_session_id=session_id,
            aliases={session_id},
        )
        with _session_tracking_lock:
            _active_sessions[session_id] = wrapped
        return wrapped
    return active


def _resolve_session_id(session_id: str) -> str:
    active = _resolve_active_session(session_id)
    return active.current_session_id if active else session_id


def _unregister_active_session(active: ActiveSession) -> None:
    with _session_tracking_lock:
        for alias in list(active.aliases):
            existing = _active_sessions.get(alias)
            if existing is active:
                _active_sessions.pop(alias, None)


def _session_file_path(session_id: str) -> Path:
    return SESSION_FILES_DIR / f"session_{session_id}.json"


def _load_session_file_messages(session_id: str) -> List[dict]:
    path = _session_file_path(session_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read session file for %s: %s", session_id, e)
        return []
    messages = data.get("messages")
    return list(messages) if isinstance(messages, list) else []


def _write_session_file_messages(session_id: str, messages: List[dict]) -> None:
    path = _session_file_path(session_id)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load session file for rewrite %s: %s", session_id, e)
        return
    data["messages"] = messages
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_db_messages(session_id: str) -> List[dict]:
    try:
        return _get_session_db().get_messages_as_conversation(session_id)
    except Exception:
        return []


def _normalize_message(msg: dict) -> dict:
    normalized = {"role": msg.get("role", "unknown"), "content": msg.get("content")}
    for key in ("tool_name", "tool_calls", "tool_call_id"):
        if msg.get(key) is not None:
            normalized[key] = msg.get(key)
    return normalized


def _candidate_history_sort_key(source: str, messages: List[dict]) -> tuple[int, int]:
    source_priority = {"agent": 3, "db": 2, "file": 1}.get(source, 0)
    return (len(messages), source_priority)


def _load_best_history(session_id: str) -> tuple[str, List[dict]]:
    canonical_id = _resolve_session_id(session_id)
    candidate_ids = list(dict.fromkeys([canonical_id, session_id]))
    candidates: List[tuple[str, List[dict]]] = []

    agent = _session_agents.get(canonical_id) or _session_agents.get(session_id)
    history = getattr(agent, "conversation_history", None) if agent else None
    if history:
        candidates.append(("agent", [_normalize_message(msg) for msg in history]))

    for candidate_id in candidate_ids:
        db_messages = [_normalize_message(msg) for msg in _load_db_messages(candidate_id)]
        if db_messages:
            candidates.append(("db", db_messages))
        file_messages = [_normalize_message(msg) for msg in _load_session_file_messages(candidate_id)]
        if file_messages:
            candidates.append(("file", file_messages))

    if not candidates:
        return canonical_id, []

    source, messages = max(candidates, key=lambda item: _candidate_history_sort_key(item[0], item[1]))
    logger.info("Resolved history for %s via %s (%d messages)", canonical_id, source, len(messages))
    return canonical_id, messages


def _rewrite_session_history(session_id: str, messages: List[dict]) -> None:
    session_db = _get_session_db()
    session_db.clear_messages(session_id)
    for msg in messages:
        session_db.append_message(
            session_id=session_id,
            role=msg.get("role", "unknown"),
            content=msg.get("content"),
            tool_name=msg.get("tool_name"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
        )
    _write_session_file_messages(session_id, messages)


def _has_compaction_summary(messages: List[dict]) -> bool:
    for msg in messages:
        content = (msg.get("content") or "").strip()
        if content.startswith(SUMMARY_PREFIX) or content.startswith(LEGACY_SUMMARY_PREFIX):
            return True
    return False


def _truncate_summary_text(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _tool_call_name(tool_call: dict) -> Optional[str]:
    if not isinstance(tool_call, dict):
        return None
    return (tool_call.get("function") or {}).get("name")


def _build_summary_bullet(
    current_user: Optional[str],
    current_tool_names: List[str],
    current_assistant: Optional[str],
) -> Optional[str]:
    if current_user is None and current_assistant is None:
        return None

    parts = []
    if current_user:
        parts.append(f"User asked: {_truncate_summary_text(current_user)}")
    if current_tool_names:
        parts.append(f"Tools used: {', '.join(dict.fromkeys(current_tool_names))}")
    if current_assistant:
        parts.append(f"Outcome: {_truncate_summary_text(current_assistant)}")
    if not parts:
        return None
    return "- " + " | ".join(parts)


def _build_fallback_compaction_summary(messages: List[dict]) -> Optional[str]:
    if not messages:
        return None

    bullets = []
    current_user = None
    current_tool_names: List[str] = []
    current_assistant = None

    def flush_current() -> None:
        bullet = _build_summary_bullet(current_user, current_tool_names, current_assistant)
        if bullet:
            bullets.append(bullet)

    for msg in messages:
        role = msg.get("role")
        if role == "user":
            flush_current()
            current_user = msg.get("content") or ""
            current_tool_names = []
            current_assistant = None
        elif role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            for tool_call in tool_calls:
                name = _tool_call_name(tool_call)
                if name:
                    current_tool_names.append(name)
            content = (msg.get("content") or "").strip()
            if content:
                current_assistant = content
        elif role == "tool":
            tool_name = msg.get("tool_name")
            if tool_name:
                current_tool_names.append(tool_name)

    flush_current()
    if not bullets:
        return None
    return f"{SUMMARY_PREFIX}\n" + "\n".join(bullets[:8])


def _ensure_compaction_summary(session_id: str, source_messages: List[dict]) -> bool:
    if not source_messages:
        return False

    current_messages = _load_db_messages(session_id)
    if not current_messages:
        current_messages = _load_session_file_messages(session_id)
    if not current_messages or _has_compaction_summary(current_messages):
        return False

    summary = _build_fallback_compaction_summary(source_messages)
    if not summary:
        return False

    summary_role = "assistant"
    first_role = current_messages[0].get("role")
    if first_role == "assistant":
        summary_role = "user"

    rewritten = [{"role": summary_role, "content": summary}, *current_messages]
    _rewrite_session_history(session_id, rewritten)
    agent = _session_agents.get(session_id)
    if agent and hasattr(agent, "conversation_history"):
        agent.conversation_history = rewritten
    logger.info("Injected fallback compaction summary into session %s", session_id)
    return True


def _is_enabled_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _configured_mcp_toolsets() -> List[str]:
    cfg = hermes_config.load_config() or {}
    servers = cfg.get("mcp_servers") or {}
    toolsets: List[str] = []
    for name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue
        if _is_enabled_flag(server_cfg.get("enabled", True)):
            toolsets.append(f"mcp-{name}")
    return toolsets


def _normalize_enabled_toolsets(enabled_toolsets: Optional[List[str]]) -> Optional[List[str]]:
    if enabled_toolsets is None:
        return None

    normalized: List[str] = []
    mcp_toolsets: Optional[List[str]] = None

    for name in enabled_toolsets:
        if name == "mcp":
            if mcp_toolsets is None:
                mcp_toolsets = _configured_mcp_toolsets()
            normalized.extend(mcp_toolsets)
            continue
        if name:
            normalized.append(name)

    return list(dict.fromkeys(normalized))


def _validate_enabled_toolsets(enabled_toolsets: Optional[List[str]]) -> Optional[List[str]]:
    normalized = _normalize_enabled_toolsets(enabled_toolsets)
    if normalized is None:
        return None
    if not normalized:
        raise ValueError(
            "enabled_toolsets resolved to no configured MCP servers. "
            "Use a concrete Hermes toolset such as 'web' or 'file', or a configured MCP server alias like 'zo' or 'mcp-zo'."
        )

    tool_defs = _get_tool_definitions(enabled_toolsets=normalized, quiet_mode=True)
    if not tool_defs:
        raise ValueError(
            f"enabled_toolsets={normalized} resolved to zero available tools. "
            "For Zo MCP, use a configured server alias like 'zo' or 'mcp-zo'. Bare 'mcp' is only an alias for configured MCP servers."
        )
    return normalized


def _resolve_model(requested_model: Optional[str]) -> tuple[str, Optional[str]]:
    if requested_model and requested_model.startswith("byok:"):
        return (
            DEFAULT_MODEL,
            f"Hermes cannot use requested model {requested_model}; falling back to {DEFAULT_MODEL}.",
        )
    return requested_model or DEFAULT_MODEL, None


def _resolve_reasoning_config(requested_effort: Optional[str]) -> dict:
    if requested_effort is not None:
        effort = str(requested_effort).strip().lower()
        if effort == "none":
            return {"enabled": False}
        return {"effort": effort}

    effort = ""
    try:
        cfg = hermes_config.load_config() or {}
        effort = str(cfg.get("agent", {}).get("reasoning_effort", "") or "").strip().lower()
    except Exception:
        effort = ""

    if effort == "none":
        return {"enabled": False}

    valid = {"xhigh", "high", "medium", "low", "minimal"}
    if effort in valid:
        return {"effort": effort}

    if effort:
        logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
    return {"effort": "medium"}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    input: str
    stream: bool = False
    session_id: Optional[str] = Field(None, alias="conversation_id")
    model_name: Optional[str] = None
    persona_id: Optional[str] = None
    ephemeral_system_prompt: Optional[str] = None
    max_iterations: Optional[int] = None
    reasoning_effort: Optional[str] = None  # off/low/medium/high
    skip_memory: Optional[bool] = False
    skip_context: Optional[bool] = False
    enabled_toolsets: Optional[List[str]] = None
    disabled_toolsets: Optional[List[str]] = None

    model_config = {"populate_by_name": True}  # accept both session_id and conversation_id


class CancelRequest(BaseModel):
    session_id: str


class SessionRequest(BaseModel):
    session_id: str


class ClarifyResponse(BaseModel):
    session_id: str
    response: str


# ---------------------------------------------------------------------------
# Agent runner (sync, runs in threadpool)
# ---------------------------------------------------------------------------
def _run_agent_sync(
    user_message: str,
    session_id: str,
    model: str,
    max_iterations: int,
    cancel_event: threading.Event,
    active_session: Optional[ActiveSession] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    thinking_queue: Optional[asyncio.Queue] = None,
    message_queue: Optional[asyncio.Queue] = None,
    clarify_queue: Optional[asyncio.Queue] = None,
    ephemeral_system_prompt: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    skip_memory: bool = False,
    skip_context: bool = False,
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
) -> dict:
    """
    Instantiate AIAgent and run a conversation turn. Called from threadpool.
    Returns the result dict from run_conversation(), plus the agent's
    potentially-updated session_id (changes after context compression).
    """
    if loop is None:
        raise ValueError("loop is required")

    session_db = _get_session_db()

    # Load conversation history if continuing a session
    conversation_history = None
    _, history = _load_best_history(session_id)
    if history:
        conversation_history = history

    # reasoning_callback: Hermes calls this from TWO code paths per block:
    #   1. _fire_reasoning_delta — per-token streaming deltas (small fragments)
    #   2. _build_assistant_message — full block text after API call completes
    # We only want (2). Accumulate deltas silently to detect the full-text
    # delivery (its prefix matches accumulated deltas), then forward it.
    reasoning_cb = None
    if thinking_queue is not None:
        _block_acc = [""]

        def reasoning_cb(reasoning_text: str):
            try:
                block = _block_acc[0]
                is_full = (len(block) >= 30
                           and len(reasoning_text) >= 30
                           and reasoning_text[:30] == block[:30])
                if is_full:
                    _block_acc[0] = ""
                    loop.call_soon_threadsafe(thinking_queue.put_nowait, ("thinking", reasoning_text))
                    return
                _block_acc[0] += reasoning_text
            except Exception:
                pass

    # stream_callback goes on run_conversation (called with text deltas)
    # Also accumulate streamed text as fallback when final_response is empty
    streamed_text_parts: list[str] = []
    stream_cb = None
    if message_queue is not None:

        def stream_cb(text: str):
            streamed_text_parts.append(text)
            try:
                loop.call_soon_threadsafe(message_queue.put_nowait, ("message", text))
            except Exception:
                pass

    # Clarify callback: blocks agent thread, signals SSE to emit ClarifyEvent,
    # waits for response from POST /clarify-response
    clarify_cb = None
    if clarify_queue is not None:
        def clarify_cb(question: str, choices):
            response_event = threading.Event()
            clarify_state = {
                "event": response_event,
                "response": None,
                "question": question,
                "choices": choices,
            }
            _pending_clarify[session_id] = clarify_state
            try:
                loop.call_soon_threadsafe(
                    clarify_queue.put_nowait,
                    ("clarify", {"question": question, "choices": choices}),
                )
                # Block until response arrives or 120s timeout
                if response_event.wait(timeout=120):
                    return clarify_state["response"] or ""
                return (
                    "The user did not provide a response within the time limit. "
                    "Use your best judgement to make the choice and proceed."
                )
            finally:
                _pending_clarify.pop(session_id, None)

    # Resolve provider (gets Claude Code OAuth token, base URL, etc.)
    provider_info = resolve_runtime_provider()

    # Instantiate agent
    agent_kwargs = dict(
        session_id=session_id,
        session_db=session_db,
        base_url=provider_info.get("base_url"),
        api_key=provider_info.get("api_key"),
        provider=provider_info.get("provider"),
        api_mode=provider_info.get("api_mode"),
        model=model,
        quiet_mode=True,
        platform="discord",
        max_iterations=max_iterations,
        save_trajectories=True,
        reasoning_callback=reasoning_cb,
        clarify_callback=clarify_cb,
        reasoning_config=_resolve_reasoning_config(reasoning_effort),
        pass_session_id=True,
        ephemeral_system_prompt=ephemeral_system_prompt,
        skip_memory=skip_memory,
        skip_context_files=skip_context,
    )
    if enabled_toolsets is not None:
        agent_kwargs["enabled_toolsets"] = enabled_toolsets
    if disabled_toolsets is not None:
        agent_kwargs["disabled_toolsets"] = disabled_toolsets

    agent = AIAgent(**agent_kwargs)
    _session_agents[session_id] = agent

    # Translate API-level cancel requests into Hermes' native interrupt path.
    def _watch_for_cancel() -> None:
        cancel_event.wait()
        try:
            agent.interrupt("Interrupted by a newer Discord message.")
        except Exception:
            logger.exception("Failed to interrupt agent for session %s", session_id)

    cancel_watcher = threading.Thread(target=_watch_for_cancel, daemon=True)
    cancel_watcher.start()

    # Change CWD for file access parity with Zo.
    original_cwd = os.getcwd()
    try:
        os.chdir(HERMES_CWD)
        result = agent.run_conversation(
            user_message=user_message,
            conversation_history=conversation_history,
            stream_callback=stream_cb,
        )
    finally:
        os.chdir(original_cwd)

    # If final_response is empty but we streamed text, use the streamed text.
    # This happens when the agent does tool work and the response is in the
    # stream but not captured as final_response by run_conversation().
    if not result.get("final_response") and streamed_text_parts:
        result["final_response"] = "".join(streamed_text_parts)
        logger.info("Using streamed text as final_response (was empty)")

    # After compression, agent.session_id may have changed. Propagate it.
    effective_id = agent.session_id
    if effective_id != session_id:
        _session_agents[effective_id] = agent
        _session_agents.pop(session_id, None)
        if active_session is not None:
            _register_session_alias(active_session, effective_id)
        _ensure_compaction_summary(effective_id, conversation_history or [])
    result["_session_id"] = effective_id
    return result


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
def _sse_event(event_type: str, data: dict) -> str:
    """Format an SSE event matching Zo's format."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("zo-hermes starting on port %d, CWD=%s", PORT, HERMES_CWD)
    yield
    logger.info("zo-hermes shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="zo-hermes", lifespan=lifespan)


@app.post("/ask")
async def ask(req: AskRequest):
    """
    Main endpoint — compatible with Zo's /zo/ask contract.
    Supports both streaming (SSE) and non-streaming (JSON) modes.
    """
    session_id = req.session_id or _generate_session_id()
    model, model_fallback = _resolve_model(req.model_name)
    max_iterations = req.max_iterations or DEFAULT_MAX_ITERATIONS
    reasoning_effort = req.reasoning_effort
    skip_memory = req.skip_memory or False
    skip_context = req.skip_context or False
    try:
        enabled_toolsets = _validate_enabled_toolsets(req.enabled_toolsets)
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e), "conversation_id": session_id},
            headers={"X-Conversation-Id": session_id},
        )
    disabled_toolsets = req.disabled_toolsets

    # Register cancel event
    cancel_event = threading.Event()
    active_session = _register_active_session(session_id, cancel_event)

    if req.stream:
        try:
            return await _handle_streaming(
                req.input, session_id, model, max_iterations, cancel_event,
                active_session,
                ephemeral_system_prompt=req.ephemeral_system_prompt,
                reasoning_effort=reasoning_effort, skip_memory=skip_memory,
                skip_context=skip_context, enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets, model_fallback=model_fallback,
            )
        except Exception:
            _unregister_active_session(active_session)
            raise

    return await _handle_non_streaming(
        req.input, session_id, model, max_iterations, cancel_event,
        active_session,
        ephemeral_system_prompt=req.ephemeral_system_prompt,
        reasoning_effort=reasoning_effort, skip_memory=skip_memory,
        skip_context=skip_context, enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets, model_fallback=model_fallback,
    )


async def _handle_non_streaming(
    user_message: str,
    session_id: str,
    model: str,
    max_iterations: int,
    cancel_event: threading.Event,
    active_session: Optional[ActiveSession] = None,
    ephemeral_system_prompt: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    skip_memory: bool = False,
    skip_context: bool = False,
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
    model_fallback: Optional[str] = None,
) -> JSONResponse:
    """Non-streaming mode for zo-dispatcher: returns JSON with output + conversation_id."""
    loop = asyncio.get_event_loop()

    try:
        result = await loop.run_in_executor(
            None,
            lambda: _run_agent_sync(
                user_message,
                session_id,
                model,
                max_iterations,
                cancel_event,
                active_session=active_session,
                loop=loop,
                thinking_queue=None,
                message_queue=None,
                ephemeral_system_prompt=ephemeral_system_prompt,
                reasoning_effort=reasoning_effort, skip_memory=skip_memory,
                skip_context=skip_context, enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
            ),
        )

        output = result.get("final_response", "")
        effective_session_id = result.get("_session_id", session_id)
        headers = {"X-Conversation-Id": effective_session_id}
        if model_fallback:
            headers["X-Model-Fallback"] = model_fallback
        return JSONResponse(
            content={
                "output": output,
                "conversation_id": effective_session_id,
                "model_fallback": model_fallback,
            },
            headers=headers,
        )

    except Exception as e:
        logger.exception("Error in non-streaming ask for session %s", session_id)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "conversation_id": session_id},
        )
    finally:
        if active_session is not None:
            _unregister_active_session(active_session)


async def _handle_streaming(
    user_message: str,
    session_id: str,
    model: str,
    max_iterations: int,
    cancel_event: threading.Event,
    active_session: Optional[ActiveSession] = None,
    ephemeral_system_prompt: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    skip_memory: bool = False,
    skip_context: bool = False,
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
    model_fallback: Optional[str] = None,
) -> StreamingResponse:
    """Streaming mode for zo-discord: returns SSE with Zo-compatible events."""
    loop = asyncio.get_event_loop()
    message_queue: asyncio.Queue = asyncio.Queue()
    thinking_queue: asyncio.Queue = asyncio.Queue()
    clarify_queue: asyncio.Queue = asyncio.Queue()

    # Run agent in background thread
    agent_task = loop.run_in_executor(
        None,
        lambda: _run_agent_sync(
            user_message,
            session_id,
            model,
            max_iterations,
            cancel_event,
            active_session=active_session,
            loop=loop,
            thinking_queue=thinking_queue,
            message_queue=message_queue,
            clarify_queue=clarify_queue,
            ephemeral_system_prompt=ephemeral_system_prompt,
            reasoning_effort=reasoning_effort, skip_memory=skip_memory,
            skip_context=skip_context, enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
        ),
    )

    async def event_generator():
        """Yield SSE events as the agent produces output.

        Thinking arrives as complete blocks from reasoning_cb (which filters
        out per-token deltas and only forwards the full-text delivery).
        Each block is emitted as a single PartStart(thinking) + PartEnd pair.
        """
        text_buffer = ""
        in_text_part = False
        agent_done = False

        try:
            while not agent_done:
                try:
                    # Drain thinking queue — each item is a complete block
                    try:
                        while True:
                            event_type, content = thinking_queue.get_nowait()
                            if content and content.strip():
                                yield _sse_event("PartStartEvent", {
                                    "part": {"part_kind": "thinking", "content": content}
                                })
                                yield _sse_event("PartEndEvent", {})
                    except asyncio.QueueEmpty:
                        pass

                    # Drain message queue (final response text)
                    try:
                        while True:
                            event_type, content = message_queue.get_nowait()
                            if content:
                                if not in_text_part:
                                    yield _sse_event("PartStartEvent", {
                                        "part": {"part_kind": "text", "content": ""}
                                    })
                                    in_text_part = True

                                yield _sse_event("PartDeltaEvent", {
                                    "delta": {
                                        "part_delta_kind": "text",
                                        "content_delta": content,
                                    }
                                })
                                text_buffer += content
                    except asyncio.QueueEmpty:
                        pass

                    # Drain clarify queue
                    try:
                        while True:
                            event_type, clarify_data = clarify_queue.get_nowait()
                            if event_type == "clarify":
                                # Close any open text part before clarify
                                if in_text_part:
                                    yield _sse_event("PartEndEvent", {})
                                    in_text_part = False
                                yield _sse_event("ClarifyEvent", {
                                    "question": clarify_data["question"],
                                    "choices": clarify_data.get("choices"),
                                    "session_id": session_id,
                                })
                    except asyncio.QueueEmpty:
                        pass

                    # Check if agent is done
                    if agent_task.done():
                        agent_done = True
                    else:
                        await asyncio.sleep(0.05)

                except asyncio.CancelledError:
                    cancel_event.set()
                    raise

            # Agent finished — get result
            result = agent_task.result()
            final_response = result.get("final_response", "")

            # Close any open text part
            if in_text_part:
                # If we haven't streamed the full response yet, send remainder
                if final_response and len(text_buffer) < len(final_response):
                    remainder = final_response[len(text_buffer):]
                    yield _sse_event("PartDeltaEvent", {
                        "delta": {
                            "part_delta_kind": "text",
                            "content_delta": remainder,
                        }
                    })
                yield _sse_event("PartEndEvent", {})
            elif final_response:
                # Never started streaming — emit the whole response
                yield _sse_event("PartStartEvent", {
                    "part": {"part_kind": "text", "content": final_response}
                })
                yield _sse_event("PartEndEvent", {})

            # End event with complete output and potentially-updated session_id
            effective_session_id = result.get("_session_id", session_id)
            yield _sse_event("End", {
                "data": {"output": final_response, "conversation_id": effective_session_id}
            })

        except Exception as e:
            logger.exception("Error in SSE stream for session %s", session_id)
            yield _sse_event("SSEErrorEvent", {"message": str(e)})
        finally:
            if active_session is not None:
                _unregister_active_session(active_session)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Conversation-Id": session_id,
            **({"X-Model-Fallback": model_fallback} if model_fallback else {}),
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/cancel")
async def cancel(req: CancelRequest):
    """Cancel an in-flight session."""
    active = _resolve_active_session(req.session_id)
    if active:
        active.cancel_event.set()
        return JSONResponse(content={"status": "cancelled", "session_id": active.current_session_id})
    return JSONResponse(
        status_code=404,
        content={"error": "Session not found or already completed", "session_id": req.session_id},
    )


@app.post("/clarify-response")
async def clarify_response(req: ClarifyResponse):
    """Provide the user's response to a pending clarify question."""
    state = _pending_clarify.get(req.session_id)
    if not state:
        return JSONResponse(
            status_code=404,
            content={"error": "No pending clarify request for this session"},
        )
    state["response"] = req.response
    state["event"].set()
    return JSONResponse(content={"status": "ok", "session_id": req.session_id})


@app.get("/health")
async def health():
    """Health check for Zo service monitoring."""
    return {"status": "ok", "service": "zo-hermes", "version": "0.1.0"}


# ---------------------------------------------------------------------------
# Session management endpoints
# ---------------------------------------------------------------------------
@app.post("/undo")
async def undo(req: SessionRequest):
    """Remove the last user+assistant exchange from a session's transcript."""
    resolved_session_id, messages = _load_best_history(req.session_id)
    if not messages:
        return JSONResponse(status_code=400, content={"error": "No messages in session"})

    # Find the last user message index
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        return JSONResponse(status_code=400, content={"error": "No user message found to undo"})

    removed = messages[last_user_idx:]
    remaining = messages[:last_user_idx]

    _rewrite_session_history(resolved_session_id, remaining)

    # Also update in-memory agent history if we have one
    agent = _session_agents.get(resolved_session_id)
    if agent and hasattr(agent, "conversation_history"):
        agent.conversation_history = remaining

    return JSONResponse(content={
        "status": "undone",
        "session_id": resolved_session_id,
        "requested_session_id": req.session_id,
        "removed_count": len(removed),
        "remaining_count": len(remaining),
        "removed_messages": [
            {"role": m.get("role"), "content": (m.get("content") or "")[:500]}
            for m in removed
        ],
    })


@app.get("/status")
async def status(session_id: str):
    """Return running/idle state and agent info for a session."""
    resolved_session_id = _resolve_session_id(session_id)
    is_running = _resolve_active_session(session_id) is not None
    agent = _session_agents.get(resolved_session_id) or _session_agents.get(session_id)

    result = {
        "session_id": resolved_session_id,
        "requested_session_id": session_id,
        "state": "running" if is_running else "idle",
    }

    if agent:
        budget = getattr(agent, "iteration_budget", None)
        result["iterations_used"] = budget._used if budget else 0
        result["iterations_max"] = budget.max_total if budget else 0
        result["model"] = getattr(agent, "model", None)
        result["input_tokens"] = getattr(agent, "session_input_tokens", 0) or 0
        result["output_tokens"] = getattr(agent, "session_output_tokens", 0) or 0
        result["api_calls"] = getattr(agent, "session_api_calls", 0) or 0
    else:
        # No agent instance — check if session exists in DB at all
        messages = _load_db_messages(resolved_session_id) or _load_session_file_messages(resolved_session_id)
        if not messages:
            return JSONResponse(status_code=404, content={"error": "Session not found"})
        result["message_count"] = len(messages)

    return JSONResponse(content=result)


@app.get("/usage")
async def usage(session_id: str):
    """Return token usage details for a session."""
    resolved_session_id = _resolve_session_id(session_id)
    agent = _session_agents.get(resolved_session_id) or _session_agents.get(session_id)

    if not agent:
        # No agent — return minimal info from DB
        messages = _load_db_messages(resolved_session_id) or _load_session_file_messages(resolved_session_id)
        if not messages:
            return JSONResponse(status_code=404, content={"error": "Session not found"})
        try:
            from agent.model_metadata import estimate_messages_tokens_rough
            approx_tokens = estimate_messages_tokens_rough(messages)
        except Exception:
            approx_tokens = None
        return JSONResponse(content={
            "session_id": resolved_session_id,
            "requested_session_id": session_id,
            "message_count": len(messages),
            "estimated_context_tokens": approx_tokens,
            "note": "No active agent — showing estimates only",
        })

    input_tokens = getattr(agent, "session_input_tokens", 0) or 0
    output_tokens = getattr(agent, "session_output_tokens", 0) or 0
    cache_read_tokens = getattr(agent, "session_cache_read_tokens", 0) or 0
    cache_write_tokens = getattr(agent, "session_cache_write_tokens", 0) or 0
    prompt_tokens = agent.session_prompt_tokens
    completion_tokens = agent.session_completion_tokens
    total_tokens = agent.session_total_tokens
    api_calls = agent.session_api_calls

    compressor = agent.context_compressor
    last_prompt = compressor.last_prompt_tokens
    ctx_len = compressor.context_length
    pct = (last_prompt / ctx_len * 100) if ctx_len else 0

    # Cost estimation
    cost_usd = None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        cost_result = estimate_usage_cost(
            agent.model,
            CanonicalUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            provider=getattr(agent, "provider", None),
            base_url=getattr(agent, "base_url", None),
        )
        if cost_result.amount_usd is not None:
            cost_usd = float(cost_result.amount_usd)
    except Exception:
        pass

    return JSONResponse(content={
        "session_id": resolved_session_id,
        "requested_session_id": session_id,
        "model": agent.model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "api_calls": api_calls,
        "context_window_tokens": ctx_len,
        "context_used_tokens": last_prompt,
        "context_used_pct": round(pct, 1),
        "compression_count": compressor.compression_count,
        "cost_usd": cost_usd,
    })


@app.post("/compress")
async def compress(req: SessionRequest):
    """Compress context for a session — flushes memories and summarizes middle turns."""
    resolved_session_id = _resolve_session_id(req.session_id)
    agent = _session_agents.get(resolved_session_id) or _session_agents.get(req.session_id)
    if not agent:
        return JSONResponse(status_code=404, content={
            "error": "No active agent for this session. Send a message first via /ask.",
        })

    if not agent.compression_enabled:
        return JSONResponse(status_code=400, content={"error": "Compression is disabled in config"})

    session_db = _get_session_db()
    try:
        messages = session_db.get_messages_as_conversation(resolved_session_id)
    except Exception:
        messages = getattr(agent, "conversation_history", None) or []

    if not messages or len(messages) < 4:
        return JSONResponse(status_code=400, content={
            "error": f"Not enough messages to compress (have {len(messages)}, need at least 4)",
        })

    try:
        from agent.model_metadata import estimate_messages_tokens_rough
        before_tokens = estimate_messages_tokens_rough(messages)
        before_count = len(messages)

        compressed, new_system = agent._compress_context(
            messages,
            agent._cached_system_prompt or "",
            approx_tokens=before_tokens,
        )

        after_tokens = estimate_messages_tokens_rough(compressed)
        after_count = len(compressed)

        # The agent's session_id may have changed after compression
        new_session_id = agent.session_id
        if new_session_id != resolved_session_id:
            _session_agents[new_session_id] = agent
            _session_agents.pop(resolved_session_id, None)
            active = _resolve_active_session(req.session_id)
            if active:
                _register_session_alias(active, new_session_id)
            _ensure_compaction_summary(new_session_id, messages)

        return JSONResponse(content={
            "status": "compressed",
            "session_id": new_session_id,
            "requested_session_id": req.session_id,
            "previous_session_id": resolved_session_id if new_session_id != resolved_session_id else None,
            "before": {"messages": before_count, "tokens": before_tokens},
            "after": {"messages": after_count, "tokens": after_tokens},
        })

    except Exception as e:
        logger.exception("Compression failed for session %s", resolved_session_id)
        return JSONResponse(status_code=500, content={"error": f"Compression failed: {e}"})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
