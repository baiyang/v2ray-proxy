from __future__ import annotations

import os

from app.config import get_config


def main() -> int:
    config = get_config().v2ray
    os.execv(config.binary, [config.binary, "run", "-c", str(config.config_path)])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
