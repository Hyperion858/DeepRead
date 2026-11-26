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
import time as _time_module
from pathlib import Path
from typing import Any, Callable

import httpx

from pipeline.transcribe import detect_lang, probe_duration

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
    """Extract bottom-crop frames as jpgs at a fixed interval.

    One ffmpeg seek per sample: decode starts at the keyframe before the
    sample time and runs forward (~one GOP), instead of decoding the whole
    30fps stream in a single pass. Output frames are identical to the
    sequential filter-chain version; on low-power ARM boxes this cuts a
    760s video's extraction from minutes to well under one minute.
    """
    frames_dir = workdir / "ocr_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # Frames left over from an earlier run would both inflate the live count
    # (it looked stuck at "380 帧") and be OCR'd if this run failed early.
    for stale in frames_dir.glob("f*.jpg"):
        try:
            stale.unlink()
        except OSError:
            pass
    duration = probe_duration(media_path)
    expected = max(1, round(duration / interval)) if duration else 0
    # crop bottom 22% of the frame, full width — where douyin burns subtitles.
    import concurrent.futures as _futures

    vf = "crop=iw:ih*0.22:0:ih*0.78,scale=960:-2"

    def _grab(i: int) -> int:
        t = (i + 0.5) * interval
        out = frames_dir / f"f{i:06d}.jpg"
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(media_path),
                 "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)],
                capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return -1
        return i if (r.returncode == 0 and out.exists()) else -1

    ok = 0
    with _futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_grab, i) for i in range(expected)]
        for n_done, fut in enumerate(_futures.as_completed(futs), 1):
            if fut.result() >= 0:
                ok += 1
            if progress_cb:
                progress_cb(0.01 + 0.26 * n_done / expected,
                            f"抽帧中… 已提取 {ok}/{expected} 帧")
    if progress_cb:
        progress_cb(0.3, f"抽取 {ok} 帧")
    if expected and ok == 0:
        raise RuntimeError("抽帧失败（所有采样点均未产出帧）")
    # keep original sample order; OCR step reads the sample index from the
    # filename so a missing seek doesn't shift everyone's timestamps
    return sorted(frames_dir.glob("f*.jpg"))


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
        timeout=httpx.Timeout(connect=15, read=90, write=30, pool=15),
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
        # caption switch: if the new caption starts where the old one ended
        # (a long sentence split across two frames), join instead of cut
        if (norm and cur_text is not None and last_seen is not None
                and abs(t - last_seen) <= interval + 0.01
                and _is_continuation(cur_text, text)):
            cur_text = cur_text.rstrip("。，,．.？！?!；;：:…") + text.lstrip()
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


def merge_fragments(cues: list[dict[str, Any]], min_len: int = 14) -> list[dict[str, Any]]:
    """Post-merge: short cues (fragments left by frame sampling, e.g. 'quick',
    'm on a Friday') are absorbed into the neighbor they continue."""
    if not cues:
        return cues
    out: list[dict[str, Any]] = [dict(cues[0])]
    for c in cues[1:]:
        prev = out[-1]
        prev_n = _norm(prev["text"])
        cur_n = _norm(c["text"])
        gap = c["start"] - prev["end"]
        # either side is short, they are temporally adjacent, and text continues
        if (gap <= 3.0 and (len(prev_n) <= min_len or len(cur_n) <= min_len)
                and _is_continuation(prev["text"], c["text"])):
            prev["text"] = prev["text"].rstrip("。，,．.？！?!；;：:…") + c["text"].lstrip()
            prev["end"] = max(prev["end"], c["end"])
        else:
            out.append(dict(c))
    return out


def _is_continuation(prev: str, nxt: str) -> bool:
    """Heuristic: the next caption continues an unfinished sentence when the
    previous one lacks terminal punctuation and the next lacks a capital at
    an English sentence start / is short."""
    p = prev.strip()
    n = nxt.strip()
    if not p or not n:
        return False
    # unfinished prev: no ending punctuation
    if p[-1] in "。．.？！?!":
        return False
    # english: next starting lowercase suggests continuation
    if p[-1] in "，,、；;：:" :
        return True
    if n[0].isascii() and n[0].isalpha() and n[0].islower():
        return True
    # chinese: prev ending with connector words
    if p[-1] in "的和是在了把对跟与或":
        return True
    return False


def extract_ocr_subtitles(
    media_path: Path,
    course_dir: Path,
    cfg: dict[str, Any],
    progress_cb: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    workers: int = 6,
    interval: float = SAMPLE_INTERVAL,
) -> list[dict[str, Any]]:
    """Full OCR subtitle channel. Returns [{start,end,text,lang,source='ocr'}]."""
    prov = _vision_provider(cfg)
    if not prov["base_url"] or not prov["model"]:
        return []
    frames = extract_frames(media_path, course_dir,
                            interval=interval, progress_cb=progress_cb)
    if not frames:
        return []

    results: list[tuple[float, str]] = []
    if should_cancel and should_cancel():
        return []

    # concurrent OCR: frames are independent; 6 workers cut wall time ~6x
    # vs serial while staying polite to the relay.
    import concurrent.futures as _futures

    def _ocr_one(entry: tuple[int, Path]) -> tuple[int, str]:
        i, frame = entry
        t = (i + 0.5) * interval
        # several OCR models selected: walk them in order
        if len([m for m in (cfg.get("vision_models") or []) if m]) > 1:
            from pipeline.lesson import ocr_with_failover
            for attempt in range(3):
                try:
                    return (i, ocr_with_failover(cfg, frame))
                except RuntimeError as exc:
                    if attempt == 2:
                        return (i, "")
                    _time_module.sleep(1.0)
                except Exception:
                    if attempt == 2:
                        return (i, "")
                    _time_module.sleep(1.0 * (attempt + 1))
        text = ""
        for attempt in range(3):
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=15, read=90, write=30, pool=15)) as client:
                    text = ocr_frame(client, prov, frame)
                from pipeline.lesson import note_model_result
                note_model_result(prov.get("id", ""), prov.get("model", ""), True)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    from pipeline.lesson import note_model_result
                    note_model_result(prov.get("id", ""), prov.get("model", ""), False, str(exc)[:160])
                    return (i, "")
                _time_module.sleep(1.0 * (attempt + 1))
        return (i, text)

    done_count = 0
    # filename carries the sample index (extract_frames skips failed seeks),
    # so timestamps stay aligned even when some frames are missing
    entries: list[tuple[int, Path]] = []
    for frame in frames:
        m = re.match(r"f(\d+)\.jpg$", frame.name)
        if m:
            entries.append((int(m.group(1)), frame))
    with _futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_ocr_one, e): e[0] for e in entries}
        for fut in _futures.as_completed(futures):
            i, text = fut.result()
            results.append(((i + 0.5) * interval, text))
            done_count += 1
            if progress_cb and done_count % 5 == 0:
                progress_cb(0.3 + 0.6 * done_count / len(entries),
                            f"OCR 识别 {done_count}/{len(entries)} 帧…")
    if progress_cb and entries:
        progress_cb(0.9, f"OCR 完成 {len(entries)} 帧")
    results.sort(key=lambda x: x[0])

    cues = merge_cues(results, interval)
    cues = merge_fragments(cues)
    for c in cues:
        c["source"] = OCR_PREFIX
    if progress_cb:
        progress_cb(0.95, f"OCR 提取 {len(cues)} 条字幕")
    return cues
