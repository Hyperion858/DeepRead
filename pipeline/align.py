"""Align & merge the ASR transcript with embedded subtitles.

Strategy: ASR timeline is the backbone (it covers the whole audio). Embedded
subtitle lines are mapped onto ASR segments by time overlap; where they agree,
the embedded text (usually human-authored) wins; where they disagree strongly,
keep ASR text and flag `uncertain` so the UI/lesson layer can double-check.
Also merges fragmented ASR segments into sentence-level units.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def _norm(text: str) -> str:
    """Lowercase, strip punctuation/whitespace — for fuzzy comparison."""
    return re.sub(r"[\s\W_]+", "", text.lower())


def _similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def merge_asr_fragments(segments: list[dict[str, Any]], max_dur: float = 8.0, max_chars: int = 200) -> list[dict[str, Any]]:
    """Merge consecutive same-language ASR fragments into readable sentences.

    Heuristics: break after terminal punctuation; otherwise pack until
    duration/char limits. Language switches always force a break.
    """
    merged: list[dict[str, Any]] = []
    buf: dict[str, Any] | None = None
    for seg in segments:
        text = seg["text"]
        if buf is None:
            buf = dict(seg)
            continue
        same_lang = buf["lang"] == seg["lang"]
        dur = seg["end"] - buf["start"]
        can_merge = (
            same_lang
            and dur <= max_dur
            and len(buf["text"]) + len(text) <= max_chars
            and not re.search(r"[.。?!?!，,]$", buf["text"].strip())
        )
        if can_merge:
            joiner = "" if buf["text"].endswith(("'", "-")) else " "
            if buf["lang"] == "zh":
                joiner = ""
            buf["text"] = buf["text"] + joiner + text
            buf["end"] = seg["end"]
        else:
            merged.append(buf)
            buf = dict(seg)
    if buf:
        merged.append(buf)
    return merged


def split_long_segment(seg: dict[str, Any], max_chars: int = 120) -> list[dict[str, Any]]:
    """Split very long segments (e.g. plain-text fallback covering minutes)
    into roughly even sub-segments so the reading pane stays usable."""
    text = seg["text"]
    if len(text) <= max_chars:
        return [seg]
    # split on sentence punctuation, then repack
    pieces = re.split(r"(?<=[.。?!?!])\s*", text)
    pieces = [p for p in pieces if p.strip()]
    out: list[dict[str, Any]] = []
    dur = seg["end"] - seg["start"]
    total = sum(len(p) for p in pieces) or 1
    t = seg["start"]
    buf_text = ""
    buf_start = t
    consumed = 0
    for p in pieces:
        buf_text += (" " if buf_text and seg["lang"] == "en" else "") + p
        consumed += len(p)
        if len(buf_text) >= max_chars:
            sub_end = seg["start"] + dur * consumed / total
            out.append({**seg, "start": buf_start, "end": sub_end, "text": buf_text.strip()})
            buf_text, buf_start = "", sub_end
    if buf_text.strip():
        out.append({**seg, "start": buf_start, "end": seg["end"], "text": buf_text.strip()})
    return out


def align(asr_segments: list[dict[str, Any]], embedded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # normalize + merge ASR fragments first
    asr = merge_asr_fragments(asr_segments)
    asr = [s for s in (split_long_segment(s) for s in asr) for s in s]

    if not embedded:
        for s in asr:
            s["uncertain"] = False
        return asr

    used_embedded: set[int] = set()
    results: list[dict[str, Any]] = []

    for seg in asr:
        s_start, s_end = seg["start"], seg["end"]
        span = max(s_end - s_start, 0.1)
        # embedded lines overlapping this ASR segment by >30% of their own duration
        overlapping = []
        for i, sub in enumerate(embedded):
            ov_start = max(s_start, sub["start"])
            ov_end = min(s_end, sub["end"])
            ov = ov_end - ov_start
            sub_dur = max(sub["end"] - sub["start"], 0.1)
            if ov > 0 and ov / sub_dur > 0.3:
                overlapping.append((i, sub))
        overlapping.sort(key=lambda x: x[1]["start"])

        if not overlapping:
            results.append({**seg, "source": "asr", "uncertain": False})
            continue

        sub_text = " ".join((sub["text"] if sub["lang"] == "en" else sub["text"])
                            for _, sub in overlapping)
        for i, _ in overlapping:
            used_embedded.add(i)

        sim = _similarity(seg["text"], sub_text)
        if sim > 0.75:
            # agree: embedded text is human-authored, prefer it
            results.append({
                **seg,
                "text": sub_text,
                "source": "embedded",
                "uncertain": False,
            })
        elif sim > 0.4:
            # partial agreement: prefer embedded but flag for review
            results.append({
                **seg,
                "text": sub_text,
                "source": "embedded",
                "uncertain": True,
            })
        else:
            # strong disagreement: keep ASR, flag
            results.append({**seg, "source": "asr", "uncertain": True})

    # embedded lines that matched no ASR segment (e.g. ASR dropped them): append
    orphans = [embedded[i] for i in range(len(embedded)) if i not in used_embedded]
    for sub in orphans:
        if _norm(sub["text"]):
            results.append({
                "start": sub["start"],
                "end": sub["end"],
                "text": sub["text"],
                "lang": sub.get("lang") or "en",
                "source": "embedded",
                "uncertain": False,
            })

    results.sort(key=lambda x: x["start"])
    return results
