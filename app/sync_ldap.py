from __future__ import annotations

import fcntl
import logging
from pathlib import Path

from app import db
from app.config import get_config
from app.ldap_service import LdapService
from app.xray_manager import sync_v2ray_config_and_restart


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _with_lock(lock_path: Path) -> object:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("ldap_sync result=skipped reason=already_running")
        raise SystemExit(0)
    return handle


def run() -> int:
    config = get_config()
    lock_handle = _with_lock(config.sync.lock_path)
    _ = lock_handle

    db.init_db()
    ldap = LdapService(config.ldap)
    active_users = db.list_ldap_users_by_status(db.ACTIVE)
    revoked = 0

    logger.info("ldap_sync result=start active_users=%s", len(active_users))
    for user in active_users:
        exists = ldap.user_exists(user.ldap_user_id)
        if exists:
            logger.info("ldap_sync_user user=%s result=active", user.ldap_user_id)
            continue

        db.revoke_user(user.ldap_user_id)
        revoked += 1
        logger.info("ldap_sync_user user=%s result=revoked", user.ldap_user_id)

    if revoked:
        sync_v2ray_config_and_restart()
        logger.info("ldap_sync result=changed revoked=%s", revoked)
    else:
        logger.info("ldap_sync result=unchanged revoked=0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception:
        logger.exception("ldap_sync result=failed")
        raise SystemExit(1)
