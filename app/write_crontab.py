from __future__ import annotations

from pathlib import Path

from app.config import get_config


def main() -> int:
    cron = get_config().sync.cron
    Path("/etc/crontabs/root").write_text(
        f"{cron} cd /app && /usr/bin/python -m app.sync_ldap\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
