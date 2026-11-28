"""Tests for the A-track refactor of the timestamp pipeline.

Covers (batch 1, task 5 — A 档方案):
  * A1 offset invariant: channel-layer helpers return SEGMENT-RELATIVE times;
    ``shift_to_wall`` is the only relative->absolute conversion point;
  * A1 envelope validation: ``_envelope_violations`` flags segments outside
    0 <= start <= end <= part_dur + 1 s (incl. non-finite values);
  * A6: ``sanitize_segments`` drops non-finite values;
  * A4: ``validate_timeline`` end-to-end gate (negatives, over-length,
    overlap order, coverage >= 80%, empty timeline);
  * A3: sequential-array translation protocol — order contract, count
    mismatch retry, empty-item sub-batch retry, wrong-language retry,
    recursive halving on persistent failure.

Run:  python -m pytest tests/test_a_refactor.py -v
  or: python tests/test_a_refactor.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pipeline.lesson as lesson
import pipeline.transcribe as tr


# --------------------------------------------------------------------------
# A1: offset invariant
# --------------------------------------------------------------------------

def test_shift_to_wall_is_linear_and_in_place():
    segs = [{"start": 1.0, "end": 2.0, "text": "a"},
            {"start": 2.5, "end": 4.0, "text": "b"}]
    out = tr.shift_to_wall(segs, 300.0)
    assert out[0]["start"] == 301.0 and out[0]["end"] == 302.0
    assert out[1]["start"] == 302.5 and out[1]["end"] == 304.0
    assert out is segs  # converts in place, same objects


def test_parse_native_segments_returns_relative_times():
    """No offset parameter exists any more — output must start at the part's
    own clock (ms -> s conversion only)."""
    output = {
        "sentence": {
            "begin_time": 0,
            "end_time": 6800,
            "words": [
                {"text": "Hello", "begin_time": 100, "end_time": 900, "punctuation": ""},
                {"text": " ", "begin_time": 900, "end_time": 950, "punctuation": ""},
                {"text": "world", "begin_time": 950, "end_time": 2000,
                 "punctuation": ".", "begin_time": 950},
                {"text": "Second", "begin_time": 4000, "end_time": 4800, "punctuation": ""},
                {"text": "one", "begin_time": 4800, "end_time": 6000,
                 "punctuation": ".", "begin_time": 4800},
            ],
        }
    }
    segs = tr._parse_native_segments(output)
    assert len(segs) == 2
    assert abs(segs[0]["start"] - 0.1) < 1e-6, "relative start, not offset-shifted"
    assert abs(segs[0]["end"] - 2.0) < 1e-6
    assert abs(segs[1]["start"] - 4.0) < 1e-6


def test_envelope_violations_flags_bad_segments():
    part_dur = 90.0
    segs = [
        {"start": 0.0, "end": 3.0},          # ok
        {"start": -5.0, "end": 2.0},         # negative start
        {"start": 3.0, "end": 3.0},          # degenerate
        {"start": 4.0, "end": 1.0},          # inverted
        {"start": 95.0, "end": 96.0},        # past part_dur + 1
        {"start": float("nan"), "end": 5.0},  # non-finite
        {"start": float("inf"), "end": float("inf")},
        {"start": -0.03, "end": 2.0},        # float noise — tolerated
        {"start": 80.0, "end": 91.0},        # end <= part_dur + 1 — ok
    ]
    bad = tr._envelope_violations(segs, part_dur)
    assert bad == [1, 2, 3, 4, 5, 6], bad


# --------------------------------------------------------------------------
# A6: sanitize_segments non-finite guard
# --------------------------------------------------------------------------

def test_sanitize_drops_non_finite():
    segs = [
        {"start": 0.0, "end": 2.0, "text": "ok"},
        {"start": float("nan"), "end": 3.0, "text": "nan"},
        {"start": 3.0, "end": float("inf"), "text": "inf"},
    ]
    out = tr.sanitize_segments(segs, 10.0)
    assert [s["text"] for s in out] == ["ok"]


# --------------------------------------------------------------------------
# A4: end-to-end gate
# --------------------------------------------------------------------------

def _ok_timeline(n: int = 10, dur: float = 100.0) -> list[dict[str, Any]]:
    segs = []
    step = dur / n
    for i in range(n):
        segs.append({"start": i * step, "end": (i + 1) * step, "text": f"s{i}"})
    return segs


def test_gate_passes_a_healthy_timeline():
    ok, reason = tr.validate_timeline(_ok_timeline(), 100.0)
    assert ok, reason


def test_gate_rejects_empty_timeline():
    ok, reason = tr.validate_timeline([], 100.0)
    assert not ok and "为空" in reason


def test_gate_rejects_negative_start():
    segs = _ok_timeline()
    segs[3]["start"] = -12.0
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert not ok and "为负" in reason


def test_gate_rejects_overlength_start():
    segs = _ok_timeline()
    segs[9]["start"] = 900.0
    segs[9]["end"] = 910.0
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert not ok and "超出媒体时长" in reason


def test_gate_rejects_inverted_segment():
    segs = _ok_timeline()
    segs[5]["end"] = segs[5]["start"] - 1.0
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert not ok and "不小于终点" in reason


def test_gate_rejects_non_finite():
    segs = _ok_timeline()
    segs[4]["end"] = float("nan")
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert not ok and "非有限" in reason


def test_gate_rejects_backwards_order():
    segs = _ok_timeline()
    segs[6]["start"] = segs[4]["start"]   # jumps back before prev_end - 0.25
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert not ok and "重叠" in reason


def test_gate_rejects_low_coverage():
    """A collapsed timeline (one 5 s sentence on a 100 s video) must fail."""
    segs = [{"start": 0.0, "end": 5.0, "text": "all"}]
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert not ok and "覆盖率" in reason


def test_gate_accepts_just_enough_coverage():
    segs = [{"start": 0.0, "end": 80.0, "text": "speech"},
            {"start": 80.0, "end": 100.0, "text": "more"}]
    ok, reason = tr.validate_timeline(segs, 100.0)
    assert ok, reason


def test_gate_skips_duration_checks_without_duration():
    segs = [{"start": 0.0, "end": 3.0, "text": "a"},
            {"start": 3.0, "end": 6.0, "text": "b"}]
    ok, reason = tr.validate_timeline(segs, 0.0)
    assert ok, reason


# --------------------------------------------------------------------------
# A3: sequential-array translation protocol
# --------------------------------------------------------------------------

class _StubChatClient:
    """Placeholder httpx.Client — _chat is monkeypatched, the client is unused."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False



def _n_items(body: str) -> int:
    """Count the numbered input lines (1. / 2. / ...) in a protocol prompt."""
    return sum(1 for ln in body.splitlines() if ln[:2].rstrip(".").isdigit() and ". " in ln[:5])


def _prompt_n(messages: list[dict[str, str]]) -> int:
    """The original prompt is always messages[0] — feedback retries append
    assistant/user turns after it. Batch size comes from the original."""
    return _n_items(messages[0]["content"])

def _seg(text: str) -> dict[str, Any]:
    return {"start": 0.0, "end": 1.0, "text": text, "lang": tr.detect_lang(text)}


def _batch(*texts: str) -> list[tuple[int, dict[str, Any]]]:
    return [(i, _seg(t)) for i, t in enumerate(texts)]


def test_protocol_perfect_reply_orders_positionally():
    """The order contract: item i is the translation of line i — no ids."""
    calls: list[list[dict[str, Any]]] = []

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        calls.append(messages)
        return json.dumps(["你好。", "再见。"])

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("Hello.", "Goodbye."), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "你好。", 1: "再见。"}
    assert len(calls) == 1


def test_protocol_count_mismatch_retries_with_feedback():
    prompts: list[str] = []

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return json.dumps(["你好。"])          # wrong count
        return json.dumps(["你好。", "早上好。"])

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("Hello.", "Good morning."), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "你好。", 1: "早上好。"}
    assert len(prompts) == 2
    assert "1 项" in prompts[1] or "数组" in prompts[1], "retry must feed the error back"


def test_protocol_unparseable_reply_retries():
    calls = 0

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "我没有理解任务。"                # no JSON at all
        return json.dumps(["猫。"])

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("A cat."), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "猫。"}
    assert calls == 2


def test_protocol_empty_item_retranslates_only_that_item():
    """Good items are kept as-is; the empty one goes back in a smaller batch."""
    sizes: list[int] = []

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        sizes.append(_prompt_n(messages))
        if sizes[-1] == 3:
            return json.dumps(["你好。", "", "晚上好。"])
        return json.dumps(["下午好。"])             # sub-batch of 1

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(
            _batch("Hello.", "Good afternoon.", "Good evening."), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "你好。", 1: "下午好。", 2: "晚上好。"}
    assert 1 in sizes, "the bad item must be retried in a smaller batch"


def test_protocol_wrong_language_retranslates_that_item():
    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        if _prompt_n(messages) == 2:
            return json.dumps(["你好。", "Fine thank you and you?"])
        return json.dumps(["我很好，谢谢。"])        # sub-batch retry

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(
            _batch("Hello.", "I am fine, thank you."), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "你好。", 1: "我很好，谢谢。"}


def test_protocol_zh_to_en_must_not_contain_cjk():
    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        if _prompt_n(messages) == 2:
            return json.dumps(["Hello", "这是英文吗？"])
        return json.dumps(["Is this English?"])

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("你好", "这是英文吗？"), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "Hello", 1: "Is this English?"}


def test_protocol_dict_items_reject_whole_batch_with_feedback():
    """Regression: a dict item must NOT be str()-coerced into a translation.
    Any non-string item makes the whole reply invalid -> retry with feedback,
    then accept the corrected reply. zh->en direction (the str(dict) leak
    used to slip past the CJK check here)."""
    prompts: list[str] = []

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return json.dumps([{"id": 0, "text": "Hello"}, {"id": 1, "text": "Yes it is."}])
        return json.dumps(["Hello.", "Yes it is."])

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("你好。", "是的吗？"), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {0: "Hello.", 1: "Yes it is."}
    assert len(prompts) == 2
    assert "非字符串" in prompts[1], "retry must report the non-string items"
    for txt in result.values():
        assert "{" not in txt and "'id'" not in txt, "dict repr must never leak"


def test_protocol_mixed_str_and_dict_items_reject_whole_batch():
    """A single dict item among valid strings invalidates the ENTIRE reply —
    non-string items are batch-invalid, not per-item narrowable like empty
    strings, so nothing from the bad reply is kept."""
    prompts: list[str] = []

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return json.dumps(["你好。", {"text": "再见。"}])
        return json.dumps(["早上好。", "晚上好。"])

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("Good morning.", "Good evening."), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    # the leaked "你好。" from the poisoned reply must not be reused
    assert result == {0: "早上好。", 1: "晚上好。"}
    assert len(prompts) == 2
    assert "非字符串" in prompts[1]


def test_protocol_persistent_failure_splits_in_half_and_leaves_empty():
    """16 items, one cursed sentence at position 7: both top-level attempts
    fail, the batch splits 16 -> 8+8, the poisoned left 8 splits again into
    4+4; the clean half translates, the half still containing the cursed
    item hits the <=5 terminal and those sentences end as "" — no exception,
    no wrong text."""
    cursed = "CURSED_SENTENCE_XY"
    calls: list[int] = []

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        n = _prompt_n(messages)
        calls.append(n)
        if cursed in messages[0]["content"]:
            # the cursed sentence poisons every batch that contains it
            return "抱歉，我无法处理这段内容。"
        texts = [f"译{i}" for i in range(n)]
        return json.dumps(texts)

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        batch = _batch(*[f"src {i}" for i in range(16)])
        batch[7] = (7, _seg(cursed))
        result = lesson._translate_batch(batch, {}, _StubChatClient())
    finally:
        lesson._chat = orig
    for i in (4, 5, 6, 7):
        assert result.get(i, "") == "", "terminal failure leaves those empty"
    for i in (0, 1, 2, 3):
        assert result[i] == f"译{i}", result
    for i in range(8, 16):
        # the stub numbers replies batch-locally: right half -> 译0..译7
        assert result[i] == f"译{i - 8}", result
    assert 16 in calls and 8 in calls and 4 in calls, calls


def test_protocol_terminal_small_batch_leaves_empty_without_recursion():
    """<=5 items that fail twice stop there — no infinite halving."""
    calls = 0

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        nonlocal calls
        calls += 1
        return "还是不行。"

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        result = lesson._translate_batch(_batch("a", "b"), {}, _StubChatClient())
    finally:
        lesson._chat = orig
    assert result == {}
    assert calls == 2


def test_translate_segments_end_to_end_with_stub():
    """Full translate_segments: caption-prefilled sentences skip the LLM; the
    rest go through the protocol and missing entries come back as ""."""
    segments = [
        _seg("Hello."),
        _seg("Good morning."),
        _seg("Already translated."),
    ]
    segments[2]["translation"] = "已经译好。"

    def fake_chat(client, cfg, messages, max_tokens=8000, temperature=0.3):
        n = _prompt_n(messages)
        return json.dumps(["你好。"] * n)

    orig = lesson._chat
    lesson._chat = fake_chat
    try:
        out = lesson.translate_segments(segments, {}, workers=1)
    finally:
        lesson._chat = orig
    assert out == ["你好。", "你好。", "已经译好。"]


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
