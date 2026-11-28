"""Timestamp-alignment regression tests (fix for the late-ASR-offset bug).

Symptom on a real course (douyin vlog, 196 s): every sentence's stored start
was 2-6 s LATE — "播放原句" landed mid-sentence. Root cause: text-only ASR
(SenseVoiceSmall) timestamps came from ``align_to_speech``, which distributed
sentences proportionally by char count over the raw WALL-CLOCK part duration;
intro cards, breath pauses and music-only b-roll pushed everything after them
progressively later, and the silence-snap tolerance (1-2.5 s) could not recover
a multi-second drift.

Fix: allocate over SPEECH time (wall time minus detected silences), map back
onto the wall clock, then snap.

Two layers here:

1. Pure align tests (no ffmpeg): ``align_to_speech`` runs against a fake
   silence map. The pre-fix implementation is embedded below as
   ``legacy_align_to_speech`` (verbatim copy of git HEAD ca700bc) so one run
   proves the before/after difference on identical input.

2. End-to-end test (needs ffmpeg): a wav with speech bursts at *known*
   positions is synthesized directly (no ASR call) and pushed through
   ``transcribe()`` with a stubbed HTTP client. Output sentence times must
   match the known speech onsets — offset 0.

Run:  python -m pytest tests/test_align_and_transcribe.py -v
  or: python tests/test_align_and_transcribe.py
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

import pipeline.transcribe as tr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_REAL_DETECT = tr.detect_silence_stretches


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# --------------------------------------------------------------------------
# Synthetic scenario (45 s of "audio") — models the real course's shape:
#
#   0.0 -  2.5  silence   (intro card)
#   2.5 -  7.5  SPEECH A  (sentence 1)
#   7.5 -  8.3  silence   (breath pause)
#   8.3 - 13.3  SPEECH B  (sentence 2)
#  13.3 - 33.3  silence   (20 s music-only b-roll; real ASR still emits junk)
#  33.3 - 38.3  SPEECH C  (sentence 3)
#  38.3 - 45.0  silence   (outro)
#
# The stub ASR returns one line per region; the junk line mirrors what
# SenseVoice "transcribed" from background music on the real course.
# --------------------------------------------------------------------------

PART_DUR = 45.0
BURSTS = [(2.5, 7.5), (8.3, 13.3), (33.3, 38.3)]

SENT_TEXTS = [
    "aa " * 9 + "ab.",   # burst A  (30 chars)
    "bb " * 9 + "bc.",   # burst B  (30 chars)
    "啦啦啦。",            # junk "transcribed" from the music stretch (4 chars)
    "cc " * 9 + "cd.",   # burst C  (30 chars)
]

ONSET_A, ONSET_B, ONSET_C = 2.5, 8.3, 33.3
PAUSE_B = (7.5, 8.3)          # breath pause between A and B
SPEECH_END = 38.3


def _fake_stretches(part, noise_db="-30dB", min_silence=0.4):
    """Stand-in for detect_silence_stretches: the exact synthetic layout."""
    silences = []
    cursor = 0.0
    for s, e in BURSTS:
        if s > cursor:
            silences.append((cursor, s))
        cursor = e
    if cursor < PART_DUR:
        silences.append((cursor, PART_DUR))
    return silences


def _patched(fake):
    """Decorator/ctx helper: temporarily replace detect_silence_stretches."""
    class _Ctx:
        def __enter__(self):
            tr.detect_silence_stretches = fake
            return self

        def __exit__(self, *exc):
            tr.detect_silence_stretches = _REAL_DETECT
            return False
    return _Ctx()


# --------------------------------------------------------------------------
# Legacy implementation, copied verbatim from git HEAD (ca700bc) so the test
# documents WHAT was wrong, not just that something changed.
# --------------------------------------------------------------------------

def legacy_align_to_speech(part, sentences, offset, part_dur):
    if not sentences:
        return []
    sentences = tr.resplit_for_subtitles(sentences, part_dur)
    if not sentences:
        return []
    if len(sentences) == 1 or part_dur <= 0:
        text = sentences[0]
        return [{"start": offset, "end": offset + max(part_dur, 0.2),
                 "text": text, "lang": tr.detect_lang(text), "source": "asr"}]
    try:
        pauses = tr.detect_silences(part) if part_dur >= 8 else []
    except Exception:  # noqa: BLE001
        pauses = []

    total_chars = sum(len(s) for s in sentences) or 1
    min_dur = 0.6 if part_dur / len(sentences) >= 0.6 else part_dur / len(sentences) * 0.5
    tol = min(2.5, max(1.0, part_dur * 0.02))

    bounds = []
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
        remaining = len(sentences) - i - 1
        latest = part_dur - remaining * min_dur
        if target > latest:
            target = latest
        if target <= cursor + min_dur * 0.6:
            target = cursor + min_dur * 0.6
        bounds.append(target)
        cursor = target

    edges = [0.0] + bounds + [part_dur]
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] + 0.05
    if edges[-1] > part_dur and edges[-1] > 0:
        scale = part_dur / edges[-1]
        edges = [e * scale for e in edges]
    segs = []
    for i, text in enumerate(sentences):
        start, end = edges[i], edges[i + 1]
        if end <= start:
            end = start + 0.15
        segs.append({"start": offset + start, "end": offset + end,
                     "text": text, "lang": tr.detect_lang(text), "source": "asr"})
    return segs


# --------------------------------------------------------------------------
# Layer 1: pure align tests (no ffmpeg needed)
# --------------------------------------------------------------------------

def test_align_starts_at_speech_onset():
    """Sentence 1 must start where speech starts, not at the part head."""
    with _patched(_fake_stretches):
        segs = tr.align_to_speech(Path("fake.wav"), list(SENT_TEXTS), PART_DUR)
    assert len(segs) == 4
    assert abs(segs[0]["start"] - ONSET_A) < 0.05


def test_legacy_drift_is_large_and_fix_is_small():
    """THE regression: on the same input the legacy timeline put sentence B
    >5 s late (jump landed mid-sentence — the exact user complaint), while the
    fixed timeline lands inside the breath pause just before it."""
    with _patched(_fake_stretches):
        segs = tr.align_to_speech(Path("fake.wav"), list(SENT_TEXTS), PART_DUR)
        legacy = legacy_align_to_speech(Path("fake.wav"), list(SENT_TEXTS), 0.0, PART_DUR)

    # legacy: proportional over wall time -> B starts deep after its onset
    assert legacy[1]["start"] - ONSET_B > 5.0, legacy[1]
    # fixed: boundary sits inside the breath pause (7.5-8.3)
    assert PAUSE_B[0] <= segs[1]["start"] <= PAUSE_B[1], segs[1]
    # and sentence C starts within 0.6 s of its true onset
    assert abs(segs[3]["start"] - ONSET_C) < 0.6, segs[3]


def test_align_ends_at_speech_end():
    """Last sentence must end where speech ends, not at the part end."""
    with _patched(_fake_stretches):
        segs = tr.align_to_speech(Path("fake.wav"), list(SENT_TEXTS), PART_DUR)
    assert abs(segs[-1]["end"] - SPEECH_END) < 0.05
    with _patched(_fake_stretches):
        legacy = legacy_align_to_speech(Path("fake.wav"), list(SENT_TEXTS), 0.0, PART_DUR)
    assert abs(legacy[-1]["end"] - PART_DUR) < 0.05  # legacy ran into the outro


def test_shift_to_wall_shifts_everything_linearly():
    """align_to_speech returns RELATIVE times (offset invariant); the single
    conversion point shift_to_wall must shift them 1:1 — no scaling."""
    with _patched(_fake_stretches):
        base = tr.align_to_speech(Path("fake.wav"), list(SENT_TEXTS), PART_DUR)
    off = 12.0
    segs = tr.shift_to_wall([dict(s) for s in base], off)
    for a, b in zip(base, segs):
        assert abs((a["start"] + off) - b["start"]) < 1e-6
        assert abs((a["end"] + off) - b["end"]) < 1e-6


def test_align_monotonic():
    with _patched(_fake_stretches):
        segs = tr.align_to_speech(Path("fake.wav"), list(SENT_TEXTS), PART_DUR)
    for prev, cur in zip(segs, segs[1:]):
        assert cur["start"] >= prev["start"]
        assert cur["end"] > cur["start"]


def test_align_no_silence_fallback_matches_legacy():
    """Without silence data the timeline falls back to wall-clock proportional —
    bit-identical to the legacy numbers for a pause-free part."""
    with _patched(lambda part, noise_db="-30dB", min_silence=0.4: []):
        segs = tr.align_to_speech(Path("fake.wav"), list(SENT_TEXTS), PART_DUR)
        legacy = legacy_align_to_speech(Path("fake.wav"), list(SENT_TEXTS), 0.0, PART_DUR)
    for a, b in zip(legacy, segs):
        assert abs(a["start"] - b["start"]) < 1e-6
        assert abs(a["end"] - b["end"]) < 1e-6


def test_speech_spans_and_mapping():
    spans = tr.speech_spans(45.0, [(0.0, 2.5), (7.5, 8.3), (13.3, 33.3), (38.3, 45.0)])
    assert spans == [(2.5, 7.5), (8.3, 13.3), (33.3, 38.3)]
    assert abs(tr._speech_to_wall(spans, 0.0) - 2.5) < 1e-9, "t=0 -> start of A"
    assert abs(tr._speech_to_wall(spans, 5.0) - 7.5) < 1e-9, "5 s speech = end of A"
    assert abs(tr._speech_to_wall(spans, 5.5) - 8.8) < 1e-9, "5.5 s = 0.5 s into B"
    assert abs(tr._speech_to_wall(spans, 10.0) - 13.3) < 1e-9, "10 s speech = end of B"
    assert abs(tr._speech_to_wall(spans, 15.0) - 38.3) < 1e-9, "past the end clamps"


# --------------------------------------------------------------------------
# Layer 2: end-to-end transcribe() with a synthetic wav + stub ASR
# --------------------------------------------------------------------------

def _synthesize_wav(path: Path, sr: int = 16000) -> None:
    """Write the synthetic scenario as a real 16 kHz mono wav (pure python)."""
    n_total = int(PART_DUR * sr)
    samples = bytearray(n_total * 2)
    amp = int(0.4 * 32767)  # ≈ -8 dBFS, far above the -30 dB silence threshold
    for s, e in BURSTS:
        for i in range(int(s * sr), int(e * sr)):
            v = int(amp * math.sin(2 * math.pi * 440.0 * i / sr))
            struct.pack_into("<h", samples, i * 2, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(samples))


class _StubResponse:
    status_code = 200
    text = json.dumps({"text": "\n".join(SENT_TEXTS)})

    def json(self):
        return json.loads(self.text)


class _StubClient:
    """Replaces httpx.Client inside transcribe(): answers the /audio/
    transcriptions call with the fixed stub transcript."""

    def __init__(self, *a, **k):
        pass

    def post(self, url, **k):
        return _StubResponse()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_transcribe_e2e_offset_zero():
    """Full pipeline (extract → split → stub ASR → align) must reproduce the
    known speech layout with ~zero offset."""
    if not _has_ffmpeg():
        print("skip: ffmpeg not available")
        return
    workdir = Path(tempfile.mkdtemp(prefix="dr_e2e_"))
    media = workdir / "synth.wav"
    _synthesize_wav(media)

    cfg = {
        "providers": [{"id": "p1", "name": "stub", "base_url": "http://stub/v1",
                       "api_key": "k", "kind": "openai"}],
        "asr_provider": "p1",
        "asr_model": "stub-whisper",
    }

    orig_client = tr.httpx.Client
    tr.httpx.Client = _StubClient
    try:
        segs = tr.transcribe(media, cfg, workdir, workers=1)
    finally:
        tr.httpx.Client = orig_client

    by_text = {}
    for s in segs:
        by_text.setdefault(s["text"].strip(), s)

    s_a = by_text["aa " * 9 + "ab."]
    s_b = by_text["bb " * 9 + "bc."]
    s_c = by_text["cc " * 9 + "cd."]
    # offset vs the true speech onsets must be ~0 (silencedetect granularity
    # is ±0.1 s; sentence B/C boundaries may lead their onset by <0.6 s because
    # half the breath pause is a legitimate lead-in)
    assert abs(s_a["start"] - ONSET_A) < 0.3, s_a
    assert PAUSE_B[0] - 0.1 <= s_b["start"] <= ONSET_B + 0.1, s_b
    assert ONSET_C - 0.1 <= s_c["start"] <= ONSET_C + 0.6, s_c
    assert abs(s_c["end"] - SPEECH_END) < 0.4, s_c


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
