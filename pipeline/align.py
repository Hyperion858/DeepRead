"""Align & merge the ASR transcript with embedded subtitles.

Strategy: ASR timeline is the backbone (it covers the whole audio). Embedded
subtitle lines are mapped onto ASR segments by time overlap; where they agree,
the embedded text (usually human-authored) wins; where they disagree strongly,
keep ASR text and flag `uncertain` so the UI/lesson layer can double-check.
Also merges fragmented ASR segments into sentence-level units.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

import httpx

from pipeline.transcribe import detect_lang


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


def _warp_asr_to_cues(asr: list[dict[str, Any]], cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warp ASR segment times onto cue times.

    When embedded/OCR cues carry reliable timing (frame sampling for OCR,
    authoring for embedded tracks), ASR's evenly-spread estimates within a
    long window are the weaker clock. This maps each ASR segment matched to
    a cue onto the cue's real interval, then re-flows unmatched segments
    between anchored points.
    """
    # build anchor list: (asr_idx, cue_start, cue_end) matched by similarity
    anchors: list[tuple[int, float, float]] = []
    used_cues: set[int] = set()
    for ai, seg in enumerate(asr):
        best_i, best_sim = None, 0.0
        for ci, cue in enumerate(cues):
            if ci in used_cues:
                continue
            s = _similarity(seg["text"], cue["text"])
            if s > best_sim:
                best_sim, best_i = s, ci
        if best_i is not None and best_sim > 0.5:
            used_cues.add(best_i)
            anchors.append((ai, cues[best_i]["start"], cues[best_i]["end"]))
    anchors.sort(key=lambda a: a[0])

    out: list[dict[str, Any]] = [None] * len(asr)
    # place anchored segments at cue times
    for ai, cs, ce in anchors:
        seg = asr[ai]
        dur = max(seg["end"] - seg["start"], 0.1)
        cs = max(cs, 0.0)
        out[ai] = {
            **seg,
            "start": cs,
            "end": max(cs + dur * 0.6, ce) if ce - cs < dur else ce,
            "source": seg.get("source", "asr"),
            "aligned_to_cue": True,
        }
    # re-flow unmatched segments in the gaps between anchored ones
    n = len(asr)
    tail_cursor: float | None = None  # sequential placement for tail runs
    for idx in range(n):
        if out[idx] is not None:
            tail_cursor = None
            continue
        prev_anchor = next((a for a in anchors if a[0] < idx), None)
        next_anchor = next((a for a in anchors if a[0] > idx), None)
        lo = prev_anchor[2] if prev_anchor else (asr[idx]["start"] if not next_anchor else 0.0)
        hi = next_anchor[1] if next_anchor else None
        seg = asr[idx]
        dur = max(seg["end"] - seg["start"], 0.1)
        # anchors are matched by text similarity, so cue times may arrive out
        # of order relative to asr order — a non-positive gap would collapse
        # or negate positions. Fall back to sequential tail placement instead.
        if hi is not None and hi <= lo:
            hi = None
        if hi is None:
            # no right anchor (leading run, tail run or inverted gap): lay
            # segments sequentially by their own durations instead of dumping
            # them all onto one instant (observed: 6 sentences sharing
            # identical times) or onto a negative base
            if tail_cursor is None:
                tail_cursor = max(lo, 0.0)
            new_start = tail_cursor
            tail_cursor = new_start + dur
        else:
            # spread unmatched segments evenly in [lo, hi)
            gap = hi - lo
            # count consecutive unmatched segments from idx
            run = 0
            k = idx
            while k < n and out[k] is None:
                run += 1
                k += 1
            before = idx - (prev_anchor[0] + 1 if prev_anchor else 0)
            new_start = max(lo + gap * (before / max(run, 1)), 0.0)
        out[idx] = {
            **seg,
            "start": new_start,
            "end": new_start + dur,
        }
    result = [x for x in out if x]
    result.sort(key=lambda x: x["start"])
    return result


def _split_bilingual(text: str) -> tuple[str, str]:
    """Split a bilingual caption into (main_text, secondary_text).

    Douyin vlogs burn EN+ZH two-line subtitles; the two languages must be
    separated for study (main) and free translation (secondary). Runs of the
    same script are grouped; the dominant script is the main text, everything
    of the other script joins the secondary text. Either side may be empty.
    """
    runs: list[tuple[str, str]] = []   # (lang, text)
    cur_lang, cur = None, ""
    for ch in text:
        lang = "zh" if "一" <= ch <= "鿿" else ("en" if (ch.isascii() and ch.isalpha()) else None)
        if lang is None:            # punctuation/space stays with current run
            cur += ch
            continue
        if lang != cur_lang:
            if cur_lang and cur.strip():
                runs.append((cur_lang, cur))
            cur_lang, cur = lang, ""
        cur += ch
    if cur_lang and cur.strip():
        runs.append((cur_lang, cur))
    if not runs:
        return text.strip(), ""
    # dominant language = most text overall; keep ALL its runs in order
    # (English often gets split by the Chinese translation line)
    totals: dict[str, int] = {}
    for lang, run in runs:
        totals[lang] = totals.get(lang, 0) + len(run.strip())
    best_lang = max(totals, key=totals.get)
    main = " ".join(run.strip() for lang, run in runs if lang == best_lang)
    secondary = " ".join(run.strip() for lang, run in runs if lang != best_lang)
    return main, secondary


def align(asr_segments: list[dict[str, Any]], embedded: list[dict[str, Any]],
          align_base: str = "asr", cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    # normalize + merge ASR fragments first
    asr = merge_asr_fragments(asr_segments)
    asr = [s for s in (split_long_segment(s) for s in asr) for s in s]

    if not embedded:
        for s in asr:
            s["uncertain"] = False
        return asr

    # bilingual burned-in captions (EN+ZH douyin style) poison every
    # comparison — split into (main, secondary); secondary doubles as the
    # free translation so the translate step can skip the LLM
    split_subs = []
    for sub in embedded:
        main, secondary = _split_bilingual(sub["text"])
        if not _norm(main):
            continue
        split_subs.append({**sub, "text": main, "caption_translation": secondary,
                           "lang": detect_lang(main)})
    embedded = split_subs

    # caption-only path: no ASR at all, cues ARE the timeline (accurate times)
    if not asr:
        results = []
        for i, sub in enumerate(embedded):
            if _norm(sub["text"]):
                results.append({
                    "start": sub["start"],
                    "end": sub["end"],
                    "text": sub["text"],
                    "lang": sub.get("lang") or "en",
                    "source": sub.get("source", "embedded"),
                    "uncertain": False,
                })
        results.sort(key=lambda x: x["start"])
        return results

    # when cues have trustworthy timing, warp the ASR timeline onto them
    asr = _warp_asr_to_cues(asr, embedded)

    used_embedded: set[int] = set()
    results: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []   # handed to the LLM arbiter after the loop

    # the baseline channel wins text disagreements. Default asr: caption text
    # is usually human-authored so strong agreement takes it. ocr/embedded as
    # baseline: keep the caption text unless ASR clearly carries more content.
    base_is_caption = align_base in ("ocr", "embedded")

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
        asr_n, sub_n = _norm(seg["text"]), _norm(sub_text)
        if base_is_caption:
            # caption is the authority: take its text whenever it carries
            # comparable content; flag when ASR disagrees noticeably
            if len(sub_n) >= len(asr_n) * 0.5 or sim > 0.5:
                results.append({
                    **seg,
                    "text": sub_text,
                    "source": "caption",
                    "uncertain": sim <= 0.4,
                })
            else:
                # caption too fragmentary to serve — fall back to ASR text
                results.append({**seg, "source": "asr", "uncertain": True})
        elif sim > 0.75:
            # strong agreement: caption text is human-authored, take it whole
            results.append({
                **seg,
                "text": sub_text,
                "source": "embedded",
                "uncertain": False,
            })
        elif sub_n and asr_n.startswith(sub_n):
            # caption is a prefix of the ASR text: OCR caught the caption
            # mid-fade or the caption summarizes — ASR is the full sentence
            results.append({**seg, "source": "asr", "uncertain": False})
        elif asr_n and sub_n.startswith(asr_n) and len(sub_n) > len(asr_n):
            # ASR is a prefix of the caption: ASR missed the tail, the burned
            # caption carries it — take the caption
            results.append({
                **seg,
                "text": sub_text,
                "source": "embedded",
                "uncertain": False,
            })
        elif sim > 0.4 and len(sub_n) >= len(asr_n) * 0.6:
            # partial agreement: genuinely ambiguous — hand it to the LLM
            # arbiter (batched after the loop); till then keep ASR, flag it.
            # NOTE: the arbiter must update the result DICT OBJECT, not an
            # index — `results` is re-sorted before the arbiter runs, which
            # invalidates append-time indices (observed: the arbitrated text
            # overwrote a random other sentence while the original stayed,
            # producing duplicate sentences with identical times)
            slot = {**seg, "source": "asr", "uncertain": True}
            ambiguous.append({
                "asr": seg["text"],
                "caption": sub_text,
                "slot": slot,
                "seg": seg,
                "sub_text": sub_text,
            })
            results.append(slot)
        else:
            # weak match: keep the ASR text (more complete) but BORROW the
            # caption's timing when the warp didn't anchor this segment —
            # never let a fragmentary caption degrade the text.
            if overlapping and not seg.get("aligned_to_cue"):
                first = overlapping[0][1]
                last = overlapping[-1][1]
                results.append({
                    **seg,
                    "start": min(seg["start"], first["start"]),
                    "end": max(seg["end"], last["end"]),
                    "source": "asr",
                    "uncertain": True,
                })
            else:
                results.append({**seg, "source": "asr", "uncertain": True})

    # embedded lines that matched no ASR segment (e.g. ASR dropped them): append
    orphans = [embedded[i] for i in range(len(embedded)) if i not in used_embedded]
    for sub in orphans:
        if _norm(sub["text"]):
            results.append({
                "start": sub["start"],
                "end": sub["end"],
                "text": sub["text"],
                "lang": sub.get("lang") or detect_lang(sub["text"]),
                "caption_translation": sub.get("caption_translation", ""),
                "source": "embedded",
                "uncertain": False,
            })

    results.sort(key=lambda x: x["start"])

    # LLM arbiter for the genuinely ambiguous middle band (0.4 < sim <= 0.75):
    # one batched chat call merges ASR + caption per sentence with context —
    # far better than char-level rules for homophone vs. typo errors
    if ambiguous:
        _arbiter_resolve(ambiguous, results, cfg)

    # keep the caption-provided translation when a sentence carries one and
    # re-derive lang from the final text (post-split / post-merge)
    for seg in results:
        ct = seg.pop("caption_translation", "") or ""
        if ct and not seg.get("translation"):
            seg["translation"] = ct
        seg["lang"] = detect_lang(seg["text"])
    results.sort(key=lambda x: x["start"])

    # defensive dedupe: identical time+text pairs would render as doubled
    # subtitle lines; any residue from the best-effort merge paths is junk
    seen: set[tuple[float, float, str]] = set()
    deduped: list[dict[str, Any]] = []
    for seg in results:
        key = (round(seg["start"], 2), round(seg["end"], 2), _norm(seg["text"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(seg)
    return deduped


def _arbiter_prompt(ambiguous: list[dict[str, Any]]) -> str:
    items = []
    for k, a in enumerate(ambiguous):
        items.append(f'{{"id": {k}, "asr": {json.dumps(a["asr"], ensure_ascii=False)}, '
                     f'"caption": {json.dumps(a["caption"], ensure_ascii=False)}}}')
    return (
        "以下是同一段视频里两路识别结果不一致的句子。asr 是语音转写（完整但同音词易错），"
        "caption 是画面烧录字幕（人工定稿但可能残缺/截断）。请逐条给出最可能的原始文本。\n"
        "规则：两者高度重合时合并出最通顺的原文；caption 是 asr 的截断时取 asr；"
        "asr 明显漏词时以 caption 为准补全；无法判断时取更完整、更符合语境的那个。\n"
        '输出严格 JSON 数组：[{"id": 0, "text": "..."}, ...]，不要解释。\n\n'
        "[" + ",\n".join(items) + "]"
    )


def _arbiter_resolve(ambiguous: list[dict[str, Any]], results: list[dict[str, Any]],
                     cfg: dict[str, Any] | None = None) -> None:
    """Merge ASR/caption for ambiguous sentences via one chat call (best-effort)."""
    if not cfg:
        return
    try:
        from pipeline.lesson import chat_with_failover, _extract_json
    except ImportError:
        return
    prompt = _arbiter_prompt(ambiguous)
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=15, read=120, write=30, pool=15)) as client:
            reply, _used = chat_with_failover(cfg, [{"role": "user", "content": prompt}],
                                              max_tokens=4000, temperature=0)
    except Exception:  # noqa: BLE001 — arbiter is opportunistic; keep pre-arbitration text
        return
    try:
        parsed = _extract_json(reply)
        if not isinstance(parsed, list):
            return
        for item in parsed:
            try:
                k = int(item.get("id"))
                text = str(item.get("text") or "").strip()
            except (ValueError, TypeError, AttributeError):
                continue
            if not text or k < 0 or k >= len(ambiguous):
                continue
            a = ambiguous[k]
            merged = _similarity(text, a["asr"])
            if merged >= 0.3:   # sanity: the arbiter shouldn't invent new content
                slot = a["slot"]
                slot.clear()
                slot.update({
                    **a["seg"],
                    "text": text,
                    "source": "arbitrated",
                    "uncertain": False,
                })
    except Exception:  # noqa: BLE001
        return
