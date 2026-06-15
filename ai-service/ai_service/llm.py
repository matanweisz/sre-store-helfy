"""Provider-neutral LLM seam for the agent loop.

The agent loop in ``app.py`` is written against the :class:`LLMClient` protocol
and never sees a provider's wire format. Two implementations back it:

- :class:`OpenRouterClient` — the OpenAI SDK pointed at OpenRouter (the default,
  multi-provider path; behavior identical to the pre-1c loop).
- :class:`AnthropicClient` — the native Anthropic SDK, which adds **prompt
  caching** on the stable system+tools prefix and **structured outputs** for the
  incident report.

Each client owns its own native message history; the loop only exchanges the
neutral :class:`AssistantTurn` / :class:`ToolCall` types with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings


# ─── neutral types ───────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantTurn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    """What the agent loop needs from a provider. Implementations own their own
    message history across a single investigation."""

    model: str

    def start(self, system: str, user: str) -> None: ...
    def step(self, tools: list[dict[str, Any]]) -> AssistantTurn: ...
    def add_tool_result(self, tool_call_id: str, content: str) -> None: ...
    def structure(self, prose: str, schema: dict[str, Any]) -> dict[str, Any] | None: ...


# ─── shared helpers ──────────────────────────────────────────────────────────

_STRUCTURE_SYSTEM = (
    "You convert an SRE incident note into a structured JSON object. Use only "
    "information present in the note — do not invent evidence or numbers."
)


def _structure_user(prose: str) -> str:
    return f"Incident note:\n\n{prose}\n\nReturn the structured report."


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing ```json fence if the model wrapped its JSON."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _safe_json(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(_strip_code_fence(text))
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


# ─── OpenRouter (OpenAI SDK) ─────────────────────────────────────────────────


class OpenRouterClient:
    """OpenAI SDK pointed at OpenRouter. Owns OpenAI-style message history."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.model = settings.openrouter_model
        self._messages: list[dict[str, Any]] = []
        if client is not None:
            self._client = client
            return
        from openai import OpenAI  # lazy: keep import cost out of unrelated paths

        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY env var is empty")
        import httpx

        self._client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            default_headers={
                "HTTP-Referer": "https://github.com/matanweisz/ai-observability-shop",
                "X-Title": "AI-Driven Observability",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
            max_retries=1,
        )

    def start(self, system: str, user: str) -> None:
        self._messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def step(self, tools: list[dict[str, Any]]) -> AssistantTurn:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=self._messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
        )
        msg = resp.choices[0].message

        assistant: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            assistant["content"] = msg.content
        if msg.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        self._messages.append(assistant)

        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return AssistantTurn(text=msg.content, tool_calls=calls)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def structure(self, prose: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        if not prose.strip():
            return None
        msgs = [
            {"role": "system", "content": _STRUCTURE_SYSTEM},
            {"role": "user", "content": _structure_user(prose)},
        ]
        # Attempt 1: strict json_schema response format.
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=msgs,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "incident_report", "strict": True, "schema": schema},
                },
                temperature=0,
            )
            return _safe_json(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001 — fall through to the portable path
            pass
        # Attempt 2: plain "JSON only" instruction.
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _STRUCTURE_SYSTEM + " Respond with ONLY a JSON object."},
                    {"role": "user", "content": _structure_user(prose) + f"\n\nJSON keys: {list(schema['properties'])}"},
                ],
                temperature=0,
            )
            return _safe_json(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            return None


# ─── native Anthropic ────────────────────────────────────────────────────────


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI function-tool schema → Anthropic tool schema."""
    out = []
    for t in tools:
        fn = t["function"]
        out.append({"name": fn["name"], "description": fn["description"], "input_schema": fn["parameters"]})
    return out


class AnthropicClient:
    """Native Anthropic SDK. Adds prompt caching on the system+tools prefix.

    Caching: a ``cache_control`` breakpoint on the system block caches the system
    prompt + tool definitions together (render order is tools → system), so every
    turn after the first reads that prefix from cache instead of reprocessing it.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.model = settings.anthropic_model
        self._max_tokens = settings.anthropic_max_tokens
        self._system: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []
        if client is not None:
            self._client = client
            return
        import anthropic  # lazy: only required when this provider is selected

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY env var is empty")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def start(self, system: str, user: str) -> None:
        self._system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self._messages = [{"role": "user", "content": user}]

    def step(self, tools: list[dict[str, Any]]) -> AssistantTurn:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=self._system,
            messages=self._messages,
            tools=_to_anthropic_tools(tools),
            tool_choice={"type": "auto"},
        )
        # Preserve the native content blocks for the next request's history.
        self._messages.append({"role": "assistant", "content": resp.content})

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        return AssistantTurn(text="".join(text_parts) or None, tool_calls=calls)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": content}]}
        )

    def structure(self, prose: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        if not prose.strip():
            return None
        # Attempt 1: structured outputs via output_config.format.
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=_STRUCTURE_SYSTEM,
                messages=[{"role": "user", "content": _structure_user(prose)}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            return _safe_json(_first_text(resp))
        except Exception:  # noqa: BLE001 — fall through
            pass
        # Attempt 2: plain "JSON only" instruction.
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=_STRUCTURE_SYSTEM + " Respond with ONLY a JSON object.",
                messages=[
                    {"role": "user", "content": _structure_user(prose) + f"\n\nJSON keys: {list(schema['properties'])}"}
                ],
            )
            return _safe_json(_first_text(resp))
        except Exception:  # noqa: BLE001
            return None


def _first_text(resp: Any) -> str:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


# ─── factory ─────────────────────────────────────────────────────────────────


def build_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings)
    return OpenRouterClient(settings)
