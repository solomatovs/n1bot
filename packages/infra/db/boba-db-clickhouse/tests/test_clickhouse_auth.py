"""Варианты авторизации clickhouse на живом сервере: кем он видит клиента.

Каждый вариант auth собирается моделью и проверяется вопросом к самому серверу
(`currentUser()`). Kerberos у HTTP-интерфейса идёт заголовком Negotiate, поэтому
проверяется ещё и то, что имя пользователя клиенту не передаётся.

Учётки и адреса приходят из конфига стенда.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from boba.connections.clickhouse import ClickHouseConfig, PasswordAuth
from boba.connections.kerberos import KerberosPasswordAuth, KeytabAuth
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.krb import KerberosWorkspace, KeytabCredentials, ServiceTicketIssuer
from boba.stand.site import Stand

STAND = Stand.required()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
    pytest.mark.skipif(
        not STAND.ch_addr, reason="в конфиге стенда нет clickhouse (ch_addr)"
    ),
]

needs_ch_keytab = pytest.mark.skipif(
    not STAND.krb_ch_keytab, reason="в конфиге стенда нет keytab clickhouse"
)


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> None:
    """Кэши билетов теста живут в своём каталоге, как у приложения."""
    KerberosWorkspace.configure(STAND.krb_config, str(tmp_path / "cache"))


def _profile(auth: Any) -> ClickHouseConfig:
    return ClickHouseConfig.model_validate(
        {
            "host": STAND.ch_addr,
            "port": STAND.ch_port,
            "interface": "http",
            "database": STAND.ch_database,
            "server_host_name": STAND.ch_host,
            "connect_timeout": 10,
            "client_name": "boba-auth-test",
            "auth": auth,
        }
    )


async def _current_user(profile: ClickHouseConfig) -> str:
    async with PayloadClickHouse.opened_config(profile) as client:
        result = await client.query("select currentUser()")

    rows = list(result.result_rows)
    if not rows:
        raise AssertionError("clickhouse returned no row")

    return str(rows[0][0])


def _keytab() -> KeytabAuth:
    return KeytabAuth(
        method="kerberos_keytab",
        principal=STAND.service_principal,
        keytab=STAND.krb_ch_keytab,
        service=STAND.ch_krbsrvname,
    )


async def test_password_auth_logs_in_as_its_own_user() -> None:
    """method = password: имя и пароль уезжают клиенту, сервер видит их."""
    if not STAND.ch_user:
        pytest.skip("в конфиге стенда нет пользователя clickhouse с паролем")

    profile = _profile(
        PasswordAuth(method="password", user=STAND.ch_user, password=STAND.ch_password)
    )

    if await _current_user(profile) != STAND.ch_user:
        raise AssertionError("password auth must log in as its own user")


@needs_ch_keytab
async def test_keytab_auth_logs_in_as_the_service_principal() -> None:
    """method = kerberos_keytab: пользователя сервер берёт из заголовка Negotiate."""
    if await _current_user(_profile(_keytab())) != STAND.krb_ch_user:
        raise AssertionError("keytab auth must log in as the service principal")


async def test_kerberos_sends_no_basic_credentials() -> None:
    """Имя и пароль kerberos-соединению не нужны: их заменяет SPNEGO."""
    settings = _profile(_keytab()).client_settings()

    if "username" in settings or "password" in settings:
        raise AssertionError(f"kerberos must send no basic credentials: {settings}")


async def test_kerberos_password_auth_logs_in_as_that_user() -> None:
    """method = kerberos_password: TGT по паролю, дальше тот же Negotiate."""
    profile = _profile(
        KerberosPasswordAuth(
            method="kerberos_password",
            principal=STAND.reader_principal,
            password=STAND.reader_password,
            service=STAND.ch_krbsrvname,
        )
    )

    expected = STAND.reader_principal.split("@")[0]
    if await _current_user(profile) != expected:
        raise AssertionError("kerberos password auth must log in as that user")


@needs_ch_keytab
async def test_ticket_auth_logs_in_as_the_ticket_owner() -> None:
    """method = kerberos_ticket: в песочницу уезжает билет, им и ходим."""
    source = KeytabCredentials.of(_keytab())
    ticket = await ServiceTicketIssuer(min_lifetime=60).issue_async(
        source, STAND.ch_spn
    )

    if await _current_user(_profile(ticket)) != STAND.krb_ch_user:
        raise AssertionError("ticket auth must log in as the ticket owner")


async def test_wrong_password_is_reported() -> None:
    """Неверный пароль — ошибка, а не тихий вход пользователем по умолчанию."""
    if not STAND.ch_user:
        pytest.skip("в конфиге стенда нет пользователя clickhouse с паролем")

    profile = _profile(
        PasswordAuth(
            method="password",
            user=STAND.ch_user,
            password=SecretStr("not-the-password"),
        )
    )

    with pytest.raises(Exception, match=r"[Aa]uthentication"):
        await _current_user(profile)
