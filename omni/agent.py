from __future__ import annotations

import json
from typing import Any, Callable

from omni.broker import Broker
from omni.config import DEFAULT_SYSTEM_PROMPT
from omni.providers.base import Provider
from omni.registry import Context, Registry

Emit = Callable[[dict[str, Any]], None]


def _tool_message_content(result: Any) -> str:
    text = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    if len(text) > 20_000:
        text = text[:20_000] + "… (truncated)"
    return text


class Agent:
    def __init__(
        self,
        provider: Provider,
        registry: Registry,
        broker: Broker,
        config: dict[str, Any],
        emit: Emit | None = None,
        ctx: Context | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.broker = broker
        self.config = config
        self.emit = emit or (lambda event: None)
        self.ctx = ctx or Context(config=config, broker=broker)
        self.max_steps = int(config.get("agent", {}).get("max_steps", 10))
        self.system_prompt = (
            config.get("agent", {}).get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        )

    def _notify(self, request: Any) -> None:
        self.emit({"type": "approval_required", "request": request.public()})

    def run(self, user_text: str, history: list[dict[str, Any]] | None = None) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for item in history or []:
            if item.get("role") in {"user", "assistant"} and item.get("content"):
                messages.append({"role": item["role"], "content": str(item["content"])})
        messages.append({"role": "user", "content": user_text})

        tools = self.registry.specs()
        for step in range(self.max_steps):
            self.emit({"type": "status", "text": "thinking", "step": step + 1})
            reply = self.provider.chat(messages, tools)

            if not reply.tool_calls:
                content = reply.content or "(empty response)"
                messages.append({"role": "assistant", "content": content})
                self.emit({"type": "assistant", "content": content})
                self.emit({"type": "done", "steps": step + 1})
                return content

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [call.canonical() for call in reply.tool_calls],
                }
            )

            for call in reply.tool_calls:
                self.emit(
                    {
                        "type": "tool_call",
                        "tool": call.name,
                        "args": call.arguments,
                        "id": call.id,
                    }
                )
                result = self.registry.invoke(
                    call.name, call.arguments, self.ctx, notify=self._notify
                )
                preview = json.dumps(result.to_dict(), ensure_ascii=False, default=str)[:4000]
                self.emit(
                    {
                        "type": "tool_result",
                        "tool": call.name,
                        "id": call.id,
                        "ok": result.ok,
                        "error": result.error,
                        "preview": preview,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": _tool_message_content(result),
                    }
                )

        message = f"Stopped after {self.max_steps} tool steps without a final answer."
        self.emit({"type": "error", "message": message})
        return message
