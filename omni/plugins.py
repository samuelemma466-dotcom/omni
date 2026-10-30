from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from omni.broker import ToolSpec
from omni.registry import Registry


@dataclass
class PluginLoad:
    name: str
    path: Path
    tools: list[str] = field(default_factory=list)
    error: str | None = None


def discover(dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for directory in dirs:
        if not directory.is_dir():
            continue
        for manifest in sorted(directory.glob("*/manifest.json")):
            resolved = manifest.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(manifest)
    return found


def _load_module(entry: Path, plugin_name: str):
    module_name = f"omni_plugin_{plugin_name.replace('.', '_').replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _make_handler(module: Any, tool_name: str) -> Callable[..., Any]:
    if hasattr(module, "run"):
        return lambda args, ctx: module.run(tool_name, args, ctx)
    tools = getattr(module, "TOOLS", None)
    if isinstance(tools, dict) and tool_name in tools:
        return tools[tool_name]
    raise AttributeError(f"plugin has neither run() nor TOOLS[{tool_name!r}]")


def load_plugin(manifest_path: Path, registry: Registry) -> PluginLoad:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return PluginLoad(manifest_path.parent.name, manifest_path.parent, error=str(exc))

    name = str(data.get("name") or manifest_path.parent.name)
    loaded = PluginLoad(name, manifest_path.parent)
    runtime = data.get("runtime", "python")
    if runtime != "python":
        loaded.error = f"unsupported runtime: {runtime}"
        return loaded

    entry_name = data.get("entry", "handler.py")
    entry = manifest_path.parent / entry_name
    if not entry.is_file():
        loaded.error = f"missing entry file: {entry_name}"
        return loaded

    try:
        module = _load_module(entry, name)
    except Exception as exc:
        loaded.error = f"{type(exc).__name__}: {exc}"
        return loaded

    for tool in data.get("tools", []):
        tool_name = tool.get("name")
        if not tool_name:
            loaded.error = "tool without a name"
            continue
        try:
            spec = ToolSpec(
                name=tool_name,
                description=str(tool.get("description", "")),
                input_schema=tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}},
                capabilities=frozenset(tool.get("capabilities", ["read"])),
                danger=str(tool.get("danger", "low")),
                handler=_make_handler(module, tool_name),
                scope_args=tuple(tool.get("scope_args", ())),
            )
            registry.register(spec)
            loaded.tools.append(tool_name)
        except Exception as exc:
            loaded.error = f"{tool_name}: {type(exc).__name__}: {exc}"
    return loaded


def load_plugins(dirs: list[Path], registry: Registry) -> list[PluginLoad]:
    results = []
    for manifest in discover(dirs):
        results.append(load_plugin(manifest, registry))
    return results
