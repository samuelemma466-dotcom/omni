from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="omni-server-tests-"))
os.environ["XDG_CONFIG_HOME"] = str(_SANDBOX / "config")
os.environ["XDG_STATE_HOME"] = str(_SANDBOX / "state")

from omni.broker import ToolSpec  # noqa: E402
from omni.providers.base import ChatReply, ToolCall  # noqa: E402
from omni.server import Handler, Runtime  # noqa: E402


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ChatReply(content="", tool_calls=[ToolCall("c1", "demo.approval", {"x": 1})])
        return ChatReply(content="all done", tool_calls=[])


class ServerFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = {
            "server": {"host": "127.0.0.1", "port": 0, "approval_timeout": 10},
            "agent": {"provider": "auto", "model": "", "max_steps": 3, "system_prompt": "test"},
            "providers": {},
            "policy": {
                "read": "allow",
                "write": "allow",
                "exec": "ask",
                "network": "allow",
                "device": "ask",
                "write_roots": ["~", "/tmp"],
                "approval_timeout": 10,
            },
            "plugins": {"dirs": []},
        }
        cls.runtime = Runtime(config)
        cls.runtime._provider = FakeProvider()
        cls.runtime.registry.register(
            ToolSpec(
                name="demo.approval",
                description="test tool",
                input_schema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                    "required": ["x"],
                },
                capabilities=frozenset({"exec"}),
                danger="high",
                handler=lambda args, ctx: {"ran": True, "x": args["x"]},
            )
        )
        Handler.runtime = cls.runtime
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_health_needs_no_token(self):
        with urllib.request.urlopen(self._url("/health"), timeout=5) as response:
            body = json.loads(response.read())
        self.assertTrue(body["ok"])

    def test_tools_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self._url("/api/tools"), timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_chat_stream_with_approval(self):
        payload = json.dumps({"message": "do the thing"}).encode()
        request = urllib.request.Request(
            self._url("/api/chat"),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Omni-Token": self.runtime.token,
            },
        )
        events = []
        with urllib.request.urlopen(request, timeout=20) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] == "approval_required":
                    approval = json.dumps({"approved": True, "scope": "once"}).encode()
                    approve_request = urllib.request.Request(
                        self._url(f"/api/approvals/{event['request']['id']}"),
                        data=approval,
                        headers={
                            "Content-Type": "application/json",
                            "X-Omni-Token": self.runtime.token,
                        },
                    )
                    with urllib.request.urlopen(approve_request, timeout=5) as approved:
                        self.assertEqual(approved.status, 200)
                if event["type"] == "done":
                    break

        kinds = [event["type"] for event in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("approval_required", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("assistant", kinds)
        result = [event for event in events if event["type"] == "tool_result"][0]
        self.assertTrue(result["ok"])
        assistant = [event for event in events if event["type"] == "assistant"][0]
        self.assertEqual(assistant["content"], "all done")


if __name__ == "__main__":
    unittest.main()
