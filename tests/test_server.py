"""Tests for zo-hermes request parsing and session endpoints."""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def run(coro):
    return asyncio.run(coro)


class FakeSessionDB:
    def __init__(self):
        self.messages = {}
        self.cleared = []
        self.appended = []

    def get_messages_as_conversation(self, session_id):
        return list(self.messages.get(session_id, []))

    def clear_messages(self, session_id):
        self.cleared.append(session_id)
        self.messages[session_id] = []

    def append_message(self, **kwargs):
        self.appended.append(kwargs)
        self.messages.setdefault(kwargs["session_id"], []).append(
            {
                "role": kwargs["role"],
                "content": kwargs["content"],
                "tool_name": kwargs.get("tool_name"),
                "tool_calls": kwargs.get("tool_calls"),
                "tool_call_id": kwargs.get("tool_call_id"),
            }
        )


def load_server_module():
    service_dir = Path("/home/workspace/Services/zo-hermes")
    module_name = "zo_hermes_server_test"

    fake_run_agent = types.ModuleType("run_agent")

    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.session_id = kwargs["session_id"]

        def run_conversation(self, **kwargs):
            return {"final_response": "ok"}

    fake_run_agent.AIAgent = FakeAIAgent

    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = FakeSessionDB

    fake_runtime_parent = types.ModuleType("hermes_cli")
    fake_runtime = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime.resolve_runtime_provider = lambda: {
        "base_url": "http://localhost",
        "api_key": "test-key",
        "provider": "test-provider",
        "api_mode": "responses",
    }

    fake_fastapi = types.ModuleType("fastapi")
    fake_fastapi_responses = types.ModuleType("fastapi.responses")

    class FakeFastAPI:
        def __init__(self, *args, **kwargs):
            self.routes = []

        def post(self, _path):
            def decorator(func):
                return func
            return decorator

        def get(self, _path):
            def decorator(func):
                return func
            return decorator

    class FakeJSONResponse:
        def __init__(self, content=None, status_code=200, headers=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers or {}
            self.body = json.dumps(content).encode("utf-8")

    class FakeStreamingResponse:
        def __init__(self, content=None, media_type=None, headers=None):
            self.content = content
            self.media_type = media_type
            self.headers = headers or {}

    fake_fastapi.FastAPI = FakeFastAPI
    fake_fastapi.Request = object
    fake_fastapi_responses.JSONResponse = FakeJSONResponse
    fake_fastapi_responses.StreamingResponse = FakeStreamingResponse

    fake_pydantic = types.ModuleType("pydantic")

    class FakeFieldInfo:
        def __init__(self, default=None, alias=None):
            self.default = default
            self.alias = alias

    def FakeField(default=None, alias=None):
        return FakeFieldInfo(default=default, alias=alias)

    class FakeBaseModel:
        model_config = {}

        def __init__(self, **kwargs):
            annotations = getattr(self.__class__, "__annotations__", {})
            for name in annotations:
                default = getattr(self.__class__, name, None)
                if isinstance(default, FakeFieldInfo):
                    alias = default.alias
                    if name in kwargs:
                        value = kwargs[name]
                    elif alias and alias in kwargs:
                        value = kwargs[alias]
                    else:
                        value = default.default
                else:
                    value = kwargs.get(name, default)
                setattr(self, name, value)

        @classmethod
        def model_validate(cls, data):
            return cls(**data)

    fake_pydantic.BaseModel = FakeBaseModel
    fake_pydantic.Field = FakeField

    sys.modules["run_agent"] = fake_run_agent
    sys.modules["hermes_state"] = fake_state
    sys.modules["hermes_cli"] = fake_runtime_parent
    sys.modules["hermes_cli.runtime_provider"] = fake_runtime
    sys.modules["fastapi"] = fake_fastapi
    sys.modules["fastapi.responses"] = fake_fastapi_responses
    sys.modules["pydantic"] = fake_pydantic

    spec = importlib.util.spec_from_file_location(module_name, service_dir / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._session_db = FakeSessionDB()
    module._active_sessions.clear()
    module._session_agents.clear()
    module._pending_clarify.clear()
    return module


class TestAskRequest:
    def test_accepts_conversation_id_alias(self):
        module = load_server_module()
        req = module.AskRequest.model_validate({"input": "hello", "conversation_id": "conv-1"})
        assert req.session_id == "conv-1"

    def test_accepts_session_id_directly(self):
        module = load_server_module()
        req = module.AskRequest.model_validate({"input": "hello", "session_id": "sess-1"})
        assert req.session_id == "sess-1"


class TestAskEndpoint:
    def test_non_streaming_ask_threads_all_new_params(self):
        module = load_server_module()
        captured = {}

        async def fake_non_streaming(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"ok": True}

        module._handle_non_streaming = fake_non_streaming
        req = module.AskRequest.model_validate(
            {
                "input": "hello",
                "stream": False,
                "conversation_id": "conv-1",
                "reasoning_effort": "high",
                "skip_memory": True,
                "skip_context": True,
                "enabled_toolsets": ["web"],
                "disabled_toolsets": ["rl"],
                "max_iterations": 5,
            }
        )

        run(module.ask(req))

        assert captured["args"][1] == "conv-1"
        assert captured["kwargs"]["reasoning_effort"] == "high"
        assert captured["kwargs"]["skip_memory"] is True
        assert captured["kwargs"]["skip_context"] is True
        assert captured["kwargs"]["enabled_toolsets"] == ["web"]
        assert captured["kwargs"]["disabled_toolsets"] == ["rl"]

    def test_streaming_ask_threads_all_new_params(self):
        module = load_server_module()
        captured = {}

        async def fake_streaming(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"streaming": True}

        module._handle_streaming = fake_streaming
        req = module.AskRequest.model_validate(
            {
                "input": "hello",
                "stream": True,
                "session_id": "sess-1",
                "reasoning_effort": "low",
                "skip_memory": True,
                "skip_context": True,
                "enabled_toolsets": ["file"],
                "disabled_toolsets": ["browser"],
            }
        )

        run(module.ask(req))

        assert captured["args"][1] == "sess-1"
        assert captured["kwargs"]["reasoning_effort"] == "low"
        assert captured["kwargs"]["enabled_toolsets"] == ["file"]
        assert captured["kwargs"]["disabled_toolsets"] == ["browser"]


class TestSessionEndpoints:
    def test_clarify_response_handles_missing_and_success(self):
        module = load_server_module()
        missing = run(module.clarify_response(module.ClarifyResponse(session_id="sess-1", response="A")))
        assert missing.status_code == 404

        event = SimpleNamespace(set=lambda: setattr(event, "was_set", True))
        event.was_set = False
        module._pending_clarify["sess-1"] = {"event": event, "response": None}
        present = run(module.clarify_response(module.ClarifyResponse(session_id="sess-1", response="B")))
        assert present.status_code == 200
        assert module._pending_clarify["sess-1"]["response"] == "B"
        assert event.was_set is True

    def test_undo_rewrites_transcript_and_updates_agent_history(self):
        module = load_server_module()
        db = module._session_db
        db.messages["sess-1"] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "redo"},
            {"role": "assistant", "content": "later"},
        ]
        agent = SimpleNamespace(conversation_history=list(db.messages["sess-1"]))
        module._session_agents["sess-1"] = agent

        response = run(module.undo(module.SessionRequest(session_id="sess-1")))
        body = json.loads(response.body)

        assert body["removed_count"] == 2
        assert db.cleared == ["sess-1"]
        assert len(db.appended) == 2
        assert agent.conversation_history == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_status_returns_live_agent_details(self):
        module = load_server_module()
        module._active_sessions["sess-1"] = asyncio.Event()
        module._session_agents["sess-1"] = SimpleNamespace(
            iteration_budget=SimpleNamespace(_used=2, max_total=7),
            model="gpt-5.4",
            session_input_tokens=100,
            session_output_tokens=50,
            session_api_calls=3,
        )

        response = run(module.status("sess-1"))
        body = json.loads(response.body)

        assert body["state"] == "running"
        assert body["iterations_used"] == 2
        assert body["iterations_max"] == 7
        assert body["model"] == "gpt-5.4"

    def test_status_falls_back_to_transcript_count(self):
        module = load_server_module()
        module._session_db.messages["sess-1"] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

        response = run(module.status("sess-1"))
        body = json.loads(response.body)

        assert body["state"] == "idle"
        assert body["message_count"] == 2

    def test_usage_estimate_only_mode_after_restart(self):
        module = load_server_module()
        module._session_db.messages["sess-1"] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

        fake_model_metadata = types.ModuleType("agent.model_metadata")
        fake_model_metadata.estimate_messages_tokens_rough = lambda messages: 42
        sys.modules["agent.model_metadata"] = fake_model_metadata

        response = run(module.usage("sess-1"))
        body = json.loads(response.body)

        assert body["message_count"] == 2
        assert body["estimated_context_tokens"] == 42
        assert "estimates only" in body["note"]

    def test_usage_live_agent_mode(self):
        module = load_server_module()
        module._session_agents["sess-1"] = SimpleNamespace(
            model="gpt-5.4",
            session_input_tokens=100,
            session_output_tokens=50,
            session_cache_read_tokens=10,
            session_cache_write_tokens=5,
            session_prompt_tokens=120,
            session_completion_tokens=55,
            session_total_tokens=175,
            session_api_calls=4,
            context_compressor=SimpleNamespace(
                last_prompt_tokens=120,
                context_length=400,
                compression_count=2,
            ),
            provider="test-provider",
            base_url="http://localhost",
        )

        fake_usage_pricing = types.ModuleType("agent.usage_pricing")
        fake_usage_pricing.CanonicalUsage = lambda **kwargs: kwargs
        fake_usage_pricing.estimate_usage_cost = lambda *args, **kwargs: SimpleNamespace(amount_usd=0.1234)
        sys.modules["agent.usage_pricing"] = fake_usage_pricing

        response = run(module.usage("sess-1"))
        body = json.loads(response.body)

        assert body["model"] == "gpt-5.4"
        assert body["total_tokens"] == 175
        assert body["context_used_pct"] == 30.0
        assert body["compression_count"] == 2
        assert body["cost_usd"] == 0.1234

    def test_compress_requires_live_agent(self):
        module = load_server_module()
        response = run(module.compress(module.SessionRequest(session_id="sess-1")))
        assert response.status_code == 404

    def test_compress_updates_session_id_after_compression(self):
        module = load_server_module()
        module._session_db.messages["sess-1"] = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]

        fake_model_metadata = types.ModuleType("agent.model_metadata")
        fake_model_metadata.estimate_messages_tokens_rough = lambda messages: len(messages) * 10
        sys.modules["agent.model_metadata"] = fake_model_metadata

        agent = SimpleNamespace(
            compression_enabled=True,
            _cached_system_prompt="system",
            session_id="sess-2",
            _compress_context=lambda messages, system_prompt, approx_tokens=None: (
                messages[:2],
                system_prompt,
            ),
        )
        module._session_agents["sess-1"] = agent

        response = run(module.compress(module.SessionRequest(session_id="sess-1")))
        body = json.loads(response.body)

        assert body["session_id"] == "sess-2"
        assert body["previous_session_id"] == "sess-1"
        assert body["before"]["messages"] == 4
        assert body["after"]["messages"] == 2
