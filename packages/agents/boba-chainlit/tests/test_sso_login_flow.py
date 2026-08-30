"""Полный путь SSO-входа: SPNEGO-токен браузера -> заголовки -> JWT -> тикет.

Стенд: живой KDC и LDAP домена. Браузер моделируется initiate-контекстом
пользователя readonly; middleware, сборка cl.User, JWT и реестр тикетов —
боевые. Тест держит связку «вход выдал метки -> обвязка нашла по ним креды».
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import chainlit as cl
import krb5
import pytest
from chainlit.auth.jwt import create_jwt
from gssapi import Credentials, Name, NameType, SecurityContext
from starlette.requests import Request

from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.infra.session import ChainlitSession
from boba.identity.roles import RoleExcludeConfig
from boba.identity.session import SignInProvider, UserMetadataField
from boba.krb import KerberosEnv, ServiceTicketIssuer
from boba.runtime.auth_config import KerberosAuthConfig, KerberosRolesConfig
from boba.runtime.sso import Challenge, Signed, SsoRefresh
from boba.config import bind
from boba.stand.site import Stand

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_SPN = f"HTTP/{STAND.krb_domain}@{STAND.krb_realm}"
USER_PRINCIPAL = STAND.reader_principal
TARGET = STAND.pg_spn

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        not STAND.live(),
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


@pytest.fixture
def excluding_auth(raw_config: Any, auth_token: str) -> KerberosAuth:
    """Тот же SSO, но принципал стенда попал в список исключённых AD."""
    config = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
    roles = config.roles
    if roles is None:
        roles = KerberosRolesConfig()

    excluded = roles.model_copy(
        update={"principal_ex": RoleExcludeConfig([USER_PRINCIPAL])}
    )
    return KerberosAuth("/boba", config.model_copy(update={"roles": excluded}))


class Browser:
    """Клиентская сторона: TGT пользователя по паролю и AP-REQ к SPN сервиса."""

    @staticmethod
    def token(tmp_path: Path) -> bytes:
        password = STAND.reader_password.get_secret_value()

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
        initiator = SecurityContext(name=target, creds=creds, usage="initiate", flags=0)
        return initiator.step()


class Sso:
    """Прогон /auth/sso через боевой обмен: исход handshake."""

    @staticmethod
    async def signed(auth: KerberosAuth, token: bytes) -> Signed:
        import base64

        scope = {
            "type": "http",
            "method": "GET",
            "path": auth._urls.sso,
            "query_string": b"",
            "headers": [(b"authorization", b"Negotiate " + base64.b64encode(token))],
            "client": ("127.0.0.1", 1234),
        }
        outcome = await auth.gate.handshake(Request(scope))
        if isinstance(outcome, Challenge):
            raise AssertionError(f"SPNEGO must sign the browser in: {outcome.reason}")

        return outcome


class Refresh:
    """Прогон /auth/sso/refresh: молчаливый повторный обмен живой сессии."""

    @staticmethod
    async def exchange(
        auth: KerberosAuth,
        token: bytes,
        jwt_cookie: str | None,
        own_header: bool = True,
    ) -> tuple[int, str]:
        """Статус ответа и JWT из его Set-Cookie (пустой — cookie не выдана)."""
        import base64

        from chainlit.auth.cookie import _auth_cookie_name

        headers = [(b"authorization", b"Negotiate " + base64.b64encode(token))]
        if own_header:
            headers.append((SsoRefresh.HEADER.encode(), SsoRefresh.VALUE.encode()))
        if jwt_cookie is not None:
            cookie = f"{_auth_cookie_name}={jwt_cookie}".encode()
            headers.append((b"cookie", cookie))
        scope = {
            "type": "http",
            "method": "GET",
            "path": auth._urls.refresh,
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
        }

        response = await auth.refresh(Request(scope))
        raw = [(key, value) for key, value in response.raw_headers]

        return response.status_code, Refresh._token_of(raw, _auth_cookie_name)

    @staticmethod
    async def status(
        auth: KerberosAuth,
        token: bytes,
        jwt_cookie: str | None,
        own_header: bool = True,
    ) -> int:
        status, _ = await Refresh.exchange(auth, token, jwt_cookie, own_header)
        return status

    @staticmethod
    def _token_of(headers: list[tuple[bytes, bytes]], name: str) -> str:
        """JWT из Set-Cookie: целиком либо из чанков name_0..name_n по порядку."""
        chunks: dict[int, str] = {}
        whole = ""
        for key, value in headers:
            if key.lower() != b"set-cookie":
                continue

            cookie = value.decode().split(";", 1)[0]
            cookie_name, _, raw_value = cookie.partition("=")
            # удаление cookie приходит как пустое значение в кавычках
            cookie_value = raw_value.strip('"')
            if cookie_name == name and cookie_value:
                whole = cookie_value
                continue

            prefix = f"{name}_"
            if cookie_name.startswith(prefix) and cookie_value:
                chunks[int(cookie_name[len(prefix) :])] = cookie_value

        if whole:
            return whole

        return "".join(chunks[index] for index in sorted(chunks))


async def _signed_in(auth: KerberosAuth, tmp_path: Path) -> cl.User:
    outcome = await Sso.signed(auth, Browser.token(tmp_path))
    return KerberosAuth.user_of(outcome.signed)


async def test_sign_in_puts_principal_and_ticket_into_the_jwt(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    user = await _signed_in(kerberos_auth, tmp_path)
    metadata = user.metadata
    if metadata.get(UserMetadataField.PRINCIPAL) != USER_PRINCIPAL:
        raise AssertionError(f"principal must be in metadata: {metadata}")
    if not metadata.get(UserMetadataField.TICKET):
        raise AssertionError(f"sealed ticket must be in metadata: {metadata}")
    if not metadata.get(UserMetadataField.ROLES):
        raise AssertionError(f"roles must be mapped: {metadata}")

    sso = ChainlitSession.ticket_of_token(create_jwt(user))
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the ticket")
    if sso.principal != USER_PRINCIPAL:
        raise AssertionError(sso.principal)

    tickets = kerberos_auth.tickets()
    credentials = tickets.credentials_of(tickets.open(sso.sealed))
    ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
    if ticket.principal != USER_PRINCIPAL:
        raise AssertionError(f"ticket must belong to the user: {ticket.principal}")


async def test_users_row_keeps_no_ticket(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Билет живёт только в JWT: копия для строки users идёт без него."""
    user = await _signed_in(kerberos_auth, tmp_path)
    stored = KerberosAuth._without_ticket(user)
    if UserMetadataField.TICKET in stored.metadata:
        raise AssertionError(
            f"the users row must not carry the ticket: {stored.metadata}"
        )
    if stored.metadata.get(UserMetadataField.PRINCIPAL) != USER_PRINCIPAL:
        raise AssertionError(f"the rest of metadata must survive: {stored.metadata}")


async def test_stale_jwt_without_ticket_is_refused(
    kerberos_auth: KerberosAuth,
) -> None:
    """JWT прошлой версии приложения: провайдер есть, билета нет."""
    stale = cl.User(
        identifier="readonly",
        metadata={
            UserMetadataField.PROVIDER: SignInProvider.KERBEROS,
            UserMetadataField.ROLES: ["read"],
        },
    )
    if ChainlitSession.ticket_of_token(create_jwt(stale)) is not None:
        raise AssertionError("a sign-in without a ticket must not resolve")


async def test_refresh_issues_a_new_jwt_with_a_fresh_ticket(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Билет входа на исходе, а сессия жива: обмен выдаёт новый JWT с новым билетом."""
    user = await _signed_in(kerberos_auth, tmp_path)
    token = create_jwt(user)
    before = ChainlitSession.ticket_of_token(token)
    if before is None:
        raise AssertionError("JWT of the sign-in must carry the ticket")

    status, renewed = await Refresh.exchange(
        kerberos_auth, Browser.token(tmp_path), token
    )
    if status != 204:
        raise AssertionError(f"refresh must succeed: {status}")
    if not renewed:
        raise AssertionError("refresh must set a new session cookie")

    after = ChainlitSession.ticket_of_token(renewed)
    if after is None:
        raise AssertionError(
            f"the new JWT must carry a ticket: {len(renewed)} chars, "
            f"decoded {ChainlitSession.user_of_token(renewed)}"
        )
    if after.sealed == before.sealed:
        raise AssertionError("refresh must seal a fresh ticket")
    if after.principal != USER_PRINCIPAL:
        raise AssertionError(f"the ticket must stay the user's: {after.principal}")

    tickets = kerberos_auth.tickets()
    credentials = tickets.credentials_of(tickets.open(after.sealed))
    ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
    if ticket.principal != USER_PRINCIPAL:
        raise AssertionError(f"ticket must belong to the user: {ticket.principal}")


async def test_refresh_without_a_session_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Токен без подписанного входа билет не обновляет: обмен привязан к сессии."""
    status = await Refresh.status(kerberos_auth, Browser.token(tmp_path), None)
    if status != 403:
        raise AssertionError(f"refresh without a session must be refused: {status}")


async def test_refresh_of_another_principal_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Чужой SPNEGO-токен под чужой сессией: сессия остаётся при своём."""
    stranger = cl.User(
        identifier="stranger",
        metadata={
            UserMetadataField.PROVIDER: SignInProvider.KERBEROS,
            UserMetadataField.PRINCIPAL: f"stranger@{STAND.krb_realm}",
            UserMetadataField.TICKET: "sealed-of-a-stranger",
            UserMetadataField.ROLES: ["read"],
        },
    )
    status, renewed = await Refresh.exchange(
        kerberos_auth, Browser.token(tmp_path), create_jwt(stranger)
    )
    if status != 403:
        raise AssertionError(f"a foreign principal must be refused: {status}")
    if renewed:
        raise AssertionError("refusal must not issue a cookie")


async def test_page_script_knows_where_to_refresh(kerberos_auth: KerberosAuth) -> None:
    """Адрес обмена подставляет сервер: скрипт страницы не собирает его сам."""
    script = kerberos_auth._get_static_button()
    if kerberos_auth._urls.refresh not in script:
        raise AssertionError("sso.js must carry the refresh url")
    if SsoRefresh.HEADER not in script:
        raise AssertionError("sso.js must mark its own request with the header")
    if "__REFRESH" in script:
        raise AssertionError("the placeholders must be replaced")


async def test_refresh_without_its_own_header_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Запрос чужого сайта заголовок не несёт: обмена не будет даже с cookie."""
    user = await _signed_in(kerberos_auth, tmp_path)
    status = await Refresh.status(
        kerberos_auth, Browser.token(tmp_path), create_jwt(user), own_header=False
    )
    if status != 403:
        raise AssertionError(f"cross-site refresh must be refused: {status}")


async def test_refresh_of_an_excluded_principal_is_refused(
    kerberos_auth: KerberosAuth,
    excluding_auth: KerberosAuth,
    tmp_path: Path,
    krb5_env: None,
) -> None:
    """Запрет в AD появился после входа: обмен спрашивает допуск заново."""
    user = await _signed_in(kerberos_auth, tmp_path)
    token = create_jwt(user)

    # вход и его билет остаются прежними, меняется только политика допуска
    status = await Refresh.status(excluding_auth, Browser.token(tmp_path), token)
    if status != 403:
        raise AssertionError(f"an excluded principal must be refused: {status}")
