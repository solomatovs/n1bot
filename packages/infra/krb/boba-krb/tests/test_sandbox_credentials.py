"""Что уезжает в песочницу вместе с конфигом соединения.

Тело инструмента работает готовым тикетом: keytab — долговременный ключ
принципала, и его место у приложения. Здесь проверяется сама граница —
дамп конфига, который приложение отправляет внутрь.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.db.clickhouse.config import ClickHouseConfig
from boba.db.postgres.config import PostgresConfig
from boba.krb import (
    CcacheConfig,
    CcacheCredentials,
    ClientCredentials,
    CredentialsExpiredError,
    KerberosEnv,
    KeytabConfig,
    KeytabCredentials,
)


class Fixtures:
    """Значения соединения: конкретика теста в одном месте."""

    KEYTAB: ClassVar[str] = "/etc/boba/boba-svc.keytab"
    CCACHE: ClassVar[str] = "FILE:/tmp/krb5cc_pg"  # noqa: S108
    PRINCIPAL: ClassVar[str] = "boba-svc@LOSHARA.COM"
    KRB5: ClassVar[str] = "/etc/boba/krb5.conf"

    @classmethod
    def keytab(cls) -> KeytabConfig:
        return KeytabConfig(
            keytab=cls.KEYTAB,
            principal=cls.PRINCIPAL,
            ccache=cls.CCACHE,
            krb5_config=cls.KRB5,
        )

    @classmethod
    def postgres(cls) -> PostgresConfig:
        return PostgresConfig.model_validate(
            {
                "host": "pg.loshara.com",
                "user": "boba-svc",
                "dbname": "boba",
                "gssencmode": "require",
                "kerberos": cls.keytab().model_dump(mode="json"),
            }
        )

    @classmethod
    def clickhouse(cls) -> ClickHouseConfig:
        return ClickHouseConfig.model_validate(
            {
                "host": "ch.loshara.com",
                "port": 8123,
                "interface": "http",
                "database": "default",
                "krbsrvname": "HTTP",
                "kerberos": cls.keytab().model_dump(mode="json"),
            }
        )


class TestKeytabStaysWithTheApp:
    """Дефект: keytab уезжал в песочницу вместе с конфигом соединения.

    Тело получало вечный ключ принципала: украденный keytab работает, пока
    не сменят пароль учётной записи, а украденный тикет — до конца срока.
    """

    def test_postgres_dump_carries_a_ticket(self) -> None:
        dumped = Fixtures.postgres().model_dump(
            mode="json", context={PostgresConfig.REVEAL_SECRETS: True}
        )

        kerberos = dumped["kerberos"]
        if "keytab" in kerberos:
            raise AssertionError(f"keytab уехал в песочницу: {kerberos}")

        if kerberos["ccache"] != Fixtures.CCACHE:
            raise AssertionError(f"тикет потерян: {kerberos}")

    def test_clickhouse_dump_carries_a_ticket(self) -> None:
        dumped = Fixtures.clickhouse().model_dump(
            mode="json", context={ClickHouseConfig.REVEAL_SECRETS: True}
        )

        kerberos = dumped["kerberos"]
        if "keytab" in kerberos:
            raise AssertionError(f"keytab уехал в песочницу: {kerberos}")

    def test_host_dump_keeps_the_keytab(self) -> None:
        """Дамп без раскрытия секретов — конфиг хоста, keytab ему нужен."""
        dumped = Fixtures.postgres().model_dump(mode="json")

        if "keytab" not in dumped["kerberos"]:
            raise AssertionError(f"хост остался без keytab: {dumped['kerberos']}")

    def test_body_reads_the_dump_back(self) -> None:
        """Тело собирает конфиг обратно и получает креды без keytab."""
        dumped = Fixtures.postgres().model_dump(
            mode="json", context={PostgresConfig.REVEAL_SECRETS: True}
        )
        restored = PostgresConfig.model_validate(dumped)

        if not isinstance(restored.kerberos, CcacheConfig):
            raise AssertionError(f"телу достались {type(restored.kerberos).__name__}")

        credentials = ClientCredentials.of(restored.kerberos)
        if not isinstance(credentials, CcacheCredentials):
            raise AssertionError(f"выбрана реализация {type(credentials).__name__}")

    def test_host_config_keeps_keytab_credentials(self) -> None:
        credentials = ClientCredentials.of(Fixtures.keytab())
        if not isinstance(credentials, KeytabCredentials):
            raise AssertionError(f"выбрана реализация {type(credentials).__name__}")


class TestTicketIsNotRenewedInside:
    """Тикета нет или он истёк — вызов не начинают: выпустить новый нечем."""

    def test_missing_ticket_is_refused(self, tmp_path) -> None:
        config = CcacheConfig(
            principal=Fixtures.PRINCIPAL,
            ccache=f"FILE:{tmp_path / 'absent'}",
            krb5_config=Fixtures.KRB5,
        )

        with pytest.raises(CredentialsExpiredError):
            CcacheCredentials(config).ensure()

    def test_env_has_no_client_keytab(self) -> None:
        """KRB5_CLIENT_KTNAME телу не ставится: kinit внутри делать нечем."""
        config = CcacheConfig(
            principal=Fixtures.PRINCIPAL,
            ccache=Fixtures.CCACHE,
            krb5_config=Fixtures.KRB5,
        )

        env = CcacheCredentials(config).env()
        if KerberosEnv.CLIENT_KEYTAB in env:
            raise AssertionError(f"телу дан keytab окружением: {dict(env)}")

        if env[KerberosEnv.CCACHE] != Fixtures.CCACHE:
            raise AssertionError(f"тикет не тот: {dict(env)}")
