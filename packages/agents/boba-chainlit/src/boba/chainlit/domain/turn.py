"""Контекст идущего хода: единственный реестр ходов по thread_id.

Контекст открывает оркестрация хода; всё, что живёт ровно один ход, лежит
здесь: отмена, ссылка на ход, живые журналы вызовов.
Инструменты и отрисовка находят ход только через этот реестр, закрытие
контекста — единственный finally, гасящий всё сразу.

Остановка хода адресуется thread_id и работает из любого потока: синхронный
код прерывают зарегистрированные прерыватели, асинхронный — отмена задачи
хода, зарегистрированная как прерыватель при открытии контекста.

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

from boba.cancellation import StopReason, TurnCancellation, turn_cancellation
from boba.toolkit.channels import CallOutcome

__all__ = ["LiveStream", "TurnContext", "TurnPort"]

logger = logging.getLogger(__name__)


class TurnPort(Protocol):
    """Что инструменту нужно от хода: куда крепить созданный им элемент."""

    @property
    def answer_step_id(self) -> str | None: ...


class LiveStream(Protocol):
    """Живой журнал вызова: контекст закрывает его и защищает его файлы.

    call_prefix — префикс файлов вызова в томе журнала: контекст отдаёт его
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


class TurnContext:
    """Всё состояние одного идущего хода под одним ключом thread_id."""

    _LOCK: ClassVar[threading.Lock] = threading.Lock()
    _ACTIVE: ClassVar[dict[str, TurnContext]] = {}

    def __init__(
        self,
        thread_id: str,
        turn: TurnPort,
        cancellation: TurnCancellation,
    ) -> None:
        self._thread_id = thread_id
        self._turn = turn
        self._cancellation = cancellation
        self._streams: dict[str, LiveStream] = {}

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def turn(self) -> TurnPort:
        """Ход, открывший контекст."""
        return self._turn

    @property
    def cancellation(self) -> TurnCancellation:
        """Отмена хода; та же, что опубликована в contextvar исполнения."""
        return self._cancellation

    @classmethod
    @contextmanager
    def open(cls, thread_id: str, turn: TurnPort) -> Generator[TurnContext, None, None]:
        """Открывает ход треда: отмена в контексте исполнения, ход в реестре.

        Закрытие контекста снимает насос и закрывает живые журналы вызовов —
        файлы журнала переживают ход, живые объекты нет.
        """
        with turn_cancellation() as cancellation:
            context = cls(thread_id, turn, cancellation)
            cls._register(context)
            try:
                with cls._task_abort(cancellation):
                    yield context
            finally:
                cls._release(context)
                context._close_live()

    @classmethod
    def active(cls, thread_id: str) -> TurnContext | None:
        """Контекст идущего хода; None — тред ничем не занят."""
        with cls._LOCK:
            return cls._ACTIVE.get(thread_id)

    @classmethod
    def turn_of(cls, thread_id: str) -> TurnPort | None:
        """Ход треда для инструментов; None — тред ничем не занят."""
        context = cls.active(thread_id)
        if context is None:
            return None

        return context.turn

    @classmethod
    def stop(cls, thread_id: str, reason: StopReason) -> bool:
        """Останавливает ход треда из любого потока; False — нечего."""
        context = cls.active(thread_id)
        if context is None:
            logger.info("stop requested for thread %s: no active turn", thread_id)
            return False

        logger.info("stopping turn of thread %s (%s)", thread_id, reason.value)
        context.cancellation.cancel(reason)
        return True

    def add_stream(self, call_id: str, stream: LiveStream) -> None:
        """Регистрирует живой журнал вызова; жизнь журнала кончится с ходом."""
        with self._LOCK:
            self._streams[call_id] = stream

    def stream(self, call_id: str) -> LiveStream | None:
        """Живой журнал вызова; None — вызов не журналируется или закончился."""
        with self._LOCK:
            return self._streams.get(call_id)

    @classmethod
    def live_threads(cls) -> frozenset[str]:
        """Треды с живыми журналами: их нельзя удалять инструментом уборки."""
        with cls._LOCK:
            live: list[str] = []
            for context in cls._ACTIVE.values():
                if context._streams:
                    live.append(context._thread_id)

            return frozenset(live)

    @classmethod
    def live_prefixes(cls) -> frozenset[str]:
        """Префиксы файлов живых вызовов: вытеснять их из тома нельзя."""
        with cls._LOCK:
            return frozenset(cls._live_call_prefixes())

    @classmethod
    def _live_call_prefixes(cls) -> Iterator[str]:
        for context in cls._ACTIVE.values():
            for stream in context._streams.values():
                yield stream.call_prefix

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        with cls._LOCK:
            cls._ACTIVE.clear()

    @classmethod
    def _register(cls, context: TurnContext) -> None:
        with cls._LOCK:
            stale = cls._ACTIVE.get(context._thread_id)
            cls._ACTIVE[context._thread_id] = context

        if stale is None:
            return

        # новый ход того же треда: предыдущий дорабатывать незачем
        logger.warning(
            "thread %s already had an active turn; stopping it", context._thread_id
        )
        stale.cancellation.cancel(StopReason.SUPERSEDED)

    @classmethod
    def _release(cls, context: TurnContext) -> None:
        with cls._LOCK:
            if cls._ACTIVE.get(context._thread_id) is context:
                del cls._ACTIVE[context._thread_id]

    def _close_live(self) -> None:
        """Конец хода: живые журналы закрываются, файлы журнала остаются."""
        with self._LOCK:
            streams = list(self._streams.values())
            self._streams.clear()

        for stream in streams:
            if not stream.closed:
                stream.close(CallOutcome.STOPPED.value)

    @classmethod
    @contextmanager
    def _task_abort(cls, cancellation: TurnCancellation) -> Generator[None, None, None]:
        """Отмена задачи хода как прерыватель: асинхронный мир тоже обрывается."""
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
