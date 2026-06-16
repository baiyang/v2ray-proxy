from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


class ConfigError(RuntimeError):
    """Raised when application configuration is invalid."""


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"Missing required environment variable: {name}")

    return ENV_PATTERN.sub(replace, value)


def _expand_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_tree(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand_tree(child) for child in value]
    if isinstance(value, str):
        return _expand_env(value)
    return value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _auto_string(value: Any) -> str:
    text = str(value or "").strip()
    return text or "auto"


@dataclass(frozen=True)
class LdapConfig:
    server_uri: str
    bind_dn: str
    bind_password: str
    user_ou: str
    user_filter: str
    attributes: dict[str, str]
    connect_timeout_seconds: int


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class V2RayConfig:
    binary: str
    process_name: str
    config_path: Path
    listen: str
    port: int
    protocol: str
    network: str
    security: str
    flow: str
    alter_id: int


@dataclass(frozen=True)
class SubscriptionConfig:
    listen: str
    port: int
    public_base_url: str
    secret_key: str
    token_ttl_seconds: int
    node_name: str
    public_host: str
    public_port: int
    scheme: str
    workers: int
    threads: int
    routing_mode: str
    custom_rules: list[str]


@dataclass(frozen=True)
class AdminConfig:
    username: str
    password: str


@dataclass(frozen=True)
class TrafficConfig:
    enabled: bool
    interval_seconds: int
    timezone: str
    default_daily_limit_gb: float
    default_daily_limit_bytes: int
    api_host: str
    api_port: int


@dataclass(frozen=True)
class SyncConfig:
    lock_path: Path
    cron: str


@dataclass(frozen=True)
class AppConfig:
    ldap: LdapConfig
    database: DatabaseConfig
    v2ray: V2RayConfig
    subscription: SubscriptionConfig
    admin: AdminConfig
    traffic: TrafficConfig
    sync: SyncConfig


def _require(mapping: dict[str, Any], key: str) -> Any:
    value = mapping.get(key)
    if value in (None, ""):
        raise ConfigError(f"Missing required config key: {key}")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a YAML mapping")
    return _expand_tree(raw)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    path = Path(os.environ.get("APP_CONFIG_PATH", "/app/config.yaml"))
    raw = _load_yaml(path)

    ldap = raw.get("ldap") or {}
    ldap_attributes = ldap.get("attributes") or {}
    if not isinstance(ldap_attributes, dict):
        raise ConfigError("ldap.attributes must be a mapping")

    database = raw.get("database") or {}
    v2ray = raw.get("v2ray") or {}
    subscription = raw.get("subscription") or {}
    routing = subscription.get("routing") or {}
    custom_rules = routing.get("custom_rules") or []
    if not isinstance(custom_rules, list):
        raise ConfigError("subscription.routing.custom_rules must be a list")
    admin = raw.get("admin") or {}
    traffic = raw.get("traffic") or {}
    sync = raw.get("sync") or {}
    default_daily_limit_gb = float(traffic.get("default_daily_limit_gb", 20))

    return AppConfig(
        ldap=LdapConfig(
            server_uri=str(_require(ldap, "server_uri")),
            bind_dn=str(_require(ldap, "bind_dn")),
            bind_password=str(_require(ldap, "bind_password")),
            user_ou=str(_require(ldap, "user_ou")),
            user_filter=str(_require(ldap, "user_filter")),
            attributes={
                "username": str(ldap_attributes.get("username", "cn")),
                "name": str(ldap_attributes.get("name", "sn")),
                "email": str(ldap_attributes.get("email", "mail")),
            },
            connect_timeout_seconds=int(ldap.get("connect_timeout_seconds", 5)),
        ),
        database=DatabaseConfig(path=Path(str(_require(database, "path")))),
        v2ray=V2RayConfig(
            binary=str(v2ray.get("binary", "/usr/bin/v2ray")),
            process_name=str(v2ray.get("process_name", "v2ray")),
            config_path=Path(str(v2ray.get("config_path", "/etc/v2ray/config.json"))),
            listen=str(v2ray.get("listen", "0.0.0.0")),
            port=int(v2ray.get("port", 10086)),
            protocol=str(v2ray.get("protocol", "vless")),
            network=str(v2ray.get("network", "tcp")),
            security=str(v2ray.get("security", "none")),
            flow=str(v2ray.get("flow", "")),
            alter_id=int(v2ray.get("alter_id", 0)),
        ),
        subscription=SubscriptionConfig(
            listen=str(subscription.get("listen", "0.0.0.0")),
            port=int(subscription.get("port", 8080)),
            public_base_url=_auto_string(subscription.get("public_base_url", "auto")).rstrip("/"),
            secret_key=str(_require(subscription, "secret_key")),
            token_ttl_seconds=int(subscription.get("token_ttl_seconds", 0)),
            node_name=str(subscription.get("node_name", "enterprise-vless")),
            public_host=_auto_string(subscription.get("public_host", "auto")),
            public_port=int(subscription.get("public_port", v2ray.get("port", 10086))),
            scheme=_auto_string(subscription.get("scheme", "auto")),
            workers=int(subscription.get("workers", 2)),
            threads=int(subscription.get("threads", 4)),
            routing_mode=str(routing.get("mode", "cn_direct")),
            custom_rules=[str(rule) for rule in custom_rules],
        ),
        admin=AdminConfig(
            username=str(admin.get("username", "admin2")),
            password=str(admin.get("password", "Mdt123456!")),
        ),
        traffic=TrafficConfig(
            enabled=_bool(traffic.get("enabled", True)),
            interval_seconds=int(traffic.get("interval_seconds", 5)),
            timezone=str(traffic.get("timezone", "Asia/Shanghai")),
            default_daily_limit_gb=default_daily_limit_gb,
            default_daily_limit_bytes=int(default_daily_limit_gb * 1024 * 1024 * 1024),
            api_host=str(traffic.get("api_host", "127.0.0.1")),
            api_port=int(traffic.get("api_port", 10085)),
        ),
        sync=SyncConfig(
            lock_path=Path(str(sync.get("lock_path", "/tmp/v2ray-proxy-ldap-sync.lock"))),
            cron=str(sync.get("cron", "*/5 * * * *")),
        ),
    )


def is_development() -> bool:
    return _bool(os.environ.get("FLASK_DEBUG", "false"))
