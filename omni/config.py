from __future__ import annotations

import copy
import os
import secrets
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omni"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omni"
CONFIG_PATH = CONFIG_DIR / "config.toml"
TOKEN_PATH = STATE_DIR / "token"
AUDIT_PATH = STATE_DIR / "audit.jsonl"

DEFAULT_SYSTEM_PROMPT = """You are Omni, a local assistant running on the user's Omarchy (Arch/Hyprland) machine.

You can call tools to read files, write files, run commands, inspect USB devices, install drivers,
and flash USB drives. Rules:
- Use tools when they give a real answer. Never invent tool output.
- Prefer the smallest, safest action. Read before writing.
- Destructive actions (writes outside the home directory, commands, flashing drives) need the
  user's approval; the permission broker enforces this independently of you.
- When a tool is denied or fails, report the exact error and suggest the next step.
- Keep answers short and concrete. Show the command or path you used when relevant.
"""

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8737,
        "approval_timeout": 300,
    },
    "agent": {
        "provider": "auto",
        "model": "",
        "max_steps": 10,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
    },
    "providers": {
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5:3b",
        },
        "gemini": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-2.0-flash",
            "api_key_env": "GEMINI_API_KEY",
        },
        "groq": {
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "api_key_env": "GROQ_API_KEY",
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key_env": "OPENAI_API_KEY",
        },
    },
    "policy": {
        "read": "allow",
        "write": "ask",
        "exec": "ask",
        "network": "ask",
        "device": "ask",
        "write_roots": ["~", "/tmp"],
        "approval_timeout": 300,
    },
    "plugins": {
        "dirs": ["~/.config/omni/plugins", "@project/plugins"],
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def expand_path(value: str | Path) -> Path:
    text = str(value)
    if text == "@project":
        return PROJECT_ROOT
    if text.startswith("@project/"):
        return PROJECT_ROOT / text[len("@project/") :]
    return Path(os.path.expandvars(os.path.expanduser(text)))


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        return copy.deepcopy(DEFAULTS)
    with config_path.open("rb") as handle:
        user_config = tomllib.load(handle)
    return _deep_merge(DEFAULTS, user_config)


def _render_value(value: Any) -> str:
    import json

    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    return str(value)


def _render_table(name: str, values: dict[str, Any], lines: list[str]) -> None:
    lines.append(f"[{name}]")
    subtables: list[tuple[str, dict[str, Any]]] = []
    for key, value in values.items():
        if isinstance(value, dict):
            subtables.append((key, value))
            continue
        lines.append(f"{key} = {_render_value(value)}")
    lines.append("")
    for key, value in subtables:
        _render_table(f"{name}.{key}", value, lines)


def render_default_config() -> str:
    """A config generated from DEFAULTS so it can never drift."""
    lines = ["# Omni configuration. Edit, then run: omni doctor", ""]
    for section, values in DEFAULTS.items():
        _render_table(section, values, lines)
    return "\n".join(lines)


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "plugins").mkdir(parents=True, exist_ok=True)


def write_default_config(force: bool = False) -> Path:
    ensure_dirs()
    if CONFIG_PATH.exists() and not force:
        return CONFIG_PATH
    CONFIG_PATH.write_text(render_default_config(), encoding="utf-8")
    return CONFIG_PATH


def server_token() -> str:
    ensure_dirs()
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    return token


def plugin_dirs(config: dict[str, Any]) -> list[Path]:
    dirs: list[Path] = []
    for raw in config.get("plugins", {}).get("dirs", []):
        dirs.append(expand_path(raw))
    return dirs


def provider_model(config: dict[str, Any], provider: str) -> str:
    explicit = str(config.get("agent", {}).get("model") or "").strip()
    if explicit:
        return explicit
    return str(config.get("providers", {}).get(provider, {}).get("model") or "")
