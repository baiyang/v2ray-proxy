from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.config import get_config


ACTIVE = "active"
REVOKED = "revoked"
REVOKED_MANUAL = "manual"
REVOKED_LDAP_MISSING = "ldap_missing"
REVOKED_QUOTA_EXCEEDED = "quota_exceeded"


@dataclass(frozen=True)
class UserRecord:
    ldap_user_id: str
    uuid: str | None
    status: str
    display_name: str | None
    email: str | None
    is_ldap: bool
    bound_at: str | None
    daily_traffic_limit_bytes: int | None
    revoked_reason: str | None
    revoked_until: str | None
    created_at: str
    updated_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class TrafficDaily:
    user_id: str
    day: str
    uplink_bytes: int
    downlink_bytes: int
    updated_at: str


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
        daily_traffic_limit_bytes=row["daily_traffic_limit_bytes"],
        revoked_reason=row["revoked_reason"],
        revoked_until=row["revoked_until"],
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
                daily_traffic_limit_bytes INTEGER,
                revoked_reason TEXT,
                revoked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        _ensure_column(db, "users", "is_ldap", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "users", "bound_at", "TEXT")
        _ensure_column(db, "users", "daily_traffic_limit_bytes", "INTEGER")
        _ensure_column(db, "users", "revoked_reason", "TEXT")
        _ensure_column(db, "users", "revoked_until", "TEXT")
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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS traffic_counters (
                stat_name TEXT PRIMARY KEY,
                stat_value INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS traffic_daily (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                uplink_bytes INTEGER NOT NULL DEFAULT 0,
                downlink_bytes INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, day)
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_traffic_daily_day
            ON traffic_daily(day)
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
    force_reactivate: bool = False,
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
                    bound_at, daily_traffic_limit_bytes, created_at, updated_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    ldap_user_id,
                    generated_uuid,
                    ACTIVE,
                    display_name,
                    email,
                    1 if is_ldap else 0,
                    now,
                    get_config().traffic.default_daily_limit_bytes,
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
    status = ACTIVE
    revoked_at = None
    revoked_reason = None
    revoked_until = None
    if existing.status == REVOKED and existing.revoked_reason == REVOKED_QUOTA_EXCEEDED and not force_reactivate:
        status = REVOKED
        revoked_at = existing.revoked_at
        revoked_reason = existing.revoked_reason
        revoked_until = existing.revoked_until

    with connect() as db:
        db.execute(
            """
            UPDATE users
            SET uuid = ?, status = ?, display_name = ?, email = ?, is_ldap = ?,
                bound_at = COALESCE(bound_at, ?),
                daily_traffic_limit_bytes = COALESCE(daily_traffic_limit_bytes, ?),
                updated_at = ?, revoked_at = ?, revoked_reason = ?, revoked_until = ?
            WHERE ldap_user_id = ?
            """,
            (
                generated_uuid,
                status,
                display_name,
                email,
                1 if is_ldap else 0,
                bound_at,
                get_config().traffic.default_daily_limit_bytes,
                now,
                revoked_at,
                revoked_reason,
                revoked_until,
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


def revoke_user(
    ldap_user_id: str,
    reason: str = REVOKED_MANUAL,
    revoked_until: str | None = None,
) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE users
            SET status = ?, updated_at = ?, revoked_at = ?, revoked_reason = ?, revoked_until = ?
            WHERE ldap_user_id = ?
            """,
            (REVOKED, now, now, reason, revoked_until, ldap_user_id),
        )


def restore_user(ldap_user_id: str) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE users
            SET status = ?, updated_at = ?, revoked_at = NULL,
                revoked_reason = NULL, revoked_until = NULL
            WHERE ldap_user_id = ?
            """,
            (ACTIVE, now, ldap_user_id),
        )


def list_quota_revoked_users_due(now_iso: str) -> list[UserRecord]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM users
            WHERE status = ?
              AND revoked_reason = ?
              AND revoked_until IS NOT NULL
              AND revoked_until <= ?
            ORDER BY ldap_user_id
            """,
            (REVOKED, REVOKED_QUOTA_EXCEEDED, now_iso),
        ).fetchall()
        return [_row_to_user(row) for row in rows]


def set_daily_traffic_limit(ldap_user_id: str, limit_bytes: int) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE users
            SET daily_traffic_limit_bytes = ?, updated_at = ?
            WHERE ldap_user_id = ?
            """,
            (limit_bytes, now, ldap_user_id),
        )


def get_counter(stat_name: str) -> int | None:
    with connect() as db:
        row = db.execute(
            "SELECT stat_value FROM traffic_counters WHERE stat_name = ?",
            (stat_name,),
        ).fetchone()
        return int(row["stat_value"]) if row else None


def set_counter(stat_name: str, stat_value: int) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO traffic_counters (stat_name, stat_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(stat_name) DO UPDATE SET
                stat_value = excluded.stat_value,
                updated_at = excluded.updated_at
            """,
            (stat_name, stat_value, now),
        )


def add_daily_traffic(user_id: str, day: str, uplink_delta: int, downlink_delta: int) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO traffic_daily (user_id, day, uplink_bytes, downlink_bytes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET
                uplink_bytes = traffic_daily.uplink_bytes + excluded.uplink_bytes,
                downlink_bytes = traffic_daily.downlink_bytes + excluded.downlink_bytes,
                updated_at = excluded.updated_at
            """,
            (user_id, day, max(0, uplink_delta), max(0, downlink_delta), now),
        )


def get_daily_traffic(user_id: str, day: str) -> TrafficDaily:
    with connect() as db:
        row = db.execute(
            """
            SELECT *
            FROM traffic_daily
            WHERE user_id = ? AND day = ?
            """,
            (user_id, day),
        ).fetchone()
        if row:
            return TrafficDaily(
                user_id=row["user_id"],
                day=row["day"],
                uplink_bytes=row["uplink_bytes"],
                downlink_bytes=row["downlink_bytes"],
                updated_at=row["updated_at"],
            )
    return TrafficDaily(user_id=user_id, day=day, uplink_bytes=0, downlink_bytes=0, updated_at=utc_now())


def list_daily_traffic(user_id: str, end_day: date, days: int) -> list[TrafficDaily]:
    start_day = end_day - timedelta(days=days - 1)
    day_strings = [
        (start_day + timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM traffic_daily
            WHERE user_id = ? AND day BETWEEN ? AND ?
            """,
            (user_id, day_strings[0], day_strings[-1]),
        ).fetchall()
    by_day = {
        row["day"]: TrafficDaily(
            user_id=row["user_id"],
            day=row["day"],
            uplink_bytes=row["uplink_bytes"],
            downlink_bytes=row["downlink_bytes"],
            updated_at=row["updated_at"],
        )
        for row in rows
    }
    return [
        by_day.get(
            day,
            TrafficDaily(user_id=user_id, day=day, uplink_bytes=0, downlink_bytes=0, updated_at=utc_now()),
        )
        for day in day_strings
    ]
