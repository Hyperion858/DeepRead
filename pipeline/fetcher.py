"""Video fetcher: download from a share URL (douyin etc.).

Douyin has its own signed-API client (yt-dlp's extractor is blocked by
Argus security as of late 2026); yt-dlp handles everything else.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# secsdk signature salt for the web detail API (matches PR yt-dlp#17826)
_SEC_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"


def _douyin_id(url: str) -> str:
    m = re.search(r"douyin\.com/(?:video/)?(\d{15,})", url)
    return m.group(1) if m else ""


def _http(url: str, headers: dict[str, str] | None = None,
          data: bytes | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def _douyin_ttwid() -> str:
    """Fresh ttwid from bytedance's register endpoint (no login needed)."""
    r = _http("https://www.douyin.com/ttwid/register",
              headers={"content-type": "application/json", "user-agent": _UA},
              data=json.dumps({"aid": 6383, "service": "www.douyin.com"}).encode())
    return dict(r.headers).get("Set-Cookie", "").split("ttwid=")[1].split(";")[0]


def _douyin_detail(video_id: str) -> dict[str, Any]:
    """Signed web-detail API call. Returns the aweme_detail dict.

    Needs ttwid + uifid (x-tt-trace-host of the homepage) + an MD5
    x-secsdk-web-signature over the query — that's what Argus checks.
    """
    ttwid = _douyin_ttwid()
    home = _http("https://www.douyin.com/",
                 headers={"user-agent": _UA, "cookie": f"ttwid={ttwid}"})
    uifid = home.headers.get("x-tt-trace-host", "")
    ts = str(int(time.time()))
    q = {"aweme_id": video_id, "aid": "6383", "uifid": uifid, "timestamp": ts}
    encoded = urllib.parse.urlencode(q)
    sig = hashlib.md5(f"{uifid}_{ts}_{_SEC_SALT}_{encoded}".encode()).hexdigest()
    url = ("https://www.douyin.com/aweme/v1/web/aweme/detail/?"
           + encoded + "&x-secsdk-web-signature=" + sig)
    r = _http(url, headers={"user-agent": _UA, "cookie": f"ttwid={ttwid}",
                            "referer": "https://www.douyin.com/"})
    return json.load(r).get("aweme_detail") or {}


def douyin_fetch(url: str, media_dir: Path, cid: str,
                 progress_cb: Any = None) -> dict[str, Any]:
    """Download a douyin video through the signed web API.

    Returns the same shape as fetch_info + a chosen media path.
    Raises RuntimeError when the URL isn't douyin or the API refuses.
    """
    video_id = _douyin_id(url)
    if not video_id:
        raise RuntimeError("不是抖音链接")
    detail = _douyin_detail(video_id)
    if not detail:
        raise RuntimeError("抖音接口未返回视频详情（可能需要登录 cookies）")
    video = detail.get("video") or {}
    bitrates = video.get("bit_rate") or []
    # fall back to play_addr when the bitrate ladder is missing
    play_url = ""
    if bitrates:
        best = max(bitrates, key=lambda b: b.get("bit_rate") or 0)
        play_url = ((best.get("play_addr") or {}).get("url_list") or [""])[0]
    if not play_url:
        play_url = ((video.get("play_addr") or {}).get("url_list") or [""])[0]
    if not play_url:
        raise RuntimeError("抖音接口未返回可下载的视频地址")

    media_dir.mkdir(parents=True, exist_ok=True)
    out = media_dir / f"{cid}.mp4"
    with _http(play_url, headers={"user-agent": _UA,
                                  "referer": "https://www.douyin.com/"},
               timeout=600) as resp, open(out, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if progress_cb and total:
                progress_cb(got / total, f"下载中 {got // 1048576}/{total // 1048576} MB")
    if not out.exists() or out.stat().st_size < 10000:
        raise RuntimeError("抖音视频下载不完整")
    duration = float(video.get("duration") or detail.get("duration") or 0) / 1000.0
    return {"path": out, "title": (detail.get("desc") or "").strip() or url,
            "duration": duration, "kind": "video"}


def _cookie_args() -> list[str]:
    """yt-dlp cookie options: use the mounted cookies file when present."""
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
    if should_cancel and should_cancel():
        raise RuntimeError("已取消")
    # douyin: signed-API client (yt-dlp's extractor is 403-blocked)
    if _douyin_id(url):
        return douyin_fetch(url, media_dir, cid, progress_cb=progress_cb)
    media_dir.mkdir(parents=True, exist_ok=True)
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
