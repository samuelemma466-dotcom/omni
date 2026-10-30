from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from omni import __version__
from omni.agent import Agent
from omni.audit import AuditLog
from omni.broker import Broker
from omni.config import load_config, plugin_dirs, server_token
from omni.plugins import load_plugins
from omni.providers import ProviderError, build_provider
from omni.registry import Context, Registry
from omni.tools import register_builtin_tools


class Runtime:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.audit = AuditLog()
        self.registry = Registry()
        register_builtin_tools(self.registry)
        self.plugins = load_plugins(plugin_dirs(config), self.registry)
        self.broker = Broker(config.get("policy", {}), self.audit)
        self.token = server_token()
        self._provider = None
        self._provider_lock = threading.Lock()

    def provider(self):
        with self._provider_lock:
            if self._provider is None:
                self._provider = build_provider(self.config)
            return self._provider

    def agent(self, emit) -> Agent:
        ctx = Context(config=self.config, broker=self.broker, audit=self.audit)
        return Agent(self.provider(), self.registry, self.broker, self.config, emit=emit, ctx=ctx)


class Handler(BaseHTTPRequestHandler):
    server_version = f"omni/{__version__}"
    runtime: Runtime

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[omni] %s\n" % (fmt % args))

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Omni-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _authorized(self) -> bool:
        parsed = urlparse(self.path)
        supplied = self.headers.get("X-Omni-Token", "")
        if not supplied:
            supplied = (parse_qs(parsed.query).get("token") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.runtime.token)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

    def _sse_send(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # --------------------------------------------------------------------- HTTP
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "version": __version__, "tools": len(self.runtime.registry.names())})
            return
        if not parsed.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "missing or invalid token"})
            return

        if parsed.path == "/api/tools":
            self._send(200, {"tools": self.runtime.registry.specs()})
        elif parsed.path == "/api/plugins":
            self._send(
                200,
                {
                    "plugins": [
                        {"name": p.name, "path": str(p.path), "tools": p.tools, "error": p.error}
                        for p in self.runtime.plugins
                    ]
                },
            )
        elif parsed.path == "/api/approvals":
            self._send(200, {"approvals": self.runtime.broker.pending()})
        elif parsed.path == "/api/audit":
            limit = int((parse_qs(parsed.query).get("limit") or ["20"])[0])
            self._send(200, {"events": self.runtime.audit.tail(max(1, min(limit, 500)))})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorized():
            self._send(401, {"error": "missing or invalid token"})
            return

        if parsed.path == "/api/chat":
            self._handle_chat()
            return

        if parsed.path.startswith("/api/approvals/"):
            request_id = parsed.path.rsplit("/", 1)[-1]
            try:
                body = self._read_json()
                request = self.runtime.broker.decide(
                    request_id,
                    bool(body.get("approved")),
                    str(body.get("scope", "once")),
                )
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
                return
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, {"approval": request.public()})
            return

        self._send(404, {"error": "not found"})

    def _handle_chat(self) -> None:
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        message = str(body.get("message") or "").strip()
        if not message:
            self._send(400, {"error": "message is required"})
            return
        history = body.get("history")
        if not isinstance(history, list):
            history = []

        self._sse_start()
        started = time.time()

        def emit(event: dict[str, Any]) -> None:
            try:
                self._sse_send(event)
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            agent = self.runtime.agent(emit)
            agent.run(message, history=history)
        except ProviderError as exc:
            emit({"type": "error", "message": f"provider error: {exc}"})
            emit({"type": "done", "steps": 0})
        except Exception as exc:  # keep the stream well-formed
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            emit({"type": "done", "steps": 0})
        finally:
            self.runtime.audit.write(
                "chat",
                message=message[:2000],
                duration_ms=int((time.time() - started) * 1000),
            )


def serve(config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    runtime = Runtime(config)
    Handler.runtime = runtime
    host = str(config.get("server", {}).get("host", "127.0.0.1"))
    port = int(config.get("server", {}).get("port", 8737))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"omni listening on http://{host}:{port}")
    print(f"token file: ~/.local/state/omni/token")
    for plugin in runtime.plugins:
        state = "ok" if not plugin.error else f"error: {plugin.error}"
        print(f"plugin {plugin.name}: {state}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
