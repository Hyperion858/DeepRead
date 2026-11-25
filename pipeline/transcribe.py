"""Transcription channel: extract audio with ffmpeg, segment, call an
OpenAI-compatible /v1/audio/transcriptions endpoint, merge with offsets."""
from __future__ import annotations

import base64
import json
import re
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


def detect_silences(wav_path: Path, noise_db: str = "-30dB", min_silence: float = 0.4) -> list[float]:
    """Midpoints of silence stretches — natural cut points."""
    r = _run([
        "ffmpeg", "-i", str(wav_path), "-af",
        f"silencedetect=noise={noise_db}:d={min_silence}",
        "-f", "null", "-",
    ], timeout=600)
    if r.returncode != 0:
        return []
    out = r.stderr.decode(errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    # midpoint of each silence stretch
    points = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else s + min_silence
        points.append((s + e) / 2)
    return points


def split_audio(wav_path: Path, out_dir: Path, seg_seconds: int,
                cut_points: list[float] | None = None) -> list[Path]:
    """Split wav into segments. When cut_points (silence midpoints) are given,
    cuts land at natural pauses near each window boundary instead of mid-word."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(wav_path)
    # choose boundaries: ideal boundary every seg_seconds; snap to the nearest
    # silence midpoint within ±8s when available
    boundaries = [0.0]
    ideal = seg_seconds
    while ideal < duration:
        target = ideal
        if cut_points:
            candidates = [p for p in cut_points if abs(p - target) <= 8.0 and p > boundaries[-1] + 10]
            if candidates:
                target = min(candidates, key=lambda p: abs(p - target))
        if target <= boundaries[-1] + 10:  # no usable snap — hard cut
            target = ideal
        boundaries.append(target)
        ideal += seg_seconds
    boundaries.append(duration)
    # dedupe + sort
    boundaries = sorted(set(round(b, 2) for b in boundaries))

    parts: list[Path] = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        if end - start < 5:
            continue
        part = out_dir / f"seg_{i:04d}.wav"
        r = _run([
            "ffmpeg", "-y", "-i", str(wav_path), "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
            "-c", "copy", str(part),
        ])
        if r.returncode != 0:
            r = _run([
                "ffmpeg", "-y", "-i", str(wav_path), "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                "-c:a", "pcm_s16le", str(part),
            ])
            if r.returncode != 0:
                raise RuntimeError(f"音频分段失败 seg_{i}: {r.stderr.decode(errors='replace')[-300:]}")
        parts.append(part)
    return parts, boundaries[:-1]  # part start times for offset math


def _transcribe_via_chat(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    part: Path,
    offset: float,
) -> list[dict[str, Any]]:
    """Transcribe an audio segment through chat completions with input_audio.

    No native timestamps — we ask the model for line-per-sentence output and
    distribute the segment duration evenly across lines (line count ≈ speech density).
    """
    data = part.read_bytes()
    b64 = base64.b64encode(data).decode()
    fmt = "mp3" if part.suffix.lower() == ".mp3" else "wav"
    r = client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "逐字转写这段音频。要求：按说话顺序输出，每句话单独一行；"
                    "保留原始语言（中文说中文，英文说英文）；不要添加任何解释、总结或标注。"
                )},
                {"type": "input_audio", "input_audio": {"data": f"data:audio/{fmt};base64,{b64}", "format": fmt}},
            ]}],
            "max_tokens": 8000,
            "temperature": 0,
        },
        timeout=600,
    )
    if r.status_code != 200:
        raise RuntimeError(f"转写 chat API {r.status_code}: {r.text[:300]}")
    content = r.json()["choices"][0]["message"]["content"].strip()
    if not content:
        return []

    part_dur = probe_duration(part)
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    # strip markdown list artifacts
    lines = [re.sub(r"^[-*\d.、)\s]+", "", ln) or ln for ln in lines]
    n = max(len(lines), 1)
    dur_per = part_dur / n
    segments = []
    t = offset
    for ln in lines:
        segments.append({
            "start": t,
            "end": t + dur_per,
            "text": ln,
            "lang": detect_lang(ln),
            "source": "asr",
        })
        t += dur_per
    return segments


def _transcribe_segment(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    part: Path,
    offset: float,
    language: str | None,
    mode: str = "api",
) -> list[dict[str, Any]]:
    """Call /v1/audio/transcriptions with verbose_json; fall back to plain text.
    For omni-style models (mode='chat'), routes through chat completions instead."""
    if mode == "chat":
        return _transcribe_via_chat(client, base_url, api_key, model, part, offset)
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


def _provider(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve {base_url, api_key, model, mode} for the ASR provider.

    mode: "api"  -> /v1/audio/transcriptions (whisper-style)
          "chat" -> chat completions with input_audio (omni models)
    Determined from the model name: omni/audio/qwen-audio models speak chat.
    """
    providers = cfg.get("providers") or []
    pid = cfg.get("asr_provider")
    prov = next((p for p in providers if p.get("id") == pid), None)
    if prov is None and providers:
        prov = providers[0]
    if prov:
        base = prov.get("base_url", "").rstrip("/")
        key = prov.get("api_key", "")
        model = cfg.get("asr_model") or "whisper-1"
    else:  # legacy flat config
        base = (cfg.get("asr_base_url") or cfg.get("base_url") or "").rstrip("/")
        key = cfg.get("asr_api_key") or cfg.get("api_key", "")
        model = cfg.get("asr_model") or cfg.get("transcribe_model") or "whisper-1"
    low = model.lower()
    is_chat_audio = any(k in low for k in ("omni", "qwen-audio", "audio-chat", "gemini", "gpt-4o-audio"))
    return {"base_url": base, "api_key": key, "model": model,
            "mode": "chat" if is_chat_audio else "api"}


def transcribe(
    media_path: Path,
    cfg: dict[str, Any],
    workdir: Path,
    progress_cb: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    workers: int = 4,
) -> list[dict[str, Any]]:
    prov = _provider(cfg)
    base_url = prov["base_url"]
    api_key = prov["api_key"]
    model = prov["model"]
    mode = prov["mode"]
    if not base_url:
        raise RuntimeError("转写 API base_url 未配置")
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

    # chat-mode omni models have no native timestamps: smaller segments mean
    # tighter time accuracy (each segment's lines are spread across its window).
    # API mode (whisper-style) returns real timestamps, so long segments are fine.
    seg_secs = max(seg_minutes * 60, 60) if mode == "api" else 60
    # snap cuts to natural speech pauses where possible — avoids mid-sentence
    # splits at window boundaries
    silences = detect_silences(wav)
    parts, part_starts = split_audio(wav, audio_dir / "parts", seg_secs, cut_points=silences)

    # compress chat-mode parts to mp3: ~6x smaller upload per segment
    if mode == "chat":
        mp3_parts = []
        for part in parts:
            mp3 = part.with_suffix(".mp3")
            r = _run(["ffmpeg", "-y", "-i", str(part), "-b:a", "48k", str(mp3)], timeout=300)
            mp3_parts.append(mp3 if (r.returncode == 0 and mp3.exists()) else part)
        parts = mp3_parts

    all_segments: list[dict[str, Any]] = []

    # concurrent transcription: segments are independent; 4 workers balance
    # speed against relay rate limits
    import concurrent.futures as _futures

    def _transcribe_one(i: int) -> list[dict[str, Any]]:
        if should_cancel and should_cancel():
            raise PipelineCancelled()
        offset = part_starts[i] if i < len(part_starts) else i * seg_secs
        # auto_model_switch: walk the ASR model priority list per segment
        if cfg.get("auto_model_switch"):
            from pipeline.lesson import audio_transcribe_with_failover
            segs, _used = audio_transcribe_with_failover(cfg, parts[i])
            # re-offset: failover helper returns segment-local times
            for s in segs:
                s["start"] += offset
                s["end"] += offset
            return segs
        with httpx.Client(timeout=httpx.Timeout(connect=15, read=300, write=120, pool=15)) as client:
            return _transcribe_segment(client, base_url, api_key, model, parts[i], offset, None, mode)

    done = 0
    with _futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_transcribe_one, i): i for i in range(len(parts))}
        for fut in _futures.as_completed(futures):
            i = futures[fut]
            try:
                all_segments.extend(fut.result())
            except PipelineCancelled:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            done += 1
            if progress_cb:
                progress_cb(done / len(parts), f"转写分段 {done}/{len(parts)}…")

    all_segments.sort(key=lambda s: s["start"])
    if progress_cb:
        progress_cb(1.0, f"转写完成，共 {len(all_segments)} 段")
    return all_segments
