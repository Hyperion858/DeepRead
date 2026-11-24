"""DeepRead — English intensive-reading study platform.

FastAPI backend: course library, upload, generation pipeline orchestration,
progress persistence, media streaming with Range support.
"""
from __future__ import annotations

import asyncio
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
from pipeline.transcribe import transcribe
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
    "base_url": "",
    "api_key": "",
    "transcribe_model": "whisper-1",
    "chat_model": "gpt-4o-mini",
    "segment_minutes": 5,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# ---------------------------------------------------------------- course store

class JobState:
    """In-memory pipeline job state; disk status.json is the source of truth for recovery."""

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
            # 1. dual-channel text acquisition
            set_stage(cid, "transcribing", "调用转写 API 中…", 0.05)
            transcript = transcribe(
                media_path,
                cfg=load_config(),
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
                cfg=load_config(),
                progress_cb=lambda p, msg: set_stage(cid, "generating", msg, 0.65 + 0.33 * p),
                should_cancel=lambda: bool(job and job.cancelled),
            )
            write_json(cdir / "lesson.json", lesson)

            meta["duration"] = sum((s.get("end", 0) - s.get("start", 0)) for s in segments) or meta.get("duration")
            write_json(cdir / "meta.json", meta)
            set_stage(cid, "ready", "课程生成完成", 1.0)
        except PipelineCancelled:
            set_stage(cid, "cancelled", "任务已取消", read_json(cdir / "status.json", {}).get("progress", 0))
        except Exception as exc:  # noqa: BLE001 — surface any pipeline failure to the UI
            set_stage(cid, "error", f"{type(exc).__name__}: {exc}")
    finally:
        JOBS.pop(cid, None)


class PipelineCancelled(Exception):
    pass


# subtitles import placed here to avoid circulars is unnecessary; direct import is fine
from pipeline.subtitles import extract_subtitle_channel  # noqa: E402


# ---------------------------------------------------------------- API: config

@app.get("/api/config")
def get_config():
    cfg = load_config()
    cfg["api_key"] = mask_key(cfg.get("api_key", ""))
    cfg["has_key"] = bool(load_config().get("api_key"))
    return cfg


@app.put("/api/config")
async def put_config(request: Request):
    body = await request.json()
    cfg = load_config()
    for k in DEFAULT_CONFIG:
        if k in body:
            val = body[k]
            # don't overwrite a stored key with the masked placeholder
            if k == "api_key" and isinstance(val, str) and ("*" in val or val == ""):
                continue
            cfg[k] = val
    save_config(cfg)
    return {"ok": True}


@app.post("/api/config/test")
async def test_config():
    """Test both the transcription endpoint and the chat endpoint."""
    import httpx

    cfg = load_config()
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise HTTPException(400, "base_url 未配置")
    results = {"transcribe": False, "chat": False, "detail": ""}
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}"}
    async with httpx.AsyncClient(timeout=30) as client:
        # chat endpoint probe
        try:
            r = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={"model": cfg["chat_model"], "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            )
            results["chat"] = r.status_code == 200
            if r.status_code != 200:
                results["detail"] = f"chat {r.status_code}: {r.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            results["detail"] = f"chat: {exc}"
        # transcriptions endpoint probe (list models as a cheap proxy)
        try:
            r = await client.get(f"{base}/models", headers=headers)
            results["transcribe"] = r.status_code == 200
            if r.status_code != 200 and not results["detail"]:
                results["detail"] = f"models {r.status_code}: {r.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            if not results["detail"]:
                results["detail"] = f"models: {exc}"
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
        lesson = read_json(cdir / "lesson.json") or {}
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
    if not load_config().get("base_url"):
        raise HTTPException(400, "请先在设置页配置 API")
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
        # bytes=start-end
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
    return FileResponse(APP_DIR / "index.html")


app.mount("/static", StaticFiles(directory=APP_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
