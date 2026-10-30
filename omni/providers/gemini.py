from __future__ import annotations

from typing import Any

from omni.providers.base import ChatReply, Provider, ProviderError, ToolCall, _as_arguments, http_json


def _gemini_schema(schema: Any) -> Any:
    """Gemini's REST API expects upper-case JSON-schema type names."""
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                out[key] = value.upper()
            elif key == "properties" and isinstance(value, dict):
                out[key] = {name: _gemini_schema(sub) for name, sub in value.items()}
            elif key in {"items", "additionalProperties"}:
                out[key] = _gemini_schema(value)
            elif key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
                out[key] = [_gemini_schema(item) for item in value]
            else:
                out[key] = value
        return out
    return schema


def _append(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "user":
            _append(contents, "user", [{"text": str(message.get("content", ""))}])
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.get("content"):
                parts.append({"text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                parts.append(
                    {"functionCall": {"name": call["name"], "args": call.get("arguments", {})}}
                )
            if parts:
                _append(contents, "model", parts)
        elif role == "tool":
            _append(
                contents,
                "user",
                [
                    {
                        "functionResponse": {
                            "name": message.get("name") or "tool",
                            "response": {"result": str(message.get("content", ""))},
                        }
                    }
                ],
            )
    return contents


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 180) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatReply:
        if not self.api_key:
            raise ProviderError("missing Gemini API key")
        system = "\n\n".join(
            str(message.get("content", "")) for message in messages if message.get("role") == "system"
        ).strip()
        payload: dict[str, Any] = {
            "contents": _contents(messages),
            "generationConfig": {"temperature": 0.2},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": _gemini_schema(
                                tool.get("parameters") or {"type": "object", "properties": {}}
                            ),
                        }
                        for tool in tools
                    ]
                }
            ]
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        data = http_json(url, payload, timeout=self.timeout)
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ProviderError(f"no candidates in Gemini response: {feedback or data}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text", "")) for part in parts)
        calls = []
        for index, part in enumerate(parts):
            function_call = part.get("functionCall")
            if not function_call:
                continue
            calls.append(
                ToolCall(
                    id=f"gemini_{index}",
                    name=str(function_call.get("name", "")),
                    arguments=_as_arguments(function_call.get("args")),
                )
            )
        return ChatReply(content=text, tool_calls=calls, raw=data)
