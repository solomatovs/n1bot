"""Полный путь SSO-входа: SPNEGO-токен браузера -> заголовки -> JWT -> тикет.

Стенд: живой KDC и LDAP домена. Браузер моделируется initiate-контекстом
пользователя readonly; middleware, сборка cl.User, JWT и реестр тикетов —
боевые. Тест держит связку «вход выдал метки -> обвязка нашла по ним креды».
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import krb5
import pytest
from chainlit.auth.jwt import create_jwt
from gssapi import Credentials, Name, NameType, SecurityContext
from starlette.datastructures import Headers

from boba.chainlit.auth.kerberos import (
    KerberosAuth,
    KerberosAuthConfig,
    SpnegoMiddleware,
)
from boba.chainlit.domain.session import UserMetadataField
from boba.chainlit.infra.user_connections import SsoLogin
from boba.krb import KerberosEnv, ServiceTicketIssuer
from boba.settings import bind

_REPO = Path(__file__).resolve().parents[4]
_KRB = _REPO / "compose" / "conf" / "krb"
KRB5_CONF = _KRB / "krb5.conf"
SERVICE_KEYTAB = _KRB / "boba-svc.keytab"
SERVICE_SPN = "HTTP/loshara.com@LOSHARA.COM"
USER_PRINCIPAL = "readonly@LOSHARA.COM"
TARGET = "postgres@postgres-17.loshara.com"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not SERVICE_KEYTAB.is_file() or not KRB5_CONF.is_file(),
        reason="нет keytab/krb5.conf локального AD",
    ),
]


@pytest.fixture
def krb5_env() -> Iterator[None]:
    saved = os.environ.get(KerberosEnv.CONFIG)
    os.environ[KerberosEnv.CONFIG] = str(KRB5_CONF)
    yield
    if saved is None:
        os.environ.pop(KerberosEnv.CONFIG, None)
        return
    os.environ[KerberosEnv.CONFIG] = saved


@pytest.fixture
def kerberos_auth(raw_config: Any, auth_token: str) -> KerberosAuth:
    """Провайдер SSO по боевой секции [auth.kerberos]; auth_token ставит JWT-секрет."""
    config = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
    return KerberosAuth("/boba", config)


class Browser:
    """Клиентская сторона: TGT пользователя по паролю и AP-REQ к SPN сервиса."""

    @staticmethod
    def token(tmp_path: Path) -> bytes:
        with (_REPO / "compose" / "conf" / "config.toml").open("rb") as handle:
            password = str(tomllib.load(handle)["site"]["ldap_bind_password"])

        context = krb5.init_context()
        user = krb5.parse_name_flags(context, USER_PRINCIPAL.encode())
        options = krb5.get_init_creds_opt_alloc(context)
        krb5.get_init_creds_opt_set_forwardable(options, True)
        tgt = krb5.get_init_creds_password(context, user, options, password.encode())

        ccache = f"FILE:{tmp_path / 'browser'}"
        cache = krb5.cc_resolve(context, ccache.encode())
        krb5.cc_initialize(context, cache, user)
        krb5.cc_store_cred(context, cache, tgt)

        creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
        target = Name(SERVICE_SPN, NameType.kerberos_principal)
        initiator = SecurityContext(
            name=target, creds=creds, usage="initiate", flags=0
        )
        return initiator.step()


class Sso:
    """Прогон /auth/sso через боевую middleware; итог — заголовки для _build_user."""

    @staticmethod
    async def headers(auth: KerberosAuth, token: bytes) -> Headers:
        import base64

        captured: dict[str, Headers] = {}

        async def app(scope: Any, receive: Any, send: Any) -> None:
            captured["headers"] = Headers(scope=scope)

        middleware = SpnegoMiddleware(
            app,
            urls=auth._urls,
            config=auth._config,
            acceptor=auth.acceptor,
            delegation=auth.delegation,
        )
        scope = {
            "type": "http",
            "path": auth._urls.sso,
            "headers": [
                (b"authorization", b"Negotiate " + base64.b64encode(token))
            ],
            "client": ("127.0.0.1", 1234),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: Any) -> None:
            return

        await middleware(scope, receive, send)

        found = captured.get("headers")
        if found is None:
            raise AssertionError("SPNEGO did not pass the request through")

        return found


async def test_sign_in_puts_principal_and_login_into_the_jwt(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

    metadata = user.metadata
    if metadata.get(UserMetadataField.PRINCIPAL) != USER_PRINCIPAL:
        raise AssertionError(f"principal must be in metadata: {metadata}")
    if not metadata.get(UserMetadataField.LOGIN):
        raise AssertionError(f"login label must be in metadata: {metadata}")
    if not metadata.get(UserMetadataField.ROLES):
        raise AssertionError(f"roles must be mapped: {metadata}")

    sso = SsoLogin.of_token(create_jwt(user))
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the labels")
    if sso.principal != USER_PRINCIPAL:
        raise AssertionError(sso.principal)

    credentials = kerberos_auth.registry.of_login(sso.login)
    if credentials is None:
        raise AssertionError("registry must hold the credentials of that login")

    ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
    if ticket.principal != USER_PRINCIPAL:
        raise AssertionError(f"ticket must belong to the user: {ticket.principal}")


async def test_stale_jwt_without_labels_is_refused(
    kerberos_auth: KerberosAuth,
) -> None:
    """JWT прошлой версии приложения: провайдер есть, меток нет."""
    import chainlit as cl

    stale = cl.User(
        identifier="readonly",
        metadata={
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.ROLES: ["read"],
        },
    )

    if SsoLogin.of_token(create_jwt(stale)) is not None:
        raise AssertionError("a sign-in without labels must not resolve")
