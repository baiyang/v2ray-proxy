from __future__ import annotations

import json
import logging
import subprocess
import sys
import time as time_module
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import db
from app.config import get_config
from app.xray_manager import sync_v2ray_config, sync_v2ray_config_and_restart


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STAT_PATTERN = r"user>>>.*>>>traffic>>>(uplink|downlink)"


@dataclass(frozen=True)
class UserTrafficDelta:
    uplink: int = 0
    downlink: int = 0


def run() -> int:
    config = get_config()
    if not config.traffic.enabled:
        logger.info("traffic_collect result=skipped reason=disabled")
        return 0

    db.init_db()
    restored = restore_quota_users()
    deltas = query_v2ray_stats()
    today = current_day()
    quota_revoked = apply_traffic_deltas(today, deltas)

    if restored or quota_revoked:
        sync_v2ray_config_and_restart()

    logger.info(
        "traffic_collect result=ok users=%s restored=%s quota_revoked=%s",
        len(deltas),
        restored,
        quota_revoked,
    )
    return 0


def loop() -> int:
    config = get_config()
    interval = max(1, config.traffic.interval_seconds)
    logger.info("traffic_loop result=start interval_seconds=%s", interval)
    while True:
        try:
            run()
        except Exception:
            logger.exception("traffic_loop_collect result=failed")
        time_module.sleep(interval)


def restore_quota_users() -> int:
    now = datetime.now(ZoneInfo(get_config().traffic.timezone)).isoformat(timespec="seconds")
    users = db.list_quota_revoked_users_due(now)
    for user in users:
        db.restore_user(user.ldap_user_id)
        logger.info("traffic_restore user=%s result=active", user.ldap_user_id)
    return len(users)


def query_v2ray_stats() -> dict[str, UserTrafficDelta]:
    config = get_config()
    server = f"{config.traffic.api_host}:{config.traffic.api_port}"
    result = subprocess.run(
        [
            config.v2ray.binary,
            "api",
            "stats",
            "-json",
            "--server",
            server,
            "-regexp",
            STAT_PATTERN,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("traffic_stats_query result=failed stderr=%s", result.stderr.strip())
        return {}
    if not result.stdout.strip():
        return {}

    payload = json.loads(result.stdout)
    rows = payload.get("stat") or payload.get("stats") or []
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"uplink": 0, "downlink": 0})
    for row in rows:
        name = str(row.get("name", ""))
        value = int(row.get("value", 0))
        user_id, direction = parse_stat_name(name)
        if user_id and direction:
            last_value = db.get_counter(name)
            delta = value if last_value is None or value < last_value else value - last_value
            db.set_counter(name, value)
            buckets[user_id][direction] += max(0, delta)

    return {
        user_id: UserTrafficDelta(uplink=values["uplink"], downlink=values["downlink"])
        for user_id, values in buckets.items()
    }


def parse_stat_name(name: str) -> tuple[str | None, str | None]:
    parts = name.split(">>>")
    if len(parts) != 4:
        return None, None
    if parts[0] != "user" or parts[2] != "traffic":
        return None, None
    direction = parts[3]
    if direction not in {"uplink", "downlink"}:
        return None, None
    return parts[1], direction


def current_day() -> str:
    return datetime.now(ZoneInfo(get_config().traffic.timezone)).date().isoformat()


def apply_traffic_deltas(today: str, deltas: dict[str, UserTrafficDelta]) -> int:
    revoked = 0
    for user_id, delta in deltas.items():
        user = db.get_user(user_id)
        if user is None:
            logger.info("traffic_delta user=%s result=ignored reason=unknown", user_id)
            continue

        db.add_daily_traffic(user_id, today, delta.uplink, delta.downlink)
        daily = db.get_daily_traffic(user_id, today)
        limit = user.daily_traffic_limit_bytes or get_config().traffic.default_daily_limit_bytes
        total_bytes = daily.uplink_bytes + daily.downlink_bytes
        logger.info(
            "traffic_delta user=%s uplink=%s downlink=%s today_total=%s limit=%s",
            user_id,
            delta.uplink,
            delta.downlink,
            total_bytes,
            limit,
        )

        if user.status == db.ACTIVE and total_bytes >= limit:
            db.revoke_user(
                user_id,
                reason=db.REVOKED_QUOTA_EXCEEDED,
                revoked_until=next_midnight_iso(),
            )
            revoked += 1
            logger.info("traffic_quota user=%s result=revoked", user_id)
    return revoked


def next_midnight_iso() -> str:
    tz = ZoneInfo(get_config().traffic.timezone)
    now = datetime.now(tz)
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=tz).isoformat(timespec="seconds")


if __name__ == "__main__":
    try:
        raise SystemExit(loop() if "--loop" in sys.argv else run())
    except Exception:
        logger.exception("traffic_collect result=failed")
        raise SystemExit(1)
