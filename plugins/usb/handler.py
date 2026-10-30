from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from omni.config import expand_path
from omni.registry import Context, ToolResult

SYS_USB = Path("/sys/bus/usb/devices")
RECIPES_DIR = Path(__file__).resolve().parent / "recipes"
IMAGE_CHUNK = 4 * 1024 * 1024


# ------------------------------------------------------------------ utilities
def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _run(
    argv: list[str],
    *,
    sudo: bool = False,
    ctx: Context,
    timeout: float = 900,
    cwd: str | None = None,
) -> dict[str, Any]:
    argv = [str(part) for part in argv]
    if sudo:
        if ctx.interactive and sys.stdin.isatty():
            argv = ["sudo", *argv]
        elif shutil.which("pkexec"):
            argv = ["pkexec", shutil.which(argv[0]) or argv[0], *argv[1:]]
        else:
            argv = ["sudo", "-n", *argv]
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"argv": argv, "rc": None, "error": f"timed out after {timeout:g}s"}
    except OSError as exc:
        return {"argv": argv, "rc": None, "error": str(exc)}
    return {
        "argv": argv,
        "rc": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
        "duration_ms": int((time.time() - started) * 1000),
    }


# ------------------------------------------------------------------ usb.list
def enumerate_usb(root: Path = SYS_USB) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    if not root.is_dir():
        return devices
    for entry in root.iterdir():
        vendor = _read(entry / "idVendor")
        product = _read(entry / "idProduct")
        if not vendor or not product:
            continue
        try:
            bus = int(_read(entry / "busnum", "0"))
            devnum = int(_read(entry / "devnum", "0"))
        except ValueError:
            bus = devnum = 0
        devices.append(
            {
                "bus": bus,
                "device": devnum,
                "vendor_id": vendor.lower(),
                "product_id": product.lower(),
                "manufacturer": _read(entry / "manufacturer"),
                "product": _read(entry / "product"),
                "serial": _read(entry / "serial"),
                "speed_mbps": _read(entry / "speed"),
                "usb_class": _read(entry / "bDeviceClass"),
                "sysfs": str(entry),
            }
        )
    return sorted(devices, key=lambda d: (d["bus"], d["device"]))


def usb_list(args: dict[str, Any], ctx: Context) -> ToolResult:
    devices = enumerate_usb()
    return ToolResult(True, data={"count": len(devices), "devices": devices})


# ----------------------------------------------------------------- usb.drives
def lsblk_data() -> dict[str, Any]:
    proc = subprocess.run(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,PATH,TYPE,SIZE,TRAN,MODEL,SERIAL,RM,HOTPLUG,ROTA,MOUNTPOINTS",
        ],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lsblk failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)


def _index_parents(
    nodes: list[dict[str, Any]],
    parent: dict[str, Any] | None = None,
    parent_of: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    parent_of = parent_of if parent_of is not None else {}
    for node in nodes:
        if parent is not None:
            parent_of[node.get("path", "")] = parent
        _index_parents(node.get("children") or [], node, parent_of)
    return parent_of


def _clean(node: dict[str, Any]) -> dict[str, Any]:
    mounts = node.get("mountpoints") or []
    return {
        "name": node.get("name"),
        "path": node.get("path"),
        "size": node.get("size"),
        "size_gb": round((node.get("size") or 0) / 1e9, 2),
        "tran": node.get("tran"),
        "model": (node.get("model") or "").strip(),
        "serial": (node.get("serial") or "").strip(),
        "removable": bool(node.get("rm")),
        "hotplug": bool(node.get("hotplug")),
        "readonly": bool(node.get("ro")),
        "mountpoints": [m for m in mounts if m],
        "children": [_clean(child) for child in node.get("children") or []],
    }


def enumerate_drives(data: dict[str, Any], include_all: bool = False) -> list[dict[str, Any]]:
    drives = []
    for node in data.get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        removable = bool(node.get("rm")) or bool(node.get("hotplug")) or node.get("tran") == "usb"
        if not include_all and not removable:
            continue
        drives.append(_clean(node))
    return drives


def usb_drives(args: dict[str, Any], ctx: Context) -> ToolResult:
    try:
        data = lsblk_data()
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        return ToolResult(False, error=str(exc))
    drives = enumerate_drives(data, include_all=bool(args.get("all", False)))
    return ToolResult(True, data={"count": len(drives), "drives": drives})


# ---------------------------------------------------------------- usb.recipes
def load_recipes(directory: Path = RECIPES_DIR) -> list[dict[str, Any]]:
    recipes = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["_path"] = str(path)
        recipes.append(data)
    return recipes


def match_recipes(devices: list[dict[str, Any]], recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for recipe in recipes:
        vendor = str(recipe.get("vendor", "")).lower()
        product = str(recipe.get("product", "")).lower()
        matched = [
            device
            for device in devices
            if device["vendor_id"] == vendor and device["product_id"] == product
        ]
        out.append(
            {
                "id": recipe.get("id"),
                "name": recipe.get("name"),
                "vendor": vendor,
                "product": product,
                "description": recipe.get("description", ""),
                "requires_terminal": bool(recipe.get("requires_terminal")),
                "connected": bool(matched),
                "matched_devices": matched,
            }
        )
    return out


def usb_recipes(args: dict[str, Any], ctx: Context) -> ToolResult:
    recipes = match_recipes(enumerate_usb(), load_recipes())
    if not args.get("all", False):
        recipes = [r for r in recipes if r["connected"]]
    return ToolResult(True, data={"count": len(recipes), "recipes": recipes})


# ------------------------------------------------------------ install_driver
class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _expand(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(_SafeDict(mapping))
    if isinstance(value, list):
        return [_expand(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, mapping) for key, item in value.items()}
    return value


def _recipe_by_id(recipe_id: str) -> dict[str, Any] | None:
    for recipe in load_recipes():
        if recipe.get("id") == recipe_id:
            return recipe
    return None


def _plan_step(step: dict[str, Any]) -> dict[str, Any]:
    kind = step.get("kind")
    if kind == "pacman":
        return {"kind": kind, "argv": ["pacman", "-S", "--needed", "--noconfirm", *step.get("packages", [])], "sudo": True}
    if kind == "command":
        return {"kind": kind, "argv": step.get("argv", []), "cwd": step.get("cwd"), "sudo": bool(step.get("sudo"))}
    if kind == "copy":
        return {"kind": kind, "from": step.get("from"), "to": step.get("to")}
    if kind == "verify":
        return {"kind": kind, "argv": step.get("argv", [])}
    if kind == "cups_usb_queue":
        return {"kind": kind, "queue": step.get("queue"), "ppd": step.get("ppd"), "vendor": step.get("vendor")}
    if kind == "print_test":
        return {"kind": kind, "queue": step.get("queue"), "text": step.get("text", "")}
    return {"kind": kind, "unsupported": True, "step": step}


def _cups_usb_uri(vendor: str) -> str | None:
    backend = Path("/usr/lib/cups/backend/usb")
    if not backend.exists():
        return None
    proc = subprocess.run([str(backend)], text=True, capture_output=True, timeout=15)
    for line in proc.stdout.splitlines():
        if vendor.lower() not in line.lower():
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) >= 2 and parts[1].startswith("usb://"):
            return parts[1]
    return None


def install_driver(args: dict[str, Any], ctx: Context) -> ToolResult:
    recipe_id = str(args["recipe"])
    dry_run = bool(args.get("dry_run", ctx.dry_run))
    recipe = _recipe_by_id(recipe_id)
    if recipe is None:
        return ToolResult(False, error=f"unknown recipe: {recipe_id}")

    devices = enumerate_usb()
    matches = [
        device
        for device in devices
        if device["vendor_id"] == str(recipe.get("vendor", "")).lower()
        and device["product_id"] == str(recipe.get("product", "")).lower()
    ]
    if not matches and not args.get("force", False):
        return ToolResult(
            False,
            error=(
                f"{recipe.get('name')} is not connected. Plug it in and power it on, "
                "or pass force=true to plan anyway."
            ),
        )

    source_dir = expand_path(recipe.get("source_dir", "~"))
    if not dry_run and not source_dir.is_dir():
        return ToolResult(False, error=f"recipe source directory not found: {source_dir}")
    if not dry_run and recipe.get("requires_terminal") and not ctx.interactive:
        return ToolResult(
            False,
            error=(
                "this driver build needs a terminal (makepkg calls sudo). "
                f"Run: omni call usb.install_driver --args '{{\"recipe\":\"{recipe_id}\",\"dry_run\":false}}' --interactive --yes"
            ),
        )

    tmp = tempfile.mkdtemp(prefix="omni-usb-") if not dry_run else "$TMPDIR"
    mapping = {
        "user": os.environ.get("USER", ""),
        "home": str(Path.home()),
        "tmp": tmp,
        "recipe_dir": str(Path(recipe.get("_path", "")).parent),
        "source_dir": str(source_dir),
    }
    steps = [_plan_step(_expand(step, mapping)) for step in recipe.get("steps", [])]
    plan = {
        "recipe": recipe_id,
        "device": matches[0] if matches else None,
        "source_dir": str(source_dir),
        "steps": steps,
    }
    if dry_run:
        return ToolResult(True, data={"dry_run": True, **plan})

    log: list[dict[str, Any]] = []
    ok = True
    try:
        for step in steps:
            kind = step["kind"]
            if kind == "pacman":
                outcome = _run(step["argv"], sudo=True, ctx=ctx)
            elif kind == "command":
                outcome = _run(step["argv"], sudo=step.get("sudo", False), ctx=ctx, cwd=step.get("cwd"))
            elif kind == "copy":
                try:
                    Path(step["to"]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(step["from"], step["to"])
                    outcome = {"ok": True, "from": step["from"], "to": step["to"]}
                except OSError as exc:
                    outcome = {"ok": False, "error": str(exc)}
            elif kind == "verify":
                outcome = _run(step["argv"], ctx=ctx)
            elif kind == "cups_usb_queue":
                uri = _cups_usb_uri(str(step.get("vendor") or ""))
                if not uri:
                    outcome = {"ok": False, "error": "CUPS cannot see the printer on USB"}
                else:
                    queue = step["queue"]
                    _run(["lpadmin", "-x", queue], sudo=True, ctx=ctx)
                    outcome = _run(
                        ["lpadmin", "-p", queue, "-E", "-v", uri, "-m", step["ppd"]],
                        sudo=True,
                        ctx=ctx,
                    )
                    outcome["uri"] = uri
                    if outcome.get("rc") == 0:
                        outcome = {**outcome, "ok": True}
            elif kind == "print_test":
                handle, name = tempfile.mkstemp(prefix="omni-test-", suffix=".txt")
                os.close(handle)
                Path(name).write_text(step.get("text", ""), encoding="utf-8")
                outcome = _run(["lp", "-d", step["queue"], name], sudo=True, ctx=ctx)
                Path(name).unlink(missing_ok=True)
            else:
                outcome = {"ok": False, "error": f"unsupported step kind: {kind}"}

            if "ok" not in outcome:
                outcome["ok"] = outcome.get("rc") == 0
            log.append({"step": step, "result": outcome})
            if not outcome.get("ok"):
                ok = False
                break
    finally:
        if not dry_run:
            shutil.rmtree(tmp, ignore_errors=True)

    return ToolResult(ok, data={"recipe": recipe_id, "log": log}, error=None if ok else "a step failed")


# ----------------------------------------------------------------- usb.flash
def root_disk_paths(data: dict[str, Any]) -> set[str]:
    nodes = data.get("blockdevices", [])
    parent_of = _index_parents(nodes)

    def walk(items: list[dict[str, Any]]):
        for node in items:
            yield node
            yield from walk(node.get("children") or [])

    roots: set[str] = set()
    for node in walk(nodes):
        mounts = node.get("mountpoints") or []
        if "/" not in mounts and "[SWAP]" not in mounts:
            continue
        current: dict[str, Any] | None = node
        seen: set[str] = set()
        while current is not None:
            key = current.get("path", "")
            if key in seen:
                break
            seen.add(key)
            if current.get("type") == "disk":
                roots.add(key)
                break
            current = parent_of.get(key)
    return {path for path in roots if path}


def _descendants(node: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for child in node.get("children") or []:
        out.append(child)
        out.extend(_descendants(child))
    return out


def validate_flash(
    *,
    image: Path,
    device: str,
    data: dict[str, Any],
    confirm: str | None,
    unmount: bool,
    allow_internal: bool,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if not image.exists() or not image.is_file():
        return None, f"image not found: {image}"
    if image.stat().st_size <= 0:
        return None, f"image is empty: {image}"
    if not device.startswith("/dev/"):
        return None, "device must be a /dev path"
    target = Path(device)
    try:
        mode = target.stat().st_mode
    except OSError as exc:
        return None, f"cannot stat {device}: {exc}"
    if not stat.S_ISBLK(mode):
        return None, f"not a block device: {device}"
    return validate_flash_plan(
        image_size=image.stat().st_size,
        image=str(image),
        device=device,
        data=data,
        confirm=confirm,
        unmount=unmount,
        allow_internal=allow_internal,
        dry_run=dry_run,
    )


def validate_flash_plan(
    *,
    image_size: int,
    image: str,
    device: str,
    data: dict[str, Any],
    confirm: str | None,
    unmount: bool,
    allow_internal: bool,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    disk = None
    for node in data.get("blockdevices", []):
        if node.get("path") == device:
            disk = node
            break
    if disk is None:
        return None, f"{device} is not a whole disk (or lsblk cannot see it)"
    if disk.get("type") != "disk":
        return None, f"{device} is a {disk.get('type')}, not a whole disk"

    removable = bool(disk.get("rm")) or bool(disk.get("hotplug")) or disk.get("tran") == "usb"

    roots = root_disk_paths(data)
    if device in roots:
        return None, f"{device} holds the running system; refusing"

    if not removable and not allow_internal:
        return None, f"{device} is not removable/USB; refusing (pass allow_internal=true to override)"

    disk_size = int(disk.get("size") or 0)
    if disk_size and image_size > disk_size:
        return None, f"image is larger than {device} ({image_size} > {disk_size} bytes)"

    mounted = [child for child in _descendants(disk) if child.get("mountpoints")]
    if mounted and not unmount:
        names = ", ".join(child.get("path", "?") for child in mounted)
        return None, f"mounted partitions on {device}: {names}; unmount them first or pass unmount=true"

    if not dry_run and confirm != device:
        return None, f"confirmation required: pass confirm={device!r} to write for real"

    plan = []
    for child in mounted:
        plan.append({"action": "unmount", "path": child.get("path")})
    plan.append(
        {
            "action": "write",
            "argv": ["dd", f"if={image}", f"of={device}", "bs=4M", "conv=fsync", "status=progress"],
            "sudo": True,
        }
    )
    plan.append({"action": "sync", "argv": ["sync"], "sudo": True})
    plan.append({"action": "reread", "argv": ["blockdev", "--rereadpt", device], "sudo": True})
    return {
        "device": _clean(disk),
        "image": image,
        "image_bytes": image_size,
        "plan": plan,
    }, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(IMAGE_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def flash(args: dict[str, Any], ctx: Context) -> ToolResult:
    dry_run = bool(args.get("dry_run", ctx.dry_run))
    image = expand_path(args["image"])
    device = str(args["device"])
    unmount = bool(args.get("unmount", True))
    allow_internal = bool(args.get("allow_internal", False))
    confirm = args.get("confirm")

    try:
        data = lsblk_data()
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        return ToolResult(False, error=str(exc))

    plan, error = validate_flash(
        image=image,
        device=device,
        data=data,
        confirm=confirm,
        unmount=unmount,
        allow_internal=allow_internal,
        dry_run=dry_run,
    )
    if error:
        return ToolResult(False, error=error)
    assert plan is not None

    expected = args.get("sha256")
    if expected:
        actual = _sha256(image)
        if actual.lower() != str(expected).lower():
            return ToolResult(False, error=f"sha256 mismatch: expected {expected}, got {actual}")
        plan["sha256"] = actual

    if dry_run:
        return ToolResult(True, data={"dry_run": True, **plan})

    log: list[dict[str, Any]] = []
    for step in plan["plan"]:
        action = step["action"]
        if action == "unmount":
            part = step["path"]
            if shutil.which("udisksctl"):
                outcome = _run(["udisksctl", "unmount", "-b", part], ctx=ctx, timeout=60)
                if outcome.get("rc") != 0:
                    outcome = _run(["umount", part], sudo=True, ctx=ctx, timeout=60)
            else:
                outcome = _run(["umount", part], sudo=True, ctx=ctx, timeout=60)
        else:
            outcome = _run(step["argv"], sudo=bool(step.get("sudo")), ctx=ctx, timeout=3600)
        outcome["ok"] = outcome.get("rc") == 0
        log.append({"action": action, "result": outcome})
        if not outcome["ok"]:
            return ToolResult(False, data={"log": log}, error=f"{action} failed")

    return ToolResult(True, data={"device": device, "image": str(image), "log": log})


# ---------------------------------------------------------------- plugin API
def run(tool_name: str, args: dict[str, Any], ctx: Context) -> ToolResult:
    tools = {
        "usb.list": usb_list,
        "usb.drives": usb_drives,
        "usb.recipes": usb_recipes,
        "usb.install_driver": install_driver,
        "usb.flash": flash,
    }
    handler = tools.get(tool_name)
    if handler is None:
        return ToolResult(False, error=f"unknown tool: {tool_name}")
    return handler(args, ctx)
