from __future__ import annotations

import logging
from dataclasses import dataclass
from string import Template
from urllib.parse import urlparse

from ldap3 import ALL, Connection, Server
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.config import LdapConfig, get_config


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LdapUser:
    user_id: str
    display_name: str | None
    email: str | None
    dn: str


class LdapService:
    def __init__(self, config: LdapConfig | None = None) -> None:
        self.config = config or get_config().ldap

    def authenticate(self, username: str, password: str) -> LdapUser | None:
        if not username or not password:
            return None

        user = self.find_user(username)
        if user is None:
            logger.info("ldap_auth user=%s result=not_found", username)
            return None

        server = self._server()
        try:
            with Connection(server, user=user.dn, password=password, auto_bind=True):
                logger.info("ldap_auth user=%s result=success", username)
                return user
        except LDAPException as exc:
            logger.info("ldap_auth user=%s result=failed error=%s", username, exc.__class__.__name__)
            return None

    def user_exists(self, user_id: str) -> bool:
        return self.find_user(user_id) is not None

    def find_user(self, user_id: str) -> LdapUser | None:
        filter_value = self._user_filter(user_id)
        attributes = sorted(set(self.config.attributes.values()))
        server = self._server()
        try:
            with Connection(
                server,
                user=self.config.bind_dn,
                password=self.config.bind_password,
                auto_bind=True,
            ) as connection:
                connection.search(
                    search_base=self.config.user_ou,
                    search_filter=filter_value,
                    attributes=attributes,
                    size_limit=1,
                )
                if not connection.entries:
                    return None
                entry = connection.entries[0]
                return self._entry_to_user(user_id, entry)
        except LDAPException as exc:
            logger.error("ldap_search user=%s result=error error=%s", user_id, exc.__class__.__name__)
            raise

    def _server(self) -> Server:
        parsed = urlparse(self.config.server_uri)
        if parsed.scheme in {"ldap", "ldaps"}:
            default_port = 636 if parsed.scheme == "ldaps" else 389
            return Server(
                parsed.hostname or self.config.server_uri,
                port=parsed.port or default_port,
                use_ssl=parsed.scheme == "ldaps",
                get_info=ALL,
                connect_timeout=self.config.connect_timeout_seconds,
            )

        return Server(
            self.config.server_uri,
            get_info=ALL,
            connect_timeout=self.config.connect_timeout_seconds,
        )

    def _user_filter(self, user_id: str) -> str:
        escaped = escape_filter_chars(user_id)
        return Template(self.config.user_filter.replace("%(user)s", "${user}")).substitute(user=escaped)

    def _entry_to_user(self, requested_user_id: str, entry: object) -> LdapUser:
        username_attr = self.config.attributes["username"]
        name_attr = self.config.attributes["name"]
        email_attr = self.config.attributes["email"]

        username = self._entry_attr(entry, username_attr) or requested_user_id
        display_name = self._entry_attr(entry, name_attr)
        email = self._entry_attr(entry, email_attr)
        dn = str(getattr(entry, "entry_dn"))

        return LdapUser(
            user_id=username,
            display_name=display_name,
            email=email,
            dn=dn,
        )

    @staticmethod
    def _entry_attr(entry: object, attr_name: str) -> str | None:
        value = getattr(entry, attr_name, None)
        if value is None:
            return None
        if hasattr(value, "value"):
            raw = value.value
        else:
            raw = value
        if raw is None:
            return None
        return str(raw)
