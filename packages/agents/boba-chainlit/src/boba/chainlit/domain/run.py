"""Реестр идущих запусков: один запуск на область (scope.id).

Запуск открывает его владелец — ход чата, раннер workflow; всё, что живёт
ровно один запуск, лежит здесь: отмена, порт владельца, живые журналы
вызовов. Инструменты и отрисовка находят запуск только через реестр,
закрытие записи — единственный finally, гасящий всё сразу.

Остановка адресуется scope.id и работает из любого потока: синхронный код
прерывают зарегистрированные прерыватели, асинхронный — отмена задачи
запуска, зарегистрированная как прерыватель при открытии.

Ошибки: своих не выпускает; ToolStopped поднимает raise_if_cancelled отмены.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import abstractmethod
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import ClassVar, Protocol

from boba.cancellation import RunCancellation, StopReason
from boba.toolkit.channels import CallOutcome

__all__ = ["LiveStream", "RunPort", "RunRegistry"]

logger = logging.getLogger(__name__)


class RunPort(Protocol):
    """Что инструменту нужно от владельца запуска: куда крепить его элемент."""

    answer_step_id: str | None


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


class RunRegistry:
    """Всё состояние одного идущего запуска под одним ключом scope_id."""

    _LOCK: ClassVar[threading.Lock] = threading.Lock()
    _ACTIVE: ClassVar[dict[str, RunRegistry]] = {}

    def __init__(
        self,
        scope_id: str,
        port: RunPort,
        cancellation: RunCancellation,
    ) -> None:
        self._scope_id = scope_id
        self._port = port
        self._cancellation = cancellation
        self._streams: dict[str, LiveStream] = {}

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def port(self) -> RunPort:
        """Владелец, открывший запуск."""
        return self._port

    @property
    def cancellation(self) -> RunCancellation:
        """Отмена запуска; та же, что опубликована в contextvar исполнения."""
        return self._cancellation

    @classmethod
    @contextmanager
    def open(
        cls, scope_id: str, port: RunPort, cancellation: RunCancellation
    ) -> Generator[RunRegistry, None, None]:
        """Открывает запуск области: отмена в контексте исполнения, запись в реестре.

        Закрытие снимает запись и закрывает живые журналы вызовов — файлы
        журнала переживают запуск, живые объекты нет.
        """
        with cancellation.published():
            registry = cls(scope_id, port, cancellation)
            cls._register(registry)
            try:
                with cls._task_abort(cancellation):
                    yield registry
            finally:
                cls._release(registry)
                registry._close_live()

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
    def stop(cls, scope_id: str, reason: StopReason) -> bool:
        """Останавливает запуск области из любого потока; False — нечего."""
        registry = cls.active(scope_id)
        if registry is None:
            logger.info("stop requested for scope %s: no active run", scope_id)
            return False

        logger.info("stopping run of scope %s (%s)", scope_id, reason.value)
        registry.cancellation.cancel(reason)
        return True

    def add_stream(self, call_id: str, stream: LiveStream) -> None:
        """Регистрирует живой журнал вызова; жизнь журнала кончится с запуском."""
        with self._LOCK:
            self._streams[call_id] = stream

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
                    live.append(registry._scope_id)

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
            stale = cls._ACTIVE.get(registry._scope_id)
            cls._ACTIVE[registry._scope_id] = registry

        if stale is None:
            return

        # новый запуск той же области: предыдущий дорабатывать незачем
        logger.warning(
            "scope %s already had an active run; stopping it", registry._scope_id
        )
        stale.cancellation.cancel(StopReason.SUPERSEDED)

    @classmethod
    def _release(cls, registry: RunRegistry) -> None:
        with cls._LOCK:
            if cls._ACTIVE.get(registry._scope_id) is registry:
                del cls._ACTIVE[registry._scope_id]

    def _close_live(self) -> None:
        """Конец запуска: живые журналы закрываются, файлы журнала остаются."""
        with self._LOCK:
            streams = list(self._streams.values())
            self._streams.clear()

        for stream in streams:
            if not stream.closed:
                stream.close(CallOutcome.STOPPED.value)

    @classmethod
    @contextmanager
    def _task_abort(cls, cancellation: RunCancellation) -> Generator[None, None, None]:
        """Отмена задачи запуска как прерыватель: асинхронный мир тоже обрывается."""
        abort = cls._task_canceller()
        if abort is None:
            yield
            return

        with cancellation.abort_with(abort):
            yield

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
