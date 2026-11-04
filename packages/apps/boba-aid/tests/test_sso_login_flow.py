"""Полный путь SSO-входа: SPNEGO-токен браузера -> подписанная сессия -> тикет.

Стенд: живой KDC и LDAP домена. Браузер моделируется initiate-контекстом
пользователя readonly; маршруты, запись users, JWT и реестр тикетов — боевые.
Тест проверяет, что билет лежит в подписанной сессии и открывается через MCP.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import krb5
import pytest
from aid_stand import StandTokens
from gssapi import Credentials, Name, NameType, SecurityContext
from starlette.requests import Request

from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.kerberos import KerberosAuth
from boba.aid.auth.policy import SessionRenewal
from boba.aid.auth.refresh import PageUrls, SessionRefresh
from boba.aid.auth.session import BrowserSessions, IssuedSession
from boba.aid.auth.tokens import JwtTokens
from boba.aid.data.users import UsersTable
from boba.aid.infra.config import AppConfig
from boba.aid.infra.session import ChainlitSessions
from boba.auth import SessionService
from boba.auth.config import (
    KerberosAuthConfig,
    KerberosRoleProviders,
    PrincipalRolesConfig,
)
from boba.auth.transport.http import OwnRequest
from boba.chat.profiles import ChatProfiles
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.admission import RoleExcludeConfig
from boba.identity.context import DelegatedTicket
from boba.identity.errors import AuthorizationError
from boba.identity.principal import IdentityRule
from boba.identity.sso import SsoChallenge, SsoRequest, SsoSigned
from boba.krb import KerberosEnv, ServiceTicketIssuer
from boba.krb.seal import SsoTickets, TicketSealer
from boba.stand.migrate import StandMigrations
from boba.stand.signin import SignInStand
from boba.stand.site import Stand

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_SPN = f"HTTP/{STAND.krb_domain}@{STAND.krb_realm}"
USER_PRINCIPAL = STAND.reader_principal
TARGET = STAND.pg_spn

APP_ROOT = Path(__file__).resolve().parents[1] / "assets"
"""Каталог chainlit стенда — ассеты пакета: там лежит собранный скрипт страницы."""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.failif(
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
async def users(
    runtime_config: AppConfig,
    pool: AsyncPostgresPool,
    test_postgres: PostgresConfig,
) -> UsersTable:
    """Таблица users в базе набора: тест создаёт её сам, не полагаясь на соседей."""
    await StandMigrations().application(
        test_postgres, runtime_config.db_schema, "boba.aid"
    )

    return UsersTable(pool, runtime_config.db_schema)


def _kerberos_auth(
    config: KerberosAuthConfig, runtime_config: AppConfig, users: UsersTable
) -> KerberosAuth:
    """SSO chainlit над сервисом входа: секрет и cookie из [session], users — база."""
    session = runtime_config.session
    profiles = ChatProfiles(runtime_config.profiles)
    sign_ins = SignInStand.backends(profiles=profiles).build(
        [config], session.auth_secret
    )
    auth = BrowserSessions(
        sign_ins=sign_ins,
        tokens=JwtTokens(
            session.auth_secret,
            session.lifetime(),
            session.session_generation(),
            issuer=StandTokens.ISSUER,
            audience=StandTokens.ISSUER,
        ),
        cookie=CookieSpec(
            name=session.cookie,
            samesite=session.cookie_samesite,
            ttl_sec=session.session_ttl_sec,
        ),
        sessions=SessionService(users, profiles),
        renewal=SessionRenewal.of(
            session.session_ttl_sec, session.session_ttl_sec * 24
        ),
    )

    return KerberosAuth(config.sso_path, PageUrls.of("/boba", config.sso_path), auth)


@pytest.fixture
def kerberos_auth(
    raw_config: Any,
    runtime_config: AppConfig,
    users: UsersTable,
    auth_token: str,
) -> KerberosAuth:
    """Провайдер SSO по боевой секции [auth.kerberos]; auth_token ставит JWT-секрет."""
    config = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
    return _kerberos_auth(config, runtime_config, users)


@pytest.fixture
def excluding_auth(
    raw_config: Any,
    runtime_config: AppConfig,
    users: UsersTable,
    auth_token: str,
) -> KerberosAuth:
    """Тот же SSO, но принципал стенда попал в список исключённых AD."""
    config = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
    principal = config.roles.principal
    if principal is None:
        principal = PrincipalRolesConfig()

    excluded = principal.model_copy(
        update={"principal_ex": RoleExcludeConfig([USER_PRINCIPAL])}
    )
    providers: KerberosRoleProviders = config.roles.model_copy(
        update={"principal": excluded}
    )
    modified = config.model_copy(update={"roles": providers})

    return _kerberos_auth(modified, runtime_config, users)


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
    async def signed(auth: KerberosAuth, token: bytes) -> IssuedSession:
        import base64

        scope = {
            "type": "http",
            "method": "GET",
            "path": auth._urls.sso,
            "query_string": b"",
            "headers": [(b"authorization", b"Negotiate " + base64.b64encode(token))],
            "client": ("127.0.0.1", 1234),
        }
        outcome = await auth.auth.by_spnego(auth._requests.of(Request(scope)))
        if isinstance(outcome, SsoChallenge):
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
        """Статус ответа и JWT из его Set-Cookie (пустой — cookie не выдана).

        Имя cookie — из [session] площадки, которой собран auth."""
        cookie_name = auth.auth.cookie().name

        headers: list[tuple[bytes, bytes]] = [
            (b"authorization", b"Negotiate " + base64.b64encode(token))
        ]
        if own_header:
            headers.append((OwnRequest.HEADER.encode(), OwnRequest.VALUE.encode()))
        if jwt_cookie is not None:
            cookie = f"{cookie_name}={jwt_cookie}".encode()
            headers.append((b"cookie", cookie))
        scope = {
            "type": "http",
            "method": "GET",
            "path": auth._urls.refresh,
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
        }

        refresh = SessionRefresh(
            auth._urls, auth.auth, ChainlitSessions(StandTokens()), APP_ROOT
        )
        response = await refresh.refresh(Request(scope))
        raw = [(key, value) for key, value in response.raw_headers]

        return response.status_code, Refresh._token_of(raw, cookie_name)

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


async def _signed_in(auth: KerberosAuth, tmp_path: Path) -> IssuedSession:
    """Итог настоящего SSO-входа с полным подписанным токеном сессии."""
    return await Sso.signed(auth, Browser.token(tmp_path))


async def test_sign_in_puts_principal_and_ticket_into_the_jwt(
    kerberos_auth: KerberosAuth,
    runtime_config: AppConfig,
    tmp_path: Path,
    krb5_env: None,
) -> None:
    session = await _signed_in(kerberos_auth, tmp_path)
    claims = StandTokens().read(session.token)
    sso = claims.ticket()
    if sso is None:
        raise AssertionError("JWT of the sign-in must carry the ticket")
    if sso.principal != USER_PRINCIPAL:
        raise AssertionError(sso.principal)
    if not claims.principal().roles:
        raise AssertionError("JWT must carry roles resolved for the sign-in")

    tickets = _tickets(runtime_config)
    credentials = tickets.credentials_of(tickets.open(sso.sealed))
    ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
    if ticket.principal != USER_PRINCIPAL:
        raise AssertionError(f"ticket must belong to the user: {ticket.principal}")


async def test_users_row_keeps_identity_while_ticket_stays_in_jwt(
    kerberos_auth: KerberosAuth,
    users: UsersTable,
    tmp_path: Path,
    krb5_env: None,
) -> None:
    """В БД остаётся identity и роли, а principal и билет только в JWT."""
    session = await _signed_in(kerberos_auth, tmp_path)
    stored = await users.stored(session.user.identity)
    assert stored is not None, "sign-in must persist the users row"
    assert stored.identity == session.user.identity
    claims = StandTokens().read(session.token)
    assert claims.ticket() is not None


async def test_stale_jwt_without_ticket_is_refused(
    kerberos_auth: KerberosAuth,
) -> None:
    """JWT прошлой версии приложения: провайдер есть, билета нет."""
    stale = StandTokens.issue(StandTokens.session("corporate:readonly"))
    if ChainlitSessions(StandTokens()).ticket_of_token(stale) is not None:
        raise AssertionError("a sign-in without a ticket must not resolve")


async def test_refresh_issues_a_new_jwt_with_a_fresh_ticket(
    kerberos_auth: KerberosAuth,
    runtime_config: AppConfig,
    tmp_path: Path,
    krb5_env: None,
) -> None:
    """Билет входа на исходе, а сессия жива: обмен выдаёт новый JWT с новым билетом."""
    session = await _signed_in(kerberos_auth, tmp_path)
    token = session.token
    before = ChainlitSessions(StandTokens()).ticket_of_token(token)
    if before is None:
        raise AssertionError("JWT of the sign-in must carry the ticket")

    status, renewed = await Refresh.exchange(
        kerberos_auth, Browser.token(tmp_path), token
    )
    if status != 204:
        raise AssertionError(f"refresh must succeed: {status}")
    if not renewed:
        raise AssertionError("refresh must set a new session cookie")

    after = ChainlitSessions(StandTokens()).ticket_of_token(renewed)
    if after is None:
        raise AssertionError(f"the new JWT must carry a ticket: {len(renewed)} chars")
    if after.sealed == before.sealed:
        raise AssertionError("refresh must seal a fresh ticket")
    if after.principal != USER_PRINCIPAL:
        raise AssertionError(f"the ticket must stay the user's: {after.principal}")

    tickets = _tickets(runtime_config)
    credentials = tickets.credentials_of(tickets.open(after.sealed))
    ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
    if ticket.principal != USER_PRINCIPAL:
        raise AssertionError(f"ticket must belong to the user: {ticket.principal}")


def _tickets(config: AppConfig) -> SsoTickets:
    """Читатель билета с тем же секретом сессии и конфигурацией Kerberos."""
    return SsoTickets(TicketSealer(config.session.auth_secret), str(KRB5_CONF))


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
    session = await _signed_in(kerberos_auth, tmp_path)
    credential = session.session.ticket()
    assert credential is not None
    mismatched = session.session.model_copy(
        update={
            "credential": DelegatedTicket(
                principal=f"stranger@{STAND.krb_realm}", sealed=credential.sealed
            )
        }
    )
    stranger_token = StandTokens.issue(mismatched)
    status, renewed = await Refresh.exchange(
        kerberos_auth, Browser.token(tmp_path), stranger_token
    )
    if status != 403:
        raise AssertionError(f"a foreign principal must be refused: {status}")
    if renewed:
        raise AssertionError("refusal must not issue a cookie")


async def test_page_script_knows_where_to_refresh(kerberos_auth: KerberosAuth) -> None:
    """Адрес обмена подставляет сервер: скрипт страницы не собирает его сам."""
    refresh = SessionRefresh(
        kerberos_auth._urls,
        kerberos_auth.auth,
        ChainlitSessions(StandTokens()),
        APP_ROOT,
    )
    script = refresh.script()
    if kerberos_auth._urls.refresh not in script:
        raise AssertionError("page.js must carry the refresh url")
    if kerberos_auth._urls.sso not in script:
        raise AssertionError("page.js must carry the sso url")
    if OwnRequest.HEADER not in script:
        raise AssertionError("page.js must mark its own request with the header")
    if "__REFRESH" in script:
        raise AssertionError("the placeholders must be replaced")


async def test_refresh_without_its_own_header_is_refused(
    kerberos_auth: KerberosAuth, tmp_path: Path, krb5_env: None
) -> None:
    """Запрос чужого сайта заголовок не несёт: обмена не будет даже с cookie."""
    session = await _signed_in(kerberos_auth, tmp_path)
    status = await Refresh.status(
        kerberos_auth, Browser.token(tmp_path), session.token, own_header=False
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
    session = await _signed_in(kerberos_auth, tmp_path)
    token = session.token

    # вход и его билет остаются прежними, меняется только политика допуска
    status = await Refresh.status(excluding_auth, Browser.token(tmp_path), token)
    if status != 403:
        raise AssertionError(f"an excluded principal must be refused: {status}")


async def test_multiple_spnego_realms_use_the_selected_identity_rule(
    raw_config: Any,
    runtime_config: AppConfig,
    krb5_env: None,
    tmp_path: Path,
) -> None:
    configured = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
    office = configured.model_copy(update={"identity": IdentityRule(realm="office")})
    partner = configured.model_copy(update={"identity": IdentityRule(realm="partner")})
    providers = SignInStand.backends().build(
        [office, partner],
        runtime_config.session.auth_secret,
    )
    with pytest.raises(AuthorizationError, match="select an identity realm"):
        providers.exchange()

    for realm in ("office", "partner"):
        token = Browser.token(tmp_path)
        request = SsoRequest(
            authorization="Negotiate " + base64.b64encode(token).decode()
        )
        outcome = await providers.exchange(realm).handshake(request)
        assert isinstance(outcome, SsoSigned)
        login, _, _ = USER_PRINCIPAL.partition("@")
        assert outcome.signed.identifier == f"{realm}:{login.lower()}"
