"""Transcription channel: extract audio with ffmpeg, segment, call an
OpenAI-compatible /v1/audio/transcriptions endpoint, merge with offsets.

OFFSET INVARIANT (A1 refactor): every channel-level function
(``_transcribe_via_native`` / ``_transcribe_via_chat`` / ``_transcribe_segment``
/ ``align_to_speech`` / tencent) returns SEGMENT-RELATIVE times — the offset
of the audio part within the whole media is always 0 inside a channel. The
only place relative times become wall-clock times is ``shift_to_wall``, called
exactly once per part in ``transcribe()`` after envelope validation. No other
code path may add an offset.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx

_log = logging.getLogger("deepread.transcribe")


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


def probe_stream_durations(media_path: Path) -> tuple[float, float]:
    """Per-stream (audio, video) durations from ffprobe.

    A container's ``format`` duration can mask a muxing bug where the audio
    stream is much longer than the video stream (observed: an mp4 whose audio
    stream ran 1481 s while video stopped at 760 s — every timeline built on
    the container duration drifted past the real picture).

    Returns 0.0 for a stream that is absent or unprobeable.
    """
    out: list[float] = []
    for sel in ("a", "v"):
        r = _run([
            "ffprobe", "-v", "error", "-select_streams", sel,
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
        ])
        vals: list[float] = []
        for tok in r.stdout.decode(errors="replace").split():
            try:
                v = float(tok)
            except ValueError:
                continue
            if math.isfinite(v) and v > 0:
                vals.append(v)
        out.append(max(vals) if vals else 0.0)
    return out[0], out[1]


def normalize_media_duration(media_path: Path) -> float:
    """Normalize A/V stream duration mismatch (>5%) and return the media
    duration every downstream stage must use.

    When the audio stream and the video stream disagree by more than 5%, the
    longer stream would stretch every derived timeline into dead time. The
    media is re-encoded once with ``-shortest`` (atomic replace) so the
    container, both streams and the player clock agree afterwards. Returns
    the normalized format duration, or the probed duration unchanged when the
    file is audio-only, unprobeable, or normalization fails (best-effort).
    """
    audio, video = probe_stream_durations(media_path)
    base = probe_duration(media_path)
    if audio <= 0 or video <= 0:
        return base                     # audio-only file or unprobeable
    if abs(audio - video) <= max(audio, video) * 0.05:
        return base                     # streams agree — nothing to fix
    tmp = media_path.with_suffix(".norm" + media_path.suffix)
    try:
        r = _run([
            "ffmpeg", "-y", "-i", str(media_path), "-shortest",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(tmp),
        ], timeout=3600)
        dur = probe_duration(tmp) if tmp.exists() else 0.0
        if r.returncode != 0 or dur <= 0:
            tmp.unlink(missing_ok=True)
            return base                 # normalization failed — keep original
        os.replace(tmp, media_path)
        _log.info("媒体 A/V 流时长不一致 (audio %.1fs / video %.1fs)，已用 -shortest 规范化为 %.1fs",
                  audio, video, dur)
        return dur
    finally:
        tmp.unlink(missing_ok=True)


def extract_audio(media_path: Path, out_wav: Path) -> Path:
    """16kHz mono wav — the format transcription APIs accept best."""
    r = _run([
        "ffmpeg", "-y", "-i", str(media_path), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav),
    ])
    if r.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"ffmpeg 音频提取失败: {r.stderr.decode(errors='replace')[-500:]}")
    return out_wav


def detect_silence_stretches(
    wav_path: Path, noise_db: str = "-30dB", min_silence: float = 0.4
) -> list[tuple[float, float]]:
    """(start, end) of every silence stretch, detected with ffmpeg.

    The raw stretches (not just midpoints) are what speech-region alignment
    needs: the complement of the silences is the actual speech timeline.
    """
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
    stretches: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else s + min_silence
        stretches.append((s, e))
    return stretches


def detect_silences(wav_path: Path, noise_db: str = "-30dB", min_silence: float = 0.4) -> list[float]:
    """Midpoints of silence stretches — natural cut points."""
    return [(s + e) / 2 for s, e in detect_silence_stretches(wav_path, noise_db, min_silence)]


def speech_spans(
    duration: float, silences: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Complement of the silence stretches within [0, duration] — the speech.

    Returns a sorted, non-overlapping list of (start, end) wall-clock spans in
    which audio is actually voiced. Music-only b-roll and intro/outro cards
    are indistinguishable from silence at -30dB, which is exactly what we want:
    both are stretches where nobody is talking.
    """
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if s > cursor:
            spans.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        spans.append((cursor, duration))
    return spans


def _speech_to_wall(spans: list[tuple[float, float]], t: float) -> float:
    """Map a position in cumulative-speech time back to wall-clock time.

    ``t`` counts only voiced seconds (0 = start of the first speech span).
    A value past the end of all speech clamps to the end of the last span.
    """
    remaining = t
    for s, e in spans:
        span = e - s
        if remaining <= span:
            return s + remaining
        remaining -= span
    return spans[-1][1] if spans else 0.0


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
    sentences: list[str], part_dur: float, max_sec: float = 7.0,
    max_chars: int | None = None,
) -> list[str]:
    """Break oversized sentences into subtitle-sized chunks.

    ASR text from models like SenseVoiceSmall often arrives with little or no
    punctuation, so one "sentence" can span a minute of speech. Subtitles need
    short lines, so anything longer than ~max_sec of speech is split at commas
    and then hard-wrapped by character budget. ``max_chars`` lets the caller
    precompute one global budget instead of a per-call one (unit-wise
    resplitting must use the same budget for every unit).
    """
    if not sentences or part_dur <= 0:
        return sentences
    if max_chars is None:
        total_chars = sum(len(s) for s in sentences) or 1
        cps = total_chars / part_dur             # characters per second
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


def _pause_positions(stretches: list[tuple[float, float]]) -> list[float]:
    """Candidate boundary positions from silence stretches.

    Short breaths (<1 s) contribute their midpoint — the boundary splits the
    pause between the two sentences. Longer stretches (music-only b-roll,
    scene changes) contribute a point near their END, so the sentence that
    follows a long non-speech region starts near its actual speech onset
    instead of in the middle of the music.
    """
    out: list[float] = []
    for s, e in stretches:
        out.append(max((s + e) / 2, e - 0.5))
    return out


def _dp_assign_blocks(
    sentences: list[str],
    positions: list[float],
    speech: list[tuple[float, float]],
    first_speech: float,
    last_speech: float,
    total_speech: float,
) -> list[float] | None:
    """Assign sentences to pause-delimited audio blocks with a global DP.

    Blocks are the regions between candidate pause positions (and the part's
    first/last speech onsets). The DP chooses how the sentence sequence is cut
    into consecutive groups and which group lands in which block, minimizing
    the squared deviation between each group's share of total speech time
    (from its char count) and the voiced time actually inside the block.

    Why a DP and not proportional-then-snap: a greedy target drifts past its
    pause and every following sentence inherits the error (+2-6 s observed).
    Why not per-sentence boundary snapping: much of real speech flows without
    any detectable pause, so many sentence boundaries have no pause to snap
    to — those sentences must share a block and be spread proportionally
    inside it, which the two-level scheme below does.

    A group may span ONE pause: real sentences can contain a >=0.4 s comma
    pause, so a sentence group that straddles two adjacent blocks is legal —
    it gets assigned the summed voiced time of both blocks and is laid out
    across their merged voiced spans. Blocks with no sentences (pure music
    b-roll) are simply skipped. Allowing this prevents the DP from wrongly
    merging two short groups into one long block just because one of them
    contains an internal pause.

    Returns the N-1 internal boundary wall times, or None when no usable
    positions exist (caller falls back to proportional spread).
    """
    n = len(sentences)
    chars = [len(s) for s in sentences]
    csum = [0]
    for c in chars:
        csum.append(csum[-1] + c)
    total_chars = csum[n] or 1

    pos = sorted(p for p in positions if first_speech < p < last_speech)
    if not pos:
        return None
    P = [first_speech] + pos + [last_speech]
    M = len(P) - 1  # number of blocks

    # per-block voiced spans/duration (silence clipped out)
    bspans: list[list[tuple[float, float]]] = []
    bdur: list[float] = []
    for m in range(M):
        a, b = P[m], P[m + 1]
        clipped: list[tuple[float, float]] = []
        t = 0.0
        for s, e in speech:
            lo, hi = max(a, s), min(b, e)
            if hi > lo:
                clipped.append((lo, hi))
                t += hi - lo
        bspans.append(clipped)
        bdur.append(t)

    # prefix voiced time up to each boundary — O(1) voiced-time lookup for any
    # run of consecutive blocks, needed by the cross-pause group transitions
    cum = [0.0]
    for t in bdur:
        cum.append(cum[-1] + t)

    def voiced_between(m1: int, m2: int) -> tuple[list[tuple[float, float]], float]:
        """Merged voiced spans and total duration over blocks m1..m2 inclusive."""
        if m1 == m2:
            return bspans[m1], bdur[m1]
        a, b = P[m1], P[m2 + 1]
        spans: list[tuple[float, float]] = []
        t = 0.0
        for s, e in speech:
            lo, hi = max(a, s), min(b, e)
            if hi > lo:
                spans.append((lo, hi))
                t += hi - lo
        return spans, t or cum[m2 + 1] - cum[m1]

    INF = float("inf")
    # dp[j][m]: first j sentences assigned to first m blocks, min cost
    dp = [[INF] * (M + 1) for _ in range(n + 1)]
    # bk[j][m] = (j2, span): last transition consumed sentences j2..j across
    # `span` blocks (span 0 = block m-1 left empty / pure music)
    bk: list[tuple[int, int] | None] = [[None] * (M + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for m in range(1, M + 1):
        for j in range(0, n + 1):
            best, bestk = INF, None
            # skip block m-1 entirely (no speech worth assigning). Skipping is
            # NOT free: any voiced time left unclaimed would otherwise be
            # silently swallowed by the previous sentence's span, shifting
            # every following boundary later (real-course regression: +6-9 s).
            # Charging bdur² makes the DP account for all speech while pure
            # music blocks (bdur ≈ 0) still skip for free.
            if dp[j][m - 1] < best:
                best, bestk = dp[j][m - 1] + bdur[m - 1] ** 2, (j, 0)
            # group j2..j-1 inside block m-1 alone
            for j2 in range(0, j + 1):
                prev = dp[j2][m - 1]
                if prev == INF:
                    continue
                gchars = csum[j] - csum[j2]
                c = prev + (gchars / total_chars * total_speech - bdur[m - 1]) ** 2
                if c < best:
                    best, bestk = c, (j2, 1)
            # group j2..j-1 straddling the pause between blocks m-2 and m-1
            if m >= 2:
                for j2 in range(0, j + 1):
                    prev = dp[j2][m - 2]
                    if prev == INF:
                        continue
                    gchars = csum[j] - csum[j2]
                    vdur = cum[m] - cum[m - 2]
                    # spanning costs a fixed penalty: char-share noise alone
                    # should not merge two groups that could each own a block
                    # (merging places the internal boundary mid-speech ~1s off,
                    # while splitting lands it exactly on the pause)
                    c = prev + (gchars / total_chars * total_speech - vdur) ** 2 + 0.5
                    if c < best:
                        best, bestk = c, (j2, 2)
            dp[j][m] = best
            bk[j][m] = bestk
    if dp[n][M] == INF:
        return None

    # backtrack the sentence->block groups, then lay each group's sentences
    # out proportionally by chars across the group's voiced time
    groups: list[tuple[int, int, int, int]] = []  # (first, last+1, blk_from, blk_to)
    j, m = n, M
    while m > 0:
        k = bk[j][m]
        if k is None:
            return None
        j2, span = k
        if span == 1:
            groups.append((j2, j, m - 1, m - 1))
        elif span == 2:
            groups.append((j2, j, m - 2, m - 1))
        if span == 0:
            m -= 1
        else:
            j, m = j2, m - span
    groups.reverse()

    starts: list[float] = []
    for i0, i1, m1, m2 in groups:
        spans, bs = voiced_between(m1, m2)
        gchars = csum[i1] - csum[i0]
        starts.append(P[m1])         # first sentence of the group opens the block
        acc = 0
        for t in range(i0, i1 - 1):  # internal boundaries only
            acc += chars[t]
            target = acc / gchars * bs if gchars and bs > 0 else 0.0
            starts.append(_speech_to_wall(spans, target))
    # n sentence starts (last one ends at last_speech via the caller)
    if len(starts) != n:
        return None
    return starts[1:]


def align_to_speech(
    part: Path,
    sentences: list[str],
    part_dur: float,
) -> list[dict[str, Any]]:
    """Give plain-text sentences real timestamps using detected speech pauses.

    Models like SenseVoiceSmall return text only, with no timing. We rebuild
    the timeline from the audio itself: silencedetect gives the voiced spans
    (music-only b-roll, intro cards and breath pauses all read as silence at
    -30 dB), sentence boundaries are assigned to pause positions by a global
    DP that matches each sentence's character share against the voiced time
    of its assigned segment, and the first/last sentence are pinned to the
    first/last speech onset.

    The returned times are SEGMENT-RELATIVE (part starts at 0). The caller
    converts to wall-clock times with ``shift_to_wall`` — never by hand.

    Two failure modes this replaces (both observed on real courses as a
    "ASR timestamps are ~2-6 s late" complaint):
      * proportional spread over raw WALL time spreads sentences across
        non-speech stretches, so drift compounds along the part;
      * a greedy local snap (proportional target -> nearest pause within a
        small tolerance) cannot recover once the target has drifted past the
        right pause — every following sentence inherits the error.
    """
    if not sentences:
        return []

    # silence stretches are the only reliable structural signal we have
    try:
        stretches = detect_silence_stretches(part) if part_dur >= 8 else []
    except Exception:  # noqa: BLE001 — alignment is best-effort
        stretches = []
    speech = speech_spans(part_dur, stretches) if stretches else []
    total_speech = sum(e - s for s, e in speech)

    # subtitle-length budgeting should use speech density, not wall density,
    # or non-speech stretches shrink the per-line char budget and over-split
    sentences = resplit_for_subtitles(
        sentences, total_speech if total_speech > 0 else part_dur
    )
    if not sentences:
        return []
    if len(sentences) == 1 or part_dur <= 0:
        text = sentences[0]
        return [{
            "start": speech[0][0] if speech else 0.0,
            "end": max(speech[-1][1], 0.2) if speech else max(part_dur, 0.2),
            "text": text, "lang": detect_lang(text), "source": "asr",
        }]

    bounds: list[float] = []
    if speech and total_speech > 0:
        first_speech = speech[0][0]
        last_speech = speech[-1][1]
        positions = _pause_positions(stretches) if stretches else []
        dp_bounds = None
        if any(first_speech < p < last_speech for p in positions):
            dp_bounds = _dp_assign_blocks(
                sentences, positions, speech,
                first_speech, last_speech, total_speech,
            )
        if dp_bounds is not None and len(dp_bounds) == len(sentences) - 1:
            bounds = dp_bounds
        else:
            # pauses exist but none usable (e.g. only intro/outro silence):
            # spread boundaries over speech time proportionally
            total_chars = sum(len(s) for s in sentences) or 1
            acc = 0
            for s in sentences[:-1]:
                acc += len(s)
                bounds.append(_speech_to_wall(speech, acc / total_chars * total_speech))
    else:
        # no usable speech map — legacy proportional spread over wall time
        total_chars = sum(len(s) for s in sentences) or 1
        acc = 0
        for s in sentences[:-1]:
            acc += len(s)
            bounds.append(acc / total_chars * part_dur)

    # first sentence starts where speech starts (not at the part head), and the
    # last one ends where speech ends — intro/outro silence belongs to nobody
    first_edge = speech[0][0] if speech else 0.0
    last_edge = speech[-1][1] if speech else part_dur
    edges = [first_edge] + bounds + [last_edge]
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
            "start": start,
            "end": end,
            "text": text,
            "lang": detect_lang(text),
            "source": "asr",
        })
    return segs


def shift_to_wall(
    segments: list[dict[str, Any]], part_start: float
) -> list[dict[str, Any]]:
    """THE single conversion point from segment-relative to absolute time.

    Every channel returns part-relative times (offset invariant); after
    envelope validation the caller shifts them onto the media clock here and
    nowhere else.
    """
    for s in segments:
        s["start"] = float(s.get("start", 0) or 0) + part_start
        s["end"] = float(s.get("end", 0) or 0) + part_start
    return segments


def _envelope_violations(
    segments: list[dict[str, Any]], part_dur: float
) -> list[int]:
    """Indices of segments that violate the part envelope.

    Envelope (A1): 0 <= start <= end <= part_dur + 1 s, values finite. A
    segment outside it is garbage from the provider (mis-parsed timestamps,
    wrong sample-rate math, clock overflow) and must never reach the fusion
    layer — not even clamped.
    """
    limit = part_dur + 1.0
    bad: list[int] = []
    for i, s in enumerate(segments):
        try:
            start = float(s.get("start", 0) or 0)
            end = float(s.get("end", 0) or 0)
        except (TypeError, ValueError):
            bad.append(i)
            continue
        if not (math.isfinite(start) and math.isfinite(end)):
            bad.append(i)
            continue
        # -0.05 tolerates provider float noise; anything earlier is garbage
        if start < -0.05 or end <= start or start > limit or end > limit:
            bad.append(i)
    return bad


def validate_timeline(
    segments: list[dict[str, Any]], duration: float
) -> tuple[bool, str]:
    """End-to-end timestamp gate (A4) — the LAST check before anything is
    written to disk.

    Asserts the fused timeline sits on the real media clock:
      * no negative starts, no non-finite values;
      * every segment has start < end and starts within duration + 1 s;
      * segments are monotonic and overlap by at most 0.25 s;
      * sentence time covers >= 80% of the media duration (a collapsed or
        mostly-dropped timeline is a broken pipeline, not a sparse one).

    Returns (ok, reason). ``duration <= 0`` skips the duration-dependent
    checks (nothing to compare against).
    """
    if not segments:
        return False, "时间线为空（没有任何句子）"
    prev_end = 0.0
    covered = 0.0
    for i, s in enumerate(segments):
        try:
            start = float(s.get("start", 0) or 0)
            end = float(s.get("end", 0) or 0)
        except (TypeError, ValueError):
            return False, f"第 {i} 句时间字段无法解析: {s.get('start')!r}/{s.get('end')!r}"
        if not (math.isfinite(start) and math.isfinite(end)):
            return False, f"第 {i} 句时间为非有限值 ({start}, {end})"
        if start < -0.05:
            return False, f"第 {i} 句起点为负 ({start:.2f}s)"
        if end <= start:
            return False, f"第 {i} 句起点不小于终点 ({start:.2f} >= {end:.2f})"
        if duration > 0 and start > duration + 1.0:
            return False, (
                f"第 {i} 句起点超出媒体时长 ({start:.2f}s > {duration:.2f}s)"
            )
        if start < prev_end - 0.25:
            return False, (
                f"第 {i} 句与前句重叠超过 0.25s (start {start:.2f} < prev_end {prev_end:.2f})"
            )
        prev_end = max(prev_end, end)
        if duration > 0:
            covered += max(0.0, min(end, duration) - max(start, 0.0))
        else:
            covered += end - start
    if duration > 0:
        coverage = covered / duration
        if coverage < 0.8:
            return False, (
                f"句子时间覆盖率不足 ({coverage:.0%} < 80%) —— 时间线可能塌缩或大量丢句"
            )
    return True, ""


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


def _parse_native_segments(output: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn word-level timestamps into sentence segments.

    The native endpoint always returns ONE ``sentence`` object holding the whole
    transcript plus a ``words`` array (per-word begin/end in ms). We regroup
    words into sentences at punctuation and at long pauses, so subtitle timing
    is real audio timing rather than a guess.

    Returns SEGMENT-RELATIVE times (the part starts at 0) — offset invariant.
    """
    sent = output.get("sentence") or {}
    words = sent.get("words") or []
    if not words:
        text = (sent.get("text") or output.get("text") or "").strip()
        if not text:
            return []
        return [{
            "start": float(sent.get("begin_time", 0)) / 1000.0,
            "end": float(sent.get("end_time", 0)) / 1000.0,
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
                "start": float(real[0].get("begin_time", 0)) / 1000.0,
                "end": float(real[-1].get("end_time", 0)) / 1000.0,
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
) -> list[dict[str, Any]]:
    """Native DashScope ASR — returns precise word/sentence timestamps.

    Times are SEGMENT-RELATIVE (offset invariant).
    """
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
    return _parse_native_segments(r.json().get("output") or {})


def _transcribe_via_chat(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    part: Path,
) -> list[dict[str, Any]]:
    """Transcribe an audio segment through chat completions with input_audio.

    No native timestamps — we ask the model for line-per-sentence output and
    align the lines onto detected pauses. Returns SEGMENT-RELATIVE times.
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
    return align_to_speech(part, sentences, probe_duration(part))


def _transcribe_segment(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    part: Path,
    language: str | None,
    mode: str = "api",
) -> list[dict[str, Any]]:
    """Dispatch by mode:
      native -> DashScope native ASR (precise timestamps)
      chat   -> chat completions + input_audio (omni models, no timestamps)
      api    -> /v1/audio/transcriptions (whisper-style)

    All modes return SEGMENT-RELATIVE times (offset invariant).
    """
    if mode == "native":
        return _transcribe_via_native(client, base_url, api_key, model, part)
    if mode == "chat":
        return _transcribe_via_chat(client, base_url, api_key, model, part)
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
                "start": float(seg.get("start", 0)),
                "end": float(seg.get("end", 0)),
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
    return align_to_speech(part, sentences, probe_duration(part))


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


def sanitize_segments(
    segments: list[dict[str, Any]], duration: float
) -> list[dict[str, Any]]:
    """Drop or clamp segments that cannot sit on the real media timeline.

    Any provider (native word timestamps, mis-probed wav headers, buggy
    offsets) can hand back negative starts or times beyond the media length —
    both are garbage that poisons the timeline ("sentences at -90 minutes" or
    "1444:50" in a 12-minute video). Rules:
      * non-finite values (NaN/inf) are dropped;
      * starts before -0.25 s (beyond float noise) are dropped;
      * starts at/after duration + 5 s are dropped (existing guard);
      * everything else is clamped into [-0.0, duration + 1];
      * empty/degenerate spans are dropped.
    """
    out: list[dict[str, Any]] = []
    for s in segments:
        start = float(s.get("start", 0) or 0)
        end = float(s.get("end", 0) or 0)
        if not (math.isfinite(start) and math.isfinite(end)):
            continue          # NaN/inf would poison every comparison downstream
        if start < -0.25:
            continue
        if end <= start + 0.01:
            continue          # inverted/degenerate span — unusable
        if duration > 0:
            if start >= duration + 5:
                continue
            start = min(max(start, 0.0), duration + 1)
            end = min(max(end, start + 0.05), duration + 1)
        else:
            start = max(start, 0.0)
            end = max(end, start + 0.05)
        if not str(s.get("text") or "").strip():
            continue
        out.append({**s, "start": start, "end": end})
    return out


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

    def _call_channel(i: int) -> list[dict[str, Any]]:
        """Channel layer only: returns SEGMENT-RELATIVE times for part i."""
        if len(selected_models) > 1 and mode != "tencent":
            from pipeline.lesson import audio_transcribe_with_failover
            segs, _used = audio_transcribe_with_failover(cfg, parts[i])
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
                if not s.get("lang"):
                    s["lang"] = detect_lang(s["text"])
            from pipeline.lesson import note_model_result
            note_model_result(prov.get("provider_id", ""), model, True)
            return segs
        with httpx.Client(timeout=httpx.Timeout(connect=15, read=300, write=120, pool=15)) as client:
            try:
                segs = _transcribe_segment(client, base_url, api_key, model, parts[i], None, mode)
            except Exception as exc:  # noqa: BLE001 — record the mark, then fail
                from pipeline.lesson import note_model_result
                note_model_result(prov.get("provider_id", ""), model, False, str(exc)[:160])
                raise
            from pipeline.lesson import note_model_result
            note_model_result(prov.get("provider_id", ""), model, True)
            return segs

    def _transcribe_one(i: int) -> list[dict[str, Any]]:
        """Channel call + envelope validation + THE wall-clock conversion.

        Per-part envelope check (A1): every segment must satisfy
        0 <= start <= end <= part_dur + 1 s. Violations trigger exactly one
        full-part retry (transient provider glitches are common, structural
        ones are not); if violations persist, the offending segments are
        DROPPED and logged — never passed on to poison the timeline.
        """
        if should_cancel and should_cancel():
            raise PipelineCancelled()
        offset = part_starts[i] if i < len(part_starts) else i * seg_secs
        part_dur = probe_duration(parts[i])

        segs = _call_channel(i)
        viol = _envelope_violations(segs, part_dur)
        if viol:
            msg = f"分段 {i} 包络校验失败 {len(viol)}/{len(segs)} 段，重试一次…"
            _log.warning(msg)
            if progress_cb:
                progress_cb((i + 1) / max(1, len(parts)), msg)
            segs2 = _call_channel(i)
            viol2 = _envelope_violations(segs2, part_dur)
            if len(viol2) < len(viol):
                segs, viol = segs2, viol2
        if viol:
            msg = (f"分段 {i} 重试后仍有 {len(viol)} 段越界 "
                   f"({[round(float(segs[j].get('start', 0) or 0), 2) for j in viol[:5]]}…)，已丢弃")
            _log.warning(msg)
            if progress_cb:
                progress_cb((i + 1) / max(1, len(parts)), msg)
            bad = set(viol)
            segs = [s for j, s in enumerate(segs) if j not in bad]
        # offset invariant: this is the ONLY relative -> absolute conversion
        return shift_to_wall(segs, offset)

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

    # guard against offset misalignment / bogus provider timestamps: any
    # segment that lands past the media duration or before its start is
    # garbage and would poison the timeline ("sentences at 52 minutes" or
    # negative times in a short video)
    duration = probe_duration(wav)
    all_segments = sanitize_segments(all_segments, duration)
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
