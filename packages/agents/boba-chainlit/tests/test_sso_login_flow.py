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

import krb5
import pytest
from chainlit.auth.jwt import create_jwt
from gssapi import Credentials, Name, NameType, SecurityContext
from stand_site import Stand
from starlette.datastructures import Headers

from boba.chainlit.auth.config import KerberosAuthConfig, KerberosRolesConfig
from boba.chainlit.auth.kerberos import (
    KerberosAuth,
    SpnegoMiddleware,
    SsoRefresh,
    SsoRuntime,
)
from boba.chainlit.infra.session import ChainlitSession
from boba.identity.roles import RoleExcludeConfig
from boba.identity.session import UserMetadataField
from boba.krb import KerberosEnv, RefreshWaiters, ServiceTicketIssuer
from boba.settings import bind

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
    """Прогон /auth/sso через боевую middleware; итог — заголовки для _build_user."""

    @staticmethod
    async def headers(auth: KerberosAuth, token: bytes) -> Headers:
        import base64

        captured: dict[str, Headers] = {}

        async def app(scope: Any, receive: Any, send: Any) -> None:
            captured["headers"] = Headers(scope=scope)

        middleware = SpnegoMiddleware(app, sso=auth.runtime())
        scope = {
            "type": "http",
            "path": auth._urls.sso,
            "headers": [(b"authorization", b"Negotiate " + base64.b64encode(token))],
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


class Refresh:
    """Прогон /auth/sso/refresh: молчаливый повторный обмен живой сессии."""

    @staticmethod
    async def status(
        auth: KerberosAuth,
        token: bytes,
        jwt_cookie: str | None,
        own_header: bool = True,
        runtime: SsoRuntime | None = None,
    ) -> int:
        import base64

        from chainlit.auth.cookie import _auth_cookie_name

        async def app(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("refresh must not fall through to the application")

        parts = runtime
        if parts is None:
            parts = auth.runtime()

        middleware = SpnegoMiddleware(app, sso=parts)
        headers = [(b"authorization", b"Negotiate " + base64.b64encode(token))]
        if own_header:
            headers.append((SsoRefresh.HEADER.encode(), SsoRefresh.VALUE.encode()))

        if jwt_cookie is not None:
            cookie = f"{_auth_cookie_name}={jwt_cookie}".encode()
            headers.append((b"cookie", cookie))

        scope = {
            "type": "http",
            "path": auth._urls.refresh,
            "headers": headers,
            "client": ("127.0.0.1", 1234),
        }
        captured: dict[str, int] = {}

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = int(message["status"])

        await middleware(scope, receive, send)

        status = captured.get("status")
        if status is None:
            raise AssertionError("refresh must answer the request itself")

        return status


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

    sso = ChainlitSession.ticket_of_token(create_jwt(user))
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

    if ChainlitSession.ticket_of_token(create_jwt(stale)) is not None:
        raise AssertionError("a sign-in without labels must not resolve")


async def test_refresh_puts_new_credentials_under_the_same_login(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Тикет входа на исходе, а сессия жива: обмен обновляет его под той же меткой."""
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

    token = create_jwt(user)
    sso = ChainlitSession.ticket_of_token(token)
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the labels")

    before = kerberos_auth.registry.of_login(sso.login)
    if before is None:
        raise AssertionError("the sign-in must hold credentials")

    stamp = _ccache_stamp(before.ccache)

    status = await Refresh.status(kerberos_auth, Browser.token(tmp_path), token)
    if status != 204:
        raise AssertionError(f"refresh must succeed: {status}")

    credentials = kerberos_auth.registry.of_login(sso.login)
    if credentials is None:
        raise AssertionError("refresh must keep the credentials of that login")
    if _ccache_stamp(credentials.ccache) == stamp:
        raise AssertionError("refresh must write a fresh ticket")
    if credentials.principal != USER_PRINCIPAL:
        raise AssertionError(f"credentials must stay the user's: {credentials}")

    ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
    if ticket.principal != USER_PRINCIPAL:
        raise AssertionError(f"ticket must belong to the user: {ticket.principal}")


async def test_refresh_without_a_session_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Токен без подписанного входа тикет не обновляет: обмен привязан к сессии."""
    status = await Refresh.status(kerberos_auth, Browser.token(tmp_path), None)
    if status != 403:
        raise AssertionError(f"refresh without a session must be refused: {status}")


async def test_refresh_of_another_principal_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Чужой SPNEGO-токен под чужой меткой входа: сессия остаётся при своём."""
    import chainlit as cl

    stranger = cl.User(
        identifier="stranger",
        metadata={
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: f"stranger@{STAND.krb_realm}",
            UserMetadataField.LOGIN: "login-of-a-stranger",
            UserMetadataField.ROLES: ["read"],
        },
    )

    status = await Refresh.status(
        kerberos_auth, Browser.token(tmp_path), create_jwt(stranger)
    )
    if status != 403:
        raise AssertionError(f"a foreign principal must be refused: {status}")

    if kerberos_auth.registry.of_login("login-of-a-stranger") is not None:
        raise AssertionError("refusal must leave no credentials behind")


async def test_page_script_knows_where_to_refresh(kerberos_auth: KerberosAuth) -> None:
    """Адрес обмена подставляет сервер: скрипт страницы не собирает его сам."""
    script = kerberos_auth._get_static_button()

    if kerberos_auth._urls.refresh not in script:
        raise AssertionError("sso.js must carry the refresh url")
    if SsoRefresh.HEADER not in script:
        raise AssertionError("sso.js must mark its own request with the header")
    if "__REFRESH" in script:
        raise AssertionError("the placeholders must be replaced")


def _ccache_stamp(ccache: str) -> tuple[str, ...]:
    """Слепок содержимого ccache: по нему видно, что тикет переписан."""
    context = krb5.init_context()
    cache = krb5.cc_resolve(context, ccache.encode())
    stamps: list[str] = []
    for cred in cache:
        server = krb5.unparse_name_flags(context, cred.server).decode()
        stamps.append(f"{server}:{cred.ticket.hex()}")

    return tuple(stamps)


async def test_refresh_does_not_revive_a_signed_out_session(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """После logout метка мертва: обмен её не воскрешает, нужен новый вход."""
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

    token = create_jwt(user)
    sso = ChainlitSession.ticket_of_token(token)
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the labels")

    kerberos_auth.registry.drop(sso.login)

    status = await Refresh.status(kerberos_auth, Browser.token(tmp_path), token)
    if status != 403:
        raise AssertionError(f"a signed-out label must be refused: {status}")

    if kerberos_auth.registry.of_login(sso.login) is not None:
        raise AssertionError("the refusal must leave the sign-in dead")


async def test_refresh_without_its_own_header_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Запрос чужого сайта заголовок не несёт: обмена не будет даже с cookie."""
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

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
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

    token = create_jwt(user)
    sso = ChainlitSession.ticket_of_token(token)
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the labels")

    # вход и его тикет остаются прежними, меняется только политика допуска
    runtime = kerberos_auth.runtime()
    excluded = SsoRuntime(
        urls=runtime.urls,
        config=runtime.config,
        acceptor=runtime.acceptor,
        delegation=runtime.delegation,
        admission=excluding_auth,
    )

    status = await Refresh.status(
        kerberos_auth, Browser.token(tmp_path), token, runtime=excluded
    )
    if status != 403:
        raise AssertionError(f"an excluded principal must be refused: {status}")


async def test_refresh_wakes_the_waiting_tool_call(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Вызов инструмента ждёт обмена: настоящий обмен снимает ожидание."""
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

    token = create_jwt(user)
    sso = ChainlitSession.ticket_of_token(token)
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the labels")

    with kerberos_auth.registry.arm_refresh(sso.login) as waiting:
        status = await Refresh.status(kerberos_auth, Browser.token(tmp_path), token)
        if status != 204:
            raise AssertionError(f"refresh must succeed: {status}")

        if not await waiting.wait(RefreshWaiters.TIMEOUT_SEC):
            raise AssertionError("the refreshed sign-in must end the wait")


async def test_a_failed_refresh_leaves_the_caller_waiting(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Отказ обмена ожидание не снимает: вызов дождётся таймаута и объяснит отказ."""
    headers = await Sso.headers(kerberos_auth, Browser.token(tmp_path))
    user = await kerberos_auth._build_user(headers)
    if user is None:
        raise AssertionError("SSO must build a user")

    token = create_jwt(user)
    sso = ChainlitSession.ticket_of_token(token)
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the labels")

    with kerberos_auth.registry.arm_refresh(sso.login) as waiting:
        status = await Refresh.status(
            kerberos_auth, Browser.token(tmp_path), token, own_header=False
        )
        if status != 403:
            raise AssertionError(f"cross-site refresh must be refused: {status}")

        if await waiting.wait(0.2):
            raise AssertionError("a refused refresh must not end the wait")
