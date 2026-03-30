"""Tests for zo-hermes request parsing and session endpoints."""

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def run(coro):
    return asyncio.run(coro)


async def collect_stream(response):
    chunks = []
    async for chunk in response.content:
        chunks.append(chunk)
    return chunks


def parse_sse_events(stream):
    events = []
    for block in stream.strip().split("\n\n"):
        if not block.strip():
            continue
        event_type = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        events.append({"event": event_type, "data": data})
    return events


def get_sse_events(stream, event_type):
    return [event["data"] for event in parse_sse_events(stream) if event["event"] == event_type]


class FakeSessionDB:
    def __init__(self):
        self.messages = {}
        self.cleared = []
        self.appended = []

    def get_messages_as_conversation(self, session_id):
        conversation = []
        for message in self.messages.get(session_id, []):
            item = {
                "role": message["role"],
                "content": message.get("content"),
            }
            if message.get("tool_name") is not None:
                item["tool_name"] = message.get("tool_name")
            if message.get("tool_calls") is not None:
                item["tool_calls"] = message.get("tool_calls")
            if message.get("tool_call_id") is not None:
                item["tool_call_id"] = message.get("tool_call_id")
            conversation.append(item)
        return conversation

    def get_messages(self, session_id):
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
                "finish_reason": kwargs.get("finish_reason"),
                "reasoning": kwargs.get("reasoning"),
                "reasoning_details": kwargs.get("reasoning_details"),
                "codex_reasoning_items": kwargs.get("codex_reasoning_items"),
            }
        )


def load_server_module(config_data=None):
    service_dir = Path("/home/workspace/Services/zo-hermes")
    module_name = "zo_hermes_server_test"

    fake_run_agent = types.ModuleType("run_agent")

    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.session_id = kwargs["session_id"]
            self.interrupt_calls = []

        def run_conversation(self, **kwargs):
            return {"final_response": "ok"}

        def interrupt(self, message):
            self.interrupt_calls.append(message)

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
    fake_config = types.ModuleType("hermes_cli.config")
    merged_config = {
        "model": {"default": "gpt-5.4"},
        "agent": {"max_turns": 90},
        "mcp_servers": {
            "zo": {
                "headers": {
                    "Authorization": "Bearer ${HERMES_ZO_ACCESS_TOKEN}"
                }
            }
        }
    }
    if config_data:
        merged_config.update(config_data)
    fake_config.load_config = lambda: merged_config
    fake_model_tools = types.ModuleType("model_tools")
    fake_agent_parent = types.ModuleType("agent")
    fake_prompt_builder = types.ModuleType("agent.prompt_builder")
    fake_prompt_builder._scan_context_content = lambda content, _path: content
    fake_prompt_builder._truncate_content = lambda content, _label: content
    fake_prompt_builder._find_hermes_md = lambda _cwd: None
    fake_prompt_builder._strip_yaml_frontmatter = lambda content: content
    fake_prompt_builder.load_soul_md = lambda: ""
    fake_context_compressor = types.ModuleType("agent.context_compressor")
    fake_context_compressor.LEGACY_SUMMARY_PREFIX = "[legacy-summary]"
    fake_context_compressor.SUMMARY_PREFIX = "[summary]"

    def fake_get_tool_definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=False):
        if enabled_toolsets is None:
            return [{"function": {"name": "default_tool"}}]
        names = set(enabled_toolsets)
        valid = {
            "web",
            "file",
            "terminal",
            "mcp-zo",
            "zo",
        }
        if names & valid:
            return [{"function": {"name": "mock_tool"}}]
        return []

    fake_model_tools.get_tool_definitions = fake_get_tool_definitions

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
    sys.modules["hermes_cli.config"] = fake_config
    sys.modules["hermes_cli.runtime_provider"] = fake_runtime
    sys.modules["model_tools"] = fake_model_tools
    sys.modules["agent"] = fake_agent_parent
    sys.modules["agent.prompt_builder"] = fake_prompt_builder
    sys.modules["agent.context_compressor"] = fake_context_compressor
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

    def test_accepts_honcho_session_key(self):
        module = load_server_module()
        req = module.AskRequest.model_validate(
            {"input": "hello", "conversation_id": "conv-1", "honcho_session_key": "discord-thread-123"}
        )
        assert req.session_id == "conv-1"
        assert req.honcho_session_key == "discord-thread-123"


class TestZoMcpConfigExpansion:
    def test_expands_env_vars_in_loaded_config(self):
        original_token = os.environ.get("HERMES_ZO_ACCESS_TOKEN")
        try:
            os.environ["HERMES_ZO_ACCESS_TOKEN"] = "header.payload.signature"
            module = load_server_module()
            cfg = module.hermes_config.load_config()
            auth = cfg["mcp_servers"]["zo"]["headers"]["Authorization"]
            assert auth == "Bearer header.payload.signature"
            assert cfg["mcp_servers"]["zo"]["tools"]["include"] == module.DEFAULT_ZO_MCP_INCLUDE_TOOLS
            assert cfg["mcp_servers"]["zo"]["tools"]["resources"] is False
            assert cfg["mcp_servers"]["zo"]["tools"]["prompts"] is False
        finally:
            if original_token is None:
                os.environ.pop("HERMES_ZO_ACCESS_TOKEN", None)
            else:
                os.environ["HERMES_ZO_ACCESS_TOKEN"] = original_token

    def test_preserves_explicit_zo_tools_policy(self):
        module = load_server_module()
        custom_cfg = {
            "mcp_servers": {
                "zo": {
                    "tools": {
                        "include": ["custom_tool"],
                        "resources": True,
                        "prompts": True,
                    }
                }
            }
        }
        result = module._apply_default_zo_mcp_policy(custom_cfg)
        assert result["mcp_servers"]["zo"]["tools"]["include"] == ["custom_tool"]
        assert result["mcp_servers"]["zo"]["tools"]["resources"] is True
        assert result["mcp_servers"]["zo"]["tools"]["prompts"] is True

    def test_normalizes_mcp_alias_to_configured_server_toolset(self):
        module = load_server_module()
        assert module._normalize_enabled_toolsets(["mcp"]) == ["mcp-zo"]

    def test_rejects_empty_mcp_alias_when_no_servers_enabled(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"mcp_servers": {"zo": {"enabled": False}}}
        try:
            module._validate_enabled_toolsets(["mcp"])
            assert False, "expected ValueError"
        except ValueError as e:
            assert "no configured MCP servers" in str(e)


class TestAskEndpoint:
    def test_resolve_model_returns_fallback_message_for_byok(self):
        module = load_server_module()

        model, fallback = module._resolve_model("byok:test-model")

        assert model == module.DEFAULT_MODEL
        assert "falling back" in fallback

    def test_ask_ignores_byok_model_ids_and_uses_default_model(self):
        module = load_server_module()
        captured = {}

        async def fake_non_streaming(*args, **kwargs):
            captured["args"] = args
            return {"ok": True}

        module._handle_non_streaming = fake_non_streaming
        req = module.AskRequest.model_validate(
            {"input": "hello", "stream": False, "model_name": "byok:test-model"}
        )

        run(module.ask(req))

        assert captured["args"][2] == module.DEFAULT_MODEL

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
                "honcho_session_key": "discord-thread-123",
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
        assert captured["kwargs"]["honcho_session_key"] == "discord-thread-123"
        assert captured["kwargs"]["reasoning_effort"] == "high"
        assert captured["kwargs"]["skip_memory"] is True
        assert captured["kwargs"]["skip_context"] is True
        assert captured["kwargs"]["enabled_toolsets"] == ["web"]
        assert captured["kwargs"]["disabled_toolsets"] == ["rl"]

    def test_non_streaming_ask_normalizes_mcp_alias(self):
        module = load_server_module()
        captured = {}

        async def fake_non_streaming(*args, **kwargs):
            captured["kwargs"] = kwargs
            return {"ok": True}

        module._handle_non_streaming = fake_non_streaming
        req = module.AskRequest.model_validate(
            {
                "input": "hello",
                "stream": False,
                "enabled_toolsets": ["mcp"],
            }
        )

        run(module.ask(req))

        assert captured["kwargs"]["enabled_toolsets"] == ["mcp-zo"]

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

    def test_ask_returns_400_when_enabled_toolsets_resolve_to_zero_tools(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"mcp_servers": {"zo": {"enabled": False}}}
        req = module.AskRequest.model_validate(
            {
                "input": "hello",
                "enabled_toolsets": ["mcp"],
            }
        )

        response = run(module.ask(req))
        body = json.loads(response.body)

        assert response.status_code == 400
        assert "no configured MCP servers" in body["error"]


class TestReasoningConfigResolution:
    def test_explicit_request_none_disables_reasoning(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "high"}}

        assert module._resolve_reasoning_config("none") == {"enabled": False}

    def test_explicit_request_effort_wins_over_config_default(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "low"}}

        assert module._resolve_reasoning_config("high") == {"effort": "high"}

    def test_omitted_request_effort_uses_config_default(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "low"}}

        assert module._resolve_reasoning_config(None) == {"effort": "low"}

    def test_omitted_request_effort_uses_config_none_to_disable_reasoning(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "none"}}

        assert module._resolve_reasoning_config(None) == {"enabled": False}

    def test_missing_config_falls_back_to_medium(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {}

        assert module._resolve_reasoning_config(None) == {"effort": "medium"}


class TestSessionEndpoints:
    def test_load_best_history_preserves_replay_fields_from_db_file_and_agent(self, tmp_path):
        module = load_server_module()
        module.SESSION_FILES_DIR = tmp_path

        db_message = {
            "role": "assistant",
            "content": "db answer",
            "finish_reason": "stop",
            "reasoning": "db reasoning",
            "reasoning_details": [{"type": "summary", "text": "db detail"}],
            "codex_reasoning_items": [{"id": "db-r1", "encrypted_content": "enc-db"}],
            "tool_calls": [{"function": {"name": "web_search", "arguments": "{}"}}],
        }
        file_message = {
            "role": "assistant",
            "content": "file answer",
            "finish_reason": "length",
            "reasoning": "file reasoning",
            "reasoning_details": [{"type": "summary", "text": "file detail"}],
            "codex_reasoning_items": [{"id": "file-r1", "encrypted_content": "enc-file"}],
        }
        agent_message = {
            "role": "assistant",
            "content": "agent answer",
            "finish_reason": "tool_calls",
            "reasoning": "agent reasoning",
            "reasoning_details": [{"type": "summary", "text": "agent detail"}],
            "codex_reasoning_items": [{"id": "agent-r1", "encrypted_content": "enc-agent"}],
        }

        module._session_db.messages["db-sess"] = [db_message]
        file_path = tmp_path / "session_file-sess.json"
        file_path.write_text(json.dumps({"messages": [file_message]}), encoding="utf-8")
        module._session_agents["agent-sess"] = SimpleNamespace(conversation_history=[agent_message])

        _, db_history = module._load_best_history("db-sess")
        _, file_history = module._load_best_history("file-sess")
        _, agent_history = module._load_best_history("agent-sess")

        assert db_history == [db_message]
        assert file_history == [file_message]
        assert agent_history == [agent_message]

    def test_rewrite_session_history_preserves_reasoning_and_finish_metadata(self, tmp_path):
        module = load_server_module()
        module.SESSION_FILES_DIR = tmp_path
        file_path = tmp_path / "session_sess-1.json"
        file_path.write_text(json.dumps({"messages": []}), encoding="utf-8")

        messages = [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "answer",
                "finish_reason": "stop",
                "reasoning": "chain of thought",
                "reasoning_details": [{"type": "summary", "text": "detail"}],
                "codex_reasoning_items": [{"id": "r1", "encrypted_content": "enc-1"}],
                "tool_calls": [{"function": {"name": "web_search", "arguments": "{}"}}],
            },
        ]

        module._rewrite_session_history("sess-1", messages)

        assert module._session_db.appended[1]["finish_reason"] == "stop"
        assert module._session_db.appended[1]["reasoning"] == "chain of thought"
        assert module._session_db.appended[1]["reasoning_details"] == [{"type": "summary", "text": "detail"}]
        assert module._session_db.appended[1]["codex_reasoning_items"] == [{"id": "r1", "encrypted_content": "enc-1"}]

        rewritten_file = json.loads(file_path.read_text(encoding="utf-8"))
        assert rewritten_file["messages"] == messages

    def test_cancel_handles_success_and_missing_session(self):
        module = load_server_module()
        active = module.ActiveSession(
            cancel_event=__import__("threading").Event(),
            root_session_id="sess-1",
            current_session_id="sess-1",
            aliases={"sess-1"},
        )
        module._active_sessions["sess-1"] = active

        response = run(module.cancel(module.CancelRequest(session_id="sess-1")))
        body = json.loads(response.body)

        assert response.status_code == 200
        assert body["status"] == "cancelled"
        assert module._active_sessions["sess-1"].cancel_event.is_set() is True

        missing = run(module.cancel(module.CancelRequest(session_id="sess-2")))
        assert missing.status_code == 404

    def test_health_reports_service_metadata(self):
        module = load_server_module()

        body = run(module.health())

        assert body["status"] == "ok"
        assert body["service"] == "zo-hermes"
        assert body["version"] == "0.1.0"

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
            {
                "role": "assistant",
                "content": "hello",
                "finish_reason": "stop",
                "reasoning": "kept reasoning",
                "reasoning_details": [{"type": "summary", "text": "kept detail"}],
                "codex_reasoning_items": [{"id": "keep-1", "encrypted_content": "enc-keep"}],
            },
            {"role": "user", "content": "redo"},
            {
                "role": "assistant",
                "content": "later",
                "finish_reason": "stop",
                "reasoning": "removed reasoning",
            },
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
            {
                "role": "assistant",
                "content": "hello",
                "finish_reason": "stop",
                "reasoning": "kept reasoning",
                "reasoning_details": [{"type": "summary", "text": "kept detail"}],
                "codex_reasoning_items": [{"id": "keep-1", "encrypted_content": "enc-keep"}],
            },
        ]
        assert db.messages["sess-1"][1]["reasoning"] == "kept reasoning"
        assert db.messages["sess-1"][1]["finish_reason"] == "stop"
        assert db.messages["sess-1"][1]["codex_reasoning_items"] == [
            {"id": "keep-1", "encrypted_content": "enc-keep"}
        ]

    def test_compaction_summary_injection_preserves_later_assistant_fields(self):
        module = load_server_module()
        source_messages = [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "first answer",
                "reasoning": "source reasoning",
            },
        ]
        module._session_db.messages["sess-1"] = [
            {"role": "user", "content": "surviving user"},
            {
                "role": "assistant",
                "content": "surviving answer",
                "finish_reason": "stop",
                "reasoning": "surviving reasoning",
                "reasoning_details": [{"type": "summary", "text": "surviving detail"}],
                "codex_reasoning_items": [{"id": "survive-1", "encrypted_content": "enc-survive"}],
            },
        ]
        agent = SimpleNamespace(conversation_history=list(module._session_db.messages["sess-1"]))
        module._session_agents["sess-1"] = agent

        injected = module._ensure_compaction_summary("sess-1", source_messages)

        assert injected is True
        assert module._session_db.messages["sess-1"][0]["content"].startswith(module.SUMMARY_PREFIX)
        assert module._session_db.messages["sess-1"][2]["reasoning"] == "surviving reasoning"
        assert module._session_db.messages["sess-1"][2]["reasoning_details"] == [
            {"type": "summary", "text": "surviving detail"}
        ]
        assert module._session_db.messages["sess-1"][2]["codex_reasoning_items"] == [
            {"id": "survive-1", "encrypted_content": "enc-survive"}
        ]
        assert agent.conversation_history[2]["finish_reason"] == "stop"

    def test_load_best_history_preserves_codex_reasoning_only_assistant_messages(self):
        module = load_server_module()
        codex_message = {
            "role": "assistant",
            "content": "",
            "finish_reason": "incomplete",
            "codex_reasoning_items": [{"id": "codex-1", "encrypted_content": "enc-codex"}],
        }
        module._session_agents["sess-1"] = SimpleNamespace(conversation_history=[codex_message])

        _, history = module._load_best_history("sess-1")

        assert history == [codex_message]

    def test_status_returns_live_agent_details(self):
        module = load_server_module()
        module._active_sessions["sess-1"] = module.ActiveSession(
            cancel_event=__import__("threading").Event(),
            root_session_id="sess-1",
            current_session_id="sess-1",
            aliases={"sess-1"},
        )
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


class TestStreamingAndAgentBehavior:
    def test_module_uses_hermes_config_defaults_even_when_bridge_env_vars_are_set(self):
        old_model = os.environ.get("HERMES_DEFAULT_MODEL")
        old_max_iterations = os.environ.get("HERMES_MAX_ITERATIONS")
        try:
            os.environ["HERMES_DEFAULT_MODEL"] = "env-model"
            os.environ["HERMES_MAX_ITERATIONS"] = "123"
            module = load_server_module(
                config_data={
                    "model": {"default": "cfg-model"},
                    "agent": {"max_turns": 77},
                }
            )
            assert module.DEFAULT_MODEL == "cfg-model"
            assert module.DEFAULT_MAX_ITERATIONS == 77
        finally:
            if old_model is None:
                os.environ.pop("HERMES_DEFAULT_MODEL", None)
            else:
                os.environ["HERMES_DEFAULT_MODEL"] = old_model
            if old_max_iterations is None:
                os.environ.pop("HERMES_MAX_ITERATIONS", None)
            else:
                os.environ["HERMES_MAX_ITERATIONS"] = old_max_iterations

    def test_module_requires_model_and_max_turns_in_hermes_config(self):
        old_model = os.environ.pop("HERMES_DEFAULT_MODEL", None)
        old_max_iterations = os.environ.pop("HERMES_MAX_ITERATIONS", None)
        try:
            with pytest.raises(RuntimeError, match="model.default.*agent.max_turns"):
                load_server_module(config_data={"model": {}, "agent": {}})
        finally:
            if old_model is not None:
                os.environ["HERMES_DEFAULT_MODEL"] = old_model
            if old_max_iterations is not None:
                os.environ["HERMES_MAX_ITERATIONS"] = old_max_iterations

    def test_run_agent_sync_uses_hermes_cwd_for_context_discovery(self, tmp_path):
        old_hermes_cwd = os.environ.get("HERMES_CWD")
        old_terminal_cwd = os.environ.get("TERMINAL_CWD")
        loop = asyncio.new_event_loop()
        try:
            hermes_cwd = str(tmp_path / "zo-hermes-context")
            Path(hermes_cwd).mkdir(parents=True, exist_ok=True)
            os.environ["HERMES_CWD"] = hermes_cwd
            os.environ["TERMINAL_CWD"] = "/opt/hermes-agent"
            module = load_server_module()
            seen = {}

            class CapturingAgent:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                    self.session_id = kwargs["session_id"]

                def run_conversation(self, **kwargs):
                    seen["cwd"] = os.getcwd()
                    seen["terminal_cwd"] = os.environ.get("TERMINAL_CWD")
                    return {"final_response": "ok"}

                def interrupt(self, message):
                    return None

            module.AIAgent = CapturingAgent

            result = module._run_agent_sync(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                loop=loop,
            )

            assert result["final_response"] == "ok"
            assert seen["cwd"] == hermes_cwd
            assert seen["terminal_cwd"] == hermes_cwd
        finally:
            loop.close()
            if old_hermes_cwd is None:
                os.environ.pop("HERMES_CWD", None)
            else:
                os.environ["HERMES_CWD"] = old_hermes_cwd
            if old_terminal_cwd is None:
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = old_terminal_cwd

    def test_non_streaming_includes_model_fallback_header_and_body(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {"final_response": "done", "_session_id": "sess-1"}

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
                model_fallback="Hermes cannot use requested model byok:test; falling back to gpt-5.4.",
            )
        )

        body = json.loads(response.body)
        assert body["model_fallback"].startswith("Hermes cannot use requested model")
        assert response.headers["X-Model-Fallback"].startswith("Hermes cannot use requested model")

    def test_non_streaming_includes_persona_ignored_header(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {"final_response": "done", "_session_id": "sess-1"}

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
                persona_ignored=True,
            )
        )

        assert response.headers["X-Persona-Ignored"] == "true"

    def test_non_streaming_returns_updated_conversation_header(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {"final_response": "done", "_session_id": "sess-2"}

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )

        body = json.loads(response.body)
        assert body["conversation_id"] == "sess-2"
        assert response.headers["X-Conversation-Id"] == "sess-2"

    def test_streaming_emits_thinking_text_clarify_and_end_events(self):
        module = load_server_module()

        def fake_run_agent_sync(
            user_message,
            session_id,
            model,
            max_iterations,
            cancel_event,
            active_session,
            loop,
            thinking_queue=None,
            message_queue=None,
            clarify_queue=None,
            **kwargs,
        ):
            loop.call_soon_threadsafe(
                thinking_queue.put_nowait, ("thinking", "Plan the answer")
            )
            loop.call_soon_threadsafe(
                message_queue.put_nowait, ("message", "Hello ")
            )
            loop.call_soon_threadsafe(
                message_queue.put_nowait, ("message", "world")
            )
            loop.call_soon_threadsafe(
                clarify_queue.put_nowait,
                ("clarify", {"question": "Pick one", "choices": ["A", "B"]}),
            )
            return {
                "final_response": "Hello world",
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
                "_session_id": "sess-2",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        chunks = run(collect_stream(response))
        stream = "".join(chunks)
        end_payload = get_sse_events(stream, "End")[0]["data"]

        assert response.headers["X-Conversation-Id"] == "sess-1"
        assert 'event: PartStartEvent\ndata: {"part": {"part_kind": "thinking", "content": "Plan the answer"}}' in stream
        assert 'event: PartDeltaEvent\ndata: {"delta": {"part_delta_kind": "text", "content_delta": "Hello "}}' in stream
        assert 'event: ClarifyEvent\ndata: {"question": "Pick one", "choices": ["A", "B"], "session_id": "sess-1"}' in stream
        assert end_payload == {
            "output": "Hello world",
            "conversation_id": "sess-2",
            "result": {
                "turn_status": "completed",
                "output_source": "final_response",
                "output_present": True,
                "hermes_final_response_present": True,
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
            },
        }

    def test_streaming_emits_error_event_on_failure(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            raise RuntimeError("boom")

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        stream = "".join(run(collect_stream(response)))

        assert get_sse_events(stream, "SSEErrorEvent") == [
            {"message": "boom", "turn_status": "error"}
        ]

    def test_streaming_ask_keeps_session_active_until_stream_finishes(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "done",
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(module.ask(module.AskRequest(input="hello", stream=True, conversation_id="sess-1")))

        assert "sess-1" in module._active_sessions

        stream = "".join(run(collect_stream(response)))
        end_payload = get_sse_events(stream, "End")[0]["data"]

        assert end_payload["output"] == "done"
        assert end_payload["conversation_id"] == "sess-1"
        assert end_payload["result"]["turn_status"] == "completed"
        assert "sess-1" not in module._active_sessions

    def test_non_streaming_completed_turn_returns_result_envelope(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "done",
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )

        body = json.loads(response.body)
        assert body["output"] == "done"
        assert body["result"] == {
            "turn_status": "completed",
            "output_source": "final_response",
            "output_present": True,
            "hermes_final_response_present": True,
            "completed": True,
            "partial": False,
            "failed": False,
            "interrupted": False,
            "error": None,
        }

    def test_streaming_completed_turn_end_result_matches_non_streaming(self):
        module = load_server_module()
        hermes_result = {
            "final_response": "done",
            "completed": True,
            "partial": False,
            "failed": False,
            "interrupted": False,
            "error": None,
            "_session_id": "sess-1",
        }

        module._run_agent_sync = lambda *args, **kwargs: dict(hermes_result)
        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        non_stream_body = json.loads(response.body)

        module._run_agent_sync = lambda *args, **kwargs: dict(hermes_result)
        stream_response = run(
            module._handle_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        stream = "".join(run(collect_stream(stream_response)))
        end_payload = get_sse_events(stream, "End")[0]["data"]

        assert end_payload["output"] == "done"
        assert end_payload["result"] == non_stream_body["result"]

    def test_completed_streamed_only_turn_uses_streamed_text(self):
        module = load_server_module()

        def fake_run_agent_sync(
            user_message,
            session_id,
            model,
            max_iterations,
            cancel_event,
            active_session,
            loop,
            thinking_queue=None,
            message_queue=None,
            clarify_queue=None,
            **kwargs,
        ):
            loop.call_soon_threadsafe(message_queue.put_nowait, ("message", "Hello "))
            loop.call_soon_threadsafe(message_queue.put_nowait, ("message", "world"))
            return {
                "final_response": "",
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        stream = "".join(run(collect_stream(response)))
        end_payload = get_sse_events(stream, "End")[0]["data"]

        assert end_payload["output"] == "Hello world"
        assert end_payload["result"] == {
            "turn_status": "completed_streamed_only",
            "output_source": "streamed_text",
            "output_present": True,
            "hermes_final_response_present": False,
            "completed": True,
            "partial": False,
            "failed": False,
            "interrupted": False,
            "error": None,
        }

    def test_streaming_end_uses_authoritative_bridge_streamed_text(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "",
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
                "_session_id": "sess-1",
                "_bridge_streamed_text": "Hello world",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        stream = "".join(run(collect_stream(response)))
        end_payload = get_sse_events(stream, "End")[0]["data"]

        assert end_payload["output"] == "Hello world"
        assert end_payload["result"]["turn_status"] == "completed_streamed_only"

    def test_empty_success_turn_has_no_output(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "",
                "completed": True,
                "partial": False,
                "failed": False,
                "interrupted": False,
                "error": None,
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        body = json.loads(response.body)

        assert body["output"] == ""
        assert body["result"]["turn_status"] == "empty_success"
        assert body["result"]["output_source"] == "none"
        assert body["result"]["output_present"] is False

    def test_partial_turn_preserves_error_text(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "partial answer",
                "completed": False,
                "partial": True,
                "failed": False,
                "interrupted": False,
                "error": "tool timeout",
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        body = json.loads(response.body)

        assert body["output"] == "partial answer"
        assert body["result"]["turn_status"] == "partial"
        assert body["result"]["error"] == "tool timeout"

    def test_failed_turn_with_text_uses_final_response(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "failed answer",
                "completed": False,
                "partial": False,
                "failed": True,
                "interrupted": False,
                "error": "model failure",
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        body = json.loads(response.body)

        assert body["output"] == "failed answer"
        assert body["result"]["turn_status"] == "failed"
        assert body["result"]["output_source"] == "final_response"

    def test_failed_turn_without_text_uses_none_output_source(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "",
                "completed": False,
                "partial": False,
                "failed": True,
                "interrupted": False,
                "error": "model failure",
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        body = json.loads(response.body)

        assert body["output"] == ""
        assert body["result"]["turn_status"] == "failed"
        assert body["result"]["output_source"] == "none"

    def test_interrupted_turn_maps_to_partial_and_preserves_flag(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            return {
                "final_response": "partial answer",
                "completed": False,
                "partial": True,
                "failed": False,
                "interrupted": True,
                "error": None,
                "_session_id": "sess-1",
            }

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        body = json.loads(response.body)

        assert body["result"]["turn_status"] == "partial"
        assert body["result"]["interrupted"] is True

    def test_non_streaming_bridge_exception_returns_error_result(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            raise RuntimeError("boom")

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_non_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        body = json.loads(response.body)

        assert response.status_code == 500
        assert body["output"] == ""
        assert body["error"] == "boom"
        assert body["result"]["turn_status"] == "error"
        assert body["result"]["output_source"] == "none"

    def test_streaming_bridge_exception_emits_error_without_fake_end(self):
        module = load_server_module()

        def fake_run_agent_sync(*args, **kwargs):
            raise RuntimeError("boom")

        module._run_agent_sync = fake_run_agent_sync

        response = run(
            module._handle_streaming(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
            )
        )
        stream = "".join(run(collect_stream(response)))

        error_events = get_sse_events(stream, "SSEErrorEvent")
        end_events = get_sse_events(stream, "End")
        assert error_events == [{"message": "boom", "turn_status": "error"}]
        assert end_events == []

    def test_run_agent_sync_captures_streamed_text_without_message_queue(self):
        module = load_server_module()

        class StreamingOnlyAgent:
            def __init__(self, **kwargs):
                self.session_id = kwargs["session_id"]

            def run_conversation(self, **kwargs):
                stream_callback = kwargs["stream_callback"]
                stream_callback("Hello ")
                stream_callback("world")
                return {
                    "final_response": "",
                    "completed": True,
                    "partial": False,
                    "failed": False,
                    "interrupted": False,
                    "error": None,
                }

            def interrupt(self, message):
                pass

        module.AIAgent = StreamingOnlyAgent

        loop = asyncio.new_event_loop()
        try:
            result = module._run_agent_sync(
                "hello",
                "sess-1",
                "gpt-5.4",
                5,
                __import__("threading").Event(),
                None,
                loop,
                message_queue=None,
            )
        finally:
            loop.close()

        assert result["_bridge_streamed_text"] == "Hello world"

    def test_run_agent_sync_deduplicates_reasoning_and_passes_overlay_separately(self):
        module = load_server_module()
        captured = {}

        class RecordingAgent:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.session_id = kwargs["session_id"]

            def run_conversation(self, **kwargs):
                captured["user_message"] = kwargs["user_message"]
                reasoning_cb = captured["kwargs"]["reasoning_callback"]
                part_one = "Plan the answer carefully with detailed"
                part_two = " steps and reflect before replying."
                full_text = part_one + part_two
                reasoning_cb(part_one)
                reasoning_cb(part_two)
                reasoning_cb(full_text)
                return {"final_response": "ok"}

        module.AIAgent = RecordingAgent
        queued = []
        thinking_queue = SimpleNamespace(put_nowait=queued.append)

        result = module._run_agent_sync(
            "Original prompt",
            "sess-1",
            "gpt-5.4",
            5,
            __import__("threading").Event(),
            None,
            SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)),
            thinking_queue=thinking_queue,
            ephemeral_system_prompt="## Message Source\nDiscord context",
            honcho_session_key="discord-thread-123",
        )

        assert result["_session_id"] == "sess-1"
        assert captured["kwargs"]["pass_session_id"] is True
        assert captured["kwargs"]["honcho_session_key"] == "discord-thread-123"
        assert captured["user_message"] == "Original prompt"
        assert captured["kwargs"]["ephemeral_system_prompt"] == "## Message Source\nDiscord context"
        assert queued == [("thinking", "Plan the answer carefully with detailed steps and reflect before replying.")]

    def test_run_agent_sync_uses_config_reasoning_default_when_request_omitted(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "low"}}
        captured = {}

        class RecordingAgent:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.session_id = kwargs["session_id"]

            def run_conversation(self, **kwargs):
                return {"final_response": "ok"}

            def interrupt(self, message):
                pass

        module.AIAgent = RecordingAgent

        module._run_agent_sync(
            "hello",
            "sess-1",
            "gpt-5.4",
            5,
            __import__("threading").Event(),
            None,
            SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)),
        )

        assert captured["kwargs"]["reasoning_config"] == {"effort": "low"}

    def test_run_agent_sync_disables_reasoning_for_explicit_request_none(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "high"}}
        captured = {}

        class RecordingAgent:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.session_id = kwargs["session_id"]

            def run_conversation(self, **kwargs):
                return {"final_response": "ok"}

            def interrupt(self, message):
                pass

        module.AIAgent = RecordingAgent

        module._run_agent_sync(
            "hello",
            "sess-1",
            "gpt-5.4",
            5,
            __import__("threading").Event(),
            None,
            SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)),
            reasoning_effort="none",
        )

        assert captured["kwargs"]["reasoning_config"] == {"enabled": False}

    def test_run_agent_sync_disables_reasoning_when_config_none_and_request_omitted(self):
        module = load_server_module()
        module.hermes_config.load_config = lambda: {"agent": {"reasoning_effort": "none"}}
        captured = {}

        class RecordingAgent:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.session_id = kwargs["session_id"]

            def run_conversation(self, **kwargs):
                return {"final_response": "ok"}

            def interrupt(self, message):
                pass

        module.AIAgent = RecordingAgent

        module._run_agent_sync(
            "hello",
            "sess-1",
            "gpt-5.4",
            5,
            __import__("threading").Event(),
            None,
            SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)),
        )

        assert captured["kwargs"]["reasoning_config"] == {"enabled": False}

    def test_run_agent_sync_interrupts_agent_when_cancel_event_is_set(self):
        module = load_server_module()
        interrupted = __import__("threading").Event()

        class InterruptibleAgent:
            def __init__(self, **kwargs):
                self.session_id = kwargs["session_id"]

            def interrupt(self, message):
                interrupted.set()

            def run_conversation(self, **kwargs):
                for _ in range(50):
                    if interrupted.is_set():
                        return {"final_response": "[interrupted]"}
                    __import__("time").sleep(0.01)
                return {"final_response": "ok"}

        module.AIAgent = InterruptibleAgent
        cancel_event = __import__("threading").Event()

        async def trigger_cancel():
            await asyncio.sleep(0.05)
            cancel_event.set()

        async def exercise():
            loop = asyncio.get_running_loop()
            asyncio.create_task(trigger_cancel())
            return await loop.run_in_executor(
                None,
                lambda: module._run_agent_sync(
                    "hello",
                    "sess-1",
                    "gpt-5.4",
                    5,
                    cancel_event,
                    None,
                    loop,
                ),
            )

        result = run(exercise())

        assert interrupted.is_set() is True
        assert result["final_response"] == "[interrupted]"
