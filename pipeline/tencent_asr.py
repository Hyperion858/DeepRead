"""Tencent Cloud ASR (录音文件识别) adapter.

Why a separate module: Tencent uses TC3-HMAC-SHA256 request signing and an
async task model (CreateRecTask -> poll DescribeTaskStatus), so it cannot go
through the OpenAI-compatible `/audio/transcriptions` path.

Audio is sent inline (SourceType=1) because the files live on a private LAN.
That path caps the request at 5MB base64, so callers must pass mp3 segments.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

HOST = "asr.tencentcloudapi.com"
SERVICE = "asr"
VERSION = "2019-06-14"

# 5MB upstream cap applies to the base64 payload; stay a little under.
MAX_DATA_BYTES = 3_600_000

DEFAULT_ENGINE = "16k_zh_en_2.0"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sign_request(
    secret_id: str, secret_key: str, action: str, payload: dict[str, Any], region: str
) -> tuple[str, dict[str, str]]:
    """Build the Authorization header per Tencent 签名方法 v3."""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ctype = "application/json; charset=utf-8"

    canonical_headers = f"content-type:{ctype}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
    )

    scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    k_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    k_service = _hmac(k_date, SERVICE)
    k_signing = _hmac(k_service, "tc3_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    auth = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": auth,
        "Content-Type": ctype,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": VERSION,
        "X-TC-Region": region or "ap-guangzhou",
    }
    return body, headers


def _call(
    secret_id: str,
    secret_key: str,
    action: str,
    payload: dict[str, Any],
    region: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    body, headers = _sign_request(secret_id, secret_key, action, payload, region)
    r = httpx.post(f"https://{HOST}", headers=headers, content=body.encode("utf-8"), timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"腾讯云 {action} 返回非 JSON（HTTP {r.status_code}）: {r.text[:200]}")
    resp = data.get("Response") or {}
    if resp.get("Error"):
        err = resp["Error"]
        raise RuntimeError(f"腾讯云 {action} 失败: {err.get('Code')} {err.get('Message')}")
    if r.status_code != 200:
        raise RuntimeError(f"腾讯云 {action} HTTP {r.status_code}: {r.text[:200]}")
    return resp


def transcribe_native(
    part: Path,
    secret_id: str,
    secret_key: str,
    region: str = "ap-guangzhou",
    engine: str = "",
    poll_interval: float = 2.0,
    poll_timeout: float = 300.0,
) -> list[dict[str, Any]]:
    """Transcribe one audio file. Returns segments with absolute file-relative
    times (caller adds its own offset).

    ResTextFormat=3 is used so Tencent splits the result at punctuation, which
    is exactly what subtitles need.
    """
    raw = part.read_bytes()
    if len(raw) > MAX_DATA_BYTES:
        raise RuntimeError(
            f"音频过大（{len(raw)/1048576:.1f}MB），腾讯云直传上限约 3.6MB；请缩短切段或降低码率。"
        )
    data_b64 = base64.b64encode(raw).decode()

    create_payload = {
        "EngineModelType": engine or DEFAULT_ENGINE,
        "ChannelNum": 1,          # 16k audio must be mono
        "ResTextFormat": 3,       # split at punctuation — subtitle friendly
        "SourceType": 1,          # inline base64 (files are on a private LAN)
        "Data": data_b64,
        "DataLen": len(raw),
    }
    resp = _call(secret_id, secret_key, "CreateRecTask", create_payload, region, timeout=120)
    task_id = (resp.get("Data") or {}).get("TaskId")
    if not task_id:
        raise RuntimeError(f"腾讯云未返回 TaskId: {json.dumps(resp)[:200]}")

    # poll until the task settles (0=waiting, 1=doing, 2=success, 3=failed)
    deadline = time.time() + poll_timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"腾讯云识别超时（>{poll_timeout:.0f}s），TaskId={task_id}")
        time.sleep(poll_interval)
        st = _call(secret_id, secret_key, "DescribeTaskStatus", {"TaskId": task_id}, region)
        data = st.get("Data") or {}
        status = data.get("Status")
        if status == 2:
            break
        if status == 3:
            raise RuntimeError(f"腾讯云识别失败: {data.get('ErrorMsg') or '未知原因'}（TaskId={task_id}）")

    segments: list[dict[str, Any]] = []
    for item in data.get("ResultDetail") or []:
        text = (item.get("FinalSentence") or "").strip()
        if not text:
            continue
        segments.append({
            "start": float(item.get("StartMs", 0)) / 1000.0,
            "end": float(item.get("EndMs", 0)) / 1000.0,
            "text": text,
            "lang": "",
            "source": "asr",
            "speaker": item.get("SpeakerId"),
        })
    return segments


def test_connection(
    secret_id: str, secret_key: str, region: str = "ap-guangzhou"
) -> tuple[bool, str]:
    """Cheap credential check.

    A bogus TaskId returns FailedOperation.NoSuchTask when the signature is
    valid, and AuthFailure.* when it is not — that distinction lets us verify
    credentials without spending any transcription quota.
    """
    try:
        _call(secret_id, secret_key, "DescribeTaskStatus", {"TaskId": 1}, region, timeout=20)
        return True, "✓ 凭证有效"
    except RuntimeError as exc:
        msg = str(exc)
        if "NoSuchTask" in msg:
            return True, "✓ 凭证有效（签名校验通过）"
        if "AuthFailure" in msg:
            if "SecretIdNotFound" in msg or "InvalidSecretId" in msg:
                return False, "✗ SecretId 不存在，请检查密钥对的 SecretId"
            if "SignatureFailure" in msg:
                return False, "✗ 签名校验失败，SecretKey 可能不正确"
            return False, f"✗ 鉴权失败: {msg[:160]}"
        if "ServiceIsolate" in msg or "UserHasNoFreeAmount" in msg:
            return False, f"✗ 账号未开通服务或额度耗尽: {msg[:140]}"
        return False, f"✗ {msg[:180]}"
