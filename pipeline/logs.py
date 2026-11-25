"""Logging: runtime log (pipeline stages, API calls, errors) and user action
log (uploads, imports, generates, deletes, settings changes). Stored as
JSONL in data/logs/, queryable by category and keyword via the API."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG_DIR = Path("data") / "logs"
_lock = threading.Lock()

# rotate size: 5MB per file
_MAX_BYTES = 5 * 1024 * 1024
_KEEP = 5

RUNTIME = "runtime"      # pipeline internals, API errors, downloads
ACTION = "action"        # user operations
PIPELINE = "pipeline"    # course generation lifecycle detail
MATERIAL = "material"    # material lifecycle: import/download/rename/delete/generate link


def _init_dir(data_dir: Path) -> Path:
    d = data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rotate(path: Path) -> None:
    if path.exists() and path.stat().st_size > _MAX_BYTES:
        for i in range(_KEEP - 1, 0, -1):
            older = path.with_suffix(path.suffix + f".{i}")
            newer = path.with_suffix(path.suffix + f".{i - 1}") if i > 1 else path
            if newer.exists():
                older.write_bytes(newer.read_bytes())
        path.write_text("", encoding="utf-8")


def log_event(category: str, message: str, *, data_dir: Path | None = None,
              course_id: str = "", detail: str = "", level: str = "info") -> None:
    """Append one log line. Thread-safe. category: runtime / action / pipeline."""
    try:
        d = _init_dir(data_dir) if data_dir else Path(__file__).resolve().parent.parent / "data" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "category": category,
            "level": level,
            "message": message,
        }
        if course_id:
            entry["course_id"] = course_id
        if detail:
            entry["detail"] = detail[:2000]
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            path = d / f"{category}.jsonl"
            _rotate(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass  # logging must never break the app


def query_logs(data_dir: Path, categories: list[str], keyword: str = "",
               limit: int = 300, level: str = "") -> list[dict[str, Any]]:
    """Query logs across categories, newest first."""
    d = _init_dir(data_dir)
    out: list[dict[str, Any]] = []
    kw = (keyword or "").lower()
    for cat in categories:
        if cat not in (RUNTIME, ACTION, PIPELINE, MATERIAL):
            continue
        path = d / f"{cat}.jsonl"
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):  # newest first
            if len(out) >= limit:
                break
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if level and entry.get("level") != level:
                continue
            if kw and kw not in json.dumps(entry, ensure_ascii=False).lower():
                continue
            entry["category"] = cat
            out.append(entry)
    out.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return out[:limit]
