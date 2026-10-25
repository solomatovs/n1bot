"""Писатель журнала: переносит несброшенные записи сессии в порт истории.

Задача в цикле событий просыпается по событию «добавлена запись» и не реже
раза в interval_sec, берёт записи с seq больше последнего сброшенного и зовёт
`append` одной пачкой (план, раздел 5.15); после каждой сохранённой пачки
приёмник получает HistoryWrittenEvent со временем её последней записи.
Неудача — запись в лог, записи остаются несброшенными, следующий тик
пробует снова; ход не останавливается.
Принудительный сброс с ожиданием — в конце хода и при завершении процесса.

RecordKeeper — единственная дверь, через которую ход и исполнитель
инструментов добавляют записи: запись уходит в сессию, писатель просыпается,
записи разговора и граница сжатия показываются наружу событием.
ClientCommands пишет команды, которые клиент исполнил сам, на границах хода.
RequestRecorder чеканит идентификатор каждого запроса к модели и, если запись
запросов включена, кладёт служебную запись `api-request` с телом запроса и
его дайджестом (план, раздел 5.18). До неё RequestReplay, если сверка
включена (тесты, отладка), пересобирает запрос из журнала, прочитанного из
хранилища заново, и сверяет дайджест.

Ошибки:
HistoryError — поднимают `flush(strict=True)` при завершении, когда журнал
    так и не удалось сбросить, и `stored()` перед запросом к модели;
    фоновая задача ошибки не выпускает.
SessionError — нарушен порядок записей сессии (ошибка ядра).
RequestReplayError — запрос к модели не совпал с пересобранным из журнала
    или из журнала не собирается; в тексте — первая расходящаяся часть.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar

from boba.agent.builder import RequestBuilder, RequestBuildError
from boba.agent.events import AgentEvent, EventSink, HistoryWrittenEvent, RecordEvent
from boba.agent.history import (
    HistoryError,
    HistoryStore,
    SessionId,
    SessionLease,
    StoredHistory,
)
from boba.agent.ids import Clock, IdMint
from boba.agent.loader import HistoryLoader
from boba.agent.records import (
    ApiRequestRecord,
    HarnessCommandRecord,
    Record,
    RecordError,
)
from boba.agent.session import Session
from boba.llm.chat import ChatRequest

logger = logging.getLogger(__name__)

__all__ = [
    "ClientCommand",
    "ClientCommands",
    "HistoryWriter",
    "RecordKeeper",
    "RequestRecorder",
    "RequestReplay",
    "RequestReplayError",
]

R = TypeVar("R", bound=Record)


class HistoryWriter:
    """Фоновый сброс записей сессии в хранилище истории."""

    INTERVAL_SEC: ClassVar[float] = 0.1

    def __init__(
        self,
        session: Session,
        store: HistoryStore,
        lease: SessionLease,
        flushed_seq: int,
        sink: EventSink,
    ) -> None:
        self._session = session
        self._store = store
        self._lease = lease
        self._flushed = flushed_seq
        self._sink = sink
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._last_error = ""

    @property
    def flushed_seq(self) -> int:
        return self._flushed

    @property
    def unflushed(self) -> int:
        return len(self._session.since(self._flushed))

    @property
    def last_error(self) -> str:
        """Текст последней неудачи сброса; пусто — сбросы проходят."""
        return self._last_error

    def start(self) -> None:
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._run(), name="history-writer")

    def notify(self) -> None:
        """Добавлена запись: писатель просыпается, не дожидаясь тика."""
        self._wake.set()

    async def flush(self, strict: bool = False) -> bool:
        """Сбрасывает всё несброшенное сейчас; False — хранилище отказало."""
        try:
            await self._append_pending()
        except HistoryError as exc:
            self._last_error = str(exc)
            logger.error(
                "history flush failed, %d record(s) kept: %s", self.unflushed, exc
            )
            if strict:
                raise

            return False

        self._last_error = ""

        return True

    async def stored(self) -> StoredHistory:
        """Разговор по записям, которые хранилище подтвердило: несброшенное
        сбрасывается сейчас, и запрос к модели собирается только из того,
        что уже лежит в журнале.

        Ошибки:
        HistoryError — хранилище отказало, запрос из истории не собрать.
        """
        while self.unflushed:
            await self.flush(strict=True)

        return StoredHistory(self._session.conversation())

    async def stop(self) -> None:
        """Последняя попытка сброса и остановка задачи."""
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

        await self.flush()

    def failed(self) -> BaseException | None:
        """Исключение, которым упала задача писателя; None — живёт."""
        if self._task is None or not self._task.done():
            return None

        if self._task.cancelled():
            return None

        return self._task.exception()

    async def _run(self) -> None:
        while not self._stopping:
            await self._sleep_until_wake()
            await self.flush()

    async def _sleep_until_wake(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.INTERVAL_SEC)
        except TimeoutError:
            return

        self._wake.clear()

    async def _append_pending(self) -> None:
        """Один сброс за раз: тик и принудительный flush не пишут одно и то же."""
        async with self._lock:
            pending = self._session.since(self._flushed)
            if not pending:
                return

            await self._store.append(self._lease, pending)
            last = pending[-1]
            self._flushed = last.seq
            self._session.durable_through(self._flushed)

        await self._sink.emit(HistoryWrittenEvent(recorded_at=last.timestamp))


class RecordKeeper:
    """Добавляет записи в сессию, будит писателя и показывает наружу каждую
    записанную запись событием RecordEvent: что из неё показать человеку,
    решает сама запись и отрисовщик приёмника. Конец хода объявляет агент
    событием ResultEvent, когда сессия свободна, поэтому его запись
    добавляется без события. Создаёт агент на сессию; им пользуются цикл
    хода и исполнитель инструментов."""

    def __init__(
        self, session: Session, writer: HistoryWriter, sink: EventSink
    ) -> None:
        self._session = session
        self._writer = writer
        self._sink = sink

    @property
    def session(self) -> Session:
        return self._session

    async def add(self, record: R) -> R:
        """Запись без seq: сессия выдаёт seq, писатель и порт узнают о ней."""
        sequenced = self._session.add(record)
        await self.added(sequenced)

        return sequenced

    async def added(self, record: Record) -> None:
        """Запись, уже добавленная в сессию кем-то другим."""
        self._writer.notify()
        await self._sink.emit(RecordEvent(record=record))

    def add_unannounced(self, record: R) -> R:
        """Запись без события: о ней объявит своё событие (конец хода —
        ResultEvent)."""
        sequenced = self._session.add(record)
        self._writer.notify()

        return sequenced

    async def emit(self, event: AgentEvent) -> None:
        await self._sink.emit(event)


@dataclass(frozen=True)
class ClientCommand:
    """Команда, которую клиент исполнил сам: uuid её записи (его даёт
    клиент), имя, набранный текст и итог в форме клиента."""

    message_id: str
    name: str
    text: str
    outcome: Mapping[str, Any]


class ClientCommands:
    """Записи команд клиента (HarnessCommandRecord) в историю сессии на
    границах хода: вне хода команда пишется сразу, посреди хода ждёт
    ближайшей границы шага, а не дождавшаяся её пишется перед концом хода.
    Так запись не встаёт между вызовом инструмента и его итогом, и лента
    видит команду между шагами — вживую и в истории одинаково. Создаёт
    агент на сессию, команду ему отдаёт вход (Agent.record_command), границы
    отмечает цикл хода (TurnLoop)."""

    def __init__(self, keeper: RecordKeeper, clock: Clock, version: str) -> None:
        self._keeper = keeper
        self._clock = clock
        self._version = version
        self._held: list[ClientCommand] = []
        self._in_turn = False

    async def add(self, command: ClientCommand) -> None:
        if self._in_turn:
            self._held.append(command)
            return

        await self._write(command)

    def opened(self) -> None:
        """Ход начался: команды ждут его границ."""
        self._in_turn = True

    async def boundary(self) -> None:
        """Граница шага: ждущие команды пишутся по порядку прихода."""
        while self._held:
            await self._write(self._held.pop(0))

    async def closed(self) -> None:
        """Ход кончается: ждущие команды пишутся до его конца, следующие —
        сразу."""
        self._in_turn = False
        await self.boundary()

    async def _write(self, command: ClientCommand) -> None:
        session = self._keeper.session
        await self._keeper.add(
            HarnessCommandRecord(
                uuid=command.message_id,
                parent_uuid=session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=session.id.value,
                version=self._version,
                name=command.name,
                text=command.text,
                outcome=dict(command.outcome),
            )
        )


class RequestReplayError(Exception):
    """Запрос к модели расходится с пересобранным из журнала."""


class RequestReplay:
    """Сверка запроса к модели с журналом: перед отправкой запрос
    пересобирается сборщиком (RequestBuilder) из истории, прочитанной из
    хранилища заново (HistoryLoader.stored), — так, как его соберёт
    возобновление, — и тела сравниваются. Расхождение — ошибка с первой
    разошедшейся частью тела. Включают тесты и отладка; выключенная сверка
    ничего не читает. Держит RequestRecorder."""

    ABSENT: ClassVar[str] = "<absent>"
    SHOWN: ClassVar[int] = 200
    """Сколько знаков значения показывает текст ошибки."""

    def __init__(self, loader: HistoryLoader, enabled: bool) -> None:
        self._loader = loader
        self._enabled = enabled
        self._builder = RequestBuilder()

    async def check(self, session: SessionId, request: ChatRequest) -> None:
        """Ошибки:
        RequestReplayError — запрос не совпал с пересобранным или из журнала
            не собирается.
        HistoryError — хранилище отказало при чтении журнала.
        """
        if not self._enabled:
            return

        history = await self._loader.stored(session)
        try:
            rebuilt = self._builder.build(history)
        except RequestBuildError as exc:
            msg = (
                f"The model request of session {session.value} cannot be rebuilt "
                f"from its stored journal: {exc}"
            )
            raise RequestReplayError(msg) from exc
        except RecordError as exc:
            msg = (
                f"The model request of session {session.value} cannot be rebuilt: "
                f"the stored journal is damaged: {exc}"
            )
            raise RequestReplayError(msg) from exc

        sent = request.model_dump(mode="json", exclude_none=True)
        again = rebuilt.model_dump(mode="json", exclude_none=True)
        if sent == again:
            return

        part = self._first_difference(sent, again, "request")
        msg = (
            f"The model request of session {session.value} differs from the one "
            f"rebuilt from its stored journal at {part.path}: sent {part.sent!r}, "
            f"rebuilt {part.rebuilt!r}. Something the request depends on is not "
            "in the history."
        )
        raise RequestReplayError(msg)

    def _first_difference(self, sent: Any, rebuilt: Any, path: str) -> Difference:
        """Первая расходящаяся часть тел: путь ключей и индексов и оба значения."""
        if isinstance(sent, dict) and isinstance(rebuilt, dict):
            return self._mapping_difference(sent, rebuilt, path)

        if isinstance(sent, list) and isinstance(rebuilt, list):
            return self._list_difference(sent, rebuilt, path)

        return Difference(
            path=path, sent=self._brief(sent), rebuilt=self._brief(rebuilt)
        )

    def _mapping_difference(
        self, sent: dict[str, Any], rebuilt: dict[str, Any], path: str
    ) -> Difference:
        keys = sorted(set(sent) | set(rebuilt))
        for key in keys:
            left = sent.get(key, self.ABSENT)
            right = rebuilt.get(key, self.ABSENT)
            if left == right:
                continue

            return self._first_difference(left, right, f"{path}.{key}")

        return Difference(path=path, sent="", rebuilt="")

    def _list_difference(
        self, sent: list[Any], rebuilt: list[Any], path: str
    ) -> Difference:
        for index, (left, right) in enumerate(zip(sent, rebuilt, strict=False)):
            if left == right:
                continue

            return self._first_difference(left, right, f"{path}[{index}]")

        return Difference(
            path=f"{path} length", sent=str(len(sent)), rebuilt=str(len(rebuilt))
        )

    def _brief(self, value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False)

        return text[: self.SHOWN]


@dataclass(frozen=True)
class Difference:
    """Первая расходящаяся часть тела запроса: путь и оба значения коротко."""

    path: str
    sent: str
    rebuilt: str


class RequestRecorder:
    """Идентификатор запроса к модели, сверка его с журналом (RequestReplay)
    и запись в журнал по флагу."""

    def __init__(
        self,
        keeper: RecordKeeper,
        ids: IdMint,
        clock: Clock,
        enabled: bool,
        replay: RequestReplay,
    ) -> None:
        self._keeper = keeper
        self._ids = ids
        self._clock = clock
        self._enabled = enabled
        self._replay = replay

    async def record(self, request: ChatRequest) -> str:
        """Возвращает идентификатор запроса; сверка с журналом и запись —
        только когда включены.

        Ошибки:
        RequestReplayError — запрос не совпал с пересобранным из журнала.
        """
        request_id = self._ids.request_id()
        session = self._keeper.session
        await self._replay.check(session.id, request)
        if not self._enabled:
            return request_id

        params = request.model_dump(mode="json", exclude_none=True)
        canonical = json.dumps(params, ensure_ascii=False, sort_keys=True)
        await self._keeper.add(
            ApiRequestRecord(
                timestamp=self._clock.now(),
                session_id=session.id.value,
                id=self._ids.uuid(),
                request_id=request_id,
                params=params,
                digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            )
        )

        return request_id
