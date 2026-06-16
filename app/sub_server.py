from __future__ import annotations

import logging
from datetime import datetime
from functools import wraps
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app import db
from app.config import get_config, is_development
from app.ldap_service import LdapService
from app.subscription import (
    clash_yaml,
    make_token,
    parse_token,
    qrcode_data_uri,
    resolve_public_endpoint,
    PublicEndpoint,
    subscription_url,
    vless_uri,
)
from app.xray_manager import sync_v2ray_config, sync_v2ray_config_and_restart


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., object])


def create_app() -> Flask:
    config = get_config()
    db.init_db()
    app = Flask(__name__)
    app.secret_key = config.subscription.secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    @app.get("/")
    def index() -> Response | str:
        if session.get("user_id"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        if request.method != "POST":
            return render_template("login.html")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ldap_user = LdapService().authenticate(username, password)
        if ldap_user is None:
            flash("LDAP 用户名或密码不正确")
            return render_template("login.html"), 401

        session.clear()
        session["user_id"] = ldap_user.user_id
        session["display_name"] = ldap_user.display_name
        session["email"] = ldap_user.email
        return redirect(url_for("dashboard"))

    @app.get("/logout")
    def logout() -> Response:
        session.clear()
        return redirect(url_for("login"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login() -> Response | str:
        if request.method != "POST":
            return render_template("admin_login.html")

        admin = get_config().admin
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username != admin.username or password != admin.password:
            flash("管理员用户名或密码不正确")
            return render_template("admin_login.html"), 401

        session.clear()
        session["admin_user"] = admin.username
        return redirect(url_for("admin_dashboard"))

    @app.get("/admin/logout")
    def admin_logout() -> Response:
        session.clear()
        return redirect(url_for("admin_login"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard() -> str:
        endpoint = resolve_public_endpoint(request.headers, request.scheme, request.host)
        today = current_traffic_day()
        users = [
            {
                "user": user,
                "subscription": subscription_payload(user, endpoint) if user.status == db.ACTIVE and user.uuid else None,
                "traffic": traffic_summary(user, today),
            }
            for user in db.list_users()
        ]
        return render_template("admin_dashboard.html", users=users)

    @app.get("/admin/users/<path:user_id>")
    @admin_required
    def admin_user_detail(user_id: str) -> Response | str:
        user = db.get_user(user_id)
        if user is None:
            return Response("user not found\n", status=404, mimetype="text/plain")
        endpoint = resolve_public_endpoint(request.headers, request.scheme, request.host)
        today = current_traffic_day()
        history = db.list_daily_traffic(user_id, datetime.fromisoformat(today).date(), 30)
        max_total = max((row.uplink_bytes + row.downlink_bytes for row in history), default=0)
        return render_template(
            "admin_user_detail.html",
            user=user,
            subscription=subscription_payload(user, endpoint) if user.status == db.ACTIVE and user.uuid else None,
            traffic=traffic_summary(user, today),
            history=[
                {
                    "day": row.day,
                    "uplink": row.uplink_bytes,
                    "downlink": row.downlink_bytes,
                    "uplink_label": format_bytes(row.uplink_bytes),
                    "downlink_label": format_bytes(row.downlink_bytes),
                    "total": row.uplink_bytes + row.downlink_bytes,
                    "total_label": format_bytes(row.uplink_bytes + row.downlink_bytes),
                    "uplink_height": chart_height(row.uplink_bytes, max_total),
                    "downlink_height": chart_height(row.downlink_bytes, max_total),
                }
                for row in history
            ],
        )

    @app.post("/admin/users/<path:user_id>/quota")
    @admin_required
    def admin_update_quota(user_id: str) -> Response:
        limit_gb = request.form.get("daily_limit_gb", "").strip()
        try:
            limit_bytes = int(float(limit_gb) * 1024 * 1024 * 1024)
        except ValueError:
            flash("每日额度必须是数字")
            return redirect(url_for("admin_user_detail", user_id=user_id))
        if limit_bytes <= 0:
            flash("每日额度必须大于 0")
            return redirect(url_for("admin_user_detail", user_id=user_id))

        db.set_daily_traffic_limit(user_id, limit_bytes)
        logger.info("admin_quota_update user=%s limit_bytes=%s", user_id, limit_bytes)
        return redirect(url_for("admin_user_detail", user_id=user_id))

    @app.post("/admin/users")
    @admin_required
    def admin_create_user() -> Response:
        user_id = request.form.get("user_id", "").strip()
        display_name = request.form.get("display_name", "").strip() or user_id
        email = request.form.get("email", "").strip() or None
        if not user_id:
            flash("用户名不能为空")
            return redirect(url_for("admin_dashboard"))

        user = db.ensure_subscription_user(
            ldap_user_id=user_id,
            display_name=display_name,
            email=email,
            is_ldap=False,
            force_reactivate=True,
        )
        sync_v2ray_config(reload_process=True)
        logger.info("admin_user_create user=%s uuid=%s", user.ldap_user_id, user.uuid)
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/users/<path:user_id>/revoke")
    @admin_required
    def admin_revoke_user(user_id: str) -> Response:
        db.revoke_user(user_id)
        sync_v2ray_config_and_restart()
        logger.info("admin_user_revoke user=%s", user_id)
        return redirect(url_for("admin_dashboard"))

    @app.get("/dashboard")
    @login_required
    def dashboard() -> str:
        user_id = str(session["user_id"])
        user = db.get_user(user_id)
        subscription = None
        if user and user.status == db.ACTIVE and user.uuid:
            endpoint = resolve_public_endpoint(request.headers, request.scheme, request.host)
            subscription = subscription_payload(user, endpoint)
        return render_template("dashboard.html", user_id=user_id, subscription=subscription)

    @app.post("/subscription")
    @login_required
    def create_subscription() -> Response:
        user_id = str(session["user_id"])
        user = db.ensure_subscription_user(
            ldap_user_id=user_id,
            display_name=session.get("display_name"),
            email=session.get("email"),
        )
        sync_v2ray_config(reload_process=True)
        logger.info("subscription_create user=%s uuid=%s", user.ldap_user_id, user.uuid)
        return redirect(url_for("dashboard"))

    @app.get("/subscribe/<token>")
    def subscribe(token: str) -> Response:
        user_id = parse_token(token)
        if user_id is None:
            return Response("invalid subscription token\n", status=403, mimetype="text/plain")
        user = db.get_user(user_id)
        if user is None or user.status != db.ACTIVE or not user.uuid:
            return Response("subscription revoked\n", status=403, mimetype="text/plain")
        endpoint = resolve_public_endpoint(request.headers, request.scheme, request.host)
        return Response(clash_yaml(user, endpoint), mimetype="text/yaml; charset=utf-8")

    return app


def login_required(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def admin_required(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        if not session.get("admin_user"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def subscription_payload(user: db.UserRecord, endpoint: PublicEndpoint) -> dict[str, str]:
    token = make_token(user.ldap_user_id)
    url = subscription_url(token, endpoint)
    return {
        "url": url,
        "qr": qrcode_data_uri(url),
        "vless": vless_uri(user, endpoint),
    }


def current_traffic_day() -> str:
    tz = ZoneInfo(get_config().traffic.timezone)
    return datetime.now(tz).date().isoformat()


def traffic_summary(user: db.UserRecord, day: str) -> dict[str, object]:
    daily = db.get_daily_traffic(user.ldap_user_id, day)
    limit = user.daily_traffic_limit_bytes or get_config().traffic.default_daily_limit_bytes
    total_bytes = daily.uplink_bytes + daily.downlink_bytes
    percent = min(100, round((total_bytes / limit) * 100, 1)) if limit else 0
    return {
        "day": day,
        "uplink_bytes": daily.uplink_bytes,
        "downlink_bytes": daily.downlink_bytes,
        "total_bytes": total_bytes,
        "uplink": format_bytes(daily.uplink_bytes),
        "downlink": format_bytes(daily.downlink_bytes),
        "total": format_bytes(total_bytes),
        "limit_bytes": limit,
        "limit_gb": round(limit / 1024 / 1024 / 1024, 2),
        "limit": format_bytes(limit),
        "percent": percent,
    }


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024


def chart_height(value: int, max_value: int) -> int:
    if max_value <= 0 or value <= 0:
        return 2
    return max(2, round((value / max_value) * 180))


if __name__ == "__main__":
    cfg = get_config().subscription
    create_app().run(host=cfg.listen, port=cfg.port, debug=is_development())
