"""Реестр идущих запусков: один запуск на область (scope.id).

Запуск открывает его владелец — ход чата, REST-вызов, раннер workflow —
контекстом вызова; запись запуска несёт контекст, отмену и порт владельца
(у headless-запусков его нет). Инструменты и отрисовка находят запуск
только через реестр, закрытие записи — единственный finally, гасящий всё,
что подписалось на конец запуска.

Остановка адресуется scope.id и работает из любого потока: синхронный код
прерывают зарегистрированные прерыватели; обрывать ли саму корутину
запуска, решает владелец — task_abort подключается отдельно.

Ошибки:
RefusalError(RunRefusal) — инструменту чата нужен живой ход, а его нет.
ToolStopped поднимает raise_if_cancelled отмены.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import abstractmethod
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation, StopReason
from boba.identity.context import CallContext, CallContexts
from boba.identity.errors import RefusalError

__all__ = [
    "ElementTarget",
    "Run",
    "RunPort",
    "RunRefusal",
    "Runs",
]

logger = logging.getLogger(__name__)


class RunRefusal(StrEnum):
    """Отказы владельца запуска инструменту чата."""

    NO_TURN = "no_turn"
    NO_TOOL_CALL = "no_tool_call"


class ElementTarget(BaseModel):
    """Куда крепится элемент, созданный инструментом: шаг ответа и id элемента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    for_id: str = Field(min_length=1)
    element_id: str = Field(min_length=1)


class RunPort(Protocol):
    """Что инструменту нужно от владельца запуска: куда крепить его элемент."""

    @abstractmethod
    def element_target(self, tool_call_id: str) -> ElementTarget:
        """Адрес элемента вызова; отказ — RefusalError(RunRefusal)."""
        ...

    @abstractmethod
    async def show_element(self, tool_call_id: str, element: Mapping[str, Any]) -> None:
        """Показывает сохранённый элемент вызова во всех вкладках запуска."""
        ...


class Run:
    """Идущий запуск области: контекст вызова, владелец и действия при закрытии.

    Создаёт его Runs.open; читают инструменты и отрисовка через Runs.active.
    Кто держит состояние на время запуска (живые журналы вызовов),
    подписывается on_close и гасит его вместе с запуском.
    """

    def __init__(self, context: CallContext, port: RunPort | None) -> None:
        self._context = context
        self._port = port
        self._lock = threading.Lock()
        self._closers: list[Callable[[], None]] = []

    @property
    def scope_id(self) -> str:
        return self._context.scope.id

    @property
    def context(self) -> CallContext:
        """Контекст вызова, под которым открыт запуск."""
        return self._context

    @property
    def port(self) -> RunPort | None:
        """Владелец с лентой чата; None — запуск headless."""
        return self._port

    @property
    def cancellation(self) -> RunCancellation:
        """Отмена запуска; та же, что опубликована в contextvar исполнения."""
        return self._context.cancellation

    def on_close(self, closer: Callable[[], None]) -> None:
        """Действие при закрытии запуска; зовётся один раз, в порядке подписки."""
        with self._lock:
            self._closers.append(closer)

    def close(self) -> None:
        with self._lock:
            closers = list(self._closers)
            self._closers.clear()

        for closer in closers:
            closer()


class Runs:
    """Реестр идущих запусков процесса: один запуск на область (scope.id).

    Объект один на процесс: его создаёт сборка приложения рядом с держателем
    контекста вызова и отдаёт через конструкторы тем, кто открывает запуски
    (ход чата, вызовы API, задания) и кто их ищет (остановка, журналы
    вызовов, отрисовка элементов).
    """

    def __init__(self, contexts: CallContexts) -> None:
        self._contexts = contexts
        self._lock = threading.Lock()
        self._active: dict[str, Run] = {}

    @contextmanager
    def open(
        self, context: CallContext, port: RunPort | None = None
    ) -> Generator[Run, None, None]:
        """Открывает запуск области: контекст и отмена в исполнении, запись в реестре.

        Закрытие снимает запись и зовёт подписчиков on_close — живые журналы
        вызовов гаснут здесь, файлы журнала переживают запуск.
        """
        with self._contexts.applied(context), context.cancellation.published():
            run = Run(context, port)
            self._register(run)
            try:
                yield run
            finally:
                self._release(run)
                run.close()

    @contextmanager
    def task_abort(self, cancellation: RunCancellation) -> Generator[None, None, None]:
        """Отмена корутины запуска как прерыватель: выбор владельца, не реестра."""
        abort = self._task_canceller()
        if abort is None:
            yield
            return

        with cancellation.abort_with(abort):
            yield

    def active(self, scope_id: str) -> Run | None:
        """Запись идущего запуска; None — область ничем не занята."""
        with self._lock:
            return self._active.get(scope_id)

    def port_of(self, scope_id: str) -> RunPort | None:
        """Владелец запуска области для инструментов; None — запуска нет."""
        run = self.active(scope_id)
        if run is None:
            return None

        return run.port

    def require_port(self, scope_id: str) -> RunPort:
        """Владелец с лентой чата; без него — RefusalError(RunRefusal.NO_TURN)."""
        port = self.port_of(scope_id)
        if port is None:
            msg = (
                f"runs: scope {scope_id!r} has no active run, "
                "the turn is already finished"
            )
            raise RefusalError(RunRefusal.NO_TURN, msg)

        return port

    def stop(self, scope_id: str, reason: StopReason) -> bool:
        """Останавливает запуск области из любого потока; False — нечего."""
        run = self.active(scope_id)
        if run is None:
            logger.info("stop requested for scope %s: no active run", scope_id)
            return False

        logger.info("stopping run of scope %s (%s)", scope_id, reason.value)
        run.cancellation.cancel(reason)
        return True

    def stop_all(self, reason: StopReason) -> int:
        """Останавливает все запуски процесса при его остановке; возвращает их число."""
        with self._lock:
            runs = list(self._active.values())

        for run in runs:
            run.cancellation.cancel(reason)

        return len(runs)

    def _register(self, run: Run) -> None:
        with self._lock:
            stale = self._active.get(run.scope_id)
            self._active[run.scope_id] = run

        if stale is None:
            return

        # новый запуск той же области: предыдущий дорабатывать незачем
        logger.warning(
            "runs: scope %s already had an active run, "
            "stopping the previous one as superseded",
            run.scope_id,
        )
        stale.cancellation.cancel(StopReason.SUPERSEDED)

    def _release(self, run: Run) -> None:
        with self._lock:
            if self._active.get(run.scope_id) is run:
                del self._active[run.scope_id]

    @staticmethod
    def _task_canceller() -> Callable[[], None] | None:
        """Прерыватель зовут из чужого потока — задачу трогаем только через loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        task = asyncio.current_task()
        if task is None:
            return None

        def cancel_task() -> None:
            loop.call_soon_threadsafe(task.cancel)

        return cancel_task
