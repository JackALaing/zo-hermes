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

# ---------------------------------------------------------------------------
# Hermes imports — add repo to path
# ---------------------------------------------------------------------------
HERMES_ROOT = Path("/opt/hermes-agent")
sys.path.insert(0, str(HERMES_ROOT))

from run_agent import AIAgent  # noqa: E402
from hermes_state import SessionDB  # noqa: E402
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402

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

# ---------------------------------------------------------------------------
# Session tracking — active sessions that can be cancelled
# ---------------------------------------------------------------------------
_active_sessions: dict[str, asyncio.Event] = {}  # session_id -> cancel_event
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


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    input: str
    stream: bool = False
    session_id: Optional[str] = Field(None, alias="conversation_id")
    model_name: Optional[str] = None
    persona_id: Optional[str] = None
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
    cancel_event: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
    thinking_queue: Optional[asyncio.Queue] = None,
    message_queue: Optional[asyncio.Queue] = None,
    clarify_queue: Optional[asyncio.Queue] = None,
    progress_queue: Optional[asyncio.Queue] = None,
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
    session_db = _get_session_db()

    # Load conversation history if continuing a session
    conversation_history = None
    try:
        history = session_db.get_messages_as_conversation(session_id)
        if history:
            conversation_history = history
    except Exception:
        pass  # New session, no history

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

    # Hermes session IDs are safe to pass directly to tools like zo-discord.
    # Inject the current session ID into the prompt so agents can target their
    # own thread explicitly instead of relying on process-wide env vars.
    session_hint = (
        "## Hermes Session\n"
        f"Your current Hermes session ID is `{session_id}`.\n"
        "If a tool needs a conversation or session identifier, pass this exact "
        "value explicitly instead of relying on auto-detection.\n"
        f'For Discord thread actions, use `zo-discord --conv-id {session_id} ...`.\n\n'
    )
    effective_user_message = session_hint + user_message

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
        reasoning_config={"effort": reasoning_effort or "medium"},
        pass_session_id=True,
        skip_memory=skip_memory,
        skip_context_files=skip_context,
    )
    if enabled_toolsets is not None:
        agent_kwargs["enabled_toolsets"] = enabled_toolsets
    if disabled_toolsets is not None:
        agent_kwargs["disabled_toolsets"] = disabled_toolsets

    agent = AIAgent(**agent_kwargs)
    _session_agents[session_id] = agent

    # Change CWD for file access parity with Zo.
    original_cwd = os.getcwd()
    try:
        os.chdir(HERMES_CWD)
        result = agent.run_conversation(
            user_message=effective_user_message,
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
    # Ignore Zo BYOK model IDs (e.g. "byok:xxx") — use default Hermes model instead
    model = req.model_name if req.model_name and not req.model_name.startswith("byok:") else DEFAULT_MODEL
    max_iterations = req.max_iterations or DEFAULT_MAX_ITERATIONS
    reasoning_effort = req.reasoning_effort
    skip_memory = req.skip_memory or False
    skip_context = req.skip_context or False
    enabled_toolsets = req.enabled_toolsets
    disabled_toolsets = req.disabled_toolsets

    # Register cancel event
    cancel_event = asyncio.Event()
    _active_sessions[session_id] = cancel_event

    try:
        if req.stream:
            return await _handle_streaming(
                req.input, session_id, model, max_iterations, cancel_event,
                reasoning_effort=reasoning_effort, skip_memory=skip_memory,
                skip_context=skip_context, enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
            )
        else:
            return await _handle_non_streaming(
                req.input, session_id, model, max_iterations, cancel_event,
                reasoning_effort=reasoning_effort, skip_memory=skip_memory,
                skip_context=skip_context, enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
            )
    finally:
        _active_sessions.pop(session_id, None)


async def _handle_non_streaming(
    user_message: str,
    session_id: str,
    model: str,
    max_iterations: int,
    cancel_event: asyncio.Event,
    reasoning_effort: Optional[str] = None,
    skip_memory: bool = False,
    skip_context: bool = False,
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
) -> JSONResponse:
    """Non-streaming mode for zo-dispatcher: returns JSON with output + conversation_id."""
    loop = asyncio.get_event_loop()

    try:
        result = await loop.run_in_executor(
            None,
            lambda: _run_agent_sync(
                user_message, session_id, model, max_iterations,
                cancel_event, loop, None, None,
                reasoning_effort=reasoning_effort, skip_memory=skip_memory,
                skip_context=skip_context, enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
            ),
        )

        output = result.get("final_response", "")
        effective_session_id = result.get("_session_id", session_id)
        return JSONResponse(
            content={"output": output, "conversation_id": effective_session_id},
            headers={"X-Conversation-Id": effective_session_id},
        )

    except Exception as e:
        logger.exception("Error in non-streaming ask for session %s", session_id)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "conversation_id": session_id},
        )


async def _handle_streaming(
    user_message: str,
    session_id: str,
    model: str,
    max_iterations: int,
    cancel_event: asyncio.Event,
    reasoning_effort: Optional[str] = None,
    skip_memory: bool = False,
    skip_context: bool = False,
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
) -> StreamingResponse:
    """Streaming mode for zo-discord: returns SSE with Zo-compatible events."""
    loop = asyncio.get_event_loop()
    message_queue: asyncio.Queue = asyncio.Queue()
    thinking_queue: asyncio.Queue = asyncio.Queue()
    clarify_queue: asyncio.Queue = asyncio.Queue()
    progress_queue: asyncio.Queue = asyncio.Queue()

    # Run agent in background thread
    agent_task = loop.run_in_executor(
        None,
        lambda: _run_agent_sync(
            user_message, session_id, model, max_iterations,
            cancel_event, loop, thinking_queue, message_queue,
            clarify_queue=clarify_queue,
            progress_queue=progress_queue,
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

                    # Drain progress queue (tool progress / subagent updates)
                    try:
                        while True:
                            event_type, progress_msg = progress_queue.get_nowait()
                            if event_type == "progress" and progress_msg:
                                yield _sse_event("ProgressEvent", {
                                    "message": progress_msg,
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Conversation-Id": session_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/cancel")
async def cancel(req: CancelRequest):
    """Cancel an in-flight session."""
    cancel_event = _active_sessions.get(req.session_id)
    if cancel_event:
        cancel_event.set()
        return JSONResponse(content={"status": "cancelled", "session_id": req.session_id})
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
    session_db = _get_session_db()
    try:
        messages = session_db.get_messages_as_conversation(req.session_id)
    except Exception as e:
        return JSONResponse(status_code=404, content={"error": f"Session not found: {e}"})

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

    # Rewrite transcript: clear and re-insert remaining messages
    session_db.clear_messages(req.session_id)
    for msg in remaining:
        session_db.append_message(
            session_id=req.session_id,
            role=msg.get("role", "unknown"),
            content=msg.get("content"),
            tool_name=msg.get("tool_name"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
        )

    # Also update in-memory agent history if we have one
    agent = _session_agents.get(req.session_id)
    if agent and hasattr(agent, "conversation_history"):
        agent.conversation_history = remaining

    return JSONResponse(content={
        "status": "undone",
        "session_id": req.session_id,
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
    is_running = session_id in _active_sessions
    agent = _session_agents.get(session_id)

    result = {
        "session_id": session_id,
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
        session_db = _get_session_db()
        try:
            messages = session_db.get_messages_as_conversation(session_id)
            if not messages:
                return JSONResponse(status_code=404, content={"error": "Session not found"})
            result["message_count"] = len(messages)
        except Exception:
            return JSONResponse(status_code=404, content={"error": "Session not found"})

    return JSONResponse(content=result)


@app.get("/usage")
async def usage(session_id: str):
    """Return token usage details for a session."""
    agent = _session_agents.get(session_id)

    if not agent:
        # No agent — return minimal info from DB
        session_db = _get_session_db()
        try:
            messages = session_db.get_messages_as_conversation(session_id)
            if not messages:
                return JSONResponse(status_code=404, content={"error": "Session not found"})
            # Estimate tokens from messages
            try:
                from agent.model_metadata import estimate_messages_tokens_rough
                approx_tokens = estimate_messages_tokens_rough(messages)
            except Exception:
                approx_tokens = None
            return JSONResponse(content={
                "session_id": session_id,
                "message_count": len(messages),
                "estimated_context_tokens": approx_tokens,
                "note": "No active agent — showing estimates only",
            })
        except Exception:
            return JSONResponse(status_code=404, content={"error": "Session not found"})

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
        "session_id": session_id,
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
    agent = _session_agents.get(req.session_id)
    if not agent:
        return JSONResponse(status_code=404, content={
            "error": "No active agent for this session. Send a message first via /ask.",
        })

    if not agent.compression_enabled:
        return JSONResponse(status_code=400, content={"error": "Compression is disabled in config"})

    session_db = _get_session_db()
    try:
        messages = session_db.get_messages_as_conversation(req.session_id)
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
        if new_session_id != req.session_id:
            _session_agents[new_session_id] = agent
            _session_agents.pop(req.session_id, None)

        return JSONResponse(content={
            "status": "compressed",
            "session_id": new_session_id,
            "previous_session_id": req.session_id if new_session_id != req.session_id else None,
            "before": {"messages": before_count, "tokens": before_tokens},
            "after": {"messages": after_count, "tokens": after_tokens},
        })

    except Exception as e:
        logger.exception("Compression failed for session %s", req.session_id)
        return JSONResponse(status_code=500, content={"error": f"Compression failed: {e}"})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
