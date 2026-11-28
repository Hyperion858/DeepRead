"""SQLite store for authn/authz metadata.

Course/media payloads stay on disk as JSON (courses/<id>/, materials/<id>/).
This DB only holds users, sessions, ownership, grants and a few settings.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(__file__).resolve().parent / "data" / "deepread.db"
_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    display_name  TEXT NOT NULL DEFAULT '',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS material_owner (
    material_id TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS course_grants (
    course_id TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    PRIMARY KEY (course_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_grants_user ON course_grants(user_id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "allow_user_generate": "1",   # admin switch: let non-admin users generate
}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        conn.commit()


def connect() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _LOCAL.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- users

def create_user(username: str, password_hash: str, role: str,
                display_name: str = "") -> dict[str, Any]:
    uid = secrets.token_hex(8)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO users(id, username, password_hash, role, display_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, username, password_hash, role, display_name, now),
        )
    return get_user(uid)  # type: ignore[return-value]


def get_user(uid: str) -> dict[str, Any] | None:
    row = connect().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def get_user_by_name(username: str) -> dict[str, Any] | None:
    row = connect().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    rows = connect().execute("SELECT id, username, role, display_name, disabled, created_at "
                             "FROM users ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def count_users() -> int:
    return int(connect().execute("SELECT COUNT(*) FROM users").fetchone()[0])


def set_user_disabled(uid: str, disabled: bool) -> None:
    with transaction() as conn:
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if disabled else 0, uid))


def set_user_role(uid: str, role: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))


def set_user_password(uid: str, password_hash: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, uid))


def delete_user(uid: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM material_owner WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM course_grants WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))


# ---------------------------------------------------------------- sessions

def create_session(user_id: str, token_hash: str, expires_at: int) -> None:
    import time
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, expires_at, int(time.time())),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


def session_user(token_hash: str) -> dict[str, Any] | None:
    import time
    row = connect().execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (token_hash, int(time.time())),
    ).fetchone()
    return dict(row) if row else None


def delete_session(token_hash: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_sessions_for_user(user_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------- ownership

def set_material_owner(material_id: str, user_id: str) -> None:
    with transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO material_owner(material_id, user_id) VALUES (?, ?)",
                     (material_id, user_id))


def material_owner(material_id: str) -> str | None:
    row = connect().execute("SELECT user_id FROM material_owner WHERE material_id = ?",
                            (material_id,)).fetchone()
    return row[0] if row else None


def owned_material_ids(user_id: str) -> set[str]:
    rows = connect().execute("SELECT material_id FROM material_owner WHERE user_id = ?",
                             (user_id,)).fetchall()
    return {r[0] for r in rows}


def delete_material_owner(material_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM material_owner WHERE material_id = ?", (material_id,))


def grant_course(course_id: str, user_id: str) -> None:
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO course_grants(course_id, user_id) VALUES (?, ?)",
                     (course_id, user_id))


def revoke_course(course_id: str, user_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM course_grants WHERE course_id = ? AND user_id = ?",
                     (course_id, user_id))


def course_grantees(course_id: str) -> list[str]:
    rows = connect().execute("SELECT user_id FROM course_grants WHERE course_id = ?",
                             (course_id,)).fetchall()
    return [r[0] for r in rows]


def granted_course_ids(user_id: str) -> set[str]:
    rows = connect().execute("SELECT course_id FROM course_grants WHERE user_id = ?",
                             (user_id,)).fetchall()
    return {r[0] for r in rows}


def delete_course_grants(course_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM course_grants WHERE course_id = ?", (course_id,))


# ---------------------------------------------------------------- settings

def get_setting(key: str, default: str = "") -> str:
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def all_settings() -> dict[str, str]:
    rows = connect().execute("SELECT key, value FROM settings").fetchall()
    return {r[0]: r[1] for r in rows}


def grant_all_users(course_id: str) -> None:
    for u in list_users():
        grant_course(course_id, u["id"])


def migrate_legacy_owners(admin_id: str) -> None:
    """One-time: existing materials/courses on disk belong to the bootstrap admin."""
    if get_setting("legacy_owners_migrated") == "1":
        return
    materials_root = DB_PATH.parent / "materials"
    courses_root = DB_PATH.parent / "courses"
    if materials_root.exists():
        for mdir in materials_root.iterdir():
            if mdir.is_dir():
                set_material_owner(mdir.name, admin_id)
    if courses_root.exists():
        for cdir in courses_root.iterdir():
            if cdir.is_dir():
                grant_course(cdir.name, admin_id)
    set_setting("legacy_owners_migrated", "1")


def export_for_backup() -> dict[str, Any]:
    """Serializable snapshot for backup/restore."""
    conn = connect()
    return {
        "users": [dict(r) for r in conn.execute("SELECT * FROM users")],
        "sessions": [],  # deliberately excluded: restore invalidates all logins
        "material_owner": [dict(r) for r in conn.execute("SELECT * FROM material_owner")],
        "course_grants": [dict(r) for r in conn.execute("SELECT * FROM course_grants")],
        "settings": all_settings(),
    }


def import_from_backup(snapshot: dict[str, Any]) -> None:
    with transaction() as conn:
        for table in ("sessions", "material_owner", "course_grants", "users", "settings"):
            conn.execute(f"DELETE FROM {table}")
        for u in snapshot.get("users", []):
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, display_name, disabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (u["id"], u["username"], u["password_hash"], u["role"],
                 u.get("display_name", ""), u.get("disabled", 0), u["created_at"]),
            )
        for row in snapshot.get("material_owner", []):
            conn.execute("INSERT INTO material_owner(material_id, user_id) VALUES (?, ?)",
                         (row["material_id"], row["user_id"]))
        for row in snapshot.get("course_grants", []):
            conn.execute("INSERT INTO course_grants(course_id, user_id) VALUES (?, ?)",
                         (row["course_id"], row["user_id"]))
        for key, value in snapshot.get("settings", {}).items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (key, value))
