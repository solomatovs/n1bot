"""Лента чата из уведомлений ACP: перевод `session/update` в шаги ChatView.

Один переводчик (UpdateView) рисует и живой ход, чьи уведомления приходят из
шины от держателя, и историю, которую `session/load` воспроизводит тому же
процессу: живая лента и история — один компонент по одним данным.
Идентификаторы шагов выводятся из `messageId` и `toolCallId` уведомлений
теми же правилами ChatView.derive_id, что и у сохранённой истории:
вопрос и его ответы — id вопроса (`"{id}#{n}"` у n-го ответа хода),
рассуждения — id сообщения ассистента, вызов, график и элемент — id вызова.

Ошибки:
ValidationError — тело уведомления из шины не по схеме ACP либо запись
    `rawOutput` с ключами итога не проходит модель ToolUseResult.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)
from acp.schema import ContentChunk as AcpContentChunk
from pydantic import BaseModel, ConfigDict, TypeAdapter

from boba.chainlit.acp.client import SessionUpdate
from boba.chainlit.rendering.chat_view import ChatView, StepText
from boba.toolkit.calls import CallViews
from boba.toolkit.dag import JournalAddress, ToolCard, ToolUseResults
from boba.toolkit.result import ToolArtifact
from chainlit.step import Step

__all__ = [
    "JournalRegistry",
    "MetaKey",
    "ProgressMeta",
    "SessionUpdateCodec",
    "StopReasonName",
    "ToolMedia",
    "ToolStatusName",
    "UpdateView",
]

logger = logging.getLogger(__name__)


class MetaKey(StrEnum):
    """Расширения агента `boba acp` в `_meta` уведомлений."""

    PROGRESS = "boba/progress"
    TOOL_NAME = "boba/toolName"


class ToolStatusName(StrEnum):
    """Статусы вызова в `tool_call`/`tool_call_update`."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (ToolStatusName.COMPLETED, ToolStatusName.FAILED)


class StopReasonName(StrEnum):
    """Исходы хода из ответа `session/prompt`, которые лента различает."""

    END_TURN = "end_turn"
    CANCELLED = "cancelled"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"

    @classmethod
    def note(cls, stop_reason: str) -> str:
        """Пометка ленты о раннем конце хода; пустая — ход закончен штатно."""
        if stop_reason == cls.END_TURN:
            return ""

        if stop_reason == cls.CANCELLED:
            return StepText.STOPPED.value

        return f"the turn ended early: {stop_reason}"


class ProgressMeta(BaseModel):
    """Расширение `boba/progress`: отчёт сервера MCP о ходе вызова."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    message: str = ""
    progress: float
    total: float | None = None


class SessionUpdateCodec:
    """Уведомление ACP туда и обратно в JSON шины: держатель кладёт его в
    сообщение AgentUpdate, рендерер читает. Литералы вида (`sessionUpdate`)
    различают классы SDK сами."""

    def __init__(self) -> None:
        self._adapter: TypeAdapter[SessionUpdate] = TypeAdapter(SessionUpdate)

    def encode(self, update: SessionUpdate) -> dict[str, Any]:
        return update.model_dump(mode="json", by_alias=True, exclude_none=True)

    def decode(self, raw: Any) -> SessionUpdate:
        """Ошибки:
        ValidationError — тело не проходит схему уведомления.
        """
        return self._adapter.validate_python(raw)


class ToolMedia:
    """Типы содержимого аргументов инструментов серверов MCP по именам: по
    ним лента рисует вход шага (sql, bash, json). Один объект на процесс;
    карточки называет сборка приёмника по портам серверов профиля."""

    def __init__(self) -> None:
        self._views = CallViews()
        self._media: dict[str, Mapping[str, str]] = {}

    def shown(self, cards: Iterable[ToolCard]) -> None:
        for card in cards:
            self._media[card.name] = self._views.media_of(card.parameters)

    def of(self, name: str) -> Mapping[str, str] | None:
        """Типы аргументов инструмента; None — инструмент серверам неизвестен."""
        return self._media.get(name)


class JournalRegistry(Protocol):
    """Куда лента кладёт адрес журнала закончившегося вызова; реализует
    RemoteJournals (canvas.remote)."""

    @abstractmethod
    def remember(self, thread_id: str, call_id: str, address: JournalAddress) -> None:
        """Адрес журнала вызова call_id треда."""


class UpdateView:
    """Перевод уведомлений одной сессии в шаги ленты через ChatView.

    Создаётся рендерером шины (ChatRenderer) на тред и приёмником потока
    вкладки (AgentFeed) на воспроизведение истории. Ход открывается
    `begin_turn`/`question` (живой — по TurnStarted из шины, при
    воспроизведении — по `user_message_chunk`), уведомления идут в `apply`,
    конец хода — `finish_turn`.
    """

    STEP_FALLBACK_NAME: ClassVar[str] = StepText.TOOL.value

    def __init__(
        self,
        view: ChatView,
        thread_id: str,
        journals: JournalRegistry,
        media: ToolMedia,
    ) -> None:
        self._view = view
        self._thread_id = thread_id
        self._journals = journals
        self._media = media
        self._results = ToolUseResults()
        self._turn_key: str | None = None
        self._thought_id: str | None = None
        self._answer_id: str | None = None
        self._steps: dict[str, Step] = {}

    @property
    def view(self) -> ChatView:
        return self._view

    @property
    def turn_key(self) -> str | None:
        """Ключ открытого хода; None — ход не открыт."""
        return self._turn_key

    def open_steps(self) -> Sequence[Step]:
        """Шаги вызовов, у которых ещё нет итога."""
        return list(self._steps.values())

    def begin_turn(self, key: str) -> None:
        """Открывает ход: его ключ адресует контейнер и ответы."""
        self._turn_key = key
        self._thought_id = None
        self._answer_id = None
        self._steps.clear()
        self._view.begin_turn(key)

    async def question(self, text: str, key: str, author: str | None = None) -> None:
        """Вопрос пользователя: шаг с id сообщения chainlit."""
        await self._view.question(text, key, name=author)

    async def apply(self, update: SessionUpdate) -> None:
        """Уведомление сессии в ленту; служебные (usage, настройки) не рисуются."""
        if isinstance(update, UserMessageChunk):
            await self._user_chunk(update)
            return

        if isinstance(update, AgentThoughtChunk):
            await self._thought_chunk(update)
            return

        if isinstance(update, AgentMessageChunk):
            await self._message_chunk(update)
            return

        if isinstance(update, ToolCallStart):
            await self._tool_call(update)
            return

        if isinstance(update, ToolCallProgress):
            await self._tool_update(update)
            return

        logger.debug(
            "thread %s: update %s is not drawn", self._thread_id, type(update).__name__
        )

    async def finish_turn(self) -> None:
        """Ход закончен любым исходом: открытые узлы закрываются, кружок
        ожидания снимается."""
        await self._close_thinking()
        if self._answer_id is not None:
            await self._view.close_answer(self._turn_key)
            self._answer_id = None

        for step in list(self._steps.values()):
            await self._view.tool_stopped(step, StepText.FINISHED.value)

        self._steps.clear()
        await self._view.finish_turn()
        self._turn_key = None

    async def _user_chunk(self, update: UserMessageChunk) -> None:
        """Вопрос из воспроизведения: закрывает прежний ход и открывает свой."""
        text = self._text(update)
        if text is None:
            return

        key = update.message_id
        if key is None:
            logger.warning(
                "thread %s: user_message_chunk without messageId is skipped",
                self._thread_id,
            )
            return

        if self._turn_key is not None:
            await self.finish_turn()

        self.begin_turn(key)
        await self.question(text, key)

    async def _thought_chunk(self, update: AgentThoughtChunk) -> None:
        text = self._text(update)
        if text is None:
            return

        await self._view.model_answered()
        if self._thought_id is not None and self._thought_id != update.message_id:
            await self._close_thinking()

        if self._answer_id is not None and self._answer_id != update.message_id:
            await self._view.seal_answer()
            self._answer_id = None

        self._thought_id = update.message_id
        await self._view.stream_thinking(text, update.message_id)

    async def _message_chunk(self, update: AgentMessageChunk) -> None:
        text = self._text(update)
        if text is None:
            return

        await self._view.model_answered()
        await self._close_thinking()
        if self._answer_id is not None and self._answer_id != update.message_id:
            await self._view.seal_answer()

        self._answer_id = update.message_id
        await self._view.stream_answer(text, self._turn_key)

    async def _tool_call(self, update: ToolCallStart) -> None:
        await self._close_thinking()
        self._answer_id = None
        name = self._tool_name(update.field_meta, update.title)
        step = await self._view.tool_started(
            name,
            self._args(update.raw_input),
            update.tool_call_id,
            self._media.of(name),
        )
        self._steps[update.tool_call_id] = step

    async def _tool_update(self, update: ToolCallProgress) -> None:
        step = await self._step_of(update)
        if update.field_meta:
            await self._progress(step, update.field_meta)

        if update.status is None:
            return

        status = ToolStatusName(update.status)
        if not status.terminal:
            return

        self._steps.pop(update.tool_call_id, None)
        await self._tool_result(step, update, status)

    async def _step_of(self, update: ToolCallProgress) -> Step:
        """Шаг вызова; итог вызова без начала рисуется по полям итога."""
        step = self._steps.get(update.tool_call_id)
        if step is not None:
            return step

        name = self._tool_name(update.field_meta, update.title)
        step = await self._view.tool_started(
            name,
            self._args(update.raw_input),
            update.tool_call_id,
            self._media.of(name),
        )
        self._steps[update.tool_call_id] = step

        return step

    async def _progress(self, step: Step, meta: Mapping[str, Any]) -> None:
        raw = meta.get(MetaKey.PROGRESS.value)
        if raw is None:
            return

        progress = ProgressMeta.model_validate(raw)
        await self._view.tool_progressed(
            step, progress.progress, progress.total, progress.message
        )

    async def _tool_result(
        self, step: Step, update: ToolCallProgress, status: ToolStatusName
    ) -> None:
        result = self._results.of(update.raw_output)
        if result.journal is None:
            self._view.drop_journal(step)
        else:
            self._journals.remember(
                self._thread_id, update.tool_call_id, result.journal
            )

        artifact = ToolArtifact.revive(result.artifact)
        if artifact is not None:
            await self._view.tool_finished(step, artifact, update.tool_call_id)
            return

        if status is ToolStatusName.FAILED:
            await self._view.tool_failed(step, self._failure_text(update))
            return

        raw = result.artifact
        if raw is None:
            raw = self._content_text(update)

        await self._view.tool_finished(step, raw, update.tool_call_id)

    async def _close_thinking(self) -> None:
        if self._thought_id is None:
            return

        await self._view.close_thinking()
        self._thought_id = None

    def _failure_text(self, update: ToolCallProgress) -> str:
        text = self._content_text(update)
        if text:
            return text

        return f"tool call {update.tool_call_id} failed without a message"

    @staticmethod
    def _content_text(update: ToolCallProgress) -> str:
        if update.content is None:
            return ""

        parts: list[str] = []
        for item in update.content:
            if item.type != "content":
                continue

            block = item.content
            if isinstance(block, TextContentBlock):
                parts.append(block.text)

        return "\n".join(parts)

    @classmethod
    def _tool_name(cls, meta: Mapping[str, Any] | None, title: str | None) -> str:
        """Имя инструмента из расширения агента; чужой агент имени не шлёт —
        шаг называется заголовком вызова."""
        if meta is not None:
            name = meta.get(MetaKey.TOOL_NAME.value)
            if isinstance(name, str) and name:
                return name

        if title:
            return title

        return cls.STEP_FALLBACK_NAME

    @staticmethod
    def _args(raw_input: Any) -> Mapping[str, Any]:
        if isinstance(raw_input, Mapping):
            return raw_input

        return {}

    @staticmethod
    def _text(update: AcpContentChunk) -> str | None:
        """Текст блока куска; картинки и ресурсы лента не показывает."""
        content = update.content
        if isinstance(content, TextContentBlock):
            return content.text

        return None
