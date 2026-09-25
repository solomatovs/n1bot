"""Секция [ix_stand] глазами стенда перекачки: источники postgres (ключ
sources), ClickHouse (ch_sources) и Oracle (ora_sources) с профилями.

Ошибки:
IxStandError — конфиг стенда недоступен или неполон.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, SecretStr

from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.oracle.connection import OracleConfig, PasswordAuth
from boba.db.postgres.connection import PostgresConfig
from boba.stand.ix import IxStand

__all__ = ["ChSource", "OraSource", "PgSource", "PumpStand"]


class PgSource(BaseModel):
    """Источник postgres или Greenplum: имя и профиль."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    postgres: PostgresConfig


class ChSource(BaseModel):
    """Источник ClickHouse: имя, профиль и разрешение создавать базы; чужой
    кластер (demo = false) в матрицы не входит."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    @property
    def admin(self) -> ClickHouseConfig:
        """Тот же профиль без readonly-настроек сессии: стенд пересоздаётся DDL."""
        return self.clickhouse.model_copy(
            update={"settings": ClickHouseSettingsConfig.model_validate({})}
        )


class OraSource(BaseModel):
    """Источник Oracle: имя, профиль с минимальными правами и профиль
    администратора, которым пересоздаётся схема стенда."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    oracle: OracleConfig
    admin: OracleConfig

    def owner(self, user: str, password: str) -> OracleConfig:
        """Профиль владельца схемы стенда: адрес администратора, учётка схемы."""
        auth = PasswordAuth(method="password", user=user, password=SecretStr(password))

        return self.admin.model_copy(update={"auth": auth})


class PumpStand(IxStand):
    """Общий стенд ix плюс списки источников трёх баз."""

    sources: Sequence[PgSource]
    ch_sources: Sequence[ChSource]
    ora_sources: Sequence[OraSource]

    def demo_clickhouse(self) -> list[ChSource]:
        chosen: list[ChSource] = []
        for source in self.ch_sources:
            if source.demo:
                chosen.append(source)

        return chosen
