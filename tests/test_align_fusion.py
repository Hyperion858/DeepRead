"""Regression tests for the lawyer-course data corruption (20261128164307).

Symptoms observed on a real 12:40 video:
  * 30 sentences with NEGATIVE starts (down to -5379 s) and 78 beyond the
    media duration (up to 2920 s) — timeline garbage that survived every
    guard because the duration probe itself had been inflated;
  * 21 sentence PAIRS sharing identical (start, end): the LLM arbiter wrote
    its verdict by INDEX into `results` AFTER the list had been re-sorted,
    so it overwrote a random other sentence while the original stayed;
  * runs of unmatched ASR segments collapsing onto ONE instant (6 sentences
    at 155.57-165.93) after the last cue anchor.

Fixes under test:
  * transcribe.sanitize_segments drops/clamps off-timeline segments;
  * _warp_asr_to_cues never spreads backwards and lays tail runs out
    sequentially instead of dumping them onto one instant;
  * the arbiter updates the result dict OBJECT, so sorting can't misroute it;
  * align() dedupes identical time+text pairs.

Run:  python -m pytest tests/test_align_fusion.py -v
  or: python tests/test_align_fusion.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pipeline.align as al
import pipeline.transcribe as tr


# --------------------------------------------------------------------------
# sanitize_segments: the last line of defence against off-timeline garbage
# --------------------------------------------------------------------------

def test_sanitize_drops_negative_starts():
    segs = [
        {"start": -5379.9, "end": -5374.4, "text": "garbage from a bad offset"},
        {"start": -0.1, "end": 1.0, "text": "float noise, keep"},
    ]
    out = tr.sanitize_segments(segs, duration=760.0)
    assert len(out) == 1
    assert out[0]["start"] == 0.0        # float noise clamped to the origin


def test_sanitize_drops_beyond_duration():
    segs = [
        {"start": 2920.0, "end": 2924.0, "text": "past the end of the media"},
        {"start": 758.0, "end": 761.5, "text": "1.5s overshoot, clamp"},
    ]
    out = tr.sanitize_segments(segs, duration=760.0)
    assert len(out) == 1
    assert out[0]["end"] <= 761.0 + 1e-9


def test_sanitize_clamps_and_drops_empty():
    segs = [
        {"start": -2.0, "end": -1.0, "text": "negative"},          # dropped
        {"start": 8000.0, "end": 9000.0, "text": "far out"},       # dropped
        {"start": 10.0, "end": 9.0, "text": "inverted"},           # dropped
        {"start": 12.0, "end": 13.0, "text": "  "},                # dropped
        {"start": -0.2, "end": 3.0, "text": "ok"},                 # kept
    ]
    out = tr.sanitize_segments(segs, duration=760.0)
    assert [s["text"] for s in out] == ["ok"]
    assert out[0]["start"] == 0.0


# --------------------------------------------------------------------------
# _warp_asr_to_cues: tail runs and inverted anchors
# --------------------------------------------------------------------------

def _seg(start, end, text):
    return {"start": start, "end": end, "text": text, "lang": "en", "source": "asr"}


def test_warp_tail_run_spread_not_collapsed():
    # 5 unmatched ASR segments after the last anchor used to all land on the
    # anchor's end instant (identical times). They must now run sequentially.
    asr = [_seg(100 + i * 3, 103 + i * 3, f"unmatched tail sentence number {i}") for i in range(5)]
    asr.append(_seg(0, 2, "anchor me"))
    cues = [{"start": 0.5, "end": 2.0, "text": "anchor me", "source": "ocr"}]
    out = al._warp_asr_to_cues(asr, cues)
    tail = [s for s in out if s["text"].startswith("unmatched")]
    assert len(tail) == 5
    starts = sorted(s["start"] for s in tail)
    # distinct, strictly increasing, no negative
    assert len(set(round(s["start"], 2) for s in tail)) == 5
    assert starts[0] >= 0
    assert all(b > a for a, b in zip(starts, starts[1:]))


def test_warp_inverted_anchor_order_no_negative():
    # anchor #1 (by asr order) is matched to a LATE cue, anchor #2 to an
    # early one — the reflow between them used to go negative.
    asr = [
        _seg(0, 2, "first spoken line here"),
        _seg(300, 302, "middle unmatched filler words"),
        _seg(600, 602, "final spoken line here"),
    ]
    cues = [
        {"start": 5.0, "end": 7.0, "text": "final spoken line here", "source": "ocr"},
        {"start": 700.0, "end": 702.0, "text": "first spoken line here", "source": "ocr"},
    ]
    out = al._warp_asr_to_cues(asr, cues)
    assert all(s["start"] >= 0 for s in out), out
    assert all(s["end"] > s["start"] for s in out)


# --------------------------------------------------------------------------
# arbiter slot identity: sorting before the arbiter must not misroute writes
# --------------------------------------------------------------------------

class _FakeChat:
    """Monkeypatch target for pipeline.lesson.chat_with_failover."""

    def __init__(self, reply):
        self.reply = reply
        self.called = False

    def __call__(self, cfg, messages, **kw):
        self.called = True
        return self.reply, "stub-model"


def test_arbiter_updates_right_sentence_after_sort():
    # three ASR sentences; the middle one is ambiguous vs the OCR cue
    # (0.4 < sim <= 0.75). After align() the arbitrated text must replace
    # THAT sentence — once — with no duplicate or lost sentence.
    asr = [
        _seg(0, 4, "alpha beta gamma delta"),
        _seg(10, 14, "one two three four five six seven"),
        _seg(20, 24, "iota kappa lambda mu"),
    ]
    cue_text = "one two three eight nine ten"
    assert 0.4 < al._similarity(asr[1]["text"], cue_text) <= 0.75
    cues = [{"start": 10.5, "end": 13.5, "text": cue_text, "source": "ocr"}]
    reply = json.dumps([{"id": 0, "text": "one two three ARBITRATED"}])
    fake = _FakeChat(reply)
    import pipeline.lesson as lesson_mod
    orig = lesson_mod.chat_with_failover
    lesson_mod.chat_with_failover = fake
    try:
        out = al.align([dict(s) for s in asr], cues, align_base="asr", cfg={"chat_model": "stub"})
    finally:
        lesson_mod.chat_with_failover = orig
    texts = [s["text"] for s in out]
    assert fake.called
    assert sum("ARBITRATED" in t for t in texts) == 1          # applied once
    arb = next(s for s in out if "ARBITRATED" in s["text"])
    assert abs(arb["start"] - 10.0) < 5.0                       # the right sentence
    assert len(out) == 3                                        # none lost/duplicated
    assert all(t in texts for t in ("alpha beta gamma delta", "iota kappa lambda mu"))


def test_arbiter_slot_is_inplace_identity():
    # direct unit: the arbiter mutates the slot dict object held by results,
    # so ANY re-sorting of results between append and resolve stays correct
    results = [
        {"start": 30.0, "end": 34.0, "text": "unrelated sentence one"},
        {"start": 10.0, "end": 14.0, "text": "ambiguous original"},
    ]
    ambiguous = [{
        "asr": "ambiguous original",
        "caption": "ambiguous caption",
        "slot": results[1],
        "seg": {"start": 10.0, "end": 14.0, "text": "ambiguous original",
                "lang": "en", "source": "asr"},
        "sub_text": "ambiguous caption",
    }]
    results.reverse()   # simulate the sort invalidating indices
    fake = _FakeChat(json.dumps([{"id": 0, "text": "arbitrated final"}]))
    import pipeline.lesson as lesson_mod
    orig = lesson_mod.chat_with_failover
    lesson_mod.chat_with_failover = fake
    try:
        al._arbiter_resolve(ambiguous, results, cfg={"chat_model": "stub"})
    finally:
        lesson_mod.chat_with_failover = orig
    assert results[0]["text"] == "arbitrated final"     # slot updated in place
    assert results[1]["text"] == "unrelated sentence one"  # neighbour untouched
    assert len(results) == 2


def test_align_dedupes_identical_time_and_text():
    asr = [
        _seg(0, 4, "same words repeated here"),
        _seg(0, 4, "same words repeated here"),   # exact duplicate pair
        _seg(20, 24, "different later sentence"),
    ]
    out = al.align(asr, [], align_base="asr")
    assert len(out) == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
