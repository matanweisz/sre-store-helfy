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


# ─── Anthropic-shaped fakes ──────────────────────────────────────────────────


class _ATextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _AToolUseBlock:
    type = "tool_use"

    def __init__(self, block_id: str, name: str, inp: dict) -> None:
        self.id = block_id
        self.name = name
        self.input = inp


class _AResponse:
    def __init__(self, content: list) -> None:
        self.content = content


def a_text(text: str) -> _ATextBlock:
    return _ATextBlock(text)


def a_tool_use(block_id: str, name: str, inp: dict) -> _AToolUseBlock:
    return _AToolUseBlock(block_id, name, inp)


def a_response(*blocks: Any) -> _AResponse:
    return _AResponse(list(blocks))


class FakeAnthropicClient:
    """Returns queued responses in order from ``messages.create``."""

    def __init__(self, responses: list[_AResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Messages:
            def create(self, **kwargs: Any) -> _AResponse:
                outer.calls.append(kwargs)
                if not outer._responses:
                    raise AssertionError("FakeAnthropicClient ran out of queued responses")
                return outer._responses.pop(0)

        self.messages = _Messages()


# ─── neutral LLMClient fake (drives the agent loop) ──────────────────────────


class FakeLLMClient:
    """Implements the LLMClient protocol with scripted turns + a fixed structure
    result, so investigate() can be driven end-to-end without a network."""

    def __init__(self, turns: list, structured: dict | None = None, model: str = "fake-model") -> None:
        self.model = model
        self._turns = list(turns)
        self._structured = structured
        self.started: tuple[str, str] | None = None
        self.tool_results: list[tuple[str, str]] = []

    def start(self, system: str, user: str) -> None:
        self.started = (system, user)

    def step(self, tools: list[dict[str, Any]]):
        if not self._turns:
            raise AssertionError("FakeLLMClient ran out of scripted turns")
        return self._turns.pop(0)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.tool_results.append((tool_call_id, content))

    def structure(self, prose: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        return self._structured
