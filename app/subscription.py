from __future__ import annotations

import base64
import io
import urllib.parse
from dataclasses import dataclass
from typing import Mapping

import qrcode
import yaml
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import SubscriptionConfig, V2RayConfig, get_config
from app.db import UserRecord


@dataclass(frozen=True)
class PublicEndpoint:
    base_url: str
    proxy_host: str


def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_config().subscription.secret_key, salt="v2ray-proxy-subscription")


def make_token(ldap_user_id: str) -> str:
    return serializer().dumps({"user": ldap_user_id})


def parse_token(token: str) -> str | None:
    config = get_config().subscription
    try:
        payload = serializer().loads(
            token,
            max_age=config.token_ttl_seconds if config.token_ttl_seconds > 0 else None,
        )
    except (BadSignature, SignatureExpired):
        return None
    user = payload.get("user") if isinstance(payload, dict) else None
    return str(user) if user else None


def resolve_public_endpoint(
    headers: Mapping[str, str],
    request_scheme: str,
    request_host: str,
) -> PublicEndpoint:
    config = get_config().subscription
    forwarded_host = _header_value(headers, "X-Forwarded-Host")
    host = forwarded_host or _header_value(headers, "Host") or request_host
    scheme = _header_value(headers, "X-Forwarded-Proto") or request_scheme

    if config.scheme != "auto":
        scheme = config.scheme

    base_url = config.public_base_url
    if base_url == "auto":
        base_url = f"{scheme}://{host}".rstrip("/")

    proxy_host = config.public_host
    if proxy_host == "auto":
        proxy_host = _strip_port(host)

    return PublicEndpoint(base_url=base_url, proxy_host=proxy_host)


def subscription_url(token: str, endpoint: PublicEndpoint) -> str:
    return f"{endpoint.base_url}/subscribe/{urllib.parse.quote(token)}"


def vless_uri(user: UserRecord, endpoint: PublicEndpoint) -> str:
    config = get_config()
    sub: SubscriptionConfig = config.subscription
    v2ray: V2RayConfig = config.v2ray
    if not user.uuid:
        raise ValueError("User has no UUID")

    params = {
        "type": v2ray.network,
        "security": v2ray.security,
        "encryption": "none",
    }
    if v2ray.flow:
        params["flow"] = v2ray.flow
    query = urllib.parse.urlencode(params)
    label = urllib.parse.quote(f"{sub.node_name}-{user.ldap_user_id}")
    return f"vless://{user.uuid}@{endpoint.proxy_host}:{sub.public_port}?{query}#{label}"


def clash_yaml(user: UserRecord, endpoint: PublicEndpoint) -> str:
    config = get_config()
    sub = config.subscription
    v2ray = config.v2ray
    if not user.uuid:
        raise ValueError("User has no UUID")

    node_name = f"{sub.node_name}-{user.ldap_user_id}"
    proxy = {
        "name": node_name,
        "type": "vless",
        "server": endpoint.proxy_host,
        "port": sub.public_port,
        "uuid": user.uuid,
        "network": v2ray.network,
        "tls": v2ray.security == "tls",
        "udp": True,
    }
    if v2ray.flow:
        proxy["flow"] = v2ray.flow

    payload = {
        "proxies": [proxy],
        "proxy-groups": [
            {
                "name": "Proxy",
                "type": "select",
                "proxies": [node_name, "DIRECT"],
            }
        ],
        "rules": clash_rules(),
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def qrcode_data_uri(value: str) -> str:
    image = qrcode.make(value)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def clash_rules() -> list[str]:
    config = get_config().subscription
    if config.routing_mode == "custom":
        if not config.custom_rules:
            raise ValueError("custom routing mode requires subscription.routing.custom_rules")
        return config.custom_rules
    if config.routing_mode == "global_proxy":
        return ["MATCH,Proxy"]
    if config.routing_mode == "direct":
        return ["MATCH,DIRECT"]
    if config.routing_mode == "cn_direct":
        return [
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "GEOIP,CN,DIRECT",
            "MATCH,Proxy",
        ]
    raise ValueError(f"Unsupported routing mode: {config.routing_mode}")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if not value:
        return None
    return value.split(",", 1)[0].strip()


def _strip_port(host: str) -> str:
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[: end + 1]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host
