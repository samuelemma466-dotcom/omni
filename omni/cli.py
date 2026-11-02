from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from omni import __version__
from omni.agent import Agent
from omni.audit import AuditLog
from omni.broker import Broker
from omni.config import (
    AUDIT_PATH,
    CONFIG_PATH,
    STATE_DIR,
    TOKEN_PATH,
    ensure_dirs,
    load_config,
    plugin_dirs,
    server_token,
    write_default_config,
)
from omni.plugins import load_plugins
from omni.providers import ProviderError, build_provider, provider_status
from omni.registry import Context, Registry
from omni.tools import register_builtin_tools


def _build(config: dict[str, Any], interactive: bool = False):
    audit = AuditLog()
    registry = Registry()
    register_builtin_tools(registry)
    plugins = load_plugins(plugin_dirs(config), registry)
    broker = Broker(config.get("policy", {}), audit)
    ctx = Context(config=config, broker=broker, audit=audit, interactive=interactive)
    return registry, broker, audit, plugins, ctx


def _asker(broker: Broker, assume_yes: bool):
    def ask(request: Any) -> None:
        public = request.public() if hasattr(request, "public") else request
        if assume_yes:
            broker.decide(public["id"], True, "once")
            return
        if not sys.stdin.isatty():
            broker.decide(public["id"], False, "once")
            return
        print()
        print(f"  approval needed: {public['tool']}  [{public['danger']}]")
        print(f"  why: {public['reason']}")
        print(f"  args: {json.dumps(public['args'], ensure_ascii=False)[:1200]}")
        if public.get("preview"):
            print("  preview:")
            for line in str(public["preview"]).splitlines()[:40]:
                print(f"    {line}")
        answer = input("  allow? [y]es / [s]ession / [N]o: ").strip().lower()
        if answer in {"y", "yes"}:
            broker.decide(public["id"], True, "once")
        elif answer in {"s", "session"}:
            broker.decide(public["id"], True, "session")
        else:
            broker.decide(public["id"], False, "once")

    return ask


def cmd_init(args: argparse.Namespace) -> int:
    path = write_default_config(force=args.force)
    server_token()
    print(f"config: {path}")
    print(f"token:  {TOKEN_PATH}")
    print("next:   omni doctor")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    status = provider_status(config)
    print(f"omni {__version__}")
    print(f"config: {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'using defaults'})")
    print(f"state:  {STATE_DIR}")
    print()
    print("provider:")
    print(f"  configured: {status['configured']}")
    print(f"  resolved:   {status.get('resolved') or 'NONE'}")
    if status.get("model"):
        print(f"  model:      {status['model']}")
    ollama = status.get("ollama", {})
    if ollama.get("reachable"):
        models = ollama.get("models") or []
        print(f"  ollama:     reachable, models: {', '.join(models) if models else '(none)'}")
    else:
        print(f"  ollama:     unreachable ({ollama.get('error', '')[:120]})")
    for name in ("gemini", "groq", "openrouter", "openai"):
        print(f"  {name + ':':<12}key {'set' if status.get(name, {}).get('key_set') else 'not set'}")
    print(f"  pollinations: keyless free fallback (always available)")
    chain = status.get("chain") or []
    if chain:
        print(f"  fallback:   {' -> '.join(chain)}")
    if status.get("error"):
        print(f"  hint: {status['error']}")
    print()
    print("plugins:")
    _, _, _, plugins, _ = _build(config)
    if not plugins:
        print("  (none)")
    for plugin in plugins:
        print(f"  {plugin.name}: {', '.join(plugin.tools) if plugin.tools else 'no tools'}"
              + (f"  ERROR: {plugin.error}" if plugin.error else ""))
    print()
    print("usb tooling:")
    for tool in ("lsusb", "lsblk", "udevadm", "udisksctl", "wipefs", "dd", "pkexec", "sudo"):
        print(f"  {tool:<10} {'ok' if shutil.which(tool) else 'missing'}")
    print()
    print("phone tooling:")
    for tool in ("mtp-detect", "adb", "ideviceinfo", "usbmuxd"):
        print(f"  {tool:<10} {'ok' if shutil.which(tool) else 'missing'}")
    print()
    print(f"audit: {AUDIT_PATH}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    config = load_config()
    registry, _, _, _, _ = _build(config)
    if args.json:
        print(json.dumps(registry.specs(), indent=2, ensure_ascii=False))
        return 0
    for name in registry.names():
        spec = registry.get(name)
        assert spec is not None
        caps = ",".join(sorted(spec.capabilities))
        print(f"{name:<18} [{spec.danger:<6}] [{caps}] {spec.description}")
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    config = load_config()
    _, _, _, plugins, _ = _build(config)
    for plugin in plugins:
        print(f"{plugin.name}  {plugin.path}")
        print(f"  tools: {', '.join(plugin.tools) if plugin.tools else '(none)'}")
        if plugin.error:
            print(f"  error: {plugin.error}")
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    config = load_config()
    if args.args_file:
        raw = Path(args.args_file).read_text(encoding="utf-8")
    else:
        raw = args.args or "{}"
    try:
        tool_args = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON arguments: {exc}", file=sys.stderr)
        return 2
    registry, broker, _, _, ctx = _build(config, interactive=args.interactive)
    ctx.dry_run = args.dry_run
    result = registry.invoke(
        args.tool,
        tool_args,
        ctx,
        notify=_asker(broker, args.yes),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    return 0 if result.ok else 1


def cmd_chat(args: argparse.Namespace) -> int:
    config = load_config()
    registry, broker, audit, _, ctx = _build(config, interactive=True)
    ctx.dry_run = args.dry_run
    try:
        provider = build_provider(config, args.provider)
    except ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 1

    ask = _asker(broker, args.yes)

    def emit(event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "status":
            print("…", end="", flush=True)
        elif kind == "tool_call":
            print(f"\n  → {event['tool']} {json.dumps(event['args'], ensure_ascii=False)[:300]}")
        elif kind == "approval_required":
            ask(event["request"])
        elif kind == "tool_result":
            mark = "ok" if event.get("ok") else f"failed: {event.get('error')}"
            print(f"  ← {event['tool']}: {mark}")
        elif kind == "assistant":
            print("\n" + str(event.get("content", "")))
        elif kind == "error":
            print(f"\nerror: {event.get('message')}", file=sys.stderr)

    agent = Agent(provider, registry, broker, config, emit=emit, ctx=ctx)
    history: list[dict[str, Any]] = []

    if args.message:
        try:
            answer = agent.run(args.message, history=history)
        except ProviderError as exc:
            print(f"provider error: {exc}", file=sys.stderr)
            return 1
        return 0 if answer else 1

    print("omni chat — Ctrl-D to quit")
    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return 0
        try:
            answer = agent.run(line, history=history)
        except ProviderError as exc:
            print(f"provider error: {exc}", file=sys.stderr)
            continue
        history.append({"role": "user", "content": line})
        history.append({"role": "assistant", "content": answer})
        history = history[-20:]


def cmd_serve(args: argparse.Namespace) -> int:
    from omni.server import serve

    config = load_config()
    if args.port:
        config["server"]["port"] = args.port
    if args.host:
        config["server"]["host"] = args.host
    try:
        serve(config)
    except OSError as exc:
        print(f"cannot start server: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    audit = AuditLog()
    for record in audit.tail(args.limit):
        print(json.dumps(record, ensure_ascii=False, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omni", description="Permissioned local AI agent for Omarchy.")
    parser.add_argument("--version", action="version", version=f"omni {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write the default config and server token")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check providers, plugins, and USB tooling")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("tools", help="list tools")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("plugins", help="list loaded plugins")
    p.set_defaults(func=cmd_plugins)

    p = sub.add_parser("call", help="call one tool directly")
    p.add_argument("tool")
    p.add_argument("--args", help="JSON object of arguments", default="{}")
    p.add_argument("--args-file", help="read arguments from a JSON file")
    p.add_argument("--dry-run", action="store_true", help="plan only, change nothing")
    p.add_argument("--yes", action="store_true", help="auto-approve prompts")
    p.add_argument("--interactive", action="store_true", help="allow sudo to prompt in this terminal")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("chat", help="chat with the agent (one-shot or REPL)")
    p.add_argument("message", nargs="?", help="one-shot prompt; omit for a REPL")
    p.add_argument("--provider", help="override provider: ollama, gemini, groq, openrouter, openai")
    p.add_argument("--dry-run", action="store_true", help="tools plan only, change nothing")
    p.add_argument("--yes", action="store_true", help="auto-approve prompts")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("serve", help="run the local HTTP/SSE daemon for the overlay")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("audit", help="show recent audit events")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
