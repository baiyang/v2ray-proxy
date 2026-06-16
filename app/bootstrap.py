from __future__ import annotations

import logging

from app import db
from app.xray_manager import sync_v2ray_config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> int:
    db.init_db()
    sync_v2ray_config(reload_process=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
