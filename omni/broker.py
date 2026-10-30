from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from omni.audit import AuditLog
from omni.config import expand_path


class Capability(str, Enum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NETWORK = "network"
    DEVICE = "device"


CAPABILITIES = {cap.value for cap in Capability}
DANGER_LEVELS = {"low", "medium", "high"}

# Even if a policy says "allow", writes here always require a prompt.
PROTECTED_PREFIXES = (
    "/etc",
    "/usr",
    "/boot",
    "/var",
    "/opt",
    "/dev",
    str(Path.home() / ".ssh"),
    str(Path.home() / ".gnupg"),
)


class Denied(Exception):
    """The broker refused a tool call, or the user did not approve it."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    capabilities: frozenset[str]
    danger: str = "low"
    handler: Callable[..., Any] | None = None
    preview: Callable[[dict[str, Any]], str | None] | None = None
    # Argument names that hold filesystem paths, used for write scoping.
    scope_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = self.capabilities - CAPABILITIES
        if unknown:
            raise ValueError(f"{self.name}: unknown capabilities {sorted(unknown)}")
        if self.danger not in DANGER_LEVELS:
            raise ValueError(f"{self.name}: unknown danger level {self.danger!r}")

    def llm_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema or {"type": "object", "properties": {}},
        }


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    args: dict[str, Any]
    capabilities: list[str]
    danger: str
    reason: str
    preview: str | None
    created: float = field(default_factory=time.time)
    status: str = "pending"
    scope: str = "once"
    event: Any = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "capabilities": self.capabilities,
            "danger": self.danger,
            "reason": self.reason,
            "preview": self.preview,
            "created": self.created,
            "status": self.status,
        }


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


class Broker:
    """Decides allow/ask/deny for tool calls and blocks on human approval."""

    def __init__(
        self,
        policy: dict[str, Any],
        audit: AuditLog | None = None,
        default_timeout: float = 300,
    ) -> None:
        self.policy = policy
        self.audit = audit or AuditLog()
        self.timeout = float(policy.get("approval_timeout", default_timeout))
        self.write_roots = [expand_path(p).resolve() for p in policy.get("write_roots", ["~"])]
        self._pending: dict[str, ApprovalRequest] = {}
        self._grants: dict[str, float] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- evaluate
    def _path_allowed(self, raw: str) -> bool:
        try:
            resolved = expand_path(raw).resolve()
        except (OSError, RuntimeError):
            return False
        for prefix in PROTECTED_PREFIXES:
            try:
                if resolved == Path(prefix) or resolved.is_relative_to(Path(prefix)):
                    return False
            except OSError:
                continue
        return any(resolved == root or resolved.is_relative_to(root) for root in self.write_roots)

    def evaluate(self, spec: ToolSpec, args: dict[str, Any]) -> tuple[str, str]:
        """Return (allow|ask|deny, human-readable reason)."""
        with self._lock:
            grant_until = self._grants.get(spec.name, 0)
            if grant_until > time.time():
                return "allow", "approved for this session"

        reasons: list[str] = []
        for capability in sorted(spec.capabilities):
            decision = str(self.policy.get(capability, "ask")).lower()
            if decision == "deny":
                return "deny", f"policy denies {capability} tools"
            if decision == "ask":
                reasons.append(f"{capability} requires approval")

        if Capability.WRITE.value in spec.capabilities:
            for key in spec.scope_args:
                for raw in _iter_strings(args.get(key)):
                    if not self._path_allowed(raw):
                        reasons.append(f"write outside allowed roots: {raw}")

        if spec.danger == "high":
            reasons.append("high-risk action")

        if reasons:
            return "ask", "; ".join(dict.fromkeys(reasons))
        return "allow", "policy allows it"

    # --------------------------------------------------------------- authorize
    def authorize(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        notify: Callable[[ApprovalRequest], None] | None = None,
        timeout: float | None = None,
    ) -> ApprovalRequest | None:
        decision, reason = self.evaluate(spec, args)
        if decision == "deny":
            self.audit.write("denied", tool=spec.name, args=args, reason=reason)
            raise Denied(reason)
        if decision == "allow":
            self.audit.write("allowed", tool=spec.name, args=args, reason=reason)
            return None

        preview = None
        if spec.preview is not None:
            try:
                preview = spec.preview(args)
            except Exception as exc:  # preview must never break the call
                preview = f"(preview failed: {exc})"

        request = ApprovalRequest(
            id=uuid.uuid4().hex[:12],
            tool=spec.name,
            args=args,
            capabilities=sorted(spec.capabilities),
            danger=spec.danger,
            reason=reason,
            preview=preview,
        )
        with self._lock:
            self._pending[request.id] = request
        self.audit.write(
            "approval_requested",
            tool=spec.name,
            args=args,
            approval_id=request.id,
            reason=reason,
        )

        if notify is not None:
            notify(request)

        wait_for = self.timeout if timeout is None else timeout
        if not request.event.wait(wait_for):
            request.status = "timeout"
        with self._lock:
            self._pending.pop(request.id, None)

        if request.status != "approved":
            self.audit.write(
                "denied",
                tool=spec.name,
                args=args,
                approval_id=request.id,
                reason=request.status,
            )
            raise Denied(f"not approved ({request.status})")

        self.audit.write(
            "approved",
            tool=spec.name,
            args=args,
            approval_id=request.id,
            scope=request.scope,
        )
        return request

    def decide(self, request_id: str, approved: bool, scope: str = "once") -> ApprovalRequest:
        with self._lock:
            request = self._pending.get(request_id)
            if request is None:
                raise KeyError(f"no pending approval {request_id!r}")
            request.status = "approved" if approved else "denied"
            request.scope = scope
            if approved and scope == "session":
                self._grants[request.tool] = time.time() + self.timeout
        request.event.set()
        return request

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [req.public() for req in self._pending.values()]

    def grant(self, tool: str, seconds: float | None = None) -> None:
        with self._lock:
            self._grants[tool] = time.time() + (seconds if seconds is not None else self.timeout)

    def revoke(self, tool: str | None = None) -> None:
        with self._lock:
            if tool is None:
                self._grants.clear()
            else:
                self._grants.pop(tool, None)
