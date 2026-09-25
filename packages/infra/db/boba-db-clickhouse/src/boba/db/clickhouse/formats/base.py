"""Контракт форматера потока ClickHouse."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any, ClassVar, Protocol, TypeVar

__all__ = ["Blocks", "StreamFormat"]

Blocks = AsyncIterable[bytes | bytearray | memoryview]
"""Байтовый поток в любом буферном представлении: ответ сервера, порт
инструмента, файл."""

TStream = TypeVar("TStream")


class StreamFormat(Protocol[TStream]):
    """Форматер ClickHouse:
        - имя формата
        - настройки сервера под формат
        - хвост запроса вида FORMAT {} для INSERT
        - обёртка байтового потока в поток формата
    реализует read и write стороны"""

    FORMAT: ClassVar[str]

    @abstractmethod
    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        """Настройки сервера для вывода в этом формате; extra накладывается
        сверху и может их перебить."""

    @abstractmethod
    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        """Настройки сервера для ввода в этом формате; extra накладывается сверху."""

    @abstractmethod
    def insert(self, query: str) -> str:
        """Текст `INSERT INTO ...` с FORMAT этого формата."""

    @abstractmethod
    async def read(self, blocks: Blocks) -> TStream:
        """Поток формата над байтовым потоком."""

    @abstractmethod
    def write(self, stream: TStream) -> AsyncIterator[memoryview]:
        """Байтовый поток из потока формата: тело для INSERT."""
