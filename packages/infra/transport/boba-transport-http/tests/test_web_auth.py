"""Варианты авторизации web-профиля на живых сервисах: кем нас видит сервер.

Anonymous и basic проверяются на HTTP-интерфейсе clickhouse (он отвечает, кем
считает клиента), bearer и negotiate — на confluence. Каждый вариант идёт своим
профилем и своим настоящим запросом, поэтому тест ловит и неверный заголовок,
и неверные креды.

Адреса и учётки приходят из конфига стенда.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from boba.connections.http import (
    BasicAuth,
    BearerAuth,
    HttpProfile,
    NegotiateAuth,
    NoneAuth,
)
from boba.kerberos import KeytabAuth
from boba.krb import KerberosWorkspace, KeytabCredentials, ServiceTicketIssuer
from boba.stand.site import Stand
from boba.transport.http import HttpRequest, HttpTransport

STAND = Stand.required()

CONFLUENCE_ME = "/rest/api/user/current"
"""Confluence отвечает, под кем принят запрос: годится для любого способа."""

LOGIN_SERVLET = "/plugins/servlet/kerberos/ntlm/login"
"""Servlet, который меняет билет на сессионную cookie confluence."""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
]


needs_clickhouse = pytest.mark.skipif(
    not STAND.ch_addr, reason="в конфиге стенда нет clickhouse (ch_addr)"
)

@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> None:
    """Кэши билетов теста живут в своём каталоге, как у приложения."""
    KerberosWorkspace.configure(STAND.krb_config, str(tmp_path / "cache"))


def _clickhouse(auth: Any) -> HttpProfile:
    return HttpProfile(
        base_url=f"http://{STAND.ch_addr}:{STAND.ch_port}",
        auth=auth,
        timeout_sec=15.0,
    )


def _confluence(auth: Any) -> HttpProfile:
    return HttpProfile(base_url=STAND.confluence_url, auth=auth, ssl_verify=False)


async def _body(profile: HttpProfile, request: HttpRequest) -> tuple[int, str]:
    async with HttpTransport(profile) as transport, transport.fetch(request) as resp:
        payload = await resp.stream.read()

    return resp.status, payload.decode("utf-8", errors="replace")


def _keytab() -> KeytabAuth:
    return KeytabAuth(
        method="kerberos_keytab",
        principal=STAND.service_principal,
        keytab=STAND.krb_http_keytab,
    )


@needs_clickhouse
async def test_none_auth_reaches_an_open_endpoint() -> None:
    """method = none: заголовка авторизации нет, открытый адрес отвечает."""
    profile = _clickhouse(NoneAuth(method="none"))

    status, body = await _body(profile, HttpRequest(url="/ping"))

    if status != 200:
        raise AssertionError(f"anonymous request must pass: {status} {body}")


@needs_clickhouse
async def test_none_auth_is_refused_where_credentials_are_required() -> None:
    """Тот же профиль без кредов: закрытый адрес отвечает отказом, а не данными."""
    profile = _clickhouse(NoneAuth(method="none"))

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _body(
            profile, HttpRequest(url="/", params={"query": "select currentUser()"})
        )

    if caught.value.response.status_code != 401:
        raise AssertionError(f"anonymous request must be refused: {caught.value}")


@needs_clickhouse
async def test_basic_auth_logs_in_as_its_own_user() -> None:
    """method = basic: имя и пароль уходят заголовком Authorization."""
    if not STAND.ch_user:
        raise AssertionError("в конфиге стенда нет пользователя clickhouse с паролем")

    profile = _clickhouse(
        BasicAuth(method="basic", user=STAND.ch_user, password=STAND.ch_password)
    )

    status, body = await _body(
        profile, HttpRequest(url="/", params={"query": "select currentUser()"})
    )

    if status != 200:
        raise AssertionError(f"basic auth must pass: {status} {body}")
    if body.strip() != STAND.ch_user:
        raise AssertionError(f"server must see the basic user: {body!r}")


@needs_clickhouse
async def test_basic_auth_with_a_wrong_password_is_refused() -> None:
    """Неверный пароль — отказ сервера, а не анонимный доступ."""
    profile = _clickhouse(
        BasicAuth(
            method="basic",
            user=STAND.ch_user,
            password=SecretStr("not-the-password"),
        )
    )

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _body(
            profile, HttpRequest(url="/", params={"query": "select currentUser()"})
        )

    if caught.value.response.status_code != 401:
        raise AssertionError(f"a wrong password must be refused: {caught.value}")


async def test_bearer_auth_names_the_token_owner() -> None:
    """method = bearer: токен уходит заголовком, confluence называет владельца."""
    if not STAND.confluence_token.get_secret_value():
        pytest.skip("в конфиге стенда нет токена confluence")

    profile = _confluence(BearerAuth(method="bearer", token=STAND.confluence_token))

    status, body = await _body(profile, HttpRequest(url=CONFLUENCE_ME))

    if status != 200:
        raise AssertionError(f"bearer auth must pass: {status} {body}")

    user = json.loads(body)
    if not user.get("username"):
        raise AssertionError(f"confluence must name the token owner: {body}")


async def test_bearer_auth_with_a_wrong_token_stays_anonymous() -> None:
    """Испорченный токен не даёт чужого имени: confluence считает нас гостем.

    Сам сервер анонима пускает (200), поэтому проверяется не код, а то, кем
    он нас видит: доступ под владельцем токена не достаётся.
    """
    profile = _confluence(BearerAuth(method="bearer", token=SecretStr("not-a-token")))

    status, body = await _body(profile, HttpRequest(url=CONFLUENCE_ME))
    if status != 200:
        raise AssertionError(f"confluence answers anonymous requests: {status}")

    user = json.loads(body)
    if user.get("username"):
        raise AssertionError(f"a wrong token must name nobody: {body}")


async def test_negotiate_keytab_logs_in_as_the_service_principal() -> None:
    """method = negotiate + keytab: SPNEGO к HTTP/host, сессия — принципала."""
    profile = _confluence(
        NegotiateAuth(method="negotiate", kerberos=_keytab(), login_path=LOGIN_SERVLET)
    )

    status, body = await _body(profile, HttpRequest(url=CONFLUENCE_ME))

    if status != 200:
        raise AssertionError(f"negotiate must pass: {status} {body}")

    user = json.loads(body)
    if user.get("username") != STAND.krb_http_user:
        raise AssertionError(f"confluence must see the principal: {body}")


async def test_negotiate_ticket_logs_in_as_the_ticket_owner() -> None:
    """method = negotiate + kerberos_ticket: в песочницу уезжает один билет."""
    source = KeytabCredentials.of(_keytab())
    ticket = await ServiceTicketIssuer(min_lifetime=60).issue_async(
        source, STAND.confluence_spn
    )
    profile = _confluence(
        NegotiateAuth(method="negotiate", kerberos=ticket, login_path=LOGIN_SERVLET)
    )

    status, body = await _body(profile, HttpRequest(url=CONFLUENCE_ME))

    if status != 200:
        raise AssertionError(f"ticket negotiate must pass: {status} {body}")

    user = json.loads(body)
    if user.get("username") != STAND.krb_http_user:
        raise AssertionError(f"confluence must see the ticket owner: {body}")


async def test_service_name_follows_the_requested_host() -> None:
    """SPN собирается из хоста профиля: билет выпускается ровно к нему."""
    profile = _confluence(NegotiateAuth(method="negotiate", kerberos=_keytab()))

    if profile.service_name() != STAND.confluence_spn:
        raise AssertionError(f"unexpected SPN: {profile.service_name()}")
