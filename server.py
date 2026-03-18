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
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
DEFAULT_MODEL = os.getenv("HERMES_DEFAULT_MODEL", "anthropic/claude-opus-4.6")
DEFAULT_MAX_ITERATIONS = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
HERMES_CWD = os.getenv("HERMES_CWD", "/home/workspace")

# ---------------------------------------------------------------------------
# Session tracking — active sessions that can be cancelled
# ---------------------------------------------------------------------------
_active_sessions: dict[str, asyncio.Event] = {}  # session_id -> cancel_event
_session_db: Optional[SessionDB] = None


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

    model_config = {"populate_by_name": True}  # accept both session_id and conversation_id


class CancelRequest(BaseModel):
    session_id: str


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

    # Resolve provider (gets Claude Code OAuth token, base URL, etc.)
    provider_info = resolve_runtime_provider()

    # Instantiate agent
    agent = AIAgent(
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
        reasoning_config={"effort": "medium"},
    )

    # Change CWD for file access parity with Zo, and set CONVERSATION_ID
    # so CLI tools (zo-discord rename, etc.) can auto-detect the conversation.
    original_cwd = os.getcwd()
    original_conv_id = os.environ.get("CONVERSATION_ID")
    try:
        os.chdir(HERMES_CWD)
        os.environ["CONVERSATION_ID"] = session_id
        result = agent.run_conversation(
            user_message=user_message,
            conversation_history=conversation_history,
            stream_callback=stream_cb,
        )
    finally:
        os.chdir(original_cwd)
        if original_conv_id is not None:
            os.environ["CONVERSATION_ID"] = original_conv_id
        else:
            os.environ.pop("CONVERSATION_ID", None)

    # If final_response is empty but we streamed text, use the streamed text.
    # This happens when the agent does tool work and the response is in the
    # stream but not captured as final_response by run_conversation().
    if not result.get("final_response") and streamed_text_parts:
        result["final_response"] = "".join(streamed_text_parts)
        logger.info("Using streamed text as final_response (was empty)")

    # After compression, agent.session_id may have changed. Propagate it.
    result["_session_id"] = agent.session_id
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

    # Register cancel event
    cancel_event = asyncio.Event()
    _active_sessions[session_id] = cancel_event

    try:
        if req.stream:
            return await _handle_streaming(req.input, session_id, model, max_iterations, cancel_event)
        else:
            return await _handle_non_streaming(req.input, session_id, model, max_iterations, cancel_event)
    finally:
        _active_sessions.pop(session_id, None)


async def _handle_non_streaming(
    user_message: str,
    session_id: str,
    model: str,
    max_iterations: int,
    cancel_event: asyncio.Event,
) -> JSONResponse:
    """Non-streaming mode for zo-dispatcher: returns JSON with output + conversation_id."""
    loop = asyncio.get_event_loop()

    try:
        result = await loop.run_in_executor(
            None,
            _run_agent_sync,
            user_message,
            session_id,
            model,
            max_iterations,
            cancel_event,
            loop,
            None,  # no thinking_queue
            None,  # no message_queue
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
) -> StreamingResponse:
    """Streaming mode for zo-discord: returns SSE with Zo-compatible events."""
    loop = asyncio.get_event_loop()
    message_queue: asyncio.Queue = asyncio.Queue()
    thinking_queue: asyncio.Queue = asyncio.Queue()

    # Run agent in background thread
    agent_task = loop.run_in_executor(
        None,
        _run_agent_sync,
        user_message,
        session_id,
        model,
        max_iterations,
        cancel_event,
        loop,
        thinking_queue,
        message_queue,
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


@app.get("/health")
async def health():
    """Health check for Zo service monitoring."""
    return {"status": "ok", "service": "zo-hermes", "version": "0.1.0"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
