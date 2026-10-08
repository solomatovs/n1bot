"""Таблица-приёмник ClickHouse как адрес: база, имя, двойник и параметры
имён для запросов приёмника. Без драйвера: модуль читают и фасады
инструментов, которые грузят драйвер лениво."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Annotated, ClassVar

from pydantic import Field

from boba.db.clickhouse.query import ChIdentifier
from boba.toolkit.transfer import StreamWire, TemplateVar, TemplateVars

__all__ = [
    "ChCluster",
    "ChInsertTuning",
    "ChPlacement",
    "ChStreamWire",
    "ChTableRef",
    "ChTableRole",
    "InsertMemory",
    "InsertMemoryBytes",
]


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


class InsertMemory(IntEnum):
    """Опорные величины расчёта настроек вставки, байт. Сняты замерами на
    ClickHouse 26.7: пик памяти запроса вставки близок к трём блокам вставки
    плюс постоянная часть."""

    OVERHEAD = 96 << 20
    """Постоянная часть памяти запроса вставки сверх блоков."""

    BLOCK_FLOOR = 16 << 20
    """Наименьший блок вставки: мельче — сервер копит куски таблицы быстрее,
    чем сливает, и начинает тормозить вставку."""

    BLOCK_CEIL = 256 << 20
    """Наибольший блок вставки: значение сервера по умолчанию."""

    FLOOR = OVERHEAD + 3 * BLOCK_FLOOR
    """Наименьшая память вставки: постоянная часть и три наименьших блока."""

    DEFAULT = 512 << 20
    """Память вставки, когда вызов её не назвал."""

    PARALLEL_PARSING = 512 << 20
    """Сколько памяти сверх блоков нужно параллельному разбору текста."""


InsertMemoryBytes = Annotated[
    int,
    Field(
        ge=InsertMemory.FLOOR,
        description=(
            "Сколько памяти сервера ClickHouse можно занять этой вставкой, в "
            "байтах. От неё считаются настройки вставки:\n"
            "   - блок вставки — около трети за вычетом постоянной части\n"
            "   - меньше — меньше память, больше кусков таблицы\n"
        ),
    ),
]
"""LLM-аргумент приёмника ClickHouse: память сервера на вставку."""


class ChInsertTuning:
    """Настройки сессии сервера для потоковой вставки, посчитанные от памяти,
    которую вставке разрешено занять.

    Создаётся приёмником (ch_stream_in) из аргумента max_insert_memory_bytes.
    Отдаёт стейтменты SET: приёмник выполняет их в начале сессии, до шагов
    before вызывающего, поэтому любой его SET перекрывает посчитанное. К
    серверу за его настройками и свободной памятью расчёт не обращается.

    Блок вставки берётся из памяти: пик запроса близок к трём блокам плюс
    постоянная часть (InsertMemory). Порог блока в строках равен порогу в
    байтах — строка не бывает короче байта, поэтому решает порог в байтах.
    Данные текстом сервер сначала разбирает блоками по числу строк и умеет
    разбирать параллельно: блок разбора подгоняется под блок вставки по
    оценке ширины строки, параллельный разбор включается, только когда
    памяти хватает и на него.
    """

    VALUE_BYTES: ClassVar[int] = 16
    """Оценка одного значения в строке текста, байт: по ней блок разбора в
    строках подгоняется под блок вставки в байтах."""

    PARSE_ROWS_FLOOR: ClassVar[int] = 8192
    PARSE_ROWS_CEIL: ClassVar[int] = 1048449
    """Пределы блока разбора текста в строках; верхний — значение сервера по
    умолчанию."""

    def __init__(self, memory_bytes: int) -> None:
        self._memory = memory_bytes

    def block_bytes(self) -> int:
        """Блок вставки: треть памяти за вычетом постоянной части, в
        пределах от наименьшего до наибольшего блока."""
        block = (self._memory - InsertMemory.OVERHEAD) // 3
        if block < InsertMemory.BLOCK_FLOOR:
            return int(InsertMemory.BLOCK_FLOOR)

        if block > InsertMemory.BLOCK_CEIL:
            return int(InsertMemory.BLOCK_CEIL)

        return block

    def parse_rows(self, columns: int) -> int:
        """Блок разбора текста в строках: блок вставки, делённый на оценку
        ширины строки из columns значений."""
        rows = self.block_bytes() // (columns * self.VALUE_BYTES)
        if rows < self.PARSE_ROWS_FLOOR:
            return self.PARSE_ROWS_FLOOR

        if rows > self.PARSE_ROWS_CEIL:
            return self.PARSE_ROWS_CEIL

        return rows

    def parallel_parsing(self) -> bool:
        """Хватает ли памяти сверх блоков на параллельный разбор текста."""
        blocks = InsertMemory.OVERHEAD + 3 * self.block_bytes()

        return self._memory - blocks >= InsertMemory.PARALLEL_PARSING

    def settings(self, wire: StreamWire, columns: int) -> dict[str, int]:
        """Настройки сессии для вставки потока формата wire из columns
        колонок, в порядке установки."""
        block = self.block_bytes()
        chosen = {
            "max_memory_usage": self._memory,
            "min_insert_block_size_bytes": block,
            "min_insert_block_size_rows": block,
            "max_insert_threads": 1,
        }
        if wire is StreamWire.ARROW:
            return chosen

        chosen["max_insert_block_size"] = self.parse_rows(columns)
        chosen["input_format_parallel_parsing"] = int(self.parallel_parsing())

        return chosen

    def statements(self, wire: StreamWire, columns: int) -> tuple[str, ...]:
        """Стейтменты SET этих настроек для начала сессии."""
        steps: list[str] = []
        for name, value in self.settings(wire, columns).items():
            steps.append(f"SET {name} = {value}")

        return tuple(steps)
