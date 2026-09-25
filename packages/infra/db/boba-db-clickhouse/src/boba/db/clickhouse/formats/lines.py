"""Общее у форматов с шапкой: снятие первых строк с байтового потока, склейка
шапки с потоком, типы колонок по именам через драйвер. Не форматер: форматеры
зовут его, друг о друге не знают."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from clickhouse_connect.datatypes.base import ClickHouseType
from clickhouse_connect.datatypes.registry import get_from_name
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks

__all__ = ["ColumnTypes", "HeadLines", "LineHead", "Settings"]


@dataclass(frozen=True)
class HeadLines:
    """Строки шапки без переводов строки и байты, пришедшие следом за ней."""

    lines: tuple[bytes, ...]
    rest: bytes


class LineHead:
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
    async def glued(
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
