"""Варианты авторизации postgres на живой базе: кем сервер видит соединение.

Каждый вариант auth собирается моделью и проверяется вопросом к самому серверу
(`current_user`), поэтому тест ловит и неверные libpq-аргументы, и неверные
креды. Учётки и адреса приходят из конфига стенда.

Пароль роли-пробника лежит в секции стенда: без него парольные варианты
пропускаются, kerberos-варианты — при отсутствии keytab.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from boba.db.postgres.payload import PayloadPostgres
from boba.db.postgres.profile import PasswordAuth, PostgresConfig
from boba.kerberos import KerberosPasswordAuth, KeytabAuth
from boba.krb import KerberosWorkspace, KeytabCredentials, ServiceTicketIssuer
from boba.stand.site import Stand

STAND = Stand.required()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
]


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> None:
    """Кэши билетов теста живут в своём каталоге, как у приложения."""
    KerberosWorkspace.configure(STAND.krb_config, str(tmp_path / "cache"))


def _profile(auth: Any) -> PostgresConfig:
    return PostgresConfig.model_validate(
        {
            "host": STAND.pg_host,
            "hostaddr": STAND.pg_addr,
            "port": STAND.pg_port,
            "dbname": STAND.pg_database,
            "sslmode": "disable",
            "connect_timeout": 10,
            "application_name": "boba-auth-test",
            "auth": auth,
        }
    )


async def _current_user(profile: PostgresConfig) -> str:
    conn = await PayloadPostgres.connect_config(profile)
    try:
        async with conn.cursor() as cur:
            await cur.execute("select current_user")
            row = await cur.fetchone()
    finally:
        await conn.close()

    if row is None:
        raise AssertionError("postgres returned no row")

    return str(row[0])


def _keytab() -> KeytabAuth:
    return KeytabAuth(
        method="kerberos_keytab",
        principal=STAND.service_principal,
        keytab=STAND.krb_pg_keytab,
        service=STAND.pg_krbsrvname,
    )


async def test_password_auth_logs_in_as_its_own_role() -> None:
    """method = password: user и пароль уезжают в libpq, сервер видит эту роль."""
    if not STAND.pg_probe_user:
        pytest.skip("в конфиге стенда нет роли-пробника с паролем")

    profile = _profile(
        PasswordAuth(
            method="password",
            user=STAND.pg_probe_user,
            password=STAND.pg_probe_password,
        )
    )

    if await _current_user(profile) != STAND.pg_probe_user:
        raise AssertionError("password auth must log in as its own role")


async def test_keytab_auth_logs_in_as_the_service_principal() -> None:
    """method = kerberos_keytab: роль выводится из принципала, пароля нет."""
    profile = _profile(_keytab())

    if await _current_user(profile) != STAND.krb_pg_user:
        raise AssertionError("keytab auth must log in as the service principal")


async def test_keytab_auth_sends_no_password() -> None:
    """Пароль kerberos-соединению не нужен: libpq требует только gss."""
    settings = _profile(_keytab()).conn_settings()

    if "password" in settings:
        raise AssertionError(f"kerberos must not send a password: {settings}")
    if settings["require_auth"] != "gss":
        raise AssertionError(f"kerberos must require gss: {settings}")


async def test_kerberos_password_auth_logs_in_as_that_user() -> None:
    """method = kerberos_password: TGT берётся по паролю, дальше всё как с keytab."""
    profile = _profile(
        KerberosPasswordAuth(
            method="kerberos_password",
            principal=STAND.reader_principal,
            password=STAND.reader_password,
            service=STAND.pg_krbsrvname,
        )
    )

    expected = STAND.reader_principal.split("@")[0]
    if await _current_user(profile) != expected:
        raise AssertionError("kerberos password auth must log in as that user")


async def test_ticket_auth_logs_in_as_the_ticket_owner() -> None:
    """method = kerberos_ticket: тело получает один билет и ходит только им."""
    source = KeytabCredentials.of(_keytab())
    ticket = await ServiceTicketIssuer(min_lifetime=60).issue_async(
        source, STAND.pg_spn
    )

    if await _current_user(_profile(ticket)) != STAND.krb_pg_user:
        raise AssertionError("ticket auth must log in as the ticket owner")


async def test_wrong_password_is_reported() -> None:
    """Неверный пароль — ошибка соединения, а не молчаливый вход другим способом."""
    if not STAND.pg_probe_user:
        pytest.skip("в конфиге стенда нет роли-пробника с паролем")

    profile = _profile(
        PasswordAuth(
            method="password",
            user=STAND.pg_probe_user,
            password=SecretStr("not-the-password"),
        )
    )

    with pytest.raises(Exception, match="password"):
        await _current_user(profile)
