"""Hard-subtitle (burned-in) OCR extraction.

Douyin videos carry subtitles burned into the pixels — no subtitle stream.
Pipeline: sample bottom-region frames at a fixed interval via ffmpeg,
send crops to a vision LLM (OpenAI-compatible /chat/completions with
image_url data URIs), then merge identical consecutive captions into
timed cue lines. These cues feed the same align channel as embedded
subtitles (source="ocr").
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import httpx

from pipeline.transcribe import detect_lang

SAMPLE_INTERVAL = 2.0   # seconds between sampled frames
OCR_PREFIX = "OCR"


def _vision_provider(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve vision model for OCR.

    Roles: vision_provider (dedicated) -> chat_provider -> first provider.
    Model: vision_model -> chat_model.
    """
    providers = cfg.get("providers") or []
    pid = cfg.get("vision_provider") or cfg.get("chat_provider")
    prov = next((p for p in providers if p.get("id") == pid), None)
    if prov is None and providers:
        prov = providers[0]
    model = cfg.get("vision_model") or cfg.get("chat_model") or ""
    return {"base_url": (prov or {}).get("base_url", "").rstrip("/"),
            "api_key": (prov or {}).get("api_key", ""),
            "model": model}


def extract_frames(media_path: Path, workdir: Path,
                   interval: float = SAMPLE_INTERVAL,
                   progress_cb: Callable[[float, str], None] | None = None) -> list[Path]:
    """Extract bottom-crop frames as jpgs at a fixed interval."""
    frames_dir = workdir / "ocr_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # crop bottom 22% of the frame, full width — where douyin burns subtitles
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(media_path), "-vf",
         f"fps=1/{interval},crop=iw:ih*0.22:0:ih*0.78,scale=960:-2",
         "-q:v", "4", str(frames_dir / "f%06d.jpg")],
        capture_output=True, timeout=1800,
    )
    if r.returncode != 0:
        raise RuntimeError(f"抽帧失败: {r.stderr.decode(errors='replace')[-300:]}")
    frames = sorted(frames_dir.glob("f*.jpg"))
    if progress_cb:
        progress_cb(0.3, f"抽取 {len(frames)} 帧")
    return frames


def ocr_frame(client: httpx.Client, prov: dict[str, Any], img_path: Path) -> str:
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    r = client.post(
        f"{prov['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {prov['api_key']}"},
        json={
            "model": prov["model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "识别图中字幕文字。只输出字幕原文，没有字幕则输出空字符串，不要解释、不要标点修正。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            "max_tokens": 200,
            "temperature": 0,
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OCR API {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"].strip()


def _norm(s: str) -> str:
    return re.sub(r"[\s。．.,，、!！?？;；:：'\"「」『』()（）\[\]]+", "", s)


def merge_cues(frame_texts: list[tuple[float, str]], interval: float,
               min_chars: int = 1) -> list[dict[str, Any]]:
    """Merge consecutive identical/overlapping captions into timed cues.

    frame_texts: [(t_seconds, text)] ordered by time. A caption that starts at
    frame k truly starts somewhere in (t_{k-1}, t_k]; we approximate the start
    at t_k - interval/2 and end at t_next_change + interval/2.
    """
    cues: list[dict[str, Any]] = []
    cur_text, cur_start, last_seen = None, None, None
    for t, text in frame_texts:
        text = text.strip()
        norm = _norm(text)
        if norm and cur_text is not None and (_norm(cur_text) == norm or norm in _norm(cur_text) or _norm(cur_text) in norm):
            # same or extended caption continues
            if len(norm) > len(_norm(cur_text)):
                cur_text = text  # OCR caught a fuller line (fade-in etc.)
            last_seen = t
            continue
        if cur_text is not None and _norm(cur_text):
            cues.append({
                "start": max(0.0, cur_start - interval / 2),
                "end": last_seen + interval,
                "text": cur_text,
                "lang": detect_lang(cur_text),
            })
        cur_text, cur_start, last_seen = (text if norm else None), t, t
    if cur_text is not None and _norm(cur_text):
        cues.append({
            "start": max(0.0, cur_start - interval / 2),
            "end": last_seen + interval,
            "text": cur_text,
            "lang": detect_lang(cur_text),
        })
    return [c for c in cues if len(_norm(c["text"])) >= min_chars]


def extract_ocr_subtitles(
    media_path: Path,
    course_dir: Path,
    cfg: dict[str, Any],
    progress_cb: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Full OCR subtitle channel. Returns [{start,end,text,lang,source='ocr'}]."""
    prov = _vision_provider(cfg)
    if not prov["base_url"] or not prov["model"]:
        return []
    frames = extract_frames(media_path, course_dir, progress_cb=progress_cb)
    if not frames:
        return []

    interval = SAMPLE_INTERVAL
    results: list[tuple[float, str]] = []
    failed = 0
    with httpx.Client() as client:
        for i, frame in enumerate(frames):
            if should_cancel and should_cancel():
                return []
            t = (i + 0.5) * interval  # ffmpeg fps=1/interval yields frames centered in each window
            try:
                text = ocr_frame(client, prov, frame)
                results.append((t, text))
            except Exception:
                failed += 1
                results.append((t, ""))
            if progress_cb and i % 5 == 0:
                progress_cb(0.3 + 0.6 * i / len(frames), f"OCR 识别 {i + 1}/{len(frames)} 帧…")

    cues = merge_cues(results, interval)
    for c in cues:
        c["source"] = OCR_PREFIX
    if progress_cb:
        progress_cb(0.95, f"OCR 提取 {len(cues)} 条字幕")
    return cues
