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


_SENT_SPLIT = re.compile(r"(?<=[.!?。！？；;])\s*")


def split_sentences(text: str) -> list[str]:
    """Split a plain transcript into sentences at punctuation / newlines."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for piece in _SENT_SPLIT.split(line):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def resplit_for_subtitles(
    sentences: list[str], part_dur: float, max_sec: float = 7.0
) -> list[str]:
    """Break oversized sentences into subtitle-sized chunks.

    ASR text from models like SenseVoiceSmall often arrives with little or no
    punctuation, so one "sentence" can span a minute of speech. Subtitles need
    short lines, so anything longer than ~max_sec of speech is split at commas
    and then hard-wrapped by character budget.
    """
    if not sentences or part_dur <= 0:
        return sentences
    total_chars = sum(len(s) for s in sentences) or 1
    cps = total_chars / part_dur                 # characters per second
    max_chars = max(30, int(cps * max_sec))

    out: list[str] = []
    for s in sentences:
        if len(s) <= max_chars:
            out.append(s)
            continue
        buf = ""
        pieces: list[str] = []
        # split AFTER the comma but keep following whitespace attached, so
        # rejoining preserves the original spacing ("Today, I am" not "Today,I am")
        for token in re.split(r"(?<=[,，、])", s):
            if not token:
                continue
            # only break at a comma once the buffer is reasonably full,
            # otherwise short openers like "Today," become their own line
            if buf and len(buf) + len(token) > max_chars and len(buf) >= max_chars * 0.45:
                pieces.append(buf.strip())
                buf = token
            else:
                buf += token
        if buf.strip():
            pieces.append(buf.strip())
        for p in pieces:
            # tokens with no comma at all can still be huge — wrap by length,
            # breaking at a space so English words are never cut in half
            while len(p) > max_chars * 1.5:
                cut = p[:max_chars]
                sp = cut.rfind(" ")
                if sp > max_chars * 0.5:
                    cut = cut[:sp]
                out.append(cut.strip())
                p = p[len(cut):].strip()
            if p:
                out.append(p)
    return out


def align_to_speech(
    part: Path,
    sentences: list[str],
    offset: float,
    part_dur: float,
) -> list[dict[str, Any]]:
    """Give plain-text sentences real timestamps using detected speech pauses.

    Models like SenseVoiceSmall return text only, with no timing. We therefore
    build a proportional timeline by character count and then snap every
    sentence boundary onto the nearest detected silence midpoint, so boundaries
    land on actual pauses instead of arbitrary positions.

    Falls back to pure proportional timing when no pause is close enough.
    """
    if not sentences:
        return []
    sentences = resplit_for_subtitles(sentences, part_dur)
    if not sentences:
        return []
    if len(sentences) == 1 or part_dur <= 0:
        text = sentences[0]
        return [{
            "start": offset, "end": offset + max(part_dur, 0.2),
            "text": text, "lang": detect_lang(text), "source": "asr",
        }]

    # pause midpoints are the only reliable structural signal we have
    try:
        pauses = detect_silences(part) if part_dur >= 8 else []
    except Exception:  # noqa: BLE001 — alignment is best-effort
        pauses = []

    total_chars = sum(len(s) for s in sentences) or 1
    # minimum screen time per sentence — keeps short lines from collapsing
    min_dur = 0.6 if part_dur / len(sentences) >= 0.6 else part_dur / len(sentences) * 0.5
    # snap tolerance must stay small: a large tolerance lets one boundary jump
    # many seconds and squeeze every following sentence into a sliver
    tol = min(2.5, max(1.0, part_dur * 0.02))

    bounds: list[float] = []
    cursor = 0.0
    acc = 0
    for i, s in enumerate(sentences[:-1]):
        acc += len(s)
        target = acc / total_chars * part_dur
        if pauses:
            cands = [p for p in pauses
                     if abs(p - target) <= tol and p > cursor + min_dur]
            if cands:
                target = min(cands, key=lambda p: abs(p - target))
        # never eat the space the remaining sentences still need
        remaining = len(sentences) - i - 1
        latest = part_dur - remaining * min_dur
        if target > latest:
            target = latest
        if target <= cursor + min_dur * 0.6:
            target = cursor + min_dur * 0.6
        bounds.append(target)
        cursor = target

    edges = [0.0] + bounds + [part_dur]
    # enforce strictly increasing edges — rounding and clamping can otherwise
    # produce a boundary that sits before its predecessor
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] + 0.05
    if edges[-1] > part_dur and edges[-1] > 0:
        scale = part_dur / edges[-1]
        edges = [e * scale for e in edges]
    segs: list[dict[str, Any]] = []
    for i, text in enumerate(sentences):
        start, end = edges[i], edges[i + 1]
        if end <= start:
            end = start + 0.15
        segs.append({
            "start": offset + start,
            "end": offset + end,
            "text": text,
            "lang": detect_lang(text),
            "source": "asr",
        })
    return segs


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
    part_starts: list[float] = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        if end - start < 5:
            continue   # skipped here means skipped in the offsets below
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
        part_starts.append(start)
    return parts, part_starts  # start times aligned 1:1 with the returned parts


def _native_url(base_url: str) -> str:
    """Map a compatible-mode base URL to the native DashScope ASR endpoint.

    https://x/compatible-mode/v1  ->  https://x/api/v1/services/aigc/multimodal-generation/generation
    The compatible-mode chat endpoint cannot serve qwen-audio-asr models
    (they answer 400 with an empty body); the native endpoint returns
    word-level timestamps.
    """
    root = (base_url or "").rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-mode/v1/"):
        if root.endswith(suffix.rstrip("/")):
            root = root[: -len(suffix.rstrip("/"))]
            break
    return f"{root}/api/v1/services/aigc/multimodal-generation/generation"


def is_native_asr_model(model: str) -> bool:
    """Dedicated ASR models speak the native endpoint and return timestamps."""
    low = (model or "").lower()
    if "realtime" in low:          # realtime variants need a WebSocket session
        return False
    if "omni" in low:              # omni models are chat-style, no timestamps
        return False
    return ("asr" in low) or ("fun-asr" in low)


def mode_for_model(model: str) -> str:
    """Single source of truth for how a model must be called.

    native -> DashScope ASR endpoint (word-level timestamps)
    chat   -> /chat/completions + input_audio (omni/end-to-end audio)
    api    -> /audio/transcriptions (whisper-style)
    """
    low = (model or "").lower()
    if is_native_asr_model(model):
        return "native"
    if any(k in low for k in ("omni", "qwen-audio", "audio-chat", "gemini", "gpt-4o-audio")):
        return "chat"
    return "api"


_SENT_END = {".", "?", "!", "。", "？", "！", ";", "；", "…"}


def _parse_native_segments(output: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    """Turn word-level timestamps into sentence segments.

    The native endpoint always returns ONE ``sentence`` object holding the whole
    transcript plus a ``words`` array (per-word begin/end in ms). We regroup
    words into sentences at punctuation and at long pauses, so subtitle timing
    is real audio timing rather than a guess.
    """
    sent = output.get("sentence") or {}
    words = sent.get("words") or []
    if not words:
        text = (sent.get("text") or output.get("text") or "").strip()
        if not text:
            return []
        return [{
            "start": offset + float(sent.get("begin_time", 0)) / 1000.0,
            "end": offset + float(sent.get("end_time", 0)) / 1000.0,
            "text": text, "lang": detect_lang(text), "source": "asr",
        }]

    segs: list[dict[str, Any]] = []
    buf: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        # whitespace arrives as its own token — keep it, or English words glue
        # together ("fun" + "on" -> "funon"). Trim only the outer edges.
        text = "".join((w.get("text") or "") + (w.get("punctuation") or "") for w in buf).strip()
        real = [w for w in buf if (w.get("text") or "").strip()]
        if text and real:
            segs.append({
                "start": offset + float(real[0].get("begin_time", 0)) / 1000.0,
                "end": offset + float(real[-1].get("end_time", 0)) / 1000.0,
                "text": text,
                "lang": detect_lang(text),
                "source": "asr",
            })
        buf = []

    prev_end = None
    word_count = 0          # counts only real words, not whitespace tokens
    for w in words:
        txt = w.get("text") or ""
        is_space = not txt.strip()
        # a pause longer than 1.2s starts a new subtitle line even mid-sentence
        if not is_space:
            b = float(w.get("begin_time", 0))
            if prev_end is not None and (b - prev_end) > 1200 and word_count >= 3:
                flush()
                word_count = 0
            prev_end = float(w.get("end_time", 0))
            word_count += 1
        buf.append(w)           # whitespace is part of the text stream
        if not is_space and (w.get("punctuation") or "") in _SENT_END:
            flush()
            word_count = 0
        elif word_count >= 40:  # safety cap for unpunctuated streams
            flush()
            word_count = 0
    flush()
    return segs


def _transcribe_via_native(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    part: Path,
    offset: float,
) -> list[dict[str, Any]]:
    """Native DashScope ASR — returns precise word/sentence timestamps."""
    b64 = base64.b64encode(part.read_bytes()).decode()
    fmt = "mp3" if part.suffix.lower() == ".mp3" else "wav"
    r = client.post(
        _native_url(base_url),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": {"messages": [{"role": "user", "content": [
                {"audio": f"data:audio/{fmt};base64,{b64}"},
            ]}]},
            "parameters": {"format": fmt, "sample_rate": 16000},
        },
        timeout=600,
    )
    if r.status_code != 200:
        body_txt = (r.text or "").strip()
        msg = f"转写原生 API {r.status_code}: {body_txt[:300]}"
        if r.status_code == 400 and body_txt in ("", "{}"):
            msg = (f"模型 {model} 原生接口返回 HTTP 400（空错误体）——请确认模型名与业务空间地域匹配。")
        elif r.status_code == 403:
            msg = f"模型 {model} 被拒绝（HTTP 403）：{body_txt[:200]}"
        elif r.status_code == 404:
            msg = f"模型 {model} 原生接口不存在（HTTP 404），请检查 base_url。"
        raise RuntimeError(msg)
    return _parse_native_segments(r.json().get("output") or {}, offset)


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
        body_txt = r.text.strip()
        msg = f"转写 chat API {r.status_code}: {body_txt[:300]}"
        if r.status_code == 400 and body_txt in ("", "{}"):
            msg = (f"模型 {model} 返回 HTTP 400（空错误体）——该模型在当前工作空间不可调用。"
                   "阿里云百炼请改用 qwen3.5-omni-flash 或 qwen3-omni-flash。")
        elif r.status_code == 404:
            msg = f"模型 {model} 不存在或未开通（HTTP 404）"
        elif r.status_code == 403:
            msg = f"模型 {model} 无权限（HTTP 403），请在服务商控制台为 API Key 开通该模型"
        raise RuntimeError(msg)
    # NOTE: reasoning_content is the model's thinking, NOT the transcript —
    # only `content` holds the transcribed text. Never fall back to it.
    content = (r.json()["choices"][0].get("message", {}).get("content") or "").strip()
    if not content:
        return []

    # chat models give no timestamps either; split into sentences and align
    # them onto real pauses instead of dividing the clip evenly by line count
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    lines = [re.sub(r"^[-*\d.、)\s]+", "", ln) or ln for ln in lines]
    sentences: list[str] = []
    for ln in lines:
        sentences.extend(split_sentences(ln) or [ln])
    return align_to_speech(part, sentences, offset, probe_duration(part))


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
    """Dispatch by mode:
      native -> DashScope native ASR (precise timestamps)
      chat   -> chat completions + input_audio (omni models, no timestamps)
      api    -> /v1/audio/transcriptions (whisper-style)
    """
    if mode == "native":
        return _transcribe_via_native(client, base_url, api_key, model, part, offset)
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

    # fallback: plain JSON {"text": ...} or bare text. Use the parsed field —
    # r.text is the raw JSON body and would leak {"text":"..."} into subtitles.
    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("text") or "").strip()
    if not text:
        text = (r.text or "").strip()
        if text.startswith("{"):     # unparseable JSON — don't dump it as text
            text = ""
    if not text:
        return []
    # text-only provider (e.g. SenseVoiceSmall): split into sentences and give
    # each a real timestamp by snapping boundaries onto detected pauses
    sentences = split_sentences(text)
    return align_to_speech(part, sentences, offset, probe_duration(part))


def detect_lang(text: str) -> str:
    """Rough script-based detection: cjk-dominant -> zh, else en."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "zh" if cjk > letters * 0.3 else "en"


def _provider(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve provider settings for transcription.

    mode: "native"  -> DashScope native ASR (word timestamps)
          "chat"    -> chat completions + input_audio (omni models, no timestamps)
          "api"     -> /v1/audio/transcriptions (whisper-style)
          "tencent" -> Tencent Cloud 录音文件识别 (TC3 signing, async task)

    A provider with kind="tencent" always uses the Tencent path; otherwise the
    mode is inferred from the model name.
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

    kind = (prov or {}).get("kind") or "openai"
    if kind == "tencent":
        mode = "tencent"
    else:
        mode = mode_for_model(model)

    return {
        "base_url": base, "api_key": key, "model": model,
        "provider_id": (prov or {}).get("id", ""), "mode": mode,
        # Tencent credentials (empty for other kinds)
        "secret_id": (prov or {}).get("secret_id", ""),
        "secret_key": (prov or {}).get("secret_key", ""),
        "app_id": (prov or {}).get("app_id", ""),
        "region": (prov or {}).get("region", "ap-guangzhou"),
        "engine": (prov or {}).get("engine", ""),
    }


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
    # several ASR models selected: walk them in order, marking failures
    selected_models = [m for m in (cfg.get("asr_models") or []) if m] or [model]
    if mode == "tencent":
        if not prov.get("secret_id") or not prov.get("secret_key"):
            raise RuntimeError("腾讯云 SecretId / SecretKey 未配置")
    elif not base_url:
        raise RuntimeError("转写 API base_url 未配置")
    # native / tencent return real timestamps, so long windows are fine.
    # api-mode providers may be text-only (SenseVoiceSmall): shorter windows
    # mean fewer sentences per window, which makes pause-based alignment far
    # more accurate. chat mode has no timestamps either and stays at 60s.
    if mode in ("native", "tencent"):
        seg_secs = 300
    elif mode == "api":
        seg_secs = 90
    else:
        seg_secs = 60

    audio_dir = workdir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav = audio_dir / "full.wav"

    if progress_cb:
        progress_cb(0.0, "ffmpeg 提取音频…")
    extract_audio(media_path, wav)
    duration = probe_duration(wav)
    if progress_cb:
        progress_cb(0.05, f"音频 {duration:.0f}s，分段中…")

    # snap cuts to natural speech pauses where possible — avoids mid-sentence
    # splits at window boundaries
    silences = detect_silences(wav)
    parts, part_starts = split_audio(wav, audio_dir / "parts", seg_secs, cut_points=silences)

    # compress to mp3 (~6x smaller): chat for upload speed; native/tencent
    # because raw wav would exceed the upstream payload caps
    if mode in ("chat", "native", "tencent"):
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
        # several models selected: walk them in order and mark the ones that
        # fail, so the settings page can flag them
        if len(selected_models) > 1 and mode != "tencent":
            from pipeline.lesson import audio_transcribe_with_failover
            segs, _used = audio_transcribe_with_failover(cfg, parts[i])
            for s in segs:
                s["start"] += offset
                s["end"] += offset
            return segs
        if mode == "tencent":
            # Tencent uses TC3 signing + an async task, so it has its own path
            from pipeline import tencent_asr
            try:
                segs = tencent_asr.transcribe_native(
                    parts[i],
                    secret_id=prov.get("secret_id", ""),
                    secret_key=prov.get("secret_key", ""),
                    region=prov.get("region") or "ap-guangzhou",
                    engine=prov.get("engine", ""),
                )
            except Exception as exc:  # noqa: BLE001
                from pipeline.lesson import note_model_result
                note_model_result(prov.get("provider_id", ""), model, False, str(exc)[:160])
                raise
            for s in segs:
                s["start"] += offset
                s["end"] += offset
                if not s.get("lang"):
                    s["lang"] = detect_lang(s["text"])
            from pipeline.lesson import note_model_result
            note_model_result(prov.get("provider_id", ""), model, True)
            return segs
        with httpx.Client(timeout=httpx.Timeout(connect=15, read=300, write=120, pool=15)) as client:
            try:
                segs = _transcribe_segment(client, base_url, api_key, model, parts[i], offset, None, mode)
            except Exception as exc:  # noqa: BLE001 — record the mark, then fail
                from pipeline.lesson import note_model_result
                note_model_result(prov.get("provider_id", ""), model, False, str(exc)[:160])
                raise
            from pipeline.lesson import note_model_result
            note_model_result(prov.get("provider_id", ""), model, True)
            return segs

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

    # guard against offset misalignment: any segment that lands past the
    # media duration is garbage from a bad offset and would poison the
    # timeline (sentences "at 52 minutes" in a 3-minute video)
    duration = probe_duration(wav)
    if duration > 0:
        all_segments = [s for s in all_segments if s["start"] < duration + 5]
    all_segments.sort(key=lambda s: s["start"])
    # clamp sub-segment boundary overlaps: adjacent windows can disagree by a
    # few hundredths of a second, which shows up as duplicated subtitle lines
    for i in range(1, len(all_segments)):
        prev_end = all_segments[i - 1]["end"]
        if all_segments[i]["start"] < prev_end - 0.01:
            all_segments[i]["start"] = prev_end
        if all_segments[i]["end"] <= all_segments[i]["start"]:
            all_segments[i]["end"] = all_segments[i]["start"] + 0.05
    if progress_cb:
        progress_cb(1.0, f"转写完成，共 {len(all_segments)} 段")
    return all_segments
