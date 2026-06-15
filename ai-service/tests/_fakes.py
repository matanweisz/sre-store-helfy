"""Lightweight fakes mimicking the OpenAI SDK response shape, so the agent loop
can be driven through scripted turns with no network. Reused across phases."""

from __future__ import annotations

from typing import Any


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = _Fn(name, arguments)


class _Message:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or None  # None when empty, like the real SDK


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


def assistant(content: str) -> _Response:
    """A terminal assistant turn (prose, no tool calls)."""
    return _Response(_Message(content=content))


def tool_call(call_id: str, name: str, arguments: str) -> _Response:
    """An assistant turn that calls one tool."""
    return _Response(_Message(tool_calls=[_ToolCall(call_id, name, arguments)]))


class FakeOpenAIClient:
    """Returns queued responses in order from ``chat.completions.create``."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Completions:
            def create(self, **kwargs: Any) -> _Response:
                outer.calls.append(kwargs)
                if not outer._responses:
                    raise AssertionError("FakeOpenAIClient ran out of queued responses")
                return outer._responses.pop(0)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()
