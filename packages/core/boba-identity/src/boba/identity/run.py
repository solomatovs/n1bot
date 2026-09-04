"""Реестр идущих запусков: один запуск на область (scope.id).

Запуск открывает его владелец — ход чата, REST-вызов, раннер workflow —
контекстом вызова; всё, что живёт ровно один запуск, лежит здесь: контекст,
отмена, порт владельца (у headless-запусков его нет), живые журналы
вызовов. Инструменты и отрисовка находят запуск только через реестр,
закрытие записи — единственный finally, гасящий всё сразу.

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
from collections.abc import Callable, Coroutine, Generator, Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation, StopReason
from boba.identity.context import CallContext
from boba.identity.errors import FailureText, RefusalError
from boba.toolkit.channels import CallOutcome

__all__ = [
    "BackgroundRuns",
    "ElementTarget",
    "LiveStream",
    "RunPort",
    "RunRefusal",
    "RunRegistry",
    "StreamObserver",
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


class LiveStream(Protocol):
    """Живой журнал вызова: запуск закрывает его и защищает его файлы.

    call_prefix — префикс файлов вызова в томе журнала: реестр отдаёт его
    ротации как защищённый, не зная формата имён журнала.
    """

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @property
    @abstractmethod
    def call_prefix(self) -> str: ...

    @abstractmethod
    def close(self, note: str) -> None: ...


StreamObserver = Callable[[str, LiveStream], None]
"""Узнаёт об открытом журнале вызова (call_id, stream); зовётся в loop'е запуска."""


class RunRegistry:
    """Всё состояние одного идущего запуска под одним ключом scope_id."""

    _LOCK: ClassVar[threading.Lock] = threading.Lock()
    _ACTIVE: ClassVar[dict[str, RunRegistry]] = {}

    def __init__(
        self,
        context: CallContext,
        port: RunPort | None,
        on_stream: StreamObserver | None = None,
    ) -> None:
        self._context = context
        self._port = port
        self._streams: dict[str, LiveStream] = {}
        self._notify: tuple[asyncio.AbstractEventLoop, StreamObserver] | None = None
        if on_stream is not None:
            self._notify = (asyncio.get_running_loop(), on_stream)

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

    @classmethod
    @contextmanager
    def open(
        cls,
        context: CallContext,
        port: RunPort | None = None,
        on_stream: StreamObserver | None = None,
    ) -> Generator[RunRegistry, None, None]:
        """Открывает запуск области: контекст и отмена в исполнении, запись в реестре.

        Закрытие снимает запись и закрывает живые журналы вызовов — файлы
        журнала переживают запуск, живые объекты нет. on_stream узнаёт о
        каждом открытом журнале из loop'а, в котором открыт запуск.
        """
        with context.applied(), context.cancellation.published():
            registry = cls(context, port, on_stream)
            cls._register(registry)
            try:
                yield registry
            finally:
                cls._release(registry)
                registry._close_live()

    @classmethod
    @contextmanager
    def task_abort(cls, cancellation: RunCancellation) -> Generator[None, None, None]:
        """Отмена корутины запуска как прерыватель: выбор владельца, не реестра."""
        abort = cls._task_canceller()
        if abort is None:
            yield
            return

        with cancellation.abort_with(abort):
            yield

    @classmethod
    def active(cls, scope_id: str) -> RunRegistry | None:
        """Запись идущего запуска; None — область ничем не занята."""
        with cls._LOCK:
            return cls._ACTIVE.get(scope_id)

    @classmethod
    def port_of(cls, scope_id: str) -> RunPort | None:
        """Владелец запуска области для инструментов; None — запуска нет."""
        registry = cls.active(scope_id)
        if registry is None:
            return None

        return registry.port

    @classmethod
    def require_port(cls, scope_id: str) -> RunPort:
        """Владелец с лентой чата; без него — RefusalError(RunRefusal.NO_TURN)."""
        port = cls.port_of(scope_id)
        if port is None:
            msg = (
                f"run registry: scope {scope_id!r} has no active run, "
                "the turn is already finished"
            )
            raise RefusalError(RunRefusal.NO_TURN, msg)

        return port

    @classmethod
    def stop(cls, scope_id: str, reason: StopReason) -> bool:
        """Останавливает запуск области из любого потока; False — нечего."""
        registry = cls.active(scope_id)
        if registry is None:
            logger.info("stop requested for scope %s: no active run", scope_id)
            return False

        logger.info("stopping run of scope %s (%s)", scope_id, reason.value)
        registry.cancellation.cancel(reason)
        return True

    @classmethod
    def stop_all(cls, reason: StopReason) -> int:
        """Останавливает все запуски процесса при его остановке; возвращает их число."""
        with cls._LOCK:
            registries = list(cls._ACTIVE.values())

        for registry in registries:
            registry.cancellation.cancel(reason)

        return len(registries)

    def add_stream(self, call_id: str, stream: LiveStream) -> None:
        """Регистрирует живой журнал вызова; жизнь журнала кончится с запуском."""
        with self._LOCK:
            self._streams[call_id] = stream

        notify = self._notify
        if notify is None:
            return

        loop, observer = notify
        try:
            loop.call_soon_threadsafe(observer, call_id, stream)
        except RuntimeError as exc:
            logger.warning(
                "run registry of scope %s: stream %s opened after the run loop "
                "closed, observer not notified: %s",
                self.scope_id,
                call_id,
                exc,
            )

    def stream(self, call_id: str) -> LiveStream | None:
        """Живой журнал вызова; None — вызов не журналируется или закончился."""
        with self._LOCK:
            return self._streams.get(call_id)

    @classmethod
    def live_scopes(cls) -> frozenset[str]:
        """Области с живыми журналами: их нельзя удалять инструментом уборки."""
        with cls._LOCK:
            live: list[str] = []
            for registry in cls._ACTIVE.values():
                if registry._streams:
                    live.append(registry.scope_id)

            return frozenset(live)

    @classmethod
    def live_prefixes(cls) -> frozenset[str]:
        """Префиксы файлов живых вызовов: вытеснять их из тома нельзя."""
        with cls._LOCK:
            return frozenset(cls._live_call_prefixes())

    @classmethod
    def _live_call_prefixes(cls) -> Iterator[str]:
        for registry in cls._ACTIVE.values():
            for stream in registry._streams.values():
                yield stream.call_prefix

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        with cls._LOCK:
            cls._ACTIVE.clear()

    @classmethod
    def _register(cls, registry: RunRegistry) -> None:
        with cls._LOCK:
            stale = cls._ACTIVE.get(registry.scope_id)
            cls._ACTIVE[registry.scope_id] = registry

        if stale is None:
            return

        # новый запуск той же области: предыдущий дорабатывать незачем
        logger.warning(
            "run registry: scope %s already had an active run, "
            "stopping the previous one as superseded",
            registry.scope_id,
        )
        stale.cancellation.cancel(StopReason.SUPERSEDED)

    @classmethod
    def _release(cls, registry: RunRegistry) -> None:
        with cls._LOCK:
            if cls._ACTIVE.get(registry.scope_id) is registry:
                del cls._ACTIVE[registry.scope_id]

    def _close_live(self) -> None:
        """Конец запуска: живые журналы закрываются, файлы журнала остаются."""
        with self._LOCK:
            streams = list(self._streams.values())
            self._streams.clear()

        for stream in streams:
            if not stream.closed:
                stream.close(CallOutcome.STOPPED.value)

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


class BackgroundRuns:
    """Запуски в фоне процесса: держит задачи, чтобы их не забрал сборщик, и
    журналирует сбой — молча фоновые запуски не умирают."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()

    def launch(self, name: str, work: Coroutine[object, object, object]) -> None:
        task = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._settle)

    @property
    def live(self) -> int:
        return len(self._tasks)

    def _settle(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            logger.warning(
                "background run %s was cancelled before finishing", task.get_name()
            )
            return

        error = task.exception()
        if error is not None:
            logger.error(
                "background run %s crashed: %s",
                task.get_name(),
                FailureText.of(error),
                exc_info=error,
            )
