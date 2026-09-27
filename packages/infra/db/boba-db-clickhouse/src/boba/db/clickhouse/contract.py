"""Контракт источника ClickHouse и разбор текста его типов без драйвера:
кадр schema с колонками из шапки TabSeparatedWithNamesAndTypes и перевод
текста типа (как его печатает сервер) в нейтральный тип для сверки. Модуль
читают и пары в другие движки, где драйвера ClickHouse нет."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.toolkit.contract import ColumnType, TimeUnit, TypeFamily
from boba.toolkit.transfer import StreamWire

__all__ = ["ChContract", "ChParsedType", "ChSourceColumn", "ChTypes", "ChWrapper"]


class ChSourceColumn(BaseModel):
    """Колонка контракта источника ClickHouse: позиция и имя из шапки ответа,
    текст типа как его напечатал сервер (Nullable внутри текста)."""

    model_config = ConfigDict(frozen=True)

    position: int
    name: str
    type_text: str


class ChContract(BaseModel):
    """Контракт источника ClickHouse в кадре schema: колонки из шапки
    TabSeparatedWithNamesAndTypes того же запроса, что везёт данные, и имя
    сервера из заголовка ответа."""

    model_config = ConfigDict(frozen=True)

    columns: tuple[ChSourceColumn, ...]
    server: str

    def names(self) -> list[str]:
        return [column.name for column in self.columns]

    def render(self, wire: StreamWire) -> str:
        lines = [
            f"streamed out {wire.value} from clickhouse {self.server}, "
            f"{len(self.columns)} columns:"
        ]
        for column in self.columns:
            lines.append(f"  {column.name}: {column.type_text}")

        return "\n".join(lines)


@dataclass(frozen=True)
class ChParsedType:
    """Тип ClickHouse, разобранный из текста: нейтральный тип, nullable,
    длина FixedString (0 — без предела), текст типа без обёрток, его имя
    (head) и аргументы в скобках как напечатал сервер."""

    kind: ColumnType
    nullable: bool
    char_length: int
    text: str
    head: str
    args: str


class ChWrapper(StrEnum):
    """Обёртки типа ClickHouse, которые не меняют семейство значения."""

    NULLABLE = "Nullable"
    LOW_CARDINALITY = "LowCardinality"


class ChTypes:
    """Текст типа ClickHouse (как его печатают system.columns и toTypeName)
    в нейтральный тип: Nullable и LowCardinality снимаются, дальше имя
    типа и его аргументы в скобках. Незнакомое семейство — other."""

    INTEGERS: ClassVar[Mapping[str, int]] = {
        "Int8": 8,
        "Int16": 16,
        "Int32": 32,
        "Int64": 64,
        "Int128": 128,
        "Int256": 256,
    }
    UNSIGNED: ClassVar[Mapping[str, int]] = {
        "UInt8": 8,
        "UInt16": 16,
        "UInt32": 32,
        "UInt64": 64,
        "UInt128": 128,
        "UInt256": 256,
    }
    FLOATS: ClassVar[Mapping[str, int]] = {"Float32": 32, "Float64": 64}
    DECIMALS: ClassVar[Mapping[str, int]] = {
        "Decimal32": 9,
        "Decimal64": 18,
        "Decimal128": 38,
        "Decimal256": 76,
    }
    PLAIN: ClassVar[Mapping[str, TypeFamily]] = {
        "String": TypeFamily.STRING,
        "Bool": TypeFamily.BOOLEAN,
        "Date": TypeFamily.DATE,
        "Date32": TypeFamily.DATE,
        "UUID": TypeFamily.UUID,
        "IPv4": TypeFamily.NETWORK,
        "IPv6": TypeFamily.NETWORK,
        "Enum8": TypeFamily.STRING,
        "Enum16": TypeFamily.STRING,
        "JSON": TypeFamily.JSON,
        "Object": TypeFamily.JSON,
        "Array": TypeFamily.ARRAY,
    }
    SPACES: ClassVar[re.Pattern[str]] = re.compile(r"\s+")
    UNITS: ClassVar[Sequence[tuple[int, TimeUnit]]] = (
        (0, TimeUnit.SECOND),
        (3, TimeUnit.MILLISECOND),
        (6, TimeUnit.MICROSECOND),
        (9, TimeUnit.NANOSECOND),
    )

    def normalized(self, text: str) -> str:
        """Текст типа с одиночными пробелами: новые серверы печатают именованный
        Tuple в несколько строк с отступами, старые — в одну; сверка текстов
        не должна зависеть от версии."""
        collapsed = self.SPACES.sub(" ", text.strip())

        return collapsed.replace("( ", "(").replace(" )", ")")

    def as_nullable(self, text: str) -> str:
        """Тот же тип, но принимающий NULL: Nullable(T); у LowCardinality(T)
        Nullable ставится внутрь, снаружи ClickHouse его не принимает."""
        head, args = self._split(text.strip())
        if head == ChWrapper.NULLABLE.value:
            return text.strip()

        if head == ChWrapper.LOW_CARDINALITY.value:
            return f"{head}({self.as_nullable(args)})"

        return f"{ChWrapper.NULLABLE.value}({text.strip()})"

    def parse(self, text: str) -> ChParsedType:
        inner = self.normalized(text)
        nullable = False
        while True:
            head, args = self._split(inner)
            if head == ChWrapper.NULLABLE.value:
                nullable = True
                inner = args
                continue

            if head == ChWrapper.LOW_CARDINALITY.value:
                inner = args
                continue

            break

        head, args = self._split(inner)
        char_length = 0
        if head == "FixedString":
            char_length = int(args)

        return ChParsedType(
            kind=self._kind(head, args, inner),
            nullable=nullable,
            char_length=char_length,
            text=inner,
            head=head,
            args=args,
        )

    def _kind(self, head: str, args: str, text: str) -> ColumnType:
        parsers: Mapping[str, Callable[[str, str, str], ColumnType]] = {
            "Decimal": self._decimal,
            "FixedString": self._fixed,
            "DateTime": self._datetime,
            "DateTime64": self._moment,
        }
        parser = parsers.get(head)
        if parser is not None:
            return parser(head, args, text)

        if head in self.INTEGERS:
            return ColumnType(TypeFamily.INTEGER, text, bits=self.INTEGERS[head])

        if head in self.UNSIGNED:
            bits = self.UNSIGNED[head]

            return ColumnType(TypeFamily.INTEGER, text, bits=bits, unsigned=True)

        if head in self.FLOATS:
            return ColumnType(TypeFamily.FLOAT, text, bits=self.FLOATS[head])

        if head in self.DECIMALS:
            precision = self.DECIMALS[head]

            return ColumnType(
                TypeFamily.DECIMAL, text, precision=precision, scale=int(args)
            )

        family = self.PLAIN.get(head, TypeFamily.OTHER)

        return ColumnType(family, text)

    def _decimal(self, head: str, args: str, text: str) -> ColumnType:
        precision_text, _, scale_text = args.partition(",")
        scale = 0
        if scale_text.strip():
            scale = int(scale_text)

        return ColumnType(
            TypeFamily.DECIMAL, text, precision=int(precision_text), scale=scale
        )

    @staticmethod
    def _fixed(head: str, args: str, text: str) -> ColumnType:
        return ColumnType(TypeFamily.STRING, text)

    @staticmethod
    def _datetime(head: str, args: str, text: str) -> ColumnType:
        return ColumnType(
            TypeFamily.TIMESTAMP, text, unit=TimeUnit.SECOND, zoned=bool(args)
        )

    def _moment(self, head: str, args: str, text: str) -> ColumnType:
        precision_text, _, zone = args.partition(",")
        precision = int(precision_text)
        unit = TimeUnit.NANOSECOND
        for limit, candidate in self.UNITS:
            if precision <= limit:
                unit = candidate
                break

        return ColumnType(
            TypeFamily.TIMESTAMP, text, unit=unit, zoned=bool(zone.strip())
        )

    @staticmethod
    def _split(text: str) -> tuple[str, str]:
        """Имя типа и текст внутри внешних скобок; без скобок — пусто."""
        head, bracket, rest = text.partition("(")
        if not bracket:
            return head.strip(), ""

        return head.strip(), rest.rstrip()[:-1]
