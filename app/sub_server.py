from __future__ import annotations

import logging
from functools import wraps
from typing import Callable, TypeVar

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
        users = [
            {
                "user": user,
                "subscription": subscription_payload(user, endpoint) if user.status == db.ACTIVE and user.uuid else None,
            }
            for user in db.list_users()
        ]
        return render_template("admin_dashboard.html", users=users)

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


if __name__ == "__main__":
    cfg = get_config().subscription
    create_app().run(host=cfg.listen, port=cfg.port, debug=is_development())
