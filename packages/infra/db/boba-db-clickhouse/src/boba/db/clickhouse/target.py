"""Таблица-приёмник ClickHouse как адрес: база, имя, двойник и параметры
имён для запросов приёмника. Без драйвера: модуль читают и фасады
инструментов, которые грузят драйвер лениво."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from boba.db.clickhouse.query import ChIdentifier
from boba.toolkit.transfer import StreamWire, TemplateVar, TemplateVars

__all__ = ["ChCluster", "ChPlacement", "ChStreamWire", "ChTableRef", "ChTableRole"]


class ChStreamWire(StrEnum):
    """Формат данных, который LLM называет источнику ClickHouse параметром
    wire: tsv — текст TabSeparated с контрактом из шапки
    TabSeparatedWithNamesAndTypes того же запроса, типы ClickHouse как есть;
    arrow — поток Arrow IPC с нейтральным контрактом для любого приёмника."""

    TSV = "tsv"
    ARROW = "arrow"

    def wire(self) -> StreamWire:
        return StreamWire(self.value)


class ChTableRole(StrEnum):
    """Роль таблицы в запросе приёмника: префикс параметров её базы и имени,
    в тексте запроса — %(<роль>_db)s.%(<роль>_name)s."""

    TABLE = "table"
    TWIN = "twin"
    RENAMED = "renamed"


@dataclass(frozen=True)
class ChTableRef:
    """Таблица-приёмник: база и имя; двойник — имя с суффиксом __ex. В
    запрос имя попадает параметрами драйвера, которые квотирует он сам."""

    EX_SUFFIX: ClassVar[str] = "__ex"
    CREATE_TABLE: ClassVar[str] = (
        "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
        "engine = ReplicatedMergeTree order by {order_by}"
    )
    """Шаблон create table по умолчанию: реплицируемая таблица, путь в Keeper
    и имя реплики берутся из default_replica_path и default_replica_name
    сервера; без кластера сервер такую таблицу не создаст."""
    ORDER_BY: ClassVar[str] = "tuple()"
    """Ключ сортировки по умолчанию: без сортировки."""
    TEMPLATE_VARS: ClassVar[TemplateVars] = TemplateVars(
        offered=(
            TemplateVar.DATABASE,
            TemplateVar.TABLE_NAME,
            TemplateVar.COLUMNS,
            TemplateVar.CLUSTER,
            TemplateVar.ORDER_BY,
        )
    )
    """Переменные, которые приёмник ClickHouse подставляет в шаблон; cluster
    пуст без кластера — в шаблоне его ставят в [ ]."""

    database: str
    name: str

    def twin(self) -> ChTableRef:
        return ChTableRef(database=self.database, name=f"{self.name}{self.EX_SUFFIX}")

    def text(self) -> str:
        return f"{self.database}.{self.name}"

    def bind(self, role: ChTableRole) -> dict[str, ChIdentifier]:
        return {
            f"{role.value}_db": ChIdentifier(self.database),
            f"{role.value}_name": ChIdentifier(self.name),
        }


@dataclass(frozen=True)
class ChCluster:
    """Кластер ClickHouse, на котором приёмник выполняет DDL: create, drop,
    rename, двойник и exchange tables идут ON CLUSTER; пустое имя — без
    кластера. Вставка и выборки идут на узел соединения."""

    name: str

    def given(self) -> bool:
        return bool(self.name)

    def template_value(self) -> str:
        """Значение {cluster} шаблона: имя в обратных кавычках или пусто."""
        if not self.given():
            return ""

        return str(ChIdentifier(self.name))

    def bind(self) -> dict[str, ChIdentifier]:
        return {"cluster": ChIdentifier(self.name)}


@dataclass(frozen=True)
class ChPlacement:
    """Где и как живёт таблица приёмника: кластер DDL и ключ сортировки для
    шаблона create table (выражение как в SQL: id, (dt, id), tuple())."""

    cluster: ChCluster
    order_by: str
