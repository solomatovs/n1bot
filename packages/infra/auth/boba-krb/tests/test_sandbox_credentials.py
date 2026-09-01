"""Что уезжает в песочницу вместе с конфигом соединения.

Тело инструмента работает билетом одного вызова: keytab — долговременный
ключ принципала, и его место у приложения. Здесь проверяется сама граница —
дамп конфига, который приложение отправляет внутрь.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from boba.db.clickhouse.profile import ClickHouseConfig
from boba.db.postgres.profile import PostgresConfig
from boba.kerberos import DelegatedAuth, KerberosError, KeytabAuth, TicketAuth
from boba.krb import (
    ClientCredentials,
    KerberosWorkspace,
    KeytabCredentials,
    TicketCredentials,
)


class Fixtures:
    """Значения соединения: конкретика теста в одном месте."""

    KEYTAB: ClassVar[str] = "/etc/boba/svc.keytab"
    PRINCIPAL: ClassVar[str] = "svc@EXAMPLE.COM"
    KRB5: ClassVar[str] = "/etc/boba/krb5.conf"
    SERVICE: ClassVar[str] = "postgres@pg.example.com"

    @classmethod
    def keytab(cls) -> KeytabAuth:
        return KeytabAuth(
            method="kerberos_keytab",
            principal=cls.PRINCIPAL,
            keytab=cls.KEYTAB,
        )

    @classmethod
    def ticket(cls) -> TicketAuth:
        return TicketAuth.of_bytes(cls.PRINCIPAL, cls.SERVICE, b"ccache", 60)

    @classmethod
    def postgres(cls, auth: object) -> PostgresConfig:
        return PostgresConfig.model_validate(
            {
                "host": "pg.example.com",
                "dbname": "boba",
                "connect_timeout": 5,
                "auth": auth,
            }
        )

    @classmethod
    def clickhouse(cls, kerberos: object) -> ClickHouseConfig:
        return ClickHouseConfig.model_validate(
            {
                "host": "ch.example.com",
                "port": 8123,
                "interface": "http",
                "database": "default",
                "connect_timeout": 5,
                "auth": kerberos,
            }
        )


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> None:
    """Каталог кэшей на тест: KeytabCredentials выбирает файл через workspace."""
    KerberosWorkspace.configure(str(tmp_path / "krb5.conf"), str(tmp_path / "cache"))


class TestKeytabStaysWithTheApp:
    """Дефект: keytab и общий ccache уезжали в песочницу вместе с конфигом.

    Тело получало вечный ключ принципала либо его полный TGT; теперь дамп
    с раскрытыми секретами обязан нести билет одного вызова и ничего другого.
    """

    def test_postgres_reveal_refuses_a_keytab(self) -> None:
        config = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))

        with pytest.raises(ValueError, match="may not leave the application"):
            config.model_dump(mode="json", context={TicketAuth.REVEAL_SECRETS: True})

    def test_clickhouse_reveal_refuses_a_keytab(self) -> None:
        config = Fixtures.clickhouse(Fixtures.keytab().model_dump(mode="json"))

        with pytest.raises(ValueError, match="may not leave the application"):
            config.model_dump(mode="json", context={TicketAuth.REVEAL_SECRETS: True})

    def test_host_dump_keeps_the_keytab(self) -> None:
        """Дамп без раскрытия секретов — конфиг хоста, keytab ему нужен."""
        config = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))
        dumped = config.model_dump(mode="json")

        if "keytab" not in dumped["auth"]:
            raise AssertionError(f"хост остался без keytab: {dumped['kerberos']}")

    def test_ticket_travels_and_reads_back(self) -> None:
        config = Fixtures.postgres(Fixtures.ticket())
        dumped = config.model_dump(
            mode="json", context={TicketAuth.REVEAL_SECRETS: True}
        )
        restored = PostgresConfig.model_validate(dumped)

        if not isinstance(restored.auth, TicketAuth):
            raise AssertionError(f"телу достались {type(restored.auth).__name__}")
        if restored.auth.ccache_bytes() != b"ccache":
            raise AssertionError("байты билета не доехали")

        credentials = ClientCredentials.of(restored.auth)
        if not isinstance(credentials, TicketCredentials):
            raise AssertionError(f"выбрана реализация {type(credentials).__name__}")

    def test_ticket_is_masked_without_reveal(self) -> None:
        dumped = Fixtures.postgres(Fixtures.ticket()).model_dump(mode="json")

        if dumped["auth"]["ccache"] != "**********":
            raise AssertionError(f"байты билета в обычном дампе: {dumped['kerberos']}")

    def test_host_config_keeps_keytab_credentials(self) -> None:
        credentials = ClientCredentials.of(Fixtures.keytab())
        if not isinstance(credentials, KeytabCredentials):
            raise AssertionError(f"выбрана реализация {type(credentials).__name__}")

    def test_body_refuses_a_delegated_section(self) -> None:
        """Секция «идёт сам пользователь» — для приложения, телу она не креды."""
        with pytest.raises(KerberosError, match="resolved by the application"):
            ClientCredentials.of(DelegatedAuth(method="kerberos_delegated"))


class TestGssModesAreDerived:
    """gssencmode и require_auth выводит вариант авторизации, а не конфиг."""

    def test_password_turns_gss_off(self) -> None:
        config = PostgresConfig.model_validate(
            {
                "host": "h",
                "dbname": "d",
                "auth": {"method": "password", "user": "u", "password": "p"},
            }
        )
        settings = config.conn_settings()
        if settings["gssencmode"] != "disable":
            raise AssertionError(settings)
        if settings["require_auth"] != "scram-sha-256":
            raise AssertionError(settings)

    def test_kerberos_requires_gss(self) -> None:
        config = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))
        settings = config.conn_settings()
        if settings["gssencmode"] != "require":
            raise AssertionError(settings)
        if settings["require_auth"] != "gss":
            raise AssertionError(settings)
        if settings["user"] != "svc":
            raise AssertionError(settings)

    def test_unknown_method_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="method"):
            PostgresConfig.model_validate(
                {"host": "h", "dbname": "d", "auth": {"method": "magic"}}
            )

    def test_kerberos_requires_connect_timeout(self) -> None:
        raw = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))
        unbounded = raw.model_dump(mode="json")
        unbounded["connect_timeout"] = None

        with pytest.raises(ValueError, match="connect_timeout"):
            PostgresConfig.model_validate(unbounded)

    def test_clickhouse_kerberos_requires_connect_timeout(self) -> None:
        raw = Fixtures.clickhouse(Fixtures.keytab().model_dump(mode="json"))
        unbounded = raw.model_dump(mode="json")
        unbounded["connect_timeout"] = None

        with pytest.raises(ValueError, match="connect_timeout"):
            ClickHouseConfig.model_validate(unbounded)
