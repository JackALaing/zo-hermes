"""Runtime-only patches for upstream Hermes code.

These patches live in zo-hermes so they survive Hermes updates under /opt/hermes-agent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERMES_ROOT = Path("/opt/hermes-agent")
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
_OPENAI_PATCH_FLAG = "_zo_safe_chat_message_patch"


def _patch_openai_chat_message() -> None:
    try:
        from openai.types.chat.chat_completion_message import ChatCompletionMessage
    except Exception:
        return

    if getattr(ChatCompletionMessage, _OPENAI_PATCH_FLAG, False):
        return

    original_getattribute = ChatCompletionMessage.__getattribute__
    original_setattr = ChatCompletionMessage.__setattr__
    safe_fields = {"tool_calls", "content", "reasoning_details"}

    def patched_getattribute(self, name):
        try:
            return original_getattribute(self, name)
        except ValueError as exc:
            if name in safe_fields and "closed file" in str(exc).lower():
                return object.__getattribute__(self, "__dict__").get(name)
            raise

    def patched_setattr(self, name, value):
        if name in safe_fields:
            try:
                object.__setattr__(self, name, value)
                return
            except Exception:
                pass
        return original_setattr(self, name, value)

    ChatCompletionMessage.__getattribute__ = patched_getattribute
    ChatCompletionMessage.__setattr__ = patched_setattr
    setattr(ChatCompletionMessage, _OPENAI_PATCH_FLAG, True)



def apply_runtime_patches() -> None:
    import agent.display as display

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

    _patch_openai_chat_message()
