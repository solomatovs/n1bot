"""Показ сбоев вкладке, о которых агент сам рассказать не может: агент не
запустился, недоступен, отказал в методе, тред занят. Сбой, о котором знает
агент, приходит пометкой `notice` в ленту и лежит в его истории; здесь —
только то, чего в истории нет и быть не может.

Каталог UserFailures декларативно сопоставляет виду отказа ключ перевода
слов человеку (FailureText) и способ показа: всплывающее уведомление (TOAST)
или состояние агента вкладки (AGENT_STATE) — полоса над полем ввода с кнопкой
повтора, поле ввода заперто.
Переводчик отказа — UserFailures.of; показывает TabNotices.

Ошибки: наружу не выпускает ничего сверх ошибок отправки chainlit.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from chainlit.config import config as chainlit_config
from pydantic import BaseModel

from boba.aid.agent.chat import EditRefusal, QuestionEditError
from boba.aid.agent.client import (
    AgentFailure,
    AgentFailureKind,
    AgentProcessError,
    AgentRequestError,
)
from boba.aid.agent.commands import CommandError
from boba.aid.agent.settings import AgentSettingsError
from boba.aid.chat.ui_text import UiText
from boba.aid.infra.session import current_session
from boba.aid.rendering.errors import show_error
from boba.canvas.canvas import CanvasError, CanvasErrorKind
from boba.identity.errors import RefusalError
from boba.identity.locks import LockBusyError
from boba.identity.session import LogLine
from boba.mcp_client.client import McpClientError, McpUnansweredError

__all__ = [
    "AgentState",
    "FailureChannel",
    "FailureText",
    "TabAction",
    "TabEvent",
    "TabNotices",
    "UserFailure",
    "UserFailures",
]

logger = logging.getLogger(__name__)


class TabEvent(StrEnum):
    """События сокета, которые aid шлёт вкладке сверх событий chainlit; их
    принимает overlay фронта (useChatSession)."""

    HISTORY_LOADED = "history_loaded"
    """История треда воспроизведена (или не загрузилась): вкладка снимает
    индикатор загрузки, который поставила на first_interaction возврата."""

    AGENT_STATE = "agent_state"
    """Доступен ли агент вкладки: недоступный запирает поле ввода полосой с
    текстом сбоя и кнопкой повтора (TabAction.RETRY_AGENT)."""


class TabAction(StrEnum):
    """Действия, которые вкладка шлёт серверу сверх действий шагов."""

    RETRY_AGENT = "agent_retry"
    """Кнопка повтора на полосе недоступного агента."""


class FailureChannel(StrEnum):
    """Куда уходит сбой: всплывающее уведомление или состояние агента
    вкладки (полоса над полем ввода)."""

    TOAST = "toast"
    AGENT_STATE = "agent_state"


class FailureText(StrEnum):
    """Ключи слов человеку о сбое в переводах chainlit: что случилось и что
    делать. Всплывающее уведомление сервер переводит сам на язык вкладки,
    полосу агента переводит фронт по ключу."""

    START_FAILED = "aid.failure.startFailed"
    UNREACHABLE = "aid.failure.unreachable"
    SIGN_IN_REFUSED = "aid.failure.signInRefused"
    BAD_ANSWER = "aid.failure.badAnswer"
    NO_ANSWER = "aid.failure.noAnswer"
    CONNECTION_LOST = "aid.failure.connectionLost"
    REFUSED = "aid.failure.refused"
    PROFILE_MISSING = "aid.failure.profileMissing"
    BUSY = "aid.failure.busy"
    SETTINGS = "aid.failure.settings"
    SERVERS = "aid.failure.servers"
    UPLOADS = "aid.failure.uploads"
    CANVAS = "aid.failure.canvas"
    COMMAND = "aid.failure.command"
    REDRAW = "aid.failure.redraw"
    NOT_EDITED = "aid.failure.notEdited"
    NOT_LOADED = "aid.failure.notLoaded"
    NO_BRANCHING = "aid.failure.noBranching"
    UNKNOWN = "aid.failure.unknown"


@dataclass(frozen=True)
class FailureEntry:
    """Запись каталога: способ показа и ключ слов человеку; собеседник,
    действие и причина уходят короткой подробностью под текстом."""

    channel: FailureChannel
    text: FailureText

    def render(self, failure: AgentFailure) -> UserFailure:
        detail = f"{failure.peer}, {failure.action}: {failure.cause}"

        return UserFailure(channel=self.channel, text=self.text, detail=detail)


@dataclass(frozen=True)
class UserFailure:
    """Сбой для вкладки: способ показа, ключ слов человеку и короткая
    техническая подробность (пусто — её нет)."""

    channel: FailureChannel
    text: FailureText
    detail: str

    def toast(self, texts: UiText) -> str:
        """Текст всплывающего уведомления на языке texts: подробность — в
        скобках в конце."""
        words = texts.text(self.text.value)
        if not self.detail:
            return words

        return f"{words} ({self.detail})"


class UserFailures:
    """Каталог сбоев вкладки и переводчик исключений пути агента в них.
    Новый вид отказа — одна запись ENTRIES, вид в AgentFailureKind и ключ
    FailureText с переводами. Держит его в поле TabNotices."""

    ENTRIES: ClassVar[Mapping[AgentFailureKind, FailureEntry]] = {
        AgentFailureKind.START_FAILED: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.START_FAILED
        ),
        AgentFailureKind.UNREACHABLE: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.UNREACHABLE
        ),
        AgentFailureKind.SIGN_IN_REFUSED: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.SIGN_IN_REFUSED
        ),
        AgentFailureKind.BAD_ANSWER: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.BAD_ANSWER
        ),
        AgentFailureKind.NO_ANSWER: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.NO_ANSWER
        ),
        AgentFailureKind.CONNECTION_LOST: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.CONNECTION_LOST
        ),
        AgentFailureKind.REFUSED: FailureEntry(
            FailureChannel.TOAST, FailureText.REFUSED
        ),
        AgentFailureKind.PROFILE_MISSING: FailureEntry(
            FailureChannel.AGENT_STATE, FailureText.PROFILE_MISSING
        ),
    }
    EDITS: ClassVar[Mapping[EditRefusal, FailureText]] = {
        EditRefusal.NOT_EDITED: FailureText.NOT_EDITED,
        EditRefusal.NOT_LOADED: FailureText.NOT_LOADED,
        EditRefusal.NO_BRANCHING: FailureText.NO_BRANCHING,
    }
    PANEL_EXPLAINS: ClassVar[frozenset[str]] = frozenset(
        {CanvasErrorKind.NO_VIEWER.value, CanvasErrorKind.RENDER_FAILED.value}
    )
    """Отказы панели, которые она объясняет сама на месте содержимого."""

    def of(self, exc: Exception) -> UserFailure:
        """Сбой вкладки по исключению пути агента."""
        if isinstance(exc, AgentProcessError | AgentRequestError):
            return self.ENTRIES[exc.failure.kind].render(exc.failure)

        if isinstance(exc, AgentSettingsError):
            return UserFailure(
                channel=FailureChannel.AGENT_STATE,
                text=FailureText.SETTINGS,
                detail=str(exc),
            )

        return self._toast(exc)

    def _toast(self, exc: Exception) -> UserFailure:
        """Сбой, после которого агент вкладки доступен: всплывающим
        уведомлением."""
        if isinstance(exc, QuestionEditError):
            text = self.EDITS[exc.refusal]
            return UserFailure(channel=FailureChannel.TOAST, text=text, detail="")

        if isinstance(exc, LockBusyError):
            return UserFailure(
                channel=FailureChannel.TOAST, text=FailureText.BUSY, detail=""
            )

        if isinstance(exc, McpClientError):
            return UserFailure(
                channel=FailureChannel.TOAST,
                text=FailureText.SERVERS,
                detail=self._brief(exc),
            )

        if isinstance(exc, CommandError):
            return UserFailure(
                channel=FailureChannel.TOAST, text=FailureText.COMMAND, detail=str(exc)
            )

        return UserFailure(
            channel=FailureChannel.TOAST, text=FailureText.UNKNOWN, detail=""
        )

    def uploads_off(self, exc: McpClientError) -> UserFailure:
        """Кнопка загрузки файлов скрыта: сервер профиля не ответил."""
        return UserFailure(
            channel=FailureChannel.TOAST,
            text=FailureText.UPLOADS,
            detail=self._brief(exc),
        )

    def explained_in_panel(self, exc: RefusalError) -> bool:
        """Отказ панели, который она показывает сама вместо содержимого."""
        if not isinstance(exc, CanvasError):
            return False

        return exc.kind in self.PANEL_EXPLAINS

    @staticmethod
    def _brief(exc: McpClientError) -> str:
        if isinstance(exc, McpUnansweredError):
            return exc.brief

        return ""


class AgentState(BaseModel):
    """Событие TabEvent.AGENT_STATE: доступен ли агент вкладки, ключ слов
    человеку о сбое (FailureText, его переводит фронт) и подробность; у
    доступного агента ключ и подробность пусты."""

    available: bool
    key: str
    detail: str


class TabNotices:
    """Показ сбоев вкладке текущей сессии chainlit: всплывающее уведомление
    на языке вкладки или состояние агента вкладки ключом перевода. Создают
    обработчики вкладки (TabAgent) и действия панели внутри сессии;
    переводит исключения каталог UserFailures, слова берёт UiText."""

    def __init__(self) -> None:
        self._failures = UserFailures()
        self._texts = UiText(Path(chainlit_config.root), current_session().language)

    async def failed(self, exc: Exception) -> None:
        """Сбой exc — вкладке способом из каталога; полный текст — в журнал."""
        logger.warning("tab failure %s: %s", type(exc).__name__, LogLine.safe(str(exc)))
        await self._show(self._failures.of(exc))

    async def uploads_off(self, exc: McpClientError) -> None:
        """Кнопка загрузки скрыта недоступным сервером профиля."""
        await self._show(self._failures.uploads_off(exc))

    async def canvas_refused(self, exc: RefusalError) -> None:
        """Панель не открыла файл по клику. Отказ, который панель объясняет
        сама (нет вьювера, диаграмма не нарисовалась), уведомлением не
        дублируется; прочие — всплывающим уведомлением с текстом отказа."""
        if self._failures.explained_in_panel(exc):
            logger.info("canvas: the panel explains the refusal itself: %s", exc)
            return

        failure = UserFailure(
            channel=FailureChannel.TOAST, text=FailureText.CANVAS, detail=str(exc)
        )
        await self._show(failure)

    async def toast(self, text: FailureText, detail: str) -> None:
        """Сбой вне хода, о котором агент не знает: всплывающим уведомлением
        со словами человеку и короткой подробностью."""
        failure = UserFailure(channel=FailureChannel.TOAST, text=text, detail=detail)
        await self._show(failure)

    async def agent_ready(self) -> None:
        """Агент вкладки отвечает или новое действие пользователя ждёт итога:
        полоса снимается, поле ввода открыто."""
        await self._agent_state(AgentState(available=True, key="", detail=""))

    async def _show(self, failure: UserFailure) -> None:
        if failure.channel is FailureChannel.TOAST:
            await show_error(failure.toast(self._texts))
            return

        state = AgentState(
            available=False, key=failure.text.value, detail=failure.detail
        )
        await self._agent_state(state)

    async def _agent_state(self, state: AgentState) -> None:
        await current_session().emit(TabEvent.AGENT_STATE.value, state.model_dump())
