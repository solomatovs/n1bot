"""Общее у форматов: снятие первых строк с байтового потока, склейка
шапки с потоком, типы колонок по именам через драйвер. Не форматер: форматеры
зовут его, друг о друге не знают."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from clickhouse_connect.datatypes.base import ClickHouseType
from clickhouse_connect.datatypes.registry import get_from_name
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks

__all__ = [
    "BackslashEscapes",
    "ColumnTypes",
    "HeadLines",
    "JsonExactOutput",
    "Lines",
    "Settings",
    "TsvEscape",
]


@dataclass(frozen=True)
class HeadLines:
    """Строки шапки без переводов строки и байты, пришедшие следом за ней."""

    lines: tuple[bytes, ...]
    rest: bytes


class Lines:
    """Снимает заданное число строк с начала байтового потока; остальное
    отдаёт дальше как есть, без просмотра байтов."""

    LINE_END: ClassVar[bytes] = b"\n"

    def __init__(self, fmt: str, count: int) -> None:
        self._fmt = fmt
        self._count = count

    async def take(self, chunks: AsyncIterator[memoryview]) -> HeadLines:
        head = bytearray()
        lines: list[bytes] = []
        start = 0
        while len(lines) < self._count:
            end = head.find(self.LINE_END, start)
            if end >= 0:
                lines.append(bytes(head[start:end]))
                start = end + 1
                continue

            chunk = await anext(chunks, None)
            if chunk is None:
                raise ClickHouseFormatError(
                    f"reading {self._fmt} header: expected {self._count} header "
                    f"lines, the stream ended after {len(head)} bytes with "
                    f"{len(lines)} of them"
                )

            head.extend(chunk)

        return HeadLines(lines=tuple(lines), rest=bytes(head[start:]))

    @staticmethod
    async def views(blocks: Blocks) -> AsyncIterator[memoryview]:
        """Любой байтовый поток как поток memoryview."""
        async for block in blocks:
            yield memoryview(block)

    @staticmethod
    async def all(
        head: bytes, source: AsyncIterator[memoryview]
    ) -> AsyncIterator[memoryview]:
        """Шапка (или остаток после неё) перед потоком."""
        if head:
            yield memoryview(head)

        async for block in source:
            yield block


class ColumnTypes:
    """Типы колонок по их именам из шапки: строит драйвер, незнакомое имя —
    ошибка формата."""

    def __init__(self, fmt: str) -> None:
        self._fmt = fmt

    def of(self, type_names: Sequence[str]) -> tuple[ClickHouseType, ...]:
        column_types: list[ClickHouseType] = []
        for type_name in type_names:
            try:
                column_types.append(get_from_name(type_name))
            except DriverError as exc:
                raise ClickHouseFormatError(
                    f"reading {self._fmt} header: expected a clickhouse type name, "
                    f"got {type_name!r}: {exc}"
                ) from exc

        return tuple(column_types)


class Settings:
    """Настройки сервера формата с наложенными сверху настройками вызывающего."""

    def __init__(self, own: Mapping[str, Any]) -> None:
        self._own = dict(own)

    def merged(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        chosen = dict(self._own)
        if extra:
            chosen.update(extra)

        return chosen


class TsvEscape(StrEnum):
    """Буквы escape-последовательностей ClickHouse (правила Escaped и Quoted),
    означающие управляющий символ. Кроме них сервер экранирует только `\\\\`
    и `\\'`, где символ после слэша означает сам себя; всё остальное, включая
    прочие управляющие символы и не-ASCII, пишется как есть."""

    TAB = "t"
    NEWLINE = "n"
    RETURN = "r"
    BACKSPACE = "b"
    FORM_FEED = "f"
    NUL = "0"

    def char(self) -> str:
        match self:
            case TsvEscape.TAB:
                return "\t"
            case TsvEscape.NEWLINE:
                return "\n"
            case TsvEscape.RETURN:
                return "\r"
            case TsvEscape.BACKSPACE:
                return "\b"
            case TsvEscape.FORM_FEED:
                return "\f"
            case TsvEscape.NUL:
                return "\0"


class BackslashEscapes:
    """Экранирование обратным слэшем по правилам сервера: раскрывает буквы
    TsvEscape, `\\\\` и `\\'`, а при записи экранирует ровно те же символы."""

    ESCAPE: ClassVar[str] = "\\"
    QUOTE: ClassVar[str] = "'"

    def __init__(self) -> None:
        self._escapes: dict[str, str] = {}
        for escape in TsvEscape:
            self._escapes[escape.char()] = self.ESCAPE + escape.value

        self._escapes[self.ESCAPE] = self.ESCAPE + self.ESCAPE
        self._escapes[self.QUOTE] = self.ESCAPE + self.QUOTE

    def unescaped(self, field: str) -> str:
        if self.ESCAPE not in field:
            return field

        chars: list[str] = []
        escaped = False
        for char in field:
            if escaped:
                chars.append(self._escape_of(char))
                escaped = False
                continue

            if char == self.ESCAPE:
                escaped = True
                continue

            chars.append(char)

        return "".join(chars)

    def escaped(self, value: str) -> str:
        chars: list[str] = []
        for char in value:
            chars.append(self._escapes.get(char, char))

        return "".join(chars)

    def _escape_of(self, char: str) -> str:
        try:
            escape = TsvEscape(char)
        except ValueError:
            return char

        return escape.char()


class JsonExactOutput(StrEnum):
    """Настройки вывода JSON, при которых путь через JSON и обратно в
    ClickHouse совпадает с TSV байт в байт, а 22.x и новые версии пишут
    одинаковые байты. Без них NaN и Inf уходят null и возвращаются нулём,
    а 64-битные целые одни версии пишут строкой, другие числом."""

    QUOTE_DENORMALS = "output_format_json_quote_denormals"
    QUOTE_64BIT_INTEGERS = "output_format_json_quote_64bit_integers"
    QUOTE_64BIT_FLOATS = "output_format_json_quote_64bit_floats"
    QUOTE_DECIMALS = "output_format_json_quote_decimals"
    VALIDATE_UTF8 = "output_format_json_validate_utf8"
    ESCAPE_FORWARD_SLASHES = "output_format_json_escape_forward_slashes"

    def value_of(self) -> int:
        """Значение настройки: всё включено, кроме замены невалидного UTF-8,
        которая портит бинарные строки и FixedString."""
        match self:
            case JsonExactOutput.VALIDATE_UTF8:
                return 0
            case _:
                return 1
