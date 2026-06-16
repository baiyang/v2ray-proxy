from __future__ import annotations

import os

from app.config import get_config


def main() -> int:
    config = get_config().subscription
    bind = f"{config.listen}:{config.port}"
    args = [
        "gunicorn",
        "--bind",
        bind,
        "--workers",
        str(config.workers),
        "--threads",
        str(config.threads),
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
        "app.sub_server:create_app()",
    ]
    os.execvp(args[0], args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
