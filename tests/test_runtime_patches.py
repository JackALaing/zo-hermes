"""Regression tests for zo-hermes runtime patches."""

from __future__ import annotations

import sys
from pathlib import Path

from openai import BaseModel as OpenAIBaseModel

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from runtime_patches import _OPENAI_PATCH_FLAG, _patch_openai_base_model


class DummyMessage(OpenAIBaseModel):
    content: str | None = None
    tool_calls: list | None = None


def _reset_openai_base_model(original_getattribute, original_setattr, original_flag):
    OpenAIBaseModel.__getattribute__ = original_getattribute
    OpenAIBaseModel.__setattr__ = original_setattr
    if original_flag is None:
        try:
            delattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG)
        except AttributeError:
            pass
    else:
        setattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG, original_flag)


def test_openai_base_model_patch_recovers_closed_file_get_and_set():
    original_getattribute = OpenAIBaseModel.__getattribute__
    original_setattr = OpenAIBaseModel.__setattr__
    original_flag = getattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG, None)

    instance = DummyMessage.model_construct(content="hello", tool_calls=["a"])

    def fake_getattribute(self, name):
        if name in {"content", "tool_calls"}:
            raise ValueError("I/O operation on closed file.")
        return object.__getattribute__(self, name)

    def fake_setattr(self, name, value):
        if name in {"content", "tool_calls"}:
            raise ValueError("I/O operation on closed file.")
        return object.__setattr__(self, name, value)

    try:
        OpenAIBaseModel.__getattribute__ = fake_getattribute
        OpenAIBaseModel.__setattr__ = fake_setattr
        if hasattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG):
            delattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG)

        _patch_openai_base_model()

        assert instance.content == "hello"
        assert instance.tool_calls == ["a"]

        instance.tool_calls = ["b"]
        assert object.__getattribute__(instance, "__dict__")["tool_calls"] == ["b"]
    finally:
        _reset_openai_base_model(original_getattribute, original_setattr, original_flag)


def test_openai_base_model_patch_preserves_non_closed_file_value_errors():
    original_getattribute = OpenAIBaseModel.__getattribute__
    original_setattr = OpenAIBaseModel.__setattr__
    original_flag = getattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG, None)

    instance = DummyMessage.model_construct(content="hello")

    def fake_getattribute(self, name):
        if name == "content":
            raise ValueError("some other value error")
        return object.__getattribute__(self, name)

    try:
        OpenAIBaseModel.__getattribute__ = fake_getattribute
        if hasattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG):
            delattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG)

        _patch_openai_base_model()

        try:
            _ = instance.content
        except ValueError as exc:
            assert "some other value error" in str(exc)
        else:
            raise AssertionError("expected ValueError to propagate")
    finally:
        _reset_openai_base_model(original_getattribute, original_setattr, original_flag)
