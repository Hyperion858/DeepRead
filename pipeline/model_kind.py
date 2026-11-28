"""Infer a model's capability from its name.

Provider /models endpoints return one flat list mixing every modality —
SiliconFlow alone returns ~93 entries covering chat, ASR, TTS, embeddings and
image generation. Without a hint the UI cannot tell which ones are usable for
transcription, so we classify by well-known naming conventions.

Rules are ordered: narrower capabilities are matched before the generic
"chat" fallback, and ambiguous tokens (e.g. "image") are made specific
("image-gen", "txt2img") to avoid tagging image *understanding* models as
image *generation*.
"""
from __future__ import annotations

# (kind, label, name fragments) — first match wins
_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("asr", "语音识别", (
        "asr", "whisper", "sensevoice", "transcrib", "paraformer",
        "fun-asr", "speech-to-text", "stt-", "-stt",
    )),
    ("tts", "语音合成", (
        "tts", "cosyvoice", "sambert", "speech-synthesis", "voice-clon",
    )),
    ("rerank", "重排", ("rerank",)),
    ("embed", "向量", (
        "embed", "bge-", "/bge", "m3e", "gte-", "text-embedding",
    )),
    ("video", "视频生成", (
        "video", "sora", "kling", "seedance", "cogvideo", "hunyuan-video",
        "wan2", "-wan-", "veo", "minimax-h",
    )),
    ("image", "文生图", (
        "flux", "kolors", "stable-diffusion", "sdxl", "z-image", "dall-e",
        "seedream", "cogview", "image-gen", "txt2img", "moss-image",
        "qwen-image", "-image/", "/image", "image-2",
    )),
    ("vision", "视觉理解", (
        "-vl", "vl-", "vision", "glm-4v", "4.1v", "internvl", "llava",
        "pixtral", "ocr", "paddleocr",
    )),
]

# capabilities that can never be exercised by a text/audio probe
_NOT_TESTABLE = {"tts", "embed", "rerank", "image", "video"}


def classify(model: str) -> dict[str, object]:
    """Return {kind, label, testable} for a model name."""
    low = (model or "").lower()
    for kind, label, keys in _RULES:
        if any(k in low for k in keys):
            return {"kind": kind, "label": label, "testable": kind not in _NOT_TESTABLE}
    if "omni" in low:
        # end-to-end multimodal: speaks chat/completions and takes audio/images
        return {"kind": "multimodal", "label": "多模态", "testable": True}
    return {"kind": "chat", "label": "对话", "testable": True}


def classify_many(models: list[str]) -> dict[str, dict[str, object]]:
    """Map model id -> capability, for attaching to a /models response."""
    return {m: classify(m) for m in models if m}
