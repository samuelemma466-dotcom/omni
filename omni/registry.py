from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from omni.audit import AuditLog
from omni.broker import Broker, Denied, ToolSpec


@dataclass
class Context:
    config: dict[str, Any]
    broker: Broker | None = None
    audit: AuditLog | None = None
    interactive: bool = False
    dry_run: bool = False


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            out["data"] = self.data
        if self.error:
            out["error"] = self.error
        if self.meta:
            out["meta"] = self.meta
        return out


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Small JSON-Schema subset validator. Returns an error string or None."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    required = schema.get("required", [])
    for key in required:
        if key not in args:
            return f"missing required argument: {key}"
    properties = schema.get("properties", {})
    for key, value in args.items():
        spec = properties.get(key)
        if spec is None:
            continue
        expected = spec.get("type")
        checks = {
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }
        if expected in checks and not checks[expected](value):
            return f"argument {key!r} must be of type {expected}"
    return None


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.handler is None:
            raise ValueError(f"{spec.name}: tool has no handler")
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [self._tools[name].llm_schema() for name in self.names()]

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        ctx: Context,
        *,
        authorize: bool = True,
        notify: Callable[..., None] | None = None,
    ) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(False, error=f"unknown tool: {name}")
        if not isinstance(args, dict):
            return ToolResult(False, error="arguments must be an object")
        problem = validate_args(spec.input_schema, args)
        if problem:
            return ToolResult(False, error=problem)

        if authorize and ctx.broker is not None and not ctx.dry_run:
            try:
                ctx.broker.authorize(spec, args, notify=notify)
            except Denied as exc:
                return ToolResult(False, error=f"denied: {exc}")

        if ctx.audit is not None:
            ctx.audit.write("tool_start", tool=name, args=args, dry_run=ctx.dry_run)
        try:
            output = spec.handler(args, ctx)  # type: ignore[misc]
        except Exception as exc:  # tools must not crash the agent
            result = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        else:
            if isinstance(output, ToolResult):
                result = output
            elif isinstance(output, dict):
                result = ToolResult(True, data=output)
            else:
                result = ToolResult(True, data=output)
        if ctx.audit is not None:
            ctx.audit.write(
                "tool_end",
                tool=name,
                ok=result.ok,
                error=result.error,
                dry_run=ctx.dry_run,
            )
        return result
