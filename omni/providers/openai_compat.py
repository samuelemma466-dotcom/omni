from __future__ import annotations

import json
from typing import Any

from omni.providers.base import ChatReply, Provider, ProviderError, ToolCall, _as_arguments, http_json


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            converted: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            calls = message.get("tool_calls") or []
            if calls:
                converted["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments", {})),
                        },
                    }
                    for call in calls
                ]
            out.append(converted)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id") or "",
                    "content": str(message.get("content", "")),
                }
            )
        else:
            out.append({"role": role, "content": message.get("content", "")})
    return out


class OpenAICompatProvider(Provider):
    """Works with OpenAI, Groq, OpenRouter, and any OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 180,
        extra_headers: dict[str, str] | None = None,
        name: str = "openai-compat",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.name = name

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatReply:
        if not self.model:
            raise ProviderError("no model configured")
        headers = dict(self.extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(messages),
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
        data = http_json(f"{self.base_url}/chat/completions", payload, headers=headers, timeout=self.timeout)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"no choices in response: {json.dumps(data)[:400]}")
        message = choices[0].get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    name=str(function.get("name", "")),
                    arguments=_as_arguments(function.get("arguments")),
                )
            )
        return ChatReply(content=str(message.get("content") or ""), tool_calls=calls, raw=data)
