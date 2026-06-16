from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from app import db
from app.config import V2RayConfig, get_config


logger = logging.getLogger(__name__)


def build_v2ray_config(config: V2RayConfig | None = None) -> dict[str, object]:
    runtime = config or get_config().v2ray
    traffic = get_config().traffic
    clients = [
        {
            "id": user.uuid,
            "email": user.ldap_user_id,
            **({"flow": runtime.flow} if runtime.flow else {}),
        }
        for user in db.list_active_users()
        if user.uuid
    ]

    return {
        "log": {
            "loglevel": "warning",
        },
        "inbounds": [
            {
                "tag": "proxy-in",
                "listen": runtime.listen,
                "port": runtime.port,
                "protocol": runtime.protocol,
                "settings": {
                    "clients": clients,
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": runtime.network,
                    "security": runtime.security,
                },
            }
        ] + (
            [
                {
                    "tag": "api",
                    "listen": traffic.api_host,
                    "port": traffic.api_port,
                    "protocol": "dokodemo-door",
                    "settings": {
                        "address": traffic.api_host,
                    },
                }
            ]
            if traffic.enabled
            else []
        ),
        "stats": {} if traffic.enabled else None,
        "api": {
            "tag": "api",
            "services": [
                "StatsService",
            ],
        } if traffic.enabled else None,
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                }
            }
        } if traffic.enabled else None,
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": [
                        "api",
                    ],
                    "outboundTag": "api",
                }
            ]
        } if traffic.enabled else None,
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom",
            },
            {
                "tag": "api",
                "protocol": "freedom",
            },
            {
                "tag": "blocked",
                "protocol": "blackhole",
            },
        ],
    }


def write_v2ray_config(payload: dict[str, object], path: Path | None = None) -> None:
    target = path or get_config().v2ray.config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(_drop_none(payload), indent=2, sort_keys=False)

    with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        handle.write(serialized)
        handle.write("\n")
        temp_name = handle.name

    os.replace(temp_name, target)
    active_client_count = len(db.list_active_users())
    logger.info("v2ray_config_write path=%s clients=%s", target, active_client_count)


def sync_v2ray_config(reload_process: bool = True) -> None:
    payload = build_v2ray_config()
    write_v2ray_config(payload)
    if reload_process:
        reload_v2ray()


def sync_v2ray_config_and_restart() -> None:
    payload = build_v2ray_config()
    write_v2ray_config(payload)
    restart_v2ray()


def reload_v2ray() -> None:
    process_name = get_config().v2ray.process_name
    result = subprocess.run(
        ["pkill", "-HUP", process_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info("v2ray_reload process=%s signal=SIGHUP result=success", process_name)
        return

    if result.returncode == 1:
        logger.warning("v2ray_reload process=%s result=not_running", process_name)
        return

    stderr = result.stderr.strip()
    logger.error("v2ray_reload process=%s result=failed stderr=%s", process_name, stderr)
    raise RuntimeError(f"Failed to reload {process_name}: {stderr}")


def restart_v2ray() -> None:
    process_name = get_config().v2ray.process_name
    result = subprocess.run(
        ["pkill", "-TERM", process_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info("v2ray_restart process=%s signal=SIGTERM result=success", process_name)
        return

    if result.returncode == 1:
        logger.warning("v2ray_restart process=%s result=not_running", process_name)
        return

    stderr = result.stderr.strip()
    logger.error("v2ray_restart process=%s result=failed stderr=%s", process_name, stderr)
    raise RuntimeError(f"Failed to restart {process_name}: {stderr}")


def _drop_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _drop_none(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_drop_none(child) for child in value]
    return value
