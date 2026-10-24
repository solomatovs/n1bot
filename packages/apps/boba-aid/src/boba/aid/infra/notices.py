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
from boba.aid.agent.settings import AgentSettingsError
from boba.aid.infra.session import current_session
from boba.aid.rendering.errors import show_error
from boba.identity.locks import LockBusyError
from boba.identity.session import LogLine
from boba.mcp_client.client import McpClientError

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
    """Запись каталога: способ показа и текст с подстановкой собеседника,
    действия и причины."""

    channel: FailureChannel
    template: str

    def render(self, failure: AgentFailure) -> str:
        return self.template.format(
            peer=failure.peer, action=failure.action, cause=failure.cause
        )


@dataclass(frozen=True)
class UserFailure:
    """Сбой для вкладки: способ показа и текст по-английски."""

    channel: FailureChannel
    text: str


class UserFailures:
    """Каталог сбоев вкладки и переводчик исключений пути агента в них.
    Новый вид отказа — одна запись ENTRIES и вид в AgentFailureKind. Держит
    его в поле TabNotices."""

    ENTRIES: ClassVar[Mapping[AgentFailureKind, FailureEntry]] = {
        AgentFailureKind.START_FAILED: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The {peer} could not be started: {cause}.",
        ),
        AgentFailureKind.UNREACHABLE: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The {peer} is unreachable ({action}): {cause}.",
        ),
        AgentFailureKind.SIGN_IN_REFUSED: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The {peer} refused the sign-in of this chat ({action}): {cause}.",
        ),
        AgentFailureKind.BAD_ANSWER: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The {peer} answered {action} not by the agent protocol: {cause}.",
        ),
        AgentFailureKind.NO_ANSWER: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The {peer} did not answer {action}: {cause}.",
        ),
        AgentFailureKind.CONNECTION_LOST: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The connection to the {peer} was lost during {action}: {cause}.",
        ),
        AgentFailureKind.REFUSED: FailureEntry(
            FailureChannel.TOAST,
            "The {peer} refused {action}: {cause}.",
        ),
        AgentFailureKind.PROFILE_MISSING: FailureEntry(
            FailureChannel.AGENT_STATE,
            "The {peer} cannot serve the chat profile of this tab: {cause}.",
        ),
    }

    def of(self, exc: Exception) -> UserFailure:
        """Сбой вкладки по исключению пути агента."""
        if isinstance(exc, AgentProcessError | AgentRequestError):
            entry = self.ENTRIES[exc.failure.kind]
            return UserFailure(channel=entry.channel, text=entry.render(exc.failure))

        if isinstance(exc, LockBusyError):
            text = (
                "This thread is busy: a turn of it is running in another tab or "
                f"window ({exc}); ask again when it ends."
            )
            return UserFailure(channel=FailureChannel.TOAST, text=text)

        if isinstance(exc, AgentSettingsError):
            text = f"The agent cannot use the chat profile of this tab: {exc}"
            return UserFailure(channel=FailureChannel.AGENT_STATE, text=text)

        if isinstance(exc, McpClientError):
            text = f"The MCP servers of the chat profile did not answer: {exc}"
            return UserFailure(channel=FailureChannel.TOAST, text=text)

        if isinstance(exc, AcpClientError):
            return UserFailure(channel=FailureChannel.TOAST, text=str(exc))

        text = f"The chat failed: {type(exc).__name__}: {exc}"
        return UserFailure(channel=FailureChannel.TOAST, text=text)


class TabNotices:
    """Показ сбоев вкладке текущей сессии chainlit: всплывающее уведомление
    или состояние агента вкладки. Создают обработчики вкладки (TabAgent) и
    действия панели; переводит исключения каталог UserFailures."""

    def __init__(self) -> None:
        self._failures = UserFailures()

    async def failed(self, exc: Exception) -> None:
        """Сбой exc — вкладке способом из каталога."""
        failure = self._failures.of(exc)
        if failure.channel is FailureChannel.TOAST:
            await show_error(failure.text)
            return

        logger.warning(
            "agent of the tab is unavailable: %s", LogLine.safe(failure.text)
        )
        await self._agent_state(available=False, text=failure.text)

    async def toast(self, text: str) -> None:
        """Сбой вне хода, о котором агент не знает: всплывающим уведомлением."""
        await show_error(text)

    async def agent_ready(self) -> None:
        """Агент вкладки отвечает: полоса снимается, поле ввода открыто."""
        await self._agent_state(available=True, text="")

    async def _agent_state(self, available: bool, text: str) -> None:
        payload = {"available": available, "text": text}
        await current_session().emit(TabEvent.AGENT_STATE.value, payload)
