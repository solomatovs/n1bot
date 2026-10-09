"""Секция [ix_stand] глазами стенда перекачки: источники postgres (ключ
sources), ClickHouse (ch_sources) и Oracle (ora_sources) с профилями.

Ошибки:
IxStandError — конфиг стенда недоступен или неполон.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.oracle.connection import OracleConfig
from boba.db.postgres.connection import PostgresConfig
from boba.stand.ix import IxStand, IxStandError

__all__ = ["ChSource", "OraSource", "PgSource", "PumpStand"]


class PgSource(BaseModel):
    """Источник postgres или Greenplum: имя и профиль."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    postgres: PostgresConfig

    PLAIN: ClassVar[str] = "pg-"
    GREENPLUM: ClassVar[str] = "gp-"
    GREENPLUM_KERNELS: ClassVar[Mapping[str, int]] = {"gp-6": 90400, "gp-7": 120000}
    """Версия ядра postgres у веток Greenplum стенда."""

    @property
    def kernel(self) -> int:
        """Версия ядра postgres числом server_version_num без патча, по имени
        источника: pg-9.4 — 90400, pg-14 — 140000. По ней наборы параметров
        тестов строятся до соединения с сервером."""
        if self.name in self.GREENPLUM_KERNELS:
            return self.GREENPLUM_KERNELS[self.name]

        if not self.name.startswith(self.PLAIN):
            raise IxStandError(
                f"ix stand: source {self.name!r} is neither {self.PLAIN}<version> "
                f"nor one of {sorted(self.GREENPLUM_KERNELS)}"
            )

        parts = self.name.removeprefix(self.PLAIN).split(".")
        version = int(parts[0]) * 10000
        if len(parts) > 1:
            version += int(parts[1]) * 100

        return version


class ChSource(BaseModel):
    """Источник ClickHouse: имя, профиль и разрешение создавать базы; чужой
    кластер (demo = false) в матрицы не входит."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    PREFIX: ClassVar[str] = "ch-"

    @property
    def major(self) -> int:
        """Мажорная версия сервера по имени источника: ch-24.12 — 24."""
        release = self.name.removeprefix(self.PREFIX).split(".")[0]
        if not self.name.startswith(self.PREFIX) or not release.isdigit():
            raise IxStandError(
                f"ix stand: clickhouse source {self.name!r} is not "
                f"{self.PREFIX}<major>.<minor>"
            )

        return int(release)

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

    PREFIX: ClassVar[str] = "ora-"

    @property
    def release(self) -> int:
        """Релиз сервера по имени источника: ora-12.2 — 12, ora-23 — 23."""
        release = self.name.removeprefix(self.PREFIX).split(".")[0]
        if not self.name.startswith(self.PREFIX) or not release.isdigit():
            raise IxStandError(
                f"ix stand: oracle source {self.name!r} is not {self.PREFIX}<release>"
            )

        return int(release)


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

    def since(self, kernel: int) -> list[PgSource]:
        """Источники с ядром не старше данного: случаи, которым нужна функция
        этой версии, на старых серверах не порождаются."""
        chosen: list[PgSource] = []
        for source in self.sources:
            if source.kernel >= kernel:
                chosen.append(source)

        return chosen

    def clickhouse_since(self, major: int) -> list[ChSource]:
        """Источники ClickHouse матриц с версией не старше данной."""
        chosen: list[ChSource] = []
        for source in self.demo_clickhouse():
            if source.major >= major:
                chosen.append(source)

        return chosen

    def oracle_before(self, release: int) -> list[OraSource]:
        """Источники Oracle старше данного релиза: случаи поведения, которое
        релиз убрал."""
        chosen: list[OraSource] = []
        for source in self.ora_sources:
            if source.release < release:
                chosen.append(source)

        return chosen

    def newest_postgres(self) -> PgSource:
        """Самый новый postgres стенда: последний источник pg-* (Greenplum не
        в счёт)."""
        return self.postgres_family(PgSource.PLAIN)[-1]

    def greenplum(self) -> list[PgSource]:
        return self.postgres_family(PgSource.GREENPLUM)

    def postgres_family(self, prefix: str) -> list[PgSource]:
        """Источники одного семейства по префиксу имени; пустой список — ошибка
        стенда: тесты семейства обязаны выполняться."""
        chosen: list[PgSource] = []
        for source in self.sources:
            if source.name.startswith(prefix):
                chosen.append(source)

        if not chosen:
            names = [source.name for source in self.sources]
            raise IxStandError(
                f"ix stand: [ix_stand].sources has no {prefix}* source, got {names}"
            )

        return chosen
