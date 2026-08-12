"""Живой стенд postgres: таблицы, строки, наборы сценариев и узлы pg для графа.

Адрес базы приходит переменной окружения PgStand.DSN_ENV; без неё сценарий
пропускается. Таблицы стенда создаёт и убирает сам стенд, поэтому прогоны не
наследуют строки друг друга.

Ошибки: StandError (boba.stand.flow) — набор сценария меньше буфера ребра,
то есть поток не докажет одновременность стадий; psycopg.Error — стенд базы
недоступен либо оператор отвергнут; pydantic.ValidationError — строка стенда
не по модели.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict, Field

from boba.sandbox.runner import ChannelPump
from boba.stand.flow import StageContribution, StandError, StandPaths, StandSandbox
from boba.tool.pg.executor import PgExecutorConfig
from boba.tool.pg.protocol import PgCopyDirection, PgStage
from boba.tool.pg.stages import PgStages
from boba.toolkit.channels import ByteText
from boba.toolkit.workflow import StageSpec


class StandTable(StrEnum):
    """Таблицы стенда: источник графа и приёмник заливки; колонки id, name."""

    SOURCE = "boba_stand_source"
    SINK = "boba_stand_sink"

    def identifier(self) -> sql.Identifier:
        return sql.Identifier(self.value)

    def copy_to_stdout(self) -> str:
        """Оператор выгрузки для узла pg_copy направления to_stdout."""
        query = f"SELECT id, name FROM {self.value} ORDER BY id"  # noqa: S608

        return f"COPY ({query}) TO STDOUT WITH (FORMAT CSV)"

    def copy_from_stdin(self) -> str:
        """Оператор заливки для узла pg_copy направления from_stdin."""
        return f"COPY {self.value} (id, name) FROM STDIN WITH (FORMAT CSV)"

    def select_rows(self) -> str:
        """Запрос строк для узла pg_query: порядок задан, чтобы сверять поток."""
        return f"SELECT id, name FROM {self.value} ORDER BY id"  # noqa: S608


class StandRow(BaseModel):
    """Строка таблицы стенда: наполнение источника и сверка продукта графа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str = Field(min_length=1)


class StandCsv:
    """CSV стенда: строки модели в байты канала.

    Правила совпадают с `FORMAT CSV` postgres: поле берётся в кавычки, только
    если несёт запятую, кавычку или перевод строки; кавычка внутри удваивается.
    """

    DELIMITER: ClassVar[str] = ","
    QUOTE: ClassVar[str] = '"'
    LINE_END: ClassVar[str] = "\n"

    @classmethod
    def render(cls, rows: Sequence[StandRow]) -> bytes:
        """Байты, которые ждёт COPY ... FROM STDIN и отдаёт COPY ... TO STDOUT."""
        buffer = io.StringIO()

        writer = csv.writer(
            buffer,
            delimiter=cls.DELIMITER,
            quotechar=cls.QUOTE,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator=cls.LINE_END,
        )

        for row in rows:
            writer.writerow((str(row.id), row.name))

        return buffer.getvalue().encode(ByteText.ENCODING)


class StandData:
    """Наборы строк сценариев: поток заведомо больше буфера ребра насоса.

    Число строк выводится из ChannelPump.EDGE_BUFFER_BYTES: и весь набор, и
    его доля KEEP обязаны превышать буфер в EDGE_FACTOR раз, иначе стадии
    обменялись бы готовым буфером, а не работали одновременно. Каждый набор
    проверяет свой объём на сборке и падает StandError, если буфер вырос.
    """

    EDGE_FACTOR: ClassVar[int] = 2
    MARK_EVERY: ClassVar[int] = 2
    KEEP: ClassVar[str] = "keep"
    DROP: ClassVar[str] = "drop"
    PAD: ClassVar[str] = "набивка-набивка-набивка-набивка"
    MEASURE_ID: ClassVar[int] = 1
    """Мерная строка: самый короткий id набора даёт длину строки снизу."""

    SMALL_ROWS: ClassVar[int] = 64
    LONG_NAME_CHARS: ClassVar[int] = 4000
    TRICKY_BASE: ClassVar[int] = 900_000
    BROKEN_LINE: ClassVar[bytes] = b"not-a-number,broken row\n"
    BROKEN_AT: ClassVar[int] = 2
    """Сколько годных строк идёт до битой: остальной поток остаётся за ней."""

    @classmethod
    def edge_floor(cls) -> int:
        """Сколько байт обязан превысить каждый потоковый набор сценария."""
        return ChannelPump.EDGE_BUFFER_BYTES * cls.EDGE_FACTOR

    @classmethod
    def bulk_rows(cls) -> int:
        """Столько строк, чтобы и весь набор, и его доля KEEP перекрыли буфер."""
        sample = StandRow(id=cls.MEASURE_ID, name=cls._name(cls.MEASURE_ID))
        row_bytes = len(StandCsv.render((sample,)))

        kept_rows = cls.edge_floor() // row_bytes + 1

        return kept_rows * cls.MARK_EVERY

    @classmethod
    def bulk(cls) -> Sequence[StandRow]:
        """Однострочные записи без спецсимволов: годятся построчному фильтру."""
        rows = tuple(cls._bulk_rows())
        cls._check_stream(rows)

        return rows

    @classmethod
    def kept(cls) -> Sequence[StandRow]:
        """Те же записи, что оставит фильтр по слову KEEP."""
        kept: list[StandRow] = []
        for row in cls._bulk_rows():
            if cls.KEEP not in row.name:
                continue

            kept.append(row)

        rows = tuple(kept)
        cls._check_stream(rows)

        return rows

    @classmethod
    def small(cls) -> Sequence[StandRow]:
        """Короткий набор: сценарию нужен не объём, а сам факт потока."""
        rows: list[StandRow] = []
        for index in range(1, cls.SMALL_ROWS + 1):
            rows.append(StandRow(id=index, name=cls._name(index)))

        return tuple(rows)

    @classmethod
    def tricky(cls) -> Sequence[StandRow]:
        """Недружелюбный текст: кириллица, кавычки, запятые, перевод строки."""
        names = (
            "Анна Каренина",
            'comma, quote " and backslash \\',
            "line one\nline two",
            "ё" * cls.LONG_NAME_CHARS,
            "tab\there; semicolon; æøå ±£¥",
        )

        rows: list[StandRow] = []
        for offset, name in enumerate(names, start=1):
            rows.append(StandRow(id=cls.TRICKY_BASE + offset, name=name))

        return tuple(rows)

    @classmethod
    def mixed(cls) -> Sequence[StandRow]:
        """Объём плюс недружелюбный текст; порядок совпадает с порядком по id."""
        rows: list[StandRow] = list(cls.bulk())
        rows.extend(cls.tricky())

        return tuple(rows)

    @classmethod
    def broken_csv(cls) -> bytes:
        """Короткий поток, где ранняя строка ломает тип колонки id."""
        rows = cls.small()

        head = StandCsv.render(rows[: cls.BROKEN_AT])
        tail = StandCsv.render(rows[cls.BROKEN_AT :])

        return head + cls.BROKEN_LINE + tail

    @classmethod
    def _check_stream(cls, rows: Sequence[StandRow]) -> None:
        size = len(StandCsv.render(rows))
        floor = cls.edge_floor()
        if size > floor:
            return

        raise StandError(
            f"stand stream is {size} bytes, it must exceed {floor} "
            f"({ChannelPump.EDGE_BUFFER_BYTES} edge buffer x {cls.EDGE_FACTOR})"
        )

    @classmethod
    def _bulk_rows(cls) -> Iterator[StandRow]:
        for index in range(1, cls.bulk_rows() + 1):
            yield StandRow(id=index, name=cls._name(index))

    @classmethod
    def _name(cls, index: int) -> str:
        mark = cls.DROP
        if index % cls.MARK_EVERY == 0:
            mark = cls.KEEP

        return f"row-{index:06d}-{mark}-{cls.PAD}"


class PgStand:
    """Живой postgres сценариев: таблицы стенда, наполнение и чтение с хоста.

    Адрес приходит переменной окружения; без неё сценарий пропускается.
    """

    DSN_ENV: ClassVar[str] = "BOBA_TEST_PG_DSN"
    CONNECT_KEYS: ClassVar[tuple[str, ...]] = (
        "host",
        "port",
        "dbname",
        "user",
        "password",
    )
    CONNECT_TIMEOUT: ClassVar[int] = 5

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @classmethod
    def required(cls) -> PgStand:
        """Стенд по переменной окружения; без переменной тест пропускается."""
        dsn = os.environ.get(cls.DSN_ENV)
        if not dsn:
            pytest.skip(f"{cls.DSN_ENV} is not set: live postgres stand is absent")

        return cls(dsn)

    def connection(self) -> Mapping[str, Any]:
        """Профиль подключения стенда для whitelist'а узлов postgres."""
        parsed = conninfo_to_dict(self._dsn)

        profile: dict[str, Any] = {"connect_timeout": self.CONNECT_TIMEOUT}
        for key in self.CONNECT_KEYS:
            value = parsed.get(key)
            if value is None:
                continue

            profile[key] = value

        return profile

    def reset(self) -> None:
        """Пустые источник и приёмник: прогоны не наследуют строки друг друга."""
        with psycopg.connect(self._dsn) as conn:
            for table in StandTable:
                conn.execute(self._drop(table))
                conn.execute(self._create(table))

    def drop(self) -> None:
        """Уборка после сценария: таблиц стенда в базе не остаётся."""
        with psycopg.connect(self._dsn) as conn:
            for table in StandTable:
                conn.execute(self._drop(table))

    def fill(self, table: StandTable, rows: Sequence[StandRow]) -> None:
        """Наполнение таблицы стенда строками сценария: объёмный набор идёт COPY."""
        statement = sql.SQL("COPY {} (id, name) FROM STDIN WITH (FORMAT CSV)").format(
            table.identifier()
        )

        data = StandCsv.render(rows)

        with (
            psycopg.connect(self._dsn) as conn,
            conn.cursor() as cur,
            cur.copy(statement) as loader,
        ):
            loader.write(data)

    def rows(self, table: StandTable) -> Sequence[StandRow]:
        """Содержимое таблицы стенда моделями, порядок по id."""
        statement = sql.SQL("SELECT id, name FROM {} ORDER BY id").format(
            table.identifier()
        )

        with psycopg.connect(self._dsn) as conn:
            cursor = conn.execute(statement)
            fetched = cursor.fetchall()

        rows: list[StandRow] = []
        for row_id, name in fetched:
            rows.append(StandRow(id=row_id, name=name))

        return rows

    @staticmethod
    def _drop(table: StandTable) -> sql.Composed:
        return sql.SQL("DROP TABLE IF EXISTS {}").format(table.identifier())

    @staticmethod
    def _create(table: StandTable) -> sql.Composed:
        return sql.SQL("CREATE TABLE {} (id int, name text)").format(
            table.identifier()
        )


class PgNodes:
    """Узлы postgres для стенда: вклад в реестр и заготовки args сценариев."""

    CONNECTION: ClassVar[str] = "stand"
    MAX_ROWS: ClassVar[int] = 1_000_000
    MAX_BYTES: ClassVar[int] = 256 * 1024 * 1024
    PACKAGES: ClassVar[tuple[str, ...]] = (
        "core/boba-cancellation",
        "core/boba-toolkit",
        "infra/db/boba-db-postgres",
        "infra/krb/boba-krb",
        "tools/boba-tool-postgres",
    )

    @classmethod
    def contribution(
        cls,
        connection: Mapping[str, Any],
        workspace: Path,
    ) -> StageContribution:
        """Узлы pg_query и pg_copy с профилем стенда: базе нужна сеть."""
        cfg = PgExecutorConfig.model_validate(
            {
                "profiles": {cls.CONNECTION: dict(connection)},
                "max_rows": cls.MAX_ROWS,
                "max_bytes": cls.MAX_BYTES,
            }
        )

        sandbox = StandSandbox(
            packages=cls.PACKAGES,
            rw_binds=(StandPaths.workspace_bind(workspace),),
            network=True,
        )

        return StageContribution(nodes=PgStages.of(cfg), profile=sandbox.profile())

    @staticmethod
    def copy_out(stage_id: str, table: StandTable) -> StageSpec:
        return StageSpec(
            id=stage_id,
            tool=PgStage.COPY,
            args={
                "connection_name": PgNodes.CONNECTION,
                "direction": PgCopyDirection.TO_STDOUT,
                "sql": table.copy_to_stdout(),
            },
        )

    @staticmethod
    def copy_in(stage_id: str, table: StandTable) -> StageSpec:
        return StageSpec(
            id=stage_id,
            tool=PgStage.COPY,
            args={
                "connection_name": PgNodes.CONNECTION,
                "direction": PgCopyDirection.FROM_STDIN,
                "sql": table.copy_from_stdin(),
            },
        )

    @staticmethod
    def query(stage_id: str, statement: str) -> StageSpec:
        return StageSpec(
            id=stage_id,
            tool=PgStage.QUERY,
            args={
                "connection_name": PgNodes.CONNECTION,
                "sql": statement,
                "params": [],
            },
        )
