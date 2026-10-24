"""Показ сбоев вкладке, о которых агент сам рассказать не может: агент не
запустился, недоступен, отказал в методе, тред занят. Сбой, о котором знает
агент, приходит пометкой `notice` в ленту и лежит в его истории; здесь —
только то, чего в истории нет и быть не может.

Каталог UserFailures декларативно сопоставляет виду отказа текст и способ
показа: всплывающее уведомление (TOAST) или состояние агента вкладки
(AGENT_STATE) — полоса над полем ввода с кнопкой повтора, поле ввода заперто.
Переводчик отказа — UserFailures.of; показывает TabNotices.

Ошибки: наружу не выпускает ничего сверх ошибок отправки chainlit.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from boba.aid.agent.client import (
    AcpClientError,
    AgentFailure,
    AgentFailureKind,
    AgentProcessError,
    AgentRequestError,
)
from boba.aid.agent.commands import CommandError
from boba.aid.agent.settings import AgentSettingsError
from boba.aid.infra.session import current_session
from boba.aid.rendering.errors import show_error
from boba.canvas.canvas import CanvasError, CanvasErrorKind
from boba.identity.errors import RefusalError
from boba.identity.locks import LockBusyError
from boba.identity.session import LogLine
from boba.mcp_client.client import McpClientError, McpUnansweredError

__all__ = [
    "FailureChannel",
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


@dataclass(frozen=True)
class FailureEntry:
    """Запись каталога: способ показа и слова человеку — что случилось и
    что делать; собеседник, действие и причина уходят короткой подробностью
    под текстом."""

    channel: FailureChannel
    text: str

    def render(self, failure: AgentFailure) -> UserFailure:
        detail = f"{failure.peer}, {failure.action}: {failure.cause}"

        return UserFailure(channel=self.channel, text=self.text, detail=detail)


@dataclass(frozen=True)
class UserFailure:
    """Сбой для вкладки: способ показа, слова человеку по-английски и
    короткая техническая подробность (пусто — её нет)."""

    channel: FailureChannel
    text: str
    detail: str

    def toast(self) -> str:
        """Текст всплывающего уведомления: подробность — в скобках в конце."""
        if not self.detail:
            return self.text

        return f"{self.text} ({self.detail})"


class UserFailures:
    """Каталог сбоев вкладки и переводчик исключений пути агента в них.
    Новый вид отказа — одна запись ENTRIES и вид в AgentFailureKind. Держит
    его в поле TabNotices."""

    ENTRIES: ClassVar[Mapping[AgentFailureKind, FailureEntry]] = {
        AgentFailureKind.START_FAILED: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The agent couldn't be started, so the chat can't answer right now. "
            "Try again with the button on the right; if it keeps failing, let the "
            "administrator know.",
        ),
        AgentFailureKind.UNREACHABLE: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The agent can't be reached right now, so the chat can't answer. "
            "Try again in a moment with the button on the right; if it keeps "
            "failing, let the administrator know.",
        ),
        AgentFailureKind.SIGN_IN_REFUSED: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The agent didn't accept the sign-in of this chat, so the chat can't "
            "answer. Reload the page to sign in again; if it keeps failing, let "
            "the administrator know.",
        ),
        AgentFailureKind.BAD_ANSWER: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The agent answered in a way the chat doesn't understand, so the chat "
            "can't answer right now. Try again with the button on the right; if it "
            "keeps failing, let the administrator know.",
        ),
        AgentFailureKind.NO_ANSWER: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The agent didn't respond in time, so the chat can't answer right "
            "now. Try again in a moment with the button on the right.",
        ),
        AgentFailureKind.CONNECTION_LOST: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The connection to the agent was lost. Reconnect with the button on the "
            "right; the answer that was in progress may need to be asked for again.",
        ),
        AgentFailureKind.REFUSED: FailureEntry(
            FailureChannel.TOAST,
            "The agent couldn't complete this request. Try again; if it keeps "
            "failing, let the administrator know.",
        ),
        AgentFailureKind.PROFILE_MISSING: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The agent doesn't offer the chat profile of this tab, so the chat "
            "can't answer. Pick another profile or ask the administrator.",
        ),
    }
    BUSY: ClassVar[str] = (
        "This chat is answering in another tab or window right now. Wait for "
        "that answer to finish, then send your message again."
    )
    SETTINGS: ClassVar[str] = (
        "The agent can't use the settings of this chat profile, so the chat "
        "can't answer. Let the administrator know."
    )
    SERVERS: ClassVar[str] = (
        "One of the MCP servers of this chat isn't answering, so its tools "
        "can't be used right now. Try again in a moment."
    )
    UPLOADS: ClassVar[str] = (
        "File uploads are turned off in this chat because one of its MCP "
        "servers isn't answering, so the chat can't tell where to store files. "
        "Reload the page to try again."
    )
    CANVAS: ClassVar[str] = "The file couldn't be opened in the side panel."
    PANEL_EXPLAINS: ClassVar[frozenset[str]] = frozenset(
        {CanvasErrorKind.NO_VIEWER.value, CanvasErrorKind.RENDER_FAILED.value}
    )
    """Отказы панели, которые она объясняет сама на месте содержимого."""
    UNKNOWN: ClassVar[str] = (
        "Something went wrong in the chat. Try again; if it keeps happening, "
        "let the administrator know."
    )

    def of(self, exc: Exception) -> UserFailure:
        """Сбой вкладки по исключению пути агента."""
        if isinstance(exc, AgentProcessError | AgentRequestError):
            return self.ENTRIES[exc.failure.kind].render(exc.failure)

        if isinstance(exc, LockBusyError):
            return UserFailure(channel=FailureChannel.TOAST, text=self.BUSY, detail="")

        if isinstance(exc, AgentSettingsError):
            return UserFailure(
                channel=FailureChannel.AGENT_STATE, text=self.SETTINGS, detail=str(exc)
            )

        if isinstance(exc, McpClientError):
            return UserFailure(
                channel=FailureChannel.TOAST, text=self.SERVERS, detail=self._brief(exc)
            )

        if isinstance(exc, AcpClientError | CommandError):
            return UserFailure(channel=FailureChannel.TOAST, text=str(exc), detail="")

        return UserFailure(channel=FailureChannel.TOAST, text=self.UNKNOWN, detail="")

    def uploads_off(self, exc: McpClientError) -> UserFailure:
        """Кнопка загрузки файлов скрыта: сервер профиля не ответил."""
        return UserFailure(
            channel=FailureChannel.TOAST, text=self.UPLOADS, detail=self._brief(exc)
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


class TabNotices:
    """Показ сбоев вкладке текущей сессии chainlit: всплывающее уведомление
    или состояние агента вкладки. Создают обработчики вкладки (TabAgent) и
    действия панели; переводит исключения каталог UserFailures."""

    def __init__(self) -> None:
        self._failures = UserFailures()

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
            channel=FailureChannel.TOAST, text=self._failures.CANVAS, detail=str(exc)
        )
        await self._show(failure)

    async def toast(self, text: str, detail: str) -> None:
        """Сбой вне хода, о котором агент не знает: всплывающим уведомлением
        со словами человеку и короткой подробностью."""
        failure = UserFailure(channel=FailureChannel.TOAST, text=text, detail=detail)
        await self._show(failure)

    async def agent_ready(self) -> None:
        """Агент вкладки отвечает: полоса снимается, поле ввода открыто."""
        await self._agent_state(available=True, text="", detail="")

    async def _show(self, failure: UserFailure) -> None:
        if failure.channel is FailureChannel.TOAST:
            await show_error(failure.toast())
            return

        await self._agent_state(
            available=False, text=failure.text, detail=failure.detail
        )

    async def _agent_state(self, available: bool, text: str, detail: str) -> None:
        payload = {"available": available, "text": text, "detail": detail}
        await current_session().emit(TabEvent.AGENT_STATE.value, payload)
