from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from omni.config import AUDIT_PATH


class AuditLog:
    """Append-only JSONL record of every tool decision and invocation."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or AUDIT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-count:]
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
