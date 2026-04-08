"""Runtime-only patches for upstream Hermes code.

These patches live in zo-hermes so they survive Hermes updates under /opt/hermes-agent.
"""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

HERMES_ROOT = Path(os.getenv("HERMES_ROOT", "/opt/hermes-agent")).expanduser()
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


class _SafeStreamProxy:
    """Best-effort wrapper for streams that may close underneath Hermes display code."""

    def __init__(self, inner):
        self._inner = inner

    def write(self, data):
        try:
            return self._inner.write(data)
        except (OSError, ValueError):
            return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def isatty(self):
        try:
            return hasattr(self._inner, "isatty") and self._inner.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


_PATCH_FLAG = "_zo_safe_output_patch"
_OPENAI_PATCH_FLAG = "_zo_safe_openai_model_patch"
_PRINT_PATCH_FLAG = "_zo_safe_print_patch"
_AGENT_PATCH_FLAG = "_zo_safe_agent_print_patch"


def _safe_model_value(model, name):
    try:
        state = object.__getattribute__(model, "__dict__")
    except Exception:
        state = {}
    if name in state:
        return state.get(name)

    for attr_name in ("__pydantic_extra__", "__pydantic_private__"):
        try:
            extra = object.__getattribute__(model, attr_name)
        except Exception:
            extra = None
        if isinstance(extra, dict) and name in extra:
            return extra.get(name)

    return None


def _patch_openai_base_model() -> None:
    try:
        from openai import BaseModel as OpenAIBaseModel
    except Exception:
        return

    if getattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG, False):
        return

    original_getattribute = OpenAIBaseModel.__getattribute__
    original_setattr = OpenAIBaseModel.__setattr__

    def patched_getattribute(self, name):
        try:
            return original_getattribute(self, name)
        except ValueError as exc:
            if "closed file" in str(exc).lower():
                return _safe_model_value(self, name)
            raise

    def patched_setattr(self, name, value):
        try:
            return original_setattr(self, name, value)
        except ValueError as exc:
            if "closed file" not in str(exc).lower():
                raise
            try:
                object.__setattr__(self, name, value)
                return
            except Exception:
                try:
                    state = object.__getattribute__(self, "__dict__")
                except Exception:
                    state = None
                if isinstance(state, dict):
                    state[name] = value
                    return
                raise

    OpenAIBaseModel.__getattribute__ = patched_getattribute
    OpenAIBaseModel.__setattr__ = patched_setattr
    setattr(OpenAIBaseModel, _OPENAI_PATCH_FLAG, True)


def _patch_stdio_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and not isinstance(stream, _SafeStreamProxy):
            setattr(sys, stream_name, _SafeStreamProxy(stream))


def _patch_builtin_print() -> None:
    original_print = builtins.print
    if getattr(original_print, _PRINT_PATCH_FLAG, False):
        return

    def safe_print(*args, **kwargs):
        try:
            return original_print(*args, **kwargs)
        except (OSError, ValueError):
            return None

    setattr(safe_print, _PRINT_PATCH_FLAG, True)
    builtins.print = safe_print


def _patch_agent_printing() -> None:
    import run_agent as run_agent_module

    agent_cls = run_agent_module.AIAgent
    if getattr(agent_cls, _AGENT_PATCH_FLAG, False):
        return

    original_safe_print = agent_cls._safe_print

    def patched_safe_print(self, *args, **kwargs):
        try:
            return original_safe_print(self, *args, **kwargs)
        except (OSError, ValueError):
            return None

    agent_cls._safe_print = patched_safe_print
    setattr(agent_cls, _AGENT_PATCH_FLAG, True)



def apply_runtime_patches() -> None:
    import agent.display as display

    _patch_stdio_streams()
    _patch_builtin_print()
    _patch_agent_printing()

    spinner_cls = display.KawaiiSpinner
    if not getattr(spinner_cls, _PATCH_FLAG, False):
        original_init = spinner_cls.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            if not isinstance(self._out, _SafeStreamProxy):
                self._out = _SafeStreamProxy(self._out)

        def patched_write_tty(text: str) -> None:
            try:
                fd = os.open("/dev/tty", os.O_WRONLY)
                try:
                    os.write(fd, text.encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                try:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                except (OSError, ValueError):
                    pass

        spinner_cls.__init__ = patched_init
        setattr(spinner_cls, _PATCH_FLAG, True)
        display.write_tty = patched_write_tty

    _patch_openai_base_model()
