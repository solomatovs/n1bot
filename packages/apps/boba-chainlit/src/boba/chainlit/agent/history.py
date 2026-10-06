"""История тредов в checkpoint'ах langgraph и агент сессии поверх графа.

CheckpointMessages читает сообщения треда прямо из хранилища checkpoint'ов —
без графа, на весь процесс: так собирают ленту слой данных и реестр журналов
вызовов. GraphAgent — граф сессии глазами чата: ход, чтение состояния треда,
запись оборванного хода и откат к вопросу. Наружу оба отдают сообщения
диалога чата (boba.chainlit.chat.dialog); сообщения langchain дальше этого
модуля не уходят.

Ошибки:
pydantic.ValidationError — сообщение истории несёт результат известного
    вида, вызов или адрес журнала, поля которых не проходят модель.
Ошибки графа и хранилища checkpoint'ов уходят наверх как есть: ход (ChatTurn)
    отчитывается ими в ленту и историю.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from enum import StrEnum

from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, PendingWrite
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit.agent.bridge import LangchainMessages
from boba.chainlit.agent.events import TurnEvents
from boba.chainlit.chat.dialog import (
    AnswerChunk,
    DialogMessage,
    ModelListener,
    PrefetchStage,
    SessionAgent,
    ThreadMessages,
    TurnRecord,
    UserMessage,
)
from boba.db.postgres import AsyncPostgresPool

__all__ = [
    "CheckpointMessages",
    "GraphAgent",
    "GraphChannel",
    "PostgresCheckpoints",
]


class GraphChannel(StrEnum):
    """Каналы состояния графа хода, которые читает и пишет история."""

    MESSAGES = "messages"


class CheckpointMessages(ThreadMessages):
    """Реализация ThreadMessages чтением langgraph-checkpointer'а: канал
    последнего checkpoint'а плюс ответы задач, доработавших раньше соседей
    по пачке.

    Создаётся сборкой чата один на процесс; его зовут сборка ленты из
    истории (TranscriptFeed) и реестр журналов вызовов (RemoteJournals), а
    билдер графа берёт у него хранилище (saver).
    """

    def __init__(self, saver: BaseCheckpointSaver) -> None:
        self._saver = saver
        self._messages = LangchainMessages()

    @property
    def saver(self) -> BaseCheckpointSaver:
        """Хранилище checkpoint'ов, на котором собирается граф хода."""
        return self._saver

    async def load(self, thread_id: str) -> Sequence[DialogMessage]:
        config = RunnableConfig(configurable={"thread_id": thread_id})
        snapshot = await self._saver.aget_tuple(config)
        if snapshot is None:
            return []

        values = snapshot.checkpoint.get("channel_values")
        if not values:
            values = {}

        messages = list(self._written(values.get(GraphChannel.MESSAGES.value)))

        pending = snapshot.pending_writes
        if pending is not None:
            messages.extend(self._pending(pending, messages))

        return list(self._messages.dialog(messages))

    def _pending(
        self, writes: Iterable[PendingWrite], known: Sequence[BaseMessage]
    ) -> Iterator[BaseMessage]:
        """Сообщения из pending writes последнего checkpoint'а: вызовы одной пачки
        идут отдельными задачами, и ответ завершившейся задачи до конца узла лежит
        только здесь.
        """
        seen: set[str] = set()
        for message in known:
            if message.id:
                seen.add(message.id)

        for _task_id, channel, value in writes:
            if channel != GraphChannel.MESSAGES.value:
                continue

            for message in self._written(value):
                if message.id and message.id in seen:
                    continue

                yield message

    @staticmethod
    def _written(value: object) -> Iterator[BaseMessage]:
        """Сообщения одной записи: узел отдаёт список или одно сообщение;
        пустой канал сообщений не несёт."""
        items: Sequence[object] = [value]
        if isinstance(value, list):
            items = value

        for item in items:
            if not isinstance(item, BaseMessage):
                continue

            yield item


class PostgresCheckpoints(CheckpointMessages):
    """История тредов в postgres: савер langgraph на пуле со своим
    search_path — схему в имена таблиц он не ставит.

    Создаётся сборкой чата на старте процесса; пул закрывает остановка
    приложения: он общий для всех, кто просит его с той же схемой.
    """

    def __init__(self, pool: AsyncPostgresPool) -> None:
        self._postgres = AsyncPostgresSaver(pool.raw)
        super().__init__(self._postgres)

    async def setup(self) -> None:
        """Создаёт и мигрирует таблицы checkpoint'ов."""
        await self._postgres.setup()


class GraphAgent(SessionAgent):
    """Реализация SessionAgent графом хода сессии.

    Создаётся сборкой чата на сессию поверх графа, собранного билдером по
    flow профиля. Обработчик сообщения запускает на нём ход: вопрос уходит в
    граф, события прогонов модели — слушателям хода через TurnEvents. Запись
    исхода хода и правка вопроса читают и переписывают состояние треда
    через него же; перевод сообщений делает LangchainMessages.
    """

    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph
        self._messages = LangchainMessages()

    async def answer(
        self,
        thread_id: str,
        question: UserMessage,
        listeners: Sequence[ModelListener],
        stage: PrefetchStage,
    ) -> AsyncIterator[AnswerChunk]:
        config = RunnableConfig(
            callbacks=[TurnEvents(listeners, stage)],
            configurable={"thread_id": thread_id},
        )
        stream = self._graph.astream(
            {GraphChannel.MESSAGES.value: [self._messages.human(question)]},
            stream_mode="messages",
            config=config,
        )

        async for item in stream:
            yield AnswerChunk(text=self._answer_text(item))

    async def load(self, thread_id: str) -> Sequence[DialogMessage]:
        return list(self._messages.dialog(await self._stored(thread_id)))

    async def remember(self, thread_id: str, record: TurnRecord) -> None:
        await self._graph.aupdate_state(
            self._config(thread_id),
            {GraphChannel.MESSAGES.value: [self._messages.record(record)]},
        )

    async def rewind(self, thread_id: str, question: UserMessage) -> None:
        """Канал переписывается целиком: точечный RemoveMessage падает, когда
        прерванный ход оставил pending writes — aget_state их показывает,
        но в канале чекпойнта их нет.
        """
        stored = await self._stored(thread_id)

        updates: list[BaseMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
        updates.extend(self.prefix(stored, question.id))
        updates.append(self._messages.human(question))

        await self._graph.aupdate_state(
            self._config(thread_id), {GraphChannel.MESSAGES.value: updates}
        )

    @staticmethod
    def prefix(
        messages: Sequence[BaseMessage], message_id: str | None
    ) -> list[BaseMessage]:
        """История до правленого вопроса; вопроса и хвоста в ней нет."""
        kept: list[BaseMessage] = []
        for message in messages:
            asked = isinstance(message, HumanMessage)
            same = message.id == message_id
            if asked and same:
                return kept

            kept.append(message)

        return []

    async def _stored(self, thread_id: str) -> list[BaseMessage]:
        """Сообщения треда, какими их видит граф."""
        state = await self._graph.aget_state(self._config(thread_id))

        return list(state.values.get(GraphChannel.MESSAGES.value, []))

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return RunnableConfig(configurable={"thread_id": thread_id})

    @staticmethod
    def _answer_text(item: object) -> str:
        """Токен ответа из порции потока графа: поток отдаёт пары «сообщение,
        метаданные», и текст ответа несут только чанки модели.
        """
        if not isinstance(item, tuple):
            return ""

        chunk = item[0]
        if not isinstance(chunk, AIMessageChunk):
            return ""

        if not isinstance(chunk.content, str):
            return ""

        return chunk.content
