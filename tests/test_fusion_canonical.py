"""Fusion canonicalization tests — the live gate failures (farm 20261126164116,
lawyer 20261128164307) reproduced with stub channel data.

Regression targets:
  * canonicalize_timeline: stable sort, cross-channel duplicate merge,
    distinct-overlap trimming, 0:00 title-orphan case;
  * align() source fixes: borrowed caption end clamped to the next ASR
    sentence, orphan caption dropped when it duplicates an emitted sentence;
  * end-to-end: farm-shaped channel data (ASR backbone + scattered captions)
    must fuse into a timeline that passes validate_timeline.

Run:  python -m pytest tests/test_fusion_canonical.py -v
  or: python tests/test_fusion_canonical.py
"""
from __future__ import annotations

import random
import sys
from typing import Any

import pipeline.align as al
import pipeline.transcribe as tr


def _seg(start: float, end: float, text: str, source: str = "asr") -> dict[str, Any]:
    return {"start": start, "end": end, "text": text, "source": source,
            "lang": tr.detect_lang(text)}


def _clean(segs: list[dict[str, Any]], tol: float = 0.25) -> None:
    """Assert the canonical invariant: sorted, no overlap beyond tol."""
    ordered = sorted(segs, key=lambda s: s["start"])
    assert [s["start"] for s in ordered] == [s["start"] for s in segs], "not sorted"
    for a, b in zip(segs, segs[1:]):
        assert b["start"] >= a["end"] - tol, (
            f"overlap {b['start']:.2f} < prev_end {a['end']:.2f}: "
            f"{a['text']!r} / {b['text']!r}"
        )


# --------------------------------------------------------------------------
# canonicalize_timeline
# --------------------------------------------------------------------------

def test_canonicalize_sorts_stably_including_zero_entries():
    """Out-of-order fusion residue (e.g. a 0:00 entry appended last) must come
    out sorted; equal starts keep their input order (stable)."""
    segs = [
        _seg(10.0, 12.0, "second"),
        _seg(0.6, 4.49, "opening line"),
        _seg(0.0, 0.5, "title card", source="OCR"),
        _seg(5.0, 8.0, "third"),
    ]
    out = al.canonicalize_timeline(segs)
    assert [s["text"] for s in out] == ["title card", "opening line", "third", "second"]
    _clean(out)


def test_canonicalize_merges_cross_channel_duplicate():
    """The same sentence from two channels (sim > 0.6) collapses into the more
    authoritative entry, span merged — never two copies of one sentence."""
    segs = [
        _seg(0.0, 4.49, "大家好我是律师张三", source="asr"),
        _seg(0.0, 4.49, "大家好我是律师张三", source="embedded"),
        _seg(5.0, 9.0, "今天讲合同纠纷", source="asr"),
    ]
    out = al.canonicalize_timeline(segs)
    assert len(out) == 2
    assert out[0]["source"] == "embedded"      # higher weight wins the text
    assert out[0]["start"] == 0.0 and out[0]["end"] == 4.49
    _clean(out)


def test_canonicalize_duplicate_keeps_longer_text_on_equal_weight():
    segs = [
        _seg(0.0, 3.0, "the same short line", source="OCR"),
        _seg(0.2, 3.5, "the same short line plus a tail", source="OCR"),
    ]
    out = al.canonicalize_timeline(segs)
    assert len(out) == 1
    assert out[0]["text"] == "the same short line plus a tail"


def test_canonicalize_trims_distinct_overlap():
    """Two genuinely different sentences overlapping > 0.25 s: the boundary of
    the less valuable entry is trimmed; both texts survive when possible."""
    segs = [
        _seg(20.0, 30.76, "a fairly long spoken sentence about contract law", source="asr"),
        _seg(23.72, 28.0, "sponsor", source="OCR"),
    ]
    out = al.canonicalize_timeline(segs)
    _clean(out)
    texts = {s["text"] for s in out}
    assert "a fairly long spoken sentence about contract law" in texts


def test_canonicalize_drops_swallowed_entry_and_passes_gate():
    """A caption fully inside a long ASR sentence with a dissimilar text is
    dropped (trimming it below 0.2 s), and the result passes the A4 gate."""
    segs = [
        _seg(0.0, 30.0, "one very long spoken sentence covering the intro" * 2, source="asr"),
        _seg(5.0, 6.0, "LOGO", source="OCR"),
        _seg(30.0, 40.0, "next sentence here", source="asr"),
    ]
    out = al.canonicalize_timeline(segs)
    assert all(s["text"] != "LOGO" for s in out)
    ok, reason = tr.validate_timeline(out, 40.0)
    assert ok, reason


def test_canonicalize_clamps_negative_and_drops_degenerate():
    segs = [
        _seg(-3.0, 2.0, "negative start"),
        _seg(5.0, 5.0, "degenerate"),
        _seg(4.0, 6.0, "fine"),
    ]
    out = al.canonicalize_timeline(segs)
    assert [s["text"] for s in out] == ["negative start", "fine"]
    assert out[0]["start"] == 0.0


# --------------------------------------------------------------------------
# align() source fixes (cfg=None -> no LLM arbiter, pure rule path)
# --------------------------------------------------------------------------

def test_align_borrowed_caption_end_is_clamped_to_next_sentence():
    """farm case: a sponsor caption spanning two ASR sentences must not lend
    its end (30.76) to the first sentence any more — the borrow is clamped to
    the next ASR start."""
    asr = [
        _seg(20.0, 25.0, "the quick brown fox jumps over the fence"),
        _seg(25.5, 30.0, "lazy dog sleeps under the old oak tree"),
    ]
    captions = [_seg(22.0, 30.76, "SUPERMARKET DISCOUNT", source="OCR")]
    out = al.align(asr, captions, cfg=None)
    _clean(out)
    first = next(s for s in out if "fox" in s["text"])
    assert first["end"] <= 25.5 + 1e-6, f"borrow not clamped: end={first['end']}"


def test_align_drops_orphan_caption_that_duplicates_first_sentence():
    """lawyer case: a 0:00 title card duplicating the first spoken sentence
    must not survive as a second entry starting at 0.00."""
    asr = [
        _seg(0.0, 4.49, "大家好我是律师张三今天讲合同纠纷"),
        _seg(5.0, 9.0, "第一个要点是违约责任的认定标准"),
    ]
    captions = [_seg(0.0, 4.49, "大家好我是律师张三", source="OCR")]
    out = al.align(asr, captions, cfg=None)
    starts = [round(s["start"], 2) for s in out]
    assert len([x for x in starts if x == 0.0]) == 1, f"0:00 duplicated: {out}"
    _clean(out)


def test_align_zero_title_dissimilar_still_passes_gate_via_canonical():
    """Even when the title text is NOT similar to the speech (so the orphan
    skip cannot catch it), canonicalization resolves the 0:00 overlap and the
    timeline passes validate_timeline."""
    asr = [
        _seg(0.0, 4.49, "welcome back to the channel everybody"),
        _seg(5.0, 9.0, "today we talk about contract law basics"),
        _seg(9.5, 14.0, "the first rule is offer and acceptance"),
    ]
    captions = [_seg(0.0, 4.49, "EPISODE 12", source="OCR")]
    out = al.align(asr, captions, cfg=None)
    ok, reason = tr.validate_timeline(out, 14.0)
    assert ok, reason
    _clean(out)


# --------------------------------------------------------------------------
# end-to-end: farm-shaped channel data through align() + the A4 gate
# --------------------------------------------------------------------------

def test_farm_shaped_channels_fuse_to_gate_clean_timeline():
    """21 embedded lines + 44 ASR segments over a 196 s video (plus a 0:00
    title and a sponsor card spanning a sentence boundary) — the fused
    timeline must pass validate_timeline(196)."""
    n = 44
    dur = 196.0
    step = dur / n
    # 6-char CV-CV-CV syllable words (168k distinct), shuffled once, six per
    # sentence: unrelated words share at most 1-2 characters, so pairwise
    # sim stays far below the 0.6 duplicate / 0.5 anchor thresholds while
    # each text still matches its own cue at 1.0
    cons, vows = "bcdfgklmnprstvz", "aeiou"
    vocab = [
        c + v + c2 + v2 + c3 + v3
        for c in cons for v in vows for c2 in cons for v2 in vows
        for c3 in cons for v3 in vows
    ]
    random.Random(42).shuffle(vocab)
    seen_pool = [" ".join(vocab[6 * i:6 * i + 6]) for i in range(n)]
    assert len({al._norm(t) for t in seen_pool}) == n
    # the pipeline only computes similarity for two pair families:
    #   * asr text vs cue text  (warp anchors, threshold 0.5)
    #   * fused entry vs fused entry (duplicate merge, threshold 0.6)
    # so those are the only pairwise distances that must stay low
    cue_idx = set(range(0, n, 3))
    for i in range(n):
        for j in range(i + 1, n):
            s = al._similarity(seen_pool[i], seen_pool[j])
            # production thresholds are strict (anchor needs > 0.5, merge
            # needs > 0.6) — so exactly-at-threshold is still safe
            assert s <= 0.6, (i, j, s)
            if i in cue_idx or j in cue_idx:
                assert s <= 0.5, ("anchor", i, j, s)
    asr = [
        _seg(round(i * step, 2), round((i + 1) * step - 0.2, 2), seen_pool[i])
        for i in range(n)
    ]
    # embedded track: every third ASR sentence, same text (human-authored),
    # slightly shifted windows like real subtitle cues
    embedded = [
        _seg(max(s["start"] - 0.1, 0.0), s["end"] + 0.1, s["text"], source="embedded")
        for s in asr[::3]
    ]
    # 0:00 title duplicating the opening line + a sponsor card across a boundary
    embedded.insert(0, _seg(0.0, 1.5, asr[0]["text"], source="OCR"))
    embedded.append(_seg(3 * step - 1.0, 3 * step + 2.0, "SPONSOR CARD", source="OCR"))

    out = al.align(asr, embedded, cfg=None)
    ok, reason = tr.validate_timeline(out, dur)
    assert ok, reason
    _clean(out)
    # fusion kept a sane number of sentences (no explosion, no collapse)
    assert 30 <= len(out) <= n + len(embedded)
    # every ASR sentence's text survives somewhere (texts may be re-merged,
    # but none may be silently dropped by the fusion/canonical layer)
    missing = [t for t in seen_pool
               if al._similarity(t, " ".join(s["text"] for s in out)) < 0.5
               and not any(al._similarity(t, s["text"]) > 0.5 for s in out)]
    assert not missing, f"dropped sentences: {missing[:3]}"


# --------------------------------------------------------------------------
# standalone runner (works without pytest)
# --------------------------------------------------------------------------

def _main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
