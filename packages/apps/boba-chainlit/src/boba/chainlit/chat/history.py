"""История треда: replay в ленту, запись исхода хода и откат к вопросу.

Лента собирается из сообщений диалога тем же ChatView, что и live: id шагов
детерминированы, поэтому история и стрим дают одинаковую раскладку. Сами
сообщения читает и пишет агент сессии и чтение checkpoint'ов (каталог agent)
за протоколами dialog. Исходы хода пишет ThreadTurnHistory; правка вопроса
приходит обычным on_message, и ThreadRewind усекает историю и вложения до
состояния «сразу после вопроса».

Ошибки: своих не выпускает; ошибки агента сессии и слоя данных уходят наверх.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from boba.chainlit.chat.dialog import (
    AssistantMessage,
    Attachment,
    DialogMessage,
    SessionAgent,
    ThreadMessages,
    ToolReply,
    TurnHistory,
    TurnMark,
    TurnRecord,
    UserMessage,
)
from boba.chainlit.rendering.chat_view import (
    ChatView,
    RecordingSink,
    StepRole,
    StepText,
    StreamableTools,
    TurnDraft,
)
from boba.toolkit.calls import CallIdPrefix
from boba.toolkit.dag import DagNode, WorkflowResult
from chainlit.data.base import BaseDataLayer
from chainlit.step import StepDict

__all__ = [
    "ConversationTranscript",
    "InterruptedTurn",
    "RewindPlan",
    "ThreadRewind",
    "ThreadTurnHistory",
    "TranscriptFeed",
]

logger = logging.getLogger(__name__)


class ThreadTurnHistory(TurnHistory):
    """Реализация TurnHistory записью в историю треда через агента сессии:
    запись читает и лента, и сам агент в следующем ходе. Создаётся
    обработчиком сообщения на ход и отдаётся ходу (ChatTurn).
    """

    def __init__(self, agent: SessionAgent, thread_id: str) -> None:
        self._agent = agent
        self._thread_id = thread_id

    async def remember(self, record: TurnRecord) -> None:
        await self._agent.remember(self._thread_id, record)
        logger.info("history record written: %s", record.mark.value)


class InterruptedTurn:
    """Дописывает в историю треда отметку STOPPED о ходе, который сторож закрыл за
    умершего держателя; пишет тот инстанс, у которого есть агент сессии треда, и не
    дублирует уже существующую отметку.
    """

    def __init__(self, agent: SessionAgent, thread_id: str) -> None:
        self._agent = agent
        self._thread_id = thread_id
        self._history = ThreadTurnHistory(agent, thread_id)

    async def remember(self, reason: str) -> bool:
        """Дописывает отметку STOPPED с причиной и возвращает True; False — отметка
        уже стоит.
        """
        messages = await self._agent.load(self._thread_id)
        if self._stopped(messages):
            return False

        record = TurnRecord(content=f"_{reason}_", mark=TurnMark.STOPPED)
        await self._history.remember(record)
        return True

    @staticmethod
    def _stopped(messages: Sequence[DialogMessage]) -> bool:
        """История уже кончается отметкой остановки."""
        if not messages:
            return False

        last = messages[-1]
        if not isinstance(last, AssistantMessage):
            return False

        return last.mark is TurnMark.STOPPED


class TranscriptFeed:
    """Лента треда для слоя данных: история checkpointer'а разворачивается в шаги.

    Реализует ThreadFeed структурно: наследовать нельзя — слой отрисовки не
    зависит от слоя данных, зависимость идёт в обратную сторону.
    """

    def __init__(self, messages: ThreadMessages, journals: StreamableTools) -> None:
        self._messages = messages
        self._journals = journals

    async def steps(self, thread_id: str, user_name: str | None) -> Sequence[StepDict]:
        messages = await self._messages.load(thread_id)
        if not messages:
            return []

        sink = RecordingSink()
        view = ChatView(thread_id, sink, self._journals, user_name=user_name)
        await ConversationTranscript(messages, view).replay()
        return sink.steps

    async def journaled(self, thread_id: str) -> frozenset[str]:
        """Вызовы треда, журнал которых можно прочитать: их ответ в истории
        несёт адрес журнала. У остальных шагов кнопки журнала нет."""
        calls: set[str] = set()
        for message in await self._messages.load(thread_id):
            if not isinstance(message, ToolReply):
                continue

            calls.update(message.journals())

        return frozenset(calls)


class ConversationTranscript:
    """Разворачивает сообщения диалога в шаги ленты. Создаётся сборкой ленты
    из истории (TranscriptFeed) на одно чтение треда и рисует тем же ChatView,
    что и живой ход.
    """

    def __init__(self, messages: Sequence[DialogMessage], view: ChatView) -> None:
        self._messages = messages
        self._view = view
        self._pending: dict[str, DagNode] = {}
        self._turn = TurnDraft()
        self._stage_queries: list[str] = []
        self._stage_elapsed = 0

    async def replay(self) -> None:
        for index, message in enumerate(self._messages):
            key = message.id
            if not key:
                key = f"#{index}"

            match message:
                case UserMessage():
                    await self._close_stage()
                    self._view.begin_turn(message.id)
                    self._pending.clear()
                    self._turn = TurnDraft(key=message.id)
                    await self._view.question(message.text, message.id)
                case ToolReply():
                    await self._open_stage(message)
                    await self._tool(message, key)
                case AssistantMessage():
                    prepares = self._prepares(message)
                    if prepares:
                        self._stage_elapsed = message.prefetch_elapsed_ms

                    if not prepares:
                        await self._close_stage()

                    await self._assistant(message, key)

        await self._close_stage()

    @staticmethod
    def _prepares(message: AssistantMessage) -> bool:
        """Сообщение подготовки: его вызовы рисуются этапом, а не ходом."""
        for call in message.calls:
            if not CallIdPrefix.PREFETCH.marks(call.key):
                continue

            return True

        return False

    async def _open_stage(self, message: ToolReply) -> None:
        """Первый ответ подготовки открывает этап, остальные копят запросы."""
        if not CallIdPrefix.PREFETCH.marks(message.call_id):
            return

        if self._view.stage_step is None:
            await self._view.begin_stage(
                StepText.PREFETCH.value, StepText.REPHRASING.value
            )

        call = self._pending.get(message.call_id)
        if call is None:
            return

        query = call.args.get("query")
        if not query:
            return

        text = str(query)
        if text in self._stage_queries:
            return

        self._stage_queries.append(text)

    async def _close_stage(self) -> None:
        """Закрывает этап подготовки; без открытого этапа закрывать нечего."""
        if self._view.stage_step is None:
            return

        await self._view.end_stage(self._stage_queries, self._stage_elapsed)
        self._stage_queries = []
        self._stage_elapsed = 0

    async def _assistant(self, message: AssistantMessage, key: str) -> None:
        if message.mark is TurnMark.ERROR:
            await self._view.error(message.text, self._answer_key(key))
            return

        if message.mark is TurnMark.STOPPED:
            await self._stopped(message, key)
            return

        if message.reasoning:
            await self._view.thinking(message.reasoning, key)

        await self._spend(message, key)

        for call in message.calls:
            self._pending[call.key] = call

        if message.text:
            await self._view.answer(message.text, self._answer_key(key))

    async def _tool(self, message: ToolReply, key: str) -> None:
        call = self._pending.pop(message.call_id, None)

        outcome = message.outcome
        if outcome is not None and isinstance(outcome.artifact, WorkflowResult):
            await self._workflow(outcome.artifact, message.media)
            return

        name = message.name
        if not name and call is not None:
            name = call.tool
        if not name:
            name = StepText.TOOL.value

        args: Mapping[str, Any] | None = None
        if call is not None:
            args = call.args

        call_key = message.call_id
        if not call_key:
            call_key = key

        media: Mapping[str, str] | None = None
        if message.media is not None:
            media = message.media.get(call_key)

        step = await self._view.tool_started(name, args, call_key, media)

        # запись старой истории без результата семейства рисуется как есть
        if outcome is None:
            await self._view.tool_finished(step, message.raw, message.call_id)
            return

        if outcome.errored:
            await self._view.tool_failed(step, outcome.artifact.chat_view().markdown)
            return

        await self._view.tool_finished(step, outcome.artifact, message.call_id)

    async def _workflow(
        self,
        result: WorkflowResult,
        stored: Mapping[str, Mapping[str, str]] | None,
    ) -> None:
        """Итог вызова workflow шагами его узлов — как их рисует живой ход:
        шаг на узел под идентификатором вызова узла; stored — типы
        содержимого аргументов узлов из записи истории."""
        for node in result.nodes:
            media: Mapping[str, str] | None = None
            if stored is not None:
                media = stored.get(node.call_id)

            step = await self._view.tool_started(
                node.tool, node.args, node.call_id, media
            )

            if node.errored:
                await self._view.tool_failed(step, node.result.chat_view().markdown)
                continue

            await self._view.tool_finished(step, node.result, node.call_id)

    async def _spend(self, message: AssistantMessage, key: str) -> None:
        """Расход прогона из записи истории: usage хранится в самом сообщении."""
        usage = message.usage
        if not usage.counted:
            return

        await self._view.tokens_spent(
            key,
            usage.input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
        )

    def _answer_key(self, key: str) -> str:
        """Ключ очередного ответа хода; вне хода адресуемся самим сообщением."""
        turn_key = self._turn.next_answer_key()
        if turn_key is None:
            return key

        return turn_key

    async def _stopped(self, message: AssistantMessage, key: str) -> None:
        """Прерванный ход: пометка остановки уже вшита в текст записи."""
        if message.reasoning:
            await self._view.thinking(message.reasoning, key)

        await self._spend(message, key)

        if message.text:
            await self._view.answer(message.text, self._answer_key(key))


@dataclass(frozen=True)
class RewindPlan:
    """Что убрать при откате — сообщения истории и вложения их шагов — и что
    сохранить: вложения самого правленого вопроса.
    """

    remove_ids: Sequence[str] = ()
    element_ids: Sequence[str] = ()
    attachments: Sequence[Attachment] = ()

    def __bool__(self) -> bool:
        if self.remove_ids:
            return True

        return bool(self.element_ids)


class ThreadRewind:
    """Приводит историю треда к состоянию «сразу после этого вопроса».

    Создаётся обработчиком сообщения на ход: по истории агента сессии
    узнаёт, что пришла правка вопроса, убирает из слоя данных вложения
    шагов, шедших за вопросом, и просит агента переписать историю.
    """

    def __init__(
        self,
        agent: SessionAgent,
        data_layer: BaseDataLayer,
        thread_id: str,
    ) -> None:
        self._agent = agent
        self._data_layer = data_layer
        self._thread_id = thread_id

    async def is_edit(self, message_id: str) -> bool:
        """Вопрос с таким id уже в истории — значит пришла правка."""
        messages = await self._agent.load(self._thread_id)

        return self._asked(messages, message_id) is not None

    def plan(self, messages: Sequence[DialogMessage], message_id: str) -> RewindPlan:
        """Хвост после вопроса: id сообщений и id их вложений."""
        index = self._asked(messages, message_id)
        if index is None:
            return RewindPlan()

        question = messages[index]
        tail = messages[index + 1 :]

        attachments: Sequence[Attachment] = ()
        if isinstance(question, UserMessage):
            attachments = question.attachments

        return RewindPlan(
            remove_ids=tuple(self._ids(tail)),
            element_ids=tuple(self._element_ids(tail)),
            attachments=attachments,
        )

    async def apply(self, message_id: str, content: str) -> RewindPlan:
        """Удалить хвост и его вложения, поставить вопросу новый текст."""
        messages = await self._agent.load(self._thread_id)
        rewind = self.plan(messages, message_id)

        for element_id in rewind.element_ids:
            await self._data_layer.delete_element(element_id, self._thread_id)

        # вложения правленого вопроса остаются с ним: правится текст, не файлы
        question = UserMessage(
            id=message_id, text=content, attachments=rewind.attachments
        )
        await self._agent.rewind(self._thread_id, question)
        return rewind

    @staticmethod
    def _asked(messages: Sequence[DialogMessage], message_id: str) -> int | None:
        """Позиция вопроса с этим id в истории; None — такого вопроса нет."""
        for position, message in enumerate(messages):
            if not isinstance(message, UserMessage):
                continue

            if message.id != message_id:
                continue

            return position

        return None

    @staticmethod
    def _ids(messages: Sequence[DialogMessage]) -> Iterator[str]:
        for message in messages:
            if message.id:
                yield message.id

    def _element_ids(self, messages: Sequence[DialogMessage]) -> Iterator[str]:
        """Вложения шагов вызовов инструментов: их id выводится из вызова."""
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue

            for call in message.calls:
                element_id = ChatView.derive_id(
                    self._thread_id, call.key, StepRole.ELEMENT
                )
                if element_id:
                    yield element_id
