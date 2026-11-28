"""Authentication and authorization for DeepRead.

Password hashing: PBKDF2-HMAC-SHA256 via stdlib (no extra dependency).
Session token: 32 random bytes, only its SHA-256 digest is stored in DB.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

import db

COOKIE_NAME = "deepread_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ROUNDS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_s)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + SESSION_TTL
    db.create_session(user_id, _token_hash(token), expires)
    return token, expires


def issue_login(response, user_id: str) -> None:
    """Set the session cookie on a Response (caller returns the response)."""
    token, expires = create_session(user_id)
    max_age = expires - int(time.time())
    response.set_cookie(
        COOKIE_NAME, token, max_age=max_age, httponly=True, samesite="lax",
        secure=False,  # Funnel terminates TLS; the cookie travels inside the encrypted tunnel
        path="/",
    )


def clear_login(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request) -> dict[str, Any]:
    """Resolve the logged-in user from the session cookie."""
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        raise HTTPException(401, "请先登录")
    user = db.session_user(_token_hash(token))
    if not user:
        raise HTTPException(401, "登录已过期，请重新登录")
    if user.get("disabled"):
        raise HTTPException(403, "账号已被禁用")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可执行此操作")
    return user


def is_admin(user: dict[str, Any]) -> bool:
    return user.get("role") == "admin"


def can_view_material(user: dict[str, Any], material_id: str) -> bool:
    return is_admin(user) or db.material_owner(material_id) == user["id"]


def can_view_course(user: dict[str, Any], course_id: str) -> bool:
    return is_admin(user) or user["id"] in db.course_grantees(course_id)


def require_material(request: Request, material_id: str) -> dict[str, Any]:
    user = current_user(request)
    if not can_view_material(user, material_id):
        raise HTTPException(403, "无权访问该素材")
    return user


def require_course(request: Request, course_id: str) -> dict[str, Any]:
    user = current_user(request)
    if not can_view_course(user, course_id):
        raise HTTPException(403, "无权访问该课程")
    return user


def can_generate(user: dict[str, Any]) -> bool:
    return is_admin(user) or db.get_setting("allow_user_generate", "1") == "1"


def admin_username() -> str:
    """Configurable bootstrap admin name (default Jeffrey)."""
    return (os.environ.get("DEEPREAD_ADMIN_USER", "Jeffrey").strip() or "Jeffrey")


def bootstrap_admin() -> dict[str, Any] | None:
    """Create the initial admin from env vars on first run."""
    password = os.environ.get("DEEPREAD_ADMIN_PASSWORD", "").strip()
    if not password:
        return None
    username = admin_username()
    existing = db.get_user_by_name(username)
    if existing:
        return existing  # never reset an existing password on restart
    admin = db.create_user(username, hash_password(password), "admin", "管理员")
    db.migrate_legacy_owners(admin["id"])
    return admin
