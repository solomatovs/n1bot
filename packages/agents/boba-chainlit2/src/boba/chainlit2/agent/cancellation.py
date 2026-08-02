"""Единый механизм остановки хода: признак отмены и реестр прерывателей.

Инструменты синхронные, langchain исполняет их в тред-пуле, а отмена
asyncio-таски поток не прерывает. Поэтому остановка объявляется явно:
TurnCancellation ставит флаг и дёргает прерыватели, зарегистрированные
длинными операциями (subprocess, postgres, http).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import cast

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

__all__ = [
    "CancellableTools",
    "ToolStopped",
    "TurnCancellation",
    "current_cancellation",
    "turn_cancellation",
]


class ToolStopped(BaseException):
    """Инструмент прерван остановкой хода.

    Наследует BaseException намеренно, как asyncio.CancelledError: остановка
    не должна превращаться в ErrorResult обработчиками `except Exception`
    внутри инструментов.
    """


class TurnCancellation:
    """Признак остановки одного хода и реестр прерывателей.

    Флаг ставится из event loop, а читают его рабочие потоки, поэтому в
    основе threading.Event. Прерыватель — callable, который умеет оборвать
    идущую операцию извне (proc.kill, conn.cancel, transport.close);
    регистрируется на время операции через abort_with.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._aborts: dict[int, Callable[[], None]] = {}
        self._next_id = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            aborts = list(self._aborts.values())
        for abort in aborts:
            try:
                abort()
            except Exception:
                logger.exception("прерыватель операции не отработал")

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ToolStopped

    def wait(self, timeout: float) -> bool:
        "ждёт отмены не дольше timeout; True — ход остановлен"
        return self._event.wait(timeout)

    @contextmanager
    def abort_with(self, abort: Callable[[], None]) -> Generator[None]:
        "регистрирует прерыватель на время блока и проверяет отмену на входе"
        self.raise_if_cancelled()
        with self._lock:
            key = self._next_id
            self._next_id += 1
            self._aborts[key] = abort
        try:
            yield
        finally:
            with self._lock:
                self._aborts.pop(key, None)


_NEVER_CANCELLED = TurnCancellation()

_CURRENT: ContextVar[TurnCancellation] = ContextVar(
    "boba_turn_cancellation",
    default=_NEVER_CANCELLED,
)


def current_cancellation() -> TurnCancellation:
    "отмена текущего хода; вне хода — объект, который никогда не отменяется"
    return _CURRENT.get()


@contextmanager
def turn_cancellation() -> Generator[TurnCancellation]:
    "открывает ход: публикует свежий TurnCancellation в контексте"
    cancellation = TurnCancellation()
    token = _CURRENT.set(cancellation)
    try:
        yield cancellation
    finally:
        _CURRENT.reset(token)


class CancellableTools:
    """Общая для всех инструментов защита: не стартовать после остановки.

    Прерыватели обрывают уже идущую операцию, но инструмент, который агент
    только собирается запустить, оборвать нечем. Обёртка закрывает этот
    случай для всего набора разом, включая инструменты без своих
    точек прерывания.

    Отмена проверяется и после вызова: инструменты переводят исключения в
    ErrorResult, поэтому обрыв транспорта прерывателем иначе доехал бы до
    ленты как обычная ошибка вместо остановки.
    """

    @staticmethod
    def guard_all(tools: list[BaseTool]) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            if callable(func):
                tool.func = CancellableTools._guard(func)
            coroutine = cast("Callable[..., Awaitable[object]] | None",
                             getattr(tool, "coroutine", None))
            if callable(coroutine):
                tool.coroutine = CancellableTools._guard_async(coroutine)
        return tools

    @staticmethod
    def _guard(func: Callable[..., object]) -> Callable[..., object]:
        @wraps(func)
        def guarded(*args: object, **kwargs: object) -> object:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            result = func(*args, **kwargs)
            cancellation.raise_if_cancelled()
            return result

        return guarded

    @staticmethod
    def _guard_async(
        coroutine: Callable[..., Awaitable[object]],
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def guarded(*args: object, **kwargs: object) -> object:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            result = await coroutine(*args, **kwargs)
            cancellation.raise_if_cancelled()
            return result

        return guarded
