"""Video fetcher: download from a share URL (douyin etc.) via yt-dlp."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def _cookie_args() -> list[str]:
    """yt-dlp cookie options: use the mounted cookies file when present.

    Douyin blocks anonymous info queries ("Fresh cookies are needed"); export
    cookies.txt from a logged-in browser (Get cookies.txt extension) and put
    it at data/cookies.txt (host) -> /app/data/cookies.txt (container).
    """
    p = Path("/app/data/cookies.txt")
    return ["--cookies", str(p)] if p.exists() else []


def fetch_info(url: str) -> dict[str, Any]:
    r = subprocess.run(
        ["yt-dlp", "-j", "--no-warnings", *_cookie_args(), url],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp 获取信息失败: {r.stderr.decode(errors='replace')[-300:]}")
    meta = json.loads(r.stdout.decode())
    return {
        "title": meta.get("title") or "",
        "webpage_id": meta.get("id") or "",
        "duration": float(meta.get("duration") or 0),
        "extractor": meta.get("extractor_key") or "",
    }


def download(url: str, media_dir: Path, cid: str,
             progress_cb: Any = None,
             should_cancel: Any = None) -> dict[str, Any]:
    """Download best mp4 into media_dir as <cid>.<ext>. Returns {path,title,duration}."""
    media_dir.mkdir(parents=True, exist_ok=True)
    if should_cancel and should_cancel():
        raise RuntimeError("已取消")
    info = fetch_info(url)
    out_tmpl = str(media_dir / f"{cid}.%(ext)s")
    cmd = [
        "yt-dlp", "--no-warnings",
        *_cookie_args(),
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", out_tmpl,
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp 下载失败: {r.stderr.decode(errors='replace')[-400:]}")

    # prefer mp4, then any media file yt-dlp produced for this cid
    media = None
    for ext in ("mp4", "mkv", "webm", "mov", "m4a", "mp3"):
        p = media_dir / f"{cid}.{ext}"
        if p.exists():
            media = p
            break
    if media is None:
        candidates = [p for p in media_dir.glob(f"{cid}.*")
                      if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".aac", ".flac"}]
        if not candidates:
            raise RuntimeError("下载完成但未找到媒体文件")
        media = candidates[0]

    kind = "audio" if media.suffix.lower() in {".mp3", ".m4a", ".aac", ".flac"} else "video"
    return {"path": media, "title": info["title"], "duration": info["duration"], "kind": kind}
