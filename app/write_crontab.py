from __future__ import annotations

from pathlib import Path

from app.config import get_config


def main() -> int:
    config = get_config()
    lines = [
        f"{config.sync.cron} cd /app && /usr/bin/python -m app.sync_ldap",
    ]
    Path("/etc/crontabs/root").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
