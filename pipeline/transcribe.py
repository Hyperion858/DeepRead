"""Transcription channel: extract audio with ffmpeg, segment, call an
OpenAI-compatible /v1/audio/transcriptions endpoint, merge with offsets."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx


class PipelineCancelled(Exception):
    pass


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def probe_duration(media_path: Path) -> float:
    r = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
    ])
    try:
        return float(r.stdout.decode().strip())
    except ValueError:
        return 0.0


def extract_audio(media_path: Path, out_wav: Path) -> Path:
    """16kHz mono wav — the format transcription APIs accept best."""
    r = _run([
        "ffmpeg", "-y", "-i", str(media_path), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav),
    ])
    if r.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"ffmpeg 音频提取失败: {r.stderr.decode(errors='replace')[-500:]}")
    return out_wav


def split_audio(wav_path: Path, out_dir: Path, seg_seconds: int) -> list[Path]:
    """Split wav into fixed-length segments (re-encoding pcm is cheap)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    duration = probe_duration(wav_path)
    n = max(1, int(duration // seg_seconds) + (1 if duration % seg_seconds else 0))
    for i in range(n):
        start = i * seg_seconds
        part = out_dir / f"seg_{i:04d}.wav"
        r = _run([
            "ffmpeg", "-y", "-i", str(wav_path), "-ss", str(start), "-t", str(seg_seconds),
            "-c", "copy", str(part),
        ])
        if r.returncode != 0:
            # stream copy with -ss can fail on wav in some builds; re-encode fallback
            r = _run([
                "ffmpeg", "-y", "-i", str(wav_path), "-ss", str(start), "-t", str(seg_seconds),
                "-c:a", "pcm_s16le", str(part),
            ])
            if r.returncode != 0:
                raise RuntimeError(f"音频分段失败 seg_{i}: {r.stderr.decode(errors='replace')[-300:]}")
        parts.append(part)
    return parts


def _transcribe_segment(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    part: Path,
    offset: float,
    language: str | None,
) -> list[dict[str, Any]]:
    """Call /v1/audio/transcriptions with verbose_json; fall back to plain text."""
    with part.open("rb") as fh:
        files = {"file": (part.name, fh, "audio/wav")}
        data: dict[str, Any] = {"model": model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        r = client.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=600,
        )
    if r.status_code != 200:
        raise RuntimeError(f"转写 API {r.status_code}: {r.text[:300]}")

    try:
        payload = r.json()
    except ValueError:
        payload = None

    segments: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        for seg in payload["segments"]:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            segments.append({
                "start": float(seg.get("start", 0)) + offset,
                "end": float(seg.get("end", 0)) + offset,
                "text": text,
                "lang": detect_lang(text),
                "source": "asr",
            })
        return segments

    # fallback: plain text, estimate timeline proportionally to file duration
    text = (r.text or "").strip()
    if not text:
        return []
    part_dur = probe_duration(part)
    est = min(part_dur / max(len(text), 1) * 1.0, 0.2) if part_dur else 0.0
    segments.append({
        "start": offset,
        "end": offset + max(part_dur, len(text) * est),
        "text": text,
        "lang": detect_lang(text),
        "source": "asr",
    })
    return segments


def detect_lang(text: str) -> str:
    """Rough script-based detection: cjk-dominant -> zh, else en."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "zh" if cjk > letters * 0.3 else "en"


def transcribe(
    media_path: Path,
    cfg: dict[str, Any],
    workdir: Path,
    progress_cb: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("transcribe_model") or "whisper-1"
    if not base_url:
        raise RuntimeError("API base_url 未配置")
    seg_minutes = int(cfg.get("segment_minutes") or 5)

    audio_dir = workdir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav = audio_dir / "full.wav"

    if progress_cb:
        progress_cb(0.0, "ffmpeg 提取音频…")
    extract_audio(media_path, wav)
    duration = probe_duration(wav)
    if progress_cb:
        progress_cb(0.05, f"音频 {duration:.0f}s，分段中…")

    parts = split_audio(wav, audio_dir / "parts", max(seg_minutes * 60, 60))

    all_segments: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for i, part in enumerate(parts):
            if should_cancel and should_cancel():
                raise PipelineCancelled()
            offset = i * seg_minutes * 60
            if progress_cb:
                progress_cb(i / len(parts), f"转写分段 {i + 1}/{len(parts)}…")
            all_segments.extend(
                _transcribe_segment(client, base_url, api_key, model, part, offset, None)
            )

    all_segments.sort(key=lambda s: s["start"])
    if progress_cb:
        progress_cb(1.0, f"转写完成，共 {len(all_segments)} 段")
    return all_segments
