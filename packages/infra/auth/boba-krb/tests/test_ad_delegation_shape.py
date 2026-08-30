"""Каким AD видит учётку приложения: ограниченное делегирование и только оно.

Режим приложения — constrained: билеты к бэкендам берутся по S4U2Proxy на
основании evidence-тикета входа. Для этого учётке нужен ровно один атрибут —
список разрешённых SPN. Два других признака делегирования обязаны быть сняты:

неограниченное делегирование (UF_TRUSTED_FOR_DELEGATION) даёт сервису право
действовать от имени пользователя где угодно;

смена протокола (UF_TRUSTED_TO_AUTHENTICATE_FOR_DELEGATION) позволяет сервису
получать билеты за пользователя без его участия, а заодно помечает выданные
билеты ok-as-delegate — из-за чего клиент форвардит сервису свой TGT, и
evidence-тикета в кредах входа не оказывается вовсе.

Ошибки: своих не выпускает, вся диагностика — в тексте assert'ов.
"""

from __future__ import annotations

from typing import Any

import pytest
from ldap3 import ALL, SUBTREE, Connection, Server

from boba.stand.site import Stand

STAND = Stand.required()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
]

TRUSTED_FOR_DELEGATION = 0x80000
"""Неограниченное делегирование: запрещено политикой."""

TRUSTED_TO_AUTH_FOR_DELEGATION = 0x1000000
"""Смена протокола (S4U2Self): режиму constrained не нужна."""

ALLOWED_TO_DELEGATE = "msDS-AllowedToDelegateTo"
USER_ACCOUNT_CONTROL = "userAccountControl"


def _account(name: str) -> dict[str, Any]:
    """Атрибуты делегирования учётки из AD стенда."""
    server = Server(STAND.ldap_url, get_info=ALL)
    connection = Connection(
        server,
        user=STAND.ldap_bind_user,
        password=STAND.ldap_bind_password.get_secret_value(),
        auto_bind=True,
    )
    try:
        connection.search(
            STAND.ldap_base_dn,
            f"(sAMAccountName={name})",
            SUBTREE,
            attributes=[USER_ACCOUNT_CONTROL, ALLOWED_TO_DELEGATE],
        )
        entries = connection.entries
        if not entries:
            raise AssertionError(f"в AD нет учётки {name!r}")

        entry = entries[0]
        return {
            USER_ACCOUNT_CONTROL: int(entry[USER_ACCOUNT_CONTROL].value),
            ALLOWED_TO_DELEGATE: list(entry[ALLOWED_TO_DELEGATE].values),
        }
    finally:
        connection.unbind()


@pytest.fixture(scope="module")
def service_account() -> dict[str, Any]:
    return _account(STAND.krb_http_user)


def test_unconstrained_delegation_is_off(service_account: dict[str, Any]) -> None:
    """Право ходить от имени пользователя куда угодно должно быть снято."""
    uac = service_account[USER_ACCOUNT_CONTROL]

    if uac & TRUSTED_FOR_DELEGATION:
        raise AssertionError(
            f"у {STAND.krb_http_user} включено неограниченное делегирование: {uac:#x}"
        )


def test_protocol_transition_is_off(service_account: dict[str, Any]) -> None:
    """Смена протокола выключена: иначе клиент форвардит TGT вместо evidence."""
    uac = service_account[USER_ACCOUNT_CONTROL]

    if uac & TRUSTED_TO_AUTH_FOR_DELEGATION:
        raise AssertionError(
            f"у {STAND.krb_http_user} включена смена протокола (any protocol): "
            f"{uac:#x}; для constrained нужен режим kerberos only"
        )


def test_backends_are_listed_for_delegation(service_account: dict[str, Any]) -> None:
    """Список разрешённых SPN покрывает бэкенды, к которым ходят инструменты."""
    allowed = {value.lower() for value in service_account[ALLOWED_TO_DELEGATE]}

    expected = {
        f"{STAND.pg_krbsrvname}/{STAND.pg_host}".lower(),
        f"HTTP/{STAND.confluence_host}".lower(),
    }
    if STAND.ch_krbsrvname:
        expected.add(f"{STAND.ch_krbsrvname}/{STAND.ch_host}".lower())

    missing = expected - allowed
    if missing:
        raise AssertionError(
            f"в msDS-AllowedToDelegateTo нет: {sorted(missing)}; есть {sorted(allowed)}"
        )
