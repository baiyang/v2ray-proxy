from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import get_config


ACTIVE = "active"
REVOKED = "revoked"


@dataclass(frozen=True)
class UserRecord:
    ldap_user_id: str
    uuid: str | None
    status: str
    display_name: str | None
    email: str | None
    is_ldap: bool
    bound_at: str | None
    created_at: str
    updated_at: str
    revoked_at: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        ldap_user_id=row["ldap_user_id"],
        uuid=row["uuid"],
        status=row["status"],
        display_name=row["display_name"],
        email=row["email"],
        is_ldap=bool(row["is_ldap"]),
        bound_at=row["bound_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revoked_at=row["revoked_at"],
    )


def get_db_path() -> Path:
    return get_config().database.path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                ldap_user_id TEXT PRIMARY KEY,
                uuid TEXT UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
                display_name TEXT,
                email TEXT,
                is_ldap INTEGER NOT NULL DEFAULT 1,
                bound_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        _ensure_column(db, "users", "is_ldap", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "users", "bound_at", "TEXT")
        db.execute(
            """
            UPDATE users
            SET bound_at = created_at
            WHERE bound_at IS NULL AND uuid IS NOT NULL
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_users_status
            ON users(status)
            """
        )


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row["name"] == column for row in rows):
        return
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_user(ldap_user_id: str) -> UserRecord | None:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM users WHERE ldap_user_id = ?",
            (ldap_user_id,),
        ).fetchone()
        return _row_to_user(row) if row else None


def ensure_subscription_user(
    ldap_user_id: str,
    display_name: str | None,
    email: str | None,
    is_ldap: bool = True,
) -> UserRecord:
    existing = get_user(ldap_user_id)
    now = utc_now()
    if existing is None:
        generated_uuid = str(uuid.uuid4())
        with connect() as db:
            db.execute(
                """
                INSERT INTO users (
                    ldap_user_id, uuid, status, display_name, email, is_ldap,
                    bound_at, created_at, updated_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    ldap_user_id,
                    generated_uuid,
                    ACTIVE,
                    display_name,
                    email,
                    1 if is_ldap else 0,
                    now,
                    now,
                    now,
                ),
            )
        return get_user(ldap_user_id)  # type: ignore[return-value]

    if existing.uuid is None:
        generated_uuid = str(uuid.uuid4())
    else:
        generated_uuid = existing.uuid
    bound_at = now if existing.bound_at is None or existing.status == REVOKED else existing.bound_at

    with connect() as db:
        db.execute(
            """
            UPDATE users
            SET uuid = ?, status = ?, display_name = ?, email = ?, is_ldap = ?,
                bound_at = COALESCE(bound_at, ?), updated_at = ?, revoked_at = NULL
            WHERE ldap_user_id = ?
            """,
            (
                generated_uuid,
                ACTIVE,
                display_name,
                email,
                1 if is_ldap else 0,
                bound_at,
                now,
                ldap_user_id,
            ),
        )
    return get_user(ldap_user_id)  # type: ignore[return-value]


def list_users() -> list[UserRecord]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM users
            ORDER BY status, is_ldap DESC, ldap_user_id
            """
        ).fetchall()
        return [_row_to_user(row) for row in rows]


def list_active_users() -> list[UserRecord]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM users
            WHERE status = ? AND uuid IS NOT NULL
            ORDER BY ldap_user_id
            """,
            (ACTIVE,),
        ).fetchall()
        return [_row_to_user(row) for row in rows]


def list_users_by_status(status: str) -> list[UserRecord]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM users
            WHERE status = ?
            ORDER BY ldap_user_id
            """,
            (status,),
        ).fetchall()
        return [_row_to_user(row) for row in rows]


def list_ldap_users_by_status(status: str) -> list[UserRecord]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM users
            WHERE status = ? AND is_ldap = 1
            ORDER BY ldap_user_id
            """,
            (status,),
        ).fetchall()
        return [_row_to_user(row) for row in rows]


def revoke_user(ldap_user_id: str) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE users
            SET status = ?, updated_at = ?, revoked_at = ?
            WHERE ldap_user_id = ?
            """,
            (REVOKED, now, now, ldap_user_id),
        )
