from __future__ import annotations

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
                    {"function": {"name": call["name"], "arguments": call.get("arguments", {})}}
                    for call in calls
                ]
            out.append(converted)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "content": str(message.get("content", "")),
                    "tool_name": message.get("name") or "tool",
                }
            )
        else:
            out.append({"role": role, "content": message.get("content", "")})
    return out


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float = 600) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def models(self) -> list[str]:
        data = http_json(f"{self.base_url}/api/tags", timeout=5)
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatReply:
        if not self.model:
            raise ProviderError("no Ollama model configured; set agent.model or providers.ollama.model")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(messages),
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 1024},
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
        data = http_json(f"{self.base_url}/api/chat", payload, timeout=self.timeout)
        message = data.get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"ollama_{index}"),
                    name=str(function.get("name", "")),
                    arguments=_as_arguments(function.get("arguments")),
                )
            )
        return ChatReply(content=str(message.get("content") or ""), tool_calls=calls, raw=data)
