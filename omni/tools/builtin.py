from __future__ import annotations

import difflib
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from omni.broker import ToolSpec
from omni.config import expand_path
from omni.registry import Context, Registry, ToolResult


def _path(raw: str) -> Path:
    return expand_path(raw)


def _display(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home) else text


# --------------------------------------------------------------------- fs.list
def fs_list(args: dict[str, Any], ctx: Context) -> ToolResult:
    path = _path(args.get("path", "~"))
    if not path.exists():
        return ToolResult(False, error=f"not found: {_display(path)}")
    if not path.is_dir():
        return ToolResult(False, error=f"not a directory: {_display(path)}")
    include_hidden = bool(args.get("include_hidden", False))
    entries = []
    for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if not include_hidden and entry.name.startswith("."):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else ("link" if entry.is_symlink() else "file"),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    return ToolResult(True, data={"path": _display(path), "entries": entries[:500]})


# --------------------------------------------------------------------- fs.read
def fs_read(args: dict[str, Any], ctx: Context) -> ToolResult:
    path = _path(args["path"])
    limit = int(args.get("max_bytes", 200_000))
    if not path.exists():
        return ToolResult(False, error=f"not found: {_display(path)}")
    if path.is_dir():
        return ToolResult(False, error=f"is a directory: {_display(path)}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ToolResult(False, error=str(exc))
    if b"\x00" in raw[:8192]:
        return ToolResult(False, error=f"binary file, not shown: {_display(path)}")
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")
    return ToolResult(
        True,
        data={"path": _display(path), "content": text, "truncated": truncated, "bytes": len(raw)},
    )


# -------------------------------------------------------------------- fs.write
def _write_preview(args: dict[str, Any]) -> str:
    path = _path(args["path"])
    new = str(args.get("content", ""))
    old = ""
    if path.exists() and path.is_file():
        try:
            old = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            old = ""
    diff = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{_display(path)}",
            tofile=f"b/{_display(path)}",
            n=3,
        )
    )
    if not diff:
        return "(no change)"
    text = "".join(diff)
    return text[:4000] + ("\n… (diff truncated)" if len(text) > 4000 else "")


def fs_write(args: dict[str, Any], ctx: Context) -> ToolResult:
    path = _path(args["path"])
    content = args["content"]
    if not isinstance(content, str):
        return ToolResult(False, error="content must be a string")
    existed = path.exists()
    if path.exists() and path.is_dir():
        return ToolResult(False, error=f"is a directory: {_display(path)}")
    if ctx.dry_run:
        return ToolResult(
            True,
            data={"dry_run": True, "path": _display(path), "bytes": len(content.encode())},
        )
    if args.get("create_dirs", True):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        True,
        data={"path": _display(path), "bytes": len(content.encode()), "created": not existed},
    )


# -------------------------------------------------------------------- fs.mkdir
def fs_mkdir(args: dict[str, Any], ctx: Context) -> ToolResult:
    path = _path(args["path"])
    if ctx.dry_run:
        return ToolResult(True, data={"dry_run": True, "path": _display(path)})
    path.mkdir(parents=True, exist_ok=bool(args.get("exist_ok", True)))
    return ToolResult(True, data={"path": _display(path), "created": True})


# ------------------------------------------------------------------ shell.exec
def shell_exec(args: dict[str, Any], ctx: Context) -> ToolResult:
    if "argv" in args:
        argv = args["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            return ToolResult(False, error="argv must be a non-empty list of strings")
    else:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(False, error="provide argv or command")
        argv = shlex.split(command)

    cwd = args.get("cwd")
    workdir = _path(cwd) if cwd else Path.home()
    timeout = float(args.get("timeout", 60))
    plan = {"argv": argv, "cwd": str(workdir), "timeout": timeout}
    if ctx.dry_run:
        return ToolResult(True, data={"dry_run": True, **plan})
    if not shutil.which(argv[0]) and not Path(argv[0]).exists():
        return ToolResult(False, error=f"program not found: {argv[0]}")
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"timed out after {timeout:g}s", meta=plan)
    except OSError as exc:
        return ToolResult(False, error=str(exc), meta=plan)
    return ToolResult(
        proc.returncode == 0,
        data={
            "returncode": proc.returncode,
            "stdout": proc.stdout[:16000],
            "stderr": proc.stderr[:8000],
        },
        error=None if proc.returncode == 0 else f"exit code {proc.returncode}",
        meta={"argv": argv, "cwd": str(workdir), "duration_ms": int((time.time() - started) * 1000)},
    )


# --------------------------------------------------------------------- sys.info
def sys_info(args: dict[str, Any], ctx: Context) -> ToolResult:
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            mem[key] = rest.strip()
    except OSError:
        pass
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        uptime = 0.0
    disk = shutil.disk_usage("/")
    batteries = []
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        try:
            batteries.append(
                {
                    "name": bat.name,
                    "capacity": int((bat / "capacity").read_text().strip()),
                    "status": (bat / "status").read_text().strip(),
                }
            )
        except (OSError, ValueError):
            continue
    return ToolResult(
        True,
        data={
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "kernel": platform.release(),
            "uptime_hours": round(uptime / 3600, 1),
            "load": [round(x, 2) for x in os.getloadavg()],
            "memory": {
                "total": mem.get("MemTotal"),
                "available": mem.get("MemAvailable"),
            },
            "disk_root": {
                "total_gb": round(disk.total / 1e9, 1),
                "used_gb": round(disk.used / 1e9, 1),
                "free_gb": round(disk.free / 1e9, 1),
            },
            "batteries": batteries,
            "python": platform.python_version(),
        },
    )


def register_builtin_tools(registry: Registry) -> None:
    registry.register(
        ToolSpec(
            name="fs.list",
            description="List a directory. Read-only.",
            capabilities=frozenset({"read"}),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (supports ~)."},
                    "include_hidden": {"type": "boolean"},
                },
                "required": ["path"],
            },
            handler=fs_list,
        )
    )
    registry.register(
        ToolSpec(
            name="fs.read",
            description="Read a UTF-8 text file. Read-only.",
            capabilities=frozenset({"read"}),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                },
                "required": ["path"],
            },
            handler=fs_read,
        )
    )
    registry.register(
        ToolSpec(
            name="fs.write",
            description="Create or overwrite a text file. Shows a diff for approval.",
            capabilities=frozenset({"write"}),
            danger="medium",
            scope_args=("path",),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_dirs": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            handler=fs_write,
            preview=_write_preview,
        )
    )
    registry.register(
        ToolSpec(
            name="fs.mkdir",
            description="Create a directory (and parents).",
            capabilities=frozenset({"write"}),
            danger="medium",
            scope_args=("path",),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=fs_mkdir,
        )
    )
    registry.register(
        ToolSpec(
            name="shell.exec",
            description=(
                "Run a program with an argument vector (no shell, no pipes). "
                "Use argv, or command which is split with shlex."
            ),
            capabilities=frozenset({"exec"}),
            danger="high",
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "number"},
                },
            },
            handler=shell_exec,
        )
    )
    registry.register(
        ToolSpec(
            name="sys.info",
            description="Host, kernel, uptime, load, memory, disk, and battery summary.",
            capabilities=frozenset({"read"}),
            input_schema={"type": "object", "properties": {}},
            handler=sys_info,
        )
    )


def dumps(result: ToolResult) -> str:
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
