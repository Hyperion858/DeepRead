"""DeepRead — English intensive-reading study platform.

FastAPI backend: course library, upload, generation pipeline orchestration,
progress persistence, media streaming with Range support.

Config model: multiple API providers (name + base_url + api_key); each role
('asr' = transcription, 'chat' = translations & lesson generation) points at
one provider.
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pipeline.lesson import generate_lesson
from pipeline.transcribe import transcribe, PipelineCancelled
from pipeline.align import align
from pipeline import backup as backup_mod
from pipeline import logs as logs_mod

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def log_event(category: str, message: str, **kw) -> None:
    logs_mod.log_event(category, message, data_dir=DATA_DIR, **kw)

MEDIA_DIR = DATA_DIR / "media"          # legacy: direct uploads (still readable)
MATERIALS_DIR = DATA_DIR / "materials"  # material-centric storage: materials/<mid>/...
COURSES_DIR = DATA_DIR / "courses"
APP_DIR = BASE_DIR / "app"
CONFIG_PATH = DATA_DIR / "config.json"

ALLOWED_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

for d in (DATA_DIR, MEDIA_DIR, MATERIALS_DIR, COURSES_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DeepRead")

# ---------------------------------------------------------------- config

DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [],      # [{id, name, base_url, api_key}]
    "asr_provider": "",   # provider id used for transcription
    "chat_provider": "",  # provider id used for chat
    "asr_model": "",
    "chat_model": "",
    "vision_provider": "",
    "vision_model": "",
    # per-role failover: when a model is dead/over quota, retry with other
    # role-capable models the provider advertises (see pipeline.models_for_role)
    "auto_switch_chat": False,
    "auto_switch_asr": False,
    "auto_switch_vision": False,
    # pipeline tuning
    "ocr_workers": 6,             # concurrent OCR frame recognitions
    "asr_workers": 4,             # concurrent ASR segment transcriptions
    "translate_workers": 3,       # concurrent translation batches
    "segment_minutes": 5,
    # backup settings
    "backup_enabled": True,
    "backup_time": "04:00",   # local time HH:MM
    "backup_keep": 3,         # max archives to retain
    "backup_include_media": True,  # include media files in backup archives
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            stored = {}
        if stored.get("providers"):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
            # migrate the old single auto-switch flag to per-role switches
            if "auto_model_switch" in stored and not any(
                k in stored for k in ("auto_switch_chat", "auto_switch_asr", "auto_switch_vision")
            ):
                legacy = bool(stored["auto_model_switch"])
                cfg["auto_switch_chat"] = cfg["auto_switch_asr"] = cfg["auto_switch_vision"] = legacy
            # the old priority lists carried the effective model when none was set
            for role in ("chat", "asr", "vision"):
                if not cfg.get(f"{role}_model") and stored.get(f"{role}_models"):
                    cfg[f"{role}_model"] = stored[f"{role}_models"][0]
        else:
            # migrate legacy flat configs (single API or two-group) into providers
            legacy_groups = []
            if stored.get("base_url") or stored.get("api_key"):
                legacy_groups.append({
                    "name": "旧配置",
                    "base_url": stored.get("base_url", ""),
                    "api_key": stored.get("api_key", ""),
                })
            for gname, prefix in (("转写(旧)", "asr_"), ("Chat(旧)", "chat_")):
                if stored.get(f"{prefix}base_url") or stored.get(f"{prefix}api_key"):
                    legacy_groups.append({
                        "name": gname,
                        "base_url": stored.get(f"{prefix}base_url", ""),
                        "api_key": stored.get(f"{prefix}api_key", ""),
                    })
            if legacy_groups:
                providers = []
                for g in legacy_groups:
                    providers.append({
                        "id": uuid.uuid4().hex[:8],
                        "name": g["name"],
                        "base_url": g["base_url"],
                        "api_key": g["api_key"],
                    })
                cfg["providers"] = providers
                cfg["asr_provider"] = providers[0]["id"]
                cfg["chat_provider"] = providers[-1]["id"]
                cfg["asr_model"] = stored.get("transcribe_model") or stored.get("asr_model") or ""
                cfg["chat_model"] = stored.get("chat_model") or ""
                # persist the migration so provider ids stay stable across restarts
                save_config(cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_provider_cfg(cfg: dict[str, Any], group: str) -> dict[str, Any]:
    """Resolve {base_url, api_key} for a role ('asr'/'chat')."""
    pid = cfg.get(f"{group}_provider")
    prov = next((p for p in cfg.get("providers", []) if p.get("id") == pid), None)
    if prov is None and cfg.get("providers"):
        prov = cfg["providers"][0]
    if prov is None:
        return {"base_url": "", "api_key": ""}
    return {"base_url": prov.get("base_url", ""), "api_key": prov.get("api_key", "")}


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _masked_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    out["providers"] = [
        {**p, "api_key": mask_key(p.get("api_key", ""))} for p in cfg.get("providers", [])
    ]
    out["has_key"] = any(p.get("api_key") for p in cfg.get("providers", []))
    return out


# ---------------------------------------------------------------- course store

class JobState:
    """In-memory pipeline job state."""

    def __init__(self, stage_lock: str = "") -> None:
        self.lock = threading.Lock()
        self.cancelled = False
        self.stage_lock = stage_lock  # material id this job is generating from


JOBS: dict[str, JobState] = {}


def course_dir(cid: str) -> Path:
    return COURSES_DIR / cid


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def new_course_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


def set_stage(cid: str, stage: str, detail: str = "", progress: float | None = None) -> None:
    status = read_json(course_dir(cid) / "status.json", {}) or {}
    # disk-level kill switch: a cancelled status halts any further stage writes
    # (covers the restart-wipes-JOBS case where in-memory cancel flags vanish)
    if status.get("stage") in ("cancelled",) and stage not in ("cancelled",):
        raise PipelineCancelled()
    status.update({
        "stage": stage,
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if progress is not None:
        status["progress"] = round(max(0.0, min(1.0, progress)), 3)
    write_json(course_dir(cid) / "status.json", status)


def run_pipeline(cid: str, timeout_sec: int = 7200, channels: set[str] | None = None) -> None:
    """Background pipeline with a hard watchdog: if it hasn't finished in
    timeout_sec, mark the course errored so a hung API call can't stall forever.

    channels: subset of {"embedded", "ocr", "asr"} — which text channels to run.
    All requested channels run, then results are fused (best text wins per
    sentence, cross-checked across channels for accuracy). Default: all three.
    """
    import time as _time

    channels = channels or {"embedded", "ocr", "asr"}
    job = JOBS.get(cid)
    cdir = course_dir(cid)
    meta = read_json(cdir / "meta.json", {}) or {}
    media_path = Path(meta.get("media_path", ""))
    started = _time.time()
    _result: dict[str, Any] = {"done": False}

    def _watchdog() -> None:
        while not _result["done"] and _time.time() - started < timeout_sec:
            _time.sleep(20)
        if not _result["done"] and cid in JOBS:
            job = JOBS.get(cid)
            if job:
                job.cancelled = True
            status = read_json(cdir / "status.json", {}) or {}
            if status.get("stage") not in ("ready", "error", "cancelled"):
                set_stage(cid, "error", f"流水线超时（{timeout_sec // 60} 分钟），已中止")

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    try:
        try:
            cfg = load_config()

            # weight per channel: embedded (human-authored) > ocr (frame-sampled)
            # > asr (audio-derived). Fusion prefers higher-weight text.
            channel_weight = {"embedded": 3, "OCR": 2, "asr": 1}

            embedded_lines: list[dict[str, Any]] = []
            ocr_lines: list[dict[str, Any]] = []
            asr_segments: list[dict[str, Any]] = []

            n_steps = len(channels) + 1
            step = 0

            def _bump(msg: str, base: float) -> None:
                set_stage(cid, f"channels", msg, base)

            # 1. embedded subtitle track
            if "embedded" in channels:
                step += 1
                set_stage(cid, "subtitles", "提取内嵌字幕轨…", 0.05)
                embedded_lines = extract_subtitle_channel(media_path, cdir)
                if not embedded_lines:
                    set_stage(cid, "subtitles", "无内嵌字幕轨", 0.08)

            # 2. OCR burned-in subtitles (video only)
            if "ocr" in channels and meta.get("kind") == "video":
                step += 1
                set_stage(cid, "ocr", "OCR 识别画面硬字幕…", 0.08)
                try:
                    ocr_lines = extract_ocr_subtitles(
                        media_path, cdir, cfg,
                        progress_cb=lambda p, msg: set_stage(cid, "ocr", msg, 0.08 + 0.62 * p),
                        should_cancel=lambda: bool(job and job.cancelled),
                        workers=max(1, int(cfg.get("ocr_workers") or 6)),
                    )
                except Exception as ocr_exc:  # noqa: BLE001 — OCR is best-effort
                    set_stage(cid, "ocr", f"OCR 失败: {ocr_exc}", 0.7)
                    ocr_lines = []

            # 3. ASR
            if "asr" in channels:
                step += 1
                set_stage(cid, "transcribing", "调用转写 API 中…", 0.15)
                asr_segments = transcribe(
                    media_path,
                    cfg=cfg,
                    workdir=cdir,
                    progress_cb=lambda p, msg: set_stage(cid, "transcribing", msg, 0.15 + 0.7 * p),
                    should_cancel=lambda: bool(job and job.cancelled),
                    workers=max(1, int(cfg.get("asr_workers") or 4)),
                )

            # 4. fusion
            caption_lines = embedded_lines + ocr_lines
            caption_lines.sort(key=lambda x: x["start"])
            log_event("runtime", f"通道结果：内嵌字幕 {len(embedded_lines)} 条 / OCR {len(ocr_lines)} 条 / ASR {len(asr_segments)} 段",
                      course_id=cid)
            set_stage(cid, "aligning", f"融合 {len(caption_lines)} 条字幕 + {len(asr_segments)} 段转写…", 0.9)
            segments = align(asr_segments, caption_lines)

            write_json(cdir / "source.json", {"segments": segments})

            # 3. lesson generation
            lesson = generate_lesson(
                segments,
                cfg=cfg,
                progress_cb=lambda p, msg: set_stage(cid, "generating", msg, 0.92 + 0.07 * p),
                should_cancel=lambda: bool(job and job.cancelled),
            )
            write_json(cdir / "lesson.json", lesson)

            meta["duration"] = sum((s.get("end", 0) - s.get("start", 0)) for s in segments) or meta.get("duration")
            write_json(cdir / "meta.json", meta)
            set_stage(cid, "ready", "课程生成完成", 1.0)
            log_event("pipeline", f"课程生成完成", course_id=cid,
                      detail=f"通道: {sorted(channels)}, 句子数: {len(segments)}")
        except PipelineCancelled:
            prog = (read_json(cdir / "status.json", {}) or {}).get("progress", 0)
            set_stage(cid, "cancelled", "任务已取消", prog)
            log_event("pipeline", "课程生成被取消", course_id=cid, level="warn")
        except Exception as exc:  # noqa: BLE001 — surface any pipeline failure to the UI
            set_stage(cid, "error", f"{type(exc).__name__}: {exc}")
            log_event("pipeline", f"课程生成失败: {type(exc).__name__}", course_id=cid,
                      detail=str(exc), level="error")
    finally:
        _result["done"] = True
        JOBS.pop(cid, None)


from pipeline.subtitles import extract_subtitle_channel  # noqa: E402
from pipeline.ocr import extract_ocr_subtitles  # noqa: E402
from pipeline import fetcher  # noqa: E402


# ---------------------------------------------------------------- API: config

@app.get("/api/config")
def get_config():
    return _masked_config(load_config())


@app.put("/api/config")
async def put_config(request: Request):
    body = await request.json()
    cfg = load_config()
    if isinstance(body.get("providers"), list):
        stored = {p["id"]: p for p in cfg.get("providers", [])}
        incoming = []
        for p in body["providers"]:
            pid = p.get("id") or uuid.uuid4().hex[:8]
            key = p.get("api_key", "")
            # masked or empty key -> keep the stored one
            if "*" in key or not key:
                key = stored.get(pid, {}).get("api_key", "")
            incoming.append({
                "id": pid,
                "name": p.get("name", "未命名"),
                "base_url": p.get("base_url", ""),
                "api_key": key,
            })
        cfg["providers"] = incoming
        live_ids = {p["id"] for p in incoming}
        if cfg.get("asr_provider") not in live_ids:
            cfg["asr_provider"] = incoming[0]["id"] if incoming else ""
        if cfg.get("chat_provider") not in live_ids:
            cfg["chat_provider"] = incoming[-1]["id"] if incoming else ""
    for k in ("asr_provider", "chat_provider", "vision_provider", "asr_model", "chat_model", "vision_model", "segment_minutes",
              "auto_switch_chat", "auto_switch_asr", "auto_switch_vision",
              "ocr_workers", "asr_workers", "translate_workers",
              "backup_enabled", "backup_time", "backup_keep", "backup_include_media"):
        if k in body:
            cfg[k] = body[k]
    save_config(cfg)
    changed = [k for k in body if k in DEFAULT_CONFIG]
    log_event("action", "修改模型/系统配置", detail="字段: " + ", ".join(changed[:10]))
    return {"ok": True}


@app.get("/api/config/models")
async def list_models(provider_id: str = ""):
    """Fetch the model list from a provider (default: active chat provider)."""
    import httpx

    cfg = load_config()
    pid = provider_id or cfg.get("chat_provider") or (cfg["providers"][0]["id"] if cfg.get("providers") else "")
    prov = next((p for p in cfg.get("providers", []) if p.get("id") == pid), None)
    if prov is None:
        raise HTTPException(400, "provider 不存在")
    base = (prov.get("base_url") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {prov.get('api_key', '')}"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{base}/models", headers=headers)
        if r.status_code != 200:
            raise HTTPException(502, f"获取模型列表失败 HTTP {r.status_code}")
        data = r.json().get("data", [])
        ids = [m.get("id") for m in data if m.get("id")]
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"连接失败: {exc}")
    asr = [m for m in ids if any(k in m.lower() for k in
           ("whisper", "transcri", "asr", "audio", "speech", "sensevoice", "paraformer", "funasr", "vox"))]
    from pipeline.lesson import filter_models_for_role
    return {"models": ids, "asr_models": asr or ids,
            "vision_models": filter_models_for_role(ids, "vision")}


_PROBE_SKIP = ("embed", "tts", "whisper", "transcri", "rerank", "moderation",
               "dall-e", "stable-diffusion", "sdxl", "flux", "image", "video", "audio", "speech")


def _probe_image_uri() -> str:
    """A small solid-colour PNG, as a data URI, for probing image support."""
    import base64
    import struct
    import zlib

    w = h = 32
    raw = b"".join(b"\x00" + bytes((220, 40, 40)) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw))
    png += chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


@app.post("/api/config/probe_vision")
async def probe_vision(request: Request):
    """Ask every model of a provider to look at a tiny image.

    Many providers advertise no modality metadata, so the only reliable way to
    tell multimodal models apart is to send one image. Returns the models that
    accepted it (HTTP 200). Body: {provider_id?}.
    """
    import asyncio
    import httpx

    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = load_config()
    pid = body.get("provider_id") or cfg.get("vision_provider") or cfg.get("chat_provider")
    prov = next((p for p in cfg.get("providers", []) if p.get("id") == pid), None)
    if prov is None:
        raise HTTPException(400, "provider 不存在")
    base = (prov.get("base_url") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {prov.get('api_key', '')}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{base}/models", headers=headers)
        if r.status_code != 200:
            raise HTTPException(502, f"获取模型列表失败 HTTP {r.status_code}")
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"连接失败: {exc}")

    candidates = [m for m in ids if not any(k in m.lower() for k in _PROBE_SKIP)]
    if not candidates:
        return {"vision_models": [], "checked": 0, "total": len(ids)}
    img = _probe_image_uri()
    sem = asyncio.Semaphore(8)

    async with httpx.AsyncClient(timeout=25) as client:
        async def probe(model: str) -> str | None:
            payload = {
                "model": model, "max_tokens": 4, "temperature": 0,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "这张图片是什么颜色？只回答一个词。"},
                    {"type": "image_url", "image_url": {"url": img}},
                ]}],
            }
            async with sem:
                try:
                    resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
                    return model if resp.status_code == 200 else None
                except Exception:  # noqa: BLE001 — one bad model must not abort the scan
                    return None

        results = await asyncio.gather(*(probe(m) for m in candidates))

    found = [m for m in results if m]
    return {"vision_models": found, "checked": len(candidates), "total": len(ids)}


@app.post("/api/config/test")
async def test_config(request: Request):
    """Probe providers: real chat + transcription endpoint checks.

    Body (all optional): {provider_id?, asr_provider?, chat_provider?, asr_model?, chat_model?}
    """
    import httpx

    cfg = load_config()
    try:
        body = await request.json()
    except Exception:
        body = {}
    results = {"transcribe": False, "chat": False, "detail": ""}

    def resolve(group: str):
        pid = body.get(f"{group}_provider") or body.get("provider_id") or cfg.get(f"{group}_provider")
        prov = next((p for p in cfg.get("providers", []) if p.get("id") == pid), None)
        if prov is None and cfg.get("providers"):
            prov = cfg["providers"][0]
        return prov

    async with httpx.AsyncClient(timeout=30) as client:
        chat_prov = resolve("chat")
        if chat_prov:
            base = (chat_prov.get("base_url") or "").rstrip("/")
            headers = {"Authorization": f"Bearer {chat_prov.get('api_key', '')}"}
            chat_model = body.get("chat_model") or cfg.get("chat_model") or "gpt-4o-mini"
            try:
                r = await client.post(
                    f"{base}/chat/completions", headers=headers,
                    json={"model": chat_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                )
                results["chat"] = r.status_code == 200
                if r.status_code != 200:
                    results["detail"] = f"chat {r.status_code}: {r.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                results["detail"] = f"chat: {exc}"

        asr_prov = resolve("asr")
        if asr_prov:
            base = (asr_prov.get("base_url") or "").rstrip("/")
            headers = {"Authorization": f"Bearer {asr_prov.get('api_key', '')}"}
            try:
                r = await client.get(f"{base}/models", headers=headers)
                results["transcribe"] = r.status_code == 200
                if r.status_code != 200 and not results["detail"]:
                    results["detail"] = f"asr models {r.status_code}: {r.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                if not results["detail"]:
                    results["detail"] = f"asr: {exc}"

    if not (results["transcribe"] or results["chat"]):
        raise HTTPException(502, results["detail"] or "连接失败")
    return results


@app.post("/api/config/test_provider")
async def test_provider(request: Request):
    """Probe a single provider with the values currently being edited (unsaved ok).

    Body: {provider_id?, base_url, api_key?}. Checks reachability + auth by
    listing the provider's models; returns 200 with ok=false on failure.
    """
    import httpx

    try:
        body = await request.json()
    except Exception:
        body = {}
    base = (body.get("base_url") or "").strip().rstrip("/")
    key = (body.get("api_key") or "").strip()
    pid = body.get("provider_id") or ""
    if not base:
        return {"ok": False, "detail": "请填写 Base URL"}
    # masked / empty key -> fall back to the stored key for this provider
    if "*" in key or not key:
        stored = next((p for p in load_config().get("providers", []) if p.get("id") == pid), None)
        if stored:
            key = stored.get("api_key", "")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"连接失败: {exc}"}
    if r.status_code != 200:
        return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    try:
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception:  # noqa: BLE001
        return {"ok": False, "detail": "返回内容不是 OpenAI 兼容的模型列表"}
    return {"ok": True, "count": len(ids), "models": ids[:20]}


# ---------------------------------------------------------------- API: backup

BACKUPS_DIR = DATA_DIR / "backups"


def run_auto_backup() -> None:
    try:
        backup_mod.create_backup(DATA_DIR, BACKUPS_DIR, note="auto",
                                 include_media=bool(load_config().get("backup_include_media", True)))
        run_retention()
    except Exception:  # noqa: BLE001 — backups must never crash the app
        pass


def run_retention() -> None:
    keep = max(1, int(load_config().get("backup_keep") or 3))
    archives = sorted(BACKUPS_DIR.glob("deepread-backup-*.zip"), reverse=True)
    for old in archives[keep:]:
        old.unlink(missing_ok=True)


def _backup_scheduler() -> None:
    """Daily auto-backup thread. Sleeps in short increments; checks the wall clock."""
    last_run_date = None
    while True:
        try:
            cfg = load_config()
            if cfg.get("backup_enabled"):
                hh, mm = (cfg.get("backup_time") or "04:00").split(":")[:2]
                now = datetime.now()
                target_h, target_m = int(hh) % 24, int(mm) % 60
                if now.hour == target_h and now.minute == target_m:
                    today = now.strftime("%Y-%m-%d")
                    if last_run_date != today:
                        last_run_date = today
                        run_auto_backup()
        except Exception:  # noqa: BLE001
            pass
        threading.Event().wait(30)


@app.get("/api/backups")
def list_backups():
    return backup_mod.list_backups(BACKUPS_DIR)


@app.post("/api/backups/create")
def create_backup_now():
    path = backup_mod.create_backup(DATA_DIR, BACKUPS_DIR, note="manual",
                                    include_media=bool(load_config().get("backup_include_media", True)))
    run_retention()
    log_event("action", "手动创建备份", detail=path.name)
    return {"ok": True, "file": path.name}


@app.get("/api/backups/{name}/download")
def download_backup(name: str):
    if "/" in name or "\\" in name or not name.startswith("deepread-backup-"):
        raise HTTPException(400, "无效的备份名")
    path = BACKUPS_DIR / name
    if not path.exists():
        raise HTTPException(404, "备份不存在")
    return FileResponse(path, media_type="application/zip", filename=name)


@app.delete("/api/backups/{name}")
def delete_backup(name: str):
    if "/" in name or "\\" in name or not name.startswith("deepread-backup-"):
        raise HTTPException(400, "无效的备份名")
    path = BACKUPS_DIR / name
    if path.exists():
        path.unlink()
    return {"ok": True}


@app.post("/api/backups/restore")
async def restore_backup(request: Request):
    """Restore from an uploaded backup zip."""
    import tempfile

    form = await request.form()
    up = form.get("file")
    if up is None:
        raise HTTPException(400, "缺少备份文件")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(up.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        result = backup_mod.restore_backup(DATA_DIR, tmp_path)
        log_event("action", "从备份恢复数据",
                  detail=f"{result.get('courses', 0)} 门课程, {result.get('materials', 0)} 个素材")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)
    return result


@app.post("/api/backups/media")
async def restore_media(request: Request):
    """Attach a re-uploaded media file to a restored course.

    multipart: file=<media>, course_id=<id>
    """
    import tempfile

    form = await request.form()
    up = form.get("file")
    cid = form.get("course_id", "")
    if up is None or not cid:
        raise HTTPException(400, "缺少文件或课程 ID")
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td) / Path(up.filename or "media").name
        with tmp_path.open("wb") as fh:
            shutil.copyfileobj(up.file, fh)
        result = backup_mod.attach_media(cid, DATA_DIR, tmp_path)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "媒体关联失败"))
    return {"ok": True}


# ---------------------------------------------------------------- API: materials

def new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


def material_dir(mid: str) -> Path:
    return MATERIALS_DIR / mid


def find_media_file(mat: dict[str, Any]) -> Path | None:
    """Resolve a material's media file, checking legacy media dir too."""
    p = mat.get("media_path", "")
    if p and Path(p).exists():
        return Path(p)
    mid = mat.get("id", "")
    name = mat.get("filename", "")
    # legacy layout: data/media/<mid>.<ext>
    if name:
        legacy = MEDIA_DIR / name
        if legacy.exists():
            return legacy
    for cand in MEDIA_DIR.glob(f"{mid}.*"):
        return cand
    return None


def material_payload(mat: dict[str, Any]) -> dict[str, Any]:
    media = find_media_file(mat)
    return {
        "id": mat.get("id"),
        "title": mat.get("title", ""),
        "filename": mat.get("filename", ""),
        "source_url": mat.get("source_url", ""),
        "kind": mat.get("kind", "video"),
        "duration": mat.get("duration", 0),
        "size_mb": round(media.stat().st_size / 1024 / 1024, 1) if media else 0,
        "media_exists": bool(media),
        "course_id": mat.get("course_id", ""),
        "created_at": mat.get("created_at", ""),
        "note": mat.get("note", ""),
    }


@app.get("/api/materials")
def list_materials():
    items = []
    for mdir in sorted(MATERIALS_DIR.iterdir(), reverse=True):
        if not mdir.is_dir():
            continue
        mat = read_json(mdir / "material.json") or {}
        if not mat:
            continue
        items.append(material_payload(mat))
    return items


@app.post("/api/materials/upload")
async def material_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型: {ext}")
    mid = new_id()
    mdir = material_dir(mid)
    mdir.mkdir(parents=True)
    dest = mdir / f"media{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    kind = "audio" if ext in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"} else "video"
    title = Path(file.filename or mid).stem
    write_json(mdir / "material.json", {
        "id": mid,
        "title": title,
        "filename": file.filename,
        "media_path": str(dest),
        "kind": kind,
        "source_url": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    log_event("material", f"上传素材「{title}」", detail=f"{file.filename} ({kind})")
    return {"id": mid, "title": title, "kind": kind}


@app.post("/api/materials/import")
async def material_import(request: Request):
    """Import a material from a share URL (douyin etc.): download only."""
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少链接")
    mid = new_id()
    mdir = material_dir(mid)
    mdir.mkdir(parents=True)
    write_json(mdir / "material.json", {
        "id": mid,
        "title": "正在获取视频信息…",
        "filename": "",
        "media_path": "",
        "kind": "video",
        "source_url": url,
        "status": "downloading",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    threading.Thread(target=_download_material, args=(mid, url), daemon=True).start()
    log_event("material", f"链接导入素材", detail=url)
    return {"id": mid, "title": url}


MATERIAL_JOBS: dict[str, JobState] = {}


def _download_material(mid: str, url: str) -> None:
    mdir = material_dir(mid)
    job = MATERIAL_JOBS.setdefault(mid, JobState())
    try:
        mat = read_json(mdir / "material.json", {}) or {}
        mat["status"] = "downloading"
        write_json(mdir / "material.json", mat)
        result = fetcher.download(url, mdir, "media",
                                  progress_cb=lambda p, msg: None,
                                  should_cancel=lambda: bool(job and job.cancelled))
        media_path = result["path"]
        mat.update({
            "title": result["title"] or url,
            "filename": media_path.name,
            "media_path": str(media_path),
            "kind": result["kind"],
            "duration": result.get("duration") or 0,
            "status": "ready",
        })
        write_json(mdir / "material.json", mat)
        log_event("material", f"素材下载完成「{mat.get('title', '')}」",
                  detail=f"{media_path.name} {result.get('duration', 0):.0f}s")
    except Exception as exc:  # noqa: BLE001
        mat = read_json(mdir / "material.json", {}) or {}
        mat["status"] = "error"
        mat["error"] = str(exc)
        write_json(mdir / "material.json", mat)
        log_event("material", "素材下载失败", detail=f"{url} · {exc}", level="error")
    finally:
        MATERIAL_JOBS.pop(mid, None)


@app.post("/api/materials/{mid}/rename")
async def material_rename(mid: str, request: Request):
    mdir = material_dir(mid)
    if not mdir.exists():
        raise HTTPException(404, "素材不存在")
    body = await request.json()
    mat = read_json(mdir / "material.json", {}) or {}
    old_title = mat.get("title", "")
    mat["title"] = (body.get("title") or "").strip() or old_title
    write_json(mdir / "material.json", mat)
    # keep the linked course title in sync
    cid = mat.get("course_id", "")
    if cid and (COURSES_DIR / cid).exists():
        cmeta = read_json(COURSES_DIR / cid / "meta.json", {}) or {}
        cmeta["title"] = mat["title"]
        write_json(COURSES_DIR / cid / "meta.json", cmeta)
    log_event("material", f"重命名素材：「{old_title}」→「{mat['title']}」")
    return {"ok": True}


@app.delete("/api/materials/{mid}")
def material_delete(mid: str):
    mdir = material_dir(mid)
    mat = read_json(mdir / "material.json", {}) or {}
    course_id = mat.get("course_id", "")
    if course_id and (COURSES_DIR / course_id).exists():
        raise HTTPException(409, f"该素材已生成课程「{course_id}」，请先删除课程")
    shutil.rmtree(mdir, ignore_errors=True)
    # also clean legacy media if it lives there
    for f in MEDIA_DIR.glob(f"{mid}.*"):
        f.unlink(missing_ok=True)
    log_event("material", f"删除素材「{mat.get('title', mid)}」")
    return {"ok": True}


@app.post("/api/materials/{mid}/generate")
def material_generate(mid: str, request: Request = None):
    """Create a course from a material and start the pipeline.

    Body (optional JSON): {"channels": ["embedded","ocr","asr"]} — at least one.
    Defaults to all three; the choice is remembered on the material.
    """
    body = {}
    if request is not None:
        try:
            import asyncio
            body = asyncio.get_event_loop().run_until_complete(request.json())
        except Exception:
            body = {}
    channels_in = body.get("channels")
    valid = {"embedded", "ocr", "asr"}
    if isinstance(channels_in, list) and channels_in:
        channels = {c for c in channels_in if c in valid}
        if not channels:
            raise HTTPException(400, "channels 无效，至少选择一个：embedded / ocr / asr")
    else:
        channels = valid
    if "ocr" in channels or "embedded" in channels:
        kind = (read_json(material_dir(mid) / "material.json", {}) or {}).get("kind", "video")
        if kind != "video" and channels != {"asr"}:
            channels = {"asr"}  # audio materials have no picture to OCR

    mdir = material_dir(mid)
    if not mdir.exists():
        raise HTTPException(404, "素材不存在")
    mat = read_json(mdir / "material.json", {}) or {}
    if mat.get("status") == "downloading":
        raise HTTPException(409, "素材还在下载中")
    media = find_media_file(mat)
    if not media:
        raise HTTPException(400, "素材媒体文件缺失")
    # lock: one generating course per material — course exists OR a stale
    # course_id points at a course currently in a working stage
    if mat.get("course_id"):
        exists = (COURSES_DIR / mat["course_id"]).exists()
        if exists:
            cstatus = read_json(COURSES_DIR / mat["course_id"] / "status.json", {}) or {}
            if cstatus.get("stage") in ("uploaded", "downloading", "transcribing", "subtitles", "ocr", "channels", "aligning", "generating"):
                raise HTTPException(409, "该素材已有课程正在生成中，请等待完成或先删除课程")
            # a finished course exists — relinking a finished course to the
            # material happens below; a NEW generate over it is not allowed
            # unless the user deleted that course first (kept as guard)
        else:
            mat["course_id"] = ""  # stale link to a deleted course — clean it
    if any(getattr(j, "stage_lock", "") == mid for j in JOBS.values()):
        raise HTTPException(409, "该素材已有课程正在生成中")
    cfg = load_config()
    if not cfg.get("providers"):
        raise HTTPException(400, "请先在模型配置页配置 API")
    if "asr" in channels and not get_provider_cfg(cfg, "asr").get("base_url"):
        raise HTTPException(400, "转写 provider 未配置")
    if not get_provider_cfg(cfg, "chat").get("base_url"):
        raise HTTPException(400, "Chat provider 未配置")

    cid = new_course_id()
    cdir = course_dir(cid)
    cdir.mkdir(parents=True)
    write_json(cdir / "meta.json", {
        "id": cid,
        "material_id": mid,
        "title": mat.get("title") or cid,
        "filename": media.name,
        "kind": mat.get("kind", "video"),
        "media_path": str(media),   # course points INTO the material storage
        "source_url": mat.get("source_url", ""),
        "channels": sorted(channels),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(cdir / "status.json", {"stage": "uploaded", "detail": "已创建，等待生成", "progress": 0})
    mat["course_id"] = cid
    mat["last_channels"] = sorted(channels)   # remember for next time
    write_json(mdir / "material.json", mat)
    JOBS[cid] = JobState(stage_lock=mid)
    threading.Thread(target=run_pipeline, args=(cid,), kwargs={"channels": channels}, daemon=True).start()
    log_event("action", f"生成课程（通道: {', '.join(sorted(channels))}）", course_id=cid)
    return {"id": cid, "title": mat.get("title")}


@app.get("/media/{mid}/material")
def media_material_file(mid: str, request: Request):
    """Stream a material's media file (Range supported)."""
    mdir = material_dir(mid)
    mat = read_json(mdir / "material.json", {}) or {}
    media = find_media_file(mat)
    if not media:
        raise HTTPException(404, "媒体文件不存在")
    return _stream_media(media, request)


# ---------------------------------------------------------------- API: logs

@app.get("/api/logs")
def query_logs(category: str = "all", keyword: str = "", level: str = "", limit: int = 300):
    """Query logs. category: all | runtime | pipeline | action | material."""
    cats = {"all": ["runtime", "pipeline", "action", "material"],
            "runtime": ["runtime"],
            "pipeline": ["pipeline"],
            "action": ["action"],
            "material": ["material"]}.get(category,
            ["runtime", "pipeline", "action", "material"])
    return logs_mod.query_logs(DATA_DIR, cats, keyword=keyword, limit=min(limit, 1000), level=level)


@app.get("/api/logs/{category}/download")
def download_logs(category: str):
    if category not in ("runtime", "pipeline", "action", "material"):
        raise HTTPException(400, "无效的日志类别")
    path = DATA_DIR / "logs" / f"{category}.jsonl"
    if not path.exists():
        raise HTTPException(404, "日志文件不存在")
    return FileResponse(path, media_type="application/octet-stream", filename=f"deepread-{category}.jsonl")


# ---------------------------------------------------------------- API: courses

@app.get("/api/courses")
def list_courses():
    items = []
    for cdir in sorted(COURSES_DIR.iterdir(), reverse=True):
        if not cdir.is_dir():
            continue
        meta = read_json(cdir / "meta.json", {}) or {}
        status = read_json(cdir / "status.json", {}) or {}
        progress = read_json(cdir / "progress.json", {}) or {}
        items.append({
            "id": cdir.name,
            "title": meta.get("title") or cdir.name,
            "filename": meta.get("filename"),
            "kind": meta.get("kind"),
            "duration": meta.get("duration") or 0,
            "created_at": meta.get("created_at"),
            "stage": status.get("stage", "unknown"),
            "detail": status.get("detail", ""),
            "progress": status.get("progress", 0),
            "sentence_count": len((read_json(cdir / "source.json", {}) or {}).get("segments", []) or []),
            "learned_percent": progress.get("learned_percent", 0),
            "material_id": meta.get("material_id", ""),
        })
    return items


@app.post("/api/courses/upload")
async def upload(file: UploadFile = File(...)):
    """Legacy direct-to-course upload (kept for compatibility).

    Stores the file as a material, then creates a course from it.
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型: {ext}")
    mid = new_id()
    mdir = material_dir(mid)
    mdir.mkdir(parents=True)
    dest = mdir / f"media{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    kind = "audio" if ext in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"} else "video"
    title = Path(file.filename or mid).stem
    mat = {
        "id": mid,
        "title": title,
        "filename": file.filename,
        "media_path": str(dest),
        "kind": kind,
        "source_url": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(mdir / "material.json", mat)
    # create the course immediately (same as material_generate, without the API guards)
    cid = new_course_id()
    cdir = course_dir(cid)
    cdir.mkdir(parents=True)
    write_json(cdir / "meta.json", {
        "id": cid,
        "material_id": mid,
        "title": title,
        "filename": dest.name,
        "kind": kind,
        "media_path": str(dest),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(cdir / "status.json", {"stage": "uploaded", "detail": "已上传，等待生成", "progress": 0})
    mat["course_id"] = cid
    write_json(mdir / "material.json", mat)
    return {"id": cid, "title": title, "kind": kind}


@app.post("/api/courses/{cid}/generate")
def generate(cid: str, request: Request = None):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    status = read_json(cdir / "status.json", {}) or {}
    if status.get("stage") in {"transcribing", "subtitles", "ocr", "aligning", "generating", "channels"}:
        raise HTTPException(409, "课程正在生成中")
    cfg = load_config()
    if not cfg.get("providers"):
        raise HTTPException(400, "请先在设置页配置 API")
    if not get_provider_cfg(cfg, "asr").get("base_url"):
        raise HTTPException(400, "转写 provider 未配置")
    if not get_provider_cfg(cfg, "chat").get("base_url"):
        raise HTTPException(400, "Chat provider 未配置")
    # channel choice: meta.channels from course creation, or material's last choice
    meta = read_json(cdir / "meta.json", {}) or {}
    channels = set(meta.get("channels") or [])
    if not channels and meta.get("material_id"):
        mat = read_json(material_dir(meta["material_id"]) / "material.json", {}) or {}
        channels = set(mat.get("last_channels") or [])
    if not channels:
        channels = {"embedded", "ocr", "asr"}
    if cid not in JOBS:
        JOBS[cid] = JobState()
        threading.Thread(target=run_pipeline, args=(cid,), kwargs={"channels": channels}, daemon=True).start()
        log_event("action", f"重新生成课程（通道: {', '.join(sorted(channels))}）", course_id=cid)
    return {"ok": True}


@app.post("/api/courses/{cid}/cancel")
def cancel(cid: str):
    job = JOBS.get(cid)
    if job:
        job.cancelled = True
        log_event("action", "取消课程生成", course_id=cid)
    else:
        # no in-memory job (e.g. after a container restart): persist a cancelled
        # marker so the running pipeline halts at its next stage write
        status = read_json(course_dir(cid) / "status.json", {}) or {}
        if status.get("stage") in ("transcribing", "subtitles", "ocr", "channels", "aligning", "generating"):
            set_stage(cid, "cancelled", "任务已取消")
            log_event("action", "取消课程生成（磁盘标记）", course_id=cid)
    return {"ok": True}


@app.get("/api/courses/{cid}")
def get_course(cid: str):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    meta = read_json(cdir / "meta.json", {}) or {}
    status = read_json(cdir / "status.json", {}) or {}
    cal = read_json(cdir / "calibration.json", {}) or {}
    return {
        "meta": meta,
        "status": status,
        "source": read_json(cdir / "source.json") or {},
        "lesson": read_json(cdir / "lesson.json") or {},
        "calibration": {
            "global_offset": cal.get("global_offset", 0.0),
            "overrides": cal.get("overrides", {}),
            "text_edits": cal.get("text_edits", {}),
        },
    }


@app.delete("/api/courses/{cid}")
def delete_course(cid: str):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    meta = read_json(cdir / "meta.json", {}) or {}
    shutil.rmtree(cdir, ignore_errors=True)
    # unlink the material's course reference — ONLY if the material still
    # points at THIS course (deleting an old duplicate must not clear the
    # link to the material's current course)
    mid = meta.get("material_id", "")
    if mid:
        mat_path = material_dir(mid) / "material.json"
        mat = read_json(mat_path)
        if mat is not None and mat.get("course_id") == cid:
            mat["course_id"] = ""
            write_json(mat_path, mat)
    # legacy courses (no material): the media file in MEDIA_DIR belongs to no one now — remove it
    if not mid:
        for f in MEDIA_DIR.glob(f"{cid}.*"):
            f.unlink(missing_ok=True)
    log_event("action", f"删除课程「{meta.get('title', cid)}」", course_id=cid)
    return {"ok": True}


# ---------------------------------------------------------------- API: calibration

@app.get("/api/courses/{cid}/calibration")
def get_calibration(cid: str):
    if not course_dir(cid).exists():
        raise HTTPException(404, "课程不存在")
    cal = read_json(course_dir(cid) / "calibration.json", {}) or {}
    return {
        "global_offset": cal.get("global_offset", 0.0),
        "overrides": cal.get("overrides", {}),      # sentenceId(str) -> {start, end}
        "text_edits": cal.get("text_edits", {}),    # sentenceId(str) -> corrected text
    }


@app.post("/api/courses/{cid}/calibration")
async def post_calibration(cid: str, request: Request):
    if not course_dir(cid).exists():
        raise HTTPException(404, "课程不存在")
    body = await request.json()
    cal = {
        "global_offset": float(body.get("global_offset", 0.0)),
        "overrides": body.get("overrides", {}),
        "text_edits": body.get("text_edits", {}),   # manual sentence text corrections
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(course_dir(cid) / "calibration.json", cal)
    return {"ok": True}


# ---------------------------------------------------------------- API: progress

@app.get("/api/courses/{cid}/progress")
def get_progress(cid: str):
    return read_json(course_dir(cid) / "progress.json", {}) or {}


@app.post("/api/courses/{cid}/progress")
async def post_progress(cid: str, request: Request):
    if not course_dir(cid).exists():
        raise HTTPException(404, "课程不存在")
    body = await request.json()
    write_json(course_dir(cid) / "progress.json", body)
    return {"ok": True}


# ---------------------------------------------------------------- media streaming

def _stream_media(media_path: Path, request: Request):
    size = media_path.stat().st_size
    content_type = "video/mp4" if media_path.suffix == ".mp4" else "application/octet-stream"
    range_header = request.headers.get("range")
    if range_header:
        try:
            unit, rng = range_header.split("=", 1)
            start_s, end_s = rng.split("-", 1)
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
        except ValueError:
            raise HTTPException(416, "Invalid Range")
        if start >= size:
            raise HTTPException(416, "Range out of bounds")
        length = end - start + 1

        def iter_file():
            with media_path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1024 * 512, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )
    return FileResponse(media_path, media_type=content_type, headers={"Accept-Ranges": "bytes"})


@app.get("/media/{cid}/file")
def media_file(cid: str, request: Request):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    meta = read_json(cdir / "meta.json", {}) or {}
    media_path = Path(meta.get("media_path", ""))
    if not media_path.exists():
        # fall back to the material storage when the course references a material
        mid = meta.get("material_id", "")
        if mid:
            mat = read_json(material_dir(mid) / "material.json", {}) or {}
            media_path = Path(mat.get("media_path", ""))
        if not media_path.exists():
            raise HTTPException(404, "媒体文件不存在")
    return _stream_media(media_path, request)


# ---------------------------------------------------------------- static frontend

@app.get("/")
def index():
    return FileResponse(
        APP_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=APP_DIR), name="static")

# start the daily auto-backup scheduler
threading.Thread(target=_backup_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
