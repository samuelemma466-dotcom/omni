from __future__ import annotations

import datetime as dt
import json
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from omni.registry import Context, ToolResult

SYS_USB = Path("/sys/bus/usb/devices")

# Vendor IDs known to be phone makers. An entry is a strong hint, not proof;
# MTP/adb/idevice probes confirm what the device actually speaks.
PHONE_VENDORS = {
    "05ac": "Apple",
    "054c": "Sony",
    "0bb4": "HTC",
    "1004": "LG",
    "12d1": "Huawei/Honor",
    "18d1": "Google/Android",
    "19d2": "ZTE",
    "1bbb": "Alcatel/TCL",
    "22b8": "Motorola",
    "22d9": "OPPO",
    "2717": "Xiaomi",
    "2a45": "Meizu",
    "2a70": "OnePlus/Nothing",
    "2d95": "vivo",
    "2e81": "HMD/Nokia",
    "04e8": "Samsung",
}

WATCH_MIN, WATCH_MAX = 5.0, 600.0


def events_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "omni" / "phones" / "events.jsonl"


# ------------------------------------------------------------------ utilities
def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _run(
    argv: list[str],
    *,
    timeout: float,
) -> dict[str, Any] | None:
    binary = shutil.which(argv[0])
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, *argv[1:]],
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"rc": None, "stdout": "", "stderr": f"timed out after {timeout:g}s"}
    except OSError as exc:
        return {"rc": None, "stdout": "", "stderr": str(exc)}
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


# ------------------------------------------------------------------ classify
def classify_device(device: dict[str, Any]) -> dict[str, Any]:
    vendor = str(device.get("vendor_id") or "").lower()
    usb_class = str(device.get("usb_class") or "").lower()
    vendor_name = PHONE_VENDORS.get(vendor)
    if usb_class == "09" or vendor == "1d6b":
        kind = "hub"
    elif vendor_name:
        kind = "phone"
    else:
        kind = "unknown"
    out = dict(device)
    out["kind"] = kind
    out["phone"] = kind == "phone"
    out["vendor_name"] = vendor_name
    return out


def snapshot_usb(root: Path = SYS_USB) -> dict[str, dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    if root.is_dir():
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
    return {str(d["sysfs"]): d for d in devices}


def diff_snapshot(
    last: dict[str, dict[str, Any]],
    now: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (plugged, unplugged) device dicts between two snapshots."""
    plugged = [classify_device(dev) for sysfs, dev in now.items() if sysfs not in last]
    unplugged = [classify_device(dev) for sysfs, dev in last.items() if sysfs not in now]
    return plugged, unplugged


def _event(action: str, device: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ev = {
        "ts": dt.datetime.now().isoformat(timespec="milliseconds"),
        "action": action,
        "phone": bool(device.get("phone")),
        "kind": device.get("kind"),
        "vendor_name": device.get("vendor_name"),
        "vendor_id": device.get("vendor_id"),
        "product_id": device.get("product_id"),
        "product": device.get("product"),
        "manufacturer": device.get("manufacturer"),
        "serial": device.get("serial"),
        "sysfs": device.get("sysfs"),
    }
    if extra:
        ev.update(extra)
    return ev


def _append_events(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the event log is best-effort; never crash the watcher


def _append_mtp(events: list[dict[str, Any]]) -> None:
    """Attach MTP identity to plug events for candidate phones (best-effort)."""
    rextra = probe_mtp()
    for ev in events:
        if ev.get("phone") and rextra.get("connected"):
            ev["mtp"] = {k: v for k, v in rextra.items() if k not in ("connected",)}


# --------------------------------------------------------------- MTP (Android)
def _bytes_from_mtp_line(line: str) -> int | None:
    """libmtp prints e.g. 'Max Capacity: 0x... (1,508,288,000 bytes)'. Some
    vendors omit the parens or the unit, so fall back to the last integer."""
    match = re.search(r"\(([0-9][0-9,.\s\u00a0]*)bytes\)", line)
    if match:
        digits = re.sub(r"[^\d]", "", match.group(1))
        if digits:
            return int(digits)
    numbers = re.findall(r"(?<![\dx])(\d[\d,.\s\u00a0]*\d)", line)
    for chunk in reversed(numbers):
        digits = re.sub(r"[^\d]", "", chunk)
        if digits:
            try:
                return int(digits)
            except ValueError:
                continue
    return None


def parse_mtp(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"connected": True}
    for field, key in (
        ("Manufacturer", "manufacturer"),
        ("Model", "model"),
        ("Device version", "os_version"),
        ("Serial number", "serial"),
    ):
        match = re.search(rf"^\s*{field}:\s*(.+)$", text, re.MULTILINE)
        if match:
            out[key] = match.group(1).strip()
    if "android.com" in text:
        out["extensions"] = ["android_MTP"]
    battery = re.search(r"Battery level (\d+) of (\d+)", text)
    if battery:
        out["battery_percent"] = round(int(battery.group(1)) / int(battery.group(2)) * 100)
    storage: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        sid = re.match(r"\s*StorageID:\s*(\S+)", line)
        if sid:
            if current:
                storage.append(current)
            current = {
                "storage_id": sid.group(1),
                "capacity_bytes": None,
                "free_bytes": None,
                "description": None,
            }
        elif current is not None:
            # standard libmtp: "Max Capacity: 0x… (1,508,288,000 bytes)"
            # vivo dialect:    "MaxCapacity: 114921799680"
            if "MaxCapacity" in line or "Max Capacity" in line:
                current["capacity_bytes"] = _bytes_from_mtp_line(line)
            elif "FreeSpaceInBytes" in line or "Free Space in Bytes" in line:
                current["free_bytes"] = _bytes_from_mtp_line(line)
            elif "StorageDescription" in line:
                match = re.search(r"StorageDescription:\s*(.*)$", line)
                current["description"] = match.group(1).strip() if match else None
    if current:
        storage.append(current)
    out["storage"] = storage
    return out or {"connected": True}


def probe_mtp(timeout: float = 30.0) -> dict[str, Any]:
    if shutil.which("mtp-detect") is None:
        return {"connected": False, "available": False, "reason": "mtp-detect not installed (pacman -S libmtp)"}
    proc = _run(["mtp-detect"], timeout=timeout)
    if proc is None:  # pragma: no cover - guarded above
        return {"connected": False, "reason": "mtp-detect missing"}
    text = proc["stdout"]
    if not re.search(r"^\s*Device 0\b", text, re.MULTILINE):
        return {"connected": False, "available": True, "reason": "no MTP device found"}
    return parse_mtp(text)


# ------------------------------------------------------------------ adb (dev)
def parse_adb_devices(text: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    started = False
    for line in text.splitlines():
        if "List of devices attached" in line:
            started = True
            continue
        if not started:
            continue
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices


def probe_adb(timeout: float = 20.0) -> dict[str, Any]:
    if shutil.which("adb") is None:
        return {"available": False, "usb_debugging": False, "reason": "adb not installed (pacman -S android-tools)"}
    proc = _run(["adb", "devices"], timeout=timeout)
    if proc is None or proc["rc"] not in (0,):
        reason = (proc or {}).get("stderr", "").strip()[:200] or "adb failed"
        return {"available": True, "usb_debugging": False, "reason": reason}
    listing = parse_adb_devices(proc["stdout"])
    out: dict[str, Any] = {"available": True, "usb_debugging": bool(listing), "devices": []}
    for entry in listing:
        item = dict(entry)
        if entry["state"] == "device":
            props = _run(["adb", "-s", entry["serial"], "shell", "getprop"], timeout=15)
            if props and props["rc"] == 0:
                picked = {}
                for key in ("ro.product.brand", "ro.product.model", "ro.build.version.release"):
                    match = re.search(rf"\[{re.escape(key)}\]:\s*\[(.*?)\]", props["stdout"])
                    if match:
                        picked[key] = match.group(1)
                item["brand"] = picked.get("ro.product.brand", "")
                item["model"] = picked.get("ro.product.model", "")
                item["android_version"] = picked.get("ro.build.version.release", "")
        elif entry["state"] == "unauthorized":
            item["hint"] = "accept the RSA prompt on the phone screen"
        out["devices"].append(item)
    if not listing:
        out["reason"] = "no adb device: USB debugging (developer options) is off, or no phone attached"
    return out


# ------------------------------------------------------------------- iPhone
def probe_apple(timeout: float = 15.0) -> dict[str, Any]:
    if shutil.which("ideviceinfo") is None:
        return {"available": False, "reason": "ideviceinfo not installed (pacman -S libimobiledevice)"}
    proc = _run(["ideviceinfo", "-s"], timeout=timeout)
    if proc is None or proc["rc"] != 0:
        reason = (proc or {}).get("stderr", "").strip()[:200] or "not found via usbmuxd"
        if "usbmuxd" in reason.lower() and shutil.which("usbmuxd") is None:
            reason += " (usbmuxd is not running)"
        return {"connected": False, "available": True, "reason": reason}
    picked: dict[str, str] = {}
    keep = {"DeviceName", "ProductType", "ProductVersion", "SerialNumber", "SIMStatus", "ActivationState"}
    for line in proc["stdout"].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in keep:
            picked[key.strip()] = value.strip()
    return {"connected": bool(picked), "available": True, **picked} if picked else {
        "connected": False,
        "available": True,
        "reason": "no idevice response",
    }


# ------------------------------------------------------------ phones.detect
def detect(args: dict[str, Any], ctx: Context, root: Path = SYS_USB) -> ToolResult:
    snap = snapshot_usb(root)
    devices = [classify_device(dev) for dev in snap.values()]
    phones = [d for d in devices if d["phone"]]

    bridges: dict[str, Any] = {
        "mtp": {"available": shutil.which("mtp-detect") is not None},
        "adb": {"available": shutil.which("adb") is not None},
        "apple": {"available": shutil.which("ideviceinfo") is not None},
    }
    hint: str | None = None

    if phones:
        if args.get("probe", True):
            mtp = probe_mtp()
            adb = probe_adb()
            bridges["mtp"].update(mtp)
            bridges["adb"].update(adb)
            if any(d["vendor_id"] == "05ac" for d in phones):
                bridges["apple"].update(probe_apple())
            for phone in phones:
                phone["mtp"] = mtp
                phone["adb"] = adb
            if not mtp.get("connected") and not adb.get("usb_debugging"):
                hint = (
                    "phone on the bus but silent: unlock the screen and set USB mode to "
                    "File Transfer (MTP), or enable Developer Options > USB debugging for adb"
                )
    else:
        hint = (
            "no phone on the bus. If it is plugged in and only charging, the cable is "
            "likely charge-only (no data wires), the port may be dead, or the phone's USB "
            "mode is set to charge only. Swap cable/port, or run phones.watch to catch it live"
        )

    return ToolResult(
        True,
        data={
            "phone_count": len(phones),
            "phones": phones,
            "device_count": len(devices),
            "bridges": bridges,
            "hint": hint,
        },
    )


# ------------------------------------------------------------ phones.watch
def _readline_deadline(
    proc: subprocess.Popen[str], sel: selectors.BaseSelector, deadline: float
) -> str | None:
    """Read one udevadm line without blocking past the deadline. None = nothing/over."""
    remain = deadline - time.monotonic()
    if remain <= 0:
        return None
    ready = sel.select(timeout=min(remain, 1.0))
    if not ready:
        return None
    return proc.stdout.readline()  # type: ignore[union-attr]


def watch(args: dict[str, Any], ctx: Context, root: Path = SYS_USB) -> ToolResult:
    if not root.is_dir():
        return ToolResult(False, error=f"sysfs unavailable: {root}")
    seconds = float(args.get("seconds", 60))
    seconds = max(WATCH_MIN, min(WATCH_MAX, seconds))
    if not shutil.which("udevadm"):
        return ToolResult(False, error="udevadm not found (package: systemd)")

    last = snapshot_usb(root)
    events: list[dict[str, Any]] = []
    phones_seen: list[dict[str, Any]] = []

    try:
        proc = subprocess.Popen(
            ["udevadm", "monitor", "--kernel", "--subsystem-match=usb"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        return ToolResult(False, error=f"udevadm failed: {exc}")
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)

    deadline = time.monotonic() + seconds
    line_re = re.compile(r"KERNEL\[\d+\.\d+\]\s+(add|remove)\s+(\S+)\s+\(usb\)")
    try:
        while time.monotonic() < deadline:
            line = _readline_deadline(proc, selector, deadline)
            if line is None:
                continue
            if not line:
                break
            match = line_re.match(line.strip())
            if not match:
                continue
            # one device triggers several uevents (device, interfaces); settle first
            time.sleep(0.6)
            now = snapshot_usb(root)
            plugged, unplugged = diff_snapshot(last, now)
            fresh: list[dict[str, Any]] = []
            for dev in plugged:
                ev = _event("plug", dev)
                if dev["phone"]:
                    _append_mtp([ev])
                    phones_seen.append(ev)
                events.append(ev)
                fresh.append(ev)
            for dev in unplugged:
                ev = _event("unplug", dev)
                events.append(ev)
                fresh.append(ev)
            _append_events(fresh)
            last = now
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    return ToolResult(
        True,
        data={
            "watched_seconds": seconds,
            "event_count": len(events),
            "events": events,
            "phones_plugged": phones_seen,
            "events_file": str(events_path()),
        },
    )


# ------------------------------------------------------------- phones.log
def read_events(limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    path = path or events_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= max(1, limit):
            break
    return list(reversed(out))


def log(args: dict[str, Any], ctx: Context) -> ToolResult:
    limit = int(args.get("limit", 50))
    events = read_events(limit)
    return ToolResult(
        True,
        data={"count": len(events), "events": events, "events_file": str(events_path())},
    )


# ---------------------------------------------------------------- plugin API
def run(tool_name: str, args: dict[str, Any], ctx: Context) -> ToolResult:
    tools = {
        "phones.detect": detect,
        "phones.watch": watch,
        "phones.log": log,
    }
    handler = tools.get(tool_name)
    if handler is None:
        return ToolResult(False, error=f"unknown tool: {tool_name}")
    return handler(args, ctx)
