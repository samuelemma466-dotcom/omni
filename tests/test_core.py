from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Isolate state before importing modules that read XDG paths at import time.
_SANDBOX = Path(tempfile.mkdtemp(prefix="omni-tests-"))
os.environ["XDG_CONFIG_HOME"] = str(_SANDBOX / "config")
os.environ["XDG_STATE_HOME"] = str(_SANDBOX / "state")

import tomllib

from omni.agent import Agent
from omni.audit import AuditLog
from omni.broker import Broker, Denied, ToolSpec
from omni.config import DEFAULTS, render_default_config
from omni.plugins import load_plugins
from omni.providers.base import ChatReply, Provider, ProviderError, ToolCall
from omni.providers.fallback import FallbackProvider
from omni.registry import Context, Registry, validate_args


def _spec(name="demo", caps=("read",), danger="low", **kwargs):
    return ToolSpec(
        name=name,
        description="demo",
        input_schema=kwargs.pop(
            "input_schema", {"type": "object", "properties": {"path": {"type": "string"}}}
        ),
        capabilities=frozenset(caps),
        danger=danger,
        handler=kwargs.pop("handler", lambda args, ctx: {"ok": True}),
        **kwargs,
    )


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid_toml(self):
        parsed = tomllib.loads(render_default_config())
        self.assertEqual(parsed["server"]["port"], DEFAULTS["server"]["port"])
        self.assertEqual(parsed["policy"]["write"], "ask")

    def test_policy_has_every_capability(self):
        for capability in ("read", "write", "exec", "network", "device"):
            self.assertIn(capability, DEFAULTS["policy"])


class AuditTests(unittest.TestCase):
    def test_write_and_tail(self):
        path = _SANDBOX / "audit.jsonl"
        audit = AuditLog(path)
        audit.write("allowed", tool="fs.read")
        audit.write("denied", tool="shell.exec")
        records = audit.tail(2)
        self.assertEqual([r["event"] for r in records], ["allowed", "denied"])
        self.assertEqual(records[0]["tool"], "fs.read")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditLog(_SANDBOX / "broker.jsonl")

    def _broker(self, **policy):
        base = {"read": "allow", "write": "allow", "exec": "allow", "device": "allow"}
        base.update(policy)
        return Broker(base, self.audit)

    def test_read_allowed_without_prompt(self):
        broker = self._broker()
        result = broker.authorize(_spec(), {"path": "/tmp/x"})
        self.assertIsNone(result)

    def test_policy_deny(self):
        broker = self._broker(read="deny")
        with self.assertRaises(Denied):
            broker.authorize(_spec(), {})

    def test_high_danger_always_asks(self):
        broker = self._broker(exec="allow")
        spec = _spec(name="shell.exec", caps=("exec",), danger="high")
        decision, reason = broker.evaluate(spec, {})
        self.assertEqual(decision, "ask")
        self.assertIn("high-risk", reason)

    def test_write_outside_roots_asks(self):
        broker = self._broker(write="allow", write_roots=["/tmp"])
        spec = _spec(caps=("write",), danger="medium", scope_args=("path",))
        self.assertEqual(broker.evaluate(spec, {"path": "/tmp/ok.txt"})[0], "allow")
        self.assertEqual(broker.evaluate(spec, {"path": "/etc/passwd"})[0], "ask")
        self.assertEqual(broker.evaluate(spec, {"path": "~/.ssh/id_ed25519"})[0], "ask")

    def test_approval_and_session_grant(self):
        broker = self._broker(exec="ask")
        spec = _spec(name="shell.exec", caps=("exec",), danger="high")
        seen = []

        def notify(request):
            seen.append(request)
            broker.decide(request.id, True, "session")

        result = broker.authorize(spec, {"command": "ls"}, notify=notify)
        self.assertEqual(result.status, "approved")
        self.assertEqual(len(seen), 1)
        # A session grant skips the next prompt.
        self.assertIsNone(broker.authorize(spec, {"command": "ls"}))

    def test_denial_raises(self):
        broker = self._broker(exec="ask")
        spec = _spec(name="shell.exec", caps=("exec",), danger="high")
        with self.assertRaises(Denied):
            broker.authorize(spec, {}, notify=lambda req: broker.decide(req.id, False))


class RegistryTests(unittest.TestCase):
    def test_invoke_and_validation(self):
        registry = Registry()
        registry.register(
            ToolSpec(
                name="demo.add",
                description="add",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
                capabilities=frozenset({"read"}),
                handler=lambda args, ctx: {"sum": args["a"] + args["b"]},
            )
        )
        ctx = Context(config={})
        self.assertEqual(registry.invoke("demo.add", {"a": 2, "b": 3}, ctx).data, {"sum": 5})
        self.assertFalse(registry.invoke("demo.add", {"a": 2}, ctx).ok)
        self.assertFalse(registry.invoke("demo.add", {"a": "x", "b": 3}, ctx).ok)
        self.assertFalse(registry.invoke("nope", {}, ctx).ok)

    def test_validate_args(self):
        schema = {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}, "items": {"type": "array"}},
            "required": ["flag"],
        }
        self.assertIsNone(validate_args(schema, {"flag": True}))
        self.assertIn("required", validate_args(schema, {}))
        self.assertIn("boolean", validate_args(schema, {"flag": "yes"}))


class PluginTests(unittest.TestCase):
    def test_loads_python_plugin(self):
        root = _SANDBOX / "plugins"
        plugin = root / "demo"
        plugin.mkdir(parents=True)
        (plugin / "manifest.json").write_text(
            """
            {
              "name": "demo",
              "runtime": "python",
              "entry": "handler.py",
              "tools": [
                {
                  "name": "demo.echo",
                  "description": "echo",
                  "capabilities": ["read"],
                  "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]
                  }
                }
              ]
            }
            """
        )
        (plugin / "handler.py").write_text(
            "def run(tool_name, args, ctx):\n"
            "    return {'tool': tool_name, 'text': args['text']}\n"
        )
        registry = Registry()
        loaded = load_plugins([root], registry)
        self.assertEqual(len(loaded), 1)
        self.assertIsNone(loaded[0].error)
        result = registry.invoke("demo.echo", {"text": "hi"}, Context(config={}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"tool": "demo.echo", "text": "hi"})


class _FakeProvider:
    """Replays scripted replies so agent behaviour can be tested without a model."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        return self.replies.pop(0) if self.replies else ChatReply(content="done")


def _agent(provider, config=None, registry=None):
    registry = registry or Registry()
    if registry.get("demo.read") is None:
        registry.register(
            ToolSpec(
                name="demo.read",
                description="read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                capabilities=frozenset({"read"}),
                handler=lambda args, ctx: {"content": "hello"},
            )
        )
    config = config or {"agent": {"max_steps": 6}}
    broker = Broker({"read": "allow"}, AuditLog())
    ctx = Context(config=config, broker=broker)
    return Agent(provider, registry, broker, config, ctx=ctx)


class AgentTests(unittest.TestCase):
    def test_answers_without_tools(self):
        provider = _FakeProvider([ChatReply(content="hi there")])
        self.assertEqual(_agent(provider).run("hello"), "hi there")
        self.assertEqual(provider.calls, 1)

    def test_retries_once_on_empty_reply(self):
        provider = _FakeProvider([ChatReply(content=""), ChatReply(content="second try")])
        self.assertEqual(_agent(provider).run("hello"), "second try")
        self.assertEqual(provider.calls, 2)

    def test_duplicate_tool_calls_stop_the_loop(self):
        call = ToolCall(id="c1", name="demo.read", arguments={"path": "/tmp/x"})
        provider = _FakeProvider([ChatReply(content="", tool_calls=[call]) for _ in range(5)])
        events = []
        agent = _agent(provider)
        agent.emit = events.append
        answer = agent.run("read it")
        self.assertIn("kept repeating", answer)
        # First call executes, two repeats are served from cache, then we stop.
        self.assertEqual(provider.calls, 3)
        self.assertTrue(any(e.get("type") == "tool_result" for e in events))

    def test_deadline_stops_a_slow_turn(self):
        class SlowProvider(_FakeProvider):
            def chat(self, messages, tools):
                import time

                time.sleep(0.05)
                return super().chat(messages, tools)

        call = ToolCall(id="c1", name="demo.read", arguments={"path": "/tmp/x"})
        provider = SlowProvider([ChatReply(content="", tool_calls=[call]), ChatReply(content="late")])
        agent = _agent(provider, {"agent": {"max_steps": 6, "timeout": 0.01}})
        self.assertIn("longer than", agent.run("read it"))


class _BoomProvider(Provider):
    name = "boom"

    def chat(self, messages, tools):
        raise ProviderError("no route to host")


class _EchoProvider(Provider):
    name = "echo"

    def chat(self, messages, tools):
        return ChatReply(content="from echo")


class FallbackProviderTests(unittest.TestCase):
    def test_uses_the_next_provider(self):
        provider = FallbackProvider([_BoomProvider(), _EchoProvider()])
        self.assertEqual(provider.chain, ["boom", "echo"])
        self.assertEqual(provider.chat([], []).content, "from echo")

    def test_reports_every_failure(self):
        provider = FallbackProvider([_BoomProvider(), _BoomProvider()])
        with self.assertRaises(ProviderError) as ctx:
            provider.chat([], [])
        self.assertIn("boom", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
