"""Что уезжает в песочницу вместе с конфигом соединения.

Тело инструмента работает билетом одного вызова: keytab — долговременный
ключ принципала, и его место у приложения. Здесь проверяется сама граница —
дамп конфига, который приложение отправляет внутрь.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.db.clickhouse.config import ClickHouseConfig
from boba.db.postgres.config import PostgresConfig
from boba.krb import (
    ClientCredentials,
    DelegatedConfig,
    KerberosError,
    KeytabConfig,
    KeytabCredentials,
    TicketConfig,
    TicketCredentials,
)


class Fixtures:
    """Значения соединения: конкретика теста в одном месте."""

    KEYTAB: ClassVar[str] = "/etc/boba/boba-svc.keytab"
    CCACHE: ClassVar[str] = "FILE:/tmp/krb5cc_pg"
    PRINCIPAL: ClassVar[str] = "boba-svc@LOSHARA.COM"
    KRB5: ClassVar[str] = "/etc/boba/krb5.conf"
    SERVICE: ClassVar[str] = "postgres@pg.loshara.com"

    @classmethod
    def keytab(cls) -> KeytabConfig:
        return KeytabConfig(
            keytab=cls.KEYTAB,
            principal=cls.PRINCIPAL,
            ccache=cls.CCACHE,
            krb5_config=cls.KRB5,
        )

    @classmethod
    def ticket(cls) -> TicketConfig:
        return TicketConfig.of_bytes(cls.PRINCIPAL, cls.SERVICE, b"ccache", 60)

    @classmethod
    def postgres(cls, kerberos: object) -> PostgresConfig:
        return PostgresConfig.model_validate(
            {
                "host": "pg.loshara.com",
                "user": "boba-svc",
                "dbname": "boba",
                "gssencmode": "require",
                "connect_timeout": 5,
                "kerberos": kerberos,
            }
        )

    @classmethod
    def clickhouse(cls, kerberos: object) -> ClickHouseConfig:
        return ClickHouseConfig.model_validate(
            {
                "host": "ch.loshara.com",
                "port": 8123,
                "interface": "http",
                "database": "default",
                "krbsrvname": "HTTP",
                "connect_timeout": 5,
                "kerberos": kerberos,
            }
        )


class TestKeytabStaysWithTheApp:
    """Дефект: keytab и общий ccache уезжали в песочницу вместе с конфигом.

    Тело получало вечный ключ принципала либо его полный TGT; теперь дамп
    с раскрытыми секретами обязан нести билет одного вызова и ничего другого.
    """

    def test_postgres_reveal_refuses_a_keytab(self) -> None:
        config = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))

        with pytest.raises(ValueError, match="may not leave the application"):
            config.model_dump(
                mode="json", context={PostgresConfig.REVEAL_SECRETS: True}
            )

    def test_clickhouse_reveal_refuses_a_keytab(self) -> None:
        config = Fixtures.clickhouse(Fixtures.keytab().model_dump(mode="json"))

        with pytest.raises(ValueError, match="may not leave the application"):
            config.model_dump(
                mode="json", context={ClickHouseConfig.REVEAL_SECRETS: True}
            )

    def test_host_dump_keeps_the_keytab(self) -> None:
        """Дамп без раскрытия секретов — конфиг хоста, keytab ему нужен."""
        config = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))
        dumped = config.model_dump(mode="json")

        if "keytab" not in dumped["kerberos"]:
            raise AssertionError(f"хост остался без keytab: {dumped['kerberos']}")

    def test_ticket_travels_and_reads_back(self) -> None:
        config = Fixtures.postgres(Fixtures.ticket())
        dumped = config.model_dump(
            mode="json", context={PostgresConfig.REVEAL_SECRETS: True}
        )
        restored = PostgresConfig.model_validate(dumped)

        if not isinstance(restored.kerberos, TicketConfig):
            raise AssertionError(f"телу достались {type(restored.kerberos).__name__}")
        if restored.kerberos.ccache_bytes() != b"ccache":
            raise AssertionError("байты билета не доехали")

        credentials = ClientCredentials.of(restored.kerberos)
        if not isinstance(credentials, TicketCredentials):
            raise AssertionError(f"выбрана реализация {type(credentials).__name__}")

    def test_ticket_is_masked_without_reveal(self) -> None:
        dumped = Fixtures.postgres(Fixtures.ticket()).model_dump(mode="json")

        if dumped["kerberos"]["ccache"] != "**********":
            raise AssertionError(f"байты билета в обычном дампе: {dumped['kerberos']}")

    def test_host_config_keeps_keytab_credentials(self) -> None:
        credentials = ClientCredentials.of(Fixtures.keytab())
        if not isinstance(credentials, KeytabCredentials):
            raise AssertionError(f"выбрана реализация {type(credentials).__name__}")

    def test_body_refuses_a_delegated_section(self) -> None:
        """Секция «идёт сам пользователь» — для приложения, телу она не креды."""
        with pytest.raises(KerberosError, match="resolved by the application"):
            ClientCredentials.of(DelegatedConfig())


class TestGssModesAreExplicit:
    """libpq без своих кредов не ходит за чужим ccache, со своими — не деградирует."""

    def test_no_kerberos_requires_gss_off(self) -> None:
        with pytest.raises(ValueError, match='gssencmode = "disable"'):
            PostgresConfig.model_validate(
                {"host": "h", "user": "u", "dbname": "d", "gssencmode": "prefer"}
            )

    def test_no_kerberos_without_mode_is_rejected(self) -> None:
        """Умолчание libpq — prefer: без явного disable конфиг не принимается."""
        with pytest.raises(ValueError, match='gssencmode = "disable"'):
            PostgresConfig.model_validate({"host": "h", "user": "u", "dbname": "d"})

    def test_kerberos_requires_gss_required(self) -> None:
        raw = Fixtures.postgres(Fixtures.keytab().model_dump(mode="json"))
        weakened = raw.model_dump(mode="json")
        weakened["gssencmode"] = "prefer"

        with pytest.raises(ValueError, match='gssencmode = "require"'):
            PostgresConfig.model_validate(weakened)

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
