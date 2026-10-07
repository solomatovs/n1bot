"""Диалог чата своими типами: сообщения треда, события обращения к модели и
порты, за которыми стоит агент сессии.

Чат хранит историю в checkpoint'ах графа и говорит с моделью через граф;
типы графа знает только каталог agent. Здесь объявлено всё, чем остальной
чат с ним обменивается: сообщения диалога (UserMessage, AssistantMessage,
ToolReply), запись оборванного хода (TurnRecord), события прогона модели
(ModelCallStarted, ModelToken, ModelCallFinished, AnswerChunk) и протоколы,
которые agent реализует (ThreadMessages, SessionAgent) либо зовёт
(ModelListener, PrefetchStage).

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from boba.toolkit.dag import DagNode, JournalAddress, NodeOutcome, WorkflowResult

__all__ = [
    "AnswerChunk",
    "AssistantMessage",
    "Attachment",
    "DialogMessage",
    "LlmUsage",
    "ModelCallFinished",
    "ModelCallStarted",
    "ModelListener",
    "ModelToken",
    "PrefetchStage",
    "SessionAgent",
    "ThreadMessages",
    "ToolReply",
    "TurnHistory",
    "TurnMark",
    "TurnRecord",
    "UserMessage",
]


class TurnMark(StrEnum):
    """Пометка исхода хода в сообщении истории; по ней сборка ленты из
    истории рисует остановленный или упавший ход.
    """

    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True)
class Attachment:
    """Вложение вопроса: имя файла и путь, каким его видит песочница."""

    name: str
    path: str


@dataclass(frozen=True)
class LlmUsage:
    """Расход токенов одного ответа модели по данным провайдера."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def counted(self) -> bool:
        """Провайдер прислал учёт: нулевой расход показывать нечего."""
        if self.input_tokens:
            return True

        return bool(self.output_tokens)


@dataclass(frozen=True)
class UserMessage:
    """Вопрос пользователя в диалоге: текст и вложения.

    Создаётся ходом (ChatTurn) из сообщения chainlit и переводом истории
    (agent) из checkpoint'а; id — идентификатор шага вопроса в ленте.
    """

    id: str | None
    text: str
    attachments: Sequence[Attachment] = ()


@dataclass(frozen=True)
class AssistantMessage:
    """Ответ ассистента в диалоге: текст, рассуждение, вызовы инструментов
    и расход токенов.

    Создаётся переводом истории и событий модели (agent). mark — ход оборван:
    запись оставил отчёт об исходе хода, а не модель. prefetch_elapsed_ms —
    длительность подготовки хода, если сообщение несёт её вызовы; 0 — не несёт.
    Вызовы без идентификатора или имени в calls не попадают: шаг ленты им
    не адресовать.
    """

    id: str | None
    text: str
    reasoning: str = ""
    calls: Sequence[DagNode] = ()
    usage: LlmUsage = LlmUsage()
    mark: TurnMark | None = None
    prefetch_elapsed_ms: int = 0


@dataclass(frozen=True)
class ToolReply:
    """Ответ инструмента в диалоге: итог вызова и адрес его журнала.

    Создаётся переводом истории (agent). outcome — итог вызова с результатом
    семейства; None — запись старой истории без результата семейства, её
    показывают как есть значением raw: сырой результат записи, а без него —
    её текст. journal от результата не зависит: адрес несёт и запись без
    результата семейства; None — вызов исполнял не сервер с журналом.
    """

    id: str | None
    call_id: str
    name: str
    text: str
    outcome: NodeOutcome | None
    raw: object
    journal: JournalAddress | None

    def journals(self) -> Mapping[str, JournalAddress]:
        """Адреса журналов по идентификаторам вызовов, какими их знает
        лента: самого вызова, а у связки — и каждого её узла (запуск у них
        общий, журнал сервер ведёт под идентификатором вызова узла)."""
        address = self.journal
        if address is None:
            return {}

        found = {self.call_id: address}
        outcome = self.outcome
        if outcome is None:
            return found

        artifact = outcome.artifact
        if not isinstance(artifact, WorkflowResult):
            return found

        for node in artifact.nodes:
            found[node.call_id] = address.model_copy(update={"call": node.call_id})

        return found


DialogMessage = UserMessage | AssistantMessage | ToolReply


@dataclass(frozen=True)
class TurnRecord:
    """Запись оборванного хода для истории агента: текст ответа, пометка исхода и
    незавершённые рассуждения, чтобы модель знала, чем ход кончился.
    """

    content: str
    mark: TurnMark
    reasoning: str = ""


@dataclass(frozen=True)
class AnswerChunk:
    """Порция потока хода на графе. text — токен ответа модели; пустой —
    граф сообщил о чём-то другом: рассуждение, вызов или ответ инструмента.
    """

    text: str


@dataclass(frozen=True)
class ModelCallStarted:
    """Начало обращения к модели: запрос ушёл провайдеру."""

    run: str
    model: str
    messages: int
    tools: int


@dataclass(frozen=True)
class ModelToken:
    """Токен потока модели: текст ответа либо рассуждение. message_id —
    идентификатор сообщения, которое модель собирает; None — провайдер его
    не назвал.
    """

    run: str
    message_id: str | None
    text: str
    reasoning: str


@dataclass(frozen=True)
class ModelCallFinished:
    """Конец обращения к модели; reply — её итоговое сообщение, None —
    провайдер ответил без сообщения ассистента.
    """

    run: str
    reply: AssistantMessage | None


class ModelListener(Protocol):
    """Слушатель обращений к модели одного хода.

    Реализуют трасер ленты (AgentTracer) и журнал состояний (LlmStateLog);
    зовёт их переводчик колбэков графа (agent) на каждое событие прогона.
    """

    @abstractmethod
    async def model_called(self, call: ModelCallStarted) -> None: ...

    @abstractmethod
    async def model_token(self, token: ModelToken) -> None: ...

    @abstractmethod
    async def model_replied(self, finish: ModelCallFinished) -> None: ...

    @abstractmethod
    async def model_failed(self, run: str, error: BaseException) -> None: ...


class PrefetchStage(Protocol):
    """Порт показа этапа подготовки хода: реализует трасер ленты
    (AgentTracer), зовёт подготовка хода на графе (agent).
    """

    @abstractmethod
    async def begin(self) -> None: ...

    @abstractmethod
    async def searching(self, queries: Sequence[str]) -> None: ...

    @abstractmethod
    async def end(self, queries: Sequence[str], elapsed_ms: int) -> None: ...


class ThreadMessages(Protocol):
    """Источник сообщений треда, из которых собирается лента. Реализует
    чтение checkpoint'ов (agent), зовут сборка ленты из истории
    (TranscriptFeed) и реестр журналов вызовов (RemoteJournals).
    """

    @abstractmethod
    async def load(self, thread_id: str) -> Sequence[DialogMessage]: ...


class TurnHistory(Protocol):
    """Порт истории хода: записывает исход так, чтобы он пережил остановку и был виден
    модели в следующем ходе.
    """

    @abstractmethod
    async def remember(self, record: TurnRecord) -> None: ...


class SessionAgent(ThreadMessages, Protocol):
    """Агент сессии: ход на графе и состояние тредов, которое он ведёт.

    Реализует обёртка графа сессии (agent); собирает её сборка чата на
    сессию. Обработчик сообщения запускает на нём ход (answer), запись
    исхода хода (ThreadTurnHistory) и правка вопроса (ThreadRewind) читают и
    переписывают через него историю треда.
    """

    @abstractmethod
    def answer(
        self,
        thread_id: str,
        question: UserMessage,
        listeners: Sequence[ModelListener],
        stage: PrefetchStage,
    ) -> AsyncIterator[AnswerChunk]:
        """Ход на графе: вопрос уходит модели, поток отдаёт порции ответа.
        listeners получают события обращений к модели, stage — этап
        подготовки хода.
        """
        ...

    @abstractmethod
    async def remember(self, thread_id: str, record: TurnRecord) -> None:
        """Дописывает в историю треда запись оборванного хода."""
        ...

    @abstractmethod
    async def rewind(self, thread_id: str, question: UserMessage) -> None:
        """Приводит историю треда к состоянию «сразу после этого вопроса»:
        всё, что шло за вопросом с тем же id, убирается, сам вопрос
        заменяется присланным.
        """
        ...
