"""Закрытие ресурсов процесса, которые живут дольше тела инструмента.

Своих ошибок нет: ошибка закрытия ресурса идёт наверх как есть.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

__all__ = ["Closer", "ProcessClosers"]

Closer = Callable[[], Awaitable[None]]
"""Асинхронное закрытие ресурса без аргументов."""


class ProcessClosers:
    """Реестр закрытий процессных ресурсов: кэшированных пулов и всего, что
    переживает один вызов. Инфра-пакет регистрирует закрытие, открывая
    ресурс, а ToolMain закрывает всё после тела инструмента в том же цикле
    событий. Без этого asyncio.run снимает такие ресурсы отменой задач, а
    воркер psycopg_pool отмену поглощает, и процесс инструмента не выходит.
    """

    _CLOSERS: ClassVar[list[Closer]] = []

    def register(self, closer: Closer) -> None:
        """Добавить закрытие; повторная регистрация того же — без эффекта."""
        if closer in self._CLOSERS:
            return

        self._CLOSERS.append(closer)

    async def close_all(self) -> None:
        """Закрыть зарегистрированное в порядке регистрации."""
        for closer in list(self._CLOSERS):
            await closer()
