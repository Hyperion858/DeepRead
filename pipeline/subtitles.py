"""Embedded-subtitle channel: probe subtitle streams, extract via ffmpeg,
parse SRT/VTT into timed lines."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from pipeline.transcribe import detect_lang, probe_duration


def probe_subtitle_streams(media_path: Path) -> list[dict[str, Any]]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-select_streams", "s",
         "-of", "json", str(media_path)],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        return []
    try:
        info = json.loads(r.stdout.decode())
    except ValueError:
        return []
    streams = []
    for st in info.get("streams", []):
        streams.append({
            "index": st.get("index"),
            "codec": st.get("codec_name"),
            "lang": (st.get("tags") or {}).get("language", ""),
        })
    return streams


def extract_subtitle_channel(media_path: Path, course_dir: Path) -> list[dict[str, Any]]:
    """Return [{start, end, text, lang, source='embedded'}] or [] when absent.

    Supports mp4 mov_text, mkv srt/ass/webvtt (ffmpeg converts to srt on extract
    for text codecs; image codecs like pgssub are skipped).
    """
    streams = probe_subtitle_streams(media_path)
    text_codecs = {"subrip", "srt", "mov_text", "webvtt", "ass", "ssa", "text"}
    stream = next((s for s in streams if s["codec"] in text_codecs), None)
    if stream is None:
        return []

    srt_path = course_dir / "embedded.srt"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(media_path), "-map", f"0:{stream['index']}",
         "-c:s", "srt", str(srt_path)],
        capture_output=True, timeout=300,
    )
    if r.returncode != 0 or not srt_path.exists():
        return []
    lines = parse_srt(srt_path.read_text(encoding="utf-8", errors="replace"))
    for ln in lines:
        ln["lang"] = ln.get("lang") or detect_lang(ln["text"])
        ln["source"] = "embedded"
    return lines


TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _ts_to_sec(ts: str) -> float:
    m = TS.match(ts.strip())
    if not m:
        return 0.0
    h, mi, s, ms = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def parse_srt(content: str) -> list[dict[str, Any]]:
    """Parse SRT or VTT into lines; merge consecutive cues that share text continuation."""
    content = content.replace("WEBVTT", "")
    blocks = re.split(r"\n\s*\n", content)
    out: list[dict[str, Any]] = []
    for block in blocks:
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue
        # drop ordinal line and anything before the timestamp line
        ts_line = next((l for l in lines if TS.search(l)), None)
        if ts_line is None:
            continue
        m = re.match(rf"\s*{TS.pattern}\s*-->\s*{TS.pattern}", ts_line)
        if not m:
            continue
        start = _ts_to_sec(m.group(0).split("-->")[0])
        end = _ts_to_sec(m.group(0).split("-->")[1])
        text_lines = lines[lines.index(ts_line) + 1:]
        # strip html-ish tags <i>, <c.xxx>, positioning inline tags
        text = re.sub(r"<[^>]+>", "", " ".join(text_lines)).strip()
        if text:
            out.append({"start": start, "end": end, "text": text, "lang": None})
    out.sort(key=lambda x: x["start"])
    return out
