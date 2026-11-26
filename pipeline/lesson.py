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


# ---- model failover -------------------------------------------------------

# error signatures that mean "this model is dead/over quota" -> try next model
_MODEL_DEAD_MARKERS = (
    "quota", "insufficient", "exceeded", "额度", "余额", "欠费", "免费额度",
    "model_not_found", "not found", "不存在", "not available",
    "no permission", "无权限", "deprecated", "decommissioned", "下线",
    "429", "403", "402", "404",
)


def _is_model_dead(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(m in low for m in _MODEL_DEAD_MARKERS)


def chat_with_failover(
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    max_tokens: int = 8000,
    temperature: float = 0.3,
) -> tuple[str, str]:
    """Call chat completions, walking the selected models in order when one is
    dead or out of quota.

    Returns (content, model_used). Raises when every model fails.
    """
    import httpx as _httpx

    prov = _resolve_provider(cfg, "chat_provider")
    if not prov:
        raise RuntimeError("chat provider 未配置")

    models = ordered_models(cfg, "chat_model", "chat", "chat_models")

    base = prov.get("base_url", "").rstrip("/")
    key = prov.get("api_key", "")
    errors: list[str] = []
    with _httpx.Client() as client:
        for model in models:
            try:
                r = client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    timeout=600,
                )
                if r.status_code == 200:
                    note_model_result(prov.get("id", ""), model, True)
                    return r.json()["choices"][0]["message"]["content"], model
                err = f"chat API {model} -> {r.status_code}: {r.text[:200]}"
                errors.append(err)
                # mark, then keep walking the selected order
                note_model_result(prov.get("id", ""), model, False, f"HTTP {r.status_code}: {r.text[:160]}")
                if not _is_model_dead(r.text):
                    # auth/network errors won't be fixed by another model; bail fast
                    if r.status_code in (401, 403) and "quota" not in r.text.lower():
                        raise RuntimeError(err)
                    if r.status_code >= 500:
                        continue  # relay hiccup — next model may work
                    continue
                # model dead -> next candidate
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{model}: {exc}")
                note_model_result(prov.get("id", ""), model, False, str(exc)[:160])
    raise RuntimeError("所有模型均失败: " + " | ".join(errors[-3:]))


def _resolve_provider(cfg: dict[str, Any], provider_key: str) -> dict[str, Any]:
    providers = cfg.get("providers") or []
    pid = cfg.get(provider_key)
    prov = next((p for p in providers if p.get("id") == pid), None)
    if prov is None and providers:
        prov = providers[0]
    return prov or {}


# ---- model lists ----
# models that one role can plausibly use
_ROLE_EXCLUDE = ("embed", "tts", "whisper", "transcri", "asr", "speech", "rerank",
                 "moderation", "dall-e", "stable-diffusion", "sdxl", "flux", "image", "ocr", "audio")
_ROLE_MARKERS = {
    "asr": ("whisper", "transcri", "asr", "audio", "speech", "sensevoice", "paraformer", "funasr", "vox", "omni"),
    "vision": ("vl", "vision", "ocr", "omni", "gemini", "gpt-4o", "gpt-4.1", "gpt-5", "claude",
               "pixtral", "llava", "internvl", "glm-4v", "qvq"),
}
# providers advertise hundreds of models; never walk more than a handful
_MODEL_LIMIT = {"chat": 8, "asr": 6, "vision": 6}


def filter_models_for_role(ids: list[str], role: str) -> list[str]:
    """Name-based guess at which of `ids` can serve `role` (may be empty)."""
    if not ids:
        return []
    if role in _ROLE_MARKERS:
        return [m for m in ids if any(k in m.lower() for k in _ROLE_MARKERS[role])]
    return [m for m in ids if not any(k in m.lower() for k in _ROLE_EXCLUDE)]


def models_for_role(prov: dict[str, Any], role: str,
                    cfg: dict[str, Any] | None = None) -> list[str]:
    """Probed capability list for this provider+role (empty when not probed).

    Kept for callers/tests that ask "what can this provider do"; failover itself
    no longer uses it — the user's selection is authoritative.
    """
    if not cfg:
        return []
    entry = (cfg.get("probe_cache") or {}).get(prov.get("id") or "") or {}
    if entry.get("base_url") != (prov.get("base_url") or "").rstrip("/"):
        return []
    return list((entry.get(role) or {}).get("models") or [])


def ordered_models(cfg: dict[str, Any], model_key: str,
                   role: str, list_key: str = "") -> list[str]:
    """Candidates for one call: exactly the models the user selected, in their
    order. The selection is the single source of truth for failover."""
    models: list[str] = []
    if list_key:
        for m in cfg.get(list_key) or []:
            if m and m not in models:
                models.append(m)
    primary = cfg.get(model_key)
    if primary and primary not in models:
        models.append(primary)
    if not models:
        default = {"chat_model": "gpt-4o-mini", "asr_model": "whisper-1", "vision_model": "qwen3.5-ocr"}.get(model_key)
        if default:
            models.append(default)
    return models[:_MODEL_LIMIT.get(role, 8)]


def note_model_result(provider_id: str, model: str, ok: bool, message: str = "") -> None:
    """Best-effort health mark so the settings page can flag dead models."""
    try:
        from pipeline import health
        health.note(provider_id, model, ok, message)
    except Exception:  # noqa: BLE001
        pass


def audio_transcribe_with_failover(
    cfg: dict[str, Any],
    part_path,
    prompt: str | None = None,
    response_format: str = "verbose_json",
) -> tuple[list[dict[str, Any]], str]:
    """Transcribe one audio segment, walking ASR model candidates on dead-model errors.

    Tries chat-mode (input_audio) and transcriptions endpoint per model
    depending on the model name. Returns (segments, model_used).
    """
    import base64 as _base64
    import httpx as _httpx

    prov = _resolve_provider(cfg, "asr_provider")
    base = prov.get("base_url", "").rstrip("/")
    key = prov.get("api_key", "")
    if not base:
        raise RuntimeError("转写 provider 未配置")

    from pipeline.transcribe import detect_lang, probe_duration
    import re as _re

    data = part_path.read_bytes()
    fmt = "mp3" if part_path.suffix.lower() == ".mp3" else "wav"
    b64 = _base64.b64encode(data).decode()
    offset = 0.0  # caller passes offset via part metadata; kept 0 and caller re-offsets

    errors: list[str] = []
    for model in ordered_models(cfg, "asr_model", "asr", "asr_models"):
        low = model.lower()
        try:
            if any(k in low for k in ("omni", "qwen-audio", "gemini", "gpt-4o-audio")):
                # chat-mode transcription
                r = _httpx.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt or (
                                "逐字转写这段音频。要求：按说话顺序输出，每句话单独一行；"
                                "保留原始语言（中文说中文，英文说英文）；不要添加任何解释、总结或标注。"
                            )},
                            {"type": "input_audio", "input_audio": {"data": f"data:audio/{fmt};base64,{b64}", "format": fmt}},
                        ]}],
                        "max_tokens": 8000,
                        "temperature": 0,
                    },
                    timeout=httpx.Timeout(connect=15, read=300, write=120, pool=15),
                )
                if r.status_code != 200:
                    errors.append(f"{model}: {r.status_code} {r.text[:120]}")
                    note_model_result(prov.get("id", ""), model, False, f"HTTP {r.status_code}: {r.text[:120]}")
                    if not _is_model_dead(r.text):
                        raise RuntimeError(errors[-1])
                    continue
                content = r.json()["choices"][0]["message"]["content"].strip()
                note_model_result(prov.get("id", ""), model, True)
                if not content:
                    return [], model
                lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
                lines = [_re.sub(r"^[-*\d.、)\s]+", "", ln) or ln for ln in lines]
                part_dur = probe_duration(part_path)
                dur_per = part_dur / max(len(lines), 1)
                segs = []
                t = 0.0
                for ln in lines:
                    segs.append({"start": t, "end": t + dur_per, "text": ln,
                                 "lang": detect_lang(ln), "source": "asr"})
                    t += dur_per
                return segs, model
            else:
                # whisper-style endpoint
                with part_path.open("rb") as fh:
                    r = _httpx.post(
                        f"{base}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files={"file": (part_path.name, fh, "audio/wav")},
                        data={"model": model, "response_format": response_format},
                        timeout=httpx.Timeout(connect=15, read=300, write=120, pool=15),
                    )
                if r.status_code != 200:
                    errors.append(f"{model}: {r.status_code} {r.text[:120]}")
                    note_model_result(prov.get("id", ""), model, False, f"HTTP {r.status_code}: {r.text[:120]}")
                    if not _is_model_dead(r.text):
                        raise RuntimeError(errors[-1])
                    continue
                try:
                    payload = r.json()
                except ValueError:
                    payload = None
                note_model_result(prov.get("id", ""), model, True)
                segs = []
                if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
                    for seg in payload["segments"]:
                        text = (seg.get("text") or "").strip()
                        if text:
                            segs.append({"start": float(seg.get("start", 0)), "end": float(seg.get("end", 0)),
                                         "text": text, "lang": detect_lang(text), "source": "asr"})
                else:
                    text = (r.text or "").strip()
                    if text:
                        part_dur = probe_duration(part_path)
                        segs = [{"start": 0.0, "end": part_dur, "text": text,
                                 "lang": detect_lang(text), "source": "asr"}]
                return segs, model
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model}: {exc}")
            note_model_result(prov.get("id", ""), model, False, str(exc)[:120])
    raise RuntimeError("所有转写模型均失败: " + " | ".join(errors[-3:]))


def ocr_with_failover(
    cfg: dict[str, Any],
    img_path,
) -> str:
    """OCR one image, walking vision model candidates on dead-model errors."""
    import base64 as _base64
    import httpx as _httpx

    prov = _resolve_provider(cfg, "vision_provider") or _resolve_provider(cfg, "chat_provider")
    base = prov.get("base_url", "").rstrip("/")
    key = prov.get("api_key", "")
    if not base:
        raise RuntimeError("OCR provider 未配置")
    b64 = _base64.b64encode(img_path.read_bytes()).decode()
    errors: list[str] = []
    for model in ordered_models(cfg, "vision_model", "vision", "vision_models"):
        try:
            r = _httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "识别图中字幕文字。只输出字幕原文，没有字幕则输出空字符串，不要解释、不要标点修正。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    "max_tokens": 200,
                    "temperature": 0,
                },
                timeout=httpx.Timeout(connect=15, read=90, write=30, pool=15),
            )
            if r.status_code == 200:
                note_model_result(prov.get("id", ""), model, True)
                return r.json()["choices"][0]["message"]["content"].strip()
            errors.append(f"{model}: {r.status_code} {r.text[:120]}")
            note_model_result(prov.get("id", ""), model, False, f"HTTP {r.status_code}: {r.text[:120]}")
            if not _is_model_dead(r.text):
                raise RuntimeError(errors[-1])
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model}: {exc}")
            note_model_result(prov.get("id", ""), model, False, str(exc)[:120])
    raise RuntimeError("所有 OCR 模型均失败: " + " | ".join(errors[-3:]))


def _chat(
    client: httpx.Client,
    cfg: dict[str, Any],
    messages: list[dict[str, str]],
    max_tokens: int = 8000,
    temperature: float = 0.3,
) -> str:
    """Chat call for one prompt.

    Always walks the selected model list in order, so a dead primary falls
    through to the next pick.
    """
    content, _ = chat_with_failover(
        cfg, messages, max_tokens=max_tokens, temperature=temperature,
    )
    return content


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
    workers: int = 3,
) -> list[str]:
    """Return one translation per segment. zh sentences -> en; en -> zh.

    Chunks run concurrently (3 workers) — translation batches are independent
    and the relay handles parallel requests fine.
    """
    translations: dict[int, str] = {}
    chunks = _chunk(segments)
    import concurrent.futures as _futures

    def _translate_chunk(ci: int) -> None:
        if should_cancel and should_cancel():
            raise PipelineCancelled()
        chunk = chunks[ci]
        lines = [f"[{i}] {s['text']}" for i, s in enumerate(chunk)]
        prompt = (
            "你是专业字幕翻译。下面是视频逐句文本（中英混合）。逐句翻译成另一种语言："
            "英文句子翻译成简体中文，中文句子翻译成英文。保留编号，输出 JSON 数组，"
            '形如 [{"i": 编号, "t": "译文"}]，不要输出其他内容。\n\n' + "\n".join(lines)
        )
        with httpx.Client() as client:
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
                            m = re.match(r"^\[?(\d+)\]?\s*[.、:：]?\s*(.*)$", item.strip())
                            if m:
                                translations[ci * 40 + int(m.group(1))] = m.group(2).strip()
                        elif isinstance(item, dict):
                            idx = item.get("id", item.get("index", item.get("n")))
                            if idx is not None:
                                translations[ci * 40 + int(idx)] = str(
                                    item.get("t", item.get("translation", item.get("text", "")))
                                ).strip()
                    break
                except PipelineCancelled:
                    raise
                except (ValueError, KeyError, RuntimeError, httpx.HTTPError) as exc:
                    if attempt == 2:
                        for i in range(len(chunk)):
                            translations.setdefault(ci * 40 + i, "")
                    else:
                        time.sleep(2 * (attempt + 1))

    done = 0
    errors: list[Exception] = []
    with _futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_translate_chunk, ci): ci for ci in range(len(chunks))}
        for fut in _futures.as_completed(futures):
            ci = futures[fut]
            try:
                fut.result()
            except PipelineCancelled:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            done += 1
            if progress_cb:
                progress_cb(done / len(chunks), f"翻译 {done}/{len(chunks)} 批…")
    if len(errors) >= len(chunks):
        raise RuntimeError(f"翻译全部批次失败: {errors[-1]}")
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
    translations = translate_segments(segments, cfg, progress_cb, should_cancel,
                                      workers=max(1, int(cfg.get("translate_workers") or 3)))

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
