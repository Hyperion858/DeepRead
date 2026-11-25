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

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
COURSES_DIR = DATA_DIR / "courses"
APP_DIR = BASE_DIR / "app"
CONFIG_PATH = DATA_DIR / "config.json"

ALLOWED_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

for d in (DATA_DIR, MEDIA_DIR, COURSES_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DeepRead")

# ---------------------------------------------------------------- config

DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [],      # [{id, name, base_url, api_key}]
    "asr_provider": "",   # provider id used for transcription
    "chat_provider": "",  # provider id used for chat
    "asr_model": "",
    "chat_model": "",
    "segment_minutes": 5,
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

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancelled = False


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
    status.update({
        "stage": stage,
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if progress is not None:
        status["progress"] = round(max(0.0, min(1.0, progress)), 3)
    write_json(course_dir(cid) / "status.json", status)


def run_pipeline(cid: str) -> None:
    """Background pipeline: preprocess -> dual-channel text -> align -> lesson."""
    job = JOBS.get(cid)
    cdir = course_dir(cid)
    meta = read_json(cdir / "meta.json", {}) or {}
    media_path = Path(meta.get("media_path", ""))
    try:
        try:
            cfg = load_config()
            # 1. dual-channel text acquisition
            set_stage(cid, "transcribing", "调用转写 API 中…", 0.05)
            transcript = transcribe(
                media_path,
                cfg=cfg,
                workdir=cdir,
                progress_cb=lambda p, msg: set_stage(cid, "transcribing", msg, 0.05 + 0.5 * p),
                should_cancel=lambda: bool(job and job.cancelled),
            )

            set_stage(cid, "subtitles", "提取内嵌字幕轨…", 0.58)
            sub_lines = extract_subtitle_channel(media_path, cdir)

            # 2. align / merge the two channels
            set_stage(cid, "aligning", "字幕与转写对齐融合…", 0.62)
            segments = align(transcript, sub_lines)
            write_json(cdir / "source.json", {"segments": segments})

            # 3. lesson generation
            lesson = generate_lesson(
                segments,
                cfg=cfg,
                progress_cb=lambda p, msg: set_stage(cid, "generating", msg, 0.65 + 0.33 * p),
                should_cancel=lambda: bool(job and job.cancelled),
            )
            write_json(cdir / "lesson.json", lesson)

            meta["duration"] = sum((s.get("end", 0) - s.get("start", 0)) for s in segments) or meta.get("duration")
            write_json(cdir / "meta.json", meta)
            set_stage(cid, "ready", "课程生成完成", 1.0)
        except PipelineCancelled:
            prog = (read_json(cdir / "status.json", {}) or {}).get("progress", 0)
            set_stage(cid, "cancelled", "任务已取消", prog)
        except Exception as exc:  # noqa: BLE001 — surface any pipeline failure to the UI
            set_stage(cid, "error", f"{type(exc).__name__}: {exc}")
    finally:
        JOBS.pop(cid, None)


from pipeline.subtitles import extract_subtitle_channel  # noqa: E402


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
    for k in ("asr_provider", "chat_provider", "asr_model", "chat_model", "segment_minutes"):
        if k in body:
            cfg[k] = body[k]
    save_config(cfg)
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
    return {"models": ids, "asr_models": asr or ids}


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
        })
    return items


@app.post("/api/courses/upload")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型: {ext}")
    cid = new_course_id()
    cdir = course_dir(cid)
    cdir.mkdir(parents=True)
    media_path = MEDIA_DIR / f"{cid}{ext}"
    with media_path.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    kind = "audio" if ext in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"} else "video"
    title = Path(file.filename or cid).stem
    write_json(cdir / "meta.json", {
        "id": cid,
        "title": title,
        "filename": file.filename,
        "kind": kind,
        "media_path": str(media_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(cdir / "status.json", {"stage": "uploaded", "detail": "已上传，等待生成", "progress": 0})
    return {"id": cid, "title": title, "kind": kind}


@app.post("/api/courses/{cid}/generate")
def generate(cid: str):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    status = read_json(cdir / "status.json", {}) or {}
    if status.get("stage") in {"transcribing", "subtitles", "aligning", "generating"}:
        raise HTTPException(409, "课程正在生成中")
    cfg = load_config()
    if not cfg.get("providers"):
        raise HTTPException(400, "请先在设置页配置 API")
    if not get_provider_cfg(cfg, "asr").get("base_url"):
        raise HTTPException(400, "转写 provider 未配置")
    if not get_provider_cfg(cfg, "chat").get("base_url"):
        raise HTTPException(400, "Chat provider 未配置")
    if cid not in JOBS:
        JOBS[cid] = JobState()
        threading.Thread(target=run_pipeline, args=(cid,), daemon=True).start()
    return {"ok": True}


@app.post("/api/courses/{cid}/cancel")
def cancel(cid: str):
    job = JOBS.get(cid)
    if job:
        job.cancelled = True
    return {"ok": True}


@app.get("/api/courses/{cid}")
def get_course(cid: str):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    meta = read_json(cdir / "meta.json", {}) or {}
    status = read_json(cdir / "status.json", {}) or {}
    return {
        "meta": meta,
        "status": status,
        "source": read_json(cdir / "source.json") or {},
        "lesson": read_json(cdir / "lesson.json") or {},
    }


@app.delete("/api/courses/{cid}")
def delete_course(cid: str):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    shutil.rmtree(cdir, ignore_errors=True)
    for f in MEDIA_DIR.glob(f"{cid}.*"):
        f.unlink(missing_ok=True)
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

@app.get("/media/{cid}/file")
def media_file(cid: str, request: Request):
    cdir = course_dir(cid)
    if not cdir.exists():
        raise HTTPException(404, "课程不存在")
    meta = read_json(cdir / "meta.json", {}) or {}
    media_path = Path(meta.get("media_path", ""))
    if not media_path.exists():
        raise HTTPException(404, "媒体文件不存在")
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


# ---------------------------------------------------------------- static frontend

@app.get("/")
def index():
    return FileResponse(
        APP_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=APP_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
