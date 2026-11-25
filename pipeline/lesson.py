"""Lesson generation via chat completions.

Layer 1: sentence-by-sentence translation (zh<->en, follow source language).
Layer 2: full-text + translation in -> structured lesson JSON out
         (vocabulary / grammar / hard sentences / expressions / domain /
          culture / shadowing tips). JSON validated, retried on failure.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from pipeline.transcribe import PipelineCancelled, detect_lang


def _chat_provider(cfg: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model) for the chat provider."""
    providers = cfg.get("providers") or []
    pid = cfg.get("chat_provider")
    prov = next((p for p in providers if p.get("id") == pid), None)
    if prov is None and providers:
        prov = providers[0]
    if prov:
        return (prov.get("base_url", "").rstrip("/"), prov.get("api_key", ""),
                cfg.get("chat_model") or "gpt-4o-mini")
    # legacy flat config
    return (cfg.get("chat_base_url") or cfg.get("base_url", "").rstrip("/"),
            cfg.get("chat_api_key") or cfg.get("api_key", ""),
            cfg.get("chat_model") or "gpt-4o-mini")


def _chat(
    client: httpx.Client,
    cfg: dict[str, Any],
    messages: list[dict[str, str]],
    max_tokens: int = 8000,
    temperature: float = 0.3,
) -> str:
    base_url, api_key, model = _chat_provider(cfg)
    r = client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=600,
    )
    if r.status_code != 200:
        raise RuntimeError(f"chat API {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def _extract_json(text: str) -> Any:
    """Pull the outermost JSON object/array out of an LLM reply."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    # pick whichever opener appears first — that's the outermost structure
    candidates = [(text.find("{"), "{", "}"), (text.find("["), "[", "]")]
    candidates = [c for c in candidates if c[0] != -1]
    candidates.sort()
    for start, opener, closer in candidates:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("no balanced JSON found")


def _chunk(segments: list[dict[str, Any]], size: int = 40) -> list[list[dict[str, Any]]]:
    return [segments[i:i + size] for i in range(0, len(segments), size)]


def translate_segments(
    segments: list[dict[str, Any]],
    cfg: dict[str, Any],
    progress_cb: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[str]:
    """Return one translation per segment. zh sentences -> en; en -> zh."""
    translations: list[dict[int, str]] = {}
    chunks = _chunk(segments)
    with httpx.Client() as client:
        for ci, chunk in enumerate(chunks):
            if should_cancel and should_cancel():
                raise PipelineCancelled()
            lines = [f"[{i}] {s['text']}" for i, s in enumerate(chunk)]
            prompt = (
                "你是专业字幕翻译。下面是视频逐句文本（中英混合）。逐句翻译成另一种语言："
                "英文句子翻译成简体中文，中文句子翻译成英文。保留编号，输出 JSON 数组，"
                '形如 [{"i": 编号, "t": "译文"}]，不要输出其他内容。\n\n' + "\n".join(lines)
            )
            for attempt in range(3):
                try:
                    reply = _chat(client, cfg, [{"role": "user", "content": prompt}], temperature=0.2)
                    parsed = _extract_json(reply)
                    if not isinstance(parsed, list):
                        raise ValueError("expected JSON array")
                    for item in parsed:
                        if isinstance(item, dict) and "i" in item:
                            translations[ci * 40 + int(item["i"])] = str(item.get("t", "")).strip()
                        elif isinstance(item, str):
                            # "[12] translation text" or "12. translation"
                            m = re.match(r"^\[?(\d+)\]?\s*[.、:：]?\s*(.*)$", item.strip())
                            if m:
                                translations[ci * 40 + int(m.group(1))] = m.group(2).strip()
                        elif isinstance(item, dict):
                            # tolerant key variants like {"id":1,"translation":"..."}
                            idx = item.get("id", item.get("index", item.get("n")))
                            if idx is not None:
                                translations[ci * 40 + int(idx)] = str(
                                    item.get("t", item.get("translation", item.get("text", "")))
                                ).strip()
                    break
                except (ValueError, KeyError, RuntimeError, httpx.HTTPError) as exc:
                    if attempt == 2:
                        # degrade gracefully: leave untranslated rather than fail the whole course
                        for i in range(len(chunk)):
                            translations.setdefault(ci * 40 + i, "")
                    else:
                        time.sleep(2 * (attempt + 1))
            if progress_cb:
                progress_cb((ci + 1) / len(chunks), f"翻译 {ci + 1}/{len(chunks)} 批…")
    return [translations.get(i, "") for i in range(len(segments))]


LESSON_SCHEMA_HINT = """{
  "vocabulary": [{"term": "英文词或词组", "phonetic": "音标(可空)", "pos": "词性", "definition": "中文释义", "example": "例句(取自原文或自然造句)", "sentenceIds": [0]}],
  "grammar": [{"sentenceId": 0, "note": "一句话语法要点(中文)，没有特殊要点可省略该句"}],
  "hardSentences": [{"sentenceId": 0, "structure": "句子主干结构描述(中文)", "breakdown": "成分拆解，逐块说明(中文)"}],
  "expressions": [{"phrase": "口语短语/习语", "meaning": "中文含义", "usage": "使用场景说明", "sentenceIds": [0]}],
  "domain": [{"term": "行业术语(如律师/法律)", "explanation": "中文解释", "sentenceIds": [0]}],
  "culture": [{"topic": "文化背景主题", "note": "中文注释", "sentenceIds": [0]}],
  "shadowing": [{"sentenceId": 0, "tip": "跟读提示：重音、连读、语调等"}]
}"""


def generate_lesson(
    segments: list[dict[str, Any]],
    cfg: dict[str, Any],
    progress_cb: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if should_cancel and should_cancel():
        raise PipelineCancelled()
    if progress_cb:
        progress_cb(0.0, "逐句翻译中…")
    translations = translate_segments(segments, cfg, progress_cb, should_cancel)

    # build numbered transcript with translations for the lesson layer
    lines = []
    for i, seg in enumerate(segments):
        lang = "中" if seg["lang"] == "zh" else "EN"
        tr = translations[i]
        tr_line = f"\n   译: {tr}" if tr else ""
        lines.append(f"[{i}] ({lang}) {seg['text']}{tr_line}")
    full_text = "\n".join(lines)

    prompt = (
        "你是英语精读课程的资深讲师。以下是一段中英混合采访视频的逐句文本（含翻译）。"
        "请为英语学习者生成精读课程内容，输出严格 JSON（不要 markdown 代码块），结构如下：\n"
        + LESSON_SCHEMA_HINT +
        "\n\n要求：\n"
        "1. vocabulary 挑 10~20 个值得学习的词汇/词组（优先英文句中的，中文句中的可挑对学英文有用的）；sentenceIds 引用出现的句子编号。\n"
        "2. grammar 只为有语法学习价值的句子给一句话注释。\n"
        "3. hardSentences 挑 3~8 个长难句/复杂句做结构拆解。\n"
        "4. expressions 挑采访中的口语表达、习语、地道用法 5~10 个。\n"
        "5. domain 挑职业/行业相关术语（本素材如涉及律师、法律、签证、职场等）。\n"
        "6. culture 挑 3~6 个文化背景点（如教育体系、执业资格、国家地区相关）。\n"
        "7. shadowing 挑 5~10 个适合跟读模仿的句子给语音提示。\n"
        "8. 全部解释用简体中文，例子保留英文原文。\n\n"
        "逐句文本：\n" + full_text
    )

    if progress_cb:
        progress_cb(0.5, "生成精讲内容…")
    with httpx.Client() as client:
        lesson: dict[str, Any] | None = None
        for attempt in range(3):
            if should_cancel and should_cancel():
                raise PipelineCancelled()
            try:
                reply = _chat(client, cfg, [{"role": "user", "content": prompt}], max_tokens=16000, temperature=0.3)
                parsed = _extract_json(reply)
                if isinstance(parsed, dict):
                    lesson = parsed
                    break
            except (ValueError, RuntimeError, httpx.HTTPError, json.JSONDecodeError):
                time.sleep(2 * (attempt + 1))
        if lesson is None:
            lesson = {"vocabulary": [], "grammar": [], "hardSentences": [], "expressions": [],
                      "domain": [], "culture": [], "shadowing": [], "_error": "精讲生成失败，仅保留翻译"}

    # merge translations into segments-shaped payload for the frontend
    sent = []
    for i, seg in enumerate(segments):
        sent.append({
            "id": i,
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"],
            "lang": seg["lang"],
            "source": seg.get("source", "asr"),
            "uncertain": bool(seg.get("uncertain")),
            "translation": translations[i],
            "grammar": next((g["note"] for g in lesson.get("grammar", []) if g.get("sentenceId") == i), ""),
        })

    if progress_cb:
        progress_cb(1.0, "精讲生成完成")
    return {
        "sentences": sent,
        "vocabulary": lesson.get("vocabulary", []),
        "hardSentences": lesson.get("hardSentences", []),
        "expressions": lesson.get("expressions", []),
        "domain": lesson.get("domain", []),
        "culture": lesson.get("culture", []),
        "shadowing": lesson.get("shadowing", []),
        "_notes": lesson.get("_error", ""),
    }
