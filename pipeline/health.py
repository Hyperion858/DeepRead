"""Model health marks.

The pipeline records which models recently failed (quota exhausted, offline,
wrong modality …) so the settings page can flag them instead of silently
retrying them every run. Stored outside config.json to keep that file small.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_PATH: Path | None = None
_CACHE: dict[str, dict[str, Any]] = {}


def configure(path: Path) -> None:
    global _PATH, _CACHE
    _PATH = Path(path)
    try:
        _CACHE = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt file just means "no marks"
        _CACHE = {}


def _key(provider_id: str, model: str) -> str:
    return f"{provider_id or ''}||{model or ''}"


def _flush() -> None:
    if not _PATH:
        return
    try:
        _PATH.write_text(json.dumps(_CACHE, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001 — health marks are best-effort
        pass


def note(provider_id: str, model: str, ok: bool, message: str = "") -> None:
    """Record the outcome of one real call against `model`."""
    if not model:
        return
    with _LOCK:
        k = _key(provider_id, model)
        if ok:
            _CACHE.pop(k, None)
        else:
            _CACHE[k] = {
                "provider_id": provider_id or "",
                "model": model,
                "message": (message or "")[:300],
                "at": time.time(),
            }
        _flush()


def snapshot() -> dict[str, dict[str, Any]]:
    with _LOCK:
        return {k: dict(v) for k, v in _CACHE.items()}


def clear(model: str = "", provider_id: str = "") -> int:
    """Drop marks; no args clears everything. Returns how many were removed."""
    with _LOCK:
        keys = [
            k for k, v in _CACHE.items()
            if (not model or v.get("model") == model)
            and (not provider_id or v.get("provider_id") == provider_id)
        ]
        for k in keys:
            _CACHE.pop(k, None)
        if keys:
            _flush()
        return len(keys)
