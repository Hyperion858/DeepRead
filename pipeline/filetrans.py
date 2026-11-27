"""Aliyun DashScope filetrans channel: whole-file async transcription.

Flow: stage the media file in data/filetrans-tmp/ (served publicly via
GET /ft/<name>), submit an async transcription task with that public URL,
poll until done, download the result JSON and map its sentence-level
timestamps (milliseconds) into the segment shape the fusion layer expects.
"""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from pipeline.transcribe import (
    PipelineCancelled, detect_lang, extract_audio, probe_duration,
)

MAX_BYTES = 2 * 1024 * 1024 * 1024      # Aliyun filetrans limit: 2 GB
MAX_DURATION = 12 * 3600                # ... and 12 hours
POLL_INTERVAL = 10                      # seconds between task-status polls
POLL_TIMEOUT = 4 * 3600                 # hard cap on waiting

_TMP = Path(__file__).resolve().parent.parent / "data" / "filetrans-tmp"


def _filetrans_base(provider_base: str) -> str:
    """DashScope native API root from an OpenAI-compatible provider base.

    .../compatible-mode/v1 or .../v1 -> .../api/v1
    """
    b = (provider_base or "").rstrip("/")
    for suffix in ("/compatible-mode/v1", "/v1"):
        if b.endswith(suffix):
            b = b[: -len(suffix)]
            break
    return b + "/api/v1"


def _tmp_dir() -> Path:
    _TMP.mkdir(parents=True, exist_ok=True)
    return _TMP


def _stage_file(media_path: Path, workdir: Path) -> Path:
    """Copy (or convert) the media into the public tmp dir; returns staged path."""
    _tmp_dir()
    suffix = media_path.suffix.lower()
    if suffix in (".mp4", ".wav"):
        src = media_path          # accepted as-is, no transcode
    else:
        # convert to the canonical 16k mono wav (mp3/m4a/mkv/etc.)
        audio_dir = workdir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        src = audio_dir / "filetrans.wav"
        extract_audio(media_path, src)
    staged = _tmp_dir() / f"{uuid.uuid4().hex[:12]}{src.suffix.lower()}"
    with src.open("rb") as fin, staged.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    return staged


def _poll_task(client: httpx.Client, headers: dict, task_url: str,
               progress_cb, should_cancel) -> list[dict[str, Any]]:
    deadline = time.time() + POLL_TIMEOUT
    while True:
        if should_cancel and should_cancel():
            raise PipelineCancelled()
        r = client.get(task_url, headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"查询任务失败 HTTP {r.status_code}: {r.text[:200]}")
        out = (r.json() or {}).get("output") or {}
        status = out.get("task_status")
        if status == "SUCCEEDED":
            results = out.get("results") or []
            if not results:
                raise RuntimeError("任务成功但未返回结果")
            bad = [x for x in results if x.get("subtask_status") not in ("SUCCEEDED", None)]
            if bad:
                raise RuntimeError(f"子任务失败: {str(bad[0])[:200]}")
            return results
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(f"任务{ '被取消' if status == 'CANCELED' else '失败' }: {str(out.get('message') or out)[:200]}")
        if time.time() > deadline:
            raise RuntimeError(f"等待云端转写超时（{POLL_TIMEOUT // 60} 分钟）")
        if progress_cb:
            progress_cb(0.4, f"等待云端转写…（已等待 {int((POLL_TIMEOUT - (deadline - time.time())) // 60)} 分钟）")
        time.sleep(POLL_INTERVAL)


def _parse_result(results: list[dict[str, Any]],
                  client: httpx.Client) -> list[dict[str, Any]]:
    """Download transcription_url JSON and map sentences into segments."""
    segments: list[dict[str, Any]] = []
    for item in results:
        url = item.get("transcription_url") or ""
        if not url:
            continue
        r = client.get(url, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"下载转写结果失败 HTTP {r.status_code}")
        payload = r.json() or {}
        for transcript in payload.get("transcripts") or []:
            if transcript.get("channel_id") not in (0, None):
                continue   # multi-channel: first channel only
            for sent in transcript.get("sentences") or []:
                text = (sent.get("text") or "").strip()
                begin = sent.get("begin_time")
                end = sent.get("end_time")
                if not text or begin is None or end is None:
                    continue
                start_s = begin / 1000.0
                end_s = end / 1000.0
                if end_s <= start_s:
                    end_s = start_s + 0.1
                segments.append({
                    "start": round(start_s, 3),
                    "end": round(end_s, 3),
                    "text": text,
                    "lang": detect_lang(text),
                    "source": "asr",
                })
    segments.sort(key=lambda s: s["start"])
    return segments


def transcribe_filetrans(media_path: Path, cfg: dict[str, Any],
                         progress_cb: Callable[[float, str], None] | None = None,
                         should_cancel: Callable[[], bool] | None = None,
                         workdir: Path | None = None) -> list[dict[str, Any]]:
    """Whole-file transcription through the DashScope async filetrans API."""
    from pipeline.lesson import _resolve_provider, note_model_result

    prov = _resolve_provider(cfg, "asr_provider")
    base_api = _filetrans_base(prov.get("base_url", ""))
    key = prov.get("api_key", "")
    pid = prov.get("id", "")
    model = (cfg.get("filetrans_model") or "qwen-audio-3.1-asr-flash-filetrans").strip()
    public_base = (cfg.get("filetrans_public_base") or "").strip().rstrip("/")
    if not public_base:
        raise RuntimeError("filetrans 公网地址未配置（转写设置）")
    if not base_api:
        raise RuntimeError("filetrans 需要 ASR provider 的 base_url")

    headers = {"Authorization": f"Bearer {key}"}

    # pre-checks (fail fast with clear messages)
    size = media_path.stat().st_size
    if size > MAX_BYTES:
        raise RuntimeError(f"文件超过 filetrans 上限（{size // (1024**2)}MB > 2GB）")
    duration = probe_duration(media_path)
    if duration > MAX_DURATION:
        raise RuntimeError(f"音频超过 filetrans 上限（{duration / 3600:.1f}h > 12h）")

    staged: Path | None = None
    t0 = time.time()
    try:
        if progress_cb:
            progress_cb(0.02, "准备转写文件…")
        staged = _stage_file(media_path, workdir=workdir or media_path.parent)
        public_url = f"{public_base}/ft/{staged.name}"

        with httpx.Client(timeout=httpx.Timeout(connect=15, read=120, write=600, pool=15)) as client:
            if progress_cb:
                progress_cb(0.15, "提交转写任务…")
            r = client.post(
                f"{base_api}/services/audio/asr/transcription",
                headers={**headers, "X-DashScope-Async": "enable"},
                json={"model": model, "input": {"file_urls": [public_url]}},
            )
            if r.status_code != 200:
                note_model_result(pid, model, False, f"HTTP {r.status_code}: {r.text[:160]}")
                raise RuntimeError(f"提交转写任务失败 HTTP {r.status_code}: {r.text[:300]}")
            task_id = ((r.json() or {}).get("output") or {}).get("task_id")
            if not task_id:
                note_model_result(pid, model, False, "响应缺少 task_id")
                raise RuntimeError(f"提交响应缺少 task_id: {r.text[:200]}")

            if progress_cb:
                progress_cb(0.3, "等待云端转写…")
            results = _poll_task(client, headers, f"{base_api}/tasks/{task_id}",
                                 progress_cb, should_cancel)

            if progress_cb:
                progress_cb(0.85, "下载转写结果…")
            segments = _parse_result(results, client)
            if not segments:
                raise RuntimeError("转写结果为空（音频无语音或解析失败）")

        note_model_result(pid, model, True)
        if progress_cb:
            progress_cb(1.0, f"转写完成，共 {len(segments)} 段")
        return segments
    except PipelineCancelled:
        note_model_result(pid, model, False, "已取消")
        raise
    except Exception as exc:  # noqa: BLE001
        if not isinstance(exc, PipelineCancelled):
            note_model_result(pid, model, False, str(exc)[:160])
        raise RuntimeError(f"filetrans 转写失败: {exc}") from exc
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        if progress_cb:
            progress_cb(1.0, f"filetrans 完成（用时 {int(time.time() - t0)}s）")
